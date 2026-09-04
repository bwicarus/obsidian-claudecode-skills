/* rc-computer-voice.js — Reader/PWA ↔ Windows 电脑语音直连入口。
 *
 * 控制与固定帧 PCM 音频只走固定的 tailnet WSS，不经过 Pi。Tailnet 身份和
 * 固定 Origin 由 Windows 端校验；Reader 不保存身份或凭据。选择模型、加载
 * 设置和 STATUS 都不会启动 Windows 应用或采音；电脑按钮的一次真实用户操作
 * 只发送 START。实时快照与 Codex 语音由 ReaderPC 服务器进程统一管理，Reader
 * 只读取其运行状态，不发送独立启停或持续运行目标。
 */
(function () {
  "use strict";

  var RC = window.RC = window.RC || {};
  var BRIDGE_CONTRACT = "reader-computer-voice-bridge/1";
  var DIRECT_CONTRACT = "reader-computer-voice-direct/1";
  var MAX_MESSAGE_BYTES = 262144;
  var PCM_FRAME_BYTES = 1956;
  var PCM_HEADER_BYTES = 36;
  var PCM_SAMPLES = 960;
  var PCM_SAMPLE_RATE = 48000;
  var PCM_QUEUE_LIMIT_MS = 400;
  var PCM_UPLINK_TRACK = 3;
  var PCM_UPLINK_BUFFER_LIMIT_BYTES = PCM_FRAME_BYTES * 10;
  var MAX_PENDING = 16;
  var OPEN_TIMEOUT_MS = 6000;
  var CLOSE_TIMEOUT_MS = 1500;
  var REQUEST_TIMEOUT_MS = 7000;
  var START_TIMEOUT_MS = 45000;
  var HEARTBEAT_INTERVAL_MS = 5000;
  // 心跳连续失败多少次才判定连接已死。见 scheduleHeartbeat 里的说明：
  // 客户端单次超时 7s 比服务端的 15s 合同还严，一次抖动就误杀整通电话。
  var HEARTBEAT_FAILURES_BEFORE_GIVING_UP = 2;
  var HEARTBEAT_TIMEOUT_MS = 15000;
  var START_GESTURE_LEASE_TTL_MS = 5000;
  var OUTGOING_CONTEXT_CONTRACT = "reader-outgoing-context/1";
  // Windows 侧合同(Codex 实现,0.1.73):扩展页直接上行网页正文。
  var WEB_PAGE_CONTEXT_CONTRACT = "reader-web-page-context/1";

  var ACTIVE_READING_CONTRACT = "reader-active-reading/1";
  var READER_RESULT_DELIVERY_CONTRACT = "reader-result-delivery/1";
  var READER_RESULT_EVENT = "reader-result";
  var READER_RESULT_ACK = "reader-result-ack";
  var READER_REALTIME_OUTPUT_CONTRACT = "reader-realtime-output/1";
  var READER_REALTIME_OUTPUT_EVENT = "reader-realtime-output";
  var READER_REALTIME_OUTPUT_ACK = "reader-realtime-output-ack";
  var READER_VISUAL_DELIVERY_CONTRACT = "reader-visual-delivery/2";
  var READER_VISUAL_EVENT = "reader-visual-request";
  var READER_QUERY_EVENT = "reader-query-request";
  var READER_QUERY_RESPONSE = "reader-query";
  var READER_QUERY_CONTRACT = "reader-query/1";
  var READER_VISUAL_CHUNK = "reader-visual";
  var READER_VISUAL_MAX_BYTES = 768 * 1024;
  var READER_VISUAL_CHUNK_CHARS = 48000;
  var READER_VISUAL_MAX_CHUNKS = 24;
  var READER_VISUAL_SCOPES = Object.freeze({
    "viewport-context": true,
    "drawing-nearby": true,
    "selection-near": true,
  });
  var CONTEXT_DELIVERY_LEGACY = "legacy-inject";
  var CONTEXT_DELIVERY_SNAPSHOT = "snapshot-mcp";
  var COMPUTER_TARGET_CODEX = "codex-desktop";
  var COMPUTER_TARGET_CLASSIC = "chatgpt-classic";
  var ACTIVE_READING_POLL_MS = 250;
  var ACTIVE_READING_HEARTBEAT_MS = 60000;
  var LOCAL_PAGE_CONTEXT_POLL_MS = 1500;
  var LOCAL_PAGE_CONTEXT_RESEND_MS = 60 * 1000;   // 同一份正文的重发间隔(桥失忆自愈)
  var LOCAL_PAGE_CONTEXT_BUILD_TIMEOUT_MS = 20 * 1000;   // 正文构建上限:超过即放栅栏、记 dlog
  var LOCAL_PAGE_TEXT_WAIT_MS = 1200;
  // 256 KiB is the direct bridge's immutable frame ceiling.  Keep enough
  // envelope headroom while allowing normal long cards to remain complete.
  var LOCAL_PAGE_CONTEXT_LIMIT = 220000;
  var LOCAL_PAGE_CONTEXT_MAX_BYTES = 224 * 1024;
  var LOCAL_STRUCTURED_ANCHOR_MAP_MAX_SEGMENTS = 4096;
  var LOCAL_STRUCTURED_ANCHOR_SEGMENT_TEXT_LIMIT = 512;
  var LOCAL_PAGE_CARDS_CONTRACT = "reader-local-page-cards/1";
  var LOCAL_PAGE_CARD_SOURCE_CONTRACT = "reader-local-page-card-source/1";
  var LOCAL_PAGE_CARD_PROJECTION_CONTRACT =
    "reader-local-page-card-projection/1";
  var READER_PAGE_CARD_DETAIL_CONTRACT = "reader-page-card-detail/1";
  var LOCAL_PAGE_CARD_CONTEXT_LIMIT = 100000;
  var LOCAL_PAGE_CARD_REPLACEMENT_FORMAT =
    "application/vnd.bw-reader.card-replacement+json;version=1";
  var LOCAL_NOTES_CHANGED_CONTRACT = "reader-local-notes-changed/1";
  var LOCAL_NOTES_CHANGED_EVENT = "bw:native-document-notes-changed";
  var LOCAL_HIGHLIGHT_SOURCE_REFRESH_MS = 30000;
  var LOCAL_HIGHLIGHT_SOURCE_RETRY_MS = 5000;
  var LOCAL_HIGHLIGHT_SOURCE_TTL_MARGIN_MS = 30000;
  var SNAPSHOT_RECONNECT_MS = 1000;
  var SNAPSHOT_RECONNECT_MAX_MS = 15000;
  // Server cleanup may consume its published 40s bound, service start another
  // 8s, and the capped reconnect window 15s.  Keep a 75s client bound so the
  // UI does not falsely roll back a healthy worst-case switch at 60s.
  var READERPC_INTENT_APPLY_TIMEOUT_MS = SNAPSHOT_RECONNECT_MAX_MS * 5;
  var CONTEXT_BOOTSTRAP_LIMIT = 500;
  var CONTEXT_LIVE_LIMIT = 32;
  var CONTEXT_LIVE_WAIT_S = 20;
  var CONTEXT_RETRY_MS = 500;
  var CONTEXT_WAIT_DENIED_RETRY_MS = 1000;
  var NATIVE_CONTEXT_REQUEST_TIMEOUT_MS = 8000;
  var DICTIONARY_LOOKUP_TIMEOUT_MS = 70000;
  var LOCAL_ANKI_ADD_TIMEOUT_MS = 45000;
  var CONTEXT_EVENT_TYPES = Object.freeze({
    "page.context": true,
    focus: true,
    drawing: true,
    command: true,
    "command-failed": true,
  });
  var READER_ORIGIN = "https://bwicarus.taile44d0c.ts.net";
  var NATIVE_APP_ORIGIN = "http://127.0.0.1:43129";
  var EXTENSION_RELAY_PORT = "BW_COMPUTER_VOICE_DIRECT_V3";
  var DIRECT_ENDPOINT =
    "wss://bwicarus-2.taile44d0c.ts.net/reader-computer-voice/v1";
  // Context only: many connections may share it, it takes no part in audio
  // ownership, and it refuses START/STOP outright. Used for the snapshot link
  // inside the App, where Swift already owns the voice and a second connection
  // on the voice endpoint would evict it.
  var CONTEXT_ENDPOINT =
    "wss://bwicarus-2.taile44d0c.ts.net/reader-context/v1";
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
    "waiting-voice-ready": true,
    "starting-capture": true,
  });

  var active = null;
  var preparedSurface = null;
  var preparedTimer = null;
  var requestSequence = 0;
  var statusListeners = [];
  var availabilityAttempt = null;
  var lastClientFailure = null;
  var computerTarget = COMPUTER_TARGET_CODEX;
  var computerTargetLoaded = false;
  var computerTargetLoadPromise = null;
  var registeredComputerButtons = new WeakSet();
  // 仅供旧合同/旧缓存页面回退；新 voicecall 不再登记电话按钮。
  var registeredLegacyPhoneButtons = new WeakSet();
  var selectedEngineKnown = false;
  var computerVoiceSelected = false;
  var selectedEngineRevision = 0;
  var selectedEngineAcceptedRevision = 0;
  var selectedEngineMutationRevision = null;
  var dialPending = false;
  // These cursors live only for this module instance. `lastContextAckCursor`
  // is advanced exclusively by an exact Windows per-event ACK.
  var lastContextAckCursor = null;
  // `lastContextResumeCursor` may additionally hold the one explicit initial
  // baseline chosen after a bounded bootstrap scan finds no page.context.
  // It is never presented as an ACK and is not persisted to page storage.
  var lastContextResumeCursor = null;
  var contextPumpGeneration = 0;
  var contextDeliveryMode = null;
  var bridgeServiceMode = "full";   // "full" | "bridge-only":ReaderPC 桥接模式旗标(context-mode 自愿升级字段带回)
  // Independent optional layer.  Missing means an older ReaderPC service;
  // preserve released voice-on behavior but do not expose a switch that the
  // old service cannot understand.
  var bridgeVoiceEnabled = true;
  var bridgeVoiceEnabledKnown = false;
  var bridgePendingServiceMode = null;
  var bridgePendingVoiceEnabled = null;
  var bridgePendingServiceModeTimer = null;
  var bridgePendingVoiceEnabledTimer = null;
  var contextModeChanging = false;
  var contextModeChangePromise = null;
  var snapshotLink = null;
  var snapshotLinkGeneration = 0;
  var snapshotReconnectTimer = null;
  // `contextDeliveryMode` is the confirmed user/server configuration.  These
  // fields describe only the disposable WSS which carries that configuration.
  // A transport failure must never turn a configured snapshot mode into an
  // unknown mode: in the App that would make the native voice owner suppress
  // the dedicated context link forever.
  var snapshotTransportState = "idle";
  var snapshotReconnectAttempt = 0;
  var nativeContextState = null;
  var nativeContextHandoffPending = false;
  var nativeContextRequestSequence = 0;
  var nativeContextPending = Object.create(null);
  var localHighlightSourceCache = null;
  var localActiveReadingStates = [];
  var readerVisualCache = null;
  var readerVisualCaptureKey = null;
  var readerVisualCapturePromise = null;
  var readerVisualGeneration = 0;
  var readerVisualPageKey = null;
  // One stable identity for this top-level Reader document. Windows uses it
  // only to route an on-demand visual/control request back to the exact live
  // surface that most recently described the snapshot; it is never an audio
  // owner and is regenerated on a real document reload.
  var readerSourceInstanceId = null;
  var READER_DRAWING_STATE_EVENT = "bw-reader-drawing-state";
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

  function normalizeComputerTarget(value) {
    return value === COMPUTER_TARGET_CLASSIC
      ? COMPUTER_TARGET_CLASSIC
      : COMPUTER_TARGET_CODEX;
  }

  function getComputerTarget() {
    return computerTarget;
  }

  function computerTargetFetch(url, options) {
    if (!window || typeof window.fetch !== "function") {
      return Promise.reject(new Error("Reader 设置接口不可用"));
    }
    // @interaction computer-voice.bridge.request
    return window.fetch(url, options);
  }

  function loadComputerTarget() {
    if (computerTargetLoaded) return Promise.resolve(computerTarget);
    if (computerTargetLoadPromise) return computerTargetLoadPromise;
    computerTargetLoadPromise = computerTargetFetch(
      "/api/assistant/voice-config"
    ).then(
      function (response) {
        if (!response || response.ok !== true ||
            typeof response.json !== "function") {
          throw new Error("电脑客户端目标读取失败");
        }
        return response.json();
      }
    ).then(function (value) {
      if (!value || value.ok !== true) {
        throw new Error("电脑客户端目标读取失败");
      }
      computerTarget = normalizeComputerTarget(
        value.cfg && value.cfg.rt_computer_target
      );
      computerTargetLoaded = true;
      return computerTarget;
    }).finally(function () {
      computerTargetLoadPromise = null;
    });
    return computerTargetLoadPromise;
  }

  function computerTargetBusy() {
    var nativeState = null;
    try {
      nativeState = window.__BW_NATIVE_COMPUTER_VOICE_STATE__;
    } catch (error) {}
    return !!active || dialPending || !!(
      nativeState &&
      (nativeState.active === true || nativeState.busy === true)
    );
  }

  function nativeComputerVoiceState() {
    try {
      var value = window.__BW_NATIVE_COMPUTER_VOICE_STATE__;
      return plainObject(value) ? value : null;
    } catch (_) {
      return null;
    }
  }

  function nativeComputerVoiceOwnsWss() {
    var value = nativeComputerVoiceState();
    return !!(
      nativeContextHandoffPending ||
      (value && (value.active === true || value.busy === true))
    );
  }

  function nativeContextHandlerAvailable() {
    try {
      return !!(
        window.webkit &&
        window.webkit.messageHandlers &&
        window.webkit.messageHandlers.bwNativeComputerContext &&
        typeof window.webkit.messageHandlers.bwNativeComputerContext
          .postMessage === "function"
      );
    } catch (_) {
      return false;
    }
  }

  function setComputerTarget(value) {
    var normalized = normalizeComputerTarget(value);
    if (computerTargetBusy()) {
      return Promise.reject(directError(
        "请先结束当前电脑语音，再切换目标",
        "BW_COMPUTER_VOICE_TARGET_BUSY",
        true
      ));
    }
    var body = { rt_computer_target: normalized };
    return computerTargetFetch("/api/assistant/voice-config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }).then(function (response) { return response.json(); }).then(
      function (result) {
        if (!result || result.ok !== true) {
          throw directError(
            "无法保存电脑客户端目标",
            "BW_COMPUTER_VOICE_TARGET_STORAGE_FAILED",
            true
          );
        }
        computerTarget = normalized;
        computerTargetLoaded = true;
        return normalized;
      }
    );
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

  var RESULT_CARD_SPECS = Object.freeze({
    weather: {
      required: ["lo", "hi", "cond"],
      optional: ["loc", "date", "precip", "tip"],
    },
    news: {
      itemRequired: ["t"],
      itemOptional: ["s", "src"],
    },
    images: {
      itemRequired: ["url"],
      itemOptional: ["title", "aid", "src"],
    },
    videos: {
      itemRequired: ["title"],
      itemOptional: ["thumb", "url", "channel", "src"],
    },
    fact: {
      required: ["answer"],
      optional: ["detail"],
    },
    general: {
      required: [],
      optional: ["text"],
    },
  });

  function resultText(value, label, required) {
    var text = safeText(value, label, 2000, !required);
    if (required && !text.trim()) {
      throw directError(label + " 不能为空", "BW_READER_RESULT_SCHEMA", false);
    }
    return text;
  }

  function resultValue(value, label, url) {
    if (!url && typeof value === "number" && Number.isFinite(value)) {
      return value;
    }
    var text = resultText(value, label, true);
    if (url) {
      var parsed;
      try { parsed = new URL(text); } catch (_) {}
      if (
        !parsed ||
        parsed.protocol !== "https:" ||
        !parsed.hostname ||
        parsed.username ||
        parsed.password ||
        text !== text.trim() ||
        text.indexOf("\\") >= 0
      ) {
        throw directError(
          label + " 不是安全 HTTPS URL",
          "BW_READER_RESULT_SCHEMA",
          false
        );
      }
    }
    return text;
  }

  function normalizeResultFields(value, required, optional, label) {
    exactObject(value, required, optional, label);
    var normalized = {};
    required.concat(optional).forEach(function (field) {
      if (!Object.prototype.hasOwnProperty.call(value, field)) return;
      normalized[field] = resultValue(
        value[field],
        label + "." + field,
        field === "url" || field === "thumb"
      );
    });
    return normalized;
  }

  /// 卡片的绑定目标。形状与服务端 `reader_card_contract._norm_bind`、
  /// C# `ReaderRealtimeOutput.ValidateCardBind` 保持一致。
  ///
  /// 区间不合法就整条拒收，不静默丢弃 —— 与其在页面上定出一个荒唐的位置，
  /// 不如让调用方立刻知道自己发错了。
  function normalizeCardBind(value) {
    if (!plainObject(value)) {
      throw directError(
        "Reader 卡片 bind 无效",
        "BW_READER_REALTIME_OUTPUT_SCHEMA",
        false
      );
    }
    var kind = safeText(value.kind, "Reader 卡片 bind.kind", 32, false);
    if (kind === "upage-block") {
      exactObject(value, ["kind", "upage", "bid"], [], "Reader 卡片 bind");
      return {
        kind: kind,
        upage: safeText(value.upage, "Reader 卡片 bind.upage", 200, false),
        bid: safeText(value.bid, "Reader 卡片 bind.bid", 200, false),
      };
    }
    if (kind === "page-chars") {
      // 序号与原文二选一；block 可选，把"按文本找"限定在某一块里。
      // 形状与服务端 reader_card_contract._norm_bind、C# ValidateCardBind 一致。
      exactObject(
        value,
        ["kind", "page"],
        ["from", "to", "text", "rev", "block"],
        "Reader 卡片 bind"
      );
      var page = value.page;
      if (!Number.isSafeInteger(page) || page < 1) {
        throw directError(
          "Reader 卡片 bind 的页码无效",
          "BW_READER_REALTIME_OUTPUT_SCHEMA",
          false
        );
      }
      // ⚠ 这里是**重建**不是透传 —— 放行了字段还必须显式搬过来。
      //   只放行不搬的表现是「校验全过、卡片照常出现、就是不钉」，
      //   链路上没有一处报错（见 CLAUDE.md 与 card-bind-whitelist-parity 测试）。
      var bind = { kind: kind, page: page };
      var hasFrom = Object.prototype.hasOwnProperty.call(value, "from");
      var hasTo = Object.prototype.hasOwnProperty.call(value, "to");
      if (hasFrom !== hasTo) {
        // 只给一半是发错了，不是"想按文本找"。
        throw directError(
          "Reader 卡片 bind 的 from/to 必须成对出现",
          "BW_READER_REALTIME_OUTPUT_SCHEMA",
          false
        );
      }
      if (hasFrom) {
        var from = value.from;
        var to = value.to;
        if (
          !Number.isSafeInteger(from) || from < 0 ||
          !Number.isSafeInteger(to) || to < from
        ) {
          throw directError(
            "Reader 卡片 bind 的字符区间无效",
            "BW_READER_REALTIME_OUTPUT_SCHEMA",
            false
          );
        }
        bind.from = from;
        bind.to = to;
      }
      ["text", "rev"].forEach(function (field) {
        if (
          Object.prototype.hasOwnProperty.call(value, field) &&
          value[field] !== null
        ) {
          bind[field] = safeText(
            value[field], "Reader 卡片 bind." + field, 200, false
          );
        }
      });
      if (Object.prototype.hasOwnProperty.call(value, "block") &&
          value.block !== null) {
        var block = value.block;
        if (!Number.isSafeInteger(block) || block < 1) {
          throw directError(
            "Reader 卡片 bind 的块号无效",
            "BW_READER_REALTIME_OUTPUT_SCHEMA",
            false
          );
        }
        if (!bind.text) {
          // 块号必须配原文：只给块号等于"钉在第 3 块的某处"，那不是一个位置。
          throw directError(
            "Reader 卡片 bind 的块号必须与 text 同时给出",
            "BW_READER_REALTIME_OUTPUT_SCHEMA",
            false
          );
        }
        bind.block = block;
      }
      if (!hasFrom && !bind.text) {
        // 既没有序号也没有原文 —— 这个锚指不向任何地方。
        throw directError(
          "Reader 卡片 bind 必须给出字符区间或原文",
          "BW_READER_REALTIME_OUTPUT_SCHEMA",
          false
        );
      }
      return bind;
    }
    throw directError(
      "Reader 卡片 bind 类型无效",
      "BW_READER_REALTIME_OUTPUT_SCHEMA",
      false
    );
  }

  function normalizeResultCard(card) {
    exactObject(
      card,
      ["kind", "data"],
      // bind 已在上游 normalizeCardBind 校验并规范化过，这里只放行
      ["title", "brief", "sources", "bind"],
      "Reader 结果 card"
    );
    var kind = safeText(card.kind, "Reader 结果 card.kind", 16, false);
    var spec = RESULT_CARD_SPECS[kind];
    if (!spec || !plainObject(card.data)) {
      throw directError(
        "Reader 结果 card.kind/data 无效",
        "BW_READER_RESULT_SCHEMA",
        false
      );
    }
    var data;
    if (spec.itemRequired) {
      exactObject(card.data, ["items"], [], "Reader 结果 " + kind + ".data");
      if (
        !Array.isArray(card.data.items) ||
        card.data.items.length < 1 ||
        card.data.items.length > 20
      ) {
        throw directError(
          "Reader 结果 items 数量无效",
          "BW_READER_RESULT_SCHEMA",
          false
        );
      }
      data = {
        items: card.data.items.map(function (item, index) {
          return normalizeResultFields(
            item,
            spec.itemRequired,
            spec.itemOptional,
            "Reader 结果 " + kind + ".items[" + index + "]"
          );
        }),
      };
    } else {
      data = normalizeResultFields(
        card.data,
        spec.required,
        spec.optional,
        "Reader 结果 " + kind + ".data"
      );
      if (
        (kind === "fact" && typeof data.detail !== "undefined" &&
          typeof data.detail !== "string") ||
        (kind === "general" && typeof data.text !== "undefined" &&
          typeof data.text !== "string")
      ) {
        throw directError(
          "Reader 结果文字字段无效",
          "BW_READER_RESULT_SCHEMA",
          false
        );
      }
    }
    var normalized = { kind: kind, data: data };
    // 这是本文件里第二次重建卡片对象。放行了却不搬，表现是"校验全过、
    // 卡片照常出现、就是不钉" —— 比被拒更难查，因为看不出哪里出了问题。
    if (Object.prototype.hasOwnProperty.call(card, "bind")) {
      normalized.bind = card.bind;
    }
    ["title", "brief"].forEach(function (field) {
      if (Object.prototype.hasOwnProperty.call(card, field)) {
        normalized[field] = resultText(
          card[field],
          "Reader 结果 card." + field,
          false
        );
      }
    });
    if (Object.prototype.hasOwnProperty.call(card, "sources")) {
      if (
        !Array.isArray(card.sources) ||
        card.sources.length < 1 ||
        card.sources.length > 5
      ) {
        throw directError(
          "Reader 结果 sources 数量无效",
          "BW_READER_RESULT_SCHEMA",
          false
        );
      }
      normalized.sources = card.sources.map(function (source, index) {
        exactObject(source, ["url", "title"], [], "Reader 结果 source");
        return {
          url: resultValue(source.url, "Reader 结果 source.url", true),
          title: resultText(
            source.title,
            "Reader 结果 source[" + index + "].title",
            true
          ),
        };
      });
    }
    return normalized;
  }

  function normalizeResultFlashcards(cards) {
    if (!Array.isArray(cards) || cards.length < 1 || cards.length > 20) {
      throw directError(
        "Reader 结果 cards 数量无效",
        "BW_READER_RESULT_SCHEMA",
        false
      );
    }
    var normalized = cards.map(function (card, index) {
      exactObject(
        card,
        ["type"],
        ["front", "back", "cloze", "text"],
        "Reader 结果 cards[" + index + "]"
      );
      var normalized = { type: card.type };
      ["front", "back", "cloze", "text"].forEach(function (field) {
        if (Object.prototype.hasOwnProperty.call(card, field)) {
          normalized[field] = resultText(
            card[field],
            "Reader 结果 cards[" + index + "]." + field,
            false
          );
        }
      });
      if (
        (card.type !== "basic" && card.type !== "cloze") ||
        (
          card.type === "basic"
            ? !String(normalized.front || "").trim()
            : !String(normalized.cloze || normalized.text || "").trim()
        )
      ) {
        throw directError(
          "Reader 结果 cards[" + index + "] 无效",
          "BW_READER_RESULT_SCHEMA",
          false
        );
      }
      return normalized;
    });
  }

  function normalizeReaderOutputTarget(target) {
    if (!plainObject(target)) {
      throw directError(
        "Reader 文档目标无效",
        "BW_READER_REALTIME_OUTPUT_SCHEMA",
        false
      );
    }
    var targetKind = safeText(
      target.kind,
      "Reader 文档目标 kind",
      16,
      false
    );
    var locationName;
    var minimum;
    if (targetKind === "pdf") {
      exactObject(target, ["kind", "page"], [], "Reader PDF 文档目标");
      locationName = "page";
      minimum = 1;
    } else if (targetKind === "epub") {
      exactObject(target, ["kind", "section"], [], "Reader EPUB 文档目标");
      locationName = "section";
      minimum = 0;
    } else {
      throw directError(
        "Reader 文档目标类型无效",
        "BW_READER_REALTIME_OUTPUT_SCHEMA",
        false
      );
    }
    var location = target[locationName];
    if (
      !Number.isSafeInteger(location) ||
      location < minimum ||
      location > 10000000
    ) {
      throw directError(
        "Reader 文档目标位置无效",
        "BW_READER_REALTIME_OUTPUT_SCHEMA",
        false
      );
    }
    var normalized = { kind: targetKind };
    normalized[locationName] = location;
    return normalized;
  }

  function normalizeReaderHighlightRangeRef(value) {
    exactObject(
      value,
      [
        "contract", "snapshotId", "documentId", "target",
        "sourceDigest", "revision", "startMarker", "endMarker",
      ],
      [],
      "Reader 范围高亮引用"
    );
    if (value.contract !== "reader-source-range/1") {
      throw directError(
        "Reader 范围高亮合同无效",
        "BW_READER_REALTIME_OUTPUT_SCHEMA",
        false
      );
    }
    var snapshotId = safeId(
      value.snapshotId,
      "Reader 范围高亮 snapshotId"
    );
    var documentId = safeText(
      value.documentId,
      "Reader 范围高亮 documentId",
      4096,
      false
    );
    var sourceDigest = safeText(
      value.sourceDigest,
      "Reader 范围高亮 sourceDigest",
      30,
      false
    );
    var revision = safeText(
      value.revision,
      "Reader 范围高亮 revision",
      160,
      false
    );
    var startMarker = safeId(
      value.startMarker,
      "Reader 范围高亮 startMarker"
    );
    var endMarker = safeId(
      value.endMarker,
      "Reader 范围高亮 endMarker"
    );
    if (
      !/^hrs_[0-9a-f]{24}$/.test(snapshotId) ||
      /[\u0000-\u001f\u007f-\u009f]/.test(documentId) ||
      !/^rsd1_[0-9a-f]{8}_[0-9a-f]{16}$/.test(sourceDigest) ||
      (/\s/.test(revision) ||
        /[\u0000-\u001f\u007f-\u009f]/.test(revision)) ||
      !/^m_[0-9a-z]{1,4}$/.test(startMarker) ||
      !/^m_[0-9a-z]{1,4}$/.test(endMarker) ||
      startMarker === endMarker
    ) {
      throw directError(
        "Reader 范围高亮身份无效",
        "BW_READER_REALTIME_OUTPUT_SCHEMA",
        false
      );
    }
    return {
      contract: "reader-source-range/1",
      snapshotId: snapshotId,
      documentId: documentId,
      target: normalizeReaderOutputTarget(value.target),
      sourceDigest: sourceDigest,
      revision: revision,
      startMarker: startMarker,
      endMarker: endMarker,
    };
  }

  function normalizeReaderAnkiDraftCards(cards) {
    if (!Array.isArray(cards) || cards.length < 1 || cards.length > 12) {
      throw directError(
        "Reader Anki 草稿卡片数量无效",
        "BW_READER_REALTIME_OUTPUT_SCHEMA",
        false
      );
    }
    return cards.map(function (card, index) {
      var label = "Reader Anki 草稿 cards[" + index + "]";
      if (!plainObject(card)) {
        throw directError(
          label + " 无效",
          "BW_READER_REALTIME_OUTPUT_SCHEMA",
          false
        );
      }
      var type = safeText(card.type, label + ".type", 16, false);
      if (type === "basic") {
        exactObject(card, ["type", "front", "back"], [], label);
        var front = safeText(card.front, label + ".front", 64000, false);
        if (!front.trim()) {
          throw directError(
            label + ".front 不能为空",
            "BW_READER_REALTIME_OUTPUT_SCHEMA",
            false
          );
        }
        return {
          type: type,
          front: front,
          back: safeText(card.back, label + ".back", 64000, true),
        };
      }
      if (type === "cloze") {
        exactObject(card, ["type", "cloze"], [], label);
        var cloze = safeText(card.cloze, label + ".cloze", 64000, false);
        if (!cloze.trim()) {
          throw directError(
            label + ".cloze 不能为空",
            "BW_READER_REALTIME_OUTPUT_SCHEMA",
            false
          );
        }
        return { type: type, cloze: cloze };
      }
      throw directError(
        label + ".type 无效",
        "BW_READER_REALTIME_OUTPUT_SCHEMA",
        false
      );
    });
    if (new TextEncoder().encode(JSON.stringify(normalized)).byteLength >
        192 * 1024) {
      throw directError(
        "Reader Anki 草稿卡面总量超过 192 KiB 安全上限",
        "BW_READER_REALTIME_OUTPUT_SCHEMA",
        false
      );
    }
    return normalized;
  }

  function normalizeReaderPageCardCards(cards) {
    if (!Array.isArray(cards) || cards.length < 1 || cards.length > 12) {
      throw directError(
        "Reader 页面卡片数量无效",
        "BW_READER_REALTIME_OUTPUT_SCHEMA",
        false
      );
    }
    return cards.map(function (card, index) {
      var label = "Reader 页面卡片 cards[" + index + "]";
      if (!plainObject(card)) {
        throw directError(label + " 无效", "BW_READER_REALTIME_OUTPUT_SCHEMA", false);
      }
      if (card.type === "basic") {
        exactObject(card, ["type", "front", "back"], [], label);
        var front = safeText(
          card.front, label + ".front", LOCAL_PAGE_CARD_CONTEXT_LIMIT, false
        );
        var back = safeText(
          card.back, label + ".back", LOCAL_PAGE_CARD_CONTEXT_LIMIT, false
        );
        if (!front.trim() || !back.trim()) {
          throw directError(label + " 正反面不能为空", "BW_READER_REALTIME_OUTPUT_SCHEMA", false);
        }
        return { type: "basic", front: front, back: back };
      }
      if (card.type === "cloze") {
        exactObject(card, ["type", "cloze"], [], label);
        var cloze = safeText(
          card.cloze, label + ".cloze", LOCAL_PAGE_CARD_CONTEXT_LIMIT, false
        );
        if (!/\{\{c[1-9][0-9]*::[\s\S]+?\}\}/.test(cloze)) {
          throw directError(label + " 缺少有效挖空", "BW_READER_REALTIME_OUTPUT_SCHEMA", false);
        }
        return { type: "cloze", cloze: cloze };
      }
      throw directError(label + ".type 无效", "BW_READER_REALTIME_OUTPUT_SCHEMA", false);
    });
  }

  function normalizeReaderResultDelivery(value) {
    exactObject(
      value,
      ["contract", "correlation", "anchor", "parts"],
      [],
      "Reader 结果事件"
    );
    if (value.contract !== READER_RESULT_DELIVERY_CONTRACT) {
      throw directError(
        "Reader 结果事件合同版本不匹配",
        "BW_READER_RESULT_SCHEMA",
        false
      );
    }
    var correlation = safeId(value.correlation, "Reader 结果 correlation");
    if (correlation.length > 40) {
      throw directError(
        "Reader 结果 correlation 过长",
        "BW_READER_RESULT_SCHEMA",
        false
      );
    }
    exactObject(
      value.anchor,
      ["file", "page"],
      [],
      "Reader 结果 anchor"
    );
    var file = resultText(
      value.anchor.file,
      "Reader 结果 anchor.file",
      true
    );
    if (
      file.indexOf("\0") >= 0 ||
      file.indexOf(":") >= 0 ||
      file.charAt(0) === "/" ||
      file.charAt(0) === "\\" ||
      file.split(/[\\/]/).indexOf("..") >= 0
    ) {
      throw directError(
        "Reader 结果 anchor.file 无效",
        "BW_READER_RESULT_SCHEMA",
        false
      );
    }
    if (
      !Number.isSafeInteger(value.anchor.page) ||
      value.anchor.page < 1
    ) {
      throw directError(
        "Reader 结果 anchor.page 无效",
        "BW_READER_RESULT_SCHEMA",
        false
      );
    }
    if (!Array.isArray(value.parts) || value.parts.length !== 1) {
      throw directError(
        "Reader 结果必须且只能包含一个展示 part",
        "BW_READER_RESULT_SCHEMA",
        false
      );
    }
    var rawPart = value.parts[0];
    var part;
    if (rawPart && rawPart.kind === "card") {
      exactObject(
        rawPart,
        ["kind", "card"],
        [],
        "Reader 结果 part(card)"
      );
      part = {
        kind: "card",
        card: normalizeResultCard(rawPart.card),
      };
    } else if (rawPart && rawPart.kind === "cards") {
      exactObject(
        rawPart,
        ["kind", "cards", "draft"],
        [],
        "Reader 结果 part(cards)"
      );
      if (rawPart.draft !== true) {
        throw directError(
          "Reader 结果 cards 当前只能是草稿",
          "BW_READER_RESULT_SCHEMA",
          false
        );
      }
      part = {
        kind: "cards",
        cards: normalizeResultFlashcards(rawPart.cards),
        draft: true,
      };
    } else {
      throw directError(
        "Reader 结果 part.kind 不受支持",
        "BW_READER_RESULT_SCHEMA",
        false
      );
    }
    return {
      contract: READER_RESULT_DELIVERY_CONTRACT,
      correlation: correlation,
      anchor: {
        file: file,
        page: value.anchor.page,
      },
      parts: [part],
    };
  }

  function normalizeReaderRealtimeOutput(value) {
    exactObject(
      value,
      [
        "contract", "commandKind", "correlation", "sourceInstanceId",
        "snapshotRevision", "file", "page", "kind", "payload",
      ],
      [],
      "Reader 实时输出"
    );
    if (
      value.contract !== READER_REALTIME_OUTPUT_CONTRACT ||
      value.commandKind !== "realtime-output"
    ) {
      throw directError(
        "Reader 实时输出合同无效",
        "BW_READER_REALTIME_OUTPUT_SCHEMA",
        false
      );
    }
    var correlation = safeId(value.correlation, "Reader 输出 correlation");
    var sourceInstanceId = safeId(
      value.sourceInstanceId,
      "Reader 输出 sourceInstanceId"
    );
    if (!Number.isSafeInteger(value.snapshotRevision) || value.snapshotRevision < 0) {
      throw directError(
        "Reader 输出 snapshotRevision 无效",
        "BW_READER_REALTIME_OUTPUT_SCHEMA",
        false
      );
    }
    var file = safeText(value.file, "Reader 输出 file", 4096, false);
    if (/[\u0000-\u001f\u007f-\u009f]/.test(file)) {
      throw directError(
        "Reader 输出 file 无效",
        "BW_READER_REALTIME_OUTPUT_SCHEMA",
        false
      );
    }
    var page = value.page;
    if (
      (typeof page === "number" && (!Number.isSafeInteger(page) || page < 0)) ||
      (typeof page === "string" && (!page || page.length > 256 || /[\u0000-\u001f\u007f]/.test(page))) ||
      (typeof page !== "number" && typeof page !== "string")
    ) {
      throw directError(
        "Reader 输出 page 无效",
        "BW_READER_REALTIME_OUTPUT_SCHEMA",
        false
      );
    }
    var kind = safeText(value.kind, "Reader 输出 kind", 32, false);
    var p = value.payload;
    if (!plainObject(p)) {
      throw directError(
        "Reader 输出 payload 无效",
        "BW_READER_REALTIME_OUTPUT_SCHEMA",
        false
      );
    }
    var payload;
    if (kind === "assistant-turn") {
      exactObject(p, ["threadId", "user", "assistant"], [], "Reader 对话轮");
      payload = {
        threadId: p.threadId === null ? null : safeId(p.threadId, "Reader 对话 threadId"),
        user: safeText(p.user, "Reader 对话 user", 8000, false),
        assistant: safeText(p.assistant, "Reader 对话 assistant", 8000, false),
      };
    } else if (kind === "tool-status") {
      exactObject(p, ["status", "tool", "label", "detail"], [], "Reader 工具状态");
      var status = safeText(p.status, "Reader 工具 status", 16, false);
      if (["running", "done", "error", "aborted"].indexOf(status) < 0) {
        throw directError("Reader 工具状态无效", "BW_READER_REALTIME_OUTPUT_SCHEMA", false);
      }
      payload = {
        status: status,
        tool: safeText(p.tool, "Reader 工具 tool", 160, false),
        label: safeText(p.label, "Reader 工具 label", 320, false),
        detail: p.detail === null ? null : safeText(p.detail, "Reader 工具 detail", 6000, true),
      };
    } else if (kind === "card") {
      exactObject(p, ["card"], [], "Reader 卡片输出");
      var rawCard = p.card;
      // bind 是可选的第四个字段：把卡片钉在正文某一段上。
      //
      // ⚠ 这道闸是这条链上**第四份**卡片字段白名单（前三份：C# ValidatePayload、
      //   MCP inputSchema、服务端 reader_card_contract）。2026-08-19 前三份都
      //   放行了 bind，唯独这里没有 —— 于是助手照说明发出来的卡片被回
      //   BW_READER_REALTIME_OUTPUT_SCHEMA。下游 rc-voicecall 读的就是
      //   `card.bind`（→ __upBindCard / __pageBindCard），链路其余部分都是通的。
      exactObject(rawCard, ["kind", "title", "data"], ["bind"], "Reader 卡片");
      var cardValue = { kind: rawCard.kind, data: rawCard.data };
      if (rawCard.title !== null) cardValue.title = rawCard.title;
      // 这里是**重建**而不是透传，所以放行还不够，必须显式搬过去；
      // 漏搬的表现是"校验通过但卡片不钉"，比直接拒更难查。
      if (
        Object.prototype.hasOwnProperty.call(rawCard, "bind") &&
        rawCard.bind !== null
      ) {
        cardValue.bind = normalizeCardBind(rawCard.bind);
      }
      payload = { card: normalizeResultCard(cardValue) };
    } else if (kind === "navigate") {
      exactObject(p, ["action", "target", "selectionId"], [], "Reader 导航输出");
      var action = safeText(p.action, "Reader 导航 action", 32, false);
      var actions = [
        "next-viewport", "previous-viewport", "scroll-to-text",
        "scroll-to-heading", "scroll-to-selection", "go-to-page", "go-to-section",
      ];
      if (actions.indexOf(action) < 0) {
        throw directError("Reader 导航动作无效", "BW_READER_REALTIME_OUTPUT_SCHEMA", false);
      }
      var target = p.target;
      var selectionId = p.selectionId;
      if (action === "scroll-to-text" || action === "scroll-to-heading") {
        target = safeText(target, "Reader 导航 target", 320, false);
        if (selectionId !== null) throw directError("Reader 导航 selectionId 多余", "BW_READER_REALTIME_OUTPUT_SCHEMA", false);
      } else if (action === "scroll-to-selection") {
        if (target !== null) throw directError("Reader 导航 target 多余", "BW_READER_REALTIME_OUTPUT_SCHEMA", false);
        selectionId = safeId(selectionId, "Reader 导航 selectionId");
      } else if (action === "go-to-page" || action === "go-to-section") {
        if (!Number.isSafeInteger(target) || target < 0 || target > 10000000 || selectionId !== null) {
          throw directError("Reader 导航位置无效", "BW_READER_REALTIME_OUTPUT_SCHEMA", false);
        }
      } else if (target !== null || selectionId !== null) {
        throw directError("Reader 导航参数多余", "BW_READER_REALTIME_OUTPUT_SCHEMA", false);
      }
      payload = { action: action, target: target, selectionId: selectionId };
    } else if (kind === "highlight") {
      exactObject(p, ["color", "note"], [], "Reader 高亮输出");
      var color = safeText(p.color, "Reader 高亮 color", 16, false);
      if (["yellow", "green", "blue", "pink"].indexOf(color) < 0) {
        throw directError("Reader 高亮颜色无效", "BW_READER_REALTIME_OUTPUT_SCHEMA", false);
      }
      payload = {
        color: color,
        note: p.note === null ? null : safeText(p.note, "Reader 高亮 note", 2000, true),
      };
    } else if (kind === "highlight-text") {
      exactObject(
        p,
        ["mutationId", "file", "target", "text", "color", "note"],
        [],
        "Reader 精确高亮输出"
      );
      var mutationId = safeText(
        p.mutationId,
        "Reader 精确高亮 mutationId",
        34,
        false
      );
      if (!/^c_[a-f0-9]{8,32}$/.test(mutationId)) {
        throw directError(
          "Reader 精确高亮 mutationId 无效",
          "BW_READER_REALTIME_OUTPUT_SCHEMA",
          false
        );
      }
      var highlightFile = safeText(
        p.file,
        "Reader 精确高亮 file",
        4096,
        false
      );
      var highlightText = safeText(
        p.text,
        "Reader 精确高亮 text",
        2000,
        false
      );
      if (
        !highlightFile.trim() ||
        /[\u0000-\u001f\u007f-\u009f]/.test(highlightFile) ||
        !highlightText.trim()
      ) {
        throw directError(
          "Reader 精确高亮来源无效",
          "BW_READER_REALTIME_OUTPUT_SCHEMA",
          false
        );
      }
      var exactColor = safeText(
        p.color,
        "Reader 精确高亮 color",
        16,
        false
      );
      if (["yellow", "green", "blue", "pink"].indexOf(exactColor) < 0) {
        throw directError(
          "Reader 精确高亮颜色无效",
          "BW_READER_REALTIME_OUTPUT_SCHEMA",
          false
        );
      }
      payload = {
        mutationId: mutationId,
        file: highlightFile,
        target: normalizeReaderOutputTarget(p.target),
        text: highlightText,
        color: exactColor,
        note: p.note === null
          ? null
          : safeText(p.note, "Reader 精确高亮 note", 2000, true),
      };
    } else if (kind === "highlight-range") {
      exactObject(
        p,
        ["mutationId", "rangeRef", "color", "note"],
        [],
        "Reader 范围高亮输出"
      );
      var rangeMutationId = safeText(
        p.mutationId,
        "Reader 范围高亮 mutationId",
        34,
        false
      );
      if (!/^c_[a-f0-9]{8,32}$/.test(rangeMutationId)) {
        throw directError(
          "Reader 范围高亮 mutationId 无效",
          "BW_READER_REALTIME_OUTPUT_SCHEMA",
          false
        );
      }
      var rangeColor = safeText(
        p.color,
        "Reader 范围高亮 color",
        16,
        false
      );
      if (["yellow", "green", "blue", "pink"].indexOf(rangeColor) < 0) {
        throw directError(
          "Reader 范围高亮颜色无效",
          "BW_READER_REALTIME_OUTPUT_SCHEMA",
          false
        );
      }
      payload = {
        mutationId: rangeMutationId,
        rangeRef: normalizeReaderHighlightRangeRef(p.rangeRef),
        color: rangeColor,
        note: p.note === null
          ? null
          : safeText(p.note, "Reader 范围高亮 note", 2000, true),
      };
    } else if (kind === "anki-draft") {
      var hasDraftFile = Object.prototype.hasOwnProperty.call(p, "file");
      var hasDraftTarget = Object.prototype.hasOwnProperty.call(p, "target");
      var hasDraftSource = Object.prototype.hasOwnProperty.call(p, "sourceText");
      var exactDraftSource = hasDraftFile && hasDraftTarget && hasDraftSource;
      if ((hasDraftFile || hasDraftTarget || hasDraftSource) && !exactDraftSource) {
        throw directError(
          "Reader Anki 引用来源必须同时提供 file/target/sourceText",
          "BW_READER_REALTIME_OUTPUT_SCHEMA",
          false
        );
      }
      exactObject(
        p,
        exactDraftSource
          ? ["draftId", "file", "target", "sourceText", "cards"]
          : ["draftId", "cards"],
        [],
        "Reader Anki 草稿输出"
      );
      var draftId = safeText(p.draftId, "Reader Anki draftId", 160, false);
      if (!/^draft-[a-f0-9]{32}$/.test(draftId)) {
        throw directError(
          "Reader Anki draftId 无效",
          "BW_READER_REALTIME_OUTPUT_SCHEMA",
          false
        );
      }
      payload = {
        draftId: draftId,
        cards: normalizeReaderAnkiDraftCards(p.cards),
      };
      if (exactDraftSource) {
        var draftFile = safeText(p.file, "Reader Anki file", 4096, false);
        var sourceText = safeText(
          p.sourceText,
          "Reader Anki sourceText",
          2000,
          false
        );
        if (
          !draftFile.trim() ||
          /[\u0000-\u001f\u007f-\u009f]/.test(draftFile) ||
          !sourceText.trim()
        ) {
          throw directError(
            "Reader Anki 引用来源无效",
            "BW_READER_REALTIME_OUTPUT_SCHEMA",
            false
          );
        }
        payload.file = draftFile;
        payload.target = normalizeReaderOutputTarget(p.target);
        payload.sourceText = sourceText;
      }
    } else if (kind === "client-action") {
      // 桥接只允许名单内的受信语义入口,逐入口校验。⚠ 这是白名单的第三份副本
      // (Windows C# ValidatePayload / rc-voicecall 执行映射 / 这里),加新入口三处
      // 必须同步——漏这里的实测表现是"接收端 SCHEMA 拒绝,而调用侧一切正常"。
      exactObject(p, ["fn", "args"], [], "Reader 客户端动作");
      var actionFn = safeText(p.fn, "Reader 客户端动作 fn", 64, false);
      if (!Array.isArray(p.args)) {
        throw directError(
          "Reader 客户端动作参数必须是数组",
          "BW_READER_REALTIME_OUTPUT_SCHEMA",
          false
        );
      }
      if (actionFn === "_nativeReaderUndoLast") {
        var undoId = p.args.length === 1
          ? safeText(p.args[0], "Reader 撤销操作编号", 32, false)
          : "";
        if (!/^rundo_[0-9a-f]{24}$/.test(undoId)) {
          throw directError(
            "Reader 撤销操作编号无效",
            "BW_READER_REALTIME_OUTPUT_SCHEMA",
            false
          );
        }
        payload = { fn: actionFn, args: [undoId] };
      } else if (actionFn === "_nativeReaderPageCardMutate") {
        if (p.args.length !== 1 || !plainObject(p.args[0])) {
          throw directError(
            "Reader 页面卡片修改需要一个对象",
            "BW_READER_REALTIME_OUTPUT_SCHEMA",
            false
          );
        }
        var mutation = p.args[0];
        var mutationOperation = safeText(
          mutation.operation, "Reader 页面卡片 operation", 16, false
        );
        var hasMutationNumber = Object.prototype.hasOwnProperty.call(
          mutation, "number"
        );
        exactObject(
          mutation,
          mutationOperation === "edit"
            ? ["operation", "operationId", "expectedId", "expectedRevision", "replacement"]
            : ["operation", "operationId", "expectedId", "expectedRevision"],
          ["number"],
          "Reader 页面卡片修改"
        );
        if (mutationOperation !== "edit" && mutationOperation !== "delete") {
          throw directError("Reader 页面卡片操作无效", "BW_READER_REALTIME_OUTPUT_SCHEMA", false);
        }
        var mutationId = safeText(
          mutation.operationId, "Reader 页面卡片 operationId", 30, false
        );
        var placementId = safeText(
          mutation.expectedId, "Reader 页面卡片 expectedId", 96, false
        );
        if (!/^pcard_[0-9a-f]{24}$/.test(mutationId) ||
            !/^[A-Za-z0-9_-]{2,96}$/.test(placementId) ||
            (hasMutationNumber &&
              (!Number.isSafeInteger(mutation.number) || mutation.number < 1)) ||
            !Number.isSafeInteger(mutation.expectedRevision) ||
            mutation.expectedRevision < 0) {
          throw directError("Reader 页面卡片稳定引用无效", "BW_READER_REALTIME_OUTPUT_SCHEMA", false);
        }
        var normalizedMutation = {
          operation: mutationOperation,
          operationId: mutationId,
          expectedId: placementId,
          expectedRevision: mutation.expectedRevision,
        };
        if (hasMutationNumber) normalizedMutation.number = mutation.number;
        if (mutationOperation === "edit") {
          if (!plainObject(mutation.replacement)) {
            throw directError("Reader 页面卡片替换无效", "BW_READER_REALTIME_OUTPUT_SCHEMA", false);
          }
          if (Object.prototype.hasOwnProperty.call(mutation.replacement, "content")) {
            exactObject(mutation.replacement, ["content"], [], "Reader 页面卡片替换");
            var replacementContent = safeText(
              mutation.replacement.content,
              "Reader 页面卡片 content",
              LOCAL_PAGE_CARD_CONTEXT_LIMIT,
              false
            );
            if (!replacementContent.trim()) {
              throw directError("Reader 页面卡片 content 不能为空", "BW_READER_REALTIME_OUTPUT_SCHEMA", false);
            }
            normalizedMutation.replacement = { content: replacementContent };
          } else {
            exactObject(mutation.replacement, ["cards"], [], "Reader 页面卡片替换");
            normalizedMutation.replacement = {
              cards: normalizeReaderPageCardCards(mutation.replacement.cards),
            };
          }
        }
        payload = { fn: actionFn, args: [normalizedMutation] };
      } else if (actionFn === "_nativeReaderLearningCardMutate") {
        if (p.args.length !== 1 || !plainObject(p.args[0])) {
          throw directError(
            "Reader 学习卡修改需要一个对象",
            "BW_READER_REALTIME_OUTPUT_SCHEMA",
            false
          );
        }
        var learningMutation = p.args[0];
        var learningOperation = safeText(
          learningMutation.operation,
          "Reader 学习卡 operation",
          16,
          false
        );
        exactObject(
          learningMutation,
          learningOperation === "edit"
            ? ["operation", "mutationId", "id", "cardIndex",
              "expectedEntityRev", "externalPolicy"]
            : ["operation", "mutationId", "id", "cardIndex",
              "expectedStateRev", "externalPolicy"],
          learningOperation === "edit" ? ["card", "source"] : [],
          "Reader 学习卡修改"
        );
        if ((learningOperation !== "edit" && learningOperation !== "delete") ||
            !/^lcard_[0-9a-f]{24}$/.test(String(learningMutation.mutationId || "")) ||
            !/^card_[0-9a-f]{4,64}$/.test(String(learningMutation.id || "")) ||
            !Number.isSafeInteger(learningMutation.cardIndex) ||
            learningMutation.cardIndex < 0 || learningMutation.cardIndex > 255 ||
            ["reader-only", "sync-if-projected"]
              .indexOf(learningMutation.externalPolicy) < 0) {
          throw directError(
            "Reader 学习卡稳定引用无效",
            "BW_READER_REALTIME_OUTPUT_SCHEMA",
            false
          );
        }
        var normalizedLearningMutation = {
          operation: learningOperation,
          mutationId: learningMutation.mutationId,
          id: learningMutation.id,
          cardIndex: learningMutation.cardIndex,
          externalPolicy: learningMutation.externalPolicy,
        };
        if (learningOperation === "edit") {
          var learningHasCard = Object.prototype.hasOwnProperty.call(
            learningMutation, "card"
          );
          var learningHasSource = Object.prototype.hasOwnProperty.call(
            learningMutation, "source"
          );
          if (!Number.isSafeInteger(learningMutation.expectedEntityRev) ||
              learningMutation.expectedEntityRev < 0 ||
              (!learningHasCard && !learningHasSource) ||
              (learningHasCard && (!plainObject(learningMutation.card) ||
                messageBytes(JSON.stringify(learningMutation.card)) > 200000))) {
            throw directError(
              "Reader 学习卡编辑内容无效",
              "BW_READER_REALTIME_OUTPUT_SCHEMA",
              false
            );
          }
          normalizedLearningMutation.expectedEntityRev =
            learningMutation.expectedEntityRev;
          if (learningHasCard) {
            normalizedLearningMutation.card = JSON.parse(
              JSON.stringify(learningMutation.card)
            );
          }
          if (learningHasSource) {
            normalizedLearningMutation.source = normalizeLearningCardSource(
              learningMutation.source,
              "Reader 学习卡 source"
            );
          }
        } else {
          if (!Number.isSafeInteger(learningMutation.expectedStateRev) ||
              learningMutation.expectedStateRev < 0) {
            throw directError(
              "Reader 学习卡删除版本无效",
              "BW_READER_REALTIME_OUTPUT_SCHEMA",
              false
            );
          }
          normalizedLearningMutation.expectedStateRev =
            learningMutation.expectedStateRev;
        }
        payload = { fn: actionFn, args: [normalizedLearningMutation] };
      } else if (actionFn === "_nativeReaderWordCardsConsolidate") {
        // 词卡整理（用户 2026-08-31）：{lemma, content} 统一 / {lemma,
        // undo:true} 撤销。二选一，跟 C# 侧 ValidatePayload 同一形状。
        if (p.args.length !== 1 || !plainObject(p.args[0])) {
          throw directError(
            "Reader 词卡整理需要一个对象",
            "BW_READER_REALTIME_OUTPUT_SCHEMA",
            false
          );
        }
        var wcReq = p.args[0];
        var wcHasContent = Object.prototype.hasOwnProperty.call(
          wcReq, "content");
        exactObject(
          wcReq,
          wcHasContent ? ["lemma", "content"] : ["lemma", "undo"],
          [],
          "Reader 词卡整理"
        );
        var wcLemma = safeText(wcReq.lemma, "Reader 词卡 lemma", 64, false)
          .trim().toLowerCase();
        if (!wcLemma) {
          throw directError(
            "Reader 词卡 lemma 无效",
            "BW_READER_REALTIME_OUTPUT_SCHEMA",
            false
          );
        }
        var wcNorm = { lemma: wcLemma };
        if (wcHasContent) {
          var wcContent = safeText(
            wcReq.content, "Reader 词卡 content", 65536, false);
          if (!wcContent.trim()) {
            throw directError(
              "Reader 词卡 content 不能为空",
              "BW_READER_REALTIME_OUTPUT_SCHEMA",
              false
            );
          }
          wcNorm.content = wcContent;
        } else {
          if (wcReq.undo !== true) {
            throw directError(
              "Reader 词卡整理需要 content 或 undo:true 之一",
              "BW_READER_REALTIME_OUTPUT_SCHEMA",
              false
            );
          }
          wcNorm.undo = true;
        }
        payload = { fn: actionFn, args: [wcNorm] };
      } else if (actionFn === "__upStartTask") {
        // 交互练习纸:与 Windows 侧同一套结构闸;内容级容错在纸的接收链里做。
        if (p.args.length !== 1 || !plainObject(p.args[0])) {
          throw directError(
            "Reader 练习纸需要一个任务对象",
            "BW_READER_REALTIME_OUTPUT_SCHEMA",
            false
          );
        }
        var paperSpec = p.args[0];
        exactObject(
          paperSpec,
          ["kind", "title", "paper", "params"],
          [],
          "Reader 练习纸任务"
        );
        if (safeText(paperSpec.kind, "Reader 练习纸 kind", 16, false) !== "free") {
          throw directError(
            "Reader 练习纸只允许 free 任务",
            "BW_READER_REALTIME_OUTPUT_SCHEMA",
            false
          );
        }
        var paperTitle = safeText(paperSpec.title, "Reader 练习纸标题", 120, false);
        var paperPreset = safeText(paperSpec.paper, "Reader 练习纸纸型", 16, false);
        exactObject(
          paperSpec.params,
          ["blocks", "paper", "title"],
          [],
          "Reader 练习纸参数"
        );
        var paperBlocks = paperSpec.params.blocks;
        if (!Array.isArray(paperBlocks) || paperBlocks.length < 1 || paperBlocks.length > 48) {
          throw directError(
            "Reader 练习纸元素必须是 1..48 个",
            "BW_READER_REALTIME_OUTPUT_SCHEMA",
            false
          );
        }
        for (var paperI = 0; paperI < paperBlocks.length; paperI++) {
          if (!plainObject(paperBlocks[paperI])) {
            throw directError(
              "Reader 练习纸元素必须是对象",
              "BW_READER_REALTIME_OUTPUT_SCHEMA",
              false
            );
          }
        }
        payload = {
          fn: actionFn,
          args: [{
            kind: "free",
            title: paperTitle,
            paper: paperPreset,
            params: { blocks: paperBlocks, paper: paperPreset, title: paperTitle },
          }],
        };
      } else {
        throw directError(
          "Reader 客户端动作不在白名单内",
          "BW_READER_REALTIME_OUTPUT_SCHEMA",
          false
        );
      }
    } else {
      throw directError("Reader 输出类型不受支持", "BW_READER_REALTIME_OUTPUT_SCHEMA", false);
    }
    return {
      contract: READER_REALTIME_OUTPUT_CONTRACT,
      correlation: correlation,
      sourceInstanceId: sourceInstanceId,
      snapshotRevision: value.snapshotRevision,
      file: file,
      page: page,
      kind: kind,
      payload: payload,
    };
  }

  function readerRealtimeOutputCorrelation(value) {
    if (!plainObject(value)) return null;
    try {
      return {
        correlation: safeId(
          value.correlation,
          "Reader 输出 correlation"
        ),
        sourceInstanceId: safeId(
          value.sourceInstanceId,
          "Reader 输出 sourceInstanceId"
        ),
      };
    } catch (_) {
      return null;
    }
  }

  function reportReaderRealtimeOutputSchemaFailure(status) {
    try {
      if (typeof window.dlog === "function") {
        window.dlog(
          "Reader 实时输出拒绝: BW_READER_REALTIME_OUTPUT_SCHEMA (" +
          status +
          ")"
        );
      }
    } catch (_) {}
  }

  function normalizeReaderVisualRequest(value) {
    exactObject(
      value,
      [
        "contract",
        "commandKind",
        "correlation",
        "sourceInstanceId",
        "snapshotRevision",
        "file",
        "page",
        "drawingRevision",
        "scope",
        "selectionId",
        "maxBytes",
        "chunkCharacters",
      ],
      [],
      "Reader 笔迹视觉请求"
    );
    if (value.contract !== READER_VISUAL_DELIVERY_CONTRACT) {
      throw directError(
        "Reader 笔迹视觉合同版本不匹配",
        "BW_READER_VISUAL_SCHEMA_INVALID",
        false
      );
    }
    if (value.commandKind !== "capture-composite") {
      throw directError(
        "Reader 视觉 commandKind 不受支持",
        "BW_READER_VISUAL_SCHEMA_INVALID",
        false
      );
    }
    var correlation = safeId(
      value.correlation,
      "Reader 笔迹视觉 correlation"
    );
    var sourceInstanceId = safeId(
      value.sourceInstanceId,
      "Reader 视觉 sourceInstanceId"
    );
    if (
      !Number.isSafeInteger(value.snapshotRevision) ||
      value.snapshotRevision < 0
    ) {
      throw directError(
        "Reader 视觉 snapshotRevision 无效",
        "BW_READER_VISUAL_SCHEMA_INVALID",
        false
      );
    }
    var file = safeText(
      value.file,
      "Reader 笔迹视觉 file",
      4096,
      false
    );
    if (/[\u0000-\u001f\u007f-\u009f]/.test(file)) {
      throw directError(
        "Reader 笔迹视觉 file 无效",
        "BW_READER_VISUAL_SCHEMA_INVALID",
        false
      );
    }
    var page = value.page;
    if (
      (
        typeof page === "number" &&
        (!Number.isSafeInteger(page) || page < 0)
      ) ||
      (
        typeof page === "string" &&
        (
          !page ||
          page.length > 256 ||
          /[\u0000-\u001f\u007f-\u009f]/.test(page)
        )
      ) ||
      (typeof page !== "number" && typeof page !== "string")
    ) {
      throw directError(
        "Reader 笔迹视觉 page 无效",
        "BW_READER_VISUAL_SCHEMA_INVALID",
        false
      );
    }
    var revision = value.drawingRevision === null
      ? null
      : safeId(
        value.drawingRevision,
        "Reader 笔迹视觉 drawingRevision"
      );
    if (
      value.maxBytes !== READER_VISUAL_MAX_BYTES ||
      value.chunkCharacters !== READER_VISUAL_CHUNK_CHARS
    ) {
      throw directError(
        "Reader 笔迹视觉大小合同不匹配",
        "BW_READER_VISUAL_SCHEMA_INVALID",
        false
      );
    }
    var scope = safeText(value.scope, "Reader 笔迹视觉 scope", 32, false);
    var selectionId = null;
    if (!READER_VISUAL_SCOPES[scope]) {
      throw directError(
        "Reader 笔迹视觉 scope 不受支持",
        "BW_READER_VISUAL_SCHEMA_INVALID",
        false
      );
    }
    if (scope === "selection-near") {
      if (value.selectionId === null) {
        throw directError(
          "Reader 选区视觉请求缺少 selectionId",
          "BW_READER_VISUAL_SCHEMA_INVALID",
          false
        );
      }
      selectionId = safeId(
        value.selectionId,
        "Reader 笔迹视觉 selectionId"
      );
    } else if (value.selectionId !== null) {
      throw directError(
        "Reader 非选区视觉请求不能携带 selectionId",
        "BW_READER_VISUAL_SCHEMA_INVALID",
        false
      );
    }
    return {
      contract: READER_VISUAL_DELIVERY_CONTRACT,
      commandKind: "capture-composite",
      correlation: correlation,
      sourceInstanceId: sourceInstanceId,
      snapshotRevision: value.snapshotRevision,
      file: file,
      page: page,
      drawingRevision: revision,
      maxBytes: value.maxBytes,
      chunkCharacters: value.chunkCharacters,
      scoped: true,
      scope: scope,
      selectionId: selectionId,
    };
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
    // Still a fixed allowlist, now with two members rather than one. The second
    // is the context-only endpoint, which accepts concurrent connections and
    // refuses START/STOP -- so admitting it cannot widen what a page may do.
    if (
      url.toString() !== DIRECT_ENDPOINT &&
      url.toString() !== CONTEXT_ENDPOINT
    ) {
      throw directError(
        "只允许已固定的 Windows 电脑语音 WSS 地址",
        "BW_COMPUTER_VOICE_DIRECT_URL",
        false
      );
    }
    return url.toString();
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

  function currentReaderSourceInstanceId() {
    if (!readerSourceInstanceId) readerSourceInstanceId = randomId("source");
    return readerSourceInstanceId;
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
      ["codexVoice"],
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
    if (Object.prototype.hasOwnProperty.call(value, "codexVoice")) {
      normalizeCodexVoicePayload(value.codexVoice, "STATUS Codex 语音");
    }
    return value;
  }

  function normalizeCodexVoicePayload(value, label) {
    exactObject(
      value,
      ["status", "active", "source", "shortcutSent"],
      ["keepActive"],
      label || "Codex 语音响应"
    );
    var status = safeText(
      value.status,
      (label || "Codex 语音响应") + " status",
      32,
      false
    );
    if (
      status !== "available" &&
      status !== "unavailable" &&
      status !== "error"
    ) {
      throw directError(
        "Codex 语音状态不受支持",
        "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
        false
      );
    }
    if (
      (status === "available" && typeof value.active !== "boolean") ||
      (status !== "available" && value.active !== null)
    ) {
      throw directError(
        "Codex 语音 active 字段无效",
        "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
        false
      );
    }
    if (value.source !== null) {
      safeText(
        value.source,
        (label || "Codex 语音响应") + " source",
        96,
        false
      );
    }
    if (typeof value.shortcutSent !== "boolean") {
      throw directError(
        "Codex 语音 shortcutSent 字段无效",
        "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
        false
      );
    }
    if (Object.prototype.hasOwnProperty.call(value, "keepActive")) {
      if (typeof value.keepActive !== "boolean") {
        throw directError(
          "Codex 语音 keepActive 字段无效",
          "BW_COMPUTER_VOICE_DIRECT_SCHEMA",
          false
        );
      }
    } else {
      // Older Windows services remain readable, but the new setting stays
      // visibly unavailable until the service that can enforce it is online.
      value.keepActive = null;
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

  function extensionRuntimeWorld() {
    var runtime = window.chrome && window.chrome.runtime;
    return !!(
      runtime &&
      typeof runtime.id === "string" &&
      runtime.id
    );
  }

  function ownsReaderUi() {
    if (currentOrigin() !== READER_ORIGIN) return true;
    var root = document && document.documentElement;
    var owner = root && root.dataset
      ? String(root.dataset.bwReaderUiOwner || "")
      : "";
    // Only a true PWA book handoff publishes an explicit owner. Ordinary
    // same-origin pages have no owner marker and must keep the extension path.
    if (owner !== "pwa" && owner !== "extension") return true;
    return extensionRuntimeWorld()
      ? owner === "extension"
      : owner === "pwa";
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

  ExtensionRelaySocket.prototype.forceClose = function () {
    if (this.terminal && this.readyState === 3) {
      this._finishClosed();
      return;
    }
    this.terminal = true;
    this.readyState = 3;
    try {
      if (this.port && typeof this.port.disconnect === "function") {
        this.port.disconnect();
      }
    } catch (_) {}
    this._finishClosed();
  };

  // True only for a document served from the extension itself -- popup, options,
  // or a dedicated page. A content script does NOT qualify: it runs under the
  // host page's origin, so the scheme alone separates "our own code" from "a web
  // page we were injected into", which is the distinction that matters here.
  var OWN_EXTENSION_SCHEMES = [
    "safari-web-extension://",
    "chrome-extension://",
    "moz-extension://",
  ];

  // Decided on origin alone. The browser assigns it and no page can forge it,
  // and a content script never carries it -- it runs under the host page's
  // origin -- so the scheme by itself draws the line this needs.
  //
  // runtime.id was required here originally, as corroboration. That was wrong:
  // Safari does not expose it to an extension document embedded in an HTTP(S)
  // page, so the inline computer-voice frame failed this test, fell through to
  // the relay branch, and went back through the very background worker the
  // frame existed to avoid. The call then failed in a way that produced no
  // error anywhere, because the relay simply never answered.
  //
  // The runtime argument stays for callers, but is not consulted.
  function isOwnExtensionPage(_runtime) {
    var origin = currentOrigin();
    if (!origin) return false;
    for (var i = 0; i < OWN_EXTENSION_SCHEMES.length; i += 1) {
      if (origin.indexOf(OWN_EXTENSION_SCHEMES[i]) === 0) return true;
    }
    return false;
  }

  function createDirectTransport(endpoint) {
    var origin = currentOrigin();
    var nativeAppPage = (
      origin === NATIVE_APP_ORIGIN &&
      window.__BW_NATIVE_COMPUTER_VOICE__ === true
    );
    if (origin === READER_ORIGIN || nativeAppPage) {
      if (typeof window.WebSocket !== "function") {
        throw directError(
          nativeAppPage
            ? "原生 App Reader 缺少 WebSocket 能力"
            : "Reader 缺少 WebSocket 能力",
          "BW_COMPUTER_VOICE_DIRECT_OFFLINE",
          true
        );
      }
      // @interaction computer-voice.bridge.request
      return new window.WebSocket(endpoint);
    }
    var runtime = window.chrome && window.chrome.runtime;
    // An extension's own page is the extension's own code, exactly as the
    // background is -- it is not a web page that happens to be injected into.
    // Relaying it through the background buys no additional trust and costs the
    // one thing that matters on iOS, where the background is reclaimed
    // aggressively: the relay port dies mid-session and the call reports only
    // "disconnected", with the bridge never having seen a connection at all.
    // The origin cannot be forged -- the browser assigns it -- and the bridge
    // still enforces its own origin allowlist and Tailscale identity check, so
    // dialling directly from here is no weaker than dialling from the Reader.
    if (isOwnExtensionPage(runtime)) {
      if (typeof window.WebSocket !== "function") {
        throw directError(
          "扩展页缺少 WebSocket 能力",
          "BW_COMPUTER_VOICE_DIRECT_OFFLINE",
          true
        );
      }
      // @interaction computer-voice.bridge.request
      return new window.WebSocket(endpoint);
    }
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
    this.closeResolve = null;
    this.closeTimer = null;
    this.readerVisualPublishedKey = null;
  }

  DirectSocket.prototype.setOptions = function (options) {
    this.options = options || {};
    return this;
  };

  DirectSocket.prototype._finishClose = function () {
    if (this.closeTimer) {
      clearTimeout(this.closeTimer);
      this.closeTimer = null;
    }
    if (this.closeResolve) {
      var resolve = this.closeResolve;
      this.closeResolve = null;
      resolve();
    }
  };

  DirectSocket.prototype._beginCloseWait = function (socket) {
    if (this.closingPromise) return this.closingPromise;
    if (!socket || socket.readyState === 3) {
      this.closingPromise = Promise.resolve();
      this._finishClose();
      return this.closingPromise;
    }
    var self = this;
    this.closingPromise = new Promise(function (resolve) {
      self.closeResolve = resolve;
      self.closeTimer = setTimeout(function () {
        try {
          if (typeof socket.forceClose === "function") socket.forceClose();
        } catch (_) {}
        self._finishClose();
      }, CLOSE_TIMEOUT_MS);
    });
    if (typeof socket.whenClosed === "function") {
      Promise.resolve(socket.whenClosed()).then(
        function () { self._finishClose(); },
        function () { self._finishClose(); }
      );
    }
    return this.closingPromise;
  };

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
    this._beginCloseWait(socket);
    try {
      if (socket && socket.readyState < 2) socket.close(1002, "protocol-error");
    } catch (_) {}
    if (socket && socket.readyState === 3) this._finishClose();
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
        self._finishClose();
        if (self.intentional || self.closed) return;
        self._fail(directError(
          "Windows 桥接器连接已断开",
          "BW_COMPUTER_VOICE_DIRECT_DISCONNECTED",
          true
        ));
      };
    });
  };

  DirectSocket.prototype._handleReaderResult = function (rawPayload) {
    var delivery = normalizeReaderResultDelivery(rawPayload);
    var receipt;
    try {
      if (
        !RC.assistant ||
        typeof RC.assistant.acceptDirectResult !== "function"
      ) {
        throw directError(
          "Reader 结果接收器尚未挂载",
          "BW_READER_RESULT_RECEIVER_UNAVAILABLE",
          true
        );
      }
      receipt = RC.assistant.acceptDirectResult(delivery);
      exactObject(
        receipt,
        ["outcome"],
        ["error"],
        "Reader 结果接收回执"
      );
      if (
        receipt.outcome !== "rendered" &&
        receipt.outcome !== "replay" &&
        receipt.outcome !== "rejected"
      ) {
        throw directError(
          "Reader 结果接收回执 outcome 无效",
          "BW_READER_RESULT_RECEIVER_INVALID",
          false
        );
      }
      if (
        (receipt.outcome === "rejected") !==
        Object.prototype.hasOwnProperty.call(receipt, "error")
      ) {
        throw directError(
          "Reader 拒绝回执必须且只能携带 error",
          "BW_READER_RESULT_RECEIVER_INVALID",
          false
        );
      }
    } catch (error) {
      receipt = {
        outcome: "rejected",
        error: String(
          (error && (error.code || error.message)) ||
          "BW_READER_RESULT_RECEIVER_FAILED"
        ).slice(0, 500),
      };
    }
    var ackFields = {
      correlation: delivery.correlation,
      outcome: receipt.outcome,
    };
    if (receipt.outcome === "rejected") {
      ackFields.error = resultText(
        receipt.error,
        "Reader 结果拒绝原因",
        true
      ).slice(0, 500);
    }
    this.request(
      READER_RESULT_ACK,
      ackFields,
      REQUEST_TIMEOUT_MS
    ).then(function (value) {
      exactObject(
        value,
        ["correlation", "outcome", "matched"],
        [],
        "Reader 结果 ACK 响应"
      );
      if (
        value.correlation !== delivery.correlation ||
        value.outcome !== receipt.outcome ||
        typeof value.matched !== "boolean"
      ) {
        throw directError(
          "Reader 结果 ACK 响应错配",
          "BW_READER_RESULT_ACK_INVALID",
          false
        );
      }
    }).catch(function () {
      // Result delivery is an application-level side channel. The bounded
      // request already reports failure to Windows; never synthesize a second
      // card or a second START from here.
    });
  };

  DirectSocket.prototype._handleReaderRealtimeOutput = function (rawPayload) {
    var self = this;
    var delivery;
    try {
      delivery = normalizeReaderRealtimeOutput(rawPayload);
    } catch (error) {
      var correlation = readerRealtimeOutputCorrelation(rawPayload);
      var sourceMatches = !!(
        correlation &&
        self.readerVisualSourceId &&
        self.readerVisualSourceId === correlation.sourceInstanceId
      );
      var canReply = !!(sourceMatches && self.readerVisualSessionId);
      reportReaderRealtimeOutputSchemaFailure(
        canReply
          ? "正在回关联拒绝回执"
          : (correlation
            ? (sourceMatches
              ? "当前 Reader 会话不存在,未回执"
              : "当前 Reader 来源不匹配,未回执")
            : "身份不可安全关联,未回执")
      );
      if (canReply) {
        self.request(READER_REALTIME_OUTPUT_ACK, {
          sessionId: self.readerVisualSessionId,
          correlation: correlation.correlation,
          sourceInstanceId: correlation.sourceInstanceId,
          outcome: "rejected",
          error: "BW_READER_REALTIME_OUTPUT_SCHEMA",
        }, REQUEST_TIMEOUT_MS).then(function (value) {
          exactObject(
            value,
            ["correlation", "outcome", "matched"],
            [],
            "Reader 非法输出拒绝 ACK 响应"
          );
          if (
            value.correlation !== correlation.correlation ||
            value.outcome !== "rejected" ||
            value.matched !== true
          ) {
            throw directError(
              "Reader 非法输出拒绝 ACK 响应错配",
              "BW_READER_REALTIME_OUTPUT_ACK_INVALID",
              false
            );
          }
          reportReaderRealtimeOutputSchemaFailure("已回关联拒绝回执");
        }).catch(function () {
          // The malformed output was never executed. Make an ACK transport
          // failure visible, while Windows remains the owner of timeout and
          // retry policy. Never execute or resend the mutation from here.
          reportReaderRealtimeOutputSchemaFailure(
            "关联拒绝回执失败 BW_READER_REALTIME_OUTPUT_ACK_FAILED"
          );
        });
      }
      return;
    }
    var receiver = RC.voicecall && RC.voicecall.acceptRealtimeOutput;
    Promise.resolve().then(function () {
      if (!readerRealtimeOutputMatchesLive(delivery, self)) {
        throw directError(
          "Reader 实时输出对应页面已经变化",
          "BW_READER_REALTIME_OUTPUT_STALE",
          true
        );
      }
      if (typeof receiver !== "function") {
        throw directError(
          "Reader 实时输出接收器尚未挂载",
          "BW_READER_REALTIME_OUTPUT_RECEIVER_UNAVAILABLE",
          true
        );
      }
      return receiver(delivery);
    }).then(function (receipt) {
      // bindOutcome / bindReason：卡片钉在正文上没有、没钉上是为什么。
      //
      // ⚠ 它们**不能复用 error** —— 下面那条不变式要求 error 当且仅当
      //   outcome==='rejected' 时存在，而「退回浮层」是 applied（卡确实送到了，
      //   只是没钉上）。所以必须是独立字段。
      // ⚠ 这一处必须与 rc-voicecall.js 那边**同一次提交、同一次投递**：
      //   只要这里没放行，上游一带新字段，这个 exactObject 就抛，被下面的
      //   catch 整个换成 rejected —— 表现是「一次成功的绑定被回成失败」，
      //   而链路上没有任何一处出声。
      exactObject(receipt, ["outcome"], ["error", "bindOutcome", "bindReason"],
        "Reader 输出回执");
      if (["applied", "replay", "rejected"].indexOf(receipt.outcome) < 0) {
        throw directError(
          "Reader 输出回执 outcome 无效",
          "BW_READER_REALTIME_OUTPUT_RECEIVER_INVALID",
          false
        );
      }
      if ((receipt.outcome === "rejected") !== Object.prototype.hasOwnProperty.call(receipt, "error")) {
        throw directError(
          "Reader 输出拒绝回执必须且只能携带 error",
          "BW_READER_REALTIME_OUTPUT_RECEIVER_INVALID",
          false
        );
      }
      return receipt;
    }).catch(function (error) {
      // 这个兜底本身是对的，但它会把**上面任何一处校验失败**都变成一条
      // 看起来像"执行失败"的 rejected。白名单漏配时症状正是如此，而且链路上
      // 没有任何一处出声 —— 所以这里至少留一句可见诊断。
      try {
        console.warn("[reader-output] 回执被判无效 →",
          (error && (error.code || error.message)) || error,
          "correlation=" + delivery.correlation);
      } catch (e) {}
      return {
        outcome: "rejected",
        error: String(
          (error && (error.code || error.message)) ||
          "BW_READER_REALTIME_OUTPUT_RECEIVER_FAILED"
        ).slice(0, 500),
      };
    }).then(function (receipt) {
      if (!self.readerVisualSessionId) return null;
      // ⚠ 这里是**重建**不是透传：放行了还要显式搬。只放行不搬的表现是
      //   「校验全过、就是不生效」，比被拒难查得多。
      return self.request(READER_REALTIME_OUTPUT_ACK, {
        sessionId: self.readerVisualSessionId,
        correlation: delivery.correlation,
        sourceInstanceId: delivery.sourceInstanceId,
        outcome: receipt.outcome,
        error: receipt.outcome === "rejected" ? receipt.error : null,
        bindOutcome: receipt.bindOutcome || null,
        bindReason: receipt.bindReason || null,
      }, REQUEST_TIMEOUT_MS);
    }).catch(function () {
      // The Windows broker owns retry/reporting. Never render or execute twice.
    });
  };

  DirectSocket.prototype._sendReaderVisualPart = function (
    request,
    fields
  ) {
    var identity = {
      sessionId: this.readerVisualSessionId,
      correlation: request.correlation,
      sourceInstanceId: request.sourceInstanceId,
      snapshotRevision: request.snapshotRevision,
      file: request.file,
      page: request.page,
      drawingRevision: request.drawingRevision,
      scope: request.scope,
      selectionId: request.selectionId,
    };
    var payload = Object.assign(identity, fields);
    return this.request(
      READER_VISUAL_CHUNK,
      payload,
      REQUEST_TIMEOUT_MS
    ).then(function (value) {
      exactObject(
        value,
        ["correlation", "chunkIndex", "accepted", "complete"],
        [],
        "Reader 笔迹视觉回执"
      );
      if (
        value.correlation !== request.correlation ||
        value.chunkIndex !== payload.chunkIndex ||
        value.accepted !== true ||
        typeof value.complete !== "boolean"
      ) {
        throw directError(
          "Reader 笔迹视觉回执错配",
          "BW_READER_VISUAL_ACK_INVALID",
          false
        );
      }
      return value;
    });
  };

  DirectSocket.prototype._declineReaderVisual = function (request) {
    return this._sendReaderVisualPart(request, {
      status: "unavailable",
      mimeType: "",
      chunkIndex: 0,
      chunkCount: 0,
      totalBytes: 0,
      data: "",
    });
  };

  function readerVisualDrawingMatches(request) {
    var drawing;
    var outgoingState;
    try {
      drawing = RC.outgoing &&
        typeof RC.outgoing.lastDrawing === "function"
        ? RC.outgoing.lastDrawing()
        : null;
      outgoingState = RC.outgoing &&
        typeof RC.outgoing._state === "function"
        ? RC.outgoing._state()
        : null;
    } catch (_) {
      drawing = null;
      outgoingState = null;
    }
    return !!(
      plainObject(drawing) &&
      plainObject(outgoingState) &&
      !outgoingState.drawPend &&
      !outgoingState.drawTimer &&
      outgoingState.inflight !== true &&
      drawing.file === request.file &&
      sameActiveScalar(drawing.page, request.page) &&
      drawing.stable === true &&
      drawing.empty === false &&
      drawing.drawingRevision === request.drawingRevision
    );
  }

  function readerVisualIdentityKey(
    file,
    page,
    drawingRevision,
    scope,
    selectionId
  ) {
    return String(file) + "\n" + String(page) + "\n" +
      String(drawingRevision) + "\n" + String(scope || "legacy") + "\n" +
      String(selectionId || "");
  }

  function readerVisualRequestKey(request) {
    return readerVisualIdentityKey(
      request.file,
      request.page,
      request.drawingRevision,
      request.scope,
      request.selectionId
    );
  }

  function readerVisualCacheMatches(request) {
    return !!(
      readerVisualCache &&
      readerVisualCache.key === readerVisualRequestKey(request)
    );
  }

  function currentReaderVisualChannel() {
    if (contextDeliveryMode !== CONTEXT_DELIVERY_SNAPSHOT) return null;
    if (
      snapshotLink &&
      !snapshotLink.stopped &&
      directChannelLive(snapshotLink.channel) &&
      snapshotLink.channel.readerVisualSourceId ===
        currentReaderSourceInstanceId()
    ) {
      return snapshotLink.channel;
    }
    return null;
  }

  function readerVisualRequestMatchesLive(request) {
    var current = localActiveReadingSnapshot();
    if (
      contextDeliveryMode !== CONTEXT_DELIVERY_SNAPSHOT ||
      !current ||
      current.sourceInstanceId !== request.sourceInstanceId ||
      current.file !== request.file ||
      !sameActiveScalar(current.page, request.page)
    ) {
      return false;
    }
    if (request.scope === "viewport-context") return true;
    return request.drawingRevision !== null &&
      readerVisualDrawingMatches(request);
  }

  function readerRealtimeOutputMatchesLive(delivery, channel) {
    var current = localActiveReadingSnapshot();
    // page-chars 已经携带自己的目标页和原始字符区间。只要还是同一本书，
    // 它就能由 __pageBindPersist 在目标页未渲染时写成 deferred PDF anchor；
    // 不应再拿生成快照时的可见页阻挡后台/跨页绑定。其它输出继续要求页面
    // 完全一致，避免导航、高亮或按当前页序号修改卡片时误投。
    var card = delivery && delivery.kind === "card" && delivery.payload &&
      delivery.payload.card;
    var bind = card && card.bind;
    var action = delivery && delivery.kind === "client-action" &&
      delivery.payload;
    var documentScopedMutation = !!(
      (bind && bind.kind === "page-chars") ||
      (action && action.fn === "_nativeReaderLearningCardMutate")
    );
    return !!(
      contextDeliveryMode === CONTEXT_DELIVERY_SNAPSHOT &&
      channel &&
      channel.readerVisualSessionId &&
      channel.readerVisualSourceId === delivery.sourceInstanceId &&
      current &&
      current.sourceInstanceId === delivery.sourceInstanceId &&
      current.file === delivery.file &&
      (sameActiveScalar(current.page, delivery.page) ||
        documentScopedMutation)
    );
  }

  function captureReaderVisual(request) {
    if (request.scoped === true) {
      var scopedTarget = {
        page: request.page,
        scope: request.scope,
      };
      if (request.selectionId) {
        scopedTarget.selectionId = request.selectionId;
      }
      if (request.scope === "viewport-context") {
        return RC.capturePageComposite &&
          typeof RC.capturePageComposite === "function"
          ? Promise.resolve(RC.capturePageComposite(scopedTarget)).catch(
            function () {
              return RC.captureView && typeof RC.captureView === "function"
                ? RC.captureView()
                : null;
            }
          )
          : Promise.resolve(
            RC.captureView && typeof RC.captureView === "function"
              ? RC.captureView()
              : null
          );
      }
      return RC.captureInkRegion &&
        typeof RC.captureInkRegion === "function"
        ? Promise.resolve(RC.captureInkRegion(scopedTarget)).catch(
          function () { return null; }
        )
        : Promise.resolve(null);
    }
    function captureLegacy() {
      if (
        RC.captureInkRegion &&
        typeof RC.captureInkRegion === "function"
      ) {
        return Promise.resolve(RC.captureInkRegion({
          page: request.page,
        })).then(function (shot) {
          if (shot) return shot;
          return RC.captureView && typeof RC.captureView === "function"
            ? RC.captureView()
            : null;
        });
      }
      return RC.captureView && typeof RC.captureView === "function"
        ? RC.captureView()
        : null;
    }
    if (
      RC.capturePageComposite &&
      typeof RC.capturePageComposite === "function"
    ) {
      return Promise.resolve(RC.capturePageComposite({
        page: request.page,
      })).then(function (shot) {
        return shot || captureLegacy();
      }, function () {
        return captureLegacy();
      });
    }
    return Promise.resolve(captureLegacy());
  }

  function ensureReaderVisual(request) {
    var key = readerVisualRequestKey(request);
    if (request.scoped !== true && readerVisualCacheMatches(request)) {
      return Promise.resolve(readerVisualCache.shot);
    }
    if (
      readerVisualCapturePromise &&
      readerVisualCaptureKey === key
    ) {
      return readerVisualCapturePromise;
    }
    var generation = readerVisualGeneration;
    var capturePromise = Promise.resolve().then(function () {
      if (!readerVisualRequestMatchesLive(request)) {
        return null;
      }
      return captureReaderVisual(request);
    }).then(function (shot) {
      if (
        generation !== readerVisualGeneration ||
        !readerVisualRequestMatchesLive(request)
      ) {
        return null;
      }
      if (!shot) return null;
      // The proactive cache has the legacy "whole page" wire shape. A scoped
      // crop must never replace it, otherwise a later reconnect could publish
      // a selection crop as though it were the legacy page composite.
      if (request.scoped !== true) {
        readerVisualCache = {
          key: key,
          file: request.file,
          page: request.page,
          drawingRevision: request.drawingRevision,
          shot: shot,
        };
      }
      return shot;
    }).finally(function () {
      if (readerVisualCapturePromise === capturePromise) {
        readerVisualCapturePromise = null;
        readerVisualCaptureKey = null;
      }
    });
    readerVisualCaptureKey = key;
    readerVisualCapturePromise = capturePromise;
    return capturePromise;
  }

  DirectSocket.prototype._sendReaderVisualShot = function (
    request,
    shot
  ) {
    var self = this;
    var b64 = shot && shot.media_type === "image/jpeg"
      ? shot.b64
      : "";
    if (
      typeof b64 !== "string" ||
      !b64 ||
      b64.length % 4 !== 0 ||
      b64.length > (
        READER_VISUAL_CHUNK_CHARS * READER_VISUAL_MAX_CHUNKS
      ) ||
      !/^[A-Za-z0-9+/]+={0,2}$/.test(b64)
    ) {
      return self._declineReaderVisual(request);
    }
    var padding = b64.endsWith("==")
      ? 2
      : b64.endsWith("=")
        ? 1
        : 0;
    var totalBytes = (b64.length / 4) * 3 - padding;
    var chunkCount = Math.ceil(
      b64.length / READER_VISUAL_CHUNK_CHARS
    );
    if (
      totalBytes < 1 ||
      totalBytes > request.maxBytes ||
      chunkCount < 1 ||
      chunkCount > READER_VISUAL_MAX_CHUNKS
    ) {
      return self._declineReaderVisual(request);
    }
    var chain = Promise.resolve();
    for (var index = 0; index < chunkCount; index += 1) {
      (function (chunkIndex) {
        var data = b64.slice(
          chunkIndex * READER_VISUAL_CHUNK_CHARS,
          (chunkIndex + 1) * READER_VISUAL_CHUNK_CHARS
        );
        chain = chain.then(function () {
          return self._sendReaderVisualPart(request, {
            status: "chunk",
            mimeType: "image/jpeg",
            chunkIndex: chunkIndex,
            chunkCount: chunkCount,
            totalBytes: totalBytes,
            data: data,
          });
        });
      })(index);
    }
    return chain;
  };

  function proactiveReaderVisualRequest(cache) {
    return {
      contract: READER_VISUAL_DELIVERY_CONTRACT,
      correlation: randomId("publish"),
      file: cache.file,
      page: cache.page,
      drawingRevision: cache.drawingRevision,
      maxBytes: READER_VISUAL_MAX_BYTES,
      chunkCharacters: READER_VISUAL_CHUNK_CHARS,
    };
  }

  function publishCachedReaderVisual(channel) {
    var cache = readerVisualCache;
    if (
      !cache ||
      !channel ||
      !directChannelLive(channel) ||
      channel.readerVisualPublishedKey === cache.key
    ) {
      return Promise.resolve(null);
    }
    var request = proactiveReaderVisualRequest(cache);
    return channel._sendReaderVisualShot(request, cache.shot).then(function () {
      if (
        readerVisualCache === cache &&
        directChannelLive(channel)
      ) {
        channel.readerVisualPublishedKey = cache.key;
      }
      return cache;
    }).catch(function () {
      // 缓存仍保留；重连或 AI 随后的按需请求可以再次取用。
      return null;
    });
  }

  function invalidateReaderVisual(sendRemote) {
    var cache = readerVisualCache;
    var channel = currentReaderVisualChannel();
    readerVisualGeneration += 1;
    readerVisualCache = null;
    readerVisualCaptureKey = null;
    readerVisualCapturePromise = null;
    if (
      sendRemote !== true ||
      !cache ||
      !channel ||
      !directChannelLive(channel)
    ) {
      return;
    }
    var request = proactiveReaderVisualRequest(cache);
    channel._declineReaderVisual(request).catch(function () {});
    if (channel.readerVisualPublishedKey === cache.key) {
      channel.readerVisualPublishedKey = null;
    }
  }

  function observeReaderVisualPage(current) {
    if (!current) return;
    var pageKey = String(current.file) + "\n" + String(current.page);
    if (readerVisualPageKey === null) {
      readerVisualPageKey = pageKey;
      return;
    }
    if (readerVisualPageKey === pageKey) return;
    readerVisualPageKey = pageKey;
    invalidateReaderVisual(true);
  }

  function readerVisualStableRequest(detail) {
    if (
      !plainObject(detail) ||
      typeof detail.file !== "string" ||
      !detail.file ||
      (
        typeof detail.page !== "number" &&
        typeof detail.page !== "string"
      ) ||
      typeof detail.drawingRevision !== "string"
    ) {
      return null;
    }
    try {
      return normalizeReaderVisualRequest({
        contract: READER_VISUAL_DELIVERY_CONTRACT,
        correlation: randomId("publish"),
        file: detail.file,
        page: detail.page,
        drawingRevision: detail.drawingRevision,
        maxBytes: READER_VISUAL_MAX_BYTES,
        chunkCharacters: READER_VISUAL_CHUNK_CHARS,
      });
    } catch (_) {
      return null;
    }
  }

  function prepareStableReaderVisual(detail) {
    var request = readerVisualStableRequest(detail);
    if (!request) return;
    // rc-core 在确认响应的 then 中发事件，its finally 才会清 inflight。
    // 让出一个任务后再校验，避免把刚确认的稳定笔迹误判成仍在绘制。
    setTimeout(function () {
      ensureReaderVisual(request).then(function (shot) {
        if (!shot || !readerVisualCacheMatches(request)) return;
        return publishCachedReaderVisual(currentReaderVisualChannel());
      }).catch(function () {});
    }, 0);
  }

  function primeReaderVisualFromOutgoing() {
    var drawing = null;
    try {
      drawing = RC.outgoing &&
        typeof RC.outgoing.lastDrawing === "function"
        ? RC.outgoing.lastDrawing()
        : null;
    } catch (_) {}
    if (
      plainObject(drawing) &&
      drawing.stable === true &&
      drawing.empty === false
    ) {
      prepareStableReaderVisual(drawing);
    } else {
      publishCachedReaderVisual(currentReaderVisualChannel());
    }
  }

  if (document && typeof document.addEventListener === "function") {
    document.addEventListener(READER_DRAWING_STATE_EVENT, function (event) {
      var detail = event && event.detail;
      if (!plainObject(detail)) return;
      if (detail.state === "changed" || detail.state === "empty") {
        invalidateReaderVisual(true);
        return;
      }
      if (detail.state === "stable") {
        prepareStableReaderVisual(detail);
      }
    });
  }

  // 桥接问，本机答。名单在这里再卡一次 —— 协议侧已经卡过，但这一侧才是真正
  // 去调函数的地方，只有这里的名单能保证"名单外的名字调不到任何东西"。
  /* 网页高亮的查询实现（扩展宿主）。
   *
   * 只回助手用得上的字段。prefix/suffix 是锚定用的上下文片段，几十字符一条，
   * 助手拿它做不了什么，却会让结果体积翻倍 —— 与书籍高亮不回 rects 同理。
   *
   * 装不下时截断并如实标注：一个被悄悄截短的列表跟完整的长得一样，
   * 助手会据此报一个总数。 */
  function _webHighlightsQuery(web, params) {
    var all;
    try {
      all = web.list() || [];
    } catch (error) {
      return Promise.reject(error);
    }
    var needle = String(params && params.contains || "").trim().toLowerCase();
    var matched = [];
    for (var i = 0; i < all.length; i += 1) {
      var item = all[i];
      if (!item || typeof item !== "object") continue;
      var text = String(item.text == null ? "" : item.text);
      if (needle && text.toLowerCase().indexOf(needle) < 0) continue;
      matched.push({
        id: String(item.id == null ? "" : item.id),
        text: text.length > 600 ? text.slice(0, 600) : text,
        color: String(item.color == null ? "" : item.color),
        note: String(item.note == null ? "" : item.note).slice(0, 600),
        kind: String(item.kind == null ? "" : item.kind),
        time: Number(item.time) || 0,
      });
    }
    matched.sort(function (a, b) { return (a.time || 0) - (b.time || 0); });
    var budget = 32 * 1024;
    var used = 0;
    var kept = [];
    var truncated = false;
    for (var m = 0; m < matched.length; m += 1) {
      var size = JSON.stringify(matched[m]).length + 1;
      if (used + size > budget) { truncated = true; break; }
      used += size;
      kept.push(matched[m]);
    }
    return Promise.resolve({
      ok: true,
      surface: "web",
      url: String((all[0] && all[0].url) || location.href || ""),
      highlights: kept,
      matched: matched.length,
      returned: kept.length,
      truncated: truncated,
    });
  }

  /* 网页便签的查询实现（扩展宿主）。
   *
   * 便签在扩展的 document-notes scoped repository 里，读要经 background，
   * 所以这里是异步的 —— 与高亮（页面内同步 list）不同，失败方式也多一种：
   * 仓库尚未 READY。那种情况必须如实抛出，不能当成"没有便签"：
   * 空列表会被读成"这个网页你没记过东西"，那是一句错的断言。
   *
   * 锚点（anchor）不回给助手：它是页面坐标与 DOM 位置，助手用不上，
   * 却会让每条记录大出好几倍 —— 与高亮不回 prefix/suffix 同理。 */
  function _webNotesQuery(repository, params) {
    return Promise.resolve()
      .then(function () { return repository.list({}); })
      .then(function (result) {
        var all = result && Array.isArray(result.notes)
          ? result.notes
          : (Array.isArray(result) ? result : []);
        var needle = String(params && params.contains || "")
          .trim().toLowerCase();
        var matched = [];
        for (var i = 0; i < all.length; i += 1) {
          var item = all[i];
          if (!item || typeof item !== "object") continue;
          var text = String(item.text == null ? "" : item.text);
          if (needle && text.toLowerCase().indexOf(needle) < 0) continue;
          matched.push({
            id: String(item.id == null ? "" : item.id),
            text: text.length > 1000 ? text.slice(0, 1000) : text,
            color: String(item.color == null ? "" : item.color),
            time: Number(item.updated || item.created || item.time) || 0,
          });
        }
        matched.sort(function (a, b) { return (a.time || 0) - (b.time || 0); });
        var budget = 32 * 1024;
        var used = 0;
        var kept = [];
        var truncated = false;
        for (var m = 0; m < matched.length; m += 1) {
          var size = JSON.stringify(matched[m]).length + 1;
          if (used + size > budget) { truncated = true; break; }
          used += size;
          kept.push(matched[m]);
        }
        return {
          ok: true,
          surface: "web",
          url: String(location.href || ""),
          notes: kept,
          matched: matched.length,
          returned: kept.length,
          truncated: truncated,
        };
      });
  }

  var READER_QUERY_HANDLERS = {
    highlights: function (params) {
      // 两种宿主各答各的：书籍走 App 的本机运行时，普通网页走扩展自己的
      // 本地存储（webHighlightsV1）。谁都不在就回 null → unsupported，
      // 不去猜另一边 —— 答错宿主的数据比答不上来更糟。
      var target = window._nativeReaderHighlights;
      if (typeof target === "function") {
        return target.call(window, {
          page: params.page,
          contains: params.contains,
        });
      }
      var web = window.__bwWebHighlights;
      if (web && typeof web.list === "function") {
        return _webHighlightsQuery(web, params);
      }
      return null;
    },
    notes: function (params) {
      // 与 highlights 同构：书里走 App 本机运行时，网页走扩展的
      // __bwDocumentNotes（scoped repository，同样不经 Pi）。
      var target = window._nativeReaderNotes;
      if (typeof target === "function") {
        return target.call(window, {
          page: params.page,
          contains: params.contains,
        });
      }
      var repository = window.__bwDocumentNotes;
      if (repository && typeof repository.list === "function") {
        return _webNotesQuery(repository, params);
      }
      return null;
    },
    search: function (params) {
      var target = window._nativeReaderSearch;
      if (typeof target !== "function") return null;
      return target.call(window, {
        query: params.query,
        limit: params.limit,
      });
    },
    toc: function () {
      var target = window._nativeReaderToc;
      if (typeof target !== "function") return null;
      return target.call(window);
    },
    "page-text": function (params) {
      // 与 highlights / notes 同构：书里走 App 本机运行时，网页走扩展的
      // __bwWebPageText（字符层来自 web-textlayer，同样不经 Pi）。
      var target = window._nativeReaderPageText;
      if (typeof target === "function") {
        // ⚠ contains 必须透传。不传的话 C# 侧放行了、页面侧却丢掉，
        //   表现是"参数写了但没生效" —— 整页照旧返回，没有一处报错。
        return target.call(window, {
          page: params.page,
          contains: params.contains
        });
      }
      var web = window.__bwWebPageText;
      if (web && typeof web.read === "function") return web.read(params || {});
      return null;
    },
    "word-cards": function (params) {
      // 一轮拿全（judgment_basis 同一条教义：依据不该让 AI 多轮自己拼）。
      // 路由只在 App 本地 runtime 存在；别的宿主 404 → null = unsupported。
      var wcLemma = String((params && params.lemma) || "")
        .trim().toLowerCase();
      if (!wcLemma || wcLemma.length > 64) return null;
      // @interaction wordcard.index.sync
      return fetch(
        "/pdf/api/word-card-index?lemma=" + encodeURIComponent(wcLemma)
      ).then(function (r) {
        return r.ok ? r.json() : null;
      }).then(function (d) {
        if (!d || d.ok !== true) return null;
        return { lemma: d.lemma, cards: d.cards || [] };
      }).catch(function () { return null; });
    },
    "page-cards": function (params) {
      var page = params && Object.prototype.hasOwnProperty.call(params, "page")
        ? params.page : null;
      if (page === null || page === undefined) {
        // Keep the query channel self-contained for hosts that replace the
        // exported pageCards function with a capability stub.
        var current = localActiveReadingSnapshot();
        if (!current || current.kind !== "pdf") return null;
        page = current.page;
      }
      return pageCardIndex(page);
    },
    "page-card": function (params) {
      return pageCard(params);
    },
    "learning-cards": function (params) {
      return learningCards(params);
    },
    "learning-card": function (params) {
      return learningCard(params);
    },
    "review-current": function () {
      return currentReviewCard();
    },
    lookup: function (params) {
      var target = window._nativeReaderLookupWord;
      if (typeof target !== "function") return null;
      return target.call(window, { word: params.word });
    },
  };

  function normalizeReaderQueryRequest(rawPayload) {
    var value = rawPayload;
    exactObject(
      value,
      [
        "contract",
        "commandKind",
        "correlation",
        "sourceInstanceId",
        "snapshotRevision",
        "file",
        "query",
        "params",
      ],
      [],
      "Reader 查询请求"
    );
    if (
      value.contract !== READER_QUERY_CONTRACT ||
      value.commandKind !== "query"
    ) {
      throw directError(
        "Reader 查询请求合同无效",
        "BW_READER_QUERY_SCHEMA",
        false
      );
    }
    var query = safeText(value.query, "query", 64, false);
    if (!Object.prototype.hasOwnProperty.call(READER_QUERY_HANDLERS, query)) {
      throw directError(
        "Reader 查询名称不在名单内",
        "BW_READER_QUERY_SCHEMA",
        false
      );
    }
    if (
      !value.params ||
      typeof value.params !== "object" ||
      Array.isArray(value.params)
    ) {
      throw directError(
        "Reader 查询参数无效",
        "BW_READER_QUERY_SCHEMA",
        false
      );
    }
    if (
      !Number.isSafeInteger(value.snapshotRevision) ||
      value.snapshotRevision < 0
    ) {
      throw directError(
        "Reader 查询 snapshotRevision 无效",
        "BW_READER_QUERY_SCHEMA",
        false
      );
    }
    return {
      correlation: safeId(value.correlation, "correlation"),
      sourceInstanceId: safeId(value.sourceInstanceId, "sourceInstanceId"),
      snapshotRevision: value.snapshotRevision,
      file: safeText(value.file, "file", 4096, false),
      query: query,
      params: value.params,
    };
  }

  DirectSocket.prototype._sendReaderQueryResult = function (
    request,
    status,
    result,
    truncated
  ) {
    return this.request(
      READER_QUERY_RESPONSE,
      {
        sessionId: this.readerVisualSessionId,
        correlation: request.correlation,
        sourceInstanceId: request.sourceInstanceId,
        snapshotRevision: request.snapshotRevision,
        file: request.file,
        query: request.query,
        status: status,
        result: result,
        truncated: truncated,
      },
      REQUEST_TIMEOUT_MS
    );
  };

  DirectSocket.prototype._handleReaderQuery = function (rawPayload) {
    var request = normalizeReaderQueryRequest(rawPayload);
    var self = this;
    var handler = READER_QUERY_HANDLERS[request.query];
    var work;
    try {
      work = handler(request.params);
    } catch (error) {
      work = Promise.reject(error);
    }
    // 界面做不到跟这次没答上来，是两种不同的事实。都答成空列表，助手就会
    // 替用户断定「你没有划过」—— 宁可让它知道没答上来。
    if (work === null) {
      this._sendReaderQueryResult(request, "unsupported", {}, false)
        .catch(function () {});
      return;
    }
    Promise.resolve(work).then(function (value) {
      var payload = value && typeof value === "object" ? value : {};
      return self._sendReaderQueryResult(
        request,
        "ok",
        payload,
        payload.truncated === true
      );
    }).catch(function () {
      return self._sendReaderQueryResult(request, "unavailable", {}, false);
    }).catch(function () {
      // 回传本身失败时由 Windows 侧的超时收尾，这里不重试也不再报第二次。
    });
  };

  DirectSocket.prototype._handleReaderVisual = function (rawPayload) {
    var request = normalizeReaderVisualRequest(rawPayload);
    var self = this;
    if (this.readerVisualBusy) {
      this._declineReaderVisual(request).catch(function () {});
      return;
    }
    this.readerVisualBusy = true;
    ensureReaderVisual(request).then(function (shot) {
      return shot
        ? self._sendReaderVisualShot(request, shot)
        : self._declineReaderVisual(request);
    }).catch(function () {
      // The MCP call is read-only and can still return the text snapshot.
      // Never retry capture or send a START from this side channel.
    }).finally(function () {
      self.readerVisualBusy = false;
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
        if (message.event === READER_RESULT_EVENT) {
          this._handleReaderResult(message.payload);
          return;
        }
        if (message.event === READER_VISUAL_EVENT) {
          this._handleReaderVisual(message.payload);
          return;
        }
        if (message.event === READER_QUERY_EVENT) {
          this._handleReaderQuery(message.payload);
          return;
        }
        if (message.event === READER_REALTIME_OUTPUT_EVENT) {
          this._handleReaderRealtimeOutput(message.payload);
          return;
        }
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
    this._beginCloseWait(socket);
    try {
      if (socket && socket.readyState < 2) {
        socket.close(1000, "client-stop");
      }
    } catch (_) {}
    if (socket && socket.readyState === 3) this._finishClose();
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

  function emitReaderPCServiceIntent() {
    try {
      window.dispatchEvent(new CustomEvent("bw-computer-voice-service-mode", {
        detail: {
          serviceMode: bridgeServiceMode,
          voiceEnabled: bridgeVoiceEnabledKnown
            ? bridgeVoiceEnabled
            : null,
          pendingServiceMode: bridgePendingServiceMode,
          pendingVoiceEnabled: bridgePendingVoiceEnabled,
        },
      }));
    } catch (_) {}
  }

  function clearReaderPCPendingTimer(axis) {
    var timer = axis === "voice"
      ? bridgePendingVoiceEnabledTimer
      : bridgePendingServiceModeTimer;
    if (timer) clearTimeout(timer);
    if (axis === "voice") bridgePendingVoiceEnabledTimer = null;
    else bridgePendingServiceModeTimer = null;
  }

  function failReaderPCPending(axis, code, message) {
    var requested = axis === "voice"
      ? bridgePendingVoiceEnabled
      : bridgePendingServiceMode;
    if (requested === null) return;
    clearReaderPCPendingTimer(axis);
    if (axis === "voice") {
      bridgePendingVoiceEnabled = null;
    } else {
      bridgePendingServiceMode = null;
    }
    emitReaderPCServiceIntent();
    emitStatus({
      state: "warning",
      message: message,
      code: code,
    });
    try {
      window.dispatchEvent(new CustomEvent(
        "bw-computer-voice-service-mode-failed",
        { detail: {
          axis: axis,
          requested: requested,
          serviceMode: bridgeServiceMode,
          voiceEnabled: bridgeVoiceEnabledKnown
            ? bridgeVoiceEnabled
            : null,
          code: code,
          message: message,
        } }
      ));
    } catch (_) {}
  }

  function beginReaderPCPending(axis, requested) {
    clearReaderPCPendingTimer(axis);
    if (axis === "voice") {
      bridgePendingVoiceEnabled = requested;
    } else {
      bridgePendingServiceMode = requested;
    }
    var timer = setTimeout(function () {
      failReaderPCPending(
        axis,
        axis === "voice"
          ? "BW_READERPC_VOICE_APPLY_TIMEOUT"
          : "BW_READERPC_SERVICE_MODE_APPLY_TIMEOUT",
        axis === "voice"
          ? "ReaderPC 未在期限内确认语音设置；已恢复显示实际状态。"
          : "ReaderPC 未在期限内确认连接模式；已恢复显示实际状态。"
      );
    }, READERPC_INTENT_APPLY_TIMEOUT_MS);
    if (timer && typeof timer.unref === "function") timer.unref();
    if (axis === "voice") bridgePendingVoiceEnabledTimer = timer;
    else bridgePendingServiceModeTimer = timer;
    emitReaderPCServiceIntent();
  }

  function reconcileReaderPCPending(axis, applied) {
    var requested = axis === "voice"
      ? bridgePendingVoiceEnabled
      : bridgePendingServiceMode;
    if (requested === null) return;
    if (requested === applied) {
      clearReaderPCPendingTimer(axis);
      if (axis === "voice") {
        bridgePendingVoiceEnabled = null;
      } else {
        bridgePendingServiceMode = null;
      }
    }
  }

  function normalizeContextMode(value) {
    // serviceMode/voiceEnabled are opt-in upgrade fields.  Missing fields
    // identify an older service and retain the released voice-on default.
    exactObject(
      value,
      ["mode"],
      ["serviceMode", "voiceEnabled"],
      "CONTEXT-MODE 响应"
    );
    if (
      value.mode !== CONTEXT_DELIVERY_LEGACY &&
      value.mode !== CONTEXT_DELIVERY_SNAPSHOT
    ) {
      throw directError(
        "Windows 上下文交付模式无效",
        "BW_READER_CONTEXT_DELIVERY_MODE_INVALID",
        false
      );
    }
    if (value.serviceMode !== undefined) {
      if (value.serviceMode !== "full" && value.serviceMode !== "bridge-only") {
        throw directError(
          "Windows 服务模式无效",
          "BW_READER_CONTEXT_DELIVERY_MODE_INVALID",
          false
        );
      }
      bridgeServiceMode = value.serviceMode;
    } else {
      bridgeServiceMode = "full";
    }
    reconcileReaderPCPending("service", bridgeServiceMode);
    if (value.voiceEnabled !== undefined) {
      if (typeof value.voiceEnabled !== "boolean") {
        throw directError(
          "Windows 语音功能状态无效",
          "BW_READERPC_VOICE_ENABLED_INVALID",
          false
        );
      }
      bridgeVoiceEnabled = value.voiceEnabled;
      bridgeVoiceEnabledKnown = true;
      reconcileReaderPCPending("voice", bridgeVoiceEnabled);
    } else {
      // Once capability was negotiated, a transient fallback response from an
      // older/stopping generation must not erase the user's pending axis.
      if (!bridgeVoiceEnabledKnown) bridgeVoiceEnabled = true;
    }
    emitReaderPCServiceIntent();
    contextDeliveryMode = value.mode;
    applyReaderPCSnapshotAuthority(value.mode);
    return value.mode;
  }

  function nativeReaderOwnsSnapshotLifecycle() {
    return window.__BW_NATIVE_COMPUTER_VOICE__ === true;
  }

  function applyReaderPCSnapshotAuthority(mode) {
    if (!nativeReaderOwnsSnapshotLifecycle()) return false;
    try {
      if (RC.ctxSync && typeof RC.ctxSync._applyServerMode === "function") {
        return RC.ctxSync._applyServerMode(mode);
      }
    } catch (_) {}
    return false;
  }

  function queryContextMode(channel) {
    // Exact-key older services reject unknown fields.  Negotiate newest →
    // serviceMode-only → legacy so either side can roll back independently.
    return channel.request("context-mode", {
      wantServiceMode: true,
      wantVoiceEnabled: true,
    })
      .then(normalizeContextMode)
      .catch(function () {
        return channel.request("context-mode", { wantServiceMode: true })
          .then(normalizeContextMode)
          .catch(function () {
            return channel.request("context-mode", {})
              .then(normalizeContextMode);
          });
      });
  }

  function contextSyncEnabled() {
    try {
      return !!(
        RC.ctxSync &&
        typeof RC.ctxSync.enabled === "function" &&
        RC.ctxSync.enabled()
      );
    } catch (_) {
      return false;
    }
  }

  function setServerContextDeliveryMode(mode, enabled) {
    var requestedEnabled = enabled == null
      ? contextSyncEnabled()
      : enabled === true;
    var fetcher = window.__bwReaderFetch;
    if (typeof fetcher !== "function" && typeof window.fetch === "function") {
      fetcher = window.fetch.bind(window);
    }
    if (typeof fetcher !== "function") {
      return Promise.reject(directError(
        "Reader 缺少上下文模式同步能力",
        "BW_READER_CONTEXT_MODE_FETCH_UNAVAILABLE",
        true
      ));
    }
    return Promise.resolve(fetcher("/pdf/api/context-sync", {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        enabled: requestedEnabled,
        deliveryMode: mode,
      }),
    })).then(function (response) {
      if (!response || response.ok !== true ||
          typeof response.json !== "function") {
        throw directError(
          "Reader 上下文交付模式写入失败",
          "BW_READER_CONTEXT_MODE_FETCH",
          true
        );
      }
      return response.json();
    }).then(function (value) {
      if (
        !plainObject(value) ||
        value.ok !== true ||
        value.enabled !== requestedEnabled ||
        value.deliveryMode !== mode
      ) {
        throw directError(
          "Reader 上下文交付模式回执无效",
          "BW_READER_CONTEXT_MODE_ACK",
          false
        );
      }
      return value;
    });
  }

  function normalizeContextModeSet(value, expectedMode) {
    exactObject(
      value,
      ["mode", "previousMode"],
      [],
      "CONTEXT-MODE-SET 响应"
    );
    if (
      (value.mode !== CONTEXT_DELIVERY_LEGACY &&
        value.mode !== CONTEXT_DELIVERY_SNAPSHOT) ||
      (value.previousMode !== CONTEXT_DELIVERY_LEGACY &&
        value.previousMode !== CONTEXT_DELIVERY_SNAPSHOT) ||
      value.mode !== expectedMode
    ) {
      throw directError(
        "Windows 上下文交付模式切换回执无效",
        "BW_READER_CONTEXT_DELIVERY_MODE_ACK",
        false
      );
    }
    contextDeliveryMode = value.mode;
    return value;
  }

  function setContextDeliveryMode(mode) {
    if (
      mode !== CONTEXT_DELIVERY_LEGACY &&
      mode !== CONTEXT_DELIVERY_SNAPSHOT
    ) {
      return Promise.reject(directError(
        "Reader 上下文交付模式无效",
        "BW_READER_CONTEXT_DELIVERY_MODE_INVALID",
        false
      ));
    }
    if (contextModeChangePromise) {
      return Promise.reject(directError(
        "Reader 上下文交付模式正在切换",
        "BW_READER_CONTEXT_DELIVERY_MODE_BUSY",
        true
      ));
    }
    if (nativeComputerVoiceOwnsWss()) {
      return Promise.reject(directError(
        "请先结束 App 电脑语音，再切换上下文交付模式",
        "BW_READER_CONTEXT_DELIVERY_MODE_NATIVE_BUSY",
        true
      ));
    }

    contextModeChanging = true;
    var channel = null;
    var activeChannel = null;
    var previousMode = null;
    var modeSessionId = randomSession().id;
    var changedOnWindows = false;
    var work = Promise.resolve().then(function () {
      return cancelAvailabilityForStart();
    }).then(function () {
      activeChannel = active && active.channel;
      if (!(active || dialPending)) return null;
      return stop("context-delivery-mode-change").then(function () {
        return activeChannel
          ? activeChannel.close()
          : null;
      });
    }).then(function () {
      return stopSnapshotLink();
    }).then(function () {
      return openDirect(null, function (opened) {
        channel = opened;
      });
    }).then(function (opened) {
      channel = opened;
      return queryContextMode(channel);
    }).then(function (currentMode) {
      previousMode = currentMode;
      if (currentMode === mode) {
        return {
          mode: mode,
          previousMode: currentMode,
        };
      }
      return channel.request("context-mode-set", {
        mode: mode,
        sessionId: modeSessionId,
      }).then(function (value) {
        changedOnWindows = true;
        return normalizeContextModeSet(value, mode);
      });
    }).then(function () {
      return setServerContextDeliveryMode(
        mode,
        mode === CONTEXT_DELIVERY_SNAPSHOT
          ? true
          : contextSyncEnabled()
      ).catch(function (error) {
        if (!changedOnWindows || !previousMode || !channel) {
          throw error;
        }
        return channel.request("context-mode-set", {
          mode: previousMode,
          sessionId: modeSessionId,
        }).then(function (value) {
          normalizeContextModeSet(value, previousMode);
        }).catch(function () {
          contextDeliveryMode = null;
        }).then(function () {
          throw error;
        });
      });
    }).then(function () {
      contextDeliveryMode = mode;
      emitStatus({
        state: mode === CONTEXT_DELIVERY_SNAPSHOT
          ? "context-ready"
          : "reader-connected",
        message: mode === CONTEXT_DELIVERY_SNAPSHOT
          ? "已切换到实时快照 MCP"
          : "已切换到旧版文字注入",
      });
      return {
        ok: true,
        mode: mode,
      };
    });

    contextModeChangePromise = work.finally(function () {
      var closePromise = channel
        ? channel.close()
        : Promise.resolve();
      channel = null;
      return Promise.resolve(closePromise).catch(function () {
      }).then(function () {
        contextModeChanging = false;
        var reconcilePromise = (
          contextDeliveryMode === CONTEXT_DELIVERY_SNAPSHOT ||
          contextSyncEnabled()
        )
          ? reconcileSnapshotLink()
          : null;
        return Promise.resolve(reconcilePromise).finally(function () {
          contextModeChangePromise = null;
        });
      });
    });
    return contextModeChangePromise;
  }

  function openDirect(options, onCreate) {
    // Defaults to the voice endpoint, so every existing caller is unaffected.
    // Only the snapshot link inside the App passes anything else.
    var endpoint = (options && options.endpoint) || DIRECT_ENDPOINT;
    var channel = new DirectSocket(endpoint, options);
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

  function normalizeDictionaryLookupRequest(value) {
    exactObject(
      value,
      ["mode", "term"],
      ["context", "reading", "english"],
      "本机词义分析请求"
    );
    var mode = safeText(value.mode, "mode", 16, false);
    if (mode !== "meaning" && mode !== "deep") {
      throw directError(
        "本机词义分析模式无效",
        "BW_READER_DICTIONARY_REQUEST_INVALID",
        false
      );
    }
    return {
      mode: mode,
      term: safeText(value.term, "term", 256, false),
      context: safeText(value.context || "", "context", 1200, true),
      reading: safeText(value.reading || "", "reading", 256, true),
      english: safeText(value.english || "", "english", 1200, true),
    };
  }

  function normalizeDictionaryLookupResult(value, request) {
    exactObject(
      value,
      ["term", "mode", "language", "text", "source", "cached"],
      [],
      "本机词义分析响应"
    );
    var result = {
      term: safeText(value.term, "term", 256, false),
      mode: safeText(value.mode, "mode", 16, false),
      language: safeText(value.language, "language", 16, false),
      text: safeText(value.text, "text", 6000, false),
      source: safeText(value.source, "source", 64, false),
      cached: value.cached,
    };
    if (
      result.term !== request.term ||
      result.mode !== request.mode ||
      result.language !== "zh-CN" ||
      result.source !== "pc-codex-cli" ||
      typeof result.cached !== "boolean" ||
      !result.text.trim()
    ) {
      throw directError(
        "本机词义分析响应与请求不匹配",
        "BW_READER_DICTIONARY_CLI_RESPONSE_INVALID",
        false
      );
    }
    return result;
  }

  function acquireDictionaryChannel() {
    // Windows 按连接顺序处理请求。CLI 释义最长可等待 60 秒，若复用
    // 语音/快照连接会把 PCM、heartbeat 和实时上下文一起堵住。
    // 词典只使用一次性的独立连接，结束后由调用方关闭。
    return acquireFreshDictionaryChannel();
  }

  function acquireFreshDictionaryChannel() {
    return openDirect({ endpoint: CONTEXT_ENDPOINT }).then(function (channel) {
      return { channel: channel, owned: true };
    });
  }

  function acquireFreshAnkiChannel() {
    // The Anki mutation must not share the voice or snapshot receive loop, but
    // unlike the read-only dictionary action it is admitted only after the
    // dedicated socket has explicitly entered context-only phase.
    var session = randomSession();
    var channel = null;
    return openDirect({ endpoint: CONTEXT_ENDPOINT }).then(function (opened) {
      channel = opened;
      return channel.request("context-open", {
        sessionId: session.id,
      });
    }).then(function (value) {
      exactObject(value, ["sessionId", "state", "mode"], [], "本机 Anki CONTEXT-OPEN 响应");
      if (value.sessionId !== session.id ||
          value.state !== "context-only" ||
          value.mode !== CONTEXT_DELIVERY_SNAPSHOT) {
        throw directError(
          "本机 Anki 上下文连接响应无效",
          "BW_READER_LOCAL_ANKI_CONTEXT_INVALID",
          false
        );
      }
      return {
        channel: channel,
        owned: true,
        sessionId: session.id,
      };
    }).catch(function (error) {
      var closed = channel ? channel.close() : Promise.resolve();
      return Promise.resolve(closed).catch(function () {}).then(function () {
        throw error;
      });
    });
  }

  // 两节点复制（references/reader-two-node-replication.md 步骤 3）的推送通道。
  // 纯传输：信封由 runtime 构造（发送端副本见 contract-sites 的
  // replication-command-envelope），这里透传并逐条投递 —— 一帧一命令是
  // 256KiB 帧限下最稳的形态。连接形态照 Anki 通道：一次性独立 context-only
  // 连接，不与语音/快照链路抢队，用完即关。
  function pushReplicationCommands(envelopes) {
    if (!Array.isArray(envelopes) || !envelopes.length) {
      return Promise.resolve([]);
    }
    var session = randomSession();
    var channel = null;
    return openDirect({ endpoint: CONTEXT_ENDPOINT }).then(function (opened) {
      channel = opened;
      return channel.request("context-open", { sessionId: session.id });
    }).then(function (value) {
      exactObject(
        value, ["sessionId", "state", "mode"], [], "复制命令 CONTEXT-OPEN 响应"
      );
      if (value.sessionId !== session.id ||
          value.state !== "context-only" ||
          value.mode !== CONTEXT_DELIVERY_SNAPSHOT) {
        throw directError(
          "复制命令上下文连接响应无效",
          "BW_REPLICATION_PUSH_CONTEXT_INVALID",
          false
        );
      }
      var results = [];
      var chain = Promise.resolve();
      envelopes.forEach(function (envelope) {
        chain = chain.then(function () {
          return sendReplicationEnvelope(channel, session, envelope)
            .then(function (reply) {
              results.push(reply);
            });
        });
      });
      return chain.then(function () { return results; });
    }).finally(function () {
      if (channel) return channel.close();
    });
  }
  window.__BW_REPLICATION_PUSH__ = pushReplicationCommands;

  // 单帧装不下的信封（大便签/真实页 blocks/大域 resync）切 base64 片，
  // 一片一帧走 replication-command-chunk；服务端会话级聚合重组后走与
  // 单帧完全相同的验证+落盘。中间片回 partial，末片才可能 accepted。
  var REPLICATION_SINGLE_FRAME_BYTES = 195 * 1024;
  var REPLICATION_CHUNK_PART_CHARS = 160 * 1024;
  function base64OfUtf8(text) {
    var bytes = new TextEncoder().encode(text);
    var out = "";
    for (var i = 0; i < bytes.length; i += 0x8000) {
      out += String.fromCharCode.apply(
        null, bytes.subarray(i, Math.min(i + 0x8000, bytes.length))
      );
    }
    return btoa(out);
  }
  function validReplicationReply(reply) {
    exactObject(
      reply, ["contract", "mutationId", "outcome"],
      ["received"], "复制命令回执"
    );
    return {
      mutationId: safeText(reply.mutationId, "mutationId", 64, false),
      outcome: safeText(reply.outcome, "outcome", 16, false),
    };
  }
  function sendReplicationEnvelope(channel, session, envelope) {
    var serialized = JSON.stringify(envelope);
    if (messageBytes(serialized) <= REPLICATION_SINGLE_FRAME_BYTES) {
      return channel.request("replication-command", {
        sessionId: session.id,
        envelope: envelope,
      }).then(validReplicationReply);
    }
    var encoded = base64OfUtf8(serialized);
    var total = Math.ceil(encoded.length / REPLICATION_CHUNK_PART_CHARS);
    var mutationId = String(envelope.op.mutationId);
    var chain = Promise.resolve(null);
    for (var seq = 0; seq < total; seq += 1) {
      (function (index) {
        chain = chain.then(function () {
          return channel.request("replication-command-chunk", {
            sessionId: session.id,
            chunk: {
              mutationId: mutationId,
              seq: index,
              total: total,
              part: encoded.slice(
                index * REPLICATION_CHUNK_PART_CHARS,
                (index + 1) * REPLICATION_CHUNK_PART_CHARS
              ),
            },
          }).then(function (reply) {
            var checked = validReplicationReply(reply);
            if (index < total - 1 && checked.outcome !== "partial") {
              throw directError(
                "复制分片中间片回执异常",
                "BW_REPLICATION_PUSH_CHUNK_INVALID",
                false
              );
            }
            return checked;
          });
        });
      })(seq);
    }
    return chain;
  }

  // 对账查询（规格 §6）：取 Windows 端每域摘要视图。连接形态同上。
  function queryReplicationDigests(replicationBookId) {
    var id = safeText(replicationBookId, "replicationBookId", 64, false);
    var session = randomSession();
    var channel = null;
    return openDirect({ endpoint: CONTEXT_ENDPOINT }).then(function (opened) {
      channel = opened;
      return channel.request("context-open", { sessionId: session.id });
    }).then(function (value) {
      exactObject(
        value, ["sessionId", "state", "mode"], [], "复制摘要 CONTEXT-OPEN 响应"
      );
      if (value.sessionId !== session.id ||
          value.state !== "context-only" ||
          value.mode !== CONTEXT_DELIVERY_SNAPSHOT) {
        throw directError(
          "复制摘要上下文连接响应无效",
          "BW_REPLICATION_PUSH_CONTEXT_INVALID",
          false
        );
      }
      return channel.request("replication-digest-query", {
        sessionId: session.id,
        replicationBookId: id,
      });
    }).then(function (reply) {
      exactObject(
        reply,
        ["contract", "replicationBookId", "generatedAtUtcMs", "domains"],
        [],
        "复制摘要视图"
      );
      if (reply.replicationBookId !== id ||
          !plainObject(reply.domains)) {
        throw directError(
          "复制摘要视图与请求不匹配",
          "BW_REPLICATION_DIGESTS_VIEW_INVALID",
          false
        );
      }
      return reply;
    }).finally(function () {
      if (channel) return channel.close();
    });
  }
  window.__BW_REPLICATION_DIGESTS__ = queryReplicationDigests;

  // 通知 tab(2026-08-26):查询 Windows 的 open 通知。一次性 context-only
  // 连接,照复制摘要同款;失败折成空列表(通知是增强,桥不在就没有)。
  function queryNotifications() {
    var session = randomSession();
    var channel = null;
    return openDirect({ endpoint: CONTEXT_ENDPOINT }).then(function (opened) {
      channel = opened;
      return channel.request("context-open", { sessionId: session.id });
    }).then(function (value) {
      exactObject(
        value, ["sessionId", "state", "mode"], [], "通知查询 CONTEXT-OPEN 响应"
      );
      if (value.sessionId !== session.id ||
          value.state !== "context-only" ||
          value.mode !== CONTEXT_DELIVERY_SNAPSHOT) {
        throw directError(
          "通知查询上下文连接响应无效",
          "BW_NOTIFICATIONS_QUERY_INVALID",
          false
        );
      }
      return channel.request("replication-notifications-query", {
        sessionId: session.id,
      });
    }).then(function (reply) {
      // review（到期/新卡数）搭通知视图的车下发（2026-08-27，小组件数据）。
      // ⚠ 放行了字段还要显式搬 —— 这里是重建不是透传（CLAUDE.md 那课）。
      exactObject(
        reply, ["contract", "items"],
        // exportedAtUtcMs=数据时刻(区分"桥在但对账循环死了")；
        // dropped=被上限丢掉的条数(静默截断会让"建了却没有"无从查起)。
        ["review", "exportedAtUtcMs", "dropped"], "通知视图"
      );
      if (reply.contract !== "reader-notifications/1" ||
          !Array.isArray(reply.items)) {
        throw directError(
          "通知视图形状无效", "BW_NOTIFICATIONS_QUERY_INVALID", false
        );
      }
      var review = null;
      if (reply.review && typeof reply.review === "object" &&
          Number.isSafeInteger(reply.review.due) &&
          Number.isSafeInteger(reply.review["new"])) {
        review = {
          due: reply.review.due,
          "new": reply.review["new"],
          atMs: Number.isSafeInteger(reply.review.atMs)
            ? reply.review.atMs : 0,
        };
      }
      var items = reply.items;
      try {
        Object.defineProperty(items, "__bwReview",
          { value: review, configurable: true });
      } catch (e) {}
      return items;
    }).finally(function () {
      if (channel) return channel.close();
    });
  }
  window.__BW_NOTIFICATIONS_QUERY__ = queryNotifications;

  // ── 网页表面的实时输出取件执行(方案 A,2026-08-26)──
  // App 内输出走 WSS 推送(_handleReaderRealtimeOutput);网页没有常驻
  // socket,由扩展 background 长轮询 Windows 取件后经 runtime 消息送到
  // 这里。同一套 normalize + 同一个 receiver(RC.voicecall
  // .acceptRealtimeOutput),回执由调用方送回 Windows —— AI 端语义与
  // App 场景逐字一致。
  function executePickedRealtimeOutput(rawPayload) {
    return Promise.resolve().then(function () {
      var delivery = normalizeReaderRealtimeOutput(rawPayload);
      var receiver = RC.voicecall && RC.voicecall.acceptRealtimeOutput;
      if (typeof receiver !== "function") {
        throw directError(
          "Reader 实时输出接收器尚未挂载",
          "BW_READER_REALTIME_OUTPUT_RECEIVER_UNAVAILABLE",
          true
        );
      }
      return Promise.resolve(receiver(delivery)).then(function (receipt) {
        exactObject(
          receipt, ["outcome"], ["error", "bindOutcome", "bindReason"],
          "Reader 输出回执"
        );
        return {
          correlation: delivery.correlation,
          sourceInstanceId: delivery.sourceInstanceId,
          outcome: receipt.outcome,
          error: receipt.error,
          bindOutcome: receipt.bindOutcome,
          bindReason: receipt.bindReason,
        };
      });
    }).catch(function (error) {
      var correlation = readerRealtimeOutputCorrelation(rawPayload);
      if (!correlation) throw error;
      return {
        correlation: correlation.correlation,
        sourceInstanceId: correlation.sourceInstanceId,
        outcome: "rejected",
        error: String(
          (error && error.code) || (error && error.message) || error
        ).slice(0, 500),
      };
    });
  }
  window.__BW_REALTIME_OUTPUT_PICKUP__ = executePickedRealtimeOutput;

  // 扩展环境(ISOLATED world 有 chrome.runtime):background 取件后经
  // 运行时消息送达。App 内没有 chrome.runtime,这段自然不挂。
  (function wirePickupMessage() {
    try {
      if (typeof chrome === "undefined" || !chrome.runtime ||
          !chrome.runtime.onMessage) return;
      chrome.runtime.onMessage.addListener(
        function (message, _sender, sendResponse) {
          if (!message || message.type !== "bw-realtime-output-pickup") {
            return undefined;
          }
          executePickedRealtimeOutput(message.payload).then(function (receipt) {
            sendResponse({ ok: true, receipt: receipt });
          }).catch(function (error) {
            sendResponse({
              ok: false,
              error: String((error && error.message) || error).slice(0, 300),
            });
          });
          return true;   // 异步 sendResponse
        }
      );
    } catch (_) {}
  })();

  // ── 侧边栏通知 tab(App/扩展;照 rc-assistant 的 asst tab 自插模式,
  //    shared-drawer 接管后 #ep-side-tabs / 接管前 #side-tabs 都能挂)──
  (function mountNotificationsTab() {
    // 无 DOM 环境(测试沙盒/worker)不挂 UI —— 查询函数本体不受影响。
    if (typeof document === 'undefined' ||
        typeof setInterval !== 'function') return;
    var mounted = false;
    var lastItems = [];
    var hiddenIds = {};   // 乐观移除:操作已入队,列表先行消失
    function bar() {
      return document.getElementById('ep-side-tabs')
        || document.getElementById('side-tabs');
    }
    function panel() {
      return document.getElementById('ep-side')
        || document.getElementById('grammar-panel');
    }
    function esc(value) {
      return String(value == null ? '' : value).replace(/[&<>"]/g, function (c) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
      });
    }
    function render(box, items) {
      var visible = items.filter(function (item) {
        return !hiddenIds[item.id];
      });
      if (!visible.length) {
        box.innerHTML =
          '<div style="color:#5a6680;font-size:12.5px;padding:8px 2px">' +
          '没有待办通知。</div>';
        return;
      }
      box.innerHTML = visible.map(function (item) {
        var stamp = '';
        try {
          stamp = new Date(item.createdAtUtcMs).toLocaleString('zh-CN', {
            month: '2-digit', day: '2-digit',
            hour: '2-digit', minute: '2-digit'
          });
        } catch (e) {}
        return '<div class="bw-ntf-card" data-id="' + esc(item.id) + '"' +
          ' style="background:rgba(20,30,56,.7);border:1px solid #263655;' +
          'border-radius:10px;padding:9px 11px;margin-bottom:8px">' +
          '<div style="display:flex;gap:6px;align-items:baseline">' +
          (item.state === 'pending'
            ? '<span style="color:#ffd28a;font-size:11px;flex:none">[新]</span>'
            : '') +
          '<div style="font-size:13px;color:#dbe4f8;font-weight:600;' +
          'min-width:0">' + esc(item.title) + '</div></div>' +
          (item.body
            ? '<div style="font-size:12px;color:#9aa7c4;margin-top:3px">' +
              esc(item.body) + '</div>'
            : '') +
          '<div style="display:flex;gap:8px;align-items:center;margin-top:7px">' +
          '<span style="color:#5a6680;font-size:11px;flex:1">' + esc(stamp) +
          '　' + esc(item.kind) + '</span>' +
          '<button type="button" data-ntf-act="resolve" style="background:#1a2540;' +
          'border:1px solid #2a3550;color:#9fe6b8;border-radius:6px;' +
          'padding:4px 10px;cursor:pointer;font-size:12px">✔ 完成</button>' +
          '<button type="button" data-ntf-act="cancel" style="background:#1a2540;' +
          'border:1px solid #2a3550;color:#8fa5c8;border-radius:6px;' +
          'padding:4px 10px;cursor:pointer;font-size:12px">✕ 不再需要</button>' +
          '</div></div>';
      }).join('');
    }
    function refresh() {
      var box = document.getElementById('bw-ntf-list');
      if (!box) return;
      box.innerHTML =
        '<div style="color:#5a6680;font-size:12.5px;padding:8px 2px">加载…</div>';
      queryNotifications().then(function (items) {
        lastItems = items;
        render(box, items);
      }).catch(function () {
        box.innerHTML =
          '<div style="color:#5a6680;font-size:12.5px;padding:8px 2px">' +
          '通知暂不可读（电脑桥离线时没有通知）。</div>';
      });
    }
    function act(id, action) {
      hiddenIds[id] = true;
      var box = document.getElementById('bw-ntf-list');
      if (box) render(box, lastItems);
      fetch('/pdf/api/notification-action', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: action, id: id })
      }).then(function () {
        try {
          if (typeof window.__BW_SYSTEM_PROJECTION_TRIGGER__ === 'function') {
            window.__BW_SYSTEM_PROJECTION_TRIGGER__();
          }
        } catch (e) {}
      }).catch(function () {
        delete hiddenIds[id];
        if (box) render(box, lastItems);
      });
    }
    function tryMount() {
      if (mounted) return true;
      var tabs = bar();
      var host = panel();
      if (!tabs || !host) return false;
      mounted = true;
      var shared = tabs.id === 'ep-side-tabs';
      var btn = document.createElement('button');
      btn.className = shared ? 'ep-side-tab' : 'side-tab';
      btn.dataset.pane = 'ntf';
      btn.innerHTML = '<svg class="si" viewBox="0 0 24 24" fill="none" ' +
        'stroke="currentColor" stroke-width="1.7" stroke-linecap="round" ' +
        'stroke-linejoin="round"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9' +
        'h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/></svg>' +
        '<span class="ep-side-tab-lb">通知</span>';
      btn.title = '通知';
      btn.onclick = function () {
        try { window.switchSideTab && window.switchSideTab('ntf'); } catch (e) {}
        refresh();
      };
      tabs.appendChild(btn);
      var pane = document.createElement('div');
      pane.className = shared ? 'ep-side-pane' : 'side-pane';
      pane.dataset.pane = 'ntf';
      pane.innerHTML =
        '<div style="padding:10px 12px">' +
        '<div id="bw-ntf-list"></div>' +
        '<div style="color:#5a6680;font-size:11px;margin-top:6px">' +
        '完成/不再需要会同步回电脑并入库留档；确定性的待办(如复习)' +
        '完成后会自动消失。</div></div>';
      host.appendChild(pane);
      return true;
    }
    // shared-drawer 接管发生在启动后:轮询挂载(≤30s),挂上即停。
    var tries = 0;
    var timer = setInterval(function () {
      tries += 1;
      if (tryMount() || tries > 60) clearInterval(timer);
    }, 500);
    document.addEventListener('click', function (event) {
      var button = event.target && event.target.closest
        ? event.target.closest('[data-ntf-act]') : null;
      if (!button) return;
      var card = button.closest('.bw-ntf-card');
      if (!card) return;
      act(card.dataset.id, button.dataset.ntfAct);
    });
  })();

  // ── iOS 系统投影同步（App 环境专属，2026-08-27 用户拍板）──
  // 定期把用户向通知 + review 摘要交给原生侧做三件事：苹果提醒事项
  // 显示副本（我们的通知系统是真值）、新 pending 的本地横幅、小组件
  // 共享数据。runtime 只在 App 里暴露 __bwSystemProjectionSync ——
  // 桌面/扩展没有该函数，这段自然不跑。返回的 resolvedIds 是用户在
  // 苹果提醒里勾完成的条目：逐条走既有 resolve 回流，与侧栏「完成」
  // 按钮语义完全等价（苹果侧勾选 = 用户 resolve）。
  (function wireSystemProjection() {
    if (typeof document === 'undefined' ||
        typeof setInterval !== 'function') return;   // 测试沙箱无 DOM/计时器
    var SYNC_EVERY_MS = 30 * 60 * 1000;
    // 首次成功前退避重试(2026-08-26 真机实锤:小组件全空 —— 原来首次
    // 只在 8 秒时试一次,那一刻 runtime 入口未挂/桥不可达就要干等
    // 30 分钟。首次成功前 8s→30s→60s→120s 封顶持续重试,成功后
    // 转 30 分钟巡航)。
    var RETRY_LADDER_MS = [8000, 30000, 60000, 120000];
    var syncing = false;
    var succeededOnce = false;
    var retryIndex = 0;
    var alarmNoticeShown = false;
    var syncedItems = [];
    function scheduleNext(delayMs) {
      setTimeout(sync, delayMs);
    }
    function retryAfterFailure() {
      if (succeededOnce) { scheduleNext(SYNC_EVERY_MS); return; }
      retryIndex = Math.min(retryIndex + 1, RETRY_LADDER_MS.length - 1);
      scheduleNext(RETRY_LADDER_MS[retryIndex]);
    }
    function sync() {
      var native = window.__bwSystemProjectionSync;
      if (typeof native !== 'function') { retryAfterFailure(); return; }
      if (syncing) return;
      syncing = true;
      queryNotifications().then(function (items) {
        syncedItems = items || [];
        return native({
          notifications: items,
          review: items.__bwReview || null,
          syncAtMs: Date.now(),
        });
      }).then(function (result) {
        if (result === null) {
          // runtime 在但没有原生 handler = 非 App 环境（扩展/桌面）——
          // 系统投影只属于 App，彻底安静，不再空转。
          syncing = false;
          return;
        }
        // 投影状态（revision 里带 reminders=/alarms=）留给诊断，并在
        // **闹钟不可用却确有到点提醒**时提醒用户一次 —— 这种能力缺席
        // 必须出声：用户会以为到点会被叫醒，实际只有普通通知。
        try {
          window.__BW_SYSTEM_PROJECTION_STATE__ = {
            revision: String(result.revision || ''),
            atMs: Date.now(),
          };
          // 三段状态各自判断,别用一句硬编码的话概括 —— 通知权限被拒
          // 时说"闹钟需要 iPadOS 26"是**答非所问**,用户会去查系统版本,
          // 而真正的原因是他自己拒过通知权限。
          var rev = String(result.revision || '');
          var hasDue = syncedItems.some(function (one) {
            return one && one.dueAtUtcMs;
          });
          // ⚠ **权限类的原因跟有没有到点时刻无关。**
          //
          // 原来整段都被 hasDue 挡着。而现实里的待办（比如倒垃圾）
          // dueAtUtcMs 是 null —— 于是"提醒事项权限没给"这条永远不显示，
          // 用户看到的就是**什么都没发生、也没有任何解释**
          // （2026-08-29 实锤：两条待办没建出苹果提醒，查了一路才发现
          // 诊断其实早就算好了，只是被这个条件挡住了）。
          //
          // 只有"到点才生效"的那两条（本地到点通知、系统闹钟）
          // 才该跟着 hasDue 走。
          if (!alarmNoticeShown && syncedItems.length) {
            var reasons = [];
            // ⚠ denied 和 calendar-unavailable **必须分开说**。
            //
            // 我第一版把两者合成一句「权限没给」，而用户当场截图证明
            // 权限是开的 —— 那句话会把人送去反复检查一个本来就对的开关。
            // 一条**指向错误方向**的诊断比没有诊断更贵：没有诊断至少
            // 不会浪费时间。
            if (/reminders=denied/.test(rev)) {
              reasons.push('提醒事项权限没给（去 设置 → BWReader → '
                + '提醒事项 打开）');
            } else if (/reminders=calendar-unavailable/.test(rev)) {
              reasons.push('权限是好的，但建不出提醒列表 '
                + '(calendar-unavailable) —— 多半是 iCloud 提醒事项没启用，'
                + '或本机没有能放提醒的账户');
            } else if (/reminders=commit-failed/.test(rev)) {
              reasons.push('提醒写入失败 (commit-failed)，这一轮一条都没落地');
            } else if (/reminders=partial:/.test(rev)) {
              reasons.push('有 ' + (rev.match(/reminders=partial:(\d+)/)
                || [, '?'])[1] + ' 条提醒没写进去');
            } else if (/reminders=projected/.test(rev)) {
              // ⚠ **成功也要说一次。**
              //
              // 「同步成功但你在别处找」和「根本没同步」，在用户眼里
              // 一模一样：都是"什么都没发生"。所以成功时报一次列表名，
              // 一个 App 生命周期只报一次，不烦人。
              reasons.push('待办已同步到提醒事项的「BW 待办」列表');
            } else if (/reminders=/.test(rev)) {
              // 兜底：状态是别的值就**原样报出来**，不要归类成已知的几种 ——
              // 归错类正是上一版的错误来源。
              reasons.push('提醒事项同步状态：'
                + (rev.match(/reminders=([^;]*)/) || [, '?'])[1]);
            }
            if (hasDue && /notifications=denied/.test(rev)) {
              reasons.push('通知权限被拒（到点通知不会响，去设置里打开）');
            }
            if (hasDue) {
              if (/alarms=(unsupported-os|sdk-unavailable)/.test(rev)) {
                reasons.push('系统闹钟需要 iPadOS 26');
              } else if (/alarms=denied/.test(rev)) {
                reasons.push('闹钟权限被拒');
              }
            }
            if (reasons.length) {
              alarmNoticeShown = true;
              if (RC && typeof RC.toast === 'function') {
                RC.toast('待办同步：' + reasons.join('；'));
              }
            }
          }
        } catch (e) {}
        var resolved = result && Array.isArray(result.resolvedIds)
          ? result.resolvedIds : [];
        resolved.forEach(function (id) {
          fetch('/pdf/api/notification-action', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ action: 'resolve', id: id })
          }).catch(function () {});
        });
        succeededOnce = true;
        scheduleNext(SYNC_EVERY_MS);
      }).catch(function () {
        // 桥离线/查询失败 —— 首次成功前按梯子重试,之后 30 分钟巡航。
        retryAfterFailure();
      }).finally(function () { syncing = false; });
    }
    scheduleNext(RETRY_LADDER_MS[0]);
    // 用户在通知 tab 完成/取消后,过一轮 Windows 对账周期再同步一次:
    // 否则已排的到点通知/闹钟要等 30 分钟巡航才撤销 —— 用户已经处理掉
    // 的事情却在到点时又响一次,比不响更糟。
    window.__BW_SYSTEM_PROJECTION_TRIGGER__ = function () {
      setTimeout(sync, 75000);
    };
  })();

  function lookupJapaneseFallback(value) {
    var request;
    try {
      request = normalizeDictionaryLookupRequest(value);
    } catch (error) {
      return Promise.reject(error);
    }
    var lease = null;
    return acquireDictionaryChannel().then(function (acquired) {
      lease = acquired;
      return acquired.channel.request(
        "dictionary-lookup",
        request,
        DICTIONARY_LOOKUP_TIMEOUT_MS
      );
    }).then(function (result) {
      return normalizeDictionaryLookupResult(result, request);
    }).finally(function () {
      if (lease && lease.owned) return lease.channel.close();
    });
  }

  function normalizeLocalAnkiCard(value) {
    if (!plainObject(value)) {
      throw directError(
        "本机 Anki 卡片无效",
        "BW_READER_LOCAL_ANKI_SCHEMA",
        false
      );
    }
    var type = safeText(value.type, "Anki card type", 16, false);
    function projectionField(markdown, name) {
      var html = ankiProjectionHtml(markdown);
      if (html.length > 64000) {
        throw directError(
          name + " 的 Anki HTML 投影超过 64000 字符",
          "BW_READER_LOCAL_ANKI_SCHEMA",
          false
        );
      }
      return html;
    }
    var canonical;
    var projection;
    if (type === "basic") {
      exactObject(value, ["type", "front", "back"], [], "本机 Anki 基础卡");
      canonical = {
        type: type,
        front: safeText(value.front, "Anki front", 64000, false),
        back: safeText(value.back, "Anki back", 64000, true),
      };
      projection = {
        type: type,
        front: projectionField(canonical.front, "Anki front"),
        back: projectionField(canonical.back, "Anki back"),
      };
      if (/\0/.test(canonical.front + canonical.back)) {
        throw directError("本机 Anki 卡片包含 NUL", "BW_READER_LOCAL_ANKI_SCHEMA", false);
      }
      return { canonical: canonical, projection: projection };
    }
    if (type === "cloze") {
      exactObject(value, ["type", "cloze"], [], "本机 Anki 填空卡");
      canonical = {
        type: type,
        cloze: safeText(value.cloze, "Anki cloze", 64000, false),
      };
      projection = {
        type: type,
        cloze: projectionField(canonical.cloze, "Anki cloze"),
      };
      if (/\0/.test(canonical.cloze)) {
        throw directError("本机 Anki 卡片包含 NUL", "BW_READER_LOCAL_ANKI_SCHEMA", false);
      }
      return { canonical: canonical, projection: projection };
    }
    throw directError(
      "本机 Anki 卡片类型无效",
      "BW_READER_LOCAL_ANKI_SCHEMA",
      false
    );
  }

  // Reader entities keep semantic Markdown. Anki fields are HTML, so both
  // initial projection and later edits must use the same renderer as the card
  // shown in Reader. Remote images remain HTTPS here; the trusted Windows/Pi
  // projection writer stores them through AnkiConnect and replaces src with a
  // deterministic local media filename before addNote/updateNoteFields.
  function ankiProjectionImageSource(value) {
    var source = String(value == null ? "" : value).trim();
    if (!source || /[\u0000-\u001f\u007f]/.test(source)) {
      throw directError(
        "Anki 卡片图片地址无效",
        "BW_READER_ANKI_MEDIA_URL_INVALID",
        false
      );
    }
    var hasScheme = /^[A-Za-z][A-Za-z0-9+.-]*:/.test(source);
    if (!hasScheme) {
      // A bare filename may already name media in collection.media. Paths,
      // protocol-relative URLs and traversal are never accepted.
      if (source.indexOf("/") >= 0 || source.indexOf("\\") >= 0 ||
          source === "." || source === ".." || /[<>:"|?*#]/.test(source)) {
        throw directError(
          "Anki 卡片图片只能引用已有媒体文件或绝对 HTTPS 地址",
          "BW_READER_ANKI_MEDIA_URL_INVALID",
          false
        );
      }
      return source;
    }
    var parsed;
    try {
      parsed = new URL(source);
    } catch (_) {
      parsed = null;
    }
    var host = parsed ? String(parsed.hostname || "").toLowerCase() : "";
    var privateName = host === "localhost" ||
      /\.(?:localhost|local|lan|internal|home|home\.arpa)$/.test(host);
    var ipLiteral = /^\[.*\]$/.test(host) ||
      /^\d{1,3}(?:\.\d{1,3}){3}$/.test(host);
    if (!parsed || parsed.protocol !== "https:" || !host ||
        parsed.username || parsed.password || parsed.hash ||
        (parsed.port && parsed.port !== "443") || privateName || ipLiteral ||
        host.indexOf(".") < 0) {
      throw directError(
        "Anki 卡片远程图片只允许公开、无凭据的绝对 HTTPS 地址",
        "BW_READER_ANKI_MEDIA_URL_INVALID",
        false
      );
    }
    return source;
  }

  function ankiProjectionHtml(value) {
    var source = String(value == null ? "" : value);
    // Validate before RC.safeHtml can silently remove an unsafe src. These
    // forms cover the canonical Markdown emitted by Reader and raw HTML kept
    // for backwards compatibility; rendered HTML is checked once more below.
    source.replace(
      /!\[[^\]]*\]\(\s*(?:<([^>]+)>|([^\s)]+))/g,
      function (_, angle, plain) {
        ankiProjectionImageSource(angle || plain || "");
        return _;
      }
    );
    source.replace(
      /<img\b[^>]*\bsrc\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))/gi,
      function (_, quoted, single, bare) {
        ankiProjectionImageSource(quoted || single || bare || "");
        return _;
      }
    );
    var html;
    try {
      html = window.RC && typeof RC.md === "function"
        ? RC.md(source)
        : (RC.esc ? RC.esc(source) : source
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;")).replace(/\r?\n/g, "<br>");
    } catch (error) {
      throw directError(
        "Anki 卡片 Markdown 渲染失败",
        "BW_READER_ANKI_MARKDOWN_INVALID",
        false,
        error
      );
    }
    var root = document.createElement("div");
    root.innerHTML = String(html || "");
    Array.prototype.forEach.call(root.querySelectorAll("img"), function (img) {
      ankiProjectionImageSource(img.getAttribute("src") || "");
    });
    return root.innerHTML;
  }

  function normalizeLocalAnkiAddRequest(value) {
    exactObject(
      value,
      ["draftId", "sourceInstanceId", "cardIndex", "aid", "card"],
      [],
      "本机 Anki 入库请求"
    );
    var draftId = safeText(value.draftId, "Anki draftId", 64, false);
    var aid = safeText(value.aid, "Anki aid", 64, false);
    if (!/^draft-[a-f0-9]{32}$/.test(draftId) ||
        !/^fc_[a-f0-9]{32}$/.test(aid) ||
        !Number.isSafeInteger(value.cardIndex) || value.cardIndex < 0 ||
        value.cardIndex > 19) {
      throw directError(
        "本机 Anki 入库身份无效",
        "BW_READER_LOCAL_ANKI_SCHEMA",
        false
      );
    }
    var normalizedCard = normalizeLocalAnkiCard(value.card);
    var normalized = {
      draftId: draftId,
      sourceInstanceId: safeId(
        value.sourceInstanceId,
        "Anki sourceInstanceId"
      ),
      cardIndex: value.cardIndex,
      aid: aid,
      card: normalizedCard.canonical,
      projection: normalizedCard.projection,
    };
    var bytes = new TextEncoder().encode(JSON.stringify(normalized)).byteLength;
    if (bytes > 192 * 1024) {
      throw directError(
        "本机 Anki 请求超过 192 KiB 安全上限",
        "BW_READER_ANKI_REQUEST_TOO_LARGE",
        false
      );
    }
    return normalized;
  }

  function normalizeLocalAnkiAddResult(value) {
    exactObject(
      value,
      ["ok", "added", "note_ids", "card_ids", "card_ids_by_note"],
      ["dedup"],
      "本机 Anki 入库响应"
    );
    if (value.ok !== true || !Number.isSafeInteger(value.added) ||
        value.added < 1 || !Array.isArray(value.note_ids) ||
        value.note_ids.length !== value.added ||
        !Array.isArray(value.card_ids) || !plainObject(value.card_ids_by_note) ||
        (Object.prototype.hasOwnProperty.call(value, "dedup") &&
          typeof value.dedup !== "boolean")) {
      throw directError(
        "本机 Anki 入库响应无效",
        "BW_READER_LOCAL_ANKI_RESPONSE_INVALID",
        false
      );
    }
    value.note_ids.forEach(function (id) {
      if (!Number.isSafeInteger(id) || id <= 0) {
        throw directError("本机 Anki note id 无效", "BW_READER_LOCAL_ANKI_RESPONSE_INVALID", false);
      }
    });
    value.card_ids.forEach(function (id) {
      if (!Number.isSafeInteger(id) || id <= 0) {
        throw directError("本机 Anki card id 无效", "BW_READER_LOCAL_ANKI_RESPONSE_INVALID", false);
      }
    });
    Object.keys(value.card_ids_by_note).forEach(function (noteId) {
      var ids = value.card_ids_by_note[noteId];
      if (!/^\d+$/.test(noteId) || !Array.isArray(ids) || ids.some(function (id) {
        return !Number.isSafeInteger(id) || id <= 0;
      })) {
        throw directError("本机 Anki 卡号映射无效", "BW_READER_LOCAL_ANKI_RESPONSE_INVALID", false);
      }
    });
    return value;
  }

  function addLocalAnkiCard(value) {
    var request;
    try {
      request = normalizeLocalAnkiAddRequest(value);
    } catch (error) {
      return Promise.reject(error);
    }
    var lease = null;
    return acquireFreshAnkiChannel().catch(function (error) {
      throw directError(
        "本机 Anki 通道不可用:" + String(error && error.message || error || "?").slice(0, 240),
        "BW_READER_LOCAL_ANKI_CHANNEL_UNAVAILABLE",
        true
      );
    }).then(function (acquired) {
      lease = acquired;
      return acquired.channel.request(
        "anki-add-cards-local",
        Object.assign({ sessionId: acquired.sessionId }, request),
        LOCAL_ANKI_ADD_TIMEOUT_MS
      );
    }).then(normalizeLocalAnkiAddResult).finally(function () {
      if (lease && lease.owned) return lease.channel.close();
    });
  }

  // Canonical Reader cards stay authoritative in the Reader repository.  This
  // channel performs only the explicitly requested projection operation in the
  // user's desktop Anki, then reports local collection and AnkiWeb sync as two
  // separate outcomes.  Keeping this beside addLocalAnkiCard also guarantees
  // that both paths acquire the same fresh, single-owner Windows bridge lease.
  function normalizeLocalAnkiOperationRequest(value) {
    if (!value || typeof value !== "object" || Array.isArray(value)) {
      throw directError(
        "本机 Anki 操作必须是对象",
        "BW_READER_LOCAL_ANKI_OPERATION_SCHEMA",
        false
      );
    }
    var operation = safeText(
      value.operation,
      "Anki operation",
      32,
      false
    );
    var allowed = {
      "read-notes": ["operation", "noteIds"],
      "read-cards": ["operation", "cardIds"],
      "update-note-fields": [
        "operation", "mutationId", "noteId", "fields", "syncMode"
      ],
      "delete-notes": [
        "operation", "mutationId", "noteIds", "syncMode"
      ],
      "answer-cards": [
        "operation", "mutationId", "answers", "syncMode"
      ],
      sync: ["operation", "mutationId"]
    }[operation];
    if (!allowed || Object.keys(value).length !== allowed.length ||
        Object.keys(value).some(function (key) {
          return allowed.indexOf(key) < 0;
        })) {
      throw directError(
        "本机 Anki 操作字段无效",
        "BW_READER_LOCAL_ANKI_OPERATION_SCHEMA",
        false
      );
    }
    function ids(name) {
      var source = value[name];
      if (!Array.isArray(source) || !source.length || source.length > 20 ||
          source.some(function (id) {
            return !Number.isSafeInteger(id) || id <= 0;
          })) {
        throw directError(
          "本机 Anki 编号无效",
          "BW_READER_LOCAL_ANKI_OPERATION_SCHEMA",
          false
        );
      }
      return source.slice();
    }
    var result = { operation: operation };
    if (operation === "read-notes" || operation === "delete-notes") {
      result.noteIds = ids("noteIds");
    } else if (operation === "read-cards") {
      result.cardIds = ids("cardIds");
    }
    if (operation === "update-note-fields") {
      if (!Number.isSafeInteger(value.noteId) || value.noteId <= 0 ||
          !plainObject(value.fields) || !Object.keys(value.fields).length ||
          Object.keys(value.fields).length > 32) {
        throw directError(
          "本机 Anki 字段更新无效",
          "BW_READER_LOCAL_ANKI_OPERATION_SCHEMA",
          false
        );
      }
      var fields = {};
      Object.keys(value.fields).forEach(function (name) {
        var fieldName = safeText(name, "Anki field name", 128, false);
        var fieldValue = safeText(
          value.fields[name],
          "Anki field value",
          100000,
          true
        );
        fields[fieldName] = fieldValue;
      });
      result.noteId = value.noteId;
      result.fields = fields;
    }
    if (operation === "answer-cards") {
      if (!Array.isArray(value.answers) || !value.answers.length ||
          value.answers.length > 20) {
        throw directError(
          "本机 Anki 评分无效",
          "BW_READER_LOCAL_ANKI_OPERATION_SCHEMA",
          false
        );
      }
      result.answers = value.answers.map(function (answer) {
        if (!plainObject(answer) ||
            Object.keys(answer).sort().join(",") !== "cardId,ease" ||
            !Number.isSafeInteger(answer.cardId) || answer.cardId <= 0 ||
            !Number.isSafeInteger(answer.ease) || answer.ease < 1 ||
            answer.ease > 4) {
          throw directError(
            "本机 Anki 评分无效",
            "BW_READER_LOCAL_ANKI_OPERATION_SCHEMA",
            false
          );
        }
        return { cardId: answer.cardId, ease: answer.ease };
      });
    }
    if (operation !== "read-notes" && operation !== "read-cards") {
      var mutationId = safeText(
        value.mutationId,
        "Anki mutationId",
        160,
        false
      );
      if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{7,159}$/.test(mutationId)) {
        throw directError(
          "本机 Anki mutationId 无效",
          "BW_READER_LOCAL_ANKI_OPERATION_SCHEMA",
          false
        );
      }
      result.mutationId = mutationId;
    }
    if (operation === "update-note-fields" || operation === "delete-notes" ||
        operation === "answer-cards") {
      if (value.syncMode !== "background" && value.syncMode !== "wait") {
        throw directError(
          "本机 Anki syncMode 无效",
          "BW_READER_LOCAL_ANKI_OPERATION_SCHEMA",
          false
        );
      }
      result.syncMode = value.syncMode;
    }
    return result;
  }

  function normalizeLocalAnkiOperationResult(value, operation) {
    if (!value || typeof value !== "object" || Array.isArray(value) ||
        (Object.prototype.hasOwnProperty.call(value, "ok") && value.ok !== true) ||
        value.operation !== operation ||
        typeof value.anki_local_applied !== "boolean" ||
        !plainObject(value.anki_web_sync)) {
      throw directError(
        "本机 Anki 操作响应无效",
        "BW_READER_LOCAL_ANKI_OPERATION_RESPONSE_INVALID",
        false
      );
    }
    var syncStatus = String(value.anki_web_sync.status || "");
    if (["not-requested", "requested", "succeeded", "failed", "unknown"]
        .indexOf(syncStatus) < 0) {
      throw directError(
        "本机 Anki 同步响应无效",
        "BW_READER_LOCAL_ANKI_OPERATION_RESPONSE_INVALID",
        false
      );
    }
    return value;
  }

  function operateLocalAnkiCard(value) {
    var request;
    try {
      request = normalizeLocalAnkiOperationRequest(value);
    } catch (error) {
      return Promise.reject(error);
    }
    var lease = null;
    return acquireFreshAnkiChannel().catch(function (error) {
      throw directError(
        "本机 Anki 通道不可用:" +
          String(error && error.message || error || "?").slice(0, 240),
        "BW_READER_LOCAL_ANKI_CHANNEL_UNAVAILABLE",
        true
      );
    }).then(function (acquired) {
      lease = acquired;
      return acquired.channel.request(
        "anki-card-operation-local",
        Object.assign({ sessionId: acquired.sessionId }, request),
        LOCAL_ANKI_ADD_TIMEOUT_MS
      );
    }).then(function (result) {
      return normalizeLocalAnkiOperationResult(result, request.operation);
    }).catch(function (error) {
      // The Windows host has already fenced this mutationId when it reports
      // an unknown outcome.  Preserve that semantic bit so callers append an
      // unknown receipt and never downgrade it to a retryable ordinary failure.
      if (error && error.code ===
          "BW_READER_ANKI_OPERATION_OUTCOME_UNKNOWN") {
        error.outcomeUnknown = true;
      }
      throw error;
    }).finally(function () {
      if (lease && lease.owned) return lease.channel.close();
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
    if (attempt.channel && !attempt.borrowedSnapshot) {
      attempt.channel.close();
    }
    return Promise.resolve(attempt.promise).then(
      function () {},
      function () {}
    );
  }

  // 复用一条常驻链路前必须**主动验活**。
  //
  // DirectSocket.closed 只在 _fail() 里置 true,而 _fail 的三条触发路径(open 超时、
  // socket.onerror、socket.onclose)全部依赖浏览器事件回调。iOS PWA 页面进入后台或被
  // 系统冻结时这些回调可能长时间不触发甚至不触发 —— Windows 服务重启后,前端的 closed
  // 会长期滞留 false,于是"复用"到一条实际已死的 socket:STATUS/START 写进虚空,
  // Windows 侧零连接、零 START、**零错误码**(前端全程认为链路正常)。
  //
  // 底层 readyState 由浏览器维护,比我们的被动标志可靠一档,所以在 closed 之外再硬校验
  // 一次。注意 _fail() 会把 this.socket 置空,因此 socket 缺失同样视为不可用。
  // ⚠ 这仍不是充分条件:iOS 上 readyState 也可能滞后于真实 TCP 状态,所以调用方必须
  // 配合"首次只读请求有界超时 + 丢弃重建"的第二层兜底(见 availability() 与
  // queryStartChannel())。
  function directChannelLive(channel) {
    if (!channel || channel.closed) return false;
    var socket = channel.socket;
    if (!socket) return false;
    return socket.readyState === 1;   // WebSocket.OPEN
  }

  function borrowSnapshotChannelForStatus(attempt) {
    var state = snapshotLink;
    // The App keeps its snapshot on /reader-context/v1.  That endpoint is
    // deliberately context-only and must never receive STATUS or the
    // status request. Only borrow a snapshot channel when it is already the
    // full computer-voice endpoint; otherwise availability() opens a bounded,
    // short-lived voice channel for the settings read.
    if (
      !state ||
      state.stopped ||
      state.endpoint !== DIRECT_ENDPOINT
    ) {
      return Promise.resolve(null);
    }
    return Promise.resolve(state.promise).catch(function () {
      return null;
    }).then(function (resolved) {
      if (
        resolved !== state ||
        snapshotLink !== state ||
        state.stopped ||
        !directChannelLive(state.channel)
      ) {
        return null;
      }
      attempt.borrowedSnapshot = true;
      attempt.channel = state.channel;
      return state.channel;
    });
  }

  function availability() {
    if (active) {
      var localActive = activeAvailability(active);
      if (!active.channel || !directChannelLive(active.channel)) {
        return Promise.resolve(localActive);
      }
      return active.channel.request("status", {}).then(normalizeStatusPayload)
        .then(function (remoteStatus) {
          if (Object.prototype.hasOwnProperty.call(remoteStatus, "codexVoice")) {
            localActive.status.codexVoice = remoteStatus.codexVoice;
          }
          localActive.status.lastError = remoteStatus.lastError;
          return localActive;
        }).catch(function () {
          // The live call remains the source of truth for bridge activity. A
          // failed read must not tear it down or pretend the call stopped, but
          // it must remain visible in settings instead of looking unsupported.
          localActive.status.codexVoice = {
            status: "error",
            active: null,
            source: null,
            shortcutSent: false,
          };
          return localActive;
        });
    }
    if (contextModeChanging) {
      return Promise.resolve({
        state: "busy",
        reason: "Reader 正在切换上下文模式",
        code: "BW_READER_CONTEXT_DELIVERY_MODE_BUSY",
        endpoint: DIRECT_ENDPOINT,
        status: null,
      });
    }
    if (!ownsReaderUi()) {
      return Promise.resolve({
        state: "unavailable",
        reason: "当前 Reader 界面由另一运行时接管",
        code: "BW_COMPUTER_VOICE_UI_NOT_OWNER",
        endpoint: DIRECT_ENDPOINT,
        status: null,
      });
    }
    if (availabilityAttempt) return availabilityAttempt.promise;

    var attempt = {
      cancelled: false,
      channel: null,
      borrowedSnapshot: false,
      retriedAfterStaleBorrow: false,
      restoreSnapshotAfterRetry: false,
      promise: null,
    };
    availabilityAttempt = attempt;
    attempt.promise = borrowSnapshotChannelForStatus(attempt)
      .then(function (channel) {
        if (channel) return channel;
        return openDirect(null, function (created) {
          attempt.channel = created;
        });
      })
      .then(function (channel) {
        if (attempt.cancelled) {
          if (!attempt.borrowedSnapshot) channel.close();
          throw directError(
            "状态刷新已让位给电脑按钮启动",
            "BW_COMPUTER_VOICE_DIRECT_CANCELLED",
            false
          );
        }
        return channel.request("status", {}).then(normalizeStatusPayload)
          .catch(function (error) {
            // 第二层兜底:readyState 说 OPEN 也可能是滞后的(iOS 尤甚)。借来的链路
            // 首次 STATUS 失败 → 判定它已死,丢弃、重建、**只重试一次**。
            // 只重试一次是刻意的:无限重试会把一次点击放大成连接风暴;而新建链路
            // 自己失败就是真失败,不该再兜。
            if (attempt.borrowedSnapshot !== true) throw error;
            if (attempt.retriedAfterStaleBorrow) throw error;
            if (attempt.cancelled) throw error;
            attempt.retriedAfterStaleBorrow = true;
            attempt.restoreSnapshotAfterRetry = true;
            return stopSnapshotLink().then(function () {
              attempt.borrowedSnapshot = false;
              attempt.channel = null;
              return openDirect(null, function (created) {
                attempt.channel = created;
              });
            }).then(function (fresh) {
              if (attempt.cancelled) {
                fresh.close();
                throw directError(
                  "状态刷新已让位给电脑按钮启动",
                  "BW_COMPUTER_VOICE_DIRECT_CANCELLED",
                  false
                );
              }
              return fresh.request("status", {}).then(normalizeStatusPayload);
            });
          });
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
      var closePromise = attempt.channel && !attempt.borrowedSnapshot
        ? attempt.channel.close()
        : Promise.resolve();
      attempt.channel = null;
      if (availabilityAttempt === attempt) availabilityAttempt = null;
      return closePromise.then(function () {
        if (attempt.restoreSnapshotAfterRetry) {
          scheduleSnapshotReconnect(0);
        }
      });
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
        "浏览器没有获得麦克风权限；请允许后再次点击电脑按钮",
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
        "网页麦克风已被系统停止；请重新点击电脑按钮",
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
            "网页麦克风持续被系统暂停；请回到页面后重新点击电脑按钮",
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
          // 下一次电脑按钮点击会只重试同一个 AudioContext，不会挂断。
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
        "必须由一次真实电脑按钮点击启动 Windows 桥接器",
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
          message: "浏览器阻止了声音；请再次点击电脑按钮允许播放",
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
        message: "浏览器仍阻止播放，请再次点击电脑按钮允许声音",
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
      BW_COMPUTER_VOICE_DIRECT_VOICE_READY_TIMEOUT:
        "等待 Codex 语音子系统就绪超时",
    };
    return reason ? (messages[reason] || reason) : "";
  }

  function statusMessage(state, reason) {
    var messages = {
      "starting-service": "正在启动 Windows 桥接服务…",
      "starting-app": "正在启动 Windows 电脑客户端…",
      "waiting-app-ready": "正在等待 Windows 电脑客户端就绪…",
      "waiting-voice-ready": "正在等待 Codex 语音子系统就绪…",
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

  function rejectNativeContextRequests(message, code) {
    Object.keys(nativeContextPending).forEach(function (requestId) {
      var pending = nativeContextPending[requestId];
      delete nativeContextPending[requestId];
      if (pending.timer) clearTimeout(pending.timer);
      pending.reject(directError(
        message || "原生 Reader 上下文连接已停止",
        code || "BW_NATIVE_COMPUTER_CONTEXT_STOPPED",
        true
      ));
    });
  }

  function applyNativeContextResult(value) {
    if (!plainObject(value) || typeof value.requestId !== "string") return;
    var pending = nativeContextPending[value.requestId];
    if (!pending) return;
    delete nativeContextPending[value.requestId];
    if (pending.timer) clearTimeout(pending.timer);
    if (value.ok === true && Object.prototype.hasOwnProperty.call(value, "value")) {
      pending.resolve(value.value);
      return;
    }
    var detail = plainObject(value.error) ? value.error : {};
    pending.reject(directError(
      typeof detail.message === "string" && detail.message
        ? detail.message
        : "原生 Reader 上下文请求失败",
      typeof detail.code === "string" && detail.code
        ? detail.code
        : "BW_NATIVE_COMPUTER_CONTEXT_FAILED",
      detail.retryable === true
    ));
  }

  window.__bwNativeComputerContextApplyResult = applyNativeContextResult;

  function nativeContextRequest(action, fields, timeoutMs) {
    if (
      (action !== "context" && action !== "active-reading") ||
      !plainObject(fields) ||
      !nativeContextHandlerAvailable()
    ) {
      return Promise.reject(directError(
        "BWReader App 原生上下文通道不可用",
        "BW_NATIVE_COMPUTER_CONTEXT_UNAVAILABLE",
        true
      ));
    }
    var requestId = "native-context-" + Date.now().toString(36) + "-" +
      (++nativeContextRequestSequence).toString(36);
    return new Promise(function (resolve, reject) {
      var timer = setTimeout(function () {
        var pending = nativeContextPending[requestId];
        if (!pending) return;
        delete nativeContextPending[requestId];
        pending.reject(directError(
          "BWReader App 原生上下文请求超时",
          "BW_NATIVE_COMPUTER_CONTEXT_TIMEOUT",
          true
        ));
      }, timeoutMs || NATIVE_CONTEXT_REQUEST_TIMEOUT_MS);
      nativeContextPending[requestId] = {
        resolve: resolve,
        reject: reject,
        timer: timer,
      };
      try {
        window.webkit.messageHandlers.bwNativeComputerContext.postMessage({
          requestId: requestId,
          action: action,
          fields: fields,
        });
      } catch (error) {
        delete nativeContextPending[requestId];
        clearTimeout(timer);
        reject(directError(
          error && error.message || "无法发送原生 Reader 上下文请求",
          "BW_NATIVE_COMPUTER_CONTEXT_SEND_FAILED",
          true
        ));
      }
    });
  }

  var nativeContextChannel = Object.freeze({
    request: nativeContextRequest,
  });

  function contextPumpAlive(state, pump) {
    var ownsLink = false;
    if (state && state.nativeContext === true) {
      ownsLink = nativeContextState === state;
    } else if (state && state.contextOnly === true) {
      ownsLink = snapshotLink === state;
    } else {
      ownsLink = active === state && state.started;
    }
    return !!(
      state &&
      pump &&
      !pump.stopped &&
      state.contextPump === pump &&
      !state.stopped &&
      ownsLink &&
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
    stopActiveReadingPump(state);
  }

  function warnAndStopContextPump(state, pump, error) {
    if (!contextPumpAlive(state, pump)) return;
    stopContextPump(state);
    if (state.contextOnly === true) {
      if (snapshotLink === state) snapshotLink = null;
      state.stopped = true;
      snapshotTransportState = "failed";
      var contextOnlyChannel = state.channel;
      state.channel = null;
      closeChannelThenScheduleSnapshotReconnectAfterFailure(
        contextOnlyChannel
      );
    }
    emitStatus({
      state: "warning",
      sessionId: state.sessionId,
      message: error && error.message || (
        state.contextOnly === true
          ? "Windows 本地 Reader 快照连接已停止"
          : "Reader 上下文桥接已停止；音频通话继续"
      ),
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

  function localActiveReadingSnapshot() {
    var source;
    var canonical;
    try {
      var state = RC.ctxSync && typeof RC.ctxSync._state === "function"
        ? RC.ctxSync._state()
        : null;
      source = state && state.pend;
      canonical = state && state.canonical;
    } catch (_) {
      source = null;
      canonical = null;
    }
    if (
      !plainObject(source) ||
      (
        source.kind !== "pdf" &&
        source.kind !== "epub" &&
        source.kind !== "html" &&
        source.kind !== "web"
      )
    ) {
      return null;
    }
    var sourceFile = source.kind === "web" ? source.url : source.file;
    var isView = source.kind !== "web" &&
      typeof sourceFile === "string" &&
      sourceFile.indexOf("vbook:") === 0;
    if (isView && (
      !plainObject(canonical) ||
      canonical.kind !== source.kind ||
      canonical.viewFile !== sourceFile ||
      !sameActiveScalar(canonical.viewPage, source.pos) ||
      typeof canonical.file !== "string" ||
      canonical.file.indexOf("vbook:") === 0
    )) {
      return null;
    }
    var file = isView ? canonical.file : sourceFile;
    if (
      typeof file !== "string" ||
      !file ||
      file.length > 4096 ||
      /[\u0000-\u001f\u007f-\u009f]/.test(file)
    ) {
      return null;
    }
    var page = isView ? canonical.page : source.pos;
    if (page === undefined || page === null) {
      page = null;
    } else if (
      (
        typeof page === "number" &&
        (!Number.isSafeInteger(page) || page < 0)
      ) ||
      (
        typeof page === "string" &&
        (
          !page ||
          page.length > 256 ||
          /[\u0000-\u001f\u007f-\u009f]/.test(page)
        )
      ) ||
      (typeof page !== "number" && typeof page !== "string")
    ) {
      return null;
    }
    var title = source.title;
    if (title === undefined || title === null) {
      title = null;
    } else if (
      typeof title !== "string" ||
      title.length > 1024 ||
      /[\u0000-\u001f\u007f-\u009f]/.test(title)
    ) {
      return null;
    }
    var selectionState = "unknown";
    var selection = null;
    if (Object.prototype.hasOwnProperty.call(source, "selection")) {
      var selectedText = typeof source.selection === "string"
        ? source.selection.trim().slice(0, 400)
        : "";
      var anchoredToCurrentPage = true;
      if (source.sel_page !== undefined && source.sel_page !== null) {
        var selectionPage = source.pos;
        if (selectionPage === undefined || selectionPage === null) {
          selectionPage = page;
        }
        anchoredToCurrentPage = sameActiveScalar(
          source.sel_page,
          selectionPage
        );
      }
      if (selectedText && anchoredToCurrentPage) {
        selectionState = "active";
        selection = selectedText;
      } else {
        selectionState = "cleared";
      }
    }
    // 选中附近的原文(宿主在 pend 里给的 sel_context / sel_context_source)。
    // 只在选中 active 时带 —— 桥的合同是"非 active 不许有上下文",违反会让
    // 整条 active-reading 被拒,且非重试错误会把上下文泵整个停死。
    // 非字符串/超限/含控制字符按**缺席**处理而不是拒绝:上下文是增强,
    // 它坏了不该连位置和选中本体一起陪葬。
    var selectionContext = null;
    var selectionContextSource = null;
    if (selectionState === "active") {
      var rawSelCtx = source.sel_context;
      if (typeof rawSelCtx === "string") {
        // 桥的合同只放行换行和制表两种控制字符;回车先归一化成换行,
        // 其余控制字符按「上下文缺席」处理 —— 上下文是增强,一个坏字符
        // 不该让位置和选中本体一起陪葬。
        rawSelCtx = rawSelCtx.replace(/\r\n?/g, "\n").trim().slice(0, 1200);
        if (
          rawSelCtx &&
          !/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/.test(rawSelCtx)
        ) {
          selectionContext = rawSelCtx;
          var rawSelCtxSrc = source.sel_context_source;
          if (
            typeof rawSelCtxSrc === "string" &&
            rawSelCtxSrc.trim() &&
            rawSelCtxSrc.trim().length <= 40 &&
            !/[\u0000-\u001f\u007f-\u009f]/.test(rawSelCtxSrc.trim())
          ) {
            selectionContextSource = rawSelCtxSrc.trim();
          }
        }
      }
    }
    var activeReading = {
      kind: source.kind,
      file: file,
      title: title,
      page: page,
      selectionState: selectionState,
      selection: selection,
      sourceInstanceId: currentReaderSourceInstanceId(),
    };
    if (selectionContext) {
      activeReading.selectionContext = selectionContext;
      if (selectionContextSource) {
        activeReading.selectionContextSource = selectionContextSource;
      }
    }
    try {
      if (typeof RC.selectionRegionsForPage === "function") {
        activeReading.selectionRegions = RC.selectionRegionsForPage({
          page: page,
        });
      }
    } catch (_) {}
    var highlightSource = currentLocalHighlightSource(activeReading);
    if (highlightSource) {
      activeReading.highlightSource = highlightSource;
    }
    if (isView) {
      activeReading.viewFile = sourceFile;
      activeReading.viewPage = source.pos;
    }
    var review = localReviewSnapshot();
    if (review) {
      activeReading.review = review;
    }
    return activeReading;
  }

  // 复习模式投影:字段缺席 = 未进入复习模式(旧构建天然一致)。
  // 这里按白名单**重建**而不是透传 —— 桥对 active-reading 是整条拒绝的
  // 合同,一个越界字段会让位置和选中一起陪葬。卡片正文按 selectionContext
  // 同一条纪律清洗控制字符;编号不合形状的条目丢弃而不是拒绝整个投影。
  var REVIEW_CARD_ID_RE = /^[A-Za-z0-9_-]{1,120}$/;
  function reviewCardText(value) {
    if (typeof value !== "string") return "";
    return value
      .replace(/\r\n?/g, "\n")
      .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f]/g, "")
      .slice(0, 2000);
  }
  function localReviewSnapshot() {
    var raw;
    try {
      raw = RC.review && typeof RC.review.snapshotState === "function"
        ? RC.review.snapshotState()
        : null;
    } catch (_) {
      return null;
    }
    if (!plainObject(raw)) return null;
    var dueTotal = Number(raw.dueTotal);
    var index = Number(raw.index);
    if (
      !Number.isSafeInteger(dueTotal) || dueTotal < 0 ||
      !Number.isSafeInteger(index) || index < 0
    ) {
      return null;
    }
    var queueIds = [];
    if (Array.isArray(raw.queueIds)) {
      for (var i = 0; i < raw.queueIds.length && queueIds.length < 200; i++) {
        var id = raw.queueIds[i];
        if (typeof id === "string" && REVIEW_CARD_ID_RE.test(id)) {
          queueIds.push(id);
        }
      }
    }
    var review = {
      dueTotal: Math.min(dueTotal, 100000),
      index: Math.min(index, 100000),
      queueIds: queueIds,
      showingAnswer: raw.showingAnswer === true,
    };
    var current = raw.current;
    if (
      plainObject(current) &&
      typeof current.id === "string" &&
      REVIEW_CARD_ID_RE.test(current.id)
    ) {
      review.current = {
        id: current.id,
        front: reviewCardText(current.front),
        back: reviewCardText(current.back),
      };
    }
    return review;
  }

  function localHighlightTarget(current) {
    var location = Number(current && current.page);
    if (!Number.isSafeInteger(location)) return null;
    if (current.kind === "pdf" && location >= 1) {
      return { kind: "pdf", page: location };
    }
    if (current.kind === "epub" && location >= 0) {
      return { kind: "epub", section: location };
    }
    return null;
  }

  function sameLocalHighlightTarget(left, right) {
    if (!left || !right || left.kind !== right.kind) return false;
    return left.kind === "pdf"
      ? left.page === right.page
      : left.section === right.section;
  }

  function localHighlightTargetKey(current, target) {
    if (!current || !target) return "";
    return String(current.file) + "\n" + target.kind + "\n" + String(
      target.kind === "pdf" ? target.page : target.section
    );
  }

  function normalizeLocalHighlightSource(value, current) {
    exactObject(
      value,
      [
        "contract", "snapshotId", "documentId", "target",
        "sourceDigest", "revision", "expiresAt", "markers",
      ],
      [],
      "Reader 高亮来源"
    );
    if (value.contract !== "reader-highlight-source/1") {
      throw directError(
        "Reader 高亮来源合同无效",
        "BW_READER_HIGHLIGHT_SOURCE_SCHEMA",
        false
      );
    }
    var snapshotId = safeId(value.snapshotId, "Reader 高亮 snapshotId");
    if (!/^hrs_[0-9a-f]{24}$/.test(snapshotId)) {
      throw directError(
        "Reader 高亮 snapshotId 无效",
        "BW_READER_HIGHLIGHT_SOURCE_SCHEMA",
        false
      );
    }
    var documentId = safeText(
      value.documentId,
      "Reader 高亮 documentId",
      4096,
      false
    );
    if (
      documentId !== current.file ||
      /[\u0000-\u001f\u007f-\u009f]/.test(documentId)
    ) {
      throw directError(
        "Reader 高亮文档身份不匹配",
        "BW_READER_HIGHLIGHT_SOURCE_SCHEMA",
        false
      );
    }
    var target = normalizeReaderOutputTarget(value.target);
    var expectedTarget = localHighlightTarget(current);
    if (!sameLocalHighlightTarget(target, expectedTarget)) {
      throw directError(
        "Reader 高亮目标不匹配",
        "BW_READER_HIGHLIGHT_SOURCE_SCHEMA",
        false
      );
    }
    var sourceDigest = safeText(
      value.sourceDigest,
      "Reader 高亮 sourceDigest",
      30,
      false
    );
    if (!/^rsd1_[0-9a-f]{8}_[0-9a-f]{16}$/.test(sourceDigest)) {
      throw directError(
        "Reader 高亮来源摘要无效",
        "BW_READER_HIGHLIGHT_SOURCE_SCHEMA",
        false
      );
    }
    var revision = safeText(
      value.revision,
      "Reader 高亮 revision",
      160,
      false
    );
    if (/\s/.test(revision) ||
        /[\u0000-\u001f\u007f-\u009f]/.test(revision)) {
      throw directError(
        "Reader 高亮 revision 无效",
        "BW_READER_HIGHLIGHT_SOURCE_SCHEMA",
        false
      );
    }
    var now = Date.now();
    if (
      !Number.isSafeInteger(value.expiresAt) ||
      value.expiresAt <= now ||
      value.expiresAt > now + 300000
    ) {
      throw directError(
        "Reader 高亮来源已过期或时钟无效",
        "BW_READER_HIGHLIGHT_SOURCE_SCHEMA",
        false
      );
    }
    if (
      !Array.isArray(value.markers) ||
      value.markers.length < 2 ||
      value.markers.length > 2048
    ) {
      throw directError(
        "Reader 高亮 marker 数量无效",
        "BW_READER_HIGHLIGHT_SOURCE_SCHEMA",
        false
      );
    }
    var seen = Object.create(null);
    var totalText = 0;
    var markers = value.markers.map(function (item, index) {
      exactObject(
        item,
        ["marker", "text"],
        [],
        "Reader 高亮 marker[" + index + "]"
      );
      var marker = safeId(
        item.marker,
        "Reader 高亮 marker[" + index + "].marker"
      );
      var text = safeText(
        item.text,
        "Reader 高亮 marker[" + index + "].text",
        512,
        true
      );
      var finalMarker = index === value.markers.length - 1;
      if (
        !/^m_[0-9a-z]{1,4}$/.test(marker) ||
        seen[marker] ||
        (finalMarker ? text.length !== 0 : text.length === 0) ||
        /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/.test(text)
      ) {
        throw directError(
          "Reader 高亮 marker 无效",
          "BW_READER_HIGHLIGHT_SOURCE_SCHEMA",
          false
        );
      }
      seen[marker] = true;
      totalText += text.length;
      return { marker: marker, text: text };
    });
    if (totalText < 1 || totalText > 16384) {
      throw directError(
        "Reader 高亮正文长度无效",
        "BW_READER_HIGHLIGHT_SOURCE_SCHEMA",
        false
      );
    }
    return {
      contract: "reader-highlight-source/1",
      snapshotId: snapshotId,
      documentId: documentId,
      target: target,
      sourceDigest: sourceDigest,
      revision: revision,
      expiresAt: value.expiresAt,
      markers: markers,
    };
  }

  function currentLocalHighlightSource(current) {
    var cached = localHighlightSourceCache;
    var target = localHighlightTarget(current);
    if (
      !cached || !target || cached.documentId !== current.file ||
      !sameLocalHighlightTarget(cached.target, target) ||
      cached.expiresAt <= Date.now()
    ) {
      return null;
    }
    return cached;
  }

  function maybeRefreshLocalHighlightSource(state, pump, current) {
    var target = localHighlightTarget(current);
    var provider = window.__bwReaderHighlightSource;
    var now = Date.now();
    if (typeof provider !== "function") {
      localHighlightSourceCache = null;
      pump.highlightSourceTargetKey = "";
      pump.nextHighlightSourceCheckAt = 0;
      return;
    }
    if (!target) return;
    var targetKey = localHighlightTargetKey(current, target);
    var targetChanged = pump.highlightSourceTargetKey !== targetKey;
    if (targetChanged) {
      pump.highlightSourceTargetKey = targetKey;
      pump.nextHighlightSourceCheckAt = 0;
      pump.lastHighlightSourceError = "";
    }
    var cached = currentLocalHighlightSource(current);
    var expiresSoon = !!(
      cached && cached.expiresAt - now <=
        LOCAL_HIGHLIGHT_SOURCE_TTL_MARGIN_MS
    );
    if (
      pump.highlightSourceInFlight ||
      (
        !targetChanged && !expiresSoon &&
        now < pump.nextHighlightSourceCheckAt
      )
    ) {
      return;
    }
    pump.nextHighlightSourceCheckAt =
      now + LOCAL_HIGHLIGHT_SOURCE_REFRESH_MS;
    pump.highlightSourceInFlight = true;
    boundedLocalPageTask(
      Promise.resolve().then(function () {
        return provider({ file: current.file, target: target });
      }),
      null
    ).then(function (value) {
      if (!activeReadingPumpAlive(state, pump)) return;
      var latest = localActiveReadingSnapshot();
      if (
        !latest || latest.file !== current.file ||
        !sameLocalHighlightTarget(localHighlightTarget(latest), target)
      ) {
        return;
      }
      if (!value) {
        localHighlightSourceCache = null;
        pump.lastSignature = null;
        pump.nextHighlightSourceCheckAt =
          Date.now() + LOCAL_HIGHLIGHT_SOURCE_RETRY_MS;
        return;
      }
      var normalized = normalizeLocalHighlightSource(
        value,
        current
      );
      var prior = localHighlightSourceCache;
      var sameSource = !!(
        prior && prior.documentId === normalized.documentId &&
        sameLocalHighlightTarget(prior.target, normalized.target) &&
        prior.sourceDigest === normalized.sourceDigest &&
        prior.revision === normalized.revision &&
        prior.expiresAt - Date.now() > 30000
      );
      if (!sameSource) {
        localHighlightSourceCache = normalized;
        // Force the next active-reading tick to publish a changed source. A
        // provider may mint a fresh snapshotId on every read, so an unchanged
        // digest+revision deliberately retains the earlier identity.
        pump.lastSignature = null;
      }
      pump.lastHighlightSourceError = "";
      pump.nextHighlightSourceCheckAt =
        Date.now() + LOCAL_HIGHLIGHT_SOURCE_REFRESH_MS;
    }).catch(function (error) {
      pump.nextHighlightSourceCheckAt =
        Date.now() + LOCAL_HIGHLIGHT_SOURCE_RETRY_MS;
      var message = String(
        error && error.message || error || "本机高亮来源读取失败"
      );
      if (message !== pump.lastHighlightSourceError) {
        pump.lastHighlightSourceError = message;
        try {
          if (window.dlog) window.dlog("本机高亮来源失败: " + message);
        } catch (_) {}
      }
    }).finally(function () {
      pump.highlightSourceInFlight = false;
    });
  }

  function sameActiveScalar(left, right) {
    if (left === null || left === undefined) {
      return right === null || right === undefined;
    }
    if (right === null || right === undefined) return false;
    return String(left) === String(right);
  }

  function cleanLocalPageText(value, maximum) {
    value = String(value == null ? "" : value)
      .replace(/\u0000/g, "")
      .replace(/\r\n?/g, "\n")
      .replace(/[ \t]+/g, " ")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
    return value.slice(0, maximum);
  }

  function localNativePageRuntime() {
    try {
      var runtime = window.BWReaderRuntime &&
        window.BWReaderRuntime.nativeLocalRuntime;
      return runtime && typeof runtime.publishPageContext === "function"
        ? runtime : null;
    } catch (_) { return null; }
  }

  function boundedLocalPageTask(task, fallback) {
    return new Promise(function (resolve) {
      var settled = false;
      var finish = function (value) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(value);
      };
      var timer = setTimeout(function () {
        finish(fallback);
      }, LOCAL_PAGE_TEXT_WAIT_MS);
      Promise.resolve(task).then(finish, function () { finish(fallback); });
    });
  }

  function localDOMPageText(page, visibleOnly) {
    try {
      if (!document || typeof document.querySelector !== "function") return "";
      var wrap = document.querySelector(
        '.page-wrap[data-page-num="' + String(page) + '"]'
      );
      var chars = wrap && wrap.__charBoxes;
      if (!Array.isArray(chars) || !chars.length) return "";
      if (!visibleOnly) {
        return cleanLocalPageText(chars.map(function (item) {
          return item && item.c != null ? String(item.c) : "";
        }).join(""), 12000);
      }
      var main = document.getElementById && document.getElementById("main");
      var layer = wrap.__charLayer;
      if (!main || !layer || typeof main.getBoundingClientRect !== "function" ||
          typeof layer.getBoundingClientRect !== "function") return "";
      var viewport = main.getBoundingClientRect();
      var bounds = layer.getBoundingClientRect();
      var layoutWidth = Number(layer.clientWidth || wrap.clientWidth) || 0;
      var layoutHeight = Number(layer.clientHeight || wrap.clientHeight) || 0;
      if (!bounds.width || !bounds.height || !layoutWidth || !layoutHeight) return "";
      var sx = bounds.width / layoutWidth;
      var sy = bounds.height / layoutHeight;
      var visible = chars.filter(function (item) {
        if (!item || item.c == null) return false;
        var left = bounds.left + Number(item.left || 0) * sx;
        var top = bounds.top + Number(item.top || 0) * sy;
        var right = left + Math.max(1, Number(item.width || 0) * sx);
        var bottom = top + Math.max(1, Number(item.height || 0) * sy);
        return right > viewport.left && left < viewport.right &&
          bottom > viewport.top && top < viewport.bottom;
      }).map(function (item) { return String(item.c); }).join("");
      return cleanLocalPageText(visible, 5000);
    } catch (_) { return ""; }
  }

  function localAdapterVisibleText() {
    try {
      var adapter = RC && typeof RC.adapter === "function" ? RC.adapter() : null;
      var context = adapter && typeof adapter.getContext === "function"
        ? adapter.getContext() : null;
      return cleanLocalPageText(
        context && (context.visible_text || context.visibleText) || "",
        5000
      );
    } catch (_) { return ""; }
  }

  function emptyLocalPageRecord(text) {
    text = cleanLocalPageText(text || "", LOCAL_PAGE_CONTEXT_LIMIT);
    return {
      text: text,
      chars: [],
      after: [],
      lastSource: -1,
      layout: null,
      layoutFallback: false,
      truncated: false
    };
  }

  // Keep the original provider-array index as the anchor coordinate while
  // building the same whitespace-normalized text that page.context exposes.
  // `after[i]` is the normalized insertion offset immediately after source
  // character i; this makes range projection independent from the rendered DOM.
  function normalizeLocalPageChars(chars, maximum) {
    chars = Array.isArray(chars) ? chars : [];
    maximum = Math.max(0, Number(maximum) || 0);
    var text = "";
    var normalizedChars = [];
    var after = [];
    var pending = "";
    var truncated = false;
    var lastSource = -1;

    function appendValue(value, sourceIndex) {
      var units = Array.from(String(value || ""));
      for (var unitIndex = 0; unitIndex < units.length; unitIndex += 1) {
        var unit = units[unitIndex];
        if (text.length + unit.length > maximum) {
          truncated = true;
          return false;
        }
        text += unit;
        lastSource = sourceIndex;
      }
      return true;
    }

    for (var index = 0; index < chars.length; index += 1) {
      var source = chars[index] && typeof chars[index] === "object"
        ? chars[index] : {};
      var item = Object.assign({}, source, { _oi: index });
      normalizedChars.push(item);
      var value = String(source.c == null ? "" : source.c)
        .replace(/\u0000/g, "")
        .replace(/\r\n?/g, "\n");
      var pieces = Array.from(value);
      for (var partIndex = 0; partIndex < pieces.length; partIndex += 1) {
        var part = pieces[partIndex];
        if (part === " " || part === "\t") {
          if (pending.indexOf("\n") < 0) pending = " ";
          continue;
        }
        if (part === "\n") {
          pending = pending.indexOf("\n") >= 0
            ? pending.slice(0, 2) + "\n"
            : "\n";
          if (pending.length > 2) pending = "\n\n";
          continue;
        }
        if (pending && text) {
          if (!appendValue(pending, index)) break;
        }
        pending = "";
        if (!appendValue(part, index)) break;
      }
      after[index] = text.length;
      if (truncated) {
        for (var rest = index + 1; rest < chars.length; rest += 1) {
          normalizedChars.push(Object.assign({}, chars[rest] || {}, { _oi: rest }));
          after[rest] = text.length;
        }
        break;
      }
    }
    return {
      text: text,
      chars: normalizedChars,
      after: after,
      lastSource: lastSource,
      truncated: truncated
    };
  }

  function normalizeLocalPageLayout(raw, charCount, pageWidth, pageHeight) {
    if (raw == null) return null;
    try {
      if (typeof pageWidth !== "number" || !Number.isFinite(pageWidth) ||
          pageWidth <= 0 || typeof pageHeight !== "number" ||
          !Number.isFinite(pageHeight) || pageHeight <= 0) {
        throw new Error("本机页面布局缺少页面尺寸");
      }
      var topKeys = ["schema", "textSource", "layoutSource", "mode",
        "readingDirection", "confidence", "gridColumns", "gridRows",
        "regions", "tables"];
      var regionKeys = ["id", "kind", "order", "bounds", "ranges",
        "gridRow", "gridColumn", "rowSpan", "columnSpan", "vertical",
        "tableId", "row", "column"];
      var tableKeys = ["id", "rows", "columns", "xEdges", "yEdges"];
      exactObject(raw, topKeys, [], "本机页面布局");
      if (raw.schema !== "reader-page-layout/1" ||
          ["vision", "unavailable"].indexOf(raw.textSource) < 0 ||
          ["manga", "ruled-table", "vision"].indexOf(raw.layoutSource) < 0 ||
          ["manga", "table", "vision", "fallback"].indexOf(raw.mode) < 0 ||
          ["ltr", "rtl"].indexOf(raw.readingDirection) < 0 ||
          ["high", "low", "fallback"].indexOf(raw.confidence) < 0 ||
          !Number.isSafeInteger(raw.gridColumns) || raw.gridColumns < 1 ||
          raw.gridColumns > 8 || !Number.isSafeInteger(raw.gridRows) ||
          raw.gridRows < 0 || raw.gridRows > 4096 ||
          !Array.isArray(raw.regions) || raw.regions.length > 4096 ||
          !Array.isArray(raw.tables) || raw.tables.length > 64) {
        throw new Error("本机页面布局无效");
      }
      if (raw.textSource === "unavailable") {
        if (raw.mode !== "fallback" || raw.confidence !== "fallback" ||
            raw.layoutSource !== "vision" || raw.gridRows !== 0 ||
            raw.regions.length || raw.tables.length) {
          throw new Error("本机页面布局无效");
        }
        return {
          schema: raw.schema, textSource: raw.textSource,
          layoutSource: raw.layoutSource, mode: raw.mode,
          readingDirection: raw.readingDirection, confidence: raw.confidence,
          gridColumns: raw.gridColumns, gridRows: 0, regions: [], tables: []
        };
      }
      if (!charCount || !raw.regions.length || !raw.gridRows ||
          raw.mode === "fallback" || raw.confidence === "fallback" ||
          (raw.mode === "manga" &&
            (raw.layoutSource !== "manga" || raw.gridColumns !== 4)) ||
          (raw.mode === "table" && raw.layoutSource !== "ruled-table") ||
          (raw.mode === "vision" && raw.layoutSource !== "vision")) {
        throw new Error("本机页面布局无效");
      }
      var tablesById = Object.create(null);
      var totalTableCells = 0;
      var tables = raw.tables.map(function (table) {
        exactObject(table, tableKeys, [], "本机页面表格布局");
        if (!Number.isSafeInteger(table.id) || table.id < 0 ||
            tablesById[table.id] || !Number.isSafeInteger(table.rows) ||
            table.rows < 1 || table.rows > 4096 ||
            !Number.isSafeInteger(table.columns) || table.columns < 2 ||
            table.columns > 4096) {
          throw new Error("本机页面表格布局无效");
        }
        totalTableCells += table.rows * table.columns;
        if (totalTableCells > 16384) {
          throw new Error("本机页面表格布局无效");
        }
        function normalizedEdges(values, count, maximum) {
          if (!Array.isArray(values) || values.length !== count) {
            throw new Error("本机页面表格布局无效");
          }
          return values.map(function (value, index) {
            if (typeof value !== "number" || !Number.isFinite(value) || value < 0 ||
                value > maximum || (index && value <= values[index - 1])) {
              throw new Error("本机页面表格布局无效");
            }
            return value;
          });
        }
        var normalized = {
          id: table.id, rows: table.rows, columns: table.columns,
          xEdges: normalizedEdges(table.xEdges, table.columns + 1, pageWidth),
          yEdges: normalizedEdges(table.yEdges, table.rows + 1, pageHeight)
        };
        tablesById[table.id] = normalized;
        return normalized;
      });
      if ((raw.mode === "table") !== (tables.length > 0)) {
        throw new Error("本机页面布局无效");
      }
      var covered = new Uint8Array(charCount);
      var ids = Object.create(null);
      var orders = Object.create(null);
      var totalRanges = 0;
      var regions = raw.regions.map(function (region) {
        exactObject(region, regionKeys, [], "本机页面区域布局");
        if (!Number.isSafeInteger(region.id) || region.id < 0 || ids[region.id] ||
            !Number.isSafeInteger(region.order) || region.order < 0 ||
            orders[region.order] ||
            ["manga-region", "vision-supplement", "table-cell", "vision-block"]
              .indexOf(region.kind) < 0 ||
            !Array.isArray(region.bounds) || region.bounds.length !== 4 ||
            !region.bounds.every(function (value) {
              return typeof value === "number" && Number.isFinite(value) && value >= 0;
            }) || region.bounds[2] < region.bounds[0] ||
            region.bounds[3] < region.bounds[1] ||
            region.bounds[2] > pageWidth || region.bounds[3] > pageHeight ||
            !Array.isArray(region.ranges) || !region.ranges.length ||
            !Number.isSafeInteger(region.gridRow) || region.gridRow < 0 ||
            !Number.isSafeInteger(region.gridColumn) || region.gridColumn < 0 ||
            !Number.isSafeInteger(region.rowSpan) || region.rowSpan < 1 ||
            !Number.isSafeInteger(region.columnSpan) || region.columnSpan < 1 ||
            region.gridRow + region.rowSpan > raw.gridRows ||
            region.gridColumn + region.columnSpan > raw.gridColumns ||
            typeof region.vertical !== "boolean") {
          throw new Error("本机页面区域布局无效");
        }
        ids[region.id] = true;
        orders[region.order] = true;
        var previousEnd = -1;
        var ranges = region.ranges.map(function (range) {
          totalRanges += 1;
          if (!Array.isArray(range) || range.length !== 2 ||
              totalRanges > Math.max(charCount, 1) ||
              !Number.isSafeInteger(range[0]) || !Number.isSafeInteger(range[1]) ||
              range[0] < 0 || range[1] < range[0] || range[1] >= charCount ||
              range[0] <= previousEnd) {
            throw new Error("本机页面区域范围无效");
          }
          previousEnd = range[1];
          for (var index = range[0]; index <= range[1]; index += 1) {
            if (covered[index]) throw new Error("本机页面区域范围重叠");
            covered[index] = 1;
          }
          return [range[0], range[1]];
        });
        var tableId = region.tableId;
        var row = region.row;
        var column = region.column;
        if (region.kind === "table-cell") {
          var table = Number.isSafeInteger(tableId) && tableId >= 0
            ? tablesById[tableId] : null;
          if (!table || !Number.isSafeInteger(row) || row < 0 ||
              !Number.isSafeInteger(column) || column < 0 ||
              row + region.rowSpan > table.rows ||
              column + region.columnSpan > table.columns) {
            throw new Error("本机页面单元格布局无效");
          }
        } else if (tableId !== null || row !== null || column !== null) {
          throw new Error("本机页面区域表格字段无效");
        }
        return {
          id: region.id, kind: region.kind, order: region.order,
          bounds: region.bounds.slice(), ranges: ranges,
          gridRow: region.gridRow, gridColumn: region.gridColumn,
          rowSpan: region.rowSpan, columnSpan: region.columnSpan,
          vertical: region.vertical, tableId: tableId, row: row, column: column
        };
      });
      for (var index = 0; index < covered.length; index += 1) {
        if (!covered[index]) throw new Error("本机页面区域未完整覆盖字符");
      }
      return {
        schema: raw.schema, textSource: raw.textSource,
        layoutSource: raw.layoutSource, mode: raw.mode,
        readingDirection: raw.readingDirection, confidence: raw.confidence,
        gridColumns: raw.gridColumns, gridRows: raw.gridRows,
        regions: regions, tables: tables
      };
    } catch (_) {
      return null;
    }
  }

  function localPageRecord(page, fallbackText) {
    page = Number(page) || 0;
    var fallback = cleanLocalPageText(
      fallbackText || localDOMPageText(page, false),
      LOCAL_PAGE_CONTEXT_LIMIT
    );
    if (!page) return Promise.resolve(emptyLocalPageRecord(fallback));
    try {
      var provider = window.BWReaderRuntime &&
        window.BWReaderRuntime.pageTextProvider;
      if (!provider || provider.contract !== "reader-page-text-provider/1" ||
          typeof provider.pageChars !== "function") {
        return Promise.resolve(emptyLocalPageRecord(fallback));
      }
      return boundedLocalPageTask(provider.pageChars(page), null).then(
        function (result) {
          var chars = result && Array.isArray(result.chars) ? result.chars : [];
          var record = normalizeLocalPageChars(chars, LOCAL_PAGE_CONTEXT_LIMIT);
          record.layout = normalizeLocalPageLayout(
            result && result.layout, record.chars.length,
            Number(result && result.pageWidth), Number(result && result.pageHeight)
          );
          record.layoutFallback = !!(
            result && (result.layoutFallback === true ||
              (result.layout && (!record.layout ||
                record.layout.textSource !== "vision" ||
                record.layout.confidence !== "high" ||
                record.layout.mode === "fallback")))
          );
          return record.text ? record : emptyLocalPageRecord(fallback);
        }
      );
    } catch (_) { return Promise.resolve(emptyLocalPageRecord(fallback)); }
  }

  function assertCompleteLocalPageCardReplacement(card) {
    if (card.contentTruncated === true) return;
    var replacement;
    try {
      replacement = JSON.parse(card.contextContent);
    } catch (_) {
      throw new Error("本机权威卡片替换正文不是 JSON");
    }
    if (!plainObject(replacement)) {
      throw new Error("本机权威卡片替换正文无效");
    }
    if (card.replacement === "content") {
      exactObject(replacement, ["content"], [], "本机权威卡片替换正文");
      if (card.kind !== "card" || typeof replacement.content !== "string" ||
          !replacement.content.trim()) {
        throw new Error("本机权威通用卡片替换正文无效");
      }
      return;
    }
    exactObject(replacement, ["cards"], [], "本机权威卡片替换正文");
    if (card.kind !== "anki") {
      throw new Error("本机权威学习卡替换正文无效");
    }
    if (!Array.isArray(replacement.cards) || replacement.cards.length < 1 ||
        replacement.cards.length > 12) {
      throw new Error("本机权威学习卡替换正文无效");
    }
    replacement.cards.forEach(function (face) {
      if (!plainObject(face)) {
        throw new Error("本机权威学习卡替换正文无效");
      }
      if (face.type === "basic") {
        exactObject(face, ["type", "front", "back"], [], "本机权威学习卡替换正文");
        if (typeof face.front !== "string" || !face.front.trim() ||
            typeof face.back !== "string" || !face.back.trim()) {
          throw new Error("本机权威学习卡替换正文无效");
        }
        return;
      }
      exactObject(face, ["type", "cloze"], [], "本机权威学习卡替换正文");
      if (face.type !== "cloze" || typeof face.cloze !== "string" ||
          !/\{\{c[1-9][0-9]*::[\s\S]+?\}\}/.test(face.cloze)) {
        throw new Error("本机权威学习卡替换正文无效");
      }
    });
  }

  function localPageCardRecords(runtime, page) {
    if (!runtime || typeof runtime.pageContextCards !== "function") {
      // Capability skew must not regress the pre-existing plain-text context.
      // We still never synthesize cards from DOM; only an available App-owned
      // authoritative provider is allowed to add CARD markers.
      return Promise.resolve({ revision: null, cards: [] });
    }
    var failed = {};
    return boundedLocalPageTask(runtime.pageContextCards({ page: page }), failed)
      .then(function (result) {
        if (result === failed || !result || typeof result !== "object" ||
            result.contract !== LOCAL_PAGE_CARDS_CONTRACT ||
            Number(result.page) !== page ||
            !Number.isSafeInteger(Number(result.revision)) ||
            Number(result.revision) < 0 || !Array.isArray(result.cards) ||
            result.cards.length > 2000) {
          throw new Error("本机权威卡片投影无效");
        }
        return {
          revision: Number(result.revision),
          cards: result.cards.map(function (card, sourceIndex) {
          var bind = card && card.bind;
          var from = bind && Number(bind.from);
          var to = bind && Number(bind.to);
          var unbound = !!(card && card.unbound === true);
          if (!card || typeof card !== "object" || Array.isArray(card) ||
              (card.kind !== "anki" && card.kind !== "card") ||
              typeof card.id !== "string" ||
              !/^[A-Za-z0-9_-]{2,96}$/.test(card.id) ||
              typeof card.label !== "string" || card.label.length > 120 ||
              typeof card.text !== "string" ||
              card.text.length > LOCAL_PAGE_CARD_CONTEXT_LIMIT ||
              typeof card.contextContent !== "string" ||
              card.contextContent.length > LOCAL_PAGE_CARD_CONTEXT_LIMIT ||
              !Number.isSafeInteger(card.contentLength) ||
              card.contentLength < 0 ||
              (card.contentTruncated === false
                ? card.contentLength !== card.contextContent.length
                : card.contentLength <= card.contextContent.length) ||
              card.contentFormat !== LOCAL_PAGE_CARD_REPLACEMENT_FORMAT ||
              card.replacement !== (card.kind === "anki" ? "cards" : "content") ||
              typeof card.contentTruncated !== "boolean" || (
                unbound
                  ? (card.number !== null || bind !== null)
                  : (!bind || typeof bind !== "object" || Array.isArray(bind) ||
                    bind.kind !== "page-chars" || Number(bind.page) !== page ||
                    !Number.isSafeInteger(from) || !Number.isSafeInteger(to) ||
                    from < 0 || to < from || to > 1000000 ||
                    typeof bind.text !== "string" || bind.text.length > 200)
              )) {
            throw new Error("本机权威卡片条目无效");
          }
          assertCompleteLocalPageCardReplacement(card);
          var normalized = {
            id: card.id,
            kind: card.kind,
            label: card.label,
            text: card.text,
            contextContent: card.contextContent,
            contentLength: card.contentLength,
            contentFormat: card.contentFormat,
            replacement: card.replacement,
            contentTruncated: card.contentTruncated,
            sourceIndex: sourceIndex
          };
          if (unbound) {
            normalized.bind = null;
            normalized.number = null;
            normalized.unbound = true;
          } else {
            normalized.bind = {
              kind: "page-chars", page: page, from: from, to: to,
              text: bind.text
            };
          }
            return normalized;
          })
        };
      });
  }

  function stripLocalCardWhitespace(value) {
    return String(value || "").replace(/\s+/g, "");
  }

  // Pure projection of a persisted page-chars anchor onto one authoritative
  // provider array. It intentionally mirrors reader.src/34-bindcard.js and
  // never consults page-wrap/__charBoxes or card component DOM.
  function resolveLocalCardRange(chars, want) {
    var from = Number(want && want.from);
    var to = Number(want && want.to);
    if (!Number.isSafeInteger(from) || !Number.isSafeInteger(to)) return null;
    to = Math.max(from, to);
    var text = stripLocalCardWhitespace(want && want.text);
    // 绑定自带字符索引集合(2026-09-02 词锚改为字符集合语义)时优先按它解析:
    // 跨行/跨格的词 from..to 区间会夹进别的字符,按区间对文本必失配。
    if (Array.isArray(want && want.ois) && want.ois.length) {
      var oisBoxes = [];
      var oisText = "";
      var lo = Infinity, hi = -1;
      want.ois.slice().sort(function (a, b) { return a - b; }).forEach(function (oi) {
        var item = chars[oi];
        if (!item) return;
        oisBoxes.push(item);
        if (!item.sp && item.c) oisText += String(item.c);
        lo = Math.min(lo, oi); hi = Math.max(hi, oi);
      });
      if (oisBoxes.length && hi >= 0 &&
          (!text || stripLocalCardWhitespace(oisText) === text)) {
        return { lo: lo, hi: hi, boxes: oisBoxes };
      }
    }
    var hit = [];
    var got = "";
    for (var index = 0; index < chars.length; index += 1) {
      var item = chars[index];
      if (!item || index < from || index > to) continue;
      hit.push(item);
      if (!item.sp && item.c) got += String(item.c);
    }
    if (hit.length && (!text || stripLocalCardWhitespace(got) === text)) {
      return { lo: from, hi: to, boxes: hit };
    }
    if (!text) return null;
    var joined = "";
    var sourceIndexes = [];
    for (var cursor = 0; cursor < chars.length; cursor += 1) {
      var character = chars[cursor];
      if (!character || !character.c || character.sp) continue;
      var sourceText = String(character.c);
      for (var unit = 0; unit < sourceText.length; unit += 1) {
        joined += sourceText.charAt(unit);
        sourceIndexes.push(cursor);
      }
    }
    var best = -1;
    var bestDistance = Infinity;
    var at = joined.indexOf(text);
    while (at >= 0) {
      var distance = Math.abs(sourceIndexes[at] - from);
      if (distance < bestDistance) {
        bestDistance = distance;
        best = at;
      }
      at = joined.indexOf(text, at + 1);
    }
    if (best >= 0) {
      var lo = sourceIndexes[best];
      var hi = sourceIndexes[Math.min(best + text.length - 1,
        sourceIndexes.length - 1)];
      return { lo: lo, hi: hi, boxes: chars.slice(lo, hi + 1) };
    }
    // 第三策略(2026-09-03 实锤 コチュジャン):词跨行/跨格时,源序里两段之间夹着别的格子的字,
    // 连续搜索必失配,而阅读器本身是按字符集合锚定的。这里允许有间隔的子序列匹配,再用几何
    // 校验(所有匹配字落在 3 个行高内、横向不超页宽六成)排除"东拼西凑"的假命中。
    var units = Array.from(text);
    if (units.length < 2 || units.length > 64) return null;
    var startFrom = Number.isSafeInteger(from) ? from : 0;
    var pageW = 0;
    for (var pw = 0; pw < chars.length; pw += 1) {
      if (chars[pw] && Number.isFinite(Number(chars[pw].x1))) pageW = Math.max(pageW, Number(chars[pw].x1));
    }
    var bestSeq = null, bestSeqDistance = Infinity;
    for (var s0 = 0; s0 < chars.length; s0 += 1) {
      var c0 = chars[s0];
      if (!c0 || c0.sp || String(c0.c || "").charAt(0) !== units[0]) continue;
      var picked = [c0], pickedIdx = [s0], ui = 1, gap = 0;
      for (var s1 = s0 + 1; s1 < chars.length && ui < units.length && gap <= 80; s1 += 1) {
        var c1 = chars[s1];
        if (!c1 || c1.sp || !c1.c) continue;
        if (String(c1.c).charAt(0) === units[ui]) { picked.push(c1); pickedIdx.push(s1); ui += 1; gap = 0; }
        else gap += 1;
      }
      if (ui !== units.length) continue;
      var xs0 = Infinity, xs1 = -Infinity, ys0 = Infinity, ys1 = -Infinity, lh = 0;
      picked.forEach(function (b) {
        xs0 = Math.min(xs0, Number(b.x0)); xs1 = Math.max(xs1, Number(b.x1));
        ys0 = Math.min(ys0, Number(b.y0)); ys1 = Math.max(ys1, Number(b.y1));
        lh = Math.max(lh, Number(b.y1) - Number(b.y0));
      });
      if (!(lh > 0) || (ys1 - ys0) > lh * 3 || (pageW && (xs1 - xs0) > pageW * 0.6)) continue;
      var d0 = Math.abs(s0 - startFrom);
      if (d0 < bestSeqDistance) {
        bestSeqDistance = d0;
        bestSeq = { lo: pickedIdx[0], hi: pickedIdx[pickedIdx.length - 1], boxes: picked };
      }
    }
    return bestSeq;
  }

  function localCardGeometry(range) {
    var picked = (range && range.boxes || []).filter(function (box) {
      return box && !box.sp && [box.x0, box.y0, box.x1, box.y1]
        .every(function (value) { return Number.isFinite(Number(value)); });
    }).map(function (box) {
      return {
        x0: Number(box.x0), y0: Number(box.y0),
        x1: Number(box.x1), y1: Number(box.y1)
      };
    });
    picked.sort(function (left, right) {
      var leftHeight = Math.max(1, left.y1 - left.y0);
      var rightHeight = Math.max(1, right.y1 - right.y0);
      var baseline = left.y1 - right.y1;
      return Math.abs(baseline) > Math.max(leftHeight, rightHeight) * 0.6
        ? baseline : left.x0 - right.x0;
    });
    var lines = [];
    var current = null;
    picked.forEach(function (box) {
      var height = Math.max(1, box.y1 - box.y0);
      if (current && Math.abs(box.y1 - current.base) < height * 0.6) {
        current.x0 = Math.min(current.x0, box.x0);
        current.y0 = Math.min(current.y0, box.y0);
        current.x1 = Math.max(current.x1, box.x1);
        current.y1 = Math.max(current.y1, box.y1);
      } else {
        current = {
          base: box.y1, x0: box.x0, y0: box.y0,
          x1: box.x1, y1: box.y1
        };
        lines.push(current);
      }
    });
    if (!lines.length) return null;
    var last = lines[lines.length - 1];
    return {
      x: last.x1,
      y: last.y0,
      rowHeight: Math.max(1, lines[0].y1 - lines[0].y0)
    };
  }

  // Page order and n= numbering are derived together from the same resolved
  // anchors as the visible marks: rows top-to-bottom, then left-to-right.
  function projectLocalPageCards(pageRecord, cards) {
    var projected = [];
    cards.forEach(function (card) {
      if (card.unbound === true) return;
      var range = resolveLocalCardRange(pageRecord.chars, card.bind);
      if (!range || range.hi > pageRecord.lastSource ||
          !Number.isSafeInteger(pageRecord.after[range.hi])) return;
      var geometry = localCardGeometry(range);
      if (!geometry) return;
      projected.push({
        card: card,
        range: range,
        offset: pageRecord.after[range.hi],
        geometry: geometry
      });
    });
    var rowTolerance = projected.reduce(function (maximum, item) {
      return Math.max(maximum, item.geometry ? item.geometry.rowHeight * 0.5 : 0);
    }, 6);
    projected.sort(function (left, right) {
      if (Math.abs(left.geometry.y - right.geometry.y) > rowTolerance) {
        return left.geometry.y - right.geometry.y;
      }
      var horizontal = left.geometry.x - right.geometry.x;
      if (horizontal) return horizontal;
      return left.card.sourceIndex - right.card.sourceIndex;
    });
    projected.forEach(function (item, index) { item.number = index + 1; });
    return projected;
  }

  function buildLocalPageCardProjection(page, pageRecord, recordSet) {
    recordSet = recordSet && typeof recordSet === "object"
      ? recordSet : { revision: null, cards: [] };
    var cards = Array.isArray(recordSet.cards) ? recordSet.cards : [];
    var revision = Number.isSafeInteger(recordSet.revision)
      ? recordSet.revision : null;
    var projected = projectLocalPageCards(pageRecord, cards);
    var numberedIndexes = Object.create(null);
    var publicCards = projected.map(function (item) {
      numberedIndexes[item.card.sourceIndex] = true;
      return {
        number: item.number,
        id: item.card.id,
        kind: item.card.kind,
        type: item.card.kind,
        label: item.card.label,
        text: item.card.text,
        content: item.card.text,
        bind: {
          kind: "page-chars",
          page: page,
          from: item.card.bind.from,
          to: item.card.bind.to,
          text: item.card.bind.text
        },
        revision: revision,
        unbound: false
      };
    });
    var unboundCards = [];
    var unresolvedCards = [];
    cards.forEach(function (card) {
      if (numberedIndexes[card.sourceIndex]) return;
      // A persisted bind that cannot be resolved is not a free card, so it must
      // not be published as unbound (validator: unbound => bind:null). 2026-09-03
      // 用户实锤:此前这里整页抛错 —— 一张 OCR 重跑后失配的旧卡让第 46 页永远
      // "无文字层"。现在这张卡从本次投影**跳过**并出声(dlog),正文照常上报。
      if (card.unbound !== true || card.bind !== null) {
        unresolvedCards.push({ id: card.id, label: card.label });
        return;
      }
      var fallback = {
        number: null,
        id: card.id,
        kind: card.kind,
        type: card.kind,
        label: card.label,
        text: card.text,
        content: card.text,
        bind: null,
        revision: revision,
        unbound: true
      };
      publicCards.push(fallback);
      unboundCards.push({ card: card, number: null });
    });
    if (unresolvedCards.length) {
      try {
        if (window.dlog) window.dlog("快照:第 " + page + " 页 " + unresolvedCards.length +
          " 张已锚卡几何未解析,已跳过:" + unresolvedCards.map(function (c) {
            return c.label || c.id;
          }).join("、"), "#e0a040");
      } catch (_) {}
    }
    return {
      value: {
        contract: LOCAL_PAGE_CARD_PROJECTION_CONTRACT,
        page: page,
        revision: revision,
        cards: publicCards
      },
      projected: projected,
      unboundCards: unboundCards,
      unresolvedCards: unresolvedCards
    };
  }

  function pageCards(page) {
    if (page === null || page === undefined) {
      var current = localActiveReadingSnapshot();
      if (!current || current.kind !== "pdf") {
        return Promise.reject(directError(
          "Reader 当前 PDF 页不可用",
          "BW_READER_PAGE_CARDS_PAGE_UNAVAILABLE",
          true
        ));
      }
      page = current.page;
    }
    page = Number(page);
    if (!Number.isSafeInteger(page) || page < 1) {
      return Promise.reject(directError(
        "Reader 当前页卡片页码无效",
        "BW_READER_PAGE_CARDS_PAGE_INVALID",
        false
      ));
    }
    var runtime = localNativePageRuntime();
    if (!runtime || typeof runtime.pageContextCards !== "function") {
      return Promise.reject(directError(
        "Reader 当前界面没有权威卡片投影",
        "BW_READER_PAGE_CARDS_UNAVAILABLE",
        true
      ));
    }
    return Promise.all([
      localPageRecord(page, ""),
      localPageCardRecords(runtime, page)
    ]).then(function (records) {
      return buildLocalPageCardProjection(page, records[0], records[1]).value;
    });
  }

  // `reader_page_cards` is an index, not the full-content transport.  Keep it
  // below the Reader query frame budget and say exactly when only a prefix was
  // returned; `reader_page_card_read` can then fetch any number/id in chunks.
  function pageCardIndex(page) {
    return pageCards(page).then(function (projection) {
      var sourceCards = Array.isArray(projection.cards) ? projection.cards : [];
      var result = {
        contract: projection.contract,
        page: projection.page,
        revision: projection.revision,
        count: sourceCards.length,
        returned: 0,
        cards: [],
        truncated: false
      };
      var budget = 32 * 1024;
      for (var index = 0; index < sourceCards.length; index += 1) {
        var source = sourceCards[index];
        var fullContent = String(source.content || source.text || "");
        var contentChunk = localPageCardUTF8Chunk(fullContent, 0, 1600);
        var item = {
          number: source.number,
          id: source.id,
          kind: source.kind,
          type: source.type,
          label: source.label,
          content: contentChunk.content,
          content_truncated: contentChunk.end < fullContent.length,
          bind: source.bind,
          revision: source.revision,
          unbound: source.unbound === true
        };
        var candidate = Object.assign({}, result, {
          returned: result.cards.length + 1,
          cards: result.cards.concat([item]),
          truncated: index + 1 < sourceCards.length
        });
        if (messageBytes(JSON.stringify(candidate)) > budget) {
          result.truncated = true;
          break;
        }
        result.cards.push(item);
        result.returned = result.cards.length;
      }
      if (result.returned < sourceCards.length) result.truncated = true;
      return result;
    });
  }

  function localPageCardUTF8Chunk(value, offset, limit) {
    value = String(value || "");
    var end = Math.min(value.length, offset + limit);
    if (end > offset && end < value.length &&
        /[\uD800-\uDBFF]/.test(value.charAt(end - 1)) &&
        /[\uDC00-\uDFFF]/.test(value.charAt(end))) {
      end -= 1;
    }
    var low = offset;
    var high = end;
    while (low < high && messageBytes(value.slice(offset, high)) > 24576) {
      var middle = offset + Math.floor((high - offset) / 2);
      if (middle > offset && middle < value.length &&
          /[\uDC00-\uDFFF]/.test(value.charAt(middle)) &&
          /[\uD800-\uDBFF]/.test(value.charAt(middle - 1))) {
        middle -= 1;
      }
      if (middle <= offset) {
        high = offset;
        break;
      }
      high = middle;
    }
    end = high;
    while (end < value.length && end < offset + limit) {
      var step = end + 1;
      if (step < value.length &&
          /[\uD800-\uDBFF]/.test(value.charAt(step - 1)) &&
          /[\uDC00-\uDFFF]/.test(value.charAt(step))) {
        step += 1;
      }
      if (step > offset + limit ||
          messageBytes(value.slice(offset, step)) > 24576) break;
      end = step;
    }
    if (end <= offset && offset < value.length) {
      end = offset + 1;
      if (end < value.length &&
          /[\uD800-\uDBFF]/.test(value.charAt(end - 1)) &&
          /[\uDC00-\uDFFF]/.test(value.charAt(end))) end += 1;
    }
    return { content: value.slice(offset, end), end: end };
  }

  // Explicit full read for one placement.  The list/context projection stays
  // compact; this endpoint resolves the same visible number to a stable id and
  // then streams the authoritative source JSON in UTF-8-bounded chunks.
  function pageCard(rawParams) {
    var params = rawParams && typeof rawParams === "object" ? rawParams : {};
    exactObject(
      params, [], ["page", "id", "number", "offset", "limit", "expectedRevision"],
      "Reader 页面卡片详情参数"
    );
    var page = Object.prototype.hasOwnProperty.call(params, "page")
      ? Number(params.page) : null;
    if (page === null) {
      var current = localActiveReadingSnapshot();
      if (!current || current.kind !== "pdf") {
        return Promise.reject(directError(
          "Reader 当前 PDF 页不可用",
          "BW_READER_PAGE_CARD_PAGE_UNAVAILABLE",
          true
        ));
      }
      page = Number(current.page);
    }
    if (!Number.isSafeInteger(page) || page < 1) {
      return Promise.reject(directError(
        "Reader 页面卡片页码无效", "BW_READER_PAGE_CARD_PARAMS", false
      ));
    }
    var hasId = Object.prototype.hasOwnProperty.call(params, "id");
    var hasNumber = Object.prototype.hasOwnProperty.call(params, "number");
    var id = hasId ? String(params.id || "") : "";
    var number = hasNumber ? Number(params.number) : null;
    if ((hasId === hasNumber) || (hasId && !/^[A-Za-z0-9_-]{2,96}$/.test(id)) ||
        (hasNumber && (!Number.isSafeInteger(number) || number < 1))) {
      return Promise.reject(directError(
        "Reader 页面卡片选择器无效", "BW_READER_PAGE_CARD_PARAMS", false
      ));
    }
    var offset = Object.prototype.hasOwnProperty.call(params, "offset")
      ? Number(params.offset) : 0;
    var limit = Object.prototype.hasOwnProperty.call(params, "limit")
      ? Number(params.limit) : 12000;
    if (!Number.isSafeInteger(offset) || offset < 0 ||
        !Number.isSafeInteger(limit) || limit < 1 || limit > 24576) {
      return Promise.reject(directError(
        "Reader 页面卡片分块参数无效", "BW_READER_PAGE_CARD_PARAMS", false
      ));
    }
    var hasExpectedRevision = Object.prototype.hasOwnProperty.call(
      params, "expectedRevision"
    );
    var expectedRevision = hasExpectedRevision
      ? Number(params.expectedRevision) : null;
    if ((hasExpectedRevision && (!Number.isSafeInteger(expectedRevision) ||
        expectedRevision < 0)) || (offset > 0 && !hasExpectedRevision)) {
      return Promise.reject(directError(
        "Reader 页面卡片续读修订号无效", "BW_READER_PAGE_CARD_PARAMS", false
      ));
    }
    var runtime = localNativePageRuntime();
    if (!runtime || typeof runtime.pageCardSource !== "function") {
      return Promise.reject(directError(
        "Reader 当前界面没有完整卡片读取能力",
        "BW_READER_PAGE_CARD_UNAVAILABLE",
        true
      ));
    }
    return pageCards(page).then(function (projection) {
      if (hasExpectedRevision &&
          Number(projection.revision) !== expectedRevision) {
        throw directError(
          "Reader 页面卡片在分块读取期间已变化，请从 offset=0 重新读取",
          "BW_READER_PAGE_CARD_STALE", true
        );
      }
      var cards = Array.isArray(projection.cards) ? projection.cards : [];
      var picked = null;
      for (var index = 0; index < cards.length; index += 1) {
        var card = cards[index];
        if (hasId && card.id !== id) continue;
        if (hasNumber && card.number !== number) continue;
        picked = card;
        break;
      }
      if (!picked) {
        throw directError(
          "Reader 当前页找不到对应卡片",
          "BW_READER_PAGE_CARD_NOT_FOUND",
          false
        );
      }
      return Promise.resolve(runtime.pageCardSource({ page: page, id: picked.id }))
        .then(function (source) {
          if (!source || typeof source !== "object" ||
              source.contract !== LOCAL_PAGE_CARD_SOURCE_CONTRACT ||
              Number(source.page) !== page || source.id !== picked.id ||
              source.kind !== picked.kind ||
              Number(source.revision) !== Number(projection.revision) ||
              typeof source.content !== "string" ||
              offset > source.content.length) {
            throw directError(
              "Reader 页面卡片详情与当前投影不一致",
              "BW_READER_PAGE_CARD_STALE",
              true
            );
          }
          var chunk = localPageCardUTF8Chunk(source.content, offset, limit);
          var nextOffset = chunk.end < source.content.length ? chunk.end : null;
          return {
            contract: READER_PAGE_CARD_DETAIL_CONTRACT,
            page: page,
            revision: Number(projection.revision),
            card: {
              id: picked.id,
              number: picked.number,
              kind: picked.kind,
              type: picked.kind,
              label: picked.label,
              bind: picked.bind,
              unbound: picked.unbound === true,
              content_format: "application/vnd.bw-reader.card+json;version=1"
            },
            content: chunk.content,
            content_length: source.content.length,
            offset: offset,
            next_offset: nextOffset,
            truncated: nextOffset !== null
          };
        });
    });
  }

  var LEARNING_CARD_CONTRACT = "reader-learning-card/1";
  var LEARNING_CARD_LIST_BUDGET = 180 * 1024;
  var LEARNING_CARD_SOURCE_LIMIT = 128 * 1024;
  var LEARNING_CARD_SOURCE_TEXT_LIMITS = Object.freeze({
    kind: 80,
    sourceId: 4096,
    documentId: 4096,
    bookId: 4096,
    url: 8192,
    title: 1024,
    quote: 32768,
    context: 65536,
    tool: 160,
    draftId: 512,
    sourceInstanceId: 512,
    requirement: 32768,
  });
  var LEARNING_CARD_SOURCE_OBJECT_FIELDS = Object.freeze([
    "location", "anchor", "selection", "legacy",
  ]);

  function normalizeLearningCardSource(value, label) {
    label = label || "Reader 学习卡 source";
    function validNestedJSON(input, seen, depth) {
      if (input === null || typeof input === "boolean") return true;
      if (typeof input === "string") return input.indexOf("\u0000") < 0;
      if (typeof input === "number") return Number.isFinite(input);
      if (typeof input !== "object" || depth > 64 ||
          (!Array.isArray(input) && !plainObject(input)) ||
          seen.indexOf(input) >= 0) return false;
      seen.push(input);
      var valid;
      if (Array.isArray(input)) {
        valid = true;
        for (var index = 0; index < input.length; index += 1) {
          if (!Object.prototype.hasOwnProperty.call(input, index) ||
              !validNestedJSON(input[index], seen, depth + 1)) {
            valid = false;
            break;
          }
        }
      } else {
        valid = Object.keys(input).every(function (key) {
          return key.indexOf("\u0000") < 0 &&
            validNestedJSON(input[key], seen, depth + 1);
        });
      }
      seen.pop();
      return valid;
    }
    var textFields = Object.keys(LEARNING_CARD_SOURCE_TEXT_LIMITS);
    exactObject(
      value,
      ["kind"],
      textFields.filter(function (key) { return key !== "kind"; })
        .concat(LEARNING_CARD_SOURCE_OBJECT_FIELDS),
      label
    );
    var normalized = {};
    textFields.forEach(function (key) {
      if (!Object.prototype.hasOwnProperty.call(value, key)) return;
      var field = value[key];
      if (typeof field !== "string" || field.indexOf("\u0000") >= 0 ||
          messageBytes(field) > LEARNING_CARD_SOURCE_TEXT_LIMITS[key] ||
          (key === "kind" && !field.trim())) {
        throw directError(
          label + "." + key + " 无效",
          "BW_READER_LEARNING_CARD_PARAMS",
          false
        );
      }
      normalized[key] = field;
    });
    LEARNING_CARD_SOURCE_OBJECT_FIELDS.forEach(function (key) {
      if (!Object.prototype.hasOwnProperty.call(value, key)) return;
      if (!plainObject(value[key]) ||
          !validNestedJSON(value[key], [], 0)) {
        throw directError(
          label + "." + key + " 必须是对象",
          "BW_READER_LEARNING_CARD_PARAMS",
          false
        );
      }
      var serialized;
      try { serialized = JSON.stringify(value[key]); }
      catch (_) { serialized = ""; }
      if (!serialized || messageBytes(serialized) > LEARNING_CARD_SOURCE_LIMIT) {
        throw directError(
          label + "." + key + " 无效",
          "BW_READER_LEARNING_CARD_PARAMS",
          false
        );
      }
      normalized[key] = JSON.parse(serialized);
    });
    if (!["sourceId", "documentId", "bookId", "url", "draftId",
      "sourceInstanceId"].some(function (key) {
        return typeof normalized[key] === "string" && !!normalized[key].trim();
      }) || messageBytes(JSON.stringify(normalized)) > LEARNING_CARD_SOURCE_LIMIT) {
      throw directError(
        label + " 缺少稳定来源或超出大小上限",
        "BW_READER_LEARNING_CARD_PARAMS",
        false
      );
    }
    return normalized;
  }

  function learningCardRepository() {
    var repository = window.BWReaderRuntime &&
      window.BWReaderRuntime.cardRepository;
    if (!repository || typeof repository.load !== "function" ||
        typeof repository.snapshot !== "function" ||
        typeof repository.replaceEntity !== "function") {
      throw directError(
        "Reader 本地学习卡仓尚未就绪",
        "BW_READER_LEARNING_CARD_UNAVAILABLE",
        true
      );
    }
    return repository;
  }

  function learningCardId(value) {
    value = String(value == null ? "" : value).trim().toLowerCase();
    if (!/^card_[a-f0-9]{4,64}$/.test(value)) {
      throw directError(
        "Reader 学习卡 id 无效",
        "BW_READER_LEARNING_CARD_PARAMS",
        false
      );
    }
    return value;
  }

  function learningCardIndex(value) {
    value = Number(value);
    if (!Number.isSafeInteger(value) || value < 0 || value > 255) {
      throw directError(
        "Reader 学习卡 cardIndex 无效",
        "BW_READER_LEARNING_CARD_PARAMS",
        false
      );
    }
    return value;
  }

  function learningCardPublic(record, cardIndex) {
    if (!record || record.deleted || !Array.isArray(record.cards) ||
        !record.states || cardIndex >= record.cards.length ||
        !record.states[String(cardIndex)]) {
      throw directError(
        "Reader 找不到指定学习卡",
        "BW_READER_LEARNING_CARD_NOT_FOUND",
        false
      );
    }
    return {
      contract: LEARNING_CARD_CONTRACT,
      id: record.id,
      card_index: cardIndex,
      entity_revision: Number(record.entityRev || 0),
      state_revision: Number(record.stateRev || 0),
      card: JSON.parse(JSON.stringify(record.cards[cardIndex])),
      source: JSON.parse(JSON.stringify(record.source || {})),
      state: JSON.parse(JSON.stringify(record.states[String(cardIndex)])),
    };
  }

  function learningCards(rawParams) {
    var params = rawParams && typeof rawParams === "object" ? rawParams : {};
    exactObject(params, [], ["id", "contains", "limit", "includeRemoved"],
      "Reader 学习卡列表参数");
    var id = Object.prototype.hasOwnProperty.call(params, "id")
      ? learningCardId(params.id) : "";
    var contains = Object.prototype.hasOwnProperty.call(params, "contains")
      ? safeText(params.contains, "contains", 256, false).trim().toLowerCase()
      : "";
    var limit = Object.prototype.hasOwnProperty.call(params, "limit")
      ? Number(params.limit) : 50;
    if (!Number.isSafeInteger(limit) || limit < 1 || limit > 200 ||
        (Object.prototype.hasOwnProperty.call(params, "includeRemoved") &&
          typeof params.includeRemoved !== "boolean")) {
      return Promise.reject(directError(
        "Reader 学习卡列表参数无效",
        "BW_READER_LEARNING_CARD_PARAMS",
        false
      ));
    }
    var includeRemoved = params.includeRemoved === true;
    var repo;
    try { repo = learningCardRepository(); }
    catch (error) { return Promise.reject(error); }
    return repo.snapshot().then(function (records) {
      var all = [];
      (Array.isArray(records) ? records : []).forEach(function (record) {
        if (!record || record.deleted || (id && record.id !== id) ||
            !Array.isArray(record.cards) || !record.states) return;
        record.cards.forEach(function (_, index) {
          var projected;
          try { projected = learningCardPublic(record, index); }
          catch (_) { return; }
          if (!includeRemoved && projected.state.removed === true) return;
          if (contains) {
            var searchable = JSON.stringify({
              card: projected.card,
              source: projected.source,
            }).toLowerCase();
            if (searchable.indexOf(contains) < 0) return;
          }
          all.push(projected);
        });
      });
      all.sort(function (left, right) {
        return left.id.localeCompare(right.id) ||
          left.card_index - right.card_index;
      });
      var kept = [];
      for (var index = 0; index < all.length && kept.length < limit; index += 1) {
        var candidate = kept.concat([all[index]]);
        if (messageBytes(JSON.stringify({ cards: candidate })) >
            LEARNING_CARD_LIST_BUDGET) break;
        kept.push(all[index]);
      }
      return {
        contract: "reader-learning-card-list/1",
        matched: all.length,
        returned: kept.length,
        cards: kept,
        truncated: kept.length < all.length,
      };
    });
  }

  function learningCard(rawParams) {
    var params = rawParams && typeof rawParams === "object" ? rawParams : {};
    exactObject(params, ["id", "cardIndex"], [], "Reader 学习卡读取参数");
    var id;
    var cardIndex;
    var repo;
    try {
      id = learningCardId(params.id);
      cardIndex = learningCardIndex(params.cardIndex);
      repo = learningCardRepository();
    } catch (error) {
      return Promise.reject(error);
    }
    return repo.load(id).then(function (record) {
      return learningCardPublic(record, cardIndex);
    });
  }

  function currentReviewCard() {
    var current = RC.review && typeof RC.review.currentCard === "function"
      ? RC.review.currentCard() : null;
    if (!current) {
      return Promise.resolve({
        contract: "reader-review-current/1",
        active: false,
        card: null,
      });
    }
    if (current.entity_id && Number.isSafeInteger(current.entity_index)) {
      return learningCard({
        id: current.entity_id,
        cardIndex: current.entity_index,
      }).then(function (card) {
        return {
          contract: "reader-review-current/1",
          active: true,
          revealed: current._showBack === true,
          card: card,
        };
      });
    }
    return Promise.resolve({
      contract: "reader-review-current/1",
      active: true,
      revealed: current._showBack === true,
      card: {
        legacy: true,
        card_id: current.card_id || current.id || null,
        note_id: current.note_id || current.noteId || null,
        question: String(current.question || current.front || ""),
        answer: String(current.answer || current.back || ""),
        source_ref: String(current.source_ref || ""),
        source_url: String(current.source_url || ""),
      },
    });
  }

  function compactAnkiOperationResult(value) {
    value = value && typeof value === "object" ? value : {};
    var localStatus = value.anki_local_applied &&
      typeof value.anki_local_applied === "object"
      ? String(value.anki_local_applied.status || "failed")
      : (value.anki_local_applied === true
        ? "succeeded"
        : String(value.anki_local_status || "failed"));
    return {
      operation: String(value.operation || ""),
      anki_local_applied: {
        status: localStatus,
      },
      anki_web_sync: {
        status: String(value.anki_web_sync &&
          value.anki_web_sync.status || "not-requested"),
        error: String(value.anki_web_sync &&
          value.anki_web_sync.error || "").slice(0, 1000),
      },
    };
  }

  function escapeAnkiProvenance(value, attribute) {
    var escaped = String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
    return attribute ? escaped.replace(/"/g, "&quot;") : escaped;
  }

  function safeAnkiSourceUrl(value) {
    var raw = String(value == null ? "" : value).trim().slice(0, 8192);
    if (!raw || /[\u0000-\u001f\u007f]/.test(raw)) return "";
    var parsed;
    try { parsed = new URL(raw); }
    catch (_) { return ""; }
    var protocol = String(parsed.protocol || "").toLowerCase();
    if (protocol === "http:" || protocol === "https:") {
      if (!parsed.hostname || parsed.username || parsed.password) return "";
      return parsed.toString();
    }
    // Mirror the review UI's fail-closed Obsidian deep-link contract.  Only
    // obsidian://open with one decoded, vault-relative file is clickable.
    if (protocol !== "obsidian:" ||
        String(parsed.hostname || "").toLowerCase() !== "open" ||
        (parsed.pathname !== "" && parsed.pathname !== "/") ||
        parsed.username || parsed.password || parsed.hash) return "";
    var files = parsed.searchParams.getAll("file");
    if (files.length !== 1) return "";
    var file = String(files[0] || "").trim();
    for (var decodePass = 0; decodePass < 5; decodePass += 1) {
      if (!file || /[\u0000-\u001f\u007f]/.test(file) ||
          file.charAt(0) === "/" || file.charAt(0) === "\\" ||
          /^[a-z][a-z0-9+.-]*:/i.test(file) ||
          file.replace(/\\/g, "/").split("/").some(function (part) {
            return part.trim() === "." || part.trim() === "..";
          })) return "";
      var decoded;
      try { decoded = decodeURIComponent(file); }
      catch (_) { return ""; }
      if (decoded === file) return parsed.toString();
      file = decoded.trim();
    }
    // Excessively nested encoding is ambiguous and must not become a link.
    return "";
  }

  function ankiSourcePage(source) {
    var candidates = [source && source.location, source && source.anchor,
      source && source.selection];
    for (var index = 0; index < candidates.length; index += 1) {
      var value = candidates[index];
      if (!value || typeof value !== "object") continue;
      var page = Number(value.page != null ? value.page :
        (value.pageNumber != null ? value.pageNumber :
          (value.unit === "page" ? value.index : null)));
      if (Number.isSafeInteger(page) && page > 0) return page;
    }
    return null;
  }

  function normalizeAnkiSourceReference(value) {
    var raw = String(value == null ? "" : value).trim().slice(0, 2000);
    if (!raw || /[\u0000-\u001f\u007f-\u009f]/.test(raw)) return "";
    var readerBook = /^reader-book:([\s\S]+)$/i.exec(raw);
    if (readerBook) {
      if (!readerBook[1].trim()) return "";
      raw = "book:" + readerBook[1];
    }
    var typed = /^(book|note|web|kg|anki):([\s\S]+)$/i.exec(raw);
    if (typed) {
      if (!typed[2].trim()) return "";
      if (typed[1].toLowerCase() === "web") {
        var webUrl = safeAnkiSourceUrl(typed[2]);
        return webUrl && !/^obsidian:/i.test(webUrl) ? "web:" + webUrl : "";
      }
      return typed[1].toLowerCase() + ":" + typed[2];
    }
    var url = safeAnkiSourceUrl(raw);
    if (url) return /^https?:/i.test(url) ? "web:" + url : url;
    if (/\.md(?:#.*)?$/i.test(raw)) return "note:" + raw;
    if (/\.(?:pdf|epub|html?|md)(?:#.*)?$/i.test(raw)) return "book:" + raw;
    return "";
  }

  function ankiSourceReference(source) {
    source = source && typeof source === "object" ? source : {};
    var reference = normalizeAnkiSourceReference(source.sourceId);
    if (!reference) {
      var sourceUrl = safeAnkiSourceUrl(source.url);
      if (sourceUrl) {
        reference = /^https?:/i.test(sourceUrl)
          ? "web:" + sourceUrl : sourceUrl;
      }
    }
    if (!reference) {
      var documentId = String(source.documentId || source.bookId || "").trim();
      if (!documentId) return "";
      reference = normalizeAnkiSourceReference(documentId);
      if (!reference) {
        reference = /note/i.test(String(source.kind || "")) || /\.md$/i.test(documentId)
          ? "note:" + documentId
          : "book:" + documentId;
      }
    }
    var page = ankiSourcePage(source);
    if (page && reference.indexOf("#") < 0 && /^book:/i.test(reference)) {
      reference += "#p" + page;
    }
    return normalizeAnkiSourceReference(reference);
  }

  function stripAnkiProvenance(value) {
    var root = document.createElement("div");
    root.innerHTML = String(value == null ? "" : value);
    function trailingNode() {
      var node = root.lastChild;
      while (node && node.nodeType === 3 && !String(node.nodeValue || "").trim()) {
        var previous = node.previousSibling;
        root.removeChild(node);
        node = previous;
      }
      return node;
    }
    function provenanceComment(node) {
      return node && node.nodeType === 8 &&
        /^\s*@(src|entity):/i.test(String(node.nodeValue || ""));
    }
    var node = trailingNode();
    while (provenanceComment(node)) {
      root.removeChild(node);
      node = trailingNode();
    }
    var reserved = node && node.nodeType === 1 &&
      node.tagName.toLowerCase() === "div" &&
      (node.classList.contains("bw-reader-anki-source") || (function () {
        var style = String(node.getAttribute("style") || "")
          .toLowerCase().replace(/\s+/g, "");
        return /^来源\s*[：:]/.test(String(node.textContent || "").trim()) &&
          style.indexOf("font-size:0.85em") >= 0 &&
          style.indexOf("color:#666") >= 0;
      })());
    if (reserved) {
      var before = node.previousSibling;
      root.removeChild(node);
      while (before && before.nodeType === 3 &&
          !String(before.nodeValue || "").trim()) {
        var beforeWhitespace = before.previousSibling;
        root.removeChild(before);
        before = beforeWhitespace;
      }
      if (before && before.nodeType === 1 &&
          before.tagName.toLowerCase() === "hr") {
        root.removeChild(before);
      }
    }
    return root.innerHTML;
  }

  function ankiProvenanceFooter(source, entityId, cardIndex) {
    var reference = ankiSourceReference(source);
    var safeEntityId = /^card_[a-f0-9]{4,64}$/.test(String(entityId || ""))
      ? String(entityId) : "";
    var markerReference = reference.replace(/--/g, "%2D%2D");
    var markers = markerReference ? "<!--@src:" + markerReference + "-->" : "";
    if (safeEntityId) {
      markers += "<!--@entity:" + safeEntityId + ":" + cardIndex + "-->";
    }
    // A synthetic Reader draft id is not a material source.  In that case
    // stale visible provenance is removed while the stable entity marker is
    // retained for future id-based edits/deletes.
    if (!reference) return markers;
    source = source && typeof source === "object" ? source : {};
    var label = String(source.title || "").trim().slice(0, 1024) || reference;
    if (label !== reference) label += " · " + reference;
    var href = safeAnkiSourceUrl(source.url);
    if (!href && /^web:/i.test(reference)) {
      href = safeAnkiSourceUrl(reference.slice(4));
    }
    var visibleSource = href
      ? '<a href="' + escapeAnkiProvenance(href, true) +
        '" rel="noopener noreferrer">' + escapeAnkiProvenance(label, false) +
        "</a>"
      : escapeAnkiProvenance(label, false);
    var lines = ["来源：" + visibleSource];
    if (safeEntityId) {
      lines.push("卡片编号：" + escapeAnkiProvenance(safeEntityId, false));
    }
    return '<hr><div class="bw-reader-anki-source">' +
      lines.join("<br>") + "</div>" + markers;
  }

  function ankiFieldsForReaderCard(note, card, source, entityId, cardIndex,
      projectionMode) {
    var fields = note && note.fields && typeof note.fields === "object"
      ? note.fields : {};
    var names = Object.keys(fields);
    function named(wanted) {
      var lower = wanted.toLowerCase();
      for (var index = 0; index < names.length; index += 1) {
        if (names[index].toLowerCase() === lower) return names[index];
      }
      return null;
    }
    function ordered() {
      return names.slice().sort(function (left, right) {
        var leftOrder = Number(fields[left] && fields[left].order);
        var rightOrder = Number(fields[right] && fields[right].order);
        if (!Number.isFinite(leftOrder)) leftOrder = names.indexOf(left);
        if (!Number.isFinite(rightOrder)) rightOrder = names.indexOf(right);
        return leftOrder - rightOrder;
      });
    }
    function currentValue(name) {
      var field = name && fields[name];
      if (field && typeof field === "object" &&
          typeof field.value === "string") return field.value;
      return typeof field === "string" ? field : "";
    }
    function projected(value) {
      return stripAnkiProvenance(ankiProjectionHtml(value));
    }
    var footer = ankiProvenanceFooter(source, entityId, cardIndex);
    var provenanceOnly = projectionMode === "provenance-only";
    var result = {};
    if (card.type === "cloze") {
      var textField = named("Text") || named("Cloze") || ordered()[0];
      if (!textField) throw directError(
        "Anki note 没有可更新的挖空字段",
        "BW_READER_ANKI_FIELD_MAP",
        false
      );
      var extraField = named("Back Extra") || named("BackExtra") ||
        named("背面额外") || ordered().filter(function (name) {
          return name !== textField;
        })[0];
      if (extraField) {
        result[extraField] =
          stripAnkiProvenance(currentValue(extraField)) + footer;
        if (provenanceOnly) {
          // Older Reader projections appended provenance to Text even when a
          // Back Extra field existed.  Remove that legacy footer while moving
          // provenance to Back Extra, without changing the cloze body.
          result[textField] = stripAnkiProvenance(currentValue(textField));
        } else {
          result[textField] = projected(card.cloze || card.text || "");
        }
      } else {
        result[textField] = provenanceOnly
          ? stripAnkiProvenance(currentValue(textField)) + footer
          : projected(card.cloze || card.text || "") + footer;
      }
      return result;
    }
    var front = named("Front") || named("正面");
    var back = named("Back") || named("背面");
    var fallbacks = ordered();
    front = front || fallbacks[0];
    back = back || fallbacks.filter(function (name) { return name !== front; })[0];
    if (!front) throw directError(
      "Anki note 没有可更新的正面字段",
      "BW_READER_ANKI_FIELD_MAP",
      false
    );
    if (provenanceOnly) {
      var footerField = back || front;
      result[footerField] =
        stripAnkiProvenance(currentValue(footerField)) + footer;
      return result;
    }
    if (!back) {
      result[front] = projected(card.front || "") + "<hr>" +
        projected(card.back || "") + footer;
      return result;
    }
    result[front] = projected(card.front || "");
    result[back] = projected(card.back || "") + footer;
    return result;
  }

  function operatePiAnkiCard(payload) {
    payload = Object.assign({}, payload);
    var operation = String(payload.operation || "");
    var mutating = operation === "update-note-fields" ||
      operation === "delete-notes";
    function transportFailure(error, code) {
      var wrapped = directError(
        String(error && error.message || error || "Pi AnkiConnect 传输失败")
          .slice(0, 1000),
        code,
        true,
        error
      );
      if (mutating) wrapped.outcomeUnknown = true;
      return wrapped;
    }
    // Pi owns the sync step for every successful mutation.  syncMode is a
    // Windows bridge scheduling choice and is intentionally not part of the
    // protected Pi endpoint contract.
    delete payload.syncMode;
    // @interaction learning.anki-card.operate
    return fetch("/pdf/api/anki-card-operation", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(function (response) {
      return response.json().then(function (body) {
        if (!response.ok || !body || body.ok !== true) {
          var error = directError(
            String(body && (body.error || body.message) ||
              "Pi AnkiConnect 操作失败").slice(0, 1000),
            String(body && body.code || "BW_READER_PI_ANKI_OPERATION"),
            response.status >= 500
          );
          error.outcomeUnknown =
            String(body && body.code || "").toLowerCase() ===
              "outcome_unknown";
          throw error;
        }
        return body;
      }, function (error) {
        throw transportFailure(
          error,
          mutating
            ? "BW_READER_PI_ANKI_RESPONSE_OUTCOME_UNKNOWN"
            : "BW_READER_PI_ANKI_RESPONSE_INVALID"
        );
      });
    }, function (error) {
      throw transportFailure(
        error,
        mutating
          ? "BW_READER_PI_ANKI_TRANSPORT_OUTCOME_UNKNOWN"
          : "BW_READER_PI_ANKI_TRANSPORT"
      );
    });
  }

  function readProjectedAnkiNotes(target, noteIds) {
    if (target === "readerpc") {
      return operateLocalAnkiCard({
        operation: "read-notes",
        noteIds: noteIds,
      });
    }
    if (target === "pi-legacy") {
      return operatePiAnkiCard({
        operation: "read-notes",
        noteIds: noteIds,
      });
    }
    return Promise.reject(directError(
      "当前 Anki 投影不支持可靠的外部读取或修改:" + target,
      "BW_READER_ANKI_TARGET_UNSUPPORTED",
      false
    ));
  }

  function writeProjectedAnki(target, operation, mutationId, receipt, card,
      source, entityId, cardIndex, projectionMode) {
    var noteIds = Array.isArray(receipt && receipt.noteIds)
      ? receipt.noteIds.map(Number).filter(function (id) {
        return Number.isSafeInteger(id) && id > 0;
      }) : [];
    if (!noteIds.length) {
      return Promise.reject(directError(
        "Anki 投影没有可验证的 note ID",
        "BW_READER_ANKI_NOTE_ID_MISSING",
        false
      ));
    }
    if (operation === "delete") {
      var deletePayload = {
        operation: "delete-notes",
        mutationId: mutationId,
        noteIds: noteIds,
        syncMode: "background",
      };
      return target === "readerpc"
        ? operateLocalAnkiCard(deletePayload)
        : (target === "pi-legacy"
          ? operatePiAnkiCard(deletePayload)
          : Promise.reject(directError(
            "AnkiMobile 目前没有可靠的按 ID 删除接口",
            "BW_READER_ANKI_TARGET_UNSUPPORTED",
            false
          )));
    }
    return readProjectedAnkiNotes(target, noteIds).then(function (readResult) {
      var notes = Array.isArray(readResult.notes)
        ? readResult.notes
        : (Array.isArray(readResult.result) ? readResult.result : []);
      var byId = {};
      notes.forEach(function (note) {
        var noteId = Number(note.noteId != null ? note.noteId : note.note_id);
        if (Number.isSafeInteger(noteId) && noteId > 0) byId[noteId] = note;
      });
      var chain = Promise.resolve([]);
      noteIds.forEach(function (noteId, index) {
        chain = chain.then(function (results) {
          var note = byId[noteId];
          if (!note) throw directError(
            "Anki 找不到投影 note:" + noteId,
            "BW_READER_ANKI_NOTE_NOT_FOUND",
            false
          );
          var updatePayload = {
            operation: "update-note-fields",
            mutationId: mutationId + ":" + index,
            noteId: noteId,
            fields: ankiFieldsForReaderCard(
              note, card, source, entityId, cardIndex, projectionMode
            ),
            syncMode: "background",
          };
          return (target === "readerpc"
            ? operateLocalAnkiCard(updatePayload)
            : operatePiAnkiCard(updatePayload)).then(function (result) {
              results.push(result);
              return results;
            });
        });
      });
      return chain.then(function (results) {
        var failedSync = results.find(function (result) {
          var status = result && result.anki_web_sync &&
            result.anki_web_sync.status;
          return status === "failed" || status === "unknown";
        });
        return Object.assign({}, results[results.length - 1] || {}, {
          operation: "update-note-fields",
          anki_local_applied: results.every(function (result) {
            return result.anki_local_applied === true ||
              result.anki_local_applied &&
                result.anki_local_applied.status === "succeeded";
          }),
          anki_local_status: "succeeded",
          anki_web_sync: failedSync
            ? failedSync.anki_web_sync
            : (results[results.length - 1] || {}).anki_web_sync,
        });
      });
    });
  }

  function persistLearningCardReceipt(repo, id, cardIndex, target, receipt,
      storageMutationId) {
    return repo.load(id).then(function (current) {
      return repo.recordAnkiReceipt(id, cardIndex, target, receipt, {
        ifStateRev: Number(current.stateRev || 0),
        mutationId: storageMutationId,
      });
    });
  }

  function projectLearningCardMutation(repo, record, cardIndex, operation,
      mutationId, card, source, projectionMode) {
    var state = record.states[String(cardIndex)] || {};
    var projections = state.projections && state.projections.anki || {};
    var targets = Object.keys(projections).filter(function (target) {
      var receipt = projections[target] || {};
      // A known failed attempt still identifies a real external note and may
      // be safely retried by a later, newly identified semantic mutation.
      // Pending/unknown outcomes remain fenced because their side effect may
      // already have happened.
      return (receipt.status === "succeeded" || receipt.status === "failed") &&
        Array.isArray(receipt.noteIds) && receipt.noteIds.length;
    });
    var results = {};
    var chain = Promise.resolve(record);
    targets.forEach(function (target, targetIndex) {
      chain = chain.then(function (current) {
        var externalMutation = mutationId + ":" + target;
        return persistLearningCardReceipt(repo, record.id, cardIndex, target, {
          status: "pending",
          mutationId: externalMutation,
          noteIds: projections[target].noteIds,
          cardIds: projections[target].cardIds || [],
          updatedAt: Date.now(),
          detail: {
            operation: operation,
            reader_applied: { status: "succeeded" },
            anki_local_applied: { status: "pending" },
            anki_web_sync: { status: "not-requested" },
          },
        }, mutationId + ":pending:" + targetIndex).then(function (pending) {
          return writeProjectedAnki(
            target,
            operation,
            externalMutation,
            projections[target],
            card,
            source,
            record.id,
            cardIndex,
            projectionMode
          ).then(function (external) {
            var compact = compactAnkiOperationResult(external);
            results[target] = compact;
            return persistLearningCardReceipt(repo, record.id, cardIndex, target, {
              status: compact.anki_local_applied.status === "succeeded"
                ? "succeeded"
                : (compact.anki_local_applied.status === "unknown"
                  ? "unknown" : "failed"),
              mutationId: externalMutation,
              noteIds: projections[target].noteIds,
              cardIds: projections[target].cardIds || [],
              updatedAt: Date.now(),
              error: compact.anki_local_applied.status === "succeeded"
                ? "" : "AnkiConnect 操作未成功",
              detail: Object.assign({
                operation: operation,
                reader_applied: { status: "succeeded" },
              }, compact, operation === "delete" ? {
                delete_scope: "note",
                deleted_note_ids: projections[target].noteIds,
              } : {}),
            }, mutationId + ":final:" + targetIndex);
          }).catch(function (error) {
            var unknown = error && error.outcomeUnknown === true;
            results[target] = {
              operation: operation,
              anki_local_applied: { status: unknown ? "unknown" : "failed" },
              anki_web_sync: { status: "not-requested" },
              code: String(error && error.code || "BW_READER_ANKI_OPERATION"),
            };
            return persistLearningCardReceipt(repo, record.id, cardIndex, target, {
              status: unknown ? "unknown" : "failed",
              mutationId: externalMutation,
              noteIds: projections[target].noteIds,
              cardIds: projections[target].cardIds || [],
              updatedAt: Date.now(),
              error: String(error && error.message || error || "AnkiConnect 操作失败")
                .slice(0, 1000),
              detail: Object.assign({
                operation: operation,
                reader_applied: { status: "succeeded" },
              }, results[target]),
            }, mutationId + ":failed:" + targetIndex);
          });
        });
      });
    });
    return chain.then(function (latest) {
      return { record: latest, external_results: results };
    });
  }

  function projectLearningCardSourceMutation(repo, record, mutationId,
      contentCardIndex) {
    var results = {};
    // Freeze the semantic payload from the entity CAS that triggered this
    // projection.  Later repo.load calls are only allowed to contribute the
    // latest receipt/state revision; a concurrent edit must not leak newer
    // card faces or source into this mutationId's external side effect.
    var frozenCards = JSON.parse(JSON.stringify(record.cards));
    var frozenSource = JSON.parse(JSON.stringify(record.source));
    var chain = Promise.resolve(record);
    frozenCards.forEach(function (_, cardIndex) {
      chain = chain.then(function (current) {
        var currentState = current.states && current.states[String(cardIndex)];
        if (!currentState || currentState.removed === true) return current;
        return projectLearningCardMutation(
          repo,
          current,
          cardIndex,
          "edit",
          mutationId + ":source:" + cardIndex,
          frozenCards[cardIndex],
          frozenSource,
          cardIndex === contentCardIndex
            ? "content-and-provenance"
            : "provenance-only"
        ).then(function (projection) {
          Object.keys(projection.external_results).forEach(function (target) {
            results[cardIndex + ":" + target] =
              projection.external_results[target];
          });
          return projection.record;
        });
      });
    });
    return chain.then(function (latest) {
      return { record: latest, external_results: results };
    });
  }

  function nativeReaderLearningCardMutate(raw) {
    var value = raw && typeof raw === "object" ? raw : {};
    var operation = String(value.operation || "");
    var editHasCard = operation === "edit" &&
      Object.prototype.hasOwnProperty.call(value, "card");
    var editHasSource = operation === "edit" &&
      Object.prototype.hasOwnProperty.call(value, "source");
    var required = operation === "edit"
      ? ["operation", "mutationId", "id", "cardIndex", "expectedEntityRev",
        "externalPolicy"]
      : ["operation", "mutationId", "id", "cardIndex", "expectedStateRev",
        "externalPolicy"];
    exactObject(
      value,
      required,
      operation === "edit" ? ["card", "source"] : [],
      "Reader 学习卡修改"
    );
    if (operation !== "edit" && operation !== "delete") {
      return Promise.reject(directError(
        "Reader 学习卡操作无效", "BW_READER_LEARNING_CARD_PARAMS", false
      ));
    }
    var mutationId = safeText(value.mutationId, "mutationId", 160, false);
    if (!/^lcard_[a-f0-9]{24}$/.test(mutationId) ||
        (value.externalPolicy !== "reader-only" &&
          value.externalPolicy !== "sync-if-projected")) {
      return Promise.reject(directError(
        "Reader 学习卡 mutationId 或外部策略无效",
        "BW_READER_LEARNING_CARD_PARAMS",
        false
      ));
    }
    var id;
    var cardIndex;
    var repo;
    try {
      id = learningCardId(value.id);
      cardIndex = learningCardIndex(value.cardIndex);
      repo = learningCardRepository();
    } catch (error) {
      return Promise.reject(error);
    }
    return repo.load(id).then(function (before) {
      var selected = learningCardPublic(before, cardIndex);
      if (selected.state.removed === true) {
        if (operation === "edit") {
          throw directError(
            "Reader 学习卡已删除，不能再次编辑",
            "BW_READER_LEARNING_CARD_REMOVED",
            false
          );
        }
        if (!Number.isSafeInteger(value.expectedStateRev) ||
            value.expectedStateRev < 0) {
          throw directError(
            "Reader 学习卡删除参数无效",
            "BW_READER_LEARNING_CARD_PARAMS",
            false
          );
        }
        if (value.expectedStateRev !== selected.state_revision) {
          throw directError(
            "Reader 学习卡状态版本已变化，请重新读取",
            "BW_READER_LEARNING_CARD_CONFLICT",
            false
          );
        }
        // Exact replay of an already removed card is locally idempotent.  It
        // must not issue a second external Anki mutation or overwrite the
        // terminal/unknown projection receipt attached to the tombstone.
        var replayViewUpdate = requestLearningCardViewRefresh(
          before, cardIndex
        );
        return {
          contract: "reader-learning-card-mutation/1",
          operation: operation,
          reader_applied: { status: "succeeded", dedup: true },
          external_results: {},
          view_update: replayViewUpdate,
          record: selected,
        };
      }
      var local;
      var nextCard = selected.card;
      if (operation === "edit") {
        if (!Number.isSafeInteger(value.expectedEntityRev) ||
            value.expectedEntityRev < 0 ||
            (!editHasCard && !editHasSource) ||
            (editHasCard && (!plainObject(value.card) ||
              messageBytes(JSON.stringify(value.card)) > 200000)) ||
            typeof repo.replaceEntity !== "function") {
          throw directError(
            "Reader 学习卡编辑参数无效",
            "BW_READER_LEARNING_CARD_PARAMS",
            false
          );
        }
        var replacement = {};
        if (editHasCard) {
          var cards = before.cards.slice();
          cards[cardIndex] = value.card;
          replacement.cards = cards;
          nextCard = value.card;
        }
        if (editHasSource) {
          replacement.source = normalizeLearningCardSource(
            value.source,
            "Reader 学习卡 source"
          );
        }
        local = repo.replaceEntity(id, replacement, {
          ifEntityRev: value.expectedEntityRev,
          mutationId: mutationId + ":reader",
        });
      } else {
        if (!Number.isSafeInteger(value.expectedStateRev) ||
            value.expectedStateRev < 0 || typeof repo.removeCard !== "function") {
          throw directError(
            "Reader 学习卡删除参数无效或仓库尚未升级",
            "BW_READER_LEARNING_CARD_PARAMS",
            false
          );
        }
        local = repo.removeCard(id, cardIndex, {
          ifStateRev: value.expectedStateRev,
          mutationId: mutationId + ":reader",
        });
      }
      return Promise.resolve(local).then(function (applied) {
        // Canonical Reader storage is authoritative.  Refresh the owning App
        // immediately after that write, before AnkiConnect/media/sync work can
        // delay the visible Review card.  The Review surface itself checks the
        // stable id + cardIndex and only redraws the foreground card.
        var viewUpdate = requestLearningCardViewRefresh(applied, cardIndex);
        if (operation === "edit") nextCard = applied.cards[cardIndex];
        if (value.externalPolicy === "reader-only") {
          return {
            contract: "reader-learning-card-mutation/1",
            operation: operation,
            reader_applied: { status: "succeeded" },
            external_results: {},
            view_update: viewUpdate,
            record: learningCardPublic(applied, cardIndex),
          };
        }
        var projected = editHasSource
          ? projectLearningCardSourceMutation(
            repo,
            applied,
            mutationId,
            editHasCard ? cardIndex : null
          )
          : projectLearningCardMutation(
            repo,
            applied,
            cardIndex,
            operation,
            mutationId,
            nextCard,
            applied.source
          );
        return projected.then(function (projection) {
          return repo.load(id).then(function (latest) {
            return {
              contract: "reader-learning-card-mutation/1",
              operation: operation,
              reader_applied: { status: "succeeded" },
              external_results: projection.external_results,
              view_update: viewUpdate,
              record: learningCardPublic(latest, cardIndex),
            };
          });
        });
      });
    });
  }

  function requestLearningCardViewRefresh(record, cardIndex) {
    try {
      if (RC.review && typeof RC.review.refreshLearningCard === "function") {
        return RC.review.refreshLearningCard(record, cardIndex) || {
          status: "requested",
          rendered: false,
        };
      }
    } catch (_) {
      return { status: "failed", rendered: false };
    }
    return { status: "unavailable", rendered: false };
  }

  window._nativeReaderLearningCardMutate = nativeReaderLearningCardMutate;

  function projectReaderPcReviewRating(detail) {
    detail = detail && typeof detail === "object" ? detail : {};
    var record = detail.record;
    var cardIndex = Number(detail.cardIndex);
    var ease = Number(detail.ease);
    if (!record || !Number.isSafeInteger(cardIndex) || cardIndex < 0 ||
        !Number.isSafeInteger(ease) || ease < 1 || ease > 4 ||
        !record.states || !record.states[String(cardIndex)]) {
      return Promise.resolve(null);
    }
    var projections = record.states[String(cardIndex)].projections;
    var receipt = projections && projections.anki &&
      projections.anki.readerpc;
    var cardIds = receipt && Array.isArray(receipt.cardIds)
      ? receipt.cardIds.map(Number).filter(function (id) {
        return Number.isSafeInteger(id) && id > 0;
      }) : [];
    if (!receipt || (receipt.status !== "succeeded" &&
        receipt.status !== "failed") || cardIds.length !== 1) {
      return Promise.resolve(null);
    }
    var repo = learningCardRepository();
    var mutationId = randomId("lcard-review-rate");
    var noteIds = Array.isArray(receipt.noteIds) ? receipt.noteIds : [];
    return persistLearningCardReceipt(repo, record.id, cardIndex, "readerpc", {
      status: "pending",
      mutationId: mutationId,
      noteIds: noteIds,
      cardIds: cardIds,
      updatedAt: Date.now(),
      detail: {
        operation: "answer-cards",
        reader_applied: { status: "succeeded" },
        anki_local_applied: { status: "pending" },
        anki_web_sync: { status: "not-requested" },
      },
    }, mutationId + ":pending").then(function () {
      return operateLocalAnkiCard({
        operation: "answer-cards",
        mutationId: mutationId,
        answers: [{ cardId: cardIds[0], ease: ease }],
        syncMode: "background",
      });
    }).then(function (external) {
      var compact = compactAnkiOperationResult(external);
      return persistLearningCardReceipt(repo, record.id, cardIndex, "readerpc", {
        status: compact.anki_local_applied.status === "succeeded"
          ? "succeeded"
          : (compact.anki_local_applied.status === "unknown"
            ? "unknown" : "failed"),
        mutationId: mutationId,
        noteIds: noteIds,
        cardIds: cardIds,
        updatedAt: Date.now(),
        detail: Object.assign({
          operation: "answer-cards",
          reader_applied: { status: "succeeded" },
        }, compact),
      }, mutationId + ":final");
    }).catch(function (error) {
      var unknown = error && error.outcomeUnknown === true;
      return persistLearningCardReceipt(repo, record.id, cardIndex, "readerpc", {
        status: unknown ? "unknown" : "failed",
        mutationId: mutationId,
        noteIds: noteIds,
        cardIds: cardIds,
        updatedAt: Date.now(),
        error: String(error && error.message || error || "AnkiConnect 评分失败")
          .slice(0, 1000),
        detail: {
          operation: "answer-cards",
          reader_applied: { status: "succeeded" },
          anki_local_applied: { status: unknown ? "unknown" : "failed" },
          anki_web_sync: { status: "not-requested" },
        },
      }, mutationId + ":failed");
    });
  }

  window.addEventListener("rc:learning-card-rated", function (event) {
    try {
      Promise.resolve(projectReaderPcReviewRating(event && event.detail))
        .catch(function () {});
    } catch (_) {}
  });

  // Review-mode deletion has already committed the canonical Reader removal.
  // Project that exact card index to its recorded Anki notes and append the
  // external outcome to the removed state; never repeat the Reader mutation.
  window.addEventListener("rc:learning-card-removed", function (event) {
    var detail = event && event.detail && typeof event.detail === "object"
      ? event.detail : {};
    var record = detail.record;
    var cardIndex = Number(detail.cardIndex);
    if (!record || !Number.isSafeInteger(cardIndex) || cardIndex < 0 ||
        !record.states || !record.states[String(cardIndex)] ||
        record.states[String(cardIndex)].removed !== true) return;
    var repo;
    var mutationId;
    try {
      repo = learningCardRepository();
      mutationId = randomId("lcard-review-delete");
    } catch (_) { return; }
    projectLearningCardMutation(
      repo, record, cardIndex, "delete", mutationId, null
    ).then(function (result) {
      try {
        window.dispatchEvent(new CustomEvent(
          "rc:learning-card-removal-projected",
          { detail: result }
        ));
      } catch (_) {}
    }).catch(function (error) {
      try {
        window.dlog && window.dlog(
          "复习卡已从 Reader 删除；Anki 投影回执保存失败：" +
            String(error && error.message || error),
          "#ff9f0a"
        );
      } catch (_) {}
    });
  });

  function escapeLocalContextText(value) {
    return String(value || "")
      .replace(/\\/g, "\\\\")
      .replace(/⟦/g, "\\⟦")
      .replace(/⟧/g, "\\⟧");
  }

  function localContextMarkerAttribute(value, maximum) {
    return String(value || "")
      .replace(/\s+/g, " ")
      .trim()
      .slice(0, maximum)
      .replace(/"/g, "'")
      .replace(/[⟦⟧]/g, "");
  }

  function localCardMarker(item) {
    var card = item.card;
    var unbound = card.unbound === true;
    // page.context is model-facing reading context, not a renderer/source
    // transport.  The authoritative provider also exposes replacement JSON so
    // an explicit page-card read can perform a lossless rich-card edit, but
    // embedding that JSON here leaks vc-* controls, proxy URLs and layout
    // markup into every snapshot.  Use the already-normalized semantic body;
    // stable id + revision remain sufficient for direct delete and guarded
    // edit selection.
    var semanticBody = String(card.text || "").trim();
    if (!semanticBody) {
      semanticBody = "（这张卡片没有可读文字；需要完整源内容时请按 ID 读取）";
    }
    return '⟦CARD_START n="' + (unbound ? '' : String(item.number)) +
      '" id="' + localContextMarkerAttribute(card.id, 120) +
      '" revision="' + String(item.revision) +
      '" type="' + localContextMarkerAttribute(card.kind, 32) +
      '" label="' + localContextMarkerAttribute(card.label, 120) +
      // anchor=被锚定的词(2026-09-04 用户:「没有开始的标记」)。卡插在锚词**之后**,只靠位置看不出钉在哪个词上;
      // 带上词面,人读快照与模型改绑/删卡都有据可指。未锚定卡没有 anchor。
      (unbound ? '" unbound="true'
        : '" anchor="' + localContextMarkerAttribute(card.bind && card.bind.text, 120)) +
      '"⟧' + escapeLocalContextText(semanticBody) + '⟦CARD_END⟧';
  }

  function localLayoutCardMarker(item) {
    return localCardMarker(item)
      .replace(/\|/g, "\\|")
      .replace(/\r\n?|\n/g, "<br>");
  }

  function localLayoutBuilder(pageRecord) {
    return {
      text: "",
      after: new Array(pageRecord.chars.length),
      append: function (value) { this.text += String(value || ""); }
    };
  }

  function escapeLocalLayoutText(value) {
    return String(value || "")
      .replace(/\\/g, "\\\\")
      .replace(/\|/g, "\\|")
      .replace(/⟦/g, "\\⟦")
      .replace(/⟧/g, "\\⟧");
  }

  function appendLocalLayoutRegion(builder, pageRecord, region) {
    var wrote = false;
    // 跨行词重接（2026-09-02 结构化二期）:range 边界=视觉行边界,但
    // 跨行的同一个分词(fugashi 的 コチュジャ|ン、合并后的收藏词组)不能
    // 被 <br> 拆开;sp 空白盒两侧都是 CJK 时也不产空格(CJK 无词间空格,
    // 快照里"パンセ オ"就是它漏的)。lastReal=上一个已输出的实字符。
    var cjk = function (ch) {
      return /[\u3000-\u303f\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff\uff00-\uffef]/.test(ch || "");
    };
    var lastReal = null;
    region.ranges.forEach(function (range, rangeIndex) {
      var pending = [];
      var rangeWrote = false;
      for (var sourceIndex = range[0]; sourceIndex <= range[1]; sourceIndex += 1) {
        var source = pageRecord.chars[sourceIndex] || {};
        var value = String(source.c == null ? "" : source.c)
          .replace(/\u0000/g, "")
          .replace(/\r\n?/g, "\n")
          .replace(/\s+/g, " ");
        var semantic = value.trim();
        if (!semantic) {
          pending.push(sourceIndex);
          continue;
        }
        var cjkJoin = lastReal && cjk(semantic.charAt(0)) &&
          cjk(String(lastReal.c || "").slice(-1));
        if (pending.length && (wrote || rangeWrote) && !cjkJoin) builder.append(" ");
        pending.forEach(function (index) { builder.after[index] = builder.text.length; });
        pending = [];
        if (rangeIndex > 0 && !rangeWrote && wrote) {
          // <br> 只让位于**同一个分词**(w 相等):普通两行 CJK 对话之间
          // 的换行必须保留 —— cjkJoin 只管空白盒不产空格,不管换行。
          var sameWord = lastReal &&
            lastReal.w != null && lastReal.w >= 0 &&
            source.w === lastReal.w;
          if (!sameWord) builder.append("<br>");
        }
        builder.append(escapeLocalLayoutText(semantic));
        builder.after[sourceIndex] = builder.text.length;
        lastReal = source;
        rangeWrote = true;
        wrote = true;
      }
      pending.forEach(function (index) { builder.after[index] = builder.text.length; });
    });
    return wrote;
  }

  function localStructuredAnchorSegments(pageRecord, layout) {
    var segments = [];
    var overflow = false;
    var group = 0;
    var regions = layout.regions.slice().sort(function (left, right) {
      return left.order - right.order;
    });

    outer:
    for (var regionIndex = 0; regionIndex < regions.length; regionIndex += 1) {
      var region = regions[regionIndex];
      for (var rangeIndex = 0; rangeIndex < region.ranges.length;
          rangeIndex += 1) {
        group += 1;
        var range = region.ranges[rangeIndex];
        for (var sourceIndex = range[0]; sourceIndex <= range[1];
            sourceIndex += 1) {
          var source = pageRecord.chars[sourceIndex] || {};
          var semantic = String(source.c == null ? "" : source.c)
            .replace(/\u0000/g, "")
            .replace(/\r\n?/g, "\n")
            .replace(/\s+/g, " ")
            .trim();
          if (!semantic) continue;
          var rawWord = source.w;
          var word = rawWord != null && rawWord !== "" &&
            Number.isSafeInteger(Number(rawWord)) && Number(rawWord) >= 0
            ? Number(rawWord) : -1;
          var previous = segments.length ? segments[segments.length - 1] : null;
          if (previous && previous._group === group && word >= 0 &&
              previous._word === word &&
              previous.text.length + semantic.length <=
                LOCAL_STRUCTURED_ANCHOR_SEGMENT_TEXT_LIMIT) {
            previous.to = sourceIndex;
            previous.text += semantic;
            continue;
          }
          if (segments.length >= LOCAL_STRUCTURED_ANCHOR_MAP_MAX_SEGMENTS) {
            overflow = true;
            break outer;
          }
          segments.push({
            from: sourceIndex,
            to: sourceIndex,
            text: semantic,
            _word: word,
            _group: group
          });
        }
      }
    }
    return { segments: segments, overflow: overflow };
  }

  // 「整页宽行」判定(2026-09-03 用户实锤 52 页):对话行 + 表格的混合页被视觉层标成 manga,
  // 4 列网格把一行文字拆进两格、留下大片空格。真漫画页的分镜块窄而分散;这里若多数
  // 正文块的宽度 ≥ 页宽一半,就不是分镜,按阅读顺序输出正文行(同一视觉行的块用空格接上)。
  function mangaLayoutIsProse(layout) {
    var regions = (layout.regions || []).filter(function (region) {
      return region.kind !== "vision-supplement" && Array.isArray(region.bounds);
    });
    if (regions.length < 2) return false;
    var pageWidth = 0;
    regions.forEach(function (region) { pageWidth = Math.max(pageWidth, region.bounds[2]); });
    if (!pageWidth) return false;
    var wide = regions.filter(function (region) {
      return (region.bounds[2] - region.bounds[0]) >= pageWidth * 0.5;
    }).length;
    return wide / regions.length >= 0.4;
  }
  // `[NN]` 是助手唯一说得出口的块地址：卡片 bind.block 认的就是它
  // （ReaderCapabilities/cards.md、MCP schema 都这么写）。
  // ⚠ 2026-09-04：此前**只有分镜网格**那一条路印它，散文页与表格页一个都不印 ——
  //   而能力说明白纸黑字写着「正文每一行形如 [NN] 这一块的文字」。于是助手只能
  //   自己数行号，数出来的号跟任何一套编号都对不上，解析端当然定位不到
  //   （实锤：说第 11 块，而那页 bk 连号只有 8 块 → 退回全页 → 钉到页内第一处
  //   同样的字上）。CLAUDE.md 那条「面向 AI 的说明写反比没写更糟」的同一形态：
  //   助手不会去追问，它会照着编。
  // 例外:真数据表的单元格不印(见下面 appendLocalTableLayout 里的说明)。
  function appendLocalRegionLabel(builder, region) {
    if (!region || !Number.isSafeInteger(region.order)) return;
    builder.append("[" + String(region.order + 1).padStart(2, "0") + "] ");
  }
  function appendLocalProseLayout(builder, pageRecord, layout) {
    var regions = (layout.regions || []).slice().sort(function (left, right) {
      return left.order - right.order;
    });
    var previous = null;
    regions.forEach(function (region) {
      if (previous) {
        var sameLine = Array.isArray(previous.bounds) && Array.isArray(region.bounds) &&
          Math.min(previous.bounds[3], region.bounds[3]) -
            Math.max(previous.bounds[1], region.bounds[1]) > 0 &&
          region.bounds[0] >= previous.bounds[0];
        builder.append(sameLine ? " " : "\n");
      }
      appendLocalRegionLabel(builder, region);
      appendLocalLayoutRegion(builder, pageRecord, region);
      previous = region;
    });
    builder.append("\n");
  }
  function appendLocalMangaLayout(builder, pageRecord, layout) {
    if (mangaLayoutIsProse(layout)) {
      appendLocalProseLayout(builder, pageRecord, layout);
      return;
    }
    var cells = [];
    for (var row = 0; row < layout.gridRows; row += 1) {
      cells[row] = [[], [], [], []];
    }
    layout.regions.forEach(function (region) {
      cells[region.gridRow][region.gridColumn].push(region);
    });
    cells.forEach(function (row) {
      row.forEach(function (regions) {
        regions.sort(function (left, right) { return left.order - right.order; });
      });
    });
    builder.append("| 左 | 中左 | 中右 | 右 |\n| --- | --- | --- | --- |\n");
    cells.forEach(function (row) {
      builder.append("|");
      row.forEach(function (regions) {
        builder.append(" ");
        regions.forEach(function (region, index) {
          if (index) builder.append("<br>");
          appendLocalRegionLabel(builder, region);
          appendLocalLayoutRegion(builder, pageRecord, region);
        });
        builder.append(" |");
      });
      builder.append("\n");
    });
  }

  function appendLocalTableLayout(builder, pageRecord, layout) {
    function sharesVisualLine(left, right) {
      var overlap = Math.min(left.bounds[3], right.bounds[3]) -
        Math.max(left.bounds[1], right.bounds[1]);
      var shorterHeight = Math.min(
        left.bounds[3] - left.bounds[1],
        right.bounds[3] - right.bounds[1]
      );
      return overlap > 0 && shorterHeight > 0 && overlap >= shorterHeight / 2;
    }
    var blocks = [];
    layout.regions.filter(function (region) {
      return region.kind !== "table-cell";
    }).forEach(function (region) {
      blocks.push({ order: region.order, region: region, table: null });
    });
    layout.tables.forEach(function (table) {
      var regions = layout.regions.filter(function (region) {
        return region.kind === "table-cell" && region.tableId === table.id;
      });
      blocks.push({
        order: regions.reduce(function (minimum, region) {
          return Math.min(minimum, region.order);
        }, Number.MAX_SAFE_INTEGER),
        region: null,
        table: table,
        regions: regions
      });
    });
    blocks.sort(function (left, right) { return left.order - right.order; });
    blocks.forEach(function (block, blockIndex) {
      if (blockIndex) builder.append("\n");
      if (block.region) {
        appendLocalRegionLabel(builder, block.region);
        appendLocalLayoutRegion(builder, pageRecord, block.region);
        builder.append("\n");
        return;
      }
      var table = block.table;
      // 稀疏假表挡板（2026-09-01 实锤 45 页）：对话气泡间的分隔线被
      // 格线检测当成表格框 → 整页划成大网格，对话散落进格子、大量
      // 空单元格。真表格（46 页对照）填充率接近全满。容量 ≥8 且
      // 填充率 <40% 的"表"按普通文本流输出 —— 混合页的对话回到
      // Markdown 正文，真表格不受影响。
      var tableCapacity = table.rows * table.columns;
      if (tableCapacity >= 8 &&
          block.regions.length / tableCapacity < 0.4) {
        block.regions.slice().sort(function (left, right) {
          return left.order - right.order;
        }).forEach(function (region) {
          appendLocalRegionLabel(builder, region);
          appendLocalLayoutRegion(builder, pageRecord, region);
          builder.append("\n");
        });
        return;
      }
      var cells = [];
      for (var row = 0; row < table.rows; row += 1) {
        cells[row] = [];
        for (var column = 0; column < table.columns; column += 1) {
          cells[row][column] = [];
        }
      }
      block.regions.forEach(function (region) {
        cells[region.row][region.column].push(region);
      });
      cells.forEach(function (row) {
        row.forEach(function (regions) {
          regions.sort(function (left, right) { return left.order - right.order; });
        });
      });
      cells.forEach(function (row, rowIndex) {
        builder.append("|");
        row.forEach(function (regions) {
          builder.append(" ");
          regions.forEach(function (region, index) {
            if (index && !sharesVisualLine(regions[index - 1], region)) {
              builder.append("<br>");
            }
            // ⚠ 真数据表的单元格**不印 [NN]** —— 印了就成了 `| [01] 国家 | [02] 特征 |`,
            //   Markdown 数据表当场毁掉(助手与用户都要读它)。分镜网格那条路的"格子"
            //   是气泡不是数据,所以那边照印。表格里的内容要钉卡就只给 text:
            //   页内重复时由 _resolveRange 的区域流兜底,再不行就如实钉不上。
            appendLocalLayoutRegion(builder, pageRecord, region);
          });
          builder.append(" |");
        });
        builder.append("\n");
        if (rowIndex === 0) {
          builder.append("|" + new Array(table.columns).fill(" --- ").join("|") + "|\n");
        }
      });
    });
  }

  function localStructuredPageProjection(
    pageRecord, projected, revision, withAnchors
  ) {
    var layout = pageRecord.layout;
    if (!layout || layout.textSource !== "vision" ||
        layout.confidence !== "high" ||
        (layout.mode !== "manga" && layout.mode !== "table")) return null;
    var builder = localLayoutBuilder(pageRecord);
    if (layout.mode === "manga") {
      appendLocalMangaLayout(builder, pageRecord, layout);
    } else {
      appendLocalTableLayout(builder, pageRecord, layout);
    }
    var anchors = withAnchors === false
      ? { segments: [], overflow: false }
      : localStructuredAnchorSegments(pageRecord, layout);
    if (builder.after.some(function (offset) { return !Number.isSafeInteger(offset); })) {
      return null;
    }
    var inserts = Object.create(null);
    for (var index = 0; index < projected.length; index += 1) {
      var item = projected[index];
      var offset = builder.after[item.range.hi];
      if (!Number.isSafeInteger(offset)) return null;
      if (!inserts[offset]) inserts[offset] = [];
      inserts[offset].push(item);
    }
    var offsets = Object.keys(inserts).map(Number).sort(function (a, b) { return a - b; });
    var output = "";
    var cursor = 0;
    offsets.forEach(function (offset) {
      output += builder.text.slice(cursor, offset);
      inserts[offset].forEach(function (item) {
        output += localLayoutCardMarker(Object.assign({ revision: revision }, item));
      });
      cursor = offset;
    });
    output += builder.text.slice(cursor);
    return {
      text: output,
      mode: layout.mode,
      sourceAfter: builder.after,
      anchorSegments: anchors.segments,
      anchorOverflow: anchors.overflow
    };
  }

  function annotateLocalPageRange(pageRecord, projected, start, end, revision) {
    start = Math.max(0, Math.min(pageRecord.text.length, Number(start) || 0));
    end = Math.max(start, Math.min(pageRecord.text.length, Number(end) || 0));
    var inserts = Object.create(null);
    projected.forEach(function (item) {
      if (item.offset <= start || item.offset > end) return;
      if (!inserts[item.offset]) inserts[item.offset] = [];
      inserts[item.offset].push(item);
    });
    var offsets = Object.keys(inserts).map(Number).sort(function (a, b) { return a - b; });
    var output = "";
    var cursor = start;
    offsets.forEach(function (offset) {
      output += escapeLocalContextText(pageRecord.text.slice(cursor, offset));
      inserts[offset].forEach(function (item) {
        output += localCardMarker(Object.assign({ revision: revision }, item));
      });
      cursor = offset;
    });
    return output + escapeLocalContextText(pageRecord.text.slice(cursor, end));
  }

  // Truncate only at complete escaped units and complete CARD blocks.  The
  // character fence protects storage while the byte fence leaves room inside
  // the direct bridge's 256 KiB JSON frame. A consumer can never receive a
  // CARD_START without its matching CARD_END.
  function truncateLocalPageContext(value, maximum, maximumBytes) {
    value = String(value || "");
    var cut = Math.min(value.length, maximum);
    if (messageBytes(JSON.stringify(value.slice(0, cut))) > maximumBytes) {
      var low = 0;
      var high = cut;
      while (low < high) {
        var middle = low + Math.ceil((high - low) / 2);
        if (messageBytes(JSON.stringify(value.slice(0, middle))) <= maximumBytes) {
          low = middle;
        } else {
          high = middle - 1;
        }
      }
      cut = low;
    }
    if (cut >= value.length) return { text: value, truncated: false };
    if (cut > 0 && /[\uD800-\uDBFF]/.test(value.charAt(cut - 1)) &&
        /[\uDC00-\uDFFF]/.test(value.charAt(cut))) cut -= 1;
    var index = 0;
    var cardStartToken = "⟦CARD_START";
    while (index < cut) {
      if (value.charAt(index) === "\\") {
        if (index + 1 >= cut) { cut = index; break; }
        index += 2;
        continue;
      }
      if (value.slice(index, index + cardStartToken.length) === cardStartToken) {
        var headEnd = value.indexOf("⟧", index + cardStartToken.length);
        var cardEnd = headEnd < 0 ? -1 : value.indexOf("⟦CARD_END⟧", headEnd + 1);
        if (headEnd < 0 || cardEnd < 0) {
          cut = index;
          break;
        }
        var blockEnd = cardEnd + "⟦CARD_END⟧".length;
        if (blockEnd > cut) {
          cut = index;
          break;
        }
        index = blockEnd;
        continue;
      }
      index += 1;
    }
    return { text: value.slice(0, cut), truncated: true };
  }

  function buildLocalPageContext(current, runtime) {
    var page = Number(current.page) || 0;
    var visible = localAdapterVisibleText() || localDOMPageText(page, true);
    // 有选中内容时正文窗口收缩到**当前页**（用户 2026-08-31）：选中 =
    // 注意力已聚焦到具体位置，前后页只稀释语境；选中消失时恢复三页窗。
    // payload 变化经既有 signature 机制自动重发，无需额外触发。
    var hasSelection = current.selectionState === "active" ||
      (typeof current.selection === "string" && current.selection.trim());
    var previousPage = (!hasSelection && page > 1) ? page - 1 : 0;
    var nextPage = (!hasSelection && page) ? page + 1 : 0;
    return Promise.all([
      previousPage ? localPageRecord(previousPage, "")
        : Promise.resolve(emptyLocalPageRecord("")),
      localPageRecord(page, visible),
      nextPage ? localPageRecord(nextPage, "")
        : Promise.resolve(emptyLocalPageRecord("")),
      // document-notes page-chars are PDF page geometry (1-based). EPUB uses
      // section index 0 for its first section and has no compatible placement
      // projection, so keep its established plain-text context path untouched.
      current.kind === "pdf" && page >= 1
        ? localPageCardRecords(runtime, page)
        : Promise.resolve({ revision: null, cards: [] })
    ]).then(function (records) {
      var previous = records[0];
      var currentPage = records[1];
      var next = records[2];
      var pageCardProjection = buildLocalPageCardProjection(
        page, currentPage, records[3]
      );
      var projected = pageCardProjection.projected;
      var unboundMarkers = [];
      var unboundMarkerSize = 0;
      var unboundTruncated = false;
      pageCardProjection.unboundCards.forEach(function (item) {
        if (unboundTruncated) return;
        var marker = localCardMarker(Object.assign(
          { revision: pageCardProjection.value.revision }, item
        ));
        if (unboundMarkerSize + marker.length + 1 >
            Math.floor(LOCAL_PAGE_CONTEXT_LIMIT / 3)) {
          unboundTruncated = true;
          return;
        }
        unboundMarkerSize += marker.length + 1;
        unboundMarkers.push(marker);
      });
      var currentText = currentPage.text || visible;
      if (!visible) visible = currentText.slice(0, 5000);
      var exactIndex = visible ? currentText.indexOf(visible) : -1;
      var sections = [];
      var structuredAnchorMapInsertAt = -1;
      var structuredAnchorMapTruncated = false;
      var structured = localStructuredPageProjection(
        currentPage, projected, pageCardProjection.value.revision, true
      );
      if (structured) {
        var previousStructured = localStructuredPageProjection(
          previous, [], null, false
        );
        var nextStructured = localStructuredPageProjection(
          next, [], null, false
        );
        var previousText = previousStructured
          ? previousStructured.text : escapeLocalContextText(previous.text);
        var nextText = nextStructured
          ? nextStructured.text : escapeLocalContextText(next.text);
        if (previousText) {
          sections.push("【当前页之前】\n" + previousText.slice(-2200));
        }
        // 结构化投影在场时不再重复可见原文（2026-09-01 用户实锤：同一页
        // 内容写了两遍，且可见原文是交错烂序的劣质版本 —— 双份浪费
        // token 还给 AI 两个矛盾版本）。无结构化的普通页走别的分支照旧。
        sections.push(
          structured.mode === "manga"
            ? "【当前页结构化文字（Markdown；按 [NN] 编号顺序阅读；四列为空表示该位置没有文字；" +
                "Markdown 字符位置不可用作 bind 下标）】\n" +
                structured.text
            : "【当前页结构化文字（Markdown 表格；Markdown 字符位置不可用作 bind 下标）】\n" +
                structured.text
        );
        structuredAnchorMapInsertAt = sections.length;
        if (nextText) {
          sections.push("【当前页之后】\n" + nextText.slice(0, 2200));
        }
      } else if (exactIndex >= 0) {
        var beforeParts = [];
        if (previous.text) {
          beforeParts.push(escapeLocalContextText(previous.text.slice(-2200)));
        }
        var beforeStart = Math.max(0, exactIndex - 1800);
        var beforeCurrent = annotateLocalPageRange(
          currentPage, projected, beforeStart, exactIndex,
          pageCardProjection.value.revision
        );
        if (beforeCurrent) beforeParts.push(beforeCurrent);
        if (beforeParts.length) {
          sections.push("【当前显示区域之前】\n" + beforeParts.join("\n"));
        }
        if (visible) {
          sections.push("【当前显示区域（重点）】\n" + annotateLocalPageRange(
            currentPage, projected, exactIndex, exactIndex + visible.length,
            pageCardProjection.value.revision
          ));
        }
        var afterParts = [];
        var visibleEnd = exactIndex + visible.length;
        var afterCurrent = annotateLocalPageRange(
          currentPage, projected, visibleEnd,
          Math.min(currentText.length, visibleEnd + 1800),
          pageCardProjection.value.revision
        );
        if (afterCurrent) afterParts.push(afterCurrent);
        if (next.text) afterParts.push(escapeLocalContextText(next.text.slice(0, 2200)));
        if (afterParts.length) {
          sections.push("【当前显示区域之后】\n" + afterParts.join("\n"));
        }
      } else {
        if (previous.text) {
          sections.push("【当前显示区域之前】\n" +
            escapeLocalContextText(previous.text.slice(-2200)));
        }
        if (currentText) {
          sections.push("【当前页文字（视口范围暂不可精确定位）】\n" +
            annotateLocalPageRange(
              currentPage, projected, 0, currentText.length,
              pageCardProjection.value.revision
            ));
        }
        if (next.text) {
          sections.push("【当前显示区域之后】\n" +
            escapeLocalContextText(next.text.slice(0, 2200)));
        }
      }
      if (!structured && currentPage.layoutFallback) {
        sections.push(
          "【布局提示】布局信息置信度不足，已按 Vision 原顺序提供正文；" +
          "如需确认人物、气泡或空间关系，可按需调用 reader_visual_image。"
        );
      }
      if (unboundMarkers.length) {
        sections.push("【当前页未锚定卡片（不参与正文及右侧标记序号）】\n" +
          unboundMarkers.join("\n"));
      }
      if (structured && structuredAnchorMapInsertAt >= 0) {
        // ⚠ 这里原先内联一整张 ⟦ANCHOR_MAP_START⟧{…segments:[[from,to,text]×N]}⟦…END⟧
        //   机读锚点表，实测占页面正文的 **41%**（5684 / 13633 字符）。
        //
        //   它是为了省掉一次 reader_page_text 工具调用 —— 用 41% 的上下文换
        //   一次工具调用，这笔账不划算。而且它并没有消除「Markdown 位置」与
        //   「pageChars 下标」两个坐标系长得一样这个陷阱，只是给陷阱加了说明书：
        //   AI 面前仍然摆着两套编号，靠一句话约束它选对的那个，记错了照样
        //   静默锚错。用户 2026-08-23 判定这个做法本身是错的。
        //
        //   现在只留**指路**：序号唯一来源是 reader_page_text 的 segments。
        //   ⚠ 但那句「Markdown 字符位置不是锚点下标」的警告必须保留 ——
        //   去掉表容易，去掉警告就等于默许 AI 拿 Markdown 位置当下标。
        var noticeSections = sections.slice();
        noticeSections.splice(
          structuredAnchorMapInsertAt, 0,
          "【锚点下标从哪来】上面这份 Markdown 是**排版投影**，" +
          "它自身的字符位置不能当作 bind 的 from/to。" +
          "要把卡片钉到某段文字上时，调 reader_page_text 取该页的 segments，" +
          "从中挑区间；那些数值才是 pageChars 的闭区间。"
        );
        var noticeText = noticeSections.join("\n\n");
        if (noticeText.length <= LOCAL_PAGE_CONTEXT_LIMIT &&
            messageBytes(JSON.stringify(noticeText)) <=
              LOCAL_PAGE_CONTEXT_MAX_BYTES) {
          sections = noticeSections;
        }
        structuredAnchorMapTruncated = false;
      }
      var bounded = truncateLocalPageContext(
        sections.join("\n\n"), LOCAL_PAGE_CONTEXT_LIMIT,
        LOCAL_PAGE_CONTEXT_MAX_BYTES
      );
      var text = bounded.text;
      return {
        kind: current.kind,
        file: current.file,
        page: current.page,
        title: current.title || "",
        text: text,
        textAvailable: !!text.trim(),
        textSource: structured
          ? "app-local-structured-layout" : "app-local-visible-window",
        fallbackReason: text ? null : "本机文字层尚未提供当前页文字",
        truncated: bounded.truncated || currentPage.truncated ||
          previous.truncated || next.truncated || unboundTruncated ||
          structuredAnchorMapTruncated
      };
    });
  }

  function maybePublishLocalPageContext(state, pump, current) {
    var runtime = localNativePageRuntime();
    var now = Date.now();
    if (!runtime || (current.kind !== "pdf" && current.kind !== "epub") ||
        pump.pageContextInFlight ||
        now - pump.lastPageContextCheckAt < LOCAL_PAGE_CONTEXT_POLL_MS) return;
    pump.lastPageContextCheckAt = now;
    pump.pageContextInFlight = true;
    var generation = pump.pageContextGeneration;
    // 构建加超时(2026-09-03):正文构建若永不落定,in-flight 栅栏永远不放,此后每次翻页都
    // 没有正文上报,快照板一直 pending。超时按失败处理(不发布、进 dlog),但栅栏必须释放。
    var buildTimer = null;
    var buildWithTimeout = new Promise(function (resolve, reject) {
      buildTimer = setTimeout(function () {
        buildTimer = null;
        reject(new Error("本机正文构建超时(" + LOCAL_PAGE_CONTEXT_BUILD_TIMEOUT_MS + "ms)"));
      }, LOCAL_PAGE_CONTEXT_BUILD_TIMEOUT_MS);
      Promise.resolve(typeof runtime.ready === "function" ? runtime.ready() : null)
        .then(function () { return buildLocalPageContext(current, runtime); })
        .then(resolve, reject);
    });
    buildWithTimeout
      .then(function (payload) {
        if (buildTimer) { clearTimeout(buildTimer); buildTimer = null; }
        if (!activeReadingPumpAlive(state, pump) ||
            generation !== pump.pageContextGeneration) return null;
        var latest = localActiveReadingSnapshot();
        if (!latest || latest.file !== current.file ||
            !sameActiveScalar(latest.page, current.page)) return null;
        var signature = JSON.stringify(payload);
        // 签名去重带过期(2026-09-02):桥重启后稳定页丢失,而正文没变就永远不重发,
        // 快照板一直"无文字层"直到翻页。同一份正文超过 60s 允许重发,桥失忆最多空一分钟。
        // ⚠ sentAt 未知(旧泵对象/别处只写了签名)时按"刚发过"处理,不重发 —— 否则每个
        //   轮询 tick 都重发同一份(契约"迟到卡片读取不能覆盖"实锤 2 次发布)。
        if (signature === pump.lastPageContextSignature) {
          var sentAt = pump.lastPageContextSentAt;
          if (!sentAt || Date.now() - sentAt < LOCAL_PAGE_CONTEXT_RESEND_MS) return null;
        }
        return Promise.resolve(runtime.publishPageContext(payload)).then(function () {
          pump.lastPageContextSignature = signature;
          pump.lastPageContextSentAt = Date.now();
          pump.lastGoodPageContext = payload;
          pump.lastPageContextError = "";
        });
      }).catch(function (error) {
        if (buildTimer) { clearTimeout(buildTimer); buildTimer = null; }
        var message = String(error && error.message || error || "本机正文上报失败");
        var firstTime = message !== pump.lastPageContextError;
        if (firstTime) {
          pump.lastPageContextError = message;
          try { if (window.dlog) window.dlog("本机快照正文失败: " + message); } catch (_) {}
        }
        // 失败不发布(契约:a failed authoritative read keeps the last complete context ——
        // 桥保留上一份完整上下文比一份带原因的空正文更有用);原因进 dlog。
      }).finally(function () {
        pump.pageContextInFlight = false;
        if (generation !== pump.pageContextGeneration &&
            activeReadingPumpAlive(state, pump)) {
          pump.lastPageContextCheckAt = 0;
          var latest = localActiveReadingSnapshot();
          if (latest) maybePublishLocalPageContext(state, pump, latest);
        }
      });
  }

  function activeReadingPumpAlive(state, pump) {
    return !!(
      state &&
      pump &&
      state.activeReadingPump === pump &&
      !pump.stopped &&
      contextDeliveryMode === CONTEXT_DELIVERY_SNAPSHOT &&
      (
        state.nativeContext === true
          ? nativeContextState === state
          : state.contextOnly === true
          ? snapshotLink === state
          : active === state && state.started
      )
    );
  }

  function localNotesChanged(event) {
    var detail = event && event.detail;
    if (!detail || typeof detail !== "object" || Array.isArray(detail) ||
        detail.contract !== LOCAL_NOTES_CHANGED_CONTRACT ||
        typeof detail.file !== "string" || !detail.file ||
        !Number.isSafeInteger(Number(detail.revision)) ||
        Number(detail.revision) < 1 || typeof detail.source !== "string") return;
    var current = localActiveReadingSnapshot();
    if (!current || current.file !== detail.file) return;
    var states = localActiveReadingStates.slice();
    states.forEach(function (state) {
      var pump = state && state.activeReadingPump;
      if (!activeReadingPumpAlive(state, pump)) return;
      pump.pageContextGeneration += 1;
      pump.lastPageContextCheckAt = 0;
      // page.context has its own in-flight fence, so a notes commit must not
      // wait for an unrelated active-reading request. If a context build is
      // already running, its generation mismatch starts this pass in finally.
      if (!pump.pageContextInFlight) {
        maybePublishLocalPageContext(state, pump, current);
      }
    });
  }

  function stopActiveReadingPump(state) {
    var pump = state && state.activeReadingPump;
    if (!pump || pump.stopped) return;
    pump.stopped = true;
    var stateIndex = localActiveReadingStates.indexOf(state);
    if (stateIndex >= 0) localActiveReadingStates.splice(stateIndex, 1);
    if (pump.timer) {
      clearTimeout(pump.timer);
      pump.timer = null;
    }
    pump.inFlight = false;
  }

  function scheduleActiveReadingPump(state, delay) {
    var pump = state && state.activeReadingPump;
    if (!activeReadingPumpAlive(state, pump) || pump.timer) return;
    pump.timer = setTimeout(function () {
      pump.timer = null;
      runActiveReadingPump(state);
    }, delay);
  }

  function normalizeActiveReadingAck(value, state) {
    exactObject(
      value,
      ["sessionId", "revision", "outcome"],
      [],
      "ACTIVE-READING 响应"
    );
    if (
      safeId(value.sessionId, "sessionId") !== state.sessionId ||
      !Number.isSafeInteger(value.revision) ||
      value.revision < 1 ||
      (value.outcome !== "accepted" && value.outcome !== "duplicate")
    ) {
      throw contextSchemaError(
        "Windows ACTIVE-READING 回执无效",
        "BW_READER_ACTIVE_READING_ACK"
      );
    }
    return value;
  }

  function runActiveReadingPump(state) {
    var pump = state && state.activeReadingPump;
    if (!activeReadingPumpAlive(state, pump) || pump.inFlight) return;
    var current = localActiveReadingSnapshot();
    if (!current) {
      scheduleActiveReadingPump(state, ACTIVE_READING_POLL_MS);
      return;
    }
    observeReaderVisualPage(current);
    maybeRefreshLocalHighlightSource(state, pump, current);
    maybePublishLocalPageContext(state, pump, current);
    var signature = JSON.stringify(current);
    var now = Date.now();
    if (
      signature === pump.lastSignature &&
      now - pump.lastSentAt < ACTIVE_READING_HEARTBEAT_MS
    ) {
      scheduleActiveReadingPump(state, ACTIVE_READING_POLL_MS);
      return;
    }
    pump.inFlight = true;
    Promise.resolve().then(function () {
      if (!activeReadingPumpAlive(state, pump)) return null;
      var latest = localActiveReadingSnapshot();
      if (!latest || JSON.stringify(latest) !== signature) {
        return null;
      }
      var activeReading = Object.assign({}, current, {
        observedAtEpochMs: Date.now(),
      });
      return state.channel.request("active-reading", {
        sessionId: state.sessionId,
        activeContract: ACTIVE_READING_CONTRACT,
        active: activeReading,
      });
    }).then(function (value) {
      if (value === null) {
        pump.inFlight = false;
        scheduleActiveReadingPump(state, ACTIVE_READING_POLL_MS);
        return;
      }
      normalizeActiveReadingAck(value, state);
      if (!activeReadingPumpAlive(state, pump)) return;
      pump.lastSignature = signature;
      pump.lastSentAt = Date.now();
      pump.inFlight = false;
      scheduleActiveReadingPump(state, ACTIVE_READING_POLL_MS);
    }).catch(function (error) {
      pump.inFlight = false;
      if (!activeReadingPumpAlive(state, pump)) return;
      if (error && error.retryable === true) {
        scheduleActiveReadingPump(state, CONTEXT_RETRY_MS);
        return;
      }
      warnAndStopContextPump(state, state.contextPump, error);
    });
  }

  function startActiveReadingPump(state) {
    stopActiveReadingPump(state);
    state.activeReadingPump = {
      stopped: false,
      timer: null,
      inFlight: false,
      lastSignature: null,
      lastSentAt: 0,
      pageContextInFlight: false,
      pageContextGeneration: 0,
      lastPageContextCheckAt: 0,
      lastPageContextSignature: null,
      lastPageContextSentAt: 0,
      lastGoodPageContext: null,
      lastPageContextError: "",
      lastPageContextErrorSentAt: 0,
      highlightSourceInFlight: false,
      highlightSourceTargetKey: "",
      nextHighlightSourceCheckAt: 0,
      lastHighlightSourceError: "",
    };
    if (localActiveReadingStates.indexOf(state) < 0) {
      localActiveReadingStates.push(state);
    }
    runActiveReadingPump(state);
  }

  function stopNativeContextRelay() {
    var state = nativeContextState;
    nativeContextState = null;
    if (!state) return;
    state.stopped = true;
    stopContextPump(state);
    rejectNativeContextRequests(
      "原生 Reader 上下文连接已停止",
      "BW_NATIVE_COMPUTER_CONTEXT_STOPPED"
    );
  }

  function startNativeContextRelay(sessionId) {
    if (
      typeof sessionId !== "string" ||
      !sessionId ||
      sessionId.length > 160 ||
      !nativeContextHandlerAvailable()
    ) {
      return null;
    }
    if (
      nativeContextState &&
      !nativeContextState.stopped &&
      nativeContextState.sessionId === sessionId
    ) {
      return nativeContextState;
    }
    stopNativeContextRelay();
    var state = {
      channel: nativeContextChannel,
      sessionId: sessionId,
      nativeContext: true,
      contextOnly: false,
      stopped: false,
      contextPump: null,
      activeReadingPump: null,
    };
    nativeContextState = state;
    if (contextSyncEnabled()) {
      startContextPump(state);
      if (contextDeliveryMode === CONTEXT_DELIVERY_SNAPSHOT) {
        startActiveReadingPump(state);
        primeReaderVisualFromOutgoing();
      }
    }
    return state;
  }

  function nativeReaderUsesDedicatedContextLink() {
    // /reader-context/v1 is the snapshot-MCP transport. Legacy injection must
    // keep using the native voice WSS so typist receives the Reader payload.
    return (
      window.__BW_NATIVE_COMPUTER_VOICE__ === true &&
      contextDeliveryMode === CONTEXT_DELIVERY_SNAPSHOT
    );
  }

  function readerContextSurfaceVisible() {
    if (nativeReaderOwnsSnapshotLifecycle()) {
      // In the native App, Swift owns the actual scene lifecycle.  WKWebView's
      // document.visibilityState can become hidden while a native voice sheet
      // or overlay is presented even though the Reader scene is still active.
      // Treating that implementation detail as background used to close the
      // independent snapshot WSS a few seconds after voice started.  The
      // native foreground flag is the single authority for this path.
      return window.__BW_NATIVE_READER_FOREGROUND__ !== false;
    }
    return !document || document.visibilityState !== "hidden";
  }

  function reconcileNativeContextRelay() {
    // The App voice socket owns audio only. Reader context has its own
    // /reader-context/v1 connection and must stay alive whether this device,
    // another device, or no device currently owns the voice session.
    if (nativeReaderOwnsSnapshotLifecycle()) {
      nativeContextHandoffPending = false;
      stopNativeContextRelay();
      return reconcileSnapshotLink();
    }
    var value = nativeComputerVoiceState();
    if (
      value &&
      value.active === true &&
      typeof value.sessionId === "string" &&
      value.sessionId
    ) {
      nativeContextHandoffPending = false;
      var sessionId = value.sessionId;
      return stopSnapshotLink().then(function () {
        var latest = nativeComputerVoiceState();
        if (
          latest &&
          latest.active === true &&
          latest.sessionId === sessionId
        ) {
          startNativeContextRelay(sessionId);
        }
      });
    }
    stopNativeContextRelay();
    if (!value || value.busy !== true) {
      nativeContextHandoffPending = false;
      return reconcileSnapshotLink();
    }
    return Promise.resolve(null);
  }

  function prepareNativeContextHandoff() {
    nativeContextHandoffPending = true;
    stopNativeContextRelay();
    if (nativeReaderOwnsSnapshotLifecycle()) {
      return reconcileSnapshotLink().then(function () {
        return "native-ready";
      }).catch(function (error) {
        nativeContextHandoffPending = false;
        throw error;
      });
    }
    var modeReady = contextDeliveryMode
      ? Promise.resolve(contextDeliveryMode)
      : (
          RC.ctxSync && typeof RC.ctxSync.getConfig === "function"
            ? RC.ctxSync.getConfig().then(function (value) {
                if (
                  !value ||
                  (value.deliveryMode !== CONTEXT_DELIVERY_LEGACY &&
                    value.deliveryMode !== CONTEXT_DELIVERY_SNAPSHOT)
                ) {
                  throw directError(
                    "Reader 上下文交付模式无效",
                    "BW_READER_CONTEXT_DELIVERY_MODE_INVALID",
                    false
                  );
                }
                contextDeliveryMode = value.deliveryMode;
                return contextDeliveryMode;
              })
            : Promise.reject(directError(
                "Reader 上下文交付模式尚未就绪",
                "BW_READER_CONTEXT_DELIVERY_MODE_UNAVAILABLE",
                true
              ))
        );
    return modeReady.then(function () {
      if (nativeReaderUsesDedicatedContextLink()) {
        return reconcileSnapshotLink();
      }
      return stopSnapshotLink();
    }).then(function () {
      return "native-ready";
    }).catch(function (error) {
      nativeContextHandoffPending = false;
      reconcileSnapshotLink();
      throw error;
    });
  }

  function snapshotLinkWanted() {
    var independentOfVoice = nativeReaderOwnsSnapshotLifecycle();
    var modeAllowsSnapshot = independentOfVoice
      ? contextDeliveryMode !== CONTEXT_DELIVERY_LEGACY
      : contextDeliveryMode === CONTEXT_DELIVERY_SNAPSHOT;
    return !!(
      ownsReaderUi() &&
      modeAllowsSnapshot &&
      !contextModeChanging &&
      readerContextSurfaceVisible() &&
      (
        independentOfVoice ||
        (!active && !dialPending && !nativeComputerVoiceOwnsWss())
      )
    );
  }

  function stopSnapshotLink() {
    if (snapshotReconnectTimer) {
      clearTimeout(snapshotReconnectTimer);
      snapshotReconnectTimer = null;
    }
    var state = snapshotLink;
    snapshotLink = null;
    snapshotLinkGeneration += 1;
    snapshotTransportState = "idle";
    snapshotReconnectAttempt = 0;
    if (!state) return Promise.resolve();
    state.stopped = true;
    stopContextPump(state);
    var closePromise = state.channel
      ? state.channel.close()
      : Promise.resolve();
    return Promise.all([
      closePromise,
      Promise.resolve(state.promise).catch(function () {}),
    ]).then(function () {});
  }

  function claimSnapshotLinkForStart() {
    var state = snapshotLink;
    if (!state) return Promise.resolve(null);
    if (snapshotReconnectTimer) {
      clearTimeout(snapshotReconnectTimer);
      snapshotReconnectTimer = null;
    }
    return Promise.resolve(state.promise).catch(function () {
      return null;
    }).then(function (resolved) {
      // 电脑按钮的 START 走这条。判定不准的代价最大:借到死链路 → START 写进虚空 →
      // Windows 完全收不到,用户看到的就是"单击没反应、要按两次"。
      var usable = (
        resolved === state &&
        snapshotLink === state &&
        !state.stopped &&
        directChannelLive(state.channel) &&
        state.sessionBytes instanceof Uint8Array &&
        state.sessionBytes.length === 16 &&
        // A context-only connection cannot become a call: the bridge refuses
        // START on it (BW_COMPUTER_VOICE_DIRECT_PHASE_INVALID). Where the
        // snapshot link sits on the context endpoint -- inside the App, and on
        // an extension page -- the call must open its own socket instead of
        // borrowing this one.
        state.endpoint === DIRECT_ENDPOINT
      );
      if (!usable) {
        return snapshotLink === state
          ? stopSnapshotLink().then(function () { return null; })
          : null;
      }
      snapshotLink = null;
      snapshotLinkGeneration += 1;
      stopContextPump(state);
      state.stopped = true;
      var claimed = {
        channel: state.channel,
        sessionId: state.sessionId,
        sessionBytes: state.sessionBytes,
      };
      state.channel = null;
      return claimed;
    });
  }

  function clearSnapshotState(state) {
    if (
      !state ||
      !state.channel ||
      typeof state.sessionId !== "string"
    ) {
      return Promise.resolve(null);
    }
    invalidateReaderVisual(true);
    return state.channel.request("context-clear", {
      sessionId: state.sessionId,
    }).then(function (value) {
      return normalizeActiveReadingAck(value, state);
    });
  }

  function clearSnapshotLink() {
    var state = snapshotLink;
    if (!state) return stopSnapshotLink();
    if (state.clearPromise) return state.clearPromise;
    stopContextPump(state);
    state.clearPromise = clearSnapshotState(state).catch(function (error) {
      emitStatus({
        state: "warning",
        message: error && error.message ||
          "Windows 本地 Reader 快照未能立即清空",
        code: error && error.code ||
          "BW_READER_CONTEXT_SNAPSHOT_CLEAR_FAILED",
      });
      return null;
    }).then(function () {
      return stopSnapshotLink();
    }).finally(function () {
      state.clearPromise = null;
    });
    return state.clearPromise;
  }

  function clearActiveSnapshotState(state) {
    if (
      !state ||
      !state.started ||
      state.contextDeliveryMode !== CONTEXT_DELIVERY_SNAPSHOT
    ) {
      return Promise.resolve(null);
    }
    stopContextPump(state);
    return clearSnapshotState(state).catch(function (error) {
      emitStatus({
        state: "warning",
        sessionId: state.sessionId,
        message: error && error.message ||
          "Windows 本地 Reader 快照未能立即清空",
        code: error && error.code ||
          "BW_READER_CONTEXT_SNAPSHOT_CLEAR_FAILED",
      });
      return null;
    });
  }

  function scheduleSnapshotReconnect(delay) {
    if (snapshotReconnectTimer || !snapshotLinkWanted()) return;
    snapshotTransportState = "backoff";
    snapshotReconnectTimer = setTimeout(function () {
      snapshotReconnectTimer = null;
      reconcileSnapshotLink();
    }, delay);
    // Node 合同环境的 Timeout 会单独挂住进程;浏览器返回数字、无此方法。
    // 这只改变测试进程生命期,不改变 App 中的有界退避。
    if (snapshotReconnectTimer && typeof snapshotReconnectTimer.unref === "function") {
      snapshotReconnectTimer.unref();
    }
  }

  function nextSnapshotReconnectDelay() {
    var exponent = Math.min(snapshotReconnectAttempt, 4);
    var delay = Math.min(
      SNAPSHOT_RECONNECT_MS * Math.pow(2, exponent),
      SNAPSHOT_RECONNECT_MAX_MS
    );
    snapshotReconnectAttempt += 1;
    return delay;
  }

  function scheduleSnapshotReconnectAfterFailure() {
    if (snapshotReconnectTimer || !snapshotLinkWanted()) return;
    scheduleSnapshotReconnect(nextSnapshotReconnectDelay());
  }

  function closeChannelThenScheduleSnapshotReconnect(channel, delay) {
    var closing = Promise.resolve();
    if (channel) {
      try {
        closing = Promise.resolve(channel.close());
      } catch (_) {}
    }
    return closing.catch(function () {}).then(function () {
      // One page owns at most one of its own context links. Reconnect only
      // after that link has settled; Windows may simultaneously accept links
      // from other Apps/devices and applies their valid updates by arrival.
      scheduleSnapshotReconnect(delay);
    });
  }

  function closeChannelThenScheduleSnapshotReconnectAfterFailure(channel) {
    var closing = Promise.resolve();
    if (channel) {
      try {
        closing = Promise.resolve(channel.close());
      } catch (_) {}
    }
    return closing.catch(function () {}).then(function () {
      scheduleSnapshotReconnectAfterFailure();
    });
  }

  function proveSnapshotPublication(state) {
    if (
      !contextPumpAlive(state, state && state.contextPump) ||
      !activeReadingPumpAlive(state, state && state.activeReadingPump)
    ) {
      return Promise.reject(directError(
        "Windows 本地 Reader 快照发送泵需要重建",
        "BW_READER_CONTEXT_SNAPSHOT_PUMP_STALE",
        true
      ));
    }
    var current = localActiveReadingSnapshot();
    if (!current) return Promise.resolve(null);
    var signature = JSON.stringify(current);
    return state.channel.request("active-reading", {
      sessionId: state.sessionId,
      activeContract: ACTIVE_READING_CONTRACT,
      active: Object.assign({}, current, {
        observedAtEpochMs: Date.now(),
      }),
    }).then(function (value) {
      normalizeActiveReadingAck(value, state);
      if (snapshotLink !== state || state.stopped) return null;
      // This proves the exact context session can still update Windows' live
      // snapshot. CONTEXT-MODE alone only proves that the WSS parser answers;
      // it cannot distinguish a working publisher from an old cached view.
      var pump = state.activeReadingPump;
      if (pump && !pump.stopped) {
        pump.lastSignature = signature;
        pump.lastSentAt = Date.now();
      }
      return value;
    });
  }

  function resumeSnapshotLinkFromForeground(event) {
    if (
      nativeReaderOwnsSnapshotLifecycle() &&
      !snapshotLink &&
      event && event.detail && event.detail.probe === true
    ) {
      // ReaderPC 可以在 App 已经打开后启用快照。Swift 的有界前台巡检是重新
      // 询问服务器的时机;先丢弃上次 legacy 结论,但不打开生产 gate,直到新回执到达。
      contextDeliveryMode = null;
      applyReaderPCSnapshotAuthority(null);
    }
    if (!snapshotLinkWanted()) return reconcileSnapshotLink();
    // iOS may suspend this one-second timer while the PWA is backgrounded.
    // Foreground signals are an explicit wake-up: discard the suspended timer
    // and reconcile now. `reconcileSnapshotLink` itself fences an existing
    // open/in-progress link, so pageshow + online cannot create duplicates.
    if (snapshotReconnectTimer) {
      clearTimeout(snapshotReconnectTimer);
      snapshotReconnectTimer = null;
    }
    var state = snapshotLink;
    if (!state) return reconcileSnapshotLink();
    if (state.foregroundProbePromise) return state.foregroundProbePromise;
    // An OPEN readyState can survive an iOS background suspension after its
    // peer has gone away. CONTEXT-MODE proves the transport. The native 12 s
    // watchdog additionally refreshes active-reading and requires its exact
    // ACK, so a responsive parser backed only by old cache is not trusted.
    var provePublication = !!(
      event && event.detail && event.detail.probe === true
    );
    state.foregroundProbePromise = Promise.resolve(state.promise).then(
      function (resolved) {
        if (
          resolved !== state ||
          snapshotLink !== state ||
          state.stopped ||
          !directChannelLive(state.channel)
        ) {
          throw directError(
            "Windows 本地 Reader 快照连接需要重建",
            "BW_READER_CONTEXT_SNAPSHOT_STALE",
            true
          );
        }
        return queryContextMode(state.channel);
      }
    ).then(function (mode) {
      if (snapshotLink !== state || state.stopped) return null;
      if (mode !== CONTEXT_DELIVERY_SNAPSHOT) {
        return stopSnapshotLink();
      }
      if (provePublication) {
        return proveSnapshotPublication(state).then(function () {
          return state;
        });
      }
      return state;
    }).then(function (proven) {
      if (!proven || snapshotLink !== state || state.stopped) return proven;
      snapshotTransportState = "open";
      snapshotReconnectAttempt = 0;
      return state;
    }).catch(function (error) {
      if (snapshotLink !== state || state.stopped) return null;
      snapshotLink = null;
      state.stopped = true;
      snapshotTransportState = "failed";
      applyReaderPCSnapshotAuthority(null);
      stopContextPump(state);
      var failedChannel = state.channel;
      state.channel = null;
      emitStatus({
        state: "warning",
        message: error && error.message ||
          "Windows 本地 Reader 快照前台验活失败",
        code: error && error.code ||
          "BW_READER_CONTEXT_SNAPSHOT_STALE",
      });
      return closeChannelThenScheduleSnapshotReconnectAfterFailure(
        failedChannel
      ).then(function () { return null; });
    }).finally(function () {
      state.foregroundProbePromise = null;
    });
    return state.foregroundProbePromise;
  }

  function reconcileSnapshotLink() {
    if (!snapshotLinkWanted()) {
      return stopSnapshotLink();
    }
    if (snapshotLink) {
      if (snapshotLink.clearPromise) {
        return snapshotLink.clearPromise.then(function () {
          return reconcileSnapshotLink();
        });
      }
      return Promise.resolve(snapshotLink);
    }
    var generation = ++snapshotLinkGeneration;
    var session = randomSession();
    var snapshotEndpoint = (
      window.__BW_NATIVE_COMPUTER_VOICE__ === true ||
      isOwnExtensionPage(window.chrome && window.chrome.runtime)
    ) ? CONTEXT_ENDPOINT : DIRECT_ENDPOINT;
    var state = {
      channel: null,
      endpoint: snapshotEndpoint,
      sessionId: session.id,
      sessionBytes: session.bytes,
      contextOnly: true,
      stopped: false,
      contextPump: null,
      activeReadingPump: null,
      generation: generation,
      foregroundProbePromise: null,
    };
    snapshotTransportState = "connecting";
    snapshotLink = state;
    var work = openDirect({
      // Inside the App, Swift owns the voice link end to end. This snapshot
      // connection bypassed the gesture guard and opened on the voice endpoint
      // anyway, and the bridge admits one owner -- so every switch back to the
      // reader evicted the native call. On the context endpoint it cannot: that
      // one is shared and refuses START/STOP.
      //
      // Elsewhere the flag is absent and this stays on the voice endpoint,
      // which matters for the web Reader on the Pi: it has no native voice and
      // still has to dial for itself.
      // The snapshot link only ever carries context, so it belongs on the
      // context endpoint wherever a separate owner holds the audio -- inside the
      // App, where Swift holds it, and on an extension page, where this same
      // module is about to dial for it.
      //
      // Sharing the voice endpoint put both on one socket: the snapshot link
      // connected first and moved it into the context-only phase, and the call
      // then tried to START on a connection no longer accepting it
      // (BW_COMPUTER_VOICE_DIRECT_PHASE_INVALID). Two purposes, two sockets.
      //
      // The Reader on the Pi keeps the voice endpoint: there is no second owner
      // there, and the snapshot link is the connection it later dials on.
      endpoint: snapshotEndpoint,
      onFatal: function (error) {
        if (snapshotLink !== state || state.stopped) return;
        state.stopped = true;
        snapshotLink = null;
        snapshotTransportState = "failed";
        applyReaderPCSnapshotAuthority(null);
        stopContextPump(state);
        emitStatus({
          state: "warning",
          message: error && error.message ||
            "Windows 本地 Reader 快照连接已断开",
          code: error && error.code ||
            "BW_READER_CONTEXT_SNAPSHOT_DISCONNECTED",
        });
        scheduleSnapshotReconnectAfterFailure();
      },
    }, function (channel) {
      state.channel = channel;
    }).then(function (channel) {
      if (
        state.stopped ||
        snapshotLink !== state ||
        generation !== snapshotLinkGeneration
      ) {
        channel.close();
        return null;
      }
      state.channel = channel;
      return queryContextMode(channel).then(function (mode) {
        return mode === CONTEXT_DELIVERY_LEGACY
          ? clearSnapshotState(state)
              .then(function () { return mode; })
          : mode;
      });
    }).then(function (mode) {
      if (mode === null) return null;
      if (mode === CONTEXT_DELIVERY_LEGACY) {
        state.stopped = true;
        if (snapshotLink === state) snapshotLink = null;
        return state.channel.close().then(function () { return null; });
      }
      return state.channel.request("context-open", {
        sessionId: state.sessionId,
      }).then(function (value) {
        exactObject(
          value,
          ["sessionId", "state", "mode"],
          [],
          "CONTEXT-OPEN 响应"
        );
        if (
          value.sessionId !== state.sessionId ||
          value.state !== "context-only" ||
          value.mode !== CONTEXT_DELIVERY_SNAPSHOT
        ) {
          throw contextSchemaError(
            "Windows CONTEXT-OPEN 回执无效",
            "BW_READER_CONTEXT_OPEN_ACK"
          );
        }
        if (snapshotLink !== state || state.stopped) return null;
        var sourceInstanceId = currentReaderSourceInstanceId();
        state.channel.readerVisualSessionId = state.sessionId;
        // Attach() can synchronously wake the durable-output replay before the
        // visual-register result is sent back.  Publish the source identity
        // before sending the registration request so that the first replayed
        // event can be correlated instead of being rejected as stale.  A
        // failed registration closes and discards this channel below.
        state.channel.readerVisualSourceId = sourceInstanceId;
        return state.channel.request("visual-register", {
          sessionId: state.sessionId,
          sourceInstanceId: sourceInstanceId,
        }).then(function (registration) {
          exactObject(
            registration,
            ["sessionId", "sourceInstanceId", "state"],
            [],
            "VISUAL-REGISTER 响应"
          );
          if (
            registration.sessionId !== state.sessionId ||
            registration.sourceInstanceId !== sourceInstanceId ||
            registration.state !== "registered"
          ) {
            throw contextSchemaError(
              "Windows VISUAL-REGISTER 回执无效",
              "BW_READER_VISUAL_REGISTER_ACK"
            );
          }
          if (snapshotLink !== state || state.stopped) return null;
          snapshotTransportState = "open";
          snapshotReconnectAttempt = 0;
          startContextPump(state);
          startActiveReadingPump(state);
          emitStatus({
            state: "context-ready",
            message: "Windows 本地 Reader 快照已连接",
          });
          return state;
        });
      });
    }).catch(function (error) {
      if (snapshotLink === state) snapshotLink = null;
      state.stopped = true;
      snapshotTransportState = "failed";
      applyReaderPCSnapshotAuthority(null);
      stopContextPump(state);
      var failedChannel = state.channel;
      state.channel = null;
      return closeChannelThenScheduleSnapshotReconnectAfterFailure(
        failedChannel
      ).then(function () { return null; });
    });
    state.promise = work;
    return work;
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
        state.heartbeatFailures = 0;
        scheduleHeartbeat(state);
      }).catch(function (error) {
        state.heartbeatInFlight = false;
        if (active !== state || state.stopped) return;
        // ⚠ 这里原来是**一次失败就整条拆掉**，而单次请求超时是 7 秒
        //   （REQUEST_TIMEOUT_MS），也就是说客户端比**服务端还严**：
        //   桥的合同允许 15 秒收不到心跳才断
        //   （DirectBridgeContract.ClientHeartbeatTimeoutMilliseconds = 15_000）。
        //   一次 >7s 的 RTT 抖动（切后台、Tailscale 换路径、Wi-Fi→蜂窝）
        //   就足以拆掉整通电话，而服务端此刻还愿意再等 8 秒。
        //   改成连续 N 次：坏连接最多晚一个心跳间隔被发现。
        state.heartbeatFailures = (state.heartbeatFailures || 0) + 1;
        if (state.heartbeatFailures < HEARTBEAT_FAILURES_BEFORE_GIVING_UP) {
          // 留一声再重试 —— 不出声的话「抖了一下自愈了」和「一直很稳」
          // 在外部一模一样，下次报"经常断"就没有数据可查。
          try {
            console.warn(
              "[direct] 心跳超时，重试中",
              state.heartbeatFailures,
              "/",
              HEARTBEAT_FAILURES_BEFORE_GIVING_UP,
              (error && error.message) || error
            );
          } catch (_) {}
          scheduleHeartbeat(state);
          return;
        }
        failActive(state, error, true);
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
    var failedChannel = state.channel;
    state.channel = null;
    releaseSurface(state.surface);
    if (emit !== false) {
      emitStatus({
        state: "failed",
        sessionId: state.sessionId || undefined,
        message: error && error.message || "Windows 桥接器启动失败",
        code: error && error.code || "BW_COMPUTER_VOICE_DIRECT_START_FAILED",
      });
    }
    closeChannelThenScheduleSnapshotReconnect(
      failedChannel,
      SNAPSHOT_RECONNECT_MS
    );
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

  function activeDirectOptions(state) {
    return {
      onStatus: function (status) {
        if (active !== state || state.stopped) return;
        emitStatus({
          state: status.state,
          sessionId: state.sessionId,
          message: statusMessage(status.state, status.reason),
        });
      },
      onFatal: function (error) {
        if (active === state && !state.stopped) {
          failActive(state, error, true);
        }
      },
      onBinary: function (buffer) {
        handlePcmFrame(state, buffer);
      },
    };
  }

  function startStateCurrent(state) {
    return !!(
      state &&
      !state.stopped &&
      !state.cancelled &&
      active === state
    );
  }

  function preStartChannelErrorRecoverable(error) {
    var code = error && error.code;
    return (
      code === "BW_COMPUTER_VOICE_DIRECT_TIMEOUT" ||
      code === "BW_COMPUTER_VOICE_DIRECT_DISCONNECTED" ||
      code === "BW_COMPUTER_VOICE_DIRECT_OFFLINE"
    );
  }

  function cancelledStartError() {
    return directError(
      "Windows 桥接启动已取消",
      "BW_COMPUTER_VOICE_DIRECT_CANCELLED",
      false
    );
  }

  function queryStartChannel(state, channel) {
    return queryContextMode(channel).then(function (mode) {
      return { channel: channel, mode: mode };
    }, function (error) {
      // A context-only socket can still look OPEN after iOS resumes even
      // though its TCP peer disappeared. CONTEXT-MODE is read-only and runs
      // before START, so this is the one safe point to discard that claimed
      // socket and retry a fresh connection exactly once. START itself is
      // never retried because its result may be unknown.
      if (
        state.claimedSnapshot !== true ||
        state.retriedAfterStaleClaim ||
        !startStateCurrent(state) ||
        !preStartChannelErrorRecoverable(error)
      ) {
        throw error;
      }
      state.retriedAfterStaleClaim = true;
      state.claimedSnapshot = false;
      if (state.channel === channel) state.channel = null;
      return channel.close().then(function () {
        if (!startStateCurrent(state)) throw cancelledStartError();
        return openDirect(null, function (fresh) {
          state.channel = fresh;
        });
      }).then(function (fresh) {
        if (!startStateCurrent(state)) {
          fresh.close();
          throw cancelledStartError();
        }
        return queryContextMode(fresh).then(function (mode) {
          return { channel: fresh, mode: mode };
        });
      });
    });
  }

  // A content script cannot place this call, so it hands it to a page that can.
  //
  // Dialling from an ordinary web page reaches Windows only by way of the
  // background worker, and on iOS that worker is reclaimed -- the connection
  // then times out with nothing to show for it. An extension page of our own
  // connects directly and has been observed to work. So the button opens that
  // page instead of trying here.
  //
  // Only in a content script: the condition is "the extension APIs exist, but
  // this document is not one of ours". The Reader on the Pi has no chrome
  // runtime and is unaffected; an extension page satisfies isOwnExtensionPage
  // and dials normally.
  function delegateToExtensionPage(options) {
    var runtime = window.chrome && window.chrome.runtime;
    if (!runtime || typeof runtime.getURL !== "function") return null;
    if (isOwnExtensionPage(runtime)) return null;
    var url;
    try {
      url = runtime.getURL("call.html");
    } catch (_) {
      return null;
    }
    if (!url) return null;
    var appKind = options && options.appKind;
    if (appKind) url += "?app=" + encodeURIComponent(String(appKind));
    try {
      // A fixed name so a second press reuses the same tab rather than stacking
      // another one on top of a call already in progress.
      var opened = window.open(url, "bw-computer-voice");
      if (opened) return Promise.resolve({ ok: true, delegated: true });
    } catch (_) {}
    try {
      // A blocked window would otherwise leave the press with no visible effect
      // at all, which is indistinguishable from the button being broken.
      window.location.href = url;
      return Promise.resolve({ ok: true, delegated: true });
    } catch (_) {}
    return Promise.reject(directError(
      "无法打开扩展通话页",
      "BW_COMPUTER_VOICE_DELEGATE_FAILED",
      false
    ));
  }

  function startFromUserGesture(options) {
    options = options || {};
    var delegated = delegateToExtensionPage(options);
    if (delegated) return delegated;
    if (contextModeChanging) {
      return Promise.reject(directError(
        "Reader 正在切换上下文模式，请稍后再拨号",
        "BW_READER_CONTEXT_DELIVERY_MODE_BUSY",
        true
      ));
    }
    if (active) {
      return Promise.reject(directError(
        "电脑客户端通话已经启动",
        "BW_COMPUTER_VOICE_ALREADY_ACTIVE",
        false
      ));
    }
    if (!ownsReaderUi()) {
      return Promise.reject(directError(
        "当前 Reader 界面由另一运行时接管",
        "BW_COMPUTER_VOICE_UI_NOT_OWNER",
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
      // 每条新连接从 0 开始 —— 不重置的话上一条连接的失败计数会带进新连接，
      // 表现是刚连上就被判死。
      heartbeatFailures: 0,
      uplinkActive: false,
      uplinkSequence: 0,
      uplinkTimestampBase: Math.floor(Date.now() * 1000),
      contextPump: null,
      activeReadingPump: null,
      contextDeliveryMode: null,
      appKind: normalizeComputerTarget(options.appKind || getComputerTarget()),
      claimedSnapshot: false,
      retriedAfterStaleClaim: false,
      pcm: {
        1: { nextSequence: 0, seen: false, timestampLow: 0, timestampHigh: 0 },
      },
    };
    surface.ownerState = state;
    active = state;
    var availabilityClosed = cancelAvailabilityForStart().then(function () {
      return claimSnapshotLinkForStart();
    }).then(function (claimed) {
      if (!claimed) return null;
      if (state.stopped || state.cancelled || active !== state) {
        return claimed.channel.close().then(function () {
          throw directError(
            "Windows 桥接启动已取消",
            "BW_COMPUTER_VOICE_DIRECT_CANCELLED",
            false
          );
        });
      }
      state.sessionId = claimed.sessionId;
      state.sessionBytes = claimed.sessionBytes;
      state.claimedSnapshot = true;
      // Keep fatal handling detached until a read-only CONTEXT-MODE probe has
      // proved this possibly stale, context-only socket. The promise chain
      // below owns all pre-START failures and may safely reconnect once.
      state.channel = claimed.channel.setOptions({});
      return state.channel;
    });
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
      if (state.channel && !state.channel.closed) {
        return state.channel;
      }
      return openDirect(null, function (channel) {
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
      return queryStartChannel(state, channel).then(function (prepared) {
        state.channel = prepared.channel.setOptions(activeDirectOptions(state));
        state.contextDeliveryMode = prepared.mode;
        if (
          prepared.mode === CONTEXT_DELIVERY_LEGACY &&
          !contextSyncEnabled()
        ) return null;
        // ReaderPC owns snapshot enable/disable. Starting voice may preserve
        // the legacy injector preference, but must never turn snapshot mode on.
        if (prepared.mode === CONTEXT_DELIVERY_SNAPSHOT) return null;
        return setServerContextDeliveryMode(
          prepared.mode,
          contextSyncEnabled()
        );
      }).then(function () {
        return state.channel;
      });
    }).then(function (channel) {
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
        appKind: state.appKind,
        // Declares that a person just pressed the button, and means it: the
        // bridge will stop an existing owner to hand the call over here.
        //
        // Safe to state unconditionally at this point, and only at this point.
        // This request is reachable from nowhere else -- startFromUserGesture
        // has no internal callers, reconnects run on the snapshot link, and
        // that link is context-only and never sends START. Automatic recovery
        // must never take the call away from whoever is holding it; only a
        // deliberate press may.
        takeover: true,
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
      if (
        state.contextDeliveryMode === CONTEXT_DELIVERY_LEGACY ||
        state.contextDeliveryMode === CONTEXT_DELIVERY_SNAPSHOT ||
        contextSyncEnabled()
      ) {
        startContextPump(state);
        if (
          state.contextDeliveryMode === CONTEXT_DELIVERY_SNAPSHOT
        ) {
          startActiveReadingPump(state);
          primeReaderVisualFromOutgoing();
        }
      }
      if (state.surface.context.state !== "running") {
        state.audioBlocked = true;
        emitStatus({
          state: "audio-blocked",
          sessionId: state.sessionId,
          message: "浏览器阻止了声音；请再次点击电脑按钮允许播放",
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
      // Cleared here as well, because failActive returns early when the state
      // is absent or already marked -- and then nothing clears it. The button
      // stays in its waiting colour and refuses further presses, so a single
      // failed start leaves the user with no way to try again. Whatever went
      // wrong, the button has to come back.
      dialPending = false;
      throw error;
    });
  }

  function stop(reason) {
    var state = active;
    active = null;
    dialPending = false;
    if (!state) {
      emitStatus({ state: "stopped", message: "电脑客户端已停止" });
      scheduleSnapshotReconnect(0);
      return Promise.resolve({ ok: true, state: "stopped" });
    }
    state.cancelled = true;
    state.stopped = true;
    state.acceptPcm = false;
    state.uplinkActive = false;
    stopContextPump(state);
    // 本地音频与路由立即释放；Windows STOP 回执只负责远端收尾。
    // 这样从电脑按钮切到普通电话时，两套 AudioContext 不会因慢 ACK 短暂重叠。
    releaseSurface(state.surface);
    clearHeartbeat(state);
    if (!state.started) {
      // START 尚未确认时不能把 STOP 排在同一条 WSS 后面等待：Windows
      // 服务端正在串行处理 START，排队 STOP 只会让应用、采音和快捷键继续
      // 启动。立即关闭连接会取消所有在途请求，并让远端会话租约 fail closed。
      var startingChannel = state.channel;
      state.channel = null;
      emitStatus({
        state: "stopped",
        message: "电脑桥接启动已取消；若 Codex Voice 已亮起，请在 Windows 退出",
      });
      closeChannelThenScheduleSnapshotReconnect(
        startingChannel,
        SNAPSHOT_RECONNECT_MS
      );
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
      var stoppedChannel = state.channel;
      state.channel = null;
      emitStatus({
        state: "stopped",
        message: "电脑桥接已停止；请确认 Windows 的 Codex Voice 已退出",
      });
      closeChannelThenScheduleSnapshotReconnect(
        stoppedChannel,
        SNAPSHOT_RECONNECT_MS
      );
      return { ok: true, state: "stopped" };
    });
  }

  function computerButtonFromEvent(event) {
    var target = event && event.target;
    while (target) {
      if (
        target.id === "asst-computer" ||
        target.id === "vc-top-computer"
      ) {
        return registeredComputerButtons.has(target) ? target : null;
      }
      if (
        (target.id === "asst-call" || target.id === "vc-top-call") &&
        registeredLegacyPhoneButtons.has(target) &&
        (
          (selectedEngineKnown && computerVoiceSelected) ||
          (!selectedEngineKnown && selectedEngineMutationRevision === null)
        )
      ) {
        return target;
      }
      target = target.parentNode;
    }
    return null;
  }

  function registerComputerButton(button) {
    if (
      !button ||
      button.nodeType !== 1 ||
      String(button.tagName || "").toUpperCase() !== "BUTTON" ||
      (
        button.id !== "asst-computer" &&
        button.id !== "vc-top-computer"
      ) ||
      button.type !== "button" ||
      button.ownerDocument !== window.document ||
      button.isConnected !== true
    ) {
      return false;
    }
    registeredComputerButtons.add(button);
    return true;
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
    registeredLegacyPhoneButtons.add(button);
    return true;
  }

  function installGestureCapture() {
    if (!document || typeof document.addEventListener !== "function") return;
    document.addEventListener("click", function (event) {
      if (!computerButtonFromEvent(event)) return;
      if (event.isTrusted !== true) return;
      // In BWReader App, Swift owns microphone, playback and the voice WSS.
      // Keep this page component context-only so one tap cannot start a second
      // browser media/voice implementation beside the native bridge.
      if (window.__BW_NATIVE_COMPUTER_VOICE__ === true) return;
      if (contextModeChanging) return;
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
      if (!active) {
        // The dedicated computer button is the sole trusted approval surface.
        // Prepare only the reversible local media lease during its trusted
        // gesture; the later bubble handler still decides whether to START.
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
    // Compatibility-only bookkeeping for older hosts. The ordinary phone
    // engine no longer grants or revokes computer-bridge authority.
    selectedEngineKnown = false;
    computerVoiceSelected = false;
    selectedEngineMutationRevision = revision;
    return revision;
  }

  function isSelectedEngineRevisionCurrent(revision) {
    return Number.isSafeInteger(revision) &&
      revision >= 1 &&
      selectedEngineMutationRevision === null &&
      revision === selectedEngineAcceptedRevision;
  }

  function setSelectedEngine(engine, revision) {
    if (revision == null) {
      revision = reserveSelectedEngineUpdate();
    }
    if (!Number.isSafeInteger(revision) || revision < 1) {
      return computerVoiceSelected;
    }
    var completesMutation = selectedEngineMutationRevision !== null &&
      revision === selectedEngineMutationRevision;
    if (selectedEngineMutationRevision !== null && !completesMutation) {
      // A polling/dial GET may reserve a newer ordinary revision while a
      // settings POST is in flight, but it only reflects the pre-mutation
      // server value. It cannot clear or supersede the mutation fence.
      return computerVoiceSelected;
    }
    if (!completesMutation && revision < selectedEngineRevision) {
      return computerVoiceSelected;
    }
    if (completesMutation) {
      selectedEngineMutationRevision = null;
      // Invalidate every ordinary GET reserved while the mutation was in
      // flight, including one whose response has not arrived yet.
      selectedEngineRevision += 1;
    } else {
      selectedEngineRevision = revision;
    }
    selectedEngineAcceptedRevision = revision;
    selectedEngineKnown = true;
    computerVoiceSelected = engine === "computer_client";
    if (!dialPending) reconcileSnapshotLink();
    return computerVoiceSelected;
  }

  function setDialPending(value) {
    dialPending = value === true;
    if (dialPending) {
      // Keep an already-open context-only WSS available for
      // claimSnapshotLinkForStart(). Tearing it down here races the Windows
      // server's single-connection gate: the fresh call socket can receive
      // HTTP 409 before the old connection's finally block releases ownership.
      // Prevent only a scheduled/new background connection while dialing.
      if (snapshotReconnectTimer) {
        clearTimeout(snapshotReconnectTimer);
        snapshotReconnectTimer = null;
      }
      return true;
    }
    if (!dialPending && !active && !computerVoiceSelected) {
      clearPreparedSurface(true);
    }
    reconcileSnapshotLink();
    return dialPending;
  }

  function cancelPreparedGesture() {
    clearPreparedSurface(true);
  }

  function contextSyncChanged() {
    if (nativeReaderOwnsSnapshotLifecycle()) {
      stopNativeContextRelay();
      return reconcileSnapshotLink();
    }
    if (contextDeliveryMode === CONTEXT_DELIVERY_SNAPSHOT) {
      if (
        active &&
        active.started &&
        active.contextDeliveryMode === CONTEXT_DELIVERY_SNAPSHOT
      ) {
        startContextPump(active);
        startActiveReadingPump(active);
        primeReaderVisualFromOutgoing();
        return Promise.resolve(active);
      }
      return reconcileSnapshotLink();
    }
    if (nativeContextState && !nativeContextState.stopped) {
      if (contextSyncEnabled()) {
        startContextPump(nativeContextState);
        if (contextDeliveryMode === CONTEXT_DELIVERY_SNAPSHOT) {
          startActiveReadingPump(nativeContextState);
          primeReaderVisualFromOutgoing();
        }
      } else {
        stopContextPump(nativeContextState);
      }
      return Promise.resolve(nativeContextState);
    }
    if (!contextSyncEnabled()) return clearSnapshotLink();
    return reconcileSnapshotLink();
  }

  function bootstrapNativeSnapshotLink() {
    if (nativeReaderOwnsSnapshotLifecycle()) {
      // App 不读取、不确认、不改写旧 context-sync 偏好。先连 ReaderPC,
      // 再以 CONTEXT-MODE 作为唯一权威;这也允许页面先开、服务后开。
      contextDeliveryMode = null;
      applyReaderPCSnapshotAuthority(null);
      return reconcileSnapshotLink().catch(function (error) {
        emitStatus({
          state: "warning",
          message: error && error.message || "ReaderPC 实时快照初始化失败",
          code: error && error.code || "BW_READER_CONTEXT_BOOTSTRAP_FAILED",
        });
        return null;
      });
    }
    if (!RC.ctxSync || typeof RC.ctxSync.getConfig !== "function") {
      emitStatus({
        state: "warning",
        message: "Reader 上下文配置尚未就绪",
        code: "BW_READER_CONTEXT_BOOTSTRAP_UNAVAILABLE",
      });
      return Promise.resolve(null);
    }
    // ReaderPC owns the snapshot lifecycle. Bootstrap only the delivery mode;
    // the old eph-ctx-sync preference gates legacy injection, never the
    // dedicated /reader-context/v1 link. This path does not request a
    // microphone and foreground fencing stays in snapshotLinkWanted().
    return RC.ctxSync.getConfig().then(function (value) {
      if (
        !plainObject(value) ||
        typeof value.enabled !== "boolean" ||
        (value.deliveryMode !== CONTEXT_DELIVERY_LEGACY &&
          value.deliveryMode !== CONTEXT_DELIVERY_SNAPSHOT)
      ) {
        throw contextSchemaError(
          "原生 Reader 上下文配置回执无效",
          "BW_READER_CONTEXT_BOOTSTRAP_SCHEMA"
        );
      }
      contextDeliveryMode = value.deliveryMode;
      if (
        plainObject(value.pi_compatibility) &&
        value.pi_compatibility.confirmed === false
      ) {
        emitStatus({
          state: "warning",
          message: "实时快照使用 Windows 直连；Pi 兼容状态尚未确认",
          code: value.pi_compatibility.code ||
            "BW_READER_CONTEXT_PI_COMPATIBILITY_UNCONFIRMED",
        });
      }
      return window.__BW_NATIVE_COMPUTER_VOICE__ === true
        ? reconcileNativeContextRelay()
        : reconcileSnapshotLink();
    }).catch(function (error) {
      emitStatus({
        state: "warning",
        message: error && error.message || "Reader 实时快照初始化失败",
        code: error && error.code || "BW_READER_CONTEXT_BOOTSTRAP_FAILED",
      });
      return null;
    });
  }

  function abortForPageExit() {
    clearPreparedSurface(true);
    // Page lifecycle events may freeze script execution before CONTEXT-CLEAR
    // receives an ACK. Release the WSS slot immediately; freshness rules keep
    // any last snapshot from being mistaken for current content.
    stopSnapshotLink();
    stopNativeContextRelay();
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

  function readerUiOwnerChanged() {
    if (ownsReaderUi()) {
      reconcileSnapshotLink();
      return;
    }
    clearPreparedSurface(true);
    if (active) {
      stop("ui-owner-changed");
      return;
    }
    stopSnapshotLink();
  }

  function mountSettings(container) {
    if (!container) return;
    var existingRoot = container.querySelector(".rc-computer-voice-settings");
    if (existingRoot) {
      if (typeof existingRoot.__rcComputerVoiceRefresh === "function") {
        existingRoot.__rcComputerVoiceRefresh();
      }
      return;
    }
    var root = document.createElement("div");
    root.className = "rc-computer-voice-settings";
    root.innerHTML =
      '<label class="ams-tdef" for="rc-computer-target">语音与文字接力目标</label>' +
      '<select id="rc-computer-target" data-role="target" ' +
      'style="width:100%;margin:6px 0 7px;background:#0d1322;' +
      'border:1px solid #2a3550;color:#e6e6f0;border-radius:6px;' +
      'padding:8px 10px;font-size:13px">' +
      '<option value="codex-desktop">Codex</option>' +
      '<option value="chatgpt-classic">GPT Classic</option>' +
      '</select>' +
      '<div class="ams-tdef" data-role="target-detail" ' +
      'style="margin-bottom:9px"></div>' +
      '<div class="ams-tdef" data-role="status">正在读取 Windows 直连状态…</div>' +
      '<div class="ams-row" style="margin-top:7px">' +
      '<button type="button" class="ams-btn" data-role="refresh">刷新直连状态</button>' +
      '</div>' +
      '<div class="ams-tdef" data-role="codex-voice-status" ' +
      'style="margin-top:10px">正在读取 Codex 语音状态…</div>' +
      '<div class="ams-tdef" data-role="detail" style="margin-top:6px"></div>' +
      '<div class="ams-tdef" data-role="error" ' +
      'style="display:none;margin-top:7px;white-space:pre-wrap;' +
      'user-select:text;color:#ffb4a8"></div>';
    container.appendChild(root);
    var targetSelect = root.querySelector('[data-role="target"]');
    var targetDetail = root.querySelector('[data-role="target-detail"]');
    var status = root.querySelector('[data-role="status"]');
    var refreshButton = root.querySelector('[data-role="refresh"]');
    var codexVoiceStatus = root.querySelector(
      '[data-role="codex-voice-status"]'
    );
    var detail = root.querySelector('[data-role="detail"]');
    var errorDetail = root.querySelector('[data-role="error"]');
    var refreshRetryTimer = null;

    function renderTarget() {
      var target = getComputerTarget();
      targetSelect.value = target;
      targetSelect.disabled = computerTargetBusy();
      targetDetail.textContent = target === COMPUTER_TARGET_CLASSIC
        ? "GPT Classic：语音启停与音频回传以 GPT Classic 为目标；" +
          "文字接力仅在“测试旧版文字注入”开启时发送到 GPT Classic，" +
          "实时快照/MCP 仍属于 Codex。"
        : "Codex：电脑按钮只建立音频桥接；实时快照与 Codex 语音由 " +
          "ReaderPC 服务器进程统一管理；音频回传和已有上下文工具/文字接力" +
          "仍以 Codex 为目标。";
    }

    function renderCodexVoice(value, connectionState) {
      if (bridgeVoiceEnabledKnown && !bridgeVoiceEnabled) {
        codexVoiceStatus.textContent =
          "○ ReaderPC 语音功能已关闭；其它 Reader 功能保持在线。";
      } else if (value && value.status === "available") {
        codexVoiceStatus.textContent = value.active
          ? "● Codex 语音正在运行（由 ReaderPC 服务器管理）。"
          : "○ Codex 语音当前未运行（由 ReaderPC 服务器管理）。";
      } else if (value && value.status === "error") {
        codexVoiceStatus.textContent = "○ Codex 语音状态读取失败。";
      } else if (value && value.status === "unavailable") {
        codexVoiceStatus.textContent =
          "○ Codex 语音状态不可读取（Codex 可能尚未运行）。";
      } else {
        codexVoiceStatus.textContent = connectionState === "offline"
          ? "○ Windows 桥接器暂时离线。"
          : "○ 暂未取得 Codex 语音状态。";
      }
    }

    function render(value) {
      if (
        value.state === "ready" ||
        (value.state === "idle" && value.status && value.status.ready === true)
      ) {
        status.textContent =
          "● Windows 桥接器已就绪；只有点击电脑按钮才会启动。";
      } else if (value.state === "offline") {
        status.textContent = "○ Windows 桥接器离线或电脑正在睡眠。";
      } else {
        status.textContent = "○ Windows 桥接器：" +
          (statusReasonMessage(value.reason) || value.state || "未就绪");
      }
      detail.textContent =
        "固定 Tailnet 直连，无需配对或填写地址；" +
        "桥接器不会创建或安装虚拟设备，A/B 必须是 Windows 已有的两根独立虚拟音频线；" +
        "电脑按钮才会申请当前网页麦克风并送入 Windows 虚拟麦克风；" +
        "所选目标的输出固定到独立虚拟扬声器后按进程树回传。" +
        "切换目标或刷新状态不会启动应用或采音。" +
        "通话中不能切换目标；挂断只停止音频桥接。" +
        "实时快照、视觉读取、浏览器控制与卡片工具由 ReaderPC 非语音服务常驻提供；" +
        "Codex 语音、F24 保活与音频路由由独立语音开关控制。";
      renderCodexVoice(
        value.status && value.status.codexVoice,
        value.state
      );
      if (value.state === "offline" && !refreshRetryTimer) {
        refreshRetryTimer = setTimeout(function () {
          refreshRetryTimer = null;
          if (root.isConnected) refresh();
        }, 1800);
      } else if (value.state !== "offline" && refreshRetryTimer) {
        clearTimeout(refreshRetryTimer);
        refreshRetryTimer = null;
      }
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
      refreshButton.disabled = true;
      codexVoiceStatus.textContent = "正在读取 Codex 语音状态…";
      return availability().then(render).catch(function (error) {
        status.textContent = "直连状态读取失败：" +
          (error.message || "未知错误");
        renderCodexVoice(null);
        detail.textContent = "未发送 START。";
      }).finally(function () {
        refreshButton.disabled = false;
      });
    }

    root.__rcComputerVoiceRefresh = refresh;
    targetSelect.addEventListener("change", function () {
      var previous = getComputerTarget();
      targetSelect.disabled = true;
      setComputerTarget(targetSelect.value).then(function () {
        renderTarget();
        if (RC && typeof RC.toast === "function") {
          RC.toast("电脑客户端目标已保存，下次连接生效");
        }
      }).catch(function (error) {
        targetSelect.value = previous;
        renderTarget();
        if (RC && typeof RC.toast === "function") {
          RC.toast(error.message || "电脑客户端目标切换失败");
        }
      });
    });
    window.addEventListener("bw-native-computer-voice-state", renderTarget);
    refreshButton.addEventListener("click", refresh);
    renderTarget();
    loadComputerTarget().then(renderTarget).catch(renderTarget);
    refresh();
  }

  // Preference-only read: no application launch, microphone access or START.
  // Begin during module load so the computer button already has the saved
  // target even when the settings pane has not been opened this session.
  loadComputerTarget().catch(function () {});
  installGestureCapture();
  if (window && typeof window.addEventListener === "function") {
    window.addEventListener("pagehide", abortForPageExit);
    window.addEventListener("pageshow", resumeSnapshotLinkFromForeground);
    window.addEventListener("pageshow", reconcileNativeContextRelay);
    window.addEventListener("online", resumeSnapshotLinkFromForeground);
    window.addEventListener(
      "bw-native-computer-voice-state",
      reconcileNativeContextRelay
    );
    window.addEventListener(
      "bw-native-reader-foreground",
      resumeSnapshotLinkFromForeground
    );
    window.addEventListener(LOCAL_NOTES_CHANGED_EVENT, localNotesChanged);
  }
  if (document && typeof document.addEventListener === "function") {
    document.addEventListener(
      "bw:book-ui-owner-changed",
      readerUiOwnerChanged
    );
    document.addEventListener(
      "visibilitychange",
      resumeSnapshotLinkFromForeground
    );
  }

  // Push the current web page to Windows over the live call.
  //
  // Page text has no other way in. active-reading carries position and
  // selection only, and page.context is assembled by Windows from the Pi --
  // which has never seen the page a browser extension is looking at. This
  // action closes that gap: Windows writes the payload straight into its
  // snapshot with textSource=extension-page.
  //
  // Only meaningful from an extension page. A Reader page's text already
  // reaches Windows through the Pi, and calling this there would merely
  // describe the same document twice.
  // Sequence must be a positive integer and rise; the bridge treats a repeat or
  // a gap as a journal fault and stops forwarding context.
  var webContextSeq = 0;
  function nextWebContextSeq() {
    webContextSeq += 1;
    return webContextSeq;
  }

  // Exactly 16 lowercase hex. The fixture writes "<12hex>" as a placeholder,
  // which is what misled the first attempt; 0.1.71's ValidateEvent wants 16 and
  // rejects anything else.
  function webContextEventId() {
    var hex = "0123456789abcdef";
    var out = "";
    for (var i = 0; i < 16; i += 1) {
      out += hex[Math.floor(Math.random() * 16)];
    }
    return out;
  }

  function sendWebPageContext(page) {
    if (!active || !active.channel) {
      return Promise.reject(directError(
        "电脑客户端通话未建立,无法发送网页上下文",
        "BW_COMPUTER_VOICE_WEB_CONTEXT_INACTIVE",
        true
      ));
    }
    page = page || {};
    var url = typeof page.url === "string" ? page.url : "";
    if (!/^https?:\/\//i.test(url)) {
      return Promise.reject(directError(
        "网页上下文 URL 必须是 http/https",
        "BW_COMPUTER_VOICE_WEB_CONTEXT_URL",
        false
      ));
    }
    var text = typeof page.text === "string" ? page.text : "";
    // Truncation is reported rather than hidden, so the assistant knows it is
    // holding part of a page instead of mistaking it for the whole.
    var truncated = text.length > 12000 || page.truncated === true;
    text = text.slice(0, 12000);

    // Sent on the existing context action rather than a dedicated one. The
    // bridge runs 0.1.71, which has no web-page-context; carrying the page as a
    // page.context event needs nothing new on the Windows side and keeps that
    // known-good voice baseline intact.
    //
    // page must be 0, matching the pos the active-reading snapshot reports.
    // Windows pairs the two by (file, page); a mismatch leaves the context
    // pending forever, which is exactly how this looked before -- snapshot
    // showing active-reading only, page null, text unavailable.
    return active.channel.request("context", {
      sessionId: active.sessionId,
      contextContract: OUTGOING_CONTEXT_CONTRACT,
      event: {
        v: 1,
        seq: nextWebContextSeq(),
        type: "page.context",
        event: "page.context",
        ts: Math.floor(Date.now() / 1000),
        id: webContextEventId(),
        stable: true,
        book_id: url,
        file: url,
        page: 0,
        title: typeof page.title === "string" ? page.title : "",
        kind: "web",
        text_available: !!text,
        page_context: {
          text: text,
          text_available: !!text,
          text_source: "extension-page",
          fallback_reason: text ? null : "扩展未取得正文",
          truncated: truncated,
          reason: "call",
          visual: null,
          embeds: { highlights: 0, blocks: 0, unanchored: [] },
        },
      },
    });
  }

  function setReaderPCServiceMode(mode) {
    // App 设置面板遥控 ReaderPC 模式:C# 写意图文件,ReaderPC 的收敛循环(≤5s)
    // 停旧代际按新模式重启——所以回执是 pending-restart,数秒后 context-mode
    // 重查会带回新 serviceMode(事件广播,图标随之切)。
    if (mode !== "full" && mode !== "bridge-only") {
      return Promise.reject(new Error("BW_READERPC_SERVICE_MODE_INVALID"));
    }
    var state = snapshotLink;
    var channel = state && state.channel;
    if (!channel) {
      return Promise.reject(new Error("BW_READERPC_SERVICE_MODE_LINK_OFFLINE"));
    }
    var previousPending = bridgePendingServiceMode;
    beginReaderPCPending("service", mode);
    return channel.request("service-mode-set", { mode: mode }).then(function (value) {
      exactObject(value, ["serviceMode", "applied"], [], "SERVICE-MODE-SET 响应");
      if (value.serviceMode !== "full" && value.serviceMode !== "bridge-only") {
        throw directError(
          "Windows 服务模式无效",
          "BW_READERPC_SERVICE_MODE_INVALID",
          false
        );
      }
      return value;
    }).catch(function (error) {
      if (bridgePendingServiceMode === mode) {
        clearReaderPCPendingTimer("service");
        bridgePendingServiceMode = null;
        if (previousPending !== null) {
          beginReaderPCPending("service", previousPending);
        } else {
          emitReaderPCServiceIntent();
        }
      }
      throw error;
    });
  }

  function setReaderPCVoiceEnabled(enabled) {
    if (typeof enabled !== "boolean") {
      return Promise.reject(new Error("BW_READERPC_VOICE_ENABLED_INVALID"));
    }
    if (!bridgeVoiceEnabledKnown) {
      return Promise.reject(new Error("BW_READERPC_VOICE_ENABLED_UNSUPPORTED"));
    }
    var state = snapshotLink;
    var channel = state && state.channel;
    if (!channel) {
      return Promise.reject(new Error("BW_READERPC_SERVICE_MODE_LINK_OFFLINE"));
    }
    var previousPending = bridgePendingVoiceEnabled;
    beginReaderPCPending("voice", enabled);
    return channel.request("service-mode-set", {
      voiceEnabled: enabled,
    }).then(function (value) {
      exactObject(
        value,
        ["voiceEnabled", "applied"],
        [],
        "SERVICE-MODE-SET 语音响应"
      );
      if (value.voiceEnabled !== enabled) {
        throw directError(
          "Windows 语音功能切换回执无效",
          "BW_READERPC_VOICE_ENABLED_INVALID",
          false
        );
      }
      return value;
    }).catch(function (error) {
      if (bridgePendingVoiceEnabled === enabled) {
        clearReaderPCPendingTimer("voice");
        bridgePendingVoiceEnabled = null;
        if (previousPending !== null) {
          beginReaderPCPending("voice", previousPending);
        } else {
          emitReaderPCServiceIntent();
        }
      }
      throw error;
    });
  }

  RC.computerVoice = Object.freeze({
    contract: BRIDGE_CONTRACT,
    getServiceMode: function () { return bridgeServiceMode; },
    getPendingServiceMode: function () {
      return bridgePendingServiceMode;
    },
    setServiceMode: setReaderPCServiceMode,
    getVoiceEnabled: function () {
      return bridgeVoiceEnabledKnown
        ? bridgeVoiceEnabled
        : null;
    },
    getPendingVoiceEnabled: function () {
      return bridgePendingVoiceEnabled;
    },
    setVoiceEnabled: setReaderPCVoiceEnabled,
    sendWebPageContext: sendWebPageContext,
    pageCards: pageCards,
    directContract: DIRECT_CONTRACT,
    availability: availability,
    reserveSelectedEngineUpdate: reserveSelectedEngineUpdate,
    beginSelectedEngineUpdate: beginSelectedEngineUpdate,
    isSelectedEngineRevisionCurrent: isSelectedEngineRevisionCurrent,
    setSelectedEngine: setSelectedEngine,
    setDialPending: setDialPending,
    contextSyncChanged: contextSyncChanged,
    prepareNativeContextHandoff: prepareNativeContextHandoff,
    setContextDeliveryMode: setContextDeliveryMode,
    getTargetApp: getComputerTarget,
    loadTargetApp: loadComputerTarget,
    setTargetApp: setComputerTarget,
    // 便宜的"这台电脑现在连着吗":只读常驻快照链路的 socket 状态,不发请求、不开新连接。
    // 词典兜底要用它决定「等」还是「立刻说未命中」——availability() 会开连接发 STATUS,
    // 拿来做这种前置判断反而把要省的开销花掉了。
    isLinked: function () {
      var state = snapshotLink;
      return !!(state && !state.stopped && directChannelLive(state.channel));
    },
    lookupJapaneseFallback: lookupJapaneseFallback,
    pushReplicationCommands: pushReplicationCommands,
    addLocalAnkiCard: addLocalAnkiCard,
    operateLocalAnkiCard: operateLocalAnkiCard,
    cancelPreparedGesture: cancelPreparedGesture,
    registerComputerButton: registerComputerButton,
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
  // Snapshot delivery is a Reader capability, not a side effect of pressing
  // the computer-voice button. Start its bounded bootstrap immediately.
  bootstrapNativeSnapshotLink();
})();
