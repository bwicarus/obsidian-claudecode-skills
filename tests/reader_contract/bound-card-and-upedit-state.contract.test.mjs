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

test("绑定卡渲染永远展开:两处 renderInto 都按 bind 强制 full", () => {
  const sites = STICKY.match(/form: card\.bind \? 'full' : card\.form/g) || [];
  assert.equal(sites.length, 1, "card 渲染点强制 full");
  const htmlSites = STICKY.match(/form: h\.bind \? 'full' : h\.form/g) || [];
  assert.equal(htmlSites.length, 1, "html 卡渲染点强制 full");
});

test("绑定卡不持久化折叠形态:两处 onForm 回写也按 bind 锁 full", () => {
  assert.match(STICKY, /card\.form = card\.bind \? 'full' : f/,
    "card onForm 持久化被锁");
  assert.match(STICKY, /h\.form = h\.bind \? 'full' : f/,
    "html 卡 onForm 持久化被锁");
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
