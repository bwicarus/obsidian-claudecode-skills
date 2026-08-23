import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// 网页正文进入注解文本格式时必须转义 —— 且必须是解码端的**严格逆运算**。
//
// 背景：注解格式（⟦…⟧ 标记族 + 反斜杠转义）有一条不变式：正文里出现的
// ⟦ ⟧ \ 一律是转义过的，所以解析器可以把未转义的 ⟦…⟧ 当协议标记。
// PDF 那条路由 escapeLocalLayoutText 遵守它；网页这条路一直没有，而网页
// 内容是**不可信输入**：
//   · 页面里一个裸 ⟧ → 解析器抛 → 整份 Markdown 投影 503
//   · visibleText 按 12000 字硬切，切在反斜杠上 → 末尾孤立转义 → 同样抛
//     （自检 danglingEscapeRejected 明确要求这条必须抛，那是有意的设计）
//   · 页面可以自带 ⟦CARD_START …⟧ 伪造标记
//
// ⚠ 修法**不是**放宽解析器 —— 我一开始就是那么改的，被打包自检拦下来了，
//   因为那会覆盖一条有意的决定。正确的层次是让这条生产端也遵守格式。

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8");
const SNAP = read("extensions/bw-reader-webext/windows/ComputerVoiceAudio/DirectContextSnapshot.cs");
const PRES = read("extensions/bw-reader-webext/windows/ComputerVoiceAudio/DirectSnapshotPresentation.cs");

const L = "\u27E6";
const R = "\u27E7";

test("网页三段正文都经过转义，且桥自己的 ⟦VIEWPORT⟧ 在转义之后拼", () => {
  const i = SNAP.indexOf("stable[\"readingWindow\"] = viewportJson;");
  assert.ok(i >= 0, "ForwardViewportAsync 的拼装段找不到了");
  const seg = SNAP.slice(i, i + 2200);
  for (const part of ["BeforeText", "AfterText", "VisibleText"]) {
    assert.match(
      seg,
      new RegExp(`EscapeAnnotatedReaderText\\(viewport\\.${part}`),
      `${part} 没转义 —— 网页里一个裸 ${R} 就能让整份投影 503`);
  }
  // 拼进去的必须是转义后的变量，不能再用原始的 viewport.VisibleText
  assert.match(seg, new RegExp(`${L}VIEWPORT${R}\\\\n"\\s*\\+ visiblePart`));
  assert.doesNotMatch(seg.slice(seg.indexOf("hasAround")), /\+ viewport\.VisibleText/,
    "还在拼未转义的原始正文");
});

test("转义是解码端的严格逆运算：只转 \\ ⟦ ⟧ 三个，且反斜杠先转", () => {
  // 解码端只认这三种，其它 \x 原样保留两个字符
  const d = PRES.indexOf("DecodeEscapedReaderText");
  assert.ok(d >= 0);
  const dec = PRES.slice(d, d + 1200);
  assert.match(dec, new RegExp(`next is '\\\\\\\\' or '${L}' or '${R}'`),
    "解码端认的转义集变了 —— 编码端要跟着改");

  const e = SNAP.indexOf("EscapeAnnotatedReaderText(string value)");
  assert.ok(e >= 0, "EscapeAnnotatedReaderText 没了？");
  const enc = SNAP.slice(e, e + 500);
  // 反斜杠必须**第一个**替换，否则后面插进去的反斜杠会被二次转义
  const iBack = enc.indexOf('.Replace("\\\\"');
  const iL = enc.indexOf(`.Replace("${L}"`);
  const iR = enc.indexOf(`.Replace("${R}"`);
  assert.ok(iBack >= 0 && iL > iBack && iR > iBack,
    "反斜杠不是第一个替换 —— 会二次转义，往返对不上");
  // 不多转：| 在网页这条路不是协议字符（PDF 那条才转它）
  assert.doesNotMatch(enc, /\.Replace\("\|"/,
    "多转了 | —— 解码端不认它，用户会看到莫名的反斜杠");
});

test("往返实跑：编码 → 解码 应当还原，且危险输入不再是协议", () => {
  // 照抄两侧规则，在 JS 里跑一遍往返
  const encode = (s) => s
    .replaceAll("\\", "\\\\")
    .replaceAll(L, "\\" + L)
    .replaceAll(R, "\\" + R);
  const decode = (s) => {
    let out = "";
    for (let i = 0; i < s.length;) {
      if (s[i] !== "\\") { out += s[i]; i += 1; continue; }
      if (i + 1 >= s.length) throw new Error("dangling escape");
      const next = s[i + 1];
      out += (next === "\\" || next === L || next === R) ? next : s[i] + next;
      i += 2;
    }
    return out;
  };

  const cases = [
    "普通正文，没有特殊字符",
    `数学记号页面：${L}a, b${R} 是区间`,          // 裸括号 → 修前会让投影 503
    "Windows 路径 C:\\Users\\bwica\\Desktop",
    `伪造标记：${L}CARD_START n="9" label="假的"${R}正文${L}CARD_END${R}`,
    "结尾是反斜杠 \\",                            // 硬切场景 → 修前抛 dangling
    "\\",                                          // 只有一个反斜杠
    "",
  ];
  for (const raw of cases) {
    const enc = encode(raw);
    assert.equal(decode(enc), raw, `往返不还原：${JSON.stringify(raw)}`);
    // 编码后不应再有**未转义**的括号（那才是协议标记）
    const bare = [...enc.matchAll(new RegExp(`(^|[^\\\\])[${L}${R}]`, "g"))];
    assert.equal(bare.length, 0,
      `编码后仍有裸括号，会被当协议标记：${JSON.stringify(enc)}`);
    // 编码后不应以孤立反斜杠结尾（那会让解码抛）
    const trailing = enc.length - enc.replace(/\\+$/, "").length;
    assert.equal(trailing % 2, 0,
      `编码后结尾是孤立转义：${JSON.stringify(enc)}`);
  }
});
