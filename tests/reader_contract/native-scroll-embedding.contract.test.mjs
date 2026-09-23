// 滚动残影 / 卡片嵌入 / 侧栏几处回归的守卫（2026-09-23 用户一次报了九件事）。
//
// 共同的病根：凡是按"窗口坐标"摆、靠 geometryRevision 在滚动之后重排的东西，
// 都必然慢一帧 —— 用户看到的就是残影。能钉在页面里的就钉在页面里（页面 overlay），
// 钉不进去的就让宿主在滚动回调里**同步**跟随（文档卡片层）。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");
const code = (source) => source.split("\n").filter((l) => !/^\s*\/\//.test(l)).join("\n");

const DOC = read("ios/BWReader/App/ReaderNativePDFDocument.swift");
const LAYER = read("ios/BWReader/App/ReaderNativeDocumentCardLayer.swift");
const CARDS = read("ios/BWReader/App/ReaderNativePageCards.swift");
const SCRIPT = read("ios/BWReader/App/ReaderNativeConversationScript.swift");
const ASSISTANT = read("_server_deploy/static/pdf/rc-assistant.js");
const VIEW = read("ios/BWReader/App/ReaderNativeConversationView.swift");
const MODEL = read("ios/BWReader/App/ReaderNativeConversationModel.swift");

test("页面装饰画在每页 overlay 里，视口上不再有那张 Canvas", () => {
  const viewport = DOC.slice(DOC.indexOf("struct ReaderNativePDFViewport"),
                             DOC.indexOf("private extension CGRect"));
  assert.doesNotMatch(code(viewport), /Canvas \{/);
  assert.match(DOC, /if let project \{ decorate\?\(context, project\) \}/);
  // 数据一变就让 overlay 重画，而不是等 geometryRevision。
  assert.match(DOC, /var highlights: \[Int: \[Highlight\]\] = \[:\] \{ didSet \{ refreshDecorations\(\) \} \}/);
  assert.match(DOC, /var vocabMarks: \[Int: \[VocabMark\]\] = \[:\] \{ didSet \{ refreshDecorations\(\) \} \}/);
});

test("锁定框和页内按钮在有文字层的 PDF 页上也点得到", () => {
  const inside = DOC.slice(DOC.indexOf("override func point(inside point: CGPoint, with event: UIEvent?) -> Bool {"));
  const head = inside.slice(0, inside.indexOf("guard let characters"));
  assert.match(head, /cardMarkerAt\(point\) != nil/);
  assert.match(head, /buttonViews\.contains/);
});

test("文档卡片层在滚动回调里同步跟随，不推到下一轮", () => {
  const follow = LAYER.slice(LAYER.indexOf("func follow()"), LAYER.indexOf("private func sync()"));
  assert.match(follow, /MainActor\.assumeIsolated \{ self\?\.sync\(\) \}/);
  assert.doesNotMatch(code(follow), /Task \{/);
  assert.match(follow, /observe\(\\\.contentOffset/);
  assert.match(follow, /observe\(\\\.zoomScale/);
  // 只在卡片上接触摸，其余放行给 PDF。
  assert.match(LAYER, /override func point\(inside point: CGPoint, with event: UIEvent\?\) -> Bool/);
});

test("卡片拖动时整张卡跟手，不留淡掉的原卡", () => {
  assert.doesNotMatch(code(CARDS), /card\.opacity\(dragging \? 0\.22 : 1\)/);
  assert.match(CARDS, /\.offset\(translation\)/);
});

test("长按带入对话只在卡身，不在标题拖动条上", () => {
  const card = CARDS.slice(CARDS.indexOf("private var card: some View"), CARDS.indexOf("private func dropPoint("));
  const body = card.slice(card.indexOf("ScrollView {"), card.indexOf(".frame(height: bodyHeight)"));
  assert.match(body, /LongPressGesture\(minimumDuration: 0\.6\)/);
  assert.equal((code(card).match(/LongPressGesture/g) || []).length, 1, "只挂一处");
});

test("选中文字能进对话：原生侧栏开关写在 window 上", () => {
  assert.match(SCRIPT, /window\.__bwNativeAssistantOpen = nativeAssistantOpen/);
  assert.doesNotMatch(code(SCRIPT), /root\.__bwNativeAssistantOpen/);
});

test("原生侧栏开着就算助手可见；首次历史失败会重试", () => {
  const visible = ASSISTANT.slice(ASSISTANT.indexOf("function _assistantPaneVisible()"));
  assert.match(visible.slice(0, 600), /window\.__bwNativeAssistantOpen === true/);
  assert.match(ASSISTANT, /var waits = \[2000, 5000, 15000, 30000\]/);
});

test("清空对话常驻在顶部菜单；侧栏有本机缓存", () => {
  const composer = VIEW.slice(VIEW.indexOf("private var composer: some View"));
  assert.doesNotMatch(code(composer).slice(0, 4000), /清空当前对话/);
  assert.match(VIEW, /"clearConversation"\]\.contains\(where: model\.supports\)/);
  assert.match(MODEL, /ReaderNativeConversationCache\.hasConversation\(rawMessages\)/);
  assert.match(MODEL, /ReaderNativeConversationCache\.clear\(conversationMode\)/);
});
