# -*- coding: utf-8 -*-
"""voice_trace — 把一次对话在两个模型里发生的事归一成时间轴（2026-09-16 用户要求）。

用户要在 ReaderPC 页面上看清楚：语音模型和文字模型各自**被注入了什么、生成了什么、
查看了什么、实际执行了什么代码**，每步花多久、报没报错、吃了多少 token，并且能折叠。

数据全部已经在硬盘上，这里只读不写：

| 来源 | 给出什么 |
|---|---|
| `%LOCALAPPDATA%/BWReader/voice-cli/events.jsonl` | 注入（ctx_backend / ctx_voice_selection）、语音转写、会话起止 |
| `~/.codex/sessions/**/rollout-*.jsonl` | 文字侧：注入的 developer 消息、推理、**执行的代码**、工具返回、按轮 token |
| `~/bw-computer-voice-bridge/runtime/mcp-tool-calls.jsonl` | 每次工具调用的耗时与成败 |
| `%LOCALAPPDATA%/BWReader/voice-cli/tool-errors.jsonl` | 工具报错原文 |

⚠ 只读、只看尾部：这些文件会长到几十 MB，全量读会把界面拖死。
"""
from __future__ import annotations

import glob
import json
import os
import time
from pathlib import Path
from typing import Any

LOCAL = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "BWReader"
VOICE_CLI = LOCAL / "voice-cli"
BRIDGE_RUNTIME = Path.home() / "bw-computer-voice-bridge" / "runtime"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"

#: 事件类型 → 泳道与颜色标签。界面按这个上色，别在页面里再写一份。
KIND_STYLE = {
    "inject": ("注入", "sky"),
    "speech": ("语音", "pink"),
    "generate": ("生成", "emerald"),
    "think": ("推理", "indigo"),
    "code": ("执行代码", "dim"),
    "tool": ("工具", "amber"),
    "error": ("报错", "rose"),
    "session": ("会话", "violet"),
}


def _tail_jsonl(path: Path, limit: int) -> list[dict]:
    """只读尾部若干行。文件可能有几十 MB，全量解析会把界面拖死。"""
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            block = min(size, max(limit * 400, 65536))
            fh.seek(size - block)
            raw = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    out = []
    for line in raw.splitlines()[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def _newest_rollout(thread_id: str | None) -> Path | None:
    pattern = "*%s*.jsonl" % thread_id if thread_id else "*.jsonl"
    hits = glob.glob(str(CODEX_SESSIONS / "**" / pattern), recursive=True)
    if not hits:
        return None
    return Path(max(hits, key=os.path.getmtime))


def _current_thread_id() -> str | None:
    try:
        return json.loads((VOICE_CLI / "state.json").read_text(encoding="utf-8")).get("threadId")
    except Exception:   # noqa: BLE001
        return None


def _clip(text: Any, limit: int = 4000) -> str:
    s = "" if text is None else str(text)
    return s if len(s) <= limit else s[:limit] + "…（已截断，共 %d 字）" % len(s)


def _voice_lane(limit: int, since: float = 0.0) -> list[dict]:
    """语音侧：注入进去的、说出来的、会话起止。

    ⚠ events.jsonl 不带 threadId，所以看"某条对话"时只能按**时间窗**裁：
    since 取那条线程的第一条记录时刻。这比混在一起看清楚得多（用户 2026-09-16）。
    """
    rows = []
    for d in _tail_jsonl(VOICE_CLI / "events.jsonl", limit * 6):
        if since and float(d.get("t") or 0) < since:
            continue
        kind = d.get("kind") or ""
        at = float(d.get("t") or 0)
        if kind == "ctx_voice_selection":
            rows.append({"lane": "voice", "kind": "inject", "at": at,
                         "title": "注入选中清单", "meta": "%s 字" % d.get("chars"), "body": ""})
        elif kind == "ctx_backend":
            rows.append({"lane": "text", "kind": "inject", "at": at,
                         "title": "注入阅读状态" + ("（带正文）" if d.get("withText") else ""),
                         "meta": "%s 字 · 第 %s 页" % (d.get("chars"), str(d.get("page") or "?")[-6:]),
                         "body": ""})
        elif kind == "transcript":
            who = "用户" if d.get("role") == "user" else "助手"
            rows.append({"lane": "voice", "kind": "speech", "at": at,
                         "title": who, "meta": "", "body": _clip(d.get("text"))})
        elif kind in ("session_connected", "session_stopped"):
            rows.append({"lane": "voice", "kind": "session", "at": at,
                         "title": "会话开始" if kind == "session_connected" else "会话结束",
                         "meta": _clip(d.get("reason") or "", 60), "body": ""})
        elif kind == "realtime_usage" and d.get("deltaMs"):
            continue   # 用量按秒滚动，逐条列没有意义；总量在页面顶部
    return rows


VOICE_CORE_URL = os.environ.get("BW_VOICE_CORE_URL", "http://127.0.0.1:43131")


def _items_via_api(thread_id: str | None, limit: int) -> list[dict] | None:
    """走运行器代理的 thread/items/list。拿不到就返回 None，让调用方退回落盘文件。"""
    try:
        import urllib.request   # noqa: WPS433 —— 只有这里用
        body = json.dumps({"threadId": thread_id or "", "limit": max(20, min(limit, 300))}).encode()
        req = urllib.request.Request(VOICE_CORE_URL + "/thread/items", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=6) as fh:
            d = json.loads(fh.read().decode("utf-8", "replace"))
    except Exception:   # noqa: BLE001 —— 运行器没起来是常态，不是错误
        return None
    rows = (((d or {}).get("result") or {}).get("data")) or []
    return rows if isinstance(rows, list) else None


def _lane_from_items(rows: list[dict]) -> list[dict]:
    """thread/items/list 的形状 → 链路行。比落盘文件干净：类型是结构化的，还带 turnId。"""
    out = []
    for x in rows:
        it = x.get("item") if isinstance(x, dict) else None
        if not isinstance(it, dict):
            continue
        turn = str(x.get("turnId") or "")[-6:]
        t = it.get("type") or ""
        if t == "userMessage":
            text = " ".join(c.get("text") or "" for c in (it.get("content") or []) if isinstance(c, dict))
            if not text.strip():
                continue
            # 我们推进去的状态都带这个前缀；用户真说的话不带 —— 据此分「注入」还是「用户」
            injected = text.lstrip().startswith(("【", "⟦"))
            out.append({"lane": "text", "kind": "inject" if injected else "speech", "at": 0.0,
                        "turn": turn, "title": "注入" if injected else "用户",
                        "meta": "%d 字" % len(text), "body": _clip(text)})
        elif t == "agentMessage":
            out.append({"lane": "text", "kind": "generate", "at": 0.0, "turn": turn, "title": "回答",
                        "meta": "%d 字" % len(it.get("text") or ""), "body": _clip(it.get("text"))})
        elif t == "reasoning":
            out.append({"lane": "text", "kind": "think", "at": 0.0, "turn": turn, "title": "推理",
                        "meta": "", "body": _clip(json.dumps(it, ensure_ascii=False), 1500)})
        elif t in ("mcpToolCall", "commandExecution", "webSearch", "fileChange"):
            name = it.get("tool") or it.get("name") or it.get("command") or t
            body = it.get("arguments") or it.get("command") or it.get("input") or ""
            out.append({"lane": "text", "kind": "code" if t == "commandExecution" else "tool",
                        "at": 0.0, "turn": turn, "title": str(name)[:60],
                        "meta": str(it.get("status") or ""),
                        "body": _clip(body if isinstance(body, str) else json.dumps(body, ensure_ascii=False))})
    return out


def _text_lane(thread_id: str | None, limit: int) -> list[dict]:
    """文字侧：模型真正读到与做出的东西。

    先走 thread/items/list（按 threadId 精确、带 turnId）；运行器没起来时退回落盘文件。
    """
    rows = _items_via_api(thread_id, limit)
    if rows:
        return _lane_from_items(rows)
    path = _newest_rollout(thread_id)
    if path is None:
        return []
    rows = []
    for d in _tail_jsonl(path, limit * 8):
        pay = d.get("payload") if isinstance(d.get("payload"), dict) else d
        kind = pay.get("type") or ""
        try:
            at = time.mktime(time.strptime((d.get("timestamp") or "")[:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:   # noqa: BLE001
            at = 0.0
        if kind == "message":
            role = pay.get("role") or ""
            text = "".join(c.get("text") or "" for c in (pay.get("content") or []) if isinstance(c, dict))
            if not text.strip():
                continue
            rows.append({"lane": "text",
                         "kind": "inject" if role == "developer" else "speech",
                         "at": at, "title": "developer 注入" if role == "developer" else role,
                         "meta": "%d 字" % len(text), "body": _clip(text)})
        elif kind == "agentMessage":
            rows.append({"lane": "text", "kind": "generate", "at": at, "title": "回答",
                         "meta": "%d 字" % len(pay.get("text") or ""), "body": _clip(pay.get("text"))})
        elif kind == "reasoning":
            rows.append({"lane": "text", "kind": "think", "at": at, "title": "推理",
                         "meta": "", "body": _clip(json.dumps(pay, ensure_ascii=False), 1500)})
        elif kind == "custom_tool_call":
            script = pay.get("input") or pay.get("arguments") or ""
            rows.append({"lane": "text", "kind": "code", "at": at,
                         "title": "执行 " + str(pay.get("name") or "?"),
                         "meta": "%d 字" % len(str(script)), "body": _clip(script)})
        elif kind == "custom_tool_call_output":
            out = pay.get("output")
            text = "".join(c.get("text") or "" for c in out if isinstance(c, dict)) if isinstance(out, list) else str(out)
            rows.append({"lane": "text", "kind": "tool", "at": at, "title": "工具返回",
                         "meta": "%d 字" % len(text), "body": _clip(text)})
        elif kind in ("token_count", "token_usage_record"):
            info = json.dumps(pay, ensure_ascii=False)
            rows.append({"lane": "text", "kind": "session", "at": at, "title": "本轮用量",
                         "meta": "", "body": _clip(info, 800)})
    return rows


def _tool_stats(days: float, cold: set[str] | None = None) -> dict:
    """工具使用统计 + 当前在热池还是折叠池（用户 2026-09-16 要看这个）。"""
    rows = _tail_jsonl(BRIDGE_RUNTIME / "mcp-tool-calls.jsonl", 4000)
    cutoff = time.time() - days * 86400
    counts: dict[str, list[int]] = {}
    for r in rows:
        try:
            at = time.mktime(time.strptime((r.get("at") or "")[:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:   # noqa: BLE001
            at = time.time()
        if at < cutoff:
            continue
        counts.setdefault(str(r.get("name") or "?"), []).append(int(r.get("ms") or 0))
    hot, folded = [], []
    for name, ms in sorted(counts.items(), key=lambda kv: -len(kv[1])):
        ordered = sorted(ms)
        row = {"name": name, "calls": len(ms),
               "medianMs": ordered[len(ordered) // 2] if ordered else 0}
        (folded if (cold and name in cold) else hot).append(row)
    # 一次没调过的冷工具也要列出来 —— 用户要看的是"现在分池的样子"，不只是"用过的"
    for name in sorted(cold or []):
        if name not in counts:
            folded.append({"name": name, "calls": 0, "medianMs": 0})
    return {"days": days, "hot": hot, "cold": folded,
            "tools": hot + folded}   # tools 保留：老界面还在用


_COLD_CACHE: dict[str, Any] = {"at": 0.0, "names": []}


def cold_tool_names(max_age: float = 300.0) -> list[str]:
    """折叠池名单：直接问桥的 MCP 进程，按"描述里带取参提示"识别。

    ⚠ 不在界面里另写一份名单 —— 名单只有一处真相（C# 的 ColdToolNames），
    而折叠过的工具描述本身就是自描述的，问一次就知道。约 200 ms，缓存 5 分钟。
    """
    if time.time() - _COLD_CACHE["at"] < max_age:
        return list(_COLD_CACHE["names"])
    exe = Path.home() / "bw-computer-voice-bridge" / "native-host" / "bw-computer-voice-audio.exe"
    state = BRIDGE_RUNTIME / "reader-context-snapshot.json"
    names: list[str] = []
    if exe.exists():
        import subprocess
        try:
            proc = subprocess.Popen(
                [str(exe), "--reader-context-mcp", "--state", str(state)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            def send(obj):
                proc.stdin.write(json.dumps(obj, ensure_ascii=False) + chr(10))
                proc.stdin.flush()
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                             "clientInfo": {"name": "readerpc-ui", "version": "0"}}})
            proc.stdout.readline()
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            deadline = time.time() + 8
            while time.time() < deadline:
                line = proc.stdout.readline()
                if not line:
                    break
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("id") == 2:
                    for tool in ((msg.get("result") or {}).get("tools") or []):
                        if "Parameters are not inlined" in (tool.get("description") or ""):
                            names.append(str(tool.get("name")))
                    break
            proc.kill()
        except Exception:   # noqa: BLE001
            names = []
    _COLD_CACHE.update({"at": time.time(), "names": names})
    return names


def build(limit: int = 120, days: float = 7.0, cold: list[str] | None = None,
          thread: str | None = None) -> dict:
    """给界面的一份时间轴。两条泳道合在一个数组里，前端按 lane 分列。

    thread 不给就看当前那条；给了就看指定的那条（用户 2026-09-16：链路该按选中的对话看）。
    """
    thread_id = thread or _current_thread_id()
    text_rows = _text_lane(thread_id, limit)
    since = min((r.get("at") or 0) for r in text_rows) if text_rows else 0.0
    rows = _voice_lane(limit, since) + text_rows
    errors = _tail_jsonl(VOICE_CLI / "tool-errors.jsonl", 40)
    for e in errors:
        rows.append({"lane": "text", "kind": "error", "at": float(e.get("t") or 0),
                     "title": "工具报错 " + str(e.get("tool") or ""),
                     "meta": str(e.get("status") or ""), "body": _clip(e.get("result"), 1200)})
    rows.sort(key=lambda r: r.get("at") or 0)
    rows = rows[-limit * 2:]
    # 每条补一个「距上一条多久」，界面直接显示步骤耗时
    previous = None
    for row in rows:
        row["gapMs"] = int(((row.get("at") or 0) - previous) * 1000) if previous else 0
        previous = row.get("at") or previous
        style = KIND_STYLE.get(row["kind"], ("其他", "dim"))
        row["label"], row["color"] = style
    return {"contract": "voice-trace/1", "threadId": thread_id, "rows": rows,
            "tools": _tool_stats(days, set(cold if cold is not None else cold_tool_names()))}


if __name__ == "__main__":   # 手工查看：python voice_trace.py
    print(json.dumps(build(limit=20), ensure_ascii=False, indent=1)[:4000])
