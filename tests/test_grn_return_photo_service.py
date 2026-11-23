import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from services import grn_return_photo_service as svc
from services.grn_return_photo_service import GrnLineKey, IncomingPhoto

KEY = GrnLineKey(1, 3, "GRN", "GRN/0001", 1, 2)


def _ok(body):
    resp = MagicMock()
    resp.ok = True
    resp.status_code = 200
    resp.json.return_value = body
    return resp


def _fail(code, message):
    resp = MagicMock()
    resp.ok = False
    resp.status_code = code
    resp.json.return_value = {"message": message}
    return resp


class GrnReturnPhotoServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name) / "uploads"
        self.patches = [
            patch.object(svc, "_UPLOAD_ROOT", root),
            patch("services.vendor_document_storage._UPLOAD_ROOT", root),
            patch.object(svc.settings, "internal_api_url", "http://erp:3003"),
        ]
        for p in self.patches:
            p.start()
        self.root = root

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def test_saves_photos_reads_labels_and_posts_to_erp(self):
        post = MagicMock(return_value=_ok({"line": {}, "header": {"returnPhotoCount": 2}}))
        labels = {0: "BATCH B12 / EXP 10-2027", 1: None}
        calls = []

        def reader(path):
            calls.append(path)
            return labels[len(calls) - 1]

        result = svc.add_return_photos(
            key=KEY,
            condition="damaged",
            photos=[IncomingPhoto("a.JPG", b"x"), IncomingPhoto("b.png", b"y")],
            remarks="  box crushed ",
            uploaded_by=84,
            label_reader=reader,
            http_post=post,
        )

        url = post.call_args.args[0]
        self.assertEqual(url, "http://erp:3003/grn-line/return-photos")
        self.assertEqual(post.call_args.kwargs["params"]["grnNumber"], "GRN/0001")
        self.assertEqual(post.call_args.kwargs["params"]["lineNo"], "2")
        sent = post.call_args.kwargs["json"]["photos"]
        self.assertEqual(len(sent), 2)
        self.assertTrue(all(p["condition"] == "DAMAGED" for p in sent))
        self.assertEqual(sorted(p["labelText"] or "" for p in sent), ["", "BATCH B12 / EXP 10-2027"])
        self.assertEqual(sent[0]["remarks"], "box crushed")
        self.assertEqual(sent[0]["uploadedBy"], 84)
        for p in sent:
            self.assertTrue(p["path"].startswith("uploads/grn_return_photos/1/3/GRN_0001/v1/line2/"))
            self.assertTrue((self.root / p["path"][len("uploads/"):]).is_file())
        self.assertEqual(result["added"], sent)

    def test_rejects_unknown_condition(self):
        with self.assertRaises(HTTPException) as ctx:
            svc.add_return_photos(
                key=KEY, condition="LOST", photos=[IncomingPhoto("a.jpg", b"x")],
                label_reader=lambda p: None, http_post=MagicMock(),
            )
        self.assertEqual(ctx.exception.status_code, 400)

    def test_rejects_non_image(self):
        with self.assertRaises(HTTPException) as ctx:
            svc.add_return_photos(
                key=KEY, condition="RETURNED", photos=[IncomingPhoto("a.pdf", b"x")],
                label_reader=lambda p: None, http_post=MagicMock(),
            )
        self.assertEqual(ctx.exception.status_code, 400)

    def test_erp_failure_removes_saved_files(self):
        post = MagicMock(return_value=_fail(404, "GRN line not found"))
        with self.assertRaises(HTTPException) as ctx:
            svc.add_return_photos(
                key=KEY, condition="RETURNED", photos=[IncomingPhoto("a.jpg", b"x")],
                label_reader=lambda p: None, http_post=post,
            )
        self.assertEqual(ctx.exception.status_code, 404)
        self.assertEqual(ctx.exception.detail, "GRN line not found")
        self.assertEqual(list(self.root.rglob("*.jpg")), [])

    def test_remove_deletes_file_after_erp_confirms(self):
        folder = self.root / "grn_return_photos" / "1"
        folder.mkdir(parents=True)
        photo = folder / "abc.jpg"
        photo.write_bytes(b"x")
        delete = MagicMock(
            return_value=_ok({"removed": {"id": "abc", "path": "uploads/grn_return_photos/1/abc.jpg"}})
        )

        svc.remove_return_photo(key=KEY, photo_id="abc", http_delete=delete)

        self.assertEqual(delete.call_args.kwargs["params"]["photoId"], "abc")
        self.assertFalse(photo.exists())

    def test_label_text_is_flattened(self):
        self.assertEqual(svc.normalize_label_text("  Batch  B12\n\nEXP 10/27 \n"), "Batch B12 / EXP 10/27")
        self.assertIsNone(svc.normalize_label_text("  \n "))


if __name__ == "__main__":
    unittest.main()
