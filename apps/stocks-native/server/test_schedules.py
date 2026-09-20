import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import tempfile
import unittest
from unittest.mock import patch

from monitoring import MonitorService
from plans import PlanService
from reports import ReportService
from schedule_runtime import ScheduleRuntime
from schedules import ScheduleError, ScheduleService, dispatch_schedule_safe, next_run, normalize_schedule
from stock_research_workflow import _basis, _output, workflow_template


def stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def ai_report(code):
    return {"code": code, "title": "行情观察", "summary": "保持观察，等待更多依据。",
            "direction": "neutral", "confidence": "low", "points": ["报价已获取"], "risks": ["信息有限"],
            "recommendedVariantId": "standard", "variants": [
                {"id": "safe", "label": "保守", "action": "观望", "targetKind": "无", "reason": "等待验证"},
                {"id": "standard", "label": "标准", "action": "观望", "targetKind": "无", "reason": "关注后续数据"}]}


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.now = stamp("2026-09-21T00:00:00Z")
        self.service = ScheduleService(self.directory.name, clock=lambda: self.now)
        self.monitor = MonitorService(self.directory.name, clock=lambda: self.now)
        self.plans = PlanService(self.directory.name)
        self.reports = ReportService(self.directory.name, self.plans, self.monitor, clock=lambda: self.now)
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
            return json.dumps({"items": [ai_report("600000")]}, ensure_ascii=False)

        runtime = ScheduleRuntime({"schedules": self.service, "monitor": self.monitor, "reports": self.reports,
                                   "live": Live(), "state": self.directory.name}, analyze=analyze)
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
        quote = seen[1][1]["context"]["600000"]["sections"]["quote"]
        self.assertEqual((quote["prevClose"], quote["changeAmount"], quote["turnover"]), (11.5, 0.5, 1200000))
        self.assertEqual(len(self.monitor.library("alice")["notifications"]), 2)
        self.assertEqual(len(self.reports.list("alice")["items"]), 1)

    def flow_task(self, selection=None, sections=None, **fields):
        task = self.task(kind="workflow", **fields)
        task.pop("prompt")
        task["workflow"] = {"id": "stock-research", "version": 1, "params": {
            "selection": selection or {"kind": "codes", "codes": ["600000"]},
            "sections": sections or ["quote"], "prompt": "检查这些股票并保存报告"}}
        return task

    def flow_due(self, task=None):
        self.mutate("task.upsert", task=task or self.flow_task())
        self.now += 60
        self.service.tick()
        return self.service.claim()

    def flow_runtime(self, *, library=None, analyze=None, seen=None, bad_codes=()):
        seen = seen if seen is not None else []
        class Live:
            async def quotes(inner, codes):
                seen.append(("quotes", list(codes)))
                return {code: {"code": code, "price": 12, "quoteTime": "2026-09-21T08:01:00"}
                        for code in codes if code not in bad_codes}
        class Selection:
            def load_library(inner, owner):
                seen.append(("selection", owner))
                return copy.deepcopy(library)
        class News:
            async def feed(inner, category, *, code, limit):
                seen.append(("news", code))
                return {"status": "cached", "items": [{"title": "实际新闻", "url": "https://example.com/news",
                    "publishedAt": "2026-09-20T23:00:00Z"}], "fetchedAt": "2026-09-21T00:00:00Z"}
        async def fake_ai(task, data, state):
            seen.append(("ai", data))
            return {"items": [ai_report(code) for code in data["context"]]}
        return ScheduleRuntime({"schedules": self.service, "monitor": self.monitor, "reports": self.reports,
            "selection": Selection(), "news": News(), "live": Live(), "state": self.directory.name}, analyze=analyze or fake_ai)

    def test_workflow_template_schema_and_no_arbitrary_tools(self):
        saved = self.mutate("task.upsert", task=self.flow_task())["task"]
        self.assertEqual(saved["workflowDefinition"], workflow_template())
        self.assertIn("task.run", self.service.catalog()["operations"])
        for edit in (lambda t: t.update(target={"kind": "folder", "folderId": "x"}),
                     lambda t: t["workflow"].update(id="arbitrary"),
                     lambda t: t["workflow"].update(steps=[{"tool": "shell"}]),
                     lambda t: t["workflow"]["params"]["selection"].update(ownerId="bob")):
            task = self.flow_task()
            edit(task)
            with self.subTest(task=task), self.assertRaises(ScheduleError):
                self.mutate("task.upsert", task=task)

    def test_folder_resolved_at_run_owner_bound_eleven_stocks_all_saved(self):
        codes = [f"600{i:03}" for i in range(11)]
        library = {"revision": 12, "groups": [{"id": "favorites", "status": "ready", "codes": [codes[0]]}]}
        seen = []
        runtime = self.flow_runtime(library=library, seen=seen)
        job = self.flow_due(self.flow_task({"kind": "folder", "folderId": "favorites"}, ["quote", "news"]))
        library["groups"][0]["codes"] = codes
        asyncio.run(runtime.execute(job))
        self.assertEqual(seen[0], ("selection", "alice"))
        self.assertEqual([len(data["context"]) for name, data in seen if name == "ai"], [5, 5, 1])
        result = self.service.runs("alice", "morning")["runs"][0]["result"]
        self.assertEqual(result["selectionRevision"], 12)
        self.assertEqual(result["resolvedCodes"], codes)
        self.assertEqual(result["counts"], {"success": 11, "data_insufficient": 0, "failed": 0})
        self.assertEqual(len(self.reports.list("alice")["items"]), 11)
        self.assertEqual(self.reports.list("bob")["items"], [])
        self.assertEqual(self.monitor.library("alice")["rules"], [])
        report = self.reports.list("alice")["items"][0]
        self.assertEqual(report["basis"]["marketAsOf"], "2026-09-21T08:01:00+08:00")
        self.assertEqual(report["plan"]["basis"], report["basis"])
        self.assertTrue(any(source["title"] == "实际新闻" for source in report["sources"]))

    def test_source_timestamp_offset_preserved(self):
        context = {"sections": {"quote": {"price": 12}}, "asOf": {"quote": "2026-09-21T00:01:00Z"}}
        self.assertEqual(_basis(context)["marketAsOf"], "2026-09-21T00:01:00+00:00")
        context["asOf"]["quote"] = "2026-09-21 08:01:00"
        self.assertEqual(_basis(context)["marketAsOf"], "2026-09-21T08:01:00+08:00")

    def test_date_only_basis_does_not_invent_midnight(self):
        context = {"code": "600000", "sections": {"quote": {"price": 12}}, "asOf": {"quote": "2026-09-18"}}
        output = _output(ai_report("600000"), context, {})
        self.assertEqual(output["report"]["basis"]["marketAsOf"], "2026-09-18")
        self.assertEqual(output["plan"]["basis"], output["report"]["basis"])
        self.assertIn("缺少具体时刻", output["report"]["risks"][0])

    def test_unready_missing_and_oversize_folder_never_calls_ai(self):
        for groups in ([], [{"id": "favorites", "status": "needs_migration", "codes": ["600000"]}],
                       [{"id": "favorites", "status": "ready", "codes": [f"600{i:03}" for i in range(51)]}]):
            with self.subTest(groups=groups):
                seen = []
                runtime = self.flow_runtime(library={"revision": 1, "groups": groups}, seen=seen)
                task = self.flow_task({"kind": "folder", "folderId": "favorites"}, id="f" + str(self.sequence),
                    schedule={"kind": "once", "at": datetime.fromtimestamp(self.now + 60, timezone.utc).isoformat(), "timezone": "Asia/Tokyo"})
                job = self.flow_due(task)
                # Previous runs retain the existing 30-second AI queue cooldown.
                if job is None:
                    self.now += 31
                    job = self.service.claim()
                asyncio.run(runtime.execute(job))
                self.assertEqual(seen, [("selection", "alice")])
        self.assertEqual(self.reports.list("alice")["items"], [])

    def test_missing_quote_skips_only_that_stock_and_daily_budget_is_visible(self):
        seen = []
        runtime = self.flow_runtime(seen=seen, bad_codes=["600001"])
        job = self.flow_due(self.flow_task({"kind": "codes", "codes": ["600000", "600001"]}))
        asyncio.run(runtime.execute(job))
        self.assertEqual(job["progress"]["counts"], {"success": 1, "data_insufficient": 1, "failed": 0})
        self.assertEqual(len([1 for name, _ in seen if name == "ai"]), 1)
        self.now += 31
        task = self.flow_task(id="limited", schedule={"kind": "once", "at": datetime.fromtimestamp(self.now + 60, timezone.utc).isoformat(), "timezone": "Asia/Tokyo"})
        job = self.flow_due(task)
        with self.service._db(write=True) as db:
            db.execute("UPDATE analysis_budget SET batches=20 WHERE owner='alice'")
        seen.clear()
        asyncio.run(runtime.execute(job))
        self.assertFalse(any(name == "ai" for name, _ in seen))
        self.assertIn("20", job["progress"]["error"])
        self.assertEqual(job["progress"]["stocks"]["600000"]["status"], "failed")

    def test_manual_run_keeps_original_once_schedule_and_is_idempotent(self):
        saved = self.mutate("task.upsert", task=self.flow_task())["task"]
        request = {"requestId": "run-now", "operation": "task.run", "id": "morning"}
        first = self.service.mutate("alice", request)
        self.assertEqual(self.service.mutate("alice", request)["runId"], first["runId"])
        asyncio.run(self.flow_runtime().execute(self.service.claim()))
        current = self.service.get("alice", "morning")["task"]
        self.assertEqual((current["status"], current["nextRunAt"], current["version"]),
                         ("active", saved["nextRunAt"], saved["version"]))

    def test_cancel_during_ai_prevents_report_and_notification(self):
        async def analyze(task, data, state):
            self.mutate("task.cancel", id="morning")
            return {"items": [ai_report("600000")]}
        job = self.flow_due()
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(self.flow_runtime(analyze=analyze).execute(job))
        self.assertEqual(self.reports.list("alice")["items"], [])
        self.assertEqual(self.monitor.library("alice")["notifications"], [])

    def test_cached_output_recovery_never_repeats_ai_even_after_report_write(self):
        seen = []
        runtime = self.flow_runtime(seen=seen)
        job = self.flow_due()
        original_save = self.reports.save
        saved_payloads = []
        def crash_after_save(owner, payload, source=None):
            saved_payloads.append(copy.deepcopy(payload))
            original_save(owner, payload, source)
            raise RuntimeError("worker interrupted after durable report")
        with patch.object(self.reports, "save", side_effect=crash_after_save), self.assertRaises(RuntimeError):
            asyncio.run(runtime.execute(job))
        self.assertEqual(len(self.reports.list("alice")["items"]), 1)
        self.service.abandon(job)
        recovered = self.service.claim()
        def replay_save(owner, payload, source=None):
            self.assertEqual(payload, saved_payloads[0])
            return original_save(owner, payload, source)
        with patch.object(self.reports, "save", side_effect=replay_save):
            asyncio.run(runtime.execute(recovered))
        self.assertEqual(len([1 for name, _ in seen if name == "ai"]), 1)
        self.assertEqual(len(self.reports.list("alice")["items"]), 1)
        self.assertEqual(recovered["progress"]["counts"]["success"], 1)

    def test_ambiguous_interrupted_ai_is_not_billed_again(self):
        job = self.flow_due()
        progress = {"workflow": {"id": "stock-research", "version": 1}, "resolvedCodes": ["600000"],
                    "stocks": {"600000": {"status": "pending"}}}
        self.service.checkpoint(job, progress)
        self.assertTrue(self.service.begin_ai_step(job, "0", ["600000"]))
        self.service.abandon(job)
        recovered, seen = self.service.claim(), []
        asyncio.run(self.flow_runtime(seen=seen).execute(recovered))
        self.assertEqual(seen, [])
        self.assertEqual(recovered["progress"]["batches"]["0"]["status"], "interrupted")
        self.assertEqual(recovered["progress"]["counts"]["failed"], 1)


if __name__ == "__main__":
    unittest.main()
