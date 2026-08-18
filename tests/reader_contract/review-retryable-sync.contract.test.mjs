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
