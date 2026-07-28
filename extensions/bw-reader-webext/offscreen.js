/* offscreen.js — Windows 主动出站的电脑客户端媒体桥。
 *
 * PCM 只在 Native Messaging → 本扩展 offscreen → WebRTC TrackGenerator
 * 之间流动；Pi 只收配对、心跳、一次性命令与 SDP/ICE。没有默认设备、
 * 系统输出、网页可见凭据或选择模型即启动的兼容回退。
 */
(() => {
  "use strict";

  const Native = globalThis.BWComputerVoiceNativeProtocol;
  const WebRtc = globalThis.BWComputerVoiceWebRtc;
  const ORIGIN = "https://bwicarus.taile44d0c.ts.net";
  const NATIVE_HOST = "space.bwicarus.computer_voice";
  const STORE_KEY = "bwComputerVoiceDeviceV1";
  const PAIRING_CONTRACT = "reader-computer-voice-pairing/1";
  const BRIDGE_CONTRACT = "reader-computer-voice-bridge/1";
  const SIGNAL_CONTRACT = "reader-computer-voice-signal/1";
  const DEVICE_CONTRACT = "reader-computer-voice-device/1";
  const OFFSCREEN_REQUEST = "BW_COMPUTER_VOICE_OFFSCREEN_REQUEST";
  const SAFE_ID = /^[A-Za-z0-9._:-]{1,160}$/;
  const PAIRING_CODE = /^[A-HJ-NP-Z2-9]{8,20}$/;

  let device = null;
  let nativePort = null;
  let nativeCapabilities = null;
  let nativeReconnectAfter = 0;
  let tickRunning = false;
  let tickTimer = null;
  let session = null;
  let signalSequence = 0;

  function fail(message, code) {
    throw Object.assign(new Error(message), {
      code: code || "BW_COMPUTER_VOICE_OFFSCREEN",
    });
  }

  function randomToken(bytes) {
    const data = new Uint8Array(bytes);
    crypto.getRandomValues(data);
    let binary = "";
    for (const value of data) binary += String.fromCharCode(value);
    return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function checkedDevice(value) {
    if (
      !value ||
      value.contract !== DEVICE_CONTRACT ||
      !SAFE_ID.test(String(value.deviceId || "")) ||
      !/^[A-Za-z0-9._~-]{32,512}$/.test(String(value.deviceToken || ""))
    ) {
      return null;
    }
    return {
      contract: DEVICE_CONTRACT,
      deviceId: String(value.deviceId),
      deviceToken: String(value.deviceToken),
      pairedAt: Number(value.pairedAt) || 0,
    };
  }

  async function loadDevice() {
    const stored = await chrome.storage.local.get(STORE_KEY);
    device = checkedDevice(stored[STORE_KEY]);
    return device;
  }

  function deviceHeaders() {
    if (!device) fail("电脑客户端尚未配对", "BW_COMPUTER_VOICE_PAIRING_REQUIRED");
    return {
      "Accept": "application/json",
      "Content-Type": "application/json",
      "Authorization": `BWComputerVoice ${device.deviceToken}`,
      "X-BW-Computer-Voice-Device-Id": device.deviceId,
    };
  }

  async function requestJson(path, body, authenticated = true) {
    const response = await fetch(ORIGIN + path, {
      method: "POST",
      credentials: "omit",
      cache: "no-store",
      headers: authenticated ? deviceHeaders() : {
        "Accept": "application/json",
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
    });
    let value = {};
    try { value = await response.json(); } catch (_) {}
    if (!response.ok || value?.ok !== true) {
      fail(
        String(value?.error || value?.code || `电脑客户端请求失败(${response.status})`),
        String(value?.code || "BW_COMPUTER_VOICE_HTTP"),
      );
    }
    return value;
  }

  function mediaApisReady() {
    return typeof RTCPeerConnection === "function" &&
      typeof MediaStreamTrackGenerator === "function" &&
      typeof AudioData === "function";
  }

  function nativeReady() {
    const value = nativeCapabilities;
    return !!(
      value &&
      value.nativeHostReady === true &&
      value.localOptIn === true &&
      value.shortcutConfigured === true &&
      value.app?.ready === true &&
      value.app?.target &&
      value.microphone?.available === true &&
      value.microphone?.deviceId &&
      value.companion?.launcherAvailable === true
    );
  }

  function connectNative() {
    if (nativePort || Date.now() < nativeReconnectAfter) return;
    nativeReconnectAfter = Date.now() + 2_000;
    try {
      const port = chrome.runtime.connectNative(NATIVE_HOST);
      nativePort = port;
      port.onMessage.addListener((message) => {
        try { onNativeMessage(message); } catch (error) {
          void failSession(error, "failed");
        }
      });
      port.onDisconnect.addListener(() => {
        if (nativePort !== port) return;
        nativePort = null;
        nativeCapabilities = null;
        nativeReconnectAfter = Date.now() + 2_000;
        if (session) void failSession(
          Object.assign(new Error("Windows 原生宿主已断开"), {
            code: "BW_COMPUTER_VOICE_NATIVE_DISCONNECTED",
          }),
          "failed",
        );
      });
      port.postMessage({
        contract: Native.CONTRACT,
        type: "hello",
        role: "extension",
        instanceId: `extension-${chrome.runtime.id}`,
        protocolVersion: 1,
      });
    } catch (_) {
      nativePort = null;
    }
  }

  function onNativeMessage(raw) {
    const message = Native.validateMessage(raw);
    if (message.type === "capabilities") {
      nativeCapabilities = message;
      return;
    }
    if (message.type === "hello") return;
    if (!session) return;
    if (message.type === "pcm") {
      session.pcm.acceptPcm(message);
      void drainTrack(message.trackId);
      return;
    }
    if (message.type === "stats") {
      if (message.sessionId !== session.sessionId) return;
      session.nativeActive = message.captureActive === true;
      if (session.nativeActive) session.nativeActiveResolve?.();
      return;
    }
    if (message.type === "error") {
      try { session.pcm.receiveError(message); } catch (error) {
        void failSession(error, "failed");
      }
    }
  }

  async function drainTrack(trackId) {
    const current = session;
    if (!current || current.draining[trackId]) return;
    current.draining[trackId] = true;
    try {
      while (session === current) {
        const pcm = current.pcm.dequeue(trackId);
        if (!pcm) break;
        const binary = atob(pcm.dataBase64);
        const bytes = new Uint8Array(binary.length);
        for (let index = 0; index < binary.length; index += 1) {
          bytes[index] = binary.charCodeAt(index);
        }
        const audio = new AudioData({
          format: "s16",
          sampleRate: Native.SAMPLE_RATE,
          numberOfFrames: Native.FRAMES_PER_CHUNK,
          numberOfChannels: Native.CHANNELS,
          timestamp: pcm.timestampUs,
          data: bytes,
        });
        try {
          await current.writers[trackId].write(audio);
        } finally {
          audio.close();
        }
      }
    } catch (error) {
      await failSession(error, "failed");
    } finally {
      current.draining[trackId] = false;
    }
  }

  function createLocalTracks() {
    if (!mediaApisReady()) {
      fail("当前 Chromium 不支持受限音频 TrackGenerator", "BW_COMPUTER_VOICE_MEDIA_UNAVAILABLE");
    }
    const output = new MediaStreamTrackGenerator({ kind: "audio" });
    const microphone = new MediaStreamTrackGenerator({ kind: "audio" });
    return {
      descriptors: [
        { trackId: Native.TRACK_APP_OUTPUT, track: output },
        { trackId: Native.TRACK_USER_MIC, track: microphone },
      ],
      writers: {
        [Native.TRACK_APP_OUTPUT]: output.writable.getWriter(),
        [Native.TRACK_USER_MIC]: microphone.writable.getWriter(),
      },
    };
  }

  function signalTransport(sessionId) {
    return Object.freeze({
      contract: SIGNAL_CONTRACT,
      exchange: (message) => requestJson(
        `/api/reader/computer-voice/device/sessions/${encodeURIComponent(sessionId)}/signals`,
        {
          contract: SIGNAL_CONTRACT,
          signals: message.signals,
          cursor: message.cursor,
        },
      ),
    });
  }

  async function waitForConnected(current) {
    if (current.rtcConnected) return;
    await Promise.race([
      current.connectedPromise,
      new Promise((_, reject) => setTimeout(
        () => reject(Object.assign(new Error("WebRTC 连接超时"), {
          code: "BW_COMPUTER_VOICE_WEBRTC_TIMEOUT",
        })),
        6_000,
      )),
    ]);
  }

  async function waitForNativeActive(current) {
    if (current.nativeActive) return;
    await Promise.race([
      current.nativeActivePromise,
      new Promise((_, reject) => setTimeout(
        () => reject(Object.assign(new Error("本机音频启动超时"), {
          code: "BW_COMPUTER_VOICE_NATIVE_TIMEOUT",
        })),
        3_500,
      )),
    ]);
  }

  function startMessage(current) {
    const capabilities = nativeCapabilities;
    return {
      contract: Native.CONTRACT,
      type: "start",
      requestId: current.command.commandId,
      sessionId: current.sessionId,
      target: capabilities.app.target,
      captureScope: Native.CAPTURE_SCOPE,
      loopbackMode: Native.LOOPBACK_MODE,
      microphone: {
        selection: "explicit-device-only",
        deviceId: capabilities.microphone.deviceId,
      },
      tracks: [Native.TRACK_APP_OUTPUT, Native.TRACK_USER_MIC],
      format: {
        sampleRate: Native.SAMPLE_RATE,
        channels: Native.CHANNELS,
        sampleFormat: Native.SAMPLE_FORMAT,
        frameDurationMs: Native.FRAME_DURATION_MS,
        framesPerChunk: Native.FRAMES_PER_CHUNK,
      },
      transport: Native.LOCAL_TRANSPORT,
      mediaDestination: Native.LOCAL_DESTINATION,
      authorization: {
        localOptIn: true,
        oneTimeTrigger: true,
        paired: true,
        nativeHostReady: true,
      },
    };
  }

  async function startClaimed(command, sessionId) {
    if (session || !nativeReady() || !mediaApisReady() || !nativePort) {
      return;
    }
    const local = createLocalTracks();
    let connectedResolve;
    let nativeActiveResolve;
    const current = {
      command,
      sessionId,
      controller: null,
      writers: local.writers,
      tracks: local.descriptors,
      pcm: Native.createPcmCreditController(),
      draining: {
        [Native.TRACK_APP_OUTPUT]: false,
        [Native.TRACK_USER_MIC]: false,
      },
      rtcConnected: false,
      nativeActive: false,
      connectedPromise: new Promise((resolve) => { connectedResolve = resolve; }),
      nativeActivePromise: new Promise((resolve) => { nativeActiveResolve = resolve; }),
      connectedResolve,
      nativeActiveResolve,
      stopping: false,
    };
    current.connectedResolve = connectedResolve;
    current.nativeActiveResolve = nativeActiveResolve;
    session = current;
    try {
      current.controller = WebRtc.createComputerVoiceWebRtcController({
        role: WebRtc.ROLE_WINDOWS_SENDER,
        sessionId,
        RTCPeerConnection,
        signalTransport: signalTransport(sessionId),
        clock: Date.now,
        setTimeout: globalThis.setTimeout.bind(globalThis),
        clearTimeout: globalThis.clearTimeout.bind(globalThis),
        signalIdFactory: () => `windows-${Date.now().toString(36)}-${(++signalSequence).toString(36)}`,
        localTracks: local.descriptors,
        releaseLocalTracks: () => {
          for (const entry of local.descriptors) {
            try { entry.track.stop(); } catch (_) {}
          }
        },
        onTrack: () => fail("Windows sender 不允许接收远端音轨"),
        onStatus: (value) => {
          if (session !== current) return;
          if (value.state === "connected") {
            current.rtcConnected = true;
            current.connectedResolve();
          } else if (value.state === "failed" || value.state === "stopped") {
            if (!current.stopping) {
              void failSession(
                Object.assign(new Error("WebRTC 已停止"), {
                  code: value.lastError?.code || "BW_COMPUTER_VOICE_WEBRTC_STOPPED",
                }),
                "failed",
              );
            }
          }
        },
      });
      await current.controller.start({
        paired: true,
        localOptIn: true,
        oneTimeTrigger: true,
        nativeReady: true,
      });
      await waitForConnected(current);
      current.pcm.setReadiness({
        localOptIn: true,
        paired: true,
        nativeHostReady: true,
      });
      current.pcm.armOneTimeTrigger();
      const message = startMessage(current);
      current.pcm.start(message);
      nativePort.postMessage(message);
      await waitForNativeActive(current);
      await heartbeat();
      await acknowledge(current, "started");
    } catch (error) {
      await failSession(error, "failed");
    }
  }

  async function acknowledge(current, result) {
    if (current.acknowledged) return;
    await requestJson(
      `/api/reader/computer-voice/device/commands/${encodeURIComponent(current.command.commandId)}/ack`,
      {
        contract: BRIDGE_CONTRACT,
        nonce: current.command.nonce,
        result,
      },
    );
    current.acknowledged = true;
  }

  async function closeSession(current, reason) {
    if (!current || current.stopping) return;
    current.stopping = true;
    if (nativePort && current.nativeActive) {
      try {
        nativePort.postMessage({
          contract: Native.CONTRACT,
          type: "stop",
          requestId: `stop-${Date.now().toString(36)}`,
          sessionId: current.sessionId,
          reason: String(reason || "stopped").slice(0, 160),
        });
      } catch (_) {}
    }
    try { await current.controller?.stop(String(reason || "stopped").slice(0, 160)); } catch (_) {}
    for (const writer of Object.values(current.writers)) {
      try { await writer.close(); } catch (_) {}
    }
    if (session === current) session = null;
  }

  async function failSession(error, result) {
    const current = session;
    if (!current) return;
    try { await acknowledge(current, result || "failed"); } catch (_) {}
    await closeSession(current, error?.code || "failed");
  }

  function heartbeatBody() {
    const capabilities = nativeCapabilities;
    const active = !!session?.nativeActive;
    return {
      contract: BRIDGE_CONTRACT,
      heartbeat: {
        app: {
          kind: "codex-desktop",
          ready: capabilities?.app?.ready === true,
        },
        voiceStart: {
          localOptIn: capabilities?.localOptIn === true,
          shortcutConfigured: capabilities?.shortcutConfigured === true,
        },
        capture: {
          microphoneAvailable: capabilities?.microphone?.available === true,
          outputScope: "process-only",
          outputTarget: "codex-desktop",
          active,
        },
        media: {
          nativeHostReady: capabilities?.nativeHostReady === true,
          mediaHostReady: mediaApisReady(),
          rtcConnected: !!session?.rtcConnected,
        },
        companion: {
          kind: "voice-typist",
          launcherAvailable: capabilities?.companion?.launcherAvailable === true,
          running: active,
        },
        bridgeVersion: "0.1.0",
      },
    };
  }

  async function heartbeat() {
    if (!device) return null;
    return requestJson(
      "/api/reader/computer-voice/device/heartbeat",
      heartbeatBody(),
    );
  }

  async function claim() {
    if (!device || session || !nativeReady() || !mediaApisReady()) return;
    const value = await requestJson(
      "/api/reader/computer-voice/device/commands/claim",
      { contract: BRIDGE_CONTRACT },
    );
    if (!value.command) return;
    if (
      value.command.contract !== BRIDGE_CONTRACT ||
      value.command.action !== "start-computer-voice" ||
      !SAFE_ID.test(String(value.command.commandId || "")) ||
      !SAFE_ID.test(String(value.sessionId || ""))
    ) {
      fail("服务端启动命令无效", "BW_COMPUTER_VOICE_COMMAND_INVALID");
    }
    await startClaimed(value.command, value.sessionId);
  }

  async function tick() {
    if (tickRunning || !device) return;
    tickRunning = true;
    try {
      connectNative();
      await heartbeat();
      await claim();
    } catch (_) {
      // Heartbeats are leases: silence makes the Reader fail closed offline.
    } finally {
      tickRunning = false;
    }
  }

  function ensureTickTimer() {
    if (tickTimer !== null) return;
    tickTimer = setInterval(() => { void tick(); }, 1_000);
  }

  async function pair(payload) {
    const pairId = String(payload?.pairId || "").trim();
    const pairingCode = String(payload?.pairingCode || "")
      .replace(/[\s-]+/g, "").toUpperCase();
    if (!SAFE_ID.test(pairId) || !PAIRING_CODE.test(pairingCode)) {
      fail("配对信息无效", "BW_COMPUTER_VOICE_PAIRING_INVALID");
    }
    if (device) fail("此扩展已配对电脑客户端", "BW_COMPUTER_VOICE_ALREADY_PAIRED");
    const next = {
      contract: DEVICE_CONTRACT,
      deviceId: `windows-${randomToken(18)}`,
      deviceToken: randomToken(48),
      pairedAt: Date.now(),
    };
    await requestJson(
      "/api/reader/computer-voice/pairings/consume",
      {
        contract: PAIRING_CONTRACT,
        pairId,
        pairingCode,
        deviceId: next.deviceId,
        deviceToken: next.deviceToken,
      },
      false,
    );
    await chrome.storage.local.set({ [STORE_KEY]: next });
    device = next;
    connectNative();
    ensureTickTimer();
    void tick();
    return publicStatus();
  }

  function publicStatus() {
    return {
      contract: "reader-computer-voice-offscreen/1",
      paired: !!device,
      deviceId: device?.deviceId || null,
      extensionId: chrome.runtime.id,
      nativeConnected: !!nativePort,
      localOptIn: nativeCapabilities?.localOptIn === true,
      appReady: nativeCapabilities?.app?.ready === true,
      microphoneAvailable: nativeCapabilities?.microphone?.available === true,
      mediaHostReady: mediaApisReady(),
      active: !!session?.nativeActive,
      state: session ? (session.nativeActive ? "active" : "starting") :
        (nativeReady() && mediaApisReady() ? "ready" : "not-ready"),
    };
  }

  async function dispatch(operation, payload) {
    if (!device) await loadDevice();
    if (operation === "PAIR") return pair(payload);
    if (operation === "STATUS") {
      connectNative();
      return publicStatus();
    }
    if (operation === "STOP") {
      await closeSession(session, "user-stopped");
      return publicStatus();
    }
    fail("不支持的电脑客户端操作", "BW_COMPUTER_VOICE_OPERATION");
  }

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (
      sender?.id !== chrome.runtime.id ||
      message?.type !== OFFSCREEN_REQUEST
    ) {
      return false;
    }
    dispatch(String(message.operation || ""), message.payload || {})
      .then((data) => sendResponse({ ok: true, data }))
      .catch((error) => sendResponse({
        ok: false,
        code: String(error?.code || "BW_COMPUTER_VOICE_OFFSCREEN"),
        error: String(error?.message || error || "电脑客户端操作失败"),
      }));
    return true;
  });

  void loadDevice().then(() => {
    if (!device) return;
    connectNative();
    ensureTickTimer();
    void tick();
  });
})();
