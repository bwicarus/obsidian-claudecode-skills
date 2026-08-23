import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// 快照查看器的公式渲染：TeX → MathML。
//
// 为什么是 MathML 而不是 MathJax/KaTeX：查看器 CSP 是
//   default-src 'none'; script-src 'unsafe-inline'（**没有 'self'**）
// 外部脚本一律加载不了，KaTeX 还要 font-src。MathML 是浏览器原生排版，
// 零外部资源、零 innerHTML。
//
// **行为测试**：把编译产物里那份真实的转换器抠出来实跑，不重写逻辑。

const CS = readFileSync(
  new URL("../../extensions/bw-reader-webext/windows/ComputerVoiceAudio/DirectSnapshotPresentation.cs",
    import.meta.url), "utf8");

function extractFn(name) {
  const i = CS.indexOf(`function ${name}(`);
  assert.ok(i >= 0, `找不到 ${name} —— 改名了？`);
  let depth = 0, started = false, j = i;
  for (; j < CS.length; j++) {
    const ch = CS[j];
    if (ch === "{") { depth++; started = true; }
    else if (ch === "}") { depth--; if (started && depth === 0) { j++; break; } }
  }
  return CS.slice(i, j);
}

function extractConst(name) {
  // ⚠ 按**大括号/中括号配平**取，不要用"找下一个 ;"或"找某个缩进" ——
  //   第一次就是那么写的，把 TEX_SYM 抠成了跨越下一个常量的一大段，
  //   结果是 "Identifier already declared"。抠错了要炸，别静静地抠一半。
  const i = CS.indexOf(`const ${name} =`);
  assert.ok(i >= 0, `找不到常量 ${name}`);
  let j = CS.indexOf("=", i) + 1;
  while (/\s/.test(CS[j])) j++;
  const open = CS[j];
  if (open !== "{" && open !== "[") {
    const semi = CS.indexOf(";", j);
    return CS.slice(i, semi + 1);
  }
  const close = open === "{" ? "}" : "]";
  let depth = 0;
  for (; j < CS.length; j++) {
    if (CS[j] === open) depth++;
    else if (CS[j] === close) { depth--; if (!depth) { j++; break; } }
  }
  const semi = CS.indexOf(";", j);
  return CS.slice(i, semi + 1);
}

// 极小 DOM 替身：只支持转换器用到的 API，并记录命名空间
class El {
  constructor(ns, tag) { this.ns = ns; this.tag = tag; this.children = []; this.attrs = {}; this._t = ""; }
  set textContent(v) { this._t = String(v); this.children = []; }
  get textContent() { return this._t + this.children.map((c) => c.textContent).join(""); }
  set className(v) { this.attrs.class = v; }
  setAttribute(k, v) { this.attrs[k] = v; }
  appendChild(c) { this.children.push(c); return c; }
}
const document = {
  createElementNS: (ns, tag) => new El(ns, tag),
  createElement: (tag) => new El(null, tag),
  createTextNode: (d) => ({ tag: "#text", textContent: d, children: [] }),
};

const src = [
  extractConst("MATHNS"), extractConst("TEX_SYM"), extractConst("TEX_FUNC"),
  extractFn("mel"), extractFn("texTokens"), extractFn("texAtom"),
  extractFn("texRow"), extractFn("texToMathML"),
  extractFn("mathSegments"), extractFn("paintText"),
  "return { texToMathML, mathSegments, paintText, MATHNS };",
].join("\n");
const api = new Function("document", src)(document);

/** 把 MathML 树压成 `tag(子)` 形式，便于断言结构。 */
function shape(el) {
  if (el.tag === "#text") return `"${el.textContent}"`;
  if (!el.children.length) return `${el.tag}:${el._t}`;
  return `${el.tag}(${el.children.map(shape).join(" ")})`;
}

test("命名空间必须是 MathML —— 用错命名空间浏览器不会排版，只会当普通元素", () => {
  const m = api.texToMathML("x", false);
  assert.equal(m.ns, "http://www.w3.org/1998/Math/MathML");
  assert.equal(m.tag, "math");
});

test("分式 / 根式 / 上下标：教科书里最高频的三样", () => {
  assert.match(shape(api.texToMathML("\\frac{a}{b}", false)), /mfrac\(mrow\(mi:a\) mrow\(mi:b\)\)/);
  assert.match(shape(api.texToMathML("\\sqrt{2}", false)), /msqrt\(mrow\(mn:2\)\)/);
  assert.match(shape(api.texToMathML("\\sqrt[3]{x}", false)), /mroot\(mrow\(mi:x\) mrow\(mn:3\)\)/);
  assert.match(shape(api.texToMathML("x^2", false)), /msup\(mi:x mn:2\)/);
  assert.match(shape(api.texToMathML("a_i", false)), /msub\(mi:a mi:i\)/);
  // 上下标同时出现要走 msubsup，顺序是 base, sub, sup
  assert.match(shape(api.texToMathML("x_i^2", false)), /msubsup\(mi:x mi:i mn:2\)/);
});

test("希腊字母与运算符走符号表", () => {
  assert.match(shape(api.texToMathML("\\alpha", false)), /mo:α/);
  assert.match(shape(api.texToMathML("\\sum", false)), /mo:∑/);
  assert.match(shape(api.texToMathML("\\int", false)), /mo:∫/);
  assert.match(shape(api.texToMathML("\\le", false)), /mo:≤/);
});

test("整式：\\int_0^1 x^2 dx 这类完整表达式", () => {
  const s = shape(api.texToMathML("\\int_0^1 x^2 \\, dx", false));
  assert.match(s, /msubsup\(mo:∫ mn:0 mn:1\)/, "积分限没变成上下标");
  assert.match(s, /msup\(mi:x mn:2\)/);
});

test("display 模式带 display=block", () => {
  assert.equal(api.texToMathML("x", true).attrs.display, "block");
  assert.equal(api.texToMathML("x", false).attrs.display, undefined);
});

test("不认识的命令 → 原样显示 TeX 源码，不渲成似是而非的东西", () => {
  assert.throws(() => api.texToMathML("\\begin{matrix}a\\end{matrix}", false));
  // paintText 会把它兜成 .texraw
  const box = document.createElement("div");
  api.paintText(box, "见 $\\begin{matrix}a\\end{matrix}$ 一式");
  const raw = box.children.find((c) => c.attrs && c.attrs.class === "texraw");
  assert.ok(raw, "没有退回 TeX 源码显示");
  assert.match(raw.textContent, /\\begin\{matrix\}/);
});

test("$ 的守卫：货币金额不能被当成公式", () => {
  const only = (t) => api.mathSegments(t).filter((s) => s.tex != null);
  assert.equal(only("这本书 $5 到 $8 之间").length, 0, "货币被当成了公式");
  assert.equal(only("设 $x^2 + 1 = 0$ 求解").length, 1, "真公式没识别出来");
  // 界定符内侧不能紧邻空白（常见的误判来源）
  assert.equal(only("A $ 1 + 1 $ B").length, 0);
});

test("行间公式 $$…$$ 与行内 $…$ 都识别", () => {
  const segs = api.mathSegments("前 $$\\frac{a}{b}$$ 中 $x^2$ 后");
  const tex = segs.filter((s) => s.tex != null);
  assert.equal(tex.length, 2);
  assert.equal(tex[0].display, true);
  assert.equal(tex[1].display, false);
});
