// 助手向 Reader 提问并等一个答案。
//
// 这条通道的价值全在"答不上来"这件事上说得清楚。三种情形在助手那里会导出
// 完全不同的话：
//   · 这本书里确实没有高亮        → 它可以说"你还没划过"
//   · 当前界面根本没有本机高亮库  → 它该说"这里读不到"，而不是替用户下结论
//   · 这次没答上来（超时/异常）    → 它该重试或说明，而不是当作没有
// 全部答成一个空列表，第二和第三种就会被当成第一种 —— 用户会听见一句自信的
// 假话。所以 unsupported / unavailable 必须是独立状态，且不得携带空结果冒充。
//
// 另一条：截断。被悄悄截短的列表跟完整的长得一模一样，助手会据此报一个总数。
// truncated 因此在协议层就是必填字段，即使是 false 也要出现。
import assert from "node:assert/strict";
import test from "node:test";
import vm from "node:vm";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const RUNTIME = read("_server_deploy/static/pdf/native-local-runtime.js");
const VOICE = read("_server_deploy/static/pdf/rc-computer-voice.js");
const QUERY = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderQuery.cs",
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

// ── 本机读入口 ──────────────────────────────────────────────────────
function readHighlights({ input = {}, surface = "pdf", stored = [] } = {}) {
  const start = RUNTIME.indexOf("function nativeReaderHighlights(");
  assert.notEqual(start, -1, "找不到高亮读入口");
  const reads = [];
  const context = {
    String, Number, Array, Promise, JSON,
    nativeInterfaceSurface: surface,
    bootPromise: Promise.resolve(),
    EXACT_HIGHLIGHT_IDB_TIMEOUT_MS: 4000,
    readState: (kind, fallback, options) => {
      reads.push({ kind, options });
      return Promise.resolve(stored);
    },
    RuntimeError: class RuntimeError extends Error {
      constructor(message, code) { super(message); this.code = code; }
    },
    out: null,
  };
  context.globalThis = context;
  vm.runInNewContext(
    `${balanced(RUNTIME, start)}
     out = nativeReaderHighlights(${JSON.stringify(input)});`,
    context,
  );
  return { result: context.out, reads };
}

const SAMPLE = [
  { id: "h1", page: 3, color: "yellow", text: "alpha beta", rects: [[0, 0, 1, 1]] },
  { id: "h2", page: 1, color: "blue", text: "gamma", rects: [[0, 0, 1, 1]] },
  { id: "h3", page: 3, color: "pink", text: "Beta again", rects: [[0, 0, 1, 1]] },
];

test("按页过滤，并按页排序", async () => {
  const { result } = readHighlights({ input: { page: 3 }, stored: SAMPLE });
  const value = await result;
  assert.deepEqual(JSON.parse(JSON.stringify(value.highlights.map((h) => h.id))), ["h1", "h3"]);
  assert.equal(value.matched, 2);
  assert.equal(value.truncated, false);
});

test("按文字过滤且忽略大小写", async () => {
  const { result } = readHighlights({ input: { contains: "beta" }, stored: SAMPLE });
  const value = await result;
  assert.deepEqual(JSON.parse(JSON.stringify(value.highlights.map((h) => h.id))), ["h1", "h3"]);
});

test("不带过滤时按页排序返回全部", async () => {
  const { result } = readHighlights({ stored: SAMPLE });
  const value = await result;
  assert.deepEqual(JSON.parse(JSON.stringify(value.highlights.map((h) => h.id))), ["h2", "h1", "h3"]);
});

test("不返回渲染几何", async () => {
  const { result } = readHighlights({ stored: SAMPLE });
  const value = await result;
  for (const item of value.highlights) {
    assert.deepEqual(
      Object.keys(item).sort(),
      ["color", "id", "page", "text"],
      "rects 占的体积能顶好几条正文，而助手拿它什么也做不了",
    );
  }
});

test("装不下时截断并如实标注", async () => {
  const many = Array.from({ length: 400 }, (_, index) => ({
    id: `h${index}`, page: index + 1, color: "yellow",
    text: "x".repeat(300),
  }));
  const { result } = readHighlights({ stored: many });
  const value = await result;
  assert.equal(value.truncated, true, "截断必须说出来");
  assert.ok(value.returned < value.matched, "返回的是前缀");
  assert.equal(value.matched, 400, "总数仍如实报出，助手才知道少了多少");
  assert.ok(
    JSON.stringify(value.highlights).length <= 32 * 1024,
    "必须真的落在预算内，否则整条消息会被传输层丢掉",
  );
});

test("EPUB 读的是 EPUB 那份，且用章节号", async () => {
  const { result, reads } = readHighlights({
    surface: "epub",
    stored: [{ id: "e1", section: 5, color: "yellow", text: "x" }],
  });
  const value = await result;
  assert.equal(reads[0].kind, "epub-highlights");
  assert.equal(value.highlights[0].page, 5);
});

test("读取带事务上界，不会挂住高亮库", async () => {
  const { result, reads } = readHighlights({ stored: [] });
  await result;
  assert.equal(
    reads[0].options?.transactionTimeoutMs, 4000,
    "一次不 settle 的读会占住 object store，之后所有高亮读写都排在它后面",
  );
});

test("界面没有本机高亮时拒绝，而不是回空列表", async () => {
  const { result } = readHighlights({ surface: "html", stored: [] });
  await assert.rejects(result, (error) => error.code === "BW_READER_QUERY_SURFACE");
});

test("参数非法时拒绝", async () => {
  for (const input of [
    { page: 0 }, { page: 1.5 }, { page: "3" }, { contains: "x".repeat(257) },
  ]) {
    const { result } = readHighlights({ input, stored: [] });
    await assert.rejects(result, (error) => error.code === "BW_READER_QUERY_PARAMS");
  }
});

// ── WSS 执行侧 ──────────────────────────────────────────────────────
function handlerSource() {
  const start = VOICE.indexOf(
    "DirectSocket.prototype._handleReaderQuery = function (rawPayload) {",
  );
  assert.notEqual(start, -1, "找不到查询处理器");
  return balanced(VOICE, start);
}

function runHandler({ query = "highlights", params = {}, impl, host } = {}) {
  const sent = [];
  const sendStart = VOICE.indexOf(
    "DirectSocket.prototype._sendReaderQueryResult = function (",
  );
  assert.notEqual(sendStart, -1, "找不到回传方法");
  const source = `${balanced(VOICE, sendStart)}\n${handlerSource()}`;
  const table = VOICE.slice(
    VOICE.indexOf("var READER_QUERY_HANDLERS = {"),
    VOICE.indexOf("function normalizeReaderQueryRequest("),
  );
  const context = {
    Promise, Object, Array, String, Number, JSON,
    window: host || { _nativeReaderHighlights: impl },
    DirectSocket: { prototype: {} },
    REQUEST_TIMEOUT_MS: 1000,
    // 常量从源文件里读，而不是在测试里另抄一份：抄一份就等于两处各有一个
    // 真相，改了源文件测试还会绿。
    READER_QUERY_RESPONSE: /READER_QUERY_RESPONSE = "([^"]+)"/.exec(VOICE)[1],
  };
  context.globalThis = context;
  vm.runInNewContext(
    `${table}
     function normalizeReaderQueryRequest(raw) { return raw; }
     ${source}
     var socket = {
       readerVisualSessionId: "s1",
       _sendReaderQueryResult: DirectSocket.prototype._sendReaderQueryResult,
       _handleReaderQuery: DirectSocket.prototype._handleReaderQuery,
       request: function (action, fields) {
         sent.push(fields);
         return Promise.resolve({ ok: true });
       },
     };
     socket._handleReaderQuery({
       correlation: "c1", sourceInstanceId: "s1", snapshotRevision: 1,
       file: "book.pdf", query: ${JSON.stringify(query)},
       params: ${JSON.stringify(params)},
     });`,
    Object.assign(context, { sent }),
  );
  return sent;
}

test("成功时回 ok，并把截断标志原样带回", async () => {
  const sent = runHandler({
    impl: () => Promise.resolve({
      ok: true, highlights: [{ id: "h1" }], truncated: true,
    }),
  });
  await new Promise((resolve) => setImmediate(() => setImmediate(resolve)));
  assert.equal(sent.length, 1);
  assert.equal(sent[0].status, "ok");
  assert.equal(sent[0].truncated, true, "截断标志不能在回传路上丢掉");
});

test("界面做不到时回 unsupported，不是空结果", () => {
  const sent = runHandler({ host: {} });
  assert.equal(sent.length, 1);
  assert.equal(
    sent[0].status, "unsupported",
    "回空列表会让助手替用户断定「你没有划过」",
  );
});

test("执行失败时回 unavailable，不是空结果", async () => {
  const sent = runHandler({ impl: () => Promise.reject(new Error("boom")) });
  await new Promise((resolve) => setImmediate(() => setImmediate(resolve)));
  assert.equal(sent.length, 1);
  assert.equal(sent[0].status, "unavailable");
});

test("名单外的名字调不到任何东西", () => {
  const table = VOICE.slice(
    VOICE.indexOf("var READER_QUERY_HANDLERS = {"),
    VOICE.indexOf("function normalizeReaderQueryRequest("),
  );
  assert.doesNotMatch(
    table, /window\s*\[/,
    "按消息里的字符串去 window 上取函数等于开放任意页面调用",
  );
  const normalizer = VOICE.slice(
    VOICE.indexOf("function normalizeReaderQueryRequest("),
    VOICE.indexOf("DirectSocket.prototype._sendReaderQueryResult"),
  );
  assert.match(
    normalizer,
    /hasOwnProperty\.call\(READER_QUERY_HANDLERS, query\)/,
    "名单校验必须查自有属性，否则 toString 之类的继承名会被当成合法查询",
  );
});

// ── 两端一致 ────────────────────────────────────────────────────────
test("结果上限落在 WSS 控制帧限制之内", () => {
  const limit = /MaximumResultBytes = (\d+) \* 1024/.exec(QUERY);
  assert.notEqual(limit, null, "找不到结果上限");
  const frame = /var MAX_MESSAGE_BYTES = (\d+);/.exec(VOICE);
  assert.notEqual(frame, null, "找不到控制帧上限");
  const limitBytes = Number(limit[1]) * 1024;
  const frameBytes = Number(frame[1]);
  assert.ok(
    limitBytes < frameBytes,
    `结果上限 ${limitBytes} 字节必须小于控制帧 ${frameBytes} 字节 —— `
      + "两端各自看都合理、合起来永远发不出去，是最难查的那类失败",
  );
});

test("只读工具声明为只读且可安全重试", () => {
  assert.match(MCP, /"reader_highlights"/);
  const start = MCP.indexOf('["name"] = HighlightsToolName');
  const spec = MCP.slice(start, start + 2600);
  assert.match(spec, /\["readOnlyHint"\] = true/);
  assert.match(spec, /\["idempotentHint"\] = true/);
  assert.match(
    spec, /truncated/,
    "工具描述必须交代截断的含义，否则助手会把前缀当全集",
  );
});

test("查询超时标为可重试，与写入通道相反", () => {
  const timeout = QUERY.slice(
    QUERY.indexOf('"BW_READER_QUERY_TIMEOUT"') - 400,
    QUERY.indexOf('"BW_READER_QUERY_TIMEOUT"') + 200,
  );
  assert.match(
    timeout, /retryable: true/,
    "读操作再问一次不会改变书里的任何东西；这跟写入的「未知不重试」是两套语义",
  );
});

test("回执必须与提问对得上", () => {
  const accept = QUERY.slice(
    QUERY.indexOf("internal void Accept("),
    QUERY.indexOf("private static ReaderQueryException Failure("),
  );
  for (const field of [
    "SourceInstanceId", "SnapshotRevision", "File", "Query",
  ]) {
    assert.match(
      accept, new RegExp(`request\\.${field} != response\\.${field}`),
      `${field} 不比对的话，一个迟到的旧答复会被当成这本书的现状`,
    );
  }
});

// ── 便签与搜索 ──────────────────────────────────────────────────────
function callEntry(name, { input = {}, surface = "pdf", stored = [], search } = {}) {
  const start = RUNTIME.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `找不到 ${name}`);
  const context = {
    String, Number, Array, Promise, JSON,
    nativeInterfaceSurface: surface,
    bootPromise: Promise.resolve(),
    EXACT_HIGHLIGHT_IDB_TIMEOUT_MS: 4000,
    readState: () => Promise.resolve(stored),
    searchPageText: search || (() => Promise.resolve({ matches: [], incomplete: false })),
    RuntimeError: class RuntimeError extends Error {
      constructor(message, code) { super(message); this.code = code; }
    },
    out: null,
  };
  context.globalThis = context;
  vm.runInNewContext(
    `${balanced(RUNTIME, start)}
     out = ${name}(${JSON.stringify(input)});`,
    context,
  );
  return context.out;
}

test("便签按页与文字过滤，锚点里的页码是真相", async () => {
  const stored = [
    { id: "n1", anchor: { kind: "pdf", page: 4 }, text: "alpha" },
    { id: "n2", anchor: { kind: "pdf", page: 2 }, text: "beta" },
    { id: "n3", anchor: { kind: "epub", section: 4 }, text: "ALPHA again" },
  ];
  const all = await callEntry("nativeReaderNotes", { stored });
  assert.deepEqual(
    JSON.parse(JSON.stringify(all.notes.map((n) => n.id))),
    ["n2", "n1", "n3"],
  );
  const page4 = await callEntry("nativeReaderNotes", { input: { page: 4 }, stored });
  assert.deepEqual(
    JSON.parse(JSON.stringify(page4.notes.map((n) => n.id))), ["n1", "n3"],
  );
  const alpha = await callEntry("nativeReaderNotes", {
    input: { contains: "alpha" }, stored,
  });
  assert.deepEqual(
    JSON.parse(JSON.stringify(alpha.notes.map((n) => n.id))), ["n1", "n3"],
  );
});

test("便签不返回笔迹与几何", async () => {
  const value = await callEntry("nativeReaderNotes", {
    stored: [{
      id: "n1", anchor: { page: 1 }, text: "x",
      strokes: [[1, 2]], w: 260, h: 180, card: { big: "payload" },
    }],
  });
  assert.deepEqual(Object.keys(value.notes[0]).sort(), ["id", "page", "text"]);
});

test("搜索把「装不下」和「没搜全」分开报", async () => {
  const value = await callEntry("nativeReaderSearch", {
    input: { query: "x" },
    search: () => Promise.resolve({
      matches: [{ page: 1, text: "hit" }], incomplete: true,
    }),
  });
  assert.equal(value.incomplete, true, "有些页没能搜到，必须说出来");
  assert.equal(value.truncated, false, "这跟「太多装不下」是两回事");
  assert.equal(
    value.matched, 1,
    "incomplete 时的零命中不等于书里没有，助手不能据此下结论",
  );
});

test("搜索结果过多时截断，且不与 incomplete 混淆", async () => {
  const many = Array.from({ length: 400 }, (_, index) => ({
    page: index + 1, text: "y".repeat(280),
  }));
  const value = await callEntry("nativeReaderSearch", {
    input: { query: "y" },
    search: () => Promise.resolve({ matches: many, incomplete: false }),
  });
  assert.equal(value.truncated, true);
  assert.equal(value.incomplete, false);
  assert.ok(value.returned < value.matched);
});

test("搜索参数非法时拒绝", async () => {
  for (const input of [{ query: "" }, { query: "x".repeat(257) },
                       { query: "x", limit: 0 }, { query: "x", limit: 201 },
                       { query: "x", limit: "5" }]) {
    await assert.rejects(
      callEntry("nativeReaderSearch", { input }),
      (error) => error.code === "BW_READER_QUERY_PARAMS",
    );
  }
});

test("便签与搜索在不支持的界面上拒绝，而不是回空", async () => {
  for (const name of ["nativeReaderNotes", "nativeReaderSearch"]) {
    await assert.rejects(
      callEntry(name, { surface: "html", input: { query: "x" } }),
      (error) => error.code === "BW_READER_QUERY_SURFACE",
    );
  }
});

test("三个查询名都在两端的名单里，且各自显式映射", () => {
  const table = VOICE.slice(
    VOICE.indexOf("var READER_QUERY_HANDLERS = {"),
    VOICE.indexOf("function normalizeReaderQueryRequest("),
  );
  for (const name of ["highlights", "notes", "search"]) {
    assert.match(table, new RegExp(`\n    ${name}: function`), `执行侧缺 ${name}`);
    assert.match(QUERY, new RegExp(`"${name}"`), `桥接名单缺 ${name}`);
  }
  assert.doesNotMatch(table, /window\s*\[/, "仍不得动态分派");
});

test("搜索工具描述交代 incomplete 的含义", () => {
  const start = MCP.indexOf('["name"] = SearchToolName');
  const spec = MCP.slice(start, start + 3000);
  assert.match(spec, /incomplete/);
  assert.match(
    spec, /absence of a match is/,
    "不写清楚，助手会把「半本书里没搜到」说成「这本书里没有」",
  );
});

// ── 目录与页文本 ────────────────────────────────────────────────────
function callFetching(name, { input, surface = "pdf", status = 200, data } = {}) {
  const start = RUNTIME.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `找不到 ${name}`);
  const sent = [];
  const context = {
    String, Number, Array, Promise, JSON, encodeURIComponent,
    nativeInterfaceSurface: surface,
    bootPromise: Promise.resolve(),
    localFileRef: () => "localbook:abc",
    localBasePath: () => "/r/" + "a".repeat(64),
    RuntimeError: class RuntimeError extends Error {
      constructor(message, code) { super(message); this.code = code; }
    },
    root: {
      fetch: (url) => {
        sent.push(url);
        return Promise.resolve({
          ok: status >= 200 && status < 300,
          status,
          json: () => Promise.resolve(data),
        });
      },
    },
    out: null,
  };
  context.globalThis = context;
  const call = input === undefined ? `${name}()` : `${name}(${JSON.stringify(input)})`;
  vm.runInNewContext(
    `${balanced(RUNTIME, start)}
     out = ${call};`,
    context,
  );
  return { result: context.out, sent };
}

test("目录返回标题、页码与层级", async () => {
  const { result, sent } = callFetching("nativeReaderTableOfContents", {
    data: {
      entries: [
        { title: "第一章", page: 1, level: 1 },
        { title: "第一节", page: 3, level: 2 },
      ],
    },
  });
  const value = await result;
  assert.match(sent[0], /entries=1/, "要的是条目列表");
  assert.deepEqual(
    JSON.parse(JSON.stringify(value.entries)),
    [
      { title: "第一章", page: 1, level: 1 },
      { title: "第一节", page: 3, level: 2 },
    ],
  );
  assert.equal(value.truncated, false);
});

test("空目录是答案，不是失败", async () => {
  const { result } = callFetching("nativeReaderTableOfContents", {
    data: { entries: [] },
  });
  const value = await result;
  assert.equal(value.ok, true, "这本书可能就没建过目录，该照实说");
  assert.equal(value.matched, 0);
});

test("目录只对 PDF，EPUB 上明确拒绝而不猜一个等价物", async () => {
  const { result, sent } = callFetching("nativeReaderTableOfContents", {
    surface: "epub", data: { entries: [] },
  });
  await assert.rejects(result, (error) => error.code === "BW_READER_QUERY_SURFACE");
  assert.deepEqual(sent, []);
});

test("页文本复用本机取文接口，不另起一套", async () => {
  const { result, sent } = callFetching("nativeReaderPageText", {
    input: { page: 12 }, data: { ok: true, text: "正文" },
  });
  const value = await result;
  assert.match(
    sent[0], /\/api\/assistant\/voice-page-text\?file=[^&]+&page=12/,
    "两份取文迟早会在振假名、栏序或 OCR 回退上各走各的",
  );
  assert.equal(value.text, "正文");
  assert.equal(value.truncated, false);
});

test("页文本触顶时标注截断", async () => {
  const { result } = callFetching("nativeReaderPageText", {
    input: { page: 1 }, data: { ok: true, text: "x".repeat(1500) },
  });
  const value = await result;
  assert.equal(
    value.truncated, true,
    "不说出来，助手会把半页当整页读",
  );
});

test("页文本页码非法或界面不支持时拒绝", async () => {
  for (const page of [0, -1, 1.5, "3"]) {
    const { result, sent } = callFetching("nativeReaderPageText", {
      input: { page }, data: { ok: true, text: "" },
    });
    await assert.rejects(result, (error) => error.code === "BW_READER_QUERY_PARAMS");
    assert.deepEqual(sent, []);
  }
  const wrong = callFetching("nativeReaderPageText", {
    input: { page: 1 }, surface: "html", data: { ok: true, text: "" },
  });
  await assert.rejects(
    wrong.result, (error) => error.code === "BW_READER_QUERY_SURFACE",
  );
});

test("五个查询名两端一致，且都显式映射", () => {
  const table = VOICE.slice(
    VOICE.indexOf("var READER_QUERY_HANDLERS = {"),
    VOICE.indexOf("function normalizeReaderQueryRequest("),
  );
  for (const name of ["highlights", "notes", "search", "toc", "page-text"]) {
    assert.match(QUERY, new RegExp(`"${name}"`), `桥接名单缺 ${name}`);
    assert.ok(
      table.includes(`${name}: function`) || table.includes(`"${name}": function`),
      `执行侧缺 ${name}`,
    );
  }
  assert.doesNotMatch(table, /window\s*\[/, "仍不得动态分派");
});

// ── 查词与生词标记（这两条落在 Pi 上）──────────────────────────────
test("查词走 prewarm，不把助手的动作记成用户遇到了生词", async () => {
  const { result, sent } = callFetching("nativeReaderLookupWord", {
    input: { word: "ephemeral" },
    data: { ok: true, lemma: "ephemeral", translation: "短暂的", senses: ["a", "b"] },
  });
  const value = await result;
  assert.match(sent[0], /prewarm=1/,
    "不加这个参数，AI 每查一次就给用户记一次曝光、建一篇生词笔记");
  assert.match(sent[0], /word=ephemeral/);
  assert.equal(value.translation, "短暂的");
  assert.equal(value.truncated, false);
});

test("查词义项过多时截断并标注", async () => {
  const { result } = callFetching("nativeReaderLookupWord", {
    input: { word: "set" },
    data: {
      ok: true, lemma: "set",
      senses: Array.from({ length: 30 }, (_, index) => `义项${index}`),
    },
  });
  const value = await result;
  assert.equal(value.senses.length, 12);
  assert.equal(value.truncated, true);
});

test("查词失败如实报错，不返回空词条", async () => {
  const { result } = callFetching("nativeReaderLookupWord", {
    input: { word: "x" }, status: 503, data: { ok: false, error: "网关不可用" },
  });
  await assert.rejects(
    result,
    (error) => error.code === "BW_READER_QUERY_LOOKUP",
    "空词条会被读成「这个词不存在」",
  );
});

test("查词参数非法时拒绝，不发请求", async () => {
  for (const word of ["", "   ", "x".repeat(129)]) {
    const { result, sent } = callFetching("nativeReaderLookupWord", {
      input: { word }, data: { ok: true },
    });
    await assert.rejects(result, (error) => error.code === "BW_READER_QUERY_PARAMS");
    assert.deepEqual(sent, []);
  }
});

test("生词标记只接受 known/unknown", async () => {
  const ok = callFetching("nativeReaderMarkVocabulary", {
    input: { word: "w", mark: "known" }, data: { ok: true },
  });
  const value = await ok.result;
  assert.equal(value.mark, "known");
  assert.equal(value.word, "w");
  assert.equal(ok.sent.length, 1, "合法取值必须真的发出去");

  for (const mark of ["", "yes", "KNOWN", null]) {
    const { result } = callFetching("nativeReaderMarkVocabulary", {
      input: { word: "w", mark }, data: { ok: true },
    });
    await assert.rejects(result, (error) => error.code === "BW_READER_VOCAB_MARK");
  }
});

test("生词标记未确认成功时如实报错，不当作已记上", async () => {
  const { result } = callFetching("nativeReaderMarkVocabulary", {
    input: { word: "w", mark: "known" },
    status: 503, data: { ok: false, error: "网关不可用" },
  });
  await assert.rejects(
    result,
    (error) => error.code === "BW_READER_VOCAB_FAILED",
    "一次没记上的「已掌握」，下次阅读还会被划成生词",
  );
});

test("查词与生词标记的工具描述交代了对 Pi 的依赖", () => {
  const lookup = MCP.slice(
    MCP.indexOf('["name"] = LookupToolName'),
    MCP.indexOf('["name"] = LookupToolName') + 2600,
  ).replace(/"\s*\+\s*"/g, "");
  assert.match(lookup, /needs the Pi/i, "离线即失效，必须说清楚");
  assert.match(lookup, /not recorded against the user's vocabulary/i);

  const mark = MCP.slice(
    MCP.indexOf('["name"] = MarkVocabToolName'),
    MCP.indexOf('["name"] = MarkVocabToolName') + 2600,
  ).replace(/"\s*\+\s*"/g, "");
  assert.match(mark, /Needs the Pi/i);
  assert.match(
    mark, /Ask before marking words the user did not bring up/i,
    "这会改变用户阅读器里的下划线和 Anki 的排程",
  );
});

// ── 网页宿主：AI 要能看到扩展在网页上做的高亮 ────────────────────────
// 书籍高亮在 App 的本机库，网页高亮在扩展的 webHighlightsV1。同一个查询名
// 落到不同宿主，各答各的 —— 谁都不在就回 unsupported，绝不去猜另一边：
// 答错宿主的数据比答不上来更糟，因为它看起来是对的。
function webQuery({ records = [], params = {}, host } = {}) {
  const start = VOICE.indexOf("function _webHighlightsQuery(");
  assert.notEqual(start, -1, "找不到网页高亮查询实现");
  const context = {
    String, Number, Array, Promise, JSON,
    location: { href: "https://example.test/a" },
    out: null,
  };
  context.globalThis = context;
  vm.runInNewContext(
    `${balanced(VOICE, start)}
     out = _webHighlightsQuery(
       ${host ? "host" : "{ list: () => records }"},
       ${JSON.stringify(params)});`,
    Object.assign(context, { records, host }),
  );
  return context.out;
}

const WEB_RECORDS = [
  { id: "wh_1", url: "https://example.test/a", text: "alpha 段落", exact: "alpha 段落",
    prefix: "前文".repeat(20), suffix: "后文".repeat(20),
    color: "#fff59d", note: "记一笔", kind: "note", time: 1000 },
  { id: "wh_2", url: "https://example.test/a", text: "beta 段落", exact: "beta 段落",
    prefix: "x".repeat(48), suffix: "y".repeat(48),
    color: "#a5d8ff", note: "", kind: "note", time: 2000 },
];

test("网页高亮能被读到，按时间排序", async () => {
  const value = await webQuery({ records: WEB_RECORDS });
  assert.equal(value.surface, "web");
  assert.deepEqual(
    JSON.parse(JSON.stringify(value.highlights.map((h) => h.id))),
    ["wh_1", "wh_2"],
  );
  assert.equal(value.matched, 2);
  assert.equal(value.truncated, false);
});

test("不回锚定用的 prefix/suffix", async () => {
  // 那是定位用的上下文片段，助手拿它做不了什么，却会让结果体积翻倍 ——
  // 与书籍高亮不回 rects 同理。
  const value = await webQuery({ records: WEB_RECORDS });
  for (const item of value.highlights) {
    assert.deepEqual(
      Object.keys(item).sort(),
      ["color", "id", "kind", "note", "text", "time"],
    );
  }
});

test("按文字过滤，忽略大小写", async () => {
  const value = await webQuery({ records: WEB_RECORDS, params: { contains: "BETA" } });
  assert.deepEqual(
    JSON.parse(JSON.stringify(value.highlights.map((h) => h.id))), ["wh_2"],
  );
});

test("装不下时截断并如实标注", async () => {
  const many = Array.from({ length: 400 }, (_, index) => ({
    id: `wh_${index}`, text: "x".repeat(300), color: "#fff59d",
    note: "", kind: "note", time: index,
  }));
  const value = await webQuery({ records: many });
  assert.equal(value.truncated, true);
  assert.equal(value.matched, 400, "总数如实报出，助手才知道少了多少");
  assert.ok(value.returned < value.matched);
});

test("读取失败如实报错，不返回空列表", async () => {
  // 空列表会被读成「这个网页你没划过」，那是一句错的断言。
  await assert.rejects(
    webQuery({ host: { list() { throw new Error("storage unavailable"); } } }),
    (error) => /storage unavailable/.test(String(error.message)),
  );
});

test("两种宿主各答各的，都不在则 unsupported", () => {
  const table = VOICE.slice(
    VOICE.indexOf("var READER_QUERY_HANDLERS = {"),
    VOICE.indexOf("function normalizeReaderQueryRequest("),
  );
  const branch = table.slice(table.indexOf("highlights: function"),
    table.indexOf("notes: function"));
  assert.match(branch, /_nativeReaderHighlights/, "书籍走 App 本机运行时");
  assert.match(branch, /__bwWebHighlights/, "网页走扩展本地存储");
  assert.match(branch, /return null;\s*\},?\s*$/m,
    "两者都不在必须回 null（→ unsupported），不能猜另一边");
});

test("每个查询声明自己适用哪种界面", () => {
  const QUERY_CS = readFileSync(new URL(
    "../../extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderQuery.cs",
    import.meta.url), "utf8");
  const fn = QUERY_CS.slice(
    QUERY_CS.indexOf("IsQueryForSurface"),
    QUERY_CS.indexOf("internal static object Event("));
  // 网页上问目录、在书里问网页锚点，都该在这里被拒，而不是走到执行侧才失败
  assert.match(fn, /"highlights" => kind is "pdf" or "epub" or "web"/);
  assert.match(fn, /"toc" => kind is "pdf"/);
  assert.match(fn, /_ => false/, "未声明的组合一律拒绝");
});
