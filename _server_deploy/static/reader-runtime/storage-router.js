/* storage-router.js — 把通用数据与文档专属数据明确分流。
 *
 * 未登记、或仍有归属冲突的 collection 会直接报错；绝不猜测应该放扩展还是 PWA。
 */
(function (root, factory) {
  var api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.BWReaderRuntime = root.BWReaderRuntime || {};
  root.BWReaderRuntime.storageRouter = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function (root) {
  'use strict';

  var CONTRACT = 'storage-router/1';
  var registryApi = root.BWReaderRuntime && root.BWReaderRuntime.dataRegistry;
  if (!registryApi && typeof module === 'object' && module.exports && typeof require === 'function') {
    registryApi = require('./data-registry.js');
  }
  /*
   * 这里不再维护第二份 collection 默认表。浏览器若漏载 data-registry.js，
   * 新 StorageRouter 会明确失败；已经存在的 PWA/legacy UI 不会因此被替换或删除。
   */

  function clone(value) {
    if (value == null) return value;
    return JSON.parse(JSON.stringify(value));
  }
  function own(obj, key) { return Object.prototype.hasOwnProperty.call(obj || {}, key); }
  function ensureStore(store, label) {
    ['get', 'list', 'put', 'remove', 'changes', 'applyChanges', 'subscribe', 'status'].forEach(function (name) {
      if (!store || typeof store[name] !== 'function') {
        throw new RouterError(label + ' 缺少 DataStore.' + name, 'BW_ROUTER_STORE');
      }
    });
    return store;
  }

  function RouterError(message, code, details) {
    this.name = 'StorageRouterError';
    this.code = code || 'BW_ROUTER_ERROR';
    this.message = message;
    this.details = details || null;
    if (Error.captureStackTrace) Error.captureStackTrace(this, RouterError);
  }
  RouterError.prototype = Object.create(Error.prototype);
  RouterError.prototype.constructor = RouterError;

  function normalizeEntry(input, fallback) {
    if (typeof input === 'string') input = { scope: input, status: 'ready' };
    fallback = fallback || {};
    input = input || {};
    var scope = String(input.scope || fallback.scope || '');
    var status = String(input.status || fallback.status || 'ready');
    var provider = own(input, 'provider') ? input.provider === true : fallback.provider === true;
    var derived = own(input, 'derived') ? input.derived === true : fallback.derived === true;
    var conflictPolicy = String(input.conflictPolicy || fallback.conflictPolicy || (derived ? 'regenerate' : 'explicit'));
    if (['global', 'document', 'device'].indexOf(scope) < 0) {
      throw new RouterError('未知存储范围：' + scope, 'BW_ROUTER_SCOPE');
    }
    if (['ready', 'pending'].indexOf(status) < 0) {
      throw new RouterError('未知归属状态：' + status, 'BW_ROUTER_STATUS');
    }
    if (provider && (scope !== 'global' || status !== 'ready')) {
      throw new RouterError('provider collection 必须是 ready global：' + scope + '/' + status, 'BW_ROUTER_PROVIDER');
    }
    return {
      scope: scope,
      status: status,
      provider: provider,
      conflictPolicy: conflictPolicy,
      derived: derived,
      reason: String(input.reason || fallback.reason || '')
    };
  }

  function createStorageRouter(options) {
    options = options || {};
    var authority = options.dataRegistryApi || registryApi;
    if (
      !authority ||
      authority.CONTRACT !== 'data-registry/1' ||
      typeof authority.scopes !== 'function' ||
      typeof authority.collection !== 'function'
    ) {
      throw new RouterError(
        '缺少 DataRegistry，不能猜测 collection 归属',
        'BW_ROUTER_REGISTRY'
      );
    }
    var authorityScopes = authority.scopes();
    if (!authorityScopes || typeof authorityScopes !== 'object' || Array.isArray(authorityScopes)) {
      throw new RouterError('DataRegistry.scopes() 返回无效', 'BW_ROUTER_REGISTRY');
    }
    var deviceStore = options.deviceStore;
    if (!deviceStore && options.allowDeviceDocumentAlias === true) {
      deviceStore = options.documentStore;
    }
    if (!deviceStore) {
      throw new RouterError(
        'deviceStore 必须显式提供；如确需与 documentStore 共库，必须显式设置 allowDeviceDocumentAlias',
        'BW_ROUTER_DEVICE_STORE'
      );
    }
    var stores = {
      global: ensureStore(options.globalStore, 'globalStore'),
      document: ensureStore(options.documentStore, 'documentStore'),
      device: ensureStore(deviceStore, 'deviceStore')
    };
    var registry = {};
    var subscriptions = [];
    var source = options.scopes || authorityScopes;
    Object.keys(source).forEach(function (collection) {
      var authoritative = authority.collection(collection);
      if (!authoritative || !own(authorityScopes, collection)) {
        throw new RouterError(
          'collection 不在 DataRegistry 白名单：' + collection,
          'BW_ROUTER_UNREGISTERED',
          { collection: collection }
        );
      }
      var expected = normalizeEntry(authoritative);
      var requested = normalizeEntry(source[collection], expected);
      [
        'scope', 'status', 'provider', 'derived', 'conflictPolicy'
      ].forEach(function (field) {
        if (requested[field] !== expected[field]) {
          throw new RouterError(
            'collection 归属与 DataRegistry 不一致：' + collection,
            'BW_ROUTER_REGISTRY_MISMATCH',
            {
              collection: collection,
              field: field,
              expected: expected[field],
              actual: requested[field]
            }
          );
        }
      });
      registry[collection] = expected;
    });

    function register(collection, entry) {
      collection = String(collection || '').trim();
      if (!collection) throw new RouterError('collection 不能为空', 'BW_ROUTER_COLLECTION');
      var authoritative = authority.collection(collection);
      if (!authoritative || !own(authorityScopes, collection)) {
        throw new RouterError(
          'collection 不在 DataRegistry 白名单：' + collection,
          'BW_ROUTER_UNREGISTERED',
          { collection: collection }
        );
      }
      var expected = normalizeEntry(authoritative);
      var requested = normalizeEntry(entry, expected);
      [
        'scope', 'status', 'provider', 'derived', 'conflictPolicy'
      ].forEach(function (field) {
        if (requested[field] !== expected[field]) {
          throw new RouterError(
            'register 不能覆盖 DataRegistry 归属：' + collection,
            'BW_ROUTER_REGISTRY_MISMATCH',
            {
              collection: collection,
              field: field,
              expected: expected[field],
              actual: requested[field]
            }
          );
        }
      });
      registry[collection] = expected;
      return clone(registry[collection]);
    }
    function describe(collection) {
      collection = String(collection || '').trim();
      if (!own(registry, collection)) {
        throw new RouterError('collection 尚未登记归属：' + collection, 'BW_ROUTER_UNREGISTERED', {
          collection: collection
        });
      }
      var entry = registry[collection];
      if (entry.status === 'pending') {
        throw new RouterError('collection 归属仍待确认：' + collection, 'BW_ROUTER_PENDING', {
          collection: collection,
          reason: entry.reason
        });
      }
      return clone(entry);
    }
    function storeFor(collection) {
      var entry = describe(collection);
      return stores[entry.scope];
    }
    function bindSubscription(subscription, store) {
      var candidate = store;
      var unsubscribe = candidate.subscribe({ collection: subscription.collection }, function (change) {
        if (stores[subscription.scope] !== candidate || !subscription.active) return;
        subscription.listener(change);
      });
      return typeof unsubscribe === 'function' ? unsubscribe : function () {};
    }
    function setStore(scope, store) {
      scope = String(scope || '');
      if (!own(stores, scope)) throw new RouterError('未知存储范围：' + scope, 'BW_ROUTER_SCOPE');
      var nextStore = ensureStore(store, scope + 'Store');
      if (stores[scope] === nextStore) return true;
      var affected = subscriptions.filter(function (subscription) {
        return subscription.active && subscription.scope === scope;
      });
      var replacements = [];
      try {
        affected.forEach(function (subscription) {
          replacements.push({
            subscription: subscription,
            unsubscribe: bindSubscription(subscription, nextStore)
          });
        });
      } catch (error) {
        replacements.forEach(function (replacement) {
          try { replacement.unsubscribe(); } catch (_error) {}
        });
        throw error;
      }
      stores[scope] = nextStore;
      replacements.forEach(function (replacement) {
        try { replacement.subscription.unsubscribe(); } catch (_error) {}
        replacement.subscription.unsubscribe = replacement.unsubscribe;
      });
      return true;
    }

    return {
      contract: CONTRACT,
      register: register,
      describe: describe,
      scopes: function () { return clone(registry); },
      setGlobalStore: function (store) { return setStore('global', store); },
      setDocumentStore: function (store) { return setStore('document', store); },
      setDeviceStore: function (store) { return setStore('device', store); },
      storeFor: storeFor,
      get: function (collection, id, opts) { return storeFor(collection).get(collection, id, opts); },
      list: function (collection, query) { return storeFor(collection).list(collection, query); },
      put: function (collection, value, opts) { return storeFor(collection).put(collection, value, opts); },
      remove: function (collection, id, opts) { return storeFor(collection).remove(collection, id, opts); },
      batch: function (mutations) {
        if (!Array.isArray(mutations)) {
          return Promise.reject(new RouterError(
            'batch mutations 必须是数组', 'BW_ROUTER_BATCH'
          ));
        }
        if (!mutations.length) return Promise.resolve([]);
        var scope = '';
        var target = null;
        try {
          mutations.forEach(function (mutation, index) {
            if (!mutation || typeof mutation !== 'object' || Array.isArray(mutation)) {
              throw new RouterError(
                'batch mutation 必须是对象', 'BW_ROUTER_BATCH', { index: index }
              );
            }
            var collection = String(mutation.collection || '').trim();
            if (!collection) {
              throw new RouterError(
                'batch mutation 缺少 collection', 'BW_ROUTER_COLLECTION', { index: index }
              );
            }
            var entry = describe(collection);
            if (!scope) {
              scope = entry.scope;
              target = stores[scope];
            } else if (entry.scope !== scope) {
              throw new RouterError(
                'batch 不能跨存储范围；原子写入必须属于同一 scope',
                'BW_ROUTER_BATCH_SCOPE',
                { index: index, expected: scope, actual: entry.scope }
              );
            }
          });
          if (!target || typeof target.batch !== 'function') {
            throw new RouterError(
              scope + 'Store 缺少 DataStore.batch', 'BW_ROUTER_STORE'
            );
          }
        } catch (error) {
          return Promise.reject(error);
        }
        return target.batch(mutations);
      },
      subscribe: function (collection, listener) {
        if (typeof listener !== 'function') {
          throw new RouterError('subscribe listener 必须是函数', 'BW_ROUTER_LISTENER');
        }
        var entry = describe(collection);
        var subscription = {
          collection: String(collection),
          scope: entry.scope,
          listener: listener,
          unsubscribe: function () {},
          active: true
        };
        subscription.unsubscribe = bindSubscription(subscription, stores[entry.scope]);
        subscriptions.push(subscription);
        return function () {
          if (!subscription.active) return;
          subscription.active = false;
          var index = subscriptions.indexOf(subscription);
          if (index >= 0) subscriptions.splice(index, 1);
          try { subscription.unsubscribe(); } catch (_error) {}
          subscription.unsubscribe = function () {};
        };
      },
      changes: function (scope, query) {
        scope = String(scope || '');
        if (!own(stores, scope)) return Promise.reject(new RouterError('changes 必须明确 global/document/device', 'BW_ROUTER_SCOPE'));
        return stores[scope].changes(query || {});
      },
      applyChanges: function (scope, changes, opts) {
        scope = String(scope || '');
        if (!own(stores, scope)) return Promise.reject(new RouterError('applyChanges 必须明确 global/document/device', 'BW_ROUTER_SCOPE'));
        if (!Array.isArray(changes)) {
          return Promise.reject(new RouterError('applyChanges changes 必须是数组', 'BW_ROUTER_CHANGES'));
        }
        try {
          changes.forEach(function (change, index) {
            change = change || {};
            var declaredCollection = String(change.collection || '').trim();
            var recordCollection = String(change.record && change.record.collection || '').trim();
            if (declaredCollection && recordCollection && declaredCollection !== recordCollection) {
              throw new RouterError('change 与 record 的 collection 不一致', 'BW_ROUTER_COLLECTION_MISMATCH', {
                index: index,
                collection: declaredCollection,
                recordCollection: recordCollection
              });
            }
            var collection = declaredCollection || recordCollection;
            if (!collection) {
              throw new RouterError('change 缺少 collection', 'BW_ROUTER_COLLECTION', { index: index });
            }
            var entry = describe(collection);
            if (entry.scope !== scope) {
              throw new RouterError('change collection 不属于指定 scope：' + collection, 'BW_ROUTER_SCOPE_MISMATCH', {
                index: index,
                collection: collection,
                expected: entry.scope,
                actual: scope
              });
            }
          });
        } catch (error) {
          return Promise.reject(error);
        }
        return stores[scope].applyChanges(changes, opts);
      },
      status: function () {
        return Promise.all([
          stores.global.status(),
          stores.document.status(),
          stores.device.status()
        ]).then(function (items) {
          return {
            contract: CONTRACT,
            global: items[0],
            document: items[1],
            device: items[2]
          };
        });
      }
    };
  }

  return {
    CONTRACT: CONTRACT,
    RouterError: RouterError,
    createStorageRouter: createStorageRouter
  };
});
