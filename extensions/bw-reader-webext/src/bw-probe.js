"use strict";

// One diagnostic channel, crossing every boundary this extension spans.
//
// Written after an evening in which the same functionality was instrumented
// nine times, each time because the previous silence hid the next one. The
// instrumentation that existed was in four unconnected pieces -- note() to the
// frame's own status line, a probe box in the page, a log file on the bridge,
// and console.warn nobody could read on an iPad -- so the page could report
// "delivered" while the bridge reported "nothing arrived", with no way to see
// which was mistaken.
//
// Three properties matter, and each of them was violated at some point:
//
//   It never routes through what it observes. A report about a broken frame
//   cannot travel through that frame. Reports go out over their own path, and
//   the page's own DOM is the terminus -- that surface survives whatever else
//   is failing.
//
//   It says who is speaking. "delivered" and "nothing arrived" are only
//   contradictory once you know they came from two different places.
//
//   It is visible without a console. On iOS there is no Web Inspector, so
//   console.warn is indistinguishable from writing nothing at all.
//
// See references/silent-failure-lessons.md for the ten failures this came from.

(function () {
const CONTRACT = "bw-probe/1";
const MAX_LINES = 14;

// Only frames we created ourselves may report.
//
// Anything on the page can postMessage, and a report box that renders arbitrary
// text from arbitrary senders is a defacement surface. Membership is decided by
// comparing against the frames this extension embedded, never by trusting a
// field inside the message.
const trustedSources = new Set();

let host = null;
let enabled = false;

function ensureHost() {
  if (host && host.isConnected) return host;
  const box = document.createElement("div");
  box.id = "__bw_probe";
  box.setAttribute("aria-hidden", "true");
  box.style.cssText = [
    "position:fixed", "left:8px", "bottom:8px", "z-index:2147483647",
    "max-width:72vw", "max-height:42vh", "overflow:auto",
    "background:rgba(12,18,32,.92)", "color:#cfe3ff",
    "font:11px/1.5 ui-monospace,Menlo,monospace",
    "padding:8px 10px", "border-radius:8px", "white-space:pre-wrap",
    // Reports must never intercept what the user is trying to press.
    "pointer-events:none",
  ].join(";");
  (document.body || document.documentElement).appendChild(box);
  host = box;
  return box;
}

function stamp() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

// Reports one step. `where` names the speaker; without it two contradictory
// lines cannot be told apart.
function probe(where, text) {
  if (!enabled) return;
  try {
    const box = ensureHost();
    const nl = String.fromCharCode(10);
    const line = `${stamp()}  [${where}] ${String(text)}`;
    box.textContent = (line + nl + box.textContent)
      .split(nl).slice(0, MAX_LINES).join(nl);
  } catch (_) {
    // Deliberately swallowed, and the only place in this file where that is
    // allowed: a diagnostic that can break the page it reports on is worse
    // than no diagnostic. Every other catch in this codebase must speak.
  }
}

// Registers a frame this extension embedded, so its reports are accepted.
function trustFrame(frame) {
  try {
    if (frame && frame.contentWindow) trustedSources.add(frame.contentWindow);
  } catch (_) {}
}

// Starts listening for reports from trusted frames.
function startProbeHost(options) {
  enabled = !(options && options.enabled === false);
  if (!enabled) return;
  try {
    window.addEventListener("message", (event) => {
      const d = event.data;
      if (!d || d.contract !== CONTRACT || typeof d.text !== "string") return;
      // Source identity, not a claim inside the payload.
      if (!trustedSources.has(event.source)) return;
      probe(String(d.where || "frame"), d.text);
    });
  } catch (_) {}
}

// Used inside an embedded frame: sends a report to the hosting page.
function probeToHost(where, text) {
  try {
    window.parent.postMessage(
      { contract: CONTRACT, where: String(where), text: String(text) },
      "*"
    );
  } catch (_) {}
}

// Wraps an early return so that leaving without acting is always stated.
//
// Rule 1 from the lessons file: every early return owes a reason. This makes
// following it cost one line instead of three, because a rule that is tedious
// to obey gets skipped exactly when things are going wrong.
function skip(where, reason) {
  probe(where, "跳过: " + reason);
  return false;
}

// Exposed on the window rather than exported: content scripts are classic
// scripts, so a module boundary here would put the channel out of reach of the
// very file that needs it most.
window.__bwProbe = Object.freeze({
  probe: probe,
  trustFrame: trustFrame,
  startProbeHost: startProbeHost,
  probeToHost: probeToHost,
  skip: skip,
});
})();
