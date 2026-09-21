// AI 精确划线在原生接管时也不需要网页渲染。
//
// 这是"拆掉双份渲染"的最后一处：`__bwReaderHighlightExactText` 原本靠
// `_pdfExactTextPage` 取 `__charBoxes`，而那要求目标页**在网页里渲出来**。
// 原生接管正文后网页不再批量渲染，于是"AI 划线"反过来会把网页渲染重新变成
// 必需品 —— 正好抵消掉止血。
//
// 现在改成：先问原生字符层要坐标（`bwNativeReaderGeometry` 通道），拿到就直接
// 落本地库；原生说不可用或没命中，才退回网页那条老路。
//
// 三条必须同时成立，缺一这条链就废：
//   ① 网页入口先走原生，且**失败要退回**而不是当成"这段文字不在书里"
//   ② 壳这侧把"没挂原生正文"和"没定位到"分开回答
//   ③ 原生定位返回的是**点坐标**，与存储同一口径
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");

const HIGHLIGHT = read("_server_deploy/static/pdf/reader.src/17-highlight.js");
const READER = read("_server_deploy/static/pdf/reader.js");
const WEBVIEW = read("ios/BWReader/App/ReaderWebView.swift");
const DOCUMENT = read("ios/BWReader/App/ReaderNativePDFDocument.swift");

const body = (source, from, to) => source.slice(source.indexOf(from), source.indexOf(to));

test("① 网页入口先走原生，拿不到要退回而不是下结论", () => {
  const entry = body(HIGHLIGHT, "window.__bwReaderHighlightExactText",
                     "window.__bwReaderValidateExactSource");
  assert.match(entry, /_nativeExactHighlight\(request, colors\[request\.color\]\)/,
    "要先问原生");
  assert.match(entry, /if \(viaNative\) return viaNative;/);
  assert.match(entry, /_pdfExactTextPage\(request\.target\.page\)/,
    "退路必须还在：原生不可用时仍要能划");

  const helper = body(HIGHLIGHT, "async function _nativeExactHighlight",
                      "window.__bwReaderHighlightExactText");
  // ⚠ 三处 return null 都是"退回"，不是"失败"。把它们改成 throw，就会让
  //   桌面/扩展表面（根本没有这个通道）直接划不了线。
  assert.ok((helper.match(/return null;/g) || []).length >= 3,
    "通道不在 / 原生不可用 / 没命中，三种都要退回");
  assert.match(helper, /saved\.ok !== true\) throw new Error/,
    "但**落库被拒**是真失败，不能悄悄退回去再写一遍");
});

test("② 壳把「没挂原生」和「没定位到」分开回答", () => {
  const handler = body(WEBVIEW, "if message.name == nativeReaderGeometryMessageName {",
                       "guard message.name == nativeLocalNotesMessageName");
  assert.match(handler, /BW_NATIVE_GEOMETRY_UNAVAILABLE/, "没挂原生正文");
  assert.match(handler, /BW_NATIVE_GEOMETRY_MISS/, "挂了但这段文字没定位到");
  // 两者混成一个错误码，调用方就无法判断该退回还是该如实报"书里没有这段"。
  assert.notEqual(handler.indexOf("BW_NATIVE_GEOMETRY_UNAVAILABLE"),
                  handler.indexOf("BW_NATIVE_GEOMETRY_MISS"));
  // exact-shape 闸：多一个字段就拒，和这套代码里其它入站闸同口径。
  assert.match(handler, /Set\(body\.keys\) == \["action", "page", "text"\]/);
});

test("③ 原生定位返回点坐标，与存储同一口径", () => {
  const resolve = body(DOCUMENT, "func resolveBinding(page: Int, text: String)",
                       "private var characterPages");
  assert.match(resolve, /rect\.minX \* chars\.pageWidth/);
  assert.match(resolve, /rect\.maxY \* chars\.pageHeight/);
  assert.match(resolve, /"pageWidth": chars\.pageWidth/,
    "页面尺寸要一起给：存储按它换算显示位置");
  assert.match(resolve, /return nil/, "页还没取到字符层时返回 nil，由调用方决定退回");
});

test("④ 改完 reader.src 要拼合", () => {
  assert.match(READER, /async function _nativeExactHighlight/);
});
