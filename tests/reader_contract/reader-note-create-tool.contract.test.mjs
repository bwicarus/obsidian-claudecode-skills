// 助手写便签：内容由调用方给，位置由 App 自己填。
//
// 桥接那侧只知道"用户想记一条"，不知道此刻停在哪一页、哪一节。让它传坐标，就是把
// 一个它没有的事实写进协议：轻则落错页，重则落到别的书上。所以受信入口只收文本，
// 锚点用受信的当前界面与页码就地构造。
//
// 执行侧仍然不做动态分派。一条跨进程消息不该能指定"调用页面上哪个函数"，
// 所以这里是一张显式映射表，表外的名字一律拒绝。
import assert from "node:assert/strict";
import test from "node:test";
import vm from "node:vm";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const RUNTIME = read("_server_deploy/static/pdf/native-local-runtime.js");
const VOICECALL = read("_server_deploy/static/pdf/rc-voicecall.js");
const BRIDGE = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderRealtimeOutput.cs",
);
const MCP = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderContextMcpServer.cs",
);

function balanced(source, start) {
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    if (source[i] === "}") depth -= 1;
    if (depth === 0) return source.slice(start, i + 1);
  }
  assert.fail("括号未闭合");
}

// ── 受信入口 ────────────────────────────────────────────────────────
function createNote({ input, surface = "pdf", page = 7, fetchImpl } = {}) {
  const start = RUNTIME.indexOf("function nativeReaderCreateNote(");
  assert.notEqual(start, -1, "找不到受信入口");
  const sent = [];
  const context = {
    String, Number, Array, Promise, JSON,
    nativeInterfaceSurface: surface,
    bootPromise: Promise.resolve(),
    readState: () => Promise.resolve({ page }),
    localFileRef: () => "localbook:abc",
    localBasePath: () => "/r/" + "a".repeat(64),
    RuntimeError: class RuntimeError extends Error {
      constructor(message, code) { super(message); this.code = code; }
    },
    root: {
      fetch: (url, init) => {
        sent.push({ url, body: JSON.parse(init.body) });
        return (fetchImpl || (() => Promise.resolve({
          ok: true, json: () => Promise.resolve({ ok: true, id: "n_1" }),
        })))();
      },
    },
    out: null,
  };
  context.globalThis = context;
  vm.runInNewContext(
    `${balanced(RUNTIME, start)}
     out = nativeReaderCreateNote(${JSON.stringify(input)});`,
    context,
  );
  return { result: context.out, sent };
}

test("只收文本，锚点由 App 用当前页自己构造", async () => {
  // 输入里塞满位置字段：调用方越权指定的每一个都必须被忽略，
  // 否则一条便签可以被写到另一页、甚至另一本书上。
  const { result, sent } = createNote({
    input: { text: "记一条", page: 999, section: 999, file: "other.pdf", anchor: { kind: "pdf", page: 999 } },
    surface: "pdf",
    page: 12,
  });
  await result;
  assert.equal(sent.length, 1);
  assert.equal(sent[0].body.text, "记一条");
  assert.deepEqual(sent[0].body.anchor.kind, "pdf");
  assert.equal(sent[0].body.anchor.page, 12, "页码取自 App 的受信状态，不由调用方给");
  assert.equal(sent[0].body.file, "localbook:abc", "书也由 App 决定，写不到别的书上");
});

test("EPUB 用 section 锚点", async () => {
  const { result, sent } = createNote({
    input: { text: "x", section: 999, page: 999 },
    surface: "epub",
    page: 4,
  });
  await result;
  assert.equal(sent[0].body.anchor.kind, "epub");
  assert.equal(sent[0].body.anchor.section, 4, "章节取自 App 的受信状态");
});

test("空内容拒绝，且不发出任何写入", async () => {
  for (const text of ["", "   ", null, undefined]) {
    const { result, sent } = createNote({ input: { text } });
    await assert.rejects(result, (error) => error.code === "BW_READER_NOTE_TEXT");
    assert.deepEqual(sent, []);
  }
});

test("超长内容拒绝", async () => {
  const { result, sent } = createNote({ input: { text: "a".repeat(4001) } });
  await assert.rejects(result, (error) => error.code === "BW_READER_NOTE_TEXT");
  assert.deepEqual(sent, []);
});

test("界面不支持便签时拒绝，不猜一个锚点", async () => {
  const { result, sent } = createNote({ input: { text: "x" }, surface: null });
  await assert.rejects(result, (error) => error.code === "BW_READER_NOTE_SURFACE");
  assert.deepEqual(sent, []);
});

test("写入未确认成功时如实报错，不假称已保存", async () => {
  const { result } = createNote({
    input: { text: "x" },
    fetchImpl: () => Promise.resolve({
      ok: false, json: () => Promise.resolve({ ok: false, error: "conflict", code: "BW_LOCAL_NOTES" }),
    }),
  });
  await assert.rejects(result, (error) => error.code === "BW_LOCAL_NOTES");
});

// ── 执行侧映射 ──────────────────────────────────────────────────────
function branch() {
  const start = VOICECALL.indexOf("} else if (delivery.kind === 'client-action') {");
  assert.notEqual(start, -1, "找不到 client-action 分支");
  const end = VOICECALL.indexOf(
    "} else {\n        throw new Error('BW_READER_REALTIME_OUTPUT_KIND_UNSUPPORTED')",
    start,
  );
  return VOICECALL.slice(start, end);
}

function dispatch({ fn, args, host }) {
  const context = {
    window: host, delivery: { kind: "client-action" }, p: { fn, args },
    Promise, Array, String, JSON, work: null, thrown: null,
  };
  context.globalThis = context;
  vm.runInNewContext(
    `try { if (false) { ${branch()} } } catch (error) { thrown = error; }`,
    context,
  );
  return context;
}

test("便签动作只把文本交给受信入口", async () => {
  const seen = [];
  const context = dispatch({
    fn: "_nativeReaderCreateNote",
    args: [{ text: "记一条", page: 999 }],
    host: { _nativeReaderCreateNote: (input) => { seen.push(input); return "ok"; } },
  });
  assert.equal(context.thrown, null);
  await context.work;
  // 沙箱对象来自另一个 realm，原型不同，只比内容。
  assert.deepEqual(
    JSON.parse(JSON.stringify(seen)),
    [{ text: "记一条" }],
    "调用方夹带的位置必须被丢弃",
  );
});

test("表外的名字一律拒绝，且不触碰 window", () => {
  let called = false;
  const context = dispatch({
    fn: "fetch", args: ["https://example.invalid/"],
    host: { fetch() { called = true; } },
  });
  assert.equal(called, false);
  assert.match(String(context.thrown && context.thrown.message), /CLIENT_ACTION_INVALID:fetch/);
});

test("仍未使用动态分派", () => {
  assert.doesNotMatch(
    branch(),
    /window\s*\[\s*_caFn\s*\]/,
    "按消息里的字符串去 window 上取函数，等于开放任意页面函数调用",
  );
});

// ── 桥接与工具面 ────────────────────────────────────────────────────
test("桥接侧只允许名单内入口，便签只接受 text", () => {
  assert.match(BRIDGE, /"_nativeReaderUndoLast" or "_nativeReaderCreateNote"/);
  assert.match(BRIDGE, /Exact\(note, "text"\)/, "多传字段应被拒绝");
});

test("工具已注册且声明为非只读、非幂等", () => {
  assert.match(MCP, /"reader_note_create"/);
  const start = MCP.indexOf('["name"] = NoteCreateToolName');
  const spec = MCP.slice(start, start + 2000);
  assert.match(spec, /\["required"\] = new JsonArray \{ "text" \}/);
  assert.match(spec, /\["additionalProperties"\] = false/, "位置字段不得由调用方传入");
  assert.match(spec, /\["readOnlyHint"\] = false/);
  assert.match(spec, /\["idempotentHint"\] = false/, "重复调用会写出第二条便签");
});
