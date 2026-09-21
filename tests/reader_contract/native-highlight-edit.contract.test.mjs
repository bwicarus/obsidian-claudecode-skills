// 点已有划线 → 原生编辑面板（改色 / 备注 / 删除）。
//
// 接管后 `.hl-layer` 不存在，网页那条「点划线弹浮层」整条断掉 —— **划得上去、
// 改不了也删不掉**。这是本轮里最容易被当成"已经做完"的一处：高亮画出来了，
// 看上去就像好了。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");

const DICT = read("_server_deploy/static/pdf/reader.src/19-dict.js");
const READER = read("_server_deploy/static/pdf/reader.js");
const DOC = read("ios/BWReader/App/ReaderNativePDFDocument.swift");
const PANEL = read("ios/BWReader/App/ReaderNativeHighlightEditor.swift");
const WEBVIEW = read("ios/BWReader/App/ReaderWebView.swift");
const SCRIPT = read("ios/BWReader/App/ReaderNativeConversationScript.swift");

const body = (source, from, to) => source.slice(source.indexOf(from), source.indexOf(to));
const code = (source) =>
  source.split(/\r?\n/).filter((line) => !/^\s*(\/\/|\/\*|\*|\/\/\/)/.test(line)).join("\n");

test("① 落库全走底座，不另写一套保存", () => {
  const entry = body(DICT, "window.__bwReaderHighlightEdit = async function",
                     "// 预览块的交互：");
  assert.match(entry, /_hlDelete\(h, null\)/);
  assert.match(entry, /_hlUpdate\(h, null, \{ color: c \}\)/);
  assert.match(entry, /_hlUpdate\(h, null, \{ note:/);
  // 颜色键是用户自己的墨水，存在他的笔记里 —— 原生不许另开端点。
  assert.doesNotMatch(code(entry), /fetch\(/);
  assert.doesNotMatch(code(PANEL), /api\/highlights/);
  assert.match(READER, /__bwReaderHighlightEdit/, "改完 reader.src 要拼合");
});

test("② 「点当前色＝取消颜色」的语义留在网页一处", () => {
  const entry = body(DICT, "window.__bwReaderHighlightEdit = async function",
                     "// 预览块的交互：");
  // 有备注 → 留虚框；没备注 → 整条删掉。
  assert.match(entry, /!c && !String\(h\.note \|\| ''\)\.trim\(\)/);
  // 面板只负责「点的是不是当前色」，剩下的让网页决定。
  const pick = body(PANEL, "func pick(_ key: String) async", "func saveNote");
  assert.match(pick, /key == colorKey \? "" : key/);
  assert.doesNotMatch(code(pick), /note\.isEmpty/, "面板不复制「有没有备注」这条判断");
});

test("③ 删除不许假删", () => {
  const entry = body(DICT, "window.__bwReaderHighlightEdit = async function",
                     "// 预览块的交互：");
  // ⚠ _hlDelete 三条路的返回值是**刻意**区分开的：未知结果按未删处理。
  // 用 `!== true` 而不是 `=== false`，否则 undefined 会被当成删成功 ——
  // 界面把它移走、刷新后又回来，这正是"删不掉"的观感。
  assert.match(entry, /await _hlDelete\(h, null\) !== true/);
  assert.match(SCRIPT, /BW_READER_HL_DELETE_FAILED: '删除未确认，它可能还在'/);
  const send = body(PANEL, "private func send(op: String", "struct ReaderNativeHighlightEditor");
  assert.match(send, /body\["deleted"\] as\? Bool == true/);
});

test("④ 划线带着身份进来，否则点上去不知道点的是哪一条", () => {
  const model = body(DOC, "struct Highlight: Identifiable", "struct NoteGeometry");
  assert.match(model, /let id: String/);
  assert.match(model, /let rects: \[CGRect\]/, "一条划线的多个矩形要归在一起");
  assert.match(model, /let note: String/);
  // 空 color 是「无色」划线；拿黄色兜底等于把用户取消掉的颜色涂回去。
  assert.match(DOC, /let hex = value\["color"\] as\? String \?\? ""/);
  const draw = body(DOC, "for highlight in document.highlights[number] ?? [] {",
                    "// 生词句子：135° 排线");
  assert.match(draw, /highlight\.colorKey\.isEmpty/);
  assert.match(draw, /dash: \[3, 2\]/, "无色画虚框");
});

test("⑤ 点划线优先于选字，且顺序不能反", () => {
  const tap = body(DOC, "@objc private func tapText", "@objc private func selectText");
  assert.match(tap, /if let id = highlightAt\?\(location\) \{ onEditHighlight\?\(id\); return \}/);
  // ⚠ 先 resolve 再判断的话选区菜单已经弹出来了，编辑面板会叠在它上面。
  assert.ok(tap.indexOf("highlightAt?(location)") < tap.indexOf("resolve(index, index)"));
  // 重叠时取后画的那条，与网页 z 顺序一致。
  assert.match(DOC, /self\.highlights\[number\]\?\.last\(where:/);
});

test("⑥ 改完要重取投影，且命令过两道闸", () => {
  const open = body(WEBVIEW, "private func openNativeHighlightEditor", "/// 选区菜单里点了「语法」");
  assert.match(open, /scheduleNativePDFProjectionRefresh\(\)/);
  const allow = WEBVIEW.slice(WEBVIEW.indexOf("let allowed: Set<String>"),
                              WEBVIEW.indexOf("guard let action = command[\"action\"]"));
  assert.match(allow, /"nativeHighlightEdit"/);
  const keys = SCRIPT.slice(SCRIPT.indexOf("const parameterKeys"),
                            SCRIPT.indexOf("const action = command.action;"));
  assert.match(keys, /nativeHighlightEdit/);
});
