from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import reader_pc_preprocess_worker as worker  # noqa: E402


PDF = b"%PDF-1.4\nquality-first-test\n%%EOF\n"


class FakeResponse:
    def __init__(self, status=200, value=None, raw=None, headers=None):
        self.status = status
        self.headers = headers or {}
        if raw is None:
            raw = json.dumps(value or {}, separators=(",", ":")).encode("utf-8")
        self.stream = io.BytesIO(raw)

    def getcode(self):
        return self.status

    def read(self, size=-1):
        return self.stream.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def open(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers), body))
        if not self.responses:
            raise AssertionError("unexpected HTTP request")
        return self.responses.pop(0)


class FakeCFunction:
    def __init__(self, result):
        self.result = result
        self.argtypes = None
        self.restype = None
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.result


def claim_payload(digest, *, completed=None):
    return {
        "ok": True,
        "contract": worker.WORKER_CONTRACT,
        "lease": {
            "leaseId": "lease_1",
            "expiresAtEpochMs": 9_999_999_999_999,
            "renewAfterMs": 60_000,
        },
        "job": {
            "jobId": "job_1",
            "bookId": "book_1",
            "contentSha256": digest,
            "engine": "manga",
            "executor": "pc",
            "processingProfile": worker.PROCESSING_PROFILE,
            "generation": "generation_1",
            "totalPages": 2,
            "sourceSize": len(PDF),
            "sourceUrl": (
                worker.SOURCE_PATH
                + "?workerId=pc_test&jobId=job_1&contentSha256="
                + digest
            ),
            "completedPages": completed or [],
            "limits": {
                "maxPages": 5000,
                "maxPdfBytes": 1024 * 1024,
                "maxPageBytes": 1024 * 1024,
                "maxFormulaBytes": 1024 * 1024,
            },
        },
    }


class PiWorkerApiTest(unittest.TestCase):
    def test_wire_contract_is_bearer_authenticated_and_model_agnostic(self):
        digest = hashlib.sha256(PDF).hexdigest()
        transport = FakeTransport(
            [
                FakeResponse(value=claim_payload(digest)),
                FakeResponse(
                    value={
                        "ok": True,
                        "contract": worker.WORKER_CONTRACT,
                        "desiredState": "running",
                    }
                ),
                FakeResponse(
                    value={
                        "ok": True,
                        "contract": worker.WORKER_CONTRACT,
                        "accepted": True,
                        "desiredState": "running",
                    }
                ),
                FakeResponse(
                    value={
                        "ok": True,
                        "contract": worker.WORKER_CONTRACT,
                        "accepted": True,
                        "desiredState": "running",
                    }
                ),
                FakeResponse(
                    value={
                        "ok": True,
                        "contract": worker.WORKER_CONTRACT,
                        "published": True,
                        "desiredState": "running",
                    }
                ),
            ]
        )
        api = worker.PiWorkerApi(
            "https://pi.example", "top-secret", "pc_test", transport=transport
        )
        claim = api.claim(("vision", "manga"))
        self.assertIsNotNone(claim)
        claim_body = json.loads(transport.calls[0][3])
        self.assertEqual(transport.calls[0][0], "POST")
        self.assertTrue(transport.calls[0][1].endswith(worker.CLAIM_PATH))
        self.assertEqual(
            transport.calls[0][2]["Authorization"], "Bearer top-secret"
        )
        self.assertEqual(
            set(claim_body["capabilities"]),
            {"engines", "maxPdfBytes", "maxPageBytes", "processingProfile"},
        )
        self.assertEqual(
            claim_body["capabilities"]["processingProfile"],
            worker.PROCESSING_PROFILE,
        )
        self.assertNotIn("formulaModel", json.dumps(claim_body))

        progress = worker._progress(2, text=1, words=1)
        api.heartbeat(claim, phase="text-ocr", current_page=2, progress=progress)
        page = {
            "schema": worker.PAGE_SCHEMA,
            "bookId": claim.book_id,
            "contentSha256": claim.content_sha256,
            "engine": claim.engine,
            "pageNumber": 2,
            "chars": [],
        }
        api.put_page(claim, 2, page, progress)
        formula = {
            "schema": worker.FORMULA_SCHEMA,
            "bookId": claim.book_id,
            "contentSha256": claim.content_sha256,
            "formulas": [],
        }
        api.put_formulas(claim, formula, "succeeded", None, progress)
        api.complete(claim, 2, progress)
        sent = [json.loads(call[3]) for call in transport.calls[1:]]
        self.assertTrue(all(value["leaseId"] == "lease_1" for value in sent))
        self.assertTrue(all(value["generation"] == "generation_1" for value in sent))
        self.assertTrue(all("workerGeneration" not in value for value in sent))
        self.assertTrue(all("profile" not in value for value in sent))
        self.assertTrue(transport.calls[2][1].endswith("/pages/2"))

    def test_source_download_never_sends_bearer_cross_origin(self):
        digest = hashlib.sha256(PDF).hexdigest()
        payload = claim_payload(digest)
        payload["job"]["sourceUrl"] = "https://attacker.example/source.pdf"
        claim = worker.Claim.parse("pc_test", payload, {
            "max_pdf_bytes": 1024,
            "max_page_bytes": 1024,
        })
        transport = FakeTransport([])
        api = worker.PiWorkerApi(
            "https://pi.example", "secret", "pc_test", transport=transport
        )
        with self.assertRaisesRegex(worker.WorkerError, "untrusted source"):
            api.source_response(claim)
        self.assertEqual(transport.calls, [])

    def test_error_redacts_credentials_and_paths(self):
        text = worker.safe_error(
            RuntimeError(
                "Authorization: Bearer secret-value at C:\\Users\\person\\private.txt "
                "from https://pi.example/source?key=ordinary-key-secret&mode=read"
            )
        )
        self.assertNotIn("secret-value", text)
        self.assertNotIn("ordinary-key-secret", text)
        self.assertNotIn("private.txt", text)
        self.assertIn("redacted", text)

    def test_default_worker_id_is_stable_within_and_unique_between_process_instances(self):
        with patch.object(worker.socket, "gethostname", return_value="reader-pc"), patch.object(
            worker.os, "getpid", return_value=1234
        ), patch.dict(os.environ, {"USERNAME": "reader"}, clear=False), patch.object(
            worker, "PROCESS_INSTANCE_NONCE", "instance-a"
        ):
            first = worker._default_worker_id()
            repeated = worker._default_worker_id()
        with patch.object(worker.socket, "gethostname", return_value="reader-pc"), patch.object(
            worker.os, "getpid", return_value=1234
        ), patch.dict(os.environ, {"USERNAME": "reader"}, clear=False), patch.object(
            worker, "PROCESS_INSTANCE_NONCE", "instance-b"
        ):
            second = worker._default_worker_id()
        self.assertEqual(first, repeated)
        self.assertNotEqual(first, second)
        self.assertRegex(first, r"^pc_[a-f0-9]{24}$")

    def test_claim_rejects_incompatible_processing_profile(self):
        payload = claim_payload(hashlib.sha256(PDF).hexdigest())
        payload["job"]["processingProfile"] = "unknown-profile"
        with self.assertRaisesRegex(worker.WorkerError, "processing profile"):
            worker.Claim.parse(
                "pc_test",
                payload,
                {"max_pdf_bytes": 1024, "max_page_bytes": 1024},
            )

    def test_mutation_responses_fail_closed_on_invalid_acknowledgements(self):
        claim = worker.Claim.parse(
            "pc_test",
            claim_payload(hashlib.sha256(PDF).hexdigest()),
            {"max_pdf_bytes": 1024 * 1024, "max_page_bytes": 1024 * 1024},
        )
        progress = worker._progress(2, text=1, words=1)
        page = {
            "schema": worker.PAGE_SCHEMA,
            "bookId": claim.book_id,
            "contentSha256": claim.content_sha256,
            "engine": claim.engine,
            "pageNumber": 2,
            "chars": [],
        }
        formula = {
            "schema": worker.FORMULA_SCHEMA,
            "bookId": claim.book_id,
            "contentSha256": claim.content_sha256,
            "formulas": [],
        }
        operations = (
            (
                "heartbeat",
                lambda api: api.heartbeat(
                    claim, phase="text-ocr", current_page=2, progress=progress
                ),
                None,
            ),
            (
                "page",
                lambda api: api.put_page(claim, 2, page, progress),
                "accepted",
            ),
            (
                "formula",
                lambda api: api.put_formulas(
                    claim, formula, "succeeded", None, progress
                ),
                "accepted",
            ),
            (
                "complete",
                lambda api: api.complete(claim, 2, progress),
                "published",
            ),
        )
        for label, invoke, required_flag in operations:
            flag_value = {required_flag: True} if required_flag else {}
            malformed = [
                {"ok": False, "contract": worker.WORKER_CONTRACT, **flag_value},
                {"ok": True, "contract": "wrong-contract", **flag_value},
            ]
            if required_flag:
                malformed.append(
                    {
                        "ok": True,
                        "contract": worker.WORKER_CONTRACT,
                        required_flag: False,
                    }
                )
            for response in malformed:
                with self.subTest(operation=label, response=response):
                    api = worker.PiWorkerApi(
                        "https://pi.example",
                        "secret",
                        "pc_test",
                        transport=FakeTransport([FakeResponse(value=response)]),
                    )
                    with self.assertRaisesRegex(worker.WorkerError, "acknowledgement"):
                        invoke(api)

    def test_formula_reason_is_a_stable_server_accepted_code(self):
        self.assertRegex(
            worker.FORMULA_RECOGNITION_FAILED,
            r"^[a-z][a-z0-9-]{0,63}$",
        )
        self.assertNotIn(":", worker.FORMULA_RECOGNITION_FAILED)

    def test_start_script_has_safe_origin_default_and_environment_override(self):
        source = (ROOT / "scripts" / "start_reader_pc_preprocess_worker.cmd").read_text(
            "utf-8"
        )
        self.assertIn("if not defined BW_READER_PC_OCR_BASE_URL", source)
        self.assertIn("https://bwicarus.taile44d0c.ts.net", source)
        self.assertIn("reader-pc-ocr-venv", source)
        self.assertIn("reader_unimernet_adapter:create_model", source)
        self.assertIn("models\\unimernet_base", source)
        self.assertIn("if not defined BW_READER_PC_DOCLAYOUT_MODEL", source)
        self.assertIn(
            "models\\doclayout_yolo\\doclayout_yolo_docstructbench_imgsz1024.pt",
            source,
        )
        self.assertIn("if not defined HF_HOME", source)
        self.assertIn("models\\hf-cache", source)
        self.assertIn("if not defined XDG_CACHE_HOME", source)
        self.assertIn("models\\cache", source)
        self.assertNotIn("HF_HUB_OFFLINE", source)
        self.assertNotIn("TRANSFORMERS_OFFLINE", source)
        self.assertNotIn("HF_DATASETS_OFFLINE", source)
        self.assertNotIn("BW_READER_PC_OCR_TOKEN=", source)


class CacheTest(unittest.TestCase):
    def test_download_is_content_addressed_and_resumes_only_matching_range(self):
        digest = hashlib.sha256(PDF).hexdigest()
        claim = worker.Claim.parse(
            "pc_test",
            claim_payload(digest),
            {"max_pdf_bytes": 1024 * 1024, "max_page_bytes": 1024 * 1024},
        )
        with tempfile.TemporaryDirectory() as temp:
            cache = worker.ContentCache(Path(temp))
            partial = cache.source_path(digest).with_suffix(".pdf.part")
            partial.parent.mkdir(parents=True)
            split = len(PDF) // 2
            partial.write_bytes(PDF[:split])

            class Api:
                def __init__(self):
                    self.offsets = []

                def source_response(self, _claim, offset):
                    self.offsets.append(offset)
                    return FakeResponse(
                        206,
                        raw=PDF[offset:],
                        headers={"Content-Range": f"bytes {offset}-{len(PDF)-1}/{len(PDF)}"},
                    )

            api = Api()
            path = cache.download(api, claim)
            self.assertEqual(api.offsets, [split])
            self.assertEqual(path.read_bytes(), PDF)
            self.assertEqual(worker._sha256_file(path), digest)


class InlineMonitor:
    def __init__(self, api, claim):
        self.api = api
        self.claim = claim
        self.phase = "preparing"
        self.current_page = None
        self.progress = worker._progress(claim.total_pages)
        self.desired = "running"

    def start(self):
        self.poll_now()

    def update(self, phase, current_page, progress):
        self.phase = phase
        self.current_page = current_page
        self.progress = progress

    def accept(self, response):
        self.desired = response.get("desiredState", "running")

    def poll_now(self):
        self.accept(
            self.api.heartbeat(
                self.claim,
                phase=self.phase,
                current_page=self.current_page,
                progress=self.progress,
            )
        )

    def checkpoint(self):
        if self.desired != "running":
            raise worker.LeaseStopped(self.desired)

    def close(self):
        pass


class FakePipeline:
    def __init__(self, _root):
        self.closed = False
        self.pages = []

    def open(self, _pdf):
        return 2

    def page(self, claim, page_number):
        self.pages.append(page_number)
        return {
            "schema": worker.PAGE_SCHEMA,
            "bookId": claim.book_id,
            "contentSha256": claim.content_sha256,
            "engine": claim.engine,
            "pageNumber": page_number,
            "chars": [{"c": "文", "w": 1, "bk": 1, "b": 0}],
            "tokenized": True,
        }

    def release_text_model(self):
        pass

    def formulas(self, claim, checkpoint, total):
        checkpoint(
            "formula-detect", 1, worker._progress(total, text=total, words=total)
        )
        return (
            {
                "schema": worker.FORMULA_SCHEMA,
                "bookId": claim.book_id,
                "contentSha256": claim.content_sha256,
                "formulas": [],
            },
            "succeeded",
            None,
            0,
            0,
        )

    def close(self):
        self.closed = True


class FakeApi:
    def __init__(self, claim, source=PDF, desired=None):
        self._claim = claim
        self.source = source
        self.desired = list(desired or [])
        self.pages = []
        self.formulas = []
        self.completed = []
        self.heartbeats = []

    def claim(self, _engines):
        value, self._claim = self._claim, None
        return value

    def source_response(self, _claim, offset):
        self.assert_offset = offset
        return FakeResponse(200, raw=self.source)

    def heartbeat(self, _claim, **kwargs):
        self.heartbeats.append(kwargs)
        return {
            "desiredState": self.desired.pop(0) if self.desired else "running"
        }

    def put_page(self, _claim, number, page, progress):
        self.pages.append((number, page, progress))
        return {"desiredState": "running"}

    def put_formulas(self, _claim, formula, state, reason, progress):
        self.formulas.append((formula, state, reason, progress))
        return {"desiredState": "running"}

    def complete(self, _claim, total, progress):
        self.completed.append((total, progress))
        return {"desiredState": "running"}


class WorkerRunnerTest(unittest.TestCase):
    def test_quality_profile_defaults_to_unimernet_and_requires_cuda(self):
        self.assertEqual(worker.QUALITY_PROFILE["formulaModel"], "unimernet-base")
        self.assertEqual(
            worker.QUALITY_PROFILE["formulaCompatibilityFallback"],
            "explicit-pix2tex-only",
        )
        fake_torch = SimpleNamespace(
            cuda=SimpleNamespace(is_available=lambda: False),
            version=SimpleNamespace(cuda=None),
        )
        with patch.object(
            worker.importlib, "import_module", return_value=fake_torch
        ):
            with self.assertRaisesRegex(worker.WorkerError, "requires CUDA"):
                worker.QualityPipeline.cuda_status()

    def test_missing_unimernet_is_explicit_and_never_falls_back_to_pix2tex(self):
        pipeline = worker.QualityPipeline(ROOT)
        pipeline._torch = SimpleNamespace(
            cuda=SimpleNamespace(is_available=lambda: True)
        )
        imported = []
        real_import = worker.importlib.import_module

        def record_import(name):
            imported.append(name)
            return real_import(name)

        with patch.dict(
            os.environ,
            {
                "BW_READER_PC_FORMULA_BACKEND": "unimernet-base",
                "BW_READER_PC_UNIMERNET_ADAPTER": "",
            },
            clear=False,
        ), patch.object(
            worker.importlib, "import_module", side_effect=record_import
        ):
            with self.assertRaisesRegex(
                worker.WorkerError, "formula-model-unavailable"
            ):
                pipeline._formula_model()
        self.assertNotIn("pix2tex.cli", imported)

    def test_one_job_uploads_tokenized_pages_formulas_then_completes(self):
        digest = hashlib.sha256(PDF).hexdigest()
        payload = claim_payload(digest, completed=[1])
        claim = worker.Claim.parse(
            "pc_test",
            payload,
            {"max_pdf_bytes": 1024 * 1024, "max_page_bytes": 1024 * 1024},
        )
        with tempfile.TemporaryDirectory() as temp:
            cache = worker.ContentCache(Path(temp))
            api = FakeApi(claim)
            runner = worker.WorkerRunner(
                api,
                cache,
                ROOT,
                ("manga",),
                pipeline_factory=FakePipeline,
                monitor_factory=InlineMonitor,
            )
            self.assertTrue(runner.run_once())
            self.assertEqual([number for number, _, _ in api.pages], [2])
            self.assertTrue(api.pages[0][1]["tokenized"])
            self.assertNotIn("profile", api.pages[0][1])
            self.assertEqual(len(api.formulas), 1)
            self.assertEqual(api.completed[0][0], 2)
            status = json.loads(cache.status_path.read_text("utf-8"))
            self.assertEqual(status["state"], "idle")
            self.assertEqual(status["profile"], worker.QUALITY_PROFILE)

    def test_pause_stops_before_next_page_and_never_completes(self):
        digest = hashlib.sha256(PDF).hexdigest()
        claim = worker.Claim.parse(
            "pc_test",
            claim_payload(digest),
            {"max_pdf_bytes": 1024 * 1024, "max_page_bytes": 1024 * 1024},
        )
        with tempfile.TemporaryDirectory() as temp:
            # start heartbeat runs, then the first page-boundary heartbeat pauses.
            api = FakeApi(claim, desired=["running", "paused"])
            runner = worker.WorkerRunner(
                api,
                worker.ContentCache(Path(temp)),
                ROOT,
                ("manga",),
                pipeline_factory=FakePipeline,
                monitor_factory=InlineMonitor,
            )
            with self.assertRaises(worker.LeaseStopped) as stopped:
                runner.run_once()
            self.assertEqual(stopped.exception.desired_state, "paused")
            self.assertEqual(api.pages, [])
            self.assertEqual(api.completed, [])
            self.assertEqual(api.heartbeats[-1]["state"], "paused")

    def test_formula_model_unavailable_still_completes_text_publication(self):
        class UnavailableFormulaPipeline(FakePipeline):
            def formulas(self, claim, checkpoint, total):
                checkpoint(
                    "formula-detect",
                    None,
                    worker._progress(total, text=total, words=total),
                )
                return (
                    {
                        "schema": worker.FORMULA_SCHEMA,
                        "bookId": claim.book_id,
                        "contentSha256": claim.content_sha256,
                        "formulas": [],
                    },
                    "unavailable",
                    "formula-model-unavailable",
                    0,
                    0,
                )

        digest = hashlib.sha256(PDF).hexdigest()
        claim = worker.Claim.parse(
            "pc_test",
            claim_payload(digest),
            {"max_pdf_bytes": 1024 * 1024, "max_page_bytes": 1024 * 1024},
        )
        with tempfile.TemporaryDirectory() as temp:
            api = FakeApi(claim)
            runner = worker.WorkerRunner(
                api,
                worker.ContentCache(Path(temp)),
                ROOT,
                ("manga",),
                pipeline_factory=UnavailableFormulaPipeline,
                monitor_factory=InlineMonitor,
            )
            self.assertTrue(runner.run_once())
            self.assertEqual(api.formulas[0][1], "unavailable")
            self.assertEqual(api.formulas[0][2], "formula-model-unavailable")
            self.assertEqual(api.completed[0][0], 2)

    def test_manga_engine_explicitly_disables_force_cpu(self):
        calls = []

        class Manga:
            def __init__(self, **kwargs):
                calls.append(kwargs)
                self.device = "cuda:0"

        pipeline = worker.QualityPipeline(ROOT)
        pipeline._torch = SimpleNamespace(
            cuda=SimpleNamespace(is_available=lambda: True)
        )
        real_import = worker.importlib.import_module

        def fake_import(name):
            if name == "mokuro.manga_page_ocr":
                return SimpleNamespace(MangaPageOcr=Manga)
            return real_import(name)

        with patch.object(worker.importlib, "import_module", side_effect=fake_import):
            pipeline._manga_engine()
        self.assertEqual(calls, [{"force_cpu": False}])

    def test_cuda_proof_stops_before_unrelated_torch_proxies(self):
        class ExplosiveProxy:
            @property
            def device(self):
                raise RuntimeError("internal torch proxy must not be inspected")

        class Model:
            device = "cuda:0"

            def __init__(self):
                self.unrelated = ExplosiveProxy()

        worker.QualityPipeline._assert_model_cuda(Model(), "formula OCR")


class WindowsPriorityTest(unittest.TestCase):
    def test_windows_handle_is_not_truncated_before_lowering_priority(self):
        pseudo_handle = 0x123456789ABCDEF0
        get_current = FakeCFunction(pseudo_handle)
        set_priority = FakeCFunction(1)
        kernel32 = SimpleNamespace(
            GetCurrentProcess=get_current,
            SetPriorityClass=set_priority,
        )
        fake_ctypes = SimpleNamespace(
            WinDLL=lambda name, use_last_error: kernel32,
            c_void_p=object(),
            c_uint32=object(),
            c_int=object(),
            get_last_error=lambda: 0,
        )

        with patch.object(worker.os, "name", "nt"), patch.dict(
            sys.modules, {"ctypes": fake_ctypes}
        ):
            worker._lower_process_priority()

        self.assertEqual(get_current.argtypes, [])
        self.assertIs(get_current.restype, fake_ctypes.c_void_p)
        self.assertEqual(
            set_priority.argtypes,
            [fake_ctypes.c_void_p, fake_ctypes.c_uint32],
        )
        self.assertIs(set_priority.restype, fake_ctypes.c_int)
        self.assertEqual(set_priority.calls, [(pseudo_handle, 0x00004000)])


if __name__ == "__main__":
    unittest.main()
