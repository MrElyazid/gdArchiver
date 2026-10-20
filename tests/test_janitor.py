import tempfile
import unittest
from pathlib import Path

import janitor


class ScratchNameTests(unittest.TestCase):
    def test_scratch_names(self):
        self.assertTrue(janitor.is_scratch("13129"))
        self.assertTrue(janitor.is_scratch("13129.pdf"))
        self.assertTrue(janitor.is_scratch("123456.jpg"))
        self.assertFalse(janitor.is_scratch("1234567.jpg"))
        self.assertFalse(janitor.is_scratch("123.pdf"))
        self.assertFalse(janitor.is_scratch("13129abc"))
        self.assertFalse(janitor.is_scratch("cours.pdf"))


class KeeperTests(unittest.TestCase):
    def test_prefers_non_scratch_and_shorter_path(self):
        paths = [
            Path("/a/b/deep/13129.pdf"),
            Path("/a/b/cours.pdf"),
            Path("/a/b/.tmp.driveupload/cours.pdf"),
        ]
        self.assertEqual(janitor.pick_keeper(paths), Path("/a/b/cours.pdf"))


class PlanTests(unittest.TestCase):
    def build_tree(self, tmp):
        root = Path(tmp) / "archive"
        (root / "a" / ".tmp.driveupload").mkdir(parents=True)
        (root / "b").mkdir(parents=True)
        data = b"same content"
        (root / "a" / "real.pdf").write_bytes(data)
        (root / "a" / ".tmp.driveupload" / "13129").write_bytes(data)
        (root / "a" / "13129.pdf").write_bytes(data)
        (root / "b" / "copy.pdf").write_bytes(data)
        (root / "b" / "unique.pdf").write_bytes(b"unique")
        return root

    def plan_for(self, root, purge_names=(), **kwargs):
        files, zeros, purge_dirs = janitor.collect(root, purge_names)
        groups = janitor.duplicate_groups(files)
        return janitor.build_plan(root, files, groups, zeros=zeros, purge_dirs=purge_dirs, **kwargs)

    def actions_by_path(self, plan):
        return {entry[2]: entry for entry in plan}

    def test_junk_is_planned_for_trash_and_duplicates_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.build_tree(tmp)
            plan = self.plan_for(root)
            by_path = self.actions_by_path(plan)

            self.assertEqual(by_path[root / "a" / ".tmp.driveupload" / "13129"][:2],
                             ("trash", "drive-upload-tmp"))
            self.assertEqual(by_path[root / "a" / "13129.pdf"][:2],
                             ("trash", "upload-scratch-duplicate"))
            action, reason, path, keeper, size, digest = by_path[root / "b" / "copy.pdf"]
            self.assertEqual((action, reason), ("report", "duplicate"))
            self.assertEqual(keeper, root / "a" / "real.pdf")
            self.assertNotIn(root / "a" / "real.pdf", by_path)

    def test_hardlink_flag_replaces_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.build_tree(tmp)
            plan = self.plan_for(root, hardlink=True)
            action, reason, path, keeper, size, digest = self.actions_by_path(plan)[root / "b" / "copy.pdf"]
            self.assertEqual((action, reason), ("hardlink", "duplicate"))
            self.assertEqual(keeper, root / "a" / "real.pdf")

    def test_trash_duplicates_flag_replaces_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.build_tree(tmp)
            plan = self.plan_for(root, trash_duplicates=True)
            action, reason, path, keeper, size, digest = self.actions_by_path(plan)[root / "b" / "copy.pdf"]
            self.assertEqual((action, reason), ("trash", "duplicate"))

    def test_apply_quarantines_junk_and_keeps_report_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.build_tree(tmp)
            trash = Path(tmp) / "trash"
            plan = self.plan_for(root)
            janitor.apply_actions(root, trash, plan)

            self.assertTrue((trash / "a" / ".tmp.driveupload" / "13129").exists())
            self.assertFalse((root / "a" / ".tmp.driveupload" / "13129").exists())
            self.assertFalse((root / "a" / "13129.pdf").exists())
            self.assertTrue((root / "b" / "copy.pdf").exists())

    def test_scratch_without_twin_is_only_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "archive"
            root.mkdir()
            (root / "13129.pdf").write_bytes(b"lonely")
            plan = self.plan_for(root)
            action, reason, path, keeper, size, digest = plan[0]
            self.assertEqual((action, reason), ("report", "scratch-no-twin"))


class PurgeTests(unittest.TestCase):
    def test_purge_dir_is_excluded_and_quarantined_as_a_whole(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "archive"
            (root / "project" / "vendor").mkdir(parents=True)
            (root / "project" / "vendor" / "lib.php").write_bytes(b"x" * 10)
            (root / "project" / "main.pdf").write_bytes(b"pdf")

            files, zeros, purge_dirs = janitor.collect(root, {"vendor"})
            self.assertEqual([p.name for p, _ in files], ["main.pdf"])
            self.assertEqual(len(purge_dirs), 1)
            self.assertEqual(purge_dirs[0][1], 1)

            plan = janitor.build_plan(root, files, janitor.duplicate_groups(files), purge_dirs=purge_dirs)
            action, reason, path, keeper, size, digest = [a for a in plan if a[0] == "trash-dir"][0]
            self.assertEqual((action, reason), ("trash-dir", "purge-dir"))
            self.assertEqual(path, root / "project" / "vendor")

            trash = Path(tmp) / "trash"
            janitor.apply_actions(root, trash, plan)
            self.assertFalse((root / "project" / "vendor").exists())
            self.assertTrue((trash / "project" / "vendor" / "lib.php").exists())

    def test_zero_byte_files_only_purged_with_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "archive"
            root.mkdir()
            (root / "empty.bin").write_bytes(b"")
            (root / "full.bin").write_bytes(b"data")

            files, zeros, purge_dirs = janitor.collect(root)
            plan = janitor.build_plan(root, files, janitor.duplicate_groups(files), zeros=zeros)
            self.assertFalse(any(a[1] == "zero-byte" for a in plan))

            plan = janitor.build_plan(root, files, janitor.duplicate_groups(files), zeros=zeros, purge_empty=True)
            self.assertTrue(any(a[1] == "zero-byte" for a in plan))

    def test_office_locks_only_purged_with_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "archive"
            root.mkdir()
            (root / "~$lock.docx").write_bytes(b"x")
            (root / "cours.docx").write_bytes(b"data")

            files, zeros, purge_dirs = janitor.collect(root)
            plan = janitor.build_plan(root, files, janitor.duplicate_groups(files))
            self.assertFalse(any(a[1] == "office-lock" for a in plan))

            plan = janitor.build_plan(root, files, janitor.duplicate_groups(files), purge_locks=True)
            locks = [a for a in plan if a[1] == "office-lock"]
            self.assertEqual(len(locks), 1)
            self.assertEqual(locks[0][2], root / "~$lock.docx")


if __name__ == "__main__":
    unittest.main()
