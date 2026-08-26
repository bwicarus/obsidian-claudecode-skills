// 系统投影同步循环（2026-08-26）：App 内每 30 分钟把用户向通知与复习
// 摘要交给原生侧（提醒事项/本地通知/系统闹钟/小组件数据）。
// 这些断言锁的都是**踩过的坑**，不是风格。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const VOICE = readFileSync(
  join(root, "_server_deploy/static/pdf/rc-computer-voice.js"), "utf8");
const RUNTIME = readFileSync(
  join(root, "_server_deploy/static/pdf/native-local-runtime.js"), "utf8");

function syncBlock() {
  const at = VOICE.indexOf("function wireSystemProjection");
  assert.ok(at >= 0, "系统投影同步循环在场");
  return VOICE.slice(at, at + 4200);
}

test("首次成功前退避重试 —— 一次失败不该干等 30 分钟", () => {
  const body = syncBlock();
  assert.match(body, /RETRY_LADDER_MS = \[8000, 30000, 60000, 120000\]/,
    "真机实锤:原来首次只试一次,那一刻桥不可达就要等一个巡航周期");
  assert.match(body, /succeededOnce/,
    "成功前后用不同节奏:成功后转 30 分钟巡航,不能一直密集重试");
});

test("非 App 环境彻底安静,不空转", () => {
  const body = syncBlock();
  assert.match(body, /if \(result === null\)/,
    "runtime 在但没有原生 handler = 扩展/桌面表面,系统投影只属于 App");
});

test("闹钟不可用却有到点提醒时,必须让用户知道", () => {
  const body = syncBlock();
  assert.match(body, /alarms=\(unsupported-os\|sdk-unavailable\|denied\)/,
    "三种闹钟缺席状态都要认出来");
  assert.match(body, /dueAtUtcMs/,
    "只在**确有到点提醒**时才提示 —— 没有提醒时提示是纯噪音");
  assert.match(body, /alarmNoticeShown/,
    "只提示一次:每 30 分钟提示一次会把用户逼疯");
});

test("用户处理通知后要触发重同步,及时撤销已排的到点提醒", () => {
  assert.match(VOICE, /__BW_SYSTEM_PROJECTION_TRIGGER__/,
    "通知 tab 与投影循环之间要有这条线");
  const at = VOICE.indexOf("window.__BW_SYSTEM_PROJECTION_TRIGGER__ = ");
  assert.ok(at >= 0);
  assert.match(VOICE.slice(at, at + 200), /setTimeout\(sync, 75000\)/,
    "要等一轮 Windows 对账周期再同步 —— 立刻同步会读到还没应用的旧状态");
});

test("原生入口在 App 外返回 null 而不是抛错", () => {
  const at = RUNTIME.indexOf("root.__bwSystemProjectionSync");
  assert.ok(at >= 0, "runtime 暴露了系统投影入口");
  const body = RUNTIME.slice(at, at + 600);
  assert.match(body, /if \(!nativePageTextHandler\(\)\) return Promise\.resolve\(null\)/,
    "App 外没有 message handler:返回 null 让调用方识别环境,不是失败");
  assert.match(RUNTIME, /'system-projection': new Set\(\['resolvedIds'\]\)/,
    "响应字段白名单必须显式登记 —— 这份白名单在链路上有多份副本");
});
