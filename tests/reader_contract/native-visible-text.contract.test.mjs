// 助手要知道「用户此刻看得见哪一段」。
//
// `_visibleText()` 是从 .page-wrap 的 __charBoxes 拼的 —— 原生接管后一页都没有，
// 于是它拿到**空字符串**。后端系统提示里「紧扣可见段落」那条就此失效：回答变泛，
// 而屏幕上没有任何异常，没有人看得出原因。这是一处纯粹的静默降级 ——
// 不像崩溃或空白页，它只是让答案悄悄变差。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");

const ASSISTANT = read("_server_deploy/static/pdf/reader.src/25-assistant.js");
const READER = read("_server_deploy/static/pdf/reader.js");
const DOC = read("ios/BWReader/App/ReaderNativePDFDocument.swift");
const WEBVIEW = read("ios/BWReader/App/ReaderWebView.swift");

const body = (source, from, to) => source.slice(source.indexOf(from), source.indexOf(to));

test("① 接管时改用原生推过来的正文", () => {
  const fn = body(ASSISTANT, "function _visibleText()", "function _pageText");
  assert.match(fn, /window\.__bwNativeVisibleText/);
  assert.match(fn, /RC\?\.readerNavigation\?\.nativeViewport/);
  // 截断（1000 字 + 省略号）只留一处：两边各截一次会得到两种长度。
  assert.match(fn, /nativeText\.slice\(0, 1000\) \+ '…'/);
  assert.match(READER, /__bwNativeVisibleText/, "改完 reader.src 要拼合");
});

test("② 整页正文用同一个选区核心的阅读顺序", () => {
  const fn = body(DOC, "func pageText(_ page: Int) -> String?", "func setVocabSentences");
  assert.match(fn, /core\.range\(from: 0, to: chars\.chars\.count - 1\)/);
  // ⚠ 自己按字符数组拼字符串会在表格/多栏页上给出另一种顺序 ——
  // 与网页那侧 _charsRangeToText(chars, 0, n-1) 必须是同一套规则。
  assert.doesNotMatch(fn.split("\n").filter((l) => !/^\s*\/\//.test(l)).join("\n"),
    /chars\.chars\.map|reduce/);
  assert.match(fn, /if let cached = pageTexts\[page\]/, "一页的正文不会变，缓存起来");
});

test("③ 跟着滚动重推，且只推可见的几页", () => {
  const publish = body(WEBVIEW, "func publishNativeVisibleText()", "func publishNativeInkSurfaces()");
  assert.match(publish, /document\.position\.visiblePages\.prefix\(4\)/);
  assert.match(publish, /prefix\(4000\)/, "别把整本书推过去");
  // 与墨迹表面同一个节流窗口：滚动时布局回调每帧都来。
  const schedule = WEBVIEW.slice(WEBVIEW.indexOf("private func scheduleNativeInkSurfacePublish"),
                                WEBVIEW.indexOf("private func scheduleNativeInkSurfacePublish") + 900);
  assert.match(schedule, /publishNativeVisibleText\(\)/);
});
