import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import {
  DataRegistry,
  DataStore,
  StorageRouter,
  makeRegistry,
  makeStore,
} from "./helpers.mjs";

const ROOT = new URL("../../", import.meta.url);
const DATA_STORE_SOURCE = readFileSync(
  new URL("_server_deploy/static/reader-runtime/data-store.js", ROOT),
  "utf8",
);
const INDEXEDDB_STORE_SOURCE = readFileSync(
  new URL("_server_deploy/static/reader-runtime/indexeddb-store.js", ROOT),
  "utf8",
);

function abortableBatchIndexedDB() {
  const keyPaths = {
    records: "pk",
    journal: "cursor",
    mutations: "mutationId",
    meta: "key",
  };
  const values = Object.fromEntries(
    Object.keys(keyPaths).map((name) => [name, new Map()]),
  );
  values.meta.set("schema", { key: "schema", value: "data-store-schema/1" });
  values.meta.set("cursor", { key: "cursor", value: 0 });
  values.meta.set("instanceEpoch", {
    key: "instanceEpoch",
    value: "data-store-instance-v1-" + "a".repeat(32),
  });
  let hangMutationRead = false;
  let hangRecordRead = false;
  let abortCount = 0;

  class Transaction {
    constructor(storeNames) {
      this.storeNames = storeNames;
      this.active = true;
      this.pending = 0;
      this.completionGeneration = 0;
      this.error = null;
      this.oncomplete = null;
      this.onabort = null;
      this.onerror = null;
    }

    maybeComplete() {
      if (!this.active || this.pending !== 0) return;
      const generation = ++this.completionGeneration;
      setImmediate(() => {
        if (!this.active || this.pending !== 0 ||
            generation !== this.completionGeneration) return;
        this.active = false;
        this.oncomplete?.();
      });
    }

    schedule(operation) {
      if (!this.active) throw new Error("transaction inactive");
      const request = { result: undefined, error: null, onsuccess: null, onerror: null };
      this.pending += 1;
      this.completionGeneration += 1;
      setImmediate(() => {
        if (!this.active) return;
        try {
          request.result = operation();
        } catch (error) {
          request.error = error;
          this.error = error;
        }
        this.pending -= 1;
        if (request.error) {
          request.onerror?.();
          this.abort();
          return;
        }
        request.onsuccess?.();
        this.maybeComplete();
      });
      return request;
    }

    objectStore(name) {
      if (!this.active || !this.storeNames.includes(name)) {
        throw new Error("transaction inactive");
      }
      const data = values[name];
      const keyPath = keyPaths[name];
      return {
        get: (key) => {
          // 按住一次 records 读，用来模拟真实 WebKit 上挂住不 settle 的 readonly
          // 事务：它会一直占着 object store，后面的读写全排在它后面。
          if (name === "records" && hangRecordRead) {
            hangRecordRead = false;
            this.pending += 1;
            this.completionGeneration += 1;
            return { result: undefined, error: null, onsuccess: null, onerror: null };
          }
          if (name === "mutations" && hangMutationRead) {
            hangMutationRead = false;
            this.pending += 1;
            this.completionGeneration += 1;
            return { result: undefined, error: null, onsuccess: null, onerror: null };
          }
          return this.schedule(() => data.get(key));
        },
        put: (value) => this.schedule(() => {
          const key = value[keyPath];
          data.set(key, value);
          return key;
        }),
        count: () => this.schedule(() => data.size),
        index: () => ({
          openCursor: () => this.schedule(() => null),
        }),
        openCursor: () => this.schedule(() => null),
      };
    }

    abort() {
      if (!this.active) throw new Error("transaction inactive");
      this.active = false;
      abortCount += 1;
      setImmediate(() => this.onabort?.());
    }
  }

  const database = {
    objectStoreNames: { contains: (name) => Object.hasOwn(keyPaths, name) },
    onversionchange: null,
    close() {},
    transaction(storeNames) {
      return new Transaction(Array.from(storeNames));
    },
  };
  return {
    factory: {
      open() {
        const request = {
          result: database,
          transaction: null,
          error: null,
          onsuccess: null,
          onerror: null,
          onblocked: null,
          onupgradeneeded: null,
        };
        setImmediate(() => request.onsuccess?.());
        return request;
      },
    },
    hangNextMutationRead() { hangMutationRead = true; },
    hangNextRecordRead() { hangRecordRead = true; },
    abortCount: () => abortCount,
  };
}

function makeWebStorageStore(name, options = {}) {
  const values = new Map();
  const storage = {
    setCalls: 0,
    getItem(key) {
      return values.has(key) ? values.get(key) : null;
    },
    setItem(key, value) {
      this.setCalls += 1;
      values.set(key, String(value));
    },
  };
  return {
    storage,
    store: DataStore.createDataStore({
      backend: DataStore.createWebStorageBackend(storage, `test-${name}`),
      deviceId: `web-storage-${name}`,
      causalCollections: options.causalCollections || [],
    }),
  };
}

function contractStores(name, options = {}) {
  return [
    {
      backend: "memory",
      store: DataStore.createDataStore({
        backend: DataStore.createMemoryBackend(),
        deviceId: `memory-${name}`,
        causalCollections: options.causalCollections || [],
      }),
    },
    {
      backend: "web-storage",
      ...makeWebStorageStore(name, options),
    },
  ];
}

function causal(parent) {
  return DataStore.causalProofForParent(parent || null);
}

function legacyMigrationBackends(name) {
  const memoryBackend = DataStore.createMemoryBackend();
  const values = new Map();
  const storage = {
    getItem(key) {
      return values.has(key) ? values.get(key) : null;
    },
    setItem(key, value) {
      values.set(key, String(value));
    },
  };
  const webBackend = DataStore.createWebStorageBackend(
    storage,
    `legacy-migration-${name}`,
  );
  return [
    {
      backend: "memory",
      open(causalCollections) {
        return DataStore.createDataStore({
          backend: memoryBackend,
          deviceId: `memory-${name}`,
          causalCollections,
        });
      },
    },
    {
      backend: "web-storage",
      open(causalCollections) {
        return DataStore.createDataStore({
          backend: webBackend,
          deviceId: `web-${name}`,
          causalCollections,
        });
      },
    },
  ];
}

test("IndexedDB bounded batch aborts a hung transaction and later writes still settle", async () => {
  const fake = abortableBatchIndexedDB();
  const context = {
    console,
    indexedDB: fake.factory,
    IDBKeyRange: {
      only: (value) => value,
      lowerBound: (value) => value,
    },
    navigator: { userAgent: "bounded-idb-contract" },
    crypto: {
      getRandomValues(bytes) {
        bytes.fill(3);
        return bytes;
      },
    },
    structuredClone,
    setTimeout,
    clearTimeout,
    setImmediate,
  };
  context.globalThis = context;
  vm.runInNewContext(DATA_STORE_SOURCE, context, { filename: "data-store.js" });
  vm.runInNewContext(INDEXEDDB_STORE_SOURCE, context, { filename: "indexeddb-store.js" });
  const store = context.BWReaderRuntime.indexedDBStore.createIndexedDBDataStore({
    dbName: "bounded-batch-timeout",
    deviceId: "bounded-idb-test",
    broadcast: false,
    webkitTransactionKeepalive: false,
  });

  fake.hangNextMutationRead();
  const hungMutations = vm.runInNewContext(`(${JSON.stringify([{
    collection: "document-state",
    value: { id: "hung", value: 1 },
    mutationId: "hung-batch",
  }])})`, context);
  const timeoutOptions = vm.runInNewContext("({ transactionTimeoutMs: 10 })", context);
  const hung = store.batch(hungMutations, timeoutOptions);
  await assert.rejects(
    Promise.race([
      hung,
      new Promise((_, reject) => setTimeout(
        () => reject(new Error("bounded batch did not settle")),
        500,
      )),
    ]),
    (error) => error.code === "BW_DATA_TIMEOUT" &&
      error.details?.timeoutMs === 10,
  );
  assert.equal(fake.abortCount(), 1, "timeout must abort the real IDB transaction");

  const laterMutations = vm.runInNewContext(`(${JSON.stringify([{
    collection: "document-state",
    value: { id: "later", value: 2 },
    mutationId: "later-batch",
  }])})`, context);
  const later = await store.batch(laterMutations);
  assert.equal(later.length, 1);
  assert.equal((await store.get("document-state", "later")).value.value, 2);
  assert.equal(fake.abortCount(), 1, "ordinary batches keep their existing no-timeout behavior");
  store.close();
});

test("DataStore 用稳定 ID 更新同一张卡，不创建视觉副本", async () => {
  const store = makeStore("stable");
  const first = await store.put(
    "cards",
    { prompt: "Q", answer: "A" },
    { id: "card_001", identityFields: ["cid", "gid"], mutationId: "m-put-1" },
  );
  assert.equal(first.id, "card_001");
  assert.equal(first.value.id, "card_001");
  assert.equal(first.value.cid, "card_001");
  assert.equal(first.value.gid, "card_001");

  const second = await store.put(
    "cards",
    { id: "card_001", cid: "card_001", gid: "card_001", prompt: "Q", answer: "A2" },
    { ifRev: first.rev, mutationId: "m-put-2" },
  );
  assert.equal(second.rev, first.rev + 1);
  assert.equal((await store.list("cards")).length, 1);
  assert.equal((await store.get("cards", "card_001")).value.answer, "A2");
});

test("list 的 id keyset 分页不受 updatedAt 变化影响", async () => {
  const store = makeStore("id-keyset");
  for (const id of ["r001", "r002", "r003", "r004"]) {
    await store.put("user-settings", { id }, { mutationId: `put-${id}` });
  }
  const first = await store.list("user-settings", {
    orderBy: "id",
    afterId: "",
    limit: 2,
  });
  await store.put(
    "user-settings",
    { id: "r003", changed: true },
    { mutationId: "move-r003" },
  );
  const second = await store.list("user-settings", {
    orderBy: "id",
    afterId: first.at(-1).id,
    limit: 2,
  });
  assert.deepEqual(
    [...first, ...second].map((record) => record.id),
    ["r001", "r002", "r003", "r004"],
  );
  assert.equal(second[0].value.changed, true);
});

test("mutationId 重放幂等，冲突不盲覆盖", async () => {
  const store = makeStore("idempotent");
  const one = await store.put("cards", { id: "c1", value: 1 }, { mutationId: "same-op" });
  const replay = await store.put("cards", { id: "c1", value: 999 }, { mutationId: "same-op" });
  assert.deepEqual(replay, one);
  assert.equal((await store.get("cards", "c1")).value.value, 1);

  await assert.rejects(
    store.put("cards", { id: "c1", value: 2 }, { ifRev: 0, mutationId: "conflict-op" }),
    (error) => error.code === "BW_DATA_CONFLICT",
  );
  assert.equal((await store.get("cards", "c1")).value.value, 1);
});

test("删除写墓碑，旧远端记录不能复活", async () => {
  const store = makeStore("tombstone");
  const put = await store.put("cards", { id: "c2", value: 1 }, { mutationId: "create-c2" });
  const removed = await store.remove("cards", "c2", { ifRev: put.rev, mutationId: "remove-c2" });
  assert.equal(removed.deleted, true);
  assert.equal(await store.get("cards", "c2"), null);
  assert.equal((await store.get("cards", "c2", { includeDeleted: true })).deleted, true);

  const applied = await store.applyChanges([{
    collection: "cards",
    mutationId: "stale-remote",
    record: put,
  }]);
  assert.equal(applied.applied.length, 0);
  assert.equal(applied.conflicts[0].reason, "stale-incoming");
  assert.equal((await store.get("cards", "c2", { includeDeleted: true })).deleted, true);
});

test("applyChanges 按业务值判断相等，忽略传输元数据与对象键顺序", async () => {
  for (const { backend, store } of contractStores("semantic-equality", {
    causalCollections: ["cards"],
  })) {
    const live = {
      schema: 1,
      collection: "cards",
      id: "semantic-live",
      rev: 1,
      updatedAt: 100,
      updatedBy: "device-a",
      deleted: false,
      value: {
        id: "semantic-live",
        nested: { alpha: 1, beta: 2 },
        tags: ["one", "two"],
      },
      causal: causal(null),
    };
    const first = await store.applyChanges([{
      collection: "cards",
      mutationId: `${backend}-semantic-seed`,
      record: live,
    }]);
    assert.equal(first.applied.length, 1);

    const reorderedSameRevision = {
      updatedBy: "device-b",
      deleted: false,
      id: "semantic-live",
      collection: "cards",
      updatedAt: 999,
      rev: 1,
      schema: 1,
      value: {
        tags: ["one", "two"],
        nested: { beta: 2, alpha: 1 },
        id: "semantic-live",
      },
    };
    const equivalent = await store.applyChanges([{
      collection: "cards",
      mutationId: `${backend}-semantic-equivalent`,
      record: reorderedSameRevision,
    }]);
    assert.equal(equivalent.applied.length, 0);
    assert.equal(equivalent.conflicts.length, 0,
      `${backend} 不应把相同业务值的元数据差异报告为冲突`);

    const different = await store.applyChanges([{
      collection: "cards",
      mutationId: `${backend}-semantic-different`,
      record: {
        ...reorderedSameRevision,
        value: {
          id: "semantic-live",
          nested: { alpha: 1, beta: 3 },
          tags: ["one", "two"],
        },
        causal: causal({
          rev: 4,
          deleted: false,
          value: { id: "semantic-live", nested: { alpha: 0 }, tags: [] },
        }),
      },
    }]);
    assert.equal(different.applied.length, 0);
    assert.equal(different.conflicts[0].reason, "causal-parent-mismatch",
      `${backend} 仍须保留不基于当前业务状态的真实分支冲突`);

    const higherSameValue = await store.applyChanges([{
      collection: "cards",
      mutationId: `${backend}-semantic-higher`,
      record: {
        ...reorderedSameRevision,
        rev: 2,
      },
    }]);
    assert.equal(higherSameValue.applied.length, 1,
      `${backend} 相同业务值的更高 revision 应推进共同基线`);
    assert.equal(
      (await store.get("cards", "semantic-live", { includeDeleted: true })).rev,
      2,
    );

    const converged = await store.get("cards", "semantic-live");
    const legitimateChild = {
      ...reorderedSameRevision,
      rev: 3,
      value: {
        id: "semantic-live",
        nested: { alpha: 1, beta: 4 },
        tags: ["one", "two"],
      },
      causal: causal(converged),
    };
    const childResult = await store.applyChanges([{
      collection: "cards",
      mutationId: `${backend}-semantic-child`,
      record: legitimateChild,
    }]);
    assert.equal(childResult.applied.length, 1,
      `${backend} 应以父业务状态而不是设备本地 revision 决定顺序`);
    assert.equal(
      (await store.get("cards", "semantic-live")).value.nested.beta,
      4,
    );

    const tombstone = {
      ...live,
      id: "semantic-deleted",
      rev: 1,
      deleted: true,
      value: { id: "semantic-deleted", retained: "device-a" },
      causal: causal(null),
    };
    await store.applyChanges([{
      collection: "cards",
      mutationId: `${backend}-tombstone-seed`,
      record: tombstone,
    }]);
    const equivalentTombstone = await store.applyChanges([{
      collection: "cards",
      mutationId: `${backend}-tombstone-equivalent`,
      record: {
        ...tombstone,
        updatedAt: 5000,
        updatedBy: "device-b",
        value: { id: "semantic-deleted", retained: "different-local-copy" },
      },
    }]);
    assert.equal(equivalentTombstone.applied.length, 0);
    assert.equal(equivalentTombstone.conflicts.length, 0,
      `${backend} 两个同编号墓碑应视为相同业务状态`);
  }
});

test("墓碑后只接受精确父状态的线性续写，无/错父证明仍由墓碑支配", async () => {
  for (const { backend, store } of contractStores("tombstone-dominates", {
    causalCollections: ["cards"],
  })) {
    const defaultLive = await store.put(
      "cards",
      { id: "default-revive", value: "live" },
      { mutationId: `${backend}-default-live` },
    );
    const defaultRemoved = await store.remove(
      "cards",
      defaultLive.id,
      { ifRev: defaultLive.rev, mutationId: `${backend}-default-remove` },
    );
    const defaultRevive = {
      ...defaultRemoved,
      rev: defaultRemoved.rev + 1,
      updatedAt: defaultRemoved.updatedAt + 10,
      updatedBy: "remote",
      deleted: false,
      value: { id: defaultLive.id, value: "remote-active" },
      causal: causal(defaultRemoved),
    };
    const defaultResult = await store.applyChanges([{
      collection: "cards",
      mutationId: `${backend}-default-revive`,
      record: defaultRevive,
    }]);
    assert.equal(defaultResult.applied.length, 1,
      `${backend} 默认行为允许明确以墓碑为父状态的活动记录`);
    assert.equal((await store.get("cards", defaultLive.id)).value.value, "remote-active");

    const guardedLive = await store.put(
      "cards",
      { id: "guarded-revive", value: "live" },
      { mutationId: `${backend}-guarded-live` },
    );
    const guardedRemoved = await store.remove(
      "cards",
      guardedLive.id,
      { ifRev: guardedLive.rev, mutationId: `${backend}-guarded-remove` },
    );
    const guardedRevive = {
      ...guardedRemoved,
      rev: guardedRemoved.rev + 1,
      updatedAt: guardedRemoved.updatedAt + 10,
      updatedBy: "remote",
      deleted: false,
      value: { id: guardedLive.id, value: "linear-revive" },
      causal: causal(guardedRemoved),
    };
    const missingParentRevive = { ...guardedRevive };
    delete missingParentRevive.causal;
    const wrongParentRevive = {
      ...guardedRevive,
      causal: causal(guardedLive),
    };
    const guardedRejections = await store.applyChanges([
      {
        collection: "cards",
        mutationId: `${backend}-guarded-revive-missing-parent`,
        record: missingParentRevive,
      },
      {
        collection: "cards",
        mutationId: `${backend}-guarded-revive-wrong-parent`,
        record: wrongParentRevive,
      },
    ], { tombstoneDominates: true });
    assert.equal(guardedRejections.applied.length, 0);
    assert.deepEqual(
      guardedRejections.conflicts.map((item) => [
        item.mutationId,
        item.reason,
      ]),
      [
        [
          `${backend}-guarded-revive-missing-parent`,
          "tombstone-dominates",
        ],
        [
          `${backend}-guarded-revive-wrong-parent`,
          "tombstone-dominates",
        ],
      ],
      `${backend} 无父与错误父证明都不能复活墓碑`,
    );
    assert.equal(
      (await store.get("cards", guardedLive.id, { includeDeleted: true })).deleted,
      true,
    );

    const guardedResult = await store.applyChanges([{
      collection: "cards",
      mutationId: `${backend}-guarded-revive`,
      record: guardedRevive,
    }], { tombstoneDominates: true });
    assert.equal(guardedResult.applied.length, 1);
    assert.equal(guardedResult.conflicts.length, 0);
    assert.equal(
      (await store.get("cards", guardedLive.id)).value.value,
      "linear-revive",
      `${backend} 精确以当前墓碑为父的 put 是合法线性续写`,
    );

    const batchLive = await store.put(
      "cards",
      { id: "same-batch", value: "live" },
      { mutationId: `${backend}-batch-live` },
    );
    const batchTombstone = {
      ...batchLive,
      rev: batchLive.rev + 1,
      updatedAt: batchLive.updatedAt + 1,
      updatedBy: "remote",
      deleted: true,
      causal: causal(batchLive),
    };
    const batchRevive = {
      ...batchLive,
      rev: batchLive.rev + 2,
      updatedAt: batchLive.updatedAt + 2,
      updatedBy: "remote",
      deleted: false,
      value: { id: batchLive.id, value: "same-batch-revive" },
      causal: causal(batchTombstone),
    };
    const batchResult = await store.applyChanges([
      {
        collection: "cards",
        mutationId: `${backend}-batch-remove`,
        record: batchTombstone,
      },
      {
        collection: "cards",
        mutationId: `${backend}-batch-revive`,
        record: batchRevive,
      },
    ], { tombstoneDominates: true });
    assert.equal(batchResult.applied.length, 2);
    assert.equal(batchResult.conflicts.length, 0);
    const batchFinal = await store.get("cards", batchLive.id);
    assert.equal(batchFinal.deleted, false);
    assert.equal(batchFinal.rev, batchRevive.rev);
    assert.equal(batchFinal.value.value, "same-batch-revive",
      `${backend} 同批 delete→put 的精确因果链必须完整应用`);
  }
});

test("v2 proofless journal 只按可信 baseline 原子补铸并可幂等重试", async () => {
  for (const fixture of legacyMigrationBackends("causal-backfill")) {
    const legacy = fixture.open([]);
    const base = await legacy.put(
      "user-settings",
      { id: "theme", mode: "system" },
      { mutationId: `${fixture.backend}-legacy-base` },
    );
    const child = await legacy.put(
      "user-settings",
      { id: "theme", mode: "dark" },
      { mutationId: `${fixture.backend}-legacy-child` },
    );
    const final = await legacy.put(
      "user-settings",
      { id: "theme", mode: "contrast" },
      { mutationId: `${fixture.backend}-legacy-final` },
    );
    assert.equal("causal" in base, false);
    assert.equal("causal" in child, false);
    assert.equal("causal" in final, false);

    const current = fixture.open(["user-settings"]);
    const before = await current.changes({ after: 1, limit: 10 });
    const inspection = await current.migrateLegacyCausal({
      contract: DataStore.CAUSAL_MIGRATION_CONTRACT,
      mode: "inspect",
      after: 1,
    });
    assert.equal(inspection.needsBaseline, true);
    assert.equal(inspection.migrated, 0);
    assert.equal("causal" in before.changes[0].record, false);

    const migrated = await current.migrateLegacyCausal({
      contract: DataStore.CAUSAL_MIGRATION_CONTRACT,
      mode: "apply",
      after: 1,
      baselineComplete: true,
      baselines: [{ collection: "user-settings", record: base }],
    });
    assert.equal(migrated.migrated, 2);
    assert.equal(migrated.throughCursor, 3);
    const after = await current.changes({ after: 1, limit: 10 });
    assert.deepEqual(after.changes.map((change) => change.mutationId), [
      `${fixture.backend}-legacy-child`,
      `${fixture.backend}-legacy-final`,
    ]);
    assert.deepEqual(after.changes[0].record.causal, causal(base));
    assert.deepEqual(after.changes[1].record.causal, causal(child));
    assert.deepEqual(
      (await current.get("user-settings", "theme")).causal,
      causal(child),
    );
    assert.deepEqual(
      await current.put(
        "user-settings",
        { id: "theme", mode: "ignored-replay" },
        { mutationId: `${fixture.backend}-legacy-child` },
      ),
      after.changes[0].record,
      `${fixture.backend} mutation receipt 与补铸 journal 一致`,
    );
    assert.equal((await current.status()).cursor, 3);

    const retried = await current.migrateLegacyCausal({
      contract: DataStore.CAUSAL_MIGRATION_CONTRACT,
      mode: "apply",
      after: 1,
    });
    assert.equal(retried.needsBaseline, false);
    assert.equal(retried.migrated, 0);
    assert.equal((await current.status()).cursor, 3);
  }
});

test("v2 因果补铸遇到错误 baseline 或 journal 缺口时零写入", async () => {
  for (const fixture of legacyMigrationBackends("causal-backfill-reject")) {
    const legacy = fixture.open([]);
    const base = await legacy.put(
      "user-settings",
      { id: "theme", mode: "system" },
      { mutationId: `${fixture.backend}-reject-base` },
    );
    await legacy.put(
      "user-settings",
      { id: "theme", mode: "dark" },
      { mutationId: `${fixture.backend}-reject-child` },
    );
    const current = fixture.open(["user-settings"]);
    const wrongBaseline = {
      ...base,
      rev: 9,
      value: { id: "theme", mode: "unrelated" },
    };
    await assert.rejects(
      current.migrateLegacyCausal({
        contract: DataStore.CAUSAL_MIGRATION_CONTRACT,
        mode: "apply",
        after: 1,
        baselineComplete: true,
        baselines: [{
          collection: "user-settings",
          record: wrongBaseline,
        }],
      }),
      (error) => error?.code === "BW_DATA_CAUSAL_MIGRATION_UNPROVEN",
    );
    const unchanged = await current.changes({ after: 1, limit: 10 });
    assert.equal("causal" in unchanged.changes[0].record, false);
    assert.equal(
      "causal" in (await current.get("user-settings", "theme")),
      false,
    );
  }

  const gapRecord = {
    schema: 1,
    collection: "user-settings",
    id: "gap",
    rev: 3,
    updatedAt: 3,
    updatedBy: "legacy",
    deleted: false,
    value: { id: "gap", value: "third" },
  };
  const gapStore = DataStore.createDataStore({
    backend: DataStore.createMemoryBackend({
      schema: 1,
      cursor: 3,
      collections: { "user-settings": { gap: gapRecord } },
      journal: [{
        cursor: 3,
        mutationId: "gap-third",
        operation: "put",
        collection: "user-settings",
        record: gapRecord,
      }],
      mutations: { "gap-third": gapRecord },
    }),
    deviceId: "gap-migration",
    causalCollections: ["user-settings"],
  });
  await assert.rejects(
    gapStore.migrateLegacyCausal({
      contract: DataStore.CAUSAL_MIGRATION_CONTRACT,
      mode: "inspect",
      after: 1,
    }),
    (error) => error?.code === "BW_DATA_CAUSAL_MIGRATION_GAP",
  );
});

test("本地 put/remove 生成非递归父状态证明，顺序子项可在同批次应用", async () => {
  for (const { backend, store } of contractStores("causal-local-chain", {
    causalCollections: ["cards"],
  })) {
    const first = await store.put(
      "cards",
      { id: "chain", value: "base" },
      { mutationId: `${backend}-chain-base` },
    );
    assert.deepEqual(first.causal, {
      contract: DataStore.CAUSAL_CONTRACT,
      parent: null,
    });
    const second = await store.put(
      "cards",
      { id: "chain", value: "local-child" },
      { mutationId: `${backend}-chain-local-child` },
    );
    assert.deepEqual(second.causal.parent, {
      deleted: false,
      value: first.value,
    });
    assert.equal("causal" in second.causal.parent, false,
      "父投影不得递归嵌套旧证明");
    const removed = await store.remove(
      "cards",
      "chain",
      { mutationId: `${backend}-chain-remove` },
    );
    assert.deepEqual(removed.causal.parent, {
      deleted: false,
      value: second.value,
    });

    const batchStore = contractStores(`${backend}-ordered`, {
      causalCollections: ["cards"],
    })[0].store;
    const base = await batchStore.put(
      "cards",
      { id: "ordered", value: "base" },
      { mutationId: `${backend}-ordered-base` },
    );
    const child = {
      ...base,
      rev: base.rev + 1,
      updatedAt: base.updatedAt + 1,
      updatedBy: "offline-a",
      value: { id: "ordered", value: "child" },
      causal: causal(base),
    };
    const grandchild = {
      ...child,
      rev: child.rev + 1,
      updatedAt: child.updatedAt + 1,
      value: { id: "ordered", value: "grandchild" },
      causal: causal(child),
    };
    const result = await batchStore.applyChanges([
      {
        collection: "cards",
        mutationId: `${backend}-ordered-child`,
        record: child,
      },
      {
        collection: "cards",
        mutationId: `${backend}-ordered-grandchild`,
        record: grandchild,
      },
    ]);
    assert.equal(result.applied.length, 2);
    assert.equal(result.conflicts.length, 0);
    assert.equal((await batchStore.get("cards", "ordered")).value.value, "grandchild");
  }
});

test("更高 rev 与离线分支都不能绕过父状态，缺失/非法证明显式保留冲突", async () => {
  for (const { backend, store } of contractStores("causal-conflicts", {
    causalCollections: ["cards"],
  })) {
    const seen = [];
    store.subscribe({ collection: "cards" }, (change) => seen.push(change));
    const base = await store.put(
      "cards",
      { id: "branch", value: "base" },
      { mutationId: `${backend}-branch-base` },
    );
    const branchA = {
      ...base,
      rev: base.rev + 1,
      updatedAt: base.updatedAt + 1,
      updatedBy: "offline-a",
      value: { id: "branch", value: "a1" },
      causal: causal(base),
    };
    const branchA2 = {
      ...branchA,
      rev: branchA.rev + 1,
      updatedAt: branchA.updatedAt + 1,
      value: { id: "branch", value: "a2" },
      causal: causal(branchA),
    };
    const accepted = await store.applyChanges([
      {
        collection: "cards",
        mutationId: `${backend}-branch-a1`,
        record: branchA,
      },
      {
        collection: "cards",
        mutationId: `${backend}-branch-a2`,
        record: branchA2,
      },
    ]);
    assert.equal(accepted.applied.length, 2);

    const beforeConflict = await store.changes({ after: 0 });
    const branchB = {
      ...base,
      rev: base.rev + 100,
      updatedAt: base.updatedAt + 2,
      updatedBy: "offline-b",
      value: { id: "branch", value: "b" },
      causal: causal(base),
    };
    const missing = { ...branchB };
    delete missing.causal;
    const invalid = {
      ...branchB,
      causal: { contract: "record-parent-state/0", parent: null },
    };
    const conflicts = await store.applyChanges([
      {
        collection: "cards",
        mutationId: `${backend}-branch-higher`,
        record: branchB,
      },
      {
        collection: "cards",
        mutationId: `${backend}-branch-missing`,
        record: missing,
      },
      {
        collection: "cards",
        mutationId: `${backend}-branch-invalid`,
        record: invalid,
      },
    ]);
    assert.deepEqual(
      conflicts.conflicts.map((item) => [item.mutationId, item.reason]),
      [
        [`${backend}-branch-higher`, "causal-parent-mismatch"],
        [`${backend}-branch-missing`, "causal-proof-missing"],
        [`${backend}-branch-invalid`, "causal-proof-invalid"],
      ],
    );
    assert.equal((await store.get("cards", "branch")).value.value, "a2");
    assert.equal((await store.changes({ after: 0 })).cursor, beforeConflict.cursor,
      `${backend} 冲突不得写 journal`);
    assert.equal(seen.length, 3,
      `${backend} 只通知 base 与两个已接受的顺序子项`);
  }
});

test("state-only CAS 允许 ABA，并把有效 rev 单调化以供下一次编辑", async () => {
  for (const { backend, store } of contractStores("causal-aba", {
    causalCollections: ["user-settings"],
  })) {
    const stateA = await store.put(
      "user-settings",
      { id: "aba", value: "A" },
      { mutationId: `${backend}-aba-a1` },
    );
    const stateBInput = {
      ...stateA,
      rev: 1,
      updatedAt: stateA.updatedAt + 1,
      updatedBy: "remote-b",
      value: { id: "aba", value: "B" },
      causal: causal(stateA),
    };
    const stateBResult = await store.applyChanges([{
      collection: "user-settings",
      mutationId: `${backend}-aba-b`,
      record: stateBInput,
    }]);
    assert.equal(stateBResult.applied[0].rev, stateA.rev + 1,
      `${backend} 接受的低 rev 子项必须提升到 current+1`);
    const stateB = await store.get("user-settings", "aba");

    const stateAAgainInput = {
      ...stateB,
      rev: 1,
      updatedAt: stateB.updatedAt + 1,
      updatedBy: "remote-a-again",
      value: { id: "aba", value: "A" },
      causal: causal(stateB),
    };
    await store.applyChanges([{
      collection: "user-settings",
      mutationId: `${backend}-aba-a2`,
      record: stateAAgainInput,
    }]);
    const stateAAgain = await store.get("user-settings", "aba");
    assert.equal(stateAAgain.value.value, "A",
      `${backend} 当前业务状态等于编辑基线时允许 ABA`);
    assert.equal(stateAAgain.rev, stateB.rev + 1);

    const higherSame = {
      ...stateAAgain,
      rev: stateAAgain.rev + 7,
      updatedAt: stateAAgain.updatedAt + 1,
      updatedBy: "remote-converged",
    };
    const converged = await store.applyChanges([{
      collection: "user-settings",
      mutationId: `${backend}-aba-same-higher`,
      record: higherSame,
    }]);
    assert.equal(converged.applied[0].rev, higherSame.rev,
      `${backend} 同业务更高 rev 应成为共同元数据基线`);

    const next = await store.put(
      "user-settings",
      { id: "aba", value: "C" },
      { mutationId: `${backend}-aba-c` },
    );
    assert.equal(next.rev, higherSame.rev + 1);
    assert.deepEqual(next.causal.parent, {
      deleted: false,
      value: higherSame.value,
    });
  }
});

test("revision 达到 JS 安全整数上限后，任何需要递增的变化都 fail closed", async () => {
  for (const { backend, store } of contractStores("causal-rev-overflow", {
    causalCollections: ["user-settings"],
  })) {
    const maximum = {
      schema: 1,
      collection: "user-settings",
      id: "max-rev",
      rev: Number.MAX_SAFE_INTEGER,
      updatedAt: 1,
      updatedBy: "remote-max",
      deleted: false,
      value: { id: "max-rev", value: "head" },
      causal: causal(null),
    };
    const seeded = await store.applyChanges([{
      collection: "user-settings",
      mutationId: `${backend}-max-seed`,
      record: maximum,
    }]);
    assert.equal(seeded.applied[0].rev, Number.MAX_SAFE_INTEGER);
    await assert.rejects(
      store.put(
        "user-settings",
        { id: "max-rev", value: "local-child" },
        { mutationId: `${backend}-max-local-child` },
      ),
      (error) => error.code === "BW_DATA_REV_OVERFLOW",
    );
    const remoteChild = {
      ...maximum,
      rev: 1,
      value: { id: "max-rev", value: "remote-child" },
      causal: causal(maximum),
    };
    const rejected = await store.applyChanges([{
      collection: "user-settings",
      mutationId: `${backend}-max-remote-child`,
      record: remoteChild,
    }]);
    assert.equal(rejected.applied.length, 0);
    assert.equal(
      rejected.conflicts[0].reason,
      "causal-revision-overflow",
    );
    assert.equal((await store.get("user-settings", "max-rev")).value.value, "head");
  }
});

test("因果父状态大小上限只约束显式同步 collection", async () => {
  const payload = "x".repeat(DataStore.MAX_CAUSAL_PARENT_BYTES + 1);
  for (const { backend, store } of contractStores("causal-size-scope", {
    causalCollections: ["user-settings"],
  })) {
    await store.put(
      "document-notes",
      { id: "large-document", payload },
      { mutationId: `${backend}-large-document-1` },
    );
    const updatedDocument = await store.put(
      "document-notes",
      { id: "large-document", payload, edited: true },
      { mutationId: `${backend}-large-document-2` },
    );
    assert.equal(updatedDocument.value.edited, true,
      `${backend} 非同步 document 数据不应生成因果父投影`);
    assert.equal("causal" in updatedDocument, false);

    await store.put(
      "user-settings",
      { id: "large-sync", payload },
      { mutationId: `${backend}-large-sync-1` },
    );
    await assert.rejects(
      store.put(
        "user-settings",
        { id: "large-sync", payload, edited: true },
        { mutationId: `${backend}-large-sync-2` },
      ),
      (error) => error.code === "BW_DATA_CAUSAL_TOO_LARGE",
      `${backend} 同步 collection 的超限父状态必须显式失败`,
    );
    assert.equal((await store.get("user-settings", "large-sync")).value.edited, undefined);
  }
});

test("DataStore 返回深拷贝，本地写入不触发网络", async () => {
  const backend = DataStore.createMemoryBackend();
  const store = DataStore.createDataStore({ backend, deviceId: "deep-copy" });
  const value = { id: "x", nested: { n: 1 } };
  await store.put("cards", value, { mutationId: "copy-1" });
  value.nested.n = 99;
  const got = await store.get("cards", "x");
  got.value.nested.n = 88;
  assert.equal((await store.get("cards", "x")).value.nested.n, 1);
  assert.equal((await store.changes({ after: 0 })).changes.length, 1);
});

test("内存与 WebStorage 对非 JSON 值使用同一拒绝语义且不静默改写", async () => {
  const circular = { id: "circular" };
  circular.self = circular;
  const sparse = [];
  sparse.length = 1;
  const invalidValues = [
    ["undefined", { id: "undefined", payload: undefined }, "BW_DATA_INVALID"],
    ["NaN", { id: "nan", payload: Number.NaN }, "BW_DATA_INVALID"],
    ["Infinity", { id: "infinity", payload: Number.POSITIVE_INFINITY }, "BW_DATA_INVALID"],
    ["function", { id: "function", payload() {} }, "BW_DATA_INVALID"],
    ["Symbol", { id: "symbol", payload: Symbol("x") }, "BW_DATA_INVALID"],
    ["BigInt", { id: "bigint", payload: 1n }, "BW_DATA_INVALID"],
    ["Date", { id: "date", payload: new Date(0) }, "BW_DATA_INVALID"],
    ["Blob", { id: "blob", payload: new Blob(["x"]) }, "BW_DATA_BINARY"],
    ["ArrayBuffer", { id: "buffer", payload: new ArrayBuffer(4) }, "BW_DATA_BINARY"],
    ["循环引用", circular, "BW_DATA_INVALID"],
    ["稀疏数组", { id: "sparse", payload: sparse }, "BW_DATA_INVALID"],
  ];

  for (const { backend, store, storage } of contractStores("invalid-values")) {
    for (const [label, value, code] of invalidValues) {
      await assert.rejects(
        store.put("invalid", value, { mutationId: `${backend}-${label}` }),
        (error) => error.code === code,
        `${backend} 应以 ${code} 拒绝 ${label}`,
      );
    }
    await assert.rejects(
      store.applyChanges([{
        collection: "invalid",
        mutationId: `${backend}-remote-undefined`,
        record: {
          schema: 1,
          collection: "invalid",
          id: "remote-undefined",
          rev: 1,
          updatedAt: 1,
          updatedBy: "remote",
          deleted: false,
          value: { id: "remote-undefined", payload: undefined },
        },
      }]),
      (error) => error.code === "BW_DATA_INVALID",
      `${backend} 的 applyChanges 不能绕过值规范`,
    );
    assert.equal((await store.list("invalid")).length, 0, `${backend} 不得保存残缺值`);
    assert.equal((await store.changes({ after: 0 })).changes.length, 0,
      `${backend} 不得为失败值生成 journal`);
    if (storage) assert.equal(storage.setCalls, 0, "失败校验不得触发 WebStorage 写入");
  }
});

test("applyChanges 严格执行 sync-v2 recordSchema，同时保留可扩展字段与旧 operation 省略入口", async () => {
  const validChange = (suffix) => ({
    collection: "incoming-contract",
    mutationId: `incoming-${suffix}`,
    operation: "put",
    record: {
      schema: 1,
      collection: "incoming-contract",
      id: `record-${suffix}`,
      rev: 1,
      updatedAt: 1_700_000_000_000,
      updatedBy: "remote-device",
      deleted: false,
      value: { id: `record-${suffix}`, payload: "valid" },
      causal: causal(null),
    },
  });
  const invalidCases = [
    ["缺少 schema", (change) => delete change.record.schema, "BW_DATA_SCHEMA"],
    ["错误 schema", (change) => { change.record.schema = 2; }, "BW_DATA_SCHEMA"],
    ["缺少 deleted", (change) => delete change.record.deleted, "BW_DATA_INVALID"],
    ["非布尔 deleted", (change) => { change.record.deleted = 0; }, "BW_DATA_INVALID"],
    ["record collection 不匹配", (change) => {
      change.record.collection = "other-collection";
    }, "BW_DATA_INVALID"],
    ["rev 小于 1", (change) => { change.record.rev = 0; }, "BW_DATA_INVALID"],
    ["rev 非整数", (change) => { change.record.rev = 1.5; }, "BW_DATA_INVALID"],
    ["rev 数字字符串", (change) => { change.record.rev = "1"; }, "BW_DATA_INVALID"],
    ["缺少 value", (change) => delete change.record.value, "BW_DATA_INVALID"],
    ["value 含非法 JSON", (change) => {
      change.record.value = { id: change.record.id, payload: undefined };
    }, "BW_DATA_INVALID"],
    ["缺少 updatedAt", (change) => delete change.record.updatedAt, "BW_DATA_INVALID"],
    ["updatedAt 非数字", (change) => {
      change.record.updatedAt = "1700000000000";
    }, "BW_DATA_INVALID"],
    ["updatedAt 为负数", (change) => { change.record.updatedAt = -1; }, "BW_DATA_INVALID"],
    ["缺少 updatedBy", (change) => delete change.record.updatedBy, "BW_DATA_INVALID"],
    ["updatedBy 为空", (change) => { change.record.updatedBy = ""; }, "BW_DATA_INVALID"],
    ["updatedBy 非字符串", (change) => { change.record.updatedBy = 7; }, "BW_DATA_INVALID"],
    ["put 与 tombstone 不匹配", (change) => {
      change.record.deleted = true;
    }, "BW_DATA_INVALID"],
    ["remove 与活动记录不匹配", (change) => {
      change.operation = "remove";
    }, "BW_DATA_INVALID"],
    ["未知 operation", (change) => {
      change.operation = "merge";
    }, "BW_DATA_INVALID"],
  ];

  for (const { backend, store, storage } of contractStores("incoming-record-contract")) {
    const writesBefore = storage?.setCalls;
    for (const [index, [label, mutate, code]] of invalidCases.entries()) {
      const change = validChange(`${backend}-invalid-${index}`);
      mutate(change);
      await assert.rejects(
        store.applyChanges([change]),
        (error) => error.code === code,
        `${backend} 应以 ${code} 拒绝 ${label}`,
      );
    }
    assert.equal(
      (await store.list("incoming-contract", { includeDeleted: true })).length,
      0,
      `${backend} 不得保存任何非法入站 envelope`,
    );
    if (storage) {
      assert.equal(
        storage.setCalls,
        writesBefore,
        "完整入站校验应在 WebStorage 写入前完成",
      );
    }

    const extended = validChange(`${backend}-extended`);
    extended.futureChangeField = { version: 2 };
    extended.record.futureRecordField = { retained: true };
    const extendedResult = await store.applyChanges([extended]);
    assert.equal(extendedResult.applied.length, 1);
    assert.deepEqual(
      (await store.get("incoming-contract", extended.record.id)).futureRecordField,
      { retained: true },
      `${backend} 保留合法 JSON 的未知 record 扩展字段`,
    );

    const legacy = validChange(`${backend}-operation-omitted`);
    delete legacy.operation;
    const legacyResult = await store.applyChanges([legacy]);
    assert.equal(
      legacyResult.applied.length,
      1,
      `${backend} 保留 data-store/1 内部调用省略 operation 的既有兼容入口`,
    );
  }
});

test("内存与 WebStorage batch 均为全有或全无，失败不通知也不写 journal", async () => {
  for (const { backend, store, storage } of contractStores("atomic-batch")) {
    const seed = await store.put(
      "atomic",
      { id: "existing", n: 1 },
      { mutationId: `${backend}-seed` },
    );
    const before = await store.changes({ after: 0 });
    const writesBefore = storage?.setCalls;
    const seen = [];
    const unsubscribe = store.subscribe({ collection: "atomic" }, (change) => seen.push(change));

    await assert.rejects(
      store.batch([
        {
          collection: "atomic",
          value: { id: "would-rollback", n: 2 },
          mutationId: `${backend}-atomic-first`,
        },
        {
          collection: "atomic",
          value: { id: "existing", n: 3 },
          ifRev: 0,
          mutationId: `${backend}-atomic-conflict`,
        },
      ]),
      (error) => error.code === "BW_DATA_CONFLICT" &&
        error.details.expectedRev === 0 &&
        error.details.actualRev === seed.rev,
    );
    assert.equal(await store.get("atomic", "would-rollback"), null,
      `${backend} 必须回滚冲突项之前的写入`);
    assert.equal((await store.get("atomic", "existing")).value.n, 1);
    assert.equal((await store.changes({ after: 0 })).cursor, before.cursor,
      `${backend} 失败批次不得推进 cursor`);
    assert.equal(seen.length, 0, `${backend} 失败批次不得发订阅通知`);
    if (storage) assert.equal(storage.setCalls, writesBefore,
      "冲突批次不得触发 WebStorage 部分保存");

    await assert.rejects(
      store.batch([
        {
          collection: "atomic",
          value: { id: "invalid-would-rollback", n: 2 },
          mutationId: `${backend}-invalid-first`,
        },
        {
          collection: "atomic",
          value: { id: "invalid-value", payload: undefined },
          mutationId: `${backend}-invalid-second`,
        },
      ]),
      (error) => error.code === "BW_DATA_INVALID",
    );
    assert.equal(await store.get("atomic", "invalid-would-rollback"), null,
      `${backend} 必须在写入前完成整批值校验`);

    await assert.rejects(
      store.batch([
        {
          collection: "atomic",
          value: { id: "unknown-op-would-rollback", n: 2 },
          mutationId: `${backend}-unknown-op-first`,
        },
        {
          operation: "erase-everything",
          collection: "atomic",
          id: "existing",
          mutationId: `${backend}-unknown-op-second`,
        },
      ]),
      (error) => error.code === "BW_DATA_INVALID" &&
        error.details.operation === "erase-everything",
    );
    assert.equal(await store.get("atomic", "unknown-op-would-rollback"), null,
      `${backend} 不得把未知 operation 静默当作 put`);

    const validWritesBefore = storage?.setCalls;
    const results = await store.batch([
      {
        collection: "atomic",
        value: { id: "valid-a", n: 2 },
        mutationId: `${backend}-valid-a`,
      },
      {
        collection: "atomic",
        value: { id: "valid-b", n: 3 },
        mutationId: `${backend}-valid-b`,
      },
    ]);
    assert.equal(results.length, 2);
    assert.equal(seen.length, 2, `${backend} 成功后再逐项通知`);
    if (storage) assert.equal(storage.setCalls, validWritesBefore + 1,
      "成功 WebStorage batch 只提交一次完整状态");
    unsubscribe();
  }
});

test("内存与 WebStorage 使用同一 ifRev 校验及冲突详情", async () => {
  for (const { backend, store } of contractStores("revision")) {
    const first = await store.put(
      "cards",
      { id: "revision", n: 1 },
      { mutationId: `${backend}-revision-seed` },
    );
    for (const [index, invalidRevision] of
      [Number.NaN, Number.POSITIVE_INFINITY, -1, 1.5].entries()) {
      await assert.rejects(
        store.put(
          "cards",
          { id: "revision", n: 2 },
          { ifRev: invalidRevision, mutationId: `${backend}-invalid-rev-${index}` },
        ),
        (error) => error.code === "BW_DATA_INVALID",
      );
    }
    await assert.rejects(
      store.put(
        "cards",
        { id: "revision", n: 3 },
        { ifRev: 0, mutationId: `${backend}-revision-conflict` },
      ),
      (error) => error.code === "BW_DATA_CONFLICT" &&
        error.details.collection === "cards" &&
        error.details.id === "revision" &&
        error.details.expectedRev === 0 &&
        error.details.actualRev === first.rev,
    );
    const second = await store.put(
      "cards",
      { id: "revision", n: 4 },
      { ifRev: String(first.rev), mutationId: `${backend}-numeric-string-rev` },
    );
    assert.equal(second.rev, first.rev + 1, `${backend} 保留数字字符串 ifRev 兼容`);
  }
});

test("applyChanges 可选择把对账导入写入目标 journal，远端落库默认不回声", async () => {
  const source = makeStore("journal-source");
  await source.put("cards", { id: "imported-card", text: "待接管" }, { mutationId: "source-op" });
  const [change] = (await source.changes({ after: 0 })).changes;

  const remoteTarget = makeStore("remote-target");
  const reconciledTarget = makeStore("reconciled-target");
  await remoteTarget.applyChanges([change]);
  await reconciledTarget.applyChanges([change], { journal: true });

  assert.equal((await remoteTarget.changes({ after: 0 })).changes.length, 0);
  const imported = await reconciledTarget.changes({ after: 0 });
  assert.equal(imported.changes.length, 1);
  assert.equal(imported.changes[0].mutationId, "source-op");
  assert.equal(imported.changes[0].record.id, "imported-card");
  assert.equal(imported.resetRequired, false);
});

test("changes 在 journal 被裁剪后显式暴露 oldestCursor 与 resetRequired", async () => {
  const store = DataStore.createDataStore({
    backend: DataStore.createMemoryBackend(),
    deviceId: "trimmed-journal",
    maxJournal: 100,
  });
  for (let index = 1; index <= 105; index += 1) {
    await store.put(
      "cards",
      { id: `trim-${String(index).padStart(3, "0")}` },
      { mutationId: `trim-op-${index}` },
    );
  }

  const gap = await store.changes({ after: 0, limit: 2000 });
  assert.equal(gap.cursor, 105);
  assert.equal(gap.oldestCursor, 6);
  assert.equal(gap.resetRequired, true);

  const resumable = await store.changes({ after: 5, limit: 2000 });
  assert.equal(resumable.oldestCursor, 6);
  assert.equal(resumable.resetRequired, false);
  assert.equal(resumable.nextCursor, 105);
  assert.equal(resumable.changes.length, 100);

  const cursorAhead = await store.changes({ after: 999, limit: 2000 });
  assert.equal(cursorAhead.resetRequired, true);
});

test("StorageRouter 只按已登记归属分流，未知和 pending 均报错", async () => {
  const globalStore = makeStore("global");
  const documentStore = makeStore("document");
  const scopes = {
    "test-global": {
      scope: "global", status: "ready", provider: true, conflictPolicy: "explicit",
    },
    "test-document": {
      scope: "document", status: "ready", provider: false, conflictPolicy: "explicit",
    },
    "legacy-highlights": {
      scope: "document",
      status: "pending",
      provider: false,
      conflictPolicy: "explicit",
      reason: "旧 HTML 与扩展高亮来源冲突",
    },
  };
  const router = StorageRouter.createStorageRouter({
    globalStore,
    documentStore,
    deviceStore: documentStore,
    scopes,
    dataRegistryApi: makeRegistry(scopes),
  });

  await router.put("test-global", { id: "global-record" }, { mutationId: "g1" });
  await router.put("test-document", { id: "doc-record", documentId: "book-1" }, { mutationId: "d1" });
  assert.ok(await globalStore.get("test-global", "global-record"));
  assert.ok(await documentStore.get("test-document", "doc-record"));

  assert.throws(() => router.storeFor("mystery"), (error) => error.code === "BW_ROUTER_UNREGISTERED");
  assert.throws(
    () => router.register("mystery", { scope: "global", status: "ready", provider: true }),
    (error) => error.code === "BW_ROUTER_UNREGISTERED",
  );
  router.register("legacy-highlights", {
    scope: "document",
    status: "pending",
    provider: false,
    conflictPolicy: "explicit",
    reason: "旧 HTML 与扩展高亮来源冲突",
  });
  assert.throws(() => router.storeFor("legacy-highlights"), (error) => error.code === "BW_ROUTER_PENDING");
});

test("StorageRouter 把同 scope batch 原子下推，并在跨 scope 前 fail closed", async () => {
  const globalStore = makeStore("router-batch-global");
  const documentStore = makeStore("router-batch-document");
  const scopes = {
    "card-entities": {
      scope: "global", status: "ready", provider: true, conflictPolicy: "explicit",
    },
    "card-states": {
      scope: "global", status: "ready", provider: true, conflictPolicy: "explicit",
    },
    "document-notes": {
      scope: "document", status: "ready", provider: false, conflictPolicy: "explicit",
    },
  };
  const router = StorageRouter.createStorageRouter({
    globalStore,
    documentStore,
    deviceStore: documentStore,
    scopes,
    dataRegistryApi: makeRegistry(scopes),
  });

  const written = await router.batch([
    {
      collection: "card-entities",
      value: { id: "card_abcd", side: "entity" },
      options: { mutationId: "router-batch-entity" },
    },
    {
      collection: "card-states",
      value: { id: "card_abcd", side: "state" },
      options: { mutationId: "router-batch-state" },
    },
  ]);
  assert.equal(written.length, 2);
  assert.equal((await globalStore.get("card-entities", "card_abcd")).value.side, "entity");
  assert.equal((await globalStore.get("card-states", "card_abcd")).value.side, "state");

  await assert.rejects(
    router.batch([
      {
        collection: "card-entities",
        value: { id: "card_beef" },
        options: { mutationId: "router-cross-global" },
      },
      {
        collection: "document-notes",
        value: { id: "note-beef" },
        options: { mutationId: "router-cross-document" },
      },
    ]),
    (error) => error.code === "BW_ROUTER_BATCH_SCOPE",
  );
  assert.equal(await globalStore.get("card-entities", "card_beef"), null);
  assert.equal(await documentStore.get("document-notes", "note-beef"), null);
});

test("生产注册表只开放已确认 collection，冲突数据保持 pending", () => {
  assert.deepEqual(
    DataRegistry.providerCollections(),
    [
      "card-entities", "card-states", "dictionary-cache", "query-cache",
      "translation-cache", "user-settings", "vocabulary-state",
    ],
  );
  assert.equal(DataRegistry.collection("user-settings").status, "ready");
  assert.equal(DataRegistry.collection("card-entities").status, "ready");
  assert.equal(DataRegistry.collection("card-states").status, "ready");
  for (const name of ["cards", "favorites", "anchors", "vocabulary", "conversation-threads"]) {
    assert.equal(DataRegistry.collection(name).status, "pending", `${name} 不得被静默开放`);
  }
});

test("StorageRouter 不再用内置默认表替代缺失或不一致的 DataRegistry", () => {
  const globalStore = makeStore("registry-required-global");
  const documentStore = makeStore("registry-required-document");
  assert.throws(
    () => StorageRouter.createStorageRouter({
      globalStore,
      documentStore,
      deviceStore: documentStore,
      dataRegistryApi: {},
    }),
    (error) => error.code === "BW_ROUTER_REGISTRY",
  );
  assert.throws(
    () => StorageRouter.createStorageRouter({
      globalStore,
      documentStore,
      deviceStore: documentStore,
      scopes: {
        "unknown-provider": {
          scope: "global", status: "ready", provider: true, conflictPolicy: "explicit",
        },
      },
    }),
    (error) => error.code === "BW_ROUTER_UNREGISTERED",
  );
  assert.throws(
    () => StorageRouter.createStorageRouter({
      globalStore,
      documentStore,
      deviceStore: documentStore,
      scopes: {
        "user-settings": {
          scope: "document", status: "ready", provider: false, conflictPolicy: "explicit",
        },
      },
    }),
    (error) => error.code === "BW_ROUTER_REGISTRY_MISMATCH",
  );
});

test("StorageRouter 不再暗中把 documentStore 当作 deviceStore", () => {
  const globalStore = makeStore("explicit-device-global");
  const documentStore = makeStore("explicit-device-document");
  const emptyRegistry = makeRegistry({});
  assert.throws(
    () => StorageRouter.createStorageRouter({
      globalStore,
      documentStore,
      dataRegistryApi: emptyRegistry,
    }),
    (error) => error.code === "BW_ROUTER_DEVICE_STORE",
  );
  const explicitAlias = StorageRouter.createStorageRouter({
    globalStore,
    documentStore,
    allowDeviceDocumentAlias: true,
    dataRegistryApi: emptyRegistry,
  });
  assert.deepEqual(explicitAlias.scopes(), {});
});

test("StorageRouter 切换 store 时取消旧订阅并把同一 listener 重绑到新 store", async () => {
  const firstStore = makeStore("subscription-first");
  const secondStore = makeStore("subscription-second");
  const documentStore = makeStore("subscription-document");
  const scopes = {
    "subscribed-global": {
      scope: "global",
      status: "ready",
      provider: true,
      conflictPolicy: "explicit",
    },
  };
  const router = StorageRouter.createStorageRouter({
    globalStore: firstStore,
    documentStore,
    deviceStore: documentStore,
    scopes,
    dataRegistryApi: makeRegistry(scopes),
  });
  const seen = [];
  const unsubscribe = router.subscribe("subscribed-global", (change) => {
    seen.push(`${change.record.updatedBy}/${change.record.id}`);
  });

  await firstStore.put("subscribed-global", { id: "before-switch" }, { mutationId: "sub-before" });
  router.setGlobalStore(secondStore);
  await firstStore.put("subscribed-global", { id: "old-after-switch" }, { mutationId: "sub-old-after" });
  await secondStore.put("subscribed-global", { id: "new-after-switch" }, { mutationId: "sub-new-after" });
  unsubscribe();
  await secondStore.put("subscribed-global", { id: "after-unsubscribe" }, { mutationId: "sub-unsubscribed" });

  assert.deepEqual(seen, [
    "subscription-first/before-switch",
    "subscription-second/new-after-switch",
  ]);
});

test("StorageRouter.applyChanges 逐项校验 ready 与 scope，不能借 scope 绕过 pending", async () => {
  const globalStore = makeStore("apply-router-global");
  const documentStore = makeStore("apply-router-document");
  const originalApply = globalStore.applyChanges;
  let applyCalls = 0;
  globalStore.applyChanges = (...args) => {
    applyCalls += 1;
    return originalApply(...args);
  };
  const scopes = {
    "ready-global": {
      scope: "global", status: "ready", provider: true, conflictPolicy: "explicit",
    },
    "pending-global": {
      scope: "global", status: "pending", provider: false, conflictPolicy: "explicit",
    },
    "ready-document": {
      scope: "document", status: "ready", provider: false, conflictPolicy: "explicit",
    },
  };
  const router = StorageRouter.createStorageRouter({
    globalStore,
    documentStore,
    deviceStore: documentStore,
    scopes,
    dataRegistryApi: makeRegistry(scopes),
  });
  const record = {
    schema: 1,
    collection: "ready-global",
    id: "safe",
    rev: 1,
    updatedAt: 1,
    updatedBy: "remote",
    deleted: false,
    value: { id: "safe" },
    causal: causal(null),
  };
  const valid = { collection: "ready-global", mutationId: "safe-change", record };

  await assert.rejects(
    router.applyChanges("global", [
      valid,
      {
        collection: "pending-global",
        mutationId: "pending-change",
        record: { ...record, collection: "pending-global", id: "blocked" },
      },
    ]),
    (error) => error.code === "BW_ROUTER_PENDING",
  );
  await assert.rejects(
    router.applyChanges("global", [{
      collection: "ready-document",
      mutationId: "wrong-scope",
      record: { ...record, collection: "ready-document", id: "document" },
    }]),
    (error) => error.code === "BW_ROUTER_SCOPE_MISMATCH",
  );
  await assert.rejects(
    router.applyChanges("global", [{
      collection: "ready-global",
      mutationId: "mismatched-collection",
      record: { ...record, collection: "ready-document" },
    }]),
    (error) => error.code === "BW_ROUTER_COLLECTION_MISMATCH",
  );
  assert.equal(applyCalls, 0, "整批必须先校验，不能先写 ready 项再发现 pending");

  const applied = await router.applyChanges("global", [valid], { journal: true });
  assert.equal(applyCalls, 1);
  assert.equal(applied.conflicts.length, 0);
  assert.ok(await globalStore.get("ready-global", "safe"));
});

test("StorageRouter 从生产 registry 补全 provider、conflictPolicy 与 derived 元数据", () => {
  const router = StorageRouter.createStorageRouter({
    globalStore: makeStore("registry-metadata-global"),
    documentStore: makeStore("registry-metadata-document"),
    deviceStore: makeStore("registry-metadata-device"),
    scopes: DataRegistry.scopes(),
  });
  const scopes = router.scopes();
  assert.equal(scopes["user-settings"].provider, true);
  assert.equal(scopes["user-settings"].conflictPolicy, "explicit");
  assert.equal(scopes["user-settings"].derived, false);
  assert.equal(scopes["query-cache"].provider, true);
  assert.equal(scopes["query-cache"].conflictPolicy, "regenerate");
  assert.equal(scopes["query-cache"].derived, true);
  assert.equal(scopes["device-preferences"].provider, false);
});

// ── 读取事务的上界 ───────────────────────────────────────────────────────
//
// 高亮写入反复超时，前两层是 StorageRouter 与 mutateDocumentState 各自把
// batchOptions 丢掉；都修完之后仍有第三层：写入之前那次前置读走的是 readonly
// 事务，而它没有上界。IndexedDB 事务按 object store 排队，一个挂住不 settle 的
// 读会把后面的写一并堵住，于是那个写入上界永远没有机会生效。
// 任何一步都不许把整组测试拖死：保护失效时应当是这条测试失败，而不是 node --test
// 整体挂住。此前一次对照实验就因为没有看门狗而拖垮了整组并中断了恢复。
function watchdog(promise, label, ms = 400) {
  return Promise.race([
    promise,
    new Promise((_, reject) => setTimeout(
      () => reject(new Error(`${label} 未在 ${ms}ms 内落定`)), ms,
    )),
  ]);
}

function boundedReadRuntime() {
  const fake = abortableBatchIndexedDB();
  const context = {
    console,
    indexedDB: fake.factory,
    IDBKeyRange: { only: (v) => v, lowerBound: (v) => v },
    navigator: { userAgent: "bounded-read-contract" },
    crypto: { getRandomValues(bytes) { bytes.fill(3); return bytes; } },
    structuredClone, setTimeout, clearTimeout, setImmediate,
  };
  context.globalThis = context;
  vm.runInNewContext(DATA_STORE_SOURCE, context, { filename: "data-store.js" });
  vm.runInNewContext(INDEXEDDB_STORE_SOURCE, context, { filename: "indexeddb-store.js" });
  const store = context.BWReaderRuntime.indexedDBStore.createIndexedDBDataStore({
    dbName: "bounded-read-timeout",
    deviceId: "bounded-read-test",
    broadcast: false,
    webkitTransactionKeepalive: false,
  });
  const options = (ms) => vm.runInNewContext(`({ transactionTimeoutMs: ${ms} })`, context);
  // 值必须在 store 所在的 realm 里创建：跨 realm 的对象过不了 isPlainObject 检查。
  const inRealm = (value) => vm.runInNewContext(`(${JSON.stringify(value)})`, context);
  return { fake, store, options, inRealm };
}

test("a hung read with a bound aborts the real transaction", async () => {
  const { fake, store, options } = boundedReadRuntime();
  fake.hangNextRecordRead();
  await assert.rejects(
    watchdog(store.get("document-highlights", "book-1", options(20)), "bounded read"),
    (error) => error.code === "BW_DATA_TIMEOUT",
    "a bounded read must end as BW_DATA_TIMEOUT instead of hanging forever",
  );
  assert.equal(
    fake.abortCount(),
    1,
    "the transaction itself must be aborted; walking away leaves it holding the store",
  );
});

test("reads and writes continue once the hung read is aborted", async () => {
  const { fake, store, options, inRealm } = boundedReadRuntime();
  fake.hangNextRecordRead();
  await assert.rejects(
    watchdog(store.get("document-highlights", "book-1", options(20)), "bounded read"),
  );

  // 用户关心的是"后面还能不能记下新高亮"，所以这里必须真的写一次再读回，
  // 只证明"还能读"是不够的：写走的是 readwrite，正是被挂起事务堵住的那一类。
  await watchdog(
    store.put("document-highlights",
      inRealm({ id: "book-1", color: "#ffd54a" }),
      inRealm({ mutationId: "m_after_abort" })),
    "later write",
  );
  const readBack = await watchdog(
    store.get("document-highlights", "book-1"), "later read",
  );
  assert.ok(readBack, "写入必须真的落库");
  assert.equal(readBack.value.color, "#ffd54a", "读回的应当是刚写进去的那条");
});

test("an unbounded read keeps its previous semantics", async () => {
  const { fake, store } = boundedReadRuntime();
  fake.hangNextRecordRead();
  const outcome = await Promise.race([
    store.get("some-other-collection", "x").then(() => "settled", () => "rejected"),
    new Promise((resolve) => setTimeout(() => resolve("still-pending"), 120)),
  ]);
  assert.equal(
    outcome, "still-pending",
    "no bound was asked for, so none is invented: unrelated collections keep their behaviour",
  );
  assert.equal(fake.abortCount(), 0, "nothing may be aborted without an explicit bound");
});
