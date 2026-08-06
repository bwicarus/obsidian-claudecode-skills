"use strict";

// Executes the small, auditable set of browser movements exposed to the AI.
//
// This file deliberately does not accept JavaScript, selectors or navigation
// targets. A request can only move the current foreground document. The
// content script is the sole caller; background.js authenticates the
// extension-owned call.html sender before forwarding a request here.
(function () {
  const CONTRACT = "bw-browser-control/1";
  const REFRESH_EVENT = "bw:browser-control-refresh";
  const ACTIONS = Object.freeze([
    "next-viewport",
    "previous-viewport",
    "scroll-to-text",
    "scroll-to-heading",
    "scroll-to-selection",
  ]);
  const ACTION_SET = new Set(ACTIONS);
  const SAFE_ID = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
  const MAX_TARGET = 320;
  const MAX_URL = 2048;
  const MAX_TITLE = 512;
  const MAX_TEXT_NODES = 8000;
  const MAX_SEARCH_TEXT = 500000;
  const MAX_CACHE = 64;

  const completed = new Map();

  function clipped(value, limit) {
    const text = String(value == null ? "" : value);
    return text.length <= limit ? text : text.slice(0, limit);
  }

  function normalizedText(value) {
    return String(value == null ? "" : value).replace(/\s+/g, " ").trim();
  }

  function safeNumber(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return 0;
    return Math.max(-100000000, Math.min(100000000, Math.round(number)));
  }

  function pageState() {
    return Object.freeze({
      scrollX: safeNumber(window.scrollX || window.pageXOffset || 0),
      scrollY: safeNumber(window.scrollY || window.pageYOffset || 0),
      url: clipped(window.location && window.location.href, MAX_URL),
      title: clipped(document.title, MAX_TITLE),
    });
  }

  function isForegroundDocument() {
    try {
      return document.visibilityState === "visible" && document.hasFocus() === true;
    } catch (_) {
      return false;
    }
  }

  function elementVisible(element) {
    if (!element || element.nodeType !== 1) return false;
    for (let current = element; current; current = current.parentElement) {
      const tag = String(current.tagName || "").toUpperCase();
      if (tag === "SCRIPT" || tag === "STYLE" || tag === "NOSCRIPT" || tag === "TEMPLATE") {
        return false;
      }
      if (current.hidden === true || current.getAttribute?.("aria-hidden") === "true") {
        return false;
      }
      let style = null;
      try { style = window.getComputedStyle(current); } catch (_) {}
      if (style && (style.display === "none" || style.visibility === "hidden")) return false;
    }
    return true;
  }

  function collectVisibleText(root, limit) {
    if (!root || !document.createTreeWalker) return { text: "", runs: [] };
    const runs = [];
    let text = "";
    let seen = 0;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node && seen < MAX_TEXT_NODES; node = walker.nextNode()) {
      seen += 1;
      if (!elementVisible(node.parentElement)) continue;
      const part = normalizedText(node.nodeValue);
      if (!part) continue;
      if (text && text.length < limit) text += " ";
      const start = text.length;
      text += part.slice(0, Math.max(0, limit - text.length));
      const end = text.length;
      if (end > start) runs.push({ start, end, node });
      if (text.length >= limit) break;
    }
    return { text, runs };
  }

  function scrollTarget(element) {
    if (!element) return false;
    try {
      element.scrollIntoView({ behavior: "auto", block: "center", inline: "nearest" });
      return true;
    } catch (_) {
      try {
        const rect = element.getBoundingClientRect();
        window.scrollBy({ top: rect.top - Math.max(0, window.innerHeight || 0) * 0.35, left: 0 });
        return true;
      } catch (_) {
        return false;
      }
    }
  }

  function findText(target) {
    const wanted = normalizedText(target).toLocaleLowerCase();
    if (!wanted) return null;
    const collected = collectVisibleText(document.body || document.documentElement, MAX_SEARCH_TEXT);
    const index = collected.text.toLocaleLowerCase().indexOf(wanted);
    if (index < 0) return null;
    const run = collected.runs.find((item) => item.end > index);
    return run && run.node ? run.node.parentElement : null;
  }

  function visibleElementText(element) {
    return collectVisibleText(element, 4096).text;
  }

  function findHeading(target) {
    const wanted = normalizedText(target).toLocaleLowerCase();
    if (!wanted || !document.createTreeWalker) return null;
    const walker = document.createTreeWalker(
      document.body || document.documentElement,
      NodeFilter.SHOW_ELEMENT,
    );
    let contains = null;
    let seen = 0;
    for (let element = walker.nextNode(); element && seen < MAX_TEXT_NODES; element = walker.nextNode()) {
      seen += 1;
      const tag = String(element.tagName || "").toUpperCase();
      const role = String(element.getAttribute?.("role") || "").toLowerCase();
      if (!/^H[1-6]$/.test(tag) && role !== "heading") continue;
      if (!elementVisible(element)) continue;
      const actual = normalizedText(visibleElementText(element)).toLocaleLowerCase();
      if (!actual) continue;
      if (actual === wanted) return element;
      if (!contains && actual.includes(wanted)) contains = element;
    }
    return contains;
  }

  function findSelection(selectionId) {
    const roots = [];
    try { if (window.__bwShadow) roots.push(window.__bwShadow); } catch (_) {}
    roots.push(document);
    for (const root of roots) {
      let paths = [];
      try { paths = root.querySelectorAll("path[data-region-id]"); } catch (_) {}
      let seen = 0;
      for (const path of paths || []) {
        if (seen >= MAX_TEXT_NODES) break;
        seen += 1;
        if (path.getAttribute?.("data-region-id") === selectionId) return path;
      }
    }
    return null;
  }

  function scrollContainer() {
    let element = null;
    try {
      element = document.elementFromPoint(
        Math.max(0, Math.floor((window.innerWidth || 1) / 2)),
        Math.max(0, Math.floor((window.innerHeight || 1) / 2)),
      );
    } catch (_) {}
    for (let current = element; current && current !== document.body; current = current.parentElement) {
      let style = null;
      try { style = window.getComputedStyle(current); } catch (_) {}
      const overflow = String(style && style.overflowY || "").toLowerCase();
      if (/^(auto|scroll|overlay)$/.test(overflow) && current.scrollHeight > current.clientHeight + 2) {
        return current;
      }
    }
    return window;
  }

  function moveViewport(direction) {
    const scroller = scrollContainer();
    const height = scroller === window
      ? Math.max(1, window.innerHeight || document.documentElement?.clientHeight || 1)
      : Math.max(1, scroller.clientHeight || 1);
    const top = Math.round(height * 0.82) * direction;
    try {
      scroller.scrollBy({ top, left: 0, behavior: "auto" });
      return true;
    } catch (_) {
      try {
        if (scroller === window) window.scrollTo(window.scrollX || 0, (window.scrollY || 0) + top);
        else scroller.scrollTop += top;
        return true;
      } catch (_) {
        return false;
      }
    }
  }

  function invalid(code, message, retryable) {
    const error = new Error(message);
    error.code = code;
    error.retryable = retryable === true;
    throw error;
  }

  function validateRequest(raw) {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
      invalid("BW_BROWSER_CONTROL_INVALID_REQUEST", "浏览器控制请求不是对象");
    }
    if (raw.contract !== CONTRACT || raw.type !== "request") {
      invalid("BW_BROWSER_CONTROL_INVALID_REQUEST", "浏览器控制合同无效");
    }
    if (typeof raw.requestId !== "string" || !SAFE_ID.test(raw.requestId)) {
      invalid("BW_BROWSER_CONTROL_INVALID_REQUEST", "requestId 无效");
    }
    if (typeof raw.sourceInstanceId !== "string" || !SAFE_ID.test(raw.sourceInstanceId)) {
      invalid("BW_BROWSER_CONTROL_INVALID_REQUEST", "sourceInstanceId 无效");
    }
    if (!ACTION_SET.has(raw.action)) {
      invalid("BW_BROWSER_CONTROL_ACTION_NOT_ALLOWED", "不支持的浏览器控制动作");
    }
    const allowed = new Set(["contract", "type", "requestId", "sourceInstanceId", "action"]);
    if (raw.action === "scroll-to-text" || raw.action === "scroll-to-heading") {
      allowed.add("target");
      if (typeof raw.target !== "string") {
        invalid("BW_BROWSER_CONTROL_INVALID_REQUEST", "文字目标必须是字符串");
      }
      const target = normalizedText(raw.target);
      if (!target || target.length > MAX_TARGET) {
        invalid("BW_BROWSER_CONTROL_INVALID_REQUEST", `文字目标必须为 1-${MAX_TARGET} 字`);
      }
    } else if (raw.action === "scroll-to-selection") {
      allowed.add("selectionId");
      if (typeof raw.selectionId !== "string" || !SAFE_ID.test(raw.selectionId)) {
        invalid("BW_BROWSER_CONTROL_INVALID_REQUEST", "selectionId 无效");
      }
    }
    for (const key of Object.keys(raw)) {
      if (!allowed.has(key)) invalid("BW_BROWSER_CONTROL_INVALID_REQUEST", `不允许字段 ${key}`);
    }
    return raw;
  }

  function perform(request) {
    if (!isForegroundDocument()) {
      invalid("BW_BROWSER_CONTROL_DOCUMENT_INACTIVE", "页面不在前台，未执行控制", true);
    }
    let acted = false;
    if (request.action === "next-viewport") acted = moveViewport(1);
    else if (request.action === "previous-viewport") acted = moveViewport(-1);
    else if (request.action === "scroll-to-text") acted = scrollTarget(findText(request.target));
    else if (request.action === "scroll-to-heading") acted = scrollTarget(findHeading(request.target));
    else if (request.action === "scroll-to-selection") acted = scrollTarget(findSelection(request.selectionId));
    if (!acted) {
      invalid("BW_BROWSER_CONTROL_TARGET_NOT_FOUND", "没有找到可滚动到的目标", true);
    }
    const state = pageState();
    try {
      window.dispatchEvent(new CustomEvent(REFRESH_EVENT, {
        detail: Object.freeze({
          requestId: request.requestId,
          sourceInstanceId: request.sourceInstanceId,
          action: request.action,
          ...state,
        }),
      }));
    } catch (_) {}
    return state;
  }

  function responseBase(request) {
    return {
      contract: CONTRACT,
      type: "result",
      requestId: clipped(request && request.requestId, 128),
      sourceInstanceId: clipped(request && request.sourceInstanceId, 128),
      action: ACTION_SET.has(request && request.action) ? request.action : "",
    };
  }

  function remember(key, response) {
    completed.set(key, response);
    while (completed.size > MAX_CACHE) completed.delete(completed.keys().next().value);
  }

  function execute(request) {
    const base = responseBase(request);
    try {
      const checked = validateRequest(request);
      const cacheKey = `${checked.sourceInstanceId}:${checked.requestId}`;
      const prior = completed.get(cacheKey);
      if (prior) return prior;
      const response = Object.freeze({ ...base, ok: true, state: perform(checked) });
      remember(cacheKey, response);
      return response;
    } catch (error) {
      return Object.freeze({
        ...base,
        ok: false,
        error: Object.freeze({
          code: clipped(error && error.code || "BW_BROWSER_CONTROL_FAILED", 96),
          message: clipped(error && error.message || "浏览器控制失败", 320),
          retryable: !!(error && error.retryable),
        }),
      });
    }
  }

  window.__bwBrowserControl = Object.freeze({
    contract: CONTRACT,
    refreshEvent: REFRESH_EVENT,
    actions: ACTIONS,
    execute,
  });
})();
