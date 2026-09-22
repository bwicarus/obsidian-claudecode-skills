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
  assert.match(CARDS, /reader\.nativePageMarkerRects\(/, "锚标记位置");
  assert.match(CARDS, /\?\? reader\.nativePageCardRect\(/, "退路必须还在");
  const geometry = body(WEBVIEW, "func nativePageCardGeometry(", "func nativePageMarkerRects(");
  assert.match(geometry, /document\.noteGeometry\(note, presentationSize: size\)/);
  assert.match(geometry, /return nil/, "没有原生几何时返回 nil，由调用方退回 —— 不猜");
  // 标记的绑定解不出来时是空数组而不是 nil：那是"这张卡确实没钉在正文上"，
  // 跟"没有原生几何"不是一回事，退回网页反而会画出错位的框。
  const markers = body(WEBVIEW, "func nativePageMarkerRects(", "func nativePageCardRect(");
  assert.match(markers, /geometry\.bindingRects\.map/);
});

test("② 跟着 PDF 滚动/缩放重画", () => {
  assert.match(CARDS, /ReaderNativeGeometryTracker\(document: document\)/);
  assert.match(CARDS, /content\(document\.geometryRevision\)/,
    "必须把 geometryRevision 读进 body，否则 SwiftUI 不知道 PDFView 动过");
  assert.match(DOCUMENT, /geometryRevision &\+= 1/,
    "document 那侧要真的在布局时 bump 它");
});

test("③ 拖动写页内归一化锚点，不是网页视口坐标", () => {
  const move = body(WEBVIEW, "func moveNativeCard(", "func resizeNativeCard(");
  assert.match(move, /document\.canonicalPoint\(local, from: document\.view\)/,
    "落点要经 PDFKit 换成 (页码, 页内归一化坐标)");
  assert.match(move, /"action": "nativeCardMove"/);
  assert.match(move, /return false/, "换不出来要退回网页那条路");
  // 视图那侧：原生成功就 return，别再写一遍网页锚点。
  const gesture = body(CARDS, "private var moveGesture", "private var resizeGesture");
  // 原生那条先走；成了就不再写网页锚点（分支体内 return）。
  const nativeFirst = gesture.slice(
    gesture.indexOf("if await reader.moveNativeCard(id: item.id, windowPoint: point) {"),
    gesture.indexOf("guard let action = item.controls"),
  );
  assert.ok(nativeFirst.length > 0, "原生落点分支必须排在网页路径之前");
  assert.match(nativeFirst, /return/);

  const branch = body(SCRIPT, "action === 'nativeCardMove' || action === 'nativeCardResize'",
                      "action === 'nativeSelectionLookup'");
  assert.match(branch, /patch\.anchor = \{ kind: 'pdf', page: value\.page/);
  assert.match(branch, /value\.x < 0 \|\| value\.x > 1/, "页内坐标必须在 0..1");
  assert.match(branch, /'\/pdf\/api\/notes'/);
  assert.match(branch, /method: 'PATCH'/, "字段级合并，别整条覆盖");
});

test("④ 改大小按卡片自身单位存", () => {
  const resize = body(WEBVIEW, "func resizeNativeCard(", "func placeNativeConversationCard(");
  assert.match(resize, /base > 0 \? pageRect\.width \/ base : 1/,
    "ratio 要与 noteGeometry 同一算法，否则每缩放一次书尺寸就记错一次");
  assert.match(resize, /Double\(size\.width\) \/ ratio/);
  // noteGeometry 那侧的同一算法：它变了，这里要跟着变。
  assert.match(DOCUMENT, /let ratio = base > 0 \? pageRect\.width \/ base : 1/);
});
