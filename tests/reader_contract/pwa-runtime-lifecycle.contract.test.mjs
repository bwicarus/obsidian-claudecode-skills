import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SOURCE = readFileSync(
  new URL("../../_server_deploy/static/reader-runtime/pwa-runtime.js", import.meta.url),
  "utf8",
);
const ACCOUNT_CONTEXT_SOURCE = readFileSync(
  new URL("../../_server_deploy/static/reader-runtime/account-context.js", import.meta.url),
  "utf8",
);
const DATA_REGISTRY_SOURCE = readFileSync(
  new URL("../../_server_deploy/static/reader-runtime/data-registry.js", import.meta.url),
  "utf8",
);
const PREFERENCE_STORE_SOURCE = readFileSync(
  new URL("../../_server_deploy/static/reader-runtime/preference-store.js", import.meta.url),
  "utf8",
);
const SYNC_CONFLICT_CONTROL_SOURCE = readFileSync(
  new URL("../../_server_deploy/static/reader-runtime/sync-conflict-control.js", import.meta.url),
  "utf8",
);
const SYNC_OWNER_LEASE_SOURCE = readFileSync(
  new URL("../../_server_deploy/static/reader-runtime/sync-owner-lease.js", import.meta.url),
  "utf8",
);
const NAMESPACE = `acct-v1-${"b".repeat(64)}`;
const TICKET = `pvt-v2-4102444800-${"a".repeat(32)}-${"c".repeat(64)}`;
const REFRESHED_TICKET = `pvt-v2-4102444800-${"d".repeat(32)}-${"e".repeat(64)}`;
let fakeStoreEpochSequence = 0;

function nextFakeStoreEpoch() {
  fakeStoreEpochSequence += 1;
  return "data-store-instance-v1-" +
    fakeStoreEpochSequence.toString(16).padStart(32, "0");
}

function expectedOwnerClaim(root, hostKind = "pdf") {
  return {
    contract: "pwa-extension-owner-claim/1",
    runtimeContract: "pwa-runtime/1",
    hostContract: "document-host/1",
    hostKind,
    markerObserved: true,
    documentLifetime: true,
    pwaServerOwner: "paused",
    pwaDirectOwner: "paused",
    deviceFamilyId: root.BWReaderRuntime.pwaRuntime.installId(),
    syncContract: "sync-v3",
    syncChangeContract: "record-parent-state/1",
    registryDigest: root.BWReaderRuntime.dataRegistry.syncDigest(),
  };
}

function expectedBridgeStart(h, ticket) {
  return {
    namespace: NAMESPACE,
    ticket,
    helloPayload: {
      syncOwnerClaim: expectedOwnerClaim(h.root),
    },
  };
}

function harness({
  storage = new Map(),
  failStarts = 0,
  namespace = NAMESPACE,
  ticket = TICKET,
  marker = true,
  refreshTickets = null,
  refreshFailure = false,
  startGate = null,
  preferenceGetGate = null,
  attachGate = null,
  attachResult = { connected: true },
  detachResult = { connected: false, mode: "pwa-fallback", mirrorConflicts: [] },
  directSync = false,
  syncRuntimeStatus = null,
  ownerLeaseMode = "acquire",
  ownerLeaseSeconds = 30,
  ownerReleaseGate = null,
  ownerReleaseFailure = false,
} = {}) {
  const documentListeners = new Map();
  const rootListeners = new Map();
  const events = [];
  const stores = [];
  const storeOptions = [];
  const bridgeStarts = [];
  const bridgeRestarts = [];
  const attachCalls = [];
  const fetchCalls = [];
  const syncTransports = [];
  const syncExchangeRequests = [];
  const syncRuntimeCalls = [];
  const directHostCalls = [];
  const directLeaderCalls = [];
  const directRelayCalls = [];
  const ownerLeaseRequests = [];
  const ownerLeaseOptions = [];
  const ownerLeaseManagers = [];
  const ownerLeaseTimers = [];
  const lifecycleOrder = [];
  let refreshIndex = 0;
  let host = null;
  let remainingFailures = failStarts;
  let currentOwnerLeaseMode = ownerLeaseMode;
  let ownerGeneration = 0;

  const add = (name, listener) => {
    if (!documentListeners.has(name)) documentListeners.set(name, []);
    documentListeners.get(name).push(listener);
  };
  const addRoot = (name, listener) => {
    if (!rootListeners.has(name)) rootListeners.set(name, []);
    rootListeners.get(name).push(listener);
  };
  class CustomEvent {
    constructor(type, options = {}) {
      this.type = type;
      this.detail = options.detail;
    }
  }
  const document = {
    readyState: "complete",
    documentElement: {
      dataset: marker ? { bwReaderExtensionProvider: "test" } : {},
    },
    addEventListener: add,
    dispatchEvent(event) {
      events.push(event);
      lifecycleOrder.push(`event:${event.type}`);
      for (const listener of documentListeners.get(event.type) || []) listener(event);
      return true;
    },
  };
  const createStore = (options) => {
    storeOptions.push({ ...options });
    const records = new Map();
    const listeners = [];
    let instanceEpoch = nextFakeStoreEpoch();
    const store = {
      closed: false,
      async instanceEpoch() {
        return instanceEpoch;
      },
      rebuildInstance() {
        records.clear();
        instanceEpoch = nextFakeStoreEpoch();
        return instanceEpoch;
      },
      get: async (collection, id, opts = {}) => {
        if (preferenceGetGate) await preferenceGetGate;
        const record = records.get(`${collection}/${id}`) || null;
        if (record?.deleted && !opts.includeDeleted) return null;
        return record ? structuredClone(record) : null;
      },
      list: async () => [],
      put: async (collection, value, opts = {}) => {
        const key = `${collection}/${opts.id || value.id}`;
        const current = records.get(key);
        const record = {
          collection,
          id: opts.id || value.id,
          rev: Number(current?.rev || 0) + 1,
          deleted: false,
          value: structuredClone(value),
        };
        records.set(key, record);
        for (const listener of listeners) listener({
          collection,
          mutationId: opts.mutationId || "",
          record: structuredClone(record),
        });
        return structuredClone(record);
      },
      remove: async (collection, id, opts = {}) => {
        const key = `${collection}/${id}`;
        const current = records.get(key);
        const record = {
          collection,
          id,
          rev: Number(current?.rev || 0) + 1,
          deleted: true,
          value: current?.value || { id },
        };
        records.set(key, record);
        for (const listener of listeners) listener({
          collection,
          mutationId: opts.mutationId || "",
          operation: "remove",
          record: structuredClone(record),
        });
        return structuredClone(record);
      },
      changes: async () => ({ changes: [] }),
      applyChanges: async () => ({ applied: [], conflicts: [], skipped: [] }),
      status: async () => ({ contract: "data-store/1" }),
      subscribe(_query, listener) {
        listeners.push(listener);
        return () => {
          const index = listeners.indexOf(listener);
          if (index >= 0) listeners.splice(index, 1);
        };
      },
      close() {
        this.closed = true;
        lifecycleOrder.push("store:close");
      },
    };
    stores.push(store);
    return store;
  };
  const sandbox = {
    console,
    CustomEvent,
    document,
    addEventListener: addRoot,
    removeEventListener(name, listener) {
      const listeners = rootListeners.get(name) || [];
      const index = listeners.indexOf(listener);
      if (index >= 0) listeners.splice(index, 1);
    },
    setTimeout(callback, delay) {
      const timer = {
        callback,
        delay,
        active: true,
        unref() {},
      };
      ownerLeaseTimers.push(timer);
      return timer;
    },
    clearTimeout(timer) {
      if (timer) timer.active = false;
    },
    location: {
      origin: "https://bwicarus.taile44d0c.ts.net",
      pathname: "/pdf/view",
    },
    localStorage: {
      get length() {
        return storage.size;
      },
      getItem(key) {
        return storage.has(key) ? storage.get(key) : null;
      },
      setItem(key, value) {
        storage.set(key, String(value));
      },
      removeItem(key) {
        storage.delete(key);
      },
      clear() {
        storage.clear();
      },
      key(index) {
        return [...storage.keys()][index] ?? null;
      },
    },
    crypto: {
      getRandomValues(bytes) {
        for (let index = 0; index < bytes.length; index += 1) {
          bytes[index] = (index * 17 + 3) & 255;
        }
        return bytes;
      },
    },
    __USER__: {
      storage_namespace: namespace,
      storage_provider_ticket: ticket,
    },
    RC: {
      documentHost: {
        current() {
          return host;
        },
      },
    },
    BWReaderRuntime: {
      extensionProvider: {
        start(options) {
          bridgeStarts.push(structuredClone(options));
          return true;
        },
        restart(options) {
          bridgeRestarts.push(structuredClone(options));
          return true;
        },
        current() {
          return null;
        },
        disconnect() {},
      },
      indexedDBStore: { createIndexedDBDataStore: createStore },
      runtimeSelector: {
        createReaderRuntime(options) {
          let mode = "pwa-fallback";
          const storeFor = (collection) =>
            collection === "device-preferences"
              ? options.pwaDeviceStore
              : options.pwaStore;
          const storageRouter = {
            get: (collection, id, opts) =>
              storeFor(collection).get(collection, id, opts),
            put: (collection, value, opts) =>
              storeFor(collection).put(collection, value, opts),
            remove: (collection, id, opts) =>
              storeFor(collection).remove(collection, id, opts),
            subscribe: (collection, listener) =>
              storeFor(collection).subscribe({ collection }, listener),
          };
          return {
            async start() {
              lifecycleOrder.push("runtime:start");
              options.ui.mount();
              if (remainingFailures > 0) {
                remainingFailures -= 1;
                throw new Error("intentional boot failure");
              }
              if (startGate) await startGate;
              lifecycleOrder.push("runtime:started");
            },
            mode: () => mode,
            storage: () => storageRouter,
            attachExtension: async (provider) => {
              attachCalls.push(provider);
              if (attachGate) await attachGate;
              if (attachResult.connected) mode = "pwa-extension-provider";
              return structuredClone(attachResult);
            },
            detachExtension: async () => {
              mode = detachResult.mode;
              return {
                ...detachResult,
                mirrorConflicts: structuredClone(detachResult.mirrorConflicts || []),
              };
            },
          };
        },
      },
      serverSyncTransport: {
        createServerSyncTransport(options) {
          syncTransports.push(options);
          return {
            exchange: async (request) => {
              syncExchangeRequests.push(structuredClone(request));
              return {
                contract: "sync-gateway/2",
                cursor: request.cursor || 0,
                headCursor: request.cursor || 0,
                changes: [],
                ackedMutationIds: [],
                conflicts: [],
              };
            },
          };
        },
      },
      syncGateway: {
        createSyncGateway({ transport, deviceId }) {
          return {
            deviceId,
            push: (request) => transport.exchange({ ...request, direction: "push" }),
            pull: (request) => transport.exchange({ ...request, direction: "pull" }),
          };
        },
      },
      syncCoordinator: {
        CONTRACT: "sync-coordinator/1",
        createSyncCoordinator() {
          throw new Error("lifecycle harness sync runtime should own this stub");
        },
      },
      syncRuntime: {
        createSyncRuntime(options) {
          let paused = true;
          let pauseReason = "initial";
          const calls = [];
          const value = {
            contract: "sync-runtime/1",
            options,
            calls,
            start(reason) {
              paused = false;
              pauseReason = "";
              calls.push({ operation: "start", reason });
              lifecycleOrder.push(`sync-runtime:start:${reason}`);
              return true;
            },
            resume(reason) {
              paused = false;
              pauseReason = "";
              calls.push({ operation: "resume", reason });
              lifecycleOrder.push(`sync-runtime:resume:${reason}`);
              return true;
            },
            pause(reason) {
              paused = true;
              pauseReason = String(reason || "paused");
              calls.push({ operation: "pause", reason });
              lifecycleOrder.push(`sync-runtime:pause:${reason}`);
              return true;
            },
            destroy(reason) {
              paused = true;
              pauseReason = String(reason || "destroyed");
              calls.push({ operation: "destroy", reason });
              lifecycleOrder.push("sync-runtime:destroy");
              return true;
            },
            resolveConflict(reason) {
              calls.push({ operation: "resolveConflict", reason });
              if (!paused || pauseReason !== "sync-conflict") return false;
              paused = false;
              pauseReason = "sync-conflict-resolved";
              return true;
            },
            async runNow(reason) {
              calls.push({ operation: "runNow", reason });
              return { contract: "sync-runtime/1", server: { ok: true } };
            },
            status: async () => ({
              contract: "sync-runtime/1",
              paused,
              destroyed: false,
              running: false,
              pauseReason,
              lastResult: structuredClone(
                syncRuntimeStatus?.lastResult || {
                  server: { conflicts: [] },
                  direct: {},
                },
              ),
            }),
          };
          syncRuntimeCalls.push(value);
          return value;
        },
      },
    },
  };
  if (directSync) {
    sandbox.RTCPeerConnection = function FakeRTCPeerConnection() {};
    sandbox.navigator = {
      locks: {
        request: async (_name, _options, callback) => callback({ name: "direct-lock" }),
      },
    };
    sandbox.BWReaderRuntime.directSyncSignalTransport = {
      createDirectSignalTransport(options) {
        lifecycleOrder.push("direct-signal:create");
        return {
          contract: "direct-sync-signal-transport/1",
          options,
          exchange: async () => ({
            contract: "direct-signal/1",
            accountProof: `account-proof-v1-${"f".repeat(64)}`,
            headCursor: 0,
            baselineReady: true,
            peers: [],
            ackedSignalIds: [],
            signals: [],
          }),
        };
      },
    };
    sandbox.BWReaderRuntime.directSyncProtocol = {
      CONTRACT: "direct-sync/1",
      createChannelTransport() {
        return {};
      },
      createStoreRelay() {
        return {
          async exchange(request) {
            directRelayCalls.push(structuredClone(request));
            return { contract: "direct-sync/1", ok: true };
          },
        };
      },
    };
    sandbox.BWReaderRuntime.directSyncHost = {
      createDirectSyncHost(options) {
        lifecycleOrder.push("direct-host:create");
        const calls = [];
        const hostValue = {
          options,
          calls,
          start(reason) {
            calls.push({ operation: "start", reason });
            lifecycleOrder.push("direct-host:start");
            return true;
          },
          pause(reason) {
            calls.push({ operation: "pause", reason });
            lifecycleOrder.push(`direct-host:pause:${reason}`);
            return true;
          },
          destroy(reason) {
            calls.push({ operation: "destroy", reason });
            lifecycleOrder.push("direct-host:destroy");
            return true;
          },
          status() {
            return { contract: "direct-sync-host/1" };
          },
        };
        directHostCalls.push(hostValue);
        return hostValue;
      },
    };
    sandbox.BWReaderRuntime.directSyncLeader = {
      createDirectSyncLeader(options) {
        lifecycleOrder.push("direct-leader:create");
        const calls = [];
        let paused = true;
        const leaderValue = {
          options,
          calls,
          start(reason) {
            paused = false;
            calls.push({ operation: "start", reason });
            lifecycleOrder.push(`direct-leader:start:${reason}`);
            options.host.start(reason);
            return true;
          },
          resume(reason) {
            paused = false;
            calls.push({ operation: "resume", reason });
            lifecycleOrder.push(`direct-leader:resume:${reason}`);
            options.host.start(reason);
            return true;
          },
          pause(reason) {
            paused = true;
            calls.push({ operation: "pause", reason });
            lifecycleOrder.push(`direct-leader:pause:${reason}`);
            options.host.pause(reason);
            return true;
          },
          destroy(reason) {
            paused = true;
            calls.push({ operation: "destroy", reason });
            lifecycleOrder.push("direct-leader:destroy");
            options.host.destroy(reason);
            return true;
          },
          status() {
            return { contract: "direct-sync-leader/1", paused };
          },
        };
        directLeaderCalls.push(leaderValue);
        return leaderValue;
      },
    };
  }
  sandbox.fetch = async (url, init) => {
    if (/\/api\/reader\/sync\/owner\/(?:claim|renew|release)$/.test(url)) {
      const body = JSON.parse(init.body);
      ownerLeaseRequests.push({
        url,
        body: structuredClone(body),
        init: {
          method: init.method,
          credentials: init.credentials,
          cache: init.cache,
          keepalive: init.keepalive === true,
        },
      });
      if (url.endsWith("/release")) {
        if (ownerReleaseGate) await ownerReleaseGate;
        if (ownerReleaseFailure) {
          throw new TypeError("owner release failed");
        }
        return {
          ok: true,
          status: 200,
          async json() {
            return { ok: true, contract: "owner-lease/1" };
          },
        };
      }
      if (currentOwnerLeaseMode === "held") {
        return {
          ok: false,
          status: 409,
          async json() {
            return {
              ok: false,
              code: "BW_SYNC_OWNER_HELD",
              error: "device family is owned elsewhere",
            };
          },
        };
      }
      ownerGeneration += 1;
      return {
        ok: true,
        status: 200,
        async json() {
          return {
            ok: true,
            contract: "owner-lease/1",
            deviceId: body.deviceId,
            deviceFamilyId: body.deviceFamilyId,
            ownerRole: body.ownerRole,
            ownerInstanceId: body.ownerInstanceId,
            ownerGeneration: ownerGeneration,
            ownerToken:
              `owner-token-v1-${String(ownerGeneration).padStart(24, "x")}`,
            expiresAt: Math.floor(Date.now() / 1000) + ownerLeaseSeconds,
          };
        },
      };
    }
    if (url !== "/api/reader/provider-ticket" || !refreshTickets) {
      throw new TypeError(`unexpected fetch: ${url}`);
    }
    fetchCalls.push({ url, init: structuredClone({
      method: init.method,
      headers: init.headers,
      body: init.body,
      credentials: init.credentials,
      cache: init.cache,
    }) });
    if (refreshFailure) throw new TypeError("offline");
    const next = refreshTickets[Math.min(refreshIndex, refreshTickets.length - 1)];
    refreshIndex += 1;
    const expiresAt = Number(/^pvt-v2-([0-9]{10,12})-/.exec(next)?.[1] || 0);
    return {
      ok: true,
      async json() {
        return {
          ok: true,
          storage_namespace: namespace,
          ticket: next,
          expires_at: expiresAt,
          expires_in: Math.max(1, expiresAt - Math.floor(Date.now() / 1000)),
        };
      },
    };
  };
  const context = vm.createContext(sandbox);
  const root = vm.runInContext("globalThis", context);
  vm.runInContext(ACCOUNT_CONTEXT_SOURCE, context, {
    filename: "account-context.js",
  });
  vm.runInContext(DATA_REGISTRY_SOURCE, context, {
    filename: "data-registry.js",
  });
  vm.runInContext(PREFERENCE_STORE_SOURCE, context, {
    filename: "preference-store.js",
  });
  vm.runInContext(SYNC_CONFLICT_CONTROL_SOURCE, context, {
    filename: "sync-conflict-control.js",
  });
  vm.runInContext(SYNC_OWNER_LEASE_SOURCE, context, {
    filename: "sync-owner-lease.js",
  });
  const realCreateOwnerLease =
    root.BWReaderRuntime.syncOwnerLease.createSyncOwnerLease;
  root.BWReaderRuntime.syncOwnerLease.createSyncOwnerLease = (options) => {
    ownerLeaseOptions.push(options);
    const manager = realCreateOwnerLease(options);
    ownerLeaseManagers.push(manager);
    return manager;
  };
  vm.runInContext(SOURCE, context, { filename: "pwa-runtime.js" });

  const dispatch = (type, detail = {}) => {
    document.dispatchEvent(new CustomEvent(type, { detail }));
  };
  const waitForEvent = async (type, count = 1) => {
    for (let turn = 0; turn < 50; turn += 1) {
      const matches = events.filter((event) => event.type === type);
      if (matches.length >= count) return matches.at(count - 1);
      await new Promise((resolve) => setImmediate(resolve));
    }
    throw new Error(`没有等到事件 ${type}`);
  };
  const waitFor = async (predicate, label = "condition") => {
    for (let turn = 0; turn < 50; turn += 1) {
      if (predicate()) return true;
      await new Promise((resolve) => setImmediate(resolve));
    }
    throw new Error(`没有等到 ${label}`);
  };
  return {
    root,
    api: root.BWReaderRuntime.pwaRuntime,
    accountContext: root.BWReaderRuntime.accountContext,
    document,
    events,
    stores,
    storeOptions,
    bridgeStarts,
    bridgeRestarts,
    attachCalls,
    fetchCalls,
    syncTransports,
    syncExchangeRequests,
    syncRuntimeCalls,
    directHostCalls,
    directLeaderCalls,
    directRelayCalls,
    ownerLeaseRequests,
    ownerLeaseOptions,
    ownerLeaseManagers,
    ownerLeaseTimers,
    lifecycleOrder,
    setOwnerLeaseMode(value) {
      currentOwnerLeaseMode = value;
    },
    async runNextOwnerLeaseTimer(predicate = () => true) {
      const timer = ownerLeaseTimers.find(
        (entry) => entry.active && predicate(entry),
      );
      if (!timer) return false;
      timer.active = false;
      timer.callback();
      for (let turn = 0; turn < 10; turn += 1) {
        await new Promise((resolve) => setImmediate(resolve));
      }
      return true;
    },
    dispatchRoot(type, detail = {}) {
      const event = { type, ...detail };
      for (const listener of rootListeners.get(type) || []) listener(event);
    },
    setHost() {
      host = {
        contract: "document-host/1",
        kind: "pdf",
        documentId: "doc-lifecycle",
        audit: () => ({ valid: true }),
      };
    },
    dispatch,
    waitForEvent,
    waitFor,
  };
}

test("PWA 只把服务器签发的 provider ticket 交给扩展握手", async () => {
  const h = harness();
  h.setHost();
  h.dispatch("bw:document-host-ready");
  await h.waitForEvent("bw:reader-runtime-ready");
  assert.deepEqual(h.bridgeStarts, [expectedBridgeStart(h, TICKET)]);
  assert.equal(
    h.ownerLeaseRequests.length,
    0,
    "document_start 扩展标记存在时 PWA 绝不能向服务器 claim owner",
  );
  assert.equal(
    h.bridgeStarts[0].helloPayload.syncOwnerClaim.deviceFamilyId,
    h.api.installId(),
  );
  assert.equal(h.accountContext.snapshot().source, "server-session");
  assert.equal(h.accountContext.snapshot().namespace, NAMESPACE);
  assert.equal(h.fetchCalls.length, 0, "新鲜页面票据不应增加一次启动 RTT");
  assert.equal(
    h.syncRuntimeCalls[0].calls.some((call) => call.operation === "resume"),
    false,
    "document_start 扩展标记出现后，握手未完成也必须保留扩展同步所有权",
  );
  assert.deepEqual(
    h.syncRuntimeCalls[0].calls.at(-1),
    { operation: "pause", reason: "extension-owner-claim" },
    "PWA 必须先同步暂停自己的 owner，再把 claim 交给 bridge",
  );

  const missing = harness({ ticket: "" });
  missing.setHost();
  missing.dispatch("bw:document-host-ready");
  await missing.waitForEvent("bw:reader-runtime-ready");
  assert.deepEqual(missing.bridgeStarts, []);
  assert.equal(
    missing.events.some(
      (event) =>
        event.type === "bw:reader-runtime-provider-error" &&
        event.detail.code === "BW_PROVIDER_AUTH_EXPIRED",
    ),
    true,
  );
});

test("PWA 不为缺失或伪造的 server-injected namespace 创建本地 store", async () => {
  for (const namespace of ["", "acct-v1-short", `acct-v1-${"A".repeat(64)}`]) {
    const h = harness({ namespace, marker: false });
    h.setHost();
    h.dispatch("bw:document-host-ready");
    const failed = await h.waitForEvent("bw:reader-runtime-error");
    assert.equal(failed.detail.code, "BW_ACCOUNT_NAMESPACE", namespace);
    assert.equal(h.stores.length, 0, namespace);
    assert.equal(h.accountContext.snapshot().active, false, namespace);
    assert.equal(
      h.events.some((event) => event.type === "bw:reader-runtime-ready"),
      false,
      namespace,
    );
  }
});

test("无扩展标记时纯 PWA 不刷新票据也不启动 provider", async () => {
  const h = harness({
    marker: false,
    refreshTickets: [REFRESHED_TICKET],
  });
  h.setHost();
  h.dispatch("bw:document-host-ready");
  await h.waitForEvent("bw:reader-runtime-ready");
  await h.waitFor(
    () => h.ownerLeaseRequests.length === 1 &&
      h.syncRuntimeCalls[0].calls.some(
        (call) =>
          call.operation === "resume" &&
          call.reason === "pwa-owner-lease-acquired",
      ),
    "PWA owner lease claim and resume",
  );
  assert.equal(h.fetchCalls.length, 0);
  assert.deepEqual(h.bridgeStarts, []);
  assert.equal(h.syncTransports.length, 1);
  assert.equal(h.syncTransports[0].origin, h.root.location.origin);
  assert.equal(h.syncTransports[0].ownerNamespace, NAMESPACE);
  assert.equal(h.syncTransports[0].credentials, "same-origin");
  assert.equal(h.syncRuntimeCalls.length, 1);
  assert.equal(h.ownerLeaseRequests.length, 1);
  assert.match(h.ownerLeaseRequests[0].url, /\/owner\/claim$/);
  assert.equal(
    h.ownerLeaseRequests[0].body.deviceFamilyId,
    h.api.installId(),
  );
  assert.equal(h.ownerLeaseRequests[0].body.ownerRole, "pwa");
  assert.match(
    h.ownerLeaseRequests[0].body.ownerInstanceId,
    /^owner-instance-v1:pwa:[a-f0-9]{32}$/,
  );
  assert.strictEqual(
    h.syncTransports[0].ownerLease,
    h.ownerLeaseManagers[0],
  );
  assert.deepEqual(
    h.syncRuntimeCalls[0].calls.at(-1),
    { operation: "resume", reason: "pwa-owner-lease-acquired" },
  );
});

test("owner lease 被占用或续租丢失时保持本地 runtime ready、暂停网络 owner 并自动重试", async () => {
  const held = harness({
    marker: false,
    directSync: true,
    ownerLeaseMode: "held",
    ownerLeaseSeconds: 2,
  });
  held.setHost();
  held.dispatch("bw:document-host-ready");
  await held.waitForEvent("bw:reader-runtime-ready");
  await held.waitFor(
    () => held.ownerLeaseRequests.length === 1,
    "initial held owner claim",
  );
  assert.equal(
    held.syncRuntimeCalls[0].calls.some(
      (call) => call.operation === "resume" || call.operation === "start",
    ),
    false,
  );
  assert.equal(
    held.directLeaderCalls[0].calls.some(
      (call) => call.operation === "resume" || call.operation === "start",
    ),
    false,
  );
  assert.equal(
    held.events.some(
      (event) =>
        event.type === "bw:reader-sync-owner-status" &&
        event.detail.state === "waiting" &&
        event.detail.code === "BW_SYNC_OWNER_HELD",
    ),
    true,
  );
  assert.equal(
    held.ownerLeaseTimers.some((timer) => timer.active),
    true,
    "首次 claim 被占用后 manager 必须保留自动重试",
  );

  held.setOwnerLeaseMode("acquire");
  await held.runNextOwnerLeaseTimer();
  await held.waitFor(
    () => held.syncRuntimeCalls[0].calls.some(
      (call) =>
        call.operation === "resume" &&
        call.reason === "pwa-owner-lease-acquired",
    ),
    "owner retry acquisition",
  );
  assert.equal(
    held.directLeaderCalls[0].calls.at(-1).operation,
    "resume",
  );

  held.setOwnerLeaseMode("held");
  await held.runNextOwnerLeaseTimer();
  await held.waitFor(
    () => held.syncRuntimeCalls[0].calls.some(
      (call) =>
        call.operation === "pause" &&
        call.reason === "pwa-owner-lease-lost",
    ),
    "owner lease loss pause",
  );
  assert.deepEqual(
    held.directLeaderCalls[0].calls.at(-1),
    { operation: "pause", reason: "pwa-owner-lease-lost" },
  );
  assert.equal(
    held.ownerLeaseTimers.some((timer) => timer.active),
    true,
    "续租丢失后 manager 必须继续自动重试",
  );

  await assert.rejects(
    held.directHostCalls[0].options.relay.exchange({
      contract: "direct-sync/1",
      operation: "store-request",
    }),
    (error) => error && error.code === "BW_SYNC_OWNER_HELD",
  );
  assert.equal(
    held.directRelayCalls.length,
    0,
    "已开通道失去 family lease 后 STORE_REQUEST 不得到达本地 store relay",
  );
});

test("PWA 只公开白名单 syncControl，不暴露原始 SyncRuntime", async () => {
  const namespaceSecret = `acct-v1-${"9".repeat(64)}`;
  const tokenSecret = `sk-${"7".repeat(32)}`;
  const h = harness({
    marker: false,
    syncRuntimeStatus: {
      lastResult: {
        server: {
          conflicts: [{
            collection: "user-settings",
            id: "theme",
            reason: "same-rev-different-value",
            incoming: {
              collection: "user-settings",
              id: "theme",
              rev: 2,
              value: { namespaceSecret, tokenSecret },
            },
            local: {
              collection: "user-settings",
              id: "theme",
              rev: 1,
              value: { private: "raw-local-record" },
            },
            upstreamMessage: "private upstream exception",
          }],
        },
        direct: {
          "private-peer-session": {
            conflicts: [],
          },
        },
      },
    },
  });
  h.setHost();
  h.dispatch("bw:document-host-ready");
  await h.waitForEvent("bw:reader-runtime-ready");

  assert.equal("syncRuntime" in h.api, false);
  assert.equal(typeof h.api.syncControl, "function");
  const control = h.api.syncControl();
  assert.equal(control.contract, "sync-conflict-control/1");
  const status = await control.status();
  assert.deepEqual(
    Object.keys(status).sort(),
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
  assert.equal(status.owner, "pwa");
  assert.equal(status.conflictCount, 1);
  assert.deepEqual(
    Object.keys(status.conflicts[0]).sort(),
    [
      "collection",
      "currentRev",
      "id",
      "incomingRev",
      "lane",
      "reason",
    ],
  );
  const serialized = JSON.stringify(status);
  for (const secret of [
    namespaceSecret,
    tokenSecret,
    "raw-local-record",
    "private upstream exception",
    "private-peer-session",
  ]) {
    assert.equal(serialized.includes(secret), false, secret);
  }
});

test("扩展 provider 接管时 syncControl 代理扩展，断开后保持只读保留状态", async () => {
  const h = harness();
  h.setHost();
  h.dispatch("bw:document-host-ready");
  await h.waitForEvent("bw:reader-runtime-ready");
  const providerControl = Object.freeze({
    contract: "sync-conflict-control/1",
    status: async () => ({
      contract: "sync-conflict-control/1",
      owner: "extension-background",
      state: "ready",
      at: 1,
      conflictCount: 0,
      truncated: false,
      conflicts: [],
    }),
  });
  const provider = {
    contract: "extension-provider-test",
    syncControl: providerControl,
  };

  h.dispatch("bw:extension-provider-ready", { provider });
  await h.waitForEvent("bw:reader-runtime-provider-attached");
  assert.strictEqual(h.api.syncControl(), providerControl);

  h.dispatch("bw:extension-provider-disconnected", { reason: "port-closed" });
  await h.waitForEvent("bw:reader-runtime-provider-detached");
  assert.notStrictEqual(h.api.syncControl(), providerControl);
  await assert.rejects(
    h.api.syncControl().status(),
    (error) => error && error.code === "BW_SYNC_OWNER_RESERVED",
  );
});

test("PWA 同步状态与错误事件只发布安全摘要，不携带 raw result/error", async () => {
  const resultSecret = `sk-${"5".repeat(32)}`;
  const namespaceSecret = `acct-v1-${"4".repeat(64)}`;
  const errorSecret = "upstream database body must stay private";
  const h = harness({ marker: false });
  h.setHost();
  h.dispatch("bw:document-host-ready");
  await h.waitForEvent("bw:reader-runtime-ready");
  const onResult = h.syncRuntimeCalls[0].options.onResult;

  const legacyResultCount = h.events.filter(
    (event) => event.type === "bw:reader-sync-result",
  ).length;
  const statusCount = h.events.filter(
    (event) => event.type === "bw:reader-sync-status",
  ).length;
  onResult({
    contract: "sync-coordinator/1",
    server: {
      ok: true,
      conflicts: [],
      rawResponse: {
        token: resultSecret,
        namespace: namespaceSecret,
      },
    },
    direct: {
      "private-peer-session": {
        rawRecords: [{ value: "private record value" }],
      },
    },
  }, null, "manual");
  const statusEvent = await h.waitForEvent(
    "bw:reader-sync-status",
    statusCount + 1,
  );
  assert.equal(
    h.events.filter(
      (event) => event.type === "bw:reader-sync-result",
    ).length,
    legacyResultCount,
    "不得继续发布携带 coordinator raw result 的旧事件",
  );
  assert.equal(
    Object.prototype.hasOwnProperty.call(statusEvent.detail, "result"),
    false,
  );

  const errorCount = h.events.filter(
    (event) => event.type === "bw:reader-sync-error",
  ).length;
  const failure = new Error(errorSecret);
  failure.code = "BW_SYNC_RETRYABLE";
  onResult(null, failure, "manual");
  const errorEvent = await h.waitForEvent(
    "bw:reader-sync-error",
    errorCount + 1,
  );
  assert.equal(
    Object.prototype.hasOwnProperty.call(errorEvent.detail, "error"),
    false,
  );

  const serialized = JSON.stringify([
    statusEvent.detail,
    errorEvent.detail,
  ]);
  for (const secret of [
    resultSecret,
    namespaceSecret,
    errorSecret,
    "private-peer-session",
    "private record value",
  ]) {
    assert.equal(serialized.includes(secret), false, secret);
  }
});

test("PWA checkpoint 只允许更高对账 epoch 授权游标回退，并拒绝旧标签反写", async () => {
  const h = harness({ marker: false });
  h.setHost();
  h.dispatch("bw:document-host-ready");
  await h.waitForEvent("bw:reader-runtime-ready");
  const checkpointStore = h.syncRuntimeCalls[0].options.checkpointStore;
  const base = {
    contract: "sync-coordinator/1",
    schema: 1,
    registryDigest: h.root.BWReaderRuntime.dataRegistry.syncDigest(),
    generation: 5,
    server: {
      localCursor: 80,
      remoteCursor: 90,
      reconciliationEpoch: 0,
    },
    peers: {},
  };
  await checkpointStore.save(base);
  await checkpointStore.save({
    ...base,
    generation: 6,
    server: {
      localCursor: 8,
      remoteCursor: 9,
      reconciliationEpoch: 1,
    },
  });
  assert.deepEqual(
    structuredClone((await checkpointStore.load()).server),
    { localCursor: 8, remoteCursor: 9, reconciliationEpoch: 1 },
  );

  await checkpointStore.save({
    ...base,
    generation: 99,
    server: {
      localCursor: 800,
      remoteCursor: 900,
      reconciliationEpoch: 0,
    },
  });
  assert.deepEqual(
    structuredClone((await checkpointStore.load()).server),
    { localCursor: 8, remoteCursor: 9, reconciliationEpoch: 1 },
  );
});

test("PWA checkpoint 绑定 global Vault epoch，数据 DB 重建后从远端 cursor 0 恢复", async () => {
  const h = harness({ marker: false });
  h.setHost();
  h.dispatch("bw:document-host-ready");
  await h.waitForEvent("bw:reader-runtime-ready");
  const checkpointStore = h.syncRuntimeCalls[0].options.checkpointStore;
  const digest = h.root.BWReaderRuntime.dataRegistry.syncDigest();
  await checkpointStore.save({
    contract: "sync-coordinator/1",
    schema: 1,
    registryDigest: digest,
    generation: 1,
    server: {
      localCursor: 0,
      remoteCursor: 17,
      reconciliationEpoch: 0,
    },
    peers: {},
  });
  const deviceCheckpoint = await h.stores[2].get(
    "ui-session",
    "reader-sync-checkpoint-v1",
    { includeDeleted: true },
  );
  assert.equal(deviceCheckpoint.value.contract, "pwa-sync-checkpoint/2");
  assert.equal(deviceCheckpoint.value.schema, 2);
  assert.equal(
    deviceCheckpoint.value.vaultEpoch,
    await h.stores[0].instanceEpoch(),
  );
  assert.equal(deviceCheckpoint.value.checkpoint.server.remoteCursor, 17);

  h.stores[0].rebuildInstance();
  const recovered = await checkpointStore.load();
  assert.equal(recovered, null);
  const firstRemoteCursor = Number(recovered?.server?.remoteCursor || 0);
  await h.syncRuntimeCalls[0].options.serverGateway.pull({
    cursor: firstRemoteCursor,
    limit: 200,
  });
  assert.equal(h.syncExchangeRequests.at(-1).direction, "pull");
  assert.equal(h.syncExchangeRequests.at(-1).cursor, 0);
});

test("PWA checkpoint 忽略未绑定 epoch 的 v1 数据，并对损坏的 v2 envelope fail closed", async () => {
  const h = harness({ marker: false });
  h.setHost();
  h.dispatch("bw:document-host-ready");
  await h.waitForEvent("bw:reader-runtime-ready");
  const checkpointStore = h.syncRuntimeCalls[0].options.checkpointStore;
  const checkpoint = {
    contract: "sync-coordinator/1",
    schema: 1,
    registryDigest: h.root.BWReaderRuntime.dataRegistry.syncDigest(),
    generation: 1,
    server: {
      localCursor: 0,
      remoteCursor: 13,
      reconciliationEpoch: 0,
    },
    peers: {},
  };
  await h.stores[2].put("ui-session", {
    id: "reader-sync-checkpoint-v1",
    schema: 1,
    checkpoint,
  }, {
    id: "reader-sync-checkpoint-v1",
    mutationId: "legacy-checkpoint",
  });
  assert.equal(await checkpointStore.load(), null);

  await h.stores[2].put("ui-session", {
    id: "reader-sync-checkpoint-v1",
    contract: "pwa-sync-checkpoint/2",
    schema: 2,
    vaultEpoch: await h.stores[0].instanceEpoch(),
    checkpoint: null,
  }, {
    id: "reader-sync-checkpoint-v1",
    mutationId: "corrupt-checkpoint",
  });
  await assert.rejects(
    checkpointStore.load(),
    (error) =>
      error?.code === "BW_SYNC_CHECKPOINT" &&
      error.retryable === false,
  );
});

test("PWA 运行中 Vault epoch 改变时拒绝把旧 checkpoint 写入新实例", async () => {
  const h = harness({ marker: false });
  h.setHost();
  h.dispatch("bw:document-host-ready");
  await h.waitForEvent("bw:reader-runtime-ready");
  const checkpointStore = h.syncRuntimeCalls[0].options.checkpointStore;
  const checkpoint = {
    contract: "sync-coordinator/1",
    schema: 1,
    registryDigest: h.root.BWReaderRuntime.dataRegistry.syncDigest(),
    generation: 1,
    server: {
      localCursor: 0,
      remoteCursor: 23,
      reconciliationEpoch: 0,
    },
    peers: {},
  };
  assert.equal(await checkpointStore.load(), null);
  h.stores[0].rebuildInstance();
  await assert.rejects(
    checkpointStore.save(checkpoint),
    (error) =>
      error?.code === "BW_SYNC_CHECKPOINT_EPOCH" &&
      error.retryable === true,
  );
  assert.equal(
    await checkpointStore.load(),
    null,
    "可重试的下一轮必须按新 epoch 丢弃旧游标",
  );
  await checkpointStore.save({
    ...checkpoint,
    generation: 2,
    server: {
      localCursor: 0,
      remoteCursor: 0,
      reconciliationEpoch: 1,
    },
  });
  const saved = await h.stores[2].get(
    "ui-session",
    "reader-sync-checkpoint-v1",
    { includeDeleted: true },
  );
  assert.equal(saved.value.vaultEpoch, await h.stores[0].instanceEpoch());
  assert.equal(saved.value.checkpoint.server.remoteCursor, 0);
});

test("PreferenceStore 的 IndexedDB 水合不阻塞阅读器 runtime-ready", async () => {
  let releasePreferences;
  const preferenceGetGate = new Promise((resolve) => {
    releasePreferences = resolve;
  });
  const h = harness({
    marker: false,
    preferenceGetGate,
  });
  h.setHost();
  h.dispatch("bw:document-host-ready");
  await h.waitForEvent("bw:reader-runtime-ready");
  assert.equal(
    h.events.some((event) => event.type === "bw:reader-preference-ready"),
    false,
  );
  releasePreferences();
  await h.waitForEvent("bw:reader-preference-ready");
});

test("扩展 provider 接管必须等待 PreferenceStore 水合完成", async () => {
  let releasePreferences;
  const preferenceGetGate = new Promise((resolve) => {
    releasePreferences = resolve;
  });
  const h = harness({
    marker: false,
    preferenceGetGate,
  });
  h.setHost();
  h.dispatch("bw:document-host-ready");
  await h.waitForEvent("bw:reader-runtime-ready");

  h.dispatch("bw:extension-provider-ready", {
    provider: { contract: "extension-provider-test" },
  });
  assert.deepEqual(
    h.syncRuntimeCalls[0].calls.at(-1),
    { operation: "pause", reason: "extension-provider-attaching" },
  );
  for (let turn = 0; turn < 5; turn += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.equal(h.attachCalls.length, 0);

  releasePreferences();
  for (let turn = 0; turn < 80 && h.attachCalls.length < 1; turn += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.equal(h.attachCalls.length, 1);
  await h.waitForEvent("bw:reader-runtime-provider-attached");
  assert.deepEqual(
    h.syncRuntimeCalls[0].calls.at(-1),
    { operation: "pause", reason: "extension-provider-attaching" },
  );
});

test("provider clean detach 不会恢复第二个 PWA 持久同步 owner", async () => {
  const h = harness();
  h.setHost();
  h.dispatch("bw:document-host-ready");
  await h.waitForEvent("bw:reader-runtime-ready");
  h.dispatch("bw:extension-provider-ready", {
    provider: { contract: "extension-provider-test" },
  });
  await h.waitForEvent("bw:reader-runtime-provider-attached");
  assert.equal(h.syncRuntimeCalls[0].calls.at(-1).operation, "pause");

  h.dispatch("bw:extension-provider-disconnected", { reason: "port-closed" });
  await h.waitForEvent("bw:reader-runtime-provider-detached");
  assert.equal(
    h.syncRuntimeCalls[0].calls.some(
      (call) =>
        call.operation === "resume" &&
        call.reason === "extension-provider-detached",
    ),
    false,
  );
});

test("页面票据缺失或临近过期时在线刷新；离线只回退仍未过期的页面票据", async () => {
  const missing = harness({
    ticket: "",
    refreshTickets: [REFRESHED_TICKET],
  });
  missing.setHost();
  missing.dispatch("bw:document-host-ready");
  await missing.waitForEvent("bw:reader-runtime-ready");
  assert.equal(missing.fetchCalls.length, 1);
  assert.equal(missing.fetchCalls[0].url, "/api/reader/provider-ticket");
  assert.equal(missing.fetchCalls[0].init.method, "POST");
  assert.equal(missing.fetchCalls[0].init.headers["X-BW-Reader-Provider"], "1");
  assert.deepEqual(
    missing.bridgeStarts,
    [expectedBridgeStart(missing, REFRESHED_TICKET)],
  );

  const nearExpiryAt = Math.floor(Date.now() / 1000) + 10;
  const nearExpiryTicket =
    `pvt-v2-${nearExpiryAt}-${"1".repeat(32)}-${"2".repeat(64)}`;
  const offline = harness({
    ticket: nearExpiryTicket,
    refreshTickets: [REFRESHED_TICKET],
    refreshFailure: true,
  });
  offline.setHost();
  offline.dispatch("bw:document-host-ready");
  await offline.waitForEvent("bw:reader-runtime-ready");
  assert.equal(offline.fetchCalls.length, 1);
  assert.deepEqual(
    offline.bridgeStarts,
    [expectedBridgeStart(offline, nearExpiryTicket)],
  );

  const expiredAt = Math.floor(Date.now() / 1000) - 1;
  const expired = harness({
    ticket: `pvt-v2-${expiredAt}-${"3".repeat(32)}-${"4".repeat(64)}`,
    refreshTickets: [REFRESHED_TICKET],
    refreshFailure: true,
  });
  expired.setHost();
  expired.dispatch("bw:document-host-ready");
  await expired.waitForEvent("bw:reader-runtime-ready");
  assert.equal(expired.fetchCalls.length, 1);
  assert.deepEqual(expired.bridgeStarts, []);
  assert.equal(
    expired.events.some(
      (event) =>
        event.type === "bw:reader-runtime-provider-error" &&
        event.detail.code === "BW_PROVIDER_AUTH_EXPIRED",
    ),
    true,
  );
});

test("AUTH 过期只触发一次显式刷新与 restart，READY 后才解除门禁", async () => {
  const h = harness({ refreshTickets: [REFRESHED_TICKET] });
  h.setHost();
  h.dispatch("bw:document-host-ready");
  await h.waitForEvent("bw:reader-runtime-ready");

  h.dispatch("bw:extension-provider-error", {
    code: "BW_PROVIDER_AUTH_EXPIRED",
    error: "expired",
  });
  for (let turn = 0; turn < 20 && h.bridgeRestarts.length < 1; turn += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.equal(h.fetchCalls.length, 1);
  assert.deepEqual(h.bridgeRestarts, [{
    namespace: NAMESPACE,
    ticket: REFRESHED_TICKET,
  }]);

  h.dispatch("bw:extension-provider-error", {
    code: "BW_PROVIDER_AUTH_EXPIRED",
    error: "still rejected",
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(h.fetchCalls.length, 1);
  assert.equal(h.bridgeRestarts.length, 1);
});

test("PWA 安装编号独立于租户 namespace，且跨启动持久复用", async () => {
  const storage = new Map();
  const first = harness({ storage });
  first.setHost();
  first.dispatch("bw:document-host-ready");
  const ready = await first.waitForEvent("bw:reader-runtime-ready");

  const installId = first.api.installId();
  assert.match(installId, /^pwa-install-v1-[a-f0-9]{32}$/);
  assert.equal(first.api.deviceId(), installId);
  assert.equal(ready.detail.installId, installId);
  assert.equal(first.storeOptions.length, 3);
  assert.equal(first.storeOptions.every((options) => options.deviceId === installId), true);
  assert.equal(first.storeOptions.some((options) => options.deviceId.includes(NAMESPACE)), false);
  assert.equal(first.storeOptions.every((options) => options.dbName.includes(NAMESPACE)), true);

  const second = harness({ storage });
  second.setHost();
  second.dispatch("bw:document-host-ready");
  await second.waitForEvent("bw:reader-runtime-ready");
  assert.equal(second.api.installId(), installId);
  assert.equal(second.storeOptions.every((options) => options.deviceId === installId), true);
});

test("启动失败完整撤销发布状态、关闭 store，并允许下一次重新启动", async () => {
  const h = harness({ failStarts: 1 });
  h.setHost();
  h.dispatch("bw:document-host-ready");
  await h.waitForEvent("bw:reader-runtime-error");

  assert.equal(h.api.runtime(), null);
  assert.equal(h.api.localStores(), null);
  assert.equal(h.api.namespace(), "");
  assert.equal(h.accountContext.snapshot().active, false);
  assert.equal(h.accountContext.snapshot().reason, "pwa-runtime-boot-failed");
  assert.equal("__BW_READER_RUNTIME__" in h.root, false);
  assert.equal("bwReaderUiOwner" in h.document.documentElement.dataset, false);
  assert.equal(h.stores.length, 3);
  assert.equal(h.stores.every((store) => store.closed), true);

  h.dispatch("bw:document-host-ready");
  await h.waitForEvent("bw:reader-runtime-ready");
  assert.notEqual(h.api.runtime(), null);
  assert.equal(h.storeOptions.length, 6);
  assert.equal(h.stores.slice(3).every((store) => !store.closed), true);
});

test("启动尚未完成时账户变化，旧 boot 结果失效且不撤销后来账户", async () => {
  let releaseStart;
  const startGate = new Promise((resolve) => { releaseStart = resolve; });
  const h = harness({ startGate });
  h.setHost();
  h.dispatch("bw:document-host-ready");

  for (let turn = 0; turn < 30 && h.stores.length < 3; turn += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  const oldLease = h.accountContext.lease();
  h.accountContext.activate({
    namespace: `acct-v1-${"d".repeat(64)}`,
    source: "server-session",
  });
  assert.equal(h.accountContext.isCurrent(oldLease), false);
  releaseStart();

  const failed = await h.waitForEvent("bw:reader-runtime-error");
  assert.equal(failed.detail.code, "BW_ACCOUNT_CONTEXT_STALE");
  assert.equal(
    h.events.some((event) => event.type === "bw:reader-runtime-ready"),
    false,
  );
  assert.equal(h.stores.every((store) => store.closed), true);
  assert.equal(h.accountContext.snapshot().active, true);
  assert.equal(
    h.accountContext.snapshot().namespace,
    `acct-v1-${"d".repeat(64)}`,
  );
});

test("provider attach 在途切换账户后不得发布旧连接结果", async () => {
  let releaseAttach;
  const attachGate = new Promise((resolve) => { releaseAttach = resolve; });
  const h = harness({ attachGate });
  h.setHost();
  h.dispatch("bw:document-host-ready");
  await h.waitForEvent("bw:reader-runtime-ready");

  h.dispatch("bw:extension-provider-ready", {
    provider: { contract: "extension-provider-test" },
  });
  for (let turn = 0; turn < 30 && h.attachCalls.length < 1; turn += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.equal(h.attachCalls.length, 1);

  h.accountContext.activate({
    namespace: `acct-v1-${"e".repeat(64)}`,
    source: "server-session",
  });
  releaseAttach();
  for (let turn = 0; turn < 5; turn += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }

  assert.equal(
    h.events.some((event) => event.type === "bw:reader-runtime-provider-attached"),
    false,
  );
  assert.equal(
    h.events.some((event) => event.type === "bw:reader-runtime-provider-error"),
    false,
  );
  assert.equal(
    h.accountContext.snapshot().namespace,
    `acct-v1-${"e".repeat(64)}`,
  );
});

test("provider attach 在途断开按 lifecycle generation 串行失效，不发布旧连接", async () => {
  let releaseAttach;
  const attachGate = new Promise((resolve) => { releaseAttach = resolve; });
  const h = harness({ attachGate });
  h.setHost();
  h.dispatch("bw:document-host-ready");
  await h.waitForEvent("bw:reader-runtime-ready");

  h.dispatch("bw:extension-provider-ready", {
    provider: { contract: "extension-provider-test" },
  });
  for (let turn = 0; turn < 30 && h.attachCalls.length < 1; turn += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.equal(h.attachCalls.length, 1);

  h.dispatch("bw:extension-provider-disconnected", {
    reason: "port-closed-during-attach",
  });
  releaseAttach();
  await h.waitForEvent("bw:reader-runtime-provider-detached");

  assert.equal(
    h.events.some((event) => event.type === "bw:reader-runtime-provider-attached"),
    false,
  );
  assert.equal(
    h.syncRuntimeCalls[0].calls.some(
      (call) =>
        call.operation === "resume" &&
        call.reason === "extension-provider-detached",
    ),
    false,
  );
});

test("同源切换账户时先保全前一 owner，并把裸镜像切到当前账户", async () => {
  const storage = new Map([["ep-side-width", "432"]]);
  const first = harness({ storage });
  first.setHost();
  first.dispatch("bw:document-host-ready");
  await first.waitForEvent("bw:reader-runtime-ready");

  const anotherNamespace = `acct-v1-${"c".repeat(64)}`;
  const second = harness({ storage, namespace: anotherNamespace });
  second.setHost();
  second.dispatch("bw:document-host-ready");
  await second.waitForEvent("bw:reader-runtime-ready");
  assert.equal(
    storage.get("bw.reader.legacy-settings-owner.v1"),
    anotherNamespace,
  );
  const firstMirror = JSON.parse(
    storage.get(`bw.reader.preference-mirror.v1:${NAMESPACE}`),
  );
  assert.equal(
    firstMirror.values["ep-side-width"],
    "432",
  );
  assert.equal(storage.has("ep-side-width"), false);
  assert.equal(second.api.preferences().namespace(), anotherNamespace);
});

test("provider 断开后若镜像有未裁决冲突，页面保持 pwa-fallback-conflict", async () => {
  const mirrorConflict = {
    source: "provider-change",
    collection: "user-settings",
    id: "setting:theme",
    details: { reason: "same-rev-different-value" },
  };
  const h = harness({
    detachResult: {
      connected: false,
      mode: "pwa-fallback-conflict",
      mirrorConflicts: [mirrorConflict],
    },
  });
  h.setHost();
  h.dispatch("bw:document-host-ready");
  await h.waitForEvent("bw:reader-runtime-ready");

  h.dispatch("bw:extension-provider-disconnected", { reason: "port-closed" });
  const detached = await h.waitForEvent("bw:reader-runtime-provider-detached");
  assert.equal(h.document.documentElement.dataset.bwReaderRuntime, "pwa-fallback-conflict");
  assert.deepEqual(detached.detail.mirrorConflicts, [mirrorConflict]);
  assert.equal(
    h.syncRuntimeCalls[0].calls.some(
      (call) => call.reason === "extension-provider-detached",
    ),
    false,
  );
});

test("具备 WebRTC、Web Locks 与直连模块时，PWA runtime 就绪后启动直连宿主", async () => {
  const h = harness({ marker: false, directSync: true });
  h.setHost();
  h.dispatch("bw:document-host-ready");
  await h.waitForEvent("bw:reader-runtime-ready");
  await h.waitFor(
    () => h.ownerLeaseRequests.length === 1 &&
      h.directLeaderCalls[0]?.calls.some(
        (call) =>
          call.operation === "resume" &&
          call.reason === "pwa-owner-lease-acquired",
      ),
    "direct owner lease acquisition",
  );

  assert.equal(h.directHostCalls.length, 1);
  assert.equal(h.directLeaderCalls.length, 1);
  const expectedDigest = h.root.BWReaderRuntime.dataRegistry.syncDigest();
  assert.match(
    expectedDigest,
    /^sync-v3:record-parent-state\/1\|/,
  );
  assert.equal(
    h.directHostCalls[0].options.registryDigest,
    expectedDigest,
  );
  assert.strictEqual(
    h.syncTransports[0].ownerLease,
    h.ownerLeaseManagers[0],
  );
  assert.strictEqual(
    h.directHostCalls[0].options.signalTransport.options.ownerLease,
    h.ownerLeaseManagers[0],
  );
  assert.equal(
    h.ownerLeaseRequests[0].body.deviceFamilyId,
    h.api.installId(),
    "持久同步与 HTTP 直连信令必须共用同一 PWA device family lease",
  );
  assert.deepEqual(
    h.directLeaderCalls[0].calls.at(-1),
    { operation: "resume", reason: "pwa-owner-lease-acquired" },
  );
  assert.deepEqual(
    h.directHostCalls[0].calls.at(-1),
    { operation: "start", reason: "pwa-owner-lease-acquired" },
  );
  assert.ok(
    h.lifecycleOrder.indexOf("runtime:started") <
      h.lifecycleOrder.indexOf("direct-leader:resume:pwa-owner-lease-acquired"),
    "直连选举不得早于 ReaderRuntime.start() 完成",
  );
  assert.ok(
    h.lifecycleOrder.indexOf("runtime:started") <
      h.lifecycleOrder.indexOf("event:bw:reader-runtime-ready"),
    "本地 ReaderRuntime 必须先 ready，网络 owner 再由异步 family lease 解锁",
  );
});

test("扩展标记存在但握手未完成时，PWA 直连 owner 也保持暂停", async () => {
  const h = harness({ directSync: true });
  h.setHost();
  h.dispatch("bw:document-host-ready");
  await h.waitForEvent("bw:reader-runtime-ready");

  assert.equal(h.directHostCalls.length, 1);
  assert.equal(h.directLeaderCalls.length, 1);
  assert.equal(
    h.directLeaderCalls[0].calls.some(
      (call) => call.operation === "resume" || call.operation === "start",
    ),
    false,
  );
  assert.equal(
    h.syncRuntimeCalls[0].calls.some(
      (call) => call.operation === "resume" || call.operation === "start",
    ),
    false,
  );
  assert.deepEqual(
    h.directLeaderCalls[0].calls.at(-1),
    { operation: "pause", reason: "extension-owner-claim" },
  );
  assert.deepEqual(
    h.syncRuntimeCalls[0].calls.at(-1),
    { operation: "pause", reason: "extension-owner-claim" },
  );
});

test("扩展接管先暂停直连、再暂停持久同步；断开后两者仍保持暂停", async () => {
  const h = harness({ directSync: true });
  h.setHost();
  h.dispatch("bw:document-host-ready");
  await h.waitForEvent("bw:reader-runtime-ready");

  h.dispatch("bw:extension-provider-ready", {
    provider: { contract: "extension-provider-test" },
  });
  await h.waitForEvent("bw:reader-runtime-provider-attached");

  const directPause = h.lifecycleOrder.lastIndexOf(
    "direct-leader:pause:extension-provider-attaching",
  );
  const durablePause = h.lifecycleOrder.lastIndexOf(
    "sync-runtime:pause:extension-provider-attaching",
  );
  assert.ok(directPause >= 0 && directPause < durablePause);
  assert.equal(
    h.directLeaderCalls[0].calls.at(-1).operation,
    "pause",
  );

  h.dispatch("bw:extension-provider-disconnected", { reason: "port-closed" });
  await h.waitForEvent("bw:reader-runtime-provider-detached");

  assert.equal(
    h.lifecycleOrder.includes(
      "sync-runtime:resume:extension-provider-detached",
    ),
    false,
  );
  assert.equal(
    h.lifecycleOrder.includes(
      "direct-leader:resume:extension-provider-detached",
    ),
    false,
  );
  assert.deepEqual(
    h.directLeaderCalls[0].calls.at(-1),
    { operation: "pause", reason: "extension-provider-attaching" },
  );
});

test("扩展接管冲突或 provider error 不得误恢复 PWA 直连", async () => {
  const conflict = harness({
    directSync: true,
    attachResult: {
      connected: false,
      conflicts: [{ collection: "user-settings", id: "setting:theme" }],
    },
  });
  conflict.setHost();
  conflict.dispatch("bw:document-host-ready");
  await conflict.waitForEvent("bw:reader-runtime-ready");
  conflict.dispatch("bw:extension-provider-ready", {
    provider: { contract: "extension-provider-test" },
  });
  await conflict.waitForEvent("bw:reader-runtime-provider-conflict");
  assert.deepEqual(
    conflict.directLeaderCalls[0].calls.at(-1),
    { operation: "pause", reason: "extension-provider-attaching" },
  );

  const providerError = harness({ directSync: true });
  providerError.setHost();
  providerError.dispatch("bw:document-host-ready");
  await providerError.waitForEvent("bw:reader-runtime-ready");
  providerError.dispatch("bw:extension-provider-ready", {
    provider: { contract: "extension-provider-test" },
  });
  await providerError.waitForEvent("bw:reader-runtime-provider-attached");
  providerError.dispatch("bw:extension-provider-error", {
    code: "BW_PROVIDER_UNAVAILABLE",
    error: "port failed",
  });
  await providerError.waitForEvent("bw:reader-runtime-provider-error");
  assert.deepEqual(
    providerError.directLeaderCalls[0].calls.at(-1),
    { operation: "pause", reason: "extension-provider-attaching" },
  );
});

test("启动失败清理先销毁直连，再销毁持久同步并关闭 store", async () => {
  const h = harness({ directSync: true, failStarts: 1, marker: false });
  h.setHost();
  h.dispatch("bw:document-host-ready");
  await h.waitForEvent("bw:reader-runtime-error");

  const directDestroy = h.lifecycleOrder.indexOf("direct-leader:destroy");
  const durableDestroy = h.lifecycleOrder.indexOf("sync-runtime:destroy");
  const storeClose = h.lifecycleOrder.indexOf("store:close");
  assert.ok(directDestroy >= 0);
  assert.ok(directDestroy < durableDestroy, "直连必须先停止，避免清理期间继续调用 sync runtime");
  assert.ok(directDestroy < storeClose, "直连必须在本地 store 关闭前停止");
  assert.equal(h.directHostCalls[0].calls.at(-1).operation, "destroy");
  assert.equal(h.ownerLeaseManagers[0].status().state, "stopped");
});

test("pagehide 立即暂停网络 owner，并尽力释放 device-family lease", async () => {
  let finishRelease;
  const releaseGate = new Promise((resolve) => {
    finishRelease = resolve;
  });
  const h = harness({
    marker: false,
    directSync: true,
    ownerReleaseGate: releaseGate,
  });
  h.setHost();
  h.dispatch("bw:document-host-ready");
  await h.waitForEvent("bw:reader-runtime-ready");
  await h.waitFor(
    () => h.ownerLeaseManagers[0]?.status().state === "active",
    "active owner lease before pagehide",
  );

  h.dispatchRoot("pagehide", { persisted: false });
  await h.waitFor(
    () => h.ownerLeaseRequests.some(
      (request) => request.url.endsWith("/release"),
    ),
    "owner lease release on pagehide",
  );
  const releaseRequest = h.ownerLeaseRequests.find(
    (request) => request.url.endsWith("/release"),
  );
  assert.equal(releaseRequest.init.keepalive, true);
  assert.deepEqual(
    h.syncRuntimeCalls[0].calls.at(-1),
    { operation: "pause", reason: "pagehide" },
  );
  assert.deepEqual(
    h.directLeaderCalls[0].calls.at(-1),
    { operation: "pause", reason: "pagehide" },
  );
  assert.equal(h.ownerLeaseManagers[0].status().state, "stopped");
  await assert.rejects(
    h.ownerLeaseManagers[0].start(),
    (error) => error.code === "BW_SYNC_OWNER_INACTIVE",
    "普通关闭必须永久销毁本页 owner",
  );
  finishRelease();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(
    h.ownerLeaseRequests.filter(
      (request) => request.url.endsWith("/claim"),
    ).length,
    1,
    "普通关闭后的迟到 release 不得重新 claim",
  );
});

test("BFCache 恢复等待旧租约释放后重新领取，并恢复同一网络 runtime", async () => {
  const h = harness({ marker: false, directSync: true });
  h.setHost();
  h.dispatch("bw:document-host-ready");
  await h.waitForEvent("bw:reader-runtime-ready");
  await h.waitFor(
    () => h.ownerLeaseManagers[0]?.status().state === "active",
    "active owner lease before BFCache",
  );
  const manager = h.ownerLeaseManagers[0];
  const syncRuntime = h.syncRuntimeCalls[0];
  const directLeader = h.directLeaderCalls[0];
  const storeCount = h.stores.length;

  h.dispatchRoot("pagehide", { persisted: true });
  await h.waitFor(
    () => h.ownerLeaseRequests.some(
      (request) => request.url.endsWith("/release"),
    ),
    "owner lease release before BFCache",
  );
  const releaseIndex = h.ownerLeaseRequests.findIndex(
    (request) => request.url.endsWith("/release"),
  );
  assert.equal(h.ownerLeaseRequests[releaseIndex].init.keepalive, true);
  assert.equal(h.ownerLeaseManagers[0].status().state, "stopped");

  h.dispatchRoot("pageshow", { persisted: true });
  await h.waitFor(
    () => h.ownerLeaseRequests.filter(
      (request) => request.url.endsWith("/claim"),
    ).length === 2,
    "owner lease reacquired after BFCache",
  );
  await h.waitFor(
    () => h.ownerLeaseManagers[0].status().state === "active",
    "active owner lease after BFCache",
  );
  const lastClaimIndex = h.ownerLeaseRequests.findLastIndex(
    (request) => request.url.endsWith("/claim"),
  );
  assert.ok(releaseIndex < lastClaimIndex, "正常恢复必须先完成 release 再 claim");
  assert.strictEqual(h.ownerLeaseManagers[0], manager);
  assert.strictEqual(h.syncRuntimeCalls[0], syncRuntime);
  assert.strictEqual(h.directLeaderCalls[0], directLeader);
  assert.equal(h.ownerLeaseManagers.length, 1);
  assert.equal(h.syncRuntimeCalls.length, 1);
  assert.equal(h.directLeaderCalls.length, 1);
  assert.equal(h.stores.length, storeCount);
  assert.equal(
    h.ownerLeaseTimers.some(
      (timer) => timer.active && timer.delay === 2000,
    ),
    false,
    "release 已完成时必须清除有界等待 timer",
  );
  assert.deepEqual(
    h.syncRuntimeCalls[0].calls.at(-1),
    { operation: "resume", reason: "pwa-owner-lease-acquired" },
  );
  assert.deepEqual(
    h.directLeaderCalls[0].calls.at(-1),
    { operation: "resume", reason: "pwa-owner-lease-acquired" },
  );
});

test("BFCache release 永不结束时有界等待；HELD 只保持暂停，重试获租后才恢复", async () => {
  let finishRelease;
  const releaseGate = new Promise((resolve) => {
    finishRelease = resolve;
  });
  const h = harness({
    marker: false,
    directSync: true,
    ownerReleaseGate: releaseGate,
  });
  h.setHost();
  h.dispatch("bw:document-host-ready");
  await h.waitForEvent("bw:reader-runtime-ready");
  await h.waitFor(
    () => h.ownerLeaseManagers[0]?.status().state === "active",
    "active owner lease before pending release",
  );
  const initialResumeCount = h.syncRuntimeCalls[0].calls.filter(
    (call) => call.operation === "resume",
  ).length;

  h.dispatchRoot("pagehide", { persisted: true });
  await h.waitFor(
    () => h.ownerLeaseRequests.some(
      (request) => request.url.endsWith("/release"),
    ),
    "pending keepalive release",
  );
  assert.equal(
    h.ownerLeaseRequests.find(
      (request) => request.url.endsWith("/release"),
    ).init.keepalive,
    true,
  );
  h.dispatchRoot("pageshow", { persisted: true });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(
    h.ownerLeaseRequests.filter(
      (request) => request.url.endsWith("/claim"),
    ).length,
    1,
    "2 秒边界前不得绕过旧 release 提前 claim",
  );
  await assert.rejects(
    h.directHostCalls[0].options.relay.exchange({
      contract: "direct-sync/1",
      operation: "store-request",
    }),
    (error) => error && error.code === "BW_SYNC_OWNER_INACTIVE",
  );
  assert.equal(h.directRelayCalls.length, 0, "释放等待期间不得发生直连写入");

  h.setOwnerLeaseMode("held");
  assert.equal(
    await h.runNextOwnerLeaseTimer((timer) => timer.delay === 2000),
    true,
    "必须存在 2 秒 release 等待上限",
  );
  await h.waitFor(
    () => h.ownerLeaseRequests.filter(
      (request) => request.url.endsWith("/claim"),
    ).length === 2,
    "bounded wait claim attempt",
  );
  assert.equal(h.ownerLeaseManagers[0].status().state, "waiting");
  assert.equal(
    h.syncRuntimeCalls[0].calls.filter(
      (call) => call.operation === "resume",
    ).length,
    initialResumeCount,
    "claim 被占用时不得恢复持久同步",
  );
  assert.equal(
    h.directLeaderCalls[0].calls.at(-1).operation,
    "pause",
    "claim 被占用时直连必须继续暂停",
  );

  finishRelease();
  await new Promise((resolve) => setImmediate(resolve));
  h.setOwnerLeaseMode("acquire");
  assert.equal(
    await h.runNextOwnerLeaseTimer((timer) => timer.delay === 3000),
    true,
    "HELD 后必须沿用既有安全重试",
  );
  await h.waitFor(
    () => h.ownerLeaseManagers[0].status().state === "active",
    "owner lease active after retry",
  );
  assert.equal(
    h.syncRuntimeCalls[0].calls.filter(
      (call) => call.operation === "resume",
    ).length,
    initialResumeCount + 1,
  );
  assert.deepEqual(
    h.directLeaderCalls[0].calls.at(-1),
    { operation: "resume", reason: "pwa-owner-lease-acquired" },
  );
  assert.equal(
    h.ownerLeaseRequests.filter(
      (request) => request.url.endsWith("/claim"),
    ).length,
    3,
  );
  assert.equal(h.directRelayCalls.length, 0);
});

test("有界等待后旧 release 迟到成功或失败，都不得撤销新 generation", async () => {
  for (const ownerReleaseFailure of [false, true]) {
    let finishRelease;
    const releaseGate = new Promise((resolve) => {
      finishRelease = resolve;
    });
    const h = harness({
      marker: false,
      directSync: true,
      ownerReleaseGate: releaseGate,
      ownerReleaseFailure,
    });
    h.setHost();
    h.dispatch("bw:document-host-ready");
    await h.waitForEvent("bw:reader-runtime-ready");
    await h.waitFor(
      () => h.ownerLeaseManagers[0]?.status().state === "active",
      "active owner lease before late release response",
    );

    h.dispatchRoot("pagehide", { persisted: true });
    await h.waitFor(
      () => h.ownerLeaseRequests.some(
        (request) => request.url.endsWith("/release"),
      ),
      "pending release before late response",
    );
    h.dispatchRoot("pageshow", { persisted: true });
    await h.runNextOwnerLeaseTimer((timer) => timer.delay === 2000);
    await h.waitFor(
      () => h.ownerLeaseManagers[0].status().state === "active",
      "new owner generation after bounded wait",
    );
    const claimCount = h.ownerLeaseRequests.filter(
      (request) => request.url.endsWith("/claim"),
    ).length;
    const resumeCount = h.syncRuntimeCalls[0].calls.filter(
      (call) => call.operation === "resume",
    ).length;

    finishRelease();
    for (let turn = 0; turn < 4; turn += 1) {
      await new Promise((resolve) => setImmediate(resolve));
    }
    assert.equal(
      h.ownerLeaseManagers[0].status().state,
      "active",
      `late release failure=${ownerReleaseFailure}`,
    );
    assert.equal(
      h.ownerLeaseRequests.filter(
        (request) => request.url.endsWith("/claim"),
      ).length,
      claimCount,
    );
    assert.equal(
      h.syncRuntimeCalls[0].calls.filter(
        (call) => call.operation === "resume",
      ).length,
      resumeCount,
    );
    assert.deepEqual(
      h.directLeaderCalls[0].calls.at(-1),
      { operation: "resume", reason: "pwa-owner-lease-acquired" },
    );
  }
});

test("pageshow 等待期间再次 pagehide，旧 continuation 不得复活 PWA owner", async () => {
  let finishRelease;
  const releaseGate = new Promise((resolve) => {
    finishRelease = resolve;
  });
  const h = harness({
    marker: false,
    directSync: true,
    ownerReleaseGate: releaseGate,
  });
  h.setHost();
  h.dispatch("bw:document-host-ready");
  await h.waitForEvent("bw:reader-runtime-ready");
  await h.waitFor(
    () => h.ownerLeaseManagers[0]?.status().state === "active",
    "active owner before repeated lifecycle",
  );
  const initialResumeCount = h.syncRuntimeCalls[0].calls.filter(
    (call) => call.operation === "resume",
  ).length;

  h.dispatchRoot("pagehide", { persisted: true });
  await h.waitFor(
    () => h.ownerLeaseRequests.some(
      (request) => request.url.endsWith("/release"),
    ),
    "pending release before stale pageshow",
  );
  h.dispatchRoot("pageshow", { persisted: true });
  h.dispatchRoot("pagehide", { persisted: true });
  await h.runNextOwnerLeaseTimer((timer) => timer.delay === 2000);
  for (let turn = 0; turn < 4; turn += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.equal(
    h.ownerLeaseRequests.filter(
      (request) => request.url.endsWith("/claim"),
    ).length,
    1,
    "过期 pageshow continuation 不得重新 claim",
  );
  assert.equal(
    h.syncRuntimeCalls[0].calls.filter(
      (call) => call.operation === "resume",
    ).length,
    initialResumeCount,
  );
  assert.deepEqual(
    h.directLeaderCalls[0].calls.at(-1),
    { operation: "pause", reason: "pagehide" },
  );
  finishRelease();
});

test("pageshow 等待期间切换账户，旧账户 continuation 不得 claim 或恢复", async () => {
  let finishRelease;
  const releaseGate = new Promise((resolve) => {
    finishRelease = resolve;
  });
  const h = harness({
    marker: false,
    directSync: true,
    ownerReleaseGate: releaseGate,
  });
  h.setHost();
  h.dispatch("bw:document-host-ready");
  await h.waitForEvent("bw:reader-runtime-ready");
  await h.waitFor(
    () => h.ownerLeaseManagers[0]?.status().state === "active",
    "active owner before account switch",
  );
  const initialResumeCount = h.syncRuntimeCalls[0].calls.filter(
    (call) => call.operation === "resume",
  ).length;

  h.dispatchRoot("pagehide", { persisted: true });
  await h.waitFor(
    () => h.ownerLeaseRequests.some(
      (request) => request.url.endsWith("/release"),
    ),
    "pending release before account switch",
  );
  h.dispatchRoot("pageshow", { persisted: true });
  h.accountContext.activate({
    namespace: `acct-v1-${"e".repeat(64)}`,
    source: "server-session",
  });
  await h.runNextOwnerLeaseTimer((timer) => timer.delay === 2000);
  for (let turn = 0; turn < 4; turn += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.equal(
    h.ownerLeaseRequests.filter(
      (request) => request.url.endsWith("/claim"),
    ).length,
    1,
    "旧账户 pageshow continuation 不得重新 claim",
  );
  assert.equal(
    h.syncRuntimeCalls[0].calls.filter(
      (call) => call.operation === "resume",
    ).length,
    initialResumeCount,
  );
  assert.deepEqual(
    h.directLeaderCalls[0].calls.at(-1),
    { operation: "pause", reason: "pagehide" },
  );
  assert.equal(h.ownerLeaseManagers[0].status().state, "stopped");
  finishRelease();
});

test("pageshow 等待期间出现扩展 marker，PWA 保持 handoff 且不得重新 claim", async () => {
  let finishRelease;
  const releaseGate = new Promise((resolve) => {
    finishRelease = resolve;
  });
  const h = harness({
    marker: false,
    directSync: true,
    ownerReleaseGate: releaseGate,
  });
  h.setHost();
  h.dispatch("bw:document-host-ready");
  await h.waitForEvent("bw:reader-runtime-ready");
  await h.waitFor(
    () => h.ownerLeaseManagers[0]?.status().state === "active",
    "active owner before delayed extension marker",
  );

  h.dispatchRoot("pagehide", { persisted: true });
  await h.waitFor(
    () => h.ownerLeaseRequests.some(
      (request) => request.url.endsWith("/release"),
    ),
    "pending release before extension handoff",
  );
  h.dispatchRoot("pageshow", { persisted: true });
  h.document.documentElement.dataset.bwReaderExtensionProvider = "late";
  await h.runNextOwnerLeaseTimer((timer) => timer.delay === 2000);
  for (let turn = 0; turn < 4; turn += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.equal(
    h.ownerLeaseRequests.filter(
      (request) => request.url.endsWith("/claim"),
    ).length,
    1,
  );
  assert.deepEqual(
    h.syncRuntimeCalls[0].calls.at(-1),
    { operation: "pause", reason: "extension-owner-reserved" },
  );
  assert.deepEqual(
    h.directLeaderCalls[0].calls.at(-1),
    { operation: "pause", reason: "extension-owner-reserved" },
  );
  assert.equal(h.ownerLeaseManagers[0].status().state, "stopped");
  finishRelease();
});
