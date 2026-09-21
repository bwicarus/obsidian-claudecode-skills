// JS 侧的 native store —— 真跑的用例，不是看文本。
//
// 它与 indexeddb-store 对外同形，区别只在底下：那边用 IndexedDB 事务，这边把
// 一次写入整体交给原生侧。判据（记录该长什么样、rev 怎么涨、墓碑怎么算）
// **两边共用 data-store.js 那一份** —— 这里的用例就是在守这件事。
//
// port 是个内存假货：够跑完整语义，又不需要设备。
import assert from "node:assert/strict";
import test from "node:test";

const { createNativeDataStore } = await import(
  "../../_server_deploy/static/reader-runtime/native-store.js").then((m) => m.default ?? m);

/** 内存 port —— 行为对齐 ReaderNativeDataStore.swift（复合键、乐观并发、游标递增）。 */
function makePort() {
  const records = new Map();            // key(collection, id) -> record
  const mutations = new Map();          // mutationId -> record
  const journal = [];                   // {cursor, ...change}
  const meta = new Map();
  let cursor = 0;
  // ⚠ 用 JSON 数组当键，不用分隔符拼字符串 —— 原生那边就因为拼串
  // （NUL 被 bind_text 截断）丢过数据。这里是假货也照同一条规矩：
  // 假货比真货宽松的话，真货才会出的问题就被掩盖掉了。
  const key = (collection, id) => JSON.stringify([collection, id]);

  const port = {
    commits: 0,
    reads: 0,
    async read(collection, id) {
      port.reads += 1;
      return records.get(key(collection, id)) ?? null;
    },
    async readMany(requests) {
      port.reads += 1;
      return requests.map((request) => records.get(key(request.collection, request.id)) ?? null);
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
    async remembered(mutationId) {
      return mutations.get(mutationId) ?? null;
    },
    async commit(entries) {
      port.commits += 1;
      // ⚠ 逐条「核对 → 写」，整批包一层回滚 —— 照抄
      //   `commitWithinTransaction` 在 SQLite 事务里的做法：
      //   · 改成"先整批核对、再整批写"会把**同一批里两条改同一条 id**
      //     判成冲突（第二条的 expectedRev 是第一条将要写出的 rev，
      //     核对的那一刻还没写）；
      //   · 而回滚才是"任何一条不过就整批不落"的来源 —— 半批落地是 batch
      //     最不该出现的结果：调用方拿到"失败"，库里却留下了一半。
      const undo = {
        records: new Map(records), mutations: new Map(mutations),
        journal: journal.length, cursor
      };
      const cursors = [];
      try {
        for (const entry of entries) {
          const slot = key(entry.collection, entry.id);
          const current = records.get(slot);
          if (entry.expectedRev != null &&
              (current ? current.rev : 0) !== entry.expectedRev) {
            const error = new Error("rev 对不上");
            error.code = "BW_DATA_CONFLICT";
            throw error;
          }
          records.set(slot, entry.record);
          // ⚠ `journal: false` → 只写记录，不进 journal、不动游标，游标回 0。
          //   桥那边是同一条规矩（默认 true，缺字段的调用方照旧入队）。
          if (entry.journal === false) {
            cursors.push(0);
          } else {
            cursor += 1;
            cursors.push(cursor);
            // 与原生桥同样的做法：调用方给的信封不带 cursor，提交时才填。
            journal.push({ ...(entry.change ?? {}), cursor });
            meta.set("cursor", String(cursor));
          }
          if (entry.mutationId) mutations.set(entry.mutationId, entry.record);
        }
      } catch (error) {
        records.clear(); undo.records.forEach((value, slot) => records.set(slot, value));
        mutations.clear(); undo.mutations.forEach((value, id) => mutations.set(id, value));
        journal.length = undo.journal;
        cursor = undo.cursor;
        meta.set("cursor", String(cursor));
        throw error;
      }
      return { cursors };
    },
    async journal({ after = 0, limit = 500 } = {}) {
      const items = journal.filter((item) => item.cursor > after).slice(0, limit);
      return { items, cursor, oldestCursor: journal.length ? journal[0].cursor : cursor + 1 };
    },
    async meta(name) { return meta.get(name) ?? null; },
    async putMeta(name, value) { meta.set(name, value); },
    async info() {
      return {
        cursor,
        journalSize: journal.length,
        collections: [...new Set([...records.values()].map((record) => record.collection))]
      };
    },
    // 测试钩子
    _forceRev(collection, id, rev) {
      const record = records.get(key(collection, id));
      if (record) record.rev = rev;
    },
    _dropOldestJournal(count) { journal.splice(0, count); }
  };
  return port;
}

const store = (port, extra = {}) =>
  createNativeDataStore({ port, deviceId: "dev", ...extra });

test("put 之后读得回来，rev 从 1 起", async () => {
  const port = makePort();
  const db = store(port);
  const written = await db.put("hl", { id: "a", color: "yellow" });
  assert.equal(written.rev, 1);
  assert.equal(written.collection, "hl");
  const read = await db.get("hl", "a");
  assert.equal(read.value.color, "yellow");
});

test("再 put 一次 rev 涨到 2", async () => {
  const port = makePort();
  const db = store(port);
  await db.put("hl", { id: "a", color: "yellow" });
  const second = await db.put("hl", { id: "a", color: "green" });
  assert.equal(second.rev, 2);
  assert.equal((await db.get("hl", "a")).value.color, "green");
});

test("remove 写的是墓碑，默认读不到、includeDeleted 才读得到", async () => {
  // ⚠ 墓碑不是"删掉这行"：另一台设备上那条还活着的记录要靠它压住，
  // 否则删掉的东西会自己回来。
  const port = makePort();
  const db = store(port);
  await db.put("hl", { id: "a" });
  const tombstone = await db.remove("hl", "a");
  assert.equal(tombstone.deleted, true);
  assert.equal(await db.get("hl", "a"), null);
  assert.equal((await db.get("hl", "a", { includeDeleted: true })).deleted, true);
});

test("ifRev 对不上要拒绝，而不是覆盖", async () => {
  const port = makePort();
  const db = store(port);
  await db.put("hl", { id: "a", color: "yellow" });
  await assert.rejects(() => db.put("hl", { id: "a", color: "red" }, { ifRev: 5 }));
  // 拒绝之后原值不能被动过
  assert.equal((await db.get("hl", "a")).value.color, "yellow");
});

test("同一个 mutationId 重放不会再写一遍", async () => {
  // ⚠ 离线队列重发是常态；不做重放去重的话 rev 会被一路推高，
  // 而每一次都会生成一条 journal，把别的设备的对齐点冲掉。
  const port = makePort();
  const db = store(port);
  const first = await db.put("hl", { id: "a" }, { mutationId: "m1" });
  const commitsAfterFirst = port.commits;
  const again = await db.put("hl", { id: "a" }, { mutationId: "m1" });
  assert.equal(again.rev, first.rev);
  assert.equal(port.commits, commitsAfterFirst, "重放不该再提交一次");
});

test("list 按 updatedAt 排、能翻页、默认不含墓碑", async () => {
  const port = makePort();
  let now = 0;
  const db = store(port, { clock: () => (now += 10) });
  await db.put("n", { id: "z" });
  await db.put("n", { id: "y" });
  await db.put("n", { id: "x" });
  await db.remove("n", "y");
  assert.deepEqual((await db.list("n")).map((r) => r.id), ["z", "x"]);
  assert.deepEqual((await db.list("n", { includeDeleted: true })).map((r) => r.id).sort(),
                   ["x", "y", "z"]);
  assert.deepEqual((await db.list("n", { limit: 1 })).map((r) => r.id), ["z"]);
  assert.deepEqual((await db.list("n", { limit: 1, offset: 1 })).map((r) => r.id), ["x"]);
});

test("list 的 orderBy:id / afterId / documentId 走本地过滤", async () => {
  // 这几种要看记录内部，下推不了 —— 与 IndexedDB 那边的做法保持一致。
  const port = makePort();
  const db = store(port);
  await db.put("n", { id: "b", documentId: "d1" });
  await db.put("n", { id: "a", documentId: "d2" });
  await db.put("n", { id: "c", documentId: "d1" });
  assert.deepEqual((await db.list("n", { orderBy: "id" })).map((r) => r.id), ["a", "b", "c"]);
  assert.deepEqual((await db.list("n", { orderBy: "id", afterId: "a" })).map((r) => r.id), ["b", "c"]);
  assert.deepEqual((await db.list("n", { documentId: "d1" })).map((r) => r.id).sort(), ["b", "c"]);
});

test("getMany 一次过桥，不是循环单取", async () => {
  // ⚠ 逐条往返会把桥变成瓶颈，而且拿到的快照还不一致。
  const port = makePort();
  const db = store(port);
  await db.put("n", { id: "a" });
  await db.put("n", { id: "b" });
  const before = port.reads;
  const got = await db.getMany([{ collection: "n", id: "a" }, { collection: "n", id: "b" },
                                { collection: "n", id: "missing" }]);
  assert.equal(port.reads - before, 1, "getMany 应该只过一次桥");
  assert.deepEqual(got.map((r) => r && r.id), ["a", "b", null]);
});

test("batch 整批一次提交，半批落地是不允许的", async () => {
  const port = makePort();
  const db = store(port);
  const before = port.commits;
  const results = await db.batch([
    { collection: "n", value: { id: "a" } },
    { collection: "n", value: { id: "b" } }
  ]);
  assert.equal(results.length, 2);
  assert.equal(port.commits - before, 1, "batch 应该只提交一次");
  assert.deepEqual((await db.list("n")).map((r) => r.id).sort(), ["a", "b"]);
});

test("批里有一条冲突 → 整批不落", async () => {
  const port = makePort();
  const db = store(port);
  await db.put("n", { id: "a" });
  await assert.rejects(() => db.batch([
    { collection: "n", value: { id: "a" }, options: { ifRev: 99 } },
    { collection: "n", value: { id: "b" } }
  ]));
  assert.equal(await db.get("n", "b"), null, "半批落地了");
});

test("并发冲突会重试，重试是重新读+重新算", async () => {
  // ⚠ 重试不是"多试几次碰运气"：每一轮都重新读当前记录、重新算记录，
  // 所以第二轮拿到的是别人写完之后的状态。
  const port = makePort();
  const db = store(port);
  await db.put("n", { id: "a", v: 1 });
  const realCommit = port.commit.bind(port);
  let injected = false;
  port.commit = async (entries) => {
    if (!injected) {
      injected = true;
      // 模拟"提交那一刻被别人抢先写了一版"
      port._forceRev("n", "a", 7);
      const error = new Error("rev 对不上");
      error.code = "BW_DATA_CONFLICT";
      throw error;
    }
    return realCommit(entries);
  };
  const written = await db.put("n", { id: "a", v: 2 });
  assert.equal(written.rev, 8, "应该在别人那版基础上继续涨");
});

test("重试有上限，不会无限转圈", async () => {
  const port = makePort();
  const db = store(port, { maxRetries: 2 });
  port.commit = async () => {
    const error = new Error("rev 对不上");
    error.code = "BW_DATA_CONFLICT";
    throw error;
  };
  await assert.rejects(() => db.put("n", { id: "a" }), /rev/);
});

test("changes 给出游标、hasMore 与 resetRequired", async () => {
  const port = makePort();
  const db = store(port);
  await db.put("n", { id: "a" });
  await db.put("n", { id: "b" });
  await db.put("n", { id: "c" });
  const page = await db.changes({ after: 0, limit: 2 });
  assert.equal(page.changes.length, 2);
  assert.equal(page.nextCursor, 2);
  assert.equal(page.cursor, 3);
  assert.equal(page.hasMore, true);
  assert.equal(page.resetRequired, false);
  const rest = await db.changes({ after: page.nextCursor });
  assert.equal(rest.changes.length, 1);
  assert.equal(rest.hasMore, false);
});

test("要的位置比现在还新 → resetRequired", async () => {
  const port = makePort();
  const db = store(port);
  await db.put("n", { id: "a" });
  const page = await db.changes({ after: 99 });
  assert.equal(page.resetRequired, true, "游标错乱时不能假装没有变更");
});

test("要的位置已经被裁掉 → resetRequired", async () => {
  // ⚠ 这条不报的话，调用方会以为自己已经对齐，而中间那段变更永远补不回来。
  const port = makePort();
  const db = store(port);
  for (const id of ["a", "b", "c", "d"]) await db.put("n", { id });
  port._dropOldestJournal(2);            // 最旧的两条被裁了
  const page = await db.changes({ after: 0 });
  assert.equal(page.resetRequired, true);
});

test("subscribe 收到写入通知，退订之后不再收", async () => {
  const port = makePort();
  const db = store(port);
  const seen = [];
  const off = db.subscribe((change) => seen.push(change.record.id));
  await db.put("n", { id: "a" });
  assert.deepEqual(seen, ["a"]);
  off();
  await db.put("n", { id: "b" });
  assert.deepEqual(seen, ["a"], "退订之后不该再收到");
});

test("subscribe 可以只听一个 collection", async () => {
  const port = makePort();
  const db = store(port);
  const seen = [];
  db.subscribe({ collection: "n" }, (change) => seen.push(change.collection));
  await db.put("n", { id: "a" });
  await db.put("other", { id: "a" });
  assert.deepEqual(seen, ["n"]);
});

test("status 报的是 native 后端", async () => {
  const port = makePort();
  const db = store(port);
  await db.put("n", { id: "a" });
  const info = await db.status();
  assert.equal(info.backend, "native-sqlite");
  assert.equal(info.cursor, 1);
  assert.equal(info.journalSize, 1);
  assert.deepEqual(info.collections, ["n"]);
});

test("instanceEpoch 只生成一次，之后稳定", async () => {
  // 它防的是"库被删了重建之后继承一个会跳过历史的游标"。
  const port = makePort();
  const db = store(port);
  const first = await db.instanceEpoch();
  assert.ok(first);
  assert.equal(await db.instanceEpoch(), first);
});

test("close 之后不再接受读写", async () => {
  const port = makePort();
  const db = store(port);
  db.close();
  await assert.rejects(() => db.put("n", { id: "a" }));
  await assert.rejects(() => db.get("n", "a"));
});

test("判据来自 data-store，不是这里自己算的", async () => {
  // 拿 data-store 直接算一遍，两边必须得到同一条记录（除了时间戳）。
  const D = await import("../../_server_deploy/static/reader-runtime/data-store.js")
    .then((m) => m.default ?? m);
  const port = makePort();
  const db = store(port, { clock: () => 12345 });
  const written = await db.put("hl", { id: "a", color: "yellow" });
  const expected = D.makePutRecord("hl", "a", null, { id: "a", color: "yellow" }, {},
                                   { updatedAt: 12345, updatedBy: "dev" });
  assert.deepEqual(written, expected);
});

test("commit 送的是完整变更信封，cursor 由存储侧填", async () => {
  // ⚠ 只送记录本体的话，changes() 拿不到 operation/mutationId —— 增量同步
  // 就无从判断这一条是写还是删、是不是自己刚发出去的。
  const port = makePort();
  const seen = [];
  const realCommit = port.commit.bind(port);
  port.commit = async (entries) => { seen.push(...entries); return realCommit(entries); };
  const db = store(port);
  await db.put("n", { id: "a" }, { mutationId: "m9" });
  assert.equal(seen.length, 1);
  assert.ok(seen[0].change, "没送 change 信封");
  assert.equal(seen[0].change.operation, "put");
  assert.equal(seen[0].change.mutationId, "m9");
  assert.equal(seen[0].change.collection, "n");
  assert.equal(seen[0].change.cursor, undefined, "cursor 该由存储侧分配，不能预先写死");
  const page = await db.changes({ after: 0 });
  assert.equal(page.changes[0].cursor, 1);
  assert.equal(page.changes[0].operation, "put");
});

test("remove 的信封带的是 remove", async () => {
  const port = makePort();
  const db = store(port);
  await db.put("n", { id: "a" });
  await db.remove("n", "a");
  const page = await db.changes({ after: 0 });
  assert.deepEqual(page.changes.map((c) => c.operation), ["put", "remove"]);
  assert.equal(page.changes[1].record.deleted, true);
});

// ── applyChanges（入站：同步把别处的记录写进来）──
//
// 这一组防的是同步里最贵的两类错：
//   · 入站记录被当成本地新改动 → 两台设备来回推同一条，谁都没改过东西；
//   · 入站记录覆盖掉更新的本地版本 → 用户刚写的东西被一次拉取吞掉。

/** 造一条入站变更信封（同步那侧递过来的形状）。 */
function incoming(collection, record, extra = {}) {
  return {
    collection,
    record: { schema: 1, collection, id: record.id, rev: record.rev,
              updatedAt: record.updatedAt ?? 1000, updatedBy: record.updatedBy ?? "peer",
              deleted: record.deleted === true,
              value: record.value ?? { id: record.id } },
    ...extra
  };
}

test("入站记录原样落库：rev / updatedAt 都不许改", async () => {
  // ⚠ 这条是这一组里最要紧的。走 put 的话 rev 会被推高、updatedAt 换成现在，
  //   于是下一轮同步把它当成"本机刚改的"又推回去 —— 两台设备来回顶。
  const port = makePort();
  const db = store(port);
  const report = await db.applyChanges([
    incoming("n", { id: "a", rev: 7, updatedAt: 1234, value: { id: "a", t: "远端" } })
  ]);
  assert.deepEqual(report.conflicts, []);
  assert.equal(report.applied.length, 1);
  const got = await db.get("n", "a");
  assert.equal(got.rev, 7, "rev 被改过了");
  assert.equal(got.updatedAt, 1234, "updatedAt 被改过了");
  assert.equal(got.value.t, "远端");
});

test("默认不写 journal —— 否则就是一个同步回环", async () => {
  // ⚠ journal 是"待发出"的队列。入站记录进了 journal，下一轮就原样推回去，
  //   A 推给 B、B 再推回 A，永远停不下来。
  const port = makePort();
  const db = store(port);
  await db.applyChanges([incoming("n", { id: "a", rev: 3 })]);
  const page = await db.changes({ after: 0 });
  assert.deepEqual(page.changes, [], "入站记录进了 journal");
  assert.equal(page.cursor, 0, "入站记录动了游标");
});

test("journal:true 才入队（导入历史用），游标跟着走", async () => {
  const port = makePort();
  const db = store(port);
  const report = await db.applyChanges([incoming("n", { id: "a", rev: 3 })], { journal: true });
  const page = await db.changes({ after: 0 });
  assert.equal(page.changes.length, 1);
  assert.equal(page.changes[0].operation, "put");
  assert.equal(page.changes[0].imported, true);
  assert.equal(report.applied[0].cursor, 1, "入队了却没报游标");
});

test("本地更新 → 入站的旧版本被判冲突，不许覆盖", async () => {
  // ⚠ 覆盖的表现是"我刚写的东西被一次同步吞了"，而且没有任何提示。
  const port = makePort();
  const db = store(port);
  await db.put("n", { id: "a", t: "本地" });   // rev 1
  await db.put("n", { id: "a", t: "本地2" });  // rev 2
  const report = await db.applyChanges([
    incoming("n", { id: "a", rev: 1, value: { id: "a", t: "远端" } })
  ]);
  assert.equal(report.applied.length, 0);
  assert.equal(report.conflicts.length, 1);
  assert.equal(report.conflicts[0].reason, "stale-incoming");
  assert.equal((await db.get("n", "a")).value.t, "本地2", "本地版本被覆盖了");
});

test("同 rev 不同内容 → 报 same-rev-different-value，交给上层裁决", async () => {
  const port = makePort();
  const db = store(port);
  await db.put("n", { id: "a", t: "本地" });
  const report = await db.applyChanges([
    incoming("n", { id: "a", rev: 1, value: { id: "a", t: "远端" } })
  ]);
  assert.equal(report.conflicts[0].reason, "same-rev-different-value");
  assert.equal(report.conflicts[0].currentRev, 1);
  assert.equal(report.conflicts[0].incomingRev, 1);
});

test("内容一样且不更新 → 算 skipped，不白写一遍", async () => {
  const port = makePort();
  const db = store(port);
  await db.put("n", { id: "a", t: "同" });
  const before = port.commits;
  const report = await db.applyChanges([
    incoming("n", { id: "a", rev: 1, value: { id: "a", t: "同" } })
  ]);
  assert.equal(report.applied.length, 0);
  assert.deepEqual(report.conflicts, []);
  assert.equal(report.skipped.length, 1);
  assert.equal(port.commits, before, "没东西可写却还是过了一次桥");
});

test("同一个 mutationId 重放 → 直接 skip", async () => {
  const port = makePort();
  const db = store(port);
  await db.applyChanges([incoming("n", { id: "a", rev: 2 }, { mutationId: "r1" })]);
  const report = await db.applyChanges([
    incoming("n", { id: "a", rev: 5, value: { id: "a", t: "又来" } }, { mutationId: "r1" })
  ]);
  assert.deepEqual(report.skipped, ["r1"]);
  assert.equal((await db.get("n", "a")).rev, 2, "重放被当成新变更写进去了");
});

test("墓碑优先：tombstoneDominates 下不许把删掉的复活", async () => {
  // ⚠ 没有这条的表现最诡异：删掉的划线过一会儿自己回来了。
  const port = makePort();
  const db = store(port);
  await db.put("n", { id: "a" });
  await db.remove("n", "a");
  const report = await db.applyChanges([
    incoming("n", { id: "a", rev: 9, value: { id: "a", t: "复活" } })
  ], { tombstoneDominates: true });
  assert.equal(report.conflicts[0].reason, "tombstone-dominates");
  assert.equal(await db.get("n", "a"), null, "墓碑被复活了");
});

test("墓碑也能原样入站（远端删了，本机跟着删）", async () => {
  const port = makePort();
  const db = store(port);
  await db.put("n", { id: "a" });
  await db.applyChanges([
    incoming("n", { id: "a", rev: 4, deleted: true })
  ]);
  assert.equal(await db.get("n", "a"), null);
  assert.equal((await db.get("n", "a", { includeDeleted: true })).rev, 4);
});

test("同一批里两条改同一条 id：后一条要看见前一条", async () => {
  // ⚠ IndexedDB 那边靠"一个事务里顺序执行"天然拿到这个。这边是 readMany 一次
  //   读全批，少了 overlay 的话后一条拿着过时的 current 去判，表现是同一批里的
  //   第二次修改被当成冲突丢掉 —— 一次拉取只落了一半。
  const port = makePort();
  const db = store(port);
  const report = await db.applyChanges([
    incoming("n", { id: "a", rev: 1, updatedAt: 100, value: { id: "a", t: "一" } }),
    incoming("n", { id: "a", rev: 2, updatedAt: 200, value: { id: "a", t: "二" } })
  ]);
  assert.deepEqual(report.conflicts, [], "第二条被误判成冲突");
  assert.equal(report.applied.length, 2);
  assert.equal((await db.get("n", "a")).value.t, "二");
});

test("整批一次提交 —— 入站也不许半批落地", async () => {
  const port = makePort();
  const db = store(port);
  const before = port.commits;
  await db.applyChanges([
    incoming("n", { id: "a", rev: 1 }),
    incoming("n", { id: "b", rev: 1 }),
    incoming("m", { id: "c", rev: 1 })
  ]);
  assert.equal(port.commits - before, 1, "拆成了多次提交");
});

test("subscribe 收到入站通知，且标着 remote", async () => {
  // ⚠ remote 标记是上层区分"别人改的"和"我改的"的唯一依据；丢了它，
  //   界面会把同步拉回来的改动当成本机操作去回放。
  const port = makePort();
  const db = store(port);
  const seen = [];
  db.subscribe({ collection: "n" }, (change) => seen.push(change));
  await db.applyChanges([incoming("n", { id: "a", rev: 6 }, { cursor: 42 })]);
  assert.equal(seen.length, 1);
  assert.equal(seen[0].remote, true);
  assert.equal(seen[0].operation, "put");
  assert.equal(seen[0].cursor, 42, "不入队时该带上来的那个远端游标");
});

test("因果集合：没有证明的入站记录进不来", async () => {
  // causal 集合（卡片这类）要求记录自带父版本证明，否则无从判断它是不是
  // 接在本机这一版后面 —— 放进来就等于允许凭空分叉。
  const port = makePort();
  const db = store(port, { causalCollections: ["card-entities"] });
  const report = await db.applyChanges([
    incoming("card-entities", { id: "a", rev: 1 })
  ]);
  assert.equal(report.applied.length, 0);
  assert.equal(report.conflicts.length, 1);
  assert.ok(report.conflicts[0].reason, "拒绝了却没说原因");
});

test("因果集合：snapshotBaseline 下允许把空库铺满", async () => {
  const port = makePort();
  const db = store(port, { causalCollections: ["card-entities"] });
  const report = await db.applyChanges([
    incoming("card-entities", { id: "a", rev: 3 })
  ], { snapshotBaseline: true });
  assert.deepEqual(report.conflicts, []);
  assert.equal((await db.get("card-entities", "a")).rev, 3);
});

test("空输入不过桥", async () => {
  const port = makePort();
  const db = store(port);
  const before = port.commits + port.reads;
  const report = await db.applyChanges([]);
  assert.deepEqual(report, { applied: [], conflicts: [], skipped: [] });
  assert.equal(port.commits + port.reads, before);
});

test("close 之后不接受入站写入", async () => {
  const port = makePort();
  const db = store(port);
  db.close();
  await assert.rejects(() => db.applyChanges([incoming("n", { id: "a", rev: 1 })]),
                       (error) => error.code === "BW_DATA_CLOSED");
});
