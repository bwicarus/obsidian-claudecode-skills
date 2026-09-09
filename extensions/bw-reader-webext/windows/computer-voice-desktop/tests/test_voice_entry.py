"""语音入口这条链：梯子、一次性尝试、保活意图、放弃报错。

这一带最贵的两个错：
  · 把"不知道"当成"没在通话" —— 那个快捷键是**切换**，按下去会挂断；
  · 悄悄放弃 —— 界面上跟"还在试"长得一模一样，人会一直等。
下面每条都盯着其中之一。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name + "_entry_test_subject", RUNTIME / (name + ".py"))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module          # 供彼此 import
    spec.loader.exec_module(module)
    return module


VAC = _load("voice_autoclose")
KEEPALIVE = _load("voice_keepalive")
LADDER = _load("voice_ladder")
STEP = _load("voice_start_step")
FAILED = _load("voice_start_failed")


def ledger(known=True, active=False):
    return lambda: {"known": known, "active": active if known else None,
                    "start": 1, "stop": 0, "why": "" if known else "读不到"}


class KeepAliveTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_missing_file_is_unknown_not_false(self):
        """读不到折成 False 会让调用方省掉一次本该发生的写入。"""
        self.assertIsNone(KEEPALIVE.read_keep_active(self.runtime))

    def test_round_trip(self):
        KEEPALIVE.write_keep_active(True, self.runtime)
        self.assertIs(KEEPALIVE.read_keep_active(self.runtime), True)
        KEEPALIVE.write_keep_active(False, self.runtime)
        self.assertIs(KEEPALIVE.read_keep_active(self.runtime), False)

    def test_garbage_is_unknown(self):
        KEEPALIVE.keepalive_path(self.runtime).write_text(
            '{"contract":"wrong","enabled":true}', encoding="utf-8")
        self.assertIsNone(KEEPALIVE.read_keep_active(self.runtime))


class StartModeTests(unittest.TestCase):
    """启动方式（2026-09-09 用户：「应该把现在的启动方式作为一个可选项放在里面」）。

    用户实测「启用语音功能怎么还是旧的快捷键启动方式」—— 出处是
    enable_readerpc_voice 里无条件把保活意图置开。这里钉住那条分叉。
    """

    def test_keep_alive_is_the_default(self):
        """多一个新选项不该悄悄改掉别人已经习惯的行为。"""
        self.assertEqual(KEEPALIVE.DEFAULT_START_MODE,
                         KEEPALIVE.START_MODE_KEEP_ALIVE)

    def test_one_shot_does_not_arm_keep_alive(self):
        """这就是「打开功能就自动起一通」与「等我触发」的分界。"""
        self.assertFalse(
            KEEPALIVE.should_keep_alive(True, KEEPALIVE.START_MODE_ONE_SHOT))
        self.assertTrue(
            KEEPALIVE.should_keep_alive(True, KEEPALIVE.START_MODE_KEEP_ALIVE))

    def test_voice_off_never_arms_keep_alive(self):
        for mode in KEEPALIVE.START_MODES:
            self.assertFalse(KEEPALIVE.should_keep_alive(False, mode))

    def test_unknown_mode_falls_back_instead_of_breaking(self):
        """封闭词汇表不做就近取整，但坏偏好也不该把语音整个卡死。"""
        self.assertEqual(KEEPALIVE.normalize_start_mode("nonsense"),
                         KEEPALIVE.DEFAULT_START_MODE)
        self.assertEqual(KEEPALIVE.normalize_start_mode(None),
                         KEEPALIVE.DEFAULT_START_MODE)
        self.assertTrue(KEEPALIVE.should_keep_alive(True, "nonsense"))


class StartStepTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.sent = []

    def _fake_urlopen(self, reply):
        class Response:
            def __init__(self, payload):
                self._payload = json.dumps(payload).encode("utf-8")

            def read(self):
                return self._payload

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        def urlopen(request, timeout=None):
            self.sent.append(json.loads(request.data.decode("utf-8")))
            return Response(reply)
        return urlopen

    def _run(self, reply, clock=None):
        original = STEP.urllib.request.urlopen
        STEP.urllib.request.urlopen = self._fake_urlopen(reply)
        try:
            return STEP.start_once(endpoint="http://x", runtime=self.runtime,
                                   clock=clock)
        finally:
            STEP.urllib.request.urlopen = original

    def test_it_asks_the_bridge_for_one_shot_not_keep_active(self):
        """一次性,不是保活 —— 保活是持续语义,会跟自动关闭互相打架。"""
        self._run({"ok": True, "pressed": True, "confirmed": True})
        self.assertEqual(self.sent, [{"startVoiceOnce": True}])

    def test_elapsed_is_recorded_for_later_tuning(self):
        """超时值是暂定的,要靠这些样本来收 —— 没有耗时就没法调。"""
        ticks = iter([100.0, 137.5])
        result = self._run({"ok": True, "confirmed": False,
                            "reason": "not-confirmed"},
                           clock=lambda: next(ticks))
        self.assertEqual(result["elapsedSeconds"], 37.5)
        line = STEP.attempts_path(self.runtime).read_text(
            encoding="utf-8").strip()
        entry = json.loads(line)
        self.assertEqual(entry["elapsedSeconds"], 37.5)
        self.assertEqual(entry["confirmed"], False)
        self.assertEqual(entry["reason"], "not-confirmed")

    def test_initial_timeout_is_generous(self):
        """用户要的是"一开始时间搞长一点",等短了会把要成的那次判成失败,
        然后去按第二下 —— 而那一下可能正好把刚起来的通话关掉。"""
        self.assertGreaterEqual(STEP.ATTEMPT_TIMEOUT_SECONDS, 60.0)

    def test_unreachable_still_records_a_sample(self):
        original = STEP.urllib.request.urlopen

        def boom(*_a, **_k):
            raise OSError("no route")

        STEP.urllib.request.urlopen = boom
        try:
            result = STEP.start_once(endpoint="http://x",
                                     runtime=self.runtime)
        finally:
            STEP.urllib.request.urlopen = original
        self.assertEqual(result["reason"], "unreachable")
        self.assertTrue(STEP.attempts_path(self.runtime).exists())


class LadderTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _ladder(self, *, heartbeat=True, voice_enabled=True,
                codex=True, active=False, known=True):
        if heartbeat:
            (self.root / "readerpc-server.status.json").write_text(
                "{}", encoding="utf-8")
        return LADDER.ladder(
            local_root=self.root,
            runtime=self.root,
            preferences={"voiceEnabled": voice_enabled},
            ledger_reader=ledger(known=known, active=active),
            process_lister=lambda: (["ChatGPT.exe"] if codex else ["explorer.exe"]),
        )

    def test_server_down_is_reported_as_unreachable(self):
        """桥是 ReaderPC 的子进程 —— 它不在,请求根本没有接收方。
        这一级只报告,不假装在推进。"""
        status = self._ladder(heartbeat=False)
        self.assertEqual(status["reached"], 0)
        self.assertEqual(status["blockedAt"], "server")
        self.assertFalse(status["reachable"])

    def test_voice_chain_off_is_the_second_rung(self):
        status = self._ladder(voice_enabled=False)
        self.assertEqual(status["reached"], 1)
        self.assertEqual(status["blockedAt"], "chain")
        self.assertTrue(status["reachable"])
        self.assertIn("2/4", status["label"])

    def test_codex_missing_is_the_third_rung(self):
        status = self._ladder(codex=False)
        self.assertEqual(status["blockedAt"], "codex")
        self.assertIn("3/4", status["label"])

    def test_all_four_reads_as_connected(self):
        status = self._ladder(active=True)
        self.assertEqual(status["reached"], 4)
        self.assertIsNone(status["blockedAt"])
        self.assertEqual(status["label"], "语音已连接")

    def test_unknown_ledger_is_not_counted_as_satisfied(self):
        """不知道 ≠ 满足。混成一个布尔,卡住时就没人说得清卡在哪。"""
        status = self._ladder(known=False)
        session = status["rungs"][3]
        self.assertFalse(session["known"])
        self.assertFalse(session["satisfied"])
        self.assertEqual(status["blockedAt"], "session")

    def test_publish_is_atomic_and_readable(self):
        status = self._ladder(active=True)
        path = LADDER.publish(status, self.root)
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8"))["label"],
            "语音已连接")


class GiveUpTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        (self.root / "readerpc-server.status.json").write_text(
            "{}", encoding="utf-8")

    def test_giving_up_leaves_a_trace(self):
        """不报错的放弃跟"还在试"在界面上长得一样,人会一直等。"""
        result = FAILED.report(attempts=2, detail="not-confirmed",
                               runtime=self.root, local_root=self.root)
        self.assertTrue(result["ok"])
        status = json.loads(
            (self.root / LADDER.STATUS_FILE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(status["startGaveUp"]["attempts"], 2)
        self.assertEqual(status["startGaveUp"]["detail"], "not-confirmed")

    def test_failure_does_not_claim_voice_is_off(self):
        """两次没进语音**不代表**语音关着,只代表没能确认它开了。"""
        receipt = FAILED.voice_status_receipt.build_receipt(
            request_id="x", task_status="error", voice_status="unknown",
            evidence="试了两次")
        self.assertEqual(receipt["voiceStatus"], "unknown")
        result = FAILED.report(attempts=2, runtime=self.root)
        written = Path(result["receipt"]).read_text(encoding="utf-8")
        self.assertIn('"voiceStatus": "unknown"', written)
        self.assertNotIn('"voiceStatus": "ended"', written)


if __name__ == "__main__":
    unittest.main()
