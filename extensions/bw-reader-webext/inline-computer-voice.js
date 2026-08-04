(function () {
  "use strict";

  var CONTRACT = "bw-extension-computer-voice-frame/1";
  var button = document.getElementById("asst-computer");
  var appKind = "codex-desktop";
  var starting = false;
  var active = false;

  // Diagnostic trail, readable from the popup.
  //
  // The failure this exists for is silent from every angle: nothing reaches
  // Windows, so there is no server-side record; the frame is 42px and cannot
  // show text; toast needs RC on the host page; and long-press does not reliably
  // surface a title on iPad. Without a trail, each attempt at a fix is a guess
  // costing one TestFlight round.
  //
  // Remove once the break is located.
  function trace(stage, detail) {
    try {
      chrome.storage.local.get("bwVoiceTrace", function (bag) {
        var list = (bag && bag.bwVoiceTrace) || [];
        list.push({
          at: new Date().toISOString().slice(11, 19),
          stage: String(stage),
          detail: detail === undefined ? "" : String(detail).slice(0, 200),
        });
        // Keep only the recent tail: one failed attempt is what matters, and an
        // unbounded list would eventually be the thing that breaks.
        while (list.length > 24) list.shift();
        chrome.storage.local.set({ bwVoiceTrace: list });
      });
    } catch (_) {}
  }

  trace("frame-loaded", location.href.slice(0, 60));
  trace("env", "runtime.id=" + (window.chrome && chrome.runtime && chrome.runtime.id ? "有" : "无")
    + " origin=" + String(location.origin).slice(0, 40)
    + " secure=" + (window.isSecureContext ? "是" : "否"));

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
    // Each condition separately, so the trail names the one that failed rather
    // than reporting the whole check as "not ready".
    trace("ready-failed",
      "button=" + (button ? "有" : "无") +
      " RC=" + (window.RC ? "有" : "无") +
      " cv=" + (window.RC && RC.computerVoice ? "有" : "无") +
      " vc=" + (window.RC && RC.voicecall ? "有" : "无") +
      " gesture=" + (function () {
        try { return RC.voicecall.canCaptureComputerVoiceGesture() === true ? "许可" : "拒绝"; }
        catch (e) { return "抛错"; }
      })() +
      " register=" + (function () {
        try { return RC.computerVoice.registerComputerButton(button) === true ? "成功" : "被拒"; }
        catch (e) { return "抛错"; }
      })());
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
    trace("click", "开始拨号");
    render("connecting", "正在连接 Windows 电脑客户端…");
    RC.computerVoice.startFromUserGesture({ appKind: appKind }).then(function () {
      trace("start-ok", "通话已建立");
      starting = false;
      active = true;
      render("active", "电脑客户端通话中，再点停止");
    }).catch(function (error) {
      starting = false;
      active = false;
      // The code is what distinguishes "refused by the bridge" from "never got
      // there", and it is exactly what has been invisible until now.
      trace("start-failed",
        (error && error.code ? error.code + " | " : "") +
        (error && error.message ? error.message : String(error)));
      render("failed", error && error.message || "电脑客户端启动失败");
    });
  });

  trace("ready-ok", "组件就绪，等待点击");
  render("idle", "启动电脑客户端语音");
  send("ready", { ok: true });
})();
