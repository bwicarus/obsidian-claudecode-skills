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

import calendar
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
    "handoff": ("交接", "amber"),   # 语音模型把活交给后台的那一刻
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


def _thread_started_at(thread_id: str | None) -> float:
    """线程的创建时刻（秒）。

    app-server 的 threadId 是 UUIDv7，**前 48 位就是毫秒时间戳** —— 直接解出来，
    不用碰磁盘。这是判断「哪些事件属于这条对话」最可靠的下界：
    2026-09-16 用户问「为何每个新对话都有这些报错」，就是因为新对话还没有历史行、
    时间下界取到了 0，于是昨天的工具报错被贴进了每一条新对话的链路。
    """
    try:
        ms = int(str(thread_id).replace("-", "")[:12], 16)
    except (TypeError, ValueError):
        return 0.0
    sec = ms / 1000.0
    # 合理性闸：2020-01-01 ~ 2100-01-01。不是 UUIDv7 就当没有下界，别误杀
    return sec if 1577836800 < sec < 4102444800 else 0.0


def _clip(text: Any, limit: int = 4000) -> str:
    s = "" if text is None else str(text)
    return s if len(s) <= limit else s[:limit] + "…（已截断，共 %d 字）" % len(s)


def _voice_lane(limit: int, since: float = 0.0, thread: str | None = None) -> list[dict]:
    """语音侧：注入进去的、说出来的、会话起止。

    按对话过滤分两种精度：
      · 事件自带 threadId（运行器 2026-09-16 起每条都盖）→ **精确匹配**；
      · 那之前的老事件没有这个字段 → 退回按时间窗裁（since = 该线程第一条记录的时刻）。
    所以老记录仍看得见，新记录不会再串到别的对话上。
    """
    rows = []
    for d in _tail_jsonl(VOICE_CLI / "events.jsonl", limit * 6):
        if thread:
            own = d.get("threadId")
            if own and own != thread:
                continue          # 明说了是别条对话的，直接跳
            if not own and since and float(d.get("t") or 0) < since:
                continue          # 没盖章的老事件：只能按时间窗判
        elif since and float(d.get("t") or 0) < since:
            continue
        kind = d.get("kind") or ""
        at = float(d.get("t") or 0)
        if kind in ("ctx_voice_selection", "ctx_voice"):
            rows.append({"lane": "voice", "kind": "inject", "at": at,
                         "title": "注入选中清单" if kind == "ctx_voice_selection" else "注入阅读状态",
                         "meta": "%s 字" % d.get("chars"),
                         # 运行器 2026-09-16 起把注入正文一并记下 —— 之前这里恒为空，
                         # 点开什么都看不到，而这个页面的意义就是看清实际注入了什么
                         "body": _clip(d.get("body") or "", 8000)})
        elif kind == "ctx_steer":
            # 2026-09-17 起的主路径：后台真的开工之后，把状态插进**正在跑的那一轮**
            rows.append({"lane": "text", "kind": "inject", "at": at,
                         "title": "插进运行中的轮",
                         "meta": "%s 字 · 第 %s 页" % (d.get("chars"), str(d.get("page") or "?")[-6:]),
                         "body": _clip(d.get("body") or "", 8000)})
        elif kind.startswith("ctx_") and kind.endswith("_error"):
            # ⚠ 这些异常原来只写一行日志、界面上看不见 —— 2026-09-17 语音侧的选中清单
            # 因为一处解包写错，每次都抛异常并被吞掉，整整一段时间一条都没投出去，
            # 直到用户说「AI 完全不知道我在说什么」才发现。注入失败必须在链路上看得见。
            rows.append({"lane": "voice" if "voice" in kind else "text",
                         "kind": "error", "at": at,
                         "title": "注入失败 " + kind[4:-6],
                         "meta": _clip(d.get("message") or "", 80), "body": ""})
        elif kind == "ctx_steer_fallback":
            rows.append({"lane": "text", "kind": "error", "at": at,
                         "title": "插播没赶上，退回追加",
                         "meta": _clip(d.get("reason") or "", 60), "body": ""})
        elif kind in ("ctx_backend", "ctx_backend_deferred"):
            rows.append({"lane": "text", "kind": "inject", "at": at,
                         "title": "注入阅读状态" + ("（带正文）" if d.get("withText") else "") +
                                  ("（延后补投）" if kind == "ctx_backend_deferred" else ""),
                         "meta": "%s 字 · 第 %s 页" % (d.get("chars"), str(d.get("page") or "?")[-6:]),
                         "body": _clip(d.get("body") or "", 8000)})
        elif kind == "dc_delegation_created":
            # 两个模型的交接点 —— 时间轴上最该看见的一步：语音模型在这一刻把活交给后台，
            # 报文里就是它转过去的原话（2026-09-16）
            raw = d.get("payload") or ""
            handed = ""
            try:
                item = (json.loads(raw) or {}).get("item") or {}
                handed = " ".join(c.get("text") or "" for c in (item.get("content") or [])
                                  if isinstance(c, dict))
            except ValueError:
                # 2026-09-16 之前这条报文被截在 200 字，JSON 解不开 —— 但转过去的原话
                # 就在截断点之前，正则捞得出来。不这么做旧记录的交接摘要全是空的
                import re   # noqa: WPS433
                m = re.search(r'"text":\s*"((?:[^"\\]|\\.)*)"', raw)
                if m:
                    # ⚠ 别用 unicode_escape：报文是 ensure_ascii=False 写的，
                    # 正文本来就是 UTF-8，再解一遍会变成乱码（第一次就踩了）。
                    # 按 JSON 字符串解才对，两种转义形式都能正确还原。
                    try:
                        handed = json.loads('"' + m.group(1) + '"')
                    except ValueError:
                        handed = m.group(1)
            rows.append({"lane": "voice", "kind": "handoff", "at": at,
                         "title": "交给后台", "meta": _clip(handed, 60),
                         "body": _clip(d.get("payload") or "", 4000)})
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


#: role=user 的消息不一定是「用户说的话」—— Codex 和运行器都会以 user 身份塞东西进来。
#: 2026-09-16 用户问「为何 11178 字那条标成语音」，就是 <recommended_plugins> 被当成了用户说话。
_SYSTEM_USER_MARKS = (
    ("<realtime_delegation>", "语音转交后台"),
    ("<recommended_plugins>", "Codex 插件清单"),
    ("<multi_agent_mode>", "Codex 多智能体说明"),
    ("<realtime_conversation>", "实时会话指令"),
    ("<user_instructions>", "用户指令"),
    ("【", "阅读状态注入"),
    ("⟦", "阅读器标记"),
)


def _classify_message(role: str, text: str) -> dict:
    """一条消息到底算「注入」还是「用户说的话」。

    只按 role 分是不够的：Codex 的插件清单、多智能体说明、运行器的委托包装，
    统统以 role=user 塞进来，全按语音显示会让人以为用户说了一万一千字。
    """
    head = text.lstrip()
    if role == "developer":
        for mark, label in _SYSTEM_USER_MARKS:
            if head.startswith(mark):
                return {"kind": "inject", "title": label}
        return {"kind": "inject", "title": "developer 注入"}
    for mark, label in _SYSTEM_USER_MARKS:
        if head.startswith(mark):
            return {"kind": "inject", "title": label}
    return {"kind": "speech", "title": role or "用户"}


def _reasoning_row(pay: dict) -> dict:
    """推理那一条里真正可读的东西只有 summary。

    2026-09-17 用户问「推理步骤里面写的是什么」—— 原来是把整个 payload 倒成 JSON，
    而里面最长的是 `encrypted_content`：OpenAI 加密的推理正文，**谁都解不开，包括我们**。
    于是屏幕上几千字符全是密文，真正有意义的那行标题反而被挤没了。
    这里只取 summary[].text，并如实说明正文不可读，别让人以为是自己看不懂。
    """
    parts = []
    for item in (pay.get("summary") or []):
        if isinstance(item, dict) and item.get("text"):
            parts.append(str(item["text"]).strip())
    enc = pay.get("encrypted_content")
    if not parts:
        return {"title": "推理",
                "meta": "模型没给摘要",
                "body": ("这一步的推理正文由 OpenAI 加密（encrypted_content，%d 字符），"
                         "本地无法解密；模型这次也没有给出摘要。" % len(str(enc or ""))) if enc else ""}
    head = parts[0].strip("*").strip()
    body = (chr(10) + chr(10)).join(parts)
    if enc:
        body += (chr(10) + chr(10) + "——" + chr(10) +
                 "（推理正文由 OpenAI 加密，共 %d 字符，本地无法解密；"
                 "上面这段是模型自己给的摘要。）" % len(str(enc)))
    return {"title": "推理：" + head[:40], "meta": "%d 段摘要" % len(parts), "body": body}


def _usage_row(pay: dict) -> dict:
    """用量那一条。

    ⚠ 这里的一条**不是一轮对话**，是**一次模型请求**（2026-09-17 用户问出来的）：
    模型每调一次工具，拿到结果后就要再请求一次模型，把到目前为止的整个上下文重发一遍。
    实测一个线程里 turn_context 只有 2 条，token_count 却有 22 条 ——
    也就是 2 轮对话用掉了 22 次模型请求。我原先把它标成「本轮用量」是错的。

    「输入」包含的是这次请求**重发的全部东西**：开场指令 + 工具面 + 这一轮之前的全部
    对话与工具返回 + 刚注入的状态。其中命中缓存的部分按 1/10 计价，
    真正按全价付的只有「新增」那一项。
    """
    def pick(*path):
        cur = pay
        for key in path:
            if not isinstance(cur, dict) or key not in cur:
                return None
            cur = cur[key]
        return cur

    last = pick("info", "last_token_usage") or pick("last_token_usage") or {}
    total = pick("info", "total_token_usage") or pick("total_token_usage") or {}
    src = last if isinstance(last, dict) and last else (total if isinstance(total, dict) else {})
    if not src:
        return {"title": "模型请求用量", "meta": "形状未识别",
                "body": _clip(json.dumps(pay, ensure_ascii=False), 800)}

    inp = int(src.get("input_tokens") or 0)
    cached = int(src.get("cached_input_tokens") or 0)
    out = int(src.get("output_tokens") or 0)
    fresh = max(0, inp - cached)
    lines = [
        "这是**一次模型请求**的账，不是一轮对话 —— 每调一次工具就要再请求一次模型，",
        "把到此为止的整个上下文重发一遍。",
        "",
        "输入 %s ＝ 开场指令 + 工具面 + 本轮之前的全部对话与工具返回 + 刚注入的状态" % f"{inp:,}",
        "  其中命中缓存 %s（按 1/10 计价）" % f"{cached:,}",
        "  **真正按全价付的新增 %s**" % f"{fresh:,}",
        "输出 %s ＝ 模型这一次生成的内容（含它写的代码与要调的工具参数）" % f"{out:,}",
    ]
    if isinstance(total, dict) and total.get("input_tokens"):
        lines += ["", "——",
                  "这条对话累计：输入 %s / 输出 %s" % (
                      f"{int(total.get('input_tokens') or 0):,}",
                      f"{int(total.get('output_tokens') or 0):,}")]
    return {"title": "模型请求用量",
            "meta": "新增 %s · 输出 %s · 缓存 %s" % (f"{fresh:,}", f"{out:,}", f"{cached:,}"),
            "body": chr(10).join(lines)}

def _text_lane(thread_id: str | None, limit: int) -> list[dict]:
    """文字侧：模型真正读到与做出的东西。

    **落盘文件优先**，thread/items/list 只作兜底。理由是实测出来的，不是偏好：
      · `thread/inject_items` 注入的内容**根本不出现在 items/list 里**（模型看得到、
        接口列不出来）。而「文字模型被注入了什么」正是这个页面最先要看的东西，
        走 items 会让那一栏整个空掉（2026-09-16 我自己踩的）；
      · 落盘文件带时间戳、带执行的代码、带每轮 token 用量，items 都没有；
      · 「按线程精确」也不是 items 独有的 —— rollout 文件名里就带 threadId。
    items/list 的价值在它带 turnId 且不依赖磁盘，所以留作找不到落盘文件时的退路。
    """
    path = _newest_rollout(thread_id)
    if path is None:
        return _lane_from_items(_items_via_api(thread_id, limit) or [])
    rows = []
    for d in _tail_jsonl(path, limit * 8):
        pay = d.get("payload") if isinstance(d.get("payload"), dict) else d
        kind = pay.get("type") or ""
        try:
            # ⚠ 落盘文件里的时间戳是 **UTC**，`time.mktime` 会当成本地时间解 ——
            # 在 JST 下整整差 9 小时，时间轴的左右交错全排错、注入去重也匹配不上
            # （2026-09-16 用户问「为何第 40 页注入了两次」时顺带查出来的）。
            # 跟当初 app_gone_watch 那个自动关闭失灵是同一类错，一样用 calendar.timegm。
            at = calendar.timegm(time.strptime((d.get("timestamp") or "")[:19], "%Y-%m-%dT%H:%M:%S"))
        except Exception:   # noqa: BLE001
            at = 0.0
        if kind == "message":
            role = pay.get("role") or ""
            text = "".join(c.get("text") or "" for c in (pay.get("content") or []) if isinstance(c, dict))
            if not text.strip():
                continue
            rows.append(dict(_classify_message(role, text), lane="text", at=at,
                             meta="%d 字" % len(text), body=_clip(text)))
        elif kind == "agentMessage":
            rows.append({"lane": "text", "kind": "generate", "at": at, "title": "回答",
                         "meta": "%d 字" % len(pay.get("text") or ""), "body": _clip(pay.get("text"))})
        elif kind == "reasoning":
            rows.append(dict(_reasoning_row(pay), lane="text", kind="think", at=at))
        elif kind == "custom_tool_call":
            # ⚠ 一次工具调用在落盘里是**三条**：调用、返回、这次请求的用量。
            # 分开列会把时间轴撑成三倍、还看不出哪条返回属于哪次调用
            # （2026-09-17 用户要求「把同一个工具调用相关内容关联起来」）。
            # 这里先记下调用，等返回与用量到了再合成一条。
            script = pay.get("input") or pay.get("arguments") or ""
            pending = {"lane": "text", "kind": "code", "at": at,
                       "title": "调用 " + str(pay.get("name") or "?"),
                       "meta": "", "body": "",
                       "_script": str(script), "_out": None, "_usage": None}
            rows.append(pending)
        elif kind == "custom_tool_call_output":
            out = pay.get("output")
            text = "".join(c.get("text") or "" for c in out if isinstance(c, dict)) if isinstance(out, list) else str(out)
            host = next((r for r in reversed(rows) if r.get("_out") is None and "_script" in r), None)
            if host is None:
                rows.append({"lane": "text", "kind": "tool", "at": at, "title": "工具返回（找不到对应调用）",
                             "meta": "%d 字" % len(text), "body": _clip(text)})
            else:
                host["_out"] = text
        elif kind in ("token_count", "token_usage_record"):
            row = dict(_usage_row(pay), lane="text", kind="session", at=at)
            host = next((r for r in reversed(rows) if r.get("_usage") is None and r.get("_out") is not None), None)
            if host is None:
                rows.append(row)          # 不属于任何工具调用（比如开场那次请求）
            else:
                host["_usage"] = row
    return [_fold_tool_row(r) for r in rows]


def _fold_tool_row(row: dict) -> dict:
    """把「调用 + 返回 + 这次请求的用量」折成一条可展开的记录。"""
    if "_script" not in row:
        return row
    script, out, usage = row.pop("_script", ""), row.pop("_out", None), row.pop("_usage", None)
    parts = ["【模型写的代码】", script or "（空）"]
    if out is not None:
        parts += ["", "【工具返回】", out]
    else:
        parts += ["", "（还没有返回 —— 这次调用可能还在跑，或那一轮被中断了）"]
    bits = ["代码 %d 字" % len(script or "")]
    if out is not None:
        bits.append("返回 %d 字" % len(out))
        # 返回特别大的直接标出来：它是不走缓存的新增输入，最花钱的一项
        if len(out) > 8000:
            bits.append("⚠ 返回过大")
    if usage:
        parts += ["", "【这次模型请求的用量】", usage.get("body") or ""]
        bits.append(usage.get("meta") or "")
    # 代码模式下每条都叫 exec，标题看不出在干什么 —— 把脚本里第一个像样的片段带上。
    # 优先找它调的工具名（mcp__x__y 或 reader_xxx），没有就取首行。
    import re   # noqa: WPS433
    hint = ""
    m = re.search(r"(?:mcp__[a-z_]+__)?(reader_[a-z_]+|kj_[a-z_]+|voice_[a-z_]+)", script or "")
    if m:
        hint = m.group(1)
    else:
        first = next((ln.strip() for ln in (script or "").splitlines() if ln.strip()), "")
        hint = first[:38]
    if hint:
        row["title"] = row["title"] + " → " + hint
    row["meta"] = " · ".join(b for b in bits if b)
    row["body"] = _clip(chr(10).join(parts), 12000)
    return row


def _tool_stats(days: float, cold: set[str] | None = None) -> dict:
    """工具使用统计 + 当前在热池还是折叠池（用户 2026-09-16 要看这个）。"""
    rows = _tail_jsonl(BRIDGE_RUNTIME / "mcp-tool-calls.jsonl", 4000)
    cutoff = time.time() - days * 86400
    counts: dict[str, list[int]] = {}
    for r in rows:
        try:
            # mcp-tool-calls.jsonl 的 at 明确写着 +00:00（UTC），不能按本地解 ——
            # 否则「近 N 天」的截止线整体偏 9 小时（JST）。
            # 注意隔壁 tool-errors.jsonl 的 at 是**本地时间**，两者不一样，别一起改
            at = calendar.timegm(time.strptime((r.get("at") or "")[:19], "%Y-%m-%dT%H:%M:%S"))
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


_MCP_CACHE: dict[str, Any] = {"at": 0.0, "tools": []}
BRIDGE_EXE = Path.home() / "bw-computer-voice-bridge" / "native-host" / "bw-computer-voice-audio.exe"
SKILL_ROOTS = (Path.home() / ".codex" / "skills",)
#: 折叠过的工具，描述里都有这句自描述 —— 名单只有一处真相（C# 的 ColdToolNames），
#: 界面不另写一份，问一次就知道。
_FOLDED_MARK = "Parameters are not inlined"


def _mcp_call(requests: list[dict], budget: float = 10.0) -> dict[int, dict]:
    """起一次桥的 MCP 子进程，发几条请求，按 id 收结果。

    起停约 200 ms。调用方自己缓存 —— 界面每次刷新都起一个进程是不行的。
    """
    if not BRIDGE_EXE.exists():
        return {}
    import subprocess
    out: dict[int, dict] = {}
    try:
        proc = subprocess.Popen(
            [str(BRIDGE_EXE), "--reader-context-mcp", "--state",
             str(BRIDGE_RUNTIME / "reader-context-snapshot.json")],
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
        want = set()
        for r in requests:
            send(r)
            want.add(r["id"])
        deadline = time.time() + budget
        while want and time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") in want:
                out[msg["id"]] = msg
                want.discard(msg["id"])
        proc.kill()
    except Exception:   # noqa: BLE001
        return out
    return out


def _mcp_tools(max_age: float = 300.0) -> list[dict]:
    """桥暴露的全部工具：名字 + 简介 + 参数表 + 在哪个池。缓存 5 分钟。"""
    if time.time() - _MCP_CACHE["at"] < max_age and _MCP_CACHE["tools"]:
        return list(_MCP_CACHE["tools"])
    got = _mcp_call([{"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}])
    tools = []
    for t in (((got.get(2) or {}).get("result") or {}).get("tools") or []):
        desc = t.get("description") or ""
        tools.append({"name": str(t.get("name")), "description": desc,
                      "inputSchema": t.get("inputSchema"),
                      "pool": "cold" if _FOLDED_MARK in desc else "hot"})
    if tools:
        _MCP_CACHE.update({"at": time.time(), "tools": tools})
    return tools


def cold_tool_names(max_age: float = 300.0) -> list[str]:
    """折叠池名单。从 _mcp_tools 派生 —— 不在这里再写一份名单。"""
    return [t["name"] for t in _mcp_tools(max_age) if t["pool"] == "cold"]


def _read_text(path: Path, limit: int = 60000) -> str:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return raw if len(raw) <= limit else raw[:limit] + "\n…（已截断，共 %d 字）" % len(raw)


def _skill_dir(name: str) -> Path | None:
    """skill 名字 → 磁盘目录。带包前缀的（pkg:name）取冒号后那段。"""
    leaf = name.split(":")[-1]
    for root in SKILL_ROOTS:
        d = root / leaf
        if d.is_dir():
            return d
    return None


def _find_flow(directory: Path) -> Path | None:
    """目录里的流程文件。

    认两种：叫 flow.json / *.flow.json 的，以及任何**内容长得像流程**的 json
    （bw-reader-skill-flow/1 的标志是顶层有 steps 数组）—— 后者是为了不漏掉
    起了别的名字的那些。流程文件才是「这个功能实际怎么跑」的那一份，要优先展示。
    """
    named = [f for f in directory.rglob("*.json")
             if f.name == "flow.json" or f.name.endswith(".flow.json")]
    if named:
        return named[0]
    for f in directory.rglob("*.json"):
        try:
            if f.stat().st_size > 400000:
                continue
            doc = json.loads(f.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and isinstance(doc.get("steps"), list):
            return f
    return None


def _task_dir(name: str) -> Path | None:
    """定时任务目录。固化下来的流程都放在这儿，一个任务一份 flow.json。"""
    leaf = name.split(":")[-1]
    d = LOCAL / "scheduled-tasks" / leaf
    return d if d.is_dir() else None


def tool_detail(name: str) -> dict:
    """一个工具/skill/定时任务的全部可展示信息（用户 2026-09-16：要能点开看）。"""
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "缺 name"}

    for t in _mcp_tools():
        if t["name"] != name:
            continue
        out = {"ok": True, "kind": "mcp", "name": name, "pool": t["pool"],
               "description": t["description"], "inputSchema": t["inputSchema"]}
        if t["pool"] == "cold":
            # 折叠后工具面上只剩一行；完整参数表要向能力指南要（C# 那边的 TryReadColdToolName 分支）
            got = _mcp_call([{"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                              "params": {"name": "reader_capability_guide",
                                         "arguments": {"tool": name}}}])
            body = (((got.get(3) or {}).get("result") or {}).get("content") or [])
            text = "".join(c.get("text") or "" for c in body if isinstance(c, dict))
            try:
                guide = json.loads(text)
                out["description"] = guide.get("description") or out["description"]
                out["inputSchema"] = guide.get("inputSchema") or out["inputSchema"]
                out["howToCall"] = guide.get("howToCall")
            except ValueError:
                out["guideRaw"] = _clip(text, 2000)
        return out

    directory = _skill_dir(name)
    if directory is not None:
        files = []
        for f in sorted(directory.rglob("*")):
            if f.is_file() and f.suffix.lower() in (".md", ".json", ".py", ".txt"):
                files.append({"path": str(f.relative_to(directory)).replace(chr(92), "/"),
                              "bytes": f.stat().st_size})
        flow = _find_flow(directory)
        return {"ok": True, "kind": "skill", "name": name, "dir": str(directory),
                "doc": _read_text(directory / "SKILL.md"),
                "flowPath": str(flow.relative_to(directory)).replace(chr(92), "/") if flow else None,
                "flow": _read_text(flow) if flow else "",
                "files": files[:60]}

    task = _task_dir(name)
    if task is not None:
        flow = _find_flow(task)
        return {"ok": True, "kind": "task", "name": name, "dir": str(task),
                "description": "定时任务。下面的流程文件就是它每次实际跑的步骤。",
                "flowPath": flow.name if flow else None,
                "flow": _read_text(flow) if flow else "",
                "doc": _read_text(task / "memory.md", 8000),
                "files": [{"path": f.name, "bytes": f.stat().st_size}
                          for f in sorted(task.iterdir()) if f.is_file()][:40]}

    for sk in _skill_list():
        if sk.get("name") == name:
            return {"ok": True, "kind": "skill", "name": name,
                    "description": sk.get("description") or "",
                    "doc": "", "files": [],
                    "note": "这个 skill 不在本机 skills 目录里（多半随插件安装），只能给到简介。"}
    return {"ok": False, "error": "没找到叫 %s 的工具或 skill" % name}


def _skill_list() -> list[dict]:
    """skill 目录（走运行器的 /skills → app-server skills/list）。

    工具表原来只有 MCP 的常驻池和折叠池，skill 是第三层，页面上完全看不见。

    ⚠ 这是**目录查询，不反映本次会话封存了哪些插件**：2026-09-16 实测，
    裸起和带 plugins.*.enabled=false 起，skills/list 都是同样的条数。
    所以别拿这个数去推算上下文成本 —— 要知道模型实际看到什么，
    得去量线程里那条 developer 消息（SLIM_PLUGINS 那 25.6K 就是那么量出来的）。
    运行器没起来就返回空，不是错误。
    """
    try:
        import urllib.request   # noqa: WPS433
        with urllib.request.urlopen(VOICE_CORE_URL + "/skills", timeout=6) as fh:
            d = json.loads(fh.read().decode("utf-8", "replace"))
    except Exception:   # noqa: BLE001
        return []
    rows = (d or {}).get("skills")
    return rows if isinstance(rows, list) else []


def _dedupe_injections(rows: list[dict]) -> list[dict]:
    """同一次注入会从两个来源各来一遍，去掉重复的那条。

    我们自己记的 `ctx_backend`（events.jsonl）和 Codex 落盘里那条 developer 消息
    说的是同一件事 —— 2026-09-16 用户问「为何第 40 页注入了两次」，就是这个。
    去重机制本身没坏，是**画了两遍**。

    判同一条的依据：都在文字泳道、都是注入、字数一致、时间相差 5 秒内。
    保留我们自己那条（它带页码和「是否带正文」，信息更全）。
    """
    def size_of(row):
        meta = str(row.get("meta") or "")
        digits = ""
        for ch in meta:
            if ch.isdigit():
                digits += ch
            elif digits:
                break
        return int(digits) if digits else -1

    keep, seen = [], []
    for row in rows:
        if row.get("lane") != "text" or row.get("kind") != "inject":
            keep.append(row)
            continue
        size, at = size_of(row), row.get("at") or 0
        dup = next((i for i, (s2, a2) in enumerate(seen)
                    if s2 == size and size > 0 and abs(a2 - at) <= 5), None)
        if dup is None:
            seen.append((size, at))
            keep.append(row)
            continue
        # 已经有一条了：谁带页码信息就留谁
        prev = next(r for r in keep if r.get("kind") == "inject"
                    and size_of(r) == size and abs((r.get("at") or 0) - at) <= 5)
        if "页" in str(row.get("meta") or "") and "页" not in str(prev.get("meta") or ""):
            keep[keep.index(prev)] = row
    return keep


def build(limit: int = 120, days: float = 7.0, cold: list[str] | None = None,
          thread: str | None = None) -> dict:
    """给界面的一份时间轴。两条泳道合在一个数组里，前端按 lane 分列。

    thread 不给就看当前那条；给了就看指定的那条（用户 2026-09-16：链路该按选中的对话看）。
    """
    thread_id = thread or _current_thread_id()
    text_rows = _text_lane(thread_id, limit)
    # 下界优先用线程自己的创建时刻（threadId 是 UUIDv7，前 48 位就是时间戳）：
    # 新对话还没有历史行时，按历史行取下界会得到 0，于是什么都过滤不掉
    since = _thread_started_at(thread_id)
    if text_rows:
        seen = [r.get("at") or 0 for r in text_rows if (r.get("at") or 0) > 0]
        if seen:
            since = max(since, min(seen)) if since else min(seen)
    rows = _voice_lane(limit, since, thread_id) + text_rows
    # ⚠ 报错要按**这条对话**筛，不能无脑贴最后 40 条。
    # 2026-09-16 用户报「为何每个新对话都有这些报错」—— 那 4 条其实是 9-15 18:13 的陈年旧账，
    # 因为这里不分线程也不分时间，于是它们出现在每一条新对话的链路里，看着像天天在坏。
    errors = _tail_jsonl(VOICE_CLI / "tool-errors.jsonl", 40)
    for e in errors:
        if since and float(e.get("t") or 0) < since:
            continue
        rows.append({"lane": "text", "kind": "error", "at": float(e.get("t") or 0),
                     "title": "工具报错 " + str(e.get("tool") or ""),
                     "meta": str(e.get("status") or ""), "body": _clip(e.get("result"), 1200)})
    rows.sort(key=lambda r: r.get("at") or 0)
    rows = _dedupe_injections(rows)
    rows = rows[-limit * 2:]
    # 每条补一个「距上一条多久」，界面直接显示步骤耗时
    previous = None
    for row in rows:
        row["gapMs"] = int(((row.get("at") or 0) - previous) * 1000) if previous else 0
        previous = row.get("at") or previous
        style = KIND_STYLE.get(row["kind"], ("其他", "dim"))
        row["label"], row["color"] = style
    stats = _tool_stats(days, set(cold if cold is not None else cold_tool_names()))
    stats["skills"] = _skill_list()
    return {"contract": "voice-trace/1", "threadId": thread_id, "rows": rows, "tools": stats}


if __name__ == "__main__":   # 手工查看：python voice_trace.py
    print(json.dumps(build(limit=20), ensure_ascii=False, indent=1)[:4000])
