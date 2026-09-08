"""新卡提醒的时机规则(2026-09-08 用户:「不能是学完立刻就进行,也不能太晚,数量也不能太多」)。

只测决策逻辑:数量/静置/时间窗三个条件怎么组合,以及**时机未到不等于不需要**。
"""
from pathlib import Path
import sys
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "extensions" / "bw-reader-webext" / "windows" / "computer-voice-desktop"
if str(DESKTOP) not in sys.path:
    sys.path.insert(0, str(DESKTOP))

import replication_notifications as rn  # noqa: E402


class FakeStore:
    def __init__(self, open_items=()):
        self.created = []
        self.resolved = []
        self._open = list(open_items)

    def create(self, **kw):
        self.created.append(kw)

    def open_items(self):
        return list(self._open)

    def resolve(self, item_id, by="auto", note=""):
        self.resolved.append((item_id, note))


WAKE_MS = 1_788_900_000_000   # 测试里"下一个起床点"的固定值


def run(store, *, new, due=0, age=99.0, hour=14, schedule=None):
    """跑一次生产者,把数量/静置/钟点/作息都钉死(作息读取本身另有用例)。"""
    plan = {"wakeHour": 8, "sleepHour": 24, "newThreshold": rn.REVIEW_NEW_SPEAK_THRESHOLD,
            "newMinAgeHours": rn.REVIEW_NEW_MIN_AGE_HOURS, "newBatch": rn.REVIEW_NEW_BATCH}
    plan.update(schedule or {})
    with mock.patch.object(rn, "count_due_cards", return_value=(due, new)),             mock.patch.object(rn, "oldest_new_card_age_hours", return_value=age),             mock.patch.object(rn, "review_schedule", return_value=plan),             mock.patch.object(rn, "next_window_start_ms", return_value=WAKE_MS),             mock.patch.object(rn, "time", wraps=time) as fake_time:
        fake_time.localtime.return_value = time.struct_time(
            (2026, 9, 8, hour, 0, 0, 0, 251, 0))
        fake_time.strftime.side_effect = time.strftime
        return rn.ensure_review_due(store, Path("."))


class ReviewNewTimingTests(unittest.TestCase):
    def test_all_three_conditions_met_speaks_with_a_bounded_batch(self):
        store = FakeStore()
        out = run(store, new=25, due=3)
        made = [c for c in store.created if c["kind"] == "review-new"]
        self.assertEqual(len(made), 1)
        self.assertIn("25", made[0]["title"])
        # 「数量不能太多太占用时间」:正文只建议做一批,不是把 25 张全推给他
        self.assertIn("先做 %d 张" % rn.REVIEW_NEW_BATCH, made[0]["body"])
        self.assertIn("分钟", made[0]["body"])
        self.assertIn("3 张到期", made[0]["body"])
        self.assertEqual(out["new"], 25)

    def test_freshly_made_cards_are_not_pushed_back_at_you(self):
        # 「不能是学完立刻就进行」:刚做完的卡内容还在脑子里,问不出真实记忆
        store = FakeStore()
        run(store, new=25, age=rn.REVIEW_NEW_MIN_AGE_HOURS - 0.5)
        self.assertEqual([c for c in store.created if c["kind"] == "review-new"], [])

    def test_outside_the_window_still_records_but_sleeps_until_morning(self):
        """过了点的卡不能被忽视:照样建通知,只是推迟到下一个起床点才浮现。"""
        for hour in (3, 7):   # 深夜与清晨,都在 8-24 之外
            store = FakeStore()
            run(store, new=25, hour=hour)
            made = [c for c in store.created if c["kind"] == "review-new"]
            self.assertEqual(len(made), 1, "%d 点也要留痕" % hour)
            self.assertEqual(made[0]["activate_at_ms"], WAKE_MS,
                             "%d 点建的通知要蛰伏到起床点" % hour)

    def test_inside_the_window_shows_immediately(self):
        store = FakeStore()
        run(store, new=25, hour=14)
        made = [c for c in store.created if c["kind"] == "review-new"]
        self.assertIsNone(made[0]["activate_at_ms"], "窗口内就该立刻可见")

    def test_schedule_file_overrides_the_defaults(self):
        """作息随时会变:改配置文件即可,不用改代码。"""
        store = FakeStore()
        run(store, new=25, hour=7, schedule={"wakeHour": 6, "sleepHour": 22})
        made = [c for c in store.created if c["kind"] == "review-new"]
        self.assertIsNone(made[0]["activate_at_ms"], "6 点起床的话 7 点已在窗口内")

    def test_schedule_reader_clamps_and_falls_back(self):
        import json, tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(rn.review_schedule(root)["wakeHour"],
                             rn.REVIEW_NEW_WINDOW_HOURS[0], "没有配置文件就用默认")
            (root / rn.REVIEW_SCHEDULE_FILE).write_text(
                json.dumps({"wakeHour": 30, "sleepHour": 2, "newBatch": 999}), encoding="utf-8")
            plan = rn.review_schedule(root)
            self.assertEqual((plan["wakeHour"], plan["sleepHour"]), rn.REVIEW_NEW_WINDOW_HOURS,
                             "起点晚于终点(跨夜)先不支持,回落默认而不是永远不提醒")
            self.assertLessEqual(plan["newBatch"], 50, "数值要钳制")
            (root / rn.REVIEW_SCHEDULE_FILE).write_text("{ 坏掉的 json", encoding="utf-8")
            self.assertEqual(rn.review_schedule(root)["wakeHour"], rn.REVIEW_NEW_WINDOW_HOURS[0],
                             "文件坏了也只回落默认,不能让复习提醒整条停摆")


    def test_timing_not_yet_reached_must_not_resolve_an_open_reminder(self):
        """时机未到 ≠ 已经不需要。把它当成回落消掉,通知会在窗口边缘反复生灭。"""
        open_item = {"id": "n1", "kind": "review-new"}
        for kwargs in ({"hour": 23}, {"age": 0.1}):
            store = FakeStore([open_item])
            run(store, new=25, **kwargs)
            self.assertEqual(store.resolved, [], "只是没到点,不该消掉 %r" % kwargs)

    def test_only_a_real_drop_resolves_it(self):
        store = FakeStore([{"id": "n1", "kind": "review-new"}])
        run(store, new=rn.REVIEW_NEW_SPEAK_THRESHOLD - 1)
        self.assertEqual([r[0] for r in store.resolved], ["n1"])
        self.assertIn("开始学了", store.resolved[0][1])

    def test_thresholds_stay_humane(self):
        self.assertLessEqual(rn.REVIEW_NEW_BATCH, 20, "一次别超过 20 张")
        self.assertGreaterEqual(rn.REVIEW_NEW_MIN_AGE_HOURS, 1)
        start, end = rn.REVIEW_NEW_WINDOW_HOURS
        # 窗口 = 起床到睡前(用户 2026-09-08)。终点允许到 24(午夜):睡前是当天最后的补课机会。
        # 这两个数字是占位,真正的边界将由睡眠状态判定接管。
        self.assertTrue(6 <= start <= 10, "起点该在起床前后")
        self.assertTrue(start < end <= 24, "终点最晚到午夜")


if __name__ == "__main__":
    unittest.main()
