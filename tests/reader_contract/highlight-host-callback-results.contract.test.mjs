import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const WEB = read("extensions/bw-reader-webext/src/web-highlights.js");
const OVERLAY = read("_server_deploy/static/pdf/pdf-uishared.js");
const HTML = read("_server_deploy/static/pdf/html-reader.js");

function section(source, start, end) {
  const from = source.indexOf(start);
  const to = source.indexOf(end, from + start.length);
  assert.notEqual(from, -1, `missing ${start}`);
  assert.notEqual(to, -1, `missing ${end}`);
  return source.slice(from, to);
}

function load(source, start, end, globals) {
  const sandbox = { console, ...globals };
  sandbox.window = sandbox;
  vm.runInContext(section(source, start, end), vm.createContext(sandbox));
  return sandbox;
}

test("ordinary-web highlight callbacks report durable success and roll back failed local writes", async () => {
  const toasts = [];
  let paints = 0;
  const rec = { id: "w1", color: "old", note: "before" };
  const sandbox = load(
    WEB,
    "async function patchRecord",
    "// CSS Highlight API",
    {
      records: [rec],
      persist: async () => {},
      applyAll: () => { paints += 1; },
      RC: { toast: (message) => toasts.push(message) },
    },
  );

  assert.equal(await sandbox.patchRecord(rec, { color: "new" }, sandbox.applyAll), true);
  assert.equal(rec.color, "new");
  assert.equal(paints, 1);

  sandbox.persist = async () => { throw new Error("disk full"); };
  assert.equal(await sandbox.patchRecord(rec, { note: "after" }), false);
  assert.equal(rec.note, "before", "failed persistence must not leak into the in-memory record");
  assert.match(toasts.at(-1), /disk full/);

  assert.equal(await sandbox.removeRecord(rec.id), false);
  assert.deepEqual(sandbox.records.map((item) => item.id), ["w1"]);
  assert.equal(paints, 2, "failed removal repaints the restored record");

  sandbox.persist = async () => {};
  assert.equal(await sandbox.removeRecord(rec.id), true);
  assert.deepEqual(sandbox.records, []);
  assert.equal(paints, 3);
});

test("ordinary-web editor gives RC.highlight the Promise<boolean> helpers", () => {
  assert.match(WEB, /onColor:c=>patchRecord\(rec,\{color:c\},applyAll\)/);
  assert.match(WEB, /onNote:t=>patchRecord\(rec,\{note:String\(t\|\|''\)\}\)/);
  assert.match(WEB, /const ok=await window\.__bwWebHighlights\.remove\(rec\.id\)[\s\S]*return ok/);
});

function htmlHarness() {
  const toasts = [];
  const mark = { style: { background: "old" } };
  const sandbox = load(
    HTML,
    "function patchHl(h, f)",
    "function openHlEditor(h)",
    {
      RC: { reqJson: async () => ({ ok: false, error: "unset" }) },
      EP: { highlights: "/highlights" },
      FREL: "book.html",
      _hls: [{ id: "h1", color: "old", note: "before" }],
      _marksOf: () => [mark],
      _unwrapMarks: () => { sandbox.unwraps += 1; },
      toast: (message) => toasts.push(message),
      unwraps: 0,
    },
  );
  return { sandbox, toasts, mark };
}

test("HTML host mutates highlight state only after an explicit server acknowledgement", async () => {
  const { sandbox, toasts, mark } = htmlHarness();
  const h = sandbox._hls[0];

  sandbox.RC.reqJson = async () => ({ ok: false, error: "write refused" });
  assert.equal(await sandbox.patchHl(h, { color: "bad" }), false);
  assert.equal(h.color, "old");
  assert.equal(mark.style.background, "old");
  assert.match(toasts.at(-1), /write refused/);

  sandbox.RC.reqJson = async () => ({ ok: true, highlight: { color: "green", note: "before" } });
  assert.equal(await sandbox.patchHl(h, { color: "green" }), true);
  assert.equal(h.color, "green");
  assert.equal(mark.style.background, "green");

  sandbox.RC.reqJson = async () => ({ ok: false, error: "delete refused" });
  assert.equal(await sandbox.delHl(h), false);
  assert.equal(sandbox._hls.length, 1);
  assert.equal(sandbox.unwraps, 0);

  sandbox.RC.reqJson = async () => ({ ok: true });
  assert.equal(await sandbox.delHl(h), true);
  assert.equal(sandbox._hls.length, 0);
  assert.equal(sandbox.unwraps, 1);
});

function overlayHarness() {
  const toasts = [];
  const mark = { style: { background: "old" } };
  const rec = { id: "page-1" };
  const h = { id: "h1", color: "old", note: "before" };
  const cache = { "page-1": [h] };
  const sandbox = load(
    OVERLAY,
    "function _ovPatchHl(rec, h, f)",
    "function _ovOpenHlEditor(rec, h)",
    {
      RC: { reqJson: async () => ({ ok: false, error: "unset" }), toast: (message) => toasts.push(message) },
      _ovHlKey: () => "book::page-1",
      _ovBodyOfRec: () => ({}),
      _ovMarksOf: () => [mark],
      _ovUnwrapMarks: () => { sandbox.unwraps += 1; },
      _ovHlCache: cache,
      unwraps: 0,
    },
  );
  return { sandbox, toasts, mark, rec, h };
}

test("PDF overlay host also returns strict booleans and keeps failed writes visible", async () => {
  const { sandbox, toasts, mark, rec, h } = overlayHarness();

  sandbox.RC.reqJson = async () => ({ ok: false, error: "write refused" });
  assert.equal(await sandbox._ovPatchHl(rec, h, { color: "bad" }), false);
  assert.equal(h.color, "old");
  assert.equal(mark.style.background, "old");
  assert.match(toasts.at(-1), /write refused/);

  sandbox.RC.reqJson = async () => ({ ok: true, highlight: { color: "blue", note: "before" } });
  assert.equal(await sandbox._ovPatchHl(rec, h, { color: "blue" }), true);
  assert.equal(h.color, "blue");
  assert.equal(mark.style.background, "blue");

  sandbox.RC.reqJson = async () => ({ ok: false, error: "delete refused" });
  assert.equal(await sandbox._ovDelHl(rec, h), false);
  assert.equal(sandbox._ovHlCache[rec.id].length, 1);
  assert.equal(sandbox.unwraps, 0);

  sandbox.RC.reqJson = async () => ({ ok: true });
  assert.equal(await sandbox._ovDelHl(rec, h), true);
  assert.equal(sandbox._ovHlCache[rec.id].length, 0);
  assert.equal(sandbox.unwraps, 1);
});

test("HTML and PDF overlay editor/list callbacks return the persistence result", () => {
  assert.match(HTML, /onColor: function \(c\) \{ return patchHl\(h, \{ color: c \}\); \}/);
  assert.match(HTML, /onNote: function \(t\) \{ return patchHl\(h, \{ note: t \}\); \}/);
  assert.match(HTML, /onDelete: function \(h\) \{ return delHl\(h\); \}/);
  assert.match(OVERLAY, /onColor: function \(c\) \{ return _ovPatchHl\(rec, h, \{ color: c \}\); \}/);
  assert.match(OVERLAY, /onNote: function \(t\) \{ return _ovPatchHl\(rec, h, \{ note: t \}\); \}/);
  assert.match(OVERLAY, /onDelete: function \(\) \{ return _ovDelHl\(rec, h\); \}/);
});
