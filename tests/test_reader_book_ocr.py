from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))

from reader_book_library import BookLibrary  # noqa: E402
from reader_book_ocr import ReaderBookOcrError, ReaderBookOcrService  # noqa: E402
from reader_book_ocr_worker import _publish_attachments, _tokenize_chars  # noqa: E402


PDF_A = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n%%EOF\n"


class _FakeProcess:
    def __init__(self, pid: int = 424242) -> None:
        self.pid = pid


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

        def launch(job_dir, source_path, job):
            self.launches.append((job_dir, source_path, dict(job)))
            return _FakeProcess()

        self.service = ReaderBookOcrService(
            self.library,
            self.base / "ocr",
            ROOT,
            launcher=launch,
            max_pdf_bytes=1024 * 1024,
            max_pages=100,
        )

    def tearDown(self) -> None:
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

        repeated, already = self.service.start(
            self.entry["bookId"], self.entry["contentSha256"], "vision"
        )
        self.assertTrue(already)
        self.assertEqual(repeated["jobId"], job["jobId"])
        self.assertEqual(len(self.launches), 1)

    def test_version_kind_and_unknown_fields_fail_closed(self) -> None:
        with self.assertRaises(ReaderBookOcrError) as changed:
            self.service.start(self.entry["bookId"], "0" * 64, "vision")
        self.assertEqual(changed.exception.code, "book-version-changed")
        with self.assertRaises(ReaderBookOcrError) as engine:
            self.service.start(self.entry["bookId"], self.entry["contentSha256"], "other")
        self.assertEqual(engine.exception.code, "invalid-engine")

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

    def test_attachment_manifest_is_immutable_and_path_whitelisted(self) -> None:
        version = self.service._version_dir(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        page = version / "vision" / "pages" / "p000001.json"
        page.parent.mkdir(parents=True)
        page.write_text('{"schema":"reader-page-chars/1"}', "utf-8")
        formulas = version / "formulas.json"
        (version / "result.json").write_text('{"engine":"vision"}', "utf-8")
        formula_source = self.base / "global-formulas.json"
        formula_source.write_text(json.dumps({
            "formulas": [{"page": 1, "bbox": [0, 0, 0.5, 0.5], "latex": "x"}],
        }), "utf-8")
        args = SimpleNamespace(
            book_id=self.entry["bookId"],
            content_sha256=self.entry["contentSha256"],
        )
        revision, manifest = _publish_attachments(args, version / "vision", formula_source)
        page_sha = hashlib.sha256(page.read_bytes()).hexdigest()
        formula_sha = hashlib.sha256(formulas.read_bytes()).hexdigest()
        loaded = self.service.attachment_manifest(
            self.entry["bookId"], self.entry["contentSha256"]
        )
        self.assertEqual(loaded["category"], "derived")
        self.assertEqual(loaded["revision"], revision)
        self.assertEqual(self.service.read_formulas(
            self.entry["bookId"], self.entry["contentSha256"]
        )[0]["latex"], "x")
        entry, resolved = self.service.read_attachment(
            self.entry["bookId"], self.entry["contentSha256"], "ocr-page-000001"
        )
        self.assertEqual(entry["sha256"], page_sha)
        self.assertEqual(resolved, page)
        with self.assertRaises(ReaderBookOcrError):
            self.service.read_attachment(
                self.entry["bookId"], self.entry["contentSha256"], "../../A.pdf"
            )

        manifest["files"][0]["downloadUrl"] = "../../A.pdf"
        (version / "attachments.json").write_text(json.dumps(manifest), "utf-8")
        with self.assertRaises(ReaderBookOcrError) as invalid_manifest:
            self.service.attachment_manifest(
                self.entry["bookId"], self.entry["contentSha256"]
            )
        self.assertEqual(invalid_manifest.exception.code, "ocr-attachments-invalid")


class ReaderBookOcrWorkerContractTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
