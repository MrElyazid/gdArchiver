import csv
import tempfile
import unittest
from pathlib import Path

import tidy


class CleanNameTests(unittest.TestCase):
    def test_entities_are_decoded(self):
        self.assertEqual(tidy.clean_name("A &amp; B"), "A & B")
        self.assertEqual(tidy.clean_name("A &amp;amp; B"), "A & B")
        self.assertEqual(tidy.clean_name("A &#39; B"), "A ' B")

    def test_invisible_marks_are_stripped(self):
        self.assertEqual(tidy.clean_name("\u202bRTL\u202c file.pdf"), "RTL file.pdf")
        self.assertEqual(tidy.clean_name("zero\u200bwidth"), "zerowidth")

    def test_whitespace_and_trailing_dots(self):
        self.assertEqual(tidy.clean_name("  a   b  .pdf "), "a b.pdf")
        self.assertEqual(tidy.clean_name("name.pdf."), "name.pdf")

    def test_unicode_is_normalized(self):
        decomposed = "e\u0301tude.pdf"
        self.assertEqual(tidy.clean_name(decomposed), "\u00e9tude.pdf")

    def test_overlong_names_are_truncated(self):
        name = "a" * 240 + ".pdf"
        cleaned = tidy.clean_name(name)
        self.assertLessEqual(len(cleaned.encode("utf-8")), tidy.MAX_NAME_BYTES)
        self.assertTrue(cleaned.endswith(".pdf"))

    def test_unusable_names_return_none(self):
        self.assertIsNone(tidy.clean_name("\u202b\u202c"))
        self.assertIsNone(tidy.clean_name("a &sol; b"))
        self.assertIsNone(tidy.clean_name(".."))

    def test_leading_decorations_are_stripped(self):
        self.assertEqual(tidy.clean_name("°ELECTROCINETIQUE"), "ELECTROCINETIQUE")
        self.assertEqual(tidy.clean_name("+++COURS X"), "COURS X")
        self.assertEqual(tidy.clean_name("~notes.txt"), "notes.txt")
        self.assertEqual(tidy.clean_name("-منهجيات.pdf"), "منهجيات.pdf")

    def test_replacement_char_is_removed(self):
        self.assertEqual(tidy.clean_name("corrig principal.pdf"), "corrig principal.pdf")

    def test_cyrillic_mojibake_is_repaired(self):
        self.assertEqual(tidy.clean_name("1BAC-histoire-gтФЬ╨╣ographie"), "1BAC-histoire-géographie")

    def test_plus_mojibake_is_repaired(self):
        self.assertEqual(tidy.clean_name("M+®canique"), "Mécanique")
        self.assertEqual(tidy.clean_name("Page de pr+®sentation.png"), "Page de présentation.png")
        self.assertEqual(tidy.clean_name("2-Mouvement h+®lico+»dale.pdf"), "2-Mouvement hélicoïdale.pdf")
        self.assertEqual(tidy.clean_name("8-Mouvement dÔÇÖun anneau.pdf"), "8-Mouvement d’un anneau.pdf")
        self.assertEqual(
            tidy.clean_name("16-Force centrale en 1sur \u00adØæƒ3.pdf"),
            "16-Force centrale en 1sur r³.pdf",
        )
        self.assertEqual(tidy.clean_name("PrÃ©sentation.pdf"), "Présentation.pdf")
        self.assertEqual(tidy.clean_name("CorrigÃ© TD nÂ° 2.pdf"), "Corrigé TD n° 2.pdf")
        self.assertEqual(tidy.clean_name("CorrigÃ©s.pdf"), "Corrigés.pdf")
        self.assertEqual(
            tidy.clean_name("12-Mode TEM dans un câble coaxial.pdf"),
            "12-Mode TEM dans un câble coaxial.pdf",
        )


class PlanAndApplyTests(unittest.TestCase):
    def build_tree(self, tmp):
        root = Path(tmp) / "archive"
        (root / "A &amp; B").mkdir(parents=True)
        (root / "A &amp; B" / "x .pdf").write_bytes(b"x")
        (root / "A &amp; B" / "inner.txt").write_bytes(b"x")
        (root / "\u202bRTL\u202c.txt").write_bytes(b"x")
        (root / "Cours.pdf").write_bytes(b"old")
        (root / "Cours .pdf").write_bytes(b"new")
        manifest = root / "manifest.csv"
        with open(manifest, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["file_id", "status", "name", "kind", "path", "bytes", "source_page"])
            writer.writerow(["id1", "ok", "x .pdf", "file", str(root / "A &amp; B" / "x .pdf"), "1", "s"])
            writer.writerow(["id2", "ok", "RTL.txt", "file", str(root / "\u202bRTL\u202c.txt"), "1", "s"])
        return root

    def test_plan_renames_and_reports_collisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.build_tree(tmp)
            renames, collisions, _stats = tidy.plan(root)
            self.assertEqual(renames[str(root / "A &amp; B")], str(root / "A & B"))
            self.assertEqual(renames[str(root / "A &amp; B" / "x .pdf")], str(root / "A & B" / "x.pdf"))
            self.assertEqual(renames[str(root / "\u202bRTL\u202c.txt")], str(root / "RTL.txt"))
            # unchanged descendants are carried by their parent, not renamed explicitly
            self.assertNotIn(str(root / "A &amp; B" / "inner.txt"), renames)
            self.assertTrue(any("Cours .pdf" in path for path, _reason in collisions))
            self.assertNotIn(str(root / "Cours .pdf"), renames)

    def test_apply_moves_children_with_parents_and_updates_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.build_tree(tmp)
            renames, _collisions, _stats = tidy.plan(root)
            applied, skipped = tidy.apply_renames(root, renames)
            self.assertEqual(applied, 3)
            self.assertEqual(skipped, 0)
            self.assertTrue((root / "A & B" / "x.pdf").exists())
            self.assertTrue((root / "RTL.txt").exists())
            self.assertTrue((root / "Cours .pdf").exists())
            tidy.update_manifest(root, renames)

            rows = list(csv.DictReader(open(root / "manifest.csv", encoding="utf-8")))
            self.assertEqual(rows[0]["path"], str(root / "A & B" / "x.pdf"))
            self.assertEqual(rows[1]["path"], str(root / "RTL.txt"))

    def test_remap_path_uses_longest_prefix(self):
        renames = {
            "/root/A": "/root/A2",
            "/root/A/B": "/root/A2/B2",
        }
        self.assertEqual(tidy.remap_path("/root/A/B/file.pdf", renames), "/root/A2/B2/file.pdf")
        self.assertEqual(tidy.remap_path("/root/A/other.doc", renames), "/root/A2/other.doc")
        self.assertEqual(tidy.remap_path("/root/C/file.pdf", renames), "/root/C/file.pdf")


if __name__ == "__main__":
    unittest.main()
