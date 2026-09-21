// 投递必须认书：给 A 书排的卡不能落到 B 书上（2026-09-21）。
//
// 带词锚的卡是 durable 的 —— 它先落 Windows 侧 outbox，重放时桥会把它改投给
// **当时在线的任何一个来源**：
//
//     ReaderRealtimeOutput.cs
//     ReaderRealtimeOutputRequest replay = entry.Request with {
//         SourceInstanceId = lease.SourceInstanceId,   // ← 换成当前在线的来源
//     };
//
// 而客户端这一侧 `_acceptReaderRealtimeOutput` 在此之前**从不比对 delivery.file
// 与当前打开的书**。今天窗口只有重连那几秒所以没炸；一旦允许"没开书也能排队"
// （用户要的形态），就会变成：给《料理师part2》第 27 页排的卡，几小时后你打开
// 《part1》，它钉到 part1 的第 27 页上。
//
// 四条钉死：
//   ① 确实按 delivery.file 比对当前书；
//   ② 拒收信息里带 UNAVAILABLE —— 桥的 ReplayMayWaitForAnotherSource 认这个词，
//      会把它**留在队列里**等那本书打开，而不是判失败丢掉；
//   ③ 只管带 bind 的卡 —— 别的 kind 不进 outbox，拦下去等于直接丢；
//   ④ 取不到当前书身份时放行 —— 宁可维持今天的行为，也不要凭猜测拒收。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");

const CALL = read("_server_deploy/static/pdf/rc-voicecall.js");
const BRIDGE = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderRealtimeOutput.cs");

const GUARD = CALL.slice(
  CALL.indexOf("function _acceptReaderRealtimeOutput"),
  CALL.indexOf("var correlation = String(delivery.correlation);"));

test("① 按 delivery.file 比对当前打开的书", () => {
  assert.ok(GUARD.length > 100, "找不到投递入口那一段");
  assert.match(GUARD, /delivery\.file/, "不比对 file 就谈不上认书");
  assert.match(CALL, /function _readerOutputCurrentFile\(\)/,
    "需要一个来源，能回答「当前打开的是哪一本」");
});

test("② 拒收信息要让桥把它留在队列里，而不是判失败", () => {
  assert.match(GUARD, /UNAVAILABLE/,
    "桥按关键词决定是 defer 还是 fail —— 不带它这条卡会被直接丢掉");
  // 判据的另一半在桥里：这两个词是它认的。任一侧改了，这条测试要跟着改。
  assert.match(BRIDGE, /ReplayMayWaitForAnotherSource/);
  assert.match(BRIDGE, /"UNAVAILABLE",\s*\n?\s*StringComparison\.OrdinalIgnoreCase/,
    "桥不再认 UNAVAILABLE 的话，客户端这条拒收就变成丢卡了");
});

test("③ 只拦带 bind 的卡", () => {
  assert.match(GUARD, /delivery\.kind === 'card'/,
    "别的 kind 不进 outbox，拦下去等于直接丢");
  assert.match(GUARD, /card\.bind/, "没有词锚的浮层卡不钉在书页上，不该按书拦");
});

test("④ 认不出当前是哪一本就放行", () => {
  // _have 为空时不拒 —— EPUB/别的表面可能没有那个全局，不能因此把卡拦死。
  assert.match(GUARD, /_want && _have && _want !== _have/,
    "三个条件缺一，都会变成凭猜测拒收");
  const src = CALL.slice(CALL.indexOf("function _readerOutputCurrentFile()"),
                         CALL.indexOf("function _acceptReaderRealtimeOutput"));
  assert.match(src, /return ''/, "取不到身份时要返回空，由调用方放行");
});
