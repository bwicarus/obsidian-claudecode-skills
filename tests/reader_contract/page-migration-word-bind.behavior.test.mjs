import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// **行为测试**，不是字面量断言：把 migrateNativePDFNotes 与 nativePDFPageMap 的
// 真实函数体从文件里抠出来实跑。测的是文件里那段代码本身 —— 挂错地方、改名、
// 语义悄悄变了都会立刻炸，而纯 match 断言只能保证"代码在"。
// （教训见 references/silent-failure-lessons.md 第六节。）
//
// 背景：插删页时词锚（page-chars）必须跟 anchor 一起迁页号。这条 2026-08-20
// 首次修时只覆盖了「Pi 的 card 槽」，两个方向都不全：
//   · 槽：AI 直绑的词锚落在 html.bind（persistBoundCard），不在 card.bind
//   · 表面：插删页在 App 内**本地执行**，Pi 那份根本不参与
// 两种漏法的症状都不是报错，而是「卡片去了新页、描边和序号留在旧页」，
// 看着像标记随机丢失。

const ROOT = new URL("../../", import.meta.url);
const SRC = readFileSync(
  new URL("_server_deploy/static/pdf/native-local-runtime.js", ROOT), "utf8");

function extract(name) {
  const i = SRC.indexOf(`function ${name}(`);
  assert.ok(i >= 0, `${name} 找不到了 —— 改名了？迁移可能已经不在这条路上`);
  const j = SRC.indexOf("\n  function ", i + 10);
  assert.ok(j > i, `切不出 ${name} 的函数体`);
  return SRC.slice(i, j);
}

const { migrateNativePDFNotes, nativePDFPageMap } = new Function(`
  function clone(v) { return JSON.parse(JSON.stringify(v)); }
  class RuntimeError extends Error { constructor(m, c) { super(m); this.code = c; } }
  ${extract("nativePDFPageMap")}
  ${extract("migrateNativePDFNotes")}
  return { migrateNativePDFNotes, nativePDFPageMap };
`)();

const INS = { operation: "insert", pivotPage: 3 };
const DEL = { operation: "delete", pivotPage: 3 };
// ⚠ 不硬编码页号映射：先问真实的 nativePDFPageMap，再拿它建期望值。
//   硬编码的话，pageMap 的语义一改，这里就在测一个想象中的系统。
const ins = (p) => nativePDFPageMap(p, INS.operation, INS.pivotPage);
const del = (p) => nativePDFPageMap(p, DEL.operation, DEL.pivotPage);

const note = (slot, page, anchorPage = 5) => ([{
  id: "n1", anchor: { kind: "pdf", page: anchorPage },
  [slot]: { content: "x", bind: { kind: "page-chars", page, from: 10, to: 12 } },
}]);

test("探针：页号映射的方向符合预期（插入让后续页后移、删除让该页消失）", () => {
  assert.equal(ins(1), 1);
  assert.equal(ins(3), 4, "插入点及其后的页应当后移");
  assert.equal(del(3), null, "被删的那一页应当映射成 null");
  assert.equal(del(4), 3);
});

test("card 槽（手动拖卡钉的）随插页迁", () => {
  const out = migrateNativePDFNotes(note("card", 4), INS);
  assert.equal(out[0].card.bind.page, ins(4));
  assert.equal(out[0].anchor.page, ins(5), "anchor 那一路不能被带坏");
});

test("html 槽（AI 直绑的）也要迁 —— 这一槽此前完全没被迁过", () => {
  const out = migrateNativePDFNotes(note("html", 4), INS);
  assert.equal(out[0].html.bind.page, ins(4));
});

test("词锚页被删 → 只撤词锚，便签留着", () => {
  // 跟 anchor 的处置不同：anchor 没了便签无处安放（整条丢弃），
  // 词锚没了它还能作为普通便签继续存在。跟服务端 _pam_notes 同口径。
  const out = migrateNativePDFNotes(note("html", 3, 9), DEL);
  assert.equal(out.length, 1, "便签不该被丢掉");
  assert.equal(out[0].html.bind, null);
  assert.equal(out[0].html.content, "x", "卡片内容必须还在");
});

test("anchor 页被删 → 整条便签丢弃（原有语义，不能被词锚这次改动带坏）", () => {
  const out = migrateNativePDFNotes(note("card", 9, 3), DEL);
  assert.equal(out.length, 0);
});

test("两个槽同时有 bind 时各迁各的", () => {
  const out = migrateNativePDFNotes([{
    id: "n2", anchor: { kind: "pdf", page: 1 },
    card: { bind: { kind: "page-chars", page: 4 } },
    html: { bind: { kind: "page-chars", page: 6 } },
  }], INS);
  assert.equal(out[0].card.bind.page, ins(4));
  assert.equal(out[0].html.bind.page, ins(6));
});

test("不该动的一律不动，且不因为脏数据崩掉", () => {
  const out = migrateNativePDFNotes([
    { id: "a", anchor: { kind: "pdf", page: 1 }, card: { bind: { kind: "epub-cfi", page: 4 } } },
    { id: "b", anchor: { kind: "pdf", page: 1 }, card: { bind: null } },
    { id: "c", anchor: { kind: "pdf", page: 1 }, html: "不是对象" },
    { id: "d", anchor: { kind: "pdf", page: 1 }, text: "普通便签" },
    { id: "e", anchor: { kind: "epub", page: 4 } },
    { id: "f", anchor: { kind: "pdf", page: 1 }, card: { bind: { kind: "page-chars", page: 4.5 } } },
  ], INS);
  assert.equal(out.length, 6, "一条都不该被丢掉");
  assert.equal(out[0].card.bind.page, 4, "非 page-chars 的 bind 不归这条管");
  assert.equal(out[1].card.bind, null);
  assert.equal(out[2].html, "不是对象", "槽不是对象时要跳过，不能崩");
  assert.equal(out[3].text, "普通便签");
  assert.equal(out[4].anchor.page, 4, "EPUB 锚不参与 PDF 页号迁移");
  assert.equal(out[5].card.bind.page, 4.5, "页号不是整数时跳过");
});

test("原有契约不变：非数组输入仍抛 BW_NATIVE_PDF_MUTATION_STATE", () => {
  assert.throws(() => migrateNativePDFNotes(null, INS), (e) => e.code === "BW_NATIVE_PDF_MUTATION_STATE");
});

test("不污染入参", () => {
  const input = note("card", 4);
  migrateNativePDFNotes(input, INS);
  assert.equal(input[0].card.bind.page, 4);
});
