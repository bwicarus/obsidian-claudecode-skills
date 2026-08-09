import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const UI = read("_server_deploy/static/pdf/rc-ui.js");
const PDF_LAYOUT = read("_server_deploy/static/pdf/reader.src/06-layout.js");
const EPUB = read("_server_deploy/static/pdf/epub-html.js");
const PDF_TEMPLATE = read("_server_deploy/templates/pdf_reader.html");
const EPUB_TEMPLATE = read("_server_deploy/templates/epub_html_reader.html");
const APP = read("ios/BWReader/App/ReaderWebView.swift");
const SHELL = read("extensions/bw-reader-webext/src/shell.js");
const BACKGROUND = read("extensions/bw-reader-webext/background.js");
const PWA_ADAPTER = read("extensions/bw-reader-webext/src/pwa-adapter.js");

class ClassList {
  constructor() { this.values = new Set(); }
  add(...names) { names.forEach((name) => this.values.add(name)); }
  remove(...names) { names.forEach((name) => this.values.delete(name)); }
  contains(name) { return this.values.has(name); }
  toggle(name, force) {
    const next = force === undefined ? !this.contains(name) : !!force;
    if (next) this.add(name); else this.remove(name);
    return next;
  }
}

class Element {
  constructor(ownerDocument, id = "") {
    this.ownerDocument = ownerDocument;
    this.id = id;
    this.dataset = {};
    this.classList = new ClassList();
    this.children = [];
    this.listeners = new Map();
    this.isConnected = true;
    this.rect = { width: 320, height: 48 };
  }
  set className(value) {
    this.classList = new ClassList();
    String(value || "").split(/\s+/).filter(Boolean).forEach((name) => this.classList.add(name));
  }
  get className() { return [...this.classList.values].join(" "); }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  addEventListener(type, listener) { this.listeners.set(type, listener); }
  click() { this.listeners.get("click")?.({ target: this }); }
  setAttribute(name, value) { this[name] = String(value); }
  getBoundingClientRect() { return this.rect; }
}

function uiHarness() {
  const storage = new Map();
  const document = {
    readyState: "complete",
    createElement: (tag) => new Element(document, tag),
    querySelector: () => null,
    querySelectorAll: () => [],
    getElementById: () => null,
    addEventListener() {},
  };
  document.head = new Element(document, "head");
  document.body = new Element(document, "body");
  document.documentElement = new Element(document, "html");
  const window = {
    RC: {},
    dispatchEvent() {},
  };
  window.window = window;
  const localStorage = {
    getItem: (key) => storage.get(key) ?? null,
    setItem: (key, value) => storage.set(key, String(value)),
  };
  vm.runInNewContext(UI, {
    CustomEvent: class CustomEvent { constructor(type) { this.type = type; } },
    Event: class Event { constructor(type) { this.type = type; } },
    document,
    localStorage,
    window,
  });
  return { document, localStorage, storage, window };
}

test("expanding the shared pill clears only stale visual fullscreen", () => {
  const h = uiHarness();
  const mount = new Element(h.document, "app");
  const bar = new Element(h.document, "header");
  mount.appendChild(bar);
  h.document.documentElement.classList.add("fs-mode");
  h.localStorage.setItem("pdf-fullscreen", "1");
  const api = h.window.RC.ui.mountCollapsibleTopbar({
    bar,
    mount,
    defaultCollapsed: true,
    persist: false,
  });
  assert.equal(api.isCollapsed(), true);
  api.pill.click();
  assert.equal(api.isCollapsed(), false);
  assert.equal(h.document.documentElement.classList.contains("fs-mode"), false);
  assert.equal(h.storage.get("pdf-fullscreen"), "0");

  h.document.fullscreenElement = {};
  h.document.documentElement.classList.add("fs-mode");
  api.close();
  api.open();
  assert.equal(
    h.document.documentElement.classList.contains("fs-mode"),
    true,
    "a real Fullscreen API session must not be cancelled by the topbar",
  );
});

test("ordinary extension pages cannot clear a site's own fs-mode class", () => {
  const h = uiHarness();
  const mount = new Element(h.document, "app");
  const bar = new Element(h.document, "header");
  mount.appendChild(bar);
  h.document.documentElement.classList.add("fs-mode");
  h.window.RC.ui.mountCollapsibleTopbar({
    bar,
    mount,
    defaultCollapsed: false,
    persist: false,
    recoverFullscreen: false,
  });
  assert.equal(h.document.documentElement.classList.contains("fs-mode"), true);
});

test("fresh or storage-blocked extension pages start expanded without page localStorage", () => {
  const h = uiHarness();
  for (const origin of ["https://one.example", "https://two.example"]) {
    const mount = new Element(h.document, `mount-${origin}`);
    const bar = new Element(h.document, `bar-${origin}`);
    mount.appendChild(bar);
    const api = h.window.RC.ui.mountCollapsibleTopbar({
      bar,
      mount,
      defaultCollapsed: false,
      readCollapsed() { throw new Error("site storage blocked"); },
      writeCollapsed() { throw new Error("site storage blocked"); },
    });
    assert.equal(api.isCollapsed(), false, `${origin} must start with a visible bar`);
  }
  assert.match(SHELL, /TOPBAR_SESSION_KEY\s*=\s*"bw-topbar-collapsed-session-v1"/);
  assert.match(SHELL, /defaultCollapsed:\s*false/);
  assert.match(SHELL, /window\.__bwExtensionStore\?\.get\(TOPBAR_PREFS_KEY\)/);
  assert.match(SHELL, /window\.__bwExtensionStore\?\.set\(TOPBAR_PREFS_KEY/);
  assert.match(SHELL, /recoverFullscreen:\s*!!window\.__bwPwaBridge/);
  assert.doesNotMatch(SHELL, /storageKey:\s*"rc-topbar-collapsed:web"/);
  assert.doesNotMatch(SHELL, /localStorage\.getItem\([^\n]*bw-top/);
  assert.match(BACKGROUND, /LOCAL_STORAGE_KEYS\s*=\s*new Set\(\[[\s\S]*"bwTopbarPreferencesV1"/);
});

test("reload never restores a visual fs-mode without a live Fullscreen API session", () => {
  assert.match(PDF_TEMPLATE, /__bwPdfFullscreenActive\s*=\s*!!\(document\.fullscreenElement/);
  assert.match(PDF_TEMPLATE, /localStorage\.getItem\('pdf-fullscreen'\)[\s\S]*__bwPdfFullscreenActive/);
  assert.match(PDF_TEMPLATE, /setItem\('pdf-fullscreen', '0'\)/);
  assert.match(EPUB_TEMPLATE, /__bwEpubFullscreenActive\s*=\s*!!\(document\.fullscreenElement/);
  assert.match(EPUB_TEMPLATE, /localStorage\.getItem\('eph-fs-mode'\)[\s\S]*__bwEpubFullscreenActive/);
  assert.match(EPUB_TEMPLATE, /setItem\('eph-fs-mode', '0'\)/);
  assert.match(PDF_LAYOUT, /_fsEnabled\(\) && _browserFsActive\(\)/);
  assert.match(EPUB, /localStorage\.getItem\('eph-fs-mode'\) === '1' && _fsActive\(\)/);
  assert.match(APP, /#fs-restore\{right:calc\(env\(safe-area-inset-right,0px\) \+ 112px\)!important\}/);
});

function pwaAdapterHarness() {
  const listeners = new Map();
  const intervals = [];
  const header = new Element(null, "header");
  const pill = new Element(null, "bw-top-pill");
  const shadow = {
    getElementById(id) { return id === "header" ? header : id === "bw-top-pill" ? pill : null; },
    querySelector() { return null; },
  };
  const calls = { takeover: 0, release: 0 };
  const bridge = {
    ready: true,
    takenOver: false,
    state: { mode: "pdf", file: "book.pdf", capabilities: {} },
    selection: null,
    on() {},
    context: () => Promise.resolve({}),
    clearSelection: () => Promise.resolve(),
    local: () => Promise.resolve(),
    takeover() { calls.takeover += 1; this.takenOver = true; return Promise.resolve(true); },
    release() { calls.release += 1; this.takenOver = false; },
  };
  const RC = {
    actions: { bind() {} },
    contract: { endpoints: (value) => value, selection: (value) => value },
    stickynote: {},
    toast() {},
    use() {},
  };
  const document = {
    visibilityState: "visible",
    title: "Book",
    addEventListener(type, listener) { listeners.set(type, listener); },
    dispatchEvent() {},
    querySelector(selector) {
      if (selector !== 'meta[name="bw-reader-app"]') return null;
      return { getAttribute: () => "pdf" };
    },
  };
  const window = {
    RC,
    __bwPwaBridge: bridge,
    __bwShadow: shadow,
    __bwRoot: { dataset: {} },
    addEventListener() {},
  };
  window.window = window;
  vm.runInNewContext(PWA_ADAPTER, {
    CustomEvent: class CustomEvent {},
    JSON,
    Math,
    Number,
    Object,
    Promise,
    Set,
    String,
    console,
    document,
    setInterval(callback, delay) { const item = { callback, delay, active: true }; intervals.push(item); return item; },
    clearInterval(item) { item.active = false; },
    window,
  });
  return { bridge, calls, header, intervals, listeners, pill };
}

test("PWA takeover is gated by a visible extension shell and releases it when the shell disappears", async () => {
  const h = pwaAdapterHarness();
  h.listeners.get("bw:shell-ready")();
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(h.calls.takeover, 1);
  assert.equal(h.bridge.takenOver, true);
  assert.ok(h.intervals.some((item) => item.delay === 1000 && item.active));

  h.header.isConnected = false;
  h.intervals.find((item) => item.delay === 1000).callback();
  assert.equal(h.calls.release, 1);
  assert.equal(h.bridge.takenOver, false);
});
