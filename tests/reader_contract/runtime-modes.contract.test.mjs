import test from "node:test";
import assert from "node:assert/strict";
import {
  DataStore,
  RuntimeSelector,
  makeDocumentFixture,
  makeRegistry,
  makeStore,
  makeUiSpy,
} from "./helpers.mjs";

const TEST_GLOBAL = "test-global";
const TEST_DOCUMENT = "test-document";
const TEST_SCOPES = {
  [TEST_GLOBAL]: {
    scope: "global",
    status: "ready",
    provider: true,
    conflictPolicy: "explicit",
  },
  [TEST_DOCUMENT]: {
    scope: "document",
    status: "ready",
    provider: false,
    conflictPolicy: "explicit",
  },
};

function createRuntime(options) {
  const scopes = options.scopes || TEST_SCOPES;
  return RuntimeSelector.createReaderRuntime({
    ...options,
    scopes,
    pwaDeviceStore: options.pwaDeviceStore || options.pwaStore,
    dataRegistryApi: options.dataRegistryApi || makeRegistry(scopes),
  });
}

function makeSeededStore(name, records) {
  const seeded = {};
  for (const item of records) {
    seeded[item.id] = {
      schema: 1,
      collection: TEST_GLOBAL,
      id: item.id,
      rev: item.rev || 1,
      updatedAt: item.updatedAt || 1,
      updatedBy: name,
      deleted: false,
      value: { id: item.id, text: item.text },
    };
  }
  return DataStore.createDataStore({
    backend: DataStore.createMemoryBackend({
      schema: 1,
      cursor: 0,
      collections: { [TEST_GLOBAL]: seeded },
      journal: [],
      mutations: {},
    }),
    deviceId: name,
  });
}

for (const kind of ["pdf", "epub", "html", "favorite"]) {
  for (const serviceMode of ["fallback", "extension"]) {
    test(`${kind} × ${serviceMode}：PWA 保留 DocumentHost 与 native fallback，扩展数据服务不替换宿主`, async () => {
      const fixture = makeDocumentFixture(kind);
      const pwaStore = makeStore(`pwa-${kind}-${serviceMode}`);
      const extensionStore = makeStore(`extension-${kind}-${serviceMode}`);
      const ui = makeUiSpy();
      const runtime = createRuntime({
        documentHost: fixture.host,
        pwaStore,
        ui,
        scopes: TEST_SCOPES,
      });
      await runtime.start();
      await runtime.start();
      const originalHost = runtime.documentHost();

      if (serviceMode === "extension") {
        const attached = await runtime.attachExtension({ dataStore: extensionStore });
        assert.equal(attached.connected, true);
        assert.equal(runtime.mode(), "pwa-extension-provider");
      } else {
        assert.equal(runtime.mode(), "pwa-fallback");
      }

      assert.equal(ui.mountCount, 1);
      // 这里的 ui 是 PWA 的 native fallback renderer；真正的扩展顶栏接管由
      // book-extension-handoff.contract.test.mjs 的 TAKEOVER 租约另行约束。
      assert.equal(ui.owner, "pwa");
      assert.equal(runtime.documentHost(), originalHost);
      assert.equal(runtime.documentHost().kind, kind);

      await runtime.storage().put(TEST_GLOBAL, { id: `${kind}-global` }, { mutationId: `${kind}-global-op` });
      await runtime.storage().put(TEST_DOCUMENT, {
        id: `${kind}-anchor`,
        documentId: fixture.documentId,
      }, { mutationId: `${kind}-anchor-op` });

      if (serviceMode === "extension") {
        assert.ok(await extensionStore.get(TEST_GLOBAL, `${kind}-global`));
        assert.ok(
          await pwaStore.get(TEST_GLOBAL, `${kind}-global`),
          "扩展主库的新写入必须同步保留在 PWA 影子库，断线后才能无缝回退",
        );
      } else {
        assert.ok(await pwaStore.get(TEST_GLOBAL, `${kind}-global`));
      }
      assert.ok(await pwaStore.get(TEST_DOCUMENT, `${kind}-anchor`));
      assert.equal(await extensionStore.get(TEST_DOCUMENT, `${kind}-anchor`), null);
    });
  }
}

test("扩展接管时不回退暴露 PWA SyncGateway，断开后才恢复", async () => {
  const pwaGateway = { contract: "sync-gateway/2", owner: "pwa" };
  const runtime = createRuntime({
    documentHost: makeDocumentFixture("pdf").host,
    pwaStore: makeStore("sync-owner-pwa"),
    pwaSyncGateway: pwaGateway,
    ui: makeUiSpy(),
    scopes: TEST_SCOPES,
  });
  assert.equal(runtime.syncGateway(), pwaGateway);
  const attached = await runtime.attachExtension({
    dataStore: makeStore("sync-owner-extension"),
    syncGateway: null,
  });
  assert.equal(attached.connected, true);
  assert.equal(runtime.syncGateway(), null);
  await runtime.detachExtension("provider-disconnected");
  assert.equal(runtime.syncGateway(), pwaGateway);
});

test("provider registry 缺失时拒绝扩展接管，但 PWA fallback 与既有 UI 继续工作", async () => {
  const fixture = makeDocumentFixture("pdf");
  const pwaStore = makeStore("missing-registry-pwa");
  const ui = makeUiSpy();
  const incompleteRegistry = makeRegistry(TEST_SCOPES);
  delete incompleteRegistry.providerCollections;
  const runtime = RuntimeSelector.createReaderRuntime({
    documentHost: fixture.host,
    pwaStore,
    pwaDeviceStore: pwaStore,
    ui,
    scopes: TEST_SCOPES,
    dataRegistryApi: incompleteRegistry,
  });
  await runtime.start();
  await runtime.storage().put(
    TEST_GLOBAL,
    { id: "fallback-still-works" },
    { mutationId: "fallback-still-works-op" },
  );
  await assert.rejects(
    runtime.attachExtension({ dataStore: makeStore("missing-registry-extension") }),
    (error) => error.code === "BW_RUNTIME_REGISTRY",
  );
  assert.equal(runtime.mode(), "pwa-fallback");
  assert.equal(ui.mountCount, 1);
  assert.ok(await pwaStore.get(TEST_GLOBAL, "fallback-still-works"));
});

test("正式 extension-services 必须声明与 PWA 完全一致的 provider/sync-v3 因果合同", async () => {
  function provider(dataStore, providerCollections, syncOverrides = {}) {
    const capabilities = {
      dataStore: true,
      networkOperations: false,
      syncCollections: [],
      syncDescriptor: [],
      syncDigest: "sync-v3:record-parent-state/1|",
      syncContract: "sync-v3",
      syncChangeContract: "record-parent-state/1",
      ...syncOverrides,
    };
    if (providerCollections !== undefined) capabilities.providerCollections = providerCollections;
    return {
      contract: "bw-reader-services/1",
      kind: "extension-services",
      dataStore,
      capabilities,
    };
  }
  const missing = createRuntime({
    documentHost: makeDocumentFixture("pdf").host,
    pwaStore: makeStore("advertised-missing-pwa"),
    ui: makeUiSpy(),
    scopes: TEST_SCOPES,
  });
  await assert.rejects(
    missing.attachExtension(provider(makeStore("advertised-missing-extension"))),
    (error) => error.code === "BW_RUNTIME_PROVIDER_REGISTRY",
  );
  assert.equal(missing.mode(), "pwa-fallback");

  const unknown = createRuntime({
    documentHost: makeDocumentFixture("epub").host,
    pwaStore: makeStore("advertised-unknown-pwa"),
    ui: makeUiSpy(),
    scopes: TEST_SCOPES,
  });
  await assert.rejects(
    unknown.attachExtension(provider(
      makeStore("advertised-unknown-extension"),
      [TEST_GLOBAL, "unknown-provider"],
    )),
    (error) => error.code === "BW_RUNTIME_PROVIDER_REGISTRY",
  );
  assert.equal(unknown.mode(), "pwa-fallback");

  for (const [label, syncOverrides] of [
    ["missing-descriptor", { syncDescriptor: undefined }],
    ["forged-digest", { syncDigest: "sync-v3:forged" }],
    ["missing-change-contract", { syncChangeContract: undefined }],
    ["unexpected-sync-collection", {
      syncCollections: [TEST_GLOBAL],
      syncDescriptor: [{
        name: TEST_GLOBAL,
        conflictPolicy: "explicit",
        derived: false,
        recordSchema: 1,
      }],
      syncDigest:
        `sync-v3:record-parent-state/1|${TEST_GLOBAL}:explicit:0:1`,
    }],
  ]) {
    const candidate = createRuntime({
      documentHost: makeDocumentFixture("html").host,
      pwaStore: makeStore(`advertised-${label}-pwa`),
      ui: makeUiSpy(),
      scopes: TEST_SCOPES,
    });
    await assert.rejects(
      candidate.attachExtension(provider(
        makeStore(`advertised-${label}-extension`),
        [TEST_GLOBAL],
        syncOverrides,
      )),
      (error) => error.code === "BW_RUNTIME_PROVIDER_REGISTRY",
      label,
    );
    assert.equal(candidate.mode(), "pwa-fallback");
  }

  const matching = createRuntime({
    documentHost: makeDocumentFixture("web").host,
    pwaStore: makeStore("advertised-matching-pwa"),
    ui: makeUiSpy(),
    scopes: TEST_SCOPES,
  });
  const result = await matching.attachExtension(provider(
    makeStore("advertised-matching-extension"),
    [TEST_GLOBAL],
  ));
  assert.equal(result.connected, true);
  assert.equal(matching.mode(), "pwa-extension-provider");
});

test("扩展晚到时先按稳定 ID 合并，再切 provider", async () => {
  const fixture = makeDocumentFixture("pdf");
  const pwaStore = makeStore("late-pwa");
  const extensionStore = makeStore("late-extension");
  const runtime = createRuntime({
    documentHost: fixture.host,
    pwaStore,
    ui: makeUiSpy(),
    scopes: TEST_SCOPES,
  });
  await pwaStore.put(TEST_GLOBAL, { id: "pwa-only" }, { mutationId: "pwa-only-op" });
  await extensionStore.put(TEST_GLOBAL, { id: "ext-only" }, { mutationId: "ext-only-op" });

  const result = await runtime.attachExtension({ dataStore: extensionStore });
  assert.equal(result.connected, true);
  assert.ok(await extensionStore.get(TEST_GLOBAL, "pwa-only"));
  assert.ok(await pwaStore.get(TEST_GLOBAL, "ext-only"));
  const extensionJournal = await extensionStore.changes({ after: 0 });
  assert.equal(
    extensionJournal.changes.some((change) => change.record.id === "pwa-only"),
    true,
    "接管导入扩展的记录必须留在扩展 journal，供后续服务器 push",
  );
});

test("扩展接管分页对账完整覆盖双方第 1001 条后的记录", async () => {
  const fixture = makeDocumentFixture("pdf");
  const common = Array.from({ length: 1000 }, (_, index) => ({
    id: `common-${String(index).padStart(4, "0")}`,
    text: `shared-${index}`,
    updatedAt: index + 1,
  }));
  const pwaStore = makeSeededStore("paged-pwa", [
    ...common,
    { id: "z-pwa-only", text: "PWA tail", updatedAt: 2001 },
  ]);
  const extensionStore = makeSeededStore("paged-extension", [
    ...common,
    { id: "z-extension-only", text: "Extension tail", updatedAt: 2001 },
  ]);
  const runtime = createRuntime({
    documentHost: fixture.host,
    pwaStore,
    ui: makeUiSpy(),
    scopes: TEST_SCOPES,
  });

  const result = await runtime.attachExtension({ dataStore: extensionStore });
  assert.equal(result.connected, true);
  assert.ok(await extensionStore.get(TEST_GLOBAL, "z-pwa-only"));
  assert.ok(await pwaStore.get(TEST_GLOBAL, "z-extension-only"));
  assert.equal((await pwaStore.list(TEST_GLOBAL, { limit: 1000, offset: 1000 })).length, 2);
  assert.equal((await extensionStore.list(TEST_GLOBAL, { limit: 1000, offset: 1000 })).length, 2);
  assert.deepEqual(result.reconciliation.copiedToPwa, [`${TEST_GLOBAL}/z-extension-only`]);
  assert.deepEqual(result.reconciliation.copiedToExtension, [`${TEST_GLOBAL}/z-pwa-only`]);

  const pwaJournal = await pwaStore.changes({ after: 0 });
  const extensionJournal = await extensionStore.changes({ after: 0 });
  assert.deepEqual(pwaJournal.changes.map((change) => change.record.id), ["z-extension-only"]);
  assert.deepEqual(extensionJournal.changes.map((change) => change.record.id), ["z-pwa-only"]);
});

test("同一稳定 ID 分叉时不擅自选边，也不切换 provider", async () => {
  const fixture = makeDocumentFixture("epub");
  const pwaStore = makeStore("conflict-pwa");
  const extensionStore = makeStore("conflict-extension");
  const runtime = createRuntime({
    documentHost: fixture.host,
    pwaStore,
    ui: makeUiSpy(),
    scopes: TEST_SCOPES,
  });
  await pwaStore.put(TEST_GLOBAL, { id: "same-card", answer: "PWA" }, { mutationId: "pwa-answer" });
  await extensionStore.put(TEST_GLOBAL, { id: "same-card", answer: "Extension" }, { mutationId: "ext-answer" });

  const result = await runtime.attachExtension({ dataStore: extensionStore });
  assert.equal(result.connected, false);
  assert.equal(result.conflicts[0].reason, "stable-id-diverged");
  assert.equal(runtime.mode(), "pwa-fallback");
  assert.equal((await pwaStore.get(TEST_GLOBAL, "same-card")).value.answer, "PWA");
  assert.equal((await extensionStore.get(TEST_GLOBAL, "same-card")).value.answer, "Extension");
});

test("业务内容相同但 envelope 元数据与 rev 不同时不误判冲突", async () => {
  const fixture = makeDocumentFixture("pdf");
  const pwaStore = makeStore("same-content-pwa");
  const extensionStore = makeStore("same-content-extension");
  const runtime = createRuntime({
    documentHost: fixture.host,
    pwaStore,
    ui: makeUiSpy(),
    scopes: TEST_SCOPES,
  });
  const value = { id: "shared-card", answer: "相同内容" };
  await pwaStore.put(TEST_GLOBAL, value, { mutationId: "pwa-shared-1" });
  await pwaStore.put(TEST_GLOBAL, value, { mutationId: "pwa-shared-2" });
  await extensionStore.put(TEST_GLOBAL, value, { mutationId: "extension-shared-1" });

  const beforePwa = await pwaStore.get(TEST_GLOBAL, "shared-card");
  const beforeExtension = await extensionStore.get(TEST_GLOBAL, "shared-card");
  assert.equal(beforePwa.rev, 2);
  assert.equal(beforeExtension.rev, 1);
  assert.notEqual(beforePwa.updatedBy, beforeExtension.updatedBy);

  const result = await runtime.attachExtension({ dataStore: extensionStore });
  assert.equal(result.connected, true);
  assert.deepEqual(result.conflicts, []);
  assert.equal(runtime.mode(), "pwa-extension-provider");

  const afterExtension = await extensionStore.get(TEST_GLOBAL, "shared-card");
  assert.equal(afterExtension.rev, 2);
  assert.equal(afterExtension.updatedBy, beforePwa.updatedBy);
  assert.deepEqual(afterExtension.value, value);
});

test("接管按 canonical 业务值比较对象键顺序，并把同 ID tombstone 视为等价", async () => {
  const pwaStore = makeStore("canonical-record-pwa");
  const extensionStore = makeStore("canonical-record-extension");
  await pwaStore.put(TEST_GLOBAL, {
    id: "ordered-record",
    nested: { beta: 2, alpha: 1 },
    enabled: true,
  }, { mutationId: "ordered-pwa" });
  await extensionStore.put(TEST_GLOBAL, {
    enabled: true,
    nested: { alpha: 1, beta: 2 },
    id: "ordered-record",
  }, { mutationId: "ordered-extension" });
  await pwaStore.put(
    TEST_GLOBAL,
    { id: "deleted-record", previous: "pwa" },
    { mutationId: "deleted-pwa-put" },
  );
  await extensionStore.put(
    TEST_GLOBAL,
    { previous: "extension", id: "deleted-record" },
    { mutationId: "deleted-extension-put" },
  );
  await pwaStore.remove(
    TEST_GLOBAL,
    "deleted-record",
    { mutationId: "deleted-pwa-remove" },
  );
  await extensionStore.remove(
    TEST_GLOBAL,
    "deleted-record",
    { mutationId: "deleted-extension-remove" },
  );
  const runtime = createRuntime({
    documentHost: makeDocumentFixture("pdf").host,
    pwaStore,
    ui: makeUiSpy(),
    scopes: TEST_SCOPES,
  });

  const result = await runtime.attachExtension({ dataStore: extensionStore });
  assert.equal(result.connected, true);
  assert.deepEqual(result.conflicts, []);
  assert.equal(runtime.mode(), "pwa-extension-provider");
  assert.equal(
    (await pwaStore.get(
      TEST_GLOBAL,
      "deleted-record",
      { includeDeleted: true },
    )).deleted,
    true,
  );
  assert.equal(
    (await extensionStore.get(
      TEST_GLOBAL,
      "deleted-record",
      { includeDeleted: true },
    )).deleted,
    true,
  );
});

test("扩展断开只切回 fallback，不重挂 UI、不更换 DocumentHost", async () => {
  const fixture = makeDocumentFixture("web");
  const ui = makeUiSpy();
  const runtime = createRuntime({
    documentHost: fixture.host,
    pwaStore: makeStore("detach-pwa"),
    ui,
    scopes: TEST_SCOPES,
  });
  await runtime.start();
  await runtime.attachExtension({ dataStore: makeStore("detach-extension") });
  const originalHost = runtime.documentHost();
  await runtime.detachExtension("test-disconnect");
  assert.equal(runtime.mode(), "pwa-fallback");
  assert.equal(runtime.documentHost(), originalHost);
  assert.equal(ui.mountCount, 1);
});

test("PWA document store 可独立于 global fallback，扩展接管时仍不切换", async () => {
  const fixture = makeDocumentFixture("pdf");
  const globalStore = makeStore("separate-global");
  const documentStore = makeStore("separate-document");
  const extensionStore = makeStore("separate-extension");
  const runtime = createRuntime({
    documentHost: fixture.host,
    pwaStore: globalStore,
    pwaDocumentStore: documentStore,
    ui: makeUiSpy(),
    scopes: TEST_SCOPES,
  });

  await runtime.attachExtension({ dataStore: extensionStore });
  await runtime.storage().put(
    TEST_DOCUMENT,
    { id: "document-anchor", documentId: fixture.documentId },
    { mutationId: "document-anchor-op" },
  );
  assert.ok(await documentStore.get(TEST_DOCUMENT, "document-anchor"));
  assert.equal(await globalStore.get(TEST_DOCUMENT, "document-anchor"), null);
  assert.equal(await extensionStore.get(TEST_DOCUMENT, "document-anchor"), null);
});

test("断线会提升 generation，旧异步 attach 完成后也不能重新接管", async () => {
  const fixture = makeDocumentFixture("pdf");
  const pwaStore = makeStore("generation-pwa");
  const extensionStore = makeStore("generation-extension");
  const runtime = createRuntime({
    documentHost: fixture.host,
    pwaStore,
    ui: makeUiSpy(),
    scopes: TEST_SCOPES,
  });
  let releaseReconcile;
  let markStarted;
  const started = new Promise((resolve) => { markStarted = resolve; });
  const blocked = new Promise((resolve) => { releaseReconcile = resolve; });

  const attaching = runtime.attachExtension({
    dataStore: extensionStore,
    async reconcile(input) {
      assert.deepEqual(input.collections, [TEST_GLOBAL]);
      assert.equal(input.registry[0].conflictPolicy, "explicit");
      markStarted();
      await blocked;
      return { conflicts: [] };
    },
  });
  await started;
  const detached = await runtime.detachExtension("port-closed");
  releaseReconcile();
  const staleResult = await attaching;

  assert.equal(detached.connected, false);
  assert.equal(staleResult.connected, false);
  assert.equal(staleResult.cancelled, true);
  assert.ok(detached.generation > staleResult.generation);
  assert.equal(runtime.mode(), "pwa-fallback");
  assert.equal((await runtime.status()).extensionConnected, false);
});

test("两阶段对账会先扫描全部 explicit collection，发现后置冲突时一条也不写", async () => {
  const scopes = {
    "a-settings": {
      scope: "global", status: "ready", provider: true, conflictPolicy: "explicit",
    },
    "z-settings": {
      scope: "global", status: "ready", provider: true, conflictPolicy: "explicit",
    },
  };
  const fixture = makeDocumentFixture("epub");
  const pwaStore = makeStore("two-phase-pwa");
  const extensionStore = makeStore("two-phase-extension");
  await pwaStore.put("a-settings", { id: "copy-if-safe", value: "PWA" }, { mutationId: "two-phase-copy" });
  await pwaStore.put("z-settings", { id: "conflict", value: "PWA" }, { mutationId: "two-phase-pwa-conflict" });
  await extensionStore.put("z-settings", { id: "conflict", value: "Extension" }, { mutationId: "two-phase-ext-conflict" });
  const runtime = createRuntime({
    documentHost: fixture.host,
    pwaStore,
    ui: makeUiSpy(),
    scopes,
  });

  const result = await runtime.attachExtension({ dataStore: extensionStore });
  assert.equal(result.connected, false);
  assert.equal(result.conflicts[0].collection, "z-settings");
  assert.equal(await extensionStore.get("a-settings", "copy-if-safe"), null);
  assert.equal(runtime.mode(), "pwa-fallback");
});

test("derived regenerate 分叉不阻断接管，也不会覆盖 explicit 用户设置", async () => {
  const scopes = {
    "derived-cache": {
      scope: "global",
      status: "ready",
      provider: true,
      conflictPolicy: "regenerate",
      derived: true,
    },
    "user-settings-test": {
      scope: "global",
      status: "ready",
      provider: true,
      conflictPolicy: "explicit",
      derived: false,
    },
  };
  const fixture = makeDocumentFixture("web");
  const pwaStore = makeStore("derived-pwa");
  const extensionStore = makeStore("derived-extension");
  await pwaStore.put("derived-cache", { id: "query-1", answer: "PWA cache" }, { mutationId: "derived-pwa-cache" });
  await extensionStore.put("derived-cache", { id: "query-1", answer: "Extension cache" }, { mutationId: "derived-ext-cache" });
  await pwaStore.put("user-settings-test", { id: "theme", value: "dark" }, { mutationId: "derived-setting" });
  const runtime = createRuntime({
    documentHost: fixture.host,
    pwaStore,
    ui: makeUiSpy(),
    scopes,
  });

  const result = await runtime.attachExtension({ dataStore: extensionStore });
  assert.equal(result.connected, true);
  assert.deepEqual(result.conflicts, []);
  assert.deepEqual(result.reconciliation.regenerated.map(({ collection, id, strategy }) => ({
    collection, id, strategy,
  })), [{
    collection: "derived-cache",
    id: "query-1",
    strategy: "keep-both-regenerate-on-demand",
  }]);
  assert.equal((await pwaStore.get("derived-cache", "query-1")).value.answer, "PWA cache");
  assert.equal((await extensionStore.get("derived-cache", "query-1")).value.answer, "Extension cache");
  assert.equal((await extensionStore.get("user-settings-test", "theme")).value.value, "dark");
});

test("只有 provider=true 的 ready global collection 进入接管对账", async () => {
  const scopes = {
    "provider-data": {
      scope: "global", status: "ready", provider: true, conflictPolicy: "explicit",
    },
    "pwa-only-global": {
      scope: "global", status: "ready", provider: false, conflictPolicy: "explicit",
    },
  };
  const fixture = makeDocumentFixture("pdf");
  const pwaStore = makeStore("provider-filter-pwa");
  const extensionStore = makeStore("provider-filter-extension");
  await pwaStore.put("provider-data", { id: "shared" }, { mutationId: "provider-data-op" });
  await pwaStore.put("pwa-only-global", { id: "local" }, { mutationId: "pwa-only-op" });
  const runtime = createRuntime({
    documentHost: fixture.host,
    pwaStore,
    ui: makeUiSpy(),
    scopes,
  });

  const result = await runtime.attachExtension({ dataStore: extensionStore });
  assert.equal(result.connected, true);
  assert.ok(await extensionStore.get("provider-data", "shared"));
  assert.equal(await extensionStore.get("pwa-only-global", "local"), null);
});

test("对账写入按最多 100 条分批，并把 applyChanges 冲突作为接管失败", async () => {
  const fixture = makeDocumentFixture("pdf");
  const pwaStore = makeSeededStore(
    "batch-pwa",
    Array.from({ length: 205 }, (_, index) => ({
      id: `batch-${String(index).padStart(3, "0")}`,
      text: `record-${index}`,
    })),
  );
  const extensionStore = makeStore("batch-extension");
  const originalApply = extensionStore.applyChanges;
  const batchSizes = [];
  extensionStore.applyChanges = async (changes, opts) => {
    batchSizes.push(changes.length);
    return originalApply(changes, opts);
  };
  const runtime = createRuntime({
    documentHost: fixture.host,
    pwaStore,
    ui: makeUiSpy(),
    scopes: TEST_SCOPES,
  });

  const result = await runtime.attachExtension({ dataStore: extensionStore });
  assert.equal(result.connected, true);
  assert.deepEqual(batchSizes, [100, 100, 5]);

  const conflictPwa = makeStore("apply-conflict-pwa");
  const conflictExtension = makeStore("apply-conflict-extension");
  await conflictPwa.put(TEST_GLOBAL, { id: "race" }, { mutationId: "race-source" });
  conflictExtension.applyChanges = async () => ({
    applied: [],
    skipped: [],
    conflicts: [{ collection: TEST_GLOBAL, id: "race", reason: "same-rev-different-value" }],
  });
  const conflictRuntime = createRuntime({
    documentHost: fixture.host,
    pwaStore: conflictPwa,
    ui: makeUiSpy(),
    scopes: TEST_SCOPES,
  });
  const conflictResult = await conflictRuntime.attachExtension({ dataStore: conflictExtension });
  assert.equal(conflictResult.connected, false);
  assert.equal(conflictResult.conflicts[0].reason, "apply-conflict");
  assert.equal(conflictResult.conflicts[0].target, "extension");
  assert.equal(conflictRuntime.mode(), "pwa-fallback");
});

test("detach 会排空已经接受的镜像队列，返回后 fallback 不缺最后变化", async () => {
  const fixture = makeDocumentFixture("pdf");
  const pwaStore = makeStore("mirror-drain-pwa");
  const extensionStore = makeStore("mirror-drain-extension");
  const originalApply = pwaStore.applyChanges;
  let releaseFirst;
  let markFirstStarted;
  const firstStarted = new Promise((resolve) => { markFirstStarted = resolve; });
  const firstGate = new Promise((resolve) => { releaseFirst = resolve; });
  let applyCalls = 0;
  pwaStore.applyChanges = async (changes, opts) => {
    applyCalls += 1;
    if (applyCalls === 1) {
      markFirstStarted();
      await firstGate;
    }
    return originalApply(changes, opts);
  };
  const runtime = createRuntime({
    documentHost: fixture.host,
    pwaStore,
    ui: makeUiSpy(),
    scopes: TEST_SCOPES,
  });
  await runtime.attachExtension({ dataStore: extensionStore });

  await extensionStore.put(TEST_GLOBAL, { id: "queued-first" }, { mutationId: "queued-first-op" });
  await firstStarted;
  await extensionStore.put(TEST_GLOBAL, { id: "queued-second" }, { mutationId: "queued-second-op" });
  let detachSettled = false;
  const detaching = runtime.detachExtension("provider-port-closed").then((result) => {
    detachSettled = true;
    return result;
  });
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(detachSettled, false, "detach 不能早于已接受镜像落盘返回");

  releaseFirst();
  const result = await detaching;
  assert.equal(result.connected, false);
  assert.equal(runtime.mode(), "pwa-fallback");
  assert.ok(await pwaStore.get(TEST_GLOBAL, "queued-first"));
  assert.ok(await pwaStore.get(TEST_GLOBAL, "queued-second"));
});

test("新 provider 对账会等待旧 provider 的镜像队列，并清理旧订阅", async () => {
  const fixture = makeDocumentFixture("epub");
  const pwaStore = makeStore("mirror-switch-pwa");
  const firstExtension = makeStore("mirror-switch-first");
  const secondExtension = makeStore("mirror-switch-second");
  function countSubscriptions(store) {
    const originalSubscribe = store.subscribe;
    let active = 0;
    let maximum = 0;
    store.subscribe = (...args) => {
      active += 1;
      maximum = Math.max(maximum, active);
      const unsubscribe = originalSubscribe(...args);
      return () => {
        if (active > 0) active -= 1;
        unsubscribe();
      };
    };
    return {
      active: () => active,
      maximum: () => maximum,
    };
  }
  const firstSubscriptions = countSubscriptions(firstExtension);
  const secondSubscriptions = countSubscriptions(secondExtension);
  const originalPwaApply = pwaStore.applyChanges;
  let releaseMirror;
  let markMirrorStarted;
  const mirrorStarted = new Promise((resolve) => { markMirrorStarted = resolve; });
  const mirrorGate = new Promise((resolve) => { releaseMirror = resolve; });
  let blockNextMirror = false;
  pwaStore.applyChanges = async (changes, opts) => {
    if (blockNextMirror) {
      blockNextMirror = false;
      markMirrorStarted();
      await mirrorGate;
    }
    return originalPwaApply(changes, opts);
  };
  let secondListCalls = 0;
  const originalSecondList = secondExtension.list;
  secondExtension.list = (...args) => {
    secondListCalls += 1;
    return originalSecondList(...args);
  };
  const runtime = createRuntime({
    documentHost: fixture.host,
    pwaStore,
    ui: makeUiSpy(),
    scopes: TEST_SCOPES,
  });
  await runtime.attachExtension({ dataStore: firstExtension });
  assert.equal(firstSubscriptions.active(), 1);

  blockNextMirror = true;
  await firstExtension.put(TEST_GLOBAL, { id: "from-first" }, { mutationId: "from-first-op" });
  await mirrorStarted;
  const switching = runtime.attachExtension({ dataStore: secondExtension });
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(firstSubscriptions.active(), 0);
  assert.equal(secondListCalls, 0, "旧镜像未落盘前不得开始新 provider 对账");

  releaseMirror();
  const result = await switching;
  assert.equal(result.connected, true);
  assert.ok(await secondExtension.get(TEST_GLOBAL, "from-first"));
  assert.equal(firstSubscriptions.maximum(), 1);
  assert.equal(secondSubscriptions.active(), 1);
  assert.equal(secondSubscriptions.maximum(), 1);

  await runtime.detachExtension("done");
  assert.equal(secondSubscriptions.active(), 0);
});

test("持续镜像只复制 applyChanges 真正 applied 的记录，并按 100 条分批", async () => {
  const fixture = makeDocumentFixture("web");
  const pwaStore = makeStore("mirror-apply-pwa");
  const extensionStore = makeStore("mirror-apply-extension");
  /* 隔离 direct-apply 路径；真实 provider 的 CHANGE 广播是另一条已覆盖路径。 */
  extensionStore.subscribe = () => () => {};
  const originalExtensionApply = extensionStore.applyChanges;
  let skipSecond = true;
  extensionStore.applyChanges = async (changes, opts) => {
    if (!skipSecond) return originalExtensionApply(changes, opts);
    skipSecond = false;
    const first = await originalExtensionApply([changes[0]], opts);
    return {
      applied: first.applied,
      conflicts: [],
      skipped: [changes[1].mutationId],
    };
  };
  const originalPwaApply = pwaStore.applyChanges;
  const mirrorBatchSizes = [];
  pwaStore.applyChanges = (changes, opts) => {
    mirrorBatchSizes.push(changes.length);
    return originalPwaApply(changes, opts);
  };
  const runtime = createRuntime({
    documentHost: fixture.host,
    pwaStore,
    ui: makeUiSpy(),
    scopes: TEST_SCOPES,
  });
  await runtime.attachExtension({ dataStore: extensionStore });
  mirrorBatchSizes.length = 0;

  function incoming(id, rev = 1) {
    return {
      collection: TEST_GLOBAL,
      mutationId: `incoming-${id}`,
      record: {
        schema: 1,
        collection: TEST_GLOBAL,
        id,
        rev,
        updatedAt: rev,
        updatedBy: "remote",
        deleted: false,
        value: { id },
      },
    };
  }
  const firstResult = await runtime.storage().applyChanges("global", [
    incoming("applied"),
    incoming("skipped"),
  ]);
  assert.deepEqual(firstResult.applied.map((item) => item.id), ["applied"]);
  assert.ok(await pwaStore.get(TEST_GLOBAL, "applied"));
  assert.equal(await pwaStore.get(TEST_GLOBAL, "skipped"), null);

  mirrorBatchSizes.length = 0;
  const bulk = Array.from({ length: 205 }, (_, index) => incoming(`bulk-${index}`));
  await runtime.storage().applyChanges("global", bulk);
  assert.deepEqual(mirrorBatchSizes, [100, 100, 5]);
});

test("镜像订阅安装失败会回滚到 PWA store，不留下半接管状态", async () => {
  const fixture = makeDocumentFixture("pdf");
  const pwaStore = makeStore("mirror-rollback-pwa");
  const extensionStore = makeStore("mirror-rollback-extension");
  extensionStore.subscribe = () => {
    throw new Error("subscribe unavailable");
  };
  const runtime = createRuntime({
    documentHost: fixture.host,
    pwaStore,
    ui: makeUiSpy(),
    scopes: TEST_SCOPES,
  });

  await assert.rejects(
    runtime.attachExtension({ dataStore: extensionStore }),
    /subscribe unavailable/,
  );
  assert.equal(runtime.mode(), "pwa-fallback");
  assert.equal((await runtime.status()).extensionConnected, false);
  await runtime.storage().put(TEST_GLOBAL, { id: "after-rollback" }, { mutationId: "rollback-op" });
  assert.ok(await pwaStore.get(TEST_GLOBAL, "after-rollback"));
  assert.equal(await extensionStore.get(TEST_GLOBAL, "after-rollback"), null);
});

test("订阅镜像落盘异常不会静默消失，会进入 status 与 detach 报告", async () => {
  const fixture = makeDocumentFixture("pdf");
  const pwaStore = makeStore("mirror-error-pwa");
  const extensionStore = makeStore("mirror-error-extension");
  const runtime = createRuntime({
    documentHost: fixture.host,
    pwaStore,
    ui: makeUiSpy(),
    scopes: TEST_SCOPES,
  });
  await runtime.attachExtension({ dataStore: extensionStore });
  const originalPwaApply = pwaStore.applyChanges;
  let markAttempted;
  const attempted = new Promise((resolve) => { markAttempted = resolve; });
  pwaStore.applyChanges = async () => {
    markAttempted();
    const error = new Error("IndexedDB unavailable");
    error.code = "IDB_CLOSED";
    throw error;
  };

  await extensionStore.put(TEST_GLOBAL, { id: "mirror-error" }, { mutationId: "mirror-error-op" });
  await attempted;
  await Promise.resolve();
  await Promise.resolve();
  const status = await runtime.status();
  assert.equal(status.mirrorConflicts.at(-1).details.reason, "mirror-write-failed");
  assert.equal(status.mirrorConflicts.at(-1).details.code, "IDB_CLOSED");
  assert.equal(status.mirrorConflicts.at(-1).id, "mirror-error");

  const detached = await runtime.detachExtension("storage-error");
  assert.equal(detached.mirrorConflicts.at(-1).details.message, "IndexedDB unavailable");
  assert.equal(detached.mode, "pwa-fallback-conflict");
  assert.equal(runtime.mode(), "pwa-fallback-conflict");

  pwaStore.applyChanges = originalPwaApply;
  const healed = await runtime.attachExtension({ dataStore: extensionStore });
  assert.equal(healed.connected, true);
  const detachedAfterReconcile = await runtime.detachExtension("healed");
  assert.equal(detachedAfterReconcile.mirrorConflicts.length, 0);
  assert.equal(detachedAfterReconcile.mode, "pwa-fallback");
});
