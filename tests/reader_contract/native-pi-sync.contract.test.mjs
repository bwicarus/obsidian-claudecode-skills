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

test("Pi sync button is explicit, serialized, and reports partial support honestly", () => {
  assert.match(TOOLS_VIEW, /Section\("Pi 同步"\)/);
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
  assert.match(COORDINATOR, /\^\[A-Za-z0-9\._-\]\+\$/);
  assert.doesNotMatch(COORDINATOR, /resolveConflict|retryAfterResolution/);
});

test("native shell uses the private Swift sync bridge and keeps pending domains closed", () => {
  assert.match(NATIVE_BOOTSTRAP, /reader-native-pi-sync-request\/1/);
  assert.match(NATIVE_BOOTSTRAP, /bwNativePiSync/);
  assert.match(NATIVE_BOOTSTRAP, /createSyncRuntime/);
  assert.match(NATIVE_BOOTSTRAP, /createSyncGateway/);
  assert.match(NATIVE_BOOTSTRAP, /manualOnly: true/);
  assert.match(NATIVE_BOOTSTRAP, /native-sync-checkpoint\/2/);
  assert.match(NATIVE_BOOTSTRAP, /instanceEpoch/);
  assert.match(NATIVE_BOOTSTRAP, /ifRev/);
  assert.match(NATIVE_BOOTSTRAP, /BW_NATIVE_SYNC_BOOTSTRAP_UNAVAILABLE/);
  assert.match(
    NATIVE_BOOTSTRAP,
    /\['user-settings', 'vocabulary-state'\]/,
  );
  assert.doesNotMatch(NATIVE_BOOTSTRAP, /progress-state|ink-state|sticky-note/);
  assert.match(NATIVE_LOCAL_RUNTIME, /createLocalPreferenceContext/);
  assert.match(NATIVE_LOCAL_RUNTIME, /createPreferenceStore/);
  assert.match(NATIVE_LOCAL_RUNTIME, /preferences\.attach\(router, preferenceLease\)/);
  assert.match(NATIVE_BOOTSTRAP, /runtime\.preferenceStore/);
  assert.match(NATIVE_BOOTSTRAP, /nativePreferences\.ready/);
  assert.match(
    NATIVE_SYNC_BRIDGE,
    /sync-v3:record-parent-state\/1\|user-settings:explicit:0:1\|vocabulary-state:explicit:0:1/,
  );
  assert.match(NATIVE_SYNC_BRIDGE, /digest == registryDigest/);
  assert.doesNotMatch(NATIVE_SYNC_BRIDGE, /digest\.hasPrefix/);
  assert.match(NATIVE_SYNC_BRIDGE, /allowedCollections: Set<String>/);
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
    BWReaderRuntime: {
      nativeLocalRuntime: runtime,
      dataRegistry: {
        SYNC_CONTRACT: "sync-v3",
        SYNC_CHANGE_CONTRACT: "record-parent-state/1",
        syncDigest() { return "sync-v3:record-parent-state/1|digest"; },
        syncCollections() { return ["user-settings", "vocabulary-state"]; },
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
                collections: ["user-settings", "vocabulary-state"],
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
              result: { state: message.action === "release" ? "released" : "ready" },
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
  assert.equal(messages[0].ownerNamespace, undefined);
  assert.equal(messages[0].ownerToken, undefined);
  assert.deepEqual(Object.keys(messages[1]).sort(), ["action", "contract", "requestId"]);
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
    BWReaderRuntime: {
      nativeLocalRuntime: runtime,
      dataRegistry: DataRegistry,
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
              } : { state: message.action === "release" ? "released" : "ready" },
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
  assert.match(PI_LOGIN, /bwicarus\.taile44d0c\.ts\.net/);
  assert.match(PI_LOGIN, /"\/login", "\/logout", "\/register", "\/dashboard\/"/);
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
