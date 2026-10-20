import json
import tempfile
import unittest
from pathlib import Path

import ia_plan


class SlugTests(unittest.TestCase):
    def test_slugs(self):
        self.assertEqual(ia_plan.slug("2BAC-MATH-SX---PC-ET-SVT"), "2bac-math-sx-pc-et-svt")
        self.assertEqual(ia_plan.slug("Étude Supérieure"), "tude-sup-rieure")
        self.assertEqual(ia_plan.slug("///"), "item")


class SplitTests(unittest.TestCase):
    def test_split_respects_max_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            top = Path(tmp) / "top"
            (top / "d2").mkdir(parents=True)
            for name in ("a.pdf", "b.pdf"):
                (top / name).write_bytes(b"%PDF-1.4")
            for name in ("c.pdf", "d.pdf", "e.pdf"):
                (top / "d2" / name).write_bytes(b"%PDF-1.4")
            cache = {}
            groups = ia_plan.split_tree(top, 2, cache)
            counts = [sum(ia_plan._count(item, cache)[0] for item in group) for group in groups]
            self.assertTrue(all(count <= 2 for count in counts))
            self.assertEqual(sum(counts), 5)


class PlanTests(unittest.TestCase):
    def build_tree(self, tmp):
        root = Path(tmp) / "archive"
        (root / "1BAC-MATH").mkdir(parents=True)
        for name in ("a.pdf", "b.pdf"):
            (root / "1BAC-MATH" / name).write_bytes(b"%PDF-1.4")
        (root / "CPGE-PHYSIQUE").mkdir(parents=True)
        for name in ("c.pdf", "d.pdf", "e.pdf"):
            (root / "CPGE-PHYSIQUE" / name).write_bytes(b"%PDF-1.4")
        (root / "manifest.csv").write_bytes(b"id\n")
        return root

    def test_plan_items_and_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.build_tree(tmp)
            plan = ia_plan.build_plan(root, max_files=2)
            identifiers = [item["identifier"] for item in plan["items"]]
            self.assertEqual(identifiers, [
                "saborpcmath-1bac-math",
                "saborpcmath-cpge-physique-1",
                "saborpcmath-cpge-physique-2",
            ])
            self.assertEqual(plan["items"][0]["count"], 2)
            self.assertEqual(plan["items"][0]["base"], "1BAC-MATH")
            self.assertEqual(sum(i["count"] for i in plan["items"]), 5)
            metadata = plan["items"][0]["metadata"]
            self.assertEqual(metadata["mediatype"], "texts")
            self.assertEqual(metadata["collection"], "opensource")
            self.assertIn("mathematics", metadata["subject"])
            self.assertIn("takedown", metadata["rights"])
            self.assertEqual(metadata["language"], ["ara", "fre"])
            # manifest.csv must not be uploaded
            self.assertFalse(any("manifest.csv" in f for item in plan["items"] for f in item["files"]))

    def test_non_pdf_with_pdf_extension_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "archive"
            (root / "SMI6").mkdir(parents=True)
            (root / "SMI6" / "good.pdf").write_bytes(b"%PDF-1.4")
            (root / "SMI6" / "bad.pdf").write_bytes(b"<html>not a pdf</html>")

            plan = ia_plan.build_plan(root)
            files = [rel for item in plan["items"] for rel in item["files"]]

            self.assertIn("SMI6/good.pdf", files)
            self.assertNotIn("SMI6/bad.pdf", files)
            self.assertEqual(plan["items"][0]["count"], 1)

    def test_identifiers_are_unique(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.build_tree(tmp)
            plan = ia_plan.build_plan(root, max_files=2)
            identifiers = [item["identifier"] for item in plan["items"]]
            self.assertEqual(len(identifiers), len(set(identifiers)))

    def test_index_item_lists_everything(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.build_tree(tmp)
            plan = ia_plan.build_plan(root, max_files=2)
            item, content = ia_plan.make_index_item(plan["items"], "ia")
            self.assertEqual(item["identifier"], "saborpcmath-archive")
            self.assertTrue(item["local_root"].endswith("ia"))
            self.assertEqual(item["files"], ["index.html"])
            self.assertEqual(item["count"], 1)
            self.assertIn("saborpcmath-1bac-math", content)
            self.assertIn("saborpcmath-cpge-physique-1", content)
            self.assertIn("<html", content)

    def test_colliding_slugs_get_unique_identifiers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "archive"
            for name in ("2BAC-MATH-SX---PC-ET-SVT", "2BAC-MATH-SX-PC-ET-SVT"):
                (root / name).mkdir(parents=True)
                (root / name / "a.pdf").write_bytes(b"%PDF-1.4")
            plan = ia_plan.build_plan(root, max_files=10)
            identifiers = [item["identifier"] for item in plan["items"]]
            self.assertEqual(len(identifiers), 2)
            self.assertEqual(len(set(identifiers)), 2)


if __name__ == "__main__":
    unittest.main()
