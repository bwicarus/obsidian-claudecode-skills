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

test("④ 词锚描边由原生补，因为网页那份画不出来", () => {
  // .pgmark 是网页在 pgbind-layer 里画的，要 __charBoxes；接管后 item.markers
  // 是空的 —— 这张卡钉在正文哪一段，屏幕上完全看不出来。
  //
  // ⚠ 但只在**没有原生正文**时由这一层补。原生 PDFKit 接管后，框改由
  // ReaderNativePDFViewport 画在跟随页面滚动的那一层（见下面 ⑤）——
  // 两份同时画，滚动时就是残影，点击还落在慢半拍的那份上（2026-09-22 实报）。
  const fallback = CARDS.slice(CARDS.indexOf("if reader.nativePDFDocument == nil,"));
  assert.match(fallback, /item\.markers\.isEmpty, item\.bound, let boxes = nativeMarkers, !boxes\.isEmpty/);
  assert.match(fallback, /RoundedRectangle\(cornerRadius: 3\)/);
  // 不猜序号：序号是网页排的，猜一个可能跟别处对不上。
  assert.doesNotMatch(fallback.split("\n").filter((l) => !/^\s*\/\//.test(l)).join("\n"),
    /marker\.number|ordinal/);
  // 原生解不出绑定时返回空数组而不是 nil —— 那是「确实没钉在正文上」。
  assert.match(WEBVIEW, /let rects = geometry\.bindingRects\.map \{/);
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
  assert.match(PDFDOC, /return value\.rects\.isEmpty \? nil : CardMarker\(id: id, rects: value\.rects\)/);
  const overlay = PDFDOC.slice(PDFDOC.indexOf("var cardMarkers: [(id: String, rects: [CGRect])]"));
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
