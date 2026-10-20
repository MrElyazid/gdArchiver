#!/usr/bin/env python3
"""Uploads the planned internet archive items, resumably and within a per-run file cap.

Reads ia/items.json (from ia_plan.py), uploads pending items in small chunks with the
internetarchive library, and records progress in ia/upload_state.json so a killed run can
continue.  Rejected PDFs are recorded as bad_files instead of aborting the whole item, and
rate-limit responses pause the run instead of burning retries.

Existing files are skipped by checksum, so re-running an item is safe.  --refresh asks IA
which planned files are actually present and recomputes the remaining count before the
batch is selected.

Usage:
    ia configure                             # once, interactive login
    python ia_upload.py --refresh --dry-run  # audit state and show the next batch
    python ia_upload.py --refresh            # upload until the file cap
    python ia_upload.py --audit              # audit every item, upload nothing
    python ia_upload.py --item saborpcmath-2bac-math-sx
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

from internetarchive import get_item, upload

DEFAULT_MAX_FILES = 3000
DEFAULT_CHUNK_SIZE = 100
DEFAULT_THROTTLE_SLEEP = 900
DEFAULT_THROTTLE_RETRIES = 3


class Throttled(Exception):
    """Internet Archive's upload task bucket is full."""


def load_json(path, default):
    path = Path(path)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return default


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def pending_items(plan, state):
    done = state.get("items", {})
    for item in plan["items"]:
        if done.get(item["identifier"], {}).get("status") == "done":
            continue
        yield item


def local_root(item, root):
    return Path(item.get("local_root") or root)


def remote_name(item, rel):
    """Strips the category prefix so each item's file tree starts at its root."""

    base = (item.get("base") or "").strip("/")
    return rel[len(base) + 1:] if base and rel.startswith(base + "/") else rel


def is_generated(name, identifier):
    """True for files internet archive creates itself and that aren't in the plan."""

    return name == "__ia_thumb.jpg" or name in {
        f"{identifier}_archive.torrent",
        f"{identifier}_files.xml",
        f"{identifier}_meta.sqlite",
        f"{identifier}_meta.xml",
        f"{identifier}_reviews.xml",
    }


def remaining_count(item, state, force=False):
    if force:
        return item["count"]
    entry = state.get("items", {}).get(item["identifier"], {})
    if entry.get("status") == "done":
        return 0
    return entry.get("remaining", item["count"])


def select_batch(plan, state, max_files, only=(), force=False):
    """Returns the next items whose remaining file count stays within max_files."""

    batch = []
    total = 0
    items = plan["items"] if force else pending_items(plan, state)
    for item in items:
        if only and item["identifier"] not in only:
            continue
        count = remaining_count(item, state, force=force)
        if count <= 0 and not force:
            continue
        if batch and total + count > max_files:
            break
        batch.append(item)
        total += count
        if total >= max_files:
            break
    return batch, total


def item_files(root, item):
    """Maps remote names to local paths, rejecting missing local files up front."""

    base = local_root(item, root)
    files = {}
    missing = []
    for rel in item["files"]:
        path = base / rel
        if not path.exists():
            missing.append(rel)
            continue
        files[remote_name(item, rel)] = str(path)
    if missing:
        raise FileNotFoundError(f"{len(missing)} files missing, e.g. {missing[0]}")
    return files


def bad_key(error, identifier, files):
    """Extracts the rejected remote name from an internetarchive upload error."""

    text = str(error)
    marker = "error uploading "
    start = text.find(marker)
    if start < 0:
        return None
    rest = text[start + len(marker):]
    for suffix in (f" to {identifier},", f" to {identifier}"):
        if suffix in rest:
            key = rest.split(suffix, 1)[0].strip()
            return key if key in files else None
    return None


def is_throttle(error):
    text = str(error).lower()
    return "reduce your request rate" in text or "bucket_tasks_queued" in text


def is_bad_content(error):
    text = str(error).lower()
    return "syntax error detected in pdf data" in text or "uploaded content is unacceptable" in text


def upload_item(
    root,
    item,
    retries,
    derive,
    dry_run=False,
    chunk_size=DEFAULT_CHUNK_SIZE,
    retry_sleep=10,
    throttle_retries=DEFAULT_THROTTLE_RETRIES,
    throttle_sleep=DEFAULT_THROTTLE_SLEEP,
):
    """Uploads one item in chunks.

    A rejected PDF is removed from its chunk and recorded in bad_files; the rest of the
    item continues.  A full IA task bucket pauses, then raises Throttled after the retry
    budget, so the run stops cleanly rather than hammering the queue.
    """

    files = item_files(root, item)
    if dry_run:
        return {"files": files, "bad_files": [], "uploaded": len(files)}

    keys = list(files)
    bad_files = []
    uploaded = 0
    throttle_waits = 0

    for start in range(0, len(keys), chunk_size):
        chunk = {key: files[key] for key in keys[start:start + chunk_size]}
        attempts = 0
        while chunk:
            try:
                upload(
                    item["identifier"],
                    files=chunk,
                    metadata=item["metadata"],
                    retries=retries,
                    retries_sleep=retry_sleep,
                    checksum=True,
                    queue_derive=False,
                    verbose=False,
                )
                uploaded += len(chunk)
                break
            except KeyboardInterrupt:
                raise
            except Exception as error:
                key = bad_key(error, item["identifier"], chunk)
                if key and is_bad_content(error):
                    bad_files.append(key)
                    del chunk[key]
                    attempts = 0
                    continue
                if is_throttle(error):
                    throttle_waits += 1
                    if throttle_waits > throttle_retries:
                        raise Throttled(str(error)) from error
                    time.sleep(throttle_sleep)
                    continue
                attempts += 1
                if attempts >= max(retries, 1):
                    raise
                time.sleep(retry_sleep)

    if derive and not bad_files:
        try:
            get_item(item["identifier"]).derive(reduced_priority=True)
        except Exception as error:
            print(f"  derive queue failed: {error}", file=sys.stderr)

    return {"files": files, "bad_files": bad_files, "uploaded": uploaded}


def refresh_state(plan, state, root, include_done=False):
    """Reconciles local plan with IA's file list and stores remaining file counts."""

    items = state.setdefault("items", {})
    for item in plan["items"]:
        identifier = item["identifier"]
        entry = items.get(identifier, {})
        if entry.get("status") == "done" and not include_done:
            for key in ("error", "bad_files", "missing", "size_mismatch"):
                entry.pop(key, None)
            entry["remaining"] = 0
            continue
        try:
            remote_item = get_item(identifier)
            remote = {
                file["name"]: file
                for file in remote_item.files
                if not is_generated(file["name"], identifier)
            }
        except Exception as error:
            print(f"  {identifier}: cannot read IA metadata: {error}")
            continue

        base = local_root(item, root)
        missing = mismatched = 0
        for rel in item["files"]:
            name = remote_name(item, rel)
            remote_file = remote.get(name)
            if not remote_file:
                missing += 1
                continue
            try:
                local_size = os.path.getsize(base / rel)
            except OSError:
                missing += 1
                continue
            try:
                remote_size = int(remote_file.get("size", -1))
            except (TypeError, ValueError):
                remote_size = -1
            if remote_size != local_size:
                mismatched += 1

        remaining = missing + mismatched
        if remaining == 0:
            clean = {
                key: value for key, value in entry.items()
                if key not in ("error", "bad_files", "missing", "size_mismatch")
            }
            items[identifier] = {
                **clean,
                "status": "done",
                "files": item["count"],
                "bytes": item["bytes"],
                "remaining": 0,
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            print(f"  done      {identifier} ({item['count']} files)")
        else:
            status = entry.get("status", "pending")
            if status == "done":
                status = "partial"
            items[identifier] = {
                **entry,
                "status": status,
                "remaining": remaining,
                "missing": missing,
                "size_mismatch": mismatched,
                "verified": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            print(f"  pending   {identifier}: {remaining} missing ({missing} absent, {mismatched} size)")
    return state


def main():
    parser = argparse.ArgumentParser(description="Upload planned items to the internet archive")
    parser.add_argument("--plan", default="ia/items.json", help="item plan (default: ia/items.json)")
    parser.add_argument("--state", default="ia/upload_state.json", help="progress state (default: ia/upload_state.json)")
    parser.add_argument("--max-files", type=int, default=DEFAULT_MAX_FILES,
                        help=f"stop this run before exceeding this many files (default: {DEFAULT_MAX_FILES})")
    parser.add_argument("--chunk-size", type=int, default=DEFAULT_CHUNK_SIZE,
                        help=f"files per upload call (default: {DEFAULT_CHUNK_SIZE})")
    parser.add_argument("--item", action="append", default=[], metavar="IDENTIFIER",
                        help="upload only these item identifiers (repeatable)")
    parser.add_argument("--force", action="store_true",
                        help="re-check done items too; requires --item")
    parser.add_argument("--retries", type=int, default=5, help="upload retries per chunk (default: 5)")
    parser.add_argument("--retry-sleep", type=int, default=10, help="seconds between retries (default: 10)")
    parser.add_argument("--throttle-retries", type=int, default=DEFAULT_THROTTLE_RETRIES,
                        help=f"pauses when IA's task bucket is full (default: {DEFAULT_THROTTLE_RETRIES})")
    parser.add_argument("--throttle-sleep", type=int, default=DEFAULT_THROTTLE_SLEEP,
                        help=f"seconds per throttle pause (default: {DEFAULT_THROTTLE_SLEEP})")
    parser.add_argument("--no-derive", action="store_true", help="do not queue full-text derivation")
    parser.add_argument("--refresh", action="store_true",
                        help="audit pending items against IA and recompute remaining counts")
    parser.add_argument("--audit", action="store_true",
                        help="audit every item (including done) against IA, upload nothing")
    parser.add_argument("--dry-run", action="store_true", help="show the next batch, upload nothing")
    args = parser.parse_args()
    if args.force and not args.item:
        parser.error("--force requires at least one --item")

    plan = load_json(args.plan, None)
    if not plan:
        raise SystemExit(f"no plan at {args.plan}; run ia_plan.py first")
    root = Path(plan["root"])
    if not root.is_dir():
        raise SystemExit(f"archive root from the plan does not exist: {root}")

    state = load_json(args.state, {"items": {}})
    state.setdefault("items", {})

    if args.audit:
        refresh_state(plan, state, root, include_done=True)
        save_json(args.state, state)
        print("\naudit complete, nothing uploaded")
        return
    if args.refresh:
        print("refreshing remaining counts from internet archive ...")
        refresh_state(plan, state, root, include_done=False)
        save_json(args.state, state)

    batch, total_files = select_batch(plan, state, args.max_files, args.item, force=args.force)
    if not batch:
        print("nothing pending" + (" for the selected items" if args.item else ""))
        return

    print(f"batch: {len(batch)} items, {total_files} files")
    for item in batch:
        count = remaining_count(item, state, force=args.force)
        print(f"  {count:5d} files {item['bytes'] / 1024**2:8.1f} MB  {item['identifier']}")

    if args.dry_run:
        print("dry run, nothing uploaded")
        return

    uploaded_items = uploaded_files = uploaded_bytes = 0
    for item in batch:
        identifier = item["identifier"]
        remaining = remaining_count(item, state, force=args.force)
        print(f"\nuploading {identifier} ({remaining} files remaining, {item['bytes'] / 1024**2:.1f} MB) ...", flush=True)
        started = time.time()
        try:
            result = upload_item(
                root,
                item,
                args.retries,
                not args.no_derive,
                chunk_size=args.chunk_size,
                retry_sleep=args.retry_sleep,
                throttle_retries=args.throttle_retries,
                throttle_sleep=args.throttle_sleep,
            )
        except KeyboardInterrupt:
            state["items"][identifier] = {"status": "interrupted", "files": item["count"],
                                          "bytes": item["bytes"], "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            save_json(args.state, state)
            print("\ninterrupted, progress saved")
            raise SystemExit(130)
        except Throttled as error:
            state["items"][identifier] = {"status": "throttled", "error": str(error)[:300],
                                          "files": item["count"], "bytes": item["bytes"],
                                          "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            save_json(args.state, state)
            print(f"  throttled: {error}")
            print("  stopping; IA's upload queue is full. re-run after it drains.")
            break
        except Exception as error:
            state["items"][identifier] = {"status": "failed", "error": str(error)[:300],
                                          "files": item["count"], "bytes": item["bytes"],
                                          "at": time.strftime("%Y-%m-%d %H:%M:%S")}
            save_json(args.state, state)
            print(f"  failed: {error}")
            continue

        if result["bad_files"]:
            remaining_files = max(item["count"] - result["uploaded"], 0)
            state["items"][identifier] = {
                "status": "partial",
                "files": item["count"],
                "bytes": item["bytes"],
                "uploaded": result["uploaded"],
                "remaining": remaining_files,
                "bad_files": result["bad_files"],
                "at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            save_json(args.state, state)
            print(f"  partial: {len(result['bad_files'])} rejected, {remaining_files} files remaining")
            for name in result["bad_files"][:5]:
                print(f"    {name}")
            continue

        state["items"][identifier] = {
            "status": "done",
            "files": item["count"],
            "bytes": item["bytes"],
            "remaining": 0,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        save_json(args.state, state)
        uploaded_items += 1
        uploaded_files += result["uploaded"]
        uploaded_bytes += item["bytes"]
        print(f"  done in {time.time() - started:.0f}s", flush=True)

    remaining = list(pending_items(plan, state))
    remaining_files = sum(remaining_count(item, state) for item in remaining)
    print(f"\nuploaded {uploaded_items} items, {uploaded_files} files, {uploaded_bytes / 1024**3:.1f} GiB")
    print(f"remaining: {len(remaining)} items, {remaining_files} files")
    print("re-run after the queue drains / daily limit resets")


if __name__ == "__main__":
    sys.exit(main())
