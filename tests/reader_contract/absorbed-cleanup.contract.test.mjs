// 收拢之后，屏幕上那条独立气泡必须被摘掉（2026-09-21）。
//
// 语音先以**独立气泡**实时渲出来；随后那句话被「收拢」进后台轮次卡，服务端把库里
// 那条零散记录删掉。但在 2026-09-21 之前，客户端根本不知道这件事发生过 ——
// absorbed 只回在 /log 的 HTTP 回执里（只有运行器看得到），没有走事件。
// 结果：同一句话在卡外面和卡里面各出现一次，而**库里其实只有一条**。
// 用户截图里那句是「还在做,马上好。」。
//
// 这条链路有三段，任一段断掉残留就会回来，所以三段都钉：
//   ① 服务端收拢时记下被删的 id
//   ② 这些 id 随 assistant-history 事件推给客户端
//   ③ 客户端拿到后真的把那个容器摘掉，且在权威重载**之前**做
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");

const SERVER = read("_server_deploy/assistant.py");
const SIDEBAR = read("_server_deploy/static/pdf/rc-assistant.js");
const TURNCARD = read("_server_deploy/static/pdf/rc-turncard.js");

test("① 服务端收拢时记下被删的 id，不只是个数", () => {
  assert.match(SERVER, /_absorbed_ids/,
    "只统计条数的话，客户端无从知道该清哪个");
  assert.match(
    SERVER,
    /_absorbed_ids\s*=\s*\[[\s\S]{0,120}b\["absorb"\]/,
    "被收拢的 id 应当取自请求里的 absorb 列表");
});

test("② id 随事件推给客户端，而不是只回在 HTTP 回执里", () => {
  // ⚠ /log 里有**两处**发布落库事件（upsert 早返回那条 + 正常落库那条），
  //   两处都可能带 absorb。第一版我只改了后一处，于是走 upsert 那条路时
  //   残留照样没人清。
  //   ⚠ 只看 /log 这一段：同文件里还有 /stream 的草稿推送和 bridge 来源的落库，
  //     那两条不带 absorb，连坐进来只会造出永远修不好的断言。
  const log = SERVER.slice(SERVER.indexOf("def assistant_log_external"));
  const publishes = log.split('"assistant-history"').slice(1)
    .map((blk) => blk.slice(0, 420))
    .filter((blk) => blk.includes('"turn_id"'));
  assert.ok(publishes.length >= 2,
    "/log 里应当有两处落库事件发布，实际 " + publishes.length);
  for (const blk of publishes) {
    assert.match(blk, /absorbed_ids/,
      "回执只到运行器，客户端看不见 —— 每一处落库事件都必须带上");
  }
});

test("③ 客户端摘容器，且在权威重载之前", () => {
  assert.match(TURNCARD, /function drop\(tid\)/, "turnCard 需要一个摘除接口");
  assert.match(TURNCARD, /drop: drop,/, "drop 必须导出，否则侧栏调不到");

  const handler = SIDEBAR.slice(
    SIDEBAR.indexOf("ev.absorbed_ids"),
    SIDEBAR.indexOf("_requestHistoryReload({ reason: 'assistant-history'"));
  assert.ok(handler.length > 40, "找不到收拢清理那段");
  assert.match(handler, /RC\.turnCard\.drop/, "要真的摘掉，不能只记个标记");
  // 顺序：清理必须排在 _flushPendingParts / 重载之前。
  const cleanupAt = SIDEBAR.indexOf("ev.absorbed_ids");
  const flushAt = SIDEBAR.indexOf("_flushPendingParts()", cleanupAt - 400);
  assert.ok(cleanupAt < flushAt,
    "清理要在权威重载之前：残留元素还在的话，整页原子换入会把它一起留下");
});

test("④ 不能把本轮自己摘掉", () => {
  const handler = SIDEBAR.slice(SIDEBAR.indexOf("ev.absorbed_ids"),
                                SIDEBAR.indexOf("ev.absorbed_ids") + 900);
  assert.match(handler, /_aid === tid\) continue/,
    "absorb 列表里若混进本轮 id，摘掉就等于整轮消失");
});
