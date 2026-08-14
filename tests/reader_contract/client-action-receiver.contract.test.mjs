// 桥接下发的可见反馈，必须真的执行得到。
//
// 通道原本是"两端都通、中间断着"：normalizer 认了 kind=client-action，而真正执行的
// _acceptReaderRealtimeOutput 没有对应分支，于是一路走到
// BW_READER_REALTIME_OUTPUT_KIND_UNSUPPORTED，永远到不了 window[fn]。
// 校验与执行是两件事；只补前者，通道看起来通了，实际一步也没走。
//
// 另一条同样要紧：runActions 找不到函数时会静默跳过。EPUB 宿主根本没有
// window._nativePDFUndoLast —— 用户说了"撤销"，界面什么都不做、也没有任何提示。
// 所以这里在调用前先自检一次，把"这个宿主做不到"变成一句能看见的错误。
import assert from "node:assert/strict";
import test from "node:test";
import vm from "node:vm";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const VOICECALL = readFileSync(
  new URL("_server_deploy/static/pdf/rc-voicecall.js", ROOT),
  "utf8",
);

// 取出 client-action 分支体，放进沙箱执行。
function clientActionBranch() {
  const start = VOICECALL.indexOf("} else if (delivery.kind === 'client-action') {");
  assert.notEqual(start, -1, "执行侧没有 client-action 分支");
  const end = VOICECALL.indexOf("} else {", start);
  assert.notEqual(end, -1, "分支未闭合");
  return VOICECALL.slice(start, end);
}

function runBranch({ fn = "_assistEdit", args = [{ type: "highlight" }], host = {} } = {}) {
  const branch = clientActionBranch();
  const context = {
    window: host,
    delivery: { kind: "client-action" },
    p: { fn, args },
    Promise,
    Array,
    String,
    work: null,
    thrown: null,
  };
  context.globalThis = context;
  // 分支体以 "} else if (...) {" 开头，补一个未闭合的 if 给它接上。
  vm.runInNewContext(
    `try { if (false) { ${branch} } } catch (error) { thrown = error; }`,
    context,
  );
  return { work: context.work, thrown: context.thrown };
}

test("执行侧确实有 client-action 分支，不再落到不支持", () => {
  assert.doesNotMatch(
    clientActionBranch(),
    /BW_READER_REALTIME_OUTPUT_KIND_UNSUPPORTED/,
    "这条 kind 必须被处理，而不是走到不支持分支",
  );
});

test("宿主具备该函数时按同一语义调用，参数原样传入", async () => {
  const seen = [];
  const { work, thrown } = runBranch({
    fn: "_assistEdit",
    args: [{ type: "highlight", file: "book.pdf" }],
    host: { _assistEdit: (payload) => { seen.push(payload); return "done"; } },
  });
  assert.equal(thrown, null);
  assert.equal(await work, "done");
  assert.equal(seen.length, 1);
  assert.equal(seen[0].file, "book.pdf", "参数须原样送达，与 Pi 路径一致");
});

test("宿主缺少该函数时明确报错，绝不静默跳过", () => {
  // EPUB 宿主没有 window._nativePDFUndoLast，这正是要防的情形。
  const { thrown } = runBranch({
    fn: "_nativePDFUndoLast",
    args: ["npdf_" + "a".repeat(24)],
    host: {},
  });
  assert.notEqual(thrown, null, "静默跳过等于用户说了撤销却什么都没发生");
  assert.match(
    String(thrown.message),
    /BW_READER_CLIENT_ACTION_UNAVAILABLE/,
    "错误须指明是客户端动作不可用",
  );
  assert.match(
    String(thrown.message),
    /_nativePDFUndoLast/,
    "错误须带上具体是哪个动作，否则排查只能靠猜",
  );
});

test("不转调未导出的 runActions", () => {
  // runActions 是 rc-assistant.js 的局部函数，从未挂到 RC 上。
  // 写一个永远不成立的分支只会误导下一个人。
  assert.doesNotMatch(
    clientActionBranch(),
    /RC\.assistant\.runActions/,
    "该分派器未导出，不能假装可以转调",
  );
});
