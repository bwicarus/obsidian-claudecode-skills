# -*- coding: utf-8 -*-
"""situation_signals 的测试。

守这个模块的三条命门：

① **「不知道」和「否」分开**。空世界里每个信号都必须说"不知道"，不能变成
   "不在家 / 没戴耳机 / 零张卡"。`review_new` 这一条是实测撞出来的：
   `count_due_cards` 在数据目录缺失时返回 0，照抄就等于把"看不见数据"
   说成"已经清空了"，而一条 `{"review_new": {"lte": 0}}` 的规则会因此
   触发一条假通知。
② **`known=False` 一律不成立**。整套触发机制最容易被绕过的一条，
   判定只写在 `matches` 一处，所以这里逐个比较器都测。
③ **词汇表封闭**。表外的名字要得到一个明确的"没有这个信号"，
   而不是静默当成永不成立的条件。
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import situation_signals  # noqa: E402


class SignalTests(unittest.TestCase):
    def setUp(self) -> None:
        base = Path(tempfile.mkdtemp(prefix="sig-"))
        self.root = base / "root"
        self.runtime = base / "runtime"
        self.root.mkdir()
        self.runtime.mkdir()

    def _read(self, *names: str) -> dict:
        return situation_signals.read_all(
            self.root, self.runtime, list(names) or None)["signals"]

    # ── ① 空世界：每一项都说不知道
    def test_empty_world_says_unknown_not_no(self) -> None:
        signals = self._read()
        for name in ("place", "place_alias", "audio_route", "headphones",
                     "app_foreground", "voice_linked", "reading_title",
                     "review_due", "review_new"):
            with self.subTest(signal=name):
                self.assertFalse(
                    signals[name]["known"],
                    "%s 在空世界里必须是「不知道」，不能折成一个方向的结论" % name)
                self.assertIsNone(signals[name]["value"])
                self.assertTrue(
                    signals[name].get("why"),
                    "%s 说不知道时必须给出原因" % name)

    def test_review_counts_unknown_without_data_directory(self) -> None:
        # 实测撞出来的那条：数据目录缺失 ≠ 零张卡。
        self.assertFalse(self._read("review_new")["review_new"]["known"])
        (self.root / "replication-data").mkdir()
        signal = self._read("review_new")["review_new"]
        self.assertTrue(signal["known"])
        self.assertEqual(signal["value"], 0)

    def test_place_missing_is_not_away_from_home(self) -> None:
        signal = self._read("place")["place"]
        self.assertFalse(signal["known"])
        self.assertNotEqual(signal["value"], "out")

    # ── 有数据时读得对，并带上年龄
    def test_place_reports_state_and_age(self) -> None:
        observed = int(time.time() * 1000) - 6 * 60_000
        (self.runtime / "current-place.json").write_text(json.dumps({
            "state": "home", "alias": "家", "observedAtUtcMs": observed,
        }), encoding="utf-8")
        signals = self._read("place", "place_alias")
        self.assertEqual(signals["place"]["value"], "home")
        self.assertEqual(signals["place_alias"]["value"], "家")
        self.assertAlmostEqual(signals["place"]["ageMinutes"], 6.0, delta=1.0)

    def _write_presence(self, route: str, age_minutes: float = 0.0) -> None:
        (self.root / situation_signals.PRESENCE_FILE_NAME).write_text(
            json.dumps({
                "audioRoute": route, "foreground": True,
                "atMs": int(time.time() * 1000
                            - age_minutes * 60_000),
            }), encoding="utf-8")

    def test_headphones_folds_from_route(self) -> None:
        for route, expected in (("speaker", False), ("receiver", False),
                                ("headphones", True), ("bluetooth", True),
                                ("airplay", True)):
            with self.subTest(route=route):
                self._write_presence(route)
                signal = self._read("headphones")["headphones"]
                self.assertTrue(signal["known"])
                self.assertIs(signal["value"], expected)

    def test_stale_presence_is_unknown_not_speaker(self) -> None:
        # 耳机状态是**现状**：旧值毫无意义，更不能当成"没戴"。
        self._write_presence(
            "headphones",
            situation_signals.PRESENCE_FRESH_MINUTES + 5)
        signal = self._read("headphones")["headphones"]
        self.assertFalse(signal["known"])
        self.assertIn("没更新", signal["why"])

    def test_stale_heartbeat_makes_status_signals_unknown(self) -> None:
        (self.root / "readerpc-server.status.json").write_text(json.dumps({
            "updatedAtEpochMs": int(time.time() * 1000) - 40 * 60_000,
            "voice": {"readerConnected": True},
            "readerContext": {"title": "某本书"},
        }), encoding="utf-8")
        signals = self._read("voice_linked", "reading_title")
        # 心跳陈旧时文件里全是旧话 —— 不能让"语音已连"冒充现状。
        self.assertFalse(signals["voice_linked"]["known"])
        self.assertFalse(signals["reading_title"]["known"])

    # ── ② matches：不可用一律不成立
    def test_unknown_never_matches(self) -> None:
        signal = situation_signals.unknown("没有数据")
        for expected in ("home", True, False, 0, {"not": "home"},
                         {"in": ["home"]}, {"gte": 0}, {"lte": 99}):
            with self.subTest(expected=expected):
                ok, why = situation_signals.matches(signal, expected)
                self.assertFalse(ok, "信号不可用时 %r 也不能成立" % (expected,))
                self.assertIn("不可用", why)

    def test_comparators(self) -> None:
        cases = [
            (situation_signals.known("home"), "home", True),
            (situation_signals.known("work"), "home", False),
            (situation_signals.known("work"), {"not": "home"}, True),
            (situation_signals.known("home"), {"in": ["home", "work"]}, True),
            (situation_signals.known("out"), {"in": ["home"]}, False),
            (situation_signals.known(12), {"gte": 10}, True),
            (situation_signals.known(9), {"gte": 10}, False),
            (situation_signals.known(9), {"lte": 10}, True),
            (situation_signals.known(True), True, True),
        ]
        for signal, expected, want in cases:
            with self.subTest(value=signal["value"], expected=expected):
                ok, _why = situation_signals.matches(signal, expected)
                self.assertIs(ok, want)

    def test_bad_comparator_is_refused_not_silently_true(self) -> None:
        signal = situation_signals.known(5)
        for expected in ({"like": 5}, {"gte": 1, "lte": 9}, {"gte": "五"}):
            with self.subTest(expected=expected):
                ok, why = situation_signals.matches(signal, expected)
                self.assertFalse(ok)
                self.assertTrue(why)

    def test_boolean_is_not_a_number_for_size_comparison(self) -> None:
        # Python 里 True >= 1 成立，但"戴着耳机 >= 1"是没有意义的比较。
        ok, _why = situation_signals.matches(
            situation_signals.known(True), {"gte": 1})
        self.assertFalse(ok)

    # ── ③ 词汇表封闭
    def test_unknown_signal_name_is_reported(self) -> None:
        signals = self._read("plaec")
        self.assertFalse(signals["plaec"]["known"])
        self.assertIn("封闭", signals["plaec"]["why"])

    def test_vocab_lists_every_signal(self) -> None:
        text = situation_signals.vocab_text()
        for name in situation_signals.SIGNALS:
            self.assertIn(name, text)
        # 值域写反比不写更糟，所以每条都必须有。
        for name, spec in situation_signals.SIGNALS.items():
            with self.subTest(signal=name):
                self.assertTrue(spec["summary"])
                self.assertTrue(spec["values"])

    def test_read_all_survives_a_broken_signal(self) -> None:
        def explode(root, runtime, now_ms):
            raise RuntimeError("故意炸")

        situation_signals.SIGNALS["place"]["read"], original = (
            explode, situation_signals.SIGNALS["place"]["read"])
        try:
            signals = self._read("place", "local_hour")
        finally:
            situation_signals.SIGNALS["place"]["read"] = original
        # 一个信号炸了不连坐其它，而且**要说出为什么**。
        self.assertFalse(signals["place"]["known"])
        self.assertIn("故意炸", signals["place"]["why"])
        self.assertTrue(signals["local_hour"]["known"])


if __name__ == "__main__":
    unittest.main()


class ReviewingSignalTests(unittest.TestCase):
    """「他开始复习了」这个信号（2026-09-09 用户要的第一件事）。

    > 设计一个连通 app 的状态监控，在后方等待我主动点击那个复习模式后返回开始
    > 复习……对他来说这其实就只是一次工具调用

    实现上不需要新链路：复习状态早就从 `RC.review.snapshotState()` 投影到
    上下文快照的 `activeReading.review` 了。这里只是把它读成信号，
    之后 AI 注册一条 `--when '{"reviewing": true}'` 的规则就等于"那次工具调用"。
    """

    def setUp(self) -> None:
        base = Path(tempfile.mkdtemp(prefix="rev-"))
        self.root = base / "root"
        self.runtime = base / "runtime"
        self.root.mkdir()
        self.runtime.mkdir()

    def snapshot(self, **fields) -> None:
        value = {"contextStatus": "ok"}
        value.update(fields)
        (self.runtime / situation_signals.CONTEXT_SNAPSHOT_FILE_NAME).write_text(
            json.dumps(value), encoding="utf-8")

    def read(self, *names) -> dict:
        return situation_signals.read_all(
            self.root, self.runtime, list(names))["signals"]

    def test_review_mode_is_visible(self):
        self.snapshot(activeReading={"review": {"dueTotal": 12, "index": 4}})
        signals = self.read("reviewing", "review_remaining")
        self.assertIs(signals["reviewing"]["value"], True)
        self.assertEqual(signals["review_remaining"]["value"], 8)

    def test_field_absent_means_not_reviewing_not_unknown(self):
        # 「字段缺席 = 未进入复习模式」是快照链本来的语义，旧构建也一样。
        self.snapshot(activeReading={"title": "某本书"})
        signals = self.read("reviewing", "review_remaining")
        self.assertTrue(signals["reviewing"]["known"])
        self.assertIs(signals["reviewing"]["value"], False)
        self.assertEqual(signals["review_remaining"]["value"], 0)

    def test_reader_disconnected_is_unknown_not_false(self):
        # 「他没在复习」和「我看不见」不是一回事：触发器拿后者当前者，
        # 会在他正复习时判定没在复习。
        self.snapshot(contextStatus="disabled",
                      activeReading={"review": {"dueTotal": 5, "index": 0}})
        signal = self.read("reviewing")["reviewing"]
        self.assertFalse(signal["known"])
        self.assertIn("没连上", signal["why"])

    def test_missing_snapshot_is_unknown(self):
        signal = self.read("reviewing")["reviewing"]
        self.assertFalse(signal["known"])
        self.assertIn("快照", signal["why"])

    def test_finished_queue_reads_zero_remaining(self):
        self.snapshot(activeReading={"review": {"dueTotal": 3, "index": 3}})
        self.assertEqual(self.read("review_remaining")["review_remaining"]["value"], 0)

    def test_compact_line_says_nothing_when_not_reviewing(self):
        # 没在复习是常态，不该占位；正在复习才说。
        self.snapshot(activeReading={"title": "书"})
        payload = situation_signals.read_all(self.root, self.runtime)
        self.assertNotIn("正在复习", situation_signals.render_compact(payload))
        self.snapshot(activeReading={"review": {"dueTotal": 9, "index": 2}})
        payload = situation_signals.read_all(self.root, self.runtime)
        line = situation_signals.render_compact(payload)
        self.assertIn("正在复习", line)
        self.assertIn("还剩7张", line)
