// 原生词典面板要能做网页小框能做的事，否则用它替换小框就是降级。
//
// 目前补齐的两件：发音、标记掌握。
//
// ⚠ 「标记掌握」不是一次 POST 那么简单：日语走 jp-vocab-mark、英语走 vocab-mark
// （英语那条还会写 vocab 笔记的 frontmatter.user_mark 并锁 mastery），标完还要
// 重画下划线。判据和副作用都留在阅读器那侧一处，原生只发起 —— 复制过去会变成
// 「在原生上标了掌握，网页上下划线还在」。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");

const WORDPOP = read("_server_deploy/static/pdf/reader.src/15-phrase-wordpop.js");
const READER = read("_server_deploy/static/pdf/reader.js");
const PANEL = read("ios/BWReader/App/ReaderNativeLookupView.swift");
const WEBVIEW = read("ios/BWReader/App/ReaderWebView.swift");
const SCRIPT = read("ios/BWReader/App/ReaderNativeConversationScript.swift");
const POLICY = read("_server_deploy/static/reader-runtime/interaction-policy.js");
const SHARED = read("_server_deploy/static/pdf/rc-wordpop.js");

const body = (source, from, to) => source.slice(source.indexOf(from), source.indexOf(to));
const code = (source) =>
  source.split(/\r?\n/).filter((line) => !/^\s*(\/\/|\/\*|\*|\/\/\/)/.test(line)).join("\n");

test("① 标记掌握的判据与副作用都在阅读器那侧", () => {
  const entry = body(WORDPOP, "window.__bwReaderMarkVocab = async function",
                     "window.__bwReaderLookupData");
  assert.match(entry, /_isJaWord\(word\)/, "语言分流复用同一条规则");
  assert.match(entry, /refreshVocabUnderlinesForAllPages\(\)/,
    "标完要重画下划线 —— 少了这句，要翻页才看得见变化");
  assert.match(READER, /__bwReaderMarkVocab/, "改完 reader.src 要拼合");
});

test("② 两条端点各带自己注册过的交互 id（不能合成 URL）", () => {
  const entry = body(WORDPOP, "window.__bwReaderMarkVocab = async function",
                     "window.__bwReaderLookupData");
  assert.match(entry, /@interaction vocabulary\.jp-mastery\.set/);
  assert.match(entry, /@interaction vocabulary\.mastery\.set/);
  // ⚠ 用三元合成 URL 的话，审计对不上任何一个 id，门禁判成新增债务。
  assert.doesNotMatch(code(entry), /fetch\(jp \?/, "不要把两条端点合成一个表达式");
  // 这两个 id 必须是注册过的，否则门禁同样会拒。
  assert.match(POLICY, /'vocabulary\.mastery\.set'/);
  assert.match(POLICY, /'vocabulary\.jp-mastery\.set'/);
});

test("③ 原生面板只发起，并在标完后让正文重取", () => {
  assert.match(PANEL, /"action": "nativeVocabMark"/);
  assert.doesNotMatch(code(PANEL), /vocab-mark|jp-vocab-mark/,
    "端点不该出现在面板里");
  assert.match(WEBVIEW, /panel\.onMarked = \{ \[weak self\] in self\?\.refreshNativePageOverlays\(force: true\) \}/,
    "标完要重取叠加数据，否则这一页的下划线要翻页才消失");
  // 命令要过两道闸。
  const allow = WEBVIEW.slice(WEBVIEW.indexOf("let allowed: Set<String>"),
                              WEBVIEW.indexOf("guard let action = command[\"action\"]"));
  assert.match(allow, /"nativeVocabMark"/);
  const keys = SCRIPT.slice(SCRIPT.indexOf("const parameterKeys"),
                            SCRIPT.indexOf("const action = command.action;"));
  assert.match(keys, /nativeVocabMark/);
});

test("④ 发音用系统 TTS，且合成器要活到念完", () => {
  assert.match(PANEL, /AVSpeechSynthesisVoice\(language: isJapanese \? "ja-JP" : "en-US"\)/,
    "日语读假名、英语读词本身");
  assert.match(PANEL, /final class ReaderNativeSpeech/);
  assert.match(PANEL, /static let shared = ReaderNativeSpeech\(\)/,
    "⚠ 每次新建合成器会让上一句还没念完就被回收 —— 表现是点了没声音");
});

test("⑤ 展开完整词典复用网页那条端点，不另开一个", () => {
  const entry = WORDPOP.slice(WORDPOP.indexOf("window.__bwReaderLookupData = async function"));
  assert.match(entry, /request\.mode === 'dict-full'/);
  // ⚠ dict-full 必须排在 isJa 之前：反了的话日语词先被 dict-jp 接走，
  // 「展开」什么都不多出来却把状态翻成已展开 —— 静默无效。
  assert.ok(entry.indexOf("request.mode === 'dict-full'") < entry.indexOf("const isJa ="),
    "dict-full 分支要在语言分流之前");
  assert.match(entry, /BW_READER_LOOKUP_JP_FULL/, "日语走到这儿要出声，不要返回旧数据");
  assert.match(PANEL, /if !model\.expanded, !model\.isJapanese \{/,
    "面板对日语不出展开按钮");
  assert.match(entry, /'\/pdf\/api\/dict\?word='/,
    "跟网页小框「展开」同一条端点 —— 融合口径只该有一处");
  assert.match(SHARED, /fetch\('\/pdf\/api\/dict\?'/, "网页那侧还在用它");
  // 一次性 JSON 而不是 SSE：原生面板不需要分段到达。
  assert.doesNotMatch(code(entry), /text\/event-stream/);
  // 新 fetch 要有注册过的交互 id，否则门禁判成新增债务。
  assert.match(entry, /@interaction dictionary\.full\.read/);
  assert.match(POLICY, /'dictionary\.full\.read'/);
  assert.match(READER, /dict-full/, "改完 reader.src 要拼合");
});

test("⑥ 展开是合并而不是替换", () => {
  const expand = body(PANEL, "func expand() async", "private func string(");
  // ⚠ 完整词条没有 mastered / reading / kanji 这些小框才有的键；直接 value = body
  // 会让「已掌握」按钮和日语读音在点开展开后凭空消失。
  assert.doesNotMatch(code(expand), /^\s*value = body\s*$/m);
  assert.match(expand, /var merged = value/);
  assert.match(expand, /for \(key, item\) in body where !\(item is NSNull\)/,
    "服务端给 null 的键不能盖掉小框已有的值");
  assert.match(expand, /"mode": "dict-full"/);
  // 展开完按钮要消失，否则重复点。
  assert.match(PANEL, /if !model\.expanded, !model\.isJapanese \{/);
});

test("⑦ 例句两种形状都收（英语字符串 / 日语 {ja,zh}）", () => {
  const examples = body(PANEL, "var examples: [(String, String)]", "var synonyms");
  assert.match(examples, /item as\? String/, "英语完整词条给的是字符串数组");
  assert.match(examples, /pair\["ja"\] as\? String \?\? pair\["en"\] as\? String/,
    "日语给的是对象，zh 缺了回退 en");
});
