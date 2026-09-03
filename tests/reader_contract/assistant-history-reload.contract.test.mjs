import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const ASSISTANT = read("_server_deploy/static/pdf/rc-assistant.js");
const DRAWER = read("_server_deploy/static/pdf/rc-sidedrawer.js");
const TURNS = read("_server_deploy/static/pdf/rc-turncard.js");
const VIDEO = read("_server_deploy/static/pdf/rc-video.js");
const PDF_ADAPTER = read("_server_deploy/static/pdf/reader.src/27-rc-adapter.js");
const EPUB = read("_server_deploy/static/pdf/epub-html.js");
const REGISTRY = read("_server_deploy/static/reader-runtime/data-registry.js");
const NATIVE_SYNC = read("ios/BWReader/App/ReaderPiSyncCoordinator.swift");

function section(source, start, end) {
  const from = source.indexOf(start);
  const to = source.indexOf(end, from + start.length);
  assert.notEqual(from, -1, `missing ${start}`);
  assert.notEqual(to, -1, `missing ${end}`);
  return source.slice(from, to);
}

class FakeNode {
  constructor(kind = "element") {
    this.kind = kind;
    this.childNodes = [];
    this.parentNode = null;
    this.attributes = {};
    this.className = "";
    this.innerHTML = "";
    const classes = new Set();
    this.classList = {
      add: (...names) => names.forEach((name) => classes.add(name)),
      remove: (...names) => names.forEach((name) => classes.delete(name)),
      contains: (name) => classes.has(name),
      toggle: (name, force) => {
        const on = force === undefined ? !classes.has(name) : Boolean(force);
        if (on) classes.add(name); else classes.delete(name);
        return on;
      },
    };
  }

  get children() { return this.childNodes.filter((node) => node.kind === "element"); }
  get firstChild() { return this.childNodes[0] || null; }
  get nextSibling() {
    if (!this.parentNode) return null;
    const siblings = this.parentNode.childNodes;
    return siblings[siblings.indexOf(this) + 1] || null;
  }

  appendChild(node) {
    if (node.kind === "fragment") {
      while (node.firstChild) this.appendChild(node.firstChild);
      return node;
    }
    if (node.parentNode) node.parentNode.removeChild(node);
    this.childNodes.push(node);
    node.parentNode = this;
    return node;
  }

  removeChild(node) {
    const index = this.childNodes.indexOf(node);
    if (index < 0) throw new Error("missing child");
    this.childNodes.splice(index, 1);
    node.parentNode = null;
    return node;
  }

  remove() { if (this.parentNode) this.parentNode.removeChild(this); }
  replaceWith(node) {
    if (!this.parentNode) return;
    const parent = this.parentNode;
    const index = parent.childNodes.indexOf(this);
    this.remove();
    if (node.parentNode) node.parentNode.removeChild(node);
    parent.childNodes.splice(index, 0, node);
    node.parentNode = parent;
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
}

function historyHarness(fetchImpl) {
  const thread = new FakeNode();
  const pane = new FakeNode();
  const old = new FakeNode(); old.innerHTML = "old history"; thread.appendChild(old);
  const toasts = [];
  const document = {
    createElement: () => new FakeNode(),
    createComment: () => new FakeNode("comment"),
    createDocumentFragment: () => new FakeNode("fragment"),
  };
  const sandbox = {
    Array,
    Error,
    Number,
    Promise,
    RC: { turnCard: { prune() {} } },
    HOST: {},
    document,
    fetch: fetchImpl,
    pane,
    thread,
    old,
    toasts,
    _assistantMode: "normal",
    _modeEpoch: 0,
    _historyEpoch: 0,
    _historyLoadCount: 0,
    _historyRenderTarget: null,
    _toast: (message) => toasts.push(message),
    _modeNorm: (mode) => mode === "review" ? "review" : "normal",
    _historyUrl: (mode) => mode === "review" ? "/review" : "/normal",
    _ctxCard: () => null,
    esc: (value) => String(value ?? ""),
    requestAnimationFrame: (fn) => fn(),
    setTimeout: (fn) => fn(),
    scrollDown() {},
    greet() { throw new Error("error path must not greet"); },
  };
  sandbox.window = sandbox;
  sandbox.addMsg = function addMsg(cls, html) {
    const node = new FakeNode();
    node.className = cls;
    node.innerHTML = html;
    (sandbox._historyRenderTarget || thread).appendChild(node);
    return node;
  };
  const source = section(
    ASSISTANT,
    "  function _historyReplayError",
    "  function reloadHistory"
  );
  vm.runInNewContext(
    `${source}\nthis.loadHistoryForTest = loadHistory; this.historyTurnIdForTest = _historyTurnId;`,
    sandbox,
    {
    filename: "assistant-history-reload-harness.js",
    },
  );
  return sandbox;
}

class FakeClock {
  constructor() { this.now = 1000; this.nextId = 1; this.tasks = []; }
  setTimeout(fn, delay = 0) {
    const id = this.nextId++;
    this.tasks.push({ id, at: this.now + Math.max(0, Number(delay) || 0), fn });
    return id;
  }
  clearTimeout(id) { this.tasks = this.tasks.filter((task) => task.id !== id); }
  tick(ms) {
    const end = this.now + ms;
    while (true) {
      this.tasks.sort((a, b) => a.at - b.at || a.id - b.id);
      const next = this.tasks[0];
      if (!next || next.at > end) break;
      this.tasks.shift();
      this.now = next.at;
      next.fn();
    }
    this.now = end;
  }
}

async function flushPromises() {
  for (let i = 0; i < 8; i += 1) await Promise.resolve();
}

function coordinatorHarness(fetchImpl) {
  const clock = new FakeClock();
  const thread = new FakeNode();
  const pane = new FakeNode(); pane.classList.add("active");
  const document = {
    createElement: () => new FakeNode(),
    createComment: () => new FakeNode("comment"),
    createDocumentFragment: () => new FakeNode("fragment"),
  };
  const sandbox = {
    Array, Error, JSON, Math, Number, Object, Promise, String,
    Date: { now: () => clock.now },
    RC: {
      assistant: {},
      sidedrawer: { isOpen: () => true },
      turnCard: { prune() {}, renderTurn() { return true; } },
    },
    HOST: {}, document, pane, thread,
    fetch: fetchImpl,
    setTimeout: (fn, delay) => clock.setTimeout(fn, delay),
    clearTimeout: (id) => clock.clearTimeout(id),
    requestAnimationFrame: (fn) => fn(),
    _assistantMode: "normal",
    _modeEpoch: 0,
    _historyEpoch: 0,
    _historyLoadCount: 0,
    _historyReloadTimer: null,
    _historyReloadQueued: null,
    _historyReloadInFlight: null,
    _historyLastPublicReloadAt: 0,
    _historyRenderTarget: null,
    _clearing: false,
    streaming: false,
    _modeNorm: (mode) => mode === "review" ? "review" : "normal",
    _historyUrl: (mode) => mode === "review" ? "/review" : "/normal",
    _ctxCard: () => null,
    _splitFollowups: (text) => ({ text, followups: [] }),
    _attachClipBtn() {}, _bubDecor() {}, _renderFollowups() {}, _attachFeedback() {},
    renderMd: (node, text) => { node.innerHTML = text; },
    esc: (value) => String(value ?? ""),
    scrollDown() {},
    greet() {},
    _toast() {},
  };
  sandbox.window = sandbox;
  sandbox.addMsg = function addMsg(cls, html) {
    const node = new FakeNode(); node.className = cls; node.innerHTML = html;
    (sandbox._historyRenderTarget || thread).appendChild(node);
    return node;
  };
  const source = section(
    ASSISTANT,
    "  var _liveSeen = {};",
    "  pane.dataset.assistantMode"
  );
  vm.runInNewContext(source, sandbox, { filename: "assistant-history-coordinator-harness.js" });
  return { sandbox, clock, thread };
}

test("online history reload is atomic and isolates one malformed record", async () => {
  const harness = historyHarness(async () => ({
    ok: true,
    async json() {
      return {
        ok: true,
        messages: [
          { role: "user", content: "first" },
          {
            role: "assistant",
            content: "broken turn",
            turn_id: "must-not-ack",
            parts: [{ kind: "text", text: "cannot render in this harness" }],
          },
          { role: "user", content: "second" },
        ],
      };
    },
  }));

  const result = await harness.loadHistoryForTest("normal");
  assert.deepEqual(
    JSON.parse(JSON.stringify(result)),
    { ok: true, count: 3, skipped: 1, turnIds: [] },
    "a malformed assistant row is not acknowledged merely because it carries a turn_id",
  );
  assert.notEqual(harness.old.parentNode, harness.thread, "old DOM is replaced only after the replay batch succeeds");
  assert.deepEqual(
    harness.thread.children.map((node) => node.innerHTML),
    ["first", "有 1 条旧对话暂时无法显示，其余记录已恢复。", "second"],
  );
});

test("network, HTTP or parse failure preserves the last successful DOM and never greets", async (t) => {
  const cases = [
    ["network", async () => { throw new Error("offline"); }],
    ["HTTP", async () => ({ ok: false, status: 503, async json() { return {}; } })],
    ["parse", async () => ({ ok: true, status: 200, async json() { throw new Error("bad json"); } })],
  ];
  for (const [name, fetchImpl] of cases) {
    await t.test(name, async () => {
      const harness = historyHarness(fetchImpl);
      const result = await harness.loadHistoryForTest("normal");
      assert.equal(result.ok, false);
      assert.equal(harness.thread.children.length, 1);
      assert.equal(harness.thread.children[0], harness.old);
      assert.equal(harness.toasts.length, 1);
      assert.match(harness.toasts[0], /现有记录已保留/);
    });
  }
});

test("history replay ids prefer stable server identity and never depend on window index", () => {
  const harness = historyHarness(async () => ({ ok: true, async json() { return { ok: true, messages: [] }; } }));
  const stable = {
    role: "assistant",
    content: "same old answer",
    history_id: "h_row_1",
    parts: [{ kind: "text", text: "x" }],
  };
  assert.equal(
    harness.historyTurnIdForTest(stable, "normal", "/book/a/history"),
    "hist_normal_h_row_1",
  );
  assert.equal(
    harness.historyTurnIdForTest(stable, "normal", "/book/b/history"),
    "hist_normal_h_row_1",
    "the same persisted row keeps its DOM identity when the public history window moves",
  );
  assert.equal(
    harness.historyTurnIdForTest(stable, "review", "/book/a/history"),
    "hist_review_h_row_1",
    "normal and review DOM identities remain isolated even if a stored id is repeated across scopes",
  );

  const direct = { role: "assistant", content: "direct", turn_id: "remote-1" };
  assert.equal(
    harness.historyTurnIdForTest(direct, "normal", "/book/a/history"),
    "hist_normal_remote-1",
  );

  const legacy = { role: "assistant", content: "legacy fallback", ts: 123 };
  const first = harness.historyTurnIdForTest(legacy, "normal", "/book/a/history");
  assert.equal(first, harness.historyTurnIdForTest(legacy, "normal", "/book/a/history"));
  assert.notEqual(first, harness.historyTurnIdForTest(legacy, "normal", "/book/b/history"));
});

test("drawer, native sync and repeated assistant-history signals coalesce into one atomic reload", async () => {
  let historyGets = 0;
  const acknowledgements = [];
  const harness = coordinatorHarness(async (url, options = {}) => {
    if (url === "/pdf/api/turn-ack") {
      acknowledgements.push(JSON.parse(options.body).turn_id);
      return { ok: true, async json() { return { ok: true }; } };
    }
    historyGets += 1;
    return {
      ok: true,
      async json() {
        return { ok: true, messages: [
          { role: "user", content: "question" },
          { role: "assistant", content: "answer", turn_id: "remote-1" },
        ] };
      },
    };
  });

  harness.sandbox.RC.assistant.onDrawerTabChanged("asst", true);
  harness.sandbox.RC.assistant.onHistoryEvent({ turn_id: "remote-1" });
  for (let i = 0; i < 500; i += 1) {
    harness.sandbox.RC.assistant.onDrawerTabChanged("asst", true);
    harness.sandbox.RC.assistant.onNativePiSyncFinished();
    harness.sandbox.RC.assistant.reloadHistory();
    harness.sandbox.RC.assistant.onHistoryEvent({ turn_id: `storm-${i}` });
  }
  assert.equal(historyGets, 0, "publicly reachable triggers are debounced before network I/O");

  harness.clock.tick(80);
  await flushPromises();
  assert.equal(historyGets, 1);
  assert.equal(harness.thread.children.length, 2, "one authoritative replay replaces, rather than appends");
  assert.deepEqual(acknowledgements, ["remote-1"]);
});

test("assistant-history arriving during a full reload is serialized and never duplicated", async () => {
  let resolveFirst;
  let historyGets = 0;
  const acknowledgements = [];
  const harness = coordinatorHarness((url, options = {}) => {
    if (url === "/pdf/api/turn-ack") {
      acknowledgements.push(JSON.parse(options.body).turn_id);
      return Promise.resolve({ ok: true, async json() { return { ok: true }; } });
    }
    historyGets += 1;
    if (historyGets === 1) {
      return new Promise((resolve) => { resolveFirst = () => resolve({
        ok: true,
        async json() { return { ok: true, messages: [{ role: "user", content: "old" }] }; },
      }); });
    }
    return Promise.resolve({
      ok: true,
      async json() { return { ok: true, messages: [
        { role: "user", content: "question" },
        { role: "assistant", content: "new", turn_id: "remote-race" },
      ] }; },
    });
  });

  harness.sandbox.RC.assistant.onDrawerTabChanged("asst", true);
  harness.clock.tick(80);
  await flushPromises();
  assert.equal(historyGets, 1);
  harness.sandbox.RC.assistant.onHistoryEvent({ turn_id: "remote-race" });
  harness.sandbox.RC.assistant.onHistoryEvent({ turn_id: "remote-race" });
  resolveFirst();
  await flushPromises();

  harness.clock.tick(400);
  await flushPromises();
  assert.equal(historyGets, 2, "the realtime signal becomes one queued successor load");
  assert.deepEqual(harness.thread.children.map((node) => node.innerHTML), ["question", "new"]);
  assert.deepEqual(acknowledgements, ["remote-race"]);
});

test("normal, review and EPUB reloads retain their existing authoritative scopes", () => {
  assert.match(ASSISTANT, /return _modeNorm\(mode \|\| _assistantMode\) === 'review'[\s\S]*'\/api\/assistant\/history\?assistant_mode=review'[\s\S]*_NORMAL_HISTURL/);
  assert.match(PDF_ADAPTER, /historyUrl: \(\) => '\/api\/assistant\/history'/);
  assert.match(EPUB, /historyUrl: function \(\) \{ return '\/pdf\/api\/epub-convo\?file=' \+ encodeURIComponent\(FREL\); \}/);
  assert.match(ASSISTANT, /RC\.assistant\.reloadHistory = function/);
  assert.match(ASSISTANT, /RC\.assistant\.onDrawerTabChanged = function/);
  assert.match(ASSISTANT, /RC\.assistant\.onNativePiSyncFinished = function/);
  assert.doesNotMatch(ASSISTANT, /addEventListener\('rc:sidedrawer-tab-changed'/);
  assert.doesNotMatch(ASSISTANT, /addEventListener\('bw:reader-pi-sync-finished'/);
  assert.match(ASSISTANT, /_historyEpoch\+\+;\s*\/\/ pending online history may never replace/);
  assert.match(ASSISTANT, /_queueHistoryReload\(\{ reason: 'post-stream-history' \}\)/);
  assert.match(ASSISTANT, /_historyReloadInFlight/);
  assert.match(ASSISTANT, /Object\.keys\(_historyPendingTurns\)\.length >= 64/);
  assert.match(ASSISTANT, /while \(_liveSeenOrder\.length > 256\)/);
  assert.match(ASSISTANT, /400 - Math\.max\(0, Date\.now\(\) - _historyLastPublicReloadAt\)/);
  assert.match(ASSISTANT, /renderVideos\(m\.videos, \{ host: el, dedupe: false \}\)/);
  assert.match(ASSISTANT, /function _historyTurnId\(message, mode, scope\)/);
  assert.match(ASSISTANT, /message\.history_id/);
  assert.match(ASSISTANT, /'\|scope:' \+ String/);
  assert.doesNotMatch(ASSISTANT, /function _historyTurnId\(message, index/);
  assert.doesNotMatch(ASSISTANT, /'\|record:' \+ String/);
  assert.match(ASSISTANT, /renderTurn\(\s*_rtid, m\.parts, target, \{ historyReplay: true \}/);
  assert.doesNotMatch(ASSISTANT, /'h' \+ token \+ '_' \+ index/);
  assert.match(TURNS, /historyReplay: options\.historyReplay === true/);
  assert.match(TURNS, /p\.draft && RC\.flashcard\.presentDraft && !t\.historyReplay/);
  assert.match(VIDEO, /window\.renderVideos = function \(videos, options\)/);
  assert.match(VIDEO, /var host = options\.host \|\| _hostBubble\(\)/);
  assert.match(DRAWER, /RC\.assistant\.onDrawerTabChanged\(name, true\)/);
  assert.doesNotMatch(DRAWER, /CustomEvent\('rc:sidedrawer-tab-changed'/);
});

test("manual Pi sync refresh is online-only and conversation collections remain pending", () => {
  assert.match(NATIVE_SYNC, /对话记录（联网时从服务器在线恢复，不在离线同步包）/);
  const refresh = section(NATIVE_SYNC, "        // 对话仍由 Pi", "        return ReaderPiDataSyncReport(");
  assert.match(refresh, /RC\.assistant\.onNativePiSyncFinished/);
  assert.doesNotMatch(refresh, /decoded\.state == \.complete|decoded\.state == \.partial/);
  assert.doesNotMatch(refresh, /new CustomEvent|dispatchEvent\(/);
  assert.match(TURNS, /function renderTurn\(tid, parts, target, options\)/);
  assert.match(TURNS, /prune: prune/);
  for (const collection of [
    "conversation-threads",
    "conversation-messages",
    "conversation-summaries",
  ]) {
    assert.match(
      REGISTRY,
      new RegExp(`'${collection}': \\{[\\s\\S]{0,160}status: 'pending', provider: false`),
    );
  }
});
