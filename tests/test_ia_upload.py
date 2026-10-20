import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import ia_upload


def make_item(identifier, count, size=1000, base="", files=None, local_root=None):
    item = {
        "identifier": identifier,
        "count": count,
        "bytes": size,
        "base": base,
        "files": files if files is not None else [f"{identifier}/f{i}.pdf" for i in range(count)],
        "metadata": {"mediatype": "texts"},
    }
    if local_root is not None:
        item["local_root"] = local_root
    return item


def build_root(tmp, item):
    root = Path(tmp) / "archive"
    for rel in item["files"]:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4")
    return root


class BatchTests(unittest.TestCase):
    def test_batch_respects_file_cap(self):
        plan = {"items": [make_item("a", 3), make_item("b", 4), make_item("c", 5)]}
        batch, total = ia_upload.select_batch(plan, {"items": {}}, max_files=5)
        self.assertEqual([i["identifier"] for i in batch], ["a"])
        self.assertEqual(total, 3)

    def test_done_items_are_skipped(self):
        plan = {"items": [make_item("a", 3), make_item("b", 4)]}
        state = {"items": {"a": {"status": "done"}}}
        batch, total = ia_upload.select_batch(plan, state, max_files=10)
        self.assertEqual([i["identifier"] for i in batch], ["b"])
        self.assertEqual(total, 4)

    def test_only_filter(self):
        plan = {"items": [make_item("a", 3), make_item("b", 4)]}
        batch, _total = ia_upload.select_batch(plan, {"items": {}}, max_files=10, only=["b"])
        self.assertEqual([i["identifier"] for i in batch], ["b"])

    def test_force_includes_done_item(self):
        plan = {"items": [make_item("a", 3), make_item("b", 4)]}
        state = {"items": {"a": {"status": "done", "remaining": 0}}}
        batch, total = ia_upload.select_batch(plan, state, max_files=10, only=["a"], force=True)
        self.assertEqual([i["identifier"] for i in batch], ["a"])
        self.assertEqual(total, 3)
        self.assertEqual(ia_upload.remaining_count(plan["items"][0], state, force=True), 3)

    def test_remaining_count_overrides_plan_count(self):
        plan = {"items": [make_item("a", 10), make_item("b", 4)]}
        state = {"items": {"a": {"status": "partial", "remaining": 2}}}
        batch, total = ia_upload.select_batch(plan, state, max_files=10)
        self.assertEqual([i["identifier"] for i in batch], ["a", "b"])
        self.assertEqual(total, 6)


class StateTests(unittest.TestCase):
    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            self.assertEqual(ia_upload.load_json(path, {"items": {}}), {"items": {}})
            ia_upload.save_json(path, {"items": {"a": {"status": "done"}}})
            self.assertEqual(ia_upload.load_json(path, {}), {"items": {"a": {"status": "done"}}})


class UploadItemTests(unittest.TestCase):
    def test_upload_maps_remote_to_local(self):
        item = make_item("a", 2)
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(tmp, item)
            with mock.patch.object(ia_upload, "upload") as upload, \
                    mock.patch.object(ia_upload, "get_item") as get_item:
                result = ia_upload.upload_item(root, item, retries=3, derive=True)
            self.assertEqual(len(result["files"]), 2)
            self.assertEqual(result["bad_files"], [])
            self.assertEqual(result["uploaded"], 2)
            kwargs = upload.call_args.kwargs
            self.assertEqual(kwargs["metadata"], item["metadata"])
            self.assertTrue(kwargs["checksum"])
            self.assertFalse(kwargs["queue_derive"])
            for rel, local in kwargs["files"].items():
                self.assertEqual(local, str(root / rel))
            get_item.assert_called_once_with("a")
            get_item.return_value.derive.assert_called_once_with(reduced_priority=True)

    def test_category_prefix_is_stripped_from_remote_names(self):
        item = make_item("a", 1, base="1BAC-MATH", files=["1BAC-MATH/COURS/x.pdf"])
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(tmp, item)
            with mock.patch.object(ia_upload, "upload") as upload:
                result = ia_upload.upload_item(root, item, retries=1, derive=False)
            files = upload.call_args.kwargs["files"]
            self.assertEqual(list(files), ["COURS/x.pdf"])
            self.assertEqual(files["COURS/x.pdf"], str(root / "1BAC-MATH/COURS/x.pdf"))
            self.assertEqual(result["uploaded"], 1)

    def test_local_root_override_for_index_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "archive"
            root.mkdir()
            (root / "ia").mkdir()
            (root / "ia" / "index.html").write_text("<html></html>")
            item = make_item("archive", 1, files=["index.html"], local_root=str(root / "ia"))
            with mock.patch.object(ia_upload, "upload") as upload:
                ia_upload.upload_item(root, item, retries=1, derive=False)
            files = upload.call_args.kwargs["files"]
            self.assertEqual(files, {"index.html": str(root / "ia" / "index.html")})

    def test_dry_run_does_not_upload(self):
        item = make_item("a", 1)
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(tmp, item)
            with mock.patch.object(ia_upload, "upload") as upload:
                result = ia_upload.upload_item(root, item, retries=1, derive=True, dry_run=True)
            upload.assert_not_called()
            self.assertEqual(len(result["files"]), 1)

    def test_missing_file_raises(self):
        item = make_item("a", 2)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "archive"
            root.mkdir()
            with self.assertRaises(FileNotFoundError):
                ia_upload.upload_item(root, item, retries=1, derive=False)

    def test_rejected_pdf_is_isolated(self):
        item = make_item("a", 2, base="a", files=["a/bad.pdf", "a/good.pdf"])

        def fake_upload(identifier, files, **kwargs):
            if "bad.pdf" in files:
                raise Exception(
                    " error uploading bad.pdf to a, Uploaded content is unacceptable. "
                    "- Syntax error detected in pdf data."
                )
            return []

        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(tmp, item)
            with mock.patch.object(ia_upload, "upload", side_effect=fake_upload) as upload:
                result = ia_upload.upload_item(root, item, retries=1, derive=False)

        self.assertEqual(result["bad_files"], ["bad.pdf"])
        self.assertEqual(result["uploaded"], 1)
        self.assertEqual(upload.call_count, 2)

    def test_throttle_stops_after_retry_budget(self):
        item = make_item("a", 1)

        def fake_upload(identifier, files, **kwargs):
            raise Exception("Please reduce your request rate. - bucket_tasks_queued exceeds rationed amount")

        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(tmp, item)
            with mock.patch.object(ia_upload, "upload", side_effect=fake_upload), \
                    mock.patch.object(ia_upload.time, "sleep") as sleep:
                with self.assertRaises(ia_upload.Throttled):
                    ia_upload.upload_item(
                        root, item, retries=1, derive=False,
                        throttle_retries=1, throttle_sleep=0,
                    )
        self.assertEqual(sleep.call_count, 1)


class RefreshTests(unittest.TestCase):
    def fake_item(self, names, size=8):
        return SimpleNamespace(files=[{"name": name, "size": size} for name in names])

    def test_refresh_marks_complete_item_done(self):
        item = make_item("a", 2, base="a", files=["a/f0.pdf", "a/f1.pdf"])
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(tmp, item)
            state = {"items": {"a": {"status": "failed", "error": "old error"}}}
            with mock.patch.object(ia_upload, "get_item", return_value=self.fake_item(["f0.pdf", "f1.pdf"])):
                ia_upload.refresh_state({"items": [item]}, state, root)
        self.assertEqual(state["items"]["a"]["status"], "done")
        self.assertEqual(state["items"]["a"]["remaining"], 0)
        self.assertNotIn("error", state["items"]["a"])

    def test_refresh_clears_stale_error_on_done_item(self):
        item = make_item("a", 1, base="a", files=["a/f0.pdf"])
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(tmp, item)
            state = {"items": {"a": {"status": "done", "error": "old", "remaining": 0}}}
            with mock.patch.object(ia_upload, "get_item") as get_item:
                ia_upload.refresh_state({"items": [item]}, state, root)
        get_item.assert_not_called()
        self.assertNotIn("error", state["items"]["a"])
        self.assertEqual(state["items"]["a"]["remaining"], 0)

    def test_refresh_counts_missing_files(self):
        item = make_item("a", 2, base="a", files=["a/f0.pdf", "a/f1.pdf"])
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(tmp, item)
            state = {"items": {}}
            with mock.patch.object(ia_upload, "get_item", return_value=self.fake_item(["f0.pdf"])):
                ia_upload.refresh_state({"items": [item]}, state, root)
        self.assertEqual(state["items"]["a"]["remaining"], 1)
        self.assertEqual(state["items"]["a"]["missing"], 1)
        self.assertNotEqual(state["items"]["a"]["status"], "done")

    def test_refresh_all_can_flag_done_item(self):
        item = make_item("a", 2, base="a", files=["a/f0.pdf", "a/f1.pdf"])
        with tempfile.TemporaryDirectory() as tmp:
            root = build_root(tmp, item)
            state = {"items": {"a": {"status": "done", "remaining": 0}}}
            with mock.patch.object(ia_upload, "get_item", return_value=self.fake_item(["f0.pdf"])):
                ia_upload.refresh_state({"items": [item]}, state, root, include_done=True)
        self.assertEqual(state["items"]["a"]["status"], "partial")
        self.assertEqual(state["items"]["a"]["remaining"], 1)


if __name__ == "__main__":
    unittest.main()
