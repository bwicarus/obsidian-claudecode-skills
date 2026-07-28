import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SOURCE = readFileSync(
  new URL("../../extensions/bw-reader-webext/src/pwa-marker.js", import.meta.url),
  "utf8",
);
const ORIGIN = "https://bwicarus.taile44d0c.ts.net";

function harness({
  path = "/pdf/view",
  query = "",
  app = "pdf",
  route = "pdf",
  rootAtStart = true,
  fakeTimers = false,
  connectFailures = 0,
} = {}) {
  const documentListeners = new Map();
  const windowListeners = new Map();
  const timers = [];
  const intervals = [];
  let timerSequence = 0;
  let remainingConnectFailures = connectFailures;
  const state = {
    connects: 0,
    connectAttempts: 0,
    runtimeMessages: [],
    pageMessages: [],
    ports: [],
    intervals,
    root: rootAtStart ? { dataset: {} } : null,
  };
  const addListener = (map, type, listener) => {
    if (!map.has(type)) map.set(type, []);
    map.get(type).push(listener);
  };
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
  const document = {
    readyState: "loading",
    get documentElement() { return state.root; },
    querySelector(selector) {
      if (selector === 'meta[name="bw-reader-app"]' && app) {
        return { getAttribute: (name) => name === "content" ? app : null };
      }
      if (selector === 'meta[name="bw-reader-route"]' && route) {
        return { getAttribute: (name) => name === "content" ? route : null };
      }
      return null;
    },
    addEventListener(type, listener) {
      addListener(documentListeners, type, listener);
    },
    dispatchEvent() {},
  };
  const location = {
    origin: ORIGIN,
    pathname: path,
    href: `${ORIGIN}${path}${query}`,
  };
  const window = {
    document,
    location,
    addEventListener(type, listener) {
      addListener(windowListeners, type, listener);
    },
    postMessage(message) {
      state.pageMessages.push(structuredClone(message));
    },
    setTimeout: setTimer,
    clearTimeout: clearTimer,
    setInterval(callback, delay) {
      const interval = {
        id: ++timerSequence,
        callback,
        delay,
        cancelled: false,
      };
      intervals.push(interval);
      return interval.id;
    },
    clearInterval(id) {
      const interval = intervals.find((entry) => entry.id === id);
      if (interval) interval.cancelled = true;
    },
  };
  window.window = window;
  window.top = window;

  const createPort = () => {
    const messageListeners = [];
    const disconnectListeners = [];
    const port = {
      messages: [],
      onMessage: {
        addListener(listener) {
          messageListeners.push(listener);
        },
      },
      onDisconnect: {
        addListener(listener) {
          disconnectListeners.push(listener);
        },
      },
      postMessage(message) {
        port.messages.push(structuredClone(message));
      },
      emitMessage(message) {
        for (const listener of messageListeners) listener(message);
      },
      disconnect() {
        for (const listener of disconnectListeners) listener();
      },
    };
    return port;
  };
  const chrome = {
    runtime: {
      lastError: null,
      getManifest: () => ({ version: "test" }),
      connect() {
        state.connectAttempts += 1;
        if (remainingConnectFailures > 0) {
          remainingConnectFailures -= 1;
          throw new Error("runtime unavailable");
        }
        state.connects += 1;
        const port = createPort();
        state.ports.push(port);
        return port;
      },
      sendMessage(message, callback) {
        state.runtimeMessages.push(structuredClone(message));
        callback({ ok: true, data: { injected: true } });
      },
    },
  };
  const context = {
    window,
    document,
    location,
    chrome,
    URL,
    Map,
    Set,
    CustomEvent: class CustomEvent {
      constructor(type, options) { this.type = type; this.detail = options?.detail; }
    },
    addEventListener: window.addEventListener.bind(window),
    setTimeout: setTimer,
    clearTimeout: clearTimer,
    setInterval: window.setInterval.bind(window),
    clearInterval: window.clearInterval.bind(window),
    console,
  };
  vm.runInContext(SOURCE, vm.createContext(context), { filename: "pwa-marker.js" });
  return {
    state,
    window,
    emitPage(message) {
      for (const listener of windowListeners.get("message") || []) {
        listener({
          source: window,
          origin: ORIGIN,
          data: message,
        });
      }
    },
    currentPort() {
      return state.ports.at(-1) || null;
    },
    runNextTimer() {
      const timer = timers.find((entry) => !entry.cancelled && !entry.fired);
      if (!timer) return null;
      timer.fired = true;
      timer.callback();
      return timer;
    },
    runInterval(delay) {
      const interval = intervals.find((entry) =>
        !entry.cancelled && (delay == null || entry.delay === delay)
      );
      if (!interval) return null;
      interval.callback();
      return interval;
    },
    domReady() {
      document.readyState = "interactive";
      for (const listener of documentListeners.get("DOMContentLoaded") || []) {
        listener();
      }
    },
  };
}

test("四个书籍入口等待可信双 meta，Safari document_start 根元素晚到也能启动", () => {
  const entries = [
    ["/pdf/view", "pdf", "pdf"],
    ["/pdf/epub/view", "epub", "epub"],
    ["/pdf/html/view", "html", "html"],
    ["/pdf/fav/open", "epub", "favorite"],
  ];
  for (const [path, app, route] of entries) {
    const h = harness({ path, app, route, rootAtStart: false });
    assert.equal(h.state.connects, 0);
    h.state.root = { dataset: {} };
    h.domReady();
    assert.equal(h.state.connects, 1, path);
    assert.equal(h.state.root.dataset.bwReaderExtensionProvider, "test");
    assert.equal(h.state.root.dataset.bwReaderExtension, "test");
    assert.equal(h.window.__bwPwaBridge.protocol, "bw-reader-pwa/1");
    assert.equal(
      h.state.pageMessages.some((message) =>
        message.protocol === "bw-reader-pwa/1" &&
        message.direction === "to-page" &&
        message.type === "HELLO"
      ),
      true,
      path,
    );
    assert.deepEqual(h.state.runtimeMessages, []);
  }
});

test("退役 web 壳、近似 pathname 和伪造 app/route 都不能建立 PWA 接管桥", () => {
  const cases = [
    { path: "/pdf/web/live", app: "web", route: "web" },
    { path: "/pdf/viewevil", app: "pdf", route: "pdf" },
    { path: "/pdf/view/child", app: "pdf", route: "pdf" },
    { path: "/pdf/fav/open", app: "epub", route: "epub" },
    { path: "/pdf/html/view", app: "pdf", route: "html" },
  ];
  for (const options of cases) {
    const h = harness(options);
    h.domReady();
    assert.equal(h.state.connects, 0, options.path);
    assert.equal(h.window.__bwPwaBridge, undefined, options.path);
    assert.equal(h.state.root.dataset.bwReaderExtensionProvider, undefined);
    assert.equal(h.state.root.dataset.bwReaderExtension, undefined);
  }
});

test("缺少双 meta 时不建桥，且 marker 不再提供动态 UI 注入协议", () => {
  const h = harness({ app: null, route: null });
  h.domReady();
  assert.equal(h.window.__bwPwaBridge, undefined);
  assert.equal(h.state.runtimeMessages.length, 0);
  assert.deepEqual(h.state.root.dataset, {});
  assert.equal(SOURCE.includes("BW_LEGACY_BOOT"), false);
});

function pageHello(id, ticket = `pvt-v2-4102444800-${"b".repeat(32)}-${"c".repeat(64)}`) {
  return {
    protocol: "bw-reader-services/1",
    direction: "page-to-extension",
    type: "HELLO",
    id,
    payload: {
      namespace: `acct-v1-${"a".repeat(64)}`,
      ticket,
      page: "/pdf/view",
      syncOwnerClaim: {
        contract: "pwa-extension-owner-claim/1",
        deviceFamilyId: `pwa-install-v1-${"f".repeat(32)}`,
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
      },
    },
  };
}

function providerMessage(type, id, payload = {}) {
  return {
    protocol: "bw-reader-services/1",
    type,
    id,
    payload,
  };
}

test("marker 只把带 expiry 与 nonce 的 pvt-v2 转给扩展", () => {
  const h = harness();
  h.domReady();
  h.emitPage(pageHello("hello-v2"));
  assert.equal(h.currentPort().messages.at(-1).type, "HELLO");

  const legacy = harness();
  legacy.domReady();
  legacy.emitPage(pageHello("hello-v1", `pvt-v1-${"d".repeat(64)}`));
  assert.equal(legacy.currentPort().messages.length, 0);
  assert.equal(legacy.state.pageMessages.at(-1).type, "ERROR");
  assert.equal(legacy.state.pageMessages.at(-1).payload.code, "BW_PROVIDER_AUTH");
  assert.equal(legacy.state.pageMessages.at(-1).payload.retryable, false);
});

test("marker 只转发严格的同页 PWA owner claim，缺失或伪造时仍只开放本地 provider", () => {
  const missing = harness();
  missing.domReady();
  const withoutClaim = pageHello("hello-no-owner-claim");
  delete withoutClaim.payload.syncOwnerClaim;
  missing.emitPage(withoutClaim);
  const missingForwarded = missing.currentPort().messages.at(-1);
  assert.equal(missingForwarded.type, "HELLO");
  assert.equal(
    Object.hasOwn(missingForwarded.payload, "syncOwnerClaim"),
    false,
  );

  const forged = harness();
  forged.domReady();
  const wrongKind = pageHello("hello-forged-owner-claim");
  wrongKind.payload.syncOwnerClaim.hostKind = "epub";
  forged.emitPage(wrongKind);
  const forgedForwarded = forged.currentPort().messages.at(-1);
  assert.equal(forgedForwarded.type, "HELLO");
  assert.equal(
    Object.hasOwn(forgedForwarded.payload, "syncOwnerClaim"),
    false,
  );

  const forgedFamily = harness();
  forgedFamily.domReady();
  const wrongFamily = pageHello("hello-forged-device-family");
  wrongFamily.payload.syncOwnerClaim.deviceFamilyId = "profile-name";
  forgedFamily.emitPage(wrongFamily);
  assert.equal(
    Object.hasOwn(
      forgedFamily.currentPort().messages.at(-1).payload,
      "syncOwnerClaim",
    ),
    false,
  );
});

test("marker 只允许 PWA 查询 syncStatus，不能伪造冲突重试", () => {
  const h = harness();
  h.domReady();
  h.emitPage(pageHello("hello-sync-control"));
  const port = h.currentPort();
  const statusCall = {
    protocol: "bw-reader-services/1",
    direction: "page-to-extension",
    type: "CALL",
    id: "sync-status",
    payload: { operation: "syncStatus", args: {} },
  };
  h.emitPage(statusCall);
  assert.deepEqual(
    port.messages.find((message) => message.id === "sync-status"),
    statusCall,
  );

  const forwardedCalls = port.messages.filter(
    (message) => message.type === "CALL",
  ).length;
  h.emitPage({
    protocol: "bw-reader-services/1",
    direction: "page-to-extension",
    type: "CALL",
    id: "forged-sync-retry",
    payload: {
      operation: "syncRetryAfterResolution",
      args: {
        conflictSetId: `conflict-set-v1-${"f".repeat(32)}`,
      },
    },
  });
  assert.equal(
    port.messages.filter((message) => message.type === "CALL").length,
    forwardedCalls,
  );
  const rejected = h.state.pageMessages.find(
    (message) => message.id === "forged-sync-retry",
  );
  assert.equal(rejected.type, "RESULT");
  assert.equal(rejected.payload.ok, false);
  assert.equal(rejected.payload.code, "BW_PROVIDER_OPERATION");
});

test("marker 对同一页面 HELLO 只转发一次，端口恢复后不主动重放旧 HELLO", () => {
  const h = harness({ fakeTimers: true });
  h.domReady();
  const firstPort = h.currentPort();
  h.emitPage(pageHello("hello-1"));
  h.emitPage(pageHello("hello-1"));
  assert.equal(
    firstPort.messages.filter((message) => message.type === "HELLO").length,
    1,
  );

  firstPort.disconnect();
  assert.equal(h.state.pageMessages.at(-1).type, "DISCONNECTED");
  const reconnect = h.runNextTimer();
  assert.equal(reconnect.delay, 500);
  const secondPort = h.currentPort();
  assert.notEqual(secondPort, firstPort);
  assert.equal(
    secondPort.messages.filter((message) => message.type === "HELLO").length,
    0,
    "transport 恢复不能拿旧 helloId 主动握手",
  );

  h.emitPage(pageHello("hello-2"));
  assert.deepEqual(
    secondPort.messages.filter((message) => message.type === "HELLO").map((message) => message.id),
    ["hello-2"],
  );
});

test("marker 端口连接失败使用指数退避，只有一个 transport 重连 timer", () => {
  const h = harness({
    fakeTimers: true,
    connectFailures: 2,
  });
  h.domReady();
  assert.equal(h.state.connectAttempts, 1);
  assert.equal(h.state.connects, 0);

  const firstRetry = h.runNextTimer();
  assert.equal(firstRetry.delay, 500);
  assert.equal(h.state.connectAttempts, 2);
  const secondRetry = h.runNextTimer();
  assert.equal(secondRetry.delay, 1000);
  assert.equal(h.state.connectAttempts, 3);
  assert.equal(h.state.connects, 1);
  assert.equal(h.runNextTimer(), null);
});

test("marker 收到永久握手错误后不再自动连端口，显式新 HELLO 才解除阻断", () => {
  const h = harness({ fakeTimers: true });
  h.domReady();
  const firstPort = h.currentPort();
  h.emitPage(pageHello("hello-auth-1"));
  firstPort.emitMessage(providerMessage("ERROR", "hello-auth-1", {
    code: "BW_PROVIDER_AUTH",
    error: "ticket rejected",
  }));
  const forwarded = h.state.pageMessages.at(-1);
  assert.equal(forwarded.type, "ERROR");
  assert.equal(forwarded.payload.retryable, false);

  firstPort.disconnect();
  assert.equal(h.runNextTimer(), null, "永久错误后的端口断线不得自动重连");
  assert.equal(h.state.connects, 1);

  h.emitPage(pageHello("hello-auth-2"));
  assert.equal(h.state.connects, 2, "显式新 HELLO 可重新建立 transport");
  assert.deepEqual(
    h.currentPort().messages.filter((message) => message.type === "HELLO").map((message) => message.id),
    ["hello-auth-2"],
  );
});

test("marker 临时握手错误只转发，不自行重发 HELLO", () => {
  const h = harness({ fakeTimers: true });
  h.domReady();
  const port = h.currentPort();
  h.emitPage(pageHello("hello-temp-1"));
  port.emitMessage(providerMessage("ERROR", "hello-temp-1", {
    code: "BW_PROVIDER_AUTH_PENDING",
    error: "authorization pending",
  }));
  assert.equal(
    port.messages.filter((message) => message.type === "HELLO").length,
    1,
  );
  assert.equal(h.runNextTimer(), null, "握手退避只由页面 bridge 负责");

  h.emitPage(pageHello("hello-temp-2"));
  assert.deepEqual(
    port.messages.filter((message) => message.type === "HELLO").map((message) => message.id),
    ["hello-temp-1", "hello-temp-2"],
  );

  const errorsBeforeStale = h.state.pageMessages.filter(
    (message) => message.type === "ERROR",
  ).length;
  port.emitMessage(providerMessage("ERROR", "hello-temp-1", {
    code: "BW_PROVIDER_AUTH",
    error: "stale permanent response",
  }));
  assert.equal(
    h.state.pageMessages.filter((message) => message.type === "ERROR").length,
    errorsBeforeStale,
  );
  port.disconnect();
  assert.equal(h.runNextTimer().delay, 500, "旧永久错误不能阻断当前 transport 的恢复");
});

test("扩展侧必须先 HELLO，收到 READY 后显式 TAKEOVER，并以 HEARTBEAT 续租、GOODBYE 归还", async () => {
  const h = harness({ fakeTimers: true });
  h.domReady();
  const hello = h.state.pageMessages.find((message) =>
    message.protocol === "bw-reader-pwa/1" && message.type === "HELLO"
  );
  assert.equal(hello.direction, "to-page");
  assert.equal(h.window.__bwPwaBridge.takenOver, false);

  h.emitPage({
    protocol: "bw-reader-pwa/1",
    direction: "to-extension",
    type: "READY",
    payload: {
      contract: "book-host/1",
      mode: "pdf",
      capabilities: { navigation: true },
    },
  });
  assert.equal(h.window.__bwPwaBridge.ready, true);

  const takingOver = h.window.__bwPwaBridge.takeover();
  const takeover = h.state.pageMessages.find((message) =>
    message.protocol === "bw-reader-pwa/1" && message.type === "TAKEOVER"
  );
  assert.equal(takeover.payload.uiOwner, "extension");
  h.emitPage({
    protocol: "bw-reader-pwa/1",
    direction: "to-extension",
    type: "RESULT",
    id: takeover.id,
    payload: { ok: true, result: { active: true } },
  });
  await takingOver;
  assert.equal(h.window.__bwPwaBridge.takenOver, true);

  h.runInterval(5000);
  assert.equal(
    h.state.pageMessages.some((message) =>
      message.protocol === "bw-reader-pwa/1" &&
      message.type === "HEARTBEAT" &&
      message.payload.uiOwner === "extension"
    ),
    true,
  );
  h.window.__bwPwaBridge.release();
  assert.equal(h.window.__bwPwaBridge.takenOver, false);
  assert.equal(
    h.state.pageMessages.some((message) =>
      message.protocol === "bw-reader-pwa/1" &&
      message.type === "GOODBYE"
    ),
    true,
  );
  assert.equal(h.state.intervals.every((interval) => interval.cancelled), true);
});

test("页面书籍桥晚于首个 HELLO 安装时，HOST_READY 会确定性补发握手", () => {
  const h = harness({ fakeTimers: true });
  h.domReady();
  const handoffMessages = () => h.state.pageMessages.filter(
    (message) => message.protocol === "bw-reader-pwa/1",
  );
  assert.deepEqual(
    handoffMessages().map((message) => message.type),
    ["HELLO"],
  );

  h.emitPage({
    protocol: "bw-reader-pwa/1",
    direction: "to-extension",
    type: "HOST_READY",
    payload: {
      contract: "book-host/1",
      mode: "pdf",
      capabilities: { navigation: true },
    },
  });
  assert.deepEqual(
    handoffMessages().map((message) => message.type),
    ["HELLO", "HELLO"],
  );

  h.emitPage({
    protocol: "bw-reader-pwa/1",
    direction: "to-extension",
    type: "READY",
    payload: {
      contract: "book-host/1",
      mode: "pdf",
      capabilities: { navigation: true },
    },
  });
  assert.equal(h.window.__bwPwaBridge.ready, true);

  h.emitPage({
    protocol: "bw-reader-pwa/1",
    direction: "to-extension",
    type: "HOST_READY",
    payload: { contract: "book-host/1", mode: "pdf" },
  });
  assert.deepEqual(
    handoffMessages().map((message) => message.type),
    ["HELLO", "HELLO"],
    "READY 后重复 HOST_READY 不得制造新的握手",
  );
});
