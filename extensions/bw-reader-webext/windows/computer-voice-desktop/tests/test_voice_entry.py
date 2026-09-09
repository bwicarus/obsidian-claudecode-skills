"""语音入口这条链：梯子、一次性尝试、保活意图、放弃报错。

这一带最贵的两个错：
  · 把"不知道"当成"没在通话" —— 那个快捷键是**切换**，按下去会挂断；
  · 悄悄放弃 —— 界面上跟"还在试"长得一模一样，人会一直等。
下面每条都盯着其中之一。
"""
from __future__ import annotations

import importlib.util
import json
import re
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

    def _fake_urlopen_sequence(self, replies):
        """按顺序回答 —— 冷却那条要看"第二次才是真按"。"""
        pending = list(replies)

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
            return Response(pending.pop(0) if pending else {})
        return urlopen

    def _run(self, reply, clock=None, replies=None, sleeper=None):
        original = STEP.urllib.request.urlopen
        STEP.urllib.request.urlopen = (
            self._fake_urlopen_sequence(replies) if replies is not None
            else self._fake_urlopen(reply))
        try:
            return STEP.start_once(endpoint="http://x", runtime=self.runtime,
                                   clock=clock, sleeper=sleeper)
        finally:
            STEP.urllib.request.urlopen = original

    def test_cooldown_is_waited_out_not_spent_as_an_attempt(self):
        """冷却期挡下时要**等过去再问**，不能当一次尝试用掉。

        桥的守卫是"上一次按键的确认还没走完之前不许再按"（10s 确认 + 3s 沉降
        = 13s），而第一次尝试正是在确认窗口耗尽时返回的。调用方紧接着跑的第二次
        必然落在冷却里 —— 不等就问，等于把"重试一次"变成一次假重试：什么都没按，
        重试预算却花掉了。

        ⚠ 这是"改成走同一条链"带来的**新**情况：老的手拼链根本不看冷却，直接按，
        那正是"按掉刚开起来的通话"的隐患。守卫生效了，等待就得跟上。
        """
        naps: list[float] = []
        result = self._run(
            None,
            replies=[
                {"ok": False, "confirmed": False, "reason": "cooldown",
                 "cooldownSeconds": 13.0},
                {"ok": True, "pressed": True, "confirmed": True,
                 "reason": "started"},
            ],
            sleeper=naps.append,
        )
        self.assertEqual(naps, [13.0])
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(result["reason"], "started")
        self.assertEqual(result["cooldownWaitSeconds"], 13.0)

    def test_cooldown_wait_is_bounded_even_if_the_bridge_says_something_odd(self):
        """桥给的数不合理时用自己的上界 —— 但仍然要等，不是不等。"""
        for given in (None, 0, -5, "十三秒", True, 9999):
            naps: list[float] = []
            self.sent.clear()
            self._run(
                None,
                replies=[
                    dict({"reason": "cooldown"},
                         **({"cooldownSeconds": given}
                            if given is not None else {})),
                    {"ok": True, "confirmed": True, "reason": "started"},
                ],
                sleeper=naps.append,
            )
            self.assertEqual(len(naps), 1, given)
            self.assertLessEqual(naps[0], STEP.MAX_COOLDOWN_WAIT_SECONDS)
            self.assertGreater(naps[0], 0, given)

    def test_cooldown_leaves_both_samples(self):
        """等待要能从记录里看出来，否则调超时的时候会把等待算进"按一次要多久"。"""
        self._run(
            None,
            replies=[
                {"reason": "cooldown", "cooldownSeconds": 13.0},
                {"ok": True, "confirmed": True, "reason": "started"},
            ],
            sleeper=lambda _s: None,
        )
        lines = STEP.attempts_path(self.runtime).read_text(
            encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        first, second = (json.loads(line) for line in lines)
        self.assertEqual(first["reason"], "cooldown")
        self.assertEqual(first["cooldownWaitSeconds"], 0.0)
        self.assertEqual(second["reason"], "started")
        self.assertEqual(second["cooldownWaitSeconds"], 13.0)

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

    def test_label_never_claims_an_action_it_cannot_know_about(self):
        """这个文件是每 30 秒无条件写一次的**静态读数**。

        它不知道此刻有没有人在开语音，所以不能说「正在打开语音」——
        2026-09-09 没人在开的时候读到那句，一句话里三个互相矛盾的说法。
        "正在打开"的框由知道自己在连接的界面那侧去加。
        """
        for status in (self._ladder(heartbeat=False),
                       self._ladder(voice_enabled=False),
                       self._ladder(codex=False),
                       self._ladder(known=False)):
            self.assertNotIn("正在打开", status["label"])

    def test_blocked_step_is_named_by_what_it_waits_for(self):
        """状态名和待办名是两回事，混用会写出自相矛盾的话。

        第 4 级的 label 是「语音已连接」；拿它当"正在等的步骤名"就拼出
        「语音已连接 —— 当前没有进行中的通话」。
        """
        status = self._ladder(active=False)
        self.assertEqual(status["blockedAt"], "session")
        self.assertIn("通话接通", status["label"])
        self.assertNotIn("语音已连接", status["label"])
        self.assertIn("当前没有进行中的通话", status["label"])
        self.assertIn("4/4", status["label"])

    def test_every_rung_names_both_states(self):
        """每级都要有两个名字 —— 少一个，下次又会被拿去顶替。"""
        for rung in self._ladder()["rungs"]:
            self.assertTrue(rung["label"])
            self.assertTrue(rung["step"])
            self.assertEqual(rung["step"], LADDER.STEP_NAMES[rung["key"]])

    def test_publish_is_atomic_and_readable(self):
        status = self._ladder(active=True)
        path = LADDER.publish(status, self.root)
        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8"))["label"],
            "语音已连接")


class WiredUpTests(unittest.TestCase):
    """建好了但没人触发 —— 这一类错今天真的发生了（2026-09-10）。

    用户报「直接就变绿显示联通但是实际上 codex 语音没起来」。查下去:入口脚本、
    失败上报、梯子、端点、能力说明全建好了,`RequestVoiceEntryAsync` 那条推送
    **一个发送方都没有**。链上任何一环缺了都表现成"什么都没发生",而"什么都
    没发生"不会红任何一条测试。

    所以这里不钉某个名字,钉的是**形态**:凡是"请对面做一件事"的推送,都必须
    有生产代码在调它。以后新加一条同样跑不掉。
    """

    BRIDGE = (Path(__file__).resolve().parents[2] / "ComputerVoiceAudio")

    def test_every_push_request_has_a_caller(self):
        push = self.BRIDGE / "ReaderCodexPush.cs"
        source = push.read_text(encoding="utf-8")
        names = set(re.findall(
            r"internal static async Task<bool> (Request\w+Async)\(", source))
        self.assertTrue(names, "一条 Request*Async 都没找到,正则该修了")
        callers = {name: [] for name in names}
        for path in self.BRIDGE.rglob("*.cs"):
            if path.name in (push.name, "DirectBridgeSelfTest.cs",
                             "ReaderCodexPushSelfTest.cs"):
                continue
            body = path.read_text(encoding="utf-8")
            for name in names:
                if name in body:
                    callers[name].append(path.name)
        orphans = sorted(n for n, where in callers.items() if not where)
        self.assertEqual(
            orphans, [],
            "这些推送没有任何生产代码在调用 —— 功能等于不存在: %s" % orphans)

    def test_voice_entry_push_is_sent_from_the_start_path(self):
        """入口推送必须挂在"音频通道刚通"那一步上。

        那是唯一知道"用户此刻要开语音"的时刻;挪到别处(比如定时器)就会变成
        没人要求也去催对面。
        """
        source = (self.BRIDGE / "DirectBridgeProtocol.cs").read_text(
            encoding="utf-8")
        self.assertIn("RequestVoiceEntryIfNobodyElseWill(appKind)", source)
        hook = source.split("private void RequestVoiceEntryIfNobodyElseWill")[1]
        hook = hook.split("private async Task<object> HandleStopAsync")[0]
        # 三条判据缺一条都会做错事,见那段的 remarks。
        self.assertIn("_codexVoiceControl.KeepActive", hook)
        self.assertIn("Active == true", hook)
        self.assertIn("DirectAppTargets.CodexDesktop", hook)

    def test_give_up_flag_reaches_the_surface_that_shows_the_blinking(self):
        """放弃的痕迹要能到显示按钮的那一层。

        voice_start_failed.py 会把 startGaveUp 写进梯子状态,但桥只透传挑出来的
        几个字段 —— 漏掉它,那个脚本存在的全部理由(留下痕迹)就落空了:按钮一直闪,
        人一直等一件不会再成的事。
        """
        source = (self.BRIDGE / "DirectBridgeProtocol.cs").read_text(
            encoding="utf-8")
        self.assertIn("startGaveUp", source)
        reader = (Path(__file__).resolve().parents[4]
                  / "_server_deploy" / "static" / "pdf" / "rc-voicecall.js")
        if reader.is_file():
            self.assertIn("ladder.startGaveUp", reader.read_text(
                encoding="utf-8"))


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
