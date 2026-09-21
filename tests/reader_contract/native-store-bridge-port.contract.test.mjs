// port → 消息通道的那根管子。
//
// 它的危险不在"会不会报错"，而在**会不会悄悄说谎**：
// · 通道不在时假装成功 → 数据写进了空气；
// · 记录解析失败时当成"没有这条" → 调用方新建一条，把原来的覆盖掉；
// · 在这里加缓存 → 「我刚写的东西读回来还是旧的」，而上下两层看起来都对。
import assert from "node:assert/strict";
import test from "node:test";

const mod = await import("../../_server_deploy/static/reader-runtime/native-store-bridge-port.js")
  .then((m) => m.default ?? m);
const { createBridgePort, available, MESSAGE } = mod;

/** 装一个假的 webkit 通道，返回收到的请求列表。 */
function install(reply) {
  const seen = [];
  globalThis.webkit = {
    messageHandlers: {
      [MESSAGE]: {
        postMessage: async (request) => {
          seen.push(request);
          return typeof reply === "function" ? reply(request) : reply;
        }
      }
    }
  };
  return seen;
}

function uninstall() { delete globalThis.webkit; }

test("没有通道时报错，绝不假装成功", async () => {
  // ⚠ 这条是这个文件里最要紧的：假装成功＝数据写进了空气，
  // 而调用方会把它当成已经落库。
  uninstall();
  assert.equal(available(), false);
  const port = createBridgePort({ store: "bw-reader-native-v1-global" });
  await assert.rejects(() => port.read("n", "a"), /UNAVAILABLE/);
  await assert.rejects(() => port.commit([{}]), /UNAVAILABLE/);
});

test("每条请求都带库名", async () => {
  const seen = install({ ok: true, record: null });
  const port = createBridgePort({ store: "bw-reader-native-v1-document" });
  await port.read("n", "a");
  assert.equal(seen[0].store, "bw-reader-native-v1-document");
  assert.equal(seen[0].action, "read");
  uninstall();
});

test("记录以 JSON 串来回，解析得出对象", async () => {
  install({ ok: true, record: JSON.stringify({ id: "a", rev: 2 }) });
  const port = createBridgePort({ store: "bw-reader-native-v1-global" });
  const record = await port.read("n", "a");
  assert.equal(record.rev, 2);
  uninstall();
});

test("记录坏了要报错，不能当成「没有这条」", async () => {
  // ⚠ 当成"没有"的话，调用方会新建一条，把原来那条覆盖掉 —— 一次读失败
  // 变成一次数据丢失。
  install({ ok: true, record: "{坏掉的" });
  const port = createBridgePort({ store: "bw-reader-native-v1-global" });
  await assert.rejects(() => port.read("n", "a"), /CORRUPT/);
  uninstall();
});

test("null 是合法的「没有这条」", async () => {
  install({ ok: true, record: null });
  const port = createBridgePort({ store: "bw-reader-native-v1-global" });
  assert.equal(await port.read("n", "a"), null);
  uninstall();
});

test("冲突翻成 ConflictError，让上层去重试", async () => {
  // 乐观并发的冲突是**正常分支**；翻成普通错误的话上层不会重试，
  // 用户看到的是"保存失败"而其实只要再来一轮就好。
  install({ ok: false, code: "BW_DATA_CONFLICT" });
  const port = createBridgePort({ store: "bw-reader-native-v1-global" });
  await assert.rejects(() => port.commit([{ collection: "n", id: "a" }]), (error) => {
    assert.equal(error.code, "BW_DATA_CONFLICT");
    assert.equal(error.name, "ConflictError");
    return true;
  });
  uninstall();
});

test("readMany 空输入不过桥", async () => {
  const seen = install({ ok: true, records: [] });
  const port = createBridgePort({ store: "bw-reader-native-v1-global" });
  assert.deepEqual(await port.readMany([]), []);
  assert.equal(seen.length, 0, "空输入还去过一次桥是白费");
  uninstall();
});

test("listCollection 不给 limit 补默认值", async () => {
  // ⚠ 补一个会把「我要全部」悄悄变成「我要前 200 条」——
  // 表现是列表莫名其妙少了一截，而且只在数据多的人那里出现。
  const seen = install({ ok: true, records: [] });
  const port = createBridgePort({ store: "bw-reader-native-v1-global" });
  await port.listCollection("n", { includeDeleted: true });
  assert.equal("limit" in seen[0], false);
  await port.listCollection("n", { limit: 10, offset: 5 });
  assert.equal(seen[1].limit, 10);
  assert.equal(seen[1].offset, 5);
  uninstall();
});

test("管子里没有缓存 —— 读两次就过两次桥", async () => {
  // ⚠ 在这里加缓存，「我刚写的东西读回来还是旧的」就会从这里开始，
  // 而上下两层看起来都对。
  const seen = install({ ok: true, record: JSON.stringify({ id: "a" }) });
  const port = createBridgePort({ store: "bw-reader-native-v1-global" });
  await port.read("n", "a");
  await port.read("n", "a");
  assert.equal(seen.length, 2);
  uninstall();
});

test("journal 的三个字段原样带回", async () => {
  install({ ok: true, items: [JSON.stringify({ cursor: 1, operation: "put" })],
            cursor: 7, oldestCursor: 3 });
  const port = createBridgePort({ store: "bw-reader-native-v1-global" });
  const page = await port.journal({ after: 0, limit: 10 });
  assert.equal(page.items[0].operation, "put");
  assert.equal(page.cursor, 7);
  assert.equal(page.oldestCursor, 3);
  uninstall();
});

test("和 native-store 接得上（端到端跑一遍）", async () => {
  // 这条是真正的验收：用假通道模拟原生侧，让完整的 store 跑一次读写。
  const records = new Map();
  const journal = [];
  let cursor = 0;
  const key = (c, i) => JSON.stringify([c, i]);
  install(async (request) => {
    switch (request.action) {
      case "readMany":
        return { ok: true, records: request.keys.map((k) => records.get(key(k.collection, k.id)) ?? null) };
      case "read":
        return { ok: true, record: records.get(key(request.collection, request.id)) ?? null };
      case "remembered":
        return { ok: true, record: null };
      case "commit": {
        const cursors = [];
        for (const entry of request.entries) {
          records.set(key(entry.collection, entry.id), JSON.stringify(entry.record));
          cursor += 1;
          cursors.push(cursor);
          journal.push(JSON.stringify({ ...entry.change, cursor }));
        }
        return { ok: true, cursors };
      }
      case "journal":
        return { ok: true, items: journal, cursor, oldestCursor: journal.length ? 1 : cursor + 1 };
      default:
        return { ok: true };
    }
  });
  const { createNativeDataStore } = await import(
    "../../_server_deploy/static/reader-runtime/native-store.js").then((m) => m.default ?? m);
  const db = createNativeDataStore({
    port: createBridgePort({ store: "bw-reader-native-v1-global" }),
    deviceId: "dev"
  });
  const written = await db.put("hl", { id: "a", color: "yellow" });
  assert.equal(written.rev, 1);
  assert.equal((await db.get("hl", "a")).value.color, "yellow");
  const page = await db.changes({ after: 0 });
  assert.equal(page.changes[0].operation, "put");
  assert.equal(page.changes[0].cursor, 1);
  uninstall();
});

test("默认不启用 —— 开关＋通道都在才接管", async () => {
  const { readFileSync } = await import("node:fs");
  const RUNTIME = readFileSync(new URL(
    "../../_server_deploy/static/pdf/native-local-runtime.js", import.meta.url), "utf8");
  const fn = RUNTIME.slice(RUNTIME.indexOf("function nativeStoreEnabled()"),
                           RUNTIME.indexOf("function createStores()"));
  // ⚠ 三样都要：开关、通道真的在、两个模块都装上了。任何一样缺席就走
  // IndexedDB —— 存储是唯一一类"选错了就把数据弄没"的东西。
  assert.match(fn, /localStorage\.getItem\('bw-native-data-store'\) !== '1'\) return false/);
  assert.match(fn, /bridge\.available\(\)/);
  assert.match(fn, /typeof store\.createNativeDataStore === 'function'/);
  // ⚠ 运行中出错**不许**静默回退到 IndexedDB：那会造成"一半数据在这边、
  // 一半在那边"，而且没人会发现。
  const create = RUNTIME.slice(RUNTIME.indexOf("function createStores()"),
                               RUNTIME.indexOf("function createRouter("));
  assert.doesNotMatch(create, /catch\s*\([^)]*\)\s*\{[^}]*createIndexedDBDataStore/);
  // 三个库都要换，不能只换一个（书的隔离靠 documentId，不是靠分库）。
  for (const suffix of ["-global", "-document", "-device"]) {
    assert.ok(create.includes(`makeNative('${suffix}'`), suffix + " 没走新存储");
  }
});
