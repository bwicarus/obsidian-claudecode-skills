/* direct-sync-signal-transport.js — fixed, account-fenced WebRTC signalling.
 *
 * The transport never accepts an arbitrary URL and never persists a bearer
 * token. PWA uses same-origin credentials; an extension may inject a
 * background-owned `exchange` function so the content host never sees token
 * material.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.BWReaderRuntime = root.BWReaderRuntime || {};
  root.BWReaderRuntime.directSyncSignalTransport = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var CONTRACT = 'direct-signal/1';
  var ENDPOINT = '/api/reader/sync/signal';
  var MAX_SIGNAL_BYTES = 64 * 1024;
  var SIGNAL_KINDS = Object.freeze({
    offer: true,
    answer: true,
    ice: true,
    bye: true
  });

  function SignalError(message, code, retryable, details) {
    this.name = 'DirectSignalError';
    this.message = String(message || '设备直连信令失败');
    this.code = String(code || 'BW_DIRECT_SIGNAL');
    this.retryable = !!retryable;
    this.details = details || null;
    if (Error.captureStackTrace) Error.captureStackTrace(this, SignalError);
  }
  SignalError.prototype = Object.create(Error.prototype);
  SignalError.prototype.constructor = SignalError;

  function clone(value) {
    if (value == null) return value;
    return JSON.parse(JSON.stringify(value));
  }
  function byteLength(value) {
    var text = typeof value === 'string' ? value : JSON.stringify(value);
    if (typeof TextEncoder !== 'undefined') {
      return new TextEncoder().encode(text).byteLength;
    }
    return unescape(encodeURIComponent(text)).length;
  }
  function safe(value, label, pattern) {
    value = String(value || '').trim();
    if (!value || value.length > 512 || pattern && !pattern.test(value)) {
      throw new SignalError(label + ' 无效', 'BW_DIRECT_SIGNAL_INVALID', false);
    }
    return value;
  }
  function cursor(value) {
    value = Number(value);
    if (!Number.isSafeInteger(value) || value < 0) {
      throw new SignalError('cursor 无效', 'BW_DIRECT_SIGNAL_INVALID', false);
    }
    return value;
  }
  function normalizeOutbound(raw) {
    if (!Array.isArray(raw)) {
      throw new SignalError('signals 必须是数组', 'BW_DIRECT_SIGNAL_INVALID', false);
    }
    if (raw.length > 32) {
      throw new SignalError('单次信令过多', 'BW_DIRECT_SIGNAL_TOO_LARGE', false);
    }
    return raw.map(function (signal) {
      signal = signal && typeof signal === 'object' ? signal : {};
      var kind = String(signal.kind || '');
      if (!SIGNAL_KINDS[kind]) {
        throw new SignalError('信令 kind 无效', 'BW_DIRECT_SIGNAL_INVALID', false);
      }
      var normalized = {
        signalId: safe(
          signal.signalId,
          'signalId',
          /^[A-Za-z0-9._:-]{1,160}$/
        ),
        toDeviceId: safe(
          signal.toDeviceId,
          'toDeviceId',
          /^[A-Za-z0-9._:-]{1,128}$/
        ),
        sessionId: safe(
          signal.sessionId,
          'sessionId',
          /^[A-Za-z0-9._:-]{1,160}$/
        ),
        kind: kind,
        payload: signal.payload == null ? null : clone(signal.payload)
      };
      if (byteLength(normalized) > MAX_SIGNAL_BYTES) {
        throw new SignalError('单条信令过大', 'BW_DIRECT_SIGNAL_TOO_LARGE', false);
      }
      return normalized;
    });
  }
  function normalizeResponse(input) {
    if (!input || typeof input !== 'object' || input.contract !== CONTRACT) {
      throw new SignalError('信令响应合同不匹配', 'BW_DIRECT_SIGNAL_CONTRACT', false);
    }
    var accountProof = safe(
      input.accountProof,
      'accountProof',
      /^account-proof-v1-[a-f0-9]{64}$/
    );
    var peers = Array.isArray(input.peers) ? input.peers : [];
    var signals = Array.isArray(input.signals) ? input.signals : [];
    var acknowledged = Array.isArray(input.ackedSignalIds)
      ? input.ackedSignalIds
      : [];
    if (peers.length > 64 || signals.length > 100) {
      throw new SignalError('信令响应超过上限', 'BW_DIRECT_SIGNAL_TOO_LARGE', false);
    }
    return {
      contract: CONTRACT,
      accountProof: accountProof,
      headCursor: cursor(input.headCursor || 0),
      baselineReady: input.baselineReady === true,
      baselineLocalCursor: input.baselineLocalCursor == null
        ? null
        : cursor(input.baselineLocalCursor),
      signalCursor: cursor(input.signalCursor || 0),
      signalResetRequired: input.signalResetRequired === true,
      hasMore: input.hasMore === true,
      ackedSignalIds: acknowledged.map(function (signalId) {
        return safe(
          signalId,
          'ackedSignalId',
          /^[A-Za-z0-9._:-]{1,160}$/
        );
      }),
      peers: peers.map(function (peer) {
        peer = peer && typeof peer === 'object' ? peer : {};
        return {
          deviceId: safe(
            peer.deviceId,
            'peer.deviceId',
            /^[A-Za-z0-9._:-]{1,128}$/
          ),
          baselineReady: peer.baselineReady === true,
          baselineLocalCursor: cursor(peer.baselineLocalCursor || 0)
        };
      }),
      signals: signals.map(function (signal) {
        signal = signal && typeof signal === 'object' ? signal : {};
        var kind = String(signal.kind || '');
        if (!SIGNAL_KINDS[kind]) {
          throw new SignalError('响应信令 kind 无效', 'BW_DIRECT_SIGNAL_INVALID', false);
        }
        var normalized = {
          id: cursor(signal.id),
          fromDeviceId: safe(
            signal.fromDeviceId,
            'signal.fromDeviceId',
            /^[A-Za-z0-9._:-]{1,128}$/
          ),
          sessionId: safe(
            signal.sessionId,
            'signal.sessionId',
            /^[A-Za-z0-9._:-]{1,160}$/
          ),
          kind: kind,
          payload: signal.payload == null ? null : clone(signal.payload)
        };
        if (byteLength(normalized) > MAX_SIGNAL_BYTES) {
          throw new SignalError('响应信令过大', 'BW_DIRECT_SIGNAL_TOO_LARGE', false);
        }
        return normalized;
      })
    };
  }
  function httpError(response, data) {
    var status = Number(response && response.status || 0);
    var retryable = !status || status === 408 || status === 429 || status >= 500;
    return new SignalError(
      data && data.error || '信令服务不可用',
      data && data.code || (retryable
        ? 'BW_DIRECT_SIGNAL_RETRYABLE'
        : 'BW_DIRECT_SIGNAL_REJECTED'),
      retryable,
      { status: status || null }
    );
  }

  function createDirectSignalTransport(options) {
    options = options || {};
    var origin = String(options.origin || '').replace(/\/+$/, '');
    var injectedExchange = options.exchange;
    var ownerNamespace = options.ownerNamespace == null &&
      typeof injectedExchange === 'function'
      ? ''
      : safe(
        options.ownerNamespace,
        'ownerNamespace',
        /^acct-v1-[a-f0-9]{64}$/
      );
    var deviceId = safe(
      options.deviceId,
      'deviceId',
      /^[A-Za-z0-9._:-]{1,128}$/
    );
    var registryDigest = safe(options.registryDigest, 'registryDigest');
    var credentials = String(options.credentials || 'omit');
    var fetchImpl = options.fetch;
    var ownerLease = options.ownerLease || null;
    if (credentials !== 'omit' && credentials !== 'same-origin') {
      throw new SignalError(
        'credentials 只允许 omit 或 same-origin',
        'BW_DIRECT_SIGNAL_INVALID',
        false
      );
    }
    if (typeof injectedExchange !== 'function' && typeof fetchImpl !== 'function') {
      throw new SignalError('缺少信令发送器', 'BW_DIRECT_SIGNAL_DEPENDENCY', false);
    }
    if (
      typeof injectedExchange !== 'function' &&
      (
        !ownerLease ||
        ownerLease.contract !== 'owner-lease/1' ||
        typeof ownerLease.ensureActive !== 'function' ||
        typeof ownerLease.status !== 'function'
      )
    ) {
      throw new SignalError(
        'HTTP 信令缺少 owner-lease/1',
        'BW_DIRECT_SIGNAL_DEPENDENCY',
        false
      );
    }

    function exchange(input) {
      input = input || {};
      var request = {
        contract: CONTRACT,
        deviceId: deviceId,
        registryDigest: registryDigest,
        localCursor: cursor(input.localCursor || 0),
        serverCursor: cursor(input.serverCursor || 0),
        serverReady: input.serverReady === true,
        signalCursor: cursor(input.signalCursor || 0),
        signals: normalizeOutbound(input.signals || [])
      };
      if (ownerNamespace) request.ownerNamespace = ownerNamespace;
      if (byteLength(request) > MAX_SIGNAL_BYTES * 4) {
        return Promise.reject(new SignalError(
          '信令请求过大',
          'BW_DIRECT_SIGNAL_TOO_LARGE',
          false
        ));
      }
      if (typeof injectedExchange === 'function') {
        return Promise.resolve().then(function () {
          return injectedExchange(clone(request));
        }).then(normalizeResponse);
      }
      return Promise.resolve(ownerLease.ensureActive()).then(function (owner) {
        Object.assign(request, clone(owner));
        if (byteLength(request) > MAX_SIGNAL_BYTES * 4) {
          throw new SignalError(
            '信令请求过大',
            'BW_DIRECT_SIGNAL_TOO_LARGE',
            false
          );
        }
        return fetchImpl(origin + ENDPOINT, {
        method: 'POST',
        headers: {
          'Accept': 'application/json',
          'Content-Type': 'application/json'
        },
        body: JSON.stringify(request),
        credentials: credentials,
        cache: 'no-store'
        });
      }).then(function (response) {
        var contentType = response && response.headers &&
          response.headers.get('Content-Type') || '';
        if (!contentType.includes('application/json')) throw httpError(response);
        return Promise.resolve(response.json()).then(function (data) {
          if (!response.ok || data && data.ok === false) {
            throw httpError(response, data);
          }
          return normalizeResponse(data);
        });
      }).catch(function (error) {
        if (error && error.name === 'DirectSignalError') throw error;
        throw new SignalError(
          String(error && error.message || error || '信令网络失败'),
          'BW_DIRECT_SIGNAL_RETRYABLE',
          true
        );
      });
    }

    return {
      contract: CONTRACT,
      endpoint: ENDPOINT,
      deviceId: deviceId,
      registryDigest: registryDigest,
      exchange: exchange,
      status: function () {
        return {
          contract: CONTRACT,
          endpoint: ENDPOINT,
          credentials: credentials,
          deviceId: deviceId,
          registryDigest: registryDigest,
          ownerLease: ownerLease ? ownerLease.status() : null
        };
      }
    };
  }

  return {
    CONTRACT: CONTRACT,
    ENDPOINT: ENDPOINT,
    MAX_SIGNAL_BYTES: MAX_SIGNAL_BYTES,
    SignalError: SignalError,
    createDirectSignalTransport: createDirectSignalTransport
  };
});
