"""一个问题只念一遍：语音委派那一轮不许后台再 voice_say。

用户 2026-09-21：「他一个问题回答我三次」。

实录（events.jsonl 13:28:26–13:29:48，同一个后台轮 01a0c238）：

    13:28:26  用户问「低中档是不是已经比 5.6 好多了」
    13:28:28  语音念「嗯,我查一下。」            ← 语音模型自己的垫场
    13:29:20  后台 agentMessage「对，通常思考档位越高…」
    13:29:34  语音念「通常思考档位越高…」        ← 上面那条被自动念出来
    13:29:34  后台调 voice_say「更准确地说…」
    13:29:48  语音念「更准确地说…」              ← 第三遍

**委派轮的 agentMessage 由 app-server 直接交给语音模型念**（runner 不经手 ——
全文件只有 say() 调 appendSpeech）。所以后台这一轮本来就有人替它开口，它再
voice_say 就是同一个问题多念一遍。

这里钉三条：
  ① 委派轮里后台的 voice_say 被拒，且**说清为什么**（模型要能据此改正）；
  ② 只拒后台 —— 定时投递 / 通知那些路没人替它们念，照旧放行；
  ③ 轮结束后闸自动放开，别把 voice_say 永久封死。
"""

import asyncio
import importlib.util
import sys
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNNER = HERE.parent / "voice_cli_runner.py"

_spec = importlib.util.spec_from_file_location("voice_cli_runner_say_gate", RUNNER)
vcr = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = vcr
_spec.loader.exec_module(vcr)


class _App:
    def __init__(self):
        self.calls = []

    async def call(self, method, params, timeout=None):
        self.calls.append((method, params))
        return {}


class SayGateTests(unittest.TestCase):
    def _runner(self, *, delegated: bool, turn: bool = True):
        r = object.__new__(vcr.Runner)
        r.settings = {}
        r.session_state = "connected"
        r.thread_id = "th-1"
        r.app = _App()
        r._turn = {"id": "01a0c238", "parts": []} if turn else None
        r._delegation_open_at = time.time() if delegated else 0.0
        r._last_app_error = (0.0, "")
        r.logged = []
        r.log = lambda kind, **d: r.logged.append((kind, d))
        r.mark_activity = lambda *a, **k: None
        return r

    def _say(self, r, text="随便一句", source=""):
        return asyncio.run(r.say(text, "none", source))

    def test_backend_say_is_refused_inside_a_delegated_turn(self):
        # ① 这就是用户听见第三遍的那一次。
        r = self._runner(delegated=True)
        out = self._say(r, source="backend")
        self.assertFalse(out["ok"])
        self.assertFalse(out["spoken"])
        self.assertEqual(out["reason"], "delegation-owns-speech")
        self.assertEqual(r.app.calls, [], "被拒就不该真的送进语音")
        self.assertIn("say_blocked", [k for k, _ in r.logged])

    def test_the_refusal_tells_the_model_what_to_do_instead(self):
        # 只说"被拒"没用 —— 模型下一轮还会再调。要说清它该怎么做。
        out = self._say(self._runner(delegated=True), source="backend")
        self.assertIn("会自动念", out["msg"])
        self.assertIn("只写一条", out["msg"])
        self.assertIn("通知", out["msg"])   # 什么时候才该用它

    def test_scheduled_and_notification_paths_are_not_touched(self):
        # ② 它们没人替它们念，拦下去就是真的没声音了。
        r = self._runner(delegated=True)
        out = self._say(r, source="")
        self.assertTrue(out["ok"])
        self.assertEqual([m for m, _ in r.app.calls],
                         ["thread/realtime/appendSpeech"])

    def test_backend_say_passes_when_nobody_delegated(self):
        # 后台主动找他（通知到期、定时提醒）——这一轮没人替它开口，必须放行。
        r = self._runner(delegated=False)
        out = self._say(r, source="backend")
        self.assertTrue(out["ok"])
        self.assertEqual([m for m, _ in r.app.calls],
                         ["thread/realtime/appendSpeech"])

    def test_gate_opens_again_once_the_turn_finishes(self):
        # ③ turn/completed 一到就放开；否则下一条通知会被这道闸吃掉。
        r = self._runner(delegated=True)
        self.assertFalse(self._say(r, source="backend")["ok"])
        r._backend_done_at = 0.0
        r._last_backend_turn_id = None
        vcr.Runner._finish_turn(r, {"id": "01a0c238"})
        self.assertEqual(r._delegation_open_at, 0.0)
        self.assertFalse(r._delegation_owns_speech())

    def test_gate_does_not_survive_a_lost_turn_completion(self):
        # app-server 掉线时 turn/completed 可能永远不来。兜底时长到了就放开，
        # 不能让 voice_say 从此哑掉。
        r = self._runner(delegated=True)
        r._delegation_open_at = time.time() - vcr.Runner.DELEGATION_SAY_BLOCK_SECONDS - 1
        self.assertFalse(r._delegation_owns_speech())
        self.assertTrue(self._say(r, source="backend")["ok"])

    def test_no_running_turn_means_no_gate(self):
        # 委派标记还在、但轮已经不在了 —— 那就没人替它开口了。
        r = self._runner(delegated=True, turn=False)
        self.assertFalse(r._delegation_owns_speech())


if __name__ == "__main__":
    unittest.main()
