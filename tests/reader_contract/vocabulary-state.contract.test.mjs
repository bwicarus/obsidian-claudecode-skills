import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";
import { webcrypto } from "node:crypto";
import { TextEncoder } from "node:util";

const SOURCE = readFileSync(
  new URL(
    "../../_server_deploy/static/reader-runtime/vocabulary-state.js",
    import.meta.url,
  ),
  "utf8",
);

function moduleInstance(extra = {}) {
  const sandbox = {
    console,
    Promise,
    Map,
    Set,
    Uint8Array,
    TextEncoder,
    Buffer,
    Date,
    Math,
    JSON,
    crypto: webcrypto,
    ...extra,
  };
  sandbox.globalThis = sandbox;
  sandbox.window = sandbox;
  vm.runInContext(SOURCE, vm.createContext(sandbox), {
    filename: "vocabulary-state.js",
  });
  return sandbox.BWReaderRuntime.vocabularyState;
}

function memoryAdapter(initial = [], identity = null) {
  const values = new Map(initial.map((value) => [value.id, structuredClone(value)]));
  const listeners = [];
  const adapter = {
    values,
    list({ offset = 0, limit = 200 } = {}) {
      return Promise.resolve(
        Array.from(values.values())
          .sort((left, right) => left.id.localeCompare(right.id))
          .slice(offset, offset + limit)
          .map((value) => structuredClone(value)),
      );
    },
    put(value) {
      const copy = structuredClone(value);
      values.set(copy.id, copy);
      for (const listener of listeners) listener({ record: copy });
      return Promise.resolve({ value: copy });
    },
    subscribe(listener) {
      listeners.push(listener);
      return () => {
        const index = listeners.indexOf(listener);
        if (index >= 0) listeners.splice(index, 1);
      };
    },
  };
  if (identity != null) adapter.identity = () => Promise.resolve(identity);
  return adapter;
}

test("稳定编号由属性、类型、语言和规范词项共同决定", () => {
  const state = moduleInstance();
  const was = state.idFor({
    kind: "word",
    language: "en",
    lemma: "be",
    word: "was",
  }, "mastered");
  const be = state.idFor({
    kind: "word",
    language: "en",
    key: " BE ",
  }, "mastered");
  assert.equal(was, be);
  assert.notEqual(
    was,
    state.idFor({ kind: "phrase", language: "en", key: "be" }, "mastered"),
  );
  assert.throws(
    () => state.idFor({ kind: "word", language: "en", key: "be" }, "favorite"),
    /只有词组可以收藏/,
  );
});

test("持久化永不返回时，本地掌握投影仍同步生效", async () => {
  const state = moduleInstance();
  let putStarted = 0;
  await state.attach({
    list: async () => [],
    put: () => {
      putStarted += 1;
      return new Promise(() => {});
    },
  });
  const transaction = state.setMastered({
    kind: "word",
    language: "en",
    lemma: "be",
    word: "was",
    forms: ["been", "being"],
  }, true);
  assert.equal(state.isMastered({
    kind: "word",
    language: "en",
    lemma: "be",
    word: "was",
  }), true);
  assert.equal(putStarted, 1);
  assert.equal(transaction.applied, true);
});

test("迟到快照不能覆盖尚未落盘的本地取消掌握", async () => {
  const state = moduleInstance();
  state.importRecord({
    property: "mastered",
    kind: "word",
    language: "en",
    key: "be",
    enabled: true,
    changedAt: 10,
  });
  state.setMastered({
    kind: "word",
    language: "en",
    key: "be",
  }, false, { changedAt: 20 });

  let release;
  const old = state.normalizeRecord({
    property: "mastered",
    kind: "word",
    language: "en",
    key: "be",
    enabled: true,
    changedAt: 15,
  });
  const attaching = state.attach({
    list: () => new Promise((resolve) => { release = () => resolve([old]); }),
    put: async (value) => value,
  });
  await Promise.resolve();
  await Promise.resolve();
  release();
  await attaching;
  assert.equal(state.isMastered({
    kind: "word",
    language: "en",
    key: "be",
  }), false);
});

test("显式语言记录优先于旧版 und 镜像，词组掌握与收藏互不覆盖", () => {
  const state = moduleInstance();
  state.importRecord({
    property: "mastered",
    kind: "word",
    language: "und",
    key: "be",
    enabled: true,
    changedAt: 1,
  });
  state.setMastered({
    kind: "word",
    language: "en",
    key: "be",
  }, false);
  assert.equal(state.isMastered({
    kind: "word",
    language: "en",
    key: "be",
  }), false);
  assert.equal(state.isMastered({
    kind: "word",
    language: "ja",
    key: "be",
  }), true);

  const phrase = { kind: "phrase", language: "en", text: "in spite of" };
  state.setMastered(phrase, true);
  state.setPhraseFavorite(phrase, false);
  assert.equal(state.isMastered(phrase), true);
  assert.equal(state.isPhraseFavorite(phrase), false);
  assert.equal(state.snapshot().filter((item) => item.kind === "phrase").length, 2);
});

test("同一适配器重建模块后仍可读取已保存状态", async () => {
  const adapter = memoryAdapter();
  const first = moduleInstance();
  await first.attach(adapter);
  const write = first.setPhraseFavorite({
    language: "ja",
    text: "お世話になります",
  }, true);
  await write.durable;

  const second = moduleInstance();
  await second.attach(adapter);
  assert.equal(second.isPhraseFavorite({
    language: "ja",
    text: "お世話になります",
  }), true);
});

test("账户作用域切换会清空旧记录和旧 pending，同账户重连才允许恢复", async () => {
  const state = moduleInstance();
  const mastered = state.normalizeRecord({
    property: "mastered",
    kind: "word",
    language: "en",
    key: "be",
    enabled: true,
  });
  let releaseOldWrite;
  const accountA = {
    identity: async () => "scope-account-a",
    list: async () => [mastered],
    put: () => new Promise((resolve) => { releaseOldWrite = resolve; }),
  };
  const accountBWrites = [];
  const accountB = {
    identity: async () => "scope-account-b",
    list: async () => [],
    put: async (value) => {
      accountBWrites.push(structuredClone(value));
      return value;
    },
  };
  await state.attach(accountA);
  assert.equal(state.isMastered({
    kind: "word", language: "en", key: "be",
  }), true);
  const oldTransaction = state.setMastered({
    kind: "word", language: "en", key: "be",
  }, false);

  await state.attach(accountB);
  assert.equal(state.isMastered({
    kind: "word", language: "en", key: "be",
  }), false);
  assert.deepEqual(accountBWrites, []);
  releaseOldWrite(mastered);
  await assert.rejects(
    oldTransaction.durable,
    (error) => error?.code === "BW_VOCABULARY_STATE_STALE",
  );

  await state.attach(memoryAdapter([mastered], "scope-account-a"));
  assert.equal(state.isMastered({
    kind: "word", language: "en", key: "be",
  }), true);
});

test("同账户两个模块通过 adapter subscribe 立即共享更新", async () => {
  const adapter = memoryAdapter([], "scope-shared-account");
  const first = moduleInstance();
  const second = moduleInstance();
  await first.attach(adapter);
  await second.attach(adapter);
  const transaction = first.setMastered({
    kind: "word",
    language: "en",
    lemma: "be",
    word: "was",
  }, true);
  await transaction.durable;
  assert.equal(second.isMastered({
    kind: "word",
    language: "en",
    lemma: "be",
    word: "was",
  }), true);
});

test("传输失效窗口只排队；重连到新账户会丢弃旧 pending，同账户则补写", async () => {
  const makeReconnectable = () => {
    let scope = "scope-a";
    let invalidation = null;
    let reconnect = null;
    const writes = [];
    return {
      writes,
      setScope(value) { scope = value; },
      invalidate() { invalidation?.(); },
      reconnect() { reconnect?.(); },
      identity: async () => scope,
      list: async () => [],
      put: async (value) => {
        writes.push({ scope, value: structuredClone(value) });
        return value;
      },
      onInvalidate(listener) {
        invalidation = listener;
        return () => { if (invalidation === listener) invalidation = null; };
      },
      onReconnect(listener) {
        reconnect = listener;
        return () => { if (reconnect === listener) reconnect = null; };
      },
    };
  };

  const sameAccount = makeReconnectable();
  const first = moduleInstance();
  await first.attach(sameAccount);
  sameAccount.invalidate();
  first.setMastered({
    kind: "word", language: "en", key: "be",
  }, true);
  assert.equal(sameAccount.writes.length, 0);
  sameAccount.reconnect();
  await first.ready();
  assert.equal(sameAccount.writes.length, 1);
  assert.equal(sameAccount.writes[0].scope, "scope-a");

  const switchedAccount = makeReconnectable();
  const second = moduleInstance();
  await second.attach(switchedAccount);
  switchedAccount.invalidate();
  second.setMastered({
    kind: "word", language: "en", key: "be",
  }, true);
  switchedAccount.setScope("scope-b");
  switchedAccount.reconnect();
  await second.ready();
  assert.equal(switchedAccount.writes.length, 0);
  assert.equal(second.isMastered({
    kind: "word", language: "en", key: "be",
  }), false);
});

test("记录拒绝无界别名，避免扩展消息与 IndexedDB 被超大载荷拖垮", () => {
  const state = moduleInstance();
  assert.throws(
    () => state.normalizeRecord({
      property: "mastered",
      kind: "word",
      language: "en",
      key: "be",
      aliases: Array.from({ length: 33 }, (_, index) => `form-${index}`),
      enabled: true,
    }),
    /别名数量或总长度超出上限/,
  );
});
