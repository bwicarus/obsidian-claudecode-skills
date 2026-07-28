/* pwa-service-bridge.js — PWA 页面世界连接扩展纯服务 provider。
 *
 * 该桥不创建 UI、不替换 DocumentHost，也不读取凭据。扩展只返回 data-store/1 的
 * 通用数据操作和只读冲突摘要；页面关闭、扩展断开或协议失败时，ReaderRuntime
 * 退回 PWA store，但同步所有权仍由扩展后台保留到页面重新加载。
 */
(function () {
  'use strict';
  var root = typeof globalThis !== 'undefined' ? globalThis : window;
  var runtimeRoot = root.BWReaderRuntime = root.BWReaderRuntime || {};
  if (runtimeRoot.extensionProvider) return;

  var PROTOCOL = 'bw-reader-services/1';
  var TO_EXTENSION = 'page-to-extension';
  var TO_PAGE = 'extension-to-page';
  var pending = {};
  var listeners = [];
  var sequence = 0;
  var connected = false;
  var provider = null;
  var namespace = '';
  var helloStarted = false;
  var helloId = '';
  var helloTimer = null;
  var retryTimer = null;
  var helloAttempt = 0;
  var stopped = false;
  var retryBlocked = false;
  var helloPayloadOptions = {};
  var bridgeOptions = {
    handshakeTimeoutMs: 15000,
    requestTimeoutMs: 15000,
    retryBaseMs: 1000,
    retryMaxMs: 30000
  };

  function clone(value) {
    if (value == null) return value;
    return JSON.parse(JSON.stringify(value));
  }
  function providerMarkerPresent() {
    return !!(document.documentElement && document.documentElement.dataset.bwReaderExtensionProvider);
  }
  function providerTicketValid() {
    return /^pvt-v2-[0-9]{10,12}-[a-f0-9]{32}-[a-f0-9]{64}$/.test(
      String(helloPayloadOptions.ticket || '')
    );
  }
  function permanentProviderError(code, payload) {
    code = String(code || '');
    if (payload && payload.retryable === false) return true;
    return [
      'BW_PROVIDER_AUTH',
      'BW_PROVIDER_AUTH_EXPIRED',
      'BW_PROVIDER_NAMESPACE',
      'BW_PROVIDER_PAGE',
      'BW_PROVIDER_ORIGIN',
      'BW_PROVIDER_PROTOCOL',
      'BW_PROVIDER_UNSUPPORTED'
    ].indexOf(code) >= 0;
  }
  function post(type, payload, id) {
    root.postMessage({
      protocol: PROTOCOL,
      direction: TO_EXTENSION,
      type: type,
      id: id || null,
      namespace: namespace || null,
      payload: payload == null ? null : payload
    }, location.origin);
  }
  function failPending(message) {
    var error = new Error(String(message || '扩展服务已断开'));
    error.code = 'BW_PROVIDER_DISCONNECTED';
    Object.keys(pending).forEach(function (id) {
      var item = pending[id];
      clearTimeout(item.timer);
      item.reject(error);
      delete pending[id];
    });
  }
  function clearHello(resetAttemptState) {
    if (helloTimer) clearTimeout(helloTimer);
    helloTimer = null;
    helloStarted = false;
    helloId = '';
    if (resetAttemptState) helloAttempt = 0;
  }
  function clearRetry() {
    if (retryTimer) clearTimeout(retryTimer);
    retryTimer = null;
  }
  function applyOptions(options) {
    options = options || {};
    ['handshakeTimeoutMs', 'requestTimeoutMs', 'retryBaseMs', 'retryMaxMs'].forEach(function (name) {
      var value = Number(options[name]);
      if (Number.isFinite(value) && value > 0) bridgeOptions[name] = value;
    });
    if (bridgeOptions.retryMaxMs < bridgeOptions.retryBaseMs) {
      bridgeOptions.retryMaxMs = bridgeOptions.retryBaseMs;
    }
    if (Object.prototype.hasOwnProperty.call(options, 'ticket')) {
      if (options.ticket == null || options.ticket === '') delete helloPayloadOptions.ticket;
      else helloPayloadOptions.ticket = String(options.ticket);
    }
    if (options.helloPayload && typeof options.helloPayload === 'object') {
      Object.keys(options.helloPayload).forEach(function (key) {
        if (key === 'namespace' || key === 'page' || key === 'runtime') return;
        helloPayloadOptions[key] = clone(options.helloPayload[key]);
      });
    }
  }
  function announceUnhealthy(detail) {
    document.dispatchEvent(new CustomEvent('bw:extension-provider-unhealthy', {
      detail: clone(detail || {})
    }));
  }
  function scheduleRetry(reason) {
    if (stopped || retryBlocked || connected || helloStarted || retryTimer ||
        !providerMarkerPresent() || !namespace) return;
    var exponent = Math.max(0, helloAttempt - 1);
    var delay = Math.min(
      bridgeOptions.retryMaxMs,
      bridgeOptions.retryBaseMs * Math.pow(2, exponent)
    );
    retryTimer = setTimeout(function () {
      retryTimer = null;
      beginHandshake(reason || 'retry');
    }, delay);
  }
  function announceError(payload) {
    clearHello(false);
    payload = payload || {};
    var code = String(payload.code || 'BW_PROVIDER_ERROR');
    var permanent = permanentProviderError(code, payload);
    if (permanent) {
      retryBlocked = true;
      clearRetry();
    }
    document.dispatchEvent(new CustomEvent('bw:extension-provider-error', {
      detail: {
        code: code,
        error: String(payload.error || '扩展服务握手失败'),
        retryable: !permanent,
        permanent: permanent
      }
    }));
    if (!permanent) scheduleRetry(code || 'provider-error');
  }
  function isWriteOperation(operation) {
    return operation === 'put' || operation === 'remove' ||
      operation === 'batch' || operation === 'applyChanges';
  }
  function request(operation, args, timeoutMs) {
    if (!connected) {
      var unavailable = new Error('扩展服务尚未连接');
      unavailable.code = 'BW_PROVIDER_UNAVAILABLE';
      return Promise.reject(unavailable);
    }
    var id = 'p' + Date.now().toString(36) + (++sequence).toString(36);
    return new Promise(function (resolve, reject) {
      var timer = setTimeout(function () {
        delete pending[id];
        var error = new Error('扩展数据服务响应超时');
        error.code = 'BW_PROVIDER_TIMEOUT';
        var write = isWriteOperation(operation);
        if (write) {
          /* 写调用可能已在扩展仓库提交；调用方必须先按 mutationId 查询/核对，
           * 不能把超时当作“未执行”并自动重写。 */
          error.outcome = 'unknown';
          error.outcomeUnknown = true;
          error.retrySafe = false;
          error.details = {
            operation: operation,
            requestId: id,
            outcome: 'unknown',
            retrySafe: false
          };
        }
        announceUnhealthy({
          code: error.code,
          error: error.message,
          operation: operation,
          requestId: id,
          outcome: write ? 'unknown' : 'not-received',
          retrySafe: write ? false : null
        });
        /*
         * A conflict-status read is advisory. Detaching the whole provider on
         * its timeout would resume the PWA server owner while the extension
         * background owner is still alive, creating two writers. Keep storage
         * authority attached and let the next explicit status read retry.
         */
        if (operation !== 'syncStatus') {
          announceDisconnected('request-timeout:' + operation);
        }
        reject(error);
      }, Math.max(1, Number(timeoutMs) || bridgeOptions.requestTimeoutMs));
      pending[id] = {
        resolve: resolve,
        reject: reject,
        timer: timer,
        operation: operation
      };
      post('CALL', { operation: operation, args: args || {} }, id);
    });
  }
  function emitChange(change) {
    listeners.slice().forEach(function (entry) {
      if (entry.collection && change && change.collection && entry.collection !== change.collection) return;
      try { entry.listener(clone(change || {})); } catch (_) {}
    });
  }

  function createRemoteDataStore() {
    return {
      contract: 'data-store/1',
      get: function (collection, id, options) {
        return request('get', { collection: collection, id: id, options: options || {} });
      },
      list: function (collection, query) {
        return request('list', { collection: collection, query: query || {} });
      },
      put: function (collection, value, options) {
        return request('put', { collection: collection, value: value, options: options || {} });
      },
      remove: function (collection, id, options) {
        return request('remove', { collection: collection, id: id, options: options || {} });
      },
      batch: function (mutations) {
        return request('batch', { mutations: mutations || [] }, 30000);
      },
      changes: function (query) {
        return request('changes', { query: query || {} }, 30000);
      },
      applyChanges: function (changes, options) {
        return request('applyChanges', { changes: changes || [], options: options || {} }, 30000);
      },
      subscribe: function (query, listener) {
        if (typeof query === 'function') { listener = query; query = {}; }
        if (typeof listener !== 'function') throw new Error('subscribe listener 必须是函数');
        var entry = {
          collection: query && query.collection ? String(query.collection) : '',
          listener: listener
        };
        listeners.push(entry);
        return function () {
          var index = listeners.indexOf(entry);
          if (index >= 0) listeners.splice(index, 1);
        };
      },
      status: function () {
        return request('status', {}).then(function (status) {
          status = status || {};
          status.remote = true;
          status.provider = 'extension-vault';
          return status;
        });
      }
    };
  }
  function createRemoteSyncControl() {
    return Object.freeze({
      contract: 'sync-conflict-control/1',
      status: function () {
        return request('syncStatus', {});
      }
    });
  }

  function announceReady(info) {
    clearHello(true);
    clearRetry();
    stopped = false;
    retryBlocked = false;
    if (connected && provider) return;
    connected = true;
    provider = {
      contract: PROTOCOL,
      kind: 'extension-services',
      version: String(info && info.version || ''),
      dataStore: createRemoteDataStore(),
      syncControl: createRemoteSyncControl(),
      syncGateway: null,
      capabilities: clone(info && info.capabilities || {})
    };
    document.dispatchEvent(new CustomEvent('bw:extension-provider-ready', {
      detail: { provider: provider }
    }));
  }
  function announceDisconnected(reason, shouldRetry) {
    clearHello(false);
    var wasConnected = connected;
    connected = false;
    provider = null;
    failPending(reason || 'extension-disconnected');
    if (wasConnected) {
      document.dispatchEvent(new CustomEvent('bw:extension-provider-disconnected', {
        detail: { reason: String(reason || 'extension-disconnected') }
      }));
    }
    if (shouldRetry !== false) scheduleRetry(reason || 'extension-disconnected');
  }

  root.addEventListener('message', function (event) {
    if (event.source !== root || event.origin !== location.origin) return;
    var message = event.data;
    if (!message || message.protocol !== PROTOCOL || message.direction !== TO_PAGE) return;
    if (message.type === 'READY') {
      if (!helloStarted || (message.id && message.id !== helloId)) return;
      announceReady(message.payload || {});
      return;
    }
    if (message.type === 'ERROR') {
      if (!helloStarted || (message.id && message.id !== helloId)) return;
      announceError(message.payload || {});
      return;
    }
    if (message.type === 'DISCONNECTED') {
      announceDisconnected(message.payload && message.payload.reason);
      return;
    }
    if (message.type === 'CHANGE') {
      if (!connected) return;
      emitChange(message.payload || {});
      return;
    }
    if (message.type !== 'RESULT' || !message.id || !pending[message.id]) return;
    var item = pending[message.id];
    clearTimeout(item.timer);
    delete pending[message.id];
    if (message.payload && message.payload.ok) item.resolve(clone(message.payload.result));
    else {
      var error = new Error(String(message.payload && message.payload.error || '扩展数据服务失败'));
      error.code = String(message.payload && message.payload.code || 'BW_PROVIDER_ERROR');
      error.details = clone(message.payload && message.payload.details || null);
      item.reject(error);
      if (error.code === 'BW_PROVIDER_AUTH' || error.code === 'BW_PROVIDER_AUTH_EXPIRED') {
        announceDisconnected('provider-authorization-expired', false);
        announceError({
          code: error.code,
          error: error.message,
          retryable: false
        });
      }
    }
  });

  function beginHandshake(reason) {
    if (stopped || retryBlocked || connected || helloStarted ||
        !providerMarkerPresent() || !namespace) return false;
    clearRetry();
    helloStarted = true;
    helloAttempt += 1;
    helloId = 'h' + Date.now().toString(36) + (++sequence).toString(36);
    var activeHelloId = helloId;
    helloTimer = setTimeout(function () {
      if (!helloStarted || helloId !== activeHelloId) return;
      announceError({
        code: 'BW_PROVIDER_TIMEOUT',
        error: '扩展服务握手超时',
        reason: reason || 'handshake'
      });
    }, Math.max(1, bridgeOptions.handshakeTimeoutMs));
    var helloPayload = {
      namespace: namespace,
      page: location.pathname,
      runtime: 'pwa'
    };
    Object.keys(helloPayloadOptions).forEach(function (key) {
      helloPayload[key] = clone(helloPayloadOptions[key]);
    });
    post('HELLO', helloPayload, helloId);
    return true;
  }

  function start(options) {
    options = options || {};
    applyOptions(options);
    var requestedNamespace = String(options.namespace || namespace || '').trim();
    if (namespace && requestedNamespace && namespace !== requestedNamespace &&
        (connected || helloStarted)) {
      stop('namespace-changed');
    }
    namespace = requestedNamespace;
    if (!providerMarkerPresent() || !namespace) return false;
    if (!providerTicketValid()) {
      retryBlocked = true;
      clearRetry();
      clearHello(false);
      document.dispatchEvent(new CustomEvent('bw:extension-provider-error', {
        detail: {
          code: 'BW_PROVIDER_AUTH',
          error: '页面没有有效的扩展 Vault 授权证明',
          retryable: false,
          permanent: true
        }
      }));
      return false;
    }
    retryBlocked = false;
    stopped = false;
    if (connected || helloStarted) return true;
    beginHandshake('start');
    return true;
  }
  function stop(reason) {
    stopped = true;
    retryBlocked = false;
    clearRetry();
    clearHello(true);
    announceDisconnected(reason || 'provider-stopped', false);
  }
  function restart(options) {
    var next = options || {};
    var requestedNamespace = String(next.namespace || namespace || '').trim();
    stop('provider-restart');
    namespace = requestedNamespace;
    stopped = false;
    return start(next);
  }

  runtimeRoot.extensionProvider = {
    contract: PROTOCOL,
    start: start,
    connected: function () { return connected; },
    current: function () { return provider; },
    disconnect: stop,
    restart: restart
  };
})();
