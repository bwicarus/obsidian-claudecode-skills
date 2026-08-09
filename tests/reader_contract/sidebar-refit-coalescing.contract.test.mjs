import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const DRAWER = read("_server_deploy/static/pdf/rc-sidedrawer.js");
const NAV = read("_server_deploy/static/pdf/reader.src/05-nav.js");
const LAYOUT = read("_server_deploy/static/pdf/reader.src/06-layout.js");
const CONTINUOUS = read("_server_deploy/static/pdf/reader.src/07-continuous.js");

function section(source, start, end) {
  const from = source.indexOf(start);
  const to = source.indexOf(end, from + start.length);
  assert.notEqual(from, -1, `missing ${start}`);
  assert.notEqual(to, -1, `missing ${end}`);
  return source.slice(from, to);
}

function fakeTimers() {
  let nextId = 1;
  const pending = new Map();
  return {
    pending,
    setTimeout(fn) { const id = nextId++; pending.set(id, fn); return id; },
    clearTimeout(id) { pending.delete(id); },
    runAll() {
      const work = [...pending.values()];
      pending.clear();
      for (const fn of work) fn();
    },
  };
}

test("侧栏宽度与滑入过渡只预览 CSS，commit/transitionend 才各通知一次", () => {
  const timers = fakeTimers();
  const sideListeners = new Map();
  const side = {
    addEventListener(type, fn) { sideListeners.set(type, fn); },
    removeEventListener(type, fn) { if (sideListeners.get(type) === fn) sideListeners.delete(type); },
  };
  const classes = new Set();
  const sandbox = {
    CustomEvent: class CustomEvent { constructor(type, init) { this.type = type; this.detail = init.detail; } },
    Event: class Event { constructor(type) { this.type = type; } },
    Math,
    clearTimeout: timers.clearTimeout,
    setTimeout: timers.setTimeout,
    document: {
      documentElement: {
        classList: { toggle(name, on) { if (on) classes.add(name); else classes.delete(name); } },
        style: { setProperty() {} },
      },
      getElementById(id) { return id === "ep-side" ? side : null; },
    },
    dispatched: [],
  };
  sandbox.window = sandbox;
  sandbox.dispatchEvent = (event) => sandbox.dispatched.push(event);
  const source = section(DRAWER, "  var _layoutPreviewKinds", "  function setFloating");
  vm.runInNewContext(`
    var _opts = {
      onWidthChange: function (n, persisted) { widthCalls.push({ n, persisted, commit: _layoutCommitDepth > 0 }); },
      onReflow: function () { reflowCalls.push({ commit: _layoutCommitDepth > 0 }); }
    };
    var widthCalls = [], reflowCalls = [];
    var LS_WIDTH = "width";
    function _akey() { return "width"; }
    function _lsSet() {}
    function _clampWidth(value) { return Number(value); }
    ${source}
    this.api = { setWidth, reflow: _reflow, preview: _layoutPreviewActive, widthCalls, reflowCalls };
  `, sandbox, { filename: "rc-sidedrawer-layout-harness.js" });

  sandbox.api.setWidth(400, false);
  sandbox.api.setWidth(440, false);
  assert.equal(sandbox.api.preview(), true);
  assert.equal(sandbox.api.widthCalls.length, 0, "pointer/input preview must not notify raster consumer");
  sandbox.api.setWidth(440, true);
  assert.equal(sandbox.api.preview(), false);
  assert.deepEqual(JSON.parse(JSON.stringify(sandbox.api.widthCalls)), [{ n: 440, persisted: true, commit: true }]);

  sandbox.api.reflow();
  assert.equal(sandbox.api.preview(), true);
  assert.equal(sandbox.api.reflowCalls.length, 0, "transition must not reflow before it settles");
  sideListeners.get("transitionend")({ target: side, propertyName: "transform" });
  assert.equal(sandbox.api.preview(), false);
  assert.deepEqual(JSON.parse(JSON.stringify(sandbox.api.reflowCalls)), [{ commit: true }]);
  assert.equal(timers.pending.size, 0, "transitionend cancels the timeout fallback");
});

test("ResizeObserver 在侧栏预览期 0 次 refit，稳定后合并为一次 visible-near refit", () => {
  const timers = fakeTimers();
  const listeners = new Map();
  const calls = [];
  const state = { preview: true, commit: false };
  const sandbox = {
    Math,
    clearTimeout: timers.clearTimeout,
    setTimeout: timers.setTimeout,
    getComputedStyle: () => ({ paddingLeft: "0", paddingRight: "0" }),
    document: {
      getElementById: () => ({ clientWidth: 1000, style: {}, scrollLeft: 0 }),
      querySelectorAll: () => [],
    },
    RC: { sidedrawer: {
      isLayoutPreviewActive: () => state.preview,
      isLayoutCommitActive: () => state.commit,
    } },
    _refitToWidth: (...args) => calls.push(args),
  };
  sandbox.window = sandbox;
  sandbox.addEventListener = (type, fn) => listeners.set(type, fn);
  const source = section(LAYOUT, "let _refitDebounce", "// (已删除");
  vm.runInNewContext(`${source}\nthis.scheduleRefit = _scheduleRefit;`, sandbox, { filename: "pdf-refit-scheduler-harness.js" });

  for (let i = 0; i < 12; i += 1) sandbox.scheduleRefit(i % 2 === 0);
  assert.equal(timers.pending.size, 0);
  assert.equal(calls.length, 0);

  state.preview = false;
  listeners.get("rc:sidedrawer-layout-settled")({ detail: { committed: true } });
  state.commit = true;
  sandbox.scheduleRefit(true); // onReflow/onWidthChange duplicate in the same commit
  sandbox.scheduleRefit(false);
  state.commit = false;
  assert.equal(timers.pending.size, 1, "all stable notifications share one debounce slot");
  timers.runAll();
  assert.deepEqual(calls, [[true, false, "visible-near"]]);

  sandbox.scheduleRefit(false);
  sandbox.scheduleRefit(true);
  timers.runAll();
  assert.deepEqual(calls.at(-1), [true, false, null], "ordinary resize remains full-scope");
});

function makeWrap(page, top) {
  return {
    dataset: { pageNum: String(page), loaded: "1" },
    style: {},
    __renderScale: 1,
    querySelector: () => ({ className: "page-img" }),
    getBoundingClientRect: () => ({ top, bottom: top + 700, height: 700 }),
  };
}

async function runRescale(options) {
  const wraps = [
    makeWrap(1, -5200), makeWrap(2, -3000), makeWrap(3, -700),
    makeWrap(4, 0), makeWrap(5, 800), makeWrap(6, 2600), makeWrap(7, 5000),
  ];
  const renders = [];
  const pendingAtCall = [];
  const container = {
    querySelectorAll: () => wraps,
    querySelector: () => null,
  };
  const main = { clientHeight: 800, getBoundingClientRect: () => ({ top: 0, bottom: 800 }) };
  const sandbox = {
    Math,
    currentPage: 4,
    readMode: "continuous",
    scale: 0.8,
    _imgMode: true,
    _cropVisWFrac: () => 1,
    _cropVisHFrac: () => 1,
    _renderPageInto: (page, wrap) => {
      renders.push(page);
      pendingAtCall.push(wrap.__sideRefitPending === true);
      return Promise.resolve();
    },
    pdfDoc: { getPage: async () => ({ getViewport: ({ scale }) => ({ width: 1000 * scale, height: 1400 * scale }) }) },
    document: { getElementById: (id) => id === "page-container" ? container : main },
  };
  sandbox.window = sandbox;
  sandbox.__imgMeta = { page_w: 1000, page_h: 1400 };
  const fn = section(CONTINUOUS, "async function _rescaleContinuousInPlace", "window._rescaleContinuousInPlace");
  vm.runInNewContext(`${fn}\nthis.rescale = _rescaleContinuousInPlace;`, sandbox, { filename: "pdf-visible-refit-harness.js" });
  await sandbox.rescale(options);
  return { renders, wraps, pendingAtCall };
}

test("侧栏 stable refit 只高清化可见/近邻页，且每页至多一次", async () => {
  const { renders, wraps, pendingAtCall } = await runRescale({ rasterScope: "visible-near" });
  assert.deepEqual(renders, [3, 4, 5]);
  assert.equal(new Set(renders).size, renders.length);
  assert.deepEqual(pendingAtCall, [true, true, true], "IO guard is armed before each scoped raster starts");
  assert.match(CONTINUOUS, /e\.target\.dataset\.loaded === '0' && !e\.target\.__sideRefitPending/);
  assert.equal(wraps[0].dataset.loaded, "0", "far loaded page is deferred to IntersectionObserver");
  assert.equal(wraps[0].style.zoom, 0.8, "far page keeps correct cheap CSS geometry");
});

test("用户主动缩放只高清化视口与邻页，远页留给 IntersectionObserver", async () => {
  const { renders } = await runRescale({ rasterScope: "visible-near" });
  assert.deepEqual(renders, [3, 4, 5]);
  assert.match(LAYOUT, /_applyZoom[\s\S]*?_rescaleContinuousInPlace\(\{ rasterScope: 'visible-near' \}\)/);
  assert.match(LAYOUT, /previewNode[\s\S]*?requestAnimationFrame/);
  assert.doesNotMatch(LAYOUT, /page-container'\)\.style\.transform = 'scale/);
  assert.match(NAV, /zoomChange[\s\S]*?_rescaleContinuousInPlace\(\{ rasterScope: 'visible-near' \}\)/);
  assert.match(DRAWER, /addEventListener\('input',[\s\S]*?setWidth\(this\.value, false\)/);
  assert.match(DRAWER, /addEventListener\('change',[\s\S]*?setWidth\(this\.value, true\)/);
});
