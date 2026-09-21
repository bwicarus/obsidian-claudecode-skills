// 语法分析在原生正文上。
//
// 网页那条路有两处硬依赖会在接管后直接断：① `_onGrammarAnalyzeNative` 要
// `_charSel.pw.__charBoxes` 才能取整句；② `analyze()` 把结果渲成 .grammar-block
// 塞进容器，而那个容器不在屏幕上。所以原生走 `RC.grammar.analyzeData`：同两条端点、
// 同一套前置，只把结构化结果交出来。
//
// 前置**不许**复制到原生：哪些 KG 开着、有没有跟踪中的节点，是 RC.grammar 的缓存
// （_enabledBooks / _hasTracked）。复制过去的表现是「网页说没开 KG、原生却自顾自
// 跑了一次 AI」——烧了额度还给不出结果。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");

const GRAMMAR = read("_server_deploy/static/pdf/rc-grammar.js");
const PANEL = read("ios/BWReader/App/ReaderNativeGrammarView.swift");
const DOC = read("ios/BWReader/App/ReaderNativePDFDocument.swift");
const WEBVIEW = read("ios/BWReader/App/ReaderWebView.swift");
const SCRIPT = read("ios/BWReader/App/ReaderNativeConversationScript.swift");
const POLICY = read("_server_deploy/static/reader-runtime/interaction-policy.js");

const body = (source, from, to) => source.slice(source.indexOf(from), source.indexOf(to));
const code = (source) =>
  source.split(/\r?\n/).filter((line) => !/^\s*(\/\/|\/\*|\*|\/\/\/)/.test(line)).join("\n");

test("① analyzeData 复用 analyze 的全部前置与同两条端点", () => {
  const entry = body(GRAMMAR, "function analyzeData(spec)", "RC.grammar = {");
  assert.match(entry, /_enabledBooks\.length/, "没启用 KG 要拒");
  assert.match(entry, /!_hasTracked/, "没有跟踪节点要拒");
  assert.match(entry, /sentence\.length < 6/, "句子太短要拒");
  assert.match(entry, /'\/pdf\/api\/grammar-analyze'/);
  assert.match(entry, /'\/pdf\/api\/grammar-stream'/);
  // 前置失败要给**可识别的原因**，不是一句 false —— 面板要把它原样说给用户。
  assert.match(entry, /BW_GRAMMAR_NO_KG/);
  assert.match(entry, /BW_GRAMMAR_NO_TRACKED/);
  assert.match(GRAMMAR, /analyzeData: analyzeData/, "要挂到 RC.grammar 上");
});

test("② 两个表面共用一份分析历史", () => {
  // 历史落库抽成 saveHistoryItem：网页块和原生面板都调它，
  // 否则「最近分析过的句子」在两个表面上会是两份。
  assert.match(GRAMMAR, /function saveHistoryItem\(file, item\)/);
  assert.match(GRAMMAR, /@interaction ai\.grammar\.history\.save/);
  assert.match(POLICY, /'ai\.grammar\.history\.save'/);
  const entry = body(GRAMMAR, "function analyzeData(spec)", "RC.grammar = {");
  assert.match(entry, /saveHistoryItem\(file, \{/);
  // 网页那侧也必须改成调它，不能留一份自己的 fetch。
  const stream = body(GRAMMAR, "function streamGrammar(block, spec)", "// 历史落库");
  assert.doesNotMatch(code(stream), /fetch\('\/pdf\/api\/grammar-history-save'/);
});

test("③ 原生面板不复制前置判断", () => {
  assert.doesNotMatch(code(PANEL), /enabledBooks|hasTracked|grammar-analyze|grammar-stream/,
    "端点与前置都不该出现在面板里");
  // 可操作的提示要原样显示，折成「分析失败」用户不知道去哪开。
  const load = body(PANEL, "func load() async", "struct ReaderNativeGrammarView");
  assert.match(load, /receipt\["error"\] as\? String \?\? "分析失败，请重试。"/);
  assert.match(SCRIPT, /请先在阅读设置里启用至少一个语法 KG/);
  assert.match(SCRIPT, /已启用的 KG 里没有跟踪中的节点/);
});

test("④ 选区菜单有「语法」，分析整句而不是选中串", () => {
  const menu = body(DOC, "func editMenuInteraction", "private func highlightAction");
  assert.match(menu, /UIAction\(title: "语法"/);
  assert.match(menu, /self\.onGrammar\?\(value\)/);
  // 网页那侧也是先取整句再把选中串当 focus —— 两边口径必须一致。
  assert.match(DOC, /self\?\.onGrammar\?\(number, value\.sentence, value\.text\)/);
  const open = body(WEBVIEW, "private func openNativeGrammar", "/// 点图徽标");
  assert.match(open, /whole\.isEmpty \? picked : whole/, "整句取不到时退用选中串");
});

test("⑤ 命令过两道闸", () => {
  const allow = WEBVIEW.slice(WEBVIEW.indexOf("let allowed: Set<String>"),
                              WEBVIEW.indexOf("guard let action = command[\"action\"]"));
  assert.match(allow, /"nativeGrammar"/);
  const keys = SCRIPT.slice(SCRIPT.indexOf("const parameterKeys"),
                            SCRIPT.indexOf("const action = command.action;"));
  assert.match(keys, /nativeGrammar/);
});

test("⑥ 「显示旧界面」时原生正文要让开", () => {
  const app = read("ios/BWReader/App/BWReaderNativeApp.swift");
  const active = body(app, "private var nativePDFSurfaceActive: Bool", "var body: some View");
  // ⚠ 不让开的话：网页层被藏着、原生正文盖在上面 —— 点了旧界面得到一片空白，
  // 而且没有出路（旧界面的按钮也在被藏的那一层里）。
  assert.match(active, /!reader\.nativeConversation\.legacyVisible/);
  assert.match(app, /if nativePDFSurfaceActive, let document = reader\.nativePDFDocument \{/,
    "挂载判断也要用同一个量，否则两处会分叉");
});
