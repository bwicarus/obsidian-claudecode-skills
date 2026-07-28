import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "../..");
const SOURCE_PATH = path.join(
  ROOT,
  "_server_deploy/static/pdf/rc-stickynote.js",
);
const SOURCE = fs.readFileSync(SOURCE_PATH, "utf8");

class FakeClassList {
  constructor() {
    this.values = new Set();
  }
  add(...names) {
    names.forEach((name) => this.values.add(name));
  }
  remove(...names) {
    names.forEach((name) => this.values.delete(name));
  }
  contains(name) {
    return this.values.has(name);
  }
  toggle(name, force) {
    const enabled = force === undefined ? !this.values.has(name) : !!force;
    if (enabled) this.values.add(name);
    else this.values.delete(name);
    return enabled;
  }
}

class FakeTarget {
  constructor() {
    this.listeners = new Map();
    this.captures = new Set();
    this.classList = new FakeClassList();
    this.style = {};
  }
  addEventListener(type, fn) {
    const rows = this.listeners.get(type) || [];
    rows.push(fn);
    this.listeners.set(type, rows);
  }
  removeEventListener(type, fn) {
    this.listeners.set(
      type,
      (this.listeners.get(type) || []).filter((row) => row !== fn),
    );
  }
  emit(type, values = {}) {
    const event = {
      type,
      pointerId: values.pointerId,
      pointerType: values.pointerType || "touch",
      button: values.button,
      clientX: values.clientX ?? 0,
      clientY: values.clientY ?? 0,
      target: values.target || this,
      currentTarget: this,
      defaultPrevented: false,
      preventDefault() {
        this.defaultPrevented = true;
      },
    };
    for (const fn of [...(this.listeners.get(type) || [])]) {
      fn.call(this, event);
    }
    return event;
  }
  setPointerCapture(pointerId) {
    this.captures.add(pointerId);
  }
  hasPointerCapture(pointerId) {
    return this.captures.has(pointerId);
  }
  releasePointerCapture(pointerId) {
    this.captures.delete(pointerId);
  }
}

function sourceBetween(startText, endText) {
  const start = SOURCE.indexOf(startText);
  const end = SOURCE.indexOf(endText, start);
  assert.ok(start >= 0 && end > start, `${startText} must be extractable`);
  return SOURCE.slice(start, end);
}

function loadHarness() {
  const document = new FakeTarget();
  const ui = {
    anchorShown: false,
    trashShown: false,
    trashHot: false,
    favoriteShown: false,
    drops: [],
    edits: 0,
  };
  const RC = {
    voiceCard: {
      trash: {
        show(value) {
          ui.trashShown = !!value;
        },
        hot(value) {
          ui.trashHot = !!value;
        },
        inZone() {
          return false;
        },
      },
      favorite: {
        hint(value) {
          ui.favoriteShown = !!value;
        },
        inZone() {
          return false;
        },
      },
    },
  };
  const rootTapSource = sourceBetween(
    "function onRootUp(ctl, e)",
    "function toggleCollapsed(ctl)",
  );
  const handleSource = sourceBetween(
    "function _isCardNote(ctl)",
    "function reanchorAt(ctl, px, py)",
  );
  const sandbox = {
    Math,
    Date,
    setTimeout,
    clearTimeout,
    document,
    navigator: { vibrate() {} },
    window: { RC },
    RC,
    MouseEvent: class {
      constructor(type, options = {}) {
        this.type = type;
        Object.assign(this, options);
      }
    },
  };
  const context = { ...sandbox, __ui: ui };
  vm.runInNewContext(
    `
      var _hd = null, EDIT = null;
      var draw = null, _rz = null, _rzTL = null;
      var CARD_DRAG_HOLD_MS = 18, CARD_DRAG_TOL = 8;
      var LP_TOL = 10, TAP_TOL = 10, TAP_WIN = 380;
      var O = { onDoubleTap: function () {} };
      function lpMs() { return 18; }
      function portalIn() {}
      function anchorFxShow() { __ui.anchorShown = true; }
      function anchorFxHide() { __ui.anchorShown = false; }
      function dropNote(ctl) { __ui.drops.push(ctl); ctl.root.style.transform = ''; }
      function deleteNote() {}
      function cardContextText() { return ''; }
      function saveText() {}
      function enterEdit() { __ui.edits += 1; }
      function toggleCollapsed() {}
      ${rootTapSource}
      ${handleSource}
      this.__api = {
        down: onHandleDown,
        cancel: cancelHandleGesture,
        rootUp: onRootUp,
        state: function () { return _hd; },
        setEdit: function (value) { EDIT = value; }
      };
    `,
    context,
    { filename: SOURCE_PATH },
  );
  return { api: context.__api, document, ui };
}

function controller(kind = "card") {
  const handle = new FakeTarget();
  const root = new FakeTarget();
  root.offsetWidth = 300;
  root.style.transform = "";
  root.getBoundingClientRect = () => ({
    left: 100,
    top: 80,
    width: 300,
    height: 210,
  });
  let shapeClicks = 0;
  const shapeTarget = {
    dispatchEvent() {
      shapeClicks += 1;
    },
  };
  const card = {
    classList: new FakeClassList(),
    querySelector() {
      return shapeTarget;
    },
  };
  const body = {
    querySelector() {
      return kind === "card" ? card : null;
    },
  };
  return {
    note: kind === "card" ? { card: { cards: [{}] } } : { text: "note" },
    handle,
    root,
    body,
    get shapeClicks() {
      return shapeClicks;
    },
  };
}

function pointer(pointerId, x = 10, y = 10) {
  return {
    pointerId,
    pointerType: "touch",
    button: 0,
    clientX: x,
    clientY: y,
    preventDefault() {},
  };
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

test("PWA 卡头只归首个 pointer，外来 move/up/cancel 不得抢占或落卡", async () => {
  const h = loadHarness();
  const first = controller("card");
  const second = controller("card");

  h.api.down(first, pointer(101, 10, 10));
  assert.equal(h.api.state().pointerId, 101);
  assert.equal(first.handle.hasPointerCapture(101), true);

  h.api.down(second, pointer(202, 30, 30));
  assert.equal(h.api.state().ctl, first, "second pointer must not replace owner");
  assert.equal(second.handle.hasPointerCapture(202), false);

  h.document.emit("pointermove", {
    pointerId: 202,
    clientX: 180,
    clientY: 180,
  });
  h.document.emit("pointerup", {
    pointerId: 202,
    clientX: 180,
    clientY: 180,
  });
  assert.equal(h.api.state().pointerId, 101);

  await wait(26);
  assert.equal(h.api.state().dragging, true);
  assert.equal(first.root.classList.contains("rc-card-drag-ready"), true);

  h.document.emit("pointermove", {
    pointerId: 101,
    clientX: 42,
    clientY: 26,
  });
  assert.match(first.root.style.transform, /translate/);
  h.document.emit("pointercancel", {
    pointerId: 202,
    clientX: 42,
    clientY: 26,
  });
  assert.ok(h.api.state(), "foreign cancel must not rollback active gesture");

  h.document.emit("pointercancel", {
    pointerId: 101,
    clientX: 42,
    clientY: 26,
  });
  assert.equal(h.api.state(), null);
  assert.equal(first.handle.hasPointerCapture(101), false);
  assert.equal(first.root.style.transform, "");
  assert.equal(first.root.classList.contains("rc-note-lift"), false);
  assert.equal(first.root.classList.contains("rc-card-drag-ready"), false);
  assert.equal(h.ui.anchorShown, false);
  assert.equal(h.ui.trashShown, false);
  assert.equal(h.ui.trashHot, false);
  assert.equal(h.ui.favoriteShown, false);
  assert.equal(h.ui.drops.length, 0, "cancel never commits a drop");
});

test("PWA 卡头正常结束和外部 teardown 都释放捕获并清理暂态", async () => {
  const h = loadHarness();
  const card = controller("card");
  h.api.down(card, pointer(301, 10, 10));
  await wait(26);
  h.document.emit("pointermove", {
    pointerId: 301,
    clientX: 50,
    clientY: 35,
  });
  h.document.emit("pointerup", {
    pointerId: 301,
    clientX: 50,
    clientY: 35,
  });
  assert.equal(h.api.state(), null);
  assert.equal(card.handle.hasPointerCapture(301), false);
  assert.equal(h.ui.drops.length, 1);
  assert.equal(h.ui.anchorShown, false);
  assert.equal(h.ui.trashShown, false);
  assert.equal(h.ui.favoriteShown, false);

  const another = controller("card");
  h.api.down(another, pointer(302, 10, 10));
  await wait(26);
  h.document.emit("pointermove", {
    pointerId: 302,
    clientX: 45,
    clientY: 30,
  });
  h.api.cancel();
  assert.equal(h.api.state(), null);
  assert.equal(another.handle.hasPointerCapture(302), false);
  assert.equal(another.root.style.transform, "");
  assert.equal(another.root.classList.contains("rc-note-lift"), false);
  assert.equal(h.ui.anchorShown, false);
  assert.equal(h.ui.trashShown, false);
  assert.equal(h.ui.trashHot, false);
  assert.equal(h.ui.favoriteShown, false);
});

test("PWA 卡片不进入旧 EDIT，遗留 EDIT 也不能绕过 420ms 蓄力", async () => {
  const h = loadHarness();
  const card = controller("card");

  for (let i = 0; i < 2; i += 1) {
    card._downPt = { x: 20, y: 20 };
    h.api.rootUp(card, {
      clientX: 20,
      clientY: 20,
      target: { closest: () => null },
    });
  }
  assert.equal(h.ui.edits, 0, "card double tap belongs to the card, not note EDIT");

  const note = controller("note");
  for (let i = 0; i < 2; i += 1) {
    note._downPt = { x: 20, y: 20 };
    h.api.rootUp(note, {
      clientX: 20,
      clientY: 20,
      target: { closest: () => null },
    });
  }
  assert.equal(h.ui.edits, 1, "ordinary sticky-note double tap remains intact");

  h.api.setEdit({ ctl: card });
  h.api.down(card, pointer(401, 10, 10));
  assert.equal(h.api.state().dragging, false);
  assert.equal(card.root.classList.contains("rc-card-drag-charging"), true);
  await wait(26);
  assert.equal(h.api.state().dragging, true);
  h.api.cancel();

  const editableNote = controller("note");
  h.api.setEdit({ ctl: editableNote });
  h.api.down(editableNote, pointer(402, 10, 10));
  assert.equal(
    h.api.state().dragging,
    true,
    "ordinary note EDIT keeps its immediate-drag behavior",
  );
  h.api.cancel();
});

test("PWA lostpointercapture forwards the event into pointer ownership filtering", () => {
  assert.match(
    SOURCE,
    /addEventListener\('lostpointercapture', function \(e\) \{[\s\S]*onHandleCancel\(e\)/,
  );
  assert.match(
    SOURCE,
    /_hd\s*=\s*\{[\s\S]*pointerId:\s*e\.pointerId,\s*captureEl:\s*ctl\.handle/,
  );
  assert.match(
    SOURCE,
    /function onHandleMove\(e\) \{\s*if \(!_hd \|\| !sameHandlePointer\(_hd, e\)\) return;/,
  );
  assert.match(
    SOURCE,
    /function onHandleUp\(e\) \{\s*var g = _hd; if \(!g \|\| !sameHandlePointer\(g, e\)\) return;/,
  );
});
