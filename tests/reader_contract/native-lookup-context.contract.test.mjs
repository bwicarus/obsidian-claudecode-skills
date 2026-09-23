// 查词 / 解释要带上**所在整句**。
//
// 这一条不是"少了个可选参数"，是结果会悄悄变差的那一类：
// · 词典：一词多义时，给出的那条释义跟用户正在读的这句话未必是同一个意思；
// · 解释：短选区不换成整句，AI 拿到的是 "at" 这样的碎词，回答基本都是
//   「内容不完整、请提供上下文」。
// 两边都照样返回、不报错，所以只看屏幕是看不出来的。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");

const WORDPOP = read("_server_deploy/static/pdf/reader.src/15-phrase-wordpop.js");
const READER = read("_server_deploy/static/pdf/reader.js");
const DOC = read("ios/BWReader/App/ReaderNativePDFDocument.swift");
const WEBVIEW = read("ios/BWReader/App/ReaderWebView.swift");

const body = (source, from, to) => source.slice(source.indexOf(from), source.indexOf(to));
const code = (source) =>
  source.split(/\r?\n/).filter((line) => !/^\s*(\/\/|\/\*|\*|\/\/\/)/.test(line)).join("\n");

test("① 整句一路传到面板，不是拿选中串充数", () => {
  assert.match(DOC, /var onLookup: \(\(Int, String, String, String\) -> Void\)\?/);
  assert.match(DOC, /self\?\.onLookup\?\(number, value\.text, value\.sentence, mode\)/);
  const open = body(WEBVIEW, "private func openNativeLookup(page: Int",
                    "/// 选区菜单里点了划线");
  // ⚠ 以前这里是 trimmed.prefix(320)：等于告诉词典"这个词的上下文就是这个词"。
  assert.doesNotMatch(code(open), /context: String\(trimmed\.prefix\(320\)\)/);
  assert.match(open, /whole\.isEmpty \? trimmed : whole/, "整句取不到才退回选中串");
});

test("② 解释的短选区换整句，与网页同一条规则", () => {
  const entry = body(WORDPOP, "if (request.mode === 'explain')", "if (request.mode === 'dict-full')");
  assert.match(entry, /text\.length < 50 && context && context\.length > text\.length/);
  assert.match(entry, /body: \{text: subject, context\}/);
  // 网页那侧的规则还在原处（这条测试防的是两边分头改）。
  const MISC = read("_server_deploy/static/pdf/reader.src/21-misc-ai.js");
  assert.match(MISC, /if \(sLen < 50\)/);
  assert.match(MISC, /if \(sentence && sentence\.length > sLen\) explainText = sentence/);
  assert.match(READER, /let subject = text;/, "改完 reader.src 要拼合");
});
