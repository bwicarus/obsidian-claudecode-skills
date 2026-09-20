import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

import monitor_mcp
from plans import PlanService
from reports import ReportService
from monitoring import MonitorService
from test_reports import sample_report, actionable_plan, NOW
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
        self.voice.report_service = ReportService(self.temp.name, self.voice.plan_service,
            MonitorService(self.temp.name, clock=lambda: NOW), clock=lambda: NOW)
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

    async def test_report_save_binds_report_and_plan_then_emits_one_non_market_change(self):
        saved = monitor_mcp.stocks_report("save", {"requestId": "research-intent", "expectedRevision": 0,
            "report": sample_report(), "plan": actionable_plan()})
        self.assertTrue(saved["ok"])
        item = {"type": "mcpToolCall", "server": "stocks_monitor", "tool": "stocks_report",
                "status": "completed", "id": "report-call", "arguments": {"action": "save"},
                "result": {"structuredContent": saved}}
        await self.voice.capture_selection_tool("report-turn", item)
        await self.voice.capture_selection_tool("report-turn", item)
        changes = [event for event in self.events if event["type"] == "report.changed"]
        self.assertEqual(len(changes), 1)
        report = self.voice.report_service.get("test-owner-a", changes[0]["reportId"])["report"]
        self.assertEqual(report["source"]["turnId"], "report-turn")
        self.assertEqual(report["plan"]["source"]["turnId"], "report-turn")
        receipt = self.voice.turns["report-turn"]["tools"][0]
        self.assertTrue(receipt["actionApplied"])
        self.assertFalse(receipt["dataReturned"])
        self.assertFalse(receipt["researchReturned"])
        self.assertTrue(receipt["nonMarketAction"])
        self.assertFalse(monitor_mcp.stocks_report("list", {"owner": "other"})["ok"])
        os.environ["STOCKS_MONITOR_OWNER"] = "test-owner-b"
        self.assertFalse(monitor_mcp.stocks_report("get", {"id": report["id"]})["ok"])

    async def test_plan_activation_mcp_uses_preview_revision_and_real_rule_ids(self):
        saved = monitor_mcp.stocks_plan("save", {"requestId": "strategy-intent", "expectedRevision": 0,
                                                "plan": actionable_plan()})["result"]
        with patch.object(monitor_mcp, "_reports_runtime", return_value=(self.voice.report_service, "test-owner-a")):
            preview = monitor_mcp.stocks_plan_activation("preview", {"planId": saved["planId"], "variantId": "standard"})
            self.assertTrue(preview["result"]["canApply"])
            result = monitor_mcp.stocks_plan_activation("apply", {"requestId": "selected-card", "expectedRevision": preview["result"]["revision"],
                "planId": saved["planId"], "variantId": "standard"})
        self.assertTrue(result["ok"])
        self.assertTrue(result["result"]["success"])
        self.assertEqual(len(result["result"]["adoption"]["ruleIds"]), 3)
        item = {"type": "mcpToolCall", "server": "stocks_monitor", "tool": "stocks_plan_activation", "status": "completed",
                "id": "activate-call", "arguments": {"action": "apply"}, "result": {"structuredContent": result}}
        await self.voice.capture_selection_tool("activate-turn", item)
        receipt = self.voice.turns["activate-turn"]["tools"][0]
        self.assertTrue(receipt["actionApplied"])
        self.assertFalse(receipt["dataReturned"])
        self.assertTrue(receipt["nonMarketAction"])
        self.assertEqual({e["type"] for e in self.events if e["type"].endswith(".changed")}, {"monitor.changed", "report.changed"})

    async def test_news_unavailable_is_failed_and_historical_read_is_not_market_verification(self):
        self.assertFalse((await monitor_mcp.stocks_news("feed", {"url": "https://example.invalid"}))["ok"])
        item = {"type": "mcpToolCall", "server": "stocks_monitor", "tool": "stocks_news", "status": "completed", "id": "news-call",
                "arguments": {"action": "feed"}, "result": {"structuredContent": {"ok": True, "action": "feed", "result": {
                    "status": "unavailable", "items": [], "warnings": ["source unavailable"]}}}}
        await self.voice.capture_selection_tool("news-turn", item)
        self.assertFalse(self.voice.turns["news-turn"]["tools"][0]["success"])
        item["id"] = "legacy-call"
        item["result"]["structuredContent"] = {"ok": True, "action": "legacy", "result": {
            "status": "historical", "asOf": "2026-09-18", "items": [{"code": "000001", "isHistorical": True}]}}
        await self.voice.capture_selection_tool("news-turn", item)
        receipt = self.voice.turns["news-turn"]["tools"][1]
        self.assertTrue(receipt["success"])
        self.assertTrue(receipt["researchReturned"])
        self.assertFalse(receipt["dataReturned"])
        self.assertTrue(receipt["nonMarketAction"])
