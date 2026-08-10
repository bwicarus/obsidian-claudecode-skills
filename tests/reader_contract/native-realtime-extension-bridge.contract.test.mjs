import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const ROOT = new URL("../../", import.meta.url);
const BACKGROUND = readFileSync(
  new URL("extensions/bw-reader-webext/background.js", ROOT),
  "utf8",
);
const FACADE = readFileSync(
  new URL("extensions/bw-reader-webext/src/facade.js", ROOT),
  "utf8",
);

function slice(source, startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  assert.ok(start >= 0 && end > start, `${startMarker} source is present`);
  return source.slice(start, end);
}

function exactKeys(value, required, optional = []) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const keys = Object.keys(value);
  const allowed = new Set([...required, ...optional]);
  return required.every((key) => keys.includes(key)) &&
    keys.every((key) => allowed.has(key));
}

function publicError(message, code) {
  return Object.assign(new Error(message), { code });
}

function loadParser() {
  const source = slice(
    BACKGROUND,
    "function nativeAppRequestPayload(message, sender) {",
    "\nfunction sendSafariNativeMessage(payload) {",
  );
  const sandbox = {
    Error,
    Number,
    Object,
    Set,
    String,
    TextEncoder,
    NATIVE_APP_ACTIONS: new Set([
      "realtime.status", "realtime.mint", "realtime.image",
      "realtime.hangup",
    ]),
    NATIVE_APP_REQUEST_ID_RE: /^[A-Za-z0-9_-]{8,96}$/,
    NATIVE_APP_CONTRACT: "bw-reader-native/1",
    NATIVE_APP_MAX_REALTIME_FILE_BYTES: 8192,
    NATIVE_APP_MAX_REALTIME_IMAGE_BYTES: 2800000,
    NATIVE_APP_MAX_NOTE_PAGE: 10000000,
    NATIVE_APP_REALTIME_CALL_ID_RE: /^rtc_[A-Za-z0-9_-]{8,156}$/,
    NATIVE_APP_REALTIME_SECRET_RE: /^ek_[A-Za-z0-9_-]{8,4093}$/,
    NATIVE_APP_REALTIME_TOOLS: new Set([
      "see_ink", "see_page", "see_figure",
    ]),
    NATIVE_APP_REALTIME_MEDIA_TYPES: new Set([
      "image/jpeg", "image/png", "image/webp",
    ]),
    nativeAppExactKeys: exactKeys,
    nativeAppPublicError: publicError,
    nativeAppByteLength(value) {
      return new TextEncoder().encode(String(value || "")).byteLength;
    },
  };
  vm.runInNewContext(
    `${source}\nglobalThis.parse = nativeAppRequestPayload;`,
    sandbox,
    { filename: "background-native-realtime-request.js" },
  );
  return sandbox.parse;
}

function loadNormalizer() {
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
    NATIVE_APP_KINDS: new Set(["codex-desktop", "chatgpt-classic"]),
    NATIVE_APP_REQUEST_ID_RE: /^[A-Za-z0-9_-]{8,96}$/,
    NATIVE_APP_REALTIME_SECRET_RE: /^ek_[A-Za-z0-9_-]{8,4093}$/,
    nativeAppExactKeys: exactKeys,
    nativeAppPublicError: publicError,
    nativeAppByteLength(value) {
      return new TextEncoder().encode(String(value || "")).byteLength;
    },
  };
  vm.runInNewContext(
    `${source}\nglobalThis.normalize = normalizeNativeAppResponse;`,
    sandbox,
    { filename: "background-native-realtime-response.js" },
  );
  return sandbox.normalize;
}

function loadFacadeBridge(protocol = "safari-web-extension:") {
  const messages = [];
  const source = slice(
    FACADE,
    "  const nativeRealtimeBridge = (() => {",
    "\n  const nativeAppDataBridge = (() => {",
  );
  const sandbox = {
    Error,
    Number,
    Object,
    Promise,
    Set,
    String,
    URL,
    Uint8Array,
    crypto,
    window: {},
    chrome: {
      runtime: {
        lastError: null,
        getURL: () => `${protocol}//unit/`,
        sendMessage(message, callback) {
          messages.push(structuredClone(message));
          callback({
            ok: true,
            data: {
              contract: "bw-reader-native/1",
              action: message.action,
              requestId: message.requestId,
              ok: true,
              clientSecret: "ek_123456789",
              expiresAt: 123,
              model: "gpt-realtime-2.1-mini",
              rtImage: true,
              compactTokens: 24000,
            },
          });
        },
      },
    },
  };
  vm.runInNewContext(
    `${source}\nglobalThis.bridge = nativeRealtimeBridge;`,
    sandbox,
    { filename: "facade-native-realtime-bridge.js" },
  );
  return { bridge: sandbox.bridge, messages, window: sandbox.window };
}

test("background accepts only bounded ephemeral Realtime native requests", () => {
  const parse = loadParser();
  const base = {
    type: "BW_NATIVE_APP_REQUEST",
    requestId: "request_12345678",
  };
  assert.deepEqual(
    structuredClone(parse({
      ...base,
      action: "realtime.mint",
      file: "web:https://example.com/article",
      page: 0,
    }, {})),
    {
      contract: "bw-reader-native/1",
      action: "realtime.mint",
      requestId: "request_12345678",
      file: "web:https://example.com/article",
      page: 0,
    },
  );
  assert.throws(() => parse({
    ...base,
    action: "realtime.mint",
    file: "",
    page: 0,
    apiKey: "sk-must-never-cross-js",
  }, {}));
  assert.throws(() => parse({
    ...base,
    action: "realtime.image",
    callId: "rtc_123456789",
    clientSecret: "sk-not-ephemeral",
    tool: "see_ink",
    mediaType: "image/jpeg",
    b64: "A".repeat(3000),
  }, {}));
});

test("background returns only normalized short-lived credentials", () => {
  const normalize = loadNormalizer();
  const payload = {
    action: "realtime.mint",
    requestId: "request_12345678",
  };
  const response = {
    contract: "bw-reader-native/1",
    action: "realtime.mint",
    requestId: "request_12345678",
    ok: true,
    clientSecret: "ek_123456789",
    expiresAt: 123,
    model: "gpt-realtime-2.1-mini",
    rtImage: true,
    compactTokens: 24000,
  };
  const result = structuredClone(normalize(response, payload));
  assert.equal(result.clientSecret, "ek_123456789");
  assert.equal("apiKey" in result, false);
  assert.throws(() => normalize({
    ...response,
    apiKey: "sk-must-never-cross-js",
  }, payload));
});

test("Safari facade maps the shared voice API without exposing the project key", async () => {
  const { bridge, messages, window } = loadFacadeBridge();
  const result = await bridge.request({
    action: "mint",
    file: "web:https://example.com/article",
    page: 4,
  });
  assert.equal(result.client_secret, "ek_123456789");
  assert.equal(messages[0].action, "realtime.mint");
  assert.equal(messages[0].file, "web:https://example.com/article");
  assert.equal("apiKey" in messages[0], false);
  assert.equal(window.__BW_NATIVE_OPENAI_REALTIME__, true);

  const chromeBridge = loadFacadeBridge("chrome-extension:");
  assert.equal(chromeBridge.window.__bwNativeRealtime, undefined);
  await assert.rejects(chromeBridge.bridge.request({ action: "mint" }));
});
