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
              now: timestamp()
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

    function close() {
      closed = true;
      listeners.splice(0, listeners.length);
      if (typeof port.close === 'function') { try { port.close(); } catch (_) {} }
    }

    return {
      contract: CONTRACT,
      get: get,
      getMany: getMany,
      list: list,
      put: put,
      remove: remove,
      batch: batch,
      changes: changes,
      subscribe: subscribe,
      instanceEpoch: instanceEpoch,
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
