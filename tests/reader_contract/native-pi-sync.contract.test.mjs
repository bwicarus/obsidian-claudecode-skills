import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const require = createRequire(import.meta.url);
const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const DataRegistry = require(
  "../../_server_deploy/static/reader-runtime/data-registry.js",
);
const DataStore = require(
  "../../_server_deploy/static/reader-runtime/data-store.js",
);
const StorageRouter = require(
  "../../_server_deploy/static/reader-runtime/storage-router.js",
);
const PreferenceStore = require(
  "../../_server_deploy/static/reader-runtime/preference-store.js",
);
const SyncGateway = require(
  "../../_server_deploy/static/reader-runtime/sync-gateway.js",
);
const SyncCoordinator = require(
  "../../_server_deploy/static/reader-runtime/sync-coordinator.js",
);
const SyncRuntime = require(
  "../../_server_deploy/static/reader-runtime/sync-runtime.js",
);
const SyncConflictControl = require(
  "../../_server_deploy/static/reader-runtime/sync-conflict-control.js",
);
const COORDINATOR = read("ios/BWReader/App/ReaderPiSyncCoordinator.swift");
const TOOLS_VIEW = read("ios/BWReader/App/NativeReaderToolsView.swift");
// 2026-08-28 688fff07「数据与同步」统一入口：Section 壳搬进 ReaderDataHub，
// 同步按钮细节区（piSyncDetailSection）留在 ToolsView —— 断言分头指。
const DATA_HUB = read("ios/BWReader/App/ReaderDataHub.swift");
const NATIVE_BOOTSTRAP = read(
  "_server_deploy/static/reader-runtime/native-sync-bootstrap.js",
);
const NATIVE_LOCAL_RUNTIME = read(
  "_server_deploy/static/pdf/native-local-runtime.js",
);
const NATIVE_SYNC_BRIDGE = read(
  "ios/BWReader/App/ReaderNativePiSyncBridge.swift",
);
const PI_LOGIN = read("ios/BWReader/App/ReaderPiLoginView.swift");

class MemoryStorage {
  constructor(seed = {}) {
    this.map = new Map(
      Object.entries(seed).map(([key, value]) => [key, String(value)]),
    );
  }
  get length() { return this.map.size; }
  getItem(key) {
    key = String(key);
    return this.map.has(key) ? this.map.get(key) : null;
  }
  setItem(key, value) { this.map.set(String(key), String(value)); }
  removeItem(key) { this.map.delete(String(key)); }
  clear() { this.map.clear(); }
  key(index) { return [...this.map.keys()][index] ?? null; }
}

function localStore(deviceId) {
  return DataStore.createDataStore({
    backend: DataStore.createMemoryBackend(),
    deviceId,
    causalCollections: DataRegistry.syncCollections(),
  });
}

function localPreferenceContext() {
  const namespace = `acct-v1-${"a".repeat(64)}`;
  const lease = Object.freeze({
    contract: "account-context-lease/1",
    contextId: "native-local-preferences-v1",
    namespace,
    generation: 1,
  });
  let active = true;
  const isCurrent = (candidate) => active && candidate &&
    candidate.contract === lease.contract &&
    candidate.contextId === lease.contextId &&
    candidate.namespace === lease.namespace &&
    candidate.generation === lease.generation;
  return {
    context: {
      CONTRACT: "account-context/1",
      normalizeNamespace(value) {
        if (!/^acct-v1-[a-f0-9]{64}$/.test(String(value || ""))) {
          throw new Error("invalid namespace");
        }
        return String(value);
      },
      lease() { return lease; },
      isCurrent,
      assertCurrent(candidate) {
        if (!isCurrent(candidate)) throw new Error("stale context");
        return candidate;
      },
      deactivate() { active = false; },
    },
    lease,
  };
}

const CARD_BOOTSTRAP_DIGEST = `sha256:${"a".repeat(64)}`;
const ACCOUNT_BINDING = `sha256:${"d".repeat(64)}`;

function bootstrapItem(id, front, options = {}) {
  return {
    id,
    cards: [{ type: "basic", front, back: `${front} answer` }],
    states: options.states || {},
    source_ref: options.source_ref || `book:test#${id}`,
    req: options.req || "legacy requirement",
    meta: options.meta || {},
  };
}

function bootstrapPage(items, options = {}) {
  const complete = options.complete !== false;
  return {
    contract: options.contract || "reader-card-repository-bootstrap/1",
    items,
    nextCursor: complete ? null : options.nextCursor,
    complete,
    snapshotDigest: options.snapshotDigest || CARD_BOOTSTRAP_DIGEST,
  };
}

async function nativeBootstrapHarness({
  pages,
  existing = {},
  importFailure = null,
  startResult = { state: "ready", accountBinding: ACCOUNT_BINDING },
}) {
  let boundControl = null;
  let fetchIndex = 0;
  let syncStarts = 0;
  let syncRuns = 0;
  const events = [];
  const fetches = [];
  const imports = [];
  const messages = [];
  const runtime = {
    deviceId: "pwa-install-v1-0123456789abcdef0123456789abcdef",
    ready() { return Promise.resolve(true); },
    localStores() {
      return {
        global: { instanceEpoch() {}, subscribe() {}, changes() {}, applyChanges() {} },
        device: { get() {}, put() {} },
      };
    },
    preferenceStore() {
      return {
        contract: "preference-store/1",
        ready() { return Promise.resolve(true); },
      };
    },
    bindSyncControl(control) { boundControl = control; },
  };
  const context = {
    Date,
    Error,
    JSON,
    Object,
    Promise,
    Uint8Array,
    setTimeout,
    clearTimeout,
    crypto: { getRandomValues(bytes) { bytes.fill(17); } },
    fetch(path, init) {
      events.push(`fetch:${path}`);
      fetches.push({ path, init: structuredClone(init) });
      assert.ok(fetchIndex < pages.length, "bootstrap fetched beyond the fixture");
      const body = structuredClone(pages[fetchIndex]);
      fetchIndex += 1;
      return Promise.resolve({
        ok: true,
        status: 200,
        json() { return Promise.resolve(body); },
      });
    },
    BWReaderRuntime: {
      nativeLocalRuntime: runtime,
      dataRegistry: {
        SYNC_CONTRACT: "sync-v3",
        SYNC_CHANGE_CONTRACT: "record-parent-state/1",
        syncDigest() { return "sync-v3:record-parent-state/1|digest"; },
        syncCollections() {
          return [
            "card-entities", "card-states", "user-settings", "vocabulary-state",
          ];
        },
        syncCheckpointMigration() { return null; },
      },
      cardRepository: {
        CONTRACT: "card-repository/1",
        load(id, query) {
          events.push(`load:${id}`);
          assert.equal(query?.includeDeleted, true);
          return Promise.resolve(existing[id] || null);
        },
        importLegacyBatch(records, options) {
          events.push("cards:import");
          imports.push({
            records: structuredClone(records),
            options: structuredClone(options),
          });
          if (importFailure) return Promise.reject(importFailure);
          return Promise.resolve(records.map((record) => ({ id: record.id })));
        },
      },
      syncGateway: {
        createSyncGateway({ deviceId }) { return { contract: "sync-gateway/2", deviceId }; },
      },
      syncCoordinator: { createSyncCoordinator() {} },
      syncRuntime: {
        createSyncRuntime() {
          let paused = true;
          return {
            contract: "sync-runtime/1",
            start() {
              events.push("sync:start");
              syncStarts += 1;
              paused = false;
              return true;
            },
            pause() { paused = true; return true; },
            status() { return Promise.resolve({ contract: "sync-runtime/1", paused }); },
            runNow() {
              events.push("sync:run");
              syncRuns += 1;
              return Promise.resolve({ server: { ok: true, applied: 2, pendingLocal: false } });
            },
          };
        },
      },
      syncConflictControl: {
        createSyncConflictControl({ runtime: syncRuntime }) {
          return {
            syncNow(request) {
              return syncRuntime.runNow().then((result) => ({
                contract: "reader-pi-data-sync-result/1",
                requestId: request.requestId,
                owner: "native-app",
                state: "complete",
                at: Date.now(),
                collections: [
                  "card-entities", "card-states", "user-settings", "vocabulary-state",
                ],
                applied: result.server.applied,
                pendingLocal: false,
                conflictCount: 0,
                errorCode: "",
                retryable: false,
              }));
            },
            status() { return Promise.resolve({ contract: "sync-conflict-control/1" }); },
          };
        },
      },
    },
    webkit: {
      messageHandlers: {
        bwNativePiSync: {
          postMessage(message) {
            messages.push(structuredClone(message));
            events.push(`bridge:${message.action}`);
            return Promise.resolve({
              contract: "reader-native-pi-sync-response/1",
              ok: true,
              requestId: message.requestId,
              action: message.action,
              result: message.action === "release"
                ? { state: "released" }
                : structuredClone(startResult),
            });
          },
        },
      },
    },
    addEventListener() {},
  };
  context.globalThis = context;
  vm.runInNewContext(NATIVE_BOOTSTRAP, context);
  for (let attempt = 0; attempt < 8 && !boundControl; attempt += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.ok(boundControl, "native sync control should bind");
  return {
    control: boundControl,
    events,
    fetches,
    imports,
    messages,
    syncStarts: () => syncStarts,
    syncRuns: () => syncRuns,
  };
}

test("Pi sync button is explicit, serialized, and reports partial support honestly", () => {
  assert.match(DATA_HUB, /Section\("数据与同步"\)/);
  assert.match(TOOLS_VIEW, /piSync\.syncToPi\(using: reader\)/);
  assert.match(TOOLS_VIEW, /\.disabled\(piSync\.isRunning\)/);
  assert.match(TOOLS_VIEW, /ProgressView\(\)/);
  assert.match(TOOLS_VIEW, /LabeledContent\("结果", value: report\.state\.title\)/);
  assert.match(TOOLS_VIEW, /仅保存在本机、尚未同步到 Pi/);
  assert.match(TOOLS_VIEW, /本机书籍会直接离线打开/);
  assert.doesNotMatch(TOOLS_VIEW, /纯本地书会先上传到 Pi，再复用完整 Reader 打开/);
  assert.match(COORDINATOR, /guard !phase\.isRunning else \{ return \}/);
  assert.match(COORDINATOR, /static let unsupportedDomains = \[/);
});

test("book sync uploads only local changes and never overwrites Pi conflicts", () => {
  assert.match(COORDINATOR, /case \.synced:\s+summary\.unchanged \+= 1/);
  assert.match(COORDINATOR, /case \.piNewer:\s+summary\.remoteNewer\.append/);
  assert.match(COORDINATOR, /case \.conflict, \.piOnly:\s+summary\.conflicts\.append/);
  assert.match(COORDINATOR, /case \.localOnly, \.localNewer:/);
  assert.match(COORDINATOR, /remoteLibrary\.upload\(/);
  assert.match(COORDINATOR, /caseInsensitiveCompare\(digest\)/);
  assert.match(COORDINATOR, /uploadOutcomeUnknown = true/);
  assert.doesNotMatch(COORDINATOR, /remoteLibrary\.download\(/);
});

test("data sync uses the bounded owner contract and prefers native local runtime", () => {
  assert.match(COORDINATOR, /contract: "reader-pi-sync-request\/1"/);
  assert.match(COORDINATOR, /runtime\.nativeLocalRuntime\.syncNow/);
  assert.match(COORDINATOR, /runtime\.pwaRuntime\.syncControl\(\)/);
  assert.match(COORDINATOR, /decoded\.contract == "reader-pi-data-sync-result\/1"/);
  assert.match(COORDINATOR, /decoded\.requestId == requestID/);
  assert.match(COORDINATOR, /\["native-app", "pwa", "extension-background"\]/);
  assert.match(COORDINATOR, /errorCode === "BW_SYNC_REGISTRY_MISMATCH"/);
  assert.match(COORDINATOR, /safeCollections == ReaderPiSyncReport\.syncedDataCollections/);
  assert.match(COORDINATOR, /\^\[A-Za-z0-9\._-\]\+\$/);
  assert.doesNotMatch(COORDINATOR, /resolveConflict|retryAfterResolution/);
});

test("native shell uses the private Swift sync bridge and opens only the four global sync collections", () => {
  assert.match(NATIVE_BOOTSTRAP, /reader-native-pi-sync-request\/1/);
  assert.match(NATIVE_BOOTSTRAP, /bwNativePiSync/);
  assert.match(NATIVE_BOOTSTRAP, /createSyncRuntime/);
  assert.match(NATIVE_BOOTSTRAP, /createSyncGateway/);
  assert.match(NATIVE_BOOTSTRAP, /manualOnly: true/);
  assert.match(NATIVE_BOOTSTRAP, /native-sync-checkpoint\/3/);
  assert.match(NATIVE_BOOTSTRAP, /accountBinding/);
  assert.match(NATIVE_BOOTSTRAP, /instanceEpoch/);
  assert.match(NATIVE_BOOTSTRAP, /ifRev/);
  assert.match(NATIVE_BOOTSTRAP, /BW_NATIVE_SYNC_BOOTSTRAP_UNAVAILABLE/);
  assert.match(
    NATIVE_BOOTSTRAP,
    /'card-entities', 'card-states', 'user-settings', 'vocabulary-state'/,
  );
  assert.doesNotMatch(NATIVE_BOOTSTRAP, /progress-state|ink-state|sticky-note/);
  assert.match(NATIVE_LOCAL_RUNTIME, /createLocalPreferenceContext/);
  assert.match(NATIVE_LOCAL_RUNTIME, /createPreferenceStore/);
  assert.match(NATIVE_LOCAL_RUNTIME, /preferences\.attach\(router, preferenceLease\)/);
  assert.match(NATIVE_BOOTSTRAP, /runtime\.preferenceStore/);
  assert.match(NATIVE_BOOTSTRAP, /nativePreferences\.ready/);
  assert.match(
    NATIVE_SYNC_BRIDGE,
    /sync-v3:record-parent-state\/1\|card-entities:explicit:0:1\|card-states:explicit:0:1\|user-settings:explicit:0:1\|vocabulary-state:explicit:0:1/,
  );
  assert.match(NATIVE_SYNC_BRIDGE, /digest == registryDigest/);
  assert.match(NATIVE_SYNC_BRIDGE, /return "sha256:" \+ digest/);
  assert.doesNotMatch(NATIVE_SYNC_BRIDGE, /digest\.hasPrefix/);
  assert.match(NATIVE_SYNC_BRIDGE, /allowedCollections: Set<String>/);
  assert.match(NATIVE_SYNC_BRIDGE, /"card-entities", "card-states"/);
  assert.match(NATIVE_SYNC_BRIDGE, /"user-settings", "vocabulary-state"/);
  assert.match(NATIVE_SYNC_BRIDGE, /changes\.allSatisfy\(isAllowedChange\)/);
  assert.match(NATIVE_SYNC_BRIDGE, /Set\(record\.keys\) == Set/);
  assert.match(NATIVE_SYNC_BRIDGE, /isAllowedCausalProof/);
  assert.match(NATIVE_SYNC_BRIDGE, /CFBooleanGetTypeID/);
  assert.doesNotMatch(NATIVE_SYNC_BRIDGE, /progress-state|ink-state|sticky-note/);
  assert.match(COORDINATOR, /本机数据未受影响/);
});

test("native local-only control binds once and reuses an exact request receipt", async () => {
  let boundControl = null;
  const context = {
    Date,
    Error,
    JSON,
    Object,
    Promise,
    BWReaderRuntime: {
      nativeLocalRuntime: {
        syncBootstrapContext() {
          return {
            contract: "reader-native-sync-bootstrap/1",
            ready: false,
            reason: "BW_NATIVE_SYNC_BOOTSTRAP_UNAVAILABLE",
          };
        },
        bindSyncControl(control) {
          assert.equal(boundControl, null);
          boundControl = control;
        },
      },
    },
    addEventListener() {},
  };
  context.globalThis = context;
  vm.runInNewContext(NATIVE_BOOTSTRAP, context);
  for (let attempt = 0; attempt < 8 && !boundControl; attempt += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }

  assert.equal(boundControl.contract, "sync-conflict-control/1");
  assert.equal(boundControl.owner, "native-app");
  const request = {
    contract: "reader-pi-sync-request/1",
    requestId: "native-manual-1",
  };
  const first = boundControl.syncNow(request);
  const second = boundControl.syncNow(request);
  assert.strictEqual(first, second);
  const result = await first;
  assert.equal(result.contract, "reader-pi-data-sync-result/1");
  assert.equal(result.state, "blocked");
  assert.equal(result.pendingLocal, true);
  assert.equal(result.errorCode, "BW_NATIVE_SYNC_BOOTSTRAP_UNAVAILABLE");
  await assert.rejects(
    boundControl.syncNow({ ...request, force: true }),
    (error) => error?.code === "BW_PI_SYNC_REQUEST_INVALID",
  );
});

test("native real control acquires and releases through Swift without page credentials", async () => {
  let boundControl = null;
  const messages = [];
  const fetches = [];
  const runtime = {
    deviceId: "pwa-install-v1-0123456789abcdef0123456789abcdef",
    ready() { return Promise.resolve(true); },
    localStores() {
      return {
        global: { instanceEpoch() {}, subscribe() {}, changes() {}, applyChanges() {} },
        device: { get() {}, put() {} },
      };
    },
    preferenceStore() {
      return {
        contract: "preference-store/1",
        ready() { return Promise.resolve(true); },
      };
    },
    bindSyncControl(control) { boundControl = control; },
  };
  const context = {
    Date,
    Error,
    JSON,
    Object,
    Promise,
    Uint8Array,
    setTimeout,
    clearTimeout,
    crypto: { getRandomValues(bytes) { bytes.fill(7); } },
    fetch(path, init) {
      fetches.push({ path, init: structuredClone(init) });
      return Promise.resolve({
        ok: true,
        status: 200,
        json() { return Promise.resolve(bootstrapPage([])); },
      });
    },
    BWReaderRuntime: {
      nativeLocalRuntime: runtime,
      dataRegistry: {
        SYNC_CONTRACT: "sync-v3",
        SYNC_CHANGE_CONTRACT: "record-parent-state/1",
        syncDigest() { return "sync-v3:record-parent-state/1|digest"; },
        syncCollections() {
          return [
            "card-entities", "card-states", "user-settings", "vocabulary-state",
          ];
        },
        syncCheckpointMigration() { return null; },
      },
      cardRepository: {
        CONTRACT: "card-repository/1",
        load() { throw new Error("empty bootstrap must not load a card"); },
        importLegacyBatch() { throw new Error("empty bootstrap must not import"); },
      },
      syncGateway: {
        createSyncGateway({ deviceId }) { return { contract: "sync-gateway/2", deviceId }; },
      },
      syncCoordinator: { createSyncCoordinator() {} },
      syncRuntime: {
        createSyncRuntime() {
          let paused = true;
          return {
            contract: "sync-runtime/1",
            start() { paused = false; return true; },
            pause() { paused = true; return true; },
            status() { return Promise.resolve({ contract: "sync-runtime/1", paused }); },
            runNow() { return Promise.resolve({ server: { ok: true, applied: 2, pendingLocal: false } }); },
          };
        },
      },
      syncConflictControl: {
        createSyncConflictControl({ runtime: syncRuntime }) {
          return {
            syncNow(request) {
              return syncRuntime.runNow().then((result) => ({
                contract: "reader-pi-data-sync-result/1",
                requestId: request.requestId,
                owner: "native-app",
                state: "complete",
                at: Date.now(),
                collections: [
                  "card-entities", "card-states", "user-settings", "vocabulary-state",
                ],
                applied: result.server.applied,
                pendingLocal: false,
                conflictCount: 0,
                errorCode: "",
                retryable: false,
              }));
            },
            status() { return Promise.resolve({ contract: "sync-conflict-control/1" }); },
          };
        },
      },
    },
    webkit: {
      messageHandlers: {
        bwNativePiSync: {
          postMessage(message) {
            messages.push(structuredClone(message));
            return Promise.resolve({
              contract: "reader-native-pi-sync-response/1",
              ok: true,
              requestId: message.requestId,
              action: message.action,
              result: message.action === "release"
                ? { state: "released" }
                : { state: "ready", accountBinding: ACCOUNT_BINDING },
            });
          },
        },
      },
    },
    addEventListener() {},
  };
  context.globalThis = context;
  vm.runInNewContext(NATIVE_BOOTSTRAP, context);
  for (let attempt = 0; attempt < 8 && !boundControl; attempt += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }

  const result = await boundControl.syncNow({
    contract: "reader-pi-sync-request/1",
    requestId: "native-real-1",
  });
  assert.equal(result.state, "complete");
  assert.deepEqual(messages.map((message) => message.action), ["start", "release"]);
  assert.deepEqual(fetches.map((request) => request.path), [
    "/pdf/api/card-repository/bootstrap?limit=100",
  ]);
  assert.equal(fetches[0].init.method, "GET");
  assert.equal(fetches[0].init.cache, "no-store");
  assert.equal(messages[0].ownerNamespace, undefined);
  assert.equal(messages[0].ownerToken, undefined);
  assert.deepEqual(Object.keys(messages[1]).sort(), ["action", "contract", "requestId"]);
});

test("native manual sync imports only missing legacy gids and ignores all old data for a local gid", async () => {
  const cursor = "eyJhZnRlciI6ImNhcmRfYWFhYSJ9";
  const stalePiCopy = bootstrapItem("card_aaaa", "stale Pi content must be ignored", {
    states: { 0: { _st: "done", _next: "90d" } },
    meta: { ts: 1_700_000_000, custom: { source: "pi" } },
  });
  const remoteOnly = bootstrapItem("card_bbbb", "remote only", {
    states: { 0: { _st: "learn", _next: "20m" } },
    meta: { ts: 1_700_000_123 },
  });
  const harness = await nativeBootstrapHarness({
    pages: [
      bootstrapPage([stalePiCopy], { complete: false, nextCursor: cursor }),
      bootstrapPage([remoteOnly]),
    ],
    existing: {
      card_aaaa: {
        id: "card_aaaa",
        cards: [{ type: "basic", front: "locally edited", back: "new local answer" }],
        states: { 0: { exactState: { _st: "review", _next: "180d" } } },
      },
    },
  });

  const result = await harness.control.syncNow({
    contract: "reader-pi-sync-request/1",
    requestId: "native-card-bootstrap-success",
  });

  assert.equal(result.state, "complete");
  assert.deepEqual(harness.fetches.map((request) => request.path), [
    "/pdf/api/card-repository/bootstrap?limit=100",
    `/pdf/api/card-repository/bootstrap?limit=100&cursor=${cursor}`,
  ]);
  assert.equal(harness.imports.length, 1);
  assert.equal(
    harness.imports[0].options.mutationId,
    "native-card-bootstrap:native-card-bootstrap-success",
  );
  assert.equal(harness.imports[0].options.missingOnly, true);
  assert.deepEqual(
    harness.imports[0].records.map((record) => record.id),
    ["card_bbbb"],
    "an existing local gid must not reach legacy import at all",
  );
  assert.deepEqual(
    harness.imports[0].records[0].states,
    remoteOnly.states,
    "a missing local gid imports its legacy state",
  );
  assert.equal(harness.imports[0].records[0].ts, 1_700_000_123);
  assert.deepEqual(harness.imports[0].records[0].meta, remoteOnly.meta);
  assert.equal(harness.syncStarts(), 1);
  assert.equal(harness.syncRuns(), 1);
  const startAt = harness.events.indexOf("bridge:start");
  const firstFetchAt = harness.events.findIndex((event) => event.startsWith("fetch:"));
  const importAt = harness.events.indexOf("cards:import");
  const syncStartAt = harness.events.indexOf("sync:start");
  const releaseAt = harness.events.indexOf("bridge:release");
  assert.ok(firstFetchAt >= 0 && firstFetchAt < importAt);
  assert.ok(importAt < startAt);
  assert.ok(startAt < syncStartAt);
  assert.ok(syncStartAt < releaseAt);
});

test("native legacy pagination fails closed before import or sync-v3", async (t) => {
  const cursor = "eyJhZnRlciI6ImNhcmRfYWFhYSJ9";
  const tooMany = Array.from({ length: 101 }, (_, index) =>
    bootstrapItem(`card_${index.toString(16).padStart(4, "0")}`, `item ${index}`));
  const overAtomicLimit = Array.from({ length: 501 }, (_, index) =>
    bootstrapItem(`card_${index.toString(16).padStart(4, "0")}`, `item ${index}`));
  const limitPages = [];
  for (let offset = 0; offset < overAtomicLimit.length; offset += 100) {
    const selected = overAtomicLimit.slice(offset, offset + 100);
    const complete = offset + selected.length >= overAtomicLimit.length;
    limitPages.push(bootstrapPage(selected, complete ? {} : {
      complete: false,
      nextCursor: `cGFnZV8${String(offset / 100)}`,
    }));
  }
  const cases = [
    {
      name: "response contract changes",
      pages: [bootstrapPage([], { contract: "reader-card-repository-bootstrap/2" })],
      code: "BW_CARD_BOOTSTRAP_CONTRACT",
    },
    {
      name: "snapshot digest changes",
      pages: [
        bootstrapPage([bootstrapItem("card_aaaa", "A")], {
          complete: false,
          nextCursor: cursor,
        }),
        bootstrapPage([bootstrapItem("card_bbbb", "B")], {
          snapshotDigest: `sha256:${"b".repeat(64)}`,
        }),
      ],
      code: "BW_CARD_BOOTSTRAP_SNAPSHOT_CHANGED",
    },
    {
      name: "cursor loops",
      pages: [
        bootstrapPage([bootstrapItem("card_aaaa", "A")], {
          complete: false,
          nextCursor: cursor,
        }),
        bootstrapPage([bootstrapItem("card_bbbb", "B")], {
          complete: false,
          nextCursor: cursor,
        }),
      ],
      code: "BW_CARD_BOOTSTRAP_CURSOR_LOOP",
    },
    {
      name: "cursor exceeds its opaque transport bound",
      pages: [bootstrapPage([bootstrapItem("card_aaaa", "A")], {
        complete: false,
        nextCursor: "a".repeat(513),
      })],
      code: "BW_CARD_BOOTSTRAP_CURSOR",
    },
    {
      name: "one page exceeds the server item limit",
      pages: [bootstrapPage(tooMany)],
      code: "BW_CARD_BOOTSTRAP_CONTRACT",
    },
    {
      name: "the complete snapshot exceeds one atomic repository batch",
      pages: limitPages,
      code: "BW_CARD_BOOTSTRAP_LIMIT",
    },
  ];

  for (const [index, fixture] of cases.entries()) {
    await t.test(fixture.name, async () => {
      const harness = await nativeBootstrapHarness({ pages: fixture.pages });
      await assert.rejects(
        harness.control.syncNow({
          contract: "reader-pi-sync-request/1",
          requestId: `native-card-bootstrap-fail-${index}`,
        }),
        (error) => error?.code === fixture.code,
      );
      assert.equal(harness.imports.length, 0);
      assert.equal(harness.syncStarts(), 0);
      assert.equal(harness.syncRuns(), 0);
      assert.equal(harness.messages.length, 0);
    });
  }
});

test("native legacy import rejection happens before owner claim or sync-v3", async () => {
  const importFailure = Object.assign(new Error("atomic import rejected"), {
    code: "BW_CARD_REPOSITORY_LEGACY_CONFLICT",
    retryable: false,
  });
  const harness = await nativeBootstrapHarness({
    pages: [bootstrapPage([bootstrapItem("card_aaaa", "A")])],
    importFailure,
  });
  await assert.rejects(
    harness.control.syncNow({
      contract: "reader-pi-sync-request/1",
      requestId: "native-card-bootstrap-import-reject",
    }),
    (error) => error?.code === "BW_CARD_REPOSITORY_LEGACY_CONFLICT",
  );
  assert.equal(harness.imports.length, 1);
  assert.equal(harness.syncStarts(), 0);
  assert.equal(harness.syncRuns(), 0);
  assert.equal(harness.messages.length, 0);
});

test("native start without opaque account binding fails closed before checkpoint or sync", async () => {
  const harness = await nativeBootstrapHarness({
    pages: [bootstrapPage([])],
    startResult: { state: "ready" },
  });
  await assert.rejects(
    harness.control.syncNow({
      contract: "reader-pi-sync-request/1",
      requestId: "native-account-binding-missing",
    }),
    (error) => error?.code === "BW_NATIVE_SYNC_START_RESPONSE",
  );
  assert.deepEqual(harness.messages.map((message) => message.action), ["start", "release"]);
  assert.equal(harness.syncStarts(), 0);
  assert.equal(harness.syncRuns(), 0);
});

test("native checkpoint is loaded only after start and resets on opaque account switch", async () => {
  const vaultEpoch = `data-store-instance-v1-${"c".repeat(32)}`;
  const oldBinding = `sha256:${"a".repeat(64)}`;
  const newBinding = `sha256:${"b".repeat(64)}`;
  let stored = {
    rev: 7,
    deleted: false,
    value: {
      id: "native-sync-checkpoint-v1",
      contract: "native-sync-checkpoint/3",
      schema: 3,
      vaultEpoch,
      accountBinding: oldBinding,
      checkpoint: {
        contract: "sync-coordinator/1",
        schema: 1,
        registryDigest: DataRegistry.syncDigest(),
        generation: 9,
        server: { localCursor: 41, remoteCursor: 53, reconciliationEpoch: 2 },
        peers: {},
      },
    },
  };
  let checkpointStore = null;
  let boundControl = null;
  let loadedCheckpoint = "not-loaded";
  let checkpointWrites = 0;
  let localDataMutations = 0;
  const events = [];
  const incoming = {
    contract: "sync-coordinator/1",
    schema: 1,
    registryDigest: DataRegistry.syncDigest(),
    generation: 1,
    server: { localCursor: 0, remoteCursor: 0, reconciliationEpoch: 0 },
    peers: {},
  };
  const runtime = {
    deviceId: "pwa-install-v1-0123456789abcdef0123456789abcdef",
    ready() { return Promise.resolve(true); },
    localStores() {
      return {
        global: {
          instanceEpoch() { return Promise.resolve(vaultEpoch); },
          subscribe() {},
          changes() { localDataMutations += 1; },
          applyChanges() { localDataMutations += 1; },
        },
        device: {
          get(collection, id) {
            assert.equal(collection, "ui-session");
            assert.equal(id, "native-sync-checkpoint-v1");
            return Promise.resolve(structuredClone(stored));
          },
          put(collection, value, options) {
            assert.equal(collection, "ui-session");
            assert.equal(options.ifRev, stored.rev);
            checkpointWrites += 1;
            stored = { rev: stored.rev + 1, deleted: false, value: structuredClone(value) };
            return Promise.resolve(structuredClone(stored));
          },
        },
      };
    },
    preferenceStore() {
      return {
        contract: "preference-store/1",
        ready() { return Promise.resolve(true); },
      };
    },
    bindSyncControl(control) { boundControl = control; },
  };
  const context = {
    Date,
    Error,
    JSON,
    Object,
    Promise,
    Uint8Array,
    fetch() {
      events.push("bootstrap");
      return Promise.resolve({
        ok: true,
        status: 200,
        json() { return Promise.resolve(bootstrapPage([])); },
      });
    },
    crypto: { getRandomValues(bytes) { bytes.fill(9); } },
    BWReaderRuntime: {
      nativeLocalRuntime: runtime,
      dataRegistry: DataRegistry,
      cardRepository: {
        CONTRACT: "card-repository/1",
        load() { throw new Error("empty bootstrap must not load a card"); },
        importLegacyBatch() { throw new Error("empty bootstrap must not import"); },
      },
      syncGateway: { createSyncGateway() { return {}; } },
      syncCoordinator: { createSyncCoordinator() {} },
      syncRuntime: {
        createSyncRuntime(options) {
          checkpointStore = options.checkpointStore;
          return {
            start() { events.push("runtime:start"); },
            pause() {},
            status() { return Promise.resolve({}); },
          };
        },
      },
      syncConflictControl: {
        createSyncConflictControl() {
          return {
            status() { return Promise.resolve({}); },
            syncNow(request) {
              events.push("checkpoint:load");
              return checkpointStore.load().then((value) => {
                loadedCheckpoint = value;
                events.push("checkpoint:save");
                return checkpointStore.save(incoming);
              }).then(() => ({
                contract: "reader-pi-data-sync-result/1",
                requestId: request.requestId,
                owner: "native-app",
                state: "complete",
                at: Date.now(),
                collections: [
                  "card-entities", "card-states", "user-settings", "vocabulary-state",
                ],
                applied: 0,
                pendingLocal: false,
                conflictCount: 0,
                errorCode: "",
                retryable: false,
              }));
            },
          };
        },
      },
    },
    webkit: {
      messageHandlers: {
        bwNativePiSync: {
          postMessage(message) {
            events.push(`bridge:${message.action}`);
            return Promise.resolve({
              contract: "reader-native-pi-sync-response/1",
              ok: true,
              requestId: message.requestId,
              action: message.action,
              result: message.action === "release"
                ? { state: "released" }
                : { state: "ready", accountBinding: newBinding },
            });
          },
        },
      },
    },
    addEventListener() {},
  };
  context.globalThis = context;
  vm.runInNewContext(NATIVE_BOOTSTRAP, context);
  for (let attempt = 0; attempt < 8 && !boundControl; attempt += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }

  assert.ok(boundControl);
  assert.ok(checkpointStore);
  await assert.rejects(
    checkpointStore.load(),
    (error) => error?.code === "BW_SYNC_ACCOUNT_BINDING",
    "checkpoint access before Swift start must fail closed",
  );
  const result = await boundControl.syncNow({
    contract: "reader-pi-sync-request/1",
    requestId: "native-account-switch",
  });
  assert.equal(result.state, "complete");
  assert.equal(loadedCheckpoint, null, "an old account cursor must not be reused");
  assert.equal(checkpointWrites, 1);
  assert.equal(stored.value.contract, "native-sync-checkpoint/3");
  assert.equal(stored.value.schema, 3);
  assert.equal(stored.value.accountBinding, newBinding);
  assert.equal(stored.value.checkpoint.server.localCursor, 0);
  assert.equal(localDataMutations, 0, "account reset must not delete local data");
  assert.ok(events.indexOf("bridge:start") < events.indexOf("checkpoint:load"));
});

test("native local setting enters user-settings and the real sync push payload", async () => {
  const deviceId = "pwa-install-v1-0123456789abcdef0123456789abcdef";
  const globalStore = localStore(deviceId);
  globalStore.instanceEpoch = () =>
    Promise.resolve(`data-store-instance-v1-${"b".repeat(32)}`);
  const documentStore = localStore(`${deviceId}-document`);
  const deviceStore = localStore(`${deviceId}-device`);
  const router = StorageRouter.createStorageRouter({
    globalStore,
    documentStore,
    deviceStore,
    scopes: DataRegistry.scopes(),
    dataRegistryApi: DataRegistry,
  });
  const storage = new MemoryStorage({ rcWebTrStyle: "inline" });
  const localContext = localPreferenceContext();
  const preferences = PreferenceStore.createPreferenceStore({
    accountContext: localContext.context,
    dataRegistry: DataRegistry,
    storage,
    lease: localContext.lease,
    messageBridge: false,
  });
  await preferences.attach(router, localContext.lease);
  const messages = [];
  let boundControl = null;
  const runtime = {
    deviceId,
    ready() { return Promise.resolve(true); },
    localStores() {
      return {
        global: globalStore,
        document: documentStore,
        // The bootstrap runs in a vm realm in this contract test. Normalize
        // checkpoint envelopes back into this realm before DataStore's strict
        // plain-object validator sees them.
        device: {
          get() { return deviceStore.get(...arguments); },
          put(collection, value, options) {
            return deviceStore.put(
              collection,
              structuredClone(value),
              structuredClone(options),
            );
          },
        },
      };
    },
    storage() { return router; },
    preferenceStore() { return preferences; },
    bindSyncControl(control) { boundControl = control; },
  };
  const context = {
    Date,
    Error,
    JSON,
    Object,
    Promise,
    Uint8Array,
    setTimeout,
    clearTimeout,
    localStorage: storage,
    document: { addEventListener() {}, removeEventListener() {}, dispatchEvent() {} },
    crypto: { getRandomValues(bytes) { bytes.fill(11); } },
    fetch() {
      return Promise.resolve({
        ok: true,
        status: 200,
        json() { return Promise.resolve(bootstrapPage([])); },
      });
    },
    BWReaderRuntime: {
      nativeLocalRuntime: runtime,
      dataRegistry: DataRegistry,
      cardRepository: {
        CONTRACT: "card-repository/1",
        load() { throw new Error("empty bootstrap must not load a card"); },
        importLegacyBatch() { throw new Error("empty bootstrap must not import"); },
      },
      preferenceStore: PreferenceStore,
      syncGateway: SyncGateway,
      syncCoordinator: SyncCoordinator,
      syncRuntime: SyncRuntime,
      syncConflictControl: SyncConflictControl,
    },
    webkit: {
      messageHandlers: {
        bwNativePiSync: {
          postMessage(message) {
            messages.push(structuredClone(message));
            const payload = message.payload || {};
            const changes = Array.isArray(payload.changes) ? payload.changes : [];
            return Promise.resolve({
              contract: "reader-native-pi-sync-response/1",
              ok: true,
              requestId: message.requestId,
              action: message.action,
              result: message.action === "exchange" ? {
                cursor: Number(payload.cursor) || 0,
                headCursor: Number(payload.cursor) || 0,
                oldestCursor: 0,
                resetRequired: false,
                hasMore: false,
                ackedMutationIds: changes.map((change) => change.mutationId),
                changes: [],
                conflicts: [],
              } : (message.action === "release"
                ? { state: "released" }
                : { state: "ready", accountBinding: ACCOUNT_BINDING }),
            });
          },
        },
      },
    },
    addEventListener() {},
    removeEventListener() {},
  };
  context.globalThis = context;
  vm.runInNewContext(NATIVE_BOOTSTRAP, context);
  for (let attempt = 0; attempt < 8 && !boundControl; attempt += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }

  assert.ok(boundControl, "native sync control should bind after preference attach");
  const stored = await router.get(
    "user-settings",
    "setting:translation.display-style",
  );
  assert.equal(stored.value.rawValue, "inline");

  const result = await boundControl.syncNow({
    contract: "reader-pi-sync-request/1",
    requestId: "native-setting-push-1",
  });
  assert.equal(result.state, "complete");
  const push = messages.find(
    (message) => message.action === "exchange" &&
      message.payload?.direction === "push" &&
      message.payload.changes?.length,
  );
  assert.ok(push, "sync should push the migrated local setting");
  assert.equal(push.payload.changes[0].collection, "user-settings");
  assert.equal(
    push.payload.changes[0].record.value.rawValue,
    "inline",
  );
  for (const message of messages) {
    assert.equal(message.ownerNamespace, undefined);
    assert.equal(message.ownerToken, undefined);
    assert.equal(JSON.stringify(message).includes("acct-v1-"), false);
  }
});

test("Swift sync bridge keeps namespace and capabilities private and exposes login recovery", () => {
  assert.match(NATIVE_SYNC_BRIDGE, /static let messageName = "bwNativePiSync"/);
  assert.match(NATIVE_SYNC_BRIDGE, /message\.frameInfo\.isMainFrame/);
  assert.match(NATIVE_SYNC_BRIDGE, /native\/bootstrap/);
  assert.match(NATIVE_SYNC_BRIDGE, /owner\/claim/);
  assert.match(NATIVE_SYNC_BRIDGE, /owner\/renew/);
  assert.match(NATIVE_SYNC_BRIDGE, /owner\/release/);
  assert.match(NATIVE_SYNC_BRIDGE, /private var lease: Lease\?/);
  assert.match(
    NATIVE_SYNC_BRIDGE,
    /current\.expiresAt\.timeIntervalSinceNow >= 10/,
  );
  assert.match(
    NATIVE_SYNC_BRIDGE,
    /lease = try await renew\(current\)[\s\S]*?catch \{[\s\S]*?lease = nil/,
  );
  assert.match(NATIVE_SYNC_BRIDGE, /BW_PI_AUTH_REQUIRED/);
  assert.match(NATIVE_SYNC_BRIDGE, /pathMatches/);
  assert.match(NATIVE_SYNC_BRIDGE, /expiryMatches/);
  assert.match(NATIVE_SYNC_BRIDGE, /secureMatches/);
  assert.match(NATIVE_SYNC_BRIDGE, /current\.path\.count >= cookie\.path\.count/);
  assert.doesNotMatch(NATIVE_SYNC_BRIDGE, /print\(|NSLog\(|os_log/);
  assert.match(PI_LOGIN, /ReaderNativePiSyncBridge\.loginURL/);
  assert.match(PI_LOGIN, /websiteDataStore = dataStore/);
  // 2026-09-02 Pi 整体退出:登录面固定指向 Windows 上的 Flask(bwicarus-2)。
  assert.match(PI_LOGIN, /bwicarus-2\.taile44d0c\.ts\.net/);
  assert.doesNotMatch(PI_LOGIN, /"https:\/\/bwicarus\.taile44d0c\.ts\.net/);
  // loginFlowPaths 语义修正后只含登录流程本身；dashboard 是登录后的
  // 落点（跳到列表之外即视为登录成功），不再在列表里。
  assert.match(PI_LOGIN, /"\/login", "\/logout", "\/register",/);
  assert.match(TOOLS_VIEW, /登录或重新登录 Pi/);
});

test("Pi sync summaries interpolate runtime counts instead of showing variable names", () => {
  for (const interpolation of [
    "\\(uploaded)",
    "\\(unchanged)",
    "\\(conflictCount)",
    "\\(errorCode)",
    "\\(current)/\\(total)",
    "\\(title)",
  ]) {
    assert.equal(COORDINATOR.includes(interpolation), true, interpolation);
  }
  for (const literal of [
    "已上传或关联 (uploaded)",
    "无需上传 (unchanged)",
    "存在 (conflictCount)",
    "正在上传 (current)/(total)：(title)",
  ]) {
    assert.equal(COORDINATOR.includes(literal), false, literal);
  }
});
