#!/usr/bin/env python3
"""Sweeps an archive tree for junk and duplicate files.

Dry-run by default: only a report is written. With --apply the junk is moved to a
quarantine directory next to the archive root (never inside it) and duplicates are
reported, hardlinked or quarantined depending on the flags. Nothing is deleted, the
quarantine can be removed by hand after review.

Usage:
    python -m helpers.janitor saborpcmath_com                       # report only
    python -m helpers.janitor saborpcmath_com --apply               # quarantine scratch junk
    python -m helpers.janitor saborpcmath_com --apply --purge-empty --purge-parts \
        --purge-dirs vendor,node_modules --trash-duplicates --prune-empty-dirs
"""

import argparse
import csv
import hashlib
import os
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

# google drive's hidden folder of upload scratch copies, uploaded by accident
TMP_COMPONENTS = {".tmp.driveupload"}

# upload scratch names like "13129" or "13129.pdf" (files google renames in transit)
SCRATCH_RE = re.compile(r"^\d{4,6}(\.[A-Za-z0-9]{1,5})?$")

# microsoft office lock/temp files (~$name.docx; tidy strips the tilde, leaving $name)
LOCK_RE = re.compile(r"^~?\$")

# capture caches of the view-only fallback, quarantined only with --purge-parts
PARTS_DIR = ".parts"

CHUNK = 1 << 20


def is_scratch(name):
    """Returns True for numbered upload scratch names like 13129 or 13129.pdf."""

    return bool(SCRATCH_RE.match(name))


def in_tmp_dir(parts):
    """Returns True when any path component is one of google's scratch folders."""

    return any(part in TMP_COMPONENTS for part in parts)


def pick_keeper(paths):
    """Returns the copy to keep: outside scratch folders, no scratch name, shortest path."""

    def key(path):
        return (
            in_tmp_dir(path.parts[:-1]),
            is_scratch(path.name),
            len(path.parts),
            len(path.name),
            str(path),
        )

    return min(paths, key=key)


def sha256_of(path):
    """Returns the sha256 hex digest of a file."""

    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dir_stats(path):
    """Returns (file count, total bytes) for a directory tree."""

    count = 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.stat(os.path.join(dirpath, name)).st_size
                count += 1
            except OSError:
                pass
    return count, total


def collect(root, purge_names=(), purge_patterns=()):
    """Walks the tree, returning (files, zero_bytes, purge_dirs).

    files is a list of (path, size); zero byte files are kept apart, and subdirectories
    whose name is in purge_names or whose relative path matches a purge_patterns regex
    are not descended into: they are returned as (path, file count, bytes) for a
    whole-directory quarantine.
    """

    root = Path(root)
    files = []
    zeros = []
    purge_dirs = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in list(dirnames):
            full = Path(dirpath) / name
            rel = full.relative_to(root).as_posix()
            if name in purge_names or any(re.search(pattern, rel) for pattern in purge_patterns):
                count, size = dir_stats(full)
                purge_dirs.append((full, count, size))
                dirnames.remove(name)
        for name in filenames:
            path = Path(dirpath) / name
            try:
                if path.is_symlink():
                    continue
                size = path.stat().st_size
            except OSError:
                continue
            if size == 0:
                zeros.append(path)
            else:
                files.append((path, size))
    return files, zeros, purge_dirs


def duplicate_groups(files):
    """Hashes files that share a size, returning {sha256: [paths]} for real duplicates."""

    by_size = defaultdict(list)
    for path, size in files:
        by_size[size].append(path)

    groups = defaultdict(list)
    hashed = 0
    for paths in by_size.values():
        if len(paths) < 2:
            continue
        for path in paths:
            try:
                digest = sha256_of(path)
            except OSError:
                continue
            groups[digest].append(path)
            hashed += 1
            if hashed % 2000 == 0:
                print(f"  hashed {hashed} candidate files ...", flush=True)
    return {digest: paths for digest, paths in groups.items() if len(paths) > 1}


def _size_of(path):
    try:
        return path.stat().st_size
    except OSError:
        return 0


def build_plan(root, files, groups, zeros=(), purge_dirs=(),
               hardlink=False, trash_duplicates=False, purge_empty=False, purge_locks=False):
    """Builds the action list.

    Returns a list of (action, reason, path, keeper, size, digest) where action is one of
    trash, trash-dir, hardlink or report. Directories in purge_dirs are always planned as
    trash-dir; duplicates are only reported unless --hardlink or --trash-duplicates is
    given; google scratch junk is always quarantined; zero byte files are quarantined
    only with purge_empty; office lock files only with purge_locks.
    """

    actions = []
    root = Path(root)

    for path, count, size in purge_dirs:
        actions.append(("trash-dir", "purge-dir", path, None, size, ""))

    if purge_empty:
        for path in zeros:
            actions.append(("trash", "zero-byte", path, None, _size_of(path), ""))

    for path, size in files:
        if in_tmp_dir(path.relative_to(root).parts):
            actions.append(("trash", "drive-upload-tmp", path, None, size, ""))
        elif purge_locks and LOCK_RE.match(path.name):
            actions.append(("trash", "office-lock", path, None, size, ""))

    grouped = set()
    for digest, paths in groups.items():
        grouped.update(paths)
        live = [p for p in paths if not in_tmp_dir(p.relative_to(root).parts)]
        if not live:
            continue
        siblings = defaultdict(list)
        for path in live:
            siblings[str(path.parent)].append(path)

        remaining = []
        for path in live:
            scratch = is_scratch(path.name)
            twin = any(not is_scratch(other.name) for other in siblings[str(path.parent)])
            if scratch and twin:
                actions.append(("trash", "upload-scratch-duplicate", path, None, _size_of(path), digest))
                continue
            remaining.append(path)

        if len(remaining) < 2:
            continue
        keeper = pick_keeper(remaining)
        for path in remaining:
            if path == keeper:
                continue
            if hardlink:
                actions.append(("hardlink", "duplicate", path, keeper, _size_of(path), digest))
            elif trash_duplicates:
                actions.append(("trash", "duplicate", path, keeper, _size_of(path), digest))
            else:
                actions.append(("report", "duplicate", path, keeper, _size_of(path), digest))

    for path, size in files:
        if path not in grouped and is_scratch(path.name) and not in_tmp_dir(path.relative_to(root).parts):
            actions.append(("report", "scratch-no-twin", path, None, size, ""))

    return actions


def summarize(actions, files, zeros, purge_dirs):
    """Prints the plan summary and returns the report rows."""

    stats = defaultdict(lambda: [0, 0])
    for action, reason, path, keeper, size, digest in actions:
        stats[(action, reason)][0] += 1
        stats[(action, reason)][1] += size

    total_bytes = sum(size for _, size in files)
    print(f"\nscanned: {len(files)} files, {total_bytes / 1024**3:.2f} GiB")
    print(f"  zero byte: {len(zeros)} | purge dirs: {len(purge_dirs)}")
    print("\nplanned actions (junk is quarantined with --apply, duplicates stay reported by default):")
    for (action, reason), (count, size) in sorted(stats.items()):
        print(f"  {action:9s} {reason:26s} {count:6d} files  {size / 1024**3:.2f} GiB")

    return [
        {
            "action": action,
            "reason": reason,
            "size": size,
            "sha256": digest,
            "path": str(path),
            "keeper": str(keeper) if keeper else "",
        }
        for action, reason, path, keeper, size, digest in actions
    ]


def apply_actions(root, trash_root, actions, prune_empty=False):
    """Moves planned junk to the quarantine and applies hardlink actions."""

    root = Path(root)
    trash_root = Path(trash_root)
    moved = moved_dirs = linked = 0
    done = 0
    for action, reason, path, keeper, size, digest in actions:
        if action == "trash-dir":
            dest = trash_root / path.relative_to(root)
            if dest.exists():
                dest = dest.with_name(dest.name + ".dup")
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(path, dest)
                moved_dirs += 1
            except OSError:
                try:
                    shutil.move(str(path), str(dest))
                    moved_dirs += 1
                except OSError as e:
                    print(f"  could not quarantine {path}: {e}")
        elif action == "trash":
            dest = trash_root / path.relative_to(root)
            if dest.exists():
                dest = dest.with_name(dest.name + ".dup")
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(path, dest)
            except OSError:
                try:
                    shutil.move(str(path), str(dest))
                except OSError as e:
                    print(f"  could not quarantine {path}: {e}")
                    continue
            moved += 1
        elif action == "hardlink" and keeper and Path(keeper).exists():
            try:
                tmp = path.with_name(path.name + ".janitor-link")
                if tmp.exists():
                    tmp.unlink()
                os.link(keeper, tmp)
                os.replace(tmp, path)
                linked += 1
            except OSError as e:
                print(f"  could not hardlink {path}: {e}")
        done += 1
        if done % 1000 == 0:
            print(f"  ... {moved} quarantined, {moved_dirs} dirs, {linked} hardlinked", flush=True)

    if prune_empty:
        removed = 0
        for dirpath, dirnames, filenames in os.walk(root, topdown=False):
            try:
                if dirpath != str(root) and not os.listdir(dirpath):
                    os.rmdir(dirpath)
                    removed += 1
            except OSError:
                pass
        print(f"  pruned {removed} empty directories")

    print(f"done: {moved} files + {moved_dirs} directories quarantined, {linked} files hardlinked")


def main():
    parser = argparse.ArgumentParser(description="Quarantine google upload junk and dedupe an archive tree")
    parser.add_argument("root", help="archive root to sweep")
    parser.add_argument("--apply", action="store_true", help="perform the planned actions (default: report only)")
    parser.add_argument("--hardlink", action="store_true", help="replace duplicate copies with hardlinks to the keeper")
    parser.add_argument("--trash-duplicates", action="store_true", help="quarantine duplicate copies instead of hardlinking")
    parser.add_argument("--purge-empty", action="store_true", help="quarantine zero byte files")
    parser.add_argument("--purge-parts", action="store_true", help="quarantine .parts capture caches")
    parser.add_argument("--purge-locks", action="store_true", help="quarantine microsoft office lock files (~$name)")
    parser.add_argument("--purge-dirs", default="", metavar="NAME[,NAME...]",
                        help="quarantine whole directories with these names, e.g. vendor,node_modules")
    parser.add_argument("--purge-pattern", action="append", default=[], metavar="REGEX",
                        help="quarantine whole directories whose relative path matches this regex (repeatable)")
    parser.add_argument("--prune-empty-dirs", action="store_true", help="remove directories left empty after --apply")
    parser.add_argument("--report", default=None, help="report csv path (default: <root>.janitor_report.csv)")
    parser.add_argument("--trash-root", default=None, help="quarantine directory (default: <root>.janitor_trash)")
    args = parser.parse_args()

    if args.hardlink and args.trash_duplicates:
        parser.error("--hardlink and --trash-duplicates are mutually exclusive")

    root = Path(args.root)
    if not root.is_dir():
        parser.error(f"not a directory: {root}")

    purge_names = {name.strip() for name in args.purge_dirs.split(",") if name.strip()}
    if args.purge_parts:
        purge_names.add(PARTS_DIR)

    report_path = Path(args.report) if args.report else root.parent / f"{root.name}.janitor_report.csv"
    trash_root = Path(args.trash_root) if args.trash_root else root.parent / f"{root.name}.janitor_trash"

    print(f"scanning {root} ...")
    files, zeros, purge_dirs = collect(root, purge_names, args.purge_pattern)
    print(f"  {len(files)} files to analyze, hashing size collisions ...")
    groups = duplicate_groups(files)
    print(f"  duplicate groups: {len(groups)}")

    actions = build_plan(root, files, groups, zeros=zeros, purge_dirs=purge_dirs,
                         hardlink=args.hardlink, trash_duplicates=args.trash_duplicates,
                         purge_empty=args.purge_empty, purge_locks=args.purge_locks)
    rows = summarize(actions, files, zeros, purge_dirs)

    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["action", "reason", "size", "sha256", "path", "keeper"])
        writer.writeheader()
        writer.writerows(rows)
    print(f"\nreport: {report_path}")

    if args.apply:
        print(f"applying: quarantine at {trash_root}")
        apply_actions(root, trash_root, actions, prune_empty=args.prune_empty_dirs)
    else:
        print("dry run, nothing changed. re-run with --apply to quarantine the junk.")


if __name__ == "__main__":
    sys.exit(main())
