import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const VOICECALL = read("_server_deploy/static/pdf/rc-voicecall.js");
const UISHARED = read("_server_deploy/static/pdf/pdf-uishared.js");
const CONTRACT = read("_server_deploy/reader_card_contract.py");

/// 取一个函数的函数体：从 `function 名(` 到下一个同缩进的 `function`。
/// 别用 indexOf(A)→indexOf(B) 切片 —— B 定义在 A 之前时会静默返回空串，
/// 测试就变成"对空字符串做断言"，红得莫名其妙（或者更糟：绿得莫名其妙）。
const bodyOf = (source, name) => {
  const start = source.indexOf(`function ${name}(`);
  if (start < 0) return "";
  const next = source.slice(start + 1).search(/\n {2}function /);
  return next < 0 ? source.slice(start) : source.slice(start, start + 1 + next);
};

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
  const body = bodyOf(UISHARED, "_upBindCardForm");
  assert.ok(body.length > 0, "找不到 _upBindCardForm");
  // 计时到点收球
  assert.match(body, /setTimeout\(function \(\) \{ collapsed = true;/);
  // 形态落盘 —— 不落盘的话刷新一次球又变回展开，"这一处看过了"的信息就没了。
  // 落盘按 rec 合并：同页多张绑定卡会**同时**到点，各发一个整数组 PATCH 会互相覆盖。
  assert.match(body, /blk\.form = collapsed \? 'dot' : 'full'/);
  assert.match(body, /_upBindSaveSoon\(rec\)/);
  assert.match(UISHARED, /function _upBindSaveSoon\(rec\)[\s\S]{0,400}?RC\.reqJson\('PATCH', UP_TEXT_API/);
  // 点球展开回来
  assert.match(body, /collapsed = false; paint\(\); save\(\); arm\(\);/);
});

test("绑定卡看不见时不计时", () => {
  // 自建页的元素挂进滚动容器后**不虚拟化**，所以裸 setTimeout 会让"绑在第 12 页
  // 的卡"在你读第 3 页时自己收成球 —— 全程没被看见过。浮层那层早有这个修复
  // （_cardsVisSync：卡片被藏起来的这段时间不该计时），块这层一直没有对等物。
  const body = bodyOf(UISHARED, "_upBindCardForm");
  assert.ok(body.length > 0, "找不到 _upBindCardForm");
  assert.match(body, /new IntersectionObserver\(/);
  assert.match(body, /if \(!visible\) return;/, "arm() 没有检查可见性");
  assert.match(body, /document\.visibilityState === 'hidden'/, "切后台没有停表");
  assert.match(body, /visibilitychange/);
});

test("重渲染会拆掉上一轮的计时器，不留僵尸", () => {
  // _upRenderBlocks 每次重建 innerHTML 并重跑 _upBindCardForm。旧闭包的 timer
  // 从来没人 clear，它仍会在已脱离的元素上触发，并改**同一个** blk 对象写
  // form:'dot' —— 表现为"刚展开的卡自己又收起来了"。
  assert.match(UISHARED, /el\.__bwBindTeardown = function \(\)/);
  assert.match(
    UISHARED,
    /if \(el\.__bwBindTeardown\) el\.__bwBindTeardown\(\);/,
    "_upBindCardForm 开头没有先拆旧实例",
  );
  // 唯一销毁块 DOM 的地方 = 唯一正确的拆卸点
  const render = UISHARED.slice(UISHARED.indexOf("function _upRenderBlocks("));
  const teardown = render.indexOf("__bwBindTeardown()");
  const wipe = render.indexOf("ov.innerHTML =");
  assert.ok(teardown >= 0, "_upRenderBlocks 没有拆卸旧卡");
  assert.ok(teardown < wipe, "拆卸排在 innerHTML 清空之后就晚了 —— 那时元素已经没了");
});

test("绑定卡的计时读用户设置，跟浮层卡同一口径", () => {
  // 写死 20s 的话，用户把自动收起关掉、或改成 60s，绑定卡完全不理。
  assert.match(UISHARED, /function _upBindIdleMs\(\)/);
  assert.match(UISHARED, /localStorage\.getItem\('rc-voice-card-hide'\) === '0'/);
  assert.match(UISHARED, /localStorage\.getItem\('rc-voice-card-secs'\)/);
  // 关掉自动收起 = 永不排表
  assert.match(UISHARED, /if \(ms === null\) return;/);
  // 与浮层卡的口径必须一致（rc-voicecall 那边的钳位也是 5..60）
  assert.match(VOICECALL, /Math\.max\(5, Math\.min\(60, v\)\)/);
  assert.match(UISHARED, /Math\.max\(5, Math\.min\(60, v\)\)/);
});

test("绑不上的卡记住想去哪，那页出现时自己归位", () => {
  // 最常见的失败是"那页还没渲染"，而这种失败**会自己好** —— 只要那页出现。
  assert.match(VOICECALL, /window\.__upBindRetry = function \(upageId\)/);
  assert.match(VOICECALL, /_bindPending\.push\(_pendBind\)/);
  // 补绑成功要把浮层那份关掉，否则同一内容两处并存
  assert.match(VOICECALL, /if \(item\.card\) _cardClose\(item\.card\)/);
  // 触发点在页面渲染收尾（挂载路径有好几条，但都收敛到 _upRenderOverlay）
  assert.match(UISHARED, /function _upBindRetryFor\(rec\)/);
  assert.match(UISHARED, /_upBindRetryFor\(rec\);\s+\/\/ 这一页出现了/);
});

test("绑不上时退回浮层而不是丢卡", () => {
  // 位置信息没了还能补，内容没了就真没了。
  assert.match(VOICECALL, /if \(okBind\) return true;/);
  assert.match(VOICECALL, /那一页还没打开，卡片先放浮层，等页面出现会自己归位/);
});
