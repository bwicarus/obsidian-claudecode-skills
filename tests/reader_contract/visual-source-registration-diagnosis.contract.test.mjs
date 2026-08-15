// 「AI 看不到这个页面」必须说得出为什么。
//
// 现场:助手在普通网页上取图,恒答「网页的图片来源离线」,Windows 侧 1ms 就
// 拒绝(BW_READER_VISUAL_SOURCE_OFFLINE)。离线是**结果** —— 桥上没有这个页面的
// 租约。原因在扩展侧:租约只由 visual-register 建立,而通往它的路上有五处提前
// 退出,过去全都无声。
//
// 于是形成了最难查的那种局面:快照照常走 POST(那条路不看这些门),助手因此
// 知道用户在看什么、甚至知道刚画过东西,却一取图就说离线,而没有任何地方
// 说得出哪一步没走成。iPad 上没有控制台,不出声就等于永远查不出来。
//
// 这个文件钉住的不是「注册会成功」,而是「不成功时说得出是哪一项」。

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const CALL = readFileSync(
  join(ROOT, "extensions/bw-reader-webext/call.js"), "utf8");
const CTX = readFileSync(
  join(ROOT, "extensions/bw-reader-webext/ctxlink.js"), "utf8");

function ensureVisualLinkBody() {
  const start = CALL.indexOf("function ensureVisualLink(");
  assert.ok(start > 0, "找不到 ensureVisualLink");
  const end = CALL.indexOf("\nfunction ", start + 10);
  return CALL.slice(start, end > start ? end : start + 3000);
}

function registerBody() {
  // 找定义而不是调用点:`#registerVisualSource()` 在 connect 链里也出现,
  // 按裸名字取会切到别处去,断言就变成在考别的代码。
  const start = CTX.indexOf("  #registerVisualSource() {");
  assert.ok(start > 0, "找不到 #registerVisualSource 的定义");
  return CTX.slice(start, start + 2200);
}

test("每个提前退出都出声", () => {
  const body = ensureVisualLinkBody();
  // 三个门:开关关、框不可见、没有来源标识。过去它们共用一个 return。
  assert.match(body, /note\("视觉来源未注册: 上下文同步开关为关"\)/);
  assert.match(body, /note\("视觉来源未注册: 通话框不可见/);
  assert.match(body, /本页尚未认领通话框/);
});

test("空标识与格式错分开说", () => {
  // 两者都通不过正则,但一个要去查认领链路、一个要去查正则。
  // 合成一句「标识无效」会把人引向错的那边。
  const body = ensureVisualLinkBody();
  assert.match(body, /source\s*\?[\s\S]{0,80}格式不合法/);
  assert.match(body, /background 未绑定 sourceInstanceId/);
});

test("不可见时报出实际的 visibilityState", () => {
  // 折成布尔前先报原始值:否则只知道"不可见",不知道是 hidden 还是
  // 别的什么让判断成立。
  assert.match(ensureVisualLinkBody(), /document\.visibilityState/);
});

test("五项跳过条件逐项归因,不折成一个布尔", () => {
  const body = registerBody();
  for (const reason of [
    "无来源标识", "无会话", "socket 尚未创建", "socket 未就绪",
  ]) {
    assert.ok(body.includes(reason), `缺归因: ${reason}`);
  }
  assert.match(body, /readyState=/, "未就绪要带上实际 readyState");
});

test("已注册不算异常,不报警", () => {
  const body = registerBody();
  assert.match(body, /already-registered/,
    "重复注册是正常路径,混进告警会让真正的故障淹没在噪声里");
});

test("注册被桥拒绝与本地跳过分开报", () => {
  const body = registerBody();
  assert.match(body, /visual-register-skipped/);
  assert.match(body, /visual-register-failed/);
  assert.match(body, /throw error/,
    "报完要继续抛,诊断不能顺手把错误吞掉");
});

test("新状态真的到达了人,而不是发进空处", () => {
  // 状态回调过去只认 error/retrying。新增状态若不在这里接住,
  // 上面所有归因都发进空处 —— 那正是这次要修的模式本身。
  const start = CALL.indexOf("function visualLinkStatus(");
  const body = CALL.slice(start, start + 1400);
  assert.match(body, /visual-register-skipped/);
  assert.match(body, /visual-register-failed/);
  assert.match(body, /note\(/,
    "失败要用 note:iPad 上没有控制台,frameProbe 到不了人");
});

test("绑定失败不再被空 catch 吞掉", () => {
  const body = ensureVisualLinkBody();
  assert.doesNotMatch(body, /bindVisualSource\(source\)\.catch\(\(\) => \{\}\)/,
    "空 catch 会让「连 id 都没记下」这种最彻底的失败完全无痕");
  assert.match(body, /视觉来源绑定失败/);
});
