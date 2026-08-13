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
const CONTEXT_ENDPOINT =
  "wss://bwicarus-2.taile44d0c.ts.net/reader-context/v1";
const READER_ORIGIN = "https://bwicarus.taile44d0c.ts.net";
const NATIVE_APP_ORIGIN = "http://127.0.0.1:43129";
const RELAY_PORT = "BW_COMPUTER_VOICE_DIRECT_V3";
const DIRECT_CONTRACT = "reader-computer-voice-direct/1";
const RESULT_DELIVERY_CONTRACT = "reader-result-delivery/1";
const REALTIME_OUTPUT_CONTRACT = "reader-realtime-output/1";
const VISUAL_DELIVERY_CONTRACT = "reader-visual-delivery/2";

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
      this.mediaSources = [];
      this.processors = [];
      this.gains = [];
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

    createMediaStreamSource(stream) {
      const record = {
        stream,
        connectedTo: null,
        disconnected: false,
      };
      this.mediaSources.push(record);
      return {
        connect(target) { record.connectedTo = target; },
        disconnect() { record.disconnected = true; },
      };
    }

    createGain() {
      const record = {
        connectedTo: null,
        disconnected: false,
        gain: { value: 1 },
      };
      this.gains.push(record);
      return {
        gain: record.gain,
        connect(target) { record.connectedTo = target; },
        disconnect() { record.disconnected = true; },
      };
    }

    createScriptProcessor(bufferSize, inputChannels, outputChannels) {
      assert.equal(bufferSize, 1024);
      assert.equal(inputChannels, 1);
      assert.equal(outputChannels, 1);
      const record = {
        onaudioprocess: null,
        connectedTo: null,
        disconnected: false,
        emit(samples) {
          assert.ok(samples instanceof Float32Array);
          this.onaudioprocess?.({
            inputBuffer: {
              getChannelData(channel) {
                assert.equal(channel, 0);
                return samples;
              },
            },
          });
        },
      };
      this.processors.push(record);
      scenario.scriptProcessors.push(record);
      return {
        get onaudioprocess() { return record.onaudioprocess; },
        set onaudioprocess(value) { record.onaudioprocess = value; },
        connect(target) { record.connectedTo = target; },
        disconnect() { record.disconnected = true; },
      };
    }
  };
}

function createServer(scenario) {
  const server = {
    requests: [],
    binaryFrames: [],
    timeline: [],
    sockets: [],
    activeSocket: null,
    deferredStart: null,
    deferredContextClears: [],
    deferredContextOpens: [],
    deferredSocketCloses: [],
    deferredStops: [],
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

  server.emitReaderResult = (payload, socket = null) => {
    const target = socket || server.sockets.at(-1);
    assert.ok(target, "missing Reader result socket");
    queueMicrotask(() => target.onmessage?.({
      data: JSON.stringify({
        contract: DIRECT_CONTRACT,
        type: "event",
        event: "reader-result",
        payload,
      }),
    }));
  };

  server.emitReaderVisual = (payload, socket = null) => {
    const target = socket || server.sockets.at(-1);
    assert.ok(target, "missing Reader visual socket");
    queueMicrotask(() => target.onmessage?.({
      data: JSON.stringify({
        contract: DIRECT_CONTRACT,
        type: "event",
        event: "reader-visual-request",
        payload,
      }),
    }));
  };

  server.emitReaderRealtimeOutput = (payload, socket = null) => {
    const target = socket || server.sockets.at(-1);
    assert.ok(target, "missing Reader realtime output socket");
    queueMicrotask(() => target.onmessage?.({
      data: JSON.stringify({
        contract: DIRECT_CONTRACT,
        type: "event",
        event: "reader-realtime-output",
        payload,
      }),
    }));
  };

  server.resolveDeferredStart = () => {
    assert.ok(server.deferredStart, "missing deferred START");
    const { socket, request } = server.deferredStart;
    server.deferredStart = null;
    result(socket, request, {
      sessionId: request.sessionId,
      state: "active",
      media: { hostReady: true, captureActive: true },
    });
  };

  server.resolveDeferredContextClear = () => {
    assert.ok(
      server.deferredContextClears.length,
      "missing deferred CONTEXT-CLEAR",
    );
    const { socket, request } = server.deferredContextClears.shift();
    result(socket, request, {
      sessionId: request.sessionId,
      revision:
        scenario.activeReadingRequests.length
        + scenario.contextClearRequests.length,
      outcome: "accepted",
    });
  };

  server.resolveDeferredContextOpen = () => {
    assert.ok(
      server.deferredContextOpens.length,
      "missing deferred CONTEXT-OPEN",
    );
    const { socket, request } = server.deferredContextOpens.shift();
    result(socket, request, {
      sessionId: request.sessionId,
      state: "context-only",
      mode: scenario.contextDeliveryMode || "legacy-inject",
    });
  };

  server.resolveDeferredSocketClose = () => {
    assert.ok(
      server.deferredSocketCloses.length,
      "missing deferred socket close",
    );
    const socket = server.deferredSocketCloses.shift();
    socket.readyState = 3;
    queueMicrotask(() => socket.onclose?.());
  };

  server.resolveDeferredStop = () => {
    assert.ok(server.deferredStops.length, "missing deferred STOP");
    const { socket, request } = server.deferredStops.shift();
    result(socket, request, {
      sessionId: request.sessionId,
      state: "idle",
    });
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
          retryable: error.retryable === true,
        },
      }),
    }));
  };

  class FakeWebSocket {
    constructor(url) {
      assert.ok(
        url === ENDPOINT || url === CONTEXT_ENDPOINT,
        `unexpected direct endpoint: ${url}`,
      );
      scenario.socketUrls.push(url);
      this.url = url;
      this.readyState = 0;
      this.binaryType = "";
      this.bufferedAmount = 0;
      this.contextModeRequests = 0;
      this.activeReadingRequests = 0;
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
      if (typeof serialized !== "string") {
        const bytes = serialized instanceof ArrayBuffer
          ? new Uint8Array(serialized.slice(0))
          : new Uint8Array(
            serialized.buffer.slice(
              serialized.byteOffset,
              serialized.byteOffset + serialized.byteLength,
            ),
          );
        server.binaryFrames.push(bytes);
        server.timeline.push({ type: "binary", bytes });
        return;
      }
      const request = JSON.parse(serialized);
      server.requests.push(request);
      server.timeline.push({ type: "text", action: request.type });
      if (request.type === "hello") {
        result(this, request, scenario.helloPayload || {
          protocolVersion: 3,
          limits: {
            maxMessageBytes: 65536,
            pcmFrameBytes: 1956,
            pcmQueueLimitMs: 400,
            heartbeatIntervalMs: 5000,
            heartbeatTimeoutMs: 15000,
            uplinkTrack: 3,
            uplinkQueueLimitMs: 200,
          },
        });
        return;
      }
      if (request.type === "status") {
        if (
          scenario.dropFirstBorrowedStatus === true &&
          this === server.sockets[0] &&
          scenario.droppedFirstBorrowedStatus !== true
        ) {
          scenario.droppedFirstBorrowedStatus = true;
          return;
        }
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
          lastError: scenario.lastError ?? null,
        });
        return;
      }
      if (request.type === "context-mode") {
        this.contextModeRequests += 1;
        if (
          scenario.dropSecondContextModeOnFirstSocket === true &&
          this === server.sockets[0] &&
          this.contextModeRequests === 2
        ) {
          return;
        }
        result(this, request, {
          mode: scenario.contextDeliveryMode || "legacy-inject",
        });
        return;
      }
      if (request.type === "context-mode-set") {
        const previousMode = scenario.contextDeliveryMode;
        if (
          previousMode === "snapshot-mcp" &&
          request.mode === "legacy-inject"
        ) {
          scenario.contextClearRequests.push({
            type: "context-clear-via-mode-set",
            sessionId: request.sessionId,
          });
        }
        scenario.contextDeliveryMode = request.mode;
        result(this, request, {
          mode: request.mode,
          previousMode,
        });
        return;
      }
      if (request.type === "context-open") {
        if (scenario.deferContextOpen) {
          server.deferredContextOpens.push({ socket: this, request });
          return;
        }
        result(this, request, {
          sessionId: request.sessionId,
          state: "context-only",
          mode: scenario.contextDeliveryMode || "legacy-inject",
        });
        return;
      }
      if (request.type === "visual-register") {
        scenario.readerVisualRegistrations.push(structuredClone(request));
        result(this, request, {
          sessionId: request.sessionId,
          sourceInstanceId: request.sourceInstanceId,
          state: "registered",
        });
        return;
      }
      if (request.type === "active-reading") {
        this.activeReadingRequests += 1;
        scenario.activeReadingRequests.push(structuredClone(request));
        if (
          scenario.dropSecondActiveReadingOnFirstSocket === true &&
          this === server.sockets[0] &&
          this.activeReadingRequests === 2
        ) {
          return;
        }
        result(this, request, {
          sessionId: request.sessionId,
          revision: scenario.activeReadingRequests.length,
          outcome: "accepted",
        });
        return;
      }
      if (request.type === "context-clear") {
        scenario.contextClearRequests.push(structuredClone(request));
        if (scenario.deferContextClear) {
          server.deferredContextClears.push({ socket: this, request });
          return;
        }
        result(this, request, {
          sessionId: request.sessionId,
          revision:
            scenario.activeReadingRequests.length
            + scenario.contextClearRequests.length,
          outcome: "accepted",
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
        queueMicrotask(() => this.onmessage?.({
          data: JSON.stringify({
            contract: DIRECT_CONTRACT,
            type: "event",
            event: "status",
            payload: { state: "waiting-voice-ready", reason: null },
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
          server.deferredStart = { socket: this, request };
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
      if (request.type === "context") {
        const responder = scenario.contextResponder;
        if (typeof responder === "function") {
          responder({
            request,
            socket: this,
            result: (payload) => result(this, request, payload),
            failure: (error) => failure(this, request, error),
          });
          return;
        }
        result(this, request, {
          sessionId: request.sessionId,
          eventId: request.event.id,
          seq: request.event.seq,
          outcome: "accepted",
        });
        return;
      }
      if (request.type === "stop") {
        if (scenario.deferStopResult === true) {
          server.deferredStops.push({ socket: this, request });
          return;
        }
        result(this, request, {
          sessionId: request.sessionId,
          state: "idle",
        });
        return;
      }
      if (request.type === "reader-result-ack") {
        scenario.readerResultAcks.push(structuredClone(request));
        result(this, request, {
          correlation: request.correlation,
          outcome: request.outcome,
          matched: true,
        });
        return;
      }
      if (request.type === "reader-realtime-output-ack") {
        scenario.readerRealtimeOutputAcks.push(structuredClone(request));
        result(this, request, {
          correlation: request.correlation,
          outcome: request.outcome,
          matched: true,
        });
        return;
      }
      if (request.type === "reader-visual") {
        scenario.readerVisualChunks.push(structuredClone(request));
        result(this, request, {
          correlation: request.correlation,
          chunkIndex: request.chunkIndex,
          accepted: true,
          complete: request.status === "unavailable"
            || request.chunkIndex + 1 === request.chunkCount,
        });
        return;
      }
      throw new Error(`unexpected action ${request.type}`);
    }

    close() {
      if (scenario.deferSocketClose) {
        this.readyState = 2;
        server.deferredSocketCloses.push(this);
        return;
      }
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
          if (message.type === "send-binary-base64") {
            assert.deepEqual(
              Object.keys(message).sort(),
              ["bytes", "data", "sequence", "type"],
            );
            assert.equal(message.bytes, 1956);
            assert.equal(Number.isSafeInteger(message.sequence), true);
            const bytes = Buffer.from(message.data, "base64");
            assert.equal(bytes.length, message.bytes);
            assert.equal(
              new DataView(
                bytes.buffer,
                bytes.byteOffset,
                bytes.byteLength,
              ).getUint32(24, true),
              message.sequence,
            );
            socket.send(bytes.buffer.slice(
              bytes.byteOffset,
              bytes.byteOffset + bytes.byteLength,
            ));
            const pendingAck = {
              emit,
              sequence: message.sequence,
              bytes: message.bytes,
            };
            scenario.pendingRelayAcks.push(pendingAck);
            if (scenario.relayAutoAck !== false) {
              queueMicrotask(() => {
                const index = scenario.pendingRelayAcks.indexOf(pendingAck);
                if (index >= 0) scenario.pendingRelayAcks.splice(index, 1);
                emit({
                  type: "binary-accepted",
                  sequence: message.sequence,
                  bytes: message.bytes,
                });
              });
            }
            return;
          }
          if (message.type === "close") {
            assert.deepEqual(Object.keys(message), ["type"]);
            if (scenario.relayCloseHangs) return;
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

function createMicrophoneStream(scenario) {
  const track = {
    kind: "audio",
    readyState: "live",
    muted: false,
    stopped: false,
    onended: null,
    onmute: null,
    onunmute: null,
    stop() {
      this.stopped = true;
      this.readyState = "ended";
    },
  };
  const stream = {
    track,
    getAudioTracks() { return [track]; },
    getTracks() { return [track]; },
  };
  scenario.microphoneTracks.push(track);
  scenario.microphoneStreams.push(stream);
  return stream;
}

function createNavigator(scenario) {
  return {
    audioSession: { type: "playback" },
    mediaDevices: {
      getUserMedia(constraints) {
        scenario.microphoneRequests.push(structuredClone(constraints));
        const outcome = scenario.microphonePlan.length
          ? scenario.microphonePlan.shift()
          : "resolve";
        if (outcome === "permission-reject") {
          const error = new Error("permission denied");
          error.name = "NotAllowedError";
          return Promise.reject(error);
        }
        if (outcome instanceof Error) return Promise.reject(outcome);
        if (outcome && typeof outcome.then === "function") return outcome;
        return Promise.resolve(createMicrophoneStream(scenario));
      },
    },
  };
}

function journalEnvelope({
  cursor = 0,
  head = 0,
  events = [],
  gap = false,
  note = "",
  waited = 0,
  waitDenied,
} = {}) {
  const value = {
    ok: true,
    contract: "reader-outgoing-context/1",
    cursor,
    head,
    events,
    gap,
    note,
    waited,
  };
  if (waitDenied !== undefined) value.waitDenied = waitDenied;
  return value;
}

function journalEvent(seq, type = "focus", extra = {}) {
  return {
    v: 1,
    seq,
    type,
    ts: 1750000000 + seq,
    id: seq.toString(16).padStart(16, "0"),
    ...extra,
  };
}

function jsonResponse(body, ok = true) {
  return {
    ok,
    json() { return Promise.resolve(structuredClone(body)); },
  };
}

function createJournalFetch(scenario) {
  return (url, options) => {
    const parsed = new URL(url, READER_ORIGIN);
    if (parsed.pathname === "/pdf/api/context-sync") {
      assert.equal(options.method, "POST");
      const body = JSON.parse(options.body);
      scenario.contextModePosts.push(structuredClone(body));
      return Promise.resolve(jsonResponse({
        ok: true,
        enabled: body.enabled === true,
        deliveryMode: body.deliveryMode,
      }));
    }
    if (parsed.pathname === "/pdf/api/active-reading") {
      assert.equal(options.method, "GET");
      scenario.activeReadingFetches += 1;
      const active = scenario.serverActiveReading
        || scenario.activeReading;
      return Promise.resolve(jsonResponse({
        ok: true,
        enabled: scenarioContextSyncEnabled(scenario),
        active: active ? structuredClone(active) : null,
        fresh: !!active,
        age_sec: active ? 0 : null,
        fresh_window_sec: 180,
      }));
    }
    const call = {
      url,
      since: Number(parsed.searchParams.get("since")),
      limit: Number(parsed.searchParams.get("limit")),
      wait: Number(parsed.searchParams.get("wait")),
      options,
    };
    scenario.journalCalls.push(call);
    if (typeof scenario.journalResponder === "function") {
      return scenario.journalResponder(call);
    }
    if (call.wait === 0) {
      return Promise.resolve(jsonResponse(journalEnvelope()));
    }
    return new Promise((resolve, reject) => {
      const pending = { call, resolve, reject };
      scenario.pendingJournalFetches.push(pending);
      options.signal.addEventListener("abort", () => {
        scenario.abortedJournalFetches += 1;
        const error = new Error("aborted");
        error.name = "AbortError";
        reject(error);
      }, { once: true });
    });
  };
}

function scenarioContextSyncEnabled(scenario) {
  if (scenario.contextSyncStorage instanceof Map) {
    return scenario.contextSyncStorage.get("eph-ctx-sync") === "1";
  }
  return scenario.contextSyncEnabled === true;
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
    deferStopResult: false,
    initialAudioState: "suspended",
    resumePlan: [],
    audioContexts: [],
    microphonePlan: [],
    microphoneRequests: [],
    microphoneTracks: [],
    microphoneStreams: [],
    scriptProcessors: [],
    origin: READER_ORIGIN,
    extensionRelay: false,
    directWebSocketAttempts: 0,
    socketUrls: [],
    deferStatusResult: false,
    dropFirstBorrowedStatus: false,
    droppedFirstBorrowedStatus: false,
    dropSecondContextModeOnFirstSocket: false,
    dropSecondActiveReadingOnFirstSocket: false,
    deferSocketClose: false,
    lastError: null,
    relayLifecycle: [],
    relayOverlapAttempts: 0,
    relayAutoAck: true,
    relayCloseHangs: false,
    pendingRelayAcks: [],
    journalCalls: [],
    journalResponder: null,
    pendingJournalFetches: [],
    abortedJournalFetches: 0,
    contextResponder: null,
    contextDeliveryMode: "legacy-inject",
    activeReadingRequests: [],
    contextClearRequests: [],
    deferContextClear: false,
    deferContextOpen: false,
    activeReadingFetches: 0,
    contextSyncEnabled: false,
    serverContextSyncEnabled: null,
    contextSyncStorage: null,
    activeReading: null,
    serverActiveReading: null,
    contextModePosts: [],
    readerResultDeliveries: [],
    readerResultAcks: [],
    readerResultReceipt: { outcome: "rendered" },
    readerRealtimeOutputDeliveries: [],
    readerRealtimeOutputAcks: [],
    readerRealtimeOutputReceipt: { outcome: "applied" },
    readerRealtimeOutputDiagnostics: [],
    readerVisualChunks: [],
    readerVisualRegistrations: [],
    readerVisualCaptureCalls: 0,
    readerVisualCaptureTargets: [],
    readerVisualCompositeCaptureCalls: 0,
    readerVisualCompositeCaptureTargets: [],
    readerVisualCapture: null,
    readerVisualDrawing: null,
    readerVisualOutgoingState: {
      drawPend: null,
      drawTimer: null,
      inflight: false,
    },
    realtimeVoiceAllowed: true,
    uiOwner: "",
    extensionWorld: false,
    nativeComputerVoice: false,
    nativeComputerVoiceState: null,
    nativeReaderForeground: null,
    nativeLocalPageContext: false,
    nativePageContextPublishes: [],
    nativePageTexts: {},
    readerAdapterContext: null,
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
  const documentEventHandlers = new Map();
  const windowEventHandlers = new Map();
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
  const computerButtons = {
    "asst-computer": {
      id: "asst-computer",
      isConnected: true,
      nodeType: 1,
      parentNode: null,
      tagName: "BUTTON",
      type: "button",
    },
    "vc-top-computer": {
      id: "vc-top-computer",
      isConnected: true,
      nodeType: 1,
      parentNode: null,
      tagName: "BUTTON",
      type: "button",
    },
  };
  const document = {
    visibilityState: scenario.visibilityState || "visible",
    documentElement: {
      dataset: {
        bwReaderUiOwner: scenario.uiOwner || "",
      },
    },
    getElementById(id) {
      return computerButtons[id] || phoneButtons[id] || null;
    },
    addEventListener(type, handler, capture) {
      if (type === "click") {
        assert.equal(capture, true);
        clickHandlers.push(handler);
        return;
      }
      const handlers = documentEventHandlers.get(type) || [];
      handlers.push(handler);
      documentEventHandlers.set(type, handlers);
    },
  };
  Object.values(phoneButtons).forEach((button) => {
    button.ownerDocument = document;
  });
  Object.values(computerButtons).forEach((button) => {
    button.ownerDocument = document;
  });
  const navigator = createNavigator(scenario);
  const window = {
    dlog(message) {
      scenario.readerRealtimeOutputDiagnostics.push(String(message));
    },
    RC: {
      voicecall: {
        canCaptureComputerVoiceGesture() {
          return scenario.realtimeVoiceAllowed === true;
        },
        acceptRealtimeOutput(delivery) {
          scenario.readerRealtimeOutputDeliveries.push(
            structuredClone(delivery),
          );
          return structuredClone(scenario.readerRealtimeOutputReceipt);
        },
      },
      ctxSync: {
        enabled() {
          return scenarioContextSyncEnabled(scenario);
        },
        getConfig() {
          const enabled = typeof scenario.serverContextSyncEnabled === "boolean"
            ? scenario.serverContextSyncEnabled
            : scenarioContextSyncEnabled(scenario);
          if (scenario.contextSyncStorage instanceof Map) {
            scenario.contextSyncStorage.set("eph-ctx-sync", enabled ? "1" : "0");
          }
          return Promise.resolve({
            ok: true,
            enabled,
            deliveryMode: scenario.contextDeliveryMode,
          });
        },
        _state() {
          return {
            pend: scenario.activeReading,
            canonical: scenario.activeReadingCanonical || null,
          };
        },
      },
      adapter() {
        if (!scenario.readerAdapterContext) return null;
        return {
          getContext() {
            return structuredClone(scenario.readerAdapterContext);
          },
        };
      },
      assistant: {
        acceptDirectResult(delivery) {
          scenario.readerResultDeliveries.push(
            structuredClone(delivery)
          );
          return structuredClone(scenario.readerResultReceipt);
        },
      },
      captureInkRegion(target) {
        scenario.readerVisualCaptureCalls += 1;
        scenario.readerVisualCaptureTargets.push(structuredClone(target));
        return Promise.resolve(scenario.readerVisualCapture);
      },
      capturePageComposite(target) {
        scenario.readerVisualCompositeCaptureCalls += 1;
        scenario.readerVisualCompositeCaptureTargets.push(
          structuredClone(target),
        );
        return Promise.resolve(scenario.readerVisualCapture);
      },
      outgoing: {
        lastDrawing() {
          return scenario.readerVisualDrawing;
        },
        _state() {
          return scenario.readerVisualOutgoingState;
        },
      },
    },
    WebSocket: scenario.extensionRelay
      ? class ForbiddenContentScriptWebSocket {
        constructor() {
          scenario.directWebSocketAttempts += 1;
          throw new Error("content script must not open direct WebSocket");
        }
      }
      : server.WebSocket,
    AudioContext: createAudioContextClass(scenario),
    navigator,
    crypto: webcrypto,
    isSecureContext: true,
    location: { origin: scenario.origin },
    localStorage: {
      getItem(key) {
        return scenario.contextSyncStorage instanceof Map
          ? scenario.contextSyncStorage.get(String(key)) ?? null
          : null;
      },
      setItem(key, value) {
        if (scenario.contextSyncStorage instanceof Map) {
          scenario.contextSyncStorage.set(String(key), String(value));
        }
      },
    },
    setTimeout: scheduleTimeout,
    clearTimeout: cancelTimeout,
    addEventListener(type, handler) {
      const handlers = windowEventHandlers.get(type) || [];
      handlers.push(handler);
      windowEventHandlers.set(type, handlers);
    },
    dispatchEvent() {},
    AbortController,
    document,
  };
  if (scenario.nativeLocalPageContext) {
    window.BWReaderRuntime = {
      nativeLocalRuntime: {
        ready() { return Promise.resolve(); },
        publishPageContext(payload) {
          scenario.nativePageContextPublishes.push(structuredClone(payload));
          return Promise.resolve({ ok: true, seq: scenario.nativePageContextPublishes.length });
        },
      },
      pageTextProvider: {
        contract: "reader-page-text-provider/1",
        pageChars(page) {
          const text = String(scenario.nativePageTexts[page] || "");
          return Promise.resolve({
            chars: Array.from(text, (character) => ({ c: character })),
          });
        },
      },
    };
  }
  window.__bwReaderFetch = createJournalFetch(scenario);
  if (scenario.nativeComputerVoice) {
    window.__BW_NATIVE_COMPUTER_VOICE__ = true;
  }
  if (scenario.nativeComputerVoiceState) {
    window.__BW_NATIVE_COMPUTER_VOICE_STATE__ = structuredClone(
      scenario.nativeComputerVoiceState,
    );
  }
  if (typeof scenario.nativeReaderForeground === "boolean") {
    window.__BW_NATIVE_READER_FOREGROUND__ =
      scenario.nativeReaderForeground;
  }
  if (scenario.extensionRelay) {
    window.chrome = {
      runtime: createRelayRuntime(server, scenario),
    };
  } else if (scenario.extensionWorld) {
    window.chrome = {
      runtime: {
        id: "abcdefghijklmnopabcdefghijklmnop",
      },
    };
  }
  const context = vm.createContext({
    window,
    document,
    navigator,
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
    window.RC.computerVoice.registerComputerButton(
      computerButtons["asst-computer"],
    ),
    true,
  );
  assert.equal(
    window.RC.computerVoice.registerComputerButton(
      computerButtons["vc-top-computer"],
    ),
    true,
  );
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
    computerButtons,
    phoneButtons,
    dispatchDocumentEvent(type, detail) {
      for (const handler of documentEventHandlers.get(type) || []) {
        handler({ type, detail });
      }
    },
    dispatchWindowEvent(type, detail) {
      for (const handler of windowEventHandlers.get(type) || []) {
        handler({ type, detail });
      }
    },
    setVisibilityState(value) {
      document.visibilityState = value;
    },
    setUiOwner(value) {
      document.documentElement.dataset.bwReaderUiOwner = value;
    },
  };
}

function computerClick(harness, {
  id = "asst-computer",
  trusted = true,
  target = null,
} = {}) {
  const event = {
    isTrusted: trusted,
    target: target || harness.computerButtons[id],
    prevented: false,
    stopped: false,
    preventDefault() { this.prevented = true; },
    stopImmediatePropagation() { this.stopped = true; },
  };
  harness.clickHandlers[0](event);
  return event;
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
  harness.api.setSelectedEngine("computer_client");
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

async function waitForCondition(predicate, label, timeoutMs = 2500) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  assert.fail(`timed out waiting for ${label}`);
}

function visualRequest(harness, fields = {}) {
  const registration = harness.scenario.readerVisualRegistrations.at(-1);
  assert.ok(registration, "missing visual-register");
  return {
    contract: VISUAL_DELIVERY_CONTRACT,
    commandKind: "capture-composite",
    correlation: "visual-0123456789abcdef",
    sourceInstanceId: registration.sourceInstanceId,
    snapshotRevision: 1,
    file: "book.pdf",
    page: 24,
    drawingRevision: null,
    scope: "viewport-context",
    selectionId: null,
    maxBytes: 768 * 1024,
    chunkCharacters: 48000,
    ...fields,
  };
}

function contextRequests(harness) {
  return harness.server.requests.filter((request) => request.type === "context");
}

async function disableSnapshot(harness) {
  harness.scenario.contextSyncEnabled = false;
  await harness.api.contextSyncChanged();
}

test("v3 HELLO 后直接 STATUS，固定 WSS 且不读取浏览器身份或麦克风", async () => {
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
  assert.equal(harness.server.requests[0].protocolVersion, 3);
  assert.equal(harness.scenario.microphoneRequests.length, 0);
  assert.equal(harness.api.beginPairing, undefined);
  assert.equal(harness.api.forgetIdentity, undefined);
  assert.doesNotMatch(
    SOURCE,
    /indexedDB|pairingCode|clientPublicKeySpki|type:\s*"pair"|type:\s*"auth"/i,
  );
  assert.doesNotMatch(SOURCE, /data-role="(?:endpoint|code|pair|forget)"/);
});

test("旧 computer_client 电话入口仅保留受控兼容，直接调用与伪同 ID 均 fail closed", async () => {
  const direct = createHarness();
  await assert.rejects(
    direct.api.startFromUserGesture(),
    (error) => error?.code === "BW_COMPUTER_VOICE_GESTURE_REQUIRED",
  );
  assert.equal(direct.scenario.audioContexts.length, 0);
  assert.equal(direct.scenario.microphoneRequests.length, 0);
  assert.equal(direct.server.sockets.length, 0);

  const synthetic = createHarness();
  synthetic.api.setSelectedEngine("computer_client");
  const untrusted = phoneClick(synthetic, { trusted: false });
  assert.equal(untrusted.prevented, false);
  await assert.rejects(
    synthetic.api.startFromUserGesture(),
    (error) => error?.code === "BW_COMPUTER_VOICE_GESTURE_REQUIRED",
  );
  assert.equal(synthetic.scenario.audioContexts.length, 0);
  assert.equal(synthetic.scenario.microphoneRequests.length, 0);
  assert.equal(synthetic.server.sockets.length, 0);

  const decoy = createHarness();
  decoy.api.setSelectedEngine("computer_client");
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
  assert.equal(decoy.scenario.microphoneRequests.length, 0);
  assert.equal(decoy.server.sockets.length, 0);

  const forwarded = createHarness();
  forwarded.api.setSelectedEngine("computer_client");
  phoneClick(forwarded, { id: "vc-top-call", trusted: true });
  phoneClick(forwarded, { id: "asst-call", trusted: false });
  const started = await forwarded.api.startFromUserGesture();
  assert.equal(started.ok, true);
  assert.equal(forwarded.scenario.microphoneRequests.length, 1);
  await forwarded.api.stop("test");
  await assert.rejects(
    forwarded.api.startFromUserGesture(),
    (error) => error?.code === "BW_COMPUTER_VOICE_GESTURE_REQUIRED",
  );
  assert.equal(forwarded.scenario.microphoneRequests.length, 1);
});

test("专用电脑按钮独立于普通语音模型签发 START，伪同 ID 与普通电话均无权", async () => {
  const dedicated = createHarness();
  assert.equal(dedicated.api.setSelectedEngine("native"), false);
  computerClick(dedicated, { trusted: true });
  const started = await dedicated.api.startFromUserGesture();
  assert.equal(started.ok, true);
  assert.equal(dedicated.scenario.microphoneRequests.length, 1);
  assert.equal(
    dedicated.server.requests.filter((request) => request.type === "start").length,
    1,
  );
  await dedicated.api.stop("test");

  const ordinaryPhone = createHarness();
  ordinaryPhone.api.setSelectedEngine("native");
  phoneClick(ordinaryPhone, { trusted: true });
  await assert.rejects(
    ordinaryPhone.api.startFromUserGesture(),
    (error) => error?.code === "BW_COMPUTER_VOICE_GESTURE_REQUIRED",
  );
  assert.equal(ordinaryPhone.scenario.microphoneRequests.length, 0);

  const decoy = createHarness();
  const original = decoy.computerButtons["asst-computer"];
  const replacement = {
    ...original,
    ownerDocument: original.ownerDocument,
  };
  computerClick(decoy, { trusted: true, target: replacement });
  await assert.rejects(
    decoy.api.startFromUserGesture(),
    (error) => error?.code === "BW_COMPUTER_VOICE_GESTURE_REQUIRED",
  );
  assert.equal(decoy.scenario.microphoneRequests.length, 0);
});

test("首次配置仍在加载时一次可信旧电话点击仍可兼容 computer_client START", async () => {
  const harness = createHarness();
  phoneClick(harness, { trusted: true });
  assert.equal(
    harness.scenario.microphoneRequests.length,
    1,
    "初始 voice-config 尚未知时也只准备可撤销的本地麦克风表面",
  );

  // Reproduce rc-voicecall's real bubble-phase order after the capture handler:
  // mark the dial pending, then apply the authoritative GET result.
  harness.api.setDialPending(true);
  assert.equal(harness.api.setSelectedEngine("computer_client"), true);
  const started = await harness.api.startFromUserGesture();
  harness.api.setDialPending(false);

  assert.equal(started.ok, true);
  assert.equal(
    harness.server.requests.filter((request) => request.type === "start").length,
    1,
  );
  assert.equal(harness.server.sockets.length, 1);
  await harness.api.stop("test");
});

test("专用电脑按钮不受历史 computer_client 配置回落影响", async () => {
  let resolveMicrophone;
  const microphone = new Promise((resolve) => {
    resolveMicrophone = resolve;
  });
  const harness = createHarness({ microphonePlan: [microphone] });
  computerClick(harness, { trusted: true });
  assert.equal(harness.scenario.microphoneRequests.length, 1);

  harness.api.setDialPending(true);
  assert.equal(harness.api.setSelectedEngine("native"), false);
  const stream = createMicrophoneStream(harness.scenario);
  resolveMicrophone(stream);
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(stream.track.stopped, false);
  const started = await harness.api.startFromUserGesture();
  assert.equal(started.ok, true);
  assert.equal(harness.server.sockets.length, 1);
  harness.api.setDialPending(false);
  await harness.api.stop("test");
});

test("STOP 回执延迟时本地麦克风与 AudioContext 先立即释放", async () => {
  const harness = createHarness({ deferStopResult: true });
  const started = await startWithTrustedGesture(harness);
  assert.equal(started.ok, true);
  const context = harness.scenario.audioContexts.at(-1);
  const track = harness.scenario.microphoneTracks.at(-1);
  assert.equal(context.closed, false);
  assert.equal(track.stopped, false);

  const stopping = harness.api.stop("switch-to-ordinary-voice");
  assert.equal(harness.api.isActive(), false);
  assert.equal(track.stopped, true);
  assert.equal(context.closed, true);
  await waitForCondition(
    () => harness.server.deferredStops.length === 1,
    "deferred STOP request",
  );
  assert.equal(
    harness.server.activeSocket.readyState,
    1,
    "remote STOP may still be waiting while local routing is already released",
  );
  harness.server.resolveDeferredStop();
  const stopped = await stopping;
  assert.equal(stopped.state, "stopped");
});

test("snapshot-mcp 真实拨号顺序保留并原地升级常驻 WSS", async () => {
  const harness = createHarness({
    contextDeliveryMode: "snapshot-mcp",
    contextSyncEnabled: true,
    activeReading: {
      kind: "pdf",
      file: "book.pdf",
      title: "Snapshot Book",
      pos: 24,
      selection: "selected words",
    },
  });
  harness.api.setSelectedEngine("computer_client");
  await waitForRequest(harness, "context-open");
  await waitForCondition(
    () => harness.scenario.activeReadingRequests.length >= 1,
    "background active-reading",
  );
  assert.equal(
    harness.server.requests.some((request) => request.type === "start"),
    false,
  );
  assert.equal(
    harness.scenario.activeReadingRequests[0].active.selectionState,
    "active",
  );
  assert.equal(
    harness.scenario.activeReadingRequests[0].active.selection,
    "selected words",
  );
  assert.equal(harness.scenario.microphoneRequests.length, 0);
  assert.deepEqual(harness.scenario.contextModePosts[0], {
    enabled: true,
    deliveryMode: "snapshot-mcp",
  });
  const backgroundSocket = harness.server.sockets[0];
  const available = await harness.api.availability();
  assert.equal(available.state, "idle");
  assert.equal(harness.server.sockets.length, 1);
  assert.equal(backgroundSocket.readyState, 1);

  phoneClick(harness, { trusted: true });
  harness.api.setDialPending(true);
  harness.api.setSelectedEngine("computer_client");
  assert.equal(
    backgroundSocket.readyState,
    1,
    "dialPending 与同值配置回执不得提前拆掉待晋升的常驻 WSS",
  );
  assert.equal(harness.server.sockets.length, 1);
  const started = await harness.api.startFromUserGesture();
  harness.api.setDialPending(false);
  assert.equal(started.ok, true);
  assert.equal(backgroundSocket.readyState, 1);
  assert.equal(harness.server.sockets.length, 1);
  assert.equal(harness.server.activeSocket, backgroundSocket);
  const starts = harness.server.requests.filter(
    (request) => request.type === "start",
  );
  const opens = harness.server.requests.filter(
    (request) => request.type === "context-open",
  );
  assert.equal(starts.length, 1);
  assert.equal(opens.length, 1);
  assert.equal(starts[0].sessionId, opens[0].sessionId);
  assert.equal(harness.scenario.microphoneRequests.length, 1);

  harness.scenario.activeReading = {
    ...harness.scenario.activeReading,
    selection: "",
  };
  await waitForCondition(
    () => harness.scenario.activeReadingRequests.some(
      (request) => request.active.selectionState === "cleared",
    ),
    "selection clear heartbeat",
  );

  await disableSnapshot(harness);
  await harness.api.stop("test");
  assert.equal(harness.api.isActive(), false);
});

test("snapshot-mcp 常驻 WSS 接收严格结果卡并回 rendered ACK，不发送 START", async () => {
  const harness = createHarness({
    contextDeliveryMode: "snapshot-mcp",
    contextSyncEnabled: true,
    activeReading: {
      kind: "pdf",
      file: "book.pdf",
      title: "Snapshot Book",
      pos: 24,
    },
  });
  harness.api.setSelectedEngine("computer_client");
  await waitForRequest(harness, "context-open");
  const delivery = {
    contract: RESULT_DELIVERY_CONTRACT,
    correlation: "weather.tokyo.24",
    anchor: {
      file: "book.pdf",
      page: 24,
    },
    parts: [{
      kind: "card",
      card: {
        kind: "weather",
        title: "东京天气",
        brief: "明日天气",
        data: {
          lo: 24,
          hi: 31,
          cond: "多云",
          precip: "20%",
        },
        sources: [{
          url: "https://example.com/weather",
          title: "天气来源",
        }],
      },
    }],
  };

  harness.server.emitReaderResult(delivery, harness.server.sockets[0]);
  await waitForCondition(
    () => harness.scenario.readerResultAcks.length === 1,
    "reader-result rendered ACK",
  );

  assert.deepEqual(harness.scenario.readerResultDeliveries, [delivery]);
  const ack = harness.scenario.readerResultAcks[0];
  assert.deepEqual(
    Object.keys(ack).sort(),
    ["contract", "type", "requestId", "correlation", "outcome"].sort(),
  );
  assert.equal(ack.contract, DIRECT_CONTRACT);
  assert.equal(ack.type, "reader-result-ack");
  assert.equal(ack.correlation, delivery.correlation);
  assert.equal(ack.outcome, "rendered");
  assert.equal(harness.server.sockets.length, 1);
  assert.equal(harness.server.sockets[0].readyState, 1);
  assert.equal(
    harness.server.requests.some((request) => request.type === "start"),
    false,
  );
  assert.equal(harness.scenario.microphoneRequests.length, 0);
  assert.equal(harness.api.isActive(), false);
  await disableSnapshot(harness);
});

test("snapshot-mcp 将结构化输出交给现有 Realtime 渲染入口并回精确 ACK", async () => {
  const harness = createHarness({
    contextDeliveryMode: "snapshot-mcp",
    contextSyncEnabled: true,
    activeReading: {
      kind: "pdf",
      file: "book.pdf",
      title: "Snapshot Book",
      pos: 24,
    },
  });
  harness.api.setSelectedEngine("computer_client");
  await waitForRequest(harness, "context-open");
  await waitForRequest(harness, "visual-register");
  const sourceInstanceId =
    harness.scenario.readerVisualRegistrations[0].sourceInstanceId;
  const delivery = {
    contract: REALTIME_OUTPUT_CONTRACT,
    commandKind: "realtime-output",
    correlation: "output-tool-24",
    sourceInstanceId,
    snapshotRevision: 1,
    file: "book.pdf",
    page: 24,
    kind: "tool-status",
    payload: {
      status: "done",
      tool: "reader_task",
      label: "完成",
      detail: "结果已经写入当前 Reader",
    },
  };

  harness.server.emitReaderRealtimeOutput(
    delivery,
    harness.server.sockets[0],
  );
  await waitForCondition(
    () => harness.scenario.readerRealtimeOutputAcks.length === 1,
    "Reader realtime output ACK",
  );

  assert.deepEqual(
    harness.scenario.readerRealtimeOutputDeliveries,
    [{
      contract: delivery.contract,
      correlation: delivery.correlation,
      sourceInstanceId: delivery.sourceInstanceId,
      snapshotRevision: delivery.snapshotRevision,
      file: delivery.file,
      page: delivery.page,
      kind: delivery.kind,
      payload: delivery.payload,
    }],
  );
  const ack = harness.scenario.readerRealtimeOutputAcks[0];
  assert.deepEqual(
    Object.keys(ack).sort(),
    [
      "contract", "type", "requestId", "sessionId", "correlation",
      "sourceInstanceId", "outcome", "error",
    ].sort(),
  );
  assert.equal(ack.type, "reader-realtime-output-ack");
  assert.equal(ack.correlation, delivery.correlation);
  assert.equal(ack.sourceInstanceId, sourceInstanceId);
  assert.equal(ack.outcome, "applied");
  assert.equal(ack.error, null);
  assert.equal(
    harness.server.requests.some((request) => request.type === "start"),
    false,
  );
  assert.equal(harness.scenario.microphoneRequests.length, 0);

  harness.server.emitReaderRealtimeOutput(
    {
      ...delivery,
      correlation: "output-stale-25",
      page: 25,
    },
    harness.server.sockets[0],
  );
  await waitForCondition(
    () => harness.scenario.readerRealtimeOutputAcks.length === 2,
    "stale Reader realtime output ACK",
  );
  assert.equal(harness.scenario.readerRealtimeOutputDeliveries.length, 1);
  assert.equal(harness.scenario.readerRealtimeOutputAcks[1].outcome, "rejected");
  assert.equal(
    harness.scenario.readerRealtimeOutputAcks[1].error,
    "BW_READER_REALTIME_OUTPUT_STALE",
  );
  await disableSnapshot(harness);
});

test("snapshot-mcp 将精确高亮和 Anki 草稿送进 Reader 接收器并逐条回 ACK", async () => {
  const harness = createHarness({
    contextDeliveryMode: "snapshot-mcp",
    contextSyncEnabled: true,
    activeReading: {
      kind: "pdf",
      file: "book.pdf",
      title: "Snapshot Book",
      pos: 24,
    },
  });
  harness.api.setSelectedEngine("computer_client");
  await waitForRequest(harness, "context-open");
  await waitForRequest(harness, "visual-register");
  const sourceInstanceId =
    harness.scenario.readerVisualRegistrations[0].sourceInstanceId;
  const base = {
    contract: REALTIME_OUTPUT_CONTRACT,
    commandKind: "realtime-output",
    sourceInstanceId,
    snapshotRevision: 1,
    file: "book.pdf",
    page: 24,
  };
  const deliveries = [{
    ...base,
    correlation: "output-highlight-text-24",
    kind: "highlight-text",
    payload: {
      mutationId: "c_0123456789abcdef0123456789abcdef",
      file: "book.pdf",
      target: { kind: "pdf", page: 24 },
      text: "精确选中的原文",
      color: "yellow",
      note: null,
    },
  }, {
    ...base,
    correlation: "output-anki-draft-24",
    kind: "anki-draft",
    payload: {
      draftId: "draft-0123456789abcdef0123456789abcdef",
      file: "book.pdf",
      target: { kind: "pdf", page: 24 },
      sourceText: "制作卡片所依据的原文",
      cards: [{
        type: "basic",
        front: "问题",
        back: "答案",
      }],
    },
  }, {
    ...base,
    correlation: "output-generic-anki-draft-24",
    kind: "anki-draft",
    payload: {
      draftId: "draft-fedcba9876543210fedcba9876543210",
      cards: [{
        type: "cloze",
        cloze: "普通卡不需要绑定当前页：{{c1::本地卡库}}先保存。",
      }],
    },
  }];

  for (const delivery of deliveries) {
    harness.server.emitReaderRealtimeOutput(
      delivery,
      harness.server.sockets[0],
    );
  }
  await waitForCondition(
    () => harness.scenario.readerRealtimeOutputAcks.length === 3,
    "Reader exact highlight and Anki draft ACKs",
  );

  assert.deepEqual(
    harness.scenario.readerRealtimeOutputDeliveries,
    deliveries.map(({ commandKind, ...delivery }) => delivery),
  );
  assert.deepEqual(
    harness.scenario.readerRealtimeOutputAcks.map((ack) => ({
      correlation: ack.correlation,
      sourceInstanceId: ack.sourceInstanceId,
      outcome: ack.outcome,
      error: ack.error,
    })),
    deliveries.map((delivery) => ({
      correlation: delivery.correlation,
      sourceInstanceId,
      outcome: "applied",
      error: null,
    })),
  );
  assert.equal(
    harness.server.requests.some((request) => request.type === "start"),
    false,
  );
  assert.equal(harness.scenario.microphoneRequests.length, 0);
  await disableSnapshot(harness);
});

test("snapshot-mcp 对可关联的非法精确输出立即回 rejected ACK且绝不执行", async () => {
  const harness = createHarness({
    contextDeliveryMode: "snapshot-mcp",
    contextSyncEnabled: true,
    activeReading: {
      kind: "pdf",
      file: "book.pdf",
      title: "Snapshot Book",
      pos: 24,
    },
  });
  harness.api.setSelectedEngine("computer_client");
  await waitForRequest(harness, "context-open");
  await waitForRequest(harness, "visual-register");
  const sourceInstanceId =
    harness.scenario.readerVisualRegistrations[0].sourceInstanceId;
  const base = {
    contract: REALTIME_OUTPUT_CONTRACT,
    commandKind: "realtime-output",
    sourceInstanceId,
    snapshotRevision: 1,
    file: "book.pdf",
    page: 24,
  };
  const malformed = [{
    ...base,
    correlation: "invalid-highlight-text-24",
    kind: "highlight-text",
    payload: {
      mutationId: "c_0123456789abcdef0123456789abcdef",
      file: "book.pdf",
      target: { kind: "pdf", page: 24 },
      text: "精确选中的原文",
      color: "red",
      note: null,
    },
  }, {
    ...base,
    correlation: "invalid-anki-draft-24",
    kind: "anki-draft",
    payload: {
      draftId: "draft-0123456789abcdef0123456789abcdef",
      cards: [],
    },
  }];

  malformed.forEach((delivery) => {
    harness.server.emitReaderRealtimeOutput(
      delivery,
      harness.server.sockets[0],
    );
  });
  harness.server.emitReaderRealtimeOutput({
    ...malformed[0],
    correlation: "unsafe correlation",
  }, harness.server.sockets[0]);
  harness.server.emitReaderRealtimeOutput({
    ...malformed[0],
    correlation: "wrong-source-highlight-24",
    sourceInstanceId: "source-0123456789abcdef0123456789abcdef",
  }, harness.server.sockets[0]);
  await waitForCondition(
    () => harness.scenario.readerRealtimeOutputAcks.length === 2,
    "malformed Reader output rejected ACKs",
  );
  await waitForCondition(
    () => harness.scenario.readerRealtimeOutputDiagnostics.length === 6,
    "malformed Reader output diagnostics",
  );

  assert.deepEqual(harness.scenario.readerRealtimeOutputDeliveries, []);
  assert.deepEqual(
    harness.scenario.readerRealtimeOutputAcks.map((ack) => ({
      correlation: ack.correlation,
      sourceInstanceId: ack.sourceInstanceId,
      outcome: ack.outcome,
      error: ack.error,
    })),
    malformed.map((delivery) => ({
      correlation: delivery.correlation,
      sourceInstanceId,
      outcome: "rejected",
      error: "BW_READER_REALTIME_OUTPUT_SCHEMA",
    })),
  );
  assert.deepEqual(
    harness.scenario.readerRealtimeOutputDiagnostics.slice(0, 4),
    [
      "Reader 实时输出拒绝: BW_READER_REALTIME_OUTPUT_SCHEMA (正在回关联拒绝回执)",
      "Reader 实时输出拒绝: BW_READER_REALTIME_OUTPUT_SCHEMA (正在回关联拒绝回执)",
      "Reader 实时输出拒绝: BW_READER_REALTIME_OUTPUT_SCHEMA (身份不可安全关联,未回执)",
      "Reader 实时输出拒绝: BW_READER_REALTIME_OUTPUT_SCHEMA (当前 Reader 来源不匹配,未回执)",
    ],
  );
  assert.deepEqual(
    harness.scenario.readerRealtimeOutputDiagnostics.slice(4),
    [
      "Reader 实时输出拒绝: BW_READER_REALTIME_OUTPUT_SCHEMA (已回关联拒绝回执)",
      "Reader 实时输出拒绝: BW_READER_REALTIME_OUTPUT_SCHEMA (已回关联拒绝回执)",
    ],
  );
  await disableSnapshot(harness);
});

test("snapshot-mcp 只在 MCP 按需请求时发送合成图且不发送 START", async () => {
  const b64 = Buffer.alloc(70000, 0x11).toString("base64");
  const drawingRevision = "dr_0123456789abcdef";
  const harness = createHarness({
    contextDeliveryMode: "snapshot-mcp",
    contextSyncEnabled: true,
    activeReading: {
      kind: "pdf",
      file: "book.pdf",
      title: "Snapshot Book",
      pos: 24,
    },
    readerVisualDrawing: {
      file: "book.pdf",
      page: 24,
      stable: true,
      empty: false,
      drawingRevision,
    },
    readerVisualCapture: {
      media_type: "image/jpeg",
      b64,
    },
  });
  harness.api.setSelectedEngine("computer_client");
  await waitForRequest(harness, "context-open");
  await waitForRequest(harness, "visual-register");
  assert.equal(harness.scenario.readerVisualChunks.length, 0);

  const pullCorrelation = "visual-0123456789abcdef0123456789abcdef";
  harness.server.emitReaderVisual(visualRequest(harness, {
    correlation: pullCorrelation,
  }));
  await waitForCondition(
    () => harness.scenario.readerVisualChunks.filter(
      (chunk) => chunk.correlation === pullCorrelation,
    ).length === 2,
    "reader visual pull chunks",
  );

  assert.equal(harness.scenario.readerVisualCompositeCaptureCalls, 1);
  assert.equal(harness.scenario.readerVisualCaptureCalls, 0);
  assert.deepEqual(
    harness.scenario.readerVisualCompositeCaptureTargets,
    [{ page: 24, scope: "viewport-context" }],
  );
  const pullChunks = harness.scenario.readerVisualChunks.filter(
    (chunk) => chunk.correlation === pullCorrelation,
  );
  assert.equal(
    pullChunks.map((chunk) => chunk.data).join(""),
    b64,
  );
  pullChunks.forEach((chunk, index) => {
    assert.equal(chunk.type, "reader-visual");
    assert.equal(chunk.status, "chunk");
    assert.equal(chunk.chunkIndex, index);
    assert.equal(chunk.chunkCount, 2);
    assert.equal(chunk.scope, "viewport-context");
    assert.equal(chunk.selectionId, null);
    assert.equal(chunk.sourceInstanceId,
      harness.scenario.readerVisualRegistrations[0].sourceInstanceId);
    assert.equal(chunk.snapshotRevision, 1);
    assert.equal(chunk.sessionId,
      harness.scenario.readerVisualRegistrations[0].sessionId);
    assert.ok(Buffer.byteLength(JSON.stringify(chunk), "utf8") < 65536);
  });
  harness.dispatchDocumentEvent("bw-reader-drawing-state", {
    state: "stable",
    file: "book.pdf",
    page: 24,
    drawingRevision,
  });
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(harness.scenario.readerVisualCompositeCaptureCalls, 1);
  assert.equal(harness.scenario.readerVisualChunks.length, 2);
  harness.dispatchDocumentEvent("bw-reader-drawing-state", {
    state: "changed",
    file: "book.pdf",
    page: 24,
    drawingRevision: null,
  });
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(harness.scenario.readerVisualChunks.length, 2);
  assert.equal(
    harness.server.requests.some((request) => request.type === "start"),
    false,
  );
  assert.equal(harness.scenario.microphoneRequests.length, 0);
  await disableSnapshot(harness);
});

test("snapshot-mcp 按需视觉 scope 固定分派且回传身份元数据，不复用旧整页缓存", async () => {
  const drawingRevision = "dr_0123456789abcdef";
  const harness = createHarness({
    contextDeliveryMode: "snapshot-mcp",
    contextSyncEnabled: true,
    activeReading: {
      kind: "pdf",
      file: "book.pdf",
      title: "Snapshot Book",
      pos: 24,
    },
    readerVisualDrawing: {
      file: "book.pdf",
      page: 24,
      stable: true,
      empty: false,
      drawingRevision,
    },
    readerVisualCapture: {
      media_type: "image/jpeg",
      b64: Buffer.alloc(4000, 0x22).toString("base64"),
    },
  });
  harness.api.setSelectedEngine("computer_client");
  await waitForRequest(harness, "context-open");
  await waitForRequest(harness, "visual-register");
  harness.scenario.readerVisualCaptureCalls = 0;
  harness.scenario.readerVisualCaptureTargets.length = 0;
  harness.scenario.readerVisualCompositeCaptureCalls = 0;
  harness.scenario.readerVisualCompositeCaptureTargets.length = 0;

  const cases = [
    {
      scope: "viewport-context",
      selectionId: null,
      capture: "composite",
      target: { page: 24, scope: "viewport-context" },
    },
    {
      scope: "drawing-nearby",
      selectionId: null,
      capture: "ink",
      target: { page: 24, scope: "drawing-nearby" },
    },
    {
      scope: "selection-near",
      selectionId: "r-selection-24",
      capture: "ink",
      target: {
        page: 24,
        scope: "selection-near",
        selectionId: "r-selection-24",
      },
    },
  ];

  for (let index = 0; index < cases.length; index += 1) {
    const item = cases[index];
    const correlation = `visual-scope-${index}`;
    const payload = visualRequest(harness, {
      correlation,
      drawingRevision: item.scope === "viewport-context"
        ? null
        : drawingRevision,
      scope: item.scope,
      selectionId: item.selectionId,
    });
    harness.server.emitReaderVisual(payload);
    await waitForCondition(
      () => harness.scenario.readerVisualChunks.some(
        (chunk) => chunk.correlation === correlation,
      ),
      `scoped reader visual ${item.scope}`,
    );
    const chunk = harness.scenario.readerVisualChunks.find(
      (candidate) => candidate.correlation === correlation,
    );
    assert.equal(chunk.status, "chunk");
    assert.equal(chunk.scope, item.scope);
    assert.equal(chunk.selectionId, item.selectionId);
    assert.equal(chunk.file, "book.pdf");
    assert.equal(chunk.page, 24);
    assert.equal(
      chunk.drawingRevision,
      item.scope === "viewport-context" ? null : drawingRevision,
    );
    if (item.capture === "composite") {
      assert.deepEqual(
        harness.scenario.readerVisualCompositeCaptureTargets.at(-1),
        item.target,
      );
    } else {
      assert.deepEqual(
        harness.scenario.readerVisualCaptureTargets.at(-1),
        item.target,
      );
    }
  }

  assert.equal(harness.scenario.readerVisualCompositeCaptureCalls, 1);
  assert.equal(harness.scenario.readerVisualCaptureCalls, 2);
  assert.equal(
    harness.server.requests.some((request) => request.type === "start"),
    false,
  );
  await disableSnapshot(harness);
});

test("snapshot-mcp 按需视觉 scope 对越权范围与缺失 selectionId 均 fail closed", async () => {
  const invalidPayloads = [
    { scope: "arbitrary-selector" },
    { scope: "selection-near" },
  ];
  for (let index = 0; index < invalidPayloads.length; index += 1) {
    const harness = createHarness({
      contextDeliveryMode: "snapshot-mcp",
      contextSyncEnabled: true,
      activeReading: {
        kind: "pdf",
        file: "book.pdf",
        title: "Snapshot Book",
        pos: 24,
      },
      readerVisualDrawing: {
        file: "book.pdf",
        page: 24,
        stable: true,
        empty: false,
        drawingRevision: "dr_0123456789abcdef",
      },
      readerVisualCapture: {
        media_type: "image/jpeg",
        b64: Buffer.alloc(4000, 0x33).toString("base64"),
      },
    });
    harness.api.setSelectedEngine("computer_client");
    await waitForRequest(harness, "context-open");
    await waitForRequest(harness, "visual-register");
    const socket = harness.server.sockets.at(-1);
    const baselineChunks = harness.scenario.readerVisualChunks.length;
    const payload = visualRequest(harness, {
      correlation: `visual-invalid-${index}`,
      drawingRevision: "dr_0123456789abcdef",
      selectionId: null,
      ...invalidPayloads[index],
    });
    harness.server.emitReaderVisual(payload, socket);
    await waitForCondition(
      () => socket.readyState === 3,
      `invalid visual scope ${index} closes socket`,
    );
    assert.equal(harness.scenario.readerVisualChunks.length, baselineChunks);
    assert.equal(harness.scenario.readerVisualCaptureCalls, 0);
    assert.ok(harness.scenario.readerVisualCompositeCaptureCalls <= 1);
    await disableSnapshot(harness);
  }
});

test("snapshot-mcp 笔迹版本不匹配时拒绝错误版本且不为它重复截图", async () => {
  const harness = createHarness({
    contextDeliveryMode: "snapshot-mcp",
    contextSyncEnabled: true,
    activeReading: {
      kind: "pdf",
      file: "book.pdf",
      title: "Snapshot Book",
      pos: 24,
    },
    readerVisualDrawing: {
      file: "book.pdf",
      page: 24,
      stable: true,
      empty: false,
      drawingRevision: "dr_aaaaaaaaaaaaaaaa",
    },
    readerVisualCapture: {
      media_type: "image/jpeg",
      b64: Buffer.alloc(4000, 0x11).toString("base64"),
    },
  });
  harness.api.setSelectedEngine("computer_client");
  await waitForRequest(harness, "context-open");
  await waitForRequest(harness, "visual-register");

  const correlation = "visual-fedcba9876543210fedcba9876543210";
  harness.server.emitReaderVisual(visualRequest(harness, {
    correlation,
    drawingRevision: "dr_bbbbbbbbbbbbbbbb",
    scope: "drawing-nearby",
  }));
  await waitForCondition(
    () => harness.scenario.readerVisualChunks.some(
      (chunk) => chunk.correlation === correlation,
    ),
    "reader visual unavailable",
  );

  const unavailable = harness.scenario.readerVisualChunks.find(
    (chunk) => chunk.correlation === correlation,
  );
  assert.equal(unavailable.status, "unavailable");
  assert.equal(unavailable.data, "");
  assert.ok(harness.scenario.readerVisualCompositeCaptureCalls <= 1);
  assert.equal(harness.scenario.readerVisualCaptureCalls, 0);
  assert.equal(
    harness.server.requests.some((request) => request.type === "start"),
    false,
  );
  await disableSnapshot(harness);
});

test("snapshot-mcp 对未触发 onclose 的 CLOSED 常驻 WSS 主动验活后只重建一次", async () => {
  const harness = createHarness({
    contextDeliveryMode: "snapshot-mcp",
    contextSyncEnabled: true,
  });
  harness.api.setSelectedEngine("computer_client");
  await waitForRequest(harness, "context-open");
  assert.equal((await harness.api.availability()).state, "idle");
  assert.equal(harness.server.sockets.length, 1);
  const staleSocket = harness.server.sockets[0];
  staleSocket.readyState = 3;

  phoneClick(harness, { trusted: true });
  harness.api.setDialPending(true);
  harness.api.setSelectedEngine("computer_client");
  const started = await harness.api.startFromUserGesture();
  harness.api.setDialPending(false);

  assert.equal(started.ok, true);
  assert.equal(harness.server.sockets.length, 2);
  assert.equal(harness.server.activeSocket, harness.server.sockets[1]);
  assert.equal(
    harness.server.requests.filter((request) => request.type === "start").length,
    1,
  );
  await disableSnapshot(harness);
  await harness.api.stop("test");
});

test("snapshot-mcp 借用 STATUS 静默超时后重建一次并恢复常驻快照", async () => {
  const timers = createManualTimers();
  const harness = createHarness({
    contextDeliveryMode: "snapshot-mcp",
    contextSyncEnabled: true,
    dropFirstBorrowedStatus: true,
    timers,
  });
  harness.api.setSelectedEngine("computer_client");
  await waitForRequest(harness, "context-open");

  const pending = harness.api.availability();
  await waitForCondition(
    () => harness.server.requests.filter(
      (request) => request.type === "status",
    ).length === 1,
    "borrowed STATUS request",
  );
  assert.equal(timers.count(7000), 1);
  timers.runOne(7000);

  const available = await pending;
  assert.equal(available.state, "idle");
  assert.equal(harness.server.sockets.length, 2);
  assert.equal(
    harness.server.requests.filter((request) => request.type === "status").length,
    2,
  );
  assert.equal(
    harness.server.requests.some((request) => request.type === "start"),
    false,
  );
  assert.equal(timers.count(0), 1);
  timers.runOne(0);
  await waitForCondition(
    () => harness.server.requests.filter(
      (request) => request.type === "context-open",
    ).length === 2,
    "restored context-only snapshot link",
  );
  assert.equal(harness.server.sockets.length, 3);
  await disableSnapshot(harness);
});

test("snapshot-mcp 晋升链 stale-OPEN 的首个只读探测超时后重连但绝不重发 START", async () => {
  const timers = createManualTimers();
  const harness = createHarness({
    contextDeliveryMode: "snapshot-mcp",
    contextSyncEnabled: true,
    dropSecondContextModeOnFirstSocket: true,
    timers,
  });
  harness.api.setSelectedEngine("computer_client");
  await waitForRequest(harness, "context-open");

  phoneClick(harness, { trusted: true });
  harness.api.setDialPending(true);
  harness.api.setSelectedEngine("computer_client");
  const pending = harness.api.startFromUserGesture();
  await waitForCondition(
    () => harness.server.requests.filter(
      (request) => request.type === "context-mode",
    ).length === 2,
    "claimed snapshot CONTEXT-MODE probe",
  );
  assert.equal(timers.count(7000), 1);
  timers.runOne(7000);

  const started = await pending;
  harness.api.setDialPending(false);
  assert.equal(started.ok, true);
  assert.equal(harness.server.sockets.length, 2);
  assert.equal(harness.server.activeSocket, harness.server.sockets[1]);
  assert.equal(
    harness.server.requests.filter((request) => request.type === "start").length,
    1,
    "only the fresh, proven channel may send the single authorized START",
  );
  await disableSnapshot(harness);
  await harness.api.stop("test");
});

test("START 已发送但结果超时时绝不重发，并完整释放本地资源", async () => {
  const timers = createManualTimers();
  const harness = createHarness({
    contextDeliveryMode: "snapshot-mcp",
    contextSyncEnabled: true,
    deferStartResult: true,
    deferSocketClose: true,
    timers,
  });
  harness.api.setSelectedEngine("computer_client");
  await waitForRequest(harness, "context-open");

  phoneClick(harness, { trusted: true });
  harness.api.setDialPending(true);
  harness.api.setSelectedEngine("computer_client");
  const pending = harness.api.startFromUserGesture();
  await waitForRequest(harness, "start");
  assert.equal(
    harness.server.requests.filter((request) => request.type === "start").length,
    1,
  );
  assert.equal(timers.count(45000), 1);
  timers.runOne(45000);

  await assert.rejects(
    pending,
    (error) => error?.code === "BW_COMPUTER_VOICE_DIRECT_START_UNKNOWN",
  );
  assert.equal(
    harness.server.requests.filter((request) => request.type === "start").length,
    1,
    "START 结果未知时不得自动发送第二次切换请求",
  );
  assert.equal(harness.api.isActive(), false);
  assert.equal(harness.scenario.microphoneTracks[0].stopped, true);
  assert.equal(harness.scenario.audioContexts[0].closed, true);
  assert.equal(harness.server.sockets.length, 1);
  assert.equal(harness.server.sockets[0].readyState, 2);
  assert.equal(harness.server.deferredSocketCloses.length, 1);
  assert.equal(
    timers.count(1000),
    0,
    "START timeout 后也必须先等旧 WSS 真正关闭",
  );

  harness.server.resolveDeferredSocketClose();
  await waitForCondition(
    () => timers.count(1000) === 1,
    "snapshot reconnect timer after failed START close",
  );
  timers.runOne(1000);
  await waitForCondition(
    () => harness.server.requests.filter(
      (request) => request.type === "context-open",
    ).length === 2,
    "snapshot reconnect after failed START",
  );
  assert.equal(
    harness.server.requests.filter((request) => request.type === "start").length,
    1,
  );
});

test("STOP 后必须等旧 WSS 完全关闭才恢复 snapshot-mcp 常驻连接", async () => {
  const timers = createManualTimers();
  const harness = createHarness({
    contextDeliveryMode: "snapshot-mcp",
    contextSyncEnabled: true,
    deferSocketClose: true,
    timers,
  });
  harness.api.setSelectedEngine("computer_client");
  await waitForRequest(harness, "context-open");

  phoneClick(harness, { trusted: true });
  harness.api.setDialPending(true);
  harness.api.setSelectedEngine("computer_client");
  await harness.api.startFromUserGesture();
  harness.api.setDialPending(false);

  const stopping = harness.api.stop("test");
  await waitForCondition(
    () => harness.server.deferredSocketCloses.length === 1,
    "active WSS close",
  );
  assert.equal(harness.server.sockets.length, 1);
  assert.equal(
    timers.count(1000),
    0,
    "旧连接 close promise settle 前不得安排替代连接",
  );

  harness.server.resolveDeferredSocketClose();
  await stopping;
  await waitForCondition(
    () => timers.count(1000) === 1,
    "snapshot reconnect timer after close",
  );
  timers.runOne(1000);
  await waitForCondition(
    () => harness.server.requests.filter(
      (request) => request.type === "context-open",
    ).length === 2,
    "snapshot reconnect after close",
  );
  assert.equal(harness.server.sockets.length, 2);
});

test("snapshot-mcp 晋升等待期间被停止会关闭认领到的旧 WSS", async () => {
  const harness = createHarness({
    contextDeliveryMode: "snapshot-mcp",
    contextSyncEnabled: true,
    deferContextOpen: true,
  });
  harness.api.setSelectedEngine("computer_client");
  await waitForCondition(
    () => harness.server.deferredContextOpens.length === 1,
    "deferred context-open",
  );
  const snapshotSocket = harness.server.sockets[0];

  phoneClick(harness, { trusted: true });
  const rejected = assert.rejects(
    harness.api.startFromUserGesture(),
    (error) => error?.code === "BW_COMPUTER_VOICE_DIRECT_CANCELLED",
  );
  await harness.api.stop("test-cancel-during-claim");
  harness.server.resolveDeferredContextOpen();
  await rejected;
  await new Promise((resolve) => setImmediate(resolve));

  assert.equal(snapshotSocket.readyState, 3);
  assert.equal(harness.api.isActive(), false);
  assert.equal(
    harness.server.requests.some((request) => request.type === "start"),
    false,
  );
  await disableSnapshot(harness);
});

test("snapshot-mcp 后台挂起的重连计时器在回到前台时立即恢复且不重复连接", async () => {
  const harness = createHarness({
    contextDeliveryMode: "snapshot-mcp",
    contextSyncEnabled: true,
    activeReading: {
      kind: "pdf",
      file: "book.pdf",
      title: "Resume Book",
      pos: 18,
    },
  });
  harness.api.setSelectedEngine("computer_client");
  await waitForRequest(harness, "context-open");
  await waitForCondition(
    () => harness.scenario.activeReadingRequests.length >= 1,
    "initial snapshot before background",
  );
  assert.equal(harness.server.sockets.length, 1);

  const firstSocket = harness.server.sockets[0];
  firstSocket.readyState = 3;
  firstSocket.onclose?.();
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(harness.server.sockets.length, 1);

  harness.setVisibilityState("visible");
  harness.dispatchDocumentEvent("visibilitychange");
  await waitForCondition(
    () => harness.server.sockets.length === 2,
    "foreground snapshot reconnect",
    500,
  );
  await waitForCondition(
    () => harness.server.requests.filter(
      (request) => request.type === "context-open",
    ).length === 2,
    "foreground context-open",
    500,
  );

  harness.dispatchWindowEvent("pageshow");
  harness.dispatchWindowEvent("online");
  await new Promise((resolve) => setTimeout(resolve, 25));
  assert.equal(harness.server.sockets.length, 2);
  assert.equal(
    harness.server.requests.some((request) => request.type === "start"),
    false,
  );
  assert.equal(harness.scenario.microphoneRequests.length, 0);
  await disableSnapshot(harness);
});

test("snapshot-mcp pagehide 立即释放 WSS，不等待异步清空再恢复", async () => {
  const harness = createHarness({
    contextDeliveryMode: "snapshot-mcp",
    contextSyncEnabled: true,
    activeReading: {
      kind: "pdf",
      file: "book.pdf",
      title: "Resume Race Book",
      pos: 20,
    },
  });
  harness.api.setSelectedEngine("computer_client");
  await waitForRequest(harness, "context-open");
  await waitForCondition(
    () => harness.scenario.activeReadingRequests.length >= 1,
    "snapshot before pagehide",
  );
  assert.equal(harness.server.sockets.length, 1);
  const firstSocket = harness.server.sockets[0];

  harness.dispatchWindowEvent("pagehide");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(firstSocket.readyState, 3);
  assert.equal(harness.scenario.contextClearRequests.length, 0);
  harness.dispatchWindowEvent("pageshow");
  harness.dispatchDocumentEvent("visibilitychange");
  harness.dispatchWindowEvent("online");
  await waitForCondition(
    () => harness.server.sockets.length === 2,
    "snapshot reconnect after pagehide",
    500,
  );
  await waitForCondition(
    () => harness.server.requests.filter(
      (request) => request.type === "context-open",
    ).length === 2,
    "context-open after pagehide",
    500,
  );
  assert.equal(
    harness.server.requests.some((request) => request.type === "start"),
    false,
  );
  assert.equal(harness.scenario.microphoneRequests.length, 0);

  harness.dispatchWindowEvent("pageshow");
  harness.dispatchWindowEvent("online");
  await new Promise((resolve) => setTimeout(resolve, 25));
  assert.equal(harness.server.sockets.length, 2);
  await disableSnapshot(harness);
});

test("PWA 与扩展只由当前 UI owner 维护 Windows 快照连接", async () => {
  const page = createHarness({
    contextDeliveryMode: "snapshot-mcp",
    contextSyncEnabled: true,
    uiOwner: "pwa",
  });
  page.api.setSelectedEngine("computer_client");
  await waitForRequest(page, "context-open");
  const pageSocket = page.server.sockets[0];

  page.setUiOwner("extension");
  page.dispatchDocumentEvent("bw:book-ui-owner-changed", {
    owner: "extension",
  });
  await waitForCondition(
    () => pageSocket.readyState === 3,
    "page owner releases snapshot socket",
    500,
  );

  const extension = createHarness({
    contextDeliveryMode: "snapshot-mcp",
    contextSyncEnabled: true,
    uiOwner: "pwa",
    extensionWorld: true,
  });
  extension.api.setSelectedEngine("computer_client");
  await new Promise((resolve) => setTimeout(resolve, 25));
  assert.equal(extension.server.sockets.length, 0);
  const unavailable = await extension.api.availability();
  assert.equal(unavailable.code, "BW_COMPUTER_VOICE_UI_NOT_OWNER");

  extension.setUiOwner("extension");
  extension.dispatchDocumentEvent("bw:book-ui-owner-changed", {
    owner: "extension",
  });
  await waitForRequest(extension, "context-open");
  assert.equal(extension.server.sockets.length, 1);
  await disableSnapshot(extension);
});

test("同源普通网页没有 book owner marker 时不被扩展门禁误吞", async () => {
  const extension = createHarness({
    extensionWorld: true,
    uiOwner: "",
  });
  const available = await extension.api.availability();
  assert.equal(available.state, "idle");
  assert.equal(extension.server.sockets.length, 1);
});

test("snapshot-mcp 前台唤醒独立于普通语音模型，关闭同步或活动通话不重复建链", async () => {
  const otherEngine = createHarness({
    contextDeliveryMode: "snapshot-mcp",
    contextSyncEnabled: true,
  });
  otherEngine.api.setSelectedEngine("codex");
  otherEngine.dispatchDocumentEvent("visibilitychange");
  otherEngine.dispatchWindowEvent("pageshow");
  otherEngine.dispatchWindowEvent("online");
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(otherEngine.server.sockets.length, 1);
  assert.equal(
    otherEngine.server.requests.some((request) => request.type === "start"),
    false,
  );
  assert.equal(otherEngine.scenario.microphoneRequests.length, 0);
  await disableSnapshot(otherEngine);

  const syncDisabled = createHarness({
    contextDeliveryMode: "snapshot-mcp",
    contextSyncEnabled: false,
  });
  syncDisabled.api.setSelectedEngine("computer_client");
  syncDisabled.dispatchDocumentEvent("visibilitychange");
  syncDisabled.dispatchWindowEvent("pageshow");
  syncDisabled.dispatchWindowEvent("online");
  await new Promise((resolve) => setTimeout(resolve, 10));
  assert.equal(syncDisabled.server.sockets.length, 0);

  const activeCall = createHarness({
    contextDeliveryMode: "snapshot-mcp",
    contextSyncEnabled: true,
    activeReading: {
      kind: "pdf",
      file: "book.pdf",
      pos: 19,
    },
  });
  activeCall.api.setSelectedEngine("computer_client");
  await waitForRequest(activeCall, "context-open");
  phoneClick(activeCall, { trusted: true });
  const started = await activeCall.api.startFromUserGesture();
  assert.equal(started.ok, true);
  const socketCount = activeCall.server.sockets.length;
  activeCall.dispatchDocumentEvent("visibilitychange");
  activeCall.dispatchWindowEvent("pageshow");
  activeCall.dispatchWindowEvent("online");
  await new Promise((resolve) => setTimeout(resolve, 25));
  assert.equal(activeCall.server.sockets.length, socketCount);
  assert.equal(
    activeCall.server.requests.filter(
      (request) => request.type === "start",
    ).length,
    1,
  );
  assert.equal(activeCall.scenario.microphoneRequests.length, 1);
  await disableSnapshot(activeCall);
  await activeCall.api.stop("test");
});

test("snapshot-mcp 仅转发 Pi ACK 绑定的 vbook 真实卷页并拒绝跨页旧选区", async () => {
  const harness = createHarness({
    contextDeliveryMode: "snapshot-mcp",
    contextSyncEnabled: true,
    activeReading: {
      kind: "pdf",
      file: "vbook:g_3e5d696e85",
      title: "Merged Book",
      pos: 31,
      selection: "old page selection",
      sel_page: 30,
    },
    activeReadingCanonical: null,
    serverActiveReading: {
      kind: "pdf",
      file: "vbook:g_3e5d696e85",
      title: "Stale Pi State",
      pos: 29,
      vbook: true,
      member: "books/part-1.pdf",
      member_pos: 29,
      selection: "stale server selection",
      has_selection: true,
      sel_page: 29,
      ts: 1_750_000_000,
    },
  });
  harness.api.setSelectedEngine("computer_client");
  await waitForRequest(harness, "context-open");
  await new Promise((resolve) => setTimeout(resolve, 300));
  assert.equal(
    harness.scenario.activeReadingRequests.length,
    0,
    "未拿到与当前 vbook 页绑定的服务端 ACK 前不得把视图标识当本地路径发送",
  );
  harness.scenario.activeReadingCanonical = {
    kind: "pdf",
    file: "books/part-2.pdf",
    page: 7,
    viewFile: "vbook:g_3e5d696e85",
    viewPage: 31,
  };
  await waitForCondition(
    () => harness.scenario.activeReadingRequests.length >= 1,
    "vbook active-reading",
  );
  const forwarded = harness.scenario.activeReadingRequests[0].active;
  assert.equal(harness.scenario.activeReadingFetches, 0);
  assert.deepEqual(Object.keys(forwarded).sort(), [
    "file",
    "kind",
    "observedAtEpochMs",
    "page",
    "selection",
    "selectionState",
    "sourceInstanceId",
    "title",
    "viewFile",
    "viewPage",
  ]);
  assert.equal(forwarded.file, "books/part-2.pdf");
  assert.equal(forwarded.title, "Merged Book");
  assert.equal(forwarded.page, 7);
  assert.equal(forwarded.viewFile, "vbook:g_3e5d696e85");
  assert.equal(forwarded.viewPage, 31);
  assert.equal(forwarded.selectionState, "cleared");
  assert.equal(forwarded.selection, null);
  assert.match(forwarded.sourceInstanceId, /^source-[A-Za-z0-9_-]{22}$/);

  const withoutSelection = { ...harness.scenario.activeReading, pos: 32 };
  delete withoutSelection.selection;
  delete withoutSelection.sel_page;
  harness.scenario.activeReading = withoutSelection;
  harness.scenario.activeReadingCanonical = null;
  await new Promise((resolve) => setTimeout(resolve, 300));
  assert.equal(
    harness.scenario.activeReadingRequests.length,
    1,
    "翻页后旧 canonical 必须立即失效",
  );
  harness.scenario.activeReadingCanonical = {
    kind: "pdf",
    file: "books/part-2.pdf",
    page: 8,
    viewFile: "vbook:g_3e5d696e85",
    viewPage: 32,
  };
  await waitForCondition(
    () => harness.scenario.activeReadingRequests.some(
      (request) =>
        request.active.selectionState === "unknown"
        && request.active.page === 8
        && request.active.viewPage === 32,
    ),
    "selection unknown realtime update",
  );
  assert.match(SOURCE, /var ACTIVE_READING_POLL_MS = 250;/);
  assert.match(SOURCE, /var ACTIVE_READING_HEARTBEAT_MS = 60000;/);
  assert.match(
    SOURCE,
    /now - pump\.lastSentAt < ACTIVE_READING_HEARTBEAT_MS/,
  );
  await disableSnapshot(harness);
  await harness.api.stop("test");
});

test("snapshot-mcp 跳过畸形 PWA pend 且后续合法状态可恢复实时发送", async () => {
  const base = {
    kind: "pdf",
    file: "book.pdf",
    title: "Valid Book",
    pos: 8,
  };
  const malformed = [
    { ...base, kind: "video" },
    { ...base, file: "" },
    { ...base, file: `book${String.fromCharCode(0)}.pdf` },
    { ...base, pos: -1 },
    { ...base, pos: Number.MAX_SAFE_INTEGER + 1 },
    { ...base, pos: "" },
    { ...base, pos: "p".repeat(257) },
    { ...base, pos: `page${String.fromCharCode(31)}` },
    { ...base, title: "t".repeat(1025) },
    { ...base, title: `bad${String.fromCharCode(127)}title` },
  ];
  const harness = createHarness({
    contextDeliveryMode: "snapshot-mcp",
    contextSyncEnabled: true,
    activeReading: malformed[0],
  });
  harness.api.setSelectedEngine("computer_client");
  await waitForRequest(harness, "context-open");
  for (const candidate of malformed) {
    harness.scenario.activeReading = candidate;
    await new Promise((resolve) => setTimeout(resolve, 280));
    assert.equal(harness.scenario.activeReadingRequests.length, 0);
  }

  harness.scenario.activeReading = {
    ...base,
    title: null,
    selection: "recovered selection",
    sel_page: 8,
  };
  await waitForCondition(
    () => harness.scenario.activeReadingRequests.length === 1,
    "valid active-reading after malformed local states",
    1000,
  );
  assert.equal(harness.scenario.activeReadingFetches, 0);
  assert.equal(
    harness.scenario.activeReadingRequests[0].active.selectionState,
    "active",
  );
  assert.equal(
    harness.scenario.activeReadingRequests[0].active.selection,
    "recovered selection",
  );
  assert.equal(harness.scenario.activeReadingRequests[0].active.title, null);
  await disableSnapshot(harness);
  await harness.api.stop("test");
});

test("切回 legacy-inject 前先清空 Windows 快照，再恢复 Pi 旧注入", async () => {
  const harness = createHarness({
    contextDeliveryMode: "legacy-inject",
    contextSyncEnabled: true,
    activeReading: {
      kind: "pdf",
      file: "book.pdf",
      pos: 9,
    },
  });
  harness.api.setSelectedEngine("computer_client");
  await waitForRequest(harness, "context-clear");
  await waitForCondition(
    () => harness.scenario.contextModePosts.length >= 1,
    "legacy context mode post",
  );
  assert.equal(harness.scenario.contextClearRequests.length, 1);
  assert.deepEqual(harness.scenario.contextModePosts[0], {
    enabled: true,
    deliveryMode: "legacy-inject",
  });
  assert.equal(
    harness.server.requests.some(
      (request) => request.type === "context-open",
    ),
    false,
  );
  assert.equal(
    harness.server.requests.some((request) => request.type === "start"),
    false,
  );
  await disableSnapshot(harness);
});

test("设置页模式开关先清快照并关闭旧 WSS，再原子切到 legacy 且不发送 START", async () => {
  const harness = createHarness({
    contextDeliveryMode: "snapshot-mcp",
    contextSyncEnabled: true,
    activeReading: {
      kind: "pdf",
      file: "book.pdf",
      pos: 9,
    },
  });
  harness.api.setSelectedEngine("computer_client");
  await waitForRequest(harness, "context-open");
  const snapshotSocket = harness.server.sockets[0];

  const changed = await harness.api.setContextDeliveryMode(
    "legacy-inject",
  );

  assert.equal(changed.ok, true);
  assert.equal(changed.mode, "legacy-inject");
  assert.equal(snapshotSocket.readyState, 3);
  assert.equal(harness.scenario.contextClearRequests.length, 1);
  assert.equal(
    harness.server.requests.filter(
      (request) => request.type === "context-mode-set",
    ).length,
    1,
  );
  assert.deepEqual(harness.scenario.contextModePosts.at(-1), {
    enabled: true,
    deliveryMode: "legacy-inject",
  });
  assert.equal(
    harness.server.requests.some((request) => request.type === "start"),
    false,
  );
  assert.equal(harness.server.sockets.at(-1).readyState, 3);
  await disableSnapshot(harness);
});

test("关闭上下文同步会先清空 Windows 快照且不触发音频 START", async () => {
  const harness = createHarness({
    contextDeliveryMode: "snapshot-mcp",
    contextSyncEnabled: true,
    activeReading: {
      kind: "pdf",
      file: "book.pdf",
      pos: 10,
    },
  });
  harness.api.setSelectedEngine("computer_client");
  await waitForRequest(harness, "context-open");
  await waitForCondition(
    () => harness.scenario.activeReadingRequests.length >= 1,
    "snapshot before disable",
  );
  const socket = harness.server.sockets[0];
  harness.scenario.contextSyncEnabled = false;
  await harness.api.contextSyncChanged();
  assert.equal(harness.scenario.contextClearRequests.length, 1);
  assert.equal(socket.readyState, 3);
  assert.equal(
    harness.server.requests.some((request) => request.type === "start"),
    false,
  );
  await disableSnapshot(harness);
});

test("选择模型、STATUS 与非 computer_client 的真实电话点击都不申请麦克风", async () => {
  const selected = createHarness();
  assert.equal(selected.api.setSelectedEngine("computer_client"), true);
  assert.equal(selected.scenario.audioContexts.length, 0);
  assert.equal(selected.scenario.microphoneRequests.length, 0);
  const availability = await selected.api.availability();
  assert.equal(availability.state, "idle");
  assert.equal(selected.scenario.microphoneRequests.length, 0);

  const otherEngine = createHarness();
  assert.equal(otherEngine.api.setSelectedEngine("native"), false);
  phoneClick(otherEngine, { trusted: true });
  await assert.rejects(
    otherEngine.api.startFromUserGesture(),
    (error) => error?.code === "BW_COMPUTER_VOICE_GESTURE_REQUIRED",
  );
  assert.equal(otherEngine.scenario.audioContexts.length, 0);
  assert.equal(otherEngine.scenario.microphoneRequests.length, 0);
  assert.equal(otherEngine.server.sockets.length, 0);
});

test("复习模式在捕获阶段阻止电脑桥接申请麦克风", async () => {
  const harness = createHarness({ realtimeVoiceAllowed: false });
  harness.api.setSelectedEngine("computer_client");
  phoneClick(harness, { trusted: true });
  await assert.rejects(
    harness.api.startFromUserGesture(),
    (error) => error?.code === "BW_COMPUTER_VOICE_GESTURE_REQUIRED",
  );
  assert.equal(harness.scenario.audioContexts.length, 0);
  assert.equal(harness.scenario.microphoneRequests.length, 0);
  assert.equal(harness.server.sockets.length, 0);
});

test("engine revision 阻止迟到配置把新选择覆写并误触发采音", async () => {
  const harness = createHarness();
  const staleRevision = harness.api.reserveSelectedEngineUpdate();
  const currentRevision = harness.api.reserveSelectedEngineUpdate();
  assert.ok(currentRevision > staleRevision);
  assert.equal(
    harness.api.setSelectedEngine("native", currentRevision),
    false,
  );
  assert.equal(
    harness.api.setSelectedEngine("computer_client", staleRevision),
    false,
    "迟到的旧配置不得恢复 computer_client",
  );
  phoneClick(harness, { trusted: true });
  assert.equal(harness.scenario.microphoneRequests.length, 0);

  const nextRevision = harness.api.reserveSelectedEngineUpdate();
  assert.equal(
    harness.api.setSelectedEngine("computer_client", nextRevision),
    true,
  );
  phoneClick(harness, { trusted: true });
  assert.equal(
    harness.scenario.microphoneRequests.length,
    1,
    "只有当前 revision 的 computer_client 真实点击可申请一次麦克风",
  );
  harness.api.cancelPreparedGesture();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(harness.scenario.microphoneTracks[0].stopped, true);
});

test("设置保存中的未知引擎围栏会立即撤销旧 computer_client 采音资格", async () => {
  const harness = createHarness();
  harness.api.setSelectedEngine("computer_client");
  const revision = harness.api.beginSelectedEngineUpdate();
  phoneClick(harness, { trusted: true });
  assert.equal(harness.scenario.microphoneRequests.length, 0);

  assert.equal(
    harness.api.setSelectedEngine("computer_client", revision),
    true,
  );
  assert.equal(
    harness.api.isSelectedEngineRevisionCurrent(revision),
    true,
  );
  phoneClick(harness, { trusted: true });
  assert.equal(harness.scenario.microphoneRequests.length, 1);
  harness.api.cancelPreparedGesture();
});

test("设置 mutation token 不会被拨号 GET 的更高 revision 提前清除", async () => {
  const harness = createHarness();
  harness.api.setSelectedEngine("computer_client");
  const mutationRevision = harness.api.beginSelectedEngineUpdate();
  const dialRevision = harness.api.reserveSelectedEngineUpdate();
  assert.ok(dialRevision > mutationRevision);

  assert.equal(
    harness.api.setSelectedEngine("computer_client", dialRevision),
    false,
    "POST 在途时拨号 GET 只能读到旧值，不能清除 mutation fence",
  );
  assert.equal(
    harness.api.isSelectedEngineRevisionCurrent(dialRevision),
    false,
  );
  phoneClick(harness, { trusted: true });
  assert.equal(harness.scenario.microphoneRequests.length, 0);

  assert.equal(
    harness.api.setSelectedEngine("native", mutationRevision),
    false,
  );
  assert.equal(
    harness.api.isSelectedEngineRevisionCurrent(mutationRevision),
    true,
    "只有对应 mutation token 的 ACK 可以解除围栏",
  );
  assert.equal(
    harness.api.setSelectedEngine("computer_client", dialRevision),
    false,
    "mutation ACK 后，先前在途的旧 GET 也必须保持失效",
  );
  phoneClick(harness, { trusted: true });
  assert.equal(harness.scenario.microphoneRequests.length, 0);
});

test("配置 fetch 在途时第二次真实电话点击撤销 gesture，迟到 START 不可采音", async () => {
  const harness = createHarness();
  harness.api.setSelectedEngine("computer_client");
  phoneClick(harness, { trusted: true });
  assert.equal(harness.scenario.microphoneRequests.length, 1);
  harness.api.setDialPending(true);
  phoneClick(harness, { trusted: true });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(harness.scenario.microphoneRequests.length, 1);
  assert.equal(harness.scenario.microphoneTracks[0].stopped, true);
  await assert.rejects(
    harness.api.startFromUserGesture(),
    (error) => error?.code === "BW_COMPUTER_VOICE_GESTURE_REQUIRED",
  );
  assert.equal(harness.server.requests.length, 0);
  assert.equal(harness.server.sockets.length, 0);
  harness.api.setDialPending(false);
});

test("网页麦克风权限拒绝时不发送 HELLO/START，并完整释放播放表面", async () => {
  const harness = createHarness({
    microphonePlan: ["permission-reject"],
  });
  await assert.rejects(
    startWithTrustedGesture(harness),
    (error) => error?.code === "BW_COMPUTER_VOICE_MICROPHONE_PERMISSION",
  );
  assert.equal(harness.scenario.microphoneRequests.length, 1);
  assert.equal(harness.server.sockets.length, 0);
  assert.equal(harness.server.requests.length, 0);
  assert.equal(harness.api.isActive(), false);
  assert.equal(harness.scenario.audioContexts.at(-1).closed, true);
});

test("STATUS 保留结构化 lastError，非法 HRESULT 或时间 fail closed", async () => {
  const lastError = {
    failureId: "failure-123",
    code: "BW_COMPUTER_VOICE_DIRECT_MEDIA_START_FAILED",
    stage: "virtual-microphone.render",
    hresult: "0x80070490",
    atUtc: "2026-07-29T12:34:56.000Z",
  };
  const valid = createHarness({ lastError });
  const availability = await valid.api.availability();
  assert.equal(availability.state, "idle");
  assert.deepEqual(availability.status.lastError, lastError);
  assert.equal(valid.scenario.microphoneRequests.length, 0);

  for (const invalidLastError of [
    { ...lastError, hresult: "80070490" },
    { ...lastError, atUtc: "not-a-time" },
    { ...lastError, injected: true },
  ]) {
    const invalid = createHarness({ lastError: invalidLastError });
    const result = await invalid.api.availability();
    assert.equal(result.state, "offline");
    assert.equal(result.code, "BW_COMPUTER_VOICE_DIRECT_SCHEMA");
    assert.equal(invalid.scenario.microphoneRequests.length, 0);
  }
});

test("真实点击 start lease 五秒过期后不能迟到启动", async () => {
  const timers = createManualTimers();
  const harness = createHarness({ timers });
  harness.api.setSelectedEngine("computer_client");
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

test("原生 App 只有精确 loopback origin 与原生标记同时成立才直连", async () => {
  const nativeApp = createHarness({
    origin: NATIVE_APP_ORIGIN,
    nativeComputerVoice: true,
  });
  const available = await nativeApp.api.availability();
  assert.equal(available.state, "idle");
  assert.equal(nativeApp.server.sockets.length, 1);
  assert.equal(nativeApp.scenario.socketUrls[0], ENDPOINT);

  for (const candidate of [
    { origin: NATIVE_APP_ORIGIN, nativeComputerVoice: false },
    { origin: "http://127.0.0.1:43130", nativeComputerVoice: true },
    { origin: "http://localhost:43129", nativeComputerVoice: true },
  ]) {
    const rejected = createHarness(candidate);
    const result = await rejected.api.availability();
    assert.equal(
      result.code,
      "BW_COMPUTER_VOICE_DIRECT_RELAY_REQUIRED",
      candidate.origin,
    );
    assert.equal(rejected.server.sockets.length, 0, candidate.origin);
  }
});

test("原生 App 的 eph-ctx-sync=1 完成 context 握手并启动快照泵", async () => {
  const contextSyncStorage = new Map([["eph-ctx-sync", "1"]]);
  const harness = createHarness({
    origin: NATIVE_APP_ORIGIN,
    nativeComputerVoice: true,
    contextSyncStorage,
    contextDeliveryMode: "snapshot-mcp",
    activeReading: {
      kind: "pdf",
      file: "localbook:app-snapshot",
      title: "Native App Snapshot",
      pos: 7,
      selection: "",
    },
  });

  harness.api.setSelectedEngine("computer_client");
  await waitForRequest(harness, "context-open");
  await waitForCondition(
    () => harness.scenario.journalCalls.length >= 1,
    "native App context journal pump",
  );
  await waitForCondition(
    () => harness.scenario.activeReadingRequests.length >= 1,
    "native App active-reading pump",
  );

  assert.equal(harness.scenario.socketUrls[0], CONTEXT_ENDPOINT);
  const actions = harness.server.requests.map((request) => request.type);
  assert.ok(actions.indexOf("context-mode") >= 0);
  assert.ok(actions.indexOf("context-open") > actions.indexOf("context-mode"));
  assert.ok(
    actions.indexOf("visual-register") > actions.indexOf("context-open"),
  );
  assert.deepEqual(harness.scenario.contextModePosts[0], {
    enabled: true,
    deliveryMode: "snapshot-mcp",
  });

  contextSyncStorage.set("eph-ctx-sync", "0");
  await harness.api.contextSyncChanged();
});

test("原生 App 前台标志是专用快照可见性的权威而不受隐藏 WebView 误杀", async () => {
  const contextSyncStorage = new Map([["eph-ctx-sync", "1"]]);
  const foreground = createHarness({
    origin: NATIVE_APP_ORIGIN,
    nativeComputerVoice: true,
    nativeReaderForeground: true,
    visibilityState: "hidden",
    contextSyncStorage,
    contextDeliveryMode: "snapshot-mcp",
  });

  await waitForRequest(foreground, "context-open");
  assert.equal(foreground.server.sockets.length, 1);
  assert.equal(foreground.scenario.socketUrls[0], CONTEXT_ENDPOINT);

  const background = createHarness({
    origin: NATIVE_APP_ORIGIN,
    nativeComputerVoice: true,
    nativeReaderForeground: false,
    visibilityState: "visible",
    contextSyncStorage: new Map([["eph-ctx-sync", "1"]]),
    contextDeliveryMode: "snapshot-mcp",
  });
  await new Promise((resolve) => setTimeout(resolve, 25));
  assert.equal(background.server.sockets.length, 0);

  contextSyncStorage.set("eph-ctx-sync", "0");
  await foreground.api.contextSyncChanged();
});

test("原生语音占用主 WSS 时专用快照断线保留配置并有界退避重连", async () => {
  const timers = createManualTimers();
  const contextSyncStorage = new Map([["eph-ctx-sync", "1"]]);
  const harness = createHarness({
    origin: NATIVE_APP_ORIGIN,
    nativeComputerVoice: true,
    nativeComputerVoiceState: {
      active: true,
      busy: true,
      sessionId: "native-voice-session",
    },
    contextSyncStorage,
    contextDeliveryMode: "snapshot-mcp",
    timers,
  });

  await waitForRequest(harness, "context-open");
  const firstSocket = harness.server.sockets[0];
  firstSocket.readyState = 3;
  firstSocket.onclose?.();
  await waitForCondition(
    () => timers.count(1000) === 1,
    "first dedicated snapshot reconnect",
  );
  timers.runOne(1000);
  await waitForCondition(
    () => harness.server.requests.filter(
      (request) => request.type === "context-open",
    ).length === 2,
    "dedicated snapshot reconnect while native voice remains active",
  );
  assert.equal(
    harness.server.requests.some((request) => request.type === "start"),
    false,
  );
  assert.equal(harness.scenario.microphoneRequests.length, 0);

  contextSyncStorage.set("eph-ctx-sync", "0");
  await harness.api.contextSyncChanged();
});

test("原生专用快照连续建链失败按 1/2/4/8/15 秒封顶退避", async () => {
  const timers = createManualTimers();
  const harness = createHarness({
    origin: NATIVE_APP_ORIGIN,
    nativeComputerVoice: true,
    nativeComputerVoiceState: {
      active: true,
      busy: true,
      sessionId: "native-voice-session",
    },
    contextSyncStorage: new Map([["eph-ctx-sync", "1"]]),
    contextDeliveryMode: "snapshot-mcp",
    offline: true,
    timers,
  });

  for (const delay of [1000, 2000, 4000, 8000, 15000, 15000]) {
    await waitForCondition(
      () => timers.count(delay) === 1,
      `snapshot reconnect delay ${delay}`,
    );
    timers.runOne(delay);
  }
  await waitForCondition(
    () => timers.count(15000) === 1,
    "bounded snapshot reconnect delay remains capped",
  );
  assert.equal(harness.server.sockets.length, 7);
  assert.equal(
    harness.server.requests.some((request) => request.type === "start"),
    false,
  );
});

test("原生专用快照前台唤醒先用只读 CONTEXT-MODE 验活并重建 stale OPEN", async () => {
  const timers = createManualTimers();
  const contextSyncStorage = new Map([["eph-ctx-sync", "1"]]);
  const harness = createHarness({
    origin: NATIVE_APP_ORIGIN,
    nativeComputerVoice: true,
    nativeComputerVoiceState: {
      active: true,
      busy: true,
      sessionId: "native-voice-session",
    },
    contextSyncStorage,
    contextDeliveryMode: "snapshot-mcp",
    dropSecondContextModeOnFirstSocket: true,
    timers,
  });

  await waitForRequest(harness, "context-open");
  harness.dispatchWindowEvent("bw-native-reader-foreground");
  await waitForCondition(
    () => harness.server.requests.filter(
      (request) => request.type === "context-mode",
    ).length === 2,
    "foreground read-only context-mode probe",
  );
  assert.equal(timers.count(7000), 1);
  timers.runOne(7000);
  await waitForCondition(
    () => timers.count(1000) === 1,
    "stale foreground link reconnect",
  );
  timers.runOne(1000);
  await waitForCondition(
    () => harness.server.requests.filter(
      (request) => request.type === "context-open",
    ).length === 2,
    "fresh context-open after foreground probe timeout",
  );
  assert.equal(
    harness.server.requests.some((request) => request.type === "start"),
    false,
  );
  assert.equal(harness.scenario.microphoneRequests.length, 0);

  contextSyncStorage.set("eph-ctx-sync", "0");
  await harness.api.contextSyncChanged();
});

test("原生 12 秒巡检必须证明当前页写入而不能只接受可读旧缓存", async () => {
  const timers = createManualTimers();
  const contextSyncStorage = new Map([["eph-ctx-sync", "1"]]);
  const harness = createHarness({
    origin: NATIVE_APP_ORIGIN,
    nativeComputerVoice: true,
    nativeReaderForeground: true,
    contextSyncStorage,
    contextDeliveryMode: "snapshot-mcp",
    activeReading: {
      kind: "pdf",
      file: "localbook:watchdog-current-page",
      title: "Watchdog Current Page",
      pos: 19,
      selection: "",
    },
    dropSecondActiveReadingOnFirstSocket: true,
    timers,
  });

  await waitForRequest(harness, "context-open");
  await waitForCondition(
    () => harness.scenario.activeReadingRequests.length === 1,
    "initial active-reading publish",
  );
  harness.dispatchWindowEvent("bw-native-reader-foreground", {
    active: true,
    probe: true,
  });
  await waitForCondition(
    () => harness.scenario.activeReadingRequests.length === 2,
    "watchdog active-reading proof",
  );
  assert.equal(
    harness.server.requests.filter(
      (request) => request.type === "context-mode",
    ).length,
    2,
    "transport probe still precedes the current-page proof",
  );
  assert.equal(timers.count(7000), 1);
  timers.runOne(7000);
  await waitForCondition(
    () => timers.count(1000) === 1,
    "publication timeout reconnect",
  );
  timers.runOne(1000);
  await waitForCondition(
    () => harness.server.requests.filter(
      (request) => request.type === "context-open",
    ).length === 2,
    "fresh snapshot session after publication timeout",
  );
  assert.equal(
    harness.server.requests.some((request) => request.type === "start"),
    false,
  );
  assert.equal(harness.scenario.microphoneRequests.length, 0);

  contextSyncStorage.set("eph-ctx-sync", "0");
  await harness.api.contextSyncChanged();
});

test("原生 App 本地书把当前视口前后正文写进本地 page.context", async () => {
  const contextSyncStorage = new Map([["eph-ctx-sync", "1"]]);
  const harness = createHarness({
    origin: NATIVE_APP_ORIGIN,
    nativeComputerVoice: true,
    nativeLocalPageContext: true,
    contextSyncStorage,
    contextDeliveryMode: "snapshot-mcp",
    nativePageTexts: {
      6: "上一页结尾",
      7: "当前页完整文字，包含当前窗口重点内容。",
      8: "下一页开头",
    },
    readerAdapterContext: {
      visible_text: "当前窗口重点内容",
    },
    activeReading: {
      kind: "pdf",
      file: "localbook:app-snapshot",
      title: "Native App Snapshot",
      pos: 7,
      selection: "",
    },
  });

  await waitForRequest(harness, "context-open");
  await waitForCondition(
    () => harness.scenario.nativePageContextPublishes.length >= 1,
    "native App local page context",
  );
  const payload = harness.scenario.nativePageContextPublishes[0];
  assert.equal(payload.kind, "pdf");
  assert.equal(payload.file, "localbook:app-snapshot");
  assert.equal(payload.page, 7);
  assert.equal(payload.textSource, "app-local-visible-window");
  assert.match(payload.text, /【当前显示区域之前】[\s\S]*上一页结尾/);
  assert.match(payload.text, /【当前显示区域（重点）】[\s\S]*当前窗口重点内容/);
  assert.match(payload.text, /【当前显示区域之后】[\s\S]*下一页开头/);

  contextSyncStorage.set("eph-ctx-sync", "0");
  await harness.api.contextSyncChanged();
});

test("原生 App 未打开设置且未启动语音也会恢复开关并独立上报快照", async () => {
  const contextSyncStorage = new Map();
  const harness = createHarness({
    origin: NATIVE_APP_ORIGIN,
    nativeComputerVoice: true,
    contextSyncStorage,
    serverContextSyncEnabled: true,
    contextDeliveryMode: "snapshot-mcp",
    activeReading: {
      kind: "pdf",
      file: "localbook:startup-snapshot",
      title: "Startup Snapshot",
      pos: 11,
      selection: "startup selection",
    },
  });

  await waitForRequest(harness, "context-open");
  await waitForCondition(
    () => harness.scenario.journalCalls.length >= 1,
    "startup context journal pump",
  );
  await waitForCondition(
    () => harness.scenario.activeReadingRequests.length >= 1,
    "startup active-reading pump",
  );

  assert.equal(contextSyncStorage.get("eph-ctx-sync"), "1");
  assert.equal(harness.scenario.socketUrls[0], CONTEXT_ENDPOINT);
  assert.equal(
    harness.server.requests.some((request) => request.type === "start"),
    false,
  );
  assert.equal(harness.scenario.microphoneRequests.length, 0);
  assert.equal(
    harness.scenario.activeReadingRequests[0].active.page,
    11,
  );

  contextSyncStorage.set("eph-ctx-sync", "0");
  await harness.api.contextSyncChanged();
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

test("扩展后台不确认 close 时有界强制释放 relay 后才允许新连接", async () => {
  const timers = createManualTimers();
  const harness = createHarness({
    origin: "https://arbitrary.example",
    extensionRelay: true,
    relayCloseHangs: true,
    timers,
  });
  const refresh = harness.api.availability();
  await waitForRequest(harness, "status");
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(timers.count(1500), 1);
  timers.runOne(1500);
  const available = await refresh;
  assert.equal(available.state, "idle");
  assert.equal(
    harness.scenario.relayLifecycle.includes("port-disconnect"),
    true,
  );

  const started = await startWithTrustedGesture(harness);
  assert.equal(started.ok, true);
  assert.equal(harness.scenario.relayOverlapAttempts, 0);
  assert.equal(harness.scenario.relayPorts.length, 2);
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

test("旧 v2 或附加认证字段会 fail closed，且不会继续 STATUS", async () => {
  const harness = createHarness({
    helloPayload: {
      protocolVersion: 2,
      paired: true,
      limits: {
        maxMessageBytes: 65536,
        pcmFrameBytes: 1956,
        pcmQueueLimitMs: 400,
        heartbeatIntervalMs: 5000,
        heartbeatTimeoutMs: 15000,
        uplinkTrack: 3,
        uplinkQueueLimitMs: 200,
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
  assert.equal(harness.scenario.microphoneTracks.length, 1);
  assert.equal(harness.scenario.microphoneTracks[0].stopped, true);

  harness.scenario.startError = null;
  const started = await startWithTrustedGesture(harness);
  assert.equal(started.ok, true);
  assert.equal(harness.api.isActive(), true);
  assert.equal(
    harness.server.requests.filter((request) => request.type === "start").length,
    2,
  );
  await harness.api.stop("test");
  assert.equal(harness.scenario.microphoneTracks.length, 2);
  assert.equal(harness.scenario.microphoneTracks[1].stopped, true);
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

test("START 确认前麦克风不发帧，确认后只发 1956B BWCV track 3 会话连续 PCM", async () => {
  const harness = createHarness({ deferStartResult: true });
  const starting = startWithTrustedGesture(harness);
  const request = await waitForRequest(harness, "start");
  assert.equal(harness.scenario.microphoneRequests.length, 1);
  assert.equal(harness.scenario.scriptProcessors.length, 1);

  const samples = new Float32Array(960);
  const pattern = [-1, -0.5, 0, 0.5, 1];
  for (let index = 0; index < samples.length; index += 1) {
    samples[index] = pattern[index % pattern.length];
  }
  harness.scenario.scriptProcessors[0].emit(samples);
  assert.equal(
    harness.server.binaryFrames.length,
    0,
    "START result 前不得上行任何麦克风 PCM",
  );

  harness.server.resolveDeferredStart();
  const started = await starting;
  assert.equal(started.sessionId, request.sessionId);
  harness.scenario.scriptProcessors[0].emit(samples);
  assert.equal(harness.server.binaryFrames.length, 1);

  const frame = harness.server.binaryFrames[0];
  assert.equal(frame.byteLength, 1956);
  const view = new DataView(
    frame.buffer,
    frame.byteOffset,
    frame.byteLength,
  );
  assert.equal(Buffer.from(frame.subarray(0, 4)).toString("ascii"), "BWCV");
  assert.equal(view.getUint8(4), 1);
  assert.equal(view.getUint8(5), 3);
  assert.equal(view.getUint16(6, true), 0);
  assert.deepEqual(
    [...frame.subarray(8, 24)],
    [...decodeSessionBytes(started.sessionId)],
  );
  assert.equal(view.getUint32(24, true), 0);
  const timestamp =
    view.getUint32(28, true) +
    view.getUint32(32, true) * 0x100000000;
  assert.ok(timestamp > 0);
  assert.deepEqual(
    Array.from({ length: pattern.length }, (_, index) =>
      view.getInt16(36 + index * 2, true)
    ),
    [-32768, -16384, 0, 16384, 32767],
  );

  await harness.api.stop("test");
  assert.equal(harness.scenario.microphoneTracks.length, 1);
  assert.equal(harness.scenario.microphoneTracks[0].stopped, true);
  assert.equal(harness.scenario.scriptProcessors[0].disconnected, true);
});

test("扩展上行只有一个 credit，ACK 前丢实时帧且不递增 PCM sequence", async () => {
  const harness = createHarness({
    origin: "https://arbitrary.example",
    extensionRelay: true,
    relayAutoAck: false,
  });
  await startWithTrustedGesture(harness);
  const samples = new Float32Array(960);
  samples.fill(0.25);
  const processor = harness.scenario.scriptProcessors[0];

  processor.emit(samples);
  assert.equal(harness.server.binaryFrames.length, 1);
  assert.equal(harness.scenario.pendingRelayAcks.length, 1);
  assert.equal(
    new DataView(
      harness.server.binaryFrames[0].buffer,
      harness.server.binaryFrames[0].byteOffset,
      harness.server.binaryFrames[0].byteLength,
    ).getUint32(24, true),
    0,
  );

  processor.emit(samples);
  assert.equal(
    harness.server.binaryFrames.length,
    1,
    "ACK 前后续实时帧不得堆积到 extension Port/WSS",
  );
  const binaryMessagesBeforeAck =
    harness.scenario.relayClientMessages.filter(
      (message) => message.type === "send-binary-base64",
    );
  assert.equal(binaryMessagesBeforeAck.length, 1);

  const ack = harness.scenario.pendingRelayAcks.shift();
  ack.emit({
    type: "binary-accepted",
    sequence: ack.sequence,
    bytes: ack.bytes,
  });
  processor.emit(samples);
  assert.equal(harness.server.binaryFrames.length, 2);
  const secondFrame = harness.server.binaryFrames[1];
  assert.equal(
    new DataView(
      secondFrame.buffer,
      secondFrame.byteOffset,
      secondFrame.byteLength,
    ).getUint32(24, true),
    1,
    "被 credit 丢弃的帧不得消耗 header sequence",
  );
  assert.deepEqual(
    harness.scenario.relayClientMessages
      .filter((message) => message.type === "send-binary-base64")
      .map((message) => message.sequence),
    [0, 1],
  );
  await harness.api.stop("test");
});

test("扩展上行 ACK sequence 错配立即 fail closed 并释放麦克风", async () => {
  const harness = createHarness({
    origin: "https://arbitrary.example",
    extensionRelay: true,
    relayAutoAck: false,
  });
  await startWithTrustedGesture(harness);
  harness.scenario.scriptProcessors[0].emit(new Float32Array(960));
  const ack = harness.scenario.pendingRelayAcks.shift();
  ack.emit({
    type: "binary-accepted",
    sequence: ack.sequence + 1,
    bytes: ack.bytes,
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(harness.api.isActive(), false);
  assert.equal(harness.scenario.microphoneTracks[0].stopped, true);
  assert.equal(
    harness.scenario.relayLifecycle.includes("port-disconnect"),
    true,
  );
});

test("直连 WSS 缓冲恰好 10 帧可发送，第 11 帧 fail closed 并清麦克风", async () => {
  const harness = createHarness();
  await startWithTrustedGesture(harness);
  const samples = new Float32Array(960);
  const socket = harness.server.activeSocket;
  socket.bufferedAmount = 9 * 1956;
  harness.scenario.scriptProcessors[0].emit(samples);
  assert.equal(harness.server.binaryFrames.length, 1);
  assert.equal(harness.api.isActive(), true);

  socket.bufferedAmount = 10 * 1956;
  harness.scenario.scriptProcessors[0].emit(samples);
  assert.equal(harness.server.binaryFrames.length, 1);
  assert.equal(harness.api.isActive(), false);
  assert.equal(harness.scenario.microphoneTracks[0].stopped, true);
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

test("Reader outgoing journal 与 CONTEXT 在 START 成功前绝不启动", async (t) => {
  const harness = createHarness({ deferStartResult: true });
  t.after(() => harness.api.stop("cleanup"));
  harness.api.setSelectedEngine("computer_client");
  phoneClick(harness);
  assert.equal(harness.scenario.journalCalls.length, 0);
  assert.equal(contextRequests(harness).length, 0);

  const pending = harness.api.startFromUserGesture();
  await waitForRequest(harness, "start");
  assert.equal(harness.scenario.journalCalls.length, 0);
  assert.equal(contextRequests(harness).length, 0);
  harness.server.resolveDeferredStart();
  await pending;
  await waitForCondition(
    () => harness.scenario.journalCalls.length > 0,
    "bootstrap journal probe",
  );
  await harness.api.stop("test");
});

test("context bootstrap 只从最新 page.context 回放且不把 outer tail 当 ACK", async (t) => {
  const rows = Array.from({ length: 10 }, (_, index) => {
    const seq = index + 1;
    const type = seq === 4 || seq === 8 ? "page.context" : "focus";
    return journalEvent(seq, type, {
      payloadSentinel: { seq, untouched: true },
    });
  });
  const harness = createHarness({
    journalResponder(call) {
      if (call.wait === 20) return new Promise(() => {});
      if (call.limit === 1) {
        return Promise.resolve(jsonResponse(journalEnvelope({
          cursor: 10,
          head: 1,
          events: [rows[0]],
        })));
      }
      return Promise.resolve(jsonResponse(journalEnvelope({
        cursor: 10,
        head: 1,
        events: rows,
      })));
    },
  });
  t.after(() => harness.api.stop("cleanup"));
  const started = await startWithTrustedGesture(harness);
  await waitForCondition(
    () => contextRequests(harness).length === 3,
    "latest page context replay",
  );
  const sent = contextRequests(harness);
  assert.deepEqual(sent.map((request) => request.event.seq), [8, 9, 10]);
  assert.deepEqual(sent.map((request) => request.contextContract), [
    "reader-outgoing-context/1",
    "reader-outgoing-context/1",
    "reader-outgoing-context/1",
  ]);
  assert.ok(sent.every((request) => request.sessionId === started.sessionId));
  assert.deepEqual(sent[0].event, rows[7], "event payload must remain untouched");
  assert.equal(
    sent.some((request) => request.event === 10),
    false,
    "outer cursor/tail is never submitted as an event",
  );
  await waitForCondition(
    () => harness.scenario.journalCalls.some(
      (call) => call.wait === 20 && call.since === 10,
    ),
    "live poll at final per-event ACK",
  );
  await harness.api.stop("test");
});

test("bootstrap 扫描 bounded 500 条仍无 page.context 时只建立 resume baseline", async (t) => {
  const rows = Array.from(
    { length: 500 },
    (_, index) => journalEvent(index + 1, index % 2 ? "focus" : "drawing"),
  );
  const harness = createHarness({
    journalResponder(call) {
      if (call.limit === 1) {
        return Promise.resolve(jsonResponse(journalEnvelope({
          cursor: 500,
          head: 1,
          events: [rows[0]],
        })));
      }
      if (call.wait === 0) {
        return Promise.resolve(jsonResponse(journalEnvelope({
          cursor: 500,
          head: 1,
          events: rows,
        })));
      }
      return new Promise(() => {});
    },
  });
  t.after(() => harness.api.stop("cleanup"));
  await startWithTrustedGesture(harness);
  await waitForCondition(
    () => harness.scenario.journalCalls.some(
      (call) => call.wait === 20 && call.since === 500,
    ),
    "no-page resume baseline",
  );
  assert.equal(contextRequests(harness).length, 0);
  assert.deepEqual(
    harness.scenario.journalCalls.slice(0, 3).map(
      ({ since, limit, wait }) => ({ since, limit, wait }),
    ),
    [
      { since: 0, limit: 1, wait: 0 },
      { since: 0, limit: 500, wait: 0 },
      { since: 500, limit: 32, wait: 20 },
    ],
  );
  await harness.api.stop("test");
});

test("bootstrap 使用冻结 tail，不能被并发追加的 outer cursor 跳过", async (t) => {
  const oldRows = Array.from(
    { length: 500 },
    (_, index) => journalEvent(index + 1, index % 2 ? "focus" : "drawing"),
  );
  const appended = journalEvent(501, "page.context", {
    payloadSentinel: { appended: true },
  });
  const harness = createHarness({
    journalResponder(call) {
      if (call.limit === 1) {
        return Promise.resolve(jsonResponse(journalEnvelope({
          cursor: 500,
          head: 1,
          events: [oldRows[0]],
        })));
      }
      if (call.wait === 0) {
        return Promise.resolve(jsonResponse(journalEnvelope({
          cursor: 501,
          head: 1,
          events: oldRows,
        })));
      }
      if (call.since === 500) {
        return Promise.resolve(jsonResponse(journalEnvelope({
          cursor: 501,
          head: 1,
          events: [appended],
        })));
      }
      return new Promise(() => {});
    },
  });
  t.after(() => harness.api.stop("cleanup"));
  await startWithTrustedGesture(harness);
  await waitForCondition(
    () => contextRequests(harness).length === 1,
    "appended page.context must remain visible after frozen bootstrap",
  );
  assert.equal(contextRequests(harness)[0].event.seq, 501);
  assert.ok(
    harness.scenario.journalCalls.some(
      (call) => call.wait === 20 && call.since === 500,
    ),
  );
  await harness.api.stop("test");
});

test("context ACK 每条推进，截断 live batch 不会跳到 outer cursor", async (t) => {
  const first = Array.from(
    { length: 32 },
    (_, index) => journalEvent(index + 1),
  );
  const second = Array.from(
    { length: 8 },
    (_, index) => journalEvent(index + 33),
  );
  const harness = createHarness({
    journalResponder(call) {
      if (call.wait === 0) {
        return Promise.resolve(jsonResponse(journalEnvelope()));
      }
      if (call.since === 0) {
        return Promise.resolve(jsonResponse(journalEnvelope({
          cursor: 40,
          head: 1,
          events: first,
        })));
      }
      if (call.since === 32) {
        return Promise.resolve(jsonResponse(journalEnvelope({
          cursor: 40,
          head: 1,
          events: second,
        })));
      }
      return new Promise(() => {});
    },
  });
  t.after(() => harness.api.stop("cleanup"));
  await startWithTrustedGesture(harness);
  await waitForCondition(
    () => contextRequests(harness).length === 40,
    "all truncated batches",
  );
  assert.deepEqual(
    contextRequests(harness).map((request) => request.event.seq),
    Array.from({ length: 40 }, (_, index) => index + 1),
  );
  assert.ok(harness.scenario.journalCalls.some(
    (call) => call.wait === 20 && call.since === 32,
  ));
  await waitForCondition(
    () => harness.scenario.journalCalls.some(
      (call) => call.wait === 20 && call.since === 40,
    ),
    "final per-event ACK cursor",
  );
  await harness.api.stop("test");
});

test("retryable CONTEXT 失败保留同一 event，成功 ACK 后才推进", async (t) => {
  let attempts = 0;
  const event = journalEvent(1, "command", { command: "nav.goto" });
  const harness = createHarness({
    journalResponder(call) {
      if (call.wait === 0) {
        return Promise.resolve(jsonResponse(journalEnvelope()));
      }
      if (call.since === 0) {
        return Promise.resolve(jsonResponse(journalEnvelope({
          cursor: 1,
          head: 1,
          events: [event],
        })));
      }
      return new Promise(() => {});
    },
    contextResponder({ request, result, failure }) {
      attempts += 1;
      if (attempts === 1) {
        failure({
          code: "context_ipc_busy",
          message: "busy",
          retryable: true,
        });
        return;
      }
      result({
        sessionId: request.sessionId,
        eventId: request.event.id,
        seq: request.event.seq,
        outcome: "duplicate",
      });
    },
  });
  t.after(() => harness.api.stop("cleanup"));
  await startWithTrustedGesture(harness);
  await waitForCondition(
    () => contextRequests(harness).length === 2,
    "context retry",
  );
  assert.deepEqual(
    contextRequests(harness).map((request) => request.event),
    [event, event],
  );
  await waitForCondition(
    () => harness.scenario.journalCalls.some(
      (call) => call.wait === 20 && call.since === 1,
    ),
    "cursor after duplicate ACK",
  );
  await harness.api.stop("test");
});

test("STOP 立即 abort journal，迟到 response 不能复活旧 generation", async (t) => {
  let lateResolve;
  const statuses = [];
  const harness = createHarness({
    journalResponder(call) {
      if (call.wait === 0) {
        return Promise.resolve(jsonResponse(journalEnvelope()));
      }
      return new Promise((resolve) => {
        lateResolve = resolve;
        call.options.signal.addEventListener("abort", () => {
          harness.scenario.abortedJournalFetches += 1;
        }, { once: true });
      });
    },
  });
  t.after(() => harness.api.stop("cleanup"));
  harness.api.onStatus((value) => statuses.push(value));
  await startWithTrustedGesture(harness);
  await waitForCondition(() => typeof lateResolve === "function", "live journal");
  await harness.api.stop("test");
  assert.equal(harness.scenario.abortedJournalFetches, 1);
  lateResolve(jsonResponse(journalEnvelope({
    cursor: 1,
    head: 1,
    events: [journalEvent(1, "page.context")],
  })));
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(contextRequests(harness).length, 0);
  assert.equal(harness.api.isActive(), false);
  assert.equal(
    statuses.some((value) => value.state === "warning"),
    false,
    "intentional abort is silent",
  );
});

test("journal transport TypeError 可重试且不停止音频 generation", async (t) => {
  let calls = 0;
  const statuses = [];
  const harness = createHarness({
    journalResponder(call) {
      calls += 1;
      if (calls === 1) return Promise.reject(new TypeError("network down"));
      if (call.wait === 0) {
        return Promise.resolve(jsonResponse(journalEnvelope()));
      }
      return new Promise(() => {});
    },
  });
  t.after(() => harness.api.stop("cleanup"));
  harness.api.onStatus((value) => statuses.push(value));
  await startWithTrustedGesture(harness);
  await waitForCondition(() => calls >= 3, "journal transport retry");
  assert.equal(harness.api.isActive(), true);
  assert.equal(
    statuses.some((value) => value.state === "warning"),
    false,
  );
  await harness.api.stop("test");
});

test("context schema 故障只停上下文，PCM 音频仍继续", async (t) => {
  const statuses = [];
  const harness = createHarness({
    journalResponder(call) {
      if (call.wait === 0) {
        return Promise.resolve(jsonResponse(journalEnvelope()));
      }
      const malformed = journalEnvelope({
        cursor: 1,
        head: 1,
        events: [journalEvent(1)],
      });
      delete malformed.note;
      return Promise.resolve(jsonResponse(malformed));
    },
  });
  t.after(() => harness.api.stop("cleanup"));
  harness.api.onStatus((value) => statuses.push(value));
  const started = await startWithTrustedGesture(harness);
  await waitForCondition(
    () => statuses.some((value) => value.state === "warning"),
    "context warning",
  );
  assert.equal(harness.api.isActive(), true);
  assert.equal(
    harness.server.requests.some((request) => request.type === "stop"),
    false,
  );
  harness.server.activeSocket.emitBinary(pcmFrame(started.sessionId));
  assert.equal(harness.scenario.audioContexts.at(-1).started.length, 1);
  await harness.api.stop("test");
});

test("malformed event 与 steady-state gap 都 fail closed context，不挂音频", async (t) => {
  const cases = [
    {
      name: "malformed event",
      body: journalEnvelope({
        cursor: 1,
        head: 1,
        events: [{ ...journalEvent(1), id: "NOT-LOWER-HEX" }],
      }),
      code: "BW_COMPUTER_VOICE_CONTEXT_SCHEMA",
    },
    {
      name: "steady gap",
      body: journalEnvelope({
        cursor: 9,
        head: 9,
        events: [journalEvent(9)],
        gap: true,
        note: "truncated",
      }),
      code: "BW_COMPUTER_VOICE_CONTEXT_GAP",
    },
  ];
  for (const item of cases) {
    await t.test(item.name, async () => {
      const statuses = [];
      const harness = createHarness({
        journalResponder(call) {
          if (call.wait === 0) {
            return Promise.resolve(jsonResponse(journalEnvelope()));
          }
          return Promise.resolve(jsonResponse(item.body));
        },
      });
      t.after(() => harness.api.stop("cleanup"));
      harness.api.onStatus((value) => statuses.push(value));
      await startWithTrustedGesture(harness);
      await waitForCondition(
        () => statuses.some((value) => value.state === "warning"),
        `${item.name} warning`,
      );
      assert.equal(harness.api.isActive(), true);
      assert.equal(contextRequests(harness).length, 0);
      const warning = statuses.find((value) => value.state === "warning");
      assert.equal(warning.code, item.code);
      await harness.api.stop("test");
    });
  }
});
