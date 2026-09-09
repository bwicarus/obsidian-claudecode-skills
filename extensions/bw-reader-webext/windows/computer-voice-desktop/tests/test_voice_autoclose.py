"""语音智能关闭的判据与时序。

这一带最贵的两个错分别是：
  · 把"不知道"当成"可以关" —— 会在人正说话时把通话掐了；
  · 把"推送送出去了"当成"已经关掉了" —— 于是继续按时间计费而没人知道。
下面每条测试都盯着其中之一。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "voice_autoclose_test_subject", RUNTIME / "voice_autoclose.py"
)
assert SPEC and SPEC.loader
VAC = importlib.util.module_from_spec(SPEC)
sys.modules["voice_autoclose_test_subject"] = VAC
SPEC.loader.exec_module(VAC)


def known(value):
    return {"known": True, "value": value, "why": ""}


def unknown(why="不知道"):
    return {"known": False, "value": None, "why": why}


def prefs(**overrides):
    merged = dict(VAC.PREFERENCE_DEFAULTS)
    merged["voiceAutoClose"] = True
    merged.update(overrides)
    return merged


class DecisionTests(unittest.TestCase):
    def setUp(self):
        self.state = VAC.AutoCloseState()
        self.now = 1_800_000_000_000

    def test_master_switch_off_never_closes(self):
        """总开关关着 = 持续开启模式，什么条件都不该动手。"""
        signals = {"idle_minutes": known(999), "awake": known(False)}
        settings = prefs()
        settings["voiceAutoClose"] = False
        self.assertIsNone(
            VAC.evaluate(self.state, signals, settings, self.now)
        )

    def test_unknown_signals_never_close(self):
        """信号说"不知道"时不动手 —— 关闭会打断人说话。"""
        signals = {name: unknown() for name in
                   ("idle_minutes", "awake", "place",
                    "readerpc_running", "reading_title")}
        self.assertIsNone(
            VAC.evaluate(self.state, signals, prefs(), self.now)
        )

    def test_idle_threshold_is_a_threshold(self):
        settings = prefs(voiceAutoCloseIdleMinutes=20)
        below = {"idle_minutes": known(19)}
        self.assertIsNone(VAC.evaluate(self.state, below, settings, self.now))
        at = {"idle_minutes": known(20)}
        reason = VAC.evaluate(self.state, at, settings, self.now)
        self.assertIn("闲置 20 分钟", reason or "")

    def test_sleep_closes_only_when_known_asleep(self):
        self.assertIsNone(
            VAC.evaluate(self.state, {"awake": unknown()}, prefs(), self.now)
        )
        self.assertIsNone(
            VAC.evaluate(self.state, {"awake": known(True)}, prefs(), self.now)
        )
        self.assertEqual(
            VAC.evaluate(self.state, {"awake": known(False)}, prefs(),
                         self.now),
            "已经睡着",
        )

    def test_place_condition_needs_a_remembered_start(self):
        """判的是变化，不是某个值：从家到公司也算离开。"""
        signals = {"place": known("home")}
        # 还没记住起点 → 不成立（不能拿当前值自证）
        self.assertIsNone(VAC.evaluate(self.state, signals, prefs(), self.now))
        self.state.observe(signals, self.now, in_call=True)
        self.assertEqual(self.state.place_at_start, "home")
        # 同一地点不成立
        self.assertIsNone(VAC.evaluate(self.state, signals, prefs(), self.now))
        # 换了地点才成立 —— 注意 work 也算，不只是"外出"
        moved = {"place": known("work")}
        reason = VAC.evaluate(self.state, moved, prefs(), self.now)
        self.assertIn("home", reason or "")
        self.assertIn("work", reason or "")

    def test_reader_closed_closes_immediately(self):
        signals = {"readerpc_running": known(False)}
        self.assertEqual(
            VAC.evaluate(self.state, signals, prefs(), self.now),
            "阅读器已关闭",
        )

    def test_not_reading_must_actually_last(self):
        """翻页/切窗口会让标题空一小会儿，拿瞬时值判会掐掉正在读的人。"""
        settings = prefs(voiceAutoCloseIdleMinutes=20)
        empty = {"readerpc_running": known(True), "reading_title": known("")}
        self.state.observe(empty, self.now, in_call=True)
        # 刚空下来 → 不成立
        self.assertIsNone(VAC.evaluate(self.state, empty, settings, self.now))
        # 空了 19 分钟 → 仍不成立
        self.assertIsNone(
            VAC.evaluate(self.state, empty, settings,
                         self.now + 19 * 60_000)
        )
        # 20 分钟 → 成立
        reason = VAC.evaluate(self.state, empty, settings,
                              self.now + 20 * 60_000)
        self.assertIn("没在读", reason or "")
        # 中途又开始读 → 计时清零
        reading = {"readerpc_running": known(True),
                   "reading_title": known("料理师part2")}
        self.state.observe(reading, self.now + 21 * 60_000, in_call=True)
        self.assertIsNone(self.state.not_reading_since_ms)

    def test_state_forgets_the_call_after_it_ends(self):
        signals = {"place": known("home"), "reading_title": known("")}
        self.state.observe(signals, self.now, in_call=True)
        self.assertTrue(self.state.call_seen)
        self.state.observe(signals, self.now + 1000, in_call=False)
        self.assertFalse(self.state.call_seen)
        self.assertIsNone(self.state.place_at_start)

    def test_disabled_condition_is_skipped(self):
        settings = prefs(voiceAutoCloseOnSleep=False)
        self.assertIsNone(
            VAC.evaluate(self.state, {"awake": known(False)}, settings,
                         self.now)
        )


class LedgerTests(unittest.TestCase):
    def test_active_rule_matches_the_csharp_copy(self):
        """这是同一条判据的第二份副本，两边必须逐字同义。"""
        self.assertTrue(VAC._ledger_active(10, 0))     # 开始了，没结束
        self.assertTrue(VAC._ledger_active(20, 10))    # 又开了一通
        self.assertFalse(VAC._ledger_active(10, 20))   # 已结束
        self.assertFalse(VAC._ledger_active(0, 0))     # 从没用过
        source = (RUNTIME.parents[0] / "ComputerVoiceAudio"
                  / "CodexVoiceActivity.cs").read_text(encoding="utf-8")
        self.assertIn("LastUsedTimeStart > 0", source)
        self.assertIn("LastUsedTimeStop == 0", source)

    def test_unreadable_ledger_is_unknown_not_inactive(self):
        """把"不知道"折成"没在通话"会让兜底在真通话时按下 F24。"""

        def boom(*_args, **_kwargs):
            raise OSError("no such key")

        ledger = VAC.read_ledger(open_key=boom)
        self.assertFalse(ledger["known"])
        self.assertIsNone(ledger["active"])
        self.assertIn("读不到", ledger["why"])


class CloseSequenceTests(unittest.TestCase):
    def setUp(self):
        self.slept = []
        self.clock_value = [0.0]
        self.posts = []

    def _sleep(self, seconds):
        self.slept.append(seconds)
        self.clock_value[0] += seconds

    def _clock(self):
        return self.clock_value[0]

    def _poster(self, accept=True, pressed=True):
        def post(endpoint, body):
            self.posts.append(body)
            if body.get("hangUpVoiceFallback"):
                return 200, {"ok": True, "pressed": pressed,
                             "skipped": "" if pressed else "台账显示已经不在通话"}
            return 200, {"ok": True, "hangUpRequested": accept}
        return post

    def _ledger(self, closes_after: int):
        calls = [0]

        def read():
            calls[0] += 1
            done = calls[0] >= closes_after
            return {"known": True, "active": not done,
                    "start": 2, "stop": 1, "why": ""}
        return read

    def test_push_success_reports_push_not_shortcut(self):
        result = VAC.close_voice(
            endpoint="http://x", thread_id="t", reason="闲置 20 分钟",
            ledger_reader=self._ledger(closes_after=2),
            poster=self._poster(), sleeper=self._sleep, clock=self._clock,
        )
        self.assertTrue(result["closed"])
        self.assertEqual(result["by"], "push")
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(len(self.posts), 1)
        self.assertNotIn("hangUpVoiceFallback", self.posts[0])

    def test_failure_is_not_declared_before_the_grace_period(self):
        """用户明确要求：不能太快说没成，至少等两分钟。"""
        VAC.close_voice(
            endpoint="http://x", thread_id="t", reason="r",
            ledger_reader=self._ledger(closes_after=10_000),
            poster=self._poster(), sleeper=self._sleep, clock=self._clock,
            grace_seconds=120.0,
        )
        # 两次推送各等满 120 秒
        self.assertGreaterEqual(sum(self.slept), 240.0)

    def test_two_failed_pushes_then_shortcut(self):
        result = VAC.close_voice(
            endpoint="http://x", thread_id="t", reason="r",
            ledger_reader=self._ledger(closes_after=10_000),
            poster=self._poster(), sleeper=self._sleep, clock=self._clock,
            grace_seconds=10.0,
        )
        kinds = [("fallback" if p.get("hangUpVoiceFallback") else "push")
                 for p in self.posts]
        self.assertEqual(kinds, ["push", "push", "fallback"])
        self.assertFalse(result["closed"])   # 台账始终说还在通话

    def test_shortcut_can_be_forbidden(self):
        VAC.close_voice(
            endpoint="http://x", thread_id="t", reason="r",
            ledger_reader=self._ledger(closes_after=10_000),
            poster=self._poster(), sleeper=self._sleep, clock=self._clock,
            grace_seconds=10.0, allow_shortcut_fallback=False,
        )
        self.assertNotIn(
            True, [p.get("hangUpVoiceFallback") for p in self.posts]
        )

    def test_bridge_skipping_the_shortcut_is_not_success(self):
        """桥自己复核台账后跳过 F24 时，绝不能报成已关闭。"""
        result = VAC.close_voice(
            endpoint="http://x", thread_id="t", reason="r",
            ledger_reader=self._ledger(closes_after=10_000),
            poster=self._poster(pressed=False), sleeper=self._sleep,
            clock=self._clock, grace_seconds=10.0,
        )
        self.assertFalse(result["closed"])
        self.assertIsNone(result["by"])

    def test_request_id_changes_per_attempt(self):
        VAC.close_voice(
            endpoint="http://x", thread_id="t", reason="r",
            ledger_reader=self._ledger(closes_after=10_000),
            poster=self._poster(), sleeper=self._sleep, clock=self._clock,
            grace_seconds=10.0,
        )
        ids = [p["requestId"] for p in self.posts if "requestId" in p]
        self.assertEqual(len(ids), len(set(ids)), "同一编号重复会看不出是第几次")


class ThreadLookupTests(unittest.TestCase):
    def test_uses_the_in_call_thread_not_the_push_binding(self):
        """绑定那条是**提示板**推送的目标，通常不是通话那条。"""
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "voice-history-sidebar-sync-state.json").write_text(
                json.dumps({"lastGood": {"threadId": "in-call-thread"}}),
                encoding="utf-8")
            (root / "codex-push-binding.json").write_text(
                json.dumps({"threadId": "board-push-thread"}),
                encoding="utf-8")
            self.assertEqual(VAC.in_call_thread_id(root), "in-call-thread")

    def test_missing_state_is_empty_not_a_crash(self):
        with tempfile.TemporaryDirectory() as name:
            self.assertEqual(VAC.in_call_thread_id(Path(name)), "")


if __name__ == "__main__":
    unittest.main()
