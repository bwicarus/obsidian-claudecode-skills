import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const SOURCE = fs.readFileSync(
  "_server_deploy/static/pdf/rc-computer-voice.js",
  "utf8",
);
const ENDPOINT =
  "wss://bwicarus-2.taile44d0c.ts.net/reader-computer-voice/v1";
const READER_ORIGIN = "https://bwicarus.taile44d0c.ts.net";
const RELAY_PORT = "BW_COMPUTER_VOICE_DIRECT_V2";
const DIRECT_CONTRACT = "reader-computer-voice-direct/1";

function createAudioContextClass(scenario) {
  return class FakeAudioContext {
    constructor(options) {
      assert.equal(options.sampleRate, 48000);
      this.sampleRate = 48000;
      this.state = scenario.initialAudioState || "suspended";
      this.currentTime = 0;
      this.destination = {};
      this.started = [];
      this.sourceRecords = [];
      this.closed = false;
      scenario.audioContexts.push(this);
    }

    resume() {
      const outcome = scenario.resumePlan.length
        ? scenario.resumePlan.shift()
        : "resolve";
      if (outcome === "reject") {
        return Promise.reject(new Error("NotAllowedError"));
      }
      this.state = "running";
      return Promise.resolve();
    }

    close() {
      this.closed = true;
      this.state = "closed";
      return Promise.resolve();
    }

    createBuffer(channels, length, sampleRate) {
      assert.equal(channels, 1);
      assert.equal(length, 960);
      assert.equal(sampleRate, 48000);
      const data = new Float32Array(length);
      return {
        getChannelData(channel) {
          assert.equal(channel, 0);
          return data;
        },
      };
    }

    createBufferSource() {
      const context = this;
      const record = {
        at: null,
        buffer: null,
        disconnected: false,
        started: false,
        stopped: false,
      };
      context.sourceRecords.push(record);
      return {
        buffer: null,
        onended: null,
        connect() {},
        disconnect() { record.disconnected = true; },
        start(at) {
          record.at = at;
          record.buffer = this.buffer;
          record.started = true;
          context.started.push(record);
        },
        stop() { record.stopped = true; },
      };
    }
  };
}

function createServer(scenario) {
  const server = {
    requests: [],
    sockets: [],
    activeSocket: null,
  };

  const result = (socket, request, payload) => {
    queueMicrotask(() => socket.onmessage?.({
      data: JSON.stringify({
        contract: DIRECT_CONTRACT,
        type: "result",
        requestId: request.requestId,
        ok: true,
        action: request.type,
        payload,
      }),
    }));
  };

  const failure = (socket, request, error) => {
    queueMicrotask(() => socket.onmessage?.({
      data: JSON.stringify({
        contract: DIRECT_CONTRACT,
        type: "result",
        requestId: request.requestId,
        ok: false,
        action: request.type,
        error: {
          code: error.code,
          message: error.message,
          retryable: false,
        },
      }),
    }));
  };

  class FakeWebSocket {
    constructor(url) {
      assert.equal(url, ENDPOINT);
      this.url = url;
      this.readyState = 0;
      this.binaryType = "";
      server.sockets.push(this);
      queueMicrotask(() => {
        if (scenario.offline) {
          this.onerror?.(new Error("offline"));
          return;
        }
        this.readyState = 1;
        this.onopen?.();
      });
    }

    send(serialized) {
      const request = JSON.parse(serialized);
      server.requests.push(request);
      if (request.type === "hello") {
        result(this, request, scenario.helloPayload || {
          protocolVersion: 2,
          limits: {
            maxMessageBytes: 65536,
            pcmFrameBytes: 1956,
            pcmQueueLimitMs: 400,
            heartbeatIntervalMs: 5000,
            heartbeatTimeoutMs: 15000,
          },
        });
        return;
      }
      if (request.type === "status") {
        if (scenario.binaryOnStatus) {
          queueMicrotask(() => this.onmessage?.({ data: new ArrayBuffer(1956) }));
        }
        if (scenario.deferStatusResult) return;
        result(this, request, {
          ready: true,
          state: "idle",
          reason: null,
          localOptIn: true,
          media: { hostReady: true, captureActive: false },
        });
        return;
      }
      if (request.type === "start") {
        server.activeSocket = this;
        queueMicrotask(() => this.onmessage?.({
          data: JSON.stringify({
            contract: DIRECT_CONTRACT,
            type: "event",
            event: "status",
            payload: { state: "starting-app", reason: null },
          }),
        }));
        for (const frame of scenario.framesBeforeStartResult || []) {
          queueMicrotask(() => this.onmessage?.({ data: frame(request.sessionId) }));
        }
        if (scenario.startError) {
          failure(this, request, scenario.startError);
          return;
        }
        if (scenario.deferStartResult) {
          return;
        }
        result(this, request, {
          sessionId: request.sessionId,
          state: "active",
          media: { hostReady: true, captureActive: true },
        });
        for (const frame of scenario.framesAfterStart || []) {
          setImmediate(() => this.onmessage?.({ data: frame(request.sessionId) }));
        }
        return;
      }
      if (request.type === "heartbeat") {
        result(this, request, {
          sessionId: request.sessionId,
          sequence: request.sequence,
          state: "active",
        });
        return;
      }
      if (request.type === "stop") {
        result(this, request, {
          sessionId: request.sessionId,
          state: "idle",
        });
        return;
      }
      throw new Error(`unexpected action ${request.type}`);
    }

    close() {
      this.readyState = 3;
      queueMicrotask(() => this.onclose?.());
    }

    emitBinary(buffer) {
      this.onmessage?.({ data: buffer });
    }
  }

  server.WebSocket = FakeWebSocket;
  return server;
}

function createRelayRuntime(server, scenario) {
  const ports = [];
  const clientMessages = [];
  let activePort = null;
  const runtime = {
    id: "abcdefghijklmnopabcdefghijklmnop",
    connect(options) {
      assert.equal(options?.name, RELAY_PORT);
      assert.deepEqual(Object.keys(options || {}), ["name"]);
      if (activePort) {
        scenario.relayOverlapAttempts += 1;
        throw new Error("per-tab relay slot is still occupied");
      }
      const messageListeners = [];
      const disconnectListeners = [];
      let socket = null;
      let disconnected = false;
      const emit = (message) => {
        for (const listener of messageListeners) listener(message);
      };
      const port = {
        onMessage: {
          addListener(listener) { messageListeners.push(listener); },
        },
        onDisconnect: {
          addListener(listener) { disconnectListeners.push(listener); },
        },
        postMessage(message) {
          clientMessages.push(structuredClone(message));
          if (message.type === "open") {
            assert.deepEqual(Object.keys(message), ["type"]);
            assert.equal(socket, null);
            socket = new server.WebSocket(ENDPOINT);
            socket.onopen = () => emit({ type: "open" });
            socket.onmessage = ({ data }) => {
              if (typeof data === "string") {
                emit({ type: "text", data });
                return;
              }
              const bytes = Buffer.from(data);
              emit({
                type: "binary-base64",
                data: bytes.toString("base64"),
                bytes: bytes.length,
              });
            };
            socket.onerror = () => emit({
              type: "error",
              code: "BW_COMPUTER_VOICE_DIRECT_RELAY_TEST",
              error: "relay test offline",
            });
            socket.onclose = () => emit({
              type: "close",
              code: 1000,
              reason: "relay-test-close",
              wasClean: true,
            });
            return;
          }
          if (message.type === "send-text") {
            assert.deepEqual(
              Object.keys(message).sort(),
              ["data", "type"],
            );
            assert.equal(typeof message.data, "string");
            socket.send(message.data);
            return;
          }
          if (message.type === "close") {
            assert.deepEqual(Object.keys(message), ["type"]);
            socket?.close();
            return;
          }
          assert.fail(`unexpected relay client message ${message.type}`);
        },
        disconnect() {
          if (disconnected) return;
          disconnected = true;
          if (activePort === port) activePort = null;
          scenario.relayLifecycle.push("port-disconnect");
          for (const listener of disconnectListeners) listener();
        },
      };
      activePort = port;
      ports.push(port);
      return port;
    },
  };
  scenario.relayPorts = ports;
  scenario.relayClientMessages = clientMessages;
  return runtime;
}

function createManualTimers() {
  let nextId = 1;
  const entries = new Map();
  return {
    setTimeout(callback, delay) {
      const id = nextId++;
      entries.set(id, { callback, delay });
      return id;
    },
    clearTimeout(id) {
      entries.delete(id);
    },
    count(delay) {
      return [...entries.values()].filter((entry) => entry.delay === delay)
        .length;
    },
    runOne(delay) {
      const match = [...entries.entries()].find(
        ([, entry]) => entry.delay === delay,
      );
      assert.ok(match, `missing timer with delay ${delay}`);
      entries.delete(match[0]);
      match[1].callback();
    },
  };
}

function decodeSessionBytes(sessionId) {
  assert.match(sessionId, /^session-[A-Za-z0-9_-]{22}$/);
  return Buffer.from(sessionId.slice("session-".length), "base64url");
}

function pcmFrame(sessionId, options = {}) {
  if (options.length) return new ArrayBuffer(options.length);
  const buffer = new ArrayBuffer(1956);
  const view = new DataView(buffer);
  const magic = options.magic ?? "BWCV";
  for (let index = 0; index < 4; index += 1) {
    view.setUint8(index, magic.charCodeAt(index) || 0);
  }
  view.setUint8(4, 1);
  view.setUint8(5, options.track ?? 1);
  view.setUint16(6, 0, true);
  decodeSessionBytes(sessionId).forEach((byte, index) => {
    view.setUint8(8 + index, byte);
  });
  view.setUint32(24, options.sequence ?? 0, true);
  view.setUint32(28, options.timestampLow ?? 1, true);
  view.setUint32(32, options.timestampHigh ?? 0, true);
  for (let index = 0; index < 960; index += 1) {
    view.setInt16(36 + index * 2, index % 32, true);
  }
  return buffer;
}

function createHarness(overrides = {}) {
  const scenario = {
    offline: false,
    startError: null,
    framesAfterStart: [],
    framesBeforeStartResult: [],
    binaryOnStatus: false,
    helloPayload: null,
    deferStartResult: false,
    initialAudioState: "suspended",
    resumePlan: [],
    audioContexts: [],
    origin: READER_ORIGIN,
    extensionRelay: false,
    directWebSocketAttempts: 0,
    deferStatusResult: false,
    relayLifecycle: [],
    relayOverlapAttempts: 0,
    ...overrides,
  };
  const server = createServer(scenario);
  const scheduleTimeout = scenario.timers
    ? scenario.timers.setTimeout.bind(scenario.timers)
    : setTimeout;
  const cancelTimeout = scenario.timers
    ? scenario.timers.clearTimeout.bind(scenario.timers)
    : clearTimeout;
  const clickHandlers = [];
  const phoneButtons = {
    "asst-call": {
      id: "asst-call",
      isConnected: true,
      nodeType: 1,
      parentNode: null,
      tagName: "BUTTON",
      type: "button",
    },
    "vc-top-call": {
      id: "vc-top-call",
      isConnected: true,
      nodeType: 1,
      parentNode: null,
      tagName: "BUTTON",
      type: "button",
    },
  };
  const document = {
    getElementById(id) {
      return phoneButtons[id] || null;
    },
    addEventListener(type, handler, capture) {
      if (type === "click") {
        assert.equal(capture, true);
        clickHandlers.push(handler);
      }
    },
  };
  Object.values(phoneButtons).forEach((button) => {
    button.ownerDocument = document;
  });
  const window = {
    RC: {},
    WebSocket: scenario.extensionRelay
      ? class ForbiddenContentScriptWebSocket {
        constructor() {
          scenario.directWebSocketAttempts += 1;
          throw new Error("content script must not open direct WebSocket");
        }
      }
      : server.WebSocket,
    AudioContext: createAudioContextClass(scenario),
    crypto: webcrypto,
    isSecureContext: true,
    location: { origin: scenario.origin },
    setTimeout: scheduleTimeout,
    clearTimeout: cancelTimeout,
    dispatchEvent() {},
    document,
  };
  if (scenario.extensionRelay) {
    window.chrome = {
      runtime: createRelayRuntime(server, scenario),
    };
  }
  const context = vm.createContext({
    window,
    document,
    URL,
    TextEncoder,
    Uint8Array,
    Int16Array,
    Float32Array,
    ArrayBuffer,
    DataView,
    Map,
    Set,
    Object,
    Promise,
    Date,
    Number,
    JSON,
    Math,
    Error,
    RegExp,
    String,
    console,
    setTimeout: scheduleTimeout,
    clearTimeout: cancelTimeout,
    queueMicrotask,
    CustomEvent: class CustomEvent {
      constructor(type, init) {
        this.type = type;
        this.detail = init?.detail;
      }
    },
    btoa(value) {
      return Buffer.from(value, "binary").toString("base64");
    },
    atob(value) {
      return Buffer.from(value, "base64").toString("binary");
    },
  });
  vm.runInContext(SOURCE, context, { filename: "rc-computer-voice.js" });
  assert.equal(
    window.RC.computerVoice.registerPhoneButton(phoneButtons["asst-call"]),
    true,
  );
  assert.equal(
    window.RC.computerVoice.registerPhoneButton(phoneButtons["vc-top-call"]),
    true,
  );
  return {
    api: window.RC.computerVoice,
    scenario,
    server,
    clickHandlers,
    phoneButtons,
  };
}

function phoneClick(harness, {
  id = "asst-call",
  trusted = true,
  target = null,
} = {}) {
  const event = {
    isTrusted: trusted,
    target: target || harness.phoneButtons[id],
    prevented: false,
    stopped: false,
    preventDefault() { this.prevented = true; },
    stopImmediatePropagation() { this.stopped = true; },
  };
  harness.clickHandlers[0](event);
  return event;
}

function startWithTrustedGesture(harness, options) {
  phoneClick(harness, { trusted: true });
  return harness.api.startFromUserGesture(options);
}

async function waitForRequest(harness, type) {
  for (let attempt = 0; attempt < 200; attempt += 1) {
    const request = harness.server.requests.find(
      (candidate) => candidate.type === type,
    );
    if (request) return request;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  assert.fail(`timed out waiting for ${type} request`);
}

test("v2 HELLO 后直接 STATUS，固定 WSS 且不读取浏览器身份", async () => {
  const harness = createHarness();
  const availability = await harness.api.availability();
  assert.equal(availability.state, "idle");
  assert.equal(availability.endpoint, ENDPOINT);
  assert.equal(harness.server.sockets[0].url, ENDPOINT);
  assert.equal(
    harness.server.requests.filter((request) => request.type === "start").length,
    0,
  );
  assert.deepEqual(
    harness.server.requests.map((request) => request.type),
    ["hello", "status"],
  );
  assert.deepEqual(
    Object.keys(harness.server.requests[0]).sort(),
    ["contract", "protocolVersion", "requestId", "type"],
  );
  assert.equal(harness.server.requests[0].protocolVersion, 2);
  assert.equal(harness.api.beginPairing, undefined);
  assert.equal(harness.api.forgetIdentity, undefined);
  assert.doesNotMatch(
    SOURCE,
    /indexedDB|pairingCode|clientPublicKeySpki|type:\s*"pair"|type:\s*"auth"/i,
  );
  assert.doesNotMatch(SOURCE, /data-role="(?:endpoint|code|pair|forget)"/);
});

test("START 只消费受控电话按钮的真实点击，直接调用、伪同 ID 与宿主页 click 均 fail closed", async () => {
  const direct = createHarness();
  await assert.rejects(
    direct.api.startFromUserGesture(),
    (error) => error?.code === "BW_COMPUTER_VOICE_GESTURE_REQUIRED",
  );
  assert.equal(direct.scenario.audioContexts.length, 0);
  assert.equal(direct.server.sockets.length, 0);

  const synthetic = createHarness();
  const untrusted = phoneClick(synthetic, { trusted: false });
  assert.equal(untrusted.prevented, false);
  await assert.rejects(
    synthetic.api.startFromUserGesture(),
    (error) => error?.code === "BW_COMPUTER_VOICE_GESTURE_REQUIRED",
  );
  assert.equal(synthetic.scenario.audioContexts.length, 0);
  assert.equal(synthetic.server.sockets.length, 0);

  const decoy = createHarness();
  const original = decoy.phoneButtons["asst-call"];
  const replacement = {
    ...original,
    ownerDocument: original.ownerDocument,
  };
  decoy.phoneButtons["asst-call"] = replacement;
  phoneClick(decoy, {
    trusted: true,
    target: replacement,
  });
  phoneClick(decoy, {
    id: "asst-call",
    trusted: false,
    target: original,
  });
  await assert.rejects(
    decoy.api.startFromUserGesture(),
    (error) => error?.code === "BW_COMPUTER_VOICE_GESTURE_REQUIRED",
  );
  assert.equal(decoy.scenario.audioContexts.length, 0);
  assert.equal(decoy.server.sockets.length, 0);

  const forwarded = createHarness();
  phoneClick(forwarded, { id: "vc-top-call", trusted: true });
  phoneClick(forwarded, { id: "asst-call", trusted: false });
  const started = await forwarded.api.startFromUserGesture();
  assert.equal(started.ok, true);
  await forwarded.api.stop("test");
  await assert.rejects(
    forwarded.api.startFromUserGesture(),
    (error) => error?.code === "BW_COMPUTER_VOICE_GESTURE_REQUIRED",
  );
});

test("真实点击 start lease 五秒过期后不能迟到启动", async () => {
  const timers = createManualTimers();
  const harness = createHarness({ timers });
  phoneClick(harness, { trusted: true });
  assert.equal(timers.count(5000), 1);
  timers.runOne(5000);
  await assert.rejects(
    harness.api.startFromUserGesture(),
    (error) => error?.code === "BW_COMPUTER_VOICE_GESTURE_REQUIRED",
  );
  assert.equal(harness.server.sockets.length, 0);
  assert.equal(harness.scenario.audioContexts.at(-1).closed, true);
});

test("普通网页只走扩展后台固定 relay，缺少 relay 时绝不回退 WebSocket", async () => {
  const extension = createHarness({
    origin: "https://arbitrary.example",
    extensionRelay: true,
  });
  const availability = await extension.api.availability();
  assert.equal(availability.state, "idle");
  assert.equal(extension.scenario.directWebSocketAttempts, 0);
  assert.deepEqual(
    extension.scenario.relayClientMessages.map((message) => message.type),
    ["open", "send-text", "send-text", "close"],
  );
  assert.ok(extension.scenario.relayClientMessages.every(
    (message) => !Object.hasOwn(message, "url"),
  ));

  const missing = createHarness({ origin: "https://arbitrary.example" });
  const unavailable = await missing.api.availability();
  assert.equal(unavailable.state, "offline");
  assert.equal(unavailable.code, "BW_COMPUTER_VOICE_DIRECT_RELAY_REQUIRED");
  assert.equal(missing.server.sockets.length, 0);
});

test("状态刷新让位并释放单标签 relay 后才 START，通话中刷新不再开连接", async () => {
  const harness = createHarness({
    origin: "https://arbitrary.example",
    extensionRelay: true,
    deferStatusResult: true,
  });
  const refresh = harness.api.availability();
  assert.equal(harness.api.availability(), refresh);
  await waitForRequest(harness, "status");

  const startedPromise = startWithTrustedGesture(harness);
  const [refreshResult, started] = await Promise.all([refresh, startedPromise]);

  assert.equal(refreshResult.state, "busy");
  assert.equal(started.ok, true);
  assert.equal(harness.scenario.relayOverlapAttempts, 0);
  assert.equal(harness.scenario.relayPorts.length, 2);
  assert.equal(
    harness.server.requests.filter((request) => request.type === "start").length,
    1,
  );

  const portCount = harness.scenario.relayPorts.length;
  const socketCount = harness.server.sockets.length;
  const current = await harness.api.availability();
  assert.equal(current.state, "active");
  assert.equal(harness.scenario.relayPorts.length, portCount);
  assert.equal(harness.server.sockets.length, socketCount);
  await harness.api.stop("test");
});

test("扩展 relay 的 binary-base64 精确解码后复用同一 PCM parser", async () => {
  const harness = createHarness({
    origin: "http://arbitrary.example",
    extensionRelay: true,
    framesAfterStart: [
      (sessionId) => pcmFrame(sessionId),
    ],
  });
  const started = await startWithTrustedGesture(harness);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(started.ok, true);
  assert.equal(harness.api.isActive(), true);
  assert.equal(harness.scenario.directWebSocketAttempts, 0);
  assert.equal(harness.scenario.audioContexts.at(-1).started.length, 1);
  await harness.api.stop("test");
});

test("扩展 relay close 先交付通话失败状态，再释放 runtime Port", async () => {
  const harness = createHarness({
    origin: "https://arbitrary.example",
    extensionRelay: true,
  });
  const unsubscribe = harness.api.onStatus((status) => {
    harness.scenario.relayLifecycle.push(`status:${status.state}`);
  });
  const started = await startWithTrustedGesture(harness);
  assert.equal(started.ok, true);

  harness.server.activeSocket.close();
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(harness.api.isActive(), false);
  assert.deepEqual(
    harness.scenario.relayLifecycle.slice(-2),
    ["status:failed", "port-disconnect"],
  );
  unsubscribe();
});

test("Windows 离线明确返回 offline，且不回退 Pi 或发送 START", async () => {
  const harness = createHarness({ offline: true });
  const availability = await harness.api.availability();
  assert.equal(availability.state, "offline");
  assert.equal(
    harness.server.requests.filter((request) => request.type === "start").length,
    0,
  );
  assert.doesNotMatch(SOURCE, /\/api\/reader\/computer-voice/);
});

test("旧 v1 或附加认证字段会 fail closed，且不会继续 STATUS", async () => {
  const harness = createHarness({
    helloPayload: {
      protocolVersion: 1,
      paired: true,
      limits: {
        maxMessageBytes: 65536,
        pcmFrameBytes: 1956,
        pcmQueueLimitMs: 400,
        heartbeatIntervalMs: 5000,
        heartbeatTimeoutMs: 15000,
      },
    },
  });
  const availability = await harness.api.availability();
  assert.equal(availability.state, "offline");
  assert.equal(
    harness.server.requests.filter((request) => request.type === "status").length,
    0,
  );
  assert.equal(availability.code, "BW_COMPUTER_VOICE_DIRECT_SCHEMA");
});

test("START 失败完整清 active/AudioContext/WSS，下一次拨号可重试", async () => {
  const harness = createHarness({
    startError: {
      code: "BW_COMPUTER_VOICE_DIRECT_APP_LAUNCHER_NOT_WIRED",
      message: "launcher not wired",
    },
  });
  await assert.rejects(
    startWithTrustedGesture(harness),
    /launcher not wired/,
  );
  assert.equal(harness.api.isActive(), false);
  assert.equal(harness.scenario.audioContexts.at(-1).closed, true);

  harness.scenario.startError = null;
  const started = await startWithTrustedGesture(harness);
  assert.equal(started.ok, true);
  assert.equal(harness.api.isActive(), true);
  assert.equal(
    harness.server.requests.filter((request) => request.type === "start").length,
    2,
  );
  await harness.api.stop("test");
});

test("启动中二次电话点击立即取消 WSS，不等待 START 或排队 STOP", async () => {
  const harness = createHarness({ deferStartResult: true });
  const statuses = [];
  harness.api.onStatus((status) => statuses.push(status.state));

  const starting = startWithTrustedGesture(harness).then(
    () => null,
    (error) => error,
  );
  await waitForRequest(harness, "start");
  assert.equal(harness.api.isActive(), true);

  const event = phoneClick(harness, { trusted: true });
  assert.equal(event.prevented, false);
  assert.equal(event.stopped, false);

  const stopped = await harness.api.stop("second-phone-click");
  const startError = await starting;
  assert.equal(stopped.state, "stopped");
  assert.equal(startError.code, "BW_COMPUTER_VOICE_DIRECT_CANCELLED");
  assert.equal(harness.api.isActive(), false);
  assert.equal(harness.scenario.audioContexts.at(-1).closed, true);
  assert.equal(harness.server.sockets.at(-1).readyState, 3);
  assert.equal(
    harness.server.requests.filter((request) => request.type === "start").length,
    1,
  );
  assert.equal(
    harness.server.requests.filter((request) => request.type === "stop").length,
    0,
  );
  assert.equal(statuses.at(-1), "stopped");
  assert.equal(statuses.includes("failed"), false);
});

test("通话仅在 START 后每 5 秒续租，停止后清除 heartbeat", async () => {
  const timers = createManualTimers();
  const harness = createHarness({ timers });
  await harness.api.availability();
  assert.equal(
    harness.server.requests.filter(
      (request) => request.type === "heartbeat",
    ).length,
    0,
  );

  const started = await startWithTrustedGesture(harness);
  assert.equal(started.ok, true);
  assert.equal(timers.count(5000), 1);
  timers.runOne(5000);
  await new Promise((resolve) => setImmediate(resolve));

  const heartbeats = harness.server.requests.filter(
    (request) => request.type === "heartbeat",
  );
  assert.equal(heartbeats.length, 1);
  assert.deepEqual(
    Object.keys(heartbeats[0]).sort(),
    ["contract", "requestId", "sequence", "sessionId", "type"],
  );
  assert.equal(heartbeats[0].sessionId, started.sessionId);
  assert.equal(heartbeats[0].sequence, 1);
  assert.equal(timers.count(5000), 1);
  timers.runOne(5000);
  await new Promise((resolve) => setImmediate(resolve));
  const secondHeartbeat = harness.server.requests.filter(
    (request) => request.type === "heartbeat",
  ).at(-1);
  assert.equal(secondHeartbeat.sequence, 2);
  assert.equal(timers.count(5000), 1);

  await harness.api.stop("test");
  assert.equal(timers.count(5000), 0);
});

test("AudioContext 被拒后第二次电话点击只恢复播放，不会 STOP 或重复 START", async () => {
  const harness = createHarness({
    resumePlan: ["reject", "resolve"],
    framesAfterStart: [
      (sessionId) => pcmFrame(sessionId),
    ],
  });
  const statuses = [];
  harness.api.onStatus((status) => statuses.push(status.state));
  await startWithTrustedGesture(harness);
  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(statuses.includes("audio-blocked"));
  assert.equal(harness.api.isActive(), true);

  const event = phoneClick(harness, { trusted: true });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(event.prevented, true);
  assert.equal(event.stopped, true);
  assert.equal(harness.api.isActive(), true);
  assert.equal(
    harness.server.requests.filter((request) => request.type === "start").length,
    1,
  );
  assert.equal(
    harness.server.requests.filter((request) => request.type === "stop").length,
    0,
  );
  assert.equal(harness.scenario.audioContexts.at(-1).started.length, 1);
  await harness.api.stop("test");
});

test("AudioContext 持续 blocked 时只保留最新帧，不会在 400ms 后自动挂断", async () => {
  const harness = createHarness({
    resumePlan: ["reject", "resolve"],
  });
  const statuses = [];
  harness.api.onStatus((status) => statuses.push(status.state));
  const started = await startWithTrustedGesture(harness);
  assert.ok(statuses.includes("audio-blocked"));

  for (let sequence = 0; sequence < 120; sequence += 1) {
    harness.server.activeSocket.emitBinary(pcmFrame(started.sessionId, {
      sequence,
      timestampLow: sequence + 1,
    }));
  }
  assert.equal(harness.api.isActive(), true);
  assert.equal(
    statuses.filter((state) => state === "audio-blocked").length,
    1,
  );
  assert.equal(harness.scenario.audioContexts.at(-1).started.length, 0);

  const resumed = phoneClick(harness, {
    id: "vc-top-call",
    trusted: true,
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(resumed.prevented, true);
  assert.equal(resumed.stopped, true);
  assert.equal(harness.api.isActive(), true);
  assert.equal(harness.scenario.audioContexts.at(-1).started.length, 1);
  assert.equal(
    harness.server.requests.filter((request) => request.type === "start").length,
    1,
  );
  await harness.api.stop("test");
});

test("START 的首个 PCM 可先于 result，STATUS 连接上的 PCM 仍被拒绝", async () => {
  const early = createHarness({
    framesBeforeStartResult: [
      (sessionId) => pcmFrame(sessionId),
    ],
  });
  const started = await startWithTrustedGesture(early);
  assert.equal(started.ok, true);
  assert.equal(early.api.isActive(), true);
  assert.equal(early.scenario.audioContexts.at(-1).started.length, 1);
  await early.api.stop("test");

  const unsolicited = createHarness({ binaryOnStatus: true });
  const availability = await unsolicited.api.availability();
  assert.equal(availability.state, "offline");
  assert.equal(unsolicited.api.isActive(), false);
});

test("PCM parser 对错误长度、magic、track 和 sequence 全部 fail closed", async () => {
  const cases = [
    { name: "length", frame: (sessionId) => pcmFrame(sessionId, { length: 10 }) },
    { name: "magic", frame: (sessionId) => pcmFrame(sessionId, { magic: "NOPE" }) },
    { name: "track", frame: (sessionId) => pcmFrame(sessionId, { track: 3 }) },
    {
      name: "sequence",
      validFirst: true,
      frame: (sessionId) => pcmFrame(sessionId, {
        sequence: 0,
        timestampLow: 2,
      }),
    },
  ];
  for (const item of cases) {
    const harness = createHarness();
    const started = await startWithTrustedGesture(harness);
    if (item.validFirst) {
      harness.server.activeSocket.emitBinary(
        pcmFrame(started.sessionId, { sequence: 0, timestampLow: 1 }),
      );
    }
    harness.server.activeSocket.emitBinary(item.frame(started.sessionId));
    assert.equal(
      harness.api.isActive(),
      false,
      `${item.name} must clear active`,
    );
  }
});

test("running AudioContext 批量收到合法 PCM 时有界重同步且不自动挂断", async () => {
  const harness = createHarness();
  const started = await startWithTrustedGesture(harness);
  for (let sequence = 0; sequence < 120; sequence += 1) {
    harness.server.activeSocket.emitBinary(pcmFrame(started.sessionId, {
      sequence,
      timestampLow: sequence + 1,
    }));
  }
  const context = harness.scenario.audioContexts.at(-1);
  const liveSources = context.sourceRecords.filter(
    (source) => source.started && !source.stopped,
  );
  assert.equal(harness.api.isActive(), true);
  assert.ok(
    liveSources.length <= 21,
    `scheduled source count must stay bounded, got ${liveSources.length}`,
  );
  assert.ok(
    Math.max(...liveSources.map((source) => source.at)) <= 0.4250001,
    "scheduled playback horizon must stay within 400ms plus startup margin",
  );
  assert.ok(
    context.sourceRecords.some(
      (source) => source.stopped && source.disconnected,
    ),
    "a burst must drop stale scheduled sources during resync",
  );
  await harness.api.stop("test");
});
