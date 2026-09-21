// 原生正文接管时，网页层**不再批量渲染**（这是崩溃的止血点）。
//
// 用户 2026-09-21：「旧的和新的一起使用经常会崩溃」。原因看得见：原生接管后
// 网页层仍在渲页 —— 同一本书被 PDF.js 和 PDFKit 各渲一遍，PDF 数据在内存里
// 也是两份。大书上被系统杀掉完全说得通。
//
// 但**不能一刀切成永不渲染**：还有路径要靠网页的 `__charBoxes` 取坐标
// （AI 精确划线 / 来源校验）。所以规则是：批量渲染关掉，按需渲单页留着。
//
// ⚠ 顺带记一个查出来的事实：在这次改动**之前**，原生接管时 AI 精确划线就已经
//   是坏的 —— `_pdfExactTextPage` 走 `window.goToPage`，而 `renderPage` 见到
//   原生视口会直接转给原生并 return，网页那一页永远不渲，轮询 80×120ms 后必然
//   抛 TEXT_LAYER_UNAVAILABLE。这里一并修好。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");

const CONTINUOUS = read("_server_deploy/static/pdf/reader.src/07-continuous.js");
const HIGHLIGHT = read("_server_deploy/static/pdf/reader.src/17-highlight.js");
const RENDER = read("_server_deploy/static/pdf/reader.src/04-render.js");
const READER = read("_server_deploy/static/pdf/reader.js");

const code = (source) =>
  source.split(/\r?\n/).filter((line) => !/^\s*\/\//.test(line)).join("\n");

test("① 连续模式的观察者在原生接管时直接返回，不渲页", () => {
  const observer = CONTINUOUS.slice(
    CONTINUOUS.indexOf("_contIO = new IntersectionObserver"),
    CONTINUOUS.indexOf("rootMargin"));
  assert.match(code(observer), /nativeViewport\)\s*return;/,
    "观察者里没有这道闸，滚动时网页层会继续渲页跟 PDFKit 抢内存");
  // 闸必须在 forEach **之前**：放到里面等于每个 entry 都判一次，且容易被后来的
  // 改动挪进条件分支。
  const gate = observer.indexOf("nativeViewport");
  const loop = observer.indexOf("entries.forEach");
  assert.ok(gate > 0 && gate < loop, "这道闸要挡在整批之前");
});

test("② 首屏也不渲，但遮罩照撤", () => {
  const ready = CONTINUOUS.slice(CONTINUOUS.indexOf("const _afterTargetReady"),
                                 CONTINUOUS.indexOf("pdfLoadHide();") + 20);
  assert.match(code(ready), /if \(!window\.RC\?\.readerNavigation\?\.nativeViewport\)/,
    "首屏那次显式渲染也要门控");
  assert.match(ready, /pdfLoadHide\(\);/,
    "遮罩必须照撤 —— 不渲不等于没准备好，否则原生正文上会一直盖着加载遮罩");
});

test("③ 按需渲单页的路留着，且不经 goToPage", () => {
  const onDemand = HIGHLIGHT.slice(HIGHLIGHT.indexOf("async function _pdfExactTextPage"),
                                   HIGHLIGHT.indexOf("async function _pdfWaitForHighlightVisible"));
  assert.match(code(onDemand), /nativeViewport\)/, "原生接管时要走另一条路");
  assert.match(code(onDemand), /_renderPageInto\(page, ph\)/,
    "直接渲那一页；goToPage 会被 renderPage 转给原生并 return，页永远不渲");
  assert.match(code(onDemand), /window\.goToPage\(page\)/,
    "非原生接管时仍走原来的导航，不改既有行为");
  // renderPage 的早返回是上面那段推理的前提；它变了，这里的绕行就没必要了。
  assert.match(code(RENDER), /const nativeOwner = window\.RC\?\.readerNavigation\?\.nativeViewport;/);
});

test("④ 改完 reader.src 要拼合 —— 不然线上还是旧的", () => {
  assert.match(READER, /nativeViewport\)\s*return;/);
  assert.match(READER, /_renderPageInto\(page, ph\)/);
});
