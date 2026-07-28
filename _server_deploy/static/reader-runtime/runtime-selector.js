/* runtime-selector.js — 两种产品模式的唯一切换点。
 *
 * PWA 始终拥有 DocumentHost 与 UI。扩展出现时，只替换 global DataStore、
 * SyncGateway 与后续网络服务；document store 永远不被扩展接管。
 */
(function (root, factory) {
  var api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.BWReaderRuntime = root.BWReaderRuntime || {};
  root.BWReaderRuntime.runtimeSelector = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function (root) {
  'use strict';

  var CONTRACT = 'reader-runtime/1';

  function clone(value) {
    if (value == null) return value;
    return JSON.parse(JSON.stringify(value));
  }
  function stableStringify(value) {
    if (value === null || typeof value !== 'object') {
      return JSON.stringify(value);
    }
    if (Array.isArray(value)) {
      return '[' + value.map(stableStringify).join(',') + ']';
    }
    return '{' + Object.keys(value).sort().map(function (key) {
      return JSON.stringify(key) + ':' + stableStringify(value[key]);
    }).join(',') + '}';
  }
  function sameBusinessRecord(a, b) {
    if (!a || !b) return false;
    if (
      String(a.collection || '') !== String(b.collection || '') ||
      String(a.id || '') !== String(b.id || '') ||
      (a.deleted === true) !== (b.deleted === true)
    ) {
      return false;
    }
    if (a.deleted === true) return true;
    return stableStringify(a.value) === stableStringify(b.value);
  }
  function runtimeApi(name, localPath) {
    var found = root.BWReaderRuntime && root.BWReaderRuntime[name];
    if (!found && typeof module === 'object' && module.exports && typeof require === 'function') {
      found = require(localPath);
    }
    return found;
  }
  function ensureStore(store, label) {
    ['get', 'list', 'put', 'remove', 'batch', 'changes', 'applyChanges', 'subscribe', 'status'].forEach(function (name) {
      if (!store || typeof store[name] !== 'function') {
        throw new RuntimeError(label + ' 不符合 DataStore 契约：缺少 ' + name, 'BW_RUNTIME_STORE');
      }
    });
    return store;
  }

  function RuntimeError(message, code, details) {
    this.name = 'ReaderRuntimeError';
    this.code = code || 'BW_RUNTIME_ERROR';
    this.message = message;
    this.details = details || null;
    if (Error.captureStackTrace) Error.captureStackTrace(this, RuntimeError);
  }
  RuntimeError.prototype = Object.create(Error.prototype);
  RuntimeError.prototype.constructor = RuntimeError;

  function reconcileStores(left, right, collections, options) {
    left = ensureStore(left, 'leftStore');
    right = ensureStore(right, 'rightStore');
    options = options || {};
    var isCurrent = typeof options.isCurrent === 'function'
      ? options.isCurrent
      : function () { return true; };
    collections = (collections || []).map(function (item) {
      if (typeof item === 'string') {
        return {
          name: item,
          provider: true,
          conflictPolicy: 'explicit',
          derived: false
        };
      }
      item = item || {};
      return {
        name: String(item.name || item.collection || ''),
        provider: item.provider === true,
        conflictPolicy: String(item.conflictPolicy || (item.derived ? 'regenerate' : 'explicit')),
        derived: item.derived === true
      };
    }).filter(function (item) {
      return !!item.name && item.provider === true;
    });
    var copiedToLeft = [], copiedToRight = [], conflicts = [], regenerated = [];
    var toLeft = [], toRight = [];
    /* 扩展消息通道单页上限为 200，避免 Safari/Chrome 因超大结构化克隆断口。 */
    var PAGE_SIZE = 200;
    /* provider 单次 applyChanges 的协议上限。 */
    var APPLY_BATCH_SIZE = 100;

    function byId(records) {
      var out = {};
      (records || []).forEach(function (record) { out[record.id] = record; });
      return out;
    }
    function listAll(store, collection) {
      var records = [];
      function readPage(offset) {
        return store.list(collection, {
          includeDeleted: true,
          offset: offset,
          limit: PAGE_SIZE
        }).then(function (page) {
          page = Array.isArray(page) ? page : [];
          Array.prototype.push.apply(records, page);
          if (page.length < PAGE_SIZE) return records;
          return readPage(offset + page.length);
        });
      }
      return readPage(0);
    }
    function queueChange(target, collection, record, source) {
      target.push({
        label: collection + '/' + record.id,
        change: {
          collection: collection,
          mutationId: 'reconcile:' + source + ':' + collection + ':' + record.id + ':' + record.rev,
          record: record
        }
      });
    }
    function scanCollection(spec) {
      var collection = spec.name;
      return Promise.all([
        listAll(left, collection),
        listAll(right, collection)
      ]).then(function (pair) {
        var lmap = byId(pair[0]), rmap = byId(pair[1]);
        var ids = {};
        Object.keys(lmap).forEach(function (id) { ids[id] = true; });
        Object.keys(rmap).forEach(function (id) { ids[id] = true; });
        Object.keys(ids).sort().forEach(function (id) {
          var local = lmap[id], remote = rmap[id];
          if (!local && remote) {
            queueChange(toLeft, collection, remote, 'right');
            return;
          }
          if (local && !remote) {
            queueChange(toRight, collection, local, 'left');
            return;
          }
          if (sameBusinessRecord(local, remote)) {
            var localRev = Number(local.rev) || 0;
            var remoteRev = Number(remote.rev) || 0;
            if (localRev > remoteRev) {
              queueChange(toRight, collection, local, 'left');
            } else if (remoteRev > localRev) {
              queueChange(toLeft, collection, remote, 'right');
            }
            return;
          }
          if (spec.conflictPolicy === 'regenerate' || spec.derived) {
            /*
             * 衍生缓存分叉不覆盖任何一边：provider 接管后使用扩展侧缓存，PWA 侧原值
             * 留作断线 fallback；调用方依据此报告按需重新生成，用户设置不会被牵连。
             */
            regenerated.push({
              collection: collection,
              id: id,
              strategy: 'keep-both-regenerate-on-demand',
              pwa: clone(local),
              extension: clone(remote)
            });
            return;
          }
          conflicts.push({
            collection: collection,
            id: id,
            pwa: clone(local),
            extension: clone(remote),
            reason: 'stable-id-diverged'
          });
        });
      });
    }
    function applyPlan(store, plan, copied, targetName) {
      var offset = 0;
      function nextBatch() {
        if (offset >= plan.length || conflicts.length || !isCurrent()) return Promise.resolve();
        var batch = plan.slice(offset, offset + APPLY_BATCH_SIZE);
        offset += batch.length;
        return store.applyChanges(batch.map(function (item) {
          return item.change;
        }), { journal: true }).then(function (result) {
          result = result || {};
          var applyConflicts = Array.isArray(result.conflicts) ? result.conflicts : [];
          if (applyConflicts.length) {
            applyConflicts.forEach(function (conflict) {
              conflicts.push({
                collection: conflict.collection || conflict.change && conflict.change.collection || '',
                id: conflict.id || conflict.change && conflict.change.record && conflict.change.record.id || '',
                reason: 'apply-conflict',
                target: targetName,
                details: clone(conflict)
              });
            });
            return null;
          }
          var applied = {};
          (result.applied || []).forEach(function (item) {
            applied[String(item.collection || '') + '/' + String(item.id || '')] = true;
          });
          batch.forEach(function (item) {
            if (applied[item.label]) copied.push(item.label);
          });
          return nextBatch();
        });
      }
      return nextBatch();
    }
    /*
     * 第一阶段必须扫完全部 provider collection。只要任一 explicit collection
     * 分叉，就不执行任何 applyChanges，避免“前一集合已写、后一集合才发现冲突”。
     */
    return collections.reduce(function (chain, spec) {
      return chain.then(function () {
        return scanCollection(spec);
      });
    }, Promise.resolve()).then(function () {
      if (conflicts.length || !isCurrent()) return null;
      return applyPlan(left, toLeft, copiedToLeft, 'pwa').then(function () {
        if (conflicts.length || !isCurrent()) return null;
        return applyPlan(right, toRight, copiedToRight, 'extension');
      });
    }).then(function () {
      return {
        copiedToPwa: copiedToLeft,
        copiedToExtension: copiedToRight,
        conflicts: conflicts,
        regenerated: regenerated,
        cancelled: !isCurrent()
      };
    });
  }

  function createReaderRuntime(options) {
    options = options || {};
    var routerModule = options.storageRouterApi || runtimeApi('storageRouter', './storage-router.js');
    if (!routerModule || typeof routerModule.createStorageRouter !== 'function') {
      throw new RuntimeError('缺少 StorageRouter', 'BW_RUNTIME_DEPENDENCY');
    }
    var dataRegistry = options.dataRegistryApi || runtimeApi('dataRegistry', './data-registry.js');
    var documentHost = options.documentHost;
    if (!documentHost || typeof documentHost.audit !== 'function' || !documentHost.audit().valid) {
      throw new RuntimeError('documentHost 无效', 'BW_RUNTIME_HOST');
    }
    var ui = options.ui;
    if (!ui || typeof ui.mount !== 'function') throw new RuntimeError('PWA UI renderer 缺少 mount', 'BW_RUNTIME_UI');
    var pwaStore = ensureStore(options.pwaStore, 'pwaStore');
    var pwaDocumentStore = ensureStore(options.pwaDocumentStore || pwaStore, 'pwaDocumentStore');
    var pwaDeviceStore = ensureStore(options.pwaDeviceStore, 'pwaDeviceStore');
    var pwaSyncGateway = options.pwaSyncGateway || null;
    var activeSyncGateway = pwaSyncGateway;
    var mode = 'pwa-fallback';
    var mounted = false;
    var mountResult = null;
    var extensionProvider = null;
    var providerGeneration = 0;
    var attachQueue = Promise.resolve();
    var mirrorUnsubscribe = null;
    var mirrorQueue = Promise.resolve();
    var mirrorOperations = [];
    var mirrorConflicts = [];
    var router = routerModule.createStorageRouter({
      globalStore: pwaStore,
      documentStore: pwaDocumentStore,
      deviceStore: pwaDeviceStore,
      scopes: options.scopes,
      dataRegistryApi: dataRegistry
    });

    function canonicalSyncDigest(descriptor) {
      return 'sync-v3:record-parent-state/1|' +
        descriptor.map(function (item) {
        return [
          item.name,
          item.conflictPolicy,
          item.derived ? '1' : '0',
          String(item.recordSchema)
        ].join(':');
      }).join('|');
    }
    function providerRegistry(provider) {
      if (
        !dataRegistry ||
        dataRegistry.CONTRACT !== 'data-registry/1' ||
        dataRegistry.SYNC_CONTRACT !== 'sync-v3' ||
        dataRegistry.SYNC_CHANGE_CONTRACT !== 'record-parent-state/1' ||
        typeof dataRegistry.scopes !== 'function' ||
        typeof dataRegistry.collection !== 'function' ||
        typeof dataRegistry.providerCollections !== 'function' ||
        typeof dataRegistry.syncCollections !== 'function' ||
        typeof dataRegistry.syncDescriptor !== 'function' ||
        typeof dataRegistry.syncDigest !== 'function' ||
        typeof dataRegistry.isSyncCollection !== 'function'
      ) {
        throw new RuntimeError(
          '缺少完整 DataRegistry/provider/sync-v3 因果合同，保持 PWA fallback',
          'BW_RUNTIME_REGISTRY'
        );
      }
      var authorityScopes = dataRegistry.scopes();
      var declared;
      var declaredSync;
      var descriptor;
      var digest;
      try {
        declared = dataRegistry.providerCollections();
        declaredSync = dataRegistry.syncCollections();
        descriptor = dataRegistry.syncDescriptor();
        digest = String(dataRegistry.syncDigest() || '');
      }
      catch (error) {
        throw new RuntimeError(
          '读取 DataRegistry provider/sync-v3 因果合同失败',
          'BW_RUNTIME_REGISTRY',
          { cause: String(error && error.message || error) }
        );
      }
      if (
        !authorityScopes ||
        typeof authorityScopes !== 'object' ||
        Array.isArray(authorityScopes) ||
        !Array.isArray(declared) ||
        !Array.isArray(declaredSync) ||
        !Array.isArray(descriptor)
      ) {
        throw new RuntimeError(
          'DataRegistry provider/sync-v3 因果合同无效',
          'BW_RUNTIME_REGISTRY'
        );
      }
      var seen = {};
      declared = declared.map(function (name) {
        name = String(name || '').trim();
        if (!name || seen[name]) {
          throw new RuntimeError(
            'DataRegistry provider 白名单包含空项或重复项',
            'BW_RUNTIME_REGISTRY'
          );
        }
        seen[name] = true;
        return name;
      }).sort();
      var expected = Object.keys(authorityScopes).filter(function (name) {
        var entry = authorityScopes[name] || {};
        return entry.scope === 'global'
          && entry.status === 'ready'
          && entry.provider === true;
      }).sort();
      if (JSON.stringify(declared) !== JSON.stringify(expected)) {
        throw new RuntimeError(
          'DataRegistry.providerCollections 与归属表不一致',
          'BW_RUNTIME_REGISTRY',
          { declared: declared, expected: expected }
        );
      }
      var syncSeen = {};
      declaredSync = declaredSync.map(function (name) {
        name = String(name || '').trim();
        if (!name || syncSeen[name]) {
          throw new RuntimeError(
            'DataRegistry 同步白名单包含空项或重复项',
            'BW_RUNTIME_REGISTRY'
          );
        }
        syncSeen[name] = true;
        return name;
      }).sort();
      var expectedSync = Object.keys(authorityScopes).filter(function (name) {
        var entry = authorityScopes[name] || {};
        return entry.scope === 'global'
          && entry.status === 'ready'
          && entry.provider === true
          && entry.sync === true;
      }).sort();
      if (JSON.stringify(declaredSync) !== JSON.stringify(expectedSync)) {
        throw new RuntimeError(
          'DataRegistry.syncCollections 与归属表不一致',
          'BW_RUNTIME_REGISTRY',
          { declared: declaredSync, expected: expectedSync }
        );
      }
      descriptor = descriptor.map(function (raw, index) {
        var item = raw && typeof raw === 'object' && !Array.isArray(raw)
          ? clone(raw)
          : null;
        var expectedName = declaredSync[index];
        var authority = dataRegistry.collection(expectedName);
        if (
          !item ||
          JSON.stringify(Object.keys(item).sort()) !==
            JSON.stringify(['conflictPolicy', 'derived', 'name', 'recordSchema']) ||
          item.name !== expectedName ||
          !/^[A-Za-z0-9._-]+$/.test(String(expectedName || '')) ||
          typeof item.conflictPolicy !== 'string' ||
          !item.conflictPolicy.trim() ||
          !/^[A-Za-z0-9._-]+$/.test(item.conflictPolicy) ||
          typeof item.derived !== 'boolean' ||
          !Number.isInteger(item.recordSchema) ||
          item.recordSchema < 1 ||
          !authority ||
          authority.scope !== 'global' ||
          authority.status !== 'ready' ||
          authority.provider !== true ||
          authority.sync !== true ||
          dataRegistry.isSyncCollection(expectedName) !== true ||
          String(authority.conflictPolicy || '') !== item.conflictPolicy ||
          (authority.derived === true) !== item.derived ||
          Number(authority.recordSchema) !== item.recordSchema
        ) {
          throw new RuntimeError(
            'DataRegistry 同步描述符与 collection 元数据不一致：' +
              String(expectedName || ''),
            'BW_RUNTIME_REGISTRY'
          );
        }
        return item;
      });
      if (
        descriptor.length !== declaredSync.length ||
        digest !== canonicalSyncDigest(descriptor)
      ) {
        throw new RuntimeError(
          'DataRegistry sync-v3 摘要与描述符不一致',
          'BW_RUNTIME_REGISTRY'
        );
      }
      if (
        provider &&
        (provider.kind === 'extension-services' || provider.contract === 'bw-reader-services/1')
      ) {
        var advertised = provider.capabilities && provider.capabilities.providerCollections;
        if (!Array.isArray(advertised)) {
          throw new RuntimeError(
            '扩展没有声明 providerCollections，保持 PWA fallback',
            'BW_RUNTIME_PROVIDER_REGISTRY'
          );
        }
        var advertisedSeen = {};
        advertised = advertised.map(function (name) {
          name = String(name || '').trim();
          if (!name || advertisedSeen[name]) {
            throw new RuntimeError(
              '扩展 providerCollections 包含空项或重复项',
              'BW_RUNTIME_PROVIDER_REGISTRY'
            );
          }
          advertisedSeen[name] = true;
          return name;
        }).sort();
        if (JSON.stringify(advertised) !== JSON.stringify(declared)) {
          throw new RuntimeError(
            '扩展与 PWA 的 provider collection 白名单不一致',
            'BW_RUNTIME_PROVIDER_REGISTRY',
            { extension: advertised, pwa: declared }
          );
        }
        var capabilities = provider.capabilities || {};
        var advertisedSync = capabilities.syncCollections;
        var advertisedDescriptor = capabilities.syncDescriptor;
        var advertisedDigest = String(capabilities.syncDigest || '');
        var advertisedSyncContract = String(
          capabilities.syncContract || ''
        );
        var advertisedChangeContract = String(
          capabilities.syncChangeContract || ''
        );
        if (
          !Array.isArray(advertisedSync) ||
          !Array.isArray(advertisedDescriptor) ||
          advertisedSyncContract !== dataRegistry.SYNC_CONTRACT ||
          advertisedChangeContract !== dataRegistry.SYNC_CHANGE_CONTRACT
        ) {
          throw new RuntimeError(
            '扩展没有声明完整 sync-v3 因果合同，保持 PWA fallback',
            'BW_RUNTIME_PROVIDER_REGISTRY'
          );
        }
        var advertisedSyncSeen = {};
        advertisedSync = advertisedSync.map(function (name) {
          name = String(name || '').trim();
          if (!name || advertisedSyncSeen[name]) {
            throw new RuntimeError(
              '扩展 syncCollections 包含空项或重复项',
              'BW_RUNTIME_PROVIDER_REGISTRY'
            );
          }
          advertisedSyncSeen[name] = true;
          return name;
        }).sort();
        if (
          JSON.stringify(advertisedSync) !== JSON.stringify(declaredSync) ||
          JSON.stringify(advertisedDescriptor) !== JSON.stringify(descriptor) ||
          advertisedDigest !== digest
        ) {
          throw new RuntimeError(
            '扩展与 PWA 的 sync-v3 因果合同不一致',
            'BW_RUNTIME_PROVIDER_REGISTRY',
            {
              extension: {
                collections: advertisedSync,
                descriptor: advertisedDescriptor,
                digest: advertisedDigest,
                syncContract: advertisedSyncContract,
                syncChangeContract: advertisedChangeContract
              },
              pwa: {
                collections: declaredSync,
                descriptor: descriptor,
                digest: digest,
                syncContract: dataRegistry.SYNC_CONTRACT,
                syncChangeContract: dataRegistry.SYNC_CHANGE_CONTRACT
              }
            }
          );
        }
      }
      var routedScopes = router.scopes();
      return declared.map(function (collection) {
        var authority = dataRegistry.collection(collection);
        var routed = routedScopes[collection];
        if (
          !authority ||
          authority.scope !== 'global' ||
          authority.status !== 'ready' ||
          authority.provider !== true ||
          !routed ||
          routed.scope !== 'global' ||
          routed.status !== 'ready' ||
          routed.provider !== true
        ) {
          throw new RuntimeError(
            'provider collection 未经 DataRegistry 与 StorageRouter 双重确认：' + collection,
            'BW_RUNTIME_REGISTRY',
            { collection: collection }
          );
        }
        return {
          name: collection,
          provider: true,
          conflictPolicy: String(authority.conflictPolicy || (authority.derived ? 'regenerate' : 'explicit')),
          derived: authority.derived === true,
          recordSchema: Number(authority.recordSchema) || 0
        };
      });
    }
    function cancelledAttach(generation) {
      return {
        connected: false,
        mode: mode,
        conflicts: [],
        cancelled: true,
        generation: generation,
        reason: '接管请求已被更新的连接状态取消'
      };
    }
    function stopMirror() {
      if (mirrorUnsubscribe) {
        try { mirrorUnsubscribe(); } catch (_) {}
      }
      mirrorUnsubscribe = null;
    }
    function rememberMirrorConflicts(items, source) {
      (items || []).forEach(function (item) {
        mirrorConflicts.push({
          source: source,
          collection: item.collection || item.change && item.change.collection || '',
          id: item.id || item.change && item.change.record && item.change.record.id || '',
          details: clone(item)
        });
      });
      if (mirrorConflicts.length > 100) {
        mirrorConflicts = mirrorConflicts.slice(mirrorConflicts.length - 100);
      }
    }
    function rememberMirrorError(error, source, batch) {
      var first = batch && batch[0] || {};
      rememberMirrorConflicts([{
        collection: first.collection || first.record && first.record.collection || '',
        id: first.record && first.record.id || '',
        reason: 'mirror-write-failed',
        count: (batch || []).length,
        code: String(error && error.code || ''),
        message: String(error && error.message || error || 'unknown mirror error')
      }], source);
    }
    function trackMirrorOperation(operation) {
      mirrorOperations.push(operation);
      function remove() {
        var index = mirrorOperations.indexOf(operation);
        if (index >= 0) mirrorOperations.splice(index, 1);
      }
      operation.then(remove, remove);
      return operation;
    }
    function settleMirror() {
      var operations = mirrorOperations.slice();
      return Promise.all(operations.map(function (operation) {
        return operation.catch(function () { return null; });
      })).then(function () {
        return mirrorQueue.catch(function () { return null; });
      });
    }
    function mirrorChanges(changes, generation, source, accepted) {
      changes = (changes || []).filter(function (change) {
        if (!change || !change.record) return false;
        var entry;
        try { entry = router.describe(change.collection || change.record.collection); }
        catch (_) { return false; }
        return entry.scope === 'global' && entry.provider === true;
      });
      /*
       * generation 只在入队前校验。已经由当前 provider 接受的变化即使随后 detach，
       * 也必须排完队写入 fallback；否则断线后会读到旧影子。
       */
      if (!changes.length || (accepted !== true && generation !== providerGeneration)) return Promise.resolve();
      mirrorQueue = mirrorQueue.catch(function () {
        return null;
      }).then(function () {
        var offset = 0;
        var reports = [];
        function nextBatch() {
          if (offset >= changes.length) return reports;
          var batch = changes.slice(offset, offset + 100);
          offset += batch.length;
          return pwaStore.applyChanges(batch, { journal: false }).then(function (result) {
            reports.push(result || {});
            rememberMirrorConflicts(result && result.conflicts, source);
            return nextBatch();
          }).catch(function (error) {
            rememberMirrorError(error, source, batch);
            throw error;
          });
        }
        return nextBatch();
      });
      return mirrorQueue;
    }
    function mirroredStore(extensionStore, generation) {
      function activeWrite() {
        if (generation === providerGeneration) return null;
        return Promise.reject(new RuntimeError(
          '旧 provider 已退出，拒绝继续写入',
          'BW_RUNTIME_STALE_PROVIDER',
          { generation: generation, currentGeneration: providerGeneration }
        ));
      }
      function recordChange(collection, record, mutationId) {
        if (!record) return Promise.resolve();
        return mirrorChanges([{
          collection: collection,
          mutationId: String(mutationId || ''),
          record: record
        }], generation, 'direct-write', true);
      }
      return {
        contract: extensionStore.contract || 'data-store/1',
        get: function () { return extensionStore.get.apply(extensionStore, arguments); },
        list: function () { return extensionStore.list.apply(extensionStore, arguments); },
        put: function (collection, value, opts) {
          var stale = activeWrite();
          if (stale) return stale;
          opts = opts || {};
          return trackMirrorOperation(extensionStore.put(collection, value, opts).then(function (record) {
            return recordChange(collection, record, opts.mutationId).then(function () { return record; });
          }));
        },
        remove: function (collection, id, opts) {
          var stale = activeWrite();
          if (stale) return stale;
          opts = opts || {};
          return trackMirrorOperation(extensionStore.remove(collection, id, opts).then(function (record) {
            return recordChange(collection, record, opts.mutationId).then(function () { return record; });
          }));
        },
        batch: function (mutations) {
          var stale = activeWrite();
          if (stale) return stale;
          mutations = mutations || [];
          return trackMirrorOperation(extensionStore.batch(mutations).then(function (records) {
            var changes = (records || []).map(function (record, index) {
              var mutation = mutations[index] || {};
              var options2 = mutation.options || mutation;
              return {
                collection: mutation.collection || record && record.collection,
                mutationId: String(options2.mutationId || ''),
                record: record
              };
            });
            return mirrorChanges(changes, generation, 'direct-batch', true).then(function () { return records; });
          }));
        },
        changes: function () { return extensionStore.changes.apply(extensionStore, arguments); },
        applyChanges: function (changes, opts) {
          var stale = activeWrite();
          if (stale) return stale;
          return trackMirrorOperation(extensionStore.applyChanges(changes, opts).then(function (result) {
            var targets = {};
            ((result && result.applied) || []).forEach(function (item) {
              var collection = String(item.collection || '');
              var id = String(item.id || '');
              if (collection && id) targets[collection + '/' + id] = {
                collection: collection,
                id: id
              };
            });
            return Promise.all(Object.keys(targets).sort().map(function (key) {
              var target = targets[key];
              return extensionStore.get(target.collection, target.id, { includeDeleted: true }).then(function (record) {
                if (!record) return null;
                return {
                  collection: target.collection,
                  mutationId: 'mirror:direct-apply:' + target.collection + ':' + target.id + ':' + record.rev,
                  record: record
                };
              });
            })).then(function (accepted) {
              return mirrorChanges(accepted.filter(Boolean), generation, 'direct-apply', true).then(function () {
                return result;
              });
            });
          }));
        },
        subscribe: function () { return extensionStore.subscribe.apply(extensionStore, arguments); },
        status: function () { return extensionStore.status.apply(extensionStore, arguments); }
      };
    }
    function startMirror(extensionStore, generation) {
      stopMirror();
      mirrorUnsubscribe = extensionStore.subscribe({}, function (change) {
        mirrorChanges([change], generation, 'provider-change').catch(function () {});
      });
    }

    var runtime = {
      contract: CONTRACT,
      uiOwner: 'pwa',
      start: function () {
        if (mounted) return Promise.resolve(mountResult);
        mounted = true;
        try {
          return Promise.resolve(ui.mount({
            owner: 'pwa',
            documentHost: documentHost,
            storage: router
          })).then(function (result) {
            mountResult = result;
            return result;
          });
        } catch (error) {
          mounted = false;
          return Promise.reject(error);
        }
      },
      mode: function () { return mode; },
      documentHost: function () { return documentHost; },
      storage: function () { return router; },
      syncGateway: function () { return activeSyncGateway; },
      uiMounted: function () { return mounted; },
      attachExtension: function (provider) {
        provider = provider || {};
        var extensionStore = ensureStore(provider.dataStore, 'extension.dataStore');
        var generation = ++providerGeneration;
        if (extensionProvider) {
          stopMirror();
          router.setGlobalStore(pwaStore);
          activeSyncGateway = pwaSyncGateway;
          extensionProvider = null;
          mode = mirrorConflicts.length ? 'pwa-fallback-conflict' : 'pwa-fallback';
        }
        var operation = attachQueue.catch(function () {
          return null;
        }).then(function () {
          return settleMirror();
        }).then(function () {
          if (generation !== providerGeneration) return cancelledAttach(generation);
          var registry = providerRegistry(provider);
          var reconcile = typeof provider.reconcile === 'function'
            ? function () {
              return provider.reconcile({
                pwaStore: pwaStore,
                extensionStore: extensionStore,
                collections: registry.map(function (entry) { return entry.name; }),
                registry: clone(registry),
                generation: generation
              });
            }
            : function () {
              return reconcileStores(pwaStore, extensionStore, registry, {
                isCurrent: function () { return generation === providerGeneration; }
              });
            };
          return Promise.resolve().then(reconcile).then(function (report) {
            if (generation !== providerGeneration) return cancelledAttach(generation);
            report = report || {};
            var conflicts = Array.isArray(report.conflicts) ? report.conflicts : [];
            if (conflicts.length) {
              if (mirrorConflicts.length) mode = 'pwa-fallback-conflict';
              return {
                connected: false,
                mode: mode,
                conflicts: clone(conflicts),
                generation: generation,
                reason: '存在相同稳定编号的冲突，未擅自选择任何一方'
              };
            }
            var activeStore = mirroredStore(extensionStore, generation);
            try {
              router.setGlobalStore(activeStore);
              startMirror(extensionStore, generation);
            } catch (error) {
              stopMirror();
              router.setGlobalStore(pwaStore);
              throw error;
            }
            /*
             * Provider takeover transfers the global data authority to the
             * extension. A null provider gateway is intentional: it means the
             * extension background owns synchronization. Falling back to the
             * PWA gateway here would create two live sync owners.
             */
            activeSyncGateway = provider.syncGateway || null;
            extensionProvider = provider;
            /* 成功的完整对账已重新验证全部 provider collection。 */
            mirrorConflicts = [];
            mode = 'pwa-extension-provider';
            return {
              connected: true,
              mode: mode,
              conflicts: [],
              generation: generation,
              reconciliation: clone(report)
            };
          });
        });
        attachQueue = operation.then(function () {
          return null;
        }, function () {
          return null;
        });
        return operation;
      },
      detachExtension: function (reason) {
        providerGeneration += 1;
        stopMirror();
        router.setGlobalStore(pwaStore);
        activeSyncGateway = pwaSyncGateway;
        extensionProvider = null;
        mode = 'pwa-fallback';
        var generation = providerGeneration;
        return settleMirror().then(function () {
          mode = mirrorConflicts.length ? 'pwa-fallback-conflict' : 'pwa-fallback';
          return {
            connected: false,
            mode: mode,
            generation: generation,
            mirrorConflicts: clone(mirrorConflicts),
            reason: String(reason || 'extension-disconnected')
          };
        });
      },
      status: function () {
        return router.status().then(function (storageStatus) {
          return {
            contract: CONTRACT,
            mode: mode,
            uiOwner: 'pwa',
            uiMounted: mounted,
            documentHost: {
              contract: documentHost.contract,
              kind: documentHost.kind,
              documentId: documentHost.documentId
            },
            extensionConnected: !!extensionProvider,
            mirrorConflicts: clone(mirrorConflicts),
            storage: storageStatus
          };
        });
      }
    };
    return runtime;
  }

  return {
    CONTRACT: CONTRACT,
    RuntimeError: RuntimeError,
    reconcileStores: reconcileStores,
    createReaderRuntime: createReaderRuntime
  };
});
