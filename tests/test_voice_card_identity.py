"""卡片收藏接口必须往返保留跨宿主共享状态所用的稳定编号。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "_server_deploy"))

import assistant  # noqa: E402


class VoiceCardIdentityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir = assistant._VCARD_DIR
        assistant._VCARD_DIR = Path(self.tmp.name) / "voice-cards"

        app = Flask(__name__)
        app.secret_key = "voice-card-test"
        assistant.register_assistant(app)
        self.app = app
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["user_id"] = "identity-test-user"

    def tearDown(self) -> None:
        assistant._VCARD_DIR = self.old_dir
        self.tmp.cleanup()

    def _add(self, card: dict) -> dict:
        response = self.client.post(
            "/api/assistant/voice-cards",
            json={"op": "add", "card": card},
        )
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def _cards(self) -> list[dict]:
        response = self.client.get("/api/assistant/voice-cards")
        self.assertEqual(response.status_code, 200)
        return response.get_json()["cards"]

    def test_add_and_reload_preserve_cid_and_gid(self) -> None:
        card = {"front": "Q", "back": "A", "card_id": 17}
        self._add({
            "id": "favorite-row-1",
            "label": "学习卡",
            "kind": "cards",
            "cid": "card_shared-001",
            "gid": "card_shared-001",
            "raw": json.dumps([card]),
        })

        [saved] = self._cards()
        self.assertEqual(saved["cid"], "card_shared-001")
        self.assertEqual(saved["gid"], "card_shared-001")
        self.assertEqual(saved["payload"], {
            "version": 1,
            "kind": "cards",
            "cards": [card],
        })
        self.assertNotIn("raw", saved)

    def test_structured_cards_preserve_complete_identity_source_and_projection(self) -> None:
        long_back = "完整背面" * 5000  # 明确越过旧 raw[:20000] 截断点
        card = {
            "id": "anki-card-731",
            "card_id": 731,
            "note_id": 91,
            "entity_id": "entity-causal-7",
            "entity_index": 3,
            "source_note_id": "note-源-17",
            "source": {
                "file": "知识/矩阵.md",
                "heading": "相似变换",
                "href": "obsidian://open?vault=学习&file=矩阵",
            },
            "front": "为什么相似矩阵有相同特征值？",
            "back": long_back,
            "_displayFrontHtml": "<b>为什么</b>",
            "_displayBackHtml": "<p>投影背面（来源已隐藏）</p>",
            "_st": "review",
            "_showBack": True,
            "_ratingPending": True,
            "_syncPending": False,
            "_next": None,
        }
        payload = {"version": 1, "kind": "cards", "cards": [card]}
        self._add({
            "id": "anki_entity_731",
            "label": "学习卡",
            "kind": "cards",
            "cid": "anki_entity_731",
            "gid": "anki_entity_731",
            "payload": payload,
            "raw": "这个字段不得成为第二份真相",
        })

        [saved] = self._cards()
        self.assertEqual(saved["payload"], payload)
        self.assertEqual(saved["payload"]["cards"][0]["back"], long_back)
        self.assertEqual(saved["payload"]["cards"][0]["source"], card["source"])
        self.assertEqual(
            saved["payload"]["cards"][0]["_displayBackHtml"],
            card["_displayBackHtml"],
        )
        self.assertIs(saved["payload"]["cards"][0]["_ratingPending"], True)
        self.assertIs(saved["payload"]["cards"][0]["_syncPending"], False)
        self.assertEqual(saved["cid"], "anki_entity_731")
        self.assertEqual(saved["gid"], "anki_entity_731")
        self.assertNotIn("raw", saved)

    def test_malformed_or_oversized_cards_fail_closed_without_storage(self) -> None:
        malformed = self.client.post(
            "/api/assistant/voice-cards",
            json={
                "op": "add",
                "card": {
                    "id": "bad-json",
                    "kind": "cards",
                    "cid": "bad-json",
                    "gid": "bad-json",
                    "raw": '[{"front":"missing end"',
                },
            },
        )
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(
            malformed.get_json()["code"],
            "invalid_cards_payload",
        )

        oversized = self.client.post(
            "/api/assistant/voice-cards",
            json={
                "op": "add",
                "card": {
                    "id": "too-large",
                    "kind": "cards",
                    "cid": "too-large",
                    "gid": "too-large",
                    "payload": {
                        "version": 1,
                        "kind": "cards",
                        "cards": [{"front": "Q", "back": "x" * 275000}],
                    },
                },
            },
        )
        self.assertEqual(oversized.status_code, 413)
        self.assertEqual(
            oversized.get_json()["code"],
            "cards_payload_too_large",
        )
        self.assertEqual(self._cards(), [])

    def test_plain_html_and_text_keep_legacy_raw_contract(self) -> None:
        raw = "<p>" + ("x" * 25000) + "</p>"
        self._add({
            "id": "plain-html",
            "label": "普通工具结果",
            "kind": "weather",
            "raw": raw,
            "isHtml": True,
        })

        [saved] = self._cards()
        self.assertEqual(saved["raw"], raw[:20000])
        self.assertNotIn("payload", saved)

    def test_newer_review_state_dominates_late_pending_write(self) -> None:
        base = {
            "id": "anki_revision_42",
            "label": "学习卡",
            "kind": "cards",
            "cid": "anki_revision_42",
            "gid": "anki_revision_42",
        }
        pending_card = {
            "card_id": 42,
            "front": "Q",
            "back": "A",
            "_st": "done",
            "_ratingPending": True,
            "_syncPending": False,
            "_next": None,
        }
        accepted_card = {
            **pending_card,
            "_ratingPending": False,
            "_next": {"interval": 4},
        }
        self._add({
            **base,
            "revision": 2,
            "payload": {
                "version": 1,
                "kind": "cards",
                "cards": [accepted_card],
            },
        })
        stale = self.client.post(
            "/api/assistant/voice-cards",
            json={
                "op": "add",
                "card": {
                    **base,
                    "revision": 1,
                    "payload": {
                        "version": 1,
                        "kind": "cards",
                        "cards": [pending_card],
                    },
                },
            },
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.get_json()["code"], "stale_cards_revision")

        [saved] = self._cards()
        self.assertEqual(saved["revision"], 2)
        self.assertIs(
            saved["payload"]["cards"][0]["_ratingPending"],
            False,
        )
        self.assertEqual(
            saved["payload"]["cards"][0]["_next"],
            {"interval": 4},
        )

    def test_concurrent_pending_and_accepted_writes_finish_at_newer_state(self) -> None:
        barrier = threading.Barrier(2)

        def write(revision: int, pending: bool):
            client = self.app.test_client()
            with client.session_transaction() as sess:
                sess["user_id"] = "identity-test-user"
            barrier.wait(timeout=2)
            return client.post(
                "/api/assistant/voice-cards",
                json={
                    "op": "add",
                    "card": {
                        "id": "anki_concurrent_77",
                        "kind": "cards",
                        "cid": "anki_concurrent_77",
                        "gid": "anki_concurrent_77",
                        "revision": revision,
                        "payload": {
                            "version": 1,
                            "kind": "cards",
                            "cards": [{
                                "card_id": 77,
                                "front": "Q",
                                "back": "A",
                                "_st": "done",
                                "_ratingPending": pending,
                                "_next": None if pending else {"interval": 7},
                            }],
                        },
                    },
                },
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            pending_future = pool.submit(write, 1, True)
            accepted_future = pool.submit(write, 2, False)
            statuses = {
                pending_future.result(timeout=5).status_code,
                accepted_future.result(timeout=5).status_code,
            }
        self.assertTrue(statuses.issubset({200, 409}), statuses)
        self.assertIn(200, statuses)

        [saved] = self._cards()
        self.assertEqual(saved["revision"], 2)
        self.assertIs(
            saved["payload"]["cards"][0]["_ratingPending"],
            False,
        )

    def test_legacy_update_does_not_erase_existing_identity(self) -> None:
        self._add({
            "id": "favorite-row-2",
            "label": "初版",
            "cid": "cstable-2",
            "raw": "before",
        })
        # 旧客户端更新内容时并不知道 cid 字段；稳定编号仍须由已有记录继承。
        self._add({
            "id": "favorite-row-2",
            "label": "更新版",
            "raw": "after",
        })

        [saved] = self._cards()
        self.assertEqual(saved["cid"], "cstable-2")
        self.assertEqual(saved["raw"], "after")


if __name__ == "__main__":
    unittest.main()
