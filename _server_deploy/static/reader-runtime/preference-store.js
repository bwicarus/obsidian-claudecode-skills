/* preference-store.js — PWA 设置的账户隔离入口与旧 localStorage 兼容镜像。
 *
 * DataStore 是设置的权威来源；显式白名单中的旧 localStorage 键只保留为当前账户的
 * 首屏同步镜像。切换账户时先把裸键保全给旧 owner，再换入新 owner 的镜像，绝不把
 * 两个账户的值自动合并。旧组件暂时无需逐个改造：白名单 set/remove/clear 会被接入
 * 同一个 PreferenceStore，最终仍按 runtime.storage() 的 collection 归属写入。
 */
(function (root, factory) {
  var api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.BWReaderRuntime = root.BWReaderRuntime || {};
  root.BWReaderRuntime.preferenceStore = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function (root) {
  'use strict';

  var CONTRACT = 'preference-store/1';
  var OWNER_KEY = 'bw.reader.legacy-settings-owner.v1';
  var MIRROR_PREFIX = 'bw.reader.preference-mirror.v1:';
  var QUARANTINE_PREFIX = 'bw.reader.preference-quarantine.v1:';
  var OLD_SHADOW = 'legacy-localstorage-shadow-v1';
  var AUTHORITY_MARKER = 'preference-store-v1';
  var MAX_VALUE_BYTES = 64 * 1024;
  var MAX_PATCH_CHANGES = 64;
  var MESSAGE_MARKER = '__bwReaderPreference';
  var TO_PAGE = 'extension-to-page';
  var TO_EXTENSION = 'page-to-extension';
  var TRUSTED_PATHS = {
    '/pdf/view': true,
    '/pdf/epub/view': true,
    '/pdf/html/view': true,
    '/pdf/web/live': true,
    '/pdf/fav/open': true
  };
  var storagePatches = [];
  var quarantineSequence = 0;

  function PreferenceError(message, code, details) {
    this.name = 'PreferenceStoreError';
    this.code = code || 'BW_PREFERENCE_ERROR';
    this.message = String(message || 'PreferenceStore error');
    this.details = details || null;
    if (Error.captureStackTrace) Error.captureStackTrace(this, PreferenceError);
  }
  PreferenceError.prototype = Object.create(Error.prototype);
  PreferenceError.prototype.constructor = PreferenceError;

  function own(value, key) {
    return Object.prototype.hasOwnProperty.call(value || {}, key);
  }
  function clone(value) {
    if (value == null) return value;
    return JSON.parse(JSON.stringify(value));
  }
  function asError(error, fallbackCode) {
    if (error instanceof PreferenceError) return error;
    return new PreferenceError(
      String(error && error.message || error || 'PreferenceStore error'),
      String(error && error.code || fallbackCode || 'BW_PREFERENCE_ERROR')
    );
  }
  function byteLength(value) {
    value = String(value);
    if (root.TextEncoder) {
      try { return new root.TextEncoder().encode(value).length; } catch (_) {}
    }
    var bytes = 0;
    for (var i = 0; i < value.length; i += 1) {
      var code = value.charCodeAt(i);
      if (code < 0x80) bytes += 1;
      else if (code < 0x800) bytes += 2;
      else if (code >= 0xd800 && code <= 0xdbff && i + 1 < value.length) {
        var next = value.charCodeAt(i + 1);
        if (next >= 0xdc00 && next <= 0xdfff) {
          bytes += 4;
          i += 1;
        } else {
          bytes += 3;
        }
      } else {
        bytes += 3;
      }
    }
    return bytes;
  }
  function validateRawValue(value) {
    value = String(value);
    if (byteLength(value) > MAX_VALUE_BYTES) {
      throw new PreferenceError(
        '单个设置值超过 64KiB',
        'BW_PREFERENCE_VALUE_SIZE'
      );
    }
    return value;
  }
  function secureMutationNonce() {
    var cryptoApi = root.crypto;
    if (!cryptoApi) {
      throw new PreferenceError(
        '无法安全生成设置 mutationId',
        'BW_PREFERENCE_RANDOM'
      );
    }
    if (typeof cryptoApi.randomUUID === 'function') {
      var uuid = String(cryptoApi.randomUUID() || '').toLowerCase();
      if (/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(uuid)) {
        return uuid.replace(/-/g, '');
      }
    }
    if (typeof cryptoApi.getRandomValues === 'function') {
      var bytes = new Uint8Array(16);
      cryptoApi.getRandomValues(bytes);
      return Array.prototype.map.call(bytes, function (value) {
        return value.toString(16).padStart(2, '0');
      }).join('');
    }
    throw new PreferenceError(
      '无法安全生成设置 mutationId',
      'BW_PREFERENCE_RANDOM'
    );
  }
  function mirrorStorageKey(namespace) {
    return MIRROR_PREFIX + String(namespace || '');
  }
  function normalizeEntries(registry) {
    if (
      !registry ||
      registry.CONTRACT !== 'data-registry/1' ||
      typeof registry.settingMigrations !== 'function' ||
      typeof registry.collection !== 'function'
    ) {
      throw new PreferenceError(
        '缺少完整 DataRegistry 设置白名单',
        'BW_PREFERENCE_REGISTRY'
      );
    }
    var seenLegacy = {};
    var seenSemantic = {};
    return registry.settingMigrations().map(function (raw) {
      raw = raw || {};
      var legacyKey = String(raw.legacyKey || '').trim();
      var semanticKey = String(raw.semanticKey || '').trim();
      var collection = String(raw.collection || '').trim();
      var codec = String(raw.codec || 'string');
      var declaration = registry.collection(collection);
      if (
        !legacyKey ||
        !semanticKey ||
        (collection !== 'user-settings' && collection !== 'device-preferences') ||
        !declaration ||
        declaration.status !== 'ready'
      ) {
        throw new PreferenceError(
          'DataRegistry 含无效设置迁移项',
          'BW_PREFERENCE_REGISTRY',
          { legacyKey: legacyKey, semanticKey: semanticKey, collection: collection }
        );
      }
      var semanticIdentity = collection + '/' + semanticKey;
      if (seenLegacy[legacyKey] || seenSemantic[semanticIdentity]) {
        throw new PreferenceError(
          'DataRegistry 设置键重复',
          'BW_PREFERENCE_REGISTRY_DUPLICATE',
          { legacyKey: legacyKey, semanticKey: semanticKey, collection: collection }
        );
      }
      seenLegacy[legacyKey] = true;
      seenSemantic[semanticIdentity] = true;
      return Object.freeze({
        legacyKey: legacyKey,
        semanticKey: semanticKey,
        collection: collection,
        codec: codec,
        id: 'setting:' + semanticKey
      });
    });
  }
  function findStoragePatch(prototype) {
    for (var i = 0; i < storagePatches.length; i += 1) {
      if (storagePatches[i].prototype === prototype) return storagePatches[i];
    }
    return null;
  }
  function storageOperations(storage) {
    if (
      !storage ||
      typeof storage.getItem !== 'function' ||
      typeof storage.setItem !== 'function' ||
      typeof storage.removeItem !== 'function'
    ) {
      throw new PreferenceError(
        'PreferenceStore 需要完整 localStorage',
        'BW_PREFERENCE_STORAGE'
      );
    }
    var prototype = Object.getPrototypeOf(storage);
    var patch = findStoragePatch(prototype);
    var methods = patch ? patch.originals : {
      getItem: storage.getItem,
      setItem: storage.setItem,
      removeItem: storage.removeItem,
      clear: typeof storage.clear === 'function' ? storage.clear : null,
      key: typeof storage.key === 'function' ? storage.key : null
    };
    return {
      get: function (key) { return methods.getItem.call(storage, key); },
      set: function (key, value) { return methods.setItem.call(storage, key, String(value)); },
      remove: function (key) { return methods.removeItem.call(storage, key); },
      clear: methods.clear ? function () { return methods.clear.call(storage); } : null,
      key: methods.key ? function (index) { return methods.key.call(storage, index); } : null
    };
  }
  function installStoragePatch(instance) {
    var storage = instance._storage;
    var prototype = Object.getPrototypeOf(storage);
    if (
      !root.Storage ||
      prototype !== root.Storage.prototype ||
      typeof prototype.setItem !== 'function' ||
      typeof prototype.removeItem !== 'function' ||
      typeof prototype.clear !== 'function'
    ) {
      return function () {};
    }
    var patch = findStoragePatch(prototype);
    if (!patch) {
      patch = {
        prototype: prototype,
        instances: [],
        originals: {
          getItem: prototype.getItem,
          setItem: prototype.setItem,
          removeItem: prototype.removeItem,
          clear: prototype.clear,
          key: prototype.key
        }
      };
      prototype.setItem = function (key, value) {
        var matching = patch.instances.filter(function (candidate) {
          return candidate._storage === this &&
            candidate._managesKey(String(key));
        }, this);
        if (!matching.length) return patch.originals.setItem.apply(this, arguments);
        return matching[matching.length - 1]._externalSet(
          String(key),
          String(value)
        );
      };
      prototype.removeItem = function (key) {
        var matching = patch.instances.filter(function (candidate) {
          return candidate._storage === this &&
            candidate._managesKey(String(key));
        }, this);
        if (!matching.length) return patch.originals.removeItem.apply(this, arguments);
        return matching[matching.length - 1]._externalRemove(String(key));
      };
      prototype.clear = function () {
        var matching = patch.instances.filter(function (candidate) {
          return candidate._storage === this;
        }, this);
        if (!matching.length) return patch.originals.clear.apply(this, arguments);
        return matching[matching.length - 1]._externalClear();
      };
      storagePatches.push(patch);
    }
    patch.instances.push(instance);
    return function () {
      var index = patch.instances.indexOf(instance);
      if (index >= 0) patch.instances.splice(index, 1);
    };
  }
  function blankMirror(namespace) {
    return {
      contract: CONTRACT,
      namespace: namespace,
      mutationSeq: 0,
      updatedAt: 0,
      values: {},
      states: {}
    };
  }
  function quarantineMirror(ops, namespace, raw, reason) {
    var key = QUARANTINE_PREFIX + Date.now() + ':' +
      (++quarantineSequence).toString(36) + ':' + namespace;
    try {
      ops.set(key, JSON.stringify({
        contract: CONTRACT,
        reason: String(reason || 'corrupt-mirror'),
        namespace: namespace,
        raw: String(raw || '')
      }));
    } catch (error) {
      throw new PreferenceError(
        '损坏的账户设置镜像无法隔离保全',
        'BW_PREFERENCE_MIRROR_CORRUPT',
        { namespace: namespace, cause: String(error && error.message || error) }
      );
    }
  }
  function readMirror(ops, namespace, allowed) {
    var raw = null;
    try { raw = ops.get(mirrorStorageKey(namespace)); } catch (_) {}
    if (!raw) return blankMirror(namespace);
    try {
      var parsed = JSON.parse(raw);
      if (
        !parsed ||
        parsed.contract !== CONTRACT ||
        parsed.namespace !== namespace ||
        !parsed.values ||
        typeof parsed.values !== 'object' ||
        !parsed.states ||
        typeof parsed.states !== 'object'
      ) {
        quarantineMirror(ops, namespace, raw, 'invalid-envelope');
        return blankMirror(namespace);
      }
      var mirror = blankMirror(namespace);
      mirror.mutationSeq = Math.max(0, Number(parsed.mutationSeq) || 0);
      mirror.updatedAt = Math.max(0, Number(parsed.updatedAt) || 0);
      Object.keys(allowed).forEach(function (key) {
        if (own(parsed.values, key)) {
          mirror.values[key] = validateRawValue(parsed.values[key]);
        }
        if (own(parsed.states, key) && parsed.states[key] && typeof parsed.states[key] === 'object') {
          mirror.states[key] = clone(parsed.states[key]);
        }
      });
      return mirror;
    } catch (error) {
      if (error && error.code === 'BW_PREFERENCE_MIRROR_CORRUPT') throw error;
      quarantineMirror(ops, namespace, raw, 'invalid-json-or-value');
      return blankMirror(namespace);
    }
  }
  function saveMirror(ops, mirror) {
    mirror.updatedAt = Date.now();
    ops.set(mirrorStorageKey(mirror.namespace), JSON.stringify(mirror));
  }
  function trustedPwaWindow(options) {
    if (options.trustedWindow === true) return true;
    var pathname = String(root.location && root.location.pathname || '');
    return !!TRUSTED_PATHS[pathname];
  }

  function createPreferenceStore(options) {
    options = options || {};
    var context = options.accountContext ||
      root.BWReaderRuntime && root.BWReaderRuntime.accountContext;
    var registry = options.dataRegistry ||
      root.BWReaderRuntime && root.BWReaderRuntime.dataRegistry;
    var webStorage = options.storage || root.localStorage;
    var lease = options.lease;
    if (
      !context ||
      context.CONTRACT !== 'account-context/1' ||
      typeof context.assertCurrent !== 'function' ||
      typeof context.lease !== 'function'
    ) {
      throw new PreferenceError(
        '缺少完整 AccountContext',
        'BW_PREFERENCE_ACCOUNT'
      );
    }
    if (!lease) lease = context.lease();
    context.assertCurrent(lease);
    var namespace = lease.namespace;
    var entries = normalizeEntries(registry);
    var byLegacy = {};
    var byIdentity = {};
    entries.forEach(function (entry) {
      byLegacy[entry.legacyKey] = entry;
      byIdentity[entry.collection + '/' + entry.id] = entry;
    });
    var ops = storageOperations(webStorage);
    var eventTarget = options.eventTarget || root.document || null;
    var mirror = null;
    var router = null;
    var attached = false;
    var attaching = null;
    var destroyed = false;
    var pendingBeforeAttach = [];
    var pendingLatest = {};
    var writeQueue = Promise.resolve();
    var unsubscribers = [];
    var removeStoragePatch = function () {};
    var removeMessageBridge = function () {};
    var removeOwnerListener = function () {};
    var ownerStale = false;

    function assertLease(candidate) {
      if (destroyed) {
        throw new PreferenceError(
          'PreferenceStore 已停止',
          'BW_PREFERENCE_STOPPED'
        );
      }
      return context.assertCurrent(candidate || lease);
    }
    function markOwnerStale(actualOwner) {
      if (ownerStale) return;
      ownerStale = true;
      /*
       * setItem 与 owner 二次校验之间若恰好发生账户切换，旧页可能已经碰了一个
       * 新账户的裸键。用新 owner 自己的完整镜像立即修复；无效 owner 时 fail closed，
       * 不把未知裸值归给任何账户。
       */
      try {
        var validOwner = context.normalizeNamespace(actualOwner);
        if (validOwner && validOwner !== namespace) {
          applyMirrorToNaked(readMirror(ops, validOwner, byLegacy));
        }
      } catch (_) {}
      emit('bw:reader-preference-error', {
        phase: 'account-owner',
        code: 'BW_PREFERENCE_OWNER_STALE',
        error: '当前裸设置镜像已经由另一个账户接管',
        expectedOwner: namespace,
        actualOwner: String(actualOwner || '')
      });
      try {
        if (context.isCurrent && context.isCurrent(lease)) {
          context.deactivate('legacy-owner-changed');
        }
      } catch (_) {}
    }
    function assertOwner() {
      var owner = '';
      try { owner = String(ops.get(OWNER_KEY) || '').trim(); } catch (_) {}
      if (ownerStale || owner !== namespace) {
        markOwnerStale(owner);
        throw new PreferenceError(
          '当前账户已不再拥有裸设置镜像',
          'BW_PREFERENCE_OWNER_STALE',
          { expectedOwner: namespace, actualOwner: owner }
        );
      }
      return owner;
    }
    function assertFence(candidate) {
      assertLease(candidate);
      assertOwner();
      return context.assertCurrent(candidate || lease);
    }
    function emit(name, detail) {
      if (!eventTarget || typeof eventTarget.dispatchEvent !== 'function') return;
      try {
        var EventCtor = root.CustomEvent;
        if (typeof EventCtor === 'function') {
          eventTarget.dispatchEvent(new EventCtor(name, { detail: detail || {} }));
        }
      } catch (_) {}
    }
    function background(promise, phase) {
      Promise.resolve(promise).catch(function (error) {
        emit('bw:reader-preference-error', {
          phase: String(phase || 'write'),
          code: String(error && error.code || 'BW_PREFERENCE_WRITE'),
          error: String(error && error.message || error)
        });
      });
    }
    function captureNaked(targetNamespace, existing) {
      var hadExisting = !!existing;
      var target = existing || readMirror(ops, targetNamespace, byLegacy);
      entries.forEach(function (entry) {
        var key = entry.legacyKey;
        var raw = null;
        try { raw = ops.get(key); } catch (_) {}
        var hadValue = own(target.values, key);
        var previous = hadValue ? target.values[key] : null;
        if (raw == null) {
          if (hadValue) {
            delete target.values[key];
            target.states[key] = { status: 'dirty', absent: true };
          } else if (!hadExisting) {
            delete target.states[key];
          }
          return;
        }
        raw = validateRawValue(raw);
        target.values[key] = raw;
        if (!hadExisting) {
          target.states[key] = { status: 'legacy', absent: false };
        } else if (!hadValue || previous !== raw) {
          target.states[key] = { status: 'dirty', absent: false };
        }
      });
      saveMirror(ops, target);
      return target;
    }
    function applyMirrorToNaked(target) {
      entries.forEach(function (entry) {
        var key = entry.legacyKey;
        if (own(target.values, key)) ops.set(key, target.values[key]);
        else ops.remove(key);
      });
    }
    function quarantineUnownedNaked() {
      var values = {};
      entries.forEach(function (entry) {
        var raw = null;
        try { raw = ops.get(entry.legacyKey); } catch (_) {}
        if (raw != null) values[entry.legacyKey] = String(raw);
      });
      if (!Object.keys(values).length) return;
      try {
        ops.set(
          QUARANTINE_PREFIX + Date.now(),
          JSON.stringify({ contract: CONTRACT, reason: 'invalid-owner', values: values })
        );
      } catch (_) {}
    }
    function prepareAccountMirror() {
      assertLease(lease);
      var owner = '';
      try { owner = String(ops.get(OWNER_KEY) || '').trim(); } catch (_) {}
      var validOwner = '';
      if (owner) {
        try { validOwner = context.normalizeNamespace(owner); } catch (_) {}
      }
      if (!owner) {
        var firstMirror = null;
        try {
          if (ops.get(mirrorStorageKey(namespace)) != null) {
            firstMirror = readMirror(ops, namespace, byLegacy);
          }
        } catch (error) {
          if (error && error.code === 'BW_PREFERENCE_MIRROR_CORRUPT') throw error;
        }
        mirror = captureNaked(namespace, firstMirror);
        ops.set(OWNER_KEY, namespace);
      } else if (!validOwner) {
        quarantineUnownedNaked();
        entries.forEach(function (entry) { ops.remove(entry.legacyKey); });
        mirror = readMirror(ops, namespace, byLegacy);
        applyMirrorToNaked(mirror);
        ops.set(OWNER_KEY, namespace);
      } else if (validOwner !== namespace) {
        var previous = readMirror(ops, validOwner, byLegacy);
        captureNaked(validOwner, previous);
        mirror = readMirror(ops, namespace, byLegacy);
        applyMirrorToNaked(mirror);
        ops.set(OWNER_KEY, namespace);
        emit('bw:reader-preference-account-switched', {
          previousOwner: validOwner,
          currentOwner: namespace
        });
      } else {
        var currentKey = mirrorStorageKey(namespace);
        var existed = false;
        try { existed = ops.get(currentKey) != null; } catch (_) {}
        mirror = captureNaked(
          namespace,
          existed ? readMirror(ops, namespace, byLegacy) : null
        );
        ops.set(OWNER_KEY, namespace);
      }
      assertLease(lease);
    }
    function allocateMutation(entry, operation) {
      return [
        'preference-v1',
        operation,
        entry.semanticKey,
        secureMutationNonce()
      ].join(':');
    }
    function setMirrorState(entry, status, extra) {
      mirror.states[entry.legacyKey] = Object.assign({
        status: status,
        absent: !own(mirror.values, entry.legacyKey)
      }, extra || {});
    }
    function persistDirty(entry, raw, remove, source) {
      assertFence(lease);
      mirror = readMirror(ops, namespace, byLegacy);
      if (remove) delete mirror.values[entry.legacyKey];
      else mirror.values[entry.legacyKey] = validateRawValue(raw);
      var mutationId = allocateMutation(entry, remove ? 'remove' : 'put');
      setMirrorState(entry, 'dirty', {
        mutationId: mutationId,
        source: String(source || 'compatibility')
      });
      saveMirror(ops, mirror);
      assertFence(lease);
      return mutationId;
    }
    function validateStoredRecord(entry, record) {
      if (!record || record.deleted) return record;
      var value = record.value;
      if (
        !value ||
        typeof value.rawValue !== 'string' ||
        String(value.semanticKey || '') !== entry.semanticKey ||
        String(value.legacyKey || '') !== entry.legacyKey ||
        String(record.id || '') !== entry.id ||
        String(record.collection || '') !== entry.collection
      ) {
        throw new PreferenceError(
          'DataStore 设置记录与 DataRegistry 不一致',
          'BW_PREFERENCE_RECORD',
          {
            collection: entry.collection,
            id: entry.id,
            legacyKey: entry.legacyKey
          }
        );
      }
      validateRawValue(value.rawValue);
      return record;
    }
    function applySyncedRecord(entry, record, source) {
      assertFence(lease);
      mirror = readMirror(ops, namespace, byLegacy);
      if (!record || record.deleted) {
        ops.remove(entry.legacyKey);
        delete mirror.values[entry.legacyKey];
        setMirrorState(entry, 'synced', {
          absent: true,
          rev: Number(record && record.rev || 0)
        });
      } else {
        validateStoredRecord(entry, record);
        var raw = record.value.rawValue;
        ops.set(entry.legacyKey, raw);
        mirror.values[entry.legacyKey] = raw;
        setMirrorState(entry, 'synced', {
          absent: false,
          rev: Number(record.rev || 0)
        });
      }
      saveMirror(ops, mirror);
      emit('bw:reader-preference-changed', {
        legacyKey: entry.legacyKey,
        semanticKey: entry.semanticKey,
        collection: entry.collection,
        removed: !record || !!record.deleted,
        source: String(source || 'data-store')
      });
      assertFence(lease);
    }
    function makePutValue(entry, raw) {
      return {
        id: entry.id,
        semanticKey: entry.semanticKey,
        legacyKey: entry.legacyKey,
        codec: entry.codec,
        rawValue: validateRawValue(raw),
        migration: AUTHORITY_MARKER
      };
    }
    function executeOperation(operation) {
      assertFence(operation.lease);
      var action;
      if (operation.remove) {
        action = router.remove(operation.entry.collection, operation.entry.id, {
          mutationId: operation.mutationId
        });
      } else {
        action = router.put(
          operation.entry.collection,
          makePutValue(operation.entry, operation.rawValue),
          {
            id: operation.entry.id,
            mutationId: operation.mutationId
          }
        );
      }
      return Promise.resolve(action).then(function (record) {
        assertFence(operation.lease);
        if (pendingLatest[operation.entry.legacyKey] === operation) {
          delete pendingLatest[operation.entry.legacyKey];
          applySyncedRecord(operation.entry, record, operation.source);
        }
        assertFence(operation.lease);
        return { queued: false, record: record };
      });
    }
    function enqueueAttached(operation) {
      var task = writeQueue.then(function () {
        return executeOperation(operation);
      });
      writeQueue = task.catch(function () {});
      return task;
    }
    function registerOperation(operation) {
      pendingLatest[operation.entry.legacyKey] = operation;
      if (!attached) {
        pendingBeforeAttach.push(operation);
        return Promise.resolve({ queued: true });
      }
      return enqueueAttached(operation);
    }
    function scheduleOperation(entry, raw, remove, source) {
      var mutationId = persistDirty(entry, raw, remove, source);
      var operation = {
        entry: entry,
        rawValue: remove ? null : String(raw),
        remove: !!remove,
        source: String(source || 'compatibility'),
        mutationId: mutationId,
        lease: lease
      };
      return registerOperation(operation);
    }
    function setRaw(legacyKey, value, source, alreadyWritten) {
      assertFence(lease);
      var entry = byLegacy[String(legacyKey || '')];
      if (!entry) {
        return Promise.reject(new PreferenceError(
          '设置键不在 DataRegistry 白名单',
          'BW_PREFERENCE_KEY',
          { legacyKey: String(legacyKey || '') }
        ));
      }
      var raw;
      try { raw = validateRawValue(value); }
      catch (error) { return Promise.reject(error); }
      var previous = null;
      try { previous = ops.get(entry.legacyKey); } catch (_) {}
      if (!alreadyWritten) ops.set(entry.legacyKey, raw);
      try { assertFence(lease); }
      catch (error) {
        if (!alreadyWritten && error.code !== 'BW_PREFERENCE_OWNER_STALE') {
          try {
            if (previous == null) ops.remove(entry.legacyKey);
            else ops.set(entry.legacyKey, previous);
          } catch (_) {}
        }
        return Promise.reject(error);
      }
      var scheduled;
      try { scheduled = scheduleOperation(entry, raw, false, source); }
      catch (error) {
        if (!alreadyWritten && error.code !== 'BW_PREFERENCE_OWNER_STALE') {
          try {
            if (previous == null) ops.remove(entry.legacyKey);
            else ops.set(entry.legacyKey, previous);
          } catch (_) {}
        }
        return Promise.reject(error);
      }
      emit('bw:reader-preference-mirror-changed', {
        legacyKey: entry.legacyKey,
        removed: false,
        source: String(source || 'compatibility')
      });
      assertFence(lease);
      return scheduled;
    }
    function removeRaw(legacyKey, source, alreadyRemoved) {
      assertFence(lease);
      var entry = byLegacy[String(legacyKey || '')];
      if (!entry) {
        return Promise.reject(new PreferenceError(
          '设置键不在 DataRegistry 白名单',
          'BW_PREFERENCE_KEY',
          { legacyKey: String(legacyKey || '') }
        ));
      }
      var previous = null;
      try { previous = ops.get(entry.legacyKey); } catch (_) {}
      if (!alreadyRemoved) ops.remove(entry.legacyKey);
      try { assertFence(lease); }
      catch (error) {
        if (!alreadyRemoved && error.code !== 'BW_PREFERENCE_OWNER_STALE') {
          try {
            if (previous != null) ops.set(entry.legacyKey, previous);
          } catch (_) {}
        }
        return Promise.reject(error);
      }
      var scheduled;
      try { scheduled = scheduleOperation(entry, null, true, source); }
      catch (error) {
        if (!alreadyRemoved && error.code !== 'BW_PREFERENCE_OWNER_STALE') {
          try {
            if (previous != null) ops.set(entry.legacyKey, previous);
          } catch (_) {}
        }
        return Promise.reject(error);
      }
      emit('bw:reader-preference-mirror-changed', {
        legacyKey: entry.legacyKey,
        removed: true,
        source: String(source || 'compatibility')
      });
      assertFence(lease);
      return scheduled;
    }
    function prefetchEntries() {
      /*
       * EPUB 冷启动会同时建数百个章节占位、拉可视章节并做分词/装饰。若这里再把
       * 白名单设置拆成几十次串行 IndexedDB transaction，任意一次回调被主线程
       * 挤延后都会把后面的全部设置和 extension provider 接管一起排队。
       *
       * collection list 是同一 DataStore 权威快照，不改变逐 entry 的仲裁顺序：
       * 后面仍依次执行 pendingLatest 检查、记录校验、legacy/mirror 优先规则及
       * ifRev 写入。list 只把 54 次独立读取收敛成每个 collection 一次读取。
       * 若 store 没有 list（旧测试/兼容实现），或结果达到 DataStore 的 1000 条
       * 上限而不能证明“缺项就是不存在”，对应缺项仍退回精确 get，绝不猜测。
       */
      if (!router || typeof router.list !== 'function') {
        return Promise.resolve(null);
      }
      var collections = [];
      entries.forEach(function (entry) {
        if (collections.indexOf(entry.collection) < 0) {
          collections.push(entry.collection);
        }
      });
      return Promise.all(collections.map(function (collection) {
        return Promise.resolve(router.list(collection, {
          includeDeleted: true,
          offset: 0,
          limit: 1000
        })).then(function (records) {
          assertFence(lease);
          if (!Array.isArray(records)) {
            throw new PreferenceError(
              'DataStore.list 返回无效设置快照',
              'BW_PREFERENCE_ROUTER',
              { collection: collection }
            );
          }
          var byId = {};
          records.forEach(function (record) {
            if (record && record.id != null) byId[String(record.id)] = record;
          });
          return {
            collection: collection,
            byId: byId,
            complete: records.length < 1000
          };
        });
      })).then(function (snapshots) {
        var byCollection = {};
        snapshots.forEach(function (snapshot) {
          byCollection[snapshot.collection] = snapshot;
        });
        return byCollection;
      });
    }
    function initialRecord(entry, prefetched) {
      var snapshot = prefetched && prefetched[entry.collection];
      if (snapshot) {
        if (own(snapshot.byId, entry.id)) {
          return Promise.resolve(snapshot.byId[entry.id]);
        }
        if (snapshot.complete) return Promise.resolve(null);
      }
      return Promise.resolve(
        router.get(entry.collection, entry.id, { includeDeleted: true })
      );
    }
    function initializeEntry(entry, prefetched) {
      assertFence(lease);
      if (pendingLatest[entry.legacyKey]) return Promise.resolve();
      return initialRecord(entry, prefetched).then(function (record) {
        assertFence(lease);
        if (pendingLatest[entry.legacyKey]) return null;
        validateStoredRecord(entry, record);
        mirror = readMirror(ops, namespace, byLegacy);
        var hasMirror = own(mirror.values, entry.legacyKey);
        var state = mirror.states[entry.legacyKey] || {};
        var mirrorMustWin = state.status === 'legacy' || state.status === 'dirty';
        var oldShadow = !!(
          record &&
          !record.deleted &&
          record.value &&
          record.value.migration === OLD_SHADOW
        );
        if (!record || record.deleted) {
          if (!hasMirror && !mirrorMustWin) {
            applySyncedRecord(entry, record, 'initial-empty');
            return null;
          }
          mirrorMustWin = true;
        } else if (oldShadow) {
          mirrorMustWin = true;
        }
        if (!mirrorMustWin) {
          applySyncedRecord(entry, record, 'initial-data-store');
          return null;
        }

        var mutationId = allocateMutation(
          entry,
          hasMirror ? 'upgrade-put' : 'upgrade-remove'
        );
        setMirrorState(entry, 'dirty', {
          mutationId: mutationId,
          source: oldShadow ? 'old-shadow-upgrade' : 'legacy-upgrade'
        });
        saveMirror(ops, mirror);
        var operation;
        if (hasMirror) {
          operation = router.put(
            entry.collection,
            makePutValue(entry, mirror.values[entry.legacyKey]),
            {
              id: entry.id,
              mutationId: mutationId,
              ifRev: Number(record && record.rev || 0)
            }
          );
        } else {
          operation = router.remove(entry.collection, entry.id, {
            mutationId: mutationId,
            ifRev: Number(record && record.rev || 0)
          });
        }
        return Promise.resolve(operation).then(function (result) {
          assertFence(lease);
          applySyncedRecord(entry, result, oldShadow
            ? 'old-shadow-upgrade'
            : 'legacy-upgrade');
          return result;
        });
      });
    }
    function subscribeToStore() {
      ['user-settings', 'device-preferences'].forEach(function (collection) {
        var unsubscribe = router.subscribe(collection, function (change) {
          try {
            assertFence(lease);
            var record = change && change.record;
            var entry = record && byIdentity[
              String(change.collection || record.collection || '') + '/' +
              String(record.id || '')
            ];
            if (!entry) return;
            var pending = pendingLatest[entry.legacyKey];
            if (pending && pending.mutationId !== String(change.mutationId || '')) return;
            applySyncedRecord(entry, record, change.remote ? 'remote-data-store' : 'data-store');
          } catch (error) {
            if (error && error.code === 'BW_ACCOUNT_CONTEXT_STALE') return;
            emit('bw:reader-preference-error', {
              phase: 'subscription',
              code: String(error && error.code || 'BW_PREFERENCE_SUBSCRIPTION'),
              error: String(error && error.message || error)
            });
          }
        });
        if (typeof unsubscribe === 'function') unsubscribers.push(unsubscribe);
      });
    }
    function flushPending() {
      if (!pendingBeforeAttach.length) return Promise.resolve();
      var batch = pendingBeforeAttach.splice(0);
      return batch.reduce(function (chain, operation) {
        return chain.then(function () {
          assertFence(operation.lease);
          return executeOperation(operation);
        });
      }, Promise.resolve()).then(flushPending);
    }
    function attach(nextRouter, candidateLease) {
      candidateLease = candidateLease || lease;
      assertFence(candidateLease);
      if (candidateLease !== lease) {
        if (
          candidateLease.contextId !== lease.contextId ||
          candidateLease.namespace !== lease.namespace ||
          candidateLease.generation !== lease.generation
        ) {
          return Promise.reject(new PreferenceError(
            'attach 使用了不同账户租约',
            'BW_PREFERENCE_ACCOUNT'
          ));
        }
      }
      if (
        !nextRouter ||
        typeof nextRouter.get !== 'function' ||
        typeof nextRouter.put !== 'function' ||
        typeof nextRouter.remove !== 'function' ||
        typeof nextRouter.subscribe !== 'function'
      ) {
        return Promise.reject(new PreferenceError(
          'PreferenceStore.attach 需要 runtime.storage()',
          'BW_PREFERENCE_ROUTER'
        ));
      }
      if (attaching) return attaching;
      router = nextRouter;
      attaching = prefetchEntries().then(function (prefetched) {
        return entries.reduce(function (chain, entry) {
          return chain.then(function () {
            return initializeEntry(entry, prefetched);
          });
        }, Promise.resolve());
      }).then(function () {
        assertFence(lease);
        subscribeToStore();
        attached = true;
        return flushPending();
      }).then(function () {
        assertFence(lease);
        emit('bw:reader-preference-ready', {
          allowedKeys: entries.map(function (entry) { return entry.legacyKey; })
        });
        return api;
      }).catch(function (error) {
        attached = false;
        throw error;
      });
      return attaching;
    }
    function currentValues() {
      assertFence(lease);
      mirror = readMirror(ops, namespace, byLegacy);
      var values = {};
      entries.forEach(function (entry) {
        if (own(mirror.values, entry.legacyKey)) {
          values[entry.legacyKey] = mirror.values[entry.legacyKey];
        }
      });
      assertFence(lease);
      return values;
    }
    function getRaw(legacyKey) {
      assertFence(lease);
      mirror = readMirror(ops, namespace, byLegacy);
      legacyKey = String(legacyKey || '');
      if (!byLegacy[legacyKey]) {
        throw new PreferenceError(
          '设置键不在 DataRegistry 白名单',
          'BW_PREFERENCE_KEY',
          { legacyKey: legacyKey }
        );
      }
      return own(mirror.values, legacyKey) ? mirror.values[legacyKey] : null;
    }
    function applyPatch(changes, source) {
      assertFence(lease);
      if (!Array.isArray(changes) || changes.length > MAX_PATCH_CHANGES) {
        return Promise.reject(new PreferenceError(
          'PATCH changes 必须是最多 64 项的数组',
          'BW_PREFERENCE_PATCH'
        ));
      }
      var normalized = [];
      var seen = {};
      try {
        changes.forEach(function (change) {
          change = change || {};
          if (own(change, 'namespace')) {
            throw new PreferenceError(
              'PATCH 不接受 namespace',
              'BW_PREFERENCE_NAMESPACE'
            );
          }
          var legacyKey = String(change.legacyKey || '');
          if (!byLegacy[legacyKey]) {
            throw new PreferenceError(
              '设置键不在 DataRegistry 白名单',
              'BW_PREFERENCE_KEY',
              { legacyKey: legacyKey }
            );
          }
          if (seen[legacyKey]) {
            throw new PreferenceError(
              'PATCH 不允许重复设置键',
              'BW_PREFERENCE_PATCH_DUPLICATE',
              { legacyKey: legacyKey }
            );
          }
          seen[legacyKey] = true;
          if (change.value === null) {
            normalized.push({ legacyKey: legacyKey, value: null });
          } else {
            if (typeof change.value !== 'string') {
              throw new PreferenceError(
                'PATCH value 只允许字符串或 null',
                'BW_PREFERENCE_PATCH'
              );
            }
            normalized.push({
              legacyKey: legacyKey,
              value: validateRawValue(change.value)
            });
          }
        });
      } catch (error) {
        return Promise.reject(error);
      }
      assertFence(lease);
      var previousValues = {};
      normalized.forEach(function (change) {
        previousValues[change.legacyKey] = ops.get(change.legacyKey);
      });
      var mirrorKey = mirrorStorageKey(namespace);
      var previousMirrorRaw = ops.get(mirrorKey);
      mirror = readMirror(ops, namespace, byLegacy);
      var operations;
      try {
        operations = normalized.map(function (change) {
          var entry = byLegacy[change.legacyKey];
          var remove = change.value === null;
          if (remove) delete mirror.values[entry.legacyKey];
          else mirror.values[entry.legacyKey] = change.value;
          var mutationId = allocateMutation(entry, remove ? 'remove' : 'put');
          setMirrorState(entry, 'dirty', {
            mutationId: mutationId,
            source: String(source || 'message-bridge')
          });
          return {
            entry: entry,
            rawValue: remove ? null : change.value,
            remove: remove,
            source: String(source || 'message-bridge'),
            mutationId: mutationId,
            lease: lease
          };
        });
        normalized.forEach(function (change) {
          if (change.value === null) ops.remove(change.legacyKey);
          else ops.set(change.legacyKey, change.value);
        });
        saveMirror(ops, mirror);
        assertFence(lease);
      } catch (error) {
        /*
         * 本地 PATCH 是一批镜像变更：任一步失败即恢复整批。若 owner 已切换，
         * markOwnerStale 已按新 owner 镜像修复裸键，不能再把旧账户快照盖回去。
         */
        if (!error || error.code !== 'BW_PREFERENCE_OWNER_STALE') {
          Object.keys(previousValues).forEach(function (key) {
            try {
              if (previousValues[key] == null) ops.remove(key);
              else ops.set(key, previousValues[key]);
            } catch (_) {}
          });
        }
        try {
          if (previousMirrorRaw == null) ops.remove(mirrorKey);
          else ops.set(mirrorKey, previousMirrorRaw);
        } catch (_) {}
        return Promise.reject(error);
      }
      normalized.forEach(function (change) {
        emit('bw:reader-preference-mirror-changed', {
          legacyKey: change.legacyKey,
          removed: change.value === null,
          source: String(source || 'message-bridge')
        });
      });
      var tasks = operations.map(registerOperation);
      return Promise.all(tasks).then(function (results) {
        assertFence(lease);
        var values = {};
        normalized.forEach(function (change) {
          values[change.legacyKey] = getRaw(change.legacyKey);
        });
        return {
          queued: results.some(function (result) { return result && result.queued; }),
          keys: normalized.map(function (change) { return change.legacyKey; }),
          values: values
        };
      });
    }
    function installMessageBridge() {
      if (
        options.messageBridge === false ||
        !trustedPwaWindow(options) ||
        typeof root.addEventListener !== 'function' ||
        typeof root.postMessage !== 'function'
      ) {
        return function () {};
      }
      var expectedOrigin = String(
        options.origin ||
        root.location && root.location.origin ||
        ''
      );
      function respond(type, requestId, payload) {
        assertFence(lease);
        root.postMessage({
          __bwReaderPreference: CONTRACT,
          direction: TO_EXTENSION,
          type: type,
          requestId: String(requestId || ''),
          payload: payload || {}
        }, expectedOrigin || '*');
        assertFence(lease);
      }
      function listener(event) {
        if (!event || event.source !== root) return;
        if (expectedOrigin && event.origin !== expectedOrigin) return;
        var data = event.data || {};
        if (
          data[MESSAGE_MARKER] !== CONTRACT ||
          data.direction !== TO_PAGE ||
          (data.type !== 'HELLO' && data.type !== 'PATCH')
        ) {
          return;
        }
        var requestId = String(data.requestId || '').slice(0, 160);
        var payload = data.payload || {};
        if (own(data, 'namespace') || own(payload, 'namespace')) {
          try {
            respond('RESULT', requestId, {
              ok: false,
              code: 'BW_PREFERENCE_NAMESPACE',
              error: 'Preference bridge 不接受 namespace'
            });
          } catch (_) {}
          return;
        }
        if (data.type === 'HELLO') {
          try {
            assertFence(lease);
            respond('READY', requestId, {
              values: currentValues(),
              allowedKeys: entries.map(function (entry) { return entry.legacyKey; })
            });
          } catch (_) {}
          return;
        }
        Promise.resolve().then(function () {
          assertFence(lease);
          return applyPatch(payload.changes, 'extension-legacy');
        }).then(function (result) {
          assertFence(lease);
          respond('RESULT', requestId, {
            ok: true,
            queued: !!result.queued,
            keys: result.keys,
            values: result.values
          });
        }).catch(function (error) {
          try {
            assertFence(lease);
            var normalized = asError(error, 'BW_PREFERENCE_PATCH');
            respond('RESULT', requestId, {
              ok: false,
              code: normalized.code,
              error: normalized.message
            });
          } catch (_) {}
        });
      }
      root.addEventListener('message', listener);
      return function () {
        try { root.removeEventListener('message', listener); } catch (_) {}
      };
    }

    prepareAccountMirror();

    var api = {
      contract: CONTRACT,
      namespace: function () { assertFence(lease); return namespace; },
      allowedKeys: function () {
        assertFence(lease);
        return entries.map(function (entry) { return entry.legacyKey; });
      },
      values: currentValues,
      getRaw: getRaw,
      setRaw: function (key, value) { return setRaw(key, value, 'preference-api'); },
      removeRaw: function (key) { return removeRaw(key, 'preference-api'); },
      applyPatch: function (changes) { return applyPatch(changes, 'preference-api'); },
      attach: attach,
      ready: function () { return attaching || Promise.resolve(api); },
      mirrorStorageKey: function () {
        assertFence(lease);
        return mirrorStorageKey(namespace);
      },
      destroy: function () {
        if (destroyed) return;
        unsubscribers.splice(0).forEach(function (unsubscribe) {
          try { unsubscribe(); } catch (_) {}
        });
        try { removeStoragePatch(); } catch (_) {}
        try { removeMessageBridge(); } catch (_) {}
        try { removeOwnerListener(); } catch (_) {}
        destroyed = true;
        attached = false;
        router = null;
      },
      _storage: webStorage,
      _managesKey: function (key) {
        return !!byLegacy[String(key || '')];
      },
      _externalSet: function (key, value) {
        if (!byLegacy[key]) return;
        assertFence(lease);
        validateRawValue(value);
        background(setRaw(key, value, 'localStorage', false), 'localStorage-set');
      },
      _externalRemove: function (key) {
        if (!byLegacy[key]) return;
        assertFence(lease);
        background(removeRaw(key, 'localStorage', false), 'localStorage-remove');
      },
      _externalClear: function () {
        assertFence(lease);
        var tasks = entries.map(function (entry) {
          return removeRaw(entry.legacyKey, 'localStorage-clear', false);
        });
        background(Promise.all(tasks), 'localStorage-clear');
        assertFence(lease);
      }
    };
    removeStoragePatch = installStoragePatch(api);
    removeMessageBridge = installMessageBridge();
    if (typeof root.addEventListener === 'function') {
      var ownerListener = function (event) {
        if (!event || event.storageArea && event.storageArea !== webStorage) return;
        if (String(event.key || '') !== OWNER_KEY) return;
        var actualOwner = String(event.newValue || '');
        if (actualOwner !== namespace) markOwnerStale(actualOwner);
      };
      root.addEventListener('storage', ownerListener);
      removeOwnerListener = function () {
        try { root.removeEventListener('storage', ownerListener); } catch (_) {}
      };
    }
    return Object.freeze(api);
  }

  return Object.freeze({
    CONTRACT: CONTRACT,
    OWNER_KEY: OWNER_KEY,
    MIRROR_PREFIX: MIRROR_PREFIX,
    MAX_VALUE_BYTES: MAX_VALUE_BYTES,
    MAX_PATCH_CHANGES: MAX_PATCH_CHANGES,
    PreferenceError: PreferenceError,
    createPreferenceStore: createPreferenceStore
  });
});
