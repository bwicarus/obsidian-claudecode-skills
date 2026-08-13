// StorageRouter 必须把 batch 的第二个参数交给底层 store。
//
// 精确高亮靠 batchOptions.transactionTimeoutMs 让 IndexedDB 事务在 4 秒后真正
// abort；事务不 abort，writer lease 就永不释放，之后每次高亮都超时。这个参数从
// native-local-runtime 发出、由 IndexedDBStore.batch(mutations, batchOptions)
// 接收，中间只经过 router 一层 —— 而 router 曾经只声明 batch(mutations)，把它
// 静静丢掉，于是修复在包里、路径也对，却全程空转。
//
// 已有的高亮合同测试断言过这个字段并且通过了，因为它把 createStorageRouter 换成
// 了替身、又把假 store 直接当 stores 注入 —— 替换掉的正好是出问题的那一层。所以
// 这里坚持用真实 router，只把最底层换成记录器。
import test from "node:test";
import assert from "node:assert/strict";

import { StorageRouter, makeRegistry } from "./helpers.mjs";

function recordingStore() {
  const seen = [];
  return {
    seen,
    contract: "data-store/1",
    get: () => Promise.resolve(null),
    getMany: (requests) => Promise.resolve(requests.map(() => null)),
    list: () => Promise.resolve([]),
    put: () => Promise.resolve({ ok: true }),
    remove: () => Promise.resolve({ ok: true }),
    batch: (mutations, batchOptions) => {
      seen.push(batchOptions === undefined ? "undefined" : batchOptions);
      return Promise.resolve(mutations.map(() => ({ ok: true })));
    },
    // router 要求的其余接口，这里只需存在即可 —— 被测的是 batch 那一条路。
    changes: () => Promise.resolve([]),
    applyChanges: () => Promise.resolve({ ok: true }),
    status: () => ({ ok: true }),
    subscribe: () => () => {},
  };
}

const SCOPES = {
  "document-highlights": {
    scope: "document",
    status: "ready",
  },
};

function makeRouter() {
  const documentStore = recordingStore();
  const globalStore = recordingStore();
  const router = StorageRouter.createStorageRouter({
    globalStore,
    documentStore,
    deviceStore: documentStore,
    scopes: SCOPES,
    dataRegistryApi: makeRegistry(SCOPES),
  });
  return { router, documentStore };
}

const MUTATION = {
  collection: "document-highlights",
  value: { id: "highlight-1", documentId: "book-1" },
};

test("批次选项必须透传到底层 store", async () => {
  const { router, documentStore } = makeRouter();
  await router.batch([MUTATION], { transactionTimeoutMs: 4000 });
  assert.deepEqual(
    documentStore.seen,
    [{ transactionTimeoutMs: 4000 }],
    "精确高亮的有界事务超时必须抵达底层 store，否则事务永不 abort",
  );
});

test("未给选项时底层看到的仍是「没有选项」而不是空对象", async () => {
  const { router, documentStore } = makeRouter();
  await router.batch([MUTATION]);
  assert.deepEqual(
    documentStore.seen,
    ["undefined"],
    "普通写入不应被塞进一个凭空构造的选项对象",
  );
});
