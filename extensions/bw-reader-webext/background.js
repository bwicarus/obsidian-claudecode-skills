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
