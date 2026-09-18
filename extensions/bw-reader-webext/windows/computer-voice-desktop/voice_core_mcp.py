# -*- coding: utf-8 -*-
"""语音核心的 MCP 服务器（stdio）：让后台文字模型自己决定要不要开口。

注册到 ~/.codex/config.toml 的 [mcp_servers.voice_core] 后，后台线程模型在任何一轮里都能调：
  voice_status          语音核心状态（会话在不在、谁在说话、设备）
  voice_session_start   开一场语音会话（用当前设置的设备；App 连语音也会走这条线）
  voice_session_stop    结束会话
  voice_say             让语音模型立刻念一句（appendSpeech）
  voice_tell            往语音模型上下文里追加一句（appendText；闲时它会接一句）
典型用法：定时提醒到期 → 后台一轮 turn/start 收到"提醒到期"→ 它自己 voice_session_start 再 voice_say 口头提醒。

纯 stdlib：JSON-RPC 2.0 按行读写 stdout；HTTP 调本机 43131。不依赖任何 MCP SDK，也不碰音频。
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import urllib.error
import urllib.request

CORE_URL = os.environ.get("BW_VOICE_CORE_URL", "http://127.0.0.1:43131")
PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {"name": "voice_status", "description": "语音核心状态：会话状态（idle/connected…）、会话号、用户是否在说话、设备、桥标记。开口前先看一眼。",
     "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "voice_session_start", "description": "开一场语音会话（约 3 秒建立）。已经在通话中则直接返回。开完后用 voice_say 说话。",
     "inputSchema": {"type": "object", "properties": {"reason": {"type": "string", "description": "为什么要开口（写进日志）"}}, "additionalProperties": False}},
    {"name": "voice_session_stop", "description": "结束当前语音会话（挂断）。默认等语音模型把正在念/待念的那句说完再挂。用户告别、要求关语音、事情办完、提醒已送达无需回复、长时间无人说话时都应调用。",
     "inputSchema": {"type": "object", "properties": {"afterSpeech": {"type": "boolean", "description": "等当前那句念完再挂，默认 true"},
                                                     "graceSeconds": {"type": "number", "description": "最多等多少秒，默认 10"},
                                                     "reason": {"type": "string", "description": "为什么结束（写进日志）"}}, "additionalProperties": False}},
    {"name": "kj_node_ensure", "description": ("查找或创建知识节点，一步到位（用户 2026-09-15）。按名称在本地节点库找：名称或别名完全一致 → 直接返回该节点；"
        "没有 → 按给的 kind/aliases/summary 新建并返回新编号。返回 {ok, nodeId, created, matched, candidates}。制卡（reader_anki_draft 的 nodeIds）前用它拿编号，"
        "不要再自己跑脚本分两步。有近似但不完全一致的候选时也会新建，并把候选列在 candidates 里 —— 你若认为其中某个就是同一概念，用返回的 nodeId 之外那个即可。"),
     "inputSchema": {"type": "object", "properties": {"name": {"type": "string", "description": "节点名称（书里的叫法）"},
                                                     "kind": {"type": "string", "description": "concept|person|method|object|event|problem|analysis，默认 concept"},
                                                     "aliases": {"type": "array", "items": {"type": "string"}, "description": "别名（英/日原名等）"},
                                                     "summary": {"type": "string", "description": "一句话说明（新建时用）"}},
                     "required": ["name"], "additionalProperties": False}},
    {"name": "voice_call", "description": ("给用户的 iPad 打一通电话（穿透静音/专注，接通后把 text 念出来；他已经在通话中就直接念）。"
        "只用于**必须现在让他知道**的事：每次推送都会真的响铃，接听后 iPad 会强制切到阅读器前台。结果 outcome：answered=接通并念了；"
        "downgraded=拒接或两次没人接（别再打，改用通知）；blocked=没拨（看 error）。阻塞最长约 3 分钟。"),
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string", "description": "接通后念的话"},
                                                     "title": {"type": "string", "description": "来电界面上显示的一句话（默认取 text 开头）"},
                                                     "reason": {"type": "string", "description": "为什么打（写进日志）"}},
                     "required": ["text"], "additionalProperties": False}},
    {"name": "voice_say", "description": "让语音模型立刻把这段话念出来（近乎原文）。会话没开会先自动开。", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False}},
    {"name": "schedule_list", "description": "列出所有定时任务：id、名称、周期、下次运行、上次状态与摘要。", "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}},
    {"name": "schedule_create", "description": ("登记或覆盖一条定时任务（自建调度器，不依赖桌面 Codex）。flow 用 bw-reader-skill-flow/1：steps 里每步恰好是 "
        "command（本地脚本参数列表，输出一行 JSON）/ tool（阅读器 MCP 工具名+args）/ needs_ai（prompt+input+images+schema，单独用便宜模型跑一次，不带上下文）/ "
        "deliver（mode=notify|voice|say|call|log，text 模板；call=到点给 iPad 打电话、接通后念 text，只用于必须让他马上知道的事）之一；引用前面步骤用 {\"$from\":\"<id>\",\"path\":\"a.b\"} 或字符串里 {{id.path}}；"
        "when 条件用 {$from,path,eq|ne|in|exists}。schedule：{type:daily,time:HH:MM} / {type:weekly,days:[MO..SU],time} / {type:hourly,intervalHours} / "
        "{type:once,at:ISO 本地时间} / {type:manual}。登记时把流程写清楚：以后每次运行都不带对话上下文。"
        "例：明早 7 点打电话叫起床 = {id:'wake-0915', flow:{contract:'bw-reader-skill-flow/1', name:'起床电话', schedule:{type:'once', at:'2026-09-15T07:00:00'}, "
        "steps:[{id:'ring', deliver:true, mode:'call', title:'起床', text:'七点了，该起床了'}]}}（deliver 的字段写在步骤里，或写成 deliver:{mode,title,text} 对象也可以）"),
     "inputSchema": {"type": "object", "properties": {"id": {"type": "string", "description": "字母数字-_"}, "flow": {"type": "object"}, "enabled": {"type": "boolean"}},
                     "required": ["id", "flow"], "additionalProperties": False}},
    {"name": "schedule_delete", "description": "删除一条定时任务（含运行记录）。", "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": False}},
    {"name": "schedule_run_now", "description": "立刻跑一次某条定时任务（独立子进程，结果之后用 schedule_runs 看）。", "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"], "additionalProperties": False}},
    {"name": "schedule_enable", "description": "启用/停用一条定时任务。", "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}, "enabled": {"type": "boolean"}}, "required": ["id", "enabled"], "additionalProperties": False}},
    {"name": "schedule_runs", "description": "看某条定时任务最近几次运行：状态、耗时、每步结果摘要、错误。", "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}, "limit": {"type": "integer"}}, "required": ["id"], "additionalProperties": False}},
    {"name": "voice_transcript", "description": (
        "看最近几句**语音对话**（用户和语音模型各说了什么），每条带时刻与"
        "「多少秒前」。用在：你让语音模型念了一句之后，核对它到底念没念、"
        "用户回应了什么、这件事算不算办完 —— 据此决定下一步（再说一遍、"
        "改打电话、还是收尾挂断）。别靠猜：voice_say 返回成功只代表投递成功，"
        "不代表他听见了、更不代表他同意了。"),
     "inputSchema": {"type": "object", "properties": {
         "limit": {"type": "integer", "description": "最多看几条，默认 12，上限 50"},
         "sinceSeconds": {"type": "number", "description": "只看最近这么多秒内的（0 = 不限）"}},
         "additionalProperties": False}},
    {"name": "voice_tell", "description": "往语音模型的对话上下文追加一句（不保证不出声：闲时它会接一句）。", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False}},
]


def http(method: str, path: str, body: dict | None = None, timeout: float = 150) -> dict:
    data = json.dumps(body or {}, ensure_ascii=False).encode("utf-8") if method == "POST" else None
    req = urllib.request.Request(CORE_URL + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "msg": f"HTTP {e.code}"}
    except Exception as e:
        return {"ok": False, "msg": f"语音核心不可达（{e}）。它由 ReaderPC 托管，几秒后再试；或在 ReaderPC 界面点「启动语音核心」。"}


KJ_CLI = pathlib.Path(r"C:\tmp\reader-card-anchor-release\scripts\kj\cli.py")
KJ_PROJECT = r"C:\tmp\reader-card-anchor-release"
KJ_PYTHON = r"C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe"


def _kj_cli(argv: list) -> dict:
    import subprocess
    env = dict(os.environ); env["CLAUDE_PROJECT"] = KJ_PROJECT
    p = subprocess.run([KJ_PYTHON, str(KJ_CLI)] + argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, env=env,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for line in reversed((p.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return {"ok": False, "error": (p.stderr or p.stdout or "")[-300:], "exit": p.returncode}


def _norm(s) -> str:
    return "".join(ch for ch in str(s or "").lower() if not ch.isspace())


def kj_node_ensure(args: dict) -> dict:
    name = str(args.get("name") or "").strip()
    if not name:
        return {"ok": False, "msg": "name 不能为空"}
    found = _kj_cli(["search", name, "--limit", "8"])
    local = found.get("local") or [] if isinstance(found, dict) else []
    key = _norm(name)
    for n in local:
        names = [n.get("name")] + list(n.get("aliases") or [])
        if any(_norm(x) == key for x in names if x):
            return {"ok": True, "nodeId": n.get("id"), "created": False, "matched": "exact", "name": n.get("name"),
                    "candidates": [{"id": x.get("id"), "name": x.get("name")} for x in local[:5]]}
    argv = ["node-create", "--name", name, "--kind", str(args.get("kind") or "concept")]
    for a in (args.get("aliases") or [])[:8]:
        if str(a).strip():
            argv += ["--alias", str(a).strip()]
    if args.get("summary"):
        argv += ["--summary", str(args["summary"])[:300]]
    made = _kj_cli(argv)
    if not isinstance(made, dict) or not made.get("ok", True) or not made.get("node_id"):
        return {"ok": False, "msg": "新建节点失败", "detail": made, "candidates": [{"id": x.get("id"), "name": x.get("name")} for x in local[:5]]}
    out = {"ok": True, "nodeId": made.get("node_id"), "created": True, "matched": None, "name": name,
           "candidates": [{"id": x.get("id"), "name": x.get("name")} for x in local[:5]]}
    if made.get("possible_duplicate_of"):
        out["possibleDuplicateOf"] = made["possible_duplicate_of"]
        out["hint"] = made.get("hint")
    return out


def call_tool(name: str, args: dict) -> dict:
    if name == "schedule_list":
        return http("GET", "/tasks")
    if name == "schedule_create":
        return http("POST", "/tasks/upsert", {"id": args.get("id"), "flow": args.get("flow"), "enabled": args.get("enabled", True)})
    if name == "schedule_delete":
        return http("POST", "/tasks/delete", {"id": args.get("id")})
    if name == "schedule_run_now":
        return http("POST", "/tasks/run", {"id": args.get("id")})
    if name == "schedule_enable":
        return http("POST", "/tasks/enable", {"id": args.get("id"), "enabled": args.get("enabled", True)})
    if name == "schedule_runs":
        return http("GET", "/tasks/runs?id=%s&limit=%d" % (args.get("id"), int(args.get("limit") or 10)))
    if name == "voice_status":
        st = http("GET", "/status", timeout=10)
        if st.get("ok") is False:
            return st
        s = st.get("session") or {}
        return {"state": s.get("state"), "sessionNo": s.get("sessionNo"), "seconds": s.get("seconds"), "userSpeaking": s.get("userSpeaking"),
                "backendBusy": s.get("backendBusy"), "lastError": s.get("lastError"), "input": (st.get("settings") or {}).get("inputDevice"),
                "output": (st.get("settings") or {}).get("outputDevice"), "bridgeFlag": st.get("bridgeFlag"),
                "recentTranscripts": [f"{x.get('role')}: {x.get('text')}" for x in (st.get("transcripts") or [])[-4:]]}
    if name == "voice_transcript":
        return http("GET", "/transcript?limit=%d&sinceSeconds=%s" % (
            int(args.get("limit") or 12), float(args.get("sinceSeconds") or 0)), timeout=10)
    if name == "voice_session_start":
        return http("POST", "/session/start", {"reason": args.get("reason") or "backend"})
    if name == "kj_node_ensure":
        return kj_node_ensure(args)
    if name == "voice_call":
        text = str(args.get("text") or "").strip()
        if not text:
            return {"ok": False, "msg": "text 不能为空"}
        return http("POST", "/call", {"text": text, "title": args.get("title") or "", "ntf": "misc", "reason": args.get("reason") or "backend"}, timeout=270)
    if name == "voice_session_stop":
        return http("POST", "/session/stop", {"afterSpeech": args.get("afterSpeech", True) is not False,
                                              "graceSeconds": args.get("graceSeconds") or 10, "reason": args.get("reason") or "backend"})
    if name in ("voice_say", "voice_tell"):
        text = str(args.get("text") or "").strip()
        if not text:
            return {"ok": False, "msg": "text 不能为空"}
        st = http("GET", "/status", timeout=10)
        if (st.get("session") or {}).get("state") != "connected":
            started = http("POST", "/session/start", {"reason": name})
            if started.get("ok") is False:
                return {"ok": False, "msg": "会话没开起来：" + str(started.get("msg"))}
        r = http("POST", "/say" if name == "voice_say" else "/tell", {"text": text, "role": "developer"})
        return r
    return {"ok": False, "msg": f"未知工具 {name}"}


def main() -> int:
    out = sys.stdout.buffer
    for raw in sys.stdin.buffer:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line.decode("utf-8"))
        except ValueError:
            continue
        mid = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params") or {}
        resp: dict | None = None
        if method == "initialize":
            resp = {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {}}, "serverInfo": {"name": "voice_core", "version": "0.1"}}
        elif method == "notifications/initialized" or method.startswith("notifications/"):
            continue
        elif method == "tools/list":
            resp = {"tools": TOOLS}
        elif method == "tools/call":
            result = call_tool(str(params.get("name")), params.get("arguments") or {})
            resp = {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "isError": result.get("ok") is False}
        elif method == "ping":
            resp = {}
        elif mid is not None:
            out.write((json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"unknown method {method}"}}) + "\n").encode("utf-8"))
            out.flush()
            continue
        if mid is not None and resp is not None:
            out.write((json.dumps({"jsonrpc": "2.0", "id": mid, "result": resp}, ensure_ascii=False) + "\n").encode("utf-8"))
            out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
