import base64
import io
import json
import re
import tempfile
import time
import urllib.request
from pathlib import Path

import img2pdf
from PIL import Image
from playwright.sync_api import sync_playwright

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120 Safari/537.36"
)
VIEWPORT = {"width": 1440, "height": 900}
MAX_W = 3200
DPR = 4
FILE_TIMEOUT = 900


def fetch_html(url):
    """Fetches a url with a browser-like user agent and returns the decoded html."""

    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")


def sanitize(name):
    """Sanitizes a file name: strips illegal characters, ensures the .pdf extension and a non-empty stem."""

    name = sanitize_dir(name)
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    if name.lower() == ".pdf":
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
    ext = ".pdf" if name.lower().endswith(".pdf") else ""
    stem = name[: -len(ext)] if ext else name
    budget = max_bytes - len(ext.encode("utf-8"))
    stem = stem.encode("utf-8")[:budget].decode("utf-8", "ignore")
    return stem + ext


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


def get_file_name(file_id):
    """Fetches the real file name from the drive file page title, or None if it cannot be read."""

    try:
        html = fetch_html(f"https://drive.google.com/file/d/{file_id}/view")
        m = re.search(r"<title>([^<]*)</title>", html)
        if not m:
            return None
        name = m.group(1).replace("\xa0", " ").strip()
        name = re.sub(r"\s*-\s*Google\s*Drive$", "", name).strip()
        return name or None
    except Exception:
        return None


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
    """Splits (id, name, mime_type) entries into pdfs and folders, both keyed by file id."""

    pdfs, folders = {}, {}
    for file_id, name, mime in entries:
        if mime == "application/pdf":
            pdfs.setdefault(file_id, name)
        elif mime == "application/vnd.google-apps.folder":
            folders.setdefault(file_id, name)
    return pdfs, folders


def _parse_dir_payload(html):
    """Parses a folder page html into (pdfs, folders).

    Reads the embedded _DRIVE_* json payload first (capped at ~50 items) and falls back
    to the aria-label / ssk regexes when the json cannot be decoded.
    """

    pdfs, folders = {}, {}
    try:
        candidates = re.findall(r"window\['_DRIVE_[A-Za-z_]+'\]\s*=\s*'(.*?)';", html, re.S)
        data = json.loads(_unescape_js(max(candidates, key=len)))
        entries = []
        _walk_items(data, entries)
        pdfs, folders = _classify(entries)
    except Exception:
        pass

    if not pdfs and not folders:
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

    return [(fid, name) for fid, name in pdfs.items()], [(fid, name) for fid, name in folders.items()]


def parse_folder_html(folder_url):
    """Lists (pdfs, folders) of a public folder from its html. Fast, but may be capped at 50 items."""

    return _parse_dir_payload(fetch_html(folder_url))


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

        Returns:
            dict with the number of pages, the output size in bytes and the dpr used.
        """

        pages = self._capture_pages(file_id)
        if not pages:
            raise RuntimeError("no pages captured")
        out_path = Path(out_path)
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
        return {
            "pages": len(pages),
            "bytes": out_path.stat().st_size,
            "dpr": self.dpr,
        }

    def list_folder_all(self, folder_url):
        """Lists every folder item through drive's paginated items:list api, bypassing the 50 item html cap.

        Returns:
            (pdfs, folders) as lists of (id, name) tuples, or None if the api request could not be captured.
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
        pdfs, folders = _classify(entries)
        return [(fid, name) for fid, name in pdfs.items()], [(fid, name) for fid, name in folders.items()]

    def _capture_pages(self, file_id):
        """Scrolls through the drive preview and collects the raw page images.

        Returns:
            dict mapping the page index to (width, image url, image bytes).
        """

        browser = self._ensure_browser()
        ctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=self.dpr)
        page = ctx.new_page()
        pages = {}
        errors = 0

        def on_response(resp):
            """Stores the highest resolution image seen for each page of the viewer."""
            nonlocal errors
            url = resp.url
            if "viewer/img" not in url:
                return
            m = re.search(r"[?&]page=(\d+)&w=(\d+)", url)
            if not m:
                return
            idx, w = int(m.group(1)), int(m.group(2))
            try:
                body = resp.body()
            except Exception:
                errors += 1
                return
            prev = pages.get(idx)
            if prev is None or w > prev[0]:
                pages[idx] = (w, url, body)

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

            total = 0
            for _ in range(30):
                total = self._total_pages(page)
                if total:
                    break
                page.wait_for_timeout(500)

            deadline = time.time() + self.timeout
            last_n, stable, step = -1, 0, 900
            while time.time() < deadline:
                state = page.evaluate(
                    """(step) => {
                    const sc = Array.from(document.querySelectorAll('*'))
                        .filter(e => e.scrollHeight > e.clientHeight + 50 && e.clientHeight > 200)
                        .sort((a,b) => b.scrollHeight - a.scrollHeight)[0];
                    if (!sc) return null;
                    sc.scrollTop = Math.min(sc.scrollTop + step, sc.scrollHeight - sc.clientHeight);
                    return {top: sc.scrollTop, max: sc.scrollHeight - sc.clientHeight};
                }""",
                    step,
                )
                page.wait_for_timeout(450)
                n = len(pages)
                if n == last_n:
                    stable += 1
                else:
                    stable = 0
                    last_n = n
                if state and state["top"] >= state["max"] - 2 and stable >= 3:
                    if total and n >= total:
                        break
                    if stable >= 6:
                        break

            settle = time.time() + 15
            while time.time() < settle:
                if total and len(pages) >= total:
                    break
                page.wait_for_timeout(500)

            if total and len(pages) < total:
                for _ in range(3):
                    self._wiggle(page)
                    if len(pages) >= total:
                        break

            for idx in list(pages):
                w, url, _ = pages[idx]
                if w < self.max_w:
                    newurl = re.sub(r"&w=\d+", f"&w={self.max_w}", url)
                    try:
                        b64 = page.evaluate(
                            """async (url) => {
                            const r = await fetch(url);
                            if (!r.ok) return null;
                            const ab = await r.arrayBuffer();
                            const bytes = new Uint8Array(ab);
                            let bin = '';
                            for (let i = 0; i < bytes.length; i += 8192)
                                bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 8192));
                            return btoa(bin);
                        }""",
                            newurl,
                        )
                        if b64:
                            pages[idx] = (self.max_w, newurl, base64.b64decode(b64))
                    except Exception:
                        pass

            missing = (
                [i for i in range(total) if i not in pages]
                if total
                else [i for i in range(max(pages) + 1) if i not in pages]
            )
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

    @staticmethod
    def _wiggle(page):
        """Scrolls up a bit then back to the bottom to trigger late-loading pages."""

        page.evaluate(
            """() => {
            const sc = Array.from(document.querySelectorAll('*'))
                .filter(e => e.scrollHeight > e.clientHeight + 50 && e.clientHeight > 200)
                .sort((a,b) => b.scrollHeight - a.scrollHeight)[0];
            if (sc) sc.scrollTop = Math.max(0, sc.scrollHeight - sc.clientHeight - 2500);
        }"""
        )
        page.wait_for_timeout(800)
        page.evaluate(
            """() => {
            const sc = Array.from(document.querySelectorAll('*'))
                .filter(e => e.scrollHeight > e.clientHeight + 50 && e.clientHeight > 200)
                .sort((a,b) => b.scrollHeight - a.scrollHeight)[0];
            if (sc) sc.scrollTop = sc.scrollHeight;
        }"""
        )
        page.wait_for_timeout(4000)
