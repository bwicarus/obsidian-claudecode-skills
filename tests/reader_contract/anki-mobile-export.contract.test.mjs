import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const require = createRequire(import.meta.url);
const AnkiMobile = require(
  "../../_server_deploy/static/reader-runtime/anki-mobile-export.js",
);
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const GID = `card_${"a".repeat(32)}`;

function group(card, phase = "confirmed", receipt = null) {
  return {
    id: GID,
    gid: GID,
    cards: [structuredClone(card)],
    states: {
      "0": {
        phase,
        projections: {
          anki: receipt ? { "ankimobile-ipad": structuredClone(receipt) } : {},
        },
      },
    },
  };
}

function harness({
  card = {
    type: "basic",
    front: "line 1\n<b>line 2</b>",
    back: "answer",
    tags: ["physics"],
  },
  opened = true,
  native = true,
  now = 1_800_000_000_000,
  sharedState = { receipt: null },
  snapshotEmpty = false,
  failSucceededWrites = 0,
} = {}) {
  const events = new Map();
  const timers = new Map();
  const receipts = [];
  const bridgeCalls = [];
  const order = [];
  let currentNow = now;
  let timerSequence = 0;
  let remainingSucceededFailures = failSucceededWrites;
  const environment = {
    __BW_NATIVE_LOCAL_READER__: native,
    crypto: {
      getRandomValues(bytes) {
        for (let index = 0; index < bytes.length; index += 1) {
          bytes[index] = index + 1;
        }
        return bytes;
      },
    },
    addEventListener(type, listener) {
      events.set(type, listener);
    },
    setTimeout(callback, delay) {
      timerSequence += 1;
      timers.set(timerSequence, { callback, due: currentNow + delay });
      return timerSequence;
    },
    clearTimeout(id) {
      timers.delete(id);
    },
    document: {
      visibilityState: "visible",
      addEventListener(type, listener) {
        events.set(`document:${type}`, listener);
      },
    },
  };
  const repository = {
    async snapshot() {
      order.push("snapshot");
      return snapshotEmpty ? [] : [group(card, "confirmed", sharedState.receipt)];
    },
    async load(gid) {
      order.push("load");
      assert.equal(gid, GID);
      return group(card, "confirmed", sharedState.receipt);
    },
    async recordAnkiReceipt(gid, index, target, receipt, options) {
      order.push(`receipt:${receipt.status}`);
      if (receipt.status === "succeeded" && remainingSucceededFailures > 0) {
        remainingSucceededFailures -= 1;
        throw new Error("simulated receipt failure");
      }
      receipts.push({ gid, index, target, receipt, options });
      sharedState.receipt = structuredClone(receipt);
      return group(card, "confirmed", sharedState.receipt);
    },
  };
  const bridge = {
    async request(payload) {
      order.push(`bridge:${payload.action}`);
      bridgeCalls.push(structuredClone(payload));
      return opened
        ? { ok: true, opened: true }
        : { ok: false, opened: false, error: "AnkiMobile 未安装" };
    },
  };
  const create = () => AnkiMobile.createAnkiMobileExport({
      root: environment,
      repository,
      bridge,
      clock: () => currentNow,
    });
  const api = create();
  return {
    api,
    environment,
    receipts,
    bridgeCalls,
    order,
    sharedState,
    async callback(detail) {
      const listener = events.get("bw-native-anki-mobile-callback");
      assert.equal(typeof listener, "function");
      listener({ detail });
      await new Promise((resolve) => setImmediate(resolve));
    },
    reload() {
      return create();
    },
    advance(milliseconds) {
      currentNow += milliseconds;
    },
    async runDueTimers() {
      const due = [...timers.entries()].filter(([, timer]) => {
        return timer.due <= currentNow;
      });
      for (const [id, timer] of due) {
        timers.delete(id);
        timer.callback();
      }
      await new Promise((resolve) => setImmediate(resolve));
    },
    async fire(type, detail = null) {
      const listener = events.get(type);
      assert.equal(typeof listener, "function");
      listener({ detail });
      await new Promise((resolve) => setImmediate(resolve));
    },
  };
}

test("Basic 先写本地 pending，再用官方 addnote URL 打开 AnkiMobile", async () => {
  const h = harness();
  assert.equal(h.api.available(), true);
  const result = await h.api.exportCard(GID, 0);

  assert.deepEqual(h.order, ["snapshot", "load", "receipt:pending", "bridge:open"]);
  assert.deepEqual(result, {
    ok: true,
    status: "pending",
    gid: GID,
    index: 0,
    callbackExpected: true,
  });
  assert.equal(h.receipts[0].target, "ankimobile-ipad");
  assert.equal(h.receipts[0].receipt.status, "pending");

  const payload = h.bridgeCalls[0];
  assert.equal(payload.action, "open");
  assert.match(payload.nonce, /^[a-f0-9]{32}$/);
  assert.equal(h.receipts[0].receipt.detail.callbackNonce, payload.nonce);
  assert.equal(
    h.receipts[0].receipt.detail.callbackExpiresAt,
    1_800_000_600_000,
  );
  assert.equal(payload.expiresAt, 1_800_000_600_000);
  assert.doesNotMatch(JSON.stringify(h.receipts[0]), /line 1|answer/);
  assert.ok(Buffer.byteLength(payload.url, "utf8") <= 32 * 1024);
  const url = new URL(payload.url);
  assert.equal(url.protocol, "anki:");
  assert.equal(url.host, "x-callback-url");
  assert.equal(url.pathname, "/addnote");
  assert.equal(url.searchParams.get("type"), "Basic");
  assert.equal(url.searchParams.get("deck"), "BW Reader");
  assert.equal(
    url.searchParams.get("fldFront"),
    "line 1<br><b>line 2</b>",
  );
  assert.equal(url.searchParams.get("fldBack"), "answer");
  const tags = new Set(url.searchParams.get("tags").split(" "));
  assert.ok(tags.has("bwreader"));
  assert.ok(tags.has(`bwgid_${GID}`));
  assert.ok(tags.has("bwindex_0"));
  assert.ok(tags.has("physics"));

  const callback = new URL(url.searchParams.get("x-success"));
  assert.equal(callback.protocol, "bwreader:");
  assert.equal(callback.host, "anki-export-success");
  assert.equal(callback.searchParams.get("gid"), GID);
  assert.equal(callback.searchParams.get("index"), "0");
  assert.equal(callback.searchParams.get("nonce"), payload.nonce);
});

test("只有 nonce+gid+index 完全匹配的同次回调才写 succeeded", async () => {
  const h = harness();
  await h.api.exportCard(GID, 0);
  const nonce = h.bridgeCalls[0].nonce;

  await h.callback({ status: "succeeded", gid: GID, index: 0, nonce: "f".repeat(32) });
  assert.deepEqual(h.receipts.map((item) => item.receipt.status), ["pending"]);

  await h.callback({ status: "succeeded", gid: GID, index: 0, nonce });
  assert.deepEqual(
    h.receipts.map((item) => item.receipt.status),
    ["pending", "succeeded"],
  );
  assert.equal(h.receipts[1].receipt.exportedAt, 1_800_000_000_000);
  assert.deepEqual(h.receipts[1].receipt.detail, {
    channel: "x-callback-url",
    callbackExpected: true,
    callbackReceived: true,
    callbackNonce: nonce,
    callbackExpiresAt: 1_800_000_600_000,
  });
});

test("Cloze 只发送 fldText；未知字段、NUL 与超长 URL fail closed", async () => {
  const cloze = harness({
    card: { type: "cloze", cloze: "The {{c1::device}} wins.\nNext." },
  });
  await cloze.api.exportCard(GID, 0);
  const url = new URL(cloze.bridgeCalls[0].url);
  assert.equal(url.searchParams.get("type"), "Cloze");
  assert.equal(url.searchParams.get("fldText"), "The {{c1::device}} wins.<br>Next.");
  assert.equal(url.searchParams.has("fldFront"), false);
  assert.equal(url.searchParams.has("fldBack"), false);

  const unknown = harness({
    card: { type: "basic", front: "q", back: "a", injected: "no" },
  });
  await assert.rejects(
    unknown.api.exportCard(GID, 0),
    (error) => error.code === "BW_ANKIMOBILE_INPUT",
  );
  assert.equal(unknown.receipts.length, 0);
  assert.equal(unknown.bridgeCalls.length, 0);

  const nul = harness({ card: { type: "basic", front: "q\0x", back: "a" } });
  await assert.rejects(
    nul.api.exportCard(GID, 0),
    (error) => error.code === "BW_ANKIMOBILE_INPUT",
  );
  assert.equal(nul.receipts.length, 0);

  const oversized = harness({
    card: { type: "basic", front: "x".repeat(40_000), back: "a" },
  });
  await assert.rejects(
    oversized.api.exportCard(GID, 0),
    (error) => error.code === "BW_ANKIMOBILE_URL_TOO_LARGE",
  );
  assert.equal(oversized.receipts.length, 0);
  assert.equal(oversized.bridgeCalls.length, 0);
});

test("打开失败写 failed；无回调保持 pending 且不盲目重复打开", async () => {
  const failed = harness({ opened: false });
  await assert.rejects(
    failed.api.exportCard(GID, 0),
    (error) => error.code === "BW_ANKIMOBILE_OPEN_FAILED",
  );
  assert.deepEqual(
    failed.receipts.map((item) => item.receipt.status),
    ["pending", "failed"],
  );

  const pending = harness();
  await pending.api.exportCard(GID, 0);
  await assert.rejects(
    pending.api.exportCard(GID, 0),
    (error) => error.code === "BW_ANKIMOBILE_PENDING",
  );
  assert.equal(pending.bridgeCalls.length, 1);
  assert.deepEqual(
    pending.receipts.map((item) => item.receipt.status),
    ["pending"],
  );
});

test("JS reload 从本地仓 receipt 恢复 nonce，并在无内存条目时严格处理回调", async () => {
  const h = harness({ snapshotEmpty: true });
  await h.api.exportCard(GID, 0);
  const nonce = h.bridgeCalls[0].nonce;
  const reloaded = h.reload();

  assert.deepEqual(
    await reloaded.handleNativeCallback({
      status: "succeeded",
      gid: GID,
      index: 0,
      nonce: "f".repeat(32),
    }),
    { ok: false, durable: false },
  );
  assert.equal(h.sharedState.receipt.status, "pending");

  assert.deepEqual(
    await reloaded.handleNativeCallback({
      status: "succeeded",
      gid: GID,
      index: 0,
      nonce,
    }),
    { ok: true, durable: true },
  );
  assert.equal(h.sharedState.receipt.status, "succeeded");
  assert.equal(h.sharedState.receipt.detail.callbackNonce, nonce);
});

test("实际 timer 在无再次 export 时把超期 pending 转为 unknown", async () => {
  const h = harness();
  await h.api.exportCard(GID, 0);
  h.advance(10 * 60 * 1000 + 1);
  await h.runDueTimers();

  assert.equal(h.sharedState.receipt.status, "unknown");
  assert.equal(h.sharedState.receipt.detail.reason, "callback-expired");
  assert.equal(h.bridgeCalls.length, 1);
});

test("前台恢复事件也会立即结算已经超期的持久 pending", async () => {
  const h = harness();
  await h.api.exportCard(GID, 0);
  h.advance(10 * 60 * 1000 + 1);
  await h.fire("bw-native-reader-foreground", { active: true });
  assert.equal(h.sharedState.receipt.status, "unknown");
});

test("visibility 恢复也会立即结算已经超期的持久 pending", async () => {
  const h = harness();
  await h.api.exportCard(GID, 0);
  h.advance(10 * 60 * 1000 + 1);
  await h.fire("document:visibilitychange");
  assert.equal(h.sharedState.receipt.status, "unknown");
});

test("JS 只有本地 succeeded receipt 耐久后才返回 durable ack，失败可重试", async () => {
  const h = harness({ failSucceededWrites: 1 });
  await h.api.exportCard(GID, 0);
  const detail = {
    status: "succeeded",
    gid: GID,
    index: 0,
    nonce: h.bridgeCalls[0].nonce,
  };

  assert.deepEqual(await h.api.handleNativeCallback(detail), {
    ok: false,
    durable: false,
  });
  assert.equal(h.sharedState.receipt.status, "pending");
  assert.deepEqual(await h.api.handleNativeCallback(detail), {
    ok: true,
    durable: true,
  });
  assert.equal(h.sharedState.receipt.status, "succeeded");

  const reloaded = h.reload();
  assert.deepEqual(await reloaded.handleNativeCallback(detail), {
    ok: true,
    durable: true,
  });
});

test("requestSync 只返回 requested，非 App 环境不可用", async () => {
  const h = harness();
  assert.deepEqual(await h.api.requestSync(), { ok: true, status: "requested" });
  assert.deepEqual(h.bridgeCalls[0], {
    action: "sync",
    url: "anki://x-callback-url/sync",
  });

  const web = harness({ native: false });
  assert.equal(web.api.available(), false);
  await assert.rejects(
    web.api.exportCard(GID, 0),
    (error) => error.code === "BW_ANKIMOBILE_UNAVAILABLE",
  );
});

test("原生桥持久化 pending，且只在 JS durable ack 后删除", () => {
  const webView = fs.readFileSync(
    path.join(ROOT, "ios/BWReader/App/ReaderWebView.swift"),
    "utf8",
  );
  const app = fs.readFileSync(
    path.join(ROOT, "ios/BWReader/App/BWReaderNativeApp.swift"),
    "utf8",
  );
  assert.match(webView, /nativeAnkiMobileMessageName = "bwNativeAnkiMobile"/);
  assert.match(webView, /message\.frameInfo\.isMainFrame[\s\S]*message\.webView === webView[\s\S]*isTrustedReaderURL/);
  assert.match(webView, /"action", "gid", "index", "nonce", "expiresAt", "url"/);
  assert.match(webView, /rawURL\.utf8\.count <= 32 \* 1024/);
  assert.match(webView, /pending\.gid == gid[\s\S]*pending\.index == index/);
  assert.match(webView, /\$0\.value\.documentIdentity == documentIdentity/);
  assert.match(webView, /reader\.ankiMobile\.pending\.v1/);
  assert.match(webView, /Set\(row\.keys\) == Set\(\[[\s\S]*"callbackReceived"/);
  assert.match(webView, /super\.init\(\)[\s\S]*restorePendingAnkiMobileExports\(\)/);
  assert.match(webView, /func handleAnkiMobileCallback[\s\S]*restorePendingAnkiMobileExports\(\)/);
  assert.match(webView, /persistPendingAnkiMobileExports\(\)[\s\S]*UIApplication\.shared\.open/);
  assert.match(webView, /return await api\.handleNativeCallback\(detail\)/);
  assert.match(webView, /ack\["durable"\] as\? Bool == true[\s\S]*if durable \{[\s\S]*removeValue/);
  assert.doesNotMatch(
    webView,
    /window\.dispatchEvent\(new CustomEvent\([\s\S]{0,120}"bw-native-anki-mobile-callback"/,
  );
  const storedRecord = webView.match(
    /private struct ReaderAnkiMobilePendingRecord \{([\s\S]*?)\n\}/,
  )?.[1] || "";
  assert.doesNotMatch(storedRecord, /let (front|back|cloze|url|token)\b/i);
  assert.match(app, /if reader\.handleAnkiMobileCallback\(url\) \{[\s\S]*return/);
});
