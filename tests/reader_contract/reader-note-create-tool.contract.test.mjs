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
  // 断言白名单的内容，不是它的排版：换行方式变了不该让测试红，
  // 少了一个入口才该红。
  const whitelist = BRIDGE.slice(
    BRIDGE.indexOf("string fn = Text(root"),
    BRIDGE.indexOf("Reader 客户端动作不在白名单内"),
  );
  assert.match(whitelist, /"_nativeReaderCreateNote"/);
  assert.match(whitelist, /"_nativeReaderUndoLast"/);
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

// ── 改写已有便签 ────────────────────────────────────────────────────
// 助手能重写一段文字，但不该顺手把便签挪走或改色：那是用户对页面的布置。
// 另一条同样要紧 —— 便签不存在和写入失败必须分开报：前者重试多少次都一样，
// 后者可能只是这一刻不成。合并成一个错误，调用方就只能靠猜要不要再来。
function editNote({ input, surface = "pdf", status = 200, data } = {}) {
  const start = RUNTIME.indexOf("function nativeReaderEditNote(");
  assert.notEqual(start, -1, "找不到改写入口");
  const sent = [];
  const context = {
    String, Number, Array, Promise, JSON, RegExp,
    nativeInterfaceSurface: surface,
    bootPromise: Promise.resolve(),
    localFileRef: () => "localbook:abc",
    localBasePath: () => "/r/" + "a".repeat(64),
    RuntimeError: class RuntimeError extends Error {
      constructor(message, code) { super(message); this.code = code; }
    },
    root: {
      fetch: (url, init) => {
        sent.push({ url, method: init.method, body: JSON.parse(init.body) });
        return Promise.resolve({
          ok: status >= 200 && status < 300,
          status,
          json: () => Promise.resolve(
            data === undefined ? { ok: status < 300, id: "n_1" } : data,
          ),
        });
      },
    },
    out: null,
  };
  context.globalThis = context;
  vm.runInNewContext(
    `${balanced(RUNTIME, start)}
     out = nativeReaderEditNote(${JSON.stringify(input)});`,
    context,
  );
  return { result: context.out, sent };
}

test("改写只发内容，位置与外观一律不带", async () => {
  const { result, sent } = editNote({
    input: {
      id: "n_abc123", text: "改好的",
      // 全是"顺手改一下"会波及的字段：一个都不该出现在请求里
      anchor: { kind: "pdf", page: 99 }, color: "#ff0000",
      w: 999, h: 999, collapsed: true, file: "other.pdf",
    },
  });
  await result;
  assert.equal(sent[0].method, "PATCH");
  assert.deepEqual(
    Object.keys(JSON.parse(JSON.stringify(sent[0].body))).sort(),
    ["file", "id", "text"],
    "PATCH 只提及 text；未提及的字段本地原样保留，等于位置动不了",
  );
  assert.equal(sent[0].body.file, "localbook:abc", "书由 App 决定");
  assert.equal(sent[0].body.text, "改好的");
});

test("便签不存在与写入失败是两个错误", async () => {
  const missing = editNote({
    input: { id: "n_abc123", text: "x" },
    status: 404, data: { ok: false, error: "未找到便签" },
  });
  await assert.rejects(
    missing.result,
    (error) => error.code === "BW_READER_NOTE_MISSING",
    "重试解决不了「不存在」，必须与「这次没写成」分开",
  );

  const failed = editNote({
    input: { id: "n_abc123", text: "x" },
    status: 409, data: { ok: false, error: "冲突", code: "BW_LOCAL_NOTES" },
  });
  await assert.rejects(failed.result, (error) => error.code === "BW_LOCAL_NOTES");
});

test("编号非法时拒绝，不发出请求", async () => {
  for (const id of ["", "n/../x", "a".repeat(65), "n abc", null]) {
    const { result, sent } = editNote({ input: { id, text: "x" } });
    await assert.rejects(result, (error) => error.code === "BW_READER_NOTE_ID");
    assert.deepEqual(sent, []);
  }
});

test("改写同样拒绝空内容与不支持的界面", async () => {
  const empty = editNote({ input: { id: "n_a", text: "   " } });
  await assert.rejects(empty.result, (error) => error.code === "BW_READER_NOTE_TEXT");
  assert.deepEqual(empty.sent, []);

  const wrongSurface = editNote({ input: { id: "n_a", text: "x" }, surface: "html" });
  await assert.rejects(
    wrongSurface.result,
    (error) => error.code === "BW_READER_NOTE_SURFACE",
  );
  assert.deepEqual(wrongSurface.sent, []);
});

test("改写动作只把编号与文本交给受信入口", async () => {
  const seen = [];
  const context = dispatch({
    fn: "_nativeReaderEditNote",
    args: [{ id: "n_abc123", text: "改好的", color: "#f00", page: 99 }],
    host: { _nativeReaderEditNote: (input) => { seen.push(input); return "ok"; } },
  });
  assert.equal(context.thrown, null);
  await context.work;
  assert.deepEqual(
    JSON.parse(JSON.stringify(seen)),
    [{ id: "n_abc123", text: "改好的" }],
    "夹带的外观字段必须被丢弃",
  );
});

test("改写工具已注册：只收 id 与 text，声明为幂等", () => {
  assert.match(BRIDGE, /"_nativeReaderEditNote"/);
  assert.match(BRIDGE, /Exact\(edit, "id", "text"\)/, "多传字段应被拒绝");
  assert.match(MCP, /"reader_note_edit"/);
  const start = MCP.indexOf('["name"] = NoteEditToolName');
  const spec = MCP.slice(start, start + 2000);
  assert.match(spec, /\["required"\] = new JsonArray \{ "id", "text" \}/);
  assert.match(spec, /\["additionalProperties"\] = false/, "位置与外观不得由调用方传入");
  assert.match(
    spec, /\["idempotentHint"\] = true/,
    "改写成同样的内容重复执行结果一致，与新建不同",
  );
});

// ── 存成一篇笔记 ────────────────────────────────────────────────────
// 跟贴在页面上的便签是两回事：这个写的是一份文件。书和页仍由 App 填，
// 标题可以不给 —— 让助手编一个标题，出来的会是它以为用户在读的那本书。
function makeNote({ input, surface = "pdf", page = 7, data } = {}) {
  const start = RUNTIME.indexOf("function nativeReaderMakeNote(");
  assert.notEqual(start, -1, "找不到存笔记入口");
  const sent = [];
  const context = {
    String, Number, Array, Promise, JSON, Math, RegExp,
    nativeInterfaceSurface: surface,
    bootPromise: Promise.resolve(),
    readState: () => Promise.resolve({ page }),
    localFileRef: () => "localbook:小さな本.pdf",
    localBasePath: () => "/r/" + "a".repeat(64),
    RuntimeError: class RuntimeError extends Error {
      constructor(message, code) { super(message); this.code = code; }
    },
    root: {
      fetch: (url, init) => {
        sent.push(JSON.parse(init.body));
        return Promise.resolve({
          ok: true,
          json: () => Promise.resolve(
            data === undefined ? { ok: true, note_path: "/notes/a.md" } : data,
          ),
        });
      },
    },
    out: null,
  };
  context.globalThis = context;
  vm.runInNewContext(
    `${balanced(RUNTIME, start)}
     out = nativeReaderMakeNote(${JSON.stringify(input)});`,
    context,
  );
  return { result: context.out, sent };
}

test("书与页由 App 填，调用方给的被忽略", async () => {
  const { result, sent } = makeNote({
    input: { text: "正文", title: "我的标题", file: "other.pdf", page: 999 },
    page: 42,
  });
  const value = await result;
  assert.equal(sent[0].file, "localbook:小さな本.pdf");
  assert.equal(sent[0].page, 42);
  assert.equal(sent[0].name, "我的标题");
  assert.equal(value.note_path, "/notes/a.md");
});

test("不给标题时由 Reader 按书名与页码命名", async () => {
  const { result, sent } = makeNote({ input: { text: "正文" }, page: 42 });
  await result;
  assert.match(
    sent[0].name, /小さな本/,
    "名字取自 App 真正打开的那本书，不由助手猜",
  );
  assert.match(sent[0].name, /42/);
});

test("空内容与超长拒绝，且不写出任何文件", async () => {
  for (const input of [{ text: "" }, { text: "  " }, { text: "a".repeat(240001) }]) {
    const { result, sent } = makeNote({ input });
    await assert.rejects(result, (error) => error.code === "BW_READER_NOTE_TEXT");
    assert.deepEqual(sent, []);
  }
  const longTitle = makeNote({ input: { text: "x", title: "t".repeat(241) } });
  await assert.rejects(
    longTitle.result, (error) => error.code === "BW_READER_NOTE_TITLE",
  );
  assert.deepEqual(longTitle.sent, []);
});

test("写入未确认成功时如实报错", async () => {
  const { result } = makeNote({
    input: { text: "x" }, data: { ok: false, error: "磁盘满" },
  });
  await assert.rejects(result, (error) => error.code === "BW_READER_NOTE_FAILED");
});

test("存笔记动作只把标题与正文交给受信入口", async () => {
  const seen = [];
  const context = dispatch({
    fn: "_nativeReaderMakeNote",
    args: [{ title: "T", text: "正文", file: "other.pdf", page: 999 }],
    host: { _nativeReaderMakeNote: (input) => { seen.push(input); return "ok"; } },
  });
  assert.equal(context.thrown, null);
  await context.work;
  assert.deepEqual(
    JSON.parse(JSON.stringify(seen)), [{ title: "T", text: "正文" }],
  );
});

test("存笔记工具声明为非幂等：重试会写出第二份文件", () => {
  assert.match(BRIDGE, /"_nativeReaderMakeNote"/);
  assert.match(BRIDGE, /Exact\(made, "title", "text"\)/);
  assert.match(MCP, /"reader_make_note"/);
  const start = MCP.indexOf('["name"] = MakeNoteToolName');
  const spec = MCP.slice(start, start + 2600);
  assert.match(spec, /\["idempotentHint"\] = false/);
  assert.match(spec, /\["additionalProperties"\] = false/);
  // 描述在 C# 里是跨行拼接的，先还原成一句话再匹配语义，
  // 免得换个折行位置就红。
  const prose = spec.replace(/"\s*\+\s*"/g, "");
  assert.match(
    prose, /second attempt writes a second file/,
    "不写清楚，一次超时会变成两份笔记",
  );
});
