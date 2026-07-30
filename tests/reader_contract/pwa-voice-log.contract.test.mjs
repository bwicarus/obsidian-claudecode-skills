import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const SOURCE = readFileSync(
  new URL(
    "../../extensions/bw-reader-webext/src/pwa-adapter.js",
    import.meta.url,
  ),
  "utf8",
);

function loadAdapter(mode) {
  const requests = [];
  const bridge = {
    ready: false,
    selection: null,
    state: { file: "library/book.epub" },
    on() {},
    context() {
      return Promise.resolve({});
    },
    clearSelection() {
      return Promise.resolve();
    },
    local() {
      return Promise.resolve();
    },
  };
  const RC = {
    actions: { bind() {} },
    contract: {
      endpoints(value) {
        return value;
      },
      selection(value) {
        return value;
      },
    },
    md(value) {
      return String(value || "");
    },
    stickynote: {},
    toast() {},
    use() {},
  };
  const document = {
    addEventListener() {},
    dispatchEvent() {},
    querySelector(selector) {
      if (selector !== 'meta[name="bw-reader-app"]') return null;
      return {
        getAttribute(name) {
          return name === "content" ? mode : null;
        },
      };
    },
  };
  const window = {
    RC,
    __bwPwaBridge: bridge,
    __bwReaderFetch(url, init) {
      requests.push({ url, init });
      return Promise.resolve({ ok: true });
    },
  };
  window.window = window;
  vm.runInNewContext(SOURCE, {
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
    window,
  });
  return { adapter: window.__pwaAdapter, requests };
}

test("PWA adapter lets PDF use the full default turn log and keeps EPUB book history", async () => {
  const pdf = loadAdapter("pdf");
  assert.equal(
    Object.prototype.hasOwnProperty.call(pdf.adapter._host.asst, "voiceLog"),
    false,
    "PDF must not install a truthy noop that swallows rc-assistant default logging",
  );

  const epub = loadAdapter("epub");
  assert.equal(typeof epub.adapter._host.asst.voiceLog, "function");
  epub.adapter._host.asst.voiceLog("question", "answer", 7);
  await Promise.resolve();

  assert.equal(epub.requests.length, 2);
  assert.deepEqual(
    epub.requests.map(({ url, init }) => ({
      url,
      method: init.method,
      keepalive: init.keepalive,
      body: JSON.parse(init.body),
    })),
    [
      {
        url: "/pdf/api/epub-convo/append",
        method: "POST",
        keepalive: true,
        body: {
          file: "library/book.epub",
          role: "user",
          content: "question",
          section: 6,
        },
      },
      {
        url: "/pdf/api/epub-convo/append",
        method: "POST",
        keepalive: true,
        body: {
          file: "library/book.epub",
          role: "assistant",
          content: "answer",
          section: 6,
        },
      },
    ],
  );
});
