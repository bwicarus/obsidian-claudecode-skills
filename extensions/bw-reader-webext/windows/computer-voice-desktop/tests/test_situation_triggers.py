# -*- coding: utf-8 -*-
"""situation_triggers 的测试。

这个原语的价值全在"别人能不能信它"，所以测的都是**信任点**：

① **非法规则必须报错**，不能静默存下一条永远不响的规则 —— 那会让 AI
   以为绑好了，而用户什么也收不到，链路上没有一处会喊。
② **上升沿只响一次**，且注册时已成立的不立刻响。
③ **建通知失败不吃掉上升沿**。实测撞出来的：`create()` 要求必须给终止条件，
   我第一版没给，于是"响了一次、其实什么都没发生"，而 lastMatch 已被置真 ——
   下一轮不会重试。这种失败最贵，因为它看起来像成功。
④ **信号不可用一律不响**。读不到位置不等于"不在家"。
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import replication_notifications  # noqa: E402
import situation_triggers  # noqa: E402


class TriggerTests(unittest.TestCase):
    def setUp(self) -> None:
        base = Path(tempfile.mkdtemp(prefix="trg-"))
        self.root = base / "root"
        self.runtime = base / "runtime"
        self.root.mkdir()
        self.runtime.mkdir()
        self.set_place("out")

    def set_place(self, state: str) -> None:
        (self.runtime / "current-place.json").write_text(json.dumps({
            "state": state, "alias": state,
            "observedAtUtcMs": int(time.time() * 1000),
        }), encoding="utf-8")

    def drop_place(self) -> None:
        (self.runtime / "current-place.json").unlink()

    def add(self, **kwargs):
        payload = {
            "name": kwargs.pop("name", "规则"),
            "when": kwargs.pop("when", {"place": "home"}),
            "then": kwargs.pop("then", {"title": "到家了"}),
        }
        return situation_triggers.add(
            self.root, runtime=self.runtime, **payload, **kwargs)

    def evaluate(self):
        return situation_triggers.evaluate(self.root, self.runtime)

    def notifications(self) -> list[dict]:
        path = self.root / replication_notifications.OPEN_FILE_NAME
        if not path.is_file():
            return []
        return json.loads(path.read_text(encoding="utf-8"))["items"]

    def trigger_record(self, name: str = "规则") -> dict:
        table = situation_triggers.load(self.root)
        return next(one for one in table["triggers"] if one["name"] == name)

    # ── ① 非法规则一律报错
    def test_misspelled_signal_is_refused(self) -> None:
        with self.assertRaises(situation_triggers.TriggerError) as caught:
            self.add(when={"plaec": "home"})
        # 报错要**带上可用的名字**，否则 AI 只能再猜一次。
        self.assertIn("place", str(caught.exception))

    def test_bad_comparator_is_refused(self) -> None:
        with self.assertRaises(situation_triggers.TriggerError):
            self.add(when={"place": {"like": "home"}})

    def test_empty_when_is_refused(self) -> None:
        with self.assertRaises(situation_triggers.TriggerError):
            self.add(when={})

    def test_missing_title_is_refused(self) -> None:
        with self.assertRaises(situation_triggers.TriggerError):
            self.add(then={"body": "只有正文"})

    def test_bad_deliver_is_refused(self) -> None:
        with self.assertRaises(situation_triggers.TriggerError):
            self.add(then={"title": "x", "deliver": "喊一声"})

    def test_duplicate_name_is_refused(self) -> None:
        self.add(name="同名")
        with self.assertRaises(situation_triggers.TriggerError):
            self.add(name="同名")

    def test_too_many_conditions_refused(self) -> None:
        when = {name: True for name in list(
            __import__("situation_signals").SIGNALS)[
                :situation_triggers.MAX_CONDITIONS + 1]}
        with self.assertRaises(situation_triggers.TriggerError):
            self.add(when=when)

    def test_table_cap_is_enforced_loudly(self) -> None:
        for index in range(situation_triggers.MAX_TRIGGERS):
            self.add(name="规则%d" % index)
        with self.assertRaises(situation_triggers.TriggerError) as caught:
            self.add(name="再来一条")
        self.assertIn("已满", str(caught.exception))

    # ── ② 上升沿语义
    def test_registering_while_already_true_does_not_fire(self) -> None:
        self.set_place("home")
        result = self.add()
        self.assertTrue(result["baselineMatch"])
        self.assertEqual(self.evaluate()["fired"], [])
        self.assertEqual(self.notifications(), [])

    def test_rising_edge_fires_once_and_creates_notification(self) -> None:
        self.add(then={
            "title": "到家了", "body": "顺便说一句",
            "aiAction": "问他要不要做新卡", "deliver": "auto"})
        self.assertEqual(self.evaluate()["fired"], [])   # 还没到家
        self.set_place("home")
        fired = self.evaluate()["fired"]
        self.assertEqual([one["name"] for one in fired], ["规则"])
        items = self.notifications()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "到家了")
        self.assertEqual(items[0]["kind"], "situation-trigger")
        self.assertEqual(items[0]["audience"], "user")
        # aiAction 折进正文（不新开字段：字段表在导出/渲染各有副本）
        self.assertIn("问他要不要做新卡", items[0]["body"])
        self.assertIn("顺便说一句", items[0]["body"])
        # 触发出来的通知必须有寿命，否则会永远挂着
        self.assertTrue(items[0].get("expiresAtUtcMs"))
        # 条件还成立，但上升沿过去了 —— 不能再响
        self.assertEqual(self.evaluate()["fired"], [])
        self.assertEqual(len(self.notifications()), 1)

    def test_falling_then_rising_fires_again(self) -> None:
        self.add(recur="always")
        self.set_place("home")
        self.assertEqual(len(self.evaluate()["fired"]), 1)
        self.set_place("out")
        self.assertEqual(self.evaluate()["fired"], [])
        self.set_place("home")
        self.assertEqual(len(self.evaluate()["fired"]), 1)
        self.assertEqual(self.trigger_record()["fireCount"], 2)

    def test_daily_fires_only_once_per_day(self) -> None:
        self.add(recur="daily")
        self.set_place("home")
        self.assertEqual(len(self.evaluate()["fired"]), 1)
        self.set_place("out")
        self.evaluate()
        self.set_place("home")
        # 同一天第二个上升沿：不响，但记录还留着（明天还能响）
        self.assertEqual(self.evaluate()["fired"], [])
        self.assertEqual(self.trigger_record()["fireCount"], 1)

    def test_once_is_deleted_after_firing(self) -> None:
        self.add(recur="once")
        self.set_place("home")
        self.assertEqual(len(self.evaluate()["fired"]), 1)
        self.assertEqual(situation_triggers.load(self.root)["triggers"], [])

    def test_expired_rule_is_swept(self) -> None:
        self.add(expires_hours=0.5)
        table = situation_triggers.load(self.root)
        table["triggers"][0]["expiresAtUtcMs"] = int(time.time() * 1000) - 1000
        (self.root / situation_triggers.TRIGGERS_FILE_NAME).write_text(
            json.dumps(table), encoding="utf-8")
        self.set_place("home")
        result = self.evaluate()
        self.assertEqual(result["expired"], 1)
        self.assertEqual(result["fired"], [])
        self.assertEqual(situation_triggers.load(self.root)["triggers"], [])

    # ── ③ 建通知失败不吃掉上升沿
    def test_failed_fire_keeps_the_edge_for_a_retry(self) -> None:
        self.add()
        original = replication_notifications.NotificationStore.create

        def explode(self, **kwargs):
            raise replication_notifications.NotificationError("故意建不出来")

        replication_notifications.NotificationStore.create = explode
        try:
            self.set_place("home")
            self.assertEqual(self.evaluate()["fired"], [])
            record = self.trigger_record()
            # 关键：lastMatch 要**退回假**，否则下一轮不会重试，
            # 表现就是"响过一次但其实什么都没发生"。
            self.assertFalse(record["lastMatch"])
            self.assertIn("故意建不出来", record.get("lastError", ""))
            self.assertEqual(record["fireCount"], 0)
        finally:
            replication_notifications.NotificationStore.create = original
        # 修好之后下一轮真的补上了
        fired = self.evaluate()["fired"]
        self.assertEqual(len(fired), 1)
        self.assertNotIn("lastError", self.trigger_record())

    # ── ④ 信号不可用一律不响
    def test_unavailable_signal_never_fires(self) -> None:
        self.add(when={"place": {"not": "work"}})
        self.drop_place()
        # 「不在公司」在读不到位置时**不成立** —— 否则一台没定位的设备
        # 会让所有"不在某地"的规则集体误触发。
        self.assertEqual(self.evaluate()["fired"], [])
        value = situation_triggers.explain(
            self.root, key="规则", runtime=self.runtime)
        self.assertFalse(value["match"])
        self.assertFalse(value["details"][0]["known"])

    # ── 可诊断性：一条不响的规则必须说得出为什么
    def test_explain_lists_every_condition(self) -> None:
        self.add(when={"place": "home", "local_hour": {"gte": 0}})
        value = situation_triggers.explain(
            self.root, key="规则", runtime=self.runtime)
        signals = {one["signal"]: one for one in value["details"]}
        self.assertFalse(signals["place"]["ok"])
        self.assertTrue(signals["local_hour"]["ok"])
        self.assertIn("规则", situation_triggers.render_explain(value))

    def test_evaluated_timestamp_is_recorded_even_when_empty(self) -> None:
        # "引擎到底有没有在跑"必须永远答得出来。
        self.assertIsNone(
            situation_triggers.load(self.root)["lastEvaluatedAtUtcMs"])
        self.evaluate()
        self.assertIsNotNone(
            situation_triggers.load(self.root)["lastEvaluatedAtUtcMs"])

    def test_remove_by_name_and_by_id(self) -> None:
        record = self.add(name="按名字删")["trigger"]
        self.assertIsNotNone(
            situation_triggers.remove(self.root, key="按名字删"))
        self.assertIsNone(situation_triggers.remove(self.root, key="不存在"))
        record = self.add(name="按ID删")["trigger"]
        self.assertIsNotNone(
            situation_triggers.remove(self.root, key=record["id"]))

    def test_broken_table_does_not_erase_real_data(self) -> None:
        self.add(name="要保住")
        path = self.root / situation_triggers.TRIGGERS_FILE_NAME
        backup = path.read_text(encoding="utf-8")
        path.write_text("{ 这不是 JSON", encoding="utf-8")
        # 读失败返回空表，但**不写回** —— 别让一次读失败清空真数据。
        self.assertEqual(situation_triggers.load(self.root)["triggers"], [])
        self.assertEqual(path.read_text(encoding="utf-8"), "{ 这不是 JSON")
        path.write_text(backup, encoding="utf-8")
        self.assertEqual(
            len(situation_triggers.load(self.root)["triggers"]), 1)


if __name__ == "__main__":
    unittest.main()
