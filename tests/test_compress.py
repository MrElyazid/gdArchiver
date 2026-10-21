import argparse
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import compress


def make_args(**overrides):
    values = dict(langs=["fra"], jpeg_quality=82, optimize=3, min_gain=0.05, dpi=200,
                  engine="gs", retry_failed=False, limit=0)
    values.update(overrides)
    return argparse.Namespace(**values)


class StateTests(unittest.TestCase):
    def test_roundtrip_and_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = compress.State(Path(tmp) / "state.db")
            try:
                state.set("a.pdf", 100, 1.0, "done", before=1000, after=200, seconds=1.0)
                state.set("b.pdf", 50, 2.0, "kept", before=500, after=500, seconds=0.5)
                self.assertEqual(state.get("a.pdf")["status"], "done")
                self.assertEqual(state.get("a.pdf")["after"], 200)
                self.assertEqual(state.counts(), {"done": 1, "kept": 1})
                self.assertEqual(state.totals(), (1500, 700))
            finally:
                state.close()

    def test_wal_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = compress.State(Path(tmp) / "state.db")
            try:
                mode = state._conn.execute("PRAGMA journal_mode").fetchone()[0]
                self.assertEqual(mode.lower(), "wal")
            finally:
                state.close()


class CommandTests(unittest.TestCase):
    def test_command_has_compression_flags(self):
        command = compress.compress_command(Path("in.pdf"), Path("out.pdf"), ["fra", "ara"], 85, 3)
        self.assertTrue(command[0].endswith("ocrmypdf"))
        self.assertIn("--skip-text", command)
        self.assertEqual(command[command.index("--optimize") + 1], "3")
        self.assertEqual(command[command.index("--jpeg-quality") + 1], "85")
        self.assertEqual(command[command.index("--jobs") + 1], "1")
        self.assertNotIn("--force-ocr", command)


class GsCommandTests(unittest.TestCase):
    def test_gs_command_downsamples_and_encodes_jpeg(self):
        command = compress.gs_command(Path("in.pdf"), Path("out.pdf"), 200, 82)
        self.assertEqual(command[0], "gs")
        self.assertIn("-dDownsampleColorImages=true", command)
        self.assertIn("-dColorImageResolution=200", command)
        self.assertIn("-dDownsampleGrayImages=true", command)
        self.assertIn("-dJPEGQ=82", command)
        self.assertEqual(command[-2], "-sOutputFile=out.pdf")
        self.assertEqual(command[-1], "in.pdf")


class CompressOneTests(unittest.TestCase):
    def make_file(self, tmp, size=1000):
        path = Path(tmp) / "doc.pdf"
        path.write_bytes(b"%PDF-1.4" + b"x" * size)
        return path

    def test_success_replaces_and_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.make_file(tmp)
            state = compress.State(Path(tmp) / "state.db")

            def fake_run(command, **kwargs):
                out = next((a.split("=", 1)[1] for a in command if a.startswith("-sOutputFile=")), command[-1])
                Path(out).write_bytes(b"%PDF-1.4 small")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            try:
                with mock.patch.object(compress.subprocess, "run", side_effect=fake_run):
                    rel, status, detail = compress.compress_one(path, "doc.pdf", make_args(), state, os.environ.copy())
                self.assertEqual((rel, status), ("doc.pdf", "done"))
                self.assertEqual(path.read_bytes(), b"%PDF-1.4 small")
                stored = state.get("doc.pdf")
                self.assertEqual(stored["status"], "done")
                self.assertLess(stored["after"], stored["before"])
            finally:
                state.close()

    def test_bigger_output_keeps_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.make_file(tmp)
            original = path.read_bytes()
            state = compress.State(Path(tmp) / "state.db")

            def fake_run(command, **kwargs):
                out = next((a.split("=", 1)[1] for a in command if a.startswith("-sOutputFile=")), command[-1])
                Path(out).write_bytes(b"%PDF-1.4" + b"y" * 5000)
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            try:
                with mock.patch.object(compress.subprocess, "run", side_effect=fake_run):
                    rel, status, detail = compress.compress_one(path, "doc.pdf", make_args(), state, os.environ.copy())
                self.assertEqual((rel, status), ("doc.pdf", "kept"))
                self.assertEqual(path.read_bytes(), original)
                self.assertFalse((path.parent / (path.name + compress.TMP_SUFFIX)).exists())
                self.assertEqual(state.get("doc.pdf")["status"], "kept")
            finally:
                state.close()

    def test_failure_keeps_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.make_file(tmp)
            original = path.read_bytes()
            state = compress.State(Path(tmp) / "state.db")
            failed = subprocess.CompletedProcess(["ocrmypdf"], 1, stdout="", stderr="boom\n")

            try:
                with mock.patch.object(compress.subprocess, "run", return_value=failed):
                    rel, status, detail = compress.compress_one(path, "doc.pdf", make_args(), state, os.environ.copy())
                self.assertEqual((rel, status), ("doc.pdf", "failed"))
                self.assertEqual(path.read_bytes(), original)
                self.assertFalse((path.parent / (path.name + compress.TMP_SUFFIX)).exists())
                self.assertEqual(state.get("doc.pdf")["status"], "failed")
            finally:
                state.close()


class QueueTests(unittest.TestCase):
    def build_tree(self, tmp):
        root = Path(tmp) / "archive"
        root.mkdir()
        for name in ("a.pdf", "b.pdf", "c.pdf"):
            (root / name).write_bytes(b"%PDF-1.4 " + name.encode())
        return root

    def test_queue_skips_done_kept_and_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.build_tree(tmp)
            state = compress.State(Path(tmp) / "state.db")
            try:
                for name, status in (("a.pdf", "done"), ("b.pdf", "kept"), ("c.pdf", "failed")):
                    stat = (root / name).stat()
                    state.set(name, stat.st_size, stat.st_mtime, status)
                queue = compress.build_queue(root, state, make_args())
                self.assertEqual(queue, [])

                queue = compress.build_queue(root, state, make_args(retry_failed=True))
                self.assertEqual([rel for _p, rel, _s in queue], ["c.pdf"])
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
