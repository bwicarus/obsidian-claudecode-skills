import test from "node:test";
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { createRequire } from "node:module";
import {
  DataRegistry,
  makeStore,
} from "./helpers.mjs";

const require = createRequire(import.meta.url);
const Host = require(
  "../../_server_deploy/static/reader-runtime/direct-sync-host.js",
);
const Direct = require(
  "../../_server_deploy/static/reader-runtime/direct-sync-protocol.js",
);
const SyncGateway = require(
  "../../_server_deploy/static/reader-runtime/sync-gateway.js",
);
const Signals = require(
  "../../_server_deploy/static/reader-runtime/direct-sync-signal-transport.js",
);

const PROOF = `account-proof-v1-${"c".repeat(64)}`;
const NEXT_PROOF = `account-proof-v1-${"d".repeat(64)}`;
const DIGEST = DataRegistry.syncDigest();

function timers() {
  let next = 0;
  const jobs = new Map();
  return {
    setTimeout(fn, delay) {
      const id = ++next;
      jobs.set(id, { fn, delay });
      return id;
    },
    clearTimeout(id) {
      jobs.delete(id);
    },
    size() {
      return jobs.size;
    },
    runDelay(delay) {
      for (const [id, job] of jobs) {
        if (job.delay !== delay) continue;
        jobs.delete(id);
        job.fn();
        return true;
      }
      return false;
    },
  };
}

test("RTC 宿主只接受与本机 DataRegistry 完全一致的 sync-v3 因果摘要", () => {
  const base = {
    deviceId: "device-a",
    signalTransport: {
      contract: "direct-signal/1",
      async exchange() {},
    },
    syncRuntime: {
      addPeer() {},
      removePeer() {},
      status() {},
      schedule() {},
    },
    store: makeStore("registry-gate"),
    registry: DataRegistry,
    directProtocolApi: Direct,
    syncGatewayApi: SyncGateway,
    RTCPeerConnection: function FakePeerConnection() {},
    crypto: webcrypto,
  };
  for (const registryDigest of [
    "sync-v1:user-settings|vocabulary-state",
    "sync-v3:record-parent-state/1|user-settings:explicit:0:2|vocabulary-state:explicit:0:1",
    "sync-v3:forged",
  ]) {
    assert.throws(
      () => Host.createDirectSyncHost({ ...base, registryDigest }),
      (error) => error?.code === "BW_DIRECT_HOST_REGISTRY",
    );
  }
});

class FakeChannel {
  constructor() {
    this.readyState = "connecting";
    this.listeners = new Map();
    this.sent = [];
    this.closed = false;
  }
  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(listener);
  }
  removeEventListener(type, listener) {
    this.listeners.get(type)?.delete(listener);
  }
  emit(type, event = {}) {
    for (const listener of this.listeners.get(type) || []) listener(event);
  }
  open() {
    this.readyState = "open";
    this.emit("open");
  }
  send(value) {
    this.sent.push(value);
  }
  close() {
    this.closed = true;
    this.readyState = "closed";
  }
}

function peerConnectionFactory(instances) {
  return class FakePeerConnection {
    constructor(configuration) {
      this.configuration = configuration;
      this.listeners = new Map();
      this.connectionState = "new";
      this.localDescription = null;
      this.remoteDescription = null;
      this.channel = null;
      this.closed = false;
      instances.push(this);
    }
    addEventListener(type, listener) {
      if (!this.listeners.has(type)) this.listeners.set(type, new Set());
      this.listeners.get(type).add(listener);
    }
    removeEventListener(type, listener) {
      this.listeners.get(type)?.delete(listener);
    }
    emit(type, event = {}) {
      for (const listener of this.listeners.get(type) || []) listener(event);
    }
    createDataChannel() {
      this.channel = new FakeChannel();
      return this.channel;
    }
    async createOffer() {
      return { type: "offer", sdp: "offer-sdp" };
    }
    async createAnswer() {
      return { type: "answer", sdp: "answer-sdp" };
    }
    async setLocalDescription(value) {
      this.localDescription = structuredClone(value);
    }
    async setRemoteDescription(value) {
      this.remoteDescription = structuredClone(value);
    }
    async addIceCandidate() {}
    close() {
      this.closed = true;
      this.connectionState = "closed";
    }
  };
}

function fakeRuntime() {
  const state = {
    added: [],
    removed: [],
    scheduled: [],
    ready: true,
    cursor: 4,
    localCursor: 3,
  };
  return {
    state,
    addPeer(peerId, gateway, options) {
      state.added.push({ peerId, gateway, options });
    },
    removePeer(peerId) {
      state.removed.push(peerId);
    },
    schedule(reason, delay) {
      state.scheduled.push({ reason, delay });
      return true;
    },
    async status() {
      return {
        paused: false,
        lastResult: {
          server: {
            ok: state.ready,
            pendingLocal: false,
            conflicts: [],
          },
          checkpoint: {
            server: {
              localCursor: state.localCursor,
              remoteCursor: state.cursor,
            },
          },
        },
        coordinator: {
          checkpoint: {
            server: {
              localCursor: state.localCursor,
              remoteCursor: state.cursor,
            },
          },
        },
      };
    },
  };
}

function signalResponse(request, overrides = {}) {
  return {
    contract: "direct-signal/1",
    accountProof: PROOF,
    headCursor: 4,
    baselineReady: request.serverReady,
    signalCursor: request.signalCursor,
    ackedSignalIds: (request.signals || []).map((signal) => signal.signalId),
    signalResetRequired: false,
    hasMore: false,
    peers: [],
    signals: [],
    ...overrides,
  };
}

test("只有 live server baseline peer 才创建 RTC，开通后注册到共享 SyncRuntime", async () => {
  const clock = timers();
  const pcs = [];
  const requests = [];
  let baseline = false;
  const signalTransport = {
    contract: "direct-signal/1",
    async exchange(request) {
      requests.push(structuredClone(request));
      return signalResponse(request, {
        peers: [{
          deviceId: "device-b",
          baselineReady: baseline,
          baselineLocalCursor: baseline ? 6 : 0,
        }],
      });
    },
  };
  const runtime = fakeRuntime();
  const host = Host.createDirectSyncHost({
    deviceId: "device-a",
    registryDigest: DIGEST,
    signalTransport,
    syncRuntime: runtime,
    store: makeStore("direct-host-a"),
    registry: DataRegistry,
    directProtocolApi: Direct,
    syncGatewayApi: SyncGateway,
    RTCPeerConnection: peerConnectionFactory(pcs),
    crypto: webcrypto,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });

  host.start("test");
  await host.poll();
  assert.equal(pcs.length, 0);
  assert.equal(requests[0].serverCursor, 4);
  assert.equal(requests[0].localCursor, 3);
  assert.equal(requests[0].serverReady, true);

  baseline = true;
  await host.poll();
  assert.equal(pcs.length, 1);
  assert.deepEqual(pcs[0].configuration, { iceServers: [] });
  assert.equal(host.status().pendingSignals, 1);

  await host.poll();
  assert.equal(requests.at(-1).signals[0].kind, "offer");
  assert.match(
    requests.at(-1).signals[0].signalId,
    /^device-a:/,
  );
  pcs[0].channel.open();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(runtime.state.added.length, 1);
  assert.equal(runtime.state.added[0].peerId, "device-b");
  assert.equal(runtime.state.added[0].options.baselineReady, true);
  assert.equal(runtime.state.added[0].options.baselineLocalCursor, 3);
  assert.equal(runtime.state.added[0].options.baselineRemoteCursor, 6);

  baseline = false;
  await host.poll();
  assert.equal(runtime.state.removed.includes("device-b"), true);
  assert.equal(pcs[0].closed, true);
  host.destroy();
});

test("同一宿主生命周期内账户证明变化会 fail closed 且停止调度", async () => {
  const clock = timers();
  const pcs = [];
  let exchanges = 0;
  const runtime = fakeRuntime();
  const host = Host.createDirectSyncHost({
    deviceId: "device-a",
    registryDigest: DIGEST,
    signalTransport: {
      contract: "direct-signal/1",
      async exchange(request) {
        exchanges += 1;
        return signalResponse(request, {
          accountProof: exchanges === 1 ? PROOF : NEXT_PROOF,
          peers: [{
            deviceId: "device-b",
            baselineReady: true,
            baselineLocalCursor: 4,
          }],
        });
      },
    },
    syncRuntime: runtime,
    store: makeStore("direct-host-proof-generation"),
    registry: DataRegistry,
    directProtocolApi: Direct,
    syncGatewayApi: SyncGateway,
    RTCPeerConnection: peerConnectionFactory(pcs),
    crypto: webcrypto,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });

  host.start("test");
  await host.poll();
  assert.equal(pcs.length, 1);
  pcs[0].channel.open();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(runtime.state.added.length, 1);
  await assert.rejects(
    host.poll(),
    (error) => error?.code === "BW_DIRECT_HOST_PROOF" &&
      error.retryable === false,
  );
  assert.equal(host.status().lastError.code, "BW_DIRECT_HOST_PROOF");
  assert.deepEqual(host.status().peers, []);
  assert.equal(host.status().pendingSignals, 0);
  assert.equal(pcs[0].closed, true);
  assert.equal(runtime.state.removed.includes("device-b"), true);
  assert.equal(clock.size(), 0, "证明代际突变后不得继续自动信令");
});

test("信令响应缺失或畸形账户证明会关闭既有 peer 并 fail closed", async () => {
  for (const invalidProof of [undefined, "account-proof-v1-short"]) {
    const clock = timers();
    const pcs = [];
    let exchanges = 0;
    const runtime = fakeRuntime();
    const signalTransport = Signals.createDirectSignalTransport({
      deviceId: "device-a",
      registryDigest: DIGEST,
      exchange: async (request) => {
        exchanges += 1;
        return signalResponse(request, {
          accountProof: exchanges === 1 ? PROOF : invalidProof,
          peers: [{
            deviceId: "device-b",
            baselineReady: true,
            baselineLocalCursor: 4,
          }],
        });
      },
    });
    const host = Host.createDirectSyncHost({
      deviceId: "device-a",
      registryDigest: DIGEST,
      signalTransport,
      syncRuntime: runtime,
      store: makeStore(
        `direct-host-invalid-proof-${invalidProof === undefined ? "missing" : "malformed"}`,
      ),
      registry: DataRegistry,
      directProtocolApi: Direct,
      syncGatewayApi: SyncGateway,
      RTCPeerConnection: peerConnectionFactory(pcs),
      crypto: webcrypto,
      setTimeout: clock.setTimeout,
      clearTimeout: clock.clearTimeout,
    });

    host.start("test");
    await host.poll();
    assert.equal(pcs.length, 1);
    pcs[0].channel.open();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(runtime.state.added.length, 1);
    await assert.rejects(
      host.poll(),
      (error) => error?.code === "BW_DIRECT_SIGNAL_INVALID" &&
        error.retryable === false,
    );
    assert.deepEqual(host.status().peers, []);
    assert.equal(host.status().pendingSignals, 0);
    assert.equal(pcs[0].closed, true);
    assert.equal(runtime.state.removed.includes("device-b"), true);
    assert.equal(clock.size(), 0, "坏证明响应后不得继续自动信令");
  }
});

test("pause generation fence 丢弃晚到信令，不创建 RTC 或安排重试", async () => {
  const clock = timers();
  const pcs = [];
  let release;
  let started;
  const gate = new Promise((resolve) => { release = resolve; });
  const called = new Promise((resolve) => { started = resolve; });
  const signalTransport = {
    contract: "direct-signal/1",
    async exchange(request) {
      started();
      await gate;
      return signalResponse(request, {
        peers: [{ deviceId: "device-b", baselineReady: true }],
      });
    },
  };
  const host = Host.createDirectSyncHost({
    deviceId: "device-a",
    registryDigest: DIGEST,
    signalTransport,
    syncRuntime: fakeRuntime(),
    store: makeStore("direct-host-fence"),
    registry: DataRegistry,
    directProtocolApi: Direct,
    syncGatewayApi: SyncGateway,
    RTCPeerConnection: peerConnectionFactory(pcs),
    crypto: webcrypto,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });

  host.start("test");
  const pending = host.poll();
  await called;
  host.pause("extension-takeover");
  release();
  await assert.rejects(
    pending,
    (error) => error.code === "BW_DIRECT_HOST_INACTIVE",
  );
  assert.equal(pcs.length, 0);
  assert.equal(clock.size(), 0);
  host.destroy();
});

test("durable server head 前进时丢弃旧信令并重建 RTC 会话", async () => {
  const clock = timers();
  const pcs = [];
  const requests = [];
  const statuses = [];
  const runtime = fakeRuntime();
  const signalTransport = {
    contract: "direct-signal/1",
    async exchange(request) {
      requests.push(structuredClone(request));
      return signalResponse(request, {
        headCursor: runtime.state.cursor,
        peers: [{
          deviceId: "device-b",
          baselineReady: true,
          baselineLocalCursor: runtime.state.localCursor,
        }],
      });
    },
  };
  const host = Host.createDirectSyncHost({
    deviceId: "device-a",
    registryDigest: DIGEST,
    signalTransport,
    syncRuntime: runtime,
    store: makeStore("direct-host-head"),
    registry: DataRegistry,
    directProtocolApi: Direct,
    syncGatewayApi: SyncGateway,
    RTCPeerConnection: peerConnectionFactory(pcs),
    crypto: webcrypto,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
    onStatus(value) {
      statuses.push(value);
    },
  });

  host.start("test");
  await host.poll();
  assert.equal(pcs.length, 1);
  assert.equal(host.status().pendingSignals, 1);

  runtime.state.cursor = 5;
  runtime.state.localCursor = 5;
  await host.poll();

  assert.equal(requests.at(-1).serverCursor, 5);
  assert.deepEqual(requests.at(-1).signals, []);
  assert.equal(pcs[0].closed, true);
  assert.equal(pcs.length, 2);
  assert.equal(host.status().pendingSignals, 1);
  assert.equal(
    statuses.some((value) => (
      value.state === "baseline-changed" &&
      value.detail.previousHeadCursor === 4 &&
      value.detail.headCursor === 5
    )),
    true,
  );
  host.destroy();
});

test("ICE 信令按服务端上限分批，积压超过硬上限时只停直连", async () => {
  const clock = timers();
  const pcs = [];
  const requests = [];
  const runtime = fakeRuntime();
  const signalTransport = {
    contract: "direct-signal/1",
    async exchange(request) {
      requests.push(structuredClone(request));
      return signalResponse(request, {
        peers: [{
          deviceId: "device-b",
          baselineReady: true,
          baselineLocalCursor: 4,
        }],
      });
    },
  };
  const host = Host.createDirectSyncHost({
    deviceId: "device-a",
    registryDigest: DIGEST,
    signalTransport,
    syncRuntime: runtime,
    store: makeStore("direct-host-signal-bounds"),
    registry: DataRegistry,
    directProtocolApi: Direct,
    syncGatewayApi: SyncGateway,
    RTCPeerConnection: peerConnectionFactory(pcs),
    crypto: webcrypto,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });

  host.start("test");
  await host.poll();
  for (let index = 0; index < 40; index += 1) {
    pcs[0].emit("icecandidate", {
      candidate: {
        candidate: `candidate:${index}`,
        sdpMid: "0",
        sdpMLineIndex: 0,
      },
    });
  }
  assert.equal(host.status().pendingSignals, 41);
  await host.poll();
  assert.equal(requests.at(-1).signals.length, 32);
  assert.equal(host.status().pendingSignals, 9);
  await host.poll();
  assert.equal(requests.at(-1).signals.length, 9);
  assert.equal(host.status().pendingSignals, 0);

  for (let index = 0; index < 257; index += 1) {
    pcs.at(-1).emit("icecandidate", {
      candidate: {
        candidate: `overflow:${index}`,
        sdpMid: "0",
        sdpMLineIndex: 0,
      },
    });
  }
  assert.equal(host.status().paused, true);
  assert.equal(host.status().pendingSignals, 0);
  assert.equal(host.status().lastError.code, "BW_DIRECT_SIGNAL_QUEUE_FULL");
  assert.equal(runtime.state.removed.includes("device-b"), true);
  host.destroy();
});

test("请求方 baseline 未就绪时无视响应中的 ready peer 并推动 durable sync", async () => {
  const clock = timers();
  const pcs = [];
  const runtime = fakeRuntime();
  runtime.state.ready = false;
  const signalTransport = {
    contract: "direct-signal/1",
    async exchange(request) {
      return signalResponse(request, {
        baselineReady: false,
        peers: [{
          deviceId: "device-b",
          baselineReady: true,
          baselineLocalCursor: 8,
        }],
      });
    },
  };
  const host = Host.createDirectSyncHost({
    deviceId: "device-a",
    registryDigest: DIGEST,
    signalTransport,
    syncRuntime: runtime,
    store: makeStore("direct-host-unready"),
    registry: DataRegistry,
    directProtocolApi: Direct,
    syncGatewayApi: SyncGateway,
    RTCPeerConnection: peerConnectionFactory(pcs),
    crypto: webcrypto,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });

  host.start("test");
  await host.poll();
  assert.equal(pcs.length, 0);
  assert.deepEqual(host.status().baselinePeers, []);
  assert.equal(host.status().lastExchange.baselineReady, false);
  assert.equal(
    runtime.state.scheduled.some(({ reason }) => (
      reason.includes("local-server-baseline-not-ready")
    )),
    true,
  );
  host.destroy();
});

test("正常 baseline 竞态 409 会清理旧会话并按有界间隔重试", async () => {
  const clock = timers();
  const pcs = [];
  let calls = 0;
  const runtime = fakeRuntime();
  const signalTransport = {
    contract: "direct-signal/1",
    async exchange(request) {
      calls += 1;
      if (calls === 1) {
        return signalResponse(request, {
          peers: [{
            deviceId: "device-b",
            baselineReady: true,
            baselineLocalCursor: 4,
          }],
        });
      }
      const error = new Error("baseline advanced");
      error.code = "BW_DIRECT_BASELINE_REQUIRED";
      error.retryable = false;
      throw error;
    },
  };
  const host = Host.createDirectSyncHost({
    deviceId: "device-a",
    registryDigest: DIGEST,
    signalTransport,
    syncRuntime: runtime,
    store: makeStore("direct-host-race"),
    registry: DataRegistry,
    directProtocolApi: Direct,
    syncGatewayApi: SyncGateway,
    RTCPeerConnection: peerConnectionFactory(pcs),
    crypto: webcrypto,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
    pollMs: 1_234,
  });

  host.start("test");
  await host.poll();
  assert.equal(host.status().pendingSignals, 1);
  await assert.rejects(
    host.poll(),
    (error) => error.code === "BW_DIRECT_BASELINE_REQUIRED",
  );
  assert.equal(host.status().paused, false);
  assert.equal(host.status().pendingSignals, 0);
  assert.equal(pcs[0].closed, true);
  assert.equal(clock.size(), 1);
  assert.equal(
    runtime.state.scheduled.some(({ reason }) => (
      reason.includes("BW_DIRECT_BASELINE_REQUIRED")
    )),
    true,
  );
  host.destroy();
});

test("offer 发出后协商超时会回收旧 RTC，并在下一轮创建新会话重试", async () => {
  const clock = timers();
  const pcs = [];
  const requests = [];
  const runtime = fakeRuntime();
  const signalTransport = {
    contract: "direct-signal/1",
    async exchange(request) {
      requests.push(structuredClone(request));
      return signalResponse(request, {
        peers: [{
          deviceId: "device-b",
          baselineReady: true,
          baselineLocalCursor: 4,
        }],
      });
    },
  };
  const host = Host.createDirectSyncHost({
    deviceId: "device-a",
    registryDigest: DIGEST,
    signalTransport,
    syncRuntime: runtime,
    store: makeStore("direct-host-offer-timeout"),
    registry: DataRegistry,
    directProtocolApi: Direct,
    syncGatewayApi: SyncGateway,
    RTCPeerConnection: peerConnectionFactory(pcs),
    crypto: webcrypto,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
    negotiationTimeoutMs: 1_500,
  });

  host.start("test");
  await host.poll();
  assert.equal(pcs.length, 1);
  assert.equal(host.status().pendingSignals, 1);
  assert.equal(clock.runDelay(1_500), true);

  assert.equal(pcs[0].closed, true);
  assert.deepEqual(host.status().peers, []);
  assert.equal(host.status().lastError.code, "BW_DIRECT_NEGOTIATION_TIMEOUT");
  assert.equal(
    runtime.state.scheduled.some(({ reason }) => (
      reason === "direct-negotiation-timeout:device-b"
    )),
    true,
  );

  await host.poll();
  assert.equal(requests.at(-1).signals[0].kind, "bye");
  const expiredSessionId = requests.at(-1).signals[0].sessionId;
  assert.equal(pcs.length, 2);
  assert.equal(pcs[1].closed, false);
  assert.equal(host.status().pendingSignals, 1);
  await host.poll();
  assert.equal(requests.at(-1).signals[0].kind, "offer");
  assert.notEqual(
    requests.at(-1).signals[0].sessionId,
    expiredSessionId,
  );
  host.destroy();
});

test("answer 发出后 DataChannel 未开启也会超时回收并通知远端重试", async () => {
  const clock = timers();
  const pcs = [];
  const requests = [];
  let calls = 0;
  const incomingOffer = {
    signalId: "remote-offer-1",
    fromDeviceId: "device-0",
    sessionId: "direct-session-v1-remote",
    kind: "offer",
    payload: { type: "offer", sdp: "remote-offer-sdp" },
  };
  const signalTransport = {
    contract: "direct-signal/1",
    async exchange(request) {
      calls += 1;
      requests.push(structuredClone(request));
      return signalResponse(request, {
        signalCursor: 1,
        peers: [{
          deviceId: "device-0",
          baselineReady: true,
          baselineLocalCursor: 4,
        }],
        signals: calls === 1 ? [incomingOffer] : [],
      });
    },
  };
  const host = Host.createDirectSyncHost({
    deviceId: "device-a",
    registryDigest: DIGEST,
    signalTransport,
    syncRuntime: fakeRuntime(),
    store: makeStore("direct-host-answer-timeout"),
    registry: DataRegistry,
    directProtocolApi: Direct,
    syncGatewayApi: SyncGateway,
    RTCPeerConnection: peerConnectionFactory(pcs),
    crypto: webcrypto,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
    negotiationTimeoutMs: 1_500,
  });

  host.start("test");
  await host.poll();
  assert.equal(pcs.length, 1);
  assert.equal(host.status().pendingSignals, 1);
  await host.poll();
  assert.equal(requests.at(-1).signals[0].kind, "answer");
  assert.equal(host.status().pendingSignals, 0);

  assert.equal(clock.runDelay(1_500), true);
  assert.equal(pcs[0].closed, true);
  assert.deepEqual(host.status().peers, []);
  assert.equal(host.status().pendingSignals, 1);
  await host.poll();
  assert.equal(requests.at(-1).signals[0].kind, "bye");
  assert.equal(pcs.length, 1, "应答方等待远端 offerer 用新 session 发起重试");
  host.destroy();
});

test("只有 signalResetRequired 才允许客户端信令游标回退到服务端值", async () => {
  const clock = timers();
  const pcs = [];
  const requests = [];
  let calls = 0;
  const signalTransport = {
    contract: "direct-signal/1",
    async exchange(request) {
      calls += 1;
      requests.push(structuredClone(request));
      if (calls === 1) {
        return signalResponse(request, { signalCursor: 9 });
      }
      if (calls === 2) {
        return signalResponse(request, { signalCursor: 3 });
      }
      if (calls === 3) {
        return signalResponse(request, {
          signalCursor: 2,
          signalResetRequired: true,
        });
      }
      return signalResponse(request, { signalCursor: 2 });
    },
  };
  const host = Host.createDirectSyncHost({
    deviceId: "device-a",
    registryDigest: DIGEST,
    signalTransport,
    syncRuntime: fakeRuntime(),
    store: makeStore("direct-host-cursor-reset"),
    registry: DataRegistry,
    directProtocolApi: Direct,
    syncGatewayApi: SyncGateway,
    RTCPeerConnection: peerConnectionFactory(pcs),
    crypto: webcrypto,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });

  host.start("test");
  await host.poll();
  assert.equal(host.status().signalCursor, 9);
  await host.poll();
  assert.equal(requests[1].signalCursor, 9);
  assert.equal(host.status().signalCursor, 9, "普通响应不得让游标倒退");
  await host.poll();
  assert.equal(requests[2].signalCursor, 9);
  assert.equal(host.status().signalCursor, 2, "服务端 reset 必须覆盖客户端领先游标");
  await host.poll();
  assert.equal(requests[3].signalCursor, 2);
  host.destroy();
});
