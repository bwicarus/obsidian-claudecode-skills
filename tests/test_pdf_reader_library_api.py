from __future__ import annotations

from io import BytesIO
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from flask import Flask


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))

# pdf_reader also registers the Linux-only RBI transport.  The library routes do
# not exercise it, so give that import the same no-op advisory-lock surface on
# Windows rather than skipping the HTTP contract tests entirely.
if sys.platform == "win32" and "fcntl" not in sys.modules:
    fcntl_stub = types.ModuleType("fcntl")
    fcntl_stub.LOCK_EX = 1
    fcntl_stub.LOCK_SH = 2
    fcntl_stub.LOCK_NB = 4
    fcntl_stub.LOCK_UN = 8
    fcntl_stub.flock = lambda *_args, **_kwargs: None
    sys.modules["fcntl"] = fcntl_stub

import pdf_reader  # noqa: E402
from reader_book_library import BookLibrary, UploadTooLargeError  # noqa: E402


PDF_A = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"
PDF_B = b"%PDF-1.4\n2 0 obj<</Type/Catalog>>endobj\n%%EOF\n"
IDENTITY = {"user_id": 1, "storage_namespace": "acct-v1-" + "a" * 64}


class PdfReaderLibraryApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.vault = base / "vault"
        self.vault.mkdir()
        self.library = BookLibrary(self.vault, base / "state")
        self.previous = pdf_reader._READER_BOOK_LIBRARY
        pdf_reader._READER_BOOK_LIBRARY = self.library
        self.app = Flask(__name__)
        self.app.secret_key = "test"
        self.identity = IDENTITY
        self.app.extensions["reader_storage_identity_resolver"] = lambda: self.identity
        self.authorized = True
        self.app.extensions["reader_book_library_authorizer"] = (
            lambda _identity: self.authorized
        )
        self.app.register_blueprint(pdf_reader.bp)
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        pdf_reader._READER_BOOK_LIBRARY = self.previous
        self.temp.cleanup()

    def test_all_library_routes_require_verified_owner(self) -> None:
        self.identity = None
        self.assertEqual(self.client.get("/pdf/api/library/catalog").status_code, 401)
        self.assertEqual(
            self.client.get("/pdf/api/library/download/book_" + "a" * 32).status_code,
            401,
        )
        response = self.client.post(
            "/pdf/api/library/upload",
            data={"file": (BytesIO(PDF_A), "A.pdf")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 401)

    def test_verified_non_owner_cannot_access_shared_vault_library(self) -> None:
        self.authorized = False
        self.assertEqual(self.client.get("/pdf/api/library/catalog").status_code, 403)
        self.assertEqual(
            self.client.get("/pdf/api/library/download/book_" + "a" * 32).status_code,
            403,
        )
        response = self.client.post(
            "/pdf/api/library/upload",
            data={"file": (BytesIO(PDF_A), "A.pdf")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 403)

    def test_catalog_download_range_and_upload_contract(self) -> None:
        path = self.vault / "A.pdf"
        path.write_bytes(PDF_A)
        response = self.client.get("/pdf/api/library/catalog")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 1)
        entry = payload["books"][0]
        self.assertTrue(entry["downloadUrl"].startswith("/pdf/"))
        self.assertNotIn(str(self.vault), response.get_data(as_text=True))

        ranged = self.client.get(entry["downloadUrl"], headers={"Range": "bytes=0-3"})
        self.assertEqual(ranged.status_code, 206)
        self.assertEqual(ranged.data, b"%PDF")
        self.assertEqual(ranged.headers["Accept-Ranges"], "bytes")
        self.assertEqual(ranged.headers["X-Reader-Book-Id"], entry["bookId"])
        ranged.close()

        uploaded = self.client.post(
            "/pdf/api/library/upload",
            data={
                "target_dir": "资源/uploads",
                "file": (BytesIO(PDF_B), "From iPad.pdf"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(uploaded.status_code, 200)
        upload_payload = uploaded.get_json()
        self.assertTrue(upload_payload["ok"])
        self.assertFalse(upload_payload["deduplicated"])
        self.assertEqual(upload_payload["bookId"], upload_payload["book"]["bookId"])
        self.assertEqual(
            upload_payload["contentSha256"],
            upload_payload["book"]["contentSha256"],
        )

        duplicate = self.client.post(
            "/pdf/api/library/upload",
            data={"file": (BytesIO(PDF_B), "Duplicate.pdf")},
            content_type="multipart/form-data",
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertTrue(duplicate.get_json()["deduplicated"])
        self.assertEqual(duplicate.get_json()["bookId"], upload_payload["bookId"])

    def test_content_length_is_rejected_before_multipart_parsing(self) -> None:
        self.library.max_upload_bytes = 64
        response = self.client.post(
            "/pdf/api/library/upload",
            data=b"x" * 128,
            content_type="multipart/form-data; boundary=not-parsable",
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.get_json()["maxBytes"], 64)

    def test_streaming_limit_maps_to_dedicated_413_response(self) -> None:
        with patch.object(
            self.library,
            "ingest",
            side_effect=UploadTooLargeError(123),
        ):
            response = self.client.post(
                "/pdf/api/library/upload",
                data={"file": (BytesIO(PDF_A), "Too-large.pdf")},
                content_type="multipart/form-data",
            )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.get_json()["maxBytes"], 123)

    def test_upload_target_is_fixed_to_vault_uploads(self) -> None:
        response = self.client.post(
            "/pdf/api/library/upload",
            data={
                "target_dir": "Books",
                "file": (BytesIO(PDF_B), "Other.pdf"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("restricted", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
