import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import {
  DataRegistry,
  DataStore,
  StorageRouter,
  makeStore,
} from "./helpers.mjs";

const require = createRequire(import.meta.url);
const DocumentNotes = require(
  "../../_server_deploy/static/reader-runtime/document-note-repository.js",
);

const NOTE_A = `c_${"a".repeat(32)}`;
const NOTE_B = `c_${"b".repeat(32)}`;

function anchor(documentId, data = {}) {
  return {
    documentId,
    kind: "pdf-char",
    revision: 1,
    data: {
      page: 7,
      hostPrivate: {
        boxes: [[1, 2, 3, 4]],
        ...structuredClone(data),
      },
    },
  };
}

function repository(name = "notes", options = {}) {
  const store = options.store || makeStore(name);
  const repo = DocumentNotes.createDocumentNoteRepository({
    store,
    dataRegistry: DataRegistry,
    idFactory: options.idFactory || (() => NOTE_A),
  });
  return { repo, store };
}

test("document-notes 注册为 PWA document 数据且不会进入扩展 provider", async () => {
  const declaration = DataRegistry.collection("document-notes");
  assert.equal(declaration.status, "ready");
  assert.equal(declaration.scope, "document");
  assert.equal(declaration.provider, false);
  assert.equal(
    DataRegistry.providerCollections().includes("document-notes"),
    false,
  );

  const globalStore = makeStore("note-router-global");
  const documentStore = makeStore("note-router-document");
  const deviceStore = makeStore("note-router-device");
  const router = StorageRouter.createStorageRouter({
    globalStore,
    documentStore,
    deviceStore,
    dataRegistryApi: DataRegistry,
  });
  const repo = DocumentNotes.createDocumentNoteRepository({
    store: router,
    dataRegistry: DataRegistry,
    idFactory: () => NOTE_A,
  });
  const created = await repo.create({
    documentId: "doc:router",
    anchor: anchor("doc:router"),
    text: "只应进入 document store",
  }, { mutationId: "router-create" });

  const storageId = repo.storageIdFor(created.documentId, created.noteId);
  assert.ok(await documentStore.get("document-notes", storageId));
  assert.equal(await globalStore.get("document-notes", storageId), null);
  assert.equal(await deviceStore.get("document-notes", storageId), null);
});

test("repository 对 data-registry 的 provider=false 采用 fail-closed 校验", () => {
  for (const provider of [undefined, null, "false", true]) {
    const declaration = {
      scope: "document",
      status: "ready",
    };
    if (provider !== undefined) declaration.provider = provider;
    const registry = {
      CONTRACT: "data-registry/1",
      collection: () => declaration,
    };
    assert.throws(
      () => DocumentNotes.createDocumentNoteRepository({
        store: makeStore(`registry-${String(provider)}`),
        dataRegistry: registry,
      }),
      (error) => error.code === "BW_NOTE_REGISTRY",
      `provider=${String(provider)} 必须被拒绝`,
    );
  }
});

test("业务身份是 documentId/noteId，客户端编号固定为 c_ 加 32 位 hex", async () => {
  const { repo, store } = repository("identity");
  const firstAnchor = anchor("doc:a", {
    epubCfi: "/6/4[chapter]!/4/2/8",
    custom: ["不解释", { x: 0.25 }],
  });
  const first = await repo.create({
    documentId: "doc:a",
    anchor: firstAnchor,
    text: "A 文档便签",
    color: "#ffd54a",
  }, { mutationId: "identity-create-a" });
  assert.match(first.noteId, /^c_[a-f0-9]{32}$/);
  assert.equal(first.id, first.noteId);
  assert.deepEqual(first.anchor, firstAnchor);

  const second = await repo.create({
    documentId: "doc:b",
    noteId: first.noteId,
    anchor: anchor("doc:b"),
    text: "B 文档同 noteId",
  }, { mutationId: "identity-create-b" });
  assert.equal(second.noteId, first.noteId);
  assert.equal((await repo.list("doc:a")).length, 1);
  assert.equal((await repo.list("doc:b")).length, 1);
  assert.equal((await store.list("document-notes")).length, 2);
  assert.notEqual(
    repo.storageIdFor("doc:a", first.noteId),
    repo.storageIdFor("doc:b", second.noteId),
  );

  first.anchor.data.hostPrivate.custom[1].x = 99;
  first.text = "外部篡改";
  const persisted = await repo.get("doc:a", first.noteId);
  assert.equal(persisted.text, "A 文档便签");
  assert.equal(persisted.anchor.data.hostPrivate.custom[1].x, 0.25);

  const invalid = repository("invalid-id", {
    idFactory: () => "c_not-secure",
  }).repo;
  await assert.rejects(
    invalid.create({
      documentId: "doc:invalid",
      anchor: anchor("doc:invalid"),
    }),
    (error) => error.code === "BW_NOTE_RANDOM",
  );
  await assert.rejects(
    repo.create({
      documentId: "doc:invalid",
      noteId: "legacy_note_id",
      anchor: anchor("doc:invalid"),
    }),
    (error) => error.code === "BW_NOTE_IDENTITY",
  );
});

test("newNoteId 是渲染前取号的唯一同步入口，create 复用同一实例 ID 源", async () => {
  const firstPublicId = DocumentNotes.newNoteId();
  const secondPublicId = DocumentNotes.newNoteId();
  assert.match(firstPublicId, /^c_[a-f0-9]{32}$/);
  assert.match(secondPublicId, /^c_[a-f0-9]{32}$/);
  assert.notEqual(firstPublicId, secondPublicId);

  const allocated = [NOTE_A, NOTE_B];
  const repo = DocumentNotes.createDocumentNoteRepository({
    store: makeStore("new-note-id"),
    dataRegistry: DataRegistry,
    idFactory: () => allocated.shift(),
  });
  assert.equal(repo.newNoteId(), NOTE_A);
  const created = await repo.create({
    documentId: "doc:new-note-id",
    anchor: anchor("doc:new-note-id"),
    text: "create 必须走剩余的同一取号源",
  }, { mutationId: "new-note-id-create" });
  assert.equal(created.noteId, NOTE_B);
  assert.equal(allocated.length, 0);
});

test("anchor 只验证 envelope，宿主 data 原样保存且不能跨 document", async () => {
  const { repo } = repository("anchor");
  const opaque = anchor("doc:anchor", {
    page: "并非仓库关心的类型",
    nested: { section: 3, quote: "原样保留" },
  });
  const created = await repo.create({
    documentId: "doc:anchor",
    noteId: NOTE_A,
    anchor: opaque,
    text: "opaque",
  }, { mutationId: "anchor-create" });
  assert.deepEqual(created.anchor.data, opaque.data);

  await assert.rejects(
    repo.create({
      documentId: "doc:anchor",
      noteId: NOTE_B,
      anchor: anchor("doc:other"),
    }),
    (error) => error.code === "BW_NOTE_ANCHOR",
  );
  await assert.rejects(
    repo.create({
      documentId: "doc:anchor",
      noteId: NOTE_B,
      anchor: {
        documentId: "doc:anchor",
        kind: "pdf-char",
        revision: 1,
      },
    }),
    (error) => error.code === "BW_NOTE_ANCHOR",
  );
});

test("重建 repository 后仍从同一 DataStore 恢复完整便签内容", async () => {
  const backend = DataStore.createMemoryBackend();
  const firstStore = DataStore.createDataStore({
    backend,
    deviceId: "note-restart-first",
  });
  const firstRepo = DocumentNotes.createDocumentNoteRepository({
    store: firstStore,
    dataRegistry: DataRegistry,
    idFactory: () => NOTE_A,
  });
  await firstRepo.create({
    documentId: "doc:restart",
    anchor: anchor("doc:restart"),
    text: "重启后保留",
    color: "#123456",
    w: 320,
    h: 240,
    collapsed: true,
    strokes: [{ color: "#f00", points: [[1, 2], [3, 4]] }],
    video: { id: "video-1", start: 12, loop: true },
    card: { cid: "card-1", cards: [{ q: "Q", a: "A" }] },
    html: { cid: "html-1", content: "<b>opaque payload</b>" },
  }, { mutationId: "restart-create" });

  const secondStore = DataStore.createDataStore({
    backend,
    deviceId: "note-restart-second",
  });
  const secondRepo = DocumentNotes.createDocumentNoteRepository({
    store: secondStore,
    dataRegistry: DataRegistry,
  });
  const restored = await secondRepo.get("doc:restart", NOTE_A);
  assert.equal(restored.text, "重启后保留");
  assert.equal(restored.collapsed, true);
  assert.deepEqual(restored.strokes[0].points, [[1, 2], [3, 4]]);
  assert.equal(restored.video.start, 12);
  assert.equal(restored.card.cards[0].a, "A");
  assert.equal(restored.html.content, "<b>opaque payload</b>");
});

test("mutationId 重放幂等，订阅按 documentId 隔离", async () => {
  const { repo } = repository("mutation");
  const events = [];
  const otherEvents = [];
  const unsubscribe = repo.subscribe("doc:mutation", (event) => events.push(event));
  const unsubscribeOther = repo.subscribe(
    "doc:other",
    (event) => otherEvents.push(event),
  );
  const input = {
    documentId: "doc:mutation",
    noteId: NOTE_A,
    anchor: anchor("doc:mutation"),
    text: "v1",
  };
  const first = await repo.create(input, { mutationId: "create-once" });
  const replay = await repo.create(input, { mutationId: "create-once" });
  assert.equal(replay.rev, first.rev);

  const patched = await repo.patch(
    "doc:mutation",
    NOTE_A,
    { text: "v2" },
    { ifRev: first.rev, mutationId: "patch-once" },
  );
  const patchReplay = await repo.patch(
    "doc:mutation",
    NOTE_A,
    { text: "v2" },
    { ifRev: first.rev, mutationId: "patch-once" },
  );
  assert.equal(patchReplay.rev, patched.rev);
  assert.equal(events.length, 2, "create/patch 各只通知一次");
  assert.deepEqual(
    events.map((event) => [event.operation, event.note.text]),
    [["put", "v1"], ["put", "v2"]],
  );
  assert.equal(otherEvents.length, 0);

  await assert.rejects(
    repo.patch(
      "doc:mutation",
      NOTE_A,
      { text: "mutationId 被错误复用" },
      { ifRev: first.rev, mutationId: "patch-once" },
    ),
    (error) =>
      error.code === "BW_NOTE_CONFLICT" &&
      error.details.reason === "mutation-reused",
  );
  unsubscribe();
  unsubscribeOther();
});

test("不同字段可在 latest 上重放，同字段及整个 anchor 冲突显式失败", async () => {
  const { repo } = repository("fields");
  const created = await repo.create({
    documentId: "doc:fields",
    noteId: NOTE_A,
    anchor: anchor("doc:fields"),
    text: "原文",
    color: "yellow",
  }, { mutationId: "fields-create" });

  const textChanged = await repo.patch(
    "doc:fields",
    NOTE_A,
    { text: "设备 A 修改" },
    { ifRev: created.rev, mutationId: "fields-text-a" },
  );
  const safelyReplayed = await repo.patch(
    "doc:fields",
    NOTE_A,
    { color: "blue" },
    { ifRev: created.rev, mutationId: "fields-color-b" },
  );
  assert.equal(safelyReplayed.rev, textChanged.rev + 1);
  assert.equal(safelyReplayed.text, "设备 A 修改");
  assert.equal(safelyReplayed.color, "blue");
  const newFieldReplayed = await repo.patch(
    "doc:fields",
    NOTE_A,
    { collapsed: true },
    { ifRev: created.rev, mutationId: "fields-new-field-c" },
  );
  assert.equal(newFieldReplayed.collapsed, true);
  assert.equal(newFieldReplayed.text, "设备 A 修改");

  await assert.rejects(
    repo.patch(
      "doc:fields",
      NOTE_A,
      { text: "设备 C 的旧修改" },
      { ifRev: created.rev, mutationId: "fields-text-c" },
    ),
    (error) =>
      error.code === "BW_NOTE_CONFLICT" &&
      error.details.reason === "same-field" &&
      error.details.fields.includes("fields.text"),
  );

  const anchorChanged = await repo.patch(
    "doc:fields",
    NOTE_A,
    { anchor: anchor("doc:fields", { moved: "A" }) },
    { ifRev: safelyReplayed.rev, mutationId: "fields-anchor-a" },
  );
  await assert.rejects(
    repo.patch(
      "doc:fields",
      NOTE_A,
      { anchor: anchor("doc:fields", { moved: "B" }) },
      { ifRev: safelyReplayed.rev, mutationId: "fields-anchor-b" },
    ),
    (error) =>
      error.code === "BW_NOTE_CONFLICT" &&
      error.details.fields.includes("anchor") &&
      error.details.actualRev === anchorChanged.rev,
  );
});

test("删除写 tombstone，旧 patch 及任意 revision 的活动远端记录都不能复活", async () => {
  const { repo, store } = repository("tombstone");
  const events = [];
  repo.subscribe("doc:tombstone", (event) => events.push(event));
  const created = await repo.create({
    documentId: "doc:tombstone",
    noteId: NOTE_A,
    anchor: anchor("doc:tombstone"),
    text: "待删除",
  }, { mutationId: "tombstone-create" });
  const [oldRawRecord] = await store.list("document-notes", {
    documentId: "doc:tombstone",
  });

  const removed = await repo.remove(
    "doc:tombstone",
    NOTE_A,
    { ifRev: created.rev, mutationId: "tombstone-remove" },
  );
  assert.equal(removed.deleted, true);
  assert.equal(await repo.get("doc:tombstone", NOTE_A), null);
  assert.equal(
    (await repo.get("doc:tombstone", NOTE_A, { includeDeleted: true })).deleted,
    true,
  );
  assert.equal(events.at(-1).operation, "remove");

  const removeReplay = await repo.remove(
    "doc:tombstone",
    NOTE_A,
    { ifRev: created.rev, mutationId: "tombstone-remove" },
  );
  assert.equal(removeReplay.rev, removed.rev);
  await assert.rejects(
    repo.patch(
      "doc:tombstone",
      NOTE_A,
      { text: "旧页面试图复活" },
      { ifRev: created.rev, mutationId: "tombstone-stale-patch" },
    ),
    (error) =>
      error.code === "BW_NOTE_CONFLICT" &&
      error.details.reason === "deleted",
  );

  const currentTombstone = await store.get(
    "document-notes",
    repo.storageIdFor("doc:tombstone", NOTE_A),
    { includeDeleted: true },
  );
  const newerTombstone = structuredClone(currentTombstone);
  newerTombstone.rev = removed.rev + 20;
  newerTombstone.updatedAt = removed.updatedAt + 20;
  newerTombstone.updatedBy = "remote-delete-device";
  const newerDeleteResult = await repo.applyChanges([{
    collection: "document-notes",
    mutationId: "tombstone-newer-remote-delete",
    record: newerTombstone,
  }]);
  assert.equal(newerDeleteResult.applied.length, 1);
  const replayAfterNewerDelete = await repo.remove(
    "doc:tombstone",
    NOTE_A,
    { ifRev: created.rev, mutationId: "tombstone-remove" },
  );
  assert.equal(
    replayAfterNewerDelete.rev,
    newerTombstone.rev,
    "删除 mutation 重放必须返回当前最新 tombstone，而非缓存的旧 revision",
  );

  const applied = await store.applyChanges([{
    collection: "document-notes",
    mutationId: "tombstone-stale-remote",
    record: oldRawRecord,
  }]);
  assert.equal(applied.applied.length, 0);
  assert.equal(applied.conflicts[0].reason, "stale-incoming");
  assert.equal(
    (await repo.get("doc:tombstone", NOTE_A, { includeDeleted: true })).deleted,
    true,
  );

  const forgedHigherRev = structuredClone(oldRawRecord);
  forgedHigherRev.rev = removed.rev + 100;
  forgedHigherRev.updatedAt = removed.updatedAt + 100;
  forgedHigherRev.updatedBy = "stale-device-with-higher-rev";
  forgedHigherRev.deleted = false;
  forgedHigherRev.value.fields.text = "更高 revision 也不允许复活";
  const guarded = await repo.applyChanges([{
    collection: "document-notes",
    mutationId: "tombstone-higher-rev-active",
    record: forgedHigherRev,
  }]);
  assert.equal(guarded.applied.length, 0);
  assert.equal(guarded.conflicts.length, 1);
  assert.equal(guarded.conflicts[0].reason, "tombstone-dominates");
  assert.equal(
    (await repo.get("doc:tombstone", NOTE_A, { includeDeleted: true })).deleted,
    true,
  );
});

test("同批次 tombstone 后的更高 revision 活动记录也不能复活便签", async () => {
  const { repo, store } = repository("tombstone-same-batch");
  await repo.create({
    documentId: "doc:tombstone-batch",
    noteId: NOTE_A,
    anchor: anchor("doc:tombstone-batch"),
    text: "active-v1",
  }, { mutationId: "tombstone-batch-create" });
  const original = await store.get(
    "document-notes",
    repo.storageIdFor("doc:tombstone-batch", NOTE_A),
    { includeDeleted: true },
  );
  const tombstone = structuredClone(original);
  tombstone.rev = 2;
  tombstone.updatedAt += 1;
  tombstone.updatedBy = "remote-delete";
  tombstone.deleted = true;
  const attemptedRevival = structuredClone(original);
  attemptedRevival.rev = 3;
  attemptedRevival.updatedAt += 2;
  attemptedRevival.updatedBy = "stale-active-device";
  attemptedRevival.value.fields.text = "不应复活";

  const result = await repo.applyChanges([
    {
      collection: "document-notes",
      mutationId: "tombstone-batch-delete",
      record: tombstone,
    },
    {
      collection: "document-notes",
      mutationId: "tombstone-batch-revival",
      record: attemptedRevival,
    },
  ]);
  assert.equal(result.applied.length, 1);
  assert.equal(result.applied[0].rev, tombstone.rev);
  assert.equal(result.conflicts.length, 1);
  assert.equal(result.conflicts[0].reason, "tombstone-dominates");
  const final = await repo.get(
    "doc:tombstone-batch",
    NOTE_A,
    { includeDeleted: true },
  );
  assert.equal(final.deleted, true);
  assert.equal(final.rev, tombstone.rev);
});

test("两个 repository 交错 apply/remove 时，底层原子 tombstone 守卫阻止 TOCTOU 复活", async () => {
  const store = makeStore("tombstone-cross-repository");
  let enterApply;
  let releaseApply;
  const entered = new Promise((resolve) => {
    enterApply = resolve;
  });
  const released = new Promise((resolve) => {
    releaseApply = resolve;
  });
  const delayedStore = {
    contract: store.contract,
    get: (...args) => store.get(...args),
    list: (...args) => store.list(...args),
    put: (...args) => store.put(...args),
    remove: (...args) => store.remove(...args),
    subscribe: (...args) => store.subscribe(...args),
    applyChanges: async (...args) => {
      enterApply();
      await released;
      return store.applyChanges(...args);
    },
  };
  const applyingRepo = DocumentNotes.createDocumentNoteRepository({
    store: delayedStore,
    dataRegistry: DataRegistry,
    idFactory: () => NOTE_A,
  });
  const deletingRepo = DocumentNotes.createDocumentNoteRepository({
    store,
    dataRegistry: DataRegistry,
    idFactory: () => NOTE_A,
  });
  const created = await deletingRepo.create({
    documentId: "doc:tombstone-race",
    noteId: NOTE_A,
    anchor: anchor("doc:tombstone-race"),
    text: "active",
  }, { mutationId: "tombstone-race-create" });
  const staleActive = await store.get(
    "document-notes",
    deletingRepo.storageIdFor("doc:tombstone-race", NOTE_A),
    { includeDeleted: true },
  );
  staleActive.rev = 100;
  staleActive.updatedAt += 100;
  staleActive.updatedBy = "stale-active-device";
  staleActive.value.fields.text = "不应在删除后复活";

  const pendingApply = applyingRepo.applyChanges([{
    collection: "document-notes",
    mutationId: "tombstone-race-active",
    record: staleActive,
  }]);
  await entered;
  await deletingRepo.remove(
    "doc:tombstone-race",
    NOTE_A,
    { ifRev: created.rev, mutationId: "tombstone-race-remove" },
  );
  releaseApply();
  const result = await pendingApply;

  assert.equal(result.applied.length, 0);
  assert.equal(result.conflicts.length, 1);
  assert.equal(result.conflicts[0].reason, "tombstone-dominates");
  const final = await deletingRepo.get(
    "doc:tombstone-race",
    NOTE_A,
    { includeDeleted: true },
  );
  assert.equal(final.deleted, true);
  assert.equal(final.rev, 2);
});

test("远端变更字段的伪造旧 fieldRev 会被提升到 incoming revision", async () => {
  const { repo, store } = repository("remote-field-revs");
  const created = await repo.create({
    documentId: "doc:remote-field-revs",
    noteId: NOTE_A,
    anchor: anchor("doc:remote-field-revs"),
    text: "本地 v1",
    color: "yellow",
  }, { mutationId: "remote-field-revs-create" });
  const remote = await store.get(
    "document-notes",
    repo.storageIdFor("doc:remote-field-revs", NOTE_A),
    { includeDeleted: true },
  );
  remote.rev = 2;
  remote.updatedAt += 1;
  remote.updatedBy = "remote-device";
  remote.value.fields.text = "远端 v2";
  remote.value._meta.fieldRevs["fields.text"] = 1;
  remote.value._meta.fieldRevs["fields.color"] = 1;

  const applied = await repo.applyChanges([{
    collection: "document-notes",
    mutationId: "remote-field-revs-apply",
    record: remote,
  }]);
  assert.equal(applied.applied.length, 1);
  const normalized = await store.get(
    "document-notes",
    repo.storageIdFor("doc:remote-field-revs", NOTE_A),
    { includeDeleted: true },
  );
  assert.equal(normalized.value._meta.fieldRevs["fields.text"], 2);
  assert.equal(normalized.value._meta.fieldRevs["fields.color"], 1);

  await assert.rejects(
    repo.patch(
      "doc:remote-field-revs",
      NOTE_A,
      { text: "旧页面覆盖远端内容" },
      { ifRev: created.rev, mutationId: "remote-field-revs-stale-text" },
    ),
    (error) =>
      error.code === "BW_NOTE_CONFLICT" &&
      error.details.reason === "same-field" &&
      error.details.fields.includes("fields.text"),
  );
  const rebasedDifferentField = await repo.patch(
    "doc:remote-field-revs",
    NOTE_A,
    { color: "blue" },
    { ifRev: created.rev, mutationId: "remote-field-revs-safe-color" },
  );
  assert.equal(rebasedDifferentField.text, "远端 v2");
  assert.equal(rebasedDifferentField.color, "blue");
});

test("patch/remove 必须携带 ifRev，DataStore revision 冲突统一映射为 BW_NOTE_CONFLICT", async () => {
  const { repo } = repository("revision");
  const created = await repo.create({
    documentId: "doc:revision",
    noteId: NOTE_A,
    anchor: anchor("doc:revision"),
  }, { mutationId: "revision-create" });

  await assert.rejects(
    repo.patch("doc:revision", NOTE_A, { text: "missing rev" }),
    (error) => error.code === "BW_NOTE_REVISION",
  );
  await assert.rejects(
    repo.remove("doc:revision", NOTE_A, {}),
    (error) => error.code === "BW_NOTE_REVISION",
  );
  await assert.rejects(
    repo.remove(
      "doc:revision",
      NOTE_A,
      { ifRev: created.rev + 10, mutationId: "revision-future-remove" },
    ),
    (error) =>
      error.code === "BW_NOTE_CONFLICT" &&
      error.details.expectedRev === created.rev + 10 &&
      error.details.actualRev === created.rev,
  );
});
