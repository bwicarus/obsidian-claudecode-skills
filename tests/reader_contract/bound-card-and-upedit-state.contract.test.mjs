// 两个用户实锤的状态残留(2026-08-25):
//
// 1. 便签卡片绑定到元素后仍记录/回放自己的折叠形态,点击元素打开的是圆圈
//    而不是内容。合同:绑定卡**只有展开模式** —— 渲染时强制 full,持久化
//    时也不允许把折叠形态写回绑定卡。
// 2. 乐观新建用户页失败拆页后,编辑态(_upEditing 句柄 + body.up-editing)
//    残留,之后每次点 ➕ 都弹「先完成当前正在编辑的页」,直到重开书。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const STICKY = readFileSync(
  join(root, "_server_deploy/static/pdf/rc-stickynote.js"), "utf8");
const UISHARED = readFileSync(
  join(root, "_server_deploy/static/pdf/pdf-uishared.js"), "utf8");

// ⚠ 第一版修法（渲染层永远锁 full）被用户实锤打回：它把**所有**常驻显示
// 绑定卡的收起状态（圆点"锁定"）一并打散了。正确语义是分场景的：
// 渲染层尊重 form 记忆（锁定保留），只有"点标记打开"这个动作直达展开。
test("渲染层尊重 form 记忆 —— 不许按 bind 无差别强制 full", () => {
  assert.ok(!/form: card\.bind \? 'full'/.test(STICKY),
    "card 渲染点不强制（第一版的误伤形态）");
  assert.ok(!/form: h\.bind \? 'full'/.test(STICKY),
    "html 卡渲染点不强制");
  assert.ok(!/card\.form = card\.bind \? 'full'/.test(STICKY),
    "onForm 持久化不锁（用户收起的选择要被记住）");
  assert.match(STICKY, /form: card\.form/, "card 渲染仍传记忆形态");
  assert.match(STICKY, /form: h\.form/, "html 卡渲染仍传记忆形态");
});

test("点标记打开直达完全展开 —— 只动这一个入口", () => {
  const at = STICKY.indexOf("function forceOpenCardFull(ctl)");
  assert.ok(at >= 0, "展开函数存在");
  const body = STICKY.slice(at, at + 700);
  assert.match(body, /classList\.remove\('vc-dot'\)/);
  assert.match(body, /classList\.remove\('vc-min'\)/);
  assert.match(body, /__bwCardFormApply/,
    "经 onForm 同一条持久化/壳宽路径，不另造旁路");
  const calls = STICKY.match(/forceOpenCardFull\(ctl\);/g) || [];
  assert.equal(calls.length, 1, "只有 onToggle 打开分支一个调用点");
  const openAt = STICKY.indexOf("ctl._bindOpen = true;");
  const callAt = STICKY.indexOf("forceOpenCardFull(ctl);");
  assert.ok(openAt >= 0 && callAt > openAt && callAt - openAt < 800,
    "调用点在打开分支内");
});

test("乐观新建失败必须清干净编辑态,句柄和 class 一个都不能剩", () => {
  const at = UISHARED.indexOf("function _upTempFail(");
  assert.ok(at >= 0);
  const body = UISHARED.slice(at, UISHARED.indexOf("\n  }", at));
  assert.match(body, /_upEditing && el && el\.contains\(_upEditing\.el\)/,
    "面板挂在被拆临时页里时走完整关闭");
  assert.match(body, /_upCloseInline\(\);/,
    "完整关闭连 _upEditing 句柄一起清 —— 入口检查是句柄 || class 双条件");
  assert.match(body, /classList\.remove\('up-editing'\)/,
    "面板不在临时页里也至少把 class 摘掉");
});

test("入口检查确实是双条件 —— 这决定了失败路径必须两个都清", () => {
  assert.match(UISHARED,
    /_upEditing \|\| document\.body\.classList\.contains\('up-editing'\)/);
});
