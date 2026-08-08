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
  let revision = 0;
  return {
    put(collection, value) {
      const key = `${collection}:${value.id}`;
      const current = values.get(key);
      const record = clone(value);
      revision += 1;
      values.set(key, { value: record, rev: (current?.rev || 0) + 1, updatedAt: revision });
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
          return Promise.reject(new Error("revision conflict"));
        }
      }
      for (const mutation of mutations) {
        const value = clone(mutation.value);
        const key = `${mutation.collection}:${value.id}`;
        const current = values.get(key);
        revision += 1;
        values.set(key, {
          value,
          rev: (current?.rev || 0) + 1,
          updatedAt: revision,
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
  const pageTextMessages = [];
  const eventListeners = new Map();
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
    addEventListener(type, listener) {
      const list = eventListeners.get(type) || [];
      list.push(listener);
      eventListeners.set(type, list);
    },
    dispatchEvent(event) {
      for (const listener of eventListeners.get(event.type) || []) listener(event);
      return true;
    },
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
        ...(options.pageTextReply ? {
          bwNativePageText: {
            postMessage(message) {
              pageTextMessages.push(clone(message));
              return Promise.resolve(options.pageTextReply(clone(message)));
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
  return {
    context,
    gateEvents,
    gatewayMessages,
    pageTextMessages,
    preferenceEvents,
    globalStore,
    documentStore,
  };
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
    body: JSON.stringify({ kind: "pdf", pos: 9 }),
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
  const overlay = await (await context.fetch("/pdf/api/page-overlay?page=4")).json();
  assert.equal(overlay.ok, true);
  assert.equal(overlay.formula_regions[0].id, "formula-1");
  assert.equal(overlay.formula_regions[0].state, "pending");
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
      context.fetch("/pdf/api/search?q=target&limit=20").then((response) => response.json()),
      timeout,
    ]);
    assert.equal(payload.state, "ready");
    assert.equal(payload.incomplete, false);
    assert.equal(payload.total, 1);
    assert.equal(payload.pages, 1);
    assert.equal(payload.matches.length, 1);
    assert.equal(payload.matches[0].page, 1);
    assert.equal(payload.matches[0].count, 1);
    assert.match(payload.matches[0].snippet, /target/);
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
  const search = await (await context.fetch("/pdf/api/search?q=target&limit=20")).json();
  assert.equal(search.matches[0].page, 7);
  assert.deepEqual(pageTextMessages.map((message) => message.action), ["status", "search"]);
  assert.equal(pageTextMessages.some((message) => /start|preprocess|fallback/i.test(message.action)), false);
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
