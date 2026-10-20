import argparse
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ocr


def make_args(**overrides):
    values = dict(langs=["fra"], tesseract_timeout=30, deskew=False, pdfa=False,
                  min_chars=100, retry_failed=False, all=False, force_ocr=False)
    values.update(overrides)
    return argparse.Namespace(**values)


class StateTests(unittest.TestCase):
    def test_roundtrip_and_counts(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = ocr.State(Path(tmp) / "state.db")
            try:
                state.set("a.pdf", 10, 100.0, "done", pages=2, seconds=1.2)
                state.set("b.pdf", 20, 200.0, "failed", error="boom")
                state.set("c.pdf", 30, 300.0, "has-text")
                self.assertEqual(state.get("a.pdf")["status"], "done")
                self.assertEqual(state.get("a.pdf")["pages"], 2)
                self.assertEqual(state.get("b.pdf")["error"], "boom")
                self.assertEqual(state.counts(), {"done": 1, "failed": 1, "has-text": 1})
            finally:
                state.close()


class StateBatchTests(unittest.TestCase):
    def test_wal_mode_and_batched_flush(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "state.db"
            state = ocr.State(db, batch_size=3)
            mode = state._conn.execute("PRAGMA journal_mode").fetchone()[0]
            self.assertEqual(mode.lower(), "wal")
            state.set("a.pdf", 1, 0.0, "done")
            state.set("b.pdf", 2, 0.0, "done")
            self.assertEqual(state.get("a.pdf")["status"], "done")   # pending but visible
            state.set("c.pdf", 3, 0.0, "done")                        # hits the batch size, flushes
            state.close()

            import sqlite3
            con = sqlite3.connect(db)
            rows = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            con.close()
            self.assertEqual(rows, 3)


class StateThreadTests(unittest.TestCase):
    def test_writes_from_worker_threads(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = ocr.State(Path(tmp) / "state.db")
            try:
                from concurrent.futures import ThreadPoolExecutor

                def write(i):
                    state.set(f"{i}.pdf", i, float(i), "done")

                with ThreadPoolExecutor(max_workers=4) as pool:
                    list(pool.map(write, range(20)))
                self.assertEqual(state.counts(), {"done": 20})
            finally:
                state.close()


class HasTextTests(unittest.TestCase):
    def test_text_is_detected(self):
        result = subprocess.CompletedProcess([], 0, stdout="un deux trois")
        with mock.patch.object(ocr.subprocess, "run", return_value=result):
            self.assertTrue(ocr.has_text("x.pdf", min_chars=5))

    def test_empty_text_is_not_detected(self):
        result = subprocess.CompletedProcess([], 0, stdout="  \n ")
        with mock.patch.object(ocr.subprocess, "run", return_value=result):
            self.assertFalse(ocr.has_text("x.pdf", min_chars=5))


class CommandTests(unittest.TestCase):
    def test_command_has_safe_defaults(self):
        command = ocr.ocr_command(Path("in.pdf"), Path("out.pdf"), ["fra", "ara"],
                                  Path("side.txt"), 120, deskew=False, pdfa=False)
        self.assertTrue(command[0].endswith("ocrmypdf"))
        self.assertIn("--skip-text", command)
        self.assertEqual(command[command.index("--jobs") + 1], "1")
        self.assertEqual(command[command.index("-l") + 1], "fra+ara")
        self.assertIn("--sidecar", command)
        self.assertNotIn("--deskew", command)
        self.assertEqual(command[command.index("--output-type") + 1], "pdf")

    def test_pdfa_output_type(self):
        command = ocr.ocr_command(Path("in.pdf"), Path("out.pdf"), ["fra"],
                                  None, 120, deskew=True, pdfa=True)
        self.assertEqual(command[command.index("--output-type") + 1], "pdfa")
        self.assertIn("--deskew", command)
        self.assertNotIn("--sidecar", command)

    def test_force_ocr_replaces_skip_text(self):
        command = ocr.ocr_command(Path("in.pdf"), Path("out.pdf"), ["fra"],
                                  None, 120, deskew=False, pdfa=False, force_ocr=True)
        self.assertIn("--force-ocr", command)
        self.assertNotIn("--skip-text", command)


class QueueTests(unittest.TestCase):
    def build_tree(self, tmp):
        root = Path(tmp) / "archive"
        root.mkdir()
        for name in ("a.pdf", "b.pdf", "c.pdf"):
            (root / name).write_bytes(b"%PDF-1.4 " + name.encode())
        return root

    def test_queue_skips_done_and_text_bearing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.build_tree(tmp)
            state = ocr.State(Path(tmp) / "state.db")
            try:
                stat = (root / "a.pdf").stat()
                state.set("a.pdf", stat.st_size, stat.st_mtime, "done")

                def fake_has_text(path, min_chars=100):
                    return Path(path).name == "b.pdf"

                with mock.patch.object(ocr, "has_text", side_effect=fake_has_text):
                    queue = ocr.build_queue(root, state, make_args(), workers=1)

                self.assertEqual([rel for _path, rel in queue], ["c.pdf"])
                self.assertEqual(state.get("b.pdf")["status"], "has-text")
                self.assertEqual(state.get("a.pdf")["status"], "done")
            finally:
                state.close()

    def test_all_flag_skips_the_text_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.build_tree(tmp)
            state = ocr.State(Path(tmp) / "state.db")
            try:
                with mock.patch.object(ocr, "has_text", side_effect=AssertionError("must not run")):
                    queue = ocr.build_queue(root, state, make_args(all=True), workers=1)
                self.assertEqual(len(queue), 3)
            finally:
                state.close()

    def test_failed_files_are_retried_only_with_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.build_tree(tmp)
            state = ocr.State(Path(tmp) / "state.db")
            try:
                for name in ("a.pdf", "b.pdf", "c.pdf"):
                    stat = (root / name).stat()
                    state.set(name, stat.st_size, stat.st_mtime, "failed")
                with mock.patch.object(ocr, "has_text", return_value=False):
                    queue = ocr.build_queue(root, state, make_args(retry_failed=False), workers=1)
                    self.assertEqual(queue, [])
                    queue = ocr.build_queue(root, state, make_args(retry_failed=True), workers=1)
                    self.assertEqual(len(queue), 3)
            finally:
                state.close()


class OcrOneTests(unittest.TestCase):
    def test_success_replaces_file_and_records_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "doc.pdf"
            path.write_bytes(b"%PDF-1.4 original")
            state = ocr.State(Path(tmp) / "state.db")

            def fake_run(command, **kwargs):
                if command[0].endswith("ocrmypdf"):
                    Path(command[-1]).write_bytes(b"%PDF-1.4 ocr")
                    return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
                if command[0] == "pdfinfo":
                    return subprocess.CompletedProcess(command, 0, stdout="Pages:          3\n", stderr="")
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            try:
                with mock.patch.object(ocr.subprocess, "run", side_effect=fake_run):
                    rel, status, detail = ocr.ocr_one(path, "doc.pdf", make_args(), state, None, os.environ.copy())
                self.assertEqual((rel, status), ("doc.pdf", "done"))
                self.assertEqual(path.read_bytes(), b"%PDF-1.4 ocr")
                stored = state.get("doc.pdf")
                self.assertEqual(stored["status"], "done")
                self.assertEqual(stored["pages"], 3)
            finally:
                state.close()

    def test_failure_keeps_original_and_records_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "doc.pdf"
            path.write_bytes(b"%PDF-1.4 original")
            state = ocr.State(Path(tmp) / "state.db")
            failed = subprocess.CompletedProcess(["ocrmypdf"], 1, stdout="", stderr="hocr failed\n")
            try:
                with mock.patch.object(ocr.subprocess, "run", return_value=failed):
                    rel, status, detail = ocr.ocr_one(path, "doc.pdf", make_args(), state, None, os.environ.copy())
                self.assertEqual((rel, status), ("doc.pdf", "failed"))
                self.assertEqual(path.read_bytes(), b"%PDF-1.4 original")
                self.assertFalse((path.parent / (path.name + ocr.TMP_SUFFIX)).exists())
                self.assertEqual(state.get("doc.pdf")["status"], "failed")
            finally:
                state.close()


class PageCountTests(unittest.TestCase):
    def test_parses_pdfinfo(self):
        result = subprocess.CompletedProcess([], 0, stdout="Title: x\nPages:          42\n", stderr="")
        with mock.patch.object(ocr.subprocess, "run", return_value=result):
            self.assertEqual(ocr.page_count("x.pdf"), 42)


if __name__ == "__main__":
    unittest.main()
