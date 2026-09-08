import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import path from "node:path";
import { fileURLToPath } from "node:url";

// 提示板主动推送（2026-09-09 Codex→Claude 交接）。
//
// 这里守的全是**坏掉不留痕迹**的那几条。能在运行时炸出来的（开关、静默、
// 管道名）在 ReaderCodexPushSelfTest 里；打包成单文件后 .cs 不在包内，
// 所以"源码里不许出现什么"只能在这一层断言。
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");
const CS = "extensions/bw-reader-webext/windows/ComputerVoiceAudio/";
const PUSH = read(CS + "ReaderCodexPush.cs");
const ENDPOINT = read(CS + "ReaderCodexEndpoint.cs");
const BOARD = read(CS + "ReaderAttentionBoard.cs");

test("推送挂在真的写了盘之后，而不是渲染之后", () => {
  // 顺序反了会出现"推送说变了、文件还是旧的"，而对面拿到通知第一件事
  // 就是去读文件。
  const flush = BOARD.slice(
    BOARD.indexOf("internal static async Task FlushFilesAsync"),
    BOARD.indexOf("private static bool DecideSlowFlush"),
  );
  const slowWrite = flush.indexOf("bool slowChanged = await WriteIfChangedAsync");
  const push = flush.indexOf("ReaderCodexPush");
  assert.ok(slowWrite > 0 && push > slowWrite, "推送必须排在落盘之后");
  assert.match(flush, /if \(slowChanged \|\| fastChanged\)/);
});

test("写盘函数报告是否真写了，失败时报 false", () => {
  // 推送要挂在这个已经算好的判断上；让调用方再比一次内容就是把同一个
  // 政策写成两份，而慢板"攒 4 次"一旦两边不一致就会各说各话。
  assert.match(BOARD, /private static async Task<bool> WriteIfChangedAsync/);
  const body = BOARD.slice(
    BOARD.indexOf("private static async Task<bool> WriteIfChangedAsync"),
    BOARD.indexOf("private static string RenderBoard"),
  );
  // 内容相同的早退分支必须 return false
  assert.match(body, /&& File\.Exists\(path\)\)\s*\{\s*return false;/);
  // 写成功 return true
  assert.match(body, /File\.Move\(temporary, path, overwrite: true\);\s*return true;/);
  // catch 里清掉记号**并且** return false —— 那一轮对面看到的还是旧内容
  assert.match(body, /LastWritten\.Remove\(path\);\s*\}\s*return false;/);
});

test("登记表变了不推送", () => {
  // 它是清单不是情报，推它等于白唤醒一次。
  const flush = BOARD.slice(
    BOARD.indexOf("internal static async Task FlushFilesAsync"),
    BOARD.indexOf("private static bool DecideSlowFlush"),
  );
  const registry = flush.indexOf("RegistryFileName");
  assert.ok(registry > 0);
  // 注册表那一行前面不能有 bool 赋值
  assert.ok(
    !/bool \w+ = await WriteIfChangedAsync\(\s*\n\s*Path\.Combine\(directory, RegistryFileName\)/.test(flush),
    "登记表的写入结果不该被拿去推送",
  );
});

test("推送只送提醒，不送板面正文", () => {
  // 书页标题和内容是资料不是指令。塞进一条送给模型的提示里，
  // 等于把资料升级成有执行权的命令。
  for (const forbidden of ["slowToWrite", "RenderSlow", "RenderFast",
    "SlowFileName", "FastFileName"]) {
    assert.ok(
      !PUSH.includes(forbidden),
      `推送模块不该碰 ${forbidden}（那是板面正文一侧的东西）`,
    );
  }
  assert.ok(PUSH.includes("板面文件是权威"), "提示语要说清板面才是权威");
});

test("推送绝不碰业务 ack", () => {
  // success=true 只代表接口收下了。提前 ack 的后果是通知消失而人根本
  // 没被告知，且没有一处会报错。
  for (const forbidden of ["Acknowledge", "acknowledge", "Resolve(", '"ack"']) {
    assert.ok(!PUSH.includes(forbidden), `推送模块不该出现 ${forbidden}`);
  }
  assert.ok(
    PUSH.includes("不代表任何通知已交付用户"),
    "提示语要写明接口收下 != 已交付",
  );
});

test("默认关，且注册不会顺手打开", () => {
  // 消费端还在轮询时两条都开就是双发。
  assert.match(PUSH, /private static bool _enabled;/);
  assert.ok(
    !/_enabled\s*=\s*true\s*;/.test(
      PUSH.slice(0, PUSH.indexOf("internal static void SetEnabled")),
    ),
    "字段不该初始化成 true",
  );
  // 注册体里 enabled 缺省时不改开关
  assert.match(ENDPOINT, /bool\? wantEnabled/);
  assert.match(ENDPOINT, /if \(wantEnabled is bool decided\)/);
});

test("失效由连续推送失败判定，时钟只是远期兜底", () => {
  // 2026-09-09 用户当场问了那个 6 小时 TTL：时钟答不出目标死没死，
  // 而推送本身答得出。更糟的是 6 小时会在长会话**中途**把活绑定杀掉，
  // 制造出它本要防的静默停摆。两种错的代价还不对称。
  assert.match(PUSH, /ConsecutiveFailureLimit/);
  assert.match(PUSH, /ReaderCodexEndpoint\.Invalidate\(/);
  // 成功要清零，否则零星失败攒着攒着也会判死
  assert.match(PUSH, /_consecutiveFailures = 0;\s*\/\/ 成功一次就把计数清零/);
  const current = ENDPOINT.slice(
    ENDPOINT.indexOf("internal static Binding? Current()"),
    ENDPOINT.indexOf("internal static async Task WriteResponseAsync"),
  );
  // 实测判定优先于时钟
  assert.ok(
    current.indexOf('invalidReason') < current.indexOf("Lifetime"),
    "失效判定要排在时钟之前",
  );
  assert.match(current, /now - at > \(long\)Lifetime\.TotalMilliseconds/);
  // 兜底要足够远，不能又变成一个会杀活绑定的钟
  assert.match(ENDPOINT, /Lifetime = TimeSpan\.FromHours\((\d+)\)/);
  const hours = Number(/Lifetime = TimeSpan\.FromHours\((\d+)\)/.exec(ENDPOINT)[1]);
  assert.ok(hours >= 48, `兜底期限 ${hours} 小时太短，会在长会话中途杀掉活绑定`);
});

test("失效要留下原因，重新登记要清掉它", () => {
  // 删文件的话，再问"为什么不推了"就没有答案 —— 这条链没有界面。
  assert.match(ENDPOINT, /internal static void Invalidate\(string reason\)/);
  assert.match(ENDPOINT, /\["invalidReason"\] = reason/);
  // 重新登记 = "我又活了"
  assert.match(ENDPOINT, /\["invalidReason"\] = null/);
  // 并且要把上一次死过的事回给 AI，否则它不知道中间断过
  assert.match(ENDPOINT, /previousInvalidReason/);
});

test("地址和任务 id 一律不写死", () => {
  // 交接明说：安装路径带版本号会变，管道地址是动态的。
  for (const source of [PUSH, ENDPOINT]) {
    assert.ok(!/WindowsApps/.test(source), "不许出现 Codex 安装路径");
    assert.ok(
      !/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/.test(source),
      "不许写死某个具体任务 id",
    );
  }
  // 工具的 namespace 要用对面返回的，不能猜
  assert.match(PUSH, /nameSpace = \(string\?\)tool\["namespace"\]/);
  assert.ok(
    PUSH.includes("猜 namespace"),
    "要写明为什么不能猜 namespace",
  );
});

test("不自己攒重试队列", () => {
  // 下一轮板面若仍与对面不同会再写再推。自攒重试会把"同一件事说两遍"
  // 变成常态。
  assert.ok(!/retry|Retry/.test(PUSH), "推送模块不该有重试逻辑");
  assert.ok(PUSH.includes("不做重试"), "要写明为什么不重试");
});

test("端点四处注册齐了", () => {
  assert.match(ENDPOINT, /RoutePath = "\/reader-codex-endpoint\/v1"/);
  const server = read(CS + "DirectBridgeServer.cs");
  assert.ok(server.includes("ReaderCodexEndpoint.RoutePath"));
  assert.ok(server.includes("HandleCodexEndpointAsync"));
  const core = read(
    "extensions/bw-reader-webext/windows/computer-voice-desktop/bridge_core.py",
  );
  assert.ok(core.includes('"/reader-codex-endpoint/v1"'));
  const preflight = read("extensions/bw-reader-webext/release_preflight.py");
  assert.ok(preflight.includes("ComputerVoiceAudio/ReaderCodexPush.cs"));
  assert.ok(preflight.includes("ComputerVoiceAudio/ReaderCodexEndpoint.cs"));
});

test("旧的 1 秒渲染没有被顺手删掉", () => {
  // 板面文件仍是诊断面和回退路径，登记表也从它算。
  const server = read(CS + "DirectBridgeServer.cs");
  assert.match(server, /AttentionBoardFlushInterval =\s*\n?\s*TimeSpan\.FromSeconds\(1\)/);
  assert.ok(server.includes("MonitorAttentionBoardAsync"));
});
