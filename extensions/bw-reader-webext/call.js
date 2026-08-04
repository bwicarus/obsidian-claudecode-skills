"use strict";

// Drives the context bridge: hold one link to Windows, and forward whatever
// page the user is currently looking at.
//
// Pages report themselves. A content script already runs on every site, so the
// page in front of the user announces its own content and this only has to
// relay it -- no reading of other tabs, and therefore no permission over them.
// That is why switching tabs switches the context, which polling could never
// do: activeTab never extends past the tab it was granted on.

import { ContextLink } from "./ctxlink.js";

const els = {
  btn: document.getElementById("asst-computer"),
  status: document.getElementById("status"),
  detail: document.getElementById("detail"),
  ctxTitle: document.getElementById("ctxTitle"),
  ctxUrl: document.getElementById("ctxUrl"),
};

function say(text, cls) {
  els.status.textContent = text;
  els.status.className = cls || "dim";
}

function note(line) {
  els.detail.style.display = "block";
  els.detail.textContent = (els.detail.textContent + "\n" + line).trim().split("\n").slice(-14).join("\n");
}

function describe(err) {
  if (!err) return "(no error)";
  if (err.code && err.message) return `${err.code} | ${err.message}`;
  return err.message || String(err);
}

// Compared before sending. Without it the same page would be resent on every
// scroll event, each one costing a sequence number and two round trips.
let lastSignature = "";

const link = new ContextLink((s) => {
  if (s.state === "open") say("● 已连接,正在跟随", "ok");
  else if (s.state === "connecting") say("正在连接…");
  else if (s.state === "retrying") {
    say(`✗ 已断开,${Math.round((s.delayMs || 0) / 1000)} 秒后重试`, "err");
    if (s.error) note("断开: " + describe(s.error));
  } else if (s.state === "error") {
    say("✗ 握手失败", "err");
    note("握手: " + describe(s.error));
  }
});

function render(page) {
  els.ctxTitle.textContent = page.title || "(无标题)";
  els.ctxUrl.textContent =
    `${page.url}　·　正文 ${(page.text || "").length} 字` +
    (page.selection ? `　·　选中 ${page.selection.length} 字` : "");
}

async function forward(page) {
  const signature = `${page.url}|${(page.text || "").length}|${page.selection || ""}`;
  if (signature === lastSignature) return;
  lastSignature = signature;

  render(page);
  try {
    const result = await link.send(page);
    if (result?.skipped) note("待连接,已暂存当前页");
  } catch (err) {
    note("上报失败: " + describe(err));
  }
}

// Every page announces itself while it is the one being viewed; background tabs
// stay quiet, so a dozen open tabs cannot argue over what the assistant sees.
if (chrome.runtime?.onMessage) {
  chrome.runtime.onMessage.addListener((message) => {
    if (message?.type === "BW_PAGE_ACTIVE" && message.page) forward(message.page);
    return undefined;
  });
}

// The page that was open when this bridge was started, captured by the popup.
// Without it the bridge would sit blank until the user scrolled or switched.
(async function seed() {
  link.connect();
  try {
    const bag = await chrome.storage.local.get("bwCallContext");
    const ctx = bag?.bwCallContext;
    if (ctx?.url && (!ctx.capturedAt || Date.now() - ctx.capturedAt < 5 * 60 * 1000)) {
      forward(ctx);
    }
  } catch (_) {}
})();

// --- placing a call from here ------------------------------------------------
// Either side may start it, and whoever does holds it: the other end only sends
// context and never asks for audio, so switching between them no longer drops
// the call. Before, both tried to own the one voice link and each switch evicted
// the other.
let voiceActive = false;

function voiceReady() {
  const RC = window.RC;
  if (!RC?.computerVoice?.startFromUserGesture) return "✗ 语音模块未加载";
  // The capture listener consults this first; without rc-voicecall it returns
  // early and no audio lease is ever taken, which only surfaces later as
  // GESTURE_REQUIRED at the moment of dialling.
  try {
    if (RC.voicecall?.canCaptureComputerVoiceGesture?.() !== true) {
      return "✗ 手势许可未通过";
    }
  } catch (err) {
    return "✗ " + describe(err);
  }
  // Rejected silently on any mismatch -- wrong id, wrong type, detached node --
  // so the boolean is checked rather than assumed.
  if (!RC.computerVoice.registerComputerButton(els.btn)) return "✗ 按钮注册被拒";
  return null;
}

if (els.btn) {
  const problem = voiceReady();
  if (problem) {
    els.btn.disabled = true;
    els.btn.textContent = problem;
  }

  els.btn.addEventListener("click", async () => {
    const RC = window.RC;
    els.btn.disabled = true;
    try {
      if (voiceActive) {
        await RC.computerVoice.stop();
        voiceActive = false;
        els.btn.textContent = "开始通话";
        els.btn.classList.remove("stop");
      } else {
        // Carried from the sidebar button, so the page dials the same target
        // the Reader was set to rather than silently defaulting.
        const appKind = new URLSearchParams(location.search).get("app") || "";
        await RC.computerVoice.startFromUserGesture(
          appKind ? { appKind } : {}
        );
        voiceActive = true;
        els.btn.textContent = "结束通话";
        els.btn.classList.add("stop");
      }
    } catch (err) {
      // BUSY is an answer, not a failure: someone else holds the call. Said
      // plainly, because "通话: BW_..._BUSY" reads like a fault when in fact
      // nothing is broken and the user only needs to know where the call is.
      if (err?.code === "BW_COMPUTER_VOICE_DIRECT_BUSY") {
        note("另一端正在通话（App 或阅读器）。在那边挂断后即可在此发起。");
      } else {
        note("通话: " + describe(err));
      }
    }
    els.btn.disabled = false;
  });

  // The bridge can end the call on its own; without this the button would go on
  // claiming it is live.
  window.RC?.computerVoice?.onStatus?.((s) => {
    if (voiceActive && s?.active === false) {
      voiceActive = false;
      els.btn.textContent = "开始通话";
      els.btn.classList.remove("stop");
    }
  });
}

window.addEventListener("pagehide", () => link.close());
