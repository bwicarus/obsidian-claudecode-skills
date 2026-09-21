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
  assert.match(WEBVIEW, /panel\.onMarked = \{ \[weak self\] in self\?\.refreshNativePageOverlays\(\) \}/,
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
