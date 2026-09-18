#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bw_flow_runner — 定时任务的执行器（2026-09-14 用户设计）。

一个定时任务就是一份 flow.json（沿用侧栏「保存为工具」的 `bw-reader-skill-flow/1` 格式），
只是不在 Codex 会话里跑，而是在这里按步执行：

- `command` 步：本地脚本，subprocess 跑，`json: true` 时取最后一行能解析的 JSON 当输出。
- `tool` 步：阅读器 MCP 工具，直接 stdio 连桥的 `--reader-context-mcp`，不经 Codex。
- `needs_ai` 步：需要模型判断的一步，单独起一次 `codex exec`（便宜模型、`--ephemeral`、
  `--ignore-user-config`、不挂任何 MCP），`-i` 传图，`--output-schema` 把输出锁成 JSON。
  主 AI 登记任务时已经把流程写清楚了，这一步的模型不需要上下文，也不输出上下文。
- `deliver` 步：投递 —— notify（走通知系统 `replication_notifications.py create --deliver auto`，
  按情况路由）/ voice（交给语音核心后台起一轮，由它决定要不要开口）/ log（只记）。

引用：`{"$from": "<id>", "path": "a.b[0]"}` 取前面步骤的输出；字符串里 `{{id.path}}` 同义。
`when`：{"$from"…, "eq"|"ne"|"in"|"exists"} 不满足就跳过这一步。
`$spread`：{"$spread": {"$from"…}, "format": "--observed {code}={level}"} 把列表展开成多个参数。

每次运行写 `<task>/runs.jsonl` 一行 + `memory.md` 一行；退出码 0=ok，2=flow 失败，3=定义错。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

FLOW_CONTRACT = "bw-reader-skill-flow/1"
LOCALAPPDATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
BWREADER = LOCALAPPDATA / "BWReader"
TASKS_ROOT = BWREADER / "scheduled-tasks"
VOICE_CORE_URL = "http://127.0.0.1:43131"
BRIDGE_EXE = Path.home() / "bw-computer-voice-bridge" / "native-host" / "bw-computer-voice-audio.exe"
BRIDGE_STATE = Path.home() / "bw-computer-voice-bridge" / "runtime" / "reader-context-snapshot.json"
PYTHON = sys.executable if sys.executable and not sys.executable.lower().endswith("pythonw.exe") else \
    str(Path(sys.executable).with_name("python.exe"))
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DEFAULT_MODEL = "gpt-5.6-terra"


def codex_exe() -> str:
    """直接用 codex.exe 本体：npm 的 codex.cmd/shell 包装在无控制台子进程里不可靠。"""
    import shutil as _sh
    appdata = Path(os.environ.get("APPDATA", ""))
    for cand in (appdata / "npm" / "node_modules" / "@openai" / "codex" / "node_modules" / "@openai" / "codex-win32-x64"
                 / "vendor" / "x86_64-pc-windows-msvc" / "bin" / "codex.exe",):
        if cand.is_file():
            return str(cand)
    return _sh.which("codex.cmd") or _sh.which("codex") or "codex"
DEFAULT_EFFORT = "low"


class FlowError(RuntimeError):
    pass


def _expand(s: str) -> str:
    return os.path.expandvars(s) if isinstance(s, str) else s


# ---------- 引用解析 ----------
def _path_get(obj: Any, path: str) -> Any:
    if not path:
        return obj
    cur = obj
    for tok in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        if tok.startswith("["):
            idx = int(tok[1:-1])
            cur = cur[idx] if isinstance(cur, list) and -len(cur) <= idx < len(cur) else None
        elif isinstance(cur, dict):
            cur = cur.get(tok)
        else:
            return None
        if cur is None:
            return None
    return cur


class Ctx:
    def __init__(self, task_dir: Path, flow: dict):
        self.task_dir = task_dir
        self.flow = flow
        self.outputs: dict[str, Any] = {}
        self.log: list[dict] = []

    def ref(self, spec: Any) -> Any:
        """把 $from / {{}} / $spread 解析成值。字典/列表递归。"""
        if isinstance(spec, dict):
            if "$from" in spec:
                if spec["$from"] not in self.outputs:
                    # 被 when 跳过的步骤没有输出：引用它就是 default（通常 None）。id 写错由 lint 拦。
                    return spec.get("default")
                val = _path_get(self.outputs[spec["$from"]], spec.get("path", ""))
                return val if val is not None else spec.get("default")
            if "$ai" in spec:   # 兼容侧栏格式：$ai 就是那一步的输出
                return self.ref({"$from": spec["$ai"], "path": spec.get("path", "")})
            return {k: self.ref(v) for k, v in spec.items()}
        if isinstance(spec, list):
            return [self.ref(v) for v in spec]
        if isinstance(spec, str):
            def sub(m):
                val = _path_get(self.outputs.get(m.group(1)), m.group(2) or "")
                return "" if val is None else (json.dumps(val, ensure_ascii=False) if isinstance(val, (dict, list)) else str(val))
            return _expand(re.sub(r"\{\{(\w+)(?:\.([^}]*))?\}\}", sub, spec))
        return spec

    def argv(self, args: list) -> list[str]:
        out: list[str] = []
        for a in args:
            if isinstance(a, dict) and "$spread" in a:
                items = self.ref(a["$spread"]) or []
                fmt = a.get("format", "{}")
                if isinstance(items, dict):
                    items = [{"code": k, "level": v, "key": k, "value": v} for k, v in items.items()]
                # format 是列表时每个元素各成一个参数（"--observed", "{code}={level}"）；字符串则整体一个参数。
                parts = fmt if isinstance(fmt, list) else [fmt]
                for it in items:
                    for f in parts:
                        out.append(f.format(**it) if isinstance(it, dict) else f.format(it))
            else:
                v = self.ref(a)
                out.append(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
        return out

    def when(self, cond: Any) -> bool:
        if not cond:
            return True
        val = self.ref({k: v for k, v in cond.items() if k in ("$from", "path", "default")})
        if "exists" in cond:
            return (val is not None and val != "") == bool(cond["exists"])
        if "eq" in cond:
            return val == cond["eq"]
        if "ne" in cond:
            return val != cond["ne"]
        if "in" in cond:
            return val in (cond["in"] or [])
        return bool(val)


# ---------- 各类步骤 ----------
def _last_json_line(text: str) -> Any:
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{") or line.startswith("["):
            try:
                return json.loads(line)
            except ValueError:
                continue
    return None


def run_command(ctx: Ctx, step: dict) -> Any:
    argv = ctx.argv(step["command"])
    if argv and argv[0].lower() in ("python", "python3", "python.exe"):
        argv[0] = PYTHON
    timeout = float(step.get("timeout") or 300)
    cwd = _expand(step.get("cwd")) if step.get("cwd") else str(ctx.task_dir)
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, cwd=cwd, creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)
    out = {"exit": proc.returncode, "stdout": (proc.stdout or "")[-6000:], "stderr": (proc.stderr or "")[-2000:]}
    if step.get("json", True):
        parsed = _last_json_line(proc.stdout)
        if isinstance(parsed, dict):
            out = {**parsed, "_exit": proc.returncode}
        elif parsed is not None:
            out = {"result": parsed, "_exit": proc.returncode}
    if proc.returncode != 0 and not step.get("allow_failure"):
        raise FlowError("命令退出码 %s：%s" % (proc.returncode, (proc.stderr or proc.stdout or "")[-300:].strip()))
    return out


class McpStdio:
    """最小 MCP 客户端：按行 JSON-RPC，只用 initialize / tools/call。"""

    def __init__(self, argv: list[str]):
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                     text=True, encoding="utf-8", creationflags=NO_WINDOW)
        self._id = 0
        self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "bw-flow-runner", "version": "1"}})
        self.notify("notifications/initialized")

    def notify(self, method: str, params: dict | None = None):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()

    def request(self, method: str, params: dict, timeout: float = 120) -> Any:
        self._id += 1
        rid = self._id
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                raise FlowError("MCP 进程结束了")
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == rid:
                if "error" in msg:
                    raise FlowError("MCP %s: %s" % (method, msg["error"].get("message")))
                return msg.get("result")
        raise FlowError("MCP %s 超时" % method)

    def close(self):
        try:
            self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


_mcp: McpStdio | None = None


def run_tool(ctx: Ctx, step: dict) -> Any:
    global _mcp
    if _mcp is None:
        _mcp = McpStdio([str(BRIDGE_EXE), "--reader-context-mcp", "--state", str(BRIDGE_STATE)])
    result = _mcp.request("tools/call", {"name": step["tool"], "arguments": ctx.ref(step.get("args") or {})})
    texts = [c.get("text", "") for c in (result or {}).get("content", []) if c.get("type") == "text"]
    joined = "\n".join(texts)
    try:
        return json.loads(joined)
    except ValueError:
        return {"text": joined, "isError": bool((result or {}).get("isError"))}


def run_ai(ctx: Ctx, step: dict, flow: dict) -> Any:
    prompt = ctx.ref(step["prompt"])
    inputs = ctx.ref(step.get("input") or {})
    images = [p for p in (ctx.ref(step.get("images") or [])) if isinstance(p, str) and p and Path(p).is_file()]
    text = prompt
    if step.get("images") and not images:
        # 拍照失败/图不在：不让流程死在这里，把事实告诉模型，由 prompt 里的规则决定（垃圾检查=一律 uncertain）
        text += chr(10) * 2 + "【注意】本次没有可用的照片（拍照失败或文件不存在），你看不到任何画面。"
    if inputs:
        text += "\n\n输入数据（JSON）：\n" + json.dumps(inputs, ensure_ascii=False)
    text += "\n\n不要调用任何工具。只输出一个 JSON 对象，不要多余文字。"
    model = step.get("model") or flow.get("model") or DEFAULT_MODEL
    effort = step.get("effort") or flow.get("effort") or DEFAULT_EFFORT
    with tempfile.TemporaryDirectory(prefix="bw-flow-ai-") as tmp:
        out_path = Path(tmp) / "last.txt"
        argv = [codex_exe(), "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check", "-s", "read-only",
                "-C", tmp, "-m", model, "-c", "model_reasoning_effort=" + json.dumps(effort), "-o", str(out_path)]
        if step.get("schema"):
            schema_path = Path(tmp) / "schema.json"
            schema_path.write_text(json.dumps(step["schema"], ensure_ascii=False), encoding="utf-8")
            argv += ["--output-schema", str(schema_path)]
        for img in images:
            argv += ["-i", img]
        # 提示词走 stdin（官方路径）：`-i` 是可变参数，跟在后面的位置参数会被当成图片路径吞掉；
        # 而且 stdin 若是打开的管道，codex exec 会读到 EOF 才开始 —— 所以必须显式喂完并关掉。
        proc = subprocess.run(argv, input=text, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=float(step.get("timeout") or 240), creationflags=NO_WINDOW)
        raw = out_path.read_text(encoding="utf-8").strip() if out_path.exists() else ""
    if not raw:
        raise FlowError("codex exec 没有产出（exit %s）：%s" % (proc.returncode, (proc.stderr or "")[-300:]))
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip())
    try:
        return json.loads(raw)
    except ValueError:
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                pass
        raise FlowError("模型输出不是 JSON：" + raw[:200])


def _post(url: str, body: dict, timeout: float = 30) -> dict:
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read() or b"{}")


def run_deliver(ctx: Ctx, step: dict, flow: dict) -> Any:
    # 两种写法都认：扁平 {deliver:true, mode, text,…}（侧栏工具流程格式）和嵌套 {deliver:{mode, text,…}}（AI 按描述常这么写）。
    # 2026-09-15 实录：嵌套写法下 text 读成空 → 电话没打出去。
    if isinstance(step.get("deliver"), dict):
        step = {**step, **step["deliver"]}
    mode = step.get("mode") or "notify"
    text = ctx.ref(step.get("text") or "")
    title = ctx.ref(step.get("title") or flow.get("name") or "定时任务")
    if not text:
        return {"delivered": False, "reason": "empty"}
    if mode == "log":
        return {"delivered": True, "mode": "log", "text": text}
    if mode == "voice":
        r = _post(VOICE_CORE_URL + "/turn", {"text": "【定时任务·%s】%s" % (flow.get("name"), text)})
        return {"delivered": bool(r.get("ok")), "mode": "voice"}
    if mode == "say":
        # fallback=turn：语音不在线时不静默丢掉，交后台决定说/打电话/等
        # （2026-09-18：此前语音离线时 /say 照回 ok:true，delivered 记成 true 而没人听见）。
        r = _post(VOICE_CORE_URL + "/say", {"text": text, "fallback": "turn"})
        return {"delivered": bool(r.get("ok")), "mode": "say",
                "spoken": bool(r.get("spoken")), "via": r.get("via")}
    if mode == "call":
        # 先建一条 deliver=call 的待办（留档、去重、拒接后自动降级都靠它），再让运行器拨号并在接通后念 text
        argv = [PYTHON, str(BWREADER / "replication_notifications.py"), "create", "--kind", step.get("kind", "reminder"),
                "--title", str(title)[:120], "--body", str(text)[:2000], "--source", "ai-task", "--audience", "user",
                "--deliver", "call", "--end", step.get("end", "never")]
        dedupe = ctx.ref(step.get("dedupe_key") or "")
        if dedupe:
            argv += ["--dedupe-key", str(dedupe)]
        ntf = "misc"
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, creationflags=NO_WINDOW)
            parsed = _last_json_line(proc.stdout) or {}
            if isinstance(parsed, dict) and parsed.get("notificationId"):
                ntf = str(parsed["notificationId"])
        except Exception:   # noqa: BLE001 —— 待办建不成也照样打电话：电话才是这一步的目的
            pass
        r = _post(VOICE_CORE_URL + "/call", {"text": text, "title": str(title)[:80], "ntf": ntf, "reason": "scheduled:" + str(flow.get("name") or "")}, timeout=280)
        return {"delivered": bool(r.get("ok")), "mode": "call", "outcome": r.get("outcome"), "spoken": r.get("spoken"),
                "notificationId": (ntf if ntf != "misc" else None), "error": r.get("error")}
    # notify：通知系统，--deliver auto 按情况路由（提示板/语音/静默）
    argv = [PYTHON, str(BWREADER / "replication_notifications.py"), "create", "--kind", step.get("kind", "task-report"),
            "--title", str(title)[:120], "--body", str(text)[:2000], "--source", "ai-task", "--audience", "user",
            "--deliver", step.get("deliver", "auto"), "--end", step.get("end", "never")]
    dedupe = ctx.ref(step.get("dedupe_key") or "")
    if dedupe:
        argv += ["--dedupe-key", str(dedupe)]
    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60, creationflags=NO_WINDOW)
    parsed = _last_json_line(proc.stdout) or {}
    return {"delivered": proc.returncode == 0, "mode": "notify", "exit": proc.returncode,
            "notificationId": (parsed.get("notificationId") if isinstance(parsed, dict) else None),
            "stdout": (proc.stdout or "")[-400:], "stderr": (proc.stderr or "")[-300:]}


# ---------- 主流程 ----------
def lint(flow: dict) -> list[str]:
    errs = []
    if flow.get("contract") != FLOW_CONTRACT:
        errs.append("contract 必须是 " + FLOW_CONTRACT)
    if not flow.get("name"):
        errs.append("缺 name")
    steps = flow.get("steps")
    if not isinstance(steps, list) or not steps:
        errs.append("steps 必须是非空列表")
        return errs
    ids: set[str] = set()
    for i, s in enumerate(steps):
        w = "steps[%d]" % i
        if not isinstance(s, dict) or not s.get("id"):
            errs.append(w + ": 缺 id"); continue
        if s["id"] in ids:
            errs.append(w + ": id 重复 " + s["id"])
        ids.add(s["id"])
        kinds = [k for k in ("command", "tool", "needs_ai", "deliver") if s.get(k)]
        if len(kinds) != 1:
            errs.append(w + ": 必须恰好是 command / tool / needs_ai / deliver 之一")
        if s.get("needs_ai") and not s.get("prompt"):
            errs.append(w + ": needs_ai 要有 prompt")
        if s.get("command") and not isinstance(s["command"], list):
            errs.append(w + ": command 必须是参数列表")
        # $from 只能引用更早的步骤（被 when 跳过的运行时按缺省处理，但 id 必须存在）
        earlier = {x.get("id") for x in steps[:i] if isinstance(x, dict)}
        for ref_id in re.findall(r'"\$from":\s*"([^"]+)"', json.dumps(s, ensure_ascii=False)) + \
                re.findall(r"\{\{(\w+)(?:\.[^}]*)?\}\}", json.dumps(s, ensure_ascii=False)):
            if ref_id not in earlier:
                errs.append(w + ": 引用了不存在或更晚的步骤 %r" % ref_id)
    sched = flow.get("schedule")
    if sched is not None and not isinstance(sched, dict):
        errs.append("schedule 必须是对象")
    return errs


def run_flow(task_dir: Path, flow: dict) -> dict:
    ctx = Ctx(task_dir, flow)
    started = time.time()
    status, error = "ok", None
    try:
        for step in flow["steps"]:
            sid = step["id"]
            t0 = time.time()
            if not ctx.when(step.get("when")):
                ctx.log.append({"id": sid, "status": "skipped", "ms": 0})
                continue
            try:
                if step.get("command"):
                    out = run_command(ctx, step)
                elif step.get("tool"):
                    out = run_tool(ctx, step)
                elif step.get("needs_ai"):
                    out = run_ai(ctx, step, flow)
                else:
                    out = run_deliver(ctx, step, flow)
            except (FlowError, subprocess.TimeoutExpired, OSError) as exc:
                ctx.log.append({"id": sid, "status": "error", "ms": int((time.time() - t0) * 1000), "error": str(exc)[:500]})
                if step.get("on_error") == "continue":
                    ctx.outputs[sid] = {"_error": str(exc)[:500]}
                    continue
                raise
            ctx.outputs[sid] = out
            brief = json.dumps(out, ensure_ascii=False)[:400] if not isinstance(out, str) else out[:400]
            ctx.log.append({"id": sid, "status": "ok", "ms": int((time.time() - t0) * 1000), "brief": brief})
    except Exception as exc:  # noqa: BLE001
        status, error = "error", str(exc)[:600]
    finally:
        global _mcp
        if _mcp is not None:
            _mcp.close()
            _mcp = None
    record = {"startedAt": _dt.datetime.fromtimestamp(started).astimezone().isoformat(timespec="seconds"),
              "ms": int((time.time() - started) * 1000), "status": status, "error": error, "steps": ctx.log}
    summary = _summary(ctx, flow, status, error)
    record["summary"] = summary
    with open(task_dir / "runs.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    with open(task_dir / "memory.md", "a", encoding="utf-8") as f:
        f.write("%s — %s — %s\n" % (record["startedAt"], status.upper(), summary))
    return record


def _summary(ctx: Ctx, flow: dict, status: str, error: str | None) -> str:
    if error:
        return error[:300]
    tmpl = flow.get("summary_template")
    if tmpl:
        try:
            return str(ctx.ref(tmpl))[:300]
        except Exception:
            pass
    for step in reversed(flow["steps"]):
        out = ctx.outputs.get(step["id"])
        if isinstance(out, dict) and out.get("status"):
            return "%s: %s" % (step["id"], out.get("status"))
    return "完成 %d 步" % len([l for l in ctx.log if l["status"] == "ok"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="定时任务 flow 执行器")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("task_dir")
    l = sub.add_parser("lint"); l.add_argument("flow_json")
    a = ap.parse_args(argv)
    if a.cmd == "lint":
        flow = json.loads(Path(a.flow_json).read_text(encoding="utf-8"))
        errs = lint(flow)
        print(json.dumps({"ok": not errs, "errors": errs}, ensure_ascii=False))
        return 0 if not errs else 3
    task_dir = Path(a.task_dir)
    if not task_dir.is_absolute():
        task_dir = TASKS_ROOT / task_dir
    flow = json.loads((task_dir / "flow.json").read_text(encoding="utf-8"))
    errs = lint(flow)
    if errs:
        print(json.dumps({"ok": False, "errors": errs}, ensure_ascii=False))
        return 3
    record = run_flow(task_dir, flow)
    print(json.dumps({"ok": record["status"] == "ok", "status": record["status"], "summary": record["summary"],
                      "ms": record["ms"], "error": record["error"]}, ensure_ascii=False))
    return 0 if record["status"] == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
