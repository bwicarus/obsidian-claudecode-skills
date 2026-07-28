from __future__ import annotations

from pathlib import Path
import copy
import sys
import time
import unittest

from flask import Flask


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "_server_deploy"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import assistant  # noqa: E402


CARDS_JSON = (
    '[{"type":"basic","front":"改进问题","back":"改进答案",'
    '"reason":"补足困惑"}]'
)


class FakeCodexApp:
    def __init__(self):
        self.started = []
        self.turns = []
        self.closed = []

    def thread_start(self, model, service_tier=""):
        self.started.append(
            (model, service_tier) if service_tier else model
        )
        return "t-api"

    def turn_stream(
        self,
        thread_id,
        prompt,
        effort,
        timeout=180,
        service_tier="",
    ):
        self.turns.append(
            (thread_id, prompt, effort, timeout, service_tier)
        )
        yield CARDS_JSON

    def thread_close(self, thread_id):
        self.closed.append(thread_id)


class FakePdf:
    def _asset_load(self):
        return {
            "card_abc123": {
                "kind": "cards",
                "src": "vbook:math.pdf#p3",
                "data": [
                    {
                        "type": "basic",
                        "front": "可信问题",
                        "back": "可信答案",
                    }
                ],
                "states": {"0": {"_nid": 1001}},
            }
        }


class CardImprovementAssistantApiTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = "test"
        self.app.register_blueprint(assistant.bp)
        self.client = self.app.test_client()
        self.old_pdf = assistant._pdf
        self.old_codex = assistant._codex_app
        self.old_resolve = assistant._resolve
        self.old_codex_catalog = copy.deepcopy(
            assistant._codex_catalog_cache
        )
        self.fake_codex = FakeCodexApp()
        assistant._pdf = lambda: FakePdf()
        assistant._codex_app = self.fake_codex
        assistant._codex_catalog_cache.clear()
        assistant._codex_catalog_cache.update({
            "ts": time.time(),
            "verified": True,
            "error": "",
            "models": {
                "gpt-5.6-luna": {
                    "available": True,
                    "depths": ["low"],
                    "service_tiers": ["priority"],
                    "priority": True,
                    "fast": True,
                },
                "gpt-5.3-codex-spark": {
                    "available": True,
                    "depths": ["low"],
                    "service_tiers": [],
                    "priority": False,
                    "fast": False,
                },
            },
        })
        assistant._resolve = lambda action, uid: {
            "backend": "codex",
            "variant": "gpt-5.6-luna",
            "depth": "low",
        }

    def tearDown(self):
        assistant._pdf = self.old_pdf
        assistant._codex_app = self.old_codex
        assistant._resolve = self.old_resolve
        assistant._codex_catalog_cache.clear()
        assistant._codex_catalog_cache.update(self.old_codex_catalog)

    def login(self):
        with self.client.session_transaction() as session:
            session["user_id"] = "only-user"

    def test_requires_login(self):
        response = self.client.post(
            "/api/assistant/card-improvement-draft",
            json={},
        )
        self.assertEqual(response.status_code, 401)

    def test_entity_registry_is_authoritative_and_native_thread_is_used(self):
        self.login()
        response = self.client.post(
            "/api/assistant/card-improvement-draft",
            json={
                "entity_id": "card_abc123",
                "entity_index": 0,
                "card": {
                    "entity_id": "card_abc123",
                    "front": "伪造问题",
                    "back": "伪造答案",
                },
                "target": "anki",
                "pairs": [{"question": "为什么？", "answer": "有效解释"}],
            },
        )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        body = response.get_json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["draft_id"].startswith("d_"))
        self.assertEqual(body["identity"]["entity_id"], "card_abc123")
        self.assertEqual(body["identity"]["entity_index"], 0)
        self.assertEqual(body["drafts"]["cards"][0]["front"], "改进问题")
        self.assertEqual(self.fake_codex.started, ["gpt-5.6-luna"])
        self.assertEqual(self.fake_codex.closed, ["t-api"])
        self.assertEqual(len(self.fake_codex.turns), 1)
        prompt = self.fake_codex.turns[0][1]
        self.assertIn("可信问题", prompt)
        self.assertNotIn("伪造问题", prompt)

    def test_missing_entity_is_rejected_instead_of_using_spoofed_card(self):
        self.login()
        response = self.client.post(
            "/api/assistant/card-improvement-draft",
            json={
                "entity_id": "card_bad999",
                "card": {"front": "Q", "back": "A"},
                "target": "anki",
                "pairs": [{"answer": "A"}],
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("找不到", response.get_json()["error"])

    def test_supported_fast_profile_reaches_card_improvement_native_turn(self):
        self.login()
        assistant._resolve = lambda action, uid: {
            "backend": "codex",
            "variant": "gpt-5.6-luna",
            "depth": "low",
            "fast": True,
        }
        response = self.client.post(
            "/api/assistant/card-improvement-draft",
            json={
                "entity_id": "card_abc123",
                "entity_index": 0,
                "target": "anki",
                "pairs": [{"question": "为什么？", "answer": "有效解释"}],
            },
        )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(
            self.fake_codex.started,
            [("gpt-5.6-luna", "priority")],
        )
        self.assertEqual(self.fake_codex.turns[0][4], "priority")
        runner = response.get_json()["runner"]
        self.assertEqual(runner["service_tier_request"], "priority")
        self.assertEqual(runner["service_tier_effective"], "priority")

    def test_spark_profile_cannot_turn_fast_into_priority(self):
        self.login()
        assistant._resolve = lambda action, uid: {
            "backend": "codex",
            "variant": "gpt-5.3-codex-spark",
            "depth": "low",
            "fast": True,
        }
        response = self.client.post(
            "/api/assistant/card-improvement-draft",
            json={
                "entity_id": "card_abc123",
                "entity_index": 0,
                "target": "anki",
                "pairs": [{"question": "为什么？", "answer": "有效解释"}],
            },
        )

        self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
        self.assertEqual(
            self.fake_codex.started,
            ["gpt-5.3-codex-spark"],
        )
        self.assertEqual(self.fake_codex.turns[0][4], "")
        runner = response.get_json()["runner"]
        self.assertEqual(runner["service_tier_request"], "")
        self.assertEqual(runner["service_tier_effective"], "")


if __name__ == "__main__":
    unittest.main()
