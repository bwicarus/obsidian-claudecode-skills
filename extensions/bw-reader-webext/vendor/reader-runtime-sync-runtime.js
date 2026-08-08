/* sync-runtime.js — shared lifecycle for PWA and extension synchronization.
 *
 * The coordinator owns protocol/cursors. This module only owns scheduling:
 * local writes, online recovery, bounded retries and an explicit pause fence.
 * A paused owner can never keep applying remote data after ownership moved.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.BWReaderRuntime = root.BWReaderRuntime || {};
  root.BWReaderRuntime.syncRuntime = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var CONTRACT = 'sync-runtime/1';

  function clone(value) {
    if (value == null) return value;
    return JSON.parse(JSON.stringify(value));
  }
  function RuntimeError(message, code, retryable, details) {
    this.name = 'SyncRuntimeError';
    this.message = String(message || '同步运行时失败');
    this.code = code || 'BW_SYNC_RUNTIME';
    this.retryable = !!retryable;
    this.details = details || null;
    if (Error.captureStackTrace) Error.captureStackTrace(this, RuntimeError);
  }
  RuntimeError.prototype = Object.create(Error.prototype);
  RuntimeError.prototype.constructor = RuntimeError;

  function createSyncRuntime(options) {
    options = options || {};
    var coordinatorApi = options.coordinatorApi;
    var store = options.store;
    var registry = options.registry;
    var setTimer = options.setTimeout || (
      typeof setTimeout === 'function' ? setTimeout : null
    );
    var clearTimer = options.clearTimeout || (
      typeof clearTimeout === 'function' ? clearTimeout : null
    );
    var externalFence = typeof options.assertLease === 'function'
      ? options.assertLease
      : function () { return true; };
    var onResult = typeof options.onResult === 'function'
      ? options.onResult
      : function () {};
    var onlineTarget = options.onlineTarget || null;
    var intervalMs = Math.max(5000, Number(options.intervalMs) || 60000);
    var debounceMs = Math.max(0, Number(options.debounceMs) || 200);
    var retryMinMs = Math.max(1000, Number(options.retryMinMs) || 5000);
    var retryMaxMs = Math.max(retryMinMs, Number(options.retryMaxMs) || 120000);
    var manualOnly = options.manualOnly === true;
    var paused = true;
    var destroyed = false;
    var generation = 0;
    var timer = null;
    var unsubscribe = null;
    var onlineListener = null;
    var running = null;
    var retryMs = retryMinMs;
    var lastResult = null;
    var lastError = null;
    var lastReason = '';
    var pauseReason = 'initial';
    var coordinator;

    if (
      !coordinatorApi ||
      coordinatorApi.CONTRACT !== 'sync-coordinator/1' ||
      typeof coordinatorApi.createSyncCoordinator !== 'function'
    ) {
      throw new RuntimeError(
        '缺少 sync-coordinator/1',
        'BW_SYNC_RUNTIME_DEPENDENCY',
        false
      );
    }
    if (
      !store ||
      typeof store.subscribe !== 'function' ||
      typeof store.changes !== 'function' ||
      typeof store.applyChanges !== 'function'
    ) {
      throw new RuntimeError('store 不符合 DataStore 契约', 'BW_SYNC_RUNTIME_STORE', false);
    }
    if (
      !registry ||
      registry.CONTRACT !== 'data-registry/1' ||
      typeof registry.isSyncCollection !== 'function'
    ) {
      throw new RuntimeError('DataRegistry 无效', 'BW_SYNC_RUNTIME_REGISTRY', false);
    }
    if (!setTimer || !clearTimer) {
      throw new RuntimeError('宿主缺少定时器', 'BW_SYNC_RUNTIME_TIMER', false);
    }

    function assertActive(expectedGeneration) {
      if (
        destroyed ||
        paused ||
        (expectedGeneration != null && expectedGeneration !== generation)
      ) {
        throw new RuntimeError(
          destroyed ? '同步运行时已销毁' : '同步所有权已暂停或变更',
          'BW_SYNC_OWNER_INACTIVE',
          false,
          { generation: generation }
        );
      }
      return Promise.resolve(externalFence()).then(function (valid) {
        if (valid === false) {
          throw new RuntimeError(
            '账户 lease 已失效',
            'BW_SYNC_LEASE_STALE',
            false
          );
        }
        if (
          destroyed ||
          paused ||
          (expectedGeneration != null && expectedGeneration !== generation)
        ) {
          throw new RuntimeError(
            '同步所有权已暂停或变更',
            'BW_SYNC_OWNER_INACTIVE',
            false,
            { generation: generation }
          );
        }
        return true;
      });
    }

    coordinator = coordinatorApi.createSyncCoordinator({
      store: store,
      registry: registry,
      serverGateway: options.serverGateway,
      checkpointStore: options.checkpointStore,
      limit: options.limit,
      assertLease: function () { return assertActive(generation); }
    });

    function clearScheduled() {
      if (timer != null) clearTimer(timer);
      timer = null;
    }
    function syncCollection(change) {
      return String(change && (
        change.collection ||
        change.record && change.record.collection
      ) || '');
    }
    function resultNeedsRetry(result) {
      if (!result || !result.server) return true;
      if (result.server.ok !== true) return result.server.retryable !== false;
      if (result.server.pendingLocal) return true;
      return Object.keys(result.direct || {}).some(function (peerId) {
        var peer = result.direct[peerId] || {};
        return peer.ok === true && peer.pendingLocal === true ||
          peer.ok === false && peer.retryable !== false && !peer.skipped;
      });
    }
    function resultHasBlockingConflict(result) {
      if (!result || !result.server) return false;
      /* Only the durable server lane is authoritative enough to block the
       * owner. A transient direct peer can race, disappear or be rebuilt from
       * the next durable baseline; letting it pause the server backup would
       * turn an optional fast lane into a data-durability outage. */
      return Array.isArray(result.server.conflicts) &&
        result.server.conflicts.length > 0;
    }
    function schedule(reason, delayMs) {
      if (destroyed || paused || manualOnly) return false;
      lastReason = String(reason || 'scheduled');
      clearScheduled();
      var scheduledGeneration = generation;
      timer = setTimer(function () {
        timer = null;
        if (
          destroyed ||
          paused ||
          scheduledGeneration !== generation
        ) return;
        runNow(lastReason).catch(function () {});
      }, Math.max(0, Number(delayMs) || 0));
      return true;
    }
    function runNow(reason) {
      if (destroyed || paused) {
        return Promise.resolve({
          contract: CONTRACT,
          skipped: true,
          code: destroyed ? 'BW_SYNC_RUNTIME_DESTROYED' : 'BW_SYNC_OWNER_INACTIVE'
        });
      }
      if (running) return running;
      clearScheduled();
      var runGeneration = generation;
      lastReason = String(reason || 'manual');
      running = assertActive(runGeneration).then(function () {
        return coordinator.runOnce();
      }).then(function (result) {
        return assertActive(runGeneration).then(function () {
          lastResult = clone(result);
          lastError = null;
          retryMs = retryMinMs;
          try { onResult(clone(result), null, lastReason); } catch (_) {}
          if (resultHasBlockingConflict(result)) {
            /* A conflict needs an explicit user/policy decision. Replaying the
             * same pages on a timer only burns bandwidth and can conceal the
             * unresolved branch behind a later successful poll. */
            pause('sync-conflict');
          } else if (resultNeedsRetry(result)) {
            var delay = result && result.server && result.server.ok === true
              ? debounceMs
              : retryMs;
            if (!(result && result.server && result.server.ok === true)) {
              retryMs = Math.min(retryMaxMs, retryMs * 2);
            }
            schedule('continue:' + lastReason, delay);
          } else {
            schedule('interval', intervalMs);
          }
          return result;
        });
      }).catch(function (error) {
        var errorCode = String(error && error.code || 'BW_SYNC_RUNTIME');
        lastError = {
          code: /^[A-Z][A-Z0-9_]{0,79}$/.test(errorCode)
            ? errorCode
            : 'BW_SYNC_RUNTIME',
          retryable: error && error.retryable !== false
        };
        try { onResult(null, error, lastReason); } catch (_) {}
        if (
          !destroyed &&
          !paused &&
          runGeneration === generation &&
          error &&
          error.retryable !== false
        ) {
          schedule('retry:' + lastReason, retryMs);
          retryMs = Math.min(retryMaxMs, retryMs * 2);
        }
        throw error;
      }).finally(function () {
        running = null;
      });
      return running;
    }
    function start(reason) {
      if (destroyed) {
        throw new RuntimeError('同步运行时已销毁', 'BW_SYNC_RUNTIME_DESTROYED', false);
      }
      if (!paused) {
        schedule(reason || 'restart', debounceMs);
        return false;
      }
      if (pauseReason === 'sync-conflict') {
        /* Alarms, online events, provider lifecycle and worker restarts are
         * automatic callers. None of them may erase a conflict boundary. */
        return false;
      }
      paused = false;
      pauseReason = '';
      generation += 1;
      retryMs = retryMinMs;
      if (!unsubscribe) {
        unsubscribe = store.subscribe({}, function (change) {
          if (!registry.isSyncCollection(syncCollection(change))) return;
          /* Server imports use journal=false and are marked remote by both
           * in-memory and IndexedDB stores. They must not create a writeback
           * wakeup. Direct imports are journaled and therefore remain eligible. */
          if (change && change.remote === true) return;
          schedule('local-change', debounceMs);
        });
      }
      if (
        onlineTarget &&
        typeof onlineTarget.addEventListener === 'function' &&
        !onlineListener
      ) {
        onlineListener = function () { schedule('online', 0); };
        onlineTarget.addEventListener('online', onlineListener);
      }
      schedule(reason || 'start', 0);
      return true;
    }
    function pause(reason) {
      if (destroyed) return false;
      var changed = !paused;
      paused = true;
      generation += 1;
      lastReason = String(reason || 'paused');
      pauseReason = lastReason;
      clearScheduled();
      return changed;
    }
    function resolveConflict(reason) {
      if (
        destroyed ||
        !paused ||
        pauseReason !== 'sync-conflict'
      ) return false;
      pauseReason = 'sync-conflict-resolved';
      return start(reason || 'sync-conflict-resolved');
    }
    function destroy(reason) {
      if (destroyed) return false;
      pause(reason || 'destroyed');
      destroyed = true;
      if (unsubscribe) {
        try { unsubscribe(); } catch (_) {}
        unsubscribe = null;
      }
      if (
        onlineTarget &&
        onlineListener &&
        typeof onlineTarget.removeEventListener === 'function'
      ) {
        try { onlineTarget.removeEventListener('online', onlineListener); } catch (_) {}
      }
      onlineListener = null;
      return true;
    }

    return {
      contract: CONTRACT,
      coordinator: coordinator,
      start: start,
      resume: start,
      resolveConflict: resolveConflict,
      pause: pause,
      destroy: destroy,
      schedule: schedule,
      runNow: runNow,
      addPeer: function () {
        return coordinator.addPeer.apply(coordinator, arguments);
      },
      removePeer: function () {
        return coordinator.removePeer.apply(coordinator, arguments);
      },
      status: function () {
        return Promise.resolve().then(function () {
          return coordinator.status();
        }).then(function (status) {
          return {
            contract: CONTRACT,
            paused: paused,
            destroyed: destroyed,
            generation: generation,
            scheduled: timer != null,
            running: !!running,
            reason: lastReason,
            pauseReason: pauseReason,
            lastResult: clone(lastResult),
            lastError: clone(lastError),
            coordinator: status
          };
        }, function (error) {
          if (
            paused &&
            error &&
            (error.code === 'BW_SYNC_OWNER_INACTIVE' ||
             error.code === 'BW_SYNC_LEASE_STALE')
          ) {
            return {
              contract: CONTRACT,
              paused: paused,
              destroyed: destroyed,
              generation: generation,
              scheduled: timer != null,
              running: !!running,
              reason: lastReason,
              pauseReason: pauseReason,
              lastResult: clone(lastResult),
              lastError: clone(lastError),
              coordinator: null,
              coordinatorUnavailable: {
                code: String(error.code),
                retryable: false
              }
            };
          }
          throw error;
        });
      }
    };
  }

  return {
    CONTRACT: CONTRACT,
    RuntimeError: RuntimeError,
    createSyncRuntime: createSyncRuntime
  };
});
