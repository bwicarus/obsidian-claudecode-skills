// 生词下划线画到原生正文上，但**判据只留一份**。
//
// 「这个词该不该画」不是一句 label 判断：它牵涉共享仓库的掌握事实、
// `__vocabOverride` 的本地覆盖、`__masteredLocal` 兜底，以及服务端
// `label_slug='mastered'` 的收敛顺序。复制到原生那侧必然漂移，表现是
// 「同一个词网页上不画、原生上画」—— 而这种不一致没人会立刻发现。
//
// 所以：判据抽成 `_vocabMarksForDisplay` 供两边共用，原生只负责画。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");

const CHARLAYER = read("_server_deploy/static/pdf/reader.src/08-charlayer.js");
const READER = read("_server_deploy/static/pdf/reader.js");
const DOCUMENT = read("ios/BWReader/App/ReaderNativePDFDocument.swift");
const WEBVIEW = read("ios/BWReader/App/ReaderWebView.swift");
const STYLES = read("_server_deploy/static/pdf/pdf-styles.css");

const body = (source, from, to) => source.slice(source.indexOf(from), source.indexOf(to));
const code = (source) =>
  source.split(/\r?\n/).filter((line) => !/^\s*(\/\/|\/\*|\*)/.test(line)).join("\n");

test("① 判据抽出来共用，网页那侧也改调它", () => {
  assert.match(CHARLAYER, /function _vocabMarksForDisplay\(marks\)/);
  const filter = body(CHARLAYER, "function _vocabMarksForDisplay(marks)",
                      "window.__bwReaderPageVocabMarks");
  for (const rule of ["_vocabularyStateMarkMastered", "__vocabOverride",
                      "__masteredLocal", "label_slug !== 'mastered'"]) {
    assert.ok(filter.includes(rule), `判据里少了 ${rule}`);
  }
  // 原来那段内联过滤必须真的被替换掉，不能两份并存。
  const render = body(CHARLAYER, "function renderVocabUnderlines(pw, marks)",
                      "window.refreshLocalVocabMarks");
  assert.match(render, /marks = _vocabMarksForDisplay\(marks\)/);
  assert.doesNotMatch(code(render), /__masteredLocal/,
    "渲染函数里还留着一份判据 —— 两份会各自漂移");
});

test("② 数据入口只取数，不碰 DOM", () => {
  const entry = body(CHARLAYER, "window.__bwReaderPageVocabMarks",
                     "function renderVocabUnderlines");
  assert.match(entry, /_vocabMarksForDisplay\(d\.vocab_marks/);
  assert.doesNotMatch(code(entry), /document\.|querySelector|createElement/,
    "取数入口不该碰 DOM：原生接管时页面根本没渲");
  assert.match(entry, /@interaction document\.page-overlay\.read/,
    "动态 fetch 要带交互标注，否则网络依赖门禁会拦");
  assert.match(READER, /window\.__bwReaderPageVocabMarks/, "改完 reader.src 要拼合");
});

test("③ 原生只画，颜色与粗细跟 CSS 同源", () => {
  assert.match(DOCUMENT, /enum ReaderNativeVocabPalette/);
  // 四档取值必须与 .vocab-underline.m-* 对得上；掌握档不画。
  for (const [slug, hex] of [["new", "f59e0b"], ["learning", "fb923c"],
                             ["seen", "facc15"], ["known", "a3e635"]]) {
    assert.match(STYLES, new RegExp(`m-${slug}[^\\n]*${hex}`, "i"),
      `CSS 里 ${slug} 的颜色变了，原生那份要跟着改`);
    assert.match(DOCUMENT, new RegExp(`case "${slug}"`), `原生少了 ${slug} 档`);
  }
  assert.match(STYLES, /m-mastered \{display:none\}/);
  assert.match(DOCUMENT, /guard thickness > 0 else \{ continue \}/,
    "掌握档在网页是 display:none，原生对应不画");
  // 画在字底：与网页那侧 y1+1px 同一位置。
  assert.match(DOCUMENT, /y: rect\.maxY/);
});

test("④ 壳只搬运，不在这侧判该不该画", () => {
  const refresh = body(WEBVIEW, "private func refreshNativeVocabMarks()",
                       "/// 把可见页的屏幕矩形推给墨迹层");
  assert.match(refresh, /__bwReaderPageVocabMarks/);
  assert.doesNotMatch(code(refresh), /mastered|vocabOverride|label_slug ==/,
    "判据不该出现在壳这侧");
  assert.match(refresh, /position\.visiblePages/, "只取可见页");
  assert.match(refresh, /r\[0\] \/ chars\.width/, "点坐标要换成归一化，viewRect 才能算");
  // 翻页后要重取，否则新页没有下划线。
  assert.match(WEBVIEW, /self\.refreshNativeVocabMarks\(\)/);
});
