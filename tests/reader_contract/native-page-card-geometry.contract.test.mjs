// 页卡在原生正文上的坐标必须来自 PDFKit 解锚，不能再用网页 DOM 推出来的那套。
//
// 网页那套 rect 是从 DOM 位置推的。原生接管正文后，网页既不渲页、滚动也不跟着
// 动 —— 那套坐标已经不对应屏幕上的任何东西。显示会错位，**拖动会更糟**：
// 落点被当成网页视口坐标交给网页的锚点解析器，而视口里根本没有那一页，卡会飞走。
//
// 四条：
//   ① 显示用 noteGeometry（PDFKit 解锚），拿不到才退回网页那条路
//   ② 跟着 PDF 滚动/缩放重画 —— SwiftUI 观察不到 PDFView 内部，要靠 geometryRevision
//   ③ 拖动写**页内**归一化锚点，不是网页视口坐标
//   ④ 改大小按卡片自身单位存，不是屏幕像素
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");

const CARDS = read("ios/BWReader/App/ReaderNativePageCards.swift");
const WEBVIEW = read("ios/BWReader/App/ReaderWebView.swift");
const DOCUMENT = read("ios/BWReader/App/ReaderNativePDFDocument.swift");
const SCRIPT = read("ios/BWReader/App/ReaderNativeConversationScript.swift");

const body = (source, from, to) => source.slice(source.indexOf(from), source.indexOf(to));

test("① 显示走 noteGeometry，拿不到才退回网页", () => {
  assert.match(CARDS, /reader\.nativePageCardGeometry\(/, "卡身位置");
  assert.match(CARDS, /\?\? reader\.nativePageCardRect\(/, "退路必须还在");
  const geometry = body(WEBVIEW, "func nativePageCardGeometry(", "func openNativeBoundCard(");
  // ⚠ 不传 presentationSize：placement.size 是除以网页视口的归一化值，当视图点用会把
  //   调过大小的卡算成 0.3×0.2 点。尺寸一律按便签自己的 w/h 换算。
  assert.match(geometry, /document\.noteGeometry\(note\)/);
  assert.doesNotMatch(geometry, /presentationSize: size/);
  assert.match(geometry, /return nil/, "没有原生几何时返回 nil，由调用方退回 —— 不猜");
  // 锚标记位置不在这层算：原生正文下由 PDFKit 页内 overlay 画（见
  // native-bound-card-visibility ⑤），这层按窗口坐标摆必然拖影。
});

test("② 缩放/重排才重算卡片，纯滚动一帧都不算（2026-09-23「卡顿」）", () => {
  // 逐帧变的量不许 @Published：观察这份文档的 SwiftUI 视图会被每一帧唤醒。
  assert.doesNotMatch(DOCUMENT, /@Published private\(set\) var geometryRevision/);
  assert.doesNotMatch(DOCUMENT, /@Published private\(set\) var position/);
  assert.match(DOCUMENT, /@Published private\(set\) var layoutRevision = 0/);
  assert.match(DOCUMENT, /if layoutKey != lastLayoutKey \{ lastLayoutKey = layoutKey; layoutRevision &\+= 1 \}/);
  const LAYER = read("ios/BWReader/App/ReaderNativeDocumentCardLayer.swift");
  assert.match(LAYER, /let _ = document\.layoutRevision/);
  assert.doesNotMatch(LAYER, /document\.geometryRevision/);
  // 屏幕层（浮动卡、投放区）不跟页面几何。
  assert.doesNotMatch(CARDS, /ReaderNativeGeometryTracker\(|document\.geometryRevision/);
});

const STICKY = read("_server_deploy/static/pdf/rc-stickynote.js");

test("③ 拖动写页内归一化锚点，经便签自己的写入路径，不是网页视口坐标", () => {
  // 落点由 PDFKit 定页：页码 + 页内归一化坐标（+ 原生认出的词）。
  const target = body(WEBVIEW, "func nativeDropTarget(", "func nativeWordCardDocumentRect(");
  assert.match(target, /document\.pagePoint\(at: local\)/);
  assert.match(target, /document\.wordBind\(at: local\)/);
  // 便签来源的卡与词锚卡同一条：原生算落点，交给卡片自己的 move 控件。
  const gesture = body(CARDS, "private func finishDrag(", "private var resizeGesture");
  assert.match(gesture, /if item\.bound \|\| item\.fromNote \{/);
  // ⚠ 旧的 nativeCardMove / nativeCardResize 直接 PATCH /pdf/api/notes，绕过网页内存里
  //   那份便签 —— 之后网页按旧对象写回就会把改动冲掉。已删，不许回来。
  assert.doesNotMatch(WEBVIEW, /nativeCardMove|nativeCardResize/);
  assert.doesNotMatch(SCRIPT, /nativeCardMove|nativeCardResize/);
  // 写入走 patchNote（内存与持久化同一条路）；页内坐标必须在 0..1。
  const update = body(STICKY, "nativeUpdateNote: function (id, changes) {", "nativeFavoriteNote: function (id) {");
  assert.match(update, /a\.x < 0 \|\| a\.x > 1 \|\| a\.y < 0 \|\| a\.y > 1/);
  assert.match(update, /return patchNote\(note, fields\)/);
  // 自由卡拖一下不会变成钉词卡（原版同规则）。
  assert.match(SCRIPT, /bind: item\.bound \? \(v\.bind \|\| null\) : null/);
});

test("④ 卡片按屏幕 1:1 画，尺寸就是便签自己的 w/h（2026-09-23「整个卡片所有元素都小过头了」）", () => {
  const layout = body(WEBVIEW, "func nativeCardLayout(", "func placeNativeConversationCard(");
  assert.match(layout, /let scale = 1 \/ max\(zoom, 0\.01\)/, "抵消文档层的缩放");
  assert.match(layout, /note\["w"\]/);
  const LAYER = read("ios/BWReader/App/ReaderNativeDocumentCardLayer.swift");
  assert.match(LAYER, /reader\.nativeCardLayout\(item, zoom: layer\.scale\)/);
  assert.match(LAYER, /\.scaleEffect\(f, anchor: \.topLeading\)/);
  // 触摸框报的是缩放后的实际大小。
  assert.match(LAYER, /width: proxy\.size\.width \* f, height: proxy\.size\.height \* f/);
  // 卡内跟手位移要除回外层缩放，否则卡比手指走得快/慢。
  assert.match(CARDS, /\.offset\(x: translation\.width \/ contentScale, y: translation\.height \/ contentScale\)/);
  // 改尺寸：卡片点就是便签单位，直接存。
  assert.match(CARDS, /let units = value/);
  assert.doesNotMatch(WEBVIEW, /func nativeCardUnits/);
});

test("⑤ 钉在词上的卡：拖动时词由原生认，连同页内坐标交给卡片的 move 控件", () => {
  const gesture = body(CARDS, "private func finishDrag(", "private var resizeGesture");
  const start = gesture.indexOf("if item.bound || item.fromNote {");
  const bound = gesture.slice(start, gesture.indexOf("\n            return\n        }", start) + 20);
  assert.match(bound, /reader\.nativeDropTarget\(windowPoint: point\)/);
  assert.match(bound, /"value": target/);
  assert.match(DOCUMENT, /func wordBind\(at local: CGPoint\) -> WordBind\?/);
  assert.match(STICKY, /command\.key === 'move' && command\.native && typeof command\.native === 'object'/);
});

test("⑥ 原生正文下页卡只来自便签数据：网页一张卡都不挂", () => {
  // 2026-09-23 用户："不能就把网页的渲染直接彻底删掉么""所有旧的渲染在有新的功能
  // 代替后都应该把旧的给去掉"。网页只挂它自己渲染到的那几页，跟原生显示的页永远不同步。
  const ensure = body(STICKY, "function ensureMounted(note) {", "var ctlP = ctls[note.id];");
  assert.match(ensure, /if \(_nativeOwnsPageCards\(\)\)/);
  assert.match(ensure, /return false;/);
  assert.match(SCRIPT, /nativePageCards \? notePlacements\(\) : pagePlacements\(pageStates\)/);
  // 数据出口不碰 DOM。
  const cards = body(STICKY, "nativeNoteCards: function (pages) {", "nativeHasNote: function (id) {");
  assert.doesNotMatch(cards, /querySelector|getBoundingClientRect|\.root\b/);
  // 屏幕层（按窗口坐标摆、必然慢一帧）不画便签来源的卡；文档层只画它们。
  assert.match(CARDS, /if !item\.fromNote \{/);
  const LAYER = read("ios/BWReader/App/ReaderNativeDocumentCardLayer.swift");
  assert.match(LAYER, /ForEach\(model\.placements\.filter\(\\\.fromNote\)\)/);
  // 按便签数据现造 placement 的旁路已删：内容只有一个来源。
  assert.doesNotMatch(CARDS, /init\?\(nativeNote/);
  assert.doesNotMatch(WEBVIEW, /func nativeOnlyPlacements|func deleteNativeNote/);
});

test("⑦ 点锁定框打开的词锚卡总是完全展开（原版 forceOpenCardFull），位置按展开尺寸算", () => {
  assert.match(CARDS, /private var form: String \{ item\.bound && item\.fromNote \? "full" : item\.form \}/);
  const word = body(WEBVIEW, "func nativeWordCardDocumentRect(", "func nativePageCardRect(");
  assert.match(word, /document\.noteGeometry\(note, expanded: true\)/);
});
