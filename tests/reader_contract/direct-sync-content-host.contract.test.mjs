import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SOURCE = readFileSync(
  new URL(
    "../../extensions/bw-reader-webext/src/direct-sync-content-host.js",
    import.meta.url,
  ),
  "utf8",
);
const PROTOCOL = "bw-reader-direct-host/1";

function harness({ connectFailures = 0 } = {}) {
  const windowListeners = new Map();
  const timers = [];
  let timerSequence = 0;
  let remainingConnectFailures = connectFailures;
  const state = {
    connectAttempts: 0,
    ports: [],
    hosts: [],
    signalTransports: [],
  };
  const setTimer = (callback, delay) => {
    const timer = {
      id: ++timerSequence,
      callback,
      delay,
      cancelled: false,
      fired: false,
    };
    timers.push(timer);
    return timer.id;
  };
  const clearTimer = (id) => {
    const timer = timers.find((entry) => entry.id === id);
    if (timer) timer.cancelled = true;
  };
  const addWindowListener = (type, listener) => {
    if (!windowListeners.has(type)) windowListeners.set(type, []);
    windowListeners.get(type).push(listener);
  };
  const createPort = () => {
    const messageListeners = [];
    const disconnectListeners = [];
    const port = {
      messages: [],
      disconnected: false,
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
        if (port.disconnected) throw new Error("port disconnected");
        port.messages.push(structuredClone(message));
      },
      emitMessage(message) {
        for (const listener of messageListeners) listener(structuredClone(message));
      },
      disconnect() {
        if (port.disconnected) return;
        port.disconnected = true;
        for (const listener of disconnectListeners) listener();
      },
    };
    return port;
  };
  const window = {
    addEventListener: addWindowListener,
  };
  window.window = window;
  window.top = window;

  const runtime = {
    directSyncSignalTransport: {
      createDirectSignalTransport(options) {
        const transport = { options };
        state.signalTransports.push(transport);
        return transport;
      },
    },
    directSyncHost: {
      createDirectSyncHost(options) {
        const host = {
          options,
          starts: [],
          destroys: [],
          start(reason) {
            host.starts.push(reason);
          },
          destroy(reason) {
            host.destroys.push(reason);
          },
        };
        state.hosts.push(host);
        return host;
      },
    },
    directSyncProtocol: {
      createChannelTransport() {},
    },
    syncGateway: {
      createSyncGateway() {},
    },
  };
  const chrome = {
    runtime: {
      lastError: null,
      connect(options) {
        state.connectAttempts += 1;
        assert.equal(options?.name, "bw-reader-direct-host");
        if (remainingConnectFailures > 0) {
          remainingConnectFailures -= 1;
          throw new Error("MV3 service worker unavailable");
        }
        const port = createPort();
        state.ports.push(port);
        return port;
      },
    },
  };
  const context = {
    window,
    chrome,
    BWReaderRuntime: runtime,
    RTCPeerConnection: function FakeRTCPeerConnection() {},
    crypto: {},
    Map,
    Set,
    Date,
    Promise,
    Error,
    Object,
    Array,
    String,
    Number,
    Math,
    structuredClone,
    setTimeout: setTimer,
    clearTimeout: clearTimer,
    console,
  };
  vm.runInContext(
    SOURCE,
    vm.createContext(context),
    { filename: "direct-sync-content-host.js" },
  );
  return {
    state,
    currentPort() {
      return state.ports.at(-1) || null;
    },
    nextTimer() {
      return timers.find((entry) => !entry.cancelled && !entry.fired) || null;
    },
    runNextTimer() {
      const timer = this.nextTimer();
      if (!timer) return null;
      timer.fired = true;
      timer.callback();
      return timer;
    },
    activeTimers() {
      return timers.filter((entry) => !entry.cancelled && !entry.fired);
    },
    dispatchWindow(type) {
      for (const listener of windowListeners.get(type) || []) {
        listener({ type });
      }
    },
  };
}

function hostMessage(type, payload = {}, id = null) {
  return {
    protocol: PROTOCOL,
    type,
    id,
    payload,
  };
}

function readyPayload(deviceId) {
  return {
    contract: PROTOCOL,
    deviceId,
    registryDigest:
      "sync-v3:record-parent-state/1|" +
      "user-settings:explicit:0:1|vocabulary-state:explicit:0:1",
    iceServers: [],
  };
}

test("MV3 后台断开会拒绝 pending、销毁旧宿主并隔离旧端口事件后重连", async () => {
  const h = harness();
  const firstPort = h.currentPort();
  assert.equal(h.state.connectAttempts, 1);
  firstPort.emitMessage(hostMessage("READY", readyPayload("device-one")));
  assert.equal(h.state.hosts.length, 1);
  assert.deepEqual(h.state.hosts[0].starts, ["extension-background-ready"]);

  const pendingBaseline = h.state.hosts[0].options.getServerBaseline();
  const baselineCall = firstPort.messages.find(
    (message) => message.operation === "BASELINE_STATUS",
  );
  assert.ok(baselineCall);

  firstPort.disconnect();
  await assert.rejects(
    pendingBaseline,
    (error) =>
      error.code === "BW_DIRECT_HOST_INACTIVE" &&
      error.retryable === true,
  );
  assert.deepEqual(h.state.hosts[0].destroys, ["background-disconnected"]);
  const firstRetry = h.nextTimer();
  assert.equal(firstRetry.delay, 500);
  assert.equal(h.activeTimers().length, 1);

  h.runNextTimer();
  const secondPort = h.currentPort();
  assert.notEqual(secondPort, firstPort);
  assert.equal(h.state.connectAttempts, 2);

  firstPort.emitMessage(hostMessage(
    "READY",
    readyPayload("stale-device"),
  ));
  firstPort.emitMessage(hostMessage(
    "RESULT",
    { ok: true, result: { ready: true } },
    baselineCall.id,
  ));
  assert.equal(h.state.hosts.length, 1, "旧端口不能重新创建宿主");
  assert.equal(secondPort.messages.length, 0, "旧端口结果不能转发到新端口");
  await assert.rejects(
    h.state.hosts[0].options.getServerBaseline(),
    (error) => error.code === "BW_DIRECT_HOST_INACTIVE",
  );
  assert.equal(
    secondPort.messages.length,
    0,
    "旧宿主回调不能借新端口发起调用",
  );

  secondPort.emitMessage(hostMessage("STANDBY", {
    reason: "selecting-host",
    retryable: true,
  }));
  secondPort.emitMessage(hostMessage(
    "READY",
    readyPayload("device-two"),
  ));
  assert.equal(h.state.hosts.length, 2);
  assert.equal(h.state.hosts[1].options.deviceId, "device-two");

  secondPort.disconnect();
  assert.equal(
    h.nextTimer().delay,
    500,
    "新端口收到有效协议消息后应重置退避",
  );
});

test("MV3 连接失败采用单一指数退避 timer，并在 30 秒封顶", () => {
  const h = harness({ connectFailures: 9 });
  assert.equal(h.state.connectAttempts, 1);
  const expectedDelays = [
    500,
    1_000,
    2_000,
    4_000,
    8_000,
    16_000,
    30_000,
    30_000,
  ];
  for (const expected of expectedDelays) {
    assert.equal(h.activeTimers().length, 1);
    const retry = h.runNextTimer();
    assert.equal(retry.delay, expected);
  }
  assert.equal(h.state.connectAttempts, 9);
  assert.equal(h.state.ports.length, 0);
  assert.equal(h.activeTimers().length, 1);
  assert.equal(h.nextTimer().delay, 30_000);
});

test("pagehide 会清理宿主、pending、端口和重连 timer，之后不再复活", async () => {
  const h = harness();
  const port = h.currentPort();
  port.emitMessage(hostMessage("READY", readyPayload("device-unload")));
  const host = h.state.hosts[0];
  const pendingBaseline = host.options.getServerBaseline();
  assert.equal(
    port.messages.some((message) => message.operation === "BASELINE_STATUS"),
    true,
  );

  h.dispatchWindow("pagehide");
  await assert.rejects(
    pendingBaseline,
    (error) => error.code === "BW_DIRECT_HOST_INACTIVE",
  );
  assert.equal(port.disconnected, true);
  assert.deepEqual(host.destroys, ["page-unloaded"]);
  assert.equal(h.activeTimers().length, 0);

  port.emitMessage(hostMessage("READY", readyPayload("late-device")));
  port.disconnect();
  assert.equal(h.state.hosts.length, 1);
  assert.equal(h.activeTimers().length, 0);
  assert.equal(h.state.connectAttempts, 1);
  await assert.rejects(
    host.options.getServerBaseline(),
    (error) => error.code === "BW_DIRECT_HOST_INACTIVE",
  );
});
