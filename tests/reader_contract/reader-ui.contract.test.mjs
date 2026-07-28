import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const ROOT = new URL("../../", import.meta.url);
const UI_SOURCE = readFileSync(
  new URL("_server_deploy/static/pdf/rc-ui.js", ROOT),
  "utf8",
);
const EPUB_SOURCE = readFileSync(
  new URL("_server_deploy/static/pdf/epub-html.js", ROOT),
  "utf8",
);
const HTML_SOURCE = readFileSync(
  new URL("_server_deploy/static/pdf/html-reader.js", ROOT),
  "utf8",
);
const PDF_SELECTION_SOURCE = readFileSync(
  new URL("_server_deploy/static/pdf/reader.src/14-textlayer-legacy.js", ROOT),
  "utf8",
);

function loadUi() {
  const document = {
    readyState: "loading",
    head: { appendChild() {} },
    createElement() {
      return { id: "", textContent: "", style: {}, classList: { add() {} } };
    },
    addEventListener() {},
  };
  const sandbox = {
    console,
    document,
    setTimeout,
    clearTimeout,
    RC: {},
  };
  sandbox.window = sandbox;
  vm.runInContext(UI_SOURCE, vm.createContext(sandbox), { filename: "rc-ui.js" });
  return sandbox.RC.ui;
}

test("EPUB 与 HTML 共用相同的查词词项判定", () => {
  const ui = loadUi();
  assert.equal(ui.isDictionaryWord("reader"), true);
  assert.equal(ui.isDictionaryWord("can't"), true);
  assert.equal(ui.isDictionaryWord("読む"), true);
  assert.equal(ui.isDictionaryWord("two words"), false);
  assert.equal(ui.isDictionaryWord("研究"), false);
  assert.equal(ui.isDictionaryWord("a".repeat(31)), false);
  assert.equal(ui.isDictionaryWord(""), false);

  assert.doesNotMatch(EPUB_SOURCE, /function isWordSel\s*\(/);
  assert.doesNotMatch(HTML_SOURCE, /function isWordSel\s*\(/);
  assert.match(EPUB_SOURCE, /RC\.ui\.isDictionaryWord\(cur\.text\)/);
  assert.match(HTML_SOURCE, /RC\.ui\.isDictionaryWord\(cur\.text\)/);
});

test("PDF 保留现有英文词分流，不跟随 EPUB/HTML 日文规则", () => {
  assert.doesNotMatch(PDF_SELECTION_SOURCE, /isDictionaryWord/);
  assert.equal(
    PDF_SELECTION_SOURCE.includes(
      "const isWord = t.length > 0 && t.length <= 30 && /^[A-Za-z]",
    ),
    true,
  );
});

test("共享文字 Range 命中只接受真实行盒，不把空白吸附到最近词", () => {
  const ui = loadUi();
  const range = {
    getClientRects() {
      return [
        { left: 10, top: 20, right: 50, bottom: 40 },
        { left: 10, top: 60, right: 70, bottom: 80 },
      ];
    },
  };

  assert.equal(ui.rangeHitTest(range, 30, 30, { pointerType: "mouse" }), true);
  assert.equal(ui.rangeHitTest(range, 51, 30, { pointerType: "mouse" }), true);
  assert.equal(ui.rangeHitTest(range, 54, 30, { pointerType: "mouse" }), false);
  assert.equal(ui.rangeHitTest(range, 53, 30, { pointerType: "touch" }), true);
  assert.equal(ui.rangeHitTest(range, 54, 30, { pointerType: "touch" }), false);

  // 两行 Range 的 union bounding box 会覆盖 y=50；逐行 client rect 判定必须拒绝这块空白。
  assert.equal(ui.rangeHitTest(range, 30, 50, { pointerType: "mouse" }), false);
  assert.equal(ui.rangeHitTest(range, 300, 30, { pointerType: "touch" }), false);
  assert.equal(ui.rangeHitTest({ getClientRects: () => [] }, 10, 10), false);
  assert.equal(ui.rangeHitTest(null, 10, 10), false);
});
