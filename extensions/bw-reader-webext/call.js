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

// Marks the embedded form. Done here rather than in an inline <script>, which an
// extension page's CSP (script-src 'self') silently refuses -- that refusal is
// why the frame stayed unannounced and every press fell through to a new tab.
const EMBEDDED = new URLSearchParams(location.search).get("compact") === "1";
if (EMBEDDED) document.documentElement.classList.add("compact");

// Short-circuit ctxSync's upload instead of letting it be attempted.
//
// It is issued as a bare "/pdf/api/context-sync", which in this page resolves
// against safari-web-extension://<uuid>/ -- an address that does not exist, and
// Safari reports the miss as "TypeError: Load failed". Dialling then fails with
// that message and no mention of voice, which is what the user saw.
//
// The request is unnecessary here regardless: it feeds the Pi→Windows snapshot
// path, while this page sends context over its own socket. And it could not
// succeed anyway -- it carries credentials:'include' and the extension holds no
// Pi cookie.
//
// The caller checks the reply against what it asked for, so the request body is
// echoed back; a flat refusal fails that check as BW_READER_CONTEXT_MODE_ACK.
//
// This was here before call.js was rewritten as the context bridge and was lost
// in that rewrite.
(function interceptContextSync() {
  if (typeof window.fetch !== "function") return;
  const original = window.fetch.bind(window);
  window.fetch = function (input, init) {
    const url = String((input && input.url) || input || "");
    if (url.indexOf("/pdf/api/context-sync") === -1) return original(input, init);
    let echo = { ok: true };
    try {
      const sent = init && init.body ? JSON.parse(String(init.body)) : {};
      echo = { ok: true, enabled: sent.enabled, deliveryMode: sent.deliveryMode };
    } catch (_) {}
    return Promise.resolve(
      new Response(JSON.stringify(echo), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      })
    );
  };
})();

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

// Forwards a line to the embedding page.
//
// Defined up here rather than inside the embedded block below because note()
// runs long before that block, and the diagnostics worth reading are the early
// ones. Silent and harmless when this page stands on its own.
function frameTell(type, value) {
  if (!EMBEDDED || window.parent === window) return;
  try {
    window.parent.postMessage(
      { contract: "bw-extension-computer-voice-frame/1", type, value: value || null },
      "*"
    );
  } catch (_) {}
}

function note(line) {
  // Compact form hides #detail, so on an iPad this is the only way the reason
  // for a failure ever reaches a human: there is no console to open, and every
  // blind round trip costs a TestFlight build.
  frameTell("log", { line: String(line) });
  els.detail.style.display = "block";
  els.detail.textContent = (els.detail.textContent + "\n" + line).trim().split("\n").slice(-14).join("\n");
}

// Asks the bridge over plain HTTPS why the socket would not open.
//
// A WebSocket that is refused reports nothing useful: the specification denies
// onerror any detail, deliberately, so a 403 and an unreachable host look
// identical from script. The same endpoint answers an ordinary request from the
// same document with the same Origin, and there the status is readable -- 426
// means the door is open and the refusal lies elsewhere, 403 means this
// document's Origin is not on the bridge's list.
//
// Origin is the live question for the embedded form: the list admits
// safari-web-extension:// under any UUID and refuses null outright, and which
// of the two a framed extension document sends is not something reading the
// specification settles.
async function probeEndpoint() {
  // Stated here because rc-computer-voice keeps its endpoints private -- the
  // frozen surface exports no accessor. Diagnostics only; a drift would cost a
  // misleading probe, never a misrouted call.
  const url = "https://bwicarus-2.taile44d0c.ts.net/reader-computer-voice/v1";
  try {
    const res = await fetch(url, { method: "GET", cache: "no-store" });
    note("探测 " + res.status + " @ " + location.origin);
  } catch (err) {
    note("探测失败 " + describe(err) + " @ " + location.origin);
  }
}

function describe(err) {
  if (!err) return "(no error)";
  if (err.code && err.message) return `${err.code} | ${err.message}`;
  return err.message || String(err);
}

const PREFERENCE_KEY = "bwReaderExtensionPreferencesV2";
const CONTEXT_SYNC_KEY = "eph-ctx-sync";
const ACTIVE_CONTEXT_KEY = "bwActivePageContextV1";

// Compared before sending. Without it the same page would be resent on every
// scroll event, each one costing a sequence number and two round trips.
let lastSignature = "";
let lastPage = null;
let contextPreferenceKnown = false;
let contextSyncEnabled = false;
let link = null;

function contextLinkStatus(s) {
  note("链路: " + JSON.stringify(s?.state ?? s));
  if (s.state === "open") say("● 已连接,正在跟随", "ok");
  else if (s.state === "connecting") say("正在连接…");
  else if (s.state === "retrying") {
    say(`✗ 已断开,${Math.round((s.delayMs || 0) / 1000)} 秒后重试`, "err");
    if (s.error) note("断开: " + describe(s.error));
  } else if (s.state === "error") {
    say("✗ 握手失败", "err");
    note("握手: " + describe(s.error));
  }
}

function enabledFromRecord(record) {
  return !!(
    record &&
    record.schema === 2 &&
    record.values &&
    record.values[CONTEXT_SYNC_KEY] === "1"
  );
}

function contextSurfaceVisible() {
  return document.visibilityState !== "hidden";
}

function closeContextLink() {
  const current = link;
  link = null;
  lastSignature = "";
  if (!current) return;
  try { current.close(); } catch (_) {}
}

function ensureContextLink() {
  if (!contextPreferenceKnown || !contextSyncEnabled || !contextSurfaceVisible()) {
    return null;
  }
  if (!link) {
    link = new ContextLink(contextLinkStatus);
    link.connect();
  }
  return link;
}

function applyContextPreference(record) {
  const next = enabledFromRecord(record);
  // A preference read as false and a preference never written look identical
  // once folded into a boolean, yet they call for opposite fixes: one means the
  // switch is off, the other means the switch is being read from the wrong key.
  note(
    "读到偏好: " +
    (record === undefined ? "undefined(未写入)" : JSON.stringify(record)) +
    " → " + next
  );
  const changed = !contextPreferenceKnown || next !== contextSyncEnabled;
  contextPreferenceKnown = true;
  contextSyncEnabled = next;
  if (!changed) return;
  lastSignature = "";
  if (!contextSyncEnabled) {
    closeContextLink();
    say("上下文同步已关闭", "dim");
    return;
  }
  const current = ensureContextLink();
  if (current && lastPage) forward(lastPage, true);
}

function contentDigest(value) {
  const text = String(value || "");
  let hash = 2166136261;
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return (hash >>> 0).toString(16);
}

function render(page) {
  els.ctxTitle.textContent = page.title || "(无标题)";
  els.ctxUrl.textContent =
    `${page.url}　·　正文 ${(page.text || "").length} 字` +
    (page.selection ? `　·　选中 ${page.selection.length} 字` : "");
}

// Last reported gate state, so the report below fires on change rather than on
// every page event -- the same line repeating fifty times would bury the moment
// it changed, which is the only moment that matters.
let lastGateReport = "";
let lastBodySignature = "";
let lastStateSignature = "";

async function forward(page, force) {
  lastPage = page;
  render(page);
  const gate =
    `known=${contextPreferenceKnown} enabled=${contextSyncEnabled} ` +
    `visible=${contextSurfaceVisible()} link=${link ? "yes" : "no"}`;
  if (gate !== lastGateReport) {
    lastGateReport = gate;
    // States, not a failure. Three conditions guard this path and from the
    // outside they are indistinguishable: nothing is sent, nothing is said, and
    // which one stopped it cannot be told apart. On a device with no console
    // that difference costs a build to learn, so it is stated up front.
    note("同步门: " + gate);
  }
  if (!contextPreferenceKnown || !contextSyncEnabled || !contextSurfaceVisible()) return;
  const current = ensureContextLink();
  if (!current) return;
  const signature = `${page.url}|${page.title || ""}|${contentDigest(page.text)}|${contentDigest(page.selection)}`;
  if (!force && signature === lastSignature) return;

  try {
    const result = await current.send(page);
    if (result?.skipped) note("待连接,已暂存当前页");
    else if (result?.ok) lastSignature = signature;
  } catch (err) {
    note("上报失败: " + describe(err));
    if (lastSignature === signature) lastSignature = "";
  }
}

function storedPage(value) {
  if (!value || value.schema !== 1 || !value.page) return null;
  const age = Date.now() - Number(value.capturedAt || 0);
  if (!Number.isFinite(age) || age < 0 || age > 5 * 60 * 1000) return null;
  return value.page.url ? value.page : null;
}

// Every page announces itself while it is the one being viewed; background tabs
// stay quiet, so a dozen open tabs cannot argue over what the assistant sees.
// Snapshots handed over directly by the content script in the hosting page.
//
// Same document tree, no process boundary, no background worker to be reclaimed
// and no storage relay to fall out of step. This is the path that carries the
// page now; the runtime message below stays as a fallback for surfaces that
// have no frame of their own.
// Reports from inside the frame, shown by the host page.
//
// Everything this file says goes through note() to the host's own status line,
// which the page-level probe never displays -- so this entire leg has been
// mute. The page reported "delivered to frame" and the bridge saw nothing, with
// nothing in between to say why. Ninth time tonight the failing link also
// swallowed the report of its own failure.
function frameProbe(text) {
  // Same channel as the page, tagged with who is speaking. Loaded as a content
  // script into the hosting page, the shared helper is not reachable from
  // inside this document, so the wire format is written out here -- it is one
  // message shape, kept in step with src/bw-probe.js.
  try {
    window.parent.postMessage(
      { contract: "bw-probe/1", where: "frame", text: String(text) },
      "*"
    );
  } catch (_) {}
}

window.addEventListener("message", (event) => {
  const d = event.data;
  if (!d || d.contract !== "bw-page-context/1" || d.type !== "page") return;
  if (!d.page || typeof d.page !== "object") return;
  frameProbe("框收到页面: " + String(d.page.url || "").slice(0, 50));
  forwardDirect(d.page);
});

// The direct path, deliberately not guarded by the preference gates.
//
// forward() refuses to send unless the preference has been read and is true.
// That was reasonable when the preference decided whether to sync at all, but
// it also meant a preference that could not be read -- for any reason, in any
// layer -- silently disabled the whole link, and the switch the user actually
// toggled was never the thing consulted. A page handed to us by the content
// script in our own document is intent enough.
//
// Document visibility is still honoured: background tabs stay quiet, so a dozen
// open tabs cannot argue over what the assistant is looking at.
// One-shot delivery. No socket, no handshake, no session.
//
// A page's context is used once and thrown away: collect, send, done. It was
// travelling over the voice link's machinery -- WebSocket, hello, context-open,
// session id, reconnect backoff -- all of which exists to keep a conversation
// alive across time, and none of which this needs.
//
// The cost was not complexity but reach: a socket has to be held open by a
// document that stays alive, and on iOS every extension document is short-lived.
// That made "which document can hold the connection" the central problem for an
// evening. For a POST it is not a problem at all -- the frame only has to exist
// at the instant of sending, and it is recreated with every page.
const SNAPSHOT_POST_URL =
  "https://bwicarus-2.taile44d0c.ts.net/reader-context/snapshot";

function randomHex(length) {
  const hex = "0123456789abcdef";
  let out = "";
  for (let i = 0; i < length; i += 1) out += hex[(Math.random() * 16) | 0];
  return out;
}

// Remembers when each page's body was last actually sent, so a repeat can say
// when rather than say nothing.
const bodySentAt = new Map();

function clockText(ms) {
  const d = new Date(ms);
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

// `bodyIsRepeat` marks a page whose text was already delivered.
//
// Position and body are different kinds of fact and were being treated as one.
// Where the user is looking is state: it has to be true right now, every time.
// The body is content: once given, repeating it only crowds the assistant's
// context. Skipping the whole report to avoid the repetition made the position
// stale too, and a stale position does not read as "unchanged" -- it reads as
// "still here", which is a different claim and sometimes a false one.
//
// So a repeat still reports, with the body replaced by a note saying when the
// real one was sent. The assistant then knows both that the user is still on
// this page and that it has already seen its contents.
async function postSnapshot(page, bodyIsRepeat) {
  const url = String(page.url || "");
  let text = String(page.text || "").slice(0, 12000);
  let repeatNote = null;
  if (bodyIsRepeat) {
    const at = bodySentAt.get(url);
    repeatNote = at
      ? `（正文与 ${clockText(at)} 送出的相同，未重复发送）`
      : "（正文与此前送出的相同，未重复发送）";
    text = repeatNote;
  } else {
    bodySentAt.set(url, Date.now());
    // Bounded: one entry per page visited, cleared oldest-first.
    if (bodySentAt.size > 60) {
      bodySentAt.delete(bodySentAt.keys().next().value);
    }
  }
  const selection = String(page.selection || "").slice(0, 400);
  const body = {
    event: {
      v: 1,
      seq: 1,
      type: "page.context",
      event: "page.context",
      ts: Math.floor(Date.now() / 1000),
      id: randomHex(16),
      stable: true,
      book_id: url,
      file: url,
      page: 0,
      title: String(page.title || ""),
      kind: "web",
      text_available: !!text,
      page_context: {
        text,
        text_available: !!text,
        text_source: bodyIsRepeat ? "extension-page-repeat" : "extension-page",
        fallback_reason: bodyIsRepeat
          ? "正文此前已送出，本次仅更新阅读位置"
          : (text ? null : "扩展未取得正文"),
        truncated: String(page.text || "").length > 12000,
        reason: "active",
        visual: null,
        embeds: { highlights: 0, blocks: 0, unanchored: [] },
      },
    },
    // Nested under `active` with its own shape -- sent flat the bridge refuses
    // it, and because the context half succeeds first, the refusal would be
    // invisible: the snapshot updates while the reading position silently does
    // not.
    active: {
      kind: "web",
      file: url,
      title: String(page.title || ""),
      page: 0,
      selectionState: selection ? "active" : "cleared",
      selection: selection || null,
      observedAtEpochMs: Date.now(),
    },
  };
  const response = await fetch(SNAPSHOT_POST_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    cache: "no-store",
  });
  if (!response.ok) {
    let detail = "";
    try { detail = (await response.text()).slice(0, 200); } catch (_) {}
    throw new Error(`HTTP ${response.status}${detail ? " " + detail : ""}`);
  }
}

async function forwardDirect(page) {
  lastPage = page;
  render(page);
  if (!contextSurfaceVisible()) {
    frameProbe("框: 文档不可见,跳过");
    return;
  }
  // Two signatures, because they answer two different questions.
  const bodySig = `${page.url}|${contentDigest(page.text)}`;
  const stateSig =
    `${page.url}|${page.title || ""}|${contentDigest(page.selection)}`;
  const bodyIsRepeat = bodySig === lastBodySignature;
  // Nothing at all has changed -- not the page, not the selection, not the
  // text. Only then is silence honest.
  if (bodyIsRepeat && stateSig === lastStateSignature) {
    frameProbe("框: 位置与内容均未变,跳过");
    return;
  }
  try {
    frameProbe(bodyIsRepeat ? "框: 开始 POST(仅位置)" : "框: 开始 POST");
    await postSnapshot(page, bodyIsRepeat);
    lastBodySignature = bodySig;
    lastStateSignature = stateSig;
    frameProbe(bodyIsRepeat ? "框: POST 成功(正文标注为重复)" : "框: POST 成功");
  } catch (err) {
    // Said out loud. Every silent failure in this chain cost a build to find.
    note("快照投递失败: " + describe(err));
    frameProbe("框: POST 失败 " + describe(err));
    if (lastSignature === signature) lastSignature = "";
  }
}

if (chrome.runtime?.onMessage) {
  chrome.runtime.onMessage.addListener((message) => {
    // Sent by whichever page the user is looking at. The page only forwards a
    // message; this side owns the connection and the protocol. That division is
    // what made it work in 1.0.25, and what the later attempt gave up by having
    // each page open its own socket.
    if (message?.type === "BW_PAGE_ACTIVE" && message.page) forward(message.page, false);
    return undefined;
  });
}

// Reads extension storage under either API shape.
//
// Safari's chrome.* surface is not uniformly promise-based: chrome.storage
// .local.get may take a callback and return undefined instead of a promise.
// `await` on that yields undefined rather than throwing, so the failure was
// invisible twice over -- the catch never fired, and the caller went on to
// treat "no data" as "the user turned sync off". Both shapes are handled here,
// and a genuine failure now rejects so it can be reported.
function storageGet(keys) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const done = (value) => { if (!settled) { settled = true; resolve(value); } };
    const fail = (err) => { if (!settled) { settled = true; reject(err); } };
    try {
      const returned = chrome.storage.local.get(keys, (bag) => {
        const err = chrome.runtime?.lastError;
        if (err) fail(new Error(err.message || "storage.get 失败"));
        else done(bag);
      });
      if (returned && typeof returned.then === "function") returned.then(done, fail);
    } catch (err) {
      fail(err);
    }
    // Neither shape answered. Better a stated timeout than silence.
    window.setTimeout(() => fail(new Error("storage.get 无响应")), 5000);
  });
}

// The page that was open when this bridge was started, captured by the popup.
// Without it the bridge would sit blank until the user scrolled or switched.
(async function seed() {
  try {
    const bag = await storageGet([
      PREFERENCE_KEY,
      ACTIVE_CONTEXT_KEY,
      "bwCallContext",
    ]);
    applyContextPreference(bag?.[PREFERENCE_KEY]);
    const persisted = storedPage(bag?.[ACTIVE_CONTEXT_KEY]);
    const ctx = bag?.bwCallContext;
    if (persisted) {
      forward(persisted, true);
    } else if (ctx?.url && (!ctx.capturedAt || Date.now() - ctx.capturedAt < 5 * 60 * 1000)) {
      forward(ctx, true);
    } else {
      els.ctxTitle.textContent = "上下文由各网页自行上报";
      els.ctxUrl.textContent = "本页只负责通话";
    }
  } catch (err) {
    // Unknown, not off.
    //
    // Declaring sync disabled here was the trap: a preference that could not be
    // read is not a preference set to false, yet this permanently silenced the
    // link -- and the user turning the switch on changed nothing, because the
    // switch's value was never the thing being consulted. Leaving it unknown
    // lets the retry below, and storage.onChanged, still recover.
    contextPreferenceKnown = false;
    say("上下文同步设置读取失败,正在重试", "err");
    // Compact form hides #status, so on an iPad this is the only way the reason
    // travels. Silence here cost a full evening of guessing.
    note("设置读取失败: " + describe(err));
    window.setTimeout(() => { seed(); }, 3000);
  }
})();

if (chrome.storage?.onChanged) {
  chrome.storage.onChanged.addListener((changes, areaName) => {
    if (areaName !== "local" || !changes) return;
    if (changes[PREFERENCE_KEY]) {
      applyContextPreference(changes[PREFERENCE_KEY].newValue);
    }
    if (changes[ACTIVE_CONTEXT_KEY]) {
      const page = storedPage(changes[ACTIVE_CONTEXT_KEY].newValue);
      if (page) forward(page, true);
    }
  });
}

function resumeContextLink() {
  if (!contextSurfaceVisible()) {
    closeContextLink();
    return;
  }
  const current = ensureContextLink();
  if (current && lastPage) forward(lastPage, true);
}

document.addEventListener("visibilitychange", resumeContextLink, { passive: true });
window.addEventListener("pageshow", resumeContextLink, { passive: true });
window.addEventListener("focus", resumeContextLink, { passive: true });
window.addEventListener("online", resumeContextLink, { passive: true });

// --- placing a call from here ------------------------------------------------
// Either side may start it, and whoever does holds it: the other end only sends
// context and never asks for audio, so switching between them no longer drops
// the call. Before, both tried to own the one voice link and each switch evicted
// the other.
let voiceActive = false;
// Set by the host through configure when embedded; otherwise from the URL.
let frameAppKind = "";

// Publish the call state so every page's sidebar button can show it.
//
// The button that opened this page has no idea what happened next: it lives in
// another document and hears nothing back. Writing the state where any page can
// read it is what lets the button stop looking idle during a call.
function publishVoiceState(state, detail) {
  try {
    chrome.storage.local.set({
      bwVoiceState: { state, detail: detail || "", at: Date.now() },
    });
  } catch (_) {}
}

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
    // Left pressable on purpose.
    //
    // Disabling it made the reason unreachable: this button is the only surface
    // the user has, the detail panel is hidden in compact form, and a control
    // that refuses to respond looks identical to one that is broken. Pressing
    // it now states the reason instead, which on an iPad is the only way the
    // reason travels at all.
    els.btn.textContent = problem;
    els.btn.classList.add("stop");
    note("未就绪: " + problem);
  }

  els.btn.addEventListener("click", async () => {
    const RC = window.RC;
    const stillUnready = voiceReady();
    if (stillUnready) {
      note("未就绪: " + stillUnready);
      probeEndpoint();
      return;
    }
    els.btn.disabled = true;
    try {
      if (voiceActive) {
        await RC.computerVoice.stop();
        // Confirmed rather than assumed: a stop that is merely sent, and then
        // reported as done, is how a call goes on running on Windows while the
        // button says otherwise.
        const stillActive = (() => {
          try { return RC.computerVoice.isActive() === true; } catch (_) { return false; }
        })();
        voiceActive = stillActive;
        if (stillActive) {
          note("停止已发出，但桥接仍报通话中 —— 请再按一次。");
          publishVoiceState("stopping");
        } else {
          els.btn.textContent = "开始通话";
          els.btn.classList.remove("stop");
          publishVoiceState("idle");
        }
      } else {
        // Carried from the sidebar button, so the page dials the same target
        // the Reader was set to rather than silently defaulting.
        const appKind = frameAppKind || new URLSearchParams(location.search).get("app") || "";
        await RC.computerVoice.startFromUserGesture(
          appKind ? { appKind } : {}
        );
        voiceActive = true;
        els.btn.textContent = "结束通话";
        els.btn.classList.add("stop");
        publishVoiceState("active");
      }
    } catch (err) {
      // BUSY is an answer, not a failure: someone else holds the call. Said
      // plainly, because "通话: BW_..._BUSY" reads like a fault when in fact
      // nothing is broken and the user only needs to know where the call is.
      if (err?.code === "BW_COMPUTER_VOICE_DIRECT_BUSY") {
        note("另一端正在通话（App 或阅读器）。在那边挂断后即可在此发起。");
      } else {
        note("通话: " + describe(err));
        probeEndpoint();
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
      publishVoiceState("idle");
      // The page exists for the call. Once the call is over it is a stray tab,
      // and a stray tab is how the user ends up with several of them.
      //
      // Briefly delayed so the reason stays readable when the call ended in a
      // failure -- closing instantly would take the explanation with it.
      if (!EMBEDDED) closeWhenDone(s?.error ? 4000 : 600);
    }
  });

  // Dial on arrival: the sidebar press was the user's decision, and making them
  // press a second time here is asking for the same consent twice.
  //
  // Safari does not always carry gesture activation across a newly opened
  // document, so a refusal is expected and not an error -- the button stays and
  // one press completes it.
  (async function autoStart() {
    // Only when this page was opened for the call. Embedded over the sidebar
    // button it is present from the moment the page loads, and dialling then
    // would place a call nobody asked for.
    if (EMBEDDED) return;
    if (voiceReady()) return;
    els.btn.disabled = true;
    els.btn.textContent = "正在连接…";
    try {
      const appKind = frameAppKind || new URLSearchParams(location.search).get("app") || "";
      await window.RC.computerVoice.startFromUserGesture(appKind ? { appKind } : {});
      voiceActive = true;
      els.btn.textContent = "结束通话";
      els.btn.classList.add("stop");
      publishVoiceState("active");
    } catch (err) {
      els.btn.textContent = "开始通话";
      publishVoiceState("idle");
      if (err?.code === "BW_COMPUTER_VOICE_GESTURE_REQUIRED") {
        note("请点击上方按钮开始通话（本页需要一次点击才能取得麦克风）。");
      } else if (err?.code === "BW_COMPUTER_VOICE_DIRECT_BUSY") {
        note("另一端正在通话（App 或阅读器）。在那边挂断后即可在此发起。");
      } else {
        note("自动连接失败: " + describe(err));
      }
    }
    els.btn.disabled = false;
  })();
}

// Closing is only allowed for a window that script opened -- which this one is,
// via the sidebar button. When that does not hold (a manually opened tab), the
// page stays and says so rather than appearing stuck.
function closeWhenDone(delayMs) {
  setTimeout(() => {
    try {
      window.close();
    } catch (_) {}
    // Still here a moment later means the browser refused to close it.
    setTimeout(() => {
      if (!document.hidden) say("通话已结束，可关闭本页", "dim");
    }, 300);
  }, Math.max(0, delayMs || 0));
}

// --- embedded form -----------------------------------------------------------
// When framed over the sidebar button, tell the host it is usable.
//
// The host keeps the frame invisible and click-through until this arrives, so
// that a frame which failed to load never swallows the press -- the click falls
// to the original button and a tab opens instead. Silence here is precisely the
// fallback, which is why 1.0.55 always opened a tab: this page never announced
// itself, having been written before it was ever embedded.
const FRAME_CONTRACT = "bw-extension-computer-voice-frame/1";
const embedded = EMBEDDED;

if (embedded && window.parent !== window) {
  const tell = frameTell;

  // The host may set which desktop app to dial before the first press.
  window.addEventListener("message", (event) => {
    if (event.source !== window.parent) return;
    const d = event.data;
    if (!d || d.contract !== FRAME_CONTRACT || d.type !== "configure") return;
    if (d.appKind) frameAppKind = String(d.appKind);
  });

  // Announced only once the button is genuinely operable: voiceReady() has
  // passed and the button is registered. Claiming readiness earlier would take
  // the click and then be unable to act on it.
  const problem = els.btn ? voiceReady() : "✗ 无按钮";
  tell("ready", { ok: !problem });
  if (problem) {
    note("内嵌未就绪: " + problem);
    // Also as a state message: the host writes those into the button's title,
    // which is the only place the reason can be seen. Inside a 42px frame that
    // stays invisible on failure, nothing else is readable.
    tell("state", { state: "failed", message: "内嵌未就绪: " + problem });
  }

  window.RC?.computerVoice?.onStatus?.((s) => {
    tell("state", { state: s?.state || "", message: s?.message || "" });
  });
}

window.addEventListener("pagehide", closeContextLink);
