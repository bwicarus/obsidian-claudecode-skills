import tempfile
import unittest
from pathlib import Path

from selection import SelectionError
from selection_tools import dispatch_selection, dispatch_selection_safe
from voice import VoiceSession


class FakeSelectionService:
    def __init__(self, root=None):
        root = Path(root or ".").resolve()
        self.data_store = type("Store", (), {"root": root / "market"})()
        self.state_root = root / "state"
        self.calls = []

    def catalog(self):
        self.calls.append(("catalog",))
        return {"criteria": [{"id": "price_below_limit"}]}

    def load_library(self, owner):
        self.calls.append(("library", owner))
        return {"revision": 4, "groups": [{"id": "g", "codes": [f"{i:06d}" for i in range(80)],
                                             "legacySource": "old", "legacyDefinition": {"codes": ["secret"]}}],
                "presets": [{"id": "p", "definition": {"groups": []},
                             "legacySource": "old", "legacyDefinition": {"private": True}}]}

    def evaluate(self, owner, request):
        self.calls.append(("evaluate", owner, request))
        return {"revision": 4, "asOf": "2026-09-19", "matched": 60,
                "items": [{"code": f"{i:06d}"} for i in range(50)]}

    def mutate(self, owner, request):
        self.calls.append(("mutate", owner, request))
        return {"success": True, "revision": 5, "requestId": request["requestId"],
                "operation": request["operation"], "library": {"revision": 5, "groups": [], "presets": []}}


class SelectionDispatchTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeSelectionService()

    def test_reads_and_mutation_use_only_trusted_owner(self):
        dispatch_selection(self.service, "apple-owner", "catalog")
        dispatch_selection(self.service, "apple-owner", "library")
        evaluated = dispatch_selection(self.service, "apple-owner", "evaluate", {"groups": []})
        mutated = dispatch_selection(self.service, "apple-owner", "mutate", {
            "requestId": "intent-1", "expectedRevision": 4,
            "operation": "group.create", "payload": {"name": "关注"},
        })

        self.assertEqual(self.service.calls[1], ("library", "apple-owner"))
        self.assertEqual(self.service.calls[2][1], "apple-owner")
        self.assertEqual(self.service.calls[2][2]["limit"], 30)
        self.assertEqual(len(evaluated["result"]["items"]), 30)
        self.assertTrue(evaluated["result"]["itemsTruncated"])
        self.assertEqual(self.service.calls[3][1], "apple-owner")
        self.assertEqual(mutated["result"]["requestId"], "intent-1")

    def test_ai_cannot_supply_owner_at_any_depth(self):
        with self.assertRaises(SelectionError) as failure:
            dispatch_selection(self.service, "real-owner", "mutate", {
                "requestId": "intent-1", "expectedRevision": 0,
                "operation": "group.create", "payload": {"ownerId": "other", "name": "越权"},
            })
        self.assertEqual(failure.exception.code, "owner_not_allowed")
        self.assertEqual(self.service.calls, [])

    def test_library_codes_are_bounded_but_count_is_preserved(self):
        value = dispatch_selection(self.service, "apple-owner", "library")
        group = value["result"]["groups"][0]
        self.assertEqual(len(group["codes"]), 50)
        self.assertEqual(group["codeCount"], 80)
        self.assertTrue(group["codesTruncated"])
        self.assertNotIn("legacySource", group)
        self.assertNotIn("legacyDefinition", group)
        self.assertNotIn("legacySource", value["result"]["presets"][0])
        self.assertNotIn("legacyDefinition", value["result"]["presets"][0])

    def test_business_error_is_structured(self):
        class Conflict(FakeSelectionService):
            def mutate(self, owner, request):
                raise SelectionError("revision_conflict", "资料已更新", 409, {"currentRevision": 8})

        value = dispatch_selection_safe(Conflict(), "apple-owner", "mutate", {
            "requestId": "intent-1", "expectedRevision": 4,
            "operation": "group.create", "payload": {"name": "关注"},
        })
        self.assertFalse(value["ok"])
        self.assertEqual(value["error"], {"code": "revision_conflict", "message": "资料已更新",
                                           "status": 409, "detail": {"currentRevision": 8}})


class VoiceSelectionToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.events = []

        async def emit_json(value):
            self.events.append(value)

        async def emit_audio(_):
            return None

        self.service = FakeSelectionService(self.directory.name)
        self.session = VoiceSession("device", self.directory.name, None, emit_json, emit_audio,
                                    selection_service=self.service, selection_owner="apple-owner")

    async def asyncTearDown(self):
        await self.session.pc.close()
        self.directory.cleanup()

    def test_mcp_config_keeps_owner_out_of_model_arguments(self):
        config = self.session.selection_mcp_config()
        self.assertEqual(config["env"]["STOCKS_SELECTION_OWNER"], "apple-owner")
        self.assertNotIn("apple-owner", " ".join(config["args"]))
        self.assertTrue(config["args"][0].endswith("selection_mcp.py"))

    def test_unmatched_old_transcript_does_not_block_future_notifications(self):
        import time
        self.session.ready.set()
        self.session.speech_receipts = [{'text': '旧播报的转录可能使用不同的数字写法'}]
        self.assertTrue(self.session.can_announce_notification())
        self.session.last_speech_submission = time.monotonic()
        self.assertFalse(self.session.can_announce_notification())

    def test_mcp_config_can_use_isolated_python(self):
        import os
        from unittest.mock import patch
        with patch.dict(os.environ, {"STOCKS_SELECTION_PYTHON": "/opt/selection/bin/python"}):
            self.assertEqual(self.session.selection_mcp_config()["command"],
                             "/opt/selection/bin/python")

    async def test_successful_mutation_emits_receipt_and_refresh_event_once(self):
        self.session.turn_state("turn-1")["requestId"] = "turn-request"
        item = {
            "id": "mcp-call-1", "type": "mcpToolCall", "server": "stocks_selection",
            "tool": "stocks_selection", "status": "completed",
            "arguments": {"action": "mutate"},
            "result": {"content": [], "structuredContent": {"result": {
                "ok": True, "action": "mutate", "result": {
                    "success": True, "revision": 5, "requestId": "intent-1",
                    "operation": "group.create", "library": {"revision": 5},
                },
            }}},
        }
        await self.session.capture_selection_tool("turn-1", item)
        await self.session.capture_selection_tool("turn-1", item)

        tools = self.session.turn_state("turn-1")["tools"]
        self.assertEqual(len(tools), 1)
        self.assertTrue(tools[0]["actionApplied"])
        self.assertEqual(tools[0]["requestId"], "turn-request")
        changed = [event for event in self.events if event.get("type") == "selection.changed"]
        self.assertEqual(changed, [{"type": "selection.changed", "revision": 5,
                                    "requestId": "intent-1", "operation": "group.create"}])

    async def test_failed_mutation_never_emits_change(self):
        item = {
            "id": "mcp-call-2", "type": "mcpToolCall", "server": "stocks_selection",
            "tool": "stocks_selection", "status": "completed",
            "arguments": {"action": "mutate"},
            "result": {"content": [], "structuredContent": {"result": {
                "ok": False, "action": "mutate", "error": {"code": "revision_conflict"},
            }}},
        }
        await self.session.capture_selection_tool("turn-2", item)
        tool = self.session.turn_state("turn-2")["tools"][0]
        self.assertFalse(tool["success"])
        self.assertFalse(tool["actionApplied"])
        self.assertFalse(any(event.get("type") == "selection.changed" for event in self.events))

    async def test_monitor_write_has_verified_receipt_without_selection_change(self):
        item = {
            'id': 'monitor-write', 'type': 'mcpToolCall', 'server': 'stocks_monitor',
            'tool': 'stocks_monitor', 'status': 'completed', 'arguments': {'action': 'mutate'},
            'result': {'structuredContent': {'ok': True, 'action': 'mutate', 'result': {
                'success': True, 'revision': 1, 'requestId': 'monitor-intent', 'operation': 'rule.upsert'}}}}
        await self.session.capture_selection_tool('monitor-turn', item)
        await self.session.capture_selection_tool('monitor-turn', item)
        tools = self.session.turn_state('monitor-turn')['tools']
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]['name'], 'stocks_monitor')
        self.assertTrue(tools[0]['actionApplied'])
        self.assertTrue(any(event.get('type') == 'monitor.changed' for event in self.events))
        self.assertFalse(any(event.get('type') == 'selection.changed' for event in self.events))

    async def test_selection_read_counts_as_verified_tool_data(self):
        item = {
            "id": "mcp-call-3", "type": "mcpToolCall", "server": "stocks_selection",
            "tool": "stocks_selection", "status": "completed",
            "arguments": '{"action":"evaluate"}',
            "result": {"content": [{"type": "text", "text":
                '{"ok":true,"action":"evaluate","result":{"asOf":"2026-09-19","items":[]}}'}]},
        }
        await self.session.capture_selection_tool("turn-3", item)
        tool = self.session.turn_state("turn-3")["tools"][0]
        self.assertTrue(tool["success"])
        self.assertTrue(tool["dataReturned"])
        self.assertFalse(tool["actionApplied"])

    async def test_call_request_has_verified_receipt_and_one_monitor_refresh(self):
        self.session.turn_state("call-turn")["requestId"] = "voice-request"
        item = {
            "id": "call-request", "type": "mcpToolCall", "server": "stocks_monitor",
            "tool": "stocks_call", "status": "completed", "arguments": {"action": "request"},
            "result": {"structuredContent": {"ok": True, "action": "request", "result": {
                "success": True, "revision": 3, "requestId": "call-intent",
                "operation": "notification.create", "notificationId": "notice-1",
                "state": "waiting_for_current_voice", "answered": False,
            }}},
        }
        await self.session.capture_selection_tool("call-turn", item)
        await self.session.capture_selection_tool("call-turn", item)
        receipts = self.session.turn_state("call-turn")["tools"]
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["name"], "stocks_call")
        self.assertTrue(receipts[0]["success"])
        self.assertTrue(receipts[0]["actionApplied"])
        self.assertFalse(receipts[0]["dataReturned"])
        self.assertEqual(receipts[0]["requestId"], "voice-request")
        self.assertEqual(receipts[0]["mutationRequestId"], "call-intent")
        changes = [event for event in self.events if event.get("type") in ("monitor.changed", "selection.changed")]
        self.assertEqual(changes, [{"type": "monitor.changed", "revision": 3,
                                    "requestId": "call-intent", "operation": "notification.create"}])

    async def test_call_status_is_verified_data_without_mutation(self):
        item = {
            "id": "call-status", "type": "mcpToolCall", "server": "stocks_monitor",
            "tool": "stocks_call", "status": "completed", "arguments": '{"action":"status"}',
            "result": {"content": [{"type": "text", "text":
                '{"ok":true,"action":"status","result":{"available":true,"state":"push_accepted","answered":false}}'}]},
        }
        await self.session.capture_selection_tool("status-turn", item)
        receipt = self.session.turn_state("status-turn")["tools"][0]
        self.assertTrue(receipt["success"])
        self.assertTrue(receipt["dataReturned"])
        self.assertFalse(receipt["actionApplied"])
        self.assertFalse(any(event.get("type", "").endswith(".changed") for event in self.events))

    async def test_failed_call_request_does_not_claim_action_or_emit_refresh(self):
        item = {
            "id": "call-failure", "type": "mcpToolCall", "server": "stocks_monitor",
            "tool": "stocks_call", "status": "completed", "arguments": {"action": "request"},
            "result": {"structuredContent": {"ok": False, "action": "request",
                                               "error": {"code": "call_unavailable"}}},
        }
        await self.session.capture_selection_tool("failed-call-turn", item)
        receipt = self.session.turn_state("failed-call-turn")["tools"][0]
        self.assertFalse(receipt["success"])
        self.assertFalse(receipt["dataReturned"])
        self.assertFalse(receipt["actionApplied"])
        self.assertFalse(any(event.get("type", "").endswith(".changed") for event in self.events))


if __name__ == "__main__":
    unittest.main()
