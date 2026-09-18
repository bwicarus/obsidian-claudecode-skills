// 一次任务 = 侧栏一个容器（ADR references/adr-turn-container.md）。
// 这里守的是 2026-09-18 那次「一个任务散成四个框」的三条成因里，属于 rc-turncard 的两条：
//   ① 同一次工具调用被两边各画一条（运行器带 args/result，App 自己执行时画一条裸芯片）；
//   ② 流式草稿被当成正式内容落库。
// ⚠ 去重有三个入口 —— addPart（同容器先后到达）、rename 的合并分支（两条原本在
//   不同容器：App 芯片先落进临时容器，运行器那条在真轮次容器）、renderTurn（历史回放）。
//   只堵一个等于只修一半，所以三条路各有一个用例。
// ⚠ 同一个工具在一轮里**真被调用两次**是正常的（连做两张卡），必须保留两条 ——
//   去重收窄成「只并掉光有标签的那条」，否则去重就变成丢信息。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const ROOT = new URL("../../", import.meta.url);
const TURNS = readFileSync(new URL("_server_deploy/static/pdf/rc-turncard.js", ROOT), "utf8");

function el(tag) {
  const e = {
    tagName: tag || "div", children: [], style: {}, dataset: {}, attrs: {}, hidden: false,
    classList: {
      _s: new Set(),
      add(...a) { a.forEach((x) => this._s.add(x)); },
      remove(...a) { a.forEach((x) => this._s.delete(x)); },
      contains(x) { return this._s.has(x); },
      toggle() {},
    },
    appendChild(c) { this.children.push(c); c.parentNode = this; return c; },
    insertBefore(c) { this.children.unshift(c); return c; },
    removeChild(c) { this.children = this.children.filter((x) => x !== c); },
    remove() { if (this.parentNode) this.parentNode.removeChild(this); },
    replaceWith() {},
    setAttribute(k, v) { this.attrs[k] = v; },
    getAttribute(k) { return this.attrs[k]; },
    removeAttribute(k) { delete this.attrs[k]; },
    addEventListener() {}, removeEventListener() {},
    querySelector() { return null; }, querySelectorAll() { return []; }, closest() { return null; },
    scrollTo() {},
    get isConnected() { return true; },
    get firstChild() { return this.children[0] || null; },
    set innerHTML(v) { this._h = v; }, get innerHTML() { return this._h || ""; },
    set textContent(v) { this._t = v; }, get textContent() { return this._t || ""; },
  };
  return e;
}

function loadTurnCard() {
  const thread = el("div");
  const document = {
    createElement: el, createTextNode: (t) => ({ text: t }),
    body: el("body"), head: el("head"), documentElement: el("html"),
    querySelector: () => thread, querySelectorAll: () => [],
    getElementById: () => thread, createDocumentFragment: () => el("frag"),
    addEventListener() {},
  };
  const win = {
    document, RC: {
      toolChip: { flowBtn: () => el("button"), create: () => el("div"), retype() {}, setState() {}, done() {} },
      flashcard: {}, adapter: () => ({ getContext: () => ({}) }),
    },
    setTimeout, clearTimeout, setInterval, clearInterval,
    requestAnimationFrame: (f) => setTimeout(f, 0),
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    addEventListener() {}, location: { href: "http://local/" },
    navigator: { userAgent: "node" },
    matchMedia: () => ({ matches: false, addListener() {}, addEventListener() {} }),
    getComputedStyle: () => ({}),
  };
  win.window = win;
  vm.runInContext(TURNS, vm.createContext(win), { filename: "rc-turncard.js" });
  assert.ok(win.RC.turnCard, "rc-turncard 未导出 RC.turnCard");
  return { tc: win.RC.turnCard, thread };
}

// 工具部件的指纹：工具名 + 信息量（r=有结果、a=有入参、bare=光有标签）
// ⚠ Array.from 不是多余的：partsOf 返回的数组造在 vm 的 realm 里，原型与本文件的
//   Array.prototype 不是同一个，deepStrictEqual 会判「结构相同但不是同一引用」而失败。
const tools = (tc, tid) =>
  Array.from(
    tc.partsOf(tid).filter((p) => p.kind === "tool")
      .map((p) => `${p.tool || p.label}[${(p.result ? "r" : "") + (p.args ? "a" : "") || "bare"}]`),
  );

const CHIP = { kind: "tool", tool: "anki", label: "制卡" };
const FULL = { kind: "tool", tool: "anki", label: "anki · completed", args: { x: 1 }, result: "ok" };

test("裸芯片先到、带结果的后到 → 并成一条并升级", () => {
  const { tc } = loadTurnCard();
  tc.open("t1");
  tc.addPart("t1", { ...CHIP });
  tc.addPart("t1", { ...FULL });
  assert.deepEqual(tools(tc, "t1"), ["anki[ra]"]);
});

test("带结果的先到、裸芯片后到 → 一条且不被降级", () => {
  const { tc } = loadTurnCard();
  tc.open("t1");
  tc.addPart("t1", { ...FULL });
  tc.addPart("t1", { ...CHIP });
  assert.deepEqual(tools(tc, "t1"), ["anki[ra]"]);
});

test("同一个工具真被调用两次 → 保留两条（去重不得丢信息）", () => {
  const { tc } = loadTurnCard();
  tc.open("t1");
  tc.addPart("t1", { kind: "tool", tool: "anki", args: { c: 1 }, result: "卡1" });
  tc.addPart("t1", { kind: "tool", tool: "anki", args: { c: 2 }, result: "卡2" });
  assert.deepEqual(tools(tc, "t1"), ["anki[ra]", "anki[ra]"]);
});

test("不同工具 → 各自保留", () => {
  const { tc } = loadTurnCard();
  tc.open("t1");
  tc.addPart("t1", { kind: "tool", tool: "anki", args: { c: 1 }, result: "卡1" });
  tc.addPart("t1", { kind: "tool", tool: "search", args: { q: "x" }, result: "y" });
  assert.deepEqual(tools(tc, "t1"), ["anki[ra]", "search[ra]"]);
});

test("临时容器改名并进真轮次 → 同一次调用不重复", () => {
  const { tc } = loadTurnCard();
  tc.open("tmp-local");
  tc.addPart("tmp-local", { ...CHIP });
  tc.open("01a0-real");
  tc.addPart("01a0-real", { ...FULL });
  assert.equal(tc.rename("tmp-local", "01a0-real"), true);
  assert.deepEqual(tools(tc, "01a0-real"), ["anki[ra]"]);
  assert.equal(tc.has("tmp-local"), false, "改名后旧容器应当消失");
});

test("历史回放里并存的两条同一次调用 → 合成一条", () => {
  const { tc, thread } = loadTurnCard();
  tc.renderTurn("t1", [{ ...CHIP }, { ...FULL }], thread, { historyReplay: true });
  assert.deepEqual(tools(tc, "t1"), ["anki[ra]"]);
});

test("还在流的草稿不进 partsOf（半截话不得落库）", () => {
  const { tc } = loadTurnCard();
  tc.open("t1");
  tc.draftText("t1", "明白了：正面放中文，背面");
  assert.equal(tc.partsOf("t1").length, 0);
  tc.draftText("t1", "明白了：正面放中文，背面放日文。");
  tc.freezeDraft("t1");
  assert.deepEqual(Array.from(tc.partsOf("t1").map((p) => p.kind)), ["text"]);
});
