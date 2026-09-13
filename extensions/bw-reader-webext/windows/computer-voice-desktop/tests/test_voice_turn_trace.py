# -*- coding: utf-8 -*-
"""语音轨迹导出：每步带参数/输出/耗时，requestId 与侧栏历史的 turn_id 一致。"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import voice_conversation_sync as VCS  # noqa: E402
import voice_turn_trace as T  # noqa: E402

THREAD = "01a09859-0b58-7c23-a7f5-0dca962cdf87"


def user(item_id, text):
    return {"type": "userMessage", "id": item_id, "content": [{"type": "text", "text": "<realtime_delegation><input>" + text + "</input></realtime_delegation>"}]}


def result():
    return {"thread": {"id": THREAD, "turns": [
        {"id": "t1", "startedAt": 100, "completedAt": 110, "durationMs": 10000, "items": [
            user("u1", "做一张卡"),
            {"type": "mcpToolCall", "server": "reader_snapshot", "tool": "reader_context_snapshot", "status": "completed",
             "durationMs": 75, "arguments": {"brief": True}, "result": {"content": [{"type": "text", "text": "{\"selectedItems\":[]}"}]}},
            {"type": "webSearch", "query": "who rubella", "results": [{"title": "WHO", "url": "https://who.int/x"}]},
            {"type": "fileChange", "status": "completed", "changes": [{"path": "C:\\\\x.txt"}]},
            {"type": "mcpToolCall", "server": "reader_snapshot", "tool": "reader_card", "status": "completed", "durationMs": 160,
             "arguments": {"card": {"kind": "general"}}, "result": {"content": [{"type": "text", "text": "{\"ok\":true}"}]}},
            {"type": "agentMessage", "phase": "final_answer", "text": "[COMPLETE] 送上去了"},
        ]},
        {"id": "t2", "items": [user("u2", "谢谢"), {"type": "agentMessage", "phase": "final_answer", "text": "不客气"}]},
    ]}}


class TraceTests(unittest.TestCase):
    def test_trace_matches_history_turn_ids_and_keeps_args(self):
        traces = T.project_traces(result(), THREAD)
        turns = VCS.project_turns(result(), THREAD)
        self.assertEqual([t["requestId"] for t in traces], [t["requestId"] for t in turns])
        first = traces[0]
        self.assertEqual(first["user"], "做一张卡")
        self.assertEqual(first["assistant"], "送上去了")
        self.assertEqual([(s["kind"], s["tool"], s["ms"]) for s in first["steps"]],
                         [("mcp", "reader_context_snapshot", 75), ("web", "web.search", None), ("file", "local.file", None), ("mcp", "reader_card", 160)])
        self.assertEqual(first["steps"][3]["args"], {"card": {"kind": "general"}})
        self.assertEqual(first["steps"][3]["output"], "{\"ok\":true}")
        self.assertEqual(first["steps"][1]["output"][0]["url"], "https://who.int/x")
        self.assertEqual(first["durationMs"], 10000)

    def test_load_trace_picks_last_or_by_request(self):
        class Client:
            def read_thread(self, tid):
                return result()

            def close(self):
                pass

        last = T.load_trace(thread_id=THREAD, request_id=None, client=Client())
        self.assertEqual(last["user"], "谢谢")
        traces = T.project_traces(result(), THREAD)
        picked = T.load_trace(thread_id=THREAD, request_id=traces[0]["requestId"], client=Client())
        self.assertEqual(picked["user"], "做一张卡")
        with self.assertRaises(VCS.VoiceConversationError):
            T.load_trace(thread_id=THREAD, request_id="vh2:nope", client=Client())

    def test_sidebar_projection_shows_file_changes(self):
        turns = VCS.project_turns(result(), THREAD)
        self.assertIn("local.file", [t["tool"] for t in turns[0]["tools"]])


if __name__ == "__main__":
    unittest.main()
