#!/usr/bin/env python3
"""Tidies file and directory names of an archive in place.

Applies only mechanical, structure-preserving fixes: decodes html entities (`&amp;`),
strips invisible direction/zero-width marks, normalizes unicode and whitespace, drops
trailing dots/spaces and truncates over-long names. Nothing moves between directories
and nothing is deleted; collisions are skipped and reported.

Dry-run by default: writes a report and prints a summary. With --apply the names are
changed and, unless --no-manifest is given, the paths inside <root>/manifest.csv are
rewritten to match.

Usage:
    python tidy.py saborpcmath_com
    python tidy.py saborpcmath_com --apply
"""

import argparse
import csv
import html
import os
import re
import sys
import time
import unicodedata
from collections import Counter
from pathlib import Path

import ftfy

import viewonly

MAX_NAME_BYTES = 200

# mojibake markers: cyrillic letters and box-drawing characters in otherwise latin names
WEIRD_RE = re.compile(r"[\u0400-\u04ff\u2500-\u25ff]")

# mojibake that ftfy alone does not decode: the site encoded accents as plus+symbol
PLUS_MOJIBAKE = ("+®", "+»", "ÔÇÖ", "Ã", "Â", "Øæƒ")

# decorative leading characters the site owner used to tag folders/files
LEADING_JUNK_RE = re.compile(r"^[\s°+~!·*-]+")


def weird_count(text):
    return len(WEIRD_RE.findall(text))


def fix_mojibake(text, max_rounds=4):
    """Reverses double-decoded utf-8 names (cyrillic/box-drawing artifacts) by trying
    the codepage encode/decode chain until the weird characters disappear."""

    for _ in range(max_rounds):
        if not WEIRD_RE.search(text):
            break
        best = text
        for codec in ("cp866", "cp437", "cp850", "cp1251", "koi8-r",
                      "mac_cyrillic", "cp1252", "latin-1", "mac_roman"):
            try:
                candidate = text.encode(codec).decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            if weird_count(candidate) < weird_count(text):
                best = candidate
                break
        if best == text:
            break
        text = best
    return text


def fix_plus_mojibake(text):
    """Repairs the site's `+®`/`+»` accent mangling and the remaining ftfy cases."""

    text = text.replace("+®", "é").replace("+»", "ï").replace("ÔÇÖ", "’")
    text = text.replace("\xad", "").replace("Øæƒ3", "r³")
    if "Ã" in text or "Â" in text:
        text = ftfy.fix_text(text)
    return text


def clean_name(name):
    """Returns the tidied name, or None when the result would be unusable."""

    clean = name
    for _ in range(3):
        if "&" not in clean:
            break
        unescaped = html.unescape(clean)
        if unescaped == clean:
            break
        clean = unescaped
    clean = re.sub(r"[\u200b\ufeff]", "", clean)
    clean = viewonly.normalize_name(clean)
    clean = fix_plus_mojibake(clean)
    if WEIRD_RE.search(clean):
        clean = fix_mojibake(clean)
    clean = clean.replace("\ufffd", "")
    clean = unicodedata.normalize("NFC", clean)
    clean = re.sub(r"\s+", " ", clean).strip()
    clean = re.sub(r"\s+(\.[A-Za-z0-9]{1,5})$", r"\1", clean)
    clean = LEADING_JUNK_RE.sub("", clean)
    clean = clean.rstrip(". ")
    if not clean or clean in (".", "..") or any(c in clean for c in "/\\\x00"):
        return None
    if len(clean.encode("utf-8")) > MAX_NAME_BYTES:
        clean = viewonly._truncate_name(clean, MAX_NAME_BYTES)
    return clean


def reasons_for(old, new):
    """Returns short human readable reason tags for a rename."""

    tags = []
    if "&" in old and html.unescape(old) != old:
        tags.append("entities")
    stripped = re.sub(r"[\u200b\ufeff\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", old)
    if stripped != old:
        tags.append("invisible")
    if WEIRD_RE.search(old) or any(marker in old for marker in PLUS_MOJIBAKE):
        tags.append("mojibake")
    if "\ufffd" in old:
        tags.append("replacement-char")
    if LEADING_JUNK_RE.match(old):
        tags.append("decoration")
    if re.sub(r"\s+", " ", old.strip()) != old.strip() or old != old.strip():
        tags.append("spaces")
    if unicodedata.normalize("NFC", old) != old:
        tags.append("unicode")
    if old.strip().rstrip(". ") != old.strip() and old.endswith((".", " ")):
        tags.append("trailing")
    if len(old.encode("utf-8")) > MAX_NAME_BYTES and len(new.encode("utf-8")) <= MAX_NAME_BYTES:
        tags.append("truncated")
    return tags


def plan(root):
    """Walks the tree and returns (renames, collisions, stats).

    renames maps the original absolute path to its final absolute path for every entry
    whose name changes; collisions lists entries that could not be renamed.
    """

    root = Path(root)
    renames = {}
    collisions = []
    stats = Counter()

    def visit(orig_dir, final_dir, final_names):
        try:
            entries = sorted(os.listdir(orig_dir))
        except OSError:
            return
        for entry in entries:
            orig = orig_dir / entry
            if orig.is_symlink():
                continue
            clean = clean_name(entry)
            if clean is None:
                collisions.append((str(orig), "unusable name"))
                stats["skipped-unusable"] += 1
                final = orig
            elif clean != entry:
                candidate = orig_dir / clean
                if candidate.exists() or str(candidate) in final_names:
                    collisions.append((str(orig), f"target exists: {clean}"))
                    stats["skipped-collision"] += 1
                    final = orig
                else:
                    final = final_dir / clean
                    for tag in reasons_for(entry, clean):
                        stats[f"renamed-{tag}"] += 1
                    if not reasons_for(entry, clean):
                        stats["renamed-other"] += 1
            else:
                final = final_dir / entry
            final_names.add(str(final))
            # only entries whose own name changes are renamed explicitly; descendants of
            # a renamed directory are carried along by their parent
            if clean is not None and clean != entry and final != orig:
                renames[str(orig)] = str(final)
            if orig.is_dir():
                visit(orig, final, final_names)

    visit(root, root, set())
    return renames, collisions, stats


def remap_path(path, renames):
    """Rewrites a path using the longest renamed prefix found in renames."""

    current = Path(path)
    parts = current.parts
    for depth in range(len(parts), 0, -1):
        prefix = str(Path(*parts[:depth]))
        if prefix in renames:
            rest = Path(*parts[depth:]) if depth < len(parts) else None
            return str(Path(renames[prefix]) / rest) if rest else renames[prefix]
    return path


def apply_renames(root, renames):
    """Renames every planned entry, deepest first, skipping any that now collide.

    Each entry is renamed in place first (only its last component changes); renamed
    parent directories then carry the already renamed children to their final path.
    """

    applied = skipped = 0
    for old, new in sorted(renames.items(), key=lambda kv: kv[0].count(os.sep), reverse=True):
        old_path = Path(old)
        target = old_path.with_name(Path(new).name)
        if target == old_path or not old_path.exists() or target.exists():
            skipped += 1
            continue
        try:
            os.rename(old_path, target)
            applied += 1
        except OSError as e:
            print(f"  could not rename {old}: {e}")
            skipped += 1
    return applied, skipped


def update_manifest(root, renames):
    """Rewrites the paths stored in <root>/manifest.csv to the renamed ones."""

    path = Path(root) / "manifest.csv"
    if not path.exists() or not renames:
        return 0
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)
    changed = 0
    for row in rows:
        stored = row.get("path") or ""
        if not stored:
            continue
        new = remap_path(stored, renames)
        if new != stored:
            row["path"] = new
            changed += 1
    tmp = path.with_suffix(".csv.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    tmp.replace(path)
    return changed


def main():
    parser = argparse.ArgumentParser(description="Tidy archive file and directory names in place")
    parser.add_argument("root", help="archive root to tidy")
    parser.add_argument("--apply", action="store_true", help="perform the renames (default: report only)")
    parser.add_argument("--no-manifest", action="store_true", help="do not rewrite paths in manifest.csv")
    parser.add_argument("--report", default=None,
                        help="report csv path (default: <root>.tidy_report.<timestamp>.csv)")
    args = parser.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        parser.error(f"not a directory: {root}")

    default_report = f"{root.name}.tidy_report.{time.strftime('%Y%m%d-%H%M%S')}.csv"
    report_path = Path(args.report) if args.report else root.parent / default_report

    print(f"scanning {root} ...")
    renames, collisions, stats = plan(root)
    print(f"planned renames: {len(renames)}")
    for key, value in sorted(stats.items()):
        print(f"  {key:22s} {value}")
    if collisions:
        print("skipped:")
        for path, reason in collisions[:10]:
            print(f"  {path[:110]}  ({reason})")
        if len(collisions) > 10:
            print(f"  ... and {len(collisions) - 10} more")

    with open(report_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["old", "new", "status"])
        for old, new in renames.items():
            writer.writerow([old, new, "planned"])
        for path, reason in collisions:
            writer.writerow([path, "", f"skipped: {reason}"])
    print(f"\nreport: {report_path}")

    if args.apply:
        applied, skipped = apply_renames(root, renames)
        print(f"renamed {applied}, skipped {skipped}")
        if not args.no_manifest:
            changed = update_manifest(root, renames)
            print(f"manifest paths updated: {changed}")
    else:
        print("dry run, nothing changed. re-run with --apply to rename.")


if __name__ == "__main__":
    sys.exit(main())
