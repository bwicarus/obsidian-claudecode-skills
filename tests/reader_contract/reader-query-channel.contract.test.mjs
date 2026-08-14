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
  for (const input of [{ page: 0 }, { page: 1.5 }, { contains: "x".repeat(257) }]) {
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
