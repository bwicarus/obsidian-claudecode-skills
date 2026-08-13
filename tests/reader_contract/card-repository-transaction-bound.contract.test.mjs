// 本地卡片写入必须带事务上界。
//
// 用户确认一张卡，要求先在本机卡库落定并立刻可见。这条路径走 StorageRouter 到
// IndexedDB，而 IndexedDB 事务若不 settle，调用方的超时只是自己放弃：底层事务仍
//占着 object store，之后每一次读写都排在它后面，于是"制卡超时"会自我延续。
// 精确高亮已经因为同一个原因卡死过，卡仓是同一条链上的第二处。
//
// 这里刻意用真实的 DataStore 再包一层记录器，只观察传给 batch 的第二个参数。
// 已有的高亮合同测试断言过这个字段却没能发现 router 把它丢了，正是因为它把中间
// 那层换成了替身 —— 被替换掉的恰好是出问题的地方。
import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

import { DataStore } from "./helpers.mjs";

const require = createRequire(import.meta.url);
const Cards = require(
  "../../_server_deploy/static/reader-runtime/card-repository.js",
);

const CARD_A = `card_${"a".repeat(32)}`;
const EXPECTED_TIMEOUT_MS = 4000;

function recordingStore() {
  const inner = DataStore.createDataStore({
    backend: DataStore.createMemoryBackend(),
    deviceId: "cards",
    causalCollections: ["card-entities", "card-states"],
  });
  const seen = [];
  const store = new Proxy(inner, {
    get(target, key) {
      if (key === "batch") {
        return (mutations, batchOptions) => {
          seen.push(batchOptions === undefined ? "undefined" : batchOptions);
          return target.batch(mutations, batchOptions);
        };
      }
      const value = target[key];
      return typeof value === "function" ? value.bind(target) : value;
    },
  });
  return { store, seen };
}

function makeRepository() {
  const { store, seen } = recordingStore();
  const repo = Cards.createCardRepository({
    store,
    idFactory: () => CARD_A,
    mutationFactory: () => "test-mutation",
    clock: () => 1_800_000_000_000,
  });
  return { repo, seen };
}

const SOURCE = {
  kind: "reader-selection",
  sourceId: "book:physics:p12:selection-4",
  documentId: "book:physics",
  title: "Physics",
  quote: "A local-first source excerpt",
  location: { unit: "page", index: 12 },
};

const CARD = {
  type: "basic",
  front: "What is local-first?",
  back: "The device is authoritative and sync is replication.",
  deck: "Reader::Architecture",
  tags: ["Reader", "local-first"],
};

test("注册草稿的写入带事务上界", async () => {
  const { repo, seen } = makeRepository();
  await repo.registerDraft({ id: CARD_A, cards: [CARD], source: SOURCE });
  assert.ok(seen.length > 0, "草稿注册应当真的写了一次");
  for (const options of seen) {
    assert.deepEqual(
      options,
      { transactionTimeoutMs: EXPECTED_TIMEOUT_MS },
      "草稿写入的 IndexedDB 事务必须有界，否则它会挂住后续每一次卡片写入",
    );
  }
});

test("用户确认保存的写入带事务上界", async () => {
  const { repo, seen } = makeRepository();
  await repo.registerDraft({ id: CARD_A, cards: [CARD], source: SOURCE });
  seen.length = 0;
  await repo.saveConfirmedCard({ id: CARD_A, index: 0 });
  assert.ok(seen.length > 0, "确认保存应当真的写了一次");
  for (const options of seen) {
    assert.deepEqual(
      options,
      { transactionTimeoutMs: EXPECTED_TIMEOUT_MS },
      "确认保存是用户可见的那一步，事务无界时它会永远停在处理中",
    );
  }
});
