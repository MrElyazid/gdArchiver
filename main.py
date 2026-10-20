import re
import sys
import time
import html
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse, urljoin, urlencode, parse_qs, unquote
import requests
from bs4 import BeautifulSoup
import csv
import gdown
import argparse
from pathlib import Path, PurePosixPath

import viewonly


# google sheets, slides, docs share the same hostname docs.google.com
GOOGLE_HOSTS = ("drive.google.com", "docs.google.com")

# drive ids are 25-50 chars, but old-style ids are kept permissive (they contain -)
GOOGLE_ID = r"[0-9A-Za-z_-]+"

# thanks to https://github.com/wkentaro/gdown/blob/main/gdown/parse_url.py#L31
FILE_PATH_PATTERNS = [
    rf"^/file/(?:u/\d+/)?d/({GOOGLE_ID})(?:/(?:view|edit|preview|htmlview|share_link))?/?$",
    rf"^/document/(?:u/\d+/)?d/({GOOGLE_ID})(?:/(?:view|edit|preview|htmlview))?/?$",
    rf"^/presentation/(?:u/\d+/)?d/({GOOGLE_ID})(?:/(?:view|edit|preview|htmlview))?/?$",
    rf"^/spreadsheets/(?:u/\d+/)?d/({GOOGLE_ID})(?:/(?:view|edit|preview|htmlview))?/?$",
    rf"^/drive/(?:u/\d+/)?file/d/({GOOGLE_ID})",
]

FOLDER_PATH_PATTERNS = [
    rf"^/drive/(?:u/\d+/)?folders/({GOOGLE_ID})",
]

# paths that carry an ambiguous drive id in the ?id= query parameter
ID_QUERY_PATHS = ("/open", "/uc")

# query parameters that never change the page content: sessions and trackers are dropped by
# default, everything else is kept so pagination (?page=, ?start=) and id-based pages stay distinct
DROPPED_QUERY_PARAMS = {
    "sid", "phpsessid", "jsessionid", "sessionid", "session_id", "sessid", "s",
    "fbclid", "gclid", "msclkid", "yclid", "mc_cid", "mc_eid", "igshid", "_ga", "_gl",
    "mark", "usp", "si", "feature",
}
DROPPED_QUERY_PREFIXES = ("utm_",)
DROPPED_VIEW_VALUES = {"print", "next", "previous", "unread"}


def _host(url):
    """Returns the lowercased hostname without the www. prefix, or "" when it cannot be parsed."""

    host = urlparse(url).hostname or ""
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def _valid_google_id(file_id):
    """Drive ids are 20-50 characters; longer values are usually two ids glued by broken html."""

    return bool(file_id) and 20 <= len(file_id) <= 50


def _clean_google_id(raw):
    """Extracts a drive id from a possibly malformed ?id= parameter, or None."""

    if not raw:
        return None
    found = re.findall(r"[0-9A-Za-z_-]{20,}", raw)
    return found[-1] if found and _valid_google_id(found[-1]) else None


def parse_google_link(link):
    """Parses a google drive/docs link.

    Returns:
        ("file", file_id) for file links, ("folder", folder_id) for folder links,
        (None, None) when the link is not a google hosted link.
    """

    try:
        parsed = urlparse(link)
        host = _host(link)
        if host not in GOOGLE_HOSTS:
            return None, None

        for pattern in FILE_PATH_PATTERNS:
            match = re.match(pattern, parsed.path)
            if match and _valid_google_id(match.group(1)):
                return "file", match.group(1)

        for pattern in FOLDER_PATH_PATTERNS:
            match = re.match(pattern, parsed.path)
            if match and _valid_google_id(match.group(1)):
                return "folder", match.group(1)

        if parsed.path.rstrip("/") in ID_QUERY_PATHS:
            # entities are already decoded for anchor hrefs, but raw html scans keep &amp;
            query = parsed.query.replace("&amp;", "&")
            file_id = _clean_google_id((parse_qs(query).get("id") or [None])[0])
            if file_id:
                # /open?id= and /uc?id= are usually files; folders are resolved at download time
                return "file", file_id

    except Exception:
        return None, None

    return None, None


def drive_url(kind, file_id):
    """Builds the canonical drive url for a file or folder id."""

    if kind == "folder":
        return f"https://drive.google.com/drive/folders/{file_id}"
    return f"https://drive.google.com/file/d/{file_id}/view"


def is_sub_url(current_url, main_url):
    """Checks if current_url is on the same domain as main_url, ignoring the www. prefix."""

    return _host(current_url) == _host(main_url) != ""


def canonical_url(url, drop_params=(), keep_params=()):
    """Builds the identity key used to tell pages apart.

    The scheme and the www. prefix are ignored, fragments are dropped, percent-encoding is
    normalized and query parameters are sorted for stability. Session, tracking and ui-toggle
    parameters (--drop-param) are removed by default; --keep-param forces a parameter to stay
    even when it is on the default deny list, so pagination and id-based pages stay distinct.
    """

    parsed = urlparse(url)
    path = unquote(parsed.path) or "/"
    params = parse_qs(parsed.query)
    keep = set(keep_params)
    drop = set(drop_params)

    kept = []
    for key in sorted(params):
        lower = key.lower()
        if key in keep:
            pass
        elif lower in drop or lower in DROPPED_QUERY_PARAMS or lower.startswith(DROPPED_QUERY_PREFIXES):
            continue
        elif lower == "view" and all(value in DROPPED_VIEW_VALUES for value in params[key]):
            continue
        for value in sorted(params[key]):
            kept.append((key, value))

    # viewtopic.php?p=N#pN is a permalink to a post inside a topic; t= identifies the page
    if path.endswith("viewtopic.php") and params.get("t"):
        kept = [(key, value) for key, value in kept if key != "p"]

    query = urlencode(kept)
    return f"{_host(url)}{path}" + (f"?{query}" if query else "")


def canonical_page_url(url, drop_params=(), keep_params=()):
    """Returns a clean absolute page url (https, no www, no session parameters)."""

    return f"https://{canonical_url(url, drop_params, keep_params)}"


def _query_slug(params):
    """Builds a stable folder name for a page that is identified only by query parameters."""

    for key in ("p", "page", "id", "t", "pid", "article", "post"):
        values = params.get(key)
        if values:
            return viewonly.sanitize_dir(f"{key}-{values[0]}")
    key = sorted(params)[0]
    return viewonly.sanitize_dir(f"{key}-{params[key][0]}")


def page_dir(url):
    """Maps a page url to the folder that holds the files found on it.

    Static pages use their path without extension (prof/exprof1.html -> prof/exprof1),
    forum topics and boards use forum/topic-<t> and forum/board-<f>, and pages identified
    only by query parameters (?p=123) get a slug like p-123 instead of the output root.
    """

    parsed = urlparse(url)
    path = unquote(parsed.path).strip("/")
    params = parse_qs(parsed.query)
    parent = str(PurePosixPath(path).parent) if path else ""
    if parent == ".":
        parent = ""

    def join(name):
        name = viewonly.sanitize_dir(name)
        return f"{parent}/{name}" if parent else name

    if path.endswith("viewtopic.php"):
        topic = (params.get("t") or [""])[0] or (params.get("p") or [""])[0]
        return join(f"topic-{topic}" if topic else "topic")

    if path.endswith("viewforum.php"):
        forum = (params.get("f") or [""])[0]
        return join(f"board-{forum}" if forum else "board")

    if path:
        suffix = PurePosixPath(path).suffix
        if suffix in (".html", ".htm", ".php"):
            path = path[: -len(suffix)]
        if path.endswith("/index"):
            path = path[: -len("/index")]
        elif path == "index":
            path = ""

    if not path:
        return _query_slug(params) if params else ""

    return "/".join(viewonly.sanitize_dir(part) for part in PurePosixPath(path).parts)


# forum navigation pages with no archived content
SKIP_PATTERNS = [
    r"/ucp\.php",
    r"/memberlist\.php",
    r"/posting\.php",
    r"/search",
    r"/faq",
    r"/app\.php",
    r"/cron",
    r"/feed",
    r"/calendar",
    r"/download/file\.php",
    r"[?&]view=(?:print|next|previous)",
]

# only pages are crawled; any other file extension is treated as a downloadable asset
PAGE_SUFFIXES = ("", ".html", ".htm", ".php", ".asp", ".aspx", ".jsp", ".shtml")


def should_skip(url, exclude_patterns=(), crawl_forum=True):
    """Returns True when a url must not be crawled: nav pages, assets, numeric chains or --exclude matches."""

    parsed = urlparse(url)
    target = parsed.path + (f"?{parsed.query}" if parsed.query else "")

    if any(re.search(pattern, target) for pattern in exclude_patterns):
        return True

    if any(re.search(pattern, target) for pattern in SKIP_PATTERNS):
        return True

    # viewtopic.php?p=N post permalinks repeat their topic, which is reachable via its t= link
    if parsed.path.endswith("viewtopic.php") and not parse_qs(parsed.query).get("t"):
        return True

    if not crawl_forum and parsed.path.startswith("/forum"):
        return True

    path = unquote(parsed.path).rstrip("/")
    if path:
        suffix = PurePosixPath(path).suffix.lower()
        if suffix not in PAGE_SUFFIXES:
            return True
        # pagination-ish numeric chains like /flags/133/2
        if PurePosixPath(path).name.isdigit() and not suffix:
            return True

    return False


def _folder_skipped(rel_path, skip_dirs):
    """Returns True when a folder's relative drive path matches any --skip-dir regex."""

    return any(re.search(pattern, rel_path) for pattern in skip_dirs)


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)
FETCH_RETRIES = 3


def browser_headers(user_agent=None):
    """Browser-like request headers; user_agent replaces the default chrome string when given."""

    return {
        "User-Agent": user_agent or DEFAULT_USER_AGENT,
        "Accept-Language": "ar,fr;q=0.8,en;q=0.6",
    }


def _gdown_headers(user_agent):
    """gdown keyword arguments for an optional user agent; an empty dict keeps gdown's own default."""

    return {"user_agent": user_agent} if user_agent else {}


class Tee:
    """Mirrors writes to several streams, so a run can be shown and logged at the same time."""

    def __init__(self, *streams):
        self._streams = streams

    def write(self, text):
        for stream in self._streams:
            stream.write(text)
        return len(text)

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def flush(self):
        for stream in self._streams:
            stream.flush()

    def isatty(self):
        return any(getattr(stream, "isatty", lambda: False)() for stream in self._streams)

    def __getattr__(self, name):
        """Falls back to the first stream for attributes like fileno or encoding."""

        for stream in self._streams:
            if hasattr(stream, name):
                return getattr(stream, name)
        raise AttributeError(name)


def fetch_page(url, delay=0.3, user_agent=None):
    """Fetches a page with a browser user agent, retrying transient failures.

    Returns:
        (response, False) on success, (None, False) when the page is gone (404/410),
        (None, True) when the failure is transient and worth retrying.
    """

    last_error = None
    for attempt in range(FETCH_RETRIES):
        try:
            time.sleep(delay)
            response = requests.get(url, headers=browser_headers(user_agent), timeout=15)
            if response.status_code in (404, 410):
                print(f"Skipping {url}, error : HTTP {response.status_code}")
                return None, False
            if response.status_code in (403, 429) or response.status_code >= 500:
                last_error = f"HTTP {response.status_code}"
                time.sleep(1.5 * (attempt + 1))
                continue
            response.raise_for_status()
            return response, False
        except requests.exceptions.RequestException as e:
            last_error = e
            time.sleep(1.5 * (attempt + 1))

    print(f"Failed to fetch {url}, error : {last_error}")
    return None, True


def crawl_site(start_url, main_url, max_depth, seen_urls, glinks, failed,
               current_depth=1, delay=0.3, exclude=(), crawl_forum=True,
               drop_params=(), keep_params=(), user_agent=None):
    """Crawls a site with an explicit stack and records every google drive link found.

    A stack (instead of recursion) keeps deep link chains, like forum permalink pages,
    from hitting python's recursion limit.

    Args:
        start_url: first page to crawl
        main_url: starting url, used to stay on the same domain
        max_depth: maximum crawl depth, None for unlimited
        seen_urls: set of already visited canonical urls (only successful fetches are added)
        glinks: dict mapping file id to its csv row, so a file linked from many pages is recorded once
        failed: list collecting (url, depth) pairs whose fetch failed, for a retry pass
        current_depth: depth of the first page
        delay: seconds to wait between page requests
        exclude: extra regexes of urls to skip
        crawl_forum: when False, pages under /forum are not crawled
        drop_params: extra query parameters to ignore when comparing pages
        keep_params: query parameters to keep even when they are on the default deny list
        user_agent: user agent sent with every page request (default: the chrome string)
    """

    stack = [(start_url, current_depth)]

    while stack:
        url, depth = stack.pop()

        if urlparse(url).scheme not in ("http", "https"):
            continue
        if should_skip(url, exclude, crawl_forum):
            continue
        if not is_sub_url(url, main_url):
            continue

        key = canonical_url(url, drop_params, keep_params)
        if key in seen_urls:
            continue
        if max_depth is not None and depth > max_depth:
            continue

        print(f"crawling {url} at depth: {depth}/{max_depth if max_depth else 'infinity'}")
        response, retryable = fetch_page(url, delay, user_agent)
        if response is None:
            if retryable:
                failed.append((url, depth))
            continue
        seen_urls.add(key)

        soup = BeautifulSoup(response.content, 'lxml')
        crawl_path = page_dir(url)
        source_page = canonical_page_url(url, drop_params, keep_params)

        links = []
        for a_tag in soup.find_all('a', href=True):
            text = re.sub(r"\s+", " ", a_tag.get_text(" ", strip=True))
            links.append((urljoin(url, a_tag['href']), text))

        # drive links also live in plain text, embeds and javascript, not only in anchors
        for raw_link in re.findall(r"https?://(?:www\.)?(?:drive|docs)\.google\.com/[^\"'<>\s\\]+", response.text):
            links.append((raw_link, ""))

        children = []
        for absolute_link, text in links:
            kind, file_id = parse_google_link(absolute_link)

            if file_id:
                print(f"Found google drive link {kind}: {absolute_link}")
                glinks.setdefault(file_id, {
                    'file_id': file_id,
                    'href_abs': drive_url(kind, file_id),
                    'type': kind,
                    'source_page': source_page,
                    'crawl_path': crawl_path,  # folder mirroring the page the file was found on
                    'text': text,
                })

            if should_skip(absolute_link, exclude, crawl_forum):
                continue
            if is_sub_url(absolute_link, main_url) and canonical_url(absolute_link, drop_params, keep_params) not in seen_urls:
                children.append((absolute_link, depth + 1))

        # reversed so the first link on the page is crawled first
        stack.extend(reversed(children))


def write_csv(filename, data):
    """Writes the collected google links to a csv file, one row per file id, in a stable order."""

    if not data:
        print("No data to write, maybe the url has no google hosted content")
        return

    fieldnames = ['file_id', 'href_abs', 'type', 'source_page', 'crawl_path', 'text']
    rows = sorted(data.values(), key=lambda row: (row['crawl_path'], row['file_id']))

    with open(filename, 'w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)

    print(f"Successfully wrote {len(rows)} links to {filename}")


def extract_google_id(url, link_type):
    """Extracts the drive file/folder id from a google url, or None if it is not found."""

    if link_type == 'folder':
        match = re.search(r"/drive/folders/([a-zA-Z0-9_-]+)", url)
    else:
        match = re.search(r"/(?:file|document|presentation|spreadsheets)(?:/u/[0-9]+)?/d/([a-zA-Z0-9_-]+)", url)

    return match.group(1) if match else None


MAX_FOLDER_DEPTH = 15

MANIFEST_FIELDS = ['file_id', 'status', 'name', 'kind', 'path', 'bytes', 'source_page']

# status values used in manifest.csv
STATUS_OK = 'ok'
STATUS_FAILED = 'failed'
STATUS_SKIPPED_TYPE = 'skipped-type'
LEGACY_SKIPPED_TYPE = 'skipped-nonpdf'


class DownloadOptions:
    """File-type filters applied by the downloader."""

    def __init__(self, pdf_only=False, only_ext=(), exclude_ext=()):
        self.pdf_only = pdf_only
        self.only_ext = frozenset(only_ext)
        self.exclude_ext = frozenset(exclude_ext)


def type_allowed(name, options):
    """Returns True when a file name passes the active type filters."""

    suffix = PurePosixPath(name).suffix.lower()
    if options.pdf_only and suffix != ".pdf":
        return False
    if options.only_ext and suffix not in options.only_ext:
        return False
    if suffix and suffix in options.exclude_ext:
        return False
    return True


def archived_ok(path):
    """Returns True when a previously downloaded file is present and usable."""

    path = Path(path)
    try:
        if not path.exists() or path.stat().st_size == 0:
            return False
    except OSError:
        return False
    if path.suffix.lower() == ".pdf":
        return viewonly.valid_pdf(path)
    return True


class Manifest:
    """Per-file status store persisted as <output>/manifest.csv, used to resume and report."""

    def __init__(self, root):
        self.path = Path(root) / "manifest.csv"
        self.rows = {}
        self._updates = 0
        if self.path.exists():
            try:
                # utf-8-sig also strips a possible BOM
                with open(self.path, encoding='utf-8-sig', newline='') as f:
                    for row in csv.DictReader(f):
                        if row.get('file_id'):
                            stored = {key: row.get(key) or '' for key in MANIFEST_FIELDS}
                            if stored['status'] == LEGACY_SKIPPED_TYPE:
                                stored['status'] = STATUS_SKIPPED_TYPE
                            self.rows[row['file_id']] = stored
            except Exception as e:
                print(f"Could not read manifest {self.path}: {e}")

    def get(self, file_id):
        """Returns the stored row for a file id, or None."""

        return self.rows.get(file_id)

    def update(self, file_id, **fields):
        """Sets fields for a file id and flushes the manifest to disk every few updates."""

        row = self.rows.setdefault(file_id, {key: '' for key in MANIFEST_FIELDS})
        row['file_id'] = file_id
        for key, value in fields.items():
            row[key] = value if isinstance(value, str) else str(value)
        self._updates += 1
        if self._updates >= 25:
            self.save()

    def save(self):
        """Writes the manifest atomically, sorted for stable diffs."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        rows = sorted(self.rows.values(), key=lambda row: (row['status'], row['file_id']))
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, 'w', encoding='utf-8', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=MANIFEST_FIELDS, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(rows)
        tmp.replace(self.path)
        self._updates = 0


def process_pdf(url, file_id, target, capturer, user_agent=None):
    """Downloads one pdf with gdown, falling back to browser capture for view-only files.

    Args:
        url: share url of the file, used by gdown
        file_id: drive file id, used by the browser capturer
        target: destination pdf path
        capturer: ViewOnlyCapturer instance, or None to disable the fallback
        user_agent: user agent sent to drive (default: gdown's own)

    Returns:
        "ok" when a valid pdf is on disk, "failed" otherwise.
    """

    if viewonly.valid_pdf(target):
        print(f"   Already downloaded: {target.name}")
        return "ok"

    # gdown does not create missing parent directories, and the folder walker may be the
    # first thing to touch a brand-new drive subfolder
    Path(target).parent.mkdir(parents=True, exist_ok=True)

    try:
        result = gdown.download(url=url, output=str(target), fuzzy=True, quiet=False, resume=True,
                                **_gdown_headers(user_agent))
        if result and viewonly.valid_pdf(result):
            print(f"   Downloaded: {target.name}")
            return "ok"
    except Exception as e:
        print(f"   gdown failed: {e}")

    if target.exists() and not viewonly.valid_pdf(target):
        target.unlink()

    if capturer is None:
        print(f"   View-only fallback disabled, skipping: {target.name}")
        return "failed"

    print(f"   View-only fallback: capturing {target.name} ...")
    try:
        info = capturer.capture_to_pdf(file_id, target)
    except Exception as e:
        print(f"   Capture failed: {e}")
        return "failed"

    print(f"   Captured: {target.name} ({info['pages']} pages, {info['bytes']/1e6:.1f} MB)")
    return "ok"


def process_file(url, file_id, output_dir, name, used_names, user_agent=None):
    """Downloads a non-pdf file of any type with gdown.

    Google-native documents have no file extension in their drive name; gdown exports them
    (docx/xlsx/pptx) when it is allowed to name the output itself, so those are downloaded
    into the target directory and then sanitized into place.

    Returns:
        ("ok", path) when the file is on disk, ("failed", None) otherwise.
    """

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    has_ext = bool(PurePosixPath(name).suffix)

    if has_ext:
        target = unique_target(output_dir, name, file_id, used_names)
        used_names[target] = file_id
        if archived_ok(target):
            print(f"   Already downloaded: {target.name}")
            return "ok", target
        try:
            result = gdown.download(url=url, output=str(target), fuzzy=True, quiet=False, resume=True,
                                    **_gdown_headers(user_agent))
        except Exception as e:
            print(f"   gdown failed: {e}")
            return "failed", None
        result = Path(result) if result else None
        if result is None or not archived_ok(result):
            return "failed", None
        print(f"   Downloaded: {result.name}")
        return "ok", result

    try:
        result = gdown.download(url=url, output=str(output_dir) + os.sep, fuzzy=True, quiet=False, resume=True,
                                **_gdown_headers(user_agent))
    except Exception as e:
        print(f"   gdown failed: {e}")
        return "failed", None
    if not result:
        return "failed", None

    result = Path(result)
    if not archived_ok(result):
        return "failed", None
    target = unique_target(output_dir, result.name, file_id, used_names)
    used_names[target] = file_id
    if target != result:
        try:
            result.replace(target)
        except OSError:
            target = result
    print(f"   Downloaded: {target.name}")
    return "ok", target


def unique_target(output_dir, name, file_id, used_names):
    """Returns the target path, suffixing the id when another csv file already owns the name."""

    output_dir = Path(output_dir)
    clean = viewonly.sanitize(name)
    target = output_dir / clean
    taken = target.exists() and not archived_ok(target)
    if taken or used_names.get(target, file_id) != file_id:
        stem = PurePosixPath(clean).stem
        suffix = PurePosixPath(clean).suffix
        target = output_dir / viewonly.sanitize(f"{stem} ({file_id[:8]}){suffix}", "")
    return target


def _reuse_key(name):
    """Builds the match key of a file name, decoding html entities so old trees still line up.

    Older versions copied names straight out of html attributes, so they can hold "&#39;"
    where the current version has "'".
    """

    return html.unescape(viewonly.normalize_name(name)).lower()


def build_reuse_index(reuse_dir):
    """Indexes an older download tree by normalized file name -> list of paths, for --reuse migration."""

    index = {}
    for path in Path(reuse_dir).rglob("*"):
        if path.is_file():
            index.setdefault(_reuse_key(path.name), []).append(path)
    return index


def take_reused_file(name, index):
    """Pops the first unused old file matching name from the reuse index, or None."""

    key = _reuse_key(viewonly.sanitize(name))
    paths = index.get(key)
    while paths:
        path = paths.pop(0)
        if path.exists():
            return path
    return None


def prune_empty_dirs(root):
    """Removes empty directories left behind after files were moved out of an old tree."""

    root = Path(root)
    if not root.exists():
        return
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass


def resolve_name(file_id):
    """Returns (name, kind) for a drive id.

    The download header is preferred (cheap and returns the real upload name); the drive
    title page is the fallback for view-only files and folders.
    """

    name = viewonly.remote_file_name(file_id)
    if name:
        return name, "file"
    return viewonly.probe_drive(file_id)


def resolve_names(rows, manifest, workers):
    """Resolves drive titles for every file id not yet named in the manifest, in parallel."""

    pending = [row['file_id'] for row in rows if not (manifest.get(row['file_id']) or {}).get('name')]
    if not pending:
        return

    print(f"Resolving {len(pending)} file names from drive with {workers} workers ...")
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(resolve_name, file_id): file_id for file_id in pending}
        for future in as_completed(futures):
            file_id = futures[future]
            try:
                name, kind = future.result()
            except Exception:
                name, kind = None, "file"
            manifest.update(file_id, name=name or "", kind=kind)
            done += 1
            if done % 200 == 0:
                print(f"  ... {done}/{len(pending)} names resolved")
    manifest.save()


def download_row(item, target_location, manifest, capturer, reuse_index, used_names, retry_failed,
                 options=None, user_agent=None, skip_dirs=()):
    """Archives one csv row (file or folder).

    Returns:
        "ok", "skipped-type", "skipped" or "failed".
    """

    options = options or DownloadOptions()
    file_id = item['file_id']
    stored = manifest.get(file_id) or {}
    status = stored.get('status', '')
    name = stored.get('name', '')
    kind = stored.get('kind') or (item.get('type') or 'file').strip()
    source_page = item.get('source_page', '')

    if retry_failed and status in (STATUS_OK, STATUS_SKIPPED_TYPE):
        return "skipped"

    saved_path = Path(stored['path']) if stored.get('path') else None
    if status == STATUS_OK and saved_path and archived_ok(saved_path):
        return "ok"

    if not name:
        name, kind = resolve_name(file_id)
        manifest.update(file_id, name=name or "", kind=kind)

    if not name and kind != 'folder':
        # no download header and no title page: removed or inaccessible, a capture would just hang
        print(f"\nCould not resolve {file_id} from drive, marking failed")
        manifest.update(file_id, status=STATUS_FAILED, kind=kind, source_page=source_page)
        return "failed"

    if kind == 'folder':
        crawl_path = (item.get('crawl_path') or '').strip()
        output_dir = Path(target_location) / crawl_path if crawl_path else Path(target_location)
        folder_dir = output_dir / viewonly.sanitize_dir(name or file_id)
        folder_dir.mkdir(parents=True, exist_ok=True)
        url = (item.get('href_abs') or '').strip() or drive_url('folder', file_id)
        print(f"\nDownloading folder: {url}\n  -> Saving to: {folder_dir}")
        listed = download_folder_pdfs(url, str(folder_dir), capturer, user_agent=user_agent,
                                      skip_dirs=skip_dirs)
        if not listed:
            manifest.update(file_id, status=STATUS_FAILED, name=name, kind='folder', source_page=source_page)
            return "failed"
        manifest.update(file_id, status=STATUS_OK, name=name, kind='folder', path=str(folder_dir),
                        source_page=source_page)
        return "ok"

    if not name:
        name = f"{file_id}.pdf"

    if not type_allowed(name, options):
        print(f"\nSkipping filtered type: {name}")
        manifest.update(file_id, status=STATUS_SKIPPED_TYPE, name=name, kind='file', source_page=source_page)
        return STATUS_SKIPPED_TYPE

    crawl_path = (item.get('crawl_path') or '').strip()
    url = (item.get('href_abs') or '').strip() or drive_url('file', file_id)
    output_dir = Path(target_location) / crawl_path if crawl_path else Path(target_location)
    output_dir.mkdir(parents=True, exist_ok=True)

    if reuse_index:
        old_file = take_reused_file(name, reuse_index)
        if old_file is not None:
            target = unique_target(output_dir, name, file_id, used_names)
            used_names[target] = file_id
            old_file.replace(target)
            print(f"\nReused existing file: {old_file.name}\n  -> {target}")
            manifest.update(file_id, status=STATUS_OK, name=name, kind='file', path=str(target),
                            bytes=target.stat().st_size, source_page=source_page)
            return "ok"

    if name.lower().endswith('.pdf'):
        target = unique_target(output_dir, name, file_id, used_names)
        used_names[target] = file_id
        print(f"\nDownloading file: {url}\n  -> Saving to: {target}")
        status = process_pdf(url, file_id, target, capturer, user_agent)
        final = target if status == "ok" else None
    else:
        print(f"\nDownloading file: {url}\n  -> Saving to: {output_dir}")
        status, final = process_file(url, file_id, output_dir, name, used_names, user_agent)

    if status == "ok" and final is not None:
        manifest.update(file_id, status=STATUS_OK, name=name, kind='file', path=str(final),
                        bytes=final.stat().st_size, source_page=source_page)
    else:
        manifest.update(file_id, status=STATUS_FAILED, name=name, kind='file', source_page=source_page)
    return status


def list_folder_entries(url, capturer):
    """Lists (pdfs, folders, files) of a folder.

    Uses the fast html parse and switches to the browser items:list api when the
    payload hits the 50 item cap, so that nothing is missed. The fast results are
    kept whenever the browser listing fails or returns fewer items.
    """

    pdfs, folders, files = viewonly.parse_folder_html(url)
    fast_count = len(pdfs) + len(folders) + len(files)

    if fast_count < 50:
        return pdfs, folders, files

    if capturer is None:
        print("   Warning: folder may have more than 50 items, some could be missed")
        return pdfs, folders, files

    print(f"   Folder has 50+ items, listing complete contents via browser ...")
    try:
        full = capturer.list_folder_all(url)
    except Exception as e:
        print(f"   Browser listing failed, keeping the first 50 items: {e}")
        return pdfs, folders, files

    if full is not None and len(full[0]) + len(full[1]) + len(full[2]) >= fast_count:
        pdfs, folders, files = full
    else:
        print("   Warning: browser listing incomplete, keeping the first 50 items")

    return pdfs, folders, files


def subfolder_dir(output_dir, name):
    """Returns the subfolder path, reusing an existing dir whose name differs only by unicode direction marks."""

    clean = viewonly.sanitize_dir(name)
    target = Path(output_dir) / clean
    if not target.exists() and Path(output_dir).exists():
        norm = viewonly.normalize_name(clean)
        for p in Path(output_dir).iterdir():
            if p.is_dir() and viewonly.normalize_name(p.name) == norm:
                return p
    return target


def download_folder_pdfs(url, output_dir, capturer, visited=None, depth=0, user_agent=None,
                         skip_dirs=(), rel_path=""):
    """Downloads a folder and its subfolders recursively, filling in view-only pdfs via browser capture.

    The tree is walked once through drive's own folder listing: pdfs are fetched or captured,
    other files are downloaded individually, and subfolders whose relative path matches a
    --skip-dir regex are left alone. gdown's recursive folder download is not used, so each
    folder is listed exactly once instead of once per recursion level.

    Args:
        url: drive folder url
        output_dir: local directory matching this folder
        capturer: ViewOnlyCapturer instance, or None to disable the fallback
        visited: set of folder ids already processed, guards against loops
        depth: current recursion depth, capped by MAX_FOLDER_DEPTH
        user_agent: user agent sent to drive (default: gdown's own)
        skip_dirs: regexes matched against a subfolder's relative drive path
        rel_path: relative drive path of this folder, used for skip matching

    Returns:
        True when the folder contents could be listed, False when the folder is inaccessible.
    """

    if visited is None:
        visited = set()

    folder_id = extract_google_id(url, 'folder')
    if folder_id and folder_id in visited:
        return True
    if folder_id:
        visited.add(folder_id)

    Path(output_dir).mkdir(parents=True, exist_ok=True)

    try:
        pdfs, folders, files = list_folder_entries(url, capturer)
    except Exception as e:
        print(f"   Could not list folder contents: {e}")
        return False

    print(f"   Found {len(pdfs)} pdfs, {len(folders)} subfolders, {len(files)} other files")

    for file_id, name in pdfs:
        target = Path(output_dir) / viewonly.sanitize(name)
        try:
            process_pdf(f"https://drive.google.com/file/d/{file_id}/view", file_id, target, capturer, user_agent)
        except Exception as e:
            print(f"   Failed to capture {name}: {e}")

    used_names = {}
    for file_id, name in files:
        try:
            process_file(f"https://drive.google.com/file/d/{file_id}/view", file_id, output_dir, name,
                         used_names, user_agent)
        except Exception as e:
            print(f"   Failed to download {name}: {e}")

    if depth >= MAX_FOLDER_DEPTH:
        if folders:
            print(f"   Max folder depth ({MAX_FOLDER_DEPTH}) reached, skipping {len(folders)} subfolders")
        return True

    for sub_id, sub_name in folders:
        sub_rel = f"{rel_path}/{sub_name}".lstrip("/")
        if _folder_skipped(sub_rel, skip_dirs):
            print(f"   -> skipping folder: {sub_rel}")
            continue
        print(f"   -> subfolder: {sub_name}")
        try:
            download_folder_pdfs(
                f"https://drive.google.com/drive/folders/{sub_id}",
                str(subfolder_dir(output_dir, sub_name)),
                capturer,
                visited,
                depth + 1,
                user_agent,
                skip_dirs,
                sub_rel,
            )
        except Exception as e:
            print(f"   Failed subfolder {sub_name}: {e}")
    return True


def gdown_download(csv_filename, target_location, viewonly_fallback=True, reuse=None,
                   retry_failed=False, workers=16, options=None, user_agent=None, skip_dirs=()):
    """Downloads all files/folders from the csv to the target location, preserving the crawl hierarchy.

    Args:
        csv_filename: csv produced by the crawler
        target_location: root output directory
        viewonly_fallback: enable the browser capture fallback for view-only pdfs
        reuse: older download tree whose matching files are moved instead of re-downloaded
        retry_failed: only process entries marked failed or missing in manifest.csv
        workers: parallel workers used to resolve file names from drive
        options: DownloadOptions with the file-type filters (default: every type)
        user_agent: user agent sent to drive (default: gdown's own)
        skip_dirs: regexes of folder paths to leave out of folder downloads
    """
    options = options or DownloadOptions()
    try:
        # read csv file (utf-8-sig also strips a possible BOM)
        with open(csv_filename, 'r', encoding='utf-8-sig') as f:
            data = list(csv.DictReader(f))
    except FileNotFoundError as e:
        print(f"the csv file doesnt exist, the website probably has no google content: {e} ")
        return

    if not data:
        print("No data in CSV to download")
        return

    # the crawler writes one row per file id; keep the first row of hand-written csv files too
    rows = {}
    for row in data:
        file_id = (row.get('file_id') or '').strip() or extract_google_id(row.get('href_abs', ''), row.get('type', 'file'))
        if file_id:
            row['file_id'] = file_id
            rows.setdefault(file_id, row)
    rows = list(rows.values())

    Path(target_location).mkdir(parents=True, exist_ok=True)
    manifest = Manifest(target_location)
    reuse_index = build_reuse_index(reuse) if reuse else {}

    capturer = viewonly.ViewOnlyCapturer() if viewonly_fallback else None
    stats = Counter()
    failed_rows = []
    used_names = {}

    try:
        resolve_names(rows, manifest, workers)

        for item in rows:
            try:
                status = download_row(item, target_location, manifest, capturer,
                                      reuse_index, used_names, retry_failed, options, user_agent,
                                      skip_dirs)
            except Exception as e:
                print(f"   Failed: {e}")
                status = STATUS_FAILED
                manifest.update(item['file_id'], status=STATUS_FAILED, source_page=item.get('source_page', ''))
            stats[status] += 1
            if status == STATUS_FAILED:
                failed_rows.append(item)

        # one retry pass for transient failures (rate limits, timeouts)
        if failed_rows:
            print(f"\nRetrying {len(failed_rows)} failed files ...")
            retry, failed_rows = failed_rows, []
            for item in retry:
                try:
                    status = download_row(item, target_location, manifest, capturer,
                                          reuse_index, used_names, retry_failed, options, user_agent)
                except Exception as e:
                    print(f"   Failed: {e}")
                    status = STATUS_FAILED
                    manifest.update(item['file_id'], status=STATUS_FAILED, source_page=item.get('source_page', ''))
                stats[status] += 1
                if status == STATUS_FAILED:
                    failed_rows.append(item)
    finally:
        manifest.save()
        if capturer is not None:
            capturer.close()

    if reuse:
        prune_empty_dirs(reuse)
        leftover = sum(len(paths) for paths in reuse_index.values())
        if leftover:
            print(f"Left {leftover} unmatched files in {reuse}")

    summary = ", ".join(f"{count} {name}" for name, count in sorted(stats.items()) if count)
    print(f"\n{'='*60}")
    print(f"Download complete! Files saved to: {target_location}")
    print(f"Summary: {summary}")
    if failed_rows:
        failures_path = Path(target_location) / "failures.txt"
        with open(failures_path, 'w', encoding='utf-8') as f:
            for item in failed_rows:
                f.write(f"{item.get('file_id','')}\t{item.get('text','')}\t{item.get('href_abs','')}\n")
        print(f"{len(failed_rows)} files failed, listed in {failures_path}")
    print(f"{'='*60}")


def main():
    """Parses cli arguments, crawls the given url and downloads the google links found."""

    parser = argparse.ArgumentParser(
        description='Crawl a website and download Google Drive files/folders while preserving hierarchy'
    )
    
    parser.add_argument(
        'url',
        help='Starting URL to crawl'
    )
    
    parser.add_argument(
        '-d', '--depth',
        type=int,
        default=None,
        help='Maximum crawl depth (default: unlimited - crawl entire site)'
    )
    
    parser.add_argument(
        '-o', '--output',
        default=None,
        help='Output directory for downloaded files (default: ./{domain_name})'
    )
    
    parser.add_argument(
        '--csv',
        default=None,
        help='CSV file to store found links (default: {domain_name}.csv)'
    )
    
    parser.add_argument(
        '--crawl-only',
        action='store_true',
        help='Only crawl and generate CSV, skip downloading'
    )
    
    parser.add_argument(
        '--no-viewonly',
        action='store_true',
        help='Disable browser capture fallback for view-only pdfs'
    )

    parser.add_argument(
        '--no-forum',
        action='store_true',
        help='Do not crawl forum pages (they are crawled by default)'
    )

    parser.add_argument(
        '--exclude',
        action='append',
        default=[],
        metavar='REGEX',
        help='Skip urls whose path and query match this regex (repeatable)'
    )

    parser.add_argument(
        '--skip-dir',
        action='append',
        default=[],
        metavar='REGEX',
        help='Skip drive subfolders whose relative path matches this regex (repeatable)'
    )

    parser.add_argument(
        '--delay',
        type=float,
        default=0.3,
        help='Seconds to wait between page requests (default: 0.3)'
    )

    parser.add_argument(
        '--drop-param',
        action='append',
        default=[],
        metavar='NAME',
        help='Extra query parameter to ignore when comparing page urls (repeatable)'
    )

    parser.add_argument(
        '--keep-param',
        action='append',
        default=[],
        metavar='NAME',
        help='Query parameter to keep even when it is on the default deny list (repeatable)'
    )

    parser.add_argument(
        '--download-only',
        action='store_true',
        help='Skip crawling and download from the existing CSV'
    )

    parser.add_argument(
        '--pdf-only',
        action='store_true',
        help='Only download pdf files (default: all file types)'
    )

    parser.add_argument(
        '--only-ext',
        default='',
        metavar='EXT[,EXT...]',
        help='Only download these file extensions, e.g. pdf,docx'
    )

    parser.add_argument(
        '--exclude-ext',
        default='',
        metavar='EXT[,EXT...]',
        help='Never download these file extensions, e.g. rar,exe'
    )

    parser.add_argument(
        '--reuse',
        default=None,
        metavar='DIR',
        help='Move matching files from an older download tree into the new layout before downloading'
    )

    parser.add_argument(
        '--retry-failed',
        action='store_true',
        help='Only retry entries marked failed or missing in manifest.csv'
    )

    parser.add_argument(
        '--workers',
        type=int,
        default=16,
        help='Parallel workers used to resolve file names from drive (default: 16)'
    )

    parser.add_argument(
        '--user-agent',
        default=None,
        metavar='UA',
        help='User-Agent sent to the site and to drive (default: a chrome string)'
    )

    parser.add_argument(
        '--log-file',
        default=None,
        metavar='PATH',
        help='Also append run output to this file'
    )

    args = parser.parse_args()

    if args.log_file:
        log_stream = open(args.log_file, 'a', encoding='utf-8')
        log_stream.write(f"\n=== gdarchiver run {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
        sys.stdout = Tee(sys.stdout, log_stream)
        sys.stderr = Tee(sys.stderr, log_stream)

    if args.user_agent:
        viewonly.set_user_agent(args.user_agent)

    def ext_set(value):
        """Parses a comma separated extension list into a set of lowercased .ext strings."""

        return {f".{item.strip().lstrip('.').lower()}" for item in value.split(',') if item.strip()}

    options = DownloadOptions(pdf_only=args.pdf_only, only_ext=ext_set(args.only_ext),
                              exclude_ext=ext_set(args.exclude_ext))

    # add https:// if missing
    if not args.url.startswith(('http://', 'https://')):
        args.url = 'https://' + args.url

    # default output directory based on netloc if not provided
    if args.output is None:
        netloc = urlparse(args.url).netloc or "downloads"
        netloc = netloc.replace('.', '_')
        args.output = f"./{netloc}"


    if args.csv is None:

        csv_name = urlparse(args.url).netloc or "gdrive_links.csv"
        csv_name = csv_name.replace('.', '_')
        args.csv = f"{csv_name}.csv"

    if args.download_only:
        print(f"\nStarting download to {args.output}...")
        gdown_download(args.csv, args.output, viewonly_fallback=not args.no_viewonly,
                       reuse=args.reuse, retry_failed=args.retry_failed, workers=args.workers,
                       options=options, user_agent=args.user_agent, skip_dirs=args.skip_dir)
        return

    seen_urls = set()
    glinks = {}
    failed = []
    crawl_options = {
        'delay': args.delay,
        'exclude': args.exclude,
        'crawl_forum': not args.no_forum,
        'drop_params': args.drop_param,
        'keep_params': args.keep_param,
        'user_agent': args.user_agent,
    }

    # crawl start
    print(f"Starting crawler on {args.url} with max depth {args.depth}...")
    crawl_site(args.url, args.url, args.depth, seen_urls, glinks, failed, **crawl_options)

    # failed pages were never marked as seen, so one retry pass can still reach them
    if failed:
        retry = sorted(set(failed))
        failed.clear()
        print(f"\nRetrying {len(retry)} pages that failed to fetch ...")
        for page_url, page_depth in retry:
            crawl_site(page_url, args.url, args.depth, seen_urls, glinks, failed,
                            page_depth, **crawl_options)

    print(f"\nCrawl done: {len(seen_urls)} pages crawled, {len(glinks)} google files found, {len(failed)} pages failed")

    # writing to csv
    write_csv(args.csv, glinks)

    # download start
    if not args.crawl_only:
        print(f"\nStarting download to {args.output}...")
        gdown_download(args.csv, args.output, viewonly_fallback=not args.no_viewonly,
                       reuse=args.reuse, retry_failed=args.retry_failed, workers=args.workers,
                       options=options, user_agent=args.user_agent, skip_dirs=args.skip_dir)
    else:
        print(f"\nCrawl complete. Use 'python main.py --help' to see download options.")


if __name__ == "__main__":
    main()
