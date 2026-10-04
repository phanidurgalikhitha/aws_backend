import json
import unittest
from urllib.parse import quote_plus

from tests import helpers
from tests.helpers import OTHER, USER, api_event

import common
import delete_file
import download_url
import list_files
import presign
import process_upload

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20


def body(resp):
    return json.loads(resp["body"])


class Base(unittest.TestCase):
    def setUp(self):
        self.table, self.s3 = helpers.install_fakes()

    def request_url(self, **overrides):
        payload = {"fileName": "photo.png", "contentType": "image/png", "fileSize": 500}
        payload.update(overrides)
        return presign.handler(api_event(body=payload), None)


class PresignTests(Base):
    def test_success_builds_server_side_key_and_pending_record(self):
        resp = self.request_url()
        self.assertEqual(resp["statusCode"], 200)
        data = body(resp)
        self.assertTrue(data["objectKey"].startswith(f"uploads/{USER}/"))
        self.assertTrue(data["objectKey"].endswith("-photo.png"))
        self.assertEqual(data["headers"], {"Content-Type": "image/png"})
        self.assertEqual(data["expiresIn"], 300)
        item = self.table.items[(USER, data["fileId"])]
        self.assertEqual(item["status"], "PENDING")
        op, params, expires, method = self.s3.presigned[0]
        self.assertEqual((op, method, expires), ("put_object", "PUT", 300))
        self.assertEqual(params["ContentType"], "image/png")

    def test_requires_authenticated_user(self):
        resp = presign.handler(api_event(user=None, body={}), None)
        self.assertEqual(resp["statusCode"], 401)

    def test_path_traversal_is_neutralised(self):
        data = body(self.request_url(fileName="../../etc/passwd.png"))
        self.assertNotIn("..", data["objectKey"])
        self.assertTrue(data["objectKey"].startswith(f"uploads/{USER}/"))
        self.assertEqual(data["objectKey"].count("/"), 2)

    def test_validation_errors(self):
        cases = [
            (dict(fileSize=5000), 413),
            (dict(fileSize=0), 400),
            (dict(fileSize="12"), 400),
            (dict(fileSize=True), 400),
            (dict(contentType="application/x-msdownload"), 415),
            (dict(contentType="nonsense"), 400),
            (dict(fileName=""), 400),
            (dict(fileName=None), 400),
        ]
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                self.assertEqual(self.request_url(**overrides)["statusCode"], expected)
        self.assertEqual(len(self.table.items), 0)

    def test_bad_json_body(self):
        resp = presign.handler(api_event(body="{not json"), None)
        self.assertEqual(resp["statusCode"], 400)

    def test_unexpected_error_returns_generic_500(self):
        def boom(**kwargs):
            raise RuntimeError("secret internals")

        self.table.put_item = boom
        resp = self.request_url()
        self.assertEqual(resp["statusCode"], 500)
        self.assertNotIn("secret", resp["body"])


class ProcessUploadTests(Base):
    def make_pending(self, content_type="image/png"):
        data = body(self.request_url(contentType=content_type, fileName="a b.png"))
        return data["fileId"], data["objectKey"]

    def s3_event(self, key):
        return {"Records": [{"s3": {"object": {"key": quote_plus(key)}}}]}

    def test_valid_upload_marked_uploaded(self):
        file_id, key = self.make_pending()
        self.s3.objects[key] = (PNG, "image/png")
        process_upload.handler(self.s3_event(key), None)
        item = self.table.items[(USER, file_id)]
        self.assertEqual(item["status"], "UPLOADED")
        self.assertEqual(item["fileSize"], len(PNG))
        self.assertNotIn("expiresAt", item)

    def test_photo_upload_emits_metric(self):
        import contextlib
        import io

        file_id, key = self.make_pending()
        self.s3.objects[key] = (PNG, "image/png")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            process_upload.handler(self.s3_event(key), None)
        lines = [json.loads(line) for line in out.getvalue().splitlines() if line.startswith("{")]
        self.assertTrue(any(line.get("PhotosUploaded") == 1 for line in lines))
        self.assertEqual(lines[0]["_aws"]["CloudWatchMetrics"][0]["Namespace"], "SecureUpload")

    def test_rejected_upload_emits_no_metric(self):
        import contextlib
        import io

        file_id, key = self.make_pending()
        self.s3.objects[key] = (b"not a png", "image/png")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            process_upload.handler(self.s3_event(key), None)
        self.assertNotIn("PhotosUploaded", out.getvalue())

    def test_oversize_is_rejected_and_deleted(self):
        file_id, key = self.make_pending()
        self.s3.objects[key] = (PNG + b"0" * 2000, "image/png")
        process_upload.handler(self.s3_event(key), None)
        self.assertEqual(self.table.items[(USER, file_id)]["status"], "REJECTED")
        self.assertIn(key, self.s3.deleted)

    def test_fake_png_is_rejected_by_magic_bytes(self):
        file_id, key = self.make_pending()
        self.s3.objects[key] = (b"MZ this is an exe", "image/png")
        process_upload.handler(self.s3_event(key), None)
        item = self.table.items[(USER, file_id)]
        self.assertEqual(item["status"], "REJECTED")
        self.assertIn("does not match", item["rejectReason"])

    def test_content_type_mismatch_rejected(self):
        file_id, key = self.make_pending()
        self.s3.objects[key] = (PNG, "application/octet-stream")
        process_upload.handler(self.s3_event(key), None)
        self.assertEqual(self.table.items[(USER, file_id)]["status"], "REJECTED")

    def test_object_without_record_is_deleted(self):
        key = f"uploads/{USER}/{'a' * 32}-evil.png"
        self.s3.objects[key] = (PNG, "image/png")
        process_upload.handler(self.s3_event(key), None)
        self.assertIn(key, self.s3.deleted)

    def test_unexpected_key_ignored(self):
        process_upload.handler(self.s3_event("uploads/weird"), None)
        self.assertEqual(self.s3.deleted, [])

    def test_failure_raises_so_lambda_retries(self):
        file_id, key = self.make_pending()  # object never written -> head_object fails
        with self.assertRaises(RuntimeError):
            process_upload.handler(self.s3_event(key), None)


class FileApiTests(Base):
    def upload_one(self, name="doc.pdf", ctype="application/pdf", user=USER):
        resp = presign.handler(api_event(user=user, body={"fileName": name, "contentType": ctype, "fileSize": 100}), None)
        data = body(resp)
        self.s3.objects[data["objectKey"]] = (b"%PDF-1.4 hello", ctype)
        process_upload.handler({"Records": [{"s3": {"object": {"key": data["objectKey"]}}}]}, None)
        return data["fileId"]

    def test_list_only_returns_own_files_newest_first(self):
        import time

        first = self.upload_one("one.pdf")
        time.sleep(0.01)
        second = self.upload_one("two.pdf")
        self.upload_one("theirs.pdf", user=OTHER)
        files = body(list_files.handler(api_event(), None))["files"]
        self.assertEqual([f["fileId"] for f in files], [second, first])
        self.assertNotIn("objectKey", files[0])

    def test_download_url_for_owner(self):
        file_id = self.upload_one()
        resp = download_url.handler(api_event(path_params={"fileId": file_id}), None)
        self.assertEqual(resp["statusCode"], 200)
        self.assertIn("downloadUrl", body(resp))

    def test_cannot_access_someone_elses_file(self):
        file_id = self.upload_one(user=OTHER)
        for handler in (download_url.handler, delete_file.handler):
            resp = handler(api_event(path_params={"fileId": file_id}), None)
            self.assertEqual(resp["statusCode"], 404)
        self.assertEqual(self.s3.deleted, [])

    def test_pending_file_not_downloadable(self):
        data = body(presign.handler(api_event(body={"fileName": "x.pdf", "contentType": "application/pdf", "fileSize": 10}), None))
        resp = download_url.handler(api_event(path_params={"fileId": data["fileId"]}), None)
        self.assertEqual(resp["statusCode"], 409)

    def test_invalid_file_id_rejected(self):
        resp = delete_file.handler(api_event(path_params={"fileId": "../../x"}), None)
        self.assertEqual(resp["statusCode"], 400)

    def test_delete_removes_object_and_record(self):
        file_id = self.upload_one()
        key = self.table.items[(USER, file_id)]["objectKey"]
        resp = delete_file.handler(api_event(path_params={"fileId": file_id}), None)
        self.assertEqual(resp["statusCode"], 200)
        self.assertIn(key, self.s3.deleted)
        self.assertNotIn((USER, file_id), self.table.items)


class UtilTests(unittest.TestCase):
    def test_safe_filename(self):
        self.assertEqual(common.safe_filename("C:\\x\\my file (1).PDF"), "my_file_1_.PDF")
        self.assertEqual(common.safe_filename("...."), "file")
        self.assertLessEqual(len(common.safe_filename("a" * 300 + ".png")), 100)

    def test_content_type_wildcards(self):
        self.assertTrue(common.content_type_allowed("image/webp"))
        self.assertTrue(common.content_type_allowed("Application/PDF; charset=x"))
        self.assertFalse(common.content_type_allowed("video/mp4"))

    def test_file_ids_sort_by_time(self):
        import time

        a = common.new_file_id()
        time.sleep(0.01)
        b = common.new_file_id()
        self.assertEqual(len(a), 32)
        self.assertLess(a, b)


if __name__ == "__main__":
    unittest.main()
