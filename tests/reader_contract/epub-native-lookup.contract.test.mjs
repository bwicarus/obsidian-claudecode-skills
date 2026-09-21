// EPUB 也给原生面板供数 —— 用**同名同形状**的入口。
//
// App 的原生选区动作调的是 `window.__bwReaderLookupData`，它不关心自己站在哪个
// 阅读器上。PDF 那份在 reader.src/15-phrase-wordpop.js，EPUB 这份在 epub-html.js。
// 同名同形状 = 壳那边一行都不用改。
//
// ⚠ 真正的风险是**判据被复制**：英/日分流一旦在两个阅读器里各写一遍，表现会是
// 「同一个词在 PDF 里查中日词典、在 EPUB 里查英文词典」，而且只有那一类词才露馅。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");

const EPUB = read("_server_deploy/static/pdf/epub-html.js");
const WORDPOP = read("_server_deploy/static/pdf/rc-wordpop.js");
const PDF = read("_server_deploy/static/pdf/reader.src/15-phrase-wordpop.js");

const code = (source) =>
  source.split(/\r?\n/).filter((line) => !/^\s*(\/\/|\/\*|\*|\/\/\/)/.test(line)).join("\n");
const entry = () => EPUB.slice(EPUB.indexOf("window.__bwReaderLookupData = async function"));

test("① 两个阅读器用同一个入口名", () => {
  assert.match(EPUB, /window\.__bwReaderLookupData = async function/);
  assert.match(PDF, /window\.__bwReaderLookupData = async function/);
});

test("② 英/日分流不复制 —— 交给共享层", () => {
  const fn = entry();
  assert.match(fn, /RC\.wordpop\.lookupData\(text, context,/);
  // ⚠ EPUB 这份里不许出现语言判据：它在 rc-wordpop 的 _isJaWord 一处。
  assert.doesNotMatch(code(fn), /_isJaWord|dict-jp|dict-quick/);
  assert.match(WORDPOP, /function _lookupFetchRaw\(word\) \{\s*\n\s*if \(_isJaWord\(word\)\)/);
});

test("③ 必须把 file/langs 交给共享层", () => {
  const fn = entry();
  // 原生那条路不经 show()，_ctx 里还是上一本书甚至是空的。langs 决定英/日分流，
  // 拿错就是"同一个词在两个表面上查了不同的词典"。
  assert.match(fn, /file: FREL, page: 0, langs: bookLangsArr\(\)/);
  const shared = WORDPOP.slice(WORDPOP.indexOf("function lookupData(word, ctx, opts)"));
  assert.match(shared, /if \(opts\.langs\) _ctx\.langs = opts\.langs/);
});

test("④ 临时覆盖 _ctx 之后要整个恢复", () => {
  const shared = WORDPOP.slice(WORDPOP.indexOf("function lookupData(word, ctx, opts)"),
                               WORDPOP.indexOf("function _pruneJapaneseExampleZhPending"));
  assert.match(shared, /var saved = _ctx;/);
  assert.match(shared, /_ctx = Object\.assign\(\{\}, _ctx\)/, "改的是副本");
  assert.match(shared, /finally\(function \(\) \{ try \{ _ctx = saved; \}/);
  // ⚠ 只恢复 ctx 一个字段的话，别人设的 file 会被我们永久改掉。
  assert.doesNotMatch(code(shared), /savedCtx/);
});

test("⑤ 解释的短选区换整句，与 PDF 同一条规则", () => {
  const fn = entry();
  assert.match(fn, /text\.length < 50 && context && context\.length > text\.length/);
  assert.match(PDF, /text\.length < 50 && context && context\.length > text\.length/);
});

test("⑥ 返回的形状与 PDF 那侧对得上", () => {
  const fn = entry();
  // 壳与面板按这些键取值；少一个就是某一栏永远空着，而且不会报错。
  for (const key of ["mode", "jp", "word", "lemma", "reading", "kanji",
                     "phonetic", "translation", "definition", "mastered"]) {
    assert.match(fn, new RegExp(`\\b${key}:`), "少了 " + key);
  }
});
