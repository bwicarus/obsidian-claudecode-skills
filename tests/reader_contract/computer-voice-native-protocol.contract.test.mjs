import assert from "node:assert/strict";
import test from "node:test";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const Native = require(
  "../../extensions/bw-reader-webext/src/computer-voice-native-protocol.js",
);

const FORMAT = Object.freeze({
  sampleRate: 48_000,
  channels: 1,
  sampleFormat: "s16le",
  frameDurationMs: 20,
  framesPerChunk: 960,
});

function capabilities(overrides = {}) {
  return {
    contract: Native.CONTRACT,
    type: "capabilities",
    nativeHostReady: true,
    captureScope: "process-only",
    loopbackMode: "include-target-process-tree",
    systemOutputFallback: false,
    microphoneSelection: "explicit-device-only",
    transport: "native-messaging-local",
    mediaDestination: "extension-offscreen-only",
    tracks: ["app-output", "user-mic"],
    format: FORMAT,
    maxInFlightChunks: 12,
    localOptIn: true,
    shortcutConfigured: true,
    app: {
      ready: true,
      target: {
        appId: "openai-codex-desktop",
        executable: "ChatGPT.exe",
        rootProcessId: 31_180,
      },
    },
    microphone: {
      available: true,
      selection: "explicit-device-only",
      deviceId: "headset-microphone-1",
    },
    companion: {
      kind: "voice-typist",
      launcherAvailable: true,
    },
    ...overrides,
  };
}

function start(overrides = {}) {
  return {
    contract: Native.CONTRACT,
    type: "start",
    requestId: "request-1",
    sessionId: "session-1",
    target: {
      appId: "openai-codex-desktop",
      executable: "ChatGPT.exe",
      rootProcessId: 31_180,
    },
    captureScope: "process-only",
    loopbackMode: "include-target-process-tree",
    microphone: {
      selection: "explicit-device-only",
      deviceId: "headset-microphone-1",
    },
    tracks: ["app-output", "user-mic"],
    format: FORMAT,
    transport: "native-messaging-local",
    mediaDestination: "extension-offscreen-only",
    authorization: {
      localOptIn: true,
      oneTimeTrigger: true,
      paired: true,
      nativeHostReady: true,
    },
    ...overrides,
  };
}

function pcm(trackId, sequence, overrides = {}) {
  return {
    contract: Native.CONTRACT,
    type: "pcm",
    sessionId: "session-1",
    trackId,
    sequence,
    timestampUs: 1_000_000 + sequence * 20_000,
    format: FORMAT,
    mediaDestination: "extension-offscreen-only",
    dataBase64: Buffer.alloc(Native.BYTES_PER_CHUNK, sequence % 255)
      .toString("base64"),
    ...overrides,
  };
}

function stop(overrides = {}) {
  return {
    contract: Native.CONTRACT,
    type: "stop",
    requestId: "request-stop-1",
    sessionId: "session-1",
    reason: "user-stopped",
    ...overrides,
  };
}

function ready(controller) {
  controller.setReadiness({
    localOptIn: true,
    paired: true,
    nativeHostReady: true,
  });
  controller.armOneTimeTrigger();
}

test("合同只允许 hello/capabilities/start/stop/pcm/stats/error 且拒绝未知字段", () => {
  const hello = Native.validateMessage({
    contract: Native.CONTRACT,
    type: "hello",
    role: "extension",
    instanceId: "extension-instance-1",
    protocolVersion: 1,
  });
  assert.equal(hello.type, "hello");
  hello.role = "tampered";
  assert.equal(
    Native.validateMessage({
      contract: Native.CONTRACT,
      type: "hello",
      role: "extension",
      instanceId: "extension-instance-1",
      protocolVersion: 1,
    }).role,
    "extension",
  );
  assert.throws(
    () => Native.validateMessage({
      contract: Native.CONTRACT,
      type: "audio",
    }),
    (error) => error.code === "BW_COMPUTER_VOICE_NATIVE_CONTRACT",
  );
  assert.throws(
    () => Native.validateMessage({
      contract: Native.CONTRACT,
      type: "hello",
      role: "extension",
      instanceId: "extension-instance-1",
      protocolVersion: 1,
      serverUrl: "https://pi.invalid/upload",
    }),
    (error) => error.code === "BW_COMPUTER_VOICE_NATIVE_INVALID",
  );
  assert.throws(
    () => Native.validateMessage({
      contract: "reader-computer-voice-native/2",
      type: "hello",
      role: "extension",
      instanceId: "extension-instance-1",
      protocolVersion: 1,
    }),
    (error) => error.code === "BW_COMPUTER_VOICE_NATIVE_CONTRACT",
  );
});

test("能力合同固定为目标进程树、显式麦克风和本机 offscreen 目的地", () => {
  assert.deepEqual(
    Native.validateMessage(capabilities()).tracks,
    ["app-output", "user-mic"],
  );
  for (const invalid of [
    capabilities({ captureScope: "system-wide" }),
    capabilities({ captureScope: "default-output" }),
    capabilities({ loopbackMode: "all-processes" }),
    capabilities({ systemOutputFallback: true }),
    capabilities({ microphoneSelection: "default-device" }),
    capabilities({ mediaDestination: "pi" }),
    capabilities({ transport: "websocket" }),
    capabilities({ tracks: ["app-output"] }),
    capabilities({ app: { ready: true, target: null } }),
    capabilities({
      microphone: {
        available: false,
        selection: "explicit-device-only",
        deviceId: "hidden-default",
      },
    }),
    capabilities({
      companion: {
        kind: "system-audio",
        launcherAvailable: true,
      },
    }),
  ]) {
    assert.throws(
      () => Native.validateMessage(invalid),
      (error) => error.code === "BW_COMPUTER_VOICE_NATIVE_SCOPE",
    );
  }
});

test("start 严格绑定 Codex 目标进程、显式耳麦、本机传输和四项授权证明", () => {
  assert.equal(
    Native.validateMessage(start()).target.executable,
    "ChatGPT.exe",
  );
  assert.throws(
    () => Native.validateMessage(start({
      target: {
        appId: "any-process",
        executable: "browser.exe",
        rootProcessId: 42,
      },
    })),
    (error) => error.code === "BW_COMPUTER_VOICE_NATIVE_SCOPE",
  );
  assert.throws(
    () => Native.validateMessage(start({
      microphone: {
        selection: "default-device",
        deviceId: "default",
      },
    })),
    (error) => error.code === "BW_COMPUTER_VOICE_NATIVE_SCOPE",
  );
  assert.throws(
    () => Native.validateMessage(start({
      authorization: {
        localOptIn: true,
        oneTimeTrigger: false,
        paired: true,
        nativeHostReady: true,
      },
    })),
    (error) => error.code === "BW_COMPUTER_VOICE_NATIVE_INVALID",
  );
  assert.throws(
    () => Native.validateMessage(start({ mediaDestination: "pi-relay" })),
    (error) => error.code === "BW_COMPUTER_VOICE_NATIVE_SCOPE",
  );
});

test("PCM 只接受双轨 48k mono s16le、20ms/960 frames 和恰好 1920 bytes", () => {
  assert.equal(
    Native.validateMessage(pcm("app-output", 0)).dataBase64.length,
    2560,
  );
  assert.equal(
    Native.validateMessage(pcm("user-mic", 0)).trackId,
    "user-mic",
  );
  for (const invalid of [
    pcm("system-output", 0),
    pcm("app-output", 0, {
      format: { ...FORMAT, sampleRate: 44_100 },
    }),
    pcm("app-output", 0, {
      format: { ...FORMAT, channels: 2 },
    }),
    pcm("app-output", 0, {
      format: { ...FORMAT, sampleFormat: "f32le" },
    }),
    pcm("app-output", 0, {
      dataBase64: Buffer.alloc(1918).toString("base64"),
    }),
    pcm("app-output", 0, {
      dataBase64: "AB==",
    }),
    pcm("app-output", 0, {
      mediaDestination: "https://pi.invalid/audio",
    }),
  ]) {
    assert.throws(() => Native.validateMessage(invalid));
  }
});

test("控制器默认 disabled，消息自报授权不能代替可信本机门禁", () => {
  const controller = Native.createPcmCreditController();
  assert.equal(controller.status().state, "disabled");
  assert.throws(
    () => controller.start(start()),
    (error) => error.code === "BW_COMPUTER_VOICE_NATIVE_NOT_READY",
  );

  controller.setReadiness({
    localOptIn: true,
    paired: false,
    nativeHostReady: true,
  });
  assert.equal(controller.status().state, "blocked");
  assert.throws(
    () => controller.armOneTimeTrigger(),
    (error) => error.code === "BW_COMPUTER_VOICE_NATIVE_NOT_READY",
  );
});

test("四项门禁齐全且一次性用户触发后才可 start，触发随启动被消费", () => {
  const controller = Native.createPcmCreditController();
  controller.setReadiness({
    localOptIn: true,
    paired: true,
    nativeHostReady: true,
  });
  assert.equal(controller.status().state, "ready");
  assert.throws(
    () => controller.start(start()),
    (error) => error.code === "BW_COMPUTER_VOICE_NATIVE_NOT_READY",
  );

  controller.armOneTimeTrigger();
  assert.equal(controller.status().state, "armed");
  const active = controller.start(start());
  assert.equal(active.state, "active");
  assert.equal(active.oneTimeTrigger, false);
  assert.equal(active.captureActive, true);
  assert.equal(active.sessionId, "session-1");
});

test("每轨 sequence 从 0 严格递增，出错会清空队列并 fail closed", () => {
  const controller = Native.createPcmCreditController();
  ready(controller);
  controller.start(start());
  controller.acceptPcm(pcm("app-output", 0));
  controller.acceptPcm(pcm("user-mic", 0));
  assert.equal(controller.status().totalQueuedChunks, 2);

  assert.throws(
    () => controller.acceptPcm(pcm("app-output", 2)),
    (error) => error.code === "BW_COMPUTER_VOICE_NATIVE_SEQUENCE",
  );
  const failed = controller.status();
  assert.equal(failed.state, "error");
  assert.equal(failed.captureActive, false);
  assert.equal(failed.totalQueuedChunks, 0);
  assert.equal(failed.oneTimeTrigger, false);
  assert.throws(
    () => controller.acceptPcm(pcm("app-output", 0)),
    (error) => error.code === "BW_COMPUTER_VOICE_NATIVE_INACTIVE",
  );
});

test("credit/队列按轨和总量双重有界，dequeue 才归还 credit", () => {
  const controller = Native.createPcmCreditController({
    maxQueuedChunksPerTrack: 2,
    maxQueuedChunksTotal: 3,
  });
  ready(controller);
  controller.start(start());
  controller.acceptPcm(pcm("app-output", 0));
  controller.acceptPcm(pcm("app-output", 1));
  let status = controller.status();
  assert.equal(status.credits["app-output"], 0);
  assert.equal(status.credits["user-mic"], 1);

  const drained = controller.dequeue("app-output");
  assert.equal(drained.sequence, 0);
  status = controller.status();
  assert.equal(status.credits["app-output"], 1);
  controller.acceptPcm(pcm("app-output", 2));
  controller.acceptPcm(pcm("user-mic", 0));
  assert.equal(controller.status().totalQueuedChunks, 3);
  assert.equal(controller.status().credits["app-output"], 0);
  assert.equal(controller.status().credits["user-mic"], 0);

  assert.throws(
    () => controller.acceptPcm(pcm("user-mic", 1)),
    (error) => error.code === "BW_COMPUTER_VOICE_NATIVE_BACKPRESSURE",
  );
  assert.equal(controller.status().totalQueuedChunks, 0);
  assert.equal(controller.status().droppedChunks, 0);
});

test("stop、宿主断连和授权撤回均清零且不允许自动重启", () => {
  const stopped = Native.createPcmCreditController();
  ready(stopped);
  stopped.start(start());
  stopped.acceptPcm(pcm("app-output", 0));
  stopped.stop(stop());
  assert.equal(stopped.status().totalQueuedChunks, 0);
  assert.equal(stopped.status().captureActive, false);
  assert.equal(stopped.status().oneTimeTrigger, false);
  assert.throws(
    () => stopped.start(start()),
    (error) => error.code === "BW_COMPUTER_VOICE_NATIVE_NOT_READY",
  );

  const disconnected = Native.createPcmCreditController();
  ready(disconnected);
  disconnected.start(start());
  disconnected.acceptPcm(pcm("user-mic", 0));
  disconnected.disconnect();
  assert.equal(disconnected.status().nativeHostReady, false);
  assert.equal(disconnected.status().totalQueuedChunks, 0);
  assert.equal(disconnected.status().state, "blocked");

  const revoked = Native.createPcmCreditController();
  ready(revoked);
  revoked.start(start());
  revoked.acceptPcm(pcm("app-output", 0));
  revoked.setReadiness({
    localOptIn: false,
    paired: true,
    nativeHostReady: true,
  });
  assert.equal(revoked.status().state, "disabled");
  assert.equal(revoked.status().totalQueuedChunks, 0);
});

test("stats 只报告有界 credit/队列且 droppedChunks 永远为 0", () => {
  const controller = Native.createPcmCreditController({
    maxQueuedChunksPerTrack: 3,
    maxQueuedChunksTotal: 4,
  });
  ready(controller);
  controller.start(start());
  controller.acceptPcm(pcm("app-output", 0));
  const stats = controller.statsMessage();
  assert.equal(stats.contract, "reader-computer-voice-native/1");
  assert.equal(stats.type, "stats");
  assert.equal(stats.captureActive, true);
  assert.equal(stats.queuedChunks["app-output"], 1);
  assert.equal(stats.queuedChunks["user-mic"], 0);
  assert.equal(stats.droppedChunks, 0);
  assert.deepEqual(
    Native.validateMessage(stats),
    stats,
  );
  assert.throws(
    () => Native.validateMessage({ ...stats, droppedChunks: 1 }),
    (error) => error.code === "BW_COMPUTER_VOICE_NATIVE_BACKPRESSURE",
  );
});

test("原生 error 终止会话并清空 PCM，迟到或跨会话 error 被拒绝", () => {
  const controller = Native.createPcmCreditController();
  ready(controller);
  controller.start(start());
  controller.acceptPcm(pcm("app-output", 0));
  assert.throws(
    () => controller.receiveError({
      contract: Native.CONTRACT,
      type: "error",
      sessionId: "different-session",
      code: "BW_NATIVE_FAILED",
      message: "different session",
      retryable: false,
    }),
    (error) => error.code === "BW_COMPUTER_VOICE_NATIVE_INACTIVE",
  );
  assert.equal(controller.status().captureActive, true);

  assert.throws(
    () => controller.receiveError({
      contract: Native.CONTRACT,
      type: "error",
      sessionId: "session-1",
      code: "BW_NATIVE_FAILED",
      message: "capture failed",
      retryable: false,
    }),
    (error) => error.code === "BW_NATIVE_FAILED",
  );
  assert.equal(controller.status().state, "error");
  assert.equal(controller.status().totalQueuedChunks, 0);
});
