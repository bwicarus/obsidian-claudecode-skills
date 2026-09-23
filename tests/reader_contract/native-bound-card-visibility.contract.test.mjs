// 词锚卡（钉在正文某一段上的卡片）在原生接管时不能永久隐身。
//
// 这是用户报的「卡片在插入时要求目标页在显示」的真实形态：
// 卡确实存下来了（__pageBindPersist 的 deferred 路径 2026-08-31 就支持了），
// 但 ensureMounted 在 binding 解不出来时会**先把它藏起来等重试** ——
// 那次重试由 08-charlayer 在 __charBoxes 挂上后触发，而接管后网页**不再渲页**，
// __charBoxes 永远不会挂上。于是卡片一直藏着：存了、没报错、就是不出现。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");

const STICKY = read("_server_deploy/static/pdf/rc-stickynote.js");
const BINDCARD = read("_server_deploy/static/pdf/reader.src/34-bindcard.js");
const CARDS = read("ios/BWReader/App/ReaderNativePageCards.swift");
const WEBVIEW = read("ios/BWReader/App/ReaderWebView.swift");
const PDFDOC = read("ios/BWReader/App/ReaderNativePDFDocument.swift");
const SCRIPT = read("ios/BWReader/App/ReaderNativeConversationScript.swift");

const body = (source, from, to) => source.slice(source.indexOf(from), source.indexOf(to));

test("① 插入本身不要求目标页渲染", () => {
  const persist = body(BINDCARD, "window.__pageBindPersist = function",
                       "var placement = deferred");
  assert.match(persist, /g\.why === 'page-not-rendered'/);
  assert.match(persist, /if \(!g \|\| \(!g\.ok && !deferred\)\)/,
    "只有 page-not-rendered 走 deferred，其它失败仍 fail closed");
  assert.match(BINDCARD, /var placement = deferred \? \{ deferredPdfPage: g\.page \} : _bindScreenPoint\(g\)/);
});

test("② 接管时不再「藏着等一个永远不来的重试」", () => {
  const mount = body(STICKY, "var _tmp = res && (res.why === 'page-not-rendered'",
                     "// 全量重挂/重定位");
  assert.match(mount, /RC\.readerNavigation\.nativeViewport/);
  assert.match(mount, /_tmp = false/);
  // 顺序要紧：先算 _tmp、再按接管翻掉，最后才决定显不显示。
  assert.ok(mount.indexOf("nativeViewport") < mount.indexOf("if (!_tmp) ctl.root.style.display"));
});

test("③ 藏着＝原生也看不见（visible 是从 DOM 量的）", () => {
  // nativePlacementState 的 visible 来自 getBoundingClientRect；display:none 的
  // 卡片量出来是 0×0，于是整张卡在原生那侧也不画 —— 这是这条链的关键一环。
  assert.match(STICKY, /visible: rect\.width > 0 && rect\.height > 0/);
  assert.match(CARDS, /if item\.visible && rect\.maxX > 0/);
});

test("④ 页卡层不再自己补画词锚描边", () => {
  // 曾经在"网页标记为空"时由这一层用原生几何补一圈描边。那条分支只在
  // nativePDFDocument == nil 时进，而那时原生几何必然拿不到 —— 它是死的。
  // 原生正文下框统一由 PDFKit 页内 overlay 画（⑤）。
  assert.doesNotMatch(CARDS, /let boxes = nativeMarkers/);
  assert.doesNotMatch(WEBVIEW, /func nativePageMarkerRects\(/);
});

test("⑤ 锁定框画在**每一页自己的 overlay view** 里，跟着页面滚", () => {
  // ⚠ 这一条防的是 2026-09-22 连报三次的同一个现象：框"没跟紧画面、有延迟、
  // 有残影"，而且"点击后根本打不开卡片"。
  //
  // 前两版都错在"每帧重算位置"：① ReaderNativePageCards 按**窗口坐标**摆位；
  // ② ReaderNativePDFViewport 的 Canvas 按 geometryRevision 重画。两者都是
  // 在追 PDFKit 的滚动，必然慢半拍、留残影。
  //
  // 正确的位置是 pdfView(_:overlayViewFor:) 给的那个 overlay —— 它是**页面的
  // 子视图**，跟着页面一起滚，一帧都不用重算。
  assert.match(PDFDOC, /func cardMarkers\(page: Int\) -> \[CardMarker\]/);
  // 给归一化框，由 overlay 自己 project —— 给 view 坐标就又回到"每帧重算"。
  assert.match(PDFDOC, /return CardMarker\(id: id, rects: value\.rects,/);
  const overlay = PDFDOC.slice(PDFDOC.indexOf("var cardMarkers: [ReaderNativePDFDocument.CardMarker]"));
  assert.match(overlay, /for marker in cardMarkers[\s\S]*project\?\(normalized\)/);
  // 点击也在这一层：命中的就是屏幕上看到的那个框。
  assert.match(overlay, /if let id = cardMarkerAt\(location\) \{ onOpenCard\?\(id\); return \}/);
  // 视口那一层不再参与（画与点都不在那儿）。
  const viewport = PDFDOC.slice(PDFDOC.indexOf("struct ReaderNativePDFViewport"));
  assert.doesNotMatch(
    viewport.split(String.fromCharCode(10)).filter((l) => !/^\s*\/\//.test(l)).join(String.fromCharCode(10)),
    /cardMarkers|onOpenCard/,
  );
});

test("⑥ 原生正文下，页卡层一个网页标记都不画（那份按窗口坐标摆，必然拖影）", () => {
  // 2026-09-23 用户截图：框"太细颜色太浅、滚动有残影"——那条细青线加序号正是
  // 这层画的网页标记，它在原生正文模式下原来没有被挡住。
  const placement = CARDS.slice(CARDS.indexOf("private func placement("), CARDS.indexOf("private func cardRect("));
  assert.match(placement, /if reader\.nativePDFDocument == nil \{\s*webMarkers\(item, frame: frame\)\s*\}/);
});

test("⑦ 原生解锚一律用便签 id，不用界面上的 placement id", () => {
  // placement id 是 'placement-' + hash(...)，跟便签 id 永远对不上：拿它比，
  // 页内锁定框点了就是"还没加载好"，卡身也一直退回网页坐标。
  assert.match(CARDS, /nativePageCardGeometry\(\s*id: item\.noteID/);
  assert.match(WEBVIEW, /\$0\["id"\] as\? String == item\.noteID/);
  assert.match(WEBVIEW, /placements\.first\(where: \{ \$0\.noteID == noteID \}\)/);
  assert.match(SCRIPT, /return \[\{ id, noteId: item\.id,/);
  assert.match(SCRIPT, /return \[\{ id, noteId: item\.id, source: 'note',/);
});

test("⑧ 锁定框观感照原版 _bindTone：色调混深底，展开态加深 + 外晕，带序号", () => {
  assert.match(PDFDOC, /border = Self\.mix\(tone, 0\.60, Self\.hex\(0x2a2440\)\)/);
  assert.match(PDFDOC, /ink = Self\.mix\(tone, 0\.22, Self\.hex\(0x14101f\)\)/);
  assert.match(PDFDOC, /func numberedMarkers\(\)/);
});
