import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ContextSelection = require(
  "../../_server_deploy/static/reader-runtime/context-selection-registry.js"
);

const SOURCE = fs.readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-review.js", import.meta.url),
  "utf8"
);
const BACKGROUND = fs.readFileSync(
  new URL("../../extensions/bw-reader-webext/background.js", import.meta.url),
  "utf8"
);

function response(value, { ok = true, status = 200 } = {}) {
  return { ok, status, json: async () => value };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((onResolve, onReject) => {
    resolve = onResolve;
    reject = onReject;
  });
  return { promise, resolve, reject };
}

async function flushPromises() {
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
}

async function settleAsync() {
  await flushPromises();
  await new Promise((resolve) => setImmediate(resolve));
  await flushPromises();
}

class FakeClassList {
  constructor(element) {
    this.element = element;
    this.values = new Set();
  }

  reset(value) {
    this.values = new Set(String(value || "").split(/\s+/).filter(Boolean));
  }

  sync() {
    this.element._className = [...this.values].join(" ");
  }

  add(...values) {
    values.forEach((value) => this.values.add(String(value)));
    this.sync();
  }

  remove(...values) {
    values.forEach((value) => this.values.delete(String(value)));
    this.sync();
  }

  contains(value) {
    return this.values.has(String(value));
  }

  toggle(value, force) {
    value = String(value);
    const on = force === undefined ? !this.values.has(value) : Boolean(force);
    if (on) this.values.add(value);
    else this.values.delete(value);
    this.sync();
    return on;
  }
}

class FakeElement {
  constructor(tagName, ownerDocument, nodeType = 1) {
    this.tagName = String(tagName || "div").toUpperCase();
    this.ownerDocument = ownerDocument;
    this.nodeType = nodeType;
    this.parentNode = null;
    this.children = [];
    this.attributes = Object.create(null);
    this.listeners = Object.create(null);
    this._id = "";
    this._className = "";
    this._text = "";
    this._rawHtml = "";
    this.classList = new FakeClassList(this);
    this.dataset = Object.create(null);
    this.hidden = false;
    this.disabled = false;
    this.style = Object.create(null);
  }

  set id(value) {
    this._id = String(value || "");
    if (this._id) this.ownerDocument.ids.set(this._id, this);
  }

  get id() {
    return this._id;
  }

  set className(value) {
    this._className = String(value || "");
    this.classList.reset(this._className);
  }

  get className() {
    return this._className;
  }

  set textContent(value) {
    this.children = [];
    this._rawHtml = "";
    this._text = String(value == null ? "" : value);
  }

  get textContent() {
    return this._text + this.children.map((child) => child.textContent).join("");
  }

  set innerHTML(value) {
    this.children = [];
    this._text = "";
    this._rawHtml = String(value == null ? "" : value);
  }

  get innerHTML() {
    if (this._rawHtml) return this._rawHtml;
    return this._text + this.children.map((child) => child.serialize()).join("");
  }

  get firstChild() {
    return this.children[0] || null;
  }

  get isConnected() {
    let node = this;
    while (node) {
      if (node === this.ownerDocument.head || node === this.ownerDocument.body) {
        return true;
      }
      node = node.parentNode;
    }
    return false;
  }

  appendChild(child) {
    if (child.parentNode) child.remove();
    this._rawHtml = "";
    child.parentNode = this;
    this.children.push(child);
    return child;
  }

  insertBefore(child, before) {
    if (child.parentNode) child.remove();
    this._rawHtml = "";
    child.parentNode = this;
    const index = before ? this.children.indexOf(before) : -1;
    if (index < 0) this.children.push(child);
    else this.children.splice(index, 0, child);
    return child;
  }

  remove() {
    if (!this.parentNode) return;
    const index = this.parentNode.children.indexOf(this);
    if (index >= 0) this.parentNode.children.splice(index, 1);
    this.parentNode = null;
  }

  setAttribute(name, value) {
    name = String(name);
    value = String(value);
    this.attributes[name] = value;
    if (name === "id") this.id = value;
    else if (name === "class") this.className = value;
    else if (name.startsWith("data-")) {
      const key = name.slice(5).replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
      this.dataset[key] = value;
    }
  }

  getAttribute(name) {
    if (name === "id") return this.id || null;
    if (name === "class") return this.className || null;
    return Object.hasOwn(this.attributes, name) ? this.attributes[name] : null;
  }

  removeAttribute(name) {
    delete this.attributes[name];
  }

  addEventListener(type, listener) {
    (this.listeners[type] ||= []).push(listener);
  }

  contains(candidate) {
    if (candidate === this) return true;
    return this.children.some((child) => child.contains(candidate));
  }

  descendants() {
    return this.children.flatMap((child) => [child, ...child.descendants()]);
  }

  querySelectorAll(selector) {
    if (selector === "*") return this.descendants();
    if (selector === ".asst-a") {
      return this.descendants().filter((node) => node.classList.contains("asst-a"));
    }
    // Queue rendering does not parse raw HTML. Sanitizer selectors therefore
    // correctly see no executable DOM nodes in this deliberately tiny harness.
    return [];
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }

  serialize() {
    if (this.nodeType === 3) return this._text;
    const attrs = [];
    if (this.id) attrs.push(`id="${this.id}"`);
    if (this.className) attrs.push(`class="${this.className}"`);
    for (const [name, value] of Object.entries(this.attributes)) {
      if (name === "id" || name === "class") continue;
      attrs.push(`${name}="${value}"`);
    }
    const attrText = attrs.length ? ` ${attrs.join(" ")}` : "";
    return `<${this.tagName.toLowerCase()}${attrText}>${this.innerHTML}</${this.tagName.toLowerCase()}>`;
  }
}

class FakeDocument {
  constructor() {
    this.ids = new Map();
    this.head = new FakeElement("head", this);
    this.body = new FakeElement("body", this);
  }

  createElement(tagName) {
    return new FakeElement(tagName, this);
  }

  createTextNode(value) {
    const node = new FakeElement("#text", this, 3);
    node.textContent = value;
    return node;
  }

  getElementById(id) {
    return this.ids.get(String(id)) || null;
  }

  querySelectorAll() {
    return [];
  }
}

function harness({
  context,
  stored = null,
  fetchImpl,
  openImpl = null,
  dispatchImpl = null,
  outboxImpl = null,
  cardRepository = null
}) {
  const document = new FakeDocument();
  const pane = document.createElement("section");
  pane.id = "side-pane-asst";
  const quick = document.createElement("div");
  quick.id = "asst-quick";
  const thread = document.createElement("div");
  thread.id = "asst-thread";
  pane.appendChild(quick);
  pane.appendChild(thread);
  document.body.appendChild(pane);
  const storageCalls = [];
  let extensionValue = stored;
  const extensionStore = {
    async get(key) {
      storageCalls.push(["get", key]);
      return extensionValue;
    },
    async set(key, value) {
      storageCalls.push(["set", key]);
      extensionValue = structuredClone(value);
      return true;
    }
  };
  const localCalls = [];
  const localStorage = {
    getItem(key) { localCalls.push(["get", key]); return null; },
    setItem(key, value) { localCalls.push(["set", key, value]); }
  };
  const toasts = [];
  const outboxCalls = [];
  const opened = [];
  const assigned = [];
  const events = [];
  const learningCardRenders = [];
  const pagerBindings = [];
  const RC = {
    adapter() {
      if (context instanceof Error) throw context;
      return {
        getContext: () => structuredClone(
          typeof context === "function" ? context() : (context || {})
        )
      };
    },
    esc(value) { return String(value || ""); },
    toast(value) { toasts.push(String(value || "")); },
    outbox: {
      send(...args) {
        outboxCalls.push(structuredClone(args));
        return typeof outboxImpl === "function"
          ? outboxImpl(...args)
          : true;
      }
    },
    assistant: {
      setMode() {}
    },
    flashcard: {
      bindPager(host, spec) {
        let index = Number(spec.index || 0);
        const binding = {
          host,
          spec,
          index() { return index; },
          goto(next, options = {}) {
            const previous = index;
            index = Math.max(
              0,
              Math.min(spec.slides.length - 1, Number(next || 0))
            );
            if (index !== previous && options.notify !== false) {
              spec.onChange?.(
                index,
                previous,
                options.reason || "goto",
                binding
              );
            }
            return index;
          },
          emit(next, reason = "scroll") {
            return this.goto(next, { scroll: false, reason });
          },
          destroy() {}
        };
        pagerBindings.push(binding);
        return binding;
      },
      renderEntity(host, spec) {
        const el = document.createElement("section");
        el.className = `vc-card vc-inflow ${spec.className || ""}`.trim();
        el.setAttribute("data-vc-cid", spec.gid);
        el.setAttribute("data-learning-card-id", spec.gid);
        const head = document.createElement("div");
        head.className = "vc-card-hd";
        head.textContent = spec.label || "学习卡片";
        const body = document.createElement("div");
        body.className = "vc-card-bd";
        const card = spec.card || spec.cards?.[0] || {};
        const frame = document.createElement("div");
        frame.className = "fc-card";
        const front = document.createElement("div");
        front.className = "fc-face";
        front.innerHTML = typeof spec.projectFaceHtml === "function"
          ? spec.projectFaceHtml(card, "front")
          : String(card.front || card.question || "");
        frame.appendChild(front);
        if (spec.mode === "review" && spec.showBack) {
          const back = document.createElement("div");
          back.className = "fc-back";
          back.innerHTML = typeof spec.projectFaceHtml === "function"
            ? spec.projectFaceHtml(card, "back")
            : String(card.back || card.answer || "");
          frame.appendChild(back);
          [
            ["1", "再来"],
            ["2", "困难"],
            ["3", "良好"],
            ["4", "简单"]
          ].forEach(([ease, label]) => {
            const button = document.createElement("button");
            button.setAttribute("data-action", "rate");
            button.setAttribute("data-ease", ease);
            button.textContent = label;
            frame.appendChild(button);
          });
        } else if (spec.mode === "review") {
          const button = document.createElement("button");
          button.setAttribute("data-action", "show-answer");
          button.textContent = "显示答案";
          frame.appendChild(button);
        } else {
          const back = document.createElement("div");
          back.className = "fc-back";
          back.textContent = String(card.back || card.answer || "");
          frame.appendChild(back);
        }
        body.appendChild(frame);
        el.appendChild(head);
        el.appendChild(body);
        host.appendChild(el);
        learningCardRenders.push({
          gid: spec.gid,
          mode: spec.mode,
          card: structuredClone(card)
        });
        return { el, bd: body, gid: spec.gid, cid: spec.gid };
      }
    }
  };
  const location = {
    href: "https://reader.example/book",
    assign(value) {
      const target = String(value || "");
      assigned.push(target);
      this.href = target;
    }
  };
  const window = {
    RC,
    BWReaderRuntime: {
      contextSelections: ContextSelection.createRegistry(),
      ...(cardRepository ? { cardRepository } : {})
    },
    __bwExtensionStore: extensionStore,
    location,
    open(...args) {
      opened.push(args.map((value) => String(value)));
      return typeof openImpl === "function" ? openImpl(...args) : {};
    },
    dispatchEvent(event) {
      events.push(event);
      if (typeof dispatchImpl === "function") dispatchImpl(event);
      return true;
    },
    confirm() { return true; }
  };
  const calls = [];
  const sandbox = {
    window,
    RC,
    document,
    localStorage,
    fetch: async (...args) => {
      calls.push(args);
      return fetchImpl(...args);
    },
    crypto: globalThis.crypto,
    setInterval() { return 1; },
    clearInterval() {},
    setTimeout() { return 1; },
    clearTimeout() {},
    MutationObserver: class {
      observe() {}
      disconnect() {}
    },
    CustomEvent: class {
      constructor(type, init) {
        this.type = type;
        this.detail = init?.detail;
      }
    },
    URL,
    console
  };
  vm.runInContext(SOURCE, vm.createContext(sandbox), {
    filename: "rc-review.js"
  });
  return {
    RC,
    get pane() {
      return document.getElementById("rc-review-body");
    },
    paneRoot: pane,
    quick,
    thread,
    document,
    calls,
    storageCalls,
    localCalls,
    toasts,
    outboxCalls,
    opened,
    assigned,
    getLocationHref: () => location.href,
    events,
    learningCardRenders,
    pagerBindings,
    currentPager() {
      return pagerBindings.at(-1) || null;
    },
    registry: window.BWReaderRuntime.contextSelections,
    getStored: () => extensionValue,
    findActions(action) {
      const workspace = document.getElementById("asst-review-workspace");
      return workspace.descendants().filter((node) =>
        node.getAttribute("data-action") === action
      );
    },
    clickButton(button) {
      const workspace = document.getElementById("asst-review-workspace");
      assert.ok(button, "missing delegated-action button");
      const listener = workspace.listeners.click?.[0];
      assert.ok(listener, "workspace click listener missing");
      listener({
        target: {
          closest() { return button; }
        }
      });
      return button;
    },
    clickAction(action, target = null) {
      const button = this.findActions(action).find((node) =>
        target === null || node.getAttribute("data-target") === target
      );
      assert.ok(button, `missing action ${action}/${target || "*"}`);
      return this.clickButton(button);
    },
    clickEase(ease) {
      const button = this.findActions("rate").find((node) =>
        node.getAttribute("data-ease") === String(ease)
      );
      assert.ok(button, `missing ease ${ease}`);
      return this.clickButton(button);
    }
  };
}

const candidatePayload = {
  ok: true,
  contract: "card-candidate-service/1",
  context_key: "server-context",
  due_total: 4,
  related_total: 1,
  cards: [{
    id: 10,
    question: "Q10",
    answer: "A10",
    deck: "Deck",
    candidate_reasons: ["当前内容来源"],
    was_due: false
  }]
};

test("uses private extension storage and sends context only in POST body", async () => {
  const h = harness({
    context: {
      file: "book.pdf",
      page: 2,
      selection: "private selected text",
      visible_text: "private visible text"
    },
    fetchImpl: async () => response(candidatePayload)
  });
  await h.RC.review.reload();
  await Promise.resolve();

  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0][0], "/pdf/api/review-queue");
  assert.equal(h.calls[0][1].method, "POST");
  const body = JSON.parse(h.calls[0][1].body);
  assert.equal(body.context.selection, "private selected text");
  assert.equal(h.calls[0][0].includes("private"), false);
  assert.ok(h.storageCalls.some(([op, key]) => op === "set" && key === "reviewQueueV2"));
  assert.deepEqual(h.localCalls, []);
  assert.doesNotMatch(
    h.pane.innerHTML,
    /当前内容来源/,
    "候选原因属于结构化元数据，不应重新塞进卡面"
  );
  assert.match(h.pane.innerHTML, /vc-card/);
  assert.equal(h.learningCardRenders[0].gid, "anki_card_10");
});

test("mounts the card workspace above the unchanged assistant thread", async () => {
  const h = harness({
    context: { file: "book.pdf", page: 2 },
    fetchImpl: async () => response(candidatePayload)
  });
  await h.RC.review.reload();

  const workspace = h.document.getElementById("asst-review-workspace");
  const toggle = h.document.getElementById("asst-review-toggle");
  assert.ok(workspace);
  assert.ok(toggle);
  assert.equal(toggle.getAttribute("data-action"), "toggle-review");
  // 卡片区与 thread 之间只允许隔着那条拖动分隔线（用户要求可调上下比例），
  // 顺序仍是 卡片区 → 分隔线 → 完整助手聊天。
  const split = h.paneRoot.children[h.paneRoot.children.indexOf(workspace) + 1];
  assert.equal(split?.className, "rv-split", "卡片区之后必须紧跟拖动分隔线");
  assert.equal(h.paneRoot.children.indexOf(split) + 1,
    h.paneRoot.children.indexOf(h.thread),
    "复习卡片区必须紧邻且位于完整助手聊天 thread 上方");
  assert.equal(h.thread.parentNode, h.paneRoot, "原助手聊天不得被替换或移走");

  assert.equal(h.RC.review.setMode(true), "review");
  assert.equal(workspace.hidden, false);
  assert.equal(h.RC.review.setMode(false), "normal");
  assert.equal(workspace.hidden, true);
});

// 复习卡不再顶一行牌组与进度，但 deck 数据必须留着。
//
// b33fafc0 按用户拍板去掉过普通卡的顶部提示行（底部圆点已经说清进度，顶栏只是把
// 卡面越挤越小）；后来 rv-meta 又把它加了回来。这里同时钉住两件事：那一行不能出现，
// 而 deck 仍要在卡片数据里 —— 投影与导出都要用它，删展示不等于删数据。
test("复习卡不显示牌组顶行，但 deck 数据保留", async () => {
  const h = harness({
    context: { file: "book.pdf", page: 3 },
    fetchImpl: async () => response({
      ok: true,
      due_total: 1,
      related_total: 1,
      cards: [
        { id: 41, question: "Q41", answer: "A41", deck: "Reader::Architecture" },
      ],
    }),
  });
  await h.RC.review.reload();

  assert.doesNotMatch(h.pane.innerHTML, /rv-meta/, "顶栏容器不得出现在 DOM 里");
  assert.doesNotMatch(
    h.pane.innerHTML,
    /Reader::Architecture/,
    "牌组名不得占据卡面顶上那一行",
  );
  const rendered = h.learningCardRenders.at(-1);
  assert.equal(
    rendered.card.deck,
    "Reader::Architecture",
    "deck 只是不展示，数据必须仍在卡片上（投影与导出要用）",
  );
});

test("review reuses the shared flashcard pager without visible arrow buttons", async () => {
  const h = harness({
    context: { file: "book.pdf", page: 3 },
    fetchImpl: async () => response({
      ok: true,
      due_total: 3,
      related_total: 3,
      cards: [
        { id: 31, question: "Q31", answer: "A31" },
        { id: 32, question: "Q32", answer: "A32" },
        { id: 33, question: "Q33", answer: "A33" }
      ]
    })
  });
  await h.RC.review.reload();

  assert.equal(h.findActions("previous").length, 0);
  assert.equal(h.findActions("next").length, 0);
  assert.doesNotMatch(h.pane.innerHTML, /上一张|下一张|>‹<|>›</);
  assert.equal(h.currentPager().spec.slides.length, 3);
  assert.equal(h.currentPager().spec.dots.length, 3);
  assert.deepEqual(
    h.learningCardRenders.slice(0, 3).map(({ gid }) => gid),
    ["anki_card_31", "anki_card_32", "anki_card_33"],
    "each review slide must remain its own stable entity"
  );
  assert.ok(
    h.learningCardRenders.slice(0, 3).every(({ card }) =>
      card._showBack === false
    )
  );

  h.currentPager().emit(2);
  await settleAsync();
  assert.equal(h.RC.review.currentCard().id, 33);
  assert.equal(h.getStored().index, 2);
  // 进度由底部圆点表达，不再在卡面顶上占一行文字：b33fafc0 已按用户拍板去掉
  // 普通卡的顶部提示行，理由是圆点足够而顶栏把卡面越挤越小。这里守护的意图不变
  // ——翻到第 3 张之后，界面必须反映出当前就在第 3 张。
  assert.match(
    h.pane.innerHTML,
    /class="fc-dot on" data-goto="2" title="第 3 张"/,
    "当前页码必须在分页圆点上体现",
  );

  h.RC.review.previous();
  assert.equal(h.RC.review.currentCard().id, 32,
    "public previous/next APIs remain compatible");
});

test("whole review answer covers selected paragraphs without erasing their state", () => {
  const registry = ContextSelection.createRegistry();
  const answerId = "review-answer:stable";
  const firstId = `${answerId}:part:0`;
  const secondId = `${answerId}:part:1`;

  registry.select({
    id: firstId,
    kind: "review-answer-segment",
    text: "第一段",
    parentId: answerId
  });
  registry.select({
    id: secondId,
    kind: "review-answer-segment",
    text: "第二段",
    parentId: answerId
  });
  registry.select({
    id: answerId,
    kind: "review-answer",
    text: "第一段\n\n第二段",
    covers: [firstId, secondId]
  });

  assert.deepEqual(
    registry.snapshot().items.map((item) => item.id),
    [answerId],
    "整条回答入上下文后，内部段落不得重复发送"
  );
  assert.equal(registry.isSelected(firstId), true);
  assert.equal(registry.isEffective(firstId), false);

  registry.deselect(answerId);
  assert.deepEqual(
    registry.snapshot().items.map((item) => item.id),
    [firstId, secondId],
    "取消整条后应恢复原段落选择，不能破坏缓存/选择态"
  );
});

test("reuses only an exact-context snapshot and fences a changed page", async () => {
  const first = harness({
    context: { file: "book.pdf", page: 2, visible_text: "page two" },
    fetchImpl: async () => response(candidatePayload)
  });
  await first.RC.review.reload();
  await Promise.resolve();
  const stored = first.getStored();
  assert.ok(stored.client_context_key);

  const same = harness({
    context: { file: "book.pdf", page: 2, visible_text: "page two" },
    stored,
    fetchImpl: async () => { throw new Error("must not fetch exact cache"); }
  });
  await same.RC.review.load();
  assert.equal(same.calls.length, 0);
  assert.match(same.pane.innerHTML, /Q10/);

  const changed = harness({
    context: { file: "book.pdf", page: 3, visible_text: "page three" },
    stored,
    fetchImpl: async () => { throw new Error("offline"); }
  });
  await changed.RC.review.load();
  assert.equal(changed.calls.length, 2, "POST then GET due fallback");
  assert.doesNotMatch(changed.pane.innerHTML, /Q10/);
  assert.match(changed.pane.innerHTML, /拉取失败/);
});

test("missing adapter context preserves the GET due queue", async () => {
  const h = harness({
    context: new Error("adapter not ready"),
    fetchImpl: async (url, init) => {
      assert.equal(init, undefined);
      assert.equal(url, "/pdf/api/review-queue?limit=30");
      return response({ ok: true, due_total: 1, cards: [] });
    }
  });
  await h.RC.review.reload();
  assert.equal(h.calls.length, 1);
});

test("a delayed old queue response cannot replace or cache the new context", async () => {
  let currentContext = {
    file: "book.pdf",
    page: 1,
    visible_text: "first page"
  };
  const requests = [];
  const h = harness({
    context: () => currentContext,
    fetchImpl: async () => {
      const request = deferred();
      requests.push(request);
      return request.promise;
    }
  });

  const firstLoad = h.RC.review.reload();
  await settleAsync();
  currentContext = {
    file: "book.pdf",
    page: 2,
    visible_text: "second page"
  };
  const secondLoad = h.RC.review.reload();
  await settleAsync();
  assert.equal(requests.length, 2);

  requests[1].resolve(response({
    ok: true,
    due_total: 1,
    related_total: 1,
    cards: [{
      id: 22,
      entity_id: "entity-new",
      question: "NEW CONTEXT CARD",
      answer: "new"
    }]
  }));
  await secondLoad;

  requests[0].resolve(response({
    ok: true,
    due_total: 1,
    related_total: 1,
    cards: [{
      id: 11,
      entity_id: "entity-old",
      question: "STALE CONTEXT CARD",
      answer: "stale"
    }]
  }));
  await firstLoad;
  await settleAsync();

  assert.equal(h.RC.review.currentCard().entity_id, "entity-new");
  assert.match(h.pane.innerHTML, /NEW CONTEXT CARD/);
  assert.doesNotMatch(h.pane.innerHTML, /STALE CONTEXT CARD/);
  assert.equal(h.getStored().cards[0].entity_id, "entity-new",
    "cache writes must retain the validated context snapshot");
});

test("switching cards invalidates a delayed draft and busy blocks duplicates", async () => {
  const draftRequest = deferred();
  let draftCalls = 0;
  const h = harness({
    context: { file: "book.pdf", page: 4 },
    fetchImpl: async (url) => {
      if (url === "/pdf/api/review-queue") {
        return response({
          ok: true,
          due_total: 2,
          related_total: 2,
          cards: [
            {
              id: 1,
              entity_id: "entity-a",
              question: "CARD A",
              answer: "answer a"
            },
            {
              id: 2,
              entity_id: "entity-b",
              question: "CARD B",
              answer: "answer b"
            }
          ]
        });
      }
      if (url === "/api/assistant/card-improvement-draft") {
        draftCalls += 1;
        return draftRequest.promise;
      }
      throw new Error(`unexpected ${url}`);
    }
  });
  await h.RC.review.reload();
  h.RC.review.setMode(true);
  h.registry.select({
    id: "review-answer:a",
    kind: "review-answer",
    text: "selected answer",
    meta: {
      review_mode: true,
      card_key: "anki_card_1",
      answer_id: "review-answer:a",
      segment_index: -1,
      question: "selected question",
      card: { entity_id: "entity-a" }
    }
  });

  h.clickAction("prepare-draft", "anki");
  await settleAsync();
  const busyButton = h.clickAction("prepare-draft", "anki");
  assert.equal(busyButton.disabled, true);
  await settleAsync();
  assert.equal(draftCalls, 1, "busy draft must not be submitted twice");

  h.RC.review.next();
  assert.equal(h.RC.review.currentCard().entity_id, "entity-b");
  draftRequest.resolve(response({
    ok: true,
    draft_id: "stale-draft-a",
    targets: ["anki"],
    drafts: {
      cards: [{ front: "STALE DRAFT A", back: "must not render" }]
    }
  }));
  await settleAsync();

  assert.equal(h.RC.review.currentCard().entity_id, "entity-b");
  assert.match(h.pane.innerHTML, /CARD B/);
  assert.doesNotMatch(h.pane.innerHTML, /STALE DRAFT A|stale-draft-a/);
});

test("a delayed commit cannot attach to the next card and cannot double-submit", async () => {
  const commitRequest = deferred();
  let commitCalls = 0;
  const h = harness({
    context: { file: "book.pdf", page: 5 },
    fetchImpl: async (url) => {
      if (url === "/pdf/api/review-queue") {
        return response({
          ok: true,
          due_total: 2,
          related_total: 2,
          cards: [
            {
              id: 1,
              entity_id: "entity-a",
              question: "CARD A",
              answer: "answer a"
            },
            {
              id: 2,
              entity_id: "entity-b",
              question: "CARD B",
              answer: "answer b"
            }
          ]
        });
      }
      if (url === "/api/assistant/card-improvement-draft") {
        return response({
          ok: true,
          draft_id: "draft-a",
          targets: ["anki"],
          drafts: {
            cards: [{ front: "DRAFT A", back: "preview" }]
          }
        });
      }
      if (url === "/api/assistant/card-improvement-commit") {
        commitCalls += 1;
        return commitRequest.promise;
      }
      throw new Error(`unexpected ${url}`);
    }
  });
  await h.RC.review.reload();
  h.RC.review.setMode(true);
  h.registry.select({
    id: "review-answer:a",
    kind: "review-answer",
    text: "selected answer",
    meta: {
      review_mode: true,
      card_key: "anki_card_1",
      answer_id: "review-answer:a",
      segment_index: -1,
      question: "selected question",
      card: { entity_id: "entity-a" }
    }
  });

  h.clickAction("prepare-draft", "anki");
  await settleAsync();
  assert.match(h.pane.innerHTML, /DRAFT A/);

  h.clickAction("commit", "anki");
  await settleAsync();
  const busyButton = h.clickAction("commit", "anki");
  assert.equal(busyButton.disabled, true);
  await settleAsync();
  assert.equal(commitCalls, 1, "busy commit must not be submitted twice");

  h.RC.review.next();
  commitRequest.resolve(response({
    ok: true,
    summary: "STALE COMMIT RESULT"
  }));
  await settleAsync();

  assert.equal(h.RC.review.currentCard().entity_id, "entity-b");
  assert.match(h.pane.innerHTML, /CARD B/);
  assert.doesNotMatch(h.pane.innerHTML, /STALE COMMIT RESULT/);
});

test("closing review invalidates an in-flight draft before reopening", async () => {
  const draftRequest = deferred();
  const h = harness({
    context: { file: "book.pdf", page: 6 },
    fetchImpl: async (url) => {
      if (url === "/pdf/api/review-queue") {
        return response({
          ok: true,
          due_total: 1,
          related_total: 1,
          cards: [{
            id: 1,
            entity_id: "entity-a",
            question: "CARD A",
            answer: "answer a"
          }]
        });
      }
      if (url === "/api/assistant/card-improvement-draft") {
        return draftRequest.promise;
      }
      throw new Error(`unexpected ${url}`);
    }
  });
  await h.RC.review.reload();
  h.RC.review.setMode(true);
  h.registry.select({
    id: "review-answer:a",
    kind: "review-answer",
    text: "selected answer",
    meta: {
      review_mode: true,
      card_key: "anki_card_1",
      answer_id: "review-answer:a",
      segment_index: -1,
      question: "selected question",
      card: { entity_id: "entity-a" }
    }
  });

  h.clickAction("prepare-draft", "anki");
  await settleAsync();
  assert.equal(h.RC.review.setMode(false), "normal");
  draftRequest.resolve(response({
    ok: true,
    draft_id: "draft-after-close",
    targets: ["anki"],
    drafts: {
      cards: [{ front: "STALE AFTER CLOSE", back: "must not render" }]
    }
  }));
  await settleAsync();
  assert.equal(h.RC.review.setMode(true), "review");
  await settleAsync();

  assert.match(h.pane.innerHTML, /CARD A/);
  assert.doesNotMatch(h.pane.innerHTML, /STALE AFTER CLOSE|draft-after-close/);
});

test("standard Anki flow initially shows only front then reveals back and four eases", async () => {
  const h = harness({
    context: { file: "book.pdf", page: 7 },
    fetchImpl: async (url) => {
      assert.equal(url, "/pdf/api/review-queue");
      return response({
        ok: true,
        due_total: 1,
        related_total: 1,
        cards: [{
          id: 71,
          question: "FRONT ONLY 71",
          answer: "BACK HIDDEN 71",
          deck: "Deck"
        }]
      });
    }
  });
  await h.RC.review.reload();

  assert.match(h.pane.innerHTML, /FRONT ONLY 71/);
  assert.doesNotMatch(h.pane.innerHTML, /BACK HIDDEN 71/);
  assert.equal(h.findActions("show-answer").length, 1);
  assert.equal(h.findActions("rate").length, 0);

  h.clickAction("show-answer");
  assert.match(h.pane.innerHTML, /BACK HIDDEN 71/);
  assert.equal(h.findActions("show-answer").length, 0);
  const ratings = h.findActions("rate");
  assert.deepEqual(
    ratings.map((button) => [
      button.getAttribute("data-ease"),
      button.textContent
    ]),
    [
      ["1", "再来"],
      ["2", "困难"],
      ["3", "良好"],
      ["4", "简单"]
    ]
  );
});

test("local repository enumerates confirmed due cards before new cards without Pi", async () => {
  const now = Date.now();
  let fetchCount = 0;
  const state = (overrides = {}) => ({
    phase: "confirmed",
    removed: false,
    confirmedAt: now - 1000,
    review: {
      status: "new", dueAt: null, lastReviewedAt: null,
      intervalDays: 0, ease: 0, reps: 0, lapses: 0,
      ...(overrides.review || {})
    },
    flags: { favorite: false, archived: false, ...(overrides.flags || {}) },
    projections: { anki: {} },
    exactState: {},
    ...Object.fromEntries(
      Object.entries(overrides).filter(([key]) => !["review", "flags"].includes(key))
    )
  });
  const repository = {
    async snapshot() {
      return [
        {
          id: "card_bbbb", stateRev: 2, deleted: false,
          source: { kind: "reader", sourceId: "source-b" },
          cards: [
            { type: "basic", front: "NEW FRONT", back: "NEW BACK" },
            { type: "basic", front: "ARCHIVED", back: "NO" },
            { type: "basic", front: "REMOVED", back: "NO" }
          ],
          states: {
            0: state(),
            1: state({ flags: { archived: true } }),
            2: state({ removed: true })
          }
        },
        {
          id: "card_aaaa", stateRev: 3, deleted: false,
          source: { kind: "reader", sourceId: "source-a" },
          cards: [
            { type: "basic", front: "DUE LATER", back: "A2" },
            { type: "basic", front: "DUE FIRST", back: "A1" },
            { type: "basic", front: "FUTURE", back: "NO" }
          ],
          states: {
            0: state({ review: { status: "review", dueAt: now - 1000 } }),
            1: state({ review: { status: "learning", dueAt: now - 5000 } }),
            2: state({ review: { status: "review", dueAt: now + 86400000 } })
          }
        }
      ];
    },
    async patchState() { throw new Error("not used"); }
  };
  const h = harness({
    context: { file: "book.pdf", page: 7 },
    cardRepository: repository,
    fetchImpl: async () => {
      fetchCount += 1;
      throw new Error("Pi must not be read while local cards exist");
    }
  });

  await h.RC.review.reload();

  assert.equal(fetchCount, 0);
  assert.equal(h.RC.review.currentCard().question, "DUE FIRST");
  assert.match(h.pane.innerHTML, /到期 2 · 本批 3/);
  h.RC.review.next();
  assert.equal(h.RC.review.currentCard().question, "DUE LATER");
  h.RC.review.next();
  assert.equal(h.RC.review.currentCard().question, "NEW FRONT");
  assert.doesNotMatch(h.pane.innerHTML, /ARCHIVED|REMOVED|FUTURE/);
});

test("local rating persists deterministic review state before advancing UI", async () => {
  const write = deferred();
  const patchCalls = [];
  let fetchCount = 0;
  const repository = {
    async snapshot() {
      return [{
        id: "card_abcd", stateRev: 7, deleted: false,
        source: { kind: "reader", sourceId: "source-local" },
        cards: [
          { type: "basic", front: "LOCAL ONE", back: "ANSWER ONE" },
          { type: "basic", front: "LOCAL TWO", back: "ANSWER TWO" }
        ],
        states: {
          0: {
            phase: "confirmed", removed: false, confirmedAt: 1,
            review: {
              status: "review", dueAt: 1, lastReviewedAt: 1,
              intervalDays: 2, ease: 3, reps: 4, lapses: 1
            },
            flags: { archived: false }, projections: { anki: {} }, exactState: {}
          },
          1: {
            phase: "confirmed", removed: false, confirmedAt: 2,
            review: {
              status: "new", dueAt: null, lastReviewedAt: null,
              intervalDays: 0, ease: 0, reps: 0, lapses: 0
            },
            flags: { archived: false }, projections: { anki: {} }, exactState: {}
          }
        }
      }];
    },
    patchState(...args) {
      patchCalls.push(structuredClone(args));
      return write.promise;
    }
  };
  const h = harness({
    context: {},
    cardRepository: repository,
    fetchImpl: async () => {
      fetchCount += 1;
      throw new Error("pure local rating must not call Pi");
    }
  });
  await h.RC.review.reload();
  h.clickAction("show-answer");
  h.clickEase(3);

  assert.equal(h.RC.review.currentCard().question, "LOCAL ONE",
    "UI must not advance before patchState succeeds");
  assert.match(h.pane.innerHTML, /ANSWER ONE/);
  assert.equal(patchCalls.length, 1);
  assert.equal(patchCalls[0][0], "card_abcd");
  assert.equal(patchCalls[0][1], 0);
  const review = patchCalls[0][2].review;
  assert.equal(review.status, "review");
  assert.equal(review.intervalDays, 5);
  assert.equal(review.ease, 3);
  assert.equal(review.reps, 5);
  assert.equal(review.lapses, 1);
  assert.equal(review.dueAt, review.lastReviewedAt + 5 * 86400000);

  write.resolve({
    states: {
      0: { review },
      1: { review: { status: "new" } }
    }
  });
  await settleAsync();

  assert.equal(h.RC.review.currentCard().question, "LOCAL TWO");
  assert.equal(fetchCount, 0);
});

test("failed local patch leaves the revealed card and queue unchanged", async () => {
  const repository = {
    async snapshot() {
      return [{
        id: "card_dead", stateRev: 1, deleted: false,
        source: { kind: "reader", sourceId: "source-dead" },
        cards: [{ type: "basic", front: "KEEP FRONT", back: "KEEP BACK" }],
        states: { 0: {
          phase: "confirmed", removed: false, confirmedAt: 1,
          review: {
            status: "new", dueAt: null, lastReviewedAt: null,
            intervalDays: 0, ease: 0, reps: 0, lapses: 0
          },
          flags: { archived: false }, projections: { anki: {} }, exactState: {}
        } }
      }];
    },
    async patchState() { throw new Error("disk unavailable"); }
  };
  const h = harness({
    context: {}, cardRepository: repository,
    fetchImpl: async () => { throw new Error("Pi must stay unused"); }
  });
  await h.RC.review.reload();
  h.clickAction("show-answer");
  h.clickEase(4);
  await settleAsync();

  assert.equal(h.RC.review.currentCard().question, "KEEP FRONT");
  assert.match(h.pane.innerHTML, /KEEP BACK/);
  assert.equal(h.getStored().cards.length, 1);
  assert.ok(h.toasts.some((text) =>
    text.includes("评分未保存") && text.includes("disk unavailable")
  ));
});

test("legacy Anki projection runs only after local Again is durable and cannot roll it back", async () => {
  const order = [];
  let writtenReview;
  const repository = {
    async snapshot() {
      return [{
        id: "card_beef", stateRev: 4, deleted: false,
        source: { kind: "legacy", sourceId: "legacy-entity" },
        cards: [{ type: "basic", front: "LOCAL LEGACY", back: "ANSWER" }],
        states: { 0: {
          phase: "confirmed", removed: false, confirmedAt: 1,
          review: {
            status: "review", dueAt: 1, lastReviewedAt: 1,
            intervalDays: 8, ease: 3, reps: 6, lapses: 2
          },
          flags: { archived: false },
          projections: { anki: { "pi-legacy": {
            status: "succeeded", cardIds: [9001]
          } } },
          exactState: {}
        } }
      }];
    },
    async patchState(_gid, _index, patch) {
      order.push("patchState");
      writtenReview = structuredClone(patch.review);
      return { states: { 0: { review: patch.review } } };
    }
  };
  const h = harness({
    context: {}, cardRepository: repository,
    fetchImpl: async (url, init) => {
      order.push("fetch");
      assert.equal(url, "/pdf/api/review-answer");
      assert.deepEqual(JSON.parse(init.body), {
        aid: JSON.parse(init.body).aid,
        card_id: 9001,
        ease: 1
      });
      return response({ ok: false, error: "Pi unavailable" }, { ok: false, status: 503 });
    }
  });
  await h.RC.review.reload();
  h.clickAction("show-answer");
  h.clickEase(1);
  await settleAsync();

  assert.deepEqual(order, ["patchState", "fetch"]);
  assert.equal(writtenReview.status, "relearning");
  assert.equal(writtenReview.intervalDays, 0);
  assert.equal(writtenReview.reps, 7);
  assert.equal(writtenReview.lapses, 3);
  assert.equal(writtenReview.dueAt, writtenReview.lastReviewedAt + 10 * 60000);
  assert.equal(h.RC.review.currentCard(), null,
    "projection rejection must not restore an already durable local result");
  assert.ok(h.toasts.some((text) =>
    text.includes("本地评分已保存") && text.includes("Pi unavailable")
  ));
});

test("zero local cards retains the legacy external queue and answer path", async () => {
  const calls = [];
  const h = harness({
    context: {},
    cardRepository: {
      async snapshot() { return []; },
      async patchState() { throw new Error("not used"); }
    },
    fetchImpl: async (url, init) => {
      calls.push([url, init && init.body]);
      if (url === "/pdf/api/review-queue?limit=30") {
        return response({
          ok: true, due_total: 1, related_total: 0,
          cards: [{ id: 7001, question: "LEGACY FRONT", answer: "LEGACY BACK" }]
        });
      }
      return response({ ok: true, next: { due: "tomorrow" } });
    }
  });
  await h.RC.review.reload();
  h.clickAction("show-answer");
  h.clickEase(3);
  await settleAsync();

  assert.deepEqual(calls.map(([url]) => url), [
    "/pdf/api/review-queue?limit=30",
    "/pdf/api/review-answer"
  ]);
  assert.deepEqual(JSON.parse(calls[1][1]).card_id, 7001);
});

test("sibling Anki cards never collide even when provenance entity metadata is identical", async () => {
  const h = harness({
    context: { file: "book.pdf", page: 7 },
    fetchImpl: async () => response({
      ok: true,
      due_total: 2,
      related_total: 2,
      cards: [
        {
          id: 501,
          note_id: 1501,
          entity_id: "card_shared",
          entity_index: 0,
          question: "SIBLING 501",
          answer: "A501"
        },
        {
          id: 502,
          note_id: 1502,
          entity_id: "card_shared",
          entity_index: 0,
          question: "SIBLING 502",
          answer: "A502"
        }
      ]
    })
  });
  await h.RC.review.reload();
  assert.deepEqual(
    h.learningCardRenders.slice(0, 2).map(({ gid }) => gid),
    ["anki_card_501", "anki_card_502"]
  );
  h.currentPager().emit(1);
  assert.equal(h.RC.review.currentCard().id, 502);
  assert.deepEqual(
    h.learningCardRenders.slice(-2).map(({ gid }) => gid),
    ["anki_card_501", "anki_card_502"],
    "rerendered slides must keep their independent stable ids"
  );
});

test("successful ratings complete a card while Again moves it to queue tail", async () => {
  const answerPayloads = [];
  const h = harness({
    context: { file: "book.pdf", page: 8 },
    fetchImpl: async (url, init) => {
      if (url === "/pdf/api/review-queue") {
        return response({
          ok: true,
          due_total: 3,
          related_total: 3,
          cards: [
            { id: 81, question: "Q81", answer: "A81" },
            { id: 82, question: "Q82", answer: "A82" },
            { id: 83, question: "Q83", answer: "A83" }
          ]
        });
      }
      assert.equal(url, "/pdf/api/review-answer");
      answerPayloads.push(JSON.parse(init.body));
      return response({ ok: true });
    }
  });
  await h.RC.review.reload();

  h.clickAction("show-answer");
  h.clickEase(3);
  await settleAsync();
  assert.equal(h.RC.review.currentCard().id, 82);
  assert.deepEqual(h.getStored().cards.map((card) => card.id), [82, 83]);
  assert.deepEqual(h.getStored().completed_ids, [81]);

  h.clickAction("show-answer");
  h.clickEase(1);
  await settleAsync();
  assert.equal(h.RC.review.currentCard().id, 83);
  assert.deepEqual(
    h.getStored().cards.map((card) => card.id),
    [83, 82],
    "Again must retain the card exactly once at the queue tail"
  );
  assert.deepEqual(h.getStored().completed_ids, [81],
    "Again must not mark the card completed");
  assert.deepEqual(answerPayloads.map(({ card_id, ease }) => [card_id, ease]), [
    [81, 3],
    [82, 1]
  ]);

  h.RC.review.next();
  assert.equal(h.RC.review.currentCard().id, 82,
    "the Again card must remain reachable at the tail");
});

test("HTTP or business rejection restores an optimistically removed card", async () => {
  const h = harness({
    context: { file: "book.pdf", page: 9 },
    fetchImpl: async (url) => {
      if (url === "/pdf/api/review-queue") {
        return response({
          ok: true,
          due_total: 2,
          related_total: 2,
          cards: [
            { id: 91, question: "Q91", answer: "A91" },
            { id: 92, question: "Q92", answer: "A92" }
          ]
        });
      }
      assert.equal(url, "/pdf/api/review-answer");
      return response(
        { ok: false, error: "scheduler rejected card" },
        { ok: false, status: 409 }
      );
    }
  });
  await h.RC.review.reload();

  h.clickAction("show-answer");
  h.clickEase(3);
  await settleAsync();

  assert.deepEqual(h.getStored().cards.map((card) => card.id), [91, 92]);
  assert.deepEqual(h.getStored().completed_ids, []);
  // 只数**评分**（kind 'rev'）。复习事件日志（kind 'revlog'）是另一回事：
  // 它总是要记，跟这次评分被不被接受无关 —— 它正是补投的底账。
  assert.equal(
    h.outboxCalls.filter(([kind]) => kind === "rev").length, 0,
    "an HTTP rejection is authoritative and must never masquerade as offline");
  assert.ok(h.toasts.some((text) =>
    text.includes("卡片已放回复习队列") &&
    text.includes("scheduler rejected card")
  ));
  h.RC.review.previous();
  assert.equal(h.RC.review.currentCard().id, 91);
});

test("a delayed rejection restores the front instead of inheriting the next card answer", async () => {
  const delayedAnswer = deferred();
  const h = harness({
    context: { file: "book.pdf", page: 91 },
    fetchImpl: async (url) => {
      if (url === "/pdf/api/review-queue") {
        return response({
          ok: true,
          due_total: 2,
          related_total: 2,
          cards: [
            { id: 911, question: "Q911", answer: "ANSWER 911" },
            { id: 912, question: "Q912", answer: "ANSWER 912" }
          ]
        });
      }
      assert.equal(url, "/pdf/api/review-answer");
      return delayedAnswer.promise;
    }
  });
  await h.RC.review.reload();

  h.clickAction("show-answer");
  h.clickEase(3);
  assert.equal(h.RC.review.currentCard().id, 912);
  h.clickAction("show-answer");
  assert.match(h.pane.innerHTML, /ANSWER 912/);

  delayedAnswer.resolve(response(
    { ok: false, error: "scheduler rejected after next reveal" },
    { ok: false, status: 409 }
  ));
  await settleAsync();

  assert.equal(h.RC.review.currentCard().id, 911);
  assert.equal(h.getStored().index, 0);
  assert.equal(h.currentPager().spec.index, 0);
  assert.doesNotMatch(h.pane.innerHTML, /ANSWER 911|ANSWER 912/,
    "restored and non-current cards must both return to their fronts");
  assert.equal(h.findActions("rate").length, 0);
  assert.equal(h.findActions("show-answer").length, 2);
});

test("a delayed rejection fences and clears the next card's pending draft", async () => {
  const delayedAnswer = deferred();
  const delayedDraft = deferred();
  const h = harness({
    context: { file: "book.pdf", page: 92 },
    fetchImpl: async (url) => {
      if (url === "/pdf/api/review-queue") {
        return response({
          ok: true,
          due_total: 2,
          related_total: 2,
          cards: [
            {
              id: 921,
              entity_id: "entity-921",
              question: "Q921",
              answer: "A921"
            },
            {
              id: 922,
              entity_id: "entity-922",
              question: "Q922",
              answer: "A922"
            }
          ]
        });
      }
      if (url === "/pdf/api/review-answer") return delayedAnswer.promise;
      if (url === "/api/assistant/card-improvement-draft") {
        return delayedDraft.promise;
      }
      throw new Error(`unexpected ${url}`);
    }
  });
  await h.RC.review.reload();
  h.RC.review.setMode(true);

  h.clickAction("show-answer");
  h.clickEase(3);
  assert.equal(h.RC.review.currentCard().id, 922);
  h.registry.select({
    id: "review-answer:922",
    kind: "review-answer",
    text: "selected answer for 922",
    meta: {
      review_mode: true,
      card_key: "anki_card_922",
      answer_id: "review-answer:922",
      segment_index: -1,
      question: "selected question for 922",
      card: { entity_id: "entity-922" }
    }
  });
  h.clickAction("prepare-draft", "anki");
  await settleAsync();
  assert.match(h.pane.innerHTML, /正在生成草稿/);
  assert.ok(h.findActions("prepare-draft").every((button) => button.disabled));

  delayedAnswer.resolve(response(
    { ok: false, error: "scheduler rejected during draft" },
    { ok: false, status: 409 }
  ));
  await settleAsync();
  assert.equal(h.RC.review.currentCard().id, 921);
  assert.doesNotMatch(h.pane.innerHTML, /正在生成草稿/);
  assert.ok(h.findActions("prepare-draft").every((button) => !button.disabled));

  delayedDraft.resolve(response({
    ok: true,
    draft_id: "stale-draft-922",
    targets: ["anki"],
    drafts: {
      cards: [{ front: "STALE DRAFT 922", back: "must not render" }]
    }
  }));
  await settleAsync();
  assert.equal(h.RC.review.currentCard().id, 921);
  assert.doesNotMatch(h.pane.innerHTML, /STALE DRAFT 922|stale-draft-922/);
  assert.ok(h.findActions("prepare-draft").every((button) => !button.disabled));
});

test("a rejected rating restores its original page after the user switches context", async () => {
  let currentContext = {
    file: "book.pdf",
    page: 12,
    visible_text: "page A"
  };
  let pageALoads = 0;
  const delayedAnswer = deferred();
  const h = harness({
    context: () => currentContext,
    fetchImpl: async (url, init) => {
      if (url === "/pdf/api/review-answer") return delayedAnswer.promise;
      assert.equal(url, "/pdf/api/review-queue");
      const body = JSON.parse(init.body);
      if (body.context.page === 12) {
        pageALoads += 1;
        return response({
          ok: true,
          due_total: 1,
          related_total: 1,
          cards: pageALoads === 1
            ? [{ id: 121, question: "QA", answer: "AA" }]
            : []
        });
      }
      return response({
        ok: true,
        due_total: 1,
        related_total: 1,
        cards: [{ id: 122, question: "QB", answer: "AB" }]
      });
    }
  });
  await h.RC.review.reload();

  h.clickAction("show-answer");
  h.clickEase(3);
  currentContext = {
    file: "book.pdf",
    page: 13,
    visible_text: "page B"
  };
  await h.RC.review.reload();
  assert.equal(h.RC.review.currentCard().id, 122);

  delayedAnswer.resolve(response(
    { ok: false, error: "scheduler rejected after navigation" },
    { ok: false, status: 500 }
  ));
  await settleAsync();
  assert.equal(h.RC.review.currentCard().id, 122,
    "an old rejection must not replace the active page");

  currentContext = {
    file: "book.pdf",
    page: 12,
    visible_text: "page A"
  };
  await h.RC.review.reload();
  assert.equal(h.RC.review.currentCard().id, 121,
    "returning to the original page must restore the rejected card");
  assert.deepEqual(h.getStored().cards.map((card) => card.id), [121]);
  assert.deepEqual(h.getStored().completed_ids, []);
});

test("network TypeError preserves optimistic state and sends one durable outbox item", async () => {
  const h = harness({
    context: { file: "book.pdf", page: 10 },
    fetchImpl: async (url) => {
      if (url === "/pdf/api/review-queue") {
        return response({
          ok: true,
          due_total: 2,
          related_total: 2,
          cards: [
            { id: 101, question: "Q101", answer: "A101" },
            { id: 102, question: "Q102", answer: "A102" }
          ]
        });
      }
      assert.equal(url, "/pdf/api/review-answer");
      throw new TypeError("network offline");
    }
  });
  await h.RC.review.reload();

  h.clickAction("show-answer");
  h.clickEase(4);
  await settleAsync();

  // 同上：评分只该入队一次；事件日志是独立的一条，不参与这里的计数。
  const ratingCalls = h.outboxCalls.filter(([kind]) => kind === "rev");
  assert.equal(ratingCalls.length, 1);
  const [kind, aid, url, payload] = ratingCalls[0];
  assert.equal(kind, "rev");
  assert.match(aid, /^a_[a-f0-9]{16}$/);
  assert.equal(url, "/pdf/api/review-answer");
  assert.deepEqual(payload, { aid, card_id: 101, ease: 4 });
  assert.deepEqual(h.getStored().cards.map((card) => card.id), [102]);
  assert.deepEqual(h.getStored().completed_ids, [101]);
  // 断言"入队了 + 说了为什么"，不锁死整句文案。
  // 2026-08-19 起 toast 会带上失败原因：不只是断网会入队，服务端暂时处理不了
  // （Anki 没起来 / 正在 sync → 502/503）也会 —— 那时只说"恢复后同步"会让用户
  // 以为是自己没网。
  assert.ok(
    h.toasts.some((text) => text.includes("答题已入队")),
    "断网时必须告诉用户答题已入队",
  );
});

test("open source permits safe Obsidian and HTTP URLs but rejects unsafe sources", async () => {
  async function loadSourceCard(card, options = {}) {
    const h = harness({
      context: { file: "book.pdf", page: 11 },
      openImpl: options.openImpl || null,
      fetchImpl: async () => response({
        ok: true,
        due_total: 1,
        related_total: 1,
        cards: [Object.assign({
          id: 111,
          question: "SOURCE CARD",
          answer: "SOURCE ANSWER"
        }, card)]
      })
    });
    await h.RC.review.reload();
    h.RC.review.setImproveExpanded(true);
    h.clickAction("open-source");
    return h;
  }

  const obsidian = await loadSourceCard({
    source_ref:
      "obsidian://open?vault=Obsidian%20Vault&file=notes%2Flinear-algebra.md"
  });
  assert.equal(obsidian.opened.length, 0);
  assert.equal(obsidian.assigned.length, 1);
  assert.match(obsidian.assigned[0], /^obsidian:\/\/open\?/);
  assert.match(obsidian.assigned[0], /file=notes%2Flinear-algebra\.md/);

  const http = await loadSourceCard({
    source_url: "https://example.test/source?q=direct-sum"
  });
  assert.deepEqual(http.opened, [[
    "https://example.test/source?q=direct-sum",
    "_blank",
    "noopener,noreferrer"
  ]]);
  assert.deepEqual(http.assigned, []);

  const noopenerNull = await loadSourceCard({
    source_url: "https://example.test/source?q=noopener-null"
  }, {
    openImpl: () => null
  });
  assert.equal(noopenerNull.opened.length, 1);
  assert.deepEqual(noopenerNull.assigned, []);
  assert.equal(
    noopenerNull.getLocationHref(),
    "https://reader.example/book",
    "noopener returning null must not replace the reader after opening a tab"
  );

  const invalid = await loadSourceCard({
    source_url: "https://user:secret@example.test/private",
    source_ref: "obsidian://open?vault=Obsidian%20Vault&file=../secret.md"
  });
  assert.deepEqual(invalid.opened, []);
  assert.deepEqual(invalid.assigned, []);
  const fallback = invalid.events.find((event) =>
    event.type === "rc:open-card-source"
  );
  assert.ok(fallback, "invalid direct URL should use the non-navigation event fallback");
  assert.equal(fallback.detail.source_url, "",
    "unsafe source URL must not be forwarded through the fallback event");
  assert.ok(invalid.toasts.includes(
    "这张卡尚未记录可直接打开的原笔记链接"
  ));

  for (const unsafeFile of [
    "C%3A%5Csecret.md",
    "%252e%252e%252fsecret.md",
    "%255c%255cserver%255cshare%255csecret.md"
  ]) {
    const unsafe = await loadSourceCard({
      source_url:
        `obsidian://open?vault=Obsidian%20Vault&file=${unsafeFile}`
    });
    assert.deepEqual(unsafe.opened, []);
    assert.deepEqual(unsafe.assigned, []);
    assert.equal(unsafe.getLocationHref(), "https://reader.example/book");
  }
});

test("extension background permits only GET/POST and owns the private queue key", () => {
  assert.match(BACKGROUND, /"reviewQueueV2"/);
  assert.match(
    BACKGROUND,
    /add\(\["\/pdf\/api\/review-queue"\], \["GET", "POST"\]\)/
  );
});
