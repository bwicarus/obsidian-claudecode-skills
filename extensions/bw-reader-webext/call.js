"use strict";

// Drives the in-extension call. rc-computer-voice.js already implements the
// whole bridge protocol -- handshake, PCM framing, sequence guard, reconnect --
// and has no DOM dependency at all, so nothing about it needs reimplementing
// here. This file only supplies the page around it: context, a button, and a
// readable account of what happened.
//
// Note what is deliberately absent: __bwReaderFetch. In an injected page that
// global replaces fetch with a proxy through the background worker, which is
// exactly what iOS keeps killing mid-request ("翻译中" forever). Leaving it
// unset makes rc-computer-voice fall back to the page's own fetch, so the
// fragile hop is not merely avoided -- it does not exist on this path.

const els = {
  btn: document.getElementById("btn"),
  status: document.getElementById("status"),
  detail: document.getElementById("detail"),
  ctxTitle: document.getElementById("ctxTitle"),
  ctxUrl: document.getElementById("ctxUrl"),
};

function say(text, cls) {
  els.status.textContent = text;
  els.status.className = cls || "";
}

function detail(lines) {
  els.detail.style.display = "block";
  els.detail.textContent = lines.join("\n");
}

// Errors reach the user through the device screen or not at all -- there is no
// console to open on an iPad. Anything thrown gets flattened rather than shown
// as the useless "[object Object]".
function describe(err) {
  if (!err) return "(no error object)";
  const parts = [];
  if (err.code) parts.push("code=" + err.code);
  if (err.name && err.name !== "Error") parts.push("name=" + err.name);
  if (err.message) parts.push(err.message);
  if (!parts.length) {
    try { return JSON.stringify(err); } catch { return String(err); }
  }
  return parts.join(" | ");
}

// --- self-check -------------------------------------------------------------
// Runs before any user action, because a missing dependency and a refused
// connection are different problems and must not present identically.
function selfCheck() {
  const notes = [];
  const RC = window.RC;

  if (!RC) {
    say("✗ rc-core.js 未加载", "err");
    detail(["window.RC 不存在 —— vendor/rc-core.js 没有被打包进扩展。"]);
    return false;
  }
  notes.push("RC ✓");
  notes.push("RC.ctxSync " + (RC.ctxSync ? "✓" : "缺失(降级)"));
  notes.push("RC.toast " + (RC.toast ? "✓" : "缺失(降级)"));
  notes.push("RC.outgoing " + (RC.outgoing ? "✓" : "缺失(降级)"));

  if (!RC.computerVoice || typeof RC.computerVoice.startFromUserGesture !== "function") {
    say("✗ rc-computer-voice.js 未加载", "err");
    detail(notes.concat(["RC.computerVoice.startFromUserGesture 不可用。"]));
    return false;
  }
  notes.push("RC.computerVoice ✓");

  try {
    const target = RC.computerVoice.getTargetApp && RC.computerVoice.getTargetApp();
    notes.push("目标 App: " + (target || "(默认)"));
  } catch (err) {
    notes.push("目标 App 读取失败: " + describe(err));
  }

  detail(notes);
  return true;
}

// --- page context -----------------------------------------------------------
// The popup captures the active tab and leaves it here. This page has no access
// to the page being read: it is a separate origin, and by the time it opens the
// tab may not even be frontmost.
async function loadContext() {
  try {
    const bag = await chrome.storage.local.get("bwCallContext");
    const ctx = bag && bag.bwCallContext;
    if (!ctx || !ctx.url) return null;

    // Stale context is worse than none: answering about the wrong page is a
    // silent error, whereas an empty card is visibly empty.
    if (ctx.capturedAt && Date.now() - ctx.capturedAt > 5 * 60 * 1000) return null;

    els.ctxTitle.textContent = ctx.title || "(无标题)";
    els.ctxUrl.textContent = ctx.url;
    return ctx;
  } catch {
    return null;
  }
}

// --- call lifecycle ---------------------------------------------------------
let active = false;

async function start() {
  els.btn.disabled = true;
  say("正在连接…");

  try {
    // Must be awaited inside the click handler: iOS grants microphone access
    // only while a user gesture is being processed, and rc-computer-voice
    // requests capture as part of starting.
    await window.RC.computerVoice.startFromUserGesture({});
    active = true;
    els.btn.disabled = false;
    els.btn.textContent = "结束通话";
    els.btn.classList.add("stop");
    say("● 通话中", "ok");
  } catch (err) {
    els.btn.disabled = false;
    say("✗ 连接失败", "err");
    detail([
      describe(err),
      "",
      "若为 BW_COMPUTER_VOICE_* 开头,是桥接协议本身拒绝(Windows 侧可查);",
      "若为网络或 1006,是链路不通(确认 Tailscale 在线、Windows 桥接器在跑)。",
    ]);
  }
}

async function stop() {
  els.btn.disabled = true;
  try {
    await window.RC.computerVoice.stop();
  } catch (err) {
    detail(["停止时报错(通话可能已断): " + describe(err)]);
  }
  active = false;
  els.btn.disabled = false;
  els.btn.textContent = "开始通话";
  els.btn.classList.remove("stop");
  say("已结束");
}

els.btn.addEventListener("click", () => (active ? stop() : start()));

// The bridge can drop the call on its own -- Windows side stopping, network
// loss, sequence guard tripping. Without this the button would keep claiming
// the call is live.
if (window.RC && window.RC.computerVoice && window.RC.computerVoice.onStatus) {
  window.RC.computerVoice.onStatus((s) => {
    if (!s) return;
    if (s.error) detail(["桥接状态: " + describe(s.error)]);
    if (active && s.active === false) {
      active = false;
      els.btn.textContent = "开始通话";
      els.btn.classList.remove("stop");
      say("通话已断开", "err");
    }
  });
}

(async function init() {
  if (!selfCheck()) {
    els.btn.disabled = true;
    return;
  }
  const ctx = await loadContext();
  if (!ctx) {
    els.ctxTitle.textContent = "（未取得网页上下文）";
    els.ctxUrl.textContent = "AI 将听不到当前网页内容";
  }
  say("就绪");
})();
