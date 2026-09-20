"""一次后台任务 = 侧栏一个容器（ADR references/adr-turn-container.md）的两条运行器侧规则。

守的是 2026-09-18 用户实测的两件事：
  ① 「ai 的流式内容应该直接显示在工具卡内部而不是外部」—— 改投必须单向；
  ② 「从使用工具开始就应该建立工具卡片，所以工具卡片应该在生成物前面」—— 记录
     在历史里的位置由它被创建的那一刻决定，所以第一个工具调用就得把容器建出来。
"""

import asyncio
import importlib.util
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNNER = HERE.parent / "voice_cli_runner.py"

_spec = importlib.util.spec_from_file_location("voice_cli_runner_under_test", RUNNER)
vcr = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = vcr
_spec.loader.exec_module(vcr)

BACKEND = "01a0b4e0-36cc-7410-9169-061a180c3c10"
VOICE = "v-789740998692"


class UserTranscriptIdentityTest(unittest.TestCase):
    def _runner(self):
        from collections import deque
        r = object.__new__(vcr.Runner)
        r.settings = {"historyMode": "subtitle"}
        r.transcripts = deque()
        r._voice_user_stream = ""
        r._voice_user_turn_id = None
        r._voice_turn_id = VOICE
        r._turn = None
        r._last_backend_turn_id = None
        r._backend_done_at = 0
        r.posted, r.streamed = [], []
        r._history_post = lambda body: r.posted.append(body)
        r._stream_post = lambda tid, text, role="assistant": r.streamed.append((tid, text, role))
        r.log = lambda *args, **kwargs: None
        r._promise_watch = lambda *args: None
        return r

    def _delta(self, r, text):
        asyncio.run(r.on_notification("thread/realtime/transcript/delta", {"role": "user", "delta": text}))

    def test_two_user_segments_before_reply_have_distinct_history_ids(self):
        r = self._runner()
        self._delta(r, "第一")
        first = r.streamed[-1][0]
        r._subtitle_done("user", "第一句完整内容")
        self._delta(r, "第二")
        second = r.streamed[-1][0]
        r._subtitle_done("user", "第二句补充")
        self.assertNotEqual(first, second)
        self.assertEqual([p["turn_id"] for p in r.posted], [first, second])
        self.assertEqual([p["user"] for p in r.posted], ["第一句完整内容", "第二句补充"])

    def test_assistant_final_during_user_speech_cannot_change_user_identity(self):
        r = self._runner()
        self._delta(r, "我正在说")
        original = r.streamed[-1][0]
        r._subtitle_done("assistant", "助手此时才说完")
        self._delta(r, "的话")
        self.assertEqual(r.streamed[-1], (original, "我正在说的话", "user"))
        r._subtitle_done("user", "我正在说的话。")
        self.assertEqual(r.posted[-1]["turn_id"], original)

    def test_final_without_delta_does_not_overwrite_previous_user_message(self):
        r = self._runner()
        r._subtitle_done("user", "没有逐字事件的第一句")
        r._subtitle_done("user", "没有逐字事件的第二句")
        self.assertNotEqual(r.posted[0]["turn_id"], r.posted[1]["turn_id"])
        self.assertTrue(all(p["turn_id"].endswith(".u") for p in r.posted))

    def test_queued_partial_after_final_cannot_publish_empty_draft(self):
        from types import SimpleNamespace
        r = self._runner()
        identifier = "vu-queue.u"
        queued = iter([("log", {"user": "完整", "turn_id": identifier}), ("stream", identifier)])
        r._history_q = SimpleNamespace(get=lambda: next(queued))
        r._stream_latest = {identifier: "完整"}
        r._stream_role = {identifier: "user"}
        r._stream_queued = {identifier}
        r.history_stats = {"written": 0, "streamed": 0, "errors": 0}
        r.loop = SimpleNamespace(call_soon_threadsafe=lambda fn: fn())
        requests = []
        r._history_request = lambda path, body: requests.append((path, body)) or {}
        with self.assertRaises(StopIteration):
            r._history_worker()
        self.assertEqual([path for path, _ in requests], ["/api/assistant/log"])
        self.assertEqual(r._stream_role, {})


class StreamOwnerTest(unittest.TestCase):
    """改投是单向的：v- → 后台轮可以，后台轮 → v- 绝对不行。"""

    def test_没有后台轮时用自己的_v_容器(self):
        self.assertEqual(vcr.stream_owner(None, VOICE, None), (VOICE, None))

    def test_后台轮在跑就投进后台轮(self):
        self.assertEqual(vcr.stream_owner(BACKEND, VOICE, None), (BACKEND, None))

    def test_说到一半后台轮起来了_改投并清掉旧草稿(self):
        # 委派常发生在语音还在说的中途。不清空旧容器的草稿，同一段文字会顶在上面。
        self.assertEqual(vcr.stream_owner(BACKEND, VOICE, VOICE), (BACKEND, VOICE))

    def test_后台轮结束也不许把这句话甩回_v_容器(self):
        # 2026-09-18 实录 seq83 的原样复现：turn/completed 一到 backend 变 None。
        # 一句话一旦属于某次后台任务，它就一直属于那次。
        self.assertEqual(vcr.stream_owner(None, VOICE, BACKEND), (BACKEND, None))

    def test_换了另一个后台轮也不搬(self):
        # 真换轮次时，新一轮的第一个 delta 之前 _voice_stream_owner 已被重置为 None
        # （turn.created / transcript.done 两处都置空），所以这里看到 prev 还是旧后台轮
        # 只可能是同一句话 —— 不搬。
        self.assertEqual(vcr.stream_owner("01a0b999-aaaa", VOICE, BACKEND),
                         (BACKEND, None))


class ToolOpensContainerTest(unittest.TestCase):
    """第一个工具调用就把本轮容器建出来 —— 为的是**次序**，不只是早点显示。"""

    def _runner(self, turn):
        r = object.__new__(vcr.Runner)
        r._turn = turn
        r._voice_parts = {}
        r._pre_turn_voice = None
        r.posted = []
        r.logs = []
        r._history_post = lambda body: r.posted.append(body)
        r.log = lambda kind, **kv: r.logs.append((kind, kv))
        return r

    def test_首个工具调用就落一条裸部件建出记录(self):
        rec = {"id": BACKEND}
        r = self._runner(rec)
        r._tool_opened({"type": "mcpToolCall", "tool": "reader_anki_draft",
                        "server": "reader_snapshot"})
        self.assertEqual(len(r.posted), 1)
        body = r.posted[0]
        self.assertEqual(body["turn_id"], BACKEND)
        self.assertEqual(body["create_if_missing"], 1)
        self.assertEqual(body["parts"][0]["kind"], "tool")
        self.assertEqual(body["parts"][0]["tool"],
                         "reader_snapshot.reader_anki_draft")
        self.assertEqual(body["parts"][0]["origin"], "runner")
        # 裸部件：不带 args/result，收尾那条才带 —— 前端按同名工具就地升级，不另开方块。
        self.assertNotIn("args", body["parts"][0])
        self.assertNotIn("result", body["parts"][0])

    def test_同一轮里第二个工具不再重复建(self):
        rec = {"id": BACKEND}
        r = self._runner(rec)
        r._tool_opened({"type": "mcpToolCall", "tool": "a"})
        r._tool_opened({"type": "mcpToolCall", "tool": "b"})
        self.assertEqual(len(r.posted), 1)

    def test_没有本轮记录时什么都不做(self):
        r = self._runner(None)
        r._tool_opened({"type": "mcpToolCall", "tool": "a"})
        self.assertEqual(r.posted, [])


class PromiseRescueTest(unittest.TestCase):
    """补投的判据是「后台到底动没动」，不是委派序号。

    2026-09-18 连着三次误触发都栽在参照点上：委派可能比用户那句话的转写定稿**更早**，
    于是"从提问到现在没有新委派"恒成立，补投照发、正文还写着「一次后台调用都没有发生」，
    而后台刚把活干完。宁可漏补，也不要白起一轮 + 在侧栏多一个框 + 说一句假话。
    """

    def _runner(self, **kw):
        r = object.__new__(vcr.Runner)
        r.settings = {"promiseWatchSeconds": 0.01,
                      "promiseRecentBackendSeconds": 30.0}
        r.backend_busy = False
        r.app = object()
        r.thread_id = "t1"
        r._turn = None
        r._backend_done_at = 0.0
        r._delegation_seq = 0
        r.transcripts = [(0.0, "user", "帮我制卡")]
        r.logs = []
        r.log = lambda kind, **kv: r.logs.append((kind, kv))

        async def _turn_stub(text, **kwargs):
            r.logs.append(("turn_called", {}))
        r.turn = _turn_stub
        for k, v in kw.items():
            setattr(r, k, v)
        return r

    def _fired(self, r):
        asyncio.run(r._promise_rescue_inner())
        return "promise_rescue" in [k for k, _ in r.logs]

    def test_后台刚跑完就不补(self):
        import time
        now = time.time()
        r = self._runner(_backend_done_at=now - 4, _delegation_seq=7)
        r._promise_pending = (now, "我刚才的内容再试一次", "已经帮你做好了", True, 7)
        self.assertFalse(self._fired(r))

    def test_后台正在跑就不补(self):
        import time
        r = self._runner(_turn={"id": BACKEND}, _delegation_seq=7)
        r._promise_pending = (time.time(), "问", "在做了", True, 7)
        self.assertFalse(self._fired(r))

    def test_后台很久没动过才补(self):
        import time
        now = time.time()
        r = self._runner(_backend_done_at=now - 600, _delegation_seq=7)
        r._promise_pending = (now, "帮我制卡", "已经做好了", True, 7)
        self.assertTrue(self._fired(r))

    def test_提问后确实派过活就不补(self):
        import time
        now = time.time()
        r = self._runner(_backend_done_at=now - 600, _delegation_seq=9)
        r._promise_pending = (now, "帮我制卡", "已经做好了", True, 7)
        self.assertFalse(self._fired(r))


class VoicePartsTest(unittest.TestCase):
    """容器要**累积** AI 说的话，而不是后一句顶掉前一句。

    服务端按 origin 整组替换（_convo_upsert_turn），所以每次必须重发这一轮累计的全部
    语音正文。2026-09-18 实录：一轮里两句语音都正确路由进了 01a0b4e0，存储里却只剩
    后一句 —— 前一句被第二次投递整组换掉了。
    """

    def _runner(self):
        r = object.__new__(vcr.Runner)
        r._voice_parts = {}
        r.posted = []
        r.logs = []
        r._history_post = lambda body: r.posted.append(body)
        r.log = lambda kind, **kv: r.logs.append((kind, kv))
        return r

    def test_同一轮里两句都留着(self):
        r = self._runner()
        r._voice_post(BACKEND, "好的，我看一下。")
        r._voice_post(BACKEND, "已经帮你把卡片做好了。")
        texts = [p["text"] for p in r.posted[-1]["parts"]]
        self.assertEqual(texts, ["好的，我看一下。", "已经帮你把卡片做好了。"])
        self.assertTrue(all(p["origin"] == "voice" for p in r.posted[-1]["parts"]))

    def test_同一句不重复堆积(self):
        r = self._runner()
        r._voice_post(BACKEND, "一样的话")
        r._voice_post(BACKEND, "一样的话")
        self.assertEqual(len(r.posted[-1]["parts"]), 1)

    def test_不同轮次各自累计互不干扰(self):
        r = self._runner()
        r._voice_post(BACKEND, "甲")
        r._voice_post("01a0other", "乙")
        self.assertEqual([p["text"] for p in r.posted[-1]["parts"]], ["乙"])
        r._voice_post(BACKEND, "丙")
        self.assertEqual([p["text"] for p in r.posted[-1]["parts"]], ["甲", "丙"])

    def test_带_absorb_时一并请求删掉原记录(self):
        r = self._runner()
        r._voice_post(BACKEND, "好的，我看一下。", absorb="v-123")
        self.assertEqual(r.posted[-1]["absorb"], ["v-123"])


class PreTurnVoiceTest(unittest.TestCase):
    """委派前那句「好的，我看一下」要被收进本轮容器，否则它永远是工具卡外的孤框。"""

    def _runner(self, pre, last_user_at=0.0):
        r = object.__new__(vcr.Runner)
        r._turn = {"id": BACKEND}
        r._voice_parts = {}
        r._pre_turn_voice = pre
        r.transcripts = [(last_user_at, "user", "帮我制卡")]
        r.posted = []
        r.logs = []
        r._history_post = lambda body: r.posted.append(body)
        r.log = lambda kind, **kv: r.logs.append((kind, kv))
        return r

    def test_刚说完就开工_收编进本轮(self):
        import time
        r = self._runner(("v-123", "好的，我看一下。", time.time()))
        r._tool_opened({"type": "mcpToolCall", "tool": "reader_anki_draft"})
        kinds = [(b.get("via"), b.get("absorb")) for b in r.posted]
        self.assertIn(("voice", ["v-123"]), kinds)
        self.assertIsNone(r._pre_turn_voice, "收编后必须清掉，不能被下一轮再收一次")

    def test_太久以前的那句不收(self):
        import time
        r = self._runner(("v-123", "很久以前说的", time.time() - 300))
        r._tool_opened({"type": "mcpToolCall", "tool": "reader_anki_draft"})
        self.assertTrue(all(b.get("via") != "voice" for b in r.posted))

    def test_上一轮对话的回答不收(self):
        # 「嗯，听得到。」也在 30 秒内，但它答的是**上一次**提问。用户一开口，
        # 之前的话就都不再属于接下来这次任务 —— 否则等于把别人的话塞进本轮容器。
        import time
        now = time.time()
        r = self._runner(("v-old", "嗯，听得到。", now - 12), last_user_at=now - 3)
        r._tool_opened({"type": "mcpToolCall", "tool": "reader_anki_draft"})
        self.assertTrue(all(b.get("via") != "voice" for b in r.posted))

    def test_用户开口之后说的那句才收(self):
        import time
        now = time.time()
        r = self._runner(("v-123", "好的，我看一下。", now - 1), last_user_at=now - 3)
        r._tool_opened({"type": "mcpToolCall", "tool": "reader_anki_draft"})
        self.assertIn(("voice", ["v-123"]),
                      [(b.get("via"), b.get("absorb")) for b in r.posted])


class VoiceDraftTest(unittest.TestCase):
    """流式草稿 = 本轮已说完的几句 + 正在说的这句。

    容器只有一个草稿槽。只投"正在说的这句"，上一句就会被下一句的第一个字顶掉 ——
    用户 2026-09-19：「开头显示正常，中途文字突然消失，说完后又出现了」。
    """

    def _runner(self):
        r = object.__new__(vcr.Runner)
        r._voice_parts = {}
        r._voice_stream = ""
        r.posted = []
        r._history_post = lambda body: r.posted.append(body)
        r.log = lambda kind, **kv: None
        return r

    def test_说完一句再说下一句时上一句仍在(self):
        r = self._runner()
        r._voice_post(BACKEND, "在看。")                 # 第一句说完落库
        r._voice_stream = "我再试"                        # 第二句正在说
        self.assertEqual(r._voice_draft(BACKEND), "在看。" + "\n\n" + "我再试")

    def test_本轮第一句_只有正在说的那句(self):
        r = self._runner()
        r._voice_stream = "在看"
        self.assertEqual(r._voice_draft(BACKEND), "在看")

    def test_一句刚说完还没开口_不留空行(self):
        r = self._runner()
        r._voice_post(BACKEND, "在看。")
        r._voice_stream = ""
        self.assertEqual(r._voice_draft(BACKEND), "在看。")

    def test_草稿内容与最终落库的几句一致(self):
        # 草稿和收尾重载后的正文必须是同一串，否则收尾那一下会"跳一下"。
        r = self._runner()
        r._voice_post(BACKEND, "甲")
        r._voice_post(BACKEND, "乙")
        parts = [p["text"] for p in r.posted[-1]["parts"]]
        self.assertEqual(r._voice_draft(BACKEND), "\n\n".join(parts))


if __name__ == "__main__":
    unittest.main()
