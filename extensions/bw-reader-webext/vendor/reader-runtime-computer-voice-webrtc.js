/*
 * reader-runtime/computer-voice-webrtc.js
 *
 * Pure, dependency-injected WebRTC controller for the optional Windows
 * computer-voice bridge.  This module owns negotiation state only:
 *
 *   - Windows sends exactly two local audio tracks, in the fixed semantic
 *     order `app-output`, then `user-mic`.
 *   - Reader receives exactly those two tracks and binds their SDP mids to
 *     the same fixed semantic identities.
 *   - The Pi transport carries only bounded offer/answer/ICE/bye metadata.
 *
 * It deliberately contains no fetch/WebSocket, getUserMedia, Native
 * Messaging, DOM or extension API calls.  Constructing a real peer connection
 * is also deferred until an explicit, one-shot gated `start()` call.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  root.BWComputerVoiceWebRtc = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  var CONTRACT = "reader-computer-voice-webrtc/1";
  var SIGNAL_CONTRACT = "reader-computer-voice-signal/1";
  var ROLE_WINDOWS_SENDER = "windows-sender";
  var ROLE_READER_RECEIVER = "reader-receiver";
  var TRACK_APP_OUTPUT = "app-output";
  var TRACK_USER_MIC = "user-mic";
  var TRACK_IDS = Object.freeze([TRACK_APP_OUTPUT, TRACK_USER_MIC]);
  var SIGNAL_KINDS = Object.freeze({
    offer: true,
    answer: true,
    ice: true,
    bye: true,
  });
  var MAX_SIGNAL_BATCH = 32;
  var MAX_PENDING_SIGNALS = 64;
  var MAX_SEEN_SIGNALS = 256;
  var MAX_PENDING_ICE = 64;
  var MAX_SDP_BYTES = 32 * 1024;
  var DEFAULT_POLL_INTERVAL_MS = 500;
  var DEFAULT_NEGOTIATION_TIMEOUT_MS = 20 * 1000;
  var DEFAULT_MAX_POLLS = 48;

  function WebRtcControllerError(message, code, retryable, details) {
    this.name = "ComputerVoiceWebRtcError";
    this.message = String(message || "电脑客户端 WebRTC 状态错误");
    this.code = String(code || "BW_COMPUTER_VOICE_WEBRTC_INVALID");
    this.retryable = !!retryable;
    this.details = details || null;
    if (Error.captureStackTrace) {
      Error.captureStackTrace(this, WebRtcControllerError);
    }
  }
  WebRtcControllerError.prototype = Object.create(Error.prototype);
  WebRtcControllerError.prototype.constructor = WebRtcControllerError;

  function fail(message, code, retryable, details) {
    throw new WebRtcControllerError(message, code, retryable, details);
  }

  function isPlainObject(value) {
    if (!value || Object.prototype.toString.call(value) !== "[object Object]") {
      return false;
    }
    var prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
  }

  function exactKeys(value, keys, label) {
    if (!isPlainObject(value)) {
      fail(label + " 必须是普通对象");
    }
    var actual = Object.keys(value).sort();
    var expected = keys.slice().sort();
    if (
      actual.length !== expected.length ||
      actual.some(function (key, index) { return key !== expected[index]; })
    ) {
      fail(label + " 字段不匹配");
    }
    return value;
  }

  function allowedKeys(value, required, optional, label) {
    if (!isPlainObject(value)) {
      fail(label + " 必须是普通对象");
    }
    var allowed = Object.create(null);
    required.forEach(function (key) { allowed[key] = true; });
    (optional || []).forEach(function (key) { allowed[key] = true; });
    required.forEach(function (key) {
      if (!Object.prototype.hasOwnProperty.call(value, key)) {
        fail(label + " 缺少字段：" + key);
      }
    });
    Object.keys(value).forEach(function (key) {
      if (!allowed[key]) fail(label + " 包含未知字段：" + key);
    });
    return value;
  }

  function clone(value) {
    if (value == null) return value;
    try {
      return JSON.parse(JSON.stringify(value));
    } catch (_) {
      fail("WebRTC 信令不能序列化");
    }
  }

  function byteLength(value) {
    var text = typeof value === "string" ? value : JSON.stringify(value);
    if (typeof TextEncoder !== "undefined") {
      return new TextEncoder().encode(text).byteLength;
    }
    return unescape(encodeURIComponent(text)).length;
  }

  function safeId(value, label) {
    if (
      typeof value !== "string" ||
      !/^[A-Za-z0-9._:-]{1,160}$/.test(value)
    ) {
      fail(label + " 无效");
    }
    return value;
  }

  function safeReason(value) {
    if (
      typeof value !== "string" ||
      value.length < 1 ||
      value.length > 160 ||
      /[\u0000-\u001f\u007f]/.test(value)
    ) {
      fail("bye reason 无效");
    }
    return value;
  }

  function safeInteger(value, label, minimum, maximum) {
    if (
      typeof value !== "number" ||
      !Number.isSafeInteger(value) ||
      value < minimum ||
      value > maximum
    ) {
      fail(label + " 无效");
    }
    return value;
  }

  function normalizeDescription(kind, value) {
    exactKeys(value, ["type", "sdp"], kind + " payload");
    if (
      value.type !== kind ||
      typeof value.sdp !== "string" ||
      !value.sdp ||
      byteLength(value.sdp) > MAX_SDP_BYTES ||
      value.sdp.indexOf("\u0000") !== -1
    ) {
      fail(kind + " SDP 无效");
    }
    return { type: kind, sdp: value.sdp };
  }

  function normalizeIce(value) {
    exactKeys(
      value,
      ["candidate", "sdpMid", "sdpMLineIndex"],
      "ICE payload",
    );
    if (
      typeof value.candidate !== "string" ||
      !value.candidate ||
      value.candidate.length > 1024 ||
      value.candidate.indexOf("\u0000") !== -1 ||
      (
        value.sdpMid !== null &&
        (
          typeof value.sdpMid !== "string" ||
          !/^[A-Za-z0-9._:-]{0,64}$/.test(value.sdpMid)
        )
      ) ||
      (
        value.sdpMLineIndex !== null &&
        (
          typeof value.sdpMLineIndex !== "number" ||
          !Number.isSafeInteger(value.sdpMLineIndex) ||
          value.sdpMLineIndex < 0 ||
          value.sdpMLineIndex > 65535
        )
      )
    ) {
      fail("ICE candidate 无效");
    }
    return {
      candidate: value.candidate,
      sdpMid: value.sdpMid,
      sdpMLineIndex: value.sdpMLineIndex,
    };
  }

  function normalizeSignal(value, incoming) {
    exactKeys(
      value,
      incoming
        ? ["cursor", "signalId", "kind", "payload"]
        : ["signalId", "kind", "payload"],
      "signal",
    );
    var kind = String(value.kind || "");
    if (!SIGNAL_KINDS[kind]) fail("signal kind 无效");
    var payload;
    if (kind === "offer" || kind === "answer") {
      payload = normalizeDescription(kind, value.payload);
    } else if (kind === "ice") {
      payload = normalizeIce(value.payload);
    } else {
      exactKeys(value.payload, ["reason"], "bye payload");
      payload = { reason: safeReason(value.payload.reason) };
    }
    var result = {
      signalId: safeId(value.signalId, "signalId"),
      kind: kind,
      payload: payload,
    };
    if (incoming) {
      result.cursor = safeInteger(
        value.cursor,
        "signal cursor",
        1,
        Number.MAX_SAFE_INTEGER,
      );
    }
    return result;
  }

  function signalDigest(signal) {
    return JSON.stringify({
      kind: signal.kind,
      payload: signal.payload,
      signalId: signal.signalId,
    });
  }

  function audioMidsFromSdp(sdp) {
    var lines = String(sdp).replace(/\r\n/g, "\n").replace(/\r/g, "\n")
      .split("\n");
    var sections = [];
    var current = null;
    lines.forEach(function (line) {
      if (line.indexOf("m=") === 0) {
        current = { media: line.slice(2).split(/\s+/, 1)[0], mid: null };
        sections.push(current);
      } else if (current && line.indexOf("a=mid:") === 0) {
        if (current.mid !== null) fail("SDP media 段包含重复 mid");
        current.mid = line.slice(6);
      }
    });
    if (
      sections.length !== 2 ||
      sections.some(function (section) {
        return section.media !== "audio" ||
          !/^[A-Za-z0-9._-]{1,64}$/.test(section.mid || "");
      }) ||
      sections[0].mid === sections[1].mid
    ) {
      fail(
        "WebRTC SDP 必须且只能包含两个具名 audio media 段",
        "BW_COMPUTER_VOICE_WEBRTC_MEDIA_SCOPE",
        false,
      );
    }
    return [sections[0].mid, sections[1].mid];
  }

  function listen(target, type, handler) {
    if (target && typeof target.addEventListener === "function") {
      target.addEventListener(type, handler);
      return function () {
        try { target.removeEventListener(type, handler); } catch (_) {}
      };
    }
    var property = "on" + type;
    var previous = target && target[property];
    var installed = function (event) {
      if (typeof previous === "function") previous.call(target, event);
      handler(event);
    };
    if (target) target[property] = installed;
    return function () {
      if (target && target[property] === installed) {
        target[property] = previous || null;
      }
    };
  }

  function validateGate(value) {
    exactKeys(
      value,
      ["paired", "localOptIn", "oneTimeTrigger", "nativeReady"],
      "启动门禁",
    );
    if (
      value.paired !== true ||
      value.localOptIn !== true ||
      value.oneTimeTrigger !== true ||
      value.nativeReady !== true
    ) {
      fail(
        "配对、本机同意、一次性触发与 nativeReady 必须同时成立",
        "BW_COMPUTER_VOICE_WEBRTC_GATE",
        false,
      );
    }
  }

  function validateSenderTracks(value) {
    if (!Array.isArray(value) || value.length !== 2) {
      fail(
        "Windows 必须提供且只提供两条本机音轨",
        "BW_COMPUTER_VOICE_WEBRTC_MEDIA_SCOPE",
        false,
      );
    }
    var byId = Object.create(null);
    value.forEach(function (entry) {
      exactKeys(entry, ["trackId", "track"], "Windows 本机音轨");
      var trackId = String(entry.trackId || "");
      var track = entry.track;
      if (
        !track ||
        track.kind !== "audio" ||
        TRACK_IDS.indexOf(trackId) === -1 ||
        byId[trackId]
      ) {
        fail(
          "Windows 音轨必须是唯一的 app-output 与 user-mic",
          "BW_COMPUTER_VOICE_WEBRTC_MEDIA_SCOPE",
          false,
        );
      }
      byId[trackId] = { trackId: trackId, track: track };
    });
    return TRACK_IDS.map(function (trackId) { return byId[trackId]; });
  }

  function createComputerVoiceWebRtcController(options) {
    options = options || {};
    var role = String(options.role || "");
    if (role !== ROLE_WINDOWS_SENDER && role !== ROLE_READER_RECEIVER) {
      fail("role 无效");
    }
    var sessionId = safeId(options.sessionId, "sessionId");
    var PeerConnection = options.RTCPeerConnection;
    var signalTransport = options.signalTransport;
    var clock = options.clock;
    var setTimer = options.setTimeout;
    var clearTimer = options.clearTimeout;
    var idFactory = options.signalIdFactory;
    var onTrack = typeof options.onTrack === "function"
      ? options.onTrack
      : function () {};
    var onStatus = typeof options.onStatus === "function"
      ? options.onStatus
      : function () {};
    var releaseLocalTracks = typeof options.releaseLocalTracks === "function"
      ? options.releaseLocalTracks
      : function () {};
    var localTracks = options.localTracks;
    var pollIntervalMs = Math.max(
      50,
      Math.min(5000, Number(options.pollIntervalMs) ||
        DEFAULT_POLL_INTERVAL_MS),
    );
    var negotiationTimeoutMs = Math.max(
      1000,
      Math.min(120000, Number(options.negotiationTimeoutMs) ||
        DEFAULT_NEGOTIATION_TIMEOUT_MS),
    );
    var maxPolls = Math.max(
      1,
      Math.min(120, Number(options.maxPolls) || DEFAULT_MAX_POLLS),
    );

    if (
      typeof PeerConnection !== "function" ||
      !signalTransport ||
      signalTransport.contract !== SIGNAL_CONTRACT ||
      typeof signalTransport.exchange !== "function" ||
      typeof clock !== "function" ||
      typeof setTimer !== "function" ||
      typeof clearTimer !== "function" ||
      typeof idFactory !== "function"
    ) {
      fail(
        "WebRTC 控制器依赖不完整",
        "BW_COMPUTER_VOICE_WEBRTC_DEPENDENCY",
        false,
      );
    }

    var state = "idle";
    var peer = null;
    var startedAt = null;
    var deadlineAt = null;
    var timer = null;
    var pollInFlight = null;
    var pollCount = 0;
    var cursor = 0;
    var pendingSignals = new Map();
    var sentSignalIds = new Set();
    var seenSignals = new Map();
    var pendingIce = [];
    var remoteDescriptionSet = false;
    var localDescriptionSet = false;
    var descriptionReceived = false;
    var triggerConsumed = false;
    var listeners = [];
    var lastError = null;
    var receiverMidMap = new Map();
    var senderMidMap = new Map();
    var senderTrackIds = new Map();
    var receivedTracks = new Map();
    var released = false;

    function snapshot() {
      return {
        contract: CONTRACT,
        signalContract: SIGNAL_CONTRACT,
        role: role,
        sessionId: sessionId,
        state: state,
        cursor: cursor,
        pollCount: pollCount,
        pendingSignals: pendingSignals.size,
        receivedTrackIds: TRACK_IDS.filter(function (trackId) {
          return receivedTracks.has(trackId);
        }),
        localDescriptionSet: localDescriptionSet,
        remoteDescriptionSet: remoteDescriptionSet,
        triggerConsumed: triggerConsumed,
        autoReconnect: false,
        lastError: lastError && {
          code: lastError.code,
          message: lastError.message,
        },
      };
    }

    function emitStatus() {
      try { onStatus(snapshot()); } catch (_) {}
    }

    function clearScheduledPoll() {
      if (timer !== null) {
        try { clearTimer(timer); } catch (_) {}
        timer = null;
      }
    }

    function detachListeners() {
      listeners.splice(0).forEach(function (remove) {
        try { remove(); } catch (_) {}
      });
    }

    function releaseOnce(reason) {
      if (released) return;
      released = true;
      try { releaseLocalTracks(reason); } catch (_) {}
    }

    function closePeer(reason, failure) {
      if (
        state === "stopped" ||
        state === "failed"
      ) {
        return;
      }
      clearScheduledPoll();
      detachListeners();
      pendingIce.length = 0;
      pendingSignals.clear();
      pollInFlight = null;
      if (failure) {
        lastError = failure instanceof WebRtcControllerError
          ? failure
          : new WebRtcControllerError(
            failure && failure.message || String(failure || reason),
            "BW_COMPUTER_VOICE_WEBRTC_FAILED",
            false,
          );
        state = "failed";
      } else {
        state = "stopped";
      }
      if (peer) {
        try { peer.close(); } catch (_) {}
      }
      releaseOnce(reason);
      emitStatus();
    }

    function failClosed(error, code) {
      var normalized = error instanceof WebRtcControllerError
        ? error
        : new WebRtcControllerError(
          error && error.message || String(error || "WebRTC 失败"),
          code || "BW_COMPUTER_VOICE_WEBRTC_FAILED",
          false,
        );
      closePeer("failed", normalized);
      return normalized;
    }

    function nextSignalId() {
      return safeId(idFactory(), "signalId");
    }

    function queueSignal(kind, payload) {
      if (pendingSignals.size >= MAX_PENDING_SIGNALS) {
        fail(
          "待发送信令超过本地上限",
          "BW_COMPUTER_VOICE_WEBRTC_CAPACITY",
          false,
        );
      }
      var normalized = normalizeSignal({
        signalId: nextSignalId(),
        kind: kind,
        payload: payload,
      }, false);
      if (
        pendingSignals.has(normalized.signalId) ||
        sentSignalIds.has(normalized.signalId)
      ) {
        fail(
          "signalId 生成器返回重复值",
          "BW_COMPUTER_VOICE_WEBRTC_SIGNAL_ID",
          false,
        );
      }
      pendingSignals.set(normalized.signalId, normalized);
      sentSignalIds.add(normalized.signalId);
      return normalized.signalId;
    }

    function localIce(event) {
      try {
        if (!event || !event.candidate) return;
        var candidate = event.candidate;
        var value = typeof candidate.toJSON === "function"
          ? candidate.toJSON()
          : {
            candidate: candidate.candidate,
            sdpMid: candidate.sdpMid,
            sdpMLineIndex: candidate.sdpMLineIndex,
          };
        queueSignal("ice", normalizeIce({
          candidate: value.candidate,
          sdpMid: value.sdpMid == null ? null : value.sdpMid,
          sdpMLineIndex: value.sdpMLineIndex == null
            ? null
            : value.sdpMLineIndex,
        }));
      } catch (error) {
        failClosed(error);
      }
    }

    function maybeConnected() {
      if (!peer || peer.connectionState !== "connected") return;
      if (
        role === ROLE_READER_RECEIVER &&
        receivedTracks.size !== TRACK_IDS.length
      ) {
        return;
      }
      state = "connected";
      clearScheduledPoll();
      emitStatus();
    }

    function connectionChanged() {
      if (!peer) return;
      if (
        state === "stopping" ||
        state === "stopped" ||
        state === "failed"
      ) {
        return;
      }
      var connectionState = String(peer.connectionState || "");
      var iceState = String(peer.iceConnectionState || "");
      if (
        connectionState === "failed" ||
        connectionState === "disconnected" ||
        connectionState === "closed" ||
        iceState === "failed" ||
        iceState === "disconnected" ||
        iceState === "closed"
      ) {
        failClosed(new WebRtcControllerError(
          "WebRTC/ICE 连接已失效",
          "BW_COMPUTER_VOICE_WEBRTC_CONNECTION_LOST",
          false,
          { connectionState: connectionState, iceConnectionState: iceState },
        ));
        return;
      }
      maybeConnected();
    }

    function remoteTrack(event) {
      try {
        if (
          state === "stopping" ||
          state === "stopped" ||
          state === "failed"
        ) {
          return;
        }
        if (role !== ROLE_READER_RECEIVER) {
          fail(
            "Windows sender 不允许接收远端音轨",
            "BW_COMPUTER_VOICE_WEBRTC_MEDIA_SCOPE",
            false,
          );
        }
        var track = event && event.track;
        var mid = event && event.transceiver &&
          String(event.transceiver.mid || "");
        var trackId = receiverMidMap.get(mid);
        if (
          !track ||
          track.kind !== "audio" ||
          !trackId ||
          receivedTracks.has(trackId) ||
          receivedTracks.size >= TRACK_IDS.length
        ) {
          fail(
            "远端音轨不在预绑定的两个 audio mid 内",
            "BW_COMPUTER_VOICE_WEBRTC_MEDIA_SCOPE",
            false,
          );
        }
        receivedTracks.set(trackId, track);
        onTrack({ trackId: trackId, mid: mid, track: track });
        maybeConnected();
      } catch (error) {
        failClosed(error);
      }
    }

    function installPeerListeners() {
      listeners.push(listen(peer, "icecandidate", localIce));
      listeners.push(listen(peer, "track", remoteTrack));
      listeners.push(listen(peer, "connectionstatechange", connectionChanged));
      listeners.push(listen(peer, "iceconnectionstatechange", connectionChanged));
    }

    function bindSenderMids(sdp) {
      var sdpMids = audioMidsFromSdp(sdp);
      if (typeof peer.getTransceivers !== "function") {
        fail("RTCPeerConnection 缺少 getTransceivers");
      }
      var mapped = Object.create(null);
      peer.getTransceivers().forEach(function (transceiver) {
        var track = transceiver && transceiver.sender &&
          transceiver.sender.track;
        var trackId = track && senderTrackIds.get(track);
        if (trackId) {
          var mid = String(transceiver.mid || "");
          if (!mid || sdpMids.indexOf(mid) === -1 || mapped[trackId]) {
            fail(
              "Windows sender 的音轨 mid 绑定无效",
              "BW_COMPUTER_VOICE_WEBRTC_MEDIA_SCOPE",
              false,
            );
          }
          mapped[trackId] = mid;
        }
      });
      if (
        !mapped[TRACK_APP_OUTPUT] ||
        !mapped[TRACK_USER_MIC] ||
        mapped[TRACK_APP_OUTPUT] === mapped[TRACK_USER_MIC]
      ) {
        fail(
          "Windows sender 未建立两条唯一音轨的 mid 绑定",
          "BW_COMPUTER_VOICE_WEBRTC_MEDIA_SCOPE",
          false,
        );
      }
      senderMidMap = new Map([
        [TRACK_APP_OUTPUT, mapped[TRACK_APP_OUTPUT]],
        [TRACK_USER_MIC, mapped[TRACK_USER_MIC]],
      ]);
    }

    function bindReceiverMids(sdp) {
      var mids = audioMidsFromSdp(sdp);
      receiverMidMap = new Map([
        [mids[0], TRACK_APP_OUTPUT],
        [mids[1], TRACK_USER_MIC],
      ]);
    }

    function addRemoteIce(payload) {
      if (!remoteDescriptionSet) {
        if (pendingIce.length >= MAX_PENDING_ICE) {
          fail(
            "远端 ICE 缓冲超过上限",
            "BW_COMPUTER_VOICE_WEBRTC_CAPACITY",
            false,
          );
        }
        pendingIce.push(payload);
        return Promise.resolve();
      }
      return Promise.resolve(peer.addIceCandidate(clone(payload)));
    }

    async function drainRemoteIce() {
      var buffered = pendingIce.splice(0);
      for (var index = 0; index < buffered.length; index += 1) {
        await peer.addIceCandidate(clone(buffered[index]));
      }
    }

    async function processOffer(payload) {
      if (role !== ROLE_READER_RECEIVER || descriptionReceived) {
        fail(
          "当前角色不能接收此 offer",
          "BW_COMPUTER_VOICE_WEBRTC_NEGOTIATION",
          false,
        );
      }
      bindReceiverMids(payload.sdp);
      descriptionReceived = true;
      await peer.setRemoteDescription(clone(payload));
      remoteDescriptionSet = true;
      await drainRemoteIce();
      var answer = normalizeDescription(
        "answer",
        await peer.createAnswer(),
      );
      audioMidsFromSdp(answer.sdp);
      await peer.setLocalDescription(clone(answer));
      localDescriptionSet = true;
      queueSignal("answer", answer);
    }

    async function processAnswer(payload) {
      if (role !== ROLE_WINDOWS_SENDER || descriptionReceived) {
        fail(
          "当前角色不能接收此 answer",
          "BW_COMPUTER_VOICE_WEBRTC_NEGOTIATION",
          false,
        );
      }
      var answerMids = audioMidsFromSdp(payload.sdp);
      if (
        answerMids[0] !== senderMidMap.get(TRACK_APP_OUTPUT) ||
        answerMids[1] !== senderMidMap.get(TRACK_USER_MIC)
      ) {
        fail(
          "answer 的 audio mid 与本机双轨绑定不匹配",
          "BW_COMPUTER_VOICE_WEBRTC_MEDIA_SCOPE",
          false,
        );
      }
      descriptionReceived = true;
      await peer.setRemoteDescription(clone(payload));
      remoteDescriptionSet = true;
      await drainRemoteIce();
    }

    async function processIncoming(signal) {
      if (signal.kind === "offer") {
        await processOffer(signal.payload);
      } else if (signal.kind === "answer") {
        await processAnswer(signal.payload);
      } else if (signal.kind === "ice") {
        await addRemoteIce(signal.payload);
      } else {
        closePeer("remote-bye", null);
      }
    }

    function normalizeExchangeResponse(value) {
      allowedKeys(
        value,
        [
          "contract",
          "sessionId",
          "ackedSignalIds",
          "signals",
          "cursor",
        ],
        ["ok", "deviceId", "expiresAt"],
        "信令响应",
      );
      if (value.contract !== SIGNAL_CONTRACT) {
        fail(
          "信令响应合同不匹配",
          "BW_COMPUTER_VOICE_WEBRTC_SIGNAL_CONTRACT",
          false,
        );
      }
      if (value.sessionId !== sessionId) {
        fail(
          "信令响应 sessionId 不匹配",
          "BW_COMPUTER_VOICE_WEBRTC_SIGNAL_CONTRACT",
          false,
        );
      }
      if (Object.prototype.hasOwnProperty.call(value, "ok") && value.ok !== true) {
        fail(
          "信令服务未返回成功回执",
          "BW_COMPUTER_VOICE_WEBRTC_SIGNAL_CONTRACT",
          false,
        );
      }
      if (Object.prototype.hasOwnProperty.call(value, "deviceId")) {
        safeId(value.deviceId, "deviceId");
      }
      if (Object.prototype.hasOwnProperty.call(value, "expiresAt")) {
        safeInteger(
          value.expiresAt,
          "expiresAt",
          1,
          Number.MAX_SAFE_INTEGER,
        );
      }
      var responseCursor = safeInteger(
        value.cursor,
        "response cursor",
        cursor,
        Number.MAX_SAFE_INTEGER,
      );
      var acknowledged = Array.isArray(value.ackedSignalIds)
        ? value.ackedSignalIds.map(function (id) {
          return safeId(id, "ackedSignalId");
        })
        : fail("ackedSignalIds 必须是数组");
      var signals = Array.isArray(value.signals)
        ? value.signals.map(function (signal) {
          return normalizeSignal(signal, true);
        })
        : fail("signals 必须是数组");
      if (
        acknowledged.length > MAX_SIGNAL_BATCH ||
        signals.length > MAX_SIGNAL_BATCH
      ) {
        fail(
          "信令响应超过单批上限",
          "BW_COMPUTER_VOICE_WEBRTC_CAPACITY",
          false,
        );
      }
      var previousCursor = cursor;
      signals.forEach(function (signal) {
        if (
          signal.cursor <= previousCursor ||
          signal.cursor > responseCursor
        ) {
          fail(
            "响应信令 cursor 不连续递增",
            "BW_COMPUTER_VOICE_WEBRTC_SIGNAL_CURSOR",
            false,
          );
        }
        previousCursor = signal.cursor;
      });
      return {
        cursor: responseCursor,
        ackedSignalIds: acknowledged,
        signals: signals,
      };
    }

    async function exchangeOnce() {
      if (state !== "negotiating") return snapshot();
      if (
        pollCount >= maxPolls ||
        clock() >= deadlineAt
      ) {
        throw new WebRtcControllerError(
          "WebRTC 信令等待超时",
          "BW_COMPUTER_VOICE_WEBRTC_TIMEOUT",
          false,
        );
      }
      pollCount += 1;
      var outbound = Array.from(pendingSignals.values())
        .slice(0, MAX_SIGNAL_BATCH)
        .map(clone);
      var rawResponse = await signalTransport.exchange({
        contract: SIGNAL_CONTRACT,
        sessionId: sessionId,
        signals: outbound,
        cursor: cursor,
      });
      if (state !== "negotiating") return snapshot();
      var response = normalizeExchangeResponse(rawResponse);
      response.ackedSignalIds.forEach(function (signalId) {
        if (!sentSignalIds.has(signalId)) {
          fail(
            "信令服务确认了未知 signalId",
            "BW_COMPUTER_VOICE_WEBRTC_SIGNAL_CONTRACT",
            false,
          );
        }
        pendingSignals.delete(signalId);
      });
      for (var index = 0; index < response.signals.length; index += 1) {
        var signal = response.signals[index];
        var digest = signalDigest(signal);
        var previous = seenSignals.get(signal.signalId);
        if (previous !== undefined) {
          if (previous !== digest) {
            fail(
              "远端 signalId 被用于不同内容",
              "BW_COMPUTER_VOICE_WEBRTC_SIGNAL_ID",
              false,
            );
          }
          continue;
        }
        if (seenSignals.size >= MAX_SEEN_SIGNALS) {
          fail(
            "远端信令去重表超过上限",
            "BW_COMPUTER_VOICE_WEBRTC_CAPACITY",
            false,
          );
        }
        seenSignals.set(signal.signalId, digest);
        await processIncoming(signal);
        if (state !== "negotiating") break;
      }
      cursor = response.cursor;
      maybeConnected();
      emitStatus();
      return snapshot();
    }

    function schedulePoll() {
      if (state !== "negotiating" || timer !== null) return;
      timer = setTimer(function () {
        timer = null;
        poll().catch(function () {});
      }, pollIntervalMs);
    }

    async function poll() {
      if (state !== "negotiating") return snapshot();
      if (pollInFlight) return pollInFlight;
      pollInFlight = exchangeOnce().then(function (result) {
        pollInFlight = null;
        if (state === "negotiating") schedulePoll();
        return result;
      }, function (error) {
        pollInFlight = null;
        throw failClosed(error);
      });
      return pollInFlight;
    }

    async function start(gate) {
      if (state !== "idle" || triggerConsumed) {
        fail(
          "一次性 WebRTC 启动已消费",
          "BW_COMPUTER_VOICE_WEBRTC_TRIGGER_CONSUMED",
          false,
        );
      }
      validateGate(gate);
      triggerConsumed = true;
      state = "starting";
      startedAt = clock();
      deadlineAt = startedAt + negotiationTimeoutMs;
      try {
        peer = new PeerConnection({
          iceServers: [],
          iceTransportPolicy: "all",
          bundlePolicy: "max-bundle",
        });
        if (
          !peer ||
          typeof peer.addTransceiver !== "function" ||
          typeof peer.setLocalDescription !== "function" ||
          typeof peer.setRemoteDescription !== "function" ||
          typeof peer.addIceCandidate !== "function" ||
          typeof peer.close !== "function"
        ) {
          fail("RTCPeerConnection 实现不完整");
        }
        installPeerListeners();
        if (role === ROLE_WINDOWS_SENDER) {
          if (
            typeof peer.addTrack !== "function" ||
            typeof peer.createOffer !== "function"
          ) {
            fail("Windows RTCPeerConnection 缺少发送能力");
          }
          validateSenderTracks(localTracks).forEach(function (entry) {
            senderTrackIds.set(entry.track, entry.trackId);
            var sender = peer.addTrack(entry.track);
            var transceiver = peer.getTransceivers().find(function (entry) {
              return entry && entry.sender === sender;
            });
            if (!transceiver) {
              fail("addTrack 未返回可绑定的 audio transceiver");
            }
            transceiver.direction = "sendonly";
          });
          if (
            peer.getTransceivers().length !== TRACK_IDS.length ||
            peer.getTransceivers().some(function (entry) {
              return !entry ||
                entry.direction !== "sendonly" ||
                !entry.sender ||
                !entry.sender.track ||
                entry.sender.track.kind !== "audio";
            })
          ) {
            fail(
              "Windows sender 只能建立两个 sendonly audio transceiver",
              "BW_COMPUTER_VOICE_WEBRTC_MEDIA_SCOPE",
              false,
            );
          }
          var offer = normalizeDescription("offer", await peer.createOffer());
          await peer.setLocalDescription(clone(offer));
          localDescriptionSet = true;
          bindSenderMids(offer.sdp);
          queueSignal("offer", offer);
        } else {
          if (typeof peer.createAnswer !== "function") {
            fail("Reader RTCPeerConnection 缺少接收能力");
          }
          peer.addTransceiver("audio", { direction: "recvonly" });
          peer.addTransceiver("audio", { direction: "recvonly" });
          if (
            peer.getTransceivers().length !== TRACK_IDS.length ||
            peer.getTransceivers().some(function (entry) {
              return !entry || entry.direction !== "recvonly";
            })
          ) {
            fail(
              "Reader receiver 只能建立两个 recvonly audio transceiver",
              "BW_COMPUTER_VOICE_WEBRTC_MEDIA_SCOPE",
              false,
            );
          }
        }
        state = "negotiating";
        emitStatus();
        await poll();
        return snapshot();
      } catch (error) {
        throw failClosed(error);
      }
    }

    async function stop(reason) {
      reason = safeReason(reason || "user-stopped");
      if (state === "idle") {
        state = "stopped";
        releaseOnce(reason);
        emitStatus();
        return snapshot();
      }
      if (state === "stopped" || state === "failed") return snapshot();
      clearScheduledPoll();
      var activeState = state;
      state = "stopping";
      emitStatus();
      try {
        if (activeState === "negotiating" || activeState === "connected") {
          queueSignal("bye", { reason: reason });
          var outbound = Array.from(pendingSignals.values())
            .slice(0, MAX_SIGNAL_BATCH)
            .map(clone);
          await signalTransport.exchange({
            contract: SIGNAL_CONTRACT,
            sessionId: sessionId,
            signals: outbound,
            cursor: cursor,
          });
        }
      } catch (_) {
        // Teardown is local and fail-closed even when the metadata bye fails.
      }
      closePeer(reason, null);
      return snapshot();
    }

    return Object.freeze({
      contract: CONTRACT,
      signalContract: SIGNAL_CONTRACT,
      role: role,
      start: start,
      poll: poll,
      stop: stop,
      status: snapshot,
    });
  }

  return Object.freeze({
    CONTRACT: CONTRACT,
    SIGNAL_CONTRACT: SIGNAL_CONTRACT,
    ROLE_WINDOWS_SENDER: ROLE_WINDOWS_SENDER,
    ROLE_READER_RECEIVER: ROLE_READER_RECEIVER,
    TRACK_APP_OUTPUT: TRACK_APP_OUTPUT,
    TRACK_USER_MIC: TRACK_USER_MIC,
    TRACK_IDS: TRACK_IDS,
    WebRtcControllerError: WebRtcControllerError,
    createComputerVoiceWebRtcController: createComputerVoiceWebRtcController,
  });
});
