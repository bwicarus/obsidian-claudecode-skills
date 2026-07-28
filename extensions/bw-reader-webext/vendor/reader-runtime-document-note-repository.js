/* document-note-repository.js — 文档便签的共享、本地优先仓库。
 *
 * 业务身份始终是 (documentId, noteId)。DataStore 负责 rev/ifRev、mutationId、
 * journal 和 tombstone；本层只定义便签记录、锚点 envelope 与字段级并发语义。
 * Anchor.data 对本层完全不透明，不在这里解释 PDF/EPUB/HTML 的私有坐标。
 */
(function (root, factory) {
  var runtime = root.BWReaderRuntime || {};
  var dataStore = runtime.dataStore;
  var dataRegistry = runtime.dataRegistry;
  if (typeof module === 'object' && module.exports && typeof require === 'function') {
    if (!dataStore) dataStore = require('./data-store.js');
    if (!dataRegistry) dataRegistry = require('./data-registry.js');
  }
  var api = factory(root, dataStore, dataRegistry);
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.BWReaderRuntime = root.BWReaderRuntime || {};
  root.BWReaderRuntime.documentNoteRepository = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function (
  root,
  dataStoreApi,
  defaultRegistry
) {
  'use strict';

  var CONTRACT = 'document-note-repository/1';
  var COLLECTION = 'document-notes';
  var RECORD_SCHEMA = 1;
  var MAX_FIELDS = 128;
  var MAX_FIELD_NAME = 80;
  var MAX_DOCUMENT_ID = 4096;
  var MAX_MUTATIONS = 64;
  var MAX_REBASE_ATTEMPTS = 8;
  var NOTE_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$/;
  var CLIENT_ID_RE = /^c_[a-f0-9]{32}$/;
  var RESERVED_FIELDS = {
    anchor: true,
    collection: true,
    contract: true,
    deleted: true,
    documentId: true,
    fields: true,
    id: true,
    noteId: true,
    rev: true,
    schema: true,
    updatedAt: true,
    updatedBy: true,
    _meta: true
  };

  function NoteError(message, code, details) {
    this.name = 'DocumentNoteError';
    this.code = code || 'BW_NOTE_ERROR';
    this.message = String(message || '文档便签错误');
    this.details = details || null;
    if (Error.captureStackTrace) Error.captureStackTrace(this, NoteError);
  }
  NoteError.prototype = Object.create(Error.prototype);
  NoteError.prototype.constructor = NoteError;

  function NoteConflictError(message, details) {
    NoteError.call(
      this,
      message || '文档便签存在并发冲突',
      'BW_NOTE_CONFLICT',
      details || null
    );
    this.name = 'DocumentNoteConflictError';
  }
  NoteConflictError.prototype = Object.create(NoteError.prototype);
  NoteConflictError.prototype.constructor = NoteConflictError;

  function own(value, key) {
    return Object.prototype.hasOwnProperty.call(value || {}, key);
  }
  function plain(value) {
    return !!(
      value &&
      Object.prototype.toString.call(value) === '[object Object]' &&
      (Object.getPrototypeOf(value) === Object.prototype ||
       Object.getPrototypeOf(value) === null)
    );
  }
  function requireDataStore() {
    if ((!dataStoreApi || dataStoreApi.CONTRACT !== 'data-store/1') &&
        root.BWReaderRuntime && root.BWReaderRuntime.dataStore) {
      dataStoreApi = root.BWReaderRuntime.dataStore;
    }
    if (
      !dataStoreApi ||
      dataStoreApi.CONTRACT !== 'data-store/1' ||
      typeof dataStoreApi.cloneJSON !== 'function' ||
      typeof dataStoreApi.normalizeExpectedRev !== 'function'
    ) {
      throw new NoteError(
        '缺少完整 DataStore 合同',
        'BW_NOTE_DATA_STORE'
      );
    }
    return dataStoreApi;
  }
  function cloneJSON(value, path) {
    try {
      return requireDataStore().cloneJSON(value, path || 'note');
    } catch (error) {
      if (error instanceof NoteError) throw error;
      throw new NoteError(
        String(error && error.message || '便签包含无效数据'),
        String(error && error.code || 'BW_NOTE_INPUT'),
        error && error.details || null
      );
    }
  }
  function documentIdOf(value) {
    value = String(value == null ? '' : value).trim();
    if (
      !value ||
      value.length > MAX_DOCUMENT_ID ||
      /[\u0000-\u001f\u007f]/.test(value)
    ) {
      throw new NoteError(
        'documentId 为空、过长或含控制字符',
        'BW_NOTE_IDENTITY'
      );
    }
    return value;
  }
  function noteIdOf(value) {
    value = String(value == null ? '' : value).trim();
    if (!NOTE_ID_RE.test(value)) {
      throw new NoteError(
        'noteId 必须是 1-160 位稳定安全编号',
        'BW_NOTE_IDENTITY'
      );
    }
    return value;
  }
  function storageIdFor(documentId, noteId) {
    documentId = documentIdOf(documentId);
    noteId = noteIdOf(noteId);
    return 'document-note-v1:' + documentId.length + ':' + documentId + ':' + noteId;
  }
  function secureClientId() {
    var cryptoApi = root.crypto;
    var hex = '';
    try {
      if (cryptoApi && typeof cryptoApi.randomUUID === 'function') {
        var uuid = String(cryptoApi.randomUUID() || '').toLowerCase();
        if (/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(uuid)) {
          hex = uuid.replace(/-/g, '');
        }
      }
      if (!hex && cryptoApi && typeof cryptoApi.getRandomValues === 'function') {
        var bytes = new Uint8Array(16);
        cryptoApi.getRandomValues(bytes);
        hex = Array.prototype.map.call(bytes, function (value) {
          return value.toString(16).padStart(2, '0');
        }).join('');
      }
    } catch (_) {}
    if (!/^[a-f0-9]{32}$/.test(hex)) {
      throw new NoteError(
        '无法安全生成客户端便签编号',
        'BW_NOTE_RANDOM'
      );
    }
    return 'c_' + hex;
  }
  function generatedNoteId(idFactory) {
    var value = typeof idFactory === 'function' ? idFactory() : secureClientId();
    value = String(value || '').toLowerCase();
    if (!CLIENT_ID_RE.test(value)) {
      throw new NoteError(
        '客户端便签编号必须是 c_ 加 32 位十六进制',
        'BW_NOTE_RANDOM'
      );
    }
    return value;
  }
  function mutationIdOf(options) {
    if (!options || options.mutationId == null) return '';
    var value = String(options.mutationId).trim();
    if (!value || value.length > 512 || /[\u0000-\u001f\u007f]/.test(value)) {
      throw new NoteError('mutationId 无效', 'BW_NOTE_MUTATION');
    }
    return value;
  }
  function expectedRevision(options, required) {
    if ((!options || options.ifRev == null) && required) {
      throw new NoteError(
        'patch/remove 必须提供 ifRev',
        'BW_NOTE_REVISION'
      );
    }
    try {
      return requireDataStore().normalizeExpectedRev(
        options && options.ifRev,
        'ifRev'
      );
    } catch (error) {
      throw new NoteError(
        String(error && error.message || 'ifRev 无效'),
        'BW_NOTE_REVISION'
      );
    }
  }
  function normalizeAnchor(value, documentId) {
    if (!plain(value)) {
      throw new NoteError(
        'anchor 必须是 DocumentHost 的不透明 envelope',
        'BW_NOTE_ANCHOR'
      );
    }
    var anchorDocumentId = documentIdOf(value.documentId);
    if (anchorDocumentId !== documentId) {
      throw new NoteError(
        'anchor.documentId 与便签 documentId 不一致',
        'BW_NOTE_ANCHOR',
        { expected: documentId, actual: anchorDocumentId }
      );
    }
    var kind = String(value.kind || '').trim();
    var revision = Number(value.revision);
    if (
      !kind ||
      kind.length > 80 ||
      /[\u0000-\u001f\u007f]/.test(kind) ||
      !Number.isSafeInteger(revision) ||
      revision < 1 ||
      !own(value, 'data')
    ) {
      throw new NoteError(
        'anchor envelope 缺少合法的 kind/revision/data',
        'BW_NOTE_ANCHOR'
      );
    }
    return {
      documentId: anchorDocumentId,
      kind: kind,
      revision: revision,
      // 只验证 JSON 可存储性；绝不解释宿主私有字段。
      data: cloneJSON(value.data, 'anchor.data')
    };
  }
  function fieldNameOf(value) {
    value = String(value || '');
    if (
      !value ||
      value.length > MAX_FIELD_NAME ||
      RESERVED_FIELDS[value] ||
      value.charAt(0) === '_' ||
      value === '__proto__' ||
      value === 'prototype' ||
      value === 'constructor' ||
      /[\u0000-\u001f\u007f]/.test(value)
    ) {
      throw new NoteError(
        '便签字段名无效或已保留：' + value,
        'BW_NOTE_FIELD'
      );
    }
    return value;
  }
  function collectFields(input, mode) {
    if (!plain(input)) {
      throw new NoteError(
        (mode === 'patch' ? 'patch' : 'create') + ' 输入必须是普通对象',
        'BW_NOTE_INPUT'
      );
    }
    var output = {};
    var count = 0;
    function add(name, value) {
      name = fieldNameOf(name);
      if (own(output, name)) {
        throw new NoteError(
          '便签字段重复：' + name,
          'BW_NOTE_FIELD'
        );
      }
      output[name] = cloneJSON(value, 'fields.' + name);
      count += 1;
      if (count > MAX_FIELDS) {
        throw new NoteError(
          '单张便签字段超过上限',
          'BW_NOTE_FIELD'
        );
      }
    }
    if (own(input, 'fields')) {
      if (!plain(input.fields)) {
        throw new NoteError('fields 必须是普通对象', 'BW_NOTE_INPUT');
      }
      Object.keys(input.fields).forEach(function (name) {
        add(name, input.fields[name]);
      });
    }
    Object.keys(input).forEach(function (name) {
      if (
        name === 'fields' ||
        name === 'anchor' ||
        name === 'documentId' ||
        name === 'noteId' ||
        name === 'id'
      ) return;
      if (RESERVED_FIELDS[name]) {
        throw new NoteError(
          '不能写入仓库保留字段：' + name,
          'BW_NOTE_FIELD'
        );
      }
      add(name, input[name]);
    });
    return output;
  }
  function canonical(value) {
    if (value === null || typeof value !== 'object') return JSON.stringify(value);
    if (Array.isArray(value)) {
      return '[' + value.map(canonical).join(',') + ']';
    }
    return '{' + Object.keys(value).sort().map(function (key) {
      return JSON.stringify(key) + ':' + canonical(value[key]);
    }).join(',') + '}';
  }
  function operationSignature(operation, anchorChanged, anchor, fields) {
    return operation + ':' + canonical({
      anchorChanged: anchorChanged === true,
      anchor: anchorChanged ? anchor : null,
      fields: fields || {}
    });
  }
  function emptyMeta() {
    return { fieldRevs: {}, mutations: [] };
  }
  function normalizeMeta(value) {
    var output = emptyMeta();
    value = plain(value) ? value : {};
    if (plain(value.fieldRevs)) {
      Object.keys(value.fieldRevs).forEach(function (field) {
        var revision = Number(value.fieldRevs[field]);
        if (Number.isSafeInteger(revision) && revision > 0) {
          output.fieldRevs[field] = revision;
        }
      });
    }
    if (Array.isArray(value.mutations)) {
      value.mutations.slice(-MAX_MUTATIONS).forEach(function (entry) {
        if (
          entry &&
          typeof entry.id === 'string' &&
          typeof entry.operation === 'string' &&
          typeof entry.signature === 'string'
        ) {
          output.mutations.push({
            id: entry.id,
            operation: entry.operation,
            signature: entry.signature,
            rev: Math.max(0, Number(entry.rev) || 0)
          });
        }
      });
    }
    return output;
  }
  function validFieldRevision(value, maximum) {
    value = Number(value);
    maximum = Number(maximum);
    return Number.isSafeInteger(value) &&
      value > 0 &&
      Number.isSafeInteger(maximum) &&
      value <= maximum
      ? value
      : 0;
  }
  function trackedFieldName(path) {
    path = String(path || '');
    if (path === 'anchor') return path;
    if (path.indexOf('fields.') !== 0) return '';
    return 'fields.' + fieldNameOf(path.slice('fields.'.length));
  }
  function normalizeIncomingRecordMeta(record, current) {
    var incomingRevision = Number(record && record.rev);
    if (!Number.isSafeInteger(incomingRevision) || incomingRevision < 1) {
      throw new NoteError(
        '远端便签 revision 无效',
        'BW_NOTE_CORRUPT',
        { rev: record && record.rev }
      );
    }
    var incomingValue = record.value;
    var incomingRawMeta = plain(incomingValue._meta)
      ? incomingValue._meta
      : {};
    var incomingRawFieldRevs = plain(incomingRawMeta.fieldRevs)
      ? incomingRawMeta.fieldRevs
      : {};
    var incomingMeta = normalizeMeta(incomingRawMeta);
    var currentValue = current && current.value;
    var currentRevision = Number(current && current.rev || 0);
    var currentRawMeta = currentValue && plain(currentValue._meta)
      ? currentValue._meta
      : {};
    var currentRawFieldRevs = plain(currentRawMeta.fieldRevs)
      ? currentRawMeta.fieldRevs
      : {};
    var tracked = { anchor: true };

    Object.keys(incomingValue.fields).forEach(function (field) {
      tracked['fields.' + fieldNameOf(field)] = true;
    });
    if (currentValue && plain(currentValue.fields)) {
      Object.keys(currentValue.fields).forEach(function (field) {
        tracked['fields.' + fieldNameOf(field)] = true;
      });
    }
    [incomingRawFieldRevs, currentRawFieldRevs].forEach(function (fieldRevs) {
      Object.keys(fieldRevs).forEach(function (path) {
        var normalized = trackedFieldName(path);
        if (normalized) tracked[normalized] = true;
      });
    });

    incomingMeta.fieldRevs = {};
    Object.keys(tracked).forEach(function (path) {
      var incomingOwn;
      var currentOwn;
      var incomingField;
      var currentField;
      if (path === 'anchor') {
        incomingOwn = true;
        currentOwn = !!currentValue;
        incomingField = incomingValue.anchor;
        currentField = currentValue && currentValue.anchor;
      } else {
        var field = path.slice('fields.'.length);
        incomingOwn = own(incomingValue.fields, field);
        currentOwn = !!(
          currentValue &&
          plain(currentValue.fields) &&
          own(currentValue.fields, field)
        );
        incomingField = incomingOwn ? incomingValue.fields[field] : null;
        currentField = currentOwn ? currentValue.fields[field] : null;
      }
      var changed = !currentValue ||
        incomingOwn !== currentOwn ||
        (incomingOwn && canonical(incomingField) !== canonical(currentField));
      var incomingFieldRevision = validFieldRevision(
        incomingRawFieldRevs[path],
        incomingRevision
      );
      var currentFieldRevision = validFieldRevision(
        currentRawFieldRevs[path],
        currentRevision
      );

      // 远端快照改变了某字段时，该字段的变更版本只能是整个快照的
      // revision。不能信任远端提供的更旧 fieldRev，否则旧页面可绕过
      // same-field 检查覆盖新内容。未改变字段则尽量保留双方的合法历史；
      // 缺失/损坏时采取保守版本，宁可显式冲突也不静默丢数据。
      if (changed) {
        incomingMeta.fieldRevs[path] = incomingRevision;
      } else if (incomingFieldRevision || currentFieldRevision) {
        incomingMeta.fieldRevs[path] = Math.max(
          incomingFieldRevision,
          currentFieldRevision
        );
      } else {
        incomingMeta.fieldRevs[path] = incomingRevision;
      }
    });
    incomingValue._meta = incomingMeta;
    return record;
  }
  function rememberMutation(meta, mutationId, operation, signature, revision) {
    if (!mutationId) return;
    meta.mutations = meta.mutations.filter(function (entry) {
      return entry.id !== mutationId;
    });
    meta.mutations.push({
      id: mutationId,
      operation: operation,
      signature: signature,
      rev: revision
    });
    if (meta.mutations.length > MAX_MUTATIONS) {
      meta.mutations.splice(0, meta.mutations.length - MAX_MUTATIONS);
    }
  }
  function findMutation(meta, mutationId) {
    if (!mutationId) return null;
    for (var i = meta.mutations.length - 1; i >= 0; i -= 1) {
      if (meta.mutations[i].id === mutationId) return meta.mutations[i];
    }
    return null;
  }
  function noteValue(documentId, noteId, anchor, fields, meta) {
    return {
      schema: RECORD_SCHEMA,
      documentId: documentId,
      noteId: noteId,
      anchor: cloneJSON(anchor, 'anchor'),
      fields: cloneJSON(fields || {}, 'fields'),
      _meta: cloneJSON(meta || emptyMeta(), '_meta')
    };
  }
  function assertStoredRecord(record, documentId, noteId) {
    if (!record || !plain(record.value)) {
      throw new NoteError('便签记录损坏', 'BW_NOTE_CORRUPT');
    }
    var value = record.value;
    if (
      value.schema !== RECORD_SCHEMA ||
      value.documentId !== documentId ||
      value.noteId !== noteId ||
      record.id !== storageIdFor(documentId, noteId) ||
      !plain(value.fields)
    ) {
      throw new NoteError(
        '便签记录身份或 schema 不一致',
        'BW_NOTE_CORRUPT',
        { documentId: documentId, noteId: noteId, recordId: record.id }
      );
    }
    normalizeAnchor(value.anchor, documentId);
    return record;
  }
  function projectRecord(record, documentId, noteId) {
    assertStoredRecord(record, documentId, noteId);
    var value = record.value;
    var note = {
      contract: CONTRACT,
      schema: RECORD_SCHEMA,
      id: noteId,
      noteId: noteId,
      documentId: documentId,
      rev: Number(record.rev) || 0,
      updatedAt: Number(record.updatedAt) || 0,
      updatedBy: String(record.updatedBy || ''),
      deleted: record.deleted === true,
      anchor: normalizeAnchor(value.anchor, documentId)
    };
    Object.keys(value.fields).forEach(function (field) {
      fieldNameOf(field);
      note[field] = cloneJSON(value.fields[field], 'fields.' + field);
    });
    return note;
  }
  function conflict(documentId, noteId, reason, fields, expected, actual) {
    return new NoteConflictError(
      '文档便签冲突：' + documentId + '/' + noteId,
      {
        documentId: documentId,
        noteId: noteId,
        reason: String(reason || 'conflict'),
        fields: (fields || []).slice(),
        expectedRev: expected == null ? null : expected,
        actualRev: actual == null ? null : actual
      }
    );
  }
  function notFound(documentId, noteId) {
    return new NoteError(
      '文档便签不存在：' + documentId + '/' + noteId,
      'BW_NOTE_NOT_FOUND',
      { documentId: documentId, noteId: noteId }
    );
  }
  function assertRegistry(registry) {
    if (
      !registry ||
      registry.CONTRACT !== 'data-registry/1' ||
      typeof registry.collection !== 'function'
    ) {
      throw new NoteError(
        '缺少完整 DataRegistry',
        'BW_NOTE_REGISTRY'
      );
    }
    var declaration = registry.collection(COLLECTION);
    if (
      !declaration ||
      declaration.status !== 'ready' ||
      declaration.scope !== 'document' ||
      declaration.provider !== false
    ) {
      throw new NoteError(
        'document-notes 必须声明为 ready/document/provider=false',
        'BW_NOTE_REGISTRY',
        declaration || null
      );
    }
    return declaration;
  }
  function ensureStore(store) {
    ['get', 'list', 'put', 'remove', 'applyChanges', 'subscribe'].forEach(function (method) {
      if (!store || typeof store[method] !== 'function') {
        throw new NoteError(
          '便签 store 缺少 ' + method,
          'BW_NOTE_STORE'
        );
      }
    });
    return store;
  }
  function dataConflict(error) {
    return !!(error && error.code === 'BW_DATA_CONFLICT');
  }

  function createDocumentNoteRepository(options) {
    options = options || {};
    requireDataStore();
    assertRegistry(
      options.dataRegistry ||
      defaultRegistry ||
      root.BWReaderRuntime && root.BWReaderRuntime.dataRegistry
    );
    var store = options.store;
    if (!store && root.__BW_READER_RUNTIME__ &&
        typeof root.__BW_READER_RUNTIME__.storage === 'function') {
      store = root.__BW_READER_RUNTIME__.storage();
    }
    store = ensureStore(store);
    var idFactory = options.idFactory;
    // 仓库级操作必须串行：远端 applyChanges 的 tombstone 检查与实际落库
    // 是一个业务事务，不能被同仓库的 remove/patch 穿插。
    var repositoryQueue = Promise.resolve();

    function serializeRepository(work) {
      var result = repositoryQueue.then(work);
      repositoryQueue = result.catch(function () {});
      return result;
    }

    function afterRepositoryWrites(work) {
      return repositoryQueue.then(work);
    }

    // 所有需要在渲染前取得便签身份的调用方都从这里取号；
    // create() 也复用同一个入口，避免宿主复制随机 ID 算法。
    function newNoteId() {
      return generatedNoteId(idFactory);
    }

    function rawGet(documentId, noteId) {
      return store.get(
        COLLECTION,
        storageIdFor(documentId, noteId),
        { includeDeleted: true }
      );
    }
    function create(input, operationOptions) {
      input = input || {};
      operationOptions = operationOptions || {};
      var documentId;
      var noteId;
      var explicitNoteId;
      var anchor;
      var fields;
      var mutationId;
      try {
        documentId = documentIdOf(input.documentId);
        explicitNoteId = input.noteId != null || input.id != null;
        noteId = explicitNoteId
          ? noteIdOf(input.noteId != null ? input.noteId : input.id)
          : newNoteId();
        if (!CLIENT_ID_RE.test(noteId)) {
          throw new NoteError(
            '新建便签的客户端编号必须是 c_ 加 32 位十六进制',
            'BW_NOTE_IDENTITY'
          );
        }
        anchor = normalizeAnchor(input.anchor, documentId);
        fields = collectFields(input, 'create');
        mutationId = mutationIdOf(operationOptions);
      } catch (error) {
        return Promise.reject(error);
      }
      var signature = operationSignature('create', true, anchor, fields);
      var meta = emptyMeta();
      meta.fieldRevs.anchor = 1;
      Object.keys(fields).forEach(function (field) {
        meta.fieldRevs['fields.' + field] = 1;
      });
      rememberMutation(meta, mutationId, 'create', signature, 1);
      var value = noteValue(documentId, noteId, anchor, fields, meta);
      var storageId = storageIdFor(documentId, noteId);

      function acceptResult(record) {
        if (!record || !record.value) {
          throw conflict(documentId, noteId, 'mutation-reused', [], 0, null);
        }
        var resultDocumentId = String(record.value.documentId || '');
        var resultNoteId = String(record.value.noteId || '');
        if (
          resultDocumentId !== documentId ||
          (explicitNoteId && resultNoteId !== noteId)
        ) {
          throw conflict(documentId, noteId, 'mutation-reused', [], 0, record.rev);
        }
        return rawGet(resultDocumentId, resultNoteId).then(function (latest) {
          if (!latest) {
            throw conflict(documentId, noteId, 'mutation-reused', [], 0, null);
          }
          assertStoredRecord(latest, resultDocumentId, resultNoteId);
          if (latest.deleted) {
            throw conflict(
              resultDocumentId,
              resultNoteId,
              'deleted',
              [],
              0,
              latest.rev
            );
          }
          if (mutationId) {
            var found = findMutation(normalizeMeta(latest.value._meta), mutationId);
            if (!found || found.operation !== 'create' || found.signature !== signature) {
              throw conflict(
                resultDocumentId,
                resultNoteId,
                'mutation-reused',
                [],
                0,
                latest.rev
              );
            }
          }
          return projectRecord(latest, resultDocumentId, resultNoteId);
        });
      }

      return store.put(COLLECTION, value, {
        id: storageId,
        ifRev: 0,
        mutationId: mutationId || undefined
      }).then(acceptResult).catch(function (error) {
        if (!dataConflict(error)) throw error;
        return rawGet(documentId, noteId).then(function (current) {
          if (!current) throw conflict(documentId, noteId, 'exists', [], 0, 0);
          if (mutationId && !current.deleted) {
            var found = findMutation(normalizeMeta(current.value._meta), mutationId);
            if (found && found.operation === 'create' && found.signature === signature) {
              return projectRecord(current, documentId, noteId);
            }
          }
          throw conflict(
            documentId,
            noteId,
            current.deleted ? 'deleted' : 'exists',
            [],
            0,
            current.rev
          );
        });
      });
    }

    function get(documentId, noteId, query) {
      try {
        documentId = documentIdOf(documentId);
        noteId = noteIdOf(noteId);
      } catch (error) {
        return Promise.reject(error);
      }
      query = query || {};
      return rawGet(documentId, noteId).then(function (record) {
        if (!record || (record.deleted && query.includeDeleted !== true)) return null;
        return projectRecord(record, documentId, noteId);
      });
    }

    function list(documentId, query) {
      try { documentId = documentIdOf(documentId); }
      catch (error) { return Promise.reject(error); }
      query = query || {};
      return store.list(COLLECTION, {
        documentId: documentId,
        includeDeleted: query.includeDeleted === true,
        offset: query.offset,
        limit: query.limit
      }).then(function (records) {
        return (records || []).map(function (record) {
          var value = record && record.value || {};
          return projectRecord(record, documentId, noteIdOf(value.noteId));
        });
      });
    }

    function patch(documentId, noteId, changes, operationOptions) {
      operationOptions = operationOptions || {};
      var expected;
      var mutationId;
      var fields;
      var anchorChanged;
      var anchor;
      try {
        documentId = documentIdOf(documentId);
        noteId = noteIdOf(noteId);
        expected = expectedRevision(operationOptions, true);
        mutationId = mutationIdOf(operationOptions);
        if (!plain(changes)) {
          throw new NoteError('patch 必须是普通对象', 'BW_NOTE_INPUT');
        }
        [
          'documentId', 'noteId', 'id', 'rev', 'deleted',
          'updatedAt', 'updatedBy', 'schema', 'contract', '_meta'
        ].forEach(function (field) {
          if (own(changes, field)) {
            throw new NoteError(
              'patch 不能改写身份或版本字段：' + field,
              'BW_NOTE_FIELD'
            );
          }
        });
        fields = collectFields(changes, 'patch');
        anchorChanged = own(changes, 'anchor');
        anchor = anchorChanged ? normalizeAnchor(changes.anchor, documentId) : null;
        if (!anchorChanged && !Object.keys(fields).length) {
          throw new NoteError('patch 不能为空', 'BW_NOTE_INPUT');
        }
      } catch (error) {
        return Promise.reject(error);
      }
      var changedKeys = Object.keys(fields).sort().map(function (field) {
        return 'fields.' + field;
      });
      if (anchorChanged) changedKeys.unshift('anchor');
      var signature = operationSignature('patch', anchorChanged, anchor, fields);

      function attempt(number) {
        return rawGet(documentId, noteId).then(function (current) {
          if (!current) throw notFound(documentId, noteId);
          assertStoredRecord(current, documentId, noteId);
          if (current.deleted) {
            throw conflict(documentId, noteId, 'deleted', changedKeys, expected, current.rev);
          }
          var meta = normalizeMeta(current.value._meta);
          var prior = findMutation(meta, mutationId);
          if (prior) {
            if (prior.operation !== 'patch' || prior.signature !== signature) {
              throw conflict(
                documentId,
                noteId,
                'mutation-reused',
                changedKeys,
                expected,
                current.rev
              );
            }
            return projectRecord(current, documentId, noteId);
          }
          if (expected > current.rev) {
            throw conflict(
              documentId,
              noteId,
              'future-revision',
              changedKeys,
              expected,
              current.rev
            );
          }
          var conflictingFields = [];
          if (expected < current.rev) {
            changedKeys.forEach(function (field) {
              var lastChanged = Number(meta.fieldRevs[field]);
              var neverWritten = field.indexOf('fields.') === 0 &&
                !own(current.value.fields, field.slice('fields.'.length));
              if (
                (!Number.isSafeInteger(lastChanged) && !neverWritten) ||
                (Number.isSafeInteger(lastChanged) && lastChanged > expected)
              ) {
                conflictingFields.push(field);
              }
            });
          }
          if (conflictingFields.length) {
            throw conflict(
              documentId,
              noteId,
              'same-field',
              conflictingFields,
              expected,
              current.rev
            );
          }

          var nextFields = cloneJSON(current.value.fields, 'fields');
          Object.keys(fields).forEach(function (field) {
            nextFields[field] = cloneJSON(fields[field], 'fields.' + field);
          });
          var nextAnchor = anchorChanged
            ? normalizeAnchor(anchor, documentId)
            : normalizeAnchor(current.value.anchor, documentId);
          var targetRevision = current.rev + 1;
          changedKeys.forEach(function (field) {
            meta.fieldRevs[field] = targetRevision;
          });
          rememberMutation(meta, mutationId, 'patch', signature, targetRevision);
          var nextValue = noteValue(
            documentId,
            noteId,
            nextAnchor,
            nextFields,
            meta
          );
          return store.put(COLLECTION, nextValue, {
            id: current.id,
            ifRev: current.rev,
            mutationId: mutationId || undefined
          }).then(function () {
            return rawGet(documentId, noteId);
          }).then(function (latest) {
            if (!latest) throw notFound(documentId, noteId);
            assertStoredRecord(latest, documentId, noteId);
            if (latest.deleted) {
              throw conflict(
                documentId,
                noteId,
                'deleted',
                changedKeys,
                expected,
                latest.rev
              );
            }
            if (mutationId) {
              var applied = findMutation(normalizeMeta(latest.value._meta), mutationId);
              if (!applied || applied.operation !== 'patch' || applied.signature !== signature) {
                throw conflict(
                  documentId,
                  noteId,
                  'mutation-reused',
                  changedKeys,
                  expected,
                  latest.rev
                );
              }
            }
            return projectRecord(latest, documentId, noteId);
          }).catch(function (error) {
            if (dataConflict(error) && number < MAX_REBASE_ATTEMPTS) {
              return attempt(number + 1);
            }
            if (dataConflict(error)) {
              throw conflict(
                documentId,
                noteId,
                'revision-race',
                changedKeys,
                expected,
                error.details && error.details.actualRev
              );
            }
            throw error;
          });
        });
      }
      return attempt(0);
    }

    function remove(documentId, noteId, operationOptions) {
      operationOptions = operationOptions || {};
      var expected;
      var mutationId;
      try {
        documentId = documentIdOf(documentId);
        noteId = noteIdOf(noteId);
        expected = expectedRevision(operationOptions, true);
        mutationId = mutationIdOf(operationOptions);
      } catch (error) {
        return Promise.reject(error);
      }
      return rawGet(documentId, noteId).then(function (current) {
        if (!current) throw notFound(documentId, noteId);
        assertStoredRecord(current, documentId, noteId);
        return store.remove(COLLECTION, current.id, {
          ifRev: expected,
          mutationId: mutationId || undefined
        });
      }).then(function (record) {
        if (
          !record ||
          record.id !== storageIdFor(documentId, noteId) ||
          record.deleted !== true
        ) {
          throw conflict(
            documentId,
            noteId,
            'mutation-reused',
            [],
            expected,
            record && record.rev
          );
        }
        return rawGet(documentId, noteId).then(function (latest) {
          if (!latest) {
            throw conflict(
              documentId,
              noteId,
              'mutation-reused',
              [],
              expected,
              null
            );
          }
          assertStoredRecord(latest, documentId, noteId);
          if (latest.deleted !== true) {
            throw conflict(
              documentId,
              noteId,
              'mutation-reused',
              [],
              expected,
              latest.rev
            );
          }
          // DataStore 的 mutationId 重放返回首次操作时的快照。期间若已
          // 合并更高 revision 的 tombstone，调用方必须看到当前 tombstone，
          // 不能被幂等缓存拉回旧 revision。
          return projectRecord(latest, documentId, noteId);
        });
      }).catch(function (error) {
        if (dataConflict(error)) {
          throw conflict(
            documentId,
            noteId,
            'revision',
            [],
            expected,
            error.details && error.details.actualRev
          );
        }
        throw error;
      });
    }

    // document-notes 的所有远端合并都必须经过这里。DataStore 的通用规则允许
    // 更高 rev 覆盖较低 rev；便签没有“恢复已删除记录”操作，因此本层额外规定
    // tombstone 永远支配活动记录，避免任意更高 rev 的旧设备副本将其复活。
    function applyChanges(incoming, applyOptions) {
      var changes;
      try {
        if (!Array.isArray(incoming)) {
          throw new NoteError(
            'applyChanges changes 必须是数组',
            'BW_NOTE_CHANGES'
          );
        }
        changes = cloneJSON(incoming, 'changes');
      } catch (error) {
        return Promise.reject(error);
      }
      var allowed = [];
      var tombstoneConflicts = [];
      var sequence = Promise.resolve();

      changes.forEach(function (change, index) {
        sequence = sequence.then(function () {
          if (!plain(change) || !plain(change.record)) {
            throw new NoteError(
              'document-notes change/record 无效',
              'BW_NOTE_CHANGES',
              { index: index }
            );
          }
          var declaredCollection = String(change.collection || '').trim();
          var recordCollection = String(change.record.collection || '').trim();
          if (
            (declaredCollection && declaredCollection !== COLLECTION) ||
            (recordCollection && recordCollection !== COLLECTION) ||
            (declaredCollection && recordCollection &&
             declaredCollection !== recordCollection)
          ) {
            throw new NoteError(
              'applyChanges 只能接收 document-notes',
              'BW_NOTE_COLLECTION',
              {
                index: index,
                collection: declaredCollection,
                recordCollection: recordCollection
              }
            );
          }
          change.collection = COLLECTION;
          change.record.collection = COLLECTION;
          var value = change.record.value;
          if (!plain(value)) {
            throw new NoteError(
              '远端便签记录缺少 value',
              'BW_NOTE_CORRUPT',
              { index: index }
            );
          }
          var documentId = documentIdOf(value.documentId);
          var noteId = noteIdOf(value.noteId);
          assertStoredRecord(change.record, documentId, noteId);

          return rawGet(documentId, noteId).then(function (current) {
            if (current) assertStoredRecord(current, documentId, noteId);
            normalizeIncomingRecordMeta(change.record, current);
            if (current && current.deleted === true &&
                change.record.deleted !== true) {
              tombstoneConflicts.push({
                collection: COLLECTION,
                id: change.record.id,
                local: cloneJSON(current, 'local'),
                incoming: cloneJSON(change.record, 'incoming'),
                reason: 'tombstone-dominates'
              });
              return;
            }
            allowed.push(change);
          });
        });
      });

      return sequence.then(function () {
        if (!allowed.length) {
          return {
            applied: [],
            conflicts: [],
            skipped: []
          };
        }
        var guardedOptions = Object.assign({}, applyOptions || {}, {
          tombstoneDominates: true
        });
        if (store.contract === 'storage-router/1') {
          return store.applyChanges('document', allowed, guardedOptions);
        }
        return store.applyChanges(allowed, guardedOptions);
      }).then(function (result) {
        result = result || {};
        return {
          applied: Array.isArray(result.applied) ? result.applied : [],
          conflicts: tombstoneConflicts.concat(
            Array.isArray(result.conflicts) ? result.conflicts : []
          ),
          skipped: Array.isArray(result.skipped) ? result.skipped : []
        };
      });
    }

    function subscribe(documentId, listener) {
      documentId = documentIdOf(documentId);
      if (typeof listener !== 'function') {
        throw new NoteError(
          'subscribe listener 必须是函数',
          'BW_NOTE_LISTENER'
        );
      }
      var onChange = function (change) {
        var record = change && change.record;
        if (!record || !record.value || record.value.documentId !== documentId) return;
        var event = {
          contract: CONTRACT,
          operation: record.deleted ? 'remove' : 'put',
          mutationId: String(change.mutationId || ''),
          cursor: Number(change.cursor) || 0,
          remote: change.remote === true,
          note: null,
          error: null
        };
        try {
          event.note = projectRecord(
            record,
            documentId,
            noteIdOf(record.value.noteId)
          );
        } catch (error) {
          event.error = {
            code: String(error && error.code || 'BW_NOTE_CORRUPT'),
            message: String(error && error.message || error)
          };
        }
        listener(event);
      };
      if (store.contract === 'storage-router/1') {
        return store.subscribe(COLLECTION, onChange);
      }
      return store.subscribe({ collection: COLLECTION }, onChange);
    }

    return Object.freeze({
      contract: CONTRACT,
      collection: COLLECTION,
      newNoteId: newNoteId,
      list: function (documentId, query) {
        return afterRepositoryWrites(function () {
          return list(documentId, query);
        });
      },
      get: function (documentId, noteId, query) {
        return afterRepositoryWrites(function () {
          return get(documentId, noteId, query);
        });
      },
      create: function (input, operationOptions) {
        return serializeRepository(function () {
          return create(input, operationOptions);
        });
      },
      patch: function (documentId, noteId, changes, operationOptions) {
        return serializeRepository(function () {
          return patch(documentId, noteId, changes, operationOptions);
        });
      },
      remove: function (documentId, noteId, operationOptions) {
        return serializeRepository(function () {
          return remove(documentId, noteId, operationOptions);
        });
      },
      applyChanges: function (incoming, applyOptions) {
        return serializeRepository(function () {
          return applyChanges(incoming, applyOptions);
        });
      },
      subscribe: subscribe,
      storageIdFor: storageIdFor
    });
  }

  return Object.freeze({
    CONTRACT: CONTRACT,
    COLLECTION: COLLECTION,
    RECORD_SCHEMA: RECORD_SCHEMA,
    NoteError: NoteError,
    NoteConflictError: NoteConflictError,
    storageIdFor: storageIdFor,
    newNoteId: function () {
      return generatedNoteId();
    },
    createDocumentNoteRepository: createDocumentNoteRepository
  });
});
