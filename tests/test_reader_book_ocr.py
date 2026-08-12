from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))

from reader_book_library import BookLibrary  # noqa: E402
import reader_book_ocr  # noqa: E402
import reader_book_ocr_worker  # noqa: E402
from reader_book_ocr import ReaderBookOcrError, ReaderBookOcrService  # noqa: E402
from reader_book_ocr_worker import (  # noqa: E402
    _manga_line_char_boxes,
    _publish_attachments,
    _publish_release,
    _tokenize_chars,
)


PDF_A = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"


class _FakeProcess:
    def __init__(self, pid: int | None = None) -> None:
        self.pid = int(pid or os.getpid())


class ReaderBookOcrFormulaWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        (self.project / "scripts").mkdir()
        (self.project / "scripts" / "yolo_figures.py").write_text("", "utf-8")
        self.pdf = self.project / "book.pdf"
        self.pdf.write_bytes(PDF_A)
        self.job_path = self.project / "job.json"
        self.job_path.write_text(json.dumps({
            "jobId": "ocrjob_formula_test",
            "workerGeneration": "ocrgen_formula_test",
            "totalPages": 588,
        }), "utf-8")
        self.control_path = self.project / "control.json"
        self.control_path.write_text(
            json.dumps({"desiredState": "running"}), "utf-8"
        )
        self.args = SimpleNamespace(
            project=str(self.project),
            pdf=str(self.pdf),
        )
        reader_book_ocr_worker._set_worker_identity(None, None)

    def tearDown(self) -> None:
        reader_book_ocr_worker._set_worker_identity(None, None)
        self.temp.cleanup()

    @staticmethod
    def _completed_child():
        class Child:
            returncode = 0

            @staticmethod
            def poll():
                return 0

        return Child()

    def _rewrite_formula_sidecar(self) -> None:
        formula_path = reader_book_ocr_worker._formula_path(
            self.project, self.pdf
        )
        value = json.loads(formula_path.read_text("utf-8"))
        value["geom_at"] = int(time.time())
        reader_book_ocr_worker._atomic_json(formula_path, value)

    def test_formula_detector_success_requires_this_run_to_rewrite_target(self) -> None:
        captured = {}

        def launch(_cmd, **kwargs):
            captured.update(kwargs)
            kwargs["stdout"].write(b"processing 1 sidecar(s)...\ndone\n")
            kwargs["stdout"].flush()
            self._rewrite_formula_sidecar()
            return self._completed_child()

        with patch.object(
            reader_book_ocr_worker.subprocess, "Popen", side_effect=launch
        ):
            result = reader_book_ocr_worker._run_formula_pipeline(
                self.args, self.job_path, self.control_path
            )

        self.assertEqual(result, 0)
        self.assertIs(captured["stderr"], reader_book_ocr_worker.subprocess.STDOUT)
        self.assertTrue(captured["stdout"].closed)
        logs = list(
            (self.project / "state" / "pdf-figures" / "formula-detect-logs")
            .glob("*.log")
        )
        self.assertEqual(len(logs), 1)
        self.assertIn("ocrjob_formula_test", logs[0].name)
        self.assertIn("processing 1 sidecar", logs[0].read_text("utf-8"))
        if os.name == "posix":
            self.assertEqual(logs[0].stat().st_mode & 0o777, 0o600)

    def test_formula_detector_stdout_error_cannot_be_reported_as_success(self) -> None:
        captured_handle = None

        def launch(_cmd, **kwargs):
            nonlocal captured_handle
            captured_handle = kwargs["stdout"]
            captured_handle.write(
                b"processing 1 sidecar(s)...\n"
                b"  ERROR fake-sidecar.json: missing doclayout_yolo\n"
                b"done\n"
            )
            captured_handle.flush()
            self._rewrite_formula_sidecar()
            return self._completed_child()

        with patch.object(
            reader_book_ocr_worker.subprocess, "Popen", side_effect=launch
        ):
            with self.assertRaisesRegex(RuntimeError, "reported ERROR"):
                reader_book_ocr_worker._run_formula_pipeline(
                    self.args, self.job_path, self.control_path
                )
        self.assertTrue(captured_handle.closed)

    def test_formula_detector_zero_match_and_unchanged_target_fail_closed(self) -> None:
        for output, expected in (
            (b"processing 0 sidecar(s)...\ndone\n", "matched no target sidecar"),
            (b"processing 1 sidecar(s)...\ndone\n", "did not update the target"),
        ):
            with self.subTest(expected=expected):
                def launch(_cmd, **kwargs):
                    kwargs["stdout"].write(output)
                    kwargs["stdout"].flush()
                    return self._completed_child()

                with patch.object(
                    reader_book_ocr_worker.subprocess, "Popen", side_effect=launch
                ):
                    with self.assertRaisesRegex(RuntimeError, expected):
                        reader_book_ocr_worker._run_formula_pipeline(
                            self.args, self.job_path, self.control_path
                        )

    def test_formula_log_open_failure_is_visible_and_does_not_launch(self) -> None:
        with patch.object(
            reader_book_ocr_worker,
            "_open_formula_detect_log",
            side_effect=PermissionError("log path denied"),
        ), patch.object(reader_book_ocr_worker.subprocess, "Popen") as launch:
            with self.assertRaisesRegex(RuntimeError, "log could not open"):
                reader_book_ocr_worker._run_formula_pipeline(
                    self.args, self.job_path, self.control_path
                )
        launch.assert_not_called()

    def test_formula_log_handle_closes_when_detector_cannot_start(self) -> None:
        log_handle = io.BytesIO()
        with patch.object(
            reader_book_ocr_worker,
            "_open_formula_detect_log",
            return_value=log_handle,
        ), patch.object(
            reader_book_ocr_worker.subprocess,
            "Popen",
            side_effect=FileNotFoundError("detector missing"),
        ):
            with self.assertRaisesRegex(RuntimeError, "could not start"):
                reader_book_ocr_worker._run_formula_pipeline(
                    self.args, self.job_path, self.control_path
                )
        self.assertTrue(log_handle.closed)

    def test_formula_log_handle_closes_and_child_stops_on_monitor_failure(self) -> None:
        log_handle = io.BytesIO()

        class Child:
            returncode = None
            terminated = False

            def poll(self):
                return self.returncode

            def terminate(self):
                self.terminated = True
                self.returncode = -15

            def wait(self, timeout):
                return self.returncode

            def kill(self):
                raise AssertionError("terminate should have stopped the child")

        child = Child()
        with patch.object(
            reader_book_ocr_worker,
            "_open_formula_detect_log",
            return_value=log_handle,
        ), patch.object(
            reader_book_ocr_worker.subprocess, "Popen", return_value=child
        ), patch.object(
            reader_book_ocr_worker,
            "_run_controlled",
            side_effect=RuntimeError("job update failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "job update failed"):
                reader_book_ocr_worker._run_formula_pipeline(
                    self.args, self.job_path, self.control_path
                )
        self.assertTrue(child.terminated)
        self.assertTrue(log_handle.closed)

    def test_formula_log_tail_read_is_bounded(self) -> None:
        path = self.project / "large.log"
        path.write_bytes(b"x" * 100_000 + b"TAIL")
        self.assertEqual(
            reader_book_ocr_worker._read_formula_detect_log_tail(path, 8),
            "xxxxTAIL",
        )


class ReaderBookOcrServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.vault = self.base / "vault"
        self.vault.mkdir()
        (self.vault / "A.pdf").write_bytes(PDF_A)
        self.library = BookLibrary(self.vault, self.base / "catalog")
        self.entry = self.library.catalog()[0]
        self.launches = []
        self.fake_worker_pid = 424242
        self.pid_alive_patcher = patch.object(
            reader_book_ocr,
            "_pid_alive",
            side_effect=lambda pid: int(pid or 0) == self.fake_worker_pid,
        )
        self.pid_alive_patcher.start()

        def launch(job_dir, source_path, job):
            self.launches.append((job_dir, source_path, dict(job)))
            return _FakeProcess(self.fake_worker_pid)

        self.service = ReaderBookOcrService(
            self.library,
            self.base / "ocr",
            ROOT,
            launcher=launch,
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
        )

    def tearDown(self) -> None:
        self.pid_alive_patcher.stop()
        self.temp.cleanup()

    def test_start_is_content_addressed_path_free_and_idempotent(self) -> None:
        job, already = self.service.start(
            self.entry["bookId"], self.entry["contentSha256"], "vision"
        )
        self.assertFalse(already)
        self.assertEqual(job["state"], "queued")
        self.assertEqual(job["pauseMode"], "checkpoint-restart")
        self.assertNotIn(str(self.vault), json.dumps(job))
        stored = json.loads((self.launches[0][0] / "job.json").read_text("utf-8"))
        self.assertNotIn("sourcePath", stored)
        self.assertNotIn(str(self.vault), json.dumps(stored))
        self.assertEqual(stored["workerPid"], self.fake_worker_pid)
        self.assertEqual(stored["pid"], self.fake_worker_pid)
        self.assertEqual(
            stored["workerGeneration"], self.launches[0][2]["workerGeneration"]
        )

        # A model may spend a long time importing before its first progress
        # update.  The synchronous spawn handshake already owns the generation,
        # so age alone must never turn this queued job into a retryable failure.
        stored["updatedAtEpochMs"] = 1
        (self.launches[0][0] / "job.json").write_text(json.dumps(stored), "utf-8")
        slow_start = self.service.status(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(slow_start["state"], "queued")

        repeated, already = self.service.start(
            self.entry["bookId"], self.entry["contentSha256"], "vision"
        )
        self.assertTrue(already)
        self.assertEqual(repeated["jobId"], job["jobId"])
        self.assertEqual(len(self.launches), 1)

    def test_pc_executor_claim_upload_and_common_publication(self) -> None:
        service = ReaderBookOcrService(
            self.library,
            self.base / "pc-ocr",
            ROOT,
            launcher=lambda *_args: self.fail("PC executor must not spawn a Pi worker"),
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 1,
            pc_lease_seconds=60,
            pc_online_seconds=60,
        )
        job, already = service.start(
            self.entry["bookId"], self.entry["contentSha256"], "manga", "pc"
        )
        self.assertFalse(already)
        self.assertEqual(job["executor"], "pc")
        self.assertEqual(job["totalPages"], 1)

        claimed = service.claim_pc_worker("pc_test", {
            "engines": ["manga"],
            "maxPdfBytes": 1024 * 1024,
            "maxPageBytes": 1024 * 1024,
            "processingProfile": "quality-first-v2",
        })
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["job"]["completedPages"], [])
        pc_status = next(
            item for item in service.executor_status() if item["executor"] == "pc"
        )
        self.assertTrue(pc_status["online"])
        identity = {
            "contract": reader_book_ocr.WORKER_CONTRACT,
            "workerId": "pc_test",
            "bookId": claimed["job"]["bookId"],
            "contentSha256": claimed["job"]["contentSha256"],
            "jobId": claimed["job"]["jobId"],
            "generation": claimed["job"]["generation"],
            "leaseId": claimed["lease"]["leaseId"],
        }
        _entry, source, lease = service.pc_worker_source(identity)
        self.assertEqual(source, self.vault / "A.pdf")
        self.assertEqual(lease["desiredState"], "running")

        page = {
            "schema": "reader-page-chars/1",
            "bookId": self.entry["bookId"],
            "contentSha256": self.entry["contentSha256"],
            "engine": "manga",
            "pageNumber": 1,
            "page_w": 10,
            "page_h": 20,
            "imageWidth": 100,
            "imageHeight": 200,
            "chars": [{
                "c": "A", "x0": 1, "y0": 1, "x1": 2, "y1": 2,
                "w": 1, "bk": 0, "b": 0, "line": 0,
            }],
            "furigana": [],
            "textCharCount": 1,
            "tokenized": True,
        }
        uploaded = service.upload_pc_page(1, {**identity, "page": page})
        self.assertTrue(uploaded["accepted"])
        self.assertEqual(uploaded["job"]["successfulPages"], 1)

        formula = {
            "schema": "reader-formula-regions/1",
            "bookId": self.entry["bookId"],
            "contentSha256": self.entry["contentSha256"],
            "formulas": [],
        }
        with self.assertRaises(ReaderBookOcrError) as pending_formula:
            service.upload_pc_formulas({
                **identity,
                "formula": formula,
                "formulaState": "pending",
                "formulaReason": "formula-model-unavailable",
            })
        self.assertEqual(
            pending_formula.exception.code, "worker-formula-not-publishable"
        )
        inconsistent_formula = {
            **formula,
            "formulas": [{
                "page": 1,
                "bbox": [0.1, 0.1, 0.2, 0.2],
                "latex": "x",
            }],
        }
        with self.assertRaises(ReaderBookOcrError) as inconsistent:
            service.upload_pc_formulas({
                **identity,
                "formula": inconsistent_formula,
                "formulaState": "unavailable",
                "formulaReason": "formula-model-unavailable",
            })
        self.assertEqual(
            inconsistent.exception.code, "worker-formula-inconsistent"
        )
        formula_result = service.upload_pc_formulas({
            **identity,
            "formula": formula,
            "formulaState": "unavailable",
            "formulaReason": "formula-model-unavailable",
        })
        self.assertEqual(formula_result["job"]["formulaState"], "unavailable")
        self.assertEqual(
            formula_result["job"]["formulaReason"], "formula-model-unavailable"
        )
        self.assertEqual(formula_result["job"]["formulaProgress"]["completed"], 0)
        self.assertEqual(formula_result["job"]["formulaProgress"]["unavailable"], 1)

        job_path = service._job_dir(
            self.entry["bookId"], self.entry["contentSha256"], "manga"
        ) / "job.json"
        publishable_job = json.loads(job_path.read_text("utf-8"))
        tampered_job = {**publishable_job, "formulaState": "failed"}
        job_path.write_text(json.dumps(tampered_job), "utf-8")
        with self.assertRaises(ReaderBookOcrError) as failed_completion:
            service.complete_pc_worker({**identity, "totalPages": 1})
        self.assertEqual(
            failed_completion.exception.code, "worker-formula-not-publishable"
        )
        job_path.write_text(json.dumps(publishable_job), "utf-8")

        completed = service.complete_pc_worker({
            **identity,
            "totalPages": 1,
        })
        self.assertTrue(completed["published"])
        self.assertRegex(completed["revision"], r"^ocr_[0-9a-f]{20}$")
        status = service.status(self.entry["bookId"], self.entry["contentSha256"])
        self.assertEqual(status["state"], "succeeded")
        self.assertEqual(status["executor"], "pc")
        self.assertEqual(status["formulaState"], "unavailable")
        self.assertEqual(status["formulaProgress"]["completed"], 0)
        self.assertEqual(status["formulaProgress"]["unavailable"], 1)
        self.assertIn("公式不可用", status["message"])
        manifest = service.attachment_manifest(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(manifest["revision"], completed["revision"])
        self.assertEqual(manifest["formulaReason"], "formula-model-unavailable")
        self.assertEqual(manifest["executor"], "pc")
        self.assertEqual(manifest["processingProfile"], "quality-first-v2")
        snapshot = service._published_snapshot(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(snapshot["result"]["executor"], "pc")
        self.assertEqual(
            snapshot["result"]["processingProfile"], "quality-first-v2"
        )

    def test_pc_expired_lease_is_reclaimable_and_old_upload_is_rejected(self) -> None:
        service = ReaderBookOcrService(
            self.library,
            self.base / "pc-lease-ocr",
            ROOT,
            launcher=lambda *_args: self.fail("PC executor must not spawn a Pi worker"),
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 1,
            pc_lease_seconds=60,
        )
        service.start(
            self.entry["bookId"], self.entry["contentSha256"], "vision", "pc"
        )
        capabilities = {
            "engines": ["vision"],
            "maxPdfBytes": 1024 * 1024,
            "maxPageBytes": 1024 * 1024,
            "processingProfile": "quality-first-v2",
        }
        first = service.claim_pc_worker("pc_first", capabilities)
        version_dir = service._version_dir(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        job_path = version_dir / "vision" / "job.json"
        stored = json.loads(job_path.read_text("utf-8"))
        stored["leaseExpiresAtEpochMs"] = 1
        job_path.write_text(json.dumps(stored), "utf-8")

        second = service.claim_pc_worker("pc_second", capabilities)
        self.assertIsNotNone(second)
        self.assertNotEqual(first["lease"]["leaseId"], second["lease"]["leaseId"])
        old_identity = {
            "contract": reader_book_ocr.WORKER_CONTRACT,
            "workerId": "pc_first",
            "bookId": first["job"]["bookId"],
            "contentSha256": first["job"]["contentSha256"],
            "jobId": first["job"]["jobId"],
            "generation": first["job"]["generation"],
            "leaseId": first["lease"]["leaseId"],
        }
        with self.assertRaises(ReaderBookOcrError) as stale:
            service.pc_worker_heartbeat(old_identity)
        self.assertEqual(stale.exception.code, "ocr-worker-lease-stale")
        second_identity = {
            "contract": reader_book_ocr.WORKER_CONTRACT,
            "workerId": "pc_second",
            "bookId": second["job"]["bookId"],
            "contentSha256": second["job"]["contentSha256"],
            "jobId": second["job"]["jobId"],
            "generation": second["job"]["generation"],
            "leaseId": second["lease"]["leaseId"],
        }
        requested = service.pause(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(requested["state"], "pause-requested")
        stopping = service.pc_worker_heartbeat({
            **second_identity, "state": "running", "currentPage": None,
        })
        self.assertEqual(stopping["desiredState"], "paused")
        stopped = service.pc_worker_heartbeat({
            **second_identity, "state": "paused", "currentPage": None,
        })
        self.assertEqual(stopped["job"]["state"], "paused")

    def test_source_fingerprint_ignores_windows_ctime_rounding_only(self) -> None:
        common = {
            "st_dev": 7,
            "st_ino": 11,
            "st_size": len(PDF_A),
            "st_mtime_ns": 123_456_789,
        }
        fd_stat = SimpleNamespace(**common, st_ctime_ns=800_000_000)
        path_stat = SimpleNamespace(**common, st_ctime_ns=800_001_000)
        self.assertEqual(
            self.service._source_fingerprint(fd_stat),
            self.service._source_fingerprint(path_stat),
        )

    def test_worker_errors_redact_sensitive_url_query_values(self) -> None:
        raw = (
            "fetch failed https://pc.invalid/run?token=token-secret"
            "&api_key=api-secret&key=key-secret&safe=visible"
        )
        public = reader_book_ocr._safe_public_job({"error": raw})["error"]
        stored = self.service._sanitize_worker_error(raw)
        for value in (public, stored):
            self.assertNotIn("token-secret", value)
            self.assertNotIn("api-secret", value)
            self.assertNotIn("key-secret", value)
            self.assertIn("safe=visible", value)
            self.assertGreaterEqual(value.count("<redacted>"), 3)

    def test_switching_pi_publication_to_pc_archives_only_mutable_staging(self) -> None:
        launches = []
        service = ReaderBookOcrService(
            self.library,
            self.base / "identity-ocr",
            ROOT,
            launcher=lambda job_dir, source_path, job: (
                launches.append((job_dir, source_path, dict(job)))
                or _FakeProcess(self.fake_worker_pid)
            ),
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 1,
            pc_lease_seconds=60,
        )
        pi_job, _already = service.start(
            self.entry["bookId"], self.entry["contentSha256"], "vision", "pi"
        )
        self.assertEqual(pi_job["processingProfile"], "pi-default-v1")
        job_dir = launches[0][0]
        page = {
            "schema": "reader-page-chars/1",
            "bookId": self.entry["bookId"],
            "contentSha256": self.entry["contentSha256"],
            "engine": "vision",
            "executor": "pi",
            "processingProfile": "pi-default-v1",
            "pageNumber": 1,
            "page_w": 10,
            "page_h": 20,
            "chars": [{
                "c": "P", "x0": 1, "y0": 1, "x1": 2, "y1": 2,
                "w": 1, "bk": 0, "b": 0,
            }],
            "furigana": [],
        }
        (job_dir / "pages").mkdir(parents=True, exist_ok=True)
        (job_dir / "pages" / "p000001.json").write_text(
            json.dumps(page), "utf-8"
        )
        formula_path = job_dir / "formula-source.json"
        formula_path.write_text(json.dumps({"formulas": []}), "utf-8")
        stored = json.loads((job_dir / "job.json").read_text("utf-8"))
        final_job = {
            **stored,
            "state": "succeeded",
            "totalPages": 1,
            "processedPages": 1,
            "successfulPages": 1,
            "recognizedPages": 1,
            "formulaState": "succeeded",
            "formulaReason": None,
            "formulaTotal": 0,
            "formulaRecognized": 0,
            "resultAvailable": True,
            "updatedAtEpochMs": 1,
        }
        (job_dir / "job.json").write_text(json.dumps(final_job), "utf-8")
        args = SimpleNamespace(
            book_id=self.entry["bookId"],
            content_sha256=self.entry["contentSha256"],
            engine="vision",
            max_bytes=1024 * 1024,
        )
        reader_book_ocr_worker._set_worker_identity(None, None)
        revision = _publish_release(
            args,
            job_dir,
            formula_path,
            final_job,
            source_path=self.vault / "A.pdf",
        )
        final_job["pageCharsRevision"] = revision
        (job_dir / "job.json").write_text(json.dumps(final_job), "utf-8")
        version_dir = service._version_dir(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        release_dir = version_dir / "releases" / revision
        self.assertTrue(release_dir.is_dir())
        manifest = json.loads((release_dir / "attachments.json").read_text("utf-8"))
        self.assertEqual(manifest["executor"], "pi")
        self.assertEqual(manifest["processingProfile"], "pi-default-v1")

        pc_job, already = service.start(
            self.entry["bookId"], self.entry["contentSha256"], "vision", "pc"
        )
        self.assertFalse(already)
        self.assertEqual(pc_job["executor"], "pc")
        self.assertEqual(pc_job["processingProfile"], "quality-first-v2")
        self.assertEqual(pc_job["successfulPages"], 0)
        self.assertTrue(release_dir.is_dir(), "immutable Pi release must remain")
        self.assertFalse((job_dir / "pages" / "p000001.json").exists())
        archives = list((version_dir / "staging-archive").glob("*"))
        self.assertEqual(len(archives), 1)
        self.assertTrue(archives[0].is_dir())
        claimed = service.claim_pc_worker("pc_identity", {
            "engines": ["vision"],
            "maxPdfBytes": 1024 * 1024,
            "maxPageBytes": 1024 * 1024,
            "processingProfile": "quality-first-v2",
        })
        self.assertEqual(claimed["job"]["completedPages"], [])
        self.assertEqual(
            claimed["job"]["processingProfile"], "quality-first-v2"
        )

    def test_historical_pc_v1_publication_remains_readable_and_can_restart_as_v2(self) -> None:
        service = ReaderBookOcrService(
            self.library,
            self.base / "pc-profile-upgrade-ocr",
            ROOT,
            launcher=lambda *_args: self.fail("PC executor must not spawn a Pi worker"),
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 1,
        )
        _job, _already = service.start(
            self.entry["bookId"], self.entry["contentSha256"], "manga", "pc"
        )
        version_dir = service._version_dir(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        job_dir = version_dir / "manga"
        stored = json.loads((job_dir / "job.json").read_text("utf-8"))
        historical_profile = "quality-first-v1"
        stored["processingProfile"] = historical_profile
        page = {
            "schema": "reader-page-chars/1",
            "bookId": self.entry["bookId"],
            "contentSha256": self.entry["contentSha256"],
            "engine": "manga",
            "executor": "pc",
            "processingProfile": historical_profile,
            "pageNumber": 1,
            "page_w": 10,
            "page_h": 20,
            "imageWidth": 100,
            "imageHeight": 200,
            "chars": [{
                "c": "旧", "x0": 1, "y0": 1, "x1": 2, "y1": 2,
                "w": 1, "bk": 0, "b": 0, "line": 0,
            }],
            "furigana": [],
        }
        (job_dir / "pages").mkdir(parents=True, exist_ok=True)
        (job_dir / "pages" / "p000001.json").write_text(
            json.dumps(page), "utf-8"
        )
        formula_path = job_dir / "pc-formulas.json"
        formula_path.write_text(json.dumps({"formulas": []}), "utf-8")
        final_job = {
            **stored,
            "state": "succeeded",
            "totalPages": 1,
            "processedPages": 1,
            "successfulPages": 1,
            "recognizedPages": 1,
            "formulaState": "succeeded",
            "formulaReason": None,
            "formulaTotal": 0,
            "formulaRecognized": 0,
            "resultAvailable": True,
            "updatedAtEpochMs": 1,
        }
        (job_dir / "job.json").write_text(json.dumps(final_job), "utf-8")
        args = SimpleNamespace(
            book_id=self.entry["bookId"],
            content_sha256=self.entry["contentSha256"],
            engine="manga",
            max_bytes=1024 * 1024,
        )
        reader_book_ocr_worker._set_worker_identity(None, None)
        revision = _publish_release(
            args,
            job_dir,
            formula_path,
            final_job,
            source_path=self.vault / "A.pdf",
        )

        manifest = service.attachment_manifest(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(manifest["processingProfile"], historical_profile)
        self.assertTrue((version_dir / "releases" / revision).is_dir())

        restarted, already = service.start(
            self.entry["bookId"], self.entry["contentSha256"], "manga", "pc"
        )
        self.assertFalse(already)
        self.assertEqual(restarted["executor"], "pc")
        self.assertEqual(restarted["processingProfile"], "quality-first-v2")
        self.assertEqual(restarted["state"], "queued")
        self.assertTrue((version_dir / "releases" / revision).is_dir())
        self.assertEqual(len(list((version_dir / "staging-archive").glob("*"))), 1)

    def test_version_kind_and_unknown_fields_fail_closed(self) -> None:
        with self.assertRaises(ReaderBookOcrError) as changed:
            self.service.start(self.entry["bookId"], "0" * 64, "vision")
        self.assertEqual(changed.exception.code, "book-version-changed")
        with self.assertRaises(ReaderBookOcrError) as engine:
            self.service.start(self.entry["bookId"], self.entry["contentSha256"], "other")
        self.assertEqual(engine.exception.code, "invalid-engine")
        with self.assertRaises(ReaderBookOcrError) as profile:
            self.service.start(
                self.entry["bookId"],
                self.entry["contentSha256"],
                "vision",
                "pc",
                "pi-default-v1",
            )
        self.assertEqual(profile.exception.code, "invalid-processing-profile")

        epub = self.vault / "B.epub"
        epub.write_bytes(b"not-needed-for-catalog")
        epub_entry = next(item for item in self.library.catalog() if item["kind"] == "epub")
        with self.assertRaises(ReaderBookOcrError) as unsupported:
            self.service.start(epub_entry["bookId"], epub_entry["contentSha256"], "vision")
        self.assertEqual(unsupported.exception.code, "unsupported-book-kind")

    def test_pause_resume_cancel_and_retry_are_checkpoint_actions(self) -> None:
        self.service.start(self.entry["bookId"], self.entry["contentSha256"], "vision")
        paused = self.service.pause(self.entry["bookId"], self.entry["contentSha256"])
        self.assertEqual(paused["state"], "pause-requested")
        self.assertIn("当前页可能", paused["message"])
        control = self.launches[0][0] / "control.json"
        self.assertEqual(json.loads(control.read_text("utf-8"))["desiredState"], "paused")

        # Simulate the worker acknowledging the page-boundary checkpoint.
        job_path = self.launches[0][0] / "job.json"
        stored = json.loads(job_path.read_text("utf-8"))
        stored["state"] = "paused"
        job_path.write_text(json.dumps(stored), "utf-8")
        resumed = self.service.resume(self.entry["bookId"], self.entry["contentSha256"])
        self.assertEqual(resumed["state"], "queued")
        self.assertEqual(len(self.launches), 2)

        cancelled = self.service.cancel(self.entry["bookId"], self.entry["contentSha256"])
        self.assertEqual(cancelled["state"], "cancel-requested")
        stored = json.loads(job_path.read_text("utf-8"))
        stored["state"] = "cancelled"
        job_path.write_text(json.dumps(stored), "utf-8")
        retried = self.service.retry(self.entry["bookId"], self.entry["contentSha256"])
        self.assertEqual(retried["state"], "queued")
        self.assertEqual(len(self.launches), 3)

    @unittest.skipUnless(os.name == "posix", "process-group cleanup is POSIX-only")
    def test_dead_worker_cleanup_waits_for_the_entire_generation_group(self) -> None:
        job = {
            "workerPid": 424242,
            "processGroupId": 424242,
            "processStartToken": "start-a",
            "workerGeneration": "ocrgen_a",
        }
        with (
            patch.object(reader_book_ocr, "_process_start_token", return_value=None),
            patch.object(
                self.service,
                "_process_group_alive",
                side_effect=[True, True, False],
            ),
            patch.object(reader_book_ocr.os, "killpg") as killpg,
            patch.object(reader_book_ocr.time, "sleep", return_value=None),
        ):
            self.assertTrue(self.service._terminate_worker_generation(job))
        killpg.assert_called_once_with(424242, reader_book_ocr.signal.SIGTERM)

    def test_legacy_adoption_is_previewable_copy_only_and_idempotent(self) -> None:
        project = self.base / "legacy-project"
        rel_key = hashlib.sha1(self.entry["rel"].encode("utf-8")).hexdigest()[:16]

        def layer(text: str) -> dict:
            return {
                "chars": [{
                    "c": text, "x0": 1, "y0": 2, "x1": 3, "y1": 4,
                    "sp": 0, "w": 7, "b": 0, "bk": 1,
                }],
                "page_w": 100,
                "page_h": 200,
                "furigana": [],
            }

        override = project / "state" / "pdf-page-ocr" / f"{rel_key}-p1.json"
        override.parent.mkdir(parents=True)
        override.write_text(json.dumps({
            **layer("override"),
            "contentSha256": self.entry["contentSha256"],
        }), "utf-8")
        mtime = int((self.vault / "A.pdf").stat().st_mtime)
        cache = (
            project / "state" / "pdf-char-cache"
            / f"{rel_key}-p2-{mtime}-ja.json"
        )
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps({
            **layer("cache"),
            "cver": 11,
            "sourceContentSha256": self.entry["contentSha256"],
        }), "utf-8")
        lower_priority_cache = (
            project / "state" / "pdf-char-cache"
            / f"{rel_key}-p1-{mtime}-ja.json"
        )
        lower_priority_cache.write_text(
            json.dumps({
                **layer("must-not-win"),
                "cver": 11,
                "sourceContentSha256": self.entry["contentSha256"],
            }), "utf-8"
        )
        formula_key = hashlib.sha1(
            str((self.vault / "A.pdf").resolve()).encode("utf-8")
        ).hexdigest()[:16]
        formula = project / "state" / "pdf-figures" / f"{formula_key}.json"
        formula.parent.mkdir(parents=True)
        formula.write_text('{"formulas": []}\n216\n}', "utf-8")
        old_bytes = {
            override: override.read_bytes(),
            cache: cache.read_bytes(),
            lower_priority_cache: lower_priority_cache.read_bytes(),
            formula: formula.read_bytes(),
            self.vault / "A.pdf": (self.vault / "A.pdf").read_bytes(),
        }
        state_root = self.base / "adopted-ocr"
        service = ReaderBookOcrService(
            self.library,
            state_root,
            project,
            launcher=lambda *_args: self.fail("adoption must not launch OCR"),
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 3,
            legacy_embedded_page_reader=(
                lambda _path, _rel, page: layer("embedded") if page == 3 else None
            ),
            legacy_language_resolver=lambda _rel: "ja",
            legacy_char_cache_version=11,
        )

        preview = service.preview_adoption(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertFalse(state_root.exists())
        self.assertTrue(preview["available"])
        self.assertEqual(
            preview["pageSources"],
            {"override": 1, "char-cache": 1, "embedded": 1, "missing": 0},
        )
        self.assertEqual(preview["formula"]["state"], "pending")
        self.assertEqual(
            preview["formula"]["reason"], "legacy-formulas-invalid-json"
        )
        self.assertNotIn(str(self.vault), json.dumps(preview))

        job, adoption, already = service.adopt_legacy(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertFalse(already)
        self.assertEqual(job["state"], "succeeded")
        self.assertEqual(job["engine"], "legacy")
        self.assertEqual(job["formulaState"], "pending")
        self.assertTrue(adoption["revision"].startswith("ocr_"))
        self.assertEqual(service.read_page(
            self.entry["bookId"], self.entry["contentSha256"], 1
        )[0]["legacySource"], "override")
        self.assertEqual(service.read_page(
            self.entry["bookId"], self.entry["contentSha256"], 2
        )[0]["legacySource"], "char-cache")
        self.assertEqual(service.read_page(
            self.entry["bookId"], self.entry["contentSha256"], 3
        )[0]["legacySource"], "embedded")
        self.assertEqual(service.read_formulas(
            self.entry["bookId"], self.entry["contentSha256"]
        ), [])
        manifest = service.attachment_manifest(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(manifest["formulaState"], "pending")
        self.assertEqual(manifest["formulaReason"], "legacy-formulas-invalid-json")
        for path, original in old_bytes.items():
            self.assertEqual(path.read_bytes(), original)

        repeated_job, repeated, already = service.adopt_legacy(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertTrue(already)
        self.assertEqual(repeated_job["state"], "succeeded")
        self.assertEqual(repeated["revision"], adoption["revision"])

    def test_legacy_adoption_fails_closed_when_a_page_is_missing(self) -> None:
        service = ReaderBookOcrService(
            self.library,
            self.base / "missing-ocr",
            self.base / "missing-project",
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 2,
            legacy_embedded_page_reader=lambda _path, _rel, page: (
                {
                    "chars": [{
                        "c": "A", "x0": 1, "y0": 1, "x1": 2, "y1": 2,
                    }],
                    "page_w": 10,
                    "page_h": 10,
                    "furigana": [],
                }
                if page == 1 else None
            ),
        )
        preview = service.preview_adoption(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertFalse(preview["available"])
        self.assertEqual(preview["missingPages"], [2])
        with self.assertRaises(ReaderBookOcrError) as missing:
            service.adopt_legacy(self.entry["bookId"], self.entry["contentSha256"])
        self.assertEqual(missing.exception.code, "legacy-result-incomplete")
        self.assertFalse(any((self.base / "missing-ocr").rglob("publication.json")))

    def test_legacy_adoption_hashes_actual_pdf_not_only_catalog_metadata(self) -> None:
        path = self.vault / "A.pdf"
        path.write_bytes(PDF_A + b"changed")

        class StaleCatalog:
            def resolve(_self, _book_id):
                return dict(self.entry), path

        service = ReaderBookOcrService(
            StaleCatalog(),
            self.base / "stale-ocr",
            self.base / "stale-project",
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 1,
        )
        with self.assertRaises(ReaderBookOcrError) as changed:
            service.preview_adoption(
                self.entry["bookId"], self.entry["contentSha256"]
            )
        self.assertEqual(changed.exception.code, "book-version-changed")
        self.assertFalse((self.base / "stale-ocr").exists())

    def test_legacy_sources_require_full_sha_and_exact_language(self) -> None:
        project = self.base / "provenance-project"
        rel_key = hashlib.sha1(self.entry["rel"].encode("utf-8")).hexdigest()[:16]
        mtime = int((self.vault / "A.pdf").stat().st_mtime)

        def layer(text: str) -> dict:
            return {
                "chars": [{"c": text, "x0": 1, "y0": 1, "x1": 2, "y1": 2}],
                "page_w": 10, "page_h": 20, "furigana": [],
            }

        override = project / "state" / "pdf-page-ocr" / f"{rel_key}-p1.json"
        override.parent.mkdir(parents=True)
        override.write_text(json.dumps(layer("unbound-override")), "utf-8")
        cache_dir = project / "state" / "pdf-char-cache"
        cache_dir.mkdir(parents=True)
        (cache_dir / f"{rel_key}-p1-{mtime}-zh.json").write_text(json.dumps({
            **layer("wrong-language"),
            "cver": 11,
            "sourceContentSha256": self.entry["contentSha256"],
        }), "utf-8")
        (cache_dir / f"{rel_key}-p1-{mtime}-ja.json").write_text(json.dumps({
            **layer("unbound-cache"), "cver": 11,
        }), "utf-8")
        formula_key = hashlib.sha1(
            str((self.vault / "A.pdf").resolve()).encode("utf-8")
        ).hexdigest()[:16]
        formula = project / "state" / "pdf-figures" / f"{formula_key}.json"
        formula.parent.mkdir(parents=True)
        formula.write_text(json.dumps({
            "pdf": str((self.vault / "A.pdf").resolve()),
            "book_mtime": mtime,
            "formulas": [{"page": 1, "bbox": [0, 0, 1, 1], "latex": "x"}],
        }), "utf-8")
        embedded_calls = []
        service = ReaderBookOcrService(
            self.library,
            self.base / "provenance-ocr",
            project,
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 1,
            legacy_embedded_page_reader=lambda *_args: (
                embedded_calls.append(_args) or layer("embedded")
            ),
            legacy_language_resolver=lambda _rel: "ja",
            legacy_char_cache_version=11,
        )
        preview = service.preview_adoption(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(
            preview["pageSources"],
            {"override": 0, "char-cache": 0, "embedded": 1, "missing": 0},
        )
        self.assertEqual(preview["formula"], {
            "state": "pending", "count": 0, "reason": "legacy-formulas-unbound",
        })
        self.assertEqual(len(embedded_calls), 1)

    def test_adoption_fence_failure_has_zero_visibility_and_retry_ignores_residue(self) -> None:
        state_root = self.base / "fence-ocr"
        service = ReaderBookOcrService(
            self.library,
            state_root,
            self.base / "fence-project",
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 1,
            legacy_embedded_page_reader=lambda *_args: {
                "chars": [{"c": "A", "x0": 1, "y0": 1, "x1": 2, "y1": 2}],
                "page_w": 10, "page_h": 20, "furigana": [],
            },
        )
        real_atomic_write = reader_book_ocr.atomic_write_json

        def fail_publication(path, *args, **kwargs):
            if Path(path).name == "publication.json":
                raise OSError("fault before publication fence")
            return real_atomic_write(path, *args, **kwargs)

        with patch.object(reader_book_ocr, "atomic_write_json", side_effect=fail_publication):
            with self.assertRaises(OSError):
                service.adopt_legacy(
                    self.entry["bookId"], self.entry["contentSha256"]
                )
        version = service._version_dir(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertFalse((version / "publication.json").exists())
        with self.assertRaises(ReaderBookOcrError) as status_error:
            service.status(self.entry["bookId"], self.entry["contentSha256"])
        self.assertEqual(status_error.exception.code, "ocr-publication-incomplete")
        with self.assertRaises(ReaderBookOcrError) as page_error:
            service.read_page(self.entry["bookId"], self.entry["contentSha256"], 1)
        self.assertEqual(page_error.exception.code, "ocr-result-not-found")
        with self.assertRaises(ReaderBookOcrError) as manifest_error:
            service.attachment_manifest(
                self.entry["bookId"], self.entry["contentSha256"]
            )
        self.assertEqual(manifest_error.exception.code, "ocr-attachments-not-found")

        residue = state_root / ".adopt-staging-residue" / "pages"
        residue.mkdir(parents=True)
        (residue / "p999999.json").write_text("{}", "utf-8")
        job, adoption, already = service.adopt_legacy(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertFalse(already)
        self.assertTrue(adoption["alreadyAdopted"])
        self.assertEqual(job["state"], "succeeded")
        manifest = service.attachment_manifest(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(
            [item["attachmentId"] for item in manifest["files"]],
            ["ocr-page-000001", "ocr-formulas"],
        )

    def test_adoption_source_replaced_during_fence_write_is_rolled_back(self) -> None:
        state_root = self.base / "source-swap-adoption-ocr"
        service = ReaderBookOcrService(
            self.library,
            state_root,
            self.base / "source-swap-project",
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 1,
            legacy_embedded_page_reader=lambda *_args: {
                "chars": [{"c": "A", "x0": 1, "y0": 1, "x1": 2, "y1": 2}],
                "page_w": 10, "page_h": 20, "furigana": [],
            },
        )
        source = self.vault / "A.pdf"
        version = service._version_dir(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        fence_path = version / "publication.json"
        real_atomic_write = reader_book_ocr.atomic_write_json
        swapped = False

        def replace_source_at_fence(path, *args, **kwargs):
            nonlocal swapped
            if Path(path) == fence_path and not swapped:
                swapped = True
                replacement = self.vault / "replacement.pdf"
                replacement.write_bytes(PDF_A + b"different")
                if os.name == "nt":
                    # Windows denies replacing a path whose source guard is
                    # open; an in-place mutation exercises the same post-fence
                    # check.  POSIX exercises the original atomic-replace race.
                    source.write_bytes(replacement.read_bytes())
                    replacement.unlink()
                else:
                    os.replace(replacement, source)
            return real_atomic_write(path, *args, **kwargs)

        with patch.object(
            reader_book_ocr, "atomic_write_json", side_effect=replace_source_at_fence
        ):
            with self.assertRaises(ReaderBookOcrError) as changed:
                service.adopt_legacy(
                    self.entry["bookId"], self.entry["contentSha256"]
                )
        self.assertTrue(swapped)
        self.assertEqual(changed.exception.code, "book-version-changed")
        self.assertFalse(fence_path.exists())

    def test_adoption_same_metadata_source_change_at_fence_is_rolled_back(self) -> None:
        state_root = self.base / "same-metadata-adoption-ocr"
        service = ReaderBookOcrService(
            self.library,
            state_root,
            self.base / "same-metadata-adoption-project",
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 1,
            legacy_embedded_page_reader=lambda *_args: {
                "chars": [{"c": "A", "x0": 1, "y0": 1, "x1": 2, "y1": 2}],
                "page_w": 10, "page_h": 20, "furigana": [],
            },
        )
        source = self.vault / "A.pdf"
        original_stat = source.stat()
        original_identity = service._source_identity(original_stat)
        changed_bytes = bytearray(PDF_A)
        changed_bytes[-1] ^= 1
        version = service._version_dir(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        fence_path = version / "publication.json"
        real_atomic_write = reader_book_ocr.atomic_write_json
        swapped = False

        def change_source_at_fence(path, *args, **kwargs):
            nonlocal swapped
            if Path(path) == fence_path and not swapped:
                swapped = True
                source.write_bytes(bytes(changed_bytes))
                os.utime(
                    source,
                    ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                )
                self.assertEqual(
                    service._source_identity(source.stat()), original_identity
                )
            return real_atomic_write(path, *args, **kwargs)

        with patch.object(
            reader_book_ocr, "atomic_write_json", side_effect=change_source_at_fence
        ):
            with self.assertRaises(ReaderBookOcrError) as changed:
                service.adopt_legacy(
                    self.entry["bookId"], self.entry["contentSha256"]
                )
        self.assertTrue(swapped)
        self.assertEqual(changed.exception.code, "book-version-changed")
        self.assertNotEqual(
            hashlib.sha256(source.read_bytes()).hexdigest(),
            self.entry["contentSha256"],
        )
        self.assertFalse(fence_path.exists())

    def test_same_byte_atomic_source_replacement_keeps_old_revision_readable(self) -> None:
        service = ReaderBookOcrService(
            self.library,
            self.base / "same-byte-ocr",
            self.base / "same-byte-project",
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 1,
            legacy_embedded_page_reader=lambda *_args: {
                "chars": [{"c": "A", "x0": 1, "y0": 1, "x1": 2, "y1": 2}],
                "page_w": 10, "page_h": 20, "furigana": [],
            },
        )
        _job, adoption, _already = service.adopt_legacy(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        source = self.vault / "A.pdf"
        replacement = self.vault / "same-bytes.pdf"
        replacement.write_bytes(PDF_A)
        os.replace(replacement, source)

        entry, path, manifest = service.read_attachment(
            self.entry["bookId"],
            self.entry["contentSha256"],
            "ocr-page-000001",
            expected_revision=adoption["revision"],
        )
        self.assertEqual(manifest["revision"], adoption["revision"])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), entry["sha256"])

    def test_non_adoption_publication_is_not_reported_as_adopted_and_formula_failed_stays_failed(self) -> None:
        state_root = self.base / "normal-publication-ocr"
        launches = []
        service = ReaderBookOcrService(
            self.library,
            state_root,
            self.base / "normal-project",
            launcher=lambda *args: (
                launches.append(args) or _FakeProcess(self.fake_worker_pid)
            ),
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 1,
            legacy_embedded_page_reader=lambda *_args: {
                "chars": [{"c": "E", "x0": 1, "y0": 1, "x1": 2, "y1": 2}],
                "page_w": 10, "page_h": 20, "furigana": [],
            },
        )
        version = service._version_dir(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        job_dir = version / "vision"
        pages = job_dir / "pages"
        pages.mkdir(parents=True)
        (pages / "p000001.json").write_text(json.dumps({
            "schema": "reader-page-chars/1",
            "bookId": self.entry["bookId"],
            "contentSha256": self.entry["contentSha256"],
            "engine": "vision",
            "pageNumber": 1,
            "page_w": 10,
            "page_h": 20,
            "chars": [{"c": "V", "x0": 1, "y0": 1, "x1": 2, "y1": 2}],
            "furigana": [],
        }), "utf-8")
        formula_path = job_dir / "formula-source.json"
        formula_path.write_text('{"formulas":[]}', "utf-8")
        final_job = {
            "contract": "reader-library-ocr/1",
            "jobId": "ocrjob_normal",
            "bookId": self.entry["bookId"],
            "contentSha256": self.entry["contentSha256"],
            "engine": "vision",
            "state": "succeeded",
            "totalPages": 1,
            "successfulPages": 1,
            "formulaState": "failed",
            "formulaTotal": 0,
            "resultAvailable": True,
            "updatedAtEpochMs": 1,
        }
        (job_dir / "job.json").write_text(json.dumps(final_job), "utf-8")
        vision_revision = _publish_release(
            SimpleNamespace(
                book_id=self.entry["bookId"],
                content_sha256=self.entry["contentSha256"],
                engine="vision",
                max_bytes=1024 * 1024,
            ),
            job_dir,
            formula_path,
            final_job,
            source_path=self.vault / "A.pdf",
        )
        preview = service.preview_adoption(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertFalse(preview["alreadyAdopted"])
        self.assertEqual(preview["pageSources"]["embedded"], 1)
        published_status = service.status(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(published_status["engine"], "vision")
        self.assertEqual(published_status["formulaState"], "failed")
        old_entry, old_path, _old_manifest = service.read_attachment(
            self.entry["bookId"],
            self.entry["contentSha256"],
            "ocr-page-000001",
            expected_revision=vision_revision,
        )
        old_payload = old_path.read_bytes()

        (job_dir / "job.json").write_text(json.dumps({
            **final_job,
            "jobId": "ocrjob_killed_before_fence",
            "state": "running",
            "phase": "finalizing",
            "pid": 999999999,
            "resultAvailable": False,
            "pageCharsRevision": None,
        }), "utf-8")
        killed = service.status(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(killed["state"], "failed")
        self.assertEqual(killed["errorCode"], "worker-stopped")
        self.assertTrue(killed["canRetry"])

        (job_dir / "job.json").write_text(json.dumps({
            **final_job,
            "jobId": "ocrjob_unpublished_same_engine",
            "pageCharsRevision": None,
        }), "utf-8")
        mismatched = service.status(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(mismatched["state"], "failed")
        self.assertEqual(mismatched["errorCode"], "ocr-publication-incomplete")
        restored, already = service.start(
            self.entry["bookId"], self.entry["contentSha256"], "vision"
        )
        self.assertTrue(already)
        self.assertEqual(restored["jobId"], final_job["jobId"])

        manga_dir = version / "manga"
        manga_dir.mkdir(parents=True)
        (manga_dir / "job.json").write_text(json.dumps({
            **final_job,
            "jobId": "ocrjob_stale_manga",
            "engine": "manga",
        }), "utf-8")
        switched, already = service.start(
            self.entry["bookId"], self.entry["contentSha256"], "manga"
        )
        self.assertFalse(already)
        self.assertEqual(switched["state"], "queued")
        self.assertEqual(switched["engine"], "manga")
        self.assertEqual(len(launches), 1)
        self.assertEqual(
            service.attachment_manifest(
                self.entry["bookId"], self.entry["contentSha256"]
            )["engine"],
            "vision",
        )
        self.assertEqual(
            service.read_page(
                self.entry["bookId"], self.entry["contentSha256"], 1
            )[0]["engine"],
            "vision",
        )

        manga_pages = manga_dir / "pages"
        manga_pages.mkdir()
        (manga_pages / "p000001.json").write_text(json.dumps({
            "schema": "reader-page-chars/1",
            "bookId": self.entry["bookId"],
            "contentSha256": self.entry["contentSha256"],
            "engine": "manga",
            "pageNumber": 1,
            "page_w": 10,
            "page_h": 20,
            "chars": [{"c": "M", "x0": 1, "y0": 1, "x1": 2, "y1": 2}],
            "furigana": [],
        }), "utf-8")
        manga_formula = manga_dir / "formula-source.json"
        manga_formula.write_text('{"formulas":[]}', "utf-8")
        manga_final = {
            **switched,
            "state": "succeeded",
            "totalPages": 1,
            "successfulPages": 1,
            "formulaState": "failed",
            "formulaTotal": 0,
            "resultAvailable": True,
        }
        manga_revision = _publish_release(
            SimpleNamespace(
                book_id=self.entry["bookId"],
                content_sha256=self.entry["contentSha256"],
                engine="manga",
                max_bytes=1024 * 1024,
            ),
            manga_dir,
            manga_formula,
            manga_final,
            source_path=self.vault / "A.pdf",
        )
        self.assertNotEqual(manga_revision, vision_revision)
        self.assertEqual(
            service.attachment_manifest(
                self.entry["bookId"], self.entry["contentSha256"]
            )["engine"],
            "manga",
        )
        retained_entry, retained_path, retained_manifest = service.read_attachment(
            self.entry["bookId"],
            self.entry["contentSha256"],
            "ocr-page-000001",
            expected_revision=vision_revision,
        )
        self.assertEqual(retained_manifest["revision"], vision_revision)
        self.assertEqual(retained_entry["sha256"], old_entry["sha256"])
        self.assertEqual(retained_path.read_bytes(), old_payload)

    def test_partial_release_manifest_is_rejected_even_with_updated_fence_digest(self) -> None:
        service = ReaderBookOcrService(
            self.library,
            self.base / "partial-ocr",
            self.base / "partial-project",
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 2,
            legacy_embedded_page_reader=lambda _path, _rel, page: {
                "chars": [{"c": str(page), "x0": 1, "y0": 1, "x1": 2, "y1": 2}],
                "page_w": 10, "page_h": 20, "furigana": [],
            },
        )
        _job, adoption, _already = service.adopt_legacy(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        version = service._version_dir(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        release = version / "legacy" / "releases" / adoption["revision"]
        manifest_path = release / "attachments.json"
        manifest = json.loads(manifest_path.read_text("utf-8"))
        manifest["files"] = [
            item for item in manifest["files"]
            if item["attachmentId"] != "ocr-page-000002"
        ]
        manifest_path.write_text(json.dumps(manifest), "utf-8")
        fence_path = version / "publication.json"
        fence = json.loads(fence_path.read_text("utf-8"))
        fence["manifestSha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        fence_path.write_text(json.dumps(fence), "utf-8")
        with self.assertRaises(ReaderBookOcrError) as invalid:
            service.attachment_manifest(
                self.entry["bookId"], self.entry["contentSha256"]
            )
        self.assertEqual(invalid.exception.code, "ocr-publication-invalid")

    def test_attachment_manifest_is_immutable_and_path_whitelisted(self) -> None:
        project = self.base / "manifest-project"
        formula_key = hashlib.sha1(
            str((self.vault / "A.pdf").resolve()).encode("utf-8")
        ).hexdigest()[:16]
        formula_source = project / "state" / "pdf-figures" / f"{formula_key}.json"
        formula_source.parent.mkdir(parents=True)
        formula_source.write_text(json.dumps({
            "sourceContentSha256": self.entry["contentSha256"],
            "pdf": str((self.vault / "A.pdf").resolve()),
            "book_mtime": int((self.vault / "A.pdf").stat().st_mtime),
            "formulas": [{"page": 1, "bbox": [0, 0, 0.5, 0.5], "latex": "x"}],
        }), "utf-8")
        service = ReaderBookOcrService(
            self.library,
            self.base / "manifest-ocr",
            project,
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
            legacy_page_count_reader=lambda _path: 1,
            legacy_embedded_page_reader=lambda *_args: {
                "chars": [{"c": "A", "x0": 1, "y0": 1, "x1": 2, "y1": 2}],
                "page_w": 10, "page_h": 20, "furigana": [],
            },
        )
        _job, adoption, _already = service.adopt_legacy(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        loaded = service.attachment_manifest(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(loaded["category"], "derived")
        self.assertEqual(loaded["revision"], adoption["revision"])
        self.assertEqual(service.read_formulas(
            self.entry["bookId"], self.entry["contentSha256"]
        )[0]["latex"], "x")
        entry, resolved, manifest = service.read_attachment(
            self.entry["bookId"], self.entry["contentSha256"], "ocr-page-000001"
        )
        self.assertEqual(entry["sha256"], hashlib.sha256(resolved.read_bytes()).hexdigest())
        with self.assertRaises(ReaderBookOcrError):
            service.read_attachment(
                self.entry["bookId"], self.entry["contentSha256"], "../../A.pdf"
            )

        manifest["files"][0]["downloadUrl"] = "../../A.pdf"
        resolved.parent.parent.joinpath("attachments.json").write_text(
            json.dumps(manifest), "utf-8"
        )
        with self.assertRaises(ReaderBookOcrError) as invalid_manifest:
            service.attachment_manifest(
                self.entry["bookId"], self.entry["contentSha256"]
            )
        self.assertEqual(invalid_manifest.exception.code, "ocr-publication-invalid")


class ReaderBookOcrWorkerContractTest(unittest.TestCase):
    def test_manga_line_geometry_follows_vertical_japanese_writing(self) -> None:
        boxes = _manga_line_char_boxes(
            "取り寄せ",
            [[100, 20], [120, 20], [120, 180], [100, 180]],
            vertical=True,
        )
        self.assertEqual([item[0] for item in boxes], list("取り寄せ"))
        self.assertTrue(all(item[1] == 100 and item[3] == 120 for item in boxes))
        self.assertEqual([item[2] for item in boxes], [20, 60, 100, 140])
        self.assertEqual([item[4] for item in boxes], [60, 100, 140, 180])

    def test_manga_line_geometry_keeps_horizontal_text_on_x_axis(self) -> None:
        boxes = _manga_line_char_boxes(
            "HTTP",
            [[10, 30], [210, 30], [210, 50], [10, 50]],
            vertical=False,
        )
        self.assertEqual([item[1] for item in boxes], [10, 60, 110, 160])
        self.assertTrue(all(item[2] == 30 and item[4] == 50 for item in boxes))

    def test_manga_line_geometry_uses_authoritative_direction_for_square_box(self) -> None:
        boxes = _manga_line_char_boxes(
            "縦書",
            [[10, 10], [50, 10], [50, 50], [10, 50]],
            vertical=True,
        )
        self.assertEqual([(item[2], item[4]) for item in boxes], [(10, 30), (30, 50)])
        self.assertTrue(all(item[1] == 10 and item[3] == 50 for item in boxes))

    def test_manga_line_geometry_follows_skewed_polygon(self) -> None:
        boxes = _manga_line_char_boxes(
            "AB",
            [[10, 10], [50, 20], [46, 40], [6, 30]],
            vertical=False,
        )
        self.assertEqual(boxes[0][1:], ("A", 6.0, 10.0, 30.0, 35.0)[1:])
        self.assertEqual(boxes[1][1:], (26.0, 15.0, 50.0, 40.0))

    def test_manga_line_geometry_never_inverts_dense_character_boxes(self) -> None:
        boxes = _manga_line_char_boxes(
            "1234567890",
            [[0, 0], [2, 0], [2, 1], [0, 1]],
            vertical=False,
        )
        self.assertEqual(len(boxes), 10)
        self.assertTrue(all(item[1] < item[3] and item[2] < item[4] for item in boxes))

    def test_non_japanese_tokenization_uses_real_boundaries(self) -> None:
        chars = [
            {"c": "A", "w": -1, "bk": 0, "line": 0},
            {"c": "B", "w": -1, "bk": 0, "line": 0},
            {"c": "中", "w": -1, "bk": 0, "line": 0},
            {"c": "文", "w": -1, "bk": 0, "line": 0},
        ]
        out = _tokenize_chars(chars)
        self.assertEqual(out[0]["w"], out[1]["w"])
        self.assertNotEqual(out[1]["w"], out[2]["w"])
        self.assertNotEqual(out[2]["w"], out[3]["w"])

    def test_published_manifest_lists_only_derived_immutable_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            version = Path(temp) / ("a" * 64)
            job_dir = version / "vision"
            pages = job_dir / "pages"
            pages.mkdir(parents=True)
            (pages / "p000001.json").write_text("{}", "utf-8")
            formula = Path(temp) / "global-formulas.json"
            formula.write_text(json.dumps({"formulas": [{
                "page": 1,
                "bbox": [0, 0, 1, 1],
                "latex": "x",
                "multiline": True,
            }]}), "utf-8")
            args = SimpleNamespace(
                book_id="book_" + "a" * 32,
                content_sha256="b" * 64,
            )
            revision, manifest = _publish_attachments(args, job_dir, formula)
            self.assertTrue(revision.startswith("ocr_"))
            self.assertEqual(manifest["category"], "derived")
            self.assertEqual(manifest["mergePolicy"], "immutable")
            self.assertEqual({item["category"] for item in manifest["files"]}, {"derived"})
            self.assertTrue(all("revision=" + revision in item["downloadUrl"] for item in manifest["files"]))
            self.assertNotIn(temp, json.dumps(manifest))
            exported = json.loads((version / "formulas.json").read_text("utf-8"))
            self.assertTrue(exported["formulas"][0]["multiline"])

    def test_revision_addresses_provenance_and_formula_reason(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            job_dir = base / "job"
            pages = job_dir / "pages"
            pages.mkdir(parents=True)
            (pages / "p000001.json").write_text("{}", "utf-8")
            args = SimpleNamespace(
                book_id="book_" + "a" * 32,
                content_sha256="b" * 64,
            )
            common = {
                "adoptionContract": "reader-library-ocr-adoption/1",
                "source": "legacy-sidecars",
                "formulaState": "pending",
                "formulaCount": 0,
                "engine": "legacy",
                "totalPages": 1,
            }
            first_revision, _ = _publish_attachments(
                args,
                job_dir,
                formula_records=[],
                manifest_metadata={
                    **common,
                    "pageSources": {"embedded": 1},
                    "formulaReason": "legacy-formulas-missing",
                },
                publish_manifest=False,
                output_dir=base / "first",
                generated_at_epoch_ms=0,
            )
            second_revision, _ = _publish_attachments(
                args,
                job_dir,
                formula_records=[],
                manifest_metadata={
                    **common,
                    "pageSources": {"override": 1},
                    "formulaReason": "legacy-formulas-unbound",
                },
                publish_manifest=False,
                output_dir=base / "second",
                generated_at_epoch_ms=0,
            )
            self.assertNotEqual(first_revision, second_revision)

    def test_stale_worker_generation_cannot_update_the_replacement_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            job_path = Path(temp) / "job.json"
            job_path.write_text(json.dumps({
                "jobId": "ocrjob_new",
                "workerGeneration": "ocrgen_new",
                "state": "queued",
            }), "utf-8")
            reader_book_ocr_worker._set_worker_identity(
                "ocrjob_old", "ocrgen_old"
            )
            try:
                with self.assertRaisesRegex(RuntimeError, "no longer current"):
                    reader_book_ocr_worker._update_job(job_path, state="running")
            finally:
                reader_book_ocr_worker._set_worker_identity(None, None)
            self.assertEqual(
                json.loads(job_path.read_text("utf-8"))["state"], "queued"
            )

    def test_controlled_child_termination_does_not_treat_child_pid_as_pgid(self) -> None:
        class Child:
            pid = 987654

            def __init__(self):
                self.terminated = False
                self.waited = False

            def poll(self):
                return None

            def terminate(self):
                self.terminated = True

            def wait(self, timeout):
                self.waited = timeout == 8
                return 0

            def kill(self):
                raise AssertionError("graceful termination should have succeeded")

        child = Child()
        reader_book_ocr_worker._terminate(child)
        self.assertTrue(child.terminated)
        self.assertTrue(child.waited)

    def test_release_rejects_wrong_page_formula_range_and_changed_source_before_fence(self) -> None:
        for case in ("wrong-engine", "formula-out-of-range", "source-changed"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temp:
                base = Path(temp)
                source = base / "source.pdf"
                source.write_bytes(PDF_A)
                content_sha = hashlib.sha256(PDF_A).hexdigest()
                job_dir = base / "state" / "vision"
                pages = job_dir / "pages"
                pages.mkdir(parents=True)
                page_engine = "manga" if case == "wrong-engine" else "vision"
                (pages / "p000001.json").write_text(json.dumps({
                    "schema": "reader-page-chars/1",
                    "bookId": "book_" + "a" * 32,
                    "contentSha256": content_sha,
                    "engine": page_engine,
                    "pageNumber": 1,
                    "page_w": 10,
                    "page_h": 20,
                    "chars": [],
                    "furigana": [],
                }), "utf-8")
                formula = job_dir / "formula.json"
                formula_records = (
                    [{"page": 2, "bbox": [0, 0, 1, 1], "latex": "x"}]
                    if case == "formula-out-of-range" else []
                )
                formula.write_text(json.dumps({"formulas": formula_records}), "utf-8")
                final_job = {
                    "contract": "reader-library-ocr/1",
                    "jobId": "ocrjob_test",
                    "bookId": "book_" + "a" * 32,
                    "contentSha256": content_sha,
                    "engine": "vision",
                    "state": "succeeded",
                    "totalPages": 1,
                    "successfulPages": 1,
                    "formulaState": "succeeded",
                    "formulaTotal": len(formula_records),
                    "resultAvailable": True,
                }
                args = SimpleNamespace(
                    book_id="book_" + "a" * 32,
                    content_sha256=content_sha,
                    engine="vision",
                    max_bytes=1024 * 1024,
                )
                if case == "source-changed":
                    source.write_bytes(PDF_A + b"changed")
                with self.assertRaises(RuntimeError):
                    _publish_release(
                        args,
                        job_dir,
                        formula,
                        final_job,
                        source_path=source,
                    )
                self.assertFalse((job_dir.parent / "publication.json").exists())

    def test_worker_source_replaced_during_fence_write_is_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source.pdf"
            source.write_bytes(PDF_A)
            content_sha = hashlib.sha256(PDF_A).hexdigest()
            job_dir = base / "state" / "vision"
            pages = job_dir / "pages"
            pages.mkdir(parents=True)
            book_id = "book_" + "a" * 32
            (pages / "p000001.json").write_text(json.dumps({
                "schema": "reader-page-chars/1",
                "bookId": book_id,
                "contentSha256": content_sha,
                "engine": "vision",
                "pageNumber": 1,
                "page_w": 10,
                "page_h": 20,
                "chars": [],
                "furigana": [],
            }), "utf-8")
            formula = job_dir / "formula.json"
            formula.write_text('{"formulas":[]}', "utf-8")
            final_job = {
                "contract": "reader-library-ocr/1",
                "jobId": "ocrjob_test",
                "bookId": book_id,
                "contentSha256": content_sha,
                "engine": "vision",
                "state": "succeeded",
                "totalPages": 1,
                "successfulPages": 1,
                "formulaState": "succeeded",
                "formulaTotal": 0,
                "resultAvailable": True,
            }
            (job_dir / "job.json").write_text(json.dumps(final_job), "utf-8")
            args = SimpleNamespace(
                book_id=book_id,
                content_sha256=content_sha,
                engine="vision",
                max_bytes=1024 * 1024,
            )
            fence_path = job_dir.parent / "publication.json"
            real_atomic = reader_book_ocr_worker._atomic_json
            swapped = False

            def replace_source_at_fence(path, value):
                nonlocal swapped
                if Path(path) == fence_path and not swapped:
                    swapped = True
                    replacement = base / "replacement.pdf"
                    replacement.write_bytes(PDF_A + b"different")
                    if os.name == "nt":
                        source.write_bytes(replacement.read_bytes())
                        replacement.unlink()
                    else:
                        os.replace(replacement, source)
                return real_atomic(path, value)

            with patch.object(
                reader_book_ocr_worker,
                "_atomic_json",
                side_effect=replace_source_at_fence,
            ):
                with self.assertRaisesRegex(RuntimeError, "changed before"):
                    _publish_release(
                        args,
                        job_dir,
                        formula,
                        final_job,
                        source_path=source,
                    )
            self.assertTrue(swapped)
            self.assertFalse(fence_path.exists())

    def test_worker_same_metadata_source_change_at_fence_is_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source.pdf"
            source.write_bytes(PDF_A)
            original_stat = source.stat()
            original_identity = reader_book_ocr_worker._source_identity(original_stat)
            changed_bytes = bytearray(PDF_A)
            changed_bytes[-1] ^= 1
            content_sha = hashlib.sha256(PDF_A).hexdigest()
            job_dir = base / "state" / "vision"
            pages = job_dir / "pages"
            pages.mkdir(parents=True)
            book_id = "book_" + "a" * 32
            (pages / "p000001.json").write_text(json.dumps({
                "schema": "reader-page-chars/1",
                "bookId": book_id,
                "contentSha256": content_sha,
                "engine": "vision",
                "pageNumber": 1,
                "page_w": 10,
                "page_h": 20,
                "chars": [],
                "furigana": [],
            }), "utf-8")
            formula = job_dir / "formula.json"
            formula.write_text('{"formulas":[]}', "utf-8")
            final_job = {
                "contract": "reader-library-ocr/1",
                "jobId": "ocrjob_test",
                "workerGeneration": "ocrgen_test",
                "bookId": book_id,
                "contentSha256": content_sha,
                "engine": "vision",
                "state": "succeeded",
                "totalPages": 1,
                "successfulPages": 1,
                "formulaState": "succeeded",
                "formulaTotal": 0,
                "resultAvailable": True,
            }
            (job_dir / "job.json").write_text(json.dumps(final_job), "utf-8")
            args = SimpleNamespace(
                book_id=book_id,
                content_sha256=content_sha,
                engine="vision",
                max_bytes=1024 * 1024,
            )
            fence_path = job_dir.parent / "publication.json"
            real_atomic = reader_book_ocr_worker._atomic_json
            swapped = False

            def change_source_at_fence(path, value):
                nonlocal swapped
                if Path(path) == fence_path and not swapped:
                    swapped = True
                    source.write_bytes(bytes(changed_bytes))
                    os.utime(
                        source,
                        ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
                    )
                    self.assertEqual(
                        reader_book_ocr_worker._source_identity(source.stat()),
                        original_identity,
                    )
                return real_atomic(path, value)

            reader_book_ocr_worker._set_worker_identity(
                final_job["jobId"], final_job["workerGeneration"]
            )
            try:
                with patch.object(
                    reader_book_ocr_worker,
                    "_atomic_json",
                    side_effect=change_source_at_fence,
                ):
                    with self.assertRaisesRegex(RuntimeError, "changed before"):
                        _publish_release(
                            args,
                            job_dir,
                            formula,
                            final_job,
                            source_path=source,
                        )
            finally:
                reader_book_ocr_worker._set_worker_identity(None, None)
            self.assertTrue(swapped)
            self.assertNotEqual(hashlib.sha256(source.read_bytes()).hexdigest(), content_sha)
            self.assertFalse(fence_path.exists())

    def test_stale_worker_generation_at_fence_restores_previous_publication(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            source = base / "source.pdf"
            source.write_bytes(PDF_A)
            content_sha = hashlib.sha256(PDF_A).hexdigest()
            book_id = "book_" + "a" * 32
            job_dir = base / "state" / "vision"
            pages = job_dir / "pages"
            pages.mkdir(parents=True)

            def write_page(text: str) -> None:
                (pages / "p000001.json").write_text(json.dumps({
                    "schema": "reader-page-chars/1",
                    "bookId": book_id,
                    "contentSha256": content_sha,
                    "engine": "vision",
                    "pageNumber": 1,
                    "page_w": 10,
                    "page_h": 20,
                    "chars": [{"c": text}],
                    "furigana": [],
                }), "utf-8")

            formula = job_dir / "formula.json"
            formula.write_text('{"formulas":[]}', "utf-8")
            args = SimpleNamespace(
                book_id=book_id,
                content_sha256=content_sha,
                engine="vision",
                max_bytes=1024 * 1024,
            )
            first_job = {
                "jobId": "ocrjob_first",
                "workerGeneration": "ocrgen_first",
                "bookId": book_id,
                "contentSha256": content_sha,
                "engine": "vision",
                "state": "succeeded",
                "totalPages": 1,
                "successfulPages": 1,
                "formulaState": "succeeded",
                "formulaTotal": 0,
                "resultAvailable": True,
            }
            write_page("A")
            (job_dir / "job.json").write_text(json.dumps(first_job), "utf-8")
            reader_book_ocr_worker._set_worker_identity(
                first_job["jobId"], first_job["workerGeneration"]
            )
            try:
                _publish_release(
                    args, job_dir, formula, first_job, source_path=source
                )
            finally:
                reader_book_ocr_worker._set_worker_identity(None, None)
            fence_path = job_dir.parent / "publication.json"
            previous_fence = fence_path.read_bytes()

            stale_job = {
                **first_job,
                "jobId": "ocrjob_stale",
                "workerGeneration": "ocrgen_stale",
            }
            replacement_job = {
                **first_job,
                "jobId": "ocrjob_replacement",
                "workerGeneration": "ocrgen_replacement",
                "state": "queued",
                "resultAvailable": False,
            }
            write_page("B")
            (job_dir / "job.json").write_text(json.dumps(stale_job), "utf-8")
            real_atomic = reader_book_ocr_worker._atomic_json
            swapped = False

            def replace_generation_at_fence(path, value):
                nonlocal swapped
                if Path(path) == fence_path and not swapped:
                    swapped = True
                    real_atomic(job_dir / "job.json", replacement_job)
                return real_atomic(path, value)

            reader_book_ocr_worker._set_worker_identity(
                stale_job["jobId"], stale_job["workerGeneration"]
            )
            try:
                with patch.object(
                    reader_book_ocr_worker,
                    "_atomic_json",
                    side_effect=replace_generation_at_fence,
                ):
                    with self.assertRaisesRegex(RuntimeError, "no longer current"):
                        _publish_release(
                            args,
                            job_dir,
                            formula,
                            stale_job,
                            source_path=source,
                        )
            finally:
                reader_book_ocr_worker._set_worker_identity(None, None)
            self.assertTrue(swapped)
            self.assertEqual(fence_path.read_bytes(), previous_fence)
            self.assertEqual(
                json.loads((job_dir / "job.json").read_text("utf-8"))["jobId"],
                replacement_job["jobId"],
            )

    def test_fence_write_failure_keeps_previous_release_visible(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            book_id = "book_" + "a" * 32
            source = base / "source.pdf"
            source.write_bytes(PDF_A)
            content_sha = hashlib.sha256(PDF_A).hexdigest()

            def publish(engine: str, text: str, job_id: str) -> str:
                job_dir = base / engine
                pages = job_dir / "pages"
                pages.mkdir(parents=True, exist_ok=True)
                (pages / "p000001.json").write_text(json.dumps({
                    "schema": "reader-page-chars/1",
                    "bookId": book_id,
                    "contentSha256": content_sha,
                    "engine": engine,
                    "pageNumber": 1,
                    "page_w": 10,
                    "page_h": 20,
                    "chars": [{"c": text}],
                    "furigana": [],
                }), "utf-8")
                formula = job_dir / "formula.json"
                formula.write_text('{"formulas":[]}', "utf-8")
                return _publish_release(
                    SimpleNamespace(
                        book_id=book_id,
                        content_sha256=content_sha,
                        engine=engine,
                        max_bytes=1024 * 1024,
                    ),
                    job_dir,
                    formula,
                    {
                        "jobId": job_id,
                        "bookId": book_id,
                        "contentSha256": content_sha,
                        "engine": engine,
                        "state": "succeeded",
                        "totalPages": 1,
                        "successfulPages": 1,
                        "formulaState": "succeeded",
                        "formulaTotal": 0,
                        "resultAvailable": True,
                    },
                    source_path=source,
                )

            first_revision = publish("vision", "A", "ocrjob_a")
            fence_path = base / "publication.json"
            first_fence = fence_path.read_bytes()
            real_atomic = reader_book_ocr_worker._atomic_json

            def fail_new_fence(path, value):
                if Path(path) == fence_path:
                    raise OSError("fault before fence replace")
                return real_atomic(path, value)

            with patch.object(
                reader_book_ocr_worker, "_atomic_json", side_effect=fail_new_fence
            ):
                with self.assertRaises(OSError):
                    publish("manga", "B", "ocrjob_b")
            self.assertEqual(fence_path.read_bytes(), first_fence)
            self.assertEqual(json.loads(first_fence)["revision"], first_revision)


if __name__ == "__main__":
    unittest.main()
