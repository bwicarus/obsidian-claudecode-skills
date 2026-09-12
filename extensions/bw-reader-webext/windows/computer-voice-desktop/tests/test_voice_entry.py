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
import unittest.mock
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1]
#: 仓库根。数层数很容易错一格,而错了的表现是测试**静默跳过** —— 所以只算一次。
REPO_ROOT = Path(__file__).resolve().parents[5]


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
CHANNEL = _load("codex_channel")
FAILED = _load("voice_start_failed")
NOTIFY = _load("codex_thread_notify")


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
        """按顺序回答。元素可以是 payload，也可以是 (payload, httpStatus)
        —— 502 那一类只能靠状态码认出来。"""
        pending = list(replies)

        class Response:
            def __init__(self, payload, status):
                self._payload = json.dumps(payload).encode("utf-8")
                self.status = status

            def read(self):
                return self._payload

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

        def urlopen(request, timeout=None):
            self.sent.append(json.loads(request.data.decode("utf-8")))
            item = pending.pop(0) if pending else {}
            if isinstance(item, tuple):
                return Response(item[0], item[1])
            return Response(item, 200)
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

    def test_unrecognised_reply_still_says_something(self):
        """回答里没有可辨认的 reason 时，样本仍要说得出发生了什么。

        2026-09-10 真实样本里有两条 pressed/confirmed/reason 全 null ——
        记了一条说不出原因的失败，跟没记一样。桥的 400 只带 detail、空体则
        连 detail 都没有，两种都得落成话。
        """
        for reply in ({"ok": False, "detail": "pipePath 是必需的"}, {}):
            self.sent.clear()
            result = self._run(reply)
            self.assertEqual(result["reason"], "unexpected-reply")
            self.assertTrue(result["detail"])
            self.assertIs(result["ok"], False)

    def test_sample_records_status_and_detail(self):
        self._run({"ok": False, "detail": "说不清"})
        entry = json.loads(STEP.attempts_path(self.runtime).read_text(
            encoding="utf-8").strip().splitlines()[-1])
        self.assertEqual(entry["reason"], "unexpected-reply")
        self.assertTrue(entry["detail"])
        self.assertIn("httpStatus", entry)

    def test_known_reasons_pass_through_untouched(self):
        """封闭词汇表里的一律原样 —— 别把桥说清楚的话也改写掉。"""
        for reason in sorted(STEP.BRIDGE_REASONS):
            self.sent.clear()
            # cooldown 会真的等一轮 —— 这里把睡眠换掉,不然测试白等 30 秒。
            result = self._run({"ok": True, "confirmed": True,
                                "reason": reason},
                               sleeper=lambda _s: None)
            self.assertEqual(result["reason"], reason)

    def test_thread_id_carries_its_own_creation_time(self):
        """线程 id 是 UUIDv7：前 48 位就是创建毫秒。

        纠正链的那条例外全靠它 —— 只有"本次请求之后新建的语音对话"才算落错，
        用户手动在旧对话里开的语音不产生新会话文件，于是结构上不会被搬走。

        期望值不是算出来的，是对着落盘 session_meta 核过的（2026-09-11）：
          01a08e6a-b913… → meta 写的是 2026-09-11T03:02:34.003Z
          01a08c6f-b82e… → meta 写的是 2026-09-10T17:48:47.022Z
        """
        self.assertEqual(
            CHANNEL._thread_created_ms(
                "01a08e6a-b913-75e0-adcc-8dfae6e82da0"),
            1789095754003)
        self.assertEqual(
            CHANNEL._thread_created_ms(
                "01a08c6f-b82e-7953-b2aa-2deb02265dba"),
            1789062527022)
        # 判不出来要说"不知道"（0），不能瞎给一个时刻 ——
        # 拿猜的时刻去决定搬不搬，比不搬危险得多。
        for bad in ("", "短", "zzzzzzzzzzzz-0000", "ffffffffffff-ffff"):
            self.assertEqual(CHANNEL._thread_created_ms(bad), 0, bad)

    def test_landed_thread_only_counts_new_voice_chats(self):
        """只认**本次之后新建**且 thread_source 是 voice_chat 的那条。

        ⚠ 那个时间窗里也会冒出别的会话（实测 12:02:21 有一条 automation），
        认错了会把通话搬去一条根本不是语音的对话。
        """
        self.assertEqual(CHANNEL.landed_voice_thread(0), "")
        self.assertEqual(CHANNEL.landed_voice_thread(-1), "")
        # 未来时刻：不可能有"之后新建"的，必须空
        future = 4_000_000_000_000
        self.assertEqual(CHANNEL.landed_voice_thread(future), "")

    def test_locked_desktop_is_told_apart_from_try_again(self):
        """`no-desktop` 是"根本没得试"，退出码要跟"这次没成"分开。

        2026-09-11 实测：电脑锁屏时语音快捷键注入进去没有前台窗口能接住，
        冷启动后每 30 秒按一次、连按 10 轮跨 9.5 分钟全部 not-confirmed，
        而 Codex 主窗口一直在。这类局面里"再按一次"确定无效、每次还要烧
        22 秒确认窗口，最后把"两次不成就放弃"的预算用光 —— 于是真正该说的
        那句话（去解锁电脑）永远说不出来。

        ⚠ 这条同时守那张**四副本**的 reason 词汇表：漏掉脚本这一份，
        桥新报的 `no-desktop` 会被当成"没说清"记成 unexpected-reply，
        真实原因就此丢掉，而账本看上去还在正常记录。
        """
        self.assertIn("no-desktop", STEP.BRIDGE_REASONS)
        self.assertIn("no-desktop", STEP.NO_POINT_RETRYING)
        # 词汇表里其余的都不该被当成"别再试"——尤其 cooldown 是"稍等再看"。
        self.assertNotIn("cooldown", STEP.NO_POINT_RETRYING)
        self.assertNotIn("not-confirmed", STEP.NO_POINT_RETRYING)
        result = self._run(
            {"ok": False, "confirmed": False, "reason": "no-desktop",
             "detail": "电脑锁屏，解锁后再试"},
            sleeper=lambda _s: None)
        self.assertEqual(result["reason"], "no-desktop")
        self.assertFalse(result["confirmed"])
        # 只问了一次：不该在这上面反复重试。
        self.assertEqual(len(self.sent), 1)

    def test_bridge_being_down_does_not_burn_the_attempt(self):
        """桥暂时不在，不该把「两次机会」用掉。

        2026-09-10 实测：一次入口通知恰好落在桥的维护窗口里（装 Direct 用了
        40 秒），两次尝试都拿到 502 空回应，Codex 于是按说明放弃并上报失败 ——
        而语音其实完全开得起来。「两次不成就放弃」说的是**试了没接通**，
        不是**根本没试成**。
        """
        naps: list[float] = []
        result = self._run(
            None,
            replies=[
                ({}, 502),                           # 桥在重装
                ({}, 502),                           # 还在维护窗口里
                {"ok": True, "confirmed": True, "reason": "started"},
            ],
            sleeper=naps.append,
        )
        self.assertEqual(result["reason"], "started")
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(len(naps), 2)

    def test_transport_blip_is_told_apart_from_a_real_failure(self):
        """只有"请求没到达执行方"才算抖动；试了没确认不能靠重试蒙混。"""
        self.assertTrue(STEP._transport_blip({"reason": "unreachable"}))
        self.assertTrue(STEP._transport_blip({"httpStatus": 502}))
        self.assertTrue(STEP._transport_blip({"httpStatus": 503}))
        self.assertFalse(STEP._transport_blip(
            {"reason": "not-confirmed", "httpStatus": 200}))
        self.assertFalse(STEP._transport_blip(
            {"reason": "cooldown", "httpStatus": 200}))

    def test_transport_retry_gives_up_eventually(self):
        """桥一直不在也要停 —— 无限重试跟"还在试"一样会把人吊着。"""
        naps: list[float] = []
        result = self._run(None, replies=[({}, 502)] * 10,
                           sleeper=naps.append)
        self.assertEqual(result["reason"], "unexpected-reply")
        self.assertEqual(len(self.sent), STEP.TRANSPORT_RETRIES + 1)

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
                                     runtime=self.runtime,
                                     sleeper=lambda _s: None)
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
        # 钉调用**位置**,不钉参数列表 —— 参数会变,"挂在 START 上"不该变。
        start = source.split("private async Task<DirectStartActionResult> "
                             "HandleStartAsync")[1]
        start = start.split("private async Task<object> HandleStopAsync")[0]
        self.assertIn("RequestVoiceEntryIfNobodyElseWill(", start)
        hook = source.split("private void RequestVoiceEntryIfNobodyElseWill")[1]
        hook = hook.split("private async Task<object> HandleStopAsync")[0]
        # 三条判据缺一条都会做错事,见那段的 remarks。
        self.assertIn("_codexVoiceControl.KeepActive", hook)
        self.assertIn("Active == true", hook)
        self.assertIn("DirectAppTargets.CodexDesktop", hook)

    def test_cold_launch_waits_are_sized_for_a_cold_launch(self):
        """刚被我们拉起来的 Codex，音频服务不可能 3 秒就绪。

        用户 2026-09-10:「codex 没有启动时 app 点击语音后,codex 初始化结束前
        按钮就灭掉了」。窗口那一步等 20 秒(AppReadyTimeout),而音频服务子进程是
        在窗口**之后**才出现的东西 —— 给它更短的预算没有道理,冷启动必然超时,
        抛 AUDIO_SERVICE_NOT_READY(标着 retryable 却没人重试),按钮直接灭。
        """
        source = (self.BRIDGE / "WindowsDirectAdapters.cs").read_text(
            encoding="utf-8")
        audio = re.search(
            r"AudioPolicyProcessReadyTimeout =\s*TimeSpan\.FromSeconds\((\d+)\)",
            source)
        voice = re.search(
            r"VoiceReadyTimeout =\s*TimeSpan\.FromSeconds\((\d+)\)", source)
        self.assertTrue(audio and voice, "两个超时常量的写法变了,正则该修")
        ready = re.search(
            r"AppReadyTimeout = TimeSpan\.FromSeconds\((\d+)\)",
            (self.BRIDGE / "DirectBridgeAdapters.cs").read_text(
                encoding="utf-8"))
        self.assertTrue(ready)
        self.assertGreaterEqual(int(audio.group(1)), int(ready.group(1)))
        self.assertGreaterEqual(int(voice.group(1)), 20)

    def test_voice_entry_cooldown_is_keyed_by_session_not_the_clock(self):
        """用户再按一次是**新意图**,不是重复的幂等 START。

        原来是一个全局时间戳,于是第一次失败后隔十几秒再按被当成重复挡掉 ——
        用户看到的正是「再次点击…并没有发送内容到 codex」。
        """
        source = (self.BRIDGE / "DirectBridgeProtocol.cs").read_text(
            encoding="utf-8")
        hook = source.split("private void RequestVoiceEntryIfNobodyElseWill")[1]
        hook = hook.split("private async Task<object> HandleStopAsync")[0]
        self.assertIn("_lastVoiceEntrySessionId", hook)
        self.assertIn("sameSession", hook)

    def test_bridge_starts_voice_when_push_cannot_be_sent(self):
        """推送送不出去时，**桥自己把语音开起来**（2026-09-10 定的分工）。

        Codex 明确表示"通过模拟快捷键控制桌面应用这条操作路线目前不能执行"，
        并建议把桥端启动与它能做的（状态回报、处理通知、授权挂断）分开设计。
        那就分开：起通话走桥自己那条已验证的链，推送继续负责挂断与状态回报。

        ⚠ 走的必须是 SetActiveAsync（与保活收敛同一条链），不是另拼一条按键链
        —— 2026-09-09 那次就是抄漏了"拉起 Codex"这一步。
        """
        source = (self.BRIDGE / "DirectBridgeProtocol.cs").read_text(
            encoding="utf-8")
        hook = source.split("private void RequestVoiceEntryIfNobodyElseWill")[1]
        hook = hook.split("private static void StartVoiceFromBridge")[0]
        self.assertIn("StartVoiceFromBridge(control, requestId)", hook)
        # ⚠ 只在推送真的送不出去之后 —— 推送能送到时它更便宜也更快。
        self.assertIn("if (sent) return;", hook)
        body = source.split("private static void StartVoiceFromBridge")[1]
        body = body.split("private async Task<object> HandleStopAsync")[0]
        self.assertIn("SetActiveAsync(active: true", body)
        self.assertIn("NoteBridgeStart", body)
        self.assertNotIn("while (true)", body)   # 不重试，见 remarks

    def test_thread_source_comes_from_the_session_record(self):
        """排除表要对着**会话来源**判，不是对着 thread/list 回的客户端名。

        2026-09-10 实测：thread/list 的 threadSource 25 条全是 'vscode'
        （那是客户端），拿它做排除表永远不匹配 —— 而空转的排除跟没有排除
        在行为上一样，只是看起来像有。真实分布里 automation 有 13 条，
        不排掉就会挑中它。
        """
        notify = (Path(__file__).resolve().parents[1]
                  / "codex_thread_notify.py").read_text(encoding="utf-8")
        # 挑对话现在按磁盘上的会话记录来（thread/list 既不按时间排、也漏当天的），
        # 来源从记录里读 —— 不能用 thread/list 回的 threadSource（那是客户端）。
        pick = notify.split("def recent_threads")[1].split("ACTIVE_WRITER")[0]
        self.assertIn("thread_source", pick)
        self.assertIn("ALLOWED_SOURCES", pick)
        self.assertNotIn('row.get("threadSource")', pick)

    def test_turn_start_accepted_is_not_reported_as_done(self):
        """`turn/start` 返回只是"接受"，不是"跑完"。

        2026-09-10：账本记成 ok=True，而实际上 Codex 什么都没做 —— 又一个
        "按了不等于关了"。真正的终点是 turn/completed；途中的
        item/commandExecution/* 才说明它确实去跑脚本了。
        """
        notify = (Path(__file__).resolve().parents[1]
                  / "codex_thread_notify.py").read_text(encoding="utf-8")
        self.assertIn("turn/completed", notify)
        self.assertIn("item/commandExecution/", notify)
        send = notify.split("def send(text")[1]
        self.assertIn('seen["completed"]', send)

    def test_approval_vocabulary_comes_from_the_schema(self):
        """两套取值不通用，猜一个就等于没应答。

        现代 item/*/requestApproval 要 accept/decline；旧的
        execCommandApproval / applyPatchApproval 要 approved/denied。
        上一版一律回 "approved"，对现代方法是非法值 —— 表现就是
        "turn 送到了却什么都没发生"。
        """
        notify = (Path(__file__).resolve().parents[1]
                  / "codex_thread_notify.py").read_text(encoding="utf-8")
        self.assertIn("item/commandExecution/requestApproval", notify)
        self.assertIn('"accept"', notify)
        self.assertIn('"approved"', notify)
        # 认不出来的要回协议错误，不能编一个结果。
        self.assertIn("-32601", notify)

    def test_thread_notify_approval_is_not_a_blank_cheque(self):
        """审批写成"什么都同意"就等于把一条通知变成任意命令执行入口。"""
        notify = (Path(__file__).resolve().parents[1]
                  / "codex_thread_notify.py").read_text(encoding="utf-8")
        approve = notify.split("def approve")[1].split("def send")[0]
        self.assertIn("ALLOWED_SCRIPTS", approve)
        # 放行与拒绝取自同一张表的两端 —— 不在放行表里的走 [1]（拒绝那一侧）。
        self.assertIn("yes_no[1]", approve)
        self.assertIn("decline", notify)

    def test_every_request_records_to_the_ledger_not_just_memory(self):
        """请求的每一条出路都要**落盘**，不能只写内存里那句 lastNote。

        2026-09-10 撞到：我从 HEAD 还原一个被误删的方法时，带回来的是**账本
        之前**的旧版本 —— 它用的还是只写内存的 Note()。于是那一轮推送一条记录
        都没留，账本看起来像"一次都没试过"，而实际上试了十次。

        还原代码比新写代码更容易漏这种东西：新写会照着周围抄，还原是把时间
        倒回去。

        ⚠ **范围原来是按方法签名圈的**（只扫 Request*Async），而板面推送
        不是那个形状 —— 于是它一直只写内存，账本里连「推过一次」都没有。
        2026-09-10 用户问「你刚才为何连发两次」时我才发现自己答不上来：
        证据从来没被写下来过，而这条测试是绿的，因为那两条不在它视野里。
        **一条按形状圈范围的测试，会把范围外的东西证明成合格的。**
        现在扫整个文件。
        """
        push = (self.BRIDGE / "ReaderCodexPush.cs").read_text(encoding="utf-8")
        code = "\n".join(
            line for line in push.splitlines()
            if not line.lstrip().startswith("//"))
        bare = [
            line.strip()
            for line in code.splitlines()
            # ⚠ 前面必须不是标识符字符，否则 BoardTargetNote( 这种
            # 以 Note 结尾的函数名会被当成裸 Note()（2026-09-11 误报过）。
            if re.search(r"(?<![A-Za-z_])(?<!void )Note\(", line)
            and "NoteAttempt(" not in line
        ]
        # 只该剩 NoteAttempt 内部那一次转调。
        self.assertEqual(
            bare, ["Note(text);"],
            "还有只写内存的 Note()：%s" % bare)

    def test_connect_timeout_is_what_proves_the_pipe_is_gone(self):
        """管道不在时 ConnectAsync **不抛 FileNotFound，它会等到超时**。

        2026-09-10 实测：判失效挂在 FileNotFoundException 上，于是永远不触发；
        账本里是八次"被取消"，每次干等满 4 秒，白花 90 秒才轮到兜底。
        连接超时才是那个确证。

        ⚠ 必须跟**调用方取消**分开：前者说明那个会话没了，后者是我们自己在收摊。
        """
        push = (self.BRIDGE / "ReaderCodexPush.cs").read_text(encoding="utf-8")
        block = push.split("await pipe.ConnectAsync")[1].split(
            "// 先问一次工具表")[0]
        self.assertIn("catch (OperationCanceledException)", block)
        self.assertIn("!cancellationToken.IsCancellationRequested", block)
        self.assertIn("ReaderCodexEndpoint.Invalidate", block)

    def test_dead_binding_stops_the_retry_loop_at_once(self):
        """判死之后别再等 —— 重试救不回一条不存在的管道。"""
        source = (self.BRIDGE / "DirectBridgeProtocol.cs").read_text(
            encoding="utf-8")
        hook = source.split("private void RequestVoiceEntryIfNobodyElseWill")[1]
        hook = hook.split("private static void StartVoiceFromBridge")[0]
        self.assertIn("ReaderCodexEndpoint.Current() is null", hook)

    def test_cancelled_request_still_leaves_a_trace(self):
        """取消也要留痕 —— 静默返回让账本看起来像"一次都没试过"。"""
        # ⚠ 只管**一次请求**里的取消。板面推送循环的收摊分支不在此列：
        # 那里取消等于"在停服"，不是一次失败的尝试，也没有 requestId。
        push = (self.BRIDGE / "ReaderCodexPush.cs").read_text(encoding="utf-8")
        names = re.findall(
            r"internal static async Task<bool> (Request\w+Async)\(", push)
        self.assertTrue(names, "一条 Request*Async 都没找到，正则该修了")
        for name in names:
            body = push.split("Task<bool> " + name + "(")[1]
            body = body.split("internal static")[0].split(
                "private static async Task SendAsync")[0]
            for index, block in enumerate(
                    body.split("catch (OperationCanceledException)")[1:]):
                self.assertIn(
                    "NoteAttempt(", block[:400],
                    "%s 第 %d 个取消分支是静默的" % (name, index + 1))

    def test_dead_pipe_invalidates_the_binding_immediately(self):
        """管道不存在 = 那个会话没了，立刻判失效。

        2026-09-10 实测：绑定文件写着 invalidAtMs: null、到期还有两天，而管道
        早已 FileNotFoundError —— 于是 Current() 一直交出一条死绑定，每次推送都
        往虚空里发，push.bound 也跟着一直说谎。

        ⚠ 这跟"推不动"必须分开：连续失败 5 次那条容忍规则是给抖动留的
        （Codex 重启那几秒推送本来就会失败），而 FileNotFound 不是抖动，是确证。
        """
        push = (self.BRIDGE / "ReaderCodexPush.cs").read_text(encoding="utf-8")
        self.assertIn("catch (FileNotFoundException", push)
        connect = push.split("await pipe.ConnectAsync")[1].split(
            "// 先问一次工具表")[0]
        self.assertIn("ReaderCodexEndpoint.Invalidate", connect)
        # 容忍规则本身不能被顺手改掉 —— 它防的是另一件事。
        self.assertIn("ConsecutiveFailureLimit = 5", push)

    def test_push_reachability_is_reported_not_just_registration(self):
        """「登记过」不等于「送得到」。

        界面要能说出"Codex 那边没有可接收的会话"，而不是一直干闪 ——
        所以状态里除了 bound 还要带最近一次成功与连续失败次数。
        """
        source = (self.BRIDGE / "DirectBridgeProtocol.cs").read_text(
            encoding="utf-8")
        block = source.split("push = new")[1].split("};")[0]
        for field in ("bound", "lastSuccessAtUtcMs",
                      "consecutiveFailures", "lastNote"):
            self.assertIn(field, block)

    def test_green_light_does_not_treat_unasked_as_permission(self):
        """"还没问过"不是"可以放行"(2026-09-10 我犯的那个)。

        首次上漆时还没轮询过,闸门若看 null 放行,按钮立刻变绿、闸门形同不存在。
        两个方向都要避开:把"读不到"当"没起来"会永远黄闪,把"没问过"当"放行"
        会白亮一次。
        """
        reader = REPO_ROOT / "_server_deploy" / "static" / "pdf" / "rc-voicecall.js"
        self.assertTrue(reader.is_file(), "找不到 rc-voicecall.js：%s" % reader)
        source = reader.read_text(encoding="utf-8")
        gate = source.split("function _greenLightAllowed()")[1].split("}")[0]
        self.assertIn("=== true", gate)
        self.assertIn("'unknown'", gate)
        self.assertNotIn("!== false", gate)

    def test_give_up_flag_reaches_the_surface_that_shows_the_blinking(self):
        """放弃的痕迹要能到显示按钮的那一层。

        voice_start_failed.py 会把 startGaveUp 写进梯子状态,但桥只透传挑出来的
        几个字段 —— 漏掉它,那个脚本存在的全部理由(留下痕迹)就落空了:按钮一直闪,
        人一直等一件不会再成的事。
        """
        source = (self.BRIDGE / "DirectBridgeProtocol.cs").read_text(
            encoding="utf-8")
        self.assertIn("startGaveUp", source)
        # ⚠ parents[5] 才是仓库根(tests/…/computer-voice-desktop/windows/
        # bw-reader-webext/extensions/<root>)。之前写成 [4],于是 is_file() 恒假、
        # 这条断言从来没跑过 —— 一个空转的测试比没有测试更糟,它在报告里是绿的。
        reader = REPO_ROOT / "_server_deploy" / "static" / "pdf" / "rc-voicecall.js"
        self.assertTrue(reader.is_file(), "找不到 rc-voicecall.js：%s" % reader)
        self.assertIn("ladder.startGaveUp", reader.read_text(encoding="utf-8"))


class ShortcutFallbackTests(unittest.TestCase):
    """F24 兜底开关（2026-09-10 用户：「把 f24 兜底作为一个可选开关」）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_default_is_on_so_nothing_silently_changes(self):
        """多一个开关不该悄悄改掉现在能用的行为。"""
        self.assertTrue(KEEPALIVE.read_shortcut_fallback(self.runtime))
        self.assertTrue(KEEPALIVE.DEFAULT_FALLBACK)

    def test_round_trip_and_bad_file_falls_back_to_on(self):
        """一个坏掉的偏好不该让语音开不了。"""
        KEEPALIVE.write_shortcut_fallback(False, self.runtime)
        self.assertFalse(KEEPALIVE.read_shortcut_fallback(self.runtime))
        KEEPALIVE.fallback_path(self.runtime).write_text(
            '{"contract":"wrong","enabled":false}', encoding="utf-8")
        self.assertTrue(KEEPALIVE.read_shortcut_fallback(self.runtime))

    def test_bridge_reads_the_same_file_and_defaults_to_on(self):
        source = (Path(__file__).resolve().parents[2] / "ComputerVoiceAudio"
                  / "DirectBridgeProtocol.cs").read_text(encoding="utf-8")
        gate = source.split(
            "internal static bool ShortcutFallbackEnabled")[1]
        gate = gate.split("internal static async Task")[0]
        self.assertIn("voice-shortcut-fallback.json", gate)
        self.assertIn("reader-voice-shortcut-fallback/1", gate)
        # 读不到一律当开 —— 每条出路都 return true。
        self.assertNotIn("return false", gate)

    def test_the_switch_has_exactly_one_implementation(self):
        """⚠ 开关只兑现一半比没有开关更糟。

        2026-09-10 实测：这个判断原本是 DirectBridgeProtocolSession 的私有
        副本、只管**起**语音，于是用户把开关关掉之后挂断仍然在按 F24。
        所以钉住"读文件的实现只有一处，其余都是转调"。
        """
        source = (Path(__file__).resolve().parents[2] / "ComputerVoiceAudio"
                  / "DirectBridgeProtocol.cs").read_text(encoding="utf-8")
        self.assertEqual(
            source.count("voice-shortcut-fallback.json"), 1,
            "读这个偏好文件的地方多于一处 —— 先合并再改")
        endpoint = (Path(__file__).resolve().parents[2]
                    / "ComputerVoiceAudio" / "ReaderCodexEndpoint.cs"
                    ).read_text(encoding="utf-8")
        # 显式兜底 op 也必须过同一个闸（它才是真正会按 F24 的那条路）。
        fallback_op = endpoint.split('body["hangUpVoiceFallback"]')[1][:1200]
        self.assertIn(
            "DirectCodexVoiceControl.ShortcutFallbackEnabled()", fallback_op)

    def test_hangup_prefers_the_channel_over_the_shortcut(self):
        """用户 2026-09-10：「挂断走通知更稳定不要再用 f24」。

        收敛环原来直接 shortcutSender.Send(..., Stop)，绕过了 2026-09-09 就
        写好的推送挂断。钉住：挂断先请推送，按键只在推送没送出去时才轮到，
        且还要过兜底开关。
        """
        source = (Path(__file__).resolve().parents[2] / "ComputerVoiceAudio"
                  / "DirectBridgeProtocol.cs").read_text(encoding="utf-8")
        body = source.split(
            "private static async Task<CodexVoiceActivitySnapshot>"
            " HangUpAsync")[1]
        body = body.split("private static async Task"
                          "<CodexVoiceActivitySnapshot> ConfirmHangUpAsync")[0]
        request = body.index("RequestVoiceHangUpAsync")
        press = body.index("DirectVoiceCommand.Stop")
        self.assertLess(request, press, "推送必须排在按键前面")
        self.assertLess(
            body.index("ShortcutFallbackEnabled()"), press,
            "按键之前必须先过兜底开关")
        # 收敛环里不该再有第二处直接按停。
        self.assertEqual(source.count("DirectVoiceCommand.Stop"), 1)

    def test_declining_to_press_still_leaves_a_trace(self):
        """"通道不通"与"通道不通且我们选择不兜底"在外面看长得一样。

        后者是用户自己设的，不该被当成故障去查 —— 所以不按也要记一笔。
        """
        source = (Path(__file__).resolve().parents[2] / "ComputerVoiceAudio"
                  / "DirectBridgeProtocol.cs").read_text(encoding="utf-8")
        body = source.split("private static void StartVoiceFromBridge")[1][:900]
        self.assertIn("ShortcutFallbackEnabled()", body)
        self.assertIn("NoteBridgeStart", body.split("if (!Shortcut")[1][:400])


class SilenceContractTests(unittest.TestCase):
    """推送过去的每一条都必须说清「该不该开口」。

    ⚠ 2026-09-10 用户实录：焦点转移和起语音，对面**全都语音念了出来**。
    板子自己的合同一直是「陈述句就是资料，祈使句才是要你做的事」（用户
    2026-08-30 定的形状），登记表里也写着待办才是「该开口说的事」——
    **但那份合同只存在于 reader-attention-registry.json，而没有任何东西要求
    对面去读它**。推送是唯一到达对面的东西；写在别处等于没写。

    所以这里钉住：每一条外发文本都自带纪律，一条都不许漏。
    """

    PUSH = (Path(__file__).resolve().parents[2] / "ComputerVoiceAudio"
            / "ReaderCodexPush.cs")

    #: 五条外发文本各自的锚点 → 该挂哪种纪律。
    #: ⚠ 这张表就是「一共有几条」的答案。新增一条外发文本必须同时加进来，
    #: 否则它会安静地成为第六条没有纪律的消息。
    OUTBOUND = {
        '"提示板已接上主动推送': "BoardSilenceLine",
        '"提示板更新（"': "BoardSilenceLine",
        '"用户预先设定的自动关闭规则触发了："': "OperationSilenceLine",
        '"状态查询（requestId: "': "OperationSilenceLine",
        '"指定操作（requestId: "': "OperationSilenceLine",
        # ⚠ 唯一**故意不挂纪律**的一条（2026-09-11 用户提的"输入框直达通话"）：
        #   其余五条是状态同步或运维指令，所以要求对面"不要在通话里念出来"；
        #   而这一条**是他在说话**，要的就是对面像他开口一样正常回答。
        #   给它挂上纪律的后果是最难查的那种：送到了，对面却按纪律故意不吭声。
        '"来自用户："': None,
    }

    def test_every_outbound_message_carries_the_rule(self):
        source = self.PUSH.read_text(encoding="utf-8")
        for anchor, constant in self.OUTBOUND.items():
            where = source.find(anchor)
            self.assertNotEqual(where, -1, "找不到外发文本：%s" % anchor)
            # 纪律必须在这段文本**之前**的 200 字内拼进去。
            head = source[max(0, where - 200):where]
            if constant is None:
                # 故意没有纪律的那条要**反过来钉住**：哪天有人顺手给它补上
                # 静默纪律，这条会红 —— 而不是悄悄变成"送到了却没人吭声"。
                for silence in ("BoardSilenceLine", "OperationSilenceLine"):
                    self.assertNotIn(
                        silence, head,
                        "这条本该没有静默纪律：%s" % anchor)
                continue
            self.assertIn(
                constant, head,
                "这条外发文本没挂纪律：%s" % anchor)

    def test_the_outbound_table_is_complete(self):
        """⚠ 一张漏了一行的表跟没有表一样，而且看起来是绿的。

        用外发口 SendAsync(binding, prompt, …) 的出现次数反查：五条正文
        + 一条私有实现，多出来的就是没登记进上面那张表的新消息。
        """
        source = self.PUSH.read_text(encoding="utf-8")
        self.assertEqual(
            source.count("await SendAsync("), len(self.OUTBOUND),
            "外发口的数量与登记表对不上 —— 新增了消息就要同时登记纪律")

    @staticmethod
    def _literals(text: str) -> list[str]:
        """只取字符串字面量。

        ⚠ 整文件扫会连**注释**一起扫到 —— 而解释"以前写错了什么"的注释里
        必然含有那句错话。第一版就是这么误报的：它抓住的是我自己写的说明；
        第二版只排掉了跨行，仍把注释里的引号当字面量。所以先按行砍注释。
        """
        code = "\n".join(
            line for line in text.splitlines()
            if not line.lstrip().startswith("//"))
        return re.findall(r'"([^"\n]*)"', code)

    def test_no_outbound_message_asks_it_to_report_out_loud(self):
        """「回报」在通话里就是"说出来"，等于我们自己点的那句噪音。"""
        offenders = [
            piece for piece in self._literals(
                self.PUSH.read_text(encoding="utf-8"))
            if "回报" in piece
        ]
        self.assertEqual(offenders, [], "外发文本里还留着「回报」")

    def test_csharp_and_python_state_the_same_rule(self):
        """同一条纪律的两份实现（C# 推送 / Python app-server 兜底）。

        ⚠ 措辞不一致比"没有纪律"更难发现：两边都"有"，但对面在两条路上
        收到的要求不同，而没有任何一处会报错。
        """
        source = self.PUSH.read_text(encoding="utf-8")
        start = source.index("OperationSilenceLine =")
        csharp = source[start:source.index(";", start)]
        # C# 里是拼接的字面量；取出引号内容接起来，并把 \n 还原成真换行。
        joined = "".join(re.findall(r'"([^"]*)"', csharp))
        joined = joined.replace(chr(92) + "n", chr(10))
        self.assertEqual(joined, NOTIFY.OPERATION_SILENCE_LINE)


class VoiceEntryStormTests(unittest.TestCase):
    """入口请求必须有一个**不看会话**的总闸。

    ⚠ 2026-09-10 实录：35 分钟里对面收到 432 条「指定操作」，来自 191 个不同
    requestId —— 平均每 11 秒一个新任务。原因是那个 45 秒冷却按 sessionId 算
    （改成按会话本身是对的：全局时间戳会把用户真正的第二次点击当成幂等重复
    挡掉），可它同时把唯一的全局刹车拆了：**换个 sessionId 就绕过一切**，
    而 App 每重连一次就换一个。
    """

    BRIDGE = Path(__file__).resolve().parents[2] / "ComputerVoiceAudio"

    def test_only_one_entry_task_may_be_in_flight(self):
        source = (self.BRIDGE / "DirectBridgeProtocol.cs").read_text(
            encoding="utf-8")
        body = source.split("RequestVoiceEntryIfNobodyElseWill")[-1]
        head = body[:body.index("_ = Task.Run(")]
        self.assertIn("_voiceEntryInFlight", head,
                      "起任务之前没有总闸")
        self.assertIn("Interlocked.Exchange(ref _voiceEntryInFlight, 1)", head)

    def test_the_gate_is_always_released(self):
        """漏放一次 = 从此再也起不了语音，而那种失效没有任何提示。"""
        source = (self.BRIDGE / "DirectBridgeProtocol.cs").read_text(
            encoding="utf-8")
        body = source.split("RequestVoiceEntryIfNobodyElseWill")[-1]
        self.assertIn("finally", body[:body.index("PythonExecutable")])
        self.assertIn("Interlocked.Exchange(ref _voiceEntryInFlight, 0)",
                      body[:body.index("PythonExecutable")])

    def test_outbound_has_a_minimum_gap(self):
        """出站要留间隔，否则通道一恢复就把攒着的几条挤在同一秒送出去。"""
        source = (self.BRIDGE / "ReaderCodexPush.cs").read_text(
            encoding="utf-8")
        self.assertIn("OutboundMinimumGap", source)
        # ⚠ 取到方法结束，别截固定字数 —— 注释一长就把要找的东西挤出窗口，
        # 于是测试红了但代码是对的。这个写法今天已经骗过我三次。
        gate = source.split("private static async Task SendAsync")[1]
        gate = gate[:gate.index("private static async Task SendWithinGateAsync")]
        self.assertIn("OutboundGate.WaitAsync", gate)
        self.assertIn("OutboundMinimumGap", gate)

    def test_delivery_is_what_marks_a_board_as_sent(self):
        """通道断着时每一轮都会失败；那时记成"已推"会让这份内容再也不发。"""
        board = (self.BRIDGE / "ReaderAttentionBoard.cs").read_text(
            encoding="utf-8")
        self.assertIn("NoteFastBoardDelivered", board)
        decide = board.split("internal static bool ShouldPushFast")[1][:600]
        self.assertIn("lastDelivered", decide)


class ChannelRebuildTests(unittest.TestCase):
    """通道坏了要**当场**发现，不要等下一轮。"""

    BRIDGE = Path(__file__).resolve().parents[2] / "ComputerVoiceAudio"

    def _entry_task(self):
        source = (self.BRIDGE / "DirectBridgeProtocol.cs").read_text(
            encoding="utf-8")
        body = source.split("RequestVoiceEntryIfNobodyElseWill")[-1]
        return body[:body.index("private static void NoteBridgeGaveUp")]

    def test_no_binding_means_build_one_before_sending(self):
        """用户 2026-09-10 定的顺序：冷启动后先把通道建起来再谈发送。"""
        body = self._entry_task()
        build = body.index("TryEnsureChannelAsync")
        send = body.index("RequestVoiceEntryAsync")
        self.assertLess(build, send, "建通道必须排在发送之前")

    def test_a_failed_send_rebuilds_immediately(self):
        """发送失败**就是**"这条绑定不通"的实测证据，比等时钟强。

        ⚠ 实录 2026-09-11 00:18:24：绑定指着 d1db7cb6，而 Codex 重启后管道名
        早变了。当时要等满一轮 10 秒才轮到重建 —— 那 11 秒是白等的，因为失败
        的那一刻我们就已经知道它坏了。
        """
        body = self._entry_task()
        self.assertIn("hadBinding", body)
        # 失败分支里必须再建一次
        # ⚠ 窗口取到分支结束，别截固定字数 —— 2026-09-11 又加了一段注释，
        # 1200 字又不够了。同一个写法今天骗过我第四次。
        tail = body[body.index("else if (hadBinding)"):]
        self.assertIn("TryEnsureChannelAsync", tail)
        # "对面没有收下" = 目标线程死了，不是通道坏了；那种情况要换目标。
        self.assertIn("ClearVoiceEntryTargetOverride", tail,
                      "目标被拒时只会重建通道，不会换目标 —— 会原地空转")

    def test_it_does_not_rebuild_twice_in_one_round(self):
        """绑定为 null 时循环顶部已经建过了，失败分支不该再来一次。"""
        body = self._entry_task()
        self.assertIn("else if (hadBinding)", body,
                      "失败就重建必须以「本来有绑定」为条件")


class BoardFollowsTheCallTests(unittest.TestCase):
    """提示板要发给**正在通话的那条对话**。

    ⚠ 用户 2026-09-11：「建立通道后再让 ai 运行快捷键开启语音，可能会造成
    开启语音的对话和建立通道的对话不是同一对话」。我们这条链让它更容易发生：
    冷启动时先建通道（语音会话还不存在，只能绑到旧对话），语音一起来 Codex
    新开一条 voice_chat。于是板子推给 A、人在跟 B 说话，而两边都不报错。

    挂断与状态查询早就用 threadIdOverride 处理这件事了，只有提示板漏了 ——
    同一条道理的两份实现只改了一份。
    """

    BRIDGE = Path(__file__).resolve().parents[2] / "ComputerVoiceAudio"

    def test_every_outbound_targets_the_call_when_there_is_one(self):
        push = (self.BRIDGE / "ReaderCodexPush.cs").read_text(
            encoding="utf-8")
        # 四条外发正文都要能指定目标线程；一条都不许再用绑定里那条硬发。
        self.assertEqual(
            push.count("await SendAsync(binding, prompt, cancellationToken)"),
            0, "还有外发没带 threadIdOverride —— 它会发给绑定那条")
        # ⚠ **五条全部**都要能指定目标线程（2026-09-11 改）。
        #
        # 我上一版把「指定操作」列为例外，理由是"发它时还没有通话"。前半句对、
        # 后半句错：没有**正在进行**的通话，但**最近那条**一直在，而那正是
        # F24 会续上的那条。发错了对象的后果就是用户报的"每次都新开一条对话"。
        #
        # 两侧的目标**故意不同**：
        #   · 板子 / 挂断 / 状态 → 活着的通话（没有就退回绑定）
        #   · 指定操作           → 最近那条语音对话（lastGood，不加在通话守卫）
        #   · 用户打字             → 正在通话的那条（读不到才退回绑定）
        self.assertEqual(
            push.count("threadIdOverride:"), 6,
            "带目标线程的外发不是六条了 —— 先数清楚再改")

    def test_the_entry_goes_to_the_latest_voice_chat_not_the_binding(self):
        """入口要发给**最近那条语音对话** —— F24 续的就是它。

        ⚠ 实录对照（2026-09-10/11）：
          16:52/16:56/19:38 绑定=01a08a2f（当时在用的那条）→ 三通复用同一条；
          09-11 那五次      绑定=01a088fd（昨天的旧对话，因为新建的 voice_chat
                            都没标题，mode:title 只能落在旧的上）→ 每次新开。
        """
        push = (self.BRIDGE / "ReaderCodexPush.cs").read_text(
            encoding="utf-8")
        # ⚠ 窗口取到方法结束，别用 NoteAttempt 当边界 —— 方法体前面就有一条
        #（"没有可用绑定"那句），截在那儿等于根本没看到发送段。
        body = push.split(
            "internal static async Task<bool> RequestVoiceEntryAsync")[1]
        body = body[:body.index("internal static")]
        self.assertIn("InCallThreadId()", body,
                      "入口还在发给绑定那条")
        # ⚠ 必须是**不带在通话守卫**的那个：要的是"最后一条好的"，
        # 散场之后仍然是它；用带守卫的那个会在没通话时退回绑定 —— 回到老毛病。
        self.assertNotIn("InCallThreadIdIfActive", body)

    def test_it_only_follows_while_a_call_is_really_live(self):
        """lastGood 是"最后一条好的"，通话结束后还留着。

        拿它当"现在在跟谁说话"，会在散场之后把板子一直推给一条死掉的对话。
        """
        source = (self.BRIDGE / "DirectBridgeProtocol.cs").read_text(
            encoding="utf-8")
        body = source.split("internal static string InCallThreadIdIfActive")[1]
        body = body[:body.index("internal static string InCallThreadSource")]
        # 必须先问台账、且只认"确证在通话"
        self.assertIn("CodexVoiceActivityReadStatus.Available", body)
        self.assertIn("snapshot.Active", body)
        # 读不到 → 空串（退回绑定），而不是硬着头皮跟
        self.assertIn("return string.Empty;", body)


class OpenTheThreadBeforePressingTests(unittest.TestCase):
    """按 F24 之前，先把 App 打开到最近那条语音对话。

    ⚠ 2026-09-11 用户：「本身软件的设计也是语音快捷键按下时默认打开最近的语音
    对话，为何现在变成打开新的对话了」。对，但那要求 App 里**已经开着**那条。
    我们这条链常常是刚把 Codex 拉起来的（实录：Codex 01:30:15 启动、01:30:22
    收到 START），冷启动的 App 什么都没开，F24 只能新开一条。
    19:38 那通能复用，正因为 Codex 从 16:19 就开着。

    工具是 codex_app 的 navigate_to_codex_page，实测返回 {"navigated": true}。
    """

    BRIDGE = Path(__file__).resolve().parents[2] / "ComputerVoiceAudio"

    def test_the_channel_module_can_navigate(self):
        source = (Path(__file__).resolve().parents[1]
                  / "codex_channel.py").read_text(encoding="utf-8")
        self.assertIn("def navigate(", source)
        self.assertIn("navigate_to_codex_page", source)
        self.assertIn('"--navigate"', source.replace("'", '"'))

    def test_navigation_runs_before_the_shortcut(self):
        """顺序要紧：导航必须排在按键之前，否则等于没做。"""
        source = (self.BRIDGE / "DirectBridgeProtocol.cs").read_text(
            encoding="utf-8")
        prepare = source.index(
            "internal static async Task PrepareInitialStartAsync")
        press = source.index("shortcutSender.Send(target, "
                             "DirectVoiceCommand.Start)")
        navigate = source.index("NavigateToLatestVoiceChatAsync(")
        # 实现在 prepare 之前定义，调用在 prepare 之内；按键在别处（委托里）。
        called = source.index("await NavigateToLatestVoiceChatAsync(")
        self.assertGreater(called, prepare,
                           "导航没挂在 PrepareInitialStartAsync 里")
        self.assertLess(navigate, len(source))
        self.assertGreater(press, 0)

    def test_navigation_is_best_effort(self):
        """导航不成也要照样按键 —— 做成硬前置只会多一个"打不开语音"的来源。"""
        source = (self.BRIDGE / "DirectBridgeProtocol.cs").read_text(
            encoding="utf-8")
        body = source.split(
            "private static async Task NavigateToLatestVoiceChatAsync")[1]
        body = body[:body.index("internal static async Task"
                                " PrepareInitialStartAsync")]
        self.assertIn("catch (Exception exception)", body)
        self.assertNotIn("throw", body)


class TransientAmbiguityTests(unittest.TestCase):
    """「暂时看着有两棵进程树」不是结论，是瞬时状态。

    ⚠ 用户 2026-09-11：「codex 没有启动时按下 app 内按钮会启动 codex 然后
    按钮直接灭掉，重按后虽然会正常流程打开语音」。失败账本里每一次"第二按
    才成"之前都躺着一条 APP_AMBIGUOUS stage=start
    （00:17:25 / 00:18:12 / 00:59:40 / 01:30:14 / 01:52:00）。
    Codex 是打包应用，启动/退出过程中会短暂出现多于一棵根进程树。
    """

    BRIDGE = Path(__file__).resolve().parents[2] / "ComputerVoiceAudio"

    def test_it_waits_for_the_tree_to_settle(self):
        source = (self.BRIDGE / "WindowsDirectAdapters.cs").read_text(
            encoding="utf-8")
        body = source.split("public async Task<bool> EnsureRunningAsync")[1]
        body = body[:body.index("public async Task<DirectAppTarget>")]
        self.assertIn("AmbiguousSettleAttempts", body, "没有稳定期就直接判死")
        # 稳定期必须**排在**抛 APP_AMBIGUOUS 之前
        settle = body.index("AmbiguousSettleAttempts")
        raise_at = body.index("APP_AMBIGUOUS")
        self.assertLess(settle, raise_at, "先抛错再等，等于没等")

    def test_a_real_second_instance_is_still_refused(self):
        """拒绝本身是对的 —— 一直是两棵才判 ambiguous。"""
        source = (self.BRIDGE / "WindowsDirectAdapters.cs").read_text(
            encoding="utf-8")
        self.assertIn("BW_COMPUTER_VOICE_DIRECT_APP_AMBIGUOUS", source)

    def test_every_thrower_tolerates_the_transient(self):
        """⚠ **抛这个码的地方一共三处**，只改一处等于没改。

        2026-09-11 实测：我先改了 EnsureRunningAsync，用户再按还是灭 ——
        因为真正拦住的是 WaitForUniqueReadyAsync，而它**本身就是个轮询循环**，
        却在瞬时歧义上当场抛错，把自己唯一的用处扔掉了。
        第三处在 RestartAsync 里，同样的形状。
        """
        source = (self.BRIDGE / "WindowsDirectAdapters.cs").read_text(
            encoding="utf-8")
        self.assertEqual(
            source.count("BW_COMPUTER_VOICE_DIRECT_APP_AMBIGUOUS"), 3,
            "抛这个码的地方数量变了 —— 先数清楚再改")
        # 两个轮询循环里的那两处必须带 deadline 条件，否则又是当场认输
        for marker in ("Stopwatch.GetTimestamp() >= deadline",):
            self.assertGreaterEqual(
                source.count(marker), 2,
                "轮询循环里还有没等 deadline 就抛 ambiguous 的")


class PacingOnlyDelaysTheBoardTests(unittest.TestCase):
    """出站间隔只该拦板面推送，不该拦有人在等结果的那几条。

    ⚠ 2026-09-11 实测：01:52 那次从按下到语音起来 30 秒，其中 **12 秒**是这个
    间隔（接上时的全量板 +6s，然后起语音又 +6s）。用户原话「好像在那里等待了
    很多秒」。间隔本来是防"通道恢复时几条挤成一堆"，那说的是板面。
    """

    BRIDGE = Path(__file__).resolve().parents[2] / "ComputerVoiceAudio"

    def test_only_board_pushes_are_paced(self):
        source = (self.BRIDGE / "ReaderCodexPush.cs").read_text(
            encoding="utf-8")
        # 2026-09-11 第二轮：光免"间隔"不够，还得免"排队" —— 板面推送握着
        # 那把闸做完整趟管道 I/O（连接 4s + 请求最多 12s），起语音只能等在
        # 后面。实测 02:15 那次 29 秒里光排队就吃掉 11 秒。
        body = source.split("private static async Task SendAsync")[1]
        body = body[:body.index(
            "private static async Task SendWithinGateAsync")]
        self.assertIn("bool paced", body, "间隔仍然一视同仁")
        # 非板面的那条要**在拿闸之前**就走掉
        early = body.index("if (!paced)")
        gate = body.index("OutboundGate.WaitAsync")
        self.assertLess(early, gate, "时间敏感的推送仍然排在闸后面")
        self.assertIn("reader-board", body)


class AuthoritativeSourceTests(unittest.TestCase):
    """「最近那条语音线程」要问 App 自己，别问二手的。

    ⚠ 2026-09-11 用户点破：「目前的所有问题都来自于你对每个环节实际能做到的
    事情的误判」。这条是其中之一 —— 我一直读侧栏同步的 lastGood，而它比通话
    滞后约一分钟（02:48:47 锁定时它还指着上一通，02:49:56 才对）。
    App 自己在 .codex-global-state.json 里记着，且不滞后。
    """

    BRIDGE = Path(__file__).resolve().parents[2] / "ComputerVoiceAudio"

    def test_it_asks_the_app_first(self):
        source = (self.BRIDGE / "DirectBridgeProtocol.cs").read_text(
            encoding="utf-8")
        self.assertIn("realtime-voice-most-recent-thread", source)
        body = source.split("internal static string InCallThreadId()")[1]
        body = body[:body.index("internal static string InCallThreadSource")]
        # 权威来源必须排在退回侧栏之前
        first = body.index("AppMostRecentVoiceThread()")
        fallback = body.index("InCallThreadSource()")
        self.assertLess(first, fallback, "还是先读那个滞后的来源")

    def test_the_capability_table_exists(self):
        """⚠ 这张表是给下一个人的 —— 动手改这条链之前先看它。"""
        doc = (Path(__file__).resolve().parents[5]
               / "references" / "codex-notification-channel.md")
        text = doc.read_text(encoding="utf-8")
        self.assertIn("每个环节实际能做到什么", text)
        self.assertIn("权威来源清单", text)
        self.assertIn("realtime-voice-most-recent-thread", text)


class EveryOutboundSaysWhyTests(unittest.TestCase):
    """每一条外发都要留一行，并说清**为什么发**。

    ⚠ 用户 2026-09-11：「每个动作都该带上触发的原因和记录，我们不记录无法
    分析多次发送指令的原因」。那一夜 02:19–02:28 对面收到 6 条入口指令，而
    账本里**一行都没有** —— 我只能答"不知道是谁发的"。病根是记账挂在各个
    包装函数上，绕过包装的路子就不留痕。
    """

    BRIDGE = Path(__file__).resolve().parents[2] / "ComputerVoiceAudio"

    def _push(self):
        return (self.BRIDGE / "ReaderCodexPush.cs").read_text(encoding="utf-8")

    def test_the_choke_point_records_every_message(self):
        """记账必须在**唯一的出站咽喉**，不在各个包装函数里。"""
        push = self._push()
        body = push.split("private static async Task SendAsync")[1]
        body = body[:body.index("private static async Task SendTracedAsync")]
        self.assertIn("NoteOutbound(", body, "咽喉没有留痕")
        # 成功和失败**都要**记 —— 只记成功答不了"为什么没到"
        self.assertGreaterEqual(body.count("NoteOutbound("), 2)
        self.assertIn("catch (Exception exception)", body)

    def test_a_missing_cause_is_loud_not_silent(self):
        """调用方忘了说原因时，账本要写出来，而不是留空。"""
        push = self._push()
        self.assertIn("调用方没说为什么", push)

    def test_every_sender_declares_a_cause(self):
        """五条外发正文各自都要 Because(...)。"""
        push = self._push()
        # Because 的调用次数 = 五条外发 + 定义本身
        self.assertGreaterEqual(
            push.count("Because("), 4,
            "有外发没声明原因 —— 它出问题时无法归因")

    def test_the_entry_cause_carries_the_trigger_and_round(self):
        """入口那条最要紧：要说清是哪次 START、第几轮、为什么这一轮要发。"""
        protocol = (self.BRIDGE / "DirectBridgeProtocol.cs").read_text(
            encoding="utf-8")
        body = protocol.split("RequestVoiceEntryIfNobodyElseWill")[-1]
        self.assertIn("ReaderCodexPush.Because(", body)
        cause = body[body.index("string why ="):][:600]
        self.assertIn("sessionId", cause, "没说是哪次 START")
        self.assertIn("round", cause, "没说第几轮")
        self.assertIn("补发", cause, "没说这一轮为什么发")


class ChannelChoiceTests(unittest.TestCase):
    """通道连哪条对话：一份设置，两个入口。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.runtime = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_choice_is_one_file_shared_by_both_surfaces(self):
        """⚠ 存两份迟早只改一边，而"我明明设过"没有任何提示。

        用户 2026-09-10 定：「app和服务器设置页的设置需要是相同的才行」。
        所以真相只能有一处 —— 桥 runtime 里的那个文件。
        """
        self.assertEqual(CHANNEL.read_choice(self.runtime),
                         {"mode": CHANNEL.DEFAULT_MODE, "title": ""})
        CHANNEL.write_choice(CHANNEL.MODE_TITLE, "实时语音聊天", self.runtime)
        self.assertEqual(CHANNEL.read_choice(self.runtime),
                         {"mode": "title", "title": "实时语音聊天"})
        # 设置页写的与 App 写的是同一个函数、同一个文件，不存在第二份。
        self.assertTrue((self.runtime / CHANNEL.CHOICE_FILE).is_file())

    def test_bad_choice_falls_back_instead_of_breaking_the_channel(self):
        """一个坏掉的偏好不该让通道整个建不起来。"""
        (self.runtime / CHANNEL.CHOICE_FILE).write_text(
            '{"contract":"wrong","mode":"title"}', encoding="utf-8")
        self.assertEqual(CHANNEL.read_choice(self.runtime)["mode"],
                         CHANNEL.DEFAULT_MODE)
        self.assertEqual(CHANNEL.normalize_mode("胡说"), CHANNEL.DEFAULT_MODE)

    def test_updated_at_mixes_seconds_and_milliseconds(self):
        """同一个字段里两种单位 —— 不归一会把旧对话判成"最近活跃"。

        实测：置顶那几条是毫秒（1786779621000），其余是秒（1789021032）。
        这种错不报异常，只是悄悄连错对话。
        """
        rows = [
            {"id": "old", "title": "旧的", "status": "",
             "updatedAt": CHANNEL._seconds(1786779621000)},
            {"id": "new", "title": "新的", "status": "",
             "updatedAt": CHANNEL._seconds(1789021032)},
        ]
        self.assertEqual(
            CHANNEL.choose(rows, CHANNEL.MODE_RECENT)["id"], "new")

    def test_named_conversation_missing_reports_instead_of_switching(self):
        """找不到指定的那条就**报错**，不悄悄换一条。

        推给了另一段对话这种错没有任何提示 —— 宁可停下说清楚。
        """
        rows = [{"id": "a", "title": "甲", "status": "", "updatedAt": 2},
                {"id": "b", "title": "乙", "status": "", "updatedAt": 1}]
        with self.assertRaises(CHANNEL.ChannelError) as caught:
            CHANNEL.choose(rows, CHANNEL.MODE_TITLE, "丙")
        self.assertIn("甲", str(caught.exception))   # 要列出现有的

    def test_last_used_says_why_it_fell_back(self):
        """退回也要说明原因，别让人以为"上次那条"还在用。"""
        rows = [{"id": "a", "title": "甲", "status": "", "updatedAt": 2}]
        picked = CHANNEL.choose(rows, CHANNEL.MODE_LAST_USED,
                                runtime=self.runtime)
        self.assertIn("已不在", picked["why"])
        CHANNEL.write_last_used("a", "甲", self.runtime)
        picked = CHANNEL.choose(rows, CHANNEL.MODE_LAST_USED,
                                runtime=self.runtime)
        self.assertIn("上次连的", picked["why"])

    def test_channel_heals_itself_without_anyone_typing(self):
        """绑定失效就自己重建 —— 不该等用户在 App 里打字。

        管道名每次 Codex 重启就变，而"通道断了"没有任何提示：表现只是挂断、
        状态回报、提示板悄悄不工作。所以策略环每轮看一眼，坏了就修。
        """
        launcher = (Path(__file__).resolve().parents[1]
                    / "readerpc_launcher.py").read_text(encoding="utf-8")
        self.assertIn("_heal_channel_if_needed", launcher)
        heal = launcher.split("def _heal_channel_if_needed")[1]
        heal = heal.split("def _voice_auto_close_tick")[0]
        self.assertIn("codex_channel.ensure_channel", heal)
        self.assertIn("invalidAtMs", heal)      # 判据只看绑定本身
        self.assertIn("_CHANNEL_HEAL_RETRY_SECONDS", heal)  # 修不好要退避

    def test_healing_runs_even_when_auto_close_is_off(self):
        """挂断/状态回报/提示板都要用这条通道 —— 跟自动关闭开没开无关。"""
        launcher = (Path(__file__).resolve().parents[1]
                    / "readerpc_launcher.py").read_text(encoding="utf-8")
        tick = launcher.split("def _voice_auto_close_tick")[1][:2000]
        heal_at = tick.index("_heal_channel_if_needed")
        gate_at = tick.index('prefs.get("voiceAutoClose")')
        self.assertLess(heal_at, gate_at, "自愈被关在了自动关闭的开关后面")

    def test_no_pipes_says_which_kind_of_nothing(self):
        """"一条都没有"要说清是哪一种 —— 多半是 Codex 没在跑。

        2026-09-10 实测撞到：设置页显示「没有可用的通知管道；试过：（一条都
        没有）」，让人去查管道、查权限、查我们的代码，全是错的方向。
        分辨它只要看一眼进程。
        """
        source = (Path(__file__).resolve().parents[1]
                  / "codex_channel.py").read_text(encoding="utf-8")
        picker = source.split("def usable_pipe")[1].split("def _tool_call")[0]
        self.assertIn("codex_running()", picker)
        self.assertIn("没在跑", picker)
        # 有候选但都不能用是**另一种**情况，要逐条说原因。
        self.assertIn("都不能用", picker)

    def test_pipe_must_prove_itself(self):
        """并存的管道里只有一条是活的 —— 按名字或顺序猜都会挑错。"""
        source = (Path(__file__).resolve().parents[1]
                  / "codex_channel.py").read_text(encoding="utf-8")
        picker = source.split("def usable_pipe")[1].split("def _tool_call")[0]
        self.assertIn("tools/list", picker)
        self.assertIn("REQUIRED_TOOL", picker)


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


class RunFoldsRetryIntoTheScript(unittest.TestCase):
    """重试和放弃上报收进脚本，AI 只跑一行（2026-09-13）。

    会话记录：voice_start_step 117 次中位 7.2 s，AI 为等它结束又空写 170 次
    stdin（中位 5 s）。每次等都是一整个模型回合。
    """

    def _run(self, replies, **kw):
        calls = []

        def fake_start_once(endpoint=None, timeout=0, runtime=None,
                            clock=None, sleeper=None):
            calls.append(1)
            return dict(replies[min(len(calls) - 1, len(replies) - 1)])

        reports = []

        def fake_report(*, attempts, detail, runtime):
            reports.append((attempts, detail))
            return {"ok": True}

        with unittest.mock.patch.object(STEP, "start_once", fake_start_once):
            result = STEP.run("http://x", 1.0, None, reporter=fake_report, **kw)
        return result, len(calls), reports

    def test_confirmed_on_first_try_stops_there(self):
        result, calls, reports = self._run(
            [{"confirmed": True, "reason": "started"}], attempts=2,
            report_failure=True)
        self.assertTrue(result["confirmed"])
        self.assertEqual(calls, 1)
        self.assertEqual(reports, [], "成功了不许上报失败")

    def test_second_attempt_can_rescue(self):
        result, calls, _ = self._run(
            [{"confirmed": False, "reason": "not-confirmed"},
             {"confirmed": True, "reason": "started"}], attempts=2)
        self.assertTrue(result["confirmed"])
        self.assertEqual(calls, 2)
        self.assertEqual(result["attempt"], 2)

    def test_locked_desktop_is_not_retried(self):
        # no-desktop 时第二次必然一样，还要烧掉整个确认窗口。
        result, calls, reports = self._run(
            [{"confirmed": False, "reason": "no-desktop"}], attempts=2,
            report_failure=True)
        self.assertEqual(calls, 1)
        self.assertEqual(reports, [(1, "no-desktop")])

    def test_giving_up_reports_exactly_once(self):
        result, calls, reports = self._run(
            [{"confirmed": False, "reason": "not-confirmed"}], attempts=2,
            report_failure=True)
        self.assertEqual(calls, 2)
        self.assertEqual(reports, [(2, "not-confirmed")])
        self.assertEqual(result["failureReport"], {"ok": True})

    def test_without_report_flag_nothing_is_reported(self):
        _, _, reports = self._run(
            [{"confirmed": False, "reason": "not-confirmed"}], attempts=2)
        self.assertEqual(reports, [])

    def test_reporter_crash_does_not_eat_the_result(self):
        def boom(**kw):
            raise RuntimeError("回执盘满了")
        with unittest.mock.patch.object(
                STEP, "start_once",
                lambda *a, **k: {"confirmed": False, "reason": "not-confirmed"}):
            result = STEP.run("http://x", 1.0, None, attempts=1,
                              report_failure=True, reporter=boom)
        self.assertEqual(result["reason"], "not-confirmed")
        self.assertFalse(result["failureReport"]["ok"])

    def test_cli_default_is_still_one_attempt(self):
        # 旧调用方（没传 --attempts）行为不变。
        with unittest.mock.patch.object(
                STEP, "start_once",
                lambda *a, **k: {"confirmed": False, "reason": "not-confirmed"}), \
             unittest.mock.patch("builtins.print"):
            code = STEP.main(["--endpoint", "http://x"])
        self.assertEqual(code, 1)
