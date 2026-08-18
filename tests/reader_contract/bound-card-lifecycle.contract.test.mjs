import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const VOICECALL = read("_server_deploy/static/pdf/rc-voicecall.js");
const UISHARED = read("_server_deploy/static/pdf/pdf-uishared.js");
const CONTRACT = read("_server_deploy/reader_card_contract.py");

// 用户设计（2026-08-18）：「（绑定卡）落下时按普通浮动卡的非活跃计时且自动保持
// 打开，计时结束不消失而是收起为圆球」。
//
// 卡片的位置本身就是信息 —— 关掉等于把「这一处有过一次纠正」也一起丢了，
// 而球留在原位还认得出来。

test("绑定卡到点收起成球，不会消失", () => {
  // ① 契约里有 bind 字段（四处同步的那一处）
  assert.match(CONTRACT, /_CARD_TOP_OPT = \([^)]*"bind"/);

  // ② _armAuto 里有 keepAsDot 分支：到点变 dot 而不是 _cardClose
  assert.match(
    VOICECALL,
    /if \(c\.keepAsDot\) \{[^}]*_cardForm\(el, 'dot'\)/,
    "_armAuto 缺少 keepAsDot 分支",
  );

  // ③ 关键：keepAsDot 必须真的被**赋值**。
  //    2026-08-19 之前这个分支写好了却没有任何写入点，于是绑定卡照样消失 ——
  //    "机制在但没接线"是最难发现的一类缺陷：代码读起来完全正确。
  assert.match(
    VOICECALL,
    /keepAsDot: !!opts\.keepAsDot/,
    "_cardPush 没把 opts.keepAsDot 写进卡对象",
  );
  assert.match(
    VOICECALL,
    /keepAsDot: !!card\.bind/,
    "带 bind 的卡退回浮层时没有请求 keepAsDot",
  );
});

test("侧栏开着时绑定卡照常计时（它的归宿是变成球，不是消失）", () => {
  // 普通浮层卡在侧栏开着时不排计时（看不见就不该计时，关侧栏时补排）；
  // 绑定卡不同 —— 它到点是收成球留在原地，跟可见性无关。
  assert.match(VOICECALL, /if \(_sideOpen\(\) && !c\.keepAsDot\) return;/);
});

test("绑进自建页的卡有自己的展开↔球与计时，且形态跨会话保留", () => {
  // 这一条路（bind.kind === 'upage-block'）不走浮层：卡片成为自建页 blocks
  // 数组里的一个 block，屏幕位置和内容序列位置是同一件事。
  assert.match(UISHARED, /function _upBindCardForm\(/);
  assert.match(UISHARED, /if \(b\.boundTo\) _upBindCardForm\(/);
  const body = UISHARED.slice(
    UISHARED.indexOf("function _upBindCardForm("),
    UISHARED.indexOf("function _upRenderBlocks("),
  );
  assert.ok(body.length > 0);
  // 计时到点收球
  assert.match(body, /setTimeout\(function \(\) \{ collapsed = true;/);
  // 形态落盘 —— 不落盘的话刷新一次球又变回展开，"这一处看过了"的信息就没了
  assert.match(body, /blk\.form = collapsed \? 'dot' : 'full'/);
  assert.match(body, /RC\.reqJson\('PATCH', UP_TEXT_API/);
  // 点球展开回来
  assert.match(body, /collapsed = false; paint\(\); save\(\); arm\(\);/);
});

test("绑不上时退回浮层而不是丢卡", () => {
  // 位置信息没了还能补，内容没了就真没了。
  assert.match(VOICECALL, /if \(okBind\) return true;/);
  assert.match(VOICECALL, /没找到要绑定的位置，卡片先放浮层/);
});
