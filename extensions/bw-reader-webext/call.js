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
const ACTIVE_CONTEXT_KEY = "bwActivePageContextV1";

// Compared before sending. Without it the same page would be resent on every
// scroll event, each one costing a sequence number and two round trips.
let lastSignature = "";
let lastPage = null;
let contextPreferenceKnown = false;
let contextSyncEnabled = false;
let contextMirrorKnown = false;
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
  lastSignature = "";
  if (!contextSyncEnabled) {
    closeContextLink();
    closeVisualLink();
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
let lastDocumentSignature = "";
let preparedDocumentCache = null;
let directPostRunning = false;
let directPostQueued = null;

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
  const signature = `${page.url}|${page.title || ""}|${page.viewKey || ""}|${contentDigest(page.text)}|${contentDigest(page.selection)}`;
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
let visualQueue = Promise.resolve();
const visualCapturePending = new Map();
const browserControlPending = new Map();
let browserControlQueue = Promise.resolve();

function closeVisualLink() {
  const current = visualLink;
  visualLink = null;
  visualSourceInstanceId = "";
  visualPage = null;
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
  return new Promise((resolve) => {
    const timer = window.setTimeout(() => {
      visualCapturePending.delete(request.correlation);
      resolve(null);
    }, 20000);
    visualCapturePending.set(request.correlation, {
      sourceInstanceId: request.sourceInstanceId,
      resolve: (value) => {
        window.clearTimeout(timer);
        resolve(value);
      },
    });
    try {
      window.parent.postMessage({
        contract: LOCAL_VISUAL_CONTRACT,
        type: "capture-request",
        request,
      }, "*");
    } catch (_) {
      window.clearTimeout(timer);
      visualCapturePending.delete(request.correlation);
      resolve(null);
    }
  });
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
    request.sourceInstanceId !== String(visualPage?.document?.sourceInstanceId || "") ||
    request.file !== String(visualPage?.url || "")
  ) {
    await sendVisualUnavailable(request);
    return;
  }
  const capture = await localVisualCapture(request);
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

function localBrowserControl(request) {
  return new Promise((resolve) => {
    const timer = window.setTimeout(() => {
      browserControlPending.delete(request.correlation);
      resolve(null);
    }, 10000);
    browserControlPending.set(request.correlation, {
      sourceInstanceId: request.sourceInstanceId,
      action: request.action,
      resolve: (value) => {
        window.clearTimeout(timer);
        resolve(value);
      },
    });
    const local = {
      contract: LOCAL_BROWSER_CONTROL_CONTRACT,
      type: "request",
      requestId: request.correlation,
      sourceInstanceId: request.sourceInstanceId,
      action: request.action,
    };
    if (request.target !== null) local.target = request.target;
    if (request.selectionId !== null) local.selectionId = request.selectionId;
    try {
      window.parent.postMessage(local, "*");
    } catch (_) {
      window.clearTimeout(timer);
      browserControlPending.delete(request.correlation);
      resolve(null);
    }
  });
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
  let result = null;
  if (
    request.sourceInstanceId === String(visualPage?.document?.sourceInstanceId || "") &&
    request.file === String(visualPage?.url || "") &&
    request.page === 0
  ) result = await localBrowserControl(request);
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
  if (visualLink && visualSourceInstanceId === source) return;
  try { visualLink?.close(); } catch (_) {}
  visualSourceInstanceId = source;
  visualLink = new ContextLink(visualLinkStatus, handleContextBridgeEvent);
  visualLink.bindVisualSource(source).catch(() => {});
  visualLink.connect();
}

window.addEventListener("message", (event) => {
  const d = event.data;
  if (
    event.source === window.parent &&
    d?.contract === LOCAL_BROWSER_CONTROL_CONTRACT &&
    d?.type === "result"
  ) {
    const pending = browserControlPending.get(d.requestId);
    if (!pending || pending.sourceInstanceId !== d.sourceInstanceId || pending.action !== d.action) return;
    const success = d.ok === true &&
      exactKeys(d, [
        "contract", "type", "requestId", "sourceInstanceId", "action", "ok", "state",
      ]) &&
      exactKeys(d.state, ["scrollX", "scrollY", "url", "title"]) &&
      Number.isFinite(Number(d.state.scrollX)) &&
      Number.isFinite(Number(d.state.scrollY)) &&
      /^https?:\/\//.test(String(d.state.url || ""));
    const failure = d.ok === false &&
      exactKeys(d, [
        "contract", "type", "requestId", "sourceInstanceId", "action", "ok", "error",
      ]) &&
      exactKeys(d.error, ["code", "message", "retryable"]) &&
      typeof d.error.code === "string" &&
      typeof d.error.message === "string" &&
      typeof d.error.retryable === "boolean";
    if (!success && !failure) return;
    browserControlPending.delete(d.requestId);
    pending.resolve(success
      ? { ok: true, state: d.state }
      : { ok: false, error: d.error });
    return;
  }
  if (
    event.source === window.parent &&
    exactKeys(d, [
      "contract", "type", "correlation", "sourceInstanceId",
      "status", "mimeType", "b64",
    ]) &&
    d.contract === LOCAL_VISUAL_CONTRACT &&
    d.type === "capture-response"
  ) {
    const pending = visualCapturePending.get(d.correlation);
    if (!pending || pending.sourceInstanceId !== d.sourceInstanceId) return;
    visualCapturePending.delete(d.correlation);
    pending.resolve({
      status: d.status,
      mimeType: d.mimeType,
      b64: d.b64,
    });
    return;
  }
  if (!d || d.contract !== "bw-page-context/1" || d.type !== "page") return;
  if (event.source !== window.parent) return;
  if (!d.page || typeof d.page !== "object") return;
  frameProbe("框收到页面: " + String(d.page.url || "").slice(0, 50));
  ensureVisualLink(d.page);
  queueDirect(d.page);
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
// A document is sent once per content revision and stored outside the live
// snapshot.  Keep enough room for the complete text of an ordinary long page
// (including CJK), while the bridge still enforces a hard request/corpus cap.
const MAX_DOCUMENT_UTF8_BYTES = 768 * 1024;
const MAX_DOCUMENT_CHARACTERS = 256 * 1024;
const MAX_CONTEXT_URL_CHARACTERS = 4096;
const MAX_CONTEXT_TITLE_CHARACTERS = 1024;

function normalizeReadableText(value) {
  return String(value || "")
    .replace(/\r\n?/g, "\n")
    .replace(/\u00a0/g, " ")
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, "")
    .replace(/[ \t]+/g, " ")
    .replace(/ *\n */g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

function safeContextTitle(value) {
  return String(value || "")
    .replace(/[\u0000-\u001f\u007f]/g, " ")
    .slice(0, MAX_CONTEXT_TITLE_CHARACTERS);
}

function safeContextUrl(value) {
  const text = String(value || "");
  if (
    !text || text.length > MAX_CONTEXT_URL_CHARACTERS ||
    /[\u0000-\u001f\u007f]/.test(text)
  ) return null;
  try {
    const parsed = new URL(text);
    const canonical = parsed.href;
    return (
      (parsed.protocol === "http:" || parsed.protocol === "https:") &&
      canonical.length <= MAX_CONTEXT_URL_CHARACTERS &&
      !/[\u0000-\u001f\u007f]/.test(canonical)
    ) ? canonical : null;
  } catch (_) {
    return null;
  }
}

async function sha256Hex(value) {
  if (!globalThis.crypto?.subtle || typeof TextEncoder !== "function") {
    throw new Error("扩展页缺少 SHA-256 支持");
  }
  const bytes = new TextEncoder().encode(String(value || ""));
  const digest = await globalThis.crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function boundUtf8Text(value, maxBytes) {
  const text = String(value || "");
  const encoder = new TextEncoder();
  if (encoder.encode(text).byteLength <= maxBytes) return { text, truncated: false };
  let low = 0;
  let high = text.length;
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    if (encoder.encode(text.slice(0, middle)).byteLength <= maxBytes) low = middle;
    else high = middle - 1;
  }
  // Never leave a dangling UTF-16 high surrogate at the byte boundary.
  if (low > 0 && /[\uD800-\uDBFF]/.test(text.charAt(low - 1))) low -= 1;
  return { text: text.slice(0, low), truncated: true };
}

async function prepareDocument(page) {
  const raw = page && page.document;
  if (!raw || typeof raw !== "object") return null;
  const sourceInstanceId = String(raw.sourceInstanceId || "");
  if (!/^[A-Za-z0-9_-]{22}$/.test(sourceInstanceId)) {
    throw new Error("页面来源标识无效");
  }
  const fullText = normalizeReadableText(raw.text);
  const url = safeContextUrl(page.url);
  if (!url) throw new Error("页面 URL 无效");
  const documentKey = safeContextUrl(raw.documentKey) || url;
  if (
    preparedDocumentCache &&
    preparedDocumentCache.sourceInstanceId === sourceInstanceId &&
    preparedDocumentCache.documentKey === documentKey &&
    preparedDocumentCache.fullText === fullText &&
    preparedDocumentCache.sourceTruncated === !!raw.truncated
  ) return preparedDocumentCache.payload;
  let characterBoundText = fullText.slice(0, MAX_DOCUMENT_CHARACTERS);
  if (
    characterBoundText &&
    /[\uD800-\uDBFF]/.test(characterBoundText.charAt(characterBoundText.length - 1))
  ) characterBoundText = characterBoundText.slice(0, -1);
  const characterTruncated = characterBoundText.length < fullText.length;
  const bounded = boundUtf8Text(characterBoundText, MAX_DOCUMENT_UTF8_BYTES);
  const payload = {
    contract: "reader-document/1",
    sourceInstanceId,
    documentKey,
    url,
    title: safeContextTitle(page.title),
    // Hash the normalized full text, not the transmitted prefix. A change
    // beyond the byte bound still produces a new corpus revision.
    contentRevision: await sha256Hex(fullText),
    text: bounded.text,
    truncated: !!raw.truncated || characterTruncated || bounded.truncated,
    observedAtEpochMs: Date.now(),
  };
  preparedDocumentCache = {
    sourceInstanceId,
    documentKey,
    fullText,
    sourceTruncated: !!raw.truncated,
    payload,
  };
  return payload;
}

function prepareViewport(page) {
  const raw = page && page.viewport && typeof page.viewport === "object"
    ? page.viewport
    : {};
  const selection = normalizeReadableText(raw.selection ?? page.selection ?? "").slice(0, 400);
  const visibleText = normalizeReadableText(raw.visibleText ?? page.text ?? "").slice(0, 12000);
  const sourceInstanceId = String(page?.document?.sourceInstanceId || "");
  const url = safeContextUrl(page?.url);
  if (!url) throw new Error("页面 URL 无效");
  const documentKey = safeContextUrl(page?.document?.documentKey) || url;
  const payload = {
    contract: "reader-viewport/1",
    sourceInstanceId,
    documentKey,
    url,
    title: safeContextTitle(page?.title),
    beforeText: normalizeReadableText(raw.beforeText || "").slice(-2400),
    visibleText,
    afterText: normalizeReadableText(raw.afterText || "").slice(0, 2400),
    selectionState: selection ? "active" : "cleared",
    selection: selection || null,
    observedAtEpochMs: Date.now(),
  };
  const controlCorrelation = safeVisualId(raw.controlCorrelation, true);
  if (raw.controlCorrelation !== undefined && raw.controlCorrelation !== null) {
    if (!controlCorrelation) throw new Error("浏览控制关联标识无效");
    payload.controlCorrelation = controlCorrelation;
  }
  return payload;
}

function prepareSelectionRegions(page) {
  const raw = page?.selectionRegions;
  if (raw === undefined) {
    return {
      contract: "reader-selection-regions/1",
      total: 0,
      truncated: false,
      items: [],
    };
  }
  if (
    !exactKeys(raw, ["contract", "total", "truncated", "items"]) ||
    raw.contract !== "reader-selection-regions/1" ||
    !Number.isSafeInteger(raw.total) || raw.total < 0 || raw.total > 1000000 ||
    typeof raw.truncated !== "boolean" ||
    !Array.isArray(raw.items) || raw.items.length > 128 ||
    raw.truncated !== (raw.total > raw.items.length) ||
    (!raw.truncated && raw.total !== raw.items.length)
  ) throw new Error("页面选区索引无效");
  const seen = new Set();
  let priorOrdinal = 0;
  const firstOrdinal = raw.total - raw.items.length + 1;
  const items = raw.items.map((item, index) => {
    if (
      !exactKeys(item, [
        "selectionId", "label", "ordinal", "createdAtEpochMs",
      ]) ||
      !safeVisualId(item.selectionId, false) || seen.has(item.selectionId) ||
      typeof item.label !== "string" || item.label.length < 1 ||
      item.label.length > 80 || /[\u0000-\u001f\u007f]/.test(item.label) ||
      !Number.isSafeInteger(item.ordinal) || item.ordinal <= priorOrdinal ||
      item.ordinal !== firstOrdinal + index ||
      item.ordinal < 1 || item.ordinal > raw.total ||
      !item.label.startsWith(`#${item.ordinal} `) ||
      !Number.isSafeInteger(item.createdAtEpochMs) || item.createdAtEpochMs < 0
    ) throw new Error("页面选区条目无效");
    seen.add(item.selectionId);
    priorOrdinal = item.ordinal;
    return {
      selectionId: item.selectionId,
      label: item.label,
      ordinal: item.ordinal,
      createdAtEpochMs: item.createdAtEpochMs,
    };
  });
  if (items.length > 0 && priorOrdinal !== raw.total) {
    throw new Error("页面选区条目序号无效");
  }
  return {
    contract: "reader-selection-regions/1",
    total: raw.total,
    truncated: raw.truncated,
    items,
  };
}

function randomHex(length) {
  const hex = "0123456789abcdef";
  let out = "";
  for (let i = 0; i < length; i += 1) out += hex[(Math.random() * 16) | 0];
  return out;
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
// A repeat still sends `active` plus the structured viewport. The legacy
// page.context body is omitted, so the bridge can refresh current position
// without overwriting stable snapshot text. A new document revision is carried
// independently even when the visible viewport itself is a repeat.
async function postSnapshot(page, bodyIsRepeat, documentPayload) {
  const viewport = prepareViewport(page);
  const url = viewport.url;
  const title = viewport.title;
  const text = viewport.visibleText;
  const selection = viewport.selection || "";
  const selectionRegions = prepareSelectionRegions(page);
  const body = {
    // Current view and full document are deliberately separate. The bridge may
    // put viewport text in the live snapshot and persist document in a corpus;
    // document.text must never become currentPage.text by accident.
    viewport,
    // Nested under `active` with its own shape -- sent flat the bridge refuses
    // it, and because the context half succeeds first, the refusal would be
    // invisible: the snapshot updates while the reading position silently does
    // not.
    active: {
      kind: "web",
      file: url,
      title,
      page: 0,
      selectionState: selection ? "active" : "cleared",
      selection: selection || null,
      sourceInstanceId: viewport.sourceInstanceId,
      selectionRegions,
      observedAtEpochMs: Date.now(),
    },
  };
  if (documentPayload) body.document = documentPayload;
  if (!bodyIsRepeat) {
    body.event = {
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
      title,
      kind: "web",
      text_available: !!text,
      page_context: {
        text,
        text_available: !!text,
        text_source: "extension-viewport",
        fallback_reason: text ? null : "扩展未取得正文",
        truncated: String(page.viewport?.visibleText ?? page.text ?? "").length > 12000,
        reason: "active",
        visual: null,
        embeds: { highlights: 0, blocks: 0, unanchored: [] },
      },
    };
  }
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
  // The current view includes URL, title, visible body and the internal view
  // key. A selection or heartbeat in the same view does not resend the body;
  // an actual scroll does, even when adjacent regions contain identical text.
  const bodySig =
    `${page.url}|${page.title || ""}|${page.viewKey || ""}|${contentDigest(page.text)}`;
  const bodyIsRepeat = bodySig === lastBodySignature;
  try {
    const preparedDocument = await prepareDocument(page);
    const documentSig = preparedDocument
      ? `${preparedDocument.sourceInstanceId}|${preparedDocument.documentKey}|${preparedDocument.contentRevision}`
      : "";
    const documentPayload = documentSig && documentSig !== lastDocumentSignature
      ? preparedDocument
      : null;
    frameProbe(
      (bodyIsRepeat ? "框: 开始 POST(仅位置" : "框: 开始 POST(视口") +
      (documentPayload ? "+全文)" : ")")
    );
    await postSnapshot(page, bodyIsRepeat, documentPayload);
    lastBodySignature = bodySig;
    if (documentPayload) lastDocumentSignature = documentSig;
    frameProbe(
      (bodyIsRepeat ? "框: POST 成功(仅位置" : "框: POST 成功(视口") +
      (documentPayload ? "+全文)" : ")")
    );
  } catch (err) {
    // Said out loud. Every silent failure in this chain cost a build to find.
    note("快照投递失败: " + describe(err));
    frameProbe("框: POST 失败 " + describe(err));
  }
}

// Serialize one-shot writes and coalesce anything waiting behind the active
// request to the newest view. Without this, a slow POST for the old scroll
// position can complete after a newer POST and overwrite the snapshot with a
// stale viewport even though the user has already moved on.
async function drainDirectQueue() {
  if (directPostRunning) return;
  directPostRunning = true;
  try {
    while (directPostQueued) {
      const next = directPostQueued;
      directPostQueued = null;
      await forwardDirect(next);
    }
  } finally {
    directPostRunning = false;
    // A page can be queued between the loop condition and finally in unusual
    // promise scheduling. Re-enter rather than leaving the newest view stuck.
    if (directPostQueued) drainDirectQueue();
  }
}

function queueDirect(page) {
  if (directPostQueued) frameProbe("框: 合并等待中的旧视图");
  directPostQueued = page;
  drainDirectQueue();
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
      CONTEXT_SYNC_MIRROR_KEY,
      ACTIVE_CONTEXT_KEY,
      "bwCallContext",
    ]);
    applyContextPreference(
      bag?.[PREFERENCE_KEY],
      bag?.[CONTEXT_SYNC_MIRROR_KEY]
    );
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
    if (changes[ACTIVE_CONTEXT_KEY]) {
      const page = storedPage(changes[ACTIVE_CONTEXT_KEY].newValue);
      if (page) forward(page, true);
    }
  });
}

function resumeContextLink() {
  if (!contextSurfaceVisible()) {
    closeContextLink();
    closeVisualLink();
    return;
  }
  const current = ensureContextLink();
  if (current && lastPage) forward(lastPage, true);
  if (lastPage) ensureVisualLink(lastPage);
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
