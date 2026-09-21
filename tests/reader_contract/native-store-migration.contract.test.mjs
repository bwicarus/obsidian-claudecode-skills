// 老数据搬家（IndexedDB → App 沙盒 SQLite）的判据。
//
// 这个文件盯的是**搬家特有的**错，它们都长一个样：搬少了，而检查还说没问题。
//   · list 缺省只回 200 条 → 每个集合只搬走前 200，前后一比都是 200；
//   · 因果集合（卡片）缺 snapshotBaseline → 全变成 conflicts，静静地一条不落；
//   · 只看 global 的完成标记 → device 搬到一半被打断，下次直接跳过。
// 所以这里既有真跑的行为用例（真 native-store + 假 port + 假旧库），也有对着
// runtime 源码钉住的几条约束 —— 那几条是"顺序"和"开关"，行为测不到。
import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";

const RUNTIME = readFileSync(new URL(
  "../../_server_deploy/static/pdf/native-local-runtime.js", import.meta.url), "utf8");

const section = (from, to) => {
  const start = RUNTIME.indexOf(from);
  const end = RUNTIME.indexOf(to, start + from.length);
  assert.ok(start >= 0 && end > start, "锚点找不到：" + from + " / " + to);
  return RUNTIME.slice(start, end);
};

// ⚠ 只切到 storeMeta 之前 —— 也就是搬家那个函数的**函数体**。切得太宽会把下面
//   helper 的注释也圈进来，于是"不许出现 localStorage"这类断言被自己写的注释
//   绊倒（今天已经在别处栽过一次）。
const MIGRATION = section("function migrateLegacyStoresOnBoot()", "function storeMeta(store, key)");
const BOOT = section("stores = createStores();", "attachPreferenceStore();");

test("翻页读到底 —— 不许把一次 list 当成全部", () => {
  // ⚠ IndexedDB 与 native 两边的 list 都是「缺省 200、上限 1000」。一次读当全部
  //   的表现是每个集合只搬走前 200 条，而且旧库新库都只数到 200，比总数发现不了。
  assert.match(MIGRATION, /orderBy: 'id', limit: 1000/, "没按上限翻页");
  assert.match(MIGRATION, /query\.afterId = afterId/, "没有按 id 续翻");
  assert.match(MIGRATION, /return page\(rows\[rows\.length - 1\]\.id\)/, "翻页没有递归到底");
  // offset 翻页会随数据变动错位，这里明确不用它。
  assert.doesNotMatch(MIGRATION, /offset: index/, "别用 offset 翻页");
});

test("逐条对账 —— applied + skipped 必须等于送进去的条数", () => {
  assert.match(MIGRATION,
    /var accounted = \(report\.applied \|\| \[\]\)\.length \+ \(report\.skipped \|\| \[\]\)\.length/);
  assert.match(MIGRATION, /accounted !== slice\.length/);
  assert.match(MIGRATION, /BW_LEGACY_IMPORT_SHORT/);
});

test("因果集合要带 snapshotBaseline，否则卡片一条都进不来", () => {
  assert.match(MIGRATION, /snapshotBaseline: true/);
});

test("走 applyChanges，不许走 put/batch", () => {
  // put 会把 rev 推高、updatedAt 换成现在 —— 整库都变成"刚刚改的"，
  // 于是同步把它当成一库新改动推一遍。
  assert.match(MIGRATION, /target\.applyChanges\(/);
  assert.doesNotMatch(MIGRATION, /target\.put\(/);
  assert.doesNotMatch(MIGRATION, /target\.batch\(/);
});

test("不造出站事件 —— 迁移不带 journal:true", () => {
  // journal 是"待发出"的队列。凭空造一库出站事件没有意义，而新库 epoch 是新的，
  // 同步本来就会做一次完整对账，漏了什么由对账补回来。
  assert.doesNotMatch(MIGRATION, /journal: true/);
});

test("三个库各自的完成标记都要为真才跳", () => {
  assert.match(MIGRATION, /flags\.every\(function \(flag\) \{ return flag === 'done'; \}\)/);
});

test("进度标记写在数据所在的库里，不是 localStorage", () => {
  // 标记与数据同生共死，才不会出现"标记说搬完了、库其实是空的"。
  const helpers = section("function storeMeta(store, key)", "function dataError(");
  assert.match(helpers, /store\.meta\(key\)/);
  assert.match(helpers, /store\.putMeta\(key, value\)/);
  assert.doesNotMatch(MIGRATION, /localStorage/);
});

test("空的 databases() 不当成「没有旧库」", () => {
  // 误判"没有"＝跳过真迁移，用户看到空书架；误判"有"＝白建三个空库。
  assert.match(MIGRATION, /if \(!list \|\| !list\.length\) return null;/);
});

test("迁移排在启动链最前", () => {
  // 晚一步的表现是用户看见一个空书架，然后在空的上面开始新建东西 ——
  // 等迁移补上来，两份就都在了。
  const migrate = BOOT.indexOf("migrateLegacyStoresOnBoot()");
  assert.ok(migrate >= 0, "启动链里没挂迁移");
  ["maintainDeviceStoreOnBoot()", "migrateHighlightSplitOnBoot()",
   "recoverNativePDFMutationOnBoot()"].forEach((later) => {
    const at = BOOT.indexOf(later);
    assert.ok(at > migrate, "迁移应排在 " + later + " 之前");
  });
});

test("新存储接管时不跑 device 库回收", () => {
  // 那套回收按名字删的是 IndexedDB 库、重建出来的也是 IndexedDB store ——
  // 在新存储上跑一次，就把 device 偷偷换回 IndexedDB，而其余两个还在 SQLite。
  assert.match(BOOT,
    /if \(nativeStoreEnabled\(\)\) return null;\s*\n\s*return maintainDeviceStoreOnBoot\(\);/);
});

// ── 真跑一遍：假旧库 + 真 native-store + 假 port ──

const { createNativeDataStore } = await import(
  "../../_server_deploy/static/reader-runtime/native-store.js").then((m) => m.default ?? m);

/** 够用的内存 port（与 native-store 用例里那个同形：逐条核对 + 失败回滚）。 */
function makePort() {
  const records = new Map();
  const mutations = new Map();
  const meta = new Map();
  const journal = [];
  let cursor = 0;
  const key = (collection, id) => JSON.stringify([collection, id]);
  return {
    async read(collection, id) { return records.get(key(collection, id)) ?? null; },
    async readMany(keys) {
      return keys.map((item) => records.get(key(item.collection, item.id)) ?? null);
    },
    async listCollection(collection, options = {}) {
      let rows = [...records.values()].filter((record) => record.collection === collection);
      if (!options.includeDeleted) rows = rows.filter((record) => !record.deleted);
      rows.sort((a, b) => (a.updatedAt - b.updatedAt) || (a.id < b.id ? -1 : 1));
      if (options.limit != null) {
        rows = rows.slice(options.offset ?? 0, (options.offset ?? 0) + options.limit);
      }
      return rows;
    },
    async remembered(id) { return mutations.get(id) ?? null; },
    async commit(entries) {
      const undo = { records: new Map(records), cursor, journal: journal.length };
      const cursors = [];
      try {
        for (const entry of entries) {
          const slot = key(entry.collection, entry.id);
          const current = records.get(slot);
          if (entry.expectedRev != null && (current ? current.rev : 0) !== entry.expectedRev) {
            const error = new Error("rev 对不上");
            error.code = "BW_DATA_CONFLICT";
            throw error;
          }
          records.set(slot, entry.record);
          if (entry.journal === false) {
            cursors.push(0);
          } else {
            cursor += 1;
            cursors.push(cursor);
            journal.push({ ...(entry.change ?? {}), cursor });
          }
          if (entry.mutationId) mutations.set(entry.mutationId, entry.record);
        }
      } catch (error) {
        records.clear();
        undo.records.forEach((value, slot) => records.set(slot, value));
        cursor = undo.cursor;
        journal.length = undo.journal;
        throw error;
      }
      return { cursors };
    },
    async journal({ after = 0, limit = 500 } = {}) {
      return { items: journal.filter((item) => item.cursor > after).slice(0, limit),
               cursor, oldestCursor: journal.length ? journal[0].cursor : cursor + 1 };
    },
    async meta(name) { return meta.get(name) ?? null; },
    async putMeta(name, value) { meta.set(name, value); },
    async info() { return { cursor, journalSize: journal.length, collections: [] }; }
  };
}

/** 假旧库：只要 list，而且**照抄真的那套上限**（缺省 200／上限 1000）。
 *  假货比真货宽松的话，真货才会出的问题就被掩盖掉了。 */
function makeLegacy(rows) {
  return {
    list(collection, query = {}) {
      const limit = Math.max(1, Math.min(1000, Number(query.limit) || 200));
      let out = rows.filter((record) => record.collection === collection);
      if (!query.includeDeleted) out = out.filter((record) => !record.deleted);
      out.sort((a, b) => (a.id < b.id ? -1 : 1));
      if (query.orderBy === "id" && query.afterId != null) {
        out = out.filter((record) => String(record.id) > String(query.afterId));
      }
      return Promise.resolve(out.slice(0, limit));
    }
  };
}

function legacyRecord(collection, id, rev = 1) {
  return { schema: 1, collection, id, rev, updatedAt: 1000 + rev,
           updatedBy: "old-device", deleted: false, value: { id, tag: collection } };
}

/** 照 runtime 里那套翻页 + 分批搬一个集合，返回搬走的条数。 */
async function migrate(legacy, target, collection, options = {}) {
  let moved = 0;
  let afterId = null;
  for (;;) {
    const query = { includeDeleted: true, orderBy: "id", limit: 1000 };
    if (afterId != null) query.afterId = afterId;
    const page = await legacy.list(collection, query);
    if (!page.length) break;
    for (let index = 0; index < page.length; index += 100) {
      const slice = page.slice(index, index + 100);
      const report = await target.applyChanges(
        slice.map((record) => ({ collection, record })),
        { snapshotBaseline: options.snapshotBaseline !== false });
      assert.deepEqual(report.conflicts, [], "迁移被判冲突了");
      assert.equal(report.applied.length + report.skipped.length, slice.length,
                   "有记录既没落地也没被跳过");
    }
    moved += page.length;
    afterId = page[page.length - 1].id;
  }
  return moved;
}

test("1234 条的集合要一条不少地搬过去（缺省 200 的陷阱）", async () => {
  // ⚠ 这条是「翻页读到底」那条约束的行为版：不翻页的话只到 200，
  //   而且两边都只有 200，比总数看不出来。
  const total = 1234;
  const rows = Array.from({ length: total }, (_, index) =>
    legacyRecord("hl", "id-" + String(index).padStart(5, "0")));
  const target = createNativeDataStore({ port: makePort(), deviceId: "dev" });
  assert.equal(await migrate(makeLegacy(rows), target, "hl"), total);
  assert.equal((await target.get("hl", "id-01233")).rev, 1, "最后一条没到");
  assert.equal((await target.get("hl", "id-00200")).rev, 1, "第 200 条之后的没到");
});

test("再搬一遍是幂等的：全进 skipped，rev 不动", async () => {
  // 中断重入是常态（迁移可能被杀在任何一刻）。重跑把 rev 推高的表现是
  // 同步把整库当成新改动又推一遍。
  const rows = [legacyRecord("hl", "a", 3), legacyRecord("hl", "b", 5)];
  const legacy = makeLegacy(rows);
  const target = createNativeDataStore({ port: makePort(), deviceId: "dev" });
  await migrate(legacy, target, "hl");
  const changes = rows.map((record) => ({ collection: "hl", record }));
  const again = await target.applyChanges(changes, { snapshotBaseline: true });
  assert.equal(again.applied.length, 0);
  assert.equal(again.skipped.length, 2);
  assert.equal((await target.get("hl", "a")).rev, 3, "重跑把 rev 推高了");
});

test("墓碑也要搬 —— 否则删掉的东西会在别的设备上复活", async () => {
  const tombstone = { ...legacyRecord("hl", "gone", 2), deleted: true };
  const target = createNativeDataStore({ port: makePort(), deviceId: "dev" });
  const report = await target.applyChanges(
    [{ collection: "hl", record: tombstone }], { snapshotBaseline: true });
  assert.deepEqual(report.conflicts, []);
  assert.equal(await target.get("hl", "gone"), null);
  assert.equal((await target.get("hl", "gone", { includeDeleted: true })).deleted, true);
  // ⚠ 旧库的 list 默认不含墓碑，所以迁移必须显式 includeDeleted —— 少了它，
  //   墓碑一条都过不来，而 applied 数目看着完全正常。
  assert.match(MIGRATION, /includeDeleted: true, orderBy: 'id'/);
});

test("因果集合没有 snapshotBaseline 就全军覆没（所以那个开关必须在）", async () => {
  const target = createNativeDataStore({
    port: makePort(), deviceId: "dev", causalCollections: ["card-entities"] });
  const change = [{ collection: "card-entities", record: legacyRecord("card-entities", "c1", 4) }];
  const without = await target.applyChanges(change);
  assert.equal(without.applied.length, 0, "没开关却搬进去了？那这条用例失去意义");
  assert.equal(without.conflicts.length, 1);
  const withFlag = await target.applyChanges(change, { snapshotBaseline: true });
  assert.deepEqual(withFlag.conflicts, []);
  assert.equal((await target.get("card-entities", "c1")).rev, 4);
});

test("迁移不动 journal：搬完一库，出站队列还是空的", async () => {
  const target = createNativeDataStore({ port: makePort(), deviceId: "dev" });
  await migrate(makeLegacy([legacyRecord("hl", "a")]), target, "hl");
  const page = await target.changes({ after: 0 });
  assert.deepEqual(page.changes, [], "迁移造出了出站事件");
  assert.equal(page.cursor, 0, "迁移动了游标");
});

test("保留键挡住：外面不能覆盖 instanceEpoch / cursor", async () => {
  // epoch 被覆盖＝拿着"已经同步过"的承诺去盖一个可能不完整的库；
  // cursor 被覆盖＝同步跳过一段历史，或者把发过的再发一遍。
  const target = createNativeDataStore({ port: makePort(), deviceId: "dev" });
  await assert.rejects(() => target.putMeta("instanceEpoch", "e-fake"));
  await assert.rejects(() => target.putMeta("cursor", "999"));
  assert.equal(await target.putMeta("legacyImport", "done"), true);
  assert.equal(await target.meta("legacyImport"), "done");
});

test("不搬老的 instanceEpoch —— 让同步做一次完整对账", async () => {
  // ⚠ 搬了 epoch，旧 checkpoint 就继续有效，于是同步以为"这库已经同步过了"。
  //   要是迁移恰好漏了点什么，那份漏就被这句承诺永久盖住了。不搬 → epoch 是
  //   新的 → checkpoint 作废 → 完整对账 → 漏掉的从服务器补回来。
  assert.doesNotMatch(MIGRATION, /instanceEpoch/);
});
