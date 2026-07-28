from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import fitz


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))

import pdf_reader  # noqa: E402


class PageBriefKgIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.project = self.root / "project"
        self.vault = self.root / "vault"
        self.pdf = self.vault / "books" / "sample.pdf"
        self.pdf.parent.mkdir(parents=True)
        document = fitz.open()
        page = document.new_page()
        page.insert_text((72, 72), "A vector space is closed under vector addition.")
        document.save(str(self.pdf))
        document.close()
        self.saved = (
            pdf_reader.CLAUDE_DIR,
            pdf_reader.OBSIDIAN_ROOT,
            pdf_reader._BRIEF_DIR,
            pdf_reader._BOOK_BRIEF_PATH,
        )
        pdf_reader.CLAUDE_DIR = self.project
        pdf_reader.OBSIDIAN_ROOT = self.vault
        pdf_reader._BRIEF_DIR = self.project / "state" / "pdf-page-brief"
        pdf_reader._BOOK_BRIEF_PATH = (
            self.project / "state" / "pdf-book-brief.json"
        )
        pdf_reader._brief_inflight.clear()
        # These tests replace the subprocess itself and exercise PageBrief's
        # pending/retry state machine, not release resolution. Inject explicit
        # fixture paths instead of letting the production fail-closed resolver
        # fall back to the checkout.
        self.runtime_file_patch = patch(
            "kg_runtime.runtime_file",
            side_effect=lambda relative: ROOT / relative,
        )
        self.runtime_file_patch.start()

    def tearDown(self):
        self.runtime_file_patch.stop()
        (
            pdf_reader.CLAUDE_DIR,
            pdf_reader.OBSIDIAN_ROOT,
            pdf_reader._BRIEF_DIR,
            pdf_reader._BOOK_BRIEF_PATH,
        ) = self.saved
        pdf_reader._brief_inflight.clear()
        self.temp.cleanup()

    def test_kg_retry_reuses_saved_brief_without_second_ai_call(self):
        brief = {
            "brief": "定义向量空间。",
            "tags": ["向量空间"],
            "concepts": [{
                "name": "vector space",
                "evidence": "A vector space is closed under vector addition.",
            }],
            "page_type": "knowledge",
            "subtype": "text",
            "model": "haiku",
        }
        calls = {"gen": 0, "kg": 0}

        def fake_run(command, **_kwargs):
            command_text = " ".join(str(part) for part in command)
            if "gen_page_brief.py" in command_text:
                calls["gen"] += 1
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(brief, ensure_ascii=False) + "\n",
                    stderr="",
                )
            if "concept_node_service.py" in command_text:
                calls["kg"] += 1
                if calls["kg"] == 1:
                    return subprocess.CompletedProcess(
                        command,
                        2,
                        stdout=json.dumps({
                            "ok": False,
                            "error": "temporary failure",
                        }) + "\n",
                        stderr="",
                    )
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps({
                        "ok": True,
                        "mutationId": "page-brief:stable",
                        "txId": "kgntx-1",
                        "created": [{
                            "key": "vector space",
                            "nodeId": "em:stable",
                        }],
                    }) + "\n",
                    stderr="",
                )
            raise AssertionError(command_text)

        with patch("subprocess.run", side_effect=fake_run):
            pdf_reader._brief_generate_bg(self.pdf, 1, prefetch=0)
            first = pdf_reader._brief_load_abs(self.pdf)["briefs"]["1"]
            self.assertEqual(first["kg_status"], "pending")
            self.assertEqual(calls, {"gen": 1, "kg": 1})

            pdf_reader._brief_generate_bg(self.pdf, 1, prefetch=0)
            second = pdf_reader._brief_load_abs(self.pdf)["briefs"]["1"]

        self.assertEqual(calls, {"gen": 1, "kg": 2})
        self.assertEqual(second["kg_status"], "synced")
        self.assertEqual(second["kg_result"]["mutationId"], "page-brief:stable")
        self.assertEqual(
            pdf_reader._brief_semantic_digest(first),
            pdf_reader._brief_semantic_digest(second),
        )

    def test_sidecar_save_failure_releases_inflight_and_next_call_retries(self):
        brief = {
            "brief": "定义向量空间。",
            "tags": ["向量空间"],
            "concepts": [{
                "name": "vector space",
                "evidence": "A vector space is closed under vector addition.",
            }],
            "page_type": "knowledge",
            "subtype": "text",
            "model": "haiku",
        }
        calls = {"gen": 0, "save": 0}

        def fake_run(command, **_kwargs):
            command_text = " ".join(str(part) for part in command)
            if "gen_page_brief.py" in command_text:
                calls["gen"] += 1
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(brief, ensure_ascii=False) + "\n",
                    stderr="",
                )
            if "concept_node_service.py" in command_text:
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps({
                        "ok": True,
                        "mutationId": "page-brief:retry-after-save",
                        "txId": "kgntx-save-retry",
                        "created": [],
                    }) + "\n",
                    stderr="",
                )
            raise AssertionError(command_text)

        real_save = pdf_reader._brief_save_abs

        def flaky_save(abs_path, data):
            calls["save"] += 1
            if calls["save"] == 1:
                raise OSError("injected sidecar write failure")
            return real_save(abs_path, data)

        with (
            patch("subprocess.run", side_effect=fake_run),
            patch.object(pdf_reader, "_brief_save_abs", side_effect=flaky_save),
        ):
            pdf_reader._brief_generate_bg(self.pdf, 1, prefetch=0)
            self.assertNotIn(
                (pdf_reader._book_sha(self.pdf), 1),
                pdf_reader._brief_inflight,
            )
            pdf_reader._brief_generate_bg(self.pdf, 1, prefetch=0)

        saved = pdf_reader._brief_load_abs(self.pdf)["briefs"]["1"]
        self.assertEqual(calls["gen"], 2)
        self.assertEqual(saved["kg_status"], "synced")
        self.assertEqual(
            saved["kg_result"]["mutationId"],
            "page-brief:retry-after-save",
        )

    def test_empty_generation_is_retryable_and_never_becomes_none_page(self):
        empty = {
            "brief": "",
            "tags": [],
            "concepts": [],
            "page_type": "",
            "subtype": "",
            "model": "",
        }
        calls = {"gen": 0}

        def fake_run(command, **_kwargs):
            command_text = " ".join(str(part) for part in command)
            if "gen_page_brief.py" not in command_text:
                raise AssertionError(command_text)
            calls["gen"] += 1
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps(empty, ensure_ascii=False) + "\n",
                stderr="",
            )

        with patch("subprocess.run", side_effect=fake_run):
            pdf_reader._brief_generate_bg(self.pdf, 1, prefetch=0)
            first = pdf_reader._brief_load_abs(self.pdf)
            pdf_reader._brief_generate_bg(self.pdf, 1, prefetch=0)
            second = pdf_reader._brief_load_abs(self.pdf)

        self.assertEqual(calls["gen"], 2)
        self.assertNotIn("1", first["briefs"])
        self.assertNotIn(1, first["_none_pages"])
        self.assertNotIn("1", second["briefs"])
        self.assertNotIn(1, second["_none_pages"])
        self.assertNotIn(
            (pdf_reader._book_sha(self.pdf), 1),
            pdf_reader._brief_inflight,
        )

    def test_explicit_skip_is_saved_once_and_never_promoted_to_kg(self):
        explicit_skip = {
            "brief": "",
            "tags": [],
            "concepts": [],
            "page_type": "skip",
            "subtype": "blank",
            "model": "haiku",
        }
        calls = {"gen": 0, "kg": 0}

        def fake_run(command, **_kwargs):
            command_text = " ".join(str(part) for part in command)
            if "gen_page_brief.py" in command_text:
                calls["gen"] += 1
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps(explicit_skip, ensure_ascii=False) + "\n",
                    stderr="",
                )
            if "concept_node_service.py" in command_text:
                calls["kg"] += 1
                raise AssertionError("显式 skip 不得调用 KG 服务")
            raise AssertionError(command_text)

        with patch("subprocess.run", side_effect=fake_run):
            pdf_reader._brief_generate_bg(self.pdf, 1, prefetch=0)
            pdf_reader._brief_generate_bg(self.pdf, 1, prefetch=0)

        saved = pdf_reader._brief_load_abs(self.pdf)
        self.assertEqual(calls, {"gen": 1, "kg": 0})
        self.assertNotIn(1, saved["_none_pages"])
        self.assertEqual(saved["briefs"]["1"]["page_type"], "skip")
        self.assertEqual(saved["briefs"]["1"]["subtype"], "blank")
        self.assertEqual(saved["briefs"]["1"]["kg_status"], "not_applicable")


if __name__ == "__main__":
    unittest.main()
