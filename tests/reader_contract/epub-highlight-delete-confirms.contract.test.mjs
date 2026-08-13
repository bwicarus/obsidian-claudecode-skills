// EPUB 删除高亮同样必须给出明确结论，并在未确认时原样保留。
//
// 与 PDF 是同一个洞：成功回调做事、失败回调是空函数，整个函数返回 undefined，
// 于是调用方的 `ok !== false` 判成成功——编辑框关掉、列表移除，而这条高亮还在。
// reqJson 的失败回调本来就带着错误字符串，此前被丢弃了。
//
// 这里也不再用"undefined 代表成功"来兼容旧底座：那等于让"没告诉我"继续算成功，
// 正是这条链反复假删的根源。
import assert from "node:assert/strict";
import test from "node:test";
import vm from "node:vm";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const EPUB = readFileSync(
  new URL("_server_deploy/static/pdf/epub-html.js", ROOT),
  "utf8",
);

function balanced(source, start) {
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    if (source[i] === "}") depth -= 1;
    if (depth === 0) return source.slice(start, i + 1);
  }
  assert.fail("括号未闭合");
}

function runPatch({ outcome = "ok", error = "write timed out" } = {}) {
  const start = EPUB.indexOf("function patchHl(h, f) {");
  assert.notEqual(start, -1, "找不到 patchHl");
  const toasts = [];
  const unapplied = [];
  const context = {
    FREL: "book.epub",
    Promise,
    _secElOf: () => ({}),
    applyHl: () => {},
    unapplyHl: (h) => { unapplied.push(h.id); },
    toast: (message) => { toasts.push(String(message)); },
    reqJson: (method, url, body, ok, fail) => {
      if (outcome === "ok") ok({ highlight: { color: "", note: "kept" } });
      else fail(error);
    },
    h: { id: "h1", color: "#ffd54a", note: "kept", anchor: { section: 1 } },
    out: null,
  };
  context.globalThis = context;
  vm.runInNewContext(
    `${balanced(EPUB, start)}\nout = patchHl(h, { color: "" });`,
    context,
  );
  return context.out.then((result) => ({ result, h: context.h, toasts, unapplied }));
}

// 在沙箱里跑 delHl，用可编排的 reqJson 替代真实网络。
function runDelete({ outcome = "ok", error = "server said no" } = {}) {
  const start = EPUB.indexOf("function delHl(h) {");
  assert.notEqual(start, -1, "找不到 delHl");
  const unapplied = [];
  const toasts = [];
  const context = {
    FREL: "book.epub",
    encodeURIComponent,
    Promise,
    _hls: { h1: { id: "h1" }, h2: { id: "h2" } },
    unapplyHl: (h) => { unapplied.push(h.id); },
    toast: (message) => { toasts.push(String(message)); },
    reqJson: (method, url, body, ok, err) => {
      if (outcome === "ok") ok({ ok: true });
      else err(error);
    },
    out: null,
  };
  context.globalThis = context;
  vm.runInNewContext(
    `${balanced(EPUB, start)}\nout = delHl({ id: "h1" });`,
    context,
  );
  return context.out.then((result) => ({
    result,
    remaining: Object.keys(context._hls),
    unapplied,
    toasts,
  }));
}

test("确认删除后回报成功，并从内存与页面上移除", async () => {
  const { result, remaining, unapplied, toasts } = await runDelete();
  assert.equal(result, true, "后端确认删除必须回报 true");
  assert.deepEqual(remaining, ["h2"], "被删的高亮要从 _hls 移除");
  assert.deepEqual(unapplied, ["h1"], "页面上的标记也要撤掉");
  assert.match(toasts.join(" "), /已删除/);
});

test("后端拒绝时回报未删，内存与页面一概不动", async () => {
  const { result, remaining, unapplied, toasts } = await runDelete({
    outcome: "error",
    error: "not found",
  });
  assert.equal(result, false, "未删必须回报 false，否则列表会假删");
  assert.deepEqual(remaining, ["h1", "h2"], "没删掉就不能从 _hls 拿走");
  assert.deepEqual(unapplied, [], "页面标记必须留着");
  assert.match(toasts.join(" "), /删除失败/);
  assert.match(toasts.join(" "), /not found/, "失败原因不该被吞掉");
});

test("网络异常按未删处理", async () => {
  const { result, remaining, toasts } = await runDelete({
    outcome: "error",
    error: "网络错误",
  });
  assert.equal(result, false);
  assert.deepEqual(remaining, ["h1", "h2"]);
  assert.match(toasts.join(" "), /删除失败/);
});

test("两个删除入口都把结果交出去", () => {
  // 编辑浮层与列表各有一处 onDelete；缺 return 就等于把结论丢掉。
  assert.match(EPUB, /onDelete: function \(\) \{ return delHl\(h\); \}/);
  assert.match(EPUB, /onDelete: function \(h\) \{ return delHl\(h\); \}/);
});

test("取消颜色只有后端确认后才回报成功并更新页面", async () => {
  const { result, h, unapplied } = await runPatch();
  assert.equal(result, true);
  assert.equal(h.color, "");
  assert.deepEqual(unapplied, ["h1"]);
});

test("取消颜色写入失败时保留原色并给出原因", async () => {
  const { result, h, toasts, unapplied } = await runPatch({ outcome: "error" });
  assert.equal(result, false);
  assert.equal(h.color, "#ffd54a");
  assert.deepEqual(unapplied, []);
  assert.match(toasts.join(" "), /保存失败/);
  assert.match(toasts.join(" "), /write timed out/);
});

test("EPUB 编辑器把改色与备注的 Promise 结果交给共享浮层", () => {
  assert.match(EPUB, /onColor: function \(c\) \{ return patchHl\(h, \{ color: c \}\); \}/);
  assert.match(EPUB, /onNote: function \(t\) \{ return patchHl\(h, \{ note: t \}\); \}/);
});
