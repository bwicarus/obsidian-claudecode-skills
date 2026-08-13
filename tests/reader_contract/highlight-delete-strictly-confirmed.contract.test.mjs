// 通用删除入口：只有严格 true 才算删掉。
//
// 这条链的每一次假删，追到底都是同一句话：把"没告诉我"当成了"成功"。先是底座返回
// undefined，后是适配层不 return，再是 `ok !== false` 放行 undefined。这里把口子
// 全部收死，并覆盖几种最容易漏的形状：
//
//   · onDelete 同步 throw —— Promise.resolve(onDelete()) 会在构造 Promise 之前
//     就调用它，异常直接逃出这条链，连 catch 都接不住
//   · 返回 undefined / null —— "没结论"不是"成功"
//   · 返回 {ok:false} 之类的真值对象 —— 真值不等于确认删除
//   · 根本没有 onDelete —— 早先仍会 row.remove()，是最彻底的假删
import assert from "node:assert/strict";
import test from "node:test";
import vm from "node:vm";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const RC_HIGHLIGHT = readFileSync(
  new URL("_server_deploy/static/pdf/rc-highlight.js", ROOT),
  "utf8",
);

// 取出列表删除按钮的 onclick 处理器原文。
function listDeleteHandler() {
  const anchor = RC_HIGHLIGHT.indexOf("rc-hl-swipe-del').onclick");
  assert.notEqual(anchor, -1, "找不到列表删除按钮的处理器");
  const start = RC_HIGHLIGHT.indexOf("function (e) {", anchor);
  const open = RC_HIGHLIGHT.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < RC_HIGHLIGHT.length; i += 1) {
    if (RC_HIGHLIGHT[i] === "{") depth += 1;
    if (RC_HIGHLIGHT[i] === "}") depth -= 1;
    if (depth === 0) return RC_HIGHLIGHT.slice(start, i + 1);
  }
  assert.fail("处理器括号未闭合");
}

function paletteClickHandler() {
  const anchor = RC_HIGHLIGHT.indexOf("i.onclick = function (e)");
  assert.notEqual(anchor, -1, "找不到色板点击处理器");
  const start = RC_HIGHLIGHT.indexOf("function (e) {", anchor);
  const open = RC_HIGHLIGHT.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < RC_HIGHLIGHT.length; i += 1) {
    if (RC_HIGHLIGHT[i] === "{") depth += 1;
    if (RC_HIGHLIGHT[i] === "}") depth -= 1;
    if (depth === 0) return RC_HIGHLIGHT.slice(start, i + 1);
  }
  assert.fail("色板处理器括号未闭合");
}

function colorClass(initial = false) {
  let on = initial;
  return {
    contains(name) { return name === "on" && on; },
    add(name) { if (name === "on") on = true; },
    remove(name) { if (name === "on") on = false; },
  };
}

async function clickNewColor(onColor) {
  const oldColor = { classList: colorClass(true), dataset: { c: "yellow" } };
  const newColor = { classList: colorClass(false), dataset: { c: "blue" } };
  const palette = [oldColor, newColor];
  const toasts = [];
  const context = {
    i: newColor,
    pop: { querySelectorAll: () => palette },
    opts: { note: "", body: "", sentence: "", onColor },
    toast: (message) => { toasts.push(String(message)); },
    closeEditor() {},
    colorWriteRevision: 0,
    Promise,
    Array,
    handler: null,
  };
  context.globalThis = context;
  vm.runInNewContext(`handler = (${paletteClickHandler()});`, context);
  context.handler({ stopPropagation() {} });
  await new Promise((resolve) => setImmediate(() => setImmediate(resolve)));
  return {
    oldOn: oldColor.classList.contains("on"),
    newOn: newColor.classList.contains("on"),
    toasts,
  };
}

function clickDelete(onDelete) {
  let removed = false;
  const toasts = [];
  const context = {
    opts: onDelete === undefined ? {} : { onDelete },
    row: { remove: () => { removed = true; } },
    h: { id: "h1" },
    toast: (message) => { toasts.push(String(message)); },
    Promise,
    handler: null,
  };
  context.globalThis = context;
  vm.runInNewContext(`handler = (${listDeleteHandler()});`, context);
  context.handler({ stopPropagation() {} });
  // 让链上的 then/catch 跑完
  return new Promise((resolve) => setImmediate(() => setImmediate(
    () => resolve({ removed, toasts }),
  )));
}

test("确认删除才移除该行", async () => {
  const { removed } = await clickDelete(() => Promise.resolve(true));
  assert.equal(removed, true);
});

test("同步抛出的删除不得移除该行", async () => {
  const { removed } = await clickDelete(() => { throw new Error("boom"); });
  assert.equal(removed, false, "同步异常必须被这条链接住，而不是逃出去");
});

test("返回 undefined 不算删掉", async () => {
  const { removed } = await clickDelete(() => undefined);
  assert.equal(removed, false, "「没告诉我」不是「成功」");
});

test("返回 null 不算删掉", async () => {
  const { removed } = await clickDelete(() => null);
  assert.equal(removed, false);
});

test("返回真值对象也不算删掉", async () => {
  const { removed } = await clickDelete(() => ({ ok: false }));
  assert.equal(removed, false, "真值不等于确认删除，必须严格 true");
});

test("拒绝的 Promise 不得移除该行", async () => {
  const { removed } = await clickDelete(() => Promise.reject(new Error("nope")));
  assert.equal(removed, false);
});

test("没有删除能力时保留该行并给出提示", async () => {
  const { removed, toasts } = await clickDelete(undefined);
  assert.equal(removed, false, "缺 handler 时移除该行是最彻底的假删");
  assert.match(toasts.join(" "), /不支持删除/);
});

test("颜色持久化明确成功后保留新色", async () => {
  const result = await clickNewColor(() => Promise.resolve(true));
  assert.equal(result.oldOn, false);
  assert.equal(result.newOn, true);
});

test("颜色持久化未确认时恢复原色并提示", async () => {
  const result = await clickNewColor(() => Promise.resolve(false));
  assert.equal(result.oldOn, true);
  assert.equal(result.newOn, false);
  assert.match(result.toasts.join(" "), /恢复原颜色/);
});
