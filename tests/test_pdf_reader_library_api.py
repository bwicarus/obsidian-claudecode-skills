from __future__ import annotations

from io import BytesIO
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from flask import Flask
from werkzeug.test import EnvironBuilder


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
import reader_book_ocr  # noqa: E402
from reader_book_library import BookLibrary, UploadTooLargeError  # noqa: E402
from reader_book_ocr import ReaderBookOcrService  # noqa: E402
from reader_book_ocr_worker import _publish_attachments  # noqa: E402
from reader_book_user_state import decode_package  # noqa: E402
from reader_sidecar_store import SidecarStore  # noqa: E402


PDF_A = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"
PDF_B = b"%PDF-1.4\n2 0 obj<</Type/Catalog>>endobj\n%%EOF\n"
IDENTITY = {"user_id": 1, "storage_namespace": "acct-v1-" + "a" * 64}


class PdfReaderLibraryApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        base = Path(self.temp.name)
        self.base = base
        self.vault = base / "vault"
        self.vault.mkdir()
        self.library = BookLibrary(self.vault, base / "state")
        self.previous = pdf_reader._READER_BOOK_LIBRARY
        self.previous_ocr = pdf_reader._READER_BOOK_OCR
        self.previous_user_state = pdf_reader._READER_BOOK_USER_STATE
        self.previous_sidecar_store = pdf_reader._READER_SIDECAR_STORE
        self.previous_sidecar_root = pdf_reader._READER_SIDECAR_ROOT
        pdf_reader._READER_BOOK_LIBRARY = self.library
        self.ocr_launches = []
        self.fake_worker_pid = 424242
        self.pid_alive_patcher = patch.object(
            reader_book_ocr,
            "_pid_alive",
            side_effect=lambda pid: int(pid or 0) == self.fake_worker_pid,
        )
        self.pid_alive_patcher.start()
        pdf_reader._READER_BOOK_OCR = ReaderBookOcrService(
            self.library,
            base / "ocr",
            ROOT,
            launcher=lambda job_dir, source_path, job: (
                self.ocr_launches.append((job_dir, source_path, job))
                or types.SimpleNamespace(pid=self.fake_worker_pid)
            ),
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
        )
        legacy = base / "legacy-sidecars"
        legacy.mkdir()
        pdf_reader._READER_SIDECAR_ROOT = base / "private-sidecars"
        pdf_reader._READER_SIDECAR_STORE = SidecarStore(
            pdf_reader._READER_SIDECAR_ROOT,
            legacy,
            authorize_claim=lambda _identity: False,
        )
        pdf_reader._READER_BOOK_USER_STATE = None
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
        self.pid_alive_patcher.stop()
        pdf_reader._READER_BOOK_LIBRARY = self.previous
        pdf_reader._READER_BOOK_OCR = self.previous_ocr
        pdf_reader._READER_BOOK_USER_STATE = self.previous_user_state
        pdf_reader._READER_SIDECAR_STORE = self.previous_sidecar_store
        pdf_reader._READER_SIDECAR_ROOT = self.previous_sidecar_root
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
        for route in ("start", "adopt", "pause", "resume", "cancel", "retry"):
            denied = self.client.post(
                f"/pdf/api/library/ocr/{route}",
                json={"bookId": "book_" + "a" * 32, "contentSha256": "b" * 64},
            )
            self.assertEqual(denied.status_code, 401)
        self.assertEqual(
            self.client.get(
                "/pdf/api/library/ocr/status",
                query_string={"bookId": "book_" + "a" * 32, "contentSha256": "b" * 64},
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.get("/pdf/api/library/ocr/executors").status_code, 401
        )
        # 多份预处理结果的三条（列举 / 切换 / 删除）—— 删除尤其不能漏：
        # 它是唯一会真的抹掉数据的路由。
        self.assertEqual(
            self.client.get(
                "/pdf/api/library/ocr/releases",
                query_string={"bookId": "book_" + "a" * 32, "contentSha256": "b" * 64},
            ).status_code,
            401,
        )
        for route in ("activate", "delete"):
            denied = self.client.post(
                f"/pdf/api/library/ocr/releases/{route}",
                json={
                    "bookId": "book_" + "a" * 32,
                    "contentSha256": "b" * 64,
                    "runId": "ocrrun_" + "0" * 16,
                },
            )
            self.assertEqual(denied.status_code, 401)
        self.assertEqual(
            self.client.post(
                "/pdf/api/library/ocr/worker/claim",
                json={
                    "contract": "reader-library-ocr-worker/1",
                    "workerId": "pc_denied",
                    "capabilities": {
                        "engines": ["vision"],
                        "processingProfile": "quality-first-v4",
                    },
                },
            ).status_code,
            401,
        )
        for method, route in (
            (self.client.get, "/pdf/api/library/ocr/worker/source"),
            (self.client.post, "/pdf/api/library/ocr/worker/heartbeat"),
            (self.client.put, "/pdf/api/library/ocr/worker/pages/1"),
            (self.client.put, "/pdf/api/library/ocr/worker/formulas"),
            (self.client.post, "/pdf/api/library/ocr/worker/complete"),
        ):
            self.assertEqual(method(route).status_code, 401)
        self.assertEqual(
            self.client.get(
                "/pdf/api/library/ocr/adoption-preview",
                query_string={"bookId": "book_" + "a" * 32, "contentSha256": "b" * 64},
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.get(
                "/pdf/api/library/attachments/book_" + "a" * 32,
                query_string={"contentSha256": "b" * 64},
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.get(
                "/pdf/api/library/user-state/book_" + "a" * 32,
                query_string={"contentSha256": "b" * 64},
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.get(
                "/pdf/api/library/ocr/page-chars/book_" + "a" * 32 + "/1",
                query_string={"contentSha256": "b" * 64},
            ).status_code,
            401,
        )

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
        denied = self.client.post(
            "/pdf/api/library/ocr/start",
            json={"bookId": "book_" + "a" * 32, "contentSha256": "b" * 64},
        )
        self.assertEqual(denied.status_code, 403)

    def test_manual_pi_ocr_contract_uses_only_catalog_identity(self) -> None:
        (self.vault / "A.pdf").write_bytes(PDF_A)
        entry = self.client.get("/pdf/api/library/catalog").get_json()["books"][0]
        started = self.client.post(
            "/pdf/api/library/ocr/start",
            json={
                "bookId": entry["bookId"],
                "contentSha256": entry["contentSha256"],
                "engine": "vision",
            },
        )
        self.assertEqual(started.status_code, 200)
        payload = started.get_json()
        self.assertEqual(payload["contract"], "reader-library-ocr/1")
        self.assertEqual(payload["job"]["state"], "queued")
        self.assertEqual(
            set(payload["job"]["textProgress"]),
            {"total", "completed", "pending", "failed", "unavailable"},
        )
        self.assertEqual(
            set(payload["job"]["wordProgress"]),
            {"total", "completed", "pending", "failed", "unavailable"},
        )
        self.assertEqual(
            set(payload["job"]["formulaProgress"]),
            {"total", "completed", "pending", "failed", "unavailable"},
        )
        self.assertNotIn(str(self.vault), started.get_data(as_text=True))
        self.assertEqual(len(self.ocr_launches), 1)

        status = self.client.get(
            "/pdf/api/library/ocr/status",
            query_string={
                "bookId": entry["bookId"],
                "contentSha256": entry["contentSha256"],
            },
        )
        self.assertEqual(status.status_code, 200)
        self.assertEqual(status.get_json()["job"]["bookId"], entry["bookId"])

        path_attempt = self.client.post(
            "/pdf/api/library/ocr/start",
            json={
                "bookId": entry["bookId"],
                "contentSha256": entry["contentSha256"],
                "file": "../../secret.pdf",
            },
        )
        self.assertEqual(path_attempt.status_code, 400)
        self.assertEqual(path_attempt.get_json()["code"], "invalid-request")

        stale = self.client.post(
            "/pdf/api/library/ocr/start",
            json={"bookId": entry["bookId"], "contentSha256": "0" * 64},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.get_json()["code"], "book-version-changed")

    def test_pc_worker_http_contract_is_outbound_leased_and_publishes_common_attachments(self) -> None:
        (self.vault / "A.pdf").write_bytes(PDF_A)
        entry = self.client.get("/pdf/api/library/catalog").get_json()["books"][0]
        pdf_reader._READER_BOOK_OCR = ReaderBookOcrService(
            self.library,
            self.base / "pc-http-ocr",
            ROOT,
            launcher=lambda *_args: self.fail("PC start must not spawn a Pi worker"),
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 1,
            pc_lease_seconds=60,
            pc_online_seconds=60,
        )
        before = self.client.get("/pdf/api/library/ocr/executors")
        self.assertEqual(before.status_code, 200)
        self.assertFalse(next(
            item for item in before.get_json()["executors"] if item["executor"] == "pc"
        )["online"])

        started = self.client.post(
            "/pdf/api/library/ocr/start",
            json={
                "bookId": entry["bookId"],
                "contentSha256": entry["contentSha256"],
                "engine": "vision",
                "executor": "pc",
            },
        )
        self.assertEqual(started.status_code, 200)
        self.assertEqual(started.get_json()["job"]["executor"], "pc")
        self.assertEqual(
            started.get_json()["job"]["processingProfile"], "quality-first-v4"
        )

        claimed = self.client.post(
            "/pdf/api/library/ocr/worker/claim",
            json={
                "contract": "reader-library-ocr-worker/1",
                "workerId": "pc_http",
                "capabilities": {
                    "engines": ["vision"],
                    "maxPdfBytes": 1024 * 1024,
                    "maxPageBytes": 1024 * 1024,
                    "processingProfile": "quality-first-v4",
                },
            },
        )
        self.assertEqual(claimed.status_code, 200)
        claim = claimed.get_json()
        self.assertEqual(claim["contract"], "reader-library-ocr-worker/1")
        self.assertEqual(
            claim["job"]["processingProfile"], "quality-first-v4"
        )
        identity = {
            "contract": claim["contract"],
            "workerId": "pc_http",
            "bookId": claim["job"]["bookId"],
            "contentSha256": claim["job"]["contentSha256"],
            "jobId": claim["job"]["jobId"],
            "generation": claim["job"]["generation"],
            "leaseId": claim["lease"]["leaseId"],
        }
        source = self.client.get(
            claim["job"]["sourceUrl"], headers={"Range": "bytes=0-3"}
        )
        self.assertEqual(source.status_code, 206)
        self.assertEqual(source.data, PDF_A[:4])
        source.close()

        heartbeat = self.client.post(
            "/pdf/api/library/ocr/worker/heartbeat",
            json={
                **identity,
                "phase": "text-ocr",
                "state": "running",
                "currentPage": None,
                "progress": {"totalPages": 1, "textCompleted": 0},
            },
        )
        self.assertEqual(heartbeat.status_code, 200)
        self.assertEqual(heartbeat.get_json()["desiredState"], "running")

        page = {
            "schema": "reader-page-chars/1",
            "bookId": entry["bookId"],
            "contentSha256": entry["contentSha256"],
            "engine": "vision",
            "pageNumber": 1,
            "page_w": 10,
            "page_h": 20,
            "chars": [{
                "c": "A", "x0": 1, "y0": 1, "x1": 2, "y1": 2,
                "w": 1, "bk": 0, "b": 0,
            }],
            "furigana": [],
            "tokenized": True,
        }
        uploaded = self.client.put(
            "/pdf/api/library/ocr/worker/pages/1",
            json={**identity, "page": page},
        )
        self.assertEqual(uploaded.status_code, 200)
        formulas = self.client.put(
            "/pdf/api/library/ocr/worker/formulas",
            json={
                **identity,
                "formula": {
                    "schema": "reader-formula-regions/1",
                    "bookId": entry["bookId"],
                    "contentSha256": entry["contentSha256"],
                    "formulas": [],
                },
                "formulaState": "unavailable",
                "formulaReason": "formula-model-unavailable",
            },
        )
        self.assertEqual(formulas.status_code, 200)
        self.assertEqual(
            formulas.get_json()["job"]["formulaProgress"]["unavailable"], 1
        )
        completed = self.client.post(
            "/pdf/api/library/ocr/worker/complete",
            json={**identity, "totalPages": 1},
        )
        self.assertEqual(completed.status_code, 200)
        self.assertTrue(completed.get_json()["published"])
        manifest = self.client.get(
            f"/pdf/api/library/attachments/{entry['bookId']}",
            query_string={"contentSha256": entry["contentSha256"]},
        )
        self.assertEqual(manifest.status_code, 200)
        self.assertEqual(manifest.get_json()["formulaState"], "unavailable")
        self.assertEqual(
            manifest.get_json()["formulaReason"], "formula-model-unavailable"
        )

    def test_worker_json_without_content_length_is_stream_bounded(self) -> None:
        def open_chunked(body: bytes):
            builder = EnvironBuilder(
                path="/pdf/api/library/ocr/worker/claim",
                method="POST",
                input_stream=BytesIO(body),
                content_type="application/json",
            )
            environ = builder.get_environ()
            environ.pop("CONTENT_LENGTH", None)
            environ["wsgi.input_terminated"] = True
            return self.client.open(environ)

        valid = json.dumps({
            "contract": "reader-library-ocr-worker/1",
            "workerId": "pc_chunked",
            "capabilities": {
                "engines": ["vision"],
                "maxPdfBytes": 1024 * 1024,
                "maxPageBytes": 1024 * 1024,
                "processingProfile": "quality-first-v4",
            },
        }).encode("utf-8")
        accepted = open_chunked(valid)
        self.assertEqual(accepted.status_code, 204)

        oversized = (
            b'{"contract":"reader-library-ocr-worker/1","workerId":"pc_chunked",'
            b'"capabilities":{"engines":["vision"],"processingProfile":"quality-first-v4"},'
            b'"padding":"' + (b"x" * (64 * 1024)) + b'"}'
        )
        rejected = open_chunked(oversized)
        self.assertEqual(rejected.status_code, 413)
        self.assertEqual(rejected.get_json()["code"], "worker-payload-too-large")

    def test_worker_claim_reads_json_cached_by_vbook_gate(self) -> None:
        fake_vbook = types.SimpleNamespace(is_view_ref=lambda _value: False)
        with patch.object(pdf_reader, "VB", fake_vbook):
            response = self.client.post(
                "/pdf/api/library/ocr/worker/claim",
                json={
                    "contract": "reader-library-ocr-worker/1",
                    "workerId": "pc_cached_body",
                    "capabilities": {
                        "engines": ["vision"],
                        "maxPdfBytes": 1024 * 1024,
                        "maxPageBytes": 1024 * 1024,
                        "processingProfile": "quality-first-v4",
                    },
                },
            )
        self.assertEqual(response.status_code, 204)

    def test_legacy_adoption_routes_are_authenticated_path_free_and_idempotent(self) -> None:
        (self.vault / "A.pdf").write_bytes(PDF_A)
        entry = self.client.get("/pdf/api/library/catalog").get_json()["books"][0]
        pdf_reader._READER_BOOK_OCR = ReaderBookOcrService(
            self.library,
            self.base / "adopted-ocr",
            self.base / "legacy-project",
            launcher=lambda *_args: self.fail("adoption must not launch OCR"),
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 1,
            legacy_embedded_page_reader=lambda _path, _rel, _page: {
                "chars": [{
                    "c": "A", "x0": 1, "y0": 1, "x1": 2, "y1": 2,
                    "sp": 0, "w": 1, "b": 0, "bk": 0,
                }],
                "page_w": 10,
                "page_h": 20,
                "furigana": [],
            },
        )
        preview = self.client.get(
            "/pdf/api/library/ocr/adoption-preview",
            query_string={
                "bookId": entry["bookId"],
                "contentSha256": entry["contentSha256"],
            },
        )
        self.assertEqual(preview.status_code, 200)
        self.assertTrue(preview.get_json()["adoption"]["available"])
        self.assertNotIn(str(self.vault), preview.get_data(as_text=True))

        rejected_path = self.client.post(
            "/pdf/api/library/ocr/adopt",
            json={
                "bookId": entry["bookId"],
                "contentSha256": entry["contentSha256"],
                "path": "../../A.pdf",
            },
        )
        self.assertEqual(rejected_path.status_code, 400)
        self.assertEqual(rejected_path.get_json()["code"], "invalid-request")

        adopted = self.client.post(
            "/pdf/api/library/ocr/adopt",
            json={
                "bookId": entry["bookId"],
                "contentSha256": entry["contentSha256"],
            },
        )
        self.assertEqual(adopted.status_code, 200)
        self.assertFalse(adopted.get_json()["already"])
        self.assertEqual(adopted.get_json()["job"]["engine"], "legacy")
        self.assertTrue(adopted.get_json()["adoption"]["alreadyAdopted"])
        self.assertTrue(adopted.get_json()["adoption"]["revision"].startswith("ocr_"))
        repeated = self.client.post(
            "/pdf/api/library/ocr/adopt",
            json={
                "bookId": entry["bookId"],
                "contentSha256": entry["contentSha256"],
            },
        )
        self.assertEqual(repeated.status_code, 200)
        self.assertTrue(repeated.get_json()["already"])
        self.assertEqual(
            repeated.get_json()["adoption"]["revision"],
            adopted.get_json()["adoption"]["revision"],
        )

    def test_user_state_package_is_account_scoped_and_version_bound(self) -> None:
        (self.vault / "A.pdf").write_bytes(PDF_A)
        entry = self.client.get("/pdf/api/library/catalog").get_json()["books"][0]
        response = self.client.get(
            f"/pdf/api/library/user-state/{entry['bookId']}",
            query_string={"contentSha256": entry["contentSha256"]},
        )
        self.assertEqual(response.status_code, 200)
        package = decode_package(response.data)
        self.assertEqual(package["bookId"], entry["bookId"])
        self.assertEqual(package["contentSha256"], entry["contentSha256"])
        self.assertEqual(len(package["domains"]), 8)
        account_digest = response.headers["X-Reader-Account-Scope-Digest"]
        self.assertRegex(account_digest, r"^[a-f0-9]{64}$")
        self.assertNotIn(IDENTITY["storage_namespace"], response.get_data(as_text=True))

        stale = self.client.get(
            f"/pdf/api/library/user-state/{entry['bookId']}",
            query_string={"contentSha256": "0" * 64},
        )
        self.assertEqual(stale.status_code, 409)

    def test_release_listing_switching_and_deletion_over_http(self) -> None:
        """三条新路由端到端走一遍 —— 服务层通了不等于 HTTP 层也通。"""

        (self.vault / "A.pdf").write_bytes(PDF_A)
        entry = self.client.get("/pdf/api/library/catalog").get_json()["books"][0]
        query = {
            "bookId": entry["bookId"],
            "contentSha256": entry["contentSha256"],
        }
        listing = self.client.get("/pdf/api/library/ocr/releases", query_string=query)
        self.assertEqual(listing.status_code, 200)
        payload = listing.get_json()
        # 还没跑过预处理：空列表而不是报错。
        self.assertEqual(payload["runs"], [])
        self.assertIsNone(payload["activeRunId"])
        # 删除/切换会立刻改变它，不允许任何中间缓存留副本。
        self.assertEqual(listing.headers.get("Cache-Control"), "private, no-store")

        # 未知 runId 要 404，而不是悄悄成功或 500。
        missing = self.client.post(
            "/pdf/api/library/ocr/releases/delete",
            json={**query, "runId": "ocrrun_" + "0" * 16, "allowDeactivate": True},
        )
        self.assertEqual(missing.status_code, 404)
        # 格式非法的 runId 要 400（跟"不存在"分开报，便于排查）。
        malformed = self.client.post(
            "/pdf/api/library/ocr/releases/activate",
            json={**query, "runId": "nope"},
        )
        self.assertEqual(malformed.status_code, 400)
        # 请求体是硬白名单：多一个字段就 400。
        extra = self.client.post(
            "/pdf/api/library/ocr/releases/activate",
            json={**query, "runId": "ocrrun_" + "0" * 16, "surprise": 1},
        )
        self.assertEqual(extra.status_code, 400)

    def test_versioned_attachments_and_page_formula_layer(self) -> None:
        (self.vault / "A.pdf").write_bytes(PDF_A)
        entry = self.client.get("/pdf/api/library/catalog").get_json()["books"][0]
        project = self.base / "formula-project"
        formula_key = hashlib.sha1(
            str((self.vault / "A.pdf").resolve()).encode("utf-8")
        ).hexdigest()[:16]
        formula_path = project / "state" / "pdf-figures" / f"{formula_key}.json"
        formula_path.parent.mkdir(parents=True)
        formula_path.write_text(json.dumps({
            "sourceContentSha256": entry["contentSha256"],
            "pdf": str((self.vault / "A.pdf").resolve()),
            "book_mtime": int((self.vault / "A.pdf").stat().st_mtime),
            "formulas": [{"page": 1, "bbox": [0, 0, 0.5, 0.5], "latex": "x"}],
        }), "utf-8")
        pdf_reader._READER_BOOK_OCR = ReaderBookOcrService(
            self.library,
            self.base / "formula-ocr",
            project,
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 1,
            legacy_embedded_page_reader=lambda *_args: {
            "page_w": 100,
            "page_h": 100,
            "chars": [
                {"c": "?", "x0": 10, "y0": 10, "x1": 20, "y1": 20, "w": 1, "bk": 1, "b": 0},
                {"c": "A", "x0": 70, "y0": 70, "x1": 80, "y1": 80, "w": 2, "bk": 2, "b": 0},
            ],
            "furigana": [],
            },
        )
        adopted = self.client.post(
            "/pdf/api/library/ocr/adopt",
            json={"bookId": entry["bookId"], "contentSha256": entry["contentSha256"]},
        )
        self.assertEqual(adopted.status_code, 200)
        revision = adopted.get_json()["adoption"]["revision"]

        manifest_response = self.client.get(entry["attachmentsUrl"])
        self.assertEqual(manifest_response.status_code, 200)
        manifest = manifest_response.get_json()
        self.assertEqual(manifest["category"], "derived")
        self.assertEqual(manifest["mergePolicy"], "immutable")
        self.assertEqual(manifest["revision"], revision)

        page_response = self.client.get(
            f"/pdf/api/library/ocr/page-chars/{entry['bookId']}/1",
            query_string={"contentSha256": entry["contentSha256"]},
        )
        self.assertEqual(page_response.status_code, 200)
        chars = page_response.get_json()["chars"]
        self.assertNotIn("?", [item["c"] for item in chars])
        self.assertIn("A", [item["c"] for item in chars])
        self.assertEqual(next(item for item in chars if item.get("flx"))["flx"], "x")

        formula_entry = next(
            item for item in manifest["files"] if item["attachmentId"] == "ocr-formulas"
        )
        downloaded = self.client.get(formula_entry["downloadUrl"])
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(
            hashlib.sha256(downloaded.data).hexdigest(), formula_entry["sha256"]
        )
        downloaded.close()
        stale = self.client.get(
            f"/pdf/api/library/attachments/{entry['bookId']}/ocr-formulas",
            query_string={
                "contentSha256": entry["contentSha256"],
                "revision": "ocr_" + "0" * 20,
            },
        )
        self.assertEqual(stale.status_code, 409)

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
