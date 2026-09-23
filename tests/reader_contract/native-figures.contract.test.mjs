// 图徽标 / 插图描述在原生正文上。
//
// 接管后 .fig-layer 一个都没有（页面不在 DOM 里），徽标、图区命中层、持久选中高亮
// 全都失去宿主 —— 本书开了「插图描述」也什么都看不见。数据本来就是归一坐标，
// 所以原生直接画，判据（哪张图、描述文本、带入与否）仍然只在网页那一处。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");

const SRC = read("_server_deploy/static/pdf/reader.src/26-figures.js");
const READER = read("_server_deploy/static/pdf/reader.js");
const DOC = read("ios/BWReader/App/ReaderNativePDFDocument.swift");
const PANEL = read("ios/BWReader/App/ReaderNativeFigureView.swift");
const WEBVIEW = read("ios/BWReader/App/ReaderWebView.swift");
const SCRIPT = read("ios/BWReader/App/ReaderNativeConversationScript.swift");

const body = (source, from, to) => source.slice(source.indexOf(from), source.indexOf(to));
const code = (source) =>
  source.split(/\r?\n/).filter((line) => !/^\s*(\/\/|\/\*|\*|\/\/\/)/.test(line)).join("\n");

test("① 本书没开插图描述就一张都不出（不拉端点、不烧 AI）", () => {
  const entry = body(SRC, "window.__bwReaderPageFigures = async function",
                     "window.__bwReaderFigureAttach");
  assert.match(entry, /!window\.__figBookOn/, "与 renderFiguresOnPage 同一个开关");
  assert.match(entry, /@interaction document\.page-figures\.read/);
  assert.match(READER, /__bwReaderPageFigures/, "改完 reader.src 要拼合");
});

test("② 带入与否的权威在网页，原生只发起", () => {
  const attach = body(SRC, "window.__bwReaderFigureAttach = async function",
                      "function schedulePoll");
  assert.match(attach, /_toggleFig\(fig, page\)/, "复用长按 toggle 的同一条路");
  assert.match(attach, /window\.__figAttached/, "回执给真实状态");
  // ⚠ 面板不许本地先翻：__figAttached 就是助手上下文的来源，本地先翻会和
  // 助手实际看到的东西对不上。
  const toggle = body(PANEL, "func toggleAttach() async", "var paragraphs");
  assert.match(toggle, /attached = \(receipt\["value"\] as\? \[String: Any\]\)\?\["attached"\]/);
  assert.doesNotMatch(code(toggle), /^\s*attached\.toggle\(\)/m);
  // 两道闸。
  const allow = WEBVIEW.slice(WEBVIEW.indexOf("let allowed: Set<String>"),
                              WEBVIEW.indexOf("guard let action = command[\"action\"]"));
  assert.match(allow, /"nativeFigureAttach"/);
  const keys = SCRIPT.slice(SCRIPT.indexOf("const parameterKeys"),
                            SCRIPT.indexOf("const action = command.action;"));
  assert.match(keys, /nativeFigureAttach/);
});

test("③ 徽标是真控件，锚点缺失时退图框角落", () => {
  // ⚠ 徽标是 UIKit 真控件，长在页面自己的 overlay 里（跟页面同一帧滚，不留残影）。
  const badge = DOC.slice(DOC.indexOf("private final class ReaderNativePageButtonView: UIButton"));
  assert.match(badge, /addAction\(UIAction \{ \[weak self\] _ in self\?\.spec\.action\(\) \}/);
  assert.match(DOC, /self\?\.onOpenFigure\?\(figure\)/);
  assert.match(badge, /badge\.map \{ CGPoint\(x: page\.minX \+ \$0\.x \* page\.width/, "服务端算好的锚点优先");
  assert.match(badge, /anchor\.maxX - side \* 0\.7/, "没有锚点时退图框右上角");
  assert.match(badge, /min\(max\(page\.minX \+ half, center\.x\), page\.maxX - half\)/, "夹进页面内");
  // DOM 那侧的回退要 hitsText 避开正文（需要文字层），接管后没有。
  const entry = body(SRC, "window.__bwReaderPageFigures = async function",
                     "window.__bwReaderFigureAttach");
  assert.doesNotMatch(code(entry), /hitsText/);
});

test("④ 已带入的图画持久绿框（对应 .fig-hl-sel）", () => {
  const draw = body(DOC, "for figure in figures[number] ?? [] where figure.attached",
                    "for stroke in ink[number]");
  assert.match(draw, /cornerRadius: 7/, "与 .fig-hl-sel 同一个圆角");
  assert.match(draw, /setLineWidth\(2\.5\)/);
  assert.match(SRC, /\.fig-hl-sel\{[^']*border:2\.5px solid rgba\(48,209,88/,
    "网页那侧的绿框规格没变");
  // 带入状态变了，正文上的框和徽标颜色要跟着变。
  assert.match(WEBVIEW, /setFigureAttached\(attached, id: figure\.id, page: figure\.page\)/);
});
