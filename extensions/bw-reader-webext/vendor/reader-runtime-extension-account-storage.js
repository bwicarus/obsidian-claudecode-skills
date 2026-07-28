/* extension-account-storage.js — 扩展后台唯一的账户分区 storage.local 入口。
 *
 * 所有正式键都必须由 account-context/1 的 namespacedKey 生成。旧裸键只允许通过
 * legacyInventory() 统计“是否存在/占用字节”，绝不作为正式值读取、迁移或删除。
 * 本模块只加载在扩展后台；页面和 popup 不能直接访问其中保存的 token 明文。
 */
(function (root, factory) {
  var api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.BWReaderExtension = root.BWReaderExtension || {};
  if (!root.BWReaderExtension.accountStorage) {
    root.BWReaderExtension.accountStorage = api;
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function (root) {
  'use strict';

  var CONTRACT = 'extension-account-storage/1';
  var BASES = Object.freeze({
    CREDENTIALS: 'credentials-v1'
  });
  var LEGACY_KEYS = Object.freeze([
    'apiToken',
    'dictCache',
    'webTrCacheV1',
    'ephSettingsV1'
  ]);

  function storageError(message, code, details) {
    var error = new Error(message);
    error.code = code;
    if (details) error.details = details;
    return error;
  }

  function clone(value) {
    if (value == null) return value;
    return JSON.parse(JSON.stringify(value));
  }

  function byteLength(value) {
    var text;
    try { text = JSON.stringify(value); } catch (_) { text = ''; }
    if (typeof text !== 'string') text = '';
    if (typeof TextEncoder === 'function') {
      return new TextEncoder().encode(text).byteLength;
    }
    return unescape(encodeURIComponent(text)).length;
  }

  function assertDependencies(accountContext, storage) {
    if (
      !accountContext ||
      accountContext.CONTRACT !== 'account-context/1'
    ) {
      throw storageError(
        '扩展账户存储缺少 account-context/1',
        'BW_ACCOUNT_STORAGE_CONTEXT'
      );
    }
    if (
      !storage ||
      typeof storage.get !== 'function' ||
      typeof storage.set !== 'function'
    ) {
      throw storageError(
        '扩展账户存储后端不可用',
        'BW_ACCOUNT_STORAGE_BACKEND'
      );
    }
  }

  function create(options) {
    options = options || {};
    var accountContext = options.accountContext ||
      root.BWReaderRuntime && root.BWReaderRuntime.accountContext;
    var storage = options.storage;
    var cryptoApi = options.crypto || root.crypto;
    var now = typeof options.now === 'function' ? options.now : Date.now;
    var queues = new Map();
    assertDependencies(accountContext, storage);

    function assertLease(context, lease, base) {
      if (
        !context ||
        context.CONTRACT !== accountContext.CONTRACT ||
        typeof context.assertCurrent !== 'function' ||
        typeof context.namespacedKey !== 'function'
      ) {
        throw storageError(
          '扩展账户存储收到无效 context',
          'BW_ACCOUNT_STORAGE_CONTEXT'
        );
      }
      context.assertCurrent(lease);
      return context.namespacedKey('extension:' + String(base || ''), lease);
    }

    function keyFor(context, lease, base) {
      return assertLease(context, lease, base);
    }

    async function read(context, lease, base, fallback) {
      var key = keyFor(context, lease, base);
      var stored = await storage.get(key);
      context.assertCurrent(lease);
      if (!Object.prototype.hasOwnProperty.call(stored || {}, key)) {
        return clone(fallback);
      }
      return clone(stored[key]);
    }

    async function update(context, lease, base, fallback, mutate) {
      if (typeof mutate !== 'function') {
        throw storageError(
          '账户存储更新器必须是函数',
          'BW_ACCOUNT_STORAGE_UPDATE'
        );
      }
      var key = keyFor(context, lease, base);
      var previous = queues.get(key) || Promise.resolve();
      var operation = previous.catch(function () {}).then(async function () {
        context.assertCurrent(lease);
        var stored = await storage.get(key);
        context.assertCurrent(lease);
        var current = Object.prototype.hasOwnProperty.call(stored || {}, key)
          ? clone(stored[key])
          : clone(fallback);
        var next = await mutate(current);
        context.assertCurrent(lease);
        var serialized = clone(next);
        var out = {};
        out[key] = serialized;
        await storage.set(out);
        context.assertCurrent(lease);
        return clone(serialized);
      });
      queues.set(key, operation);
      try {
        return await operation;
      } finally {
        if (queues.get(key) === operation) queues.delete(key);
      }
    }

    async function digestHex(value) {
      if (!cryptoApi || !cryptoApi.subtle) {
        throw storageError(
          '当前浏览器缺少安全摘要能力',
          'BW_ACCOUNT_STORAGE_CRYPTO'
        );
      }
      var bytes = new TextEncoder().encode(String(value || ''));
      var digest = await cryptoApi.subtle.digest('SHA-256', bytes);
      return Array.from(new Uint8Array(digest)).map(function (part) {
        return part.toString(16).padStart(2, '0');
      }).join('');
    }

    function newCandidateId() {
      if (!cryptoApi || typeof cryptoApi.getRandomValues !== 'function') {
        throw storageError(
          '当前浏览器缺少安全随机数能力',
          'BW_ACCOUNT_STORAGE_CRYPTO'
        );
      }
      var bytes = new Uint8Array(16);
      cryptoApi.getRandomValues(bytes);
      return 'credential-v1-' + Array.from(bytes).map(function (part) {
        return part.toString(16).padStart(2, '0');
      }).join('');
    }

    function normalizeCredentialRecord(value) {
      value = value && typeof value === 'object' && !Array.isArray(value)
        ? value
        : {};
      var candidates = value.candidates &&
        typeof value.candidates === 'object' &&
        !Array.isArray(value.candidates)
        ? value.candidates
        : {};
      return {
        schema: 1,
        activeCandidateId: String(value.activeCandidateId || ''),
        candidates: clone(candidates)
      };
    }

    function publicCredentialStatus(record) {
      record = normalizeCredentialRecord(record);
      var candidates = Object.values(record.candidates);
      var active = candidates.find(function (candidate) {
        return candidate &&
          candidate.id === record.activeCandidateId &&
          candidate.active === true &&
          typeof candidate.token === 'string' &&
          candidate.token.length > 0;
      });
      return Object.freeze({
        configured: !!active,
        activeCandidateId: active ? String(active.id) : '',
        activeVerifiedAt: active ? Number(active.verifiedAt || 0) : 0,
        candidateCount: candidates.length,
        inactiveCandidateCount: candidates.filter(function (candidate) {
          return candidate && candidate.active !== true;
        }).length
      });
    }

    async function saveVerifiedToken(context, lease, token) {
      token = String(token || '').trim();
      if (!token || token.length > 8192) {
        throw storageError(
          '设备令牌为空或过长',
          'BW_ACCOUNT_TOKEN_INVALID'
        );
      }
      context.assertCurrent(lease);
      var fingerprint = await digestHex(token);
      context.assertCurrent(lease);
      var verifiedAt = Math.max(1, Number(now()) || Date.now());
      var record = await update(
        context,
        lease,
        BASES.CREDENTIALS,
        { schema: 1, activeCandidateId: '', candidates: {} },
        function (raw) {
          var next = normalizeCredentialRecord(raw);
          var existing = null;
          Object.values(next.candidates).forEach(function (candidate) {
            if (!candidate || typeof candidate !== 'object') return;
            candidate.active = false;
            if (candidate.fingerprint === fingerprint) existing = candidate;
          });
          if (!existing) {
            existing = {
              id: newCandidateId(),
              fingerprint: fingerprint,
              token: token,
              createdAt: verifiedAt,
              verifiedAt: verifiedAt,
              active: true
            };
            next.candidates[existing.id] = existing;
          } else {
            existing.token = token;
            existing.verifiedAt = verifiedAt;
            existing.active = true;
          }
          next.activeCandidateId = existing.id;
          return next;
        }
      );
      return publicCredentialStatus(record);
    }

    async function activeToken(context, lease) {
      var record = normalizeCredentialRecord(await read(
        context,
        lease,
        BASES.CREDENTIALS,
        { schema: 1, activeCandidateId: '', candidates: {} }
      ));
      context.assertCurrent(lease);
      var candidate = record.candidates[record.activeCandidateId];
      if (
        !candidate ||
        candidate.active !== true ||
        typeof candidate.token !== 'string' ||
        !candidate.token.trim()
      ) return '';
      return candidate.token.trim();
    }

    async function credentialStatus(context, lease) {
      return publicCredentialStatus(await read(
        context,
        lease,
        BASES.CREDENTIALS,
        { schema: 1, activeCandidateId: '', candidates: {} }
      ));
    }

    async function legacyInventory() {
      var stored = await storage.get(Array.from(LEGACY_KEYS));
      var inventory = {};
      LEGACY_KEYS.forEach(function (key) {
        var present = Object.prototype.hasOwnProperty.call(stored || {}, key);
        inventory[key] = Object.freeze({
          quarantined: true,
          present: present,
          bytes: present ? byteLength(stored[key]) : 0
        });
      });
      return Object.freeze(inventory);
    }

    return Object.freeze({
      CONTRACT: CONTRACT,
      BASES: BASES,
      LEGACY_KEYS: LEGACY_KEYS,
      credentialKey: function (context, lease) {
        return keyFor(context, lease, BASES.CREDENTIALS);
      },
      saveVerifiedToken: saveVerifiedToken,
      activeToken: activeToken,
      credentialStatus: credentialStatus,
      legacyInventory: legacyInventory
    });
  }

  return Object.freeze({
    CONTRACT: CONTRACT,
    BASES: BASES,
    LEGACY_KEYS: LEGACY_KEYS,
    create: create
  });
});
