import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

import monitor_mcp
from plans import PlanService
from test_plans import sample_plan
from voice import VoiceSession, safe_error


class AssistantFeatureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"STOCKS_MONITOR_OWNER": "test-owner-a",
            "STOCKS_MONITOR_STATE_DIR": self.temp.name, "STOCKS_CLIENT_TIME_ZONE": "Asia/Tokyo"})
        self.env.start()
        monitor_mcp._service = None
        self.events = []

        async def emit(event):
            self.events.append(event)

        async def audio(_):
            pass

        self.voice = VoiceSession("test-device", self.temp.name, None, emit, audio,
                                  selection_owner="test-owner-a")
        self.voice.plan_service = PlanService(self.temp.name)
        self.voice.thread_id = "test-thread"
        os.environ["STOCKS_VOICE_SESSION_ID"] = self.voice.session_id

    async def asyncTearDown(self):
        for task in self.voice.tasks:
            task.cancel()
        await asyncio.gather(*self.voice.tasks, return_exceptions=True)
        await self.voice.pc.close()
        self.env.stop()
        monitor_mcp._service = None
        self.temp.cleanup()

    async def test_plan_receipt_binds_real_turn_refreshes_ui_once_and_never_verifies_price(self):
        saved = monitor_mcp.stocks_plan("save", {"requestId": "plan-intent", "expectedRevision": 0,
                                              "plan": sample_plan()})
        self.assertTrue(saved["ok"])
        item = {"type": "mcpToolCall", "server": "stocks_monitor", "tool": "stocks_plan",
                "status": "completed", "id": "plan-call", "arguments": {"action": "save"},
                "result": {"structuredContent": saved}}
        await self.voice.tool_started("actual-turn", "plan-call", "stocks_plan")
        await self.voice.capture_selection_tool("actual-turn", item)
        await self.voice.capture_selection_tool("actual-turn", item)
        changes = [e for e in self.events if e["type"] == "plan.changed"]
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]["code"], "000001")
        plan = self.voice.plan_service.get("test-owner-a", changes[0]["planId"])["plan"]
        self.assertEqual(plan["source"]["turnId"], "actual-turn")
        receipt = self.voice.turns["actual-turn"]["tools"][0]
        self.assertTrue(receipt["actionApplied"])
        self.assertTrue(receipt["nonMarketAction"])
        self.assertFalse(receipt["dataReturned"])
        os.environ["STOCKS_MONITOR_OWNER"] = "test-owner-b"
        self.assertFalse(monitor_mcp.stocks_plan("get", {"id": plan["id"]})["ok"])

    async def test_schedule_clock_owner_boundary_and_diagnostics_redaction(self):
        catalog = monitor_mcp.stocks_schedule("catalog")["result"]
        self.assertEqual(catalog["clientTimeZone"], "Asia/Tokyo")
        self.assertIn("+00:00", catalog["currentTimeUtc"])
        self.assertFalse(monitor_mcp.stocks_schedule("list", {"owner": "someone-else"})["ok"])
        self.assertFalse(monitor_mcp.stocks_plan("list", {"owner": "someone-else"})["ok"])
        redacted = safe_error('failed token="private-value" authorization=secret-value https://example.com?token=secret')
        self.assertNotIn("private-value", redacted)
        self.assertNotIn("secret-value", redacted)
        self.assertNotIn("https://", redacted)
