import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const ROOT = new URL("../../", import.meta.url);
const DATA_STORE = readFileSync(
  new URL("_server_deploy/static/reader-runtime/data-store.js", ROOT),
  "utf8",
);
const INDEXEDDB_STORE = readFileSync(
  new URL("_server_deploy/static/reader-runtime/indexeddb-store.js", ROOT),
  "utf8",
);

function nameList(names) {
  return { contains: (name) => names.includes(name) };
}

function webKitStrictIndexedDB() {
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
  let keepaliveReads = 0;

  class StrictTransaction {
    constructor(storeNames) {
      this.storeNames = storeNames;
      this.active = true;
      this.pending = 0;
      this.completed = false;
      this.error = null;
      this.oncomplete = null;
      this.onabort = null;
      this.onerror = null;
    }

    objectStore(name) {
      if (!this.active || !this.storeNames.includes(name)) {
        throw new Error(
          "Attempt to get a record from database without an in-progress transaction",
        );
      }
      const transaction = this;
      const data = values[name];
      const keyPath = keyPaths[name];
      const schedule = (operation) => {
        if (!transaction.active) {
          throw new Error(
            "Attempt to get a record from database without an in-progress transaction",
          );
        }
        const request = { result: undefined, error: null, onsuccess: null, onerror: null };
        transaction.pending += 1;
        setImmediate(() => {
          if (!transaction.active) return;
          try {
            request.result = operation();
          } catch (error) {
            request.error = error;
            transaction.error = error;
          }
          transaction.pending -= 1;
          if (request.error) {
            request.onerror?.();
            transaction.abort();
            return;
          }
          request.onsuccess?.();
          // Model WebKit's strict lifetime: Promise continuations run after
          // this callback. Unless another IDB request is already pending, the
          // transaction is inactive before that continuation can enqueue work.
          if (transaction.pending === 0 && transaction.active) {
            transaction.active = false;
            transaction.completed = true;
            setImmediate(() => transaction.oncomplete?.());
          }
        });
        return request;
      };
      return {
        get(key) {
          if (key === "__bw_reader_transaction_keepalive__") keepaliveReads += 1;
          return schedule(() => data.get(key));
        },
        put(value) {
          return schedule(() => {
            const key = value[keyPath];
            data.set(key, value);
            return key;
          });
        },
      };
    }

    abort() {
      if (!this.active) throw new Error("transaction already inactive");
      this.active = false;
      setImmediate(() => this.onabort?.());
    }
  }

  const database = {
    objectStoreNames: nameList(Object.keys(keyPaths)),
    onversionchange: null,
    close() {},
    transaction(storeNames) {
      return new StrictTransaction(Array.from(storeNames));
    },
  };
  const factory = {
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
  };
  return { factory, keepaliveReads: () => keepaliveReads };
}

test("WebKit 严格事务在 Promise 续步之间保持活跃", async () => {
  const strict = webKitStrictIndexedDB();
  const context = {
    console,
    indexedDB: strict.factory,
    IDBKeyRange: {
      only: (value) => value,
      lowerBound: (value) => value,
    },
    navigator: {
      userAgent:
        "Mozilla/5.0 AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Safari/604.1",
    },
    crypto: {
      getRandomValues(bytes) {
        bytes.fill(7);
        return bytes;
      },
    },
    setTimeout,
    clearTimeout,
    setImmediate,
  };
  context.globalThis = context;
  vm.runInNewContext(DATA_STORE, context, { filename: "data-store.js" });
  vm.runInNewContext(INDEXEDDB_STORE, context, { filename: "indexeddb-store.js" });

  const store = context.BWReaderRuntime.indexedDBStore.createIndexedDBDataStore({
    dbName: "webkit-strict-transaction",
    deviceId: "webkit-test",
    broadcast: false,
  });
  const epoch = await store.instanceEpoch();

  assert.match(epoch, /^data-store-instance-v1-[a-f0-9]{32}$/);
  assert.ok(strict.keepaliveReads() > 0, "readwrite transaction used a keepalive request");
  assert.equal(await store.instanceEpoch(), epoch, "the committed value remains readable");
  store.close();
  await new Promise((resolve) => setImmediate(resolve));
});

// 2026-09-03/04 App 日志两次实锤:网页进程挂起/恢复后第一笔 readwrite 事务报 UnknownError
// 「Attempt to get a record from database without an in-progress transaction」,复制命令入队失败 = 静默分叉。
// 事务失败即整体回滚,重开连接重试一次是安全的;只认这一类错误。
test("过期连接错误重开连接重试一次,其他错误不重试", () => {
  const SRC = readFileSync(new URL("../../_server_deploy/static/reader-runtime/indexeddb-store.js", import.meta.url), "utf8");
  assert.match(SRC, /function isStaleConnectionError\(error\)/);
  assert.match(SRC, /without an in-progress transaction/);
  assert.match(SRC, /return transactOnce\(storeNames, mode, worker, transactionOptions\)\.catch\(function \(error\) \{\n\s*if \(!isStaleConnectionError\(error\) \|\| closed\) throw error;/);
  assert.match(SRC, /databasePromise = null;\n\s*return new Promise\(function \(resolve\) \{ root\.setTimeout\(resolve, 120\); \}\)/);
});
