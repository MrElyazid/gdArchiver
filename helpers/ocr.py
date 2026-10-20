#!/usr/bin/env python3
"""Adds a searchable text layer to the image-only PDFs of an archive.

Resumable and gentle on the machine: files that already have text are skipped, every
finished file is recorded in a sqlite state db, and the whole run can be killed and
restarted at any time. OCR itself is delegated to ocrmypdf/tesseract, one file per
worker with page-level parallelism disabled, so a few workers leave the desktop usable.

Usage:
    python -m helpers.ocr saborpcmath_com                    # scan and OCR the queue
    python -m helpers.ocr saborpcmath_com --workers 2 --nice 15
    python -m helpers.ocr saborpcmath_com --dry-run          # show the queue, do nothing
    python -m helpers.ocr saborpcmath_com --retry-failed     # retry previously failed files
"""

import argparse
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

STATE_FIELDS = ("path", "size", "mtime", "status", "pages", "chars", "seconds", "error", "updated")
TESSDATA_URL = "https://github.com/tesseract-ocr/tessdata_best/raw/main/{lang}.traineddata"
TMP_SUFFIX = ".gdarchiver-ocr.tmp.pdf"

print_lock = threading.Lock()


def log(message):
    with print_lock:
        print(message, flush=True)


class State:
    """Kill-safe sqlite store of per-file OCR results, keyed by archive-relative path.

    Uses WAL and batched upserts so scanning thousands of files does not pay an fsync per
    row; pending rows are visible to get() and are flushed on counts(), close() and when
    the batch grows past batch_size.
    """

    ROW_FIELDS = ("size", "mtime", "status", "pages", "chars", "seconds", "error")

    def __init__(self, path, batch_size=200):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._batch_size = batch_size
        self._pending = {}
        self._conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                size INTEGER,
                mtime REAL,
                status TEXT,
                pages INTEGER,
                chars INTEGER,
                seconds REAL,
                error TEXT,
                updated TEXT
            )"""
        )
        self._conn.commit()

    def get(self, rel_path):
        with self._lock:
            if rel_path in self._pending:
                return dict(zip(self.ROW_FIELDS, self._pending[rel_path]))
            row = self._conn.execute(
                "SELECT size, mtime, status, pages, chars, seconds, error FROM files WHERE path = ?",
                (rel_path,),
            ).fetchone()
        if not row:
            return None
        return dict(zip(self.ROW_FIELDS, row))

    def set(self, rel_path, size, mtime, status, pages=None, chars=None, seconds=None, error=""):
        with self._lock:
            self._pending[rel_path] = (size, mtime, status, pages, chars, seconds, error)
            if len(self._pending) >= self._batch_size:
                self._flush_locked()

    def flush(self):
        with self._lock:
            self._flush_locked()

    def _flush_locked(self):
        if not self._pending:
            return
        self._conn.executemany(
            "INSERT INTO files (path, size, mtime, status, pages, chars, seconds, error, updated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime, "
            "status=excluded.status, pages=excluded.pages, chars=excluded.chars, "
            "seconds=excluded.seconds, error=excluded.error, updated=excluded.updated",
            [(path, *fields) for path, fields in self._pending.items()],
        )
        self._conn.commit()
        self._pending.clear()

    def counts(self):
        self.flush()
        with self._lock:
            return dict(self._conn.execute("SELECT status, COUNT(*) FROM files GROUP BY status").fetchall())

    def close(self):
        self.flush()
        with self._lock:
            self._conn.close()


def pdf_files(root):
    """Yields (path, relative posix path, size, mtime) for every pdf under root."""

    root = Path(root)
    for path in root.rglob("*"):
        if path.suffix.lower() != ".pdf":
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_size == 0 or path.is_symlink():
            continue
        yield path, path.relative_to(root).as_posix(), stat.st_size, stat.st_mtime


def has_text(path, min_chars=100, timeout=30):
    """Returns True when the first page already carries a text layer."""

    try:
        result = subprocess.run(
            ["pdftotext", "-f", "1", "-l", "1", "-q", str(path), "-"],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:
        return False
    return len("".join(result.stdout.split())) >= min_chars


def page_count(path, timeout=30):
    """Returns the page count reported by pdfinfo, or 0."""

    try:
        info = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True, timeout=timeout).stdout
    except Exception:
        return 0
    for line in info.splitlines():
        if line.startswith("Pages:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return 0
    return 0


def tesseract_languages():
    """Returns (system tessdata dir, set of installed language codes)."""

    try:
        result = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, timeout=20)
    except Exception:
        return None, set()
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    directory = None
    match = re.search(r'"([^"]+)"', lines[0]) if lines else None
    if match:
        directory = match.group(1)
    langs = set()
    for line in lines[1:]:
        if not line.startswith("List of"):
            langs.add(line.strip())
    return directory, langs


def ensure_tessdata(langs):
    """Makes sure every requested language exists, downloading missing ones locally.

    Returns the TESSDATA_PREFIX directory to use, or None to keep the system default.
    """
    system_dir, installed = tesseract_languages()
    missing = [lang for lang in langs if lang not in installed]
    if not missing:
        return None
    if not system_dir or not Path(system_dir).is_dir():
        raise SystemExit("tesseract is not installed or its tessdata directory was not found")

    local_dir = Path.home() / ".local" / "share" / "gdarchiver" / "tessdata"
    local_dir.mkdir(parents=True, exist_ok=True)
    for item in Path(system_dir).iterdir():
        link = local_dir / item.name
        if not link.exists():
            try:
                link.symlink_to(item)
            except OSError:
                pass
    for lang in missing:
        target = local_dir / f"{lang}.traineddata"
        log(f"  downloading {lang}.traineddata ...")
        tmp = target.with_suffix(".part")
        with urllib.request.urlopen(TESSDATA_URL.format(lang=lang), timeout=120) as response:
            tmp.write_bytes(response.read())
        tmp.replace(target)
    return str(local_dir)


def ocrmypdf_bin():
    """Returns the ocrmypdf executable, preferring the current virtualenv."""

    found = shutil.which("ocrmypdf")
    if found:
        return found
    candidate = Path(sys.executable).with_name("ocrmypdf")
    return str(candidate) if candidate.exists() else "ocrmypdf"


def ocr_command(path, tmp_path, langs, sidecar, timeout, deskew, pdfa, force_ocr=False):
    """Builds the ocrmypdf command; page parallelism stays at 1 to keep the cpu calm."""

    command = [
        ocrmypdf_bin(),
        "--force-ocr" if force_ocr else "--skip-text",
        "--jobs", "1",
        "--output-type", "pdfa" if pdfa else "pdf",
        "--tesseract-timeout", str(timeout),
        "-l", "+".join(langs),
    ]
    if sidecar:
        command += ["--sidecar", str(sidecar)]
    if deskew:
        command.append("--deskew")
    command += [str(path), str(tmp_path)]
    return command


def ocr_one(path, rel_path, args, state, sidecar_dir, env):
    """Runs ocrmypdf for one file, replaces it atomically and records the result."""

    tmp_path = path.with_name(path.name + TMP_SUFFIX)
    try:
        if tmp_path.exists():
            tmp_path.unlink()
    except OSError:
        pass

    sidecar = None
    if sidecar_dir is not None:
        sidecar = Path(sidecar_dir) / Path(rel_path).with_suffix(".txt")
        sidecar.parent.mkdir(parents=True, exist_ok=True)

    started = time.time()
    try:
        result = subprocess.run(
            ocr_command(path, tmp_path, args.langs, sidecar, args.tesseract_timeout, args.deskew, args.pdfa),
            capture_output=True, text=True, env=env,
        )
    except Exception as e:
        _record_failure(state, rel_path, path, started, str(e))
        return rel_path, "failed", str(e)

    if result.returncode != 0:
        error = (result.stderr.strip().splitlines() or ["ocrmypdf failed"])[-1][:300]
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        _record_failure(state, rel_path, path, started, error)
        return rel_path, "failed", error

    try:
        os.replace(tmp_path, path)
    except OSError as e:
        _record_failure(state, rel_path, path, started, f"replace failed: {e}")
        return rel_path, "failed", str(e)

    stat = path.stat()
    pages = page_count(path)
    state.set(rel_path, stat.st_size, stat.st_mtime, "done", pages=pages,
              chars=None, seconds=round(time.time() - started, 1))
    return rel_path, "done", f"{pages} pages in {time.time() - started:.0f}s"


def _record_failure(state, rel_path, path, started, error):
    try:
        stat = path.stat() if path.exists() else None
        state.set(rel_path, stat.st_size if stat else 0, stat.st_mtime if stat else 0,
                  "failed", seconds=round(time.time() - started, 1), error=error)
    except Exception:
        pass


def build_queue(root, state, args, workers):
    """Returns the list of (path, rel_path) that still need OCR."""

    candidates = []
    known = 0
    for path, rel_path, size, mtime in pdf_files(root):
        stored = state.get(rel_path)
        if stored and stored["size"] == size and abs(stored["mtime"] - mtime) < 0.001:
            if stored["status"] in ("done", "has-text"):
                known += 1
                continue
            if stored["status"] == "failed" and not args.retry_failed:
                known += 1
                continue
        candidates.append((path, rel_path))

    if args.all:
        log(f"queue: {len(candidates)} files to OCR (--all skips the text check), {known} already done")
        return candidates

    log(f"checking {len(candidates)} candidate pdfs for an existing text layer ...")
    queue = []
    checked = 0
    with ThreadPoolExecutor(max_workers=workers * 4) as pool:
        futures = {pool.submit(has_text, path, args.min_chars): (path, rel_path)
                   for path, rel_path in candidates}
        for future in as_completed(futures):
            path, rel_path = futures[future]
            checked += 1
            try:
                if future.result():
                    stat = path.stat()
                    state.set(rel_path, stat.st_size, stat.st_mtime, "has-text")
                    known += 1
                    if checked % 500 == 0:
                        log(f"  checked {checked}/{len(candidates)} ({known} already text-bearing)")
                        state.flush()
                    continue
            except Exception:
                pass
            queue.append((path, rel_path))
    state.flush()
    log(f"queue: {len(queue)} files to OCR, {known} already done or text-bearing")
    return queue


def main():
    parser = argparse.ArgumentParser(description="OCR the image-only pdfs of an archive, resumably")
    parser.add_argument("root", help="archive root")
    parser.add_argument("--workers", type=int, default=3, help="parallel files (default: 3)")
    parser.add_argument("--nice", type=int, default=10, help="process niceness, 0 disables (default: 10)")
    parser.add_argument("--lang", default="fra+ara", help="tesseract languages separated by + (default: fra+ara)")
    parser.add_argument("--min-chars", type=int, default=100, help="text chars on page 1 that count as a text layer")
    parser.add_argument("--retry-failed", action="store_true", help="retry files recorded as failed")
    parser.add_argument("--all", action="store_true", help="process every pdf, letting --skip-text keep existing text")
    parser.add_argument("--limit", type=int, default=0, help="stop after N files (0 = no limit)")
    parser.add_argument("--dry-run", action="store_true", help="build the queue and print counts, do not OCR")
    parser.add_argument("--deskew", action="store_true", help="deskew pages before OCR (slower)")
    parser.add_argument("--pdfa", action="store_true", help="produce PDF/A output (slower)")
    parser.add_argument("--tesseract-timeout", type=int, default=300, help="per-page OCR timeout in seconds")
    parser.add_argument("--sidecar-dir", default=None, help="where to write plain text sidecars (default: <root>.ocr_text)")
    parser.add_argument("--no-sidecar", action="store_true", help="do not write text sidecars")
    parser.add_argument("--state", default=None, help="state db path (default: <root>.ocr_state.sqlite3)")
    args = parser.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        parser.error(f"not a directory: {root}")

    if args.nice > 0:
        os.nice(args.nice)

    args.langs = [lang.strip() for lang in args.lang.split("+") if lang.strip()]
    state_path = Path(args.state) if args.state else root.parent / f"{root.name}.ocr_state.sqlite3"
    sidecar_dir = None
    if not args.no_sidecar:
        sidecar_dir = Path(args.sidecar_dir) if args.sidecar_dir else root.parent / f"{root.name}.ocr_text"
        sidecar_dir.mkdir(parents=True, exist_ok=True)

    log(f"state: {state_path}")
    state = State(state_path)
    try:
        queue = build_queue(root, state, args, args.workers)
        log(f"state counts: {state.counts()}")
        if args.dry_run:
            pages = 0
            with ThreadPoolExecutor(max_workers=args.workers * 4) as pool:
                for count in pool.map(page_count, [path for path, _ in queue]):
                    pages += count
            log(f"dry run: {len(queue)} files, {pages} pages, nothing changed")
            return

        if not queue:
            log("nothing to do")
            return

        text_env = os.environ.copy()
        tessdata = ensure_tessdata(args.langs)
        if tessdata:
            text_env["TESSDATA_PREFIX"] = tessdata
            log(f"using local tessdata: {tessdata}")

        if args.limit:
            queue = queue[:args.limit]
            log(f"limited to {len(queue)} files")

        started = time.time()
        done = failed = 0
        log(f"ocr starting: {len(queue)} files, {args.workers} workers, langs {'+'.join(args.langs)}, nice {args.nice}")
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(ocr_one, path, rel_path, args, state, sidecar_dir, text_env): rel_path
                for path, rel_path in queue
            }
            for future in as_completed(futures):
                rel_path = futures[future]
                try:
                    _, status, detail = future.result()
                except Exception as e:
                    status, detail = "failed", str(e)
                if status == "done":
                    done += 1
                else:
                    failed += 1
                elapsed = time.time() - started
                rate = (done + failed) / elapsed if elapsed else 0
                remaining = len(queue) - done - failed
                eta = remaining / rate if rate else 0
                log(f"[{done + failed}/{len(queue)}] {status:6s} {rel_path} ({detail}) "
                    f"eta {eta / 3600:.1f}h")
        log(f"finished: {done} done, {failed} failed in {(time.time() - started) / 3600:.1f}h")
    except KeyboardInterrupt:
        log("interrupted, progress is saved; re-run the same command to resume")
        raise SystemExit(130)
    finally:
        state.close()


if __name__ == "__main__":
    sys.exit(main())
