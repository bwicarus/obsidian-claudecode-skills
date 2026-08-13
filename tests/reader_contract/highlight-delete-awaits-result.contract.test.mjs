// 高亮删除必须等到明确结果，界面才可以移除它。
//
// 三条旧入口（侧栏列表、编辑浮层、助手撤销）此前都会在调用删除后立刻抹掉界面：
// _hlDelete 的成功、失败、异常三条路全返回 undefined，适配层既不 return 那个
// Promise 又用 try/catch 吞掉异常，于是调用方的 `ok !== false` 一律判成成功。
// 后端明明拒绝了，行却没了，刷新之后高亮又回来 —— 这正是"删不掉"的观感。
//
// 未知（超时、断网）与明确失败一样按未删处理：不假删、不自动重试。用户点删除
// 本身就是确认，所以这里不引入第二道确认框。
import assert from "node:assert/strict";
import test from "node:test";
import vm from "node:vm";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");

const PDF_DICT = read("_server_deploy/static/pdf/reader.src/19-dict.js");
const RC_ADAPTER = read("_server_deploy/static/pdf/reader.src/27-rc-adapter.js");
const RC_HIGHLIGHT = read("_server_deploy/static/pdf/rc-highlight.js");
const RUNTIME = read("_server_deploy/static/pdf/native-local-runtime.js");

function functionSource(source, name) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `missing function ${name}`);
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    if (source[i] === "}") depth -= 1;
    if (depth === 0) return source.slice(start, i + 1);
  }
  assert.fail(`unterminated function ${name}`);
}

test("_hlDelete 对成功、失败与未知各自给出结论", () => {
  const deletion = functionSource(PDF_DICT, "_hlDelete");
  assert.match(deletion, /return true;/, "后端确认删除后必须回报成功");
  assert.equal(
    (deletion.match(/return false;/g) || []).length,
    2,
    "明确失败与未知结果都必须回报未删（各一条出口）",
  );
  // alert 会阻塞整页事件，在 iPad 上尤其难以恢复。
  assert.doesNotMatch(deletion, /\balert\s*\(/);
  // 用户点删除即是确认，不叠第二道确认框。
  assert.doesNotMatch(deletion, /\bconfirm\s*\(/);
});

test("适配层把删除结果交出去，而不是吞掉", async () => {
  const source = RC_ADAPTER;
  assert.doesNotMatch(
    source.slice(source.indexOf("hlDelete:"), source.indexOf("hlDelete:") + 400),
    /try\s*\{[\s\S]*_hlDelete[\s\S]*catch/,
    "适配层不得用 try/catch 吞掉删除异常",
  );

  // 行为：把适配层那一行放进沙箱，注入一个明确失败的底座，看它是否如实转达。
  const snippet = source.slice(source.indexOf("hlDelete:"));
  const body = snippet.slice(0, snippet.indexOf("\n    },") + 6);
  const context = {
    document: { querySelector: () => null },
    _hlDelete: () => Promise.resolve(false),
    Promise,
    out: null,
  };
  context.globalThis = context;
  vm.runInNewContext(`out = ({ ${body} }).hlDelete({ page: 1 });`, context);
  assert.equal(
    await context.out,
    false,
    "底座回报未删时，适配层必须把 false 传上去",
  );

  context._hlDelete = () => Promise.resolve(true);
  vm.runInNewContext(`out = ({ ${body} }).hlDelete({ page: 1 });`, context);
  assert.equal(await context.out, true, "确认删除时必须传上 true");
});

// 行为层面的完整覆盖在 highlight-delete-strictly-confirmed.contract.test.mjs
// （同步 throw / undefined / null / 真值对象 / 拒绝 / 缺 handler 六种形状）。
// 这里只守护一件静态事实：调用必须包在 Promise.resolve().then(...) 里。
// 写成 Promise.resolve(onDelete(...)) 会在构造 Promise 之前同步调用它，
// 同步抛出的异常就此逃出整条链，连 catch 都接不住。
test("列表删除把调用包进 Promise 链，同步异常不会逃出", () => {
  const anchor = RC_HIGHLIGHT.indexOf("rc-hl-swipe-del').onclick");
  assert.notEqual(anchor, -1, "找不到列表删除按钮的处理器");
  const clicked = RC_HIGHLIGHT.slice(anchor, anchor + 600);
  assert.match(
    clicked,
    /Promise\.resolve\(\)\s*\.then\(function \(\) \{ return opts\.onDelete\(h\); \}\)/,
    "必须先进 Promise 链再调用，否则同步 throw 逃出这条链",
  );
  assert.match(clicked, /if \(ok !== true\) return;/, "只有严格 true 才移除该行");
});


// ⚠ 这条只证明"调用处写了上界"，不证明它抵达底层。
//
// 事实上它曾在 mutateDocumentState 少一个形参、上界被静默丢弃时照样通过 —— 那正是
// 它要防的故障。真正的行为验证在 highlight-local-write.contract.test.mjs 的
// "deleting a highlight hands the bounded timeout to the store"：那条走真实 runtime，
// 断言底层收到的值，去掉形参即失败。此处保留，只用于守护调用处不被改回去。
test("普通高亮删除的调用处写明了事务上界（不代表它抵达底层）", () => {
  const deleteBlock = RUNTIME.slice(
    RUNTIME.indexOf("function localPDFHighlights("),
    RUNTIME.indexOf("function localPDFHighlights(") + 1600,
  );
  assert.match(
    deleteBlock,
    /transactionTimeoutMs: EXACT_HIGHLIGHT_IDB_TIMEOUT_MS/,
    "删除事务同样要有界，否则它会挂住后续每一次高亮读写",
  );
});
