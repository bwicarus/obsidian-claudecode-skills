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
  var CHECKPOINT_CONTRACT = 'native-sync-checkpoint/3';
  var CARD_BOOTSTRAP_CONTRACT = 'reader-card-repository-bootstrap/1';
  var CARD_BOOTSTRAP_PATH = '/pdf/api/card-repository/bootstrap';
  var CARD_BOOTSTRAP_PAGE_LIMIT = 100;
  var CARD_BOOTSTRAP_MAX_ITEMS = 500;
  var CARD_BOOTSTRAP_MAX_PAGES = 500;
  var CARD_BOOTSTRAP_MAX_CURSOR_BYTES = 512;
  var SYNC_COLLECTIONS = [
    'card-entities', 'card-states', 'user-settings', 'vocabulary-state'
  ];
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
      collections: SYNC_COLLECTIONS.slice(),
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

  function checkedStartResult(value) {
    if (
      !exactKeys(value, ['state', 'accountBinding']) ||
      value.state !== 'ready' ||
      typeof value.accountBinding !== 'string' ||
      !/^sha256:[a-f0-9]{64}$/.test(value.accountBinding)
    ) {
      throw runtimeError(
        'App 同步 owner 启动响应无效',
        'BW_NATIVE_SYNC_START_RESPONSE',
        false
      );
    }
    return value.accountBinding;
  }
  function plainObject(value) {
    return !!value && typeof value === 'object' && !Array.isArray(value);
  }
  function checkedBootstrapItem(value) {
    if (
      !exactKeys(value, ['id', 'cards', 'states', 'source_ref', 'req', 'meta']) ||
      typeof value.id !== 'string' ||
      !/^card_[a-f0-9]{4,12}$/.test(value.id) ||
      !Array.isArray(value.cards) ||
      value.cards.length < 1 || value.cards.length > 256 ||
      !plainObject(value.states) ||
      typeof value.source_ref !== 'string' || value.source_ref.length > 8192 ||
      typeof value.req !== 'string' || value.req.length > 32768 ||
      !plainObject(value.meta)
    ) {
      throw runtimeError(
        'Pi 旧卡片 bootstrap item 无效',
        'BW_CARD_BOOTSTRAP_ITEM',
        false
      );
    }
    Object.keys(value.states).forEach(function (key) {
      if (!/^(?:0|[1-9]\d*)$/.test(key) || Number(key) >= value.cards.length) {
        throw runtimeError(
          'Pi 旧卡片 bootstrap state index 无效',
          'BW_CARD_BOOTSTRAP_ITEM',
          false
        );
      }
    });
    return clone(value);
  }
  function checkedBootstrapPage(value, expectedDigest) {
    if (
      !exactKeys(
        value,
        ['contract', 'items', 'nextCursor', 'complete', 'snapshotDigest']
      ) ||
      value.contract !== CARD_BOOTSTRAP_CONTRACT ||
      !Array.isArray(value.items) ||
      value.items.length > CARD_BOOTSTRAP_PAGE_LIMIT ||
      typeof value.complete !== 'boolean' ||
      typeof value.snapshotDigest !== 'string' ||
      !/^sha256:[a-f0-9]{64}$/.test(value.snapshotDigest)
    ) {
      throw runtimeError(
        'Pi 旧卡片 bootstrap page 合同无效',
        'BW_CARD_BOOTSTRAP_CONTRACT',
        false
      );
    }
    if (expectedDigest && value.snapshotDigest !== expectedDigest) {
      throw runtimeError(
        'Pi 旧卡片 bootstrap 快照在分页中发生变化',
        'BW_CARD_BOOTSTRAP_SNAPSHOT_CHANGED',
        true
      );
    }
    if (value.complete) {
      if (value.nextCursor !== null) {
        throw runtimeError(
          'Pi 旧卡片 bootstrap 完成页仍包含 cursor',
          'BW_CARD_BOOTSTRAP_CURSOR',
          false
        );
      }
    } else if (
      !value.items.length ||
      typeof value.nextCursor !== 'string' ||
      !/^[A-Za-z0-9_-]+$/.test(value.nextCursor) ||
      value.nextCursor.length > CARD_BOOTSTRAP_MAX_CURSOR_BYTES
    ) {
      throw runtimeError(
        'Pi 旧卡片 bootstrap cursor 无效',
        'BW_CARD_BOOTSTRAP_CURSOR',
        false
      );
    }
    return {
      items: value.items.map(checkedBootstrapItem),
      nextCursor: value.nextCursor,
      complete: value.complete,
      snapshotDigest: value.snapshotDigest
    };
  }
  function bootstrapHTTPError(response) {
    var status = Math.max(0, Number(response && response.status) || 0);
    var body = response && typeof response.json === 'function'
      ? Promise.resolve().then(function () { return response.json(); })
      : Promise.resolve(null);
    return body.catch(function () { return null; }).then(function (value) {
      var serverCode = plainObject(value) ? String(value.code || '') : '';
      var code = status === 401
        ? 'BW_PI_AUTH_REQUIRED'
        : (status === 409 && serverCode === 'snapshot_changed'
          ? 'BW_CARD_BOOTSTRAP_SNAPSHOT_CHANGED'
          : 'BW_CARD_BOOTSTRAP_HTTP');
      var message = plainObject(value) && typeof value.error === 'string'
        ? value.error.slice(0, 512)
        : ('Pi 旧卡片 bootstrap 请求失败（HTTP ' + status + '）');
      throw runtimeError(
        message,
        code,
        status === 409 || status === 429 || status >= 500
      );
    });
  }
  function fetchBootstrapPage(cursor) {
    if (typeof root.fetch !== 'function') {
      return Promise.reject(runtimeError(
        'App Pi 网关 fetch 不可用',
        'BW_CARD_BOOTSTRAP_GATEWAY',
        false
      ));
    }
    var path = CARD_BOOTSTRAP_PATH + '?limit=' + CARD_BOOTSTRAP_PAGE_LIMIT;
    if (cursor) path += '&cursor=' + encodeURIComponent(cursor);
    // @interaction card.repository.bootstrap
    return Promise.resolve(root.fetch(path, {
      method: 'GET',
      cache: 'no-store',
      credentials: 'same-origin',
      headers: { 'Accept': 'application/json' }
    })).then(function (response) {
      if (!response || response.ok !== true) return bootstrapHTTPError(response);
      if (typeof response.json !== 'function') {
        throw runtimeError(
          'Pi 旧卡片 bootstrap 响应不可解析',
          'BW_CARD_BOOTSTRAP_RESPONSE',
          false
        );
      }
      return Promise.resolve().then(function () {
        return response.json();
      }).catch(function () {
        throw runtimeError(
          'Pi 旧卡片 bootstrap JSON 无效',
          'BW_CARD_BOOTSTRAP_RESPONSE',
          false
        );
      });
    });
  }
  function legacyImportRecord(item) {
    var record = {
      id: item.id,
      kind: 'cards',
      cards: clone(item.cards),
      states: clone(item.states),
      source_ref: item.source_ref,
      req: item.req,
      meta: clone(item.meta)
    };
    if (Object.prototype.hasOwnProperty.call(item.meta, 'ts')) {
      record.ts = clone(item.meta.ts);
    }
    return record;
  }
  function importLegacySnapshot(repository, items, requestId) {
    if (!items.length) return Promise.resolve(0);
    return Promise.all(items.map(function (item) {
      return repository.load(item.id, { includeDeleted: true });
    })).then(function (existing) {
      var records = [];
      items.forEach(function (item, index) {
        /* A local gid, including a tombstone, is authoritative as a whole.
         * Do not send any older Pi cards, source metadata, or review state to
         * the repository for comparison or merge. */
        if (!existing[index]) records.push(legacyImportRecord(item));
      });
      if (!records.length) return 0;
      return repository.importLegacyBatch(records, {
        mutationId: 'native-card-bootstrap:' + requestId,
        missingOnly: true
      }).then(function (result) {
        if (!Array.isArray(result) || result.length !== records.length) {
          throw runtimeError(
            '本机卡片仓返回了无效的 bootstrap 结果',
            'BW_CARD_BOOTSTRAP_IMPORT_RESPONSE',
            false
          );
        }
        return result.length;
      });
    });
  }
  function bootstrapLegacyCards(requestId) {
    var repository = requireApi('cardRepository', 'importLegacyBatch');
    if (
      repository.CONTRACT !== 'card-repository/1' ||
      typeof repository.load !== 'function'
    ) {
      return Promise.reject(runtimeError(
        '本机卡片仓 bootstrap 接口不可用',
        'BW_CARD_BOOTSTRAP_REPOSITORY',
        false
      ));
    }
    var items = [];
    var seenIds = Object.create(null);
    var seenCursors = Object.create(null);
    var snapshotDigest = '';
    var previousId = '';
    var pages = 0;
    function page(cursor) {
      if (pages >= CARD_BOOTSTRAP_MAX_PAGES) {
        return Promise.reject(runtimeError(
          'Pi 旧卡片 bootstrap 分页超过上限',
          'BW_CARD_BOOTSTRAP_LIMIT',
          false
        ));
      }
      pages += 1;
      return fetchBootstrapPage(cursor).then(function (value) {
        var checked = checkedBootstrapPage(value, snapshotDigest);
        if (!snapshotDigest) snapshotDigest = checked.snapshotDigest;
        if (items.length + checked.items.length > CARD_BOOTSTRAP_MAX_ITEMS) {
          throw runtimeError(
            'Pi 旧卡片 bootstrap 记录超过原子导入上限',
            'BW_CARD_BOOTSTRAP_LIMIT',
            false
          );
        }
        checked.items.forEach(function (item) {
          if (seenIds[item.id] || previousId && item.id <= previousId) {
            throw runtimeError(
              'Pi 旧卡片 bootstrap item 顺序或身份重复',
              'BW_CARD_BOOTSTRAP_ITEMS',
              false
            );
          }
          seenIds[item.id] = true;
          previousId = item.id;
          items.push(item);
        });
        if (checked.complete) return items;
        if (seenCursors[checked.nextCursor]) {
          throw runtimeError(
            'Pi 旧卡片 bootstrap cursor 形成循环',
            'BW_CARD_BOOTSTRAP_CURSOR_LOOP',
            false
          );
        }
        seenCursors[checked.nextCursor] = true;
        return page(checked.nextCursor);
      });
    }
    return page(null).then(function (completeItems) {
      return importLegacySnapshot(repository, completeItems, requestId);
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
  function checkpointForRegistry(value, registry) {
    var checkpoint = checkedCheckpoint(value);
    var currentDigest = String(registry.syncDigest() || '');
    if (checkpoint.registryDigest === currentDigest) return checkpoint;
    var migration = typeof registry.syncCheckpointMigration === 'function'
      ? registry.syncCheckpointMigration(checkpoint.registryDigest)
      : null;
    if (
      migration &&
      migration.contract === 'sync-registry-migration/1' &&
      migration.from === checkpoint.registryDigest &&
      migration.to === currentDigest &&
      migration.strategy === 'reset-checkpoint'
    ) {
      /* Adding a synchronized collection invalidates every shared cursor.
       * Return an empty checkpoint to the coordinator, but do not persist an
       * upgraded envelope here: only a completed reconciliation may do that.
       * Existing user-settings/vocabulary/card records stay in DataStore. */
      return null;
    }
    // Unknown historical digests retain the coordinator's existing safe-reset
    // behavior. They never inherit cursors from an unrecognized registry.
    return checkpoint;
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
  function createCheckpointStore(
    deviceStore, globalStore, deviceId, registry, accountBinding
  ) {
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
    function binding() {
      var value = String(accountBinding() || '');
      if (!/^sha256:[a-f0-9]{64}$/.test(value)) {
        throw runtimeError(
          'App 同步账户绑定尚未建立',
          'BW_SYNC_ACCOUNT_BINDING',
          false
        );
      }
      return value;
    }
    function decode(value, vaultEpoch, currentBinding) {
      if (!value || value.deleted) return null;
      value = value.value;
      /* v2 checkpoints predate account binding and can never authorize a
       * remote cursor.  Treat them as absent without touching local data. */
      if (
        value && value.contract === 'native-sync-checkpoint/2' &&
        value.schema === 2
      ) return null;
      if (
        !value || value.contract !== CHECKPOINT_CONTRACT ||
        value.schema !== 3 || typeof value.vaultEpoch !== 'string' ||
        typeof value.accountBinding !== 'string' ||
        !/^sha256:[a-f0-9]{64}$/.test(value.accountBinding)
      ) {
        throw runtimeError(
          '本机同步游标 envelope 损坏', 'BW_SYNC_CHECKPOINT', false
        );
      }
      if (value.vaultEpoch !== vaultEpoch) return null;
      if (value.accountBinding !== currentBinding) return null;
      return checkpointForRegistry(value.checkpoint, registry);
    }
    return {
      load: function () {
        return queue.catch(function () {}).then(function () {
          var currentBinding = binding();
          return epoch().then(function (first) {
            return record().then(function (stored) {
              return epoch().then(function (confirmed) {
                observedEpoch = confirmed;
                return confirmed === first
                  ? decode(stored, first, currentBinding) : null;
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
            var currentBinding = binding();
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
                var current = decode(stored, vaultEpoch, currentBinding);
                var checkpoint = mergeCheckpoint(
                  current, checkedCheckpoint(incoming)
                );
                return deviceStore.put('ui-session', {
                  id: CHECKPOINT_ID,
                  contract: CHECKPOINT_CONTRACT,
                  schema: 3,
                  vaultEpoch: vaultEpoch,
                  accountBinding: currentBinding,
                  checkpoint: checkpoint
                }, {
                  id: CHECKPOINT_ID,
                  ifRev: Number(stored && stored.rev) || 0,
                  mutationId: [
                    'native-sync-checkpoint-v3', deviceId,
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
                  if (binding() !== currentBinding) {
                    throw runtimeError(
                      'App 同步账户在保存游标时已变化',
                      'BW_SYNC_ACCOUNT_BINDING',
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
      registryDigest: registry.syncDigest(),
      accountBinding: ''
    };
    if (
      context.syncContract !== 'sync-v3' ||
      context.syncChangeContract !== 'record-parent-state/1' ||
      registry.syncCollections().join('|') !==
        SYNC_COLLECTIONS.join('|') ||
      typeof registry.syncCheckpointMigration !== 'function'
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
        stores.device,
        stores.global,
        context.deviceId,
        registry,
        function () { return context.accountBinding; }
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
        var startAttempted = false;
        /* Validate and atomically import the complete legacy card snapshot
         * before asking the relay to advance its registry generation.  A
         * failed import must not permanently fence still-installed clients
         * that legitimately use the previous cardless digest. */
        var operation = bootstrapLegacyCards(requestId).then(function () {
          startAttempted = true;
          return bridgeRequest('start', bridgeRequestId('start'), context);
        }).then(function (startResult) {
          context.accountBinding = checkedStartResult(startResult);
          active = true;
          syncRuntime.start('native-manual:' + requestId);
          return rawControl.syncNow(request);
        }).then(function (result) {
          syncRuntime.pause('native-manual-complete');
          active = false;
          return bridgeRequest('release', releaseId, context).then(function () {
            context.accountBinding = '';
            return result;
          }, function (error) {
            context.accountBinding = '';
            throw error;
          });
        }, function (error) {
          syncRuntime.pause('native-manual-failed');
          active = false;
          if (!startAttempted) throw error;
          return bridgeRequest('release', releaseId, context).catch(
            function () {}
          ).then(function () {
            context.accountBinding = '';
            throw error;
          });
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
