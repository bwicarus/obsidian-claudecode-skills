import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// **行为测试**：网页快照 POST 的 viewport 载荷必须过得了桥的 ValidateViewport。
//
// 2026-08-23 实测的现网 bug：d0cb6109（8-15）给模型加"选区所在句"时，
// JS 侧把 selectionContext 同时挂到了 viewport 和 active 两个对象上，
// 而 C# 侧**只给 ValidateActiveReading 放行了，漏了 ValidateViewport**。
// 后果不是报错提示，而是：用户在网页上一选中文字 → 整条快照 400 →
// content.js 的 .catch 按 THROTTLE_MS(1.5s) 重发同样的 body → 无限重试。
// 表现是"快照响应慢"，跟真原因差得很远。
//
// 这条测试把**两侧的真实字面量**都读出来比对，而不是各自写一份期望值 ——
// 期望值写死的话，哪天 C# 改了允许集，这里照样绿。

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8");

const BG = read("extensions/bw-reader-webext/background.js");
const CS = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/DirectContextSnapshot.cs");

/** 从 C# 的 ValidateViewport 函数体里抠出 `string[] requiredFields = [...]` 的真实字面量。
 *  ⚠ 抠的是**函数内的局部变量**，不是某个常量名 —— 别照名字猜，猜错了测试会静静地
 *  退化成"抠出 0 个字段"然后照样绿。所以下面对抠出的条数有硬断言。 */
function viewportAllowedFields() {
  const fn = CS.indexOf("ValidateViewport");
  assert.ok(fn >= 0, "ValidateViewport 改名了？");
  const decl = CS.indexOf("string[] requiredFields", fn);
  assert.ok(decl > fn, "ValidateViewport 里找不到 requiredFields —— 结构变了");
  const end = CS.indexOf("];", decl);
  assert.ok(end > decl, "requiredFields 的字面量数组没闭合？");
  const block = CS.slice(decl, end);
  const required = [...block.matchAll(/"([a-zA-Z]+)"/g)].map((m) => m[1]);
  assert.ok(required.length >= 8,
    `必填字段只抠出 ${required.length} 个（${required}）—— 抠错了，这条测试等于没测`);
  assert.ok(required.includes("visibleText") && required.includes("selectionState"),
    `抠出来的字段不像 viewport 的：${required}`);
  return { required, allowed: new Set([...required, "controlCorrelation"]) };
}

test("桥侧 viewport 允许集是「必填 + 唯一可选 controlCorrelation」", () => {
  // 这条先钉住规则本身：允许集一旦放宽/收紧，下面那条的前提就变了。
  const i = CS.indexOf("HashSet<string> allowed = requiredFields");
  assert.ok(i >= 0, "ValidateViewport 的允许集构造变了，重新确认这组测试");
  const near = CS.slice(i, i + 260);
  assert.match(near, /\.Append\("controlCorrelation"\)/);
  // 确认没有偷偷放行 selectionContext（放行了的话 JS 侧就不必剥）
  assert.doesNotMatch(near, /selectionContext/,
    "桥已放行 selectionContext？那 background.js 的剥离可以撤掉了");
});

test("发出去的 viewport 载荷不带 selectionContext", () => {
  const i = BG.indexOf("async function readerPostSnapshot(prepared)");
  assert.ok(i >= 0, "readerPostSnapshot 改名了？");
  const j = BG.indexOf("\nasync function ", i + 10);
  const body = BG.slice(i, j > i ? j : i + 6000);

  // 必须显式剥离，而不是指望上游不设置
  assert.match(
    body,
    /const \{ selectionContext: _viewportSelectionContext, \.\.\.viewportPayload \} = viewport;/,
    "viewport 载荷没有剥掉 selectionContext —— 一带选区就整条 400 + 无限重试");
  assert.match(body, /viewport: viewportPayload,/);
  // ⚠ 不能是裸 `viewport,`（那是修之前的写法）
  assert.doesNotMatch(body, /\n    viewport,\n/,
    "又把整个 viewport 对象原样发出去了");
});

test("但 active 那份仍然带着 —— 剥错地方等于丢功能", () => {
  const i = BG.indexOf("async function readerPostSnapshot(prepared)");
  const j = BG.indexOf("\nasync function ", i + 10);
  const body = BG.slice(i, j > i ? j : i + 6000);
  // active 是从本地 viewport.selectionContext 读的；如果上游不再计算它，这里就空了
  assert.match(body, /viewport\.selectionContext\s*\n?\s*\?\s*\{ selectionContext: viewport\.selectionContext/);
  assert.match(body, /selectionContextSource: "web-block"/);
  // 而 C# 侧 active 的允许集确实放行了这两个
  const k = CS.indexOf("ValidateActiveReading");
  assert.ok(k >= 0);
  const activeNear = CS.slice(k, k + 4000);
  assert.match(activeNear, /\.Append\("selectionContext"\)/);
  assert.match(activeNear, /\.Append\("selectionContextSource"\)/);
});

test("规则实跑：修前的 body 会被拒，修后的能过", () => {
  const { required, allowed } = viewportAllowedFields();

  // 照抄 C# 的判据：缺必填 或 有允许集之外的字段 → 拒
  const accepts = (obj) => {
    const actual = new Set(Object.keys(obj));
    if (required.some((f) => !actual.has(f))) return false;
    return ![...actual].some((f) => !allowed.has(f));
  };

  const base = Object.fromEntries(required.map((f) => [f, "x"]));
  assert.equal(accepts(base), true, "必填齐全应当通过");
  assert.equal(accepts({ ...base, controlCorrelation: "c" }), true,
    "controlCorrelation 是唯一可选");
  assert.equal(accepts({ ...base, selectionContext: "选区所在句" }), false,
    "带 selectionContext 的 viewport 必须被拒 —— 这就是那个现网 bug");
  const { selectionContext: _drop, ...stripped } = { ...base, selectionContext: "选区所在句" };
  assert.equal(accepts(stripped), true, "剥掉之后应当通过");
});
