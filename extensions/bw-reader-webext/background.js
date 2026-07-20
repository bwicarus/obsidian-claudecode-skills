// 扩展后台:唯一接触 token 和 Pi API 的地方。网页脚本永远拿不到 token,也不能传任意 URL(只认固定操作名)。
// ⚠ ORIGIN 指向**当前主力的 Pi**(Tailscale,iPad 走 Tailscale 访问,和现有 QA browser 一样);
//   不是暂停的 VPS bwicarus.space(代码停在 2026-05-28)。要换服务器只改这一行 + manifest host_permissions。
const ORIGIN = "https://bwicarus.taile44d0c.ts.net";
const ALLOWED_MESSAGES = new Set(["PING", "LOOKUP", "TRANSLATE", "EXPLAIN"]);

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  // 只接受本扩展自己的 content script(防别的扩展/网页伪造)
  if (sender.id && sender.id !== chrome.runtime.id) return false;
  if (!ALLOWED_MESSAGES.has(message?.type)) return false;

  handleMessage(message)
    .then((data) => sendResponse({ ok: true, data }))
    .catch((error) =>
      sendResponse({ ok: false, error: error instanceof Error ? error.message : String(error) }));
  return true;   // 异步 sendResponse
});

async function apiRequest(path, init = {}) {
  const stored = await chrome.storage.local.get("apiToken");
  const token = String(stored.apiToken || "").trim();
  if (!token) throw new Error("请先在扩展设置中保存设备令牌");

  const headers = new Headers(init.headers || {});
  headers.set("Authorization", `Bearer ${token}`);
  if (init.body) headers.set("Content-Type", "application/json");

  const response = await fetch(ORIGIN + path, {
    ...init, headers, credentials: "omit", cache: "no-store"
  });

  const contentType = response.headers.get("Content-Type") || "";
  // token 失效时服务端会重定向到登录 HTML;识别出来给明确报错,而不是把 HTML 当 JSON
  if (response.redirected || !contentType.includes("application/json")) {
    throw new Error("设备令牌无效,或服务器返回了登录页面");
  }

  const data = await response.json();
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || `服务器错误 HTTP ${response.status}`);
  }
  return data;
}

function webContext(payload) {
  const pageUrl = new URL(payload.url);
  if (!["http:", "https:"].includes(pageUrl.protocol)) throw new Error("不支持当前页面");
  pageUrl.hash = "";
  return pageUrl;
}

async function handleMessage(message) {
  if (message.type === "PING") {
    return apiRequest("/pdf/api/ping");
  }

  if (message.type === "LOOKUP") {
    const p = message.payload || {};
    const word = String(p.text || "").trim();
    if (!word) throw new Error("请先选中文字");
    if (word.length > 120) throw new Error("查词选区过长,请选单词或短语");
    const pageUrl = webContext(p);
    // dict-quick 是 GET(核对过 pdf_reader.py:5323);file=web:<url> 与阅读器网页模式同构
    const params = new URLSearchParams({
      word,
      file: `web:${pageUrl.href}`,
      page: "1",
      context: String(p.context || "").slice(0, 1200),
      langs: String(p.lang || "")
    });
    return apiRequest(`/pdf/api/dict-quick?${params}`);
  }

  if (message.type === "TRANSLATE") {
    const p = message.payload || {};
    const text = String(p.text || "").trim();
    if (!text) throw new Error("请先选中文字");
    if (text.length > 5000) throw new Error("翻译选区过长");
    return apiRequest("/pdf/api/translate", {
      method: "POST",
      body: JSON.stringify({ text, target_lang: "中文" })
    });
  }

  if (message.type === "EXPLAIN") {
    const p = message.payload || {};
    const text = String(p.text || "").trim();
    if (!text) throw new Error("请先选中文字");
    const pageUrl = webContext(p);
    return apiRequest("/pdf/api/explain", {
      method: "POST",
      body: JSON.stringify({
        text,
        context: String(p.context || "").slice(0, 2000),
        file: `web:${pageUrl.href}`,
        page: 1
      })
    });
  }

  throw new Error("不支持的操作");
}

// ── bw-fetch 长连 port:content 门面(facade.js __bwReaderFetch)的服务端 ──
// rc-* 共享层的所有请求(含 SSE 流式)经此转发:补 Bearer、只放行本服务 ORIGIN、流式分片回传。
chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "bw-fetch") return;
  const aborts = new Map();
  port.onMessage.addListener(async (m) => {
    if (m.abort) { const c = aborts.get(m.abort); if (c) c.abort(); aborts.delete(m.abort); return; }
    const { id, url, init } = m || {};
    if (!id || typeof url !== "string" || !url.startsWith(ORIGIN + "/")) {
      try { port.postMessage({ id, type: "error", error: "blocked: origin not allowed" }); } catch (_) {}
      return;
    }
    // ── 查词跨站缓存(storage.local,键=word+langs;架构口诀"扩展采跨站的"第一实例)──
    // 命中→秒答;真实请求照发(在线学习回写不断+缓存刷新);离线→命中即答。只缓存 ok:true
    // (「伊部」教训:缓存失败态会把词典缺口放大成必现)。上限 800 词按 ts 淘汰。
    const isDq = url.startsWith(ORIGIN + "/pdf/api/dict-quick?") && (!init || !init.method || init.method === "GET");
    let dqKey = null, served = false;
    if (isDq) {
      try {
        const u = new URL(url);
        dqKey = "dq:" + (u.searchParams.get("word") || "") + ":" + (u.searchParams.get("langs") || "");
        const st = await chrome.storage.local.get("dictCache");
        const hit = st.dictCache && st.dictCache[dqKey];
        if (hit && hit.body) {
          port.postMessage({ id, type: "head", status: 200, statusText: "OK", headers: { "content-type": "application/json" } });
          port.postMessage({ id, type: "chunk", b64: btoa(unescape(encodeURIComponent(hit.body))) });
          port.postMessage({ id, type: "done" });
          served = true;
        }
      } catch (_) {}
    }
    const ac = new AbortController();
    aborts.set(id, ac);
    try {
      const stored = await chrome.storage.local.get("apiToken");
      const token = String(stored.apiToken || "").trim();
      const headers = new Headers((init && init.headers) || {});
      if (token) headers.set("Authorization", `Bearer ${token}`);
      const resp = await fetch(url, {
        method: (init && init.method) || "GET", headers,
        body: init && init.body, credentials: "omit", cache: "no-store", signal: ac.signal
      });
      const hdrs = {};
      resp.headers.forEach((v, k) => { hdrs[k] = v; });
      if (!served) port.postMessage({ id, type: "head", status: resp.status, statusText: resp.statusText, headers: hdrs });
      const accParts = [];
      if (resp.body) {
        const reader = resp.body.getReader();
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          if (dqKey) accParts.push(value.slice());
          if (!served) {
            let bin = "";
            for (let i = 0; i < value.length; i += 0x8000) bin += String.fromCharCode.apply(null, value.subarray(i, i + 0x8000));
            port.postMessage({ id, type: "chunk", b64: btoa(bin) });
          }
        }
      }
      if (!served) port.postMessage({ id, type: "done" });
      if (dqKey && resp.ok && accParts.length) {
        try {
          let len = 0; for (const a of accParts) len += a.length;
          const all = new Uint8Array(len); let off = 0;
          for (const a of accParts) { all.set(a, off); off += a.length; }
          const text = new TextDecoder().decode(all);
          const data = JSON.parse(text);
          if (data && data.ok === true) {
            const st2 = await chrome.storage.local.get("dictCache");
            const cache = st2.dictCache || {};
            cache[dqKey] = { body: text, ts: Date.now() };
            const keys = Object.keys(cache);
            if (keys.length > 800) {
              keys.sort((a, b) => (cache[a].ts || 0) - (cache[b].ts || 0));
              for (const k of keys.slice(0, keys.length - 800)) delete cache[k];
            }
            await chrome.storage.local.set({ dictCache: cache });
          }
        } catch (_) {}
      }
    } catch (e) {
      if (!served) { try { port.postMessage({ id, type: "error", error: String((e && e.message) || e) }); } catch (_) {} }
    } finally { aborts.delete(id); }
  });
  port.onDisconnect.addListener(() => { for (const c of aborts.values()) { try { c.abort(); } catch (_) {} } aborts.clear(); });
});
