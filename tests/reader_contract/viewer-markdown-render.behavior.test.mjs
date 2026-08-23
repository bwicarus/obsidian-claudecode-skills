// 把**新编译产物里那份真实的渲染器**抠出来，喂现场快照正文，
// 用一个极小的 DOM 替身跑一遍，看渲出什么结构。
// 不重写逻辑 —— 要验的是文件里那段代码。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const CS = readFileSync(
  new URL("../../extensions/bw-reader-webext/windows/ComputerVoiceAudio/DirectSnapshotPresentation.cs", import.meta.url),
  "utf8");

function extractFn(name) {
  const i = CS.indexOf(`function ${name}(`);
  if (i < 0) throw new Error(`找不到 ${name}`);
  let depth = 0, started = false, j = i;
  for (; j < CS.length; j++) {
    const ch = CS[j];
    if (ch === "{") { depth++; started = true; }
    else if (ch === "}") { depth--; if (started && depth === 0) { j++; break; } }
  }
  return CS.slice(i, j);
}

// 极小 DOM 替身：只支持渲染器用到的那几个 API
class El {
  constructor(tag) { this.tag = tag; this.children = []; this.attrs = {}; this._text = ""; }
  set className(v) { this.attrs.class = v; }
  get className() { return this.attrs.class || ""; }
  set textContent(v) { this._text = String(v); this.children = []; }
  get textContent() {
    return this._text + this.children.map((c) => c.textContent ?? c.data ?? "").join("");
  }
  set title(v) { this.attrs.title = v; }
  appendChild(c) { this.children.push(c); return c; }
  replaceChildren() { this.children = []; this._text = ""; }
  get childNodes() { return this.children; }
}
const document = {
  createElement: (t) => new El(t),
  createTextNode: (d) => ({ data: d, textContent: d, tag: "#text" }),
};

const src = [
  extractFn("splitUnescaped"),
  extractFn("unescapeLeaf"),
  extractFn("leafSegments"),
  extractFn("paintLeaf"),
  extractFn("isSeparatorRow"),
  extractFn("paintBlocks"),
  "return { paintBlocks, splitUnescaped, unescapeLeaf, isSeparatorRow };",
].join("\n");
const api = new Function("document", "MARK_L", "MARK_R", src)(
  document, "\u27E6", "\u27E7");


// ── 铁律：这两条错了会静默渲错，不会报错 ──────────────────────
test("铁律A：先按未转义的分隔切列，\| 不是列分隔", () => {
  // 版式路径把字面竖线写成 \| 。先反转义再切会多切一列 ——
  // 表格会整体错位，而且看起来"只是排版怪"，不会有任何报错。
  const cells = api.splitUnescaped(String.raw`| a | b\|c | d |`, "|");
  assert.equal(cells.length, 5, `转义竖线被当成列分隔了：${JSON.stringify(cells)}`);
  assert.equal(cells[2].trim(), String.raw`b\|c`);
});

test("铁律B：表格必须由分隔行确认，不能只看行首是 |", () => {
  // 非版式路径不转义 | ，正文里天然可能出现行首竖线。
  assert.equal(api.isSeparatorRow("| --- | --- |"), true);
  assert.equal(api.isSeparatorRow("| :--- | ---: |"), true);
  assert.equal(api.isSeparatorRow("| 左 | 右 |"), false, "把正文行当成了分隔行");
  assert.equal(api.isSeparatorRow("普通一行"), false);
});

test("反转义只认 \ | ⟦ ⟧ 四种，其余原样保留两个字符", () => {
  // ⚠ 期望值用字符码拼：这一行经过 shell heredoc → 文件 → JS 三层转义，
  //   直接写 \ 极易在某一层被吃掉一个（第一次就是这么写错的，代码是对的）。
  const BS = String.fromCharCode(92), LB = String.fromCharCode(0x27E6);
  assert.equal(api.unescapeLeaf(String.raw`a\|b\\c\⟦d`),
    "a|b" + BS + "c" + LB + "d");
  assert.equal(api.unescapeLeaf(String.raw`C:\Users`), String.raw`C:\Users`,
    "把不认识的转义吃掉了 —— 路径会变形");
});

test("渲染真实形态：小节 / 表格 / [NN] 块号 / 卡片块 / 折叠说明", () => {
  const L = "\u27E6", R = "\u27E7";
  const sample = [
    "【当前页之前】",
    "| 左 | 右 |",
    "| --- | --- |",
    String.raw`| [01] 甲 | [02] 乙\|丙 |`,
    "",
    "【当前屏幕可见原文】",
    "一段普通正文。",
    `${L}CARD_START n="3" label="キムチ"${R}卡片正文${L}CARD_END${R}`,
    "_（锚点映射已折叠：4109 字符，机读内容见 JSON 快照）_",
  ].join("\n");

  const root = new El("div");
  api.paintBlocks(root, sample);

  const kinds = {};
  (function walk(el) {
    for (const c of el.children || []) {
      const k = c.className ? `${c.tag}.${c.className}` : c.tag;
      kinds[k] = (kinds[k] || 0) + 1;
      walk(c);
    }
  })(root);

  assert.equal(kinds.h3, 2, "小节标题没渲出来");
  assert.equal(kinds.table, 1, "表格没渲出来");
  assert.equal(kinds.td, 2, "单元格数不对");
  assert.equal(kinds["span.blk"], 2, "[NN] 块号徽标没渲出来");
  assert.equal(kinds["div.cardblk"], 1, "卡片块没渲出来");
  assert.equal(kinds["div.foldnote"], 1, "折叠说明没渲出来");

  // 徽标里是编号本身，且正文里那个 [NN] 前缀被摘掉了
  const all = root.textContent;
  assert.ok(all.includes("甲") && all.includes("乙|丙"), "单元格正文丢了或没反转义");
  assert.ok(!all.includes("[01]"), "块号既进了徽标又留在正文里");
});

test("零 innerHTML —— 不可信网页正文会进这里", () => {
  const CSRC = readFileSync(
    new URL("../../extensions/bw-reader-webext/windows/ComputerVoiceAudio/DirectSnapshotPresentation.cs",
      import.meta.url), "utf8");
  const i = CSRC.indexOf("<!doctype html>");
  const j = CSRC.indexOf("</html>", i);
  const viewer = CSRC.slice(i, j);
  assert.doesNotMatch(viewer, /\.innerHTML/, "查看器里出现了 innerHTML");
  assert.doesNotMatch(viewer, /insertAdjacentHTML/);
});
