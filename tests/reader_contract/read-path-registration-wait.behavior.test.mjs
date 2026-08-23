import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// 读与浏览器控制也给注册等待（2026-08-23）。
//
// 在这之前读**比写更脆**：写有 2.5 秒注册窗口、取图也有，唯独读和浏览器控制
// 是 TryGetLease 拿不到就立刻抛。于是 reader_page_text / reader_search /
// reader_toc 这些在网络抖一下的瞬间**必然失败** —— 而它们恰恰最常被调。
// 读重试本来就没有副作用，所以等一下纯赚。

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(
  `extensions/bw-reader-webext/windows/ComputerVoiceAudio/${p}`, ROOT), "utf8");

const QUERY = read("ReaderQuery.cs");
const CONTROL = read("ReaderBrowserControl.cs");
const OUTPUT = read("ReaderRealtimeOutput.cs");

/** 三处的等待上限必须取自各自源码，不硬编码 —— 硬编码的话某处被改小/改没都发现不了。 */
function waitMs(src, label) {
  const m = /SourceRegistrationWait =\s*\n?\s*TimeSpan\.FromMilliseconds\((\d[\d_]*)\)/
    .exec(src);
  assert.ok(m, `${label} 找不到 SourceRegistrationWait`);
  return Number(m[1].replace(/_/g, ""));
}

test("读路径不再零等待", () => {
  const i = QUERY.indexOf("internal async Task<ReaderQueryResponse> RequestAsync(");
  assert.ok(i > 0, "RequestAsync 改名了？");
  const body = QUERY.slice(i, i + 1200);
  assert.match(body, /await WaitForSourceAsync\(/,
    "读又变回拿不到租约就立刻失败了");
  assert.doesNotMatch(
    body.slice(0, body.indexOf("PendingQuery pending")),
    /_router\.TryGetLease\(/,
    "等待函数之外又直接查了一次租约");
  // 失败信息要说明等过 —— 否则跟"立刻失败"在日志里分不开
  assert.match(body, /已等待 /);
});

test("浏览器控制也不再零等待", () => {
  const i = CONTROL.indexOf(
    "internal async Task<ReaderBrowserControlResponse> RequestAsync(");
  assert.ok(i > 0);
  const body = CONTROL.slice(i, i + 1200);
  assert.match(body, /await WaitForSourceAsync\(/);
  assert.match(body, /已等待 /);
});

test("三条路的等待上限一致，且都短", () => {
  const w = waitMs(OUTPUT, "写");
  const q = waitMs(QUERY, "读");
  const c = waitMs(CONTROL, "浏览器控制");
  assert.equal(q, w, `读 ${q}ms 跟写 ${w}ms 不一致 —— 同一件事应当同一个口径`);
  assert.equal(c, w, `浏览器控制 ${c}ms 跟写 ${w}ms 不一致`);
  // 用户在阅读器里是站着等的：为一次写便签等太久比直接失败更难受
  assert.ok(w > 0 && w <= 5000, `等待上限 ${w}ms 不在合理区间`);
});
