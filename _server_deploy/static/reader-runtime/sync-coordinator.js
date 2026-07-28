/* sync-coordinator.js — direct realtime lanes + durable server lane.
 *
 * Each lane owns independent local/remote cursors. Direct success never
 * cancels or advances the server lane. Direct imports enter the local journal
 * so the same changes are subsequently backed up by the server lane.
 */
(function (root, factory) {
  var api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.BWReaderRuntime = root.BWReaderRuntime || {};
  root.BWReaderRuntime.syncCoordinator = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function (root) {
  'use strict';

  var CONTRACT = 'sync-coordinator/1';
  var SNAPSHOT_OVERLAY_CONTRACT = 'sync-snapshot-overlay/1';
  var CAUSAL_CONTRACT = 'record-parent-state/1';
  var CAUSAL_MIGRATION_CONTRACT = 'sync-v2-causal-migration/1';

  function clone(value) {
    if (value == null) return value;
    return JSON.parse(JSON.stringify(value));
  }
  function stableStringify(value) {
    if (value === null || typeof value !== 'object') return JSON.stringify(value);
    if (Array.isArray(value)) {
      return '[' + value.map(stableStringify).join(',') + ']';
    }
    return '{' + Object.keys(value).sort().map(function (key) {
      return JSON.stringify(key) + ':' + stableStringify(value[key]);
    }).join(',') + '}';
  }
  function CoordinatorError(message, code, retryable, details) {
    this.name = 'SyncCoordinatorError';
    this.message = String(message || '同步协调失败');
    this.code = code || 'BW_SYNC_COORDINATOR';
    this.retryable = !!retryable;
    this.details = details || null;
    if (Error.captureStackTrace) Error.captureStackTrace(this, CoordinatorError);
  }
  CoordinatorError.prototype = Object.create(Error.prototype);
  CoordinatorError.prototype.constructor = CoordinatorError;

  function safe(value, label) {
    value = String(value || '').trim();
    if (!value) throw new CoordinatorError(label + ' 不能为空', 'BW_SYNC_INVALID', false);
    return value;
  }
  function cursor(value) { return Math.max(0, Number(value) || 0); }
  function errorView(error) {
    return {
      code: String(error && error.code || 'BW_SYNC_COORDINATOR'),
      error: String(error && error.message || error || '同步失败'),
      retryable: error && error.retryable !== false
    };
  }
  function emptyCheckpoint(registryDigest) {
    return {
      contract: CONTRACT,
      schema: 1,
      registryDigest: registryDigest,
      generation: 0,
      server: {
        localCursor: 0,
        remoteCursor: 0,
        reconciliationEpoch: 0
      },
      peers: {}
    };
  }
  function normalizeCheckpoint(input, registryDigest, legacyRegistryDigest) {
    input = input && typeof input === 'object' ? clone(input) : emptyCheckpoint(registryDigest);
    if (input.contract !== CONTRACT || Number(input.schema) !== 1) {
      return emptyCheckpoint(registryDigest);
    }
    var migratedLegacy = input.registryDigest === legacyRegistryDigest;
    if (input.registryDigest !== registryDigest && !migratedLegacy) {
      return emptyCheckpoint(registryDigest);
    }
    input.registryDigest = registryDigest;
    input.generation = cursor(input.generation);
    input.server = input.server && typeof input.server === 'object' ? input.server : {};
    input.server.localCursor = cursor(input.server.localCursor);
    input.server.remoteCursor = cursor(input.server.remoteCursor);
    input.server.reconciliationEpoch = cursor(
      input.server.reconciliationEpoch
    );
    input.peers = input.peers && typeof input.peers === 'object' ? input.peers : {};
    Object.keys(input.peers).forEach(function (peerId) {
      var peer = input.peers[peerId] || {};
      input.peers[peerId] = {
        sentCursor: cursor(peer.sentCursor),
        receivedCursor: cursor(peer.receivedCursor),
        baselineReady: peer.baselineReady === true
      };
    });
    // Direct peer cursors are tied to the exact wire/digest fence.  Server
    // cursors can be retained for the one known sync-v2 -> sync-v3 record
    // causality upgrade, but peers must establish a fresh baseline.
    if (migratedLegacy) {
      input.peers = {};
      Object.defineProperty(input, '__legacyCausalMigration', {
        value: {
          contract: CAUSAL_MIGRATION_CONTRACT,
          localCursor: input.server.localCursor,
          remoteCursor: input.server.remoteCursor
        },
        enumerable: false,
        configurable: false,
        writable: false
      });
    }
    return input;
  }
  function createMemoryCheckpointStore(initial) {
    var state = clone(initial || null);
    return {
      load: function () { return Promise.resolve(clone(state)); },
      save: function (next) { state = clone(next); return Promise.resolve(); },
      inspect: function () { return clone(state); }
    };
  }
  function canonicalSyncDigest(descriptor) {
    return 'sync-v3:' + CAUSAL_CONTRACT + '|' + descriptor.map(function (item) {
      return [
        item.name,
        item.conflictPolicy,
        item.derived ? '1' : '0',
        String(item.recordSchema)
      ].join(':');
    }).join('|');
  }
  function legacySyncDigest(descriptor) {
    return 'sync-v2:' + descriptor.map(function (item) {
      return [
        item.name,
        item.conflictPolicy,
        item.derived ? '1' : '0',
        String(item.recordSchema)
      ].join(':');
    }).join('|');
  }
  function registrySnapshot(registry) {
    if (!registry || registry.CONTRACT !== 'data-registry/1' ||
        registry.SYNC_CONTRACT !== 'sync-v3' ||
        registry.SYNC_CHANGE_CONTRACT !== CAUSAL_CONTRACT ||
        typeof registry.syncCollections !== 'function' ||
        typeof registry.isSyncCollection !== 'function' ||
        typeof registry.syncDescriptor !== 'function' ||
        typeof registry.syncDigest !== 'function' ||
        typeof registry.collection !== 'function' ||
        typeof registry.providerCollections !== 'function') {
      throw new CoordinatorError(
        'DataRegistry 缺少 sync-v3 因果同步合同',
        'BW_SYNC_REGISTRY',
        false
      );
    }
    var names = registry.syncCollections().map(String).sort();
    var descriptor = registry.syncDescriptor();
    var digest = String(registry.syncDigest() || '');
    var providerNames = registry.providerCollections().map(String).sort();
    if (!names.length || new Set(names).size !== names.length ||
        names.some(function (name) { return !registry.isSyncCollection(name); })) {
      throw new CoordinatorError('DataRegistry 同步白名单无效', 'BW_SYNC_REGISTRY', false);
    }
    if (!Array.isArray(descriptor) || descriptor.length !== names.length) {
      throw new CoordinatorError('DataRegistry 同步描述符无效', 'BW_SYNC_REGISTRY', false);
    }
    descriptor = descriptor.map(function (raw, index) {
      var item = raw && typeof raw === 'object' && !Array.isArray(raw)
        ? clone(raw)
        : null;
      var expectedName = names[index];
      var authority = registry.collection(expectedName);
      if (
        !item ||
        JSON.stringify(Object.keys(item).sort()) !==
          JSON.stringify(['conflictPolicy', 'derived', 'name', 'recordSchema']) ||
        String(item.name || '') !== expectedName ||
        !/^[A-Za-z0-9._-]+$/.test(expectedName) ||
        typeof item.conflictPolicy !== 'string' ||
        !item.conflictPolicy.trim() ||
        !/^[A-Za-z0-9._-]+$/.test(item.conflictPolicy) ||
        typeof item.derived !== 'boolean' ||
        !Number.isInteger(item.recordSchema) ||
        item.recordSchema < 1 ||
        !authority ||
        authority.scope !== 'global' ||
        authority.status !== 'ready' ||
        authority.provider !== true ||
        authority.sync !== true ||
        String(authority.conflictPolicy || '') !== item.conflictPolicy ||
        (authority.derived === true) !== item.derived ||
        Number(authority.recordSchema) !== item.recordSchema
      ) {
        throw new CoordinatorError(
          'DataRegistry 同步描述符与 collection 元数据不一致：' + expectedName,
          'BW_SYNC_REGISTRY',
          false
        );
      }
      return item;
    });
    if (digest !== canonicalSyncDigest(descriptor)) {
      throw new CoordinatorError(
        'DataRegistry sync-v3 摘要与描述符不一致',
        'BW_SYNC_REGISTRY',
        false
      );
    }
    if (
      new Set(providerNames).size !== providerNames.length ||
      providerNames.some(function (name) { return !name; })
    ) {
      throw new CoordinatorError(
        'DataRegistry provider 白名单无效',
        'BW_SYNC_REGISTRY',
        false
      );
    }
    var retiredDerivedCollections = new Set();
    providerNames.forEach(function (name) {
      if (names.indexOf(name) >= 0) return;
      var authority = registry.collection(name);
      if (
        authority &&
        authority.scope === 'global' &&
        authority.status === 'ready' &&
        authority.provider === true &&
        authority.sync !== true &&
        authority.derived === true &&
        authority.conflictPolicy === 'regenerate'
      ) {
        retiredDerivedCollections.add(name);
      }
    });
    return {
      names: names,
      collections: new Set(names),
      retiredDerivedCollections: retiredDerivedCollections,
      descriptor: descriptor,
      digest: digest,
      legacyDigest: legacySyncDigest(descriptor)
    };
  }
  function createSyncCoordinator(options) {
    options = options || {};
    var store = options.store;
    var registry = registrySnapshot(options.registry);
    var serverGateway = options.serverGateway;
    var checkpointStore = options.checkpointStore || createMemoryCheckpointStore();
    var assertLease = typeof options.assertLease === 'function'
      ? options.assertLease
      : function () { return true; };
    var limit = Math.max(1, Math.min(100, Number(options.limit) || 100));
    var maxSnapshotPages = Math.max(
      1,
      Math.min(1000, Number(options.maxSnapshotPages) || 1000)
    );
    var maxSnapshotRecords = Math.max(
      1,
      Math.min(100000, Number(options.maxSnapshotRecords) || 100000)
    );
    var digest = typeof options.digest === 'function'
      ? options.digest
      : function (text) {
        if (
          !root.crypto ||
          !root.crypto.subtle ||
          typeof root.crypto.subtle.digest !== 'function' ||
          typeof root.TextEncoder !== 'function'
        ) {
          throw new CoordinatorError(
            '宿主缺少 SHA-256，无法安全执行完整对账',
            'BW_SYNC_SNAPSHOT_DIGEST',
            false
          );
        }
        return root.crypto.subtle.digest(
          'SHA-256',
          new root.TextEncoder().encode(String(text))
        ).then(function (buffer) {
          return Array.prototype.map.call(
            new Uint8Array(buffer),
            function (byte) { return byte.toString(16).padStart(2, '0'); }
          ).join('');
        });
      };
    var peers = {};
    var queue = Promise.resolve();
    if (!store || typeof store.changes !== 'function' ||
        typeof store.applyChanges !== 'function') {
      throw new CoordinatorError('store 不符合 DataStore 契约', 'BW_SYNC_STORE', false);
    }
    if (!serverGateway || typeof serverGateway.push !== 'function' ||
        typeof serverGateway.pull !== 'function') {
      throw new CoordinatorError('缺少耐久 server gateway', 'BW_SYNC_GATEWAY', false);
    }
    if (!checkpointStore || typeof checkpointStore.load !== 'function' ||
        typeof checkpointStore.save !== 'function') {
      throw new CoordinatorError('checkpoint store 无效', 'BW_SYNC_CHECKPOINT', false);
    }

    function lease() {
      return Promise.resolve(assertLease()).then(function (valid) {
        if (valid === false) {
          throw new CoordinatorError('账户 lease 已失效', 'BW_SYNC_LEASE_STALE', false);
        }
      });
    }
    function loadCheckpoint() {
      return lease().then(function () {
        return Promise.resolve(checkpointStore.load());
      }).then(function (value) {
        return normalizeCheckpoint(
          value,
          registry.digest,
          registry.legacyDigest
        );
      });
    }
    function saveCheckpoint(checkpoint) {
      checkpoint.generation = cursor(checkpoint.generation) + 1;
      return lease().then(function () {
        return Promise.resolve(checkpointStore.save(clone(checkpoint)));
      }).then(function () {
        return lease();
      });
    }
    function collectionOf(change) {
      return String(change && (change.collection ||
        change.record && change.record.collection) || '');
    }
    function filterIncoming(changes, lane) {
      return (changes || []).filter(function (change) {
        var collection = collectionOf(change);
        if (registry.collections.has(collection)) return true;
        if (registry.retiredDerivedCollections.has(collection)) return false;
        throw new CoordinatorError(
          '同步入站包含未开放 collection：' + collection,
          'BW_SYNC_COLLECTION',
          false,
          { lane: lane, collection: collection }
        );
      });
    }
    function migrationStoreReport(report, marker) {
      report = report && typeof report === 'object' ? report : {};
      if (
        report.contract !== CAUSAL_MIGRATION_CONTRACT ||
        JSON.stringify(report.causalCollections || []) !==
          JSON.stringify(registry.names) ||
        cursor(report.after) !== cursor(marker.localCursor) ||
        !Number.isSafeInteger(Number(report.throughCursor)) ||
        Number(report.throughCursor) < cursor(marker.localCursor) ||
        !Number.isSafeInteger(Number(report.examined)) ||
        Number(report.examined) < 0 ||
        !Number.isSafeInteger(Number(report.missing)) ||
        Number(report.missing) < 0 ||
        !Number.isSafeInteger(Number(report.migrated)) ||
        Number(report.migrated) < 0 ||
        !Number.isSafeInteger(Number(report.verified)) ||
        Number(report.verified) < 0 ||
        typeof report.needsBaseline !== 'boolean'
      ) {
        throw new CoordinatorError(
          '本地 store 返回无效的旧因果迁移结果',
          'BW_SYNC_CAUSAL_MIGRATION_STORE',
          false
        );
      }
      return report;
    }
    function loadCausalMigrationBaselines(expectedCursor) {
      if (typeof serverGateway.snapshot !== 'function') {
        throw new CoordinatorError(
          '旧因果迁移需要服务端完整快照',
          'BW_SYNC_CAUSAL_MIGRATION_UNAVAILABLE',
          false
        );
      }
      var state = {
        snapshotId: '',
        snapshotCursor: null,
        offset: 0,
        records: 0,
        baselines: []
      };
      function page(iteration) {
        if (iteration >= maxSnapshotPages) {
          throw new CoordinatorError(
            '旧因果迁移快照分页超过安全上限',
            'BW_SYNC_CAUSAL_MIGRATION_LIMIT',
            false,
            { pages: iteration, records: state.records }
          );
        }
        var requestedOffset = state.offset;
        return lease().then(function () {
          return serverGateway.snapshot({
            snapshotId: state.snapshotId,
            offset: requestedOffset,
            limit: limit
          });
        }).then(function (snapshot) {
          return lease().then(function () { return snapshot || {}; });
        }).then(function (snapshot) {
          var pageId = safe(snapshot.snapshotId, 'migration.snapshotId');
          var pageCursor = cursor(snapshot.snapshotCursor);
          var pageOffset = cursor(snapshot.offset);
          var nextOffset = cursor(snapshot.nextOffset);
          if (!Array.isArray(snapshot.changes) || snapshot.resetRequired === true) {
            throw new CoordinatorError(
              '旧因果迁移快照无效',
              'BW_SYNC_CAUSAL_MIGRATION_BASELINE',
              false
            );
          }
          var changes = snapshot.changes;
          if (!state.snapshotId) {
            if (pageCursor !== cursor(expectedCursor)) {
              throw new CoordinatorError(
                '服务端 baseline 已前进，拒绝为旧本地记录伪造父链',
                'BW_SYNC_CAUSAL_MIGRATION_BASELINE_CHANGED',
                false,
                {
                  expectedCursor: cursor(expectedCursor),
                  snapshotCursor: pageCursor
                }
              );
            }
            state.snapshotId = pageId;
            state.snapshotCursor = pageCursor;
          } else if (
            pageId !== state.snapshotId ||
            pageCursor !== state.snapshotCursor
          ) {
            throw new CoordinatorError(
              '旧因果迁移快照在分页期间变化',
              'BW_SYNC_CAUSAL_MIGRATION_BASELINE_CHANGED',
              true
            );
          }
          if (
            pageOffset !== requestedOffset ||
            nextOffset < pageOffset ||
            nextOffset !== pageOffset + changes.length ||
            (snapshot.hasMore === true && nextOffset <= pageOffset)
          ) {
            throw new CoordinatorError(
              '旧因果迁移快照分页未推进',
              'BW_SYNC_CAUSAL_MIGRATION_PAGINATION',
              true,
              { offset: requestedOffset, nextOffset: nextOffset }
            );
          }
          state.records += changes.length;
          if (state.records > maxSnapshotRecords) {
            throw new CoordinatorError(
              '旧因果迁移快照记录超过安全上限',
              'BW_SYNC_CAUSAL_MIGRATION_LIMIT',
              false,
              { records: state.records }
            );
          }
          filterIncoming(changes, 'causal-migration').forEach(function (change) {
            state.baselines.push({
              collection: collectionOf(change),
              record: clone(change.record)
            });
          });
          state.offset = nextOffset;
          return snapshot.hasMore === true ? page(iteration + 1) : state;
        });
      }
      return page(0);
    }
    function migrateLegacyCheckpoint(checkpoint) {
      var marker = checkpoint && checkpoint.__legacyCausalMigration;
      if (!marker) return Promise.resolve(null);
      if (typeof store.migrateLegacyCausal !== 'function') {
        return Promise.reject(new CoordinatorError(
          '本地 store 不支持旧因果迁移',
          'BW_SYNC_CAUSAL_MIGRATION_UNAVAILABLE',
          false
        ));
      }
      function callStore(mode, baseline) {
        baseline = baseline || null;
        return lease().then(function () {
          return store.migrateLegacyCausal({
            contract: CAUSAL_MIGRATION_CONTRACT,
            mode: mode,
            after: marker.localCursor,
            baselineComplete: !!baseline,
            baselines: baseline ? baseline.baselines : []
          });
        }).then(function (report) {
          return lease().then(function () {
            return migrationStoreReport(report, marker);
          });
        });
      }
      var baseline = null;
      return callStore('inspect').then(function (inspection) {
        if (!inspection.needsBaseline) return null;
        return loadCausalMigrationBaselines(marker.remoteCursor).then(
          function (loaded) {
            baseline = loaded;
            return loaded;
          }
        );
      }).then(function () {
        return callStore('apply', baseline);
      }).then(function (report) {
        if (report.needsBaseline) {
          throw new CoordinatorError(
            '旧因果迁移仍缺少可信 baseline',
            'BW_SYNC_CAUSAL_MIGRATION_BASELINE',
            false
          );
        }
        return saveCheckpoint(checkpoint).then(function () {
          return {
            contract: CAUSAL_MIGRATION_CONTRACT,
            migrated: report.migrated,
            verified: report.verified,
            examined: report.examined,
            throughCursor: report.throughCursor,
            baselineCursor: baseline ? baseline.snapshotCursor : null,
            checkpointSaved: true
          };
        });
      });
    }
    function appliedCount(result) {
      if (Array.isArray(result && result.applied)) return result.applied.length;
      return Math.max(0, Number(result && result.applied) || 0);
    }
    function digestHex(text) {
      var output;
      try {
        output = digest(String(text));
      } catch (error) {
        return Promise.reject(error);
      }
      return Promise.resolve(output).then(function (value) {
        value = String(value || '').toLowerCase();
        if (!/^[0-9a-f]{64}$/.test(value)) {
          throw new CoordinatorError(
            '完整对账摘要无效',
            'BW_SYNC_SNAPSHOT_DIGEST',
            false
          );
        }
        return value;
      });
    }
    function recordKey(collection, id) {
      return String(collection) + '\u0000' + String(id);
    }
    function recordFingerprint(record) {
      return digestHex(stableStringify(record));
    }
    function snapshotChange(collection, record, snapshot) {
      var remoteFingerprint = snapshot.remoteFingerprints.get(
        recordKey(collection, record && record.id)
      ) || 'absent';
      var canonical = stableStringify({
        contract: SNAPSHOT_OVERLAY_CONTRACT,
        collection: collection,
        record: record,
        remoteFingerprint: remoteFingerprint
      });
      return digestHex(canonical).then(function (hash) {
        return {
          mutationId: 'snapshot-overlay-v1-' + hash,
          operation: record && record.deleted === true ? 'remove' : 'put',
          collection: collection,
          record: clone(record)
        };
      });
    }
    function advanceLocal(rawChanges, acknowledgedIds, start) {
      var acknowledged = new Set((acknowledgedIds || []).map(String));
      var next = cursor(start);
      var ordered = (rawChanges || []).slice().sort(function (left, right) {
        return cursor(left.cursor) - cursor(right.cursor);
      });
      for (var index = 0; index < ordered.length; index += 1) {
        var change = ordered[index];
        var changeCursor = cursor(change.cursor);
        if (changeCursor <= next) continue;
        if (!registry.collections.has(collectionOf(change))) {
          next = changeCursor;
          continue;
        }
        if (!acknowledged.has(String(change.mutationId || ''))) break;
        next = changeCursor;
      }
      return next;
    }
    function pullPages(gateway, startCursor, inboundJournal, laneName) {
      var state = {
        cursor: cursor(startCursor),
        applied: 0,
        conflicts: []
      };
      function page(iteration) {
        if (iteration > 100) {
          throw new CoordinatorError(
            '远端分页超过安全上限',
            'BW_SYNC_PAGINATION',
            true,
            { lane: laneName, cursor: state.cursor }
          );
        }
        return lease().then(function () {
          return gateway.pull({
            cursor: state.cursor,
            limit: limit
          });
        }).then(function (pulled) {
          return lease().then(function () { return pulled; });
        }).then(function (pulled) {
          pulled = pulled || {};
          if (pulled.resetRequired) {
            throw new CoordinatorError(
              '远端日志有缺口，必须完整对账',
              'BW_SYNC_RESET_REQUIRED',
              false,
              { lane: laneName, phase: 'remote' }
            );
          }
          var nextCursor = cursor(pulled.cursor);
          if (pulled.hasMore && nextCursor <= state.cursor) {
            throw new CoordinatorError(
              '远端分页未推进',
              'BW_SYNC_PAGINATION',
              true,
              { lane: laneName, cursor: state.cursor }
            );
          }
          var incoming = filterIncoming(pulled.changes || [], laneName);
          return lease().then(function () {
            return store.applyChanges(
              incoming,
              { journal: inboundJournal, tombstoneDominates: true }
            );
          }).then(function (applied) {
            return lease().then(function () { return applied; });
          }).then(function (applied) {
            var pageConflicts = [].concat(
              pulled.conflicts || [],
              applied && applied.conflicts || []
            );
            state.applied += appliedCount(applied);
            state.conflicts = state.conflicts.concat(pageConflicts);
            /* An unresolved page must remain replayable after a worker/page
             * restart. Non-conflicting records already applied from the same
             * page are idempotent, so retaining the previous cursor is safer
             * than persisting a cursor beyond a lost conflict description. */
            if (pageConflicts.length) return state;
            state.cursor = nextCursor;
            return pulled.hasMore ? page(iteration + 1) : state;
          });
        });
      }
      return page(0);
    }
    function captureLocalHead() {
      if (typeof store.status === 'function') {
        return lease().then(function () {
          return store.status();
        }).then(function (status) {
          return lease().then(function () { return cursor(status && status.cursor); });
        });
      }
      return lease().then(function () {
        return store.changes({ after: 0, limit: 1 });
      }).then(function (batch) {
        return lease().then(function () { return cursor(batch && batch.cursor); });
      });
    }
    function applyServerSnapshot(gateway) {
      if (typeof gateway.snapshot !== 'function') {
        throw new CoordinatorError(
          '服务器不支持完整快照对账',
          'BW_SYNC_SNAPSHOT_UNAVAILABLE',
          false
        );
      }
      var state = {
        snapshotId: '',
        snapshotCursor: null,
        offset: 0,
        records: 0,
        applied: 0,
        conflicts: [],
        remoteFingerprints: new Map()
      };
      function page(iteration) {
        if (iteration >= maxSnapshotPages) {
          throw new CoordinatorError(
            '服务器快照分页超过安全上限',
            'BW_SYNC_SNAPSHOT_LIMIT',
            false,
            { pages: iteration, records: state.records }
          );
        }
        var requestedOffset = state.offset;
        return lease().then(function () {
          return gateway.snapshot({
            snapshotId: state.snapshotId,
            offset: requestedOffset,
            limit: limit
          });
        }).then(function (snapshot) {
          return lease().then(function () { return snapshot || {}; });
        }).then(function (snapshot) {
          var pageId = safe(snapshot.snapshotId, 'snapshotId');
          var pageCursor = cursor(snapshot.snapshotCursor);
          var pageOffset = cursor(snapshot.offset);
          var nextOffset = cursor(snapshot.nextOffset);
          var changes = Array.isArray(snapshot.changes) ? snapshot.changes : [];
          if (!state.snapshotId) {
            state.snapshotId = pageId;
            state.snapshotCursor = pageCursor;
          } else if (
            pageId !== state.snapshotId ||
            pageCursor !== state.snapshotCursor
          ) {
            throw new CoordinatorError(
              '服务器快照在分页期间发生变化',
              'BW_SYNC_SNAPSHOT_CHANGED',
              true
            );
          }
          if (
            pageOffset !== requestedOffset ||
            nextOffset < pageOffset ||
            nextOffset !== pageOffset + changes.length ||
            (snapshot.hasMore === true && nextOffset <= pageOffset)
          ) {
            throw new CoordinatorError(
              '服务器快照分页未按协议推进',
              'BW_SYNC_SNAPSHOT_PAGINATION',
              true,
              { offset: requestedOffset, nextOffset: nextOffset }
            );
          }
          state.records += changes.length;
          if (state.records > maxSnapshotRecords) {
            throw new CoordinatorError(
              '服务器快照记录超过安全上限',
              'BW_SYNC_SNAPSHOT_LIMIT',
              false,
              { records: state.records }
            );
          }
          var incoming = filterIncoming(changes, 'server-snapshot');
          return Promise.all(incoming.map(function (change) {
            return recordFingerprint(change.record).then(function (fingerprint) {
              state.remoteFingerprints.set(
                recordKey(collectionOf(change), change.record && change.record.id),
                fingerprint
              );
            });
          })).then(function () {
            return lease();
          }).then(function () {
            return store.applyChanges(
              incoming,
              {
                journal: false,
                tombstoneDominates: true,
                snapshotBaseline: true
              }
            );
          }).then(function (applied) {
            return lease().then(function () { return applied; });
          }).then(function (applied) {
            state.applied += appliedCount(applied);
            state.conflicts = state.conflicts.concat(
              Array.isArray(applied && applied.conflicts)
                ? applied.conflicts
                : []
            );
            state.offset = nextOffset;
            if (state.conflicts.length) return state;
            return snapshot.hasMore === true ? page(iteration + 1) : state;
          });
        });
      }
      return page(0);
    }
    function pushLocalSnapshot(gateway, snapshot) {
      if (typeof store.list !== 'function') {
        throw new CoordinatorError(
          '本地 store 不支持完整记录枚举',
          'BW_SYNC_SNAPSHOT_UNAVAILABLE',
          false
        );
      }
      var state = {
        records: 0,
        pushed: 0,
        conflicts: []
      };
      function pushPage(collection, records) {
        return Promise.all(records.map(function (record) {
          return snapshotChange(collection, record, snapshot);
        })).then(function (changes) {
          return lease().then(function () {
            return gateway.push({
              cursor: cursor(snapshot.snapshotCursor),
              limit: limit,
              changes: changes
            });
          }).then(function (pushed) {
            return lease().then(function () { return pushed || {}; });
          }).then(function (pushed) {
            if (pushed.resetRequired) {
              throw new CoordinatorError(
                '服务器在完整对账期间重置',
                'BW_SYNC_RESET_REQUIRED',
                true,
                { lane: 'server', phase: 'snapshot-push' }
              );
            }
            var conflicts = Array.isArray(pushed.conflicts)
              ? pushed.conflicts
              : [];
            var acknowledged = new Set(
              (pushed.ackedMutationIds || []).map(String)
            );
            var conflicted = new Set(conflicts.map(function (conflict) {
              return String(conflict && conflict.mutationId || '');
            }));
            var missing = changes.filter(function (change) {
              return !acknowledged.has(change.mutationId) &&
                !conflicted.has(change.mutationId);
            });
            if (missing.length) {
              throw new CoordinatorError(
                '服务器未确认完整对账记录',
                'BW_SYNC_SNAPSHOT_ACK',
                true,
                { missingMutationIds: missing.map(function (item) {
                  return item.mutationId;
                }) }
              );
            }
            state.pushed += acknowledged.size;
            state.conflicts = state.conflicts.concat(conflicts);
            return state;
          });
        });
      }
      function collectionPage(collectionIndex, afterId, iteration) {
        if (collectionIndex >= registry.names.length || state.conflicts.length) {
          return Promise.resolve(state);
        }
        if (iteration >= maxSnapshotPages) {
          throw new CoordinatorError(
            '本地快照分页超过安全上限',
            'BW_SYNC_SNAPSHOT_LIMIT',
            false,
            { collection: registry.names[collectionIndex], afterId: afterId }
          );
        }
        var collection = registry.names[collectionIndex];
        return lease().then(function () {
          return store.list(collection, {
            includeDeleted: true,
            orderBy: 'id',
            afterId: afterId,
            limit: limit
          });
        }).then(function (records) {
          return lease().then(function () { return records; });
        }).then(function (records) {
          if (!Array.isArray(records)) {
            throw new CoordinatorError(
              '本地 store list 返回无效结果',
              'BW_SYNC_SNAPSHOT_STORE',
              false,
              { collection: collection }
            );
          }
          state.records += records.length;
          if (state.records > maxSnapshotRecords) {
            throw new CoordinatorError(
              '本地快照记录超过安全上限',
              'BW_SYNC_SNAPSHOT_LIMIT',
              false,
              { records: state.records }
            );
          }
          var previousId = String(afterId || '');
          records.forEach(function (record) {
            var recordId = safe(record && record.id, '本地快照 record.id');
            if (recordId <= previousId) {
              throw new CoordinatorError(
                '本地快照主键分页未推进',
                'BW_SYNC_SNAPSHOT_PAGINATION',
                true,
                { collection: collection, afterId: previousId, id: recordId }
              );
            }
            previousId = recordId;
          });
          var pushed = records.length
            ? pushPage(collection, records)
            : Promise.resolve(state);
          return pushed.then(function () {
            if (state.conflicts.length) return state;
            if (records.length === limit) {
              return collectionPage(
                collectionIndex,
                previousId,
                iteration + 1
              );
            }
            return collectionPage(collectionIndex + 1, '', iteration + 1);
          });
        });
      }
      return collectionPage(0, '', 0);
    }
    function reconcileServer(laneState, resetError, attempt) {
      attempt = Math.max(0, Number(attempt) || 0);
      var originalLocalCursor = cursor(laneState.localCursor);
      var originalRemoteCursor = cursor(laneState.remoteCursor);
      var localHead;
      var snapshot;
      return captureLocalHead().then(function (head) {
        localHead = head;
        return applyServerSnapshot(serverGateway);
      }).then(function (value) {
        snapshot = value;
        if (snapshot.conflicts.length) {
          return {
            ok: true,
            lane: 'server',
            reconciled: false,
            resetRecovered: false,
            resetPhase: resetError && resetError.details &&
              resetError.details.phase || '',
            localCursor: originalLocalCursor,
            remoteCursor: originalRemoteCursor,
            pendingLocal: true,
            applied: snapshot.applied,
            conflicts: snapshot.conflicts,
            snapshotId: snapshot.snapshotId,
            snapshotCursor: snapshot.snapshotCursor
          };
        }
        return pushLocalSnapshot(serverGateway, snapshot);
      }).then(function (overlay) {
        if (overlay && overlay.lane === 'server') return overlay;
        if (overlay.conflicts.length) {
          return {
            ok: true,
            lane: 'server',
            reconciled: false,
            resetRecovered: false,
            resetPhase: resetError && resetError.details &&
              resetError.details.phase || '',
            localCursor: originalLocalCursor,
            remoteCursor: originalRemoteCursor,
            pendingLocal: true,
            applied: snapshot.applied,
            conflicts: overlay.conflicts,
            snapshotId: snapshot.snapshotId,
            snapshotCursor: snapshot.snapshotCursor
          };
        }
        return pullPages(
          serverGateway,
          snapshot.snapshotCursor,
          false,
          'server'
        ).then(function (pulled) {
          laneState.localCursor = localHead;
          laneState.remoteCursor = pulled.cursor;
          return captureLocalHead().then(function (currentLocalHead) {
            return {
              ok: true,
              lane: 'server',
              reconciled: pulled.conflicts.length === 0,
              resetRecovered: pulled.conflicts.length === 0,
              resetPhase: resetError && resetError.details &&
                resetError.details.phase || '',
              localCursor: laneState.localCursor,
              remoteCursor: laneState.remoteCursor,
              pendingLocal: laneState.localCursor < currentLocalHead,
              applied: snapshot.applied + pulled.applied,
              conflicts: pulled.conflicts,
              snapshotId: snapshot.snapshotId,
              snapshotCursor: snapshot.snapshotCursor
            };
          });
        });
      }).catch(function (error) {
        laneState.localCursor = originalLocalCursor;
        laneState.remoteCursor = originalRemoteCursor;
        if (error && error.code === 'BW_SYNC_RESET_REQUIRED') {
          if (attempt < 1) {
            return reconcileServer(laneState, error, attempt + 1);
          }
          throw new CoordinatorError(
            '服务器在完整对账期间持续重置',
            'BW_SYNC_SNAPSHOT_UNSTABLE',
            true,
            { lane: 'server', phase: error.details && error.details.phase }
          );
        }
        throw error;
      });
    }
    function runLane(gateway, laneState, inboundJournal, laneName) {
      var batch;
      var pushed;
      return lease().then(function () {
        return store.changes({ after: cursor(laneState.localCursor), limit: limit });
      }).then(function (value) {
        batch = value || {};
        if (batch.resetRequired) {
          throw new CoordinatorError(
            '本地 journal 有缺口，禁止上传残缺增量',
            'BW_SYNC_RESET_REQUIRED',
            false,
            { lane: laneName, phase: 'local' }
          );
        }
        var outgoing = (batch.changes || []).filter(function (change) {
          return registry.collections.has(collectionOf(change));
        });
        return lease().then(function () {
          return gateway.push({
            cursor: cursor(laneState.remoteCursor),
            limit: limit,
            changes: outgoing
          });
        });
      }).then(function (value) {
        return lease().then(function () { return value; });
      }).then(function (value) {
        pushed = value || {};
        if (pushed.resetRequired) {
          throw new CoordinatorError(
            '远端拒绝增量，必须完整对账',
            'BW_SYNC_RESET_REQUIRED',
            false,
            { lane: laneName, phase: 'push' }
          );
        }
        laneState.localCursor = advanceLocal(
          batch.changes || [],
          pushed.ackedMutationIds || [],
          laneState.localCursor
        );
        return pullPages(
          gateway,
          laneState.remoteCursor,
          inboundJournal,
          laneName
        );
      }).then(function (pulled) {
        laneState.remoteCursor = pulled.cursor;
        var localHead = Math.max(
          cursor(batch.cursor),
          cursor(batch.nextCursor),
          cursor(laneState.localCursor)
        );
        return {
          ok: true,
          lane: laneName,
          localCursor: laneState.localCursor,
          remoteCursor: laneState.remoteCursor,
          pendingLocal: laneState.localCursor < localHead || !!batch.hasMore,
          applied: pulled.applied,
          conflicts: (pushed.conflicts || []).concat(pulled.conflicts || [])
        };
      });
    }
    function addPeer(peerId, gateway, peerOptions) {
      peerId = safe(peerId, 'peerId');
      if (!gateway || typeof gateway.push !== 'function' ||
          typeof gateway.pull !== 'function') {
        throw new CoordinatorError('peer gateway 无效', 'BW_SYNC_GATEWAY', false);
      }
      peers[peerId] = {
        gateway: gateway,
        baselineReady: !!(peerOptions && peerOptions.baselineReady),
        baselineLocalCursor: cursor(
          peerOptions && peerOptions.baselineLocalCursor
        ),
        baselineRemoteCursor: cursor(
          peerOptions && peerOptions.baselineRemoteCursor
        ),
        baselineInitialized: false
      };
      return peerId;
    }
    function removePeer(peerId) {
      delete peers[String(peerId || '')];
    }
    function executeRun() {
      var result = {
        contract: CONTRACT,
        direct: {},
        server: null,
        serverBackupAttempted: false,
        causalMigration: null
      };
      return loadCheckpoint().then(function (checkpoint) {
        return migrateLegacyCheckpoint(checkpoint).then(
          function (migration) {
            if (migration) result.causalMigration = { ok: true, ...migration };
            var chain = Promise.resolve();
            Object.keys(peers).sort().forEach(function (peerId) {
              chain = chain.then(function () {
                if (migration) {
                  result.direct[peerId] = {
                    ok: false,
                    skipped: true,
                    code: 'BW_SYNC_CAUSAL_MIGRATION_SERVER_FIRST'
                  };
                  return;
                }
                var peer = peers[peerId];
                var peerState = checkpoint.peers[peerId] || {
                  sentCursor: 0,
                  receivedCursor: 0,
                  baselineReady: false
                };
                checkpoint.peers[peerId] = peerState;
                /* baselineReady is a live property of this authenticated peer
                 * session. A persisted checkpoint from an older RTC connection
                 * must never authorize a newly created channel by itself. */
                if (!peer.baselineReady) {
                  result.direct[peerId] = {
                    ok: false,
                    skipped: true,
                    code: 'BW_SYNC_BASELINE_REQUIRED'
                  };
                  return;
                }
                if (!peer.baselineInitialized) {
                  peerState.sentCursor = peer.baselineLocalCursor;
                  peerState.receivedCursor = peer.baselineRemoteCursor;
                  peer.baselineInitialized = true;
                }
                peerState.baselineReady = true;
                var laneState = {
                  localCursor: peerState.sentCursor,
                  remoteCursor: peerState.receivedCursor
                };
                return runLane(peer.gateway, laneState, true, 'direct:' + peerId)
                  .then(function (laneResult) {
                    peerState.sentCursor = laneState.localCursor;
                    peerState.receivedCursor = laneState.remoteCursor;
                    result.direct[peerId] = laneResult;
                    return saveCheckpoint(checkpoint);
                  }, function (error) {
                    result.direct[peerId] = { ok: false, ...errorView(error) };
                  });
              });
            });
            return chain.then(function () {
              // This is intentionally unconditional: no direct result can short-circuit it.
              result.serverBackupAttempted = true;
              var laneState = {
                localCursor: checkpoint.server.localCursor,
                remoteCursor: checkpoint.server.remoteCursor
              };
              return runLane(serverGateway, laneState, false, 'server').catch(
                function (error) {
                  if (error && error.code === 'BW_SYNC_RESET_REQUIRED') {
                    return reconcileServer(laneState, error, 0);
                  }
                  throw error;
                }
              ).then(
                function (laneResult) {
                  checkpoint.server.localCursor = laneState.localCursor;
                  checkpoint.server.remoteCursor = laneState.remoteCursor;
                  if (laneResult && laneResult.resetRecovered === true) {
                    checkpoint.server.reconciliationEpoch = cursor(
                      checkpoint.server.reconciliationEpoch
                    ) + 1;
                  }
                  result.server = laneResult;
                  return saveCheckpoint(checkpoint);
                },
                function (error) {
                  result.server = { ok: false, ...errorView(error) };
                }
              );
            }).then(function () {
              result.checkpoint = clone(checkpoint);
              return result;
            });
          },
          function (error) {
            result.causalMigration = { ok: false, ...errorView(error) };
            result.server = { ok: false, ...errorView(error) };
            result.checkpoint = clone(checkpoint);
            return result;
          }
        );
      });
    }
    function runOnce() {
      var current = queue.catch(function () {}).then(executeRun);
      queue = current.catch(function () {});
      return current;
    }

    return {
      contract: CONTRACT,
      registryDigest: registry.digest,
      addPeer: addPeer,
      removePeer: removePeer,
      runOnce: runOnce,
      checkpoint: loadCheckpoint,
      status: function () {
        return loadCheckpoint().then(function (checkpoint) {
          return {
            contract: CONTRACT,
            registryDigest: registry.digest,
            peers: Object.keys(peers).sort(),
            checkpoint: checkpoint
          };
        });
      }
    };
  }

  return {
    CONTRACT: CONTRACT,
    CoordinatorError: CoordinatorError,
    createMemoryCheckpointStore: createMemoryCheckpointStore,
    createSyncCoordinator: createSyncCoordinator
  };
});
