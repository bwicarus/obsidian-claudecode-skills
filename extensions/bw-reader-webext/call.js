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

window.addEventListener("pagehide", () => link.close());
