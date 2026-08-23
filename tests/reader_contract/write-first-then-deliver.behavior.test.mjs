import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// 「写入先落地，再尝试送达」（2026-08-23）。
//
// 用户原话：「就算当时没有连通也应该更新 windows 和 pi 本地的文件，在 app
// 联通时自动进行内容更新不是么，不然我们的 Windows 和 pi 本地化的文件还有
// 传输链路就没有被利用上」。
//
// 原来的顺序是"先等租约，等不到才排队"，于是**拿到租约之后**的失败一律丢：
// 发送时租约没了 / 20 秒回执超时 / 页面回 rejected。而"网络抖一下、连接刚断"
// 恰恰发生在拿到租约之后，正好落在覆盖不到的洞里。

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8");

const OUTPUT = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderRealtimeOutput.cs");
const VOICECALL = read("_server_deploy/static/pdf/rc-voicecall.js");

function sendImplBody() {
  const start = OUTPUT.indexOf(
    "private async Task<ReaderRealtimeOutputAck> SendAsync(");
  assert.ok(start > 0, "找不到 SendAsync 的实现体");
  const end = OUTPUT.indexOf("\n    private", start + 10);
  return OUTPUT.slice(start, end > start ? end : start + 8000);
}

test("入队发生在等租约**之前**，不是等不到才排队", () => {
  const body = sendImplBody();
  const iEnqueue = body.indexOf("_outbox!.EnqueueAsync(");
  const iWait = body.indexOf("await WaitForSourceAsync(");
  assert.ok(iEnqueue > 0, "没有先入队 —— 拿到租约后的失败又会丢");
  assert.ok(iWait > 0);
  assert.ok(
    iEnqueue < iWait,
    "入队排在等租约之后 —— 那就还是老行为：拿到租约再失败就丢了");
});

test("防重入：重放路径不能再入队一次", () => {
  // 重放循环里就是 await SendAsync(entry.Request, ...)。若它再走一次入队，
  // 就会在遍历队列的同时改写队列。
  const body = sendImplBody();
  assert.match(body, /if \(durable && !alreadyQueued\)/,
    "重放会二次入队");
  assert.match(
    OUTPUT,
    /await SendAsync\(\s*\n\s*replay,\s*\n\s*CancellationToken\.None,\s*\n\s*alreadyQueued: true\)/,
    "重放路径没有传 alreadyQueued: true");
});

test("送达确知结果后销账，但**重放路径不在这里销**", () => {
  const body = sendImplBody();
  assert.match(body, /await SettleOutboxAsync\(request, durable, alreadyQueued\)/);
  // ⚠ 这一条是踩过的坑：第一版销账在重放路径上也执行，会抢在重放循环
  //   按 bindOutcome 判断之前把条目标成 applied，绕过"绑定没落实要留在队列里"
  //   那条判断 —— 打包自检表现为**整体超时**，不是任何一条断言失败，最难查。
  const settle = OUTPUT.slice(
    OUTPUT.indexOf("private async Task SettleOutboxAsync("),
    OUTPUT.indexOf("internal Task<ReaderRealtimeOutputAck> SendAsync("));
  assert.ok(settle.length > 0, "找不到 SettleOutboxAsync");
  assert.match(settle, /if \(!durable \|\| alreadyQueued \|\| _outbox is null\)/,
    "销账没有排除重放路径 —— 会绕过重放循环的 bindOutcome 判断，表现为自检超时");
  // 结果未知（超时/租约中途没了）时不能销账 —— 那正是队列存在的意义
  assert.doesNotMatch(
    body.slice(body.indexOf("if (winner == lease.LeaseRetired)")),
    /SettleOutboxAsync/,
    "结果未知时销了账 —— 那条写入就真的丢了");
});

test("接收端按 cid 幂等，重放不会多出一张卡", () => {
  // 这是"队列能覆盖浮层卡"的前提。原来 IsDurableMutation 的注释里排除浮层卡
  // 的理由就是「replaying them could duplicate」—— 在接收端幂等之前那是真的：
  // 全文件没有任何一处在建卡前查过"这个 cid 已经存在了吗"。
  const i = VOICECALL.indexOf("function _cardPush(");
  assert.ok(i > 0, "_cardPush 改名了？");
  const body = VOICECALL.slice(i, i + 3000);
  assert.match(body, /if \(cid\) \{/, "没有按 cid 去重");
  assert.match(body, /dataset\.vcCid === String\(cid\)/);
  assert.match(body, /_cardClose\(_prior\)/, "找到同 cid 的旧卡后没替换掉");
  assert.match(body, /同 cid 重放，替换既有卡而不是新建/, "替换时没留痕");
  // ⚠ 只在调用方明确给了 cid 时去重；没给 cid 的是新卡，不能拿内容当身份
  assert.match(body, /var _cid = cid \|\| _mkCid\(\);/);
});
