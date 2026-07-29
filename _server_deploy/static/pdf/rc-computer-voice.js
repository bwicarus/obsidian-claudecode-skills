/* rc-computer-voice.js — Reader/PWA ↔ Windows 电脑语音直连入口。
 *
 * 控制与固定帧 PCM 音频只走用户配置的 tailnet WSS，不经过 Pi。选择模型和
 * STATUS 都不会启动 Windows 应用或采音；只有电话按钮的一次真实用户操作
 * 会发送 START。长期身份是 IndexedDB 中不可导出的 ECDSA P-256 私钥，
 * 页面不保存 bearer token，配对码只由 Windows EXE 生成。
 */
(function () {
  "use strict";

  var RC = window.RC = window.RC || {};
  var BRIDGE_CONTRACT = "reader-computer-voice-bridge/1";
  var DIRECT_CONTRACT = "reader-computer-voice-direct/1";
  var AUTH_CONTRACT = "reader-computer-voice-auth/1";
  var DB_NAME = "bw-reader-computer-voice";
  var DB_STORE = "identity";
  var DB_KEY = "primary";
  var DB_VERSION = 1;
  var MAX_MESSAGE_BYTES = 65536;
  var PCM_FRAME_BYTES = 1956;
  var PCM_HEADER_BYTES = 36;
  var PCM_SAMPLES = 960;
  var PCM_SAMPLE_RATE = 48000;
  var PCM_QUEUE_LIMIT_MS = 400;
  var PCM_QUEUE_LIMIT_FRAMES = PCM_QUEUE_LIMIT_MS / 20;
  var MAX_PENDING = 16;
  var OPEN_TIMEOUT_MS = 6000;
  var REQUEST_TIMEOUT_MS = 7000;
  var START_TIMEOUT_MS = 45000;
  var HEARTBEAT_INTERVAL_MS = 5000;
  var HEARTBEAT_TIMEOUT_MS = 15000;
  var PREPARED_SURFACE_TTL_MS = 15000;
  var DIRECT_ENDPOINT =
    "wss://bwicarus-2.taile44d0c.ts.net/reader-computer-voice/v1";
  var PAIR_CODE_RE = /^[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{10}$/;
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
  var dbPromise = null;

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

  function safeBase64Url(value, label, minimum, maximum) {
    var text = safeText(value, label, maximum, false);
    if (
      text.length < minimum ||
      !/^[A-Za-z0-9_-]+$/.test(text) ||
      text.indexOf("=") >= 0
    ) {
      throw directError(
        label + " 格式无效",
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

  function normalizePairingCode(value) {
    var code = String(value || "").trim().toUpperCase()
      .replace(/[\s-]+/g, "");
    if (!PAIR_CODE_RE.test(code)) {
      throw directError(
        "请输入 Windows EXE 显示的 10 位一次性配对码",
        "BW_COMPUTER_VOICE_DIRECT_PAIR_CODE",
        false
      );
    }
    return code;
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

  function openIdentityDb() {
    if (dbPromise) return dbPromise;
    if (!window.indexedDB || typeof window.indexedDB.open !== "function") {
      return Promise.reject(directError(
        "浏览器不支持安全身份存储（IndexedDB）",
        "BW_COMPUTER_VOICE_DIRECT_STORAGE",
        false
      ));
    }
    dbPromise = new Promise(function (resolve, reject) {
      var request;
      try {
        request = window.indexedDB.open(DB_NAME, DB_VERSION);
      } catch (error) {
        reject(directError(
          "无法打开安全身份存储",
          "BW_COMPUTER_VOICE_DIRECT_STORAGE",
          false
        ));
        return;
      }
      request.onupgradeneeded = function () {
        var db = request.result;
        if (!db.objectStoreNames.contains(DB_STORE)) {
          db.createObjectStore(DB_STORE);
        }
      };
      request.onsuccess = function () { resolve(request.result); };
      request.onerror = function () {
        dbPromise = null;
        reject(directError(
          "无法打开安全身份存储",
          "BW_COMPUTER_VOICE_DIRECT_STORAGE",
          false
        ));
      };
      request.onblocked = request.onerror;
    });
    return dbPromise;
  }

  function idbGet() {
    return openIdentityDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var request;
        try {
          request = db.transaction(DB_STORE, "readonly")
            .objectStore(DB_STORE).get(DB_KEY);
        } catch (_) {
          reject(directError(
            "读取安全身份失败",
            "BW_COMPUTER_VOICE_DIRECT_STORAGE",
            false
          ));
          return;
        }
        request.onsuccess = function () { resolve(request.result || null); };
        request.onerror = function () {
          reject(directError(
            "读取安全身份失败",
            "BW_COMPUTER_VOICE_DIRECT_STORAGE",
            false
          ));
        };
      });
    });
  }

  function idbPut(record) {
    return openIdentityDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var request;
        try {
          request = db.transaction(DB_STORE, "readwrite")
            .objectStore(DB_STORE).put(record, DB_KEY);
        } catch (_) {
          reject(directError(
            "保存不可导出身份密钥失败",
            "BW_COMPUTER_VOICE_DIRECT_STORAGE",
            false
          ));
          return;
        }
        request.onsuccess = function () { resolve(record); };
        request.onerror = function () {
          reject(directError(
            "保存不可导出身份密钥失败",
            "BW_COMPUTER_VOICE_DIRECT_STORAGE",
            false
          ));
        };
      });
    });
  }

  function idbDelete() {
    return openIdentityDb().then(function (db) {
      return new Promise(function (resolve, reject) {
        var request;
        try {
          request = db.transaction(DB_STORE, "readwrite")
            .objectStore(DB_STORE).delete(DB_KEY);
        } catch (_) {
          reject(directError(
            "删除本页桥接身份失败",
            "BW_COMPUTER_VOICE_DIRECT_STORAGE",
            false
          ));
          return;
        }
        request.onsuccess = function () { resolve(); };
        request.onerror = function () {
          reject(directError(
            "删除本页桥接身份失败",
            "BW_COMPUTER_VOICE_DIRECT_STORAGE",
            false
          ));
        };
      });
    });
  }

  function validatePrivateKey(key) {
    if (
      !key ||
      key.type !== "private" ||
      key.extractable !== false ||
      !key.algorithm ||
      key.algorithm.name !== "ECDSA" ||
      key.algorithm.namedCurve !== "P-256" ||
      !Array.isArray(key.usages) ||
      key.usages.length !== 1 ||
      key.usages[0] !== "sign"
    ) {
      throw directError(
        "浏览器中的 Windows 桥接身份密钥无效",
        "BW_COMPUTER_VOICE_DIRECT_IDENTITY",
        false
      );
    }
    return key;
  }

  function normalizeIdentity(record) {
    if (!record) return null;
    exactObject(
      record,
      ["version", "endpoint", "paired", "privateKey", "publicKeySpki"],
      ["fingerprint"],
      "身份记录"
    );
    if (record.version !== 1 || typeof record.paired !== "boolean") {
      throw directError(
        "Windows 桥接身份记录版本无效",
        "BW_COMPUTER_VOICE_DIRECT_IDENTITY",
        false
      );
    }
    return {
      version: 1,
      endpoint: normalizeEndpoint(record.endpoint),
      paired: record.paired,
      privateKey: validatePrivateKey(record.privateKey),
      publicKeySpki: safeBase64Url(
        record.publicKeySpki,
        "SPKI 公钥",
        80,
        256
      ),
      fingerprint: record.fingerprint
        ? safeBase64Url(record.fingerprint, "身份指纹", 32, 128)
        : "",
    };
  }

  function readIdentity() {
    return idbGet().then(normalizeIdentity);
  }

  function createIdentity(endpoint) {
    if (
      !window.crypto ||
      !window.crypto.subtle ||
      typeof window.crypto.subtle.generateKey !== "function" ||
      typeof window.crypto.subtle.exportKey !== "function"
    ) {
      return Promise.reject(directError(
        "浏览器不支持不可导出的 ECDSA 身份密钥",
        "BW_COMPUTER_VOICE_DIRECT_CRYPTO",
        false
      ));
    }
    return window.crypto.subtle.generateKey(
      { name: "ECDSA", namedCurve: "P-256" },
      false,
      ["sign", "verify"]
    ).then(function (pair) {
      validatePrivateKey(pair.privateKey);
      return window.crypto.subtle.exportKey("spki", pair.publicKey)
        .then(function (spki) {
          var record = {
            version: 1,
            endpoint: endpoint,
            paired: false,
            privateKey: pair.privateKey,
            publicKeySpki: bytesToBase64Url(spki),
          };
          return idbPut(record).then(function () {
            return normalizeIdentity(record);
          });
        });
    }).catch(function (error) {
      if (error && error.code) throw error;
      throw directError(
        "无法生成或保存不可导出的 ECDSA 身份密钥",
        "BW_COMPUTER_VOICE_DIRECT_CRYPTO",
        false
      );
    });
  }

  function ensureIdentity(endpoint) {
    return readIdentity().then(function (identity) {
      if (identity && identity.endpoint === endpoint) return identity;
      return createIdentity(endpoint);
    });
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
      ["ready", "state", "reason", "localOptIn", "media"],
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
        socket = new window.WebSocket(self.endpoint);
      } catch (_) {
        reject(directError(
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

  DirectSocket.prototype.close = function () {
    if (this.intentional) return;
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
    this.pending.forEach(function (entry) {
      clearTimeout(entry.timer);
      entry.reject(cancelled);
    });
    this.pending.clear();
    try {
      if (this.socket && this.socket.readyState < 2) {
        this.socket.close(1000, "client-stop");
      }
    } catch (_) {}
    this.socket = null;
  };

  function normalizeChallenge(value) {
    exactObject(
      value,
      [
        "protocolVersion",
        "paired",
        "authentication",
        "signatureFormat",
        "challenge",
        "limits",
      ],
      [],
      "HELLO 响应"
    );
    if (
      value.protocolVersion !== 1 ||
      typeof value.paired !== "boolean" ||
      value.authentication !== "ecdsa-p256-sha256" ||
      value.signatureFormat !== "ieee-p1363-fixed-64"
    ) {
      throw directError(
        "Windows 桥接器认证能力不匹配",
        "BW_COMPUTER_VOICE_DIRECT_CONTRACT",
        false
      );
    }
    exactObject(
      value.challenge,
      [
        "challengeId",
        "nonce",
        "expiresAtUtc",
        "signingContract",
      ],
      [],
      "challenge"
    );
    var challengeId = safeId(value.challenge.challengeId, "challengeId");
    var nonce = safeBase64Url(value.challenge.nonce, "challenge nonce", 22, 256);
    var expiresAt = Date.parse(safeText(
      value.challenge.expiresAtUtc,
      "challenge expiresAtUtc",
      64,
      false
    ));
    if (
      !Number.isFinite(expiresAt) ||
      expiresAt <= Date.now() ||
      expiresAt > Date.now() + 5 * 60 * 1000 ||
      value.challenge.signingContract !== AUTH_CONTRACT
    ) {
      throw directError(
        "Windows 桥接器 challenge 无效或已过期",
        "BW_COMPUTER_VOICE_DIRECT_CHALLENGE",
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
      ],
      [],
      "HELLO limits"
    );
    if (
      value.limits.maxMessageBytes !== MAX_MESSAGE_BYTES ||
      value.limits.pcmFrameBytes !== PCM_FRAME_BYTES ||
      value.limits.pcmQueueLimitMs !== PCM_QUEUE_LIMIT_MS ||
      value.limits.heartbeatIntervalMs !== HEARTBEAT_INTERVAL_MS ||
      value.limits.heartbeatTimeoutMs !== HEARTBEAT_TIMEOUT_MS
    ) {
      throw directError(
        "Windows 桥接器容量合同不匹配",
        "BW_COMPUTER_VOICE_DIRECT_CONTRACT",
        false
      );
    }
    return {
      challengeId: challengeId,
      nonce: nonce,
    };
  }

  function originForSignature() {
    var origin = String(window.location && window.location.origin || "");
    if (!/^https:\/\/[^/\s]{1,240}$/.test(origin)) {
      throw directError(
        "当前 Reader Origin 不可用于 Windows 认证",
        "BW_COMPUTER_VOICE_DIRECT_ORIGIN",
        false
      );
    }
    return origin;
  }

  function signChallenge(identity, challenge) {
    validatePrivateKey(identity.privateKey);
    var canonical = AUTH_CONTRACT + "\n" +
      challenge.challengeId + "\n" +
      challenge.nonce + "\n" +
      originForSignature();
    return window.crypto.subtle.sign(
      { name: "ECDSA", hash: "SHA-256" },
      identity.privateKey,
      new TextEncoder().encode(canonical)
    ).then(function (signature) {
      if (signature.byteLength !== 64) {
        throw directError(
          "浏览器 ECDSA 签名格式不是固定 64 字节 P1363",
          "BW_COMPUTER_VOICE_DIRECT_SIGNATURE",
          false
        );
      }
      return bytesToBase64Url(signature);
    });
  }

  function hello(channel) {
    return channel.request("hello", {}).then(normalizeChallenge);
  }

  function authenticate(channel, identity, challenge) {
    return signChallenge(identity, challenge).then(function (signature) {
      return channel.request("auth", {
        challengeId: challenge.challengeId,
        signature: signature,
      });
    }).then(function (value) {
      exactObject(
        value,
        ["authenticated", "clientFingerprintSha256"],
        [],
        "AUTH 响应"
      );
      if (value.authenticated !== true) {
        throw directError(
          "Windows 桥接器未确认身份",
          "BW_COMPUTER_VOICE_DIRECT_AUTH",
          false
        );
      }
      safeBase64Url(
        value.clientFingerprintSha256,
        "身份指纹",
        32,
        128
      );
      return value;
    });
  }

  function openAuthenticated(identity, options) {
    var channel = new DirectSocket(identity.endpoint, options);
    return channel.open().then(function () {
      return hello(channel);
    }).then(function (challenge) {
      return authenticate(channel, identity, challenge);
    }).then(function () {
      return channel;
    }).catch(function (error) {
      channel.close();
      throw error;
    });
  }

  function beginPairing(options) {
    options = options || {};
    var endpoint;
    var pairingCode;
    var identity;
    var channel = null;
    return Promise.resolve().then(function () {
      endpoint = normalizeEndpoint(options.endpoint || options.url || "");
      pairingCode = normalizePairingCode(options.pairingCode || options.code);
      return ensureIdentity(endpoint);
    }).then(function (value) {
      identity = value;
      channel = new DirectSocket(endpoint);
      return channel.open();
    }).then(function () {
      return hello(channel);
    }).then(function (challenge) {
      return channel.request("pair", {
        pairingCode: pairingCode,
        clientPublicKeySpki: identity.publicKeySpki,
      }).then(function (value) {
        exactObject(
          value,
          ["paired", "clientFingerprintSha256"],
          [],
          "PAIR 响应"
        );
        if (value.paired !== true) {
          throw directError(
            "Windows 桥接器未确认配对",
            "BW_COMPUTER_VOICE_DIRECT_PAIR",
            false
          );
        }
        var fingerprint = safeBase64Url(
          value.clientFingerprintSha256,
          "身份指纹",
          32,
          128
        );
        return authenticate(channel, identity, challenge).then(function (auth) {
          if (auth.clientFingerprintSha256 !== fingerprint) {
            throw directError(
              "PAIR 与 AUTH 身份指纹不一致",
              "BW_COMPUTER_VOICE_DIRECT_AUTH",
              false
            );
          }
          var record = {
            version: 1,
            endpoint: endpoint,
            paired: true,
            privateKey: identity.privateKey,
            publicKeySpki: identity.publicKeySpki,
            fingerprint: fingerprint,
          };
          return idbPut(record).then(function () {
            return {
              ok: true,
              paired: true,
              endpoint: endpoint,
              fingerprint: fingerprint,
            };
          });
        });
      });
    }).finally(function () {
      if (channel) channel.close();
    });
  }

  function offlineAvailability(identity, error) {
    var code = error && error.code || "BW_COMPUTER_VOICE_DIRECT_OFFLINE";
    var authFailure = /AUTH|IDENTITY|CHALLENGE|CONTRACT|SCHEMA/.test(code);
    return {
      paired: true,
      state: authFailure ? "auth-failed" : "offline",
      reason: error && error.message || "Windows 桥接器离线",
      code: code,
      endpoint: identity.endpoint,
      status: null,
    };
  }

  function availability() {
    var identity;
    var channel = null;
    return readIdentity().then(function (value) {
      identity = value;
      if (!identity || !identity.paired) {
        return {
          paired: false,
          state: "unpaired",
          reason: "pairing-required",
          endpoint: identity && identity.endpoint || "",
          status: null,
        };
      }
      return openAuthenticated(identity).then(function (opened) {
        channel = opened;
        return channel.request("status", {}).then(normalizeStatusPayload);
      }).then(function (status) {
        return {
          paired: true,
          state: status.state,
          reason: status.reason,
          endpoint: identity.endpoint,
          status: status,
        };
      }).catch(function (error) {
        return offlineAvailability(identity, error);
      }).finally(function () {
        if (channel) channel.close();
      });
    });
  }

  function forgetIdentity() {
    if (active) {
      return Promise.reject(directError(
        "请先挂断电脑客户端通话",
        "BW_COMPUTER_VOICE_ALREADY_ACTIVE",
        false
      ));
    }
    return idbDelete().then(function () {
      emitStatus({
        state: "unpaired",
        message: "已忘记本页中的 Windows 桥接身份",
      });
      return { ok: true, state: "unpaired" };
    });
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
      released: false,
    };
  }

  function releaseSurface(surface) {
    if (!surface || surface.released) return;
    surface.released = true;
    surface.pending.length = 0;
    surface.sources.forEach(function (source) {
      try { source.stop(); } catch (_) {}
    });
    surface.sources.clear();
    try { surface.context.close(); } catch (_) {}
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
    preparedSurface = makeAudioSurface();
    primeSurface(preparedSurface);
    preparedTimer = setTimeout(function () {
      clearPreparedSurface(true);
    }, PREPARED_SURFACE_TTL_MS);
  }

  function claimPreparedSurface() {
    var surface = clearPreparedSurface(false);
    if (surface) return surface;
    surface = makeAudioSurface();
    primeSurface(surface);
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
      throw directError(
        "Windows 音频队列超过 400 ms，已停止以避免伪连续播放",
        "BW_COMPUTER_VOICE_DIRECT_PCM_OVERFLOW",
        false
      );
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
      state.audioBlocked = true;
      if (surface.pending.length >= PCM_QUEUE_LIMIT_FRAMES) {
        throw directError(
          "浏览器阻止播放超过 400 ms，PCM 队列已关闭",
          "BW_COMPUTER_VOICE_DIRECT_PCM_OVERFLOW",
          false
        );
      }
      surface.pending.push(samples);
      emitStatus({
        state: "audio-blocked",
        sessionId: state.sessionId,
        message: "浏览器阻止了声音；请再次点击电话按钮允许播放",
        code: "BW_COMPUTER_VOICE_AUDIO_BLOCKED",
      });
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
      if (
        error &&
        error.code === "BW_COMPUTER_VOICE_DIRECT_PCM_OVERFLOW"
      ) {
        failActive(state, error, true);
        throw error;
      }
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
    return messages[state] + (reason ? "：" + reason : "");
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
    clearHeartbeat(state);
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
    if ((track !== 1 && track !== 2) || view.getUint16(6, true) !== 0) {
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
    if (active && active.audioBlocked) return retryPlayback(active);
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
      pcm: {
        1: { nextSequence: 0, seen: false, timestampLow: 0, timestampHigh: 0 },
        2: { nextSequence: 0, seen: false, timestampLow: 0, timestampHigh: 0 },
      },
    };
    active = state;
    emitStatus({
      state: "checking",
      sessionId: state.sessionId,
      message: "正在直连 Windows 桥接器…",
    });

    return readIdentity().then(function (identity) {
      if (!identity || !identity.paired) {
        throw directError(
          "尚未连接 Windows 桥接器；配对码请在 Windows EXE 中生成",
          "BW_COMPUTER_VOICE_PAIRING_REQUIRED",
          false
        );
      }
      return openAuthenticated(identity, {
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
      // authorized START send, never during HELLO/AUTH/STATUS.
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
      scheduleHeartbeat(state);
      if (state.stopped || active !== state) {
        throw directError(
          "Windows 桥接启动已取消",
          "BW_COMPUTER_VOICE_DIRECT_CANCELLED",
          false
        );
      }
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
    if (!state) {
      emitStatus({ state: "stopped", message: "电脑客户端已停止" });
      return Promise.resolve({ ok: true, state: "stopped" });
    }
    state.cancelled = true;
    state.stopped = true;
    clearHeartbeat(state);
    if (!state.started) {
      // START 尚未确认时不能把 STOP 排在同一条 WSS 后面等待：Windows
      // 服务端正在串行处理 START，排队 STOP 只会让应用、采音和快捷键继续
      // 启动。立即关闭连接会取消所有在途请求，并让远端会话租约 fail closed。
      if (state.channel) state.channel.close();
      releaseSurface(state.surface);
      emitStatus({ state: "stopped", message: "电脑客户端启动已取消" });
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
      emitStatus({ state: "stopped", message: "电脑客户端已挂断" });
      return { ok: true, state: "stopped" };
    });
  }

  function phoneButtonFromEvent(event) {
    var target = event && event.target;
    while (target) {
      if (target.id === "asst-call") return target;
      target = target.parentNode;
    }
    return null;
  }

  function installGestureCapture() {
    if (!document || typeof document.addEventListener !== "function") return;
    document.addEventListener("click", function (event) {
      if (!phoneButtonFromEvent(event)) return;
      if (active && active.audioBlocked) {
        // 此次点击只恢复被浏览器拦截的同一条音频，不让上层把它当挂断。
        try { event.preventDefault(); } catch (_) {}
        try { event.stopImmediatePropagation(); } catch (_) {}
        retryPlayback(active).catch(function () {});
        return;
      }
      if (!active) prepareSurfaceFromGesture();
    }, true);
  }

  function mountSettings(container) {
    if (!container || container.querySelector(".rc-computer-voice-settings")) return;
    var root = document.createElement("div");
    root.className = "rc-computer-voice-settings";
    root.innerHTML =
      '<div class="ams-tdef" data-role="status">正在读取 Windows 直连配置…</div>' +
      '<div data-role="setup" style="display:none;margin-top:7px">' +
      '<label class="ams-tdef">Windows EXE 显示的受信任 WSS 地址</label>' +
      '<input class="ams-inp" data-role="endpoint" inputmode="url" ' +
      'autocomplete="off" spellcheck="false" placeholder="wss://电脑名.tailnet.ts.net/…">' +
      '<label class="ams-tdef" style="display:block;margin-top:6px">' +
      '一次性配对码（在 Windows 电脑客户端桥接器 EXE 中获取）</label>' +
      '<input class="ams-inp" data-role="code" autocomplete="one-time-code" ' +
      'autocapitalize="characters" maxlength="12" placeholder="10 位配对码">' +
      '<div class="ams-row" style="margin-top:7px">' +
      '<button type="button" class="ams-btn" data-role="pair">连接 Windows 桥接器</button>' +
      '</div></div>' +
      '<div class="ams-row" style="margin-top:7px">' +
      '<button type="button" class="ams-btn" data-role="refresh">刷新直连状态</button>' +
      '<button type="button" class="ams-btn" data-role="forget" style="display:none">' +
      '忘记此桥接器</button>' +
      '</div>' +
      '<div class="ams-tdef" data-role="detail" style="margin-top:6px"></div>';
    container.appendChild(root);
    var status = root.querySelector('[data-role="status"]');
    var setup = root.querySelector('[data-role="setup"]');
    var endpoint = root.querySelector('[data-role="endpoint"]');
    var code = root.querySelector('[data-role="code"]');
    var pair = root.querySelector('[data-role="pair"]');
    var forget = root.querySelector('[data-role="forget"]');
    var detail = root.querySelector('[data-role="detail"]');

    function render(value) {
      if (!value.paired || value.state === "auth-failed") {
        setup.style.display = "";
        forget.style.display = value.paired ? "" : "none";
        if (value.endpoint) endpoint.value = value.endpoint;
        if (value.state === "auth-failed") {
          status.textContent = "○ Windows 桥接身份已失效，请用 EXE 的新配对码重新连接。";
          detail.textContent =
            "将复用本浏览器中不可导出的私钥；也可先“忘记此桥接器”再建立新身份。";
        } else {
          status.textContent = "尚未连接 Windows 桥接器。";
          detail.textContent =
            "请先在 Windows EXE 中启用服务并取得 WSS 地址和一次性配对码；" +
            "选择模型或刷新状态不会启动应用或采音。";
        }
        return;
      }
      setup.style.display = "none";
      forget.style.display = "";
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
          (value.reason || value.state || "未就绪");
      }
      detail.textContent =
        "直连 " + value.endpoint +
        "；身份私钥仅存于本浏览器 IndexedDB，且不可导出。";
    }

    function refresh() {
      status.textContent = "正在直连 Windows 桥接器读取状态…";
      return availability().then(render).catch(function (error) {
        setup.style.display = "";
        status.textContent = "直连配置读取失败：" +
          (error.message || "未知错误");
        detail.textContent = "未发送 START。";
      });
    }

    root.querySelector('[data-role="refresh"]').addEventListener("click", refresh);
    forget.addEventListener("click", function () {
      if (
        typeof window.confirm === "function" &&
        !window.confirm("只删除此浏览器中的 Windows 桥接身份？")
      ) return;
      forget.disabled = true;
      forgetIdentity().then(function () {
        endpoint.value = "";
        code.value = "";
        render({
          paired: false,
          state: "unpaired",
          endpoint: "",
        });
      }).catch(function (error) {
        status.textContent = "删除失败：" + (error.message || "未知错误");
      }).finally(function () {
        forget.disabled = false;
      });
    });
    pair.addEventListener("click", function () {
      pair.disabled = true;
      status.textContent = "正在与 Windows 桥接器安全配对…";
      beginPairing({
        endpoint: endpoint.value,
        pairingCode: code.value,
      }).then(function () {
        code.value = "";
        setup.style.display = "none";
        status.textContent = "● Windows 桥接器已安全配对。";
        detail.textContent =
          "长期身份使用本浏览器不可导出的 P-256 私钥；未保存 bearer token。";
        return refresh();
      }).catch(function (error) {
        setup.style.display = "";
        status.textContent = "连接失败：" + (error.message || "未知错误");
      }).finally(function () {
        pair.disabled = false;
      });
    });
    refresh();
  }

  installGestureCapture();

  RC.computerVoice = Object.freeze({
    contract: BRIDGE_CONTRACT,
    directContract: DIRECT_CONTRACT,
    authContract: AUTH_CONTRACT,
    availability: availability,
    beginPairing: beginPairing,
    forgetIdentity: forgetIdentity,
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
