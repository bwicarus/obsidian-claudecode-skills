import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// AI 读的能力说明与真实能力必须同步。
//
// ⚠ 这不是"顺手也测一下文档"。CLAUDE.md 记着一条更贵的教训：
//   **面向 AI 的说明写反比没写更糟** —— 没写它可能去翻 schema，写反了它直接放弃。
//   网页上"能不能钉卡片"就是这种：说明里不提，AI 就永远不会在网页上试。
const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8");

const CAP = "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderCapabilities/";
const MATRIX = read(CAP + "capability-matrix.md");
const CARDS = read(CAP + "cards.md");
const GET = read(CAP + "get.md");
const QUERY_CS = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderQuery.cs",
);
const PAGETEXT = read("extensions/bw-reader-webext/src/web-pagetext.js");

test("说明里写了网页也能钉卡片", () => {
  assert.match(
    MATRIX, /把卡片钉在正文某段[\s\S]{0,200}网页/,
    "能力矩阵不写网页支持，AI 就永远不会在网页上试",
  );
  assert.match(
    CARDS, /普通网页同样可以钉/,
    "cards.md 必须明说网页支持",
  );
});

test("说明里写清了网页的 page 恒为 1", () => {
  assert.match(CARDS, /`page` 恒为 \*\*1\*\*/);
  assert.match(GET, /"page":1/);
});

test("⚠ 说明里必须写清「凭什么挑 segment」—— 按文字对，不按位置对", () => {
  // 这是整个设计的支点：segments 每项自带 text，所以不需要位置对照表。
  // 说明里只写「挑中你要讲的那几项」而不写凭什么挑，AI 会去找一张
  // 并不存在的对照表 —— 用户 2026-08-23 就是卡在这一点上问出来的。
  assert.match(
    CARDS, /按文字对，不按位置对/,
    "必须明说匹配依据是文字内容，否则 AI 会去找位置对照表",
  );
  assert.match(
    CARDS, /segments 每一项都自带 `text`/,
    "必须点明 segment 自报家门，这是不需要 anchor map 的根本原因",
  );
});

test("说明里明确 [NN] 不是字符下标", () => {
  // 这正是 ANCHOR_MAP 时代那个陷阱的网页版：两套坐标长得一样。
  assert.match(
    GET, /\[NN\][\s\S]{0,120}不\*?\*?是\*?\*?字符下标/,
    "必须明说块编号不是下标，否则 AI 会拿 [NN] 去当 from/to",
  );
});

test("说明里的 region 取值与实现完全一致", () => {
  // 说明列了实现不产出的取值 → AI 按它判断会永远落空；
  // 实现产出了说明没列的 → AI 遇到就不知道怎么办。两个方向都要钉。
  const implemented = new Set();
  for (const m of PAGETEXT.matchAll(/return '(正文|边栏|导航|页眉|页脚|其它)'/g)) {
    implemented.add(m[1]);
  }
  assert.ok(implemented.size >= 5, `实现里只找到 ${implemented.size} 种区域取值`);
  for (const region of implemented) {
    assert.ok(
      CARDS.includes(region) && GET.includes(region),
      `实现会产出「${region}」，但说明里没有 —— AI 遇到它不知道怎么办`,
    );
  }
  const documented = new Set();
  for (const m of GET.matchAll(/`(正文|边栏|导航|页眉|页脚|其它)`/g)) {
    documented.add(m[1]);
  }
  for (const region of documented) {
    assert.ok(
      implemented.has(region),
      `说明里写了「${region}」，实现却从不产出 —— AI 按它判断会永远落空`,
    );
  }
});

test("说明与 C# 表面闸不矛盾：page-text 说支持网页，闸就必须放行", () => {
  const saysWeb = /普通网页也支持/.test(GET);
  const gateAllows = /"page-text" =>\s*kind is "pdf" or "epub" or "web"/.test(QUERY_CS);
  assert.equal(
    saysWeb, gateAllows,
    "说明说支持而闸不放行 = AI 反复试反复失败；闸放行而说明不写 = 能力白做",
  );
});

test("说明里写了 truncated 的含义", () => {
  assert.match(
    GET, /truncated[\s\S]{0,80}别当成/,
    "不说清 truncated，AI 会把'只有这些'当成'全部就这些'然后据此下结论",
  );
});
