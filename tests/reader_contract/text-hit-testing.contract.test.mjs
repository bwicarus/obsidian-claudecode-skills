import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const CONTENT = read("extensions/bw-reader-webext/content.js");
const HTML = read("_server_deploy/static/pdf/html-reader.js");
const EPUB = read("_server_deploy/static/pdf/epub-html.js");
const OVERLAY = read("_server_deploy/static/pdf/pdf-uishared.js");
const PDF_SELECTION = read("_server_deploy/static/pdf/reader.src/13-selection.js");

function section(source, start, end) {
  const from = source.indexOf(start);
  const to = source.indexOf(end, from + start.length);
  assert.notEqual(from, -1, `missing ${start}`);
  assert.notEqual(to, -1, `missing ${end}`);
  return source.slice(from, to);
}

test("普通网页的下划线与独立分词都经过共享 Range 几何验收", () => {
  const click = section(
    CONTENT,
    "document.addEventListener('click', (e) => {",
    "// 点真实页别处",
  );
  assert.match(click, /vr && _rangeHit\(vr, e\)/);
  assert.match(click, /if \(!_rangeHit\(rr, e\)\) return;/);
  assert.ok(
    click.indexOf("if (!_rangeHit(rr, e)) return;") <
      click.lastIndexOf("lookup({text:"),
    "lookup must happen only after the candidate word range passes geometry",
  );
});

for (const [name, source, start, end] of [
  ["HTML", HTML, "function clickWord(x, y, pointerType)", "function _closeWordPop()"],
  ["PDF overlay", OVERLAY, "function _ovClickWord(x, y, rec, pointerType)", "function _ovResultOpts(ctx)"],
]) {
  test(`${name} 的空白 tap 不查最近词，也不消费查词节流`, () => {
    const click = section(source, start, end);
    assert.match(click, /RC\.ui\.rangeHitTest/);
    const hit = click.indexOf("RC.ui.rangeHitTest");
    const gate = click.indexOf(name === "HTML" ? "_dictGate()" : "_ovDictGate()");
    assert.ok(hit >= 0 && gate > hit, "dictionary gate must run after physical hit acceptance");
  });
}

test("EPUB 空白 tap 在计入多击状态前被拒绝并重置计数", () => {
  const tap = section(
    EPUB,
    "content.addEventListener('pointerup', function (e) {",
    "// ── 语法分析",
  );
  assert.match(tap, /RC\.ui\.rangeHitTest\(hitRange/);
  assert.match(tap, /_tapCount = 0; _tapWord = null; _tapTime = 0;/);
  assert.ok(
    tap.indexOf("RC.ui.rangeHitTest(hitRange") < tap.indexOf("var sameSpot"),
    "blank candidates must not advance the single/double/triple tap state",
  );
});

test("收藏夹 PDF 只在真实词框起手，已开始拖选后仍可 nearest 延伸", () => {
  const start = section(EPUB, "function _favStart(", "function _favMove(");
  const move = section(EPUB, "function _favMove(", "// 拖选定案");
  assert.match(start, /_favHitStrict\(/);
  assert.doesNotMatch(start, /var a = _favHit\(/);
  assert.match(move, /var idx = _favHit\(/);
});

test("PDF 主字符层保留有界 ruby 容差，不改成 DOM caret 吸附", () => {
  const strict = section(
    PDF_SELECTION,
    "const _findCharStrict = (x, y) => {",
    "const onStart = (x, y, cx, cy) => {",
  );
  assert.match(strict, /dy > h \* 0\.7/);
  assert.match(strict, /dx > h \* 1\.2/);
  assert.match(strict, /return -1;/);
  assert.doesNotMatch(strict, /caretRangeFromPoint|rangeHitTest/);
});
