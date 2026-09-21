// 原生正文上 Pencil 要能画：墨迹表面由原生几何发布。
//
// 网页那侧的墨迹表面是量 `.page-wrap` 上 `__inkCanvas` 的
// `getBoundingClientRect()` 得来的 —— 也就是**必须那一页在网页里渲出来**。
// 原生接管后网页不渲页，于是一个 surface 都没有，Pencil 在原生正文上**直接
// 画不了**（不是画偏，是根本落不下笔）。
//
// 但墨迹本身并不需要那块画布：笔画存在 `pw.__inkStrokes` 上，落库走 byPage[num]，
// 画出来的事归 PDFKit。所以接管时只需要把"这一页在屏幕上的位置"换个来源。
//
// 四条：
//   ① 接管时资格判定不再要求 __inkCanvas / dataset.loaded
//   ② 接管时表面来自原生推过来的矩形，id 仍是 page:N（落库那一路不用改）
//   ③ 原生按可见页算屏幕矩形，并在布局变化时重推（否则画在上一帧的位置）
//   ④ 非接管路径一行不变
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");

const TAIL = read("_server_deploy/static/pdf/pdf-tail.js");
const WEBVIEW = read("ios/BWReader/App/ReaderWebView.swift");

const body = (source, from, to) => source.slice(source.indexOf(from), source.indexOf(to));

test("① 接管时不再要求画布和已渲染", () => {
  const eligible = body(TAIL, "function eligible(pw)", "function resolveSurface(id)");
  assert.match(eligible, /if \(nativeInkTakeover\(\)\)/);
  // 只截**接管那一个分支**：再往后是非接管路径，那里当然还要画布。
  const takeover = eligible.slice(
    eligible.indexOf("if (nativeInkTakeover())"),
    eligible.indexOf("if (!pw.__inkCanvas) return false;"));
  assert.ok(takeover.length > 60, "找不到接管分支");
  assert.doesNotMatch(takeover, /__inkCanvas/,
    "接管分支里不该再要画布 —— 网页不渲页时它永远不存在");
  assert.doesNotMatch(takeover, /dataset\.loaded === '1'/);
  // 插入页仍按原样要画布：它是网页自己画的，不归 PDFKit。
  assert.match(eligible, /pdf-upage'\)\) \{\s*\n\s*return !!pw\.__inkCanvas/);
});

test("② 接管时表面来自原生，id 仍是 page:N", () => {
  const describe = body(TAIL, "function describe()", "window.__bwNativeInkSurfacesChanged");
  assert.match(describe, /window\.__bwNativeInkSurfaces/);
  assert.match(describe, /\/\^page:\\d\+\$\//,
    "id 形状要校验：落库那一路按它反查页号");
  assert.match(describe, /document\.querySelector\('\.page-wrap\[data-page-num="'/,
    "仍要映回 pw —— resolveSurface / _inkStrokesOf / _inkScheduleSave 都按它走");
  // 非接管路径必须原样保留。
  assert.match(describe, /pw\.__inkCanvas\.getBoundingClientRect\(\)/);
});

test("③ 原生按可见页算屏幕矩形，并在布局变化时重推", () => {
  const publish = body(WEBVIEW, "func publishNativeInkSurfaces()", "/// 原生正文接管时拖动页卡");
  assert.match(publish, /document\.position\.visiblePages/, "只算可见页，不是整本");
  assert.match(publish, /"id": "page:\\\(page\)"/);
  assert.match(publish, /local\.minX \/ webView\.bounds\.width/,
    "按 webView 归一化 —— 墨迹层的触点也是按同一个框归一化的");
  assert.match(publish, /__bwNativeInkSurfacesChanged/, "推完要让网页那边重发 layout");

  assert.match(WEBVIEW, /document\.onGeometry = \{ \[weak self\] in[\s\S]{0,160}scheduleNativeInkSurfacePublish/,
    "布局一变就要重推，否则滚动后 Pencil 画在上一帧的位置");
  const schedule = body(WEBVIEW, "private func scheduleNativeInkSurfacePublish()",
                        "func publishNativeInkSurfaces()");
  assert.match(schedule, /milliseconds\(180\)/, "合并成一次，别逐帧过 WebKit");
});

test("④ 换书/关闭时把待推任务取消掉", () => {
  const invalidate = body(WEBVIEW, "private func invalidateNativePDFDocument()",
                          "/// 把原生 PDF 主阅读区挂到界面上");
  assert.match(invalidate, /nativeInkSurfaceTask\?\.cancel\(\)/,
    "不取消的话，上一本书的页面矩形会推给下一本");
  assert.match(invalidate, /nativeProjectionRefreshTask\?\.cancel\(\)/);
});
