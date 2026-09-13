#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""voice_turn_trace.py — 导出一轮语音对话的真实轨迹（2026-09-13）。

"整理成工具"不靠 AI 回忆自己做过什么：这里从 Codex app-server 读那一轮的原始记录，
把用户原话、每一步工具的名字/参数/结果/耗时、最终回答导成一份 JSON。按钮和口头
"把刚才那个存成工具"都用它。

    voice_turn_trace.py --last                 # 当前语音线程最近一轮
    voice_turn_trace.py --turn vh2:xxxx:yyyy    # 指定轮（侧栏历史里的 turn_id）
    voice_turn_trace.py --last --out trace.json

线程默认取绑定文件（voice_conversation_sync.read_binding），没有再退 Codex 指针。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import voice_conversation_sync as VCS  # noqa: E402
import voice_history_sidebar_sync as LEGACY  # noqa: E402

TRACE_CONTRACT = "reader-voice-turn-trace/1"
MAX_OUTPUT_CHARS = 8000


def _text_blocks(result: Any) -> str:
    if isinstance(result, dict) and isinstance(result.get("content"), list):
        return "\n".join(str(c.get("text") or "") for c in result["content"] if isinstance(c, dict) and c.get("type") == "text")
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False) if result is not None else ""


def rich_step(item: dict[str, Any], index: int) -> dict[str, Any] | None:
    """一条 app-server 项 → 轨迹里的一步（带参数与输出）。不是工具的项返回 None。"""
    kind = item.get("type")
    ms = item.get("durationMs")
    ms = int(ms) if isinstance(ms, (int, float)) and not isinstance(ms, bool) else None
    if kind == "mcpToolCall":
        tool = str(item.get("tool") or "")
        return {"index": index, "kind": "mcp", "server": str(item.get("server") or ""), "tool": tool,
                "label": LEGACY._tool_label(str(item.get("server") or ""), tool, tool),
                "status": LEGACY._tool_status(item.get("status")), "ms": ms,
                "args": item.get("arguments") if isinstance(item.get("arguments"), (dict, str)) else {},
                "output": _text_blocks(item.get("result"))[:MAX_OUTPUT_CHARS],
                "raw": item.get("result") if isinstance(item.get("result"), dict) else None}
    if kind == "webSearch":
        results = item.get("results") if isinstance(item.get("results"), list) else []
        return {"index": index, "kind": "web", "tool": "web.search", "label": "网页搜索",
                "status": "done", "ms": ms, "args": {"query": item.get("query")},
                "output": [{"title": r.get("title"), "url": r.get("url")} for r in results[:10] if isinstance(r, dict)]}
    if kind == "commandExecution":
        return {"index": index, "kind": "command", "tool": "local.command", "label": "本地命令",
                "status": LEGACY._tool_status(item.get("status")), "ms": ms,
                "args": {"command": item.get("command"), "cwd": item.get("cwd")},
                "output": str(item.get("aggregatedOutput") or "")[:4000], "exitCode": item.get("exitCode")}
    if kind == "fileChange":
        changes = item.get("changes") if isinstance(item.get("changes"), list) else []
        return {"index": index, "kind": "file", "tool": "local.file", "label": "修改文件",
                "status": LEGACY._tool_status(item.get("status")), "ms": ms,
                "args": {"paths": [c.get("path") for c in changes if isinstance(c, dict)][:20]}, "output": ""}
    return None


def project_traces(result: Any, thread_id: str) -> list[dict[str, Any]]:
    """整条线程 → 每个完成轮次的轨迹。requestId 与侧栏历史的 turn_id 一致。"""
    turns = VCS.project_turns(result, thread_id)  # 用它的 requestId/user/assistant/时间
    by_request = {t["requestId"]: t for t in turns}
    traces: list[dict[str, Any]] = []
    thread = result["thread"]
    for turn_index, turn in enumerate(thread.get("turns") or []):
        items = turn.get("items") or []
        current_request: str | None = None
        steps: list[dict[str, Any]] = []
        for item_index, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "userMessage":
                user_text = LEGACY._codex_user_text(item)
                if user_text is None:
                    current_request = None
                    continue
                raw_turn_id = turn.get("id")
                turn_id = raw_turn_id if isinstance(raw_turn_id, str) and 0 < len(raw_turn_id) <= 256 else f"turn-{turn_index}"
                raw_item_id = item.get("id")
                item_id = raw_item_id if isinstance(raw_item_id, str) and 0 < len(raw_item_id) <= 256 else f"user-{item_index}"
                current_request = LEGACY._codex_request_id(thread_id, turn_id, item_id, item_index, user_text)
                steps = []
                continue
            if current_request is None:
                continue
            step = rich_step(item, len(steps))
            if step is not None:
                steps.append(step)
                continue
            if item.get("type") == "agentMessage" and item.get("phase") == "final_answer" and current_request in by_request:
                base = by_request[current_request]
                traces.append({
                    "contract": TRACE_CONTRACT, "threadId": thread_id, "requestId": current_request,
                    "turnId": base["turnId"], "user": base["user"], "assistant": LEGACY._publish_assistant_text(base["assistant"]),
                    "startedAt": base.get("startedAt"), "completedAt": base.get("completedAt"), "durationMs": base.get("durationMs"),
                    "steps": steps,
                })
                current_request = None
    return traces


def default_thread_id() -> str | None:
    binding = VCS.read_binding(VCS.default_binding_path(), max_age_seconds=7 * 86400)
    if binding:
        return binding["threadId"]
    profile = Path(os.environ.get("USERPROFILE") or Path.home())
    return VCS.pointer_thread(profile / ".codex" / ".codex-global-state.json")


def load_trace(*, thread_id: str | None, request_id: str | None, client: Any | None = None) -> dict[str, Any]:
    thread_id = thread_id or default_thread_id()
    if not thread_id:
        raise VCS.VoiceConversationError("找不到语音线程（没有绑定，也没有指针）")
    own = client is None
    client = client or LEGACY.CodexAppServerHistoryClient()
    try:
        result = client.read_thread(thread_id)
    finally:
        if own:
            client.close()
    traces = project_traces(result, thread_id)
    if not traces:
        raise VCS.VoiceConversationError("线程 %s 里没有完成的轮次" % thread_id[:8])
    if request_id:
        for trace in traces:
            if trace["requestId"] == request_id:
                return trace
        raise VCS.VoiceConversationError("线程里没有 turn %s" % request_id)
    return traces[-1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出一轮语音对话的真实轨迹")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--last", action="store_true")
    group.add_argument("--turn", help="侧栏历史里的 turn_id（vh2:…）")
    parser.add_argument("--thread", help="线程 id；默认用绑定文件，再退 Codex 指针")
    parser.add_argument("--out", type=Path, help="写到文件（仍会打印一行摘要）")
    args = parser.parse_args(argv)
    try:
        trace = load_trace(thread_id=args.thread, request_id=None if args.last else args.turn)
    except (VCS.VoiceConversationError, LEGACY.CodexAppServerError, LEGACY.SyncDataError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"ok": True, "out": str(args.out), "requestId": trace["requestId"], "steps": len(trace["steps"]),
                          "user": trace["user"][:80]}, ensure_ascii=False))
    else:
        print(json.dumps(trace, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
