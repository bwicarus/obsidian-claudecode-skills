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
        self.updated = []
        self._open = list(open_items)

    def create(self, **kw):
        self.created.append(kw)

    def open_items(self):
        return list(self._open)

    def resolve(self, item_id, by="auto", note=""):
        self.resolved.append((item_id, note))

    def update(self, item_id, **kw):
        self.updated.append((item_id, kw))
        for one in self._open:
            if one.get("id") == item_id:
                one.update({k: v for k, v in kw.items() if k in ("title", "body")})
        return {}


WAKE_MS = 1_788_900_000_000   # 测试里"下一个起床点"的固定值


def run(store, *, new, due=0, age=99.0, hour=14, schedule=None, awake=True,
        blocked=0):
    """跑一次生产者,把数量/静置/钟点/作息/醒着都钉死(各自的读取另有用例)。

    ⚠ 只 mock **一个** review_counts：四个数（到期/新卡/可评/卡住）由同一次
    遍历产出，分开 mock 会造出真实世界里不可能出现的组合（可评+卡住 != 新卡），
    于是测试通过而线上失败。"""
    plan = {"wakeHour": 8, "sleepHour": 24, "newThreshold": rn.REVIEW_NEW_SPEAK_THRESHOLD,
            "newMinAgeHours": rn.REVIEW_NEW_MIN_AGE_HOURS, "newBatch": rn.REVIEW_NEW_BATCH}
    plan.update(schedule or {})
    counts = {"due": due, "new": new,
              "newGradable": new - blocked, "newBlocked": blocked}
    with mock.patch.object(rn, "review_counts", return_value=counts), \
            mock.patch.object(rn, "oldest_new_card_age_hours", return_value=age), \
            mock.patch.object(rn, "review_schedule", return_value=plan), \
            mock.patch.object(rn, "looks_awake", return_value=(awake, "test")), \
            mock.patch.object(rn, "next_window_start_ms", return_value=WAKE_MS), \
            mock.patch.object(rn, "time", wraps=time) as fake_time:
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

    def test_only_a_real_drop_closes_it(self):
        """真回落才收尾。

        ⚠ 2026-09-09 契约变了：回落不再**直接入库**，而是把最初那条改写成
        「xx:xx 阶段复习完成」再让它自己过期（用户要提醒与完成联动）。
        立刻入库等于他做完了却什么反馈都没有。
        """
        store = FakeStore([{"id": "n1", "kind": "review-new",
                            "title": "还没开始学的新卡已有 25 张"}])
        run(store, new=rn.REVIEW_NEW_SPEAK_THRESHOLD - 1)
        self.assertEqual([one[0] for one in store.updated], ["n1"])
        self.assertIn(rn.COMPLETED_MARK, store.updated[0][1]["title"])
        self.assertEqual(store.resolved, [], "要看得见完成，不是当场消失")

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

    def test_asleep_defers_even_inside_the_window(self):
        """钟点只是粗筛:窗口内但几小时没动静(像在睡),也该蛰伏到起床点而不是当场出声。"""
        store = FakeStore()
        run(store, new=25, hour=14, awake=False)
        made = [c for c in store.created if c["kind"] == "review-new"]
        self.assertEqual(len(made), 1, "仍要留痕")
        self.assertEqual(made[0]["activate_at_ms"], WAKE_MS)

    def test_awake_signal_falls_back_to_awake_when_unreadable(self):
        """读不到活动就当醒着:守卫是为了别在睡觉时出声,不是制造"提醒神秘消失"。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            awake, reason = rn.looks_awake(Path(tmp))
        self.assertTrue(awake)
        self.assertIn("按醒着处理", reason)


class BlockedNewCardTests(unittest.TestCase):
    """评不了分的新卡不能被当成"去学吧"（2026-09-09 实测撞出来的）。

    那天的实况：10 张新卡挂了 18 天，全是 `_pcExportStatus=failed` —— 这台
    电脑上 Anki 根本没在跑，AnkiConnect 8765 拒连。而 Reader **没有本地排期**，
    第一次评分必须有真实 Anki 卡号，所以那 10 张一张都按不下去。

    提醒当时说的是「还没开始学的新卡已有 10 张，先做 10 张就好，大约 10 分钟」。
    催人去做一件按不下去的事比不催更糟：他打开卡，看见一个没有按钮也没有说明
    的空框，然后不知道该怎么办。
    """

    def test_all_blocked_does_not_ask_him_to_study(self):
        store = FakeStore()
        run(store, new=10, blocked=10)
        self.assertEqual([c for c in store.created if c["kind"] == "review-new"], [])

    def test_all_blocked_raises_a_fault_instead(self):
        store = FakeStore()
        run(store, new=10, blocked=10)
        made = [c for c in store.created if c["kind"] == "review-blocked"]
        self.assertEqual(len(made), 1)
        self.assertIn("10", made[0]["title"])
        # 故障通知要说清**怎么修**，不能只报"不行"。
        self.assertIn("Anki", made[0]["body"])
        self.assertEqual(made[0]["audience"], "user")

    def test_partly_blocked_counts_only_the_gradable_ones(self):
        store = FakeStore()
        run(store, new=14, blocked=4)
        made = [c for c in store.created if c["kind"] == "review-new"]
        self.assertEqual(len(made), 1)
        # 标题里的数字必须是**能做的**那些，否则他打开会少 4 张。
        self.assertIn("10", made[0]["title"])
        self.assertEqual(
            len([c for c in store.created if c["kind"] == "review-blocked"]), 1)

    def test_below_threshold_after_excluding_blocked(self):
        store = FakeStore()
        run(store, new=12, blocked=4)
        self.assertEqual([c for c in store.created if c["kind"] == "review-new"], [])

    def test_fault_clears_itself_once_export_works(self):
        store = FakeStore(open_items=[{"id": "b1", "kind": "review-blocked"}])
        run(store, new=10, blocked=0)
        self.assertIn("b1", [one[0] for one in store.resolved])

    def test_resolution_note_does_not_claim_he_studied_when_blocked(self):
        # 消除理由是以后查这件事的唯一线索 —— 写死"他开始学了"会在故障时
        # 留下一句反过来的记录。
        store = FakeStore(open_items=[{"id": "n1", "kind": "review-new"}])
        run(store, new=10, blocked=10)
        notes = [one[1] for one in store.resolved if one[0] == "n1"]
        self.assertEqual(len(notes), 1)
        self.assertNotIn("他开始学了", notes[0])
        self.assertIn("导出", notes[0])


class ReviewCountsTests(unittest.TestCase):
    """四个数由同一次遍历产出，构造上就不可能自相矛盾。"""

    def test_gradable_plus_blocked_always_equals_new(self):
        import json
        import tempfile
        root = Path(tempfile.mkdtemp(prefix="counts-"))
        book = root / "replication-data" / "b1"
        book.mkdir(parents=True)
        cards = [
            {"_st": "learn"},                                    # 可评
            {"_st": "learn", "_ratingUnavailable": True,
             "_ratingUnavailableReason": "not-exported"},        # 卡住
            {"_st": "learn", "_ratingUnavailable": True,
             "_ratingUnavailableReason": "external"},            # 在 Anki 里，能评
            {"_next": 1, "_st": "review"},                       # 到期
            {"_st": "learn", "_removed": True},                  # 不算
        ]
        (book / "document-notes.json").write_text(json.dumps(
            {"items": {"i1": {"card": {"cards": cards}}}}), encoding="utf-8")
        counts = rn.review_counts(root)
        self.assertEqual(counts["new"], 3)
        self.assertEqual(counts["newBlocked"], 1)
        self.assertEqual(counts["newGradable"], 2)
        self.assertEqual(counts["newGradable"] + counts["newBlocked"],
                         counts["new"])
        self.assertEqual(counts["due"], 1)
        # 旧签名不能变形：还有别的调用方在用它
        self.assertEqual(rn.count_due_cards(root), (1, 3))

    def test_missing_data_directory_is_all_zero(self):
        import tempfile
        counts = rn.review_counts(Path(tempfile.mkdtemp(prefix="empty-")))
        self.assertEqual(counts, {"due": 0, "new": 0,
                                  "newGradable": 0, "newBlocked": 0})


class CompletionWriteBackTests(unittest.TestCase):
    """完成回写到**最初那一条**（2026-09-09 用户：「完成后就直接更新慢板内容为
    xx:xx 阶段复习完成」）。

    为什么改原条目而不是另开一条：提醒和完成是同一件事的两端。分成两条会在
    板上留下一条永远得不到结果的催促，而那正是他要联动起来的东西。
    """

    def test_completion_updates_the_original_item(self):
        store = FakeStore(open_items=[{"id": "n1", "kind": "review-new",
                                       "title": "还没开始学的新卡已有 10 张"}])
        run(store, new=0, blocked=0)
        self.assertEqual(len(store.updated), 1)
        item_id, fields = store.updated[0]
        self.assertEqual(item_id, "n1")
        self.assertIn(rn.COMPLETED_MARK, fields["title"])
        # 时间要在标题里 —— 用户点名要 xx:xx
        self.assertRegex(fields["title"], r"^\d{2}:\d{2} ")
        # 让它自己过期而不是当场入库：板上要看得见这句完成
        self.assertGreater(fields["expires_at_ms"], 0)
        self.assertEqual(store.resolved, [])

    def test_partial_completion_says_what_is_left(self):
        store = FakeStore(open_items=[{"id": "n1", "kind": "review-new",
                                       "title": "还没开始学的新卡已有 10 张"}])
        run(store, new=3, blocked=0)
        _id, fields = store.updated[0]
        self.assertIn("3", fields["body"])

    def test_completion_is_written_once_not_every_round(self):
        # 每轮重写会让时间戳每 15 分钟往前跳一次，看着像刚做完。
        store = FakeStore(open_items=[{"id": "n1", "kind": "review-new",
                                       "title": "02:30 " + rn.COMPLETED_MARK}])
        run(store, new=0, blocked=0)
        self.assertEqual(store.updated, [])
        self.assertEqual(store.resolved, [], "写过完成的要等它自己过期")

    def test_blocked_is_not_completion(self):
        # 可评分的降到 0 也可能是卡全被导出堵住了 —— 那不是"做完了"。
        store = FakeStore(open_items=[{"id": "n1", "kind": "review-new",
                                       "title": "还没开始学的新卡已有 10 张"}])
        run(store, new=10, blocked=10)
        self.assertEqual(store.updated, [])
        self.assertEqual([one[0] for one in store.resolved], ["n1"])
