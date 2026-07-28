/* data-store.js — 本地优先、稳定编号、带变更日志的数据层。
 *
 * 该层不联网。PWA fallback 与扩展 Vault 使用同一套记录/冲突/墓碑语义，
 * 只替换最底层 backend。服务器同步由 SyncGateway 单独负责。
 */
(function (root, factory) {
  var api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.BWReaderRuntime = root.BWReaderRuntime || {};
  root.BWReaderRuntime.dataStore = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function (root) {
  'use strict';

  var CONTRACT = 'data-store/1';
  var SCHEMA = 1;
  var CAUSAL_CONTRACT = 'record-parent-state/1';
  var CAUSAL_MIGRATION_CONTRACT = 'sync-v2-causal-migration/1';
  var MAX_CAUSAL_PARENT_BYTES = 512 * 1024;

  function nowDefault() { return Date.now(); }
  function own(obj, key) { return Object.prototype.hasOwnProperty.call(obj || {}, key); }
  function isPlainObject(value) {
    if (!value || Object.prototype.toString.call(value) !== '[object Object]') return false;
    var prototype = Object.getPrototypeOf(value);
    return prototype === Object.prototype || prototype === null;
  }
  function isBinary(value) {
    if (!value || typeof value !== 'object') return false;
    var tag = Object.prototype.toString.call(value);
    if (tag === '[object Blob]' || tag === '[object ArrayBuffer]' ||
        tag === '[object SharedArrayBuffer]' || tag === '[object DataView]') return true;
    if (typeof root.ArrayBuffer !== 'undefined') {
      if (value instanceof root.ArrayBuffer) return true;
      if (typeof root.ArrayBuffer.isView === 'function' && root.ArrayBuffer.isView(value)) return true;
    }
    return false;
  }
  function assertDataValue(value, path, seen) {
    path = path || 'value';
    if (value === null) return;
    var type = typeof value;
    if (type === 'string' || type === 'boolean') return;
    if (type === 'number') {
      if (!Number.isFinite(value)) {
        throw new DataStoreError(path + ' 含有非有限数字', 'BW_DATA_INVALID');
      }
      return;
    }
    if (isBinary(value)) {
      throw new DataStoreError(
        path + ' 不允许 Blob、ArrayBuffer 或 TypedArray；二进制内容必须存到独立资源层',
        'BW_DATA_BINARY'
      );
    }
    if (type !== 'object') {
      throw new DataStoreError(path + ' 不是 JSON 值', 'BW_DATA_INVALID');
    }
    if (!Array.isArray(value) && !isPlainObject(value)) {
      throw new DataStoreError(path + ' 必须是普通 JSON 对象', 'BW_DATA_INVALID');
    }

    seen = seen || new Set();
    if (seen.has(value)) {
      throw new DataStoreError(path + ' 含有循环引用', 'BW_DATA_INVALID');
    }
    seen.add(value);

    var symbols = typeof Object.getOwnPropertySymbols === 'function'
      ? Object.getOwnPropertySymbols(value)
      : [];
    if (symbols.length) {
      seen.delete(value);
      throw new DataStoreError(path + ' 含有 JSON 无法保存的 Symbol 属性', 'BW_DATA_INVALID');
    }

    var names = Object.getOwnPropertyNames(value);
    if (Array.isArray(value)) {
      for (var i = 0; i < value.length; i += 1) {
        if (!own(value, i)) {
          seen.delete(value);
          throw new DataStoreError(path + '[' + i + '] 不能为空洞', 'BW_DATA_INVALID');
        }
        var arrayDescriptor = Object.getOwnPropertyDescriptor(value, String(i));
        if (!arrayDescriptor || !arrayDescriptor.enumerable ||
            own(arrayDescriptor, 'get') || own(arrayDescriptor, 'set')) {
          seen.delete(value);
          throw new DataStoreError(path + '[' + i + '] 必须是可枚举的数据属性', 'BW_DATA_INVALID');
        }
        assertDataValue(value[i], path + '[' + i + ']', seen);
      }
      for (var n = 0; n < names.length; n += 1) {
        var arrayKey = names[n];
        if (arrayKey === 'length') continue;
        var numericKey = String(Number(arrayKey)) === arrayKey &&
          Number(arrayKey) >= 0 && Number(arrayKey) < value.length &&
          Math.floor(Number(arrayKey)) === Number(arrayKey);
        if (!numericKey) {
          seen.delete(value);
          throw new DataStoreError(path + ' 含有 JSON 会忽略的数组属性：' + arrayKey, 'BW_DATA_INVALID');
        }
      }
    } else {
      for (var j = 0; j < names.length; j += 1) {
        var key = names[j];
        var descriptor = Object.getOwnPropertyDescriptor(value, key);
        if (!descriptor || !descriptor.enumerable ||
            own(descriptor, 'get') || own(descriptor, 'set')) {
          seen.delete(value);
          throw new DataStoreError(path + '.' + key + ' 必须是可枚举的数据属性', 'BW_DATA_INVALID');
        }
        assertDataValue(value[key], path + '.' + key, seen);
      }
    }
    seen.delete(value);
  }
  function cloneDataValue(value, path) {
    assertDataValue(value, path || 'value');
    return JSON.parse(JSON.stringify(value));
  }
  function clone(value, path) {
    if (value === null) return null;
    return cloneDataValue(value, path);
  }
  function canonicalDataValue(value) {
    if (value === null || typeof value !== 'object') return JSON.stringify(value);
    if (Array.isArray(value)) {
      return '[' + value.map(canonicalDataValue).join(',') + ']';
    }
    return '{' + Object.keys(value).sort().map(function (key) {
      return JSON.stringify(key) + ':' + canonicalDataValue(value[key]);
    }).join(',') + '}';
  }
  function isRecordEnvelope(value) {
    return isPlainObject(value) &&
      own(value, 'collection') &&
      own(value, 'id') &&
      typeof value.deleted === 'boolean' &&
      own(value, 'value');
  }
  function sameDataValue(a, b) {
    var leftRecord = isRecordEnvelope(a);
    var rightRecord = isRecordEnvelope(b);
    if (leftRecord || rightRecord) {
      if (!leftRecord || !rightRecord) return false;
      if (
        a.collection !== b.collection ||
        a.id !== b.id ||
        a.deleted !== b.deleted
      ) {
        return false;
      }
      // Revision/timestamp/device are transport metadata. A tombstone's old
      // payload is retained only for local recovery and is not business state.
      if (a.deleted === true) return true;
      return canonicalDataValue(a.value) === canonicalDataValue(b.value);
    }
    return canonicalDataValue(a) === canonicalDataValue(b);
  }
  function utf8ByteLength(value) {
    var text = String(value == null ? '' : value);
    if (typeof root.TextEncoder === 'function') {
      return new root.TextEncoder().encode(text).byteLength;
    }
    return unescape(encodeURIComponent(text)).length;
  }
  function businessState(record) {
    if (!record) return null;
    if (record.deleted === true) return { deleted: true };
    return {
      deleted: false,
      value: cloneDataValue(record.value, 'record.value')
    };
  }
  function causalProofForParent(record) {
    var proof = {
      contract: CAUSAL_CONTRACT,
      parent: businessState(record)
    };
    if (utf8ByteLength(JSON.stringify(proof.parent)) > MAX_CAUSAL_PARENT_BYTES) {
      throw new DataStoreError(
        '父业务状态超过因果证明大小上限',
        'BW_DATA_CAUSAL_TOO_LARGE',
        { maximumBytes: MAX_CAUSAL_PARENT_BYTES }
      );
    }
    return proof;
  }
  function inspectCausalProof(record) {
    if (!isPlainObject(record) || !own(record, 'causal')) {
      return { valid: false, reason: 'causal-proof-missing', parent: null };
    }
    var proof = record.causal;
    if (
      !isPlainObject(proof) ||
      proof.contract !== CAUSAL_CONTRACT ||
      Object.keys(proof).sort().join('\u0000') !== 'contract\u0000parent'
    ) {
      return { valid: false, reason: 'causal-proof-invalid', parent: null };
    }
    if (proof.parent === null) {
      return { valid: true, reason: '', parent: null };
    }
    var parent = proof.parent;
    if (!isPlainObject(parent) || typeof parent.deleted !== 'boolean') {
      return { valid: false, reason: 'causal-proof-invalid', parent: null };
    }
    var keys = Object.keys(parent).sort().join('\u0000');
    if (
      parent.deleted === true && keys !== 'deleted' ||
      parent.deleted === false && keys !== 'deleted\u0000value'
    ) {
      return { valid: false, reason: 'causal-proof-invalid', parent: null };
    }
    try {
      if (parent.deleted === false) {
        assertDataValue(parent.value, 'record.causal.parent.value');
      }
      if (utf8ByteLength(JSON.stringify(parent)) > MAX_CAUSAL_PARENT_BYTES) {
        return { valid: false, reason: 'causal-proof-too-large', parent: null };
      }
    } catch (_) {
      return { valid: false, reason: 'causal-proof-invalid', parent: null };
    }
    return {
      valid: true,
      reason: '',
      parent: cloneDataValue(parent, 'record.causal.parent')
    };
  }
  function causalParentMatches(current, proof) {
    if (!proof || proof.valid !== true) return false;
    if (proof.parent === null) return current === null;
    if (!current || proof.parent.deleted !== (current.deleted === true)) {
      return false;
    }
    if (proof.parent.deleted === true) return true;
    return canonicalDataValue(proof.parent.value) ===
      canonicalDataValue(current.value);
  }
  function sameRecordWithoutCausal(left, right) {
    if (!left || !right) return left === right;
    left = cloneDataValue(left, 'migration.left');
    right = cloneDataValue(right, 'migration.right');
    delete left.causal;
    delete right.causal;
    return canonicalDataValue(left) === canonicalDataValue(right);
  }
  function migrationCursor(value, label) {
    value = Number(value);
    if (!Number.isSafeInteger(value) || value < 0) {
      throw new DataStoreError(
        (label || 'cursor') + ' 必须是非负安全整数',
        'BW_DATA_CAUSAL_MIGRATION_INVALID'
      );
    }
    return value;
  }
  function migrationKey(collection, id) {
    return String(collection) + '\u0000' + String(id);
  }
  function migrationKeyParts(key) {
    var divider = String(key).indexOf('\u0000');
    return [
      String(key).slice(0, divider),
      String(key).slice(divider + 1)
    ];
  }
  function migrationRevisionFollows(parent, record) {
    if (parent && sameDataValue(parent, record)) return true;
    var parentRevision = parent ? Number(parent.rev) : 0;
    return record.rev === parentRevision + 1;
  }
  function planLegacyCausalMigration(options) {
    options = options || {};
    if (
      !isPlainObject(options) ||
      options.contract !== CAUSAL_MIGRATION_CONTRACT ||
      (options.mode !== 'inspect' && options.mode !== 'apply')
    ) {
      throw new DataStoreError(
        '旧因果迁移请求无效',
        'BW_DATA_CAUSAL_MIGRATION_INVALID'
      );
    }
    var after = migrationCursor(options.after, 'migration.after');
    var currentCursor = migrationCursor(options.cursor, 'migration.cursor');
    if (after > currentCursor) {
      throw new DataStoreError(
        '旧因果迁移游标超过本地 journal head',
        'BW_DATA_CAUSAL_MIGRATION_GAP',
        { after: after, cursor: currentCursor }
      );
    }
    var journal = cloneDataValue(
      Array.isArray(options.journal) ? options.journal : [],
      'migration.journal'
    );
    var previousCursor = 0;
    journal.forEach(function (change, index) {
      if (!isPlainObject(change)) {
        throw new DataStoreError(
          '旧因果迁移 journal 项无效',
          'BW_DATA_CAUSAL_MIGRATION_INVALID',
          { index: index }
        );
      }
      var changeCursor = migrationCursor(
        change.cursor,
        'migration.journal[' + index + '].cursor'
      );
      if (changeCursor <= previousCursor || changeCursor > currentCursor) {
        throw new DataStoreError(
          '旧因果迁移 journal 游标不连续',
          'BW_DATA_CAUSAL_MIGRATION_GAP',
          { index: index, cursor: changeCursor, previousCursor: previousCursor }
        );
      }
      previousCursor = changeCursor;
    });
    var oldestCursor = journal.length
      ? migrationCursor(journal[0].cursor, 'migration.oldestCursor')
      : currentCursor + 1;
    if (after < Math.max(0, oldestCursor - 1)) {
      throw new DataStoreError(
        '旧因果迁移 journal 已裁剪，无法证明完整父链',
        'BW_DATA_CAUSAL_MIGRATION_GAP',
        { after: after, oldestCursor: oldestCursor, cursor: currentCursor }
      );
    }
    var causalNames = new Set(
      (Array.isArray(options.causalCollections)
        ? options.causalCollections
        : []
      ).map(function (name) { return safeName(name, 'migration.causalCollection'); })
    );
    var baselineComplete = options.baselineComplete === true;
    var baselines = Array.isArray(options.baselines)
      ? cloneDataValue(options.baselines, 'migration.baselines')
      : [];
    if (!baselineComplete && baselines.length) {
      throw new DataStoreError(
        '未声明完整快照时不能提供迁移 baseline',
        'BW_DATA_CAUSAL_MIGRATION_INVALID'
      );
    }
    var heads = new Map();
    if (baselineComplete) {
      baselines.forEach(function (entry, index) {
        if (!isPlainObject(entry) || !isPlainObject(entry.record)) {
          throw new DataStoreError(
            '旧因果迁移 baseline 项无效',
            'BW_DATA_CAUSAL_MIGRATION_INVALID',
            { index: index }
          );
        }
        var normalized = normalizeIncomingRecord(
          entry.record,
          entry.collection,
          'migration.baselines[' + index + '].record'
        );
        if (!causalNames.has(normalized.collection)) {
          throw new DataStoreError(
            '旧因果迁移 baseline 包含未开放 collection',
            'BW_DATA_CAUSAL_MIGRATION_INVALID',
            { collection: normalized.collection }
          );
        }
        var key = migrationKey(normalized.collection, normalized.id);
        if (heads.has(key)) {
          throw new DataStoreError(
            '旧因果迁移 baseline 含重复记录',
            'BW_DATA_CAUSAL_MIGRATION_INVALID',
            { collection: normalized.collection, id: normalized.id }
          );
        }
        heads.set(key, normalized);
      });
    }
    var anchored = new Set();
    var needsBaselineKeys = new Set();
    var patches = [];
    var finalRecords = new Map();
    var examined = 0;
    var missing = 0;
    var verified = 0;
    journal.filter(function (change) {
      return migrationCursor(change.cursor, 'migration.change.cursor') > after;
    }).forEach(function (change, index) {
      var collection = String(change && (
        change.collection ||
        change.record && change.record.collection
      ) || '');
      if (!causalNames.has(collection)) return;
      var normalized = normalizeIncomingChange(change, index);
      var record = normalized.record;
      var key = migrationKey(record.collection, record.id);
      var knownParent = baselineComplete || anchored.has(key);
      var parent = heads.has(key) ? heads.get(key) : null;
      var proof = inspectCausalProof(record);
      examined += 1;
      if (!knownParent) {
        if (proof.valid) {
          anchored.add(key);
          verified += 1;
        } else if (proof.reason === 'causal-proof-missing') {
          needsBaselineKeys.add(key);
          missing += 1;
        } else {
          throw new DataStoreError(
            '旧因果迁移发现非法既有证明',
            'BW_DATA_CAUSAL_MIGRATION_PROOF',
            {
              collection: record.collection,
              id: record.id,
              reason: proof.reason
            }
          );
        }
      } else if (proof.valid) {
        if (!causalParentMatches(parent, proof)) {
          throw new DataStoreError(
            '旧因果迁移发现与父链不一致的既有证明',
            'BW_DATA_CAUSAL_MIGRATION_CONFLICT',
            { collection: record.collection, id: record.id }
          );
        }
        verified += 1;
      } else if (proof.reason === 'causal-proof-missing') {
        missing += 1;
        if (!migrationRevisionFollows(parent, record)) {
          throw new DataStoreError(
            '旧因果迁移无法用 revision 证明父链',
            'BW_DATA_CAUSAL_MIGRATION_UNPROVEN',
            {
              collection: record.collection,
              id: record.id,
              parentRev: parent ? parent.rev : 0,
              incomingRev: record.rev
            }
          );
        }
        record.causal = causalProofForParent(parent);
        var patched = cloneDataValue(change, 'migration.change');
        patched.record = cloneDataValue(record, 'migration.change.record');
        patches.push(patched);
      } else {
        throw new DataStoreError(
          '旧因果迁移发现非法既有证明',
          'BW_DATA_CAUSAL_MIGRATION_PROOF',
          {
            collection: record.collection,
            id: record.id,
            reason: proof.reason
          }
        );
      }
      heads.set(key, record);
      anchored.add(key);
      finalRecords.set(key, record);
    });
    var needsBaseline = needsBaselineKeys.size > 0;
    if (options.mode === 'apply' && needsBaseline && !baselineComplete) {
      throw new DataStoreError(
        '旧因果迁移需要与 checkpoint 对齐的完整服务端 baseline',
        'BW_DATA_CAUSAL_MIGRATION_BASELINE_REQUIRED',
        { keys: Array.from(needsBaselineKeys).sort() }
      );
    }
    return {
      contract: CAUSAL_MIGRATION_CONTRACT,
      after: after,
      throughCursor: currentCursor,
      examined: examined,
      missing: missing,
      migrated: options.mode === 'apply' ? patches.length : 0,
      verified: verified,
      needsBaseline: needsBaseline,
      patches: options.mode === 'apply' ? patches : [],
      finalRecords: options.mode === 'apply' ? finalRecords : new Map()
    };
  }
  function safeName(value, label) {
    var out = String(value || '').trim();
    if (!out) throw new DataStoreError(label + ' 不能为空', 'BW_DATA_INVALID');
    return out;
  }
  function normalizeExpectedRev(value, label) {
    if (value == null) return null;
    var revision = Number(value);
    if (!Number.isSafeInteger(revision) || revision < 0) {
      throw new DataStoreError((label || 'ifRev') + ' 必须是非负安全整数', 'BW_DATA_INVALID');
    }
    return revision;
  }
  function normalizeOperationOptions(options) {
    options = options || {};
    var output = {};
    if (options.id) output.id = options.id;
    if (options.prefix) output.prefix = options.prefix;
    if (options.mutationId) output.mutationId = String(options.mutationId);
    if (options.ifRev != null) output.ifRev = normalizeExpectedRev(options.ifRev, 'ifRev');
    if (Array.isArray(options.identityFields)) {
      output.identityFields = options.identityFields.map(function (field) {
        return safeName(field, 'identityField');
      });
    }
    return output;
  }
  function normalizePutValue(value, path) {
    if (!isPlainObject(value)) {
      throw new DataStoreError('put value 必须是普通 JSON 对象', 'BW_DATA_INVALID');
    }
    return cloneDataValue(value, path || 'put.value');
  }
  function prepareBatch(mutations) {
    if (!Array.isArray(mutations)) {
      throw new DataStoreError('batch 必须是数组', 'BW_DATA_INVALID');
    }
    return mutations.map(function (rawMutation, index) {
      if (!isPlainObject(rawMutation)) {
        throw new DataStoreError('batch[' + index + '] 必须是对象', 'BW_DATA_INVALID');
      }
      var operation = rawMutation.operation || rawMutation.op || 'put';
      if (operation !== 'put' && operation !== 'remove') {
        throw new DataStoreError(
          'batch[' + index + '].operation 只允许 put 或 remove',
          'BW_DATA_INVALID',
          { operation: String(operation) }
        );
      }
      var operationOptions = normalizeOperationOptions(rawMutation.options || rawMutation);
      var collection = safeName(rawMutation.collection, 'batch[' + index + '].collection');
      if (operation === 'remove') {
        return {
          operation: 'remove',
          collection: collection,
          id: safeName(rawMutation.id, 'batch[' + index + '].id'),
          options: operationOptions
        };
      }
      var rawValue = own(rawMutation, 'value')
        ? rawMutation.value
        : (own(rawMutation, 'record') ? rawMutation.record : {});
      return {
        operation: 'put',
        collection: collection,
        value: normalizePutValue(rawValue, 'batch[' + index + '].value'),
        options: operationOptions
      };
    });
  }
  function normalizeIncomingRecord(record, collection, path) {
    path = path || 'incoming.record';
    if (!isPlainObject(record)) {
      throw new DataStoreError(path + ' 必须是普通 JSON 对象', 'BW_DATA_INVALID');
    }
    var output = cloneDataValue(record, path);
    if (!own(output, 'schema') || output.schema !== SCHEMA) {
      throw new DataStoreError(
        path + '.schema 必须为 ' + SCHEMA,
        'BW_DATA_SCHEMA',
        { schema: own(output, 'schema') ? output.schema : null }
      );
    }
    if (!own(output, 'collection') || typeof output.collection !== 'string') {
      throw new DataStoreError(path + '.collection 必须是字符串', 'BW_DATA_INVALID');
    }
    var recordCollection = safeName(output.collection, path + '.collection');
    var declaredCollection = collection == null
      ? recordCollection
      : safeName(collection, path + '.declaredCollection');
    if (recordCollection !== declaredCollection) {
      throw new DataStoreError(
        path + '.collection 与 change.collection 不一致',
        'BW_DATA_INVALID',
        { collection: declaredCollection, recordCollection: recordCollection }
      );
    }
    output.collection = recordCollection;
    if (!own(output, 'id') || typeof output.id !== 'string') {
      throw new DataStoreError(path + '.id 必须是字符串', 'BW_DATA_INVALID');
    }
    output.id = safeName(output.id, path + '.id');
    if (!own(output, 'rev') ||
        !Number.isSafeInteger(output.rev) ||
        output.rev < 1) {
      throw new DataStoreError(
        path + '.rev 必须是大于 0 的安全整数',
        'BW_DATA_INVALID'
      );
    }
    if (!own(output, 'updatedAt') ||
        typeof output.updatedAt !== 'number' ||
        !Number.isFinite(output.updatedAt) ||
        output.updatedAt < 0) {
      throw new DataStoreError(
        path + '.updatedAt 必须是非负有限数字',
        'BW_DATA_INVALID'
      );
    }
    if (!own(output, 'updatedBy') ||
        typeof output.updatedBy !== 'string' ||
        output.updatedBy !== output.updatedBy.trim() ||
        !output.updatedBy ||
        output.updatedBy.length > 300 ||
        /[\u0000-\u001f\u007f]/.test(output.updatedBy)) {
      throw new DataStoreError(
        path + '.updatedBy 必须是有效的非空字符串',
        'BW_DATA_INVALID'
      );
    }
    if (!own(output, 'deleted') || typeof output.deleted !== 'boolean') {
      throw new DataStoreError(path + '.deleted 必须是布尔值', 'BW_DATA_INVALID');
    }
    if (!own(output, 'value')) {
      throw new DataStoreError(path + '.value 不能为空缺失', 'BW_DATA_INVALID');
    }
    // cloneDataValue 已验证 value 与所有未知扩展字段都是可持久化 JSON。
    // 未知字段有意保留，以允许 recordSchema 后续做向前兼容扩展。
    return output;
  }
  function normalizeIncomingChange(change, index) {
    var path = 'incoming[' + Math.max(0, Number(index) || 0) + ']';
    if (!isPlainObject(change)) {
      throw new DataStoreError(path + ' 必须是普通 JSON 对象', 'BW_DATA_INVALID');
    }
    if (!isPlainObject(change.record)) {
      throw new DataStoreError(path + '.record 必须是普通 JSON 对象', 'BW_DATA_INVALID');
    }
    var declaredCollection = own(change, 'collection')
      ? change.collection
      : null;
    if (declaredCollection != null && typeof declaredCollection !== 'string') {
      throw new DataStoreError(path + '.collection 必须是字符串', 'BW_DATA_INVALID');
    }
    var record = normalizeIncomingRecord(
      change.record,
      declaredCollection,
      path + '.record'
    );
    var operation = record.deleted ? 'remove' : 'put';
    if (own(change, 'operation')) {
      if (change.operation !== 'put' && change.operation !== 'remove') {
        throw new DataStoreError(
          path + '.operation 只允许 put 或 remove',
          'BW_DATA_INVALID',
          { operation: String(change.operation) }
        );
      }
      if (change.operation !== operation) {
        throw new DataStoreError(
          path + '.operation 与 record.deleted 不一致',
          'BW_DATA_INVALID',
          { operation: change.operation, deleted: record.deleted }
        );
      }
      operation = change.operation;
    }
    return {
      change: change,
      collection: record.collection,
      record: record,
      mutationId: String(change.mutationId || ''),
      operation: operation
    };
  }
  function emptyState() {
    return { schema: SCHEMA, cursor: 0, collections: {}, journal: [], mutations: {} };
  }
  function normalizeState(input) {
    input = isPlainObject(input) ? clone(input, 'state') : emptyState();
    if (input.schema !== SCHEMA) throw new DataStoreError('不支持的数据版本：' + input.schema, 'BW_DATA_SCHEMA');
    input.cursor = Math.max(0, Number(input.cursor) || 0);
    if (!isPlainObject(input.collections)) input.collections = {};
    if (!Array.isArray(input.journal)) input.journal = [];
    if (!isPlainObject(input.mutations)) input.mutations = {};
    return input;
  }

  function DataStoreError(message, code, details) {
    this.name = 'DataStoreError';
    this.code = code || 'BW_DATA_ERROR';
    this.message = message;
    this.details = details || null;
    if (Error.captureStackTrace) Error.captureStackTrace(this, DataStoreError);
  }
  DataStoreError.prototype = Object.create(Error.prototype);
  DataStoreError.prototype.constructor = DataStoreError;

  function ConflictError(collection, id, expected, actual) {
    DataStoreError.call(this, '记录版本冲突：' + collection + '/' + id, 'BW_DATA_CONFLICT', {
      collection: collection,
      id: id,
      expectedRev: expected,
      actualRev: actual
    });
    this.name = 'DataStoreConflictError';
  }
  ConflictError.prototype = Object.create(DataStoreError.prototype);
  ConflictError.prototype.constructor = ConflictError;

  function createMemoryBackend(initial) {
    var state = normalizeState(initial || emptyState());
    return {
      kind: 'memory',
      load: function () { return Promise.resolve(clone(state)); },
      save: function (next) { state = normalizeState(next); return Promise.resolve(); },
      inspect: function () { return clone(state); }
    };
  }

  function createWebStorageBackend(storage, key) {
    if (!storage || typeof storage.getItem !== 'function' || typeof storage.setItem !== 'function') {
      throw new DataStoreError('WebStorage backend 缺少 getItem/setItem', 'BW_DATA_BACKEND');
    }
    key = String(key || 'bw-reader-data-v1');
    return {
      kind: 'web-storage',
      key: key,
      load: function () {
        var raw = storage.getItem(key);
        if (!raw) return Promise.resolve(emptyState());
        try { return Promise.resolve(normalizeState(JSON.parse(raw))); }
        catch (error) {
          return Promise.reject(new DataStoreError('本地数据无法解析', 'BW_DATA_CORRUPT', {
            key: key,
            cause: String(error && error.message || error)
          }));
        }
      },
      save: function (state) {
        storage.setItem(key, JSON.stringify(normalizeState(state)));
        return Promise.resolve();
      }
    };
  }

  function defaultIdFactory(prefix) {
    var id = '';
    try {
      if (root.crypto && root.crypto.randomUUID) id = root.crypto.randomUUID();
    } catch (_) {}
    if (!id) id = Date.now().toString(36) + Math.random().toString(36).slice(2, 12);
    return String(prefix || 'entity') + '_' + id;
  }

  function stableId(value, options, idFactory) {
    options = options || {};
    value = value || {};
    var id = options.id || value.id || value.gid || value.cid;
    if (!id) id = idFactory(options.prefix || 'entity');
    return safeName(id, 'record.id');
  }

  function actualRevision(record) {
    return Number(record && record.rev || 0);
  }

  function assertExpectedRevision(collection, id, expected, current) {
    var actual = actualRevision(current);
    if (expected != null && expected !== actual) {
      throw new ConflictError(collection, id, expected, actual);
    }
    return actual;
  }

  function normalizeTimestamp(value) {
    value = Number(value);
    if (!Number.isFinite(value)) {
      throw new DataStoreError('clock 必须返回有限数字', 'BW_DATA_INVALID');
    }
    return value;
  }

  function makePutRecord(collection, id, current, value, operationOptions, metadata) {
    operationOptions = operationOptions || {};
    metadata = metadata || {};
    var revision = assertExpectedRevision(
      collection,
      id,
      operationOptions.ifRev,
      current
    );
    if (revision >= Number.MAX_SAFE_INTEGER) {
      throw new DataStoreError(
        '记录 revision 已达到安全整数上限',
        'BW_DATA_REV_OVERFLOW',
        { collection: collection, id: id, rev: revision }
      );
    }
    var body = cloneDataValue(value, 'put.value');
    body.id = id;
    if (Array.isArray(operationOptions.identityFields)) {
      operationOptions.identityFields.forEach(function (field) { body[field] = id; });
    }
    var record = {
      schema: SCHEMA,
      collection: collection,
      id: id,
      rev: revision + 1,
      updatedAt: normalizeTimestamp(metadata.updatedAt),
      updatedBy: safeName(metadata.updatedBy, 'updatedBy'),
      deleted: false,
      value: body
    };
    if (metadata.causal === true) record.causal = causalProofForParent(current);
    return record;
  }

  function makeRemoveRecord(collection, id, current, operationOptions, metadata) {
    operationOptions = operationOptions || {};
    metadata = metadata || {};
    var revision = assertExpectedRevision(
      collection,
      id,
      operationOptions.ifRev,
      current
    );
    if (revision >= Number.MAX_SAFE_INTEGER) {
      throw new DataStoreError(
        '记录 revision 已达到安全整数上限',
        'BW_DATA_REV_OVERFLOW',
        { collection: collection, id: id, rev: revision }
      );
    }
    var record = {
      schema: SCHEMA,
      collection: collection,
      id: id,
      rev: revision + 1,
      updatedAt: normalizeTimestamp(metadata.updatedAt),
      updatedBy: safeName(metadata.updatedBy, 'updatedBy'),
      deleted: true,
      value: current && current.value ? cloneDataValue(current.value, 'record.value') : { id: id }
    };
    if (metadata.causal === true) record.causal = causalProofForParent(current);
    return record;
  }

  function createMutationId(operationOptions, deviceId, at, collection, id) {
    operationOptions = operationOptions || {};
    if (operationOptions.mutationId) return String(operationOptions.mutationId);
    return String(
      safeName(deviceId, 'deviceId') + ':' + normalizeTimestamp(at).toString(36) + ':' +
      collection + ':' + id + ':' + Math.random().toString(36).slice(2, 8)
    );
  }

  function createDataStore(options) {
    options = options || {};
    var backend = options.backend || createMemoryBackend();
    if (!backend || typeof backend.load !== 'function' || typeof backend.save !== 'function') {
      throw new DataStoreError('backend 必须实现 load/save', 'BW_DATA_BACKEND');
    }
    var clock = options.clock || nowDefault;
    var idFactory = options.idFactory || defaultIdFactory;
    var deviceId = safeName(options.deviceId || 'local-device', 'deviceId');
    var maxJournal = Math.max(100, Number(options.maxJournal) || 10000);
    var maxMutations = Math.max(200, Number(options.maxMutations) || 20000);
    var causalCollections = new Set(
      (Array.isArray(options.causalCollections)
        ? options.causalCollections
        : []
      ).map(function (name) { return safeName(name, 'causalCollection'); })
    );
    var queue = Promise.resolve();
    var listeners = [];

    function serialize(task) {
      var run = queue.then(function () { return task(); });
      queue = run.catch(function () {});
      return run;
    }
    function load() { return Promise.resolve(backend.load()).then(normalizeState); }
    function save(state) { return Promise.resolve(backend.save(normalizeState(state))); }
    function collectionOf(state, name, create) {
      if (!own(state.collections, name) && create) state.collections[name] = {};
      return state.collections[name] || {};
    }
    function rememberMutation(state, mutationId, result) {
      if (!mutationId) return;
      state.mutations[mutationId] = clone(result);
      var ids = Object.keys(state.mutations);
      if (ids.length > maxMutations) {
        for (var i = 0; i < ids.length - maxMutations; i++) delete state.mutations[ids[i]];
      }
    }
    function appendChange(state, collection, record, mutationId, op) {
      state.cursor += 1;
      var change = {
        cursor: state.cursor,
        mutationId: mutationId,
        operation: op,
        collection: collection,
        record: clone(record)
      };
      state.journal.push(change);
      if (state.journal.length > maxJournal) state.journal.splice(0, state.journal.length - maxJournal);
      return change;
    }
    function mutationIdOf(options, collection, id) {
      if (options && options.mutationId) return String(options.mutationId);
      return createMutationId(options, deviceId, timestamp(), collection, id);
    }
    function timestamp() {
      return normalizeTimestamp(clock());
    }
    function notify(change) {
      listeners.slice().forEach(function (entry) {
        if (entry.collection && entry.collection !== change.collection) return;
        try { entry.listener(clone(change)); } catch (_) {}
      });
    }
    function notifyMany(changes) { (changes || []).forEach(notify); }

    function get(collection, id, options2) {
      collection = safeName(collection, 'collection');
      id = safeName(id, 'id');
      options2 = options2 || {};
      return serialize(function () {
        return load().then(function (state) {
          var record = collectionOf(state, collection, false)[id] || null;
          if (record && record.deleted && !options2.includeDeleted) return null;
          return clone(record);
        });
      });
    }

    function list(collection, query) {
      collection = safeName(collection, 'collection');
      query = query || {};
      return serialize(function () {
        return load().then(function (state) {
          var records = Object.keys(collectionOf(state, collection, false)).map(function (id) {
            return collectionOf(state, collection, false)[id];
          }).filter(function (record) {
            if (!query.includeDeleted && record.deleted) return false;
            if (query.documentId != null && String(record.value && record.value.documentId || '') !== String(query.documentId)) return false;
            if (
              query.orderBy === 'id' &&
              query.afterId != null &&
              String(record.id) <= String(query.afterId)
            ) return false;
            return true;
          }).sort(function (a, b) {
            if (query.orderBy === 'id') {
              return a.id < b.id ? -1 : (a.id > b.id ? 1 : 0);
            }
            return (a.updatedAt || 0) - (b.updatedAt || 0) || (a.id < b.id ? -1 : 1);
          });
          var offset = Math.max(0, Number(query.offset) || 0);
          var limit = Math.max(1, Math.min(1000, Number(query.limit) || 200));
          return clone(records.slice(offset, offset + limit));
        });
      });
    }

    function putWithinState(state, collection, body, options2) {
      var id = stableId(body, options2, idFactory);
      var mutationId = mutationIdOf(options2, collection, id);
      if (own(state.mutations, mutationId)) {
        return { result: clone(state.mutations[mutationId]), replay: true, change: null };
      }
      var records = collectionOf(state, collection, true);
      var current = records[id] || null;
      var record = makePutRecord(collection, id, current, body, options2, {
        updatedAt: timestamp(),
        updatedBy: deviceId,
        causal: causalCollections.has(collection)
      });
      records[id] = record;
      var change = appendChange(state, collection, record, mutationId, 'put');
      rememberMutation(state, mutationId, record);
      return { result: clone(record), replay: false, change: change };
    }

    function removeWithinState(state, collection, id, options2) {
      var mutationId = mutationIdOf(options2, collection, id);
      if (own(state.mutations, mutationId)) {
        return { result: clone(state.mutations[mutationId]), replay: true, change: null };
      }
      var records = collectionOf(state, collection, true);
      var current = records[id] || null;
      var record = makeRemoveRecord(collection, id, current, options2, {
        updatedAt: timestamp(),
        updatedBy: deviceId,
        causal: causalCollections.has(collection)
      });
      records[id] = record;
      var change = appendChange(state, collection, record, mutationId, 'remove');
      rememberMutation(state, mutationId, record);
      return { result: clone(record), replay: false, change: change };
    }

    function put(collection, value, options2) {
      collection = safeName(collection, 'collection');
      var body;
      try {
        body = normalizePutValue(value, 'put.value');
        options2 = normalizeOperationOptions(options2);
      } catch (error) {
        return Promise.reject(error);
      }
      return serialize(function () {
        var outcome;
        return load().then(function (state) {
          outcome = putWithinState(state, collection, body, options2);
          if (outcome.replay) return null;
          return save(state);
        }).then(function () {
          if (outcome.change) notify(outcome.change);
          return outcome.result;
        });
      });
    }

    function remove(collection, id, options2) {
      collection = safeName(collection, 'collection');
      id = safeName(id, 'id');
      try {
        options2 = normalizeOperationOptions(options2);
      } catch (error) {
        return Promise.reject(error);
      }
      return serialize(function () {
        var outcome;
        return load().then(function (state) {
          outcome = removeWithinState(state, collection, id, options2);
          if (outcome.replay) return null;
          return save(state);
        }).then(function () {
          if (outcome.change) notify(outcome.change);
          return outcome.result;
        });
      });
    }

    function batch(mutations) {
      var prepared;
      try {
        prepared = prepareBatch(mutations);
      } catch (error) {
        return Promise.reject(error);
      }
      if (!prepared.length) return Promise.resolve([]);
      return serialize(function () {
        var results = [];
        var notifications = [];
        var changed = false;
        return load().then(function (state) {
          prepared.forEach(function (mutation) {
            var outcome = mutation.operation === 'remove'
              ? removeWithinState(state, mutation.collection, mutation.id, mutation.options)
              : putWithinState(state, mutation.collection, mutation.value, mutation.options);
            results.push(outcome.result);
            if (outcome.change) {
              changed = true;
              notifications.push(outcome.change);
            }
          });
          return changed ? save(state) : null;
        }).then(function () {
          notifyMany(notifications);
          return results;
        });
      });
    }

    function changes(query) {
      query = query || {};
      var after = Math.max(0, Number(query.after) || 0);
      var limit = Math.max(1, Math.min(2000, Number(query.limit) || 500));
      return serialize(function () {
        return load().then(function (state) {
          var items = state.journal.filter(function (change) { return change.cursor > after; }).slice(0, limit);
          var oldestCursor = state.journal.length
            ? Math.max(0, Number(state.journal[0].cursor) || 0)
            : state.cursor + 1;
          var resetRequired = after > state.cursor ||
            after < Math.max(0, oldestCursor - 1);
          var nextCursor = items.length
            ? Math.max(after, Number(items[items.length - 1].cursor) || after)
            : after;
          return {
            contract: CONTRACT,
            cursor: state.cursor,
            nextCursor: nextCursor,
            oldestCursor: oldestCursor,
            resetRequired: resetRequired,
            hasMore: !resetRequired && nextCursor < state.cursor,
            changes: clone(items)
          };
        });
      });
    }

    function migrateLegacyCausal(options2) {
      options2 = options2 || {};
      return serialize(function () {
        return load().then(function (state) {
          var plan = planLegacyCausalMigration({
            contract: options2.contract,
            mode: options2.mode,
            after: options2.after,
            cursor: state.cursor,
            journal: state.journal,
            causalCollections: Array.from(causalCollections),
            baselineComplete: options2.baselineComplete === true,
            baselines: options2.baselines || []
          });
          var report = {
            contract: plan.contract,
            causalCollections: Array.from(causalCollections).sort(),
            after: plan.after,
            throughCursor: plan.throughCursor,
            examined: plan.examined,
            missing: plan.missing,
            migrated: plan.migrated,
            verified: plan.verified,
            needsBaseline: plan.needsBaseline
          };
          if (options2.mode !== 'apply' || !plan.patches.length) return report;
          var journalByCursor = new Map();
          state.journal.forEach(function (change) {
            journalByCursor.set(Number(change.cursor), change);
          });
          plan.patches.forEach(function (patch) {
            var original = journalByCursor.get(Number(patch.cursor));
            if (!original || !sameRecordWithoutCausal(original.record, patch.record)) {
              throw new DataStoreError(
                '旧因果迁移 journal 在事务内发生变化',
                'BW_DATA_CAUSAL_MIGRATION_RACE',
                { cursor: patch.cursor }
              );
            }
            var remembered = patch.mutationId
              ? state.mutations[String(patch.mutationId)]
              : null;
            if (
              remembered &&
              !sameRecordWithoutCausal(remembered, patch.record)
            ) {
              throw new DataStoreError(
                '旧因果迁移 mutation receipt 与 journal 不一致',
                'BW_DATA_CAUSAL_MIGRATION_RECEIPT',
                { mutationId: String(patch.mutationId) }
              );
            }
          });
          plan.finalRecords.forEach(function (finalRecord, key) {
            var parts = migrationKeyParts(key);
            var current = collectionOf(state, parts[0], false)[parts[1]] || null;
            if (!current || !sameDataValue(current, finalRecord)) {
              throw new DataStoreError(
                '旧因果迁移当前 head 与待上传 journal 不一致',
                'BW_DATA_CAUSAL_MIGRATION_HEAD',
                { collection: parts[0], id: parts[1] }
              );
            }
          });
          plan.patches.forEach(function (patch) {
            var original = journalByCursor.get(Number(patch.cursor));
            original.record = clone(patch.record);
            if (patch.mutationId && state.mutations[String(patch.mutationId)]) {
              state.mutations[String(patch.mutationId)] = clone(patch.record);
            }
          });
          plan.finalRecords.forEach(function (finalRecord, key) {
            var parts = migrationKeyParts(key);
            var records = collectionOf(state, parts[0], false);
            var current = records[parts[1]] || null;
            if (sameRecordWithoutCausal(current, finalRecord)) {
              records[parts[1]] = clone(finalRecord);
            }
          });
          return save(state).then(function () { return report; });
        });
      });
    }

    // 默认用于远端增量落库，不再次进入本地 journal，避免同步回声。
    // 对账/接管等本地导入可显式传入 { journal: true }，使导入记录在目标
    // store 中成为待同步变化。第二参数是可选的，旧调用方式保持兼容。
    function applyChanges(incoming, options2) {
      var normalizedIncoming;
      try {
        incoming = Array.isArray(incoming) ? cloneDataValue(incoming, 'incoming') : [];
        normalizedIncoming = incoming.map(normalizeIncomingChange);
      } catch (error) {
        return Promise.reject(error);
      }
      options2 = options2 || {};
      var journalImported = options2.journal === true;
      var tombstoneDominates = options2.tombstoneDominates === true;
      var snapshotBaseline = options2.snapshotBaseline === true;
      return serialize(function () {
        var notifications = [];
        return load().then(function (state) {
          var applied = [], conflicts = [], skipped = [];
          normalizedIncoming.forEach(function (normalized) {
            var change = normalized.change;
            var collection = normalized.collection;
            var mutationId = normalized.mutationId;
            var cleanRecord = normalized.record;
            if (mutationId && own(state.mutations, mutationId)) {
              skipped.push(mutationId);
              return;
            }
            var records = collectionOf(state, collection, true);
            var current = records[cleanRecord.id] || null;
            var incomingRev = cleanRecord.rev;
            var currentRev = Number(current && current.rev || 0);
            var sameBusiness = current && sameDataValue(current, cleanRecord);
            var causalRequired = causalCollections.has(collection);
            var proof = causalRequired
              ? inspectCausalProof(cleanRecord)
              : null;
            var linearTombstoneChild = causalRequired &&
              proof.valid &&
              causalParentMatches(current, proof);
            if (sameBusiness && incomingRev <= currentRev) {
              skipped.push(mutationId || (collection + '/' + cleanRecord.id));
              if (mutationId) rememberMutation(state, mutationId, cleanRecord);
              return;
            }
            if (tombstoneDominates && current && current.deleted === true &&
                cleanRecord.deleted !== true && !linearTombstoneChild) {
              conflicts.push({
                mutationId: mutationId,
                collection: collection,
                id: cleanRecord.id,
                local: clone(current),
                incoming: clone(cleanRecord),
                reason: 'tombstone-dominates'
              });
              return;
            }
            var causalAccepted = causalRequired && (
              snapshotBaseline && current === null ||
              proof.valid && causalParentMatches(current, proof)
            );
            if (!sameBusiness && causalRequired && !causalAccepted) {
              conflicts.push({
                mutationId: mutationId,
                collection: collection,
                id: cleanRecord.id,
                local: clone(current),
                incoming: clone(cleanRecord),
                incomingRev: incomingRev,
                currentRev: currentRev,
                reason: proof.valid ? 'causal-parent-mismatch' : proof.reason
              });
              return;
            }
            if (
              !sameBusiness &&
              !causalRequired &&
              current &&
              incomingRev <= currentRev
            ) {
              conflicts.push({
                mutationId: mutationId,
                collection: collection,
                id: cleanRecord.id,
                local: clone(current),
                incoming: clone(cleanRecord),
                incomingRev: incomingRev,
                currentRev: currentRev,
                reason: incomingRev === currentRev
                  ? 'same-rev-different-value'
                  : 'stale-incoming'
              });
              return;
            }
            if (
              causalRequired &&
              !sameBusiness &&
              currentRev >= Number.MAX_SAFE_INTEGER
            ) {
              conflicts.push({
                mutationId: mutationId,
                collection: collection,
                id: cleanRecord.id,
                local: clone(current),
                incoming: clone(cleanRecord),
                incomingRev: incomingRev,
                currentRev: currentRev,
                reason: 'causal-revision-overflow'
              });
              return;
            }
            var acceptedRecord = clone(cleanRecord);
            if (causalRequired && !sameBusiness) {
              acceptedRecord.rev = Math.max(incomingRev, currentRev + 1);
            }
            records[acceptedRecord.id] = clone(acceptedRecord);
            var notification = {
              cursor: Number(change.cursor) || 0,
              mutationId: mutationId,
              operation: acceptedRecord.deleted ? 'remove' : 'put',
              collection: collection,
              record: clone(acceptedRecord),
              remote: true
            };
            if (journalImported) {
              notification = clone(appendChange(
                state,
                collection,
                acceptedRecord,
                mutationId,
                acceptedRecord.deleted ? 'remove' : 'put'
              ));
              notification.imported = true;
              notification.remote = false;
            }
            applied.push({
              collection: collection,
              id: acceptedRecord.id,
              rev: acceptedRecord.rev,
              cursor: journalImported ? notification.cursor : null
            });
            notifications.push(notification);
            if (mutationId) rememberMutation(state, mutationId, acceptedRecord);
          });
          return save(state).then(function () {
            return { applied: applied, conflicts: conflicts, skipped: skipped };
          });
        }).then(function (result) {
          notifyMany(notifications);
          return result;
        });
      });
    }

    function subscribe(query, listener) {
      if (typeof query === 'function') { listener = query; query = {}; }
      if (typeof listener !== 'function') throw new DataStoreError('subscribe listener 必须是函数', 'BW_DATA_INVALID');
      var entry = { collection: query && query.collection ? String(query.collection) : '', listener: listener };
      listeners.push(entry);
      return function () {
        var index = listeners.indexOf(entry);
        if (index >= 0) listeners.splice(index, 1);
      };
    }

    function status2() {
      return serialize(function () {
        return load().then(function (state) {
          return {
            contract: CONTRACT,
            backend: String(backend.kind || 'custom'),
            deviceId: deviceId,
            cursor: state.cursor,
            journalSize: state.journal.length,
            collections: Object.keys(state.collections).sort()
          };
        });
      });
    }

    return {
      contract: CONTRACT,
      get: get,
      list: list,
      put: put,
      remove: remove,
      batch: batch,
      changes: changes,
      migrateLegacyCausal: migrateLegacyCausal,
      applyChanges: applyChanges,
      subscribe: subscribe,
      status: status2
    };
  }

  return {
    CONTRACT: CONTRACT,
    SCHEMA: SCHEMA,
    CAUSAL_CONTRACT: CAUSAL_CONTRACT,
    CAUSAL_MIGRATION_CONTRACT: CAUSAL_MIGRATION_CONTRACT,
    MAX_CAUSAL_PARENT_BYTES: MAX_CAUSAL_PARENT_BYTES,
    DataStoreError: DataStoreError,
    ConflictError: ConflictError,
    createMemoryBackend: createMemoryBackend,
    createWebStorageBackend: createWebStorageBackend,
    createDataStore: createDataStore,
    stableId: stableId,
    safeName: safeName,
    isPlainObject: isPlainObject,
    assertDataValue: assertDataValue,
    assertJSON: assertDataValue,
    cloneDataValue: cloneDataValue,
    cloneJSON: cloneDataValue,
    normalizeExpectedRev: normalizeExpectedRev,
    normalizeOperationOptions: normalizeOperationOptions,
    normalizePutValue: normalizePutValue,
    normalizeIncomingRecord: normalizeIncomingRecord,
    normalizeIncomingChange: normalizeIncomingChange,
    prepareBatch: prepareBatch,
    sameDataValue: sameDataValue,
    businessState: businessState,
    causalProofForParent: causalProofForParent,
    inspectCausalProof: inspectCausalProof,
    causalParentMatches: causalParentMatches,
    sameRecordWithoutCausal: sameRecordWithoutCausal,
    planLegacyCausalMigration: planLegacyCausalMigration,
    defaultIdFactory: defaultIdFactory,
    actualRevision: actualRevision,
    assertExpectedRevision: assertExpectedRevision,
    normalizeTimestamp: normalizeTimestamp,
    makePutRecord: makePutRecord,
    makeRemoveRecord: makeRemoveRecord,
    createMutationId: createMutationId
  };
});
