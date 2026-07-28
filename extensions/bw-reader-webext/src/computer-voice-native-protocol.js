/*
 * computer-voice-native-protocol.js
 *
 * Pure contract/state layer for the opt-in Windows computer-voice bridge.
 * This file deliberately has no Chrome, network, WebRTC, microphone or audio
 * capture calls. A later offscreen host may adapt this bounded state machine
 * to Chrome Native Messaging, but raw PCM must remain on the local
 * native-host -> extension-offscreen path.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.BWComputerVoiceNativeProtocol = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var CONTRACT = "reader-computer-voice-native/1";
  var TRACK_APP_OUTPUT = "app-output";
  var TRACK_USER_MIC = "user-mic";
  var TRACK_IDS = Object.freeze([TRACK_APP_OUTPUT, TRACK_USER_MIC]);
  var CAPTURE_SCOPE = "process-only";
  var LOOPBACK_MODE = "include-target-process-tree";
  var LOCAL_TRANSPORT = "native-messaging-local";
  var LOCAL_DESTINATION = "extension-offscreen-only";
  var SAMPLE_RATE = 48000;
  var CHANNELS = 1;
  var SAMPLE_FORMAT = "s16le";
  var FRAME_DURATION_MS = 20;
  var FRAMES_PER_CHUNK = 960;
  var BYTES_PER_CHUNK = FRAMES_PER_CHUNK * CHANNELS * 2;
  var MAX_WIRE_MESSAGE_BYTES = 8 * 1024;
  var DEFAULT_MAX_QUEUED_PER_TRACK = 8;
  var DEFAULT_MAX_QUEUED_TOTAL = 12;

  function NativeProtocolError(message, code, retryable, details) {
    this.name = "NativeProtocolError";
    this.message = String(message || "电脑客户端本地音频协议错误");
    this.code = String(code || "BW_COMPUTER_VOICE_NATIVE_INVALID");
    this.retryable = !!retryable;
    this.details = details || null;
    if (Error.captureStackTrace) Error.captureStackTrace(this, NativeProtocolError);
  }
  NativeProtocolError.prototype = Object.create(Error.prototype);
  NativeProtocolError.prototype.constructor = NativeProtocolError;

  function fail(message, code, retryable, details) {
    throw new NativeProtocolError(message, code, retryable, details);
  }

  function own(value, key) {
    return Object.prototype.hasOwnProperty.call(value, key);
  }

  function isPlainObject(value) {
    if (!value || Object.prototype.toString.call(value) !== "[object Object]") {
      return false;
    }
    var prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
  }

  function clone(value) {
    if (value == null) return value;
    try {
      return JSON.parse(JSON.stringify(value));
    } catch (_) {
      fail(
        "本地音频消息不能序列化",
        "BW_COMPUTER_VOICE_NATIVE_INVALID",
        false
      );
    }
  }

  function byteLength(text) {
    if (typeof TextEncoder !== "undefined") {
      return new TextEncoder().encode(text).byteLength;
    }
    return unescape(encodeURIComponent(text)).length;
  }

  function assertExactKeys(value, required, optional, label) {
    if (!isPlainObject(value)) {
      fail(
        label + " 必须是普通对象",
        "BW_COMPUTER_VOICE_NATIVE_INVALID",
        false
      );
    }
    var allowed = Object.create(null);
    required.forEach(function (key) { allowed[key] = true; });
    (optional || []).forEach(function (key) { allowed[key] = true; });
    required.forEach(function (key) {
      if (!own(value, key)) {
        fail(
          label + " 缺少字段：" + key,
          "BW_COMPUTER_VOICE_NATIVE_INVALID",
          false
        );
      }
    });
    Object.keys(value).forEach(function (key) {
      if (!allowed[key]) {
        fail(
          label + " 包含未知字段：" + key,
          "BW_COMPUTER_VOICE_NATIVE_INVALID",
          false
        );
      }
    });
    return value;
  }

  function boundedString(value, label, maximum, pattern) {
    if (
      typeof value !== "string" ||
      value.length === 0 ||
      value.length > maximum ||
      /[\u0000-\u001f\u007f]/.test(value) ||
      pattern && !pattern.test(value)
    ) {
      fail(
        label + " 无效",
        "BW_COMPUTER_VOICE_NATIVE_INVALID",
        false
      );
    }
    return value;
  }

  function boundedInteger(value, label, minimum, maximum) {
    if (
      typeof value !== "number" ||
      !Number.isSafeInteger(value) ||
      value < minimum ||
      value > maximum
    ) {
      fail(
        label + " 无效",
        "BW_COMPUTER_VOICE_NATIVE_INVALID",
        false
      );
    }
    return value;
  }

  function exactBoolean(value, expected, label) {
    if (typeof value !== "boolean" || expected != null && value !== expected) {
      fail(
        label + " 无效",
        "BW_COMPUTER_VOICE_NATIVE_INVALID",
        false
      );
    }
    return value;
  }

  function validateTrackId(value) {
    if (value !== TRACK_APP_OUTPUT && value !== TRACK_USER_MIC) {
      fail(
        "trackId 只能是 app-output 或 user-mic",
        "BW_COMPUTER_VOICE_NATIVE_SCOPE",
        false
      );
    }
    return value;
  }

  function validateTracks(value) {
    if (
      !Array.isArray(value) ||
      value.length !== 2 ||
      value[0] !== TRACK_APP_OUTPUT ||
      value[1] !== TRACK_USER_MIC
    ) {
      fail(
        "tracks 必须且只能按顺序包含 app-output 与 user-mic",
        "BW_COMPUTER_VOICE_NATIVE_SCOPE",
        false
      );
    }
    return TRACK_IDS.slice();
  }

  function validateFormat(value, label) {
    label = label || "format";
    assertExactKeys(value, [
      "sampleRate",
      "channels",
      "sampleFormat",
      "frameDurationMs",
      "framesPerChunk",
    ], [], label);
    if (
      value.sampleRate !== SAMPLE_RATE ||
      value.channels !== CHANNELS ||
      value.sampleFormat !== SAMPLE_FORMAT ||
      value.frameDurationMs !== FRAME_DURATION_MS ||
      value.framesPerChunk !== FRAMES_PER_CHUNK
    ) {
      fail(
        label + " 必须是 48kHz mono signed-16、20ms/960 frames",
        "BW_COMPUTER_VOICE_NATIVE_FORMAT",
        false
      );
    }
    return {
      sampleRate: SAMPLE_RATE,
      channels: CHANNELS,
      sampleFormat: SAMPLE_FORMAT,
      frameDurationMs: FRAME_DURATION_MS,
      framesPerChunk: FRAMES_PER_CHUNK,
    };
  }

  function validateContractAndType(message, type, required, optional) {
    assertExactKeys(
      message,
      ["contract", "type"].concat(required || []),
      optional || [],
      type + " 消息"
    );
    if (message.contract !== CONTRACT || message.type !== type) {
      fail(
        "本地音频合同或消息类型不匹配",
        "BW_COMPUTER_VOICE_NATIVE_CONTRACT",
        false
      );
    }
  }

  function decodeCanonicalBase64(text) {
    if (
      typeof text !== "string" ||
      text.length === 0 ||
      text.length % 4 !== 0 ||
      !/^[A-Za-z0-9+/]+={0,2}$/.test(text)
    ) {
      fail(
        "PCM base64 无效",
        "BW_COMPUTER_VOICE_NATIVE_PCM",
        false
      );
    }
    try {
      if (typeof Buffer !== "undefined") {
        var buffer = Buffer.from(text, "base64");
        if (buffer.toString("base64") !== text) throw new Error("non-canonical");
        return new Uint8Array(buffer);
      }
      if (typeof atob === "function") {
        var binary = atob(text);
        var bytes = new Uint8Array(binary.length);
        for (var index = 0; index < binary.length; index += 1) {
          bytes[index] = binary.charCodeAt(index);
        }
        var roundTrip = "";
        for (var offset = 0; offset < bytes.length; offset += 1) {
          roundTrip += String.fromCharCode(bytes[offset]);
        }
        if (typeof btoa !== "function" || btoa(roundTrip) !== text) {
          throw new Error("non-canonical");
        }
        return bytes;
      }
    } catch (_) {
      fail(
        "PCM base64 无效",
        "BW_COMPUTER_VOICE_NATIVE_PCM",
        false
      );
    }
    fail(
      "当前环境不支持验证 PCM base64",
      "BW_COMPUTER_VOICE_NATIVE_UNAVAILABLE",
      false
    );
  }

  function validateHello(message) {
    validateContractAndType(message, "hello", [
      "role",
      "instanceId",
      "protocolVersion",
    ]);
    if (message.role !== "extension" && message.role !== "native-host") {
      fail(
        "hello.role 无效",
        "BW_COMPUTER_VOICE_NATIVE_INVALID",
        false
      );
    }
    boundedString(message.instanceId, "instanceId", 128, /^[A-Za-z0-9._:-]+$/);
    if (message.protocolVersion !== 1) {
      fail(
        "protocolVersion 无效",
        "BW_COMPUTER_VOICE_NATIVE_CONTRACT",
        false
      );
    }
    return clone(message);
  }

  function validateCapabilities(message) {
    validateContractAndType(message, "capabilities", [
      "nativeHostReady",
      "captureScope",
      "loopbackMode",
      "systemOutputFallback",
      "microphoneSelection",
      "transport",
      "mediaDestination",
      "tracks",
      "format",
      "maxInFlightChunks",
      "localOptIn",
      "shortcutConfigured",
      "app",
      "microphone",
      "companion",
    ]);
    exactBoolean(message.nativeHostReady, null, "nativeHostReady");
    if (
      message.captureScope !== CAPTURE_SCOPE ||
      message.loopbackMode !== LOOPBACK_MODE ||
      message.systemOutputFallback !== false ||
      message.microphoneSelection !== "explicit-device-only" ||
      message.transport !== LOCAL_TRANSPORT ||
      message.mediaDestination !== LOCAL_DESTINATION
    ) {
      fail(
        "capabilities 试图越过进程级/本机媒体边界",
        "BW_COMPUTER_VOICE_NATIVE_SCOPE",
        false
      );
    }
    validateTracks(message.tracks);
    validateFormat(message.format);
    boundedInteger(message.maxInFlightChunks, "maxInFlightChunks", 1, 64);
    exactBoolean(message.localOptIn, null, "localOptIn");
    exactBoolean(
      message.shortcutConfigured,
      null,
      "shortcutConfigured"
    );
    validateCapabilityApp(message.app);
    validateCapabilityMicrophone(message.microphone);
    validateCapabilityCompanion(message.companion);
    return clone(message);
  }

  function validateAuthorization(value) {
    assertExactKeys(value, [
      "localOptIn",
      "oneTimeTrigger",
      "paired",
      "nativeHostReady",
    ], [], "authorization");
    exactBoolean(value.localOptIn, true, "authorization.localOptIn");
    exactBoolean(value.oneTimeTrigger, true, "authorization.oneTimeTrigger");
    exactBoolean(value.paired, true, "authorization.paired");
    exactBoolean(value.nativeHostReady, true, "authorization.nativeHostReady");
    return clone(value);
  }

  function validateTarget(value) {
    assertExactKeys(value, [
      "appId",
      "executable",
      "rootProcessId",
    ], [], "target");
    if (
      value.appId !== "openai-codex-desktop" ||
      value.executable !== "ChatGPT.exe"
    ) {
      fail(
        "target 必须是已配置的 ChatGPT/Codex 桌面进程",
        "BW_COMPUTER_VOICE_NATIVE_SCOPE",
        false
      );
    }
    boundedInteger(value.rootProcessId, "target.rootProcessId", 1, 0xFFFFFFFF);
    return clone(value);
  }

  function validateMicrophone(value) {
    assertExactKeys(value, ["selection", "deviceId"], [], "microphone");
    if (value.selection !== "explicit-device-only") {
      fail(
        "麦克风必须由用户显式选择设备",
        "BW_COMPUTER_VOICE_NATIVE_SCOPE",
        false
      );
    }
    boundedString(value.deviceId, "microphone.deviceId", 512);
    return clone(value);
  }

  function validateCapabilityApp(value) {
    assertExactKeys(value, ["ready", "target"], [], "capabilities.app");
    exactBoolean(value.ready, null, "capabilities.app.ready");
    if (value.target === null) {
      if (value.ready) {
        fail(
          "应用就绪时必须给出已核验目标进程",
          "BW_COMPUTER_VOICE_NATIVE_SCOPE",
          false
        );
      }
      return clone(value);
    }
    validateTarget(value.target);
    if (!value.ready) {
      fail(
        "应用未就绪时不得携带目标进程",
        "BW_COMPUTER_VOICE_NATIVE_SCOPE",
        false
      );
    }
    return clone(value);
  }

  function validateCapabilityMicrophone(value) {
    assertExactKeys(
      value,
      ["available", "selection", "deviceId"],
      [],
      "capabilities.microphone"
    );
    exactBoolean(value.available, null, "capabilities.microphone.available");
    if (value.selection !== "explicit-device-only") {
      fail(
        "麦克风能力必须坚持显式设备选择",
        "BW_COMPUTER_VOICE_NATIVE_SCOPE",
        false
      );
    }
    if (value.deviceId === null) {
      if (value.available) {
        fail(
          "麦克风可用时必须带本机显式设备 ID",
          "BW_COMPUTER_VOICE_NATIVE_SCOPE",
          false
        );
      }
      return clone(value);
    }
    boundedString(value.deviceId, "capabilities.microphone.deviceId", 512);
    if (!value.available) {
      fail(
        "麦克风不可用时不得携带设备 ID",
        "BW_COMPUTER_VOICE_NATIVE_SCOPE",
        false
      );
    }
    return clone(value);
  }

  function validateCapabilityCompanion(value) {
    assertExactKeys(
      value,
      ["kind", "launcherAvailable"],
      [],
      "capabilities.companion"
    );
    if (value.kind !== "voice-typist") {
      fail(
        "电脑客户端 companion 必须是 voice-typist",
        "BW_COMPUTER_VOICE_NATIVE_SCOPE",
        false
      );
    }
    exactBoolean(
      value.launcherAvailable,
      null,
      "capabilities.companion.launcherAvailable"
    );
    return clone(value);
  }

  function validateStart(message) {
    validateContractAndType(message, "start", [
      "requestId",
      "sessionId",
      "target",
      "captureScope",
      "loopbackMode",
      "microphone",
      "tracks",
      "format",
      "transport",
      "mediaDestination",
      "authorization",
    ]);
    boundedString(message.requestId, "requestId", 128, /^[A-Za-z0-9._:-]+$/);
    boundedString(message.sessionId, "sessionId", 128, /^[A-Za-z0-9._:-]+$/);
    validateTarget(message.target);
    if (
      message.captureScope !== CAPTURE_SCOPE ||
      message.loopbackMode !== LOOPBACK_MODE ||
      message.transport !== LOCAL_TRANSPORT ||
      message.mediaDestination !== LOCAL_DESTINATION
    ) {
      fail(
        "start 试图越过进程级/本机媒体边界",
        "BW_COMPUTER_VOICE_NATIVE_SCOPE",
        false
      );
    }
    validateMicrophone(message.microphone);
    validateTracks(message.tracks);
    validateFormat(message.format);
    validateAuthorization(message.authorization);
    return clone(message);
  }

  function validateStop(message) {
    validateContractAndType(message, "stop", [
      "requestId",
      "sessionId",
      "reason",
    ]);
    boundedString(message.requestId, "requestId", 128, /^[A-Za-z0-9._:-]+$/);
    boundedString(message.sessionId, "sessionId", 128, /^[A-Za-z0-9._:-]+$/);
    boundedString(message.reason, "reason", 256);
    return clone(message);
  }

  function validatePcm(message) {
    validateContractAndType(message, "pcm", [
      "sessionId",
      "trackId",
      "sequence",
      "timestampUs",
      "format",
      "mediaDestination",
      "dataBase64",
    ]);
    boundedString(message.sessionId, "sessionId", 128, /^[A-Za-z0-9._:-]+$/);
    validateTrackId(message.trackId);
    boundedInteger(message.sequence, "sequence", 0, Number.MAX_SAFE_INTEGER);
    boundedInteger(message.timestampUs, "timestampUs", 0, Number.MAX_SAFE_INTEGER);
    validateFormat(message.format);
    if (message.mediaDestination !== LOCAL_DESTINATION) {
      fail(
        "PCM 只能交给扩展 offscreen 本地媒体管线",
        "BW_COMPUTER_VOICE_NATIVE_SCOPE",
        false
      );
    }
    var bytes = decodeCanonicalBase64(message.dataBase64);
    if (bytes.byteLength !== BYTES_PER_CHUNK) {
      fail(
        "PCM 必须恰好包含 20ms/960 frames 的 signed-16 mono 数据",
        "BW_COMPUTER_VOICE_NATIVE_PCM",
        false
      );
    }
    return clone(message);
  }

  function validateCredits(value, label) {
    assertExactKeys(value, TRACK_IDS, [], label);
    boundedInteger(value[TRACK_APP_OUTPUT], label + ".app-output", 0, 64);
    boundedInteger(value[TRACK_USER_MIC], label + ".user-mic", 0, 64);
    return clone(value);
  }

  function validateStats(message) {
    validateContractAndType(message, "stats", [
      "sessionId",
      "state",
      "nativeHostReady",
      "captureActive",
      "credits",
      "queuedChunks",
      "droppedChunks",
    ]);
    if (message.sessionId !== null) {
      boundedString(message.sessionId, "sessionId", 128, /^[A-Za-z0-9._:-]+$/);
    }
    if ([
      "disabled",
      "blocked",
      "ready",
      "armed",
      "active",
      "stopped",
      "error",
    ].indexOf(message.state) < 0) {
      fail(
        "stats.state 无效",
        "BW_COMPUTER_VOICE_NATIVE_INVALID",
        false
      );
    }
    exactBoolean(message.nativeHostReady, null, "nativeHostReady");
    exactBoolean(message.captureActive, null, "captureActive");
    if (
      message.captureActive !== (message.state === "active") ||
      message.captureActive && message.sessionId === null
    ) {
      fail(
        "stats 的 state/captureActive/sessionId 不一致",
        "BW_COMPUTER_VOICE_NATIVE_INVALID",
        false
      );
    }
    validateCredits(message.credits, "credits");
    validateCredits(message.queuedChunks, "queuedChunks");
    if (message.droppedChunks !== 0) {
      fail(
        "本地音频合同不允许静默丢帧",
        "BW_COMPUTER_VOICE_NATIVE_BACKPRESSURE",
        false
      );
    }
    return clone(message);
  }

  function validateError(message) {
    validateContractAndType(message, "error", [
      "sessionId",
      "code",
      "message",
      "retryable",
    ]);
    if (message.sessionId !== null) {
      boundedString(message.sessionId, "sessionId", 128, /^[A-Za-z0-9._:-]+$/);
    }
    boundedString(message.code, "error.code", 128, /^[A-Z0-9_]+$/);
    boundedString(message.message, "error.message", 1024);
    exactBoolean(message.retryable, null, "error.retryable");
    return clone(message);
  }

  var VALIDATORS = Object.freeze({
    hello: validateHello,
    capabilities: validateCapabilities,
    start: validateStart,
    stop: validateStop,
    pcm: validatePcm,
    stats: validateStats,
    error: validateError,
  });

  function validateMessage(message) {
    if (!isPlainObject(message)) {
      fail(
        "本地音频消息必须是普通对象",
        "BW_COMPUTER_VOICE_NATIVE_INVALID",
        false
      );
    }
    var validator = VALIDATORS[message.type];
    if (!validator) {
      fail(
        "不支持的本地音频消息类型",
        "BW_COMPUTER_VOICE_NATIVE_CONTRACT",
        false
      );
    }
    var serialized;
    try {
      serialized = JSON.stringify(message);
    } catch (_) {
      fail(
        "本地音频消息不能序列化",
        "BW_COMPUTER_VOICE_NATIVE_INVALID",
        false
      );
    }
    if (
      typeof serialized !== "string" ||
      byteLength(serialized) > MAX_WIRE_MESSAGE_BYTES
    ) {
      fail(
        "本地音频消息超过 8 KiB 上限",
        "BW_COMPUTER_VOICE_NATIVE_TOO_LARGE",
        false
      );
    }
    return validator(message);
  }

  function createPcmCreditController(options) {
    options = options || {};
    assertExactKeys(options, [], [
      "maxQueuedChunksPerTrack",
      "maxQueuedChunksTotal",
    ], "controller options");
    var perTrack = own(options, "maxQueuedChunksPerTrack")
      ? boundedInteger(
        options.maxQueuedChunksPerTrack,
        "maxQueuedChunksPerTrack",
        1,
        64
      )
      : DEFAULT_MAX_QUEUED_PER_TRACK;
    var total = own(options, "maxQueuedChunksTotal")
      ? boundedInteger(
        options.maxQueuedChunksTotal,
        "maxQueuedChunksTotal",
        1,
        128
      )
      : DEFAULT_MAX_QUEUED_TOTAL;

    var localOptIn = false;
    var paired = false;
    var nativeHostReady = false;
    var oneTimeTrigger = false;
    var active = false;
    var sessionId = null;
    var lastError = null;
    var queues = {};
    queues[TRACK_APP_OUTPUT] = [];
    queues[TRACK_USER_MIC] = [];
    var nextSequence = {};
    nextSequence[TRACK_APP_OUTPUT] = 0;
    nextSequence[TRACK_USER_MIC] = 0;

    function totalQueued() {
      return queues[TRACK_APP_OUTPUT].length + queues[TRACK_USER_MIC].length;
    }

    function clearEphemeral() {
      queues[TRACK_APP_OUTPUT].length = 0;
      queues[TRACK_USER_MIC].length = 0;
      nextSequence[TRACK_APP_OUTPUT] = 0;
      nextSequence[TRACK_USER_MIC] = 0;
      oneTimeTrigger = false;
      active = false;
      sessionId = null;
    }

    function enterFailure(error) {
      var normalized = error instanceof NativeProtocolError
        ? error
        : new NativeProtocolError(
          String(error && error.message || error || "本地音频会话失败"),
          String(error && error.code || "BW_COMPUTER_VOICE_NATIVE_FAILED"),
          false
        );
      clearEphemeral();
      lastError = {
        code: normalized.code,
        message: normalized.message,
        retryable: normalized.retryable,
      };
      throw normalized;
    }

    function readinessSatisfied() {
      return localOptIn && paired && nativeHostReady;
    }

    function state() {
      if (lastError) return "error";
      if (active) return "active";
      if (oneTimeTrigger) return "armed";
      if (!localOptIn) return "disabled";
      if (readinessSatisfied()) return "ready";
      return "blocked";
    }

    function creditFor(trackId) {
      validateTrackId(trackId);
      return Math.max(0, Math.min(
        perTrack - queues[trackId].length,
        total - totalQueued()
      ));
    }

    function credits() {
      var value = {};
      value[TRACK_APP_OUTPUT] = creditFor(TRACK_APP_OUTPUT);
      value[TRACK_USER_MIC] = creditFor(TRACK_USER_MIC);
      return value;
    }

    function queuedChunks() {
      var value = {};
      value[TRACK_APP_OUTPUT] = queues[TRACK_APP_OUTPUT].length;
      value[TRACK_USER_MIC] = queues[TRACK_USER_MIC].length;
      return value;
    }

    function snapshot() {
      return {
        contract: CONTRACT,
        state: state(),
        localOptIn: localOptIn,
        oneTimeTrigger: oneTimeTrigger,
        paired: paired,
        nativeHostReady: nativeHostReady,
        captureActive: active,
        sessionId: sessionId,
        credits: credits(),
        queuedChunks: queuedChunks(),
        totalQueuedChunks: totalQueued(),
        droppedChunks: 0,
        lastError: clone(lastError),
      };
    }

    function setReadiness(value) {
      assertExactKeys(value, [
        "localOptIn",
        "paired",
        "nativeHostReady",
      ], [], "readiness");
      exactBoolean(value.localOptIn, null, "readiness.localOptIn");
      exactBoolean(value.paired, null, "readiness.paired");
      exactBoolean(value.nativeHostReady, null, "readiness.nativeHostReady");
      var losesBoundary = active && (
        !value.localOptIn ||
        !value.paired ||
        !value.nativeHostReady
      );
      localOptIn = value.localOptIn;
      paired = value.paired;
      nativeHostReady = value.nativeHostReady;
      if (losesBoundary || !readinessSatisfied()) clearEphemeral();
      // A trusted readiness update is also the only local reset path after a
      // failed session. Opting out must visibly return to `disabled` instead
      // of leaving a stale error state that appears to keep capture alive.
      lastError = null;
      return snapshot();
    }

    function armOneTimeTrigger() {
      if (!readinessSatisfied() || active) {
        fail(
          "本机未明确启用、未配对或原生宿主未就绪",
          "BW_COMPUTER_VOICE_NATIVE_NOT_READY",
          true
        );
      }
      lastError = null;
      oneTimeTrigger = true;
      return snapshot();
    }

    function start(message) {
      var validated = validateMessage(message);
      if (validated.type !== "start") {
        fail(
          "start() 只接受 start 消息",
          "BW_COMPUTER_VOICE_NATIVE_CONTRACT",
          false
        );
      }
      if (active) {
        fail(
          "本地音频会话已经启动",
          "BW_COMPUTER_VOICE_NATIVE_BUSY",
          false
        );
      }
      if (!readinessSatisfied() || !oneTimeTrigger) {
        fail(
          "缺少本机 opt-in、一次性用户触发、配对或宿主就绪证明",
          "BW_COMPUTER_VOICE_NATIVE_NOT_READY",
          false
        );
      }
      clearEphemeral();
      active = true;
      sessionId = validated.sessionId;
      lastError = null;
      return snapshot();
    }

    function acceptPcm(message) {
      var validated = validateMessage(message);
      if (validated.type !== "pcm") {
        fail(
          "acceptPcm() 只接受 pcm 消息",
          "BW_COMPUTER_VOICE_NATIVE_CONTRACT",
          false
        );
      }
      if (!active || !readinessSatisfied() || validated.sessionId !== sessionId) {
        fail(
          "PCM 会话已失效或未获授权",
          "BW_COMPUTER_VOICE_NATIVE_INACTIVE",
          false
        );
      }
      var trackId = validated.trackId;
      if (validated.sequence !== nextSequence[trackId]) {
        return enterFailure(new NativeProtocolError(
          "PCM sequence 不连续",
          "BW_COMPUTER_VOICE_NATIVE_SEQUENCE",
          false,
          {
            trackId: trackId,
            expected: nextSequence[trackId],
            received: validated.sequence,
          }
        ));
      }
      if (creditFor(trackId) <= 0) {
        return enterFailure(new NativeProtocolError(
          "PCM credit 已耗尽，已安全终止本地会话",
          "BW_COMPUTER_VOICE_NATIVE_BACKPRESSURE",
          false,
          { trackId: trackId }
        ));
      }
      queues[trackId].push(validated);
      nextSequence[trackId] += 1;
      return snapshot();
    }

    function dequeue(trackId) {
      validateTrackId(trackId);
      if (!active || !readinessSatisfied()) {
        fail(
          "本地音频会话未启动",
          "BW_COMPUTER_VOICE_NATIVE_INACTIVE",
          false
        );
      }
      return queues[trackId].length ? clone(queues[trackId].shift()) : null;
    }

    function stop(message) {
      var validated = validateMessage(message);
      if (validated.type !== "stop") {
        fail(
          "stop() 只接受 stop 消息",
          "BW_COMPUTER_VOICE_NATIVE_CONTRACT",
          false
        );
      }
      if (!active || validated.sessionId !== sessionId) {
        fail(
          "stop 会话已失效",
          "BW_COMPUTER_VOICE_NATIVE_INACTIVE",
          false
        );
      }
      clearEphemeral();
      lastError = null;
      return snapshot();
    }

    function disconnect() {
      clearEphemeral();
      nativeHostReady = false;
      lastError = null;
      return snapshot();
    }

    function receiveError(message) {
      var validated = validateMessage(message);
      if (validated.type !== "error") {
        fail(
          "receiveError() 只接受 error 消息",
          "BW_COMPUTER_VOICE_NATIVE_CONTRACT",
          false
        );
      }
      if (
        validated.sessionId !== null &&
        validated.sessionId !== sessionId
      ) {
        fail(
          "error 会话已失效",
          "BW_COMPUTER_VOICE_NATIVE_INACTIVE",
          false
        );
      }
      return enterFailure(new NativeProtocolError(
        validated.message,
        validated.code,
        validated.retryable
      ));
    }

    function statsMessage() {
      return validateStats({
        contract: CONTRACT,
        type: "stats",
        sessionId: sessionId,
        state: state(),
        nativeHostReady: nativeHostReady,
        captureActive: active,
        credits: credits(),
        queuedChunks: queuedChunks(),
        droppedChunks: 0,
      });
    }

    return Object.freeze({
      setReadiness: setReadiness,
      armOneTimeTrigger: armOneTimeTrigger,
      start: start,
      acceptPcm: acceptPcm,
      dequeue: dequeue,
      stop: stop,
      disconnect: disconnect,
      receiveError: receiveError,
      statsMessage: statsMessage,
      status: snapshot,
    });
  }

  return Object.freeze({
    CONTRACT: CONTRACT,
    TRACK_APP_OUTPUT: TRACK_APP_OUTPUT,
    TRACK_USER_MIC: TRACK_USER_MIC,
    TRACK_IDS: TRACK_IDS,
    CAPTURE_SCOPE: CAPTURE_SCOPE,
    LOOPBACK_MODE: LOOPBACK_MODE,
    LOCAL_TRANSPORT: LOCAL_TRANSPORT,
    LOCAL_DESTINATION: LOCAL_DESTINATION,
    SAMPLE_RATE: SAMPLE_RATE,
    CHANNELS: CHANNELS,
    SAMPLE_FORMAT: SAMPLE_FORMAT,
    FRAME_DURATION_MS: FRAME_DURATION_MS,
    FRAMES_PER_CHUNK: FRAMES_PER_CHUNK,
    BYTES_PER_CHUNK: BYTES_PER_CHUNK,
    MAX_WIRE_MESSAGE_BYTES: MAX_WIRE_MESSAGE_BYTES,
    NativeProtocolError: NativeProtocolError,
    validateMessage: validateMessage,
    createPcmCreditController: createPcmCreditController,
  });
});
