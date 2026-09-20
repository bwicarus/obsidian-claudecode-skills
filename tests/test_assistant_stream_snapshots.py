"""Turn-local text, tool and generated-card updates without history reloads."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from flask import Flask

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "_server_deploy"))
import assistant
import reader_card_contract


class AssistantStreamSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="reader-stream-contract-")
        root = Path(self.directory.name)
        self.publish = Mock(return_value=1)
        self.patches = [
            patch.object(assistant, "_CONVO_DIR", root / "history"),
            patch.object(assistant, "_REVIEW_CONVO_DIR", root / "review"),
            patch.object(assistant, "_CONVO_ARCHIVE_DIR", root / "archive"),
            patch.object(assistant, "_LIVE_TURN", {}),
            patch.object(assistant, "_EXTERNAL_STREAM_STATE", {}),
            patch.dict(sys.modules, {"reader_events": SimpleNamespace(publish=self.publish)}),
        ]
        for operation in self.patches:
            operation.start()
        app = Flask(__name__)
        app.secret_key = "stream-contract"
        app.register_blueprint(assistant.bp)
        self.client = app.test_client()
        with self.client.session_transaction() as session:
            session["user_id"] = "test-user"

    def tearDown(self):
        for operation in reversed(self.patches):
            operation.stop()
        self.directory.cleanup()

    def log(self, **values):
        return self.client.post("/api/assistant/log", json={
            "turn_id": "turn-a", "thread_id": "thread-a", "via": "codex-voice", **values})

    def rows(self):
        return assistant._convo_load("test-user")

    def event(self):
        return self.publish.call_args.args[3]

    def test_parts_update_contains_only_current_turn_and_does_not_seal_text(self):
        self.log(turn_id="other", assistant="earlier")
        response = self.log(upsert_only=True, create_if_missing=True, notify_sidebar=True,
                            streamRevision=1, item_id="parts:turn-a", parts=[{
                                "kind": "tool", "tool": "reader_card", "id": "call-a",
                                "item_id": "item-a", "call_id": "call-a", "status": "running"}])
        self.assertEqual(response.status_code, 200)
        event = self.event()
        self.assertEqual(event["stream"], "parts")
        self.assertEqual(event["item_id"], "parts:turn-a")
        self.assertEqual([m["turn_id"] for m in event["messages"]], ["turn-a"])
        row = event["messages"][0]
        self.assertFalse(row["stream_final"])
        self.assertNotIn("_stream_writers", row)
        self.assertEqual(row["parts"][0]["call_id"], "call-a")
        self.assertEqual(row["parts"][0]["status"], "running")

    def test_user_only_log_cannot_take_assistant_upsert_early_return(self):
        self.log(assistant="answer", item_id="assistant-a", streamRevision=1, stream_final=True)
        self.log(user="my question", item_id="user-a", streamRevision=1, stream_final=True)
        self.log(user="my question", item_id="user-a", streamRevision=1, stream_final=True)
        self.assertEqual([(m["role"], m["content"]) for m in self.rows()],
                         [("assistant", "answer"), ("user", "my question")])
        self.assertEqual(len(self.event()["messages"]), 2)
        self.assertEqual(self.event()["role"], "user")

    def test_partial_runner_snapshot_keeps_all_card_origins_and_distinct_calls(self):
        common = dict(upsert_only=True, create_if_missing=True, notify_sidebar=True, item_id="parts:turn-a")
        self.log(**common, streamRevision=1, parts=[
            {"kind": "tool", "tool": "reader_card", "call_id": "one", "status": "completed"},
            {"kind": "card", "id": "runner-card", "card": {"kind": "fact", "data": {"answer": "one"}}}])
        self.log(**common, via="voice", streamRevision=1, parts=[
            {"kind": "cards", "id": "app-card", "gid": "card_stable", "origin": "app", "cards": [{"front": "front", "back": "back"}]},
            {"kind": "text", "id": "voice-item", "origin": "voice", "text": "spoken"}])
        self.log(**common, streamRevision=2, parts=[
            {"kind": "tool", "tool": "reader_card", "call_id": "one", "status": "running"},
            {"kind": "tool", "tool": "reader_card", "call_id": "two", "status": "running"}])
        parts = self.rows()[0]["parts"]
        self.assertEqual({p.get("id") for p in parts if p.get("id")}, {"runner-card", "app-card", "voice-item"})
        self.assertEqual([(p["call_id"], p["status"]) for p in parts if p["kind"] == "tool"],
                         [("one", "completed"), ("two", "running")])
        self.assertEqual(next(p for p in parts if p["kind"] == "cards")["gid"], "card_stable")

    def test_stale_revision_is_ignored_per_writer_and_item(self):
        self.log(assistant="final", item_id="a", streamRevision=10, stream_final=True)
        stale = self.log(assistant="old", item_id="a", streamRevision=9, stream_final=True).get_json()
        draft = self.log(assistant="late draft", item_id="a", streamRevision=11, stream_final=False).get_json()
        self.assertTrue(stale["ignored"])
        self.assertTrue(draft["ignored"])
        self.assertEqual(self.rows()[0]["content"], "final")
        self.log(assistant="next voice item", item_id="voice-b", streamRevision=1, stream_final=True)
        self.assertEqual(self.rows()[0]["content"], "next voice item")
        self.log(via="voice", item_id="a", streamRevision=1, stream_final=False,
                 parts=[{"kind": "text", "id": "app-text", "text": "from App"}])
        self.assertEqual(self.rows()[0]["parts"][0]["text"], "from App")

    def test_duplicate_final_keeps_one_row_and_republishes_stored_payload(self):
        payload = dict(assistant="canonical", item_id="a", streamRevision=4, stream_final=True,
                       parts=[{"kind": "cards", "cards": [{"front": "f", "back": "b"}]}])
        self.log(**payload)
        first = self.rows()[0]
        response = self.log(**{**payload, "assistant": "different same revision"}).get_json()
        self.assertTrue(response["replayed"])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0], first)
        self.assertEqual(self.event()["messages"][0]["content"], "canonical")

    def test_legacy_card_update_after_response_final_is_not_sealed_as_whole_turn(self):
        self.log(via="voice", assistant="legacy final")
        response = self.log(via="voice", upsert_only=True, notify_sidebar=True,
                            parts=[{"kind": "cards", "gid": "legacy_card", "cards": [{"front": "front"}]}])
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json().get("ignored", False))
        self.assertEqual(self.rows()[0]["parts"][0]["gid"], "legacy_card")
        self.assertEqual(self.event()["stream"], "parts")

    def test_contract_validation_also_runs_on_existing_turn(self):
        self.log(assistant="keep me")
        before = self.rows()
        response = self.log(parts=[{"kind": "tool", "call_id": "bad id", "status": "running"}])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["where"], "parts")
        self.assertEqual(self.rows(), before)

    def test_same_turn_id_in_another_thread_is_not_in_snapshot(self):
        self.log(thread_id="thread-other", assistant="other thread", item_id="a", streamRevision=1, stream_final=True)
        self.log(assistant="current thread", item_id="a", streamRevision=1, stream_final=True)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual([m["content"] for m in self.event()["messages"]], ["current thread"])

    def test_write_failure_is_not_reported_as_saved(self):
        with patch.object(assistant.os, "replace", side_effect=OSError("write failed")):
            response = self.log(assistant="not durable", streamRevision=1, stream_final=True)
        self.assertEqual(response.status_code, 500)
        self.assertFalse(response.get_json()["ok"])
        self.assertEqual(self.rows(), [])
        self.publish.assert_not_called()

    def test_publish_failure_can_retry_final_without_losing_saved_card(self):
        payload = dict(assistant="saved", item_id="a", streamRevision=2, stream_final=True)
        self.publish.side_effect = RuntimeError("event unavailable")
        response = self.log(**payload)
        self.assertEqual(response.status_code, 503)
        self.assertTrue(response.get_json()["saved"])
        self.publish.side_effect = None
        response = self.log(**payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.event()["stream"], "final")

    def test_stream_final_blocks_late_draft_and_preserves_user_identity(self):
        base = {"turn_id": "u1", "thread_id": "thread-a", "item_id": "user-item", "role": "user"}
        self.client.post("/api/assistant/stream", json={**base, "stream": "delta", "streamRevision": 1, "content": "hel"})
        self.client.post("/api/assistant/stream", json={**base, "stream": "final", "streamRevision": 2, "content": "hello"})
        response = self.client.post("/api/assistant/stream", json={**base, "stream": "delta", "streamRevision": 3, "content": "late"})
        self.assertTrue(response.get_json()["ignored"])
        self.assertEqual(self.publish.call_count, 2)
        self.assertEqual(self.event()["role"], "user")
        self.assertEqual(self.event()["item_id"], "user-item")
        self.assertEqual(assistant._LIVE_TURN, {})

    def test_voice_wire_origin_is_shared_between_stream_and_log_and_long_text_survives(self):
        text = "长" * 12000
        wire = {"turn_id": "u1", "thread_id": "thread-a", "item_id": "user-item", "origin": "voice"}
        self.client.post("/api/assistant/stream", json={**wire, "role": "user", "stream": "delta",
                                                       "streamRevision": 1, "content": text})
        self.assertEqual(self.event()["content"], text)
        self.assertEqual(self.event()["origin"], "voice")
        self.log(**wire, via="voice", user=text, streamRevision=2, stream_final=True)
        self.assertEqual(self.rows()[0]["content"], text)
        self.assertEqual(self.event()["origin"], "voice")
        response = self.client.post("/api/assistant/stream", json={**wire, "role": "user", "stream": "delta",
                                                                   "streamRevision": 3, "content": "late"})
        self.assertTrue(response.get_json()["ignored"])
        response = self.log(origin="not-a-writer", assistant="bad")
        self.assertEqual(response.status_code, 400)
        text = "长" * 32000
        response = self.log(origin="runner", item_id="backend-text", streamRevision=1, stream_final=True,
                            assistant=text, parts=[{"kind": "text", "item_id": "backend-text", "text": text}])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.rows()[-1]["parts"][0]["text"], text)

    def test_empty_turn_final_notifies_without_creating_blank_message(self):
        response = self.log(upsert_only=True, turn_end=True, stream_final=True,
                            item_id="turn:turn-a", streamRevision=10)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["event_sent"])
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.event()["messages"], [])
        self.assertEqual(self.event()["stream"], "final")

    def test_final_absorbs_only_old_assistant_rows_and_includes_ids(self):
        self.log(turn_id="old", user="user stays", assistant="old voice")
        self.log(assistant="joined", item_id="new", streamRevision=1, stream_final=True, absorb=["old"])
        self.assertEqual([(m["role"], m["content"]) for m in self.rows()], [("user", "user stays"), ("assistant", "joined")])
        self.assertEqual(self.event()["absorbed_ids"], ["old"])

    def test_part_identity_contract_and_stable_gid(self):
        part = {"kind": "tool", "tool": "reader_card", "id": "one", "item_id": "two", "call_id": "three", "status": "completed"}
        result = reader_card_contract.validate_parts([part])[0]
        for key in ("id", "item_id", "call_id", "status"):
            self.assertEqual(result[key], part[key])
        with self.assertRaises(ValueError):
            reader_card_contract.validate_parts([{**part, "status": "invented"}])
        with self.assertRaises(ValueError):
            reader_card_contract.validate_parts([{**part, "item_id": "x" * 161}])
        cards = [{"kind": "cards", "gid": "card_existing", "cards": [{"front": "f"}]}]
        self.assertEqual(assistant._sanitize_ext_parts(cards)[0]["gid"], "card_existing")


if __name__ == "__main__":
    unittest.main()
