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
  const end = VOICECALL.indexOf(
    "} else {\n        throw new Error('BW_READER_REALTIME_OUTPUT_KIND_UNSUPPORTED')",
    start,
  );
  assert.notEqual(end, -1, "分支未闭合");
  return VOICECALL.slice(start, end);
}

function runBranch({
  fn = "_nativeReaderUndoLast",
  args = ["rundo_" + "a".repeat(24)],
  host = {},
} = {}) {
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

test("受信 Reader 撤销入口存在时只传入受校验的一次性编号", async () => {
  const seen = [];
  const { work, thrown } = runBranch({
    args: ["rundo_" + "b".repeat(24)],
    host: {
      _nativeReaderUndoLast: (operationId) => { seen.push(operationId); return "done"; },
    },
  });
  assert.equal(thrown, null);
  assert.equal(await work, "done");
  assert.deepEqual(seen, ["rundo_" + "b".repeat(24)]);
});

test("宿主缺少该函数时明确报错，绝不静默跳过", () => {
  const { thrown } = runBranch({
    fn: "_nativeReaderUndoLast",
    args: ["rundo_" + "a".repeat(24)],
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
    /_nativeReaderUndoLast/,
    "错误须带上具体是哪个动作，否则排查只能靠猜",
  );
});

test("facade 绕过 normalizer 也不能把任意 window 函数变成调用面", () => {
  let called = false;
  const { thrown } = runBranch({
    fn: "fetch",
    args: ["https://example.invalid/"],
    host: { fetch() { called = true; } },
  });
  assert.equal(called, false, "动态 window[fn] 会把跨进程消息升级为任意页面函数调用");
  assert.match(String(thrown && thrown.message), /BW_READER_CLIENT_ACTION_INVALID:fetch/);
  assert.doesNotMatch(
    clientActionBranch(),
    /window\s*\[\s*_caFn\s*\]|\.apply\s*\(/,
    "执行侧必须显式映射，不得动态分派",
  );
});
