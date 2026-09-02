// client-action 入口名单四份副本的跨站一致性（2026-09-02）。
//
// 活标本：C# 真闸 ValidateClientAction 一直没有 _nativeReaderWordCardsConsolidate，
// 而 MCP reader_word_cards 的整理投递用的就是它 —— 上线两天全被桥拒，
// 没有任何测试红。这里钉的是"任一侧加入口，另一侧必须跟上"：
//   · JS 派发分支（rc-voicecall `_caFn === '<fn>'`）的集合
//   · C# 真闸（ReaderRealtimeOutput.ValidateClientAction 的 `fn is not (...)`）的集合
// 两者必须相等。第三/四份（rc-computer-voice 入站闸按 kind 放行、bundle 随包）
// 不点名 fn，不在此比。
import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const VOICECALL = readFileSync(
  new URL("_server_deploy/static/pdf/rc-voicecall.js", ROOT), "utf8");
const CSHARP = readFileSync(
  new URL("extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderRealtimeOutput.cs", ROOT),
  "utf8");

function jsDispatchFns() {
  const out = new Set();
  for (const m of VOICECALL.matchAll(/_caFn === '([A-Za-z_][A-Za-z0-9_]*)'/g)) out.add(m[1]);
  return out;
}

function csharpGateFns() {
  const start = CSHARP.indexOf("private static void ValidateClientAction(JsonElement root)");
  assert.ok(start >= 0, "C# ValidateClientAction 改名了？真闸位置必须可定位");
  const gateStart = CSHARP.indexOf("if (fn is not (", start);
  const gateEnd = CSHARP.indexOf("))", gateStart);
  assert.ok(gateStart > start && gateEnd > gateStart, "C# 白名单 pattern 形状变了");
  const out = new Set();
  for (const m of CSHARP.slice(gateStart, gateEnd).matchAll(/"([A-Za-z_][A-Za-z0-9_]*)"/g)) out.add(m[1]);
  return out;
}

test("client-action 入口名单：JS 派发分支与 C# 真闸逐名相等", () => {
  const js = [...jsDispatchFns()].sort();
  const cs = [...csharpGateFns()].sort();
  assert.ok(js.length >= 8, "JS 派发分支抽取异常（<8 个入口）");
  assert.deepEqual(js, cs,
    "两侧入口名单不一致：JS=" + js.join(",") + " C#=" + cs.join(","));
});
