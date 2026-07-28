/* server-sync-transport.js — fixed-origin HTTP transport for sync-gateway/2. */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.BWReaderRuntime = root.BWReaderRuntime || {};
  root.BWReaderRuntime.serverSyncTransport = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var CONTRACT = 'server-sync-transport/1';
  var GATEWAY_CONTRACT = 'sync-gateway/2';

  function clone(value) {
    if (value == null) return value;
    return JSON.parse(JSON.stringify(value));
  }
  function safe(value, label) {
    value = String(value || '').trim();
    if (!value) throw new Error(label + ' 不能为空');
    return value;
  }
  function createServerSyncTransport(options) {
    options = options || {};
    var fetchImpl = options.fetch || (
      typeof fetch === 'function' ? fetch.bind(globalThis) : null
    );
    var origin = safe(options.origin, 'origin').replace(/\/+$/, '');
    var ownerNamespace = safe(options.ownerNamespace, 'ownerNamespace');
    var deviceId = safe(options.deviceId, 'deviceId');
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
      throw new Error('同步协议版本与 registryDigest 不一致');
    }
    var credentials = options.credentials == null
      ? 'omit'
      : String(options.credentials);
    if (credentials !== 'omit' && credentials !== 'same-origin') {
      throw new Error('credentials 只能是 omit 或 same-origin');
    }
    var headersProvider = typeof options.headers === 'function'
      ? options.headers
      : function () { return options.headers || {}; };
    var ownerLease = options.ownerLease;
    if (
      !ownerLease ||
      ownerLease.contract !== 'owner-lease/1' ||
      typeof ownerLease.ensureActive !== 'function' ||
      typeof ownerLease.status !== 'function'
    ) {
      throw new Error('缺少 owner-lease/1');
    }
    if (!fetchImpl) throw new Error('缺少 fetch');
    function send(path, payload) {
      return Promise.resolve(ownerLease.ensureActive()).then(function (owner) {
        var headers = new Headers(headersProvider() || {});
        headers.set('Content-Type', 'application/json');
        var body = Object.assign({}, clone(payload || {}), {
          contract: GATEWAY_CONTRACT,
          ownerNamespace: ownerNamespace,
          deviceId: deviceId,
          syncContract: syncContract,
          syncChangeContract: syncChangeContract,
          registryDigest: registryDigest
        }, clone(owner));
        return fetchImpl(origin + path, {
          method: 'POST',
          headers: headers,
          body: JSON.stringify(body),
          credentials: credentials,
          cache: 'no-store'
        });
      }).then(function (response) {
        return Promise.resolve(response.json()).catch(function () {
          var invalid = new Error('同步服务器返回了无效 JSON');
          invalid.code = 'BW_SYNC_HTTP_JSON';
          invalid.status = Number(response && response.status) || 0;
          invalid.retryable = !invalid.status || invalid.status >= 500;
          throw invalid;
        }).then(function (data) {
          if (!response.ok || !data || data.ok === false) {
            var error = new Error(String(data && data.error || '同步服务器错误'));
            error.code = String(data && data.code || 'BW_SYNC_HTTP');
            error.status = Number(response.status) || 0;
            error.retryable = !error.status || error.status === 408 ||
              error.status === 429 || error.status >= 500;
            throw error;
          }
          return data;
        });
      });
    }
    return {
      contract: CONTRACT,
      exchange: function (request) {
        return send('/api/reader/sync/exchange', request);
      },
      snapshot: function (request) {
        return send('/api/reader/sync/snapshot', request);
      },
      status: function () {
        return Promise.resolve({
          contract: CONTRACT,
          state: 'ready',
          ownerNamespace: ownerNamespace,
          deviceId: deviceId,
          syncContract: syncContract,
          syncChangeContract: syncChangeContract,
          registryDigest: registryDigest,
          ownerLease: ownerLease.status(),
          credentials: credentials
        });
      }
    };
  }

  return {
    CONTRACT: CONTRACT,
    createServerSyncTransport: createServerSyncTransport
  };
});
