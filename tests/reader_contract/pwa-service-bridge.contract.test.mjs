import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SOURCE = readFileSync(
  new URL("../../_server_deploy/static/reader-runtime/pwa-service-bridge.js", import.meta.url),
  "utf8",
);
const ORIGIN = "https://bwicarus.taile44d0c.ts.net";
const NAMESPACE = `acct-v1-${"a".repeat(64)}`;
const TICKET = `pvt-v2-4102444800-${"b".repeat(32)}-${"c".repeat(64)}`;
const OWNER_CLAIM = {
  contract: "pwa-extension-owner-claim/1",
  runtimeContract: "pwa-runtime/1",
  hostContract: "document-host/1",
  hostKind: "pdf",
  markerObserved: true,
  documentLifetime: true,
  pwaServerOwner: "paused",
  pwaDirectOwner: "paused",
  syncContract: "sync-v3",
  syncChangeContract: "record-parent-state/1",
  registryDigest:
    "sync-v3:record-parent-state/1|user-settings:explicit:0:1",
};

function harness({ marker = true, fakeTimers = false } = {}) {
  const windowListeners = new Map();
  const documentListeners = new Map();
  const posted = [];
  const events = [];
  const timers = [];
  let timerSequence = 0;
  const add = (map, name, listener) => {
    if (!map.has(name)) map.set(name, []);
    map.get(name).push(listener);
  };
  class CustomEvent {
    constructor(type, options = {}) {
      this.type = type;
      this.detail = options.detail;
    }
  }
  const setTimer = fakeTimers
    ? (callback, delay) => {
        const timer = {
          id: ++timerSequence,
          callback,
          delay,
          cancelled: false,
          fired: false,
        };
        timers.push(timer);
        return timer.id;
      }
    : setTimeout;
  const clearTimer = fakeTimers
    ? (id) => {
        const timer = timers.find((entry) => entry.id === id);
        if (timer) timer.cancelled = true;
      }
    : clearTimeout;
  const sandbox = {
    console,
    CustomEvent,
    setTimeout: setTimer,
    clearTimeout: clearTimer,
    location: { origin: ORIGIN, pathname: "/pdf/view" },
    document: {
      documentElement: {
        dataset: marker ? { bwReaderExtensionProvider: "test" } : {},
      },
      addEventListener(name, listener) {
        add(documentListeners, name, listener);
      },
      dispatchEvent(event) {
        events.push(event);
        for (const listener of documentListeners.get(event.type) || []) listener(event);
        return true;
      },
    },
    addEventListener(name, listener) {
      add(windowListeners, name, listener);
    },
    postMessage(message, targetOrigin) {
      posted.push({ message, targetOrigin });
    },
  };
  const context = vm.createContext(sandbox);
  const root = vm.runInContext("globalThis", context);
  vm.runInContext(SOURCE, context, { filename: "pwa-service-bridge.js" });
  const emitMessage = (message) => {
    for (const listener of windowListeners.get("message") || []) {
      listener({ source: root, origin: ORIGIN, data: message });
    }
  };
  return {
    api: root.BWReaderRuntime.extensionProvider,
    posted,
    events,
    timers,
    emitMessage,
    runNextTimer() {
      const timer = timers.find((entry) => !entry.cancelled && !entry.fired);
      if (!timer) return null;
      timer.fired = true;
      timer.callback();
      return timer;
    },
  };
}

function toPage(type, payload, id = null) {
  return {
    protocol: "bw-reader-services/1",
    direction: "extension-to-page",
    type,
    id,
    payload,
  };
}

test("PWA bridge 用带编号握手连接 provider，并转发完整 DataStore 调用", async () => {
  const h = harness();
  assert.equal(h.api.start({
    namespace: NAMESPACE,
    ticket: TICKET,
    helloPayload: {
      syncOwnerClaim: OWNER_CLAIM,
    },
  }), true);
  const hello = h.posted.at(-1).message;
  assert.equal(hello.type, "HELLO");
  assert.match(hello.id, /^h/);
  assert.equal(hello.payload.namespace, NAMESPACE);
  assert.equal(hello.payload.ticket, TICKET);
  assert.deepEqual(
    JSON.parse(JSON.stringify(hello.payload.syncOwnerClaim)),
    OWNER_CLAIM,
  );

  h.emitMessage(toPage("READY", {
    version: "test",
    capabilities: { dataStore: true },
  }, hello.id));
  const provider = h.api.current();
  assert.equal(provider.contract, "bw-reader-services/1");
  assert.equal(provider.dataStore.contract, "data-store/1");

  const pending = provider.dataStore.put(
    "user-settings",
    { id: "setting:test", rawValue: "1" },
    { mutationId: "put-setting-test" },
  );
  const call = h.posted.at(-1).message;
  assert.equal(call.type, "CALL");
  assert.equal(call.payload.operation, "put");
  h.emitMessage(toPage("RESULT", {
    ok: true,
    result: { id: "setting:test", rev: 1 },
  }, call.id));
  assert.equal(
    JSON.stringify(await pending),
    JSON.stringify({ id: "setting:test", rev: 1 }),
  );
});

test("READY 后 syncControl 转发状态与幂等 syncNow，但不暴露冲突裁决", async (t) => {
  const h = harness();
  t.after(() => h.api.disconnect("test-complete"));
  h.api.start({ namespace: NAMESPACE, ticket: TICKET });
  const hello = h.posted.at(-1).message;
  h.emitMessage(toPage("READY", {
    version: "test",
    capabilities: { dataStore: true, serverSync: true },
  }, hello.id));
  const control = h.api.current().syncControl;
  assert.equal(control.contract, "sync-conflict-control/1");

  const pendingStatus = control.status();
  const statusCall = h.posted.at(-1).message;
  assert.equal(statusCall.type, "CALL");
  assert.equal(statusCall.payload.operation, "syncStatus");
  assert.equal(JSON.stringify(statusCall.payload.args), "{}");
  const status = {
    contract: "sync-conflict-control/1",
    owner: "extension-background",
    state: "blocked",
    at: 1,
    conflictCount: 1,
    truncated: false,
    conflicts: [{
      lane: "server",
      collection: "user-settings",
      id: "theme",
      reason: "revision-conflict",
      incomingRev: 2,
      currentRev: 1,
    }],
  };
  h.emitMessage(toPage("RESULT", {
    ok: true,
    result: status,
  }, statusCall.id));
  assert.equal(JSON.stringify(await pendingStatus), JSON.stringify(status));
  const request = {
    contract: "reader-pi-sync-request/1",
    requestId: "native-sync-1",
  };
  const pendingSync = control.syncNow(request);
  const syncCall = h.posted.at(-1).message;
  assert.equal(syncCall.type, "CALL");
  assert.equal(syncCall.payload.operation, "syncNow");
  assert.deepEqual(syncCall.payload.args, request);
  const syncResult = {
    contract: "reader-pi-data-sync-result/1",
    requestId: request.requestId,
    owner: "extension-background",
    state: "complete",
  };
  h.emitMessage(toPage("RESULT", {
    ok: true,
    result: syncResult,
  }, syncCall.id));
  assert.equal(JSON.stringify(await pendingSync), JSON.stringify(syncResult));
  assert.equal("resolveConflict" in control, false);
  assert.equal("retryAfterResolution" in control, false);
});

test("syncStatus 超时只报告状态读取失败，不断开 provider 或启动自动重连", async () => {
  const h = harness({ fakeTimers: true });
  h.api.start({
    namespace: NAMESPACE,
    ticket: TICKET,
    requestTimeoutMs: 10,
    retryBaseMs: 5,
  });
  const hello = h.posted.at(-1).message;
  h.emitMessage(toPage("READY", { version: "test" }, hello.id));
  const provider = h.api.current();
  const pending = provider.syncControl.status();
  const rejection = assert.rejects(pending, (error) => {
    assert.equal(error.code, "BW_PROVIDER_TIMEOUT");
    assert.equal(error.outcomeUnknown, undefined);
    return true;
  });
  const timeout = h.runNextTimer();
  assert.equal(timeout.delay, 10);
  await rejection;

  assert.equal(h.api.current(), provider);
  assert.equal(
    h.events.some((event) =>
      event.type === "bw:extension-provider-unhealthy" &&
      event.detail.operation === "syncStatus" &&
      event.detail.outcome === "not-received"
    ),
    true,
  );
  assert.equal(
    h.events.some(
      (event) => event.type === "bw:extension-provider-disconnected",
    ),
    false,
  );
  assert.equal(
    h.timers.some((timer) =>
      !timer.cancelled &&
      !timer.fired &&
      (timer.delay === 5 || timer.delay === 10)
    ),
    false,
    "状态读取超时不能安排 provider 重连",
  );

  const secondStatus = provider.syncControl.status();
  const secondCall = h.posted.at(-1).message;
  assert.equal(secondCall.payload.operation, "syncStatus");
  h.emitMessage(toPage("RESULT", {
    ok: true,
    result: {
      contract: "sync-conflict-control/1",
      owner: "extension-background",
      state: "ready",
      at: 2,
      conflictCount: 0,
      truncated: false,
      conflicts: [],
    },
  }, secondCall.id));
  assert.equal((await secondStatus).state, "ready");
});

test("PWA bridge 的 subscribe 收到完整 change，断线会拒绝未完成调用", async () => {
  const h = harness();
  h.api.start({ namespace: NAMESPACE, ticket: TICKET });
  const hello = h.posted.at(-1).message;
  h.emitMessage(toPage("READY", { version: "test" }, hello.id));
  const store = h.api.current().dataStore;
  const received = [];
  store.subscribe({ collection: "user-settings" }, (change) => received.push(change));
  const change = {
    cursor: 7,
    mutationId: "m7",
    operation: "put",
    collection: "user-settings",
    record: { id: "setting:test", rev: 1 },
  };
  h.emitMessage(toPage("CHANGE", change));
  assert.equal(JSON.stringify(received), JSON.stringify([change]));

  const pending = store.get("user-settings", "setting:test");
  h.emitMessage(toPage("DISCONNECTED", { reason: "worker-restart" }));
  await assert.rejects(pending, (error) => error.code === "BW_PROVIDER_DISCONNECTED");
  assert.equal(h.api.current(), null);
  h.api.disconnect("test-complete");
});

test("已授权 port 的票据到期会断开 provider 并发布不可自动重试的授权错误", async () => {
  const h = harness();
  h.api.start({ namespace: NAMESPACE, ticket: TICKET });
  const hello = h.posted.at(-1).message;
  h.emitMessage(toPage("READY", { version: "test" }, hello.id));

  const pending = h.api.current().dataStore.get("user-settings", "setting:test");
  const call = h.posted.at(-1).message;
  h.emitMessage(toPage("RESULT", {
    ok: false,
    code: "BW_PROVIDER_AUTH_EXPIRED",
    error: "provider 授权已过期",
  }, call.id));

  await assert.rejects(
    pending,
    (error) => error.code === "BW_PROVIDER_AUTH_EXPIRED",
  );
  assert.equal(h.api.current(), null);
  assert.equal(
    h.events.some((event) => event.type === "bw:extension-provider-disconnected"),
    true,
  );
  const authError = h.events.find(
    (event) => event.type === "bw:extension-provider-error",
  );
  assert.equal(authError.detail.code, "BW_PROVIDER_AUTH_EXPIRED");
  assert.equal(authError.detail.retryable, false);
});

test("握手错误有独立错误事件，不会伪装成普通 RESULT 或永久等待", () => {
  const h = harness();
  h.api.start({ namespace: NAMESPACE, ticket: TICKET });
  const hello = h.posted.at(-1).message;
  h.emitMessage(toPage("ERROR", {
    code: "BW_PROVIDER_PAGE",
    error: "阅读器页面身份不匹配",
  }, hello.id));
  const errorEvent = h.events.find((event) => event.type === "bw:extension-provider-error");
  assert.equal(errorEvent.detail.code, "BW_PROVIDER_PAGE");
  assert.equal(h.api.connected(), false);
  h.api.disconnect("test-complete");
});

test("没有扩展可信标记时不发送握手", () => {
  const h = harness({ marker: false });
  assert.equal(h.api.start({ namespace: NAMESPACE, ticket: TICKET }), false);
  assert.equal(h.posted.length, 0);
});

test("缺少、旧版或伪造 provider ticket 时拒绝握手", () => {
  for (const ticket of [
    undefined,
    "",
    "pvt-v2-not-a-ticket",
    `pvt-v1-${"b".repeat(64)}`,
  ]) {
    const h = harness();
    assert.equal(h.api.start({ namespace: NAMESPACE, ticket }), false);
    assert.equal(h.posted.length, 0);
    const authError = h.events.find(
      (event) =>
        event.type === "bw:extension-provider-error" &&
        event.detail.code === "BW_PROVIDER_AUTH",
    );
    assert.ok(authError);
  }
});

test("握手超时会复位状态并按退避重新发送带新编号的 HELLO", () => {
  const h = harness({ fakeTimers: true });
  h.api.start({
    namespace: NAMESPACE,
    ticket: TICKET,
    handshakeTimeoutMs: 10,
    retryBaseMs: 5,
    retryMaxMs: 20,
  });
  const firstHello = h.posted.at(-1).message;
  const timeout = h.runNextTimer();
  assert.equal(timeout.delay, 10);
  assert.equal(h.api.connected(), false);
  assert.equal(
    h.events.some((event) =>
      event.type === "bw:extension-provider-error" &&
      event.detail.code === "BW_PROVIDER_TIMEOUT"),
    true,
  );

  const retry = h.runNextTimer();
  assert.equal(retry.delay, 5);
  const secondHello = h.posted.at(-1).message;
  assert.equal(secondHello.type, "HELLO");
  assert.notEqual(secondHello.id, firstHello.id);

  h.emitMessage(toPage("READY", { version: "stale" }, firstHello.id));
  assert.equal(h.api.connected(), false);
  h.emitMessage(toPage("READY", { version: "current" }, secondHello.id));
  assert.equal(h.api.connected(), true);
});

test("写调用超时标记 outcome unknown、宣布不健康并且不会自动重写", async () => {
  const h = harness({ fakeTimers: true });
  h.api.start({ namespace: NAMESPACE, ticket: TICKET, requestTimeoutMs: 10, retryBaseMs: 5 });
  const hello = h.posted.at(-1).message;
  h.emitMessage(toPage("READY", { version: "test" }, hello.id));

  const store = h.api.current().dataStore;
  const pending = store.put(
    "user-settings",
    { id: "setting:timeout", rawValue: "1" },
    { mutationId: "put-timeout-once" },
  );
  const rejection = assert.rejects(pending, (error) => {
    assert.equal(error.code, "BW_PROVIDER_TIMEOUT");
    assert.equal(error.outcome, "unknown");
    assert.equal(error.outcomeUnknown, true);
    assert.equal(error.retrySafe, false);
    assert.equal(error.details.operation, "put");
    return true;
  });
  h.runNextTimer();
  await rejection;

  assert.equal(h.api.connected(), false);
  assert.equal(h.api.current(), null);
  assert.equal(
    h.events.some((event) =>
      event.type === "bw:extension-provider-unhealthy" &&
      event.detail.outcome === "unknown" &&
      event.detail.retrySafe === false),
    true,
  );
  assert.equal(
    h.events.some((event) => event.type === "bw:extension-provider-disconnected"),
    true,
  );
  assert.equal(
    h.posted.filter((entry) =>
      entry.message.type === "CALL" &&
      entry.message.payload.operation === "put").length,
    1,
  );
});

test("显式 restart 可取消旧握手并立即重新开始", () => {
  const h = harness({ fakeTimers: true });
  h.api.start({ namespace: NAMESPACE, ticket: TICKET });
  const firstHello = h.posted.at(-1).message;
  assert.equal(h.api.restart({ namespace: NAMESPACE }), true);
  const secondHello = h.posted.at(-1).message;
  assert.notEqual(secondHello.id, firstHello.id);
  h.emitMessage(toPage("READY", { version: "old" }, firstHello.id));
  assert.equal(h.api.connected(), false);
  h.emitMessage(toPage("READY", { version: "new" }, secondHello.id));
  assert.equal(h.api.connected(), true);
});

test("AUTH、NAMESPACE、PAGE 等永久握手错误停止自动重试", () => {
  for (const code of [
    "BW_PROVIDER_AUTH",
    "BW_PROVIDER_AUTH_EXPIRED",
    "BW_PROVIDER_NAMESPACE",
    "BW_PROVIDER_PAGE",
  ]) {
    const h = harness({ fakeTimers: true });
    h.api.start({
      namespace: NAMESPACE,
      ticket: TICKET,
      handshakeTimeoutMs: 10,
      retryBaseMs: 5,
    });
    const hello = h.posted.at(-1).message;
    h.emitMessage(toPage("ERROR", {
      code,
      error: `permanent ${code}`,
    }, hello.id));

    const errorEvent = h.events.find(
      (event) =>
        event.type === "bw:extension-provider-error" &&
        event.detail.code === code,
    );
    assert.equal(errorEvent.detail.permanent, true);
    assert.equal(errorEvent.detail.retryable, false);
    assert.equal(h.runNextTimer(), null, `${code} 后不得保留自动重试 timer`);
    assert.equal(
      h.posted.filter((entry) => entry.message.type === "HELLO").length,
      1,
    );

    assert.equal(h.api.restart({ namespace: NAMESPACE }), true, "显式 restart 仍可恢复");
    assert.equal(
      h.posted.filter((entry) => entry.message.type === "HELLO").length,
      2,
    );
    h.api.disconnect("test-complete");
  }
});

test("临时握手错误使用单一指数退避，重复 start 和旧 ERROR 不产生重复 HELLO", () => {
  const h = harness({ fakeTimers: true });
  h.api.start({
    namespace: NAMESPACE,
    ticket: TICKET,
    handshakeTimeoutMs: 100,
    retryBaseMs: 5,
    retryMaxMs: 20,
  });
  const firstHello = h.posted.at(-1).message;
  h.api.start({ namespace: NAMESPACE, ticket: TICKET });
  assert.equal(
    h.posted.filter((entry) => entry.message.type === "HELLO").length,
    1,
    "同一握手进行中重复 start 不得再发 HELLO",
  );

  h.emitMessage(toPage("ERROR", {
    code: "BW_PROVIDER_AUTH_PENDING",
    error: "authorization pending",
  }, firstHello.id));
  const firstRetry = h.runNextTimer();
  assert.equal(firstRetry.delay, 5);
  const secondHello = h.posted.at(-1).message;
  assert.notEqual(secondHello.id, firstHello.id);

  const errorsBeforeStale = h.events.filter(
    (event) => event.type === "bw:extension-provider-error",
  ).length;
  h.emitMessage(toPage("ERROR", {
    code: "BW_PROVIDER_AUTH",
    error: "stale permanent response",
  }, firstHello.id));
  assert.equal(
    h.events.filter((event) => event.type === "bw:extension-provider-error").length,
    errorsBeforeStale,
    "旧 helloId 的永久错误也必须忽略",
  );

  h.emitMessage(toPage("ERROR", {
    code: "BW_PROVIDER_AUTH_UNAVAILABLE",
    error: "authorization service unavailable",
  }, secondHello.id));
  const secondRetry = h.runNextTimer();
  assert.equal(secondRetry.delay, 10);
  assert.equal(
    h.posted.filter((entry) => entry.message.type === "HELLO").length,
    3,
  );
  h.api.disconnect("test-complete");
});

test("重复 DISCONNECTED 只保留一个重连定时器和一条新 HELLO", () => {
  const h = harness({ fakeTimers: true });
  h.api.start({
    namespace: NAMESPACE,
    ticket: TICKET,
    retryBaseMs: 5,
  });
  const hello = h.posted.at(-1).message;
  h.emitMessage(toPage("READY", { version: "test" }, hello.id));
  h.emitMessage(toPage("DISCONNECTED", { reason: "worker-restart" }));
  h.emitMessage(toPage("DISCONNECTED", { reason: "duplicate-disconnect" }));

  const retry = h.runNextTimer();
  assert.equal(retry.delay, 5);
  assert.equal(
    h.posted.filter((entry) => entry.message.type === "HELLO").length,
    2,
  );
  assert.equal(
    h.events.filter((event) => event.type === "bw:extension-provider-disconnected").length,
    1,
  );
  h.api.disconnect("test-complete");
});
