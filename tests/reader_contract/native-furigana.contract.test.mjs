// 振假名画到原生正文上：数据原生本来就有，缺的是"哪些词不注音"和画法。
//
// `page-chars` 的响应里本来就带 furigana，Swift 的 NativeBookOCRPageCharacters
// 也早就有这个字段 —— 所以不必再取一次。真正缺的两件：
//   · 已掌握的词不注音（那份事实在 page-overlay 的 mastered_furi 里）
//   · 字号与位置：必须与网页那侧 `_makeRubySpan` **同一套算法**，否则同一本书
//     在两个表面上注音大小不一样
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");

const RUBY = read("_server_deploy/static/pdf/reader.src/09-ruby.js");
const CHARLAYER = read("_server_deploy/static/pdf/reader.src/08-charlayer.js");
const DOCUMENT = read("ios/BWReader/App/ReaderNativePDFDocument.swift");
const WEBVIEW = read("ios/BWReader/App/ReaderWebView.swift");
const MODELS = read("ios/BWReader/App/NativeBookOCRModels.swift");

const body = (source, from, to) => source.slice(source.indexOf(from), source.indexOf(to));

test("① 数据原生本来就有，不再取第二遍", () => {
  assert.match(MODELS, /let furigana: \[NativeBookOCRFurigana\]/,
    "字符层响应里就带着它");
  const entry = body(DOCUMENT, "func furigana(page: Int)", "func characterPageSize(");
  assert.match(entry, /chars\.furigana/, "直接用字符层里的那份");
});

test("② 已掌握的词不注音，且「关着」与「没有已掌握词」分得开", () => {
  assert.match(RUBY, /mastered && it\.wd && mastered\.has\(it\.wd\)/,
    "网页那侧的规则");
  const entry = body(DOCUMENT, "func furigana(page: Int)", "func characterPageSize(");
  assert.match(entry, /mastered\.contains\(word\)/, "原生要用同一份 mastered 集");
  assert.match(entry, /furiganaEnabled\[page\] == true/,
    "振假名整体关着时一个都不画 —— 那跟「这一页没有已掌握的词」不是一回事");
  // 数据来源：与生词下划线同一次取数，不额外打请求。
  assert.match(CHARLAYER, /masteredFuri: _rubyEnabled\(\) \? \(d\.mastered_furi/);
  assert.match(WEBVIEW, /setFuriganaMastered\(/);
});

test("③ 字号与位置沿用网页那套算法", () => {
  // 网页：fs = max(7, min(h*0.36, w/rt.length))，top = max(0, y0 - fs*0.34)
  assert.match(RUBY, /Math\.max\(7, Math\.min\(h \* 0\.36, w \/ Math\.max\(1, rt\.length\) \* 1\.0\)\)/);
  assert.match(RUBY, /Math\.max\(0, y0 - fs \* 0\.34\)/);
  // 振假名现在画在每页自己的 overlay 里（ReaderNativePDFDocument.drawDecorations），
  // 不再是视口上那张 Canvas —— 那张 Canvas 滚动时慢一帧，留残影。
  const draw = body(DOCUMENT, "for item in furigana(page: number)",
                    "for stroke in ink[number]");
  assert.match(draw, /max\(7, min\(h \* 0\.36, w \/ CGFloat\(max\(1, rt\.count\)\)\)\)/,
    "字号算法要与网页一致");
  assert.match(draw, /max\(frame\.minY, box\.minY - fontSize \* 0\.34\)/,
    "位置算法要与网页一致");
});
