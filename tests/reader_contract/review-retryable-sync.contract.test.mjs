import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const REVIEW = read("_server_deploy/static/pdf/rc-review.js");
const FLASHCARD = read("_server_deploy/static/pdf/rc-flashcard.js");

// C 组 #17 的 G4。
//
// 本地评分那笔写入是权威的、不会因为 Pi/Anki 失败而回滚 —— 但"外部 Anki 迟早会
// 追上"这个承诺，靠的是失败时把请求塞进 outbox 重投。以前只认
// `error.name === 'TypeError'`（fetch 本身没发出去），而 Pi 上最常见的失败根本
// 不是断网，是 **Anki 没起来 / 正在 sync** —— 那时服务端好好地返 502/503，
// 走不进 outbox，这次评分对 Anki 就永久丢了。

test("服务端暂时处理不了时，评分要能重投", () => {
  assert.match(REVIEW, /function _isRetryableSyncError\(error\)/);
  const body = REVIEW.slice(
    REVIEW.indexOf("function _isRetryableSyncError(error)"),
    REVIEW.indexOf("function _answerLocalCurrent("),
  );
  assert.ok(body.length > 0);
  assert.match(body, /error\.name === 'TypeError'/, "断网仍要算可重投");
  // 5xx 要细分：500 状态不明（服务端可能已经写了一半）不重投，
  // 502/503/504 是网关明确说"没被处理"才重投 —— 这才是 Anki 没起来的形态。
  assert.match(body, /status === 503/, "503 没被算成可重投");
  assert.match(body, /status === 502/);
  assert.doesNotMatch(body, /status >= 500/, "5xx 被一刀切了：500 重投有重复计分风险");
  assert.match(body, /status === 429/);
  assert.match(body, /status === 408/);
  // 4xx 不重投：那是"你发的东西不对"，重发一百次还是不对。
  assert.doesNotMatch(body, /status >= 400/);
});

test("状态码要带出错误对象，而不是塞进 message 让下游解析人话", () => {
  assert.match(REVIEW, /failure\.__httpStatus = response\.status \| 0;/);
  // 两个入 outbox 的判断都改用同一个函数
  // 数调用，不数定义（`function _isRetryableSyncError(error) {` 也会匹配上）。
  const uses = REVIEW.match(/(?<!function )_isRetryableSyncError\(error\)/g) || [];
  assert.equal(uses.length, 2, `期望两处评分路径都用它，实际 ${uses.length} 处`);
  assert.doesNotMatch(
    REVIEW,
    /if \(error && error\.name === 'TypeError' && RC\.outbox/,
    "还有地方在只认 TypeError",
  );
});

test("卡片状态同步必须检查 response.ok —— fetch 对非 2xx 不会 reject", () => {
  // 这一处以前**完全没有**检查 response.ok：服务端返 500 时 catch 根本不会跑，
  // 代码照常往下走，状态更新就这么没了，而且没有任何人知道。
  // 比"只认 TypeError"漏得更彻底。
  const body = FLASHCARD.slice(
    FLASHCARD.indexOf("'/pdf/api/entity/' + st.gid, body, 'PATCH'") - 2000,
    FLASHCARD.indexOf("'/pdf/api/entity/' + st.gid, body, 'PATCH'") + 400,
  );
  assert.match(body, /if \(response && response\.ok\) return true;/);
  assert.match(body, /failure\.__httpStatus = \(response && response\.status\) \| 0;/);
  assert.match(body, /status === 503/);
});

// ── C 组 #17 的 G3：「进度不统一」的本体 ──────────────────────────────
//
// 服务端评分后已经用 cardsInfo 把 Anki 的 interval/due/queue/type 回读好了并放进
// 响应的 `next`，而客户端的 .then() 只判 ok/error，把 data.next 整个扔了。于是
// 同一张卡：本地卡库存启发式间隔（1.2×/2.5×/3.5×），Anki 存 FSRS 真间隔 ——
// 从第一次评分起就分叉，而且分叉随复习次数指数放大。

test("Anki 回读的真调度要写回本地，不能扔掉", () => {
  assert.match(REVIEW, /function _adoptExternalSchedule\(local, next, reviewedAt, aid\)/);
  // 就在那个曾经把 data.next 扔掉的 .then() 里消费它
  assert.match(REVIEW, /_adoptExternalSchedule\(local, data && data\.next, reviewedAt, aid\)/);
  // 调用方要能把 local 与 reviewedAt 传进来
  assert.match(
    REVIEW,
    /function _projectLegacyLocalAnswer\(card, ease, aid, local, reviewedAt\)/,
  );
  assert.match(REVIEW, /_projectLegacyLocalAnswer\(card, ease, aid, local, reviewedAt\);/);
});

test("只用 interval 换算，不碰 Anki 的 due 语义", () => {
  // Anki 的 due 含义随卡片类型而变：new 是位置序号、review 是距 collection
  // 创建日的天数、learning/relearning 是 epoch 秒 —— 要正确解释它得知道
  // collection 的 crt。interval 不需要：正数=天，负数=秒。
  const body = REVIEW.slice(
    REVIEW.indexOf("function _externalScheduleFrom("),
    REVIEW.indexOf("function _adoptExternalSchedule("),
  );
  assert.ok(body.length > 0);
  assert.match(body, /var iv = Number\(next\.interval\)/);
  assert.match(body, /iv > 0 \? iv : \(Math\.abs\(iv\) \/ 86400\)/, "负 interval 是秒");
  assert.doesNotMatch(body, /next\.due/, "碰了 due —— 它的含义随卡片类型而变");
});

test("写回失败要出声，但绝不回滚本地评分", () => {
  const body = REVIEW.slice(
    REVIEW.indexOf("function _adoptExternalSchedule("),
    REVIEW.indexOf("function _projectLegacyLocalAnswer("),
  );
  assert.ok(body.length > 0);
  // 复习**事件**是权威的，只有它附带的"下次何时到期"是估算值。
  assert.match(body, /window\.dlog/, "写回失败被静默吞了");
  assert.doesNotMatch(body, /_restoreRejectedAnswer|revert/i);
  // 记下这个间隔是谁算的 —— 没有它就分不清"本地估算"和"Anki 真值"，
  // 而两者数值上完全可能撞车。
  assert.match(body, /scheduleSource: 'anki-fsrs'/);
});
