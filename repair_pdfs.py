#!/usr/bin/env python3
"""Repairs PDFs whose catalog entries IA rejects as malformed.

Internet Archive's upload validator refuses PDFs with broken /AcroForm or
/OCProperties dictionaries even when poppler, qpdf and Ghostscript can read
them.  Removing the malformed catalog key fixes the rejection without
touching the page content or the OCR text layer.

Originals are copied to <root>.repair_trash/<relative path> before being
replaced.  A repair is only applied when the rebuilt file passes pdfinfo with
no Syntax Error, keeps the same page count and produces the same first-page
text.

Usage:
    python repair_pdfs.py saborpcmath_com --dry-run
    python repair_pdfs.py saborpcmath_com
"""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pikepdf

# poppler message -> catalog key that has to go.
BROKEN_KEYS = (
    ("Can't get Fields array", "/AcroForm"),
    ("Expected the optional content group list", "/OCProperties"),
    ("Expected the default config", "/OCProperties"),
)


def is_pdf(path):
    """True when the file starts with a pdf header."""

    try:
        with open(path, "rb") as handle:
            return handle.read(5) == b"%PDF-"
    except OSError:
        return False


def run_pdfinfo(path, timeout):
    return subprocess.run(
        ["pdfinfo", str(path)],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def syntax_errors(path, timeout):
    result = run_pdfinfo(path, timeout)
    return [line.strip() for line in result.stderr.splitlines() if "Syntax Error" in line]


def page_count(info_stdout):
    for line in info_stdout.splitlines():
        if line.startswith("Pages:"):
            return line.split()[1]
    return None


def first_page_text(path, timeout):
    result = subprocess.run(
        ["pdftotext", "-f", "1", "-l", "1", "-enc", "UTF-8", str(path), "-"],
        capture_output=True,
        timeout=timeout,
    )
    return hashlib.md5(result.stdout).hexdigest()


def keys_for(errors):
    keys = set()
    for error in errors:
        matched = False
        for needle, key in BROKEN_KEYS:
            if needle in error:
                keys.add(key)
                matched = True
                break
        if not matched:
            return None
    return keys


def repair(path, root, backup_root, timeout=120, dry_run=False):
    """Repairs one pdf. Returns (status, detail)."""

    if not is_pdf(path):
        return "not-pdf", "header is not %PDF-"
    try:
        errors = syntax_errors(path, timeout)
    except subprocess.TimeoutExpired:
        return "failed", f"pdfinfo timed out after {timeout}s"
    if not errors:
        return "ok", ""

    keys = keys_for(errors)
    if keys is None:
        return "failed", f"unhandled syntax error: {errors[0]}"

    before = run_pdfinfo(path, timeout)
    before_pages = page_count(before.stdout)
    try:
        before_text = first_page_text(path, timeout)
    except subprocess.TimeoutExpired:
        return "failed", f"pdftotext timed out after {timeout}s"
    before_size = path.stat().st_size

    tmp = path.with_name(path.name + ".repair.tmp")
    try:
        with pikepdf.open(path) as pdf:
            removed = []
            for key in sorted(keys):
                if key in pdf.Root:
                    del pdf.Root[key]
                    removed.append(key)
            if not removed:
                return "failed", f"catalog keys not found: {sorted(keys)}"
            pdf.save(tmp)

        after = run_pdfinfo(tmp, timeout)
        after_errors = [line for line in after.stderr.splitlines() if "Syntax Error" in line]
        if after_errors:
            return "failed", f"still rejected: {after_errors[0].strip()}"
        if page_count(after.stdout) != before_pages:
            return "failed", f"page count changed: {before_pages} -> {page_count(after.stdout)}"
        if first_page_text(tmp, timeout) != before_text:
            return "failed", "first-page text changed"

        detail = f"{','.join(removed)} {before_size} -> {tmp.stat().st_size} bytes"
        if dry_run:
            return "would-repair", detail

        relative = path.relative_to(root)
        backup = backup_root / relative
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            shutil.copy2(path, backup)
        os.replace(tmp, path)
        return "repaired", detail
    except Exception as error:  # pikepdf/pdfminer errors are not all OSError
        return "failed", f"{type(error).__name__}: {error}"
    finally:
        if tmp.exists():
            tmp.unlink()


def main():
    parser = argparse.ArgumentParser(description="Repair PDFs rejected by Internet Archive")
    parser.add_argument("root", help="archive root")
    parser.add_argument("--backup", help="backup directory (default: <root>.repair_trash)")
    parser.add_argument("--timeout", type=int, default=120, help="per-command timeout (default: 120)")
    parser.add_argument("--dry-run", action="store_true", help="report repairs without changing files")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    if not root.is_dir():
        parser.error(f"not a directory: {root}")
    backup_root = Path(args.backup).resolve() if args.backup else root.with_name(root.name + ".repair_trash")

    files = sorted(path for path in root.rglob("*") if path.is_file() and path.suffix.lower() == ".pdf")
    print(f"scanning {len(files)} pdf files")
    counts = {}
    for index, path in enumerate(files, start=1):
        status, detail = repair(path, root, backup_root, args.timeout, args.dry_run)
        counts[status] = counts.get(status, 0) + 1
        if status not in ("ok",):
            print(f"  {status:12s} {path.relative_to(root)}")
            if detail:
                print(f"               {detail}")
        if index % 2000 == 0:
            print(f"  ... {index}/{len(files)}", flush=True)

    print("\nsummary:")
    for status in sorted(counts):
        print(f"  {status:12s} {counts[status]}")
    return 0 if counts.get("failed", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
