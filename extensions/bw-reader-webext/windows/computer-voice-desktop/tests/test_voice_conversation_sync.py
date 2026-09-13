# -*- coding: utf-8 -*-
"""语音对话 → 助手历史（2026-09-13 重做）的契约。

钉住的是当天翻车的三件事：① 线程按证据找、指针只兜底；② 绑定写成文件供推送通道用；
③ 轮次写进 Flask 单历史、失败出声、不重复写、补发有上限。
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import codex_thread_notify as NOTIFY  # noqa: E402
import voice_conversation_sync as VCS  # noqa: E402

T0 = 1_789_270_000.0  # 2026-09-13 03:26:40Z
THREAD_A = "01a09859-0b58-7c23-a7f5-0dca962cdf87"
THREAD_B = "01a098ce-fc2d-7d51-b715-1aed3a073dc9"


def _iso(epoch: float) -> str:
    import datetime as dt
    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _rollout(sessions: Path, thread: str, rows: list[dict], *, mtime: float | None = None) -> Path:
    day = sessions / "2026" / "09" / "13"
    day.mkdir(parents=True, exist_ok=True)
    path = day / ("rollout-2026-09-13T03-00-00-" + thread + ".jsonl")
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    if mtime is not None:
        import os
        os.utime(path, (mtime, mtime))
    return path


def _transcript(at: float) -> dict:
    return {"timestamp": _iso(at), "type": "realtime_item", "payload": {"type": "transcript_segment", "text": "hi"}}


def _delegation(at: float) -> dict:
    return {"timestamp": _iso(at), "type": "response_item",
            "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<realtime_delegation><input>x</input></realtime_delegation>"}]}}


def _user_item(item_id: str, text: str) -> dict:
    return {"type": "userMessage", "id": item_id, "content": [{"type": "text", "text": "<realtime_delegation>\n  <input>" + text + "</input>\n</realtime_delegation>"}]}


def _thread_result(thread: str, turns: list[dict]) -> dict:
    return {"thread": {"id": thread, "turns": turns}}


def _turn(index: int, *, completed: float, tools: list[dict] | None = None, final: bool = True) -> dict:
    items = [_user_item("u%d" % index, "问题%d" % index)]
    for tool in tools or []:
        items.append(tool)
    if final:
        items.append({"type": "agentMessage", "phase": "final_answer", "text": "[COMPLETE] 回答%d" % index})
    return {"id": "turn%d" % index, "items": items, "startedAt": int(completed) - 7, "completedAt": int(completed), "durationMs": 7000}


class FakeClient:
    def __init__(self, result):
        self.result = result
        self.reads = 0
        self.closed = 0
        self.last_response_bytes = 100

    def read_thread(self, thread_id):
        self.reads += 1
        return self.result if not callable(self.result) else self.result(thread_id)

    def close(self):
        self.closed += 1


class FakeWriter:
    def __init__(self, fail_after: int | None = None):
        self.calls: list[tuple[dict, str]] = []
        self.fail_after = fail_after

    def write_turn(self, turn, thread_id):
        if self.fail_after is not None and len(self.calls) >= self.fail_after:
            raise VCS.VoiceConversationError("Flask 不可达: boom")
        self.calls.append((turn, thread_id))
        return {"ok": True}


class Clock:
    def __init__(self, start=T0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class LocatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sessions = Path(self.temp.name) / "sessions"

    def test_evidence_picks_the_thread_receiving_speech_not_the_newest_file(self):
        """指针指着刚建的 chat-3，转写却进了老线程：证据说了算。"""
        _rollout(self.sessions, THREAD_A, [{"type": "session_meta"}, _transcript(T0 + 30), _delegation(T0 + 40)], mtime=T0 + 40)
        _rollout(self.sessions, THREAD_B, [{"type": "session_meta"},
                                           {"timestamp": _iso(T0 + 50), "type": "response_item",
                                            "payload": {"type": "function_call_output", "name": "send_message_to_thread"}}], mtime=T0 + 50)
        found = VCS.locate_voice_thread(self.sessions, since_epoch=T0, now=T0 + 60)
        self.assertEqual(found["threadId"], THREAD_A)
        self.assertEqual(found["evidenceKind"], "delegation")
        self.assertAlmostEqual(found["evidenceAt"], T0 + 40, places=2)

    def test_evidence_older_than_since_does_not_count(self):
        _rollout(self.sessions, THREAD_A, [_transcript(T0 - 3600)], mtime=T0 + 5)
        self.assertIsNone(VCS.locate_voice_thread(self.sessions, since_epoch=T0, now=T0 + 10))

    def test_tail_scan_survives_garbage_lines(self):
        path = _rollout(self.sessions, THREAD_A, [_transcript(T0 + 1)])
        with path.open("a", encoding="utf-8") as handle:
            handle.write("not json transcript_segment\n")
        evidence = VCS.scan_rollout_tail(path, since_epoch=T0)
        self.assertEqual(evidence["kind"], "transcript")


class ProjectionTests(unittest.TestCase):
    def test_turns_carry_tools_with_ms_and_timing(self):
        turns = VCS.project_turns(_thread_result(THREAD_A, [
            _turn(1, completed=T0 + 10, tools=[
                {"type": "mcpToolCall", "server": "reader_snapshot", "tool": "reader_context_snapshot", "status": "completed", "durationMs": 123},
                {"type": "webSearch", "query": "日本脳炎", "results": [1, 2]},
                {"type": "commandExecution", "status": "completed", "durationMs": 4500},
            ]),
            _turn(2, completed=T0 + 20, final=False),
        ]), THREAD_A)
        self.assertEqual(len(turns), 1, "没有 final_answer 的轮不算完成")
        turn = turns[0]
        self.assertEqual(turn["user"], "问题1")
        self.assertTrue(turn["requestId"].startswith("vh2:" + THREAD_A[:8] + ":"))
        self.assertEqual([t["tool"] for t in turn["tools"]], ["reader_snapshot.reader_context_snapshot", "web.search", "local.command"])
        self.assertEqual([t["ms"] for t in turn["tools"]], [123, None, 4500])
        self.assertEqual(turn["durationMs"], 7000)
        body = VCS.turn_to_log_body(turn, THREAD_A)
        self.assertEqual(body["via"], "codex-voice")
        self.assertEqual(body["assistant"], "回答1", "[COMPLETE] 传输标记不进历史")
        self.assertEqual(body["took_ms"], 7000)
        self.assertEqual(body["parts"][0]["kind"], "tool")
        self.assertEqual(body["parts"][0]["ms"], 123)
        self.assertNotIn("ms", body["parts"][1], "没有耗时就不编一个")

    def test_thread_id_mismatch_is_an_error(self):
        with self.assertRaises(VCS.VoiceConversationError):
            VCS.project_turns(_thread_result(THREAD_B, []), THREAD_A)


class WriterTests(unittest.TestCase):
    def test_posts_bearer_json_and_rejects_not_ok(self):
        seen = {}

        class Resp:
            def __init__(self, body):
                self.body = body

            def read(self):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def opener(request, timeout):
            seen["url"] = request.full_url
            seen["auth"] = request.get_header("Authorization")
            seen["body"] = json.loads(request.data.decode("utf-8"))
            return Resp(b'{"ok": true, "n": 2}')

        writer = VCS.FlaskHistoryWriter(base_url="http://127.0.0.1:5000/", token="tok", opener=opener)
        turn = {"requestId": "vh2:x:y", "user": "u", "assistant": "a", "tools": [], "durationMs": 10}
        self.assertEqual(writer.write_turn(turn, THREAD_A)["n"], 2)
        self.assertEqual(seen["url"], "http://127.0.0.1:5000/api/assistant/log")
        self.assertEqual(seen["auth"], "Bearer tok")
        self.assertEqual(seen["body"]["turn_id"], "vh2:x:y")
        self.assertEqual(seen["body"]["thread_id"], THREAD_A)

        def bad(request, timeout):
            return Resp(b'{"ok": false, "error": "parts"}')

        with self.assertRaises(VCS.VoiceConversationError):
            VCS.FlaskHistoryWriter(base_url="http://x", token="tok", opener=bad).write_turn(turn, THREAD_A)

    def test_missing_token_is_a_named_error(self):
        writer = VCS.FlaskHistoryWriter(base_url="http://x", token=None, opener=lambda *a, **k: None)
        writer._token = None
        import os
        old = os.environ.pop("MCP_WEBAPP_TOKEN", None)
        try:
            with unittest.mock.patch.object(VCS, "default_token", return_value=None):
                with self.assertRaises(VCS.VoiceConversationError) as ctx:
                    writer.write_turn({"requestId": "r", "user": "u", "assistant": "a", "tools": []}, THREAD_A)
            self.assertIn("令牌", str(ctx.exception))
        finally:
            if old is not None:
                os.environ["MCP_WEBAPP_TOKEN"] = old


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.root = root
        self.sessions = root / "sessions"
        self.binding = root / "binding.json"
        self.global_state = root / "global.json"
        self.global_state.write_text(json.dumps({"electron-persisted-atom-state": {
            "realtime-voice-most-recent-thread": {"conversationId": THREAD_B, "hostId": "local"}}}), encoding="utf-8")
        self.clock = Clock()
        self.mono = Clock(0.0)

    def make(self, client, writer=None):
        self.writer = writer or FakeWriter()
        return VCS.VoiceConversationSync(root=self.root, sessions_dir=self.sessions, global_state_path=self.global_state,
                                         binding_path=self.binding, writer=self.writer, history_client=client,
                                         clock=self.clock, monotonic=self.mono)

    def active(self, sync, generation=1):
        return sync.observe(service_online=True, capture_active=True, snapshot_mode=True, capture_generation=generation)

    def test_binds_by_evidence_and_writes_new_turns_once(self):
        _rollout(self.sessions, THREAD_A, [_transcript(T0 + 1)], mtime=T0 + 1)
        result = _thread_result(THREAD_A, [_turn(1, completed=T0 - 100), _turn(2, completed=T0 + 5)])
        client = FakeClient(result)
        sync = self.make(client)
        self.clock.advance(10)
        self.active(sync)
        binding = json.loads(self.binding.read_text(encoding="utf-8"))
        self.assertEqual(binding["threadId"], THREAD_A)
        self.assertEqual(binding["source"], "evidence")
        self.assertTrue(binding["captureActive"])
        self.assertEqual([t[0]["user"] for t in self.writer.calls], ["问题1", "问题2"], "12 小时内没写过的都补上")
        self.assertEqual(client.reads, 1)
        # 同一拍再来：rollout 没变，不重读、不重写。
        self.mono.advance(5)
        self.active(sync)
        self.assertEqual(client.reads, 1)
        self.assertEqual(len(self.writer.calls), 2)
        # rollout 长了 → 重读；已写过的不再写。
        result["thread"]["turns"].append(_turn(3, completed=T0 + 30))
        _rollout(self.sessions, THREAD_A, [_transcript(T0 + 31)])
        self.mono.advance(5)
        self.active(sync)
        self.assertEqual(client.reads, 2)
        self.assertEqual([t[0]["user"] for t in self.writer.calls], ["问题1", "问题2", "问题3"])
        status = sync.status()
        self.assertEqual(status["boundThread"], THREAD_A)
        self.assertEqual(status["bindingSource"], "evidence")
        self.assertIsNone(status["lastError"])
        # 进程重开也不重写：写过的在状态文件里。
        sync2 = self.make(FakeClient(result))
        self.mono.advance(5)
        self.active(sync2)
        self.assertEqual(self.writer.calls, [])

    def test_follows_speech_when_it_moves_to_another_thread(self):
        _rollout(self.sessions, THREAD_A, [_transcript(T0 + 1)], mtime=T0 + 1)
        results = {THREAD_A: _thread_result(THREAD_A, [_turn(1, completed=T0 + 2)]),
                   THREAD_B: _thread_result(THREAD_B, [_turn(9, completed=T0 + 60)])}
        client = FakeClient(lambda tid: results[tid])
        sync = self.make(client)
        self.clock.advance(5)
        self.active(sync)
        self.assertEqual(json.loads(self.binding.read_text(encoding="utf-8"))["threadId"], THREAD_A)
        # 转写换到 B：下一拍改绑 B，B 的轮次也写进历史。
        _rollout(self.sessions, THREAD_B, [_transcript(T0 + 50)], mtime=T0 + 50)
        self.clock.advance(50)
        self.mono.advance(5)
        self.active(sync)
        self.assertEqual(json.loads(self.binding.read_text(encoding="utf-8"))["threadId"], THREAD_B)
        self.assertEqual([t[1] for t in self.writer.calls], [THREAD_A, THREAD_B])

    def test_pointer_only_after_grace_and_evidence_overrides_it(self):
        client = FakeClient(lambda tid: _thread_result(tid, []))
        sync = self.make(client)
        self.active(sync)
        self.assertFalse(self.binding.exists(), "刚开语音、没证据：先不绑，别把指针当真")
        self.clock.advance(VCS.POINTER_FALLBACK_AFTER_SECONDS + 1)
        self.mono.advance(5)
        self.active(sync)
        binding = json.loads(self.binding.read_text(encoding="utf-8"))
        self.assertEqual((binding["threadId"], binding["source"]), (THREAD_B, "pointer"))
        _rollout(self.sessions, THREAD_A, [_transcript(self.clock.now)], mtime=self.clock.now)
        self.mono.advance(5)
        self.active(sync)
        binding = json.loads(self.binding.read_text(encoding="utf-8"))
        self.assertEqual((binding["threadId"], binding["source"]), (THREAD_A, "evidence"))

    def test_writer_failure_is_visible_and_not_marked_written(self):
        _rollout(self.sessions, THREAD_A, [_transcript(T0 + 1)], mtime=T0 + 1)
        result = _thread_result(THREAD_A, [_turn(1, completed=T0 + 2), _turn(2, completed=T0 + 3)])
        sync = self.make(FakeClient(result), FakeWriter(fail_after=1))
        self.clock.advance(5)
        self.active(sync)
        self.assertEqual(len(self.writer.calls), 1)
        self.assertIn("Flask 不可达", sync.status()["lastError"])
        self.assertEqual(sync.status()["pending"], 1)
        log = (self.root / "runtime" / "voice-conversation-sync.log").read_text(encoding="utf-8")
        self.assertIn("error: Flask 不可达", log)
        # 写端恢复 → 下一次读把剩下那轮补上，不重复第一轮。
        self.writer.fail_after = None
        _rollout(self.sessions, THREAD_A, [_transcript(T0 + 6)])
        self.mono.advance(5)
        self.active(sync)
        self.assertEqual([t[0]["user"] for t in self.writer.calls], ["问题1", "问题2"])
        self.assertIsNone(sync.status()["lastError"])

    def test_backfill_is_capped_and_old_turns_stay_history(self):
        _rollout(self.sessions, THREAD_A, [_transcript(T0 + 1)], mtime=T0 + 1)
        turns = [_turn(i, completed=T0 - 3600 * 20)] if False else []
        turns = [_turn(0, completed=T0 - 3600 * 20)]  # 超过 12 小时：永远不补
        turns += [_turn(i, completed=T0 - 600 + i) for i in range(1, VCS.BACKFILL_MAX_TURNS + 6)]
        sync = self.make(FakeClient(_thread_result(THREAD_A, turns)))
        self.clock.advance(5)
        self.active(sync)
        users = [t[0]["user"] for t in self.writer.calls]
        self.assertEqual(len(users), VCS.BACKFILL_MAX_TURNS)
        self.assertEqual(users[-1], "问题%d" % (VCS.BACKFILL_MAX_TURNS + 5))
        self.assertNotIn("问题0", users)

    def test_capture_end_keeps_binding_but_marks_inactive(self):
        _rollout(self.sessions, THREAD_A, [_transcript(T0 + 1)], mtime=T0 + 1)
        client = FakeClient(_thread_result(THREAD_A, [_turn(1, completed=T0 + 2)]))
        sync = self.make(client)
        self.clock.advance(5)
        self.active(sync)
        for _ in range(VCS.FINAL_TAIL_POLLS + 1):
            self.mono.advance(5)
            sync.observe(service_online=True, capture_active=False, snapshot_mode=True)
        binding = json.loads(self.binding.read_text(encoding="utf-8"))
        self.assertEqual(binding["threadId"], THREAD_A)
        self.assertFalse(binding["captureActive"])
        self.assertGreaterEqual(client.closed, 1)
        self.assertIsNone(sync.status()["boundThread"])

    def test_service_offline_holds_instead_of_dropping(self):
        _rollout(self.sessions, THREAD_A, [_transcript(T0 + 1)], mtime=T0 + 1)
        sync = self.make(FakeClient(_thread_result(THREAD_A, [])))
        self.clock.advance(5)
        self.active(sync)
        self.assertIsNone(sync.observe(service_online=False, capture_active=True, snapshot_mode=True))
        self.assertEqual(sync.status()["boundThread"], THREAD_A)


class BindingConsumersTests(unittest.TestCase):
    def test_notify_prefers_bound_thread_when_fresh(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            path = home / "voice-thread-binding.json"
            VCS.write_binding(path, thread_id=THREAD_A, source="evidence", bound_at=VCS.time.time(),
                              evidence_at=None, evidence_kind=None, capture_active=True, capture_generation=1)
            self.assertEqual(NOTIFY.bound_voice_thread(home), THREAD_A)
            self.assertEqual(VCS.read_binding(path, max_age_seconds=60)["threadId"], THREAD_A)
            VCS.write_binding(path, thread_id=THREAD_A, source="evidence", bound_at=VCS.time.time() - 2 * 86400,
                              evidence_at=None, evidence_kind=None, capture_active=False, capture_generation=None)
            self.assertIsNone(NOTIFY.bound_voice_thread(home), "一天没更新的绑定不认")
            self.assertIsNone(VCS.read_binding(path, max_age_seconds=86400))
            path.write_text('{"contract":"other","threadId":"' + THREAD_A + '"}', encoding="utf-8")
            self.assertIsNone(NOTIFY.bound_voice_thread(home))


if __name__ == "__main__":
    unittest.main()
