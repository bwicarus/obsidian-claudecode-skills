import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const ROOT = new URL("../../", import.meta.url);
const BOOK_HOST = readFileSync(
  new URL("_server_deploy/static/reader-runtime/book-host.js", ROOT),
  "utf8",
);
const PAGE_BRIDGE = readFileSync(
  new URL("_server_deploy/static/pdf/pwa-extension-bridge.js", ROOT),
  "utf8",
);
const PDF_HOST = readFileSync(
  new URL("_server_deploy/static/pdf/reader.src/32-extension-host.js", ROOT),
  "utf8",
);
const EPUB_HOST = readFileSync(
  new URL("_server_deploy/static/pdf/epub-html.js", ROOT),
  "utf8",
);
const HTML_HOST = readFileSync(
  new URL("_server_deploy/static/pdf/html-reader.js", ROOT),
  "utf8",
);
const TEMPLATES = ["pdf_reader.html", "epub_html_reader.html", "html_reader.html"]
  .map((name) => readFileSync(new URL(`_server_deploy/templates/${name}`, ROOT), "utf8"));

function loadBookHost() {
  const events = [];
  const sandbox = {
    console,
    document: {
      title: "Contract book",
      dispatchEvent(event) { events.push(event); },
    },
    CustomEvent: class {
      constructor(type, options) {
        this.type = type;
        this.detail = options?.detail;
      }
    },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.runInNewContext(BOOK_HOST, sandbox, { filename: "book-host.js" });
  return { sandbox, events };
}

test("book-host/1 只承认真正的书，并对动作 fail closed", async () => {
  const { sandbox, events } = loadBookHost();
  assert.throws(
    () => sandbox.BWReaderBookHost.register({ mode: "web" }),
    /pdf \/ epub \/ html \/ favorite/,
  );

  let cleared = 0;
  let highlighted = 0;
  const api = sandbox.BWReaderBookHost.register({
    mode: "html",
    file: "books/notes.md",
    title: "Notes",
    langs: ["en"],
    selection: () => ({
      text: "selected text",
      context: "surrounding sentence",
      anchor: { start: 2, end: 15 },
      rect: { left: 1, top: 2, right: 21, bottom: 12 },
    }),
    context: () => ({ file: "books/notes.md", visible_text: "body" }),
    currentLocation: () => ({ unit: "document", index: 0, total: 1 }),
    actions: {
      clear_selection() { cleared += 1; return { ok: true }; },
      highlight() { highlighted += 1; return { id: "hl-1" }; },
      // 映射表外的函数不得因宿主传入就被公开。
      delete_book() { throw new Error("must never run"); },
    },
    capabilities: { highlight: true, deleteBook: true },
  });

  assert.equal(api.contract, "book-host/1");
  assert.equal(api.mode, "html");
  assert.equal(api.selection().file, "books/notes.md");
  assert.equal(api.selection().rect.width, 20);
  assert.equal(api.capabilities.selection, true);
  assert.equal(api.capabilities.highlight, true);
  await api.localAction("clear_selection", {});
  await api.localAction("highlight", {});
  assert.equal(cleared, 1);
  assert.equal(highlighted, 1);
  await assert.rejects(api.localAction("delete_book", {}), /不允许本地命令/);
  await assert.rejects(api.localAction("toggle_ink", {}), /不允许本地命令/);
  assert.equal(events.at(-1).type, "bw:reader-local-api-ready");
  assert.equal(events.at(-1).detail.mode, "html");
});

function bridgeHarness(mode = "pdf") {
  const windowListeners = new Map();
  const documentListeners = new Map();
  const posts = [];
  const intervals = new Map();
  const elements = new Map();
  let intervalSeq = 0;
  let now = 1000;
  let closed = 0;
  let originalLookups = 0;
  const localActions = [];
  const originalLookup = () => { originalLookups += 1; };
  const adapter = { lookupWord: originalLookup };
  const add = (map, name, listener) => {
    if (!map.has(name)) map.set(name, []);
    map.get(name).push(listener);
  };
  const remove = (map, name, listener) => {
    if (!map.has(name)) return;
    map.set(name, map.get(name).filter((value) => value !== listener));
  };
  const root = { dataset: { bwReaderExtension: "test" } };
  const classList = {
    remove() {},
    contains() { return false; },
  };
  const document = {
    title: "Book",
    documentElement: root,
    head: {
      appendChild(element) {
        if (element.id) elements.set(element.id, element);
      },
    },
    body: { classList },
    createElement() { return { id: "", textContent: "" }; },
    getElementById(id) { return elements.get(id) || null; },
    addEventListener(name, listener) { add(documentListeners, name, listener); },
    removeEventListener(name, listener) { remove(documentListeners, name, listener); },
    dispatchEvent() {},
  };
  const localApi = {
    contract: "book-host/1",
    mode,
    file: "books/test",
    title: "Book",
    capabilities: { selection: true, context: true, highlight: true },
    selection: () => ({ text: "selected", context: "sentence" }),
    context: () => ({ file: "books/test" }),
    currentLocation: () => ({ unit: "page", index: 0, total: 3 }),
    localAction: async (name, payload) => {
      localActions.push({ name, payload });
      return { name };
    },
  };
  const sandbox = {
    console,
    document,
    location: { origin: "https://reader.example", href: "https://reader.example/pdf/view" },
    Date: { now: () => now },
    CustomEvent: class {
      constructor(type, options) { this.type = type; this.detail = options?.detail; }
    },
    setTimeout(fn) { fn(); return 1; },
    clearTimeout() {},
    setInterval(fn) {
      const id = ++intervalSeq;
      intervals.set(id, fn);
      return id;
    },
    clearInterval(id) { intervals.delete(id); },
    __bwReaderLocalApi: localApi,
    RC: {
      adapter: () => adapter,
      sidedrawer: { close() { closed += 1; } },
    },
    addEventListener(name, listener) { add(windowListeners, name, listener); },
    postMessage(message, targetOrigin) {
      posts.push({ message: structuredClone(message), targetOrigin });
    },
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  const context = vm.createContext(sandbox);
  vm.runInContext(PAGE_BRIDGE, context, { filename: "pwa-extension-bridge.js" });
  const windowRef = vm.runInContext("window", context);

  function send(type, payload = null, id = type.toLowerCase()) {
    const event = {
      source: windowRef,
      origin: sandbox.location.origin,
      data: {
        protocol: "bw-reader-pwa/1",
        direction: "to-page",
        type,
        payload,
        id,
      },
    };
    for (const listener of windowListeners.get("message") || []) listener(event);
  }
  const flush = () => new Promise((resolve) => setImmediate(resolve));
  return {
    adapter,
    root,
    posts,
    intervals,
    localActions,
    originalLookup,
    get closed() { return closed; },
    get originalLookups() { return originalLookups; },
    advance(ms) {
      now += ms;
      for (const callback of [...intervals.values()]) callback();
    },
    send,
    flush,
  };
}

test("HELLO 不隐藏 PWA；TAKEOVER 成功后才切换，GOODBYE 恢复", async () => {
  const h = bridgeHarness("pdf");
  h.send("HELLO", { version: "test", uiOwner: "extension" }, "hello-1");
  await h.flush();
  assert.equal(h.root.dataset.bwReaderExtensionActive, undefined);
  assert.equal(h.adapter.lookupWord, h.originalLookup);
  assert.ok(h.posts.some(({ message }) => message.type === "READY" && message.id === "hello-1"));

  h.send("TAKEOVER", { version: "test", uiOwner: "extension" }, "takeover-1");
  await h.flush();
  assert.equal(h.root.dataset.bwReaderExtensionActive, "1");
  assert.equal(h.root.dataset.bwReaderUiOwner, "extension");
  assert.notEqual(h.adapter.lookupWord, h.originalLookup);
  assert.equal(h.closed, 1);
  assert.ok(h.posts.some(({ message }) =>
    message.type === "RESULT" && message.id === "takeover-1" && message.payload.ok,
  ));
  assert.ok(h.posts.some(({ message }) =>
    message.type === "LOCATION" &&
    message.payload?.unit === "page" &&
    message.payload?.total === 3,
  ));

  h.send("GET_CONTEXT", null, "context-1");
  await h.flush();
  const contextResult = h.posts.find(({ message }) =>
    message.type === "RESULT" && message.id === "context-1",
  )?.message.payload.result;
  assert.deepEqual(contextResult.current_location, {
    unit: "page", index: 0, total: 3,
  });
  assert.deepEqual(contextResult.currentLocation, contextResult.current_location);

  h.send("LOCAL_ACTION", {
    action: "highlight",
    payload: { color: "#f4c542" },
  }, "action-1");
  await h.flush();
  assert.deepEqual(h.localActions, [{
    name: "highlight",
    payload: { color: "#f4c542" },
  }]);

  h.send("GOODBYE", null, "bye-1");
  await h.flush();
  assert.equal(h.root.dataset.bwReaderExtensionActive, undefined);
  assert.equal(h.root.dataset.bwReaderUiOwner, "pwa");
  assert.equal(h.adapter.lookupWord, h.originalLookup);
  h.adapter.lookupWord();
  assert.equal(h.originalLookups, 1);
});

test("TAKEOVER 的 15 秒租约超时会自动归还 PWA", async () => {
  const h = bridgeHarness("epub");
  h.send("TAKEOVER", null, "takeover-lease");
  await h.flush();
  assert.equal(h.root.dataset.bwReaderExtensionActive, "1");
  h.advance(15001);
  assert.equal(h.root.dataset.bwReaderExtensionActive, undefined);
  assert.equal(h.root.dataset.bwReaderUiOwner, "pwa");
  assert.equal(h.adapter.lookupWord, h.originalLookup);
});

test("LOCAL_ACTION 只广播真实副作用，anchor_fx 不触发选区或位置查询", async () => {
  const h = bridgeHarness("pdf");
  h.send("TAKEOVER", null, "takeover-effects");
  await h.flush();
  h.posts.length = 0;
  h.localActions.length = 0;

  h.send("LOCAL_ACTION", {
    action: "anchor_fx",
    payload: { show: true, x: 123, y: 234 },
  }, "anchor-fx-1");
  await h.flush();
  assert.deepEqual(h.localActions, [{
    name: "anchor_fx",
    payload: { show: true, x: 123, y: 234 },
  }]);
  assert.ok(h.posts.some(({ message }) =>
    message.type === "RESULT" &&
    message.id === "anchor-fx-1" &&
    message.payload.ok,
  ));
  assert.equal(h.posts.filter(({ message }) =>
    message.type === "SELECTION" || message.type === "LOCATION"
  ).length, 0);

  h.posts.length = 0;
  h.localActions.length = 0;
  h.send("LOCAL_ACTION", {
    action: "highlight",
    payload: { color: "#f4c542" },
  }, "highlight-effect");
  await h.flush();
  assert.deepEqual(h.localActions, [{
    name: "highlight",
    payload: { color: "#f4c542" },
  }]);
  assert.equal(h.posts.filter(({ message }) => message.type === "SELECTION").length, 1);
  assert.equal(h.posts.filter(({ message }) => message.type === "LOCATION").length, 0);

  h.posts.length = 0;
  h.localActions.length = 0;
  h.send("LOCAL_ACTION", {
    action: "jump_page",
    payload: { page: 2 },
  }, "jump-effect");
  await h.flush();
  assert.deepEqual(h.localActions, [{
    name: "jump_page",
    payload: { page: 2 },
  }]);
  assert.equal(h.posts.filter(({ message }) => message.type === "SELECTION").length, 0);
  assert.equal(h.posts.filter(({ message }) => message.type === "LOCATION").length, 1);
});

test("页面桥拒绝 web mode，三个书籍模板都装载同一宿主与接管桥", async () => {
  const h = bridgeHarness("web");
  h.send("HELLO", null, "web-hello");
  await h.flush();
  assert.equal(h.root.dataset.bwReaderExtensionActive, undefined);
  assert.ok(h.posts.some(({ message }) =>
    message.type === "RESULT" && message.id === "web-hello" && !message.payload.ok,
  ));

  for (const template of TEMPLATES) {
    assert.match(template, /\/static\/reader-runtime\/book-host\.js/);
    assert.match(template, /\/static\/pdf\/pwa-extension-bridge\.js/);
  }
  assert.match(PDF_HOST, /BWReaderBookHost\.register\(\{[\s\S]*?mode: 'pdf'/);
  assert.match(EPUB_HOST, /mode: _bookMode/);
  assert.match(HTML_HOST, /mode: 'html'/);
  assert.doesNotMatch(PAGE_BRIDGE, /mode\s*===\s*['"]web['"]/);
});
