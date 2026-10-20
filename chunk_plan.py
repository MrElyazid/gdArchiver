#!/usr/bin/env python3
"""Splits an OCR queue into size-limited chunks and prints the commands to run them.

Local-only helper for moving the OCR work to a faster machine with limited disk: it
reads the local ocr state, builds chunk lists small enough for the remote disk, and
prints the rsync/OCR/verify/cleanup commands for you to run yourself. It never opens a
connection anywhere. Which chunks were sent or finished is tracked in <outdir>/status.json.

Usage:
    python chunk_plan.py saborpcmath_com                       # plan + print next commands
    python chunk_plan.py saborpcmath_com --verify chunk-01     # check returned text layers
    python chunk_plan.py saborpcmath_com --mark chunk-01 done  # after verify + cleanup
"""

import argparse
import json
import sys
from pathlib import Path

import ocr

DEFAULT_MAX_GIB = 30
STATUS_FILE = "status.json"


def load_status(outdir):
    path = Path(outdir) / STATUS_FILE
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def save_status(outdir, status):
    path = Path(outdir) / STATUS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_queue(root, state, include_failed=False):
    """Returns [(rel_path, size)] that still need OCR, largest structures first by path."""

    queue = []
    for path, rel_path, size, mtime in ocr.pdf_files(root):
        stored = state.get(rel_path)
        if stored and stored["size"] == size and abs(stored["mtime"] - mtime) < 0.001:
            if stored["status"] in ("done", "has-text"):
                continue
            if stored["status"] == "failed" and not include_failed:
                continue
        queue.append((rel_path, size))
    queue.sort()
    return queue


def split_chunks(items, max_bytes):
    """Splits [(rel_path, size)] into chunks no larger than max_bytes (a single bigger file gets its own chunk)."""

    chunks = []
    current = []
    current_size = 0
    for rel_path, size in items:
        if current and current_size + size > max_bytes:
            chunks.append(current)
            current = []
            current_size = 0
        current.append((rel_path, size))
        current_size += size
    if current:
        chunks.append(current)
    return chunks


def covered_by_done_chunks(outdir, status, root_name):
    """Returns the set of rel_paths already handled by chunks marked done."""

    covered = set()
    for name, entry in status.items():
        if not entry.get("done"):
            continue
        list_path = Path(outdir) / f"{name}.files"
        if list_path.exists():
            for line in list_path.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if stripped:
                    prefix = f"{root_name}/"
                    covered.add(stripped[len(prefix):] if stripped.startswith(prefix) else stripped)
    return covered


def write_chunk_files(outdir, root, chunks, status):
    """Writes the file lists; returns the list of chunk names that are still pending."""

    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    names = []
    for index, chunk in enumerate(chunks, start=1):
        name = f"chunk-{index:02d}"
        names.append(name)
        status.setdefault(name, {"sent": False, "done": False})
        lines = [f"{root}/{rel_path}" for rel_path, _size in chunk]
        (outdir / f"{name}.files").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return names


def verify_chunk(outdir, root, name, min_chars=50):
    """Checks that every pdf of a chunk came back with a text layer; returns the bad count."""

    list_path = Path(outdir) / f"{name}.files"
    if not list_path.exists():
        print(f"no such chunk list: {list_path}")
        return 1
    bad = 0
    total = 0
    for line in list_path.read_text(encoding="utf-8").splitlines():
        rel = line.strip()
        if not rel:
            continue
        total += 1
        path = Path(rel)
        if not path.exists():
            print(f"  missing: {rel}")
            bad += 1
        elif not ocr.has_text(path, min_chars=min_chars):
            print(f"  no text layer: {rel}")
            bad += 1
    print(f"{name}: {total - bad}/{total} files have a text layer")
    return 1 if bad else 0


def print_commands(root, outdir, status, names, ssh_key, remote):
    """Prints the per-chunk command sequence for the first chunk that is not done."""

    pending = [name for name in names if not status.get(name, {}).get("done")]
    if not pending:
        print("all chunks are marked done")
        return
    name = pending[0]
    ssh = f'ssh -i {ssh_key} -o IdentitiesOnly=yes {remote}'
    rsync_ssh = f'-e "ssh -i {ssh_key} -o IdentitiesOnly=yes"'
    print(f"""
=== {name} ({len(pending)} chunks pending) ===

# once: install ocrmypdf on the remote and copy the script
#   (run on the remote) uv venv --python 3.13 ~/ocrwork/venv
#   (run on the remote) uv pip install --python ~/ocrwork/venv/bin/python ocrmypdf
#   (here) scp -i {ssh_key} ocr.py {remote}:~/ocrwork/

# 1. send the chunk
rsync -a --info=progress2 --relative --files-from={outdir}/{name}.files {rsync_ssh} ./ {remote}:~/ocrwork/

# 2. OCR it (resumable, rerun the same line after an interruption)
{ssh} 'cd ~/ocrwork && env TMPDIR=$HOME/ocrwork/tmp ~/ocrwork/venv/bin/python ocr.py $HOME/ocrwork/saborpcmath_com --workers 12 --nice 5'

# 3. bring the results and sidecars back
rsync -a --info=progress2 --relative --files-from={outdir}/{name}.files {rsync_ssh} {remote}:~/ocrwork/ ./
rsync -a --info=progress2 {rsync_ssh} {remote}:~/ocrwork/saborpcmath_com.ocr_text/ {root}.ocr_text/ 2>/dev/null || true

# 4. verify, then mark done
python chunk_plan.py {root} --verify {name}
python chunk_plan.py {root} --mark {name} done

# 5. free the remote disk
{ssh} 'rm -rf ~/ocrwork/saborpcmath_com ~/ocrwork/saborpcmath_com.ocr_text'
""")


def main():
    parser = argparse.ArgumentParser(description="Plan size-limited OCR chunks for a remote machine")
    parser.add_argument("root", help="archive root")
    parser.add_argument("--state", default=None, help="state db (default: <root>.ocr_state.sqlite3)")
    parser.add_argument("--outdir", default="chunks", help="directory for chunk lists and status (default: chunks)")
    parser.add_argument("--max-gib", type=float, default=DEFAULT_MAX_GIB, help=f"max chunk size (default: {DEFAULT_MAX_GIB})")
    parser.add_argument("--include-failed", action="store_true", help="include files previously marked failed")
    parser.add_argument("--ssh-key", default="~/.ssh/foxos", help="ssh key to use in the printed commands")
    parser.add_argument("--remote", default="yahya@archdesktop", help="remote ssh target")
    parser.add_argument("--mark", nargs=2, metavar=("CHUNK", "STATUS"), help="set a chunk status (sent|done|pending)")
    parser.add_argument("--verify", metavar="CHUNK", help="check that a chunk's files came back with text")
    parser.add_argument("--reset", action="store_true", help="forget all chunk status")
    args = parser.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        parser.error(f"not a directory: {root}")
    outdir = Path(args.outdir)

    if args.reset:
        status_path = outdir / STATUS_FILE
        if status_path.exists():
            status_path.unlink()
            print(f"removed {status_path}")
        return 0

    if args.verify:
        return verify_chunk(outdir, root, args.verify)

    state_path = Path(args.state) if args.state else root.parent / f"{root.name}.ocr_state.sqlite3"
    status = load_status(outdir)

    if args.mark:
        name, value = args.mark
        status.setdefault(name, {"sent": False, "done": False})[value] = value != "pending"
        save_status(outdir, status)
        print(f"{name}: {value}")
        return 0

    state = ocr.State(state_path)
    try:
        queue = build_queue(root, state, include_failed=args.include_failed)
    finally:
        state.close()
    covered = covered_by_done_chunks(outdir, status, root.name)
    queue = [(rel, size) for rel, size in queue if rel not in covered]

    total_bytes = sum(size for _rel, size in queue)
    max_bytes = int(args.max_gib * 1024**3)
    chunks = split_chunks(queue, max_bytes)
    names = write_chunk_files(outdir, root, chunks, status)
    save_status(outdir, status)

    print(f"queue: {len(queue)} files, {total_bytes / 1024**3:.1f} GiB -> {len(chunks)} chunks (max {args.max_gib} GiB)")
    for index, (name, chunk) in enumerate(zip(names, chunks), start=1):
        size = sum(s for _rel, s in chunk)
        state_label = "done" if status.get(name, {}).get("done") else ("sent" if status.get(name, {}).get("sent") else "pending")
        print(f"  {name}: {len(chunk):5d} files, {size / 1024**3:5.1f} GiB  [{state_label}]")
    print()
    print_commands(args.root, outdir, status, names, args.ssh_key, args.remote)
    return 0


if __name__ == "__main__":
    sys.exit(main())
