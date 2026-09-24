// 选区菜单要覆盖网页选中工具条上真正在用的那几件事。
//
// 网页的 #sel-btns-multi 有：词组 / 复制 / OCR / 翻译 / 解释 / 对话 / 搜索，
// 加上语法分析和色板。接管后那条工具条不在屏幕上，**它们一件都用不了** ——
// 而原生菜单看上去是有东西的（复制/查词/翻译/划线都在），所以缺的那几件
// 很容易被当成"本来就没有"。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");

const WORDPOP = read("_server_deploy/static/pdf/reader.src/15-phrase-wordpop.js");
const READER = read("_server_deploy/static/pdf/reader.js");
const DOC = read("ios/BWReader/App/ReaderNativePDFDocument.swift");
const PANEL = read("ios/BWReader/App/ReaderNativeLookupView.swift");
const WEBVIEW = read("ios/BWReader/App/ReaderWebView.swift");
const SCRIPT = read("ios/BWReader/App/ReaderNativeConversationScript.swift");

const body = (source, from, to) => source.slice(source.indexOf(from), source.indexOf(to));
const code = (source) =>
  source.split(/\r?\n/).filter((line) => !/^\s*(\/\/|\/\*|\*|\/\/\/)/.test(line)).join("\n");
const lookup = () => WORDPOP.slice(WORDPOP.indexOf("window.__bwReaderLookupData = async function"));

test("① 解释复用底座的 AI 流，不新开一条", () => {
  const entry = lookup();
  assert.match(entry, /request\.mode === 'explain'/);
  assert.match(entry, /_aiStream\('\/pdf\/api\/explain'/, "同一条端点、同一套 rid 重连");
  // 原生面板只要最终文本，不需要边到边 —— 但流还是那条流。
  assert.match(entry, /BW_READER_EXPLAIN_FAILED/);
  assert.match(READER, /BW_READER_EXPLAIN_FAILED/, "改完 reader.src 要拼合");
});

test("② 词组不新开端点：日语走中日词典、其它走整句翻译", () => {
  const entry = lookup();
  assert.match(entry, /const phrase = request\.mode === 'phrase'/);
  assert.match(entry, /if \(request\.mode === 'translate' \|\| \(phrase && !isJa\)\)/);
  // 词组的两条路都是**现成分支**；多开一条端点就是多一处会漂移的写法。
  const phraseOnly = entry.split("const phrase = request.mode === 'phrase'")[1] || "";
  assert.doesNotMatch(code(phraseOnly), /fetch\('\/pdf\/api\/phrase-/);
});

test("③ 收藏为词组走底座那一条路", () => {
  const fav = WORDPOP.slice(WORDPOP.indexOf("window.__bwReaderPhraseFav = async function"));
  assert.match(fav, /_phraseFav\(null\)/);
  // ⚠ 本地先翻、真分词重算、长下划线即时画、outbox 兜底，四件事都挂在它上面。
  assert.doesNotMatch(code(fav), /fetch\(/);
  // 归一化必须同一条规则：跨行选中带换行，不归一化会存成另一个词组
  // （表现是「收藏了却没生效」）。
  assert.match(WORDPOP, /function _phraseStateOf\(text\)/);
  assert.match(body(WORDPOP, "function _phraseStateOf(text)", "window.__bwReaderPhraseFav"),
    /replace\(\/\[\\s\\u3000\]\+\/g, ''\)/);
});

test("④ 掌握态要带回来，否则点一下反而取消了已掌握", () => {
  const entry = lookup();
  // 不带 mastered 的话面板永远显示「未掌握」，用户点一下是 unmark。
  assert.match(entry, /mastered: !!d\.mastered/);
  const load = body(PANEL, "func load() async", "/// 标记掌握");
  assert.match(load, /mastered = body\["mastered"\] as\? Bool == true/);
  assert.match(load, /favorited = body\["fav"\] as\? Bool == true/);
});

test("⑤ 菜单里这几件都在，且每件都过两道闸", () => {
  const menu = body(DOC, "func editMenuInteraction", "private func highlightAction");
  for (const title of ["复制", "选整句", "查词", "翻译", "词组", "解释", "语法"]) {
    assert.match(menu, new RegExp(`UIAction\\(title: "${title}"`), title + " 不在菜单里");
  }
  const allow = WEBVIEW.slice(WEBVIEW.indexOf("let allowed: Set<String>"),
                              WEBVIEW.indexOf("guard let action = command[\"action\"]", WEBVIEW.indexOf("let allowed: Set<String>")));
  assert.match(allow, /"nativePhraseFav"/);
  const keys = SCRIPT.slice(SCRIPT.indexOf("const parameterKeys"),
                            SCRIPT.indexOf("const action = command.action;"));
  assert.match(keys, /nativePhraseFav/);
  // 模式白名单三处要一致：脚本、壳、面板。
  assert.match(SCRIPT, /'dict', 'dict-full', 'translate', 'explain', 'phrase'/);
  assert.match(WEBVIEW, /\["dict", "translate", "explain", "phrase"\]\.contains\(mode\)/);
});
