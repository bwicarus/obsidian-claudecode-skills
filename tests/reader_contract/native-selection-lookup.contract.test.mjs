// 网页兼容数据入口保留现有路由；Swift 已迁移的查询由
// ReaderNativeLookupRequest 负责，行为在 NativeLookupRequest 中验证。
// 原生展示面板不负责网络或语言选择。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");

const WORDPOP = read("_server_deploy/static/pdf/reader.src/15-phrase-wordpop.js");
const READER = read("_server_deploy/static/pdf/reader.js");
const SCRIPT = read("ios/BWReader/App/ReaderNativeConversationScript.swift");
const WEBVIEW = read("ios/BWReader/App/ReaderWebView.swift");
const PANEL = read("ios/BWReader/App/ReaderNativeLookupView.swift");

test("① 网页兼容数据入口复用现有语言路由", () => {
  assert.match(WORDPOP, /window\.__bwReaderLookupData = async function/);
  const entry = WORDPOP.slice(WORDPOP.indexOf("window.__bwReaderLookupData"));
  assert.match(entry, /_isJaWord\(text\)/,
    "语言路由必须复用 _isJaWord —— 它依赖 BOOK_LANGS，另写一份必然漂移");
  assert.match(entry, /\/pdf\/api\/dict-jp\?/, "日语走中日词典");
  // 英文路径**一次新请求都不加**：复用网页小框那条现成的 _lookupWordFetch。
  // 少一处 fetch 就少一处会漂移的写法，网络依赖门禁的 baseline 也因此从 201 降到 199。
  assert.match(entry, /_lookupWordFetch\(text, context\)/,
    "英文路径要复用现成函数，不要另起一条 fetch");
  assert.match(READER, /async function _lookupWordFetch\(word, ctx\)[\s\S]{0,200}dict-quick/,
    "被复用的那条仍打 dict-quick —— 它换了端点，这里就跟着换了");
  assert.match(entry, /\/pdf\/api\/translate-sentence/, "整段翻译沿用网页那条端点");
});

test("② 拼合后的 reader.js 真的带上了它（改了 src 忘了拼合＝线上没有这个函数）", () => {
  assert.match(READER, /window\.__bwReaderLookupData = async function/,
    "reader.src 改完要跑 scripts/build_pdf_reader_js.sh");
});

test("③ 网页适配器复用数据入口，原生面板只展示", () => {
  const branch = SCRIPT.slice(SCRIPT.indexOf("action === 'nativeSelectionLookup'"),
                              SCRIPT.indexOf("action === 'readingSettingsRead'"));
  assert.ok(branch.length > 200, "找不到这条命令的实现");
  assert.match(branch, /window\.__bwReaderLookupData\(/, "必须转交阅读器的数据入口");
  assert.doesNotMatch(branch, /dict-jp|dict-quick|translate-sentence/,
    "网页适配器不重复拼接请求");
  // ⚠ 只看**代码**，不看注释：解释「为什么不复制」的说明必然会提到 BOOK_LANGS，
  //   把它也算成违规，就会逼着人把原因删掉 —— 那正好是最不该删的东西。
  const stripComments = (source) => source
    .split("\n").filter((line) => !/^\s*(\/\/|\*|\/\*)/.test(line)).join("\n");
  for (const source of [stripComments(branch), stripComments(PANEL)]) {
    assert.doesNotMatch(source, /BOOK_LANGS|_isJaWord\s*\(/,
      "语言判据被复制到原生这侧了");
  }
  assert.doesNotMatch(stripComments(PANEL), /dict-jp|dict-quick|translate-sentence|URLSession/,
    "面板只显示，不取数");
});

test("④ 命令要过两道闸，缺一就是哑按钮", () => {
  const allow = WEBVIEW.slice(WEBVIEW.indexOf("let allowed: Set<String>"),
                              WEBVIEW.indexOf("guard let action = command[\"action\"]"));
  assert.match(allow, /"nativeSelectionLookup"/, "壳这侧的允许清单");
  const keys = SCRIPT.slice(SCRIPT.indexOf("const parameterKeys"),
                            SCRIPT.indexOf("const action = command.action;"));
  assert.match(keys, /nativeSelectionLookup/,
    "脚本这侧的参数白名单：带 value 的命令必须登记，否则整条命令被判为参数不支持");
});
