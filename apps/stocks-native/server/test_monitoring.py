import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from monitoring import CHINA, MonitorError, MonitorService
from monitor_tools import dispatch_monitor_safe


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        # A Monday morning in the continuous trading session.
        self.now = datetime(2026, 9, 21, 10, 0, tzinfo=CHINA).timestamp()
        self.service = MonitorService(self.directory.name, clock=lambda: self.now)
        self.sequence = 0

    def tearDown(self):
        self.directory.cleanup()

    def mutate(self, operation, owner="alice", **fields):
        self.sequence += 1
        return self.service.mutate(owner, {"requestId": f"request-{self.sequence}", "operation": operation, **fields})

    def rule(self, **fields):
        value = {"id": "price-1", "title": "突破 10 元", "code": "600000",
                 "conditions": [{"metric": "price", "op": "above", "threshold": 10}]}
        return self.mutate("rule.upsert", rule={**value, **fields})

    def quote(self, price=10.1, *, seconds=0, timestamp=None, **values):
        self.now += seconds
        quote = {"code": "600000", "price": price, "changePct": 1,
                 "quoteTime": datetime.fromtimestamp(self.now if timestamp is None else timestamp, CHINA).isoformat(),
                 "quoteSource": "test", **values}
        return self.service.evaluate_quotes({"600000": quote})

    def test_confirmation_jitter_and_duplicate_quotes_do_not_trigger_early(self):
        self.rule()
        self.assertEqual(self.quote(), [])
        first_timestamp = self.now
        self.assertEqual(self.quote(seconds=8, timestamp=first_timestamp), [])
        self.assertEqual(self.quote(9.99, seconds=1), [])
        self.assertEqual(self.quote(seconds=1), [])
        self.assertEqual(self.quote(seconds=9), [])
        notices = self.quote(seconds=1)
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["evidence"]["confirmedSeconds"], 10)
        self.assertEqual(notices[0]["aiState"], "pending")
        self.assertEqual(self.quote(seconds=20), [])

    def test_recovery_cooldown_and_read_never_rearm_by_themselves(self):
        self.rule(confirmSeconds=0, cooldownSeconds=20)
        notice = self.quote()[0]
        self.mutate("notification.read", id=notice["id"])
        self.assertEqual(self.quote(seconds=25), [])
        self.assertEqual(self.quote(9.999, seconds=1), [])  # inside 0.05% hysteresis
        self.assertEqual(self.quote(seconds=1), [])
        self.assertEqual(self.quote(9.99, seconds=1), [])
        second = self.quote(seconds=1)
        self.assertEqual(len(second), 1)
        self.quote(9.99, seconds=1)
        self.assertEqual(self.quote(seconds=1), [])
        self.assertEqual(self.service.library("alice")["rules"][0]["state"], "cooldown")
        self.assertEqual(len(self.quote(seconds=20)), 1)

    def test_provider_time_freshness_missing_values_and_break_reset_confirmation(self):
        self.rule()
        self.quote()
        self.assertEqual(self.quote(seconds=11, timestamp=self.now - 120), [])
        self.assertEqual(self.service.library("alice")["rules"][0]["state"], "stale")
        self.assertEqual(self.quote(seconds=1), [])
        self.assertEqual(self.quote(float("nan"), seconds=10), [])
        self.assertEqual(self.service.library("alice")["rules"][0]["state"], "data_unavailable")
        self.quote(seconds=1)
        self.assertEqual(self.quote(seconds=11, timestamp=self.now + 60), [])
        self.assertEqual(self.quote(seconds=1), [])
        self.assertEqual(len(self.quote(seconds=10)), 1)

    def test_market_closed_weekends_and_unknown_timezone_never_trigger(self):
        self.rule(confirmSeconds=0)
        self.now = datetime(2026, 9, 21, 12, 0, tzinfo=CHINA).timestamp()
        self.assertEqual(self.quote(), [])
        self.assertEqual(self.service.library("alice")["rules"][0]["state"], "market_closed")
        self.now = datetime(2026, 9, 20, 10, 0, tzinfo=CHINA).timestamp()
        self.assertEqual(self.quote(), [])
        self.now = datetime(2026, 9, 21, 10, 0, tzinfo=CHINA).timestamp()
        self.assertEqual(self.service.evaluate_quotes({"600000": {"price": 11, "quoteTime": "2026-09-21T10:00:00"}}), [])

    def test_restart_persists_armed_cooldown_and_request_receipts(self):
        request = {"requestId": "original", "operation": "rule.upsert", "expectedRevision": 0,
                   "rule": {"id": "r", "code": "600000", "title": "测试", "confirmSeconds": 0,
                            "conditions": [{"metric": "price", "op": "above", "threshold": 10}]}}
        receipt = self.service.mutate("alice", request)
        self.quote()
        self.service = MonitorService(self.directory.name, clock=lambda: self.now)
        replay = self.service.mutate("alice", request)
        self.assertTrue(replay["replayed"])
        self.assertEqual(receipt["revision"], replay["revision"])
        self.assertEqual(self.quote(seconds=400), [])
        self.assertEqual(len(self.service.library("alice")["notifications"]), 1)
        self.quote(9, seconds=1)
        self.assertEqual(len(self.quote(seconds=1)), 1)

    def test_account_isolation_revision_and_nested_owner_rejection(self):
        self.rule(confirmSeconds=0)
        notice = self.quote()[0]
        self.assertEqual(self.service.library("bob")["rules"], [])
        self.assertIsNone(self.service.get_notification("bob", notice["id"]))
        with self.assertRaises(MonitorError) as failure:
            self.mutate("notification.resolve", owner="bob", id=notice["id"])
        self.assertEqual(failure.exception.status, 404)
        with self.assertRaises(MonitorError) as failure:
            self.mutate("rule.pause", id="price-1", expectedRevision=0)
        self.assertEqual(failure.exception.code, "revision_conflict")
        result = dispatch_monitor_safe(self.service, "alice", "mutate", {
            "requestId": "bad-owner", "operation": "notification.create",
            "notification": {"title": "test", "body": "test", "nested": {"ownerId": "bob"}}})
        self.assertEqual(result["error"]["code"], "owner_not_allowed")
        self.assertNotIn("ownerId", self.service.library("alice")["notifications"][0])

    def test_request_id_mismatch_and_invalid_nonfinite_parameters_are_atomic(self):
        request = {"requestId": "r", "operation": "notification.create", "notification": {"title": "hi", "body": "body"}}
        self.service.mutate("alice", request)
        with self.assertRaises(MonitorError) as failure:
            self.service.mutate("alice", {**request, "notification": {"title": "different", "body": "body"}})
        self.assertEqual(failure.exception.code, "request_id_conflict")
        with self.assertRaises(MonitorError):
            self.rule(confirmSeconds=float("nan"))
        self.assertEqual(self.service.library("alice")["rules"], [])

    def test_ai_failure_preserves_visual_signal_and_completion_is_idempotent(self):
        self.rule(confirmSeconds=0)
        notice = self.quote()[0]
        job = self.service.claim_ai_job()
        self.assertEqual(job["notification"]["id"], notice["id"])
        self.assertIsNone(self.service.claim_ai_job())
        failed = self.service.finish_ai_job(job["jobId"], None, error="provider unavailable")
        self.assertEqual(failed["aiState"], "failed")
        self.assertEqual(failed["body"], notice["body"])
        self.assertEqual(failed["status"], "unread")
        self.assertEqual(failed["evidence"], notice["evidence"])
        self.assertEqual(self.service.finish_ai_job(job["jobId"], "late answer"), failed)
        self.assertIsNone(self.service.claim_ai_job())

    def test_ai_recovery_fences_expired_worker_and_caps_retries(self):
        self.rule(confirmSeconds=0)
        notice = self.quote()[0]
        first = self.service.claim_ai_job()
        self.now += 181
        self.service = MonitorService(self.directory.name, clock=lambda: self.now)
        second = self.service.claim_ai_job()
        self.assertIsNone(self.service.finish_ai_job(first["jobId"], "obsolete"))
        self.assertEqual(second["attempt"], 2)
        self.now += 181
        self.assertEqual(self.service.claim_ai_job()["attempt"], 3)
        self.now += 181
        self.assertIsNone(self.service.claim_ai_job())
        self.assertEqual(self.service.get_notification("alice", notice["id"])["aiState"], "failed")

    def test_ai_enrichment_and_channel_receipts_preserve_original_and_read_state(self):
        self.rule(confirmSeconds=0)
        original = self.quote()[0]
        job = self.service.claim_ai_job()
        notice = self.service.finish_ai_job(job["jobId"], "分析后的补充")
        self.assertEqual(notice["originalBody"], original["body"])
        self.assertEqual(notice["evidence"], original["evidence"])
        self.service.mark_delivery("alice", notice["id"], {"push": {"status": "sent"}})
        self.service.mark_delivery("alice", notice["id"], {"voice": {"status": "played"}})
        item = self.service.pending_notifications()[0]
        self.assertEqual(set(item["delivery"]), {"push", "voice"})
        self.assertEqual(item["status"], "unread")
        self.mutate("notification.resolve", id=notice["id"])
        self.assertEqual(self.service.pending_notifications(), [])

    def test_generic_notice_has_no_recursive_ai_and_summaries_include_unread(self):
        result = self.mutate("notification.create", notification={"code": "600000", "title": "人工提醒", "body": "研究结束"})
        self.assertEqual(result["library"]["summary"]["600000"]["unreadCount"], 1)
        self.assertEqual(result["library"]["notifications"][0]["aiState"], "complete")
        self.assertIsNone(self.service.claim_ai_job())

    def test_deferred_ai_job_is_durable_and_does_not_exhaust_attempts(self):
        self.rule(confirmSeconds=0)
        self.quote()
        first = self.service.claim_ai_job()
        self.service.defer_ai_job(first["jobId"], 300)
        self.assertIsNone(self.service.claim_ai_job())
        self.service = MonitorService(self.directory.name, clock=lambda: self.now)
        self.now += 300
        second = self.service.claim_ai_job()
        self.assertEqual(second["attempt"], 1)
        self.assertIsNone(self.service.finish_ai_job(first["jobId"], "expired"))
        self.assertEqual(self.service.finish_ai_job(second["jobId"], "done")["aiState"], "complete")

    def test_resolved_notice_cancels_unstarted_ai_but_running_job_can_finish(self):
        self.rule(confirmSeconds=0, cooldownSeconds=0)
        first = self.quote()[0]
        self.mutate("notification.resolve", id=first["id"])
        self.assertIsNone(self.service.claim_ai_job())
        self.assertTrue(self.service.get_notification("alice", first["id"])["aiCancelled"])
        self.quote(9, seconds=1)
        second = self.quote(seconds=1)[0]
        job = self.service.claim_ai_job()
        self.mutate("notification.resolve", id=second["id"])
        finished = self.service.finish_ai_job(job["jobId"], "already in progress")
        self.assertEqual(finished["aiState"], "complete")
        self.assertEqual(finished["status"], "resolved")

    def test_overlapping_gateway_instances_create_one_event_and_one_ai_claim(self):
        self.rule(confirmSeconds=0)
        other = MonitorService(self.directory.name, clock=lambda: self.now)
        quotes = {"600000": {"price": 11, "quoteTime": datetime.fromtimestamp(self.now, CHINA).isoformat()}}
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = [pool.submit(service.evaluate_quotes, quotes) for service in (self.service, other)]
            self.assertEqual(sum(len(call.result()) for call in calls), 1)
            claims = [pool.submit(service.claim_ai_job) for service in (self.service, other)]
            self.assertEqual(sum(call.result() is not None for call in claims), 1)

    def test_any_condition_rearms_only_after_all_conditions_recover(self):
        self.rule(confirmSeconds=0, cooldownSeconds=0, match="any", conditions=[
            {"metric": "price", "op": "above", "threshold": 10},
            {"metric": "changePct", "op": "above", "threshold": 5}])
        self.assertEqual(len(self.quote(changePct=6)), 1)
        self.quote(9, seconds=1, changePct=6)
        self.assertEqual(self.quote(seconds=1, changePct=6), [])
        self.quote(9, seconds=1, changePct=4.9)
        self.assertEqual(len(self.quote(seconds=1)), 1)


if __name__ == "__main__":
    unittest.main()
