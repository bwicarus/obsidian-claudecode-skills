import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import {
  DataRegistry,
  DataStore,
  SyncGateway,
  makeStore,
} from "./helpers.mjs";

const require = createRequire(import.meta.url);
const Coordinator = require(
  "../../_server_deploy/static/reader-runtime/sync-coordinator.js",
);
const Direct = require(
  "../../_server_deploy/static/reader-runtime/direct-sync-protocol.js",
);

function emptyPull(request) {
  return {
    contract: "sync-gateway/1",
    cursor: request.cursor,
    headCursor: request.cursor,
    oldestCursor: 0,
    hasMore: false,
    resetRequired: false,
    ackedMutationIds: [],
    changes: [],
    conflicts: [],
  };
}

function ackingGateway(log = []) {
  return {
    async push(request) {
      log.push(structuredClone(request));
      return {
        ...emptyPull(request),
        ackedMutationIds: (request.changes || []).map((change) => change.mutationId),
      };
    },
    async pull(request) {
      return emptyPull(request);
    },
  };
}

function syncedChange({
  cursor = 1,
  mutationId = "server-change",
  collection = "user-settings",
  id = "setting",
  rev = 1,
  deleted = false,
  value = {},
  updatedAt = 1,
  updatedBy = "server",
  parent = null,
} = {}) {
  return {
    cursor,
    mutationId,
    operation: deleted ? "remove" : "put",
    collection,
    record: {
      schema: 1,
      collection,
      id,
      rev,
      updatedAt,
      updatedBy,
      deleted,
      value: { id, ...structuredClone(value) },
      causal: {
        contract: "record-parent-state/1",
        parent: parent == null ? null : structuredClone(parent),
      },
    },
  };
}

const LEGACY_SYNC_DIGEST =
  "sync-v2:card-entities:explicit:0:1|card-states:explicit:0:1|user-settings:explicit:0:1|vocabulary-state:explicit:0:1";
const PREVIOUS_CARDLESS_DIGEST =
  "sync-v3:record-parent-state/1|user-settings:explicit:0:1|vocabulary-state:explicit:0:1";

test("DataRegistry 的同步白名单唯一且不含 device/document/pending", () => {
  assert.deepEqual(DataRegistry.syncCollections(), [
    "card-entities",
    "card-states",
    "user-settings",
    "vocabulary-state",
  ]);
  assert.deepEqual(DataRegistry.syncDescriptor(), [
    {
      name: "card-entities",
      conflictPolicy: "explicit",
      derived: false,
      recordSchema: 1,
    },
    {
      name: "card-states",
      conflictPolicy: "explicit",
      derived: false,
      recordSchema: 1,
    },
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
  ]);
  assert.equal(
    DataRegistry.syncDigest(),
    "sync-v3:record-parent-state/1|card-entities:explicit:0:1|card-states:explicit:0:1|user-settings:explicit:0:1|vocabulary-state:explicit:0:1",
  );
  for (const name of DataRegistry.syncCollections()) {
    assert.equal(DataRegistry.isProviderCollection(name), true);
    assert.equal(DataRegistry.collection(name).sync, true);
  }
  for (const name of ["dictionary-cache", "query-cache", "translation-cache"]) {
    assert.equal(DataRegistry.isProviderCollection(name), true);
    assert.equal(DataRegistry.isSyncCollection(name), false);
    assert.equal(DataRegistry.collection(name).derived, true);
  }
  for (const name of ["cards", "document-notes", "device-preferences", "ui-session"]) {
    assert.equal(DataRegistry.isSyncCollection(name), false);
  }
  assert.deepEqual(
    DataRegistry.syncCheckpointMigration(PREVIOUS_CARDLESS_DIGEST),
    {
      contract: "sync-registry-migration/1",
      from: PREVIOUS_CARDLESS_DIGEST,
      to: DataRegistry.syncDigest(),
      strategy: "reset-checkpoint",
    },
  );
  assert.equal(DataRegistry.syncCheckpointMigration("sync-v3:unknown"), null);
});

test("协调器对缺失或自相矛盾的 sync-v3 描述 fail closed", () => {
  const store = makeStore("bad-sync-registry");
  const base = {
    ...DataRegistry,
    syncDescriptor: () => DataRegistry.syncDescriptor(),
  };
  const cases = [
    { ...base, SYNC_CONTRACT: "sync-v1" },
    { ...base, syncDescriptor: undefined },
    {
      ...base,
      syncDescriptor: () => DataRegistry.syncDescriptor().map((entry) => (
        entry.name === "user-settings"
          ? { ...entry, conflictPolicy: "last-write-wins" }
          : entry
      )),
    },
    { ...base, syncDigest: () => "sync-v2:forged" },
  ];
  for (const registry of cases) {
    assert.throws(
      () => Coordinator.createSyncCoordinator({
        store,
        registry,
        serverGateway: ackingGateway(),
      }),
      (error) => error?.code === "BW_SYNC_REGISTRY",
    );
  }
});

test("sync-v2 升级会跳过本地与 relay 中遗留的 regenerate 缓存事件并推进游标", async () => {
  const store = makeStore("sync-v2-retired-cache");
  await store.put(
    "dictionary-cache",
    { id: "local-cache", text: "可重建" },
    { mutationId: "local-cache-op" },
  );
  await store.put(
    "user-settings",
    { id: "local-setting", mode: "dark" },
    { mutationId: "local-setting-op" },
  );
  const pushes = [];
  let pulled = false;
  const server = {
    async push(request) {
      pushes.push(structuredClone(request));
      return {
        ...emptyPull(request),
        ackedMutationIds: request.changes.map((change) => change.mutationId),
      };
    },
    async pull(request) {
      if (pulled) return emptyPull(request);
      pulled = true;
      return {
        ...emptyPull(request),
        cursor: 2,
        headCursor: 2,
        changes: [
          syncedChange({
            cursor: 1,
            mutationId: "remote-cache-op",
            collection: "dictionary-cache",
            id: "remote-cache",
            value: { text: "旧 relay 缓存" },
          }),
          syncedChange({
            cursor: 2,
            mutationId: "remote-setting-op",
            id: "remote-setting",
            value: { mode: "light" },
          }),
        ],
      };
    },
  };
  const coordinator = Coordinator.createSyncCoordinator({
    store,
    registry: DataRegistry,
    serverGateway: server,
  });

  const result = await coordinator.runOnce();
  assert.deepEqual(
    pushes[0].changes.map((change) => change.mutationId),
    ["local-setting-op"],
  );
  assert.equal(result.server.localCursor, 2);
  assert.equal(result.server.remoteCursor, 2);
  assert.equal(result.server.applied, 1);
  assert.equal(await store.get("dictionary-cache", "remote-cache"), null);
  assert.equal(
    (await store.get("user-settings", "remote-setting")).value.mode,
    "light",
  );
});

test("sync-v2 完整快照忽略 relay 旧派生缓存 head 并继续推进快照游标", async () => {
  const store = makeStore("sync-v2-retired-cache-snapshot");
  let pushCount = 0;
  const server = {
    async push(request) {
      pushCount += 1;
      if (pushCount === 1) {
        return {
          ...emptyPull(request),
          resetRequired: true,
        };
      }
      return {
        ...emptyPull(request),
        ackedMutationIds: request.changes.map((change) => change.mutationId),
      };
    },
    async pull(request) {
      return emptyPull(request);
    },
    async snapshot(request) {
      assert.equal(request.offset, 0);
      return {
        contract: "sync-gateway/1",
        snapshotId: "snapshot-sync-v2-migration",
        snapshotCursor: 2,
        offset: 0,
        nextOffset: 2,
        hasMore: false,
        changes: [
          syncedChange({
            cursor: 1,
            mutationId: "snapshot-cache-op",
            collection: "translation-cache",
            id: "snapshot-cache",
            value: { text: "可重建" },
          }),
          syncedChange({
            cursor: 2,
            mutationId: "snapshot-setting-op",
            id: "snapshot-setting",
            value: { enabled: true },
          }),
        ],
      };
    },
  };
  const coordinator = Coordinator.createSyncCoordinator({
    store,
    registry: DataRegistry,
    serverGateway: server,
  });

  const result = await coordinator.runOnce();
  assert.equal(result.server.resetRecovered, true);
  assert.equal(result.server.remoteCursor, 2);
  assert.equal(await store.get("translation-cache", "snapshot-cache"), null);
  assert.equal(
    (await store.get("user-settings", "snapshot-setting")).value.enabled,
    true,
  );
});

test("服务器只确认连续第一条时 localCursor 不越过未确认变化", async () => {
  const store = makeStore("partial-ack");
  await store.put("user-settings", { id: "one" }, { mutationId: "op-one" });
  await store.put("user-settings", { id: "two" }, { mutationId: "op-two" });
  let calls = 0;
  const server = {
    async push(request) {
      calls += 1;
      return {
        ...emptyPull(request),
        ackedMutationIds: calls === 1
          ? ["op-one"]
          : request.changes.map((change) => change.mutationId),
      };
    },
    async pull(request) { return emptyPull(request); },
  };
  const checkpoints = Coordinator.createMemoryCheckpointStore();
  const coordinator = Coordinator.createSyncCoordinator({
    store,
    registry: DataRegistry,
    serverGateway: server,
    checkpointStore: checkpoints,
  });

  const first = await coordinator.runOnce();
  assert.equal(first.server.localCursor, 1);
  assert.equal(first.server.pendingLocal, true);
  assert.equal(first.checkpoint.server.localCursor, 1);

  const second = await coordinator.runOnce();
  assert.equal(second.server.localCursor, 2);
  assert.equal(second.server.pendingLocal, false);
});

test("直连成功也必须尝试服务器备份，服务器失败时保留其游标", async () => {
  const store = makeStore("direct-server-fail");
  await store.put("user-settings", { id: "theme" }, { mutationId: "theme-op" });
  const directLog = [];
  const checkpoints = Coordinator.createMemoryCheckpointStore();
  const coordinator = Coordinator.createSyncCoordinator({
    store,
    registry: DataRegistry,
    serverGateway: {
      async push() {
        const error = new Error("server offline");
        error.code = "BW_SYNC_OFFLINE";
        error.retryable = true;
        throw error;
      },
      async pull(request) { return emptyPull(request); },
    },
    checkpointStore: checkpoints,
  });
  coordinator.addPeer("peer-b", ackingGateway(directLog), { baselineReady: true });

  const result = await coordinator.runOnce();
  assert.equal(result.direct["peer-b"].ok, true);
  assert.equal(result.serverBackupAttempted, true);
  assert.equal(result.server.ok, false);
  assert.equal(result.server.code, "BW_SYNC_OFFLINE");
  assert.equal(result.checkpoint.peers["peer-b"].sentCursor, 1);
  assert.equal(result.checkpoint.server.localCursor, 0);
  assert.equal(directLog.length, 1);
});

test("旧 checkpoint 的 baselineReady 不能授权新的未对齐 RTC 会话", async () => {
  const store = makeStore("direct-stale-baseline");
  await store.put(
    "user-settings",
    { id: "theme" },
    { mutationId: "theme-op" },
  );
  const peerCalls = [];
  const checkpoint = Coordinator.createMemoryCheckpointStore({
    contract: "sync-coordinator/1",
    schema: 1,
    registryDigest:
      "sync-v1:" + DataRegistry.syncCollections().join("|"),
    generation: 1,
    server: { localCursor: 0, remoteCursor: 0 },
    peers: {
      "peer-old": {
        sentCursor: 1,
        receivedCursor: 1,
        baselineReady: true,
      },
    },
  });
  const coordinator = Coordinator.createSyncCoordinator({
    store,
    registry: DataRegistry,
    serverGateway: ackingGateway(),
    checkpointStore: checkpoint,
  });
  coordinator.addPeer(
    "peer-old",
    ackingGateway(peerCalls),
    { baselineReady: false },
  );

  const result = await coordinator.runOnce();
  assert.equal(
    result.checkpoint.registryDigest,
    DataRegistry.syncDigest(),
    "sync-v1 checkpoint 必须自动失效并升级为当前 sync-v2 摘要",
  );
  assert.equal(result.direct["peer-old"].skipped, true);
  assert.equal(result.direct["peer-old"].code, "BW_SYNC_BASELINE_REQUIRED");
  assert.equal(peerCalls.length, 0);
});

test("精确 sync-v2 checkpoint 只迁移 server 游标并清空 peers，未知摘要重置", async () => {
  const initial = {
    contract: Coordinator.CONTRACT,
    schema: 1,
    registryDigest: LEGACY_SYNC_DIGEST,
    generation: 8,
    server: {
      localCursor: 17,
      remoteCursor: 23,
      reconciliationEpoch: 2,
    },
    peers: {
      obsolete: {
        sentCursor: 9,
        receivedCursor: 11,
        baselineReady: true,
      },
    },
  };
  const migratedCoordinator = Coordinator.createSyncCoordinator({
    store: makeStore("checkpoint-migrate"),
    registry: DataRegistry,
    serverGateway: ackingGateway(),
    checkpointStore: Coordinator.createMemoryCheckpointStore(initial),
  });
  const migrated = await migratedCoordinator.checkpoint();
  assert.equal(migrated.registryDigest, DataRegistry.syncDigest());
  assert.deepEqual(migrated.server, initial.server);
  assert.deepEqual(migrated.peers, {});

  const unknownCoordinator = Coordinator.createSyncCoordinator({
    store: makeStore("checkpoint-reset"),
    registry: DataRegistry,
    serverGateway: ackingGateway(),
    checkpointStore: Coordinator.createMemoryCheckpointStore({
      ...initial,
      registryDigest: "sync-v2:unknown:explicit:0:1",
    }),
  });
  const reset = await unknownCoordinator.checkpoint();
  assert.equal(reset.registryDigest, DataRegistry.syncDigest());
  assert.deepEqual(reset.server, {
    localCursor: 0,
    remoteCursor: 0,
    reconciliationEpoch: 0,
  });
  assert.deepEqual(reset.peers, {});
});

test("精确 v2 checkpoint 在任何 push 前按冻结 baseline 一次性补铸并先走 server", async () => {
  const backend = DataStore.createMemoryBackend();
  const legacy = DataStore.createDataStore({
    backend,
    deviceId: "legacy-coordinator",
    causalCollections: [],
  });
  const base = await legacy.put(
    "user-settings",
    { id: "theme", mode: "system" },
    { mutationId: "legacy-coordinator-base" },
  );
  await legacy.put(
    "user-settings",
    { id: "theme", mode: "dark" },
    { mutationId: "legacy-coordinator-child" },
  );
  const store = DataStore.createDataStore({
    backend,
    deviceId: "current-coordinator",
    causalCollections: DataRegistry.syncCollections(),
  });
  const checkpointStore = Coordinator.createMemoryCheckpointStore({
    contract: Coordinator.CONTRACT,
    schema: 1,
    registryDigest: LEGACY_SYNC_DIGEST,
    generation: 4,
    server: {
      localCursor: 1,
      remoteCursor: 1,
      reconciliationEpoch: 0,
    },
    peers: {
      retired: {
        sentCursor: 1,
        receivedCursor: 1,
        baselineReady: true,
      },
    },
  });
  const network = [];
  const server = {
    async snapshot(request) {
      network.push(["snapshot", structuredClone(request)]);
      return {
        snapshotId: "legacy-causal-baseline",
        snapshotCursor: 1,
        offset: request.offset,
        nextOffset: 1,
        hasMore: false,
        changes: [{
          cursor: 1,
          mutationId: "server-base",
          operation: "put",
          collection: "user-settings",
          record: structuredClone(base),
        }],
      };
    },
    async push(request) {
      network.push(["server-push", structuredClone(request)]);
      return {
        ...emptyPull(request),
        cursor: 1,
        headCursor: 2,
        ackedMutationIds: request.changes.map((change) => change.mutationId),
      };
    },
    async pull(request) {
      network.push(["server-pull", structuredClone(request)]);
      return { ...emptyPull(request), cursor: 1, headCursor: 2 };
    },
  };
  const directPushes = [];
  const coordinator = Coordinator.createSyncCoordinator({
    store,
    registry: DataRegistry,
    serverGateway: server,
    checkpointStore,
  });
  coordinator.addPeer(
    "ready-peer",
    ackingGateway(directPushes),
    {
      baselineReady: true,
      baselineLocalCursor: 1,
      baselineRemoteCursor: 1,
    },
  );

  const result = await coordinator.runOnce();
  assert.deepEqual(network.map((entry) => entry[0]), [
    "snapshot",
    "server-push",
    "server-pull",
  ]);
  assert.equal(directPushes.length, 0);
  assert.equal(
    result.direct["ready-peer"].code,
    "BW_SYNC_CAUSAL_MIGRATION_SERVER_FIRST",
  );
  assert.equal(result.causalMigration.ok, true);
  assert.equal(result.causalMigration.migrated, 1);
  assert.equal(result.causalMigration.baselineCursor, 1);
  const sent = network[1][1].changes;
  assert.deepEqual(sent.map((change) => change.mutationId), [
    "legacy-coordinator-child",
  ]);
  assert.deepEqual(
    sent[0].record.causal,
    DataStore.causalProofForParent(base),
  );
  assert.equal(result.checkpoint.registryDigest, DataRegistry.syncDigest());
  assert.equal(result.checkpoint.server.localCursor, 2);
  assert.deepEqual(result.checkpoint.peers, {});
  assert.equal(
    (await store.changes({ after: 1, limit: 10 }))
      .changes[0].record.causal.contract,
    "record-parent-state/1",
  );

  const restarted = Coordinator.createSyncCoordinator({
    store,
    registry: DataRegistry,
    serverGateway: server,
    checkpointStore,
  });
  const second = await restarted.runOnce();
  assert.equal(second.causalMigration, null);
  assert.equal(
    network.filter((entry) => entry[0] === "snapshot").length,
    1,
    "checkpoint 升级后不会重复读取迁移 baseline",
  );
  assert.equal(
    network.filter((entry) =>
      entry[0] === "server-push" && entry[1].changes.length > 0
    ).length,
    1,
    "重启不会重复上传已确认 legacy mutation",
  );
});

test("v2 baseline 游标已变化时迁移零写入、零 push 且不升级持久 checkpoint", async () => {
  const backend = DataStore.createMemoryBackend();
  const legacy = DataStore.createDataStore({
    backend,
    deviceId: "legacy-baseline-changed",
    causalCollections: [],
  });
  await legacy.put(
    "user-settings",
    { id: "theme", mode: "system" },
    { mutationId: "baseline-changed-base" },
  );
  await legacy.put(
    "user-settings",
    { id: "theme", mode: "dark" },
    { mutationId: "baseline-changed-child" },
  );
  const store = DataStore.createDataStore({
    backend,
    deviceId: "current-baseline-changed",
    causalCollections: DataRegistry.syncCollections(),
  });
  const checkpointStore = Coordinator.createMemoryCheckpointStore({
    contract: Coordinator.CONTRACT,
    schema: 1,
    registryDigest: LEGACY_SYNC_DIGEST,
    generation: 1,
    server: {
      localCursor: 1,
      remoteCursor: 1,
      reconciliationEpoch: 0,
    },
    peers: {},
  });
  let pushes = 0;
  const coordinator = Coordinator.createSyncCoordinator({
    store,
    registry: DataRegistry,
    checkpointStore,
    serverGateway: {
      async snapshot(request) {
        return {
          snapshotId: "advanced-baseline",
          snapshotCursor: 2,
          offset: request.offset,
          nextOffset: 0,
          hasMore: false,
          changes: [],
        };
      },
      async push(request) {
        pushes += 1;
        return emptyPull(request);
      },
      async pull(request) {
        return emptyPull(request);
      },
    },
  });

  const result = await coordinator.runOnce();
  assert.equal(result.causalMigration.ok, false);
  assert.equal(
    result.causalMigration.code,
    "BW_SYNC_CAUSAL_MIGRATION_BASELINE_CHANGED",
  );
  assert.equal(result.serverBackupAttempted, false);
  assert.equal(pushes, 0);
  assert.equal(
    "causal" in (await store.changes({ after: 1, limit: 10 }))
      .changes[0].record,
    false,
  );
  assert.equal(
    checkpointStore.inspect().registryDigest,
    LEGACY_SYNC_DIGEST,
  );
});

test("本地补铸后 checkpoint 保存中断可重试，失败轮绝不提前 push", async () => {
  const backend = DataStore.createMemoryBackend();
  const legacy = DataStore.createDataStore({
    backend,
    deviceId: "legacy-checkpoint-retry",
    causalCollections: [],
  });
  await legacy.put(
    "user-settings",
    { id: "theme", mode: "dark" },
    { mutationId: "checkpoint-retry-theme" },
  );
  const store = DataStore.createDataStore({
    backend,
    deviceId: "current-checkpoint-retry",
    causalCollections: DataRegistry.syncCollections(),
  });
  let persisted = {
    contract: Coordinator.CONTRACT,
    schema: 1,
    registryDigest: LEGACY_SYNC_DIGEST,
    generation: 1,
    server: {
      localCursor: 0,
      remoteCursor: 0,
      reconciliationEpoch: 0,
    },
    peers: {},
  };
  let saveCalls = 0;
  const checkpointStore = {
    async load() {
      return structuredClone(persisted);
    },
    async save(value) {
      saveCalls += 1;
      if (saveCalls === 1) throw new Error("simulated checkpoint interruption");
      persisted = structuredClone(value);
    },
  };
  let snapshots = 0;
  let nonEmptyPushes = 0;
  const server = {
    async snapshot(request) {
      snapshots += 1;
      return {
        snapshotId: "checkpoint-retry-empty-baseline",
        snapshotCursor: 0,
        offset: request.offset,
        nextOffset: 0,
        hasMore: false,
        changes: [],
      };
    },
    async push(request) {
      if (request.changes.length) nonEmptyPushes += 1;
      return {
        ...emptyPull(request),
        headCursor: request.changes.length ? 1 : 0,
        ackedMutationIds: request.changes.map((change) => change.mutationId),
      };
    },
    async pull(request) {
      return emptyPull(request);
    },
  };
  const coordinator = Coordinator.createSyncCoordinator({
    store,
    registry: DataRegistry,
    serverGateway: server,
    checkpointStore,
  });

  const interrupted = await coordinator.runOnce();
  assert.equal(interrupted.causalMigration.ok, false);
  assert.equal(interrupted.serverBackupAttempted, false);
  assert.equal(nonEmptyPushes, 0);
  assert.equal(persisted.registryDigest, LEGACY_SYNC_DIGEST);
  assert.equal(
    (await store.changes({ after: 0, limit: 10 }))
      .changes[0].record.causal.parent,
    null,
    "checkpoint 失败前的本地事务已完整提交",
  );

  const retried = await coordinator.runOnce();
  assert.equal(retried.causalMigration.ok, true);
  assert.equal(retried.causalMigration.migrated, 0);
  assert.equal(snapshots, 1, "重试验证已有 proof 后不再索取 baseline");
  assert.equal(nonEmptyPushes, 1);
  assert.equal(persisted.registryDigest, DataRegistry.syncDigest());
});

test("live baseline 用双方已被 server 覆盖的游标初始化新直连会话", async () => {
  const store = makeStore("direct-live-baseline");
  await store.put("user-settings", { id: "one" }, { mutationId: "one" });
  await store.put("user-settings", { id: "two" }, { mutationId: "two" });
  const calls = [];
  const direct = {
    async push(request) {
      calls.push(["push", structuredClone(request)]);
      return {
        ...emptyPull(request),
        ackedMutationIds: request.changes.map((change) => change.mutationId),
      };
    },
    async pull(request) {
      calls.push(["pull", structuredClone(request)]);
      return emptyPull(request);
    },
  };
  const coordinator = Coordinator.createSyncCoordinator({
    store,
    registry: DataRegistry,
    serverGateway: ackingGateway(),
  });
  coordinator.addPeer("peer-new-session", direct, {
    baselineReady: true,
    baselineLocalCursor: 2,
    baselineRemoteCursor: 5,
  });

  const result = await coordinator.runOnce();
  assert.deepEqual(calls[0][1].changes, []);
  assert.equal(calls[1][1].cursor, 5);
  assert.equal(
    result.checkpoint.peers["peer-new-session"].sentCursor,
    2,
  );
  assert.equal(
    result.checkpoint.peers["peer-new-session"].receivedCursor,
    5,
  );
});

test("直连入站写 journal，并在同一轮继续备份到服务器", async () => {
  const store = makeStore("direct-import");
  const remote = {
    cursor: 1,
    mutationId: "remote-theme",
    operation: "put",
    collection: "user-settings",
    record: {
      schema: 1,
      collection: "user-settings",
      id: "remote-theme",
      rev: 1,
      updatedAt: 10,
      updatedBy: "peer-b",
      deleted: false,
      value: { id: "remote-theme", mode: "dark" },
      causal: {
        contract: "record-parent-state/1",
        parent: null,
      },
    },
  };
  let directPulled = false;
  const direct = {
    async push(request) {
      return {
        ...emptyPull(request),
        ackedMutationIds: request.changes.map((change) => change.mutationId),
      };
    },
    async pull(request) {
      if (directPulled) return emptyPull(request);
      directPulled = true;
      return {
        ...emptyPull(request),
        cursor: 1,
        headCursor: 1,
        changes: [remote],
      };
    },
  };
  const serverPushes = [];
  const coordinator = Coordinator.createSyncCoordinator({
    store,
    registry: DataRegistry,
    serverGateway: ackingGateway(serverPushes),
  });
  coordinator.addPeer("peer-b", direct, { baselineReady: true });

  const result = await coordinator.runOnce();
  assert.equal(result.direct["peer-b"].applied, 1);
  assert.equal(result.server.ok, true);
  assert.deepEqual(
    serverPushes.flatMap((request) => request.changes.map((change) => change.mutationId)),
    ["remote-theme"],
  );
  assert.equal((await store.get("user-settings", "remote-theme")).value.mode, "dark");
});

test("服务器入站不写回本地 journal，下一轮不会回声上传", async () => {
  const store = makeStore("server-no-echo");
  const remote = {
    cursor: 1,
    mutationId: "from-server",
    operation: "put",
    collection: "user-settings",
    record: {
      schema: 1,
      collection: "user-settings",
      id: "server-setting",
      rev: 1,
      updatedAt: 11,
      updatedBy: "server-device",
      deleted: false,
      value: { id: "server-setting", enabled: true },
      causal: {
        contract: "record-parent-state/1",
        parent: null,
      },
    },
  };
  const pushes = [];
  let pullCount = 0;
  const server = {
    async push(request) {
      pushes.push(structuredClone(request.changes));
      return { ...emptyPull(request), ackedMutationIds: [] };
    },
    async pull(request) {
      pullCount += 1;
      if (pullCount > 1) return emptyPull(request);
      return {
        ...emptyPull(request),
        cursor: 1,
        headCursor: 1,
        changes: [remote],
      };
    },
  };
  const coordinator = Coordinator.createSyncCoordinator({
    store,
    registry: DataRegistry,
    serverGateway: server,
  });

  await coordinator.runOnce();
  await coordinator.runOnce();
  assert.deepEqual(pushes, [[], []]);
  assert.equal((await store.get("user-settings", "server-setting")).value.enabled, true);
  assert.equal((await store.changes({ after: 0 })).changes.length, 0);
});

test("入站 pending collection fail closed 且不推进远端游标", async () => {
  const store = makeStore("reject-pending");
  const server = {
    async push(request) {
      return { ...emptyPull(request), ackedMutationIds: [] };
    },
    async pull(request) {
      return {
        ...emptyPull(request),
        cursor: 1,
        headCursor: 1,
        changes: [{
          cursor: 1,
          mutationId: "bad-card",
          operation: "put",
          collection: "cards",
          record: {
            schema: 1,
            collection: "cards",
            id: "card-1",
            rev: 1,
            updatedAt: 1,
            updatedBy: "peer",
            deleted: false,
            value: { id: "card-1" },
          },
        }],
      };
    },
  };
  const coordinator = Coordinator.createSyncCoordinator({
    store,
    registry: DataRegistry,
    serverGateway: server,
  });
  const result = await coordinator.runOnce();
  assert.equal(result.server.ok, false);
  assert.equal(result.server.code, "BW_SYNC_COLLECTION");
  assert.equal(result.checkpoint.server.remoteCursor, 0);
  assert.equal(await store.get("cards", "card-1"), null);
});

test("本地 journal 被裁剪后以服务端快照和稳定全量 overlay 自动恢复", async () => {
  const store = DataStore.createDataStore({
    backend: DataStore.createMemoryBackend(),
    deviceId: "snapshot-local-gap",
    maxJournal: 100,
  });
  for (let index = 0; index < 101; index += 1) {
    await store.put(
      "user-settings",
      { id: `local-${String(index).padStart(3, "0")}`, value: index },
      { mutationId: `local-mutation-${index}` },
    );
  }
  assert.equal((await store.changes({ after: 0 })).resetRequired, true);

  const snapshotRecord = syncedChange({
    cursor: 7,
    mutationId: "server-only",
    id: "server-only",
    value: { mode: "server" },
  });
  const snapshotRequests = [];
  const pushedBatches = [];
  const server = {
    async snapshot(request) {
      snapshotRequests.push(structuredClone(request));
      return {
        snapshotId: "snapshot-gap",
        snapshotCursor: 7,
        offset: request.offset,
        nextOffset: request.offset + (request.offset === 0 ? 1 : 0),
        hasMore: false,
        changes: request.offset === 0 ? [snapshotRecord] : [],
      };
    },
    async push(request) {
      pushedBatches.push(structuredClone(request.changes));
      return {
        ...emptyPull({ cursor: 7 }),
        cursor: 7,
        headCursor: 7,
        ackedMutationIds: request.changes.map((change) => change.mutationId),
      };
    },
    async pull(request) {
      return {
        ...emptyPull(request),
        cursor: 7,
        headCursor: 7,
      };
    },
  };
  const checkpoint = Coordinator.createMemoryCheckpointStore();
  const coordinator = Coordinator.createSyncCoordinator({
    store,
    registry: DataRegistry,
    serverGateway: server,
    checkpointStore: checkpoint,
    limit: 20,
  });

  const result = await coordinator.runOnce();
  const overlay = pushedBatches.flat();
  assert.equal(result.server.ok, true);
  assert.equal(result.server.resetRecovered, true);
  assert.equal(result.server.reconciled, true);
  assert.equal(result.server.localCursor, 101);
  assert.equal(result.server.remoteCursor, 7);
  assert.equal(result.checkpoint.server.localCursor, 101);
  assert.equal(result.checkpoint.server.remoteCursor, 7);
  assert.equal(result.checkpoint.server.reconciliationEpoch, 1);
  assert.equal(snapshotRequests.length, 1);
  assert.equal(overlay.length, 102);
  assert.equal(
    new Set(overlay.map((change) => change.mutationId)).size,
    overlay.length,
  );
  assert.equal(
    overlay.every((change) =>
      /^snapshot-overlay-v1-[0-9a-f]{64}$/.test(change.mutationId)),
    true,
  );
  assert.equal(
    (await store.get("user-settings", "server-only")).value.mode,
    "server",
  );
});

test("完整 overlay 使用 id keyset，分页中本地更新不会漏掉未读旧记录", async () => {
  const base = DataStore.createDataStore({
    backend: DataStore.createMemoryBackend(),
    deviceId: "snapshot-concurrent-write",
    maxJournal: 100,
  });
  for (let index = 0; index < 101; index += 1) {
    const id = `r${String(index).padStart(3, "0")}`;
    await base.put(
      "user-settings",
      { id, value: index },
      { mutationId: `initial-${id}` },
    );
  }
  let firstList = true;
  const store = {
    get: (...args) => base.get(...args),
    put: (...args) => base.put(...args),
    list: async (...args) => {
      const records = await base.list(...args);
      if (firstList && records.length) {
        firstList = false;
        await base.put(
          "user-settings",
          { id: "r002", value: "updated-during-snapshot" },
          { mutationId: "concurrent-r002" },
        );
      }
      return records;
    },
    changes: (...args) => base.changes(...args),
    applyChanges: (...args) => base.applyChanges(...args),
    status: (...args) => base.status(...args),
  };
  const serverRecords = new Map();
  const pushes = [];
  const server = {
    async snapshot(request) {
      return {
        snapshotId: "snapshot-keyset",
        snapshotCursor: 0,
        offset: request.offset,
        nextOffset: request.offset,
        hasMore: false,
        changes: [],
      };
    },
    async push(request) {
      pushes.push(structuredClone(request.changes));
      for (const change of request.changes) {
        serverRecords.set(
          `${change.collection}/${change.record.id}`,
          structuredClone(change.record),
        );
      }
      return {
        ...emptyPull(request),
        ackedMutationIds: request.changes.map((change) => change.mutationId),
      };
    },
    async pull(request) { return emptyPull(request); },
  };
  const checkpoint = Coordinator.createMemoryCheckpointStore();
  const coordinator = Coordinator.createSyncCoordinator({
    store,
    registry: DataRegistry,
    serverGateway: server,
    checkpointStore: checkpoint,
    limit: 2,
  });

  const recovered = await coordinator.runOnce();
  assert.equal(recovered.server.resetRecovered, true);
  assert.equal(recovered.checkpoint.server.localCursor, 101);
  assert.equal(recovered.server.pendingLocal, true);
  assert.equal(serverRecords.size, 101);
  assert.equal(
    serverRecords.get("user-settings/r002").value.value,
    "updated-during-snapshot",
  );

  const incremental = await coordinator.runOnce();
  assert.equal(incremental.checkpoint.server.localCursor, 102);
  assert.equal(serverRecords.size, 101);
  assert.equal(
    pushes.flat().some((change) => change.mutationId === "concurrent-r002"),
    true,
  );
});

test("远端 reset 后快照仅凭有效父业务状态覆盖本地并完成 catch-up", async () => {
  const store = makeStore("snapshot-remote-reset");
  await store.put(
    "user-settings",
    { id: "theme", mode: "dark" },
    { mutationId: "local-theme" },
  );
  const remote = syncedChange({
    cursor: 9,
    mutationId: "server-theme",
    id: "theme",
    rev: 2,
    value: { mode: "light" },
    updatedAt: 9,
    parent: {
      deleted: false,
      value: { id: "theme", mode: "dark" },
    },
  });
  let snapshotCalls = 0;
  const server = {
    async push(request) {
      if (
        request.changes.some((change) =>
          !change.mutationId.startsWith("snapshot-overlay-v1-"))
      ) {
        return {
          ...emptyPull(request),
          resetRequired: true,
        };
      }
      return {
        ...emptyPull({ cursor: 9 }),
        cursor: 9,
        headCursor: 9,
        ackedMutationIds: request.changes.map((change) => change.mutationId),
      };
    },
    async snapshot(request) {
      snapshotCalls += 1;
      return {
        snapshotId: "snapshot-remote",
        snapshotCursor: 9,
        offset: request.offset,
        nextOffset: 1,
        hasMore: false,
        changes: [remote],
      };
    },
    async pull(request) {
      return {
        ...emptyPull(request),
        cursor: 9,
        headCursor: 9,
      };
    },
  };
  const coordinator = Coordinator.createSyncCoordinator({
    store,
    registry: DataRegistry,
    serverGateway: server,
  });

  const result = await coordinator.runOnce();
  assert.equal(snapshotCalls, 1);
  assert.equal(result.server.resetPhase, "push");
  assert.equal(result.server.resetRecovered, true);
  assert.equal(result.checkpoint.server.localCursor, 1);
  assert.equal(result.checkpoint.server.remoteCursor, 9);
  assert.equal(result.checkpoint.server.reconciliationEpoch, 1);
  assert.equal((await store.get("user-settings", "theme")).rev, 2);
  assert.equal((await store.get("user-settings", "theme")).value.mode, "light");
});

test("认证旧快照只为空 key 建立 proofless baseline，同值可收敛而分叉保持冲突", async () => {
  async function runCase(name, localValue) {
    const store = makeStore(`legacy-snapshot-${name}`);
    if (localValue != null) {
      await store.put(
        "user-settings",
        { id: "legacy-theme", mode: localValue },
        { mutationId: `${name}-local` },
      );
    }
    const remote = syncedChange({
      cursor: 6,
      mutationId: `${name}-legacy-remote`,
      id: "legacy-theme",
      rev: 6,
      value: { mode: "dark" },
      updatedAt: 6,
    });
    delete remote.record.causal;
    let overlayPushes = 0;
    const server = {
      async push(request) {
        const overlay = request.changes.filter((change) =>
          change.mutationId.startsWith("snapshot-overlay-v1-"));
        if (!overlay.length) {
          return { ...emptyPull(request), resetRequired: true };
        }
        overlayPushes += overlay.length;
        return {
          ...emptyPull({ cursor: 6 }),
          cursor: 6,
          headCursor: 6,
          ackedMutationIds: overlay.map((change) => change.mutationId),
        };
      },
      async snapshot(request) {
        return {
          snapshotId: `legacy-snapshot-${name}`,
          snapshotCursor: 6,
          offset: request.offset,
          nextOffset: 1,
          hasMore: false,
          changes: [remote],
        };
      },
      async pull(request) {
        return { ...emptyPull(request), cursor: 6, headCursor: 6 };
      },
    };
    const coordinator = Coordinator.createSyncCoordinator({
      store,
      registry: DataRegistry,
      serverGateway: server,
    });
    return {
      store,
      result: await coordinator.runOnce(),
      overlayPushes: () => overlayPushes,
    };
  }

  const cold = await runCase("cold", null);
  assert.equal(cold.result.server.resetRecovered, true);
  assert.equal(
    (await cold.store.get("user-settings", "legacy-theme")).value.mode,
    "dark",
  );

  const same = await runCase("same", "dark");
  assert.equal(same.result.server.resetRecovered, true);
  assert.equal(
    (await same.store.get("user-settings", "legacy-theme")).rev,
    6,
  );

  const divergent = await runCase("divergent", "light");
  assert.equal(divergent.result.server.reconciled, false);
  assert.equal(
    divergent.result.server.conflicts[0].reason,
    "causal-proof-missing",
  );
  assert.equal(divergent.overlayPushes(), 0);
  assert.equal(
    (await divergent.store.get("user-settings", "legacy-theme")).value.mode,
    "light",
  );
});

test("服务端 absent 时 snapshot overlay 保留真实本地 proof，不伪造 root parent", async () => {
  const store = makeStore("snapshot-local-chain");
  await store.put(
    "user-settings",
    { id: "local-chain", value: "base" },
    { mutationId: "local-chain-base" },
  );
  const final = await store.put(
    "user-settings",
    { id: "local-chain", value: "final" },
    { mutationId: "local-chain-final" },
  );
  let observedOverlay = null;
  const server = {
    async push(request) {
      const overlay = request.changes.find((change) =>
        change.mutationId.startsWith("snapshot-overlay-v1-"));
      if (!overlay) return { ...emptyPull(request), resetRequired: true };
      observedOverlay = structuredClone(overlay);
      return {
        ...emptyPull(request),
        conflicts: [{
          mutationId: overlay.mutationId,
          collection: overlay.collection,
          id: overlay.record.id,
          reason: "causal-parent-mismatch",
        }],
      };
    },
    async snapshot(request) {
      return {
        snapshotId: "snapshot-local-chain",
        snapshotCursor: 0,
        offset: request.offset,
        nextOffset: 0,
        hasMore: false,
        changes: [],
      };
    },
    async pull(request) { return emptyPull(request); },
  };
  const coordinator = Coordinator.createSyncCoordinator({
    store,
    registry: DataRegistry,
    serverGateway: server,
  });
  const result = await coordinator.runOnce();
  assert.equal(result.server.reconciled, false);
  assert.equal(result.server.conflicts[0].reason, "causal-parent-mismatch");
  assert.deepEqual(
    observedOverlay.record.causal,
    final.causal,
    "snapshot 不得把服务端未见过的本地中间态伪造成 parent:null",
  );
  assert.equal((await store.get("user-settings", "local-chain")).value.value, "final");
});

test("快照分叉直接保持显式冲突且不把本地值静默 overlay 到服务端", async () => {
  const store = makeStore("snapshot-conflict");
  await store.put(
    "user-settings",
    { id: "theme", mode: "dark" },
    { mutationId: "local-theme" },
  );
  const remote = syncedChange({
    cursor: 4,
    mutationId: "server-theme",
    id: "theme",
    rev: 1,
    value: { mode: "light" },
    updatedAt: 4,
  });
  const conflictMutationIds = [];
  let snapshotGeneration = 0;
  const server = {
    async push(request) {
      const synthetic = request.changes.filter((change) =>
        change.mutationId.startsWith("snapshot-overlay-v1-"));
      if (!synthetic.length) {
        return { ...emptyPull(request), resetRequired: true };
      }
      conflictMutationIds.push(synthetic[0].mutationId);
      return {
        ...emptyPull({ cursor: 4 }),
        cursor: 4,
        headCursor: 4,
        conflicts: [{
          mutationId: synthetic[0].mutationId,
          collection: "user-settings",
          id: "theme",
          reason: "same-rev-different-value",
        }],
      };
    },
    async snapshot(request) {
      snapshotGeneration += 1;
      return {
        snapshotId: `snapshot-conflict-${snapshotGeneration}`,
        snapshotCursor: 4,
        offset: request.offset,
        nextOffset: 1,
        hasMore: false,
        changes: [remote],
      };
    },
    async pull(request) { return emptyPull(request); },
  };
  const checkpoint = Coordinator.createMemoryCheckpointStore();
  const coordinator = Coordinator.createSyncCoordinator({
    store,
    registry: DataRegistry,
    serverGateway: server,
    checkpointStore: checkpoint,
  });

  const first = await coordinator.runOnce();
  const second = await coordinator.runOnce();
  assert.equal(first.server.ok, true);
  assert.equal(first.server.reconciled, false);
  assert.equal(first.server.conflicts[0].reason, "causal-parent-mismatch");
  assert.equal(first.checkpoint.server.localCursor, 0);
  assert.equal(first.checkpoint.server.remoteCursor, 0);
  assert.equal(first.checkpoint.server.reconciliationEpoch, 0);
  assert.equal(second.checkpoint.server.localCursor, 0);
  assert.equal(second.checkpoint.server.remoteCursor, 0);
  assert.equal(conflictMutationIds.length, 0,
    "服务端快照与本地分叉时不得继续上传本地 overlay");
  assert.equal((await store.get("user-settings", "theme")).value.mode, "dark");
});

test("远端 head 变化仍不得覆盖本地墓碑或生成本地 winner overlay", async () => {
  const store = makeStore("snapshot-remote-fingerprint");
  const live = await store.put(
    "user-settings",
    { id: "retired-setting", value: "local" },
    { mutationId: "local-live" },
  );
  await store.remove(
    "user-settings",
    "retired-setting",
    { ifRev: live.rev, mutationId: "local-delete" },
  );
  let generation = 0;
  const mutationIds = [];
  const server = {
    async push(request) {
      const synthetic = request.changes.filter((change) =>
        change.mutationId.startsWith("snapshot-overlay-v1-"));
      if (!synthetic.length) {
        return { ...emptyPull(request), resetRequired: true };
      }
      mutationIds.push(synthetic[0].mutationId);
      return {
        ...emptyPull({ cursor: generation + 3 }),
        cursor: generation + 3,
        headCursor: generation + 3,
        conflicts: [{
          mutationId: synthetic[0].mutationId,
          collection: "user-settings",
          id: "retired-setting",
          reason: "stale-incoming",
        }],
      };
    },
    async snapshot(request) {
      generation += 1;
      const remoteCursor = generation + 3;
      return {
        snapshotId: `snapshot-remote-head-${generation}`,
        snapshotCursor: remoteCursor,
        offset: request.offset,
        nextOffset: 1,
        hasMore: false,
        changes: [syncedChange({
          cursor: remoteCursor,
          mutationId: `remote-live-${generation}`,
          id: "retired-setting",
          rev: generation + 2,
          value: { value: `remote-${generation}` },
          updatedAt: generation + 2,
        })],
      };
    },
    async pull(request) { return emptyPull(request); },
  };
  const coordinator = Coordinator.createSyncCoordinator({
    store,
    registry: DataRegistry,
    serverGateway: server,
  });

  await coordinator.runOnce();
  await coordinator.runOnce();
  assert.equal(mutationIds.length, 0);
  assert.equal(
    (await store.get(
      "user-settings",
      "retired-setting",
      { includeDeleted: true },
    )).deleted,
    true,
  );
});

test("远端分页出现冲突时不越过该页游标，重启后仍可重新观察", async () => {
  const store = makeStore("pull-conflict-cursor");
  await store.put(
    "user-settings",
    { id: "theme", mode: "dark" },
    { mutationId: "local-theme" },
  );
  const remote = syncedChange({
    cursor: 3,
    mutationId: "remote-theme",
    id: "theme",
    rev: 1,
    value: { mode: "light" },
  });
  const server = {
    async push(request) {
      return {
        ...emptyPull(request),
        ackedMutationIds: request.changes.map((change) => change.mutationId),
      };
    },
    async pull(request) {
      return {
        ...emptyPull(request),
        cursor: 3,
        headCursor: 3,
        changes: [remote],
      };
    },
  };
  const checkpoint = Coordinator.createMemoryCheckpointStore();
  const coordinator = Coordinator.createSyncCoordinator({
    store,
    registry: DataRegistry,
    serverGateway: server,
    checkpointStore: checkpoint,
  });

  const result = await coordinator.runOnce();
  assert.equal(result.server.conflicts.length, 1);
  assert.equal(result.server.remoteCursor, 0);
  assert.equal(result.checkpoint.server.remoteCursor, 0);
  assert.equal((await store.get("user-settings", "theme")).value.mode, "dark");
});

class FakeChannel {
  constructor() {
    this.readyState = "open";
    this.listeners = new Set();
    this.peer = null;
  }
  addEventListener(type, listener) {
    if (type === "message") this.listeners.add(listener);
  }
  removeEventListener(type, listener) {
    if (type === "message") this.listeners.delete(listener);
  }
  send(data) {
    const peer = this.peer;
    queueMicrotask(() => {
      for (const listener of peer.listeners) listener({ data });
    });
  }
}

function channelPair() {
  const left = new FakeChannel();
  const right = new FakeChannel();
  left.peer = right;
  right.peer = left;
  return [left, right];
}

test("RTCDataChannel 协议只传 gateway payload，远端持久成功后才 ACK", async () => {
  const leftStore = makeStore("left");
  const rightStore = makeStore("right");
  await leftStore.put(
    "user-settings",
    { id: "shared-setting", language: "ja" },
    { mutationId: "left-setting" },
  );
  const [leftChannel, rightChannel] = channelPair();
  const common = {
    sessionId: "session-1",
    accountProof: "account-proof",
    registryDigest: DataRegistry.syncDigest(),
  };
  const leftTransport = Direct.createChannelTransport({
    ...common,
    channel: leftChannel,
    relay: Direct.createStoreRelay({ store: leftStore, registry: DataRegistry }),
  });
  const rightTransport = Direct.createChannelTransport({
    ...common,
    channel: rightChannel,
    relay: Direct.createStoreRelay({ store: rightStore, registry: DataRegistry }),
  });
  const leftGateway = SyncGateway.createSyncGateway({
    transport: leftTransport,
    deviceId: "left-device",
  });
  const rightGateway = SyncGateway.createSyncGateway({
    transport: rightTransport,
    deviceId: "right-device",
  });
  const batch = await leftStore.changes({ after: 0 });
  const pushed = await leftGateway.push({ cursor: 0, changes: batch.changes });
  assert.deepEqual(pushed.ackedMutationIds, ["left-setting"]);
  assert.equal(
    (await rightStore.get("user-settings", "shared-setting")).value.language,
    "ja",
  );
  const journal = await rightStore.changes({ after: 0 });
  assert.deepEqual(journal.changes.map((change) => change.mutationId), ["left-setting"]);
  leftTransport.close();
  rightTransport.close();
  void rightGateway;
});
