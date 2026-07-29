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
const ORIGIN = "https://bwicarus.taile44d0c.ts.net";
const PAIRING_CODE = "ABCDEFGHJK";
const DIRECT_CONTRACT = "reader-computer-voice-direct/1";
const AUTH_CONTRACT = "reader-computer-voice-auth/1";

const toBase64Url = (bytes) => Buffer.from(bytes)
  .toString("base64url");

function createIndexedDb() {
  let upgraded = false;
  const values = new Map();
  const db = {
    objectStoreNames: {
      contains(name) {
        return name === "identity" && upgraded;
      },
    },
    createObjectStore(name) {
      assert.equal(name, "identity");
      upgraded = true;
      return {};
    },
    transaction(name) {
      assert.equal(name, "identity");
      return {
        objectStore() {
          return {
            get(key) {
              const request = {};
              queueMicrotask(() => {
                request.result = values.get(key);
                request.onsuccess?.();
              });
              return request;
            },
            put(value, key) {
              const request = {};
              queueMicrotask(() => {
                values.set(key, value);
                request.result = key;
                request.onsuccess?.();
              });
              return request;
            },
            delete(key) {
              const request = {};
              queueMicrotask(() => {
                values.delete(key);
                request.onsuccess?.();
              });
              return request;
            },
          };
        },
      };
    },
  };
  return {
    open(name, version) {
      assert.equal(name, "bw-reader-computer-voice");
      assert.equal(version, 1);
      const request = {};
      queueMicrotask(() => {
        request.result = db;
        if (!upgraded) request.onupgradeneeded?.();
        request.onsuccess?.();
      });
      return request;
    },
  };
}

function createAudioContextClass(scenario) {
  return class FakeAudioContext {
    constructor(options) {
      assert.equal(options.sampleRate, 48000);
      this.sampleRate = 48000;
      this.state = scenario.initialAudioState || "suspended";
      this.currentTime = 0;
      this.destination = {};
      this.started = [];
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
      return {
        buffer: null,
        onended: null,
        connect() {},
        disconnect() {},
        start(at) {
          context.started.push({ at, buffer: this.buffer });
        },
        stop() {},
      };
    }
  };
}

function createServer(scenario) {
  const server = {
    requests: [],
    sockets: [],
    paired: false,
    publicKeySpki: null,
    fingerprint: null,
    authVerified: false,
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
        result(this, request, {
          protocolVersion: 1,
          paired: server.paired,
          authentication: "ecdsa-p256-sha256",
          signatureFormat: "ieee-p1363-fixed-64",
          challenge: {
            challengeId: "challenge-1",
            nonce: toBase64Url(Buffer.alloc(24, 7)),
            expiresAtUtc: new Date(Date.now() + 60_000).toISOString(),
            signingContract: AUTH_CONTRACT,
          },
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
      if (request.type === "pair") {
        assert.equal(request.pairingCode, PAIRING_CODE);
        server.publicKeySpki = Buffer.from(
          request.clientPublicKeySpki,
          "base64url",
        );
        void webcrypto.subtle.digest("SHA-256", server.publicKeySpki)
          .then((digest) => {
            server.fingerprint = toBase64Url(digest);
            server.paired = true;
            result(this, request, {
              paired: true,
              clientFingerprintSha256: server.fingerprint,
            });
          });
        return;
      }
      if (request.type === "auth") {
        if (scenario.authFailure) {
          failure(this, request, {
            code: "BW_COMPUTER_VOICE_DIRECT_AUTH_DENIED",
            message: "client key revoked",
          });
          return;
        }
        const canonical = new TextEncoder().encode(
          `${AUTH_CONTRACT}\nchallenge-1\n` +
          `${toBase64Url(Buffer.alloc(24, 7))}\n${ORIGIN}`,
        );
        void webcrypto.subtle.importKey(
          "spki",
          server.publicKeySpki,
          { name: "ECDSA", namedCurve: "P-256" },
          false,
          ["verify"],
        ).then((publicKey) => webcrypto.subtle.verify(
          { name: "ECDSA", hash: "SHA-256" },
          publicKey,
          Buffer.from(request.signature, "base64url"),
          canonical,
        )).then((verified) => {
          server.authVerified = verified;
          if (!verified) {
            failure(this, request, {
              code: "BW_COMPUTER_VOICE_DIRECT_AUTH",
              message: "bad signature",
            });
            return;
          }
          result(this, request, {
            authenticated: true,
            clientFingerprintSha256: server.fingerprint,
          });
        });
        return;
      }
      if (request.type === "status") {
        if (scenario.binaryOnStatus) {
          queueMicrotask(() => this.onmessage?.({ data: new ArrayBuffer(1956) }));
        }
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
    authFailure: false,
    deferStartResult: false,
    initialAudioState: "suspended",
    resumePlan: [],
    audioContexts: [],
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
  const document = {
    addEventListener(type, handler, capture) {
      if (type === "click") {
        assert.equal(capture, true);
        clickHandlers.push(handler);
      }
    },
  };
  const window = {
    RC: {},
    WebSocket: server.WebSocket,
    AudioContext: createAudioContextClass(scenario),
    crypto: webcrypto,
    indexedDB: createIndexedDb(),
    isSecureContext: true,
    location: { origin: ORIGIN },
    setTimeout: scheduleTimeout,
    clearTimeout: cancelTimeout,
    dispatchEvent() {},
  };
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
  return {
    api: window.RC.computerVoice,
    scenario,
    server,
    clickHandlers,
  };
}

async function pair(harness) {
  const paired = await harness.api.beginPairing({
    endpoint: ENDPOINT,
    pairingCode: PAIRING_CODE,
  });
  assert.equal(paired.paired, true);
  assert.equal(harness.server.authVerified, true);
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

test("直连配对动态验证不可导出 P-256 challenge 签名，STATUS 不发送 START", async () => {
  const harness = createHarness();
  await pair(harness);
  const availability = await harness.api.availability();
  assert.equal(availability.state, "idle");
  assert.equal(harness.server.authVerified, true);
  assert.equal(
    harness.server.requests.filter((request) => request.type === "start").length,
    0,
  );
  const pairRequest = harness.server.requests.find(
    (request) => request.type === "pair",
  );
  assert.deepEqual(
    Object.keys(pairRequest).sort(),
    [
      "clientPublicKeySpki",
      "contract",
      "pairingCode",
      "requestId",
      "type",
    ],
  );
  assert.doesNotMatch(SOURCE, /生成一次性配对码|localStorage|Bearer /);
  assert.match(SOURCE, />连接 Windows 桥接器<\/button>/);
  await assert.rejects(
    harness.api.beginPairing({
      endpoint: "wss://bwicarus-2.taile44d0c.ts.net/not-the-bridge",
      pairingCode: PAIRING_CODE,
    }),
    /只允许已固定的 Windows 电脑语音 WSS 地址/,
  );
  await assert.rejects(
    harness.api.beginPairing({
      endpoint:
        "wss://another-node.taile44d0c.ts.net/reader-computer-voice/v1",
      pairingCode: PAIRING_CODE,
    }),
    /只允许已固定的 Windows 电脑语音 WSS 地址/,
  );

  const normalized = createHarness();
  const normalizedPair = await normalized.api.beginPairing({
    endpoint:
      "WSS://BWICARUS-2.TAILE44D0C.TS.NET:443/reader-computer-voice/v1",
    pairingCode: PAIRING_CODE,
  });
  assert.equal(normalizedPair.endpoint, ENDPOINT);
  assert.equal(normalized.server.sockets[0].url, ENDPOINT);
});

test("Windows 离线明确返回 offline，且不回退 Pi 或发送 START", async () => {
  const harness = createHarness();
  await pair(harness);
  harness.scenario.offline = true;
  const availability = await harness.api.availability();
  assert.equal(availability.paired, true);
  assert.equal(availability.state, "offline");
  assert.equal(
    harness.server.requests.filter((request) => request.type === "start").length,
    0,
  );
  assert.doesNotMatch(SOURCE, /\/api\/reader\/computer-voice/);
});

test("Windows 重新配对后可复用本页私钥恢复，忘记身份不访问 Windows", async () => {
  const harness = createHarness();
  await pair(harness);
  const firstSpki = harness.server.requests.find(
    (request) => request.type === "pair",
  ).clientPublicKeySpki;

  harness.scenario.authFailure = true;
  const stale = await harness.api.availability();
  assert.equal(stale.state, "auth-failed");
  assert.equal(stale.endpoint, ENDPOINT);
  assert.match(
    SOURCE,
    /!value\.paired \|\| value\.state === "auth-failed"[\s\S]*setup\.style\.display = ""/,
  );

  harness.scenario.authFailure = false;
  await pair(harness);
  const pairRequests = harness.server.requests.filter(
    (request) => request.type === "pair",
  );
  assert.equal(pairRequests.at(-1).clientPublicKeySpki, firstSpki);

  const beforeForget = harness.server.requests.length;
  await harness.api.forgetIdentity();
  assert.equal(harness.server.requests.length, beforeForget);
  const forgotten = await harness.api.availability();
  assert.equal(forgotten.paired, false);
});

test("START 失败完整清 active/AudioContext/WSS，下一次拨号可重试", async () => {
  const harness = createHarness({
    startError: {
      code: "BW_COMPUTER_VOICE_DIRECT_APP_LAUNCHER_NOT_WIRED",
      message: "launcher not wired",
    },
  });
  await pair(harness);
  await assert.rejects(
    harness.api.startFromUserGesture(),
    /launcher not wired/,
  );
  assert.equal(harness.api.isActive(), false);
  assert.equal(harness.scenario.audioContexts.at(-1).closed, true);

  harness.scenario.startError = null;
  const started = await harness.api.startFromUserGesture();
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
  await pair(harness);

  const starting = harness.api.startFromUserGesture().then(
    () => null,
    (error) => error,
  );
  await waitForRequest(harness, "start");
  assert.equal(harness.api.isActive(), true);

  const event = {
    target: { id: "asst-call", parentNode: null },
    prevented: false,
    stopped: false,
    preventDefault() { this.prevented = true; },
    stopImmediatePropagation() { this.stopped = true; },
  };
  harness.clickHandlers[0](event);
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
  await pair(harness);
  await harness.api.availability();
  assert.equal(
    harness.server.requests.filter(
      (request) => request.type === "heartbeat",
    ).length,
    0,
  );

  const started = await harness.api.startFromUserGesture();
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
  await pair(harness);
  await harness.api.startFromUserGesture();
  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(statuses.includes("audio-blocked"));
  assert.equal(harness.api.isActive(), true);

  const event = {
    target: { id: "asst-call", parentNode: null },
    prevented: false,
    stopped: false,
    preventDefault() { this.prevented = true; },
    stopImmediatePropagation() { this.stopped = true; },
  };
  harness.clickHandlers[0](event);
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

test("已授权 START 的首个 PCM 可先于 result，STATUS 连接上的 PCM 仍被拒绝", async () => {
  const early = createHarness({
    framesBeforeStartResult: [
      (sessionId) => pcmFrame(sessionId),
    ],
  });
  await pair(early);
  const started = await early.api.startFromUserGesture();
  assert.equal(started.ok, true);
  assert.equal(early.api.isActive(), true);
  assert.equal(early.scenario.audioContexts.at(-1).started.length, 1);
  await early.api.stop("test");

  const unsolicited = createHarness({ binaryOnStatus: true });
  await pair(unsolicited);
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
    await pair(harness);
    const started = await harness.api.startFromUserGesture();
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

  const overflow = createHarness();
  await pair(overflow);
  const started = await overflow.api.startFromUserGesture();
  for (let sequence = 0; sequence < 21 && overflow.api.isActive(); sequence += 1) {
    overflow.server.activeSocket.emitBinary(pcmFrame(started.sessionId, {
      sequence,
      timestampLow: sequence + 1,
    }));
  }
  assert.equal(overflow.api.isActive(), false, "PCM queue must be bounded");
});
