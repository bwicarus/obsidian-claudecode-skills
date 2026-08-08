/* native-sync-bootstrap.js — manual App-owned sync-v3 bootstrap.
 *
 * The page owns only the local DataStore and public sync payloads. Swift owns
 * the authenticated namespace and owner lease; neither capability is ever
 * returned to, logged by, or persisted in the page world.
 */
(function (root) {
  'use strict';

  var REQUEST_CONTRACT = 'reader-pi-sync-request/1';
  var RESULT_CONTRACT = 'reader-pi-data-sync-result/1';
  var CONTROL_CONTRACT = 'sync-conflict-control/1';
  var BRIDGE_REQUEST = 'reader-native-pi-sync-request/1';
  var BRIDGE_RESPONSE = 'reader-native-pi-sync-response/1';
  var ERROR_CODE = 'BW_NATIVE_SYNC_BOOTSTRAP_UNAVAILABLE';
  var CHECKPOINT_ID = 'native-sync-checkpoint-v1';
  var CHECKPOINT_CONTRACT = 'native-sync-checkpoint/2';
  var MAX_RECEIPTS = 64;
  var receipts = Object.create(null);
  var receiptOrder = [];
  var sequence = 0;
  var bound = false;
  var initializing = null;
  var active = false;
  var syncRuntime = null;
  var rawControl = null;
  var nativePreferences = null;

  function clone(value) {
    if (value == null) return value;
    return JSON.parse(JSON.stringify(value));
  }
  function runtimeError(message, code, retryable) {
    var error = new Error(String(message || 'Pi 同步失败'));
    error.code = String(code || 'BW_NATIVE_SYNC');
    error.retryable = retryable === true;
    return error;
  }
  function requireApi(name, method) {
    var api = root.BWReaderRuntime && root.BWReaderRuntime[name];
    if (!api || typeof api[method] !== 'function') {
      throw runtimeError(
        '本机同步组件缺少 ' + name,
        'BW_NATIVE_SYNC_DEPENDENCY',
        false
      );
    }
    return api;
  }
  function attachNativePreferences(runtime) {
    if (nativePreferences) return nativePreferences.ready();
    nativePreferences = typeof runtime.preferenceStore === 'function'
      ? runtime.preferenceStore() : null;
    if (
      !nativePreferences ||
      nativePreferences.contract !== 'preference-store/1' ||
      typeof nativePreferences.ready !== 'function'
    ) {
      throw runtimeError(
        '本机设置尚未接入 DataStore',
        'BW_NATIVE_PREFERENCE_STORAGE',
        false
      );
    }
    return Promise.resolve(nativePreferences.ready());
  }
  function exactKeys(value, expected) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
    var actual = Object.keys(value).sort();
    return JSON.stringify(actual) === JSON.stringify(expected.slice().sort());
  }
  function checkedRequest(value) {
    if (
      !exactKeys(value, ['contract', 'requestId']) ||
      value.contract !== REQUEST_CONTRACT ||
      typeof value.requestId !== 'string' ||
      !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value.requestId)
    ) {
      throw runtimeError('Pi 同步请求无效', 'BW_PI_SYNC_REQUEST_INVALID', false);
    }
    return value.requestId;
  }
  function blockedResult(requestId, error) {
    return Object.freeze({
      contract: RESULT_CONTRACT,
      requestId: requestId,
      owner: 'native-app',
      state: 'blocked',
      at: Date.now(),
      collections: ['user-settings', 'vocabulary-state'],
      applied: 0,
      pendingLocal: true,
      conflictCount: 0,
      errorCode: String(error && error.code || ERROR_CODE),
      retryable: error && error.retryable === true
    });
  }
  function trimReceipts(limit) {
    limit = Math.max(0, Number(limit) || 0);
    while (receiptOrder.length > limit) {
      var removable = -1;
      for (var index = 0; index < receiptOrder.length; index += 1) {
        if (receipts[receiptOrder[index]] && receipts[receiptOrder[index]].settled) {
          removable = index;
          break;
        }
      }
      if (removable < 0) break;
      delete receipts[receiptOrder.splice(removable, 1)[0]];
    }
  }
  function bridgeRequestId(action) {
    return ["native", action, Date.now(), ++sequence].join(':');
  }
  function bridgeHandler() {
    return root.webkit && root.webkit.messageHandlers &&
      root.webkit.messageHandlers.bwNativePiSync;
  }
  function bridgeRequest(action, requestId, context, payload) {
    var handler = bridgeHandler();
    if (!handler || typeof handler.postMessage !== 'function') {
      return Promise.reject(runtimeError(
        'App 同步桥不可用', ERROR_CODE, false
      ));
    }
    var body = {
      contract: BRIDGE_REQUEST,
      action: action,
      requestId: requestId
    };
    if (action !== 'release') {
      body.deviceId = context.deviceId;
      body.syncContract = context.syncContract;
      body.syncChangeContract = context.syncChangeContract;
      body.registryDigest = context.registryDigest;
    }
    if (payload) body.payload = clone(payload);
    return Promise.resolve(handler.postMessage(body)).then(function (value) {
      if (
        !value || value.contract !== BRIDGE_RESPONSE ||
        value.requestId !== requestId || value.action !== action
      ) {
        throw runtimeError(
          'App 同步桥响应无效', 'BW_NATIVE_SYNC_RESPONSE', false
        );
      }
      if (value.ok !== true) {
        throw runtimeError(
          value.message || 'App 同步桥拒绝请求',
          /^[A-Z][A-Z0-9_]{0,79}$/.test(String(value.errorCode || ''))
            ? value.errorCode : 'BW_NATIVE_SYNC_REJECTED',
          value.retryable === true
        );
      }
      return clone(value.result || {});
    });
  }

  function checkedCheckpoint(value) {
    if (
      !value || typeof value !== 'object' || Array.isArray(value) ||
      value.contract !== 'sync-coordinator/1'
    ) {
      throw runtimeError(
        '本机同步游标损坏，已停止增量同步',
        'BW_SYNC_CHECKPOINT',
        false
      );
    }
    return clone(value);
  }
  function checkedEpoch(value) {
    value = String(value || '');
    if (!/^data-store-instance-v1-[a-f0-9]{32}$/.test(value)) {
      throw runtimeError(
        '本机数据 Vault 实例编号无效',
        'BW_DATA_INSTANCE_EPOCH',
        false
      );
    }
    return value;
  }
  function mergeCheckpoint(current, incoming) {
    if (
      !current || current.contract !== incoming.contract ||
      current.registryDigest !== incoming.registryDigest
    ) return incoming;
    var merged = clone(incoming);
    var previous = current.server || {};
    var next = merged.server || (merged.server = {});
    var previousEpoch = Math.max(0, Number(previous.reconciliationEpoch) || 0);
    var nextEpoch = Math.max(0, Number(next.reconciliationEpoch) || 0);
    merged.generation = Math.max(
      Number(current.generation) || 0,
      Number(merged.generation) || 0
    );
    if (nextEpoch < previousEpoch) merged.server = clone(previous);
    else if (nextEpoch === previousEpoch) {
      next.localCursor = Math.max(
        Number(previous.localCursor) || 0,
        Number(next.localCursor) || 0
      );
      next.remoteCursor = Math.max(
        Number(previous.remoteCursor) || 0,
        Number(next.remoteCursor) || 0
      );
      next.reconciliationEpoch = previousEpoch;
    }
    // Native sync has no direct peer lane. Never persist a peer/session token.
    merged.peers = {};
    return merged;
  }
  function createCheckpointStore(deviceStore, globalStore, deviceId) {
    var queue = Promise.resolve();
    var observedEpoch = '';
    function epoch() {
      if (!globalStore || typeof globalStore.instanceEpoch !== 'function') {
        return Promise.reject(runtimeError(
          '本机 Vault 缺少实例编号接口',
          'BW_DATA_INSTANCE_EPOCH',
          false
        ));
      }
      return Promise.resolve(globalStore.instanceEpoch()).then(checkedEpoch);
    }
    function record() {
      return deviceStore.get(
        'ui-session', CHECKPOINT_ID, { includeDeleted: true }
      ).then(function (value) { return value || null; });
    }
    function decode(value, vaultEpoch) {
      if (!value || value.deleted) return null;
      value = value.value;
      if (
        !value || value.contract !== CHECKPOINT_CONTRACT ||
        value.schema !== 2 || typeof value.vaultEpoch !== 'string'
      ) {
        throw runtimeError(
          '本机同步游标 envelope 损坏', 'BW_SYNC_CHECKPOINT', false
        );
      }
      if (value.vaultEpoch !== vaultEpoch) return null;
      return checkedCheckpoint(value.checkpoint);
    }
    return {
      load: function () {
        return queue.catch(function () {}).then(function () {
          return epoch().then(function (first) {
            return record().then(function (stored) {
              return epoch().then(function (confirmed) {
                observedEpoch = confirmed;
                return confirmed === first ? decode(stored, first) : null;
              });
            });
          });
        });
      },
      save: function (incoming) {
        var operation = queue.catch(function () {}).then(function () {
          var attempts = 0;
          function write() {
            attempts += 1;
            return epoch().then(function (vaultEpoch) {
              if (observedEpoch && observedEpoch !== vaultEpoch) {
                throw runtimeError(
                  '本机数据 Vault 已重建，请重新执行同步',
                  'BW_SYNC_CHECKPOINT_EPOCH',
                  true
                );
              }
              observedEpoch = vaultEpoch;
              return record().then(function (stored) {
                var current = decode(stored, vaultEpoch);
                var checkpoint = mergeCheckpoint(
                  current, checkedCheckpoint(incoming)
                );
                return deviceStore.put('ui-session', {
                  id: CHECKPOINT_ID,
                  contract: CHECKPOINT_CONTRACT,
                  schema: 2,
                  vaultEpoch: vaultEpoch,
                  checkpoint: checkpoint
                }, {
                  id: CHECKPOINT_ID,
                  ifRev: Number(stored && stored.rev) || 0,
                  mutationId: [
                    'native-sync-checkpoint-v2', deviceId,
                    Date.now(), ++sequence
                  ].join(':')
                }).then(function () {
                  return epoch();
                }).then(function (confirmed) {
                  if (confirmed !== vaultEpoch) {
                    throw runtimeError(
                      '本机数据 Vault 在保存游标时已重建',
                      'BW_SYNC_CHECKPOINT_EPOCH',
                      true
                    );
                  }
                }).catch(function (error) {
                  if (error && error.code === 'BW_DATA_CONFLICT' && attempts < 5) {
                    return write();
                  }
                  throw error;
                });
              });
            });
          }
          return write();
        });
        queue = operation.catch(function () {});
        return operation;
      }
    };
  }

  function createRealControl(runtime) {
    var registry = requireApi('dataRegistry', 'syncCollections');
    var gatewayApi = requireApi('syncGateway', 'createSyncGateway');
    var runtimeApi = requireApi('syncRuntime', 'createSyncRuntime');
    var coordinatorApi = requireApi('syncCoordinator', 'createSyncCoordinator');
    var conflictApi = requireApi(
      'syncConflictControl', 'createSyncConflictControl'
    );
    var stores = runtime.localStores();
    var context = {
      deviceId: runtime.deviceId,
      syncContract: registry.SYNC_CONTRACT,
      syncChangeContract: registry.SYNC_CHANGE_CONTRACT,
      registryDigest: registry.syncDigest()
    };
    if (
      context.syncContract !== 'sync-v3' ||
      context.syncChangeContract !== 'record-parent-state/1' ||
      registry.syncCollections().join('|') !==
        ['user-settings', 'vocabulary-state'].join('|')
    ) {
      throw runtimeError(
        '本机同步 registry 超出已启用集合',
        'BW_NATIVE_SYNC_REGISTRY',
        false
      );
    }
    var transport = {
      exchange: function (payload) {
        return bridgeRequest(
          'exchange',
          'native-exchange:' + Date.now() + ':' + (++sequence),
          context,
          payload
        );
      },
      snapshot: function (payload) {
        return bridgeRequest(
          'snapshot',
          'native-snapshot:' + Date.now() + ':' + (++sequence),
          context,
          payload
        );
      },
      status: function () {
        return Promise.resolve({ state: active ? 'ready' : 'paused' });
      }
    };
    var gateway = gatewayApi.createSyncGateway({
      transport: transport,
      deviceId: context.deviceId
    });
    syncRuntime = runtimeApi.createSyncRuntime({
      coordinatorApi: coordinatorApi,
      store: stores.global,
      registry: registry,
      serverGateway: gateway,
      checkpointStore: createCheckpointStore(
        stores.device, stores.global, context.deviceId
      ),
      manualOnly: true,
      onlineTarget: root,
      assertLease: function () {
        if (!active) {
          throw runtimeError(
            'App 同步 owner 已失效',
            'BW_SYNC_OWNER_INACTIVE',
            false
          );
        }
        return true;
      }
    });
    rawControl = conflictApi.createSyncConflictControl({
      runtime: syncRuntime,
      owner: 'native-app',
      crypto: root.crypto,
      assertFence: function () { return active; },
      collections: registry.syncCollections()
    });

    return Object.freeze({
      contract: CONTROL_CONTRACT,
      owner: 'native-app',
      status: function () { return rawControl.status(); },
      syncNow: function (request) {
        var requestId;
        try { requestId = checkedRequest(request); }
        catch (error) { return Promise.reject(error); }
        if (receipts[requestId]) return receipts[requestId].promise;
        trimReceipts(MAX_RECEIPTS - 1);
        if (receiptOrder.length >= MAX_RECEIPTS) {
          return Promise.reject(runtimeError(
            'Pi 同步请求过多，请等待正在运行的请求结束',
            'BW_PI_SYNC_RECEIPT_LIMIT',
            true
          ));
        }
        var releaseId = bridgeRequestId('release');
        var operation = bridgeRequest(
          'start', bridgeRequestId('start'), context
        ).then(function () {
          active = true;
          syncRuntime.start('native-manual:' + requestId);
          return rawControl.syncNow(request);
        }).then(function (result) {
          syncRuntime.pause('native-manual-complete');
          active = false;
          return bridgeRequest('release', releaseId, context).then(function () {
            return result;
          });
        }, function (error) {
          syncRuntime.pause('native-manual-failed');
          active = false;
          return bridgeRequest('release', releaseId, context).catch(
            function () {}
          ).then(function () { throw error; });
        });
        var entry = { promise: operation, settled: false };
        receipts[requestId] = entry;
        receiptOrder.push(requestId);
        operation.then(function () {
          entry.settled = true;
          trimReceipts(MAX_RECEIPTS);
        }, function () {
          entry.settled = true;
          trimReceipts(MAX_RECEIPTS);
        });
        return operation;
      }
    });
  }

  function localOnlyControl() {
    return Object.freeze({
      contract: CONTROL_CONTRACT,
      owner: 'native-app',
      status: function () {
        return Promise.resolve({
          contract: CONTROL_CONTRACT,
          owner: 'native-app',
          state: 'blocked',
          at: Date.now(),
          errorCode: ERROR_CODE,
          retryable: false,
          conflictCount: 0,
          truncated: false,
          conflicts: []
        });
      },
      syncNow: function (request) {
        var requestId;
        try { requestId = checkedRequest(request); }
        catch (error) { return Promise.reject(error); }
        if (!receipts[requestId]) {
          trimReceipts(MAX_RECEIPTS - 1);
          if (receiptOrder.length >= MAX_RECEIPTS) {
            return Promise.reject(runtimeError(
              'Pi 同步请求过多', 'BW_PI_SYNC_RECEIPT_LIMIT', true
            ));
          }
          receipts[requestId] = {
            promise: Promise.resolve(blockedResult(requestId)),
            settled: true
          };
          receiptOrder.push(requestId);
          trimReceipts(MAX_RECEIPTS);
        }
        return receipts[requestId].promise;
      }
    });
  }

  function bind() {
    if (bound || initializing) return initializing || Promise.resolve(true);
    var runtime = root.BWReaderRuntime && root.BWReaderRuntime.nativeLocalRuntime;
    if (!runtime || typeof runtime.bindSyncControl !== 'function') {
      return Promise.resolve(false);
    }
    initializing = Promise.resolve(
      typeof runtime.ready === 'function' ? runtime.ready() : true
    ).then(function () {
      return attachNativePreferences(runtime);
    }).then(function () {
      var control = bridgeHandler()
        ? createRealControl(runtime) : localOnlyControl();
      runtime.bindSyncControl(control);
      bound = true;
      return true;
    }).catch(function (error) {
      if (!bound) {
        try {
          runtime.bindSyncControl(localOnlyControl());
          bound = true;
        } catch (_) {}
      }
      root.BWReaderRuntime.nativeSyncBootstrapError = Object.freeze({
        code: String(error && error.code || 'BW_NATIVE_SYNC_BOOTSTRAP'),
        retryable: error && error.retryable === true
      });
      return false;
    });
    return initializing;
  }

  root.BWReaderRuntime = root.BWReaderRuntime || {};
  root.BWReaderRuntime.nativeSyncBootstrap = Object.freeze({
    contract: 'native-sync-bootstrap/1',
    status: function () {
      return {
        contract: 'native-sync-bootstrap/1',
        owner: 'native-app',
        state: bound && rawControl ? 'ready' : (bound ? 'local-only' : 'waiting'),
        errorCode: bound && rawControl ? '' : ERROR_CODE
      };
    }
  });

  bind();
  if (typeof root.addEventListener === 'function') {
    root.addEventListener('bw:native-local-runtime-ready', bind, { once: true });
  }
})(typeof globalThis !== 'undefined' ? globalThis : window);
