#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bw_skill_build.py — 把 flow.json 编成可靠的 run.js，并用真实轨迹干跑一遍（2026-09-13）。

用户要的"预先提供一个什么东西保证封装的可靠性"就是这个：AI 只写 flow.json（步骤、参数、
上一步→下一步的引用）和 SKILL.md 的文字；运行器由模板生成，参数按工具的 inputSchema 校验，
引用必须指向更早的步骤，最后拿录下来的轨迹当假工具输出把 run.js 在 node 里干跑一遍。
四道都过才算"编好"。

用法：
    bw_skill_build.py <skill目录> [--trace trace.json] [--refresh-tools] [--no-dryrun]
    bw_skill_build.py --tools            # 只刷新工具面缓存并打印工具名

skill 目录里要有 flow.json（见 FLOW_CONTRACT）；SKILL.md 可选（有 bw-flow 标记就把 run.js 回填进去）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

FLOW_CONTRACT = "bw-reader-skill-flow/1"
DEFAULT_SERVER = "reader_snapshot"
TOOLS_CACHE_MAX_AGE = 24 * 3600
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
STEP_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")

HERE = Path(__file__).resolve().parent


def kit_dir() -> Path:
    return HERE


def local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))


def tools_cache_path() -> Path:
    return local_appdata() / "BWReader" / "skill-kit" / "tools-list.json"


class BuildError(RuntimeError):
    pass


# ── 工具面：从桥的 MCP 拿 tools/list（带 inputSchema） ──────────────

def _mcp_command_from_codex_config() -> list[str] | None:
    """~/.codex/config.toml 里 [mcp_servers.reader_snapshot] 的 command+args。"""
    path = Path(os.environ.get("USERPROFILE") or Path.home()) / ".codex" / "config.toml"
    try:
        import tomllib
        with path.open("rb") as handle:
            cfg = tomllib.load(handle)
    except (OSError, ValueError):
        return None
    server = ((cfg.get("mcp_servers") or {}).get(DEFAULT_SERVER)) or {}
    command = server.get("command")
    args = server.get("args") or []
    if not isinstance(command, str) or not command:
        return None
    return [command] + [str(a) for a in args]


def fetch_tools_list(command: list[str] | None = None, timeout: float = 25.0) -> list[dict[str, Any]]:
    command = command or _mcp_command_from_codex_config()
    if not command:
        raise BuildError("找不到 reader_snapshot MCP 的启动命令（~/.codex/config.toml）")
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "bw-skill-build", "version": "1"}}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
    ]
    try:
        proc = subprocess.run(command, input="\n".join(lines) + "\n", capture_output=True, text=True,
                              encoding="utf-8", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BuildError("MCP tools/list 拿不到: %s" % type(exc).__name__) from exc
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if message.get("id") == 2 and isinstance(message.get("result"), dict):
            tools = message["result"].get("tools")
            if isinstance(tools, list) and tools:
                return tools
    raise BuildError("MCP tools/list 回应里没有工具（stderr: %s）" % proc.stderr[-300:])


def load_tools(*, refresh: bool = False, cache: Path | None = None) -> dict[str, dict[str, Any]]:
    cache = cache or tools_cache_path()
    if not refresh and cache.exists() and time.time() - cache.stat().st_mtime < TOOLS_CACHE_MAX_AGE:
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("tools"), list):
                return {t["name"]: t for t in data["tools"] if isinstance(t, dict) and "name" in t}
        except (OSError, ValueError):
            pass
    tools = fetch_tools_list()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"fetchedAt": time.time(), "tools": tools}, ensure_ascii=False), encoding="utf-8")
    return {t["name"]: t for t in tools if isinstance(t, dict) and "name" in t}


# ── JSON Schema 子集校验（type/required/properties/additionalProperties/enum/items/oneOf） ──

def _type_ok(value: Any, expected: Any) -> bool:
    types = expected if isinstance(expected, list) else [expected]
    for t in types:
        if t == "string" and isinstance(value, str):
            return True
        if t == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if t == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if t == "boolean" and isinstance(value, bool):
            return True
        if t == "object" and isinstance(value, dict):
            return True
        if t == "array" and isinstance(value, list):
            return True
        if t == "null" and value is None:
            return True
    return False


def _is_ref(value: Any) -> bool:
    return isinstance(value, dict) and ("$from" in value or "$ai" in value)


def validate_against(schema: dict[str, Any], value: Any, where: str, errors: list[str]) -> None:
    """引用（$from/$ai）在运行时才有值，跳过其类型检查；字面量按 schema 子集查。"""
    if _is_ref(value):
        return
    if "oneOf" in schema and isinstance(schema["oneOf"], list):
        sub_errors: list[list[str]] = []
        for option in schema["oneOf"]:
            errs: list[str] = []
            validate_against(option, value, where, errs)
            sub_errors.append(errs)
            if not errs:
                return
        errors.append("%s: 不满足 oneOf 的任一分支（%s）" % (where, "; ".join(e[0] for e in sub_errors if e)))
        return
    if "enum" in schema and value not in schema["enum"]:
        errors.append("%s: 值 %r 不在 enum %r 里" % (where, value, schema["enum"]))
        return
    if "type" in schema and not _type_ok(value, schema["type"]):
        errors.append("%s: 类型应为 %s，给的是 %s" % (where, schema["type"], type(value).__name__))
        return
    if isinstance(value, dict):
        props = schema.get("properties") or {}
        for key in schema.get("required") or []:
            if key not in value:
                errors.append("%s: 缺必填字段 %s" % (where, key))
        if schema.get("additionalProperties") is False:
            for key in value:
                if key not in props:
                    errors.append("%s: 多出字段 %s（工具不收）" % (where, key))
        for key, sub in props.items():
            if key in value and isinstance(sub, dict):
                validate_against(sub, value[key], where + "." + key, errors)
        if isinstance(value.get("pattern"), str):
            pass
    elif isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            validate_against(schema["items"], item, "%s[%d]" % (where, index), errors)
    if isinstance(value, str):
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append("%s: 超过 maxLength %s" % (where, schema["maxLength"]))
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append("%s: 短于 minLength %s" % (where, schema["minLength"]))
        if isinstance(schema.get("pattern"), str) and not re.search(schema["pattern"], value):
            errors.append("%s: 不匹配 pattern %s" % (where, schema["pattern"]))


# ── flow.json ────────────────────────────────────────────────────

def _walk_refs(value: Any, found: list[dict[str, Any]]) -> None:
    if _is_ref(value):
        found.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            _walk_refs(v, found)
    elif isinstance(value, list):
        for v in value:
            _walk_refs(v, found)


def lint_flow(flow: Any, tools: dict[str, dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    if not isinstance(flow, dict):
        return ["flow.json 必须是对象"]
    if flow.get("contract") != FLOW_CONTRACT:
        errors.append("contract 必须是 %s" % FLOW_CONTRACT)
    name = flow.get("name")
    if not isinstance(name, str) or not NAME_RE.match(name):
        errors.append("name 必须是 kebab-case（%s）" % NAME_RE.pattern)
    if not isinstance(flow.get("summary"), str) or not flow["summary"].strip():
        errors.append("summary 不能为空")
    triggers = flow.get("trigger")
    if not isinstance(triggers, list) or not triggers or not all(isinstance(t, str) and t.strip() for t in triggers):
        errors.append("trigger 必须是非空字符串数组（用户会怎么说）")
    steps = flow.get("steps")
    if not isinstance(steps, list) or not steps:
        return errors + ["steps 必须是非空数组"]
    seen: list[str] = []
    for index, step in enumerate(steps):
        where = "steps[%d]" % index
        if not isinstance(step, dict):
            errors.append(where + ": 必须是对象")
            continue
        sid = step.get("id")
        if not isinstance(sid, str) or not STEP_ID_RE.match(sid):
            errors.append(where + ": id 必须是 %s" % STEP_ID_RE.pattern)
            sid = None
        elif sid in seen:
            errors.append(where + ": id 重复 %s" % sid)
        refs: list[dict[str, Any]] = []
        if step.get("needs_ai"):
            if not isinstance(step.get("prompt"), str) or not step["prompt"].strip():
                errors.append(where + ": needs_ai 的步骤必须有 prompt（告诉模型要产出什么）")
            _walk_refs(step.get("input"), refs)
        else:
            tool = step.get("tool")
            server = step.get("server") or flow.get("server") or DEFAULT_SERVER
            if not isinstance(tool, str) or not tool:
                errors.append(where + ": 缺 tool")
            elif server == DEFAULT_SERVER and tool not in tools:
                errors.append(where + ": 工具 %s 不在工具面上（有：%s…）" % (tool, ", ".join(sorted(tools)[:8])))
            else:
                schema = (tools.get(tool) or {}).get("inputSchema") if server == DEFAULT_SERVER else None
                args = step.get("args", {})
                if not isinstance(args, dict):
                    errors.append(where + ": args 必须是对象")
                elif isinstance(schema, dict):
                    validate_against(schema, args, where + ".args", errors)
            _walk_refs(step.get("args"), refs)
        for ref in refs:
            if "$from" in ref and ref["$from"] not in seen:
                errors.append(where + ": $from 引用了还没跑到的步骤 %r" % ref["$from"])
            if "$ai" in ref and not any(isinstance(s, dict) and s.get("needs_ai") and s.get("id") == ref["$ai"] for s in steps[:index]):
                errors.append(where + ": $ai 引用的 %r 不是更早的 needs_ai 步骤" % ref["$ai"])
        if sid:
            seen.append(sid)
    return errors


def render_run_js(flow: dict[str, Any], runtime_path: Path | None = None) -> str:
    runtime = (runtime_path or (kit_dir() / "bw_flow_runtime.js")).read_text(encoding="utf-8")
    payload = json.dumps(flow, ensure_ascii=False, indent=2)
    if "__BW_FLOW__" not in runtime:
        raise BuildError("运行器模板缺 __BW_FLOW__ 占位")
    return runtime.replace("__BW_FLOW__", payload)


# ── 干跑：用轨迹当假工具 ─────────────────────────────────────────

_HARNESS = r"""
const __log = [];
const __trace = __BW_TRACE__;
const __calls = [];
const __store = new Map();
function text(v) { __log.push(String(typeof v === "string" ? v : JSON.stringify(v))); }
function image() {}
function audio() {}
function generatedImage() {}
function notify(v) { text(v); }
function store(k, v) { if (v === undefined) __store.delete(k); else __store.set(k, JSON.parse(JSON.stringify(v))); }
function load(k) { return __store.has(k) ? JSON.parse(JSON.stringify(__store.get(k))) : undefined; }
class __Exit extends Error {}
function exit() { throw new __Exit("exit"); }
const __byTool = {};
for (const step of __trace) { (__byTool[step.tool] = __byTool[step.tool] || []).push(step); }
const tools = new Proxy({}, { get(_, name) {
  if (typeof name !== "string" || !name.startsWith("mcp__")) return undefined;
  const bare = name.replace(/^mcp__[a-z_]+?__/, "");
  return async (args) => {
    __calls.push({ tool: bare, args });
    const queue = __byTool[bare] || [];
    const rec = queue.shift();
    if (!rec) return { content: [{ type: "text", text: JSON.stringify({ ok: true, dryrun: true, tool: bare }) }] };
    return rec.raw || { content: [{ type: "text", text: typeof rec.output === "string" ? rec.output : JSON.stringify(rec.output) }] };
  };
}});
globalThis.text = text; globalThis.image = image; globalThis.audio = audio; globalThis.generatedImage = generatedImage;
globalThis.notify = notify; globalThis.store = store; globalThis.load = load; globalThis.exit = exit; globalThis.tools = tools;
try {
  await (async () => {
__BW_RUN__
  })();
} catch (e) { if (!(e instanceof __Exit)) { __log.push("[harness] " + (e && e.stack || e)); process.exitCode = 3; } }
process.stdout.write(JSON.stringify({ log: __log, calls: __calls }));
"""


def dryrun(run_js: str, trace_steps: list[dict[str, Any]], node: str | None = None) -> dict[str, Any]:
    node = node or shutil.which("node")
    if not node:
        raise BuildError("找不到 node（干跑需要它）")
    harness = _HARNESS.replace("__BW_TRACE__", json.dumps(trace_steps, ensure_ascii=False)).replace("__BW_RUN__", run_js)
    with tempfile.TemporaryDirectory(prefix="bw-skill-dryrun-") as tmp:
        script = Path(tmp) / "dryrun.mjs"
        script.write_text(harness, encoding="utf-8")
        proc = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    try:
        out = json.loads(proc.stdout.strip() or "{}")
    except ValueError:
        raise BuildError("干跑输出不是 JSON: %s" % (proc.stderr or proc.stdout)[-400:])
    out["exitCode"] = proc.returncode
    return out


def assess_dryrun(flow: dict[str, Any], out: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if out.get("exitCode") not in (0, None):
        problems.append("run.js 抛了异常: " + "; ".join(l for l in out.get("log", []) if l.startswith("[harness]")))
    expected = [s["tool"] for s in flow["steps"] if not s.get("needs_ai")]
    called = [c["tool"] for c in out.get("calls", [])]
    first_ai = next((i for i, s in enumerate(flow["steps"]) if s.get("needs_ai")), None)
    if first_ai is None:
        if called != expected:
            problems.append("干跑调用顺序 %s ≠ 期望 %s" % (called, expected))
        if not any(l.startswith("{") and "bwFlowDone" in l for l in out.get("log", [])):
            problems.append("没有跑到 bwFlowDone（中途 fail 或 exit）")
    else:
        before = [s["tool"] for s in flow["steps"][:first_ai] if not s.get("needs_ai")]
        if called != before:
            problems.append("到第一个 needs_ai 前的调用 %s ≠ 期望 %s" % (called, before))
        if not any("bwFlowHandoff" in l for l in out.get("log", [])):
            problems.append("needs_ai 步骤没有产生 handoff")
    return problems


# ── SKILL.md 回填 ────────────────────────────────────────────────

def refill_skill_md(path: Path, run_js: str) -> bool:
    if not path.exists():
        return False
    text_md = path.read_text(encoding="utf-8")
    begin, end = "<!-- bw-flow:run.js:begin -->", "<!-- bw-flow:run.js:end -->"
    if begin not in text_md or end not in text_md:
        return False
    head, rest = text_md.split(begin, 1)
    _, tail = rest.split(end, 1)
    block = begin + "\n```js\n" + run_js.rstrip("\n") + "\n```\n" + end
    path.write_text(head + block + tail, encoding="utf-8")
    return True


def render_skill_md(flow: dict[str, Any], run_js: str, *, source: str) -> str:
    template = (kit_dir() / "SKILL.template.md").read_text(encoding="utf-8")
    steps_md = "\n".join(
        "%d. `%s`%s —— %s" % (i + 1, s.get("tool") if not s.get("needs_ai") else "AI:" + s["id"],
                              "（需要模型）" if s.get("needs_ai") else "", s.get("note") or s.get("prompt") or "")
        for i, s in enumerate(flow["steps"]))
    triggers = "、".join("「%s」" % t for t in flow["trigger"])
    return (template.replace("__NAME__", flow["name"])
            .replace("__DESCRIPTION__", (flow.get("description") or (flow["summary"] + " 触发词：" + "/".join(flow["trigger"]))).replace("\n", " "))
            .replace("__TITLE__", flow.get("title") or flow["name"])
            .replace("__SUMMARY__", flow["summary"])
            .replace("__TRIGGERS__", triggers)
            .replace("__STEPS__", steps_md)
            .replace("__RUN_JS__", run_js.rstrip("\n"))
            .replace("__SOURCE__", source)
            .replace("__DATE__", time.strftime("%Y-%m-%d")))


# ── 主流程 ───────────────────────────────────────────────────────

def build(skill_dir: Path, *, trace: Path | None, refresh_tools: bool, do_dryrun: bool,
          tools: dict[str, dict[str, Any]] | None = None, source: str = "语音轨迹") -> dict[str, Any]:
    flow_path = skill_dir / "flow.json"
    if not flow_path.exists():
        raise BuildError("缺 %s" % flow_path)
    try:
        flow = json.loads(flow_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise BuildError("flow.json 不是合法 JSON: %s" % exc) from exc
    tools = tools if tools is not None else load_tools(refresh=refresh_tools)
    errors = lint_flow(flow, tools)
    if errors:
        return {"ok": False, "stage": "lint", "errors": errors}
    run_js = render_run_js(flow)
    result: dict[str, Any] = {"ok": True, "stage": "generated", "name": flow["name"], "steps": len(flow["steps"])}
    if do_dryrun:
        trace_steps: list[dict[str, Any]] = []
        if trace and trace.exists():
            try:
                data = json.loads(trace.read_text(encoding="utf-8"))
                trace_steps = [s for s in (data.get("steps") or []) if isinstance(s, dict) and s.get("kind") == "mcp"]
            except ValueError as exc:
                raise BuildError("trace 不是合法 JSON: %s" % exc) from exc
        out = dryrun(run_js, trace_steps)
        problems = assess_dryrun(flow, out)
        result["dryrun"] = {"log": out.get("log", [])[-12:], "calls": [c["tool"] for c in out.get("calls", [])]}
        if problems:
            return {**result, "ok": False, "stage": "dryrun", "errors": problems}
        result["stage"] = "dryrun-passed"
    (skill_dir / "run.js").write_text(run_js, encoding="utf-8")
    skill_md = skill_dir / "SKILL.md"
    if not refill_skill_md(skill_md, run_js):
        skill_md.write_text(render_skill_md(flow, run_js, source=source), encoding="utf-8")
        result["skillMd"] = "created"
    else:
        result["skillMd"] = "refilled"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="flow.json → run.js（校验 + 生成 + 干跑）")
    parser.add_argument("skill_dir", nargs="?", type=Path)
    parser.add_argument("--trace", type=Path, help="voice_turn_trace.py 导出的轨迹，干跑用它当假工具输出")
    parser.add_argument("--refresh-tools", action="store_true")
    parser.add_argument("--no-dryrun", action="store_true")
    parser.add_argument("--tools", action="store_true", help="只刷新工具面缓存并列出工具名")
    parser.add_argument("--source", default="语音轨迹")
    args = parser.parse_args(argv)
    try:
        if args.tools:
            tools = load_tools(refresh=True)
            print(json.dumps({"ok": True, "tools": sorted(tools)}, ensure_ascii=False))
            return 0
        if args.skill_dir is None:
            parser.error("要给 skill 目录")
        result = build(args.skill_dir, trace=args.trace, refresh_tools=args.refresh_tools,
                       do_dryrun=not args.no_dryrun, source=args.source)
    except BuildError as exc:
        print(json.dumps({"ok": False, "stage": "error", "errors": [str(exc)]}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
