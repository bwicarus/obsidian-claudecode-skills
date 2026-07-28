/* account-context.js — PWA/扩展共用的账户边界与异步失效租约。
 *
 * namespace 只能由已经验证的 server-session 或 provider-ticket 激活。本模块不读取
 * token、缓存或旧 storage key，也不负责迁移数据；调用方只用它确定“当前数据属于谁”，
 * 并在异步操作完成前用 lease 再次确认账户没有变化。
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.BWReaderRuntime = root.BWReaderRuntime || {};
  if (!root.BWReaderRuntime.accountContext) {
    root.BWReaderRuntime.accountContext = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var CONTRACT = 'account-context/1';
  var LEASE_CONTRACT = 'account-context-lease/1';
  var KEY_PREFIX = 'bw.reader.account.v1';
  var SOURCES = {
    'server-session': true,
    'provider-ticket': true
  };
  var contextSequence = 0;

  function contextError(message, code, details) {
    var error = new Error(message);
    error.code = code;
    if (details) error.details = details;
    return error;
  }

  function normalizeNamespace(value) {
    value = String(value || '').trim();
    if (!/^acct-v1-[a-f0-9]{64}$/.test(value)) {
      throw contextError(
        '无效的账户命名空间',
        'BW_ACCOUNT_NAMESPACE'
      );
    }
    return value;
  }

  function normalizeSource(value) {
    value = String(value || '').trim();
    if (!SOURCES[value]) {
      throw contextError(
        '账户上下文缺少可信身份来源',
        'BW_ACCOUNT_SOURCE'
      );
    }
    return value;
  }

  function normalizeKeyBase(value) {
    value = String(value || '').trim();
    if (!value || value.length > 160 || /[\u0000-\u001f\u007f]/.test(value)) {
      throw contextError(
        '无效的账户存储键',
        'BW_ACCOUNT_KEY'
      );
    }
    return value;
  }

  function frozenSnapshot(state, contextId) {
    return Object.freeze({
      contract: CONTRACT,
      contextId: contextId,
      active: !!state.active,
      namespace: state.active ? state.namespace : '',
      source: state.active ? state.source : '',
      generation: state.generation,
      activatedAt: state.active ? state.activatedAt : 0,
      reason: state.active ? '' : state.reason
    });
  }

  function createContext() {
    var contextId = 'account-context-' + (++contextSequence);
    var state = {
      active: false,
      namespace: '',
      source: '',
      generation: 0,
      activatedAt: 0,
      reason: 'not-activated'
    };
    var listeners = new Set();

    function snapshot() {
      return frozenSnapshot(state, contextId);
    }

    function notify(type, previous) {
      var event = Object.freeze({
        contract: CONTRACT,
        type: type,
        previous: previous,
        current: snapshot()
      });
      listeners.forEach(function (listener) {
        try { listener(event); } catch (_) {}
      });
    }

    function activate(input) {
      input = input || {};
      var namespace = normalizeNamespace(input.namespace);
      var source = normalizeSource(input.source);
      var previous = snapshot();
      state.active = true;
      state.namespace = namespace;
      state.source = source;
      state.generation += 1;
      state.activatedAt = Date.now();
      state.reason = '';
      notify('activate', previous);
      return snapshot();
    }

    function deactivate(reason) {
      if (!state.active) return snapshot();
      var previous = snapshot();
      state.active = false;
      state.namespace = '';
      state.source = '';
      state.generation += 1;
      state.activatedAt = 0;
      state.reason = String(reason || 'deactivated');
      notify('deactivate', previous);
      return snapshot();
    }

    function lease() {
      if (!state.active) {
        throw contextError(
          '账户上下文尚未激活',
          'BW_ACCOUNT_CONTEXT_UNAVAILABLE'
        );
      }
      return Object.freeze({
        contract: LEASE_CONTRACT,
        contextId: contextId,
        namespace: state.namespace,
        generation: state.generation
      });
    }

    function isCurrent(candidate) {
      return !!(
        state.active &&
        candidate &&
        candidate.contract === LEASE_CONTRACT &&
        candidate.contextId === contextId &&
        candidate.namespace === state.namespace &&
        Number(candidate.generation) === state.generation
      );
    }

    function assertCurrent(candidate) {
      if (!isCurrent(candidate)) {
        throw contextError(
          '账户上下文已变化，已拒绝旧异步结果',
          'BW_ACCOUNT_CONTEXT_STALE',
          {
            expectedNamespace: state.active ? state.namespace : '',
            expectedGeneration: state.generation,
            receivedNamespace: String(candidate && candidate.namespace || ''),
            receivedGeneration: Number(candidate && candidate.generation)
          }
        );
      }
      return snapshot();
    }

    function subscribe(listener) {
      if (typeof listener !== 'function') {
        throw contextError(
          '账户上下文订阅器必须是函数',
          'BW_ACCOUNT_SUBSCRIBER'
        );
      }
      listeners.add(listener);
      return function () { listeners.delete(listener); };
    }

    function namespacedKey(base, candidateLease) {
      var current = candidateLease ? assertCurrent(candidateLease) : snapshot();
      if (!current.active) {
        throw contextError(
          '账户上下文尚未激活',
          'BW_ACCOUNT_CONTEXT_UNAVAILABLE'
        );
      }
      return KEY_PREFIX + ':' + current.namespace + ':' +
        encodeURIComponent(normalizeKeyBase(base));
    }

    return Object.freeze({
      CONTRACT: CONTRACT,
      LEASE_CONTRACT: LEASE_CONTRACT,
      normalizeNamespace: normalizeNamespace,
      activate: activate,
      deactivate: deactivate,
      snapshot: snapshot,
      lease: lease,
      isCurrent: isCurrent,
      assertCurrent: assertCurrent,
      subscribe: subscribe,
      namespacedKey: namespacedKey,
      key: namespacedKey
    });
  }

  var singleton = createContext();
  return Object.freeze({
    CONTRACT: CONTRACT,
    LEASE_CONTRACT: LEASE_CONTRACT,
    normalizeNamespace: normalizeNamespace,
    createContext: createContext,
    activate: singleton.activate,
    deactivate: singleton.deactivate,
    snapshot: singleton.snapshot,
    lease: singleton.lease,
    isCurrent: singleton.isCurrent,
    assertCurrent: singleton.assertCurrent,
    subscribe: singleton.subscribe,
    namespacedKey: singleton.namespacedKey,
    key: singleton.key
  });
});
