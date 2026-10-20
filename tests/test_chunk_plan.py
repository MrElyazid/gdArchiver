import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import chunk_plan


class SplitTests(unittest.TestCase):
    def test_chunks_respect_the_size_limit(self):
        items = [("a", 10), ("b", 20), ("c", 5), ("d", 30)]
        chunks = chunk_plan.split_chunks(items, max_bytes=25)
        self.assertEqual([[rel for rel, _ in chunk] for chunk in chunks], [["a"], ["b", "c"], ["d"]])

    def test_files_fill_chunks_greedily(self):
        items = [("a", 10), ("b", 10), ("c", 10), ("d", 30)]
        chunks = chunk_plan.split_chunks(items, max_bytes=25)
        self.assertEqual([[rel for rel, _ in chunk] for chunk in chunks], [["a", "b"], ["c"], ["d"]])

    def test_oversized_file_gets_its_own_chunk(self):
        items = [("a", 10), ("big", 100), ("b", 10)]
        chunks = chunk_plan.split_chunks(items, max_bytes=25)
        self.assertEqual([[rel for rel, _ in chunk] for chunk in chunks], [["a"], ["big"], ["b"]])


class StatusTests(unittest.TestCase):
    def test_load_save_and_covered(self):
        with tempfile.TemporaryDirectory() as tmp:
            outdir = Path(tmp) / "chunks"
            outdir.mkdir()
            (outdir / "chunk-01.files").write_text("saborpcmath_com/a.pdf\nsaborpcmath_com/sub/b.pdf\n", encoding="utf-8")
            status = {"chunk-01": {"sent": True, "done": True}}
            chunk_plan.save_status(outdir, status)

            loaded = chunk_plan.load_status(outdir)
            self.assertEqual(loaded, status)
            covered = chunk_plan.covered_by_done_chunks(outdir, loaded, "saborpcmath_com")
            self.assertEqual(covered, {"a.pdf", "sub/b.pdf"})

    def test_pending_chunks_are_not_covered(self):
        with tempfile.TemporaryDirectory() as tmp:
            outdir = Path(tmp) / "chunks"
            outdir.mkdir()
            (outdir / "chunk-01.files").write_text("saborpcmath_com/a.pdf\n", encoding="utf-8")
            status = {"chunk-01": {"sent": True, "done": False}}
            self.assertEqual(chunk_plan.covered_by_done_chunks(outdir, status, "saborpcmath_com"), set())


class VerifyTests(unittest.TestCase):
    def test_verify_reports_files_without_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "saborpcmath_com"
            root.mkdir()
            (root / "good.pdf").write_bytes(b"%PDF-1.4")
            (root / "bad.pdf").write_bytes(b"%PDF-1.4")
            outdir = Path(tmp) / "chunks"
            outdir.mkdir()
            (outdir / "chunk-01.files").write_text(
                f"{root}/good.pdf\n{root}/bad.pdf\n{root}/missing.pdf\n", encoding="utf-8")

            def fake_has_text(path, min_chars=50):
                return Path(path).name == "good.pdf"

            with mock.patch.object(chunk_plan.ocr, "has_text", side_effect=fake_has_text):
                code = chunk_plan.verify_chunk(outdir, root, "chunk-01")
            self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
