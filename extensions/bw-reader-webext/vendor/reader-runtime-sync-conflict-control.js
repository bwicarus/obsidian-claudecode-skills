/* sync-conflict-control.js — bounded sync status + explicit manual run surface.
 *
 * The wrapped SyncRuntime remains the only synchronization owner. syncNow()
 * only asks that owner to drain its existing checkpointed journal; it never
 * unpauses a conflict and never selects a local/server winner. Advancing a
 * durable conflict still requires a real resolver that records the decision.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.BWReaderRuntime = root.BWReaderRuntime || {};
  root.BWReaderRuntime.syncConflictControl = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var CONTRACT = 'sync-conflict-control/1';
  var RUNTIME_CONTRACT = 'sync-runtime/1';
  var SYNC_REQUEST_CONTRACT = 'reader-pi-sync-request/1';
  var SYNC_RESULT_CONTRACT = 'reader-pi-data-sync-result/1';
  var MAX_PUBLIC_CONFLICTS = 50;
  var MAX_CONFLICT_COUNT = 1000000;
  var MAX_MANUAL_RECEIPTS = 64;
  var OWNER_VALUES = {
    pwa: true,
    'native-app': true,
    'extension-background': true
  };
  var REASON_VALUES = {
    'apply-conflict': true,
    'invalid-change': true,
    'revision-conflict': true,
    'same-rev-different-value': true,
    'stable-id-diverged': true,
    'stale-incoming': true,
    'tombstone-dominates': true,
    'causal-proof-missing': true,
    'causal-proof-invalid': true,
    'causal-proof-too-large': true,
    'causal-parent-mismatch': true,
    'causal-revision-overflow': true
  };
  var SECRET_PATTERN =
    /(?:bearer\s+|sk-[a-z0-9_-]{16,}|acct-v1-[a-f0-9]{64}|pvt-v[0-9]+-[a-z0-9-]{24,}|account-proof-v1-[a-f0-9]{32,})/i;
  var ownedErrors = new WeakSet();

  function ConflictControlError(message, code, retryable) {
    this.name = 'SyncConflictControlError';
    this.message = String(message || '同步冲突状态读取失败');
    this.code = String(code || 'BW_SYNC_CONFLICT_CONTROL');
    this.retryable = !!retryable;
    if (Error.captureStackTrace) {
      Error.captureStackTrace(this, ConflictControlError);
    }
  }
  ConflictControlError.prototype = Object.create(Error.prototype);
  ConflictControlError.prototype.constructor = ConflictControlError;

  function controlError(message, code, retryable) {
    var error = new ConflictControlError(message, code, retryable);
    ownedErrors.add(error);
    return error;
  }
  function isControlError(error) {
    return !!error && ownedErrors.has(error);
  }
  function own(value, key) {
    return Object.prototype.hasOwnProperty.call(value, key);
  }
  function safeOwner(value) {
    value = String(value || '');
    return OWNER_VALUES[value] ? value : 'unknown';
  }
  function safeIdentifier(value, maximum, pattern) {
    if (typeof value !== 'string') value = String(value == null ? '' : value);
    value = value.trim();
    if (
      !value ||
      value.length > maximum ||
      SECRET_PATTERN.test(value) ||
      /[\u0000-\u001f\u007f]/.test(value) ||
      !pattern.test(value)
    ) return '';
    return value;
  }
  function safeRevision(value) {
    value = Number(value);
    if (!Number.isSafeInteger(value) || value < 0) return 0;
    return value;
  }
  function safeTime(value) {
    value = Number(value);
    if (!Number.isSafeInteger(value) || value < 0) return 0;
    return value;
  }
  function exactKeys(value, expected) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
    var actual = Object.keys(value).sort();
    expected = expected.slice().sort();
    return JSON.stringify(actual) === JSON.stringify(expected);
  }
  function checkedSyncRequest(value) {
    if (
      !exactKeys(value, ['contract', 'requestId']) ||
      value.contract !== SYNC_REQUEST_CONTRACT
    ) {
      throw controlError(
        'Pi 同步请求合同无效',
        'BW_PI_SYNC_REQUEST_INVALID',
        false
      );
    }
    var requestId = safeIdentifier(
      value.requestId,
      128,
      /^[A-Za-z0-9][A-Za-z0-9._:-]*$/
    );
    if (!requestId) {
      throw controlError(
        'Pi 同步请求编号无效',
        'BW_PI_SYNC_REQUEST_INVALID',
        false
      );
    }
    return { contract: SYNC_REQUEST_CONTRACT, requestId: requestId };
  }
  function safeCollections(value) {
    var seen = Object.create(null);
    return (Array.isArray(value) ? value : []).map(function (name) {
      return safeIdentifier(name, 80, /^[A-Za-z0-9._-]+$/);
    }).filter(function (name) {
      if (!name || seen[name]) return false;
      seen[name] = true;
      return true;
    }).sort();
  }
  function safeErrorCode(value) {
    value = String(value || '');
    return /^[A-Z][A-Z0-9_]{0,79}$/.test(value)
      ? value
      : 'BW_SYNC_ERROR';
  }
  function publicReason(value) {
    value = String(value || '');
    return REASON_VALUES[value] ? value : 'conflict';
  }
  function conflictCollection(conflict) {
    return safeIdentifier(
      conflict && (
        conflict.collection ||
        conflict.incoming && conflict.incoming.collection ||
        conflict.local && conflict.local.collection ||
        conflict.change && (
          conflict.change.collection ||
          conflict.change.record && conflict.change.record.collection
        )
      ),
      80,
      /^[A-Za-z0-9._-]+$/
    );
  }
  function conflictId(conflict) {
    return safeIdentifier(
      conflict && (
        conflict.id ||
        conflict.incoming && conflict.incoming.id ||
        conflict.local && conflict.local.id ||
        conflict.change && conflict.change.record &&
          conflict.change.record.id
      ),
      256,
      /^.+$/u
    );
  }
  function incomingRevision(conflict) {
    if (!conflict) return 0;
    if (own(conflict, 'incomingRev')) return safeRevision(conflict.incomingRev);
    if (conflict.incoming) return safeRevision(conflict.incoming.rev);
    if (conflict.change && conflict.change.record) {
      return safeRevision(conflict.change.record.rev);
    }
    return 0;
  }
  function currentRevision(conflict) {
    if (!conflict) return 0;
    if (own(conflict, 'currentRev')) return safeRevision(conflict.currentRev);
    if (conflict.local) return safeRevision(conflict.local.rev);
    if (conflict.current) return safeRevision(conflict.current.rev);
    return 0;
  }
  function publicConflict(lane, conflict) {
    return {
      lane: lane,
      collection: conflictCollection(conflict),
      id: conflictId(conflict),
      reason: publicReason(conflict && conflict.reason),
      incomingRev: incomingRevision(conflict),
      currentRev: currentRevision(conflict)
    };
  }
  function collectConflicts(runtimeStatus) {
    var lastResult = runtimeStatus && runtimeStatus.lastResult || {};
    var visible = [];
    var total = 0;
    function append(lane, conflict) {
      total += 1;
      if (visible.length < MAX_PUBLIC_CONFLICTS) {
        visible.push(publicConflict(lane, conflict));
      }
    }
    var server = lastResult.server || {};
    var serverConflicts = Array.isArray(server.conflicts)
      ? server.conflicts
      : [];
    serverConflicts.forEach(function (conflict) {
      append('server', conflict);
    });
    var direct = lastResult.direct;
    if (direct && Object.prototype.toString.call(direct) === '[object Object]') {
      Object.keys(direct).sort().forEach(function (peerId) {
        var lane = direct[peerId] || {};
        var conflicts = Array.isArray(lane.conflicts) ? lane.conflicts : [];
        conflicts.forEach(function (conflict) {
          append('direct', conflict);
        });
      });
    }
    visible.sort(function (left, right) {
      var leftKey = [
        left.lane,
        left.collection,
        left.id,
        left.reason,
        left.incomingRev,
        left.currentRev
      ].join('\u001f');
      var rightKey = [
        right.lane,
        right.collection,
        right.id,
        right.reason,
        right.incomingRev,
        right.currentRev
      ].join('\u001f');
      return leftKey < rightKey ? -1 : leftKey > rightKey ? 1 : 0;
    });
    return {
      total: Math.min(MAX_CONFLICT_COUNT, total),
      truncated: total > MAX_PUBLIC_CONFLICTS ||
        total > MAX_CONFLICT_COUNT,
      conflicts: visible
    };
  }
  function publicFailure(runtimeStatus) {
    var runtimeError = runtimeStatus && runtimeStatus.lastError;
    if (runtimeError && typeof runtimeError === 'object') {
      return {
        code: safeErrorCode(runtimeError.code),
        retryable: runtimeError.retryable !== false
      };
    }
    var server = runtimeStatus && runtimeStatus.lastResult &&
      runtimeStatus.lastResult.server;
    if (server && typeof server === 'object' && server.ok === false) {
      return {
        code: safeErrorCode(server.code),
        retryable: server.retryable !== false
      };
    }
    return null;
  }
  function stateOf(runtimeStatus, conflictCount, failure) {
    if (
      conflictCount > 0 &&
      runtimeStatus.paused === true &&
      runtimeStatus.pauseReason === 'sync-conflict'
    ) return 'blocked';
    if (conflictCount > 0) return 'conflict-observed';
    if (runtimeStatus.destroyed === true) return 'destroyed';
    if (failure) return 'error';
    if (runtimeStatus.paused === true) return 'paused';
    if (runtimeStatus.running === true) return 'syncing';
    return 'ready';
  }

  function createSyncConflictControl(options) {
    options = options || {};
    var runtime = options.runtime;
    var owner = safeOwner(options.owner);
    var now = typeof options.now === 'function'
      ? options.now
      : function () { return Date.now(); };
    var assertFence = typeof options.assertFence === 'function'
      ? options.assertFence
      : function () { return true; };
    var collections = safeCollections(options.collections);
    var manualReceipts = Object.create(null);
    var manualReceiptOrder = [];

    if (
      !runtime ||
      runtime.contract !== RUNTIME_CONTRACT ||
      typeof runtime.status !== 'function' ||
      typeof runtime.runNow !== 'function'
    ) {
      throw controlError(
        'sync-runtime/1 不可用',
        'BW_SYNC_CONFLICT_DEPENDENCY',
        false
      );
    }

    function fence() {
      return Promise.resolve().then(function () {
        return assertFence();
      }).then(function (valid) {
        if (valid === false) {
          throw controlError(
            '同步冲突状态所有权已失效',
            'BW_SYNC_CONFLICT_FENCE',
            false
          );
        }
        return true;
      }, function () {
        throw controlError(
          '同步冲突状态所有权已失效',
          'BW_SYNC_CONFLICT_FENCE',
          false
        );
      });
    }
    function readStatus() {
      return fence().then(function () {
        return runtime.status();
      }).then(function (value) {
        if (!value || value.contract !== RUNTIME_CONTRACT) {
          throw controlError(
            '同步运行时状态无效',
            'BW_SYNC_CONFLICT_RUNTIME',
            false
          );
        }
        return fence().then(function () { return value; });
      }, function (error) {
        if (isControlError(error)) throw error;
        throw controlError(
          '无法读取同步冲突状态',
          'BW_SYNC_CONFLICT_RUNTIME',
          true
        );
      });
    }
    function publicStatus() {
      return readStatus().then(function (runtimeStatus) {
        var summary = collectConflicts(runtimeStatus);
        var failure = publicFailure(runtimeStatus);
        return {
          contract: CONTRACT,
          owner: owner,
          state: stateOf(runtimeStatus, summary.total, failure),
          at: safeTime(now()),
          errorCode: failure ? failure.code : '',
          retryable: failure ? failure.retryable : false,
          conflictCount: summary.total,
          truncated: summary.truncated,
          conflicts: summary.conflicts
        };
      }).catch(function (error) {
        if (isControlError(error)) throw error;
        throw controlError(
          '无法生成同步冲突状态',
          'BW_SYNC_CONFLICT_RUNTIME',
          true
        );
      });
    }

    function manualResult(requestId, runtimeStatus, rawResult) {
      runtimeStatus = runtimeStatus || {};
      rawResult = rawResult && typeof rawResult === 'object'
        ? rawResult
        : null;
      var summary = collectConflicts(runtimeStatus);
      var failure = publicFailure(runtimeStatus);
      var server = rawResult && rawResult.server &&
        typeof rawResult.server === 'object'
        ? rawResult.server
        : {};
      var skippedCode = rawResult && rawResult.skipped === true
        ? safeErrorCode(rawResult.code)
        : '';
      var state = 'unknown';
      if (summary.total > 0) state = 'blocked';
      else if (runtimeStatus.destroyed === true) state = 'error';
      else if (runtimeStatus.paused === true || skippedCode) state = 'blocked';
      else if (server.ok === true) {
        state = server.pendingLocal === true ? 'partial' : 'complete';
      } else if (server.ok === false || failure) state = 'error';
      return {
        contract: SYNC_RESULT_CONTRACT,
        requestId: requestId,
        owner: owner,
        state: state,
        at: safeTime(now()),
        collections: collections.slice(),
        applied: safeRevision(server.applied),
        pendingLocal: server.pendingLocal === true,
        conflictCount: summary.total,
        errorCode: skippedCode || (failure ? failure.code : ''),
        retryable: failure ? failure.retryable : false
      };
    }
    function trimManualReceipts(limit) {
      limit = Math.max(0, Number(limit));
      while (manualReceiptOrder.length > limit) {
        var removable = -1;
        for (var index = 0; index < manualReceiptOrder.length; index += 1) {
          var candidate = manualReceipts[manualReceiptOrder[index]];
          if (candidate && candidate.settled === true) {
            removable = index;
            break;
          }
        }
        if (removable < 0) break;
        var removed = manualReceiptOrder.splice(removable, 1)[0];
        delete manualReceipts[removed];
      }
    }
    function syncNow(input) {
      var request;
      try {
        request = checkedSyncRequest(input);
      } catch (error) {
        return Promise.reject(error);
      }
      var existing = manualReceipts[request.requestId];
      if (existing) return existing.promise;
      trimManualReceipts(MAX_MANUAL_RECEIPTS - 1);
      if (manualReceiptOrder.length >= MAX_MANUAL_RECEIPTS) {
        return Promise.reject(controlError(
          'Pi 同步请求过多，请等待正在运行的请求结束',
          'BW_PI_SYNC_RECEIPT_LIMIT',
          true
        ));
      }

      var entry = { promise: null, settled: false };
      var operation = readStatus().then(function (before) {
        if (before.destroyed === true || before.paused === true) {
          return manualResult(request.requestId, before, null);
        }
        return fence().then(function () {
          return runtime.runNow('native-pi-sync:' + request.requestId);
        }).then(function (rawResult) {
          return fence().then(function () {
            return runtime.status();
          }).then(function (after) {
            if (!after || after.contract !== RUNTIME_CONTRACT) {
              throw controlError(
                '同步运行时状态无效',
                'BW_SYNC_CONFLICT_RUNTIME',
                false
              );
            }
            return fence().then(function () {
              return manualResult(request.requestId, after, rawResult);
            });
          });
        });
      });
      entry.promise = operation;
      manualReceipts[request.requestId] = entry;
      manualReceiptOrder.push(request.requestId);
      operation.then(function () {
        entry.settled = true;
        trimManualReceipts(MAX_MANUAL_RECEIPTS);
      }, function () {
        entry.settled = true;
        trimManualReceipts(MAX_MANUAL_RECEIPTS);
      });
      return operation;
    }

    return Object.freeze({
      contract: CONTRACT,
      owner: owner,
      status: publicStatus,
      syncNow: syncNow
    });
  }

  return Object.freeze({
    CONTRACT: CONTRACT,
    SYNC_REQUEST_CONTRACT: SYNC_REQUEST_CONTRACT,
    SYNC_RESULT_CONTRACT: SYNC_RESULT_CONTRACT,
    ConflictControlError: ConflictControlError,
    createSyncConflictControl: createSyncConflictControl
  });
});
