import copy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import tempfile
import unittest
from unittest.mock import patch

from monitoring import MonitorService
from plans import PlanError, PlanService
from reports import ReportError, ReportService, validate_report
from test_plans import sample_plan


NOW = datetime(2026, 9, 21, 2, 30, tzinfo=timezone.utc).timestamp()


def sample_report(code="000001"):
    return {"code": code, "title": "价格区间与风险", "summary": "等待价格接近观察区间。",
            "direction": "neutral", "confidence": "medium", "points": ["以给定行情为依据。"],
            "risks": ["缺少持仓与成本，不能推导仓位。"], "basis": sample_plan(code)["basis"],
            "sources": [{"title": "已获取行情", "asOf": "2026-09-21T10:15:00+08:00"}]}


def actionable_plan(code="000001"):
    plan = sample_plan(code)
    plan["variants"][1]["rules"] = plan["variants"][1]["rules"][:3]
    return plan


class ReportServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.plans = PlanService(self.temp.name)
        self.monitor = MonitorService(self.temp.name, clock=lambda: NOW)
        self.service = ReportService(self.temp.name, self.plans, self.monitor, clock=lambda: NOW)

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, request="report-1", revision=0, code="000001", with_plan=True):
        value = {"requestId": request, "expectedRevision": revision, "report": sample_report(code)}
        if with_plan:
            value["plan"] = actionable_plan(code)
        return value

    def save(self, **kwargs):
        return self.service.save("owner-a", self.payload(**kwargs), {"sessionId": "session-a", "requestId": "original-request"})

    def assert_error(self, code, action):
        with self.assertRaises(ReportError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def apply_payload(self, saved, request="apply-1", variant="standard"):
        return {"requestId": request, "expectedRevision": self.service.list("owner-a")["revision"],
                "planId": saved["report"]["planId"], "variantId": variant}

    def test_report_plan_save_is_immutable_hydrated_and_has_no_monitor_side_effect(self):
        saved = self.save()
        report = saved["report"]
        self.assertEqual(report["plan"]["id"], report["planId"])
        self.assertEqual(report["source"]["reportId"], report["id"])
        self.assertEqual(report["plan"]["source"], report["source"])
        self.assertIsNone(report["adoption"])
        self.assertEqual(self.monitor.library("owner-a")["rules"], [])
        second = self.save(request="report-2", revision=1)
        self.assertNotEqual(report["id"], second["reportId"])
        self.assertEqual(self.service.get("owner-a", saved["reportId"])["report"]["summary"], report["summary"])
        self.assertEqual(len(self.service.list("owner-a")["items"]), 2)

    def test_replay_owner_isolation_request_conflict_and_revision(self):
        saved = self.save()
        self.assertTrue(self.save()["replayed"])
        changed = self.payload()
        changed["report"]["summary"] = "changed"
        self.assert_error("request_id_conflict", lambda: self.service.save("owner-a", changed))
        self.assert_error("revision_conflict", lambda: self.save(request="another", revision=0))
        self.assertEqual(self.service.list("owner-b")["items"], [])
        self.assert_error("report_not_found", lambda: self.service.get("owner-b", saved["reportId"]))
        with self.assertRaises(PlanError):
            self.service.preview("owner-b", {"planId": saved["report"]["planId"], "variantId": "standard"})
        self.assertEqual(self.service.save("owner-b", self.payload())["revision"], 1)

    def test_invalid_report_or_plan_is_rejected_before_any_plan_write(self):
        edits = [lambda p: p["report"].update(direction="buy"),
                 lambda p: p["report"].update(confidence=1),
                 lambda p: p["report"].update(owner="owner-b"),
                 lambda p: p["report"].update(points=["x"] * 13),
                 lambda p: p["report"].update(sources=[{"title": "bad", "url": "file:///tmp/a"}]),
                 lambda p: p["plan"].update(code="000002"),
                 lambda p: p["plan"]["basis"].update(referencePrice=99)]
        for edit in edits:
            payload = self.payload()
            edit(payload)
            with self.subTest(payload=payload), self.assertRaises(ReportError):
                self.service.save("owner-a", payload)
        self.assertEqual(self.plans.list("owner-a")["revision"], 0)
        self.assertEqual(self.service.list("owner-a")["revision"], 0)

    def test_recover_crash_after_plan_commit_creates_only_one_plan_and_report(self):
        original = self.plans.save
        called = []
        def crash(owner, payload, source=None):
            saved = original(owner, payload, source)
            called.append(saved["planId"])
            raise SystemExit("simulated crash after plan commit")
        with patch.object(self.plans, "save", side_effect=crash), self.assertRaises(SystemExit):
            self.save()
        self.assertEqual(len(self.plans.list("owner-a")["items"]), 1)
        self.assertEqual(self.service.list("owner-a")["items"], [])
        self.service = ReportService(self.temp.name, self.plans, self.monitor, clock=lambda: NOW)
        recovered = self.save()
        self.assertEqual(recovered["report"]["planId"], called[0])
        self.assertEqual(len(self.plans.list("owner-a")["items"]), 1)
        self.assertEqual(len(self.service.list("owner-a")["items"]), 1)

    def test_plan_revision_conflict_refreshes_before_safe_downstream_retry(self):
        original = self.plans.save
        once = []
        def conflict(owner, payload, source=None):
            if not once:
                once.append(True)
                original(owner, {"requestId": "other-plan", "expectedRevision": 0, "plan": sample_plan()})
            return original(owner, payload, source)
        with patch.object(self.plans, "save", side_effect=conflict):
            saved = self.save()
        self.assertIsNotNone(saved["report"]["plan"])
        self.assertEqual(len(self.plans.list("owner-a")["items"]), 2)

    def test_observed_turn_binds_once_and_preserves_source_in_plan_and_receipt(self):
        saved = self.save()
        self.service.bind_source("owner-a", saved["reportId"], {"sessionId": "other", "turnId": "wrong"})
        self.service.bind_source("owner-a", saved["reportId"], {"sessionId": "session-a", "turnId": "turn-1", "requestId": "overwrite"})
        self.service.bind_source("owner-a", saved["reportId"], {"sessionId": "session-a", "turnId": "turn-2"})
        current = self.service.get("owner-a", saved["reportId"])["report"]
        self.assertEqual(current["source"]["turnId"], "turn-1")
        self.assertEqual(current["source"]["requestId"], "original-request")
        self.assertEqual(current["plan"]["source"], current["source"])
        self.assertEqual(self.save()["report"]["source"], current["source"])

    def test_preview_and_apply_create_all_price_rules_only_after_user_action(self):
        saved = self.save()
        preview = self.service.preview("owner-a", {"planId": saved["report"]["planId"], "variantId": "standard"})
        self.assertTrue(preview["canApply"])
        self.assertEqual(len(preview["conditions"]), 3)
        self.assertEqual([c["op"] for c in preview["conditions"]], ["below", "below", "above"])
        self.assertEqual(self.monitor.library("owner-a")["rules"], [])
        result = self.service.apply("owner-a", self.apply_payload(saved))
        self.assertEqual(result["adoption"]["completionStatus"], "complete")
        self.assertEqual(len(result["adoption"]["ruleIds"]), 3)
        self.assertEqual({r["severity"] for r in self.monitor.library("owner-a")["rules"]}, {"normal"})
        self.assertEqual(self.service.list("owner-a")["events"][0]["adoption"]["id"], result["adoption"]["id"])
        self.assertEqual(self.service.signals("owner-a")["items"]["000001"]["adoption"]["ruleCount"], 3)

    def test_unsupported_and_conflicting_targets_reject_whole_variant(self):
        payload = self.payload()
        payload["plan"] = sample_plan()  # Includes no_add; cannot silently omit it.
        saved = self.service.save("owner-a", payload)
        self.assert_error("unsupported_plan", lambda: self.service.apply("owner-a", self.apply_payload(saved)))
        self.assertEqual(self.monitor.library("owner-a")["rules"], [])
        second = self.payload("other", 1)
        second["plan"]["variants"][1]["targetPrice"] = 12
        saved = self.service.save("owner-a", second)
        self.assert_error("unsupported_plan", lambda: self.service.apply("owner-a", self.apply_payload(saved)))

    def test_stale_naive_and_archived_plan_are_previewable_but_not_applied(self):
        for index, when in enumerate(("2026-09-10T10:00:00+08:00", "2026-09-21T10:00:00")):
            payload = self.payload(f"old-{index}", index)
            payload["report"]["basis"]["marketAsOf"] = when
            payload["plan"]["basis"]["marketAsOf"] = when
            saved = self.service.save("owner-a", payload)
            preview = self.service.preview("owner-a", {"planId": saved["report"]["planId"], "variantId": "standard"})
            self.assertTrue(preview["stale"])
            self.assert_error("stale_plan", lambda: self.service.apply("owner-a", self.apply_payload(saved)))
        saved = self.save(request="fresh", revision=2)
        self.plans.archive("owner-a", {"requestId": "archive", "expectedRevision": 3, "id": saved["report"]["planId"]})
        self.assert_error("unsupported_plan", lambda: self.service.apply("owner-a", self.apply_payload(saved)))
        self.assertEqual(self.monitor.library("owner-a")["rules"], [])

    def test_repeat_click_does_not_resume_paused_or_recreate_deleted_rules(self):
        saved = self.save()
        payload = self.apply_payload(saved)
        first = self.service.apply("owner-a", payload)
        ids = first["adoption"]["ruleIds"]
        for index, rid in enumerate(ids):
            self.monitor.mutate("owner-a", {"requestId": f"pause-{index}", "operation": "rule.pause", "id": rid})
        again = self.service.apply("owner-a", payload)
        self.assertEqual(again["adoption"]["state"], "paused")
        self.assertTrue(again["replayed"])
        self.assertEqual(self.service.apply("owner-a", self.apply_payload(saved, request="click-again"))["adoption"]["state"], "paused")
        self.monitor.mutate("owner-a", {"requestId": "delete", "operation": "rule.delete", "id": ids[0]})
        again = self.service.apply("owner-a", payload)
        self.assertEqual(again["adoption"]["state"], "partial")
        self.assertEqual(again["adoption"]["missingRuleIds"], ids[:1])
        self.assertEqual(len(self.monitor.library("owner-a")["rules"]), 2)
        self.assert_error("variant_conflict", lambda: self.service.apply("owner-a", self.apply_payload(saved, request="other-tier", variant="conservative")))

    def test_partial_monitor_write_recovers_from_durable_intent_without_duplicates(self):
        saved = self.save()
        payload = self.apply_payload(saved)
        original = self.monitor.mutate
        once = []
        def fail_after_first(owner, request):
            if once:
                raise RuntimeError("simulated database failure")
            once.append(True)
            return original(owner, request)
        with patch.object(self.monitor, "mutate", side_effect=fail_after_first):
            error = self.assert_error("adoption_partial", lambda: self.service.apply("owner-a", payload))
        self.assertEqual(error.detail["adoption"]["ruleCount"], 1)
        self.assertEqual(error.detail["adoption"]["completionStatus"], "partial")
        self.service = ReportService(self.temp.name, self.plans, self.monitor, clock=lambda: NOW)
        recovered = self.service.apply("owner-a", payload)
        self.assertEqual(recovered["adoption"]["ruleCount"], 3)
        self.assertEqual(len(self.monitor.library("owner-a")["rules"]), 3)
        self.assertEqual({r["version"] for r in self.monitor.library("owner-a")["rules"]}, {1})

    def test_crash_after_monitor_commit_replays_receipt_preserving_later_pause(self):
        saved = self.save()
        payload = self.apply_payload(saved)
        original = self.monitor.mutate
        def crash(owner, request):
            original(owner, request)
            raise SystemExit("crash")
        with patch.object(self.monitor, "mutate", side_effect=crash), self.assertRaises(SystemExit):
            self.service.apply("owner-a", payload)
        first = self.monitor.library("owner-a")["rules"][0]
        original("owner-a", {"requestId": "pause-first", "operation": "rule.pause", "id": first["id"]})
        result = self.service.apply("owner-a", payload)
        self.assertEqual(result["adoption"]["state"], "partially_paused")
        self.assertEqual(result["adoption"]["enabledCount"], 2)

    def test_pagination_signals_latest_and_account_scope(self):
        first = self.save()
        self.save(request="two", revision=1, code="000002", with_plan=False)
        third = self.save(request="three", revision=2, with_plan=False)
        page = self.service.list("owner-a", limit=2)
        self.assertEqual(len(page["items"]), 2)
        next_page = self.service.list("owner-a", limit=2, before=page["nextCursor"])
        self.assertEqual(next_page["items"][0]["id"], first["reportId"])
        signals = self.service.signals("owner-a", ["000001"])["items"]
        self.assertEqual(list(signals), ["000001"])
        self.assertEqual(signals["000001"]["reportId"], third["reportId"])
        self.assertEqual(self.service.signals("owner-b")["items"], {})
        self.assertEqual(self.service.signals("owner-a", [])["items"], {})

    def test_plan_target_alone_becomes_one_condition_and_no_condition_stays_display_only(self):
        payload = self.payload()
        payload["plan"]["variants"][1]["rules"] = []
        saved = self.service.save("owner-a", payload)
        result = self.service.apply("owner-a", self.apply_payload(saved))
        self.assertEqual(result["adoption"]["ruleCount"], 1)
        saved2 = self.save(request="display-only", revision=result["revision"])
        self.assert_error("unsupported_plan", lambda: self.service.apply("owner-a", self.apply_payload(saved2, request="empty", variant="conservative")))

    def test_concurrent_workers_share_one_adoption_and_monitor_receipts(self):
        saved = self.save()
        payload = self.apply_payload(saved)
        def apply_one(_):
            service = ReportService(self.temp.name, PlanService(self.temp.name),
                                    MonitorService(self.temp.name, clock=lambda: NOW), clock=lambda: NOW)
            return service.apply("owner-a", copy.deepcopy(payload))
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(apply_one, range(4)))
        self.assertEqual(len({item["adoption"]["id"] for item in results}), 1)
        rules = self.monitor.library("owner-a")["rules"]
        self.assertEqual(len(rules), 3)
        self.assertEqual({rule["version"] for rule in rules}, {1})

    def test_signals_include_newer_standalone_plan_without_invented_confidence(self):
        saved = self.save()
        with patch("plans._now", return_value="2026-09-21T02:31:00+00:00"):
            standalone = self.plans.save("owner-a", {"requestId": "standalone", "expectedRevision": 1, "plan": actionable_plan()})
        signal = self.service.signals("owner-a")["items"]["000001"]
        self.assertIsNone(signal["reportId"])
        self.assertEqual(signal["planId"], standalone["planId"])
        self.assertEqual(signal["action"], "买入")
        self.assertEqual(signal["confidence"], "unrated")
        self.assertTrue(signal["hasStrategy"])
        self.assertEqual(self.service.signals("owner-b")["items"], {})
        self.service.clock = lambda: NOW + 120
        newer = self.save(request="new-report", revision=1, with_plan=False)
        signal = self.service.signals("owner-a")["items"]["000001"]
        self.assertEqual(signal["reportId"], newer["reportId"])
        self.assertFalse(signal["hasStrategy"])

    def test_signals_ignore_archived_standalone_and_flag_archived_linked_plan(self):
        with patch("plans._now", return_value="2026-09-21T02:29:00+00:00"):
            standalone = self.plans.save("owner-a", {"requestId": "standalone", "expectedRevision": 0, "plan": actionable_plan("000002")})
        self.assertTrue(self.service.signals("owner-a")["items"]["000002"]["hasStrategy"])
        self.plans.archive("owner-a", {"requestId": "archive-standalone", "expectedRevision": 1, "id": standalone["planId"]})
        self.assertNotIn("000002", self.service.signals("owner-a")["items"])
        saved = self.save()
        self.plans.archive("owner-a", {"requestId": "archive-linked", "expectedRevision": 3, "id": saved["report"]["planId"]})
        signal = self.service.signals("owner-a")["items"]["000001"]
        self.assertFalse(signal["hasStrategy"])
        self.assertEqual(signal["planStatus"], "archived")
        self.assertIsNone(signal["action"])

    def test_signal_strategy_uses_selected_variant_instead_of_recommendation(self):
        payload = self.payload()
        payload["plan"]["variants"][0].update(action="买入", targetKind="买入", targetPrice=10.5)
        saved = self.service.save("owner-a", payload)
        self.service.apply("owner-a", self.apply_payload(saved, variant="conservative"))
        signal = self.service.signals("owner-a")["items"]["000001"]
        self.assertEqual(signal["adoption"]["variantId"], "conservative")
        self.assertEqual(signal["targetPrice"], 10.5)

    def test_signals_do_not_drop_older_stocks_after_global_hundred_row_cutoff(self):
        for index in range(101):
            self.service.save("owner-a", self.payload(f"report-{index}", index, f"{index:06}", with_plan=False))
        signals = self.service.signals("owner-a")["items"]
        self.assertEqual(len(signals), 101)
        self.assertIn("000000", signals)

    def test_signal_adoptions_restore_older_standalone_card_with_real_paused_state(self):
        with patch("plans._now", return_value="2026-09-21T02:29:00+00:00"):
            old = self.plans.save("owner-a", {"requestId": "old-strategy", "expectedRevision": 0, "plan": actionable_plan()})
        accepted = self.service.apply("owner-a", {"requestId": "adopt-old", "expectedRevision": 0,
            "planId": old["planId"], "variantId": "standard"})
        with patch("plans._now", return_value="2026-09-21T02:31:00+00:00"):
            new = self.plans.save("owner-a", {"requestId": "new-strategy", "expectedRevision": 1, "plan": actionable_plan()})
        for index, rule_id in enumerate(accepted["adoption"]["ruleIds"]):
            self.monitor.mutate("owner-a", {"requestId": f"pause-{index}", "operation": "rule.pause", "id": rule_id})
        response = self.service.signals("owner-a")
        self.assertEqual(response["items"]["000001"]["planId"], new["planId"])
        self.assertIsNone(response["items"]["000001"]["adoption"])
        self.assertEqual(response["adoptions"][old["planId"]]["state"], "paused")
        self.assertEqual(response["adoptions"][old["planId"]]["enabledCount"], 0)
        self.assertEqual(response["adoptions"][old["planId"]]["ruleIds"], accepted["adoption"]["ruleIds"])
        self.assertEqual(self.service.signals("owner-b")["adoptions"], {})


if __name__ == "__main__":
    unittest.main()
