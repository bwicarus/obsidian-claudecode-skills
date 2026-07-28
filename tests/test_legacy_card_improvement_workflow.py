from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "_client" / "core"
SERVER = ROOT / "_server_deploy"
for directory in (str(CORE), str(SERVER)):
    if directory not in sys.path:
        sys.path.insert(0, directory)

import card_improvement_runtime as runtime  # noqa: E402
import qa_browser  # noqa: E402


class FakeCodexApp:
    def __init__(self, note_output: str):
        self.note_output = note_output
        self.started: list[tuple[str, str]] = []
        self.turns: list[tuple[str, str, str, str]] = []
        self.closed: list[str] = []

    def thread_start(self, model, service_tier=""):
        self.started.append((model, service_tier))
        return "legacy-shared-thread"

    def turn_stream(
        self,
        thread_id,
        prompt,
        effort,
        timeout=180,
        service_tier="",
    ):
        self.turns.append((thread_id, prompt, effort, service_tier))
        if len(self.turns) == 1:
            yield (
                '[{"type":"basic","front":"改进问题","back":"改进答案",'
                '"reason":"补足困惑"}]'
            )
        else:
            yield self.note_output

    def thread_close(self, thread_id):
        self.closed.append(thread_id)


class LegacyCardImprovementWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.old_vault = qa_browser.VAULT
        self.old_records = qa_browser.ANKI_RECORDS_DIR
        self.old_runtime_modules = qa_browser._card_improvement_runtime_modules
        self.old_setting = qa_browser._qa_setting

    def tearDown(self):
        qa_browser.VAULT = self.old_vault
        qa_browser.ANKI_RECORDS_DIR = self.old_records
        qa_browser._card_improvement_runtime_modules = self.old_runtime_modules
        qa_browser._qa_setting = self.old_setting

    def _fixture(self, root: Path):
        vault = root / "vault"
        records = root / "records"
        vault.mkdir()
        records.mkdir()
        original = "---\ntitle: test\n---\n原始内容。\n"
        note = vault / "知识.md"
        note.write_text(original, encoding="utf-8")
        record_path = records / "知识.json"
        record = {
            "source_note": "知识.md",
            "source_link": "[[知识]]",
            "source_url": "obsidian://open?vault=V&file=知识",
            "cards": [
                {
                    "local_id": "legacy-1",
                    "type": "basic",
                    "front": "原问题",
                    "back": "原答案",
                    "deck": "Obsidian::知识",
                    "anki_note_id": 100,
                }
            ],
        }
        record_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        qa_browser.VAULT = vault
        qa_browser.ANKI_RECORDS_DIR = records
        qa_browser._qa_setting = lambda key, default=None: (
            "1" if key == "qa_user_id" else default
        )
        return original, note, record_path

    def test_old_page_uses_shared_runtime_and_one_native_thread_for_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            original, note, record_path = self._fixture(Path(tmp))
            note_output = original + "新增解释。\n"
            app = FakeCodexApp(note_output)
            assistant = types.SimpleNamespace(
                _resolve=lambda action, uid: {
                    "backend": "codex",
                    "variant": "gpt-5.6-luna",
                    "depth": "low",
                    "fast": False,
                },
                _codex_fast_ok=lambda model: False,
                _codex_app=app,
                _codex_exec_text=lambda *args, **kwargs: None,
            )
            qa_browser._card_improvement_runtime_modules = (
                lambda: (runtime, assistant)
            )
            owner = qa_browser._legacy_card_owner("a" * 64)

            result = qa_browser._prepare_legacy_card_draft(
                "legacy-1",
                [{"question": "为什么？", "answer": "因为有这个条件。"}],
                "all",
                verbosity="verbose",
                owner=owner,
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["targets"], ["anki", "note"])
            self.assertEqual(result["runner"]["mode"], "codex_app_thread")
            self.assertTrue(result["runner"]["native_multiturn_used"])
            self.assertEqual(result["runner"]["native_turns"], 2)
            self.assertEqual(app.started, [("gpt-5.6-luna", "")])
            self.assertEqual(app.closed, ["legacy-shared-thread"])
            self.assertEqual(len(app.turns), 2)
            self.assertIn("因为有这个条件", app.turns[0][1])
            self.assertIn("上一轮", app.turns[1][1])
            self.assertNotIn("因为有这个条件", app.turns[1][1])
            self.assertEqual(note.read_text("utf-8"), original)
            stored_record = json.loads(record_path.read_text("utf-8"))
            self.assertEqual(len(stored_record["cards"]), 1)

    def test_legacy_page_fast_preference_is_strict_boolean(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._fixture(Path(tmp))
            app = FakeCodexApp("")
            assistant = types.SimpleNamespace(
                _resolve=lambda action, uid: {
                    "backend": "codex",
                    "variant": "gpt-5.6-luna",
                    "depth": "low",
                    "fast": "true",
                },
                _codex_fast_ok=lambda model: True,
                _codex_app=app,
                _codex_exec_text=lambda *args, **kwargs: None,
            )
            qa_browser._card_improvement_runtime_modules = (
                lambda: (runtime, assistant)
            )

            result = qa_browser._prepare_legacy_card_draft(
                "legacy-1",
                [{"question": "为什么？", "answer": "因为有这个条件。"}],
                "anki",
                owner=qa_browser._legacy_card_owner("f" * 64),
            )

            self.assertTrue(result["ok"])
            self.assertEqual(app.started, [("gpt-5.6-luna", "")])
            self.assertEqual(app.turns[0][3], "")

    def test_note_commit_is_explicit_idempotent_and_conflict_safe(self):
        with tempfile.TemporaryDirectory() as tmp:
            original, note_path, _record_path = self._fixture(Path(tmp))
            owner = qa_browser._legacy_card_owner("b" * 64)
            draft_content = original + "确认后才写入。\n"
            draft_id = runtime.DEFAULT_DRAFT_STORE.create(
                owner,
                {
                    "targets": ["note"],
                    "identity": {
                        "local_id": "legacy-1",
                        "source_note": "知识.md",
                    },
                    "drafts": {
                        "note": {
                            "content": draft_content,
                            "verbosity": "verbose",
                            "base_sha256": hashlib.sha256(
                                original.encode("utf-8")
                            ).hexdigest(),
                        }
                    },
                },
            )

            first = qa_browser._commit_legacy_card_draft(
                draft_id=draft_id,
                target="note",
                owner=owner,
            )
            second = qa_browser._commit_legacy_card_draft(
                draft_id=draft_id,
                target="note",
                owner=owner,
            )

            self.assertTrue(first["ok"])
            self.assertEqual(note_path.read_text("utf-8"), draft_content)
            self.assertTrue(second["ok"])
            self.assertTrue(second["dedup"])

            current = note_path.read_text("utf-8")
            conflict_id = runtime.DEFAULT_DRAFT_STORE.create(
                owner,
                {
                    "targets": ["note"],
                    "identity": {
                        "local_id": "legacy-1",
                        "source_note": "知识.md",
                    },
                    "drafts": {
                        "note": {
                            "content": current + "旧预览内容。\n",
                            "verbosity": "concise",
                            "base_sha256": hashlib.sha256(
                                current.encode("utf-8")
                            ).hexdigest(),
                        }
                    },
                },
            )
            note_path.write_text(current + "用户的新改动。\n", encoding="utf-8")
            conflict = qa_browser._commit_legacy_card_draft(
                draft_id=conflict_id,
                target="note",
                owner=owner,
            )
            self.assertFalse(conflict["ok"])
            self.assertTrue(conflict["conflict"])
            self.assertTrue(
                note_path.read_text("utf-8").endswith("用户的新改动。\n")
            )

    def test_anki_commit_uses_frozen_draft_and_keeps_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            _original, _note, record_path = self._fixture(Path(tmp))
            owner = qa_browser._legacy_card_owner("c" * 64)
            draft_id = runtime.DEFAULT_DRAFT_STORE.create(
                owner,
                {
                    "targets": ["anki"],
                    "identity": {
                        "local_id": "legacy-1",
                        "source_note": "知识.md",
                    },
                    "drafts": {
                        "cards": [
                            {
                                "type": "basic",
                                "front": "冻结的问题",
                                "back": "冻结的答案",
                                "reason": "补足困惑",
                            }
                        ]
                    },
                },
            )
            calls: list[tuple[str, dict]] = []

            def fake_anki(action, params=None, timeout=10):
                calls.append((action, params or {}))
                if action == "findNotes":
                    return []
                if action == "addNote":
                    return 9001
                if action == "findCards":
                    return [9101]
                if action == "sync":
                    return None
                return None

            with mock.patch.object(qa_browser, "_anki_request", fake_anki):
                first = qa_browser._commit_legacy_card_draft(
                    draft_id=draft_id,
                    target="anki",
                    owner=owner,
                )
                call_count = len(calls)
                second = qa_browser._commit_legacy_card_draft(
                    draft_id=draft_id,
                    target="anki",
                    owner=owner,
                )

            self.assertTrue(first["ok"])
            self.assertTrue(second["ok"])
            self.assertTrue(second["dedup"])
            self.assertEqual(len(calls), call_count)
            record = json.loads(record_path.read_text("utf-8"))
            self.assertEqual(len(record["cards"]), 2)
            self.assertEqual(record["cards"][0]["local_id"], "legacy-1")
            self.assertEqual(record["cards"][1]["front"], "冻结的问题")
            self.assertFalse(any(action == "deleteNotes" for action, _ in calls))

    def test_signed_draft_is_bound_to_old_page_client_owner(self):
        owner = qa_browser._legacy_card_owner("d" * 64)
        other = qa_browser._legacy_card_owner("e" * 64)
        draft_id = runtime.DEFAULT_DRAFT_STORE.create(
            owner,
            {
                "targets": ["anki"],
                "identity": {"local_id": "legacy-1"},
                "drafts": {"cards": [{"type": "basic", "front": "Q", "back": "A"}]},
            },
        )
        rejected = qa_browser._commit_legacy_card_draft(
            draft_id=draft_id,
            target="anki",
            owner=other,
        )
        self.assertFalse(rejected["ok"])
        self.assertIn("校验", rejected["error"])

    def test_old_html_requires_preview_and_explicit_commit(self):
        source = (CORE / "qa_browser.py").read_text("utf-8")
        self.assertIn("草稿预览（尚未写入）", source)
        self.assertIn("api/card-update-commit", source)
        self.assertIn("window.confirm(promptText)", source)
        self.assertIn("owner_token: cardDraftOwnerToken", source)
        self.assertIn("_prepare_legacy_card_draft(", source)
        self.assertIn("_commit_legacy_card_draft(", source)
        self.assertNotIn("legacy-one-shot-cli", source)
        self.assertNotIn("delete_original", source)

    def test_assistant_and_legacy_page_share_one_commit_coordinator(self):
        legacy_source = (CORE / "qa_browser.py").read_text("utf-8")
        assistant_source = (SERVER / "assistant.py").read_text("utf-8")

        self.assertIn(
            "runtime.commit_card_improvement_draft(",
            legacy_source,
        )
        self.assertIn(
            "runtime.commit_card_improvement_draft(",
            assistant_source,
        )
        self.assertNotIn("def _commit_legacy_note_draft", legacy_source)
        self.assertNotIn("_legacy_card_commit_lock", legacy_source)
        self.assertNotIn("def _atomic_write_text", legacy_source)
        self.assertIn("runtime.atomic_replace_text(", legacy_source)


if __name__ == "__main__":
    unittest.main()
