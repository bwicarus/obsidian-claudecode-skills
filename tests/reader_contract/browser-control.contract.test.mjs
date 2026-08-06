import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const ROOT = new URL("../../", import.meta.url);
const SOURCE = readFileSync(
  new URL("extensions/bw-reader-webext/src/browser-control.js", ROOT),
  "utf8",
);

class FakeElement {
  constructor(tagName, attributes = {}) {
    this.nodeType = 1;
    this.tagName = tagName.toUpperCase();
    this.attributes = { ...attributes };
    this.childNodes = [];
    this.parentElement = null;
    this.hidden = false;
    this.scrollHeight = 100;
    this.clientHeight = 100;
    this.scrolled = 0;
  }
  append(...nodes) {
    for (const node of nodes) {
      node.parentElement = this;
      this.childNodes.push(node);
    }
    return this;
  }
  getAttribute(name) { return this.attributes[name] ?? null; }
  get textContent() { return this.childNodes.map((node) => node.nodeValue ?? node.textContent).join(""); }
  getClientRects() { return [{}]; }
  getBoundingClientRect() { return { top: 100, left: 0, width: 100, height: 20 }; }
  scrollIntoView() { this.scrolled += 1; }
  scrollBy({ top }) { this.scrollTop = (this.scrollTop || 0) + top; }
  querySelectorAll(selector) {
    const found = [];
    const visit = (node) => {
      if (node.nodeType === 1 && selector === "path[data-region-id]" &&
          node.tagName === "PATH" && node.getAttribute("data-region-id") !== null) found.push(node);
      for (const child of node.childNodes || []) visit(child);
    };
    visit(this);
    return found;
  }
}

class FakeText {
  constructor(value) {
    this.nodeType = 3;
    this.nodeValue = value;
    this.parentElement = null;
  }
}

function flatten(root, kind) {
  const output = [];
  const visit = (node) => {
    if (node !== root && ((kind === 4 && node.nodeType === 3) || (kind === 1 && node.nodeType === 1))) {
      output.push(node);
    }
    for (const child of node.childNodes || []) visit(child);
  };
  visit(root);
  return output;
}

function harness() {
  const heading = new FakeElement("h2").append(new FakeText("Detailed Results"));
  const paragraph = new FakeElement("p").append(new FakeText("Alpha target phrase omega"));
  const region = new FakeElement("path", { "data-region-id": "rg_safe_1" });
  const svg = new FakeElement("svg").append(region);
  const body = new FakeElement("body").append(heading, paragraph, svg);
  const listeners = new Map();
  const refreshes = [];
  const responses = [];
  const frameWindow = { postMessage(value) { responses.push(structuredClone(value)); } };
  const frame = {
    contentWindow: frameWindow,
    src: "safari-web-extension://unit-test/call.html?compact=1",
  };
  const document = {
    body,
    documentElement: body,
    title: "Current page",
    visibilityState: "visible",
    focused: true,
    hasFocus() { return this.focused; },
    elementFromPoint() { return paragraph; },
    querySelectorAll(selector) { return body.querySelectorAll(selector); },
    createTreeWalker(root, kind, filter) {
      const nodes = flatten(root, kind).filter((node) => !filter || !filter.acceptNode || filter.acceptNode(node) === 1);
      let index = 0;
      return { nextNode() { return nodes[index++] || null; } };
    },
  };
  const window = {
    document,
    location: { href: "https://example.test/article" },
    innerWidth: 1000,
    innerHeight: 800,
    scrollX: 0,
    scrollY: 0,
    __bwShadow: { querySelectorAll: (selector) => body.querySelectorAll(selector) },
    getComputedStyle() { return { display: "block", visibility: "visible", overflowY: "visible" }; },
    addEventListener(type, listener) { listeners.set(type, listener); },
    removeEventListener(type, listener) { if (listeners.get(type) === listener) listeners.delete(type); },
    dispatchEvent(event) { refreshes.push(event); return true; },
    scrollBy({ top }) { this.scrollY += top; },
    scrollTo(x, y) { this.scrollX = x; this.scrollY = y; },
  };
  window.chrome = {
    runtime: { getURL: (path = "") => `safari-web-extension://unit-test/${path}` },
  };
  window.window = window;
  class CustomEvent {
    constructor(type, options) { this.type = type; this.detail = options?.detail; }
  }
  const sandbox = {
    window,
    document,
    location: window.location,
    chrome: window.chrome,
    CustomEvent,
    NodeFilter: { SHOW_ELEMENT: 1, SHOW_TEXT: 4, FILTER_ACCEPT: 1, FILTER_REJECT: 2 },
    URL,
    Set,
    Map,
    Object,
    String,
    Number,
    Math,
    Error,
    TypeError,
  };
  vm.runInNewContext(SOURCE, sandbox, { filename: "browser-control.js" });
  const api = window.__bwBrowserControl;
  api.install({ frame, sourceInstanceId: "source_abc-123" });
  const send = (data, source = frameWindow) => listeners.get("message")({ source, data });
  const request = (action, extra = {}) => ({
    contract: "bw-browser-control/1",
    type: "request",
    requestId: `req_${responses.length + 1}`,
    sourceInstanceId: "source_abc-123",
    action,
    ...extra,
  });
  return { api, document, window, frame, send, request, responses, refreshes, heading, paragraph, region };
}

test("contract exposes only five fixed actions and contains no dynamic execution/navigation surface", () => {
  const h = harness();
  assert.deepEqual([...h.api.actions], [
    "next-viewport",
    "previous-viewport",
    "scroll-to-text",
    "scroll-to-heading",
    "scroll-to-selection",
  ]);
  assert.doesNotMatch(SOURCE, /\beval\s*\(|\bnew\s+Function\b|location\.(?:assign|replace)|\.innerHTML\s*=/);
  assert.doesNotMatch(SOURCE, /querySelector(All)?\s*\(\s*(?:raw|request|target|selectionId)/);
});

test("only the installed frame can issue a command and a repeated request is idempotent", () => {
  const h = harness();
  const command = h.request("next-viewport");
  h.send(command, { postMessage() {} });
  assert.equal(h.responses.length, 0);
  assert.equal(h.window.scrollY, 0);

  h.send(command);
  assert.equal(h.responses[0].ok, true);
  assert.equal(h.window.scrollY, 656);
  assert.equal(h.refreshes[0].type, "bw:browser-control-refresh");
  assert.equal(h.refreshes[0].detail.sourceInstanceId, "source_abc-123");

  h.api.install({ frame: h.frame, sourceInstanceId: "source_abc-123" });
  h.send(command);
  assert.equal(h.responses.length, 2);
  assert.equal(h.window.scrollY, 656, "retry must not scroll a second time");
});

test("installation rejects a frame that is not this extension's call.html", () => {
  const h = harness();
  for (const src of [
    "https://evil.test/call.html",
    "safari-web-extension://different-extension/call.html",
    "safari-web-extension://unit-test/not-call.html",
  ]) {
    assert.throws(
      () => h.api.install({
        frame: { src, contentWindow: { postMessage() {} } },
        sourceInstanceId: "source_abc-123",
      }),
      /embedded call\.html frame/,
      src,
    );
  }
});

test("inactive documents and mismatched sources fail closed", () => {
  const h = harness();
  h.document.focused = false;
  h.send(h.request("next-viewport"));
  assert.equal(h.responses.at(-1).error.code, "BW_BROWSER_CONTROL_DOCUMENT_INACTIVE");
  assert.equal(h.window.scrollY, 0);

  h.document.focused = true;
  h.send({ ...h.request("next-viewport"), sourceInstanceId: "source_other" });
  assert.equal(h.responses.at(-1).error.code, "BW_BROWSER_CONTROL_SOURCE_MISMATCH");
  assert.equal(h.window.scrollY, 0);
});

test("visible live text, headings and web-ink regions are located without caller selectors", () => {
  const h = harness();
  h.send(h.request("scroll-to-text", { target: "target phrase" }));
  assert.equal(h.responses.at(-1).ok, true);
  assert.equal(h.paragraph.scrolled, 1);

  h.send(h.request("scroll-to-heading", { target: "Detailed Results" }));
  assert.equal(h.responses.at(-1).ok, true);
  assert.equal(h.heading.scrolled, 1);

  h.send(h.request("scroll-to-selection", { selectionId: "rg_safe_1" }));
  assert.equal(h.responses.at(-1).ok, true);
  assert.equal(h.region.scrolled, 1);
});

test("arbitrary actions, injected selection ids and extra fields are rejected", () => {
  const h = harness();
  h.send(h.request("run-javascript", { target: "alert(1)" }));
  assert.equal(h.responses.at(-1).error.code, "BW_BROWSER_CONTROL_ACTION_NOT_ALLOWED");

  h.send(h.request("scroll-to-selection", { selectionId: 'x"] body' }));
  assert.equal(h.responses.at(-1).error.code, "BW_BROWSER_CONTROL_INVALID_REQUEST");

  h.send({ ...h.request("next-viewport"), url: "https://evil.test" });
  assert.equal(h.responses.at(-1).error.code, "BW_BROWSER_CONTROL_INVALID_REQUEST");
});
