// 原生阅读区的查词/翻译：**判据只有一份**，在阅读器那侧。
//
// 「这个词该查中日词典还是英文词典」依赖 BOOK_LANGS（这本书声明了哪些语言），
// 判据写在 reader.src 的 `_isJaWord` 里。原生那侧只负责显示结果。
//
// 这条测试守的就是别把那套路由复制到原生去：一旦有两份，它们会各自漂移，
// 表现是「同一个词在网页上查中日词典、在原生上查英文词典」—— 而这种不一致
// 没人会立刻发现。
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

test("① 数据入口在阅读器那侧，且复用它自己的语言路由", () => {
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

test("③ 原生这侧不复制语言判据，也不自己发词典请求", () => {
  const branch = SCRIPT.slice(SCRIPT.indexOf("action === 'nativeSelectionLookup'"),
                              SCRIPT.indexOf("action === 'readingSettingsRead'"));
  assert.ok(branch.length > 200, "找不到这条命令的实现");
  assert.match(branch, /window\.__bwReaderLookupData\(/, "必须转交阅读器的数据入口");
  assert.doesNotMatch(branch, /dict-jp|dict-quick|translate-sentence/,
    "端点名出现在原生这侧＝判据被复制了");
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
