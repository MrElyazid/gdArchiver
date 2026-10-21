#!/usr/bin/env python3
"""Compresses the PDFs of an archive in place, resumably.

Uses ocrmypdf with lossy image optimization (JPEG), which keeps the text layer of
already-OCR'd files and OCRs image-only pages as a side effect. Files that would not
shrink meaningfully are left untouched. Every result is recorded in a sqlite state db,
so the pass can be killed and restarted at any time.

Usage:
    python -m helpers.compress saborpcmath_com                     # compress the whole tree
    python -m helpers.compress saborpcmath_com --workers 4 --nice 10
    python -m helpers.compress saborpcmath_com --dry-run           # count files, change nothing
    python -m helpers.compress saborpcmath_com --retry-failed      # retry previously failed files
"""

import argparse
import os
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from helpers import ocr

TMP_SUFFIX = ".gdarchiver-compress.tmp.pdf"
print_lock = threading.Lock()


def log(message):
    with print_lock:
        print(message, flush=True)


class State:
    """Kill-safe sqlite store of per-file compression results."""

    def __init__(self, path, batch_size=100):
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
                before INTEGER,
                after INTEGER,
                seconds REAL,
                error TEXT,
                updated TEXT
            )"""
        )
        self._conn.commit()

    def get(self, rel_path):
        with self._lock:
            if rel_path in self._pending:
                return dict(zip(("size", "mtime", "status", "before", "after", "seconds", "error"),
                                self._pending[rel_path]))
            row = self._conn.execute(
                "SELECT size, mtime, status, before, after, seconds, error FROM files WHERE path = ?",
                (rel_path,),
            ).fetchone()
        if not row:
            return None
        return dict(zip(("size", "mtime", "status", "before", "after", "seconds", "error"), row))

    def set(self, rel_path, size, mtime, status, before=None, after=None, seconds=None, error=""):
        with self._lock:
            self._pending[rel_path] = (size, mtime, status, before, after, seconds, error)
            if len(self._pending) >= self._batch_size:
                self._flush_locked()

    def flush(self):
        with self._lock:
            self._flush_locked()

    def _flush_locked(self):
        if not self._pending:
            return
        self._conn.executemany(
            "INSERT INTO files (path, size, mtime, status, before, after, seconds, error, updated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime, "
            "status=excluded.status, before=excluded.before, after=excluded.after, "
            "seconds=excluded.seconds, error=excluded.error, updated=excluded.updated",
            [(path, *fields) for path, fields in self._pending.items()],
        )
        self._conn.commit()
        self._pending.clear()

    def counts(self):
        self.flush()
        with self._lock:
            return dict(self._conn.execute("SELECT status, COUNT(*) FROM files GROUP BY status").fetchall())

    def totals(self):
        self.flush()
        with self._lock:
            return self._conn.execute(
                "SELECT COALESCE(SUM(before),0), COALESCE(SUM(after),0) FROM files"
            ).fetchone()

    def close(self):
        self.flush()
        with self._lock:
            self._conn.close()


def compress_command(path, tmp_path, langs, jpeg_quality, optimize):
    """Builds the ocrmypdf compression command; page parallelism stays at 1."""

    return [
        ocr.ocrmypdf_bin(),
        "--skip-text",
        "--jobs", "1",
        "--output-type", "pdf",
        "--optimize", str(optimize),
        "--jpeg-quality", str(jpeg_quality),
        "--tesseract-timeout", "300",
        "-l", "+".join(langs),
        str(path),
        str(tmp_path),
    ]


def gs_command(path, tmp_path, dpi, jpeg_quality):
    """Builds the ghostscript command: downsample page images and re-encode them as jpeg.

    This is the fast path: no OCR, roughly 1-3 s for normal files, 13-16 s for a 38 MB
    scan, and it keeps any existing text layer untouched.
    """

    return [
        "gs", "-q", "-dNOPAUSE", "-dBATCH", "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.7",
        "-dAutoFilterColorImages=false", "-dColorImageFilter=/DCTEncode",
        "-dAutoFilterGrayImages=false", "-dGrayImageFilter=/DCTEncode",
        f"-dJPEGQ={jpeg_quality}",
        "-dDownsampleColorImages=true", "-dColorImageDownsampleType=/Bicubic",
        f"-dColorImageResolution={dpi}",
        "-dDownsampleGrayImages=true", "-dGrayImageDownsampleType=/Bicubic",
        f"-dGrayImageResolution={dpi}",
        "-dDownsampleMonoImages=false",
        f"-sOutputFile={tmp_path}", str(path),
    ]


def compress_one(path, rel_path, args, state, env):
    """Compresses one pdf, replaces it only when it shrinks, records the result."""

    tmp_path = path.with_name(path.name + TMP_SUFFIX)
    try:
        if tmp_path.exists():
            tmp_path.unlink()
    except OSError:
        pass

    before = path.stat().st_size
    started = time.time()
    if args.engine == "gs":
        command = gs_command(path, tmp_path, args.dpi, args.jpeg_quality)
    else:
        command = compress_command(path, tmp_path, args.langs, args.jpeg_quality, args.optimize)
    try:
        result = subprocess.run(command, capture_output=True, text=True, env=env)
    except Exception as e:
        _record(state, rel_path, path, "failed", before, None, started, str(e))
        return rel_path, "failed", str(e)

    if result.returncode != 0 or not tmp_path.exists():
        error = (result.stderr.strip().splitlines() or ["ocrmypdf failed"])[-1][:300]
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        _record(state, rel_path, path, "failed", before, None, started, error)
        return rel_path, "failed", error

    after = tmp_path.stat().st_size
    if after >= before * (1 - args.min_gain):
        # not worth it (or bigger): keep the original, avoid a second lossy generation
        tmp_path.unlink()
        _record(state, rel_path, path, "kept", before, before, started)
        return rel_path, "kept", f"{before / 1e6:.1f}MB unchanged"

    try:
        os.replace(tmp_path, path)
    except OSError as e:
        _record(state, rel_path, path, "failed", before, None, started, f"replace failed: {e}")
        return rel_path, "failed", str(e)

    _record(state, rel_path, path, "done", before, after, started)
    return rel_path, "done", f"{before / 1e6:.1f} -> {after / 1e6:.1f}MB ({100 * (1 - after / before):.0f}%)"


def _record(state, rel_path, path, status, before, after, started, error=""):
    try:
        stat = path.stat() if path.exists() else None
        state.set(rel_path, stat.st_size if stat else 0, stat.st_mtime if stat else 0, status,
                  before=before, after=after, seconds=round(time.time() - started, 1), error=error)
    except Exception:
        pass


def build_queue(root, state, args):
    """Returns [(path, rel_path, size)] for pdfs that still need compression."""

    queue = []
    known = 0
    for path, rel_path, size, mtime in ocr.pdf_files(root):
        stored = state.get(rel_path)
        if stored and stored["size"] == size and abs(stored["mtime"] - mtime) < 0.001:
            if stored["status"] in ("done", "kept"):
                known += 1
                continue
            if stored["status"] == "failed" and not args.retry_failed:
                known += 1
                continue
        queue.append((path, rel_path, size))
    log(f"queue: {len(queue)} pdfs to compress, {known} already done or kept")
    return queue


def main():
    parser = argparse.ArgumentParser(description="Compress the pdfs of an archive in place, resumably")
    parser.add_argument("root", help="archive root")
    parser.add_argument("--workers", type=int, default=3, help="parallel files (default: 3)")
    parser.add_argument("--nice", type=int, default=10, help="process niceness, 0 disables (default: 10)")
    parser.add_argument("--engine", choices=("gs", "ocrmypdf"), default="gs",
                        help="gs = fast downsample+jpeg, no OCR (default); ocrmypdf = maximum compression + OCR")
    parser.add_argument("--dpi", type=int, default=200, help="downsample images above this resolution (gs engine, default: 200)")
    parser.add_argument("--lang", default="fra+ara", help="tesseract languages for image-only pages (ocrmypdf engine)")
    parser.add_argument("--optimize", type=int, default=3, help="ocrmypdf optimize level 0-3 (default: 3)")
    parser.add_argument("--jpeg-quality", type=int, default=82, help="jpeg quality for recompressed images (default: 82)")
    parser.add_argument("--min-gain", type=float, default=0.05, help="keep the original unless it shrinks by this fraction (default: 0.05)")
    parser.add_argument("--retry-failed", action="store_true", help="retry files recorded as failed")
    parser.add_argument("--limit", type=int, default=0, help="stop after N files (0 = no limit)")
    parser.add_argument("--dry-run", action="store_true", help="count the queue, change nothing")
    parser.add_argument("--state", default=None, help="state db path (default: <root>.compress_state.sqlite3)")
    args = parser.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        parser.error(f"not a directory: {root}")

    if args.nice > 0:
        os.nice(args.nice)

    args.langs = [lang.strip() for lang in args.lang.split("+") if lang.strip()]
    state_path = Path(args.state) if args.state else root.parent / f"{root.name}.compress_state.sqlite3"

    log(f"state: {state_path}")
    state = State(state_path)
    try:
        queue = build_queue(root, state, args)
        if args.dry_run:
            total = sum(size for _path, _rel, size in queue)
            log(f"dry run: {len(queue)} pdfs, {total / 1024**3:.1f} GiB, nothing changed")
            return

        if not queue:
            log("nothing to do")
            return

        env = os.environ.copy()
        if args.engine == "ocrmypdf":
            tessdata = ocr.ensure_tessdata(args.langs)
            if tessdata:
                env["TESSDATA_PREFIX"] = tessdata
                log(f"using local tessdata: {tessdata}")

        if args.limit:
            queue = queue[:args.limit]
            log(f"limited to {len(queue)} files")

        started = time.time()
        done = kept = failed = 0
        total_before = total_after = 0
        if args.engine == "gs":
            detail = f"dpi {args.dpi}, jpeg {args.jpeg_quality}"
        else:
            detail = f"optimize {args.optimize}, jpeg {args.jpeg_quality}"
        log(f"compress starting: {len(queue)} pdfs, {args.workers} workers, engine {args.engine} ({detail}), nice {args.nice}")
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(compress_one, path, rel_path, args, state, env): (path, rel_path)
                for path, rel_path, _size in queue
            }
            for future in as_completed(futures):
                path, rel_path = futures[future]
                try:
                    _, status, detail = future.result()
                except Exception as e:
                    status, detail = "failed", str(e)
                if status == "done":
                    done += 1
                elif status == "kept":
                    kept += 1
                else:
                    failed += 1
                elapsed = time.time() - started
                finished = done + kept + failed
                rate = finished / elapsed if elapsed else 0
                eta = (len(queue) - finished) / rate if rate else 0
                log(f"[{finished}/{len(queue)}] {status:6s} {rel_path} ({detail}) eta {eta / 3600:.1f}h")
        log(f"finished: {done} compressed, {kept} kept, {failed} failed in {(time.time() - started) / 3600:.1f}h")
        before, after = state.totals()
        if before:
            log(f"bytes: {before / 1024**3:.1f} GiB -> {after / 1024**3:.1f} GiB "
                f"({100 * (1 - after / before):.0f}% smaller)")
    except KeyboardInterrupt:
        log("interrupted, progress is saved; re-run the same command to resume")
        raise SystemExit(130)
    finally:
        state.close()


if __name__ == "__main__":
    sys.exit(main())
