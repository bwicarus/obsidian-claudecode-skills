/* native-store.js — 数据落在 App 自己沙盒里的那一版 store。
 *
 * 与 `indexeddb-store.js` **对外同形**（同一组方法、同样的返回形状），区别只在
 * 底下：那边用 IndexedDB 的事务，这边把一次写入整体交给原生侧的 SQLite。
 *
 * ⚠ **判据一律复用 `data-store.js`**：记录该长什么样（revision 校验、墓碑、
 *   causal 证明、批次规划）全从那里取。照抄一份到这里，就等于让「同一次写入该
 *   得到什么记录」有两个答案 —— 而这种分歧只在两边真跑过同一条数据时才暴露，
 *   那时已经写进库里了。
 *
 * ## 事务为什么长这样
 *
 * IndexedDB 那边一次写入是「读当前 → 算新记录 → 写记录+写 journal+记 mutation」
 * **在同一个事务里**。经桥的话中间有异步往返，事务保不住。所以这里是：
 *   ① 读一次（当前记录 + 有没有重放过）
 *   ② 在本地用 data-store 的判据算出新记录
 *   ③ **一次调用**把「记录 + journal 条目 + mutation 备忘」整体提交
 * 并发用**乐观并发**兜底：提交时带上 `expectedRev`，原生侧在同一个 SQLite 事务
 * 里核对，对不上就 `BW_DATA_CONFLICT`，由这里重来一轮。
 * ⚠ 这不是妥协 —— `ifRev` / `assertExpectedRevision` 本来就在模型里。
 *
 * ## port（原生侧要实现的东西）
 *
 * 只有这几个，全是 Promise：
 *   read(collection, id)                  -> record | null
 *   readMany([{collection,id}])           -> (record|null)[]
 *   listCollection(collection, options)   -> record[]      options: {includeDeleted, limit, offset}
 *   remembered(mutationId)                -> record | null
 *   commit(entries)                       -> { cursors: number[] }   一笔事务提交多条
 *   journal({after, limit})               -> { items, cursor, oldestCursor }
 *   meta(key) / putMeta(key, value)
 *   info()                                -> { cursor, journalSize, collections }
 *
 * 测试用的假 port 见 `tests/reader_contract/native-store.contract.test.mjs`。
 */
(function (root, factory) {
  var dataStore = (typeof module === 'object' && module.exports)
    ? require('./data-store.js')
    : (root.BWReaderRuntime && root.BWReaderRuntime.dataStore);
  var api = factory(dataStore);
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.BWReaderRuntime = root.BWReaderRuntime || {};
  root.BWReaderRuntime.nativeStore = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function (D) {
  'use strict';

  var CONTRACT = 'data-store/1';

  if (!D || typeof D.makePutRecord !== 'function') {
    // 拿不到判据层就**不要**退化成自己算一份 —— 那正是这个文件开头警告的事。
    throw new Error('native-store 需要 data-store.js 提供判据');
  }

  function fail(message, code, details) {
    var error = new Error(message);
    error.name = 'DataStoreError';
    error.code = code || 'BW_DATA_INVALID';
    if (details) error.details = details;
    return error;
  }

  /** 冲突是正常分支，不是异常 —— 调用方据 code 决定重试还是上报。 */
  function isConflict(error) {
    return !!error && (error.code === 'BW_DATA_CONFLICT' || error.name === 'ConflictError');
  }

  function createNativeDataStore(options) {
    options = options || {};
    var port = options.port;
    if (!port || typeof port.commit !== 'function' || typeof port.read !== 'function') {
      throw fail('native-store 需要一个 port', 'BW_DATA_BACKEND');
    }
    var deviceId = D.safeName(options.deviceId || 'native-device', 'deviceId');
    var clock = typeof options.clock === 'function' ? options.clock : function () { return Date.now(); };
    var idFactory = typeof options.idFactory === 'function' ? options.idFactory : D.defaultIdFactory;
    var causalCollections = new Set(
      Array.isArray(options.causalCollections) ? options.causalCollections : []);
    // ⚠ 重试上限不是"多试几次碰运气"：每一轮都重新读了当前记录、重新算了记录，
    //   所以失败只可能是**真的有人在并发写**。给个上限免得两个写者互相顶着转圈。
    var maxRetries = Math.max(1, Number(options.maxRetries) || 5);
    var listeners = [];
    var closed = false;

    function timestamp() { return D.normalizeTimestamp(clock()); }

    function notify(changes) {
      if (!changes || !changes.length) return;
      listeners.slice().forEach(function (entry) {
        changes.forEach(function (change) {
          if (entry.collection && entry.collection !== change.collection) return;
          try { entry.listener(change); } catch (_) {}
        });
      });
    }

    function assertOpen() {
      if (closed) throw fail('store 已关闭', 'BW_DATA_CLOSED');
    }

    // ── 读 ──

    function get(collection, id, queryOptions) {
      try {
        assertOpen();
        collection = D.safeName(collection, 'collection');
        id = D.safeName(id, 'id');
      } catch (error) { return Promise.reject(error); }
      queryOptions = queryOptions || {};
      return Promise.resolve(port.read(collection, id)).then(function (record) {
        if (record && record.deleted && !queryOptions.includeDeleted) return null;
        return record ? D.cloneJSON(record, 'record') : null;
      });
    }

    function getMany(requests, queryOptions) {
      if (!Array.isArray(requests) || requests.length > 64) {
        return Promise.reject(fail('批量读取请求无效', 'BW_DATA_INVALID'));
      }
      var normalized;
      try {
        assertOpen();
        normalized = requests.map(function (request, index) {
          if (!request || typeof request !== 'object' || Array.isArray(request)) {
            throw fail('批量读取项无效', 'BW_DATA_INVALID', { index: index });
          }
          return {
            collection: D.safeName(request.collection, 'collection'),
            id: D.safeName(request.id, 'id'),
            includeDeleted: !!request.includeDeleted
          };
        });
      } catch (error) { return Promise.reject(error); }
      if (!normalized.length) return Promise.resolve([]);
      // ⚠ 一次过桥，**不要**在这里循环 get()：一本书的一屏可能要几十条，
      //   逐条往返会把桥变成瓶颈（而且快照还不一致）。
      return Promise.resolve(port.readMany(normalized.map(function (request) {
        return { collection: request.collection, id: request.id };
      }))).then(function (records) {
        return normalized.map(function (request, index) {
          var record = records && records[index];
          if (record && record.deleted && !request.includeDeleted) return null;
          return record ? D.cloneJSON(record, 'record') : null;
        });
      });
    }

    function list(collection, query) {
      try {
        assertOpen();
        collection = D.safeName(collection, 'collection');
      } catch (error) { return Promise.reject(error); }
      query = query || {};
      var offset = Math.max(0, Number(query.offset) || 0);
      var limit = Math.max(1, Math.min(1000, Number(query.limit) || 200));
      // 只有默认语义才把过滤/排序/分页下推给原生（SQL 里 ORDER BY updatedAt,id
      // 与 IndexedDB 那个复合索引同序）。带 documentId / afterId / orderBy:id 的
      // 查询要看记录内部，下推不了 —— 那时整集合取回来在这里过，与 IndexedDB
      // 的做法完全一致。
      var simple = query.documentId == null && query.afterId == null && query.orderBy !== 'id';
      var request = simple
        ? { includeDeleted: !!query.includeDeleted, limit: limit, offset: offset }
        : { includeDeleted: true };
      return Promise.resolve(port.listCollection(collection, request)).then(function (records) {
        records = (records || []).map(function (record) { return D.cloneJSON(record, 'record'); });
        if (simple) return records;
        return records.filter(function (record) {
          if (!query.includeDeleted && record.deleted) return false;
          if (query.documentId != null &&
              String(record.value && record.value.documentId || '') !== String(query.documentId)) return false;
          if (query.orderBy === 'id' && query.afterId != null &&
              String(record.id) <= String(query.afterId)) return false;
          return true;
        }).sort(function (a, b) {
          if (query.orderBy === 'id') return a.id < b.id ? -1 : (a.id > b.id ? 1 : 0);
          return (a.updatedAt || 0) - (b.updatedAt || 0) || (a.id < b.id ? -1 : 1);
        }).slice(offset, offset + limit);
      });
    }

    // ── 写 ──

    /** 把一条写入算成「提交条目」。不碰存储，只用判据。 */
    function planMutation(mutation, current) {
      var options2 = mutation.options || {};
      var collection = mutation.collection;
      var metadata = {
        updatedAt: timestamp(), updatedBy: deviceId,
        causal: causalCollections.has(collection)
      };
      var record;
      var id;
      if (mutation.operation === 'remove') {
        id = mutation.id;
        record = D.makeRemoveRecord(collection, id, current, options2, metadata);
      } else {
        id = D.stableId(mutation.value, options2, idFactory);
        record = D.makePutRecord(collection, id, current, mutation.value, options2, metadata);
      }
      var mutationId = D.createMutationId(options2, deviceId, metadata.updatedAt, collection, id);
      return {
        collection: collection,
        id: id,
        record: record,
        mutationId: mutationId,
        operation: mutation.operation === 'remove' ? 'remove' : 'put',
        // ⚠ 乐观并发的凭据：我们刚刚读到的那一版。原生侧在同一个事务里核对。
        expectedRev: D.actualRevision(current)
      };
    }

    /** 读当前状态 → 算 → 提交；冲突就重来（每轮都重新读、重新算）。 */
    function writeAll(mutations) {
      var attempt = 0;
      function once() {
        attempt += 1;
        var keys = mutations.map(function (mutation) {
          return {
            collection: mutation.collection,
            id: mutation.operation === 'remove'
              ? mutation.id
              : D.stableId(mutation.value, mutation.options || {}, idFactory)
          };
        });
        return Promise.all([
          Promise.resolve(port.readMany(keys)),
          Promise.all(mutations.map(function (mutation) {
            var mutationId = (mutation.options || {}).mutationId;
            return mutationId ? Promise.resolve(port.remembered(mutationId)) : Promise.resolve(null);
          }))
        ]).then(function (parts) {
          var currents = parts[0] || [];
          var replays = parts[1] || [];
          var entries = [];
          var results = [];
          mutations.forEach(function (mutation, index) {
            if (replays[index]) {
              // 重放去重：同一个 mutationId 再来一次，原样返回上次的结果，
              // **不再写一遍**（否则离线队列重发会把 rev 一路推高）。
              results.push({ replay: true, record: D.cloneJSON(replays[index], 'record') });
              return;
            }
            var entry = planMutation(mutation, currents[index] || null);
            entries.push(entry);
            results.push({ replay: false, entry: entry });
          });
          if (!entries.length) {
            return { values: results.map(function (item) { return item.record; }), changes: [] };
          }
          return Promise.resolve(port.commit(entries.map(function (entry) {
            return {
              collection: entry.collection, id: entry.id,
              record: entry.record, mutationId: entry.mutationId,
              operation: entry.operation, expectedRev: entry.expectedRev,
              now: timestamp(),
              // journal 存的是**完整的变更信封** —— 只存记录本体的话，
              // changes() 拿不到 operation/mutationId，增量同步就无从判断这一条
              // 是写还是删、是不是自己刚发出去的。
              // ⚠ 这里**不带 cursor**：它要到提交那一刻才分配，由原生侧填。
              change: {
                mutationId: entry.mutationId, operation: entry.operation,
                collection: entry.collection,
                record: D.cloneJSON(entry.record, 'change.record')
              }
            };
          }))).then(function (receipt) {
            var cursors = (receipt && receipt.cursors) || [];
            var changes = [];
            var cursorIndex = 0;
            var values = results.map(function (item) {
              if (item.replay) return item.record;
              var cursor = cursors[cursorIndex++];
              changes.push({
                cursor: cursor, mutationId: item.entry.mutationId,
                operation: item.entry.operation, collection: item.entry.collection,
                record: D.cloneJSON(item.entry.record, 'change.record')
              });
              return D.cloneJSON(item.entry.record, 'record');
            });
            return { values: values, changes: changes };
          });
        }).catch(function (error) {
          if (isConflict(error) && attempt < maxRetries) return once();
          throw error;
        });
      }
      return once();
    }

    function put(collection, value, operationOptions) {
      var mutation;
      try {
        assertOpen();
        mutation = {
          operation: 'put',
          collection: D.safeName(collection, 'collection'),
          value: D.normalizePutValue(value, 'put.value'),
          options: D.normalizeOperationOptions(operationOptions)
        };
      } catch (error) { return Promise.reject(error); }
      return writeAll([mutation]).then(function (outcome) {
        notify(outcome.changes);
        return outcome.values[0];
      });
    }

    function remove(collection, id, operationOptions) {
      var mutation;
      try {
        assertOpen();
        mutation = {
          operation: 'remove',
          collection: D.safeName(collection, 'collection'),
          id: D.safeName(id, 'id'),
          options: D.normalizeOperationOptions(operationOptions)
        };
      } catch (error) { return Promise.reject(error); }
      return writeAll([mutation]).then(function (outcome) {
        notify(outcome.changes);
        return outcome.values[0];
      });
    }

    function batch(mutations) {
      var prepared;
      try {
        assertOpen();
        prepared = D.prepareBatch(mutations);
      } catch (error) { return Promise.reject(error); }
      if (!prepared.length) return Promise.resolve([]);
      // ⚠ 整批一次提交：拆成多次的话中途失败会留下半批，而这正是 batch 存在的理由。
      return writeAll(prepared).then(function (outcome) {
        notify(outcome.changes);
        return outcome.values;
      });
    }

    // ── 入站（同步把别处的记录写进来）──

    /** 同步拉回来的记录**原样**落库：rev / updatedAt / 墓碑都按来的样子写。
     *
     * ⚠ 这里绝不能走 `put`。`put` 会把 rev 推高、updatedAt 换成现在 —— 那条记录
     *   就变成了"本机刚改的"，于是下一轮同步把它当成本地新改动又推回去，两台设备
     *   来回顶。入站与本地写是两条路，区别就在这。
     *
     * ⚠ 默认**不写 journal**（`journal !== true`）。journal 是发出去的队列；把
     *   入站记录塞进去就是一个同步回环：A 推给 B，B 原样再推回 A。
     *   `journal: true` 只给"导入一份历史"这种场合用，与 IndexedDB 那边同名同义。
     *
     * ⚠ 判据（sameDataValue / 因果证明 / 墓碑优先）全部取自 `data-store.js`，
     *   与 IndexedDB 版共用同一套。这个函数只负责「读什么、写什么、按什么顺序」。
     */
    function applyChanges(incoming, applyOptions) {
      var normalized;
      try {
        assertOpen();
        incoming = Array.isArray(incoming) ? D.cloneJSON(incoming, 'incoming') : [];
        normalized = incoming.map(D.normalizeIncomingChange);
      } catch (error) { return Promise.reject(error); }
      applyOptions = applyOptions || {};
      var journalImported = applyOptions.journal === true;
      var tombstoneDominates = applyOptions.tombstoneDominates === true;
      var snapshotBaseline = applyOptions.snapshotBaseline === true;
      if (!normalized.length) {
        return Promise.resolve({ applied: [], conflicts: [], skipped: [] });
      }

      var keyOf = function (collection, id) { return collection + ' ' + id; };
      return Promise.all([
        Promise.resolve(port.readMany(normalized.map(function (item) {
          return { collection: item.collection, id: item.record.id };
        }))),
        Promise.all(normalized.map(function (item) {
          return item.mutationId
            ? Promise.resolve(port.remembered(item.mutationId))
            : Promise.resolve(null);
        }))
      ]).then(function (parts) {
        var stored = parts[0] || [];
        var replays = parts[1] || [];
        // ⚠ 同一批里两条改同一个 id 时，后一条必须看见前一条的结果。
        //   IndexedDB 那边靠"一个事务里顺序执行"天然拿到，这边要自己叠一层
        //   overlay —— 少了它，后一条会拿着过时的 current 去判，表现是
        //   同一批里的第二次修改被当成冲突丢掉。
        var overlay = {};
        var applied = [];
        var conflicts = [];
        var skipped = [];
        var entries = [];
        var notifications = [];

        normalized.forEach(function (item, index) {
          var collection = item.collection;
          var clean = item.record;
          var mutationId = item.mutationId;
          if (replays[index]) { skipped.push(mutationId); return; }

          var key = keyOf(collection, clean.id);
          var current = Object.prototype.hasOwnProperty.call(overlay, key)
            ? overlay[key]
            : (stored[index] || null);
          var incomingRev = clean.rev;
          var currentRev = Number((current && current.rev) || 0);
          var sameBusiness = !!current && D.sameDataValue(current, clean);
          var causalRequired = causalCollections.has(collection);
          var proof = causalRequired ? D.inspectCausalProof(clean) : null;
          var linearTombstoneChild = causalRequired && proof.valid &&
            D.causalParentMatches(current, proof);

          function reject(reason, withRevs) {
            var conflict = {
              mutationId: mutationId, collection: collection, id: clean.id,
              local: current ? D.cloneJSON(current, 'local') : null,
              incoming: D.cloneJSON(clean, 'incoming'), reason: reason
            };
            if (withRevs) {
              conflict.incomingRev = incomingRev;
              conflict.currentRev = currentRev;
            }
            conflicts.push(conflict);
          }

          if (sameBusiness && incomingRev <= currentRev) {
            // 已经是这个内容了：把 mutationId 记下来（让重发认得出来）就算完。
            // ⚠ 备忘里存的是**库里那一版**（current），不是来的那一版。桥把
            //   "写记录"和"记备忘"用同一个 json，存 clean 就会把更新的 current
            //   覆盖成旧的 —— 而重放时返回库里真有的那版本来也更诚实。
            skipped.push(mutationId || (collection + '/' + clean.id));
            if (mutationId) {
              entries.push({ collection: collection, id: clean.id, record: current,
                             mutationId: mutationId, expectedRev: currentRev,
                             journal: false, change: null });
            }
            return;
          }
          if (tombstoneDominates && current && current.deleted === true &&
              clean.deleted !== true && !linearTombstoneChild) {
            reject('tombstone-dominates', false);
            return;
          }
          var causalAccepted = causalRequired && (
            (snapshotBaseline && current === null) ||
            (proof.valid && D.causalParentMatches(current, proof)));
          if (!sameBusiness && causalRequired && !causalAccepted) {
            reject(proof.valid ? 'causal-parent-mismatch' : proof.reason, true);
            return;
          }
          if (!sameBusiness && !causalRequired && current && incomingRev <= currentRev) {
            reject(incomingRev === currentRev ? 'same-rev-different-value' : 'stale-incoming',
                   true);
            return;
          }
          if (causalRequired && !sameBusiness && currentRev >= Number.MAX_SAFE_INTEGER) {
            reject('causal-revision-overflow', true);
            return;
          }

          var accepted = D.cloneJSON(clean, 'record');
          if (causalRequired && !sameBusiness) {
            accepted.rev = Math.max(incomingRev, currentRev + 1);
          }
          var operation = accepted.deleted ? 'remove' : 'put';
          overlay[key] = accepted;
          entries.push({
            collection: collection, id: accepted.id, record: accepted,
            mutationId: mutationId, expectedRev: currentRev, journal: journalImported,
            change: journalImported
              ? { mutationId: mutationId, operation: operation, collection: collection,
                  record: D.cloneJSON(accepted, 'change.record'),
                  imported: true, remote: false }
              : null
          });
          applied.push({ collection: collection, id: accepted.id, rev: accepted.rev,
                         cursor: null, entryIndex: entries.length - 1 });
          notifications.push({
            // 不写 journal 时没有本机游标可报，就带上来的那个（远端游标）。
            cursor: journalImported ? 0 : (Number(item.change && item.change.cursor) || 0),
            mutationId: mutationId, operation: operation, collection: collection,
            record: D.cloneJSON(accepted, 'change.record'), remote: !journalImported
          });
        });

        if (!entries.length) {
          return { applied: applied, conflicts: conflicts, skipped: skipped };
        }
        return Promise.resolve(port.commit(entries.map(function (entry) {
          return {
            collection: entry.collection, id: entry.id, record: entry.record,
            mutationId: entry.mutationId, expectedRev: entry.expectedRev,
            now: timestamp(),
            // journal:false → 原生侧只写记录和 mutation 备忘，不动 journal/游标。
            journal: entry.journal, change: entry.change
          };
        }))).then(function (receipt) {
          var cursors = (receipt && receipt.cursors) || [];
          applied.forEach(function (item, index) {
            var cursor = Number(cursors[item.entryIndex]);
            item.cursor = journalImported && cursor > 0 ? cursor : null;
            delete item.entryIndex;
            if (item.cursor) notifications[index].cursor = item.cursor;
          });
          notify(notifications);
          return { applied: applied, conflicts: conflicts, skipped: skipped };
        });
      });
    }

    // ⚠ **故意没有 `migrateLegacyCausal`。** 它只在 checkpoint 带着
    //   `__legacyCausalMigration` 标记时被调用，而那个标记是 sync-v2→v3 升级
    //   一份**既有** checkpoint 时打的；新建的原生库 instanceEpoch 是新的，
    //   旧 checkpoint 在 decode 阶段就被判为不存在，标记到不了这里。
    //   真走到了，sync-coordinator 会抛 `BW_SYNC_CAUSAL_MIGRATION_UNAVAILABLE`
    //   —— 一个说得清楚的硬错误，比在这条数据路径上放一段谁也没跑过的迁移好。

    // ── journal ──

    function changes(query) {
      try { assertOpen(); } catch (error) { return Promise.reject(error); }
      query = query || {};
      var after = Math.max(0, Number(query.after) || 0);
      var limit = Math.max(1, Math.min(2000, Number(query.limit) || 500));
      return Promise.resolve(port.journal({ after: after, limit: limit })).then(function (page) {
        var items = ((page && page.items) || []).map(function (item) {
          return D.cloneJSON(item, 'change');
        });
        var cursor = Math.max(0, Number(page && page.cursor) || 0);
        var oldestCursor = Number(page && page.oldestCursor);
        if (!Number.isFinite(oldestCursor) || oldestCursor <= 0) oldestCursor = cursor + 1;
        // resetRequired：要么要的位置比现在还新（游标错乱），要么它已经被裁掉了。
        // 两种情况都不能假装"没有变更"—— 调用方必须知道自己该重新全量对齐。
        var resetRequired = after > cursor || after < Math.max(0, oldestCursor - 1);
        var nextCursor = items.length
          ? Math.max(after, Number(items[items.length - 1].cursor) || after)
          : after;
        return {
          contract: CONTRACT,
          cursor: cursor,
          nextCursor: nextCursor,
          oldestCursor: oldestCursor,
          resetRequired: resetRequired,
          hasMore: !resetRequired && nextCursor < cursor,
          changes: items
        };
      });
    }

    function subscribe(query, listener) {
      if (typeof query === 'function') { listener = query; query = {}; }
      if (typeof listener !== 'function') {
        throw fail('subscribe listener 必须是函数', 'BW_DATA_INVALID');
      }
      var entry = {
        collection: query && query.collection ? String(query.collection) : '',
        listener: listener
      };
      listeners.push(entry);
      return function () {
        var index = listeners.indexOf(entry);
        if (index >= 0) listeners.splice(index, 1);
      };
    }

    function status() {
      return Promise.resolve(port.info()).then(function (info) {
        return {
          contract: CONTRACT,
          backend: 'native-sqlite',
          deviceId: deviceId,
          cursor: Math.max(0, Number(info && info.cursor) || 0),
          journalSize: Math.max(0, Number(info && info.journalSize) || 0),
          collections: ((info && info.collections) || []).slice().sort()
        };
      });
    }

    function instanceEpoch() {
      // epoch 的作用是「库被删了重建之后，别继承一个会跳过历史的游标」。
      // 原生这边库在 App 沙盒里、不会被浏览器清掉，但**重装 App 会**，
      // 所以这条照留。
      return Promise.resolve(port.meta('instanceEpoch')).then(function (existing) {
        if (existing) return existing;
        var epoch = 'e' + timestamp().toString(36) + '-' +
          Math.random().toString(36).slice(2, 10);
        return Promise.resolve(port.putMeta('instanceEpoch', epoch)).then(function () {
          return epoch;
        });
      });
    }

    // 启动期记事本：迁移进度这类「关于这个库本身」的小标记。
    //
    // ⚠ 业务数据一律走 collection，**不要**往这里塞。它没有 rev、没有墓碑、
    //   不进 journal、不参与同步 —— 拿它存业务数据的表现是"这台设备上有、
    //   别的设备上永远没有"。
    // ⚠ 保留键挡掉：`instanceEpoch` 和 `cursor` 是库的身份与发号位置，被外面
    //   覆盖一次就等于让同步跳过一段历史（epoch）或重发一遍（cursor）。
    var RESERVED_META = { instanceEpoch: true, cursor: true };

    function checkedMetaKey(key) {
      var name = String(key || '');
      if (!name) throw fail('meta 键不能为空', 'BW_DATA_INVALID');
      if (RESERVED_META[name]) {
        throw fail('meta 键 ' + name + ' 由存储自己维护，不能从外面写', 'BW_DATA_INVALID');
      }
      return name;
    }

    function meta(key) {
      try { assertOpen(); return Promise.resolve(port.meta(checkedMetaKey(key))); }
      catch (error) { return Promise.reject(error); }
    }

    function putMeta(key, value) {
      try {
        assertOpen();
        return Promise.resolve(port.putMeta(checkedMetaKey(key), String(value)))
          .then(function () { return true; });
      } catch (error) { return Promise.reject(error); }
    }

    function close() {
      closed = true;
      listeners.splice(0, listeners.length);
      if (typeof port.close === 'function') { try { port.close(); } catch (_) {} }
    }

    return {
      contract: CONTRACT,
      cardRepositoryCall: typeof port.cardRepositoryCall === 'function' ? function (operation, args) {
        try { assertOpen(); } catch (error) { return Promise.reject(error); }
        return Promise.resolve(port.cardRepositoryCall(operation, args, deviceId)).then(function (reply) {
          notify(reply.changes || []);
          return reply.result;
        });
      } : undefined,
      preferenceCall: typeof port.preferenceCall === 'function' ? function (input) {
        try { assertOpen(); } catch (error) { return Promise.reject(error); }
        return Promise.resolve(port.preferenceCall(input, deviceId)).then(function (reply) {
          assertOpen();
          notify(reply.changes || []);
          return reply.result;
        });
      } : undefined,
      observeCommitted: function (changes) {
        assertOpen();
        // Native UI transactions already wrote records and journal. This only
        // wakes existing observers/sync; it must never call port.commit again.
        notify(changes);
      },
      get: get,
      getMany: getMany,
      list: list,
      put: put,
      remove: remove,
      batch: batch,
      changes: changes,
      applyChanges: applyChanges,
      subscribe: subscribe,
      instanceEpoch: instanceEpoch,
      meta: meta,
      putMeta: putMeta,
      status: status,
      close: close
    };
  }

  return {
    CONTRACT: CONTRACT,
    createNativeDataStore: createNativeDataStore,
    isConflict: isConflict
  };
});
