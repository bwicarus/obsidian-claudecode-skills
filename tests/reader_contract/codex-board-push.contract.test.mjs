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
//: 入站闸的两份副本。放行表漏一处的表现是「整条 STATUS 被拒」。
const COPIES_CV = [
  "_server_deploy/static/pdf/rc-computer-voice.js",
  "extensions/bw-reader-webext/vendor/rc-computer-voice.js",
];
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
  // 2026-09-10：快板多了一层安静窗口，条件从 fastChanged 变成 pushFast。
  // 守的东西没变 —— **只在真的有新东西时才推** —— 只是"有没有新东西"现在
  // 由 ShouldPushFast 回答（内容与上一次真送到的不同，且过了窗口或是紧急）。
  // ⚠ 慢板仍然直接用 slowChanged：待办是祈使句，不进窗口。
  assert.match(flush, /if \(slowChanged \|\| pushFast\)/);
  assert.match(flush, /pushFast = ShouldPushFast\(/);
  // 传给推送的是 pushFast 而不是 fastChanged —— 传错的话窗口压下的那一轮
  // 会被当成"快板变了"照样送出去，等于窗口不存在。
  assert.match(flush, /NotifyBoardChangedAsync\(\s*slowChanged, pushFast,/);
});

test("快板安静窗口压得住上下文，压不住挂断", () => {
  // 「他主动挂断了电话」的语义是"看到就停止向通话说话"，压它 90 秒
  // 等于让 AI 对着已经挂断的电话继续说一分半。
  const decide = BOARD.slice(
    BOARD.indexOf("internal static bool ShouldPushFast"),
    BOARD.indexOf("internal static readonly TimeSpan FastQuietWindow"),
  );
  assert.ok(decide.length > 0, "找不到 ShouldPushFast");
  // 内容与"上一次真送到的"相同就不推（无变化静默的延伸）
  assert.match(decide, /string\.Equals\(fast, lastDelivered/);
  // 紧急标记必须排在窗口判断**之前**（|| 的左边）
  const urgent = decide.indexOf("HangUpMarker");
  const window = decide.indexOf("FastQuietWindow");
  assert.ok(urgent > 0 && window > urgent, "挂断必须先于窗口被判定");
});

test("送达才算数：判去重用真送到的，不用我们试过的", () => {
  // 通道断着时每一轮都会失败；那时记成"已推"，通道恢复后这份内容
  // 再也不会被送出去 —— 通知丢失里最难查的那种。
  assert.match(BOARD, /internal static void NoteFastBoardDelivered/);
  assert.match(PUSH, /ReaderAttentionBoard\s*\.NoteFastBoardDelivered/);
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

test("推送直接带上变了那块板的全文", () => {
  // 用户 2026-09-09：「直接把快慢板内容发过去就好，只是快板有变化就发快板的
  // 全部内容，慢板同理」。原来只发一句"有更新，去读文件"，对面每次还要
  // 再读一趟 —— 而板面本来就短，那一趟纯属多余。
  assert.match(
    PUSH,
    /NotifyBoardChangedAsync\(\s*bool slowChanged,\s*bool fastChanged,\s*string slowText,\s*string fastText,/);
  assert.ok(PUSH.includes("【快板】") && PUSH.includes("Trim(fastText)"),
    "快板变了要带快板全文");
  assert.ok(PUSH.includes("【慢板】") && PUSH.includes("Trim(slowText)"),
    "慢板变了要带慢板全文");
  // 只带**变了的那块**：没变的那块对面手上已经有了
  const region = PUSH.slice(
    PUSH.indexOf("var body = new StringBuilder()"),
    PUSH.indexOf("string prompt = body.ToString()"));
  assert.match(region, /if \(fastChanged\)/);
  assert.match(region, /if \(slowChanged\)/);
  // 病态长度要收口，不能让对面吞下整块
  assert.match(PUSH, /板面过长已截断/);
});

test("推送是支线，绝不能拖住或弄坏渲染", () => {
  // 2026-09-09 实测：await 推送之后，板子只渲了启动那一次，之后新建通知、
  // 翻书、画图全都不再更新，而每个文件都好端端躺着 —— 因为渲染循环的异常
  // 没有任何人观察（那个 task 只在关服时被 await 一次）。
  const flush = BOARD.slice(
    BOARD.indexOf("internal static async Task FlushFilesAsync"),
    BOARD.indexOf("private static bool DecideSlowFlush"));
  assert.match(flush, /_ = ReaderCodexPush\.NotifyBoardChangedAsync\(/);
  assert.ok(
    !/await ReaderCodexPush/.test(flush),
    "不许 await 推送：一次推送最长能占住 28 秒，而这个循环每秒渲一次");
  assert.match(flush, /CancellationToken\.None/);
  // 循环本身也要吞掉异常并留下原因
  const server = read(CS + "DirectBridgeServer.cs");
  assert.match(server, /private static async Task FlushOnceAsync/);
  assert.match(server, /ReaderAttentionBoard\.NoteFlushFailure\(exception\)/);
  assert.match(server, /catch \(OperationCanceledException\)\s*\{\s*throw;/);
  assert.match(BOARD, /internal static string LastFlushFailure/);
  // ⚠ 光记下来不够：得有地方读得到。那个循环的异常没有任何人观察，
  // 板子停更跟"状态确实没变"长得一模一样。登记响应就是它的出口。
  assert.match(ENDPOINT, /\["boardFlushFailure"\]/);
  assert.match(ENDPOINT, /ReaderAttentionBoard\.LastFlushFailure/);
});

test("推送绝不碰业务 ack", () => {
  // success=true 只代表接口收下了。提前 ack 的后果是通知消失而人根本
  // 没被告知，且没有一处会报错。
  for (const forbidden of ["Acknowledge", "acknowledge", "Resolve(", '"ack"']) {
    assert.ok(!PUSH.includes(forbidden), `推送模块不该出现 ${forbidden}`);
  }
  // ⚠ 这条纪律现在**写在 AGENTS.md 里**，不再每条推送重复一遍：
  // 推送带上板面全文之后，每次再附一句"本条不代表已交付"就是纯噪音，
  // 而用户要的是紧凑。代码这一侧只保证**不去碰** ack，上面那几条就是。
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

test("接上时推一次全量，否则登记前就摆在板上的东西永远送不出去", () => {
  // 用户 2026-09-09 点出来的缺口：推送只在**变化时**触发，而"接上之前
  // 就已经存在的待办"不构成变化。不补这一下，那条待办会一直躺着，
  // 而两边都不会觉得有问题 —— 板上明明写着，推送也从没出错。
  assert.match(PUSH, /internal static async Task NotifyConnectedAsync/);
  // 接上这一条同样**直接带正文**（2026-09-09 用户：「不是说了直接推送快慢板
  // 内容么怎么现在还是这种提醒」）。两条走同一条运输，一条给内容一条给指路
  // 说不通。
  assert.ok(
    PUSH.includes("ReaderAttentionBoard.CurrentBoards()"),
    "接上时要取板面正文");
  assert.ok(
    PUSH.includes("Trim(fastNow)") && PUSH.includes("Trim(slowNow)"),
    "接上时两块板都要带上（对面手上什么都还没有）");
  assert.ok(
    !PUSH.includes("把快板和慢板都完整读一遍"),
    "不许再发那句叫人去读的提醒");
  // 取的是**落盘内容**：现渲会给出一份还没落盘的版本，跟板面文件对不上
  const boards = BOARD.slice(
    BOARD.indexOf("internal static (string Slow, string Fast) CurrentBoards()"),
    BOARD.indexOf("/// 慢板这一轮要不要落盘"));
  assert.match(boards, /_lastSlowText, _lastFastText/);
  // 登记之后触发，且**不等**它
  assert.match(ENDPOINT, /_ = ReaderCodexPush\.NotifyConnectedAsync\(/);
  // ⚠ 必须 CancellationToken.None：拿请求的 token 的话响应一返回推送就被
  //   取消，表现是"登记成功但全量提醒从来没到"，没有一处会报错。
  assert.match(
    ENDPOINT,
    /NotifyConnectedAsync\(\s*announceTarget, CancellationToken\.None\)/);
  // 这一条失败不该判绑定失效 —— 刚登记完就判死太急
  const body = PUSH.slice(
    PUSH.indexOf("internal static async Task NotifyConnectedAsync"),
    PUSH.indexOf("/// 板面变了"));
  assert.ok(!/Invalidate\(/.test(body), "接上提醒失败不判绑定失效");
});

test("续期不再重复发接通提醒", () => {
  // Codex 2026-09-09 报的重复：钩子每次用户发言都续登记，而接通提醒原来挂在
  // "每次登记"上，于是每说一句话就收到一条"把两块板完整读一遍"。
  // ⚠ 终点用 lastIndexOf：注销分支里先出现过一次 `await Ok(context`，
  // 用 indexOf 会把切片切成空串，于是断言全部"通过"而什么都没检查。
  const region = ENDPOINT.slice(
    ENDPOINT.indexOf("lock (RegistrationGate)"),
    ENDPOINT.lastIndexOf("await Ok(context"));
  assert.ok(region.length > 500, "切片没取到登记那一段");
  // 只有真接上才发，四种情形写在条件里
  assert.match(region, /shouldAnnounce = pushEnabledAfter/);
  assert.match(region, /previous is null/);
  assert.match(region, /!sameTarget/);
  assert.match(region, /!wasEnabled/);
  assert.match(region, /previousInvalid\.Length > 0/);
  // 「同一个有效绑定」= 对话和管道都没变
  assert.match(region, /string\.Equals\(previousPipe, pipeName/);
  assert.match(region, /string\.Equals\(previousThread, threadId/);
});

test("读旧状态到写新绑定整段互斥", () => {
  // 两个钩子几乎同时登记同一目标时，不锁的话两边各自读到"还没登记过"，
  // 于是各发一次。
  assert.match(ENDPOINT, /private static readonly object RegistrationGate/);
  const region = ENDPOINT.slice(
    ENDPOINT.indexOf("lock (RegistrationGate)"),
    ENDPOINT.indexOf("if (writeError is not null)"));
  // 读旧、写新、定开关、下决定必须都在锁里
  for (const needle of ["ReadRecord()", "File.Move(temporary, path",
    "ReaderCodexPush.SetEnabled(decided)", "shouldAnnounce ="]) {
    assert.ok(region.includes(needle), `${needle} 必须在 RegistrationGate 里`);
  }
  // ⚠ 锁里不许 await
  assert.ok(!/await /.test(region), "锁里不能有 await");
});

test("接通提醒绑定到这次登记定下的目标", () => {
  // 异步发送时全局绑定可能已被另一段对话覆盖，那样提醒会发到别人那里。
  assert.match(PUSH, /NotifyConnectedAsync\(\s*ReaderCodexEndpoint\.Binding binding,/);
  const body = PUSH.slice(
    PUSH.indexOf("internal static async Task NotifyConnectedAsync"),
    PUSH.indexOf("/// 板面变了"));
  assert.ok(
    !/ReaderCodexEndpoint\.Current\(\)/.test(body),
    "不许在这里重读全局绑定");
  assert.match(
    ENDPOINT,
    /NotifyConnectedAsync\(\s*announceTarget, CancellationToken\.None\)/);
});

test("换目标或从失效恢复时失败计数清零", () => {
  // 不清的话上一个死绑定攒下的次数会记在新目标头上，新目标可能一上来就被判死。
  assert.match(PUSH, /internal static void ResetFailures\(\)/);
  const region = ENDPOINT.slice(
    ENDPOINT.indexOf("lock (RegistrationGate)"),
    ENDPOINT.indexOf("if (writeError is not null)"));
  assert.match(region, /ReaderCodexPush\.ResetFailures\(\)/);
});

test("响应说清这次是接上还是续期", () => {
  // 否则验收时分不清"没发"和"发丢了"。
  assert.match(ENDPOINT, /\["announcedConnect"\] = shouldAnnounce/);
  assert.match(ENDPOINT, /\["renewedOnly"\]/);
});

test("两条通道都要报焦点：扩展走 HTTP，App 走直连", () => {
  // ⚠⚠ 这是本轮最贵的一课。`ForwardActiveReadingAsync` 有**两个**调用点：
  //   DirectBridgeServer 的 HTTP POST（浏览器扩展走）和 DirectBridgeProtocol
  //   的直连通道（**App 走**）。我第一版只补了前者，于是表现正好是用户报的
  //   "扩展里打开新网页板子更新了，但 app 书换页没变化"。
  //   CLAUDE.md 那条"先数清楚它有几份副本"，这是又一次实证。
  const server = read(CS + "DirectBridgeServer.cs");
  const protocol = read(CS + "DirectBridgeProtocol.cs");
  for (const [label, source] of [["HTTP POST", server], ["直连通道", protocol]]) {
    assert.ok(
      source.includes("ReaderAttentionBoard.NoteLocation("),
      `${label} 这一支没有上报焦点`);
  }
  // 两边的判定必须逐字一致，分叉的表现是"某些设备上焦点不动"
  for (const source of [server, protocol]) {
    assert.ok(source.includes('activeReading.SelectionState, "active"'));
    assert.ok(source.includes('activeReading.File + "#" + '));
  }
});

test("新页上划选即确认焦点，不必等满 45 秒", () => {
  // 用户 2026-09-09：「不是除了 45s 还有新页面操作后直接刷新的机制么」——
  // 有，我接得太窄：原来判 Selection 非空，改成判 SelectionState。
  // 状态是权威的那一个，三种取值里只有 active 表示他真的划了。
  const server = read(CS + "DirectBridgeServer.cs");
  const region = server.slice(
    server.indexOf("阅读器自己的焦点"),
    server.indexOf("if (viewport is not null)"));
  assert.match(region, /interacted: string\.Equals\(/);
  assert.ok(region.includes('activeReading.SelectionState, "active"'));
  // ⚠ 不许把 HighlightSource 算进来：那是这一页**已有的**高亮不是这一刻的
  //   动作，算进来的话翻到任何画过线的页都会立刻确认，45 秒门槛形同虚设。
  // 只看 interacted: 那个表达式本身 —— 用整段去查会把上面那句注释也算进去。
  const interacted = region.slice(
    region.indexOf("interacted: string.Equals("),
    region.indexOf("StringComparison.Ordinal));"));
  assert.ok(interacted.length > 0, "没定位到 interacted 表达式");
  assert.ok(
    !/HighlightSource/.test(interacted),
    "已有的高亮不是操作，不能拿它确认焦点");
  assert.ok(interacted.includes("SelectionState"));
});

test("焦点判定的现场看得见", () => {
  // 一次判定有三种可能：没收到 / 收到但还在等停留 / 已确认。
  // 三者处置完全不同，而 2026-09-09 查这件事时**一种都看不到**，
  // 只能靠建测试通知去试。
  assert.match(BOARD, /internal static string FocusDiagnosis\(\)/);
  const body = BOARD.slice(
    BOARD.indexOf("internal static string FocusDiagnosis()"),
    BOARD.indexOf("/// 最近一次渲染失败的原因"));
  assert.ok(body.includes("还没收到任何焦点上报"), "要能说出「没收到」");
  assert.ok(body.includes("已等 "), "要能说出候选等了多久");
  assert.match(ENDPOINT, /\["boardFocus"\] = ReaderAttentionBoard\.FocusDiagnosis\(\)/);
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

// ── 挂断请求走同一条通道（2026-09-09）──────────────────────────────────
test("挂断请求复用推送通道，不另造一份协议", () => {
  assert.match(PUSH, /RequestVoiceHangUpAsync\(/);
  // 只能有一处实现帧协议：找 tools/call 的地方应当只有 SendAsync。
  assert.equal((PUSH.match(/"tools\/call"/g) || []).length, 1);
  // 目标线程可另指：挂断要发给**正在通话**的线程，不是板推送那条。
  assert.match(PUSH, /threadIdOverride/);
  assert.match(PUSH, /threadIdOverride: inCallThreadId/);
});

test("挂断文案如实说明它是用户预设规则触发的", () => {
  // 那个工具的自述是「Only call this tool if the user explicitly asks…」，
  // 所以文案必须说清这是用户事先定的规则，不能编成"用户刚说要挂"。
  const start = PUSH.indexOf("RequestVoiceHangUpAsync(");
  const body = PUSH.slice(start, PUSH.indexOf("\n    private static async Task SendAsync", start));
  assert.match(body, /用户预先设定的自动关闭规则触发了/);
  assert.match(body, /用户本人事先在设置里定下的/);
  assert.match(body, /end_realtime_voice_call/);
  assert.match(body, /不要只回复文字/);
  // 请求编号要带上，重复出现说明上一次没生效。
  assert.match(body, /请求编号/);
});

test("挂断失败不连累板推送的绑定", () => {
  const start = PUSH.indexOf("RequestVoiceHangUpAsync(");
  const body = PUSH.slice(start, PUSH.indexOf("\n    private static async Task SendAsync", start));
  // 不判绑定失效、不累加连续失败计数：挂不掉往往只是对面正忙。
  assert.doesNotMatch(body, /Invalidate\(/);
  assert.doesNotMatch(body, /_consecutiveFailures/);
  // 也不看板推送开关：板子推不推与"到点该挂断"是两件事。
  assert.doesNotMatch(body, /if \(!Enabled\)/);
});

test("端点只回「送出去了没有」，不谎称已挂断", () => {
  const start = ENDPOINT.indexOf('body["hangUpVoice"]');
  assert.ok(start >= 0, "端点要有 hangUpVoice 分支");
  const body = ENDPOINT.slice(start, start + 1800);
  assert.match(body, /hangUpRequested/);
  assert.match(body, /关没关成要看麦克风台账/);
  // 缺目标线程直接拒，不拿板推送那条线程顶替。
  assert.match(body, /hangUpVoice 需要 threadId/);
});

// ── 状态查询与回执（2026-09-09）────────────────────────────────────────
test("状态查询只要回答，且明确不许改变状态", () => {
  const start = PUSH.indexOf("RequestStatusReportAsync(");
  assert.ok(start >= 0, "要有状态查询");
  const body = PUSH.slice(start, PUSH.indexOf("\n    private static async Task SendAsync", start));
  assert.match(body, /不要开启语音、不要发送快捷键、不要重试/);
  // 回答方式是跑脚本，不是让它手写 JSON —— 契约由程序保证。
  assert.match(body, /voice_status_receipt\.py/);
  assert.match(body, /--request-id/);
  assert.match(body, /--voice-status/);
  // Codex 自己点名的两条纪律要写进文案
  assert.match(body, /没收到语音消息不能推断成 ended/);
  assert.match(body, /不知道就别加/);
});

test("端点不把「送出去」说成「已回答」", () => {
  const start = ENDPOINT.indexOf('body["statusQuery"]');
  assert.ok(start >= 0);
  const body = ENDPOINT.slice(start, start + 1800);
  assert.match(body, /statusRequested/);
  assert.match(body, /回执写没写要看回执账本/);
});

test("台账读不到 ≠ 已经挂断", () => {
  // 两者都不该按 F24，但原因必须分开说：把不知道折成结论，
  // 排查的人就会去错的方向。
  const start = ENDPOINT.indexOf('body["hangUpVoiceFallback"]');
  const body = ENDPOINT.slice(start, start + 2200);
  assert.match(body, /ledgerKnown/);
  assert.match(body, /台账读不到，不知道在不在通话/);
});

// ── 语音入口梯子（2026-09-09）────────────────────────────────────────
test("桥总是捎带梯子，读不到就是 null 而不是编一个就绪", () => {
  const proto = read(CS + "DirectBridgeProtocol.cs");
  assert.match(proto, /ladder = ReadVoiceLadder\(\)/);
  const start = proto.indexOf("private static object? ReadVoiceLadder()");
  assert.ok(start >= 0);
  const body = proto.slice(start, proto.indexOf("\n    private", start + 10));
  // 任何读失败都 null；不知道要如实说
  assert.match(body, /return null;/);
  assert.match(body, /voice-ladder-status\.json/);
  // 只捎带界面要用的那几项，不整份透传
  for (const field of ["reached", "total", "label", "blockedAt", "reachable"]) {
    assert.ok(body.includes(field), `缺字段 ${field}`);
  }
});

test("入站闸放行 ladder —— 漏掉会让整条 STATUS 被拒", () => {
  for (const copy of COPIES_CV) {
    const source = read(copy);
    const start = source.indexOf("function normalizeCodexVoicePayload(");
    assert.ok(start >= 0, copy);
    const body = source.slice(start, source.indexOf("\n  function ", start + 10));
    assert.match(body, /"keepActive", "ladder", "push"/);
  }
});

test("第 1 级够不到时不许继续闪", () => {
  // 闪代表"在推进"，而那一级推进不了；一直闪跟"已经不会成了"长得一样。
  const voice = read("_server_deploy/static/pdf/rc-voicecall.js");
  const start = voice.indexOf("function _applyLadder(ladder)");
  assert.ok(start >= 0);
  const body = voice.slice(start, voice.indexOf("\n  function ", start + 10));
  assert.match(body, /ladder\.reachable === false/);
  assert.match(body, /computerBtnConnecting\(false\)/);
});

test("绿灯：梯子说得准时就别去问台账", () => {
  // 用户 2026-09-10/11 连着两次实录：「codex 冷启动后按钮就变成了绿色但是
  // 语音没通」「一样，先绿然后才连上」。
  //
  // 病根是**同一份 payload 里两个来源矛盾，而我们用了弱的那个**：
  //   · 麦克风台账 —— Codex 刚拉起来时条目还不存在 → 'unknown' → 放行绿灯；
  //   · 梯子 session 级 —— known:true / satisfied:false，明确说"没有通话"。
  //
  // 下面这份 rungs 是 2026-09-11 00:20 从桥上 voice-ladder-status.json
  // 原样取的，不是编的。
  const voice = read("_server_deploy/static/pdf/rc-voicecall.js");
  const from = voice.indexOf("var LADDER_FRESH_MS");
  const to = voice.indexOf("function _greenLightAllowed");
  assert.ok(from >= 0 && to > from, "找不到判据函数");
  const body = voice.slice(from, to);
  // ⚠ ESM 里的 eval 是严格模式，函数声明**不会**泄漏到外面 —— 所以取
  // 最后一个表达式的值把函数拿出来（它闭包着同一次 eval 里的 _sessionRungEvidence）。
  // eslint-disable-next-line no-eval
  const evidence = eval(body + String.fromCharCode(10) + "_sessionEvidence");

  const cold = {
    codexVoice: {
      status: "unavailable",
      ladder: { atUtcMs: Date.now(), rungs: [
        { key: "server", known: true, satisfied: true },
        { key: "chain", known: true, satisfied: true },
        { key: "codex", known: true, satisfied: true },
        { key: "session", known: true, satisfied: false,
          why: "当前没有进行中的通话" },
      ] },
    },
  };
  assert.equal(
    evidence(cold), false,
    "梯子明说没有通话，却没被当成确定的否定 —— 绿灯会提前亮");

  const live = JSON.parse(JSON.stringify(cold));
  live.codexVoice.ladder.rungs[3].satisfied = true;
  assert.equal(evidence(live), true);

  // 梯子自己也不知道时才回退到台账 —— 那时 'unknown' 才是诚实的。
  assert.equal(evidence({ codexVoice: { status: "unavailable" } }),
    "unknown");
  const vague = JSON.parse(JSON.stringify(cold));
  vague.codexVoice.ladder.rungs[3].known = false;
  assert.equal(evidence(vague), "unknown");

  // ⚠ 梯子是 ReaderPC **每 30 秒无条件写一次**的静态读数，只在新鲜时可信。
  // 拿一份过期读数当现状，两个方向都会错：刚接通时按钮多黄闪半分钟，
  // 刚挂断时又绿着。2026-09-11 我把它设成第一优先级时正好埋了前一个坑。
  const stale = JSON.parse(JSON.stringify(cold));
  stale.codexVoice.ladder.atUtcMs = Date.now() - 40000;
  assert.equal(evidence(stale), "unknown", "过期的梯子不许冒充现状");

  // 桥是**请求那一刻现读**的，比梯子新 —— 有它就该用它。
  const fresher = JSON.parse(JSON.stringify(cold));
  fresher.codexVoice.status = "available";
  fresher.codexVoice.active = true;          // 梯子仍说 satisfied:false
  assert.equal(evidence(fresher), true, "现读的结论没有压过陈旧的梯子");
});

test("绿灯只有一条上漆路径，且它自己带闸", () => {
  // 我 2026-09-10 给 _greenLightAllowed 加宽限时**先数过**这一条：
  // 只要有第二处直接 computerBtnOn(true)，闸就形同不存在。
  const voice = read("_server_deploy/static/pdf/rc-voicecall.js");
  const greens = voice.split("computerBtnOn(true)").length - 1;
  assert.equal(greens, 1, "变绿的地方多于一处 —— 闸拦不住");
  const paint = voice.slice(
    voice.indexOf("function _paintComputerVoiceConnected"),
    voice.indexOf("function _applyNativeComputerVoiceState"),
  );
  assert.match(paint, /_greenLightAllowed\(\)/);
});

test("梯子轮询只在连接期间开着", () => {
  const voice = read("_server_deploy/static/pdf/rc-voicecall.js");
  const start = voice.indexOf("function _startLadderProgress(");
  assert.ok(start >= 0);
  const body = voice.slice(start, voice.indexOf("\n  function ", start + 10));
  // availability() 会发一次 STATUS，不该常年开着
  assert.match(body, /_computerVoiceStarting/);
  assert.match(body, /_stopLadderProgress\(\)/);
});

test("过期的梯子不许冒充现状", () => {
  // 2026-09-09 实测抓到：ReaderPC 在通话中写下「语音已连接」，随后通话结束、
  // 服务停止、ReaderPC 退出，而那份文件原样留着 —— 界面照着显示"已连接"，
  // 而实际上语音是关的。宁可什么都不显示，也不能显示一份会撒谎的状态。
  const proto = read(CS + "DirectBridgeProtocol.cs");
  const start = proto.indexOf("private static object? ReadVoiceLadder()");
  const body = proto.slice(start, proto.indexOf("\n    private", start + 10));
  assert.match(body, /atUtcMs/);
  assert.match(body, /VoiceLadderMaxAgeMs/);
  // 上限要明显大于发布周期(30s)，否则正常刷新的间隙也会被当成过期
  const cap = proto.match(/VoiceLadderMaxAgeMs = ([\d_]+);/);
  assert.ok(cap, "要有上限常量");
  assert.ok(Number(cap[1].replace(/_/g, "")) >= 60000);
  // Python 侧必须真的盖时间戳，否则上面这条判据永远判不了
  const ladder = read(
    "extensions/bw-reader-webext/windows/computer-voice-desktop/voice_ladder.py",
  );
  assert.match(ladder, /"atUtcMs": int\(now \* 1000\)/);
});
