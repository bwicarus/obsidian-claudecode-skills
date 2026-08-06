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

const VISUAL_DELIVERY_CONTRACT = "reader-visual-delivery/2";
const LOCAL_VISUAL_CONTRACT = "bw-reader-visual-local/1";
const BROWSER_CONTROL_CONTRACT = "reader-browser-control/1";
const LOCAL_BROWSER_CONTROL_CONTRACT = "bw-browser-control/1";
const VISUAL_CHUNK_CHARACTERS = 48000;
const VISUAL_MAX_BYTES = 786432;
const VISUAL_MAX_CHUNKS = 24;

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
const CONTEXT_SYNC_MIRROR_KEY = "bwCtxSyncMirrorV1";
const VISUAL_BINDING_SIGNAL_KEY = "bwReaderVisualBindingsV1";

let contextPreferenceKnown = false;
let contextSyncEnabled = false;
let contextMirrorKnown = false;

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

function applyContextPreference(record, mirrorValue) {
  const hasMirror = typeof mirrorValue === "boolean";
  if (!hasMirror && contextMirrorKnown) return;
  if (hasMirror) contextMirrorKnown = true;
  const next = hasMirror ? mirrorValue : enabledFromRecord(record);
  // A preference read as false and a preference never written look identical
  // once folded into a boolean, yet they call for opposite fixes: one means the
  // switch is off, the other means the switch is being read from the wrong key.
  note(
    "读到偏好: " +
    (hasMirror
      ? "跨站镜像=" + String(mirrorValue)
      : record === undefined ? "undefined(未写入)" : JSON.stringify(record)) +
    " → " + next
  );
  const changed = !contextPreferenceKnown || next !== contextSyncEnabled;
  contextPreferenceKnown = true;
  contextSyncEnabled = next;
  if (!changed) return;
  if (!contextSyncEnabled) {
    closeVisualLink();
    say("上下文同步已关闭", "dim");
  }
}

let contextWasVisible = contextSurfaceVisible();

function storedVisualContext(value) {
  if (!value || value.schema !== 1 || !exactKeys(value, [
    "schema", "capturedAt", "identity", "visual", "viewKey", "snapshotRevision",
  ])) return null;
  const age = Date.now() - Number(value.capturedAt || 0);
  if (!Number.isFinite(age) || age < 0 || age > 5 * 60 * 1000) return null;
  const identity = value.identity;
  if (
    !exactKeys(identity, ["sourceInstanceId", "file", "page"]) ||
    !/^[A-Za-z0-9_-]{22}$/.test(String(identity.sourceInstanceId || "")) ||
    !/^https?:\/\//.test(String(identity.file || "")) || identity.file.length > 4096 ||
    identity.page !== 0 || typeof value.viewKey !== "string" ||
    value.viewKey.length < 1 || value.viewKey.length > 160 ||
    !Number.isSafeInteger(value.snapshotRevision) || value.snapshotRevision < 1
  ) return null;
  return {
    url: identity.file,
    title: "",
    viewKey: value.viewKey,
    visual: value.visual,
    document: { sourceInstanceId: identity.sourceInstanceId },
    snapshotRevision: value.snapshotRevision,
  };
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

// The only parent->frame security bootstrap. It carries a one-time random
// capability, never page data or a business success result. background.js
// consumes it once and binds this exact tab/frame/document before any visual or
// browser-control request is accepted.
window.addEventListener("message", (event) => {
  if (event.source !== window.parent) return;
  const message = event.data;
  if (!exactKeys(message, ["contract", "type", "capability"]) ||
      message.contract !== "bw-reader-call-claim/1" || message.type !== "claim" ||
      !/^[A-Za-z0-9_-]{43}$/.test(String(message.capability || ""))) return;
  runtimeRequest({
    type: "BW_READER_CALL_CLAIM_BIND",
    capability: message.capability,
  }, 5000).then((reply) => {
    if (reply?.ok !== true || reply.data?.bound !== true ||
        !["codex-desktop", "chatgpt-classic"].includes(reply.data?.appKind)) {
      throw new Error("后台拒绝绑定通话页");
    }
    frameAppKind = reply.data.appKind;
    frameProbe("通话页认领: 已绑定当前 frame");
    return refreshVisualContext();
  }).catch((err) => frameProbe("通话页认领失败: " + describe(err)));
});

function exactKeys(value, keys) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const actual = Object.keys(value).sort();
  const expected = keys.slice().sort();
  return actual.length === expected.length &&
    actual.every((key, index) => key === expected[index]);
}

function safeVisualId(value, nullable) {
  if (nullable && value === null) return null;
  const text = String(value || "");
  return /^[A-Za-z0-9._:-]{1,160}$/.test(text) ? text : null;
}

function validVisualPage(value) {
  return (
    Number.isSafeInteger(value) && value >= 0
  ) || (
    typeof value === "string" &&
    value.length >= 1 && value.length <= 256 &&
    !/[\u0000-\u001f\u007f]/.test(value)
  );
}

function normalizeVisualEvent(message) {
  if (!exactKeys(message, ["contract", "type", "event", "payload"])) return null;
  if (
    message.contract !== "reader-computer-voice-direct/1" ||
    message.type !== "event" ||
    message.event !== "reader-visual-request"
  ) return null;
  const p = message.payload;
  if (!exactKeys(p, [
    "contract", "commandKind", "correlation", "sourceInstanceId",
    "snapshotRevision", "file", "page", "drawingRevision", "scope",
    "selectionId", "maxBytes", "chunkCharacters",
  ])) return null;
  const correlation = safeVisualId(p.correlation, false);
  const sourceInstanceId = safeVisualId(p.sourceInstanceId, false);
  const drawingRevision = safeVisualId(p.drawingRevision, true);
  const selectionId = safeVisualId(p.selectionId, true);
  if (
    p.contract !== VISUAL_DELIVERY_CONTRACT ||
    p.commandKind !== "capture-composite" ||
    !correlation || !sourceInstanceId ||
    !Number.isSafeInteger(p.snapshotRevision) || p.snapshotRevision < 0 ||
    typeof p.file !== "string" || p.file.length < 1 || p.file.length > 4096 ||
    /[\u0000-\u001f\u007f]/.test(p.file) ||
    !validVisualPage(p.page) ||
    (p.drawingRevision !== null && !drawingRevision) ||
    !["viewport-context", "drawing-nearby", "selection-near"].includes(p.scope) ||
    (p.selectionId !== null && !selectionId) ||
    (p.scope === "selection-near") !== !!selectionId ||
    p.maxBytes !== VISUAL_MAX_BYTES ||
    p.chunkCharacters !== VISUAL_CHUNK_CHARACTERS
  ) return null;
  return {
    contract: VISUAL_DELIVERY_CONTRACT,
    commandKind: "capture-composite",
    correlation,
    sourceInstanceId,
    snapshotRevision: p.snapshotRevision,
    file: p.file,
    page: p.page,
    drawingRevision,
    scope: p.scope,
    selectionId,
    maxBytes: p.maxBytes,
    chunkCharacters: p.chunkCharacters,
  };
}

let visualLink = null;
let visualSourceInstanceId = "";
let visualPage = null;
let visualCommitted = null;
let activeVisualContext = null;
let visualQueue = Promise.resolve();
let browserControlQueue = Promise.resolve();

function closeVisualLink() {
  const current = visualLink;
  visualLink = null;
  visualSourceInstanceId = "";
  visualPage = null;
  visualCommitted = null;
  if (!current) return;
  try { current.close(); } catch (_) {}
}

function visualLinkStatus(status) {
  if (status?.state === "error" || status?.state === "retrying") {
    frameProbe("视觉链路: " + status.state +
      (status.error ? " " + describe(status.error) : ""));
  }
}

function localVisualCapture(request) {
  return runtimeRequest({
    type: "BW_READER_VISUAL_CAPTURE",
    request,
    expectedViewKey: visualCommitted?.viewKey || "",
  }, 20000).then((reply) => {
    const data = reply?.ok === true ? reply.data : null;
    if (!data || !exactKeys(data, [
      "contract", "type", "correlation", "sourceInstanceId",
      "status", "mimeType", "b64",
    ]) || data.contract !== LOCAL_VISUAL_CONTRACT ||
      data.type !== "capture-response" || data.correlation !== request.correlation ||
      data.sourceInstanceId !== request.sourceInstanceId ||
      !["ready", "unavailable"].includes(data.status) ||
      typeof data.mimeType !== "string" || typeof data.b64 !== "string"
    ) return null;
    return data;
  }, () => null);
}

function visualChunkFields(request, fields) {
  return Object.assign({
    sessionId: visualLink?.sessionId || "",
    correlation: request.correlation,
    sourceInstanceId: request.sourceInstanceId,
    snapshotRevision: request.snapshotRevision,
    file: request.file,
    page: request.page,
    drawingRevision: request.drawingRevision,
    scope: request.scope,
    selectionId: request.selectionId,
  }, fields);
}

async function sendVisualUnavailable(request) {
  if (!visualLink) return;
  await visualLink.sendRequest("reader-visual", visualChunkFields(request, {
    status: "unavailable",
    mimeType: "",
    chunkIndex: 0,
    chunkCount: 0,
    totalBytes: 0,
    data: "",
  }));
}

function decodeVisualBase64(value) {
  const b64 = String(value || "");
  if (!b64 || b64.length % 4 !== 0 || !/^[A-Za-z0-9+/]+={0,2}$/.test(b64)) return null;
  try {
    const binary = atob(b64);
    if (btoa(binary) !== b64 || binary.length < 3) return null;
    if (
      binary.charCodeAt(0) !== 0xff ||
      binary.charCodeAt(1) !== 0xd8 ||
      binary.charCodeAt(2) !== 0xff
    ) return null;
    return { b64, totalBytes: binary.length };
  } catch (_) {
    return null;
  }
}

async function deliverVisualRequest(request) {
  if (
    !visualLink || !visualLink.sessionId ||
    request.sourceInstanceId !== visualSourceInstanceId
  ) return;
  if (
    !visualCommitted ||
    request.snapshotRevision !== visualCommitted.snapshotRevision ||
    request.sourceInstanceId !== String(visualPage?.document?.sourceInstanceId || "") ||
    request.file !== String(visualPage?.url || "") ||
    request.sourceInstanceId !== visualCommitted.sourceInstanceId ||
    request.file !== visualCommitted.file ||
    String(visualPage?.viewKey || "") !== visualCommitted.viewKey ||
    (visualPage?.visual?.drawing?.drawingRevision || null) !==
      visualCommitted.drawingRevision
  ) {
    await sendVisualUnavailable(request);
    return;
  }
  const capture = await localVisualCapture(request);
  if (
    !visualCommitted ||
    request.snapshotRevision !== visualCommitted.snapshotRevision ||
    request.sourceInstanceId !== String(visualPage?.document?.sourceInstanceId || "") ||
    request.file !== String(visualPage?.url || "") ||
    request.sourceInstanceId !== visualCommitted.sourceInstanceId ||
    request.file !== visualCommitted.file ||
    String(visualPage?.viewKey || "") !== visualCommitted.viewKey ||
    (visualPage?.visual?.drawing?.drawingRevision || null) !==
      visualCommitted.drawingRevision
  ) {
    await sendVisualUnavailable(request);
    return;
  }
  const decoded = capture?.status === "ready" && capture?.mimeType === "image/jpeg"
    ? decodeVisualBase64(capture.b64)
    : null;
  if (
    !decoded || decoded.totalBytes > request.maxBytes ||
    Math.ceil(decoded.b64.length / request.chunkCharacters) > VISUAL_MAX_CHUNKS
  ) {
    await sendVisualUnavailable(request);
    return;
  }
  const chunkCount = Math.ceil(decoded.b64.length / request.chunkCharacters);
  for (let index = 0; index < chunkCount; index += 1) {
    await visualLink.sendRequest("reader-visual", visualChunkFields(request, {
      status: "chunk",
      mimeType: "image/jpeg",
      chunkIndex: index,
      chunkCount,
      totalBytes: decoded.totalBytes,
      data: decoded.b64.slice(
        index * request.chunkCharacters,
        (index + 1) * request.chunkCharacters
      ),
    }));
  }
}

function handleVisualBridgeEvent(message) {
  const request = normalizeVisualEvent(message);
  if (!request || request.sourceInstanceId !== visualSourceInstanceId) return;
  visualQueue = visualQueue
    .then(() => deliverVisualRequest(request))
    .catch((err) => frameProbe("视觉回传失败: " + describe(err)));
}

function normalizeBrowserControlEvent(message) {
  if (!exactKeys(message, ["contract", "type", "event", "payload"])) return null;
  if (
    message.contract !== "reader-computer-voice-direct/1" ||
    message.type !== "event" ||
    message.event !== "reader-browser-control-request"
  ) return null;
  const p = message.payload;
  if (!exactKeys(p, [
    "contract", "commandKind", "correlation", "sourceInstanceId",
    "snapshotRevision", "file", "page", "action", "target", "selectionId",
  ])) return null;
  const correlation = safeVisualId(p.correlation, false);
  const sourceInstanceId = safeVisualId(p.sourceInstanceId, false);
  const selectionId = safeVisualId(p.selectionId, true);
  const actions = [
    "next-viewport", "previous-viewport", "scroll-to-text",
    "scroll-to-heading", "scroll-to-selection",
  ];
  const textAction = p.action === "scroll-to-text" || p.action === "scroll-to-heading";
  const selectionAction = p.action === "scroll-to-selection";
  if (
    p.contract !== BROWSER_CONTROL_CONTRACT ||
    p.commandKind !== "browser-control" ||
    !correlation || !sourceInstanceId ||
    !Number.isSafeInteger(p.snapshotRevision) || p.snapshotRevision < 0 ||
    typeof p.file !== "string" || p.file.length < 1 || p.file.length > 4096 ||
    /[\u0000-\u001f\u007f]/.test(p.file) ||
    !validVisualPage(p.page) ||
    !actions.includes(p.action) ||
    (textAction
      ? typeof p.target !== "string" || !p.target.trim() || p.target.length > 320
      : p.target !== null) ||
    (selectionAction ? !selectionId : p.selectionId !== null)
  ) return null;
  return {
    contract: BROWSER_CONTROL_CONTRACT,
    commandKind: "browser-control",
    correlation,
    sourceInstanceId,
    snapshotRevision: p.snapshotRevision,
    file: p.file,
    page: p.page,
    action: p.action,
    target: textAction ? p.target.trim() : null,
    selectionId: selectionAction ? selectionId : null,
  };
}

function localBrowserControl(request, committedViewKey) {
  const local = {
    contract: LOCAL_BROWSER_CONTROL_CONTRACT,
    type: "request",
    requestId: request.correlation,
    sourceInstanceId: request.sourceInstanceId,
    action: request.action,
  };
  if (request.target !== null) local.target = request.target;
  if (request.selectionId !== null) local.selectionId = request.selectionId;
  return runtimeRequest({
    type: "BW_READER_BROWSER_CONTROL",
    request: local,
    snapshotRevision: request.snapshotRevision,
    file: request.file,
    page: request.page,
    expectedViewKey: committedViewKey,
  }, 10000).then((reply) => {
    const data = reply?.ok === true ? reply.data : null;
    if (!data || data.contract !== LOCAL_BROWSER_CONTROL_CONTRACT ||
      data.type !== "result" || data.requestId !== local.requestId ||
      data.sourceInstanceId !== local.sourceInstanceId ||
      data.action !== local.action || typeof data.ok !== "boolean"
    ) return null;
    return data;
  }, () => null);
}

function fallbackBrowserState() {
  return {
    scrollX: 0,
    scrollY: 0,
    url: String(visualPage?.url || ""),
    title: String(visualPage?.title || "").slice(0, 1024),
  };
}

async function deliverBrowserControlRequest(request) {
  if (
    !visualLink || !visualLink.sessionId ||
    request.sourceInstanceId !== visualSourceInstanceId
  ) return;
  const committed = visualCommitted;
  let result = null;
  if (
    committed && request.snapshotRevision === committed.snapshotRevision &&
    request.sourceInstanceId === committed.sourceInstanceId &&
    request.file === committed.file && request.page === committed.page &&
    request.sourceInstanceId === String(visualPage?.document?.sourceInstanceId || "") &&
    request.file === String(visualPage?.url || "") &&
    String(visualPage?.viewKey || "") === committed.viewKey
  ) result = await localBrowserControl(request, committed.viewKey);
  const state = result?.ok === true && result.state
    ? result.state
    : fallbackBrowserState();
  const status = result?.ok === true
    ? "success"
    : result?.error?.code === "BW_BROWSER_CONTROL_TARGET_NOT_FOUND"
      ? "not-found"
      : "rejected";
  await visualLink.sendRequest("reader-browser-control", {
    sessionId: visualLink.sessionId,
    correlation: request.correlation,
    sourceInstanceId: request.sourceInstanceId,
    snapshotRevision: request.snapshotRevision,
    file: request.file,
    page: request.page,
    action: request.action,
    status,
    scrollX: Number.isFinite(Number(state.scrollX)) ? Number(state.scrollX) : 0,
    scrollY: Number.isFinite(Number(state.scrollY)) ? Number(state.scrollY) : 0,
    url: /^https?:\/\//.test(String(state.url || ""))
      ? String(state.url).slice(0, 4096)
      : String(visualPage?.url || ""),
    title: String(state.title || "").replace(/[\u0000-\u001f\u007f]/g, " ").slice(0, 1024),
  });
}

function handleContextBridgeEvent(message) {
  if (message?.event === "reader-visual-request") {
    handleVisualBridgeEvent(message);
    return;
  }
  const request = normalizeBrowserControlEvent(message);
  if (!request || request.sourceInstanceId !== visualSourceInstanceId) return;
  browserControlQueue = browserControlQueue
    .then(() => deliverBrowserControlRequest(request))
    .catch((err) => frameProbe("浏览控制回传失败: " + describe(err)));
}

function ensureVisualLink(page) {
  activeVisualContext = page;
  // The page message itself only exists after content.js has passed the
  // context-sync preference gate. Unknown is therefore safe during startup,
  // while an explicit false must tear down the already-registered visual and
  // browser-control source immediately.
  if (
    (contextPreferenceKnown && !contextSyncEnabled) ||
    !contextSurfaceVisible()
  ) {
    closeVisualLink();
    return;
  }
  const source = String(page?.document?.sourceInstanceId || "");
  if (!/^[A-Za-z0-9_-]{22}$/.test(source)) return;
  visualPage = page;
  visualCommitted = {
    snapshotRevision: page.snapshotRevision,
    sourceInstanceId: source,
    file: String(page.url || ""),
    page: 0,
    viewKey: String(page.viewKey || ""),
    drawingRevision: page.visual?.drawing?.drawingRevision || null,
  };
  if (visualLink && visualSourceInstanceId === source) return;
  try { visualLink?.close(); } catch (_) {}
  visualSourceInstanceId = source;
  visualLink = new ContextLink(visualLinkStatus, handleContextBridgeEvent);
  visualLink.bindVisualSource(source).catch(() => {});
  visualLink.connect();
}

// Reads extension storage under either API shape.
//
// Safari's chrome.* surface is not uniformly promise-based: chrome.storage
// .local.get may take a callback and return undefined instead of a promise.
// `await` on that yields undefined rather than throwing, so the failure was
// invisible twice over -- the catch never fired, and the caller went on to
// treat "no data" as "the user turned sync off". Both shapes are handled here,
// and a genuine failure now rejects so it can be reported.
function runtimeRequest(message, timeoutMs = 15000) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const timer = window.setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(new Error("runtime.sendMessage 超时"));
    }, timeoutMs);
    const done = (value) => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timer);
      const err = chrome.runtime?.lastError;
      if (err) reject(new Error(err.message || "runtime.sendMessage 失败"));
      else resolve(value);
    };
    try {
      const returned = chrome.runtime.sendMessage(message, done);
      if (returned && typeof returned.then === "function") returned.then(
        (value) => { if (!settled) { settled = true; window.clearTimeout(timer); resolve(value); } },
        (err) => { if (!settled) { settled = true; window.clearTimeout(timer); reject(err); } }
      );
    } catch (err) {
      window.clearTimeout(timer);
      reject(err);
    }
  });
}

async function refreshVisualContext() {
  try {
    const reply = await runtimeRequest({ type: "BW_READER_VISUAL_CONTEXT_GET" }, 5000);
    const page = reply?.ok === true ? storedVisualContext(reply.data) : null;
    if (!page) return false;
    ensureVisualLink(page);
    return true;
  } catch (_) {
    return false;
  }
}

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
      CONTEXT_SYNC_MIRROR_KEY,
    ]);
    applyContextPreference(
      bag?.[PREFERENCE_KEY],
      bag?.[CONTEXT_SYNC_MIRROR_KEY]
    );
    const visualReady = await refreshVisualContext();
    if (!visualReady) {
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
    if (changes[CONTEXT_SYNC_MIRROR_KEY]) {
      const value = changes[CONTEXT_SYNC_MIRROR_KEY].newValue;
      if (typeof value === "boolean") {
        applyContextPreference(undefined, value);
      } else {
        contextMirrorKnown = false;
      }
    } else if (changes[PREFERENCE_KEY]) {
      applyContextPreference(changes[PREFERENCE_KEY].newValue);
    }
    if (changes[VISUAL_BINDING_SIGNAL_KEY]) refreshVisualContext();
  });
}

function resumeVisualLink() {
  const visible = contextSurfaceVisible();
  if (!visible) {
    contextWasVisible = false;
    closeVisualLink();
    return;
  }
  if (!contextWasVisible) {
    // The bridge corpus is deliberately bounded and another foreground tab
    // may have replaced it while this page was hidden. Reattach this page's
    // full document once when it returns; tab-local de-duplication alone would
    // otherwise make A -> B -> A leave snapshot A paired with corpus B.
    contextWasVisible = true;
  }
  refreshVisualContext().then((ready) => {
    if (!ready && activeVisualContext) ensureVisualLink(activeVisualContext);
  });
}

document.addEventListener("visibilitychange", resumeVisualLink, { passive: true });
window.addEventListener("pageshow", resumeVisualLink, { passive: true });
window.addEventListener("focus", resumeVisualLink, { passive: true });
window.addEventListener("online", resumeVisualLink, { passive: true });

// --- placing a call from here ------------------------------------------------
// Either side may start it, and whoever does holds it: the other end only sends
// context and never asks for audio, so switching between them no longer drops
// the call. Before, both tried to own the one voice link and each switch evicted
// the other.
let voiceActive = false;
// Set only by the authenticated one-time frame claim when embedded; otherwise
// from this extension page's own URL. The hosting web page cannot choose it.
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

window.addEventListener("pagehide", closeVisualLink);
