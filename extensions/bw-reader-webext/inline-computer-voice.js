(function () {
  "use strict";

  var CONTRACT = "bw-extension-computer-voice-frame/1";
  var button = document.getElementById("asst-computer");
  var appKind = "codex-desktop";
  var starting = false;
  var active = false;

  function send(type, value) {
    try {
      window.parent.postMessage({
        contract: CONTRACT,
        type: type,
        value: value || null,
      }, "*");
    } catch (_) {}
  }

  function render(state, message) {
    button.classList.toggle("connecting", state === "connecting");
    button.classList.toggle("on", state === "active");
    button.classList.toggle("failed", state === "failed");
    button.disabled = state === "connecting";
    var label = message || (
      state === "active" ? "电脑客户端通话中，再点停止" :
      state === "connecting" ? "正在连接 Windows 电脑客户端" :
      state === "failed" ? "电脑客户端启动失败，点按重试" :
      "启动电脑客户端语音"
    );
    button.title = label;
    button.setAttribute("aria-label", label);
    send("state", { state: state, message: label, appKind: appKind });
  }

  function allowedTarget(value) {
    return value === "chatgpt-classic" ? "chatgpt-classic" : "codex-desktop";
  }

  window.addEventListener("message", function (event) {
    if (event.source !== window.parent) return;
    var data = event.data;
    if (!data || data.contract !== CONTRACT || data.type !== "configure") return;
    appKind = allowedTarget(data.appKind);
  });

  if (
    !button ||
    !window.RC ||
    !RC.computerVoice ||
    typeof RC.computerVoice.startFromUserGesture !== "function" ||
    typeof RC.computerVoice.stop !== "function" ||
    !RC.voicecall ||
    typeof RC.voicecall.canCaptureComputerVoiceGesture !== "function" ||
    RC.voicecall.canCaptureComputerVoiceGesture() !== true ||
    RC.computerVoice.registerComputerButton(button) !== true
  ) {
    if (button) render("failed", "扩展电脑语音组件未就绪");
    send("ready", { ok: false });
    return;
  }

  RC.computerVoice.onStatus(function (status) {
    var state = status && status.state || "";
    var message = status && status.message || "";
    if (state === "connected" || state === "audio-blocked") {
      starting = false;
      active = true;
      render("active", message || "电脑客户端通话中，再点停止");
    } else if (state === "checking" || state === "starting") {
      starting = true;
      render("connecting", message);
    } else if (state === "failed") {
      starting = false;
      active = false;
      render("failed", message);
    } else if (state === "stopped") {
      starting = false;
      active = false;
      render("idle", message);
    }
  });

  button.addEventListener("click", function () {
    if (starting) return;
    if (active || RC.computerVoice.isActive()) {
      starting = true;
      render("connecting", "正在停止电脑客户端…");
      RC.computerVoice.stop("extension-inline-button").then(function () {
        starting = false;
        active = false;
        render("idle", "启动电脑客户端语音");
      }).catch(function (error) {
        starting = false;
        render("failed", error && error.message || "电脑客户端停止失败");
      });
      return;
    }

    starting = true;
    render("connecting", "正在连接 Windows 电脑客户端…");
    RC.computerVoice.startFromUserGesture({ appKind: appKind }).then(function () {
      starting = false;
      active = true;
      render("active", "电脑客户端通话中，再点停止");
    }).catch(function (error) {
      starting = false;
      active = false;
      render("failed", error && error.message || "电脑客户端启动失败");
    });
  });

  render("idle", "启动电脑客户端语音");
  send("ready", { ok: true });
})();
