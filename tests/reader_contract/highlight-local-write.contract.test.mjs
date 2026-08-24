import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => fs.readFileSync(new URL(path, ROOT), "utf8");
const PDF = read("_server_deploy/static/pdf/reader.src/17-highlight.js");
const RUNTIME = read("_server_deploy/static/pdf/native-local-runtime.js");
const VOICECALL = read("_server_deploy/static/pdf/rc-voicecall.js");
const DOCUMENT_HOST = read("_server_deploy/static/reader-runtime/document-host.js");
const LEGACY_RC_BRIDGE = read("_server_deploy/static/reader-runtime/legacy-rc-bridge.js");
const NATIVE_INTERFACE_MANIFEST = JSON.parse(read(
  "ios/BWReader/native_reader_interface_manifest.json",
));
const BOOK_ID = "localbook-" + "b".repeat(64);
const LOCAL_FILE = "localbook:" + BOOK_ID;

async function exactHighlightRuntime(options = {}) {
  const records = new Map();
  const batchOptions = [];
  const batchMutations = [];
  let failFirstExactBatch = options.failFirstExactBatch !== false;
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
      list(collection) {
        const prefix = `${collection}:`;
        return Promise.resolve([...records.entries()]
          .filter(([key]) => key.startsWith(prefix))
          .map(([, value]) => JSON.parse(JSON.stringify(value))));
      },
      batch(mutations, options) {
        if (!documentStore) return Promise.resolve(mutations.map(() => ({ ok: true })));
        batchOptions.push(options == null ? null : JSON.parse(JSON.stringify(options)));
        batchMutations.push(mutations.map((mutation) => mutation.collection));
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
  return { context, batchOptions, batchMutations, records };
}

function documentState(records, kind) {
  // 高亮已拆 per-item + meta 序记录：物化回数组，语义与旧整册记录一致。
  if (kind === "document-highlights" || kind === "epub-highlights") {
    const meta = [...records.entries()].find(
      ([key]) => key.startsWith(`native-${kind}-split-meta:`),
    )?.[1];
    if (!meta) return [];
    const byId = new Map(
      [...records.entries()]
        .filter(([key]) => key.startsWith(`native-${kind}-items:`))
        .map(([, record]) => [String(record.value.payload.id), record.value.payload]),
    );
    return (meta.value?.payload?.order || [])
      .map((id) => byId.get(String(id)))
      .filter((item) => item && item.deleted !== true);
  }
  const record = [...records.values()].find(
    (item) => item?.value?.id?.endsWith(`:${kind}`),
  );
  return record?.value?.payload;
}

function executeUndoThroughRealtimeReceiver(context, operationId) {
  const start = VOICECALL.indexOf("} else if (delivery.kind === 'client-action') {");
  const end = VOICECALL.indexOf(
    "} else {\n        throw new Error('BW_READER_REALTIME_OUTPUT_KIND_UNSUPPORTED')",
    start,
  );
  assert.ok(start >= 0 && end > start, "missing client-action receiver branch");
  context.delivery = { kind: "client-action" };
  context.p = { fn: "_nativeReaderUndoLast", args: [operationId] };
  context.work = null;
  vm.runInContext(
    `if (false) { ${VOICECALL.slice(start, end)} }`,
    context,
    { filename: "rc-voicecall-client-action.js" },
  );
  return context.work;
}

function installLegacyAdapterHost(context, kind, assistant) {
  const adapter = {
    kind,
    fileInfo: () => ({ file: LOCAL_FILE }),
    _host: { asst: assistant },
  };
  context.RC = {
    _adapter: adapter,
    adapter() { return this._adapter; },
    use(next) { this._adapter = next || {}; return this; },
  };
  vm.runInContext(DOCUMENT_HOST, context, { filename: "document-host.js" });
  vm.runInContext(LEGACY_RC_BRIDGE, context, { filename: "legacy-rc-bridge.js" });
  assert.equal(
    typeof context.RC.documentHost.current().reloadHighlights,
    "undefined",
    "legacy DocumentHost itself intentionally does not expose projection reload",
  );
  return adapter;
}

test("App exact highlights bypass a poisoned ordinary highlight queue", () => {
  assert.match(RUNTIME, /function mutateDocumentStateNow\(/);
  assert.match(
    RUNTIME,
    /savePDFHighlight:[\s\S]*withNativePDFWriter\('assistant-exact-highlight'[\s\S]*persistAssistantPDFHighlight\(/,
  );
  assert.match(
    RUNTIME,
    /function persistAssistantPDFHighlight[\s\S]*'document-highlights'[\s\S]*'pdf-assistant-undo'[\s\S]*'pdf-assistant-ops'[\s\S]*stores\.document\.batch\(/,
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
  const items = documentState(records, "document-highlights");
  assert.equal(items.length, 1);
  assert.equal(items[0].id, "c_2222222222222222");
});

test("Direct PDF highlight and undo are one replay-safe local authority chain", async () => {
  const { context, batchMutations, batchOptions, records } = await exactHighlightRuntime({
    failFirstExactBatch: false,
  });
  const runtime = context.BWReaderRuntime.nativeLocalRuntime;
  const payload = {
    file: LOCAL_FILE,
    id: "c_3333333333333333",
    page: 8,
    rects: [[11, 22, 33, 44]],
    color: "#ffd54a",
    text: "undo this exact highlight",
  };
  await runtime.savePDFHighlight(payload);
  context._allHighlights = [{ id: payload.id }];
  context._hlByPage = { 8: [{ id: payload.id }] };
  let refreshes = 0;
  installLegacyAdapterHost(context, "pdf", {
    reloadHighlights: async () => {
      refreshes += 1;
      const persisted = documentState(records, "document-highlights") || [];
      context._allHighlights = JSON.parse(JSON.stringify(persisted));
      context._hlByPage = persisted.reduce((pages, highlight) => {
        (pages[highlight.page] ||= []).push(highlight);
        return pages;
      }, {});
    },
  });
  assert.deepEqual(JSON.parse(JSON.stringify(batchMutations[0])), [
    "native-document-highlights-items",
    "native-document-highlights-split-meta",
    "native-pdf-assistant-undo",
    "native-pdf-assistant-ops",
  ]);
  assert.equal(batchOptions[0]?.transactionTimeoutMs, 4000);
  assert.equal(documentState(records, "document-highlights").length, 1);
  assert.equal(documentState(records, "pdf-assistant-undo").length, 1);

  const operationId = "rundo_" + "3".repeat(24);
  const first = await executeUndoThroughRealtimeReceiver(context, operationId);
  assert.deepEqual(
    {
      contract: first.contract,
      ok: first.ok,
      surface: first.surface,
      operationId: first.operationId,
      replayed: first.replayed,
      remaining: first.remaining,
    },
    {
      contract: "reader-native-undo-result/1",
      ok: true,
      surface: "pdf",
      operationId,
      replayed: false,
      remaining: 0,
    },
  );
  assert.equal(documentState(records, "document-highlights").length, 0);
  assert.equal(documentState(records, "pdf-assistant-undo").length, 0);
  assert.equal(refreshes, 1, "undo must reload the visible PDF highlight projection");
  assert.deepEqual(context._allHighlights, []);
  assert.deepEqual(JSON.parse(JSON.stringify(context._hlByPage)), {});

  const replay = await context._nativeReaderUndoLast(operationId);
  assert.equal(replay.replayed, true);
  assert.equal(replay.remaining, 0);
  assert.equal(documentState(records, "document-highlights").length, 0);
});

test("Direct PDF mutation IDs reject changed payloads and later edits block undo", async () => {
  const { context, records } = await exactHighlightRuntime({
    failFirstExactBatch: false,
  });
  const runtime = context.BWReaderRuntime.nativeLocalRuntime;
  const payload = {
    file: LOCAL_FILE,
    id: "c_4444444444444444",
    page: 9,
    rects: [[1, 2, 3, 4]],
    color: "#ffd54a",
    text: "original payload",
  };
  await runtime.savePDFHighlight(payload);
  await assert.rejects(
    runtime.savePDFHighlight({ ...payload, text: "different payload" }),
    (error) => error.code === "BW_NATIVE_PDF_ASSISTANT_CONFLICT",
  );
  assert.equal(documentState(records, "document-highlights")[0].text, "original payload");
  assert.equal(documentState(records, "pdf-assistant-undo").length, 1);

  const patched = await context.fetch("/pdf/api/highlights", {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      file: LOCAL_FILE,
      id: payload.id,
      note: "edited after creation",
    }),
  });
  assert.equal(patched.status, 200);
  await assert.rejects(
    context._nativeReaderUndoLast("rundo_" + "4".repeat(24)),
    (error) => error.code === "BW_NATIVE_PDF_ASSISTANT_CONFLICT",
  );
  assert.equal(documentState(records, "document-highlights").length, 1);
  assert.equal(documentState(records, "pdf-assistant-undo").length, 1);
});

test("Direct PDF undo fails closed when the authoritative stack is empty", async () => {
  const { context } = await exactHighlightRuntime({ failFirstExactBatch: false });
  await assert.rejects(
    context._nativeReaderUndoLast("rundo_" + "5".repeat(24)),
    (error) => error.code === "BW_NATIVE_PDF_UNDO_EMPTY",
  );
});

test("a failed PDF projection refresh cannot turn a committed undo into failure", async () => {
  const { context, records } = await exactHighlightRuntime({ failFirstExactBatch: false });
  await context.BWReaderRuntime.nativeLocalRuntime.savePDFHighlight({
    file: LOCAL_FILE,
    id: "c_5555555555555555",
    page: 10,
    rects: [[5, 6, 7, 8]],
    color: "#ffd54a",
    text: "refresh failure stays post-commit",
  });
  installLegacyAdapterHost(context, "pdf", {
    reloadHighlights: () => Promise.reject(new Error("renderer unavailable")),
  });
  const result = await context._nativeReaderUndoLast("rundo_" + "6".repeat(24));
  assert.equal(result.ok, true);
  assert.equal(documentState(records, "document-highlights").length, 0);
  assert.equal(documentState(records, "pdf-assistant-undo").length, 0);
});

// 删除也必须真的把事务上界交到底层。
//
// 这条曾经断在两处、都是同一个模式：调用处写了 transactionTimeoutMs，中间那层却少
// 一个形参，于是 JS 把它静默丢掉，保护全程空转。第一次断在 StorageRouter.batch，
// 第二次断在 mutateDocumentState —— 而当时的测试只静态检查调用处有没有那串字符，
// 所以照样通过。这里必须走真实的 runtime 与真实的 mutateDocumentState，看底层收到什么。
test("deleting a highlight hands the bounded timeout to the store", async () => {
  const { context, batchOptions } = await exactHighlightRuntime();
  const runtime = context.BWReaderRuntime.nativeLocalRuntime;
  const payload = {
    file: LOCAL_FILE,
    id: "c_1111111111111111",
    page: 4,
    rects: [[10, 20, 30, 40]],
    color: "#ffd54a",
    text: "bounded local highlight",
  };
  // 夹具让第一次带上界的写入以 BW_DATA_TIMEOUT 中止，先把它用掉。
  await assert.rejects(runtime.savePDFHighlight(payload));
  await runtime.savePDFHighlight({ ...payload, id: "c_2222222222222222" });

  const before = batchOptions.length;
  const response = await context.fetch("/pdf/api/highlights", {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ file: LOCAL_FILE, id: "c_2222222222222222" }),
  });
  assert.equal(response.status, 200);
  const deleteOptions = batchOptions.slice(before);
  assert.ok(deleteOptions.length > 0, "删除必须真的写了一次");
  for (const options of deleteOptions) {
    assert.equal(
      options?.transactionTimeoutMs,
      4000,
      "删除事务的上界必须抵达底层；中间任何一层少一个形参都会让它静默失效",
    );
  }
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
