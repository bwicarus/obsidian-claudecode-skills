import test from "node:test";
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { existsSync, readFileSync } from "node:fs";
import vm from "node:vm";

const SOURCE = readFileSync(
  new URL("../../extensions/bw-reader-webext/background.js", import.meta.url),
  "utf8",
);
const ACCOUNT_CONTEXT_SOURCE = readFileSync(
  new URL("../../_server_deploy/static/reader-runtime/account-context.js", import.meta.url),
  "utf8",
);
const EXTENSION_ACCOUNT_STORAGE_SOURCE = readFileSync(
  new URL(
    "../../_server_deploy/static/reader-runtime/extension-account-storage.js",
    import.meta.url,
  ),
  "utf8",
);
const DATA_STORE_SOURCE = readFileSync(
  new URL(
    "../../_server_deploy/static/reader-runtime/data-store.js",
    import.meta.url,
  ),
  "utf8",
);
const DOCUMENT_NOTE_REPOSITORY_SOURCE = readFileSync(
  new URL(
    "../../_server_deploy/static/reader-runtime/document-note-repository.js",
    import.meta.url,
  ),
  "utf8",
);
const INTERACTION_POLICY_SOURCE = readFileSync(
  new URL(
    "../../_server_deploy/static/reader-runtime/interaction-policy.js",
    import.meta.url,
  ),
  "utf8",
);
const VOCABULARY_STATE_SOURCE = readFileSync(
  new URL(
    "../../_server_deploy/static/reader-runtime/vocabulary-state.js",
    import.meta.url,
  ),
  "utf8",
);
const SYNC_GATEWAY_SOURCE = readFileSync(
  new URL(
    "../../_server_deploy/static/reader-runtime/sync-gateway.js",
    import.meta.url,
  ),
  "utf8",
);
const DIRECT_SYNC_PROTOCOL_SOURCE = readFileSync(
  new URL(
    "../../_server_deploy/static/reader-runtime/direct-sync-protocol.js",
    import.meta.url,
  ),
  "utf8",
);
const SYNC_CONFLICT_CONTROL_SOURCE = readFileSync(
  new URL(
    "../../_server_deploy/static/reader-runtime/sync-conflict-control.js",
    import.meta.url,
  ),
  "utf8",
);
const SYNC_COORDINATOR_SOURCE = readFileSync(
  new URL(
    "../../_server_deploy/static/reader-runtime/sync-coordinator.js",
    import.meta.url,
  ),
  "utf8",
);
const SYNC_RUNTIME_SOURCE = readFileSync(
  new URL(
    "../../_server_deploy/static/reader-runtime/sync-runtime.js",
    import.meta.url,
  ),
  "utf8",
);
const SYNC_OWNER_LEASE_SOURCE = readFileSync(
  new URL(
    "../../_server_deploy/static/reader-runtime/sync-owner-lease.js",
    import.meta.url,
  ),
  "utf8",
);
const MANIFEST = JSON.parse(readFileSync(
  new URL("../../extensions/bw-reader-webext/manifest.json", import.meta.url),
  "utf8",
));
const ORIGIN = "https://bwicarus.taile44d0c.ts.net";
const NAMESPACE = `acct-v1-${"b".repeat(64)}`;
const OTHER_NAMESPACE = `acct-v1-${"c".repeat(64)}`;
const TICKET_EXPIRES_AT = Math.floor(Date.now() / 1000) + 3600;
const TICKET = `pvt-v2-${TICKET_EXPIRES_AT}-${"a".repeat(32)}-${"c".repeat(64)}`;
const OTHER_TICKET = `pvt-v2-${TICKET_EXPIRES_AT}-${"d".repeat(32)}-${"e".repeat(64)}`;
const DEVICE_FAMILY_ID = `pwa-install-v1-${"f".repeat(32)}`;
let fakeStoreEpochSequence = 0;

function nextFakeStoreEpoch() {
  fakeStoreEpochSequence += 1;
  return "data-store-instance-v1-" +
    fakeStoreEpochSequence.toString(16).padStart(32, "0");
}

const tick = () => new Promise((resolve) => setImmediate(resolve));
const settleBackground = async () => {
  await tick();
  await tick();
};
const waitForPortMessage = async (port, predicate, label) => {
  const deadline = Date.now() + 2000;
  while (Date.now() < deadline) {
    const found = port.messages.find(predicate);
    if (found) return found;
    await new Promise((resolve) => setTimeout(resolve, 1));
  }
  throw new Error(`port message timeout: ${label}`);
};

function createProviderRegistry(overrides = {}) {
  const scopes = {
    "user-settings": {
      scope: "global", status: "ready", provider: true, sync: true,
      recordSchema: 1, conflictPolicy: "explicit",
    },
    "query-cache": {
      scope: "global", status: "ready", provider: true,
      conflictPolicy: "regenerate", derived: true,
    },
    "translation-cache": {
      scope: "global", status: "ready", provider: true,
      conflictPolicy: "regenerate", derived: true,
    },
    "dictionary-cache": {
      scope: "global", status: "ready", provider: true,
      conflictPolicy: "regenerate", derived: true,
    },
    "vocabulary-state": {
      scope: "global", status: "ready", provider: true, sync: true,
      recordSchema: 1, conflictPolicy: "explicit",
    },
    "document-notes": {
      scope: "document", status: "ready", provider: false, conflictPolicy: "explicit",
    },
    "cards": {
      scope: "global", status: "pending", provider: false, conflictPolicy: "explicit",
    },
  };
  const syncDescriptor = () => [
    {
      name: "user-settings",
      conflictPolicy: "explicit",
      derived: false,
      recordSchema: 1,
    },
    {
      name: "vocabulary-state",
      conflictPolicy: "explicit",
      derived: false,
      recordSchema: 1,
    },
  ];
  const syncDigest = () =>
    "sync-v3:record-parent-state/1|" +
    syncDescriptor().map((item) => [
      item.name,
      item.conflictPolicy,
      item.derived ? "1" : "0",
      String(item.recordSchema),
    ].join(":")).join("|");
  return {
    CONTRACT: "data-registry/1",
    SYNC_CONTRACT: "sync-v3",
    SYNC_CHANGE_CONTRACT: "record-parent-state/1",
    scopes: () => structuredClone(scopes),
    collection: (name) => Object.hasOwn(scopes, String(name || ""))
      ? structuredClone(scopes[String(name || "")])
      : null,
    providerCollections: () => [
      "dictionary-cache",
      "query-cache",
      "translation-cache",
      "user-settings",
      "vocabulary-state",
    ],
    syncCollections: () => [
      "user-settings",
      "vocabulary-state",
    ],
    isSyncCollection: (name) => [
      "user-settings",
      "vocabulary-state",
    ].includes(String(name || "")),
    syncDescriptor,
    syncDigest,
    ...overrides,
  };
}

function createFakeStore(state, records = new Map(), storeOptions = {}) {
  const subscribers = [];
  let closed = false;
  const dbName = String(storeOptions.dbName || "");
  let mutationJournal = state.storeMutationJournals.get(dbName);
  if (!mutationJournal) {
    mutationJournal = new Map();
    state.storeMutationJournals.set(dbName, mutationJournal);
  }
  if (!state.storeInstanceEpochs.has(dbName)) {
    state.storeInstanceEpochs.set(dbName, nextFakeStoreEpoch());
  }
  const cloneForStore = (value) =>
    state.realmClone ? state.realmClone(value) : structuredClone(value);
  const beforeOperation = async (operation, details = {}) => {
    state.storeOperations.push({
      dbName: String(storeOptions.dbName || ""),
      operation,
      details: cloneForStore(details),
    });
    if (typeof state.beforeStoreOperation === "function") {
      await state.beforeStoreOperation(operation, {
        ...details,
        dbName: String(storeOptions.dbName || ""),
      });
    }
  };
  state.emitStoreChange = (change) => {
    for (const listener of subscribers) listener(cloneForStore(change));
  };
  return {
    contract: "data-store/1",
    async instanceEpoch() {
      await beforeOperation("instanceEpoch");
      return state.storeInstanceEpochs.get(dbName);
    },
    subscribe(_query, listener) {
      subscribers.push(listener);
      return () => {
        const index = subscribers.indexOf(listener);
        if (index >= 0) subscribers.splice(index, 1);
      };
    },
    async status() {
      await beforeOperation("status");
      return {
        contract: "data-store/1",
        backend: "indexeddb",
        collections: state.statusCollections || [
          ...new Set([...records.values()].map((record) => record.collection)),
        ],
      };
    },
    async get(collection, id) {
      await beforeOperation("get", { collection, id });
      const record = records.get(`${collection}/${id}`) || null;
      return record ? cloneForStore(record) : null;
    },
    async list(_collection, query) {
      await beforeOperation("list", { collection: _collection, query });
      state.lastListQuery = query;
      if (state.largeResponse) return [{ id: "huge", value: "界".repeat(800_000) }];
      if (Object.hasOwn(state, "listResult")) {
        return structuredClone(state.listResult || []);
      }
      const offset = Math.max(0, Number(query?.offset) || 0);
      const limit = Math.max(1, Number(query?.limit) || 200);
      return cloneForStore(
        [...records.values()]
          .filter((record) =>
            record.collection === _collection &&
            (query?.includeDeleted === true || !record.deleted) &&
            (
              query?.documentId == null ||
              record.value?.documentId === query.documentId
            )
          )
          .sort((left, right) => left.id.localeCompare(right.id))
          .slice(offset, offset + limit),
      );
    },
    async put(collection, value, options) {
      options = options || {};
      await beforeOperation("put", { collection, value, options });
      const id = options.id || value.id;
      const key = `${collection}/${id}`;
      const current = records.get(key) || null;
      const mutationId = options.mutationId;
      const mutationSignature = `put/${collection}/${id}`;
      if (state.enforceMutationJournal && mutationId) {
        const replay = mutationJournal.get(mutationId);
        if (replay && replay.signature !== mutationSignature) {
          throw Object.assign(new Error("mutation id reused"), {
            code: "BW_DATA_MUTATION_REUSED",
          });
        }
        if (replay) return cloneForStore(replay.record);
      }
      if (
        options.ifRev != null &&
        Number(options.ifRev) !== Number(current?.rev || 0)
      ) {
        throw Object.assign(new Error("revision conflict"), {
          code: "BW_DATA_CONFLICT",
          details: {
            expectedRev: Number(options.ifRev),
            actualRev: Number(current?.rev || 0),
          },
        });
      }
      const record = {
        schema: 1,
        collection,
        id,
        rev: Number(current?.rev || 0) + 1,
        updatedAt: Number(current?.updatedAt || 0) + 1,
        updatedBy: "extension-test",
        deleted: false,
        value: cloneForStore(value),
      };
      records.set(key, record);
      if (state.enforceMutationJournal && mutationId) {
        mutationJournal.set(mutationId, {
          signature: mutationSignature,
          record: cloneForStore(record),
        });
      }
      state.storeCursor = Number(state.storeCursor || 0) + 1;
      const change = {
        cursor: state.storeCursor,
        mutationId: options.mutationId,
        operation: "put",
        collection,
        record: cloneForStore(record),
      };
      for (const listener of subscribers) listener(change);
      return cloneForStore(record);
    },
    async remove(collection, id, options = {}) {
      await beforeOperation("remove", { collection, id, options });
      const key = `${collection}/${id}`;
      const current = records.get(key) || null;
      const mutationId = options.mutationId;
      const mutationSignature = `remove/${collection}/${id}`;
      if (state.enforceMutationJournal && mutationId) {
        const replay = mutationJournal.get(mutationId);
        if (replay && replay.signature !== mutationSignature) {
          throw Object.assign(new Error("mutation id reused"), {
            code: "BW_DATA_MUTATION_REUSED",
          });
        }
        if (replay) return cloneForStore(replay.record);
      }
      if (!current) {
        throw Object.assign(new Error("record not found"), {
          code: "BW_DATA_NOT_FOUND",
        });
      }
      if (
        options.ifRev != null &&
        Number(options.ifRev) !== Number(current.rev)
      ) {
        throw Object.assign(new Error("revision conflict"), {
          code: "BW_DATA_CONFLICT",
          details: {
            expectedRev: Number(options.ifRev),
            actualRev: Number(current.rev),
          },
        });
      }
      const record = {
        ...cloneForStore(current),
        rev: Number(current.rev) + 1,
        updatedAt: Number(current.updatedAt || 0) + 1,
        deleted: true,
      };
      records.set(key, record);
      if (state.enforceMutationJournal && mutationId) {
        mutationJournal.set(mutationId, {
          signature: mutationSignature,
          record: cloneForStore(record),
        });
      }
      state.storeCursor = Number(state.storeCursor || 0) + 1;
      const change = {
        cursor: state.storeCursor,
        mutationId: options.mutationId,
        operation: "remove",
        collection,
        record: cloneForStore(record),
      };
      for (const listener of subscribers) listener(change);
      return cloneForStore(record);
    },
    async batch() {
      return [];
    },
    async changes(query) {
      state.lastChangesQuery = query;
      if (state.providerChanges) return structuredClone(state.providerChanges);
      return { cursor: 0, nextCursor: 0, changes: [] };
    },
    async applyChanges(changes, options = {}) {
      await beforeOperation("applyChanges", { changes, options });
      const applied = [];
      for (const change of changes || []) {
        const record = change?.record;
        if (!record?.collection || !record?.id) continue;
        records.set(
          `${record.collection}/${record.id}`,
          cloneForStore(record),
        );
        applied.push({
          collection: record.collection,
          id: record.id,
          mutationId: String(change.mutationId || ""),
        });
      }
      return { applied, conflicts: [], skipped: [] };
    },
    close() {
      if (closed) return;
      closed = true;
      state.closedStores.push(String(storeOptions.dbName || ""));
    },
  };
}

let portSequence = 0;
function makePort(pathname, extensionId = "extension-test", senderOverrides = {}) {
  const messageListeners = [];
  const disconnectListeners = [];
  const messages = [];
  const nextId = ++portSequence;
  const sender = {
    id: extensionId,
    frameId: 0,
    documentId: `document-${nextId}`,
    tab: { id: 1000 + nextId, url: `${ORIGIN}${pathname}` },
    ...senderOverrides,
  };
  if (senderOverrides.tab) {
    sender.tab = {
      id: 1000 + nextId,
      url: `${ORIGIN}${pathname}`,
      ...senderOverrides.tab,
    };
  }
  return {
    name: "bw-reader-provider",
    sender,
    messages,
    disconnected: false,
    onMessage: {
      addListener(listener) {
        messageListeners.push(listener);
      },
    },
    onDisconnect: {
      addListener(listener) {
        disconnectListeners.push(listener);
      },
    },
    postMessage(message) {
      messages.push(structuredClone(message));
    },
    disconnect() {
      this.disconnected = true;
      for (const listener of disconnectListeners) listener();
    },
    async receive(message) {
      const incoming = this.realmClone ? this.realmClone(message) : message;
      await Promise.all(
        messageListeners.map((listener) => Promise.resolve(listener(incoming))),
      );
    },
  };
}

function makeFetchPort(sender) {
  const messageListeners = [];
  const disconnectListeners = [];
  const messages = [];
  return {
    name: "bw-fetch",
    sender: structuredClone(sender),
    messages,
    disconnected: false,
    onMessage: {
      addListener(listener) {
        messageListeners.push(listener);
      },
    },
    onDisconnect: {
      addListener(listener) {
        disconnectListeners.push(listener);
      },
    },
    postMessage(message) {
      messages.push(structuredClone(message));
    },
    disconnect() {
      if (this.disconnected) return;
      this.disconnected = true;
      for (const listener of disconnectListeners) listener();
    },
    async receive(message) {
      const incoming = this.realmClone ? this.realmClone(message) : message;
      await Promise.all(
        messageListeners.map((listener) => Promise.resolve(listener(incoming))),
      );
    },
  };
}

function makeWsPort(sender) {
  const messageListeners = [];
  const disconnectListeners = [];
  const messages = [];
  return {
    name: "bw-ws",
    sender: structuredClone(sender),
    messages,
    disconnected: false,
    onMessage: {
      addListener(listener) {
        messageListeners.push(listener);
      },
    },
    onDisconnect: {
      addListener(listener) {
        disconnectListeners.push(listener);
      },
    },
    postMessage(message) {
      messages.push(structuredClone(message));
    },
    disconnect() {
      if (this.disconnected) return;
      this.disconnected = true;
      for (const listener of disconnectListeners) listener();
    },
    async receive(message) {
      const incoming = this.realmClone ? this.realmClone(message) : message;
      await Promise.all(
        messageListeners.map((listener) => Promise.resolve(listener(incoming))),
      );
    },
  };
}

function makeComputerVoiceDirectPort(sender) {
  const port = makeWsPort(sender);
  port.name = "BW_COMPUTER_VOICE_DIRECT_V2";
  return port;
}

function makeVocabularyPort(sender) {
  const port = makeFetchPort(sender);
  port.name = "bw-vocabulary-state";
  return port;
}

function makeDocumentNotePort(sender) {
  const port = makeFetchPort(sender);
  port.name = "bw-document-notes";
  return port;
}

function makeDirectHostPort(sender) {
  const port = makeFetchPort(sender);
  port.name = "bw-reader-direct-host";
  return port;
}

let contentSenderSequence = 0;
function ordinaryContentSender(
  url = "https://example.com/article",
  overrides = {},
) {
  const sequence = ++contentSenderSequence;
  return {
    id: "extension-test",
    frameId: 0,
    documentId: `ordinary-document-${sequence}`,
    tab: { id: 8000 + sequence, url },
    ...overrides,
  };
}

function computerVoiceDirectSender(
  url = "https://example.com/article",
  overrides = {},
) {
  return ordinaryContentSender(url, {
    url: new URL(url).href,
    ...overrides,
  });
}

function harness({
  authorizationGate = null,
  authorizationTimeoutImmediately = false,
  dataRegistry = createProviderRegistry(),
  networkHandler = null,
  nowMs = Date.now(),
  persistedStores = new Map(),
  persistedStoreEpochs = new Map(),
  storageState = {},
  enforceMutationJournal = false,
  enableSyncRuntime = true,
  syncRuntimeFactory = null,
  syncConflictControlFactory = null,
  ownerLeaseHandler = null,
} = {}) {
  const connectListeners = [];
  const messageListeners = [];
  const alarmListeners = [];
  const installedListeners = [];
  const startupListeners = [];
  const immediateTimeouts = new Set();
  let immediateTimeoutSequence = 0;
  const state = {
    stores: [],
    storageReads: [],
    storageState,
    authorizationRequests: 0,
    injections: [],
    networkRequests: [],
    activeTabId: 1,
    nowMs,
    privateCredentials: new Map(),
    webSockets: [],
    persistedStores,
    storeOptions: [],
    storeRecords: new Map(),
    storeOperations: [],
    closedStores: [],
    storeMutationJournals: new Map(),
    storeInstanceEpochs: persistedStoreEpochs,
    enforceMutationJournal,
    alarms: [],
    ownerLeaseRequests: [],
    ownerLeaseGeneration: 1,
    ownerLeaseManagers: [],
  };
  state.rebuildStoreInstance = (dbName) => {
    dbName = String(dbName || "");
    state.storeRecords.get(dbName)?.clear();
    const epoch = nextFakeStoreEpoch();
    state.storeInstanceEpochs.set(dbName, epoch);
    return epoch;
  };
  class FakeWebSocket {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;

    constructor(url) {
      this.url = String(url);
      this.readyState = FakeWebSocket.CONNECTING;
      this.binaryType = "blob";
      this.sent = [];
      this.closeCalls = [];
      this.onopen = null;
      this.onmessage = null;
      this.onerror = null;
      this.onclose = null;
      state.webSockets.push(this);
    }

    send(value) {
      if (this.readyState !== FakeWebSocket.OPEN) {
        throw new Error("fake WebSocket is not open");
      }
      if (typeof value === "string") {
        this.sent.push(value);
        return;
      }
      if (ArrayBuffer.isView(value)) {
        this.sent.push(new Uint8Array(
          value.buffer.slice(value.byteOffset, value.byteOffset + value.byteLength),
        ));
        return;
      }
      if (value instanceof ArrayBuffer) {
        this.sent.push(value.slice(0));
        return;
      }
      throw new TypeError("unsupported fake WebSocket frame");
    }

    close(code, reason) {
      this.closeCalls.push({
        code: code == null ? undefined : Number(code),
        reason: reason == null ? "" : String(reason),
      });
      if (this.readyState >= FakeWebSocket.CLOSING) return;
      this.readyState = FakeWebSocket.CLOSING;
    }

    open() {
      if (this.readyState !== FakeWebSocket.CONNECTING) return;
      this.readyState = FakeWebSocket.OPEN;
      this.onopen?.({ type: "open" });
    }

    receive(data) {
      if (this.readyState !== FakeWebSocket.OPEN) {
        throw new Error("fake WebSocket is not open");
      }
      this.onmessage?.({ type: "message", data });
    }

    networkError() {
      this.onerror?.({ type: "error" });
    }

    serverClose(code = 1000, reason = "", wasClean = true) {
      if (this.readyState === FakeWebSocket.CLOSED) return;
      this.readyState = FakeWebSocket.CLOSED;
      this.onclose?.({
        type: "close",
        code,
        reason,
        wasClean,
      });
    }
  }
  const privateCredentialDb = {
    objectStoreNames: {
      contains(name) {
        return name === "credentials";
      },
    },
    createObjectStore() {},
    transaction() {
      const tx = {
        error: null,
        objectStore() {
          return {
            get(key) {
              const request = { result: undefined, error: null };
              queueMicrotask(() => {
                request.result = state.privateCredentials.has(String(key))
                  ? structuredClone(state.privateCredentials.get(String(key)))
                  : undefined;
                request.onsuccess?.();
              });
              return request;
            },
            put(value, key) {
              state.privateCredentials.set(String(key), structuredClone(value));
              queueMicrotask(() => tx.oncomplete?.());
            },
          };
        },
      };
      return tx;
    },
  };
  const indexedDB = {
    open() {
      const request = { result: privateCredentialDb, error: null };
      queueMicrotask(() => {
        request.onupgradeneeded?.();
        request.onsuccess?.();
      });
      return request;
    },
  };
  class TestDate extends Date {
    static now() {
      return state.nowMs;
    }
  }
  const sandbox = {
    console,
    URL,
    Headers,
    AbortController,
    Date: TestDate,
    crypto: webcrypto,
    TextEncoder,
    TextDecoder,
    Response,
    ArrayBuffer,
    Uint8Array,
    Blob,
    WebSocket: FakeWebSocket,
    indexedDB,
    setTimeout(callback, delay, ...args) {
      if (authorizationTimeoutImmediately && delay === 12000) {
        const id = ++immediateTimeoutSequence;
        queueMicrotask(() => {
          if (!immediateTimeouts.has(id)) callback(...args);
        });
        return id;
      }
      const timer = setTimeout(callback, delay, ...args);
      if (enableSyncRuntime && typeof timer.unref === "function") timer.unref();
      return timer;
    },
    clearTimeout(id) {
      if (Number.isInteger(id) && id <= immediateTimeoutSequence) {
        immediateTimeouts.add(id);
        return;
      }
      clearTimeout(id);
    },
    structuredClone,
    btoa,
    atob,
    unescape,
    encodeURIComponent,
    fetch: async (url, init) => {
      if (url === `${ORIGIN}/api/reader/provider-authorize`) {
        state.authorizationRequests += 1;
        const body = JSON.parse(init.body);
        const authorizedIdentity =
          (body.namespace === NAMESPACE && body.ticket === TICKET) ||
          (body.namespace === OTHER_NAMESPACE && body.ticket === OTHER_TICKET);
        if (authorizationGate) {
          await Promise.race([
            authorizationGate,
            new Promise((_, reject) => {
              if (init.signal?.aborted) {
                reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
                return;
              }
              init.signal?.addEventListener("abort", () => {
                reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
              }, { once: true });
            }),
          ]);
        }
        return {
          ok: authorizedIdentity,
          headers: new Headers({ "Content-Type": "application/json" }),
          async json() {
            return {
              ok: authorizedIdentity,
              storage_namespace: body.namespace,
              expires_at: TICKET_EXPIRES_AT,
              expires_in: Math.max(1, TICKET_EXPIRES_AT - Math.floor(Date.now() / 1000)),
            };
          },
        };
      }
      state.networkRequests.push({ url, init });
      if (
        url === `${ORIGIN}/api/reader/sync/owner/claim` ||
        url === `${ORIGIN}/api/reader/sync/owner/renew` ||
        url === `${ORIGIN}/api/reader/sync/owner/release`
      ) {
        const body = JSON.parse(init.body);
        state.ownerLeaseRequests.push({
          url,
          body: structuredClone(body),
        });
        if (ownerLeaseHandler) {
          return ownerLeaseHandler(url, init, state);
        }
        if (url.endsWith("/release")) {
          return new Response(JSON.stringify({
            ok: true,
            contract: "owner-lease/1",
          }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        return new Response(JSON.stringify({
          ok: true,
          contract: "owner-lease/1",
          deviceId: body.deviceId,
          deviceFamilyId: body.deviceFamilyId,
          ownerRole: body.ownerRole,
          ownerInstanceId: body.ownerInstanceId,
          ownerGeneration: Number(body.ownerGeneration) ||
            state.ownerLeaseGeneration,
          ownerToken: `owner-token-v1-${"o".repeat(32)}`,
          expiresAt: Math.floor(state.nowMs / 1000) + 30,
        }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      if (networkHandler) return networkHandler(url, init, state);
      throw new Error("unexpected network request");
    },
    importScripts() {},
    BWReaderRuntime: {
      indexedDBStore: {
        createIndexedDBDataStore(options) {
          state.options = options;
          state.storeOptions.push(structuredClone(options));
          let records = persistedStores.get(options.dbName);
          if (!records) {
            records = new Map();
            persistedStores.set(options.dbName, records);
          }
          state.storeRecords.set(options.dbName, records);
          const store = createFakeStore(state, records, options);
          state.stores.push(store);
          return store;
        },
      },
    },
    chrome: {
      runtime: {
        id: "extension-test",
        getManifest: () => ({ version: "test" }),
        onConnect: {
          addListener(listener) {
            connectListeners.push(listener);
          },
        },
        onMessage: {
          addListener(listener) {
            messageListeners.push(listener);
          },
        },
        onInstalled: {
          addListener(listener) {
            installedListeners.push(listener);
          },
        },
        onStartup: {
          addListener(listener) {
            startupListeners.push(listener);
          },
        },
      },
      alarms: {
        create(name, options) {
          state.alarms.push({ name, options: structuredClone(options) });
        },
        onAlarm: {
          addListener(listener) {
            alarmListeners.push(listener);
          },
        },
      },
      storage: {
        local: {
          async get(key) {
            state.storageReads.push(key);
            if (key == null) return structuredClone(state.storageState);
            const keys = Array.isArray(key) ? key : [key];
            const result = {};
            for (const item of keys) {
              if (Object.hasOwn(state.storageState, item)) {
                result[item] = structuredClone(state.storageState[item]);
              }
            }
            return result;
          },
          async set(values) {
            Object.assign(state.storageState, structuredClone(values));
          },
          async remove(keys) {
            for (const key of Array.isArray(keys) ? keys : [keys]) {
              delete state.storageState[key];
            }
          },
        },
      },
      scripting: {
        async executeScript(options) {
          state.injections.push(structuredClone(options));
          return [];
        },
      },
      tabs: {
        async query() {
          return [{ id: state.activeTabId || 1 }];
        },
      },
    },
  };
  const context = vm.createContext(sandbox);
  state.realmClone = vm.runInContext(
    `(value) => {
      const seen = new WeakMap();
      const copy = (input) => {
        if (input === null || typeof input !== "object") return input;
        if (seen.has(input)) return seen.get(input);
        if (Array.isArray(input)) {
          const output = [];
          seen.set(input, output);
          for (let index = 0; index < input.length; index += 1) {
            if (Object.prototype.hasOwnProperty.call(input, index)) {
              output[index] = copy(input[index]);
            }
          }
          return output;
        }
        const output = {};
        seen.set(input, output);
        for (const key of Object.keys(input)) output[key] = copy(input[key]);
        return output;
      };
      return copy(value);
    }`,
    context,
  );
  vm.runInContext(ACCOUNT_CONTEXT_SOURCE, context, { filename: "account-context.js" });
  const defaultAccountContext = sandbox.BWReaderRuntime.accountContext;
  const accountContexts = [];
  sandbox.BWReaderRuntime.accountContext = Object.freeze({
    ...defaultAccountContext,
    createContext() {
      const created = defaultAccountContext.createContext();
      accountContexts.push(created);
      return created;
    },
  });
  vm.runInContext(
    EXTENSION_ACCOUNT_STORAGE_SOURCE,
    context,
    { filename: "extension-account-storage.js" },
  );
  vm.runInContext(
    INTERACTION_POLICY_SOURCE,
    context,
    { filename: "interaction-policy.js" },
  );
  vm.runInContext(
    VOCABULARY_STATE_SOURCE,
    context,
    { filename: "vocabulary-state.js" },
  );
  vm.runInContext(
    DATA_STORE_SOURCE,
    context,
    { filename: "data-store.js" },
  );
  state.accountContexts = accountContexts;
  state.defaultAccountContext = defaultAccountContext;
  if (dataRegistry) sandbox.BWReaderRuntime.dataRegistry = dataRegistry;
  if (enableSyncRuntime) {
    vm.runInContext(
      SYNC_GATEWAY_SOURCE,
      context,
      { filename: "sync-gateway.js" },
    );
    vm.runInContext(
      DIRECT_SYNC_PROTOCOL_SOURCE,
      context,
      { filename: "direct-sync-protocol.js" },
    );
    vm.runInContext(
      SYNC_COORDINATOR_SOURCE,
      context,
      { filename: "sync-coordinator.js" },
    );
    vm.runInContext(
      SYNC_RUNTIME_SOURCE,
      context,
      { filename: "sync-runtime.js" },
    );
    vm.runInContext(
      SYNC_OWNER_LEASE_SOURCE,
      context,
      { filename: "sync-owner-lease.js" },
    );
    const ownerLeaseApi = sandbox.BWReaderRuntime.syncOwnerLease;
    sandbox.BWReaderRuntime.syncOwnerLease = Object.freeze({
      ...ownerLeaseApi,
      createSyncOwnerLease(options) {
        const manager = ownerLeaseApi.createSyncOwnerLease(options);
        state.ownerLeaseManagers.push(manager);
        return manager;
      },
    });
    if (syncRuntimeFactory) {
      sandbox.BWReaderRuntime.syncRuntime = syncRuntimeFactory;
    }
    vm.runInContext(
      SYNC_CONFLICT_CONTROL_SOURCE,
      context,
      { filename: "sync-conflict-control.js" },
    );
    if (syncConflictControlFactory) {
      sandbox.BWReaderRuntime.syncConflictControl =
        syncConflictControlFactory;
    }
  }
  vm.runInContext(
    DOCUMENT_NOTE_REPOSITORY_SOURCE,
    context,
    { filename: "document-note-repository.js" },
  );
  vm.runInContext(SOURCE, context, { filename: "background.js" });
  const connect = (port) => {
    port.realmClone = state.realmClone;
    for (const listener of connectListeners) listener(port);
  };
  const sendRuntimeMessage = async (message, sender) => {
    return new Promise((resolve, reject) => {
      let settled = false;
      let keepChannel = false;
      const timer = setTimeout(() => {
        if (!settled) reject(new Error(`runtime message timeout: ${message.type}`));
      }, 2000);
      const finish = (value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(value);
      };
      for (const listener of messageListeners) {
        if (listener(message, sender, finish) === true) keepChannel = true;
      }
      if (!keepChannel && !settled) queueMicrotask(() => finish(undefined));
    });
  };
  return {
    connect,
    sendRuntimeMessage,
    state,
    fireAlarm(name = "bw-reader-provider-sync-v1") {
      for (const listener of alarmListeners) listener({ name });
    },
  };
}

function pageMessage(type, payload, id = null) {
  if (
    type === "HELLO" &&
    payload &&
    typeof payload === "object" &&
    !Object.hasOwn(payload, "syncOwnerClaim")
  ) {
    const hostKinds = {
      "/pdf/view": "pdf",
      "/pdf/epub/view": "epub",
      "/pdf/html/view": "html",
      "/pdf/fav/open": "favorite",
    };
    const registry = createProviderRegistry();
    payload = {
      ...payload,
      syncOwnerClaim: {
        contract: "pwa-extension-owner-claim/1",
        deviceFamilyId: DEVICE_FAMILY_ID,
        runtimeContract: "pwa-runtime/1",
        hostContract: "document-host/1",
        hostKind: hostKinds[String(payload.page || "")] || "",
        markerObserved: true,
        documentLifetime: true,
        pwaServerOwner: "paused",
        pwaDirectOwner: "paused",
        syncContract: registry.SYNC_CONTRACT,
        syncChangeContract: registry.SYNC_CHANGE_CONTRACT,
        registryDigest: registry.syncDigest(),
      },
    };
  }
  return {
    protocol: "bw-reader-services/1",
    direction: "page-to-extension",
    type,
    id,
    payload,
  };
}

function vocabularyCall(operation, payload, id) {
  return {
    protocol: "bw-vocabulary-state/1",
    type: "CALL",
    id,
    operation,
    payload,
  };
}

function documentNoteCall(operation, payload, id) {
  return {
    protocol: "bw-document-notes/1",
    type: "CALL",
    id,
    operation,
    payload,
  };
}

async function authorizePersistentAccount(h, namespace, ticket, pathname = "/pdf/view") {
  const provider = makePort(pathname);
  h.connect(provider);
  await provider.receive(pageMessage("HELLO", {
    namespace,
    ticket,
    page: pathname,
  }, `authorize-${namespace.slice(-6)}`));
  assert.equal(provider.messages.at(-1).type, "READY");
  return provider;
}

test("正式 manifest 在所有 http(s) 页面载入完整扩展，并只在四个书籍路由预装接管 marker", () => {
  const bookEntries = [
    `${ORIGIN}/pdf/epub/view`,
    `${ORIGIN}/pdf/epub/view?*`,
    `${ORIGIN}/pdf/fav/open`,
    `${ORIGIN}/pdf/fav/open?*`,
    `${ORIGIN}/pdf/html/view`,
    `${ORIGIN}/pdf/html/view?*`,
    `${ORIGIN}/pdf/view`,
    `${ORIGIN}/pdf/view?*`,
  ];
  assert.deepEqual(MANIFEST.host_permissions, [`${ORIGIN}/*`]);
  assert.deepEqual(
    [...MANIFEST.permissions].sort(),
    ["alarms", "nativeMessaging", "offscreen", "storage"],
  );
  assert.equal(MANIFEST.optional_permissions, undefined);
  assert.equal(MANIFEST.optional_host_permissions, undefined);
  assert.equal(MANIFEST.externally_connectable, undefined);
  assert.equal(MANIFEST.content_scripts.length, 2);
  const [marker, full] = MANIFEST.content_scripts;
  assert.deepEqual(
    Object.keys(marker).sort(),
    ["all_frames", "js", "matches", "run_at"],
  );
  assert.deepEqual(marker.js, ["src/pwa-marker.js"]);
  assert.deepEqual([...marker.matches].sort(), bookEntries);
  assert.equal(marker.run_at, "document_start");
  assert.equal(marker.all_frames, false);
  assert.deepEqual([...full.matches].sort(), ["http://*/*", "https://*/*"]);
  assert.equal(full.run_at, "document_idle");
  assert.equal(full.all_frames, false);
  assert.equal(full.js[0], "src/facade.js");
  assert.equal(full.js[1], "src/settings-sync.js");
  assert.equal(
    full.js.indexOf("vendor/reader-runtime-interaction-policy.js") <
      full.js.indexOf("vendor/reader-runtime-vocabulary-state.js"),
    true,
  );
  assert.ok(full.js.includes("src/web-adapter.js"));
  assert.ok(full.js.includes("src/web-highlights.js"));
  assert.ok(full.js.includes("src/web-pins.js"));
  assert.ok(full.js.includes("src/web-notes.js"));
  assert.ok(full.js.includes("src/web-ink.js"));
  assert.ok(full.js.includes("src/pwa-adapter.js"));
  assert.equal(full.js.at(-3), "src/shell.js");
  assert.equal(full.js.at(-2), "src/direct-sync-content-host.js");
  assert.equal(full.js.at(-1), "content.js");
  assert.equal(
    full.js.indexOf("vendor/reader-runtime-direct-sync-protocol.js") <
      full.js.indexOf("vendor/reader-runtime-direct-sync-host.js") &&
      full.js.indexOf("vendor/reader-runtime-direct-sync-signal-transport.js") <
      full.js.indexOf("vendor/reader-runtime-direct-sync-host.js") &&
      full.js.indexOf("vendor/reader-runtime-direct-sync-host.js") <
      full.js.indexOf("src/direct-sync-content-host.js"),
    true,
  );
  assert.equal(
    full.js.indexOf("src/pwa-adapter.js") < full.js.indexOf("src/web-adapter.js"),
    true,
  );
  assert.deepEqual(MANIFEST.background, {
    service_worker: "background.js",
    scripts: ["background.js"],
    persistent: false,
  });
});

test("完整静态注入文件都存在，后台不再保留旧动态 UI 注入入口", () => {
  const full = MANIFEST.content_scripts[1];
  for (const file of full.js) {
    assert.equal(
      existsSync(new URL(`../../extensions/bw-reader-webext/${file}`, import.meta.url)),
      true,
      file,
    );
  }
  assert.equal(
    existsSync(new URL(
      "../../extensions/bw-reader-webext/vendor/reader-runtime-document-note-repository.js",
      import.meta.url,
    )),
    true,
    "后台 document-note repository vendor",
  );
  assert.equal(SOURCE.includes("BW_LEGACY_BOOT"), false);
  assert.equal(SOURCE.includes("chrome.scripting.executeScript"), false);
});

test("复习快照只进扩展私有存储，并按当前已验证账户分区", async () => {
  const h = harness();
  await authorizePersistentAccount(h, NAMESPACE, TICKET);
  const sender = ordinaryContentSender();
  const first = { schema: 2, cards: [{ id: 10, question: "private" }] };
  const saved = await h.sendRuntimeMessage({
    type: "BW_LOCAL_STORAGE_SET",
    key: "reviewQueueV2",
    value: first,
  }, sender);
  assert.equal(saved.ok, true);
  assert.deepEqual(
    h.state.storageState[`reviewQueueV2:${NAMESPACE}`],
    first,
  );
  assert.equal(Object.hasOwn(h.state.storageState, "reviewQueueV2"), false);

  const readA = await h.sendRuntimeMessage({
    type: "BW_LOCAL_STORAGE_GET",
    key: "reviewQueueV2",
  }, sender);
  assert.deepEqual(readA.data, first);

  await authorizePersistentAccount(
    h,
    OTHER_NAMESPACE,
    OTHER_TICKET,
    "/pdf/epub/view",
  );
  const readB = await h.sendRuntimeMessage({
    type: "BW_LOCAL_STORAGE_GET",
    key: "reviewQueueV2",
  }, sender);
  assert.equal(readB.data, null);
});

test("页面卡片尺寸使用设备本地逐 cid 网关，不依赖已验证账户", async () => {
  const h = harness();
  const sender = ordinaryContentSender();
  const first = {
    w: 384,
    h: 256,
    updatedAt: 1720000000000,
    ignored: "不会进入持久化状态",
  };
  const expectedFirst = {
    w: 384,
    h: 256,
    updatedAt: 1720000000000,
  };
  const saved = await h.sendRuntimeMessage({
    type: "BW_PAGE_CARD_PRESENTATION_SET",
    cid: "anki:cid-01",
    value: first,
  }, sender);
  assert.equal(saved.ok, true);
  assert.deepEqual(structuredClone(saved.data), expectedFirst);
  assert.deepEqual(h.state.storageState.pageCardPresentationV1, {
    schema: 1,
    cards: {
      "anki:cid-01": expectedFirst,
    },
  });
  assert.equal(
    Object.keys(h.state.storageState).some(
      (key) => key.startsWith("pageCardPresentationV1:"),
    ),
    false,
  );

  const readA = await h.sendRuntimeMessage({
    type: "BW_PAGE_CARD_PRESENTATION_GET",
    cid: "anki:cid-01",
  }, sender);
  assert.equal(readA.ok, true);
  assert.deepEqual(structuredClone(readA.data), expectedFirst);

  // 即使后来有账户租约，页面 placement 尺寸仍是这台设备的本地 UI 状态，
  // 不能随账户 namespace 切换，也不能要求普通网页先建立 PWA 账户上下文。
  await authorizePersistentAccount(
    h,
    OTHER_NAMESPACE,
    OTHER_TICKET,
    "/pdf/epub/view",
  );
  const readB = await h.sendRuntimeMessage({
    type: "BW_PAGE_CARD_PRESENTATION_GET",
    cid: "anki:cid-01",
  }, sender);
  assert.equal(readB.ok, true);
  assert.deepEqual(structuredClone(readB.data), expectedFirst);

  const missing = await h.sendRuntimeMessage({
    type: "BW_PAGE_CARD_PRESENTATION_GET",
    cid: "tool:not-saved",
  }, sender);
  assert.equal(missing.ok, true);
  assert.equal(missing.data, null);
});

test("页面卡片尺寸按 cid 原子合并，并发标签页不丢记录且旧时间戳不覆盖新值", async () => {
  const h = harness();
  const senderA = ordinaryContentSender("https://example.com/one");
  const senderB = ordinaryContentSender("https://example.org/two");
  const first = { w: 360, h: 210, updatedAt: 1720000000100 };
  const second = { w: 640, h: 420, updatedAt: 1720000000200 };

  const [savedA, savedB] = await Promise.all([
    h.sendRuntimeMessage({
      type: "BW_PAGE_CARD_PRESENTATION_SET",
      cid: "anki:concurrent-a",
      value: first,
    }, senderA),
    h.sendRuntimeMessage({
      type: "BW_PAGE_CARD_PRESENTATION_SET",
      cid: "tool:concurrent-b",
      value: second,
    }, senderB),
  ]);
  assert.equal(savedA.ok, true);
  assert.equal(savedB.ok, true);
  assert.deepEqual(structuredClone(savedA.data), first);
  assert.deepEqual(structuredClone(savedB.data), second);
  assert.deepEqual(h.state.storageState.pageCardPresentationV1, {
    schema: 1,
    cards: {
      "anki:concurrent-a": first,
      "tool:concurrent-b": second,
    },
  });

  const newer = { w: 510, h: 330, updatedAt: 1720000000400 };
  const older = { w: 200, h: 120, updatedAt: 1720000000300 };
  const [newerResult, olderResult] = await Promise.all([
    h.sendRuntimeMessage({
      type: "BW_PAGE_CARD_PRESENTATION_SET",
      cid: "anki:concurrent-a",
      value: newer,
    }, senderA),
    h.sendRuntimeMessage({
      type: "BW_PAGE_CARD_PRESENTATION_SET",
      cid: "anki:concurrent-a",
      value: older,
    }, senderB),
  ]);
  assert.equal(newerResult.ok, true);
  assert.deepEqual(structuredClone(newerResult.data), newer);
  assert.equal(olderResult.ok, true);
  assert.deepEqual(
    structuredClone(olderResult.data),
    newer,
    "较旧的跨标签页抬手结果必须返回并保留当前较新记录",
  );
  assert.deepEqual(
    h.state.storageState.pageCardPresentationV1.cards["anki:concurrent-a"],
    newer,
  );
  assert.deepEqual(
    h.state.storageState.pageCardPresentationV1.cards["tool:concurrent-b"],
    second,
    "更新一个 cid 不得覆盖另一个 cid",
  );
});

test("页面卡片尺寸网关对非法 sender、编号、尺寸、旧整表入口和损坏存量 fail-closed", async () => {
  const h = harness();
  const sender = ordinaryContentSender();
  const baseline = { w: 320, h: 180, updatedAt: 1720000000000 };
  const saved = await h.sendRuntimeMessage({
    type: "BW_PAGE_CARD_PRESENTATION_SET",
    cid: "anki:baseline",
    value: baseline,
  }, sender);
  assert.equal(saved.ok, true);

  const invalidMessages = [
    {
      label: "保留编号",
      cid: "__proto__",
      value: { w: 320, h: 180, updatedAt: 1 },
    },
    {
      label: "含控制字符的编号",
      cid: "bad\ncid",
      value: { w: 320, h: 180, updatedAt: 1 },
    },
    {
      label: "宽度低于下限",
      cid: "anki:too-narrow",
      value: { w: 179, h: 180, updatedAt: 1 },
    },
    {
      label: "高度超过上限",
      cid: "anki:too-tall",
      value: { w: 320, h: 721, updatedAt: 1 },
    },
    {
      label: "非整数尺寸",
      cid: "anki:fractional",
      value: { w: 320.5, h: 180, updatedAt: 1 },
    },
    {
      label: "负更新时间",
      cid: "anki:negative-time",
      value: { w: 320, h: 180, updatedAt: -1 },
    },
  ];
  for (const sample of invalidMessages) {
    const rejected = await h.sendRuntimeMessage({
      type: "BW_PAGE_CARD_PRESENTATION_SET",
      cid: sample.cid,
      value: sample.value,
    }, sender);
    assert.equal(rejected.ok, false, sample.label);
    assert.equal(rejected.code, "BW_LOCAL_STORAGE_VALUE", sample.label);
    assert.deepEqual(
      h.state.storageState.pageCardPresentationV1,
      { schema: 1, cards: { "anki:baseline": baseline } },
      `${sample.label} 不得覆盖已验证状态`,
    );
  }

  const childSender = ordinaryContentSender(
    "https://example.com/frame",
    { frameId: 1 },
  );
  const childRejected = await h.sendRuntimeMessage({
    type: "BW_PAGE_CARD_PRESENTATION_GET",
    cid: "anki:baseline",
  }, childSender);
  assert.equal(childRejected.ok, false);
  assert.equal(childRejected.code, "BW_LOCAL_STORAGE_SENDER");

  const legacyRejected = await h.sendRuntimeMessage({
    type: "BW_LOCAL_STORAGE_SET",
    key: "cardPresentationV1",
    value: { schema: 1, cards: { "anki:legacy": baseline } },
  }, sender);
  assert.equal(legacyRejected.ok, false);
  assert.equal(legacyRejected.code, "BW_LOCAL_STORAGE_KEY");
  assert.equal(Object.hasOwn(h.state.storageState, "cardPresentationV1"), false);

  h.state.storageState.pageCardPresentationV1 = {
    schema: 1,
    cards: { "anki:corrupt": { w: 20, h: 180, updatedAt: 1 } },
  };
  const corruptRead = await h.sendRuntimeMessage({
    type: "BW_PAGE_CARD_PRESENTATION_GET",
    cid: "anki:corrupt",
  }, sender);
  assert.equal(corruptRead.ok, false);
  assert.equal(corruptRead.code, "BW_LOCAL_STORAGE_VALUE");
  assert.deepEqual(h.state.storageState.pageCardPresentationV1, {
    schema: 1,
    cards: { "anki:corrupt": { w: 20, h: 180, updatedAt: 1 } },
  });
});

test("词汇状态长连接按账户分区、跨标签广播，并在账户切换时撤销旧租约", async () => {
  const h = harness();
  await authorizePersistentAccount(h, NAMESPACE, TICKET);

  const senderA = ordinaryContentSender("https://example.com/first");
  const senderB = ordinaryContentSender("https://example.org/second");
  const first = makeVocabularyPort(senderA);
  const second = makeVocabularyPort(senderB);
  h.connect(first);
  h.connect(second);
  const firstReady = await waitForPortMessage(
    first,
    (message) => message.type === "READY",
    "first vocabulary READY",
  );
  const secondReady = await waitForPortMessage(
    second,
    (message) => message.type === "READY",
    "second vocabulary READY",
  );
  assert.match(firstReady.scope, /^vstate-scope-v1-[a-f0-9]{64}$/);
  assert.equal(secondReady.scope, firstReady.scope);
  assert.equal(JSON.stringify(firstReady).includes(NAMESPACE), false);

  const record = {
    id: "vstate-v1.mastered.word.en.YmU",
    schema: 1,
    property: "mastered",
    kind: "word",
    language: "en",
    key: "be",
    aliases: ["was"],
    enabled: true,
  };
  await first.receive(vocabularyCall("PUT", {
    record,
    mutationId: `vstate-mut-v1:${record.id}:00112233445566778899aabb`,
  }, "put-be"));
  const putResult = first.messages.find(
    (message) => message.type === "RESULT" && message.id === "put-be",
  );
  assert.equal(putResult.ok, true);
  assert.deepEqual(
    second.messages.find((message) => message.type === "CHANGE").record,
    record,
  );

  await second.receive(vocabularyCall("LIST", {
    query: { offset: 0, limit: 200 },
  }, "list-a"));
  const listA = second.messages.find(
    (message) => message.type === "RESULT" && message.id === "list-a",
  );
  assert.equal(listA.ok, true);
  assert.equal(listA.data.length, 1);
  assert.deepEqual(listA.data[0].value, record);

  await authorizePersistentAccount(
    h,
    OTHER_NAMESPACE,
    OTHER_TICKET,
    "/pdf/epub/view",
  );
  await settleBackground();
  assert.equal(first.disconnected, true);
  assert.equal(second.disconnected, true);
  assert.equal(
    first.messages.some((message) => message.type === "INVALIDATED"),
    true,
  );

  const other = makeVocabularyPort(ordinaryContentSender());
  h.connect(other);
  const otherReady = await waitForPortMessage(
    other,
    (message) => message.type === "READY",
    "other-account vocabulary READY",
  );
  assert.notEqual(otherReady.scope, firstReady.scope);
  await other.receive(vocabularyCall("LIST", { query: {} }, "list-b"));
  assert.deepEqual(
    other.messages.find((message) => message.id === "list-b").data,
    [],
  );
  other.sender.documentId = "navigated-document";
  await other.receive(vocabularyCall("LIST", { query: {} }, "stale-document"));
  assert.equal(other.disconnected, true);
  assert.equal(
    other.messages.some(
      (message) =>
        message.type === "INVALIDATED" &&
        message.reason === "sender-document-changed",
    ),
    true,
  );

  const child = makeVocabularyPort(ordinaryContentSender(
    "https://example.net/article",
    { frameId: 2 },
  ));
  h.connect(child);
  assert.equal(child.disconnected, true);
});

test("词汇状态 Vault 在扩展后台重启后恢复，且 mutation 与记录格式均 fail closed", async () => {
  const persistedStores = new Map();
  const storageState = {};
  const first = harness({ persistedStores, storageState });
  await authorizePersistentAccount(first, NAMESPACE, TICKET);
  const writer = makeVocabularyPort(ordinaryContentSender());
  first.connect(writer);
  await waitForPortMessage(
    writer,
    (message) => message.type === "READY",
    "writer vocabulary READY",
  );
  const record = {
    id: "vstate-v1.favorite.phrase.en.aW4gc3BpdGUgb2Y",
    schema: 1,
    property: "favorite",
    kind: "phrase",
    language: "en",
    key: "in spite of",
    aliases: [],
    enabled: true,
  };
  await writer.receive(vocabularyCall("PUT", {
    record,
    mutationId: `vstate-mut-v1:${record.id}:abcdef0123456789abcdef01`,
  }, "persist"));
  assert.equal(
    writer.messages.find((message) => message.id === "persist").ok,
    true,
  );

  const restarted = harness({ persistedStores, storageState });
  const reader = makeVocabularyPort(ordinaryContentSender());
  restarted.connect(reader);
  await waitForPortMessage(
    reader,
    (message) => message.type === "READY",
    "restarted vocabulary READY",
  );
  await reader.receive(vocabularyCall("LIST", { query: {} }, "after-restart"));
  const restored = reader.messages.find((message) => message.id === "after-restart");
  assert.equal(restored.ok, true);
  assert.deepEqual(restored.data.map((item) => item.value), [record]);

  await reader.receive(vocabularyCall("PUT", {
    record,
    mutationId: "unstable",
  }, "bad-mutation"));
  assert.equal(
    reader.messages.find((message) => message.id === "bad-mutation").code,
    "BW_VOCABULARY_STATE_MUTATION",
  );
  await reader.receive(vocabularyCall("PUT", {
    record: { ...record, id: record.id + ".spoof" },
    mutationId: `vstate-mut-v1:${record.id}:abcdef0123456789abcdef02`,
  }, "bad-record"));
  assert.equal(
    reader.messages.find((message) => message.id === "bad-record").code,
    "BW_VOCABULARY_STATE_ID",
  );
});

test("普通网页文档便签使用后台派生 documentId 完成 CRUD，并只向同账户同文档广播 CHANGE", async () => {
  const h = harness();
  await authorizePersistentAccount(h, NAMESPACE, TICKET);

  const first = makeDocumentNotePort(ordinaryContentSender(
    "https://example.com/article?edition=1#part-a",
  ));
  const sameDocument = makeDocumentNotePort(ordinaryContentSender(
    "https://example.com/article?edition=1#part-b",
  ));
  const otherDocument = makeDocumentNotePort(ordinaryContentSender(
    "https://example.com/article?edition=2",
  ));
  h.connect(first);
  h.connect(sameDocument);
  h.connect(otherDocument);
  const firstReady = await waitForPortMessage(
    first,
    (message) => message.type === "READY",
    "document notes first READY",
  );
  const sameReady = await waitForPortMessage(
    sameDocument,
    (message) => message.type === "READY",
    "document notes same-document READY",
  );
  const otherReady = await waitForPortMessage(
    otherDocument,
    (message) => message.type === "READY",
    "document notes other-document READY",
  );
  assert.equal(
    firstReady.documentId,
    "web:https://example.com/article?edition=1",
  );
  assert.equal(sameReady.documentId, firstReady.documentId);
  assert.notEqual(otherReady.documentId, firstReady.documentId);
  assert.match(firstReady.scope, /^document-notes-scope-v1-[a-f0-9]{64}$/);
  assert.equal(JSON.stringify(firstReady).includes(NAMESPACE), false);

  await first.receive(documentNoteCall("NEW_ID", {}, "new-id"));
  const noteId = first.messages.find((message) => message.id === "new-id").data;
  assert.match(noteId, /^c_[a-f0-9]{32}$/);

  const createPayload = {
    input: {
      noteId,
      anchor: {
        kind: "web-dom",
        revision: 1,
        data: { selector: "#chapter-one", dx: 12, dy: 30 },
      },
      text: "第一版便签",
      color: "yellow",
    },
    options: {
      mutationId: "note-create:test-crud-001",
    },
  };
  const createMessageOffset = first.messages.length;
  await first.receive(documentNoteCall("CREATE", createPayload, "create"));
  const created = first.messages.find((message) => message.id === "create");
  assert.equal(created.ok, true, JSON.stringify(created));
  assert.equal(created.data.documentId, firstReady.documentId);
  assert.equal(created.data.anchor.documentId, firstReady.documentId);
  assert.equal(created.data.rev, 1);
  assert.equal(created.data.text, "第一版便签");
  assert.equal(
    sameDocument.messages.some(
      (message) =>
        message.type === "CHANGE" &&
        message.data?.note?.noteId === noteId,
    ),
    true,
  );
  assert.equal(
    otherDocument.messages.some((message) => message.type === "CHANGE"),
    false,
  );
  const createFlow = first.messages.slice(createMessageOffset).filter(
    (message) =>
      (
        message.type === "CHANGE" &&
        message.data?.mutationId === "note-create:test-crud-001"
      ) ||
      (message.type === "RESULT" && message.id === "create"),
  );
  assert.deepEqual(
    createFlow.map((message) => message.type),
    ["CHANGE", "RESULT"],
    "本地提交先发布一次 CHANGE，再确认 RESULT",
  );

  const replayMessageOffset = first.messages.length;
  await first.receive(documentNoteCall(
    "CREATE",
    createPayload,
    "create-replay",
  ));
  const replayFlow = first.messages.slice(replayMessageOffset);
  assert.equal(
    replayFlow.filter((message) => message.type === "CHANGE").length,
    0,
    "相同 mutation 重放不能再次广播 CHANGE",
  );
  assert.equal(
    replayFlow.filter(
      (message) => message.type === "RESULT" && message.id === "create-replay",
    ).length,
    1,
    "相同 mutation 重放只返回一个 RESULT",
  );

  await sameDocument.receive(documentNoteCall("LIST", {
    query: { limit: 9999 },
  }, "list"));
  const listed = sameDocument.messages.find((message) => message.id === "list");
  assert.equal(listed.ok, true);
  assert.deepEqual(listed.data.map((note) => note.noteId), [noteId]);
  assert.equal(h.state.lastListQuery.limit, 200);

  await sameDocument.receive(documentNoteCall("GET", {
    noteId,
  }, "get"));
  assert.equal(
    sameDocument.messages.find((message) => message.id === "get").data.text,
    "第一版便签",
  );

  await first.receive(documentNoteCall("PATCH", {
    noteId,
    changes: { text: "第二版便签" },
    options: {
      ifRev: created.data.rev,
      mutationId: "note-patch:test-crud-002",
    },
  }, "patch"));
  const patched = first.messages.find((message) => message.id === "patch");
  assert.equal(patched.ok, true, JSON.stringify(patched));
  assert.equal(patched.data.rev, 2);
  assert.equal(patched.data.text, "第二版便签");

  await first.receive(documentNoteCall("REMOVE", {
    noteId,
    options: {
      ifRev: patched.data.rev,
      mutationId: "note-remove:test-crud-003",
    },
  }, "remove"));
  const removed = first.messages.find((message) => message.id === "remove");
  assert.equal(removed.ok, true, JSON.stringify(removed));
  assert.equal(removed.data.deleted, true);
  assert.equal(removed.data.rev, 3);

  await sameDocument.receive(documentNoteCall("LIST", {}, "list-after-remove"));
  assert.deepEqual(
    sameDocument.messages.find((message) => message.id === "list-after-remove").data,
    [],
  );
});

test("文档便签使用独立 DB/channel/journal，相同 mutationId 不与 provider 串扰", async () => {
  const h = harness({ enforceMutationJournal: true });
  const provider = await authorizePersistentAccount(h, NAMESPACE, TICKET);
  const sharedMutationId = "shared-mutation:provider-and-note-001";

  await provider.receive(pageMessage("CALL", {
    operation: "put",
    args: {
      collection: "user-settings",
      value: { id: "setting:shared-mutation", rawValue: "provider" },
      options: { mutationId: sharedMutationId },
    },
  }, "provider-shared-mutation"));
  assert.equal(
    provider.messages.find(
      (message) => message.type === "RESULT" &&
        message.id === "provider-shared-mutation",
    ).payload.ok,
    true,
  );

  const notes = makeDocumentNotePort(ordinaryContentSender(
    "https://example.com/independent-vault",
  ));
  h.connect(notes);
  await waitForPortMessage(
    notes,
    (message) => message.type === "READY",
    "independent document notes READY",
  );
  await notes.receive(documentNoteCall("CREATE", {
    input: {
      noteId: `c_${"1".repeat(32)}`,
      anchor: { kind: "web-dom", revision: 1, data: { selector: "main" } },
      text: "独立便签",
    },
    options: { mutationId: sharedMutationId },
  }, "note-shared-mutation"));
  assert.equal(
    notes.messages.find((message) => message.id === "note-shared-mutation").ok,
    true,
  );

  const providerDb = `bw-reader-extension-vault-v1-${NAMESPACE}`;
  const notesDb = `bw-reader-extension-document-notes-v1-${NAMESPACE}`;
  assert.deepEqual(
    h.state.storeOptions.map((options) => options.dbName).sort(),
    [notesDb, providerDb].sort(),
  );
  assert.notEqual(
    h.state.storeOptions.find((options) => options.dbName === providerDb)
      .channelName,
    h.state.storeOptions.find((options) => options.dbName === notesDb)
      .channelName,
  );
  assert.equal(
    [...h.state.storeRecords.get(providerDb).values()].some(
      (record) => record.collection === "document-notes",
    ),
    false,
  );
  assert.equal(
    [...h.state.storeRecords.get(notesDb).values()].some(
      (record) => record.collection === "user-settings",
    ),
    false,
  );
  assert.equal(
    provider.messages.some(
      (message) =>
        message.type === "CHANGE" &&
        message.payload?.collection === "document-notes",
    ),
    false,
    "便签写入不得进入 provider CHANGE/status/journal",
  );
  const sharedWrites = h.state.storeOperations.filter(
    (entry) =>
      entry.operation === "put" &&
      entry.details?.options?.mutationId === sharedMutationId,
  );
  assert.deepEqual(
    sharedWrites.map((entry) => entry.dbName).sort(),
    [notesDb, providerDb].sort(),
  );

  await authorizePersistentAccount(
    h,
    NAMESPACE,
    TICKET,
    "/pdf/html/view",
  );
  await settleBackground();
  assert.equal(notes.disconnected, false);
  assert.equal(h.state.closedStores.includes(notesDb), false);
  await notes.receive(documentNoteCall("LIST", {}, "same-account-list"));
  assert.deepEqual(
    notes.messages.find((message) => message.id === "same-account-list")
      .data.map((note) => note.text),
    ["独立便签"],
    "同账户重新验证不能淘汰仍被活动 port 使用的便签 Vault",
  );
});

test("文档便签拒绝页面伪造身份、子 frame、可信书籍 PWA 与连接期导航", async () => {
  const h = harness();
  await authorizePersistentAccount(h, NAMESPACE, TICKET);

  const pwa = makeDocumentNotePort(ordinaryContentSender(
    `${ORIGIN}/pdf/view?file=book.pdf`,
  ));
  h.connect(pwa);
  assert.equal(pwa.disconnected, true);
  assert.equal(
    pwa.messages.at(-1).code,
    "BW_DOCUMENT_NOTES_PWA_HOST_REQUIRED",
  );

  const child = makeDocumentNotePort(ordinaryContentSender(
    "https://example.com/article",
    { frameId: 2 },
  ));
  h.connect(child);
  assert.equal(child.disconnected, true);
  assert.equal(child.messages.at(-1).code, "BW_DOCUMENT_NOTES_SENDER");

  const port = makeDocumentNotePort(ordinaryContentSender(
    "https://example.com/article?edition=1",
  ));
  h.connect(port);
  await waitForPortMessage(
    port,
    (message) => message.type === "READY",
    "identity-fence document notes READY",
  );
  const attempts = [
    {
      id: "spoof-top",
      payload: {
        documentId: "web:https://evil.example/",
        input: {},
        options: {},
      },
    },
    {
      id: "spoof-input",
      payload: {
        input: {
          documentId: "web:https://evil.example/",
          anchor: { kind: "web-dom", revision: 1, data: {} },
        },
        options: { mutationId: "note-create:spoof-input" },
      },
    },
    {
      id: "spoof-anchor",
      payload: {
        input: {
          anchor: {
            documentId: "web:https://evil.example/",
            kind: "web-dom",
            revision: 1,
            data: {},
          },
        },
        options: { mutationId: "note-create:spoof-anchor" },
      },
    },
    {
      id: "spoof-url",
      payload: {
        url: "https://evil.example/",
        input: {},
        options: {},
      },
    },
  ];
  for (const attempt of attempts) {
    await port.receive(documentNoteCall("CREATE", attempt.payload, attempt.id));
    const result = port.messages.find((message) => message.id === attempt.id);
    assert.equal(result.ok, false, attempt.id);
    assert.equal(result.code, "BW_DOCUMENT_NOTES_IDENTITY", attempt.id);
  }

  port.sender.tab.url = "https://example.com/article?edition=2";
  await port.receive(documentNoteCall("LIST", {}, "after-navigation"));
  assert.equal(port.disconnected, true);
  assert.equal(
    port.messages.some(
      (message) =>
        message.type === "INVALIDATED" &&
        message.reason === "sender-document-changed",
    ),
    true,
  );
});

test("文档便签允许同一 Document 的同源 SPA sender.url 快照，但拒绝跨源错配", async () => {
  const h = harness();
  await authorizePersistentAccount(h, NAMESPACE, TICKET);

  const spaSender = ordinaryContentSender(
    "https://example.com/article?edition=2",
    { url: "https://example.com/article?edition=1" },
  );
  const spa = makeDocumentNotePort(spaSender);
  h.connect(spa);
  const ready = await waitForPortMessage(
    spa,
    (message) => message.type === "READY",
    "same-origin SPA document notes READY",
  );
  assert.equal(
    ready.documentId,
    "web:https://example.com/article?edition=2",
  );

  const crossOrigin = makeDocumentNotePort(ordinaryContentSender(
    "https://example.org/article?edition=2",
    { url: "https://example.com/article?edition=1" },
  ));
  h.connect(crossOrigin);
  assert.equal(crossOrigin.disconnected, true);
  assert.equal(
    crossOrigin.messages.at(-1).code,
    "BW_DOCUMENT_NOTES_SENDER",
  );
});

test("文档便签限制 mutation、请求和聚合响应大小", async () => {
  const h = harness();
  await authorizePersistentAccount(h, NAMESPACE, TICKET);
  const port = makeDocumentNotePort(ordinaryContentSender(
    "https://example.com/bounded-notes",
  ));
  h.connect(port);
  await waitForPortMessage(
    port,
    (message) => message.type === "READY",
    "bounded document notes READY",
  );

  await port.receive(documentNoteCall("CREATE", {
    input: {
      anchor: { kind: "web-dom", revision: 1, data: {} },
      text: "small",
    },
    options: { mutationId: "x".repeat(513) },
  }, "oversized-mutation"));
  assert.equal(
    port.messages.find((message) => message.id === "oversized-mutation").code,
    "BW_DOCUMENT_NOTES_MUTATION",
  );
  for (const [id, mutationId] of [
    ["numeric-mutation", 12345],
    ["blank-mutation", "   "],
  ]) {
    await port.receive(documentNoteCall("CREATE", {
      input: {
        anchor: { kind: "web-dom", revision: 1, data: {} },
        text: "strict mutation type",
      },
      options: { mutationId },
    }, id));
    assert.equal(
      port.messages.find((message) => message.id === id).code,
      "BW_DOCUMENT_NOTES_MUTATION",
      id,
    );
  }
  await port.receive(documentNoteCall("CREATE", {
    input: {
      anchor: { kind: "web-dom", revision: 1, data: {} },
      text: "string zero is not a revision",
    },
    options: {
      mutationId: "note-create:string-zero-revision",
      ifRev: "0",
    },
  }, "string-create-revision"));
  assert.equal(
    port.messages.find((message) => message.id === "string-create-revision").code,
    "BW_DOCUMENT_NOTES_REVISION",
  );

  const strictNoteId = `c_${"2".repeat(32)}`;
  await port.receive(documentNoteCall("CREATE", {
    input: {
      noteId: strictNoteId,
      anchor: { kind: "web-dom", revision: 1, data: {} },
      text: "strict revision source",
    },
    options: { mutationId: "note-create:strict-revision-source" },
  }, "strict-revision-source"));
  await port.receive(documentNoteCall("PATCH", {
    noteId: strictNoteId,
    changes: { text: "must not accept coerced revision" },
    options: {
      mutationId: "note-patch:string-revision",
      ifRev: "1",
    },
  }, "string-patch-revision"));
  assert.equal(
    port.messages.find((message) => message.id === "string-patch-revision").code,
    "BW_DOCUMENT_NOTES_REVISION",
  );

  for (const [id, invalidValue] of [
    ["undefined-field", undefined],
    ["nan-field", Number.NaN],
  ]) {
    await port.receive(documentNoteCall("CREATE", {
      input: {
        anchor: { kind: "web-dom", revision: 1, data: {} },
        text: invalidValue,
      },
      options: { mutationId: `note-create:${id}` },
    }, id));
    const result = port.messages.find((message) => message.id === id);
    assert.equal(result.ok, false, id);
    assert.equal(result.code, "BW_DATA_INVALID", id);
  }

  await port.receive(documentNoteCall("CREATE", {
    input: {
      anchor: { kind: "web-dom", revision: 1, data: {} },
      text: "x".repeat(600_000),
    },
    options: { mutationId: "note-create:oversized-request" },
  }, "oversized-request"));
  assert.equal(
    port.messages.find((message) => message.id === "oversized-request").code,
    "BW_DOCUMENT_NOTES_PAYLOAD",
  );

  for (let index = 0; index < 5; index += 1) {
    const noteId = `c_${index.toString(16).padStart(32, "0")}`;
    const id = `large-note-${index}`;
    await port.receive(documentNoteCall("CREATE", {
      input: {
        noteId,
        anchor: {
          kind: "web-dom",
          revision: 1,
          data: { selector: `#large-${index}` },
        },
        text: String(index).repeat(430_000),
      },
      options: { mutationId: `note-create:large-response-${index}` },
    }, id));
    assert.equal(
      port.messages.find((message) => message.id === id).ok,
      true,
      id,
    );
  }
  await port.receive(documentNoteCall("LIST", {}, "oversized-list-response"));
  const listResult = port.messages.find(
    (message) => message.id === "oversized-list-response",
  );
  assert.equal(listResult.ok, false);
  assert.equal(listResult.code, "BW_DOCUMENT_NOTES_PAYLOAD");
});

test("文档便签并发重复 request id 会失效连接，且明确回报未知写入结果", async () => {
  let releaseWrite;
  let signalWrite;
  const writeGate = new Promise((resolve) => { releaseWrite = resolve; });
  const writeStarted = new Promise((resolve) => { signalWrite = resolve; });
  const h = harness();
  await authorizePersistentAccount(h, NAMESPACE, TICKET);
  const port = makeDocumentNotePort(ordinaryContentSender(
    "https://example.com/duplicate-request",
  ));
  h.connect(port);
  await waitForPortMessage(
    port,
    (message) => message.type === "READY",
    "duplicate request document notes READY",
  );
  const notesDb = `bw-reader-extension-document-notes-v1-${NAMESPACE}`;
  h.state.beforeStoreOperation = async (operation, details) => {
    if (operation !== "put" || details.dbName !== notesDb) return;
    signalWrite();
    await writeGate;
  };
  const payload = {
    input: {
      noteId: `c_${"3".repeat(32)}`,
      anchor: { kind: "web-dom", revision: 1, data: { selector: "article" } },
      text: "only one physical write",
    },
    options: { mutationId: "note-create:duplicate-request-id-001" },
  };
  const pending = port.receive(documentNoteCall(
    "CREATE",
    payload,
    "duplicate-call-id",
  ));
  await writeStarted;
  await port.receive(documentNoteCall(
    "CREATE",
    payload,
    "duplicate-call-id",
  ));
  const invalidated = port.messages.find(
    (message) =>
      message.type === "INVALIDATED" &&
      message.reason === "duplicate-request-id",
  );
  assert.equal(port.disconnected, true);
  assert.equal(invalidated.details.outcomeUnknown, true);
  assert.equal(
    invalidated.details.mutationId,
    "note-create:duplicate-request-id-001",
  );
  assert.deepEqual(
    invalidated.details.mutationIds,
    ["note-create:duplicate-request-id-001"],
  );

  releaseWrite();
  await pending;
  await settleBackground();
  assert.equal(
    port.messages.filter(
      (message) => message.type === "RESULT" &&
        message.id === "duplicate-call-id",
    ).length,
    0,
    "连接失效后不能再发同 id 的晚到 RESULT",
  );
  assert.equal(
    h.state.storeOperations.filter(
      (entry) => entry.dbName === notesDb && entry.operation === "put",
    ).length,
    1,
  );
});

test("文档便签账户切换会撤销旧 port，并让新账户读取独立 Vault", async () => {
  const h = harness();
  await authorizePersistentAccount(h, NAMESPACE, TICKET);
  const first = makeDocumentNotePort(ordinaryContentSender(
    "https://example.com/account-scoped",
  ));
  h.connect(first);
  const firstReady = await waitForPortMessage(
    first,
    (message) => message.type === "READY",
    "account A document notes READY",
  );
  await first.receive(documentNoteCall("NEW_ID", {}, "new-account-a-note"));
  const noteId = first.messages.find(
    (message) => message.id === "new-account-a-note",
  ).data;
  await first.receive(documentNoteCall("CREATE", {
    input: {
      noteId,
      anchor: { kind: "web-dom", revision: 1, data: { selector: "main" } },
      text: "只属于账户 A",
    },
    options: { mutationId: "note-create:account-a-001" },
  }, "create-account-a"));
  assert.equal(
    first.messages.find((message) => message.id === "create-account-a").ok,
    true,
  );

  await authorizePersistentAccount(
    h,
    OTHER_NAMESPACE,
    OTHER_TICKET,
    "/pdf/html/view",
  );
  await settleBackground();
  assert.equal(first.disconnected, true);
  assert.equal(
    first.messages.some((message) => message.type === "INVALIDATED"),
    true,
  );

  const second = makeDocumentNotePort(ordinaryContentSender(
    "https://example.com/account-scoped",
  ));
  h.connect(second);
  const secondReady = await waitForPortMessage(
    second,
    (message) => message.type === "READY",
    "account B document notes READY",
  );
  assert.notEqual(secondReady.scope, firstReady.scope);
  await second.receive(documentNoteCall("LIST", {}, "list-account-b"));
  assert.deepEqual(
    second.messages.find((message) => message.id === "list-account-b").data,
    [],
  );
});

test("文档便签写入途中切号会丢弃迟到成功响应，记录只落原账户 Vault", async () => {
  let releaseWrite;
  let markWriteStarted;
  const writeGate = new Promise((resolve) => {
    releaseWrite = resolve;
  });
  const writeStarted = new Promise((resolve) => {
    markWriteStarted = resolve;
  });
  const h = harness();
  await authorizePersistentAccount(h, NAMESPACE, TICKET);
  const accountA = makeDocumentNotePort(ordinaryContentSender(
    "https://example.com/late-write",
  ));
  h.connect(accountA);
  await waitForPortMessage(
    accountA,
    (message) => message.type === "READY",
    "late-write account A READY",
  );
  await accountA.receive(documentNoteCall("NEW_ID", {}, "late-new-id"));
  const noteId = accountA.messages.find(
    (message) => message.id === "late-new-id",
  ).data;
  h.state.beforeStoreOperation = async (operation) => {
    if (operation !== "put") return;
    markWriteStarted();
    await writeGate;
  };
  const pendingWrite = accountA.receive(documentNoteCall("CREATE", {
    input: {
      noteId,
      anchor: { kind: "web-dom", revision: 1, data: { selector: "article" } },
      text: "账户 A 的迟到写入",
    },
    options: { mutationId: "note-create:late-account-a-001" },
  }, "late-create"));
  await writeStarted;

  await authorizePersistentAccount(
    h,
    OTHER_NAMESPACE,
    OTHER_TICKET,
    "/pdf/epub/view",
  );
  await settleBackground();
  const staleWriteInvalidation = accountA.messages.find(
    (message) => message.type === "INVALIDATED",
  );
  assert.equal(staleWriteInvalidation.details.outcomeUnknown, true);
  assert.equal(
    staleWriteInvalidation.details.mutationId,
    "note-create:late-account-a-001",
  );
  assert.deepEqual(
    staleWriteInvalidation.details.mutationIds,
    ["note-create:late-account-a-001"],
  );
  const accountADocumentDb =
    `bw-reader-extension-document-notes-v1-${NAMESPACE}`;
  assert.equal(
    h.state.closedStores.includes(accountADocumentDb),
    false,
    "仍有写入时不能关闭旧账户便签 Vault",
  );
  releaseWrite();
  await pendingWrite;
  await settleBackground();
  assert.equal(accountA.disconnected, true);
  assert.equal(
    accountA.messages.some(
      (message) => message.type === "RESULT" && message.id === "late-create",
    ),
    false,
  );
  assert.equal(
    h.state.closedStores.includes(accountADocumentDb),
    true,
    "旧账户 port 与写入都结束后安全淘汰便签 Vault",
  );

  h.state.beforeStoreOperation = null;
  const accountB = makeDocumentNotePort(ordinaryContentSender(
    "https://example.com/late-write",
  ));
  h.connect(accountB);
  await waitForPortMessage(
    accountB,
    (message) => message.type === "READY",
    "late-write account B READY",
  );
  await accountB.receive(documentNoteCall("LIST", {}, "list-account-b-late"));
  assert.deepEqual(
    accountB.messages.find(
      (message) => message.id === "list-account-b-late",
    ).data,
    [],
  );

  await authorizePersistentAccount(h, NAMESPACE, TICKET, "/pdf/html/view");
  const accountAAgain = makeDocumentNotePort(ordinaryContentSender(
    "https://example.com/late-write",
  ));
  h.connect(accountAAgain);
  await waitForPortMessage(
    accountAAgain,
    (message) => message.type === "READY",
    "late-write account A restored READY",
  );
  await accountAAgain.receive(documentNoteCall(
    "LIST",
    {},
    "list-account-a-restored",
  ));
  assert.deepEqual(
    accountAAgain.messages.find(
      (message) => message.id === "list-account-a-restored",
    ).data.map((note) => note.noteId),
    [noteId],
  );
});

test("provider 只接受四个正式书籍 PWA 入口，明确拒绝退役网页壳和第三方代理页", () => {
  const h = harness();
  for (const path of [
    "/pdf/web/live?url=https://example.com",
    "/pdf/web/proxy?url=https://example.com",
    "/pdf/web/p/https/example.com/",
    "/pdf/web/frame",
    "/pdf/api/ping",
  ]) {
    const port = makePort(path);
    h.connect(port);
    assert.equal(port.disconnected, true, path);
  }
  const trusted = makePort("/pdf/view?file=book.pdf");
  h.connect(trusted);
  assert.equal(trusted.disconnected, false);
  const favorite = makePort("/pdf/fav/open?id=f_test");
  h.connect(favorite);
  assert.equal(favorite.disconnected, false);
});

test("provider port 绑定顶层 tab/frame/document，Safari 缺 documentId 时仍以 port 生命周期隔离", async () => {
  const h = harness();
  const childFrame = makePort("/pdf/view", "extension-test", { frameId: 3 });
  h.connect(childFrame);
  assert.equal(childFrame.disconnected, true);

  const missingTab = makePort("/pdf/view", "extension-test", { tab: null });
  h.connect(missingTab);
  assert.equal(missingTab.disconnected, true);

  const safari = makePort("/pdf/view", "extension-test", { documentId: undefined });
  h.connect(safari);
  assert.equal(safari.disconnected, false);

  const changedDocument = makePort("/pdf/view");
  h.connect(changedDocument);
  changedDocument.sender.documentId = "document-after-navigation";
  await changedDocument.receive(pageMessage("HELLO", {
    namespace: NAMESPACE,
    ticket: TICKET,
    page: "/pdf/view",
  }, "changed-document"));
  assert.equal(
    changedDocument.messages.find((message) => message.type === "ERROR").payload.code,
    "BW_PROVIDER_SENDER",
  );
  assert.equal(h.state.authorizationRequests, 0);
});

test("DataRegistry 的 provider/sync-v3 因果合同缺失、未知或不一致时后台启动即 fail closed", () => {
  const cases = [
    ["missing-registry", null],
    ["missing-provider-collections", createProviderRegistry({ providerCollections: undefined })],
    ["missing-sync-descriptor", createProviderRegistry({ syncDescriptor: undefined })],
    ["missing-sync-digest", createProviderRegistry({ syncDigest: undefined })],
    ["old-sync-contract", createProviderRegistry({ SYNC_CONTRACT: "sync-v1" })],
    ["missing-change-contract", createProviderRegistry({ SYNC_CHANGE_CONTRACT: undefined })],
    ["unknown-provider-collection", createProviderRegistry({
      providerCollections: () => [
        "dictionary-cache",
        "query-cache",
        "translation-cache",
        "unknown-provider",
        "user-settings",
        "vocabulary-state",
      ],
    })],
    ["incomplete-provider-collections", createProviderRegistry({
      providerCollections: () => ["user-settings"],
    })],
    ["inconsistent-sync-descriptor", createProviderRegistry({
      syncDescriptor: () => [{
        name: "user-settings",
        conflictPolicy: "last-write-wins",
        derived: false,
        recordSchema: 1,
      }, {
        name: "vocabulary-state",
        conflictPolicy: "explicit",
        derived: false,
        recordSchema: 1,
      }],
    })],
    ["forged-sync-digest", createProviderRegistry({
      syncDigest: () => "sync-v3:forged",
    })],
  ];
  for (const [label, dataRegistry] of cases) {
    assert.throws(
      () => harness({ dataRegistry }),
      (error) => error?.code === "BW_PROVIDER_REGISTRY",
      label,
    );
  }
});

test("握手绑定真实页面与不透明账户编号，Safari/Chrome Vault 不再使用 32 位散列", async () => {
  const h = harness();
  const port = makePort("/pdf/view?file=book.pdf");
  h.connect(port);
  await port.receive(pageMessage("HELLO", {
    namespace: NAMESPACE,
    ticket: TICKET,
    page: "/pdf/view",
  }, "hello-1"));
  const ready = port.messages.find((message) => message.type === "READY");
  assert.equal(ready.id, "hello-1");
  assert.equal(ready.payload.dataStore.contract, "data-store/1");
  assert.deepEqual(
    ready.payload.capabilities.providerCollections,
    ["dictionary-cache", "query-cache", "translation-cache", "user-settings", "vocabulary-state"],
  );
  assert.equal(ready.payload.capabilities.networkOperations, false);
  assert.deepEqual(
    ready.payload.capabilities.syncCollections,
    ["user-settings", "vocabulary-state"],
  );
  assert.deepEqual(
    ready.payload.capabilities.syncDescriptor,
    [{
      name: "user-settings",
      conflictPolicy: "explicit",
      derived: false,
      recordSchema: 1,
    }, {
      name: "vocabulary-state",
      conflictPolicy: "explicit",
      derived: false,
      recordSchema: 1,
    }],
  );
  assert.equal(
    ready.payload.capabilities.syncDigest,
    "sync-v3:record-parent-state/1|" +
      "user-settings:explicit:0:1|vocabulary-state:explicit:0:1",
  );
  assert.equal(ready.payload.capabilities.syncContract, "sync-v3");
  assert.equal(
    ready.payload.capabilities.syncChangeContract,
    "record-parent-state/1",
  );
  assert.equal(JSON.stringify(ready).includes("must-never-leave-background"), false);
  assert.equal(h.state.storageReads.includes("apiToken"), false);
  assert.equal(h.state.authorizationRequests, 1);
  assert.equal(h.state.options.dbName.endsWith(NAMESPACE), true);
  assert.match(h.state.options.deviceId, /^extension-install-v1-[a-f0-9]{32}$/);
  assert.equal(h.state.options.deviceId.includes(NAMESPACE), false);

  const wrongPage = makePort("/pdf/epub/view?file=book.epub");
  h.connect(wrongPage);
  await wrongPage.receive(pageMessage("HELLO", {
    namespace: NAMESPACE,
    ticket: TICKET,
    page: "/pdf/view",
  }, "hello-bad-page"));
  assert.equal(
    wrongPage.messages.find((message) => message.type === "ERROR").payload.code,
    "BW_PROVIDER_PAGE",
  );

  const cached = makePort("/pdf/html/view?file=page.html");
  h.connect(cached);
  await cached.receive(pageMessage("HELLO", {
    namespace: NAMESPACE,
    ticket: TICKET,
    page: "/pdf/html/view",
  }, "hello-cached"));
  assert.equal(cached.messages.at(-1).type, "READY");
  assert.equal(h.state.authorizationRequests, 1);
});

test("已知 namespace 没有对应服务器证明也不能打开旧账户 Vault", async () => {
  const h = harness();
  const port = makePort("/pdf/view");
  h.connect(port);
  await port.receive(pageMessage("HELLO", {
    namespace: NAMESPACE,
    ticket: `pvt-v2-${TICKET_EXPIRES_AT}-${"d".repeat(32)}-${"d".repeat(64)}`,
    page: "/pdf/view",
  }, "wrong-ticket"));
  const error = port.messages.find((message) => message.type === "ERROR");
  assert.equal(error.payload.code, "BW_PROVIDER_AUTH");
  assert.equal(h.state.stores.length, 0);

  await port.receive(pageMessage("CALL", {
    operation: "get",
    args: { collection: "user-settings", id: "setting:secret" },
  }, "call-after-denied"));
  const deniedCall = port.messages.find(
    (message) => message.type === "RESULT" && message.id === "call-after-denied",
  );
  assert.equal(deniedCall.payload.code, "BW_PROVIDER_AUTH");
  assert.equal(h.state.stores.length, 0);
});

test("provider 授权缓存到期后必须重新在线核验", async () => {
  const h = harness();
  const first = makePort("/pdf/view");
  h.connect(first);
  await first.receive(pageMessage("HELLO", {
    namespace: NAMESPACE,
    ticket: TICKET,
    page: "/pdf/view",
  }, "first"));
  assert.equal(h.state.authorizationRequests, 1);

  const cache = h.state.storageState.providerNamespaceAuthorizationsV2;
  assert.equal(cache[NAMESPACE].expiresAt, TICKET_EXPIRES_AT);
  cache[NAMESPACE].validUntilMs = h.state.nowMs - 1;

  const second = makePort("/pdf/epub/view");
  h.connect(second);
  await second.receive(pageMessage("HELLO", {
    namespace: NAMESPACE,
    ticket: TICKET,
    page: "/pdf/epub/view",
  }, "second"));
  assert.equal(h.state.authorizationRequests, 2);
  assert.equal(second.messages.at(-1).type, "READY");
});

test("已授权 port 到期后撤销 namespace，后续 CALL 不再访问 Vault", async () => {
  const h = harness();
  const port = makePort("/pdf/view");
  h.connect(port);
  await port.receive(pageMessage("HELLO", {
    namespace: NAMESPACE,
    ticket: TICKET,
    page: "/pdf/view",
  }, "active"));
  assert.equal(port.messages.at(-1).type, "READY");

  h.state.nowMs = TICKET_EXPIRES_AT * 1000 + 1;
  await port.receive(pageMessage("CALL", {
    operation: "status",
    args: {},
  }, "expired-call"));
  const expired = port.messages.find(
    (message) => message.type === "RESULT" && message.id === "expired-call",
  );
  assert.equal(expired.payload.code, "BW_PROVIDER_AUTH_EXPIRED");

  await port.receive(pageMessage("CALL", {
    operation: "status",
    args: {},
  }, "after-expired"));
  const revoked = port.messages.find(
    (message) => message.type === "RESULT" && message.id === "after-expired",
  );
  assert.equal(revoked.payload.code, "BW_PROVIDER_AUTH");
});

test("扩展安装编号跨后台重启持久复用，不由账户 namespace 派生", async () => {
  const sharedStorage = {};
  const first = harness({ storageState: sharedStorage });
  const firstPort = makePort("/pdf/view");
  first.connect(firstPort);
  await firstPort.receive(pageMessage("HELLO", {
    namespace: NAMESPACE,
    ticket: TICKET,
    page: "/pdf/view",
  }, "first-install"));
  const firstInstallId = first.state.options.deviceId;
  assert.match(firstInstallId, /^extension-install-v1-[a-f0-9]{32}$/);
  assert.equal(firstInstallId.includes(NAMESPACE), false);

  const restarted = harness({ storageState: sharedStorage });
  const restartedPort = makePort("/pdf/html/view");
  restarted.connect(restartedPort);
  await restartedPort.receive(pageMessage("HELLO", {
    namespace: NAMESPACE,
    ticket: TICKET,
    page: "/pdf/html/view",
  }, "restarted-install"));
  assert.equal(restarted.state.options.deviceId, firstInstallId);
  assert.equal(restarted.state.authorizationRequests, 0);

  const otherInstall = harness();
  const otherPort = makePort("/pdf/fav/open");
  otherInstall.connect(otherPort);
  await otherPort.receive(pageMessage("HELLO", {
    namespace: NAMESPACE,
    ticket: TICKET,
    page: "/pdf/fav/open",
  }, "other-install"));
  assert.notEqual(otherInstall.state.options.deviceId, firstInstallId);
});

test("provider 授权完成前 CALL 不得抢先打开账户 Vault", async () => {
  let releaseAuthorization;
  const gate = new Promise((resolve) => {
    releaseAuthorization = resolve;
  });
  const h = harness({ authorizationGate: gate });
  const port = makePort("/pdf/view");
  h.connect(port);

  const pendingHello = port.receive(pageMessage("HELLO", {
    namespace: NAMESPACE,
    ticket: TICKET,
    page: "/pdf/view",
  }, "hello-pending"));
  await tick();
  await port.receive(pageMessage("HELLO", {
    namespace: `acct-v1-${"e".repeat(64)}`,
    ticket: TICKET,
    page: "/pdf/view",
  }, "hello-overlap"));
  const overlappingHello = port.messages.find(
    (message) => message.type === "ERROR" && message.id === "hello-overlap",
  );
  assert.equal(overlappingHello.payload.code, "BW_PROVIDER_AUTH_PENDING");

  await port.receive(pageMessage("CALL", {
    operation: "get",
    args: { collection: "user-settings", id: "setting:secret" },
  }, "call-during-auth"));

  const earlyCall = port.messages.find(
    (message) => message.type === "RESULT" && message.id === "call-during-auth",
  );
  assert.equal(earlyCall.payload.code, "BW_PROVIDER_AUTH_PENDING");
  assert.equal(h.state.stores.length, 0);

  releaseAuthorization();
  await pendingHello;
  assert.equal(port.messages.some((message) => message.type === "READY"), true);
  assert.equal(h.state.options.dbName.endsWith(NAMESPACE), true);
});

test("两个页面并发握手同一 namespace 只创建一个 Vault/store 订阅", async () => {
  let releaseAuthorization;
  const gate = new Promise((resolve) => {
    releaseAuthorization = resolve;
  });
  const h = harness({ authorizationGate: gate });
  const first = makePort("/pdf/view");
  const second = makePort("/pdf/html/view");
  h.connect(first);
  h.connect(second);

  const firstHello = first.receive(pageMessage("HELLO", {
    namespace: NAMESPACE,
    ticket: TICKET,
    page: "/pdf/view",
  }, "parallel-first"));
  const secondHello = second.receive(pageMessage("HELLO", {
    namespace: NAMESPACE,
    ticket: TICKET,
    page: "/pdf/html/view",
  }, "parallel-second"));
  await tick();
  releaseAuthorization();
  await Promise.all([firstHello, secondHello]);

  assert.equal(first.messages.at(-1).type, "READY");
  assert.equal(second.messages.at(-1).type, "READY");
  assert.equal(h.state.stores.length, 1);
});

test("每个 provider port 使用独立 AccountContext，并另保留一个跨普通网页的持久账户 context", async () => {
  const h = harness();
  const first = makePort("/pdf/view");
  const second = makePort("/pdf/html/view");
  h.connect(first);
  h.connect(second);
  assert.equal(h.state.accountContexts.length, 3);
  assert.equal(h.state.defaultAccountContext.snapshot().active, false);

  await first.receive(pageMessage("HELLO", {
    namespace: NAMESPACE,
    ticket: TICKET,
    page: "/pdf/view",
  }, "context-first"));
  await second.receive(pageMessage("HELLO", {
    namespace: NAMESPACE,
    ticket: TICKET,
    page: "/pdf/html/view",
  }, "context-second"));
  assert.equal(h.state.accountContexts[0].snapshot().active, true, "持久账户已由可信票据激活");
  assert.equal(h.state.accountContexts[1].snapshot().active, true);
  assert.equal(h.state.accountContexts[2].snapshot().active, true);

  first.disconnect();
  assert.equal(h.state.accountContexts[0].snapshot().active, true, "PWA 端口断开不应让普通网页失去已验证账户");
  assert.equal(h.state.accountContexts[1].snapshot().active, false);
  assert.equal(h.state.accountContexts[2].snapshot().active, true);
  assert.equal(h.state.defaultAccountContext.snapshot().active, false);
});

test("HELLO 授权等待期间断开 port，晚到授权不能激活账户、打开 Vault 或发布 READY", async () => {
  let releaseAuthorization;
  const authorizationGate = new Promise((resolve) => {
    releaseAuthorization = resolve;
  });
  const h = harness({ authorizationGate });
  const port = makePort("/pdf/view");
  h.connect(port);
  const pendingHello = port.receive(pageMessage("HELLO", {
    namespace: NAMESPACE,
    ticket: TICKET,
    page: "/pdf/view",
  }, "disconnect-during-hello"));
  await tick();
  port.disconnect();
  releaseAuthorization();
  await pendingHello;

  assert.equal(port.messages.some((message) => message.type === "READY"), false);
  assert.equal(h.state.stores.length, 0);
  assert.equal(h.state.accountContexts[1].snapshot().active, false);
  assert.equal(h.state.accountContexts[0].snapshot().active, false);
});

test("同一 port 重新握手会使旧 CALL 租约失效，晚到读取结果不会越过新代际", async () => {
  const h = harness();
  const port = makePort("/pdf/view");
  h.connect(port);
  await port.receive(pageMessage("HELLO", {
    namespace: NAMESPACE,
    ticket: TICKET,
    page: "/pdf/view",
  }, "generation-first"));

  let releaseList;
  let signalListStarted;
  const listStarted = new Promise((resolve) => { signalListStarted = resolve; });
  const listGate = new Promise((resolve) => { releaseList = resolve; });
  h.state.listResult = [{ id: "old-account-secret" }];
  h.state.beforeStoreOperation = async (operation) => {
    if (operation !== "list") return;
    signalListStarted();
    await listGate;
  };
  const pendingCall = port.receive(pageMessage("CALL", {
    operation: "list",
    args: { collection: "user-settings", query: { limit: 1 } },
  }, "old-generation-call"));
  await listStarted;

  await port.receive(pageMessage("HELLO", {
    namespace: NAMESPACE,
    ticket: TICKET,
    page: "/pdf/view",
  }, "generation-second"));
  releaseList();
  await pendingCall;

  const stale = port.messages.find((message) => message.id === "old-generation-call");
  assert.equal(stale.payload.ok, false);
  assert.notEqual(stale.payload.result?.[0]?.id, "old-account-secret");
  assert.ok(
    ["BW_PROVIDER_DISCONNECTED", "BW_ACCOUNT_CONTEXT_STALE"].includes(stale.payload.code),
    stale.payload.code,
  );
  assert.equal(
    port.messages.filter((message) => message.type === "READY").length,
    2,
  );
});

test("账户切换期间的晚到写只落原 lease Vault，返回 outcome unknown 且绝不写入新账户", async () => {
  const h = harness();
  const port = makePort("/pdf/view");
  h.connect(port);
  await port.receive(pageMessage("HELLO", {
    namespace: NAMESPACE,
    ticket: TICKET,
    page: "/pdf/view",
  }, "write-account-a"));

  let releasePut;
  let signalPutStarted;
  const putStarted = new Promise((resolve) => { signalPutStarted = resolve; });
  const putGate = new Promise((resolve) => { releasePut = resolve; });
  h.state.beforeStoreOperation = async (operation) => {
    if (operation !== "put") return;
    signalPutStarted();
    await putGate;
  };
  const pendingWrite = port.receive(pageMessage("CALL", {
    operation: "put",
    args: {
      collection: "user-settings",
      value: { id: "setting:account-a-only", rawValue: "A" },
      options: { mutationId: "mutation-account-a-only" },
    },
  }, "old-account-write"));
  await putStarted;

  await port.receive(pageMessage("HELLO", {
    namespace: OTHER_NAMESPACE,
    ticket: OTHER_TICKET,
    page: "/pdf/view",
  }, "switch-to-account-b"));
  releasePut();
  await pendingWrite;

  const staleWrite = port.messages.find((message) => message.id === "old-account-write");
  assert.equal(staleWrite.payload.ok, false);
  assert.equal(staleWrite.payload.details.outcomeUnknown, true);
  assert.equal(staleWrite.payload.details.mutationId, "mutation-account-a-only");

  h.state.beforeStoreOperation = null;
  await port.receive(pageMessage("CALL", {
    operation: "get",
    args: { collection: "user-settings", id: "setting:account-a-only" },
  }, "read-from-account-b"));
  assert.equal(
    port.messages.find((message) => message.id === "read-from-account-b").payload.result,
    null,
  );

  await port.receive(pageMessage("HELLO", {
    namespace: NAMESPACE,
    ticket: TICKET,
    page: "/pdf/view",
  }, "switch-back-to-account-a"));
  await port.receive(pageMessage("CALL", {
    operation: "get",
    args: { collection: "user-settings", id: "setting:account-a-only" },
  }, "read-from-account-a"));
  assert.equal(
    port.messages.find((message) => message.id === "read-from-account-a")
      .payload.result.value.rawValue,
    "A",
  );
});

test("store 操作期间授权到期，最终围栏拒绝旧结果并撤销当前 context", async () => {
  const h = harness();
  const port = makePort("/pdf/view");
  h.connect(port);
  await port.receive(pageMessage("HELLO", {
    namespace: NAMESPACE,
    ticket: TICKET,
    page: "/pdf/view",
  }, "expiry-first"));

  let releaseList;
  let signalListStarted;
  const listStarted = new Promise((resolve) => { signalListStarted = resolve; });
  const listGate = new Promise((resolve) => { releaseList = resolve; });
  h.state.listResult = [{ id: "must-not-return-after-expiry" }];
  h.state.beforeStoreOperation = async (operation) => {
    if (operation !== "list") return;
    signalListStarted();
    await listGate;
  };
  const pendingCall = port.receive(pageMessage("CALL", {
    operation: "list",
    args: { collection: "user-settings", query: { limit: 1 } },
  }, "expiry-call"));
  await listStarted;
  h.state.nowMs = TICKET_EXPIRES_AT * 1000 + 1;
  releaseList();
  await pendingCall;

  const expired = port.messages.find((message) => message.id === "expiry-call");
  assert.equal(expired.payload.ok, false);
  assert.equal(expired.payload.code, "BW_PROVIDER_AUTH_EXPIRED");
  assert.equal(h.state.accountContexts[1].snapshot().active, false);
  assert.equal(h.state.accountContexts[0].snapshot().active, true);
});

test("provider 授权 fetch 超时会中止并返回可重试的 unavailable", async () => {
  const never = new Promise(() => {});
  const h = harness({
    authorizationGate: never,
    authorizationTimeoutImmediately: true,
  });
  const port = makePort("/pdf/view");
  h.connect(port);
  await port.receive(pageMessage("HELLO", {
    namespace: NAMESPACE,
    ticket: TICKET,
    page: "/pdf/view",
  }, "auth-timeout"));

  const error = port.messages.find(
    (message) => message.type === "ERROR" && message.id === "auth-timeout",
  );
  assert.equal(error.payload.code, "BW_PROVIDER_AUTH_UNAVAILABLE");
  assert.match(error.payload.error, /超时/);
  assert.equal(h.state.stores.length, 0);
});

test("provider 阻止 pending collection 和无稳定 mutationId 的写入", async () => {
  const h = harness();
  const port = makePort("/pdf/view");
  h.connect(port);
  await port.receive(pageMessage("HELLO", {
    namespace: NAMESPACE,
    ticket: TICKET,
    page: "/pdf/view",
  }, "hello"));
  port.messages.length = 0;

  await port.receive(pageMessage("CALL", {
    operation: "put",
    args: {
      collection: "cards",
      value: { id: "card-1" },
      options: { mutationId: "cards-put" },
    },
  }, "forbidden"));
  assert.equal(port.messages.at(-1).payload.code, "BW_PROVIDER_COLLECTION");

  await port.receive(pageMessage("CALL", {
    operation: "put",
    args: {
      collection: "user-settings",
      value: { id: "setting:test", rawValue: "1" },
      options: {},
    },
  }, "no-mutation"));
  assert.equal(port.messages.at(-1).payload.code, "BW_PROVIDER_MUTATION_ID");

  await port.receive(pageMessage("CALL", {
    operation: "batch",
    args: {
      mutations: [{
        operation: "put",
        collection: "user-settings",
        value: { rawValue: "missing-stable-id" },
        options: { mutationId: "batch-no-id" },
      }],
    },
  }, "batch-no-id"));
  assert.equal(port.messages.at(-1).payload.code, "BW_PROVIDER_PAYLOAD");

  await port.receive(pageMessage("CALL", {
    operation: "batch",
    args: {
      mutations: [{
        operation: "erase-everything",
        collection: "user-settings",
        id: "setting:test",
        options: { mutationId: "batch-bad-operation" },
      }],
    },
  }, "batch-bad-operation"));
  assert.equal(port.messages.at(-1).payload.code, "BW_PROVIDER_OPERATION");
});

test("旧 Vault 中未知 collection 不能经 status、changes 或 CHANGE 广播越过白名单", async () => {
  const h = harness();
  const port = makePort("/pdf/view");
  h.connect(port);
  await port.receive(pageMessage("HELLO", {
    namespace: NAMESPACE,
    ticket: TICKET,
    page: "/pdf/view",
  }, "hello"));
  port.messages.length = 0;

  h.state.statusCollections = ["cards", "user-settings"];
  await port.receive(pageMessage("CALL", {
    operation: "status",
    args: {},
  }, "status"));
  const status = port.messages.find((message) => message.id === "status");
  assert.deepEqual(status.payload.result.collections, ["user-settings"]);
  assert.deepEqual(
    status.payload.result.providerCollections,
    ["dictionary-cache", "query-cache", "translation-cache", "user-settings", "vocabulary-state"],
  );

  h.state.providerChanges = {
    contract: "data-store/1",
    cursor: 2,
    nextCursor: 2,
    oldestCursor: 1,
    resetRequired: false,
    hasMore: false,
    changes: [
      {
        cursor: 1,
        collection: "cards",
        record: { collection: "cards", id: "private-card" },
      },
      {
        cursor: 2,
        collection: "user-settings",
        record: { collection: "user-settings", id: "visible-setting" },
      },
    ],
  };
  await port.receive(pageMessage("CALL", {
    operation: "changes",
    args: { query: { after: 0, limit: 200 } },
  }, "changes"));
  const changes = port.messages.find((message) => message.id === "changes");
  assert.deepEqual(
    changes.payload.result.changes.map((change) => change.record.id),
    ["visible-setting"],
  );
  assert.equal(changes.payload.result.nextCursor, 2);

  h.state.emitStoreChange({
    cursor: 3,
    collection: "cards",
    record: { collection: "cards", id: "hidden-broadcast" },
  });
  assert.equal(port.messages.some((message) => message.type === "CHANGE"), false);
  h.state.emitStoreChange({
    cursor: 4,
    collection: "user-settings",
    record: { collection: "user-settings", id: "visible-broadcast" },
  });
  const broadcast = port.messages.find((message) => message.type === "CHANGE");
  assert.equal(broadcast.payload.record.id, "visible-broadcast");
});

test("真实 store change envelope 被逐条广播，分页和响应大小都有硬边界", async () => {
  const h = harness();
  const port = makePort("/pdf/view");
  h.connect(port);
  await port.receive(pageMessage("HELLO", {
    namespace: NAMESPACE,
    ticket: TICKET,
    page: "/pdf/view",
  }, "hello"));
  port.messages.length = 0;

  await port.receive(pageMessage("CALL", {
    operation: "put",
    args: {
      collection: "user-settings",
      value: { id: "setting:test", rawValue: "1" },
      options: { mutationId: "setting-put" },
    },
  }, "put"));
  const change = port.messages.find((message) => message.type === "CHANGE");
  assert.equal(change.payload.record.id, "setting:test");
  assert.equal(change.payload.mutationId, "setting-put");
  assert.equal(change.payload.cursor, 1);

  await port.receive(pageMessage("CALL", {
    operation: "list",
    args: { collection: "user-settings", query: { limit: 1000 } },
  }, "list"));
  assert.equal(h.state.lastListQuery.limit, 200);

  h.state.largeResponse = true;
  await port.receive(pageMessage("CALL", {
    operation: "list",
    args: { collection: "user-settings", query: { limit: 1 } },
  }, "large"));
  const oversized = port.messages.find((message) => message.id === "large");
  assert.equal(oversized.payload.ok, false);
  assert.equal(oversized.payload.code, "BW_PROVIDER_PAYLOAD");
});

function popupSender() {
  return {
    id: "extension-test",
    url: "chrome-extension://extension-test/popup.html",
  };
}

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function authorizationHeader(init) {
  if (typeof init?.headers?.get === "function") {
    return String(init.headers.get("Authorization") || "");
  }
  return String(init?.headers?.Authorization || init?.headers?.authorization || "");
}

function decodedChunks(messages, id) {
  return messages
    .filter((message) => message.id === id && message.type === "chunk")
    .map((message) => Buffer.from(message.b64, "base64").toString("utf8"))
    .join("");
}

async function authorizePort(h, port, namespace = NAMESPACE, ticket = TICKET) {
  h.connect(port);
  await port.receive(pageMessage("HELLO", {
    namespace,
    ticket,
    page: new URL(port.sender.tab.url).pathname,
  }, "account-runtime-hello"));
  assert.equal(port.messages.at(-1).type, "READY");
  h.state.activeTabId = port.sender.tab.id;
}

async function saveAccountToken(h, provider, token) {
  const saved = await h.sendRuntimeMessage({
    type: "BW_ACCOUNT_TOKEN_SAVE",
    target: { tabId: provider.sender.tab.id, frameId: 0 },
    payload: { token },
  }, popupSender());
  assert.equal(saved.ok, true, JSON.stringify(saved));
  assert.equal(saved.data.credential.configured, true);
}

async function openPairedDirectHostForLeaseTest(label) {
  const token = `lease-test-token-${label}`;
  const h = harness({
    enableSyncRuntime: true,
    networkHandler: async (url, init) => {
      if (url === `${ORIGIN}/api/reader/token-owner`) {
        assert.equal(authorizationHeader(init), `Bearer ${token}`);
        return jsonResponse({ ok: true, storage_namespace: NAMESPACE });
      }
      if (url === `${ORIGIN}/api/reader/sync/exchange`) {
        const request = JSON.parse(init.body);
        return jsonResponse({
          ok: true,
          contract: "sync-gateway/2",
          cursor: request.cursor,
          headCursor: request.cursor,
          oldestCursor: 0,
          hasMore: false,
          resetRequired: false,
          ackedMutationIds: request.direction === "push"
            ? request.changes.map((change) => change.mutationId)
            : [],
          changes: [],
          conflicts: [],
        });
      }
      throw new Error(`unexpected network request: ${url}`);
    },
  });
  const provider = makePort("/pdf/view");
  await authorizePort(h, provider);
  await saveAccountToken(h, provider, token);
  const directHost = makeDirectHostPort(ordinaryContentSender(
    `https://example.com/${label}`,
  ));
  h.connect(directHost);
  await waitForPortMessage(
    directHost,
    (message) => message.type === "READY",
    `${label} direct host ready`,
  );
  assert.equal(h.state.ownerLeaseManagers.length, 1);
  return {
    h,
    provider,
    directHost,
    ownerLease: h.state.ownerLeaseManagers[0],
  };
}

test("缺少可信 PWA owner claim 时只保留本地 Vault，alarm 与任意网页直连均不启动", async () => {
  let runtimeCreates = 0;
  const h = harness({
    syncRuntimeFactory: {
      CONTRACT: "sync-runtime/1",
      createSyncRuntime() {
        runtimeCreates += 1;
        throw new Error("unclaimed owner must not be created");
      },
    },
  });
  const provider = makePort("/pdf/view");
  h.connect(provider);
  await provider.receive(pageMessage("HELLO", {
    namespace: NAMESPACE,
    ticket: TICKET,
    page: "/pdf/view",
    syncOwnerClaim: null,
  }, "unclaimed-provider"));
  const ready = provider.messages.at(-1);
  assert.equal(ready.type, "READY", JSON.stringify(ready));
  assert.equal(ready.payload.capabilities.dataStore, true);
  assert.equal(ready.payload.capabilities.syncControl, false);
  assert.equal(ready.payload.capabilities.serverSync, false);
  assert.equal(ready.payload.capabilities.syncOwner, "reserved-unclaimed");

  await provider.receive(pageMessage("CALL", {
    operation: "put",
    args: {
      collection: "user-settings",
      value: { id: "setting:local-only", rawValue: "1" },
      options: { mutationId: "local-only-put" },
    },
  }, "local-only-put"));
  assert.equal(
    provider.messages.find((message) => message.id === "local-only-put")
      ?.payload?.ok,
    true,
  );

  await provider.receive(pageMessage("CALL", {
    operation: "syncStatus",
    args: {},
  }, "unclaimed-sync-status"));
  const unclaimedStatus = provider.messages.find(
    (message) => message.id === "unclaimed-sync-status",
  );
  assert.equal(unclaimedStatus.payload.ok, false);
  assert.equal(unclaimedStatus.payload.code, "BW_SYNC_OWNER_UNCLAIMED");

  h.fireAlarm();
  await settleBackground();
  assert.equal(runtimeCreates, 0);

  const directHost = makeDirectHostPort(ordinaryContentSender());
  h.connect(directHost);
  await settleBackground();
  assert.equal(
    directHost.messages.some((message) => message.type === "READY"),
    false,
  );
  assert.equal(
    directHost.messages.some(
      (message) =>
        message.type === "STANDBY" &&
        message.payload?.reason === "selecting-host",
    ),
    true,
  );
});

test("扩展后台以固定端点、账户租约和私有 token 完成 server sync，并持久化独立游标", async () => {
  const exchanges = [];
  const serverRecord = {
    schema: 1,
    collection: "vocabulary-state",
    id: "word:remote",
    rev: 1,
    updatedAt: 10,
    updatedBy: "remote-device",
    deleted: false,
    value: { id: "word:remote", mastered: true },
  };
  const h = harness({
    enableSyncRuntime: true,
    networkHandler: async (url, init) => {
      if (url === `${ORIGIN}/api/reader/token-owner`) {
        return jsonResponse({ ok: true, storage_namespace: NAMESPACE });
      }
      assert.equal(url, `${ORIGIN}/api/reader/sync/exchange`);
      assert.equal(init.credentials, "omit");
      assert.equal(authorizationHeader(init), "Bearer sync-private-token");
      const request = JSON.parse(init.body);
      assert.equal(request.contract, "sync-gateway/2");
      assert.equal(request.syncContract, "sync-v3");
      assert.equal(request.syncChangeContract, "record-parent-state/1");
      assert.equal(
        request.registryDigest,
        "sync-v3:record-parent-state/1|" +
          "user-settings:explicit:0:1|vocabulary-state:explicit:0:1",
      );
      assert.equal(request.ownerNamespace, NAMESPACE);
      assert.match(
        request.deviceId,
        /^extension-install-v1-[a-f0-9]{32}$/,
      );
      assert.equal(JSON.stringify(request).includes("sync-private-token"), false);
      exchanges.push(request);
      if (request.direction === "push") {
        return jsonResponse({
          ok: true,
          contract: "sync-gateway/2",
          cursor: request.cursor,
          headCursor: request.cursor,
          oldestCursor: 0,
          hasMore: false,
          resetRequired: false,
          ackedMutationIds: request.changes.map(
            (change) => change.mutationId,
          ),
          changes: [],
          conflicts: [],
        });
      }
      return jsonResponse({
        ok: true,
        contract: "sync-gateway/2",
        cursor: 1,
        headCursor: 1,
        oldestCursor: 0,
        hasMore: false,
        resetRequired: false,
        ackedMutationIds: [],
        changes: [{
          cursor: 1,
          mutationId: "remote-word-1",
          operation: "put",
          collection: "vocabulary-state",
          record: serverRecord,
        }],
        conflicts: [],
      });
    },
  });
  const provider = makePort("/pdf/view");
  await authorizePort(h, provider);
  const ready = provider.messages.find((message) => message.type === "READY");
  assert.equal(ready.payload.capabilities.serverSync, true);
  assert.equal(ready.payload.capabilities.directSync, false);
  assert.equal(ready.payload.capabilities.syncOwner, "extension-background");
  assert.deepEqual(
    ready.payload.capabilities.syncCollections,
    [
      "user-settings",
      "vocabulary-state",
    ],
  );

  h.state.providerChanges = {
    cursor: 1,
    nextCursor: 1,
    oldestCursor: 0,
    hasMore: false,
    resetRequired: false,
    changes: [{
      cursor: 1,
      mutationId: "local-setting-1",
      operation: "put",
      collection: "user-settings",
      record: {
        schema: 1,
        collection: "user-settings",
        id: "setting:sync-test",
        rev: 1,
        updatedAt: 5,
        updatedBy: "extension-test",
        deleted: false,
        value: { id: "setting:sync-test", enabled: true },
      },
    }],
  };
  await saveAccountToken(h, provider, "sync-private-token");
  const deadline = Date.now() + 2000;
  while (
    Date.now() < deadline &&
    (
      !exchanges.some((request) => request.direction === "push") ||
      !exchanges.some((request) => request.direction === "pull")
    )
  ) {
    await new Promise((resolve) => setTimeout(resolve, 5));
  }

  const pushed = exchanges.find((request) => request.direction === "push");
  assert.equal(pushed.changes.length, 1);
  assert.equal(pushed.changes[0].collection, "user-settings");
  assert.equal(
    h.state.storeOperations.some((entry) =>
      entry.operation === "applyChanges" &&
      entry.details.options.journal === false &&
      entry.details.changes.some(
        (change) => change.record?.id === "word:remote",
      )
    ),
    true,
  );
  const checkpointEntries = Object.entries(h.state.storageState).filter(
    ([, value]) => value?.contract === "provider-sync-checkpoint/2",
  );
  assert.equal(checkpointEntries.length, 1);
  assert.equal(checkpointEntries[0][1].schema, 2);
  assert.equal(checkpointEntries[0][1].checkpoint.server.localCursor, 1);
  assert.equal(checkpointEntries[0][1].checkpoint.server.remoteCursor, 1);
  assert.equal(
    JSON.stringify(h.state.storageState).includes("sync-private-token"),
    false,
    "token 只能保存在私有 IndexedDB credential store",
  );
  assert.equal(
    h.state.alarms.some((alarm) =>
      alarm.name === "bw-reader-provider-sync-v1" &&
      alarm.options.periodInMinutes === 1
    ),
    true,
  );
});

test("扩展 checkpoint 绑定 provider Vault epoch，DB 重建后首轮 pull 从 cursor 0 恢复", async () => {
  let runtimeOptions = null;
  const exchanges = [];
  const syncRuntimeFactory = {
    CONTRACT: "sync-runtime/1",
    createSyncRuntime(options) {
      runtimeOptions = options;
      return {
        contract: "sync-runtime/1",
        resume() {},
        pause() {},
        destroy() {},
        async runNow() {
          return { contract: "sync-runtime/1", server: null, direct: {} };
        },
        async status() {
          return {
            contract: "sync-runtime/1",
            state: "idle",
            paused: false,
            pauseReason: "",
            updatedAt: 1,
            lastResult: null,
          };
        },
        async resolveConflict() {
          return { resolved: true };
        },
      };
    },
  };
  const token = "checkpoint-epoch-token";
  const h = harness({
    syncRuntimeFactory,
    networkHandler: async (url, init) => {
      if (url === `${ORIGIN}/api/reader/token-owner`) {
        return jsonResponse({ ok: true, storage_namespace: NAMESPACE });
      }
      assert.equal(url, `${ORIGIN}/api/reader/sync/exchange`);
      assert.equal(authorizationHeader(init), `Bearer ${token}`);
      const request = JSON.parse(init.body);
      exchanges.push(request);
      return jsonResponse({
        ok: true,
        contract: "sync-gateway/2",
        cursor: request.cursor,
        headCursor: request.cursor,
        oldestCursor: 0,
        hasMore: false,
        resetRequired: false,
        ackedMutationIds: [],
        changes: [],
        conflicts: [],
      });
    },
  });
  const provider = makePort("/pdf/view");
  await authorizePort(h, provider);
  const deadline = Date.now() + 2000;
  while (Date.now() < deadline && !runtimeOptions) {
    await new Promise((resolve) => setTimeout(resolve, 1));
  }
  assert.ok(runtimeOptions?.checkpointStore);
  await saveAccountToken(h, provider, token);

  const checkpointStore = runtimeOptions.checkpointStore;
  const checkpoint = {
    contract: "sync-coordinator/1",
    schema: 1,
    registryDigest: createProviderRegistry().syncDigest(),
    generation: 1,
    server: {
      localCursor: 0,
      remoteCursor: 31,
      reconciliationEpoch: 0,
    },
    peers: {},
  };
  assert.equal(await checkpointStore.load(), null);
  await checkpointStore.save(checkpoint);
  const checkpointEntry = Object.entries(h.state.storageState).find(
    ([, value]) => value?.contract === "provider-sync-checkpoint/2",
  );
  assert.ok(checkpointEntry);
  const [checkpointKey, envelope] = checkpointEntry;
  const providerDbName = h.state.storeOptions.find(
    (options) =>
      options.dbName === `bw-reader-extension-vault-v1-${NAMESPACE}`,
  )?.dbName;
  assert.ok(providerDbName);
  assert.equal(envelope.schema, 2);
  assert.equal(
    envelope.vaultEpoch,
    h.state.storeInstanceEpochs.get(providerDbName),
  );
  assert.equal(envelope.checkpoint.server.localCursor, 0);
  assert.equal(envelope.checkpoint.server.remoteCursor, 31);

  h.state.storageState[checkpointKey] = structuredClone(checkpoint);
  assert.equal(
    await checkpointStore.load(),
    null,
    "没有 Vault epoch 的 v1 raw checkpoint 必须失效",
  );
  h.state.storageState[checkpointKey] = {
    contract: "provider-sync-checkpoint/2",
    schema: 2,
    vaultEpoch: h.state.storeInstanceEpochs.get(providerDbName),
    checkpoint: null,
  };
  await assert.rejects(
    checkpointStore.load(),
    (error) => error?.code === "BW_SYNC_CHECKPOINT",
  );
  await checkpointStore.save(checkpoint);
  assert.equal((await checkpointStore.load()).server.remoteCursor, 31);

  h.state.rebuildStoreInstance(providerDbName);
  await assert.rejects(
    checkpointStore.save(checkpoint),
    (error) =>
      error?.code === "BW_SYNC_CHECKPOINT_EPOCH" &&
      error.retryable === true,
    "运行中 DB 重建不得把旧 coordinator 游标写进新 Vault epoch",
  );
  const recovered = await checkpointStore.load();
  assert.equal(recovered, null);
  const firstRemoteCursor = Number(recovered?.server?.remoteCursor || 0);
  await runtimeOptions.serverGateway.pull({
    cursor: firstRemoteCursor,
    limit: 200,
  });
  assert.equal(exchanges.at(-1).direction, "pull");
  assert.equal(exchanges.at(-1).cursor, 0);
  await checkpointStore.save({
    ...checkpoint,
    generation: 2,
    server: {
      localCursor: 0,
      remoteCursor: 0,
      reconciliationEpoch: 1,
    },
  });
  const recoveredEnvelope = h.state.storageState[checkpointKey];
  assert.equal(
    recoveredEnvelope.vaultEpoch,
    h.state.storeInstanceEpochs.get(providerDbName),
  );
  assert.equal(recoveredEnvelope.checkpoint.server.remoteCursor, 0);
});

test("PWA provider 与 popup 都只读同步状态，旧重试入口被拒绝且不泄漏危险字段", async () => {
  const providerOpsBlock = SOURCE.match(
    /const PROVIDER_OPS\s*=\s*new Set\(\[[\s\S]*?\]\);/,
  )?.[0] || "";
  const providerSyncOpsBlock = SOURCE.match(
    /const PROVIDER_SYNC_OPS\s*=\s*new Set\(\[[\s\S]*?\]\);/,
  )?.[0] || "";
  assert.ok(providerOpsBlock);
  assert.ok(providerSyncOpsBlock);
  assert.equal(providerOpsBlock.includes("syncStatus"), false);
  assert.equal(
    providerOpsBlock.includes("syncRetryAfterResolution"),
    false,
  );
  assert.equal(providerSyncOpsBlock.includes('"syncStatus"'), true);
  assert.equal(
    providerSyncOpsBlock.includes("syncRetryAfterResolution"),
    false,
  );

  const rawRecord = "provider-raw-record-must-not-escape";
  const upstreamText = "provider upstream database exception";
  const token = "provider-sync-conflict-token";
  const h = harness({
    networkHandler: async (url, init) => {
      if (url === `${ORIGIN}/api/reader/token-owner`) {
        return jsonResponse({ ok: true, storage_namespace: NAMESPACE });
      }
      assert.equal(url, `${ORIGIN}/api/reader/sync/exchange`);
      assert.equal(authorizationHeader(init), `Bearer ${token}`);
      const request = JSON.parse(init.body);
      const conflict = {
        mutationId: request.changes?.[0]?.mutationId || "local-theme-conflict",
        collection: "user-settings",
        id: "theme",
        reason: "revision-conflict",
        incomingRev: 2,
        currentRev: 1,
        rawRecord,
        upstreamText,
        token,
        namespace: NAMESPACE,
      };
      return jsonResponse({
        ok: true,
        contract: "sync-gateway/2",
        cursor: request.cursor,
        headCursor: request.cursor,
        oldestCursor: 0,
        hasMore: false,
        resetRequired: false,
        ackedMutationIds: [],
        changes: [],
        conflicts: request.direction === "push" ? [conflict] : [],
      });
    },
  });
  const unauthorized = makePort("/pdf/view");
  h.connect(unauthorized);
  await unauthorized.receive(pageMessage("CALL", {
    operation: "syncStatus",
    args: {},
  }, "sync-status-before-hello"));
  const rejected = unauthorized.messages.find(
    (message) => message.id === "sync-status-before-hello",
  );
  assert.equal(rejected.payload.ok, false);
  assert.equal(rejected.payload.code, "BW_PROVIDER_AUTH");

  const provider = makePort("/pdf/view");
  await authorizePort(h, provider);
  h.state.providerChanges = {
    cursor: 1,
    nextCursor: 1,
    oldestCursor: 0,
    hasMore: false,
    resetRequired: false,
    changes: [{
      cursor: 1,
      mutationId: "local-theme-conflict",
      operation: "put",
      collection: "user-settings",
      record: {
        schema: 1,
        collection: "user-settings",
        id: "theme",
        rev: 1,
        updatedAt: 1,
        updatedBy: "extension-test",
        deleted: false,
        value: { id: "theme", mode: "dark" },
      },
    }],
  };
  await saveAccountToken(h, provider, token);

  let blocked = null;
  const deadline = Date.now() + 2000;
  let attempt = 0;
  while (Date.now() < deadline && !blocked) {
    const id = `sync-status-${++attempt}`;
    await provider.receive(pageMessage("CALL", {
      operation: "syncStatus",
      args: {},
    }, id));
    const response = provider.messages.find((message) => message.id === id);
    if (
      response?.payload?.ok === true &&
      response.payload.result?.state === "blocked"
    ) {
      blocked = response.payload.result;
      break;
    }
    await new Promise((resolve) => setTimeout(resolve, 2));
  }
  assert.ok(blocked, JSON.stringify(provider.messages.slice(-4)));
  assert.deepEqual(
    Object.keys(blocked).sort(),
    [
      "at",
      "conflictCount",
      "conflicts",
      "contract",
      "errorCode",
      "owner",
      "retryable",
      "state",
      "truncated",
    ],
  );
  assert.equal(blocked.contract, "sync-conflict-control/1");
  assert.equal(blocked.owner, "extension-background");
  assert.equal(Object.hasOwn(blocked, "conflictSetId"), false);
  const serialized = JSON.stringify(blocked);
  assert.equal(serialized.includes(rawRecord), false);
  assert.equal(serialized.includes(upstreamText), false);
  assert.equal(serialized.includes(token), false);
  assert.equal(serialized.includes(NAMESPACE), false);

  await provider.receive(pageMessage("CALL", {
    operation: "status",
    args: {},
  }, "ordinary-data-store-status"));
  const ordinary = provider.messages.find(
    (message) => message.id === "ordinary-data-store-status",
  );
  assert.equal(ordinary.payload.ok, true);
  assert.equal(ordinary.payload.result.contract, "data-store/1");
  assert.equal(
    Object.hasOwn(ordinary.payload.result, "conflictSetId"),
    false,
  );

  await provider.receive(pageMessage("CALL", {
    operation: "syncRetryAfterResolution",
    args: {
      conflictSetId: `conflict-set-v1-${"d".repeat(32)}`,
      token: "must-not-escape-provider",
      namespace: OTHER_NAMESPACE,
    },
  }, "sync-retry-after-resolution"));
  const retried = provider.messages.find(
    (message) => message.id === "sync-retry-after-resolution",
  );
  assert.equal(retried.payload.ok, false, JSON.stringify(retried));
  assert.equal(retried.payload.code, "BW_PROVIDER_OPERATION");
  assert.equal(
    JSON.stringify(retried).includes("must-not-escape-provider"),
    false,
  );

  const wrongTarget = await h.sendRuntimeMessage({
    type: "BW_SYNC_STATUS",
    target: { tabId: provider.sender.tab.id + 1, frameId: 0 },
  }, popupSender());
  assert.equal(wrongTarget.ok, false);
  assert.equal(wrongTarget.code, "BW_ACCOUNT_ACTIVE_TAB");

  const popupStatus = await h.sendRuntimeMessage({
    type: "BW_SYNC_STATUS",
    target: { tabId: provider.sender.tab.id, frameId: 0 },
  }, popupSender());
  assert.equal(popupStatus.ok, true, JSON.stringify(popupStatus));
  assert.deepEqual(
    Object.keys(popupStatus.data).sort(),
    Object.keys(blocked).sort(),
  );
  assert.equal(Object.hasOwn(popupStatus.data, "conflictSetId"), false);

  const popupRetry = await h.sendRuntimeMessage({
    type: "BW_SYNC_RETRY_AFTER_RESOLUTION",
    target: { tabId: provider.sender.tab.id, frameId: 0 },
    payload: {
      conflictSetId: `conflict-set-v1-${"e".repeat(32)}`,
      token: "must-be-ignored",
      namespace: OTHER_NAMESPACE,
    },
  }, popupSender());
  assert.equal(popupRetry, undefined);
});

test("PWA provider 的迟到 syncStatus 结果不能越过账户/port fence", async () => {
  for (const operation of ["syncStatus"]) {
    let release;
    let markStarted;
    const gate = new Promise((resolve) => { release = resolve; });
    const started = new Promise((resolve) => { markStarted = resolve; });
    const safeStatus = {
      contract: "sync-conflict-control/1",
      owner: "extension-background",
      state: "blocked",
      at: 1,
      conflictCount: 1,
      truncated: false,
      conflicts: [],
    };
    const factory = {
      CONTRACT: "sync-conflict-control/1",
      createSyncConflictControl(options) {
        const gated = async () => {
          options.assertFence();
          markStarted();
          await gate;
          options.assertFence();
          return structuredClone(safeStatus);
        };
        return {
          contract: "sync-conflict-control/1",
          status: gated,
        };
      },
    };
    const h = harness({ syncConflictControlFactory: factory });
    const provider = makePort("/pdf/view");
    await authorizePort(h, provider);
    const id = `late-${operation}`;
    const pending = provider.receive(pageMessage("CALL", {
      operation,
      args: {},
    }, id));
    await started;
    await provider.receive(pageMessage("HELLO", {
      namespace: OTHER_NAMESPACE,
      ticket: OTHER_TICKET,
      page: "/pdf/view",
    }, `switch-during-${operation}`));
    release();
    await pending;

    const response = provider.messages.find((message) => message.id === id);
    assert.equal(response.payload.ok, false, operation);
    assert.ok(
      [
        "BW_ACCOUNT_CONTEXT_STALE",
        "BW_PROVIDER_DISCONNECTED",
      ].includes(response.payload.code),
      `${operation}: ${response.payload.code}`,
    );
    assert.equal(
      JSON.stringify(response).includes("conflict-set-v1-"),
      false,
    );
  }
});

test("popup 同步状态的迟到结果不能越过活动账户 fence", async () => {
  for (const messageType of ["BW_SYNC_STATUS"]) {
    let release;
    let markStarted;
    const gate = new Promise((resolve) => { release = resolve; });
    const started = new Promise((resolve) => { markStarted = resolve; });
    const safeStatus = {
      contract: "sync-conflict-control/1",
      owner: "extension-background",
      state: "blocked",
      at: 1,
      conflictCount: 1,
      truncated: false,
      conflicts: [],
    };
    const factory = {
      CONTRACT: "sync-conflict-control/1",
      createSyncConflictControl(options) {
        const gated = async () => {
          options.assertFence();
          markStarted();
          await gate;
          options.assertFence();
          return structuredClone(safeStatus);
        };
        return {
          contract: "sync-conflict-control/1",
          status: gated,
        };
      },
    };
    const h = harness({ syncConflictControlFactory: factory });
    const provider = makePort("/pdf/view");
    await authorizePort(h, provider);
    const pending = h.sendRuntimeMessage({
      type: messageType,
      target: { tabId: provider.sender.tab.id, frameId: 0 },
    }, popupSender());
    await started;
    await provider.receive(pageMessage("HELLO", {
      namespace: OTHER_NAMESPACE,
      ticket: OTHER_TICKET,
      page: "/pdf/view",
    }, `popup-switch-during-${messageType}`));
    release();

    const response = await pending;
    assert.equal(response.ok, false, messageType);
    assert.ok(
      [
        "BW_ACCOUNT_CONTEXT_STALE",
        "BW_PROVIDER_DISCONNECTED",
      ].includes(response.code),
      `${messageType}: ${response.code}`,
    );
    assert.equal(
      JSON.stringify(response).includes("conflict-set-v1-"),
      false,
    );
  }
});

test("未配对 remembered account 不启动 owner，配对后的 lease 不依赖 PWA port 存活", async () => {
  const runtimeState = {
    created: 0,
    destroyed: [],
    resumed: [],
    runNow: [],
    assertLease: null,
    gateNextStatus: false,
  };
  let releaseStatus;
  let markStatusStarted;
  const statusGate = new Promise((resolve) => { releaseStatus = resolve; });
  const statusStarted = new Promise((resolve) => {
    markStatusStarted = resolve;
  });
  const syncRuntimeFactory = {
    CONTRACT: "sync-runtime/1",
    createSyncRuntime(options) {
      runtimeState.created += 1;
      runtimeState.assertLease = options.assertLease;
      return {
        contract: "sync-runtime/1",
        async status() {
          if (runtimeState.gateNextStatus) {
            runtimeState.gateNextStatus = false;
            markStatusStarted();
            await statusGate;
          }
          return {
            contract: "sync-runtime/1",
            state: "idle",
            paused: false,
            pauseReason: "",
            updatedAt: 1,
            lastResult: null,
          };
        },
        pause() {},
        async resolveConflict() {
          return { resolved: true };
        },
        resume(reason) {
          runtimeState.resumed.push(String(reason || ""));
        },
        async runNow(reason) {
          runtimeState.runNow.push(String(reason || ""));
          return { ok: true };
        },
        destroy(reason) {
          runtimeState.destroyed.push(String(reason || ""));
        },
      };
    },
  };
  const h = harness({
    syncRuntimeFactory,
    networkHandler: async (url) => {
      assert.equal(url, `${ORIGIN}/api/reader/token-owner`);
      return jsonResponse({ ok: true, storage_namespace: NAMESPACE });
    },
  });
  await settleBackground();
  h.state.storageState.readerActiveVerifiedAccountV1 = {
    schema: 1,
    namespace: NAMESPACE,
    verifiedAt: Date.now(),
    source: "provider-ticket",
  };

  h.fireAlarm();
  h.fireAlarm();
  await settleBackground();
  assert.equal(runtimeState.runNow.length, 0);
  assert.equal(runtimeState.created, 0);

  const firstProvider = makePort("/pdf/view");
  await authorizePort(h, firstProvider);
  const secondProvider = makePort("/pdf/view");
  await authorizePort(h, secondProvider);
  const ownerDeadline = Date.now() + 2000;
  while (Date.now() < ownerDeadline && runtimeState.created < 1) {
    await new Promise((resolve) => setTimeout(resolve, 1));
  }
  assert.equal(runtimeState.created, 1);
  assert.throws(
    () => runtimeState.assertLease(),
    (error) => error?.code === "BW_SYNC_OWNER_INACTIVE",
    "没有设备 token 时 runtime 已构造但不得取得网络写权限",
  );
  await saveAccountToken(h, secondProvider, "paired-owner-token");
  const leaseDeadline = Date.now() + 2000;
  while (Date.now() < leaseDeadline) {
    try {
      if (runtimeState.assertLease() === true) break;
    } catch (_) {}
    await new Promise((resolve) => setTimeout(resolve, 1));
  }
  assert.equal(runtimeState.assertLease(), true);

  const runBeforeAlarms = runtimeState.runNow.length;
  h.fireAlarm();
  h.fireAlarm();
  const wakeDeadline = Date.now() + 2000;
  while (
    Date.now() < wakeDeadline &&
    runtimeState.runNow.length < runBeforeAlarms + 2
  ) {
    await new Promise((resolve) => setTimeout(resolve, 1));
  }
  assert.equal(runtimeState.runNow.length, runBeforeAlarms + 2);
  assert.equal(runtimeState.created, 1);

  await firstProvider.receive(pageMessage("CALL", {
    operation: "syncStatus",
    args: {},
  }, "persistent-owner-status"));
  const firstStatus = firstProvider.messages.find(
    (message) => message.id === "persistent-owner-status",
  );
  assert.equal(firstStatus.payload.ok, true, JSON.stringify(firstStatus));
  firstProvider.disconnect();
  await settleBackground();
  assert.equal(runtimeState.created, 1);
  assert.deepEqual(runtimeState.destroyed, []);
  assert.equal(
    runtimeState.assertLease(),
    true,
    "同账户第二个可信 PWA claim 仍存活时 owner 必须保留",
  );

  runtimeState.gateNextStatus = true;
  const pending = secondProvider.receive(pageMessage("CALL", {
    operation: "syncStatus",
    args: {},
  }, "disconnect-during-status"));
  await statusStarted;
  secondProvider.disconnect();
  releaseStatus();
  await pending;

  const late = secondProvider.messages.find(
    (message) => message.id === "disconnect-during-status",
  );
  assert.equal(late.payload.ok, false, JSON.stringify(late));
  assert.ok(
    [
      "BW_PROVIDER_DISCONNECTED",
      "BW_SYNC_CONFLICT_FENCE",
    ].includes(late.payload.code),
    late.payload.code,
  );
  assert.equal(runtimeState.created, 1);
  assert.deepEqual(runtimeState.destroyed, []);

  const runBeforeDetachedAlarm = runtimeState.runNow.length;
  h.fireAlarm();
  const detachedDeadline = Date.now() + 2000;
  while (
    Date.now() < detachedDeadline &&
    runtimeState.runNow.length <= runBeforeDetachedAlarm
  ) {
    await new Promise((resolve) => setTimeout(resolve, 1));
  }
  assert.equal(runtimeState.created, 1);
  assert.equal(
    runtimeState.runNow.length,
    runBeforeDetachedAlarm + 1,
    "设备族已经配对后，最后一个 PWA port 断开不应阻止 MV3 alarm 续跑",
  );
});

test("设备直连内容宿主只获得不透明身份/代际证明，后台独占账户信令与账户切换围栏", async () => {
  const signalRequests = [];
  const syncRequests = [];
  const token = "direct-private-token";
  const h = harness({
    enableSyncRuntime: true,
    networkHandler: async (url, init) => {
      if (url === `${ORIGIN}/api/reader/token-owner`) {
        assert.equal(authorizationHeader(init), `Bearer ${token}`);
        return jsonResponse({ ok: true, storage_namespace: NAMESPACE });
      }
      if (url === `${ORIGIN}/api/reader/sync/exchange`) {
        assert.equal(authorizationHeader(init), `Bearer ${token}`);
        const request = JSON.parse(init.body);
        assert.equal(request.ownerNamespace, NAMESPACE);
        assert.equal(request.deviceFamilyId, DEVICE_FAMILY_ID);
        assert.equal(request.ownerRole, "extension");
        assert.match(
          request.ownerInstanceId,
          /^owner-instance-v1:extension:[a-f0-9]{32}$/,
        );
        assert.equal(request.ownerGeneration, 1);
        assert.match(
          request.ownerToken,
          /^owner-token-v1-[A-Za-z0-9_-]{24,256}$/,
        );
        assert.equal(JSON.stringify(request).includes(token), false);
        syncRequests.push(request);
        return jsonResponse({
          ok: true,
          contract: "sync-gateway/2",
          cursor: request.cursor,
          headCursor: request.cursor,
          oldestCursor: 0,
          hasMore: false,
          resetRequired: false,
          ackedMutationIds: request.direction === "push"
            ? request.changes.map((change) => change.mutationId)
            : [],
          changes: [],
          conflicts: [],
        });
      }
      if (url === `${ORIGIN}/api/reader/sync/signal`) {
        assert.equal(init.credentials, "omit");
        assert.equal(authorizationHeader(init), `Bearer ${token}`);
        const request = JSON.parse(init.body);
        assert.equal(request.contract, "direct-signal/1");
        assert.equal(request.ownerNamespace, NAMESPACE);
        assert.equal(request.deviceFamilyId, DEVICE_FAMILY_ID);
        assert.equal(request.ownerRole, "extension");
        assert.match(
          request.ownerInstanceId,
          /^owner-instance-v1:extension:[a-f0-9]{32}$/,
        );
        assert.equal(request.ownerGeneration, 1);
        assert.match(
          request.ownerToken,
          /^owner-token-v1-[A-Za-z0-9_-]{24,256}$/,
        );
        assert.equal(JSON.stringify(request).includes(token), false);
        signalRequests.push(request);
        return jsonResponse({
          ok: true,
          contract: "direct-signal/1",
          accountProof: `account-proof-v1-${"a".repeat(64)}`,
          headCursor: request.serverCursor,
          baselineReady: true,
          baselineLocalCursor: request.localCursor,
          peers: [],
          ackedSignalIds: [],
          signals: [],
          signalCursor: request.signalCursor,
          signalResetRequired: false,
          hasMore: false,
        });
      }
      throw new Error(`unexpected network request: ${url}`);
    },
  });
  const provider = makePort("/pdf/view");
  await authorizePort(h, provider);
  await saveAccountToken(h, provider, token);

  const directHost = makeDirectHostPort(ordinaryContentSender(
    "https://example.com/direct-reader",
  ));
  h.connect(directHost);
  const ready = await waitForPortMessage(
    directHost,
    (message) =>
      message.protocol === "bw-reader-direct-host/1" &&
      message.type === "READY",
    "direct host ready",
  );
  assert.deepEqual(
    Object.keys(ready.payload).sort(),
    ["contract", "deviceId", "iceServers", "reason", "registryDigest"],
  );
  assert.equal(ready.payload.contract, "bw-reader-direct-host/1");
  assert.match(
    ready.payload.deviceId,
    /^extension-install-v1-[a-f0-9]{32}$/,
  );
  assert.match(
    ready.payload.registryDigest,
    /^sync-v3:record-parent-state\/1\|/,
  );
  assert.deepEqual(ready.payload.iceServers, []);
  assert.equal(Object.hasOwn(ready.payload, "ownerNamespace"), false);
  assert.equal(Object.hasOwn(ready.payload, "token"), false);
  assert.equal(JSON.stringify(ready).includes(NAMESPACE), false);
  assert.equal(JSON.stringify(ready).includes(token), false);

  const syncDeadline = Date.now() + 2000;
  while (Date.now() < syncDeadline && syncRequests.length < 2) {
    await new Promise((resolve) => setTimeout(resolve, 2));
  }
  assert.equal(
    syncRequests.some((request) => request.direction === "push"),
    true,
  );
  assert.equal(
    syncRequests.some((request) => request.direction === "pull"),
    true,
  );

  await directHost.receive({
    protocol: "bw-reader-direct-host/1",
    type: "CALL",
    id: "baseline-status",
    operation: "BASELINE_STATUS",
    payload: {},
  });
  const baseline = await waitForPortMessage(
    directHost,
    (message) => message.type === "RESULT" && message.id === "baseline-status",
    "direct baseline status",
  );
  assert.equal(baseline.payload.ok, true, JSON.stringify(baseline));
  assert.deepEqual(
    Object.keys(baseline.payload.result).sort(),
    ["localCursor", "ready", "serverCursor"],
  );
  assert.equal(baseline.payload.result.localCursor, 0);
  assert.equal(baseline.payload.result.serverCursor, 0);
  assert.equal(typeof baseline.payload.result.ready, "boolean");

  const signalRequest = {
    contract: "direct-signal/1",
    deviceId: ready.payload.deviceId,
    registryDigest: ready.payload.registryDigest,
    localCursor: baseline.payload.result.localCursor,
    serverCursor: baseline.payload.result.serverCursor,
    serverReady: baseline.payload.result.ready,
    signalCursor: 0,
    signals: [],
  };
  assert.equal(Object.hasOwn(signalRequest, "ownerNamespace"), false);
  assert.equal(Object.hasOwn(signalRequest, "token"), false);
  await directHost.receive({
    protocol: "bw-reader-direct-host/1",
    type: "CALL",
    id: "signal-exchange",
    operation: "SIGNAL_EXCHANGE",
    payload: { request: signalRequest },
  });
  const signal = await waitForPortMessage(
    directHost,
    (message) => message.type === "RESULT" && message.id === "signal-exchange",
    "direct signal exchange",
  );
  assert.equal(signal.payload.ok, true, JSON.stringify(signal));
  assert.equal(signalRequests.length, 1);
  assert.equal(signalRequests[0].ownerNamespace, NAMESPACE);
  assert.equal(signalRequests[0].deviceId, ready.payload.deviceId);
  assert.equal(signalRequests[0].registryDigest, ready.payload.registryDigest);
  assert.match(
    signal.payload.result.accountProof,
    /^account-proof-v1-[a-f0-9]{64}$/,
  );
  assert.equal(JSON.stringify(signal).includes(NAMESPACE), false);
  assert.equal(JSON.stringify(signal).includes(token), false);

  const peerId = "peer-device-stable";
  const answeredDirectCalls = new Set();
  const checkpoint = () => Object.values(h.state.storageState).find(
    (value) => value?.contract === "provider-sync-checkpoint/2",
  )?.checkpoint;
  const establishPeerSession = async (sessionId, suffix) => {
    await directHost.receive({
      protocol: "bw-reader-direct-host/1",
      type: "CALL",
      id: `peer-ready-${suffix}`,
      operation: "PEER_READY",
      payload: {
        peerId,
        sessionId,
        baselineLocalCursor: 0,
        baselineRemoteCursor: 0,
      },
    });
    const registered = await waitForPortMessage(
      directHost,
      (message) =>
        message.type === "RESULT" &&
        message.id === `peer-ready-${suffix}`,
      `direct peer ${suffix} registered`,
    );
    assert.equal(registered.payload.ok, true, JSON.stringify(registered));

    const directions = new Set();
    const deadline = Date.now() + 2000;
    while (Date.now() < deadline && directions.size < 2) {
      for (const message of directHost.messages) {
        if (
          message.type !== "DIRECT_CALL" ||
          message.payload?.peerId !== peerId ||
          message.payload?.sessionId !== sessionId ||
          answeredDirectCalls.has(message.id)
        ) continue;
        answeredDirectCalls.add(message.id);
        const request = message.payload.request;
        directions.add(request.direction);
        await directHost.receive({
          protocol: "bw-reader-direct-host/1",
          type: "DIRECT_RESULT",
          id: message.id,
          payload: {
            ok: true,
            result: {
              contract: "sync-gateway/2",
              cursor: request.cursor,
              headCursor: request.cursor,
              oldestCursor: 0,
              hasMore: false,
              resetRequired: false,
              ackedMutationIds: request.direction === "push"
                ? request.changes.map((change) => change.mutationId)
                : [],
              changes: [],
              conflicts: [],
            },
          },
        });
      }
      await new Promise((resolve) => setTimeout(resolve, 1));
    }
    assert.deepEqual(
      [...directions].sort(),
      ["pull", "push"],
      `session ${sessionId} 应完成一轮直连 push/pull`,
    );
    await settleBackground();
  };

  await establishPeerSession("direct-session-v1-first", "first");
  assert.deepEqual(
    Object.keys(checkpoint()?.peers || {}),
    [`rtc:${peerId}`],
  );
  await directHost.receive({
    protocol: "bw-reader-direct-host/1",
    type: "CALL",
    id: "peer-closed-first",
    operation: "PEER_CLOSED",
    payload: { peerId, reason: "renegotiate" },
  });
  const closed = await waitForPortMessage(
    directHost,
    (message) =>
      message.type === "RESULT" &&
      message.id === "peer-closed-first",
    "first direct peer session closed",
  );
  assert.equal(closed.payload.ok, true, JSON.stringify(closed));

  await establishPeerSession("direct-session-v1-second", "second");
  assert.deepEqual(
    Object.keys(checkpoint()?.peers || {}),
    [`rtc:${peerId}`],
    "同一设备重建 RTC session 不得累积持久 checkpoint peer key",
  );

  await provider.receive(pageMessage("HELLO", {
    namespace: OTHER_NAMESPACE,
    ticket: OTHER_TICKET,
    page: "/pdf/view",
  }, "switch-account-revokes-direct-host"));
  assert.equal(provider.messages.at(-1).type, "READY");
  await waitForPortMessage(
    directHost,
    (message) =>
      message.type === "REVOKE" &&
      [
        "active-account-changed",
        "owner-lease-lost",
        "provider-rehandshake",
      ].includes(message.payload?.reason),
    "direct host account switch revoke",
  );

  await directHost.receive({
    protocol: "bw-reader-direct-host/1",
    type: "CALL",
    id: "stale-baseline-status",
    operation: "BASELINE_STATUS",
    payload: {},
  });
  const stale = await waitForPortMessage(
    directHost,
    (message) =>
      message.type === "RESULT" &&
      message.id === "stale-baseline-status",
    "stale direct host rejected",
  );
  assert.equal(stale.payload.ok, false);
  assert.equal(stale.payload.code, "BW_DIRECT_HOST_INACTIVE");
});

test("直连宿主失去 owner lease 后拒绝 STORE_EXCHANGE，且不会调用 applyChanges", async () => {
  const { h, directHost, ownerLease } =
    await openPairedDirectHostForLeaseTest("lease-lost-before-store");
  const before = h.state.storeOperations.filter(
    (entry) => entry.operation === "applyChanges",
  ).length;
  await ownerLease.stop("forced-lease-loss", false);
  await directHost.receive({
    protocol: "bw-reader-direct-host/1",
    type: "CALL",
    id: "store-after-lease-loss",
    operation: "STORE_EXCHANGE",
    payload: {
      request: {
        contract: "sync-gateway/2",
        direction: "push",
        deviceId: "remote-peer",
        cursor: 0,
        limit: 1,
        changes: [],
      },
    },
  });
  const result = await waitForPortMessage(
    directHost,
    (message) =>
      message.type === "RESULT" &&
      message.id === "store-after-lease-loss",
    "store rejected after owner lease loss",
  );
  assert.equal(result.payload.ok, false, JSON.stringify(result));
  assert.ok(
    [
      "BW_DIRECT_HOST_INACTIVE",
      "BW_SYNC_OWNER_INACTIVE",
    ].includes(result.payload.code),
    result.payload.code,
  );
  assert.equal(
    h.state.storeOperations.filter(
      (entry) => entry.operation === "applyChanges",
    ).length,
    before,
  );
});

test("STORE_EXCHANGE 执行途中失租不得向内容宿主返回成功", async () => {
  const { h, directHost, ownerLease } =
    await openPairedDirectHostForLeaseTest("lease-lost-during-store");
  let releaseApply;
  let markApplyStarted;
  const applyGate = new Promise((resolve) => { releaseApply = resolve; });
  const applyStarted = new Promise((resolve) => {
    markApplyStarted = resolve;
  });
  h.state.beforeStoreOperation = async (operation) => {
    if (operation !== "applyChanges") return;
    markApplyStarted();
    await applyGate;
  };
  const pending = directHost.receive({
    protocol: "bw-reader-direct-host/1",
    type: "CALL",
    id: "store-during-lease-loss",
    operation: "STORE_EXCHANGE",
    payload: {
      request: {
        contract: "sync-gateway/2",
        direction: "push",
        deviceId: "remote-peer",
        cursor: 0,
        limit: 1,
        changes: [],
      },
    },
  });
  await applyStarted;
  await ownerLease.stop("forced-inflight-lease-loss", false);
  releaseApply();
  await pending;
  const result = await waitForPortMessage(
    directHost,
    (message) =>
      message.type === "RESULT" &&
      message.id === "store-during-lease-loss",
    "inflight store rejected after owner lease loss",
  );
  assert.equal(result.payload.ok, false, JSON.stringify(result));
  assert.ok(
    [
      "BW_DIRECT_HOST_INACTIVE",
      "BW_SYNC_OWNER_INACTIVE",
    ].includes(result.payload.code),
    result.payload.code,
  );
});

test("popup 只管理当前活动 provider 账户，token-owner 不匹配时不保存且不回传明文", async () => {
  const h = harness({
    storageState: { apiToken: "quarantined-old-token" },
    networkHandler: async (url) => {
      assert.equal(url, `${ORIGIN}/api/reader/token-owner`);
      return jsonResponse({
        ok: true,
        storage_namespace: OTHER_NAMESPACE,
      });
    },
  });
  const provider = makePort("/pdf/view");
  await authorizePort(h, provider);

  const wrongTarget = await h.sendRuntimeMessage({
    type: "BW_ACCOUNT_STATUS",
    target: { tabId: provider.sender.tab.id + 1, frameId: 0 },
  }, popupSender());
  assert.equal(wrongTarget.ok, false);
  assert.equal(wrongTarget.code, "BW_ACCOUNT_ACTIVE_TAB");

  const rejected = await h.sendRuntimeMessage({
    type: "BW_ACCOUNT_TOKEN_SAVE",
    target: { tabId: provider.sender.tab.id, frameId: 0 },
    payload: { token: "token-for-other-account" },
  }, popupSender());
  assert.equal(rejected.ok, false);
  assert.equal(rejected.code, "BW_ACCOUNT_TOKEN_OWNER_MISMATCH");
  assert.equal(JSON.stringify(rejected).includes("token-for-other-account"), false);

  const status = await h.sendRuntimeMessage({
    type: "BW_ACCOUNT_STATUS",
    target: { tabId: provider.sender.tab.id, frameId: 0 },
  }, popupSender());
  assert.equal(status.ok, true, JSON.stringify(status));
  assert.equal(status.data.credential.configured, false);
  assert.equal(status.data.legacyQuarantine.apiToken.present, true);
  assert.equal(status.data.legacyQuarantine.apiToken.quarantined, true);
  assert.equal(JSON.stringify(status).includes("quarantined-old-token"), false);
  assert.equal(h.state.storageState.apiToken, "quarantined-old-token");
});

test("A/B 账户各自保留一个 active token 和独立历史，popup 状态永不含 token", async () => {
  const h = harness({
    networkHandler: async (url, init) => {
      assert.equal(url, `${ORIGIN}/api/reader/token-owner`);
      const token = authorizationHeader(init).replace(/^Bearer\s+/, "");
      const namespace = token.startsWith("token-b") ? OTHER_NAMESPACE : NAMESPACE;
      return jsonResponse({ ok: true, storage_namespace: namespace });
    },
  });
  const provider = makePort("/pdf/view");
  await authorizePort(h, provider);
  const target = { tabId: provider.sender.tab.id, frameId: 0 };

  for (const token of ["token-a-one", "token-a-two"]) {
    const saved = await h.sendRuntimeMessage({
      type: "BW_ACCOUNT_TOKEN_SAVE",
      target,
      payload: { token },
    }, popupSender());
    assert.equal(saved.ok, true, JSON.stringify(saved));
    assert.equal(JSON.stringify(saved).includes(token), false);
  }
  let status = await h.sendRuntimeMessage({
    type: "BW_ACCOUNT_STATUS",
    target,
  }, popupSender());
  assert.equal(status.data.namespace, NAMESPACE);
  assert.equal(status.data.credential.candidateCount, 2);
  assert.equal(status.data.credential.inactiveCandidateCount, 1);

  await provider.receive(pageMessage("HELLO", {
    namespace: OTHER_NAMESPACE,
    ticket: OTHER_TICKET,
    page: "/pdf/view",
  }, "switch-token-account"));
  const savedB = await h.sendRuntimeMessage({
    type: "BW_ACCOUNT_TOKEN_SAVE",
    target,
    payload: { token: "token-b-one" },
  }, popupSender());
  assert.equal(savedB.ok, true);
  assert.equal(savedB.data.namespace, OTHER_NAMESPACE);
  assert.equal(savedB.data.credential.candidateCount, 1);

  await provider.receive(pageMessage("HELLO", {
    namespace: NAMESPACE,
    ticket: TICKET,
    page: "/pdf/view",
  }, "switch-token-back"));
  status = await h.sendRuntimeMessage({
    type: "BW_ACCOUNT_TOKEN_TEST",
    target,
  }, popupSender());
  assert.equal(status.ok, true);
  assert.equal(status.data.namespace, NAMESPACE);
  assert.equal(status.data.credential.candidateCount, 2);
  assert.equal(JSON.stringify(status).includes("token-a-two"), false);
  assert.equal(JSON.stringify(status).includes("token-b-one"), false);
});

test("token-owner 晚到时账户切换会拒绝旧保存，且失败后新账户仍可恢复保存", async () => {
  let releaseFirst;
  let signalFirst;
  let requestCount = 0;
  const firstStarted = new Promise((resolve) => { signalFirst = resolve; });
  const firstGate = new Promise((resolve) => { releaseFirst = resolve; });
  const h = harness({
    networkHandler: async (url, init) => {
      assert.equal(url, `${ORIGIN}/api/reader/token-owner`);
      requestCount += 1;
      const token = authorizationHeader(init).replace(/^Bearer\s+/, "");
      if (requestCount === 1) {
        signalFirst();
        await firstGate;
        return jsonResponse({ ok: true, storage_namespace: NAMESPACE });
      }
      assert.equal(token, "token-b-after-switch");
      return jsonResponse({ ok: true, storage_namespace: OTHER_NAMESPACE });
    },
  });
  const provider = makePort("/pdf/view");
  await authorizePort(h, provider);
  const target = { tabId: provider.sender.tab.id, frameId: 0 };

  const pendingSave = h.sendRuntimeMessage({
    type: "BW_ACCOUNT_TOKEN_SAVE",
    target,
    payload: { token: "token-a-late-secret" },
  }, popupSender());
  await firstStarted;
  await provider.receive(pageMessage("HELLO", {
    namespace: OTHER_NAMESPACE,
    ticket: OTHER_TICKET,
    page: "/pdf/view",
  }, "switch-during-token-owner"));
  releaseFirst();
  const stale = await pendingSave;
  assert.equal(stale.ok, false);
  assert.ok(
    ["BW_ACCOUNT_CONTEXT_STALE", "BW_PROVIDER_DISCONNECTED"]
      .includes(stale.code),
    stale.code,
  );
  assert.equal(JSON.stringify(stale).includes("token-a-late-secret"), false);

  const recovered = await h.sendRuntimeMessage({
    type: "BW_ACCOUNT_TOKEN_SAVE",
    target,
    payload: { token: "token-b-after-switch" },
  }, popupSender());
  assert.equal(recovered.ok, true, JSON.stringify(recovered));
  assert.equal(recovered.data.namespace, OTHER_NAMESPACE);
  assert.equal(recovered.data.credential.candidateCount, 1);
  assert.equal(JSON.stringify(recovered).includes("token-b-after-switch"), false);
});

test("token-owner 上游响应或异常即使回显 token，也只向 popup 返回通用错误", async () => {
  const secret = "token-must-not-return-to-popup";
  const thrownSecret = "token-must-not-return-from-thrown-error";
  let requestCount = 0;
  const h = harness({
    networkHandler: async (url) => {
      assert.equal(url, `${ORIGIN}/api/reader/token-owner`);
      requestCount += 1;
      if (requestCount === 2) {
        throw new Error(`network failed with ${thrownSecret}`);
      }
      return jsonResponse({
        ok: false,
        error: `invalid Authorization: Bearer ${secret}`,
      }, 401);
    },
  });
  const provider = makePort("/pdf/view");
  await authorizePort(h, provider);
  const rejected = await h.sendRuntimeMessage({
    type: "BW_ACCOUNT_TOKEN_SAVE",
    target: { tabId: provider.sender.tab.id, frameId: 0 },
    payload: { token: secret },
  }, popupSender());
  assert.equal(rejected.ok, false);
  assert.equal(rejected.code, "BW_ACCOUNT_TOKEN_INVALID");
  assert.equal(rejected.error, "设备令牌验证失败");
  assert.equal(JSON.stringify(rejected).includes(secret), false);

  const networkRejected = await h.sendRuntimeMessage({
    type: "BW_ACCOUNT_TOKEN_SAVE",
    target: { tabId: provider.sender.tab.id, frameId: 0 },
    payload: { token: thrownSecret },
  }, popupSender());
  assert.equal(networkRejected.ok, false);
  assert.equal(networkRejected.code, "BW_ACCOUNT_OPERATION");
  assert.equal(networkRejected.error, "设备令牌操作失败，请稍后重试");
  assert.equal(JSON.stringify(networkRejected).includes(thrownSecret), false);
});

test("普通网页 bw-fetch 使用已验证的持久账户，不依赖仍存活的 PWA provider，且旧裸缓存不能命中", async () => {
  const oldBody = JSON.stringify({ ok: true, source: "old-bare-cache" });
  const h = harness({
    storageState: {
      dictCache: {
        "dq:isolation:en": { body: oldBody, ts: 1 },
      },
    },
  });
  const provider = makePort("/pdf/view");
  await authorizePort(h, provider);
  provider.disconnect();
  const fetchPort = makeFetchPort(ordinaryContentSender());
  h.connect(fetchPort);
  await fetchPort.receive({
    id: "old-cache-request",
    url: `${ORIGIN}/pdf/api/dict-quick?word=isolation&langs=en`,
    init: { method: "GET" },
  });
  assert.equal(
    fetchPort.messages.some((message) =>
      message.type === "head" && message.status === 200
    ),
    false,
  );
  assert.equal(
    decodedChunks(fetchPort.messages, "old-cache-request")
      .includes("当前账户的设备令牌"),
    true,
  );
  assert.equal(JSON.stringify(fetchPort.messages).includes("old-bare-cache"), false);

  const otherPage = makeFetchPort(ordinaryContentSender(
    "https://developer.mozilla.org/article",
  ));
  h.connect(otherPage);
  await otherPage.receive({
    id: "other-ordinary-page",
    url: `${ORIGIN}/pdf/api/dict-quick?word=identity&langs=en`,
    init: { method: "GET" },
  });
  assert.equal(
    decodedChunks(otherPage.messages, "other-ordinary-page")
      .includes("当前账户的设备令牌"),
    true,
    "第二个普通网页应复用持久账户，而不是要求同 document 的 PWA lease",
  );

  fetchPort.messages.length = 0;
  await fetchPort.receive({
    id: "blocked-admin",
    url: `${ORIGIN}/api/admin/users`,
    init: { method: "GET" },
  });
  assert.equal(fetchPort.messages.at(-1).code, "BW_FETCH_OPERATION");
  await fetchPort.receive({
    id: "blocked-method",
    url: `${ORIGIN}/pdf/api/translate`,
    init: { method: "DELETE" },
  });
  assert.equal(fetchPort.messages.at(-1).code, "BW_FETCH_OPERATION");
  assert.equal(h.state.networkRequests.length, 0);
});

test("普通网页 dictionary-cache 使用持久账户 Vault 分区，账户 B 不会命中账户 A", async () => {
  const h = harness({
    networkHandler: async (url, init) => {
      if (url === `${ORIGIN}/api/reader/token-owner`) {
        const token = authorizationHeader(init).replace(/^Bearer\s+/, "");
        return jsonResponse({
          ok: true,
          storage_namespace: token === "token-b" ? OTHER_NAMESPACE : NAMESPACE,
        });
      }
      if (url.startsWith(`${ORIGIN}/pdf/api/dict-quick?`)) {
        const token = authorizationHeader(init);
        return jsonResponse({
          ok: true,
          account: token.endsWith("token-b") ? "B" : "A",
        });
      }
      throw new Error("unexpected test request");
    },
  });
  const provider = makePort("/pdf/view");
  await authorizePort(h, provider);
  const target = { tabId: provider.sender.tab.id, frameId: 0 };
  await h.sendRuntimeMessage({
    type: "BW_ACCOUNT_TOKEN_SAVE",
    target,
    payload: { token: "token-a" },
  }, popupSender());
  const fetchPort = makeFetchPort(ordinaryContentSender());
  h.connect(fetchPort);
  const dictUrl = `${ORIGIN}/pdf/api/dict-quick?word=partition&langs=en`;
  await fetchPort.receive({ id: "dict-a", url: dictUrl, init: { method: "GET" } });
  assert.equal(
    JSON.stringify(fetchPort.messages).includes(
      btoa(JSON.stringify({ ok: true, account: "A" })),
    ),
    true,
  );
  assert.equal(
    provider.messages.some((message) =>
      message.type === "CHANGE" &&
      message.payload?.collection === "dictionary-cache"
    ),
    true,
  );

  await provider.receive(pageMessage("HELLO", {
    namespace: OTHER_NAMESPACE,
    ticket: OTHER_TICKET,
    page: "/pdf/view",
  }, "cache-account-b"));
  fetchPort.messages.length = 0;
  await fetchPort.receive({ id: "dict-b-no-token", url: dictUrl, init: { method: "GET" } });
  assert.equal(
    fetchPort.messages.some((message) =>
      message.type === "head" && message.status === 200
    ),
    false,
  );
  assert.equal(
    decodedChunks(fetchPort.messages, "dict-b-no-token")
      .includes("当前账户的设备令牌"),
    true,
  );

  await h.sendRuntimeMessage({
    type: "BW_ACCOUNT_TOKEN_SAVE",
    target,
    payload: { token: "token-b" },
  }, popupSender());
  fetchPort.messages.length = 0;
  await fetchPort.receive({ id: "dict-b", url: dictUrl, init: { method: "GET" } });
  assert.equal(
    JSON.stringify(fetchPort.messages).includes(
      btoa(JSON.stringify({ ok: true, account: "B" })),
    ),
    true,
  );
  assert.equal(JSON.stringify(fetchPort.messages).includes('"account":"A"'), false);
  assert.equal(h.state.stores.length, 2);
});

test("普通网页译文缓存按账户与服务端命名空间隔离，GET 只读本地且 URL 不出扩展", async () => {
  const googleNamespace = "web-google-v1-gtranslate-v2";
  const aiNamespace = "web-ai-v1-claude-abc12345-sonnet-12345678";
  const oldTranslation = {
    "https://example.com/article": {
      ts: 1,
      items: { hello: "旧裸缓存译文" },
    },
  };
  const h = harness({
    storageState: {
      apiToken: "old-token",
      dictCache: { old: { body: "old" } },
      webTrCacheV1: structuredClone(oldTranslation),
      ephSettingsV1: { "eph-gp-floating": "1" },
    },
    networkHandler: async (url, init) => {
      if (url === `${ORIGIN}/api/reader/token-owner`) {
        return jsonResponse({ ok: true, storage_namespace: NAMESPACE });
      }
      if (url === `${ORIGIN}/pdf/api/web-translate`) {
        const body = JSON.parse(init.body);
        assert.equal(Object.hasOwn(body, "url"), false);
        assert.deepEqual(body.texts, ["hello"]);
        if (body.backend === "ai") {
          assert.deepEqual(body.glossary, { term: "术语" });
          return jsonResponse({
            ok: true,
            zh: ["账户 A AI 译文"],
            sources: ["ai"],
            cacheNamespace: aiNamespace,
            googleCacheNamespace: googleNamespace,
          });
        }
        assert.equal(body.backend, "google");
        return jsonResponse({
          ok: true,
          zh: ["账户 A Google 译文"],
          sources: ["google"],
          cacheNamespace: googleNamespace,
          googleCacheNamespace: googleNamespace,
        });
      }
      throw new Error("unexpected translation request");
    },
  });
  const provider = makePort("/pdf/view");
  await authorizePort(h, provider);
  const target = { tabId: provider.sender.tab.id, frameId: 0 };
  await h.sendRuntimeMessage({
    type: "BW_ACCOUNT_TOKEN_SAVE",
    target,
    payload: { token: "token-a" },
  }, popupSender());
  const fetchPort = makeFetchPort(ordinaryContentSender());
  h.connect(fetchPort);
  const pageUrl = "https://example.com/article";
  await fetchPort.receive({
    id: "translate-a",
    url: `${ORIGIN}/pdf/api/web-translate`,
    init: {
      method: "POST",
      body: JSON.stringify({
        texts: ["hello"],
        url: pageUrl,
        backend: "google",
      }),
    },
  });
  await fetchPort.receive({
    id: "translate-a-ai",
    url: `${ORIGIN}/pdf/api/web-translate`,
    init: {
      method: "POST",
      body: JSON.stringify({
        texts: ["hello"],
        url: `${pageUrl}#fragment`,
        backend: "ai",
        glossary: { term: "术语" },
      }),
    },
  });
  assert.equal(
    provider.messages.some((message) =>
      message.type === "CHANGE" &&
      message.payload?.collection === "translation-cache"
    ),
    true,
  );

  const accountAEnvelope = await h.sendRuntimeMessage({
    type: "BW_TRANSLATION_CACHE_GET",
    cacheNamespace: googleNamespace,
  }, ordinaryContentSender(pageUrl));
  assert.equal(accountAEnvelope.ok, true, JSON.stringify(accountAEnvelope));
  const accountAResult = accountAEnvelope.data;
  assert.equal(accountAResult.items.hello, "账户 A Google 译文");
  assert.equal(accountAResult.cacheNamespace, googleNamespace);
  assert.equal(JSON.stringify(accountAResult).includes("旧裸缓存译文"), false);

  const accountAAiEnvelope = await h.sendRuntimeMessage({
    type: "BW_TRANSLATION_CACHE_GET",
    cacheNamespace: aiNamespace,
  }, ordinaryContentSender(pageUrl));
  assert.equal(accountAAiEnvelope.ok, true, JSON.stringify(accountAAiEnvelope));
  const accountAAiResult = accountAAiEnvelope.data;
  assert.equal(accountAAiResult.items.hello, "账户 A AI 译文");
  assert.equal(accountAAiResult.cacheNamespace, aiNamespace);

  await provider.receive(pageMessage("HELLO", {
    namespace: OTHER_NAMESPACE,
    ticket: OTHER_TICKET,
    page: "/pdf/view",
  }, "translation-account-b"));
  const accountBEnvelope = await h.sendRuntimeMessage({
    type: "BW_TRANSLATION_CACHE_GET",
    cacheNamespace: googleNamespace,
  }, ordinaryContentSender(pageUrl));
  assert.equal(accountBEnvelope.ok, true, JSON.stringify(accountBEnvelope));
  const accountBResult = accountBEnvelope.data;
  assert.deepEqual(Object.keys(accountBResult.items), []);
  fetchPort.messages.length = 0;
  await fetchPort.receive({
    id: "retired-translation-cache",
    url: `${ORIGIN}/pdf/api/web-trcache?url=${encodeURIComponent(pageUrl)}`,
    init: { method: "GET" },
  });
  assert.equal(
    fetchPort.messages.some((message) =>
      message.id === "retired-translation-cache" &&
      message.type === "error" &&
      message.code === "BW_FETCH_OPERATION"
    ),
    true,
  );
  assert.equal(
    h.state.networkRequests.some(({ url }) =>
      url.startsWith(`${ORIGIN}/pdf/api/web-trcache?`)
    ),
    false,
  );
  for (const request of h.state.networkRequests.filter(
    ({ url }) => url === `${ORIGIN}/pdf/api/web-translate`,
  )) {
    assert.equal(Object.hasOwn(JSON.parse(request.init.body), "url"), false);
  }
  assert.deepEqual(h.state.storageState.webTrCacheV1, oldTranslation);
  assert.equal(h.state.storageState.apiToken, "old-token");
  assert.deepEqual(h.state.storageState.ephSettingsV1, { "eph-gp-floating": "1" });
});

test("网页 AI 模式只取扩展权威设置，敌对字段不出站且每个 bw-fetch port 绑定随机文档 UUID", async () => {
  const aiNamespace = "web-ai-v2-claude-sonnet-session";
  const statelessNamespace = "web-ai-v2-claude-sonnet-stateless";
  const googleNamespace = "web-google-v1-gtranslate-v2";
  const translated = [];
  const h = harness({
    storageState: {
      bwReaderExtensionPreferencesV2: {
        schema: 2,
        values: {
          "eph-web-tr-mode": "session",
          "eph-web-tr-threshold": "73",
        },
        updatedAt: 10,
      },
    },
    networkHandler: async (url, init) => {
      if (url === `${ORIGIN}/api/reader/token-owner`) {
        return jsonResponse({ ok: true, storage_namespace: NAMESPACE });
      }
      if (url === `${ORIGIN}/pdf/api/web-translate`) {
        const body = JSON.parse(init.body);
        const documentId = init.headers.get("X-BW-Translate-Document");
        translated.push({ body, documentId });
        return jsonResponse({
          ok: true,
          zh: body.texts.map((text) => `译:${text}`),
          sources: body.texts.map(() => "ai"),
          // 单值是旧客户端兼容别名；正式缓存桶必须按实际 resolved mode 选。
          cacheNamespace: statelessNamespace,
          cacheNamespaces: {
            stateless: statelessNamespace,
            session: aiNamespace,
          },
          modeRequested: "session",
          modeResolved: "session",
          googleCacheNamespace: googleNamespace,
        });
      }
      throw new Error("unexpected translation request");
    },
  });
  const provider = makePort("/pdf/view");
  await authorizePort(h, provider);
  const target = { tabId: provider.sender.tab.id, frameId: 0 };
  await h.sendRuntimeMessage({
    type: "BW_ACCOUNT_TOKEN_SAVE",
    target,
    payload: { token: "token-a" },
  }, popupSender());

  const pageUrl = "https://example.com/mode-contract";
  const first = makeFetchPort(ordinaryContentSender(pageUrl));
  const second = makeFetchPort(ordinaryContentSender(pageUrl));
  h.connect(first);
  h.connect(second);
  const hostile = (text) => ({
    texts: [text],
    backend: "ai",
    glossary: { safe: "术语" },
    mode: "stateless",
    estimatedUnits: 999,
    url: "https://attacker.invalid/private",
    model: "attacker-model",
    sessionKey: "attacker-session",
    session_key: "attacker-session-snake",
    cacheNamespace: "web-ai-v2-attacker",
    pageUnits: 1,
  });
  await first.receive({
    id: "mode-authority-a",
    url: `${ORIGIN}/pdf/api/web-translate`,
    init: {
      method: "POST",
      headers: { "X-BW-Translate-Document": "attacker-document" },
      body: JSON.stringify(hostile("first")),
    },
  });
  await first.receive({
    id: "mode-authority-b",
    url: `${ORIGIN}/pdf/api/web-translate`,
    init: { method: "POST", body: JSON.stringify(hostile("second")) },
  });
  await second.receive({
    id: "mode-authority-c",
    url: `${ORIGIN}/pdf/api/web-translate`,
    init: { method: "POST", body: JSON.stringify(hostile("third")) },
  });

  assert.equal(translated.length, 3);
  for (const request of translated) {
    assert.equal(request.body.mode, "session");
    assert.deepEqual(request.body.glossary, { safe: "术语" });
    for (const field of [
      "url",
      "model",
      "sessionKey",
      "session_key",
      "cacheNamespace",
      "pageUnits",
      "estimatedUnits",
    ]) {
      assert.equal(Object.hasOwn(request.body, field), false, field);
    }
    assert.match(
      request.documentId,
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
    assert.notEqual(request.documentId, "attacker-document");
  }
  assert.equal(translated[0].documentId, translated[1].documentId);
  assert.notEqual(translated[0].documentId, translated[2].documentId);
  assert.equal(
    h.state.storageReads.includes("bwReaderExtensionPreferencesV2"),
    true,
  );

  const cached = await h.sendRuntimeMessage({
    type: "BW_TRANSLATION_CACHE_GET",
    cacheNamespace: aiNamespace,
  }, ordinaryContentSender(pageUrl));
  assert.equal(cached.ok, true, JSON.stringify(cached));
  assert.equal(cached.data.items.first, "译:first");
  assert.equal(cached.data.items.second, "译:second");
  assert.equal(cached.data.items.third, "译:third");
  assert.equal(cached.data.cacheNamespace, aiNamespace);

  const wrongBucket = await h.sendRuntimeMessage({
    type: "BW_TRANSLATION_CACHE_GET",
    cacheNamespace: statelessNamespace,
  }, ordinaryContentSender(pageUrl));
  assert.equal(Object.keys(wrongBucket.data.items).length, 0);
});

test("网页翻译 auto 对短内容用 stateless、超过扩展阈值才用 session，未知值安全落 stateless", async () => {
  const outbound = [];
  const h = harness({
    storageState: {
      bwReaderExtensionPreferencesV2: {
        schema: 2,
        values: {
          "eph-web-tr-mode": "auto",
          "eph-web-tr-threshold": "50",
        },
        updatedAt: 20,
      },
    },
    networkHandler: async (url, init) => {
      if (url === `${ORIGIN}/api/reader/token-owner`) {
        return jsonResponse({ ok: true, storage_namespace: NAMESPACE });
      }
      if (url === `${ORIGIN}/pdf/api/web-translate`) {
        outbound.push(JSON.parse(init.body));
        return jsonResponse({
          ok: true,
          zh: ["ok"],
          sources: ["ai"],
          cacheNamespace: "web-ai-v2-claude-sonnet-stateless",
          googleCacheNamespace: "web-google-v1-gtranslate-v2",
        });
      }
      throw new Error("unexpected translation request");
    },
  });
  const provider = makePort("/pdf/view");
  await authorizePort(h, provider);
  const target = { tabId: provider.sender.tab.id, frameId: 0 };
  await h.sendRuntimeMessage({
    type: "BW_ACCOUNT_TOKEN_SAVE",
    target,
    payload: { token: "token-a" },
  }, popupSender());
  const fetchPort = makeFetchPort(
    ordinaryContentSender("https://example.com/auto-mode"),
  );
  h.connect(fetchPort);

  const send = async (id, estimatedUnits, attackerMode, pageUnits = 500) => {
    await fetchPort.receive({
      id,
      url: `${ORIGIN}/pdf/api/web-translate`,
      init: {
        method: "POST",
        body: JSON.stringify({
          texts: [id],
          backend: "ai",
          mode: attackerMode,
          estimatedUnits,
          pageUnits,
        }),
      },
    });
  };
  await send("auto-short", 50, "stateless");
  await send("auto-long", 100, "session");
  await send("auto-unknown", "unknown", "session", 1);

  assert.deepEqual(outbound.map((body) => body.mode), [
    "stateless",
    "session",
    "stateless",
  ]);
  for (const body of outbound) {
    assert.equal(Object.hasOwn(body, "estimatedUnits"), false);
    assert.equal(Object.hasOwn(body, "pageUnits"), false);
  }
});

test("普通网页请求不随 PWA provider 租约结束而中断，仍由持久账户完成并分区缓存", async () => {
  let releaseTranslation;
  let signalTranslation;
  const translationStarted = new Promise((resolve) => { signalTranslation = resolve; });
  const translationGate = new Promise((resolve) => { releaseTranslation = resolve; });
  const h = harness({
    networkHandler: async (url) => {
      if (url === `${ORIGIN}/api/reader/token-owner`) {
        return jsonResponse({ ok: true, storage_namespace: NAMESPACE });
      }
      if (url === `${ORIGIN}/pdf/api/web-translate`) {
        signalTranslation();
        await translationGate;
        return jsonResponse({ ok: true, zh: ["过期后不得采用"] });
      }
      throw new Error("unexpected translation request");
    },
  });
  const provider = makePort("/pdf/view");
  await authorizePort(h, provider);
  const target = { tabId: provider.sender.tab.id, frameId: 0 };
  await h.sendRuntimeMessage({
    type: "BW_ACCOUNT_TOKEN_SAVE",
    target,
    payload: { token: "token-a" },
  }, popupSender());
  const fetchPort = makeFetchPort(ordinaryContentSender());
  h.connect(fetchPort);
  const pending = fetchPort.receive({
    id: "translation-expiry",
    url: `${ORIGIN}/pdf/api/web-translate`,
    init: {
      method: "POST",
      body: JSON.stringify({
        texts: ["expires"],
        url: "https://example.com/expires",
      }),
    },
  });
  await translationStarted;
  h.state.nowMs = TICKET_EXPIRES_AT * 1000 + 1;
  provider.disconnect();
  releaseTranslation();
  await pending;

  assert.equal(
    fetchPort.messages.some((message) =>
      message.type === "head" && message.status === 200
    ),
    true,
  );
  assert.equal(
    decodedChunks(fetchPort.messages, "translation-expiry")
      .includes("过期后不得采用"),
    true,
  );
  assert.equal(
    h.state.stores[0] != null,
    true,
  );
  assert.equal(h.state.accountContexts[0].snapshot().active, true);
});

test("可信 PWA 切换持久账户时，普通网页晚到 bw-fetch 被 generation 围栏丢弃", async () => {
  let releaseDictionary;
  let signalDictionary;
  const dictionaryStarted = new Promise((resolve) => { signalDictionary = resolve; });
  const dictionaryGate = new Promise((resolve) => { releaseDictionary = resolve; });
  const h = harness({
    networkHandler: async (url) => {
      if (url === `${ORIGIN}/api/reader/token-owner`) {
        return jsonResponse({ ok: true, storage_namespace: NAMESPACE });
      }
      if (url.startsWith(`${ORIGIN}/pdf/api/dict-quick?`)) {
        signalDictionary();
        await dictionaryGate;
        return jsonResponse({ ok: true, secret: "late-account-a-result" });
      }
      throw new Error("unexpected test request");
    },
  });
  const provider = makePort("/pdf/view");
  await authorizePort(h, provider);
  const target = { tabId: provider.sender.tab.id, frameId: 0 };
  await h.sendRuntimeMessage({
    type: "BW_ACCOUNT_TOKEN_SAVE",
    target,
    payload: { token: "token-a" },
  }, popupSender());
  const fetchPort = makeFetchPort(ordinaryContentSender());
  h.connect(fetchPort);
  const pending = fetchPort.receive({
    id: "late-dictionary",
    url: `${ORIGIN}/pdf/api/dict-quick?word=late&langs=en`,
    init: { method: "GET" },
  });
  await dictionaryStarted;
  await provider.receive(pageMessage("HELLO", {
    namespace: OTHER_NAMESPACE,
    ticket: OTHER_TICKET,
    page: "/pdf/view",
  }, "switch-while-fetching"));
  releaseDictionary();
  await pending;

  assert.equal(fetchPort.messages.some((message) => message.type === "head"), false);
  assert.equal(
    JSON.stringify(fetchPort.messages).includes("late-account-a-result"),
    false,
  );
  assert.ok(
    ["BW_ACCOUNT_CONTEXT_STALE", "BW_PROVIDER_DISCONNECTED"]
      .includes(fetchPort.messages.at(-1).code),
    fetchPort.messages.at(-1).code,
  );
});

test("电脑语音 relay 只接受扩展顶层 canonical HTTP(S) sender，并固定 Windows WSS", async () => {
  const h = harness();
  await settleBackground();
  const storageReads = h.state.storageReads.length;

  for (const sender of [
    computerVoiceDirectSender("https://example.com/rejected", {
      id: "other-extension",
    }),
    computerVoiceDirectSender("https://example.com/rejected", {
      frameId: 1,
    }),
    computerVoiceDirectSender("https://example.com/rejected", {
      tab: null,
    }),
    computerVoiceDirectSender("https://example.com/rejected", {
      url: "data:text/plain,rejected",
    }),
    computerVoiceDirectSender("https://example.com/rejected", {
      url: "https://EXAMPLE.com/rejected",
    }),
    computerVoiceDirectSender("https://example.com/rejected", {
      url: undefined,
    }),
  ]) {
    const rejected = makeComputerVoiceDirectPort(sender);
    h.connect(rejected);
    assert.equal(rejected.disconnected, true);
  }
  assert.equal(h.state.webSockets.length, 0);

  const port = makeComputerVoiceDirectPort(computerVoiceDirectSender());
  h.connect(port);
  await port.receive({ type: "open" });
  assert.equal(h.state.webSockets.length, 1);
  const socket = h.state.webSockets[0];
  assert.equal(
    socket.url,
    "wss://bwicarus-2.taile44d0c.ts.net/reader-computer-voice/v1",
  );
  assert.equal(socket.binaryType, "arraybuffer");
  assert.deepEqual(socket.sent, [], "relay 自身不得生成 START 或其它控制帧");
  assert.equal(
    h.state.storageReads.length,
    storageReads,
    "Windows relay 不得读取配对或账户 storage",
  );

  socket.open();
  assert.deepEqual(port.messages, [{ type: "open" }]);
  await port.receive({ type: "send-text", data: "client-control-frame" });
  assert.deepEqual(socket.sent, ["client-control-frame"]);

  const pcmFrame = Uint8Array.from(
    { length: 1956 },
    (_, index) => index & 0xff,
  );
  socket.receive("server-control-frame");
  socket.receive(pcmFrame.buffer);
  await settleBackground();
  assert.deepEqual(port.messages.slice(1), [
    { type: "text", data: "server-control-frame" },
    {
      type: "binary-base64",
      data: Buffer.from(pcmFrame).toString("base64"),
      bytes: 1956,
    },
  ]);
});

test("电脑语音 relay 严格限制操作、字段、文本大小与固定 PCM 帧长", async () => {
  const rejectedMessages = [
    { type: "open", url: "wss://attacker.invalid/" },
    { type: "send-text", data: "not-open" },
    { type: "send-text", data: 1 },
    { type: "send-text", data: "x", extra: true },
    { type: "close", code: 1000 },
    { type: "send-binary", data: "AA==" },
    { type: "unknown" },
    null,
  ];
  for (const message of rejectedMessages) {
    const h = harness();
    const port = makeComputerVoiceDirectPort(computerVoiceDirectSender());
    h.connect(port);
    await port.receive(message);
    const error = port.messages.find((entry) => entry.type === "error");
    assert.deepEqual(
      Object.keys(error || {}).sort(),
      ["code", "error", "type"],
      JSON.stringify(message),
    );
    assert.match(error.code, /^BW_COMPUTER_VOICE_DIRECT_/);
    const close = port.messages.find((entry) => entry.type === "close");
    assert.deepEqual(
      Object.keys(close || {}).sort(),
      ["code", "reason", "type", "wasClean"],
      JSON.stringify(message),
    );
    assert.equal(close.wasClean, false, JSON.stringify(message));
  }

  const outbound = harness();
  const outboundPort = makeComputerVoiceDirectPort(
    computerVoiceDirectSender(),
  );
  outbound.connect(outboundPort);
  await outboundPort.receive({ type: "open" });
  const outboundSocket = outbound.state.webSockets[0];
  outboundSocket.open();
  await outboundPort.receive({
    type: "send-text",
    data: "界".repeat(21846),
  });
  assert.equal(
    outboundPort.messages.some((entry) =>
      entry.type === "error" &&
      entry.code === "BW_COMPUTER_VOICE_DIRECT_CAPACITY"
    ),
    true,
  );
  assert.deepEqual(outboundSocket.sent, []);
  assert.equal(outboundSocket.readyState, 2);

  const inbound = harness();
  const inboundPort = makeComputerVoiceDirectPort(computerVoiceDirectSender());
  inbound.connect(inboundPort);
  await inboundPort.receive({ type: "open" });
  const inboundSocket = inbound.state.webSockets[0];
  inboundSocket.open();
  inboundSocket.receive(new Uint8Array(64 * 1024 + 1).buffer);
  await settleBackground();
  assert.equal(
    inboundPort.messages.some((entry) =>
      entry.type === "error" &&
      entry.code === "BW_COMPUTER_VOICE_DIRECT_FRAME"
    ),
    true,
  );
  assert.equal(
    inboundPort.messages.some((entry) =>
      entry.type === "binary-base64"
    ),
    false,
  );
  assert.equal(inboundSocket.readyState, 2);
});

test("电脑语音 relay 每个标签页只保留一个连接，所有终态清理且不自动重连", async () => {
  const h = harness();
  const sender = computerVoiceDirectSender(
    "https://example.com/same-tab",
  );
  const first = makeComputerVoiceDirectPort(sender);
  h.connect(first);

  const duplicate = makeComputerVoiceDirectPort(sender);
  h.connect(duplicate);
  assert.equal(duplicate.disconnected, true);
  assert.deepEqual(duplicate.messages, [{
    type: "error",
    code: "BW_COMPUTER_VOICE_DIRECT_TAB",
    error: "当前标签页已有 Windows 语音直连",
  }]);

  await first.receive({ type: "open" });
  const firstSocket = h.state.webSockets[0];
  firstSocket.open();
  await first.receive({ type: "open" });
  assert.equal(
    first.messages.some((entry) =>
      entry.type === "error" &&
      entry.code === "BW_COMPUTER_VOICE_DIRECT_STATE"
    ),
    true,
  );
  assert.equal(firstSocket.readyState, 2);

  const afterProtocolError = makeComputerVoiceDirectPort(sender);
  h.connect(afterProtocolError);
  await afterProtocolError.receive({ type: "open" });
  const secondSocket = h.state.webSockets[1];
  secondSocket.open();
  secondSocket.networkError();
  assert.equal(
    afterProtocolError.messages.some((entry) =>
      entry.type === "error" &&
      entry.code === "BW_COMPUTER_VOICE_DIRECT_NETWORK"
    ),
    true,
  );
  await settleBackground();
  assert.equal(h.state.webSockets.length, 2, "网络错误后不得自动重连");

  const afterNetworkError = makeComputerVoiceDirectPort(sender);
  h.connect(afterNetworkError);
  await afterNetworkError.receive({ type: "open" });
  const thirdSocket = h.state.webSockets[2];
  thirdSocket.open();
  thirdSocket.serverClose(1001, "s".repeat(200), true);
  assert.deepEqual(afterNetworkError.messages.at(-1), {
    type: "close",
    code: 1001,
    reason: "s".repeat(123),
    wasClean: true,
  });

  const afterServerClose = makeComputerVoiceDirectPort(sender);
  h.connect(afterServerClose);
  await afterServerClose.receive({ type: "open" });
  const fourthSocket = h.state.webSockets[3];
  fourthSocket.open();
  await afterServerClose.receive({ type: "close" });
  assert.deepEqual(fourthSocket.closeCalls, [{
    code: 1000,
    reason: "",
  }]);
  assert.deepEqual(afterServerClose.messages.at(-1), {
    type: "close",
    code: 1000,
    reason: "",
    wasClean: true,
  });

  const disconnected = makeComputerVoiceDirectPort(sender);
  h.connect(disconnected);
  await disconnected.receive({ type: "open" });
  const fifthSocket = h.state.webSockets[4];
  disconnected.disconnect();
  assert.deepEqual(fifthSocket.closeCalls, [{
    code: 1000,
    reason: "content-disconnected",
  }]);

  const afterDisconnect = makeComputerVoiceDirectPort(sender);
  h.connect(afterDisconnect);
  assert.equal(afterDisconnect.disconnected, false);
});

test("bw-ws 只接受扩展顶层网页脚本，并要求已验证账户和设备令牌", async () => {
  const untrusted = harness();
  for (const sender of [
    ordinaryContentSender("https://example.com/ws", { id: "other-extension" }),
    ordinaryContentSender("https://example.com/ws", { frameId: 2 }),
    ordinaryContentSender("https://example.com/ws", { tab: null }),
  ]) {
    const port = makeWsPort(sender);
    untrusted.connect(port);
    assert.equal(port.disconnected, true);
  }
  assert.equal(untrusted.state.webSockets.length, 0);

  const missingAccount = makeWsPort(ordinaryContentSender());
  untrusted.connect(missingAccount);
  await missingAccount.receive({ type: "open", path: "/voice-rt?mode=tts" });
  await settleBackground();
  assert.equal(
    missingAccount.messages.some((message) =>
      message.type === "error" &&
      message.code === "BW_ACCOUNT_CONTEXT_UNAVAILABLE"
    ),
    true,
  );
  assert.equal(untrusted.state.webSockets.length, 0);

  const noToken = harness();
  const provider = makePort("/pdf/view");
  await authorizePort(noToken, provider);
  const missingToken = makeWsPort(ordinaryContentSender());
  noToken.connect(missingToken);
  await missingToken.receive({ type: "open", path: "/voice-rt?mode=tts" });
  await settleBackground();
  assert.equal(
    missingToken.messages.some((message) =>
      message.type === "error" &&
      message.code === "BW_ACCOUNT_TOKEN_MISSING"
    ),
    true,
  );
  assert.equal(noToken.state.webSockets.length, 0);
});

test("bw-ws 严格限制 voice-rt 路由，并保持文本与二进制帧逐字节双向传输", async () => {
  const h = harness({
    networkHandler: async (url) => {
      if (url === `${ORIGIN}/api/reader/token-owner`) {
        return jsonResponse({ ok: true, storage_namespace: NAMESPACE });
      }
      throw new Error("unexpected network request");
    },
  });
  const provider = makePort("/pdf/view");
  await authorizePort(h, provider);
  await saveAccountToken(h, provider, "token-a");

  const rejectedPaths = [
    "wss://bwicarus.taile44d0c.ts.net/voice-rt?mode=tts",
    "//attacker.invalid/voice-rt?mode=tts",
    "/voice-rt/extra?mode=tts",
    "/voice-rt?mode=tts#fragment",
    "/voice-rt?mode=tts&file=blocked",
    "/voice-rt?mode=tts&mode=agent",
    "/voice-rt?mode=unknown",
  ];
  for (const path of rejectedPaths) {
    const port = makeWsPort(ordinaryContentSender());
    h.connect(port);
    await port.receive({ type: "open", path });
    await settleBackground();
    assert.equal(
      port.messages.some((message) =>
        message.type === "error" &&
        ["BW_WS_OPERATION", "BW_WS_ORIGIN"].includes(message.code)
      ),
      true,
      path,
    );
  }
  assert.equal(h.state.webSockets.length, 0);

  const port = makeWsPort(ordinaryContentSender());
  h.connect(port);
  await port.receive({ type: "open", path: "/voice-rt?mode=tts" });
  await settleBackground();
  assert.equal(h.state.webSockets.length, 1);
  const socket = h.state.webSockets[0];
  assert.equal(
    socket.url,
    "wss://bwicarus.taile44d0c.ts.net/voice-rt?mode=tts",
  );
  assert.equal(socket.binaryType, "arraybuffer");

  socket.open();
  assert.deepEqual(
    port.messages.find((message) => message.type === "open"),
    {
      type: "open",
      url: "wss://bwicarus.taile44d0c.ts.net/voice-rt?mode=tts",
    },
  );

  await port.receive({ type: "send", data: "client-text" });
  await port.receive({
    type: "send",
    binary: true,
    bytes: 5,
    b64: Buffer.from([0, 1, 127, 128, 255]).toString("base64"),
  });
  assert.equal(socket.sent[0], "client-text");
  assert.equal(socket.sent.length, 2, JSON.stringify(port.messages));
  assert.deepEqual(
    Array.from(socket.sent[1]),
    [0, 1, 127, 128, 255],
  );

  socket.receive("server-text");
  socket.receive(new Uint8Array([255, 128, 127, 1, 0]).buffer);
  await settleBackground();
  assert.equal(
    port.messages.some((message) =>
      message.type === "message" && message.data === "server-text"
    ),
    true,
  );
  assert.deepEqual(
    port.messages.find((message) =>
      message.type === "message" && message.binary === true
    ),
    {
      type: "message",
      binary: true,
      bytes: 5,
      b64: Buffer.from([255, 128, 127, 1, 0]).toString("base64"),
    },
  );

  port.disconnect();
  assert.equal(socket.readyState, 2);
  assert.deepEqual(socket.closeCalls, [{
    code: 1000,
    reason: "content disconnected",
  }]);
  assert.equal(socket.onopen, null);
  assert.equal(socket.onmessage, null);
  assert.equal(socket.onerror, null);
  assert.equal(socket.onclose, null);
});

test("bw-ws 账户切换后阻断旧连接的迟到帧", async () => {
  const h = harness({
    networkHandler: async (url, init) => {
      if (url === `${ORIGIN}/api/reader/token-owner`) {
        const token = authorizationHeader(init).replace(/^Bearer\s+/, "");
        return jsonResponse({
          ok: true,
          storage_namespace: token === "token-b"
            ? OTHER_NAMESPACE
            : NAMESPACE,
        });
      }
      throw new Error("unexpected network request");
    },
  });
  const provider = makePort("/pdf/view");
  await authorizePort(h, provider);
  await saveAccountToken(h, provider, "token-a");

  const port = makeWsPort(ordinaryContentSender());
  h.connect(port);
  await port.receive({ type: "open", path: "/voice-rt?mode=tts" });
  await settleBackground();
  const socket = h.state.webSockets[0];
  socket.open();
  assert.equal(
    port.messages.some((message) => message.type === "open"),
    true,
  );

  await provider.receive(pageMessage("HELLO", {
    namespace: OTHER_NAMESPACE,
    ticket: OTHER_TICKET,
    page: "/pdf/view",
  }, "switch-while-ws-open"));
  assert.equal(provider.messages.at(-1).type, "READY");
  h.state.activeTabId = provider.sender.tab.id;
  await saveAccountToken(h, provider, "token-b");

  // 当前实现会在账户切换后的下一次凭据围栏立即终止旧连接；若实现改成
  // 等服务器下一帧才检查，下面仍主动投递一帧来证明它不能越过旧 lease。
  if (socket.readyState === 1) {
    socket.receive("late-account-a-frame");
  }
  await settleBackground();
  assert.equal(
    port.messages.some((message) =>
      message.type === "message" &&
      message.data === "late-account-a-frame"
    ),
    false,
  );
  assert.equal(
    port.messages.some((message) =>
      message.type === "error" &&
      message.code === "BW_ACCOUNT_CONTEXT_STALE"
    ),
    true,
  );
  assert.deepEqual(socket.closeCalls, [{
    code: 1008,
    reason: "BW WebSocket bridge rejected",
  }]);
  assert.equal(
    port.messages.some((message) =>
      message.type === "close" && message.wasClean === false
    ),
    true,
  );
});
