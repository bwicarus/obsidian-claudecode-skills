import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("../..", import.meta.url));
const VOICE_PATH = `${ROOT}/_server_deploy/static/pdf/rc-voicecall.js`;
const BACKGROUND_PATH = `${ROOT}/extensions/bw-reader-webext/background.js`;
const WEB_PINS_PATH = `${ROOT}/extensions/bw-reader-webext/src/web-pins.js`;
const STICKY_PATH = `${ROOT}/_server_deploy/static/pdf/rc-stickynote.js`;
const VOICE_SOURCE = fs.readFileSync(VOICE_PATH, "utf8");
const BACKGROUND_SOURCE = fs.readFileSync(BACKGROUND_PATH, "utf8");
const WEB_PINS_SOURCE = fs.readFileSync(WEB_PINS_PATH, "utf8");
const STICKY_SOURCE = fs.readFileSync(STICKY_PATH, "utf8");

const tick = () => new Promise((resolve) => setImmediate(resolve));

class FakeClassList {
  constructor() {
    this.names = new Set();
  }
  add(...names) {
    names.forEach((name) => this.names.add(String(name)));
  }
  remove(...names) {
    names.forEach((name) => this.names.delete(String(name)));
  }
  contains(name) {
    return this.names.has(String(name));
  }
  toggle(name, force) {
    name = String(name);
    const on = force === undefined ? !this.names.has(name) : Boolean(force);
    if (on) this.names.add(name);
    else this.names.delete(name);
    return on;
  }
}

class FakeStyle {
  constructor() {
    this.values = new Map();
  }
  setProperty(name, value) {
    this.values.set(String(name), String(value));
  }
  removeProperty(name) {
    this.values.delete(String(name));
  }
  getPropertyValue(name) {
    return this.values.get(String(name)) || "";
  }
}

class FakeElement {
  constructor(tagName = "div") {
    this.tagName = String(tagName).toUpperCase();
    this.classList = new FakeClassList();
    this.style = new FakeStyle();
    this.dataset = Object.create(null);
    this.children = [];
    this.listeners = new Map();
    this.captures = new Set();
    this.isConnected = true;
    this.parentNode = null;
    this.type = "";
    this.title = "";
    this.attributes = new Map();
    this.rect = { left: 0, top: 0, width: 320, height: 200 };
  }
  set className(value) {
    this.classList.names = new Set(
      String(value || "").split(/\s+/).filter(Boolean),
    );
  }
  get className() {
    return [...this.classList.names].join(" ");
  }
  appendChild(child) {
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  querySelector(selector) {
    if (selector === ".vc-card-rs") {
      return this.children.find((child) =>
        child.classList.contains("vc-card-rs")
      ) || null;
    }
    return null;
  }
  setAttribute(name, value) {
    this.attributes.set(String(name), String(value));
  }
  getAttribute(name) {
    return this.attributes.get(String(name)) || null;
  }
  closest(selector) {
    for (const part of String(selector).split(",")) {
      const item = part.trim();
      if (!item) continue;
      if (item === "button" && this.tagName === "BUTTON") return this;
      if (item === "a" && this.tagName === "A") return this;
      if (item === "input" && this.tagName === "INPUT") return this;
      if (item === "textarea" && this.tagName === "TEXTAREA") return this;
      if (item === "select" && this.tagName === "SELECT") return this;
      if (item.startsWith(".") && this.classList.contains(item.slice(1))) {
        return this;
      }
      if (item === '[contenteditable="true"]' &&
          this.getAttribute("contenteditable") === "true") {
        return this;
      }
      if (item === '[role="button"]' &&
          this.getAttribute("role") === "button") {
        return this;
      }
      if (item === "[data-fc]" && this.dataset.fc != null) return this;
    }
    return this.parentNode?.closest?.(selector) || null;
  }
  addEventListener(type, listener) {
    const rows = this.listeners.get(type) || [];
    rows.push(listener);
    this.listeners.set(type, rows);
  }
  removeEventListener(type, listener) {
    this.listeners.set(
      type,
      (this.listeners.get(type) || []).filter((row) => row !== listener),
    );
  }
  emit(type, values = {}) {
    const event = {
      type,
      target: values.target || this,
      currentTarget: this,
      pointerId: values.pointerId ?? 1,
      pointerType: values.pointerType || "touch",
      isPrimary: values.isPrimary ?? true,
      button: values.button ?? 0,
      clientX: values.clientX ?? 10,
      clientY: values.clientY ?? 10,
      cancelable: values.cancelable ?? true,
      defaultPrevented: false,
      propagationStopped: false,
      immediateStopped: false,
      preventDefault() {
        if (this.cancelable) this.defaultPrevented = true;
      },
      stopPropagation() {
        this.propagationStopped = true;
      },
      stopImmediatePropagation() {
        this.immediateStopped = true;
        this.propagationStopped = true;
      },
    };
    for (const listener of [...(this.listeners.get(type) || [])]) {
      listener.call(this, event);
      if (event.immediateStopped) break;
    }
    return event;
  }
  getBoundingClientRect() {
    return { ...this.rect };
  }
  setPointerCapture(pointerId) {
    this.captures.add(pointerId);
  }
  releasePointerCapture(pointerId) {
    this.captures.delete(pointerId);
  }
}

function clone(value) {
  return value == null ? value : structuredClone(value);
}

function resizeHarness({
  extensionRecords = null,
  pwaRecords = null,
} = {}) {
  const start = VOICE_SOURCE.indexOf("var _CARD_PRESENTATION_ID");
  const end = VOICE_SOURCE.indexOf("function _pinPaint", start);
  assert.ok(start >= 0 && end > start, "card resize helper block must remain extractable");

  const extensionStored = new Map(
    Object.entries(extensionRecords || {}).map(([key, value]) => [
      key,
      clone(value),
    ]),
  );
  const extensionCalls = [];
  const extensionStore = extensionRecords === false ? null : {
    async get(cid) {
      extensionCalls.push({ op: "get", cid });
      return clone(extensionStored.get(cid) || null);
    },
    async set(cid, value) {
      extensionCalls.push({ op: "set", cid, value: clone(value) });
      extensionStored.set(cid, clone(value));
      return true;
    },
  };

  const records = new Map(
    Object.entries(pwaRecords || {}).map(([key, value]) => [key, clone(value)]),
  );
  const pwaCalls = [];
  const pwaStore = {
    async get(namespace, id, options) {
      pwaCalls.push({ op: "get", namespace, id, options: clone(options) });
      return clone(records.get(id) || null);
    },
    async put(namespace, record, options) {
      pwaCalls.push({
        op: "put",
        namespace,
        record: clone(record),
        options: clone(options),
      });
      records.set(record.id, { ...clone(record), rev: (options.ifRev || 0) + 1 });
      return clone(records.get(record.id));
    },
  };
  const BWReaderRuntime = {
    pwaRuntime: {
      localStores() {
        return { device: pwaStore };
      },
    },
  };

  let rafSequence = 0;
  const rafs = new Map();
  const documentListeners = new Map();
  const document = {
    createElement(tagName) {
      return new FakeElement(tagName);
    },
    addEventListener(type, listener) {
      const rows = documentListeners.get(type) || [];
      rows.push(listener);
      documentListeners.set(type, rows);
    },
    emit(type, values = {}) {
      const event = {
        type,
        target: values.target || null,
        cancelable: values.cancelable ?? true,
        defaultPrevented: false,
        propagationStopped: false,
        immediateStopped: false,
        preventDefault() {
          if (this.cancelable) this.defaultPrevented = true;
        },
        stopPropagation() {
          this.propagationStopped = true;
        },
        stopImmediatePropagation() {
          this.immediateStopped = true;
          this.propagationStopped = true;
        },
      };
      if (values.path) event.composedPath = () => [...values.path];
      for (const listener of [...(documentListeners.get(type) || [])]) {
        listener.call(document, event);
        if (event.immediateStopped) break;
      }
      return event;
    },
  };
  const window = {
    __bwPageCardPresentation: extensionStore || undefined,
    BWReaderRuntime,
  };
  const RC = { toast() {} };
  const sandbox = {
    window,
    document,
    BWReaderRuntime,
    RC,
    _pins: { byCid: Object.create(null), cids: {}, map: {} },
    _cardForm() { return "full"; },
    Math,
    Date,
    Number,
    String,
    Object,
    Array,
    Promise,
    Error,
    setTimeout,
    clearTimeout,
    requestAnimationFrame(callback) {
      const id = ++rafSequence;
      rafs.set(id, callback);
      return id;
    },
    cancelAnimationFrame(id) {
      rafs.delete(id);
    },
  };
  vm.runInNewContext(
    `${VOICE_SOURCE.slice(start, end)}
this.api = {
  cid: _cardSizeCid,
  record: _cardSizeRecord,
  accept: _cardSizeAccept,
  load: _cardSizeLoad,
  persist: _cardSizePersist,
  pinReg: _pinReg,
  pageReg: _cardSizePageReg,
  bind: _cardResizeBind,
  eligible: _cardPressEligible,
  get: function (cid) {
    return _cardSizes[cid] ? Object.assign({}, _cardSizes[cid]) : null;
  }
};`,
    sandbox,
    { filename: VOICE_PATH },
  );
  return {
    api: sandbox.api,
    extensionCalls,
    pwaCalls,
    records,
    extensionStored: (cid) => clone(extensionStored.get(cid) || null),
    emitDocument(type, values) {
      return document.emit(type, values);
    },
    flushRaf() {
      const pending = [...rafs.entries()];
      rafs.clear();
      pending.forEach(([, callback]) => callback());
    },
  };
}

function newCard(cid, onSize) {
  const card = new FakeElement("div");
  card.classList.add("vc-card");
  card.dataset.vcCid = cid;
  card.__bwCardSizeApply = onSize || null;
  const body = new FakeElement("div");
  body.classList.add("vc-card-bd");
  card.appendChild(body);
  card.body = body;
  return card;
}

function tap(
  pressTarget,
  target = pressTarget,
  {
    x = 30,
    y = 40,
    pointerType = "mouse",
    pointerId = 1,
    leaveAfter = false,
    isPrimary = true,
  } = {},
) {
  pressTarget.emit("pointerdown", {
    target,
    pointerId,
    pointerType,
    isPrimary,
    clientX: x,
    clientY: y,
  });
  pressTarget.emit("pointerup", {
    target,
    pointerId,
    pointerType,
    isPrimary,
    clientX: x,
    clientY: y,
  });
  if (leaveAfter) {
    pressTarget.emit("pointerleave", {
      target,
      pointerId,
      pointerType,
      isPrimary,
      clientX: x,
      clientY: y,
    });
  }
}

function doubleTap(
  pressTarget,
  target = pressTarget,
  { x = 30, y = 40, pointerType = "mouse", leaveAfterEach = false } = {},
) {
  for (let index = 0; index < 2; index += 1) {
    const pointerId = pointerType === "mouse" ? 1 : index + 1;
    tap(pressTarget, target, {
      x,
      y,
      pointerType,
      pointerId,
      leaveAfter: leaveAfterEach,
    });
  }
}

test("尺寸只投影到页面 placement：同 cid 页面实例同步，侧栏/收藏/复习不受影响", () => {
  const h = resizeHarness({ extensionRecords: {} });
  const appliedA = [];
  const appliedB = [];
  const pageA = newCard("card_shared_7", (value) => appliedA.push(clone(value)));
  const pageB = newCard("card_shared_7", (value) => appliedB.push(clone(value)));
  const sidebar = newCard("card_shared_7");
  const favorite = newCard("card_shared_7");
  const review = newCard("card_shared_7");
  for (const card of [pageA, pageB, sidebar, favorite, review]) {
    h.api.pinReg(card, "card_shared_7");
  }
  h.api.pageReg(pageA, "card_shared_7");
  h.api.pageReg(pageB, "card_shared_7");

  const accepted = h.api.accept(
    "card_shared_7",
    { w: 486, h: 312, updatedAt: 101 },
    false,
  );
  assert.equal(JSON.stringify(accepted), JSON.stringify({
    w: 486,
    h: 312,
    updatedAt: 101,
  }));
  for (const card of [pageA, pageB]) {
    assert.equal(card.classList.contains("vc-user-sized"), true);
    assert.equal(card.classList.contains("vc-page-placement"), true);
    assert.equal(card.style.getPropertyValue("--vc-user-w"), "486px");
    assert.equal(card.style.getPropertyValue("--vc-user-h"), "312px");
  }
  for (const card of [sidebar, favorite, review]) {
    assert.equal(
      card.classList.contains("vc-user-sized"),
      false,
      "非页面投影不能继承页面专属尺寸",
    );
    assert.equal(card.style.getPropertyValue("--vc-user-w"), "");
    assert.equal(card.style.getPropertyValue("--vc-user-h"), "");
  }
  assert.equal(appliedA.at(-1).cid, "card_shared_7");
  assert.equal(appliedB.at(-1).cid, "card_shared_7");
  assert.equal(
    h.api.bind(sidebar, "card_shared_7", sidebar.body),
    null,
    "侧栏/收藏/复习不能绑定页面 resize 手势或生成把手",
  );
  assert.equal(sidebar.querySelector(".vc-card-rs"), null);
});

test("双击严格复用长按 pressTarget：鼠标与触屏正文可触发，外框及控件不可触发", () => {
  const h = resizeHarness({ extensionRecords: {} });
  const card = newCard("card_press_surface");
  h.api.pinReg(card, "card_press_surface");
  h.api.pageReg(card, "card_press_surface");
  h.api.bind(card, "card_press_surface", card.body);

  const header = new FakeElement("div");
  header.classList.add("vc-card-hd");
  card.appendChild(header);
  doubleTap(card, header);
  assert.equal(
    card.classList.contains("vc-resize-armed"),
    false,
    "pressTarget 外的卡头/边缘没有 resize 监听",
  );

  const face = new FakeElement("div");
  face.classList.add("fc-face");
  card.body.appendChild(face);
  assert.equal(h.api.eligible(face), true, "Anki 卡面属于正文长按面");
  doubleTap(card.body, face);
  assert.equal(
    card.classList.contains("vc-resize-armed"),
    true,
    "Anki 卡面鼠标双击应从与长按相同的正文面武装",
  );
  doubleTap(card.body, face);
  assert.equal(card.classList.contains("vc-resize-armed"), false);

  tap(card.body, face, {
    pointerType: "touch",
    pointerId: 11,
    leaveAfter: true,
  });
  const firstSyntheticClick = card.body.emit("click", {
    target: face,
    pointerType: "mouse",
  });
  assert.equal(card.classList.contains("vc-resize-armed"), false);
  assert.equal(
    firstSyntheticClick.defaultPrevented,
    false,
    "第一点后的兼容 click 仍保留卡面原有单击语义",
  );
  tap(card.body, face, {
    pointerType: "touch",
    pointerId: 12,
    leaveAfter: true,
  });
  const secondSyntheticClick = card.body.emit("click", {
    target: face,
    pointerType: "mouse",
  });
  assert.equal(
    card.classList.contains("vc-resize-armed"),
    true,
    "触屏的 pointerleave 与兼容 click 不能破坏双点计数",
  );
  assert.equal(
    secondSyntheticClick.defaultPrevented,
    true,
    "完成双点后的第二个兼容 click 必须被吞掉，避免重复触发卡面",
  );
  doubleTap(card.body, face, {
    pointerType: "touch",
    leaveAfterEach: true,
  });
  assert.equal(card.classList.contains("vc-resize-armed"), false);

  for (const [label, control] of [
    ["按钮", new FakeElement("button")],
    ["链接", new FakeElement("a")],
    ["分页点", new FakeElement("span")],
  ]) {
    if (label === "分页点") control.classList.add("fc-dot");
    card.body.appendChild(control);
    assert.equal(h.api.eligible(control), false, `${label}不属于长按/双击面`);
    doubleTap(card.body, control, { pointerType: "touch" });
    assert.equal(
      card.classList.contains("vc-resize-armed"),
      false,
      `${label}不能误触尺寸模式`,
    );
  }
});

test("缩放待命在卡外普通点击时立即退出，卡内与把手事件保持原行为", () => {
  const h = resizeHarness({ extensionRecords: {} });
  const card = newCard("card_outside_dismiss");
  h.api.pinReg(card, "card_outside_dismiss");
  h.api.pageReg(card, "card_outside_dismiss");
  h.api.bind(card, "card_outside_dismiss", card.body);
  const handle = card.querySelector(".vc-card-rs");
  const outside = new FakeElement("main");

  doubleTap(card.body);
  assert.equal(card.classList.contains("vc-resize-armed"), true);

  const insidePointer = h.emitDocument("pointerdown", {
    target: card.body,
    path: [card.body, card],
  });
  assert.equal(card.classList.contains("vc-resize-armed"), true);
  assert.equal(insidePointer.defaultPrevented, false);

  const handlePointer = h.emitDocument("pointerdown", {
    target: handle,
    path: [handle, card],
  });
  assert.equal(card.classList.contains("vc-resize-armed"), true);
  assert.equal(handlePointer.defaultPrevented, false);

  const outsidePointer = h.emitDocument("pointerdown", { target: outside });
  assert.equal(
    card.classList.contains("vc-resize-armed"),
    false,
    "卡外 pointerdown 应立即隐藏当前页面卡的把手",
  );
  assert.equal(
    outsidePointer.defaultPrevented,
    false,
    "退出待命不能吞掉宿主页原有点击",
  );

  doubleTap(card.body);
  assert.equal(card.classList.contains("vc-resize-armed"), true);
  const outsideClick = h.emitDocument("click", { target: outside });
  assert.equal(
    card.classList.contains("vc-resize-armed"),
    false,
    "非 pointer 的卡外 click 也应退出待命",
  );
  assert.equal(outsideClick.defaultPrevented, false);
});

test("双击武装把手，拖动实时同步页面副本并在抬手持久化单个 cid", async () => {
  const h = resizeHarness({
    extensionRecords: {
      card_other: { w: 240, h: 140, updatedAt: 1 },
    },
  });
  const card = newCard("card_resize_1");
  const sibling = newCard("card_resize_1");
  const sidebar = newCard("card_resize_1");
  h.api.pinReg(card, "card_resize_1");
  h.api.pinReg(sibling, "card_resize_1");
  h.api.pinReg(sidebar, "card_resize_1");
  h.api.pageReg(card, "card_resize_1");
  h.api.pageReg(sibling, "card_resize_1");
  h.api.bind(card, "card_resize_1", card.body);
  await tick();

  const control = new FakeElement("button");
  card.body.appendChild(control);
  doubleTap(card.body, control);
  assert.equal(
    card.classList.contains("vc-resize-armed"),
    false,
    "卡内按钮不能误触双击尺寸模式",
  );
  doubleTap(card.body);
  assert.equal(card.classList.contains("vc-resize-armed"), true);

  const handle = card.querySelector(".vc-card-rs");
  assert.ok(handle, "页面 placement 在绑定正文手势后拥有 resize handle");
  handle.emit("pointerdown", {
    pointerId: 77,
    clientX: 100,
    clientY: 100,
  });
  handle.emit("pointermove", {
    pointerId: 77,
    clientX: 1100,
    clientY: 1100,
  });
  assert.equal(
    h.extensionCalls.filter((call) => call.op === "set").length,
    0,
    "移动中只更新内存投影，不频繁写仓库",
  );
  h.flushRaf();
  assert.equal(card.style.getPropertyValue("--vc-user-w"), "720px");
  assert.equal(sibling.style.getPropertyValue("--vc-user-w"), "720px");
  assert.equal(card.style.getPropertyValue("--vc-user-h"), "720px");
  assert.equal(
    sidebar.style.getPropertyValue("--vc-user-w"),
    "",
    "同 cid 的侧栏投影不能跟随页面尺寸",
  );
  handle.emit("pointerup", {
    pointerId: 77,
    clientX: 1100,
    clientY: 1100,
  });
  await tick();
  await tick();

  const writes = h.extensionCalls.filter((call) => call.op === "set");
  assert.equal(writes.length, 1);
  assert.equal(writes[0].cid, "card_resize_1");
  assert.deepEqual(writes[0].value, {
    w: 720,
    h: 720,
    updatedAt: writes[0].value.updatedAt,
  });
  assert.deepEqual(h.extensionStored("card_other"), {
    w: 240,
    h: 140,
    updatedAt: 1,
  });
});

test("pointercancel 回滚到开始尺寸且不写仓库；尺寸与 cid 都 fail closed", async () => {
  const h = resizeHarness({ extensionRecords: {} });
  const card = newCard("card_cancel_1");
  const sibling = newCard("card_cancel_1");
  h.api.pinReg(card, "card_cancel_1");
  h.api.pinReg(sibling, "card_cancel_1");
  h.api.pageReg(card, "card_cancel_1");
  h.api.pageReg(sibling, "card_cancel_1");
  h.api.bind(card, "card_cancel_1", card.body);
  h.api.accept("card_cancel_1", { w: 280, h: 180, updatedAt: 10 }, false);
  card.rect = { left: 0, top: 0, width: 280, height: 180 };

  const handle = card.querySelector(".vc-card-rs");
  handle.emit("pointerdown", {
    pointerId: 88,
    clientX: 20,
    clientY: 20,
  });
  handle.emit("pointermove", {
    pointerId: 88,
    clientX: 120,
    clientY: 100,
  });
  h.flushRaf();
  assert.equal(sibling.style.getPropertyValue("--vc-user-w"), "380px");
  handle.emit("pointercancel", {
    pointerId: 88,
    clientX: 120,
    clientY: 100,
  });
  await tick();
  assert.equal(card.style.getPropertyValue("--vc-user-w"), "280px");
  assert.equal(sibling.style.getPropertyValue("--vc-user-h"), "180px");
  assert.equal(
    h.extensionCalls.filter((call) => call.op === "set").length,
    0,
  );

  for (const invalid of [
    { w: 179, h: 100, updatedAt: 1 },
    { w: 721, h: 100, updatedAt: 1 },
    { w: 180, h: 99, updatedAt: 1 },
    { w: 180, h: 721, updatedAt: 1 },
    { w: 180, h: 100, updatedAt: Number.NaN },
  ]) {
    assert.equal(h.api.record(invalid), null);
  }
  for (const invalidCid of ["", "__proto__", "constructor", "bad\u0000id"]) {
    assert.equal(h.api.cid(invalidCid), "");
  }
  await assert.rejects(
    h.api.persist("card_bad", { w: 900, h: 200, updatedAt: 1 }),
    /卡片尺寸无效/,
  );
});

test("页面刷新后从扩展逐 cid 本地仓恢复尺寸，非页面投影不恢复", async () => {
  const h = resizeHarness({
    extensionRecords: {
      card_reload_1: { w: 510, h: 330, updatedAt: 900 },
    },
  });
  const page = newCard("card_reload_1");
  const sidebar = newCard("card_reload_1");
  h.api.pinReg(page, "card_reload_1");
  h.api.pinReg(sidebar, "card_reload_1");
  h.api.pageReg(page, "card_reload_1");
  await h.api.load("card_reload_1");
  assert.equal(page.style.getPropertyValue("--vc-user-w"), "510px");
  assert.equal(page.style.getPropertyValue("--vc-user-h"), "330px");
  assert.equal(sidebar.style.getPropertyValue("--vc-user-w"), "");
  assert.deepEqual(
    h.extensionCalls.map((call) => [call.op, call.cid]),
    [["get", "card_reload_1"]],
  );
});

test("无扩展 PWA 使用 device ui-session 单记录回退并带 revision 围栏", async () => {
  const id = "page-card-presentation-v1:card_pwa_1";
  const h = resizeHarness({
    extensionRecords: false,
    pwaRecords: {
      [id]: {
        id,
        schema: 1,
        cid: "card_pwa_1",
        w: 360,
        h: 220,
        updatedAt: 10,
        rev: 4,
      },
    },
  });
  const card = newCard("card_pwa_1");
  h.api.pinReg(card, "card_pwa_1");
  h.api.pageReg(card, "card_pwa_1");
  await h.api.load("card_pwa_1");
  assert.equal(card.style.getPropertyValue("--vc-user-w"), "360px");

  await h.api.persist("card_pwa_1", {
    w: 420,
    h: 260,
    updatedAt: 20,
  });
  const put = h.pwaCalls.find((call) => call.op === "put");
  assert.ok(put);
  assert.equal(put.namespace, "ui-session");
  assert.equal(put.record.id, id);
  assert.equal(put.record.cid, "card_pwa_1");
  assert.equal(put.record.w, 420);
  assert.equal(put.record.h, 260);
  assert.equal(put.options.id, id);
  assert.equal(put.options.ifRev, 4);
  assert.match(put.options.mutationId, /^card-presentation-v1:/);
});

test("扩展后台动态校验 pageCardPresentationV1 安全边界且不依赖账户 namespace", () => {
  const start = BACKGROUND_SOURCE.indexOf(
    "function checkedPageCardPresentationCid",
  );
  const end = BACKGROUND_SOURCE.indexOf(
    "async function handleLocalStorageMessage",
    start,
  );
  assert.ok(start >= 0 && end > start);
  const sandbox = {};
  vm.runInNewContext(
    `${BACKGROUND_SOURCE.slice(start, end)}
this.checked = checkedPageCardPresentation;
this.checkedCid = checkedPageCardPresentationCid;
this.checkedRecord = checkedPageCardPresentationRecord;`,
    sandbox,
    { filename: BACKGROUND_PATH },
  );
  const valid = sandbox.checked({
    schema: 1,
    cards: {
      card_account_1: { w: 180, h: 100, updatedAt: 0 },
      card_account_2: { w: 720, h: 720, updatedAt: 999 },
    },
  });
  assert.equal(
    JSON.stringify(valid),
    JSON.stringify({
      schema: 1,
      cards: {
        card_account_1: { w: 180, h: 100, updatedAt: 0 },
        card_account_2: { w: 720, h: 720, updatedAt: 999 },
      },
    }),
  );
  assert.throws(
    () => sandbox.checked({
      schema: 1,
      cards: { "wp\u0000bad": { w: 200, h: 120, updatedAt: 1 } },
    }),
    /非法编号/,
  );
  assert.throws(
    () => sandbox.checked({
      schema: 1,
      cards: { card_bad: { w: 721, h: 120, updatedAt: 1 } },
    }),
    /安全范围/,
  );
  assert.match(
    BACKGROUND_SOURCE,
    /PAGE_CARD_PRESENTATION_STORAGE_KEY\s*=\s*"pageCardPresentationV1"/,
  );
  assert.doesNotMatch(
    BACKGROUND_SOURCE,
    /LOCAL_STORAGE_KEYS\s*=\s*new Set\(\[[\s\S]*"cardPresentationV1"[\s\S]*\]\)/,
    "页面尺寸不再走要求账户租约的通用本地键",
  );
  const handlerStart = BACKGROUND_SOURCE.indexOf(
    "async function handlePageCardPresentationMessage",
  );
  const handlerEnd = BACKGROUND_SOURCE.indexOf(
    "async function handleTranslationCacheMessage",
    handlerStart,
  );
  assert.ok(handlerStart >= 0 && handlerEnd > handlerStart);
  const handler = BACKGROUND_SOURCE.slice(handlerStart, handlerEnd);
  assert.match(
    handler,
    /isTopLevelOwnContentSender\(sender\)/,
    "页面桥仍须拒绝 iframe/外部 sender",
  );
  assert.doesNotMatch(
    handler,
    /capturePersistentAccountForContentSender|namespace/,
    "页面 placement 尺寸是本机设备状态，普通网页无账户时也必须可保存",
  );
});

test("双击与长按由同一 pressTarget 和同一 eligibility 谓词控制", () => {
  const bindStart = VOICE_SOURCE.indexOf("function _pinBind");
  const bindEnd = VOICE_SOURCE.indexOf("function _pinLongpress", bindStart);
  const pinBindSource = VOICE_SOURCE.slice(bindStart, bindEnd);
  assert.match(
    pinBindSource,
    /if \(!_cardPressEligible\(ev\.target\)\) return;/,
    "长按必须复用统一的控件排除谓词",
  );
  assert.match(
    pinBindSource,
    /_cardResizeBind\(el,\s*_bindCid,\s*pressTarget\)/,
    "双击必须绑定到长按收到的同一个 pressTarget",
  );
  const eligibilityStart = VOICE_SOURCE.indexOf("function _cardPressEligible");
  const eligibilityEnd = VOICE_SOURCE.indexOf(
    "function _cardResizeArm",
    eligibilityStart,
  );
  const eligibility = VOICE_SOURCE.slice(eligibilityStart, eligibilityEnd);
  assert.match(eligibility, /\.fc-dot/);
  assert.doesNotMatch(
    eligibility,
    /\.fc-face|\[data-fc\]/,
    "Anki 正文/卡面不能被排除在共同选择面之外",
  );
  assert.match(
    VOICE_SOURCE,
    /\.vc-card\.vc-page-placement:not\(\.vc-dot\):not\(\.vc-min\)>\.vc-card-bd\{touch-action:manipulation\}/,
    "页面卡正文须保留滚动并避免触屏双点被浏览器缩放抢走",
  );
});

test("web/PWA 页面 placement wrapper 只接收尺寸回调，不把尺寸写入 placement", () => {
  assert.match(
    WEB_PINS_SOURCE,
    /const onSize=size=>\{box\.style\.width=size\?\(Math\.max\(180,Math\.min\(720,Number\(size\.w\)\|\|360\)\)\+'px'\):'';\};/,
  );
  assert.equal(
    (WEB_PINS_SOURCE.match(/cid:p\.cid,onSize/g) || []).length,
    2,
    "网页 HTML/Anki 两种 wrapper 都必须传稳定实体 cid 和 onSize",
  );
  assert.doesNotMatch(
    WEB_PINS_SOURCE,
    /p\.(?:id|placementId)\s*=\s*size|size\s*:\s*p\.(?:id|placementId)/,
    "页面 placement 只保存位置/锚点，不能成为尺寸实体键",
  );

  assert.equal(
    (STICKY_SOURCE.match(/ctl\._cardPresentationSize = size \|\| null/g) || [])
      .length,
    2,
    "PWA Anki/HTML wrapper 都消费共享卡的 onSize",
  );
  assert.match(
    STICKY_SOURCE,
    /function _formW\(ctl, f\)[\s\S]*ctl\._cardPresentationSize[\s\S]*Math\.max\(180, Math\.min\(720, Math\.round\(ctl\._cardPresentationSize\.w\)\)\)/,
  );
  assert.match(
    STICKY_SOURCE,
    /cid: card\.cid \|\| card\.gid,[\s\S]*onSize: function \(size\)/,
  );
  assert.match(
    STICKY_SOURCE,
    /cid: h\.cid,[\s\S]*onSize: function \(size\)/,
  );
});

test("钉在书页上的卡只有 标记 ⇄ 完全展开 两态，不进长条", () => {
  // 用户 2026-08-18：「固定到页面上的卡片其实很少用到长条形态……固定后应该默认
  // 只保留球和最终完全展开两种形态」。理由站得住：长条的作用是"在一堆卡里给个
  // 大概的信息概要"，而钉住的卡**锚点本身就说明了它是关于什么的**，概要与锚点重复。
  //
  // 实现上是在 _cardForm 里按宿主裁剪形态，跟内联卡跳过圆点是同一手法 ——
  // 不给每个宿主另造一套状态机。
  const source = VOICE_SOURCE;
  assert.match(
    source,
    /if \(f === 'min' && el\.classList\.contains\('vc-pinned'\)\) f = 'full';/,
    "钉入卡的长条态必须在形态写入处被裁掉，而不是靠 CSS 藏起来",
  );
  // _cardDom 直接写 class、不经过 _cardForm，所以历史存过 min 的钉入卡
  // 必须在打上 vc-pinned 之后再归一一次，否则恢复出来还是长条。
  const pinAt = source.indexOf("el.classList.add('vc-pinned');");
  assert.ok(pinAt > 0);
  assert.match(
    source.slice(pinAt, pinAt + 400),
    /if \(_cardForm\(el\) === 'min'\) _cardForm\(el, 'full'\);/,
  );
  // 长条本身没删：浮层卡与侧栏内联卡仍在用它。
  assert.match(source, /\.vc-card\.vc-min \.vc-card-bd\{display:none\}/);
});
