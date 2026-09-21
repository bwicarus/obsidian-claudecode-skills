// 搜索跳转后，命中要在原生正文上亮出来。
//
// 网页那条路 `_highlightSearchResultsOnPage` 要 `__charBoxes`，而原生接管时那一页
// 根本没渲 —— 它会轮询 4.8 秒、然后把待办标记**清掉**，命中永远不亮，而且看起来
// 像"搜索没找到"。原生这侧有自己的字符层，自己找自己画。
//
// 三条：
//   ① 网页那侧在接管时不消费标记（留给原生）
//   ② 标记经数据入口交给原生，**取走即清** —— 只亮一次，不是每次翻页重亮
//   ③ 原生按同一口径找子串并按行合并矩形
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");

const SEARCH = read("_server_deploy/static/pdf/reader.src/11-search.js");
const CHARLAYER = read("_server_deploy/static/pdf/reader.src/08-charlayer.js");
const READER = read("_server_deploy/static/pdf/reader.js");
const DOCUMENT = read("ios/BWReader/App/ReaderNativePDFDocument.swift");
const WEBVIEW = read("ios/BWReader/App/ReaderWebView.swift");

const body = (source, from, to) => source.slice(source.indexOf(from), source.indexOf(to));

test("① 接管时网页不消费搜索标记", () => {
  const apply = body(SEARCH, "function _applyPendingSearchHighlight(tries)",
                     "function _buildRectsFromCharRange");
  assert.match(apply, /nativeViewport\) return;/,
    "不早返回的话，4.8 秒后标记被清掉，命中永远不亮");
  // 早返回必须排在"找 wrap / 判 dataset.loaded"之前，否则照样会走进轮询。
  // ⚠ 先剥注释再比位置：说明这道闸为什么存在，必然会提到 dataset.loaded，
  //   照字面比会把注释当成代码（这个坑今天踩到第三次了）。
  const code = apply.split(/\r?\n/).filter((line) => !/^\s*\/\//.test(line)).join("\n");
  const gate = code.indexOf("nativeViewport");
  const probe = code.indexOf("dataset.loaded");
  assert.ok(gate > 0 && probe > 0 && gate < probe, "这道闸要挡在轮询之前");
});

test("② 标记交给原生，取走即清", () => {
  assert.match(CHARLAYER, /searchQuery: _takePendingSearchQuery\(page\)/);
  const take = body(CHARLAYER, "function _takePendingSearchQuery(page)",
                    "function renderVocabUnderlines");
  assert.match(take, /Number\(ph\.page\) !== Number\(page\)/, "只给目标页");
  assert.match(take, /window\._pendingSearchHighlight = null/,
    "取走即清 —— 不清的话每次翻页都会重亮一遍");
  assert.match(READER, /_takePendingSearchQuery/, "改完 reader.src 要拼合");
  assert.match(WEBVIEW, /document\.highlightSearchHits\(query: query, page: page\)/);
});

test("③ 原生按同一口径找子串并按行合并", () => {
  const hits = body(DOCUMENT, "func highlightSearchHits(query: String, page: Int)",
                    "private static func mergeRowRects");
  assert.match(hits, /lowercased\(\)/, "大小写不敏感 —— 与网页一致");
  assert.match(hits, /while let found = text\.range\(of: needle/, "找**所有**命中，不是第一处");
  assert.match(hits, /seconds\(6\)/, "几秒后自动淡掉，与网页同一时长");
  const merge = body(DOCUMENT, "private static func mergeRowRects",
                     "/// 这一页的点坐标尺寸");
  assert.match(merge, /glyph\.sp == 0/, "跳过空白字符");
  assert.match(merge, /cur\.height \* 0\.6/, "同行判据与网页 _buildRectsFromCharRange 一致");
});
