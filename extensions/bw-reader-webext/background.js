// 扩展后台:唯一接触 token 和 Pi API 的地方。网页脚本永远拿不到 token,也不能传任意 URL(只认固定操作名)。
// ⚠ ORIGIN 指向**当前主力的 Pi**(Tailscale,iPad 走 Tailscale 访问,和现有 QA browser 一样);
//   不是暂停的 VPS bwicarus.space(代码停在 2026-05-28)。要换服务器只改这一行 + manifest host_permissions。
globalThis.__BW_READER_BACKGROUND_BUILD_VERSION = "0.2.73";
if (typeof importScripts === "function") {
  importScripts(
    "vendor/reader-runtime-account-context.js",
    "vendor/reader-runtime-extension-account-storage.js",
    "vendor/reader-runtime-data-store.js",
    "vendor/reader-runtime-indexeddb-store.js",
    "vendor/reader-runtime-data-registry.js",
    "vendor/reader-runtime-sync-owner-lease.js",
    "vendor/reader-runtime-sync-gateway.js",
    "vendor/reader-runtime-server-sync-transport.js",
    "vendor/reader-runtime-direct-sync-protocol.js",
    "vendor/reader-runtime-sync-coordinator.js",
    "vendor/reader-runtime-sync-runtime.js",
    "vendor/reader-runtime-sync-conflict-control.js",
    "vendor/reader-runtime-document-note-repository.js",
    "vendor/reader-runtime-interaction-policy.js",
    "vendor/reader-runtime-vocabulary-state.js"
  );
}

const ORIGIN = "https://bwicarus.taile44d0c.ts.net";
const interactionPolicy = globalThis.BWReaderRuntime?.interactionPolicy;
if (
  !interactionPolicy ||
  interactionPolicy.CONTRACT !== "interaction-policy/1" ||
  typeof interactionPolicy.validate !== "function" ||
  interactionPolicy.validate().ok !== true
) {
  throw new Error("扩展交互网络策略运行时不可用");
}
const ALLOWED_MESSAGES = new Set(["PING", "LOOKUP", "TRANSLATE", "EXPLAIN"]);
const LOCAL_STORAGE_MESSAGES = new Set([
  "BW_LOCAL_STORAGE_GET",
  "BW_LOCAL_STORAGE_SET",
  "BW_LOCAL_STORAGE_REMOVE"
]);
const PAGE_CARD_PRESENTATION_MESSAGES = new Set([
  "BW_PAGE_CARD_PRESENTATION_GET",
  "BW_PAGE_CARD_PRESENTATION_SET"
]);
const TRANSLATION_CACHE_MESSAGES = new Set([
  "BW_TRANSLATION_CACHE_GET"
]);
const LOCAL_STORAGE_KEYS = new Set([
  "bwReaderExtensionPreferencesV2",
  "webHighlightsV1",
  "webCardPinsV1",
  "webInkV1",
  "reviewQueueV2"
]);
const PAGE_CARD_PRESENTATION_STORAGE_KEY = "pageCardPresentationV1";
const MAX_LOCAL_STORAGE_VALUE_BYTES = 4 * 1024 * 1024;
const ACCOUNT_MESSAGES = new Set([
  "BW_ACCOUNT_STATUS",
  "BW_ACCOUNT_TOKEN_SAVE",
  "BW_ACCOUNT_TOKEN_TEST",
  "BW_SYNC_STATUS"
]);
const PUBLIC_ACCOUNT_ERROR_CODES = new Set([
  "BW_ACCOUNT_ACTIVE_TAB",
  "BW_ACCOUNT_CONTEXT_UNAVAILABLE",
  "BW_ACCOUNT_CONTEXT_STALE",
  "BW_ACCOUNT_OPERATION",
  "BW_ACCOUNT_POPUP_REQUIRED",
  "BW_ACCOUNT_PROVIDER_AMBIGUOUS",
  "BW_ACCOUNT_PROVIDER_SENDER",
  "BW_ACCOUNT_PROVIDER_UNAVAILABLE",
  "BW_ACCOUNT_STORAGE_BACKEND",
  "BW_ACCOUNT_STORAGE_CONTEXT",
  "BW_ACCOUNT_STORAGE_CRYPTO",
  "BW_ACCOUNT_STORAGE_UPDATE",
  "BW_ACCOUNT_TOKEN_INVALID",
  "BW_ACCOUNT_TOKEN_MISSING",
  "BW_ACCOUNT_TOKEN_OWNER_MISMATCH",
  "BW_SYNC_CONFLICT_DEPENDENCY",
  "BW_SYNC_CONFLICT_FENCE",
  "BW_SYNC_CONFLICT_RUNTIME",
  "BW_SYNC_OWNER_RESERVED",
  "BW_PROVIDER_AUTH_EXPIRED",
  "BW_PROVIDER_DISCONNECTED",
  "BW_PROVIDER_SENDER"
]);
const BW_FETCH_ROUTE_METHODS = (() => {
  const routes = new Map();
  const add = (paths, methods) => {
    for (const path of paths) routes.set(path, new Set(methods));
  };
  add([
    "/pdf/api/ai-stream-result",
    "/pdf/api/dict",
    "/pdf/api/dict-jp",
    "/pdf/api/dict-jp-ai",
    "/pdf/api/dict-jp-zh",
    "/pdf/api/dict-quick",
    "/pdf/api/grammar-books",
    "/pdf/api/grammar-history",
    "/pdf/api/epub-convo",
    "/pdf/api/img-proxy",
    "/pdf/api/job-status",
    "/pdf/api/page-nodes",
    "/pdf/api/toc",
    "/pdf/api/vocab-list",
    "/pdf/api/vocab-mastery-map",
    "/pdf/api/web-translate-config",
    "/api/assistant/action-prefs",
    "/api/assistant/creations-brief",
    "/api/assistant/history",
    "/api/assistant/voice-page-text",
    "/api/voice/task-status"
  ], ["GET"]);
  add([
    "/pdf/api/anki-add-cards",
    "/pdf/api/epub-furigana",
    "/pdf/api/epub-assistant",
    "/pdf/api/epub-convo/append",
    "/pdf/api/epub-convo/clear",
    "/pdf/api/epub-translate-section",
    "/pdf/api/explain",
    "/pdf/api/grammar-analyze",
    "/pdf/api/grammar-forget",
    "/pdf/api/grammar-history-save",
    "/pdf/api/grammar-stream",
    "/pdf/api/lookup-event",
    "/pdf/api/note-composite",
    "/pdf/api/reading-pos",
    "/pdf/api/review-answer",
    "/pdf/api/run-save",
    "/pdf/api/snippets-to-async",
    "/pdf/api/sync-batch",
    "/pdf/api/to-note",
    "/pdf/api/translate",
    "/pdf/api/translate-sentence",
    "/pdf/api/vocab-anki",
    "/pdf/api/vocab-mark",
    "/pdf/api/web-translate",
    "/pdf/api/web-vocab",
    "/api/assistant/action-pref",
    "/api/assistant/card-improvement-commit",
    "/api/assistant/card-improvement-draft",
    "/api/assistant/chat",
    "/api/assistant/clear",
    "/api/assistant/clip-attach",
    "/api/assistant/compact-history",
    "/api/assistant/log",
    "/api/assistant/prewarm",
    "/api/assistant/route-text",
    "/api/assistant/rtc-call",
    "/api/assistant/rtc-hangup",
    "/api/assistant/rtc-session",
    "/api/assistant/rtc-usage",
    "/api/assistant/undo",
    "/api/assistant/voice-clip",
    "/api/assistant/voice-tool"
  ], ["POST"]);
  add([
    "/pdf/api/favorites",
    "/pdf/api/epub-highlights",
    "/pdf/api/html-highlights",
    "/pdf/api/grammar-tracked",
    "/pdf/api/highlights",
    "/pdf/api/jp-vocab-mark",
    "/pdf/api/notes",
    "/pdf/api/phrase-mark",
    "/pdf/api/phrases",
    "/pdf/api/translate-config",
    "/pdf/api/userpages",
    "/pdf/api/video-player-prefs",
    "/api/assistant/pref-profiles",
    "/api/assistant/tool-prompt",
    "/api/assistant/voice-cards",
    "/api/assistant/voice-config"
  ], ["GET", "POST", "PATCH", "DELETE"]);
  add(["/pdf/api/review-queue"], ["GET", "POST"]);
  return routes;
})();
const BW_FETCH_DYNAMIC_ROUTES = Object.freeze([
  { pattern: /^\/pdf\/api\/asset\/[^/]+$/, methods: new Set(["GET"]) },
  { pattern: /^\/pdf\/api\/entity\/[^/]+$/, methods: new Set(["GET", "PATCH"]) },
  { pattern: /^\/pdf\/api\/toolshot\/[^/]+$/, methods: new Set(["GET"]) },
  { pattern: /^\/pdf\/api\/video-subtitles\/[^/]+$/, methods: new Set(["GET"]) },
  { pattern: /^\/api\/assistant\/voice-clip\/[^/]+$/, methods: new Set(["GET"]) },
  { pattern: /^\/skilltree\/[^/]+\/api\/toggle-tracked$/, methods: new Set(["POST"]) }
]);
function checkedBwFetchRequest(value, init) {
  let url;
  try { url = new URL(value); } catch (_) { url = null; }
  const method = String(init?.method || "GET").toUpperCase();
  if (!url || url.origin !== ORIGIN || url.username || url.password) {
    throw Object.assign(new Error("blocked: origin not allowed"), {
      code: "BW_FETCH_ORIGIN"
    });
  }
  let methods = BW_FETCH_ROUTE_METHODS.get(url.pathname);
  if (!methods) {
    methods = BW_FETCH_DYNAMIC_ROUTES.find((rule) =>
      rule.pattern.test(url.pathname)
    )?.methods;
  }
  if (!methods || !methods.has(method)) {
    throw Object.assign(new Error("blocked: network operation not allowed"), {
      code: "BW_FETCH_OPERATION",
      details: { path: url.pathname, method }
    });
  }
  return { url, method };
}
const MAX_BW_FETCH_BINARY_BODY_BYTES = 8 * 1024 * 1024;
const MAX_BW_FETCH_BINARY_BODY_B64_CHARS =
  4 * Math.ceil(MAX_BW_FETCH_BINARY_BODY_BYTES / 3);
function normalizedBwFetchInit(checked, value) {
  const init = value && typeof value === "object"
    ? Object.assign({}, value)
    : {};
  init.method = checked.method;
  const hasBinary = Object.prototype.hasOwnProperty.call(init, "bodyB64");
  if (!hasBinary) {
    if (init.body != null && typeof init.body !== "string") {
      throw Object.assign(new Error("blocked: unsupported request body"), {
        code: "BW_FETCH_BODY"
      });
    }
    delete init.bodyBytes;
    return init;
  }
  if (
    checked.method !== "POST" ||
    checked.url.pathname !== "/api/assistant/voice-clip" ||
    init.body != null
  ) {
    throw Object.assign(new Error("blocked: binary body not allowed"), {
      code: "BW_FETCH_BODY"
    });
  }
  const declared = Number(init.bodyBytes);
  const encoded = init.bodyB64;
  if (
    !Number.isSafeInteger(declared) ||
    declared < 1 ||
    declared > MAX_BW_FETCH_BINARY_BODY_BYTES ||
    typeof encoded !== "string" ||
    !encoded ||
    encoded.length > MAX_BW_FETCH_BINARY_BODY_B64_CHARS ||
    encoded.length % 4 !== 0 ||
    !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(encoded)
  ) {
    throw Object.assign(new Error("blocked: invalid binary request body"), {
      code: "BW_FETCH_BODY"
    });
  }
  let raw;
  try {
    raw = atob(encoded);
  } catch (_) {
    throw Object.assign(new Error("blocked: invalid binary request body"), {
      code: "BW_FETCH_BODY"
    });
  }
  if (raw.length !== declared || raw.length > MAX_BW_FETCH_BINARY_BODY_BYTES) {
    throw Object.assign(new Error("blocked: binary request size mismatch"), {
      code: "BW_FETCH_BODY"
    });
  }
  const body = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) body[i] = raw.charCodeAt(i);
  delete init.bodyB64;
  delete init.bodyBytes;
  init.body = body;
  return init;
}
const BW_WS_QUERY_KEYS = new Set([
  "mode",
  "file",
  "page",
  "fresh",
  "fe",
  "call_id",
  "uid",
  "tk"
]);
const MAX_BW_WS_FRAME_BYTES = 8 * 1024 * 1024;
const MAX_BW_WS_FRAME_B64_CHARS =
  4 * Math.ceil(MAX_BW_WS_FRAME_BYTES / 3);
const COMPUTER_VOICE_DIRECT_PORT = "BW_COMPUTER_VOICE_DIRECT_V3";
const COMPUTER_VOICE_DIRECT_ENDPOINT =
  "wss://bwicarus-2.taile44d0c.ts.net/reader-computer-voice/v1";
const MAX_COMPUTER_VOICE_DIRECT_TEXT_BYTES = 64 * 1024;
const COMPUTER_VOICE_DIRECT_PCM_FRAME_BYTES = 1956;
const COMPUTER_VOICE_DIRECT_UPLINK_BUFFER_LIMIT_BYTES =
  COMPUTER_VOICE_DIRECT_PCM_FRAME_BYTES * 10;
function checkedBwWebSocketPath(value) {
  if (
    typeof value !== "string" ||
    !value.startsWith("/voice-rt") ||
    value.length > 16 * 1024
  ) {
    throw Object.assign(new Error("blocked: invalid WebSocket path"), {
      code: "BW_WS_OPERATION"
    });
  }
  let url;
  try { url = new URL(value, ORIGIN); } catch (_) { url = null; }
  if (
    !url ||
    url.origin !== ORIGIN ||
    url.pathname !== "/voice-rt" ||
    url.username ||
    url.password ||
    url.hash
  ) {
    throw Object.assign(new Error("blocked: WebSocket origin not allowed"), {
      code: "BW_WS_ORIGIN"
    });
  }
  for (const key of new Set(url.searchParams.keys())) {
    if (
      !BW_WS_QUERY_KEYS.has(key) ||
      url.searchParams.getAll(key).length !== 1
    ) {
      throw Object.assign(new Error("blocked: WebSocket operation not allowed"), {
        code: "BW_WS_OPERATION"
      });
    }
  }
  const mode = url.searchParams.get("mode") || "";
  if (!["", "tts", "agent", "rtc"].includes(mode)) {
    throw Object.assign(new Error("blocked: invalid WebSocket mode"), {
      code: "BW_WS_OPERATION"
    });
  }
  const keys = new Set(url.searchParams.keys());
  const allowedForMode = mode === "tts"
    ? new Set(["mode"])
    : mode === "agent"
      ? new Set(["mode", "file", "page"])
      : mode === "rtc"
        ? new Set(["mode", "fe", "call_id", "file", "page", "uid", "tk"])
        : new Set(["file", "page", "fresh"]);
  if ([...keys].some((key) => !allowedForMode.has(key))) {
    throw Object.assign(new Error("blocked: WebSocket parameters do not match mode"), {
      code: "BW_WS_OPERATION"
    });
  }
  const page = url.searchParams.get("page");
  const fe = url.searchParams.get("fe");
  const fresh = url.searchParams.get("fresh");
  const callId = url.searchParams.get("call_id") || "";
  const uid = url.searchParams.get("uid") || "";
  const ticket = url.searchParams.get("tk") || "";
  if (
    (page != null && (!/^\d{1,10}$/.test(page) || Number(page) > 1_000_000_000)) ||
    (fe != null && !/^[1-9]\d?$/.test(fe)) ||
    (fresh != null && !/^[01]$/.test(fresh)) ||
    String(url.searchParams.get("file") || "").length > 8192 ||
    callId.length > 256 ||
    uid.length > 256 ||
    ticket.length > 4096 ||
    (
      mode === "rtc" &&
      (
        fe !== "4" ||
        !/^[A-Za-z0-9._:-]{1,256}$/.test(callId) ||
        !/^[A-Za-z0-9._:@-]{1,256}$/.test(uid) ||
        !/^[a-f0-9]{32}$/.test(ticket)
      )
    )
  ) {
    throw Object.assign(new Error("blocked: invalid WebSocket parameters"), {
      code: "BW_WS_OPERATION"
    });
  }
  url.protocol = "wss:";
  return url.href;
}
function decodedBwWebSocketFrame(value) {
  const declared = Number(value?.bytes);
  const encoded = value?.b64;
  if (
    !Number.isSafeInteger(declared) ||
    declared < 0 ||
    declared > MAX_BW_WS_FRAME_BYTES ||
    typeof encoded !== "string" ||
    encoded.length > MAX_BW_WS_FRAME_B64_CHARS ||
    encoded.length % 4 !== 0 ||
    !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(encoded)
  ) {
    throw Object.assign(new Error("blocked: invalid WebSocket binary frame"), {
      code: "BW_WS_FRAME"
    });
  }
  let raw;
  try { raw = atob(encoded); } catch (_) { raw = null; }
  if (raw == null || raw.length !== declared) {
    throw Object.assign(new Error("blocked: WebSocket frame size mismatch"), {
      code: "BW_WS_FRAME"
    });
  }
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return bytes;
}
function encodedBwWebSocketFrame(value) {
  let bytes;
  if (value instanceof ArrayBuffer) {
    bytes = new Uint8Array(value);
  } else if (ArrayBuffer.isView(value)) {
    bytes = new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  } else {
    throw new TypeError("unsupported WebSocket binary frame");
  }
  if (bytes.byteLength > MAX_BW_WS_FRAME_BYTES) {
    throw new TypeError("WebSocket frame exceeds 8 MiB");
  }
  let raw = "";
  for (let i = 0; i < bytes.length; i += 0x8000) {
    raw += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return {
    binary: true,
    bytes: bytes.byteLength,
    b64: btoa(raw)
  };
}
const PRIVATE_CREDENTIAL_DB = "bw-reader-private-v1";
const PRIVATE_CREDENTIAL_STORE = "credentials";
const PRIVATE_CREDENTIAL_KEY = /^bw\.reader\.account\.v1:acct-v1-[a-f0-9]{64}:extension%3Acredentials-v1$/;
let privateCredentialDbPromise = null;
function openPrivateCredentialDb() {
  if (privateCredentialDbPromise) return privateCredentialDbPromise;
  privateCredentialDbPromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(PRIVATE_CREDENTIAL_DB, 1);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(PRIVATE_CREDENTIAL_STORE)) {
        db.createObjectStore(PRIVATE_CREDENTIAL_STORE);
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || new Error(
      "无法打开扩展私有凭据库"
    ));
    request.onblocked = () => reject(new Error("扩展私有凭据库被旧连接阻塞"));
  }).catch((error) => {
    privateCredentialDbPromise = null;
    throw error;
  });
  return privateCredentialDbPromise;
}
async function privateCredentialGet(key) {
  const db = await openPrivateCredentialDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(PRIVATE_CREDENTIAL_STORE, "readonly");
    const request = tx.objectStore(PRIVATE_CREDENTIAL_STORE).get(key);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error || tx.error);
  });
}
async function privateCredentialPut(key, value) {
  if (!PRIVATE_CREDENTIAL_KEY.test(key)) {
    throw Object.assign(new Error("拒绝写入非凭据私有键"), {
      code: "BW_ACCOUNT_STORAGE_BACKEND"
    });
  }
  const db = await openPrivateCredentialDb();
  await new Promise((resolve, reject) => {
    const tx = db.transaction(PRIVATE_CREDENTIAL_STORE, "readwrite");
    tx.objectStore(PRIVATE_CREDENTIAL_STORE).put(value, key);
    tx.oncomplete = () => resolve();
    tx.onabort = tx.onerror = () => reject(tx.error || new Error(
      "私有凭据写入失败"
    ));
  });
}
function credentialSummary(value) {
  const candidates = value?.candidates &&
    typeof value.candidates === "object" &&
    !Array.isArray(value.candidates)
    ? Object.values(value.candidates)
    : [];
  return {
    schema: 1,
    private: true,
    migratedAt: Date.now(),
    candidateCount: candidates.length,
    activeCandidateId: String(value?.activeCandidateId || "")
  };
}
async function migratePublicCredential(key, value) {
  if (!PRIVATE_CREDENTIAL_KEY.test(key) || !value || value.private === true) {
    return undefined;
  }
  const containsToken = Object.values(value?.candidates || {}).some(
    (candidate) => typeof candidate?.token === "string" && candidate.token
  );
  if (!containsToken) return undefined;
  await privateCredentialPut(key, value);
  const verified = await privateCredentialGet(key);
  if (!verified || verified.activeCandidateId !== value.activeCandidateId) {
    throw Object.assign(new Error("私有凭据迁移校验失败"), {
      code: "BW_ACCOUNT_STORAGE_BACKEND"
    });
  }
  // 不删除原键：用不含 token 的迁移存根替换，保留候选数量与 active id 供诊断。
  await chrome.storage.local.set({ [key]: credentialSummary(value) });
  return verified;
}
async function privateCredentialReadWithMigration(key) {
  const current = await privateCredentialGet(key);
  if (current !== undefined) return current;
  const publicStored = await chrome.storage.local.get(key);
  return migratePublicCredential(key, publicStored?.[key]);
}
const credentialStorage = Object.freeze({
  async get(keys) {
    const requested = Array.isArray(keys) ? keys : [keys];
    const out = {};
    const publicKeys = [];
    for (const key of requested.map(String)) {
      if (PRIVATE_CREDENTIAL_KEY.test(key)) {
        const value = await privateCredentialReadWithMigration(key);
        if (value !== undefined) out[key] = value;
      } else {
        publicKeys.push(key);
      }
    }
    if (publicKeys.length) {
      Object.assign(out, await chrome.storage.local.get(publicKeys));
    }
    return out;
  },
  async set(values) {
    for (const [key, value] of Object.entries(values || {})) {
      await privateCredentialPut(key, value);
      // 新凭据从一开始就只写私有 IndexedDB；公开区只保存无密文诊断存根。
      await chrome.storage.local.set({ [key]: credentialSummary(value) });
    }
  }
});
async function migratePrivateCredentialsAtStartup() {
  const stored = await chrome.storage.local.get(null);
  for (const [key, value] of Object.entries(stored || {})) {
    if (PRIVATE_CREDENTIAL_KEY.test(key)) {
      await migratePublicCredential(key, value);
    }
  }
}
const privateCredentialReady = migratePrivateCredentialsAtStartup().catch(
  () => {}
);
// Chromium 支持时，彻底禁止 content script 直接枚举 storage.local。网页功能通过
// 下方固定键网关访问非敏感数据；Safari 即使缺少该 API，token 也只存在私有 IndexedDB。
try {
  chrome.storage.local.setAccessLevel?.({ accessLevel: "TRUSTED_CONTEXTS" })
    ?.catch?.(() => {});
} catch (_) {}
const accountStorageFactory = globalThis.BWReaderExtension?.accountStorage;
if (
  !accountStorageFactory ||
  accountStorageFactory.CONTRACT !== "extension-account-storage/1"
) {
  throw new Error("扩展账户存储运行时不可用");
}
const accountStorage = accountStorageFactory.create({
  accountContext: globalThis.BWReaderRuntime?.accountContext,
  storage: credentialStorage,
  crypto
});
const PROVIDER_PROTOCOL = "bw-reader-services/1";
const TRUSTED_PWA_ORIGINS = new Set([
  "https://bwicarus.taile44d0c.ts.net"
]);
const TRUSTED_PWA_PATHS = new Set([
  "/pdf/view",
  "/pdf/epub/view",
  "/pdf/html/view",
  "/pdf/fav/open"
]);
const TRUSTED_PWA_HOST_KINDS = new Map([
  ["/pdf/view", "pdf"],
  ["/pdf/epub/view", "epub"],
  ["/pdf/html/view", "html"],
  ["/pdf/fav/open", "favorite"]
]);
const PWA_SYNC_OWNER_CLAIM_CONTRACT = "pwa-extension-owner-claim/1";
const PROVIDER_OPS = new Set([
  "get", "list", "put", "remove", "batch",
  "changes", "applyChanges", "status"
]);
const PROVIDER_SYNC_OPS = new Set([
  "syncStatus"
]);
const providerPorts = new Set();
const vocabularyStatePorts = new Set();
const documentNotePorts = new Set();
const providerVaults = new Map();
const providerSyncRuntimes = new Map();
const directHostPorts = new Set();
const directPeerBindings = new Map();
let activeDirectHost = null;
let directHostSequence = 0;
let directRpcSequence = 0;
// 文档便签不是 provider collection。它必须拥有独立的 IndexedDB、journal
// 与 BroadcastChannel；否则相同 mutationId 会和 provider 写入串扰，便签写入
// 也会推进 provider status/cursor。
const documentNoteVaults = new Map();
const VOCABULARY_STATE_PROTOCOL = "bw-vocabulary-state/1";
const DOCUMENT_NOTES_PROTOCOL = "bw-document-notes/1";
const PROVIDER_AUTH_KEY = "providerNamespaceAuthorizationsV2";
const EXTENSION_INSTALL_ID_KEY = "readerExtensionInstallIdV1";
const ACTIVE_VERIFIED_ACCOUNT_KEY = "readerActiveVerifiedAccountV1";
const PROVIDER_SYNC_ALARM = "bw-reader-provider-sync-v1";
const PROVIDER_SYNC_CHECKPOINT_CONTRACT = "provider-sync-checkpoint/2";
const PROVIDER_SYNC_CHECKPOINT_SCHEMA = 2;
const DIRECT_HOST_PROTOCOL = "bw-reader-direct-host/1";
const DIRECT_HOST_PORT = "bw-reader-direct-host";
const MAX_DIRECT_BRIDGE_BYTES = 2 * 1024 * 1024;
const DIRECT_RPC_TIMEOUT_MS = 30000;
const PROVIDER_AUTH_EXPIRY_SKEW_MS = 5000;
const PROVIDER_AUTH_TIMEOUT_MS = 12000;
const MAX_PROVIDER_REQUEST_BYTES = 512 * 1024;
const MAX_PROVIDER_RESPONSE_BYTES = 2 * 1024 * 1024;
const MAX_PROVIDER_MUTATIONS = 100;
const MAX_PROVIDER_PAGE = 200;
const MAX_DOCUMENT_NOTE_REQUEST_BYTES = 512 * 1024;
const MAX_DOCUMENT_NOTE_RESPONSE_BYTES = 2 * 1024 * 1024;
const MAX_DOCUMENT_NOTE_PAGE = 200;
let providerInstallIdPromise = null;
let providerPortSequence = 0;
const persistentAccountContext = newProviderAccountContext();
// 普通网页沿用最后一次经 PWA ticket 验证的账户，但仍必须受同一份生产
// DataRegistry 白名单约束。不能因为它没有活跃 provider port 就绕过
// collection 门禁，也不能复制一份手写 collection 名单。
const persistentProviderRegistry = providerRegistrySnapshot();
let persistentAccountTransition = Promise.resolve();
let persistentDeviceFamilyId = "";

function checkedDeviceFamilyId(value, optional = false) {
  value = String(value || "").trim();
  if (optional && !value) return "";
  if (!/^pwa-install-v1-[a-f0-9]{32}$/.test(value)) {
    throw Object.assign(new Error("PWA 设备族编号无效"), {
      code: "BW_SYNC_DEVICE_FAMILY",
      retryable: false
    });
  }
  return value;
}

function senderUrl(sender) {
  try { return new URL(sender?.tab?.url || sender?.url || ""); }
  catch (_) { return null; }
}
function isOwnContentSender(sender, trustedPwaOnly = false) {
  if (!sender || sender.id !== chrome.runtime.id) return false;
  const url = senderUrl(sender);
  if (!url || !["http:", "https:"].includes(url.protocol)) return false;
  if (!trustedPwaOnly) return true;
  return TRUSTED_PWA_ORIGINS.has(url.origin) && TRUSTED_PWA_PATHS.has(url.pathname);
}
function safeNamespace(value) {
  const accountContext = globalThis.BWReaderRuntime?.accountContext;
  if (
    !accountContext ||
    accountContext.CONTRACT !== "account-context/1" ||
    typeof accountContext.normalizeNamespace !== "function"
  ) {
    throw Object.assign(new Error("账户上下文运行时不可用"), {
      code: "BW_PROVIDER_ACCOUNT_CONTEXT"
    });
  }
  try {
    return accountContext.normalizeNamespace(value);
  } catch (cause) {
    throw Object.assign(new Error("无效的用户命名空间"), { code: "BW_PROVIDER_NAMESPACE" });
  }
}
function newProviderAccountContext() {
  const accountContext = globalThis.BWReaderRuntime?.accountContext;
  if (
    !accountContext ||
    accountContext.CONTRACT !== "account-context/1" ||
    typeof accountContext.createContext !== "function"
  ) {
    throw Object.assign(new Error("账户上下文运行时不可用"), {
      code: "BW_PROVIDER_ACCOUNT_CONTEXT"
    });
  }
  return accountContext.createContext();
}
const persistentAccountEntry = Object.freeze({
  accountContext: persistentAccountContext,
  providerCollections: persistentProviderRegistry.collections,
  syncCollections: persistentProviderRegistry.syncCollections,
  persistent: true
});
function serializePersistentAccount(operation) {
  const current = persistentAccountTransition
    .catch(() => {})
    .then(operation);
  persistentAccountTransition = current.catch(() => {});
  return current;
}
async function rememberVerifiedAccount(namespace, deviceFamilyId = "") {
  namespace = safeNamespace(namespace);
  deviceFamilyId = checkedDeviceFamilyId(deviceFamilyId, true);
  return serializePersistentAccount(async () => {
    const previous = persistentAccountContext.snapshot();
    const accountChanged = !previous.active || previous.namespace !== namespace;
    const nextDeviceFamilyId = deviceFamilyId || (
      !accountChanged ? persistentDeviceFamilyId : ""
    );
    const familyChanged =
      nextDeviceFamilyId !== persistentDeviceFamilyId;
    if (accountChanged || familyChanged) {
      destroyProviderSyncRuntimes("active-account-changed");
      revokeDirectHosts("active-account-changed");
    }
    if (accountChanged) {
      persistentAccountContext.activate({
        namespace,
        source: "provider-ticket"
      });
    }
    persistentDeviceFamilyId = nextDeviceFamilyId;
    try {
      await chrome.storage.local.set({
        [ACTIVE_VERIFIED_ACCOUNT_KEY]: {
          schema: 2,
          namespace,
          deviceFamilyId: persistentDeviceFamilyId,
          verifiedAt: Date.now(),
          source: "provider-ticket"
        }
      });
    } catch (error) {
      if (accountChanged) {
        persistentAccountContext.deactivate("active-account-persist-failed");
      }
      if (familyChanged) persistentDeviceFamilyId = "";
      throw Object.assign(new Error("无法保存扩展当前账户"), {
        code: "BW_ACCOUNT_STORAGE_BACKEND",
        cause: error
      });
    }
    if (accountChanged) pruneDocumentNoteVaults();
    if (accountChanged || familyChanged) {
      void startActiveProviderSync("active-account-paired", { runNow: true });
      void promoteDirectHost("active-account-changed");
    }
    return persistentAccountContext.snapshot();
  });
}
async function ensurePersistentAccount() {
  return serializePersistentAccount(async () => {
    const current = persistentAccountContext.snapshot();
    if (current.active) return current;
    const stored = await chrome.storage.local.get(ACTIVE_VERIFIED_ACCOUNT_KEY);
    const record = stored?.[ACTIVE_VERIFIED_ACCOUNT_KEY];
    if (
      !record ||
      ![1, 2].includes(Number(record.schema)) ||
      record.source !== "provider-ticket"
    ) {
      throw Object.assign(new Error(
        "请先打开一次已登录的 BW 书籍 PWA，让扩展确认当前账户"
      ), {
        code: "BW_ACCOUNT_CONTEXT_UNAVAILABLE"
      });
    }
    const namespace = safeNamespace(record.namespace);
    const deviceFamilyId = Number(record.schema) === 2
      ? checkedDeviceFamilyId(record.deviceFamilyId, true)
      : "";
    persistentAccountContext.activate({
      namespace,
      source: "provider-ticket"
    });
    persistentDeviceFamilyId = deviceFamilyId;
    return persistentAccountContext.snapshot();
  });
}
async function capturePersistentAccount() {
  await privateCredentialReady;
  await ensurePersistentAccount();
  return Object.freeze({
    kind: "persistent",
    entry: persistentAccountEntry,
    lease: persistentAccountContext.lease(),
    deviceFamilyId: persistentDeviceFamilyId
  });
}
function isTopLevelOwnContentSender(sender) {
  return !!(
    isOwnContentSender(sender, false) &&
    Number.isInteger(sender?.tab?.id) &&
    Number(sender?.frameId ?? 0) === 0
  );
}
function contentSenderBinding(sender) {
  const url = senderUrl(sender);
  if (!isTopLevelOwnContentSender(sender) || !url) {
    throw Object.assign(new Error("只允许扩展顶层网页脚本连接"), {
      code: "BW_VOCABULARY_STATE_SENDER"
    });
  }
  return Object.freeze({
    extensionId: String(sender.id || ""),
    tabId: sender.tab.id,
    frameId: Number(sender.frameId ?? 0),
    documentId: String(sender.documentId || ""),
    origin: url.origin,
    pathname: url.pathname
  });
}
function canonicalOrdinaryDocumentUrl(sender) {
  if (
    !sender ||
    sender.id !== chrome.runtime.id ||
    !Number.isInteger(sender?.tab?.id) ||
    Number(sender?.frameId ?? 0) !== 0
  ) {
    throw Object.assign(new Error("文档便签只允许扩展顶层网页脚本连接"), {
      code: "BW_DOCUMENT_NOTES_SENDER"
    });
  }
  let tabUrl;
  try { tabUrl = new URL(String(sender?.tab?.url || "")); }
  catch (_) { tabUrl = null; }
  if (
    !tabUrl ||
    !["http:", "https:"].includes(tabUrl.protocol) ||
    tabUrl.username ||
    tabUrl.password
  ) {
    throw Object.assign(new Error("当前标签页不能使用网页便签"), {
      code: "BW_DOCUMENT_NOTES_PAGE"
    });
  }
  if (
    TRUSTED_PWA_ORIGINS.has(tabUrl.origin) &&
    TRUSTED_PWA_PATHS.has(tabUrl.pathname)
  ) {
    throw Object.assign(new Error(
      "书籍 PWA 便签必须由 PWA 文档宿主保存，不能写入普通网页 Vault"
    ), {
      code: "BW_DOCUMENT_NOTES_PWA_HOST_REQUIRED"
    });
  }
  tabUrl.hash = "";
  const canonicalUrl = tabUrl.href;
  // Chromium/Safari 会同时提供顶层 frame URL。SPA pushState 后，新建
  // runtime Port 的 sender.url 仍可能是该 Document 的初始 URL，而
  // sender.tab.url 已是当前 history entry。两者同源时以浏览器提供的
  // tab.url 派生文档身份；facade 还会把 READY documentId 与页面实时
  // location 逐次核对。跨源不一致仍必须拒绝，避免旧文档导航竞态。
  if (sender.url) {
    let frameUrl;
    try {
      frameUrl = new URL(String(sender.url));
      frameUrl.hash = "";
    } catch (_) {
      frameUrl = null;
    }
    if (
      !frameUrl ||
      !["http:", "https:"].includes(frameUrl.protocol) ||
      frameUrl.username ||
      frameUrl.password ||
      frameUrl.origin !== tabUrl.origin
    ) {
      throw Object.assign(new Error("网页便签发送者 URL 与当前标签页不一致"), {
        code: "BW_DOCUMENT_NOTES_SENDER"
      });
    }
  }
  return canonicalUrl;
}
function documentNoteSenderBinding(sender) {
  const canonicalUrl = canonicalOrdinaryDocumentUrl(sender);
  return Object.freeze({
    extensionId: String(sender.id || ""),
    tabId: sender.tab.id,
    frameId: Number(sender.frameId ?? 0),
    browserDocumentId: String(sender.documentId || ""),
    canonicalUrl,
    documentId: "web:" + canonicalUrl
  });
}
function documentNoteBindingMatchesSender(binding, sender) {
  if (!binding || !sender) return false;
  // port.sender 在 Chromium 中通常是建连时快照：这里能拦截浏览器明确替换的
  // sender/documentId，但 SPA 的 pushState 必须由 facade 观察 URL 后主动断线重连。
  // 传输层绝不接受页面上报新 URL 来“续用”旧连接。
  let canonicalUrl;
  try { canonicalUrl = canonicalOrdinaryDocumentUrl(sender); }
  catch (_) { return false; }
  return !!(
    String(sender.id || "") === binding.extensionId &&
    Number(sender?.tab?.id) === binding.tabId &&
    Number(sender?.frameId ?? 0) === binding.frameId &&
    String(sender.documentId || "") === binding.browserDocumentId &&
    canonicalUrl === binding.canonicalUrl
  );
}
async function capturePersistentAccountForContentSender(sender) {
  if (!isTopLevelOwnContentSender(sender)) {
    throw Object.assign(new Error("账户网络只允许扩展的顶层网页脚本使用"), {
      code: "BW_ACCOUNT_PROVIDER_SENDER"
    });
  }
  return capturePersistentAccount();
}
function providerSenderBinding(sender) {
  const url = senderUrl(sender);
  if (
    !isOwnContentSender(sender, true) ||
    !Number.isInteger(sender?.tab?.id) ||
    Number(sender?.frameId ?? 0) !== 0 ||
    !url
  ) {
    throw Object.assign(new Error("provider 只允许可信 PWA 顶层文档连接"), {
      code: "BW_PROVIDER_SENDER"
    });
  }
  return Object.freeze({
    extensionId: String(sender.id || ""),
    tabId: sender.tab.id,
    frameId: Number(sender.frameId ?? 0),
    documentId: String(sender.documentId || ""),
    origin: url.origin,
    pathname: url.pathname,
    connectionNonce: `provider-port-${++providerPortSequence}`
  });
}
function providerBindingMatches(entry) {
  return providerBindingMatchesSender(entry?.binding, entry?.port?.sender);
}
function providerBindingMatchesSender(binding, sender) {
  const url = senderUrl(sender);
  return !!(
    binding &&
    sender &&
    url &&
    String(sender.id || "") === binding.extensionId &&
    Number(sender?.tab?.id) === binding.tabId &&
    Number(sender?.frameId ?? 0) === binding.frameId &&
    String(sender.documentId || "") === binding.documentId &&
    url.origin === binding.origin &&
    url.pathname === binding.pathname
  );
}
function checkedProviderSyncOwnerClaim(value, binding, registry) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  const expectedKeys = [
    "contract",
    "deviceFamilyId",
    "documentLifetime",
    "hostContract",
    "hostKind",
    "markerObserved",
    "pwaDirectOwner",
    "pwaServerOwner",
    "registryDigest",
    "runtimeContract",
    "syncChangeContract",
    "syncContract"
  ];
  if (
    JSON.stringify(Object.keys(value).sort()) !== JSON.stringify(expectedKeys) ||
    value.contract !== PWA_SYNC_OWNER_CLAIM_CONTRACT ||
    !/^pwa-install-v1-[a-f0-9]{32}$/.test(
      String(value.deviceFamilyId || "")
    ) ||
    value.runtimeContract !== "pwa-runtime/1" ||
    value.hostContract !== "document-host/1" ||
    value.hostKind !== TRUSTED_PWA_HOST_KINDS.get(binding?.pathname) ||
    value.markerObserved !== true ||
    value.documentLifetime !== true ||
    value.pwaServerOwner !== "paused" ||
    value.pwaDirectOwner !== "paused" ||
    value.syncContract !== registry.syncContract ||
    value.syncChangeContract !== registry.syncChangeContract ||
    value.registryDigest !== registry.syncDigest
  ) return null;
  return Object.freeze({
    contract: PWA_SYNC_OWNER_CLAIM_CONTRACT,
    deviceFamilyId: String(value.deviceFamilyId),
    runtimeContract: "pwa-runtime/1",
    hostContract: "document-host/1",
    hostKind: value.hostKind,
    markerObserved: true,
    documentLifetime: true,
    pwaServerOwner: "paused",
    pwaDirectOwner: "paused",
    syncContract: registry.syncContract,
    syncChangeContract: registry.syncChangeContract,
    registryDigest: registry.syncDigest
  });
}
function providerSyncOwnerClaimActive(entry, namespace) {
  return !!(
    entry &&
    !entry.closed &&
    providerPorts.has(entry) &&
    entry.authorized === true &&
    Number(entry.authorizationExpiresAtMs) > Date.now() &&
    entry.namespace === namespace &&
    entry.syncOwnerClaim?.contract === PWA_SYNC_OWNER_CLAIM_CONTRACT &&
    providerBindingMatches(entry)
  );
}
function activeProviderSyncOwnerClaims(namespace) {
  return [...providerPorts].filter((entry) =>
    providerSyncOwnerClaimActive(entry, namespace)
  );
}
function assertProviderSyncOwnerClaim(captured) {
  const namespace = String(captured?.lease?.namespace || "");
  if (captured?.kind === "persistent") {
    fenceCapturedAccount(captured);
    checkedDeviceFamilyId(captured.deviceFamilyId);
    return true;
  }
  const claims = activeProviderSyncOwnerClaims(namespace);
  if (
    !claims.length ||
    (
      !claims.includes(captured?.entry)
    )
  ) {
    throw Object.assign(new Error(
      "扩展后台同步等待书籍 PWA 的同文档所有权证明"
    ), {
      code: "BW_SYNC_OWNER_UNCLAIMED",
      retryable: true
    });
  }
  if (
    claims.some((entry) =>
      entry.syncOwnerClaim.deviceFamilyId !== persistentDeviceFamilyId
    )
  ) {
    throw Object.assign(new Error(
      "书籍 PWA 与扩展配对的设备族不一致"
    ), {
      code: "BW_SYNC_DEVICE_FAMILY",
      retryable: false
    });
  }
  return claims;
}
function providerTransportFence(entry, transportGeneration) {
  if (
    !entry ||
    entry.closed ||
    !providerPorts.has(entry) ||
    entry.transportGeneration !== transportGeneration
  ) {
    throw Object.assign(new Error("provider 页面连接已经失效"), {
      code: "BW_PROVIDER_DISCONNECTED"
    });
  }
  if (!providerBindingMatches(entry)) {
    throw Object.assign(new Error("provider 页面身份在连接期间发生变化"), {
      code: "BW_PROVIDER_SENDER"
    });
  }
}
function providerAccountFence(entry, transportGeneration, lease) {
  providerTransportFence(entry, transportGeneration);
  if (Number(entry.authorizationExpiresAtMs) <= Date.now()) {
    revokeProviderEntry(entry, "provider-authorization-expired");
    throw Object.assign(new Error("provider 授权已过期"), {
      code: "BW_PROVIDER_AUTH_EXPIRED"
    });
  }
  entry.accountContext.assertCurrent(lease);
}
function revokeProviderEntry(entry, reason) {
  if (!entry) return;
  const hadSyncOwnerClaim =
    entry.syncOwnerClaim?.contract === PWA_SYNC_OWNER_CLAIM_CONTRACT;
  entry.authorized = false;
  entry.namespace = "";
  entry.authorizationExpiresAtMs = 0;
  entry.providerCollections = null;
  entry.lease = null;
  entry.syncOwnerClaim = null;
  try { entry.accountContext?.deactivate(reason || "provider-revoked"); } catch (_) {}
  if (hadSyncOwnerClaim) {
    reconcileProviderSyncOwners(reason || "provider-owner-claim-revoked");
  }
}
function captureProviderEntry(entry) {
  if (!entry) {
    throw Object.assign(new Error("当前页面尚未连接扩展账户服务"), {
      code: "BW_ACCOUNT_PROVIDER_UNAVAILABLE"
    });
  }
  const transportGeneration = entry.transportGeneration;
  const lease = entry.accountContext.lease();
  providerAccountFence(entry, transportGeneration, lease);
  return Object.freeze({ entry, lease, transportGeneration });
}
function activeProviderEntries() {
  const nowMs = Date.now();
  const entries = [];
  for (const entry of providerPorts) {
    if (entry.authorized && Number(entry.authorizationExpiresAtMs) <= nowMs) {
      revokeProviderEntry(entry, "provider-authorization-expired");
      continue;
    }
    if (entry.authorized && providerBindingMatches(entry)) entries.push(entry);
  }
  return entries;
}
function captureProviderForContentSender(sender) {
  if (
    !isOwnContentSender(sender, true) ||
    !Number.isInteger(sender?.tab?.id) ||
    Number(sender?.frameId ?? 0) !== 0
  ) {
    throw Object.assign(new Error("账户操作只允许已验证的 PWA 顶层页面"), {
      code: "BW_ACCOUNT_PROVIDER_SENDER"
    });
  }
  const matches = activeProviderEntries().filter((entry) =>
    providerBindingMatchesSender(entry.binding, sender)
  );
  if (matches.length !== 1) {
    throw Object.assign(new Error(
      matches.length
        ? "当前页面存在多个账户服务连接，已拒绝猜测账户"
        : "当前页面尚未完成扩展账户授权"
    ), {
      code: matches.length
        ? "BW_ACCOUNT_PROVIDER_AMBIGUOUS"
        : "BW_ACCOUNT_PROVIDER_UNAVAILABLE"
    });
  }
  return captureProviderEntry(matches[0]);
}
function isPopupSender(sender) {
  if (!sender || sender.id !== chrome.runtime.id || sender.tab) return false;
  const url = senderUrl(sender);
  return !!(
    url &&
    ["chrome-extension:", "moz-extension:", "safari-web-extension:"].includes(url.protocol) &&
    /\/popup\.html$/.test(url.pathname)
  );
}
async function captureProviderForPopup(sender, target) {
  if (!isPopupSender(sender)) {
    throw Object.assign(new Error("账户凭据只能由扩展弹窗管理"), {
      code: "BW_ACCOUNT_POPUP_REQUIRED"
    });
  }
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  const activeTab = Array.isArray(tabs) ? tabs[0] : null;
  if (!Number.isInteger(activeTab?.id)) {
    throw Object.assign(new Error("找不到当前活动页面"), {
      code: "BW_ACCOUNT_ACTIVE_TAB"
    });
  }
  if (!Number.isInteger(target?.tabId) || target.tabId !== activeTab.id) {
    throw Object.assign(new Error("弹窗目标已不是当前活动页面，请重新打开弹窗"), {
      code: "BW_ACCOUNT_ACTIVE_TAB"
    });
  }
  if ((Number.isInteger(target?.frameId) ? target.frameId : 0) !== 0) {
    throw Object.assign(new Error("账户凭据只能由顶层页面的扩展弹窗管理"), {
      code: "BW_ACCOUNT_ACTIVE_TAB"
    });
  }
  // 当前账户在书籍 PWA 完成一次 provider 证明后持久保存；弹窗此后可在任意
  // 网页打开，不再要求同一标签页维持一个 PWA provider lease。
  return capturePersistentAccount();
}
function fenceCapturedAccount(captured) {
  if (captured?.kind === "persistent") {
    captured.entry.accountContext.assertCurrent(captured.lease);
    if (
      String(captured.deviceFamilyId || "") !==
      String(persistentDeviceFamilyId || "")
    ) {
      throw Object.assign(new Error("扩展同步设备族在操作期间发生变化"), {
        code: "BW_ACCOUNT_CONTEXT_STALE",
        retryable: false
      });
    }
    return;
  }
  providerAccountFence(
    captured.entry,
    captured.transportGeneration,
    captured.lease
  );
}
function parseProviderTicket(value) {
  value = String(value || "").trim();
  const match = /^pvt-v2-([0-9]{10,12})-([a-f0-9]{32})-([a-f0-9]{64})$/.exec(value);
  if (!match) {
    throw Object.assign(new Error("无效的 provider 授权证明"), { code: "BW_PROVIDER_AUTH" });
  }
  return { ticket: value, expiresAt: Number(match[1]) };
}
async function sha256Hex(value) {
  const bytes = new TextEncoder().encode(String(value || ""));
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}
async function authorizeProviderNamespace(namespace, ticket) {
  namespace = safeNamespace(namespace);
  const parsedTicket = parseProviderTicket(ticket);
  const ticketHash = await sha256Hex(parsedTicket.ticket);
  const stored = await chrome.storage.local.get(PROVIDER_AUTH_KEY);
  const authorizations = stored[PROVIDER_AUTH_KEY] || {};
  const nowMs = Date.now();
  const cached = authorizations[namespace];
  if (
    cached?.ticketHash === ticketHash &&
    Number(cached.expiresAt) === parsedTicket.expiresAt &&
    Number(cached.validUntilMs) > nowMs + PROVIDER_AUTH_EXPIRY_SKEW_MS
  ) {
    return {
      cached: true,
      expiresAt: parsedTicket.expiresAt,
      validUntilMs: Number(cached.validUntilMs)
    };
  }
  let response;
  const authorizationController = new AbortController();
  const authorizationTimeout = setTimeout(
    () => authorizationController.abort(),
    PROVIDER_AUTH_TIMEOUT_MS
  );
  try {
    response = await fetch(ORIGIN + "/api/reader/provider-authorize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ namespace, ticket: parsedTicket.ticket }),
      credentials: "omit",
      cache: "no-store",
      signal: authorizationController.signal
    });
  } catch (error) {
    throw Object.assign(new Error(
      error?.name === "AbortError"
        ? "扩展 Vault 授权请求超时"
        : "首次授权扩展 Vault 时需要连接服务器"
    ), {
      code: "BW_PROVIDER_AUTH_UNAVAILABLE",
      cause: error
    });
  } finally {
    clearTimeout(authorizationTimeout);
  }
  const contentType = response.headers.get("Content-Type") || "";
  const data = contentType.includes("application/json")
    ? await response.json()
    : { ok: false, error: "authorization returned non-JSON response" };
  const responseExpiresAt = Number(data.expires_at);
  const responseExpiresIn = Number(data.expires_in);
  if (
    !response.ok ||
    data.ok !== true ||
    data.storage_namespace !== namespace ||
    !Number.isSafeInteger(responseExpiresAt) ||
    responseExpiresAt !== parsedTicket.expiresAt ||
    !Number.isFinite(responseExpiresIn) ||
    responseExpiresIn <= 0
  ) {
    throw Object.assign(new Error(data.error || "provider 授权失败"), {
      code: "BW_PROVIDER_AUTH"
    });
  }
  const authorizedAt = Date.now();
  const validUntilMs = Math.min(
    responseExpiresAt * 1000,
    authorizedAt + Math.floor(responseExpiresIn * 1000)
  );
  if (validUntilMs <= authorizedAt + PROVIDER_AUTH_EXPIRY_SKEW_MS) {
    throw Object.assign(new Error("provider 授权证明已过期"), {
      code: "BW_PROVIDER_AUTH"
    });
  }
  authorizations[namespace] = {
    ticketHash,
    authorizedAt,
    expiresAt: responseExpiresAt,
    validUntilMs
  };
  const ordered = Object.entries(authorizations)
    .filter((entry) =>
      Number(entry[1]?.validUntilMs) > authorizedAt + PROVIDER_AUTH_EXPIRY_SKEW_MS
    )
    .sort((left, right) => Number(right[1]?.authorizedAt || 0) - Number(left[1]?.authorizedAt || 0))
    .slice(0, 16);
  await chrome.storage.local.set({ [PROVIDER_AUTH_KEY]: Object.fromEntries(ordered) });
  return {
    cached: false,
    expiresAt: responseExpiresAt,
    validUntilMs
  };
}
function validExtensionInstallId(value) {
  return /^extension-install-v1-[a-f0-9]{32}$/.test(String(value || ""));
}
function newExtensionInstallId() {
  const bytes = new Uint8Array(16);
  crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  return "extension-install-v1-" + Array.from(bytes)
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}
async function extensionInstallId() {
  if (providerInstallIdPromise) return providerInstallIdPromise;
  providerInstallIdPromise = (async () => {
    const stored = await chrome.storage.local.get(EXTENSION_INSTALL_ID_KEY);
    let installId = String(stored[EXTENSION_INSTALL_ID_KEY] || "").trim();
    if (validExtensionInstallId(installId)) return installId;
    installId = newExtensionInstallId();
    await chrome.storage.local.set({ [EXTENSION_INSTALL_ID_KEY]: installId });
    const verified = await chrome.storage.local.get(EXTENSION_INSTALL_ID_KEY);
    const persisted = String(verified[EXTENSION_INSTALL_ID_KEY] || "").trim();
    if (!validExtensionInstallId(persisted)) {
      throw Object.assign(new Error("无法持久化扩展安装编号"), {
        code: "BW_PROVIDER_DEVICE_ID"
      });
    }
    return persisted;
  })().catch((error) => {
    providerInstallIdPromise = null;
    throw error;
  });
  return providerInstallIdPromise;
}
function providerRegistrySnapshot() {
  const registry = globalThis.BWReaderRuntime?.dataRegistry;
  if (
    !registry ||
    registry.CONTRACT !== "data-registry/1" ||
    registry.SYNC_CONTRACT !== "sync-v3" ||
    registry.SYNC_CHANGE_CONTRACT !== "record-parent-state/1" ||
    typeof registry.scopes !== "function" ||
    typeof registry.collection !== "function" ||
    typeof registry.providerCollections !== "function" ||
    typeof registry.syncCollections !== "function" ||
    typeof registry.isSyncCollection !== "function" ||
    typeof registry.syncDescriptor !== "function" ||
    typeof registry.syncDigest !== "function"
  ) {
    throw Object.assign(new Error(
      "扩展缺少完整 DataRegistry，已拒绝打开 provider Vault"
    ), { code: "BW_PROVIDER_REGISTRY" });
  }
  const scopes = registry.scopes();
  let declared;
  let declaredSync;
  let syncDescriptor;
  let syncDigest;
  try {
    declared = registry.providerCollections();
    declaredSync = registry.syncCollections();
    syncDescriptor = registry.syncDescriptor();
    syncDigest = String(registry.syncDigest() || "");
  } catch (error) {
    throw Object.assign(new Error("无法读取 DataRegistry provider/sync-v3 因果合同"), {
      code: "BW_PROVIDER_REGISTRY",
      cause: error
    });
  }
  if (
    !scopes ||
    typeof scopes !== "object" ||
    Array.isArray(scopes) ||
    !Array.isArray(declared) ||
    !Array.isArray(declaredSync) ||
    !Array.isArray(syncDescriptor)
  ) {
    throw Object.assign(new Error("DataRegistry provider/sync-v3 因果合同无效"), {
      code: "BW_PROVIDER_REGISTRY"
    });
  }
  const names = [];
  const seen = new Set();
  for (const rawName of declared) {
    const name = String(rawName || "").trim();
    if (!name || seen.has(name)) {
      throw Object.assign(new Error("DataRegistry provider 白名单包含空项或重复项"), {
        code: "BW_PROVIDER_REGISTRY"
      });
    }
    const entry = registry.collection(name);
    if (
      !Object.prototype.hasOwnProperty.call(scopes, name) ||
      !entry ||
      entry.scope !== "global" ||
      entry.status !== "ready" ||
      entry.provider !== true
    ) {
      throw Object.assign(new Error(
        "DataRegistry provider 白名单包含未知或未开放 collection：" + name
      ), {
        code: "BW_PROVIDER_REGISTRY",
        details: { collection: name }
      });
    }
    seen.add(name);
    names.push(name);
  }
  names.sort();
  const expected = Object.keys(scopes).filter((name) => {
    const entry = scopes[name] || {};
    return entry.scope === "global" &&
      entry.status === "ready" &&
      entry.provider === true;
  }).sort();
  if (JSON.stringify(names) !== JSON.stringify(expected)) {
    throw Object.assign(new Error(
      "DataRegistry.providerCollections 与 collection 归属表不一致"
    ), {
      code: "BW_PROVIDER_REGISTRY",
      details: { declared: names, expected }
    });
  }
  const syncNames = [];
  const syncSeen = new Set();
  for (const rawName of declaredSync) {
    const name = String(rawName || "").trim();
    const entry = registry.collection(name);
    if (
      !name ||
      syncSeen.has(name) ||
      !seen.has(name) ||
      !entry ||
      entry.scope !== "global" ||
      entry.status !== "ready" ||
      entry.provider !== true ||
      entry.sync !== true ||
      registry.isSyncCollection(name) !== true
    ) {
      throw Object.assign(new Error(
        "DataRegistry 同步白名单包含未知或未开放 collection：" + name
      ), {
        code: "BW_PROVIDER_REGISTRY",
        details: { collection: name }
      });
    }
    syncSeen.add(name);
    syncNames.push(name);
  }
  syncNames.sort();
  const expectedSync = Object.keys(scopes).filter((name) => {
    const entry = scopes[name] || {};
    return entry.scope === "global" &&
      entry.status === "ready" &&
      entry.provider === true &&
      entry.sync === true;
  }).sort();
  if (JSON.stringify(syncNames) !== JSON.stringify(expectedSync)) {
    throw Object.assign(new Error(
      "DataRegistry.syncCollections 与 collection 归属表不一致"
    ), {
      code: "BW_PROVIDER_REGISTRY",
      details: { declared: syncNames, expected: expectedSync }
    });
  }
  syncDescriptor = syncDescriptor.map((raw, index) => {
    const item = raw && typeof raw === "object" && !Array.isArray(raw)
      ? structuredClone(raw)
      : null;
    const expectedName = syncNames[index];
    const authority = registry.collection(expectedName);
    if (
      !item ||
      JSON.stringify(Object.keys(item).sort()) !==
        JSON.stringify(["conflictPolicy", "derived", "name", "recordSchema"]) ||
      item.name !== expectedName ||
      !/^[A-Za-z0-9._-]+$/.test(String(expectedName || "")) ||
      typeof item.conflictPolicy !== "string" ||
      !item.conflictPolicy.trim() ||
      !/^[A-Za-z0-9._-]+$/.test(item.conflictPolicy) ||
      typeof item.derived !== "boolean" ||
      !Number.isInteger(item.recordSchema) ||
      item.recordSchema < 1 ||
      !authority ||
      String(authority.conflictPolicy || "") !== item.conflictPolicy ||
      (authority.derived === true) !== item.derived ||
      Number(authority.recordSchema) !== item.recordSchema
    ) {
      throw Object.assign(new Error(
        "DataRegistry 同步描述符与 collection 元数据不一致：" +
          String(expectedName || "")
      ), { code: "BW_PROVIDER_REGISTRY" });
    }
    return item;
  });
  const expectedDigest =
    "sync-v3:record-parent-state/1|" +
    syncDescriptor.map((item) => [
      item.name,
      item.conflictPolicy,
      item.derived ? "1" : "0",
      String(item.recordSchema)
    ].join(":")).join("|");
  if (
    syncDescriptor.length !== syncNames.length ||
    syncDigest !== expectedDigest
  ) {
    throw Object.assign(new Error(
      "DataRegistry sync-v3 摘要与描述符不一致"
    ), { code: "BW_PROVIDER_REGISTRY" });
  }
  return {
    names,
    collections: new Set(names),
    syncNames,
    syncCollections: new Set(syncNames),
    syncDescriptor,
    syncDigest,
    syncContract: registry.SYNC_CONTRACT,
    syncChangeContract: registry.SYNC_CHANGE_CONTRACT
  };
}
async function vaultFor(namespace) {
  namespace = safeNamespace(namespace);
  if (providerVaults.has(namespace)) return providerVaults.get(namespace);
  const deviceId = await extensionInstallId();
  // 两个页面可能同时越过第一次检查并等待安装编号；恢复后必须再次检查，
  // 保证同一 namespace 只创建一个 store 与一条 subscribe。
  if (providerVaults.has(namespace)) return providerVaults.get(namespace);
  const indexed = globalThis.BWReaderRuntime?.indexedDBStore;
  if (!indexed?.createIndexedDBDataStore) {
    throw Object.assign(new Error("扩展 IndexedDB Vault 未加载"), { code: "BW_PROVIDER_DEPENDENCY" });
  }
  const store = indexed.createIndexedDBDataStore({
    dbName: "bw-reader-extension-vault-v1-" + namespace,
    deviceId,
    channelName: "bw-reader-extension-vault-events-" + namespace,
    causalCollections: persistentProviderRegistry.syncNames
  });
  store.subscribe({}, (change) => broadcastProviderChange(namespace, change));
  providerVaults.set(namespace, store);
  return store;
}
function destroyProviderSyncRuntimes(reason) {
  for (const entry of providerSyncRuntimes.values()) {
    try {
      void entry.ownerLease?.destroy(String(reason || "provider-sync-reset"));
    } catch (_) {}
    try {
      entry.runtime.destroy(String(reason || "provider-sync-reset"));
    } catch (_) {}
  }
  providerSyncRuntimes.clear();
}
function destroyProviderSyncRuntime(namespace, reason) {
  namespace = String(namespace || "");
  const entry = providerSyncRuntimes.get(namespace);
  if (!entry) return false;
  providerSyncRuntimes.delete(namespace);
  try {
    void entry.ownerLease?.destroy(
      String(reason || "provider-sync-owner-released")
    );
  } catch (_) {}
  try {
    entry.runtime.destroy(String(reason || "provider-sync-owner-released"));
  } catch (_) {}
  return true;
}
function reconcileProviderSyncOwners(reason) {
  for (const namespace of [...providerSyncRuntimes.keys()]) {
    if (
      namespace !== persistentAccountContext.snapshot().namespace ||
      !/^pwa-install-v1-[a-f0-9]{32}$/.test(persistentDeviceFamilyId)
    ) {
      destroyProviderSyncRuntime(
        namespace,
        reason || "provider-sync-pairing-released"
      );
    }
  }
  let revokedDirect = false;
  for (const entry of directHostPorts) {
    if (entry.ready && !directHostOwnerClaimMatches(entry)) {
      if (activeDirectHost === entry) activeDirectHost = null;
      revokeDirectEntry(
        entry,
        reason || "provider-sync-owner-released"
      );
      revokedDirect = true;
    }
  }
  const activeNamespace = persistentAccountContext.snapshot().namespace;
  if (
    revokedDirect ||
    (
      activeNamespace &&
      /^pwa-install-v1-[a-f0-9]{32}$/.test(persistentDeviceFamilyId)
    )
  ) {
    void promoteDirectHost(
      reason || "provider-sync-owner-reconciled"
    );
  }
}
function providerSyncCheckpointError(
  message,
  code = "BW_SYNC_CHECKPOINT",
  retryable = false
) {
  return Object.assign(new Error(String(message || "扩展同步游标损坏")), {
    code,
    retryable: retryable === true
  });
}
function checkedProviderVaultEpoch(value) {
  value = String(value || "");
  if (!/^data-store-instance-v1-[a-f0-9]{32}$/.test(value)) {
    throw providerSyncCheckpointError(
      "扩展数据 Vault 实例编号无效，已停止增量同步",
      "BW_DATA_INSTANCE_EPOCH"
    );
  }
  return value;
}
function checkedProviderCoordinatorCheckpoint(value) {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    value.contract !== "sync-coordinator/1"
  ) {
    throw providerSyncCheckpointError(
      "扩展同步游标损坏，已停止增量同步"
    );
  }
  return structuredClone(value);
}
function providerCheckpointFromEnvelope(value, vaultEpoch) {
  if (
    value &&
    typeof value === "object" &&
    !Array.isArray(value) &&
    value.contract === "sync-coordinator/1"
  ) {
    /* v1 checkpoint 没有 Vault epoch，继续使用会在 DB 重建后跳过远端历史。 */
    return null;
  }
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    value.contract !== PROVIDER_SYNC_CHECKPOINT_CONTRACT ||
    value.schema !== PROVIDER_SYNC_CHECKPOINT_SCHEMA ||
    typeof value.vaultEpoch !== "string"
  ) {
    throw providerSyncCheckpointError(
      "扩展同步游标损坏，已停止增量同步"
    );
  }
  if (value.vaultEpoch !== vaultEpoch) return null;
  return checkedProviderCoordinatorCheckpoint(value.checkpoint);
}
function providerSyncCheckpointStore(captured, store) {
  fenceCapturedAccount(captured);
  assertProviderSyncOwnerClaim(captured);
  if (!store || typeof store.instanceEpoch !== "function") {
    throw providerSyncCheckpointError(
      "扩展数据 Vault 缺少实例编号接口",
      "BW_DATA_INSTANCE_EPOCH"
    );
  }
  const context = captured.entry.accountContext;
  const lease = captured.lease;
  const key = context.namespacedKey(
    "extension:sync-checkpoint-v1",
    lease
  );
  let observedVaultEpoch = "";
  const readVaultEpoch = async () => {
    fenceCapturedAccount(captured);
    assertProviderSyncOwnerClaim(captured);
    const value = checkedProviderVaultEpoch(await store.instanceEpoch());
    fenceCapturedAccount(captured);
    assertProviderSyncOwnerClaim(captured);
    return value;
  };
  return {
    async load() {
      fenceCapturedAccount(captured);
      assertProviderSyncOwnerClaim(captured);
      const vaultEpoch = await readVaultEpoch();
      const stored = await chrome.storage.local.get(key);
      fenceCapturedAccount(captured);
      assertProviderSyncOwnerClaim(captured);
      const confirmedEpoch = await readVaultEpoch();
      if (confirmedEpoch !== vaultEpoch) {
        observedVaultEpoch = confirmedEpoch;
        return null;
      }
      observedVaultEpoch = vaultEpoch;
      const value = stored?.[key];
      if (value == null) return null;
      return providerCheckpointFromEnvelope(value, vaultEpoch);
    },
    async save(value) {
      fenceCapturedAccount(captured);
      assertProviderSyncOwnerClaim(captured);
      const vaultEpoch = await readVaultEpoch();
      if (observedVaultEpoch && observedVaultEpoch !== vaultEpoch) {
        throw providerSyncCheckpointError(
          "扩展数据 Vault 已重建，请重新启动同步",
          "BW_SYNC_CHECKPOINT_EPOCH",
          true
        );
      }
      observedVaultEpoch = vaultEpoch;
      const out = {
        [key]: {
          contract: PROVIDER_SYNC_CHECKPOINT_CONTRACT,
          schema: PROVIDER_SYNC_CHECKPOINT_SCHEMA,
          vaultEpoch,
          checkpoint: checkedProviderCoordinatorCheckpoint(value)
        }
      };
      await chrome.storage.local.set(out);
      fenceCapturedAccount(captured);
      assertProviderSyncOwnerClaim(captured);
      const confirmedEpoch = await readVaultEpoch();
      if (confirmedEpoch !== vaultEpoch) {
        throw providerSyncCheckpointError(
          "扩展数据 Vault 已重建，请重新启动同步",
          "BW_SYNC_CHECKPOINT_EPOCH",
          true
        );
      }
    }
  };
}
function providerServerSyncTransport(captured, deviceId, ownerLease) {
  const send = async (path, request) => {
    fenceCapturedAccount(captured);
    assertProviderSyncOwnerClaim(captured);
    const owner = await ownerLease.ensureActive();
    fenceCapturedAccount(captured);
    ownerLease.assertActive();
    const body = Object.assign({}, structuredClone(request || {}), {
      contract: "sync-gateway/2",
      ownerNamespace: captured.lease.namespace,
      deviceId,
      syncContract: persistentProviderRegistry.syncContract,
      syncChangeContract: persistentProviderRegistry.syncChangeContract,
      registryDigest: persistentProviderRegistry.syncDigest
    }, owner);
    const result = await apiRequest(path, {
      method: "POST",
      body: JSON.stringify(body)
    }, captured);
    fenceCapturedAccount(captured);
    assertProviderSyncOwnerClaim(captured);
    ownerLease.assertActive();
    return result;
  };
  return {
    exchange(request) {
      return send("/api/reader/sync/exchange", request);
    },
    snapshot(request) {
      return send("/api/reader/sync/snapshot", request);
    }
  };
}
async function providerSyncRuntimeFor(requestCaptured) {
  fenceCapturedAccount(requestCaptured);
  assertProviderSyncOwnerClaim(requestCaptured);
  /*
   * Store/checkpoint identity is persistent.  Network ownership is a separate
   * authenticated server lease scoped to the paired PWA device family, so an
   * MV3 worker wakeup may safely resume without depending on a live PWA tab.
   */
  const captured = requestCaptured.kind === "persistent"
    ? requestCaptured
    : await capturePersistentAccount();
  fenceCapturedAccount(requestCaptured);
  fenceCapturedAccount(captured);
  assertProviderSyncOwnerClaim(requestCaptured);
  assertProviderSyncOwnerClaim(captured);
  if (captured.lease.namespace !== requestCaptured.lease.namespace) {
    throw Object.assign(new Error("同步账户在请求期间发生变化"), {
      code: "BW_ACCOUNT_CONTEXT_STALE",
      retryable: false
    });
  }
  const namespace = captured.lease.namespace;
  const existing = providerSyncRuntimes.get(namespace);
  if (existing) {
    try {
      fenceCapturedAccount(existing.captured);
      fenceCapturedAccount(requestCaptured);
      assertProviderSyncOwnerClaim(requestCaptured);
      assertProviderSyncOwnerClaim(existing.captured);
      if (
        existing.deviceFamilyId !==
        checkedDeviceFamilyId(captured.deviceFamilyId)
      ) {
        throw Object.assign(new Error("同步设备族已经变化"), {
          code: "BW_ACCOUNT_CONTEXT_STALE"
        });
      }
      return existing.runtime;
    } catch (_) {
      try { existing.runtime.destroy("stale-account-lease"); } catch (_) {}
      try {
        void existing.ownerLease?.destroy("stale-account-lease");
      } catch (_) {}
      providerSyncRuntimes.delete(namespace);
    }
  }
  const modules = globalThis.BWReaderRuntime || {};
  const syncGateway = modules.syncGateway;
  const syncCoordinator = modules.syncCoordinator;
  const syncRuntime = modules.syncRuntime;
  const syncConflictControl = modules.syncConflictControl;
  const registry = modules.dataRegistry;
  const syncOwnerLease = modules.syncOwnerLease;
  if (
    !syncGateway?.createSyncGateway ||
    syncGateway.CONTRACT !== "sync-gateway/2" ||
    !syncCoordinator?.createSyncCoordinator ||
    syncCoordinator.CONTRACT !== "sync-coordinator/1" ||
    !syncRuntime?.createSyncRuntime ||
    syncRuntime.CONTRACT !== "sync-runtime/1" ||
    !syncConflictControl?.createSyncConflictControl ||
    syncConflictControl.CONTRACT !== "sync-conflict-control/1" ||
    !syncOwnerLease?.createSyncOwnerLease ||
    syncOwnerLease.CONTRACT !== "owner-lease/1"
  ) {
    throw Object.assign(new Error("扩展同步运行时依赖不完整"), {
      code: "BW_SYNC_RUNTIME_DEPENDENCY",
      retryable: false
    });
  }
  const deviceId = await extensionInstallId();
  const deviceFamilyId = checkedDeviceFamilyId(captured.deviceFamilyId);
  fenceCapturedAccount(requestCaptured);
  fenceCapturedAccount(captured);
  assertProviderSyncOwnerClaim(requestCaptured);
  assertProviderSyncOwnerClaim(captured);
  const store = await vaultFor(namespace);
  fenceCapturedAccount(requestCaptured);
  fenceCapturedAccount(captured);
  assertProviderSyncOwnerClaim(requestCaptured);
  assertProviderSyncOwnerClaim(captured);
  /*
   * Several MV3 wake sources can enter above awaits together. Re-check after
   * the final await so only one subscribed SyncRuntime becomes the namespace
   * owner; a later contender reuses that owner instead of orphaning another
   * runtime behind the Map entry.
   */
  const raced = providerSyncRuntimes.get(namespace);
  if (raced) {
    try {
      fenceCapturedAccount(raced.captured);
      fenceCapturedAccount(requestCaptured);
      assertProviderSyncOwnerClaim(requestCaptured);
      assertProviderSyncOwnerClaim(raced.captured);
      if (
        raced.deviceFamilyId !==
        checkedDeviceFamilyId(captured.deviceFamilyId)
      ) {
        throw Object.assign(new Error("同步设备族已经变化"), {
          code: "BW_ACCOUNT_CONTEXT_STALE"
        });
      }
      return raced.runtime;
    } catch (_) {
      try { raced.runtime.destroy("stale-account-lease"); } catch (_) {}
      try {
        void raced.ownerLease?.destroy("stale-account-lease");
      } catch (_) {}
      providerSyncRuntimes.delete(namespace);
    }
  }
  const ownerLease = syncOwnerLease.createSyncOwnerLease({
    ownerNamespace: namespace,
    deviceId,
    deviceFamilyId,
    ownerRole: "extension",
    syncContract: persistentProviderRegistry.syncContract,
    syncChangeContract: persistentProviderRegistry.syncChangeContract,
    registryDigest: persistentProviderRegistry.syncDigest,
    crypto: globalThis.crypto,
    request(path, body) {
      fenceCapturedAccount(captured);
      return apiRequest(path, {
        method: "POST",
        body: JSON.stringify(body)
      }, captured);
    },
    onAcquired() {
      const active = providerSyncRuntimes.get(namespace);
      if (!active || active.ownerLease !== ownerLease) return;
      try {
        fenceCapturedAccount(active.captured);
        active.runtime.resume("owner-lease-acquired");
        active.runtime.schedule("owner-lease-acquired", 0);
        void promoteDirectHost("owner-lease-acquired");
      } catch (_) {}
    },
    onLost() {
      const active = providerSyncRuntimes.get(namespace);
      if (!active || active.ownerLease !== ownerLease) return;
      try { active.runtime.pause("owner-lease-lost"); } catch (_) {}
      revokeDirectHosts("owner-lease-lost");
    }
  });
  const gateway = syncGateway.createSyncGateway({
    transport: providerServerSyncTransport(captured, deviceId, ownerLease),
    deviceId
  });
  const runtime = syncRuntime.createSyncRuntime({
    coordinatorApi: syncCoordinator,
    store,
    registry,
    serverGateway: gateway,
    checkpointStore: providerSyncCheckpointStore(captured, store),
    intervalMs: 60000,
    debounceMs: 250,
    retryMinMs: 5000,
    retryMaxMs: 120000,
    assertLease() {
      fenceCapturedAccount(captured);
      assertProviderSyncOwnerClaim(captured);
      ownerLease.assertActive();
      return true;
    }
  });
  let control;
  try {
    control = syncConflictControl.createSyncConflictControl({
      runtime,
      owner: "extension-background",
      crypto: globalThis.crypto,
      assertFence() {
        fenceCapturedAccount(captured);
        assertProviderSyncOwnerClaim(captured);
        const active = providerSyncRuntimes.get(namespace);
        if (
          !active ||
          active.runtime !== runtime ||
          active.ownerLease !== ownerLease
        ) {
          throw Object.assign(new Error("扩展同步运行时已经失效"), {
            code: "BW_ACCOUNT_CONTEXT_STALE",
            retryable: false
          });
        }
        return true;
      }
    });
  } catch (error) {
    try { runtime.destroy("conflict-control-create-failed"); } catch (_) {}
    throw error;
  }
  const entry = {
    captured,
    runtime,
    control,
    gateway,
    deviceId,
    deviceFamilyId,
    ownerLease
  };
  providerSyncRuntimes.set(namespace, entry);
  try {
    fenceCapturedAccount(requestCaptured);
    fenceCapturedAccount(captured);
    assertProviderSyncOwnerClaim(requestCaptured);
    assertProviderSyncOwnerClaim(captured);
  } catch (error) {
    if (providerSyncRuntimes.get(namespace) === entry) {
      providerSyncRuntimes.delete(namespace);
    }
    try { runtime.destroy("provider-owner-claim-lost"); } catch (_) {}
    try { void ownerLease.destroy("provider-owner-create-failed"); } catch (_) {}
    throw error;
  }
  return runtime;
}
async function providerSyncControlFor(captured) {
  fenceCapturedAccount(captured);
  assertProviderSyncOwnerClaim(captured);
  await providerSyncRuntimeFor(captured);
  fenceCapturedAccount(captured);
  assertProviderSyncOwnerClaim(captured);
  const entry = providerSyncRuntimes.get(captured.lease.namespace);
  if (
    !entry ||
    !entry.control ||
    entry.control.contract !== "sync-conflict-control/1"
  ) {
    throw Object.assign(new Error("扩展同步冲突控制不可用"), {
      code: "BW_SYNC_CONFLICT_DEPENDENCY",
      retryable: false
    });
  }
  fenceCapturedAccount(entry.captured);
  assertProviderSyncOwnerClaim(entry.captured);
  return entry.control;
}
async function startActiveProviderSync(reason, options = {}) {
  try {
    const captured = options.captured || await capturePersistentAccount();
    fenceCapturedAccount(captured);
    assertProviderSyncOwnerClaim(captured);
    const runtime = await providerSyncRuntimeFor(captured);
    fenceCapturedAccount(captured);
    assertProviderSyncOwnerClaim(captured);
    const entry = providerSyncRuntimes.get(captured.lease.namespace);
    if (!entry || entry.runtime !== runtime || !entry.ownerLease) {
      throw Object.assign(new Error("扩展同步 owner lease 不可用"), {
        code: "BW_SYNC_OWNER_INACTIVE",
        retryable: true
      });
    }
    await entry.ownerLease.start();
    fenceCapturedAccount(captured);
    entry.ownerLease.assertActive();
    runtime.resume(String(reason || "extension-sync-start"));
    if (options.runNow === true) {
      return await runtime.runNow(String(reason || "extension-sync-now"));
    }
    return runtime;
  } catch (_) {
    reconcileProviderSyncOwners(
      String(reason || "extension-sync-owner-waiting")
    );
    return null;
  }
}
function directBridgeError(message, code, retryable = false) {
  return Object.assign(new Error(String(message || "设备直连桥接失败")), {
    code: String(code || "BW_DIRECT_BRIDGE"),
    retryable: !!retryable
  });
}
function directHostBinding(sender) {
  if (!isTopLevelOwnContentSender(sender)) {
    throw directBridgeError(
      "设备直连只允许扩展顶层网页脚本连接",
      "BW_DIRECT_HOST_SENDER"
    );
  }
  const url = senderUrl(sender);
  return Object.freeze({
    extensionId: String(sender.id || ""),
    tabId: Number(sender.tab.id),
    frameId: Number(sender.frameId ?? 0),
    documentId: String(sender.documentId || ""),
    origin: String(url?.origin || ""),
    pathname: String(url?.pathname || "")
  });
}
function directHostBindingMatches(entry) {
  if (!entry || entry.closed || !directHostPorts.has(entry)) return false;
  const sender = entry.port?.sender;
  const url = senderUrl(sender);
  return !!(
    url &&
    String(sender?.id || "") === entry.binding.extensionId &&
    Number(sender?.tab?.id) === entry.binding.tabId &&
    Number(sender?.frameId ?? 0) === entry.binding.frameId &&
    String(sender?.documentId || "") === entry.binding.documentId &&
    url.origin === entry.binding.origin &&
    url.pathname === entry.binding.pathname
  );
}
function directHostOwnerClaimMatches(entry) {
  if (!entry?.captured || !entry?.ownerLease) return false;
  try {
    fenceCapturedAccount(entry.captured);
    entry.ownerLease.assertActive();
    return (
      entry.deviceFamilyId === entry.captured.deviceFamilyId &&
      entry.deviceFamilyId === persistentDeviceFamilyId
    );
  } catch (_) {
    return false;
  }
}
function assertDirectHostOwnerClaim(entry, proof) {
  void proof;
  if (!directHostOwnerClaimMatches(entry)) {
    throw directBridgeError(
      "设备直连的 server owner lease 已经失效",
      "BW_SYNC_OWNER_INACTIVE",
      true
    );
  }
  return true;
}
function assertDirectOwnerLeaseSnapshot(entry, expected) {
  let current;
  try {
    current = entry?.ownerLease?.fields();
  } catch (_) {
    current = null;
  }
  if (
    !current ||
    !expected ||
    current.deviceFamilyId !== expected.deviceFamilyId ||
    current.ownerRole !== expected.ownerRole ||
    current.ownerInstanceId !== expected.ownerInstanceId ||
    current.ownerGeneration !== expected.ownerGeneration ||
    current.ownerToken !== expected.ownerToken
  ) {
    throw directBridgeError(
      "设备直连的 server owner lease 已换代",
      "BW_SYNC_OWNER_INACTIVE",
      true
    );
  }
  return true;
}
function fenceDirectHost(entry, expectedGeneration) {
  if (
    !entry ||
    entry.closed ||
    entry !== activeDirectHost ||
    !entry.ready ||
    !directHostBindingMatches(entry) ||
    !directHostOwnerClaimMatches(entry) ||
    (
      expectedGeneration != null &&
      entry.generation !== expectedGeneration
    )
  ) {
    throw directBridgeError(
      "设备直连宿主已经失效",
      "BW_DIRECT_HOST_INACTIVE"
    );
  }
  fenceCapturedAccount(entry.captured);
}
function directPublicError(error) {
  return {
    code: String(error?.code || "BW_DIRECT_BRIDGE"),
    error: String(error?.message || error || "设备直连桥接失败"),
    retryable: error?.retryable !== false
  };
}
function postDirectHost(entry, message) {
  if (!entry || entry.closed) return false;
  try {
    checkJsonSize(message, MAX_DIRECT_BRIDGE_BYTES, "设备直连消息");
    entry.port.postMessage(message);
    return true;
  } catch (_) {
    return false;
  }
}
function removeDirectPeer(entry, peerId, reason) {
  if (!entry) return false;
  peerId = String(peerId || "");
  const binding = entry.peers.get(peerId);
  if (!binding) return false;
  entry.peers.delete(peerId);
  directPeerBindings.delete(binding.key);
  try { binding.runtime.removePeer(binding.runtimePeerId); } catch (_) {}
  return true;
}
function revokeDirectEntry(entry, reason, notify = true) {
  if (!entry) return;
  entry.generation += 1;
  entry.ready = false;
  Array.from(entry.peers.keys()).forEach((peerId) => {
    removeDirectPeer(entry, peerId, reason || "direct-host-revoked");
  });
  for (const pending of entry.pending.values()) {
    clearTimeout(pending.timer);
    pending.reject(directBridgeError(
      "设备直连宿主已撤销",
      "BW_DIRECT_HOST_INACTIVE"
    ));
  }
  entry.pending.clear();
  entry.captured = null;
  entry.runtime = null;
  entry.relay = null;
  entry.ownerLease = null;
  entry.deviceFamilyId = "";
  entry.ownerClaimEntry = null;
  entry.ownerClaimGeneration = 0;
  entry.ownerClaimNamespace = "";
  if (notify) {
    postDirectHost(entry, {
      protocol: DIRECT_HOST_PROTOCOL,
      type: "REVOKE",
      payload: { reason: String(reason || "revoked") }
    });
  }
}
function revokeDirectHosts(reason) {
  const previous = activeDirectHost;
  activeDirectHost = null;
  if (previous) revokeDirectEntry(previous, reason || "direct-host-reset");
  for (const entry of directHostPorts) {
    if (entry !== previous && entry.ready) {
      revokeDirectEntry(entry, reason || "direct-host-reset");
    }
  }
}
function callDirectContent(entry, peerId, sessionId, payload) {
  const expectedGeneration = entry.generation;
  return Promise.resolve()
    .then(() => entry.ownerLease.ensureActive())
    .then((ownerSnapshot) => {
      fenceDirectHost(entry, expectedGeneration);
      entry.ownerLease.assertActive();
      assertDirectOwnerLeaseSnapshot(entry, ownerSnapshot);
      checkJsonSize(payload, MAX_DIRECT_BRIDGE_BYTES, "直连交换请求");
      const id = "direct-rpc-" + (++directRpcSequence).toString(36);
      return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      entry.pending.delete(id);
      reject(directBridgeError(
        "RTC DataChannel 响应超时",
        "BW_DIRECT_TIMEOUT",
        true
      ));
    }, DIRECT_RPC_TIMEOUT_MS);
    entry.pending.set(id, {
      timer,
      generation: expectedGeneration,
      resolve(value) {
        try {
          fenceDirectHost(entry, expectedGeneration);
          entry.ownerLease.assertActive();
          assertDirectOwnerLeaseSnapshot(entry, ownerSnapshot);
          resolve(value);
        } catch (error) {
          reject(error);
        }
      },
      reject
    });
    if (!postDirectHost(entry, {
      protocol: DIRECT_HOST_PROTOCOL,
      type: "DIRECT_CALL",
      id,
      payload: {
        peerId,
        sessionId,
        request: structuredClone(payload)
      }
    })) {
      clearTimeout(timer);
      entry.pending.delete(id);
      reject(directBridgeError(
        "无法向 RTC 内容宿主发送请求",
        "BW_DIRECT_HOST_INACTIVE",
        true
      ));
    }
      });
    });
}
async function directBaselineStatus(entry) {
  const generation = entry.generation;
  fenceDirectHost(entry, generation);
  const ownerSnapshot = await entry.ownerLease.ensureActive();
  fenceDirectHost(entry, generation);
  entry.ownerLease.assertActive();
  assertDirectOwnerLeaseSnapshot(entry, ownerSnapshot);
  const status = await entry.runtime.status();
  fenceDirectHost(entry, generation);
  entry.ownerLease.assertActive();
  assertDirectOwnerLeaseSnapshot(entry, ownerSnapshot);
  const result = status?.lastResult;
  const checkpoint = status?.coordinator?.checkpoint ||
    result?.checkpoint ||
    null;
  const server = checkpoint?.server || {};
  const latest = result?.server || {};
  return {
    localCursor: Math.max(0, Number(server.localCursor) || 0),
    serverCursor: Math.max(0, Number(server.remoteCursor) || 0),
    ready: status?.paused !== true &&
      latest.ok === true &&
      latest.pendingLocal !== true &&
      (!Array.isArray(latest.conflicts) || latest.conflicts.length === 0)
  };
}
async function activateDirectHost(entry, reason) {
  if (
    !entry ||
    entry.closed ||
    activeDirectHost && activeDirectHost !== entry ||
    !directHostBindingMatches(entry)
  ) return false;
  const activationGeneration = entry.generation + 1;
  entry.generation = activationGeneration;
  try {
    const captured = await capturePersistentAccount();
    fenceCapturedAccount(captured);
    assertProviderSyncOwnerClaim(captured);
    const token = await accountStorage.activeToken(
      captured.entry.accountContext,
      captured.lease
    );
    fenceCapturedAccount(captured);
    assertProviderSyncOwnerClaim(captured);
    if (!token) {
      postDirectHost(entry, {
        protocol: DIRECT_HOST_PROTOCOL,
        type: "STANDBY",
        payload: {
          reason: "token-missing",
          retryable: false
        }
      });
      return false;
    }
    const runtime = await providerSyncRuntimeFor(captured);
    const syncEntry = providerSyncRuntimes.get(captured.lease.namespace);
    if (
      !syncEntry ||
      syncEntry.runtime !== runtime ||
      !syncEntry.ownerLease ||
      syncEntry.deviceFamilyId !== captured.deviceFamilyId
    ) {
      throw directBridgeError(
        "扩展同步 owner lease 不可用",
        "BW_SYNC_OWNER_INACTIVE",
        true
      );
    }
    await syncEntry.ownerLease.start();
    syncEntry.ownerLease.assertActive();
    const store = await vaultFor(captured.lease.namespace);
    fenceCapturedAccount(captured);
    assertProviderSyncOwnerClaim(captured);
    syncEntry.ownerLease.assertActive();
    if (
      entry.closed ||
      entry.generation !== activationGeneration ||
      activeDirectHost && activeDirectHost !== entry
    ) {
      return false;
    }
    const directApi = globalThis.BWReaderRuntime?.directSyncProtocol;
    const registry = globalThis.BWReaderRuntime?.dataRegistry;
    if (
      !directApi?.createStoreRelay ||
      directApi.CONTRACT !== "direct-sync/1"
    ) {
      throw directBridgeError(
        "扩展后台缺少直连 relay",
        "BW_DIRECT_RUNTIME_DEPENDENCY"
      );
    }
    entry.captured = captured;
    entry.runtime = runtime;
    entry.ownerLease = syncEntry.ownerLease;
    entry.deviceFamilyId = syncEntry.deviceFamilyId;
    entry.deviceId = await extensionInstallId();
    entry.registryDigest = persistentProviderRegistry.syncDigest;
    entry.ready = true;
    activeDirectHost = entry;
    assertDirectHostOwnerClaim(entry);
    const leasedStore = {
      async status() {
        const generation = entry.generation;
        const ownerSnapshot = await entry.ownerLease.ensureActive();
        fenceDirectHost(entry, generation);
        assertDirectOwnerLeaseSnapshot(entry, ownerSnapshot);
        const value = await store.status();
        fenceDirectHost(entry, generation);
        entry.ownerLease.assertActive();
        assertDirectOwnerLeaseSnapshot(entry, ownerSnapshot);
        return value;
      },
      async changes(query) {
        const generation = entry.generation;
        const ownerSnapshot = await entry.ownerLease.ensureActive();
        fenceDirectHost(entry, generation);
        assertDirectOwnerLeaseSnapshot(entry, ownerSnapshot);
        const value = await store.changes(query);
        fenceDirectHost(entry, generation);
        entry.ownerLease.assertActive();
        assertDirectOwnerLeaseSnapshot(entry, ownerSnapshot);
        return value;
      },
      async applyChanges(changes, options) {
        const generation = entry.generation;
        const ownerSnapshot = await entry.ownerLease.ensureActive();
        fenceDirectHost(entry, generation);
        assertDirectOwnerLeaseSnapshot(entry, ownerSnapshot);
        const value = await store.applyChanges(changes, options);
        fenceDirectHost(entry, generation);
        entry.ownerLease.assertActive();
        assertDirectOwnerLeaseSnapshot(entry, ownerSnapshot);
        return value;
      }
    };
    entry.relay = directApi.createStoreRelay({
      store: leasedStore,
      registry
    });
    runtime.resume("direct-host-activated");
    runtime.schedule("direct-host-baseline", 0);
    postDirectHost(entry, {
      protocol: DIRECT_HOST_PROTOCOL,
      type: "READY",
      payload: {
        contract: DIRECT_HOST_PROTOCOL,
        deviceId: entry.deviceId,
        registryDigest: entry.registryDigest,
        iceServers: [],
        reason: String(reason || "")
      }
    });
    for (const other of directHostPorts) {
      if (other !== entry && !other.closed) {
        postDirectHost(other, {
          protocol: DIRECT_HOST_PROTOCOL,
          type: "STANDBY",
          payload: { reason: "another-host-active", retryable: true }
        });
      }
    }
    return true;
  } catch (error) {
    if (activeDirectHost === entry) activeDirectHost = null;
    revokeDirectEntry(entry, "direct-host-activation-failed", false);
    postDirectHost(entry, {
      protocol: DIRECT_HOST_PROTOCOL,
      type: "ERROR",
      payload: directPublicError(error)
    });
    return false;
  }
}
async function promoteDirectHost(reason) {
  if (
    activeDirectHost?.ready &&
    directHostBindingMatches(activeDirectHost) &&
    directHostOwnerClaimMatches(activeDirectHost)
  ) {
    return true;
  }
  if (activeDirectHost) {
    revokeDirectEntry(activeDirectHost, "direct-host-invalid");
    activeDirectHost = null;
  }
  const candidates = [...directHostPorts]
    .filter((entry) =>
      !entry.closed && directHostBindingMatches(entry)
    )
    .sort((left, right) => left.sequence - right.sequence);
  for (const candidate of candidates) {
    if (await activateDirectHost(candidate, reason)) return true;
  }
  return false;
}
async function handleDirectHostCall(entry, operation, payload) {
  const generation = entry.generation;
  fenceDirectHost(entry, generation);
  checkJsonSize(payload || {}, MAX_DIRECT_BRIDGE_BYTES, "直连桥接请求");
  const owner = await entry.ownerLease.ensureActive();
  fenceDirectHost(entry, generation);
  entry.ownerLease.assertActive();
  assertDirectOwnerLeaseSnapshot(entry, owner);
  if (operation === "SIGNAL_EXCHANGE") {
    const request = payload?.request;
    if (
      !request ||
      request.contract !== "direct-signal/1" ||
      request.deviceId !== entry.deviceId ||
      request.registryDigest !== entry.registryDigest
    ) {
      throw directBridgeError(
        "信令请求合同不匹配",
        "BW_DIRECT_SIGNAL_CONTRACT"
      );
    }
    const result = await apiRequest("/api/reader/sync/signal", {
      method: "POST",
      body: JSON.stringify({
        contract: "direct-signal/1",
        ownerNamespace: entry.captured.lease.namespace,
        deviceId: entry.deviceId,
        registryDigest: entry.registryDigest,
        localCursor: request.localCursor,
        serverCursor: request.serverCursor,
        serverReady: request.serverReady,
        signalCursor: request.signalCursor,
        signals: request.signals,
        deviceFamilyId: owner.deviceFamilyId,
        ownerRole: owner.ownerRole,
        ownerInstanceId: owner.ownerInstanceId,
        ownerGeneration: owner.ownerGeneration,
        ownerToken: owner.ownerToken
      })
    }, entry.captured);
    fenceDirectHost(entry, generation);
    entry.ownerLease.assertActive();
    assertDirectOwnerLeaseSnapshot(entry, owner);
    return result;
  }
  if (operation === "BASELINE_STATUS") {
    return directBaselineStatus(entry);
  }
  if (operation === "SERVER_SCHEDULE") {
    fenceDirectHost(entry, generation);
    entry.ownerLease.assertActive();
    assertDirectOwnerLeaseSnapshot(entry, owner);
    entry.runtime.schedule(
      "direct-content:" + String(payload?.reason || "schedule").slice(0, 120),
      0
    );
    return { scheduled: true };
  }
  if (operation === "STORE_EXCHANGE") {
    fenceDirectHost(entry, generation);
    entry.ownerLease.assertActive();
    assertDirectOwnerLeaseSnapshot(entry, owner);
    const result = await entry.relay.exchange(payload?.request);
    fenceDirectHost(entry, generation);
    entry.ownerLease.assertActive();
    assertDirectOwnerLeaseSnapshot(entry, owner);
    return result;
  }
  if (operation === "PEER_READY") {
    fenceDirectHost(entry, generation);
    entry.ownerLease.assertActive();
    assertDirectOwnerLeaseSnapshot(entry, owner);
    const peerId = String(payload?.peerId || "");
    const sessionId = String(payload?.sessionId || "");
    if (
      !/^[A-Za-z0-9._:-]{1,128}$/.test(peerId) ||
      !/^[A-Za-z0-9._:-]{1,160}$/.test(sessionId) ||
      peerId === entry.deviceId
    ) {
      throw directBridgeError(
        "直连 peer 身份无效",
        "BW_DIRECT_PEER_INVALID"
      );
    }
    removeDirectPeer(entry, peerId, "peer-session-replaced");
    const gatewayApi = globalThis.BWReaderRuntime?.syncGateway;
    if (
      !gatewayApi?.createSyncGateway ||
      gatewayApi.CONTRACT !== "sync-gateway/2"
    ) {
      throw directBridgeError(
        "扩展后台缺少 SyncGateway",
        "BW_DIRECT_RUNTIME_DEPENDENCY"
      );
    }
    const transport = {
      exchange(request) {
        return callDirectContent(
          entry,
          peerId,
          sessionId,
          request
        );
      }
    };
    const gateway = gatewayApi.createSyncGateway({
      transport,
      deviceId: entry.deviceId
    });
    /*
     * SyncCoordinator persists its peer cursor map.  The RTC session id is
     * intentionally ephemeral and already fences the content-host transport;
     * putting it in the persisted peer key would leak one checkpoint entry on
     * every renegotiation.  Keep the coordinator identity stable per device.
     */
    const runtimePeerId = `rtc:${peerId}`;
    const binding = {
      key: entry.sequence + "|" + peerId,
      peerId,
      sessionId,
      runtimePeerId,
      gateway,
      runtime: entry.runtime
    };
    entry.peers.set(peerId, binding);
    directPeerBindings.set(binding.key, binding);
    entry.runtime.addPeer(runtimePeerId, gateway, {
      baselineReady: true,
      baselineLocalCursor: Math.max(
        0,
        Number(payload?.baselineLocalCursor) || 0
      ),
      baselineRemoteCursor: Math.max(
        0,
        Number(payload?.baselineRemoteCursor) || 0
      )
    });
    entry.runtime.schedule("direct-peer-ready:" + peerId, 0);
    fenceDirectHost(entry, generation);
    entry.ownerLease.assertActive();
    assertDirectOwnerLeaseSnapshot(entry, owner);
    return { registered: true };
  }
  if (operation === "PEER_CLOSED") {
    const result = {
      removed: removeDirectPeer(
        entry,
        String(payload?.peerId || ""),
        String(payload?.reason || "peer-closed")
      )
    };
    fenceDirectHost(entry, generation);
    entry.ownerLease.assertActive();
    assertDirectOwnerLeaseSnapshot(entry, owner);
    return result;
  }
  throw directBridgeError(
    "不支持的设备直连操作",
    "BW_DIRECT_BRIDGE_OPERATION"
  );
}
function jsonByteLength(value) {
  let text;
  try { text = JSON.stringify(value == null ? null : value); }
  catch (_) { text = null; }
  if (typeof text !== "string") {
    throw Object.assign(new Error("数据必须可以序列化为 JSON"), { code: "BW_PROVIDER_PAYLOAD" });
  }
  return new TextEncoder().encode(text).byteLength;
}
function checkJsonSize(value, limit, label) {
  const size = jsonByteLength(value);
  if (size > limit) {
    throw Object.assign(new Error(label + " 超出大小限制"), {
      code: "BW_PROVIDER_PAYLOAD",
      details: { limit, size }
    });
  }
}
function checkedId(value, label) {
  value = String(value || "").trim();
  if (!value || value.length > 512) {
    throw Object.assign(new Error(label + " 无效"), { code: "BW_PROVIDER_PAYLOAD" });
  }
  return value;
}
function boundedQuery(input) {
  const query = { ...(input || {}) };
  query.limit = Math.max(1, Math.min(MAX_PROVIDER_PAGE, Number(query.limit) || MAX_PROVIDER_PAGE));
  if (query.offset != null) query.offset = Math.max(0, Number(query.offset) || 0);
  if (query.after != null) query.after = Math.max(0, Number(query.after) || 0);
  return query;
}
function checkCollection(collection, allowedCollections) {
  collection = String(collection || "");
  if (!(allowedCollections instanceof Set) || !allowedCollections.has(collection)) {
    throw Object.assign(new Error("该 collection 不允许交给扩展 provider：" + collection), {
      code: "BW_PROVIDER_COLLECTION"
    });
  }
  return collection;
}
function validateProviderCall(operation, args, allowedCollections) {
  if (!PROVIDER_OPS.has(operation)) {
    throw Object.assign(new Error("不允许的数据操作"), { code: "BW_PROVIDER_OPERATION" });
  }
  checkJsonSize(args, MAX_PROVIDER_REQUEST_BYTES, "请求");
  if (operation === "status") return;
  if (operation === "changes") {
    args.query = boundedQuery(args.query);
    return;
  }
  if (operation === "applyChanges") {
    if (!Array.isArray(args.changes) || args.changes.length > MAX_PROVIDER_MUTATIONS) {
      throw Object.assign(new Error("导入记录数量超出限制"), { code: "BW_PROVIDER_PAYLOAD" });
    }
    for (const change of args.changes) {
      checkCollection(change?.collection || change?.record?.collection, allowedCollections);
      checkedId(change?.record?.id, "record.id");
      if (!change?.mutationId || String(change.mutationId).length > 512) {
        throw Object.assign(new Error("扩展导入必须携带 mutationId"), { code: "BW_PROVIDER_MUTATION_ID" });
      }
    }
    return;
  }
  if (operation === "batch") {
    if (!Array.isArray(args.mutations) || args.mutations.length > MAX_PROVIDER_MUTATIONS) {
      throw Object.assign(new Error("批量写入数量超出限制"), { code: "BW_PROVIDER_PAYLOAD" });
    }
    for (const mutation of args.mutations) {
      checkCollection(mutation?.collection, allowedCollections);
      const options = mutation?.options || mutation || {};
      const itemOperation = String(mutation?.operation || mutation?.op || "put");
      if (!["put", "remove"].includes(itemOperation)) {
        throw Object.assign(new Error("批量写入包含不允许的操作"), { code: "BW_PROVIDER_OPERATION" });
      }
      if (itemOperation === "remove") {
        checkedId(mutation?.id, "id");
      } else {
        const value = mutation?.value || mutation?.record || {};
        checkedId(options.id || value.id || value.gid || value.cid, "record.id");
      }
      if (!options.mutationId || String(options.mutationId).length > 512) {
        throw Object.assign(new Error("扩展写入必须携带 mutationId"), { code: "BW_PROVIDER_MUTATION_ID" });
      }
    }
    return;
  }
  checkCollection(args.collection, allowedCollections);
  if (operation === "get" || operation === "remove") args.id = checkedId(args.id, "id");
  if (operation === "list") args.query = boundedQuery(args.query);
  if (operation === "put") {
    checkedId(args.options?.id || args.value?.id || args.value?.gid || args.value?.cid, "record.id");
  }
  if ((operation === "put" || operation === "remove") && !args.options?.mutationId) {
    throw Object.assign(new Error("扩展写入必须携带 mutationId"), { code: "BW_PROVIDER_MUTATION_ID" });
  }
}
function filterProviderStatus(status, allowedCollections) {
  status = { ...(status || {}) };
  if (Array.isArray(status.collections)) {
    status.collections = status.collections.filter((collection) =>
      allowedCollections.has(String(collection || ""))
    );
  }
  status.providerCollections = Array.from(allowedCollections).sort();
  return status;
}
function filterProviderChanges(result, allowedCollections) {
  result = { ...(result || {}) };
  result.changes = (Array.isArray(result.changes) ? result.changes : []).filter((change) =>
    allowedCollections.has(String(change?.collection || change?.record?.collection || ""))
  );
  return result;
}
async function runProviderCall(namespace, operation, args, allowedCollections, fence) {
  validateProviderCall(operation, args || {}, allowedCollections);
  const store = await vaultFor(namespace);
  if (typeof fence === "function") fence();
  if (operation === "get") return store.get(args.collection, args.id, args.options || {});
  if (operation === "list") return store.list(args.collection, args.query || {});
  if (operation === "put") return store.put(args.collection, args.value, args.options || {});
  if (operation === "remove") return store.remove(args.collection, args.id, args.options || {});
  if (operation === "batch") return store.batch(args.mutations || []);
  if (operation === "changes") {
    return filterProviderChanges(
      await store.changes(args.query || {}),
      allowedCollections
    );
  }
  if (operation === "applyChanges") return store.applyChanges(args.changes || [], args.options || {});
  if (operation === "status") {
    return filterProviderStatus(await store.status(), allowedCollections);
  }
  throw Object.assign(new Error("不允许的数据操作"), { code: "BW_PROVIDER_OPERATION" });
}
async function runProviderSyncCall(captured, operation, args) {
  if (!PROVIDER_SYNC_OPS.has(operation)) {
    throw Object.assign(new Error("不允许的同步控制操作"), {
      code: "BW_PROVIDER_OPERATION"
    });
  }
  checkJsonSize(args || {}, 4096, "同步控制请求");
  fenceCapturedAccount(captured);
  const control = await providerSyncControlFor(captured);
  fenceCapturedAccount(captured);
  if (operation === "syncStatus") {
    const result = await control.status();
    fenceCapturedAccount(captured);
    return result;
  }
  throw Object.assign(new Error("不允许的同步控制操作"), {
    code: "BW_PROVIDER_OPERATION",
    retryable: false
  });
}
function providerResult(port, id, result, error) {
  if (!error) {
    try { checkJsonSize(result, MAX_PROVIDER_RESPONSE_BYTES, "响应"); }
    catch (sizeError) { error = sizeError; }
  }
  const payload = error ? {
    ok: false,
    code: String(error.code || "BW_PROVIDER_ERROR"),
    error: String(error.message || error),
    details: error.details || null
  } : { ok: true, result };
  try { port.postMessage({ protocol: PROVIDER_PROTOCOL, type: "RESULT", id, payload }); } catch (_) {}
}
function providerHandshakeError(port, id, error) {
  try {
    port.postMessage({
      protocol: PROVIDER_PROTOCOL,
      type: "ERROR",
      id: id || null,
      payload: {
        code: String(error.code || "BW_PROVIDER_ERROR"),
        error: String(error.message || error)
      }
    });
  } catch (_) {}
}
function broadcastProviderChange(namespace, change) {
  const nowMs = Date.now();
  for (const entry of providerPorts) {
    if (!entry.authorized || Number(entry.authorizationExpiresAtMs) <= nowMs) {
      if (entry.authorized) revokeProviderEntry(entry, "provider-authorization-expired");
      continue;
    }
    let lease;
    try {
      lease = entry.accountContext.lease();
      providerAccountFence(entry, entry.transportGeneration, lease);
    } catch (_) {
      continue;
    }
    if (
      lease.namespace !== namespace ||
      !(entry.providerCollections instanceof Set) ||
      !entry.providerCollections.has(String(
        change?.collection || change?.record?.collection || ""
      ))
    ) continue;
    try {
      entry.port.postMessage({
        protocol: PROVIDER_PROTOCOL,
        type: "CHANGE",
        payload: change || {}
      });
    } catch (_) {}
  }
  if (
    String(change?.collection || change?.record?.collection || "") !==
    "vocabulary-state"
  ) return;
  const spec = globalThis.BWReaderRuntime?.vocabularyState;
  if (!spec || spec.CONTRACT !== "vocabulary-state/1") return;
  let record;
  try { record = spec.normalizeRecord(change?.record || change); }
  catch (_) { return; }
  for (const entry of vocabularyStatePorts) {
    if (entry.closed || !entry.ready || entry.namespace !== namespace) continue;
    try {
      fenceCapturedAccount(entry.captured);
      entry.port.postMessage({
        protocol: VOCABULARY_STATE_PROTOCOL,
        type: "CHANGE",
        record
      });
    } catch (_) {
      entry.invalidate("account-context-stale");
    }
  }
}

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "bw-reader-provider") return;
  let binding;
  let accountContext;
  try {
    binding = providerSenderBinding(port.sender);
    accountContext = newProviderAccountContext();
  } catch (_) {
    try { port.disconnect(); } catch (_) {}
    return;
  }
  const entry = {
    port,
    binding,
    accountContext,
    lease: null,
    namespace: "",
    page: binding.pathname,
    authorized: false,
    handshakePending: false,
    authorizationExpiresAtMs: 0,
    providerCollections: null,
    syncOwnerClaim: null,
    transportGeneration: 0,
    closed: false
  };
  providerPorts.add(entry);
  port.onMessage.addListener(async (message) => {
    if (!message || message.protocol !== PROVIDER_PROTOCOL || message.direction !== "page-to-extension") return;
    if (message.type === "HELLO") {
      if (entry.handshakePending) {
        providerHandshakeError(
          port,
          message.id || null,
          Object.assign(new Error("provider 授权正在进行"), { code: "BW_PROVIDER_AUTH_PENDING" })
        );
        return;
      }
      const handshakeGeneration = entry.transportGeneration + 1;
      entry.transportGeneration = handshakeGeneration;
      entry.handshakePending = true;
      revokeProviderEntry(entry, "provider-rehandshake");
      try {
        providerTransportFence(entry, handshakeGeneration);
        if (String(message.payload?.page || "") !== entry.page) {
          throw Object.assign(new Error("阅读器页面身份不匹配"), { code: "BW_PROVIDER_PAGE" });
        }
        const registry = providerRegistrySnapshot();
        const syncOwnerClaim = checkedProviderSyncOwnerClaim(
          message.payload?.syncOwnerClaim,
          entry.binding,
          registry
        );
        const candidateNamespace = safeNamespace(message.payload?.namespace);
        const authorization = await authorizeProviderNamespace(
          candidateNamespace,
          message.payload?.ticket
        );
        providerTransportFence(entry, handshakeGeneration);
        accountContext.activate({
          namespace: candidateNamespace,
          source: "provider-ticket"
        });
        const lease = accountContext.lease();
        entry.authorizationExpiresAtMs = authorization.validUntilMs;
        const status = filterProviderStatus(
          await (await vaultFor(lease.namespace)).status(),
          registry.collections
        );
        providerAccountFence(entry, handshakeGeneration, lease);
        // provider ticket 已由服务器证明账户归属。把命名空间保存为扩展当前账户，
        // 后续任意网页都能独立使用此前验证并加密分区保存的设备令牌。
        await rememberVerifiedAccount(
          lease.namespace,
          syncOwnerClaim?.deviceFamilyId || ""
        );
        providerAccountFence(entry, handshakeGeneration, lease);
        // 只有账户证明与 Vault 都成功后才原子发布授权状态；失败或进行中 CALL 均不可用。
        entry.namespace = lease.namespace;
        entry.lease = lease;
        entry.authorized = true;
        entry.providerCollections = registry.collections;
        entry.syncOwnerClaim = syncOwnerClaim;
        reconcileProviderSyncOwners("provider-owner-claim-published");
        if (syncOwnerClaim) {
          void startActiveProviderSync("provider-authorized", {
            captured: Object.freeze({
              entry,
              lease,
              transportGeneration: handshakeGeneration
            })
          });
        }
        port.postMessage({
          protocol: PROVIDER_PROTOCOL,
          type: "READY",
          id: message.id || null,
          payload: {
            version: chrome.runtime.getManifest().version,
            dataStore: status,
            authorizationExpiresAt: authorization.expiresAt,
            accountContext: {
              contract: accountContext.CONTRACT,
              generation: lease.generation
            },
            capabilities: {
              dataStore: true,
              syncControl: !!syncOwnerClaim,
              syncControlRetry: false,
              syncGateway: false,
              serverSync: !!syncOwnerClaim,
              directSync: false,
              syncOwner: syncOwnerClaim
                ? "extension-background"
                : "reserved-unclaimed",
              networkOperations: false,
              providerCollections: registry.names,
              syncCollections: registry.syncNames,
              syncDescriptor: registry.syncDescriptor,
              syncDigest: registry.syncDigest,
              syncContract: registry.syncContract,
              syncChangeContract: registry.syncChangeContract
            }
          }
        });
      } catch (error) {
        if (entry.transportGeneration === handshakeGeneration) {
          revokeProviderEntry(entry, "provider-handshake-failed");
        }
        providerHandshakeError(port, message.id || null, error);
      } finally {
        if (entry.transportGeneration === handshakeGeneration) {
          entry.handshakePending = false;
        }
      }
      return;
    }
    if (message.type !== "CALL" || !message.id) return;
    const authorizationExpired =
      entry.authorized &&
      Number(entry.authorizationExpiresAtMs) <= Date.now();
    if (authorizationExpired) {
      revokeProviderEntry(entry, "provider-authorization-expired");
    }
    if (!entry.authorized || !entry.namespace) {
      providerResult(port, message.id, null, Object.assign(
        new Error(entry.handshakePending
          ? "provider 授权尚未完成"
          : (authorizationExpired ? "provider 授权已过期" : "provider 尚未通过账户授权")),
        {
          code: entry.handshakePending
            ? "BW_PROVIDER_AUTH_PENDING"
            : (authorizationExpired ? "BW_PROVIDER_AUTH_EXPIRED" : "BW_PROVIDER_AUTH")
        }
      ));
      return;
    }
    const operation = String(message.payload?.operation || "");
    const args = message.payload?.args || {};
    const callGeneration = entry.transportGeneration;
    let lease;
    try {
      lease = entry.accountContext.lease();
      providerAccountFence(entry, callGeneration, lease);
      const captured = Object.freeze({
        entry,
        lease,
        transportGeneration: callGeneration
      });
      const result = PROVIDER_SYNC_OPS.has(operation)
        ? await runProviderSyncCall(captured, operation, args)
        : await runProviderCall(
          lease.namespace,
          operation,
          args,
          entry.providerCollections,
          () => providerAccountFence(entry, callGeneration, lease)
        );
      providerAccountFence(entry, callGeneration, lease);
      providerResult(port, message.id, result, null);
    } catch (error) {
      if (
        lease &&
        [
          "put",
          "remove",
          "batch",
          "applyChanges"
        ].includes(operation) &&
        [
          "BW_ACCOUNT_CONTEXT_STALE",
          "BW_PROVIDER_DISCONNECTED",
          "BW_PROVIDER_AUTH_EXPIRED",
          "BW_PROVIDER_SENDER"
        ].includes(error?.code)
      ) {
        error.details = Object.assign({}, error.details, {
          outcomeUnknown: true,
          mutationId: String(
            args?.options?.mutationId ||
            args?.mutationId ||
            ""
          )
        });
      }
      providerResult(port, message.id, null, error);
    }
  });
  port.onDisconnect.addListener(() => {
    entry.closed = true;
    entry.transportGeneration += 1;
    revokeProviderEntry(entry, "provider-port-disconnected");
    providerPorts.delete(entry);
  });
});

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== DIRECT_HOST_PORT) return;
  let binding;
  try {
    binding = directHostBinding(port.sender);
  } catch (error) {
    postDirectHost({ port, closed: false }, {
      protocol: DIRECT_HOST_PROTOCOL,
      type: "ERROR",
      payload: directPublicError(error)
    });
    try { port.disconnect(); } catch (_) {}
    return;
  }
  const entry = {
    sequence: ++directHostSequence,
    port,
    binding,
    closed: false,
    ready: false,
    generation: 0,
    captured: null,
    runtime: null,
    relay: null,
    ownerLease: null,
    deviceFamilyId: "",
    ownerClaimEntry: null,
    ownerClaimGeneration: 0,
    ownerClaimNamespace: "",
    deviceId: "",
    registryDigest: "",
    pending: new Map(),
    inboundIds: new Set(),
    inboundIdOrder: [],
    peers: new Map()
  };
  directHostPorts.add(entry);
  postDirectHost(entry, {
    protocol: DIRECT_HOST_PROTOCOL,
    type: "STANDBY",
    payload: { reason: "selecting-host", retryable: true }
  });
  port.onMessage.addListener(async (message) => {
    if (!message || message.protocol !== DIRECT_HOST_PROTOCOL) return;
    if (message.type === "DIRECT_RESULT" && message.id) {
      const pending = entry.pending.get(String(message.id));
      if (!pending) return;
      clearTimeout(pending.timer);
      entry.pending.delete(String(message.id));
      try {
        checkJsonSize(message.payload || {}, MAX_DIRECT_BRIDGE_BYTES, "直连响应");
        if (message.payload?.ok === true) {
          pending.resolve(structuredClone(message.payload.result));
        } else {
          pending.reject(directBridgeError(
            message.payload?.error || "RTC 内容宿主请求失败",
            message.payload?.code || "BW_DIRECT_REMOTE",
            message.payload?.retryable !== false
          ));
        }
      } catch (error) {
        pending.reject(error);
      }
      return;
    }
    if (message.type !== "CALL" || !message.id) return;
    const id = String(message.id);
    if (entry.inboundIds.has(id)) {
      revokeDirectEntry(entry, "duplicate-direct-call-id");
      if (activeDirectHost === entry) activeDirectHost = null;
      void promoteDirectHost("duplicate-direct-call-id");
      return;
    }
    entry.inboundIds.add(id);
    entry.inboundIdOrder.push(id);
    while (entry.inboundIdOrder.length > 256) {
      const oldestId = entry.inboundIdOrder.shift();
      entry.inboundIds.delete(oldestId);
    }
    try {
      const result = await handleDirectHostCall(
        entry,
        String(message.operation || ""),
        message.payload || {}
      );
      postDirectHost(entry, {
        protocol: DIRECT_HOST_PROTOCOL,
        type: "RESULT",
        id,
        payload: { ok: true, result }
      });
    } catch (error) {
      postDirectHost(entry, {
        protocol: DIRECT_HOST_PROTOCOL,
        type: "RESULT",
        id,
        payload: { ok: false, ...directPublicError(error) }
      });
    }
  });
  port.onDisconnect.addListener(() => {
    if (entry.closed) return;
    entry.closed = true;
    const wasActive = activeDirectHost === entry;
    if (wasActive) activeDirectHost = null;
    revokeDirectEntry(entry, "direct-host-disconnected", false);
    directHostPorts.delete(entry);
    if (wasActive) void promoteDirectHost("direct-host-disconnected");
  });
  void promoteDirectHost("direct-host-connected");
});

function ensureProviderSyncAlarm() {
  if (!chrome.alarms || typeof chrome.alarms.create !== "function") return;
  try {
    const created = chrome.alarms.create(PROVIDER_SYNC_ALARM, {
      delayInMinutes: 1,
      periodInMinutes: 1
    });
    if (created && typeof created.catch === "function") {
      created.catch(() => {});
    }
  } catch (_) {}
}
if (chrome.alarms && chrome.alarms.onAlarm) {
  chrome.alarms.onAlarm.addListener((alarm) => {
    if (alarm?.name !== PROVIDER_SYNC_ALARM) return;
    void startActiveProviderSync("periodic-alarm", { runNow: true });
  });
}
if (chrome.runtime.onInstalled) {
  chrome.runtime.onInstalled.addListener(() => {
    ensureProviderSyncAlarm();
    void startActiveProviderSync("extension-installed", { runNow: true });
  });
}
if (chrome.runtime.onStartup) {
  chrome.runtime.onStartup.addListener(() => {
    ensureProviderSyncAlarm();
    void startActiveProviderSync("browser-startup", { runNow: true });
  });
}
ensureProviderSyncAlarm();
void startActiveProviderSync("worker-start", { runNow: true });

// ── 账户分区缓存：旧 dictCache/webTrCacheV1 只留在隔离区，不读取、不迁移、不删除。──
const cacheWriteQueues = new Map();
function newCacheMutationId(kind) {
  const bytes = new Uint8Array(12);
  crypto.getRandomValues(bytes);
  return "cache-mutation-v1:" + kind + ":" + Array.from(bytes)
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}
async function cacheRecordId(kind, value) {
  return kind + ":v1:" + await sha256Hex(value);
}
async function cacheStore(captured, collection) {
  fenceCapturedAccount(captured);
  checkCollection(collection, captured.entry.providerCollections);
  const store = await vaultFor(captured.lease.namespace);
  fenceCapturedAccount(captured);
  return store;
}
async function queueCacheWrite(captured, collection, id, operation) {
  const queueKey = captured.lease.namespace + ":" + collection + ":" + id;
  const previous = cacheWriteQueues.get(queueKey) || Promise.resolve();
  const current = previous.catch(() => {}).then(async () => {
    fenceCapturedAccount(captured);
    return operation();
  });
  cacheWriteQueues.set(queueKey, current);
  try {
    return await current;
  } finally {
    if (cacheWriteQueues.get(queueKey) === current) cacheWriteQueues.delete(queueKey);
  }
}
async function pruneDerivedCache(captured, store, collection, maximum) {
  const records = await store.list(collection, { limit: maximum + 1 });
  fenceCapturedAccount(captured);
  if (!Array.isArray(records) || records.length <= maximum) return;
  for (const record of records.slice(0, records.length - maximum)) {
    fenceCapturedAccount(captured);
    await store.remove(collection, record.id, {
      mutationId: newCacheMutationId("prune-" + collection)
    });
    fenceCapturedAccount(captured);
  }
}
async function dictionaryCacheGet(captured, key) {
  fenceCapturedAccount(captured);
  const id = await cacheRecordId("dictionary", key);
  fenceCapturedAccount(captured);
  const store = await cacheStore(captured, "dictionary-cache");
  const record = await store.get("dictionary-cache", id);
  fenceCapturedAccount(captured);
  const value = record?.value;
  return (
    value &&
    value.schema === 1 &&
    value.cacheKey === key &&
    typeof value.body === "string"
  ) ? value : null;
}
async function dictionaryCachePut(captured, key, body) {
  fenceCapturedAccount(captured);
  const id = await cacheRecordId("dictionary", key);
  fenceCapturedAccount(captured);
  const store = await cacheStore(captured, "dictionary-cache");
  await queueCacheWrite(captured, "dictionary-cache", id, async () => {
    await store.put("dictionary-cache", {
      id,
      schema: 1,
      cacheKey: key,
      body,
      ts: Date.now()
    }, {
      id,
      mutationId: newCacheMutationId("dictionary-put")
    });
    fenceCapturedAccount(captured);
    await pruneDerivedCache(captured, store, "dictionary-cache", 800);
  });
  fenceCapturedAccount(captured);
}
const WEB_TRANSLATE_GOOGLE_NS = "web-google-v1-gtranslate-v2";
function checkedTranslationCacheNamespace(value) {
  const namespace = String(value || WEB_TRANSLATE_GOOGLE_NS);
  if (
    namespace.length > 120 ||
    !/^web-(?:google-v1|ai-v(?:1|2))-[a-z0-9._-]+$/.test(namespace)
  ) {
    throw Object.assign(new Error("翻译缓存命名空间无效"), {
      code: "BW_TRANSLATION_CACHE_NAMESPACE"
    });
  }
  return namespace;
}
function newWebTranslateDocumentKey() {
  try {
    if (crypto && typeof crypto.randomUUID === "function") {
      return crypto.randomUUID();
    }
    const bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, "0"));
    return `${hex.slice(0, 4).join("")}-${hex.slice(4, 6).join("")}-${hex.slice(6, 8).join("")}-${hex.slice(8, 10).join("")}-${hex.slice(10).join("")}`;
  } catch (_) {
    throw Object.assign(new Error("无法建立网页翻译文档身份"), {
      code: "BW_WEB_TRANSLATE_DOCUMENT"
    });
  }
}
async function trustedWebTranslatePolicy() {
  const stored = await chrome.storage.local.get("bwReaderExtensionPreferencesV2");
  const record = stored?.bwReaderExtensionPreferencesV2;
  const values = (
    record &&
    record.schema === 2 &&
    record.values &&
    typeof record.values === "object" &&
    !Array.isArray(record.values)
  ) ? record.values : {};
  const rawMode = String(values["eph-web-tr-mode"] || "auto");
  const mode = rawMode === "session" || rawMode === "stateless"
    ? rawMode
    : "auto";
  const rawThreshold = Number.parseInt(
    String(values["eph-web-tr-threshold"] || "50"),
    10
  );
  const threshold = Number.isFinite(rawThreshold)
    ? Math.max(10, Math.min(500, rawThreshold))
    : 50;
  return { mode, threshold };
}
function resolvedWebTranslateMode(policy, estimatedUnits) {
  if (policy.mode === "session" || policy.mode === "stateless") {
    return policy.mode;
  }
  const units = estimatedUnits;
  if (
    typeof units !== "number" ||
    !Number.isInteger(units) ||
    units < 0 ||
    units > 100000
  ) {
    return "stateless";
  }
  return units > policy.threshold ? "session" : "stateless";
}
async function trCacheGet(captured, pageUrl, cacheNamespace) {
  if (!pageUrl) return {};
  cacheNamespace = checkedTranslationCacheNamespace(cacheNamespace);
  fenceCapturedAccount(captured);
  const id = await cacheRecordId(
    "web-translation",
    cacheNamespace + "\0" + pageUrl
  );
  fenceCapturedAccount(captured);
  const store = await cacheStore(captured, "translation-cache");
  const record = await store.get("translation-cache", id);
  fenceCapturedAccount(captured);
  const value = record?.value;
  return (
    value &&
    value.schema === 2 &&
    value.pageUrl === pageUrl &&
    value.cacheNamespace === cacheNamespace &&
    value.items &&
    typeof value.items === "object" &&
    !Array.isArray(value.items)
  ) ? value.items : {};
}
async function trCachePut(captured, pageUrl, cacheNamespace, pairs) {
  if (!pageUrl || !pairs || typeof pairs !== "object") return;
  cacheNamespace = checkedTranslationCacheNamespace(cacheNamespace);
  fenceCapturedAccount(captured);
  const id = await cacheRecordId(
    "web-translation",
    cacheNamespace + "\0" + pageUrl
  );
  fenceCapturedAccount(captured);
  const store = await cacheStore(captured, "translation-cache");
  await queueCacheWrite(captured, "translation-cache", id, async () => {
    const existing = await store.get("translation-cache", id);
    fenceCapturedAccount(captured);
    const currentItems = (
      existing?.value?.schema === 2 &&
      existing.value.pageUrl === pageUrl &&
      existing.value.cacheNamespace === cacheNamespace &&
      existing.value.items &&
      typeof existing.value.items === "object" &&
      !Array.isArray(existing.value.items)
    ) ? existing.value.items : {};
    const items = Object.assign({}, currentItems, pairs);
    const itemKeys = Object.keys(items);
    if (itemKeys.length > 600) {
      for (const key of itemKeys.slice(0, itemKeys.length - 600)) delete items[key];
    }
    await store.put("translation-cache", {
      id,
      schema: 2,
      pageUrl,
      cacheNamespace,
      items,
      ts: Date.now()
    }, {
      id,
      mutationId: newCacheMutationId("translation-put")
    });
    fenceCapturedAccount(captured);
    await pruneDerivedCache(captured, store, "translation-cache", 50);
  });
  fenceCapturedAccount(captured);
}

function checkedPageCardPresentationCid(value) {
  const cid = String(value ?? "");
  if (
    !cid ||
    cid.length > 200 ||
    cid === "__proto__" ||
    cid === "prototype" ||
    cid === "constructor" ||
    /[\u0000-\u001f\u007f]/.test(cid)
  ) {
    throw Object.assign(new Error("页面卡片呈现状态包含非法编号"), {
      code: "BW_LOCAL_STORAGE_VALUE"
    });
  }
  return cid;
}

function checkedPageCardPresentationRecord(value) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw Object.assign(new Error("页面卡片呈现状态格式无效"), {
      code: "BW_LOCAL_STORAGE_VALUE"
    });
  }
  const width = Number(value.w);
  const height = Number(value.h);
  const updatedAt = Number(value.updatedAt);
  if (
    !Number.isSafeInteger(width) ||
    width < 180 ||
    width > 720 ||
    !Number.isSafeInteger(height) ||
    height < 100 ||
    height > 720 ||
    !Number.isSafeInteger(updatedAt) ||
    updatedAt < 0
  ) {
    throw Object.assign(new Error("页面卡片呈现尺寸超出安全范围"), {
      code: "BW_LOCAL_STORAGE_VALUE"
    });
  }
  return { w: width, h: height, updatedAt };
}

function checkedPageCardPresentation(value) {
  if (
    !value ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    value.schema !== 1 ||
    !value.cards ||
    typeof value.cards !== "object" ||
    Array.isArray(value.cards)
  ) {
    throw Object.assign(new Error("卡片呈现状态格式无效"), {
      code: "BW_LOCAL_STORAGE_VALUE"
    });
  }
  const cards = {};
  const entries = Object.entries(value.cards);
  if (entries.length > 2000) {
    throw Object.assign(new Error("卡片呈现状态超过记录上限"), {
      code: "BW_LOCAL_STORAGE_VALUE"
    });
  }
  for (const [cid, record] of entries) {
    const safeCid = checkedPageCardPresentationCid(cid);
    cards[safeCid] = checkedPageCardPresentationRecord(record);
  }
  return { schema: 1, cards };
}

let pageCardPresentationWrite = Promise.resolve();

async function handleLocalStorageMessage(message, sender) {
  if (!isTopLevelOwnContentSender(sender)) {
    throw Object.assign(new Error("本地数据网关只允许扩展顶层内容脚本"), {
      code: "BW_LOCAL_STORAGE_SENDER"
    });
  }
  const key = String(message?.key || "");
  if (!LOCAL_STORAGE_KEYS.has(key)) {
    throw Object.assign(new Error("本地数据键不在白名单"), {
      code: "BW_LOCAL_STORAGE_KEY"
    });
  }
  let storageKey = key;
  let captured = null;
  if (key === "reviewQueueV2") {
    captured = await capturePersistentAccountForContentSender(sender);
    fenceCapturedAccount(captured);
    storageKey = key + ":" + captured.lease.namespace;
  }
  if (message.type === "BW_LOCAL_STORAGE_GET") {
    const stored = await chrome.storage.local.get(storageKey);
    if (captured) fenceCapturedAccount(captured);
    return Object.prototype.hasOwnProperty.call(stored || {}, storageKey)
      ? stored[storageKey]
      : null;
  }
  if (message.type === "BW_LOCAL_STORAGE_REMOVE") {
    await chrome.storage.local.remove(storageKey);
    if (captured) fenceCapturedAccount(captured);
    return true;
  }
  const value = message.value;
  let serialized = "";
  try { serialized = JSON.stringify(value); } catch (_) {}
  if (
    typeof serialized !== "string" ||
    new TextEncoder().encode(serialized).byteLength > MAX_LOCAL_STORAGE_VALUE_BYTES
  ) {
    throw Object.assign(new Error("本地数据超过扩展上限"), {
      code: "BW_LOCAL_STORAGE_VALUE"
    });
  }
  await chrome.storage.local.set({ [storageKey]: value });
  if (captured) fenceCapturedAccount(captured);
  return true;
}

async function handlePageCardPresentationMessage(message, sender) {
  if (!isTopLevelOwnContentSender(sender)) {
    throw Object.assign(new Error("页面卡片呈现网关只允许扩展顶层内容脚本"), {
      code: "BW_LOCAL_STORAGE_SENDER"
    });
  }
  const cid = checkedPageCardPresentationCid(message?.cid);
  if (message.type === "BW_PAGE_CARD_PRESENTATION_GET") {
    // 等已经接受的写操作完成再读；同一个 service worker 内所有标签页因此共享
    // 单一顺序，不能在 get→merge→set 之间互相覆盖不同 cid。
    await pageCardPresentationWrite.catch(() => {});
    const stored = await chrome.storage.local.get(
      PAGE_CARD_PRESENTATION_STORAGE_KEY
    );
    if (!Object.prototype.hasOwnProperty.call(
      stored || {},
      PAGE_CARD_PRESENTATION_STORAGE_KEY
    )) return null;
    const state = checkedPageCardPresentation(
      stored[PAGE_CARD_PRESENTATION_STORAGE_KEY]
    );
    return Object.prototype.hasOwnProperty.call(state.cards, cid)
      ? state.cards[cid]
      : null;
  }
  const record = checkedPageCardPresentationRecord(message?.value);
  const operation = pageCardPresentationWrite.catch(() => {}).then(async () => {
    const stored = await chrome.storage.local.get(
      PAGE_CARD_PRESENTATION_STORAGE_KEY
    );
    const state = Object.prototype.hasOwnProperty.call(
      stored || {},
      PAGE_CARD_PRESENTATION_STORAGE_KEY
    )
      ? checkedPageCardPresentation(stored[PAGE_CARD_PRESENTATION_STORAGE_KEY])
      : { schema: 1, cards: {} };
    const previous = state.cards[cid];
    // 同一卡两个页面乱序抬手时按 updatedAt 保留较新的本机呈现；不同 cid
    // 在后台串行合并，避免内容脚本各自提交整张 map 造成丢记录。
    if (!previous || record.updatedAt >= previous.updatedAt) {
      state.cards[cid] = record;
    }
    const keys = Object.keys(state.cards);
    if (keys.length > 2000) {
      keys.sort((a, b) => (
        Number(state.cards[a]?.updatedAt || 0) -
        Number(state.cards[b]?.updatedAt || 0)
      ));
      for (const key of keys.slice(0, keys.length - 2000)) {
        delete state.cards[key];
      }
    }
    await chrome.storage.local.set({
      [PAGE_CARD_PRESENTATION_STORAGE_KEY]: state
    });
    return state.cards[cid];
  });
  pageCardPresentationWrite = operation;
  return operation;
}

async function handleTranslationCacheMessage(message, sender) {
  if (!isTopLevelOwnContentSender(sender)) {
    throw Object.assign(new Error("译文缓存只允许扩展顶层内容脚本读取"), {
      code: "BW_TRANSLATION_CACHE_SENDER"
    });
  }
  if (
    Object.prototype.hasOwnProperty.call(message || {}, "url") ||
    Object.prototype.hasOwnProperty.call(message || {}, "pageUrl")
  ) {
    throw Object.assign(new Error("译文缓存页面只能由发送者标签页确定"), {
      code: "BW_TRANSLATION_CACHE_REQUEST"
    });
  }
  const pageUrl = String(sender?.tab?.url || "").split("#")[0];
  if (!/^https?:\/\//.test(pageUrl)) {
    throw Object.assign(new Error("当前页面不能使用译文缓存"), {
      code: "BW_TRANSLATION_CACHE_PAGE"
    });
  }
  const cacheNamespace = checkedTranslationCacheNamespace(
    message?.cacheNamespace
  );
  const captured = await capturePersistentAccountForContentSender(sender);
  return {
    items: await trCacheGet(captured, pageUrl, cacheNamespace),
    cacheNamespace
  };
}

function vocabularyStateSpec() {
  const spec = globalThis.BWReaderRuntime?.vocabularyState;
  if (
    !spec ||
    spec.CONTRACT !== "vocabulary-state/1" ||
    spec.COLLECTION !== "vocabulary-state"
  ) {
    throw Object.assign(new Error("扩展缺少词汇状态语义仓库"), {
      code: "BW_VOCABULARY_STATE_DEPENDENCY"
    });
  }
  return spec;
}
async function vocabularyStateScope(captured) {
  fenceCapturedAccount(captured);
  const digest = await sha256Hex(
    "bw-vocabulary-state-scope-v1:" + captured.lease.namespace
  );
  fenceCapturedAccount(captured);
  return "vstate-scope-v1-" + digest;
}
async function runVocabularyStateOperation(captured, operation, payload) {
  const spec = vocabularyStateSpec();
  checkJsonSize(payload || {}, 128 * 1024, "词汇状态请求");
  fenceCapturedAccount(captured);
  const store = await cacheStore(captured, spec.COLLECTION);
  fenceCapturedAccount(captured);
  if (operation === "LIST") {
    const query = boundedQuery(payload?.query || {});
    query.includeDeleted = false;
    const result = await store.list(spec.COLLECTION, query);
    fenceCapturedAccount(captured);
    checkJsonSize(result, MAX_PROVIDER_RESPONSE_BYTES, "词汇状态响应");
    return result;
  }
  if (operation !== "PUT") {
    throw Object.assign(new Error("不允许的词汇状态操作"), {
      code: "BW_VOCABULARY_STATE_OPERATION"
    });
  }
  const record = spec.normalizeRecord(payload?.record);
  const requestedMutationId = String(payload?.mutationId || "");
  if (
    !/^vstate-mut-v1:[A-Za-z0-9._:-]{1,480}$/.test(requestedMutationId)
  ) {
    throw Object.assign(new Error("词汇状态写入缺少稳定 mutationId"), {
      code: "BW_VOCABULARY_STATE_MUTATION"
    });
  }
  return queueCacheWrite(captured, spec.COLLECTION, record.id, async () => {
    fenceCapturedAccount(captured);
    const current = await store.get(spec.COLLECTION, record.id, {
      includeDeleted: true
    });
    fenceCapturedAccount(captured);
    if (
      current &&
      !current.deleted &&
      JSON.stringify(current.value || null) === JSON.stringify(record)
    ) {
      return current;
    }
    const saved = await store.put(spec.COLLECTION, record, {
      id: record.id,
      ifRev: Number(current?.rev || 0),
      mutationId: requestedMutationId
    });
    fenceCapturedAccount(captured);
    return saved;
  });
}

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "bw-vocabulary-state") return;
  let binding;
  try { binding = contentSenderBinding(port.sender); }
  catch (_) {
    try { port.disconnect(); } catch (_) {}
    return;
  }
  const entry = {
    port,
    captured: null,
    namespace: "",
    scope: "",
    ready: false,
    closed: false,
    unsubscribeAccount: null,
    binding,
    invalidate: null
  };
  const send = (message) => {
    if (entry.closed) return;
    port.postMessage(Object.assign({
      protocol: VOCABULARY_STATE_PROTOCOL
    }, message));
  };
  const close = () => {
    if (entry.closed) return;
    entry.closed = true;
    entry.ready = false;
    vocabularyStatePorts.delete(entry);
    try { entry.unsubscribeAccount?.(); } catch (_) {}
    entry.unsubscribeAccount = null;
  };
  entry.invalidate = (reason) => {
    if (entry.closed) return;
    try {
      send({
        type: "INVALIDATED",
        code: "BW_VOCABULARY_STATE_STALE",
        reason: String(reason || "account-context-stale")
      });
    } catch (_) {}
    close();
    try { port.disconnect(); } catch (_) {}
  };
  vocabularyStatePorts.add(entry);
  port.onDisconnect.addListener(close);
  port.onMessage.addListener(async (message) => {
    if (
      entry.closed ||
      !message ||
      message.protocol !== VOCABULARY_STATE_PROTOCOL ||
      message.type !== "CALL" ||
      !/^[A-Za-z0-9._:-]{1,120}$/.test(String(message.id || ""))
    ) return;
    if (!providerBindingMatchesSender(entry.binding, port.sender)) {
      entry.invalidate("sender-document-changed");
      return;
    }
    if (!entry.ready || !entry.captured) {
      send({
        type: "RESULT",
        id: message.id,
        ok: false,
        code: "BW_VOCABULARY_STATE_NOT_READY",
        error: "词汇状态仓库尚未就绪"
      });
      return;
    }
    try {
      fenceCapturedAccount(entry.captured);
      const data = await runVocabularyStateOperation(
        entry.captured,
        String(message.operation || ""),
        message.payload || {}
      );
      fenceCapturedAccount(entry.captured);
      send({ type: "RESULT", id: message.id, ok: true, data });
    } catch (error) {
      const code = String(error?.code || "BW_VOCABULARY_STATE_OPERATION");
      if (
        code === "BW_ACCOUNT_CONTEXT_STALE" ||
        code === "BW_ACCOUNT_CONTEXT_UNAVAILABLE"
      ) {
        entry.invalidate("account-context-stale");
        return;
      }
      send({
        type: "RESULT",
        id: message.id,
        ok: false,
        code,
        error: error instanceof Error ? error.message : String(error)
      });
    }
  });
  Promise.resolve().then(async () => {
    if (!providerBindingMatchesSender(entry.binding, port.sender)) {
      throw Object.assign(new Error("词汇状态页面身份已变化"), {
        code: "BW_VOCABULARY_STATE_SENDER"
      });
    }
    const captured = await capturePersistentAccountForContentSender(port.sender);
    const scope = await vocabularyStateScope(captured);
    await cacheStore(captured, vocabularyStateSpec().COLLECTION);
    fenceCapturedAccount(captured);
    if (!providerBindingMatchesSender(entry.binding, port.sender)) {
      throw Object.assign(new Error("词汇状态页面身份已变化"), {
        code: "BW_VOCABULARY_STATE_SENDER"
      });
    }
    if (entry.closed) return;
    entry.captured = captured;
    entry.namespace = captured.lease.namespace;
    entry.scope = scope;
    entry.ready = true;
    entry.unsubscribeAccount = persistentAccountContext.subscribe(() => {
      if (
        entry.closed ||
        !entry.captured ||
        persistentAccountContext.isCurrent(entry.captured.lease)
      ) return;
      entry.invalidate("account-context-changed");
    });
    send({ type: "READY", scope });
  }).catch((error) => {
    if (entry.closed) return;
    try {
      send({
        type: "ERROR",
        code: String(error?.code || "BW_VOCABULARY_STATE_UNAVAILABLE"),
        error: error instanceof Error ? error.message : String(error)
      });
    } catch (_) {}
    close();
    try { port.disconnect(); } catch (_) {}
  });
});

function documentNoteRepositorySpec() {
  const spec = globalThis.BWReaderRuntime?.documentNoteRepository;
  if (
    !spec ||
    spec.CONTRACT !== "document-note-repository/1" ||
    spec.COLLECTION !== "document-notes" ||
    typeof spec.createDocumentNoteRepository !== "function"
  ) {
    throw Object.assign(new Error("扩展缺少文档便签本地仓库"), {
      code: "BW_DOCUMENT_NOTES_DEPENDENCY"
    });
  }
  return spec;
}
function closeDocumentNoteVault(entry) {
  if (!entry || entry.closed) return;
  entry.closed = true;
  documentNoteVaults.delete(entry.namespace);
  try { entry.store?.close?.(); } catch (_) {}
}
function pruneDocumentNoteVaults() {
  const active = persistentAccountContext.snapshot();
  for (const entry of documentNoteVaults.values()) {
    if (
      entry.closed ||
      entry.activePorts > 0 ||
      entry.inFlight > 0 ||
      (active.active && active.namespace === entry.namespace)
    ) continue;
    closeDocumentNoteVault(entry);
  }
}
function retainDocumentNoteVault(entry) {
  if (!entry || entry.closed) {
    throw Object.assign(new Error("文档便签 Vault 已关闭"), {
      code: "BW_DOCUMENT_NOTES_STALE"
    });
  }
  entry.activePorts += 1;
  entry.lastUsedAt = Date.now();
}
function releaseDocumentNoteVault(entry) {
  if (!entry || entry.closed) return;
  entry.activePorts = Math.max(0, entry.activePorts - 1);
  entry.lastUsedAt = Date.now();
  pruneDocumentNoteVaults();
}
async function documentNoteVaultFor(captured) {
  fenceCapturedAccount(captured);
  const namespace = captured.lease.namespace;
  const cached = documentNoteVaults.get(namespace);
  if (cached && !cached.closed) {
    cached.lastUsedAt = Date.now();
    return cached;
  }
  const deviceId = await extensionInstallId();
  fenceCapturedAccount(captured);
  const raced = documentNoteVaults.get(namespace);
  if (raced && !raced.closed) {
    raced.lastUsedAt = Date.now();
    return raced;
  }
  const indexed = globalThis.BWReaderRuntime?.indexedDBStore;
  if (!indexed?.createIndexedDBDataStore) {
    throw Object.assign(new Error("扩展文档便签 IndexedDB Vault 未加载"), {
      code: "BW_DOCUMENT_NOTES_DEPENDENCY"
    });
  }
  const store = indexed.createIndexedDBDataStore({
    dbName: "bw-reader-extension-document-notes-v1-" + namespace,
    deviceId,
    channelName: "bw-reader-extension-document-notes-events-v1-" + namespace,
    causalCollections: []
  });
  let repository;
  try {
    repository = documentNoteRepositorySpec().createDocumentNoteRepository({
      store,
      dataRegistry: globalThis.BWReaderRuntime?.dataRegistry
    });
  } catch (error) {
    try { store?.close?.(); } catch (_) {}
    throw error;
  }
  const entry = {
    namespace,
    store,
    repository,
    activePorts: 0,
    inFlight: 0,
    lastUsedAt: Date.now(),
    closed: false
  };
  documentNoteVaults.set(namespace, entry);
  return entry;
}
async function runWithDocumentNoteVault(entry, work) {
  if (!entry || entry.closed) {
    throw Object.assign(new Error("文档便签 Vault 已关闭"), {
      code: "BW_DOCUMENT_NOTES_STALE"
    });
  }
  entry.inFlight += 1;
  entry.lastUsedAt = Date.now();
  try {
    return await work(entry.repository);
  } finally {
    entry.inFlight = Math.max(0, entry.inFlight - 1);
    entry.lastUsedAt = Date.now();
    pruneDocumentNoteVaults();
  }
}
async function documentNoteAccountScope(captured) {
  fenceCapturedAccount(captured);
  const digest = await sha256Hex(
    "bw-document-notes-scope-v1:" + captured.lease.namespace
  );
  fenceCapturedAccount(captured);
  return "document-notes-scope-v1-" + digest;
}
function documentNotePlainObject(value) {
  return !!(
    value &&
    Object.prototype.toString.call(value) === "[object Object]"
  );
}
function checkDocumentNoteJsonSize(value, limit, label) {
  const size = jsonByteLength(value);
  if (size > limit) {
    throw Object.assign(new Error(label + " 超出大小限制"), {
      code: "BW_DOCUMENT_NOTES_PAYLOAD",
      details: { limit, size }
    });
  }
}
function cloneDocumentNoteJSON(value, label) {
  const dataStore = globalThis.BWReaderRuntime?.dataStore;
  if (
    !dataStore ||
    dataStore.CONTRACT !== "data-store/1" ||
    typeof dataStore.cloneJSON !== "function"
  ) {
    throw Object.assign(new Error("扩展缺少 DataStore JSON 合同"), {
      code: "BW_DOCUMENT_NOTES_DEPENDENCY"
    });
  }
  // 不用 JSON.stringify/parse 直接“清洗”：cloneJSON 会先拒绝 undefined、
  // NaN、稀疏数组、二进制和访问器，保持 DataStore 的 fail-closed 语义。
  return dataStore.cloneJSON(value, label || "document-note");
}
function documentNoteAllowedKeys(value, allowed, label) {
  if (!documentNotePlainObject(value)) {
    throw Object.assign(new Error(label + " 必须是普通对象"), {
      code: "BW_DOCUMENT_NOTES_PAYLOAD"
    });
  }
  for (const key of Object.keys(value)) {
    if (!allowed.has(key)) {
      throw Object.assign(new Error(label + " 含有不允许的字段：" + key), {
        code: "BW_DOCUMENT_NOTES_PAYLOAD"
      });
    }
  }
}
function rejectDocumentIdentity(value, label) {
  if (!value || typeof value !== "object") return;
  for (const key of ["documentId", "url", "pageUrl"]) {
    if (Object.prototype.hasOwnProperty.call(value, key)) {
      throw Object.assign(new Error(label + " 不能提供 " + key), {
        code: "BW_DOCUMENT_NOTES_IDENTITY"
      });
    }
  }
}
function checkedDocumentNoteId(value) {
  value = String(value || "").trim();
  if (!/^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/.test(value)) {
    throw Object.assign(new Error("noteId 无效"), {
      code: "BW_DOCUMENT_NOTES_PAYLOAD"
    });
  }
  return value;
}
function checkedDocumentNoteOptions(value, operation) {
  value = value == null ? {} : value;
  documentNoteAllowedKeys(
    value,
    new Set(["ifRev", "mutationId"]),
    operation + ".options"
  );
  const mutationId = value.mutationId;
  if (
    typeof mutationId !== "string" ||
    !mutationId.trim() ||
    mutationId.length > 512 ||
    /[\u0000-\u001f\u007f]/.test(mutationId)
  ) {
    throw Object.assign(new Error(operation + " 必须携带稳定且有界的 mutationId"), {
      code: "BW_DOCUMENT_NOTES_MUTATION"
    });
  }
  const result = { mutationId };
  if (operation !== "CREATE") {
    const ifRev = value.ifRev;
    if (
      typeof ifRev !== "number" ||
      !Number.isSafeInteger(ifRev) ||
      ifRev < 1
    ) {
      throw Object.assign(new Error(operation + " 必须携带正整数 ifRev"), {
        code: "BW_DOCUMENT_NOTES_REVISION"
      });
    }
    result.ifRev = ifRev;
  } else if (value.ifRev != null) {
    if (
      typeof value.ifRev !== "number" ||
      !Number.isSafeInteger(value.ifRev) ||
      value.ifRev !== 0
    ) {
      throw Object.assign(new Error("CREATE.ifRev 只能为 0"), {
        code: "BW_DOCUMENT_NOTES_REVISION"
      });
    }
    result.ifRev = 0;
  }
  return result;
}
function checkedDocumentNoteQuery(value) {
  value = value == null ? {} : value;
  documentNoteAllowedKeys(
    value,
    new Set(["includeDeleted", "limit", "offset"]),
    "query"
  );
  rejectDocumentIdentity(value, "query");
  const result = {
    includeDeleted: value.includeDeleted === true,
    limit: Math.max(
      1,
      Math.min(MAX_DOCUMENT_NOTE_PAGE, Number(value.limit) || MAX_DOCUMENT_NOTE_PAGE)
    )
  };
  if (value.offset != null) {
    result.offset = Math.max(0, Number(value.offset) || 0);
  }
  return result;
}
function validateDocumentNotePayload(operation, payload, documentId) {
  payload = payload == null ? {} : payload;
  checkDocumentNoteJsonSize(
    payload,
    MAX_DOCUMENT_NOTE_REQUEST_BYTES,
    "文档便签请求"
  );
  rejectDocumentIdentity(payload, "请求");
  if (operation === "NEW_ID") {
    documentNoteAllowedKeys(payload, new Set(), "NEW_ID");
    return {};
  }
  if (operation === "LIST") {
    documentNoteAllowedKeys(payload, new Set(["query"]), "LIST");
    return { query: checkedDocumentNoteQuery(payload.query) };
  }
  if (operation === "GET") {
    documentNoteAllowedKeys(payload, new Set(["noteId", "query"]), "GET");
    return {
      noteId: checkedDocumentNoteId(payload.noteId),
      query: checkedDocumentNoteQuery(payload.query)
    };
  }
  if (operation === "CREATE") {
    documentNoteAllowedKeys(payload, new Set(["input", "options"]), "CREATE");
    if (!documentNotePlainObject(payload.input)) {
      throw Object.assign(new Error("CREATE.input 必须是普通对象"), {
        code: "BW_DOCUMENT_NOTES_PAYLOAD"
      });
    }
    rejectDocumentIdentity(payload.input, "CREATE.input");
    if (!documentNotePlainObject(payload.input.anchor)) {
      throw Object.assign(new Error("CREATE.input.anchor 必须是对象"), {
        code: "BW_DOCUMENT_NOTES_PAYLOAD"
      });
    }
    rejectDocumentIdentity(payload.input.anchor, "CREATE.input.anchor");
    const input = cloneDocumentNoteJSON(payload.input, "CREATE.input");
    input.documentId = documentId;
    input.anchor = Object.assign({}, input.anchor, { documentId });
    return {
      input,
      options: checkedDocumentNoteOptions(payload.options, "CREATE")
    };
  }
  if (operation === "PATCH") {
    documentNoteAllowedKeys(
      payload,
      new Set(["noteId", "changes", "options"]),
      "PATCH"
    );
    if (!documentNotePlainObject(payload.changes)) {
      throw Object.assign(new Error("PATCH.changes 必须是普通对象"), {
        code: "BW_DOCUMENT_NOTES_PAYLOAD"
      });
    }
    rejectDocumentIdentity(payload.changes, "PATCH.changes");
    const changes = cloneDocumentNoteJSON(payload.changes, "PATCH.changes");
    if (Object.prototype.hasOwnProperty.call(changes, "anchor")) {
      if (!documentNotePlainObject(changes.anchor)) {
        throw Object.assign(new Error("PATCH.changes.anchor 必须是对象"), {
          code: "BW_DOCUMENT_NOTES_PAYLOAD"
        });
      }
      rejectDocumentIdentity(changes.anchor, "PATCH.changes.anchor");
      changes.anchor = Object.assign({}, changes.anchor, { documentId });
    }
    return {
      noteId: checkedDocumentNoteId(payload.noteId),
      changes,
      options: checkedDocumentNoteOptions(payload.options, "PATCH")
    };
  }
  if (operation === "REMOVE") {
    documentNoteAllowedKeys(
      payload,
      new Set(["noteId", "options"]),
      "REMOVE"
    );
    return {
      noteId: checkedDocumentNoteId(payload.noteId),
      options: checkedDocumentNoteOptions(payload.options, "REMOVE")
    };
  }
  throw Object.assign(new Error("不允许的文档便签操作"), {
    code: "BW_DOCUMENT_NOTES_OPERATION"
  });
}
async function runDocumentNoteOperation(
  repository,
  documentId,
  operation,
  payload
) {
  const args = validateDocumentNotePayload(operation, payload, documentId);
  if (operation === "NEW_ID") return repository.newNoteId();
  if (operation === "LIST") return repository.list(documentId, args.query);
  if (operation === "GET") {
    return repository.get(documentId, args.noteId, args.query);
  }
  if (operation === "CREATE") {
    return repository.create(args.input, args.options);
  }
  if (operation === "PATCH") {
    return repository.patch(
      documentId,
      args.noteId,
      args.changes,
      args.options
    );
  }
  if (operation === "REMOVE") {
    return repository.remove(documentId, args.noteId, args.options);
  }
  throw Object.assign(new Error("不允许的文档便签操作"), {
    code: "BW_DOCUMENT_NOTES_OPERATION"
  });
}

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "bw-document-notes") return;
  let binding;
  try {
    binding = documentNoteSenderBinding(port.sender);
  } catch (error) {
    try {
      port.postMessage({
        protocol: DOCUMENT_NOTES_PROTOCOL,
        type: "ERROR",
        code: String(error?.code || "BW_DOCUMENT_NOTES_SENDER"),
        error: error instanceof Error ? error.message : String(error)
      });
    } catch (_) {}
    try { port.disconnect(); } catch (_) {}
    return;
  }
  const entry = {
    port,
    binding,
    captured: null,
    namespace: "",
    scope: "",
    vaultEntry: null,
    ready: false,
    closed: false,
    generation: 1,
    inFlightRequests: new Map(),
    unsubscribeAccount: null,
    unsubscribeNotes: null,
    invalidate: null
  };
  const send = (message) => {
    if (entry.closed) return;
    port.postMessage(Object.assign({
      protocol: DOCUMENT_NOTES_PROTOCOL
    }, message));
  };
  const close = () => {
    if (entry.closed) return;
    entry.closed = true;
    entry.ready = false;
    entry.generation += 1;
    documentNotePorts.delete(entry);
    try { entry.unsubscribeAccount?.(); } catch (_) {}
    try { entry.unsubscribeNotes?.(); } catch (_) {}
    entry.unsubscribeAccount = null;
    entry.unsubscribeNotes = null;
    const vaultEntry = entry.vaultEntry;
    entry.vaultEntry = null;
    releaseDocumentNoteVault(vaultEntry);
  };
  const unknownOutcomeDetails = (details) => {
    details = Object.assign({}, details || {});
    const mutationIds = [];
    const seenMutationIds = new Set();
    const addMutationId = (value) => {
      if (
        typeof value !== "string" ||
        !value ||
        seenMutationIds.has(value)
      ) return;
      seenMutationIds.add(value);
      mutationIds.push(value);
    };
    for (const request of entry.inFlightRequests.values()) {
      if (
        ["CREATE", "PATCH", "REMOVE"].includes(request.operation) &&
        typeof request.mutationId === "string"
      ) addMutationId(request.mutationId);
    }
    addMutationId(details.mutationId);
    if (details.mutationId == null) delete details.mutationId;
    if (mutationIds.length) {
      details.outcomeUnknown = true;
      details.mutationIds = mutationIds.slice();
      if (mutationIds.length === 1) details.mutationId = mutationIds[0];
    }
    return Object.keys(details).length ? details : null;
  };
  entry.invalidate = (reason, details) => {
    if (entry.closed) return;
    try {
      send({
        type: "INVALIDATED",
        code: "BW_DOCUMENT_NOTES_STALE",
        reason: String(reason || "document-context-stale"),
        details: unknownOutcomeDetails(details)
      });
    } catch (_) {}
    close();
    try { port.disconnect(); } catch (_) {}
  };
  const fence = (generation, captured) => {
    if (
      entry.closed ||
      !documentNotePorts.has(entry) ||
      entry.generation !== generation
    ) {
      throw Object.assign(new Error("文档便签连接已经失效"), {
        code: "BW_DOCUMENT_NOTES_STALE"
      });
    }
    if (!documentNoteBindingMatchesSender(binding, port.sender)) {
      throw Object.assign(new Error("网页文档身份已经变化"), {
        code: "BW_DOCUMENT_NOTES_SENDER"
      });
    }
    fenceCapturedAccount(captured);
    if (captured.lease.namespace !== entry.namespace) {
      throw Object.assign(new Error("文档便签账户已经变化"), {
        code: "BW_ACCOUNT_CONTEXT_STALE"
      });
    }
  };
  documentNotePorts.add(entry);
  port.onDisconnect.addListener(close);
  port.onMessage.addListener(async (message) => {
    if (
      entry.closed ||
      !message ||
      message.protocol !== DOCUMENT_NOTES_PROTOCOL ||
      message.type !== "CALL" ||
      !/^[A-Za-z0-9._:-]{1,120}$/.test(String(message.id || ""))
    ) return;
    const requestId = String(message.id);
    if (entry.inFlightRequests.has(requestId)) {
      const duplicateMutationId =
        typeof message.payload?.options?.mutationId === "string"
          ? message.payload.options.mutationId
          : "";
      entry.invalidate("duplicate-request-id", {
        requestId,
        mutationId: duplicateMutationId || undefined
      });
      return;
    }
    if (!documentNoteBindingMatchesSender(binding, port.sender)) {
      entry.invalidate("sender-document-changed");
      return;
    }
    if (!entry.ready || !entry.captured || !entry.vaultEntry) {
      send({
        type: "RESULT",
        id: requestId,
        ok: false,
        code: "BW_DOCUMENT_NOTES_NOT_READY",
        error: "文档便签仓库尚未就绪"
      });
      return;
    }
    const operation = String(message.operation || "");
    const request = {
      operation,
      mutationId:
        typeof message.payload?.options?.mutationId === "string"
          ? message.payload.options.mutationId
          : ""
    };
    entry.inFlightRequests.set(requestId, request);
    const generation = entry.generation;
    const captured = entry.captured;
    try {
      fence(generation, captured);
      const data = await runWithDocumentNoteVault(
        entry.vaultEntry,
        (repository) => runDocumentNoteOperation(
          repository,
          binding.documentId,
          operation,
          message.payload || {}
        )
      );
      fence(generation, captured);
      checkDocumentNoteJsonSize(
        data,
        MAX_DOCUMENT_NOTE_RESPONSE_BYTES,
        "文档便签响应"
      );
      send({ type: "RESULT", id: requestId, ok: true, data });
    } catch (error) {
      const code = String(error?.code || "BW_DOCUMENT_NOTES_OPERATION");
      if (
        [
          "BW_ACCOUNT_CONTEXT_STALE",
          "BW_ACCOUNT_CONTEXT_UNAVAILABLE",
          "BW_DOCUMENT_NOTES_SENDER",
          "BW_DOCUMENT_NOTES_STALE"
        ].includes(code)
      ) {
        const details = Object.assign({}, error?.details || {});
        if (["CREATE", "PATCH", "REMOVE"].includes(operation)) {
          Object.assign(details, {
            outcomeUnknown: true,
            mutationId: request.mutationId || undefined
          });
        }
        entry.invalidate(code, details);
        return;
      }
      send({
        type: "RESULT",
        id: requestId,
        ok: false,
        code,
        error: error instanceof Error ? error.message : String(error),
        details: error?.details || null
      });
    } finally {
      if (entry.inFlightRequests.get(requestId) === request) {
        entry.inFlightRequests.delete(requestId);
      }
    }
  });
  Promise.resolve().then(async () => {
    if (!documentNoteBindingMatchesSender(binding, port.sender)) {
      throw Object.assign(new Error("网页文档身份已经变化"), {
        code: "BW_DOCUMENT_NOTES_SENDER"
      });
    }
    const captured = await capturePersistentAccountForContentSender(port.sender);
    const vaultEntry = await documentNoteVaultFor(captured);
    const scope = await documentNoteAccountScope(captured);
    fenceCapturedAccount(captured);
    if (
      entry.closed ||
      !documentNoteBindingMatchesSender(binding, port.sender)
    ) {
      throw Object.assign(new Error("网页文档身份已经变化"), {
        code: "BW_DOCUMENT_NOTES_SENDER"
      });
    }
    entry.captured = captured;
    entry.namespace = captured.lease.namespace;
    entry.scope = scope;
    entry.vaultEntry = vaultEntry;
    retainDocumentNoteVault(vaultEntry);
    entry.unsubscribeNotes = vaultEntry.repository.subscribe(
      binding.documentId,
      (event) => {
        if (entry.closed || !entry.ready) return;
        try {
          fence(entry.generation, entry.captured);
          if (event?.note?.documentId !== binding.documentId) return;
          checkDocumentNoteJsonSize(
            event,
            MAX_DOCUMENT_NOTE_RESPONSE_BYTES,
            "文档便签变更"
          );
          send({ type: "CHANGE", data: event });
        } catch (_) {
          entry.invalidate("document-change-fence");
        }
      }
    );
    entry.unsubscribeAccount = persistentAccountContext.subscribe(() => {
      if (
        entry.closed ||
        !entry.captured ||
        persistentAccountContext.isCurrent(entry.captured.lease)
      ) return;
      entry.invalidate("account-context-changed");
    });
    entry.ready = true;
    send({
      type: "READY",
      documentId: binding.documentId,
      scope
    });
  }).catch((error) => {
    pruneDocumentNoteVaults();
    if (entry.closed) return;
    try {
      send({
        type: "ERROR",
        code: String(error?.code || "BW_DOCUMENT_NOTES_UNAVAILABLE"),
        error: error instanceof Error ? error.message : String(error)
      });
    } catch (_) {}
    close();
    try { port.disconnect(); } catch (_) {}
  });
});

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!sender || sender.id !== chrome.runtime.id) return false;
  if (
    !ALLOWED_MESSAGES.has(message?.type) &&
    !ACCOUNT_MESSAGES.has(message?.type) &&
    !LOCAL_STORAGE_MESSAGES.has(message?.type) &&
    !PAGE_CARD_PRESENTATION_MESSAGES.has(message?.type) &&
    !TRANSLATION_CACHE_MESSAGES.has(message?.type)
  ) {
    return false;
  }
  const operation = LOCAL_STORAGE_MESSAGES.has(message.type)
    ? handleLocalStorageMessage(message, sender)
    : PAGE_CARD_PRESENTATION_MESSAGES.has(message.type)
      ? handlePageCardPresentationMessage(message, sender)
    : TRANSLATION_CACHE_MESSAGES.has(message.type)
      ? handleTranslationCacheMessage(message, sender)
    : ACCOUNT_MESSAGES.has(message.type)
      ? handleAccountMessage(message, sender)
      : Promise.resolve().then(async () => {
        const captured = await capturePersistentAccountForContentSender(sender);
        return handleMessage(message, captured);
      });
  operation
    .then((data) => sendResponse({ ok: true, data }))
    .catch((error) => {
      const explicitCode = String(error?.code || "");
      const code = explicitCode || "BW_ACCOUNT_OPERATION";
      const isAccountOperation = ACCOUNT_MESSAGES.has(message.type);
      sendResponse({
        ok: false,
        code,
        // 账户入口只透传扩展自己定义的错误；浏览器、网络或上游抛出的任意
        // 文本都可能意外包含 Authorization，必须在到达 popup 前截断。
        error: isAccountOperation &&
          (!explicitCode || !PUBLIC_ACCOUNT_ERROR_CODES.has(explicitCode))
          ? "设备令牌操作失败，请稍后重试"
          : (error instanceof Error ? error.message : String(error))
      });
    });
  return true;   // 异步 sendResponse
});

async function verifyTokenOwner(captured, token) {
  token = String(token || "").trim();
  if (!token || token.length > 8192) {
    throw Object.assign(new Error("请输入有效的设备令牌"), {
      code: "BW_ACCOUNT_TOKEN_INVALID"
    });
  }
  fenceCapturedAccount(captured);
  const response = await fetch(ORIGIN + "/api/reader/token-owner", {
    method: "POST",
    headers: { Authorization: `Bearer ${token}` },
    credentials: "omit",
    cache: "no-store"
  });
  fenceCapturedAccount(captured);
  const contentType = response.headers.get("Content-Type") || "";
  if (response.redirected || !contentType.includes("application/json")) {
    throw Object.assign(new Error("设备令牌无效，服务器没有返回账户证明"), {
      code: "BW_ACCOUNT_TOKEN_INVALID"
    });
  }
  let data;
  try {
    data = await response.json();
  } catch (_) {
    fenceCapturedAccount(captured);
    throw Object.assign(new Error("设备令牌验证失败"), {
      code: "BW_ACCOUNT_TOKEN_INVALID"
    });
  }
  fenceCapturedAccount(captured);
  if (!response.ok || data?.ok !== true) {
    // 服务器错误文本不向 popup 透传：即使上游错误地回显 Authorization，
    // 扩展也不能把用户刚粘贴的 token 明文带回页面或弹窗。
    throw Object.assign(new Error("设备令牌验证失败"), {
      code: "BW_ACCOUNT_TOKEN_INVALID"
    });
  }
  if (String(data.storage_namespace || "") !== captured.lease.namespace) {
    throw Object.assign(new Error("设备令牌不属于当前阅读器账户，已拒绝保存"), {
      code: "BW_ACCOUNT_TOKEN_OWNER_MISMATCH"
    });
  }
  return true;
}

async function accountCredentialStatus(captured) {
  fenceCapturedAccount(captured);
  const credential = await accountStorage.credentialStatus(
    captured.entry.accountContext,
    captured.lease
  );
  fenceCapturedAccount(captured);
  const legacyQuarantine = await accountStorage.legacyInventory();
  fenceCapturedAccount(captured);
  const syncControl = await providerSyncControlFor(captured);
  const sync = await syncControl.status();
  fenceCapturedAccount(captured);
  return {
    namespace: captured.lease.namespace,
    credential,
    legacyQuarantine,
    sync
  };
}

async function handleAccountMessage(message, sender) {
  if (
    [
      "BW_ACCOUNT_STATUS",
      "BW_ACCOUNT_TOKEN_SAVE",
      "BW_ACCOUNT_TOKEN_TEST",
      "BW_SYNC_STATUS"
    ]
      .includes(message.type)
  ) {
    const captured = await captureProviderForPopup(sender, message.target || {});
    if (message.type === "BW_SYNC_STATUS") {
      return (await providerSyncControlFor(captured)).status();
    }
    if (message.type === "BW_ACCOUNT_STATUS") {
      return accountCredentialStatus(captured);
    }
    if (message.type === "BW_ACCOUNT_TOKEN_SAVE") {
      const token = String(message.payload?.token || "").trim();
      await verifyTokenOwner(captured, token);
      fenceCapturedAccount(captured);
      await accountStorage.saveVerifiedToken(
        captured.entry.accountContext,
        captured.lease,
        token
      );
      fenceCapturedAccount(captured);
      void startActiveProviderSync("token-saved", {
        captured,
        runNow: true
      });
      void promoteDirectHost("token-saved");
      return accountCredentialStatus(captured);
    }
    const token = await accountStorage.activeToken(
      captured.entry.accountContext,
      captured.lease
    );
    fenceCapturedAccount(captured);
    if (!token) {
      throw Object.assign(new Error("当前账户尚未保存设备令牌"), {
        code: "BW_ACCOUNT_TOKEN_MISSING"
      });
    }
    await verifyTokenOwner(captured, token);
    fenceCapturedAccount(captured);
    await accountStorage.saveVerifiedToken(
      captured.entry.accountContext,
      captured.lease,
      token
    );
    fenceCapturedAccount(captured);
    void startActiveProviderSync("token-verified", {
      captured,
      runNow: true
    });
    void promoteDirectHost("token-verified");
    return accountCredentialStatus(captured);
  }

  throw Object.assign(new Error("不支持的账户操作"), {
    code: "BW_ACCOUNT_OPERATION"
  });
}

async function apiRequest(path, init = {}, captured) {
  fenceCapturedAccount(captured);
  const token = await accountStorage.activeToken(
    captured.entry.accountContext,
    captured.lease
  );
  fenceCapturedAccount(captured);
  if (!token) {
    throw Object.assign(new Error("请先在扩展设置中保存当前账户的设备令牌"), {
      code: "BW_ACCOUNT_TOKEN_MISSING"
    });
  }

  const headers = new Headers(init.headers || {});
  headers.set("Authorization", `Bearer ${token}`);
  if (init.body) headers.set("Content-Type", "application/json");

  const response = await fetch(ORIGIN + path, {
    ...init, headers, credentials: "omit", cache: "no-store"
  });
  fenceCapturedAccount(captured);

  const contentType = response.headers.get("Content-Type") || "";
  // token 失效时服务端会重定向到登录 HTML;识别出来给明确报错,而不是把 HTML 当 JSON
  if (response.redirected || !contentType.includes("application/json")) {
    throw Object.assign(
      new Error("设备令牌无效,或服务器返回了登录页面"),
      {
        code: "BW_ACCOUNT_TOKEN_INVALID",
        status: Number(response.status) || 0,
        retryable: false
      }
    );
  }

  const data = await response.json();
  fenceCapturedAccount(captured);
  if (!response.ok || data.ok === false) {
    throw Object.assign(
      new Error(data.error || `服务器错误 HTTP ${response.status}`),
      {
        code: String(data.code || "BW_API_HTTP"),
        status: Number(response.status) || 0,
        retryable: response.status === 408 ||
          response.status === 429 ||
          response.status >= 500
      }
    );
  }
  return data;
}

function webContext(payload) {
  const pageUrl = new URL(payload.url);
  if (!["http:", "https:"].includes(pageUrl.protocol)) throw new Error("不支持当前页面");
  pageUrl.hash = "";
  return pageUrl;
}

async function handleMessage(message, captured) {
  if (message.type === "PING") {
    return apiRequest("/pdf/api/ping", {}, captured);
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
    return apiRequest(`/pdf/api/dict-quick?${params}`, {}, captured);
  }

  if (message.type === "TRANSLATE") {
    const p = message.payload || {};
    const text = String(p.text || "").trim();
    if (!text) throw new Error("请先选中文字");
    if (text.length > 5000) throw new Error("翻译选区过长");
    return apiRequest("/pdf/api/translate", {
      method: "POST",
      body: JSON.stringify({ text, target_lang: "中文" })
    }, captured);
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
    }, captured);
  }

  throw new Error("不支持的操作");
}

// ── bw-fetch 长连 port:content 门面(facade.js __bwReaderFetch)的服务端 ──
// rc-* 共享层的所有请求(含 SSE 流式)经此转发:补 Bearer、只放行本服务 ORIGIN、流式分片回传。
chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "bw-fetch") return;
  if (!isTopLevelOwnContentSender(port.sender)) {
    try { port.disconnect(); } catch (_) {}
    return;
  }
  const aborts = new Map();
  // 一个 content port 对应一个顶层 document 生命周期；导航会销毁 port 并得到新 UUID。
  // 服务端会再按账户分区，URL 本身永远不离开浏览器。
  const webTranslateDocumentKey = newWebTranslateDocumentKey();
  const post = (captured, message) => {
    fenceCapturedAccount(captured);
    port.postMessage(message);
  };
  const jsonError = (captured, id, message, status = 401) => {
    const body = JSON.stringify({ ok: false, error: message });
    post(captured, {
      id,
      type: "head",
      status,
      statusText: "Unauthorized",
      headers: { "content-type": "application/json; charset=utf-8" }
    });
    post(captured, {
      id,
      type: "chunk",
      b64: btoa(unescape(encodeURIComponent(body)))
    });
    post(captured, { id, type: "done" });
  };
  port.onMessage.addListener(async (m) => {
    if (m.abort) {
      const controller = aborts.get(m.abort);
      if (controller) controller.abort();
      aborts.delete(m.abort);
      return;
    }
    const { id, url } = m || {};
    let init = (m || {}).init;
    if (!id || typeof url !== "string") {
      try {
        port.postMessage({
          id,
          type: "error",
          code: "BW_FETCH_OPERATION",
          error: "blocked: invalid network request"
        });
      } catch (_) {}
      return;
    }
    try {
      const checked = checkedBwFetchRequest(url, init);
      init = normalizedBwFetchInit(checked, init);
    } catch (error) {
      try {
        port.postMessage({
          id,
          type: "error",
          code: String(error?.code || "BW_FETCH_OPERATION"),
          error: String(error?.message || error)
        });
      } catch (_) {}
      return;
    }
    let captured;
    let served = false;
    try {
      // 每个请求都捕获扩展持久账户的新租约。普通网页不依赖任何 PWA 标签页；
      // 若书籍 PWA 切换账户，旧请求会在下一道 fence 处失效。
      captured = await capturePersistentAccountForContentSender(port.sender);
      const isDq = url.startsWith(ORIGIN + "/pdf/api/dict-quick?") &&
        (!init || !init.method || init.method === "GET");
      let dqKey = null;
      if (isDq) {
        const u = new URL(url);
        dqKey = "dq:" + (u.searchParams.get("word") || "") + ":" + (u.searchParams.get("langs") || "");
        const hit = await dictionaryCacheGet(captured, dqKey);
        if (hit && hit.body) {
          post(captured, {
            id,
            type: "head",
            status: 200,
            statusText: "OK",
            headers: { "content-type": "application/json" }
          });
          post(captured, {
            id,
            type: "chunk",
            b64: btoa(unescape(encodeURIComponent(hit.body)))
          });
          post(captured, { id, type: "done" });
          served = true;
        }
      }

      // 页面 URL 只从浏览器认证过的 sender.tab 取得，用于账户分区本地缓存；
      // 翻译请求体不接受 URL，也绝不向服务器转发页面地址。
      let trPage = null;
      let trTexts = null;
      let trBackend = "google";
      let trMode = "stateless";
      if (
        url.startsWith(ORIGIN + "/pdf/api/web-translate") &&
        init &&
        init.method === "POST" &&
        typeof init.body === "string"
      ) {
        try {
          const body = JSON.parse(init.body);
          if (!body || !Array.isArray(body.texts)) {
            throw new Error("invalid web translation body");
          }
          trBackend = body.backend == null ? "google" : body.backend;
          if (trBackend !== "google" && trBackend !== "ai") {
            throw new Error("invalid web translation backend");
          }
          trTexts = body.texts;
          trPage = String(port.sender?.tab?.url || "").split("#")[0];
          if (!/^https?:\/\//.test(trPage)) trPage = null;

          // 只重建正式协议字段。即使旧 content script 或敌对页面塞入
          // url/model/session/cacheNamespace，也不允许它们离开扩展。mode 与阈值
          // 从扩展权威设置读取；页面只上报本次 discovery 得到的预计阅读句数。
          const outbound = { texts: body.texts, backend: trBackend };
          if (Object.prototype.hasOwnProperty.call(body, "glossary")) {
            outbound.glossary = body.glossary;
          }
          if (trBackend === "ai") {
            const policy = await trustedWebTranslatePolicy();
            trMode = resolvedWebTranslateMode(policy, body.estimatedUnits);
            outbound.mode = trMode;
          }
          init.body = JSON.stringify(outbound);
        } catch (_) {
          throw Object.assign(new Error("blocked: invalid web translation body"), {
            code: "BW_FETCH_BODY"
          });
        }
      }

      const token = await accountStorage.activeToken(
        captured.entry.accountContext,
        captured.lease
      );
      fenceCapturedAccount(captured);
      if (!token) {
        if (served) return;
        jsonError(captured, id, "请先在扩展设置中保存当前账户的设备令牌");
        return;
      }
      const headers = new Headers((init && init.headers) || {});
      headers.set("Authorization", `Bearer ${token}`);
      if (trTexts && trBackend === "ai" && trMode === "session") {
        // 强制覆盖，页面无法自选、复用或跨导航猜测会话身份。
        headers.set("X-BW-Translate-Document", webTranslateDocumentKey);
      } else {
        headers.delete("X-BW-Translate-Document");
      }
      const controller = new AbortController();
      aborts.set(id, controller);
      const resp = await fetch(url, {
        method: (init && init.method) || "GET",
        headers,
        body: init && init.body,
        credentials: "omit",
        cache: "no-store",
        signal: controller.signal
      });
      fenceCapturedAccount(captured);
      const ctype = resp.headers.get("content-type") || "";
      if (resp.redirected || (ctype.includes("text/html") && url.startsWith(ORIGIN + "/"))) {
        if (!served) jsonError(captured, id, "设备令牌无效，服务器返回了登录页面");
        return;
      }
      const hdrs = {};
      resp.headers.forEach((v, k) => { hdrs[k] = v; });
      if (!served) {
        post(captured, {
          id,
          type: "head",
          status: resp.status,
          statusText: resp.statusText,
          headers: hdrs
        });
      }
      const accParts = [];
      if (resp.body) {
        const reader = resp.body.getReader();
        for (;;) {
          const { done, value } = await reader.read();
          fenceCapturedAccount(captured);
          if (done) break;
          if (dqKey || trTexts) accParts.push(value.slice());
          if (!served) {
            let bin = "";
            for (let i = 0; i < value.length; i += 0x8000) bin += String.fromCharCode.apply(null, value.subarray(i, i + 0x8000));
            post(captured, { id, type: "chunk", b64: btoa(bin) });
          }
        }
      }
      if (!served) post(captured, { id, type: "done" });
      if (dqKey && resp.ok && accParts.length) {
        let length = 0;
        for (const part of accParts) length += part.length;
        const all = new Uint8Array(length);
        let offset = 0;
        for (const part of accParts) {
          all.set(part, offset);
          offset += part.length;
        }
        const text = new TextDecoder().decode(all);
        const data = JSON.parse(text);
        if (data && data.ok === true) {
          await dictionaryCachePut(captured, dqKey, text);
        }
      }
      if (trTexts && trPage && resp.ok && accParts.length) {
        try {
          let length = 0;
          for (const part of accParts) length += part.length;
          const all = new Uint8Array(length);
          let offset = 0;
          for (const part of accParts) {
            all.set(part, offset);
            offset += part.length;
          }
          const data = JSON.parse(new TextDecoder().decode(all));
          if (data && Array.isArray(data.zh)) {
            const responseMode = data.modeResolved === "session"
              ? "session"
              : "stateless";
            const responseNamespaces = (
              data.cacheNamespaces &&
              typeof data.cacheNamespaces === "object" &&
              !Array.isArray(data.cacheNamespaces)
            ) ? data.cacheNamespaces : {};
            const aiNamespace = checkedTranslationCacheNamespace(
              responseNamespaces[responseMode] ||
              data.cacheNamespace ||
              WEB_TRANSLATE_GOOGLE_NS
            );
            const googleNamespace = checkedTranslationCacheNamespace(
              data.googleCacheNamespace || WEB_TRANSLATE_GOOGLE_NS
            );
            const grouped = new Map();
            trTexts.forEach((text, index) => {
              if (
                typeof text !== "string" ||
                !text ||
                text.length > 4000 ||
                !data.zh[index]
              ) return;
              const source = Array.isArray(data.sources)
                ? data.sources[index]
                : (trBackend === "ai" ? "" : "google");
              const namespace = source === "ai"
                ? aiNamespace
                : (source === "google" ? googleNamespace : "");
              if (!namespace) return;   // 来源不明不缓存，避免把降级结果塞进 AI 桶。
              if (!grouped.has(namespace)) grouped.set(namespace, {});
              grouped.get(namespace)[text] = data.zh[index];
            });
            for (const [namespace, pairs] of grouped) {
              if (Object.keys(pairs).length) {
                await trCachePut(captured, trPage, namespace, pairs);
              }
            }
          }
        } catch (_) {}
      }
    } catch (error) {
      if (!served) {
        try {
          port.postMessage({
            id,
            type: "error",
            code: String(error?.code || "BW_FETCH_ERROR"),
            error: String(error?.message || error)
          });
        } catch (_) {}
      }
    } finally {
      aborts.delete(id);
    }
  });
  port.onDisconnect.addListener(() => {
    for (const controller of aborts.values()) {
      try { controller.abort(); } catch (_) {}
    }
    aborts.clear();
  });
});

// ── bw-ws 长连 port：共享 rc-voicecall 的 WebSocket 传输适配器 ──
// 第三方网页 CSP 会阻止 content script 直连 WSS；facade 只提供原生 WebSocket 同接口，
// 本后台只代建本服务的精确 /voice-rt 路由。侧栏、语音状态机和截图逻辑仍全部来自阅读器
// rc-voicecall.js，不在扩展另写一套。
chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== "bw-ws") return;
  if (!isTopLevelOwnContentSender(port.sender)) {
    try { port.disconnect(); } catch (_) {}
    return;
  }

  let captured = null;
  let socket = null;
  let opening = false;
  let disconnected = false;
  let terminal = false;
  let inboundQueue = Promise.resolve();
  let unsubscribeAccount = null;

  const rawPost = (message) => {
    if (disconnected) return false;
    try {
      port.postMessage(message);
      return true;
    } catch (_) {
      return false;
    }
  };
  const closeNative = (code = 1008, reason = "BW WebSocket bridge closed") => {
    const current = socket;
    socket = null;
    if (!current) return;
    try {
      current.onopen = current.onmessage =
        current.onerror = current.onclose = null;
      if (current.readyState < 2) current.close(code, reason.slice(0, 120));
    } catch (_) {}
  };
  const finish = (code, reason, wasClean) => {
    if (terminal) return;
    terminal = true;
    if (unsubscribeAccount) {
      try { unsubscribeAccount(); } catch (_) {}
      unsubscribeAccount = null;
    }
    rawPost({
      type: "close",
      code: Number(code || 0),
      reason: String(reason || ""),
      wasClean: !!wasClean
    });
  };
  const fail = (error, closeCode = 1008) => {
    const message = String(error?.message || error || "WebSocket bridge failed");
    rawPost({
      type: "error",
      code: String(error?.code || "BW_WS_ERROR"),
      error: message
    });
    closeNative(closeCode, "BW WebSocket bridge rejected");
    finish(closeCode, message.slice(0, 120), false);
  };
  const fence = () => {
    if (!captured) {
      throw Object.assign(new Error("WebSocket account is unavailable"), {
        code: "BW_ACCOUNT_CONTEXT_UNAVAILABLE"
      });
    }
    fenceCapturedAccount(captured);
  };
  const fencedPost = (message) => {
    try {
      fence();
      return rawPost(message);
    } catch (error) {
      fail(error);
      return false;
    }
  };
  const open = async (path) => {
    if (opening || socket || terminal || disconnected) {
      throw Object.assign(new Error("blocked: duplicate WebSocket open"), {
        code: "BW_WS_OPERATION"
      });
    }
    opening = true;
    try {
      const url = checkedBwWebSocketPath(path);
      captured = await capturePersistentAccountForContentSender(port.sender);
      unsubscribeAccount = captured.entry.accountContext.subscribe(() => {
        if (terminal || disconnected) return;
        try { fence(); } catch (error) { fail(error); }
      });
      const token = await accountStorage.activeToken(
        captured.entry.accountContext,
        captured.lease
      );
      fence();
      if (!token) {
        throw Object.assign(new Error(
          "请先在扩展设置中保存当前账户的设备令牌"
        ), {
          code: "BW_ACCOUNT_TOKEN_MISSING"
        });
      }
      if (disconnected || terminal) return;
      const current = new WebSocket(url);
      socket = current;
      current.binaryType = "arraybuffer";
      current.onopen = () => {
        if (socket !== current || terminal) return;
        fencedPost({ type: "open", url });
      };
      current.onmessage = (event) => {
        // 设置 binaryType 后主路径同步；保留 Blob 兜底时串行处理，避免 await 导致帧重排。
        inboundQueue = inboundQueue.then(async () => {
          if (socket !== current || terminal) return;
          try {
            fence();
            let data = event.data;
            if (typeof Blob !== "undefined" && data instanceof Blob) {
              data = await data.arrayBuffer();
              fence();
            }
            if (typeof data === "string") {
              fencedPost({ type: "message", data });
            } else {
              fencedPost(Object.assign(
                { type: "message" },
                encodedBwWebSocketFrame(data)
              ));
            }
          } catch (error) {
            fail(error, 1009);
          }
        });
      };
      current.onerror = () => {
        if (socket !== current || terminal) return;
        fencedPost({
          type: "error",
          code: "BW_WS_NETWORK",
          error: "BW WebSocket network error"
        });
      };
      current.onclose = (event) => {
        if (socket !== current || terminal) return;
        socket = null;
        try {
          fence();
          finish(event.code, event.reason, event.wasClean);
        } catch (error) {
          fail(error);
        }
      };
    } finally {
      opening = false;
    }
  };

  port.onMessage.addListener((message) => {
    if (!message || disconnected || terminal) return;
    if (message.type === "open") {
      open(message.path).catch(fail);
      return;
    }
    if (message.type === "ping") {
      try { fence(); } catch (error) { fail(error); }
      return;
    }
    if (message.type === "close") {
      try {
        fence();
        const code = message.code == null ? undefined : Number(message.code);
        const reason = String(message.reason || "");
        if (
          code != null &&
          code !== 1000 &&
          !(code >= 3000 && code <= 4999)
        ) {
          throw Object.assign(new Error("blocked: invalid WebSocket close code"), {
            code: "BW_WS_OPERATION"
          });
        }
        if (new TextEncoder().encode(reason).byteLength > 123) {
          throw Object.assign(new Error("blocked: WebSocket close reason too long"), {
            code: "BW_WS_OPERATION"
          });
        }
        if (socket && socket.readyState < 2) {
          if (code == null) socket.close();
          else socket.close(code, reason);
        } else {
          finish(code || 1000, reason, true);
        }
      } catch (error) {
        fail(error);
      }
      return;
    }
    if (message.type !== "send") {
      fail(Object.assign(new Error("blocked: invalid WebSocket operation"), {
        code: "BW_WS_OPERATION"
      }));
      return;
    }
    try {
      fence();
      if (!socket || socket.readyState !== 1) {
        throw Object.assign(new Error("WebSocket is not open"), {
          code: "BW_WS_STATE"
        });
      }
      if (message.binary === true) {
        socket.send(decodedBwWebSocketFrame(message));
      } else {
        if (typeof message.data !== "string") {
          throw Object.assign(new Error("blocked: invalid WebSocket text frame"), {
            code: "BW_WS_FRAME"
          });
        }
        if (new TextEncoder().encode(message.data).byteLength > MAX_BW_WS_FRAME_BYTES) {
          throw Object.assign(new Error("blocked: WebSocket frame exceeds 8 MiB"), {
            code: "BW_WS_FRAME"
          });
        }
        socket.send(message.data);
      }
    } catch (error) {
      fail(error, error?.code === "BW_WS_FRAME" ? 1009 : 1008);
    }
  });
  port.onDisconnect.addListener(() => {
    disconnected = true;
    if (unsubscribeAccount) {
      try { unsubscribeAccount(); } catch (_) {}
      unsubscribeAccount = null;
    }
    closeNative(1000, "content disconnected");
  });
});

// ── Windows 电脑语音固定直连 ──
// 普通网页的 content script 继承页面网络语境，不能自行承担这条跨源 WSS。
// relay 只接受扩展自身的顶层 content script，并把 endpoint 固定在后台；网页不能
// 选择 URL、直接取得 runtime Port，或借字段扩展恢复配对/任意网络能力。
const computerVoiceDirectTabs = new Map();

function computerVoiceDirectSenderTabId(sender) {
  if (
    !sender ||
    sender.id !== chrome.runtime.id ||
    !Number.isInteger(sender?.tab?.id) ||
    sender.frameId !== 0 ||
    typeof sender.url !== "string"
  ) {
    return null;
  }
  let url;
  try { url = new URL(sender.url); }
  catch (_) { url = null; }
  if (
    !url ||
    !["http:", "https:"].includes(url.protocol) ||
    url.username ||
    url.password ||
    url.href !== sender.url
  ) {
    return null;
  }
  return sender.tab.id;
}

function computerVoiceDirectExactMessage(message, fields) {
  return !!(
    message &&
    typeof message === "object" &&
    !Array.isArray(message) &&
    Object.keys(message).length === fields.length &&
    fields.every((field) =>
      Object.prototype.hasOwnProperty.call(message, field)
    )
  );
}

function computerVoiceDirectFrameBytes(value) {
  if (Object.prototype.toString.call(value) === "[object ArrayBuffer]") {
    return new Uint8Array(value);
  }
  if (ArrayBuffer.isView(value)) {
    return new Uint8Array(
      value.buffer,
      value.byteOffset,
      value.byteLength
    );
  }
  throw Object.assign(new Error("Windows 返回了不支持的二进制帧"), {
    code: "BW_COMPUTER_VOICE_DIRECT_FRAME"
  });
}

function computerVoiceDirectBase64(bytes) {
  if (bytes.byteLength !== COMPUTER_VOICE_DIRECT_PCM_FRAME_BYTES) {
    throw Object.assign(new Error("Windows PCM 帧长度必须是 1956 bytes"), {
      code: "BW_COMPUTER_VOICE_DIRECT_FRAME"
    });
  }
  let binary = "";
  for (let offset = 0; offset < bytes.byteLength; offset += 1) {
    binary += String.fromCharCode(bytes[offset]);
  }
  return btoa(binary);
}

function computerVoiceDirectDecodeUplink(message) {
  if (
    !computerVoiceDirectExactMessage(
      message,
      ["type", "data", "bytes", "sequence"]
    ) ||
    message.type !== "send-binary-base64" ||
    message.bytes !== COMPUTER_VOICE_DIRECT_PCM_FRAME_BYTES ||
    !Number.isSafeInteger(message.sequence) ||
    message.sequence < 0 ||
    message.sequence > 0xffffffff ||
    typeof message.data !== "string" ||
    message.data.length !== 4 * Math.ceil(message.bytes / 3) ||
    !/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/
      .test(message.data)
  ) {
    throw Object.assign(new Error("Reader 麦克风 PCM 编码无效"), {
      code: "BW_COMPUTER_VOICE_DIRECT_FRAME"
    });
  }
  let raw;
  try { raw = atob(message.data); }
  catch (_) { raw = null; }
  if (!raw || raw.length !== message.bytes) {
    throw Object.assign(new Error("Reader 麦克风 PCM 长度不匹配"), {
      code: "BW_COMPUTER_VOICE_DIRECT_FRAME"
    });
  }
  const bytes = new Uint8Array(raw.length);
  for (let index = 0; index < raw.length; index += 1) {
    bytes[index] = raw.charCodeAt(index);
  }
  if (
    bytes[0] !== 0x42 ||
    bytes[1] !== 0x57 ||
    bytes[2] !== 0x43 ||
    bytes[3] !== 0x56 ||
    bytes[4] !== 1 ||
    bytes[5] !== 3 ||
    bytes[6] !== 0 ||
    bytes[7] !== 0 ||
    new DataView(
      bytes.buffer,
      bytes.byteOffset,
      bytes.byteLength
    ).getUint32(24, true) !== message.sequence
  ) {
    throw Object.assign(new Error("Reader 麦克风 PCM 方向或版本无效"), {
      code: "BW_COMPUTER_VOICE_DIRECT_FRAME"
    });
  }
  return {
    buffer: bytes.buffer,
    sequence: message.sequence
  };
}

chrome.runtime.onConnect.addListener((port) => {
  if (port.name !== COMPUTER_VOICE_DIRECT_PORT) return;
  const tabId = computerVoiceDirectSenderTabId(port.sender);
  if (tabId == null) {
    try { port.disconnect(); } catch (_) {}
    return;
  }
  if (computerVoiceDirectTabs.has(tabId)) {
    try {
      port.postMessage({
        type: "error",
        code: "BW_COMPUTER_VOICE_DIRECT_TAB",
        error: "当前标签页已有 Windows 语音直连"
      });
    } catch (_) {}
    try { port.disconnect(); } catch (_) {}
    return;
  }

  const entry = {
    port,
    tabId,
    socket: null,
    terminal: false,
    inboundQueue: Promise.resolve()
  };
  computerVoiceDirectTabs.set(tabId, entry);

  const post = (message) => {
    if (entry.terminal) return false;
    try {
      port.postMessage(message);
      return true;
    } catch (_) {
      return false;
    }
  };
  const releaseTab = () => {
    if (computerVoiceDirectTabs.get(tabId) === entry) {
      computerVoiceDirectTabs.delete(tabId);
    }
  };
  const detachSocket = (code, reason) => {
    const current = entry.socket;
    entry.socket = null;
    if (!current) return;
    current.onopen = current.onmessage =
      current.onerror = current.onclose = null;
    if (current.readyState < 2) {
      try { current.close(code, String(reason || "").slice(0, 120)); }
      catch (_) {}
    }
  };
  const finishClose = (code, reason, wasClean, nativeCode = null) => {
    if (entry.terminal) return;
    post({
      type: "close",
      code: Number(code || 0),
      reason: String(reason || "").slice(0, 123),
      wasClean: !!wasClean
    });
    entry.terminal = true;
    detachSocket(nativeCode, reason);
    releaseTab();
  };
  const fail = (error, nativeCode = 4000) => {
    if (entry.terminal) return;
    const code = String(
      error?.code || "BW_COMPUTER_VOICE_DIRECT_RELAY"
    );
    const message = String(
      error?.message || error || "Windows 语音直连失败"
    ).slice(0, 300);
    post({ type: "error", code, error: message });
    post({
      type: "close",
      code: nativeCode,
      reason: message.slice(0, 120),
      wasClean: false
    });
    entry.terminal = true;
    detachSocket(nativeCode, "computer-voice-relay-error");
    releaseTab();
  };
  const open = () => {
    if (entry.socket || entry.terminal) {
      throw Object.assign(new Error("Windows 语音直连已经打开"), {
        code: "BW_COMPUTER_VOICE_DIRECT_STATE"
      });
    }
    // @interaction computer-voice.bridge.request
    const socket = new WebSocket(COMPUTER_VOICE_DIRECT_ENDPOINT);
    entry.socket = socket;
    socket.binaryType = "arraybuffer";
    socket.onopen = () => {
      if (entry.socket !== socket || entry.terminal) return;
      post({ type: "open" });
    };
    socket.onmessage = (event) => {
      entry.inboundQueue = entry.inboundQueue.then(async () => {
        if (entry.socket !== socket || entry.terminal) return;
        let data = event.data;
        if (typeof Blob !== "undefined" && data instanceof Blob) {
          data = await data.arrayBuffer();
          if (entry.socket !== socket || entry.terminal) return;
        }
        if (typeof data === "string") {
          if (
            new TextEncoder().encode(data).byteLength >
              MAX_COMPUTER_VOICE_DIRECT_TEXT_BYTES
          ) {
            throw Object.assign(new Error("Windows 文本帧超过 64 KiB"), {
              code: "BW_COMPUTER_VOICE_DIRECT_CAPACITY"
            });
          }
          post({ type: "text", data });
          return;
        }
        const bytes = computerVoiceDirectFrameBytes(data);
        post({
          type: "binary-base64",
          data: computerVoiceDirectBase64(bytes),
          bytes: bytes.byteLength
        });
      }).catch((error) => fail(error, 4002));
    };
    socket.onerror = () => {
      if (entry.socket !== socket || entry.terminal) return;
      fail(Object.assign(new Error("Windows 语音 WSS 网络错误"), {
        code: "BW_COMPUTER_VOICE_DIRECT_NETWORK"
      }), 4001);
    };
    socket.onclose = (event) => {
      if (entry.socket !== socket || entry.terminal) return;
      entry.socket = null;
      finishClose(event.code, event.reason, event.wasClean);
    };
  };

  port.onMessage.addListener((message) => {
    if (entry.terminal) return;
    try {
      if (
        computerVoiceDirectExactMessage(message, ["type"]) &&
        message.type === "open"
      ) {
        open();
        return;
      }
      if (
        computerVoiceDirectExactMessage(message, ["type", "data"]) &&
        message.type === "send-text"
      ) {
        if (typeof message.data !== "string") {
          throw Object.assign(new Error("Windows 语音文本帧格式无效"), {
            code: "BW_COMPUTER_VOICE_DIRECT_FRAME"
          });
        }
        if (
          new TextEncoder().encode(message.data).byteLength >
            MAX_COMPUTER_VOICE_DIRECT_TEXT_BYTES
        ) {
          throw Object.assign(new Error("Windows 语音文本帧超过 64 KiB"), {
            code: "BW_COMPUTER_VOICE_DIRECT_CAPACITY"
          });
        }
        if (!entry.socket || entry.socket.readyState !== 1) {
          throw Object.assign(new Error("Windows 语音 WSS 尚未打开"), {
            code: "BW_COMPUTER_VOICE_DIRECT_STATE"
          });
        }
        entry.socket.send(message.data);
        return;
      }
      if (message?.type === "send-binary-base64") {
        if (!entry.socket || entry.socket.readyState !== 1) {
          throw Object.assign(new Error("Windows 语音 WSS 尚未打开"), {
            code: "BW_COMPUTER_VOICE_DIRECT_STATE"
          });
        }
        if (
          entry.socket.bufferedAmount +
            COMPUTER_VOICE_DIRECT_PCM_FRAME_BYTES >
            COMPUTER_VOICE_DIRECT_UPLINK_BUFFER_LIMIT_BYTES
        ) {
          throw Object.assign(
            new Error("Reader 麦克风上行已落后，拒绝发送过期语音"),
            { code: "BW_COMPUTER_VOICE_DIRECT_UPLINK_BACKPRESSURE" }
          );
        }
        const uplink = computerVoiceDirectDecodeUplink(message);
        entry.socket.send(uplink.buffer);
        post({
          type: "binary-accepted",
          sequence: uplink.sequence,
          bytes: COMPUTER_VOICE_DIRECT_PCM_FRAME_BYTES
        });
        return;
      }
      if (
        computerVoiceDirectExactMessage(message, ["type"]) &&
        message.type === "close"
      ) {
        finishClose(1000, "", true, 1000);
        return;
      }
      throw Object.assign(new Error("Windows 语音 relay 操作或字段无效"), {
        code: "BW_COMPUTER_VOICE_DIRECT_OPERATION"
      });
    } catch (error) {
      fail(error);
    }
  });
  port.onDisconnect.addListener(() => {
    if (entry.terminal) return;
    entry.terminal = true;
    detachSocket(1000, "content-disconnected");
    releaseTab();
  });
});
