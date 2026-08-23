import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// 用户 2026-08-23：「作为一个生成物，ai 那里并没有读取，修改，删除的手段」。
// 调查确认三分之二成立：创建链相当完整，读取残缺，**改和删完全没有** ——
// 于是 AI 造纸是不可逆写操作。这份钉住补上的那三样。
const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8");

const ASSIST = read("_server_deploy/assistant.py");
const RCASSIST = read("_server_deploy/static/pdf/rc-assistant.js");

test("三个工具都注册进 TOOLS —— 只写函数不注册等于没有", () => {
  for (const name of ["userpage_list", "userpage_edit", "userpage_delete"]) {
    assert.match(
      ASSIST, new RegExp(`"${name}": \\(`),
      `${name} 必须注册进 TOOLS，否则模型根本看不见它`,
    );
    assert.match(
      ASSIST, new RegExp(`def _t_${name}\\(`),
      `${name} 的实现必须存在`,
    );
  }
});

test("⚠ 读取补上了用户手打的 md —— 此前一个字都不返回", () => {
  const fn = ASSIST.slice(
    ASSIST.indexOf("def _upage_read_text"),
    ASSIST.indexOf("def _t_read_page"),
  );
  assert.match(
    fn, /_md = \(it\.get\("md"\) or ""\)\.strip\(\)/,
    "必须读 md —— 只认 blocks 的话，用户写完笔记问 AI，AI 只看得到标题",
  );
  assert.match(fn, /out\.append\(_md/, "读了还要真的放进输出");
});

test("⚠ 自建页命中后不再 continue —— 否则钉在这页上的卡片/便签全被绕过", () => {
  const loop = ASSIST.slice(
    ASSIST.indexOf("up = _upage_read_text(file_rel, pg, ctx)"),
    ASSIST.indexOf("up = _upage_read_text(file_rel, pg, ctx)") + 700,
  );
  assert.doesNotMatch(
    loop, /up_hit = True; continue/,
    "continue 会把 _read_one（负责补便签/卡片上下文）整条绕过，且不报错",
  );
  assert.match(loop, /b = _read_one\(file_rel, ctx, pg, figd\)/);
});

test("⚠ 不给 id 时不猜要改哪张", () => {
  const fn = ASSIST.slice(
    ASSIST.indexOf("def _upage_write_target"),
    ASSIST.indexOf("def _t_userpage_edit"),
  );
  assert.match(
    fn, /要改哪一张/,
    "必须明确要 id —— 改错一张纸没有撤销余地，宁可让调用方说清",
  );
  assert.match(fn, /"known":/, "拒绝时要给出候选，否则模型无从修正");
});

test("写操作交给 App，不在 Pi 直接落盘", () => {
  // /pdf/api/userpages 是 owner=local + runtime 有本地分支 → App 内本地执行。
  // 在 Pi 直接写 sidecar 对 App 无效，而且不会报错。
  for (const fn of ["_t_userpage_edit", "_t_userpage_delete"]) {
    const body = ASSIST.slice(ASSIST.indexOf(`def ${fn}(`), ASSIST.indexOf(`def ${fn}(`) + 1400);
    assert.match(body, /"client_action"/, `${fn} 必须经 client_action 交给 App`);
    assert.match(body, /"fn": "_assistEdit"/, "复用既有入口，不新增 client-action fn");
    assert.match(body, /"type": "userpage"/, "用 type 判别，不碰那份 4 副本白名单");
  }
});

test("⚠ 删除必须可撤销 —— 动作里要带着重建所需的全部内容", () => {
  const body = ASSIST.slice(
    ASSIST.indexOf("def _t_userpage_delete("),
    ASSIST.indexOf("def _t_userpage_delete(") + 1400,
  );
  assert.match(
    body, /"before": \{k: hit\.get\(k\) for k in \("after", "title", "md", "blocks"\)/,
    "删之前必须把整条留在动作里，否则用户反悔时内容就真没了",
  );
});

test("前端执行器存在、接在分发上，且失败要出声", () => {
  assert.match(RCASSIST, /function _assistUserPage\(d\)/, "执行器必须存在");
  assert.match(
    RCASSIST, /if \(d && d\.type === 'userpage'\) return _assistUserPage\(d\);/,
    "必须接进 _assistEdit 的分发，否则动作发过去没人执行",
  );
  const fn = RCASSIST.slice(
    RCASSIST.indexOf("function _assistUserPage(d)"),
    RCASSIST.indexOf("window._assistEdit = function (d)"),
  );
  assert.match(fn, /撤销失败/, "撤销失败要说出来 —— 静默会让用户以为撤销成功了");
  assert.match(fn, /自建页操作失败/, "写入失败也要说出来");
  assert.match(
    fn, /__upRerender/,
    "改完要重渲那一页，否则用户看到的还是旧内容",
  );
});

test("⚠ 位置标签只用于显示，绝不进整数页码通路", () => {
  const fn = ASSIST.slice(
    ASSIST.indexOf("def _upage_label"),
    ASSIST.indexOf("def _upages_labeled"),
  );
  assert.match(
    fn, /绝不进 bind\.page/,
    "必须写明这条边界：parseInt('46-a') === 46 会把它静默当成真实的第 46 页",
  );
  // 反向：实现里不得把 label 塞进 page 字段
  const tools = ASSIST.slice(
    ASSIST.indexOf("def _t_userpage_list"),
    ASSIST.indexOf("TOOLS = {"),
  );
  assert.doesNotMatch(
    tools, /"page": _upage_label/,
    "label 绝不能出现在 page 字段上",
  );
});
