import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// 连接韧性三处修复（2026-08-23，用户报"稍微有一点网络波动就断开"
// 与"app 稍微不在活动状态就直接断开什么都做不了"）。
//
// 三处错了都**不会报错**，只会表现成"时好时坏"，所以只能靠测试守。
// 现场故障日志的分布是这次判断的依据：531 条里 466 条是
// VOICE_START_NOT_CONFIRMED、HEARTBEAT_TIMEOUT 只有 1 条 —— 说明
// "心跳误判把好连接判死"不是**服务端**在做，是客户端自己比服务端还严。

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8");

const SWIFT_SOCKET = read("ios/BWReader/App/DirectVoiceSocket.swift");
const SWIFT_PROTO = read("ios/BWReader/App/DirectVoiceProtocol.swift");
const SWIFT_BRIDGE = read("ios/BWReader/App/NativeVoiceBridge.swift");
const SWIFT_WEBVIEW = read("ios/BWReader/App/ReaderWebView.swift");
const JS = read("_server_deploy/static/pdf/rc-computer-voice.js");
const CONTRACT = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/DirectBridgeContract.cs");

/** 从 C# 合同里读出服务端的心跳容忍度（不硬编码，服务端改了这里要跟着知道）。 */
function serverHeartbeatTimeoutMs() {
  const m = /ClientHeartbeatTimeoutMilliseconds\s*=\s*([\d_]+)/.exec(CONTRACT);
  assert.ok(m, "服务端心跳超时常量改名了？");
  return Number(m[1].replace(/_/g, ""));
}
function clientRequestTimeoutMs() {
  const m = /REQUEST_TIMEOUT_MS\s*=\s*(\d+)/.exec(JS);
  assert.ok(m, "JS 请求超时常量改名了？");
  return Number(m[1]);
}

test("前提仍然成立：客户端单次请求超时比服务端心跳容忍度还短", () => {
  // 这是整组修复的立论。哪天两边调成反过来了，"连续 N 次"就不再必要，
  // 这条会提醒重新评估，而不是让修复变成无意义的复杂度。
  const server = serverHeartbeatTimeoutMs();
  const client = clientRequestTimeoutMs();
  assert.ok(
    client < server,
    `客户端 ${client}ms 已经不比服务端 ${server}ms 严了 —— 重新评估这组修复`);
});

test("① iOS .inactive 不再立刻拆链，要走宽限期", () => {
  // iOS 在用户**根本没离开 App** 时也会给 .inactive：下拉通知中心、
  // 进 App 切换器、系统弹窗。原来它和 .background 走同一条路、零缓冲。
  const i = SWIFT_WEBVIEW.indexOf("func setReaderScenePhase(");
  assert.ok(i >= 0, "setReaderScenePhase 改名了？");
  const body = SWIFT_WEBVIEW.slice(i, SWIFT_WEBVIEW.indexOf("\n    }", i));
  assert.match(body, /case \.inactive:\s*\n[^\n]*\n\s*scheduleReaderInactiveGrace\(\)/,
    ".inactive 又变成立刻断链了");
  // .background 必须仍然立刻断（iOS 会挂起进程，留着没用）
  assert.match(body, /case \.background:[\s\S]*?setReaderForeground\(false/);
  // 回到 .active 要取消宽限期，否则定时器照样会把连接掐掉
  assert.match(body, /case \.active:\s*\n\s*cancelReaderInactiveGrace\(\)/,
    "回前台没取消宽限定时器 —— 会在用户正用着的时候把连接掐掉");
  assert.match(SWIFT_WEBVIEW, /readerInactiveGrace: TimeInterval = \d+/);
});

test("② 心跳连续失败才判死 —— Swift 侧", () => {
  assert.match(SWIFT_SOCKET, /heartbeatFailuresBeforeGivingUp = \d+/);
  const i = SWIFT_SOCKET.indexOf("private func startHeartbeat()");
  const body = SWIFT_SOCKET.slice(i, SWIFT_SOCKET.indexOf("\n    private func", i + 10));
  assert.match(body, /var consecutiveFailures = 0/);
  // ⚠ 必须钉住"**成功之后**清零"这个位置关系，不能只查 `consecutiveFailures = 0`
  //   —— 声明那一行也长这样，于是把清零删掉测试照样绿（变异验证抓到的）。
  //   不清零的后果：失败次数跨心跳累积，几次零星抖动之后连接被误杀，
  //   而且是**间歇性**的，最难查。
  assert.match(
    body,
    /try await self\?\.sendHeartbeat\(\)\s*\n\s*consecutiveFailures = 0/,
    "成功后没紧接着清零 —— 失败会跨心跳累积到误杀");
  assert.match(body, /if consecutiveFailures\s*\n?\s*< Self\.heartbeatFailuresBeforeGivingUp/);
  assert.match(body, /noteHeartbeatRetry\(/, "被容忍的失败没留痕");
  assert.match(body, /continue/, "没到判死线时要继续循环，不是 return");
});

test("② 心跳连续失败才判死 —— JS 侧（网页/扩展）", () => {
  assert.match(JS, /HEARTBEAT_FAILURES_BEFORE_GIVING_UP = \d+/);
  const i = JS.indexOf("function scheduleHeartbeat(");
  const body = JS.slice(i, JS.indexOf("\n  function ", i + 10));
  assert.match(body, /state\.heartbeatFailures = \(state\.heartbeatFailures \|\| 0\) \+ 1;/);
  assert.match(body, /state\.heartbeatFailures = 0;/, "成功后没清零");
  assert.match(body, /if \(state\.heartbeatFailures < HEARTBEAT_FAILURES_BEFORE_GIVING_UP\)/);
  assert.match(body, /console\.warn\(\s*\n?\s*"\[direct\] 心跳超时，重试中"/, "被容忍的失败没留痕");
  // 新连接必须从 0 开始，否则上一条的计数会带进来 → "刚连上就被判死"
  assert.match(JS, /heartbeatSequence: 0,[\s\S]{0,300}?heartbeatFailures: 0,/);
});

test("③ 瞬时重试有独立事件档，且**不能**走 .error", () => {
  // .error 在上层会 handleSessionFailure 把会话收掉 ——
  // 用它上报"已容忍的失败"等于把容忍又变回照样断。
  assert.match(SWIFT_PROTO, /case transientRetry\(DirectVoiceFailure, attempt: Int\)/);
  const i = SWIFT_BRIDGE.indexOf("case .transientRetry(");
  assert.ok(i >= 0, "上层没消费这个事件 —— Swift 的 switch 会不穷尽，编译不过");
  const branch = SWIFT_BRIDGE.slice(i, SWIFT_BRIDGE.indexOf("case .error(", i));
  assert.match(branch, /recordDiagnostic\(/, "没留痕，等于白加这一档");
  // ⚠ 只看**可执行行**：这个分支的注释里本来就写着"绝不能走
  //   handleSessionFailure"，直接对整段 doesNotMatch 会被自己的注释命中。
  //   第一次就是这么误报的 —— 代码是对的，断言写错了。
  const executable = branch
    .split("\n")
    .filter((line) => !/^\s*(\/\/|\*|\/\*)/.test(line))
    .join("\n");
  assert.doesNotMatch(executable, /handleSessionFailure/,
    "瞬时重试走了收会话的路 —— 容忍变成了照样断");
});
