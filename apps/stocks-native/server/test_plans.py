import math
from pathlib import Path
import tempfile
import unittest

from plans import PlanError, PlanService


def sample_plan(code="000001"):
    return {"code": code, "title": "观察区间方案", "summary": "按已有行情提出的参考方案。",
            "mode": "watch", "recommendedVariantId": "standard",
            "basis": {"marketAsOf": "2026-09-21T10:15:00+08:00", "referencePrice": 11.7, "contextRevision": 3},
            "variants": [
                {"id": "conservative", "label": "保守", "action": "观望", "targetPrice": None,
                 "targetKind": "无", "suggestedShares": None, "reason": "等待条件明确。", "rules": []},
                {"id": "standard", "label": "标准", "action": "买入", "targetPrice": 11.5,
                 "targetKind": "买入", "suggestedShares": None, "urgency": "normal", "reason": "接近所选支撑区间再观察。",
                 "rules": [{"type": "target_buy", "value": 11.5}, {"type": "hard_stop", "value": 11},
                           {"type": "take_profit", "value": 12.5}, {"type": "no_add", "value": None}]}]}


class PlanServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = PlanService(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def save(self, *, owner="apple:a", request_id="intent-1", revision=0, plan=None, source=None):
        return self.service.save(owner, {"requestId": request_id, "expectedRevision": revision,
                                         "plan": plan if plan is not None else sample_plan()}, source=source)

    def assert_error(self, code, action):
        with self.assertRaises(PlanError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_save_roundtrip_keeps_watch_stop_and_take_rules_without_side_effects(self):
        receipt = self.save(source={"turnId": "turn-1", "messageId": "backend:message"})
        reopened = PlanService(self.temp.name)
        plan = reopened.get("apple:a", receipt["planId"])["plan"]
        self.assertEqual(plan, receipt["plan"])
        self.assertEqual(plan["source"]["turnId"], "turn-1")
        self.assertEqual(plan["status"], "proposed")
        self.assertEqual([r["type"] for r in plan["variants"][1]["rules"]], ["target_buy", "hard_stop", "take_profit", "no_add"])
        self.assertIsNone(plan["variants"][1]["suggestedShares"])
        self.assertEqual(reopened.list("apple:a", "000001")["items"], [plan])
        self.assertFalse(any("monitor" in p.name or "holding" in p.name for p in Path(self.temp.name).iterdir()))

    def test_owner_isolation_for_ids_lists_revisions_and_receipts(self):
        first = self.save()
        self.assertEqual(self.service.list("apple:b")["revision"], 0)
        self.assertEqual(self.service.list("apple:b")["items"], [])
        self.assert_error("plan_not_found", lambda: self.service.get("apple:b", first["planId"]))
        self.assert_error("plan_not_found", lambda: self.service.archive("apple:b", {
            "requestId": "archive-1", "expectedRevision": 0, "id": first["planId"]}))
        second = self.save(owner="apple:b")
        self.assertNotEqual(first["planId"], second["planId"])
        self.assertEqual(second["revision"], 1)

    def test_provenance_bound_only_once_by_original_session(self):
        saved = self.save(source={"sessionId": "session-a"})
        first = self.service.bind_source("apple:a", saved["planId"], {"sessionId": "session-b", "turnId": "wrong"})
        self.assertNotIn("turnId", first["source"])
        first = self.service.bind_source("apple:a", saved["planId"], {"sessionId": "session-a", "turnId": "turn-a"})
        second = self.service.bind_source("apple:a", saved["planId"], {"sessionId": "session-a", "turnId": "turn-b"})
        self.assertEqual(first["source"], second["source"])
        self.assertEqual(self.service.get("apple:a", saved["planId"])["plan"]["source"]["turnId"], "turn-a")

    def test_same_intent_replay_works_after_revision_changes_without_duplication(self):
        first = self.save()
        self.save(request_id="intent-2", revision=1, plan=sample_plan("000002"))
        replay = self.save()
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["planId"], first["planId"])
        self.assertEqual(len(self.service.list("apple:a")["items"]), 2)
        self.assertEqual(self.service.list("apple:a")["revision"], 2)

    def test_reused_request_id_with_different_payload_or_operation_conflicts(self):
        first = self.save()
        self.assert_error("request_id_conflict", lambda: self.save(plan=sample_plan("000002")))
        self.assert_error("request_id_conflict", lambda: self.service.archive("apple:a", {
            "requestId": "intent-1", "expectedRevision": 0, "id": first["planId"]}))

    def test_stale_revision_does_not_write(self):
        self.save()
        error = self.assert_error("revision_conflict", lambda: self.save(request_id="stale", revision=0))
        self.assertEqual(error.status, 409)
        self.assertEqual(error.detail["currentRevision"], 1)
        self.assertEqual(len(self.service.list("apple:a")["items"]), 1)

    def test_archive_is_durable_replay_safe_and_preserves_history(self):
        saved = self.save()
        payload = {"requestId": "archive-1", "expectedRevision": 1, "id": saved["planId"]}
        archived = self.service.archive("apple:a", payload)
        self.assertEqual(archived["plan"]["status"], "archived")
        self.assertEqual(archived["plan"]["revision"], 2)
        self.assertTrue(self.service.archive("apple:a", payload)["replayed"])
        self.assertEqual(self.service.list("apple:a")["items"], [])
        self.assertEqual(len(self.service.list("apple:a", include_archived=True)["items"]), 1)
        self.assertEqual(self.service.get("apple:a", saved["planId"])["plan"]["variants"], saved["plan"]["variants"])

    def test_list_filters_stock_and_returns_newest_with_bound(self):
        first = self.save()
        second = self.save(request_id="i2", revision=1, plan=sample_plan("000002"))
        third = self.save(request_id="i3", revision=2)
        self.assertEqual([p["id"] for p in self.service.list("apple:a", code="000001")["items"]], [third["planId"], first["planId"]])
        self.assertEqual(self.service.list("apple:a", limit=1)["items"][0]["id"], third["planId"])
        self.assertEqual(self.service.list("apple:a", code="000002")["items"][0]["id"], second["planId"])

    def test_untrusted_owner_source_and_generated_fields_are_rejected(self):
        for key, value in (("owner", "apple:b"), ("source", {"turnId": "spoof"}), ("status", "accepted"), ("id", "fake")):
            with self.subTest(key=key):
                plan = sample_plan()
                plan[key] = value
                self.assert_error("invalid_plan", lambda: self.save(plan=plan))
        self.assert_error("invalid_plan", lambda: self.save(source={"owner": "apple:b"}))
        self.assertEqual(self.service.list("apple:a")["revision"], 0)

    def test_invalid_numbers_enum_and_lengths_are_atomic(self):
        edits = [
            lambda p: p.update(code="000001 OR 1=1"),
            lambda p: p.update(title="x" * 81),
            lambda p: p.update(mode="trading"),
            lambda p: p.update(recommendedVariantId="missing"),
            lambda p: p.update(variants=p["variants"][:1]),
            lambda p: p["variants"][0].update(label="标准"),
            lambda p: p["variants"][0].update(id="standard"),
            lambda p: p["variants"][1].update(action="自动下单"),
            lambda p: p["variants"][1].update(targetPrice=True),
            lambda p: p["variants"][1].update(targetPrice=-1),
            lambda p: p["variants"][1].update(targetPrice="11.5"),
            lambda p: p["variants"][1].update(targetPrice=None),
            lambda p: p["variants"][1].update(suggestedShares=2.5),
            lambda p: p["variants"][1].update(suggestedShares=0),
            lambda p: p["variants"][1].update(suggestedShares=True),
            lambda p: p["variants"][1].update(reason="x" * 401),
            lambda p: p["variants"][1].update(rules=[{"type": "unknown", "value": 1}]),
            lambda p: p["variants"][1].update(rules=[{"type": "no_add", "value": 1}]),
            lambda p: p["variants"][1].update(rules=[{"type": "max_shares", "value": 1.5}]),
            lambda p: p["variants"][1].update(rules=[{"type": "hard_stop", "value": 10}] * 2),
            lambda p: p["variants"][1].update(rules=[{"type": "trailing_drawdown", "value": 101}]),
            lambda p: p["basis"].update(marketAsOf="昨天"),
            lambda p: p["basis"].update(contextRevision=True),
        ]
        for edit in edits:
            plan = sample_plan()
            edit(plan)
            with self.subTest(plan=plan), self.assertRaises(PlanError):
                self.save(plan=plan)
        self.assertEqual(self.service.list("apple:a")["revision"], 0)
        self.assertEqual(self.service.list("apple:a")["items"], [])

    def test_nonfinite_values_cannot_reach_database(self):
        for number in (math.nan, math.inf, -math.inf):
            plan = sample_plan()
            plan["basis"]["referencePrice"] = number
            self.assert_error("invalid_request", lambda: self.save(plan=plan))

    def test_default_mode_and_missing_share_count_do_not_invent_holdings(self):
        plan = sample_plan()
        del plan["mode"]
        del plan["variants"][1]["suggestedShares"]
        saved = self.save(plan=plan)["plan"]
        self.assertEqual(saved["mode"], "unspecified")
        self.assertIsNone(saved["variants"][1]["suggestedShares"])

    def test_revision_and_query_inputs_are_strict(self):
        for value in (True, -1, "0", 0.5, None):
            self.assert_error("invalid_revision", lambda: self.save(revision=value))
        for value in (True, 0, 101, "20"):
            self.assert_error("invalid_limit", lambda: self.service.list("apple:a", limit=value))
        self.assert_error("invalid_request", lambda: self.service.list("apple:a", include_archived="false"))
        self.assert_error("invalid_code", lambda: self.service.list("apple:a", code="000001'"))


if __name__ == "__main__":
    unittest.main()
