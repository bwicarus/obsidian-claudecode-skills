import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const ROOT = new URL("../../", import.meta.url);
const SOURCE = readFileSync(
  new URL("_server_deploy/static/pdf/rc-stickynote.js", ROOT),
  "utf8",
);
const WEB_NOTES_SOURCE = readFileSync(
  new URL("extensions/bw-reader-webext/src/web-notes.js", ROOT),
  "utf8",
);

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));
const deferred = () => {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
};

class FakeClassList {
  constructor() {
    this.values = new Set();
  }
  add(...values) {
    values.forEach((value) => this.values.add(value));
  }
  remove(...values) {
    values.forEach((value) => this.values.delete(value));
  }
  contains(value) {
    return this.values.has(value);
  }
  toggle(value, force) {
    const enabled = force === undefined ? !this.values.has(value) : !!force;
    if (enabled) this.values.add(value);
    else this.values.delete(value);
    return enabled;
  }
}

class FakeElement {
  constructor(tagName = "div") {
    this.tagName = tagName.toUpperCase();
    this.style = {};
    this.dataset = {};
    this.classList = new FakeClassList();
    this.children = [];
    this.parentElement = null;
    this.listeners = new Map();
    this.queries = new Map();
    this.attributes = new Map();
    this.value = "";
    this.innerHTML = "";
    this.textContent = "";
    this.width = 0;
    this.height = 0;
    this.clientWidth = 1000;
    this.clientHeight = 800;
    this.scrollLeft = 0;
    this.scrollTop = 0;
    this.clientLeft = 0;
    this.clientTop = 0;
    this._connected = false;
  }
  get isConnected() {
    if (this._connected) return true;
    return !!this.parentElement?.isConnected;
  }
  appendChild(child) {
    if (child.parentElement) {
      const index = child.parentElement.children.indexOf(child);
      if (index >= 0) child.parentElement.children.splice(index, 1);
    }
    child.parentElement = this;
    this.children.push(child);
    return child;
  }
  remove() {
    if (!this.parentElement) return;
    const index = this.parentElement.children.indexOf(this);
    if (index >= 0) this.parentElement.children.splice(index, 1);
    this.parentElement = null;
  }
  addEventListener(type, listener) {
    const listeners = this.listeners.get(type) || [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }
  setAttribute(name, value) {
    this.attributes.set(name, String(value));
  }
  dispatch(type, extra = {}) {
    const event = {
      target: this,
      currentTarget: this,
      stopPropagation() {},
      stopImmediatePropagation() {},
      preventDefault() {},
      ...extra,
    };
    for (const listener of this.listeners.get(type) || []) listener(event);
  }
  click() {
    this.dispatch("click");
  }
  querySelector(selector) {
    if (this.queries.has(selector)) return this.queries.get(selector);
    const tag = selector.includes("textarea") || selector === ".rc-note-text"
      ? "textarea"
      : selector.includes("canvas") || selector === ".rc-note-ink"
        ? "canvas"
        : selector.includes("button") || selector === ".rc-note-del" ||
            selector === ".rc-note-anchor"
          ? "button"
          : "div";
    const child = new FakeElement(tag);
    this.queries.set(selector, child);
    this.appendChild(child);
    return child;
  }
  querySelectorAll() {
    return [];
  }
  closest() {
    return null;
  }
  getBoundingClientRect() {
    return {
      left: 0,
      top: 0,
      right: this.clientWidth,
      bottom: this.clientHeight,
      width: this.clientWidth,
      height: this.clientHeight,
    };
  }
  getContext() {
    return {
      setTransform() {},
      clearRect() {},
      save() {},
      translate() {},
      restore() {},
    };
  }
}

function anchor(documentId, selector = "#article") {
  return {
    documentId,
    kind: "web-dom",
    revision: 1,
    data: { kind: "web", selector, rx: 0.2, ry: 0.3 },
  };
}

function note(documentId, id, rev, text, extra = {}) {
  return {
    id,
    noteId: id,
    documentId,
    rev,
    deleted: false,
    anchor: anchor(documentId),
    text,
    color: "#ffffff",
    w: 260,
    h: 180,
    collapsed: false,
    strokes: [],
    ...structuredClone(extra),
  };
}

function loadStickynote({
  repository,
  mount = null,
  fetchCalls = [],
  localStorageValues = {},
  noteWordRect = null,
  selectionController = null,
  pageBindCard = null,
  fetchImpl = null,
  file = null,
  source = SOURCE,
}) {
  const head = new FakeElement("head");
  head._connected = true;
  const body = new FakeElement("body");
  body._connected = true;
  const documentElement = new FakeElement("html");
  documentElement._connected = true;
  const document = {
    head,
    body,
    documentElement,
    scrollingElement: documentElement,
    activeElement: null,
    visibilityState: "visible",
    createElement(tag) {
      return new FakeElement(tag);
    },
    addEventListener() {},
    removeEventListener() {},
  };
  const sandbox = {
    console,
    document,
    localStorage: { getItem(key) { return localStorageValues[key] ?? null; } },
    Promise,
    setTimeout,
    clearTimeout,
    requestAnimationFrame(callback) {
      return setTimeout(callback, 0);
    },
    cancelAnimationFrame: clearTimeout,
    innerWidth: 1200,
    innerHeight: 800,
    devicePixelRatio: 1,
    crypto: globalThis.crypto,
    confirm: () => true,
    alert() {},
    addEventListener() {},
    removeEventListener() {},
    getComputedStyle() {
      return { position: "relative", overflow: "visible", overflowY: "visible" };
    },
    fetch(url, init) {
      fetchCalls.push({ url: String(url), init });
      if (fetchImpl) return fetchImpl(String(url), init || {});
      return Promise.reject(new Error("repository 模式不应 fetch"));
    },
    RC: {},
    RCInk: {
      drawStroke() {},
      hit() { return false; },
    },
  };
  if (selectionController) sandbox.__bwSelectionController = selectionController;
  if (pageBindCard) sandbox.__pageBindCard = pageBindCard;
  sandbox.window = sandbox;
  vm.runInContext(source, vm.createContext(sandbox), {
    filename: "rc-stickynote.js",
  });
  const documentId = "web:https://example.test/article";
  const container = new FakeElement("section");
  container._connected = true;
  sandbox.RC.stickynote.init({
    documentId,
    ...(file ? { file } : {}),
    repository,
    disablePortal: true,
    mount: mount || (() => ({ el: container, left: 20, top: 30 })),
    anchorFromPoint: () => anchor(documentId),
    noteWordRect: noteWordRect || undefined,
    toast() {},
  });
  return { sandbox, documentId, container, fetchCalls };
}

test("native Pencil tool/style sync is atomic, updates mounted note UI, and keeps regions on the page", async () => {
  const id = `c_${"a".repeat(32)}`;
  const repository = {
    newId: () => `c_${"b".repeat(32)}`,
    list: async () => [note("web:https://example.test/article", id, 1, "ink")],
    get: async () => null,
    create: async () => null,
    patch: async () => null,
    remove: async () => null,
    subscribe: () => () => {},
  };
  const { sandbox, container } = loadStickynote({
    repository,
  });
  await tick();
  await tick();

  const sticky = sandbox.RC.stickynote;
  const root = container.children.find((child) => child.dataset.noteId === id);
  const toolButton = root.querySelector(".rc-note-tools").querySelector(".rc-note-tool");

  assert.equal(sticky.synchronizeInkToolStyle("eraser", "#12ABef", 7.5), true);
  assert.equal(toolButton.classList.contains("on"), true);
  assert.equal(toolButton.textContent, "🧹");

  assert.equal(sticky.synchronizeInkToolStyle("pen", "red", 7.5), false);
  assert.equal(sticky.synchronizeInkToolStyle("pen", "#123456", "7.5"), false);
  assert.equal(toolButton.classList.contains("on"), true, "invalid calls leave the prior tool intact");

  assert.equal(sticky.synchronizeInkToolStyle("region", "#123456", 3), true);
  assert.equal(sticky.penRoute(100, 100), null, "page region tool must bypass sticky-note ink");
  assert.equal(sticky.penBegin({ clientX: 100, clientY: 100 }, {}), false);
  assert.equal(toolButton.disabled, true);
  assert.match(toolButton.title, /选区笔只作用于书页/);

  assert.equal(sticky.synchronizeInkToolStyle("pen", "#12ABef", 7.5), true);
  assert.equal(sticky.penRoute(100, 100), id);
  assert.equal(toolButton.disabled, false);
  assert.equal(toolButton.classList.contains("on"), false);
  assert.equal(sticky.penBegin({ clientX: 100, clientY: 100 }, {}), true);
  assert.equal(sticky.notes()[0].strokes[0].c, "#12abef");
  assert.equal(sticky.notes()[0].strokes[0].w, 7.5);
});

test("repository LIST/CHANGE/RESULT 按 noteId+rev 合并，旧 LIST 不覆盖先到 CHANGE", async () => {
  const list = deferred();
  let subscriber = null;
  const repository = {
    newId: () => `c_${"b".repeat(32)}`,
    list: () => list.promise,
    get: async () => null,
    create: async () => null,
    patch: async () => null,
    remove: async () => null,
    subscribe(callback) {
      subscriber = callback;
      return () => { subscriber = null; };
    },
  };
  const { sandbox, documentId, container, fetchCalls } = loadStickynote({
    repository,
  });
  const id = `c_${"a".repeat(32)}`;
  subscriber({
    data: {
      operation: "put",
      note: note(documentId, id, 2, "CHANGE 先到"),
    },
  });
  const root = container.children.find((child) => child.dataset.noteId === id);
  list.resolve([note(documentId, id, 1, "旧 LIST")]);
  await tick();
  await tick();

  assert.equal(sandbox.RC.stickynote.notes().length, 1);
  assert.equal(sandbox.RC.stickynote.notes()[0].text, "CHANGE 先到");
  assert.equal(
    container.children.find((child) => child.dataset.noteId === id),
    root,
    "相同/旧 revision 不重建便签 DOM",
  );
  assert.equal(fetchCalls.length, 0);
});

test("repository patch 逐 note 串行并带最新 ifRev，CHANGE 先于 RESULT 不重复重建；删除也只走 repository", async () => {
  const id = `c_${"a".repeat(32)}`;
  let subscriber = null;
  const patchCalls = [];
  const removeCalls = [];
  const patchDeferred = [deferred(), deferred()];
  const repository = {
    newId: () => `c_${"b".repeat(32)}`,
    list: async () => [note("web:https://example.test/article", id, 1, "v1")],
    get: async () => null,
    create: async () => null,
    patch(noteId, fields, options) {
      const index = patchCalls.length;
      patchCalls.push({ noteId, fields: structuredClone(fields), ...options });
      return patchDeferred[index].promise;
    },
    remove(noteId, options) {
      removeCalls.push({ noteId, ...options });
      const tombstone = {
        ...note("web:https://example.test/article", id, 4, "v3"),
        deleted: true,
      };
      subscriber({ operation: "remove", note: tombstone });
      return Promise.resolve(tombstone);
    },
    subscribe(callback) {
      subscriber = callback;
      return () => { subscriber = null; };
    },
  };
  const { sandbox, container, documentId, fetchCalls } = loadStickynote({
    repository,
  });
  await tick();
  await tick();

  const root = container.children.find((child) => child.dataset.noteId === id);
  const textarea = root.querySelector(".rc-note-text");
  textarea.value = "v2";
  textarea.dispatch("blur");
  textarea.value = "v3";
  textarea.dispatch("blur");
  await tick();
  assert.equal(patchCalls.length, 1, "第二个 patch 必须等待第一个完成");
  assert.equal(patchCalls[0].ifRev, 1);
  assert.match(patchCalls[0].mutationId, /^rc-note:patch:/);

  const changed = note(documentId, id, 2, "v2", {
    strokes: [{ c: "#f00", w: 2, pts: [[0, 0], [1, 1]] }],
  });
  subscriber({ operation: "put", note: changed });
  patchDeferred[0].resolve(structuredClone(changed));
  await tick();
  await tick();
  assert.equal(patchCalls.length, 2);
  assert.equal(patchCalls[1].ifRev, 2, "队列后项使用 CHANGE/RESULT 后的当前 rev");
  assert.notEqual(patchCalls[1].mutationId, patchCalls[0].mutationId);
  assert.equal(
    container.children.find((child) => child.dataset.noteId === id),
    root,
    "CHANGE 与同 revision RESULT 不能重复插入/重建",
  );

  const final = note(documentId, id, 3, "v3");
  patchDeferred[1].resolve(final);
  await tick();
  await tick();
  root.querySelector(".rc-note-del").dispatch("click");
  await tick();
  await tick();
  assert.equal(removeCalls.length, 1);
  assert.equal(removeCalls[0].ifRev, 3);
  assert.match(removeCalls[0].mutationId, /^rc-note:remove:/);
  assert.equal(sandbox.RC.stickynote.notes().length, 0);
  assert.equal(fetchCalls.length, 0);
});

test("四种 create 都先取 repository ID；card 的 cid/gid 原样保存且 CHANGE/RESULT 不重复", async () => {
  const ids = ["b", "c", "d", "e"].map((value) => `c_${value.repeat(32)}`);
  let subscriber = null;
  const creates = [];
  const repository = {
    newId: () => ids[creates.length],
    list: async () => [],
    get: async () => null,
    create(input, options) {
      creates.push({ input: structuredClone(input), options });
      const created = {
        ...structuredClone(input),
        id: input.noteId,
        noteId: input.noteId,
        rev: 1,
        deleted: false,
      };
      subscriber({ operation: "put", mutationId: options.mutationId, note: created });
      return Promise.resolve(structuredClone(created));
    },
    patch: async () => null,
    remove: async () => null,
    subscribe(callback) {
      subscriber = callback;
      return () => { subscriber = null; };
    },
  };
  const { sandbox, documentId, fetchCalls } = loadStickynote({ repository });
  await tick();

  const rc = sandbox.RC.stickynote;
  rc.createAt(anchor(documentId, "#plain"));
  await tick();
  rc.createVideoAt(10, 10, "abcdefghijk", { src: "yt" });
  await tick();
  rc.createCardAt(10, 10, [{ q: "Q", a: "A" }], "card_keep_42");
  await tick();
  rc.createHtmlAt(10, 10, {
    content: "<b>tool</b>",
    isHtml: true,
    cid: "html_keep_42",
  });
  await tick();
  await tick();

  assert.equal(creates.length, 4);
  assert.deepEqual(
    creates.map(({ input }) => input.noteId),
    ["b", "c", "d", "e"].map((value) => `c_${value.repeat(32)}`),
  );
  assert.ok(creates.every(({ input }) => input.documentId === documentId));
  assert.ok(creates.every(({ input }) => input.anchor.documentId === documentId));
  assert.ok(creates.every(({ options }) => /^rc-note:create:/.test(options.mutationId)));
  assert.equal(creates[2].input.card.gid, "card_keep_42");
  assert.equal(creates[2].input.card.cid, "card_keep_42");
  assert.equal(creates[3].input.html.cid, "html_keep_42");
  assert.equal(rc.notes().length, 4, "CHANGE+RESULT 对每个 create 只保留一张");
  assert.equal(fetchCalls.length, 0);
});

test("placement storage 接受十万字符并显式拒绝异常超大的页面卡片", async () => {
  let nextId = 0;
  const creates = [];
  const repository = {
    newId: () => `c_${String(++nextId).padStart(32, "0")}`,
    list: async () => [],
    get: async () => null,
    create(input) {
      creates.push(structuredClone(input));
      return Promise.resolve({
        ...structuredClone(input),
        id: input.noteId,
        noteId: input.noteId,
        rev: 1,
        deleted: false,
      });
    },
    patch: async () => null,
    remove: async () => null,
    subscribe: () => () => {},
  };
  const { sandbox } = loadStickynote({ repository });
  await tick();
  const rc = sandbox.RC.stickynote;

  assert.equal(rc.createHtmlAt(10, 10, {
    content: "甲".repeat(100000),
    contextText: "完整正文",
    isHtml: false,
  }), true);
  await tick();
  assert.equal(creates[0].html.content.length, 100000);

  assert.equal(rc.createHtmlAt(10, 10, {
    content: "甲".repeat(100001),
    contextText: "完整正文",
    isHtml: false,
  }), false);
  assert.equal(rc.createCardAt(10, 10, [{
    type: "basic", front: "问".repeat(100001), back: "答",
  }], "oversized-learning"), false);

  const bind = { kind: "page-chars", page: 3, from: 1, to: 2, text: "词" };
  const oversizedContent = await rc.persistBoundCard(
    bind, { raw: "文".repeat(100001) }, { x: 10, y: 10 },
  );
  assert.equal(oversizedContent.ok, false);
  assert.equal(oversizedContent.why, "card-too-large");
  const oversizedContext = await rc.persistBoundCard(bind, {
      raw: "正文", contextText: "文".repeat(100001),
    }, { x: 10, y: 10 });
  assert.equal(oversizedContext.ok, false);
  assert.equal(oversizedContext.why, "card-context-too-large");
  assert.equal(creates.length, 1, "异常输入不能进入 repository");
});

test("手动拖入学习卡与 HTML 卡即使靠近文字也保持自由卡片", async () => {
  const ids = ["f", "0"].map((value) => `c_${value.repeat(32)}`);
  const creates = [];
  const repository = {
    newId: () => ids[creates.length],
    list: async () => [],
    get: async () => null,
    create(input, options) {
      creates.push({ input: structuredClone(input), options });
      return Promise.resolve({
        ...structuredClone(input),
        id: input.noteId,
        noteId: input.noteId,
        rev: 1,
        deleted: false,
      });
    },
    patch: async () => null,
    remove: async () => null,
    subscribe: () => () => {},
  };
  const words = [
    { page: 7, from: 10, to: 11, text: "学习词", dist: 3 },
    { page: 7, from: 20, to: 21, text: "工具词", dist: 4 },
  ];
  const { sandbox } = loadStickynote({
    repository,
    noteWordRect: () => words.shift(),
  });
  await tick();

  sandbox.RC.stickynote.createCardAt(
    10,
    10,
    [{ question: "渲染器题目", answer: "渲染器答案" }],
    "manual-learning-card",
  );
  await tick();
  sandbox.RC.stickynote.createHtmlAt(20, 20, {
    content: "<b>工具卡正文</b>",
    isHtml: true,
    label: "工具卡",
    cid: "manual-html-card",
  });
  await tick();

  assert.equal(creates.length, 2);
  assert.equal(Object.hasOwn(creates[0].input.card, "bind"), false);
  assert.equal(Object.hasOwn(creates[1].input.html, "bind"), false);
  assert.equal(words.length, 2,
    "普通投放不能偷偷调用分词吸附；只有显式 ⚓️ 或 AI 自动锚定才写 bind");
  assert.equal(
    sandbox.RC.stickynote.cardContextText(creates[0].input.card.cards),
    "卡片 1\n正面：渲染器题目\n背面：渲染器答案",
  );
});

test("自由学习卡与 HTML 卡普通拖动不会升级为 page-chars", async () => {
  const instrumented = SOURCE.replace(
    "  // ── 词锚便签：正文里显示成「词描边 + 右上角序号」，点词才展开真卡 ──────",
    "  window.__testRebindWord = _rebindWord;\n\n" +
      "  // ── 词锚便签：正文里显示成「词描边 + 右上角序号」，点词才展开真卡 ──────",
  );
  assert.notEqual(instrumented, SOURCE, "test hook insertion point must stay attached to _rebindWord");
  const words = [
    { page: 7, from: 30, to: 31, text: "补锚词", dist: 2 },
    { page: 7, from: 40, to: 41, text: "补工具词", dist: 2 },
  ];
  const repository = {
    newId: async () => `c_${"1".repeat(32)}`,
    list: async () => [],
    get: async () => null,
    create: async () => null,
    patch: async () => null,
    remove: async () => null,
    subscribe: () => () => {},
  };
  const { sandbox, documentId } = loadStickynote({
    repository,
    noteWordRect: () => words.shift(),
    source: instrumented,
  });
  await tick();

  const learning = note(documentId, `c_${"2".repeat(32)}`, 1, "", {
    card: { gid: "legacy-learning", cid: "legacy-learning", cards: [] },
  });
  const html = note(documentId, `c_${"3".repeat(32)}`, 1, "", {
    html: { cid: "legacy-html", content: "<b>旧工具卡</b>" },
  });
  const learningCtl = { note: learning, root: new FakeElement("div") };
  const htmlCtl = { note: html, root: new FakeElement("div") };

  assert.equal(sandbox.__testRebindWord(learningCtl, 10, 10), false);
  assert.equal(Object.hasOwn(learning.card, "bind"), false);
  assert.equal(sandbox.__testRebindWord(htmlCtl, 20, 20), false);
  assert.equal(Object.hasOwn(html.html, "bind"), false);
  assert.equal(words.length, 2, "自由卡普通拖动不应调用词探测");
});

test("展开自由卡点击 ⚓️ 才持久化精确选区并立即切成正文标记", async () => {
  const documentId = "web:https://example.test/article";
  const id = `c_${"4".repeat(32)}`;
  const initial = note(documentId, id, 1, "", {
    anchor: { kind: "pdf", page: 7, x: 0.25, y: 0.4 },
    card: {
      gid: "free-learning", cid: "free-learning", form: "full",
      cards: [{ question: "自由题目", answer: "自由答案" }],
    },
  });
  const patches = [];
  const markerCalls = [];
  const repository = {
    newId: async () => `c_${"5".repeat(32)}`,
    list: async () => [structuredClone(initial)],
    get: async () => null,
    patch(noteId, fields, options) {
      patches.push({ noteId, fields: structuredClone(fields), options });
      return Promise.resolve({
        ...structuredClone(initial),
        ...structuredClone(fields),
        id, noteId: id, rev: 2,
      });
    },
    create: async () => null,
    remove: async () => null,
    subscribe: () => () => {},
  };
  const { sandbox, container } = loadStickynote({
    repository,
    selectionController: {
      current: () => ({
        text: "锁定元素",
        anchor: {
          kind: "pdf-char", page: 7, startIdx: 12, endIdx: 15,
        },
      }),
    },
    pageBindCard(bind, payload) {
      markerCalls.push({ bind: structuredClone(bind), payload });
      return { ok: true, key: "p7b12_15" };
    },
  });
  await tick();
  await tick();

  const root = container.children.find((child) => child.dataset.noteId === id);
  const button = root.querySelector(".rc-note-anchor");
  assert.equal(root.classList.contains("rc-note-free-card-open"), true);
  assert.equal(button.attributes.get("aria-hidden"), "false");
  button.dispatch("click");
  await tick();
  await tick();

  assert.equal(patches.length, 1);
  assert.deepEqual(patches[0].fields.card.bind, {
    kind: "page-chars", page: 7, from: 12, to: 15, text: "锁定元素",
  });
  assert.deepEqual(structuredClone(sandbox.RC.stickynote.notes()[0].card.bind), {
    kind: "page-chars", page: 7, from: 12, to: 15, text: "锁定元素",
  });
  assert.equal(markerCalls.length, 1,
    "repository commit 后才允许画正文框和右侧标记");
  assert.equal(root.style.display, "none");
  assert.equal(root.classList.contains("rc-note-free-card-open"), false);
});

test("PDF legacy PATCH 成功后同一会话立即从自由卡切成正文词锚", async () => {
  const documentId = "web:https://example.test/article";
  const id = `c_${"6".repeat(32)}`;
  const initial = note(documentId, id, 0, "", {
    anchor: { kind: "pdf", page: 7, x: 0.25, y: 0.4 },
    card: {
      gid: "legacy-free", cid: "legacy-free", form: "full",
      cards: [{ question: "旧题目", answer: "旧答案" }],
    },
  });
  const markerCalls = [];
  const patchBodies = [];
  const { sandbox, container } = loadStickynote({
    file: "localbook:localbook-" + "b".repeat(64),
    selectionController: {
      current: () => ({
        text: "锁定元素",
        anchor: { kind: "pdf-char", page: 7, startIdx: 22, endIdx: 24 },
      }),
    },
    pageBindCard(bind, payload) {
      markerCalls.push({ bind: structuredClone(bind), payload });
      return { ok: true, key: "p7b22_24" };
    },
    fetchImpl: async (_url, init) => {
      const method = String(init.method || "GET").toUpperCase();
      if (method === "GET") {
        return { ok: true, json: async () => ({ ok: true, notes: [structuredClone(initial)] }) };
      }
      assert.equal(method, "PATCH");
      const body = JSON.parse(init.body);
      patchBodies.push(body);
      const saved = structuredClone(initial);
      saved.card = structuredClone(body.card);
      return { ok: true, json: async () => ({ ok: true, note: saved }) };
    },
  });
  await tick();
  await tick();

  const root = container.children.find((child) => child.dataset.noteId === id);
  root.querySelector(".rc-note-anchor").dispatch("click");
  await tick();
  await tick();

  assert.equal(patchBodies.length, 1);
  assert.deepEqual(structuredClone(sandbox.RC.stickynote.notes()[0].card.bind), {
    kind: "page-chars", page: 7, from: 22, to: 24, text: "锁定元素",
  });
  assert.equal(markerCalls.length, 1);
  assert.equal(root.style.display, "none");
  assert.equal(root.classList.contains("rc-note-free-card-open"), false);
});

test("AI page-chars 只在 repository create 与本地投影完成后报告持久化成功", async () => {
  const id = `c_${"9".repeat(32)}`;
  const gate = deferred();
  const creates = [];
  const repository = {
    newId: () => id,
    list: async () => [],
    get: async () => null,
    create(input, options) {
      creates.push({ input: structuredClone(input), options });
      return gate.promise;
    },
    patch: async () => null,
    remove: async () => null,
    subscribe: () => () => {},
  };
  const { sandbox, documentId, fetchCalls } = loadStickynote({ repository });
  await tick();

  const bind = { kind: "page-chars", page: 3, from: 12, to: 15, text: "示例" };
  const payload = {
    uid: "ai-card-42",
    raw: "<figure>配图卡</figure>",
    text: "配图卡",
    isHtml: true,
    label: "配图",
    category: "image",
    icon: "▧",
  };
  const first = sandbox.RC.stickynote.persistBoundCard(bind, payload, { x: 100, y: 120 });
  const retry = sandbox.RC.stickynote.persistBoundCard(bind, payload, { x: 100, y: 120 });
  assert.equal(typeof first?.then, "function", "公开入口必须返回 Promise 状态");
  let settled = false;
  first.then(() => { settled = true; });
  await tick();
  assert.equal(settled, false, "仓库写入未完成前不能提前报告 bound");
  assert.equal(creates.length, 1, "同一 AI 卡重试不能重复创建持久记录");

  const created = {
    ...structuredClone(creates[0].input),
    id,
    noteId: id,
    rev: 1,
    deleted: false,
  };
  gate.resolve(created);
  const [firstResult, retryResult] = await Promise.all([first, retry]);
  assert.equal(firstResult.ok, true);
  assert.equal(firstResult.noteId, id);
  assert.equal(retryResult.ok, true);
  assert.equal(creates[0].input.documentId, documentId);
  assert.deepEqual(creates[0].input.html.bind, bind);
  assert.equal(creates[0].input.html.sourceUid, payload.uid);
  assert.equal(creates[0].input.html.category, "image");
  assert.equal(creates[0].input.html.type, "#34d399");
  assert.equal(creates[0].input.html.content, payload.raw);
  assert.equal(creates[0].input.html.contextText, payload.text);
  assert.equal(sandbox.RC.stickynote.notes().length, 1);

  const reused = await sandbox.RC.stickynote.persistBoundCard(bind, payload, { x: 100, y: 120 });
  assert.equal(reused.ok, true);
  assert.equal(reused.reused, true);
  assert.equal(creates.length, 1, "已持久化的相同 uid+词区间也不能再建一张");
  assert.equal(fetchCalls.length, 0);
});

test("AI page-chars 回执未知后以同一 noteId+mutationId 重放，不创建第二张卡", async () => {
  const id = `c_${"8".repeat(32)}`;
  let newIdCalls = 0;
  const creates = [];
  const repository = {
    newId() {
      newIdCalls += 1;
      return id;
    },
    list: async () => [],
    get: async () => null,
    create(input, options) {
      creates.push({ input: structuredClone(input), options: structuredClone(options) });
      if (creates.length === 1) {
        const error = new Error("create outcome unknown");
        error.code = "BW_DATA_OUTCOME_UNKNOWN";
        return Promise.reject(error);
      }
      return Promise.resolve({
        ...structuredClone(input),
        id: input.noteId,
        noteId: input.noteId,
        rev: 1,
        deleted: false,
      });
    },
    patch: async () => null,
    remove: async () => null,
    subscribe: () => () => {},
  };
  const { sandbox } = loadStickynote({ repository });
  await tick();

  const bind = { kind: "page-chars", page: 6, from: 20, to: 22, text: "事务" };
  const payload = {
    uid: "ai-stable-intent",
    raw: "<b>稳定重放</b>",
    isHtml: true,
    label: "文字",
    category: "text",
  };
  const first = await sandbox.RC.stickynote.persistBoundCard(bind, payload, { x: 50, y: 60 });
  assert.equal(first.ok, false);
  const second = await sandbox.RC.stickynote.persistBoundCard(bind, payload, { x: 55, y: 65 });
  assert.equal(second.ok, true);
  assert.equal(newIdCalls, 1, "结果未知后不能重新取 ID");
  assert.equal(creates.length, 2, "第二次只重放同一逻辑 create");
  assert.equal(creates[0].input.noteId, creates[1].input.noteId);
  assert.equal(creates[0].options.mutationId, creates[1].options.mutationId);
  assert.deepEqual(creates[0].input, creates[1].input,
    "重放必须连 anchor/payload 都保持不变，不能让同 mutationId 对应不同签名");
  assert.equal(sandbox.RC.stickynote.notes().length, 1);
});

test("repository remove 只有有效墓碑投影后才报告成功并撤 UI", async () => {
  const documentId = "web:https://example.test/article";
  const id = `c_${"7".repeat(32)}`;
  const repository = {
    newId: () => `c_${"6".repeat(32)}`,
    list: async () => [note(documentId, id, 2, "keep")],
    get: async () => null,
    create: async () => null,
    patch: async () => null,
    remove: async () => undefined,
    subscribe: () => () => {},
  };
  const { sandbox, container } = loadStickynote({ repository });
  await tick();
  await tick();
  const root = container.children.find((child) => child.dataset.noteId === id);
  root.querySelector(".rc-note-del").dispatch("click");
  await tick();
  await tick();
  assert.equal(sandbox.RC.stickynote.notes().length, 1,
    "resolved 但不是 tombstone 的响应不能删除本地记录");
  assert.equal(root.isConnected, true, "删除失败时原卡必须继续留在页面");
});

test("repository LIST 完整分页，并安全 reconcile 缺失项而保留分页途中 CHANGE", async () => {
  const documentId = "web:https://example.test/article";
  const all = Array.from({ length: 201 }, (_, index) => {
    const suffix = index.toString(16).padStart(32, "0");
    return note(documentId, `c_${suffix}`, 1, `note-${index}`);
  });
  let subscriber = null;
  const calls = [];
  let mode = "pages";
  let pending = null;
  const repository = {
    newId: () => `c_${"f".repeat(32)}`,
    list(query) {
      calls.push({ ...query });
      if (mode === "pending") return pending.promise;
      return Promise.resolve(all.slice(query.offset, query.offset + query.limit));
    },
    get: async () => null,
    create: async () => null,
    patch: async () => null,
    remove: async () => null,
    subscribe(callback) {
      subscriber = callback;
      return () => { subscriber = null; };
    },
  };
  const { sandbox, fetchCalls } = loadStickynote({
    repository,
    mount: () => null,
  });
  await tick();
  await tick();
  assert.deepEqual(calls.slice(0, 2).map((call) => call.offset), [0, 200]);
  assert.ok(calls.slice(0, 2).every((call) => call.limit === 200));
  assert.equal(sandbox.RC.stickynote.notes().length, 201);

  mode = "pending";
  pending = deferred();
  sandbox.RC.stickynote.loadAll();
  const duringId = `c_${"f".repeat(32)}`;
  subscriber({ operation: "put", note: note(documentId, duringId, 1, "during") });
  pending.resolve([]);
  await tick();
  await tick();
  assert.equal(
    sandbox.RC.stickynote.notes().length,
    1,
    "加载前快照中的 absent 项被清理，分页途中新 CHANGE 被保留",
  );
  assert.equal(sandbox.RC.stickynote.notes()[0].noteId, duringId);
  assert.equal(fetchCalls.length, 0);
});

test("web-notes 先 identity，再用完整 web-dom envelope 对称 mount/create，并接失效与 SPA teardown", () => {
  assert.match(WEB_NOTES_SOURCE, /await repository\.identity\(\)/);
  assert.match(WEB_NOTES_SOURCE, /kind:\s*'web-dom'/);
  assert.match(WEB_NOTES_SOURCE, /revision:\s*1/);
  assert.match(WEB_NOTES_SOURCE, /data:\s*rawAnchor/);
  assert.match(WEB_NOTES_SOURCE, /pins\.resolveAnchor\(rawAnchor\)/);
  assert.match(WEB_NOTES_SOURCE, /repository\.onInvalidate/);
  assert.match(WEB_NOTES_SOURCE, /RC\.stickynote\.removeAll\(\)/);
  assert.match(WEB_NOTES_SOURCE, /invalidateAndRefresh\(\)/);
  assert.match(WEB_NOTES_SOURCE, /if \(!currentIdentity && !\(await boundedRefresh\(\)\)\)/);
});
