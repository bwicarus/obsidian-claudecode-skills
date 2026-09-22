// 顶栏 🗒 新建便签，在原生接管时必须走原生那条路。
//
// 这一条是「彻底静默」的教科书例子：网页的 createAtCenter 靠
// document.elementFromPoint 找落点，接管后一页都不在 DOM 里，七个候选点全落空
// —— 便签没建，而"这里放不了便签"的 toast 也在被藏起来的那一层里，所以
// **点下去什么都不会发生，也没有任何线索**。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");

const WORDPOP = read("_server_deploy/static/pdf/reader.src/15-phrase-wordpop.js");
const READER = read("_server_deploy/static/pdf/reader.js");
const HTML = read("_server_deploy/templates/pdf_reader.html");
const WORKSPACE = read("ios/BWReader/App/ReaderNativeWorkspace.swift");
const WEBVIEW = read("ios/BWReader/App/ReaderWebView.swift");
const MODEL = read("ios/BWReader/App/ReaderNativeConversationModel.swift");
const SCRIPT = read("ios/BWReader/App/ReaderNativeConversationScript.swift");

const body = (source, from, to) => source.slice(source.indexOf(from), source.indexOf(to));
const code = (source) =>
  source.split(/\r?\n/).filter((line) => !/^\s*(\/\/|\/\*|\*|\/\/\/)/.test(line)).join("\n");

test("① 落库仍走 RC.stickynote.createAt，只是锚点由原生给", () => {
  const entry = WORDPOP.slice(WORDPOP.indexOf("window.__bwReaderCreateNote = function"));
  assert.match(entry, /sticky\.createAt\(\{ kind: 'pdf', page: page, x: x, y: y \}\)/);
  // 锚定、代次校验、渲染、失败提示都在 createAt 那条路上，绕过去会得到一张
  // 存下来却不显示、或显示了却没存的便签。
  assert.doesNotMatch(code(entry), /fetch\(|createRecord\(/);
  assert.doesNotMatch(code(entry), /elementFromPoint/, "落点不该再回网页问");
  assert.match(READER, /__bwReaderCreateNote/, "改完 reader.src 要拼合");
});

test("② 顶栏按钮有 id，原生才认得出它", () => {
  // 没有 id 的话只能按标题字符串认，改一个字就失效。
  assert.match(HTML, /<button id="note-new" onclick="window\._noteCreateAtCenter/);
  // ⚠ 拦截点搬到了 `runReadingTool`（2026-09-22 顶栏工具改成可自定义，
  //   菜单项和钉在顶栏上的按钮**共用同一条执行路径**）。
  //   约定本身没变，而且共用一条路反而更安全：不会出现"菜单里拦住了、
  //   顶栏那个没拦住"。
  const menu = WORKSPACE.slice(WORKSPACE.indexOf("private func runReadingTool("));
  assert.match(menu, /control\.key == "note-new", reader\.nativePDFDocument != nil/);
  assert.match(menu, /reader\.createNativeStickyNote\(\)/);
  // 拦下来之后不能再 liveAction 一次，否则网页那条静默路径照样跑。
  assert.match(menu, /createNativeStickyNote\(\)[\s]*return/);
});

test("③ 位置由 PDFKit 给，并且失败要出声", () => {
  const create = body(WEBVIEW, "func createNativeStickyNote()", "/// 点了已有划线");
  assert.match(create, /document\.position\.visiblePages/);
  assert.match(create, /"x": 0\.5, "y": 0\.5/);
  // ⚠ 原来这条路是彻底静默的 —— 原生路径的失败必须自己出声。
  assert.match(create, /nativeConversation\.report\(/);
  assert.match(MODEL, /func report\(_ message: String\)/);
  assert.match(create, /scheduleNativePDFProjectionRefresh\(\)/, "建完要重取才看得见");
});

test("④ 命令过两道闸，参数有界", () => {
  const handler = body(SCRIPT, "} else if (action === 'nativeCreateNote') {",
                       "} else if (action === 'nativePhraseFav') {");
  assert.match(handler, /Number\.isSafeInteger\(value\.page\) && value\.page < 1|Number\.isSafeInteger\(value\.page\)/);
  assert.match(handler, /value\.x >= 0 && value\.x <= 1/);
  const allow = WEBVIEW.slice(WEBVIEW.indexOf("let allowed: Set<String>"),
                              WEBVIEW.indexOf("guard let action = command[\"action\"]"));
  assert.match(allow, /"nativeCreateNote"/);
  const keys = SCRIPT.slice(SCRIPT.indexOf("const parameterKeys"),
                            SCRIPT.indexOf("const action = command.action;"));
  assert.match(keys, /nativeCreateNote/);
});
