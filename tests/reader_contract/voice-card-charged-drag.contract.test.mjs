import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "../..");
const VOICE_PATH = path.join(
  ROOT,
  "_server_deploy/static/pdf/rc-voicecall.js",
);
const SOURCE = fs.readFileSync(VOICE_PATH, "utf8");

class FakeClassList {
  constructor() {
    this.names = new Set();
  }
  add(...names) {
    names.forEach((name) => this.names.add(name));
  }
  remove(...names) {
    names.forEach((name) => this.names.delete(name));
  }
  contains(name) {
    return this.names.has(name);
  }
  toggle(name, force) {
    const on = force === undefined ? !this.names.has(name) : !!force;
    if (on) this.names.add(name);
    else this.names.delete(name);
    return on;
  }
}

class FakeTarget {
  constructor(ownerDocument = null) {
    this.ownerDocument = ownerDocument;
    this.isConnected = true;
    this.style = { touchAction: "" };
    this.classList = new FakeClassList();
    this.listeners = new Map();
    this.captures = new Set();
  }
  addEventListener(type, fn, options) {
    const capture = options === true || !!options?.capture;
    const rows = this.listeners.get(type) || [];
    rows.push({ fn, capture });
    this.listeners.set(type, rows);
  }
  removeEventListener(type, fn, options) {
    const capture = options === true || !!options?.capture;
    const rows = (this.listeners.get(type) || []).filter(
      (row) => row.fn !== fn || row.capture !== capture,
    );
    this.listeners.set(type, rows);
  }
  emit(type, values = {}) {
    const event = {
      type,
      target: values.target || this,
      currentTarget: this,
      pointerId: values.pointerId,
      pointerType: values.pointerType || "touch",
      button: values.button,
      clientX: values.clientX ?? 0,
      clientY: values.clientY ?? 0,
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
    for (const row of [...(this.listeners.get(type) || [])]) {
      row.fn.call(this, event);
      if (event.immediateStopped) break;
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

function loadHelper() {
  const start = SOURCE.indexOf("var _activeChargedDrag");
  const end = SOURCE.indexOf("function _dragToDock", start);
  assert.ok(start >= 0 && end > start, "charged-drag helper must be extractable");
  const sandbox = {
    Math,
    Date,
    setTimeout,
    clearTimeout,
    document: {},
    window: {},
  };
  vm.runInNewContext(
    `${SOURCE.slice(start, end)}\nthis.bindChargedDrag = _bindChargedDrag;`,
    sandbox,
    { filename: VOICE_PATH },
  );
  return sandbox.bindChargedDrag;
}

function fixture() {
  const win = new FakeTarget();
  win.navigator = {};
  const doc = new FakeTarget();
  doc.defaultView = win;
  doc.hidden = false;
  const handle = new FakeTarget(doc);
  const feedback = new FakeTarget(doc);
  return { win, doc, handle, feedback };
}

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

test("shared charged drag keeps short taps and pre-charge slop inert", async () => {
  const bind = loadHelper();

  {
    const { doc, handle, feedback } = fixture();
    const seen = [];
    const controller = bind(handle, {
      holdMs: 18,
      slop: 8,
      feedbackEl: feedback,
      onReady: () => seen.push("ready"),
      onEnd: () => seen.push("end"),
      onCancel: (_session, _event, reason) => seen.push(reason),
    });
    handle.emit("pointerdown", {
      pointerId: 11,
      button: 0,
      clientX: 20,
      clientY: 30,
    });
    assert.equal(controller.isActive(), true);
    doc.emit("pointerup", { pointerId: 11, clientX: 20, clientY: 30 });
    await wait(25);
    const click = handle.emit("click");
    assert.deepEqual(seen, []);
    assert.equal(click.defaultPrevented, false, "a genuine short click survives");
    assert.equal(controller.isActive(), false);
    assert.equal(handle.hasPointerCapture(11), false);
    assert.equal(feedback.classList.contains("vc-drag-charging"), false);
  }

  {
    const { doc, handle, feedback } = fixture();
    const seen = [];
    const controller = bind(handle, {
      holdMs: 18,
      slop: 8,
      feedbackEl: feedback,
      onReady: () => seen.push("ready"),
      onCancel: (_session, _event, reason) => seen.push(reason),
    });
    handle.emit("pointerdown", {
      pointerId: 12,
      button: 0,
      clientX: 0,
      clientY: 0,
    });
    doc.emit("pointermove", { pointerId: 12, clientX: 9, clientY: 0 });
    await wait(25);
    assert.deepEqual(seen, ["slop"]);
    assert.equal(controller.isActive(), false);
    assert.equal(feedback.classList.contains("vc-drag-ready"), false);
    const syntheticClick = handle.emit("click");
    const nextClick = handle.emit("click");
    assert.equal(
      syntheticClick.defaultPrevented,
      true,
      "movement beyond pre-charge slop is not a short tap",
    );
    assert.equal(
      nextClick.defaultPrevented,
      false,
      "pre-charge slop suppression is one-shot",
    );
  }
});

test("ready drag owns one pointer, commits on pointerup, and consumes one click", async () => {
  const bind = loadHelper();
  const { doc, handle, feedback } = fixture();
  const seen = [];
  let ended = null;
  const controller = bind(handle, {
    holdMs: 18,
    slop: 8,
    dragSlop: 1,
    feedbackEl: feedback,
    onReady: () => seen.push("ready"),
    onMove: (session) => seen.push(`move:${session.dx},${session.dy}`),
    onEnd: (session) => {
      seen.push("end");
      ended = session;
    },
    onCancel: (_session, _event, reason) => seen.push(`cancel:${reason}`),
  });

  handle.emit("pointerdown", {
    pointerId: 21,
    button: 0,
    clientX: 10,
    clientY: 10,
  });
  await wait(25);
  assert.equal(feedback.classList.contains("vc-drag-ready"), true);

  doc.emit("pointermove", { pointerId: 99, clientX: 80, clientY: 80 });
  doc.emit("pointerup", { pointerId: 99, clientX: 80, clientY: 80 });
  assert.equal(controller.isActive(), true, "foreign pointers cannot end the drag");

  doc.emit("pointermove", { pointerId: 21, clientX: 22, clientY: 15 });
  doc.emit("pointerup", { pointerId: 21, clientX: 22, clientY: 15 });
  assert.deepEqual(seen, ["ready", "move:12,5", "end"]);
  assert.equal(ended.moved, true);
  assert.equal(handle.hasPointerCapture(21), false);
  assert.equal(feedback.classList.contains("vc-drag-ready"), false);

  const syntheticClick = handle.emit("click");
  const nextClick = handle.emit("click");
  assert.equal(syntheticClick.defaultPrevented, true);
  assert.equal(syntheticClick.immediateStopped, true);
  assert.equal(nextClick.defaultPrevented, false, "suppression is one-shot");
});

test("pointercancel, lost capture, blur, and hidden state use rollback only", async () => {
  const bind = loadHelper();
  for (const reason of ["pointercancel", "lostpointercapture", "blur", "hidden"]) {
    const { win, doc, handle } = fixture();
    const seen = [];
    const controller = bind(handle, {
      holdMs: 12,
      onReady: () => seen.push("ready"),
      onEnd: () => seen.push("end"),
      onCancel: (_session, _event, why) => seen.push(`cancel:${why}`),
    });
    handle.emit("pointerdown", {
      pointerId: 31,
      button: 0,
      clientX: 2,
      clientY: 3,
    });
    await wait(18);
    if (reason === "pointercancel") {
      doc.emit("pointercancel", { pointerId: 31, clientX: 2, clientY: 3 });
    } else if (reason === "lostpointercapture") {
      handle.emit("lostpointercapture", { pointerId: 31 });
    } else if (reason === "blur") {
      win.emit("blur");
    } else {
      doc.hidden = true;
      doc.emit("visibilitychange");
    }
    assert.deepEqual(seen, ["ready", `cancel:${reason}`]);
    assert.equal(controller.isActive(), false);
    assert.equal(seen.includes("end"), false);
  }
});

test("a detached owner or handle releases only its stale global drag lock", () => {
  for (const detached of ["handle", "owner"]) {
    const bind = loadHelper();
    const first = fixture();
    const second = fixture();
    const seen = [];
    const firstController = bind(first.handle, {
      holdMs: 200,
      feedbackEl: first.feedback,
      onEnd: () => seen.push("end"),
      onCancel: (_session, _event, reason) => seen.push(reason),
    });
    const secondController = bind(second.handle, {
      holdMs: 200,
      feedbackEl: second.feedback,
    });

    first.handle.emit("pointerdown", {
      pointerId: 51,
      button: 0,
      clientX: 2,
      clientY: 3,
    });
    assert.equal(firstController.isActive(), true);
    if (detached === "handle") first.handle.isConnected = false;
    else first.feedback.isConnected = false;

    second.handle.emit("pointerdown", {
      pointerId: 52,
      button: 0,
      clientX: 4,
      clientY: 5,
    });
    assert.equal(firstController.isActive(), false);
    assert.equal(secondController.isActive(), true);
    assert.deepEqual(seen, ["stale-active"]);
    assert.equal(
      seen.includes("end"),
      false,
      "stale cleanup must roll back rather than commit a drop",
    );
    second.doc.emit("pointerup", {
      pointerId: 52,
      clientX: 4,
      clientY: 5,
    });
  }
});

test("a session that lost runtime listeners is reapable, but a live drag is not", async () => {
  {
    const bind = loadHelper();
    const current = fixture();
    const seen = [];
    const controller = bind(current.handle, {
      holdMs: 200,
      feedbackEl: current.feedback,
      onCancel: (_session, _event, reason) => seen.push(reason),
    });
    current.handle.emit("pointerdown", {
      pointerId: 49,
      button: 0,
      clientX: 1,
      clientY: 1,
    });
    // Same binding, same browser pointerId, but a new pointerdown event:
    // the previous up/cancel was lost and local state must not deadlock it.
    current.handle.emit("pointerdown", {
      pointerId: 49,
      button: 0,
      clientX: 2,
      clientY: 2,
    });
    assert.equal(controller.isActive(), true);
    assert.deepEqual(seen, ["stale-active"]);
    current.doc.emit("pointerup", {
      pointerId: 49,
      clientX: 2,
      clientY: 2,
    });
  }

  {
    const bind = loadHelper();
    const first = fixture();
    const second = fixture();
    let firstSession = null;
    const seen = [];
    const firstController = bind(first.handle, {
      holdMs: 1,
      feedbackEl: first.feedback,
      onReady: (session) => {
        firstSession = session;
      },
      onCancel: (_session, _event, reason) => seen.push(reason),
    });
    const secondController = bind(second.handle, {
      holdMs: 200,
      feedbackEl: second.feedback,
    });
    first.handle.emit("pointerdown", {
      pointerId: 61,
      button: 0,
      clientX: 1,
      clientY: 1,
    });
    await wait(8);
    assert.ok(firstSession);
    firstSession.runtimeAttached = false;

    second.handle.emit("pointerdown", {
      pointerId: 62,
      button: 0,
      clientX: 2,
      clientY: 2,
    });
    assert.equal(firstController.isActive(), false);
    assert.equal(secondController.isActive(), true);
    assert.deepEqual(seen, ["stale-active"]);
    second.doc.emit("pointerup", {
      pointerId: 62,
      clientX: 2,
      clientY: 2,
    });
  }

  {
    const bind = loadHelper();
    const first = fixture();
    const second = fixture();
    const seen = [];
    const firstController = bind(first.handle, {
      holdMs: 200,
      feedbackEl: first.feedback,
      onCancel: (_session, _event, reason) => seen.push(reason),
    });
    const secondController = bind(second.handle, {
      holdMs: 200,
      feedbackEl: second.feedback,
    });
    first.handle.emit("pointerdown", {
      pointerId: 81,
      button: 0,
      clientX: 3,
      clientY: 3,
    });
    // Simulate a connected host that lost the previous pointerup/cancel.
    // A new pointerdown with the same browser pointerId proves that the old
    // active-pointer lifecycle has already ended.
    second.handle.emit("pointerdown", {
      pointerId: 81,
      button: 0,
      clientX: 4,
      clientY: 4,
    });
    assert.equal(firstController.isActive(), false);
    assert.equal(secondController.isActive(), true);
    assert.deepEqual(seen, ["stale-active"]);
    second.doc.emit("pointerup", {
      pointerId: 81,
      clientX: 4,
      clientY: 4,
    });
  }

  {
    const bind = loadHelper();
    const first = fixture();
    const second = fixture();
    const firstController = bind(first.handle, {
      holdMs: 200,
      feedbackEl: first.feedback,
    });
    const secondController = bind(second.handle, {
      holdMs: 200,
      feedbackEl: second.feedback,
    });
    first.handle.emit("pointerdown", {
      pointerId: 71,
      button: 0,
      clientX: 3,
      clientY: 3,
    });
    second.handle.emit("pointerdown", {
      pointerId: 72,
      button: 0,
      clientX: 4,
      clientY: 4,
    });
    assert.equal(firstController.isActive(), true);
    assert.equal(
      secondController.isActive(),
      false,
      "a connected drag with live listeners retains global ownership",
    );

    first.doc.emit("pointerup", {
      pointerId: 71,
      clientX: 3,
      clientY: 3,
    });
    second.handle.emit("pointerdown", {
      pointerId: 72,
      button: 0,
      clientX: 4,
      clientY: 4,
    });
    assert.equal(secondController.isActive(), true);
    second.doc.emit("pointerup", {
      pointerId: 72,
      clientX: 4,
      clientY: 4,
    });
  }
});

test("pinBind replaces an owner's old whole-card target with one body target", () => {
  const start = SOURCE.indexOf("function _pinBind");
  const end = SOURCE.indexOf("function _imgGoneNote", start);
  assert.ok(start >= 0 && end > start, "pinBind helper must be extractable");
  let nextTimer = 1;
  const timers = new Map();
  const toggles = [];
  const sandbox = {
    setTimeout(fn) {
      const id = nextTimer++;
      timers.set(id, fn);
      return id;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    _pinContextId() {},
    _pinToggle(_el, label, textFn, spec) {
      toggles.push({ label, text: textFn(), spec });
    },
    _cardPressEligible() {
      return true;
    },
  };
  vm.runInNewContext(
    `${SOURCE.slice(start, end)}\nthis.pinBind = _pinBind;`,
    sandbox,
    { filename: VOICE_PATH },
  );

  const owner = new FakeTarget();
  owner.dataset = { vcCid: "card-1" };
  owner.closest = () => null;
  const body = new FakeTarget();
  body.closest = () => null;
  const first = sandbox.pinBind(
    owner,
    "old whole card",
    () => "old",
    { id: "card:1" },
    owner,
  );
  assert.equal((owner.listeners.get("pointerdown") || []).length, 1);

  const second = sandbox.pinBind(
    owner,
    "new body",
    () => "new",
    { id: "card:1", host: "page" },
    body,
  );
  assert.notEqual(second, first);
  assert.equal(
    (owner.listeners.get("pointerdown") || []).length,
    0,
    "the obsolete whole-card timer must be detached",
  );
  assert.equal((body.listeners.get("pointerdown") || []).length, 1);
  assert.equal(owner.__bwPinHoldBindings.length, 1);

  const repeated = sandbox.pinBind(
    owner,
    "latest body",
    () => "latest",
    { id: "card:1", host: "updated" },
    body,
  );
  assert.equal(repeated, second);
  assert.equal(
    (body.listeners.get("pointerdown") || []).length,
    1,
    "same owner+body rebind only updates payload",
  );

  body.emit("pointerdown", {
    target: body,
    pointerId: 41,
    button: 0,
    clientX: 5,
    clientY: 6,
  });
  for (const [id, fn] of [...timers]) {
    timers.delete(id);
    fn();
  }
  assert.deepEqual(toggles, [
    {
      label: "latest body",
      text: "latest",
      spec: { id: "card:1", host: "updated" },
    },
  ]);
});

test("voice-card public API and both drag hosts use the shared helper", () => {
  assert.match(
    SOURCE,
    /bindChargedDrag:\s*_bindChargedDrag/,
    "the shared helper must be public for web placements",
  );
  assert.match(
    SOURCE,
    /function _pinBind\(el, label, textFn, spec, pressTarget\)/,
  );
  assert.match(
    SOURCE,
    /__bwPinHoldBindings[\s\S]*pressTarget === pressTarget[\s\S]*return pinBindings\[pbi\]/,
    "repeated owner+pressTarget binding must update rather than stack listeners",
  );
  assert.match(
    SOURCE,
    /pinBindings\.slice\(\)\.forEach[\s\S]*oldBinding\.destroy\(\)/,
    "changing an owner's pressTarget must detach the obsolete nested listener",
  );
  assert.match(SOURCE, /suppressClickUntil[\s\S]*stopImmediatePropagation/);
  assert.ok(
    (SOURCE.match(/_bindChargedDrag\(hd,\s*\{/g) || []).length >= 2,
    "sidebar drag-out and floating-card drag must share the charged helper",
  );
  assert.match(SOURCE, /vc-drag-charging/);
  assert.match(SOURCE, /vc-drag-ready/);
});
