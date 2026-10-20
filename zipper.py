#!/usr/bin/env python3
"""Zips code project folders and quarantines the originals.

A unit is a directory containing at least --min-code code files, a code ratio of at
least --min-ratio and at most --max-docs documents; the shallowest such directory wins,
so nested projects are covered by a single zip. Mixed folders with course pdfs fail the
document limit and are split further, keeping the pdfs out of the zips and searchable.
Dry-run by default: the proposed units are only listed. With --apply each unit is zipped
next to itself, verified, recorded in a manifest, and the original directory is moved to
a quarantine directory (never deleted).

Usage:
    python zipper.py saborpcmath_com                       # list proposed units
    python zipper.py saborpcmath_com --apply               # zip, verify, quarantine
    python zipper.py saborpcmath_com --apply --unit "SMI6/S6 SMI/000 Développement web"
"""

import argparse
import csv
import hashlib
import os
import shutil
import sys
import zipfile
from pathlib import Path

CODE_EXTENSIONS = {
    ".php", ".js", ".java", ".py", ".cpp", ".h", ".c", ".cs", ".rb", ".go", ".rs",
    ".sh", ".bat", ".sql", ".html", ".css", ".scss", ".ts", ".tsx", ".jsx",
    ".xml", ".json", ".yml", ".yaml", ".twig", ".gradle", ".properties", ".ini",
    ".conf", ".lock", ".class", ".jar", ".xlf",
}

# documents stay out of zips so they remain searchable, units must be nearly doc-free
DOC_EXTENSIONS = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".odt", ".odp"}

# trees that never count towards a project's code ratio
SKIP_DIRS = {".git", ".parts", "vendor", "node_modules", "__pycache__"}

CHUNK = 1 << 20


def sha256_of(path):
    """Returns the sha256 hex digest of a file."""

    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def subtree_stats(root):
    """Returns ({dir: (code, docs, files, bytes)}, {dir: [child dirs]}) for the whole tree."""

    root = str(Path(root))
    own = {}
    children = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        children[dirpath] = [os.path.join(dirpath, d) for d in dirnames]
        code = docs = files = 0
        total = 0
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                total += os.stat(path).st_size
            except OSError:
                continue
            files += 1
            ext = os.path.splitext(name)[1].lower()
            if ext in CODE_EXTENSIONS:
                code += 1
            elif ext in DOC_EXTENSIONS:
                docs += 1
        own[dirpath] = (code, docs, files, total)

    stats = {}

    def total_for(path):
        if path in stats:
            return stats[path]
        code, docs, files, size = own.get(path, (0, 0, 0, 0))
        for child in children.get(path, []):
            c, d, f, s = total_for(child)
            code += c
            docs += d
            files += f
            size += s
        stats[path] = (code, docs, files, size)
        return stats[path]

    total_for(root)
    return stats, children


def pick_units(stats, children, root, min_code=20, min_ratio=0.6, max_docs=5):
    """Returns the shallowest directories that qualify as code project units.

    A unit needs at least min_code code files, a code ratio of at least min_ratio and at
    most max_docs documents, so mixed folders with course pdfs are split further and the
    pdfs stay out of the zips.
    """

    root = str(Path(root))
    units = []

    def visit(path):
        code, docs, files, _size = stats.get(path, (0, 0, 0, 0))
        if files and code >= min_code and code / files >= min_ratio and docs <= max_docs:
            units.append(Path(path))
            return
        for child in sorted(children.get(path, [])):
            visit(child)

    visit(root)
    return units


def zip_unit(unit, zip_path):
    """Writes the zip with the unit directory as its top level entry; returns the file count."""

    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for dirpath, _dirnames, filenames in os.walk(unit):
            for name in sorted(filenames):
                path = Path(dirpath) / name
                arcname = Path(unit.name) / path.relative_to(unit)
                archive.write(path, arcname=str(arcname))
                count += 1
    return count


def verify_zip(zip_path, expected_files):
    """Returns (ok, message); checks entry count and integrity."""

    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
        if len(names) != expected_files:
            return False, f"{len(names)} entries != {expected_files} files"
        bad = archive.testzip()
        if bad:
            return False, f"corrupt entry: {bad}"
    return True, ""


def describe(root, units):
    """Prints the proposed units and returns (file count, total bytes) per unit."""

    rows = []
    print(f"proposed zip units: {len(units)}")
    for unit in units:
        files = size = 0
        for dirpath, _dirnames, filenames in os.walk(unit):
            for name in filenames:
                try:
                    size += (Path(dirpath) / name).stat().st_size
                    files += 1
                except OSError:
                    pass
        rel = unit.relative_to(root)
        print(f"  {files:6d} files  {size / 1024**2:9.2f} MB  {rel}  ->  {rel}.zip")
        rows.append((unit, files, size))
    total_files = sum(r[1] for r in rows)
    total_size = sum(r[2] for r in rows)
    print(f"\ntotal: {len(units)} zips for {total_files} files / {total_size / 1024**3:.2f} GiB")
    return rows


def apply_units(root, trash_root, units, manifest_path, force=False, prune_empty=False):
    """Zips each unit, verifies it, quarantines the original and writes the manifest."""

    rows = []
    for unit in units:
        rel = unit.relative_to(root)
        zip_path = unit.with_name(unit.name + ".zip")
        if zip_path.exists() and not force:
            print(f"  exists, skipping: {zip_path.name}")
            rows.append({"unit": str(rel), "zip": str(zip_path.relative_to(root)), "files": "",
                         "bytes": "", "zip_bytes": zip_path.stat().st_size, "sha256": "", "status": "exists"})
            continue
        try:
            count = zip_unit(unit, zip_path)
            ok, message = verify_zip(zip_path, count)
            if not ok:
                raise RuntimeError(message)
        except Exception as e:
            print(f"  failed {rel}: {e}")
            rows.append({"unit": str(rel), "zip": "", "files": "", "bytes": "",
                         "zip_bytes": "", "sha256": "", "status": f"error: {e}"})
            continue

        size = sum((Path(dirpath) / name).stat().st_size
                   for dirpath, _dirnames, filenames in os.walk(unit) for name in filenames)
        digest = sha256_of(zip_path)

        dest = trash_root / rel
        if dest.exists():
            dest = dest.with_name(dest.name + ".dup")
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(unit, dest)
        except OSError:
            shutil.move(str(unit), str(dest))

        print(f"  zipped {count} files -> {zip_path.name} ({zip_path.stat().st_size / 1024**2:.1f} MB)")
        rows.append({"unit": str(rel), "zip": str(zip_path.relative_to(root)), "files": count,
                     "bytes": size, "zip_bytes": zip_path.stat().st_size, "sha256": digest, "status": "ok"})

    with open(manifest_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["unit", "zip", "files", "bytes", "zip_bytes", "sha256", "status"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"manifest: {manifest_path}")

    if prune_empty:
        removed = 0
        for dirpath, _dirnames, _filenames in os.walk(root, topdown=False):
            try:
                if dirpath != str(root) and not os.listdir(dirpath):
                    os.rmdir(dirpath)
                    removed += 1
            except OSError:
                pass
        print(f"pruned {removed} empty directories")


def main():
    parser = argparse.ArgumentParser(description="Zip code project folders and quarantine the originals")
    parser.add_argument("root", help="archive root to scan")
    parser.add_argument("--apply", action="store_true", help="create the zips (default: list units only)")
    parser.add_argument("--min-code", type=int, default=20, help="minimum code files for a unit (default: 20)")
    parser.add_argument("--min-ratio", type=float, default=0.6, help="minimum code ratio for a unit (default: 0.6)")
    parser.add_argument("--max-docs", type=int, default=5, help="maximum documents in a unit, so pdfs stay searchable (default: 5)")
    parser.add_argument("--unit", action="append", default=[], metavar="PATH",
                        help="force a unit by its relative path (repeatable, replaces auto units below it)")
    parser.add_argument("--exclude", action="append", default=[], metavar="PATH",
                        help="never zip this relative path (repeatable)")
    parser.add_argument("--force", action="store_true", help="overwrite an existing zip")
    parser.add_argument("--prune-empty-dirs", action="store_true", help="remove directories left empty after --apply")
    parser.add_argument("--trash-root", default=None, help="quarantine directory (default: <root>.zipper_trash)")
    parser.add_argument("--manifest", default=None, help="manifest csv path (default: <root>.zipper_manifest.csv)")
    args = parser.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        parser.error(f"not a directory: {root}")

    trash_root = Path(args.trash_root) if args.trash_root else root.parent / f"{root.name}.zipper_trash"
    manifest_path = Path(args.manifest) if args.manifest else root.parent / f"{root.name}.zipper_manifest.csv"

    print(f"scanning {root} ...")
    stats, children = subtree_stats(root)
    units = pick_units(stats, children, root, min_code=args.min_code,
                       min_ratio=args.min_ratio, max_docs=args.max_docs)

    excluded = [(root / rel).resolve() for rel in args.exclude]
    units = [u for u in units if not any(u.resolve() == e or e in u.resolve().parents for e in excluded)]

    for rel in args.unit:
        forced = (root / rel).resolve()
        if not forced.is_dir():
            parser.error(f"--unit is not a directory: {rel}")
        units = [u for u in units if u.resolve() != forced and forced not in u.resolve().parents]
        if forced not in [u.resolve() for u in units]:
            units.append(forced)

    units = sorted(units)
    rows = describe(root, units)

    if not units:
        print("no code project units found")
        return

    if args.apply:
        print(f"applying: quarantine at {trash_root}")
        apply_units(root, trash_root, units, manifest_path, force=args.force,
                    prune_empty=args.prune_empty_dirs)
    else:
        print("dry run, nothing changed. re-run with --apply to zip and quarantine.")


if __name__ == "__main__":
    sys.exit(main())
