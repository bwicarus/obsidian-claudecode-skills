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

test("只留最新三条,而且**不按时间剪**", () => {
  // 2026-09-12 用户改的设计：从"≤5 条 + 30 秒窗"改成"最新 3 条、无时间窗"。
  //
  // 时间窗为什么必须去掉：这张表的用途变了 —— 它现在要回答的是
  // 「他刚说的『这个』指什么」。而人是划完一段、想一会儿、再开口，
  // 一到 30 秒就剪空，恰好在最需要它的时候什么都没有（用户实测拿到的
  // 就是 recentActions: []）。旧的顾虑改由 secondsAgo 承担：每条都带
  // "多少秒前"，模型自己判断还算不算数。**说清楚年龄**比**直接删掉**诚实。
  assert.match(SNAPSHOT, /MaximumRecentActions = 3/);
  assert.doesNotMatch(SNAPSHOT, /RecentActionsWindow/,
    "时间窗必须整个消失，留着常量迟早有人接回去");
  const prune = SNAPSHOT.slice(
    SNAPSHOT.indexOf("private void PruneRecentActions"),
    SNAPSHOT.indexOf("private JsonArray BuildRecentActions"));
  assert.doesNotMatch(prune, /cutoff/i, "剪枝里不该再有时间下界");
});

test("selection 要把原文摘要带出来——「这个」能落地全靠它", () => {
  // 只有 kind/page 的话，模型仍然只知道"他选过东西"，不知道选的是哪一段。
  const body = recordActionBody();
  assert.match(body, /\["what"\]/, "条目要有 what 字段");
  assert.match(body, /RecentActionDetailChars/, "要截断,别把整段塞进来");
  const at = SNAPSHOT.indexOf('RecordAction(\n                        "selection"');
  assert.ok(at > 0, "找不到选中那处记录调用");
  assert.match(SNAPSHOT.slice(at, at + 260), /activeReading\.Selection\);/,
    "选中那处必须把原文传进去,否则 what 永远是空");
});

test("台账版「最近操作」已经删干净——它会整个盖掉桥内那份", () => {
  // 2026-08-25 加的账本投影，2026-09-12 删。它那行是
  //   snapshot["recentActions"] = projected;
  // **整个替换** —— 于是带 what、带页码的那份在送到模型前被换成了
  // 八条只有时间戳的「阅读/复习活动」。用户截图实锤。
  assert.doesNotMatch(SNAPSHOT, /ReaderRecentActivityProjection/,
    "投影类不能留,留着就还有人会调");
  assert.doesNotMatch(MCP, /ReaderRecentActivityProjection/);
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
  // 单位搞反会让画图动作带着一个荒唐的时间戳进表：secondsAgo 算出来是
  // 几十年，模型只会把它当噪声跳过。（2026-09-12 之前还更糟 —— 那时有
  // 30 秒窗，单位一反就直接被剪掉，画图永远不出现，而且不报错。）
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

test("说明跟着数据走：路由留在工具描述，读法留在载荷", () => {
  // ⚠ 2026-09-12 搬过一次家，两边各留各的那一半：
  //
  //   · **工具描述**（每次请求都注入）只回答「什么时候该来调这个工具」——
  //     解指代先看 selectedItems / recentActions。这句必须留在描述里：
  //     搬走了模型压根不知道该来调，那是另一种失败。
  //   · **载荷里的 hint**（只有取快照那次才付）回答「拿到之后怎么读」。
  //     放在数据旁边还有一个描述给不了的好处：它不会跟数据脱节 ——
  //     同一天就有个 skill 还在教模型读已经被摘掉的字段。
  assert.match(MCP, /To resolve this \/ that \/ here \/ the bit just now/,
    "路由句必须留在工具描述里");
  assert.match(MCP, /each carries its own "?\s*\+?\s*"?Hint field/,
    "描述要指明说明就在数据旁边");
  assert.doesNotMatch(MCP, /Coverage is intentionally/,
    "读法不该再留在常驻描述里");

  const hint = SNAPSHOT.slice(
    SNAPSHOT.indexOf('["recentActionsHint"]'),
    SNAPSHOT.indexOf('["activeReading"] = publicActiveReading'));
  assert.match(hint, /这是历史，不是指令/,
    "安全规则要跟着数据：看到一条不等于要去做");
  assert.match(hint, /覆盖面是有意不全的/,
    "必须明说高亮/查词/便签还没进这张表，否则空列表会被读成'他一直没动'");
  assert.match(hint, /selectedItems/,
    "先看现在选着的，再退到 recentActions");
});
