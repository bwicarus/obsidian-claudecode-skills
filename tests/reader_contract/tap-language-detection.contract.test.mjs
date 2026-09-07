import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const SEL = read("_server_deploy/static/pdf/reader.src/13-selection.js");
const READER = read("_server_deploy/static/pdf/reader.js");

// 用户 2026-09-07 实锤：「有些词可以滑动选中但是无法单击选中」（2位 心疾患）。未声明语言的书里，
// 纯汉字词只看所在句有没有假名；列表/标题/表格里的孤立词整句就一行、没假名 → 被当成母语中文词、
// 连选中一起清掉。拖选走别的分支所以正常。现在再补两级证据：所点处的 jp 下划线、整页有没有假名。
test("未声明语言时纯汉字词的日语判定不止看所在句：下划线 jp 或整页假名也算", () => {
  assert.match(
    SEL,
    /isJa = hasKana\(_t\) \|\| \(hasKanji\(_t\) && \(hasKana\(_ctx\) \|\| _tapMarkIsJa\(pw, startIdx\) \|\| _pageHasKana\(pw\)\)\);/,
  );
  // 声明了语言的书不受影响：仍按声明
  assert.match(SEL, /isJa = BOOK_LANGS\.includes\('ja'\) && \(hasKana\(_t\) \|\| hasKanji\(_t\)\);/);
  assert.ok(READER.includes("function _pageHasKana(pw)"), "reader.js 需重新拼合");
});

const fnSource = (name) => {
  const start = SEL.indexOf(`function ${name}(`);
  assert.ok(start >= 0, `${name} 不存在`);
  const end = SEL.indexOf("\n}\n", start);
  return SEL.slice(start, end + 2);
};

test("_pageHasKana 按字符层数组身份缓存、换层重扫；_tapMarkIsJa 只认 jp 下划线", () => {
  const ctx = {
    _findVocabMarkAt: (pw, idx) => (pw.__vocabMarks || []).find((m) => m.at === idx) || null,
  };
  vm.createContext(ctx);
  vm.runInContext(fnSource("_tapMarkIsJa") + fnSource("_pageHasKana"), ctx);
  const pw = { __charBoxes: [{ c: "2" }, { c: "位" }, { c: " ", sp: true }, { c: "心" }, { c: "疾" }, { c: "患" }] };
  assert.equal(ctx._pageHasKana(pw), false, "整页没有假名");
  pw.__charBoxes.push({ c: "が" });   // 同一数组原地追加不会重扫（缓存按数组身份）
  assert.equal(ctx._pageHasKana(pw), false);
  pw.__charBoxes = pw.__charBoxes.slice();   // 换了字符层数组 → 重扫
  assert.equal(ctx._pageHasKana(pw), true);
  pw.__vocabMarks = [{ at: 3, jp: true }, { at: 0, jp: false }];
  assert.equal(ctx._tapMarkIsJa(pw, 3), true);
  assert.equal(ctx._tapMarkIsJa(pw, 0), false);
  assert.equal(ctx._tapMarkIsJa(pw, 1), false);
  assert.equal(ctx._pageHasKana(null), false);
});
