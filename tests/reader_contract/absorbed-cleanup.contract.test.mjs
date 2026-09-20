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
//   ③ 客户端原位合并目标成功后摘掉来源，不触发整页重载
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");

const SERVER = read("_server_deploy/assistant.py");
const SIDEBAR = read("_server_deploy/static/pdf/rc-assistant.js");
const TURNCARD = read("_server_deploy/static/pdf/rc-turncard.js");

test("① 服务端收拢时记下被删的 id，不只是个数", () => {
  assert.match(SERVER, /absorbed_ids/,
    "只统计条数的话，客户端无从知道该清哪个");
  assert.match(
    SERVER,
    /absorbed_ids\s*=\s*\[[\s\S]{0,120}b\.get\("absorb"\)/,
    "被收拢的 id 应当取自请求里的 absorb 列表");
});

test("② id 随事件推给客户端，而不是只回在 HTTP 回执里", () => {
  // /log 的普通写入与 upsert 现在共用同一处单轮快照发布。
  const log = SERVER.slice(SERVER.indexOf("def assistant_log_external"));
  const publishes = log.split('"assistant-history"').slice(1)
    .map((blk) => blk.slice(0, 600));
  assert.ok(publishes.length >= 1, "/log 必须发布单轮快照");
  for (const blk of publishes) {
    assert.match(blk, /absorbed_ids/,
      "回执只到运行器，客户端看不见 —— 每一处落库事件都必须带上");
  }
});

test("③ 原位合并成功后摘来源，且不触发整页重载", () => {
  assert.match(TURNCARD, /function drop\(tid\)/, "turnCard 需要一个摘除接口");
  assert.match(TURNCARD, /drop: drop,/, "drop 必须导出，否则侧栏调不到");

  const handler = SIDEBAR.slice(SIDEBAR.indexOf('  function _streamMessages'), SIDEBAR.indexOf('  function _legacyTurnDrain'));
  const operations = [];
  const sandbox = { RC: { turnCard: {
    reconcile(id) { operations.push('merge:' + id); return {}; },
    tidByTurnId(id) { return 'history:' + id; },
    drop(id) { operations.push('drop:' + id); },
  } }, _streamState() { return {}; }, _streamViewId: id => id, _liveUserDrafts: {}, _liveStreams: {},
    _historyMarkSeen() {}, _streamAck() {}, Object, String, Array };
  vm.runInNewContext(handler + '\nthis.run = _streamMessages;', sandbox);
  sandbox.run({ turn_id: 'target', messages: [{ turn_id: 'target', role: 'assistant' }], absorbed_ids: ['source', 'target'] }, true);
  assert.deepEqual(operations, ['merge:target', 'drop:history:source']);
  assert.doesNotMatch(handler, /_requestHistoryReload|_historyCommit|_flushPendingParts/);
  operations.length = 0;
  sandbox.RC.turnCard.reconcile = () => null;
  sandbox.run({ turn_id: 'target', messages: [{ turn_id: 'target', role: 'assistant' }], absorbed_ids: ['source'] }, true);
  assert.deepEqual(operations, [], '目标渲染失败时保留来源，不能把唯一可见内容删除');
});

test("④ 不能把本轮自己摘掉", () => {
  const handler = SIDEBAR.slice(SIDEBAR.indexOf("ev.absorbed_ids"),
                                SIDEBAR.indexOf("ev.absorbed_ids") + 900);
  assert.match(handler, /id === ev\.turn_id\) return/,
    "absorb 列表里若混进本轮 id，摘掉就等于整轮消失");
});
