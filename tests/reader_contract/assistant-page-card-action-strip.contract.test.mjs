import assert from "node:assert/strict";
import test from "node:test";
import vm from "node:vm";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const ASSISTANT = readFileSync(
  new URL("_server_deploy/static/pdf/rc-assistant.js", ROOT),
  "utf8",
);

function balancedFunction(name) {
  const start = ASSISTANT.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `找不到 ${name}`);
  const open = ASSISTANT.indexOf("{", start);
  let depth = 0;
  for (let index = open; index < ASSISTANT.length; index += 1) {
    if (ASSISTANT[index] === "{") depth += 1;
    if (ASSISTANT[index] === "}") depth -= 1;
    if (depth === 0) return ASSISTANT.slice(start, index + 1);
  }
  assert.fail(`${name} 括号未闭合`);
}

function assignedFunction(marker) {
  const start = ASSISTANT.indexOf(marker);
  assert.notEqual(start, -1, `找不到 ${marker}`);
  const functionStart = ASSISTANT.indexOf("function", start);
  const open = ASSISTANT.indexOf("{", functionStart);
  let depth = 0;
  for (let index = open; index < ASSISTANT.length; index += 1) {
    if (ASSISTANT[index] === "{") depth += 1;
    if (ASSISTANT[index] === "}") depth -= 1;
    if (depth === 0) {
      return `${ASSISTANT.slice(start, index + 1)};`;
    }
  }
  assert.fail(`${marker} 括号未闭合`);
}

class FakeElement {
  constructor(tag) {
    this.tagName = tag;
    this.children = [];
    this.attributes = {};
    this.textContent = "";
    this.className = "";
    this.disabled = false;
  }
  appendChild(child) { this.children.push(child); return child; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
}

function harness(actionResult = { ok: true }) {
  const calls = [];
  const reloads = [];
  const thread = new FakeElement("thread");
  const context = {
    Promise,
    Number,
    String,
    Object,
    document: { createElement: (tag) => new FakeElement(tag) },
    thread,
    _assistEdits: {},
    _aeCtr: 0,
    scrollDown() {},
    window: {
      _nativeReaderPageCardAction(payload) {
        calls.push(structuredClone(payload));
        return Promise.resolve(actionResult);
      },
      notesReload() { reloads.push(true); },
    },
    render: null,
    toggle: null,
  };
  context.globalThis = context;
  vm.runInNewContext(
    `${balancedFunction("_assistPageCard")}
     ${balancedFunction("_syncPageCardButtons")}
     ${balancedFunction("_pageCardEditToggle")}
     ${assignedFunction("window._assistEdit = ")}
     render = window._assistEdit;
     toggle = _pageCardEditToggle;`,
    context,
  );
  return { context, thread, calls, reloads };
}

test("共享助手实际入口在 items 守卫前接收页面卡回执并生成聊天操作卡", () => {
  const { context, thread, reloads } = harness();
  context.render({
    type: "page-card",
    op: "delete",
    native_operation_id: "npdf_" + "a".repeat(24),
    page: 12,
    number: 3,
    item: { id: "c_1111111111111111" },
  });

  assert.equal(thread.children.length, 1);
  const card = thread.children[0];
  assert.equal(card.children[0].textContent, "🗑 已删除第 3 个卡片");
  assert.equal(card.children[1].children[0].attributes["data-page"], "12");
  assert.equal(card.children[2].textContent, "↩ 撤销");
  assert.equal(Object.keys(context._assistEdits).length, 1);
  assert.equal(reloads.length, 1,
    "committed delete must reload notes so frames, rail markers and numbers update now");

  context.render({
    type: "page-card",
    op: "delete",
    native_operation_id: "npdf_" + "a".repeat(24),
    page: 12,
    number: 3,
    item: { id: "c_1111111111111111" },
  });
  assert.equal(thread.children.length, 1,
    "transport replay must not duplicate the same operation strip");
});

test("操作条只按稳定 operation id 撤销和重做，成功回执后才切换按钮", async () => {
  const { context, calls } = harness();
  const state = {
    ntype: "page-card",
    operationId: "pcard_" + "b".repeat(24),
    undone: false,
  };
  const button = new FakeElement("button");
  button.disabled = true;

  context.toggle(state, button);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(calls[0], {
    operationId: "pcard_" + "b".repeat(24),
    action: "undo",
  });
  assert.equal(state.undone, true);
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, "↪ 重做");

  button.disabled = true;
  context.toggle(state, button);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(calls[1].action, "redo");
  assert.equal(state.undone, false);
  assert.equal(button.textContent, "↩ 撤销");
});

test("共享聊天把 number=null 的未绑定卡显示为自由卡且操作卡不会定时消失", async () => {
  const { context, thread } = harness();
  context.render({
    type: "page-card",
    op: "edit",
    native_operation_id: "npdf_" + "c".repeat(24),
    page: 8,
    number: null,
    item: { id: "placement-free" },
  });

  assert.equal(thread.children.length, 1);
  assert.equal(thread.children[0].children[0].textContent, "✏️ 已修改自由卡片");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(thread.children.length, 1,
    "聊天操作卡是持久控件，不得像画面 snackbar 一样自动移除");
});

test("非法或缺失 operation id 不渲染假撤销条", () => {
  const { context, thread } = harness();
  context.render({ type: "page-card", op: "delete", number: 1, page: 3 });
  assert.equal(thread.children.length, 0);
});

test("点击代理显式把 page-card 分流到目标级撤销入口", () => {
  const click = ASSISTANT.slice(
    ASSISTANT.indexOf("var eb = e.target && e.target.closest"),
    ASSISTANT.indexOf("var btn = e.target && e.target.closest", ASSISTANT.indexOf("var eb = e.target && e.target.closest")),
  );
  assert.match(click, /st\.ntype === 'page-card'/);
  assert.match(click, /_pageCardEditToggle\(st, eb\)/);
});

test("画面 snackbar 可操作但 6.5 秒后自动消失，且层级低于设置面板", () => {
  const snack = balancedFunction("_showPageCardSnack") + balancedFunction("_armPageCardSnack");
  assert.match(snack, /_pageCardEditToggle\(st, button\)/);
  assert.match(snack, /setTimeout\([\s\S]*6500/);
  assert.match(snack, /pointerenter/);
  assert.match(snack, /focusin/);
  assert.match(ASSISTANT, /\.asst-pc-snack\{[^}]*z-index:320/);
  assert.match(ASSISTANT, /\.ams-mask\{[^}]*z-index:2147483400/);
});
