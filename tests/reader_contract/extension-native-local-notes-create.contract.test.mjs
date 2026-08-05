import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const FACADE = readFileSync(
  new URL("../../extensions/bw-reader-webext/src/facade.js", import.meta.url),
  "utf8",
);
const BACKGROUND = readFileSync(
  new URL("../../extensions/bw-reader-webext/background.js", import.meta.url),
  "utf8",
);

function slice(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  assert.ok(start >= 0 && end > start, `${startMarker} source is present`);
  return source.slice(start, end);
}

function loadFacadeBridge() {
  const messages = [];
  const source = slice(
    FACADE,
    "  const nativeAppDataBridge = (() => {",
    "\n  window.__bwNativeAppDataBridge = nativeAppDataBridge;",
  );
  const sandbox = {
    Error,
    Object,
    Promise,
    Set,
    String,
    TextEncoder,
    Uint8Array,
    crypto,
    chrome: {
      runtime: {
        lastError: null,
        sendMessage(message, callback) {
          messages.push(structuredClone(message));
          callback({
            ok: true,
            data: {
              contract: "bw-reader-native/1",
              action: message.action,
              requestId: message.requestId,
              ok: true,
              handled: false,
              disposition: "pi",
            },
          });
        },
      },
    },
  };
  vm.runInNewContext(
    `${source}\nglobalThis.bridge = nativeAppDataBridge;`,
    sandbox,
    { filename: "facade-native-app-data-bridge.js" },
  );
  return { bridge: sandbox.bridge, messages };
}

function loadFetchInterceptor() {
  const source = slice(
    FACADE,
    "  function createNativeLocalNotesFetchInterceptor(environment) {",
    "\n  const nativeLocalNotesFetchInterceptor =",
  );
  const sandbox = { Error, JSON, Object, String, TypeError };
  vm.runInNewContext(
    `${source}\nglobalThis.factory = createNativeLocalNotesFetchInterceptor;`,
    sandbox,
    { filename: "facade-native-local-note-fetch.js" },
  );
  return sandbox.factory;
}

function loadBackgroundRequestParser() {
  const source = slice(
    BACKGROUND,
    "function nativeAppRequestPayload(message, sender) {",
    "\nfunction sendSafariNativeMessage(payload) {",
  );
  const sandbox = {
    URL,
    Number,
    Object,
    Set,
    String,
    TextEncoder,
    NATIVE_APP_ACTIONS: new Set(["notes.create"]),
    NATIVE_APP_REQUEST_ID_RE: /^[A-Za-z0-9_-]{8,96}$/,
    NATIVE_APP_CONTRACT: "bw-reader-native/1",
    NATIVE_APP_MAX_NOTE_NAME_BYTES: 512,
    NATIVE_APP_MAX_NOTE_TEXT_BYTES: 262144,
    NATIVE_APP_MAX_NOTE_SOURCE_BYTES: 8192,
    NATIVE_APP_MAX_NOTE_PAGE: 10000000,
    TRUSTED_PWA_ORIGINS: new Set(["https://bwicarus.taile44d0c.ts.net"]),
    TRUSTED_PWA_PATHS: new Set(["/pdf/view", "/pdf/epub/view", "/pdf/html/view"]),
    nativeAppExactKeys(value, required, optional = []) {
      if (!value || typeof value !== "object" || Array.isArray(value)) return false;
      const keys = Object.keys(value);
      const allowed = new Set([...required, ...optional]);
      return required.every((key) => keys.includes(key)) &&
        keys.every((key) => allowed.has(key));
    },
    nativeAppPublicError(message, code) {
      return Object.assign(new Error(message), { code });
    },
    nativeAppByteLength(value) {
      return new TextEncoder().encode(String(value || "")).byteLength;
    },
    senderUrl(sender) {
      return new URL(sender?.tab?.url || sender?.url || "");
    },
    canonicalOrdinaryDocumentUrl(sender) {
      const url = new URL(sender.tab.url);
      url.hash = "";
      return url.href;
    },
  };
  vm.runInNewContext(
    `${source}\nglobalThis.parse = nativeAppRequestPayload;`,
    sandbox,
    { filename: "background-native-note-request.js" },
  );
  return sandbox.parse;
}

function loadBackgroundResponseNormalizer() {
  const source = slice(
    BACKGROUND,
    "function normalizeNativeNotesStorage(raw) {",
    "\nasync function handleNativeAppMessage(message, sender) {",
  );
  const sandbox = {
    Error,
    Number,
    Object,
    Set,
    String,
    TextEncoder,
    NATIVE_APP_CONTRACT: "bw-reader-native/1",
    NATIVE_APP_REQUEST_ID_RE: /^[A-Za-z0-9_-]{8,96}$/,
    NATIVE_APP_KINDS: new Set(["codex-desktop", "chatgpt-classic"]),
    nativeAppExactKeys(value, required, optional = []) {
      if (!value || typeof value !== "object" || Array.isArray(value)) return false;
      const keys = Object.keys(value);
      const allowed = new Set([...required, ...optional]);
      return required.every((key) => keys.includes(key)) &&
        keys.every((key) => allowed.has(key));
    },
    nativeAppPublicError(message, code) {
      return Object.assign(new Error(message), { code });
    },
    nativeAppByteLength(value) {
      return new TextEncoder().encode(String(value || "")).byteLength;
    },
  };
  vm.runInNewContext(
    `${source}\nglobalThis.normalize = normalizeNativeAppResponse;`,
    sandbox,
    { filename: "background-native-note-response.js" },
  );
  return sandbox.normalize;
}

const request = (overrides = {}) => ({
  type: "BW_NATIVE_APP_REQUEST",
  action: "notes.create",
  requestId: "request_12345678",
  name: "摘录",
  text: "网页选中的正文",
  file: "web:https://forged.invalid/",
  page: 7,
  ...overrides,
});

const note = (overrides = {}) => ({
  id: "request_12345678",
  title: "摘录",
  fileName: "摘录.md",
  preview: "网页选中的正文",
  contentTruncated: false,
  sourceFile: "web:https://example.com/article",
  sourcePage: 0,
  createdAt: 1,
  pendingExport: true,
  content: "# 摘录\n\n网页选中的正文\n",
  ...overrides,
});
const clean = (value) => JSON.parse(JSON.stringify(value));

test("facade notes.create validates input before native messaging", async () => {
  const { bridge, messages } = loadFacadeBridge();
  await assert.rejects(
    bridge.createNote({ name: "n", text: "t", unexpected: true }),
    (error) => error.code === "BW_NATIVE_NOTE_CREATE_INVALID",
  );
  assert.equal(messages.length, 0);

  const result = await bridge.createNote({
    name: " 摘录 ",
    text: " 正文 ",
    file: "web:https://example.com/",
    page: 0,
  });
  assert.equal(result.handled, false);
  assert.equal(messages.length, 1);
  assert.deepEqual(
    Object.keys(messages[0]).sort(),
    ["action", "file", "name", "page", "requestId", "text", "type"].sort(),
  );
  assert.equal(messages[0].action, "notes.create");
  assert.equal(messages[0].name, "摘录");
  assert.equal(messages[0].text, "正文");
});

test("background binds ordinary notes to authenticated tab URL and preserves book identity", () => {
  const parse = loadBackgroundRequestParser();
  const ordinary = parse(request(), {
    tab: { url: "https://example.com/article?chapter=1#selection" },
    url: "https://example.com/article?chapter=1#selection",
    frameId: 0,
  });
  assert.equal(ordinary.file, "web:https://example.com/article?chapter=1");
  assert.equal(ordinary.page, 0);

  const book = parse(request({ file: "资源/books/book.pdf", page: 12 }), {
    tab: { url: "https://bwicarus.taile44d0c.ts.net/pdf/view?file=book" },
    url: "https://bwicarus.taile44d0c.ts.net/pdf/view?file=book",
    frameId: 0,
  });
  assert.equal(book.file, "资源/books/book.pdf");
  assert.equal(book.page, 12);

  assert.throws(
    () => parse(request({ extra: true }), {
      tab: { url: "https://example.com/" },
      url: "https://example.com/",
      frameId: 0,
    }),
  );
});

test("Safari /to-note uses local result, while disabled and Chromium continue Pi", async () => {
  const factory = loadFetchInterceptor();
  const target = "https://bwicarus.taile44d0c.ts.net/pdf/api/to-note";
  const init = { method: "POST", body: JSON.stringify({ name: "摘录", text: "正文" }) };
  let calls = 0;
  const chromium = factory({
    origin: "https://bwicarus.taile44d0c.ts.net",
    runtime: { getURL: () => "chrome-extension://unit/" },
    bridge: { async createNote() { calls += 1; } },
    URL,
    Response,
  });
  assert.equal(await chromium(target, init), null);
  assert.equal(calls, 0);

  const disabled = factory({
    origin: "https://bwicarus.taile44d0c.ts.net",
    runtime: { getURL: () => "safari-web-extension://unit/" },
    bridge: { async createNote() { return { handled: false, disposition: "pi" }; } },
    URL,
    Response,
  });
  assert.equal(await disabled(target, init), null);

  const queued = factory({
    origin: "https://bwicarus.taile44d0c.ts.net",
    runtime: { getURL: () => "safari-web-extension://unit/" },
    bridge: {
      async createNote() {
        return {
          handled: true,
          disposition: "queued",
          plannedFileName: "摘录.md",
          obsidianURL: "",
        };
      },
    },
    URL,
    Response,
  });
  const response = await queued(target, init);
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), {
    ok: true,
    note_path: "摘录.md",
    planned_note_path: "摘录.md",
    obsidian_url: "",
    local_disposition: "queued",
    pending_export: true,
  });
});

test("native create errors never silently fall back to Pi", async () => {
  const factory = loadFetchInterceptor();
  const expected = Object.assign(new Error("queue full"), { code: "QUEUE_FULL" });
  const intercept = factory({
    origin: "https://bwicarus.taile44d0c.ts.net",
    runtime: { getURL: () => "safari-web-extension://unit/" },
    bridge: { async createNote() { throw expected; } },
    URL,
    Response,
  });
  await assert.rejects(
    intercept("https://bwicarus.taile44d0c.ts.net/pdf/api/to-note", {
      method: "POST",
      body: JSON.stringify({ name: "摘录", text: "正文" }),
    }),
    (error) => error === expected,
  );
});

test("background accepts pending fields and strictly validates create outcomes", () => {
  const normalize = loadBackgroundResponseNormalizer();
  const base = {
    contract: "bw-reader-native/1",
    action: "notes.create",
    requestId: "request_12345678",
    ok: true,
  };
  assert.deepEqual(
    clean(normalize({ ...base, handled: false, disposition: "pi" }, {
      action: "notes.create",
      requestId: "request_12345678",
    })),
    { ...base, handled: false, disposition: "pi" },
  );

  const queued = normalize({
    ...base,
    handled: true,
    disposition: "queued",
    plannedFileName: "摘录.md",
    obsidianURL: "",
    note: note(),
  }, { action: "notes.create", requestId: "request_12345678" });
  assert.equal(queued.note.pendingExport, true);

  assert.throws(() => normalize({
    ...base,
    handled: true,
    disposition: "queued",
    plannedFileName: "摘录.md",
    obsidianURL: "",
    note: note({ pendingExport: false }),
  }, { action: "notes.create", requestId: "request_12345678" }));

  const status = normalize({
    contract: "bw-reader-native/1",
    action: "notes.status",
    requestId: "request_12345678",
    ok: true,
    storage: {
      enabled: true,
      configured: true,
      folderName: "Vault",
      updatedAt: 1,
      count: 2,
      pendingCount: 1,
    },
  }, { action: "notes.status", requestId: "request_12345678" });
  assert.equal(status.storage.pendingCount, 1);
});
