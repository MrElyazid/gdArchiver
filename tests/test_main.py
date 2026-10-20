import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import main
import viewonly

FILE_ID = "0ByqxF-yryhmyc1JfV3JycGdoUTQ"
OTHER_ID = "1tFjA48XJoMnQ7Mkfp-ZU5J-BFqfoPo3M"
FOLDER_ID = "1WQltv-A48XJoMnQ7MkfpZU5JBFqfoP"


class ParseGoogleLinkTests(unittest.TestCase):
    def test_file_path_forms(self):
        forms = [
            f"https://drive.google.com/file/d/{FILE_ID}/view",
            f"https://drive.google.com/file/d/{FILE_ID}/view?usp=sharing",
            f"https://drive.google.com/file/d/{FILE_ID}/view?usp=drive_open",
            f"https://drive.google.com/file/d/{FILE_ID}/view?usp=share_link",
            f"https://drive.google.com/file/d/{FILE_ID}",
            f"https://drive.google.com/file/d/{FILE_ID}/",
            f"https://drive.google.com/file/d/{FILE_ID}/edit",
            f"https://drive.google.com/file/d/{FILE_ID}/preview",
            f"https://drive.google.com/file/u/0/d/{FILE_ID}/view",
            f"https://drive.google.com/drive/u/0/file/d/{FILE_ID}/view",
            f"http://www.drive.google.com/file/d/{FILE_ID}/view",
        ]
        for url in forms:
            with self.subTest(url=url):
                self.assertEqual(main.parse_google_link(url), ("file", FILE_ID))

    def test_docs_path_forms(self):
        forms = [
            f"https://docs.google.com/document/d/{FILE_ID}/edit?usp=sharing",
            f"https://docs.google.com/document/u/0/d/{FILE_ID}/view",
            f"https://docs.google.com/spreadsheets/d/{FILE_ID}/edit",
            f"https://docs.google.com/presentation/d/{FILE_ID}/view",
            f"https://docs.google.com/presentation/d/{FILE_ID}/htmlview",
        ]
        for url in forms:
            with self.subTest(url=url):
                self.assertEqual(main.parse_google_link(url), ("file", FILE_ID))

    def test_query_id_forms(self):
        forms = [
            f"https://drive.google.com/open?id={OTHER_ID}",
            f"https://drive.google.com/open?id={OTHER_ID}&usp=sharing",
            f"https://drive.google.com/uc?export=download&id={OTHER_ID}",
            f"https://drive.google.com/uc?id={OTHER_ID}",
            f"https://drive.google.com/uc?export=download&amp;id={OTHER_ID}",
            f"https://drive.google.com/open?id=1WQltv-https://drive.google.com/open?id={OTHER_ID}",
        ]
        for url in forms:
            with self.subTest(url=url):
                self.assertEqual(main.parse_google_link(url), ("file", OTHER_ID))

    def test_folder_forms(self):
        forms = [
            f"https://drive.google.com/drive/folders/{FOLDER_ID}",
            f"https://drive.google.com/drive/folders/{FOLDER_ID}?usp=sharing",
            f"https://drive.google.com/drive/u/0/folders/{FOLDER_ID}",
        ]
        for url in forms:
            with self.subTest(url=url):
                self.assertEqual(main.parse_google_link(url), ("folder", FOLDER_ID))

    def test_non_google_links(self):
        for url in [
            "https://example.com/file/d/0ByqxF-yryhmyc1JfV3JycGdoUTQ/view",
            "https://drive.google.com/",
            "https://drive.google.com/drive/my-drive",
            "https://docs.google.com/spreadsheets/u/0/",
            "mailto:someone@example.com",
            "not a url",
        ]:
            with self.subTest(url=url):
                self.assertEqual(main.parse_google_link(url), (None, None))

    def test_glued_ids_from_broken_html_are_rejected(self):
        glued = f"{OTHER_ID}{FILE_ID}"
        self.assertGreater(len(glued), 50)
        self.assertEqual(main.parse_google_link(f"https://drive.google.com/open?id={glued}"), (None, None))
        self.assertEqual(main.parse_google_link(f"https://drive.google.com/file/d/{glued}/view"), (None, None))


class CanonicalUrlTests(unittest.TestCase):
    def test_scheme_and_www_are_the_same_page(self):
        self.assertEqual(
            main.canonical_url("http://pc1.ma/x.html"),
            main.canonical_url("https://www.pc1.ma/x.html"),
        )

    def test_fragment_is_dropped(self):
        self.assertEqual(
            main.canonical_url("https://pc1.ma/x.html#section"),
            main.canonical_url("https://pc1.ma/x.html"),
        )

    def test_percent_encoding_is_normalized(self):
        self.assertEqual(
            main.canonical_url("https://pc1.ma/3eme%20annee.html"),
            main.canonical_url("https://pc1.ma/3eme annee.html"),
        )

    def test_forum_identity_keeps_topic_and_pagination(self):
        base = main.canonical_url("https://pc1.ma/forum/viewtopic.php?f=9&t=1101")
        self.assertEqual(
            base,
            main.canonical_url("https://pc1.ma/forum/viewtopic.php?f=9&t=1101&sid=abc123"),
        )
        self.assertNotEqual(base, main.canonical_url("https://pc1.ma/forum/viewtopic.php?f=9&t=1102"))
        self.assertNotEqual(base, main.canonical_url("https://pc1.ma/forum/viewtopic.php?f=9&t=1101&start=20"))

    def test_query_order_does_not_matter(self):
        self.assertEqual(
            main.canonical_url("https://pc1.ma/forum/viewtopic.php?t=1&f=9"),
            main.canonical_url("https://pc1.ma/forum/viewtopic.php?f=9&t=1"),
        )

    def test_generic_pagination_and_id_params_are_kept(self):
        self.assertNotEqual(
            main.canonical_url("https://example.com/list?page=1"),
            main.canonical_url("https://example.com/list?page=2"),
        )
        self.assertNotEqual(
            main.canonical_url("https://example.com/?id=5"),
            main.canonical_url("https://example.com/?id=6"),
        )

    def test_tracking_and_session_params_are_dropped(self):
        base = main.canonical_url("https://example.com/post")
        for noisy in ["?sid=abc", "?utm_source=news", "?utm_campaign=x", "?fbclid=x",
                      "?PHPSESSID=z", "?s=search", "?mark=read"]:
            with self.subTest(noisy=noisy):
                self.assertEqual(main.canonical_url("https://example.com/post" + noisy), base)

    def test_view_print_is_dropped(self):
        self.assertEqual(
            main.canonical_url("https://example.com/topic?t=1&view=print"),
            main.canonical_url("https://example.com/topic?t=1"),
        )

    def test_drop_and_keep_overrides(self):
        self.assertEqual(
            main.canonical_url("https://example.com/list?page=2", drop_params=["page"]),
            main.canonical_url("https://example.com/list"),
        )
        self.assertNotEqual(
            main.canonical_url("https://example.com/post?sid=x", keep_params=["sid"]),
            main.canonical_url("https://example.com/post"),
        )

    def test_post_permalink_collapses_into_its_topic(self):
        self.assertEqual(
            main.canonical_url("https://pc1.ma/forum/viewtopic.php?f=9&t=1101&p=2000#p2000"),
            main.canonical_url("https://pc1.ma/forum/viewtopic.php?f=9&t=1101"),
        )
        self.assertNotEqual(
            main.canonical_url("https://pc1.ma/forum/viewtopic.php?p=1032"),
            main.canonical_url("https://pc1.ma/forum/viewtopic.php?p=1033"),
        )


class PageDirTests(unittest.TestCase):
    def test_root_goes_to_output_root(self):
        self.assertEqual(main.page_dir("https://pc1.ma/"), "")

    def test_flat_and_nested_pages(self):
        self.assertEqual(main.page_dir("http://pc1.ma/1inter.html"), "1inter")
        self.assertEqual(main.page_dir("https://www.pc1.ma/prof/exprof1.html"), "prof/exprof1")
        self.assertEqual(main.page_dir("http://pc1.ma/Qoraan.htm"), "Qoraan")
        self.assertEqual(main.page_dir("http://pc1.ma/3eme annee.html"), "3eme annee")

    def test_forum_pages(self):
        self.assertEqual(
            main.page_dir("http://pc1.ma/forum/viewtopic.php?f=9&t=1101&sid=abc"),
            "forum/topic-1101",
        )
        self.assertEqual(main.page_dir("http://pc1.ma/forum/viewforum.php?f=9"), "forum/board-9")
        self.assertEqual(main.page_dir("http://pc1.ma/forum/index.php"), "forum")

    def test_query_only_pages_get_a_slug(self):
        self.assertEqual(main.page_dir("https://example.com/?p=123"), "p-123")
        self.assertEqual(main.page_dir("https://example.com/index.php?page=2"), "page-2")
        self.assertEqual(main.page_dir("https://example.com/list.php?id=9"), "list")
        self.assertEqual(main.page_dir("https://example.com/"), "")


class ShouldSkipTests(unittest.TestCase):
    def test_navigation_and_assets_are_skipped(self):
        skipped = [
            "http://pc1.ma/forum/ucp.php?mode=login",
            "http://pc1.ma/forum/memberlist.php",
            "http://pc1.ma/forum/app.php/flags/133",
            "http://pc1.ma/forum/viewtopic.php?f=9&t=1&view=print",
            "http://pc1.ma/forum/viewtopic.php?p=1032#p1032",
            "http://pc1.ma/flags/133/2",
            "http://pc1.ma/2/cont11.pdf",
            "http://pc1.ma/files/archive.rar",
            "http://pc1.ma/flash TC/NatureCourant.exe",
            "http://pc1.ma/cours/migration_des_ions.pps",
        ]
        for url in skipped:
            with self.subTest(url=url):
                self.assertTrue(main.should_skip(url))

    def test_content_pages_are_not_skipped(self):
        for url in [
            "http://pc1.ma/1inter.html",
            "http://pc1.ma/prof/exprof1.html",
            "http://pc1.ma/forum/viewtopic.php?f=9&t=1101",
            "http://pc1.ma/forum/viewforum.php?f=9",
        ]:
            with self.subTest(url=url):
                self.assertFalse(main.should_skip(url))

    def test_forum_toggle(self):
        self.assertTrue(main.should_skip("http://pc1.ma/forum/viewforum.php?f=9", crawl_forum=False))
        self.assertFalse(main.should_skip("http://pc1.ma/1inter.html", crawl_forum=False))

    def test_exclude_patterns(self):
        self.assertTrue(main.should_skip("http://pc1.ma/blog/post.html", exclude_patterns=[r"/blog/"]))
        self.assertFalse(main.should_skip("http://pc1.ma/1inter.html", exclude_patterns=[r"/blog/"]))


class FakeResponse:
    def __init__(self, content):
        self.content = content
        self.text = content.decode("utf-8")


PAGES = {
    "https://pc1.ma/": b"""<html><body>
        <a href="/1inter.html">1 inter</a>
        <a href="/forum/ucp.php">login</a>
        <a href="/2/cont11.pdf">pdf</a>
        <a href="https://drive.google.com/open?id=0ByqxF-yryhmyc1JfV3JycGdoUTQ">home file</a>
    </body></html>""",
    "https://pc1.ma/1inter.html": b"""<html><body>
        <a href="/1inter.html">self</a>
        <a href="https://drive.google.com/file/d/1tFjA48XJoMnQ7Mkfp-ZU5J-BFqfoPo3M/view?usp=sharing">page file</a>
        <a href="https://drive.google.com/open?id=1tFjA48XJoMnQ7Mkfp-ZU5J-BFqfoPo3M">same file, other form</a>
        <a href="https://drive.google.com/drive/folders/1WQltv-A48XJoMnQ7MkfpZU5JBFqfoP">folder</a>
    </body></html>""",
}


class CrawlTests(unittest.TestCase):
    def crawl(self, start="https://pc1.ma/", pages=None, **kwargs):
        pages = pages if pages is not None else PAGES
        seen, glinks, failed = set(), {}, []
        fetched = []

        def fake_fetch(url, delay=0.3, user_agent=None):
            fetched.append(url)
            content = pages.get(url)
            if content is None:
                return None, True
            return FakeResponse(content), False

        with mock.patch.object(main, "fetch_page", side_effect=fake_fetch):
            main.crawl_site(start, start, None, seen, glinks, failed, **kwargs)
        return seen, glinks, failed, fetched

    def test_links_are_deduped_by_file_id(self):
        seen, glinks, failed, fetched = self.crawl()
        self.assertEqual(failed, [])
        self.assertEqual(len(glinks), 3)
        self.assertEqual(glinks["1tFjA48XJoMnQ7Mkfp-ZU5J-BFqfoPo3M"]["href_abs"],
                         "https://drive.google.com/file/d/1tFjA48XJoMnQ7Mkfp-ZU5J-BFqfoPo3M/view")

    def test_crawl_path_is_the_page_folder(self):
        _, glinks, _, _ = self.crawl()
        self.assertEqual(glinks["0ByqxF-yryhmyc1JfV3JycGdoUTQ"]["crawl_path"], "")
        self.assertEqual(glinks["1tFjA48XJoMnQ7Mkfp-ZU5J-BFqfoPo3M"]["crawl_path"], "1inter")
        self.assertEqual(glinks["1WQltv-A48XJoMnQ7MkfpZU5JBFqfoP"]["type"], "folder")
        self.assertEqual(glinks["1WQltv-A48XJoMnQ7MkfpZU5JBFqfoP"]["crawl_path"], "1inter")

    def test_assets_and_nav_pages_are_not_fetched(self):
        _, _, _, fetched = self.crawl()
        self.assertEqual(sorted(fetched), ["https://pc1.ma/", "https://pc1.ma/1inter.html"])

    def test_failed_pages_are_reported_with_depth(self):
        pages = dict(PAGES)
        pages["https://pc1.ma/broken.html"] = None
        pages["https://pc1.ma/"] = b"""<html><body><a href="/broken.html">broken</a></body></html>"""
        seen, glinks, failed, _ = self.crawl(pages=pages)
        self.assertEqual(failed, [("https://pc1.ma/broken.html", 2)])
        self.assertNotIn("pc1.ma/broken.html", seen)

    def test_forum_can_be_disabled(self):
        pages = dict(PAGES)
        pages["https://pc1.ma/"] = b"""<html><body><a href="/forum/viewforum.php?f=9">forum</a></body></html>"""
        _, _, _, fetched = self.crawl(pages=pages, crawl_forum=False)
        self.assertEqual(fetched, ["https://pc1.ma/"])

    def test_thread_replies_are_crawled_with_page_dir(self):
        pages = {
            "https://pc1.ma/forum/viewtopic.php?f=9&t=1101": b"""<html><body>
                <a href="https://drive.google.com/open?id=0ByqxF-yryhmyc1JfV3JycGdoUTQ">f</a>
            </body></html>""",
        }
        _, glinks, failed, _ = self.crawl(start="https://pc1.ma/forum/viewtopic.php?f=9&t=1101", pages=pages)
        self.assertEqual(failed, [])
        row = glinks["0ByqxF-yryhmyc1JfV3JycGdoUTQ"]
        self.assertEqual(row["crawl_path"], "forum/topic-1101")
        self.assertEqual(row["source_page"], "https://pc1.ma/forum/viewtopic.php?f=9&t=1101")

    def test_deep_link_chains_do_not_hit_recursion_limit(self):
        chain = 1500
        pages = {
            f"https://pc1.ma/p{i}.html": f'<html><body><a href="/p{i + 1}.html">next</a></body></html>'.encode()
            for i in range(chain)
        }
        pages[f"https://pc1.ma/p{chain}.html"] = b"<html><body>end</body></html>"
        seen, _, failed, _ = self.crawl(start="https://pc1.ma/p0.html", pages=pages)
        self.assertEqual(failed, [])
        self.assertEqual(len(seen), chain + 1)


class WriteCsvTests(unittest.TestCase):
    def test_rows_are_written_in_stable_order(self):
        data = {
            "b": {"file_id": "b", "href_abs": "u", "type": "file", "source_page": "s",
                  "crawl_path": "z", "text": "t"},
            "a": {"file_id": "a", "href_abs": "u", "type": "file", "source_page": "s",
                  "crawl_path": "", "text": "t"},
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.csv"
            main.write_csv(path, data)
            with open(path, encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        self.assertEqual([r["file_id"] for r in rows], ["a", "b"])
        self.assertEqual(rows[0]["href_abs"], "u")


class ManifestTests(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = main.Manifest(tmp)
            manifest.update("id1", status="ok", name="a.pdf", kind="file",
                            path=f"{tmp}/a.pdf", bytes=12, source_page="s")
            manifest.save()

            reloaded = main.Manifest(tmp)
            row = reloaded.get("id1")
            self.assertEqual(row["status"], "ok")
            self.assertEqual(row["name"], "a.pdf")
            self.assertEqual(row["bytes"], "12")
            self.assertIsNone(reloaded.get("missing"))

    def test_missing_manifest_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(main.Manifest(tmp).get("x"))

    def test_update_flushes_periodically(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = main.Manifest(tmp)
            for i in range(25):
                manifest.update(f"id{i}", status="failed", name=f"{i}.pdf")
            self.assertTrue((Path(tmp) / "manifest.csv").exists())

    def test_legacy_skipped_status_is_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.csv"
            path.write_text(
                "file_id,status,name,kind,path,bytes,source_page\n"
                "id1,skipped-nonpdf,cours.docx,file,,,s\n",
                encoding="utf-8",
            )
            manifest = main.Manifest(tmp)
            self.assertEqual(manifest.get("id1")["status"], "skipped-type")


class TypeAllowedTests(unittest.TestCase):
    def test_default_allows_everything(self):
        options = main.DownloadOptions()
        for name in ["a.pdf", "b.docx", "c.rar", "noext"]:
            with self.subTest(name=name):
                self.assertTrue(main.type_allowed(name, options))

    def test_pdf_only(self):
        options = main.DownloadOptions(pdf_only=True)
        self.assertTrue(main.type_allowed("a.pdf", options))
        self.assertFalse(main.type_allowed("b.docx", options))

    def test_only_ext(self):
        options = main.DownloadOptions(only_ext={".pdf", ".docx"})
        self.assertTrue(main.type_allowed("a.pdf", options))
        self.assertTrue(main.type_allowed("B.DOCX", options))
        self.assertFalse(main.type_allowed("c.rar", options))
        self.assertFalse(main.type_allowed("noext", options))

    def test_exclude_ext(self):
        options = main.DownloadOptions(exclude_ext={".rar", ".exe"})
        self.assertFalse(main.type_allowed("a.rar", options))
        self.assertTrue(main.type_allowed("a.pdf", options))


class ProcessFileTests(unittest.TestCase):
    def test_downloads_named_file(self):
        def fake_download(url=None, output=None, **kwargs):
            Path(output).write_bytes(b"PK fake")
            return output

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(main.gdown, "download", side_effect=fake_download):
                status, path = main.process_file("u", "id1", tmp, "cours.docx", {})
            self.assertEqual(status, "ok")
            self.assertEqual(Path(path).name, "cours.docx")
            self.assertEqual(Path(path).read_bytes(), b"PK fake")

    def test_google_native_file_uses_the_exported_name(self):
        def fake_download(url=None, output=None, **kwargs):
            export = Path(output) / "Document export.docx"
            export.write_bytes(b"PK exported")
            return str(export)

        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(main.gdown, "download", side_effect=fake_download):
                status, path = main.process_file("u", "id1", tmp, "Document export", {})
            self.assertEqual(status, "ok")
            self.assertEqual(Path(path).name, "Document export.docx")

    def test_failed_download_returns_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(main.gdown, "download", side_effect=Exception("boom")):
                status, path = main.process_file("u", "id1", tmp, "cours.docx", {})
        self.assertEqual(status, "failed")
        self.assertIsNone(path)


class UniqueTargetExtensionTests(unittest.TestCase):
    def test_suffix_keeps_extension(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = main.unique_target(tmp, "same.docx", "id1", {})
            second = main.unique_target(tmp, "same.docx", "id2", {first: "id1"})
        self.assertEqual(first.name, "same.docx")
        self.assertEqual(second.name, "same (id2).docx")

    def test_unique_target_suffixes_other_file_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = main.unique_target(tmp, "same.pdf", "id1", {})
            second = main.unique_target(tmp, "same.pdf", "id2", {first: "id1"})
        self.assertEqual(first.name, "same.pdf")
        self.assertEqual(second.name, "same (id2).pdf")

    def test_unique_target_replaces_invalid_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            stale = Path(tmp) / "same.pdf"
            stale.write_bytes(b"<html>not a pdf</html>")
            target = main.unique_target(tmp, "same.pdf", "id1", {})
        self.assertEqual(target.name, "same (id1).pdf")

    def test_unique_target_keeps_existing_valid_pdf(self):
        with tempfile.TemporaryDirectory() as tmp:
            valid = Path(tmp) / "same.pdf"
            valid.write_bytes(b"%PDF-1.4 content")
            target = main.unique_target(tmp, "same.pdf", "id1", {})
        self.assertEqual(target, valid)

    def test_reuse_index_matches_normalized_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "deep" / "old"
            old.mkdir(parents=True)
            file = old / "cours.pdf"
            file.write_bytes(b"%PDF-1.4")
            index = main.build_reuse_index(tmp)
            self.assertEqual(main.take_reused_file("cours.pdf", index), file)
            self.assertIsNone(main.take_reused_file("cours.pdf", index))
            self.assertIsNone(main.take_reused_file("other.pdf", index))

    def test_prune_empty_dirs(self):
        with tempfile.TemporaryDirectory() as tmp:
            keep = Path(tmp) / "keep"
            keep.mkdir()
            (keep / "f.txt").write_text("x")
            (Path(tmp) / "a" / "b" / "c").mkdir(parents=True)
            main.prune_empty_dirs(tmp)
            self.assertTrue(keep.exists())
            self.assertFalse((Path(tmp) / "a").exists())


class TitleParsingTests(unittest.TestCase):
    def test_normal_title(self):
        self.assertEqual(
            viewonly._title_from_html("<title>cours.pdf - Google Drive</title>"),
            "cours.pdf",
        )

    def test_sign_in_titles_are_rejected(self):
        for html in [
            "<title>Google Drive: Sign-in</title>",
            "<title>Sign in - Google Accounts</title>",
            "<title>Google Drive</title>",
        ]:
            with self.subTest(html=html):
                self.assertIsNone(viewonly._title_from_html(html))


class ContentDispositionTests(unittest.TestCase):
    def test_plain_filename(self):
        self.assertEqual(
            viewonly._decode_content_disposition('attachment; filename="cours.pdf"'),
            "cours.pdf",
        )

    def test_utf8_extended_filename(self):
        self.assertEqual(
            viewonly._decode_content_disposition("attachment; filename*=UTF-8''cours%20%C3%A9t%C3%A9.pdf"),
            "cours été.pdf",
        )

    def test_mojibake_filename_is_fixed(self):
        arabic = "حركة دوران"
        header = f'attachment; filename="{arabic.encode("utf-8").decode("latin-1")}.pdf"'
        self.assertEqual(viewonly._decode_content_disposition(header), f"{arabic}.pdf")

    def test_missing_header(self):
        self.assertIsNone(viewonly._decode_content_disposition(""))


class PageImageTests(unittest.TestCase):
    def test_image_ext_detects_known_formats(self):
        cases = [
            (b"\xff\xd8\xff\xe0rest", ".jpg"),
            (b"\x89PNG\r\n\x1a\nrest", ".png"),
            (b"RIFF\x00\x00\x00\x00WEBPVP8 rest", ".webp"),
            (b"\x00\x00\x00\x18ftypavif", ".avif"),
        ]
        for data, ext in cases:
            with self.subTest(ext=ext):
                self.assertEqual(viewonly._image_ext(data), ext)

    def test_image_ext_rejects_error_pages(self):
        for data in (b"", b'<html lang=en>Error 400 (Bad Request)!!1</html>', b"not an image"):
            with self.subTest(data=data):
                self.assertIsNone(viewonly._image_ext(data))

    def test_image_ok_requires_image_type_and_magic(self):
        png = b"\x89PNG\r\n\x1a\nrest"
        self.assertTrue(viewonly._image_ok(png, "image/png"))
        self.assertTrue(viewonly._image_ok(png))
        self.assertFalse(viewonly._image_ok(png, "text/html; charset=utf-8"))
        self.assertFalse(viewonly._image_ok(b"<html>Error 400</html>", "image/png"))

    def test_page_cache_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = viewonly._page_cache_dir(Path(tmp) / "out.pdf", FILE_ID)
            self.assertEqual(cache, Path(tmp) / ".parts" / FILE_ID)
            png = b"\x89PNG\r\n\x1a\n" + b"a" * 4
            webp = b"RIFF\x00\x00\x00\x00WEBP" + b"b" * 4
            viewonly._store_cached_page(cache, 3, 1600, png)
            viewonly._store_cached_page(cache, 4, 2400, webp)
            pages = viewonly._load_cached_pages(cache)
            self.assertEqual(sorted(pages), [3, 4])
            self.assertEqual(pages[3][0], 1600)
            self.assertEqual(pages[4][2], webp)
            # a higher resolution recapture replaces the older file of the same page
            viewonly._store_cached_page(cache, 3, 2400, png)
            self.assertEqual(len(list(cache.glob("page-0003-*"))), 1)
            self.assertEqual(viewonly._load_cached_pages(cache)[3][0], 2400)

    def test_cache_ignores_unfinished_and_foreign_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / ".parts" / FILE_ID
            cache.mkdir(parents=True)
            (cache / "page-0001-1600.png.part").write_bytes(b"\x89PNG\r\n\x1a\nx")
            (cache / "junk.txt").write_bytes(b"x")
            self.assertEqual(viewonly._load_cached_pages(cache), {})


class ViewerUrlTests(unittest.TestCase):
    def test_meta_info_parses_pages_and_width(self):
        self.assertEqual(
            viewonly._meta_info(")]}'\n{\"pages\":25,\"maxPageWidth\":3200}"), (25, 3200)
        )
        self.assertEqual(viewonly._meta_info('{"pages":7}'), (7, 0))
        self.assertEqual(viewonly._meta_info("not json"), (0, 0))

    def test_viewer_page_url_rewrites_page_and_width(self):
        template = "https://drive.google.com/viewer/img?id=a%3Db&page=1&w=800&webp=true"
        url = viewonly._viewer_page_url(template, 12, 3200)
        self.assertIn("page=12", url)
        self.assertIn("w=3200", url)
        self.assertIn("id=a%3Db", url)
        self.assertIn("webp=true", url)

    def test_viewer_page_url_keeps_width_when_not_given(self):
        template = "https://drive.google.com/viewer/img?id=x&page=3&w=800"
        self.assertIn("w=800", viewonly._viewer_page_url(template, 4, 0))


class FolderListingTests(unittest.TestCase):
    def test_classify_splits_pdfs_folders_and_files(self):
        entries = [
            ("a" * 25, "cours.pdf", "application/pdf"),
            ("b" * 25, "dossier", "application/vnd.google-apps.folder"),
            ("c" * 25, "main.php", "text/x-php"),
            ("d" * 25, "notes.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ]
        pdfs, folders, files = viewonly._classify(entries)
        self.assertEqual(list(pdfs.values()), ["cours.pdf"])
        self.assertEqual(list(folders.values()), ["dossier"])
        self.assertEqual(sorted(files.values()), ["main.php", "notes.docx"])

    def test_skip_dir_matches_relative_path(self):
        patterns = [r"(^|/)vendor(/|$)", r"node_modules", r"\.git"]
        self.assertTrue(main._folder_skipped("Symfony/vendor/doctrine", patterns))
        self.assertTrue(main._folder_skipped("app/node_modules/x", patterns))
        self.assertTrue(main._folder_skipped(".git", patterns))
        self.assertFalse(main._folder_skipped("Symfony/src", patterns))
        self.assertFalse(main._folder_skipped("vendors/2020", patterns))
        self.assertFalse(main._folder_skipped("vendorless", patterns))


class ProcessPdfTests(unittest.TestCase):
    def test_missing_parent_directory_is_created(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "sub" / "dir" / "cours.pdf"

            def fake_download(url, output, **kwargs):
                Path(output).write_bytes(b"%PDF-1.4 fake")
                return output

            with mock.patch.object(main.gdown, "download", side_effect=fake_download) as download:
                status = main.process_pdf("https://drive.google.com/file/d/abc/view", "abc", target, None)

            self.assertEqual(status, "ok")
            self.assertTrue(viewonly.valid_pdf(target))
            self.assertTrue(target.parent.is_dir())
            download.assert_called_once()


class FolderWalkTests(unittest.TestCase):
    def test_walk_creates_output_dir_and_skips_matching_subfolders(self):
        top_id = "top" + "x" * 22
        sub_id = "sub" + "x" * 22
        vendor_id = "vendor" + "x" * 19
        calls = []

        def listing(url, capturer):
            calls.append(url)
            if url.endswith(top_id):
                return [], [(vendor_id, "vendor"), (sub_id, "cours")], []
            return [], [], []

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            with mock.patch.object(main, "list_folder_entries", side_effect=listing):
                main.download_folder_pdfs(
                    f"https://drive.google.com/drive/folders/{top_id}", str(out), None,
                    skip_dirs=[r"(^|/)vendor(/|$)"],
                )
            self.assertTrue(out.is_dir())

        called_ids = [u.rsplit("/", 1)[-1] for u in calls]
        self.assertIn(top_id, called_ids)
        self.assertIn(sub_id, called_ids)
        self.assertNotIn(vendor_id, called_ids)


class ResolveNameTests(unittest.TestCase):
    def test_download_header_is_preferred(self):
        with mock.patch.object(main.viewonly, "remote_file_name", return_value="cours.pdf"), \
             mock.patch.object(main.viewonly, "probe_drive") as probe:
            self.assertEqual(main.resolve_name("id1"), ("cours.pdf", "file"))
            probe.assert_not_called()

    def test_title_fallback_detects_folders(self):
        with mock.patch.object(main.viewonly, "remote_file_name", return_value=None), \
             mock.patch.object(main.viewonly, "probe_drive", return_value=("cours", "folder")):
            self.assertEqual(main.resolve_name("id1"), ("cours", "folder"))


class DownloadRowTests(unittest.TestCase):
    def row(self, file_id="id1", name="", kind="file"):
        return {"file_id": file_id, "href_abs": f"https://drive.google.com/file/d/{file_id}/view",
                "type": kind, "crawl_path": "1inter", "source_page": "s", "text": "t"}

    def test_non_pdf_is_skipped_in_pdf_only_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = main.Manifest(tmp)
            manifest.update("id1", name="cours.docx", kind="file")
            with mock.patch.object(main, "process_file") as process:
                status = main.download_row(self.row(), tmp, manifest, None, {}, {}, False,
                                           main.DownloadOptions(pdf_only=True))
            self.assertEqual(status, "skipped-type")
            self.assertEqual(manifest.get("id1")["status"], "skipped-type")
            process.assert_not_called()

    def test_non_pdf_is_downloaded_by_default(self):
        def fake_process(url, file_id, output_dir, name, used_names, user_agent=None):
            target = Path(output_dir) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"PK fake docx")
            return "ok", target

        with tempfile.TemporaryDirectory() as tmp:
            manifest = main.Manifest(tmp)
            manifest.update("id1", name="cours.docx", kind="file")
            with mock.patch.object(main, "process_file", side_effect=fake_process):
                status = main.download_row(self.row(), tmp, manifest, None, {}, {}, False)
            self.assertEqual(status, "ok")
            row = manifest.get("id1")
            self.assertEqual(row["status"], "ok")
            self.assertTrue(row["path"].endswith("cours.docx"))
            self.assertEqual(row["bytes"], str(len(b"PK fake docx")))

    def test_exclude_ext_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = main.Manifest(tmp)
            manifest.update("id1", name="bundle.rar", kind="file")
            with mock.patch.object(main, "process_file") as process:
                status = main.download_row(self.row(), tmp, manifest, None, {}, {}, False,
                                           main.DownloadOptions(exclude_ext={".rar"}))
            self.assertEqual(status, "skipped-type")
            process.assert_not_called()

    def test_folder_listing_failure_is_recorded_as_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = main.Manifest(tmp)
            with mock.patch.object(main, "resolve_name", return_value=("dossier", "folder")), \
                 mock.patch.object(main, "download_folder_pdfs", return_value=False):
                status = main.download_row(self.row(kind="folder"), tmp, manifest, None, {}, {}, False)
            self.assertEqual(status, "failed")
            self.assertEqual(manifest.get("id1")["status"], "failed")

    def test_folder_listing_success_is_recorded_as_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = main.Manifest(tmp)
            with mock.patch.object(main, "resolve_name", return_value=("dossier", "folder")), \
                 mock.patch.object(main, "download_folder_pdfs", return_value=True):
                status = main.download_row(self.row(kind="folder"), tmp, manifest, None, {}, {}, False)
            self.assertEqual(status, "ok")
            row = manifest.get("id1")
            self.assertEqual(row["status"], "ok")
            self.assertTrue(Path(row["path"]).is_dir())

    def test_skip_dirs_are_forwarded_to_folder_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = main.Manifest(tmp)
            with mock.patch.object(main, "resolve_name", return_value=("dossier", "folder")), \
                 mock.patch.object(main, "download_folder_pdfs", return_value=True) as download:
                main.download_row(self.row(kind="folder"), tmp, manifest, None, {}, {}, False,
                                  skip_dirs=["vendor"])
            self.assertEqual(download.call_args.kwargs.get("skip_dirs"), ["vendor"])

    def test_reuse_moves_file_instead_of_downloading(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_tree = Path(tmp) / "old"
            old_tree.mkdir()
            (old_tree / "cours.pdf").write_bytes(b"%PDF-1.4 content")
            new_root = Path(tmp) / "new"
            manifest = main.Manifest(new_root)
            manifest.update("id1", name="cours.pdf", kind="file")
            with mock.patch.object(main, "process_pdf") as process:
                status = main.download_row(self.row(), new_root, manifest, None,
                                           main.build_reuse_index(old_tree), {}, False)
            self.assertEqual(status, "ok")
            self.assertFalse((old_tree / "cours.pdf").exists())
            self.assertTrue((new_root / "1inter" / "cours.pdf").exists())
            process.assert_not_called()

    def test_failed_download_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = main.Manifest(tmp)
            manifest.update("id1", name="cours.pdf", kind="file")
            with mock.patch.object(main, "process_pdf", return_value="failed"):
                status = main.download_row(self.row(), tmp, manifest, None, {}, {}, False)
            self.assertEqual(status, "failed")
            self.assertEqual(manifest.get("id1")["status"], "failed")

    def test_unresolvable_id_is_marked_failed_without_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = main.Manifest(tmp)
            with mock.patch.object(main, "resolve_name", return_value=(None, "file")), \
                 mock.patch.object(main, "process_pdf") as process:
                status = main.download_row(self.row(), tmp, manifest, None, {}, {}, False)
            self.assertEqual(status, "failed")
            process.assert_not_called()

    def test_lazy_name_resolution_for_unknown_files(self):
        def fake_process(url, file_id, target, capturer, user_agent=None):
            Path(target).write_bytes(b"%PDF-1.4 content")
            return "ok"

        with tempfile.TemporaryDirectory() as tmp:
            manifest = main.Manifest(tmp)
            with mock.patch.object(main, "resolve_name", return_value=("cours.pdf", "file")), \
                 mock.patch.object(main, "process_pdf", side_effect=fake_process):
                status = main.download_row(self.row(), tmp, manifest, None, {}, {}, False)
            self.assertEqual(status, "ok")
            self.assertEqual(manifest.get("id1")["name"], "cours.pdf")

    def test_retry_failed_skips_completed_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            valid = Path(tmp) / "1inter"
            valid.mkdir()
            pdf = valid / "cours.pdf"
            pdf.write_bytes(b"%PDF-1.4 content")
            manifest = main.Manifest(tmp)
            manifest.update("id1", status="ok", name="cours.pdf", kind="file", path=str(pdf))
            with mock.patch.object(main, "process_pdf") as process:
                status = main.download_row(self.row(), tmp, manifest, None, {}, {}, True)
            self.assertEqual(status, "skipped")
            process.assert_not_called()


class UserAgentTests(unittest.TestCase):
    def test_default_headers_use_the_chrome_string(self):
        self.assertEqual(main.browser_headers()["User-Agent"], main.DEFAULT_USER_AGENT)

    def test_custom_user_agent_overrides_the_default(self):
        self.assertEqual(main.browser_headers("gdarchiver/0.1")["User-Agent"], "gdarchiver/0.1")

    def test_fetch_page_sends_the_user_agent(self):
        response = mock.MagicMock()
        response.status_code = 200
        with mock.patch.object(main.requests, "get", return_value=response) as get:
            main.fetch_page("https://pc1.ma/", delay=0, user_agent="gdarchiver/0.1")
        self.assertEqual(get.call_args.kwargs["headers"]["User-Agent"], "gdarchiver/0.1")

    def test_viewonly_agent_can_be_overridden(self):
        original = viewonly.UA
        try:
            viewonly.set_user_agent("gdarchiver/0.1")
            self.assertEqual(viewonly.UA, "gdarchiver/0.1")
        finally:
            viewonly.set_user_agent(original)

    def test_gdown_receives_the_user_agent_when_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(main.gdown, "download", return_value=None) as download:
                main.process_pdf("u", "id1", Path(tmp) / "f.pdf", None, user_agent="gdarchiver/0.1")
            self.assertEqual(download.call_args.kwargs["user_agent"], "gdarchiver/0.1")

    def test_gdown_keeps_its_default_agent_when_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(main.gdown, "download", return_value=None) as download:
                main.process_pdf("u", "id1", Path(tmp) / "f.pdf", None)
            self.assertNotIn("user_agent", download.call_args.kwargs)


class TeeTests(unittest.TestCase):
    def test_writes_and_flushes_every_stream(self):
        first, second = io.StringIO(), io.StringIO()
        tee = main.Tee(first, second)
        tee.write("hello")
        tee.flush()
        self.assertEqual(first.getvalue(), "hello")
        self.assertEqual(second.getvalue(), "hello")

    def test_attributes_fall_through_to_the_first_stream(self):
        class WithFileno(io.StringIO):
            def fileno(self):
                return 7

        tee = main.Tee(WithFileno(), io.StringIO())
        self.assertEqual(tee.fileno(), 7)


class ReuseEntityTests(unittest.TestCase):
    def test_reuse_matches_html_entity_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "Fiche 4 _ l&#39;eau (www.pc1.ma).pdf"
            old.write_bytes(b"%PDF-1.4")
            index = main.build_reuse_index(tmp)
            self.assertEqual(main.take_reused_file("Fiche 4 _ l'eau (www.pc1.ma).pdf", index), old)

    def test_reuse_still_rejects_different_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "cours1.pdf").write_bytes(b"%PDF-1.4")
            index = main.build_reuse_index(tmp)
            self.assertIsNone(main.take_reused_file("cours2.pdf", index))


if __name__ == "__main__":
    unittest.main()
