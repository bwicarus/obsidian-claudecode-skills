/* rc-computer-voice.js — 电脑客户端桥接器的共享 Reader/PWA/扩展入口。
 *
 * 选择模型只读取状态，不触发 Windows。只有电话按钮的一次真实用户操作会
 * 调用 startFromUserGesture；服务端仍要再次证明配对、opt-in、应用就绪与
 * native/media host 就绪，任一不成立都 fail closed。
 */
(function () {
  "use strict";

  var RC = window.RC = window.RC || {};
  var WebRtc = window.BWComputerVoiceWebRtc;
  var BRIDGE_CONTRACT = "reader-computer-voice-bridge/1";
  var PAIRING_CONTRACT = "reader-computer-voice-pairing/1";
  var SIGNAL_CONTRACT = "reader-computer-voice-signal/1";
  var BASE = "/api/reader/computer-voice";
  var active = null;
  var sequence = 0;
  var statusListeners = [];

  function safeId(value) {
    var text = String(value || "");
    return /^[A-Za-z0-9._:-]{1,160}$/.test(text) ? text : "";
  }

  function requestJson(path, init) {
    init = init || {};
    var headers = Object.assign({
      "Accept": "application/json",
    }, init.headers || {});
    if (init.body != null) headers["Content-Type"] = "application/json";
    // @interaction computer-voice.bridge.request
    return fetch(path, Object.assign({}, init, {
      credentials: "same-origin",
      cache: "no-store",
      headers: headers,
    })).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (!response.ok || !body || body.ok !== true) {
          var error = new Error(
            (body && (body.error || body.code)) ||
            ("电脑客户端请求失败(" + response.status + ")")
          );
          error.code = body && body.code || "BW_COMPUTER_VOICE_HTTP";
          error.status = response.status;
          throw error;
        }
        return body;
      });
    });
  }

  function emitStatus(value) {
    var snapshot = Object.assign({
      mode: "computer-client",
      active: !!active,
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

  function listDevices() {
    return requestJson(BASE + "/devices", { method: "GET" }).then(function (body) {
      return (body.devices || []).filter(function (device) {
        return device && device.state === "active" && safeId(device.deviceId);
      });
    });
  }

  function preferredDevice(devices) {
    var saved = "";
    try { saved = safeId(localStorage.getItem("rc-computer-voice-device")); } catch (_) {}
    var found = devices.find(function (device) {
      return device.deviceId === saved;
    }) || devices[0] || null;
    if (found) {
      try {
        localStorage.setItem("rc-computer-voice-device", found.deviceId);
      } catch (_) {}
    }
    return found;
  }

  function deviceStatus(deviceId) {
    deviceId = safeId(deviceId);
    if (!deviceId) return Promise.reject(new Error("电脑客户端设备 ID 无效"));
    return requestJson(
      BASE + "/devices/" + encodeURIComponent(deviceId) + "/status",
      { method: "GET" }
    );
  }

  function availability() {
    return listDevices().then(function (devices) {
      var device = preferredDevice(devices);
      if (!device) {
        return {
          paired: false,
          state: "unpaired",
          reason: "pairing-required",
          device: null,
        };
      }
      return deviceStatus(device.deviceId).then(function (status) {
        return {
          paired: true,
          state: status.state,
          reason: status.reason || null,
          device: device,
          status: status,
        };
      });
    });
  }

  function beginPairing() {
    return requestJson(BASE + "/pairings", {
      method: "POST",
      body: JSON.stringify({ contract: PAIRING_CONTRACT }),
    });
  }

  function createSignalTransport(sessionId) {
    return Object.freeze({
      contract: SIGNAL_CONTRACT,
      exchange: function (message) {
        if (!message || message.sessionId !== sessionId) {
          return Promise.reject(new Error("电脑客户端信令会话不匹配"));
        }
        return requestJson(
          BASE + "/sessions/" + encodeURIComponent(sessionId) + "/signals",
          {
            method: "POST",
            body: JSON.stringify({
              contract: SIGNAL_CONTRACT,
              signals: message.signals,
              cursor: message.cursor,
            }),
          }
        );
      },
    });
  }

  function makeAudioSurface() {
    var audio = document.createElement("audio");
    audio.id = "rc-computer-voice-audio";
    audio.autoplay = true;
    audio.playsInline = true;
    audio.setAttribute("aria-hidden", "true");
    audio.style.display = "none";
    (document.body || document.documentElement).appendChild(audio);
    return {
      audio: audio,
      outputStream: new MediaStream(),
      micStream: new MediaStream(),
    };
  }

  function releaseSurface(surface) {
    if (!surface) return;
    [surface.outputStream, surface.micStream].forEach(function (stream) {
      try {
        stream.getTracks().forEach(function (track) { track.stop(); });
      } catch (_) {}
    });
    try {
      surface.audio.pause();
      surface.audio.srcObject = null;
      surface.audio.remove();
    } catch (_) {}
  }

  function startFromUserGesture(options) {
    options = options || {};
    if (active) {
      var existing = new Error("电脑客户端通话已经启动");
      existing.code = "BW_COMPUTER_VOICE_ALREADY_ACTIVE";
      return Promise.reject(existing);
    }
    if (!WebRtc || typeof WebRtc.createComputerVoiceWebRtcController !== "function") {
      var unavailable = new Error("电脑客户端 WebRTC 组件未加载");
      unavailable.code = "BW_COMPUTER_VOICE_WEBRTC_UNAVAILABLE";
      return Promise.reject(unavailable);
    }
    var surface = makeAudioSurface();
    var state = {
      controller: null,
      surface: surface,
      deviceId: "",
      sessionId: "",
      stopped: false,
    };
    active = state;
    emitStatus({ state: "checking", message: "正在确认电脑客户端…" });

    return availability().then(function (available) {
      if (
        !available.paired ||
        available.state !== "ready" ||
        !available.status ||
        !available.status.media ||
        available.status.media.hostReady !== true
      ) {
        var error = new Error(
          available.paired
            ? ("电脑客户端未就绪：" + (available.reason || available.state))
            : "尚未配对电脑客户端"
        );
        error.code = available.paired
          ? "BW_COMPUTER_VOICE_NOT_READY"
          : "BW_COMPUTER_VOICE_PAIRING_REQUIRED";
        throw error;
      }
      state.deviceId = available.device.deviceId;
      emitStatus({
        state: "starting",
        deviceId: state.deviceId,
        message: "正在请求电脑客户端启动…",
      });
      return requestJson(
        BASE + "/devices/" + encodeURIComponent(state.deviceId) + "/start",
        {
          method: "POST",
          body: JSON.stringify({ contract: BRIDGE_CONTRACT }),
        }
      );
    }).then(function (started) {
      if (state.stopped || active !== state) {
        throw Object.assign(new Error("电脑客户端启动已取消"), {
          code: "BW_COMPUTER_VOICE_CANCELLED",
        });
      }
      state.sessionId = safeId(started.session && started.session.sessionId);
      if (!state.sessionId) throw new Error("服务端未返回有效媒体会话");
      state.controller = WebRtc.createComputerVoiceWebRtcController({
        role: WebRtc.ROLE_READER_RECEIVER,
        sessionId: state.sessionId,
        RTCPeerConnection: window.RTCPeerConnection,
        signalTransport: createSignalTransport(state.sessionId),
        clock: Date.now,
        setTimeout: window.setTimeout.bind(window),
        clearTimeout: window.clearTimeout.bind(window),
        signalIdFactory: function () {
          sequence += 1;
          return "reader-" + Date.now().toString(36) + "-" + sequence.toString(36);
        },
        onTrack: function (event) {
          if (!event || !event.track) return;
          if (event.trackId === WebRtc.TRACK_APP_OUTPUT) {
            surface.outputStream.addTrack(event.track);
            surface.audio.srcObject = surface.outputStream;
            var play = surface.audio.play();
            if (play && typeof play.catch === "function") {
              play.catch(function () {
                emitStatus({
                  state: "audio-blocked",
                  message: "浏览器阻止了播放，请再点一次通话按钮",
                });
              });
            }
          } else if (event.trackId === WebRtc.TRACK_USER_MIC) {
            // The mic track reaches the Reader client for context/visualisation
            // but is intentionally not played back, preventing headset echo.
            surface.micStream.addTrack(event.track);
          }
          try {
            window.dispatchEvent(new CustomEvent(
              "bw-computer-voice-track",
              {
                detail: {
                  trackId: event.trackId,
                  stream: event.trackId === WebRtc.TRACK_APP_OUTPUT
                    ? surface.outputStream
                    : surface.micStream,
                },
              }
            ));
          } catch (_) {}
        },
        onStatus: function (snapshot) {
          emitStatus({
            state: snapshot.state,
            deviceId: state.deviceId,
            sessionId: state.sessionId,
            message: snapshot.state === "connected"
              ? "电脑客户端通话中"
              : "电脑客户端：" + snapshot.state,
            details: snapshot,
          });
        },
      });
      return state.controller.start({
        paired: true,
        localOptIn: true,
        oneTimeTrigger: true,
        nativeReady: true,
      });
    }).then(function () {
      return {
        ok: true,
        deviceId: state.deviceId,
        sessionId: state.sessionId,
      };
    }).catch(function (error) {
      if (active === state) active = null;
      state.stopped = true;
      try {
        if (state.controller) state.controller.stop("start-failed");
      } catch (_) {}
      releaseSurface(surface);
      emitStatus({
        state: "failed",
        message: error && error.message || "电脑客户端启动失败",
        code: error && error.code || "BW_COMPUTER_VOICE_START_FAILED",
      });
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
    state.stopped = true;
    var pending = Promise.resolve();
    try {
      if (state.controller) {
        pending = Promise.resolve(state.controller.stop(reason || "user-stopped"));
      }
    } catch (_) {}
    return pending.catch(function () {}).then(function () {
      releaseSurface(state.surface);
      emitStatus({ state: "stopped", message: "电脑客户端已挂断" });
      return { ok: true, state: "stopped" };
    });
  }

  function mountSettings(container) {
    if (!container || container.querySelector(".rc-computer-voice-settings")) return;
    var root = document.createElement("div");
    root.className = "rc-computer-voice-settings";
    root.innerHTML =
      '<div class="ams-tdef" data-role="status">正在检查电脑客户端…</div>' +
      '<div class="ams-row" style="margin-top:7px">' +
      '<button type="button" class="ams-btn" data-role="pair">生成一次性配对码</button>' +
      '<button type="button" class="ams-btn" data-role="refresh">刷新状态</button>' +
      '</div><div class="ams-tdef" data-role="pairing" style="display:none"></div>';
    container.appendChild(root);
    var status = root.querySelector('[data-role="status"]');
    var pairing = root.querySelector('[data-role="pairing"]');
    function refresh() {
      status.textContent = "正在检查电脑客户端…";
      availability().then(function (value) {
        if (!value.paired) {
          status.textContent = "尚未配对。生成配对码后，在 Windows 桥接器中输入。";
        } else if (value.state === "ready") {
          status.textContent = "● 电脑客户端已就绪；点击侧栏电话按钮才会启动。";
        } else {
          status.textContent = "○ 电脑客户端：" +
            (value.reason || value.state || "未就绪");
        }
      }).catch(function (error) {
        status.textContent = "状态读取失败：" + (error.message || "未知错误");
      });
    }
    root.querySelector('[data-role="refresh"]').addEventListener("click", refresh);
    root.querySelector('[data-role="pair"]').addEventListener("click", function () {
      beginPairing().then(function (value) {
        pairing.style.display = "";
        pairing.textContent = "配对 ID " + value.pairId +
          "　配对码 " + value.pairingCode +
          "（" + Math.max(0, Math.round((value.expiresAt * 1000 - Date.now()) / 1000)) +
          " 秒内有效）。请把两项填入 Windows 扩展弹窗。";
      }).catch(function (error) {
        pairing.style.display = "";
        pairing.textContent = "生成失败：" + (error.message || "未知错误");
      });
    });
    refresh();
  }

  RC.computerVoice = Object.freeze({
    contract: BRIDGE_CONTRACT,
    availability: availability,
    beginPairing: beginPairing,
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
