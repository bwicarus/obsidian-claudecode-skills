/* sync-owner-lease.js — one network sync owner per local PWA device family.
 *
 * PWA tabs and the extension background can both exist in one browser
 * profile.  A document marker is enough to hand off one tab, but it cannot
 * see an uninstrumented sibling tab.  This lease is therefore enforced by the
 * authenticated relay and is attached to every server/signal request.
 */
(function (root, factory) {
  var api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.BWReaderRuntime = root.BWReaderRuntime || {};
  root.BWReaderRuntime.syncOwnerLease = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function (root) {
  'use strict';

  var CONTRACT = 'owner-lease/1';
  var CLAIM_PATH = '/api/reader/sync/owner/claim';
  var RENEW_PATH = '/api/reader/sync/owner/renew';
  var RELEASE_PATH = '/api/reader/sync/owner/release';
  var FAMILY_RE = /^pwa-install-v1-[a-f0-9]{32}$/;
  var NAME_RE = /^[A-Za-z0-9._:-]{1,128}$/;
  var TOKEN_RE = /^owner-token-v1-[A-Za-z0-9_-]{24,256}$/;
  /*
   * The relay lease is 30 seconds.  Never trust its absolute epoch beyond a
   * shorter local window: a client clock behind the server must not keep a
   * locally active DataChannel owner after the relay has already expired it.
   */
  var MAX_LOCAL_LEASE_MS = 29000;

  function LeaseError(message, code, retryable, details) {
    this.name = 'SyncOwnerLeaseError';
    this.message = String(message || '同步所有权租约失败');
    this.code = String(code || 'BW_SYNC_OWNER_INACTIVE');
    this.retryable = retryable !== false;
    this.details = details || null;
    if (Error.captureStackTrace) Error.captureStackTrace(this, LeaseError);
  }
  LeaseError.prototype = Object.create(Error.prototype);
  LeaseError.prototype.constructor = LeaseError;

  function safe(value, label, pattern) {
    value = String(value || '').trim();
    if (!value || pattern && !pattern.test(value)) {
      throw new LeaseError(
        label + ' 无效',
        'BW_SYNC_OWNER_CONFIG',
        false
      );
    }
    return value;
  }

  function clone(value) {
    if (value == null) return value;
    return JSON.parse(JSON.stringify(value));
  }

  function createOwnerInstanceId(role, cryptoApi) {
    role = safe(role, 'ownerRole', /^(?:pwa|extension)$/);
    cryptoApi = cryptoApi || root.crypto;
    if (!cryptoApi || typeof cryptoApi.getRandomValues !== 'function') {
      throw new LeaseError(
        '缺少安全随机数，不能创建同步 owner 实例',
        'BW_SYNC_OWNER_CRYPTO',
        false
      );
    }
    var bytes = new Uint8Array(16);
    cryptoApi.getRandomValues(bytes);
    return [
      'owner-instance-v1',
      role,
      Array.prototype.map.call(bytes, function (byte) {
        return byte.toString(16).padStart(2, '0');
      }).join('')
    ].join(':');
  }

  function createSyncOwnerLease(options) {
    options = options || {};
    var ownerNamespace = safe(
      options.ownerNamespace,
      'ownerNamespace',
      /^acct-v1-[a-f0-9]{64}$/
    );
    var deviceId = safe(options.deviceId, 'deviceId', NAME_RE);
    var deviceFamilyId = safe(
      options.deviceFamilyId,
      'deviceFamilyId',
      FAMILY_RE
    );
    var ownerRole = safe(
      options.ownerRole,
      'ownerRole',
      /^(?:pwa|extension)$/
    );
    var ownerInstanceId = safe(
      options.ownerInstanceId ||
        createOwnerInstanceId(ownerRole, options.crypto),
      'ownerInstanceId',
      NAME_RE
    );
    var syncContract = safe(options.syncContract, 'syncContract');
    var syncChangeContract = safe(
      options.syncChangeContract,
      'syncChangeContract'
    );
    var registryDigest = safe(options.registryDigest, 'registryDigest');
    if (
      syncContract !== 'sync-v3' ||
      syncChangeContract !== 'record-parent-state/1' ||
      registryDigest.indexOf(
        syncContract + ':' + syncChangeContract + '|'
      ) !== 0
    ) {
      throw new LeaseError(
        'owner lease 与 sync-v3 registry 不一致',
        'BW_SYNC_OWNER_CONFIG',
        false
      );
    }
    var origin = String(options.origin || '').trim().replace(/\/+$/, '');
    var credentials = options.credentials == null
      ? 'omit'
      : String(options.credentials);
    if (credentials !== 'omit' && credentials !== 'same-origin') {
      throw new LeaseError(
        'credentials 只允许 omit 或 same-origin',
        'BW_SYNC_OWNER_CONFIG',
        false
      );
    }
    var fetchImpl = options.fetch || (
      typeof root.fetch === 'function' ? root.fetch.bind(root) : null
    );
    var requestImpl = typeof options.request === 'function'
      ? options.request
      : null;
    if (!requestImpl && (!origin || !fetchImpl)) {
      throw new LeaseError(
        '缺少 owner lease 请求器',
        'BW_SYNC_OWNER_CONFIG',
        false
      );
    }
    var setTimer = options.setTimeout || root.setTimeout;
    var clearTimer = options.clearTimeout || root.clearTimeout;
    var customNow = typeof options.now === 'function';
    var now = customNow
      ? options.now
      : function () { return Date.now(); };
    var monotonicNow = typeof options.monotonicNow === 'function'
      ? options.monotonicNow
      : (
        customNow
          ? now
          : (
            root.performance &&
            typeof root.performance.now === 'function'
              ? root.performance.now.bind(root.performance)
              : now
          )
      );
    var renewBeforeMs = Math.max(
      1000,
      Number(options.renewBeforeMs) || 10000
    );
    var retryMs = Math.max(1000, Number(options.retryMs) || 3000);
    var autoRenew = options.autoRenew !== false;
    var onAcquired = typeof options.onAcquired === 'function'
      ? options.onAcquired
      : function () {};
    var onLost = typeof options.onLost === 'function'
      ? options.onLost
      : function () {};
    var lease = null;
    var running = false;
    var destroyed = false;
    var timer = null;
    var inflight = null;
    var lossNotified = true;
    var lifecycleGeneration = 0;

    function baseBody() {
      return {
        contract: CONTRACT,
        ownerNamespace: ownerNamespace,
        deviceId: deviceId,
        deviceFamilyId: deviceFamilyId,
        ownerRole: ownerRole,
        ownerInstanceId: ownerInstanceId,
        syncContract: syncContract,
        syncChangeContract: syncChangeContract,
        registryDigest: registryDigest
      };
    }

    function publicFields(value) {
      value = value || lease;
      return {
        deviceFamilyId: deviceFamilyId,
        ownerRole: ownerRole,
        ownerInstanceId: ownerInstanceId,
        ownerGeneration: value.generation,
        ownerToken: value.token
      };
    }

    function httpRequest(path, body, requestOptions) {
      requestOptions = requestOptions &&
        typeof requestOptions === 'object'
        ? requestOptions
        : {};
      if (requestImpl) {
        return Promise.resolve(requestImpl(
          path,
          clone(body),
          clone(requestOptions)
        ));
      }
      var fetchOptions = {
        method: 'POST',
        headers: {
          'Accept': 'application/json',
          'Content-Type': 'application/json'
        },
        body: JSON.stringify(body),
        credentials: credentials,
        cache: 'no-store'
      };
      if (requestOptions.keepalive === true) {
        fetchOptions.keepalive = true;
      }
      return Promise.resolve(
        fetchImpl(origin + path, fetchOptions)
      ).then(function (response) {
        return Promise.resolve(response.json()).catch(function () {
          throw new LeaseError(
            'owner lease 服务返回无效 JSON',
            'BW_SYNC_OWNER_HTTP_JSON',
            !response || !response.status || response.status >= 500,
            { status: Number(response && response.status) || 0 }
          );
        }).then(function (data) {
          if (!response.ok || !data || data.ok === false) {
            var status = Number(response && response.status) || 0;
            throw new LeaseError(
              data && data.error || 'owner lease 服务拒绝请求',
              data && data.code || 'BW_SYNC_OWNER_INACTIVE',
              !status || status === 408 || status === 429 || status >= 500,
              { status: status || null }
            );
          }
          return data;
        });
      });
    }

    function checkedResponse(
      value,
      requestStartedAtMs,
      requestStartedAtMonoMs
    ) {
      var receivedAtMs = now();
      var receivedAtMonoMs = monotonicNow();
      if (
        !value ||
        value.ok !== true ||
        value.contract !== CONTRACT ||
        value.deviceId !== deviceId ||
        value.deviceFamilyId !== deviceFamilyId ||
        value.ownerRole !== ownerRole ||
        value.ownerInstanceId !== ownerInstanceId ||
        !Number.isSafeInteger(value.ownerGeneration) ||
        value.ownerGeneration < 1 ||
        !TOKEN_RE.test(String(value.ownerToken || '')) ||
        !Number.isSafeInteger(value.expiresAt) ||
        value.expiresAt * 1000 <= receivedAtMs
      ) {
        throw new LeaseError(
          'owner lease 响应无效',
          'BW_SYNC_OWNER_RESPONSE',
          false
        );
      }
      var localLeaseMs = Math.min(
        MAX_LOCAL_LEASE_MS,
        value.expiresAt * 1000 - receivedAtMs,
        requestStartedAtMs + MAX_LOCAL_LEASE_MS - receivedAtMs,
        requestStartedAtMonoMs +
          MAX_LOCAL_LEASE_MS -
          receivedAtMonoMs
      );
      if (!(localLeaseMs > 0)) {
        throw new LeaseError(
          'owner lease 响应到达时已超过本地安全窗口',
          'BW_SYNC_OWNER_RESPONSE',
          true
        );
      }
      return {
        generation: value.ownerGeneration,
        token: String(value.ownerToken),
        expiresAtMs: receivedAtMs + localLeaseMs,
        expiresAtMonoMs: receivedAtMonoMs + localLeaseMs
      };
    }

    function clearScheduled() {
      if (timer != null && typeof clearTimer === 'function') clearTimer(timer);
      timer = null;
    }

    function notifyLost(error) {
      if (lossNotified) return;
      lossNotified = true;
      try {
        onLost(error || new LeaseError(
          '同步 owner lease 已失效',
          'BW_SYNC_OWNER_INACTIVE',
          true
        ));
      } catch (_) {}
    }

    function active() {
      return !!(
        running &&
        !destroyed &&
        lease &&
        Number.isSafeInteger(lease.generation) &&
        lease.expiresAtMs > now() &&
        lease.expiresAtMonoMs > monotonicNow()
      );
    }

    function schedule() {
      clearScheduled();
      if (!running || destroyed || !autoRenew || typeof setTimer !== 'function') {
        return;
      }
      var delay = lease
        ? Math.max(
          250,
          Math.min(
            lease.expiresAtMs - now(),
            lease.expiresAtMonoMs - monotonicNow()
          ) - renewBeforeMs
        )
        : retryMs;
      var scheduledGeneration = lifecycleGeneration;
      timer = setTimer(function () {
        timer = null;
        if (
          destroyed ||
          !running ||
          scheduledGeneration !== lifecycleGeneration
        ) return;
        ensureActive().then(function () {
          /*
           * accept() normally schedules after claim/renew.  A platform timer
           * may fire slightly early, though, making ensureActive() reuse the
           * current lease without accept(); schedule again in both cases.
           */
          schedule();
        }).catch(function (error) {
          if (!active()) {
            lease = null;
            notifyLost(error);
          }
          schedule();
        });
      }, delay);
      if (timer && typeof timer.unref === 'function') timer.unref();
    }

    function assertLifecycle(expectedGeneration) {
      if (
        destroyed ||
        !running ||
        (
          expectedGeneration != null &&
          expectedGeneration !== lifecycleGeneration
        )
      ) {
        throw new LeaseError(
          destroyed ? 'owner lease 已销毁' : 'owner lease 未启动或已经停止',
          'BW_SYNC_OWNER_INACTIVE',
          false
        );
      }
      return true;
    }

    function accept(
      value,
      expectedGeneration,
      requestStartedAtMs,
      requestStartedAtMonoMs
    ) {
      assertLifecycle(expectedGeneration);
      var previousGeneration = lease && lease.generation;
      var checked = checkedResponse(
        value,
        requestStartedAtMs,
        requestStartedAtMonoMs
      );
      assertLifecycle(expectedGeneration);
      lease = checked;
      lossNotified = false;
      if (lease.generation !== previousGeneration) {
        try { onAcquired(publicFields()); } catch (_) {}
      }
      schedule();
      return publicFields();
    }

    function serialize(work) {
      var expectedGeneration = lifecycleGeneration;
      try { assertLifecycle(expectedGeneration); }
      catch (error) { return Promise.reject(error); }
      if (inflight) return inflight;
      var operation = Promise.resolve().then(function () {
        assertLifecycle(expectedGeneration);
        return work(expectedGeneration);
      }).then(function (value) {
        assertLifecycle(expectedGeneration);
        return value;
      });
      inflight = operation.finally(function () {
        if (inflight === operation || inflight === wrapped) inflight = null;
      });
      var wrapped = inflight;
      return wrapped;
    }

    function claim() {
      return serialize(function (expectedGeneration) {
        var requestStartedAtMs = now();
        var requestStartedAtMonoMs = monotonicNow();
        return httpRequest(CLAIM_PATH, baseBody()).then(function (value) {
          return accept(
            value,
            expectedGeneration,
            requestStartedAtMs,
            requestStartedAtMonoMs
          );
        });
      });
    }

    function renew() {
      return serialize(function (expectedGeneration) {
        if (!active()) {
          lease = null;
          var claimStartedAtMs = now();
          var claimStartedAtMonoMs = monotonicNow();
          return httpRequest(CLAIM_PATH, baseBody()).then(function (value) {
            return accept(
              value,
              expectedGeneration,
              claimStartedAtMs,
              claimStartedAtMonoMs
            );
          });
        }
        var renewStartedAtMs = now();
        var renewStartedAtMonoMs = monotonicNow();
        return httpRequest(RENEW_PATH, Object.assign(
          baseBody(),
          publicFields()
        )).then(function (value) {
          return accept(
            value,
            expectedGeneration,
            renewStartedAtMs,
            renewStartedAtMonoMs
          );
        }).catch(function (error) {
          if (
            error &&
            (
              error.code === 'BW_SYNC_OWNER_INACTIVE' ||
              error.code === 'BW_SYNC_OWNER_HELD'
            )
          ) {
            lease = null;
            notifyLost(error);
          }
          throw error;
        });
      });
    }

    function ensureActive() {
      try { assertLifecycle(lifecycleGeneration); }
      catch (error) { return Promise.reject(error); }
      if (
        active() &&
        Math.min(
          lease.expiresAtMs - now(),
          lease.expiresAtMonoMs - monotonicNow()
        ) > renewBeforeMs
      ) {
        return Promise.resolve(publicFields());
      }
      return active() ? renew() : claim();
    }

    function assertActive() {
      assertLifecycle(lifecycleGeneration);
      if (!active()) {
        if (lease) lease = null;
        var error = new LeaseError(
          '同步 owner lease 未持有或已过期',
          'BW_SYNC_OWNER_INACTIVE',
          true
        );
        notifyLost(error);
        throw error;
      }
      return true;
    }

    function start() {
      if (destroyed) {
        return Promise.reject(new LeaseError(
          'owner lease 已销毁',
          'BW_SYNC_OWNER_INACTIVE',
          false
        ));
      }
      if (!running) {
        lifecycleGeneration += 1;
        running = true;
      }
      var expectedGeneration = lifecycleGeneration;
      return ensureActive().catch(function (error) {
        if (
          running &&
          !destroyed &&
          lifecycleGeneration === expectedGeneration
        ) schedule();
        throw error;
      });
    }

    function releaseSnapshot(current, requestOptions) {
      if (!current) return Promise.resolve(false);
      return httpRequest(RELEASE_PATH, Object.assign(
        baseBody(),
        publicFields(current)
      ), requestOptions).then(function () { return true; });
    }

    function stop(reason, releaseRemote, requestOptions) {
      void reason;
      var current = lease;
      running = false;
      lifecycleGeneration += 1;
      clearScheduled();
      lease = null;
      inflight = null;
      notifyLost();
      var operation = releaseRemote === false
        ? Promise.resolve(false)
        : releaseSnapshot(current, requestOptions).catch(function () {
          return false;
        });
      return operation;
    }

    function release(reason, requestOptions) {
      return stop(reason || 'released', true, requestOptions);
    }

    function destroy(reason, requestOptions) {
      var operation = stop(reason || 'destroyed', true, requestOptions);
      destroyed = true;
      lifecycleGeneration += 1;
      return operation;
    }

    function status() {
      return {
        contract: CONTRACT,
        state: active() ? 'active' : (running ? 'waiting' : 'stopped'),
        ownerRole: ownerRole,
        ownerInstanceId: ownerInstanceId,
        deviceFamilyId: deviceFamilyId,
        generation: lease ? lease.generation : 0,
        expiresAt: lease ? Math.floor(lease.expiresAtMs / 1000) : 0
      };
    }

    return {
      contract: CONTRACT,
      start: start,
      claim: claim,
      renew: renew,
      ensureActive: ensureActive,
      assertActive: assertActive,
      fields: function () {
        assertActive();
        return clone(publicFields());
      },
      release: release,
      stop: stop,
      destroy: destroy,
      status: status
    };
  }

  return {
    CONTRACT: CONTRACT,
    CLAIM_PATH: CLAIM_PATH,
    RENEW_PATH: RENEW_PATH,
    RELEASE_PATH: RELEASE_PATH,
    MAX_LOCAL_LEASE_MS: MAX_LOCAL_LEASE_MS,
    LeaseError: LeaseError,
    createOwnerInstanceId: createOwnerInstanceId,
    createSyncOwnerLease: createSyncOwnerLease
  };
});
