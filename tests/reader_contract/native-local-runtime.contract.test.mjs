import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { webcrypto } from "node:crypto";

const ROOT = new URL("../../", import.meta.url);
const SOURCE = readFileSync(
  new URL("_server_deploy/static/pdf/native-local-runtime.js", ROOT),
  "utf8",
);
const RC_MD = readFileSync(
  new URL("_server_deploy/static/pdf/rc-md.js", ROOT),
  "utf8",
);
const RC_CORE = readFileSync(
  new URL("_server_deploy/static/pdf/rc-core.js", ROOT),
  "utf8",
);
const ACCOUNT_CONTEXT = readFileSync(
  new URL("_server_deploy/static/reader-runtime/account-context.js", ROOT),
  "utf8",
);
const PDF_AI = readFileSync(
  new URL("_server_deploy/static/pdf/reader.src/21-misc-ai.js", ROOT),
  "utf8",
);
const NATIVE_INTERFACE_MANIFEST = JSON.parse(readFileSync(
  new URL("ios/BWReader/native_reader_interface_manifest.json", ROOT),
  "utf8",
));
const DEFAULT_LOCAL_BOOK_ID = "localbook-" + "b".repeat(64);
const DEFAULT_LOCAL_FILE = "localbook:" + DEFAULT_LOCAL_BOOK_ID;

function clone(value) {
  return value == null ? value : JSON.parse(JSON.stringify(value));
}

function fakeClock(start = 1_800_000_000_000) {
  let now = start;
  class FakeDate extends Date {
    constructor(...args) { super(...(args.length ? args : [now])); }
    static now() { return now; }
  }
  return {
    Date: FakeDate,
    advance(milliseconds) { now += milliseconds; },
  };
}

async function enableNativeContext(harnessResult) {
  const response = await harnessResult.context.fetch("/pdf/api/context-sync", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: true, deliveryMode: "snapshot-mcp" }),
  });
  assert.equal(response.status, 200);
}

function withNativeOCRMutationRoutesSupported() {
  const manifest = clone(NATIVE_INTERFACE_MANIFEST);
  const paths = new Set([
    "/pdf/api/ocr-selection",
    "/pdf/api/reocr-page",
    "/pdf/api/reocr-page/clear",
  ]);
  for (const route of manifest.routes) {
    if (paths.has(route.path)) {
      route.status = "supported";
      route.owner = "local";
      route.remoteBook = null;
    }
  }
  return manifest;
}

function withNativePDFMutationRoutesSupported() {
  const manifest = clone(NATIVE_INTERFACE_MANIFEST);
  const route = manifest.routes.find(
    (item) => item.path === "/pdf/api/pdf-insert-page",
  );
  route.status = "supported";
  route.owner = "local";
  route.remoteBook = null;
  return manifest;
}

function withGenericAssistantRoutesSupported() {
  const manifest = clone(NATIVE_INTERFACE_MANIFEST);
  for (const route of manifest.routes) {
    if (route.path === "/api/assistant/chat" ||
        route.path === "/api/assistant/voice-tool") {
      route.status = "supported";
    }
  }
  return manifest;
}

function withNativePDFAssistantAndMutationRoutesSupported() {
  const manifest = withGenericAssistantRoutesSupported();
  const route = manifest.routes.find(
    (item) => item.path === "/pdf/api/pdf-insert-page",
  );
  route.status = "supported";
  route.owner = "local";
  route.remoteBook = null;
  return manifest;
}

function withNativeLocalContentRoutesSupported() {
  const manifest = clone(NATIVE_INTERFACE_MANIFEST);
  const localPaths = new Set([
    "/api/assistant/voice-page-text",
    "/pdf/api/book-crop",
    "/pdf/api/epub-search",
    "/pdf/api/page-translate",
  ]);
  for (const route of manifest.routes) {
    if (localPaths.has(route.path)) {
      route.status = "supported";
      route.owner = "local";
      route.remoteBook = null;
    }
    if (route.path === "/pdf/api/epub-translate-section") {
      route.status = "supported";
      route.owner = "pi";
      route.surfaces = [...new Set([...(route.surfaces || []), "pdf"])];
    }
  }
  return manifest;
}

function nativePageReply(message, { text = "校正", authority = "local-override" } = {}) {
  return {
    contract: "reader-native-page-text-response/1",
    action: message.action,
    requestId: message.requestId,
    ok: true,
    state: "ready",
    source: "apple",
    revision: "native-page-revision",
    error: null,
    page: message.page,
    pageWidth: 600,
    pageHeight: 800,
    chars: [{ c: text, x0: 10, y0: 20, x1: 70, y1: 40, sp: 0, w: 1, bk: 1 }],
    furigana: [],
    wordSegmentation: "ready",
    characterGeometry: "exact",
    formulaCoverage: "unavailable",
    formulaRegions: [],
    textAuthority: authority,
  };
}

function nativePDFMutationResponder(options = {}) {
  const durable = options.durableState || {
    pageCount: options.pageCount || 4,
    contentSHA256: "a".repeat(64),
    journal: null,
  };
  let lastPrepare = null;
  const ticket = "npmt_" + "d".repeat(32);
  return (message) => {
    const common = {
      contract: "reader-native-pdf-mutation-response/1",
      requestId: message.requestId,
      ok: true,
      localBookId: message.localBookId,
      ticket,
    };
    if (message.action === "recover") {
      if (options.hangCommittedRecover &&
          durable.journal?.phase === "committed") {
        return new Promise(() => {});
      }
      let outcome = "none";
      if (durable.journal) {
        if (durable.journal.phase === "committed") {
          outcome = "committed";
          durable.contentSHA256 = durable.journal.stagedContentSHA256;
        } else {
          outcome = "rolled-back";
          durable.contentSHA256 = durable.journal.oldContentSHA256;
          durable.pageCount = durable.journal.oldPageCount;
        }
        durable.journal = null;
      } else if (message.stagedContentSHA256 === durable.contentSHA256) {
        outcome = "committed";
      } else if (message.oldContentSHA256 === durable.contentSHA256) {
        outcome = "rolled-back";
      }
      return {
        ...common,
        action: "recovered",
        ticket: message.ticket,
        outcome,
        contentSHA256: durable.contentSHA256,
        mtime: 1_800_000_000,
        byteCount: 123_456,
      };
    }
    if (message.action === "prepare") {
      lastPrepare = message;
      const next = durable.pageCount + (message.operation === "insert" ? 1 : 0)
        - (message.operation === "delete" ? 1 : 0);
      durable.journal = {
        phase: "staged",
        oldContentSHA256: durable.contentSHA256,
        stagedContentSHA256: "b".repeat(64),
        oldPageCount: durable.pageCount,
      };
      return {
        ...common,
        action: "prepared",
        operation: message.operation,
        pivotPage: message.operation === "insert" ? message.after + 1 : message.page,
        oldPageCount: durable.pageCount,
        newPageCount: next,
        oldContentSHA256: "a".repeat(64),
        stagedContentSHA256: "b".repeat(64),
        warnings: [],
      };
    }
    if (message.action === "commit") {
      if (options.hangCommit) return new Promise(() => {});
      if (options.failCommit) throw new Error("native replace failed");
      const prepare = lastPrepare;
      const operation = prepare.operation;
      const oldPageCount = durable.pageCount;
      durable.pageCount += (operation === "insert" ? 1 : 0)
        - (operation === "delete" ? 1 : 0);
      durable.contentSHA256 = "b".repeat(64);
      durable.journal.phase = "replaced";
      return {
        ...common,
        action: "committed",
        operation,
        pivotPage: operation === "insert" ? prepare.after + 1 : prepare.page,
        oldPageCount,
        newPageCount: durable.pageCount,
        contentSHA256: "b".repeat(64),
        mtime: 1_800_000_000,
        byteCount: 123_456,
      };
    }
    if (message.action === "finalize") {
      durable.journal.phase = "committed";
      return { ...common, action: "finalized" };
    }
    if (message.action === "cancel") {
      return { ...common, action: "cancelled" };
    }
    throw new Error(`unexpected PDF mutation action ${message.action}`);
  };
}

async function waitForNativePDFJob(context, jobId) {
  let payload = null;
  for (let attempt = 0; attempt < 200; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    const response = await context.fetch(
      "/pdf/api/job-status?id=" + encodeURIComponent(jobId),
    );
    assert.equal(response.status, 200);
    payload = await response.json();
    if (payload.status === "done" || payload.status === "error") return payload;
  }
  assert.fail(`native PDF job did not settle: ${JSON.stringify(payload)}`);
}

async function waitForPDFMutationAction(messages, action, minimumCount = 1) {
  for (let attempt = 0; attempt < 200; attempt += 1) {
    if (messages.filter((message) => message.action === action).length >= minimumCount) return;
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  assert.fail(`native PDF action was not observed: ${action}`);
}

function makeDataStore(gateEvents, state = { values: new Map(), revision: 0 }) {
  const values = state.values;
  return {
    put(collection, value) {
      const key = `${collection}:${value.id}`;
      const current = values.get(key);
      const record = clone(value);
      state.revision += 1;
      values.set(key, { value: record, rev: (current?.rev || 0) + 1, updatedAt: state.revision });
      if (String(value.id).startsWith("native-idb-gate-")) gateEvents.push("put");
      return Promise.resolve({ ok: true });
    },
    get(collection, id) {
      const key = `${collection}:${id}`;
      const value = values.get(key);
      if (String(id).startsWith("native-idb-gate-")) gateEvents.push("get");
      return Promise.resolve(value == null ? null : clone(value));
    },
    getMany(requests) {
      return Promise.resolve(requests.map(({ collection, id }) => {
        const value = values.get(`${collection}:${id}`);
        return value == null ? null : clone(value);
      }));
    },
    batch(mutations) {
      for (const mutation of mutations) {
        const value = clone(mutation.value);
        const key = `${mutation.collection}:${value.id}`;
        const current = values.get(key);
        const expected = mutation.options?.ifRev;
        if (expected != null && expected !== (current?.rev || 0)) {
          const error = new Error("revision conflict");
          error.code = "BW_DATA_CONFLICT";
          return Promise.reject(error);
        }
      }
      for (const mutation of mutations) {
        const value = clone(mutation.value);
        const key = `${mutation.collection}:${value.id}`;
        const current = values.get(key);
        state.revision += 1;
        values.set(key, {
          value,
          rev: (current?.rev || 0) + 1,
          updatedAt: state.revision,
        });
      }
      return Promise.resolve(mutations.map(() => ({ ok: true })));
    },
    remove(collection, id) {
      values.delete(`${collection}:${id}`);
      if (String(id).startsWith("native-idb-gate-")) gateEvents.push("remove");
      return Promise.resolve({ ok: true });
    },
  };
}

async function harness(options = {}) {
  const gateEvents = [];
  const preferenceEvents = [];
  const gatewayMessages = [];
  const originalFetchCalls = [];
  const pageTextMessages = [];
  const pdfMutationMessages = [];
  const eventListeners = new Map();
  const localStorage = options.localStorageState || new Map();
  const dataStoresState = options.dataStoresState || {
    global: { values: new Map(), revision: 0 },
    document: { values: new Map(), revision: 0 },
    device: { values: new Map(), revision: 0 },
  };
  const globalStore = makeDataStore([], dataStoresState.global);
  const documentStore = makeDataStore(gateEvents, dataStoresState.document);
  const deviceStore = makeDataStore(gateEvents, dataStoresState.device);
  const pendingStores = [globalStore, documentStore, deviceStore];
  const router = {
    contract: "storage-router/1",
    get: (collection, id) => globalStore.get(collection, id),
    put: (collection, value) => globalStore.put(collection, value),
    remove: (collection, id) => globalStore.remove(collection, id),
    subscribe: () => () => {},
  };
  const shellPath = "/r/" + "a".repeat(64) +
    `/shells/${options.surface === "epub" ? "epub" : "pdf"}.html`;
  const defaultStreamURL = "http://127.0.0.1:43129/r/" + "a".repeat(64) +
    "/pi-proxy/" + "c".repeat(32);
  const context = {
    URL,
    URLSearchParams,
    Request,
    Response,
    Blob,
    Uint8Array,
    TextEncoder,
    TextDecoder,
    ReadableStream,
    Set,
    Map,
    Promise,
    setTimeout: options.setTimeout || setTimeout,
    clearTimeout: options.clearTimeout || clearTimeout,
    setInterval: options.setInterval || setInterval,
    clearInterval: options.clearInterval || clearInterval,
    Date: options.Date || Date,
    JSON,
    Error,
    atob,
    btoa,
    crypto: webcrypto,
    console,
    location: {
      origin: "http://127.0.0.1:43129",
      href: "http://127.0.0.1:43129" + shellPath,
      pathname: shellPath,
    },
    __BW_NATIVE_LOCAL_READER__: true,
    __BW_NATIVE_LOCAL_BOOK_ID__: options.bookId || DEFAULT_LOCAL_BOOK_ID,
    __BW_NATIVE_LOCAL_BASE_PATH__: "/r/" + "a".repeat(64),
    __BW_NATIVE_INTERFACE_MANIFEST__: clone(
      options.interfaceManifest || NATIVE_INTERFACE_MANIFEST,
    ),
    localStorage: {
      getItem: (key) => localStorage.get(key) ?? null,
      setItem: (key, value) => localStorage.set(key, String(value)),
      removeItem: (key) => localStorage.delete(key),
    },
    navigator: { sendBeacon: () => false },
    document: {
      readyState: "complete",
      visibilityState: "visible",
      getElementById: () => null,
      createElement: (tagName) => (
        tagName === "canvas" && options.canvasFactory
          ? options.canvasFactory()
          : { style: {}, appendChild() {} }
      ),
      body: { appendChild() {} },
      documentElement: { appendChild() {} },
      addEventListener() {},
      removeEventListener() {},
    },
    CustomEvent: class CustomEvent {
      constructor(type, init) { this.type = type; this.detail = init?.detail; }
    },
    addEventListener(type, listener) {
      const list = eventListeners.get(type) || [];
      list.push(listener);
      eventListeners.set(type, list);
    },
    dispatchEvent(event) {
      for (const listener of eventListeners.get(event.type) || []) listener(event);
      return true;
    },
    fetch: async (input, init) => {
      originalFetchCalls.push({ input, init });
      if (options.originalFetch) return options.originalFetch(input, init);
      const url = new URL(typeof input === "string" ? input : input.url, context.location.href);
      if (/\/pi-proxy\/[0-9a-f]{32}$/.test(url.pathname)) {
        const payload = typeof options.piProxyResponse === "function"
          ? await options.piProxyResponse(clone(gatewayMessages.at(-1)))
          : { ok: true };
        return new Response(JSON.stringify(payload), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response("not intercepted", { status: 418 });
    },
    webkit: {
      messageHandlers: {
        bwNativePiGateway: {
          postMessage(message) {
            gatewayMessages.push(clone(message));
            if (options.gatewayReply) {
              return Promise.resolve().then(() => options.gatewayReply(clone(message)));
            }
            return Promise.resolve({
              contract: "reader-native-pi-response/2",
              streamURL: defaultStreamURL,
            });
          },
        },
        ...(options.pageTextReply ? {
          bwNativePageText: {
            postMessage(message) {
              pageTextMessages.push(clone(message));
              return Promise.resolve(options.pageTextReply(clone(message)));
            },
          },
        } : {}),
        ...(options.pdfMutationReply ? {
          bwNativePDFMutation: {
            postMessage(message) {
              pdfMutationMessages.push(clone(message));
              return Promise.resolve().then(() =>
                options.pdfMutationReply(clone(message))
              );
            },
          },
        } : {}),
      },
    },
    BWReaderRuntime: {
      indexedDBStore: {
        createIndexedDBDataStore: () => pendingStores.shift(),
      },
      dataRegistry: {
        CONTRACT: "data-registry/1",
        syncCollections: () => [],
        scopes: () => ({}),
        settingMigrations: () => [{
          legacyKey: "eph-th",
          collection: "user-settings",
          semanticKey: "reader.theme",
          codec: "string",
        }],
        collection: () => ({ status: "ready" }),
      },
      storageRouter: {
        createStorageRouter: () => router,
      },
      preferenceStore: {
        createPreferenceStore(options) {
          preferenceEvents.push({ phase: "create", options });
          return {
            contract: "preference-store/1",
            attach(nextRouter, lease) {
              preferenceEvents.push({ phase: "attach", lease });
              if (options.preferenceAttach) {
                return options.preferenceAttach(nextRouter, lease);
              }
              return Promise.resolve().then(() => nextRouter.put("user-settings", {
                id: "setting:reader.theme",
                value: "dark",
              }));
            },
          };
        },
      },
    },
  };
  context.globalThis = context;
  context.window = context;
  if (options.accountNamespace) {
    vm.runInNewContext(ACCOUNT_CONTEXT, context, { filename: "account-context.js" });
    context.BWReaderRuntime.accountContext.activate({
      namespace: options.accountNamespace,
      source: "provider-ticket",
    });
  }
  vm.runInNewContext(SOURCE, context, { filename: "native-local-runtime.js" });
  const readyPromise = context.BWReaderRuntime.nativeLocalRuntime.ready();
  if (options.awaitReady !== false) await readyPromise;
  return {
    context,
    gateEvents,
    gatewayMessages,
    originalFetchCalls,
    pageTextMessages,
    pdfMutationMessages,
    preferenceEvents,
    localStorageState: localStorage,
    dataStoresState,
    globalStore,
    documentStore,
    deviceStore,
    readyPromise,
  };
}

test("native local runtime does not mutate both stores merely to open a book", async () => {
  const { context, gateEvents } = await harness();
  assert.equal(context.BWReaderRuntime.nativeLocalRuntime.status().state, "ready");
  assert.deepEqual(gateEvents, []);
});

test("ReaderPC contextual Chinese meanings persist only in the App device store", async () => {
  const first = await harness();
  const request = {
    mode: "meaning",
    term: "それどころではない",
    context: "締切が迫っていて、それどころではない。",
    reading: "",
    english: "",
  };
  const cache = first.context.BWReaderRuntime.nativeLocalRuntime
    .dictionaryFallbackCache;
  assert.equal(await cache.get(request), null);
  await cache.put(request, {
    language: "zh-CN",
    text: "根本顾不上那件事",
    source: "pc-codex-cli",
  });
  assert.equal(first.gatewayMessages.length, 0);

  const reopened = await harness({
    dataStoresState: first.dataStoresState,
    localStorageState: first.localStorageState,
  });
  assert.deepEqual(
    JSON.parse(JSON.stringify(
      await reopened.context.BWReaderRuntime.nativeLocalRuntime
        .dictionaryFallbackCache.get(request),
    )),
    {
      language: "zh-CN",
      text: "根本顾不上那件事",
      source: "pc-codex-cli",
      cached: true,
    },
  );
  assert.equal(
    await reopened.context.BWReaderRuntime.nativeLocalRuntime
      .dictionaryFallbackCache.get({ ...request, context: "不同句境" }),
    null,
  );
  assert.equal(reopened.gatewayMessages.length, 0);
});

test("native PDF book metadata bypasses a pending annotation-store boot", async () => {
  let releasePreferences;
  const pendingPreferences = new Promise((resolve) => {
    releasePreferences = resolve;
  });
  const result = await harness({
    awaitReady: false,
    preferenceAttach: () => pendingPreferences,
    originalFetch(input) {
      const url = new URL(typeof input === "string" ? input : input.url,
        "http://127.0.0.1:43129");
      assert.equal(
        url.pathname,
        "/r/" + "a".repeat(64) + "/native-api/book-meta",
      );
      return Promise.resolve(new Response(JSON.stringify({
        ok: true, page_count: 337, page_w: 612, page_h: 792,
      }), { status: 200, headers: { "Content-Type": "application/json" } }));
    },
  });

  assert.equal(
    result.context.BWReaderRuntime.nativeLocalRuntime.status().state,
    "starting",
  );
  const response = await result.context.fetch(
    "/pdf/api/book-meta?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE),
  );
  assert.deepEqual(await response.json(), {
    ok: true, page_count: 337, page_w: 612, page_h: 792,
  });

  releasePreferences();
  await result.readyPromise;
  assert.equal(
    result.context.BWReaderRuntime.nativeLocalRuntime.status().state,
    "ready",
  );
});

test("clean native PDF boot accepts a no-mutation recovery receipt without a full-content digest", async () => {
  const result = await harness({
    pdfMutationReply(message) {
      assert.equal(message.action, "recover");
      assert.equal(message.ticket, null);
      assert.equal(message.oldContentSHA256, null);
      assert.equal(message.stagedContentSHA256, null);
      return {
        contract: "reader-native-pdf-mutation-response/1",
        action: "recovered",
        requestId: message.requestId,
        ok: true,
        localBookId: message.localBookId,
        ticket: null,
        outcome: "none",
        contentSHA256: null,
        mtime: 1_800_000_000,
        byteCount: 626_900_000,
      };
    },
  });

  assert.equal(result.context.BWReaderRuntime.nativeLocalRuntime.status().state, "ready");
  assert.deepEqual(
    result.pdfMutationMessages.map((message) => message.action),
    ["recover"],
  );
});

test("IndexedDB batch queues its first request before yielding to a Promise", () => {
  const source = readFileSync(
    new URL("_server_deploy/static/reader-runtime/indexeddb-store.js", ROOT),
    "utf8",
  );
  const start = source.indexOf("function batch(mutations");
  const end = source.indexOf("function changes(query)", start);
  assert.ok(start >= 0 && end > start, "IndexedDB batch implementation must be present");
  const batch = source.slice(start, end);
  assert.match(batch, /var work = applyMutation\(mutations\[0\]\)/);
  assert.doesNotMatch(batch, /Promise\.resolve\(\)\)\.then\(function \(\) \{\s*var work/);
});

test("native local runtime hydrates PreferenceStore into user-settings before ready", async () => {
  const { context, preferenceEvents, globalStore } = await harness();
  assert.deepEqual(preferenceEvents.map((event) => event.phase), ["create", "attach"]);
  assert.equal(preferenceEvents[0].options.accountContext.CONTRACT, "account-context/1");
  assert.equal(preferenceEvents[0].options.messageBridge, false);
  assert.equal(context.BWReaderRuntime.nativeLocalRuntime.status().preferencesBound, true);
  assert.equal(context.__BW_READER_PREFERENCES__.contract, "preference-store/1");
  const record = await globalStore.get("user-settings", "setting:reader.theme");
  assert.equal(record.value.value, "dark");
});

test("native ocr-selection persists through the strict bridge and immediately overrides embedded text", async () => {
  const result = await harness({
    interfaceManifest: withNativeOCRMutationRoutesSupported(),
    pageTextReply(message) {
      if (message.action === "ocr-selection") {
        return {
          contract: "reader-native-page-text-response/1",
          action: message.action,
          requestId: message.requestId,
          ok: true,
          state: "ready",
          source: "apple",
          revision: "selection-revision",
          error: null,
          page: message.page,
          text: "正确文字",
          cv: "selection-revision",
          persisted: true,
          textAuthority: "local-override",
        };
      }
      if (message.action === "page-chars") {
        return nativePageReply(message, { text: "正确文字" });
      }
      throw new Error(`unexpected action ${message.action}`);
    },
  });
  result.context.BWReaderRuntime.pageTextProvider.registerEmbeddedPage(3, {
    pageWidth: 600,
    pageHeight: 800,
    revision: "embedded-old",
    chars: [{ c: "乱码", x0: 10, y0: 20, x1: 70, y1: 40, w: 1, bk: 1 }],
  });
  const response = await result.context.fetch("/pdf/api/ocr-selection", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      file: DEFAULT_LOCAL_FILE,
      page: 3,
      bbox: [10, 20, 70, 40],
      model: "",
      effort: "",
    }),
  });
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), {
    ok: true,
    text: "正确文字",
    cv: "selection-revision",
    persisted: true,
    persistence: "native-ocr-fix",
  });
  assert.deepEqual(result.pageTextMessages[0], {
    contract: "reader-native-page-text-request/1",
    action: "ocr-selection",
    requestId: result.pageTextMessages[0].requestId,
    localBookId: DEFAULT_LOCAL_BOOK_ID,
    page: 3,
    bbox: [10, 20, 70, 40],
  });
  const chars = await (await result.context.fetch(
    "/pdf/api/page-chars?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE) + "&page=3",
  )).json();
  assert.equal(chars.ok, true);
  assert.equal(chars.chars.map((item) => item.c).join(""), "正确文字");
  assert.deepEqual(result.pageTextMessages.map((message) => message.action), [
    "ocr-selection", "page-chars",
  ]);
  assert.equal(result.gatewayMessages.length, 0);
});

test("native single-page reOCR is durable and clear removes only that manual authority", async () => {
  const result = await harness({
    interfaceManifest: withNativeOCRMutationRoutesSupported(),
    pageTextReply(message) {
      if (message.action === "reocr-page") {
        return {
          contract: "reader-native-page-text-response/1",
          action: message.action,
          requestId: message.requestId,
          ok: true,
          state: "ready",
          source: "apple",
          revision: "manual-revision",
          error: null,
          page: message.page,
          chars: 22,
          cv: "manual-revision",
          textAuthority: "local-override",
        };
      }
      if (message.action === "clear-reocr-page") {
        return {
          contract: "reader-native-page-text-response/1",
          action: message.action,
          requestId: message.requestId,
          ok: true,
          state: "ready",
          source: "apple",
          revision: "selection-still-present",
          error: null,
          page: message.page,
          cleared: true,
          cv: "selection-still-present",
          textAuthority: "local-override",
        };
      }
      if (message.action === "page-chars") {
        return nativePageReply(message, { text: "选区校正仍在" });
      }
      throw new Error(`unexpected action ${message.action}`);
    },
  });
  result.context.BWReaderRuntime.pageTextProvider.registerEmbeddedPage(5, {
    pageWidth: 600,
    pageHeight: 800,
    chars: [{ c: "原文字", x0: 1, y0: 1, x1: 20, y1: 20, w: 1, bk: 1 }],
  });
  const reocr = await result.context.fetch("/pdf/api/reocr-page", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file: DEFAULT_LOCAL_FILE, page: 5 }),
  });
  assert.deepEqual(await reocr.json(), { ok: true, chars: 22, cv: "manual-revision" });
  const cleared = await result.context.fetch("/pdf/api/reocr-page/clear", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file: DEFAULT_LOCAL_FILE, page: 5 }),
  });
  assert.deepEqual(await cleared.json(), {
    ok: true, cleared: true, cv: "selection-still-present",
  });
  const chars = await (await result.context.fetch(
    "/pdf/api/page-chars?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE) + "&page=5",
  )).json();
  assert.equal(chars.chars.map((item) => item.c).join(""), "选区校正仍在");
  assert.deepEqual(result.pageTextMessages.map((message) => message.action), [
    "reocr-page", "clear-reocr-page", "page-chars",
  ]);
  assert.equal(result.gatewayMessages.length, 0);
});

test("native manual OCR routes fail visibly when Vision or durable storage fails", async () => {
  const manifest = withNativeOCRMutationRoutesSupported();
  const failed = await harness({
    interfaceManifest: manifest,
    pageTextReply(message) {
      return {
        contract: "reader-native-page-text-response/1",
        action: message.action,
        requestId: message.requestId,
        ok: false,
        state: "failed",
        source: "apple",
        revision: "0",
        error: {
          code: "BW_NATIVE_PAGE_TEXT_READ_FAILED",
          message: "保存本机文字预处理结果失败",
          retryable: true,
        },
        page: message.page,
        chars: 0,
        cv: "0",
        textAuthority: "supplemental",
      };
    },
  });
  const response = await failed.context.fetch("/pdf/api/reocr-page", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file: DEFAULT_LOCAL_FILE, page: 2 }),
  });
  assert.equal(response.status, 422);
  const payload = await response.json();
  assert.equal(payload.ok, false);
  assert.match(payload.error, /保存本机文字预处理结果失败/);

  const unavailable = await harness({ interfaceManifest: manifest });
  const missing = await unavailable.context.fetch("/pdf/api/ocr-selection", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file: DEFAULT_LOCAL_FILE, page: 2, bbox: [1, 1, 20, 20] }),
  });
  assert.equal(missing.status, 503);
  assert.equal((await missing.json()).ok, false);
  assert.equal(unavailable.gatewayMessages.length, 0);
});

async function sha256Text(value) {
  const bytes = new TextEncoder().encode(value);
  const digest = await webcrypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

test("book user-state bridge snapshots all domains and atomically recombines ink regions", async () => {
  const { context, documentStore } = await harness();
  const api = context.BWReaderRuntime.nativeLocalRuntime.bookUserState;
  const localBookId = "localbook-" + "b".repeat(64);
  const snapshot = await api.snapshotHeaders({
    contract: "reader-book-user-state-web-request/1",
    action: "snapshot-headers",
    requestId: "usr_" + "1".repeat(32),
    localBookId,
  });
  assert.equal(snapshot.contract, "reader-book-user-state-web-response/1");
  assert.equal(snapshot.headers.length, 8);
  assert.deepEqual(
    snapshot.headers.map((item) => item.name),
    [
      "reading-position", "highlights", "ink", "closed-regions",
      "notes", "user-pages", "card-placements", "entity-references",
    ],
  );
  assert.equal(snapshot.headers.find((item) => item.name === "ink").empty, true);

  const inkJSON = '{"epub":{},"pdf":{"1":[{"pts":[[1,2]],"t":"pen"}]}}';
  const regionJSON = '{"epub":{},"pdf":{"1":[{"regionId":"r1","t":"region"}]}}';
  const domains = [
    ["reading-position", '{"kind":"pdf","pos":1.0,"ts":1}'],
    ["ink", inkJSON],
    ["closed-regions", regionJSON],
  ];
  const transactionDomains = [];
  for (const [name, payloadJson] of domains) {
    transactionDomains.push({
      name,
      revision: 1,
      digest: await sha256Text(payloadJson),
      byteCount: new TextEncoder().encode(payloadJson).byteLength,
      empty: false,
      payloadJson,
    });
  }
  const applied = await api.applyAtomically({
    contract: "reader-book-user-state-web-request/1",
    action: "apply-atomically",
    requestId: "usr_" + "2".repeat(32),
    localBookId,
    transaction: {
      contract: "reader-book-user-state-import/1",
      transactionId: "us_" + "3".repeat(32),
      localBookId,
      remoteBookId: "book_" + "4".repeat(32),
      contentSha256: "5".repeat(64),
      packageRevision: 1,
      expectedLocalHeaders: Object.fromEntries(
        snapshot.headers
          .filter((item) => transactionDomains.some((domain) => domain.name === item.name))
          .map(({ name, digest, revision, empty }) => [
            name,
            { digest, revision, empty },
          ]),
      ),
      domains: transactionDomains,
    },
  });
  assert.equal(applied.receipt.committed, true);
  assert.deepEqual(clone(applied.receipt.domainDigests), Object.fromEntries(
    transactionDomains.map((item) => [item.name, item.digest]),
  ));
  const stored = await documentStore.get(
    "native-ink",
    `${localBookId}:ink`,
  );
  assert.equal(stored.value.payload["1"].length, 2);
  assert.equal(stored.value.payload["1"][0].t, "pen");
  assert.equal(stored.value.payload["1"][1].t, "region");
  const after = await api.snapshotHeaders({
    contract: "reader-book-user-state-web-request/1",
    action: "snapshot-headers",
    requestId: "usr_" + "6".repeat(32),
    localBookId,
  });
  assert.equal(
    after.headers.find((item) => item.name === "reading-position").digest,
    transactionDomains.find((item) => item.name === "reading-position").digest,
    "an imported Python digest remains stable even when JS re-encodes 1.0 as 1",
  );
});

test("book user-state import rejects a local edit made after prepare", async () => {
  const { context } = await harness();
  const api = context.BWReaderRuntime.nativeLocalRuntime.bookUserState;
  const localBookId = "localbook-" + "b".repeat(64);
  const prepared = await api.snapshotHeaders({
    contract: "reader-book-user-state-web-request/1",
    action: "snapshot-headers",
    requestId: "usr_" + "7".repeat(32),
    localBookId,
  });
  const expected = prepared.headers.find((item) => item.name === "reading-position");
  await context.fetch("/pdf/api/reading-pos", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file: DEFAULT_LOCAL_FILE, kind: "pdf", pos: 9 }),
  });
  const payloadJson = '{"kind":"pdf","pos":3,"ts":1}';
  const digest = await sha256Text(payloadJson);
  await assert.rejects(
    api.applyAtomically({
      contract: "reader-book-user-state-web-request/1",
      action: "apply-atomically",
      requestId: "usr_" + "8".repeat(32),
      localBookId,
      transaction: {
        contract: "reader-book-user-state-import/1",
        transactionId: "us_" + "9".repeat(32),
        localBookId,
        remoteBookId: "book_" + "a".repeat(32),
        contentSha256: "b".repeat(64),
        packageRevision: 1,
        expectedLocalHeaders: {
          "reading-position": {
            digest: expected.digest,
            revision: expected.revision,
            empty: expected.empty,
          },
        },
        domains: [{
          name: "reading-position",
          revision: 1,
          digest,
          byteCount: new TextEncoder().encode(payloadJson).byteLength,
          empty: false,
          payloadJson,
        }],
      },
    }),
    (error) => error?.code === "BW_USER_STATE_LOCAL_CHANGED",
  );
});

test("book user-state embedded PDF surfaces require a positive one-based page", async () => {
  const { context } = await harness();
  const api = context.BWReaderRuntime.nativeLocalRuntime.bookUserState;
  const localBookId = "localbook-" + "b".repeat(64);
  const prepared = await api.snapshotHeaders({
    contract: "reader-book-user-state-web-request/1",
    action: "snapshot-headers",
    requestId: "usr_" + "a".repeat(32),
    localBookId,
  });
  const expected = prepared.headers.find((item) => item.name === "ink");
  const payloadJson = JSON.stringify({
    pdf: { "pdf|embedded/chapter.pdf|0": [{ t: "pen" }] },
    epub: {},
  });
  await assert.rejects(
    api.applyAtomically({
      contract: "reader-book-user-state-web-request/1",
      action: "apply-atomically",
      requestId: "usr_" + "b".repeat(32),
      localBookId,
      transaction: {
        contract: "reader-book-user-state-import/1",
        transactionId: "us_" + "c".repeat(32),
        localBookId,
        remoteBookId: "book_" + "d".repeat(32),
        contentSha256: "e".repeat(64),
        packageRevision: 1,
        expectedLocalHeaders: {
          ink: {
            digest: expected.digest,
            revision: expected.revision,
            empty: expected.empty,
          },
        },
        domains: [{
          name: "ink",
          revision: 1,
          digest: await sha256Text(payloadJson),
          byteCount: new TextEncoder().encode(payloadJson).byteLength,
          empty: false,
          payloadJson,
        }],
      },
    }),
    (error) => error?.code === "BW_USER_STATE_DOMAIN_INVALID",
  );
});

test("EPUB validates central-directory limits before any member inflation", () => {
  assert.match(SOURCE, /JSZip\.loadAsync\(bytes, \{ checkCRC32: false \}\)/);
  const envelope = SOURCE.indexOf("assertEPUBCentralDirectoryEnvelope(bytes)");
  const load = SOURCE.indexOf("JSZip.loadAsync(bytes");
  const metadataGate = SOURCE.indexOf("names.length > 10000", load);
  const firstTextInflation = SOURCE.indexOf("return zipText(zip", metadataGate);
  assert.ok(envelope >= 0 && load > envelope && metadataGate > load && firstTextInflation > metadataGate);
  assert.match(SOURCE, /0x06054b50/);
  assert.match(SOURCE, /0x02014b50/);
  assert.match(SOURCE, /actualEntries > 10000/);
  assert.match(SOURCE, /actualEntries !== entries/);
  assert.match(SOURCE, /directoryOffset \+ directorySize !== eocd/);
  assert.match(SOURCE, /EPUB 暂不接受 ZIP64/);
  const textReader = SOURCE.slice(
    SOURCE.indexOf("function zipText"),
    SOURCE.indexOf("function loadEPUB"),
  );
  assert.ok(textReader.indexOf("maximumEPUBTextBytes") < textReader.indexOf("new TextDecoder"));
  assert.match(textReader, /boundedInflate/);
  assert.match(textReader, /maximumEPUBTextTotalBytes/);
  assert.match(textReader, /epubTextQueue\.then/);
  assert.match(textReader, /epubTextByPath\[path\]/);
  assert.ok(
    textReader.indexOf("epubActualTextBytes += bytes.byteLength") <
      textReader.indexOf("new TextDecoder"),
  );
  assert.match(textReader, /BW_LOCAL_EPUB_TEXT_LIMIT/);
});

test("bounded inflater rejects actual bytes even when ZIP metadata lies", async () => {
  const inflateSource = SOURCE.slice(
    SOURCE.indexOf("function boundedInflate"),
    SOURCE.indexOf("function zipText"),
  );
  const context = {
    Promise,
    Error,
    Uint8Array,
    RuntimeError: class RuntimeError extends Error {
      constructor(message, code) { super(message); this.code = code; }
    },
  };
  vm.runInNewContext(`${inflateSource};this.inflate=boundedInflate;`, context);
  const entry = (sizes) => ({
    internalStream() {
      const listeners = {};
      let paused = false;
      return {
        on(name, listener) { listeners[name] = listener; return this; },
        pause() { paused = true; },
        resume() {
          for (const size of sizes) {
            if (paused) break;
            listeners.data(new Uint8Array(size));
          }
          if (!paused) listeners.end();
        },
      };
    },
  });
  const exact = await context.inflate(entry([2, 2]), 4, "LIMIT", "fixture");
  assert.equal(exact.byteLength, 4);
  await assert.rejects(
    context.inflate(entry([3, 2]), 4, "LIMIT", "fixture"),
    (error) => error.code === "LIMIT" && /实际解压大小/.test(error.message),
  );
});

test("EPUB preflight counts real central headers and rejects declaration tricks", () => {
  const gateSource = SOURCE.slice(
    SOURCE.indexOf("function assertEPUBCentralDirectoryEnvelope"),
    SOURCE.indexOf("function loadEPUB"),
  );
  const context = {
    DataView,
    RuntimeError: class RuntimeError extends Error {
      constructor(message, code) { super(message); this.code = code; }
    },
  };
  vm.runInNewContext(`${gateSource};this.gate=assertEPUBCentralDirectoryEnvelope;`, context);

  const archive = (actual, declared = actual, commentLength = 0) => {
    const centralSize = actual * 46;
    const bytes = new ArrayBuffer(centralSize + 22 + commentLength);
    const view = new DataView(bytes);
    for (let index = 0; index < actual; index += 1) {
      view.setUint32(index * 46, 0x02014b50, true);
    }
    const eocd = centralSize;
    view.setUint32(eocd, 0x06054b50, true);
    view.setUint16(eocd + 8, declared, true);
    view.setUint16(eocd + 10, declared, true);
    view.setUint32(eocd + 12, centralSize, true);
    view.setUint32(eocd + 16, 0, true);
    view.setUint16(eocd + 20, commentLength, true);
    return bytes;
  };

  assert.doesNotThrow(() => context.gate(archive(1)));
  assert.throws(() => context.gate(archive(2, 1)), /计数不一致/);
  assert.throws(() => context.gate(archive(1, 0)), /计数不一致/);
  assert.throws(() => context.gate(archive(0, 0xffff)), /ZIP64/);
  assert.doesNotThrow(() => context.gate(archive(1, 1, 8)));
  const hiddenTrailingHeader = archive(2, 1);
  new DataView(hiddenTrailingHeader).setUint32(92 + 12, 46, true);
  assert.throws(() => context.gate(hiddenTrailingHeader), /声明的文件项过多/);
});

test("EPUB sections fail closed through the strict DOMPurify HTML profile", () => {
  const sanitizer = SOURCE.slice(
    SOURCE.indexOf("function epubSanitizerOptions"),
    SOURCE.indexOf("function handleEPUB"),
  );
  assert.match(sanitizer, /BW_LOCAL_EPUB_SANITIZER_UNAVAILABLE/);
  assert.match(sanitizer, /DOMPurify\.sanitize/);
  assert.match(sanitizer, /USE_PROFILES: \{ html: true \}/);
  assert.match(sanitizer, /'style', 'svg', 'math'/);
  assert.match(sanitizer, /'area', 'map'/);
  assert.match(sanitizer, /'srcset', 'usemap', 'ismap', 'background'/);
  assert.match(sanitizer, /querySelectorAll\('\[href\]'\)/);
  assert.match(sanitizer, /tagName \|\| ''\)\.toLowerCase\(\) !== 'a'/);
  assert.match(sanitizer, /ALLOW_UNKNOWN_PROTOCOLS: false/);
  assert.match(sanitizer, /allowCustomizedBuiltInElements: false/);
  assert.ok(
    sanitizer.indexOf("DOMPurify.sanitize") <
      sanitizer.indexOf("new DOMParser().parseFromString"),
  );
});

test("EPUB local search matches sanitized visible text with bounded legacy excerpts", () => {
  const searchSource = SOURCE.slice(
    SOURCE.indexOf("function appendEPUBSearchMatches"),
    SOURCE.indexOf("function localEPUBSearch"),
  );
  const context = { String, Array, Math };
  vm.runInNewContext(
    `${searchSource};this.match=appendEPUBSearchMatches;`,
    context,
  );
  const results = [];
  context.match("before Target after target then TARGET", "target", 4, "chapter-5", results);
  assert.equal(results.length, 3);
  assert.deepEqual(results.map((item) => item.idx), [4, 4, 4]);
  assert.deepEqual(results.map((item) => item.loc), ["chapter-5", "chapter-5", "chapter-5"]);
  assert.equal(results.every((item) => item.excerpt.includes("\u0001") && item.excerpt.includes("\u0002")), true);
  const capped = Array.from({ length: 79 }, (_, idx) => ({ idx }));
  context.match("target target target", "target", 5, "chapter-6", capped);
  assert.equal(capped.length, 80);
  const suppliedContext = SOURCE.slice(
    SOURCE.indexOf("function nativeEPUBAssistantContext"),
    SOURCE.indexOf("function nativePiJSON"),
  );
  assert.match(suppliedContext, /epubSectionVisibleText\(epub, index\)/);
  assert.match(suppliedContext, /context\.visible_text/);
});

test("EPUB resource inflation is serial and bounded before bytes are materialized", () => {
  assert.match(SOURCE, /maximumEPUBResourceBytes = 32 \* 1024 \* 1024/);
  assert.match(SOURCE, /maximumEPUBResourceTotalBytes = 128 \* 1024 \* 1024/);
  assert.match(SOURCE, /maximumEPUBResourceCount = 128/);
  const resources = SOURCE.slice(
    SOURCE.indexOf("function createEPUBResourceBudget"),
    SOURCE.indexOf("function requestHeader"),
  );
  assert.ok(
    resources.indexOf("budgetedResource(epub, path, budget)") <
      resources.indexOf("boundedInflate("),
  );
  assert.match(resources, /shared\.queue\.then/);
  assert.match(resources, /sharedEPUBResourceBudget/);
  assert.match(resources, /shared\.urlByPath\[path\]/);
  assert.doesNotMatch(resources, /Promise\.all\(tasks\)/);
  assert.doesNotMatch(resources, /Promise\.all\(cssItems/);
  assert.match(resources, /resource exceeds local budget/);
  assert.doesNotMatch(resources, /\.async\('(blob|uint8array)'\)/);
  assert.doesNotMatch(resources, /sectionBlobURLs|cssBlobURLs/);
  assert.match(resources, /new root\.CSSStyleSheet\(\)/);
  assert.match(resources, /sheet\.replaceSync/);
  assert.match(resources, /scopeEPUBCSSRules/);
  assert.match(resources, /#ep-col/);
  assert.match(resources, /BW_LOCAL_EPUB_CSS_UNAVAILABLE/);
  assert.doesNotMatch(resources, /epub-css'[\s\S]*textResponse\('', 200/);
});

test("EPUB local manifest preserves title, sha, EPUB3 nav or EPUB2 NCX, section idx, and visible failures", () => {
  const epubSource = SOURCE.slice(
    SOURCE.indexOf("function xmlElementsByLocalName"),
    SOURCE.indexOf("function requestHeader"),
  );
  assert.match(epubSource, /root\.EPUB_CFG && root\.EPUB_CFG\.sha/);
  assert.match(epubSource, /xmlElementsByLocalName\(opf, 'title'\)/);
  assert.match(epubSource, /function epub3TOC/);
  assert.match(epubSource, /getAttribute\('epub:type'\)/);
  assert.match(epubSource, /function epub2TOC/);
  assert.match(epubSource, /getAttribute\('src'\)/);
  assert.match(epubSource, /toc: epub\.toc, sha: epub\.sha/);
  assert.match(epubSource, /\{ ok: true, html: html, idx: idx \}/);
  assert.match(epubSource, /epubJSONRoute/);
  assert.match(epubSource, /epubCSSFailureResponse/);

  const selectorSource = SOURCE.slice(
    SOURCE.indexOf("function splitEPUBSelectorList"),
    SOURCE.indexOf("function rewriteEPUBCSSURLs"),
  );
  const context = {
    RuntimeError: class RuntimeError extends Error {
      constructor(message, code) { super(message); this.code = code; }
    },
  };
  vm.runInNewContext(
    `${selectorSource};this.split=splitEPUBSelectorList;this.scope=scopeEPUBSelector;`,
    context,
  );
  assert.deepEqual(
    Array.from(context.split("p:is(.lead,.body), body > h1")),
    ["p:is(.lead,.body)", "body > h1"],
  );
  assert.equal(context.scope("body > h1"), "#ep-col > h1");
  assert.equal(context.scope("p.note"), "#ep-col p.note");
});

test("AI markdown is purified after marked and math restoration", () => {
  let purifierInput = "";
  let purifierConfig = null;
  const context = {
    window: null,
    marked: { parse(value) { return String(value) + '<img src=x onerror=alert(1)><svg><a xlink:href="javascript:x">x</a></svg>'; } },
    DOMPurify: {
      sanitize(value, config) {
        purifierInput = String(value);
        purifierConfig = config;
        return "<p>safe</p>";
      },
    },
    document: {
      createElement() {
        return {
          _html: "",
          set innerHTML(value) { this._html = String(value); },
          get innerHTML() { return this._html; },
          querySelectorAll() { return []; },
        };
      },
    },
  };
  context.window = context;
  vm.runInNewContext(RC_MD, context, { filename: "rc-md.js" });
  assert.equal(context.RC.md("$a<b$"), "<p>safe</p>");
  assert.match(purifierInput, /&lt;/);
  assert.equal(purifierConfig.USE_PROFILES.html, true);
  assert.equal(purifierConfig.ALLOW_UNKNOWN_PROTOCOLS, false);
  for (const token of ["style", "svg", "math", "form", "iframe", "object", "embed"]) {
    assert.ok(purifierConfig.FORBID_TAGS.includes(token));
  }
  for (const token of ["style", "srcdoc", "srcset", "xlink:href", "formaction"]) {
    assert.ok(purifierConfig.FORBID_ATTR.includes(token));
  }
  assert.match(PDF_AI, /typeof RC\.safeHtml === 'function'/);
  assert.match(PDF_AI, /\? RC\.safeHtml\(html\)/);
});

test("native Pi fetch uses the shared manifest and rejects unclassified or wrong-method routes", async () => {
  const { context, gatewayMessages, originalFetchCalls } = await harness();
  const response = await context.fetch("/pdf/api/translate", {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ text: "hello" }),
  });
  assert.equal(response.status, 200);
  assert.deepEqual(gatewayMessages, [{
    contract: "reader-native-pi-request/2",
    action: "fetch",
    method: "POST",
    path: "/pdf/api/translate",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ text: "hello" }),
    bodyEncoding: "utf8",
  }]);
  assert.equal(originalFetchCalls.length, 1);
  assert.match(String(originalFetchCalls[0].input), /\/pi-proxy\/[0-9a-f]{32}$/);
  assert.equal(originalFetchCalls[0].init.method, "GET");
  assert.equal(originalFetchCalls[0].init.credentials, "omit");

  const refused = await context.fetch("/pdf/api/dictionary-evil");
  assert.equal(refused.status, 501);
  assert.equal((await refused.json()).code, "BW_NATIVE_INTERFACE_UNCLASSIFIED");
  const wrongMethod = await context.fetch("/pdf/api/translate", { method: "GET" });
  assert.equal(wrongMethod.status, 405);
  assert.equal((await wrongMethod.json()).code, "BW_NATIVE_INTERFACE_METHOD");
  assert.equal(gatewayMessages.length, 1);
  assert.doesNotMatch(SOURCE, /PI_ALLOWED_EXACT|PI_ALLOWED_SEGMENTS/);
  assert.match(SOURCE, /__BW_NATIVE_INTERFACE_MANIFEST__/);
});

test("native Pi fetch preserves binary voice clips and redeems only the scoped stream ticket", async () => {
  const controller = new AbortController();
  const result = await harness({
    originalFetch(input, init) {
      return Promise.resolve(new Response(new Uint8Array([9, 8, 7]), {
        status: 201,
        headers: { "Content-Type": "application/octet-stream" },
      }));
    },
  });
  const response = await result.context.fetch("/api/assistant/voice-clip?id=clip_1", {
    method: "POST",
    headers: { "Content-Type": "audio/mp4" },
    body: new Blob([new Uint8Array([0, 1, 254, 255])], { type: "audio/mp4" }),
    signal: controller.signal,
  });
  assert.equal(response.status, 201);
  assert.deepEqual([...new Uint8Array(await response.arrayBuffer())], [9, 8, 7]);
  assert.deepEqual(result.gatewayMessages, [{
    contract: "reader-native-pi-request/2",
    action: "fetch",
    method: "POST",
    path: "/api/assistant/voice-clip?id=clip_1",
    headers: { Accept: "*/*", "Content-Type": "audio/mp4" },
    body: "AAH+/w==",
    bodyEncoding: "base64",
  }]);
  assert.equal(result.originalFetchCalls.length, 1);
  assert.equal(result.originalFetchCalls[0].init.signal, controller.signal);
  assert.match(
    String(result.originalFetchCalls[0].input),
    /^http:\/\/127\.0\.0\.1:43129\/r\/[0-9a-f]{64}\/pi-proxy\/[0-9a-f]{32}$/,
  );
});

test("native Pi fetch rejects forged or widened stream replies before loopback fetch", async () => {
  const replies = [
    { contract: "reader-native-pi-response/2", streamURL: "https://example.com/pi-proxy/" + "c".repeat(32) },
    { contract: "reader-native-pi-response/2", streamURL: "http://127.0.0.1:43129/r/" + "b".repeat(64) + "/pi-proxy/" + "c".repeat(32) },
    { contract: "reader-native-pi-response/2", streamURL: "http://127.0.0.1:43129/r/" + "a".repeat(64) + "/pi-proxy/" + "c".repeat(32) + "?again=1" },
    { contract: "reader-native-pi-response/2", streamURL: "http://127.0.0.1:43129/r/" + "a".repeat(64) + "/pi-proxy/" + "c".repeat(32), extra: true },
  ];
  for (const reply of replies) {
    const result = await harness({ gatewayReply: () => reply });
    const response = await result.context.fetch("/pdf/api/translate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    assert.equal(response.status, 503);
    assert.equal((await response.json()).code, "BW_PI_GATEWAY_RESPONSE");
    assert.equal(result.originalFetchCalls.length, 0);
  }
});

test("native page-overlay keeps Pi vocabulary and offset while adding device formula regions", async () => {
  const formula = {
    id: "formula-device-1",
    x0: 10, y0: 20, x1: 90, y1: 60,
    state: "ready", latex: "x^2", multiline: false, error: null,
  };
  const result = await harness({
    originalFetch() {
      return Promise.resolve(new Response(JSON.stringify({
        ok: true,
        vocab_marks: [{ word: "integral", rects: [[1, 2, 3, 4]] }],
        vocab_sentences: [{ text: "an integral" }],
        mastered_furi: ["既知"],
        offset: { dx: 3, dy: -2, scale: 1.01 },
        cv: "pi-cv-7",
      }), { status: 200, headers: { "Content-Type": "application/json" } }));
    },
    pageTextReply(message) {
      return {
        contract: "reader-native-page-text-response/1",
        action: message.action,
        requestId: message.requestId,
        ok: true,
        state: "readyEmpty",
        source: "apple",
        revision: "device-cv-3",
        page: message.page,
        pageWidth: 600,
        pageHeight: 800,
        chars: [],
        furigana: [],
        wordSegmentation: "ready",
        characterGeometry: "exact",
        formulaCoverage: "complete",
        formulaRegions: [formula],
        error: null,
      };
    },
  });
  const response = await result.context.fetch(
    "/pdf/api/page-overlay?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE) + "&page=2",
  );
  const payload = await response.json();
  assert.equal(response.status, 200);
  assert.deepEqual(payload.vocab_marks, [{ word: "integral", rects: [[1, 2, 3, 4]] }]);
  assert.deepEqual(payload.offset, { dx: 3, dy: -2, scale: 1.01 });
  assert.deepEqual(payload.formula_regions, [formula]);
  assert.equal(payload.native_formula_state, "ready");
  assert.equal(payload.native_formula_source, "apple");
  assert.equal(payload.cv, "pi-cv-7|native:device-cv-3");
});

test("native page-overlay remains locally usable when Pi enrichment hangs", async () => {
  const formula = {
    id: "formula-local-only", x0: 20, y0: 30, x1: 110, y1: 70,
    state: "ready", latex: "E=mc^2", multiline: false, error: null,
  };
  const timeoutDelays = [];
  const result = await harness({
    gatewayReply() { return new Promise(() => {}); },
    setTimeout(callback, delay, ...args) {
      if (delay === 750) {
        timeoutDelays.push(delay);
        queueMicrotask(() => callback(...args));
        return 750;
      }
      return setTimeout(callback, delay, ...args);
    },
    pageTextReply(message) {
      return {
        ...nativePageReply(message, { text: "" }),
        state: "readyEmpty",
        revision: "local-formula-revision",
        chars: [],
        formulaCoverage: "complete",
        formulaRegions: [formula],
      };
    },
  });
  const response = await result.context.fetch(
    "/pdf/api/page-overlay?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE) + "&page=7",
  );
  const payload = await response.json();
  assert.equal(response.status, 200);
  assert.equal(payload.ok, true);
  assert.deepEqual(payload.formula_regions, [formula]);
  assert.deepEqual(payload.vocab_marks, []);
  assert.equal(payload.native_formula_source, "apple");
  assert.deepEqual(timeoutDelays, [750]);
});

test("voice page text reads embedded PDF content locally without opening Pi", async () => {
  const result = await harness({ interfaceManifest: withNativeLocalContentRoutesSupported() });
  const text = "local voice context ".repeat(120);
  result.context.BWReaderRuntime.pageTextProvider.registerEmbeddedPage(6, {
    pageWidth: 600, pageHeight: 800, revision: "voice-local",
    chars: Array.from(text).map((c, index) => ({
      c, x0: index, y0: 20, x1: index + 1, y1: 32, w: 1, bk: 1, sp: c === " ",
    })),
  });
  const response = await result.context.fetch(
    "/api/assistant/voice-page-text?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE) + "&page=6",
  );
  const payload = await response.json();
  assert.equal(response.status, 200);
  assert.equal(payload.ok, true);
  assert.equal(payload.text.length, 1500);
  assert.equal(payload.text.startsWith("local voice context"), true);
  assert.equal(result.gatewayMessages.length, 0);
});

test("book crop is App-owned, validated, and durable across runtime reload", async () => {
  const dataStoresState = {
    global: { values: new Map(), revision: 0 },
    document: { values: new Map(), revision: 0 },
    device: { values: new Map(), revision: 0 },
  };
  const manifest = withNativeLocalContentRoutesSupported();
  const first = await harness({ dataStoresState, interfaceManifest: manifest });
  const initial = await first.context.fetch(
    "/pdf/api/book-crop?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE),
  );
  assert.deepEqual(await initial.json(), { ok: true, crop: { l: 0, r: 0, t: 0, b: 0 } });
  const saved = await first.context.fetch("/pdf/api/book-crop", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file: DEFAULT_LOCAL_FILE, crop: { l: 4.5, r: 3, t: 2, b: 1 } }),
  });
  assert.equal(saved.status, 200);
  const second = await harness({ dataStoresState, interfaceManifest: manifest });
  const loaded = await second.context.fetch(
    "/pdf/api/book-crop?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE),
  );
  assert.deepEqual((await loaded.json()).crop, { l: 4.5, r: 3, t: 2, b: 1 });
  const invalid = await second.context.fetch("/pdf/api/book-crop", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file: DEFAULT_LOCAL_FILE, crop: { l: 45, r: 45, t: 0, b: 0 } }),
  });
  assert.equal(invalid.status, 400);
  assert.equal(second.gatewayMessages.length, 0);
});

test("page translation keeps PDF text and geometry local and sends only sentence strings to Pi", async () => {
  const result = await harness({
    interfaceManifest: withNativeLocalContentRoutesSupported(),
    piProxyResponse(message) {
      assert.equal(message.path, "/pdf/api/epub-translate-section");
      assert.deepEqual(JSON.parse(message.body), { texts: ["Hello."] });
      return { ok: true, translations: ["你好。"], translated: 1, total: 1 };
    },
  });
  result.context.BWReaderRuntime.pageTextProvider.registerEmbeddedPage(2, {
    pageWidth: 612, pageHeight: 792, revision: "translate-local",
    chars: Array.from("Hello.").map((c, index) => ({
      c, x0: 10 + index * 8, y0: 20, x1: 18 + index * 8, y1: 32,
      w: 1, bk: 1, sp: false,
    })),
  });
  const response = await result.context.fetch(
    "/pdf/api/page-translate?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE) + "&page=2",
  );
  const payload = await response.json();
  assert.equal(response.status, 200);
  assert.equal(payload.page_w, 612);
  assert.equal(payload.page_h, 792);
  assert.equal(payload.sentences[0].text, "Hello.");
  assert.equal(payload.sentences[0].zh, "你好。");
  assert.deepEqual(payload.sentences[0].first_char, [10, 20, 18, 32]);
  assert.deepEqual(payload.sentences[0].last_char, [50, 20, 58, 32]);
  const forwarded = JSON.parse(result.gatewayMessages[0].body);
  assert.deepEqual(Object.keys(forwarded), ["texts"]);
});

test("PDF chat and voice share App-supplied local page text and authority state", async () => {
  const result = await harness({
    interfaceManifest: withGenericAssistantRoutesSupported(),
    piProxyResponse() {
      return { ok: true, result: { client_action: null } };
    },
  });
  const text = "This is the exact page currently available on the device.";
  result.context.BWReaderRuntime.pageTextProvider.registerEmbeddedPage(4, {
    pageWidth: 600, pageHeight: 800, revision: "assistant-local-text",
    chars: Array.from(text).map((c, index) => ({
      c, x0: index * 4, y0: 20, x1: index * 4 + 4, y1: 32,
      w: 1, bk: 1, sp: c === " ",
    })),
  });
  const response = await result.context.fetch("/api/assistant/voice-tool", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cmd: "{}", ctx: { page: 4 } }),
  });
  assert.equal(response.status, 200);
  const forwarded = JSON.parse(result.gatewayMessages[0].body);
  assert.equal(forwarded.ctx.visible_text, text);
  assert.equal(forwarded.ctx.native_page_text.page, 4);
  assert.equal(forwarded.ctx.native_page_text.source, "embedded");
  assert.equal(forwarded.ctx.native_local_state.contract,
    "reader-native-pdf-assistant-state/1");
  assert.equal(forwarded.ctx.native_local_state.file, DEFAULT_LOCAL_FILE);
  const requestSource = SOURCE.slice(
    SOURCE.indexOf("function nativePDFRequestBody"),
    SOURCE.indexOf("function nativePDFOperationID"),
  );
  assert.match(requestSource, /nativePDFAssistantContext\(context\)/);
  assert.match(SOURCE.slice(
    SOURCE.indexOf("function nativePDFChatFetch"),
    SOURCE.indexOf("function nativeEPUBAuthoritySnapshot"),
  ), /nativePDFRequestBody\(input, init, 'context'/);
});

test("native book-meta delegates to the capability-scoped PDFKit endpoint", async () => {
  const result = await harness({
    originalFetch(input) {
      const url = new URL(typeof input === "string" ? input : input.url,
        "http://127.0.0.1:43129");
      assert.equal(
        url.pathname,
        "/r/" + "a".repeat(64) + "/native-api/book-meta",
      );
      assert.equal(url.searchParams.get("book"), DEFAULT_LOCAL_BOOK_ID);
      return Promise.resolve(new Response(JSON.stringify({
        ok: true, page_count: 53, page_w: 612, page_h: 792, mtime: 1800000000,
      }), { status: 200, headers: { "Content-Type": "application/json" } }));
    },
  });
  const response = await result.context.fetch(
    "/pdf/api/book-meta?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE),
  );
  assert.deepEqual(await response.json(), {
    ok: true, page_count: 53, page_w: 612, page_h: 792, mtime: 1800000000,
  });
  assert.equal(result.originalFetchCalls.length, 1);
  assert.equal(result.gatewayMessages.length, 0);

  const wrongBook = await result.context.fetch("/pdf/api/book-meta?file=localbook:wrong");
  assert.equal(wrongBook.status, 409);
  assert.equal((await wrongBook.json()).code, "BW_LOCAL_BOOK_META_REQUEST");
  assert.equal(result.originalFetchCalls.length, 1);
});

test("native PDF page images use the loopback PDFKit renderer without Pi", async () => {
  const jpeg = new Uint8Array([0xff, 0xd8, 0xff, 0xd9]);
  const result = await harness({
    originalFetch(input) {
      const url = new URL(typeof input === "string" ? input : input.url,
        "http://127.0.0.1:43129");
      assert.equal(url.pathname, "/pdf/api/page-image");
      assert.equal(url.searchParams.get("file"), DEFAULT_LOCAL_FILE);
      assert.equal(url.searchParams.get("page"), "7");
      assert.equal(url.searchParams.get("w"), "1400");
      return Promise.resolve(new Response(jpeg, {
        status: 200,
        headers: {
          "Content-Type": "image/jpeg",
          "X-BW-PDF-Renderer": "pdfkit",
        },
      }));
    },
  });
  const response = await result.context.fetch(
    `/pdf/api/page-image?file=${encodeURIComponent(DEFAULT_LOCAL_FILE)}` +
      "&page=7&w=1400&v=1800000000",
  );
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("X-BW-PDF-Renderer"), "pdfkit");
  assert.deepEqual(new Uint8Array(await response.arrayBuffer()), jpeg);
  assert.equal(result.originalFetchCalls.length, 1);
  assert.equal(result.gatewayMessages.length, 0);
});

test("native-owned interfaces delegate only to the captured App bridge and never Pi", async () => {
  const nativeRequests = [];
  const result = await harness({
    originalFetch: async (input, init) => {
      nativeRequests.push({ input: String(input), method: init?.method || "GET" });
      return new Response(JSON.stringify({ ok: true, owner: "native-app" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    },
  });
  const response = await result.context.fetch("/pdf/api/to-note", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text: "local note" }),
  });
  assert.equal(response.status, 200);
  assert.equal((await response.json()).owner, "native-app");
  assert.deepEqual(nativeRequests, [{ input: "/pdf/api/to-note", method: "POST" }]);
  assert.equal(result.gatewayMessages.length, 0);
});

test("native local reading position persists through the local document store", async () => {
  const { context } = await harness();
  const put = await context.fetch("/pdf/api/reading-pos", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file: DEFAULT_LOCAL_FILE, kind: "pdf", pos: 12 }),
  });
  assert.equal(put.status, 200);
  const get = await context.fetch("/pdf/api/reading-pos");
  const payload = await get.json();
  assert.equal(payload.positions["localbook:localbook-" + "b".repeat(64)].pos, 12);
});

test("local sidecar mutations serialize concurrent creates and retain every record", async () => {
  const { context, dataStoresState } = await harness();
  const responses = await Promise.all(Array.from({ length: 24 }, (_, index) => (
    context.fetch("/pdf/api/highlights", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        file: DEFAULT_LOCAL_FILE,
        id: `c_${String(index).padStart(16, "0")}`,
        page: index + 1,
        rects: [[10, 20, 30, 40]],
        color: "#ffd54a",
        text: `highlight-${index}`,
      }),
    })
  )));
  assert.deepEqual(responses.map((response) => response.status), Array(24).fill(200));
  const listed = await (await context.fetch(
    "/pdf/api/highlights?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE),
  )).json();
  assert.equal(listed.highlights.length, 24);
  assert.equal(new Set(listed.highlights.map((item) => item.id)).size, 24);
  const record = dataStoresState.document.values.get(
    `native-document-highlights:${DEFAULT_LOCAL_BOOK_ID}:document-highlights`,
  );
  assert.equal(record.rev, 24);
  assert.match(SOURCE, /ifRev: ifRev == null \? undefined : ifRev/);
});

test("local CRUD and ink routes reject malformed writes instead of storing fake success", async () => {
  const { context } = await harness();
  const { context: epubContext } = await harness({ surface: "epub" });
  const json = (body) => ({
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const badHighlight = await context.fetch("/pdf/api/highlights", json({
    file: DEFAULT_LOCAL_FILE, page: 1, rects: [],
  }));
  assert.equal(badHighlight.status, 400);
  const badEPUBHighlight = await epubContext.fetch("/pdf/api/epub-highlights", json({
    file: DEFAULT_LOCAL_FILE, text: "missing anchor",
  }));
  assert.equal(badEPUBHighlight.status, 400);
  const badNote = await context.fetch("/pdf/api/notes", json({
    file: DEFAULT_LOCAL_FILE, anchor: { kind: "pdf", page: 0 },
  }));
  assert.equal(badNote.status, 400);
  const badInk = await context.fetch("/pdf/api/ink", json({
    file: DEFAULT_LOCAL_FILE, page: "not-a-page", strokes: [],
  }));
  assert.equal(badInk.status, 400);
  const badEPUBInk = await epubContext.fetch("/pdf/api/epub-ink", json({
    file: DEFAULT_LOCAL_FILE, strokes: [],
  }));
  assert.equal(badEPUBInk.status, 400);
  const wrongBook = await context.fetch("/pdf/api/userpages", json({
    file: "localbook:localbook-other", after: 1,
  }));
  assert.equal(wrongBook.status, 409);
  const ink = await (await context.fetch(
    "/pdf/api/ink?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE),
  )).json();
  assert.deepEqual(ink.pages, {});
  assert.equal(Object.hasOwn(ink.pages, "NaN"), false);
  assert.equal(Object.hasOwn(ink.pages, "undefined"), false);
});

test("preferences are device-global with null deletion and reading positions aggregate books", async () => {
  const sharedStores = {
    global: { values: new Map(), revision: 0 },
    document: { values: new Map(), revision: 0 },
    device: { values: new Map(), revision: 0 },
  };
  const sharedLocalStorage = new Map();
  const first = await harness({
    dataStoresState: sharedStores,
    localStorageState: sharedLocalStorage,
  });
  const firstPrefs = await first.context.fetch("/pdf/api/prefs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ patch: { theme: "dark", removeMe: "old" } }),
  });
  assert.equal(firstPrefs.status, 200);
  await first.context.fetch("/pdf/api/prefs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ patch: { removeMe: null } }),
  });
  await first.context.fetch("/pdf/api/reading-pos", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file: DEFAULT_LOCAL_FILE, kind: "pdf", pos: 17 }),
  });

  const secondBookId = "localbook-" + "c".repeat(64);
  const secondFile = "localbook:" + secondBookId;
  const second = await harness({
    bookId: secondBookId,
    dataStoresState: sharedStores,
    localStorageState: sharedLocalStorage,
  });
  const secondPrefs = await (await second.context.fetch("/pdf/api/prefs")).json();
  assert.equal(secondPrefs.prefs.theme, "dark");
  assert.equal(Object.hasOwn(secondPrefs.prefs, "removeMe"), false);
  await second.context.fetch("/pdf/api/reading-pos", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file: secondFile, kind: "epub", pos: 9 }),
  });
  const positions = await (await second.context.fetch("/pdf/api/reading-pos")).json();
  assert.equal(positions.positions[DEFAULT_LOCAL_FILE].pos, 17);
  assert.equal(positions.positions[secondFile].pos, 9);
  const refused = await second.context.fetch("/pdf/api/reading-pos", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file: secondFile, kind: "epub", pos: -1 }),
  });
  assert.equal(refused.status, 400);
});

test("video player geometry is App-owned and survives books without contacting Pi", async () => {
  const sharedStores = {
    global: { values: new Map(), revision: 0 },
    document: { values: new Map(), revision: 0 },
    device: { values: new Map(), revision: 0 },
  };
  const sharedLocalStorage = new Map();
  const first = await harness({
    dataStoresState: sharedStores,
    localStorageState: sharedLocalStorage,
  });
  const saved = await first.context.fetch("/pdf/api/video-player-prefs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      patch: { x: 120, y: 48, w: 640, h: 420, showEn: false, subOut: true },
    }),
  });
  assert.equal(saved.status, 200);
  assert.equal(first.gatewayMessages.length, 0);

  const secondBookId = "localbook-" + "d".repeat(64);
  const second = await harness({
    bookId: secondBookId,
    dataStoresState: sharedStores,
    localStorageState: sharedLocalStorage,
  });
  const loaded = await (await second.context.fetch(
    "/pdf/api/video-player-prefs",
  )).json();
  assert.deepEqual(loaded.prefs, {
    x: 120, y: 48, w: 640, h: 420, showEn: false, subOut: true,
  });
  assert.equal(second.gatewayMessages.length, 0);

  const deleted = await second.context.fetch("/pdf/api/video-player-prefs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ patch: { h: null } }),
  });
  assert.equal(deleted.status, 200);
  const afterDelete = await (await second.context.fetch(
    "/pdf/api/video-player-prefs",
  )).json();
  assert.equal(Object.hasOwn(afterDelete.prefs, "h"), false);

  const refused = await second.context.fetch("/pdf/api/video-player-prefs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ patch: { unknown: 1 } }),
  });
  assert.equal(refused.status, 400);
});

test("book language preferences use the established whitelist and stay document-scoped", async () => {
  const { context } = await harness();
  const saved = await context.fetch("/pdf/api/book-langs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file: DEFAULT_LOCAL_FILE, langs: ["ja", "xx", "ja", "en"] }),
  });
  assert.equal(saved.status, 200);
  assert.deepEqual((await saved.json()).langs, ["ja", "en"]);
  const listed = await (await context.fetch(
    "/pdf/api/book-langs?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE),
  )).json();
  assert.deepEqual(listed.langs, ["ja", "en"]);
});

test("native PDF.js prewarm routes are strict local no-ops with trusted page totals", async () => {
  const result = await harness();
  result.context.BWReaderRuntime.pageTextProvider.setEmbeddedPageLoader(
    () => Promise.resolve(null),
    53,
  );
  const started = await result.context.fetch("/pdf/api/prewarm-async", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file: DEFAULT_LOCAL_FILE, width: 1260 }),
  });
  assert.equal(started.status, 200);
  assert.deepEqual(await started.json(), {
    ok: true,
    started: false,
    running: false,
    not_applicable: true,
    reason: "native-pdfjs",
  });
  const status = await result.context.fetch(
    "/pdf/api/prewarm-status?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE) + "&width=1260",
  );
  assert.equal(status.status, 200);
  assert.deepEqual(await status.json(), {
    ok: true,
    total: 53,
    done: 53,
    percent: 100,
    running: false,
    not_applicable: true,
    reason: "native-pdfjs",
  });
  assert.equal(result.gatewayMessages.length, 0);

  const malformed = await result.context.fetch("/pdf/api/prewarm-async", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file: DEFAULT_LOCAL_FILE, width: "1260" }),
  });
  assert.equal(malformed.status, 400);
  const widened = await result.context.fetch(
    "/pdf/api/prewarm-status?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE) +
      "&width=1260&extra=1",
  );
  assert.equal(widened.status, 400);

  const routes = Object.fromEntries(
    NATIVE_INTERFACE_MANIFEST.routes
      .filter((route) => route.path.startsWith("/pdf/api/prewarm-"))
      .map((route) => [route.path, route]),
  );
  for (const route of Object.values(routes)) {
    assert.equal(route.owner, "local");
    assert.equal(route.status, "supported");
    assert.equal(route.remoteBook, null);
  }
});

test("native context-sync keeps local WSS authority while reconciling Pi compatibility", async () => {
  const first = await harness();
  const initialResponse = await first.context.fetch("/pdf/api/context-sync");
  assert.equal(initialResponse.status, 200);
  const initialPayload = await initialResponse.json();
  assert.equal(initialPayload.ok, true);
  assert.equal(initialPayload.enabled, false);
  assert.equal(initialPayload.deliveryMode, "snapshot-mcp");
  assert.equal(initialPayload.ts, 0);
  assert.equal(initialPayload.local_persisted, true);
  assert.equal(initialPayload.windows_context_source, "native-context-wss");
  assert.equal(initialPayload.pi_compatibility.confirmed, false);
  assert.equal(first.gatewayMessages.length, 1);
  assert.equal(first.gatewayMessages[0].path, "/pdf/api/context-sync");
  assert.equal(first.gatewayMessages[0].method, "GET");

  const savedResponse = await first.context.fetch("/pdf/api/context-sync", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: true, deliveryMode: "snapshot-mcp" }),
  });
  assert.equal(savedResponse.status, 200);
  const savedPayload = await savedResponse.json();
  assert.equal(savedPayload.ok, true);
  assert.equal(savedPayload.enabled, true);
  assert.equal(savedPayload.deliveryMode, "snapshot-mcp");
  assert.equal(savedPayload.local_persisted, true);
  assert.equal(savedPayload.pi_compatibility.confirmed, false);
  assert.equal(first.localStorageState.get("eph-ctx-sync"), "1");
  assert.equal(first.gatewayMessages.length, 2);
  assert.equal(first.gatewayMessages[1].path, "/pdf/api/context-sync");
  assert.equal(first.gatewayMessages[1].method, "POST");
  assert.deepEqual(JSON.parse(first.gatewayMessages[1].body), {
    enabled: true,
    deliveryMode: "snapshot-mcp",
  });

  const reloaded = await harness({
    localStorageState: first.localStorageState,
  });
  const persisted = await (
    await reloaded.context.fetch("/pdf/api/context-sync")
  ).json();
  assert.equal(persisted.enabled, true);
  assert.equal(persisted.deliveryMode, "snapshot-mcp");
  assert.ok(persisted.ts > 0);
  assert.equal(persisted.pi_compatibility.confirmed, false);
  assert.equal(reloaded.gatewayMessages.length, 1);

  const requestsBeforeInvalidBodies = reloaded.gatewayMessages.length;
  for (const body of [
    { enabled: true, deliveryMode: "nearby-mode" },
    { enabled: 1, deliveryMode: "snapshot-mcp" },
    { enabled: true, deliveryMode: "snapshot-mcp", extra: true },
  ]) {
    const rejected = await reloaded.context.fetch("/pdf/api/context-sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    assert.equal(rejected.status, 400);
  }
  assert.equal(reloaded.gatewayMessages.length, requestsBeforeInvalidBodies);

  const reconciled = await harness({
    piProxyResponse(message) {
      assert.equal(message.path, "/pdf/api/context-sync");
      assert.equal(message.method, "GET");
      return {
        ok: true,
        enabled: true,
        deliveryMode: "snapshot-mcp",
        ts: 42,
      };
    },
  });
  const reconciledPayload = await (
    await reconciled.context.fetch("/pdf/api/context-sync")
  ).json();
  assert.equal(reconciledPayload.enabled, true);
  assert.equal(reconciledPayload.pi_compatibility.confirmed, true);
  assert.equal(reconciled.localStorageState.get("eph-ctx-sync"), "1");
});

test("shared Reader reporting reaches the native snapshot state end to end", async () => {
  const result = await harness({
    setTimeout(callback, delay) {
      if (delay === 0) queueMicrotask(callback);
      return delay + 1;
    },
    clearTimeout() {},
    setInterval() { return 1; },
    clearInterval() {},
  });
  await enableNativeContext(result);
  assert.equal(result.localStorageState.get("eph-ctx-sync"), "1");
  vm.runInContext(RC_CORE, result.context, { filename: "rc-core.js" });

  assert.equal(result.context.RC.ctxSync.report({
    kind: "pdf",
    file: "localbook:" + "localbook-" + "b".repeat(64),
    pos: 17,
    total: 53,
    title: "本机快照接线验证",
    selection: "当前选中文字",
  }, { immediate: true }), true);

  let payload = null;
  for (let attempt = 0; attempt < 20; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    payload = await (await result.context.fetch("/pdf/api/active-reading")).json();
    if (payload.active) break;
  }
  assert.equal(payload.ok, true);
  assert.equal(payload.enabled, true);
  assert.equal(payload.fresh, true);
  assert.equal(payload.active.kind, "pdf");
  assert.equal(payload.active.pos, 17);
  assert.equal(payload.active.total, 53);
  assert.equal(payload.active.selection, "当前选中文字");
  assert.equal(payload.active.has_selection, true);
  assert.deepEqual(
    result.gatewayMessages.map((message) => message.path),
    ["/pdf/api/context-sync", "/pdf/api/active-reading"],
  );
  assert.equal(JSON.parse(result.gatewayMessages[1].body).pos, 17);
});

test("native pagehide sends active-reading JSON synchronously so the last visible page is not stale", async () => {
  const result = await harness({
    setTimeout() { return 1; },
    clearTimeout() {},
    setInterval() { return 1; },
    clearInterval() {},
  });
  result.context.localStorage.setItem("eph-ctx-sync", "1");
  const nativeBeacon = result.context.navigator.sendBeacon;
  let beaconBody;
  result.context.navigator.sendBeacon = (url, body) => {
    beaconBody = body;
    return nativeBeacon(url, body);
  };
  vm.runInContext(RC_CORE, result.context, { filename: "rc-core.js" });
  result.context.RC.ctxSync.report({
    kind: "pdf",
    file: DEFAULT_LOCAL_FILE,
    pos: 23,
    total: 53,
    title: "最后一页快照",
    selection: "",
  });
  result.context.dispatchEvent({ type: "pagehide" });
  assert.equal(typeof beaconBody, "string");
  assert.equal(JSON.parse(beaconBody).pos, 23);
  let payload;
  for (let attempt = 0; attempt < 20; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    payload = await (await result.context.fetch("/pdf/api/active-reading")).json();
    if (payload.active?.pos === 23) break;
  }
  assert.equal(payload.active.pos, 23);
  assert.equal(result.gatewayMessages.length, 1);
  assert.equal(result.gatewayMessages[0].path, "/pdf/api/active-reading");
  assert.equal(JSON.parse(result.gatewayMessages[0].body).pos, 23);
});

test("native active-reading refreshes locally, preserves explicit selection clearing, and intercepts pagehide beacon", async () => {
  const clock = fakeClock();
  const first = await harness({
    Date: clock.Date,
    gatewayReply() { throw new Error("Pi offline"); },
  });
  await enableNativeContext(first);
  const file = "localbook-" + "b".repeat(64);

  const initial = await first.context.fetch("/pdf/api/active-reading", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      kind: "pdf", file, pos: 4, title: "Local PDF", total: 40,
      selection: "old selection", sel_page: 4,
    }),
  });
  assert.equal(initial.status, 200);
  const initialPayload = await initial.json();
  assert.deepEqual(initialPayload.canonical, {
    kind: "pdf", file, page: 4, viewFile: null, viewPage: null,
  });
  assert.equal(initialPayload.local_persisted, true);
  assert.equal(initialPayload.pi_compatibility.confirmed, false);
  clock.advance(2_000);
  const cleared = await first.context.fetch("/pdf/api/active-reading", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      kind: "pdf", file, pos: 5, title: "Local PDF", total: 40,
      selection: "", sel_page: 5,
    }),
  });
  assert.equal(cleared.status, 200);
  const active = await (await first.context.fetch("/pdf/api/active-reading")).json();
  assert.equal(active.active.pos, 5);
  assert.equal(active.active.selection, "");
  assert.equal(active.active.has_selection, false);
  assert.equal(active.active.ts, (await cleared.json()).ts);
  assert.equal(first.gatewayMessages.length, 3);
  assert.deepEqual(
    first.gatewayMessages.map((message) => message.path),
    [
      "/pdf/api/context-sync",
      "/pdf/api/active-reading",
      "/pdf/api/active-reading",
    ],
  );

  const reloaded = await harness({
    Date: clock.Date,
    localStorageState: first.localStorageState,
    dataStoresState: first.dataStoresState,
  });
  assert.equal((await (await reloaded.context.fetch("/pdf/api/active-reading")).json()).active.pos, 5);
  clock.advance(1_000);
  assert.equal(reloaded.context.navigator.sendBeacon(
    "/pdf/api/active-reading",
    new Blob([JSON.stringify({ kind: "pdf", file, pos: 6, selection: "" })], {
      type: "application/json",
    }),
  ), true);
  let beaconActive = null;
  for (let attempt = 0; attempt < 20; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0));
    beaconActive = await (await reloaded.context.fetch("/pdf/api/active-reading")).json();
    if (beaconActive.active?.pos === 6) break;
  }
  assert.equal(beaconActive.active.pos, 6);
  assert.equal(reloaded.gatewayMessages.length, 1);
  assert.equal(reloaded.gatewayMessages[0].path, "/pdf/api/active-reading");
  assert.equal(JSON.parse(reloaded.gatewayMessages[0].body).pos, 6);
});

test("native read-dwell beacon starts the authorized Pi request before page suspension", async () => {
  const result = await harness();
  const file = "localbook-" + "b".repeat(64);
  const body = JSON.stringify({ file, dwell: [{ page: 7, secs: 12 }] });
  assert.equal(
    result.context.navigator.sendBeacon("/pdf/api/read-dwell", body),
    true,
  );
  for (let attempt = 0; attempt < 20 && result.gatewayMessages.length === 0; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  assert.equal(result.gatewayMessages.length, 1);
  assert.equal(result.gatewayMessages[0].path, "/pdf/api/read-dwell");
  assert.equal(result.gatewayMessages[0].method, "POST");
  assert.deepEqual(JSON.parse(result.gatewayMessages[0].body), {
    file,
    dwell: [{ page: 7, secs: 12 }],
  });
});

test("native focus and bounded journal keep monotonic cursors across reload without Pi", async () => {
  const clock = fakeClock();
  const first = await harness({ Date: clock.Date });
  await enableNativeContext(first);
  const file = "localbook-" + "b".repeat(64);
  const postFocus = (body) => first.context.fetch("/pdf/api/outgoing/focus", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  assert.equal((await postFocus({
    kind: "text", ref: { file, page: 8, text: "selected" }, task: "read",
  })).status, 200);
  clock.advance(100);
  assert.equal((await postFocus({ cancel: true, task: "read" })).status, 200);
  const state = await (await first.context.fetch("/pdf/api/outgoing/state")).json();
  assert.equal(state.focus.state, "cancelled");
  assert.equal(state.focus.cancelledObject.ref.text, "selected");
  const journal = await (await first.context.fetch(
    "/pdf/api/outgoing/journal?since=0&limit=20&wait=0",
  )).json();
  assert.deepEqual(journal.events.map((event) => event.seq), [1, 2]);
  assert.deepEqual(journal.events.map((event) => event.action), ["set", "cancel"]);
  assert.equal(journal.cursor, 2);
  assert.equal(first.gatewayMessages.length, 1);
  assert.equal(first.gatewayMessages[0].path, "/pdf/api/context-sync");

  const reloaded = await harness({
    Date: clock.Date,
    localStorageState: first.localStorageState,
    dataStoresState: first.dataStoresState,
  });
  const third = await reloaded.context.fetch("/pdf/api/outgoing/focus", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind: "card", ref: { id: "card-1" } }),
  });
  assert.equal((await third.json()).seq, 3);
  const resumed = await (await reloaded.context.fetch(
    "/pdf/api/outgoing/journal?since=1&limit=2&wait=0",
  )).json();
  assert.deepEqual(resumed.events.map((event) => event.seq), [2, 3]);
  assert.equal(resumed.cursor, 3);
  const denied = await (await reloaded.context.fetch(
    "/pdf/api/outgoing/journal?since=3&limit=20&wait=20",
  )).json();
  assert.equal(denied.waitDenied, true);
  assert.equal(reloaded.gatewayMessages.length, 0);
});

test("native local page context joins the same monotonic journal without Pi", async () => {
  const result = await harness();
  await enableNativeContext(result);
  const runtime = result.context.BWReaderRuntime.nativeLocalRuntime;
  const file = "localbook-" + "b".repeat(64);
  const published = await runtime.publishPageContext({
    kind: "pdf",
    file,
    page: 12,
    title: "Local PDF",
    text: "【当前显示区域之前】\n前文\n\n【当前显示区域（重点）】\n当前文字\n\n【当前显示区域之后】\n后文",
    textAvailable: true,
    textSource: "app-local-visible-window",
    fallbackReason: null,
    truncated: false,
  });
  assert.equal(published.ok, true);
  assert.equal(published.seq, 1);

  const focus = await result.context.fetch("/pdf/api/outgoing/focus", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      kind: "text",
      ref: { file, page: 12, text: "当前文字" },
      task: "selection",
    }),
  });
  assert.equal(focus.status, 200);
  const journal = await (await result.context.fetch(
    "/pdf/api/outgoing/journal?since=0&limit=20&wait=0",
  )).json();
  assert.deepEqual(journal.events.map((event) => event.seq), [1, 2]);
  assert.deepEqual(journal.events.map((event) => event.type), [
    "page.context",
    "focus",
  ]);
  assert.equal(journal.events[0].file, file);
  assert.equal(journal.events[0].page, 12);
  assert.equal(
    journal.events[0].page_context.text_source,
    "app-local-visible-window",
  );
  assert.match(journal.events[0].page_context.text, /当前显示区域（重点）/);
  assert.equal(result.gatewayMessages.length, 1);
  assert.equal(result.gatewayMessages[0].path, "/pdf/api/context-sync");

  assert.throws(
    () => runtime.publishPageContext({
      kind: "pdf",
      file: "localbook-foreign",
      page: 12,
      title: "Foreign",
      text: "x",
      textAvailable: true,
      textSource: "app-local-visible-window",
      fallbackReason: null,
      truncated: false,
    }),
    (error) => error?.code === "BW_LOCAL_OUTGOING_PAGE_CONTEXT",
  );
});

test("native drawing derives a revision-only stable edge from local PDF and EPUB ink", async () => {
  const clock = fakeClock();
  const first = await harness({ Date: clock.Date });
  await enableNativeContext(first);
  const file = "localbook-" + "b".repeat(64);
  await first.context.fetch("/pdf/api/ink", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file: DEFAULT_LOCAL_FILE, page: 7, strokes: [{ t: "pen", p: [[1, 2], [3, 4]] }] }),
  });
  const drawingURL = "/pdf/api/outgoing/drawing?file=" + encodeURIComponent(file) + "&page=7";
  const pending = await (await first.context.fetch(drawingURL)).json();
  assert.equal(pending.stable, false);
  assert.equal(pending.inProgress, true);
  assert.equal(pending.drawingRevision, null);
  assert.equal(pending.artifact, "revision-only");
  assert.equal(pending.compositeAvailable, false);
  clock.advance(1_001);
  const stable = await (await first.context.fetch(drawingURL)).json();
  assert.equal(stable.stable, true);
  assert.match(stable.drawingRevision, /^dr_[0-9a-f]{16}$/);
  assert.equal(stable.ref.revision, stable.drawingRevision);
  assert.equal(stable.compositeAvailable, false);
  const journal = await (await first.context.fetch(
    "/pdf/api/outgoing/journal?since=0&limit=20&wait=0",
  )).json();
  assert.equal(journal.events.length, 1);
  assert.equal(journal.events[0].type, "drawing");
  assert.equal(journal.events[0].revisionOnly, true);
  await first.context.fetch(drawingURL);
  assert.equal((await (await first.context.fetch(
    "/pdf/api/outgoing/journal?since=0&limit=20&wait=0",
  )).json()).events.length, 1);

  const epub = await harness({ Date: clock.Date, surface: "epub" });
  await enableNativeContext(epub);
  await epub.context.fetch("/pdf/api/epub-ink", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file: DEFAULT_LOCAL_FILE, idx: 9, strokes: [{ t: "region", id: "r1" }] }),
  });
  const epubURL = "/pdf/api/outgoing/drawing?file=" + encodeURIComponent(file) + "&page=9";
  assert.equal((await (await epub.context.fetch(epubURL)).json()).inProgress, true);
  clock.advance(1_001);
  assert.equal((await (await epub.context.fetch(epubURL)).json()).stable, true);
  const reloaded = await harness({
    Date: clock.Date,
    localStorageState: first.localStorageState,
    dataStoresState: first.dataStoresState,
  });
  assert.equal((await (await reloaded.context.fetch(drawingURL)).json()).stable, true);
  assert.equal(reloaded.gatewayMessages.length, 0);
});

test("native outgoing compatibility routes fail closed and expose invalid input or corrupt state", async () => {
  const disabled = await harness();
  const file = "localbook-" + "b".repeat(64);
  for (const request of [
    disabled.context.fetch("/pdf/api/active-reading"),
    disabled.context.fetch("/pdf/api/outgoing/journal"),
    disabled.context.fetch("/pdf/api/outgoing/state"),
    disabled.context.fetch("/pdf/api/outgoing/drawing?file=" + encodeURIComponent(file)),
    disabled.context.fetch("/pdf/api/outgoing/focus", { method: "POST" }),
  ]) {
    const response = await request;
    assert.equal(response.status, 409);
    assert.equal((await response.json()).code, "BW_LOCAL_CONTEXT_SYNC_DISABLED");
  }
  assert.equal(disabled.gatewayMessages.length, 0);

  await enableNativeContext(disabled);
  const badActive = await disabled.context.fetch("/pdf/api/active-reading", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind: "pdf", file, pos: 1, unexpected: true }),
  });
  assert.equal(badActive.status, 400);
  assert.match((await badActive.json()).error, /未知字段/);
  assert.equal((await disabled.context.fetch(
    "/pdf/api/outgoing/journal?since=oops",
  )).status, 400);
  assert.equal((await disabled.context.fetch(
    "/pdf/api/outgoing/drawing?file=localbook-wrong",
  )).status, 400);
  assert.equal((await disabled.context.fetch("/pdf/api/outgoing/focus", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind: "text", ref: { text: "x" }, extra: true }),
  })).status, 400);
  assert.equal((await disabled.context.fetch("/pdf/api/outgoing/state", {
    method: "POST",
  })).status, 405);

  await disabled.context.fetch("/pdf/api/outgoing/focus", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ kind: "card", ref: { id: "card-corrupt" } }),
  });
  const journalEntry = [...disabled.dataStoresState.device.values.entries()]
    .find(([key]) => key.startsWith("native-outgoing-journal:"));
  assert.ok(journalEntry);
  journalEntry[1].value.payload.unknown = true;
  const corrupt = await disabled.context.fetch("/pdf/api/outgoing/journal");
  assert.equal(corrupt.status, 500);
  assert.equal((await corrupt.json()).code, "BW_LOCAL_OUTGOING_JOURNAL_CORRUPT");
  assert.equal(disabled.gatewayMessages.length, 1);
  assert.equal(disabled.gatewayMessages[0].path, "/pdf/api/context-sync");
});

test("native local notes retain offline card placement payloads", async () => {
  const { context } = await harness();
  const card = {
    id: "card-42",
    cid: "card-42",
    gid: "card-42",
  };
  const put = await context.fetch("/pdf/api/notes", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      file: DEFAULT_LOCAL_FILE,
      id: "c_0123456789abcdef",
      anchor: { kind: "pdf", page: 7, x: 0.25, y: 0.4 },
      card,
    }),
  });
  assert.equal(put.status, 200);
  const get = await context.fetch("/pdf/api/notes?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE));
  const payload = await get.json();
  assert.equal(payload.notes.length, 1);
  assert.equal(payload.notes[0].id, "c_0123456789abcdef");
  assert.equal(payload.notes[0].card.gid, card.gid);
});

function canvasRecorder() {
  const operations = [];
  const ctx = {
    fillStyle: "",
    strokeStyle: "",
    lineWidth: 0,
    lineCap: "",
    lineJoin: "",
    font: "",
    textBaseline: "",
    fillRect(x, y, width, height) {
      operations.push({ op: "fillRect", color: this.fillStyle, x, y, width, height });
    },
    measureText(text) { return { width: Array.from(String(text)).length * 18 }; },
    fillText(text, x, y) {
      operations.push({ op: "fillText", color: this.fillStyle, text, x, y });
    },
    beginPath() { operations.push({ op: "beginPath" }); },
    moveTo(x, y) { operations.push({ op: "moveTo", x, y }); },
    lineTo(x, y) { operations.push({ op: "lineTo", x, y }); },
    stroke() {
      operations.push({
        op: "stroke", color: this.strokeStyle, width: this.lineWidth,
        cap: this.lineCap, join: this.lineJoin,
      });
    },
    arc(x, y, radius) { operations.push({ op: "arc", x, y, radius }); },
    fill() { operations.push({ op: "fill", color: this.fillStyle }); },
  };
  const canvas = {
    width: 0,
    height: 0,
    getContext(kind) { return kind === "2d" ? ctx : null; },
    toDataURL(kind) {
      operations.push({ op: "toDataURL", kind, width: this.width, height: this.height });
      return "data:image/png;base64,cG5n";
    },
  };
  return { canvas, operations };
}

test("native note composite renders wrapped text and every multi-color, multi-width stroke at 2x without Pi", async () => {
  for (const surface of ["pdf", "epub"]) {
    const recorder = canvasRecorder();
    const result = await harness({ surface, canvasFactory: () => recorder.canvas });
    const file = "localbook:" + "localbook-" + "b".repeat(64);
    const note = {
      id: `note-composite-${surface}`,
      file,
      anchor: { kind: surface, page: 1, section: 0, x: 0.2, y: 0.3 },
      text: "第一行文字\n第二行文字",
      color: "#ffeeaa",
      w: 300,
      h: 100,
      iar: 1,
      strokes: [
        { c: "#ff0000", w: 2, pts: [[0, 0], [1, 1]] },
        { c: "#0055ff", w: 5, pts: [[0.25, 0.75], [0.75, 0.25]] },
      ],
    };
    const saved = await result.context.fetch("/pdf/api/notes", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(note),
    });
    assert.equal(saved.status, 200);
    const savedPayload = await saved.json();

    const response = await result.context.fetch("/pdf/api/note-composite", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file, id: savedPayload.id }),
    });
    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), {
      ok: true,
      data_url: "data:image/png;base64,cG5n",
    });
    assert.deepEqual(
      recorder.operations.find((operation) => operation.op === "fillRect"),
      { op: "fillRect", color: "#ffeeaa", x: 0, y: 0, width: 600, height: 200 },
    );
    assert.deepEqual(
      recorder.operations.filter((operation) => operation.op === "stroke")
        .map(({ color, width, cap, join }) => ({ color, width, cap, join })),
      [
        { color: "#ff0000", width: 4, cap: "round", join: "round" },
        { color: "#0055ff", width: 10, cap: "round", join: "round" },
      ],
    );
    assert.deepEqual(
      recorder.operations.filter((operation) => operation.op === "fillText")
        .map((operation) => operation.text),
      ["第一行文字", "第二行文字"],
    );
    assert.deepEqual(
      recorder.operations.find((operation) => operation.op === "moveTo"),
      { op: "moveTo", x: 200, y: 0 },
      "iar=1 letterboxes the 600x200 canvas to a centered 200x200 ink box",
    );
    assert.deepEqual(
      recorder.operations.find((operation) => operation.op === "toDataURL"),
      { op: "toDataURL", kind: "image/png", width: 600, height: 200 },
    );
    assert.equal(result.gatewayMessages.length, 0);

    const missing = await result.context.fetch("/pdf/api/note-composite", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file, id: "missing" }),
    });
    assert.equal(missing.status, 404);
    assert.equal((await missing.json()).code, "BW_LOCAL_NOTE_COMPOSITE_NOT_FOUND");
    const extra = await result.context.fetch("/pdf/api/note-composite", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file, id: note.id, extra: true }),
    });
    assert.equal(extra.status, 400);
    assert.equal(result.gatewayMessages.length, 0);
  }
});

test("native book-figures preserves the old Pi-backed GET and POST semantics", async () => {
  const first = await harness({
    originalFetch(input) {
      const url = new URL(typeof input === "string" ? input : input.url);
      const enabled = url.pathname.endsWith("/" + "e".repeat(32));
      return Promise.resolve(new Response(JSON.stringify({ ok: true, enabled }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }));
    },
    gatewayReply(message) {
      return {
        contract: "reader-native-pi-response/2",
        streamURL: "http://127.0.0.1:43129/r/" + "a".repeat(64) +
          "/pi-proxy/" + (message.method === "GET" ? "d" : "e").repeat(32),
      };
    },
  });
  const file = "localbook:" + "localbook-" + "b".repeat(64);
  const initial = await first.context.fetch(
    "/pdf/api/book-figures?file=" + encodeURIComponent(file),
  );
  assert.equal(initial.status, 200);
  assert.deepEqual(await initial.json(), { ok: true, enabled: false });
  const enabled = await first.context.fetch("/pdf/api/book-figures", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file, enabled: true }),
  });
  assert.equal(enabled.status, 200);
  assert.deepEqual(await enabled.json(), { ok: true, enabled: true });
  assert.deepEqual(
    first.gatewayMessages.map(({ method, path, body }) => ({ method, path, body })),
    [
      { method: "GET", path: `/pdf/api/book-figures?file=${encodeURIComponent(file)}`, body: "" },
      { method: "POST", path: "/pdf/api/book-figures", body: JSON.stringify({ file, enabled: true }) },
    ],
  );
  assert.doesNotMatch(SOURCE, /book-figures-preference|processingStarted/);
});

test("native PDF mutation waits for an assistant SSE writer and starts only after stream cancel", async () => {
  let upstreamController = null;
  let upstreamCancelled = false;
  const result = await harness({
    interfaceManifest: withNativePDFAssistantAndMutationRoutesSupported(),
    pdfMutationReply: nativePDFMutationResponder({ pageCount: 4 }),
    originalFetch(input) {
      const raw = typeof input === "string" ? input : input.url;
      const url = new URL(raw, "http://127.0.0.1:43129");
      if (/\/pi-proxy\/[0-9a-f]{32}$/.test(url.pathname)) {
        const stream = new ReadableStream({
          start(controller) { upstreamController = controller; },
          cancel() { upstreamCancelled = true; },
        });
        return Promise.resolve(new Response(stream, {
          status: 200,
          headers: { "Content-Type": "text/event-stream; charset=utf-8" },
        }));
      }
      return Promise.resolve(new Response("not intercepted", { status: 418 }));
    },
  });

  const chat = await result.context.fetch("/api/assistant/chat", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: "hold", rid: "rid-pdf-barrier", context: {} }),
  });
  assert.equal(chat.status, 200);
  assert.ok(upstreamController, "assistant upstream stream must be open");
  const reader = chat.body.getReader();

  const started = await result.context.fetch("/pdf/api/pdf-insert-page", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file: DEFAULT_LOCAL_FILE, after: 1, md: "after SSE" }),
  });
  assert.equal(started.status, 200);
  const receipt = await started.json();
  for (let attempt = 0; attempt < 10; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  assert.deepEqual(
    result.pdfMutationMessages.map((message) => message.action),
    ["recover"],
    "the native prepare snapshot must wait while SSE still owns its writer lease",
  );

  await reader.cancel("test releases assistant writer");
  assert.equal(upstreamCancelled, true);
  await waitForPDFMutationAction(result.pdfMutationMessages, "prepare");
  const job = await waitForNativePDFJob(result.context, receipt.job_id);
  assert.equal(job.status, "done");
});

test("native PDF prepare failure releases the barrier so later writes and mutation can continue", async () => {
  const responder = nativePDFMutationResponder({ pageCount: 4 });
  let failFirstPrepare = true;
  const result = await harness({
    interfaceManifest: withNativePDFMutationRoutesSupported(),
    pdfMutationReply(message) {
      if (message.action === "prepare" && failFirstPrepare) {
        failFirstPrepare = false;
        throw new Error("prepare rejected for test");
      }
      return responder(message);
    },
  });

  const failed = await result.context.fetch("/pdf/api/pdf-insert-page", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file: DEFAULT_LOCAL_FILE, after: 1, md: "fail once" }),
  });
  const failedJob = await waitForNativePDFJob(
    result.context, (await failed.json()).job_id,
  );
  assert.equal(failedJob.status, "error");
  assert.match(failedJob.error, /prepare rejected for test/);

  const write = await result.context.fetch("/pdf/api/highlights", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      file: DEFAULT_LOCAL_FILE,
      page: 2,
      rects: [[1, 2, 3, 4]],
      text: "writer reopened",
    }),
  });
  assert.equal(write.status, 200);

  const retried = await result.context.fetch("/pdf/api/pdf-insert-page", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file: DEFAULT_LOCAL_FILE, after: 1, md: "retry" }),
  });
  const retriedJob = await waitForNativePDFJob(
    result.context, (await retried.json()).job_id,
  );
  assert.equal(retriedJob.status, "done");
});

test("native PDF insert, edit and delete keep the legacy job API and migrate every App-owned page anchor", async () => {
  const responder = nativePDFMutationResponder({ pageCount: 4 });
  const result = await harness({
    interfaceManifest: withNativePDFMutationRoutesSupported(),
    pdfMutationReply: responder,
  });
  const { context } = result;

  assert.equal((await context.fetch("/pdf/api/highlights", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      file: DEFAULT_LOCAL_FILE,
      page: 2,
      rects: [[1, 2, 20, 30]],
      text: "anchored highlight",
    }),
  })).status, 200);
  assert.equal((await context.fetch("/pdf/api/ink", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      file: DEFAULT_LOCAL_FILE,
      page: 2,
      strokes: [{ t: "pen", c: "#f00", w: 2, p: [[0, 0], [1, 1]] }],
    }),
  })).status, 200);
  assert.equal((await context.fetch("/pdf/api/notes", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      file: DEFAULT_LOCAL_FILE,
      anchor: { kind: "pdf", page: 2, x: 0.2, y: 0.3 },
      text: "anchored note",
      card: { id: "card-pdf-page", cid: "card-pdf-page", gid: "card-pdf-page" },
    }),
  })).status, 200);
  assert.equal((await context.fetch("/pdf/api/reading-pos", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file: DEFAULT_LOCAL_FILE, kind: "pdf", pos: 2 }),
  })).status, 200);

  const inserted = await context.fetch("/pdf/api/pdf-insert-page", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      file: DEFAULT_LOCAL_FILE,
      after: 1,
      title: "Inserted title",
      md: "Selectable body",
    }),
  });
  assert.equal(inserted.status, 200);
  const insertReceipt = await inserted.json();
  assert.equal(insertReceipt.ok, true);
  assert.match(insertReceipt.job_id, /^npj_[a-f0-9]{24}$/);
  const insertJob = await waitForNativePDFJob(context, insertReceipt.job_id);
  assert.equal(insertJob.status, "done");
  assert.deepEqual(insertJob.result, {
    ok: true,
    mode: "insert",
    warnings: [
      "本机 OCR、分词与公式页已随 PDF 页号迁移；新插入页或改写页可按需重新预处理",
      "Pi 页锚数据保留在旧内容摘要下；上传/同步新 PDF 前，联网页锚接口会拒绝旧绑定",
    ],
    mtime: 1_800_000_000,
    page: 2,
  });

  const highlightsAfterInsert = await (await context.fetch(
    "/pdf/api/highlights?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE),
  )).json();
  assert.equal(highlightsAfterInsert.highlights[0].page, 3);
  const inkAfterInsert = await (await context.fetch(
    "/pdf/api/ink?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE),
  )).json();
  assert.deepEqual(Object.keys(inkAfterInsert.pages), ["3"]);
  const notesAfterInsert = await (await context.fetch(
    "/pdf/api/notes?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE),
  )).json();
  assert.equal(notesAfterInsert.notes[0].anchor.page, 3);
  const positionsAfterInsert = await (await context.fetch(
    "/pdf/api/reading-pos",
  )).json();
  assert.equal(positionsAfterInsert.positions[DEFAULT_LOCAL_FILE].pos, 3);
  let userPages = await (await context.fetch(
    "/pdf/api/userpages?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE),
  )).json();
  const realPage = userPages.pages.find((item) => Number.isInteger(item.page));
  assert.ok(realPage);
  assert.equal(realPage.page, 2);
  assert.equal(realPage.mode, "overlay");

  const textUpdate = await context.fetch("/pdf/api/userpages", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      file: DEFAULT_LOCAL_FILE,
      id: realPage.id,
      title: "Edited title",
      md: "Edited selectable body",
    }),
  });
  assert.equal(textUpdate.status, 200);
  const edited = await context.fetch("/pdf/api/pdf-insert-page", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file: DEFAULT_LOCAL_FILE, id: realPage.id }),
  });
  const editReceipt = await edited.json();
  assert.equal(editReceipt.ok, true);
  assert.equal((await waitForNativePDFJob(context, editReceipt.job_id)).status, "done");
  userPages = await (await context.fetch(
    "/pdf/api/userpages?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE),
  )).json();
  const editedPage = userPages.pages.find((item) => item.id === realPage.id);
  assert.equal(editedPage.title, "Edited title");
  assert.equal(editedPage.md, "Edited selectable body");
  assert.equal(editedPage.synced_ver, editedPage.md_ver);

  const removed = await context.fetch(
    "/pdf/api/pdf-insert-page?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE) +
      "&id=" + encodeURIComponent(realPage.id),
    { method: "DELETE" },
  );
  const deleteReceipt = await removed.json();
  assert.equal(deleteReceipt.ok, true);
  assert.equal((await waitForNativePDFJob(context, deleteReceipt.job_id)).status, "done");
  userPages = await (await context.fetch(
    "/pdf/api/userpages?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE),
  )).json();
  assert.equal(userPages.pages.some((item) => item.id === realPage.id), false);
  const highlightsAfterDelete = await (await context.fetch(
    "/pdf/api/highlights?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE),
  )).json();
  assert.equal(highlightsAfterDelete.highlights[0].page, 2);
  const notesAfterDelete = await (await context.fetch(
    "/pdf/api/notes?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE),
  )).json();
  assert.equal(notesAfterDelete.notes[0].anchor.page, 2);
  assert.deepEqual(
    result.pdfMutationMessages.map((message) => message.action),
    [
      "recover", "prepare", "commit", "finalize", "recover",
      "prepare", "commit", "finalize", "recover",
      "prepare", "commit", "finalize", "recover",
    ],
  );
  assert.equal(result.gatewayMessages.length, 0);
});

test("native PDF commit failure rolls every local page anchor back and reports job error", async () => {
  const result = await harness({
    interfaceManifest: withNativePDFMutationRoutesSupported(),
    pdfMutationReply: nativePDFMutationResponder({ pageCount: 4, failCommit: true }),
  });
  const { context } = result;
  await context.fetch("/pdf/api/highlights", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      file: DEFAULT_LOCAL_FILE,
      page: 2,
      rects: [[1, 2, 20, 30]],
      text: "must survive rollback",
    }),
  });
  const response = await context.fetch("/pdf/api/pdf-insert-page", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file: DEFAULT_LOCAL_FILE, after: 1, md: "temporary" }),
  });
  const receipt = await response.json();
  const job = await waitForNativePDFJob(context, receipt.job_id);
  assert.equal(job.status, "error");
  assert.match(job.error, /native replace failed/);
  const highlights = await (await context.fetch(
    "/pdf/api/highlights?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE),
  )).json();
  assert.equal(highlights.highlights[0].page, 2);
  const pages = await (await context.fetch(
    "/pdf/api/userpages?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE),
  )).json();
  assert.equal(pages.pages.length, 0);
  assert.deepEqual(
    result.pdfMutationMessages.map((message) => message.action),
    ["recover", "prepare", "commit", "recover"],
  );
  assert.equal(result.gatewayMessages.length, 0);
});

test("native PDF restart recovery rolls back durable IndexedDB anchors when the process dies before byte commit", async () => {
  const dataStoresState = {
    global: { values: new Map(), revision: 0 },
    document: { values: new Map(), revision: 0 },
    device: { values: new Map(), revision: 0 },
  };
  const durableState = {
    pageCount: 4,
    contentSHA256: "a".repeat(64),
    journal: null,
  };
  const first = await harness({
    dataStoresState,
    interfaceManifest: withNativePDFMutationRoutesSupported(),
    pdfMutationReply: nativePDFMutationResponder({
      durableState,
      hangCommit: true,
    }),
  });
  await first.context.fetch("/pdf/api/highlights", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      file: DEFAULT_LOCAL_FILE,
      page: 2,
      rects: [[1, 2, 20, 30]],
      text: "survives process death",
    }),
  });
  const started = await first.context.fetch("/pdf/api/pdf-insert-page", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file: DEFAULT_LOCAL_FILE, after: 1, md: "pending" }),
  });
  assert.equal(started.status, 200);
  await waitForPDFMutationAction(first.pdfMutationMessages, "commit");

  const fenced = await first.context.fetch("/pdf/api/highlights", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      file: DEFAULT_LOCAL_FILE,
      page: 4,
      rects: [[1, 1, 2, 2]],
      text: "must be fenced",
    }),
  });
  assert.equal(fenced.status, 409);
  assert.equal((await fenced.json()).code, "BW_NATIVE_PDF_MUTATION_BUSY");
  const piFenced = await first.context.fetch(
    "/pdf/api/book-figures?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE),
  );
  assert.equal(piFenced.status, 409);
  assert.equal((await piFenced.json()).code, "BW_NATIVE_PDF_MUTATION_BUSY");
  assert.equal(first.gatewayMessages.length, 0);

  const restarted = await harness({
    dataStoresState,
    interfaceManifest: withNativePDFMutationRoutesSupported(),
    pdfMutationReply: nativePDFMutationResponder({ durableState }),
  });
  const highlights = await (await restarted.context.fetch(
    "/pdf/api/highlights?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE),
  )).json();
  assert.equal(highlights.highlights.length, 1);
  assert.equal(highlights.highlights[0].page, 2);
  const journal = await restarted.documentStore.get(
    "native-pdf-mutation-journal",
    DEFAULT_LOCAL_BOOK_ID + ":pdf-mutation-journal",
  );
  assert.equal(journal, null);
  assert.equal(durableState.contentSHA256, "a".repeat(64));
  assert.equal((await restarted.context.fetch("/pdf/api/highlights", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      file: DEFAULT_LOCAL_FILE,
      page: 4,
      rects: [[1, 1, 2, 2]],
      text: "gate reopened",
    }),
  })).status, 200);
});

test("native PDF journals isolate two books in one library directory and later recover the interrupted book", async () => {
  const dataStoresState = {
    global: { values: new Map(), revision: 0 },
    document: { values: new Map(), revision: 0 },
    device: { values: new Map(), revision: 0 },
  };
  const bookA = DEFAULT_LOCAL_BOOK_ID;
  const fileA = "localbook:" + bookA;
  const bookB = "localbook-" + "c".repeat(64);
  const fileB = "localbook:" + bookB;
  const durableA = {
    pageCount: 4,
    contentSHA256: "a".repeat(64),
    journal: null,
  };
  const durableB = {
    pageCount: 3,
    contentSHA256: "a".repeat(64),
    journal: null,
  };

  const firstA = await harness({
    bookId: bookA,
    dataStoresState,
    interfaceManifest: withNativePDFMutationRoutesSupported(),
    pdfMutationReply: nativePDFMutationResponder({
      durableState: durableA,
      hangCommit: true,
    }),
  });
  assert.equal((await firstA.context.fetch("/pdf/api/pdf-insert-page", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file: fileA, after: 1, md: "pending A" }),
  })).status, 200);
  await waitForPDFMutationAction(firstA.pdfMutationMessages, "commit");
  assert.equal(durableA.journal.phase, "staged");

  const openedB = await harness({
    bookId: bookB,
    dataStoresState,
    interfaceManifest: withNativePDFMutationRoutesSupported(),
    pdfMutationReply: nativePDFMutationResponder({ durableState: durableB }),
  });
  assert.equal(openedB.pdfMutationMessages[0].action, "recover");
  assert.equal(openedB.pdfMutationMessages[0].localBookId, bookB);
  assert.equal(durableA.journal.phase, "staged");
  assert.notEqual(await openedB.documentStore.get(
    "native-pdf-mutation-journal",
    bookA + ":pdf-mutation-journal",
  ), null);
  assert.equal(await openedB.documentStore.get(
    "native-pdf-mutation-journal",
    bookB + ":pdf-mutation-journal",
  ), null);

  const startedB = await openedB.context.fetch("/pdf/api/pdf-insert-page", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file: fileB, after: 1, md: "independent B" }),
  });
  assert.equal(startedB.status, 200);
  const jobB = await waitForNativePDFJob(openedB.context, (await startedB.json()).job_id);
  assert.equal(jobB.status, "done");
  assert.equal(durableB.contentSHA256, "b".repeat(64));
  assert.equal(durableB.journal, null);
  assert.equal(durableA.journal.phase, "staged");

  const restoredA = await harness({
    bookId: bookA,
    dataStoresState,
    interfaceManifest: withNativePDFMutationRoutesSupported(),
    pdfMutationReply: nativePDFMutationResponder({ durableState: durableA }),
  });
  assert.equal(durableA.contentSHA256, "a".repeat(64));
  assert.equal(durableA.journal, null);
  assert.equal(await restoredA.documentStore.get(
    "native-pdf-mutation-journal",
    bookA + ":pdf-mutation-journal",
  ), null);
});

test("native PDF restart recovery completes durable anchors after native committed tombstone", async () => {
  const dataStoresState = {
    global: { values: new Map(), revision: 0 },
    document: { values: new Map(), revision: 0 },
    device: { values: new Map(), revision: 0 },
  };
  const durableState = {
    pageCount: 4,
    contentSHA256: "a".repeat(64),
    journal: null,
  };
  const first = await harness({
    dataStoresState,
    interfaceManifest: withNativePDFMutationRoutesSupported(),
    pdfMutationReply: nativePDFMutationResponder({
      durableState,
      hangCommittedRecover: true,
    }),
  });
  await first.context.fetch("/pdf/api/highlights", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      file: DEFAULT_LOCAL_FILE,
      page: 2,
      rects: [[1, 2, 20, 30]],
      text: "committed anchor",
    }),
  });
  await first.context.fetch("/pdf/api/pdf-insert-page", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file: DEFAULT_LOCAL_FILE, after: 1, md: "committed" }),
  });
  await waitForPDFMutationAction(first.pdfMutationMessages, "recover", 2);
  assert.equal(durableState.journal.phase, "committed");

  const restarted = await harness({
    dataStoresState,
    interfaceManifest: withNativePDFMutationRoutesSupported(),
    pdfMutationReply: nativePDFMutationResponder({ durableState }),
  });
  const highlights = await (await restarted.context.fetch(
    "/pdf/api/highlights?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE),
  )).json();
  assert.equal(highlights.highlights.length, 1);
  assert.equal(highlights.highlights[0].page, 3);
  assert.equal(durableState.contentSHA256, "b".repeat(64));
  assert.equal(durableState.journal, null);
  const journal = await restarted.documentStore.get(
    "native-pdf-mutation-journal",
    DEFAULT_LOCAL_BOOK_ID + ":pdf-mutation-journal",
  );
  assert.equal(journal, null);
});

test("PageTextProvider feeds embedded PDF text into the existing page-chars route", async () => {
  const { context, pageTextMessages } = await harness();
  const provider = context.BWReaderRuntime.pageTextProvider;
  assert.equal(provider.contract, "reader-page-text-provider/1");
  provider.registerEmbeddedPage(1, {
    pageWidth: 600,
    pageHeight: 800,
    revision: "embedded-fixture-1",
    chars: [
      { c: "本", x0: 10, y0: 20, x1: 20, y1: 32, w: 17, bk: 2, sp: false },
      { c: "文", x0: 20, y0: 20, x1: 30, y1: 32, w: 17, bk: 2, sp: false },
    ],
  });
  const response = await context.fetch("/pdf/api/page-chars?page=1");
  const payload = await response.json();
  assert.equal(response.status, 200);
  assert.equal(payload.ok, true);
  assert.equal(payload.state, "ready");
  assert.equal(payload.source, "embedded");
  assert.deepEqual(payload.chars.map((item) => item.w), [17, 17]);
  assert.equal(payload.page_w, 600);
  assert.equal(payload.page_h, 800);
  assert.equal(pageTextMessages.length, 0);
});

test("embedded PDF text drops isolated bad glyphs but distinguishes corrupt and empty pages", async () => {
  const { context } = await harness();
  const provider = context.BWReaderRuntime.pageTextProvider;

  const mixed = provider.registerEmbeddedPage(21, {
    pageWidth: 600,
    pageHeight: 800,
    revision: "embedded-mixed-glyphs",
    chars: [
      { c: "保", x0: 10, y0: 20, x1: 20, y1: 32, w: 3, bk: 1, sp: false },
      { c: "坏", x0: 30, y0: 20, x1: 10, y1: 32, w: 3, bk: 1, sp: false },
      { c: "留", x0: 20, y0: 20, x1: 30, y1: 32, w: 3, bk: 1, sp: false },
    ],
  });
  assert.equal(mixed.state, "ready");
  assert.equal(mixed.chars.map((item) => item.c).join(""), "保留");

  assert.throws(
    () => provider.registerEmbeddedPage(22, {
      pageWidth: 600,
      pageHeight: 800,
      revision: "embedded-all-corrupt",
      chars: [
        { c: "坏", x0: 30, y0: 20, x1: 10, y1: 32 },
        { c: "", x0: 10, y0: 20, x1: 20, y1: 32 },
      ],
    }),
    (error) => error?.code === "BW_PAGE_TEXT_EMBEDDED_INVALID",
  );

  const empty = provider.registerEmbeddedPage(23, {
    pageWidth: 600,
    pageHeight: 800,
    revision: "embedded-genuinely-empty",
    chars: [],
  });
  assert.equal(empty.state, "readyEmpty");
  assert.equal(empty.chars.length, 0);

  for (const [offset, malformedChars] of [undefined, null, false, {}].entries()) {
    const candidate = {
      pageWidth: 600,
      pageHeight: 800,
      revision: `embedded-malformed-chars-${offset}`,
    };
    if (malformedChars !== undefined) candidate.chars = malformedChars;
    assert.throws(
      () => provider.registerEmbeddedPage(24 + offset, candidate),
      (error) => error?.code === "BW_PAGE_TEXT_EMBEDDED_INVALID",
      "only an explicit empty array may become readyEmpty",
    );
  }
});

test("embedded PDF text returns immediately while native formula prefetch is hung", async () => {
  const { context, pageTextMessages, gatewayMessages, gateEvents } = await harness({
    pageTextReply() {
      return new Promise(() => {});
    },
  });
  const gateBaseline = [...gateEvents];
  context.BWReaderRuntime.pageTextProvider.registerEmbeddedPage(11, {
    pageWidth: 600,
    pageHeight: 800,
    revision: "embedded-immediate-1",
    chars: [
      { c: "可", x0: 10, y0: 20, x1: 20, y1: 32, w: 1, bk: 1, sp: false },
      { c: "选", x0: 20, y0: 20, x1: 30, y1: 32, w: 1, bk: 1, sp: false },
    ],
  });

  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error("embedded page-chars waited for native reply")), 100);
  });
  try {
    const payload = await Promise.race([
      context.fetch("/pdf/api/page-chars?page=11").then((response) => response.json()),
      timeout,
    ]);
    assert.equal(payload.state, "ready");
    assert.equal(payload.source, "embedded");
    assert.equal(payload.chars.map((item) => item.c).join(""), "可选");
  } finally {
    clearTimeout(timer);
  }
  assert.deepEqual(gateEvents, gateBaseline, "passive page text must not mutate local state");
  assert.equal(gatewayMessages.length, 0);
  assert.equal(pageTextMessages.length, 1);
  assert.equal(pageTextMessages[0].action, "page-chars");
  assert.equal(Object.hasOwn(pageTextMessages[0], "start"), false);
  assert.equal(Object.hasOwn(pageTextMessages[0], "engine"), false);
});

test("passive page text reads never turn native pending or failed into empty success", async (t) => {
  for (const state of ["pending", "failed"]) {
    await t.test(state, async () => {
      const { context, pageTextMessages } = await harness({
        pageTextReply(message) {
          return {
            contract: "reader-native-page-text-response/1",
            action: message.action,
            requestId: message.requestId,
            ok: false,
            state,
            source: "apple",
            revision: `${state}-fixture`,
            page: message.page,
            pageWidth: 0,
            pageHeight: 0,
            chars: null,
            formulaRegions: [],
            error: {
              code: state === "pending" ? "BW_PAGE_TEXT_PENDING" : "BW_PAGE_TEXT_FAILED",
              message: state === "pending" ? "processing" : "failed",
              retryable: state === "pending",
            },
          };
        },
      });
      context.BWReaderRuntime.pageTextProvider.registerEmbeddedPage(2, {
        pageWidth: 600,
        pageHeight: 800,
        chars: [],
      });
      const response = await context.fetch("/pdf/api/page-chars?page=2");
      const payload = await response.json();
      assert.equal(payload.ok, false);
      assert.equal(payload.state, state);
      assert.equal(Object.hasOwn(payload, "chars"), false);
      assert.equal(response.status, state === "pending" ? 202 : 422);
      assert.equal(pageTextMessages.length, 1);
      assert.deepEqual(Object.keys(pageTextMessages[0]).sort(), [
        "action", "contract", "localBookId", "page", "requestId",
      ]);
      assert.equal(pageTextMessages[0].action, "page-chars");
      assert.equal(pageTextMessages[0].contract, "reader-native-page-text-request/1");
      assert.equal(Object.hasOwn(pageTextMessages[0], "engine"), false);
      assert.equal(Object.hasOwn(pageTextMessages[0], "start"), false);
    });
  }
});

test("native idle and pending page replies are transient and never poison the cache", async (t) => {
  for (const transientState of ["idle", "pending"]) {
    await t.test(transientState, async () => {
      let call = 0;
      const { context, pageTextMessages } = await harness({
        pageTextReply(message) {
          call += 1;
          const ready = call === 2;
          return {
            contract: "reader-native-page-text-response/1",
            action: message.action,
            requestId: message.requestId,
            ok: ready,
            state: ready ? "ready" : transientState,
            source: ready || transientState === "pending" ? "apple" : null,
            revision: ready ? "loaded-after-status-restore" : "0",
            page: message.page,
            pageWidth: ready ? 600 : 0,
            pageHeight: ready ? 800 : 0,
            chars: ready
              ? [{ c: "新", x0: 10, y0: 10, x1: 20, y1: 25, w: 1, bk: 1, sp: false }]
              : null,
            formulaRegions: [],
            error: null,
          };
        },
      });
      context.BWReaderRuntime.pageTextProvider.registerEmbeddedPage(12, {
        pageWidth: 600,
        pageHeight: 800,
        chars: [],
      });

      const first = await (await context.fetch("/pdf/api/page-chars?page=12")).json();
      const second = await (await context.fetch("/pdf/api/page-chars?page=12")).json();
      assert.equal(
        first.state,
        transientState === "idle" ? "readyEmpty" : "pending",
        "empty embedded text may mask only the transient idle state",
      );
      assert.equal(second.state, "ready");
      assert.equal(second.chars[0].c, "新");
      assert.equal(pageTextMessages.length, 2);
    });
  }
});

test("whole-layer readiness retries a native-only PDFKit page first seen as idle", async () => {
  let ready = false;
  const { context, pageTextMessages } = await harness({
    pageTextReply(message) {
      return {
        contract: "reader-native-page-text-response/1",
        action: message.action,
        requestId: message.requestId,
        ok: ready,
        state: ready ? "ready" : "idle",
        source: ready ? "apple" : null,
        revision: ready ? "pdfkit-identity-ready" : "0",
        page: message.page,
        pageWidth: ready ? 600 : 0,
        pageHeight: ready ? 800 : 0,
        chars: ready
          ? [{ c: "可", x0: 10, y0: 10, x1: 20, y1: 25, w: 1, bk: 1, sp: false }]
          : null,
        formulaRegions: [],
        error: null,
      };
    },
  });
  const first = await (await context.fetch("/pdf/api/page-chars?page=9")).json();
  assert.equal(first.state, "idle");

  const refresh = new Promise((resolve) => {
    context.addEventListener("bw:page-text-updated", (event) => {
      if (event.detail.page === 9) resolve(event.detail);
    });
  });
  ready = true;
  context.dispatchEvent(new context.CustomEvent("bw:native-page-text-updated", {
    detail: {
      contract: "reader-native-page-text-update/1",
      localBookId: "localbook-" + "b".repeat(64),
      page: null,
      state: "idle",
      source: null,
      revision: "content-identity-ready",
    },
  }));
  assert.equal((await refresh).page, 9);

  const second = await (await context.fetch("/pdf/api/page-chars?page=9")).json();
  assert.equal(second.state, "ready");
  assert.equal(second.chars[0].c, "可");
  assert.equal(pageTextMessages.length, 2);
});

test("imported Pi page text is consumed without starting Apple OCR", async () => {
  const { context, pageTextMessages } = await harness({
    pageTextReply(message) {
      return {
        contract: "reader-native-page-text-response/1",
        action: message.action,
        requestId: message.requestId,
        ok: true,
        state: "ready",
        source: "pi",
        revision: "pi-import-v4",
        page: message.page,
        pageWidth: 720,
        pageHeight: 960,
        chars: [
          { c: "読", x0: 30, y0: 40, x1: 45, y1: 60, w: 90, bk: 7, sp: false },
          { c: "書", x0: 45, y0: 40, x1: 60, y1: 60, w: 90, bk: 7, sp: false },
        ],
        furigana: [
          { x0: 30, y0: 40, x1: 60, y1: 60, rt: "どくしょ", wd: "読書" },
        ],
        wordSegmentation: "ready",
        characterGeometry: "exact",
        formulaCoverage: "complete",
        formulaRegions: [],
        error: null,
      };
    },
  });
  context.BWReaderRuntime.pageTextProvider.registerEmbeddedPage(3, {
    pageWidth: 720,
    pageHeight: 960,
    chars: [],
  });
  const payload = await (await context.fetch("/pdf/api/page-chars?page=3")).json();
  assert.equal(payload.source, "pi");
  assert.equal(payload.state, "ready");
  assert.equal(payload.chars.map((item) => item.c).join(""), "読書");
  assert.equal(payload.furigana[0].rt, "どくしょ");
  assert.equal(payload.word_segmentation, "ready");
  assert.equal(payload.character_geometry, "exact");
  assert.equal(payload.formula_coverage, "complete");
  assert.deepEqual(pageTextMessages.map((message) => message.action), ["page-chars"]);
});

test("cached pending and failed formula regions never downgrade embedded text", async () => {
  const { context } = await harness({
    pageTextReply(message) {
      const formulaState = message.page === 8 ? "failed" : "pending";
      return {
        contract: "reader-native-page-text-response/1",
        action: message.action,
        requestId: message.requestId,
        ok: false,
        state: "failed",
        source: "apple",
        revision: "formula-pending-1",
        page: message.page,
        pageWidth: 0,
        pageHeight: 0,
        chars: null,
        formulaRegions: [{
          id: "formula-1",
          x0: 100, y0: 100, x1: 200, y1: 150,
          state: formulaState,
          latex: "",
          multiline: false,
          error: formulaState === "failed"
            ? { code: "BW_FORMULA_FAILED", message: "formula unavailable", retryable: false }
            : null,
        }],
        error: { code: "BW_PAGE_TEXT_FAILED", message: "page OCR unavailable", retryable: false },
      };
    },
  });
  context.BWReaderRuntime.pageTextProvider.registerEmbeddedPage(4, {
    pageWidth: 600,
    pageHeight: 800,
    chars: [
      { c: "A", x0: 20, y0: 20, x1: 30, y1: 35, w: 1, bk: 1, sp: false },
      { c: "乱", x0: 120, y0: 110, x1: 140, y1: 130, w: 2, bk: 2, sp: false },
    ],
  });
  let resolvePage4;
  const page4Refresh = new Promise((resolve) => { resolvePage4 = resolve; });
  context.addEventListener("bw:page-text-updated", (event) => {
    if (event.detail.page === 4) resolvePage4(event.detail);
  });
  const immediate = await (await context.fetch("/pdf/api/page-chars?page=4")).json();
  assert.equal(immediate.state, "ready");
  assert.equal(immediate.chars.map((item) => item.c).join(""), "A乱");
  assert.deepEqual(immediate.formula_regions, []);
  await page4Refresh;
  const chars = await (await context.fetch("/pdf/api/page-chars?page=4")).json();
  assert.equal(chars.state, "ready");
  assert.equal(chars.chars.map((item) => item.c).join(""), "A乱");
  assert.equal(chars.formula_regions[0].state, "pending");
  context.BWReaderRuntime.pageTextProvider.registerEmbeddedPage(6, {
    pageWidth: 600,
    pageHeight: 800,
    chars: [
      { c: "乱", x0: 120, y0: 110, x1: 140, y1: 130, w: 2, bk: 2, sp: false },
    ],
  });
  let resolvePage6;
  const page6Refresh = new Promise((resolve) => { resolvePage6 = resolve; });
  context.addEventListener("bw:page-text-updated", (event) => {
    if (event.detail.page === 6) resolvePage6(event.detail);
  });
  const formulaOnlyImmediate = await context.fetch("/pdf/api/page-chars?page=6");
  const formulaOnlyImmediatePayload = await formulaOnlyImmediate.json();
  assert.equal(formulaOnlyImmediate.status, 200);
  assert.equal(formulaOnlyImmediatePayload.state, "ready");
  assert.equal(formulaOnlyImmediatePayload.chars[0].c, "乱");
  await page6Refresh;
  const formulaOnly = await context.fetch("/pdf/api/page-chars?page=6");
  const formulaOnlyPayload = await formulaOnly.json();
  assert.equal(formulaOnly.status, 200);
  assert.equal(formulaOnlyPayload.ok, true);
  assert.equal(formulaOnlyPayload.state, "ready");
  assert.equal(formulaOnlyPayload.chars[0].c, "乱");
  assert.equal(formulaOnlyPayload.formula_regions[0].state, "pending");

  context.BWReaderRuntime.pageTextProvider.registerEmbeddedPage(8, {
    pageWidth: 600,
    pageHeight: 800,
    chars: [
      { c: "原", x0: 120, y0: 110, x1: 140, y1: 130, w: 2, bk: 2, sp: false },
    ],
  });
  let resolvePage8;
  const page8Refresh = new Promise((resolve) => { resolvePage8 = resolve; });
  context.addEventListener("bw:page-text-updated", (event) => {
    if (event.detail.page === 8) resolvePage8(event.detail);
  });
  const failedImmediate = await (await context.fetch("/pdf/api/page-chars?page=8")).json();
  assert.equal(failedImmediate.state, "ready");
  assert.equal(failedImmediate.chars[0].c, "原");
  await page8Refresh;
  const failedFormula = await (await context.fetch("/pdf/api/page-chars?page=8")).json();
  assert.equal(failedFormula.state, "ready");
  assert.equal(failedFormula.chars[0].c, "原");
  assert.equal(failedFormula.formula_regions[0].state, "failed");
});

test("ready formulas preserve the existing fml/flx string contract", async () => {
  const latex = "\\frac{a}{b}";
  const { context } = await harness({
    pageTextReply(message) {
      return {
        contract: "reader-native-page-text-response/1",
        action: message.action,
        requestId: message.requestId,
        ok: false,
        state: "failed",
        source: "apple",
        revision: "formula-ready-1",
        page: message.page,
        pageWidth: 0,
        pageHeight: 0,
        chars: null,
        formulaRegions: [{
          id: "formula-ready",
          x0: 100, y0: 100, x1: 220, y1: 150,
          state: "ready",
          latex,
          multiline: false,
          error: null,
        }],
        error: { code: "BW_PAGE_TEXT_FAILED", message: "page OCR unavailable", retryable: false },
      };
    },
  });
  context.BWReaderRuntime.pageTextProvider.registerEmbeddedPage(7, {
    pageWidth: 600,
    pageHeight: 800,
    chars: [
      { c: "乱", x0: 120, y0: 110, x1: 140, y1: 130, w: 2, bk: 2, sp: false },
    ],
  });

  let resolveRefresh;
  const refresh = new Promise((resolve) => { resolveRefresh = resolve; });
  context.addEventListener("bw:page-text-updated", (event) => {
    if (event.detail.page === 7) resolveRefresh(event.detail);
  });
  const immediate = await (await context.fetch("/pdf/api/page-chars?page=7")).json();
  assert.equal(immediate.chars[0].c, "乱");
  await refresh;
  const payload = await (await context.fetch("/pdf/api/page-chars?page=7")).json();
  const formulaChars = payload.chars.filter((item) => item.fml);
  assert.equal(payload.state, "ready");
  assert.equal(formulaChars.length > 1, true);
  assert.equal(formulaChars[0].flx, latex);
  assert.equal(typeof formulaChars[0].flx, "string");
  assert.equal(formulaChars.slice(1).every((item) => item.flx === ""), true);
  assert.equal(formulaChars.every((item) => item.w === formulaChars[0].w), true);
  assert.equal(payload.chars.some((item) => item.c === "乱"), false);
});

test("native page text update invalidates passive cache and emits a char-layer refresh event", async () => {
  let state = "pending";
  const { context, pageTextMessages } = await harness({
    pageTextReply(message) {
      return {
        contract: "reader-native-page-text-response/1",
        action: message.action,
        requestId: message.requestId,
        ok: state === "ready",
        state,
        source: "apple",
        revision: state + "-revision",
        page: message.page,
        pageWidth: state === "ready" ? 600 : 0,
        pageHeight: state === "ready" ? 800 : 0,
        chars: state === "ready"
          ? [{ c: "好", x0: 10, y0: 10, x1: 20, y1: 25, w: 4, bk: 1, sp: false }]
          : null,
        formulaRegions: [],
        error: state === "ready" ? null : {
          code: "BW_PAGE_TEXT_PENDING", message: "processing", retryable: true,
        },
      };
    },
  });
  context.BWReaderRuntime.pageTextProvider.registerEmbeddedPage(5, {
    pageWidth: 600,
    pageHeight: 800,
    chars: [],
  });
  const refreshes = [];
  context.addEventListener("bw:page-text-updated", (event) => refreshes.push(event.detail));
  const pending = await (await context.fetch("/pdf/api/page-chars?page=5")).json();
  assert.equal(pending.state, "pending");
  state = "ready";
  context.dispatchEvent(new context.CustomEvent("bw:native-page-text-updated", {
    detail: {
      contract: "reader-native-page-text-update/1",
      localBookId: "localbook-" + "b".repeat(64),
      page: 5,
      state: "ready",
      source: "apple",
      revision: "ready-revision",
    },
  }));
  const ready = await (await context.fetch("/pdf/api/page-chars?page=5")).json();
  assert.equal(ready.state, "ready");
  assert.equal(ready.chars[0].c, "好");
  assert.equal(pageTextMessages.length, 2);
  assert.equal(refreshes.length, 1);
  assert.equal(refreshes[0].page, 5);
});

test("whole text-layer switch invalidates every loaded native page and reloads it", async () => {
  let character = "旧";
  const { context, pageTextMessages } = await harness({
    pageTextReply(message) {
      return {
        contract: "reader-native-page-text-response/1",
        action: message.action,
        requestId: message.requestId,
        ok: true,
        state: "ready",
        source: "pi",
        revision: `layer-${character}`,
        page: message.page,
        pageWidth: 600,
        pageHeight: 800,
        textAuthority: "local-override",
        chars: [{ c: character, x0: 10, y0: 10, x1: 20, y1: 25, w: 4, bk: 1, sp: false }],
        formulaRegions: [],
        error: null,
      };
    },
  });
  context.BWReaderRuntime.pageTextProvider.registerEmbeddedPage(6, {
    pageWidth: 600,
    pageHeight: 800,
    chars: [],
  });
  const first = await (await context.fetch("/pdf/api/page-chars?page=6")).json();
  assert.equal(first.chars[0].c, "旧");

  character = "新";
  context.dispatchEvent(new context.CustomEvent("bw:native-page-text-updated", {
    detail: {
      contract: "reader-native-page-text-update/1",
      localBookId: "localbook-" + "b".repeat(64),
      page: null,
      state: "ready",
      source: "pi",
      revision: "selected-layer-new",
    },
  }));
  const second = await (await context.fetch("/pdf/api/page-chars?page=6")).json();
  assert.equal(second.chars[0].c, "新");
  assert.equal(pageTextMessages.length, 2);
});

test("whole text-layer switch can replace a populated embedded PDF layer", async () => {
  const { context, pageTextMessages } = await harness({
    pageTextReply(message) {
      return nativePageReply(message, { text: "PC 文字层" });
    },
  });
  context.BWReaderRuntime.pageTextProvider.registerEmbeddedPage(7, {
    pageWidth: 600,
    pageHeight: 800,
    revision: "embedded-original",
    chars: [{ c: "PDF 原文字层", x0: 10, y0: 10, x1: 90, y1: 25, w: 4, bk: 1 }],
  });
  context.dispatchEvent(new context.CustomEvent("bw:native-page-text-updated", {
    detail: {
      contract: "reader-native-page-text-update/1",
      localBookId: "localbook-" + "b".repeat(64),
      page: null,
      state: "ready",
      source: "pi",
      revision: "selected-pc-layer",
    },
  }));
  const selected = await (await context.fetch("/pdf/api/page-chars?page=7")).json();
  assert.equal(selected.chars[0].c, "PC 文字层");
  assert.equal(selected.revision, "native-page-revision");
  assert.equal(pageTextMessages.length, 1);
});

test("a late pre-update page reply cannot overwrite the refreshed native cache", async () => {
  let firstResolve;
  let call = 0;
  const reply = (message, character, revision) => ({
    contract: "reader-native-page-text-response/1",
    action: message.action,
    requestId: message.requestId,
    ok: true,
    state: "ready",
    source: "apple",
    revision,
    page: message.page,
    pageWidth: 600,
    pageHeight: 800,
    chars: [{ c: character, x0: 10, y0: 10, x1: 20, y1: 25, w: 4, bk: 1, sp: false }],
    formulaRegions: [],
    error: null,
  });
  const { context, pageTextMessages } = await harness({
    pageTextReply(message) {
      call += 1;
      if (call === 1) {
        return new Promise((resolve) => {
          firstResolve = () => resolve(reply(message, "旧", "old-revision"));
        });
      }
      return reply(message, "新", "new-revision");
    },
  });
  context.BWReaderRuntime.pageTextProvider.registerEmbeddedPage(9, {
    pageWidth: 600,
    pageHeight: 800,
    chars: [],
  });

  const oldFetch = context.fetch("/pdf/api/page-chars?page=9");
  for (let attempt = 0; attempt < 10 && pageTextMessages.length < 1; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  assert.equal(pageTextMessages.length, 1);
  context.dispatchEvent(new context.CustomEvent("bw:native-page-text-updated", {
    detail: {
      contract: "reader-native-page-text-update/1",
      localBookId: "localbook-" + "b".repeat(64),
      page: 9,
      state: "ready",
      source: "apple",
      revision: "new-revision",
    },
  }));
  const fresh = await (await context.fetch("/pdf/api/page-chars?page=9")).json();
  firstResolve();
  await oldFetch;
  const cached = await (await context.fetch("/pdf/api/page-chars?page=9")).json();

  assert.equal(fresh.chars[0].c, "新");
  assert.equal(cached.chars[0].c, "新");
  assert.equal(pageTextMessages.length, 2);
});

test("embedded PDF search completes while passive native search is hung", async () => {
  const { context, pageTextMessages, gatewayMessages, gateEvents } = await harness({
    pageTextReply() {
      return new Promise(() => {});
    },
  });
  const gateBaseline = [...gateEvents];
  context.BWReaderRuntime.pageTextProvider.setEmbeddedPageLoader((page) => {
    const text = page === 1 ? "alpha target beta" : "other page";
    return {
      pageWidth: 600,
      pageHeight: 800,
      revision: `embedded-search-${page}`,
      chars: Array.from(text).map((c, index) => ({
        c, x0: 10 + index * 6, y0: 20, x1: 16 + index * 6, y1: 32,
        w: index, bk: 1, sp: c === " ",
      })),
    };
  }, 2);

  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error("embedded search waited for native reply")), 100);
  });
  try {
    const payload = await Promise.race([
      context.fetch("/pdf/api/search?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE) + "&q=target&limit=20").then((response) => response.json()),
      timeout,
    ]);
    assert.equal(payload.state, "ready");
    assert.equal(payload.incomplete, false);
    assert.equal(payload.total, 1);
    assert.equal(payload.pages, 1);
    assert.equal(payload.matches.length, 1);
    assert.equal(payload.matches[0].page, 1);
    assert.equal(payload.matches[0].count, 1);
    assert.equal(payload.matches[0].pos, 6);
    assert.match(payload.matches[0].snippet, /target/);
    assert.equal(payload.q, "target");
  } finally {
    clearTimeout(timer);
  }
  assert.deepEqual(gateEvents, gateBaseline, "passive search must not mutate local state");
  assert.equal(gatewayMessages.length, 0);
  assert.equal(pageTextMessages.length, 1);
  assert.equal(pageTextMessages[0].action, "search");
  assert.equal(Object.hasOwn(pageTextMessages[0], "start"), false);
  assert.equal(Object.hasOwn(pageTextMessages[0], "engine"), false);
});

test("page text status and search use read-only native actions only", async () => {
  const { context, pageTextMessages } = await harness({
    pageTextReply(message) {
      if (message.action === "status") {
        return {
          contract: "reader-native-page-text-response/1",
          action: message.action,
          requestId: message.requestId,
          ok: true,
          state: "pending",
          source: "apple",
          revision: "status-v1",
          progress: {
            total: 10, ready: 4, pending: 1, failed: 0, activePage: 5, currentPage: 5,
            textProgress: { total: 10, completed: 4, pending: 1, failed: 0, unavailable: 5 },
            wordProgress: { total: 10, completed: 3, pending: 1, failed: 0, unavailable: 6 },
            formulaProgress: { total: 10, completed: 2, pending: 2, failed: 1, unavailable: 5 },
            formulaPendingRegions: 3,
            formulaFailedRegions: 1,
          },
        };
      }
      return {
        contract: "reader-native-page-text-response/1",
        action: message.action,
        requestId: message.requestId,
        ok: true,
        state: "ready",
        source: "pi",
        revision: "search-v1",
        matches: [{ page: 7, count: 2, snippet: "target text" }],
        total: 2,
        pages: 1,
        incomplete: false,
      };
    },
  });
  const status = await (await context.fetch("/pdf/api/page-text-status")).json();
  assert.equal(status.progress.activePage, 5);
  assert.equal(status.progress.formulaProgress.pending, 2);
  assert.equal(status.progress.formulaPendingRegions, 3);
  const search = await (await context.fetch("/pdf/api/search?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE) + "&q=target&limit=20")).json();
  assert.equal(search.matches[0].page, 7);
  assert.equal(search.matches[0].pos, 0);
  assert.equal(search.q, "target");
  assert.deepEqual(pageTextMessages.map((message) => message.action), ["status", "search"]);
  assert.equal(pageTextMessages.some((message) => /start|preprocess|fallback/i.test(message.action)), false);
});

test("native local search rejects an empty query with a visible 400", async () => {
  const { context } = await harness();
  const response = await context.fetch(
    "/pdf/api/search?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE) + "&q=",
  );
  const payload = await response.json();
  assert.equal(response.status, 400);
  assert.equal(payload.ok, false);
  assert.equal(payload.code, "BW_PAGE_TEXT_SEARCH_FAILED");
});

test("native page text replies reject unknown top-level fields", async () => {
  const { context } = await harness({
    pageTextReply(message) {
      return {
        contract: "reader-native-page-text-response/1",
        action: message.action,
        requestId: message.requestId,
        ok: false,
        state: "idle",
        source: null,
        revision: "strict-v1",
        page: message.page,
        pageWidth: 0,
        pageHeight: 0,
        chars: null,
        formulaRegions: [],
        error: null,
        unexpected: true,
      };
    },
  });
  context.BWReaderRuntime.pageTextProvider.registerEmbeddedPage(8, {
    pageWidth: 600,
    pageHeight: 800,
    chars: [],
  });
  const response = await context.fetch("/pdf/api/page-chars?page=8");
  const payload = await response.json();
  assert.equal(response.status, 422);
  assert.equal(payload.ok, false);
  assert.equal(payload.code, "BW_PAGE_TEXT_BRIDGE_RESPONSE");
});

test("reader char-layer refreshes on native completion and assistant exposes pending formula state", () => {
  const charLayer = readFileSync(
    new URL("_server_deploy/static/pdf/reader.src/08-charlayer.js", ROOT),
    "utf8",
  );
  const assistant = readFileSync(
    new URL("_server_deploy/static/pdf/reader.src/25-assistant.js", ROOT),
    "utf8",
  );
  const search = readFileSync(
    new URL("_server_deploy/static/pdf/reader.src/11-search.js", ROOT),
    "utf8",
  );
  assert.match(charLayer, /addEventListener\('bw:page-text-updated'/);
  assert.match(charLayer, /loadCharsAndBindLayer\(page, wrap, viewport, 0\)/);
  assert.match(charLayer, /new Intl\.Segmenter\(locale, \{ granularity: 'word' \}\)/);
  assert.match(charLayer, /d\.state === 'pending'/);
  assert.match(assistant, /公式区域正在处理/);
  assert.match(assistant, /page_text_status/);
  assert.match(search, /d\.incomplete \? ' · 部分页待识别'/);
});

test("embedded PDF text uses real word segmentation even without spaces", () => {
  const charLayer = readFileSync(
    new URL("_server_deploy/static/pdf/reader.src/08-charlayer.js", ROOT),
    "utf8",
  );
  const start = charLayer.indexOf("let _charSel");
  const end = charLayer.indexOf("function _nativePageTextProvider");
  assert.equal(start >= 0 && end > start, true);
  const context = vm.createContext({
    BOOK_LANGS: ["ja"],
    CHARS_VER: 99,
    Intl,
    textContent: {
      items: [{
        str: "私は学生です",
        dir: "ltr",
        width: 120,
        height: 20,
        transform: [1, 0, 0, 1, 10, 90],
      }],
    },
    viewport: {
      width: 600,
      height: 800,
      convertToViewportPoint(x, y) { return [x, 800 - y]; },
    },
  });
  vm.runInContext(charLayer.slice(start, end), context);
  const page = vm.runInContext("_embeddedPageText(textContent, viewport, 1)", context);
  const wordIds = new Set(page.chars.map((item) => item.w).filter((word) => word >= 0));
  assert.equal(page.chars.map((item) => item.c).join(""), "私は学生です");
  assert.equal(wordIds.size >= 3, true);
  assert.equal(page.chars.every((item) => Number.isInteger(item.w)), true);
});

function nativeEPUBAssistantManifest() {
  const manifest = clone(NATIVE_INTERFACE_MANIFEST);
  for (const route of manifest.routes) {
    if (["/pdf/api/epub-assistant", "/pdf/api/epub-action"].includes(route.path)) {
      route.status = "supported";
    }
  }
  return manifest;
}

test("native EPUB assistant injects one complete App-owned sidecar snapshot", async () => {
  const result = await harness({
    surface: "epub",
    interfaceManifest: nativeEPUBAssistantManifest(),
  });
  await result.context.fetch("/pdf/api/epub-highlights", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      file: DEFAULT_LOCAL_FILE, id: "c_1111111111111111",
      anchor: { section: 3, start: 10, end: 18 }, text: "local highlight",
      color: "#ffd54a",
    }),
  });
  await result.context.fetch("/pdf/api/notes", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      file: DEFAULT_LOCAL_FILE, id: "c_2222222222222222",
      anchor: { kind: "epub", section: 3, x: 0.7, y: 0.2 },
      text: "local note", strokes: [],
    }),
  });
  await result.context.fetch("/pdf/api/epub-ink", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      file: DEFAULT_LOCAL_FILE, idx: 3,
      strokes: [{ c: "#ff0000", w: 2, pts: [[0, 0], [1, 1]] }],
    }),
  });
  const response = await result.context.fetch("/pdf/api/epub-assistant", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
    body: JSON.stringify({
      message: "what did I mark?",
      context: { file: DEFAULT_LOCAL_FILE, current_section_idx: 3 },
    }),
  });
  assert.equal(response.status, 200);
  assert.equal(result.gatewayMessages.length, 1);
  const forwarded = JSON.parse(result.gatewayMessages[0].body);
  const state = forwarded.context.native_local_state;
  assert.equal(state.contract, "reader-native-epub-assistant-state/1");
  assert.equal(state.file, DEFAULT_LOCAL_FILE);
  assert.deepEqual(
    { highlights: state.revisions.highlights, notes: state.revisions.notes, ink: state.revisions.ink },
    { highlights: 1, notes: 1, ink: 1 },
  );
  assert.equal(state.highlights[0].text, "local highlight");
  assert.equal(state.notes[0].text, "local note");
  assert.equal(state.ink["3"].length, 1);
});

test("native EPUB note apply and undo mutate only App state while Pi stores action metadata", async () => {
  let lastGatewayMessage = null;
  const result = await harness({
    surface: "epub",
    interfaceManifest: nativeEPUBAssistantManifest(),
    gatewayReply(message) {
      lastGatewayMessage = message;
      return {
        contract: "reader-native-pi-response/2",
        streamURL: "http://127.0.0.1:43129/r/" + "a".repeat(64) +
          "/pi-proxy/" + "d".repeat(32),
      };
    },
    originalFetch() {
      const request = JSON.parse(lastGatewayMessage.body);
      const payload = request.op === "attach"
        ? { ok: true, stored: true, actions: request.actions }
        : { ok: true, state: request.action.state, action: request.action };
      return Promise.resolve(new Response(JSON.stringify(payload), {
        status: 200, headers: { "Content-Type": "application/json" },
      }));
    },
  });
  const note = {
    id: "c_3333333333333333",
    anchor: { kind: "epub", section: 4, x: 0.7, y: 0.2 },
    text: "created locally", color: "#ffffff", w: 260, h: 180,
    collapsed: false, strokes: [], video: null, card: null, html: null,
    iar: null, created: 1_800_000_000, updated: 1_800_000_000,
  };
  const action = {
    id: "act_native_note_1", kind: "notes_create", title: "new note", detail: "",
    undo: { op: "sticky_delete", file: DEFAULT_LOCAL_FILE, ids: [note.id] },
    redo: { op: "sticky_create", file: DEFAULT_LOCAL_FILE, notes: [note] },
    state: "done", ts: 1_800_000_000,
  };
  const applied = await result.context.fetch("/pdf/api/epub-action", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      op: "native_apply", contract: "reader-native-epub-action/1",
      file: DEFAULT_LOCAL_FILE, action,
    }),
  });
  assert.equal(applied.status, 200);
  const appliedPayload = await applied.json();
  assert.equal(appliedPayload.ok, true);
  let notes = await (await result.context.fetch(
    "/pdf/api/notes?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE),
  )).json();
  assert.equal(notes.notes[0].text, "created locally");

  const undone = await result.context.fetch("/pdf/api/epub-action", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ op: "undo", file: DEFAULT_LOCAL_FILE, action: appliedPayload.action }),
  });
  assert.equal(undone.status, 200);
  assert.equal((await undone.json()).state, "undone");
  notes = await (await result.context.fetch(
    "/pdf/api/notes?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE),
  )).json();
  assert.equal(notes.notes.length, 0);
  assert.deepEqual(
    result.gatewayMessages.map((message) => JSON.parse(message.body).op),
    ["attach", "native_commit"],
  );
  assert.equal(JSON.parse(result.gatewayMessages[1].body).native_contract,
    "reader-native-epub-action/1");
});

test("native EPUB action keeps local state when Pi metadata does not commit", async () => {
  let lastGatewayMessage = null;
  const result = await harness({
    surface: "epub",
    interfaceManifest: nativeEPUBAssistantManifest(),
    gatewayReply(message) {
      lastGatewayMessage = message;
      return {
        contract: "reader-native-pi-response/2",
        streamURL: "http://127.0.0.1:43129/r/" + "a".repeat(64) +
          "/pi-proxy/" + "e".repeat(32),
      };
    },
    originalFetch() {
      assert.equal(JSON.parse(lastGatewayMessage.body).op, "attach");
      return Promise.resolve(new Response(JSON.stringify({ ok: false, error: "metadata offline" }), {
        status: 503, headers: { "Content-Type": "application/json" },
      }));
    },
  });
  const note = {
    id: "c_4444444444444444",
    anchor: { kind: "epub", section: 1, x: 0.7, y: 0.2 },
    text: "must remain local", color: "#ffffff", w: 260, h: 180,
    collapsed: false, strokes: [], video: null, card: null, html: null,
    iar: null, created: 1_800_000_000, updated: 1_800_000_000,
  };
  const response = await result.context.fetch("/pdf/api/epub-action", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      op: "native_apply", contract: "reader-native-epub-action/1", file: DEFAULT_LOCAL_FILE,
      action: {
        id: "act_native_note_rollback", kind: "notes_create", title: "new note", detail: "",
        undo: { op: "sticky_delete", file: DEFAULT_LOCAL_FILE, ids: [note.id] },
        redo: { op: "sticky_create", file: DEFAULT_LOCAL_FILE, notes: [note] },
        state: "done", ts: 1_800_000_000,
      },
    }),
  });
  assert.equal(response.status, 200);
  const payload = await response.json();
  assert.equal(payload.ok, true);
  assert.equal(payload.metadata_synced, false);
  assert.equal(payload.metadata_pending, true);
  assert.match(payload.warning, /metadata offline/);
  const notes = await (await result.context.fetch(
    "/pdf/api/notes?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE),
  )).json();
  assert.equal(notes.notes.length, 1);
  assert.equal(notes.notes[0].text, "must remain local");
});

test("native EPUB attach rejects a local action for another book before Pi metadata", async () => {
  const result = await harness({
    surface: "epub",
    interfaceManifest: nativeEPUBAssistantManifest(),
  });
  const response = await result.context.fetch("/pdf/api/epub-action", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      op: "attach", file: DEFAULT_LOCAL_FILE,
      actions: [{
        id: "act_wrong_book", kind: "highlight", state: "done",
        undo: { op: "hl_delete", file: "localbook:another-book", ids: ["c_wrong"] },
        redo: { op: "hl_create", file: "localbook:another-book", highlights: [] },
      }],
    }),
  });
  assert.equal(response.status, 400);
  assert.equal((await response.json()).code, "BW_NATIVE_EPUB_ACTION_BODY");
  assert.equal(result.gatewayMessages.length, 0);
});

test("generic EPUB chat and voice wait for the App mutation before exposing success", async () => {
  const mutation = {
    contract: "reader-native-epub-action/1",
    file: DEFAULT_LOCAL_FILE,
    action: {
      id: "act_generic_epub_note", kind: "notes_create", title: "new note", detail: "",
      undo: { op: "sticky_delete", file: DEFAULT_LOCAL_FILE, ids: ["c_5555555555555555"] },
      redo: { op: "sticky_create", file: DEFAULT_LOCAL_FILE, notes: [] },
      state: "done", ts: 1_800_000_000,
    },
  };
  const action = { fn: "nativeLocalEPUBMutation", args: [mutation] };
  const highlightAction = {
    fn: "epubHighlight",
    args: [{ section: 2, texts: ["exact EPUB sentence"], color: "#ffd54a" }],
  };
  let proxyCall = 0;
  const result = await harness({
    surface: "epub",
    interfaceManifest: withGenericAssistantRoutesSupported(),
    originalFetch() {
      proxyCall += 1;
      if (proxyCall === 1) {
        return Promise.resolve(new Response(
          "event: actions\ndata: " + JSON.stringify([action]) +
          "\n\nevent: answer\ndata: \"saved\"\n\nevent: done\ndata: {}\n\n",
          { status: 200, headers: { "Content-Type": "text/event-stream" } },
        ));
      }
      return Promise.resolve(new Response(JSON.stringify({
        ok: true, result: { ok: true, client_action: highlightAction },
      }), { status: 200, headers: { "Content-Type": "application/json" } }));
    },
  });

  const releases = [];
  const calls = [];
  result.context.nativeLocalEPUBMutationTransaction = (request) => new Promise((resolve) => {
    calls.push({ kind: "note", request: clone(request) });
    releases.push(resolve);
  });
  result.context.nativeLocalEPUBHighlight = (request) => new Promise((resolve) => {
    calls.push({ kind: "highlight", request: clone(request) });
    releases.push(resolve);
  });

  const chatResponse = await result.context.fetch("/api/assistant/chat", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      message: "save", rid: "rid-generic-epub",
      context: { file_rel: DEFAULT_LOCAL_FILE, current_section_idx: 2 },
    }),
  });
  let chatSettled = false;
  let chatValue = "";
  const chatText = chatResponse.text().then((text) => {
    chatSettled = true;
    chatValue = text;
    return text;
  });
  for (let attempt = 0; attempt < 20 && calls.length < 1; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  assert.equal(calls.length, 1, JSON.stringify({
    chatSettled, chatValue, gatewayMessages: result.gatewayMessages,
  }));
  assert.equal(calls[0].kind, "note");
  assert.equal(chatSettled, false);
  const chatRequest = JSON.parse(result.gatewayMessages[0].body);
  assert.equal(chatRequest.context.file_rel, DEFAULT_LOCAL_FILE);
  assert.equal(chatRequest.context.native_local_state.contract,
    "reader-native-epub-assistant-state/1");
  assert.deepEqual(Object.keys(chatRequest.context.native_local_state.revisions).sort(),
    ["highlights", "ink", "notes"]);
  releases.shift()();
  assert.match(await chatText, /event: actions\ndata: \[\]/);
  assert.equal(chatSettled, true);

  let voiceSettled = false;
  const voicePromise = result.context.fetch("/api/assistant/voice-tool", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      cmd: JSON.stringify({ tool: "notes_create", args: { text: "note" } }),
      ctx: { file_rel: DEFAULT_LOCAL_FILE, page: 3 },
    }),
  }).then((response) => {
    voiceSettled = true;
    return response;
  });
  for (let attempt = 0; attempt < 20 && calls.length < 2; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  assert.equal(calls.length, 2);
  assert.equal(calls[1].kind, "highlight");
  assert.equal(voiceSettled, false);
  const voiceRequest = JSON.parse(result.gatewayMessages[1].body);
  assert.equal(voiceRequest.ctx.file_rel, DEFAULT_LOCAL_FILE);
  assert.equal(voiceRequest.ctx.current_section_idx, 2);
  assert.equal(voiceRequest.ctx.native_local_state.contract,
    "reader-native-epub-assistant-state/1");
  releases.shift()();
  const voiceResponse = await voicePromise;
  assert.equal(voiceResponse.status, 200);
  assert.equal((await voiceResponse.json()).result.client_action, null);
  assert.equal(voiceSettled, true);
});

test("generic EPUB local mutation failure suppresses later chat and voice success", async () => {
  const action = {
    fn: "nativeLocalEPUBMutation",
    args: [{
      contract: "reader-native-epub-action/1", file: DEFAULT_LOCAL_FILE,
      action: { id: "act_fail", undo: {}, redo: {} },
    }],
  };
  let proxyCall = 0;
  const result = await harness({
    surface: "epub",
    interfaceManifest: withGenericAssistantRoutesSupported(),
    originalFetch() {
      proxyCall += 1;
      if (proxyCall === 1) {
        return Promise.resolve(new Response(
          "event: actions\ndata: " + JSON.stringify([action]) +
          "\n\nevent: answer\ndata: \"must not escape\"\n\nevent: done\ndata: {}\n\n",
          { status: 200, headers: { "Content-Type": "text/event-stream" } },
        ));
      }
      return Promise.resolve(new Response(JSON.stringify({
        ok: true, result: { ok: true, client_action: action },
      }), { status: 200, headers: { "Content-Type": "application/json" } }));
    },
  });
  result.context.nativeLocalEPUBMutationTransaction = () => Promise.reject(new Error("disk full"));

  const chat = await result.context.fetch("/api/assistant/chat", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ message: "save", rid: "rid-fail", context: {} }),
  });
  const chatText = await chat.text();
  assert.match(chatText, /event: error/);
  assert.match(chatText, /disk full/);
  assert.doesNotMatch(chatText, /must not escape/);

  const voice = await result.context.fetch("/api/assistant/voice-tool", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cmd: "{}", ctx: {} }),
  });
  assert.equal(voice.status, 500);
  assert.equal((await voice.json()).ok, false);
});

test("native sync-batch executes mixed local and Pi operations without forwarding the envelope", async () => {
  const ownerNamespace = "acct-v1-" + "7".repeat(64);
  const result = await harness({ accountNamespace: ownerNamespace });
  const operations = [
    {
      mutationId: "mut-v2-" + "1".repeat(32),
      url: "/pdf/api/highlights",
      method: "POST",
      body: {
        file: DEFAULT_LOCAL_FILE,
        id: "c_1111111111111111",
        page: 4,
        rects: [[10, 20, 30, 40]],
        color: "#ffd54a",
        text: "batch highlight",
      },
    },
    {
      mutationId: "mut-v2-" + "2".repeat(32),
      url: "/pdf/api/lookup-event",
      method: "POST",
      body: {
        file: DEFAULT_LOCAL_FILE,
        word: "同期",
        context: "同期処理",
      },
    },
    {
      mutationId: "mut-v2-" + "3".repeat(32),
      url: "/pdf/api/notes",
      method: "POST",
      body: {
        file: DEFAULT_LOCAL_FILE,
        id: "c_3333333333333333",
        anchor: { kind: "pdf", page: 5, x: 0.2, y: 0.3 },
        text: "batch note",
      },
    },
    {
      mutationId: "invalid-mutation-id",
      url: "/pdf/api/highlights",
      method: "POST",
      body: {},
    },
  ];

  const response = await result.context.fetch("/pdf/api/sync-batch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      contract: "command-outbox/2",
      ownerNamespace,
      generation: 1,
      ops: operations,
    }),
  });
  assert.equal(response.status, 200);
  const payload = await response.json();
  assert.equal(payload.ok, true);
  assert.equal(payload.ownerNamespace, ownerNamespace);
  assert.equal(payload.generation, 1);
  assert.deepEqual(
    payload.results.map((item) => item.status),
    [200, 200, 200, 400],
  );
  assert.equal(payload.results[3].code, "BW_NATIVE_SYNC_BATCH_OPERATION");

  const highlights = await (await result.context.fetch(
    "/pdf/api/highlights?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE),
  )).json();
  const notes = await (await result.context.fetch(
    "/pdf/api/notes?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE),
  )).json();
  assert.equal(highlights.highlights.length, 1);
  assert.equal(highlights.highlights[0].text, "batch highlight");
  assert.equal(notes.notes.length, 1);
  assert.equal(notes.notes[0].text, "batch note");

  assert.deepEqual(result.gatewayMessages.map((message) => message.path), [
    "/pdf/api/lookup-event",
  ]);
  assert.equal(result.gatewayMessages[0].method, "POST");
  assert.equal(JSON.parse(result.gatewayMessages[0].body).file, DEFAULT_LOCAL_FILE);
  assert.equal(result.originalFetchCalls.some((call) => {
    const input = typeof call.input === "string" ? call.input : call.input.url;
    return new URL(input, result.context.location.href).pathname === "/pdf/api/sync-batch";
  }), false);
});

test("native PDF barrier drains an in-flight sync batch and stale account generation blocks later items", async () => {
  const ownerNamespace = "acct-v1-" + "9".repeat(64);
  let releaseGateway = null;
  const gatewayBarrier = new Promise((resolve) => { releaseGateway = resolve; });
  const result = await harness({
    accountNamespace: ownerNamespace,
    interfaceManifest: withNativePDFMutationRoutesSupported(),
    pdfMutationReply: nativePDFMutationResponder({ pageCount: 4 }),
    gatewayReply() {
      return gatewayBarrier.then(() => ({
        contract: "reader-native-pi-response/2",
        streamURL: "http://127.0.0.1:43129/r/" + "a".repeat(64) +
          "/pi-proxy/" + "f".repeat(32),
      }));
    },
  });

  const batchPromise = result.context.fetch("/pdf/api/sync-batch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      contract: "command-outbox/2",
      ownerNamespace,
      generation: 1,
      ops: [
        {
          mutationId: "mut-v2-" + "6".repeat(32),
          url: "/pdf/api/lookup-event",
          method: "POST",
          body: { file: DEFAULT_LOCAL_FILE, word: "drain" },
        },
        {
          mutationId: "mut-v2-" + "7".repeat(32),
          url: "/pdf/api/notes",
          method: "POST",
          body: {
            file: DEFAULT_LOCAL_FILE,
            id: "c_7777777777777777",
            anchor: { kind: "pdf", page: 3, x: 0.2, y: 0.3 },
            text: "must not commit after generation change",
          },
        },
      ],
    }),
  });
  for (let attempt = 0; attempt < 20 && result.gatewayMessages.length < 1; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  assert.equal(result.gatewayMessages.length, 1, "first sync item must be in flight");

  const mutation = await result.context.fetch("/pdf/api/pdf-insert-page", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file: DEFAULT_LOCAL_FILE, after: 1, md: "after batch" }),
  });
  const mutationReceipt = await mutation.json();
  for (let attempt = 0; attempt < 10; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
  assert.deepEqual(
    result.pdfMutationMessages.map((message) => message.action),
    ["recover"],
    "PDF prepare must not snapshot while the first sync item is in flight",
  );

  result.context.BWReaderRuntime.accountContext.deactivate("test-generation-change");
  releaseGateway();
  const batch = await batchPromise;
  assert.notEqual(batch.status, 200);
  const batchPayload = await batch.json();
  assert.equal(batchPayload.ok, false);
  assert.equal(batchPayload.code, "BW_ACCOUNT_CONTEXT_STALE");
  assert.equal(Object.hasOwn(batchPayload, "results"), false);

  const notes = await (await result.context.fetch(
    "/pdf/api/notes?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE),
  )).json();
  assert.deepEqual(notes.notes, []);

  await waitForPDFMutationAction(result.pdfMutationMessages, "prepare");
  const mutationJob = await waitForNativePDFJob(
    result.context, mutationReceipt.job_id,
  );
  assert.equal(mutationJob.status, "done");
});

test("native sync-batch fails closed when the owner lease changes mid-batch", async () => {
  const ownerNamespace = "acct-v1-" + "8".repeat(64);
  let accountContext = null;
  const result = await harness({
    accountNamespace: ownerNamespace,
    gatewayReply() {
      accountContext.deactivate("test-owner-change");
      return {
        contract: "reader-native-pi-response/2",
        streamURL: "http://127.0.0.1:43129/r/" + "a".repeat(64) +
          "/pi-proxy/" + "e".repeat(32),
      };
    },
  });
  accountContext = result.context.BWReaderRuntime.accountContext;

  const response = await result.context.fetch("/pdf/api/sync-batch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      contract: "command-outbox/2",
      ownerNamespace,
      generation: 1,
      ops: [
        {
          mutationId: "mut-v2-" + "4".repeat(32),
          url: "/pdf/api/lookup-event",
          method: "POST",
          body: { file: DEFAULT_LOCAL_FILE, word: "lease" },
        },
        {
          mutationId: "mut-v2-" + "5".repeat(32),
          url: "/pdf/api/notes",
          method: "POST",
          body: {
            file: DEFAULT_LOCAL_FILE,
            id: "c_5555555555555555",
            anchor: { kind: "pdf", page: 6, x: 0.2, y: 0.3 },
            text: "must not land",
          },
        },
      ],
    }),
  });
  assert.notEqual(response.status, 200);
  const payload = await response.json();
  assert.equal(payload.ok, false);
  assert.equal(payload.code, "BW_ACCOUNT_CONTEXT_STALE");
  assert.equal(Object.hasOwn(payload, "results"), false);
  assert.deepEqual(result.gatewayMessages.map((message) => message.path), [
    "/pdf/api/lookup-event",
  ]);

  const notes = await (await result.context.fetch(
    "/pdf/api/notes?file=" + encodeURIComponent(DEFAULT_LOCAL_FILE),
  )).json();
  assert.deepEqual(notes.notes, []);
});
