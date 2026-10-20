#!/usr/bin/env python3
"""Builds the internet archive item plan for an archive tree.

Splits the tree into items of at most --max-files files (IA recommends <=1000 per item),
generates identifiers and metadata, and writes ia/items.json. Dry-run by default: it only
prints the plan; the uploader reads the json.

Usage:
    python ia_plan.py saborpcmath_com                 # preview the items
    python ia_plan.py saborpcmath_com --outdir ia     # write ia/items.json
"""

import argparse
import html
import json
import os
import re
import sys
import time
from datetime import date
from pathlib import Path

RIGHTS = (
    "Educational material collected from publicly accessible sources. "
    "Copyright remains with the original authors and institutions. "
    "Provided for educational use; takedown requests are honored."
)

SKIP_ROOT_FILES = {"manifest.csv", "failures.txt"}

SUBJECT_KEYS = (
    ("MATH", "mathematics"),
    ("PHYS", "physics"),
    ("CHIM", "chemistry"),
    ("SVT", "biology"),
    ("INFO", "computer science"),
    ("CONCOURS", "competitive examinations"),
    ("CPGE", "preparatory classes"),
    ("AGREGATION", "agrégation"),
    ("BAC", "baccalaureate"),
    ("LYCEE", "high school"),
    ("ETUDE", "higher education"),
)

ORDER_PREFIXES = ("1BAC", "2BAC", "TCS", "CPGE", "CONCOURS", "AGREGATION", "ETUDE")


def is_pdf(path):
    """True when a .pdf file actually starts with a pdf header."""

    try:
        with open(path, "rb") as handle:
            return handle.read(5) == b"%PDF-"
    except OSError:
        return False


def slug(text, max_len=80):
    """Turns a directory name into a valid internet archive identifier fragment."""

    slugged = text.lower()
    slugged = re.sub(r"[^a-z0-9._-]+", "-", slugged)
    slugged = re.sub(r"-{2,}", "-", slugged).strip("-._")
    return slugged[:max_len] or "item"


def _count(path, cache):
    path = str(path)
    if path in cache:
        return cache[path]
    if os.path.isfile(path):
        if path.lower().endswith(".pdf") and not is_pdf(path):
            result = (0, 0)
        else:
            size = os.path.getsize(path)
            result = (1, size)
    else:
        files = size = 0
        for dirpath, _dirnames, filenames in os.walk(path):
            for name in filenames:
                file_path = os.path.join(dirpath, name)
                if name.lower().endswith(".pdf") and not is_pdf(file_path):
                    continue
                try:
                    size += os.path.getsize(file_path)
                    files += 1
                except OSError:
                    pass
        result = (files, size)
    cache[path] = result
    return result


def split_tree(path, max_files, cache):
    """Returns groups of paths whose file counts each stay within max_files."""

    files, _size = _count(path, cache)
    if files <= max_files:
        return [[str(path)]]
    children = sorted(Path(path).iterdir(), key=lambda p: str(p))
    groups = []
    for child in children:
        if child.name in SKIP_ROOT_FILES and str(child.parent) == str(path):
            continue
        groups += split_tree(child, max_files, cache)

    merged = []
    current = []
    current_files = 0
    for group in groups:
        group_files = sum(_count(item, cache)[0] for item in group)
        if current and current_files + group_files > max_files:
            merged.append(current)
            current = []
            current_files = 0
        current += group
        current_files += group_files
        if current_files >= max_files:
            merged.append(current)
            current = []
            current_files = 0
    if current:
        merged.append(current)
    return merged


def subjects_for(name):
    subjects = [label for key, label in SUBJECT_KEYS if key in name.upper()]
    if not subjects:
        subjects = ["education"]
    subjects.extend(["Morocco", "saborpcmath"])
    return sorted(set(subjects))


def priority(identifier):
    for index, prefix in enumerate(ORDER_PREFIXES):
        if f"-{prefix.lower()}" in identifier:
            return index
    return len(ORDER_PREFIXES)


def build_plan(root, max_files=1000, outdir="ia"):
    root = Path(root)
    cache = {}
    items = []
    used_identifiers = set()

    def unique(identifier):
        """Appends a numeric suffix when two folders slug to the same identifier."""

        candidate = identifier
        counter = 2
        while candidate in used_identifiers:
            candidate = f"{identifier}-{counter}"
            counter += 1
        used_identifiers.add(candidate)
        return candidate

    for top in sorted(root.iterdir(), key=lambda p: str(p)):
        if top.is_file():
            if top.name in SKIP_ROOT_FILES:
                continue
            groups = [[str(top)]]
        elif top.is_dir():
            groups = split_tree(top, max_files, cache)
        else:
            continue
        base = slug(top.name)
        for index, group in enumerate(groups, start=1):
            files = []
            total = 0
            for entry in group:
                entry_path = Path(entry)
                if entry_path.is_file():
                    if entry_path.suffix.lower() == ".pdf" and not is_pdf(entry_path):
                        continue
                    rel = entry_path.relative_to(root).as_posix()
                    files.append(rel)
                    total += entry_path.stat().st_size
                else:
                    for dirpath, _dirnames, filenames in os.walk(entry_path):
                        for filename in filenames:
                            path = Path(dirpath) / filename
                            if path.suffix.lower() == ".pdf" and not is_pdf(path):
                                continue
                            rel = path.relative_to(root).as_posix()
                            files.append(rel)
                            total += path.stat().st_size
            files.sort()
            if not files:
                continue
            identifier = unique(f"saborpcmath-{base}" + (f"-{index}" if len(groups) > 1 else ""))
            name = top.name if top.is_dir() else "misc"
            items.append({
                "identifier": identifier,
                "name": name,
                "base": top.name if top.is_dir() else "",
                "files": files,
                "count": len(files),
                "bytes": total,
                "metadata": {
                    "title": f"SABOR PC Math — {name}",
                    "description": (
                        f"Moroccan mathematics, physics, chemistry and biology course material "
                        f"from {name}, archived from https://saborpcmath.com/ ({len(files)} files). "
                        f"Collected in {date.today().isoformat()}; text layer where available; "
                        f"images downsampled for size."
                    ),
                    "mediatype": "texts",
                    "collection": "opensource",
                    "language": ["ara", "fre"],
                    "subject": subjects_for(name),
                    "rights": RIGHTS,
                    "source": "https://saborpcmath.com/",
                },
            })
    items.sort(key=lambda item: (priority(item["identifier"]), item["identifier"]))
    plan = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "root": str(root),
        "max_files": max_files,
        "items": items,
    }
    return plan


def make_index_item(items, outdir="ia"):
    """Builds the landing item (saborpcmath-archive) and its html content."""

    rows = "\n".join(
        f'<li><a href="https://archive.org/details/{item["identifier"]}">'
        f'{html.escape(item["metadata"]["title"])}</a> — {item["count"]} files</li>'
        for item in items
    )
    content = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>SABOR PC Math archive</title></head>
<body>
<h1>SABOR PC Math archive</h1>
<p>Course material archived from <a href="https://saborpcmath.com/">saborpcmath.com</a>:
{len(items)} items, {sum(i["count"] for i in items)} files.</p>
<ul>
{rows}
</ul>
<p>{html.escape(RIGHTS)}</p>
</body></html>
"""
    item = {
        "identifier": "saborpcmath-archive",
        "name": "index",
        "base": "",
        "local_root": str(Path(outdir).resolve()),
        "files": ["index.html"],
        "count": 1,
        "bytes": len(content.encode("utf-8")),
        "metadata": {
            "title": "SABOR PC Math archive — index",
            "description": (
                f"Index of the SABOR PC Math archive from https://saborpcmath.com/: "
                f"{len(items)} items, {sum(i['count'] for i in items)} files of Moroccan "
                f"mathematics, physics, chemistry and biology course material."
            ),
            "mediatype": "texts",
            "collection": "opensource",
            "language": ["ara", "fre"],
            "subject": ["education", "Morocco", "saborpcmath"],
            "rights": RIGHTS,
            "source": "https://saborpcmath.com/",
        },
    }
    return item, content


def main():
    parser = argparse.ArgumentParser(description="Build the internet archive item plan")
    parser.add_argument("root", help="archive root")
    parser.add_argument("--max-files", type=int, default=1000, help="max files per item (default: 1000)")
    parser.add_argument("--outdir", default="ia", help="where to write items.json (default: ia)")
    parser.add_argument("--no-index", action="store_true", help="do not generate the saborpcmath-archive index item")
    parser.add_argument("--dry-run", action="store_true", help="only print the plan")
    args = parser.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        parser.error(f"not a directory: {root}")

    plan = build_plan(root, args.max_files, args.outdir)
    index_item = None
    index_html = None
    if not args.no_index:
        index_item, index_html = make_index_item(plan["items"], args.outdir)
        plan["items"].insert(0, index_item)
    total_files = sum(item["count"] for item in plan["items"])
    total_bytes = sum(item["bytes"] for item in plan["items"])
    print(f"items: {len(plan['items'])}, files: {total_files}, size: {total_bytes / 1024**3:.1f} GiB")
    print(f"days at 5000 files/day: {total_files / 5000:.1f}")
    for item in plan["items"]:
        print(f"  {item['count']:5d} files {item['bytes'] / 1024**2:8.1f} MB  {item['identifier']}")

    if not args.dry_run:
        outdir = Path(args.outdir)
        outdir.mkdir(parents=True, exist_ok=True)
        if index_html is not None:
            (outdir / "index.html").write_text(index_html, encoding="utf-8")
        out = outdir / "items.json"
        out.write_text(json.dumps(plan, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"\nwrote {out}")
    else:
        print("\ndry run, nothing written")


if __name__ == "__main__":
    sys.exit(main())
