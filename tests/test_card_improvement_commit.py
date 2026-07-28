"""Commit-boundary contracts for assistant card-improvement drafts."""

from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from flask import Flask, jsonify


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "_server_deploy"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import assistant  # noqa: E402
import card_improvement_runtime as runtime  # noqa: E402


class _FakePdf:
    def __init__(self):
        self.calls = []
        self.old_card = {
            "entity_id": "card_abc123",
            "front": "旧问题",
            "back": "旧答案",
        }
        self.response = {
            "ok": True,
            "added": 1,
            "note_ids": [2468],
        }
        self.status = 200
        self.delay = 0.0

    def pdf_api_anki_add_cards(self, body_override=None):
        if self.delay:
            time.sleep(self.delay)
        self.calls.append(copy.deepcopy(body_override))
        return jsonify(copy.deepcopy(self.response)), self.status


class CardImprovementCommitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(
            prefix="assistant-card-commit-"
        )
        self.vault = Path(self.tmp.name) / "vault"
        self.vault.mkdir()
        self.note = self.vault / "知识" / "原笔记.md"
        self.note.parent.mkdir()
        self.note.write_text("原始笔记\n", "utf-8")

        self.old_vault = assistant.VAULT_ROOT
        self.old_pdf = assistant._pdf
        self.old_store = runtime.DEFAULT_DRAFT_STORE
        assistant.VAULT_ROOT = self.vault
        self.fake_pdf = _FakePdf()
        assistant._pdf = lambda: self.fake_pdf
        runtime.DEFAULT_DRAFT_STORE = runtime.CardImprovementDraftStore(
            secret=b"commit-contract-secret".ljust(32, b"x")
        )

        self.app = Flask(__name__)
        self.app.secret_key = "card-commit-contract"
        self.app.register_blueprint(assistant.bp)
        self.client = self.app.test_client()

    def tearDown(self):
        assistant.VAULT_ROOT = self.old_vault
        assistant._pdf = self.old_pdf
        runtime.DEFAULT_DRAFT_STORE = self.old_store
        self.tmp.cleanup()

    def _login(self, uid="alice"):
        with self.client.session_transaction() as flask_session:
            flask_session["user_id"] = uid

    def _create(
        self,
        *,
        owner="assistant:alice",
        targets=("anki",),
        cards=None,
        note_content=None,
        base_text=None,
    ):
        drafts = {}
        if "anki" in targets:
            drafts["cards"] = copy.deepcopy(
                cards
                or [{
                    "type": "basic",
                    "front": "冻结后的新问题",
                    "back": "冻结后的新答案",
                }]
            )
        if "note" in targets:
            base = self.note.read_text("utf-8") if base_text is None else base_text
            drafts["note"] = {
                "content": note_content or (base + "\n补充内容\n"),
                "base_sha256": hashlib.sha256(
                    base.encode("utf-8")
                ).hexdigest(),
                "base_chars": len(base),
                "verbosity": "verbose",
            }
        return runtime.DEFAULT_DRAFT_STORE.create(
            owner,
            {
                "targets": list(targets),
                "identity": {
                    "entity_id": "card_abc123",
                    "entity_index": 0,
                    "source_note": "知识/原笔记.md",
                },
                "drafts": drafts,
                "trace": [],
                "runner": {},
            },
        )

    def test_auth_owner_and_target_are_all_enforced(self):
        draft_id = self._create()
        unauthenticated = self.client.post(
            "/api/assistant/card-improvement-commit",
            json={"draft_id": draft_id, "target": "anki"},
        )
        self.assertEqual(unauthenticated.status_code, 401)

        self._login("bob")
        wrong_owner = self.client.post(
            "/api/assistant/card-improvement-commit",
            json={"draft_id": draft_id, "target": "anki"},
        )
        self.assertEqual(wrong_owner.status_code, 400)
        self.assertIn("draft", wrong_owner.get_json()["error"])

        self._login("alice")
        invalid_target = self.client.post(
            "/api/assistant/card-improvement-commit",
            json={"draft_id": draft_id, "target": "all"},
        )
        self.assertEqual(invalid_target.status_code, 400)
        self.assertIn("anki 或 note", invalid_target.get_json()["error"])

        missing_target = self.client.post(
            "/api/assistant/card-improvement-commit",
            json={"draft_id": draft_id, "target": "note"},
        )
        self.assertEqual(missing_target.status_code, 400)
        self.assertIn("不包含", missing_target.get_json()["error"])
        self.assertEqual(self.fake_pdf.calls, [])

    def test_anki_commit_uses_only_frozen_draft_and_is_idempotent(self):
        frozen = [{
            "type": "basic",
            "front": "可信冻结问题",
            "back": "可信冻结答案",
        }]
        draft_id = self._create(cards=frozen)
        original_card = copy.deepcopy(self.fake_pdf.old_card)
        self._login()

        first = self.client.post(
            "/api/assistant/card-improvement-commit",
            json={
                "draft_id": draft_id,
                "target": "anki",
                # These fields are deliberately hostile.  The commit boundary
                # must ignore all client-supplied content and identity.
                "cards": [{
                    "front": "伪造客户端问题",
                    "back": "伪造客户端答案",
                }],
                "entity_id": "card_bad999",
                "entity_index": 99,
            },
        )
        self.assertEqual(first.status_code, 200, first.get_data(as_text=True))
        self.assertEqual(len(self.fake_pdf.calls), 1)
        submitted = self.fake_pdf.calls[0]
        self.assertEqual(submitted["cards"], frozen)
        self.assertEqual(submitted["entity_id"], "card_abc123")
        self.assertEqual(submitted["card_index"], 0)
        self.assertRegex(submitted["aid"], r"^ci_[a-f0-9]{48}$")
        self.assertEqual(self.fake_pdf.old_card, original_card)

        second = self.client.post(
            "/api/assistant/card-improvement-commit",
            json={"draft_id": draft_id, "target": "anki"},
        )
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.get_json()["dedup"])
        self.assertEqual(len(self.fake_pdf.calls), 1)
        self.assertEqual(
            runtime.DEFAULT_DRAFT_STORE.get(
                draft_id,
                "assistant:alice",
            )["committed"],
            ["anki"],
        )

    def test_failed_anki_write_is_not_marked_committed(self):
        draft_id = self._create()
        self.fake_pdf.response = {
            "ok": False,
            "error": "AnkiConnect 不可达",
        }
        self.fake_pdf.status = 502
        self._login()

        response = self.client.post(
            "/api/assistant/card-improvement-commit",
            json={"draft_id": draft_id, "target": "anki"},
        )
        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            runtime.DEFAULT_DRAFT_STORE.get(
                draft_id,
                "assistant:alice",
            )["committed"],
            [],
        )

    def test_concurrent_anki_commits_cross_the_side_effect_boundary_once(self):
        draft_id = self._create()
        self.fake_pdf.delay = 0.04
        starts = threading.Barrier(8)
        results = []
        errors = []
        result_lock = threading.Lock()

        def commit():
            try:
                starts.wait(timeout=2)
                with self.app.app_context():
                    value = assistant._commit_card_improvement_for_user(
                        {"draft_id": draft_id, "target": "anki"},
                        "alice",
                    )
                with result_lock:
                    results.append(value)
            except Exception as error:
                with result_lock:
                    errors.append(error)

        workers = [threading.Thread(target=commit) for _ in range(8)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=3)

        self.assertFalse(
            any(worker.is_alive() for worker in workers),
            "concurrent commit workers must not deadlock",
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 8)
        self.assertEqual(len(self.fake_pdf.calls), 1)
        self.assertEqual(
            sum(1 for result in results if result.get("dedup") is True),
            7,
        )
        self.assertEqual(
            runtime.DEFAULT_DRAFT_STORE.get(
                draft_id,
                "assistant:alice",
            )["committed"],
            ["anki"],
        )

    def test_note_commit_is_atomic_and_repeated_commit_is_idempotent(self):
        content = "原始笔记\n\n确认后的补充内容\n"
        draft_id = self._create(
            targets=("note",),
            note_content=content,
        )
        self._login()
        original_replace = os.replace

        with patch.object(
            runtime.os,
            "replace",
            wraps=original_replace,
        ) as replace:
            first = self.client.post(
                "/api/assistant/card-improvement-commit",
                json={"draft_id": draft_id, "target": "note"},
            )
            self.assertEqual(
                first.status_code,
                200,
                first.get_data(as_text=True),
            )
            self.assertEqual(replace.call_count, 1)
            self.assertEqual(self.note.read_text("utf-8"), content)

            second = self.client.post(
                "/api/assistant/card-improvement-commit",
                json={"draft_id": draft_id, "target": "note"},
            )
            self.assertEqual(second.status_code, 200)
            self.assertTrue(second.get_json()["dedup"])
            self.assertEqual(replace.call_count, 1)

        leftovers = list(
            self.note.parent.glob(
                "." + self.note.name + ".card-improvement-*.tmp"
            )
        )
        self.assertEqual(leftovers, [])
        self.assertEqual(
            runtime.DEFAULT_DRAFT_STORE.get(
                draft_id,
                "assistant:alice",
            )["committed"],
            ["note"],
        )

    def test_note_base_sha_conflict_preserves_newer_content(self):
        base = self.note.read_text("utf-8")
        draft_id = self._create(
            targets=("note",),
            note_content=base + "\nAI 草稿\n",
            base_text=base,
        )
        newer = base + "\n用户在预览后新增的内容\n"
        self.note.write_text(newer, "utf-8")
        self._login()

        response = self.client.post(
            "/api/assistant/card-improvement-commit",
            json={"draft_id": draft_id, "target": "note"},
        )
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.get_json()["conflict"])
        self.assertEqual(self.note.read_text("utf-8"), newer)
        self.assertEqual(
            runtime.DEFAULT_DRAFT_STORE.get(
                draft_id,
                "assistant:alice",
            )["committed"],
            [],
        )


if __name__ == "__main__":
    unittest.main()
