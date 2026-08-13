import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => fs.readFileSync(new URL(path, ROOT), "utf8");
const PDF = read("_server_deploy/static/pdf/reader.src/17-highlight.js");
const RUNTIME = read("_server_deploy/static/pdf/native-local-runtime.js");
const NATIVE_INTERFACE_MANIFEST = JSON.parse(read(
  "ios/BWReader/native_reader_interface_manifest.json",
));
const BOOK_ID = "localbook-" + "b".repeat(64);
const LOCAL_FILE = "localbook:" + BOOK_ID;

async function exactHighlightRuntime() {
  const records = new Map();
  const batchOptions = [];
  let failFirstExactBatch = true;
  function makeStore(documentStore = false) {
    return {
      get(collection, id) {
        return Promise.resolve(records.get(`${collection}:${id}`) || null);
      },
      getMany(requests) {
        return Promise.resolve(requests.map(({ collection, id }) =>
          records.get(`${collection}:${id}`) || null));
      },
      put() { return Promise.resolve({ ok: true }); },
      remove() { return Promise.resolve({ ok: true }); },
      batch(mutations, options) {
        if (!documentStore) return Promise.resolve(mutations.map(() => ({ ok: true })));
        batchOptions.push(options == null ? null : JSON.parse(JSON.stringify(options)));
        if (options?.transactionTimeoutMs && failFirstExactBatch) {
          failFirstExactBatch = false;
          const error = new Error("bounded IndexedDB transaction aborted");
          error.code = "BW_DATA_TIMEOUT";
          return Promise.reject(error);
        }
        for (const mutation of mutations) {
          const key = `${mutation.collection}:${mutation.value.id}`;
          const current = records.get(key);
          records.set(key, {
            value: mutation.value,
            rev: Number(current?.rev || 0) + 1,
          });
        }
        return Promise.resolve(mutations.map(() => ({ ok: true })));
      },
    };
  }
  const stores = [makeStore(), makeStore(true), makeStore()];
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
    Date,
    JSON,
    Error,
    atob,
    btoa,
    crypto: webcrypto,
    console,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    location: {
      origin: "http://127.0.0.1:43129",
      href: "http://127.0.0.1:43129/r/" + "a".repeat(64) + "/shells/pdf.html",
      pathname: "/r/" + "a".repeat(64) + "/shells/pdf.html",
    },
    __BW_NATIVE_LOCAL_READER__: true,
    __BW_NATIVE_LOCAL_BOOK_ID__: BOOK_ID,
    __BW_NATIVE_LOCAL_BASE_PATH__: "/r/" + "a".repeat(64),
    __BW_NATIVE_INTERFACE_MANIFEST__: NATIVE_INTERFACE_MANIFEST,
    localStorage: {
      getItem: () => null,
      setItem() {},
      removeItem() {},
    },
    navigator: { sendBeacon: () => false },
    document: {
      readyState: "complete",
      visibilityState: "visible",
      getElementById: () => null,
      createElement: () => ({ style: {}, appendChild() {} }),
      body: { appendChild() {} },
      documentElement: { appendChild() {} },
      addEventListener() {},
      removeEventListener() {},
    },
    CustomEvent: class CustomEvent {
      constructor(type, init) { this.type = type; this.detail = init?.detail; }
    },
    addEventListener() {},
    dispatchEvent() { return true; },
    fetch: async () => new Response("not intercepted", { status: 418 }),
    webkit: { messageHandlers: {} },
    BWReaderRuntime: {
      indexedDBStore: {
        createIndexedDBDataStore: () => stores.shift(),
      },
      dataRegistry: {
        CONTRACT: "data-registry/1",
        syncCollections: () => [],
        scopes: () => ({}),
        settingMigrations: () => [],
        collection: () => ({ status: "ready" }),
      },
      storageRouter: {
        createStorageRouter: () => ({
          contract: "storage-router/1",
          get: () => Promise.resolve(null),
          put: () => Promise.resolve({ ok: true }),
          remove: () => Promise.resolve({ ok: true }),
          subscribe: () => () => {},
        }),
      },
      preferenceStore: {
        createPreferenceStore: () => ({
          contract: "preference-store/1",
          attach: () => Promise.resolve(),
        }),
      },
    },
  };
  context.globalThis = context;
  context.window = context;
  vm.runInNewContext(RUNTIME, context, { filename: "native-local-runtime.js" });
  await context.BWReaderRuntime.nativeLocalRuntime.ready();
  return { context, batchOptions, records };
}

test("App exact highlights bypass a poisoned ordinary highlight queue", () => {
  assert.match(RUNTIME, /function mutateDocumentStateNow\(/);
  assert.match(
    RUNTIME,
    /savePDFHighlight:[\s\S]*withNativePDFWriter\('assistant-exact-highlight'[\s\S]*persistLocalPDFHighlight\([\s\S]*true/,
  );
  assert.match(
    RUNTIME,
    /function mutateDocumentState\([\s\S]*serializeLocalStateMutation\('document', kind,[\s\S]*mutateDocumentStateNow/,
  );
});

test("an aborted exact-highlight batch releases its writer and a later write settles", async () => {
  const { context, batchOptions, records } = await exactHighlightRuntime();
  const runtime = context.BWReaderRuntime.nativeLocalRuntime;
  const payload = {
    file: LOCAL_FILE,
    id: "c_1111111111111111",
    page: 4,
    rects: [[10, 20, 30, 40]],
    color: "#ffd54a",
    text: "bounded local highlight",
  };

  await assert.rejects(
    runtime.savePDFHighlight(payload),
    (error) => error.code === "BW_DATA_TIMEOUT",
  );
  const saved = await Promise.race([
    runtime.savePDFHighlight({ ...payload, id: "c_2222222222222222" }),
    new Promise((_, reject) => setTimeout(
      () => reject(new Error("later highlight stayed blocked behind the released writer")),
      500,
    )),
  ]);

  assert.equal(saved.ok, true);
  assert.equal(saved.id, "c_2222222222222222");
  assert.deepEqual(
    batchOptions.map((options) => options?.transactionTimeoutMs),
    [4000, 4000],
    "only the exact-highlight direct batches receive the inner IDB timeout",
  );
  const record = [...records.values()].find(
    (item) => item?.value?.id?.endsWith(":document-highlights"),
  );
  assert.equal(record.value.payload.length, 1);
  assert.equal(record.value.payload[0].id, "c_2222222222222222");
});

test("exact highlight uses the bounded App-local persistence entrypoint", () => {
  const start = PDF.indexOf("async function saveHighlight");
  const end = PDF.indexOf("function _pdfExactTextProjection", start);
  const save = PDF.slice(start, end);
  assert.match(save, /nativeRuntime\.savePDFHighlight\(payload\)/);
  assert.match(save, /BW_READER_HIGHLIGHT_LOCAL_WRITE_TIMEOUT/);
  assert.match(save, /6000/);
  assert.ok(
    save.indexOf("nativeRuntime.savePDFHighlight(payload)") <
      save.indexOf("fetch('/pdf/api/highlights'"),
    "App-local persistence must be chosen before the legacy fetch path",
  );
});
