// bw_flow_runtime.js — 生成 run.js 用的运行器模板（2026-09-13）。
//
// 跑在 Codex 的 exec 沙箱里：一个新的 V8 isolate，**没有 Node、没有文件、没有网络**，
// 只有 `tools.mcp__<server>__<tool>(args)`、`text()`、`image()`、`store()/load()`、`exit()`。
// 所以 run.js 必须自包含：本文件 + 一段 FLOW JSON 拼成一个文件，由 bw_skill_build.py 生成，
// AI 不写运行器、只填 flow.json。
//
// 规则：
// - 按 FLOW.steps 顺序调工具，上一步输出按 `{"$from": "<stepId>", "path": "a.b[0]"}` 直接喂下一步，
//   全程不回模型。
// - `needs_ai: true` 的步骤停下：把到此为止的输出存进 store，text() 一段 handoff，脚本结束。
//   模型处理完把结果 `store("<STORE_KEY>:ai", value)` 再原样重跑本脚本，它从停下的那步继续。
// - 每步 text() 一行 `[bw-flow] <name> <i>/<n> <tool> ok|fail` —— 给人看、也给 ReaderPC 算进度。
/* __BW_FLOW_JSON__ */
const FLOW = __BW_FLOW__;
const STORE_KEY = "bw_flow:" + FLOW.name;

function pick(value, path) {
  if (!path) return value;
  const parts = String(path).replace(/\[(\d+)\]/g, ".$1").split(".").filter(Boolean);
  let cur = value;
  for (const part of parts) {
    if (cur == null) return undefined;
    cur = cur[part];
  }
  return cur;
}

function textOf(result) {
  // MCP 结果 {content:[{type:"text",text:"..."}]} → 第一段文本；能 JSON 就解析。
  if (result && Array.isArray(result.content)) {
    const t = result.content.filter(c => c && c.type === "text").map(c => c.text).join("\n");
    try { return JSON.parse(t); } catch (_) { return t; }
  }
  return result;
}

function resolve(arg, outputs) {
  if (Array.isArray(arg)) return arg.map(v => resolve(v, outputs));
  if (arg && typeof arg === "object") {
    if (typeof arg.$from === "string") {
      const src = outputs[arg.$from];
      if (src === undefined) throw new Error("step '" + arg.$from + "' has no output yet");
      const base = arg.raw ? src.raw : src.value;
      const got = pick(base, arg.path);
      if (got === undefined && arg.default !== undefined) return arg.default;
      return got;
    }
    if (typeof arg.$ai === "string") {
      const ai = outputs.__ai || {};
      if (!(arg.$ai in ai)) throw new Error("AI 结果里没有 '" + arg.$ai + "'");
      return ai[arg.$ai];
    }
    const out = {};
    for (const k of Object.keys(arg)) out[k] = resolve(arg[k], outputs);
    return out;
  }
  return arg;
}

const saved = load(STORE_KEY) || { step: 0, outputs: {} };
const outputs = saved.outputs || {};
const aiResult = load(STORE_KEY + ":ai");
if (aiResult !== undefined) {
  outputs.__ai = Object.assign({}, outputs.__ai || {}, aiResult);
  store(STORE_KEY + ":ai", undefined);
}
let i = saved.step || 0;
const n = FLOW.steps.length;
for (; i < n; i++) {
  const step = FLOW.steps[i];
  if (step.needs_ai) {
    if (outputs.__ai && step.id in outputs.__ai) {
      outputs[step.id] = { value: outputs.__ai[step.id], raw: outputs.__ai[step.id] };
      text("[bw-flow] " + FLOW.name + " " + (i + 1) + "/" + n + " ai:" + step.id + " ok");
      continue;
    }
    store(STORE_KEY, { step: i, outputs });
    const input = step.input ? resolve(step.input, outputs) : null;
    text("[bw-flow] " + FLOW.name + " " + (i + 1) + "/" + n + " ai:" + step.id + " handoff");
    text(JSON.stringify({ bwFlowHandoff: { skill: FLOW.name, step: step.id, prompt: step.prompt || "", input,
      resume: "store(" + JSON.stringify(STORE_KEY + ":ai") + ", {" + JSON.stringify(step.id) + ": <你的结果>}) 然后原样重跑 run.js" } }));
    exit();
  }
  const fn = tools["mcp__" + (step.server || FLOW.server || "reader_snapshot") + "__" + step.tool];
  if (typeof fn !== "function") {
    text("[bw-flow] " + FLOW.name + " " + (i + 1) + "/" + n + " " + step.tool + " fail: tool missing");
    store(STORE_KEY, undefined);
    exit();
  }
  let args;
  try { args = resolve(step.args || {}, outputs); }
  catch (e) {
    text("[bw-flow] " + FLOW.name + " " + (i + 1) + "/" + n + " " + step.tool + " fail: " + e.message);
    store(STORE_KEY, undefined);
    exit();
  }
  const raw = await fn(args);
  const value = textOf(raw);
  outputs[step.id] = { value, raw };
  const failed = raw && raw.isError === true || (value && typeof value === "object" && value.ok === false);
  text("[bw-flow] " + FLOW.name + " " + (i + 1) + "/" + n + " " + step.tool + (failed ? " fail" : " ok"));
  if (failed) {
    text(JSON.stringify({ bwFlowFailed: { skill: FLOW.name, step: step.id, result: value } }).slice(0, 4000));
    store(STORE_KEY, undefined);
    exit();
  }
  if (step.show && raw && Array.isArray(raw.content)) {
    for (const c of raw.content) { if (c.type === "image") image(c); }
  }
}
store(STORE_KEY, undefined);
const last = FLOW.steps.length ? outputs[FLOW.steps[FLOW.steps.length - 1].id] : null;
text(JSON.stringify({ bwFlowDone: { skill: FLOW.name, steps: n, result: last ? last.value : null } }).slice(0, 6000));
