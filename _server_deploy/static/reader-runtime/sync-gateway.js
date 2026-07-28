/* sync-gateway.js — 跨设备变化中转契约。
 *
 * Gateway 只传递 DataStore change，不解释卡片、锚点、UI 或阅读器几何。
 * 离线/服务器错误必须可重试，调用方据此保留本地 journal。
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.BWReaderRuntime = root.BWReaderRuntime || {};
  root.BWReaderRuntime.syncGateway = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var CONTRACT = 'sync-gateway/2';

  function clone(value) {
    if (value == null) return value;
    return JSON.parse(JSON.stringify(value));
  }
  function safe(value, label) {
    value = String(value || '').trim();
    if (!value) throw new SyncError(label + ' 不能为空', 'BW_SYNC_INVALID', false);
    return value;
  }

  function SyncError(message, code, retryable, details) {
    this.name = 'SyncGatewayError';
    this.code = code || 'BW_SYNC_ERROR';
    this.retryable = !!retryable;
    this.message = message;
    this.details = details || null;
    if (Error.captureStackTrace) Error.captureStackTrace(this, SyncError);
  }
  SyncError.prototype = Object.create(Error.prototype);
  SyncError.prototype.constructor = SyncError;

  function normalizeResult(input, fallbackCursor) {
    input = input || {};
    var fallback = Math.max(0, Number(fallbackCursor) || 0);
    var hasCursor = input.cursor != null && input.cursor !== '';
    var receivedCursor = Math.max(0, Number(input.cursor) || 0);
    var cursorBehind = hasCursor && receivedCursor < fallback;
    var cursor = Math.max(fallback, receivedCursor);
    var headCursor = Math.max(cursor, Number(input.headCursor) || 0);
    return {
      contract: CONTRACT,
      cursor: cursor,
      headCursor: headCursor,
      oldestCursor: Math.max(0, Number(input.oldestCursor) || 0),
      resetRequired: input.resetRequired === true || cursorBehind,
      hasMore: input.hasMore === true || cursor < headCursor,
      ackedMutationIds: Array.isArray(input.ackedMutationIds) ? clone(input.ackedMutationIds) : [],
      changes: Array.isArray(input.changes) ? clone(input.changes) : [],
      conflicts: Array.isArray(input.conflicts) ? clone(input.conflicts) : []
    };
  }

  function normalizeSnapshot(input) {
    input = input || {};
    return {
      contract: CONTRACT,
      snapshotId: safe(input.snapshotId, 'snapshotId'),
      snapshotCursor: Math.max(0, Number(input.snapshotCursor) || 0),
      offset: Math.max(0, Number(input.offset) || 0),
      nextOffset: Math.max(0, Number(input.nextOffset) || 0),
      hasMore: input.hasMore === true,
      changes: Array.isArray(input.changes) ? clone(input.changes) : []
    };
  }

  function wrapTransportError(error) {
    if (error && error.name === 'SyncGatewayError') return error;
    var status = Number(error && error.status || 0);
    var retryable = !status || status === 408 || status === 429 || status >= 500;
    return new SyncError(
      String(error && error.message || error || '同步失败'),
      retryable ? 'BW_SYNC_RETRYABLE' : 'BW_SYNC_REJECTED',
      retryable,
      { status: status || null }
    );
  }

  function createSyncGateway(options) {
    options = options || {};
    var transport = options.transport || {};
    var deviceId = safe(options.deviceId || 'local-device', 'deviceId');
    if (typeof transport.exchange !== 'function' &&
        (typeof transport.push !== 'function' || typeof transport.pull !== 'function')) {
      throw new SyncError('transport 必须实现 exchange，或同时实现 push/pull', 'BW_SYNC_TRANSPORT', false);
    }

    function exchange(direction, payload) {
      payload = payload || {};
      var request = {
        contract: CONTRACT,
        direction: direction,
        deviceId: deviceId,
        cursor: Math.max(0, Number(payload.cursor) || 0),
        limit: Math.max(1, Math.min(2000, Number(payload.limit) || 500)),
        changes: Array.isArray(payload.changes) ? clone(payload.changes) : []
      };
      var fn = typeof transport.exchange === 'function'
        ? function () { return transport.exchange(clone(request)); }
        : function () { return transport[direction](clone(request)); };
      try {
        return Promise.resolve(fn()).then(function (result) {
          return normalizeResult(result, request.cursor);
        }, function (error) {
          return Promise.reject(wrapTransportError(error));
        });
      } catch (error) {
        return Promise.reject(wrapTransportError(error));
      }
    }

    return {
      contract: CONTRACT,
      deviceId: deviceId,
      push: function (payload) { return exchange('push', payload || {}); },
      pull: function (payload) { return exchange('pull', payload || {}); },
      snapshot: function (payload) {
        if (typeof transport.snapshot !== 'function') {
          return Promise.reject(new SyncError(
            'transport 不支持完整快照对账',
            'BW_SYNC_SNAPSHOT_UNAVAILABLE',
            false
          ));
        }
        payload = payload || {};
        var request = {
          contract: CONTRACT,
          deviceId: deviceId,
          snapshotId: String(payload.snapshotId || ''),
          offset: Math.max(0, Number(payload.offset) || 0),
          limit: Math.max(1, Math.min(2000, Number(payload.limit) || 500))
        };
        try {
          return Promise.resolve(transport.snapshot(clone(request))).then(
            normalizeSnapshot,
            function (error) { return Promise.reject(wrapTransportError(error)); }
          );
        } catch (error) {
          return Promise.reject(wrapTransportError(error));
        }
      },
      status: function () {
        if (typeof transport.status !== 'function') {
          return Promise.resolve({ contract: CONTRACT, state: 'ready', deviceId: deviceId });
        }
        try {
          return Promise.resolve(transport.status()).then(function (status) {
            status = clone(status || {});
            status.contract = CONTRACT;
            status.deviceId = deviceId;
            return status;
          });
        } catch (error) {
          return Promise.reject(wrapTransportError(error));
        }
      }
    };
  }

  function createOfflineSyncGateway(options) {
    options = options || {};
    var deviceId = String(options.deviceId || 'local-device');
    function offline() {
      return Promise.reject(new SyncError(
        String(options.reason || '当前离线，本地变更已保留'),
        'BW_SYNC_OFFLINE',
        true
      ));
    }
    return {
      contract: CONTRACT,
      deviceId: deviceId,
      push: offline,
      pull: offline,
      status: function () {
        return Promise.resolve({
          contract: CONTRACT,
          state: 'offline',
          deviceId: deviceId,
          retryable: true,
          reason: String(options.reason || 'offline')
        });
      }
    };
  }

  function syncOnce(options) {
    options = options || {};
    var store = options.store;
    var gateway = options.gateway;
    if (!store || typeof store.changes !== 'function' || typeof store.applyChanges !== 'function') {
      return Promise.reject(new SyncError('store 不符合 DataStore 契约', 'BW_SYNC_STORE', false));
    }
    if (!gateway || typeof gateway.push !== 'function' || typeof gateway.pull !== 'function') {
      return Promise.reject(new SyncError('gateway 不符合 SyncGateway 契约', 'BW_SYNC_GATEWAY', false));
    }
    var pullCursor = Math.max(0, Number(options.cursor) || 0);
    var afterLocal = Math.max(0, Number(options.afterLocal) || 0);
    var localBatch;
    function stopForGap(phase, batch, requestedCursor) {
      return Promise.reject(new SyncError(
        '增量日志已出现缺口，必须先执行完整对账，未继续同步',
        'BW_SYNC_RESET_REQUIRED',
        false,
        {
          phase: phase,
          requestedCursor: Math.max(0, Number(requestedCursor) || 0),
          oldestCursor: Math.max(0, Number(batch && batch.oldestCursor) || 0),
          currentCursor: Math.max(0, Number(batch && batch.cursor) || 0)
        }
      ));
    }
    function acknowledgedLocalCursor(batch, pushed) {
      var acknowledged = {};
      (pushed.ackedMutationIds || []).forEach(function (mutationId) {
        acknowledged[String(mutationId)] = true;
      });
      var cursor = afterLocal;
      var ordered = (batch.changes || []).slice().sort(function (left, right) {
        return (Number(left.cursor) || 0) - (Number(right.cursor) || 0);
      });
      for (var index = 0; index < ordered.length; index += 1) {
        var change = ordered[index];
        var changeCursor = Math.max(0, Number(change.cursor) || 0);
        if (changeCursor <= cursor) continue;
        if (!acknowledged[String(change.mutationId || '')]) break;
        cursor = changeCursor;
      }
      return cursor;
    }
    function pullAll(cursor, page, summary) {
      if (page > 100) {
        return Promise.reject(new SyncError(
          '远端分页超过安全上限',
          'BW_SYNC_PAGINATION',
          true,
          { cursor: cursor }
        ));
      }
      return gateway.pull({ cursor: cursor, limit: options.limit }).then(function (pulled) {
        if (pulled.resetRequired) {
          return stopForGap('remote-pull', pulled, cursor);
        }
        if (pulled.hasMore && pulled.cursor <= cursor) {
          return Promise.reject(new SyncError(
            '远端分页未推进游标',
            'BW_SYNC_PAGINATION',
            true,
            { cursor: cursor, headCursor: pulled.headCursor }
          ));
        }
        return store.applyChanges(
          pulled.changes,
          { journal: options.remoteJournal === true }
        ).then(function (applied) {
          summary.cursor = pulled.cursor;
          summary.headCursor = pulled.headCursor;
          summary.applied += Math.max(0, Number(applied.applied) || 0);
          summary.conflicts = summary.conflicts.concat(
            pulled.conflicts || [],
            applied.conflicts || []
          );
          if (pulled.hasMore) {
            return pullAll(pulled.cursor, page + 1, summary);
          }
          return summary;
        });
      });
    }
    return store.changes({ after: afterLocal, limit: Number(options.limit) || 500 })
      .then(function (batch) {
        localBatch = batch;
        if (batch.resetRequired) {
          return stopForGap('local-journal', batch, options.afterLocal);
        }
        return gateway.push({ cursor: pullCursor, changes: batch.changes, limit: options.limit });
      })
      .then(function (pushed) {
        if (pushed.resetRequired) {
          return stopForGap('remote-push', pushed, pullCursor);
        }
        // push 返回的游标包含本次上传带来的推进，也可能已经越过调用方尚未
        // 拉取的远端变化。pull 必须从本轮开始前的远端游标继续；拉到自己刚
        // push 的变化没有副作用，DataStore 会按 mutationId 幂等跳过。
        var localCursor = acknowledgedLocalCursor(localBatch, pushed);
        var localHead = Math.max(
          afterLocal,
          Number(localBatch.cursor) || 0,
          Number(localBatch.nextCursor) || 0
        );
        return pullAll(pullCursor, 0, {
          cursor: pullCursor,
          headCursor: pullCursor,
          applied: 0,
          conflicts: (pushed.conflicts || []).slice()
        }).then(function (remote) {
          return {
            contract: CONTRACT,
            cursor: remote.cursor,
            headCursor: remote.headCursor,
            localCursor: localCursor,
            localHasMore: localCursor < localHead || !!localBatch.hasMore,
            ackedMutationIds: pushed.ackedMutationIds,
            applied: remote.applied,
            conflicts: remote.conflicts
          };
        });
      });
  }

  return {
    CONTRACT: CONTRACT,
    SyncError: SyncError,
    createSyncGateway: createSyncGateway,
    createOfflineSyncGateway: createOfflineSyncGateway,
    syncOnce: syncOnce
  };
});
