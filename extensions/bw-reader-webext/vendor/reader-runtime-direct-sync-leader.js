/* direct-sync-leader.js — one PWA tab owns WebRTC for an account/device.
 *
 * Web Locks are the only supported election primitive. If the browser cannot
 * provide an exclusive lock, direct sync stays disabled while durable server
 * sync continues; this avoids two tabs publishing the same device endpoint.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.BWReaderRuntime = root.BWReaderRuntime || {};
  root.BWReaderRuntime.directSyncLeader = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var CONTRACT = 'direct-sync-leader/1';

  function LeaderError(message, code, retryable) {
    this.name = 'DirectSyncLeaderError';
    this.message = String(message || '直连宿主选举失败');
    this.code = String(code || 'BW_DIRECT_LEADER');
    this.retryable = !!retryable;
    if (Error.captureStackTrace) Error.captureStackTrace(this, LeaderError);
  }
  LeaderError.prototype = Object.create(Error.prototype);
  LeaderError.prototype.constructor = LeaderError;

  function createDirectSyncLeader(options) {
    options = options || {};
    var locks = options.locks;
    var host = options.host;
    var lockName = String(options.lockName || '').trim();
    var assertLease = typeof options.assertLease === 'function'
      ? options.assertLease
      : function () { return true; };
    var onStatus = typeof options.onStatus === 'function'
      ? options.onStatus
      : function () {};
    var setTimer = options.setTimeout || (
      typeof setTimeout === 'function' ? setTimeout : null
    );
    var clearTimer = options.clearTimeout || (
      typeof clearTimeout === 'function' ? clearTimeout : null
    );
    var retryMs = Math.max(1000, Number(options.retryMs) || 5000);
    var paused = true;
    var destroyed = false;
    var generation = 0;
    var requesting = false;
    var leader = false;
    var release = null;
    var timer = null;

    if (!host || typeof host.start !== 'function' ||
        typeof host.pause !== 'function' || typeof host.destroy !== 'function') {
      throw new LeaderError('直连 host 无效', 'BW_DIRECT_LEADER_DEPENDENCY', false);
    }
    if (!lockName || lockName.length > 240) {
      throw new LeaderError('lockName 无效', 'BW_DIRECT_LEADER_INVALID', false);
    }
    function emit(state, detail) {
      try {
        onStatus({
          contract: CONTRACT,
          state: state,
          detail: detail || {}
        });
      } catch (_) {}
    }
    function clearScheduled() {
      if (timer != null && clearTimer) clearTimer(timer);
      timer = null;
    }
    function schedule() {
      if (paused || destroyed || requesting || leader || !setTimer) return;
      clearScheduled();
      var expected = generation;
      timer = setTimer(function () {
        timer = null;
        if (!paused && !destroyed && expected === generation) acquire();
      }, retryMs);
    }
    function acquire() {
      if (paused || destroyed || requesting || leader) return Promise.resolve(false);
      if (!locks || typeof locks.request !== 'function') {
        emit('unavailable', { code: 'BW_DIRECT_LEADER_UNAVAILABLE' });
        return Promise.resolve(false);
      }
      requesting = true;
      var expected = generation;
      return Promise.resolve(locks.request(
        lockName,
        { mode: 'exclusive', ifAvailable: true },
        function (lock) {
          requesting = false;
          if (!lock || paused || destroyed || expected !== generation) {
            emit('standby', {});
            schedule();
            return false;
          }
          return Promise.resolve(assertLease()).then(function (valid) {
            if (valid === false || paused || destroyed || expected !== generation) {
              throw new LeaderError(
                '账户 lease 已失效',
                'BW_DIRECT_LEADER_LEASE',
                false
              );
            }
            leader = true;
            host.start('pwa-leader-acquired');
            emit('leader', {});
            return new Promise(function (resolve) {
              release = resolve;
            });
          }).finally(function () {
            release = null;
            if (leader) {
              leader = false;
              host.pause('pwa-leader-released');
            }
          });
        }
      )).then(function () {
        requesting = false;
        schedule();
        return leader;
      }, function (error) {
        requesting = false;
        emit('error', {
          code: error && error.code || 'BW_DIRECT_LEADER',
          error: String(error && error.message || error)
        });
        schedule();
        return false;
      });
    }
    function start(reason) {
      if (destroyed) {
        throw new LeaderError(
          '直连选举器已销毁',
          'BW_DIRECT_LEADER_DESTROYED',
          false
        );
      }
      if (!paused) return false;
      paused = false;
      generation += 1;
      emit('starting', { reason: String(reason || '') });
      acquire();
      return true;
    }
    function pause(reason) {
      if (destroyed) return false;
      var changed = !paused;
      paused = true;
      generation += 1;
      clearScheduled();
      host.pause(reason || 'pwa-leader-paused');
      if (release) release();
      emit('paused', { reason: String(reason || '') });
      return changed;
    }
    function destroy(reason) {
      if (destroyed) return false;
      pause(reason || 'pwa-leader-destroyed');
      destroyed = true;
      host.destroy(reason || 'pwa-leader-destroyed');
      emit('destroyed', { reason: String(reason || '') });
      return true;
    }

    return {
      contract: CONTRACT,
      start: start,
      resume: start,
      pause: pause,
      destroy: destroy,
      acquire: acquire,
      status: function () {
        return {
          contract: CONTRACT,
          paused: paused,
          destroyed: destroyed,
          generation: generation,
          requesting: requesting,
          leader: leader,
          supported: !!(locks && typeof locks.request === 'function'),
          scheduled: timer != null,
          host: typeof host.status === 'function' ? host.status() : null
        };
      }
    };
  }

  return {
    CONTRACT: CONTRACT,
    LeaderError: LeaderError,
    createDirectSyncLeader: createDirectSyncLeader
  };
});
