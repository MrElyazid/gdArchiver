import base64
import io
import json
import re
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

import img2pdf
import requests
from PIL import Image
from playwright.sync_api import sync_playwright

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)
VIEWPORT = {"width": 1440, "height": 900}


def set_user_agent(user_agent):
    """Overrides the user agent used for drive page and header requests."""

    global UA
    if user_agent:
        UA = user_agent
# page images are fetched at this width; drive's viewer serves up to 3200px (~388 dpi for a4)
MAX_W = 3200
DPR = 2
FILE_TIMEOUT = 900

# seconds to wait between direct page fetches, the viewer image endpoint 400s on bursts
PAGE_DELAY = 0.15


def fetch_html(url):
    """Fetches a url with a browser-like user agent and returns the decoded html."""

    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")


def sanitize(name, default_ext=".pdf"):
    """Sanitizes a file name: strips illegal characters, ensures an extension and a non-empty stem."""

    name = sanitize_dir(name)
    if not PurePosixPath(name).suffix:
        name += default_ext
    if name.lower() == ".pdf" or not PurePosixPath(name).stem:
        name = "unnamed.pdf"
    return _truncate_name(name, 200)


def sanitize_dir(name):
    """Sanitizes a folder name: strips invisible unicode marks and illegal path characters."""

    name = normalize_name(name)
    name = re.sub(r'[<>:"/\\|?*]', "-", name).strip()
    return _truncate_name(name, 200)


def _truncate_name(name, max_bytes):
    """Truncates a name to max_bytes of utf-8 (keeping the extension) to stay under filesystem limits."""

    if len(name.encode("utf-8")) <= max_bytes:
        return name
    suffix = PurePosixPath(name).suffix
    stem = name[: -len(suffix)] if suffix else name
    budget = max_bytes - len(suffix.encode("utf-8"))
    stem = stem.encode("utf-8")[:budget].decode("utf-8", "ignore")
    return stem + suffix


def normalize_name(name):
    """Removes invisible unicode direction marks (RTL/LTR control characters) from a name."""

    return re.sub(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", name).strip()


def valid_pdf(path):
    """Returns True if the file exists and starts with the %PDF- magic bytes."""

    try:
        with open(path, "rb") as f:
            return f.read(5) == b"%PDF-"
    except OSError:
        return False


def _decode_content_disposition(header):
    """Extracts and decodes a file name from a Content-Disposition header, or None."""

    if not header:
        return None

    match = re.search(r"filename\*=UTF-8''([^;]+)", header)
    if match:
        return unquote(match.group(1)).strip()

    match = re.search(r'filename="?([^";]+)"?', header)
    if not match:
        return None
    name = match.group(1).strip()
    try:
        # header values are latin-1 decoded by requests, but google sends utf-8 bytes
        return name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name


_thread_local = threading.local()


def _http_session():
    """Returns a thread-local requests session, so parallel workers reuse connections."""

    session = getattr(_thread_local, "session", None)
    if session is None:
        session = requests.Session()
        _thread_local.session = session
    return session


def remote_file_name(file_id, retries=3):
    """Reads the original upload name of a drive file from its download headers.

    Only headers are requested, so this is cheap and avoids the rate limited file pages.
    Returns None for view-only files, folders and google-native documents.
    """

    url = f"https://drive.usercontent.google.com/download?id={file_id}&export=download"
    for attempt in range(retries):
        try:
            response = _http_session().head(url, headers={"User-Agent": UA}, timeout=30, allow_redirects=True)
            if response.status_code in (429, 503):
                time.sleep(3 * (attempt + 1))
                continue
            return _decode_content_disposition(response.headers.get("Content-Disposition", ""))
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
    return None


INVALID_TITLES = {
    "google drive",
    "google drive: sign-in",
    "sign in - google accounts",
    "google accounts",
}


def _title_from_html(html):
    """Extracts a drive title from a page's <title>, stripping the google drive suffix.

    Returns None for sign-in and error pages, which carry no useful file name.
    """

    match = re.search(r"<title>([^<]*)</title>", html)
    if not match:
        return None
    name = match.group(1).replace("\xa0", " ").strip()
    name = re.sub(r"\s*-\s*Google\s*Drive$", "", name).strip()
    if name.lower() in INVALID_TITLES:
        return None
    return name or None


def _fetch_drive_page(url, retries=3):
    """Fetches a drive page, returning (name, kind) where kind is "file" or "folder"."""

    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as response:
                final_url = response.geturl()
                html = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            # drive answers 429 when the pages are requested too fast
            if e.code == 429 and attempt < retries - 1:
                time.sleep(10 * (attempt + 1))
                continue
            return None, "file"
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            return None, "file"

        if "accounts.google.com" in final_url:
            # private or removed file: the request landed on the sign-in page
            return None, "file"

        kind = "folder" if "/drive/folders/" in final_url or "application/vnd.google-apps.folder" in html else "file"
        return _title_from_html(html), kind

    return None, "file"


def probe_drive(file_id):
    """Fetches the drive page of a file or folder id.

    Returns:
        (name, kind) where kind is "file" or "folder"; name is None when it cannot be read.
    """

    name, kind = _fetch_drive_page(f"https://drive.google.com/file/d/{file_id}/view")
    if name is None:
        folder_name, folder_kind = _fetch_drive_page(f"https://drive.google.com/drive/folders/{file_id}")
        if folder_name:
            return folder_name, folder_kind
    return name, kind


def _unescape_js(js):
    """Unescapes a javascript string literal (\\xNN, \\uNNNN, \\/ and identity escapes) into plain text."""

    js = re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), js)
    js = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), js)
    js = js.replace("\\/", "/")
    return re.sub(r"\\([^\\\"bfnrtu/])", r"\1", js)


def _walk_items(node, out):
    """Recursively collects (id, name, mime_type) tuples from drive's nested json payloads."""

    if isinstance(node, list):
        if (
            len(node) > 3
            and isinstance(node[0], str)
            and re.fullmatch(r"[0-9A-Za-z_-]{25,50}", node[0])
            and isinstance(node[3], str)
            and "/" in node[3]
        ):
            out.append((node[0], node[2] if isinstance(node[2], str) else "", node[3]))
        for child in node:
            _walk_items(child, out)


def _classify(entries):
    """Splits (id, name, mime_type) entries into pdfs, folders and other files, all keyed by file id."""

    pdfs, folders, files = {}, {}, {}
    for file_id, name, mime in entries:
        if mime == "application/pdf":
            pdfs.setdefault(file_id, name)
        elif mime == "application/vnd.google-apps.folder":
            folders.setdefault(file_id, name)
        else:
            files.setdefault(file_id, name)
    return pdfs, folders, files


def _parse_dir_payload(html):
    """Parses a folder page html into (pdfs, folders, files).

    Reads the embedded _DRIVE_* json payload first (capped at ~50 items) and falls back
    to the aria-label / ssk regexes when the json cannot be decoded.
    """

    pdfs, folders, files = {}, {}, {}
    try:
        candidates = re.findall(r"window\['_DRIVE_[A-Za-z_]+'\]\s*=\s*'(.*?)';", html, re.S)
        data = json.loads(_unescape_js(max(candidates, key=len)))
        entries = []
        _walk_items(data, entries)
        pdfs, folders, files = _classify(entries)
    except Exception:
        pass

    if not pdfs and not folders and not files:
        for name, fid in re.findall(
            r'aria-label="([^"]+?)\s+PDF\s+Shared"[^>]*?ssk=\\?\'5:[^:]+:([0-9A-Za-z_-]{33})(?:-\d+-\d+)?\\?\'',
            html,
        ):
            if name.lower().endswith(".pdf"):
                pdfs.setdefault(fid, name)
        for name, fid in re.findall(
            r'aria-label="([^"]+?)\s+Shared folder"[^>]*?ssk=\\?\'5:[^:]+:([0-9A-Za-z_-]{33})(?:-\d+-\d+)?\\?\'',
            html,
        ):
            folders.setdefault(fid, name)

    return (
        [(fid, name) for fid, name in pdfs.items()],
        [(fid, name) for fid, name in folders.items()],
        [(fid, name) for fid, name in files.items()],
    )


def parse_folder_html(folder_url):
    """Lists (pdfs, folders, files) of a public folder from its html. Fast, but may be capped at 50 items."""

    return _parse_dir_payload(fetch_html(folder_url))


def _image_ext(data):
    """Returns the file extension of a known page image, or None for anything else."""

    if not data:
        return None
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return ".webp"
    if data[4:8] == b"ftyp" and data[8:12].startswith(b"avi"):
        return ".avif"
    return None


def _image_ok(data, content_type=""):
    """Returns True when the bytes are a page image rather than a google error page.

    Drive answers some viewer image requests with a small html error page, so the content
    type and the magic bytes must both agree before a body is kept.
    """

    if content_type and not content_type.startswith("image/"):
        return False
    return _image_ext(data) is not None


def _page_cache_dir(out_path, file_id):
    """Returns the hidden directory that stores a file's captured pages between attempts."""

    return Path(out_path).parent / ".parts" / file_id


def _load_cached_pages(cache_dir):
    """Returns {page: (width, url, bytes)} for the pages captured by earlier attempts."""

    pages = {}
    if not cache_dir.is_dir():
        return pages
    for path in cache_dir.iterdir():
        match = re.fullmatch(r"page-(\d+)-(\d+)\.(?:webp|png|jpg|jpeg|avif)", path.name)
        if not match:
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if _image_ok(data):
            pages[int(match.group(1))] = (int(match.group(2)), None, data)
    return pages


def _store_cached_page(cache_dir, page, width, data):
    """Persists one page image, replacing any earlier capture of the same page."""

    ext = _image_ext(data)
    if not ext:
        return
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        for old in cache_dir.glob(f"page-{page:04d}-*"):
            old.unlink()
        target = cache_dir / f"page-{page:04d}-{width}{ext}"
        tmp = target.with_name(target.name + ".part")
        tmp.write_bytes(data)
        tmp.replace(target)
    except OSError:
        pass


FETCH_IMAGE_JS = """async (url) => {
    const r = await fetch(url);
    if (!r.ok) return null;
    const ab = await r.arrayBuffer();
    const bytes = new Uint8Array(ab);
    let bin = '';
    for (let i = 0; i < bytes.length; i += 8192)
        bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 8192));
    return {ct: r.headers.get('content-type') || '', b64: btoa(bin)};
}"""


def _fetch_image(page, url, attempts=3):
    """Re-fetches one page image inside the browser, retrying google's transient error pages.

    Returns the image bytes, or None when every attempt came back without an image.
    """

    for attempt in range(attempts):
        try:
            result = page.evaluate(FETCH_IMAGE_JS, url)
        except Exception:
            result = None
        if result:
            data = base64.b64decode(result["b64"])
            if _image_ok(data, result.get("ct", "")):
                return data
        page.wait_for_timeout(1000 * (attempt + 1))
    return None


def _meta_info(body):
    """Parses the drive viewer meta payload into (pages, max page width), or (0, 0)."""

    try:
        data = json.loads(re.sub(r"^\)\]\}'\s*", "", body))
        return int(data.get("pages") or 0), int(data.get("maxPageWidth") or 0)
    except Exception:
        return 0, 0


def _viewer_page_url(template, page, width):
    """Rewrites the page and width of a viewer/img url, keeping its session token intact."""

    url = re.sub(r"([?&]page=)\d+", rf"\g<1>{page}", template)
    if width:
        url = re.sub(r"([?&]w=)\d+", rf"\g<1>{width}", url)
    return url


class ViewOnlyCapturer:
    """Owns one headless chromium instance used to capture view-only pdfs and folder listings."""

    def __init__(self, dpr=DPR, max_w=MAX_W, timeout=FILE_TIMEOUT):
        """Sets the render resolution (dpr), the maximum page image width and the per-file timeout."""

        self.dpr = dpr
        self.max_w = max_w
        self.timeout = timeout
        self._pw = None
        self._browser = None

    def _ensure_browser(self):
        """Starts playwright and the headless browser on first use, then returns the browser."""

        if self._browser is None:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
        return self._browser

    def close(self):
        """Closes the browser and stops playwright. Safe to call more than once."""

        if self._browser is not None:
            self._browser.close()
            self._browser = None
        if self._pw is not None:
            self._pw.stop()
            self._pw = None

    def __enter__(self):
        """Returns self so the capturer can be used as a context manager."""

        return self

    def __exit__(self, *exc):
        """Closes the browser when leaving a with block."""

        self.close()

    def capture_to_pdf(self, file_id, out_path):
        """Captures every page of a view-only pdf and writes it to out_path as an a4 pdf.

        Pages captured by an earlier attempt are reused from a hidden cache directory next
        to out_path, so retries only fetch what is still missing. The cache is removed once
        the pdf is complete.

        Returns:
            dict with the number of pages, the output size in bytes and the dpr used.
        """

        out_path = Path(out_path)
        cache_dir = _page_cache_dir(out_path, file_id)
        pages = self._capture_pages(file_id, _load_cached_pages(cache_dir), cache_dir)
        if not pages:
            raise RuntimeError("no pages captured")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            pngs = []
            for i in sorted(pages):
                _, _, data = pages[i]
                im = Image.open(io.BytesIO(data)).convert("RGB")
                dpi = im.width / (210 / 25.4)
                p = Path(tmp) / f"page-{i:04d}.png"
                im.save(p, format="PNG", dpi=(dpi, dpi))
                pngs.append(str(p))
            with open(out_path, "wb") as f:
                f.write(img2pdf.convert(pngs))
        shutil.rmtree(cache_dir, ignore_errors=True)
        try:
            cache_dir.parent.rmdir()
        except OSError:
            pass
        return {
            "pages": len(pages),
            "bytes": out_path.stat().st_size,
            "dpr": self.dpr,
        }

    def list_folder_all(self, folder_url):
        """Lists every folder item through drive's paginated items:list api, bypassing the 50 item html cap.

        Returns:
            (pdfs, folders, files) as lists of (id, name) tuples, or None when the api request
            could not be captured.
        """

        browser = self._ensure_browser()
        ctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=1)
        page = ctx.new_page()
        cap = {}

        def on_response(resp):
            """Stores the url, body and field mask of the page's own items:list request."""
            if "items:list" in resp.url and "url" not in cap:
                try:
                    cap["url"] = resp.url
                    cap["post"] = resp.request.post_data
                    cap["fieldmask"] = resp.request.headers.get("x-goog-fieldmask", "")
                except Exception:
                    pass

        try:
            page.on("response", on_response)
            page.goto(folder_url, wait_until="domcontentloaded", timeout=90000)
            for _ in range(40):
                if "url" in cap:
                    break
                page.wait_for_timeout(500)
            if "url" not in cap:
                return None

            result = page.evaluate(
                """async ({url, post, fieldmask}) => {
                const headers = {'content-type': 'application/json+protobuf', 'x-goog-fieldmask': fieldmask};
                const body = JSON.parse(post);
                const found = new Map();
                let token = '';
                for (let pageNo = 0; pageNo < 50; pageNo++) {
                    body[1][1] = token;
                    const r = await fetch(url, {method: 'POST', headers, body: JSON.stringify(body)});
                    if (!r.ok) return {error: r.status, items: Array.from(found.values())};
                    const d = await r.json();
                    const walk = (n) => {
                        if (Array.isArray(n)) {
                            if (n.length > 3 && typeof n[0] === 'string' && /^[0-9A-Za-z_-]{25,50}$/.test(n[0])
                                && typeof n[3] === 'string' && n[3].includes('/')) {
                                found.set(n[0], [n[0], typeof n[2] === 'string' ? n[2] : '', n[3]]);
                            }
                            n.forEach(walk);
                        }
                    };
                    walk(d);
                    token = (Array.isArray(d) && typeof d[1] === 'string') ? d[1] : '';
                    if (!token) break;
                }
                return {items: Array.from(found.values())};
            }""",
                {"url": cap["url"], "post": cap["post"], "fieldmask": cap["fieldmask"]},
            )
        finally:
            ctx.close()

        entries = [(i[0], i[1], i[2]) for i in result.get("items", [])]
        pdfs, folders, files = _classify(entries)
        return (
            [(fid, name) for fid, name in pdfs.items()],
            [(fid, name) for fid, name in folders.items()],
            [(fid, name) for fid, name in files.items()],
        )

    def _capture_pages(self, file_id, cached=None, cache_dir=None):
        """Collects the raw page images of a view-only pdf.

        The drive preview renders pages on demand, and one observed viewer/img url can be
        rewritten to fetch any page directly, so pages are pulled one by one without
        scrolling. Wheel scrolling is only used when some pages keep failing, and every
        captured page is cached for future attempts.

        Args:
            file_id: drive id of the file to capture
            cached: pages already captured by an earlier attempt, from _load_cached_pages
            cache_dir: when given, every accepted page is persisted there for future attempts

        Returns:
            dict mapping the page index to (width, image url, image bytes).
        """

        browser = self._ensure_browser()
        ctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=self.dpr)
        page = ctx.new_page()
        pages = dict(cached or {})
        page_urls = {}
        meta = {}

        def keep(idx, w, url, data):
            """Stores the best capture of a page so far and caches it on disk."""
            prev = pages.get(idx)
            if prev is not None and prev[0] >= w:
                return
            pages[idx] = (w, url, data)
            if cache_dir is not None:
                _store_cached_page(cache_dir, idx, w, data)

        def on_response(resp):
            """Stores the viewer meta payload and the best image seen for each page."""
            url = resp.url
            if "/viewer/meta" in url and not meta:
                try:
                    meta["pages"], meta["max_width"] = _meta_info(resp.text())
                except Exception:
                    pass
                return
            if "viewer/img" not in url:
                return
            m = re.search(r"[?&]page=(\d+)&w=(\d+)", url)
            if not m:
                return
            idx, w = int(m.group(1)), int(m.group(2))
            page_urls[idx] = url
            try:
                body = resp.body()
            except Exception:
                return
            if _image_ok(body, resp.headers.get("content-type", "")):
                keep(idx, w, url, body)

        try:
            page.on("response", on_response)
            page.goto(
                f"https://drive.google.com/file/d/{file_id}/preview",
                wait_until="domcontentloaded",
                timeout=90000,
            )
            state = page.wait_for_function(
                """() => {
                if (document.querySelector("img[src^='blob:']")) return 'ready';
                const t = document.body.innerText || '';
                if (/Impossible de prévisualiser|Cannot preview|can't preview|لا يمكن معاينة/.test(t)) return 'error';
                return false;
            }""",
                timeout=90000,
            ).json_value()
            if state == "error":
                raise RuntimeError("google cannot preview this file (corrupted or unsupported format)")
            page.wait_for_timeout(2000)

            deadline = time.time() + self.timeout
            for _ in range(60):
                if meta.get("pages") and page_urls:
                    break
                page.wait_for_timeout(500)

            width = min(self.max_w, meta.get("max_width") or self.max_w)
            template = next(iter(page_urls.values()), "")
            total = meta.get("pages") or self._total_pages(page)

            if template and total:
                self._fetch_pages(page, pages, template, total, width, deadline, keep)

            if self._missing(pages, total) and time.time() < deadline:
                self._scroll_for_pages(page, pages, deadline)

            # the viewer serves pages at its display width and only the ones it scrolled to,
            # so re-fetch pages that are missing or below the target width through their url
            for idx in self._unfinished(pages, total, width):
                if time.time() > deadline:
                    break
                url = page_urls.get(idx) or (template and _viewer_page_url(template, idx, width))
                if not url:
                    continue
                data = _fetch_image(page, _viewer_page_url(url, idx, width))
                if data:
                    keep(idx, width, url, data)

            missing = self._missing(pages, total)
            if missing:
                raise RuntimeError(f"missing {len(missing)}/{total or '?'} pages: {missing[:10]}")
            return pages
        finally:
            ctx.close()

    @staticmethod
    def _total_pages(page):
        """Reads the total page count from the viewer toolbar text, or 0 if it is not shown yet."""

        txt = page.evaluate("() => document.body.innerText")
        m = re.search(r"Page\s+\d+\s+(?:sur|of)\s+(\d+)", txt)
        if not m:
            m = re.search(r"Page\s*/\s*(\d+)", txt)
        return int(m.group(1)) if m else 0

    def _fetch_pages(self, page, pages, template, total, width, deadline, keep):
        """Fetches every page directly through the viewer's image url, one at a time.

        Drive renders pages on demand, so rewriting the page and width of one observed url
        works for any page, even ones the viewer never scrolled to. Pages already captured
        at the target width are skipped, so retries only fetch what is still needed.
        """

        for idx in range(total):
            if time.time() > deadline:
                return
            prev = pages.get(idx)
            if prev is not None and prev[0] >= width:
                continue
            url = _viewer_page_url(template, idx, width)
            data = _fetch_image(page, url)
            if data:
                keep(idx, width, url, data)
            page.wait_for_timeout(int(PAGE_DELAY * 1000))

    def _scroll_for_pages(self, page, pages, deadline):
        """Wheels through the viewer until its lazy loader stops producing new pages.

        Some documents ignore scrollTop changes from javascript and only render pages on
        real wheel events, so the fallback scrolls with the mouse.
        """

        last, stable, steps = -1, 0, 0
        while time.time() < deadline and stable < 8 and steps < 600:
            page.mouse.move(VIEWPORT["width"] // 2, VIEWPORT["height"] // 2)
            page.mouse.wheel(0, 2200)
            page.wait_for_timeout(300)
            steps += 1
            n = len(pages)
            if n == last:
                stable += 1
            else:
                stable, last = 0, n
        page.wait_for_timeout(1500)

    @staticmethod
    def _missing(pages, total):
        """Returns the page indices that have no capture yet."""

        if total:
            return [i for i in range(total) if i not in pages]
        return [i for i in range(max(pages) + 1) if i not in pages] if pages else []

    @staticmethod
    def _unfinished(pages, total, width):
        """Returns page indices that are missing or captured below the target width."""

        span = range(total) if total else (range(max(pages) + 1) if pages else range(0))
        return [i for i in span if i not in pages or pages[i][0] < width]
