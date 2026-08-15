// recentActions:快照里「用户刚做了什么」的那一格。
//
// 前身是 latestEvent,装的是内部记账事件(readerpc.recovering / active.reading /
// viewport.context),对 AI 毫无用处却一直发出去。这个字段换成用户真实动作,
// 但故意只覆盖两种:翻页、画完一笔——这两种是唯一能从现有信号无歧义识别出来的,
// 高亮/查词/写便签目前都没有被记进任何 journal,硬凑会把猜测当事实发给模型。
//
// 三条设计约束(references/local-first-data-architecture.md 第 16 条):
//   ≤5 条、30 秒窗、限当前书。外加一条这个实现特有的:字段名与工具描述都要
//   让模型读出"这是历史记录"而不是"这是待办指令"。
//
// 注:本机只有 .NET runtime 无 SDK,这是文本校验,不能替代编译。

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

const DIR = join(
  fileURLToPath(new URL("../..", import.meta.url)),
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio",
);
const SNAPSHOT = readFileSync(join(DIR, "DirectContextSnapshot.cs"), "utf8");
const MCP = readFileSync(join(DIR, "ReaderContextMcpServer.cs"), "utf8");

function recordActionBody() {
  const start = SNAPSHOT.indexOf("private void RecordAction(");
  assert.ok(start > 0, "找不到 RecordAction");
  const end = SNAPSHOT.indexOf("\n    private void PruneRecentActions", start);
  assert.ok(end > start);
  return SNAPSHOT.slice(start, end);
}

function buildRecentActionsBody() {
  const start = SNAPSHOT.indexOf("private JsonArray BuildRecentActions(");
  assert.ok(start > 0, "找不到 BuildRecentActions");
  return SNAPSHOT.slice(start, start + 1200);
}

test("上限五条,窗口三十秒", () => {
  assert.match(SNAPSHOT, /MaximumRecentActions = 5/);
  assert.match(SNAPSHOT, /RecentActionsWindow =\s*TimeSpan\.FromSeconds\(30\)/);
});

test("换书清空——上一本书翻到第几页跟这本书无关", () => {
  const body = recordActionBody();
  assert.match(body, /_recentActions\.Clear\(\)/);
  assert.match(body, /_recentActionsFile = file/);
});

test("空文件标识不记录,不留下无法归属的动作", () => {
  const body = recordActionBody();
  assert.match(body, /IsNullOrEmpty\(file\)/);
});

test("读取时重新按当前时间剪一遍,不只信写入时的那一次", () => {
  const body = buildRecentActionsBody();
  assert.match(body, /PruneRecentActions\(\);/);
});

test("给模型的是相对时间,不是原始时间戳——不该让模型自己算现在减去多少", () => {
  const body = buildRecentActionsBody();
  assert.match(body, /secondsAgo/);
  assert.doesNotMatch(body, /\["atMs"\] = entry/,
    "atMs 是内部记账字段,不该原样递给模型");
});

test("翻页只在真的换页时记录,不是每次上报都记", () => {
  const at = SNAPSHOT.indexOf('RecordAction(\n                        "page-turn"');
  assert.ok(at > 0, "找不到 WSS 路径的翻页记录调用");
  const before = SNAPSHOT.slice(Math.max(0, at - 200), at);
  assert.match(before, /if \(changedPage\)/);
});

test("绘图动作用真正折叠后的稳定态判断,不是传入值", () => {
  // FoldDrawingEvent 页不对时原样返回 stablePage;拿传入的 value 猜稳没稳,
  // 会在折叠根本没发生的时候也误判成"刚画完"。
  const start = SNAPSHOT.indexOf('else if (contextEvent.Type == "drawing")');
  const body = SNAPSHOT.slice(start, start + 1400);
  assert.match(body, /JsonObject\? folded = FoldDrawingEvent\(value, _stablePage\);/);
  assert.match(body, /afterDrawing\["stable"\]/,
    "必须读折叠后 folded 里的 stable,不是 value 里的");
  assert.match(body, /!wasStable && nowStable/,
    "只有从不稳定到稳定的那一刻才算'刚画完',不是每次稳定态的重复上报");
});

test("lastEditedAt 按秒解释,不当毫秒读", () => {
  // 单位搞反会让每一次画图动作在 RecordAction 里立刻被 30 秒窗剪掉,
  // 画图这个动作类型就永远不会出现在 recentActions 里,而且不会报错。
  const start = SNAPSHOT.indexOf('else if (contextEvent.Type == "drawing")');
  const body = SNAPSHOT.slice(start, start + 2000);
  assert.match(body, /seconds \* 1000/, "秒转毫秒的换算必须存在");
});

test("清空上下文时一并清空动作记录", () => {
  const start = SNAPSHOT.indexOf("public async Task<DirectSnapshotForwardResult> ClearAsync(");
  const body = SNAPSHOT.slice(start, start + 1200);
  assert.match(body, /_recentActions\.Clear\(\)/);
  assert.match(body, /_recentActionsFile = null/);
});

test("参与事务性回滚——失败的转发不能留下半写的动作记录", () => {
  assert.match(SNAPSHOT, /IReadOnlyList<JsonObject> RecentActions,\s*\n\s*string\? RecentActionsFile\);/);
  const captureAt = SNAPSHOT.indexOf("private AdapterState CaptureState()");
  const captureBody = SNAPSHOT.slice(captureAt, captureAt + 700);
  assert.match(captureBody, /_recentActions/);
  assert.match(captureBody, /_recentActionsFile/);
  const restoreAt = SNAPSHOT.indexOf("private void RestoreState(AdapterState state)");
  const restoreEnd = SNAPSHOT.indexOf("\n    private void LoadExistingState", restoreAt);
  assert.ok(restoreEnd > restoreAt, "找不到 RestoreState 结尾");
  const restoreBody = SNAPSHOT.slice(restoreAt, restoreEnd);
  assert.match(restoreBody, /_recentActions\.Clear\(\)/);
  assert.match(restoreBody, /_recentActionsFile = state\.RecentActionsFile/);
});

test("字段真的挂到了顶层快照上", () => {
  assert.match(SNAPSHOT, /\["recentActions"\] = BuildRecentActions\(\),/);
});

test("待接收状态的兜底对象也带这个字段,不是缺省省略", () => {
  const at = MCP.indexOf('["latestEvent"] = null,');
  assert.ok(at > 0);
  const around = MCP.slice(at, at + 120);
  assert.match(around, /\["recentActions"\] = new JsonArray\(\),/);
});

test("工具描述说明这是历史记录而非待办指令,且如实说明覆盖不全", () => {
  assert.match(MCP, /recentActions lists things the user just did/);
  assert.match(MCP, /never act on an entry unless the user's own/);
  assert.match(MCP, /Coverage is intentionally/);
  assert.match(MCP, /highlighting, word lookups, and sticky notes/,
    "必须明说高亮/查词/便签还没被覆盖,否则空列表会被读成'用户什么都没做'");
});
