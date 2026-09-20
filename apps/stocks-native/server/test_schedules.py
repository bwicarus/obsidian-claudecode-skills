import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import tempfile
import unittest
from unittest.mock import patch

from monitoring import MonitorService
from schedule_runtime import ScheduleRuntime
from schedules import ScheduleError, ScheduleService, dispatch_schedule_safe, next_run, normalize_schedule


def stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.now = stamp("2026-09-21T00:00:00Z")
        self.service = ScheduleService(self.directory.name, clock=lambda: self.now)
        self.monitor = MonitorService(self.directory.name, clock=lambda: self.now)
        self.sequence = 0

    def tearDown(self):
        self.directory.cleanup()

    def mutate(self, operation, owner="alice", **fields):
        self.sequence += 1
        return self.service.mutate(owner, {"requestId": f"r{self.sequence}", "operation": operation, **fields})

    def task(self, **fields):
        return {"id": "morning", "kind": "reminder", "title": "早上提醒", "prompt": "看一下股票",
                "schedule": {"kind": "once", "at": "2026-09-21T09:01:00", "timezone": "Asia/Tokyo"}, **fields}

    def due(self, **fields):
        self.mutate("task.upsert", task=self.task(**fields))
        self.now += 60
        self.service.tick()
        return self.service.claim()

    def test_explicit_timezone_weekly_and_dst(self):
        with self.assertRaises(ScheduleError):
            normalize_schedule({"kind": "once", "at": "2026-09-21T09:00:00"})
        schedule, at = normalize_schedule({"kind": "once", "at": "2026-09-21T00:01:00Z", "timezone": "Asia/Tokyo"})
        self.assertEqual(schedule["at"], "2026-09-21T09:01:00+09:00")
        self.assertEqual(at, self.now + 60)
        schedule, _ = normalize_schedule({"kind": "weekly", "time": "09:00", "days": ["MO", "WE"], "timezone": "Asia/Tokyo"})
        self.assertEqual(next_run(schedule, self.now), stamp("2026-09-23T00:00:00Z"))
        schedule, _ = normalize_schedule({"kind": "daily", "time": "02:30", "timezone": "America/New_York"})
        self.assertEqual(next_run(schedule, stamp("2026-03-08T05:00:00Z")), stamp("2026-03-09T06:30:00Z"))
        with self.assertRaises(ScheduleError):
            normalize_schedule({"kind": "once", "at": "2026-11-01T01:30:00", "timezone": "America/New_York"})

    def test_owner_idempotency_and_revision(self):
        request = {"requestId": "create", "operation": "task.upsert", "task": self.task()}
        first = self.service.mutate("alice", request)
        self.assertTrue(self.service.mutate("alice", request)["replayed"])
        self.assertEqual(self.service.list("alice")["revision"], 1)
        self.assertEqual(self.service.list("bob")["tasks"], [])
        with self.assertRaises(ScheduleError):
            self.service.get("bob", first["task"]["id"])
        with self.assertRaises(ScheduleError):
            self.mutate("task.pause", id="morning", expectedRevision=0)
        request["task"]["prompt"] = "changed"
        with self.assertRaises(ScheduleError):
            self.service.mutate("alice", request)
        rejected = dispatch_schedule_safe(self.service, "alice", "mutate", {"ownerId": "bob"})
        self.assertEqual(rejected["error"]["code"], "owner_not_allowed")

    def test_concurrent_tick_claim_and_once_completion(self):
        self.mutate("task.upsert", task=self.task())
        self.now += 60
        other = ScheduleService(self.directory.name, clock=lambda: self.now)
        with ThreadPoolExecutor(2) as pool:
            list(pool.map(lambda service: service.tick(), [self.service, other]))
            jobs = list(pool.map(lambda service: service.claim(), [self.service, other]))
        self.assertEqual(sum(job is not None for job in jobs), 1)
        job = next(job for job in jobs if job)
        self.service.prepare(job, "提醒正文")
        notice = self.service.publish(job, self.monitor)
        self.assertIsNotNone(notice)
        self.assertEqual(self.service.get("alice", "morning")["task"]["status"], "completed")
        self.assertEqual(len(self.monitor.library("alice")["notifications"]), 1)
        self.assertEqual(self.service.runs("alice", "morning")["runs"][0]["notificationId"], notice)

    def test_missed_once_expired_and_resume_does_not_backfill(self):
        self.mutate("task.upsert", task=self.task())
        self.now += 121
        self.service.tick()
        self.assertIsNone(self.service.claim())
        self.assertEqual(self.service.get("alice", "morning")["task"]["status"], "expired")
        self.mutate("task.upsert", task=self.task(schedule={"kind": "daily", "time": "09:03", "timezone": "Asia/Tokyo"}))
        self.mutate("task.pause", id="morning")
        self.now += 240
        result = self.mutate("task.resume", id="morning")
        self.assertEqual(result["task"]["nextRunAt"], "2026-09-22T00:03:00+00:00")
        self.service.tick()
        self.assertIsNone(self.service.claim())

    def test_cancel_or_edit_invalidates_inflight_and_ready_output(self):
        job = self.due()
        self.service.prepare(job, "过时结果")
        self.mutate("task.cancel", id="morning")
        self.assertFalse(self.service.renew(job))
        self.assertIsNone(self.service.publish(job, self.monitor))
        self.assertEqual(self.monitor.library("alice")["notifications"], [])
        self.assertEqual(self.service.runs("alice", "morning")["runs"][0]["status"], "cancelled")

    def test_crash_after_notification_write_replays_same_notice(self):
        job = self.due(deliveryMode="call")
        self.service.prepare(job, "现在可以查看")
        with patch.object(self.service, "_complete_once", side_effect=RuntimeError("crash")):
            with self.assertRaises(RuntimeError):
                self.service.publish(job, self.monitor)
        notice = self.service.publish(job, self.monitor)
        notices = self.monitor.library("alice")["notifications"]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["id"], notice)
        self.assertEqual(notices[0]["severity"], "urgent")
        self.assertEqual(notices[0]["deliveryMode"], "call")
        self.assertEqual(notices[0]["aiState"], "complete")

    def test_abandoned_claim_recovers_and_old_token_cannot_publish(self):
        old = self.due()
        self.service.abandon(old)
        new = self.service.claim()
        self.assertNotEqual(old["token"], new["token"])
        self.assertFalse(self.service.prepare(old, "旧结果"))
        self.assertTrue(self.service.prepare(new, "新结果"))

    def test_analysis_lease_cooldown_survives_success(self):
        job = self.due(kind="analysis", codes=["600000"])
        self.service.prepare(job, "分析结果")
        self.service.publish(job, self.monitor)
        self.service.abandon(job)
        with self.service._db() as db:
            self.assertGreater(db.execute("SELECT expires FROM leases WHERE name='analysis'").fetchone()[0], self.now)

    def test_cancel_releases_only_original_analysis_lease(self):
        old = self.due(kind="analysis", codes=["600000"])
        self.mutate("task.cancel", id="morning")
        self.service.abandon(old)
        with self.service._db() as db:
            self.assertEqual(db.execute("SELECT expires FROM leases WHERE name='analysis'").fetchone()[0], self.now)
        self.mutate("task.upsert", task=self.task(id="other", kind="analysis", codes=["600000"],
                    schedule={"kind": "once", "at": "2026-09-21T09:02:00", "timezone": "Asia/Tokyo"}))
        self.now += 60
        self.service.tick()
        new = self.service.claim()
        self.assertIsNotNone(new)
        self.assertNotEqual(old["token"], new["token"])
        self.service.abandon(old)
        with self.service._db() as db:
            lease = db.execute("SELECT token,expires FROM leases WHERE name='analysis'").fetchone()
            self.assertEqual(lease["token"], new["token"])
            self.assertGreater(lease["expires"], self.now)

    def test_runtime_reminder_uses_no_ai_and_analysis_fetches_at_execution(self):
        seen = []

        class Live:
            async def quotes(inner, codes):
                seen.append(("quotes", codes))
                return {"600000": {"code": "600000", "price": 12, "prevClose": 11.5, "changeAmount": 0.5,
                                   "turnover": 1200000, "quoteTime": "2026-09-21T08:01:00+08:00", "quoteSource": "test"}}

        async def analyze(task, data, state):
            seen.append(("ai", data))
            return "600000 行情时间 08:01，最新可用报价12元。"

        runtime = ScheduleRuntime({"schedules": self.service, "monitor": self.monitor, "live": Live(), "state": self.directory.name}, analyze=analyze)
        reminder = self.due()
        asyncio.run(runtime.execute(reminder))
        self.assertEqual(seen, [])
        self.mutate("task.upsert", task=self.task(id="analysis", kind="analysis", codes=["600000"],
                    schedule={"kind": "once", "at": "2026-09-21T09:02:00", "timezone": "Asia/Tokyo"}))
        self.now += 60
        self.service.tick()
        asyncio.run(runtime.execute(self.service.claim()))
        self.assertEqual(seen[0], ("quotes", ["600000"]))
        self.assertEqual(seen[1][0], "ai")
        self.assertEqual(seen[1][1]["fetchedAt"], "2026-09-21T00:02:00+00:00")
        quote = seen[1][1]["quotes"]["600000"]
        self.assertEqual((quote["prevClose"], quote["changeAmount"], quote["turnover"]), (11.5, 0.5, 1200000))
        self.assertEqual(len(self.monitor.library("alice")["notifications"]), 2)


if __name__ == "__main__":
    unittest.main()
