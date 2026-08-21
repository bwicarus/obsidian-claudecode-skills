// 取图前等快照指定的 source 完成 visual-register。
//
// 页面刷新、前后台恢复和 Direct 服务重连都会重新建立 WSS。快照恢复 ready
// 与 visual-register/Attach 完成不是同一时刻；如果在中间几十毫秒调用取图，
// 单次 TryGetLease 会把一个正在恢复的来源误报成永久离线。
//
// 等待只允许发生在请求发出前。取图虽是只读操作，但来源替换后重新发送会让
// 旧快照身份与新页面混在一起，仍应 fail closed。

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const VISUAL = readFileSync(
  join(ROOT, "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderVisualDelivery.cs"),
  "utf8",
);

function requestBody() {
  const start = VISUAL.indexOf("internal async Task<ReaderVisualCapture?> RequestAsync(");
  assert.ok(start > 0, "找不到 RequestAsync");
  const end = VISUAL.indexOf("\n    internal ReaderVisualDeliveryAck Accept(", start);
  assert.ok(end > start, "找不到 RequestAsync 结尾");
  return VISUAL.slice(start, end);
}

function waitBody() {
  const start = VISUAL.indexOf(
    "private async Task<ReaderContextSourceLease?> WaitForSourceAsync(",
  );
  assert.ok(start > 0, "找不到 WaitForSourceAsync");
  const end = VISUAL.indexOf("\n    private static void RequireIdentity(", start);
  assert.ok(end > start, "找不到 WaitForSourceAsync 结尾");
  return VISUAL.slice(start, end);
}

test("取图只在发送前有界等待来源注册", () => {
  const body = requestBody();
  const waitAt = body.indexOf("await WaitForSourceAsync(");
  const pendingAt = body.indexOf("PendingDelivery pending");
  const sendAt = body.indexOf("await _router.SendAsync(");

  assert.ok(waitAt >= 0, "取图入口仍会单次查询后立即报离线");
  assert.ok(pendingAt > waitAt, "等待必须发生在登记 pending 之前");
  assert.ok(sendAt > pendingAt, "等待必须发生在真正发送之前");
  assert.doesNotMatch(
    body.slice(0, pendingAt),
    /_router\.TryGetLease\(/,
    "入口不能绕过等待函数再单次查租约",
  );
});

test("等待覆盖短暂重连但不会把离线工具挂住", () => {
  const match = /SourceRegistrationWait =\s*TimeSpan\.FromMilliseconds\((\d[\d_]*)\)/
    .exec(VISUAL);
  assert.notEqual(match, null, "找不到等待上限");
  const milliseconds = Number(match[1].replace(/_/g, ""));
  assert.ok(milliseconds >= 1000, "等待应覆盖重连退避的第一档");
  assert.ok(milliseconds <= 5000, "视觉工具不应为离线页面长时间阻塞");

  const body = requestBody();
  assert.match(body, /已等待/,
    "离线回执必须区分立即失败与等待后仍离线");
  assert.match(body, /DescribeRegisteredSources\(\)/,
    "等待超时后仍须保留零来源/来源不匹配诊断");
});

test("等待循环只观察注册状态并响应取消", () => {
  const body = waitBody();
  assert.match(body, /_router\.TryGetLease\(/);
  assert.match(
    body,
    /Task\.Delay\(\s*SourceRegistrationPoll,\s*cancellationToken\)/,
    "用户取消后不得继续等待",
  );
  for (const forbidden of ["SendAsync", "_pending", "PendingDelivery"]) {
    assert.ok(
      !body.includes(forbidden),
      `注册等待不得触碰 ${forbidden}`,
    );
  }
});

test("请求发出或旧租约退休后不等待新来源、不重发", () => {
  const body = requestBody();
  const sent = body.slice(body.indexOf("await _router.SendAsync("));
  assert.doesNotMatch(sent, /WaitForSourceAsync/,
    "请求可能已被旧页面接收，不能换新租约重发");

  const retired = body.slice(body.indexOf("winner == lease.LeaseRetired"));
  assert.doesNotMatch(retired, /WaitForSourceAsync/);
  assert.match(retired, /取图期间指定 Reader 页面来源已离线/);
});
