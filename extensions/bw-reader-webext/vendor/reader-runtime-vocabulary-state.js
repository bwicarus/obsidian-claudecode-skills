/* vocabulary-state.js — 单词/词组用户状态的唯一、本地优先仓库。
 *
 * 这里只保存用户动作的结果（掌握、词组收藏），不接管 Vault 词典笔记、释义、熟练度
 * 算法或文档内下划线几何。PWA 通过 runtime.storage() 使用 IndexedDB；普通网页通过
 * 扩展后台的账户分区 Vault。UI 同步更新内存投影，持久化和服务器兼容同步均在后台进行。
 */
(function (root, factory) {
  var api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.BWReaderRuntime = root.BWReaderRuntime || {};
  root.BWReaderRuntime.vocabularyState = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function (root) {
  'use strict';

  var CONTRACT = 'vocabulary-state/1';
  var COLLECTION = 'vocabulary-state';
  var RECORD_SCHEMA = 1;
  var PAGE_SIZE = 200;
  var MAX_KEY_BYTES = 240;
  var MAX_ALIASES = 32;
  var MAX_ALIAS_BYTES = 4096;
  var VALID_KIND = { word: true, phrase: true };
  var VALID_LANGUAGE = { en: true, ja: true, und: true };
  // lookup(2026-09-03):查过一次即记,生词下划线的本地依据(此前只靠服务端查词日志,本地词典命中根本不出网)
  var VALID_PROPERTY = { mastered: true, favorite: true, lookup: true };
  var records = new Map();
  var pending = new Map();
  var listeners = [];
  var adapter = null;
  var desiredAdapter = null;
  var adapterScope = null;
  var adapterScopeKnown = false;
  var adapterGeneration = 0;
  var attachPromise = Promise.resolve({ attached: false, records: 0 });
  var unsubscribe = null;
  var unsubscribeReconnect = null;
  var unsubscribeInvalidate = null;
  var legacyStorage = null;

  function StateError(message, code, details) {
    this.name = 'VocabularyStateError';
    this.code = code || 'BW_VOCABULARY_STATE';
    this.message = String(message || '词汇状态错误');
    this.details = details || null;
    if (Error.captureStackTrace) Error.captureStackTrace(this, StateError);
  }
  StateError.prototype = Object.create(Error.prototype);
  StateError.prototype.constructor = StateError;

  function clone(value) {
    return value == null ? value : JSON.parse(JSON.stringify(value));
  }
  function utf8Bytes(value) {
    value = String(value || '');
    if (typeof TextEncoder !== 'undefined') return new TextEncoder().encode(value);
    var encoded = unescape(encodeURIComponent(value));
    var bytes = new Uint8Array(encoded.length);
    for (var i = 0; i < encoded.length; i++) bytes[i] = encoded.charCodeAt(i);
    return bytes;
  }
  function base64Url(bytes) {
    var binary = '';
    for (var i = 0; i < bytes.length; i += 0x8000) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    var encoded;
    if (typeof btoa === 'function') encoded = btoa(binary);
    else if (typeof Buffer !== 'undefined') encoded = Buffer.from(bytes).toString('base64');
    else throw new StateError('当前环境无法建立稳定词汇编号', 'BW_VOCABULARY_STATE_ID');
    return encoded.replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/g, '');
  }
  function checkedEnum(value, allowed, label, fallback) {
    value = String(value || fallback || '').trim().toLowerCase();
    if (!allowed[value]) {
      throw new StateError(label + '无效', 'BW_VOCABULARY_STATE_INPUT', {
        field: label,
        value: value
      });
    }
    return value;
  }
  function normalizeKey(value) {
    value = String(value == null ? '' : value);
    try { value = value.normalize('NFKC'); } catch (_) {}
    value = value.trim().replace(/\s+/g, ' ').toLocaleLowerCase('en-US');
    var bytes = utf8Bytes(value);
    if (!value || bytes.length > MAX_KEY_BYTES || /[\u0000-\u001f\u007f]/.test(value)) {
      throw new StateError('词汇状态键为空或过长', 'BW_VOCABULARY_STATE_INPUT');
    }
    return value;
  }
  function normalizedSpec(input, property) {
    input = input || {};
    var kind = checkedEnum(input.kind, VALID_KIND, 'kind', 'word');
    var language = checkedEnum(input.language, VALID_LANGUAGE, 'language', 'und');
    property = checkedEnum(
      property || input.property,
      VALID_PROPERTY,
      'property',
      'mastered'
    );
    if (kind !== 'phrase' && property === 'favorite') {
      throw new StateError('只有词组可以收藏', 'BW_VOCABULARY_STATE_INPUT');
    }
    var canonical = input.lemma || input.key || input.text || input.word;
    var key = normalizeKey(canonical);
    var aliases = [];
    [input.word, input.text, input.surface].concat(input.forms || [], input.aliases || [])
      .filter(function (value) { return value != null && String(value).trim(); })
      .forEach(function (value) {
        var normalized;
        try { normalized = normalizeKey(value); } catch (_) { return; }
        if (normalized !== key && aliases.indexOf(normalized) < 0) aliases.push(normalized);
      });
    aliases.sort();
    if (
      aliases.length > MAX_ALIASES ||
      aliases.reduce(function (total, alias) {
        return total + utf8Bytes(alias).length;
      }, 0) > MAX_ALIAS_BYTES
    ) {
      throw new StateError(
        '词汇状态别名数量或总长度超出上限',
        'BW_VOCABULARY_STATE_INPUT'
      );
    }
    return {
      kind: kind,
      language: language,
      property: property,
      key: key,
      aliases: aliases
    };
  }
  function idFor(input, property) {
    var spec = normalizedSpec(input, property);
    return [
      'vstate-v1',
      spec.property,
      spec.kind,
      spec.language,
      base64Url(utf8Bytes(spec.key))
    ].join('.');
  }
  function normalizeRecord(value) {
    value = value && value.value && value.collection ? value.value : value;
    value = value || {};
    var spec = normalizedSpec(value, value.property);
    var id = idFor(spec, spec.property);
    if (value.id != null && String(value.id) !== id) {
      throw new StateError('词汇状态编号与内容不一致', 'BW_VOCABULARY_STATE_ID');
    }
    return {
      id: id,
      schema: RECORD_SCHEMA,
      property: spec.property,
      kind: spec.kind,
      language: spec.language,
      key: spec.key,
      aliases: spec.aliases,
      enabled: value.enabled === true
    };
  }
  function mutationId(record) {
    var suffix = '';
    try {
      if (root.crypto && typeof root.crypto.getRandomValues === 'function') {
        var bytes = new Uint8Array(12);
        root.crypto.getRandomValues(bytes);
        suffix = Array.prototype.map.call(bytes, function (byte) {
          return byte.toString(16).padStart(2, '0');
        }).join('');
      }
    } catch (_) {}
    if (!suffix) suffix = Date.now().toString(36) + Math.random().toString(36).slice(2, 14);
    return 'vstate-mut-v1:' + record.id + ':' + suffix;
  }
  function eventFor(record, source) {
    return {
      contract: CONTRACT,
      source: source || 'local',
      record: clone(record)
    };
  }
  function notify(record, source) {
    var event = eventFor(record, source);
    listeners.slice().forEach(function (listener) {
      try { listener(clone(event)); } catch (_) {}
    });
    try {
      if (root.document && typeof root.CustomEvent === 'function') {
        root.document.dispatchEvent(new root.CustomEvent(
          'bw:vocabulary-state-change',
          { detail: clone(event) }
        ));
      }
    } catch (_) {}
  }
  function exactRecord(spec, property, language) {
    var candidate = Object.assign({}, spec, { language: language || spec.language });
    return records.get(idFor(candidate, property)) || null;
  }
  function lookup(input, property) {
    var spec;
    try { spec = normalizedSpec(input, property); } catch (_) { return null; }
    var direct = exactRecord(spec, property, spec.language);
    if (direct) return clone(direct);
    if (spec.language !== 'und') {
      var legacy = exactRecord(spec, property, 'und');
      if (legacy) return clone(legacy);
    }
    var candidates = [spec.key].concat(spec.aliases);
    var languages = spec.language === 'und' ? ['und'] : [spec.language, 'und'];
    for (var li = 0; li < languages.length; li++) {
      var language = languages[li];
      var found = null;
      records.forEach(function (record) {
        if (found ||
            record.property !== property ||
            record.kind !== spec.kind ||
            record.language !== language) return;
        var keys = [record.key].concat(record.aliases || []);
        if (candidates.some(function (key) { return keys.indexOf(key) >= 0; })) found = record;
      });
      if (found) return clone(found);
    }
    return null;
  }
  function enabled(input, property) {
    var found = lookup(input, property);
    return !!(found && found.enabled);
  }
  function writeLegacyMirror() {
    if (!legacyStorage) return;
    var mastered = [];
    records.forEach(function (record) {
      if (
        record.property === 'mastered' &&
        record.kind === 'word' &&
        record.enabled &&
        mastered.indexOf(record.key) < 0
      ) mastered.push(record.key);
    });
    mastered.sort();
    try {
      legacyStorage.setItem('vocab-mastered-v1', JSON.stringify({
        ts: Date.now(),
        set: mastered,
        source: CONTRACT
      }));
    } catch (_) {}
  }
  function acceptRecord(value, source, onlyMissing) {
    var record;
    try { record = normalizeRecord(value); } catch (_) { return false; }
    var current = records.get(record.id);
    if (pending.has(record.id)) return false;
    if (onlyMissing && current) return false;
    if (current && JSON.stringify(current) === JSON.stringify(record)) return false;
    records.set(record.id, record);
    writeLegacyMirror();
    notify(record, source || 'hydrate');
    return true;
  }
  function persist(record, id) {
    var generation = adapterGeneration;
    pending.set(record.id, record);
    if (!adapter) {
      return Promise.resolve({ queued: true, record: clone(record) });
    }
    return Promise.resolve(adapter.put(clone(record), id || mutationId(record)))
      .then(function (result) {
        if (generation !== adapterGeneration) {
          throw new StateError('词汇存储已切换，写入结果不可确认', 'BW_VOCABULARY_STATE_STALE');
        }
        var current = pending.get(record.id);
        if (current === record) pending.delete(record.id);
        return result || { record: clone(record) };
      });
  }
  function setProperty(input, property, value, options) {
    options = options || {};
    var spec = normalizedSpec(input, property);
    var id = idFor(spec, property);
    var previous = records.get(id) || null;
    var record = normalizeRecord({
      id: id,
      property: property,
      kind: spec.kind,
      language: spec.language,
      key: spec.key,
      aliases: spec.aliases,
      enabled: value === true
    });
    records.set(id, record);
    writeLegacyMirror();
    notify(record, options.source || 'local');
    var durable = persist(record, options.mutationId);
    return {
      applied: true,
      record: clone(record),
      durable: durable,
      undo: function () {
        var restored = previous
          ? Object.assign({}, previous)
          : Object.assign({}, record, { enabled: false });
        records.set(id, restored);
        writeLegacyMirror();
        notify(restored, 'undo');
        return persist(restored);
      }
    };
  }
  function importLegacy() {
    if (!legacyStorage) return;
    var mastered = [];
    var overrides = {};
    try {
      var snapshot = JSON.parse(legacyStorage.getItem('vocab-mastered-v1') || 'null');
      if (snapshot && Array.isArray(snapshot.set)) mastered = snapshot.set;
    } catch (_) {}
    try {
      var parsed = JSON.parse(legacyStorage.getItem('vocab-override-v1') || '{}');
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) overrides = parsed;
    } catch (_) {}
    mastered.forEach(function (key) {
      try {
        var spec = normalizedSpec({ kind: 'word', language: 'und', key: key }, 'mastered');
        var record = normalizeRecord(Object.assign({}, spec, {
          enabled: true
        }));
        if (!records.has(record.id)) {
          records.set(record.id, record);
          pending.set(record.id, record);
        }
      } catch (_) {}
    });
    Object.keys(overrides).forEach(function (key) {
      try {
        var item = overrides[key] || {};
        var spec = normalizedSpec({ kind: 'word', language: 'und', key: key }, 'mastered');
        var record = normalizeRecord(Object.assign({}, spec, {
          enabled: item.v === true
        }));
        records.set(record.id, record);
        pending.set(record.id, record);
      } catch (_) {}
    });
    writeLegacyMirror();
  }
  function flushPending(generation) {
    var queue = Array.from(pending.values()).sort(function (left, right) {
      return left.id.localeCompare(right.id);
    });
    return queue.reduce(function (promise, record) {
      return promise.then(function () {
        if (generation !== adapterGeneration || !adapter) return null;
        return persist(record);
      });
    }, Promise.resolve());
  }
  function listAll(sourceAdapter, generation) {
    var output = [];
    function page(offset) {
      if (generation !== adapterGeneration) return Promise.resolve(output);
      return Promise.resolve(sourceAdapter.list({
        offset: offset,
        limit: PAGE_SIZE,
        includeDeleted: false
      })).then(function (items) {
        items = Array.isArray(items) ? items : [];
        Array.prototype.push.apply(output, items);
        if (items.length < PAGE_SIZE) return output;
        return page(offset + items.length);
      });
    }
    return page(0);
  }
  function adapterScopeOf(nextAdapter) {
    if (typeof nextAdapter.identity !== 'function') {
      return Promise.resolve(nextAdapter);
    }
    return Promise.resolve(nextAdapter.identity()).then(function (value) {
      value = String(value || '').trim();
      if (
        !value ||
        value.length > 512 ||
        /[\u0000-\u001f\u007f]/.test(value)
      ) {
        throw new StateError(
          '词汇状态适配器返回了无效账户作用域',
          'BW_VOCABULARY_STATE_SCOPE'
        );
      }
      return value;
    });
  }
  function resetForScope(nextScope) {
    if (!adapterScopeKnown || adapterScope === nextScope) return false;
    records.clear();
    pending.clear();
    writeLegacyMirror();
    try {
      if (root.document && typeof root.CustomEvent === 'function') {
        root.document.dispatchEvent(new root.CustomEvent(
          'bw:vocabulary-state-reset',
          { detail: { contract: CONTRACT, reason: 'adapter-scope-changed' } }
        ));
      }
    } catch (_) {}
    return true;
  }
  function attach(nextAdapter) {
    if (!nextAdapter ||
        typeof nextAdapter.list !== 'function' ||
        typeof nextAdapter.put !== 'function') {
      return Promise.reject(new StateError(
        '词汇状态适配器必须实现 list/put',
        'BW_VOCABULARY_STATE_ADAPTER'
      ));
    }
    if (unsubscribe) {
      try { unsubscribe(); } catch (_) {}
      unsubscribe = null;
    }
    if (unsubscribeReconnect) {
      try { unsubscribeReconnect(); } catch (_) {}
      unsubscribeReconnect = null;
    }
    if (unsubscribeInvalidate) {
      try { unsubscribeInvalidate(); } catch (_) {}
      unsubscribeInvalidate = null;
    }
    adapter = null;
    desiredAdapter = nextAdapter;
    var generation = ++adapterGeneration;
    if (typeof nextAdapter.onReconnect === 'function') {
      unsubscribeReconnect = nextAdapter.onReconnect(function () {
        if (desiredAdapter === nextAdapter) attach(nextAdapter).catch(function () {});
      });
    }
    if (typeof nextAdapter.onInvalidate === 'function') {
      unsubscribeInvalidate = nextAdapter.onInvalidate(function () {
        if (desiredAdapter !== nextAdapter) return;
        adapter = null;
        adapterGeneration += 1;
        attachPromise = Promise.resolve({
          attached: false,
          suspended: true,
          records: records.size
        });
      });
    }
    attachPromise = adapterScopeOf(nextAdapter).then(function (nextScope) {
      if (generation !== adapterGeneration) return { attached: false, stale: true };
      resetForScope(nextScope);
      adapter = nextAdapter;
      adapterScope = nextScope;
      adapterScopeKnown = true;
      return listAll(nextAdapter, generation).then(function (items) {
        if (generation !== adapterGeneration) return { attached: false, stale: true };
        items.forEach(function (item) { acceptRecord(item, 'hydrate', false); });
        if (typeof nextAdapter.subscribe === 'function') {
          unsubscribe = nextAdapter.subscribe(function (change) {
            if (generation !== adapterGeneration) return;
            acceptRecord(change && (change.record || change), 'provider', false);
          });
        }
        return flushPending(generation).then(function () {
          var result = { attached: true, records: records.size };
          try {
            if (root.document && typeof root.CustomEvent === 'function') {
              root.document.dispatchEvent(new root.CustomEvent(
                'bw:vocabulary-state-ready',
                { detail: clone(result) }
              ));
            }
          } catch (_) {}
          return result;
        });
      });
    });
    return attachPromise;
  }
  function pwaAdapter() {
    var runtime = root.__BW_READER_RUNTIME__;
    if (!runtime || typeof runtime.storage !== 'function') return null;
    var storage = runtime.storage();
    var queues = Object.create(null);
    function serialized(id, work) {
      var before = queues[id] || Promise.resolve();
      var current = before.catch(function () {}).then(work);
      queues[id] = current;
      return current.then(function (result) {
        if (queues[id] === current) delete queues[id];
        return result;
      }, function (error) {
        if (queues[id] === current) delete queues[id];
        throw error;
      });
    }
    return {
      identity: function () {
        var pwa = root.BWReaderRuntime && root.BWReaderRuntime.pwaRuntime;
        var namespace = pwa && typeof pwa.namespace === 'function'
          ? pwa.namespace()
          : (root.__USER__ && root.__USER__.storage_namespace);
        namespace = String(namespace || '').trim();
        return namespace ? 'pwa:' + namespace : 'pwa:unscoped';
      },
      list: function (query) { return storage.list(COLLECTION, query || {}); },
      put: function (value, mutation) {
        return serialized(value.id, function () {
          return storage.get(COLLECTION, value.id, { includeDeleted: true })
            .then(function (current) {
              if (
                current &&
                JSON.stringify(current.value || null) === JSON.stringify(value)
              ) {
                return current;
              }
              return storage.put(COLLECTION, value, {
                id: value.id,
                ifRev: Number(current && current.rev || 0),
                mutationId: mutation
              });
            });
        });
      },
      subscribe: function (listener) {
        return storage.subscribe(COLLECTION, listener);
      }
    };
  }
  function autoAttach() {
    if (root.__bwVocabularyStateTransport) {
      attach(root.__bwVocabularyStateTransport).catch(function () {});
      return;
    }
    var found = pwaAdapter();
    if (found) attach(found).catch(function () {});
  }

  if (root.__USER__ && root.localStorage) {
    legacyStorage = root.localStorage;
    importLegacy();
  }
  if (root.document && typeof root.document.addEventListener === 'function') {
    root.document.addEventListener('bw:reader-runtime-ready', autoAttach);
  }
  // The native App publishes its storage router only after the local runtime
  // has opened IndexedDB. The eager microtask above can therefore see the
  // runtime API while storage() still returns null. Retry on the native ready
  // edge instead of leaving the in-memory/legacy mirror detached for the rest
  // of the book session.
  if (root && typeof root.addEventListener === 'function') {
    root.addEventListener('bw:native-local-runtime-ready', autoAttach);
  }
  Promise.resolve().then(autoAttach);

  return Object.freeze({
    CONTRACT: CONTRACT,
    COLLECTION: COLLECTION,
    RECORD_SCHEMA: RECORD_SCHEMA,
    StateError: StateError,
    normalizeKey: normalizeKey,
    normalizeRecord: normalizeRecord,
    idFor: idFor,
    lookup: lookup,
    isMastered: function (input) { return enabled(input, 'mastered'); },
    isPhraseFavorite: function (input) {
      return enabled(Object.assign({}, input || {}, { kind: 'phrase' }), 'favorite');
    },
    setMastered: function (input, value, options) {
      return setProperty(input, 'mastered', value, options);
    },
    setPhraseFavorite: function (input, value, options) {
      input = Object.assign({}, input || {}, { kind: 'phrase' });
      return setProperty(input, 'favorite', value, options);
    },
    isLookedUp: function (input) { return enabled(input, 'lookup'); },
    setLookedUp: function (input, value, options) {
      return setProperty(input, 'lookup', value !== false, options);
    },
    importRecord: function (value, options) {
      return acceptRecord(value, options && options.source, options && options.onlyMissing);
    },
    snapshot: function () {
      return Array.from(records.values()).sort(function (left, right) {
        return left.id.localeCompare(right.id);
      }).map(clone);
    },
    subscribe: function (listener) {
      if (typeof listener !== 'function') {
        throw new StateError('listener 必须是函数', 'BW_VOCABULARY_STATE_LISTENER');
      }
      listeners.push(listener);
      return function () {
        var index = listeners.indexOf(listener);
        if (index >= 0) listeners.splice(index, 1);
      };
    },
    attach: attach,
    ready: function () { return attachPromise; },
    status: function () {
      return {
        contract: CONTRACT,
        collection: COLLECTION,
        records: records.size,
        pending: pending.size,
        attached: !!adapter,
        scoped: adapterScopeKnown
      };
    }
  });
});
