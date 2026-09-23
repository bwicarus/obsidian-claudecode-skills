"""侧栏打的字一律直接交给后台，以用户原话、不加标签，而且要落侧栏记录。

用户 2026-09-23 实录（events.jsonl seq 790–803）：通话中打「你知道最新的jev和laya么」，
运行器把它加上「【用户打字】」前缀追加进语音会话 —— 语音模型把前缀连原话**念了出来**，
还顺手把上一轮的旧请求又委派了一遍；这句话本身也从没写进侧栏。
用户：「通话中打字时直接把信息传给后台ai就好」。

钉四条：
  ① 通话中打字也不再 appendText 进语音会话，而是起后台轮；
  ② 送出去的正文就是原话，没有任何前缀；
  ③ 后台正忙时插进那一轮（turn/steer），并且把这句写进侧栏；
  ④ 盘上 settings.json 里的旧提示词句子在加载时被迁移掉。
"""

import asyncio
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNNER = HERE.parent / "voice_cli_runner.py"

_spec = importlib.util.spec_from_file_location("voice_cli_runner_typed", RUNNER)
vcr = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = vcr
_spec.loader.exec_module(vcr)


class _App:
    def __init__(self):
        self.calls = []

    async def call(self, method, params, timeout=None):
        self.calls.append((method, params))
        return {}


class TypedTests(unittest.TestCase):
    def _runner(self, *, connected: bool, busy: bool):
        r = object.__new__(vcr.Runner)
        r.settings = {"typedPrefix": "【用户打字】"}   # 旧盘上值：必须被无视
        r.session_state = "connected" if connected else "idle"
        r.thread_id = "th-1"
        r.app = _App()
        r.backend_busy = busy
        r._turn = {"id": "turn-9", "parts": []} if busy else None
        r._pending_turn_user = None
        r.logged, r.history = [], []
        r.log = lambda kind, **d: r.logged.append((kind, d))
        r.mark_activity = lambda *_: None
        r._history_post = lambda body: r.history.append(body)

        async def _noop(*_a, **_k):
            return None
        r._ctx_on_speech = _noop
        r._ctx_inject_backend = _noop
        r.ensure_app = _noop
        return r

    def test_in_call_goes_to_backend_turn_without_prefix(self):
        r = self._runner(connected=True, busy=False)
        res = asyncio.run(r.typed("你知道最新的jev和laya么"))
        self.assertEqual(res, {"ok": True, "via": "backend"})
        methods = [m for m, _ in r.app.calls]
        self.assertNotIn("thread/realtime/appendText", methods)
        self.assertIn("turn/start", methods)
        params = dict(r.app.calls)["turn/start"]
        self.assertEqual(params["input"], [{"type": "text", "text": "你知道最新的jev和laya么"}])
        # 用户句在 turn/started 时由 _pending_turn_user 落侧栏
        self.assertEqual(r._pending_turn_user, "你知道最新的jev和laya么")

    def test_busy_backend_steers_and_records(self):
        r = self._runner(connected=True, busy=True)
        r.settings["turnSteerEnabled"] = True
        res = asyncio.run(r.typed("换成日语"))
        self.assertEqual(res["via"], "backend")
        method, params = r.app.calls[0]
        self.assertEqual(method, "turn/steer")
        self.assertEqual(params["input"], [{"type": "text", "text": "换成日语"}])
        self.assertEqual(len(r.history), 1)
        self.assertEqual(r.history[0]["user"], "换成日语")
        self.assertEqual(r.history[0]["via"], "codex-voice")

    def test_not_in_call_still_backend(self):
        r = self._runner(connected=False, busy=False)
        res = asyncio.run(r.typed("hi"))
        self.assertEqual(res["via"], "backend")
        self.assertEqual(r.app.calls[0][0], "turn/start")

    def test_persisted_prompt_sentences_migrated(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "settings.json"
            old_prompt = "前文。" + vcr._RETIRED_PROMPT_SENTENCES[0][1] + "后文。"
            old_backend = "前文。" + vcr._RETIRED_PROMPT_SENTENCES[1][1]
            path.write_text(json.dumps({"prompt": old_prompt, "backendThreadInstructions": old_backend,
                                        "typedPrefix": "【用户打字】"}, ensure_ascii=False), encoding="utf-8")
            saved = vcr.SETTINGS_PATH
            vcr.SETTINGS_PATH = path
            try:
                s = vcr.Runner.load_settings(object.__new__(vcr.Runner))
            finally:
                vcr.SETTINGS_PATH = saved
        self.assertNotIn("typedPrefix", s)
        self.assertNotIn("用户打字", s["prompt"] + s["backendThreadInstructions"])
        self.assertIn("前文。", s["prompt"])


if __name__ == "__main__":
    unittest.main()
