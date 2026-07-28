import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const WebRtc = require(
  "../../_server_deploy/static/reader-runtime/computer-voice-webrtc.js",
);
const SOURCE_PATH =
  "_server_deploy/static/reader-runtime/computer-voice-webrtc.js";

const SESSION_ID = "voice-session-1";
const OFFER_SDP = [
  "v=0",
  "m=audio 9 UDP/TLS/RTP/SAVPF 111",
  "a=mid:0",
  "m=audio 9 UDP/TLS/RTP/SAVPF 111",
  "a=mid:1",
  "",
].join("\r\n");
const ANSWER_SDP = [
  "v=0",
  "m=audio 9 UDP/TLS/RTP/SAVPF 111",
  "a=mid:0",
  "m=audio 9 UDP/TLS/RTP/SAVPF 111",
  "a=mid:1",
  "",
].join("\r\n");

function gate(overrides = {}) {
  return {
    paired: true,
    localOptIn: true,
    oneTimeTrigger: true,
    nativeReady: true,
    ...overrides,
  };
}

function tracks() {
  return [
    {
      trackId: "app-output",
      track: { id: "output-track", kind: "audio", label: "" },
    },
    {
      trackId: "user-mic",
      track: { id: "mic-track", kind: "audio", label: "" },
    },
  ];
}

function parseMids(sdp) {
  return String(sdp).split(/\r?\n/).filter(
    (line) => line.startsWith("a=mid:"),
  ).map((line) => line.slice(6));
}

class FakePeerConnection {
  static instances = [];

  constructor(configuration) {
    this.configuration = configuration;
    this.transceivers = [];
    this.listeners = new Map();
    this.connectionState = "new";
    this.iceConnectionState = "new";
    this.localDescription = null;
    this.remoteDescription = null;
    this.addedIce = [];
    this.closed = 0;
    FakePeerConnection.instances.push(this);
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

  addTrack(track) {
    const transceiver = {
      mid: null,
      direction: "sendrecv",
      sender: { track },
      receiver: { track: null },
    };
    this.transceivers.push(transceiver);
    return transceiver.sender;
  }

  addTransceiver(kind, init) {
    assert.equal(kind, "audio");
    assert.deepEqual(init, { direction: "recvonly" });
    const transceiver = {
      mid: null,
      direction: init.direction,
      sender: { track: null },
      receiver: { track: { kind: "audio" } },
    };
    this.transceivers.push(transceiver);
    return transceiver;
  }

  getTransceivers() {
    return this.transceivers.slice();
  }

  async createOffer() {
    return { type: "offer", sdp: OFFER_SDP };
  }

  async createAnswer() {
    return { type: "answer", sdp: ANSWER_SDP };
  }

  async setLocalDescription(description) {
    this.localDescription = description;
    parseMids(description.sdp).forEach((mid, index) => {
      if (this.transceivers[index]) this.transceivers[index].mid = mid;
    });
  }

  async setRemoteDescription(description) {
    this.remoteDescription = description;
    parseMids(description.sdp).forEach((mid, index) => {
      if (this.transceivers[index]) this.transceivers[index].mid = mid;
    });
  }

  async addIceCandidate(candidate) {
    this.addedIce.push(candidate);
  }

  setConnected() {
    this.connectionState = "connected";
    this.iceConnectionState = "connected";
    this.emit("connectionstatechange");
  }

  setDisconnected() {
    this.connectionState = "disconnected";
    this.iceConnectionState = "disconnected";
    this.emit("iceconnectionstatechange");
  }

  emitTrack(mid, track) {
    const transceiver = this.transceivers.find((item) => item.mid === mid) || {
      mid,
    };
    this.emit("track", { transceiver, track });
  }

  close() {
    this.closed += 1;
    this.connectionState = "closed";
    this.iceConnectionState = "closed";
  }
}

class FakeTimers {
  constructor() {
    this.nextId = 1;
    this.pending = new Map();
  }

  setTimeout = (callback, delay) => {
    const id = this.nextId++;
    this.pending.set(id, { callback, delay });
    return id;
  };

  clearTimeout = (id) => {
    this.pending.delete(id);
  };

  async runNext() {
    const entry = this.pending.entries().next().value;
    assert.ok(entry, "expected a scheduled poll");
    const [id, timer] = entry;
    this.pending.delete(id);
    await timer.callback();
    await Promise.resolve();
  }
}

class FakeSignalTransport {
  constructor(handler) {
    this.contract = WebRtc.SIGNAL_CONTRACT;
    this.requests = [];
    this.handler = handler || ((request) => ({
      contract: WebRtc.SIGNAL_CONTRACT,
      sessionId: request.sessionId,
      ackedSignalIds: request.signals.map((signal) => signal.signalId),
      signals: [],
      cursor: request.signals.length ? 1 : request.cursor,
    }));
  }

  async exchange(request) {
    this.requests.push(structuredClone(request));
    return this.handler(request, this.requests.length);
  }
}

function fixture(overrides = {}) {
  FakePeerConnection.instances = [];
  const timers = new FakeTimers();
  const transport = overrides.transport || new FakeSignalTransport();
  let now = 1_000;
  let signalSequence = 0;
  const statuses = [];
  const received = [];
  let releases = 0;
  const controller = WebRtc.createComputerVoiceWebRtcController({
    role: overrides.role || WebRtc.ROLE_WINDOWS_SENDER,
    sessionId: SESSION_ID,
    RTCPeerConnection: FakePeerConnection,
    signalTransport: transport,
    clock: () => now,
    setTimeout: timers.setTimeout,
    clearTimeout: timers.clearTimeout,
    signalIdFactory: () => `signal-${++signalSequence}`,
    localTracks: overrides.localTracks === undefined
      ? tracks()
      : overrides.localTracks,
    pollIntervalMs: overrides.pollIntervalMs || 100,
    negotiationTimeoutMs: overrides.negotiationTimeoutMs || 5_000,
    maxPolls: overrides.maxPolls || 8,
    onStatus: (status) => statuses.push(status),
    onTrack: (entry) => received.push(entry),
    releaseLocalTracks: () => { releases += 1; },
  });
  return {
    controller,
    transport,
    timers,
    statuses,
    received,
    setNow(value) { now = value; },
    get releases() { return releases; },
  };
}

test("模块加载不会构建连接或调用浏览器、网络和媒体捕获 API", () => {
  assert.equal(FakePeerConnection.instances.length, 0);
  const source = fs.readFileSync(SOURCE_PATH, "utf8");
  assert.doesNotMatch(source, /\bfetch\s*\(/);
  assert.doesNotMatch(source, /\bnew\s+WebSocket\b/);
  assert.doesNotMatch(source, /\bgetUserMedia\s*\(/);
  assert.doesNotMatch(source, /\bcreateDataChannel\s*\(/);
  assert.doesNotMatch(source, /\bchrome\./);
});

test("paired + localOptIn + oneTimeTrigger + nativeReady 缺一即拒绝且不构建连接", async () => {
  for (const key of [
    "paired",
    "localOptIn",
    "oneTimeTrigger",
    "nativeReady",
  ]) {
    const { controller } = fixture();
    await assert.rejects(
      controller.start(gate({ [key]: false })),
      (error) => error.code === "BW_COMPUTER_VOICE_WEBRTC_GATE",
    );
    assert.equal(controller.status().state, "idle");
  }
  assert.equal(FakePeerConnection.instances.length, 0);
});

test("Windows sender 只在两条显式标记的 audio track 齐备时发送固定双音轨 offer", async () => {
  const { controller, transport } = fixture();
  await controller.start(gate());
  const peer = FakePeerConnection.instances[0];
  assert.deepEqual(peer.configuration, {
    iceServers: [],
    iceTransportPolicy: "all",
    bundlePolicy: "max-bundle",
  });
  assert.deepEqual(
    peer.getTransceivers().map((entry) => entry.sender.track.id),
    ["output-track", "mic-track"],
  );
  assert.equal(transport.requests.length, 1);
  assert.deepEqual(
    transport.requests[0].signals.map((signal) => signal.kind),
    ["offer"],
  );
  assert.deepEqual(Object.keys(transport.requests[0]).sort(), [
    "contract",
    "cursor",
    "sessionId",
    "signals",
  ]);

  for (const invalid of [
    [{ trackId: "app-output", track: { kind: "audio" } }],
    [
      { trackId: "app-output", track: { kind: "audio" } },
      { trackId: "user-mic", track: { kind: "video" } },
    ],
    [
      { trackId: "app-output", track: { kind: "audio" } },
      { trackId: "app-output", track: { kind: "audio" } },
    ],
    [
      { trackId: "system-output", track: { kind: "audio" } },
      { trackId: "user-mic", track: { kind: "audio" } },
    ],
  ]) {
    const bad = fixture({ localTracks: invalid });
    await assert.rejects(
      bad.controller.start(gate()),
      (error) => error.code === "BW_COMPUTER_VOICE_WEBRTC_MEDIA_SCOPE",
    );
    assert.equal(bad.controller.status().state, "failed");
  }
});

test("未 ack 的 offer 使用同一 signalId 重发，answer 按 cursor 处理后连接且不自动重连", async () => {
  let offerId;
  const transport = new FakeSignalTransport((request, call) => {
    if (call === 1) {
      offerId = request.signals[0].signalId;
      return {
        contract: WebRtc.SIGNAL_CONTRACT,
        sessionId: SESSION_ID,
        ackedSignalIds: [],
        signals: [],
        cursor: 1,
      };
    }
    assert.equal(request.signals[0].signalId, offerId);
    return {
      contract: WebRtc.SIGNAL_CONTRACT,
      sessionId: SESSION_ID,
      ackedSignalIds: [offerId],
      signals: [{
        cursor: 2,
        signalId: "answer-1",
        kind: "answer",
        payload: { type: "answer", sdp: ANSWER_SDP },
      }],
      cursor: 2,
    };
  });
  const context = fixture({ transport });
  await context.controller.start(gate());
  await context.controller.poll();
  const peer = FakePeerConnection.instances[0];
  assert.equal(peer.remoteDescription.type, "answer");
  peer.setConnected();
  assert.equal(context.controller.status().state, "connected");
  assert.equal(context.controller.status().autoReconnect, false);
  assert.equal(FakePeerConnection.instances.length, 1);
});

test("Reader receiver 预建两个 recvonly transceiver 并按 offer mid 映射两条远端轨", async () => {
  const transport = new FakeSignalTransport((request, call) => {
    if (call === 1) {
      assert.deepEqual(request.signals, []);
      return {
        contract: WebRtc.SIGNAL_CONTRACT,
        sessionId: SESSION_ID,
        ackedSignalIds: [],
        signals: [{
          cursor: 1,
          signalId: "offer-remote-1",
          kind: "offer",
          payload: { type: "offer", sdp: OFFER_SDP },
        }],
        cursor: 1,
      };
    }
    return {
      contract: WebRtc.SIGNAL_CONTRACT,
      sessionId: SESSION_ID,
      ackedSignalIds: request.signals.map((signal) => signal.signalId),
      signals: [],
      cursor: 2,
    };
  });
  const context = fixture({
    role: WebRtc.ROLE_READER_RECEIVER,
    localTracks: null,
    transport,
  });
  await context.controller.start(gate());
  const peer = FakePeerConnection.instances[0];
  assert.deepEqual(
    peer.getTransceivers().map((entry) => entry.direction),
    ["recvonly", "recvonly"],
  );
  assert.equal(peer.remoteDescription.type, "offer");
  assert.equal(context.controller.status().pendingSignals, 1);
  await context.controller.poll();
  peer.emitTrack("0", { id: "remote-output", kind: "audio" });
  peer.emitTrack("1", { id: "remote-mic", kind: "audio" });
  peer.setConnected();
  assert.deepEqual(
    context.received.map(({ trackId, mid }) => ({ trackId, mid })),
    [
      { trackId: "app-output", mid: "0" },
      { trackId: "user-mic", mid: "1" },
    ],
  );
  assert.equal(context.controller.status().state, "connected");
});

test("Reader 拒绝未知 mid、额外轨和非 audio 轨并立即清理", async () => {
  const offerResponse = () => ({
    contract: WebRtc.SIGNAL_CONTRACT,
    sessionId: SESSION_ID,
    ackedSignalIds: [],
    signals: [{
      cursor: 1,
      signalId: "offer-remote-1",
      kind: "offer",
      payload: { type: "offer", sdp: OFFER_SDP },
    }],
    cursor: 1,
  });
  for (const emit of [
    (peer) => peer.emitTrack("9", { kind: "audio" }),
    (peer) => peer.emitTrack("0", { kind: "video" }),
    (peer) => {
      peer.emitTrack("0", { kind: "audio" });
      peer.emitTrack("0", { kind: "audio" });
    },
  ]) {
    const context = fixture({
      role: WebRtc.ROLE_READER_RECEIVER,
      localTracks: null,
      transport: new FakeSignalTransport(offerResponse),
    });
    await context.controller.start(gate());
    const peer = FakePeerConnection.instances[0];
    emit(peer);
    assert.equal(context.controller.status().state, "failed");
    assert.equal(peer.closed, 1);
    assert.equal(context.releases, 1);
  }
});

test("ICE 在 SDP 前有界缓冲、SDP 后应用；字段外数据和 video SDP 被拒绝", async () => {
  const transport = new FakeSignalTransport((request) => ({
    contract: WebRtc.SIGNAL_CONTRACT,
    sessionId: SESSION_ID,
    ackedSignalIds: [],
    signals: [
      {
        cursor: 1,
        signalId: "ice-before-offer",
        kind: "ice",
        payload: {
          candidate: "candidate:1 1 UDP 1 192.0.2.1 123 typ host",
          sdpMid: "0",
          sdpMLineIndex: 0,
        },
      },
      {
        cursor: 2,
        signalId: "offer-after-ice",
        kind: "offer",
        payload: { type: "offer", sdp: OFFER_SDP },
      },
    ],
    cursor: 2,
  }));
  const context = fixture({
    role: WebRtc.ROLE_READER_RECEIVER,
    localTracks: null,
    transport,
  });
  await context.controller.start(gate());
  assert.equal(FakePeerConnection.instances[0].addedIce.length, 1);

  const badPayload = fixture({
    role: WebRtc.ROLE_READER_RECEIVER,
    localTracks: null,
    transport: new FakeSignalTransport(() => ({
      contract: WebRtc.SIGNAL_CONTRACT,
      sessionId: SESSION_ID,
      ackedSignalIds: [],
      signals: [{
        cursor: 1,
        signalId: "smuggled-audio",
        kind: "ice",
        payload: {
          candidate: "candidate:1 1 UDP 1 192.0.2.1 123 typ host",
          sdpMid: "0",
          sdpMLineIndex: 0,
          pcm: "AAAA",
        },
      }],
      cursor: 1,
    })),
  });
  await assert.rejects(badPayload.controller.start(gate()));
  assert.equal(badPayload.controller.status().state, "failed");

  const videoSdp = OFFER_SDP.replace("m=audio", "m=video");
  const badSdp = fixture({
    role: WebRtc.ROLE_READER_RECEIVER,
    localTracks: null,
    transport: new FakeSignalTransport(() => ({
      contract: WebRtc.SIGNAL_CONTRACT,
      sessionId: SESSION_ID,
      ackedSignalIds: [],
      signals: [{
        cursor: 1,
        signalId: "video-offer",
        kind: "offer",
        payload: { type: "offer", sdp: videoSdp },
      }],
      cursor: 1,
    })),
  });
  await assert.rejects(
    badSdp.controller.start(gate()),
    (error) => error.code === "BW_COMPUTER_VOICE_WEBRTC_MEDIA_SCOPE",
  );
});

test("轮询次数与本地时限均有界；失败/断线只清理一次且不能自动重启", async () => {
  const bounded = fixture({ maxPolls: 2 });
  await bounded.controller.start(gate());
  await bounded.controller.poll();
  await assert.rejects(
    bounded.controller.poll(),
    (error) => error.code === "BW_COMPUTER_VOICE_WEBRTC_TIMEOUT",
  );
  assert.equal(bounded.controller.status().state, "failed");
  assert.equal(FakePeerConnection.instances[0].closed, 1);
  await assert.rejects(
    bounded.controller.start(gate()),
    (error) => error.code === "BW_COMPUTER_VOICE_WEBRTC_TRIGGER_CONSUMED",
  );

  const disconnected = fixture();
  await disconnected.controller.start(gate());
  const peer = FakePeerConnection.instances[0];
  peer.setDisconnected();
  peer.setDisconnected();
  assert.equal(disconnected.controller.status().state, "failed");
  assert.equal(peer.closed, 1);
  assert.equal(disconnected.releases, 1);
});

test("stop 只发送结构化 bye 元数据并完成本地清理", async () => {
  const context = fixture();
  await context.controller.start(gate());
  await context.controller.stop("user-stopped");
  const last = context.transport.requests.at(-1);
  assert.equal(last.signals.at(-1).kind, "bye");
  assert.deepEqual(last.signals.at(-1).payload, {
    reason: "user-stopped",
  });
  assert.equal(context.controller.status().state, "stopped");
  assert.equal(FakePeerConnection.instances[0].closed, 1);
  assert.equal(context.releases, 1);
});
