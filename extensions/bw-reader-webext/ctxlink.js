"use strict";

// A context-only link to the Windows bridge.
//
// Separate from the voice endpoint on purpose. This one accepts many concurrent
// connections, takes no part in audio ownership, and evicts nobody -- so the
// App can hold the call from end to end while whichever surface is in front of
// the user keeps describing what that is. Snapshot writes are serialised on the
// Windows side and the last one wins, which is what makes switching pages
// require no arbitration at all: whoever is in front simply writes.
//
// The protocol is the existing one, minus everything to do with audio. START,
// STOP, heartbeat and binary frames are refused by the endpoint, so this file
// structurally cannot start or stop a call.

const CTX_ENDPOINT = "wss://bwicarus-2.taile44d0c.ts.net/reader-context/v1";
const CONTRACT = "reader-computer-voice-direct/1";
const OUTGOING_CONTRACT = "reader-outgoing-context/1";

// Backoff exists because this link is meant to outlive brief network trouble:
// it is the thing that keeps the assistant aware of the page, and a link that
// gives up after one failure would quietly stop doing that.
const RECONNECT_MIN_MS = 1000;
const RECONNECT_MAX_MS = 15000;

function randomHex(length) {
  const hex = "0123456789abcdef";
  let out = "";
  for (let i = 0; i < length; i += 1) out += hex[(Math.random() * 16) | 0];
  return out;
}

// session- plus 22 base64url characters: the encoding of 16 random bytes.
//
// Drawing 22 characters at random from the alphabet is not the same thing and
// the bridge rejects it as "base64url 字段无效". 22 characters carry 132 bits
// while 16 bytes are 128, so the final character only ever holds 2 bits and is
// restricted to A/Q/g/w -- a constraint random picking violates almost every
// time. Encoding real bytes satisfies it by construction.
function newSessionId() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
  const b64 = btoa(binary)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
  return "session-" + b64;
}

export class ContextLink {
  constructor(onStatus, onEvent) {
    this.onStatus = typeof onStatus === "function" ? onStatus : () => {};
    this.onEvent = typeof onEvent === "function" ? onEvent : () => {};
    this.socket = null;
    this.sessionId = null;
    this.ready = false;
    this.closed = false;
    this.seq = 0;
    this.requestId = 0;
    this.pending = new Map();
    this.retryMs = RECONNECT_MIN_MS;
    this.retryTimer = null;
    // Held so a reconnect can restate the page immediately: the bridge keeps
    // the previous snapshot, but it would be describing a page the user may
    // have left while the link was down.
    this.lastPage = null;
    // A context connection may expose exactly one visual source. Keep the
    // binding across reconnects, but restate it after each context-open.
    this.sourceInstanceId = null;
    this.registeredSourceInstanceId = null;
  }

  connect() {
    if (this.closed || this.socket) return;
    this.onStatus({ state: "connecting" });

    let socket;
    try {
      socket = new WebSocket(CTX_ENDPOINT);
    } catch (err) {
      this.#scheduleRetry(err);
      return;
    }
    this.socket = socket;

    socket.onopen = () => {
      this.#request("hello", { protocolVersion: 3 })
        .then(() => {
          this.sessionId = newSessionId();
          return this.#request("context-open", { sessionId: this.sessionId });
        })
        .then(() => this.#registerVisualSource())
        .then(() => {
          this.ready = true;
          this.retryMs = RECONNECT_MIN_MS;
          this.onStatus({ state: "open" });
          if (this.lastPage) this.send(this.lastPage).catch(() => {});
        })
        .catch((err) => {
          this.onStatus({ state: "error", error: err });
          try { socket.close(); } catch (_) {}
        });
    };

    socket.onmessage = (event) => this.#onMessage(event);

    socket.onclose = () => {
      const wasReady = this.ready;
      this.ready = false;
      this.socket = null;
      this.sessionId = null;
      this.registeredSourceInstanceId = null;
      for (const [, entry] of this.pending) entry.reject(new Error("连接已关闭"));
      this.pending.clear();
      if (this.closed) return;
      this.#scheduleRetry(wasReady ? null : new Error("握手未完成即断开"));
    };

    // WebSocket error events carry no reason by specification; close code and
    // the absence of a completed handshake are the only signals available.
    socket.onerror = () => {};
  }

  bindVisualSource(sourceInstanceId) {
    const value = String(sourceInstanceId || "");
    if (!/^[A-Za-z0-9_-]{22}$/.test(value)) {
      return Promise.reject(new Error("页面视觉来源标识无效"));
    }
    if (this.sourceInstanceId && this.sourceInstanceId !== value) {
      return Promise.reject(new Error("一条上下文连接不能切换视觉来源"));
    }
    this.sourceInstanceId = value;
    return this.#registerVisualSource();
  }

  sendRequest(type, fields) {
    return this.#request(type, fields);
  }

  close() {
    this.closed = true;
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = null;
    try { this.socket?.close(); } catch (_) {}
    this.socket = null;
    this.ready = false;
  }

  // Reports the page and the reading position together. Windows pairs the two
  // by (file, page); page 0 on both sides is what lets an ordinary web page,
  // which has no pagination, participate in a protocol built around books.
  async send(page) {
    this.lastPage = page;
    if (!this.ready) return { skipped: "link-not-ready" };

    const text = String(page.text || "").slice(0, 12000);
    const selection = String(page.selection || "").slice(0, 400);
    const url = String(page.url || "");

    this.seq += 1;
    await this.#request("context", {
      sessionId: this.sessionId,
      contextContract: OUTGOING_CONTRACT,
      event: {
        v: 1,
        seq: this.seq,
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
          text_source: "extension-page",
          fallback_reason: text ? null : "扩展未取得正文",
          truncated: String(page.text || "").length > 12000,
          reason: "active",
          visual: null,
          embeds: { highlights: 0, blocks: 0, unanchored: [] },
        },
      },
    });

    // Nested under `active`, with its own contract -- sent flat it is refused as
    // BW_COMPUTER_VOICE_DIRECT_MESSAGE_INVALID, and since the preceding context
    // call succeeds, the snapshot updates while this half silently fails.
    await this.#request("active-reading", {
      sessionId: this.sessionId,
      activeContract: "reader-active-reading/1",
      active: {
        kind: "web",
        file: url,
        title: String(page.title || ""),
        page: 0,
        selectionState: selection ? "active" : "cleared",
        selection: selection || null,
        observedAtEpochMs: Date.now(),
      },
    });

    return { ok: true, seq: this.seq };
  }

  #request(type, fields) {
    return new Promise((resolve, reject) => {
      if (!this.socket || this.socket.readyState !== 1) {
        reject(new Error("连接未就绪"));
        return;
      }
      this.requestId += 1;
      const requestId = "ctx-" + this.requestId;
      const message = Object.assign(
        { contract: CONTRACT, type, requestId },
        fields || {}
      );
      const timer = setTimeout(() => {
        this.pending.delete(requestId);
        reject(new Error(type + " 超时"));
      }, 10000);
      this.pending.set(requestId, {
        resolve: (v) => { clearTimeout(timer); resolve(v); },
        reject: (e) => { clearTimeout(timer); reject(e); },
      });
      try {
        this.socket.send(JSON.stringify(message));
      } catch (err) {
        clearTimeout(timer);
        this.pending.delete(requestId);
        reject(err);
      }
    });
  }

  #onMessage(event) {
    let message;
    try {
      message = JSON.parse(String(event.data || ""));
    } catch (_) {
      return;
    }
    const entry = this.pending.get(message?.requestId);
    if (!entry) {
      if (
        message?.contract === CONTRACT &&
        message?.type === "event" &&
        typeof message?.event === "string"
      ) {
        try { this.onEvent(message); } catch (_) {}
      }
      return;
    }
    this.pending.delete(message.requestId);
    if (message.ok === true) entry.resolve(message.payload ?? message);
    else entry.reject(new Error(message?.error?.message || message?.error?.code || "请求被拒绝"));
  }

  #registerVisualSource() {
    if (
      !this.sourceInstanceId ||
      !this.sessionId ||
      !this.socket ||
      this.socket.readyState !== 1 ||
      this.registeredSourceInstanceId === this.sourceInstanceId
    ) return Promise.resolve({ skipped: true });
    const source = this.sourceInstanceId;
    return this.#request("visual-register", {
      sessionId: this.sessionId,
      sourceInstanceId: source,
    }).then((result) => {
      this.registeredSourceInstanceId = source;
      return result;
    });
  }

  #scheduleRetry(err) {
    if (this.closed || this.retryTimer) return;
    this.onStatus({ state: "retrying", error: err, delayMs: this.retryMs });
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      this.connect();
    }, this.retryMs);
    this.retryMs = Math.min(this.retryMs * 2, RECONNECT_MAX_MS);
  }
}
