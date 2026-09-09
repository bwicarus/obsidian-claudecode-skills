# -*- coding: utf-8 -*-
"""situation_actions 的测试，以及它与触发器、后台任务闸的联动。

用户 2026-09-08 说明思路时补的要求：

> 不只是需要及时的获取数据，还牵扯到这些数据变化为某个状态时自动触发的各种
> 行为能力，比如触发某个或者复合条件后停下或者开始某些功能

守四条命门：

① **动作名写错要在注册时就报错**，不是等触发那一刻。存下一条动作永远不生效的
   规则，表现是"规则响了、功能没动"，而链路上没有一处会喊。
② **白名单是安全边界**。看门狗和引导任务永远不可关 —— 关掉看门狗会把一次崩溃
   变成永久停摆，而排查的人不会想到去翻计划任务。
③ **按住后台任务必须真的拦住闸**，而且必须会自动过期。没有上限的"静音"
   会变成永久停摆，且没人记得去解除。
④ **动作失败不许静默**。纯动作规则做成了可以不打扰人，但失败一定要留通知。
"""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import time
import os
import unittest

DESKTOP = Path(__file__).resolve().parent.parent
if str(DESKTOP) not in sys.path:
    sys.path.insert(0, str(DESKTOP))
# 闸在主项目树里（两棵树互相 import 不了，只能靠文件名对接）
GATE_DIR = DESKTOP.parents[3] / "scripts" / "lib"
if str(GATE_DIR) not in sys.path:
    sys.path.insert(0, str(GATE_DIR))

import readerpc_gate  # noqa: E402
import situation_actions as actions  # noqa: E402
import situation_triggers as triggers  # noqa: E402


class ActionVocabularyTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    # ── ① 名字写错就报错
    def test_unknown_action_is_refused_with_the_list(self):
        with self.assertRaises(actions.ActionError) as caught:
            actions.validate("backgorund.hold")
        self.assertIn("background.hold", str(caught.exception))

    # ── ② 白名单是安全边界
    def test_watchdog_can_never_be_disabled(self):
        # 这条是**故意**写死的：看门狗关掉，一次崩溃就变成永久停摆。
        self.assertNotIn("BW ReaderPC Watchdog", actions.TASK_WHITELIST)
        self.assertNotIn("BW Computer Voice Setup", actions.TASK_WHITELIST)
        for name in ("BW ReaderPC Watchdog", "Windows Update", ""):
            with self.subTest(task=name):
                with self.assertRaises(actions.ActionError):
                    actions.validate("task.disable", {"target": name})

    def test_whitelisted_task_passes_validation(self):
        checked = actions.validate(
            "task.disable", {"target": actions.TASK_WHITELIST[0]})
        self.assertEqual(checked["action"], "task.disable")

    # ── ③ 按住要真拦住闸，而且会过期
    def test_hold_blocks_the_gate_and_resume_releases_it(self):
        (self.root / readerpc_gate.STATUS_NAME).write_text(
            json.dumps({"updatedAtEpochMs": int(time.time() * 1000)}),
            encoding="utf-8")
        allowed, _why = readerpc_gate.readerpc_active(self.root)
        self.assertTrue(allowed, "心跳新鲜时本该放行")

        actions.run("background.hold", {"minutes": 30}, self.root)
        allowed, why = readerpc_gate.readerpc_active(self.root)
        self.assertFalse(allowed)
        self.assertIn("按住", why)

        actions.run("background.resume", {}, self.root)
        allowed, _why = readerpc_gate.readerpc_active(self.root)
        self.assertTrue(allowed)

    def test_expired_hold_releases_itself(self):
        (self.root / readerpc_gate.STATUS_NAME).write_text(
            json.dumps({"updatedAtEpochMs": int(time.time() * 1000)}),
            encoding="utf-8")
        (self.root / actions.BACKGROUND_HOLD_FILE_NAME).write_text(
            json.dumps({"untilMs": int(time.time() * 1000) - 1000}),
            encoding="utf-8")
        allowed, _why = readerpc_gate.readerpc_active(self.root)
        self.assertTrue(allowed, "过期的按住必须自动失效，不能等谁来清理")
        self.assertIsNone(actions.background_hold_until_ms(self.root))

    def test_hold_has_an_upper_bound(self):
        for minutes in (0, 0.5, actions.BACKGROUND_HOLD_MAX_MINUTES + 1):
            with self.subTest(minutes=minutes):
                with self.assertRaises(actions.ActionError):
                    actions.run("background.hold", {"minutes": minutes},
                                self.root)

    def test_gate_and_actions_agree_on_the_file_name(self):
        # 两棵 git 树只能靠文件名对接，而不能靠人记住两处要一致。
        self.assertEqual(readerpc_gate.BACKGROUND_HOLD_NAME,
                         actions.BACKGROUND_HOLD_FILE_NAME)

    def test_broken_hold_file_does_not_wedge_the_gate(self):
        (self.root / readerpc_gate.STATUS_NAME).write_text(
            json.dumps({"updatedAtEpochMs": int(time.time() * 1000)}),
            encoding="utf-8")
        (self.root / actions.BACKGROUND_HOLD_FILE_NAME).write_text(
            "{ 这不是 JSON", encoding="utf-8")
        allowed, _why = readerpc_gate.readerpc_active(self.root)
        # 读不出来就当没按住 —— 守卫是省资源的，不是制造"任务神秘不执行"。
        self.assertTrue(allowed)

    def test_every_action_declares_why_it_takes_effect(self):
        # `why` 不是注释而是准入条件：填不出"它凭什么生效"的动作不该存在，
        # 因为一个"校验全过、其实什么都没发生"的动作比没有这个动作糟得多。
        for name, spec in actions.ACTIONS.items():
            with self.subTest(action=name):
                self.assertTrue(spec["summary"])
                self.assertTrue(spec["params"])
                self.assertGreater(len(spec["why"]), 10)


class TriggerActionTests(unittest.TestCase):
    """触发器带动作的三种形态。"""

    def setUp(self):
        base = Path(tempfile.mkdtemp(prefix="trgact-"))
        self.root = base / "root"
        self.runtime = base / "runtime"
        self.root.mkdir()
        self.runtime.mkdir()
        self.place("out")

    def place(self, state: str) -> None:
        (self.runtime / "current-place.json").write_text(json.dumps(
            {"state": state, "observedAtUtcMs": int(time.time() * 1000)}),
            encoding="utf-8")

    def notifications(self) -> list[dict]:
        path = self.root / "notifications.json"
        if not path.is_file():
            return []
        return json.loads(path.read_text(encoding="utf-8"))["items"]

    # ── ① 注册时校验动作
    def test_bad_action_is_refused_at_registration(self):
        with self.assertRaises(triggers.TriggerError) as caught:
            triggers.add(self.root, runtime=self.runtime, name="错的",
                         when={"place": "home"},
                         then={"title": "x", "action": "backgorund.hold"})
        self.assertIn("background.hold", str(caught.exception))

    def test_bad_action_target_is_refused_at_registration(self):
        with self.assertRaises(triggers.TriggerError):
            triggers.add(self.root, runtime=self.runtime, name="想关看门狗",
                         when={"place": "home"},
                         then={"title": "x", "action": "task.disable",
                               "actionParams": {"target": "BW ReaderPC Watchdog"}})

    def test_then_needs_title_or_action(self):
        with self.assertRaises(triggers.TriggerError):
            triggers.add(self.root, runtime=self.runtime, name="空的",
                         when={"place": "home"}, then={"body": "只有正文"})

    # ── 纯动作规则
    def test_pure_action_rule_does_the_thing_without_a_notification(self):
        triggers.add(self.root, runtime=self.runtime, name="回家就静一会",
                     when={"place": "home"},
                     then={"action": "background.hold",
                           "actionParams": {"minutes": 45}})
        self.place("home")
        result = triggers.evaluate(self.root, self.runtime)
        self.assertEqual([one["name"] for one in result["fired"]],
                         ["回家就静一会"])
        self.assertIsNotNone(actions.background_hold_until_ms(self.root))
        # 这条规则只想做事 —— 不该占用户一条通知。
        self.assertEqual(self.notifications(), [])

    def test_action_plus_title_does_both(self):
        triggers.add(self.root, runtime=self.runtime, name="做了并说一声",
                     when={"place": "home"},
                     then={"title": "已经帮你压下后台了",
                           "action": "background.hold"})
        self.place("home")
        triggers.evaluate(self.root, self.runtime)
        items = self.notifications()
        self.assertEqual(len(items), 1)
        # 正文要说清到底动了什么，别让人猜。
        self.assertIn("background.hold", items[0]["body"])

    # ── ④ 动作失败不许静默
    def test_failed_action_still_leaves_a_notification(self):
        triggers.add(self.root, runtime=self.runtime, name="注定失败",
                     when={"place": "home"},
                     then={"action": "background.hold"})
        original = actions.run

        def explode(name, params=None, root=None):
            raise actions.ActionError("故意做不成")

        actions.run = explode
        try:
            self.place("home")
            triggers.evaluate(self.root, self.runtime)
        finally:
            actions.run = original
        items = self.notifications()
        # 纯动作规则做成了可以安静，**失败一定要留通知** ——
        # 静默失败的动作等于没有这个动作。
        self.assertEqual(len(items), 1)
        self.assertIn("故意做不成", items[0]["body"])
        table = triggers.load(self.root)
        self.assertIn("lastActionError", table["triggers"][0])

    def test_pure_action_does_not_rerun_every_round(self):
        # "silent" 被当成失败的话，lastMatch 会被退回假，于是动作每轮重做一次。
        triggers.add(self.root, runtime=self.runtime, name="只做一次",
                     when={"place": "home"},
                     then={"action": "background.hold"})
        self.place("home")
        first = triggers.evaluate(self.root, self.runtime)
        second = triggers.evaluate(self.root, self.runtime)
        self.assertEqual(len(first["fired"]), 1)
        self.assertEqual(second["fired"], [])
        self.assertEqual(triggers.load(self.root)["triggers"][0]["fireCount"], 1)


if __name__ == "__main__":
    unittest.main()


class AnkiActionTests(unittest.TestCase):
    """把 Anki 拉起来（2026-09-09）。

    为什么这值得一个动作：第一次评分**完全依赖** Anki 在跑 —— Reader 自己
    不排期。实测这台机器上 Anki 没开，于是 34 张草稿 0 回执、10 张新卡挂了
    18 天评不了分，而没有一处会喊。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_already_running_does_not_restart(self):
        # 重启会打断用户正在做的复习。项目里那套 force_restart 是给凌晨
        # 批处理用的，不该由情境触发替他决定。
        original = actions._anki_port_open
        launched = []
        actions._anki_port_open = lambda timeout=0.4: True
        popen = actions.subprocess.Popen
        actions.subprocess.Popen = lambda *a, **k: launched.append(a)
        try:
            result = actions.run("anki.start", {}, self.root)
        finally:
            actions._anki_port_open = original
            actions.subprocess.Popen = popen
        self.assertTrue(result["result"]["alreadyRunning"])
        self.assertEqual(launched, [], "已经在跑就不该再起一个")

    def test_start_is_minimized_and_does_not_steal_focus(self):
        """起 Anki 不该抢屏（2026-09-09 用户提出）。

        Anki 没有无界面模式 —— AnkiConnect 是插件、跑在 GUI 进程里；
        Pi 上那套 headless 是 Xvfb 虚拟屏，Windows 没有等价物。
        所以这里能做的就是最小化且不激活地起，别把用户从正在做的事上踢开。
        """
        if os.name != "nt":
            self.skipTest("wShowWindow 只有 Windows 有")
        original = actions._anki_port_open
        seen = {}
        calls = [0]

        def port(timeout=0.4):
            calls[0] += 1
            return calls[0] > 1        # 第一次没开(于是会去起)，之后开了

        def fake_popen(*args, **kwargs):
            seen.update(kwargs)
            return None

        popen = actions.subprocess.Popen
        exe = Path(__file__)
        which = actions.Path
        try:
            actions._anki_port_open = port
            actions.subprocess.Popen = fake_popen
            actions.Path = lambda *a, **k: (
                exe if a and "anki" in str(a[0]).lower() else which(*a, **k))
            result = actions.run("anki.start", {}, self.root)
        finally:
            actions._anki_port_open = original
            actions.subprocess.Popen = popen
            actions.Path = which
        self.assertFalse(result["result"]["alreadyRunning"])
        info = seen.get("startupinfo")
        self.assertIsNotNone(info, "Windows 上必须带 STARTUPINFO")
        self.assertTrue(info.dwFlags & actions.subprocess.STARTF_USESHOWWINDOW)
        self.assertEqual(info.wShowWindow, 7, "SW_SHOWMINNOACTIVE：最小化且不夺焦点")

    def test_port_never_opens_is_an_error_not_a_shrug(self):
        # "启动了就当成了"是最贵的那种交待：调用方以为好了，其实没有。
        original = actions._anki_port_open
        actions._anki_port_open = lambda timeout=0.4: False
        popen = actions.subprocess.Popen
        actions.subprocess.Popen = lambda *a, **k: None
        sleep = actions.time.sleep
        actions.time.sleep = lambda _s: None
        budget = actions.ANKI_START_TIMEOUT_SECONDS
        actions.ANKI_START_TIMEOUT_SECONDS = 0.2
        exe = Path(__file__)          # 随便一个存在的文件，绕开"找不到 Anki"
        which = actions.Path
        try:
            actions.Path = lambda *a, **k: exe if a and "anki" in str(a[0]).lower() else which(*a, **k)
            with self.assertRaises(actions.ActionError):
                actions.run("anki.start", {}, self.root)
        finally:
            actions._anki_port_open = original
            actions.subprocess.Popen = popen
            actions.time.sleep = sleep
            actions.ANKI_START_TIMEOUT_SECONDS = budget
            actions.Path = which

    def test_action_is_in_the_closed_table(self):
        checked = actions.validate("anki.start")
        self.assertEqual(checked["action"], "anki.start")
        self.assertIn("8765", actions.ACTIONS["anki.start"]["why"])
