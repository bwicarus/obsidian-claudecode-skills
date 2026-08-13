import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { DataStore } from "./helpers.mjs";

const require = createRequire(import.meta.url);
const Cards = require(
  "../../_server_deploy/static/reader-runtime/card-repository.js",
);
const CARD_A = `card_${"a".repeat(32)}`;
const CARD_B = `card_${"b".repeat(32)}`;

function makeStore(name = "cards") {
  return DataStore.createDataStore({
    backend: DataStore.createMemoryBackend(),
    deviceId: name,
    causalCollections: ["card-entities", "card-states"],
  });
}

function source(overrides = {}) {
  return {
    kind: "reader-selection",
    sourceId: "book:physics:p12:selection-4",
    documentId: "book:physics",
    title: "Physics",
    quote: "A local-first source excerpt",
    location: { unit: "page", index: 12 },
    ...structuredClone(overrides),
  };
}

function basic(front = "What is local-first?") {
  return {
    type: "basic",
    front,
    back: "The device is authoritative and sync is replication.",
    deck: "Reader::Architecture",
    tags: ["Reader", "local-first"],
  };
}

function cloze() {
  return {
    type: "cloze",
    text: "The {{c1::device}} is authoritative.",
  };
}

function repository(store = makeStore(), overrides = {}) {
  return {
    store,
    repo: Cards.createCardRepository({
      store,
      idFactory: () => CARD_A,
      mutationFactory: () => "test-mutation",
      clock: () => 1_800_000_000_000,
      ...overrides,
    }),
  };
}

test("缺 runtime.storage/batch 时 fail closed，不会伪装成本地入库成功", () => {
  assert.throws(
    () => Cards.createCardRepository({ store: null }),
    (error) => error.code === "BW_CARD_REPOSITORY_UNAVAILABLE",
  );
  assert.throws(
    () => Cards.createCardRepository({
      store: { get() {}, list() {}, put() {}, remove() {}, subscribe() {} },
    }),
    (error) => error.code === "BW_CARD_REPOSITORY_UNAVAILABLE",
  );
});

test("一个 card_* gid 原子保存整批 cards，states 严格按批内 index", async () => {
  const { repo, store } = repository();
  const draft = await repo.registerDraft({
    id: CARD_A,
    cid: CARD_A,
    gid: CARD_A,
    cards: [basic("A0"), cloze()],
    source: source(),
  }, { mutationId: "draft-a" });

  assert.equal(draft.id, CARD_A);
  assert.equal(draft.cid, CARD_A);
  assert.equal(draft.gid, CARD_A);
  assert.equal(draft.cards.length, 2);
  assert.deepEqual(Object.keys(draft.states), ["0", "1"]);
  assert.equal(draft.states["0"].phase, "draft");
  assert.equal(draft.states["1"].phase, "draft");
  assert.deepEqual(draft.states["0"].exactState, {});

  const entity = await store.get("card-entities", CARD_A);
  const state = await store.get("card-states", CARD_A);
  assert.equal(entity.value.id, CARD_A);
  assert.equal(entity.value.cid, CARD_A);
  assert.equal(entity.value.gid, CARD_A);
  assert.equal(entity.value.cards.length, 2);
  assert.equal(state.value.id, CARD_A);
  assert.deepEqual(Object.keys(state.value.states), ["0", "1"]);
  assert.equal((await store.list("card-entities")).length, 1);
  assert.equal((await store.list("card-states")).length, 1);
});

test("saveConfirmedCard 只确认指定 index，不拆分或重编号批内卡片", async () => {
  const store = makeStore("confirm-index");
  const first = repository(store).repo;
  await first.registerDraft({
    id: CARD_A,
    cards: [basic("draft 0"), basic("draft 1")],
    source: source(),
  }, { mutationId: "confirm-draft" });

  const confirmed = await first.saveConfirmedCard({
    id: CARD_A,
    cardIndex: 1,
    card: basic("edited index 1"),
  }, { mutationId: "confirm-index-1", ifEntityRev: 1, ifStateRev: 1 });
  assert.equal(confirmed.id, CARD_A);
  assert.equal(confirmed.cards.length, 2);
  assert.equal(confirmed.cards[0].front, "draft 0");
  assert.equal(confirmed.cards[1].front, "edited index 1");
  assert.equal(confirmed.states["0"].phase, "draft");
  assert.equal(confirmed.states["1"].phase, "confirmed");
  assert.equal(confirmed.states["1"].review.status, "new");

  const rebuilt = repository(store, { clock: () => 1_800_000_001_000 }).repo;
  assert.deepEqual(await rebuilt.load(CARD_A), confirmed);
  await assert.rejects(
    rebuilt.saveConfirmedCard({ id: CARD_A }),
    (error) => error.code === "BW_CARD_REPOSITORY_CARD_INDEX",
  );
});

test("删除已持久化草稿只写稳定 index tombstone，剩余卡保存与重载不复活", async () => {
  const store = makeStore("remove-draft-index");
  const repo = repository(store).repo;
  await repo.registerDraft({
    id: CARD_A,
    cards: [basic("draft 0"), basic("draft 1"), basic("draft 2")],
    source: source(),
  }, { mutationId: "remove-draft-seed" });

  const removed = await repo.removeDraftCard(CARD_A, 1, {
    mutationId: "remove-draft-1",
  });
  assert.equal(removed.cards.length, 3, "实体 cards 不得 splice");
  assert.deepEqual(Object.keys(removed.states), ["0", "1", "2"]);
  assert.equal(removed.states[0].removed, false);
  assert.equal(removed.states[1].removed, true);
  assert.equal(removed.states[2].removed, false);

  const confirmed = await repo.saveConfirmedCard({
    id: CARD_A,
    cardIndex: 2,
    card: basic("saved stable index 2"),
    cards: removed.cards,
  }, { mutationId: "confirm-stable-index-2" });
  assert.equal(confirmed.cards.length, 3);
  assert.equal(confirmed.cards[1].front, "draft 1");
  assert.equal(confirmed.cards[2].front, "saved stable index 2");
  assert.equal(confirmed.states[1].removed, true);
  assert.equal(confirmed.states[2].phase, "confirmed");

  const rebuilt = repository(store).repo;
  const reloaded = await rebuilt.load(CARD_A);
  assert.equal(reloaded.states[1].removed, true);
  await assert.rejects(
    rebuilt.saveConfirmedCard({
      id: CARD_A,
      cardIndex: 1,
      card: basic("must not revive"),
      cards: reloaded.cards,
    }),
    (error) => error.code === "BW_CARD_REPOSITORY_CARD_REMOVED",
  );
  await assert.rejects(
    rebuilt.registerDraft({
      id: CARD_A,
      cards: [basic("renumber 0"), basic("renumber 1")],
      source: source(),
    }),
    (error) => error.code === "BW_CARD_REPOSITORY_CONTENT_CONFLICT",
  );
});

test("草稿 gid 只允许 cards/source 精确幂等重放，已有记录必须带相同 draftId", async () => {
  const { repo } = repository();
  const originalSource = source({ draftId: "draft-stable-a" });
  const originalCards = [basic("stable 0"), basic("stable 1")];
  const created = await repo.registerDraft({
    id: CARD_A,
    cards: originalCards,
    source: originalSource,
  }, {
    mutationId: "strict-draft-create",
    requireDraftIdForReplay: true,
  });

  const replayed = await repo.registerDraft({
    id: CARD_A,
    cards: originalCards,
    source: originalSource,
  }, {
    mutationId: "strict-draft-replay",
    requireDraftIdForReplay: true,
  });
  assert.deepEqual(replayed, created);

  await assert.rejects(
    repo.registerDraft({
      id: CARD_A,
      cards: originalCards,
      source: source(),
    }, {
      mutationId: "strict-draft-no-id",
      requireDraftIdForReplay: true,
    }),
    (error) => error.code === "BW_CARD_REPOSITORY_SOURCE_CONFLICT" &&
      error.details?.field === "source.draftId",
  );
  await assert.rejects(
    repo.registerDraft({
      id: CARD_A,
      cards: [basic("forked 0"), basic("stable 1")],
      source: originalSource,
    }, {
      mutationId: "strict-draft-card-fork",
      requireDraftIdForReplay: true,
    }),
    (error) => error.code === "BW_CARD_REPOSITORY_CONTENT_CONFLICT" &&
      error.details?.field === "cards",
  );
  await assert.rejects(
    repo.registerDraft({
      id: CARD_A,
      cards: originalCards,
      source: source({ draftId: "draft-stable-a", quote: "forked source" }),
    }, {
      mutationId: "strict-draft-source-fork",
      requireDraftIdForReplay: true,
    }),
    (error) => error.code === "BW_CARD_REPOSITORY_SOURCE_CONFLICT" &&
      error.details?.field === "source",
  );
});

test("草稿正文按稳定 batch index 写入 exactState，重载不改原始呈现基线", async () => {
  const { repo } = repository();
  const originalCards = [basic("draft 0"), basic("draft 1")];
  const originalSource = source({ draftId: "draft-edit-a" });
  const created = await repo.registerDraft({
    id: CARD_A,
    cards: originalCards,
    source: originalSource,
  }, { mutationId: "draft-edit-create" });
  const patched = await repo.patchState(CARD_A, 1, {
    exactState: {
      _st: "draft",
      front: "edited stable index 1",
      back: "edited back",
    },
  }, { mutationId: "draft-edit-index-1", ifStateRev: created.stateRev });

  assert.equal(patched.cards[0].front, "draft 0");
  assert.equal(patched.cards[1].front, "draft 1");
  assert.equal(patched.states[0].exactState.front, undefined);
  assert.equal(patched.states[1].exactState.front, "edited stable index 1");
  const reloaded = await repo.load(CARD_A);
  assert.equal(reloaded.states[1].exactState.back, "edited back");
});

test("basic/cloze/source 采用精确形状并拒绝 NUL、过长与身份分叉", async () => {
  const { repo } = repository();
  const legacyBasic = Cards.normalizeCard({
    ...basic("legacy basic"),
    cloze: null,
  });
  assert.equal(legacyBasic.front, "legacy basic");
  assert.equal("cloze" in legacyBasic, false);
  const legacyCloze = Cards.normalizeCard({
    type: "cloze",
    cloze: "The {{c1::legacy}} shape.",
    front: null,
    back: null,
  });
  assert.equal(legacyCloze.cloze, "The {{c1::legacy}} shape.");
  const legacyTextAlias = Cards.normalizeCard({
    type: "cloze",
    cloze: null,
    text: "The {{c1::text alias}} shape.",
    front: null,
    back: null,
  });
  assert.equal(legacyTextAlias.cloze, "The {{c1::text alias}} shape.");
  await assert.rejects(
    repo.registerDraft({
      id: CARD_A, cid: CARD_B,
      cards: [basic()], source: source(),
    }),
    (error) => error.code === "BW_CARD_REPOSITORY_ID",
  );
  await assert.rejects(
    repo.registerDraft({
      id: CARD_A,
      cards: [{ ...basic(), cloze: "{{c1::wrong}}" }],
      source: source(),
    }),
    (error) => error.code === "BW_CARD_REPOSITORY_CARD_SHAPE",
  );
  await assert.rejects(
    repo.registerDraft({
      id: CARD_A,
      cards: [{
        type: "cloze",
        cloze: "{{c1::first}}",
        text: "{{c1::second}}",
        front: null,
        back: null,
      }],
      source: source(),
    }),
    (error) => error.code === "BW_CARD_REPOSITORY_CARD_SHAPE",
  );
  await assert.rejects(
    repo.registerDraft({
      id: CARD_A,
      cards: [{ type: "cloze", cloze: "no deletion" }],
      source: source(),
    }),
    (error) => error.code === "BW_CARD_REPOSITORY_CARD_SHAPE",
  );
  await assert.rejects(
    repo.registerDraft({
      id: CARD_A,
      cards: [basic("bad\0front")],
      source: source(),
    }),
    (error) => error.code === "BW_CARD_REPOSITORY_INPUT",
  );
  await assert.rejects(
    repo.registerDraft({
      id: CARD_A,
      cards: [basic()],
      source: source({ unexpected: "field" }),
    }),
    (error) => error.code === "BW_CARD_REPOSITORY_INPUT",
  );
});

test("跨 collection batch 失败时不会留下半个卡组", async () => {
  const real = makeStore("atomic-fail");
  const failing = {
    get: real.get,
    list: real.list,
    put: real.put,
    remove: real.remove,
    subscribe: real.subscribe,
    batch: async () => {
      throw Object.assign(new Error("injected batch failure"), {
        code: "TEST_BATCH_FAILURE",
      });
    },
  };
  const repo = repository(failing).repo;
  await assert.rejects(
    repo.registerDraft({
      id: CARD_A,
      cards: [basic()],
      source: source(),
    }, { mutationId: "atomic-failure" }),
    (error) => error.code === "TEST_BATCH_FAILURE",
  );
  assert.equal(await real.get("card-entities", CARD_A), null);
  assert.equal(await real.get("card-states", CARD_A), null);
});

test("state patch round-trip rc exactState；Anki id 仅是 index projection receipt", async () => {
  const { repo, store } = repository();
  const saved = await repo.saveConfirmedCard({
    id: CARD_A,
    cardIndex: 0,
    cards: [basic("A0"), basic("A1")],
    source: source(),
  }, { mutationId: "state-confirm" });
  const exact = {
    _st: "learn",
    _nid: 987654321,
    _next: "10m",
    _showBack: true,
    id: 123456789,
    card_id: 123456789,
    _ratingUnavailable: false,
    _ratingUnavailableReason: null,
    _ratingPending: false,
    _syncPending: false,
    _addPending: false,
    _addQueued: false,
    _addAid: "fc_abc",
    _ratingAid: "rate_abc",
    _ratingEase: 3,
    _ratingCardId: 123456789,
  };
  const patched = await repo.patchState(CARD_A, 0, {
    exactState: exact,
    review: { status: "learning", reps: 1, intervalDays: 0.25 },
    flags: { favorite: true },
  }, { mutationId: "exact-state", ifStateRev: saved.stateRev });
  assert.deepEqual(patched.states["0"].exactState, exact);
  assert.deepEqual(patched.states["1"].exactState, {});

  const projected = await repo.recordAnkiReceipt(CARD_A, 0, "ankimobile-ipad", {
    status: "succeeded",
    mutationId: "export-once",
    noteIds: [987654321],
    cardIds: [123456789],
    exportedAt: 1_800_000_020_000,
  }, { mutationId: "anki-receipt", ifStateRev: patched.stateRev });
  assert.equal(projected.id, CARD_A);
  assert.deepEqual(
    projected.states["0"].projections.anki["ankimobile-ipad"].cardIds,
    [123456789],
  );
  assert.equal(projected.states["1"].projections.anki["ankimobile-ipad"], undefined);
  assert.equal(
    JSON.stringify((await store.get("card-entities", CARD_A)).value).includes("987654321"),
    false,
  );
  await assert.rejects(
    repo.patchState(CARD_A, 0, { flags: { archived: true } }, {
      mutationId: "stale-state",
      ifStateRev: 1,
    }),
    (error) => error.code === "BW_CARD_REPOSITORY_CONFLICT",
  );
});

test("legacy batch 原子导入 gid+cards+states，缺失状态可补、真实分叉显式冲突", async () => {
  const { repo } = repository();
  const legacy = {
    id: "card_1a2b3c",
    kind: "cards",
    cards: [
      { ...basic("legacy 0"), cloze: null },
      {
        type: "cloze",
        cloze: "{{c1::legacy 1}}",
        front: null,
        back: null,
      },
    ],
    states: {
      1: {
        _st: "learn",
        _nid: 444,
        _next: "20m",
        id: 555,
        card_id: 555,
        _ratingUnavailable: false,
      },
    },
    source_ref: "book:legacy.pdf#p9",
    req: "根据选区制作两张卡",
    ts: 1_700_000_000,
  };
  const [imported] = await repo.importLegacyBatch([legacy], {
    mutationId: "legacy-first",
  });
  assert.equal(imported.id, "card_1a2b3c");
  assert.equal(imported.cards.length, 2);
  assert.equal(imported.states["0"].phase, "draft");
  assert.equal(imported.states["1"].phase, "confirmed");
  assert.deepEqual(imported.states["1"].exactState, legacy.states[1]);
  assert.deepEqual(
    imported.states["1"].projections.anki["pi-legacy"].noteIds,
    [444],
  );
  assert.equal(imported.source.legacy.source_ref, legacy.source_ref);
  assert.equal(imported.source.requirement, legacy.req);

  const [replayed] = await repo.importLegacyBatch([legacy], {
    mutationId: "legacy-replay",
  });
  assert.deepEqual(replayed, imported);

  await assert.rejects(
    repo.importLegacyBatch([{
      ...legacy,
      cards: [basic("different"), { type: "cloze", cloze: "{{c1::legacy 1}}" }],
    }], {
      mutationId: "legacy-content-conflict",
    }),
    (error) => error.code === "BW_CARD_REPOSITORY_LEGACY_CONFLICT" &&
      error.details.field === "cards",
  );
  await assert.rejects(
    repo.importLegacyBatch([{
      ...legacy,
      states: { 1: { ...legacy.states[1], _next: "99d" } },
    }], { mutationId: "legacy-state-conflict" }),
    (error) => error.code === "BW_CARD_REPOSITORY_LEGACY_CONFLICT" &&
      error.details.cardIndex === 1,
  );
});

test("legacy import 与同批其它记录任一冲突时全部不写", async () => {
  const { repo } = repository();
  await repo.saveConfirmedCard({
    id: CARD_A,
    cardIndex: 0,
    cards: [basic("local authoritative")],
    source: source(),
  }, { mutationId: "batch-conflict-seed" });

  await assert.rejects(
    repo.importLegacyBatch([
      {
        id: CARD_B,
        kind: "cards",
        cards: [basic("would be new")],
        states: {},
        source_ref: "book:new#p1",
      },
      {
        id: CARD_A,
        kind: "cards",
        cards: [basic("conflicting legacy")],
        states: {},
        source_ref: "book:old#p2",
      },
    ], { mutationId: "batch-conflict" }),
    (error) => error.code === "BW_CARD_REPOSITORY_LEGACY_CONFLICT",
  );
  assert.equal(await repo.load(CARD_B), null);
});

test("legacy missingOnly 原子跳过本地权威 gid 并只导入缺失 gid", async () => {
  const { repo } = repository();
  await repo.saveConfirmedCard({
    id: CARD_A,
    cardIndex: 0,
    cards: [basic("locally edited")],
    source: source(),
    exactState: { _st: "review", _next: "180d" },
  }, { mutationId: "missing-only-local" });
  const before = await repo.load(CARD_A);

  const imported = await repo.importLegacyBatch([
    {
      id: CARD_A,
      kind: "cards",
      cards: [basic("stale Pi content")],
      states: { 0: { _st: "learn", _next: "20m" } },
      source_ref: "book:old#p1",
    },
    {
      id: CARD_B,
      kind: "cards",
      cards: [basic("missing local gid")],
      states: { 0: { _st: "learn", _next: "20m" } },
      source_ref: "book:old#p2",
    },
  ], { mutationId: "missing-only-bootstrap", missingOnly: true });

  assert.deepEqual(imported[0], before);
  assert.deepEqual(await repo.load(CARD_A), before);
  assert.equal(imported[1].id, CARD_B);
  assert.equal((await repo.load(CARD_B)).cards[0].front, "missing local gid");
});

test("snapshot/subscribe 返回组合卡组，双 collection 墓碑保持一致", async () => {
  const { repo } = repository();
  const events = [];
  const unsubscribe = repo.subscribe((event) => events.push(event));
  await repo.saveConfirmedCard({
    id: CARD_A,
    cardIndex: 0,
    cards: [basic("A")],
    source: source(),
  }, { mutationId: "sub-a" });
  await repo.saveConfirmedCard({
    id: CARD_B,
    cardIndex: 0,
    cards: [basic("B")],
    source: source({ sourceId: "book:physics:p13" }),
  }, { mutationId: "sub-b" });
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.deepEqual((await repo.snapshot()).map((item) => item.id), [CARD_A, CARD_B]);
  assert.ok(events.some((event) => event.cardId === CARD_A));

  const before = await repo.load(CARD_A);
  const deleted = await repo.tombstone(CARD_A, {
    mutationId: "sub-remove",
    ifEntityRev: before.entityRev,
    ifStateRev: before.stateRev,
  });
  assert.equal(deleted.deleted, true);
  assert.equal(await repo.load(CARD_A), null);
  assert.equal((await repo.load(CARD_A, { includeDeleted: true })).deleted, true);
  assert.deepEqual((await repo.snapshot()).map((item) => item.id), [CARD_B]);
  assert.deepEqual(
    (await repo.snapshot({ includeDeleted: true })).map((item) => [item.id, item.deleted]),
    [[CARD_A, true], [CARD_B, false]],
  );
  unsubscribe();
});
