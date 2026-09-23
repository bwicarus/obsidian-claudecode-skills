// 原生正文接管时，网页层**不再批量渲染**（这是崩溃的止血点）。
//
// 用户 2026-09-21：「旧的和新的一起使用经常会崩溃」。原因看得见：原生接管后
// 网页层仍在渲页 —— 同一本书被 PDF.js 和 PDFKit 各渲一遍，PDF 数据在内存里
// 也是两份。大书上被系统杀掉完全说得通。
//
// 字符定位也直接读 PDFKit 数据；App 不再通过按需渲染隐藏单页获得坐标。
// 浏览器保留自己的导航和文字层路径，App 原生读取失败不能启用网页出图兜底。
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

test("③ App 定位只读原生字符数据，不创建网页页图", async () => {
  const onDemand = HIGHLIGHT.slice(HIGHLIGHT.indexOf("async function _pdfExactTextPage"),
                                   HIGHLIGHT.indexOf("async function _pdfWaitForHighlightVisible"));
  assert.doesNotMatch(code(onDemand), /_renderPageInto|createElement/);
  assert.match(code(onDemand), /window\.goToPage\(page\)/,
    "非原生接管时仍走原来的导航，不改既有行为");
  assert.match(code(RENDER), /const nativeOwner = window\.RC\?\.readerNavigation\?\.nativeViewport;/);
  const forbidden = () => { assert.fail("App 文字定位不应读取 DOM 或触发导航/出图"); };
  const factory = new Function("window", "document", "pdfDoc", "_mapCharBoxes",
    `const _NATIVE_LOCAL_PDF = true; ${onDemand}; return _pdfExactTextPage;`);
  const requests = [];
  const chars = [{ c: "字", left: 10, top: 20 }];
  let response = { ok: true, chars, revision: "rev1", pageWidth: 100, pageHeight: 200 };
  const window = { goToPage: forbidden, webkit: { messageHandlers: { bwNativeReaderGeometry: {
    async postMessage(request) { requests.push(request); return response; }
  } } } };
  const resolve = factory(window, { querySelector: forbidden, createElement: forbidden }, { numPages: 3 }, (value) => value);
  const result = await resolve(2);
  assert.deepEqual(requests, [{ action: "characters", page: 2 }]);
  assert.equal(result.__nativeSource, true);
  assert.equal(result.__charBoxes, chars);
  assert.equal(result.__pageWPt, 100);
  assert.equal(result.__pageTextRevision, "rev1");
  response = { ok: false };
  await assert.rejects(resolve(2), /TEXT_LAYER_UNAVAILABLE/);
  delete window.webkit.messageHandlers.bwNativeReaderGeometry;
  await assert.rejects(resolve(2), /GEOMETRY_UNAVAILABLE/);
});

test("④ 改完 reader.src 要拼合 —— 不然线上还是旧的", () => {
  assert.match(READER, /nativeViewport\)\s*return;/);
  const from = HIGHLIGHT.indexOf("async function _pdfExactTextPage");
  const to = HIGHLIGHT.indexOf("async function _pdfWaitForHighlightVisible");
  assert.ok(READER.includes(HIGHLIGHT.slice(from, to)), "原生字符数据入口必须同步到生成物");
});
