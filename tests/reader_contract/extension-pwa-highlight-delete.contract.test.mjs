import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";

const ROOT = path.resolve(import.meta.dirname, "../..");
const SHELL = fs.readFileSync(
  path.join(ROOT, "extensions/bw-reader-webext/src/shell.js"),
  "utf8",
);
const BOOK_HOST = fs.readFileSync(path.join(ROOT, "_server_deploy/static/reader-runtime/book-host.js"), "utf8");
const PDF_HOST = fs.readFileSync(path.join(ROOT, "_server_deploy/static/pdf/reader.src/32-extension-host.js"), "utf8");
const EPUB_HOST = fs.readFileSync(path.join(ROOT, "_server_deploy/static/pdf/epub-html.js"), "utf8");
const HTML_HOST = fs.readFileSync(path.join(ROOT, "_server_deploy/static/pdf/html-reader.js"), "utf8");

function functionSource(source, name) {
  let start = source.indexOf(`async function ${name}(`);
  if (start < 0) start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `missing ${name}`);
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    if (source[i] === "}") depth -= 1;
    if (depth === 0) return source.slice(start, i + 1);
  }
  assert.fail(`unterminated ${name}`);
}

const DELETE = functionSource(SHELL, "deletePwaHighlight");
const SYNC = functionSource(SHELL, "syncPwaHighlightRemoval");

async function runDelete({ response, reject } = {}) {
  const calls = [];
  const toasts = [];
  const synced = [];
  const context = {
    encodeURIComponent,
    RC: { toast: (message) => toasts.push(message) },
    bwFetch: async (...args) => {
      calls.push(args);
      if (reject) throw reject;
      return response;
    },
    syncPwaHighlightRemoval: async (...args) => {
      synced.push(args);
      return true;
    },
  };
  vm.runInNewContext(`${DELETE}; result = deletePwaHighlight;`, context);
  const result = await context.result("/pdf/api/highlights", "book.pdf", "pdf", { id: "hl-1" });
  return { result, calls, toasts, synced };
}

test("PWA pane removes its row only after HTTP and payload explicitly confirm deletion", async () => {
  const ok = await runDelete({
    response: { ok: true, status: 200, json: async () => ({ ok: true }) },
  });
  assert.equal(ok.result, true);
  assert.equal(ok.synced.length, 1);
  assert.deepEqual(ok.synced[0], ["pdf", { id: "hl-1" }]);
  assert.equal(ok.calls[0][1].method, "DELETE");

  const refused = await runDelete({
    response: { ok: true, status: 200, json: async () => ({ ok: false, error: "locked" }) },
  });
  assert.equal(refused.result, false);
  assert.equal(refused.synced.length, 0);
  assert.match(refused.toasts.join(" "), /locked/);

  const httpFailure = await runDelete({
    response: { ok: false, status: 503, json: async () => ({ ok: true }) },
  });
  assert.equal(httpFailure.result, false);
  assert.equal(httpFailure.synced.length, 0);
});

test("PWA pane keeps the highlight when the deletion result is unknown", async () => {
  const network = await runDelete({ reject: new Error("offline") });
  assert.equal(network.result, false);
  assert.equal(network.synced.length, 0);
  assert.match(network.toasts.join(" "), /删除未确认.*offline/);

  const invalid = await runDelete({
    response: { ok: true, status: 200, json: async () => { throw new Error("bad json"); } },
  });
  assert.equal(invalid.result, false);
  assert.equal(invalid.synced.length, 0);
  assert.match(invalid.toasts.join(" "), /响应无法解析/);
});

test("book-host/1 exposes projection-only remove_highlight through the highlight capability", async () => {
  const calls = [];
  const context = {
    console,
    document: { dispatchEvent() {}, title: "Book" },
    CustomEvent: class {},
  };
  vm.runInNewContext(BOOK_HOST, context);
  const api = context.BWReaderBookHost.register({
    mode: "pdf",
    actions: { remove_highlight: (payload) => { calls.push(payload); return { ok: true }; } },
    capabilities: { highlight: true },
  });
  assert.deepEqual(await api.localAction("remove_highlight", { id: "hl-1" }), { ok: true });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].id, "hl-1");
});

test("shell asks the page host to remove the projection and only trusts explicit success", async () => {
  async function run(local) {
    const calls = [];
    const fallbacks = [];
    const context = {
      pwaBridge: { local: async (...args) => { calls.push(args); return local(...args); } },
      removePwaHighlightDom: (...args) => { fallbacks.push(args); return true; },
    };
    vm.runInNewContext(`${SYNC}; result = syncPwaHighlightRemoval;`, context);
    return { value: await context.result("epub", { id: "hl-1" }), calls, fallbacks };
  }

  const ok = await run(async () => ({ ok: true }));
  assert.equal(ok.value, true);
  assert.equal(ok.calls.length, 1);
  assert.equal(ok.calls[0][0], "remove_highlight");
  assert.equal(ok.calls[0][1].id, "hl-1");
  assert.equal(ok.fallbacks.length, 0);

  const ambiguous = await run(async () => ({ ok: false }));
  assert.equal(ambiguous.value, false);
  assert.equal(ambiguous.fallbacks.length, 0);

  const oldHost = await run(async () => { throw new Error("书籍宿主不允许本地命令：remove_highlight"); });
  assert.equal(oldHost.value, true);
  assert.deepEqual(oldHost.fallbacks, [["epub", "hl-1"]]);

  const broken = await run(async () => { throw new Error("projection crashed"); });
  assert.equal(broken.value, false);
  assert.equal(broken.fallbacks.length, 0);
});

test("PDF, EPUB, and HTML hosts clear memory plus overlays without issuing another DELETE", () => {
  const pdf = functionSource(PDF_HOST, "_bookRemoveHighlightProjection");
  const pdfNodes = [{ dataset: { id: "hl-1" }, remove() { this.removed = true; } }];
  const pdfContext = { document: { querySelectorAll: () => pdfNodes } };
  vm.runInNewContext(
    `var _allHighlights=[{id:'hl-1',page:1},{id:'hl-2',page:2}];` +
      `var _hlByPage={1:[{id:'hl-1'}],2:[{id:'hl-2'}]};${pdf};` +
      `result=_bookRemoveHighlightProjection('hl-1');state={all:_allHighlights,byPage:_hlByPage};`,
    pdfContext,
  );
  assert.equal(pdfContext.result.ok, true);
  assert.equal(pdfContext.state.all.length, 1);
  assert.equal(pdfContext.state.byPage[1].length, 0);
  assert.equal(pdfNodes[0].removed, true);

  const epub = functionSource(EPUB_HOST, "_bookRemoveHighlightProjection");
  const unapplied = [];
  const epubContext = { unapplyHl: (record) => unapplied.push(record.id) };
  vm.runInNewContext(`var _hls={'hl-1':{id:'hl-1'}};${epub};result=_bookRemoveHighlightProjection('hl-1');state=_hls;`, epubContext);
  assert.equal(epubContext.result.ok, true);
  assert.equal(Object.keys(epubContext.state).length, 0);
  assert.deepEqual(unapplied, ["hl-1"]);

  const html = functionSource(HTML_HOST, "_bookRemoveHighlightProjection");
  const unwrapped = [];
  const htmlContext = { _unwrapMarks: (id) => unwrapped.push(id) };
  vm.runInNewContext(`var _hls=[{id:'hl-1'},{id:'hl-2'}];${html};result=_bookRemoveHighlightProjection('hl-1');state=_hls;`, htmlContext);
  assert.equal(htmlContext.result.ok, true);
  assert.equal(htmlContext.state.length, 1);
  assert.deepEqual(unwrapped, ["hl-1"]);

  for (const source of [pdf, epub, html]) assert.doesNotMatch(source, /\b(?:fetch|reqJson)\b|DELETE/);
  assert.match(PDF_HOST, /'highlight', 'remove_highlight'/);
  assert.match(EPUB_HOST, /'highlight', 'remove_highlight'/);
  assert.match(HTML_HOST, /'highlight', 'remove_highlight'/);
});
