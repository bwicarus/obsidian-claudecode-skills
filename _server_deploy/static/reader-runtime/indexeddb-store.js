/* indexeddb-store.js — data-store/1 的正式 PWA IndexedDB 实现。
 *
 * 与内存兼容实现不同，这里不会把全部数据序列化为一个 blob。每一次写入都在
 * records / journal / mutations / meta 四个 object store 上使用同一个原子
 * readwrite transaction，因而多个标签页和多个 store 实例也能正确竞争 ifRev。
 */
(function (root, factory) {
  var api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.BWReaderRuntime = root.BWReaderRuntime || {};
  root.BWReaderRuntime.indexedDBStore = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function (root) {
  'use strict';

  var dataStoreSpec = root.BWReaderRuntime && root.BWReaderRuntime.dataStore;
  if (!dataStoreSpec || dataStoreSpec.CONTRACT !== 'data-store/1') {
    throw new Error('indexeddb-store.js 必须在 data-store.js 之后加载');
  }
  var CONTRACT = dataStoreSpec.CONTRACT;
  var SCHEMA = dataStoreSpec.SCHEMA;
  var DB_VERSION = 1;
  var STORE_RECORDS = 'records';
  var STORE_JOURNAL = 'journal';
  var STORE_MUTATIONS = 'mutations';
  var STORE_META = 'meta';
  var ALL_STORES = [STORE_RECORDS, STORE_JOURNAL, STORE_MUTATIONS, STORE_META];
  var INSTANCE_EPOCH_CONTRACT = 'data-store-instance-v1';

  var DataStoreError = dataStoreSpec.DataStoreError;
  var ConflictError = dataStoreSpec.ConflictError;
  var safeName = dataStoreSpec.safeName;
  var isPlainObject = dataStoreSpec.isPlainObject;
  var assertJSON = dataStoreSpec.assertDataValue;
  var cloneJSON = dataStoreSpec.cloneDataValue;
  var stableId = dataStoreSpec.stableId;
  var normalizeOperationOptions = dataStoreSpec.normalizeOperationOptions;
  var normalizePutValue = dataStoreSpec.normalizePutValue;
  var normalizeIncomingChange = dataStoreSpec.normalizeIncomingChange;
  var prepareBatch = dataStoreSpec.prepareBatch;
  var same = dataStoreSpec.sameDataValue;
  var inspectCausalProof = dataStoreSpec.inspectCausalProof;
  var causalParentMatches = dataStoreSpec.causalParentMatches;
  var sameRecordWithoutCausal = dataStoreSpec.sameRecordWithoutCausal;
  var planLegacyCausalMigration = dataStoreSpec.planLegacyCausalMigration;
  var CAUSAL_MIGRATION_CONTRACT = dataStoreSpec.CAUSAL_MIGRATION_CONTRACT;
  var defaultIdFactory = dataStoreSpec.defaultIdFactory;
  var normalizeTimestamp = dataStoreSpec.normalizeTimestamp;
  var makePutRecord = dataStoreSpec.makePutRecord;
  var makeRemoveRecord = dataStoreSpec.makeRemoveRecord;
  var createMutationId = dataStoreSpec.createMutationId;

  function requestResult(request) {
    return new Promise(function (resolve, reject) {
      request.onsuccess = function () { resolve(request.result); };
      request.onerror = function () {
        reject(request.error || new DataStoreError('IndexedDB 请求失败', 'BW_DATA_BACKEND'));
      };
    });
  }

  function backendError(error, message) {
    if (error && error.code && String(error.code).indexOf('BW_') === 0) return error;
    return new DataStoreError(message || 'IndexedDB 操作失败', 'BW_DATA_BACKEND', {
      cause: String(error && error.message || error || 'unknown')
    });
  }

  function physicalKey(collection, id) {
    return collection + '\u0000' + id;
  }

  function toStoredRecord(record) {
    var output = cloneJSON(record, 'record');
    output.pk = physicalKey(output.collection, output.id);
    return output;
  }

  function fromStoredRecord(record) {
    if (!record) return null;
    var output = cloneJSON(record, 'record');
    delete output.pk;
    return output;
  }

  function openCursorValues(source, range, limit) {
    return new Promise(function (resolve, reject) {
      var values = [];
      var request;
      try { request = source.openCursor(range || null); }
      catch (error) { reject(error); return; }
      request.onerror = function () { reject(request.error); };
      request.onsuccess = function () {
        var cursor = request.result;
        if (!cursor || (limit && values.length >= limit)) {
          resolve(values);
          return;
        }
        values.push(cursor.value);
        cursor.continue();
      };
    });
  }

  function trimOldest(store, maximum, indexName) {
    return requestResult(store.count()).then(function (count) {
      var remaining = Math.max(0, count - maximum);
      if (!remaining) return;
      var source = indexName ? store.index(indexName) : store;
      return new Promise(function (resolve, reject) {
        var request = source.openCursor();
        request.onerror = function () { reject(request.error); };
        request.onsuccess = function () {
          var cursor = request.result;
          if (!cursor || remaining <= 0) {
            resolve();
            return;
          }
          var deletion = cursor.delete();
          deletion.onerror = function () { reject(deletion.error); };
          deletion.onsuccess = function () {
            remaining -= 1;
            cursor.continue();
          };
        };
      });
    });
  }

  function createIndexedDBDataStore(options) {
    options = options || {};
    var indexedDBFactory = options.indexedDB || root.indexedDB;
    var KeyRange = options.IDBKeyRange || root.IDBKeyRange;
    if (!indexedDBFactory || typeof indexedDBFactory.open !== 'function') {
      throw new DataStoreError('当前环境不支持 IndexedDB', 'BW_DATA_UNAVAILABLE');
    }
    if (!KeyRange || typeof KeyRange.only !== 'function') {
      throw new DataStoreError('当前环境缺少 IDBKeyRange', 'BW_DATA_UNAVAILABLE');
    }

    var dbName = safeName(options.name || options.dbName || 'bw-reader-data-v1', 'dbName');
    var deviceId = safeName(options.deviceId || 'pwa-device', 'deviceId');
    var clock = typeof options.clock === 'function' ? options.clock : function () { return Date.now(); };
    var idFactory = typeof options.idFactory === 'function' ? options.idFactory : defaultIdFactory;
    var maxJournal = Math.max(100, Number(options.maxJournal) || 10000);
    var maxMutations = Math.max(200, Number(options.maxMutations) || 20000);
    var causalCollections = new Set(
      (Array.isArray(options.causalCollections)
        ? options.causalCollections
        : []
      ).map(function (name) { return safeName(name, 'causalCollection'); })
    );
    var listeners = [];
    var databasePromise = null;
    var database = null;
    var closed = false;
    var instanceId = defaultIdFactory('idb-instance');
    var broadcast = null;

    function newInstanceEpoch() {
      var cryptoApi = root.crypto;
      if (!cryptoApi || typeof cryptoApi.getRandomValues !== 'function') {
        throw new DataStoreError(
          '当前环境无法生成 IndexedDB 实例代次',
          'BW_DATA_INSTANCE_EPOCH'
        );
      }
      var bytes = new Uint8Array(16);
      cryptoApi.getRandomValues(bytes);
      return INSTANCE_EPOCH_CONTRACT + '-' +
        Array.prototype.map.call(bytes, function (byte) {
          return byte.toString(16).padStart(2, '0');
        }).join('');
    }

    function checkedInstanceEpoch(value) {
      value = String(value || '');
      if (
        new RegExp('^' + INSTANCE_EPOCH_CONTRACT + '-[a-f0-9]{32}$')
          .test(value)
      ) return value;
      throw new DataStoreError(
        'IndexedDB 实例代次损坏',
        'BW_DATA_INSTANCE_EPOCH'
      );
    }

    function openDatabase() {
      if (closed) return Promise.reject(new DataStoreError('IndexedDB store 已关闭', 'BW_DATA_CLOSED'));
      if (databasePromise) return databasePromise;
      databasePromise = new Promise(function (resolve, reject) {
        var request;
        try { request = indexedDBFactory.open(dbName, DB_VERSION); }
        catch (error) { reject(backendError(error, '无法打开 IndexedDB')); return; }
        request.onupgradeneeded = function () {
          var db = request.result;
          var transaction = request.transaction;
          var records;
          if (!db.objectStoreNames.contains(STORE_RECORDS)) {
            records = db.createObjectStore(STORE_RECORDS, { keyPath: 'pk' });
            records.createIndex('collection', 'collection', { unique: false });
            records.createIndex('collectionUpdated', ['collection', 'updatedAt', 'id'], { unique: false });
          } else {
            records = transaction.objectStore(STORE_RECORDS);
            if (!records.indexNames.contains('collection')) {
              records.createIndex('collection', 'collection', { unique: false });
            }
            if (!records.indexNames.contains('collectionUpdated')) {
              records.createIndex('collectionUpdated', ['collection', 'updatedAt', 'id'], { unique: false });
            }
          }
          if (!db.objectStoreNames.contains(STORE_JOURNAL)) {
            db.createObjectStore(STORE_JOURNAL, { keyPath: 'cursor' });
          }
          var mutations;
          if (!db.objectStoreNames.contains(STORE_MUTATIONS)) {
            mutations = db.createObjectStore(STORE_MUTATIONS, { keyPath: 'mutationId' });
            mutations.createIndex('rememberedAt', 'rememberedAt', { unique: false });
          } else {
            mutations = transaction.objectStore(STORE_MUTATIONS);
            if (!mutations.indexNames.contains('rememberedAt')) {
              mutations.createIndex('rememberedAt', 'rememberedAt', { unique: false });
            }
          }
          var meta;
          var metaCreated = false;
          if (!db.objectStoreNames.contains(STORE_META)) {
            meta = db.createObjectStore(STORE_META, { keyPath: 'key' });
            metaCreated = true;
          } else {
            meta = transaction.objectStore(STORE_META);
          }
          meta.put({ key: 'schema', value: SCHEMA });
          if (metaCreated) {
            meta.put({ key: 'cursor', value: 0 });
            meta.put({ key: 'instanceEpoch', value: newInstanceEpoch() });
          }
        };
        request.onerror = function () {
          databasePromise = null;
          reject(backendError(request.error, '无法打开 IndexedDB'));
        };
        request.onblocked = function () {
          databasePromise = null;
          reject(new DataStoreError('IndexedDB 升级被其他标签页阻塞', 'BW_DATA_BLOCKED', { dbName: dbName }));
        };
        request.onsuccess = function () {
          var db = request.result;
          if (closed) {
            db.close();
            databasePromise = null;
            reject(new DataStoreError('IndexedDB store 已关闭', 'BW_DATA_CLOSED'));
            return;
          }
          var missing = ALL_STORES.filter(function (name) { return !db.objectStoreNames.contains(name); });
          if (missing.length) {
            db.close();
            databasePromise = null;
            reject(new DataStoreError('IndexedDB 结构不完整', 'BW_DATA_SCHEMA', { missing: missing }));
            return;
          }
          db.onversionchange = function () {
            db.close();
            if (database === db) database = null;
            databasePromise = null;
          };
          database = db;
          resolve(db);
        };
      });
      return databasePromise;
    }

    function transact(storeNames, mode, worker) {
      return openDatabase().then(function (db) {
        return new Promise(function (resolve, reject) {
          var transaction;
          try { transaction = db.transaction(storeNames, mode); }
          catch (error) { reject(backendError(error)); return; }
          var result;
          var workerError = null;
          var workerDone = false;
          transaction.oncomplete = function () {
            if (!workerDone) {
              reject(new DataStoreError('IndexedDB transaction 提前完成', 'BW_DATA_BACKEND'));
              return;
            }
            resolve(result);
          };
          transaction.onerror = function () {};
          transaction.onabort = function () {
            reject(workerError || backendError(transaction.error, 'IndexedDB transaction 已回滚'));
          };
          var work;
          try { work = worker(transaction); }
          catch (error) {
            workerError = error;
            try { transaction.abort(); } catch (_) { reject(error); }
            return;
          }
          Promise.resolve(work).then(function (value) {
            result = value;
            workerDone = true;
          }).catch(function (error) {
            workerError = error;
            try { transaction.abort(); } catch (_) { reject(error); }
          });
        });
      });
    }

    function cursorOf(transaction) {
      return requestResult(transaction.objectStore(STORE_META).get('cursor')).then(function (entry) {
        return Math.max(0, Number(entry && entry.value) || 0);
      });
    }

    /*
     * Checkpoints may live in a different persistence area (PWA device DB or
     * extension chrome.storage).  Bind them to this value so deleting and
     * recreating the records DB cannot inherit a cursor that skips history.
     * A readwrite transaction also upgrades pre-epoch databases atomically;
     * concurrent tabs serialize on STORE_META and therefore observe one
     * stable value.
     */
    function instanceEpoch() {
      return transact([STORE_META], 'readwrite', function (transaction) {
        var meta = transaction.objectStore(STORE_META);
        return requestResult(meta.get('instanceEpoch')).then(function (entry) {
          if (entry && entry.value != null) {
            return checkedInstanceEpoch(entry.value);
          }
          var generated = newInstanceEpoch();
          return requestResult(meta.put({
            key: 'instanceEpoch',
            value: generated
          })).then(function () {
            return generated;
          });
        });
      });
    }

    function mutationIdOf(operationOptions, collection, id) {
      if (operationOptions && operationOptions.mutationId) {
        return String(operationOptions.mutationId);
      }
      return createMutationId(operationOptions, deviceId, timestamp(), collection, id);
    }

    function timestamp() {
      return normalizeTimestamp(clock());
    }

    function getRemembered(transaction, mutationId) {
      if (!mutationId) return Promise.resolve(null);
      return requestResult(transaction.objectStore(STORE_MUTATIONS).get(mutationId));
    }

    function remember(transaction, mutationId, result) {
      if (!mutationId) return Promise.resolve();
      return requestResult(transaction.objectStore(STORE_MUTATIONS).put({
        mutationId: mutationId,
        rememberedAt: timestamp(),
        result: cloneJSON(result, 'mutation.result')
      }));
    }

    function appendJournal(transaction, collection, record, mutationId, operation, metadata) {
      return cursorOf(transaction).then(function (currentCursor) {
        var nextCursor = currentCursor + 1;
        var change = {
          cursor: nextCursor,
          mutationId: mutationId,
          operation: operation,
          collection: collection,
          record: cloneJSON(record, 'change.record')
        };
        Object.keys(metadata || {}).forEach(function (key) {
          change[key] = cloneJSON(metadata[key], 'change.' + key);
        });
        return Promise.all([
          requestResult(transaction.objectStore(STORE_META).put({ key: 'cursor', value: nextCursor })),
          requestResult(transaction.objectStore(STORE_JOURNAL).put(change))
        ]).then(function () { return change; });
      });
    }

    function notifyOne(change, fromBroadcast) {
      listeners.slice().forEach(function (entry) {
        if (entry.collection && entry.collection !== change.collection) return;
        try { entry.listener(cloneJSON(change, 'change')); } catch (_) {}
      });
      if (!fromBroadcast && broadcast) {
        try {
          broadcast.postMessage({
            type: 'bw-reader-data-change',
            sender: instanceId,
            change: cloneJSON(change, 'change')
          });
        } catch (_) {}
      }
    }

    function notifyMany(changes, fromBroadcast) {
      (changes || []).forEach(function (change) { notifyOne(change, fromBroadcast); });
    }

    if (options.broadcast !== false && typeof root.BroadcastChannel === 'function') {
      try {
        broadcast = new root.BroadcastChannel(options.channelName || ('bw-reader-data:' + dbName));
        broadcast.onmessage = function (event) {
          var message = event && event.data;
          if (!message || message.type !== 'bw-reader-data-change' || message.sender === instanceId) return;
          try { notifyOne(cloneJSON(message.change, 'broadcast.change'), true); } catch (_) {}
        };
      } catch (_) {
        broadcast = null;
      }
    }

    function get(collection, id, queryOptions) {
      collection = safeName(collection, 'collection');
      id = safeName(id, 'id');
      queryOptions = queryOptions || {};
      return transact([STORE_RECORDS], 'readonly', function (transaction) {
        return requestResult(transaction.objectStore(STORE_RECORDS).get(physicalKey(collection, id)))
          .then(function (stored) {
            var record = fromStoredRecord(stored);
            if (record && record.deleted && !queryOptions.includeDeleted) return null;
            return record;
          });
      });
    }

    function list(collection, query) {
      collection = safeName(collection, 'collection');
      query = query || {};
      var offset = Math.max(0, Number(query.offset) || 0);
      var limit = Math.max(1, Math.min(1000, Number(query.limit) || 200));
      return transact([STORE_RECORDS], 'readonly', function (transaction) {
        var index = transaction.objectStore(STORE_RECORDS).index('collection');
        return openCursorValues(index, KeyRange.only(collection)).then(function (storedRecords) {
          var records = storedRecords.map(fromStoredRecord).filter(function (record) {
            if (!query.includeDeleted && record.deleted) return false;
            if (query.documentId != null &&
                String(record.value && record.value.documentId || '') !== String(query.documentId)) return false;
            if (
              query.orderBy === 'id' &&
              query.afterId != null &&
              String(record.id) <= String(query.afterId)
            ) return false;
            return true;
          }).sort(function (a, b) {
            if (query.orderBy === 'id') {
              return a.id < b.id ? -1 : (a.id > b.id ? 1 : 0);
            }
            return (a.updatedAt || 0) - (b.updatedAt || 0) || (a.id < b.id ? -1 : 1);
          });
          return records.slice(offset, offset + limit);
        });
      });
    }

    function putWithin(transaction, collection, value, operationOptions) {
      var body = value;
      var id = stableId(body, operationOptions, idFactory);
      var mutationId = mutationIdOf(operationOptions, collection, id);
      var records = transaction.objectStore(STORE_RECORDS);
      return getRemembered(transaction, mutationId).then(function (remembered) {
        if (remembered) return { result: cloneJSON(remembered.result), replay: true, change: null };
        return requestResult(records.get(physicalKey(collection, id))).then(function (storedCurrent) {
          var current = fromStoredRecord(storedCurrent);
          var record = makePutRecord(collection, id, current, body, operationOptions, {
            updatedAt: timestamp(),
            updatedBy: deviceId,
            causal: causalCollections.has(collection)
          });
          return requestResult(records.put(toStoredRecord(record))).then(function () {
            return appendJournal(transaction, collection, record, mutationId, 'put');
          }).then(function (change) {
            return remember(transaction, mutationId, record).then(function () {
              return { result: cloneJSON(record), replay: false, change: change };
            });
          });
        });
      });
    }

    function removeWithin(transaction, collection, id, operationOptions) {
      var mutationId = mutationIdOf(operationOptions, collection, id);
      var records = transaction.objectStore(STORE_RECORDS);
      return getRemembered(transaction, mutationId).then(function (remembered) {
        if (remembered) return { result: cloneJSON(remembered.result), replay: true, change: null };
        return requestResult(records.get(physicalKey(collection, id))).then(function (storedCurrent) {
          var current = fromStoredRecord(storedCurrent);
          var record = makeRemoveRecord(collection, id, current, operationOptions, {
            updatedAt: timestamp(),
            updatedBy: deviceId,
            causal: causalCollections.has(collection)
          });
          return requestResult(records.put(toStoredRecord(record))).then(function () {
            return appendJournal(transaction, collection, record, mutationId, 'remove');
          }).then(function (change) {
            return remember(transaction, mutationId, record).then(function () {
              return { result: cloneJSON(record), replay: false, change: change };
            });
          });
        });
      });
    }

    function finishWrite(result) {
      notifyMany(result.changes);
      return result.value;
    }

    function put(collection, value, operationOptions) {
      try {
        collection = safeName(collection, 'collection');
        value = normalizePutValue(value, 'put.value');
        operationOptions = normalizeOperationOptions(operationOptions);
      } catch (error) {
        return Promise.reject(error);
      }
      var changes = [];
      return transact(ALL_STORES, 'readwrite', function (transaction) {
        return putWithin(transaction, collection, value, operationOptions).then(function (outcome) {
          if (outcome.change) changes.push(outcome.change);
          return Promise.all([
            trimOldest(transaction.objectStore(STORE_JOURNAL), maxJournal),
            trimOldest(transaction.objectStore(STORE_MUTATIONS), maxMutations, 'rememberedAt')
          ]).then(function () { return { value: outcome.result, changes: changes }; });
        });
      }).then(finishWrite);
    }

    function remove(collection, id, operationOptions) {
      try {
        collection = safeName(collection, 'collection');
        id = safeName(id, 'id');
        operationOptions = normalizeOperationOptions(operationOptions);
      } catch (error) {
        return Promise.reject(error);
      }
      var changes = [];
      return transact(ALL_STORES, 'readwrite', function (transaction) {
        return removeWithin(transaction, collection, id, operationOptions).then(function (outcome) {
          if (outcome.change) changes.push(outcome.change);
          return Promise.all([
            trimOldest(transaction.objectStore(STORE_JOURNAL), maxJournal),
            trimOldest(transaction.objectStore(STORE_MUTATIONS), maxMutations, 'rememberedAt')
          ]).then(function () { return { value: outcome.result, changes: changes }; });
        });
      }).then(finishWrite);
    }

    function batch(mutations) {
      try {
        mutations = prepareBatch(mutations);
      } catch (error) {
        return Promise.reject(error);
      }
      if (!mutations.length) return Promise.resolve([]);
      var changes = [];
      return transact(ALL_STORES, 'readwrite', function (transaction) {
        var results = [];
        return mutations.reduce(function (chain, mutation) {
          return chain.then(function () {
            var work = mutation.operation === 'remove'
              ? removeWithin(transaction, mutation.collection, mutation.id, mutation.options)
              : putWithin(transaction, mutation.collection, mutation.value, mutation.options);
            return work.then(function (outcome) {
              results.push(outcome.result);
              if (outcome.change) changes.push(outcome.change);
            });
          });
        }, Promise.resolve()).then(function () {
          return Promise.all([
            trimOldest(transaction.objectStore(STORE_JOURNAL), maxJournal),
            trimOldest(transaction.objectStore(STORE_MUTATIONS), maxMutations, 'rememberedAt')
          ]).then(function () { return { value: results, changes: changes }; });
        });
      }).then(finishWrite);
    }

    function changes(query) {
      query = query || {};
      var after = Math.max(0, Number(query.after) || 0);
      var limit = Math.max(1, Math.min(2000, Number(query.limit) || 500));
      return transact([STORE_JOURNAL, STORE_META], 'readonly', function (transaction) {
        var range = KeyRange.lowerBound(after, true);
        return Promise.all([
          openCursorValues(transaction.objectStore(STORE_JOURNAL), range, limit),
          cursorOf(transaction),
          openCursorValues(transaction.objectStore(STORE_JOURNAL), null, 1)
        ]).then(function (parts) {
          var items = parts[0].map(function (item) { return cloneJSON(item, 'change'); });
          var cursor = parts[1];
          var oldestCursor = parts[2].length
            ? Math.max(0, Number(parts[2][0].cursor) || 0)
            : cursor + 1;
          var resetRequired = after > cursor ||
            after < Math.max(0, oldestCursor - 1);
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
      });
    }

    function migrateLegacyCausal(options2) {
      options2 = options2 || {};
      return transact(ALL_STORES, 'readwrite', function (transaction) {
        var journalStore = transaction.objectStore(STORE_JOURNAL);
        var recordStore = transaction.objectStore(STORE_RECORDS);
        var mutationStore = transaction.objectStore(STORE_MUTATIONS);
        return Promise.all([
          openCursorValues(journalStore),
          cursorOf(transaction)
        ]).then(function (parts) {
          var journal = parts[0];
          var plan = planLegacyCausalMigration({
            contract: options2.contract,
            mode: options2.mode,
            after: options2.after,
            cursor: parts[1],
            journal: journal,
            causalCollections: Array.from(causalCollections),
            baselineComplete: options2.baselineComplete === true,
            baselines: options2.baselines || []
          });
          var report = {
            contract: plan.contract,
            causalCollections: Array.from(causalCollections).sort(),
            after: plan.after,
            throughCursor: plan.throughCursor,
            examined: plan.examined,
            missing: plan.missing,
            migrated: plan.migrated,
            verified: plan.verified,
            needsBaseline: plan.needsBaseline
          };
          if (options2.mode !== 'apply' || !plan.patches.length) return report;
          var currentEntries = Array.from(plan.finalRecords.values());
          return Promise.all([
            Promise.all(currentEntries.map(function (record) {
              return requestResult(
                recordStore.get(physicalKey(record.collection, record.id))
              );
            })),
            Promise.all(plan.patches.map(function (patch) {
              return patch.mutationId
                ? requestResult(mutationStore.get(String(patch.mutationId)))
                : Promise.resolve(null);
            }))
          ]).then(function (loaded) {
            loaded[0].forEach(function (storedCurrent, index) {
              var current = fromStoredRecord(storedCurrent);
              var finalRecord = currentEntries[index];
              if (!current || !same(current, finalRecord)) {
                throw new DataStoreError(
                  '旧因果迁移当前 head 与待上传 journal 不一致',
                  'BW_DATA_CAUSAL_MIGRATION_HEAD',
                  { collection: finalRecord.collection, id: finalRecord.id }
                );
              }
            });
            loaded[1].forEach(function (remembered, index) {
              if (
                remembered &&
                !sameRecordWithoutCausal(
                  remembered.result,
                  plan.patches[index].record
                )
              ) {
                throw new DataStoreError(
                  '旧因果迁移 mutation receipt 与 journal 不一致',
                  'BW_DATA_CAUSAL_MIGRATION_RECEIPT',
                  { mutationId: String(plan.patches[index].mutationId) }
                );
              }
            });
            var writes = [];
            plan.patches.forEach(function (patch, index) {
              writes.push(requestResult(journalStore.put(cloneJSON(
                patch,
                'migration.journal.patch'
              ))));
              var remembered = loaded[1][index];
              if (remembered) {
                remembered.result = cloneJSON(
                  patch.record,
                  'migration.mutation.result'
                );
                writes.push(requestResult(mutationStore.put(remembered)));
              }
            });
            loaded[0].forEach(function (storedCurrent, index) {
              var current = fromStoredRecord(storedCurrent);
              var finalRecord = currentEntries[index];
              if (sameRecordWithoutCausal(current, finalRecord)) {
                writes.push(requestResult(
                  recordStore.put(toStoredRecord(finalRecord))
                ));
              }
            });
            return Promise.all(writes).then(function () { return report; });
          });
        });
      });
    }

    function applyChanges(incoming, applyOptions) {
      var normalizedIncoming;
      try {
        incoming = Array.isArray(incoming) ? cloneJSON(incoming, 'incoming') : [];
        normalizedIncoming = incoming.map(normalizeIncomingChange);
      } catch (error) {
        return Promise.reject(error);
      }
      applyOptions = applyOptions || {};
      var journalImported = applyOptions.journal === true;
      var tombstoneDominates = applyOptions.tombstoneDominates === true;
      var snapshotBaseline = applyOptions.snapshotBaseline === true;
      var notifications = [];
      return transact(ALL_STORES, 'readwrite', function (transaction) {
        var applied = [];
        var conflicts = [];
        var skipped = [];
        return normalizedIncoming.reduce(function (chain, normalized) {
          return chain.then(function () {
            var change = normalized.change;
            var collection = normalized.collection;
            var mutationId = normalized.mutationId;
            var cleanRecord = normalized.record;
            return getRemembered(transaction, mutationId).then(function (remembered) {
              if (remembered) {
                skipped.push(mutationId);
                return;
              }
              var records = transaction.objectStore(STORE_RECORDS);
              return requestResult(records.get(physicalKey(collection, cleanRecord.id))).then(function (storedCurrent) {
                var current = fromStoredRecord(storedCurrent);
                var incomingRev = cleanRecord.rev;
                var currentRev = Number(current && current.rev || 0);
                var sameBusiness = current && same(current, cleanRecord);
                var causalRequired = causalCollections.has(collection);
                var proof = causalRequired
                  ? inspectCausalProof(cleanRecord)
                  : null;
                var linearTombstoneChild = causalRequired &&
                  proof.valid &&
                  causalParentMatches(current, proof);
                if (sameBusiness && incomingRev <= currentRev) {
                  skipped.push(mutationId || (collection + '/' + cleanRecord.id));
                  return mutationId
                    ? remember(transaction, mutationId, cleanRecord)
                    : undefined;
                }
                if (tombstoneDominates && current && current.deleted === true &&
                    cleanRecord.deleted !== true && !linearTombstoneChild) {
                  conflicts.push({
                    mutationId: mutationId,
                    collection: collection,
                    id: cleanRecord.id,
                    local: cloneJSON(current),
                    incoming: cloneJSON(cleanRecord),
                    reason: 'tombstone-dominates'
                  });
                  return;
                }
                var causalAccepted = causalRequired && (
                  snapshotBaseline && current === null ||
                  proof.valid && causalParentMatches(current, proof)
                );
                if (!sameBusiness && causalRequired && !causalAccepted) {
                  conflicts.push({
                    mutationId: mutationId,
                    collection: collection,
                    id: cleanRecord.id,
                    local: cloneJSON(current),
                    incoming: cloneJSON(cleanRecord),
                    incomingRev: incomingRev,
                    currentRev: currentRev,
                    reason: proof.valid ? 'causal-parent-mismatch' : proof.reason
                  });
                  return;
                }
                if (
                  !sameBusiness &&
                  !causalRequired &&
                  current &&
                  incomingRev <= currentRev
                ) {
                  conflicts.push({
                    mutationId: mutationId,
                    collection: collection,
                    id: cleanRecord.id,
                    local: cloneJSON(current),
                    incoming: cloneJSON(cleanRecord),
                    incomingRev: incomingRev,
                    currentRev: currentRev,
                    reason: incomingRev === currentRev
                      ? 'same-rev-different-value'
                      : 'stale-incoming'
                  });
                  return;
                }
                if (
                  causalRequired &&
                  !sameBusiness &&
                  currentRev >= Number.MAX_SAFE_INTEGER
                ) {
                  conflicts.push({
                    mutationId: mutationId,
                    collection: collection,
                    id: cleanRecord.id,
                    local: cloneJSON(current),
                    incoming: cloneJSON(cleanRecord),
                    incomingRev: incomingRev,
                    currentRev: currentRev,
                    reason: 'causal-revision-overflow'
                  });
                  return;
                }
                var acceptedRecord = cloneJSON(cleanRecord);
                if (causalRequired && !sameBusiness) {
                  acceptedRecord.rev = Math.max(incomingRev, currentRev + 1);
                }
                var apply = requestResult(records.put(toStoredRecord(acceptedRecord))).then(function () {
                  var notification = {
                    cursor: Number(change.cursor) || 0,
                    mutationId: mutationId,
                    operation: acceptedRecord.deleted ? 'remove' : 'put',
                    collection: collection,
                    record: cloneJSON(acceptedRecord),
                    remote: true
                  };
                  if (!journalImported) {
                    return notification;
                  }
                  return appendJournal(
                    transaction,
                    collection,
                    acceptedRecord,
                    mutationId,
                    acceptedRecord.deleted ? 'remove' : 'put',
                    { imported: true, remote: false }
                  );
                }).then(function (notification) {
                  applied.push({
                    collection: collection,
                    id: acceptedRecord.id,
                    rev: acceptedRecord.rev,
                    cursor: journalImported ? notification.cursor : null
                  });
                  notifications.push(notification);
                });
                return apply.then(function () {
                  return mutationId ? remember(transaction, mutationId, acceptedRecord) : undefined;
                });
              });
            });
          });
        }, Promise.resolve()).then(function () {
          return Promise.all([
            trimOldest(transaction.objectStore(STORE_JOURNAL), maxJournal),
            trimOldest(transaction.objectStore(STORE_MUTATIONS), maxMutations, 'rememberedAt')
          ]).then(function () {
            return { applied: applied, conflicts: conflicts, skipped: skipped };
          });
        });
      }).then(function (result) {
        notifyMany(notifications);
        return result;
      });
    }

    function subscribe(query, listener) {
      if (typeof query === 'function') {
        listener = query;
        query = {};
      }
      if (typeof listener !== 'function') {
        throw new DataStoreError('subscribe listener 必须是函数', 'BW_DATA_INVALID');
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
      return transact([STORE_RECORDS, STORE_JOURNAL, STORE_META], 'readonly', function (transaction) {
        var recordStore = transaction.objectStore(STORE_RECORDS);
        return Promise.all([
          cursorOf(transaction),
          requestResult(transaction.objectStore(STORE_JOURNAL).count()),
          openCursorValues(recordStore.index('collection'))
        ]).then(function (parts) {
          var collectionSet = {};
          parts[2].forEach(function (record) {
            if (record && record.collection) collectionSet[String(record.collection)] = true;
          });
          return {
            contract: CONTRACT,
            backend: 'indexeddb',
            deviceId: deviceId,
            cursor: parts[0],
            journalSize: parts[1],
            collections: Object.keys(collectionSet).sort()
          };
        });
      });
    }

    function close() {
      if (closed) return;
      closed = true;
      listeners.splice(0, listeners.length);
      if (broadcast) {
        try { broadcast.close(); } catch (_) {}
        broadcast = null;
      }
      if (database) {
        try { database.close(); } catch (_) {}
        database = null;
      } else if (databasePromise) {
        databasePromise.then(function (db) {
          try { db.close(); } catch (_) {}
        }).catch(function () {});
      }
      databasePromise = null;
    }

    return {
      contract: CONTRACT,
      get: get,
      list: list,
      put: put,
      remove: remove,
      batch: batch,
      changes: changes,
      migrateLegacyCausal: migrateLegacyCausal,
      applyChanges: applyChanges,
      subscribe: subscribe,
      instanceEpoch: instanceEpoch,
      status: status,
      close: close
    };
  }

  return {
    CONTRACT: CONTRACT,
    SCHEMA: SCHEMA,
    DB_VERSION: DB_VERSION,
    INSTANCE_EPOCH_CONTRACT: INSTANCE_EPOCH_CONTRACT,
    CAUSAL_MIGRATION_CONTRACT: CAUSAL_MIGRATION_CONTRACT,
    DataStoreError: DataStoreError,
    ConflictError: ConflictError,
    createIndexedDBDataStore: createIndexedDBDataStore,
    createDataStore: createIndexedDBDataStore,
    stableId: stableId,
    assertJSON: assertJSON
  };
});
