// 写回前等 source 完成注册。
//
// 现场症状:快照 ready、health 说 contextConnected=true,写便签却立刻报
// BW_READER_REALTIME_OUTPUT_SOURCE_OFFLINE。原因是 WSS 连上(DirectBridgeServer)
// 与「这个 source 能收东西」(DirectBridgeProtocol 的 visual-register/Attach)
// 不是同一时刻,中间有空窗。
//
// 这里要钉住的是**等待与重试的区别**,因为两者看起来很像而后果相反:
//   · 进 SendAsync 时没租约 —— 什么都还没发出去,等的是"能不能发",安全。
//   · SendAsync 抛错 / 租约中途退休 —— 已经发出去了,结果未知。再来一次
//     就是第二条便签。这两处绝不能也加等待。
//
// 注:本机只有 .NET runtime 无 SDK,这是文本校验,不能替代编译。

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const OUTPUT = readFileSync(
  join(ROOT, "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderRealtimeOutput.cs"),
  "utf8",
);

function sendBody() {
  const start = OUTPUT.indexOf("internal async Task<ReaderRealtimeOutputAck> SendAsync(");
  assert.ok(start > 0, "找不到 SendAsync");
  const end = OUTPUT.indexOf("\n    private", start);
  return OUTPUT.slice(start, end > start ? end : start + 6000);
}

test("进入时没租约就等一小会儿,而不是立刻失败", () => {
  const body = sendBody();
  assert.match(body, /await WaitForSourceAsync\(/,
    "开头的 TryGetLease 必须换成有界等待");
  assert.doesNotMatch(
    body.slice(0, body.indexOf("PendingOutput pending")),
    /_router\.TryGetLease\(/,
    "等待函数之外不该再直接查一次租约",
  );
});

test("等待有上限,且上限短", () => {
  const match = /SourceRegistrationWait =\s*TimeSpan\.FromMilliseconds\((\d[\d_]*)\)/
    .exec(OUTPUT);
  assert.notEqual(match, null, "找不到等待上限");
  const ms = Number(match[1].replace(/_/g, ""));
  assert.ok(ms > 0, "必须真的等一下");
  assert.ok(
    ms <= 5000,
    `等待上限 ${ms}ms 太长 —— 用户在阅读器里是站着等的,`
      + "为一次写便签等这么久比直接失败更难受",
  );
});

test("等不到时说明等过,不与'立刻失败'混为一谈", () => {
  const body = sendBody();
  assert.match(body, /已等待/,
    "错误里不说等过多久,就无法判断是不是等得不够");
  assert.match(body, /retryable: true/);
});

test("已经发出去之后的离线不加等待——那等于重发", () => {
  const body = sendBody();
  // 发送抛错这一处
  const afterSend = body.slice(body.indexOf("catch (ReaderVisualDeliveryException"));
  assert.doesNotMatch(afterSend, /WaitForSourceAsync/,
    "发送已经发生,结果未知;再等再发会写出第二条");
  // 租约中途退休这一处
  const retired = body.slice(body.indexOf("winner == lease.LeaseRetired"));
  assert.doesNotMatch(retired, /WaitForSourceAsync/,
    "输出期间掉线同样是结果未知");
});

test("等待只查状态,不碰 mutation", () => {
  const start = OUTPUT.indexOf("private async Task<ReaderContextSourceLease?> WaitForSourceAsync(");
  assert.ok(start > 0, "找不到 WaitForSourceAsync");
  const body = OUTPUT.slice(start, start + 1200);
  assert.match(body, /_router\.TryGetLease\(/, "只查注册状态");
  for (const forbidden of ["SendAsync", "_pending", "PendingOutput"]) {
    assert.ok(!body.includes(forbidden),
      `等待期间不得触碰 ${forbidden} —— 它只该回答"能不能发"`);
  }
});

test("等待会响应取消", () => {
  const start = OUTPUT.indexOf("private async Task<ReaderContextSourceLease?> WaitForSourceAsync(");
  const body = OUTPUT.slice(start, start + 1200);
  assert.match(body, /Task\.Delay\(\s*SourceRegistrationPoll,\s*cancellationToken\)/,
    "不接受取消的等待会把挂断的用户继续晾着");
});
