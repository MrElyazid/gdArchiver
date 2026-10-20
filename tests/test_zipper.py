import tempfile
import unittest
import zipfile
from pathlib import Path

import zipper


class PickUnitsTests(unittest.TestCase):
    def build_tree(self, tmp):
        root = Path(tmp) / "archive"
        (root / "docs").mkdir(parents=True)
        for name in ("a", "b", "c", "d", "e"):
            (root / "docs" / f"{name}.pdf").write_bytes(name.encode())
        (root / "proj" / "app").mkdir(parents=True)
        (root / "proj" / "app" / "main.py").write_bytes(b"print()")
        (root / "proj" / "app" / "util.js").write_bytes(b"//")
        (root / "proj" / "app" / "data.json").write_bytes(b"{}")
        (root / "proj" / "notes.pdf").write_bytes(b"pdf")
        (root / "proj2").mkdir(parents=True)
        (root / "proj2" / "only.py").write_bytes(b"x")
        (root / "proj2" / "notes.pdf").write_bytes(b"pdf")
        (root / "proj2" / "vendor").mkdir()
        (root / "proj2" / "vendor" / "lib.php").write_bytes(b"x")
        return root

    def test_picks_shallowest_code_heavy_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.build_tree(tmp)
            stats, children = zipper.subtree_stats(root)
            units = zipper.pick_units(stats, children, root, min_code=2, min_ratio=0.5)
            self.assertEqual(units, [root / "proj"])

    def test_vendor_is_not_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.build_tree(tmp)
            stats, _children = zipper.subtree_stats(root)
            code, _docs, files, _size = stats[str(root / "proj2")]
            self.assertEqual(code, 1)
            self.assertEqual(files, 2)

    def test_doc_heavy_dirs_are_not_units(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.build_tree(tmp)
            stats, children = zipper.subtree_stats(root)
            units = zipper.pick_units(stats, children, root, min_code=1, min_ratio=0.9)
            self.assertEqual(units, [root / "proj" / "app"])

    def test_too_many_documents_splits_the_unit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "archive"
            proj = root / "proj"
            proj.mkdir(parents=True)
            for name in ("a.pdf", "b.pdf", "c.pdf"):
                (proj / name).write_bytes(b"pdf")
            for i in range(6):
                (proj / f"code{i}.py").write_bytes(b"x")
            (proj / "app").mkdir()
            (proj / "app" / "main.py").write_bytes(b"x")
            (proj / "app" / "util.py").write_bytes(b"x")

            stats, children = zipper.subtree_stats(root)
            units = zipper.pick_units(stats, children, root, min_code=2, min_ratio=0.6, max_docs=2)
            self.assertEqual(units, [proj / "app"])


class ZipTests(unittest.TestCase):
    def test_zip_and_verify(self):
        with tempfile.TemporaryDirectory() as tmp:
            unit = Path(tmp) / "project"
            (unit / "src").mkdir(parents=True)
            (unit / "src" / "main.py").write_bytes(b"print('hello')")
            (unit / "readme.md").write_bytes(b"# readme")
            zip_path = Path(tmp) / "project.zip"

            count = zipper.zip_unit(unit, zip_path)
            self.assertEqual(count, 2)
            ok, message = zipper.verify_zip(zip_path, 2)
            self.assertTrue(ok, message)
            with zipfile.ZipFile(zip_path) as archive:
                self.assertEqual(sorted(archive.namelist()), ["project/readme.md", "project/src/main.py"])
                self.assertEqual(archive.read("project/src/main.py"), b"print('hello')")

    def test_verify_detects_wrong_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            unit = Path(tmp) / "project"
            unit.mkdir()
            (unit / "a.py").write_bytes(b"x")
            zip_path = Path(tmp) / "project.zip"
            zipper.zip_unit(unit, zip_path)
            ok, message = zipper.verify_zip(zip_path, 2)
            self.assertFalse(ok)
            self.assertIn("1 entries", message)


class ApplyTests(unittest.TestCase):
    def test_apply_zips_quarantines_and_writes_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "archive"
            unit = root / "proj"
            unit.mkdir(parents=True)
            (unit / "main.py").write_bytes(b"print()")
            (unit / "lib.php").write_bytes(b"<?php")
            trash = Path(tmp) / "trash"
            manifest = Path(tmp) / "manifest.csv"

            zipper.apply_units(root, trash, [unit], manifest)

            self.assertTrue((unit.with_name("proj.zip")).exists())
            self.assertFalse(unit.exists())
            self.assertTrue((trash / "proj" / "main.py").exists())
            ok, message = zipper.verify_zip(unit.with_name("proj.zip"), 2)
            self.assertTrue(ok, message)

            rows = list(__import__("csv").DictReader(open(manifest, encoding="utf-8")))
            self.assertEqual(rows[0]["status"], "ok")
            self.assertEqual(rows[0]["unit"], "proj")
            self.assertEqual(rows[0]["files"], "2")
            self.assertTrue(rows[0]["sha256"])


if __name__ == "__main__":
    unittest.main()
