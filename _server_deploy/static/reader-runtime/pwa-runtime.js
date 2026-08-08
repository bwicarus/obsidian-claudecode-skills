/* pwa-runtime.js — PWA 唯一 UI 的生产启动器。
 *
 * 本文件组装 DocumentHost、三个 PWA IndexedDB store、PreferenceStore 与可选扩展
 * provider。显式白名单设置由 PreferenceStore 统一接入 runtime.storage()；旧组件看到
 * 的 localStorage 只是当前账户的首屏兼容镜像。
 */
(function () {
  'use strict';
  var root = typeof globalThis !== 'undefined' ? globalThis : window;
  var api = root.BWReaderRuntime = root.BWReaderRuntime || {};
  if (api.pwaRuntime) return;

  var bootPromise = null;
  var runtime = null;
  var localStores = null;
  var pwaSyncGateway = null;
  var pwaSyncRuntime = null;
  var pwaSyncControl = null;
  var pwaSyncOwnerLease = null;
  var pwaSyncOwnerLifecycleGeneration = 0;
  var pwaNetworkRuntimeReady = false;
  var pageOwnershipRelease = Promise.resolve(false);
  var pageOwnershipLifecycleGeneration = 0;
  var providerSyncControl = null;
  var reservedSyncControl = null;
  var extensionSyncOwnerReserved = false;
  var pwaDirectHost = null;
  var pwaDirectLeader = null;
  var preferences = null;
  var preferenceHydration = Promise.resolve();
  var preferenceHydrationError = null;
  var namespace = '';
  var accountLease = null;
  var installId = '';
  var pendingProvider = null;
  var pendingProviderGeneration = 0;
  var providerLifecycleGeneration = 0;
  var providerOperationTail = Promise.resolve();
  var providerAuthRecoveryAttempted = false;
  var providerAuthRecoveryPending = false;
  var INSTALL_ID_KEY = 'bw.reader.pwa.install-id.v1';
  var PROVIDER_TICKET_SKEW_SECONDS = 5;
  var PROVIDER_TICKET_BOOT_REFRESH_SECONDS = 30;
  var SYNC_CHECKPOINT_ID = 'reader-sync-checkpoint-v1';
  var SYNC_CHECKPOINT_CONTRACT = 'pwa-sync-checkpoint/2';
  var SYNC_CHECKPOINT_SCHEMA = 2;
  var PAGE_OWNERSHIP_RELEASE_WAIT_MS = 2000;
  var syncCheckpointSequence = 0;

  function accountContext() {
    var context = requireApi('accountContext', 'activate');
    if (
      context.CONTRACT !== 'account-context/1' ||
      typeof context.deactivate !== 'function' ||
      typeof context.lease !== 'function' ||
      typeof context.isCurrent !== 'function' ||
      typeof context.assertCurrent !== 'function'
    ) {
      var invalid = new Error('ReaderRuntime.accountContext 合同不完整');
      invalid.code = 'BW_ACCOUNT_CONTEXT_CONTRACT';
      throw invalid;
    }
    return context;
  }
  function activateServerAccount() {
    var value = String(
      root.__USER__ && root.__USER__.storage_namespace || ''
    ).trim();
    var context = accountContext();
    var snapshot = context.snapshot();
    var active = snapshot.active &&
      snapshot.namespace === value &&
      snapshot.source === 'server-session'
      ? snapshot
      : context.activate({
        namespace: value,
        source: 'server-session'
      });
    namespace = active.namespace;
    accountLease = context.lease();
    return accountLease;
  }
  function assertAccountLease(lease) {
    return accountContext().assertCurrent(lease);
  }
  function accountFenceError(error) {
    return !!(
      error &&
      (
        error.code === 'BW_ACCOUNT_CONTEXT_STALE' ||
        error.code === 'BW_ACCOUNT_CONTEXT_UNAVAILABLE'
      )
    );
  }
  function publicSyncErrorCode(error) {
    var code = String(error && error.code || '');
    return /^[A-Z0-9_]{1,80}$/.test(code)
      ? code
      : 'BW_SYNC_RUNTIME';
  }
  function emitPwaSyncStatus(error) {
    if (
      error &&
      (
        accountFenceError(error) ||
        error.code === 'BW_SYNC_OWNER_INACTIVE'
      )
    ) return;
    if (error) {
      emit('bw:reader-sync-error', {
        contract: 'sync-conflict-control/1',
        owner: 'pwa',
        code: publicSyncErrorCode(error)
      });
    }
    var control = pwaSyncControl;
    if (!control || typeof control.status !== 'function') return;
    Promise.resolve().then(function () {
      return control.status();
    }).then(function (status) {
      emit('bw:reader-sync-status', status);
    }).catch(function (statusError) {
      if (accountFenceError(statusError)) return;
      emit('bw:reader-sync-error', {
        contract: 'sync-conflict-control/1',
        owner: 'pwa',
        code: publicSyncErrorCode(statusError)
      });
    });
  }
  function providerAuthorization(value) {
    value = String(value || '').trim();
    var match = /^pvt-v2-([0-9]{10,12})-[a-f0-9]{32}-[a-f0-9]{64}$/.exec(value);
    if (!match) return null;
    return { ticket: value, expiresAt: Number(match[1]) };
  }
  function unexpiredProviderAuthorization(value, minimumRemainingSeconds) {
    var authorization = providerAuthorization(value);
    if (!authorization) return null;
    return authorization.expiresAt > (
      Math.floor(Date.now() / 1000) +
      Math.max(PROVIDER_TICKET_SKEW_SECONDS, Number(minimumRemainingSeconds) || 0)
    ) ? authorization : null;
  }
  function pageProviderAuthorization(minimumRemainingSeconds) {
    return unexpiredProviderAuthorization(
      root.__USER__ && root.__USER__.storage_provider_ticket,
      minimumRemainingSeconds
    );
  }
  function extensionProviderMarkerPresent() {
    return !!(
      document.documentElement &&
      document.documentElement.dataset.bwReaderExtensionProvider
    );
  }
  function reserveExtensionSyncOwner() {
    if (extensionProviderMarkerPresent()) {
      /*
       * Presence is latched for this document lifetime.  A provider port is a
       * storage transport, not the background SyncRuntime lease: MV3 worker
       * restart, CALL timeout or port reconnect must not revive a second PWA
       * server/direct owner.  A true no-extension PWA is established only by
       * a fresh page load on which the document_start marker is absent.
       */
      if (!extensionSyncOwnerReserved) {
        extensionSyncOwnerReserved = true;
        pwaSyncOwnerLifecycleGeneration += 1;
      }
    }
    return extensionSyncOwnerReserved;
  }
  function createExtensionSyncOwnerClaim(host) {
    if (!reserveExtensionSyncOwner()) return null;
    var registry = requireApi('dataRegistry', 'syncDigest');
    var audit = host && typeof host.audit === 'function'
      ? host.audit()
      : null;
    if (
      !host ||
      host.contract !== 'document-host/1' ||
      !audit ||
      audit.valid !== true ||
      ['pdf', 'epub', 'html', 'favorite'].indexOf(host.kind) < 0 ||
      registry.SYNC_CONTRACT !== 'sync-v3' ||
      registry.SYNC_CHANGE_CONTRACT !== 'record-parent-state/1'
    ) {
      var invalid = new Error(
        '无法证明书籍 PWA 已把同步所有权交给扩展后台'
      );
      invalid.code = 'BW_SYNC_OWNER_CLAIM';
      throw invalid;
    }
    /*
     * The claim is emitted only after both local owners have been
     * synchronously paused.  The marker/background bind it to the browser's
     * own tab/frame/document sender identity, so it cannot become an
     * account-wide lease or survive this document.
     */
    pausePwaDirectSync('extension-owner-claim');
    pausePwaSync('extension-owner-claim');
    return Object.freeze({
      contract: 'pwa-extension-owner-claim/1',
      runtimeContract: 'pwa-runtime/1',
      hostContract: host.contract,
      hostKind: host.kind,
      markerObserved: true,
      documentLifetime: true,
      pwaServerOwner: 'paused',
      pwaDirectOwner: 'paused',
      deviceFamilyId: persistentInstallId(),
      syncContract: registry.SYNC_CONTRACT,
      syncChangeContract: registry.SYNC_CHANGE_CONTRACT,
      registryDigest: String(registry.syncDigest() || '')
    });
  }
  function extensionReservedSyncControl() {
    if (reservedSyncControl) return reservedSyncControl;
    reservedSyncControl = Object.freeze({
      contract: 'sync-conflict-control/1',
      status: function () {
        var error = new Error(
          '扩展后台保留同步所有权，但当前状态通道不可用'
        );
        error.code = 'BW_SYNC_OWNER_RESERVED';
        error.retryable = true;
        return Promise.reject(error);
      },
      syncNow: function () {
        var error = new Error(
          '扩展后台保留同步所有权，但当前命令通道不可用'
        );
        error.code = 'BW_SYNC_OWNER_RESERVED';
        error.retryable = true;
        return Promise.reject(error);
      }
    });
    return reservedSyncControl;
  }
  function nextProviderLifecycleGeneration() {
    providerLifecycleGeneration += 1;
    return providerLifecycleGeneration;
  }
  function currentProviderLifecycle(generation) {
    return Number(generation) === providerLifecycleGeneration;
  }
  function queueProviderOperation(operation) {
    var task = providerOperationTail.catch(function () {}).then(operation);
    providerOperationTail = task.catch(function () {});
    return task;
  }
  function providerAuthError(message, code) {
    var error = new Error(String(message || '无法刷新扩展 Vault 授权证明'));
    error.code = code || 'BW_PROVIDER_AUTH';
    return error;
  }
  function refreshProviderAuthorization(options) {
    options = options || {};
    var lease = options.lease || accountLease;
    assertAccountLease(lease);
    if (options.preferFreshPageTicket) {
      var freshPageTicket = pageProviderAuthorization(
        PROVIDER_TICKET_BOOT_REFRESH_SECONDS
      );
      if (freshPageTicket) {
        assertAccountLease(lease);
        return Promise.resolve({
          ticket: freshPageTicket.ticket,
          expiresAt: freshPageTicket.expiresAt,
          source: 'page-fresh'
        });
      }
    }
    var fallback = pageProviderAuthorization();
    if (typeof root.fetch !== 'function') {
      return fallback
        ? Promise.resolve().then(function () {
          assertAccountLease(lease);
          return {
            ticket: fallback.ticket,
            expiresAt: fallback.expiresAt,
            source: 'page-offline'
          };
        })
        : Promise.reject(providerAuthError(
          '离线且页面中的扩展 Vault 授权证明已经过期',
          'BW_PROVIDER_AUTH_EXPIRED'
        ));
    }
    assertAccountLease(lease);
    return root.fetch('/api/reader/provider-ticket', {
      method: 'POST',
      headers: {
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'X-BW-Reader-Provider': '1'
      },
      body: JSON.stringify({ page: location.pathname }),
      credentials: 'same-origin',
      cache: 'no-store'
    }).then(function (response) {
      assertAccountLease(lease);
      return Promise.resolve(response.json()).catch(function () {
        throw providerAuthError('刷新扩展 Vault 授权时服务器返回了无效响应');
      }).then(function (data) {
        assertAccountLease(lease);
        var refreshed = providerAuthorization(data && data.ticket);
        if (
          !response.ok ||
          !data ||
          data.ok !== true ||
          data.storage_namespace !== namespace ||
          !refreshed ||
          Number(data.expires_at) !== refreshed.expiresAt ||
          refreshed.expiresAt <= Math.floor(Date.now() / 1000) + PROVIDER_TICKET_SKEW_SECONDS
        ) {
          throw providerAuthError(data && data.error || '刷新扩展 Vault 授权失败');
        }
        assertAccountLease(lease);
        if (root.__USER__) root.__USER__.storage_provider_ticket = refreshed.ticket;
        return {
          ticket: refreshed.ticket,
          expiresAt: refreshed.expiresAt,
          source: 'server'
        };
      });
    }).catch(function (error) {
      if (accountFenceError(error)) throw error;
      assertAccountLease(lease);
      /* 只有网络层失败才允许使用页面票据；401/403/格式错误代表在线授权失败，
       * 不能用旧票绕过服务器的账户状态判断。 */
      if (error && error.code === 'BW_PROVIDER_AUTH') throw error;
      if (fallback) {
        return {
          ticket: fallback.ticket,
          expiresAt: fallback.expiresAt,
          source: 'page-offline'
        };
      }
      throw providerAuthError(
        '无法在线刷新扩展 Vault 授权，且页面票据已经过期',
        'BW_PROVIDER_AUTH_EXPIRED'
      );
    });
  }
  function requireApi(name, method) {
    var found = api[name];
    if (!found || (method && typeof found[method] !== 'function')) {
      throw new Error('缺少 ReaderRuntime.' + name + (method ? '.' + method : ''));
    }
    return found;
  }
  function newInstallId() {
    var cryptoApi = root.crypto;
    if (!cryptoApi || typeof cryptoApi.getRandomValues !== 'function') {
      var unavailable = new Error('当前环境无法生成安全的 PWA 安装编号');
      unavailable.code = 'BW_PWA_INSTALL_ID';
      throw unavailable;
    }
    var bytes = new Uint8Array(16);
    cryptoApi.getRandomValues(bytes);
    /* UUID v4 位只用于方便排查格式；编号本身不包含账户或设备硬件信息。 */
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    return 'pwa-install-v1-' + Array.prototype.map.call(bytes, function (value) {
      return value.toString(16).padStart(2, '0');
    }).join('');
  }
  function persistentInstallId() {
    if (installId) return installId;
    var stored = '';
    try { stored = String(root.localStorage.getItem(INSTALL_ID_KEY) || '').trim(); }
    catch (_) {
      var unreadable = new Error('无法读取 PWA 安装编号');
      unreadable.code = 'BW_PWA_INSTALL_ID';
      throw unreadable;
    }
    if (/^pwa-install-v1-[a-f0-9]{32}$/.test(stored)) {
      installId = stored;
      return installId;
    }
    var generated = newInstallId();
    try {
      root.localStorage.setItem(INSTALL_ID_KEY, generated);
      stored = String(root.localStorage.getItem(INSTALL_ID_KEY) || '').trim();
    } catch (_) {
      var unwritable = new Error('无法持久化 PWA 安装编号');
      unwritable.code = 'BW_PWA_INSTALL_ID';
      throw unwritable;
    }
    if (!/^pwa-install-v1-[a-f0-9]{32}$/.test(stored)) {
      var invalid = new Error('PWA 安装编号持久化校验失败');
      invalid.code = 'BW_PWA_INSTALL_ID';
      throw invalid;
    }
    installId = stored;
    return installId;
  }
  function pausePwaNetworkOwners(reason) {
    pausePwaDirectSync(reason);
    pausePwaSync(reason);
  }
  function assertPwaNetworkOwnerLease(lease, expectedGeneration) {
    assertAccountLease(lease);
    if (
      extensionSyncOwnerReserved ||
      reserveExtensionSyncOwner() ||
      !pwaSyncOwnerLease ||
      typeof pwaSyncOwnerLease.assertActive !== 'function'
    ) {
      var inactive = new Error('PWA 同步 owner 未持有活动租约');
      inactive.code = 'BW_SYNC_OWNER_INACTIVE';
      inactive.retryable = true;
      throw inactive;
    }
    pwaSyncOwnerLease.assertActive();
    if (
      expectedGeneration != null &&
      Number(expectedGeneration) !== pwaSyncOwnerLifecycleGeneration
    ) {
      var stale = new Error('PWA 同步 owner 生命周期已经变化');
      stale.code = 'BW_SYNC_OWNER_INACTIVE';
      stale.retryable = true;
      throw stale;
    }
    assertAccountLease(lease);
    return true;
  }
  function onPwaOwnerLeaseAcquired(lease, fields) {
    void fields;
    try {
      assertAccountLease(lease);
      if (reserveExtensionSyncOwner()) {
        pausePwaNetworkOwners('extension-owner-reserved');
        return false;
      }
      pwaSyncOwnerLifecycleGeneration += 1;
      assertPwaNetworkOwnerLease(lease);
      if (!pwaNetworkRuntimeReady) return true;
      resumePwaSync('pwa-owner-lease-acquired');
      resumePwaDirectSync('pwa-owner-lease-acquired');
      emit('bw:reader-sync-owner-status', {
        contract: 'owner-lease/1',
        owner: 'pwa',
        state: 'active'
      });
      return true;
    } catch (error) {
      pausePwaNetworkOwners('pwa-owner-lease-invalid');
      return false;
    }
  }
  function onPwaOwnerLeaseLost(lease, error) {
    try { assertAccountLease(lease); } catch (_) { return; }
    pwaSyncOwnerLifecycleGeneration += 1;
    if (!pwaNetworkRuntimeReady) return;
    pausePwaNetworkOwners('pwa-owner-lease-lost');
    emit('bw:reader-sync-owner-status', {
      contract: 'owner-lease/1',
      owner: 'pwa',
      state: 'waiting',
      code: publicSyncErrorCode(error || {
        code: 'BW_SYNC_OWNER_INACTIVE'
      })
    });
  }
  function createPwaSyncOwnerLease(deviceId, lease) {
    assertAccountLease(lease);
    var ownerModule = requireApi(
      'syncOwnerLease',
      'createSyncOwnerLease'
    );
    var registry = requireApi('dataRegistry', 'syncDigest');
    if (
      ownerModule.CONTRACT !== 'owner-lease/1' ||
      typeof ownerModule.createOwnerInstanceId !== 'function'
    ) {
      var invalid = new Error('PWA 缺少完整 owner-lease/1 合同');
      invalid.code = 'BW_SYNC_OWNER_CONFIG';
      throw invalid;
    }
    var ownerInstanceId = ownerModule.createOwnerInstanceId(
      'pwa',
      root.crypto
    );
    pwaSyncOwnerLifecycleGeneration += 1;
    pwaSyncOwnerLease = ownerModule.createSyncOwnerLease({
      origin: location.origin,
      credentials: 'same-origin',
      fetch: typeof root.fetch === 'function' ? root.fetch.bind(root) : null,
      ownerNamespace: namespace,
      deviceId: deviceId,
      deviceFamilyId: persistentInstallId(),
      ownerRole: 'pwa',
      ownerInstanceId: ownerInstanceId,
      syncContract: registry.SYNC_CONTRACT,
      syncChangeContract: registry.SYNC_CHANGE_CONTRACT,
      registryDigest: registry.syncDigest(),
      crypto: root.crypto,
      onAcquired: function (fields) {
        onPwaOwnerLeaseAcquired(lease, fields);
      },
      onLost: function (error) {
        onPwaOwnerLeaseLost(lease, error);
      }
    });
    assertAccountLease(lease);
    return pwaSyncOwnerLease;
  }
  function startPwaSyncOwnerLease(lease) {
    assertAccountLease(lease);
    if (reserveExtensionSyncOwner()) return Promise.resolve(false);
    pausePwaNetworkOwners('pwa-owner-lease-pending');
    if (!pwaSyncOwnerLease || typeof pwaSyncOwnerLease.start !== 'function') {
      return Promise.resolve(false);
    }
    return Promise.resolve(pwaSyncOwnerLease.start()).then(function () {
      assertPwaNetworkOwnerLease(lease);
      return true;
    }).catch(function (error) {
      if (accountFenceError(error)) throw error;
      pausePwaNetworkOwners('pwa-owner-lease-waiting');
      emit('bw:reader-sync-owner-status', {
        contract: 'owner-lease/1',
        owner: 'pwa',
        state: 'waiting',
        code: publicSyncErrorCode(error)
      });
      return false;
    });
  }
  function destroyPwaSyncOwnerLease(reason, owner, requestOptions) {
    owner = owner || pwaSyncOwnerLease;
    if (!owner || typeof owner.destroy !== 'function') {
      return Promise.resolve(false);
    }
    try {
      return Promise.resolve(
        owner.destroy(reason, requestOptions)
      ).catch(function () {
        return false;
      });
    } catch (_) {
      return Promise.resolve(false);
    }
  }
  function waitForPageOwnershipRelease(operation) {
    if (typeof root.setTimeout !== 'function') {
      return Promise.resolve(false);
    }
    return new Promise(function (resolve) {
      var settled = false;
      var timer = null;
      function finish(completed) {
        if (settled) return;
        settled = true;
        if (timer != null && typeof root.clearTimeout === 'function') {
          root.clearTimeout(timer);
        }
        resolve(completed);
      }
      timer = root.setTimeout(function () {
        finish(false);
      }, PAGE_OWNERSHIP_RELEASE_WAIT_MS);
      Promise.resolve(operation).then(function () {
        finish(true);
      }, function () {
        finish(true);
      });
    });
  }
  function dbName(scope) {
    return 'bw-reader-pwa-v1-' + namespace + '-' + scope;
  }
  function currentHost() {
    try {
      var host = root.RC && RC.documentHost && RC.documentHost.current && RC.documentHost.current();
      if (host && host.audit && host.audit().valid) return host;
    } catch (_) {}
    return null;
  }
  function emit(name, detail) {
    try { document.dispatchEvent(new CustomEvent(name, { detail: detail || {} })); } catch (_) {}
  }
  function setMode(value) {
    try { document.documentElement.dataset.bwReaderRuntime = value; } catch (_) {}
  }
  function ensurePreferenceStore(lease) {
    assertAccountLease(lease);
    if (preferences) return preferences;
    var module = requireApi('preferenceStore', 'createPreferenceStore');
    var registry = requireApi('dataRegistry', 'settingMigrations');
    preferences = module.createPreferenceStore({
      accountContext: accountContext(),
      dataRegistry: registry,
      storage: root.localStorage,
      lease: lease,
      eventTarget: document
    });
    root.__BW_READER_PREFERENCES__ = preferences;
    assertAccountLease(lease);
    return preferences;
  }

  function waitForPreferenceHydration(lease) {
    assertAccountLease(lease);
    return Promise.resolve(preferenceHydration).then(function () {
      assertAccountLease(lease);
      if (preferenceHydrationError) throw preferenceHydrationError;
      return true;
    });
  }

  function closeStores(stores) {
    Object.keys(stores || {}).forEach(function (scope) {
      var store = stores[scope];
      if (store && typeof store.close === 'function') {
        try { store.close(); } catch (_) {}
      }
    });
  }
  function mergeSyncCheckpoint(current, incoming) {
    if (
      !current ||
      current.contract !== incoming.contract ||
      current.registryDigest !== incoming.registryDigest
    ) return incoming;
    var merged = JSON.parse(JSON.stringify(incoming));
    merged.generation = Math.max(
      Number(current.generation) || 0,
      Number(incoming.generation) || 0
    );
    merged.server = merged.server || {};
    current.server = current.server || {};
    var currentReconciliationEpoch = Math.max(
      0,
      Number(current.server.reconciliationEpoch) || 0
    );
    var incomingReconciliationEpoch = Math.max(
      0,
      Number(merged.server.reconciliationEpoch) || 0
    );
    if (incomingReconciliationEpoch < currentReconciliationEpoch) {
      merged.server = JSON.parse(JSON.stringify(current.server));
    } else if (incomingReconciliationEpoch === currentReconciliationEpoch) {
      merged.server.localCursor = Math.max(
        Number(current.server.localCursor) || 0,
        Number(merged.server.localCursor) || 0
      );
      merged.server.remoteCursor = Math.max(
        Number(current.server.remoteCursor) || 0,
        Number(merged.server.remoteCursor) || 0
      );
      merged.server.reconciliationEpoch = currentReconciliationEpoch;
    } else {
      /* A successful full snapshot is the only operation allowed to advance
       * this epoch. It intentionally authorizes cursors to move backwards
       * after a server/store reset; a stale tab with an older epoch can no
       * longer overwrite that recovered checkpoint. */
      merged.server.localCursor = Math.max(
        0,
        Number(merged.server.localCursor) || 0
      );
      merged.server.remoteCursor = Math.max(
        0,
        Number(merged.server.remoteCursor) || 0
      );
      merged.server.reconciliationEpoch = incomingReconciliationEpoch;
    }
    merged.peers = merged.peers || {};
    Object.keys(current.peers || {}).forEach(function (peerId) {
      var previous = current.peers[peerId] || {};
      var next = merged.peers[peerId] || {};
      merged.peers[peerId] = {
        sentCursor: Math.max(
          Number(previous.sentCursor) || 0,
          Number(next.sentCursor) || 0
        ),
        receivedCursor: Math.max(
          Number(previous.receivedCursor) || 0,
          Number(next.receivedCursor) || 0
        ),
        baselineReady: previous.baselineReady === true ||
          next.baselineReady === true
      };
    });
    return merged;
  }
  function syncCheckpointError(message, code, retryable) {
    var error = new Error(message);
    error.code = code || 'BW_SYNC_CHECKPOINT';
    error.retryable = retryable === true;
    return error;
  }
  function checkedVaultEpoch(value) {
    value = String(value || '');
    if (!/^data-store-instance-v1-[a-f0-9]{32}$/.test(value)) {
      throw syncCheckpointError(
        'PWA 数据 Vault 实例编号无效，已停止增量同步',
        'BW_DATA_INSTANCE_EPOCH'
      );
    }
    return value;
  }
  function checkedCoordinatorCheckpoint(value) {
    if (
      !value ||
      typeof value !== 'object' ||
      Array.isArray(value) ||
      value.contract !== 'sync-coordinator/1'
    ) {
      throw syncCheckpointError('PWA 同步游标损坏，已停止增量同步');
    }
    return JSON.parse(JSON.stringify(value));
  }
  function checkpointFromEnvelope(value, vaultEpoch) {
    if (
      value &&
      typeof value === 'object' &&
      !Array.isArray(value) &&
      (
        (
          value.schema === 1 &&
          value.checkpoint &&
          typeof value.checkpoint === 'object' &&
          !Array.isArray(value.checkpoint) &&
          value.checkpoint.contract === 'sync-coordinator/1'
        ) ||
        value.contract === 'sync-coordinator/1'
      ) &&
      value.contract !== SYNC_CHECKPOINT_CONTRACT
    ) {
      /* v1 checkpoint 没有绑定 records Vault，绝不能继承它的游标。 */
      return null;
    }
    if (
      !value ||
      typeof value !== 'object' ||
      Array.isArray(value) ||
      value.contract !== SYNC_CHECKPOINT_CONTRACT ||
      value.schema !== SYNC_CHECKPOINT_SCHEMA ||
      typeof value.vaultEpoch !== 'string'
    ) {
      throw syncCheckpointError('PWA 同步游标损坏，已停止增量同步');
    }
    if (value.vaultEpoch !== vaultEpoch) return null;
    return checkedCoordinatorCheckpoint(value.checkpoint);
  }
  function createSyncCheckpointStore(
    deviceStore,
    dataStore,
    deviceId,
    lease
  ) {
    var queue = Promise.resolve();
    var observedVaultEpoch = '';
    function readVaultEpoch() {
      assertAccountLease(lease);
      if (!dataStore || typeof dataStore.instanceEpoch !== 'function') {
        return Promise.reject(syncCheckpointError(
          'PWA 数据 Vault 缺少实例编号接口',
          'BW_DATA_INSTANCE_EPOCH'
        ));
      }
      return Promise.resolve(dataStore.instanceEpoch()).then(function (value) {
        assertAccountLease(lease);
        return checkedVaultEpoch(value);
      });
    }
    function loadRecord() {
      assertAccountLease(lease);
      return deviceStore.get(
        'ui-session',
        SYNC_CHECKPOINT_ID,
        { includeDeleted: true }
      ).then(function (record) {
        assertAccountLease(lease);
        return record || null;
      });
    }
    return {
      load: function () {
        return queue.catch(function () {}).then(function () {
          return readVaultEpoch().then(function (vaultEpoch) {
            return loadRecord().then(function (record) {
              return readVaultEpoch().then(function (confirmedEpoch) {
                if (confirmedEpoch !== vaultEpoch) {
                  observedVaultEpoch = confirmedEpoch;
                  return { record: null, vaultEpoch: confirmedEpoch };
                }
                observedVaultEpoch = vaultEpoch;
                return { record: record, vaultEpoch: vaultEpoch };
              });
            });
          });
        }).then(function (loaded) {
          var record = loaded.record;
          if (!record || record.deleted) return null;
          return checkpointFromEnvelope(record.value, loaded.vaultEpoch);
        });
      },
      save: function (value) {
        var operation = queue.catch(function () {}).then(function () {
          var attempt = 0;
          function write() {
            attempt += 1;
            return readVaultEpoch().then(function (vaultEpoch) {
              if (
                observedVaultEpoch &&
                observedVaultEpoch !== vaultEpoch
              ) {
                throw syncCheckpointError(
                  'PWA 数据 Vault 已重建，请重新启动同步',
                  'BW_SYNC_CHECKPOINT_EPOCH',
                  true
                );
              }
              observedVaultEpoch = vaultEpoch;
              return loadRecord().then(function (record) {
                var current = null;
                if (record && !record.deleted) {
                  current = checkpointFromEnvelope(
                    record.value,
                    vaultEpoch
                  );
                }
                var checkpoint = mergeSyncCheckpoint(
                  current,
                  checkedCoordinatorCheckpoint(value)
                );
                var mutationId = [
                  'pwa-sync-checkpoint-v2',
                  deviceId,
                  Date.now(),
                  ++syncCheckpointSequence
                ].join(':');
                return deviceStore.put('ui-session', {
                  id: SYNC_CHECKPOINT_ID,
                  contract: SYNC_CHECKPOINT_CONTRACT,
                  schema: SYNC_CHECKPOINT_SCHEMA,
                  vaultEpoch: vaultEpoch,
                  checkpoint: checkpoint
                }, {
                  id: SYNC_CHECKPOINT_ID,
                  ifRev: Number(record && record.rev) || 0,
                  mutationId: mutationId
                }).then(function () {
                  assertAccountLease(lease);
                  return readVaultEpoch();
                }).then(function (confirmedEpoch) {
                  if (confirmedEpoch !== vaultEpoch) {
                    throw syncCheckpointError(
                      'PWA 数据 Vault 已重建，请重新启动同步',
                      'BW_SYNC_CHECKPOINT_EPOCH',
                      true
                    );
                  }
                }).catch(function (error) {
                  if (
                    error &&
                    error.code === 'BW_DATA_CONFLICT' &&
                    attempt < 5
                  ) {
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
  function createPwaSync(deviceId, lease) {
    assertAccountLease(lease);
    var transportModule = requireApi(
      'serverSyncTransport',
      'createServerSyncTransport'
    );
    var gatewayModule = requireApi('syncGateway', 'createSyncGateway');
    var runtimeModule = requireApi('syncRuntime', 'createSyncRuntime');
    var conflictModule = requireApi(
      'syncConflictControl',
      'createSyncConflictControl'
    );
    var coordinatorModule = requireApi(
      'syncCoordinator',
      'createSyncCoordinator'
    );
    var registry = requireApi('dataRegistry', 'syncCollections');
    var transport = transportModule.createServerSyncTransport({
      origin: location.origin,
      ownerNamespace: namespace,
      deviceId: deviceId,
      syncContract: registry.SYNC_CONTRACT,
      syncChangeContract: registry.SYNC_CHANGE_CONTRACT,
      registryDigest: registry.syncDigest(),
      ownerLease: pwaSyncOwnerLease,
      credentials: 'same-origin',
      fetch: typeof root.fetch === 'function' ? root.fetch.bind(root) : null
    });
    pwaSyncGateway = gatewayModule.createSyncGateway({
      transport: transport,
      deviceId: deviceId
    });
    pwaSyncRuntime = runtimeModule.createSyncRuntime({
      coordinatorApi: coordinatorModule,
      store: localStores.global,
      registry: registry,
      serverGateway: pwaSyncGateway,
      checkpointStore: createSyncCheckpointStore(
        localStores.device,
        localStores.global,
        deviceId,
        lease
      ),
      onlineTarget: root,
      assertLease: function () {
        return assertPwaNetworkOwnerLease(lease);
      },
      onResult: function (result, error, reason) {
        /*
         * SyncRuntime 的 raw result 可能包含记录、peer/session 或上游错误。
         * 页面世界只接收 sync-conflict-control/1 的有界摘要。
         */
        void result;
        void reason;
        emitPwaSyncStatus(error);
      }
    });
    pwaSyncControl = conflictModule.createSyncConflictControl({
      runtime: pwaSyncRuntime,
      owner: 'pwa',
      crypto: root.crypto,
      assertFence: function () {
        assertAccountLease(lease);
        return true;
      },
      collections: registry.syncCollections()
    });
    assertAccountLease(lease);
    return pwaSyncRuntime;
  }
  function createPwaDirectSync(deviceId, lease) {
    assertAccountLease(lease);
    if (
      typeof root.RTCPeerConnection !== 'function' ||
      !root.navigator ||
      !root.navigator.locks ||
      typeof root.navigator.locks.request !== 'function'
    ) {
      emit('bw:reader-direct-sync-status', {
        owner: 'pwa',
        state: 'unavailable',
        code: 'BW_DIRECT_HOST_UNAVAILABLE'
      });
      return null;
    }
    var signalModule = requireApi(
      'directSyncSignalTransport',
      'createDirectSignalTransport'
    );
    var hostModule = requireApi('directSyncHost', 'createDirectSyncHost');
    var leaderModule = requireApi(
      'directSyncLeader',
      'createDirectSyncLeader'
    );
    var directModule = requireApi(
      'directSyncProtocol',
      'createChannelTransport'
    );
    var gatewayModule = requireApi('syncGateway', 'createSyncGateway');
    var registry = requireApi('dataRegistry', 'syncDigest');
    if (
      registry.SYNC_CONTRACT !== 'sync-v3' ||
      registry.SYNC_CHANGE_CONTRACT !== 'record-parent-state/1' ||
      typeof registry.syncDescriptor !== 'function'
    ) {
      var invalidRegistry = new Error(
        'PWA 直连要求完整 DataRegistry sync-v3 因果合同'
      );
      invalidRegistry.code = 'BW_DIRECT_HOST_REGISTRY';
      throw invalidRegistry;
    }
    if (
      typeof directModule.createStoreRelay !== 'function'
    ) {
      var invalidDirect = new Error(
        'PWA 直连要求可围栏的 store relay'
      );
      invalidDirect.code = 'BW_DIRECT_HOST_REGISTRY';
      throw invalidDirect;
    }
    var registryDigest = String(registry.syncDigest() || '');
    var rawRelay = directModule.createStoreRelay({
      store: localStores.global,
      registry: registry
    });
    if (!rawRelay || typeof rawRelay.exchange !== 'function') {
      var invalidRelay = new Error('PWA 直连 store relay 无效');
      invalidRelay.code = 'BW_DIRECT_HOST_REGISTRY';
      throw invalidRelay;
    }
    var fencedRelay = {
      exchange: function (request) {
        var expectedGeneration = pwaSyncOwnerLifecycleGeneration;
        assertAccountLease(lease);
        if (!pwaSyncOwnerLease) {
          return Promise.reject((function () {
            var missing = new Error('PWA 直连缺少 owner lease');
            missing.code = 'BW_SYNC_OWNER_INACTIVE';
            return missing;
          })());
        }
        return Promise.resolve(
          pwaSyncOwnerLease.ensureActive()
        ).then(function () {
          assertPwaNetworkOwnerLease(lease, expectedGeneration);
          return rawRelay.exchange(request);
        }).then(function (result) {
          assertPwaNetworkOwnerLease(lease, expectedGeneration);
          return result;
        });
      }
    };
    var transport = signalModule.createDirectSignalTransport({
      origin: location.origin,
      ownerNamespace: namespace,
      deviceId: deviceId,
      registryDigest: registryDigest,
      ownerLease: pwaSyncOwnerLease,
      credentials: 'same-origin',
      fetch: typeof root.fetch === 'function' ? root.fetch.bind(root) : null
    });
    pwaDirectHost = hostModule.createDirectSyncHost({
      deviceId: deviceId,
      registryDigest: registryDigest,
      signalTransport: transport,
      syncRuntime: pwaSyncRuntime,
      store: localStores.global,
      registry: registry,
      relay: fencedRelay,
      directProtocolApi: directModule,
      syncGatewayApi: gatewayModule,
      RTCPeerConnection: root.RTCPeerConnection,
      crypto: root.crypto,
      /* Do not silently send reading metadata to a public STUN/TURN service.
       * The reliable server lane stays active until a trusted ICE service is
       * explicitly configured. Host candidates still support LAN/Tailscale. */
      iceServers: [],
      assertLease: function () {
        return assertPwaNetworkOwnerLease(lease);
      },
      onStatus: function (status) {
        emit('bw:reader-direct-sync-status', {
          owner: 'pwa',
          status: status
        });
      }
    });
    pwaDirectLeader = leaderModule.createDirectSyncLeader({
      locks: root.navigator.locks,
      host: pwaDirectHost,
      lockName: [
        'bw-reader-direct-v1',
        namespace,
        deviceId
      ].join(':'),
      assertLease: function () {
        return assertPwaNetworkOwnerLease(lease);
      },
      onStatus: function (status) {
        emit('bw:reader-direct-sync-leader', {
          owner: 'pwa',
          status: status
        });
      }
    });
    assertAccountLease(lease);
    return pwaDirectLeader;
  }
  function pausePwaSync(reason) {
    if (pwaSyncRuntime && typeof pwaSyncRuntime.pause === 'function') {
      pwaSyncRuntime.pause(reason || 'pwa-sync-paused');
    }
  }
  function resumePwaSync(reason) {
    if (reserveExtensionSyncOwner()) return false;
    try { assertPwaNetworkOwnerLease(accountLease); }
    catch (_) { return false; }
    if (pwaSyncRuntime && typeof pwaSyncRuntime.resume === 'function') {
      return pwaSyncRuntime.resume(reason || 'pwa-sync-resumed');
    }
    return false;
  }
  function pausePwaDirectSync(reason) {
    if (pwaDirectLeader && typeof pwaDirectLeader.pause === 'function') {
      pwaDirectLeader.pause(reason || 'pwa-direct-paused');
    }
  }
  function resumePwaDirectSync(reason) {
    if (reserveExtensionSyncOwner()) return false;
    try { assertPwaNetworkOwnerLease(accountLease); }
    catch (_) { return false; }
    if (pwaDirectLeader && typeof pwaDirectLeader.resume === 'function') {
      return pwaDirectLeader.resume(reason || 'pwa-direct-resumed');
    }
    return false;
  }
  function createStores(deviceId) {
    var indexed = requireApi('indexedDBStore', 'createIndexedDBDataStore');
    var registry = requireApi('dataRegistry', 'syncCollections');
    var causalCollections = registry.syncCollections();
    var stores = {};
    try {
      stores.global = indexed.createIndexedDBDataStore({
        dbName: dbName('global'),
        deviceId: deviceId,
        channelName: dbName('global') + '-events',
        causalCollections: causalCollections
      });
      stores.document = indexed.createIndexedDBDataStore({
        dbName: dbName('document'),
        deviceId: deviceId,
        channelName: dbName('document') + '-events',
        causalCollections: []
      });
      stores.device = indexed.createIndexedDBDataStore({
        dbName: dbName('device'),
        deviceId: deviceId,
        channelName: dbName('device') + '-events',
        causalCollections: []
      });
      return stores;
    } catch (error) {
      closeStores(stores);
      throw error;
    }
  }

  function attachProviderNow(provider, lease, generation) {
    lease = lease || accountLease;
    assertAccountLease(lease);
    pendingProvider = null;
    pendingProviderGeneration = 0;
    providerSyncControl = null;
    /*
     * UI/runtime-ready 不等设置水合，但 global provider 切换必须等它结束。
     * 否则同一次 PreferenceStore 初始化可能先写 PWA、后写扩展，形成两个真源。
     */
    return waitForPreferenceHydration(lease).then(function () {
      assertAccountLease(lease);
      if (!currentProviderLifecycle(generation)) {
        return { connected: false, reason: 'stale-provider-lifecycle' };
      }
      return runtime.attachExtension(provider);
    }).then(function (result) {
      if (!currentProviderLifecycle(generation)) {
        return { connected: false, reason: 'stale-provider-lifecycle' };
      }
      try {
        assertAccountLease(lease);
      } catch (error) {
        if (runtime && typeof runtime.detachExtension === 'function') {
          try {
            Promise.resolve(
              runtime.detachExtension('account-context-stale')
            ).catch(function () {});
          } catch (_) {}
        }
        throw error;
      }
      if (result.connected) {
        if (
          provider.syncControl &&
          provider.syncControl.contract === 'sync-conflict-control/1' &&
          typeof provider.syncControl.status === 'function'
        ) {
          providerSyncControl = provider.syncControl;
        }
        setMode('pwa-extension-provider');
        emit('bw:reader-runtime-provider-attached', result);
      } else {
        providerSyncControl = null;
        if (!(result && Array.isArray(result.conflicts) && result.conflicts.length)) {
          resumePwaSync('extension-provider-not-connected');
          resumePwaDirectSync('extension-provider-not-connected');
        }
        setMode('pwa-fallback-conflict');
        emit('bw:reader-runtime-provider-conflict', result);
      }
      return result;
    }).catch(function (error) {
      if (!currentProviderLifecycle(generation)) {
        return { connected: false, reason: 'stale-provider-lifecycle' };
      }
      providerSyncControl = null;
      if (accountFenceError(error)) throw error;
      assertAccountLease(lease);
      resumePwaSync('extension-provider-error');
      resumePwaDirectSync('extension-provider-error');
      setMode('pwa-fallback-provider-error');
      emit('bw:reader-runtime-provider-error', {
        code: error && error.code || 'BW_PROVIDER_ATTACH',
        error: String(error && error.message || error)
      });
      return { connected: false, reason: String(error && error.message || error) };
    });
  }
  function attachProvider(provider, lease, generation) {
    lease = lease || accountLease;
    assertAccountLease(lease);
    extensionSyncOwnerReserved = true;
    generation = Number.isSafeInteger(Number(generation)) && Number(generation) > 0
      ? Number(generation)
      : nextProviderLifecycleGeneration();
    if (!runtime || !provider) {
      pendingProvider = provider || pendingProvider;
      pendingProviderGeneration = generation;
      return Promise.resolve({ connected: false, reason: 'runtime-not-ready' });
    }
    /*
     * Stop the PWA owner synchronously when takeover is announced.  The
     * serialized provider operation may wait for preference hydration, but
     * no server/direct work may run during that wait.
     */
    pausePwaDirectSync('extension-provider-attaching');
    pausePwaSync('extension-provider-attaching');
    return queueProviderOperation(function () {
      return attachProviderNow(provider, lease, generation);
    });
  }

  function cleanupFailedBoot(error, lease) {
    var failedRuntime = runtime;
    var failedStores = localStores;
    var failedPreferences = preferences;
    var failedSyncRuntime = pwaSyncRuntime;
    var failedSyncOwnerLease = pwaSyncOwnerLease;
    var failedDirectLeader = pwaDirectLeader;
    var failedDirectHost = pwaDirectHost;
    var context = null;
    try { context = accountContext(); } catch (_) {}
    if (context && lease && context.isCurrent(lease)) {
      try { context.deactivate('pwa-runtime-boot-failed'); } catch (_) {}
    }
    runtime = null;
    localStores = null;
    pwaSyncGateway = null;
    pwaSyncRuntime = null;
    pwaSyncControl = null;
    pwaSyncOwnerLease = null;
    pwaSyncOwnerLifecycleGeneration += 1;
    pwaNetworkRuntimeReady = false;
    providerSyncControl = null;
    nextProviderLifecycleGeneration();
    pendingProviderGeneration = 0;
    pwaDirectHost = null;
    pwaDirectLeader = null;
    preferences = null;
    preferenceHydration = Promise.resolve();
    preferenceHydrationError = null;
    namespace = '';
    accountLease = null;
    pendingProvider = null;
    if (root.__BW_READER_RUNTIME__ === failedRuntime) {
      try { delete root.__BW_READER_RUNTIME__; }
      catch (_) { root.__BW_READER_RUNTIME__ = null; }
    }
    if (root.__BW_READER_PREFERENCES__ === failedPreferences) {
      try { delete root.__BW_READER_PREFERENCES__; }
      catch (_) { root.__BW_READER_PREFERENCES__ = null; }
    }
    try { delete document.documentElement.dataset.bwReaderUiOwner; } catch (_) {}

    var detach = failedRuntime && typeof failedRuntime.detachExtension === 'function'
      ? Promise.resolve().then(function () {
        return failedRuntime.detachExtension('pwa-runtime-boot-failed');
      }).catch(function () {})
      : Promise.resolve();
    return detach.then(function () {
      if (failedPreferences && typeof failedPreferences.destroy === 'function') {
        try { failedPreferences.destroy(); } catch (_) {}
      }
      /*
       * Stop the RTC owner before destroying the durable runtime it calls
       * into.  This closes DataChannels and cancels leader work while the
       * coordinator is still valid, so late peer callbacks cannot target an
       * already-destroyed SyncRuntime during a failed boot.
       */
      if (failedDirectLeader && typeof failedDirectLeader.destroy === 'function') {
        try { failedDirectLeader.destroy('pwa-runtime-boot-failed'); } catch (_) {}
      } else if (failedDirectHost && typeof failedDirectHost.destroy === 'function') {
        try { failedDirectHost.destroy('pwa-runtime-boot-failed'); } catch (_) {}
      }
      if (failedSyncRuntime && typeof failedSyncRuntime.destroy === 'function') {
        try { failedSyncRuntime.destroy('pwa-runtime-boot-failed'); } catch (_) {}
      }
      destroyPwaSyncOwnerLease(
        'pwa-runtime-boot-failed',
        failedSyncOwnerLease
      );
      closeStores(failedStores);
      var bridge = api.extensionProvider;
      if (bridge && typeof bridge.disconnect === 'function') {
        try { bridge.disconnect('pwa-runtime-boot-failed'); } catch (_) {}
      }
      return error;
    });
  }

  function startForHost(host) {
    if (bootPromise) return bootPromise;
    var bootLease = null;
    bootPromise = Promise.resolve().then(function () {
      bootLease = activateServerAccount();
      assertAccountLease(bootLease);
      reserveExtensionSyncOwner();
      ensurePreferenceStore(bootLease);
      localStores = createStores(persistentInstallId());
      createPwaSyncOwnerLease(installId, bootLease);
      createPwaSync(installId, bootLease);
      createPwaDirectSync(installId, bootLease);
    }).then(function () {
      assertAccountLease(bootLease);
      var selector = requireApi('runtimeSelector', 'createReaderRuntime');
      var registry = requireApi('dataRegistry', 'scopes');
      runtime = selector.createReaderRuntime({
        documentHost: host,
        pwaStore: localStores.global,
        pwaDocumentStore: localStores.document,
        pwaDeviceStore: localStores.device,
        pwaSyncGateway: pwaSyncGateway,
        scopes: registry.scopes(),
        ui: {
          mount: function () {
            document.documentElement.dataset.bwReaderUiOwner = 'pwa';
            emit('bw:reader-runtime-ui-owned', { owner: 'pwa' });
            return { owner: 'pwa', existingUi: true };
          }
        }
      });
      root.__BW_READER_RUNTIME__ = runtime;
      setMode('pwa-fallback');
      if (!runtime.storage || typeof runtime.storage !== 'function') {
        var missingStorage = new Error('ReaderRuntime 缺少 storage()');
        missingStorage.code = 'BW_PREFERENCE_ROUTER';
        throw missingStorage;
      }
      /*
       * 首屏账户镜像已在本脚本加载时同步切换。IndexedDB/provider 水合异步进行，
       * 不阻塞阅读器 UI 与 runtime-ready；失败保留当前账户镜像并显式上报。
      */
      preferenceHydrationError = null;
      var hydrationPreferences = preferences;
      preferenceHydration = Promise.resolve(
        hydrationPreferences.attach(runtime.storage(), bootLease)
      ).then(function () {
        assertAccountLease(bootLease);
        return true;
      }).catch(function (error) {
        /* 启动失败后旧水合可能晚到；不能污染下一次 boot 的状态。 */
        if (preferences !== hydrationPreferences) return false;
        preferenceHydrationError = error;
        if (!accountFenceError(error)) {
          try {
            assertAccountLease(bootLease);
            emit('bw:reader-preference-error', {
              phase: 'attach',
              code: error && error.code || 'BW_PREFERENCE_ATTACH',
              error: String(error && error.message || error)
            });
          } catch (_) {}
        }
        return false;
      });
      return runtime.start();
    }).then(function () {
      assertAccountLease(bootLease);
      var bridge = api.extensionProvider;
      if (
        extensionProviderMarkerPresent() &&
        bridge &&
        typeof bridge.start === 'function'
      ) {
        /*
         * 握手本身不读写 DataStore，可立即进行；真正 attachExtension 会在
         * attachProvider() 中等待 PreferenceStore 水合，避免切库竞态。
         */
        var ownerClaim = createExtensionSyncOwnerClaim(host);
        return refreshProviderAuthorization({
          preferFreshPageTicket: true,
          lease: bootLease
        }).then(function (authorization) {
          assertAccountLease(bootLease);
          bridge.start({
            namespace: namespace,
            ticket: authorization.ticket,
            helloPayload: {
              syncOwnerClaim: ownerClaim
            }
          });
          if (
            !pendingProvider &&
            bridge.current &&
            bridge.current()
          ) {
            pendingProvider = bridge.current();
            pendingProviderGeneration = nextProviderLifecycleGeneration();
          }
        }).catch(function (error) {
          if (accountFenceError(error)) throw error;
          assertAccountLease(bootLease);
          emit('bw:reader-runtime-provider-error', {
            code: error && error.code || 'BW_PROVIDER_AUTH',
            error: String(error && error.message || error)
          });
        });
      }
      return null;
    }).then(function () {
      assertAccountLease(bootLease);
      return pendingProvider
        ? attachProvider(
          pendingProvider,
          bootLease,
          pendingProviderGeneration || undefined
        )
        : null;
    }).then(function () {
      assertAccountLease(bootLease);
      pwaNetworkRuntimeReady = true;
      if (
        !runtime ||
        (
          runtime.mode() !== 'pwa-extension-provider' &&
          runtime.mode() !== 'pwa-fallback-conflict'
        )
      ) {
        startPwaSyncOwnerLease(bootLease).catch(function (error) {
          if (accountFenceError(error)) return;
          pausePwaNetworkOwners('pwa-owner-lease-error');
        });
      }
      emit('bw:reader-runtime-ready', {
        namespace: namespace,
        installId: installId,
        deviceId: installId,
        mode: runtime.mode(),
        documentKind: host.kind,
        documentId: host.documentId
      });
      return runtime;
    }).catch(function (error) {
      return cleanupFailedBoot(error, bootLease).then(function () {
        bootPromise = null;
        setMode('error');
        emit('bw:reader-runtime-error', {
          code: error && error.code || 'BW_PWA_RUNTIME_BOOT',
          error: String(error && error.message || error)
        });
        throw error;
      });
    });
    return bootPromise;
  }

  function tryStart() {
    if (runtime || bootPromise) return;
    var host = currentHost();
    if (!host) return;
    if (document.readyState === 'loading' || !root.__USER__) {
      document.addEventListener('DOMContentLoaded', tryStart, { once: true });
      return;
    }
    startForHost(host).catch(function () {});
  }

  document.addEventListener('bw:document-host-ready', tryStart);
  document.addEventListener('bw:extension-provider-ready', function (event) {
    var provider = event && event.detail && event.detail.provider;
    if (!provider) return;
    var lease;
    try { lease = accountContext().lease(); } catch (_) { return; }
    var generation = nextProviderLifecycleGeneration();
    extensionSyncOwnerReserved = true;
    pwaSyncOwnerLifecycleGeneration += 1;
    pausePwaNetworkOwners('extension-provider-ready');
    destroyPwaSyncOwnerLease('extension-provider-ready');
    providerAuthRecoveryAttempted = false;
    providerAuthRecoveryPending = false;
    pendingProvider = provider;
    pendingProviderGeneration = generation;
    if (runtime) {
      attachProvider(provider, lease, generation).catch(function (error) {
        if (accountFenceError(error)) return;
        emit('bw:reader-runtime-provider-error', {
          code: error && error.code || 'BW_PROVIDER_ATTACH',
          error: String(error && error.message || error)
        });
      });
    }
  });
  document.addEventListener('bw:extension-provider-disconnected', function (event) {
    var lease;
    try { lease = accountContext().lease(); } catch (_) { return; }
    var generation = nextProviderLifecycleGeneration();
    pendingProvider = null;
    pendingProviderGeneration = 0;
    providerSyncControl = null;
    if (!runtime) return;
    queueProviderOperation(function () {
      return runtime.detachExtension(
        event && event.detail && event.detail.reason
      );
    }).then(function (result) {
      assertAccountLease(lease);
      var hasMirrorConflicts = result
        && Array.isArray(result.mirrorConflicts)
        && result.mirrorConflicts.length > 0;
      if (!currentProviderLifecycle(generation)) return result;
      setMode(result && result.mode === 'pwa-fallback-conflict' || hasMirrorConflicts
        ? 'pwa-fallback-conflict'
        : 'pwa-fallback');
      if (!hasMirrorConflicts) {
        resumePwaSync('extension-provider-detached');
        resumePwaDirectSync('extension-provider-detached');
      }
      emit('bw:reader-runtime-provider-detached', result);
    }).catch(function (error) {
      if (accountFenceError(error)) return;
      emit('bw:reader-runtime-provider-error', {
        code: error && error.code || 'BW_PROVIDER_DETACH',
        error: String(error && error.message || error)
      });
    });
  });
  document.addEventListener('bw:extension-provider-error', function (event) {
    var lease;
    try { lease = accountContext().lease(); } catch (_) { return; }
    var detail = event && event.detail || {};
    var code = String(detail.code || 'BW_PROVIDER_ERROR');
    assertAccountLease(lease);
    setMode('pwa-fallback-provider-error');
    emit('bw:reader-runtime-provider-error', {
      code: code,
      error: detail.error || '扩展服务不可用'
    });
    if (
      runtime &&
      extensionProviderMarkerPresent() &&
      (code === 'BW_PROVIDER_AUTH' || code === 'BW_PROVIDER_AUTH_EXPIRED') &&
      !providerAuthRecoveryAttempted &&
      !providerAuthRecoveryPending
    ) {
      providerAuthRecoveryAttempted = true;
      providerAuthRecoveryPending = true;
      refreshProviderAuthorization({ lease: lease }).then(function (authorization) {
        assertAccountLease(lease);
        var bridge = api.extensionProvider;
        if (!bridge || typeof bridge.restart !== 'function') {
          throw providerAuthError('扩展服务不支持刷新后重新握手');
        }
        if (!bridge.restart({
          namespace: namespace,
          ticket: authorization.ticket
        })) {
          throw providerAuthError('扩展服务刷新后未能重新握手');
        }
      }).catch(function (error) {
        if (accountFenceError(error)) return;
        assertAccountLease(lease);
        emit('bw:reader-runtime-provider-error', {
          code: error && error.code || 'BW_PROVIDER_AUTH',
          error: String(error && error.message || error)
        });
      }).then(function () {
        providerAuthRecoveryPending = false;
      });
    }
  });

  /* 尽早锁存 document_start 标记；之后即使 provider 端口重启或页面
   * 修改了 DOM 标记，本页也不会启动第二个 server/direct sync owner。 */
  reserveExtensionSyncOwner();

  function releasePageOwnership(event) {
    pageOwnershipLifecycleGeneration += 1;
    pwaNetworkRuntimeReady = false;
    pwaSyncOwnerLifecycleGeneration += 1;
    pausePwaNetworkOwners('pagehide');
    var owner = pwaSyncOwnerLease;
    if (
      event &&
      event.persisted === true &&
      owner &&
      typeof owner.stop === 'function'
    ) {
      try {
        pageOwnershipRelease = Promise.resolve(
          owner.stop(
            'pagehide-bfcache',
            true,
            { keepalive: true }
          )
        ).catch(function () { return false; });
      } catch (_) {
        pageOwnershipRelease = Promise.resolve(false);
      }
      return;
    }
    pageOwnershipRelease = destroyPwaSyncOwnerLease(
      'pagehide',
      owner,
      { keepalive: true }
    );
  }
  function restorePageOwnership(event) {
    if (!event || event.persisted !== true || !runtime || !accountLease) return;
    pageOwnershipLifecycleGeneration += 1;
    var pageGeneration = pageOwnershipLifecycleGeneration;
    pwaNetworkRuntimeReady = true;
    if (reserveExtensionSyncOwner()) {
      pausePwaNetworkOwners('extension-owner-reserved');
      return;
    }
    var lease = accountLease;
    waitForPageOwnershipRelease(pageOwnershipRelease).then(function () {
      assertAccountLease(lease);
      if (
        pageGeneration !== pageOwnershipLifecycleGeneration ||
        !pwaNetworkRuntimeReady
      ) return false;
      if (reserveExtensionSyncOwner()) {
        pausePwaNetworkOwners('extension-owner-reserved');
        return false;
      }
      return startPwaSyncOwnerLease(lease);
    }).catch(function (error) {
      if (accountFenceError(error)) return;
      if (
        pageGeneration !== pageOwnershipLifecycleGeneration ||
        !pwaNetworkRuntimeReady
      ) return;
      pausePwaNetworkOwners('pageshow-owner-lease-error');
    });
  }
  if (typeof root.addEventListener === 'function') {
    root.addEventListener('pagehide', releasePageOwnership);
    root.addEventListener('pageshow', restorePageOwnership);
  }

  api.pwaRuntime = {
    contract: 'pwa-runtime/1',
    start: tryStart,
    runtime: function () { return runtime; },
    namespace: function () { return namespace; },
    installId: function () { return installId; },
    deviceId: function () { return installId; },
    localStores: function () { return localStores; },
    syncControl: function () {
      if (
        extensionSyncOwnerReserved &&
        providerSyncControl
      ) return providerSyncControl;
      if (extensionSyncOwnerReserved) {
        return extensionReservedSyncControl();
      }
      return pwaSyncControl;
    },
    directSyncHost: function () { return pwaDirectHost; },
    directSyncLeader: function () { return pwaDirectLeader; },
    preferences: function () { return preferences; },
    attachProvider: attachProvider
  };

  /* 在后续共享 UI 脚本读取 localStorage 前同步切好当前账户首屏镜像。 */
  if (root.__USER__) {
    try {
      var initialLease = activateServerAccount();
      ensurePreferenceStore(initialLease);
    } catch (error) {
      emit('bw:reader-runtime-account-prepare-error', {
        code: error && error.code || 'BW_PREFERENCE_BOOT',
        error: String(error && error.message || error)
      });
    }
  }
  tryStart();
})();
