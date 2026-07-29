/* rc-computer-voice.js — Reader/PWA ↔ Windows 电脑语音直连入口。
 *
 * 控制与固定帧 PCM 音频只走固定的 tailnet WSS，不经过 Pi。Tailnet 身份和
 * 固定 Origin 由 Windows 端校验；Reader 不保存身份或凭据。选择模型、加载
 * 设置和 STATUS 都不会启动 Windows 应用或采音；只有电话按钮的一次真实
 * 用户操作会发送 START。
 */
(function () {
  "use strict";

  var RC = window.RC = window.RC || {};
  var BRIDGE_CONTRACT = "reader-computer-voice-bridge/1";
  var DIRECT_CONTRACT = "reader-computer-voice-direct/1";
  var MAX_MESSAGE_BYTES = 65536;
  var PCM_FRAME_BYTES = 1956;
  var PCM_HEADER_BYTES = 36;
  var PCM_SAMPLES = 960;
  var PCM_SAMPLE_RATE = 48000;
  var PCM_QUEUE_LIMIT_MS = 400;
  var PCM_UPLINK_TRACK = 3;
  var PCM_UPLINK_BUFFER_LIMIT_BYTES = PCM_FRAME_BYTES * 10;
  var MAX_PENDING = 16;
  var OPEN_TIMEOUT_MS = 6000;
  var REQUEST_TIMEOUT_MS = 7000;
  var START_TIMEOUT_MS = 45000;
  var HEARTBEAT_INTERVAL_MS = 5000;
  var HEARTBEAT_TIMEOUT_MS = 15000;
  var START_GESTURE_LEASE_TTL_MS = 5000;
  var OUTGOING_CONTEXT_CONTRACT = "reader-outgoing-context/1";
  var CONTEXT_BOOTSTRAP_LIMIT = 500;
  var CONTEXT_LIVE_LIMIT = 32;
  var CONTEXT_LIVE_WAIT_S = 20;
  var CONTEXT_RETRY_MS = 500;
  var CONTEXT_WAIT_DENIED_RETRY_MS = 1000;
  var CONTEXT_EVENT_TYPES = Object.freeze({
    "page.context": true,
    focus: true,
    drawing: true,
    command: true,
    "command-failed": true,
  });
  var READER_ORIGIN = "https://bwicarus.taile44d0c.ts.net";
  var EXTENSION_RELAY_PORT = "BW_COMPUTER_VOICE_DIRECT_V3";
  var DIRECT_ENDPOINT =
    "wss://bwicarus-2.taile44d0c.ts.net/reader-computer-voice/v1";
  var STATUS_STATES = Object.freeze({
    idle: true,
    ready: true,
    active: true,
    stopped: true,
    disabled: true,
    busy: true,
    error: true,
    unavailable: true,
    "starting-service": true,
    "starting-app": true,
    "waiting-app-ready": true,
    "starting-capture": true,
  });

  var active = null;
  var preparedSurface = null;
  var preparedTimer = null;
  var requestSequence = 0;
  var statusListeners = [];
  var availabilityAttempt = null;
  var lastClientFailure = null;
  var registeredPhoneButtons = new WeakSet();
  var selectedEngineKnown = false;
  var computerVoiceSelected = false;
  var selectedEngineRevision = 0;
  var dialPending = false;
  // These cursors live only for this module instance. `lastContextAckCursor`
  // is advanced exclusively by an exact Windows per-event ACK.
  var lastContextAckCursor = null;
  // `lastContextResumeCursor` may additionally hold the one explicit initial
  // baseline chosen after a bounded bootstrap scan finds no page.context.
  // It is never presented as an ACK and is not persisted to page storage.
  var lastContextResumeCursor = null;
  var contextPumpGeneration = 0;
  var MICROPHONE_WORKLET =
    'class BWMicCapture extends AudioWorkletProcessor{' +
    'process(i){var c=i[0]&&i[0][0];if(c)this.port.postMessage(c.slice(0));' +
    'return true}}registerProcessor("bw-computer-voice-mic",BWMicCapture);';

  function directError(message, code, retryable) {
    var error = new Error(String(message || "Windows 桥接器直连失败"));
    error.code = String(code || "BW_COMPUTER_VOICE_DIRECT_FAILED");
    error.retryable = retryable === true;
    return error;
  }

  function plainObject(value) {
    return !!value && typeof value === "object" &&
      !Array.isArray(value) &&
      Object.prototype.toString.call(value) === "[object Object]";
  }

  function exactObject(value, required, optional, label) {
    if (!plainObject(value)) {
      throw directError(
        label + " 必须是对象",
        "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
        false
      );
    }
    var allowed = Object.create(null);
    required.concat(optional || []).forEach(function (key) {
      allowed[key] = true;
    });
    Object.keys(value).forEach(function (key) {
      if (!allowed[key]) {
        throw directError(
          label + " 含未知字段 " + key,
          "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
          false
        );
      }
    });
    required.forEach(function (key) {
      if (!Object.prototype.hasOwnProperty.call(value, key)) {
        throw directError(
          label + " 缺少字段 " + key,
          "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
          false
        );
      }
    });
    return value;
  }

  function safeText(value, label, maximum, allowEmpty) {
    if (typeof value !== "string") {
      throw directError(
        label + " 必须是字符串",
        "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
        false
      );
    }
    if ((!allowEmpty && !value) || value.length > maximum) {
      throw directError(
        label + " 长度无效",
        "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
        false
      );
    }
    return value;
  }

  function safeId(value, label) {
    var text = safeText(value, label || "ID", 160, false);
    if (!/^[A-Za-z0-9._:-]+$/.test(text)) {
      throw directError(
        (label || "ID") + " 格式无效",
        "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
        false
      );
    }
    return text;
  }

  function normalizeEndpoint(value) {
    var text = safeText(
      String(value || "").trim(),
      "Windows WSS 地址",
      512,
      false
    );
    var url;
    try {
      url = new URL(text);
    } catch (_) {
      throw directError(
        "Windows WSS 地址无效",
        "BW_COMPUTER_VOICE_DIRECT_URL",
        false
      );
    }
    if (
      url.toString() !== DIRECT_ENDPOINT
    ) {
      throw directError(
        "只允许已固定的 Windows 电脑语音 WSS 地址",
        "BW_COMPUTER_VOICE_DIRECT_URL",
        false
      );
    }
    return DIRECT_ENDPOINT;
  }

  function messageBytes(text) {
    if (typeof TextEncoder === "function") {
      return new TextEncoder().encode(text).byteLength;
    }
    return unescape(encodeURIComponent(text)).length;
  }

  function bytesToBase64Url(buffer) {
    var bytes = new Uint8Array(buffer);
    var binary = "";
    for (var index = 0; index < bytes.length; index += 1) {
      binary += String.fromCharCode(bytes[index]);
    }
    return btoa(binary)
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/g, "");
  }

  function randomId(prefix) {
    var bytes = new Uint8Array(16);
    if (!window.crypto || typeof window.crypto.getRandomValues !== "function") {
      throw directError(
        "浏览器缺少安全随机数支持",
        "BW_COMPUTER_VOICE_DIRECT_CRYPTO",
        false
      );
    }
    window.crypto.getRandomValues(bytes);
    return prefix + "-" + bytesToBase64Url(bytes);
  }

  function randomSession() {
    var bytes = new Uint8Array(16);
    if (!window.crypto || typeof window.crypto.getRandomValues !== "function") {
      throw directError(
        "浏览器缺少安全随机数支持",
        "BW_COMPUTER_VOICE_DIRECT_CRYPTO",
        false
      );
    }
    window.crypto.getRandomValues(bytes);
    return {
      id: "session-" + bytesToBase64Url(bytes),
      bytes: bytes,
    };
  }

  function nextRequestId() {
    requestSequence += 1;
    return randomId("request") + "-" + requestSequence.toString(36);
  }

  function emitStatus(value) {
    var snapshot = Object.assign({
      mode: "computer-client",
      active: !!active,
      direct: true,
      at: Date.now(),
    }, value || {});
    statusListeners.slice().forEach(function (listener) {
      try { listener(snapshot); } catch (_) {}
    });
    try {
      window.dispatchEvent(new CustomEvent(
        "bw-computer-voice-status",
        { detail: snapshot }
      ));
    } catch (_) {}
    return snapshot;
  }

  function normalizeRemoteError(value) {
    exactObject(value, ["code", "message", "retryable"], [], "远端错误");
    var code = safeId(value.code, "错误码");
    var message = safeText(value.message, "错误消息", 300, false);
    if (typeof value.retryable !== "boolean") {
      throw directError(
        "远端错误 retryable 无效",
        "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
        false
      );
    }
    return directError(message, code, value.retryable);
  }

  function normalizeStatusPayload(value) {
    exactObject(
      value,
      [
        "ready",
        "state",
        "reason",
        "localOptIn",
        "media",
        "lastError",
      ],
      [],
      "STATUS 响应"
    );
    if (
      typeof value.ready !== "boolean" ||
      typeof value.localOptIn !== "boolean"
    ) {
      throw directError(
        "STATUS 布尔字段无效",
        "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
        false
      );
    }
    var state = safeText(value.state, "STATUS state", 64, false);
    if (!STATUS_STATES[state]) {
      throw directError(
        "STATUS state 不受支持",
        "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
        false
      );
    }
    if (value.reason !== null) safeText(value.reason, "STATUS reason", 240, true);
    exactObject(
      value.media,
      ["hostReady", "captureActive"],
      [],
      "STATUS media"
    );
    if (
      typeof value.media.hostReady !== "boolean" ||
      typeof value.media.captureActive !== "boolean"
    ) {
      throw directError(
        "STATUS media 布尔字段无效",
        "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
        false
      );
    }
    if (value.lastError !== null) {
      exactObject(
        value.lastError,
        ["failureId", "code", "stage", "hresult", "atUtc"],
        [],
        "STATUS lastError"
      );
      safeId(value.lastError.failureId, "failureId");
      safeId(value.lastError.code, "lastError code");
      safeId(value.lastError.stage, "lastError stage");
      if (
        value.lastError.hresult !== null &&
        (
          typeof value.lastError.hresult !== "string" ||
          !/^0x[0-9A-F]{8}$/.test(value.lastError.hresult)
        )
      ) {
        throw directError(
          "STATUS lastError HRESULT 无效",
          "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
          false
        );
      }
      var atUtc = safeText(
        value.lastError.atUtc,
        "lastError atUtc",
        64,
        false
      );
      if (!Number.isFinite(Date.parse(atUtc))) {
        throw directError(
          "STATUS lastError 时间无效",
          "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
          false
        );
      }
    }
    return value;
  }

  function normalizeStatusEvent(value) {
    exactObject(value, ["state", "reason"], [], "状态事件");
    var state = safeText(value.state, "状态事件 state", 64, false);
    if (!STATUS_STATES[state]) {
      throw directError(
        "状态事件 state 不受支持",
        "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
        false
      );
    }
    if (value.reason !== null) safeText(value.reason, "状态事件 reason", 240, true);
    return value;
  }

  function currentOrigin() {
    return String(window.location && window.location.origin || "");
  }

  function exactRelayMessage(value, type, fields, label) {
    exactObject(value, ["type"].concat(fields || []), [], label);
    if (value.type !== type) {
      throw directError(
        label + " type 无效",
        "BW_COMPUTER_VOICE_DIRECT_RELAY_SCHEMA",
        false
      );
    }
    return value;
  }

  function decodeRelayBinary(value) {
    exactRelayMessage(
      value,
      "binary-base64",
      ["data", "bytes"],
      "扩展中继二进制消息"
    );
    if (
      typeof value.data !== "string" ||
      value.data.length !== 4 * Math.ceil(value.bytes / 3) ||
      !Number.isSafeInteger(value.bytes) ||
      value.bytes !== PCM_FRAME_BYTES ||
      !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/
        .test(value.data)
    ) {
      throw directError(
        "扩展中继 PCM 编码无效",
        "BW_COMPUTER_VOICE_DIRECT_RELAY_BINARY",
        false
      );
    }
    var raw;
    try {
      raw = atob(value.data);
    } catch (_) {
      throw directError(
        "扩展中继 PCM 无法解码",
        "BW_COMPUTER_VOICE_DIRECT_RELAY_BINARY",
        false
      );
    }
    if (raw.length !== value.bytes) {
      throw directError(
        "扩展中继 PCM 长度不匹配",
        "BW_COMPUTER_VOICE_DIRECT_RELAY_BINARY",
        false
      );
    }
    var buffer = new ArrayBuffer(raw.length);
    var bytes = new Uint8Array(buffer);
    for (var index = 0; index < raw.length; index += 1) {
      bytes[index] = raw.charCodeAt(index);
    }
    return buffer;
  }

  function encodeRelayBinary(value) {
    var bytes;
    if (
      Object.prototype.toString.call(value) === "[object ArrayBuffer]"
    ) {
      bytes = new Uint8Array(value);
    } else if (ArrayBuffer.isView(value)) {
      bytes = new Uint8Array(
        value.buffer,
        value.byteOffset,
        value.byteLength
      );
    } else {
      throw directError(
        "扩展中继只允许固定 PCM 二进制帧",
        "BW_COMPUTER_VOICE_DIRECT_RELAY_BINARY",
        false
      );
    }
    if (
      bytes.byteLength !== PCM_FRAME_BYTES ||
      bytes[0] !== 0x42 ||
      bytes[1] !== 0x57 ||
      bytes[2] !== 0x43 ||
      bytes[3] !== 0x56 ||
      bytes[4] !== 1 ||
      bytes[5] !== PCM_UPLINK_TRACK ||
      bytes[6] !== 0 ||
      bytes[7] !== 0
    ) {
      throw directError(
        "扩展中继只允许 Reader 麦克风 PCM 帧",
        "BW_COMPUTER_VOICE_DIRECT_RELAY_BINARY",
        false
      );
    }
    var binary = "";
    for (var index = 0; index < bytes.byteLength; index += 1) {
      binary += String.fromCharCode(bytes[index]);
    }
    return {
      type: "send-binary-base64",
      data: btoa(binary),
      bytes: bytes.byteLength,
      sequence: new DataView(
        bytes.buffer,
        bytes.byteOffset,
        bytes.byteLength
      ).getUint32(24, true),
    };
  }

  function ExtensionRelaySocket(runtime) {
    this.readyState = 0;
    this.binaryType = "arraybuffer";
    this.onopen = null;
    this.onmessage = null;
    this.onerror = null;
    this.onclose = null;
    this.port = null;
    this.terminal = false;
    this.closedPromise = null;
    this.closedResolve = null;
    this.binaryInFlight = null;
    this.binaryAckTimer = null;
    var self = this;
    this.closedPromise = new Promise(function (resolve) {
      self.closedResolve = resolve;
    });

    var port;
    try {
      port = runtime.connect({ name: EXTENSION_RELAY_PORT });
    } catch (_) {
      throw directError(
        "扩展后台直连中继不可用",
        "BW_COMPUTER_VOICE_DIRECT_RELAY_UNAVAILABLE",
        true
      );
    }
    if (
      !port ||
      typeof port.postMessage !== "function" ||
      !port.onMessage ||
      typeof port.onMessage.addListener !== "function" ||
      !port.onDisconnect ||
      typeof port.onDisconnect.addListener !== "function"
    ) {
      try { if (port && typeof port.disconnect === "function") port.disconnect(); }
      catch (_) {}
      throw directError(
        "扩展后台直连中继合同无效",
        "BW_COMPUTER_VOICE_DIRECT_RELAY_UNAVAILABLE",
        false
      );
    }
    this.port = port;
    port.onMessage.addListener(function (message) {
      self._onRelayMessage(message);
    });
    port.onDisconnect.addListener(function () {
      if (self.terminal) return;
      self.terminal = true;
      self.readyState = 3;
      try {
        if (typeof self.onclose === "function") {
          self.onclose({
            code: 1006,
            reason: "extension-relay-disconnected",
            wasClean: false,
          });
        }
      } finally {
        self._finishClosed();
      }
    });
    try {
      port.postMessage({ type: "open" });
    } catch (_) {
      this.terminal = true;
      this.readyState = 3;
      try { if (typeof port.disconnect === "function") port.disconnect(); }
      catch (_) {}
      this._finishClosed();
      throw directError(
        "扩展后台直连中继启动失败",
        "BW_COMPUTER_VOICE_DIRECT_RELAY_UNAVAILABLE",
        true
      );
    }
  }

  ExtensionRelaySocket.prototype._finishClosed = function () {
    if (this.binaryAckTimer) {
      clearTimeout(this.binaryAckTimer);
      this.binaryAckTimer = null;
    }
    this.binaryInFlight = null;
    var resolve = this.closedResolve;
    this.closedResolve = null;
    if (typeof resolve === "function") resolve();
  };

  ExtensionRelaySocket.prototype.whenClosed = function () {
    return this.closedPromise || Promise.resolve();
  };

  ExtensionRelaySocket.prototype._protocolFailure = function (error) {
    if (this.terminal) return;
    this.terminal = true;
    this.readyState = 3;
    try { this.port.postMessage({ type: "close" }); } catch (_) {}
    try {
      if (this.port && typeof this.port.disconnect === "function") {
        this.port.disconnect();
      }
    } catch (_) {}
    if (typeof this.onerror === "function") this.onerror(error);
    this._finishClosed();
  };

  ExtensionRelaySocket.prototype._onRelayMessage = function (message) {
    if (this.terminal) return;
    try {
      if (!plainObject(message) || typeof message.type !== "string") {
        throw directError(
          "扩展后台直连中继消息无效",
          "BW_COMPUTER_VOICE_DIRECT_RELAY_SCHEMA",
          false
        );
      }
      if (message.type === "open") {
        exactRelayMessage(message, "open", [], "扩展中继 open");
        if (this.readyState !== 0) {
          throw directError(
            "扩展后台重复打开直连中继",
            "BW_COMPUTER_VOICE_DIRECT_RELAY_SCHEMA",
            false
          );
        }
        this.readyState = 1;
        if (typeof this.onopen === "function") this.onopen();
        return;
      }
      if (message.type === "text") {
        exactRelayMessage(message, "text", ["data"], "扩展中继 text");
        if (this.readyState !== 1 || typeof message.data !== "string") {
          throw directError(
            "扩展后台直连文本状态无效",
            "BW_COMPUTER_VOICE_DIRECT_RELAY_SCHEMA",
            false
          );
        }
        if (typeof this.onmessage === "function") {
          this.onmessage({ data: message.data });
        }
        return;
      }
      if (message.type === "binary-base64") {
        if (this.readyState !== 1) {
          throw directError(
            "扩展后台直连二进制状态无效",
            "BW_COMPUTER_VOICE_DIRECT_RELAY_SCHEMA",
            false
          );
        }
        var buffer = decodeRelayBinary(message);
        if (typeof this.onmessage === "function") {
          this.onmessage({ data: buffer });
        }
        return;
      }
      if (message.type === "binary-accepted") {
        exactRelayMessage(
          message,
          "binary-accepted",
          ["sequence", "bytes"],
          "扩展中继上行确认"
        );
        if (
          this.readyState !== 1 ||
          !Number.isSafeInteger(message.sequence) ||
          message.sequence < 0 ||
          message.sequence > 0xffffffff ||
          message.bytes !== PCM_FRAME_BYTES ||
          message.sequence !== this.binaryInFlight
        ) {
          throw directError(
            "扩展后台上行确认错配",
            "BW_COMPUTER_VOICE_DIRECT_RELAY_BINARY_ACK",
            false
          );
        }
        if (this.binaryAckTimer) {
          clearTimeout(this.binaryAckTimer);
          this.binaryAckTimer = null;
        }
        this.binaryInFlight = null;
        return;
      }
      if (message.type === "error") {
        exactRelayMessage(
          message,
          "error",
          ["code", "error"],
          "扩展中继 error"
        );
        safeId(message.code, "扩展中继错误码");
        safeText(message.error, "扩展中继错误消息", 300, false);
        this._protocolFailure(directError(
          message.error,
          message.code,
          true
        ));
        return;
      }
      if (message.type === "close") {
        exactRelayMessage(
          message,
          "close",
          ["code", "reason", "wasClean"],
          "扩展中继 close"
        );
        if (
          !Number.isSafeInteger(message.code) ||
          message.code < 1000 ||
          message.code > 4999 ||
          typeof message.wasClean !== "boolean"
        ) {
          throw directError(
            "扩展后台直连 close 字段无效",
            "BW_COMPUTER_VOICE_DIRECT_RELAY_SCHEMA",
            false
          );
        }
        safeText(message.reason, "扩展中继关闭原因", 123, true);
        this.terminal = true;
        this.readyState = 3;
        try {
          if (typeof this.onclose === "function") {
            this.onclose({
              code: message.code,
              reason: message.reason,
              wasClean: message.wasClean,
            });
          }
        } finally {
          try {
            if (this.port && typeof this.port.disconnect === "function") {
              this.port.disconnect();
            }
          } catch (_) {}
          this._finishClosed();
        }
        return;
      }
      throw directError(
        "扩展后台直连中继消息类型不受支持",
        "BW_COMPUTER_VOICE_DIRECT_RELAY_SCHEMA",
        false
      );
    } catch (error) {
      this._protocolFailure(error && error.code ? error : directError(
        "扩展后台直连中继消息无法解析",
        "BW_COMPUTER_VOICE_DIRECT_RELAY_SCHEMA",
        false
      ));
    }
  };

  ExtensionRelaySocket.prototype.send = function (value) {
    if (
      this.terminal ||
      this.readyState !== 1
    ) {
      throw directError(
        "扩展后台直连中继不可发送",
        "BW_COMPUTER_VOICE_DIRECT_RELAY_DISCONNECTED",
        true
      );
    }
    if (typeof value === "string") {
      this.port.postMessage({ type: "send-text", data: value });
      return true;
    }
    if (this.binaryInFlight !== null) return false;
    var message = encodeRelayBinary(value);
    this.binaryInFlight = message.sequence;
    var self = this;
    this.binaryAckTimer = setTimeout(function () {
      self.binaryAckTimer = null;
      self._protocolFailure(directError(
        "扩展后台未确认 Reader 麦克风帧",
        "BW_COMPUTER_VOICE_DIRECT_RELAY_BINARY_ACK",
        true
      ));
    }, 2000);
    try {
      this.port.postMessage(message);
    } catch (error) {
      clearTimeout(this.binaryAckTimer);
      this.binaryAckTimer = null;
      this.binaryInFlight = null;
      throw error;
    }
    return true;
  };

  ExtensionRelaySocket.prototype.close = function () {
    if (this.terminal || this.readyState >= 2) return;
    this.readyState = 2;
    try {
      this.port.postMessage({ type: "close" });
    } catch (_) {
      this.terminal = true;
      this.readyState = 3;
      try {
        if (this.port && typeof this.port.disconnect === "function") {
          this.port.disconnect();
        }
      } catch (_) {}
      this._finishClosed();
    }
  };

  function createDirectTransport(endpoint) {
    if (currentOrigin() === READER_ORIGIN) {
      if (typeof window.WebSocket !== "function") {
        throw directError(
          "Reader 缺少 WebSocket 能力",
          "BW_COMPUTER_VOICE_DIRECT_OFFLINE",
          true
        );
      }
      // @interaction computer-voice.bridge.request
      return new window.WebSocket(endpoint);
    }
    var runtime = window.chrome && window.chrome.runtime;
    if (
      runtime &&
      typeof runtime.id === "string" &&
      runtime.id &&
      typeof runtime.connect === "function"
    ) {
      return new ExtensionRelaySocket(runtime);
    }
    throw directError(
      "普通网页必须通过受信扩展后台连接 Windows 桥接器",
      "BW_COMPUTER_VOICE_DIRECT_RELAY_REQUIRED",
      false
    );
  }

  function DirectSocket(endpoint, options) {
    this.endpoint = normalizeEndpoint(endpoint);
    this.options = options || {};
    this.socket = null;
    this.pending = new Map();
    this.opened = false;
    this.closed = false;
    this.intentional = false;
    this.openReject = null;
    this.openTimer = null;
    this.closingPromise = null;
  }

  DirectSocket.prototype._fail = function (error) {
    if (this.closed) return;
    this.closed = true;
    if (this.openTimer) {
      clearTimeout(this.openTimer);
      this.openTimer = null;
    }
    var socket = this.socket;
    this.socket = null;
    if (this.openReject) {
      this.openReject(error);
      this.openReject = null;
    }
    this.pending.forEach(function (entry) {
      clearTimeout(entry.timer);
      entry.reject(error);
    });
    this.pending.clear();
    try {
      if (socket && socket.readyState < 2) socket.close(1002, "protocol-error");
    } catch (_) {}
    if (!this.intentional && typeof this.options.onFatal === "function") {
      try { this.options.onFatal(error); } catch (_) {}
    }
  };

  DirectSocket.prototype.open = function () {
    var self = this;
    if (window.isSecureContext === false) {
      return Promise.reject(directError(
        "Windows 桥接直连要求安全页面",
        "BW_COMPUTER_VOICE_DIRECT_INSECURE_CONTEXT",
        false
      ));
    }
    return new Promise(function (resolve, reject) {
      var socket;
      try {
        // @interaction computer-voice.bridge.request
        socket = createDirectTransport(self.endpoint);
      } catch (error) {
        reject(error && error.code ? error : directError(
          "Windows 桥接器离线或 WSS 地址不可达",
          "BW_COMPUTER_VOICE_DIRECT_OFFLINE",
          true
        ));
        return;
      }
      self.socket = socket;
      socket.binaryType = "arraybuffer";
      self.openReject = reject;
      self.openTimer = setTimeout(function () {
        self._fail(directError(
          "连接 Windows 桥接器超时",
          "BW_COMPUTER_VOICE_DIRECT_TIMEOUT",
          true
        ));
      }, OPEN_TIMEOUT_MS);
      socket.onopen = function () {
        if (self.closed) return;
        clearTimeout(self.openTimer);
        self.openTimer = null;
        self.opened = true;
        self.openReject = null;
        resolve(self);
      };
      socket.onmessage = function (event) {
        self._onMessage(event);
      };
      socket.onerror = function () {
        self._fail(directError(
          "Windows 桥接器离线或 WSS 连接失败",
          "BW_COMPUTER_VOICE_DIRECT_OFFLINE",
          true
        ));
      };
      socket.onclose = function () {
        if (self.intentional || self.closed) return;
        self._fail(directError(
          "Windows 桥接器连接已断开",
          "BW_COMPUTER_VOICE_DIRECT_DISCONNECTED",
          true
        ));
      };
    });
  };

  DirectSocket.prototype._onMessage = function (event) {
    if (this.closed) return;
    if (
      event &&
      Object.prototype.toString.call(event.data) === "[object ArrayBuffer]"
    ) {
      if (event.data.byteLength !== PCM_FRAME_BYTES) {
        this._fail(directError(
          "Windows PCM 帧长度无效",
          "BW_COMPUTER_VOICE_DIRECT_PCM_FRAME",
          false
        ));
        return;
      }
      if (typeof this.options.onBinary !== "function") {
        this._fail(directError(
          "Windows 在非通话连接发送了 PCM",
          "BW_COMPUTER_VOICE_DIRECT_PCM_UNEXPECTED",
          false
        ));
        return;
      }
      try {
        this.options.onBinary(event.data);
      } catch (error) {
        this._fail(error && error.code ? error : directError(
          "Windows PCM 帧无法解析",
          "BW_COMPUTER_VOICE_DIRECT_PCM_FRAME",
          false
        ));
      }
      return;
    }
    if (!event || typeof event.data !== "string") {
      this._fail(directError(
        "Windows 桥接器发送了未知二进制类型",
        "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
        false
      ));
      return;
    }
    if (messageBytes(event.data) > MAX_MESSAGE_BYTES) {
      this._fail(directError(
        "Windows 桥接器控制帧超过 64 KiB",
        "BW_COMPUTER_VOICE_DIRECT_CAPACITY",
        false
      ));
      return;
    }
    var message;
    try {
      message = JSON.parse(event.data);
      exactObject(
        message,
        ["contract", "type"],
        ["requestId", "ok", "action", "payload", "error", "event"],
        "WSS 响应"
      );
      if (message.contract !== DIRECT_CONTRACT) {
        throw directError(
          "Windows 桥接器合同版本不匹配",
          "BW_COMPUTER_VOICE_DIRECT_CONTRACT",
          false
        );
      }
      if (message.type === "event") {
        exactObject(
          message,
          ["contract", "type", "event", "payload"],
          [],
          "WSS 事件"
        );
        if (message.event !== "status") {
          throw directError(
            "Windows 桥接器事件类型不受支持",
            "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
            false
          );
        }
        var status = normalizeStatusEvent(message.payload);
        if (typeof this.options.onStatus === "function") {
          this.options.onStatus(status);
        }
        return;
      }
      exactObject(
        message,
        ["contract", "type", "requestId", "ok", "action"],
        ["payload", "error"],
        "WSS result"
      );
      if (message.type !== "result" || typeof message.ok !== "boolean") {
        throw directError(
          "Windows 桥接器 result 无效",
          "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
          false
        );
      }
      var requestId = safeId(message.requestId, "requestId");
      var action = safeText(message.action, "action", 32, false);
      var pending = this.pending.get(requestId);
      if (!pending || pending.action !== action) {
        throw directError(
          "Windows 桥接器返回未知或错配的 requestId",
          "BW_COMPUTER_VOICE_DIRECT_REQUEST",
          false
        );
      }
      this.pending.delete(requestId);
      clearTimeout(pending.timer);
      if (message.ok === true) {
        if (
          !Object.prototype.hasOwnProperty.call(message, "payload") ||
          Object.prototype.hasOwnProperty.call(message, "error")
        ) {
          throw directError(
            "Windows 桥接器成功响应字段无效",
            "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
            false
          );
        }
        pending.resolve(message.payload);
      } else {
        if (
          !Object.prototype.hasOwnProperty.call(message, "error") ||
          Object.prototype.hasOwnProperty.call(message, "payload")
        ) {
          throw directError(
            "Windows 桥接器失败响应字段无效",
            "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
            false
          );
        }
        pending.reject(normalizeRemoteError(message.error));
      }
    } catch (error) {
      this._fail(error && error.code ? error : directError(
        "Windows 桥接器响应无法解析",
        "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
        false
      ));
    }
  };

  DirectSocket.prototype.request = function (action, fields, timeoutMs) {
    if (
      this.closed ||
      !this.socket ||
      this.socket.readyState !== 1
    ) {
      return Promise.reject(directError(
        "Windows 桥接器连接不可用",
        "BW_COMPUTER_VOICE_DIRECT_DISCONNECTED",
        true
      ));
    }
    if (this.pending.size >= MAX_PENDING) {
      return Promise.reject(directError(
        "Windows 桥接器待处理请求过多",
        "BW_COMPUTER_VOICE_DIRECT_CAPACITY",
        false
      ));
    }
    var requestId = nextRequestId();
    var message = Object.assign({
      contract: DIRECT_CONTRACT,
      type: action,
      requestId: requestId,
    }, fields || {});
    var serialized;
    try {
      serialized = JSON.stringify(message);
    } catch (_) {
      return Promise.reject(directError(
        "Windows 桥接请求不能序列化",
        "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
        false
      ));
    }
    var serializedBytes = messageBytes(serialized);
    if (serializedBytes > MAX_MESSAGE_BYTES) {
      return Promise.reject(directError(
        "Windows 桥接请求超过 64 KiB",
        "BW_COMPUTER_VOICE_DIRECT_CAPACITY",
        false
      ));
    }
    var self = this;
    return new Promise(function (resolve, reject) {
      var timer = setTimeout(function () {
        self.pending.delete(requestId);
        reject(directError(
          action === "start"
            ? "Windows 启动结果未知；连接已关闭，不会自动重试"
            : "Windows 桥接器请求超时",
          action === "start"
            ? "BW_COMPUTER_VOICE_DIRECT_START_UNKNOWN"
            : "BW_COMPUTER_VOICE_DIRECT_TIMEOUT",
          false
        ));
        self._fail(directError(
          "Windows 桥接器请求超时",
          "BW_COMPUTER_VOICE_DIRECT_TIMEOUT",
          false
        ));
      }, timeoutMs || REQUEST_TIMEOUT_MS);
      self.pending.set(requestId, {
        action: action,
        resolve: resolve,
        reject: reject,
        timer: timer,
      });
      try {
        self.socket.send(serialized);
      } catch (_) {
        clearTimeout(timer);
        self.pending.delete(requestId);
        reject(directError(
          "Windows 桥接请求发送失败",
          "BW_COMPUTER_VOICE_DIRECT_DISCONNECTED",
          true
        ));
      }
    });
  };

  DirectSocket.prototype.sendBinary = function (buffer) {
    if (
      this.closed ||
      !this.socket ||
      this.socket.readyState !== 1 ||
      Object.prototype.toString.call(buffer) !== "[object ArrayBuffer]" ||
      buffer.byteLength !== PCM_FRAME_BYTES
    ) {
      throw directError(
        "Reader 麦克风 PCM 连接不可用",
        "BW_COMPUTER_VOICE_DIRECT_UPLINK_DISCONNECTED",
        true
      );
    }
    if (
      typeof this.socket.bufferedAmount === "number" &&
      this.socket.bufferedAmount + PCM_FRAME_BYTES >
        PCM_UPLINK_BUFFER_LIMIT_BYTES
    ) {
      throw directError(
        "Reader 麦克风上行已落后，已停止而不发送过期语音",
        "BW_COMPUTER_VOICE_DIRECT_UPLINK_BACKPRESSURE",
        true
      );
    }
    try {
      return this.socket.send(buffer) !== false;
    } catch (error) {
      throw directError(
        "Reader 麦克风 PCM 发送失败",
        "BW_COMPUTER_VOICE_DIRECT_UPLINK_DISCONNECTED",
        true
      );
    }
  };

  DirectSocket.prototype.close = function () {
    if (this.intentional) {
      return this.closingPromise || Promise.resolve();
    }
    this.intentional = true;
    this.closed = true;
    if (this.openTimer) {
      clearTimeout(this.openTimer);
      this.openTimer = null;
    }
    var cancelled = directError(
      "Windows 桥接连接已关闭",
      "BW_COMPUTER_VOICE_DIRECT_CANCELLED",
      false
    );
    if (this.openReject) {
      this.openReject(cancelled);
      this.openReject = null;
    }
    this.pending.forEach(function (entry) {
      clearTimeout(entry.timer);
      entry.reject(cancelled);
    });
    this.pending.clear();
    var socket = this.socket;
    var transportClosed =
      socket && typeof socket.whenClosed === "function"
        ? socket.whenClosed()
        : Promise.resolve();
    this.closingPromise = Promise.resolve(transportClosed).catch(function () {});
    try {
      if (socket && socket.readyState < 2) {
        socket.close(1000, "client-stop");
      }
    } catch (_) {}
    this.socket = null;
    return this.closingPromise;
  };

  function normalizeHello(value) {
    exactObject(
      value,
      ["protocolVersion", "limits"],
      [],
      "HELLO 响应"
    );
    if (value.protocolVersion !== 3) {
      throw directError(
        "Windows 桥接器直连协议版本不匹配",
        "BW_COMPUTER_VOICE_DIRECT_CONTRACT",
        false
      );
    }
    exactObject(
      value.limits,
      [
        "maxMessageBytes",
        "pcmFrameBytes",
        "pcmQueueLimitMs",
        "heartbeatIntervalMs",
        "heartbeatTimeoutMs",
        "uplinkTrack",
        "uplinkQueueLimitMs",
      ],
      [],
      "HELLO limits"
    );
    if (
      value.limits.maxMessageBytes !== MAX_MESSAGE_BYTES ||
      value.limits.pcmFrameBytes !== PCM_FRAME_BYTES ||
      value.limits.pcmQueueLimitMs !== PCM_QUEUE_LIMIT_MS ||
      value.limits.heartbeatIntervalMs !== HEARTBEAT_INTERVAL_MS ||
      value.limits.heartbeatTimeoutMs !== HEARTBEAT_TIMEOUT_MS ||
      value.limits.uplinkTrack !== PCM_UPLINK_TRACK ||
      value.limits.uplinkQueueLimitMs !== 200
    ) {
      throw directError(
        "Windows 桥接器容量合同不匹配",
        "BW_COMPUTER_VOICE_DIRECT_CONTRACT",
        false
      );
    }
    return value;
  }

  function hello(channel) {
    return channel.request("hello", {
      protocolVersion: 3,
    }).then(normalizeHello);
  }

  function openDirect(options, onCreate) {
    var channel = new DirectSocket(DIRECT_ENDPOINT, options);
    if (typeof onCreate === "function") onCreate(channel);
    return channel.open().then(function () {
      return hello(channel);
    }).then(function () {
      return channel;
    }).catch(function (error) {
      channel.close();
      throw error;
    });
  }

  function offlineAvailability(error) {
    var code = error && error.code || "BW_COMPUTER_VOICE_DIRECT_OFFLINE";
    return {
      state: "offline",
      reason: error && error.message || "Windows 桥接器离线",
      code: code,
      endpoint: DIRECT_ENDPOINT,
      status: null,
    };
  }

  function activeAvailability(state) {
    var started = !!(state && state.started);
    var currentState = started ? "active" : "busy";
    var reason = started ? "电脑客户端通话中" : "电脑客户端正在启动";
    return {
      state: currentState,
      reason: reason,
      endpoint: DIRECT_ENDPOINT,
      status: {
        ready: started,
        state: currentState,
        reason: reason,
        localOptIn: true,
        media: {
          hostReady: started,
          captureActive: started,
        },
        lastError: null,
      },
    };
  }

  function cancelAvailabilityForStart() {
    var attempt = availabilityAttempt;
    if (!attempt) return Promise.resolve();
    attempt.cancelled = true;
    if (attempt.channel) attempt.channel.close();
    return Promise.resolve(attempt.promise).then(
      function () {},
      function () {}
    );
  }

  function availability() {
    if (active) return Promise.resolve(activeAvailability(active));
    if (availabilityAttempt) return availabilityAttempt.promise;

    var attempt = {
      cancelled: false,
      channel: null,
      promise: null,
    };
    availabilityAttempt = attempt;
    attempt.promise = openDirect(null, function (channel) {
      attempt.channel = channel;
    }).then(function (channel) {
      if (attempt.cancelled) {
        channel.close();
        throw directError(
          "状态刷新已让位给电话按钮启动",
          "BW_COMPUTER_VOICE_DIRECT_CANCELLED",
          false
        );
      }
      return channel.request("status", {}).then(normalizeStatusPayload);
    }).then(function (status) {
      return {
        state: status.state,
        reason: status.reason,
        endpoint: DIRECT_ENDPOINT,
        status: status,
      };
    }).catch(function (error) {
      if (attempt.cancelled && active) return activeAvailability(active);
      return offlineAvailability(error);
    }).finally(function () {
      var closePromise = attempt.channel
        ? attempt.channel.close()
        : Promise.resolve();
      attempt.channel = null;
      if (availabilityAttempt === attempt) availabilityAttempt = null;
      return closePromise;
    });
    return attempt.promise;
  }

  function makeAudioSurface() {
    var AudioContextCtor = window.AudioContext || window.webkitAudioContext;
    if (typeof AudioContextCtor !== "function") {
      throw directError(
        "浏览器缺少 PCM 播放能力",
        "BW_COMPUTER_VOICE_AUDIO_UNAVAILABLE",
        false
      );
    }
    var context = new AudioContextCtor({ sampleRate: PCM_SAMPLE_RATE });
    return {
      context: context,
      sources: new Set(),
      pending: [],
      nextAt: 0,
      microphone: null,
      microphonePromise: null,
      microphoneError: null,
      ownerState: null,
      released: false,
    };
  }

  function setComputerVoiceAudioSession(kind) {
    try {
      if (
        navigator.audioSession &&
        typeof navigator.audioSession.type === "string"
      ) {
        navigator.audioSession.type = kind;
      }
    } catch (_) {}
  }

  function stopSurfaceMicrophone(surface) {
    if (!surface) return;
    var microphone = surface.microphone;
    if (!microphone) return;
    surface.microphone = null;
    microphone.active = false;
    microphone.pending = new Float32Array(0);
    if (microphone.muteTimer) {
      clearTimeout(microphone.muteTimer);
      microphone.muteTimer = null;
    }
    if (microphone.track) {
      microphone.track.onended = null;
      microphone.track.onmute = null;
      microphone.track.onunmute = null;
    }
    try {
      if (microphone.node && microphone.node.port) {
        microphone.node.port.onmessage = null;
      }
      if (microphone.node && "onaudioprocess" in microphone.node) {
        microphone.node.onaudioprocess = null;
      }
    } catch (_) {}
    try { if (microphone.source) microphone.source.disconnect(); } catch (_) {}
    try { if (microphone.node) microphone.node.disconnect(); } catch (_) {}
    try { if (microphone.silent) microphone.silent.disconnect(); } catch (_) {}
    try {
      microphone.stream.getTracks().forEach(function (track) {
        track.stop();
      });
    } catch (_) {}
  }

  function releaseSurface(surface) {
    if (!surface || surface.released) return;
    surface.released = true;
    stopSurfaceMicrophone(surface);
    surface.pending.length = 0;
    surface.sources.forEach(function (source) {
      try { source.stop(); } catch (_) {}
    });
    surface.sources.clear();
    try { surface.context.close(); } catch (_) {}
    setComputerVoiceAudioSession("playback");
  }

  function microphoneStartError(error) {
    var name = String(error && error.name || "");
    if (name === "NotAllowedError" || name === "SecurityError") {
      return directError(
        "浏览器没有获得麦克风权限；请允许后再次点击电话按钮",
        "BW_COMPUTER_VOICE_MICROPHONE_PERMISSION",
        false
      );
    }
    if (name === "NotFoundError" || name === "DevicesNotFoundError") {
      return directError(
        "当前设备没有可用的网页麦克风",
        "BW_COMPUTER_VOICE_MICROPHONE_NOT_FOUND",
        true
      );
    }
    if (name === "OverconstrainedError") {
      return directError(
        "网页麦克风不支持所需音频格式",
        "BW_COMPUTER_VOICE_MICROPHONE_CONSTRAINT",
        false
      );
    }
    if (
      name === "NotReadableError" ||
      name === "AbortError" ||
      name === "TrackStartError"
    ) {
      return directError(
        "网页麦克风当前被系统占用或不可读",
        "BW_COMPUTER_VOICE_MICROPHONE_UNAVAILABLE",
        true
      );
    }
    return error && error.code ? error : directError(
      "网页麦克风启动失败",
      "BW_COMPUTER_VOICE_MICROPHONE_START_FAILED",
      true
    );
  }

  function failForMicrophoneState(surface, message, code) {
    var state = surface && surface.ownerState;
    if (!state || state.stopped || active !== state) return;
    failActive(state, directError(message, code, true), true);
  }

  function installMicrophoneTrackGuards(surface, microphone) {
    var track = microphone.track;
    track.onended = function () {
      failForMicrophoneState(
        surface,
        "网页麦克风已被系统停止；请重新点击电话按钮",
        "BW_COMPUTER_VOICE_MICROPHONE_ENDED"
      );
    };
    track.onmute = function () {
      if (microphone.muteTimer) clearTimeout(microphone.muteTimer);
      var state = surface.ownerState;
      if (state && !state.stopped && active === state) {
        emitStatus({
          state: "microphone-muted",
          sessionId: state.sessionId,
          message: "网页麦克风被系统暂停；持续暂停将结束通话",
          code: "BW_COMPUTER_VOICE_MICROPHONE_MUTED",
        });
      }
      microphone.muteTimer = setTimeout(function () {
        microphone.muteTimer = null;
        if (track.muted === true) {
          failForMicrophoneState(
            surface,
            "网页麦克风持续被系统暂停；请回到页面后重新点击电话按钮",
            "BW_COMPUTER_VOICE_MICROPHONE_MUTED"
          );
        }
      }, 2000);
    };
    track.onunmute = function () {
      if (microphone.muteTimer) {
        clearTimeout(microphone.muteTimer);
        microphone.muteTimer = null;
      }
      var state = surface.ownerState;
      if (state && state.started && !state.stopped && active === state) {
        emitStatus({
          state: "connected",
          sessionId: state.sessionId,
          message: "网页麦克风已恢复，电脑客户端通话中",
        });
      }
    };
  }

  function appendMicrophoneChunk(surface, chunk, sampleRate) {
    var microphone = surface && surface.microphone;
    var state = surface && surface.ownerState;
    if (
      !microphone ||
      !microphone.active ||
      !state ||
      !state.uplinkActive ||
      state.stopped ||
      active !== state
    ) {
      if (microphone) microphone.pending = new Float32Array(0);
      return;
    }
    if (
      !chunk ||
      typeof chunk.length !== "number" ||
      !Number.isFinite(sampleRate) ||
      sampleRate < 8000 ||
      sampleRate > 192000
    ) {
      failActive(state, directError(
        "网页麦克风 PCM 格式无效",
        "BW_COMPUTER_VOICE_MICROPHONE_PCM",
        false
      ), true);
      return;
    }
    var combined = new Float32Array(
      microphone.pending.length + chunk.length
    );
    combined.set(microphone.pending);
    combined.set(chunk, microphone.pending.length);
    microphone.pending = combined;
    var inputSamples = Math.round(sampleRate * 0.02);
    while (
      microphone.pending.length >= inputSamples &&
      !state.stopped &&
      active === state
    ) {
      var input = microphone.pending.subarray(0, inputSamples);
      microphone.pending = microphone.pending.slice(inputSamples);
      var output = new Int16Array(PCM_SAMPLES);
      for (var index = 0; index < PCM_SAMPLES; index += 1) {
        var position = PCM_SAMPLES === 1
          ? 0
          : index * (inputSamples - 1) / (PCM_SAMPLES - 1);
        var left = Math.floor(position);
        var right = Math.min(inputSamples - 1, left + 1);
        var fraction = position - left;
        var value = input[left] +
          (input[right] - input[left]) * fraction;
        value = Math.max(-1, Math.min(1, Number(value) || 0));
        output[index] = value < 0
          ? Math.round(value * 32768)
          : Math.round(value * 32767);
      }
      try {
        var sent = state.channel.sendBinary(
          encodeMicrophoneFrame(state, output)
        );
        if (sent) state.uplinkSequence += 1;
      } catch (error) {
        failActive(state, error, true);
        return;
      }
    }
  }

  function encodeMicrophoneFrame(state, samples) {
    if (
      !state ||
      !state.sessionBytes ||
      samples.length !== PCM_SAMPLES ||
      state.uplinkSequence < 0 ||
      state.uplinkSequence > 0xffffffff
    ) {
      throw directError(
        "Reader 麦克风 PCM 帧合同无效",
        "BW_COMPUTER_VOICE_DIRECT_UPLINK_FRAME",
        false
      );
    }
    var buffer = new ArrayBuffer(PCM_FRAME_BYTES);
    var view = new DataView(buffer);
    view.setUint8(0, 0x42);
    view.setUint8(1, 0x57);
    view.setUint8(2, 0x43);
    view.setUint8(3, 0x56);
    view.setUint8(4, 1);
    view.setUint8(5, PCM_UPLINK_TRACK);
    view.setUint16(6, 0, true);
    for (var byteIndex = 0; byteIndex < 16; byteIndex += 1) {
      view.setUint8(8 + byteIndex, state.sessionBytes[byteIndex]);
    }
    view.setUint32(24, state.uplinkSequence, true);
    var timestamp = state.uplinkTimestampBase +
      state.uplinkSequence * 20000;
    var timestampHigh = Math.floor(timestamp / 0x100000000);
    var timestampLow = timestamp - timestampHigh * 0x100000000;
    view.setUint32(28, timestampLow, true);
    view.setUint32(32, timestampHigh, true);
    for (var sampleIndex = 0; sampleIndex < PCM_SAMPLES; sampleIndex += 1) {
      view.setInt16(
        PCM_HEADER_BYTES + sampleIndex * 2,
        samples[sampleIndex],
        true
      );
    }
    return buffer;
  }

  function attachMicrophoneCapture(surface, stream) {
    if (surface.released) {
      try {
        stream.getTracks().forEach(function (track) { track.stop(); });
      } catch (_) {}
      throw directError(
        "网页麦克风启动已取消",
        "BW_COMPUTER_VOICE_DIRECT_CANCELLED",
        false
      );
    }
    var tracks = stream.getAudioTracks();
    if (tracks.length !== 1 || tracks[0].readyState !== "live") {
      try {
        stream.getTracks().forEach(function (track) { track.stop(); });
      } catch (_) {}
      throw directError(
        "网页麦克风轨道未就绪",
        "BW_COMPUTER_VOICE_MICROPHONE_NOT_READY",
        true
      );
    }
    var microphone = {
      stream: stream,
      track: tracks[0],
      source: null,
      node: null,
      silent: null,
      pending: new Float32Array(0),
      muteTimer: null,
      active: false,
    };
    surface.microphone = microphone;
    installMicrophoneTrackGuards(surface, microphone);

    var context = surface.context;
    var source = context.createMediaStreamSource(stream);
    microphone.source = source;
    var silent = context.createGain();
    silent.gain.value = 0;
    silent.connect(context.destination);
    microphone.silent = silent;

    function attachScriptProcessor() {
      if (typeof context.createScriptProcessor !== "function") {
        throw directError(
          "浏览器缺少实时麦克风 PCM 能力",
          "BW_COMPUTER_VOICE_MICROPHONE_PROCESSOR_UNAVAILABLE",
          false
        );
      }
      var processor = context.createScriptProcessor(1024, 1, 1);
      processor.onaudioprocess = function (event) {
        var input = event.inputBuffer &&
          event.inputBuffer.getChannelData(0);
        if (input) {
          appendMicrophoneChunk(
            surface,
            new Float32Array(input),
            context.sampleRate
          );
        }
      };
      microphone.node = processor;
      source.connect(processor);
      processor.connect(silent);
      return microphone;
    }

    if (
      !context.audioWorklet ||
      typeof context.audioWorklet.addModule !== "function" ||
      typeof window.AudioWorkletNode !== "function"
    ) {
      return Promise.resolve(attachScriptProcessor());
    }
    var moduleUrl = URL.createObjectURL(
      new Blob([MICROPHONE_WORKLET], { type: "text/javascript" })
    );
    return context.audioWorklet.addModule(moduleUrl).then(function () {
      URL.revokeObjectURL(moduleUrl);
      if (surface.released) {
        throw directError(
          "网页麦克风启动已取消",
          "BW_COMPUTER_VOICE_DIRECT_CANCELLED",
          false
        );
      }
      var node = new window.AudioWorkletNode(
        context,
        "bw-computer-voice-mic"
      );
      microphone.node = node;
      node.port.onmessage = function (event) {
        appendMicrophoneChunk(surface, event.data, context.sampleRate);
      };
      source.connect(node);
      node.connect(silent);
      return microphone;
    }).catch(function (error) {
      try { URL.revokeObjectURL(moduleUrl); } catch (_) {}
      if (surface.released) throw error;
      try { source.disconnect(); } catch (_) {}
      try {
        if (microphone.node) microphone.node.disconnect();
      } catch (_) {}
      microphone.node = null;
      microphone.source = context.createMediaStreamSource(stream);
      source = microphone.source;
      return attachScriptProcessor();
    });
  }

  function prepareMicrophoneFromGesture(surface) {
    if (
      !navigator.mediaDevices ||
      typeof navigator.mediaDevices.getUserMedia !== "function"
    ) {
      surface.microphoneError = directError(
        "浏览器不支持网页麦克风采集",
        "BW_COMPUTER_VOICE_MICROPHONE_UNAVAILABLE",
        false
      );
      return Promise.resolve(null);
    }
    var acquisition;
    try {
      acquisition = navigator.mediaDevices.getUserMedia({
        audio: {
          channelCount: { ideal: 1 },
          sampleRate: { ideal: PCM_SAMPLE_RATE },
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
        video: false,
      });
    } catch (error) {
      acquisition = Promise.reject(error);
    }
    return Promise.resolve(acquisition).then(function (stream) {
      return attachMicrophoneCapture(surface, stream);
    }).catch(function (error) {
      surface.microphoneError = microphoneStartError(error);
      stopSurfaceMicrophone(surface);
      return null;
    });
  }

  function discardScheduledPcm(surface) {
    if (!surface || surface.released) return;
    surface.sources.forEach(function (source) {
      try { source.stop(); } catch (_) {}
      try { source.disconnect(); } catch (_) {}
    });
    surface.sources.clear();
    surface.nextAt = 0;
  }

  function primeSurface(surface) {
    if (!surface || surface.released) return;
    try {
      var resumed = surface.context.resume();
      if (resumed && typeof resumed.catch === "function") {
        resumed.catch(function () {
          // 下一次电话点击会只重试同一个 AudioContext，不会挂断。
        });
      }
    } catch (_) {}
  }

  function clearPreparedSurface(release) {
    if (preparedTimer) {
      clearTimeout(preparedTimer);
      preparedTimer = null;
    }
    var surface = preparedSurface;
    preparedSurface = null;
    if (release) releaseSurface(surface);
    return surface;
  }

  function prepareSurfaceFromGesture() {
    clearPreparedSurface(true);
    setComputerVoiceAudioSession("play-and-record");
    preparedSurface = makeAudioSurface();
    primeSurface(preparedSurface);
    preparedSurface.microphonePromise =
      prepareMicrophoneFromGesture(preparedSurface);
    preparedTimer = setTimeout(function () {
      clearPreparedSurface(true);
    }, START_GESTURE_LEASE_TTL_MS);
  }

  function claimPreparedSurface() {
    var surface = clearPreparedSurface(false);
    if (!surface || surface.released) {
      throw directError(
        "必须由一次真实电话按钮点击启动 Windows 桥接器",
        "BW_COMPUTER_VOICE_GESTURE_REQUIRED",
        false
      );
    }
    return surface;
  }

  function playbackMessage(state) {
    return state.started
      ? "电脑客户端通话中"
      : "Windows 音频播放已恢复";
  }

  function schedulePcm(surface, samples) {
    if (!surface || surface.released) {
      throw directError(
        "PCM 播放表面已释放",
        "BW_COMPUTER_VOICE_DIRECT_PCM_STATE",
        false
      );
    }
    var context = surface.context;
    var now = Number(context.currentTime) || 0;
    var startAt = Math.max(now + 0.025, surface.nextAt || 0);
    if (startAt - now > (PCM_QUEUE_LIMIT_MS / 1000) + 0.025) {
      // 浏览器主线程卡顿后可能同步交付一批仍然合法且连续的 WSS 帧。
      // 不把旧排程伪装成实时语音，也不因此挂断：丢掉尚未播放的旧 source，
      // 从当前这一帧重新锚定播放时钟，队列始终保持在 400 ms 内。
      discardScheduledPcm(surface);
      startAt = now + 0.025;
    }
    var buffer = context.createBuffer(1, PCM_SAMPLES, PCM_SAMPLE_RATE);
    var channel = buffer.getChannelData(0);
    for (var index = 0; index < PCM_SAMPLES; index += 1) {
      channel[index] = samples[index] / 32768;
    }
    var source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(context.destination);
    surface.sources.add(source);
    source.onended = function () {
      surface.sources.delete(source);
      try { source.disconnect(); } catch (_) {}
    };
    source.start(startAt);
    surface.nextAt = startAt + (PCM_SAMPLES / PCM_SAMPLE_RATE);
  }

  function flushPendingPcm(state) {
    var surface = state.surface;
    while (surface.pending.length) {
      schedulePcm(surface, surface.pending.shift());
    }
  }

  function queueAppOutput(state, samples) {
    var surface = state.surface;
    if (surface.context.state !== "running" || state.audioBlocked) {
      var newlyBlocked = !state.audioBlocked;
      state.audioBlocked = true;
      if (newlyBlocked) {
        // AudioContext 可能在通话中被系统挂起。停止已经排程但尚未播放的
        // source，并丢弃时间轴，恢复时不能把中断期间的旧音频伪装成连续流。
        discardScheduledPcm(surface);
      }
      // 阻塞期间只保留最新 20 ms；持续 PCM 不得撑爆内存或自行挂断。
      // 用户再次真实点击后，从最新帧接回实时流，不回放失联期间的语音。
      surface.pending.length = 0;
      surface.pending.push(samples);
      if (newlyBlocked) {
        emitStatus({
          state: "audio-blocked",
          sessionId: state.sessionId,
          message: "浏览器阻止了声音；请再次点击电话按钮允许播放",
          code: "BW_COMPUTER_VOICE_AUDIO_BLOCKED",
        });
      }
      return;
    }
    schedulePcm(surface, samples);
  }

  function retryPlayback(state) {
    if (!state || state.stopped || active !== state) {
      return Promise.reject(directError(
        "电脑客户端通话已结束",
        "BW_COMPUTER_VOICE_DIRECT_CANCELLED",
        false
      ));
    }
    var resumed;
    try {
      resumed = state.surface.context.resume();
    } catch (error) {
      resumed = Promise.reject(error);
    }
    return Promise.resolve(resumed).then(function () {
      if (state.surface.context.state !== "running") {
        throw directError(
          "浏览器尚未允许 Windows 音频播放",
          "BW_COMPUTER_VOICE_AUDIO_BLOCKED",
          true
        );
      }
      state.audioBlocked = false;
      flushPendingPcm(state);
      emitStatus({
        state: state.started ? "connected" : "starting",
        sessionId: state.sessionId,
        message: playbackMessage(state),
      });
      return {
        ok: true,
        resumedAudio: true,
        sessionId: state.sessionId,
      };
    }).catch(function (error) {
      state.audioBlocked = true;
      emitStatus({
        state: "audio-blocked",
        sessionId: state.sessionId,
        message: "浏览器仍阻止播放，请再次点击电话按钮允许声音",
        code: "BW_COMPUTER_VOICE_AUDIO_BLOCKED",
      });
      throw directError(
        "浏览器仍阻止 Windows 音频播放",
        "BW_COMPUTER_VOICE_AUDIO_BLOCKED",
        true
      );
    });
  }

  function statusReasonMessage(reason) {
    var messages = {
      BW_COMPUTER_VOICE_DIRECT_LOCAL_OPT_IN_REQUIRED:
        "本机桥接服务尚未启用",
      BW_COMPUTER_VOICE_DIRECT_APP_LAUNCHER_NOT_WIRED:
        "Windows 应用启动器尚未就绪",
      BW_COMPUTER_VOICE_DIRECT_MEDIA_NOT_WIRED:
        "Windows 原生音频组件尚未就绪",
      BW_COMPUTER_VOICE_DIRECT_RENDER_ENDPOINT_UNAVAILABLE:
        "两根虚拟音频线缆尚未安装、失活或配置不匹配",
      BW_COMPUTER_VOICE_DIRECT_OUTPUT_ROUTE_UNVERIFIED:
        "尚未验证 Codex/ChatGPT 输出已固定到虚拟扬声器 B",
    };
    return reason ? (messages[reason] || reason) : "";
  }

  function statusMessage(state, reason) {
    var messages = {
      "starting-service": "正在启动 Windows 桥接服务…",
      "starting-app": "正在启动 Windows 电脑客户端…",
      "waiting-app-ready": "正在等待 Windows 电脑客户端就绪…",
      "starting-capture": "正在启动受限的 Windows 音频采集…",
      ready: "Windows 桥接器已就绪",
      active: "Windows 已开始受限音频桥接",
      idle: "Windows 桥接器空闲",
      stopped: "Windows 桥接器已停止",
      busy: "Windows 桥接器正被其他会话使用",
      disabled: "Windows 桥接器未启用",
      unavailable: "Windows 桥接器暂不可用",
      error: "Windows 桥接器报告错误",
    };
    var prefix = messages[state] || "Windows 桥接器状态";
    var detail = statusReasonMessage(reason);
    return prefix + (detail ? "：" + detail : "");
  }

  function contextSchemaError(message, code) {
    return directError(
      message,
      code || "BW_COMPUTER_VOICE_CONTEXT_SCHEMA",
      false
    );
  }

  function normalizeOutgoingContextEvent(value) {
    if (!plainObject(value)) {
      throw contextSchemaError("Reader outgoing event 必须是对象");
    }
    ["v", "seq", "type", "ts", "id"].forEach(function (key) {
      if (!Object.prototype.hasOwnProperty.call(value, key)) {
        throw contextSchemaError("Reader outgoing event 缺少字段 " + key);
      }
    });
    if (
      value.v !== 1 ||
      !Number.isSafeInteger(value.seq) ||
      value.seq <= 0 ||
      !CONTEXT_EVENT_TYPES[value.type] ||
      !Number.isFinite(value.ts) ||
      Math.floor(value.ts) !== value.ts ||
      typeof value.id !== "string" ||
      !/^[0-9a-f]{16}$/.test(value.id)
    ) {
      throw contextSchemaError("Reader outgoing event 核心字段无效");
    }
    // Return the original object. Windows receives the journal payload exactly
    // as authored; this layer validates the core but does not reshape it.
    return value;
  }

  function normalizeOutgoingJournal(value) {
    exactObject(
      value,
      [
        "ok",
        "contract",
        "cursor",
        "head",
        "events",
        "gap",
        "note",
        "waited",
      ],
      ["waitDenied"],
      "Reader outgoing journal"
    );
    if (
      value.ok !== true ||
      value.contract !== OUTGOING_CONTEXT_CONTRACT ||
      !Number.isSafeInteger(value.cursor) ||
      value.cursor < 0 ||
      !Number.isSafeInteger(value.head) ||
      value.head < 0 ||
      !Array.isArray(value.events) ||
      typeof value.gap !== "boolean" ||
      typeof value.note !== "string" ||
      !Number.isFinite(value.waited) ||
      value.waited < 0 ||
      (
        Object.prototype.hasOwnProperty.call(value, "waitDenied") &&
        typeof value.waitDenied !== "boolean"
      )
    ) {
      throw contextSchemaError("Reader outgoing journal 字段无效");
    }
    var previous = 0;
    value.events.forEach(function (event) {
      normalizeOutgoingContextEvent(event);
      if (previous && event.seq !== previous + 1) {
        throw contextSchemaError("Reader outgoing journal 事件序号不连续");
      }
      if (event.seq > value.cursor) {
        throw contextSchemaError("Reader outgoing journal event 超过 tail");
      }
      previous = event.seq;
    });
    return value;
  }

  function contextPumpAlive(state, pump) {
    return !!(
      state &&
      pump &&
      !pump.stopped &&
      state.contextPump === pump &&
      state.started &&
      !state.stopped &&
      active === state &&
      pump.generation === contextPumpGeneration
    );
  }

  function stopContextPump(state) {
    var pump = state && state.contextPump;
    if (!pump || pump.stopped) return;
    pump.stopped = true;
    if (pump.timer) {
      clearTimeout(pump.timer);
      pump.timer = null;
    }
    if (pump.controller) {
      try { pump.controller.abort(); } catch (_) {}
      pump.controller = null;
    }
    pump.queue.length = 0;
    pump.pendingEvent = null;
  }

  function warnAndStopContextPump(state, pump, error) {
    if (!contextPumpAlive(state, pump)) return;
    stopContextPump(state);
    emitStatus({
      state: "warning",
      sessionId: state.sessionId,
      message: error && error.message ||
        "Reader 上下文桥接已停止；音频通话继续",
      code: error && error.code ||
        "BW_COMPUTER_VOICE_CONTEXT_FAILED",
    });
  }

  function scheduleContextPump(state, pump, delay) {
    if (!contextPumpAlive(state, pump) || pump.timer) return;
    pump.timer = setTimeout(function () {
      pump.timer = null;
      runContextPump(state, pump);
    }, delay);
  }

  function outgoingJournalFetch(state, pump, since, limit, wait) {
    var fetcher = window.__bwReaderFetch;
    if (typeof fetcher !== "function" && typeof window.fetch === "function") {
      fetcher = window.fetch.bind(window);
    }
    if (typeof fetcher !== "function") {
      return Promise.reject(contextSchemaError(
        "Reader 页面缺少 journal fetch 能力",
        "BW_COMPUTER_VOICE_CONTEXT_FETCH_UNAVAILABLE"
      ));
    }
    var AbortControllerCtor = window.AbortController;
    if (typeof AbortControllerCtor !== "function") {
      return Promise.reject(contextSchemaError(
        "Reader 页面缺少 AbortController",
        "BW_COMPUTER_VOICE_CONTEXT_ABORT_UNAVAILABLE"
      ));
    }
    var controller = new AbortControllerCtor();
    pump.controller = controller;
    var url = "/pdf/api/outgoing/journal?since=" +
      encodeURIComponent(String(since)) +
      "&limit=" + encodeURIComponent(String(limit)) +
      "&wait=" + encodeURIComponent(String(wait));
    var transport;
    try {
      transport = fetcher(url, {
        method: "GET",
        credentials: "same-origin",
        cache: "no-store",
        signal: controller.signal,
      });
    } catch (error) {
      transport = Promise.reject(error);
    }
    return Promise.resolve(transport).catch(function (error) {
      if (
        controller.signal.aborted ||
        (error && error.name === "AbortError")
      ) {
        throw directError(
          "Reader context fetch 已取消",
          "BW_COMPUTER_VOICE_CONTEXT_CANCELLED",
          false
        );
      }
      throw directError(
        "Reader outgoing journal 网络请求失败",
        "BW_COMPUTER_VOICE_CONTEXT_FETCH",
        true
      );
    }).then(function (response) {
      if (!contextPumpAlive(state, pump)) {
        throw directError(
          "Reader context pump 已停止",
          "BW_COMPUTER_VOICE_CONTEXT_CANCELLED",
          false
        );
      }
      if (!response || response.ok !== true ||
          typeof response.json !== "function") {
        throw directError(
          "Reader outgoing journal 暂不可用",
          "BW_COMPUTER_VOICE_CONTEXT_FETCH",
          true
        );
      }
      return Promise.resolve(response.json()).catch(function () {
        throw contextSchemaError("Reader outgoing journal JSON 无法解析");
      });
    }).then(normalizeOutgoingJournal).finally(function () {
      if (pump.controller === controller) pump.controller = null;
    });
  }

  function normalizeContextAck(value, state, event) {
    exactObject(
      value,
      ["sessionId", "eventId", "seq", "outcome"],
      [],
      "CONTEXT 响应"
    );
    if (
      safeId(value.sessionId, "sessionId") !== state.sessionId ||
      value.eventId !== event.id ||
      value.seq !== event.seq ||
      (value.outcome !== "accepted" && value.outcome !== "duplicate")
    ) {
      throw contextSchemaError(
        "Windows CONTEXT 回执无效",
        "BW_COMPUTER_VOICE_CONTEXT_ACK"
      );
    }
    return value;
  }

  function sendPendingContextEvent(state, pump) {
    var event = pump.pendingEvent;
    return state.channel.request("context", {
      sessionId: state.sessionId,
      contextContract: OUTGOING_CONTEXT_CONTRACT,
      event: event,
    }).then(function (value) {
      normalizeContextAck(value, state, event);
      if (!contextPumpAlive(state, pump) || pump.pendingEvent !== event) return;
      lastContextAckCursor = event.seq;
      lastContextResumeCursor = event.seq;
      pump.cursor = event.seq;
      pump.pendingEvent = null;
    });
  }

  function validateSteadyEvents(events, cursor) {
    var expected = cursor + 1;
    events.forEach(function (event) {
      if (event.seq !== expected) {
        throw contextSchemaError(
          "Reader outgoing journal 出现未声明的序号缺口",
          "BW_COMPUTER_VOICE_CONTEXT_GAP"
        );
      }
      expected += 1;
    });
  }

  function finishContextBootstrap(state, pump, journal) {
    // A bootstrap gap is expected when the retained journal already starts
    // after seq=1. Start only at the most recent full page context so older
    // focus/drawing state cannot be replayed as current.
    // Freeze the scan at the tail observed by the first probe. The journal may
    // append while the bounded second request is in flight; its outer cursor
    // and returned rows are therefore not a safe bootstrap boundary.
    var bootstrapEvents = journal.events.filter(function (event) {
      return event.seq <= pump.bootstrapTail;
    });
    var startIndex = -1;
    for (var index = bootstrapEvents.length - 1; index >= 0; index -= 1) {
      if (bootstrapEvents[index].type === "page.context") {
        startIndex = index;
        break;
      }
    }
    pump.bootstrapDone = true;
    if (startIndex < 0) {
      // All rows up to the frozen bootstrap tail were inspected and none could
      // establish a current page. Old focus/drawing events are deliberately
      // discarded. Events appended after that tail remain visible to the first
      // live poll. This is a resume baseline, not a Windows ACK.
      pump.cursor = pump.bootstrapTail;
      lastContextResumeCursor = pump.bootstrapTail;
      return;
    }
    pump.cursor = bootstrapEvents[startIndex].seq - 1;
    pump.queue = bootstrapEvents.slice(startIndex);
  }

  function runContextPump(state, pump) {
    if (!contextPumpAlive(state, pump) || pump.running) return;
    pump.running = true;
    var work;
    if (pump.pendingEvent) {
      work = sendPendingContextEvent(state, pump);
    } else if (pump.queue.length) {
      pump.pendingEvent = pump.queue.shift();
      work = sendPendingContextEvent(state, pump);
    } else if (!pump.bootstrapDone && pump.bootstrapTail === null) {
      work = outgoingJournalFetch(state, pump, 0, 1, 0).then(function (journal) {
        if (!contextPumpAlive(state, pump)) return;
        pump.bootstrapTail = journal.cursor;
      });
    } else if (!pump.bootstrapDone) {
      var since = Math.max(0, pump.bootstrapTail - CONTEXT_BOOTSTRAP_LIMIT);
      work = outgoingJournalFetch(
        state,
        pump,
        since,
        CONTEXT_BOOTSTRAP_LIMIT,
        0
      ).then(function (journal) {
        if (!contextPumpAlive(state, pump)) return;
        finishContextBootstrap(state, pump, journal);
      });
    } else {
      work = outgoingJournalFetch(
        state,
        pump,
        pump.cursor,
        CONTEXT_LIVE_LIMIT,
        CONTEXT_LIVE_WAIT_S
      ).then(function (journal) {
        if (!contextPumpAlive(state, pump)) return;
        if (journal.gap) {
          throw contextSchemaError(
            "Reader outgoing journal 已越过保留窗口；上下文桥接已停止",
            "BW_COMPUTER_VOICE_CONTEXT_GAP"
          );
        }
        validateSteadyEvents(journal.events, pump.cursor);
        pump.queue = journal.events.slice();
        if (!pump.queue.length && journal.waitDenied === true) {
          pump.waitDenied = true;
        }
      });
    }
    Promise.resolve(work).then(function () {
      if (!contextPumpAlive(state, pump)) return;
      var delay = pump.waitDenied
        ? CONTEXT_WAIT_DENIED_RETRY_MS
        : 0;
      pump.waitDenied = false;
      scheduleContextPump(state, pump, delay);
    }).catch(function (error) {
      if (!contextPumpAlive(state, pump)) return;
      if (error && error.retryable === true) {
        scheduleContextPump(state, pump, CONTEXT_RETRY_MS);
        return;
      }
      warnAndStopContextPump(state, pump, error);
    }).finally(function () {
      pump.running = false;
    });
  }

  function startContextPump(state) {
    stopContextPump(state);
    contextPumpGeneration += 1;
    var pump = {
      generation: contextPumpGeneration,
      stopped: false,
      running: false,
      timer: null,
      controller: null,
      bootstrapDone: lastContextResumeCursor !== null,
      bootstrapTail: null,
      cursor: lastContextResumeCursor === null ? 0 : lastContextResumeCursor,
      queue: [],
      pendingEvent: null,
      waitDenied: false,
    };
    state.contextPump = pump;
    runContextPump(state, pump);
  }

  function clearHeartbeat(state) {
    if (!state) return;
    if (state.heartbeatTimer) {
      clearTimeout(state.heartbeatTimer);
      state.heartbeatTimer = null;
    }
    state.heartbeatInFlight = false;
  }

  function scheduleHeartbeat(state) {
    if (
      !state ||
      state.stopped ||
      !state.started ||
      active !== state ||
      !state.channel ||
      state.channel.closed
    ) {
      clearHeartbeat(state);
      return;
    }
    if (state.heartbeatTimer || state.heartbeatInFlight) return;
    state.heartbeatTimer = setTimeout(function () {
      state.heartbeatTimer = null;
      if (
        state.stopped ||
        !state.started ||
        active !== state ||
        !state.channel ||
        state.channel.closed
      ) {
        clearHeartbeat(state);
        return;
      }
      state.heartbeatInFlight = true;
      state.heartbeatSequence += 1;
      var sequence = state.heartbeatSequence;
      state.channel.request("heartbeat", {
        sessionId: state.sessionId,
        sequence: sequence,
      }).then(function (value) {
        exactObject(
          value,
          ["sessionId", "sequence", "state"],
          [],
          "HEARTBEAT 响应"
        );
        if (
          safeId(value.sessionId, "sessionId") !== state.sessionId ||
          value.sequence !== sequence ||
          value.state !== "active"
        ) {
          throw directError(
            "Windows 桥接器 HEARTBEAT 回执无效",
            "BW_COMPUTER_VOICE_DIRECT_HEARTBEAT",
            false
          );
        }
        state.heartbeatInFlight = false;
        scheduleHeartbeat(state);
      }).catch(function (error) {
        state.heartbeatInFlight = false;
        if (active === state && !state.stopped) {
          failActive(state, error, true);
        }
      });
    }, HEARTBEAT_INTERVAL_MS);
  }

  function failActive(state, error, emit) {
    if (!state || state.failed || state.cancelled) return;
    state.failed = true;
    state.stopped = true;
    dialPending = false;
    state.uplinkActive = false;
    lastClientFailure = {
      code: error && error.code ||
        "BW_COMPUTER_VOICE_DIRECT_START_FAILED",
      message: error && error.message ||
        "Windows 桥接器启动失败",
      at: new Date().toISOString(),
    };
    clearHeartbeat(state);
    stopContextPump(state);
    if (active === state) active = null;
    if (state.channel) state.channel.close();
    releaseSurface(state.surface);
    if (emit !== false) {
      emitStatus({
        state: "failed",
        sessionId: state.sessionId || undefined,
        message: error && error.message || "Windows 桥接器启动失败",
        code: error && error.code || "BW_COMPUTER_VOICE_DIRECT_START_FAILED",
      });
    }
  }

  function handlePcmFrame(state, buffer) {
    if (!state.acceptPcm || state.stopped || active !== state) {
      throw directError(
        "Windows 在 START 确认前发送了 PCM",
        "BW_COMPUTER_VOICE_DIRECT_PCM_UNEXPECTED",
        false
      );
    }
    if (buffer.byteLength !== PCM_FRAME_BYTES) {
      throw directError(
        "Windows PCM 帧长度无效",
        "BW_COMPUTER_VOICE_DIRECT_PCM_FRAME",
        false
      );
    }
    var view = new DataView(buffer);
    if (
      view.getUint8(0) !== 0x42 ||
      view.getUint8(1) !== 0x57 ||
      view.getUint8(2) !== 0x43 ||
      view.getUint8(3) !== 0x56 ||
      view.getUint8(4) !== 1
    ) {
      throw directError(
        "Windows PCM magic 或版本无效",
        "BW_COMPUTER_VOICE_DIRECT_PCM_MAGIC",
        false
      );
    }
    var track = view.getUint8(5);
    if (track !== 1 || view.getUint16(6, true) !== 0) {
      throw directError(
        "Windows PCM track 或 flags 无效",
        "BW_COMPUTER_VOICE_DIRECT_PCM_TRACK",
        false
      );
    }
    for (var byteIndex = 0; byteIndex < 16; byteIndex += 1) {
      if (view.getUint8(8 + byteIndex) !== state.sessionBytes[byteIndex]) {
        throw directError(
          "Windows PCM session 与当前通话不匹配",
          "BW_COMPUTER_VOICE_DIRECT_PCM_SESSION",
          false
        );
      }
    }
    var stream = state.pcm[track];
    var sequence = view.getUint32(24, true);
    if (sequence !== stream.nextSequence) {
      throw directError(
        "Windows PCM sequence 不连续",
        "BW_COMPUTER_VOICE_DIRECT_PCM_SEQUENCE",
        false
      );
    }
    var timestampLow = view.getUint32(28, true);
    var timestampHigh = view.getUint32(32, true);
    if (
      stream.seen &&
      (
        timestampHigh < stream.timestampHigh ||
        (timestampHigh === stream.timestampHigh &&
          timestampLow <= stream.timestampLow)
      )
    ) {
      throw directError(
        "Windows PCM timestamp 未严格递增",
        "BW_COMPUTER_VOICE_DIRECT_PCM_TIMESTAMP",
        false
      );
    }
    stream.seen = true;
    stream.nextSequence += 1;
    stream.timestampLow = timestampLow;
    stream.timestampHigh = timestampHigh;

    if (track === 1) {
      var samples = new Int16Array(PCM_SAMPLES);
      for (var sampleIndex = 0; sampleIndex < PCM_SAMPLES; sampleIndex += 1) {
        samples[sampleIndex] = view.getInt16(
          PCM_HEADER_BYTES + sampleIndex * 2,
          true
        );
      }
      queueAppOutput(state, samples);
    }
  }

  function startFromUserGesture(options) {
    options = options || {};
    if (active) {
      return Promise.reject(directError(
        "电脑客户端通话已经启动",
        "BW_COMPUTER_VOICE_ALREADY_ACTIVE",
        false
      ));
    }
    var surface;
    var session;
    try {
      surface = claimPreparedSurface();
      session = randomSession();
    } catch (error) {
      return Promise.reject(error);
    }
    dialPending = true;
    var state = {
      channel: null,
      surface: surface,
      sessionId: session.id,
      sessionBytes: session.bytes,
      cancelled: false,
      stopped: false,
      failed: false,
      started: false,
      acceptPcm: false,
      audioBlocked: false,
      heartbeatTimer: null,
      heartbeatInFlight: false,
      heartbeatSequence: 0,
      uplinkActive: false,
      uplinkSequence: 0,
      uplinkTimestampBase: Math.floor(Date.now() * 1000),
      contextPump: null,
      pcm: {
        1: { nextSequence: 0, seen: false, timestampLow: 0, timestampHigh: 0 },
      },
    };
    surface.ownerState = state;
    active = state;
    var availabilityClosed = cancelAvailabilityForStart();
    emitStatus({
      state: "checking",
      sessionId: state.sessionId,
      message: "正在直连 Windows 桥接器…",
    });

    return availabilityClosed.then(function () {
      return surface.microphonePromise;
    }).then(function () {
      if (state.stopped || active !== state) {
        throw directError(
          "Windows 桥接启动已取消",
          "BW_COMPUTER_VOICE_DIRECT_CANCELLED",
          false
        );
      }
      if (surface.microphoneError) throw surface.microphoneError;
      if (
        !surface.microphone ||
        !surface.microphone.track ||
        surface.microphone.track.readyState !== "live"
      ) {
        throw directError(
          "网页麦克风没有就绪",
          "BW_COMPUTER_VOICE_MICROPHONE_NOT_READY",
          true
        );
      }
      return openDirect({
        onStatus: function (status) {
          if (active !== state || state.stopped) return;
          emitStatus({
            state: status.state,
            sessionId: state.sessionId,
            message: statusMessage(status.state, status.reason),
          });
        },
        onFatal: function (error) {
          if (active === state && !state.stopped) failActive(state, error, true);
        },
        onBinary: function (buffer) {
          handlePcmFrame(state, buffer);
        },
      }, function (channel) {
        state.channel = channel;
      });
    }).then(function (channel) {
      if (state.stopped || active !== state) {
        channel.close();
        throw directError(
          "Windows 桥接启动已取消",
          "BW_COMPUTER_VOICE_DIRECT_CANCELLED",
          false
        );
      }
      state.channel = channel;
      // Windows capture pump may enqueue its first frame before the START
      // result task is delivered. This gate opens immediately before the one
      // authorized START send, never during HELLO/STATUS.
      state.acceptPcm = true;
      emitStatus({
        state: "starting",
        sessionId: state.sessionId,
        message: "正在请求 Windows 启动电脑客户端…",
      });
      return channel.request("start", {
        sessionId: state.sessionId,
      }, START_TIMEOUT_MS);
    }).then(function (started) {
      exactObject(started, ["sessionId", "state", "media"], [], "START 响应");
      if (
        safeId(started.sessionId, "sessionId") !== state.sessionId ||
        started.state !== "active"
      ) {
        throw directError(
          "Windows 桥接器未确认本次 START",
          "BW_COMPUTER_VOICE_DIRECT_START",
          false
        );
      }
      exactObject(
        started.media,
        ["hostReady", "captureActive"],
        [],
        "START media"
      );
      if (
        started.media.hostReady !== true ||
        started.media.captureActive !== true
      ) {
        throw directError(
          "Windows 音频宿主未就绪",
          "BW_COMPUTER_VOICE_DIRECT_MEDIA",
          false
        );
      }
      state.started = true;
      dialPending = false;
      lastClientFailure = null;
      state.uplinkActive = true;
      state.surface.microphone.active = true;
      scheduleHeartbeat(state);
      if (state.stopped || active !== state) {
        throw directError(
          "Windows 桥接启动已取消",
          "BW_COMPUTER_VOICE_DIRECT_CANCELLED",
          false
        );
      }
      startContextPump(state);
      if (state.surface.context.state !== "running") {
        state.audioBlocked = true;
        emitStatus({
          state: "audio-blocked",
          sessionId: state.sessionId,
          message: "浏览器阻止了声音；请再次点击电话按钮允许播放",
          code: "BW_COMPUTER_VOICE_AUDIO_BLOCKED",
        });
      } else {
        emitStatus({
          state: "connected",
          sessionId: state.sessionId,
          message: "电脑客户端通话中",
        });
      }
      return {
        ok: true,
        sessionId: state.sessionId,
      };
    }).catch(function (error) {
      failActive(state, error, true);
      throw error;
    });
  }

  function stop(reason) {
    var state = active;
    active = null;
    dialPending = false;
    if (!state) {
      emitStatus({ state: "stopped", message: "电脑客户端已停止" });
      return Promise.resolve({ ok: true, state: "stopped" });
    }
    state.cancelled = true;
    state.stopped = true;
    state.uplinkActive = false;
    stopContextPump(state);
    stopSurfaceMicrophone(state.surface);
    clearHeartbeat(state);
    if (!state.started) {
      // START 尚未确认时不能把 STOP 排在同一条 WSS 后面等待：Windows
      // 服务端正在串行处理 START，排队 STOP 只会让应用、采音和快捷键继续
      // 启动。立即关闭连接会取消所有在途请求，并让远端会话租约 fail closed。
      if (state.channel) state.channel.close();
      releaseSurface(state.surface);
      emitStatus({
        state: "stopped",
        message: "电脑桥接启动已取消；若 Codex Voice 已亮起，请在 Windows 退出",
      });
      return Promise.resolve({ ok: true, state: "stopped" });
    }
    return Promise.resolve().then(function () {
      if (!state.channel || state.channel.closed) return null;
      return state.channel.request("stop", {
        sessionId: state.sessionId,
      }).then(function (value) {
        exactObject(value, ["sessionId", "state"], [], "STOP 响应");
        if (
          safeId(value.sessionId, "sessionId") !== state.sessionId ||
          value.state !== "idle"
        ) {
          throw directError(
            "Windows 桥接器 STOP 回执无效",
            "BW_COMPUTER_VOICE_DIRECT_STOP",
            false
          );
        }
        return value;
      }).catch(function () {
        // 本地释放不依赖远端回执；断线端由会话租约 fail closed。
        return null;
      });
    }).then(function () {
      if (state.channel) state.channel.close();
      releaseSurface(state.surface);
      emitStatus({
        state: "stopped",
        message: "电脑桥接已停止；请确认 Windows 的 Codex Voice 已退出",
      });
      return { ok: true, state: "stopped" };
    });
  }

  function phoneButtonFromEvent(event) {
    var target = event && event.target;
    while (target) {
      if (target.id === "asst-call" || target.id === "vc-top-call") {
        return registeredPhoneButtons.has(target) ? target : null;
      }
      target = target.parentNode;
    }
    return null;
  }

  function registerPhoneButton(button) {
    if (
      !button ||
      button.nodeType !== 1 ||
      String(button.tagName || "").toUpperCase() !== "BUTTON" ||
      (button.id !== "asst-call" && button.id !== "vc-top-call") ||
      button.type !== "button" ||
      button.ownerDocument !== window.document ||
      button.isConnected !== true
    ) {
      return false;
    }
    registeredPhoneButtons.add(button);
    return true;
  }

  function installGestureCapture() {
    if (!document || typeof document.addEventListener !== "function") return;
    document.addEventListener("click", function (event) {
      if (!phoneButtonFromEvent(event)) return;
      if (event.isTrusted !== true) return;
      try {
        if (
          !RC.voicecall ||
          typeof RC.voicecall.canCaptureComputerVoiceGesture !== "function" ||
          RC.voicecall.canCaptureComputerVoiceGesture() !== true
        ) {
          return;
        }
      } catch (_) {
        return;
      }
      if (active && active.audioBlocked) {
        // 此次点击只恢复被浏览器拦截的同一条音频，不让上层把它当挂断。
        try { event.preventDefault(); } catch (_) {}
        try { event.stopImmediatePropagation(); } catch (_) {}
        retryPlayback(active).catch(function () {});
        return;
      }
      if (!active && dialPending) {
        clearPreparedSurface(true);
        return;
      }
      if (!active && selectedEngineKnown && computerVoiceSelected) {
        prepareSurfaceFromGesture();
      }
    }, true);
  }

  function reserveSelectedEngineUpdate() {
    selectedEngineRevision += 1;
    return selectedEngineRevision;
  }

  function beginSelectedEngineUpdate() {
    var revision = reserveSelectedEngineUpdate();
    // A settings write is not authoritative until the server ACKs it. Fence
    // both the old and proposed values so a phone click during the POST cannot
    // acquire a microphone lease for either one.
    selectedEngineKnown = false;
    computerVoiceSelected = false;
    clearPreparedSurface(true);
    return revision;
  }

  function isSelectedEngineRevisionCurrent(revision) {
    return Number.isSafeInteger(revision) &&
      revision >= 1 &&
      revision === selectedEngineRevision;
  }

  function setSelectedEngine(engine, revision) {
    if (revision == null) {
      revision = reserveSelectedEngineUpdate();
    }
    if (
      !Number.isSafeInteger(revision) ||
      revision < 1 ||
      revision < selectedEngineRevision
    ) {
      return computerVoiceSelected;
    }
    selectedEngineRevision = revision;
    selectedEngineKnown = true;
    computerVoiceSelected = engine === "computer_client";
    if (!computerVoiceSelected) clearPreparedSurface(true);
    return computerVoiceSelected;
  }

  function setDialPending(value) {
    dialPending = value === true;
    if (!dialPending && !active && !computerVoiceSelected) {
      clearPreparedSurface(true);
    }
    return dialPending;
  }

  function cancelPreparedGesture() {
    clearPreparedSurface(true);
  }

  function abortForPageExit() {
    clearPreparedSurface(true);
    var state = active;
    active = null;
    dialPending = false;
    if (!state) return;
    state.cancelled = true;
    state.stopped = true;
    state.uplinkActive = false;
    stopContextPump(state);
    clearHeartbeat(state);
    stopSurfaceMicrophone(state.surface);
    if (state.channel) state.channel.close();
    releaseSurface(state.surface);
  }

  function mountSettings(container) {
    if (!container || container.querySelector(".rc-computer-voice-settings")) return;
    var root = document.createElement("div");
    root.className = "rc-computer-voice-settings";
    root.innerHTML =
      '<div class="ams-tdef" data-role="status">正在读取 Windows 直连状态…</div>' +
      '<div class="ams-row" style="margin-top:7px">' +
      '<button type="button" class="ams-btn" data-role="refresh">刷新直连状态</button>' +
      '</div>' +
      '<div class="ams-tdef" data-role="detail" style="margin-top:6px"></div>' +
      '<div class="ams-tdef" data-role="error" ' +
      'style="display:none;margin-top:7px;white-space:pre-wrap;' +
      'user-select:text;color:#ffb4a8"></div>';
    container.appendChild(root);
    var status = root.querySelector('[data-role="status"]');
    var detail = root.querySelector('[data-role="detail"]');
    var errorDetail = root.querySelector('[data-role="error"]');

    function render(value) {
      if (
        value.state === "ready" ||
        (value.state === "idle" && value.status && value.status.ready === true)
      ) {
        status.textContent =
          "● Windows 桥接器已就绪；只有点击电话按钮才会启动。";
      } else if (value.state === "offline") {
        status.textContent = "○ Windows 桥接器离线或电脑正在睡眠。";
      } else {
        status.textContent = "○ Windows 桥接器：" +
          (statusReasonMessage(value.reason) || value.state || "未就绪");
      }
      detail.textContent =
        "固定 Tailnet 直连，无需配对或填写地址；" +
        "桥接器不会创建或安装虚拟设备，A/B 必须是 Windows 已有的两根独立虚拟音频线；" +
        "电话按钮才会申请当前网页麦克风并送入 Windows 虚拟麦克风；" +
        "Codex 输出固定到独立虚拟扬声器后按进程树回传。" +
        "选择模型或刷新状态不会启动应用或采音。" +
        "挂断只保证停止桥接，Codex Voice 需在 Windows 确认退出。";
      var remoteError = value.status && value.status.lastError;
      if (remoteError) {
        errorDetail.style.display = "";
        errorDetail.textContent =
          "最近 Windows 错误（可选择复制）\n" +
          remoteError.code + " · " + remoteError.stage +
          (remoteError.hresult ? " · " + remoteError.hresult : "") +
          "\n" + remoteError.failureId + " · " + remoteError.atUtc;
      } else if (lastClientFailure) {
        errorDetail.style.display = "";
        errorDetail.textContent =
          "最近浏览器错误（可选择复制）\n" +
          lastClientFailure.code + "\n" +
          lastClientFailure.message + "\n" +
          lastClientFailure.at;
      } else {
        errorDetail.style.display = "none";
        errorDetail.textContent = "";
      }
    }

    function refresh() {
      status.textContent = "正在直连 Windows 桥接器读取状态…";
      return availability().then(render).catch(function (error) {
        status.textContent = "直连状态读取失败：" +
          (error.message || "未知错误");
        detail.textContent = "未发送 START。";
      });
    }

    root.querySelector('[data-role="refresh"]').addEventListener("click", refresh);
    refresh();
  }

  installGestureCapture();
  if (window && typeof window.addEventListener === "function") {
    window.addEventListener("pagehide", abortForPageExit);
  }

  RC.computerVoice = Object.freeze({
    contract: BRIDGE_CONTRACT,
    directContract: DIRECT_CONTRACT,
    availability: availability,
    reserveSelectedEngineUpdate: reserveSelectedEngineUpdate,
    beginSelectedEngineUpdate: beginSelectedEngineUpdate,
    isSelectedEngineRevisionCurrent: isSelectedEngineRevisionCurrent,
    setSelectedEngine: setSelectedEngine,
    setDialPending: setDialPending,
    cancelPreparedGesture: cancelPreparedGesture,
    registerPhoneButton: registerPhoneButton,
    startFromUserGesture: startFromUserGesture,
    stop: stop,
    isActive: function () { return !!active; },
    onStatus: function (listener) {
      if (typeof listener !== "function") return function () {};
      statusListeners.push(listener);
      return function () {
        statusListeners = statusListeners.filter(function (item) {
          return item !== listener;
        });
      };
    },
    mountSettings: mountSettings,
  });
})();
