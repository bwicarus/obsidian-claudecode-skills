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

function runHandler({
  query = "highlights", params = {}, impl, host,
  pageCardsImpl, pageCardImpl, activeReadingSnapshot,
} = {}) {
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
    pageCards: pageCardsImpl || (() => Promise.reject(new Error("page cards unavailable"))),
    pageCardIndex: pageCardsImpl || (() => Promise.reject(new Error("page cards unavailable"))),
    pageCard: pageCardImpl || (() => Promise.reject(new Error("page card unavailable"))),
    localActiveReadingSnapshot: activeReadingSnapshot || (() => null),
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

function functionSource(name) {
  const start = VOICE.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `找不到 ${name}`);
  return balanced(VOICE, start);
}

function runPageCard({
  input = {}, page = 7, projectionCards = [], revision = 4,
  sourceById = {}, activeReadingSnapshot,
} = {}) {
  const pageReads = [];
  const sourceReads = [];
  const runtime = {
    pageCardSource(selector) {
      sourceReads.push(structuredClone(selector));
      const source = sourceById[selector.id];
      return Promise.resolve(typeof source === "function" ? source(selector) : source);
    },
  };
  const context = {
    Error, String, Number, Array, Promise, JSON, Object, TextEncoder,
    LOCAL_PAGE_CARD_SOURCE_CONTRACT: "reader-local-page-card-source/1",
    READER_PAGE_CARD_DETAIL_CONTRACT: "reader-page-card-detail/1",
    localActiveReadingSnapshot: activeReadingSnapshot
      || (() => ({ kind: "pdf", page })),
    localNativePageRuntime: () => runtime,
    pageCards(requestedPage) {
      pageReads.push(requestedPage);
      return Promise.resolve({
        contract: "reader-local-page-card-projection/1",
        page: requestedPage,
        revision,
        cards: structuredClone(projectionCards),
      });
    },
    input,
    out: null,
  };
  context.globalThis = context;
  vm.runInNewContext(
    `${functionSource("directError")}
     ${functionSource("plainObject")}
     ${functionSource("exactObject")}
     ${functionSource("messageBytes")}
     ${functionSource("localPageCardUTF8Chunk")}
     ${functionSource("pageCard")}
     out = Promise.resolve().then(function () { return pageCard(input); });`,
    context,
  );
  return { result: context.out, pageReads, sourceReads };
}

function runPageCardIndex(cards, revision = 4) {
  const context = {
    String, Number, Array, Promise, JSON, Object, TextEncoder,
    pageCards(page) {
      return Promise.resolve({
        contract: "reader-local-page-card-projection/1",
        page,
        revision,
        cards: structuredClone(cards),
      });
    },
    out: null,
  };
  context.globalThis = context;
  vm.runInNewContext(
    `${functionSource("messageBytes")}
     ${functionSource("localPageCardUTF8Chunk")}
     ${functionSource("pageCardIndex")}
     out = pageCardIndex(7);`,
    context,
  );
  return context.out;
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

test("page-cards 显式页码复用只读投影；省略页码只认可靠 PDF 快照", async () => {
  const calls = [];
  const explicit = runHandler({
    query: "page-cards",
    params: { page: 9 },
    pageCardsImpl(page) {
      calls.push(page);
      return Promise.resolve({
        contract: "reader-local-page-card-projection/1",
        page,
        revision: 4,
        cards: [],
      });
    },
  });
  await new Promise((resolve) => setImmediate(() => setImmediate(resolve)));
  assert.deepEqual(calls, [9]);
  assert.equal(explicit[0].status, "ok");
  assert.equal(explicit[0].result.page, 9);

  const implicitCalls = [];
  const implicit = runHandler({
    query: "page-cards",
    params: {},
    activeReadingSnapshot: () => ({ kind: "pdf", page: 7 }),
    pageCardsImpl(page) {
      implicitCalls.push(page);
      return Promise.resolve({
        contract: "reader-local-page-card-projection/1",
        page,
        revision: 5,
        cards: [],
      });
    },
  });
  await new Promise((resolve) => setImmediate(() => setImmediate(resolve)));
  assert.deepEqual(implicitCalls, [7]);
  assert.equal(implicit[0].status, "ok");

  const unsupported = runHandler({
    query: "page-cards",
    params: {},
    activeReadingSnapshot: () => ({ kind: "epub", page: 7 }),
  });
  assert.equal(unsupported[0].status, "unsupported",
    "不能从多页 DOM 顺序猜当前 PDF 页");
});

test("page-cards 是含正文摘要的有界索引，超预算返回真实前缀", async () => {
  const small = await runPageCardIndex([
    {
      number: 1, id: "c_small0000000001", kind: "card", type: "card",
      label: "锚定词", text: "不是标签而是真实卡片正文", content: "不是标签而是真实卡片正文",
      bind: { kind: "page-chars", page: 7, from: 1, to: 3, text: "词" },
      revision: 4, unbound: false,
    },
  ]);
  assert.equal(small.truncated, false);
  assert.equal(small.count, 1);
  assert.equal(small.returned, 1);
  assert.equal(small.cards[0].content, "不是标签而是真实卡片正文");
  assert.equal(small.cards[0].content_truncated, false);
  assert.equal("text" in small.cards[0], false, "索引不重复携带同一正文两次");

  const many = Array.from({ length: 120 }, (_, index) => ({
    number: index + 1,
    id: `c_index${String(index).padStart(10, "0")}`,
    kind: "card", type: "card", label: `词${index}`,
    text: "长正文".repeat(900), content: "长正文".repeat(900),
    bind: {
      kind: "page-chars", page: 7,
      from: index * 2, to: index * 2 + 1, text: `词${index}`,
    },
    revision: 4, unbound: false,
  }));
  const bounded = await runPageCardIndex(many);
  assert.equal(bounded.count, many.length);
  assert.equal(bounded.truncated, true);
  assert.ok(bounded.returned > 0 && bounded.returned < bounded.count);
  assert.ok(
    new TextEncoder().encode(JSON.stringify(bounded)).byteLength <= 32 * 1024,
    "索引必须真的落在 Reader 查询帧预算内",
  );
  assert.equal(bounded.cards[0].content_truncated, true);
});

test("page-card WSS 查询显式分派并原样回传分块状态", async () => {
  const calls = [];
  const params = {
    id: "c_detailcard00001", offset: 12, limit: 1000, expectedRevision: 4,
  };
  const sent = runHandler({
    query: "page-card",
    params,
    pageCardImpl(input) {
      calls.push(structuredClone(input));
      return Promise.resolve({
        contract: "reader-page-card-detail/1",
        content: "后续内容",
        next_offset: 16,
        truncated: true,
      });
    },
  });
  await new Promise((resolve) => setImmediate(() => setImmediate(resolve)));
  assert.deepEqual(calls, [params]);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].status, "ok");
  assert.equal(sent[0].truncated, true);
  assert.equal(sent[0].result.next_offset, 16);
});

test("page-card 可按当前序号或稳定 id 读取同一份权威源内容", async () => {
  const cards = [
    {
      number: 1,
      id: "c_firstcard000001",
      kind: "card",
      type: "card",
      label: "第一张",
      bind: { kind: "page-chars", page: 7, from: 1, to: 2, text: "甲乙" },
      unbound: false,
    },
    {
      number: 2,
      id: "c_secondcard00002",
      kind: "anki",
      type: "anki",
      label: "第二张",
      bind: { kind: "page-chars", page: 7, from: 8, to: 9, text: "丙丁" },
      unbound: false,
    },
  ];
  const content = JSON.stringify({
    contract: "reader-page-card-source/1",
    cards: [{ type: "basic", front: "完整问题", back: "完整答案" }],
  });
  const sourceById = {
    [cards[1].id]: {
      contract: "reader-local-page-card-source/1",
      page: 7,
      id: cards[1].id,
      kind: cards[1].kind,
      revision: 4,
      content,
    },
  };

  const byNumber = runPageCard({
    input: { page: 7, number: 2, offset: 0, limit: 24576 },
    projectionCards: cards,
    sourceById,
  });
  const numbered = await byNumber.result;
  assert.deepEqual(byNumber.pageReads, [7]);
  assert.deepEqual(byNumber.sourceReads, [{ page: 7, id: cards[1].id }]);
  assert.equal(numbered.card.number, 2);
  assert.equal(numbered.card.id, cards[1].id,
    "可见序号只能解析到该次投影里的稳定 placement id");
  assert.equal(numbered.content, content);
  assert.equal(numbered.next_offset, null);

  const byId = runPageCard({
    input: { id: cards[1].id, offset: 0, limit: 24576 },
    projectionCards: cards,
    sourceById,
  });
  const stable = await byId.result;
  assert.equal(stable.card.id, cards[1].id);
  assert.equal(stable.card.number, 2);
  assert.equal(stable.content, content);
});

test("page-card 中文源内容按 UTF-16 offset 连续续读且每块不超过 24 KiB UTF-8", async () => {
  const id = "c_chinesechunk0001";
  const card = {
    number: 1,
    id,
    kind: "card",
    type: "card",
    label: "中文长卡",
    bind: { kind: "page-chars", page: 7, from: 3, to: 5, text: "中文卡" },
    unbound: false,
  };
  const content = JSON.stringify({ html: `<p>${"汉字🙂".repeat(9000)}</p>` });
  const sourceById = {
    [id]: {
      contract: "reader-local-page-card-source/1",
      page: 7,
      id,
      kind: "card",
      revision: 4,
      content,
    },
  };
  let offset = 0;
  let combined = "";
  let chunks = 0;
  let expectedRevision = null;
  while (true) {
    const { result } = runPageCard({
      input: Object.assign(
        { id, offset, limit: 24576 },
        offset > 0 ? { expectedRevision } : {},
      ),
      projectionCards: [card],
      sourceById,
    });
    const value = await result;
    if (expectedRevision === null) expectedRevision = value.revision;
    assert.equal(value.revision, expectedRevision);
    assert.equal(value.offset, offset);
    assert.ok(value.content.length <= 24576, "limit 按 UTF-16 code units 约束");
    assert.ok(
      Buffer.byteLength(value.content, "utf8") <= 24 * 1024,
      "中文与 emoji 也必须落在独立的 24 KiB UTF-8 上限内",
    );
    assert.doesNotMatch(value.content.slice(-1), /[\uD800-\uDBFF]/,
      "分块不得把 surrogate pair 从中间切开");
    assert.doesNotMatch(value.content.slice(0, 1), /[\uDC00-\uDFFF]/,
      "续块不得从孤立 low surrogate 开始");
    combined += value.content;
    chunks += 1;
    if (value.next_offset === null) {
      assert.equal(value.truncated, false);
      assert.equal(offset + value.content.length, content.length);
      break;
    }
    assert.equal(value.truncated, true);
    assert.equal(value.next_offset, offset + value.content.length,
      "next_offset 必须紧接本块，不能跳字或重叠");
    assert.ok(value.next_offset < content.length);
    offset = value.next_offset;
  }
  assert.ok(chunks > 1, "样本必须真的触发 UTF-8 分块");
  assert.equal(combined, content, "沿 next_offset 续读必须无损拼回完整源 JSON");
});

test("page-card 续块必须沿用首块 revision，内容变化时拒绝混拼", async () => {
  const id = "c_revisionguard001";
  const card = {
    number: 1, id, kind: "card", type: "card", label: "修订守卫",
    bind: { kind: "page-chars", page: 7, from: 1, to: 2, text: "修订" },
    unbound: false,
  };
  const sourceById = {
    [id]: {
      contract: "reader-local-page-card-source/1", page: 7, id,
      kind: "card", revision: 5, content: JSON.stringify({ content: "新版本" }),
    },
  };
  const { result } = runPageCard({
    input: { id, offset: 2, limit: 10, expectedRevision: 4 },
    projectionCards: [card], revision: 5, sourceById,
  });
  await assert.rejects(
    result,
    (error) => error.code === "BW_READER_PAGE_CARD_STALE"
      && error.retryable === true,
  );
});

test("page-card 自由卡没有序号，仍可凭稳定 id 读取", async () => {
  const id = "c_freecard0000001";
  const content = JSON.stringify({ html: "<article>自由卡完整正文</article>" });
  const card = {
    number: null,
    id,
    kind: "card",
    type: "card",
    label: "自由卡",
    bind: null,
    unbound: true,
  };
  const { result } = runPageCard({
    input: { id, offset: 0, limit: 24576 },
    projectionCards: [card],
    sourceById: {
      [id]: {
        contract: "reader-local-page-card-source/1",
        page: 7,
        id,
        kind: "card",
        revision: 4,
        content,
      },
    },
  });
  const value = await result;
  assert.equal(value.card.id, id);
  assert.equal(value.card.number, null);
  assert.equal(value.card.bind, null);
  assert.equal(value.card.unbound, true);
  assert.equal(value.content, content);
});

test("page-card 非法选择器、分块边界与未知字段全部 fail closed", async () => {
  const validCard = {
    number: 1,
    id: "c_validcard000001",
    kind: "card",
    type: "card",
    label: "合法卡",
    bind: { kind: "page-chars", page: 7, from: 0, to: 0, text: "甲" },
    unbound: false,
  };
  for (const input of [
    {},
    { page: 0, id: validCard.id },
    { id: "x" },
    { id: "bad id" },
    { number: 0 },
    { id: validCard.id, number: 1 },
    { id: validCard.id, offset: -1 },
    { id: validCard.id, offset: 1 },
    { id: validCard.id, limit: 24577 },
    { id: validCard.id, expectedRevision: -1 },
    { id: validCard.id, extra: true },
  ]) {
    const { result } = runPageCard({ input, projectionCards: [validCard] });
    await assert.rejects(
      result,
      (error) => error.retryable === false
        && ["BW_READER_PAGE_CARD_PARAMS", "BW_COMPUTER_VOICE_DIRECT_SCHEMA"]
          .includes(error.code),
      `非法参数不得落到权威内容读取：${JSON.stringify(input)}`,
    );
  }
});

test("page-card MCP 参数层严格 id xor number，并补齐连续续读默认值", () => {
  const start = MCP.indexOf("internal static bool TryReadPageCardReadQuery(");
  const end = MCP.indexOf("internal static bool TryReadPageCardMutation(", start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const parser = MCP.slice(start, end);
  assert.match(parser, /actual\.Contains\("id"\) == actual\.Contains\("number"\)/,
    "缺 selector 与同时给两个 selector 都必须拒绝");
  assert.match(parser, /long offset = 0;/);
  assert.match(parser, /int limit = ReaderQueryProtocol\.MaximumPageCardChunkCodeUnits;/);
  assert.match(parser, /parameters\["offset"\] = offset;/);
  assert.match(parser, /parameters\["limit"\] = limit;/);
  assert.match(parser, /else if \(offset > 0\)/,
    "续块缺 expectedRevision 必须拒绝");
  assert.match(parser, /parameters\["expectedRevision"\] = expectedRevision;/);

  const matchStart = QUERY.indexOf("internal static bool PageCardResponseMatchesRequest(");
  const matchEnd = QUERY.indexOf("internal static void RequireBoundedJson(", matchStart);
  const matcher = QUERY.slice(matchStart, matchEnd);
  for (const guard of [
    'parameters["offset"]', 'parameters["limit"]', 'parameters["page"]',
    'parameters["id"]', 'parameters["number"]',
    'parameters["expectedRevision"]',
  ]) {
    assert.ok(matcher.includes(guard), `回包缺请求匹配守卫 ${guard}`);
  }
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
function callFetching(name, {
  input,
  surface = "pdf",
  status = 200,
  data,
  vocabularyState = null,
  applyVocabLocalOverride = null,
} = {}) {
  const start = RUNTIME.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `找不到 ${name}`);
  let dependency = "";
  if (name === "nativeReaderMarkVocabulary") {
    const helperStart = RUNTIME.indexOf("function projectNativeReaderVocabulary(");
    assert.notEqual(helperStart, -1, "找不到本地词汇投影 helper");
    dependency = balanced(RUNTIME, helperStart) + "\n";
  }
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
      BWReaderRuntime: vocabularyState == null ? {} : { vocabularyState },
      applyVocabLocalOverride,
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
    `${dependency}${balanced(RUNTIME, start)}
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

test("七个 Reader 查询名两端一致，且都显式映射", () => {
  const table = VOICE.slice(
    VOICE.indexOf("var READER_QUERY_HANDLERS = {"),
    VOICE.indexOf("function normalizeReaderQueryRequest("),
  );
  for (const name of [
    "highlights", "notes", "search", "toc", "page-text", "page-cards", "page-card",
  ]) {
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
  assert.equal(value.localProjected, true);
  assert.equal(ok.sent.length, 1, "合法取值必须真的发出去");

  for (const mark of ["", "yes", "KNOWN", null]) {
    const { result } = callFetching("nativeReaderMarkVocabulary", {
      input: { word: "w", mark }, data: { ok: true },
    });
    await assert.rejects(result, (error) => error.code === "BW_READER_VOCAB_MARK");
  }
});

test("生词标记按语言写兼容词库并在确认后热更新 App 本地投影", async () => {
  const stateCalls = [];
  const legacyCalls = [];
  const vocabularyState = {
    CONTRACT: "vocabulary-state/1",
    setMastered(spec, enabled, meta) {
      stateCalls.push({
        spec: structuredClone(spec),
        enabled,
        meta: structuredClone(meta),
      });
    },
  };
  const japanese = callFetching("nativeReaderMarkVocabulary", {
    input: { word: "中学生", mark: "known" },
    data: { ok: true },
    vocabularyState,
    applyVocabLocalOverride(word, mastered, meta) {
      legacyCalls.push({ word, mastered, meta: structuredClone(meta) });
    },
  });
  const value = await japanese.result;
  assert.match(japanese.sent[0], /\/pdf\/api\/jp-vocab-mark$/);
  assert.deepEqual(stateCalls[0], {
    spec: {
      kind: "word", language: "ja", lemma: "中学生",
      word: "中学生", surface: "中学生", forms: [],
    },
    enabled: true,
    meta: { source: "reader-query" },
  });
  assert.deepEqual(legacyCalls[0], {
    word: "中学生",
    mastered: true,
    meta: { word: "中学生", surface: "中学生", forms: [], jp: true },
  });
  assert.equal(value.language, "ja");

  const english = callFetching("nativeReaderMarkVocabulary", {
    input: { word: "integral", mark: "unknown" },
    data: { ok: true },
    vocabularyState,
  });
  await english.result;
  assert.match(english.sent[0], /\/pdf\/api\/vocab-mark$/);
  assert.equal(stateCalls.at(-1).spec.language, "en");
  assert.equal(stateCalls.at(-1).enabled, false);
});

test("生词标记未确认成功时如实报错，不当作已记上", async () => {
  const stateCalls = [];
  const { result } = callFetching("nativeReaderMarkVocabulary", {
    input: { word: "w", mark: "known" },
    status: 503, data: { ok: false, error: "网关不可用" },
    vocabularyState: {
      CONTRACT: "vocabulary-state/1",
      setMastered(...args) { stateCalls.push(args); },
    },
  });
  await assert.rejects(
    result,
    (error) => error.code === "BW_READER_VOCAB_FAILED",
    "一次没记上的「已掌握」，下次阅读还会被划成生词",
  );
  assert.deepEqual(stateCalls, [], "Pi 未确认时不能制造本地假成功");
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

// ── 网页便签 ────────────────────────────────────────────────────────
// 与高亮同构，但多一种失败方式：便签在扩展 background 的仓库里，读要跨进程，
// 仓库可能尚未 READY。那种情况必须抛，不能当成"没有便签" ——
// 空列表会被读成"这个网页你没记过东西"，那是一句错的断言。
function webNotesQuery({ notes = [], params = {}, repository } = {}) {
  const start = VOICE.indexOf("function _webNotesQuery(");
  assert.notEqual(start, -1, "找不到网页便签查询实现");
  const context = {
    String, Number, Array, Promise, JSON,
    location: { href: "https://example.test/a" },
    out: null,
  };
  context.globalThis = context;
  vm.runInNewContext(
    `${balanced(VOICE, start)}
     out = _webNotesQuery(
       ${repository ? "repo" : "{ list: () => Promise.resolve({ notes }) }"},
       ${JSON.stringify(params)});`,
    Object.assign(context, { notes, repo: repository }),
  );
  return context.out;
}

const WEB_NOTES = [
  { id: "n_1", text: "第一条", color: "#fff8c5", created: 1000, updated: 1500,
    anchor: { x: 0.3, y: 0.4, kind: "web", documentId: "d1" } },
  { id: "n_2", text: "第二条 beta", color: "#d0ebff", created: 2000,
    anchor: { x: 0.1, y: 0.9, kind: "web", documentId: "d1" } },
];

test("网页便签能被读到，按时间排序", async () => {
  const value = await webNotesQuery({ notes: WEB_NOTES });
  assert.equal(value.surface, "web");
  assert.deepEqual(
    JSON.parse(JSON.stringify(value.notes.map((n) => n.id))), ["n_1", "n_2"],
  );
  assert.equal(value.matched, 2);
});

test("不回锚点坐标", async () => {
  // 页面坐标与 DOM 位置助手用不上，却让每条大出好几倍 ——
  // 与高亮不回 prefix/suffix、书籍高亮不回 rects 同理。
  const value = await webNotesQuery({ notes: WEB_NOTES });
  for (const item of value.notes) {
    assert.deepEqual(Object.keys(item).sort(), ["color", "id", "text", "time"]);
  }
});

test("按文字过滤", async () => {
  const value = await webNotesQuery({ notes: WEB_NOTES, params: { contains: "BETA" } });
  assert.deepEqual(
    JSON.parse(JSON.stringify(value.notes.map((n) => n.id))), ["n_2"],
  );
});

test("仓库未就绪时抛错，不返回空列表", async () => {
  await assert.rejects(
    webNotesQuery({
      repository: {
        list: () => Promise.reject(
          Object.assign(new Error("尚未 READY"), { code: "BW_DOCUMENT_NOTES_NOT_READY" }),
        ),
      },
    }),
    (error) => error.code === "BW_DOCUMENT_NOTES_NOT_READY",
    "空列表会被读成「这个网页你没记过东西」",
  );
});

test("装不下时截断并如实标注", async () => {
  const many = Array.from({ length: 300 }, (_, index) => ({
    id: `n_${index}`, text: "y".repeat(400), color: "#fff8c5", created: index,
  }));
  const value = await webNotesQuery({ notes: many });
  assert.equal(value.truncated, true);
  assert.equal(value.matched, 300);
  assert.ok(value.returned < value.matched);
});

test("两种宿主各答各的，都不在则 unsupported", () => {
  const table = VOICE.slice(
    VOICE.indexOf("var READER_QUERY_HANDLERS = {"),
    VOICE.indexOf("function normalizeReaderQueryRequest("),
  );
  const branch = table.slice(table.indexOf("notes: function"),
    table.indexOf("search: function"));
  assert.match(branch, /_nativeReaderNotes/, "书籍走 App 本机运行时");
  assert.match(branch, /__bwDocumentNotes/, "网页走扩展仓库");
  assert.match(branch, /return null;/, "都不在必须回 null → unsupported");
});

test("notes 的适用面已含 web", () => {
  const QUERY_CS = readFileSync(new URL(
    "../../extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderQuery.cs",
    import.meta.url), "utf8");
  assert.match(QUERY_CS, /"notes" => kind is "pdf" or "epub" or "web"/);
});
