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
const PDF_AI = readFileSync(
  new URL("_server_deploy/static/pdf/reader.src/21-misc-ai.js", ROOT),
  "utf8",
);

function clone(value) {
  return value == null ? value : JSON.parse(JSON.stringify(value));
}

function makeDataStore(gateEvents) {
  const values = new Map();
  return {
    put(collection, value) {
      const key = `${collection}:${value.id}`;
      values.set(key, clone(value));
      if (String(value.id).startsWith("native-idb-gate-")) gateEvents.push("put");
      return Promise.resolve({ ok: true });
    },
    get(collection, id) {
      const key = `${collection}:${id}`;
      const value = values.get(key);
      if (String(id).startsWith("native-idb-gate-")) gateEvents.push("get");
      return Promise.resolve(value == null ? null : { value: clone(value) });
    },
    remove(collection, id) {
      values.delete(`${collection}:${id}`);
      if (String(id).startsWith("native-idb-gate-")) gateEvents.push("remove");
      return Promise.resolve({ ok: true });
    },
  };
}

async function harness() {
  const gateEvents = [];
  const preferenceEvents = [];
  const gatewayMessages = [];
  const localStorage = new Map();
  const globalStore = makeDataStore([]);
  const documentStore = makeDataStore([]);
  const deviceStore = makeDataStore(gateEvents);
  const pendingStores = [globalStore, documentStore, deviceStore];
  const router = {
    contract: "storage-router/1",
    get: (collection, id) => globalStore.get(collection, id),
    put: (collection, value) => globalStore.put(collection, value),
    remove: (collection, id) => globalStore.remove(collection, id),
    subscribe: () => () => {},
  };
  const context = {
    URL,
    URLSearchParams,
    Request,
    Response,
    Blob,
    Uint8Array,
    TextEncoder,
    TextDecoder,
    Set,
    Map,
    Promise,
    Date,
    JSON,
    Error,
    atob,
    btoa,
    crypto: webcrypto,
    console,
    location: {
      origin: "http://127.0.0.1:43129",
      href: "http://127.0.0.1:43129/r/" + "a".repeat(64) + "/shells/pdf.html",
    },
    __BW_NATIVE_LOCAL_READER__: true,
    __BW_NATIVE_LOCAL_BOOK_ID__: "localbook-" + "b".repeat(64),
    __BW_NATIVE_LOCAL_BASE_PATH__: "/r/" + "a".repeat(64),
    localStorage: {
      getItem: (key) => localStorage.get(key) ?? null,
      setItem: (key, value) => localStorage.set(key, String(value)),
      removeItem: (key) => localStorage.delete(key),
    },
    navigator: { sendBeacon: () => false },
    document: {
      readyState: "complete",
      getElementById: () => null,
      createElement: () => ({ style: {}, appendChild() {} }),
      body: { appendChild() {} },
      documentElement: { appendChild() {} },
    },
    CustomEvent: class CustomEvent {
      constructor(type, init) { this.type = type; this.detail = init?.detail; }
    },
    addEventListener() {},
    dispatchEvent() {},
    fetch: async () => new Response("not intercepted", { status: 418 }),
    webkit: {
      messageHandlers: {
        bwNativePiGateway: {
          postMessage(message) {
            gatewayMessages.push(clone(message));
            return Promise.resolve({
              contract: "reader-native-pi-response/1",
              status: 200,
              headers: { "Content-Type": "application/json" },
              bodyBase64: btoa(JSON.stringify({ ok: true })),
            });
          },
        },
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
  vm.runInNewContext(SOURCE, context, { filename: "native-local-runtime.js" });
  await context.BWReaderRuntime.nativeLocalRuntime.ready();
  return { context, gateEvents, gatewayMessages, preferenceEvents, globalStore };
}

test("native local runtime performs a real put/get/delete/get store gate before ready", async () => {
  const { context, gateEvents } = await harness();
  assert.equal(context.BWReaderRuntime.nativeLocalRuntime.status().state, "ready");
  assert.deepEqual(gateEvents, ["put", "get", "remove", "get"]);
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
    SOURCE.indexOf("function sanitizeSection"),
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

test("EPUB resource inflation is serial and bounded before bytes are materialized", () => {
  assert.match(SOURCE, /maximumEPUBResourceBytes = 32 \* 1024 \* 1024/);
  assert.match(SOURCE, /maximumEPUBResourceTotalBytes = 128 \* 1024 \* 1024/);
  assert.match(SOURCE, /maximumEPUBResourceCount = 128/);
  const resources = SOURCE.slice(
    SOURCE.indexOf("function createEPUBResourceBudget"),
    SOURCE.indexOf("var PI_ALLOWED_EXACT"),
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
  assert.match(resources, /epub-css'[\s\S]*textResponse\('', 200/);
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

test("native Pi fetch sends the strict Swift gateway envelope and rejects adjacent routes", async () => {
  const { context, gatewayMessages } = await harness();
  const response = await context.fetch("/pdf/api/translate", {
    method: "POST",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ text: "hello" }),
  });
  assert.equal(response.status, 200);
  assert.deepEqual(gatewayMessages, [{
    contract: "reader-native-pi-request/1",
    action: "fetch",
    method: "POST",
    path: "/pdf/api/translate",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify({ text: "hello" }),
  }]);

  const refused = await context.fetch("/pdf/api/dictionary-evil");
  assert.equal(refused.status, 503);
  assert.equal((await refused.json()).code, "BW_PI_GATEWAY_ROUTE");
  assert.equal(gatewayMessages.length, 1);
});

test("native local reading position persists through the local document store", async () => {
  const { context } = await harness();
  const put = await context.fetch("/pdf/api/reading-pos", {
    method: "POST",
    body: JSON.stringify({ kind: "pdf", pos: 12 }),
  });
  assert.equal(put.status, 200);
  const get = await context.fetch("/pdf/api/reading-pos");
  const payload = await get.json();
  assert.equal(payload.positions["localbook:localbook-" + "b".repeat(64)].pos, 12);
});

test("native local notes retain offline card placement payloads", async () => {
  const { context } = await harness();
  const card = {
    id: "note-card-1",
    type: "card",
    gid: "card-42",
    placement: { page: 7, x: 0.25, y: 0.4, width: 0.3, height: 0.2 },
  };
  const put = await context.fetch("/pdf/api/notes", {
    method: "POST",
    body: JSON.stringify(card),
  });
  assert.equal(put.status, 200);
  const get = await context.fetch("/pdf/api/notes");
  const payload = await get.json();
  assert.equal(payload.notes.length, 1);
  assert.equal(payload.notes[0].id, card.id);
  assert.equal(payload.notes[0].gid, card.gid);
  assert.deepEqual(payload.notes[0].placement, card.placement);
});
