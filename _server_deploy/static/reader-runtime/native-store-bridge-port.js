/* native-store-bridge-port.js — `native-store.js` 的 port，接到 App 的消息通道上。
 *
 * 一句话：把 port 的每个动作翻成一条 `bwNativeDataStore` 消息。
 *
 * ⚠ **这里不做判据、也不做缓存**。它是一根管子：
 *   · 加判据 → 同一次写入会有两个答案（另一个在 data-store.js 里）；
 *   · 加缓存 → 「我刚写的东西读回来还是旧的」这类问题会从这里开始，
 *     而且极难查，因为上下两层看起来都对。
 *
 * ⚠ 记录以 **JSON 字符串**在两边之间走。原生那侧原样存、原样还，中间不解析 ——
 *   解析就意味着要理解它，也就离"在这里加判据"只差一步。
 */
(function (root, factory) {
  var api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.BWReaderRuntime = root.BWReaderRuntime || {};
  root.BWReaderRuntime.nativeStoreBridgePort = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function (root) {
  'use strict';

  var MESSAGE = 'bwNativeDataStore';

  /** App 里才有这条通道；没有就该退回 IndexedDB，而不是假装成功。 */
  function available() {
    try {
      var handlers = root.webkit && root.webkit.messageHandlers;
      return !!(handlers && handlers[MESSAGE] && typeof handlers[MESSAGE].postMessage === 'function');
    } catch (_) { return false; }
  }

  function conflict() {
    var error = new Error('记录已被改过');
    error.name = 'ConflictError';
    error.code = 'BW_DATA_CONFLICT';
    return error;
  }

  function createBridgePort(options) {
    options = options || {};
    var storeName = String(options.store || '');
    if (!storeName) throw new Error('native-store port 需要库名');

    function call(action, payload) {
      if (!available()) {
        return Promise.reject(new Error('BW_NATIVE_DATA_STORE_UNAVAILABLE'));
      }
      var request = { store: storeName, action: action };
      Object.keys(payload || {}).forEach(function (key) { request[key] = payload[key]; });
      return root.webkit.messageHandlers[MESSAGE].postMessage(request).then(function (reply) {
        if (reply && reply.ok === false && reply.code === 'BW_DATA_CONFLICT') throw conflict();
        if (!reply || reply.ok !== true) {
          var error = new Error((reply && (reply.error || reply.code)) || 'BW_NATIVE_DATA_STORE_FAILED');
          error.code = (reply && reply.code) || 'BW_NATIVE_DATA_STORE_FAILED';
          throw error;
        }
        return reply;
      });
    }

    /** 原生给回来的是 JSON 串（或 null）。⚠ 解析失败**不要**当成"没有这条"：
     *  那会让调用方以为记录不存在，于是新建一条把原来的覆盖掉。 */
    function parse(text, what) {
      if (text == null) return null;
      try { return JSON.parse(text); }
      catch (_) { throw new Error('BW_NATIVE_DATA_STORE_CORRUPT:' + (what || '')); }
    }

    return {
      kind: 'native-bridge',
      store: storeName,
      preferenceCall: ['bw-reader-native-v1-global', 'bw-reader-native-v1-device'].indexOf(storeName) >= 0 ? function (input, deviceId) {
        return call('preference', { request: Object.assign({}, input, { deviceID: deviceId }) });
      } : undefined,
      cardRepositoryCall: storeName === 'bw-reader-native-v1-global' ? function (operation, args, deviceId) {
        var optionIndex = operation === 'patchState' ? 3 : operation === 'recordAnkiReceipt' ? 4 :
          ['registerDraft', 'saveConfirmedCard', 'tombstone', 'importLegacyBatch', 'commitReview'].indexOf(operation) >= 0 ? 1 : 2;
        var options = args[optionIndex] || {};
        var mutationId = options.mutationId != null ? String(options.mutationId).trim() :
          ('native-card:' + Date.now().toString(36) + ':' + root.crypto.randomUUID());
        return call('cardRepository', { request: {
          operation: operation, arguments: args, mutationId: mutationId, deviceID: deviceId
        } });
      } : undefined,

      read: function (collection, id) {
        return call('read', { collection: collection, id: id }).then(function (reply) {
          return parse(reply.record, collection + '/' + id);
        });
      },

      readMany: function (keys) {
        if (!keys || !keys.length) return Promise.resolve([]);
        return call('readMany', { keys: keys }).then(function (reply) {
          return (reply.records || []).map(function (item, index) {
            return parse(item, (keys[index] || {}).collection || '');
          });
        });
      },

      listCollection: function (collection, listOptions) {
        listOptions = listOptions || {};
        var payload = { collection: collection, includeDeleted: !!listOptions.includeDeleted };
        // limit 缺席＝整集合（调用方带了下推不了的条件）。这里**不补默认值**：
        // 补一个会把"我要全部"悄悄变成"我要前 200 条"。
        if (listOptions.limit != null) payload.limit = listOptions.limit;
        if (listOptions.offset != null) payload.offset = listOptions.offset;
        return call('listCollection', payload).then(function (reply) {
          return (reply.records || []).map(function (item) { return parse(item, collection); });
        });
      },

      remembered: function (mutationId) {
        return call('remembered', { mutationId: mutationId }).then(function (reply) {
          return parse(reply.record, mutationId);
        });
      },

      commit: function (entries) {
        return call('commit', { entries: entries }).then(function (reply) {
          return { cursors: reply.cursors || [] };
        });
      },

      journal: function (query) {
        query = query || {};
        return call('journal', { after: query.after || 0, limit: query.limit || 500 })
          .then(function (reply) {
            return {
              items: (reply.items || []).map(function (item) { return parse(item, 'journal'); }),
              cursor: reply.cursor || 0,
              oldestCursor: reply.oldestCursor
            };
          });
      },

      meta: function (key) {
        return call('meta', { key: key }).then(function (reply) {
          return reply.value == null ? null : String(reply.value);
        });
      },

      putMeta: function (key, value) {
        return call('putMeta', { key: key, value: String(value) }).then(function () { return true; });
      },

      info: function () {
        return call('info', {}).then(function (reply) {
          return {
            cursor: reply.cursor || 0,
            journalSize: reply.journalSize || 0,
            collections: reply.collections || []
          };
        });
      }
    };
  }

  return { MESSAGE: MESSAGE, available: available, createBridgePort: createBridgePort };
});
