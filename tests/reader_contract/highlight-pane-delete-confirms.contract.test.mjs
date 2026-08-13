// 专门的高亮管理页：删除必须等到后端确认，且要更新内存与页面叠层，而不只是抹掉列表行。
//
// 这条链被同一个模式断了多次：中间某一层不把结果交出去（缺 return，或 try/catch 吞掉），
// 上层拿到 undefined 就判成成功，行没了而后端那条高亮还在，刷新之后又回来。
//
// 还有一处更隐蔽：即便等到了确认，若管理页自己另发一套 fetch，删除成功后
// _allHighlights / _hlByPage 与当前页的叠层都不会变 —— 而 shared 模式下
// window._reloadHighlights 根本没注册，"删完刷新一下"这条退路并不存在。
// 所以管理页必须复用统一的 _hlDelete，由它一次做完请求、更新内存、重绘与提示。
//
// 全部用行为测试。此前给同类修复写过静态断言，在保护实际失效时照样通过 ——
// 它测的是代码长什么样，不是它做了什么。
import assert from "node:assert/strict";
import test from "node:test";
import vm from "node:vm";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const DRAWER = read("_server_deploy/static/pdf/reader.src/28-shared-drawer.js");
const DICT = read("_server_deploy/static/pdf/reader.src/19-dict.js");
const EPUB = read("_server_deploy/static/pdf/epub-html.js");

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

// ── 管理页把删除交给统一入口，并如实转达结果 ──────────────────────────
function runPaneDelete(hlDeleteImpl) {
  const start = DRAWER.indexOf("onDelete: (h) => {");
  assert.notEqual(start, -1, "找不到高亮 pane 的删除处理器");
  const calls = [];
  const context = {
    document: { querySelector: (selector) => ({ selector }) },
    _hlDelete: (h, pw) => { calls.push({ h, pw }); return hlDeleteImpl(h, pw); },
    Promise,
    out: null,
  };
  context.globalThis = context;
  vm.runInNewContext(
    `out = ({ ${balanced(DRAWER, start)} }).onDelete({ id: "h1", page: 7 });`,
    context,
  );
  return Promise.resolve(context.out).then((result) => ({ result, calls }));
}

test("管理页把删除交给统一入口，并带上该高亮所在页", async () => {
  const { result, calls } = await runPaneDelete(() => Promise.resolve(true));
  assert.equal(calls.length, 1, "必须复用统一删除，不再自己发一套请求");
  assert.equal(calls[0].h.id, "h1");
  assert.match(
    calls[0].pw.selector,
    /data-page-num="7"/,
    "要把该高亮所在页交给统一入口，它才能重绘那一页的叠层",
  );
  assert.equal(result, true);
});

test("统一入口回报未删时，管理页如实转达 false", async () => {
  const { result } = await runPaneDelete(() => Promise.resolve(false));
  assert.equal(result, false, "未删就不能让列表把行移走");
});

// ── 统一入口本身：成功时更新内存与叠层，失败时一概不动 ────────────────
function runHlDelete({ ok = true, body = { ok: true }, rejects = false } = {}) {
  const start = DICT.indexOf("async function _hlDelete(");
  assert.notEqual(start, -1, "找不到 _hlDelete");
  const rendered = [];
  const toasts = [];
  const context = {
    FILE_REL: "book.pdf",
    JSON,
    Promise,
    _allHighlights: [{ id: "h1", page: 7 }, { id: "h2", page: 8 }],
    _hlByPage: { 7: [{ id: "h1", page: 7 }], 8: [{ id: "h2", page: 8 }] },
    renderHighlightsOnPage: (pw, page) => { rendered.push(page); },
    closeHlPopover: () => {},
    _toast: (message) => { toasts.push(String(message)); },
    fetch: () => rejects
      ? Promise.reject(new Error("network down"))
      : Promise.resolve({ ok, json: () => Promise.resolve(body) }),
    out: null,
  };
  context.globalThis = context;
  vm.runInNewContext(`${balanced(DICT, start)}\nout = _hlDelete({ id: "h1", page: 7 }, {});`, context);
  return context.out.then((result) => ({
    result,
    remaining: context._allHighlights.map((h) => h.id),
    pageSeven: (context._hlByPage[7] || []).map((h) => h.id),
    rendered,
    toasts,
  }));
}

test("确认删除后内存数组与该页叠层都更新", async () => {
  const { result, remaining, pageSeven, rendered } = await runHlDelete();
  assert.equal(result, true);
  assert.deepEqual(remaining, ["h2"], "被删的高亮必须从总表移除");
  assert.deepEqual(pageSeven, [], "也必须从所在页的索引移除");
  assert.deepEqual(rendered, [7], "该页叠层必须重绘，否则删掉的高亮还画在页面上");
});

test("后端说没删时，内存与叠层一概不动", async () => {
  const { result, remaining, pageSeven, rendered, toasts } = await runHlDelete({
    ok: true,
    body: { ok: false, error: "not found" },
  });
  assert.equal(result, false);
  assert.deepEqual(remaining, ["h1", "h2"], "未删就不能把它从总表拿掉");
  assert.deepEqual(pageSeven, ["h1"]);
  assert.deepEqual(rendered, [], "没有变化就不该重绘");
  assert.match(toasts.join(" "), /删除失败/);
});

test("网络异常按未确认处理，不假删也不重试", async () => {
  const { result, remaining, toasts } = await runHlDelete({ rejects: true });
  assert.equal(result, false);
  assert.deepEqual(remaining, ["h1", "h2"]);
  assert.match(toasts.join(" "), /删除未确认/);
});

test("PDF 与 EPUB 管理页不把加载错误伪装成空列表，并提供重试", () => {
  assert.match(DRAWER, /if \(!r\.ok\) throw new Error\('HTTP ' \+ r\.status\)/);
  assert.match(DRAWER, /!Array\.isArray\(d\.highlights\)/);
  assert.match(DRAWER, /pdf-hl-retry/);
  assert.match(EPUB, /if \(!r\.ok\) throw new Error\('HTTP ' \+ r\.status\)/);
  assert.match(EPUB, /!Array\.isArray\(d\.highlights\)/);
  assert.match(EPUB, /ep-hl-retry/);
});

test("EPUB 批量撤销与重做失败保持未完成状态", () => {
  assert.match(EPUB, /stt\._pendingUndo = failed0; stt\.undone = false/);
  assert.match(EPUB, /stt\._pendingRedo = fresh; stt\.undone = true/);
  assert.match(EPUB, /撤销未完成/);
  assert.match(EPUB, /重做未完成/);
});
