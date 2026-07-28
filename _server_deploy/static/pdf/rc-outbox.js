/* rc-outbox.js — 账户隔离、逐 mutation 持久化的 local-first 命令队列。
 *
 * 每个 pending mutation 都使用 account-context/1 当前 lease 生成独立 namespacedKey；
 * 不再读改写一个整包 JSON。flush 按 queueKey 只发送快照中最新 mutation，成功后只按
 * 精确 storage key + 原始值删除该快照记录，发送期间其它窗口写入的新 mutation 不受影响。
 *
 * 2xx 删除；普通 4xx 移入当前账户独立的 dead-letter 逐记录区；429、5xx、网络
 * 错误继续留队。历史 rc-outbox-v1 永久隔离：只统计条目数，不导入、重放、删除或外泄内容。
 * dead-letter 不自动截断；空间不足时写入失败且原 pending 保留，清理/导出策略留给显式用户流程。
 * 跨窗口/Beacon 是 at-least-once（至少一次）投递；稳定 mutationId 交给服务端做幂等，
 * 本模块不承诺 exactly-once。
 */
(function () {
  'use strict';
  var RC = (window.RC = window.RC || {});
  if (RC.outbox) return;

  var CONTRACT = 'command-outbox/2';
  var ACCOUNT_CONTRACT = 'account-context/1';
  var POLICY_CONTRACT = 'interaction-policy/1';
  var LEGACY_KEY = 'rc-outbox-v1';
  var MUTATION_AREA = 'mutation';
  var DEAD_LETTER_AREA = 'dead-letter';
  var MAX_BATCH = 100;
  var account = window.BWReaderRuntime && window.BWReaderRuntime.accountContext;
  var interactionPolicy = window.BWReaderRuntime && window.BWReaderRuntime.interactionPolicy;
  var originalFetch = window.fetch.bind(window);
  var busy = false;
  var contextEpoch = 0;
  var windowTimer = null;
  var lastError = '';
  var interactionPolicyReady = null;

  function outboxError(message, code) {
    var error = new Error(message);
    error.code = code;
    return error;
  }

  function requireInteractionPolicy() {
    if (interactionPolicyReady === true) return interactionPolicy;
    if (interactionPolicyReady === false) {
      throw outboxError(
        '交互策略尚未就绪，命令没有入队',
        'BW_OUTBOX_POLICY_UNAVAILABLE'
      );
    }
    var validation = null;
    try {
      if (
        interactionPolicy &&
        interactionPolicy.CONTRACT === POLICY_CONTRACT &&
        typeof interactionPolicy.match === 'function' &&
        typeof interactionPolicy.validate === 'function'
      ) {
        validation = interactionPolicy.validate();
      }
    } catch (_) {}
    if (
      !validation ||
      validation.contract !== POLICY_CONTRACT ||
      validation.ok !== true
    ) {
      interactionPolicyReady = false;
      throw outboxError(
        '交互策略尚未就绪，命令没有入队',
        'BW_OUTBOX_POLICY_UNAVAILABLE'
      );
    }
    interactionPolicyReady = true;
    return interactionPolicy;
  }

  function hasUnsafeDynamicPolicy(policy) {
    var matches = policy && policy.matches;
    if (!Array.isArray(matches)) return true;
    for (var i = 0; i < matches.length; i++) {
      var names = [];
      String(matches[i] && matches[i].path || '')
        .replace(/\{([A-Za-z][A-Za-z0-9_]*)\}/g, function (_, name) {
          names.push(name);
          return _;
        });
      for (var j = 0; j < names.length; j++) {
        var rule = matches[i].params && matches[i].params[names[j]];
        if (!rule || typeof rule.pattern !== 'string' || !rule.pattern) {
          return true;
        }
      }
    }
    return false;
  }

  function legacySize() {
    var raw = '';
    try { raw = localStorage.getItem(LEGACY_KEY) || ''; } catch (_) { return 0; }
    if (!raw) return 0;
    try {
      var value = JSON.parse(raw);
      if (Array.isArray(value)) return value.length;
      if (value && typeof value === 'object') return Object.keys(value).length;
    } catch (_) {}
    return 0;
  }

  function requireAccount() {
    if (
      !account ||
      account.CONTRACT !== ACCOUNT_CONTRACT ||
      typeof account.lease !== 'function' ||
      typeof account.assertCurrent !== 'function' ||
      typeof account.namespacedKey !== 'function'
    ) {
      throw outboxError('账户上下文尚未就绪，命令没有入队', 'BW_OUTBOX_ACCOUNT_UNAVAILABLE');
    }
    var lease = account.lease();
    account.assertCurrent(lease);
    return { lease: lease, ownerNamespace: lease.namespace };
  }

  function logicalKey(area, mutationId) {
    return CONTRACT + ':' + area + ':' + (mutationId || '');
  }

  function areaPrefix(scope, area) {
    return account.namespacedKey(logicalKey(area, ''), scope.lease);
  }

  function recordKey(scope, area, mutationId) {
    return account.namespacedKey(logicalKey(area, mutationId), scope.lease);
  }

  function normalizeKeyPart(value, label) {
    value = String(value == null ? '' : value).trim();
    if (!value || value.length > 500 || /[\u0000-\u001f\u007f]/.test(value)) {
      throw outboxError('无效的命令' + label, 'BW_OUTBOX_KEY');
    }
    return value;
  }

  function normalizeRequest(url, method) {
    method = String(method || 'POST').toUpperCase();
    var parsed;
    try { parsed = new URL(String(url || ''), location.origin); }
    catch (_) { throw outboxError('无效的同步端点', 'BW_OUTBOX_ENDPOINT'); }
    if (
      parsed.origin !== location.origin ||
      parsed.username ||
      parsed.password ||
      parsed.hash
    ) {
      throw outboxError('命令队列只允许当前站点固定端点', 'BW_OUTBOX_ENDPOINT');
    }
    var policyRegistry = requireInteractionPolicy();
    var policy = policyRegistry.match(parsed.pathname, method);
    if (
      !policy ||
      policy.sync !== 'outbox' ||
      !policy.transport ||
      policy.transport.outbox !== true ||
      !Array.isArray(policy.surfaces) ||
      policy.surfaces.indexOf('pwa') < 0 ||
      hasUnsafeDynamicPolicy(policy)
    ) {
      throw outboxError('命令不在同步白名单中', 'BW_OUTBOX_ENDPOINT');
    }
    return { url: parsed.pathname + parsed.search, method: method };
  }

  function newMutationId() {
    var cryptoApi = window.crypto;
    if (!cryptoApi || typeof cryptoApi.getRandomValues !== 'function') {
      throw outboxError('当前环境无法生成稳定 mutationId', 'BW_OUTBOX_MUTATION_ID');
    }
    var bytes = new Uint8Array(16);
    cryptoApi.getRandomValues(bytes);
    return 'mut-v2-' + Array.prototype.map.call(bytes, function (value) {
      return value.toString(16).padStart(2, '0');
    }).join('');
  }

  function validMutationId(value) {
    return /^mut-v2-[a-f0-9]{32}$/.test(String(value || ''));
  }

  function parseRecord(scope, area, storageKey, raw) {
    var value;
    try { value = JSON.parse(raw); } catch (_) { return null; }
    if (
      !value ||
      value.contract !== CONTRACT ||
      value.recordType !== area ||
      value.ownerNamespace !== scope.ownerNamespace ||
      !validMutationId(value.mutationId) ||
      storageKey !== recordKey(scope, area, value.mutationId)
    ) {
      return null;
    }
    if (area === MUTATION_AREA) {
      if (!value.queueKey) return null;
      try {
        var request = normalizeRequest(value.url, value.method);
        value.url = request.url;
        value.method = request.method;
      } catch (_) { return null; }
    } else {
      var status = Number(value.status);
      var rejected = status >= 400 && status < 500 && status !== 429;
      var superseded = (
        status === 0 &&
        value.reason === 'superseded-by-rejected-latest' &&
        validMutationId(value.supersededByMutationId)
      );
      if (!value.queueKey || (!rejected && !superseded)) return null;
    }
    value.storageKey = storageKey;
    value.raw = raw;
    return value;
  }

  function scanArea(scope, area) {
    account.assertCurrent(scope.lease);
    var prefix = areaPrefix(scope, area);
    var keys = [];
    try {
      for (var i = 0; i < localStorage.length; i++) {
        var key = localStorage.key(i);
        if (key && key.indexOf(prefix) === 0) keys.push(key);
      }
    } catch (_) {
      throw outboxError('无法扫描命令队列', 'BW_OUTBOX_STORAGE');
    }
    var records = [];
    for (var j = 0; j < keys.length; j++) {
      var raw = '';
      try { raw = localStorage.getItem(keys[j]) || ''; } catch (_) {}
      if (!raw) continue;
      var record = parseRecord(scope, area, keys[j], raw);
      if (record) records.push(record);
      else lastError = 'BW_OUTBOX_RECORD';
    }
    account.assertCurrent(scope.lease);
    return records;
  }

  function writeRecord(scope, area, record) {
    account.assertCurrent(scope.lease);
    if (
      !record ||
      record.contract !== CONTRACT ||
      record.recordType !== area ||
      record.ownerNamespace !== scope.ownerNamespace ||
      !validMutationId(record.mutationId)
    ) {
      throw outboxError('拒绝写入其它账户的命令记录', 'BW_OUTBOX_OWNER');
    }
    var key = recordKey(scope, area, record.mutationId);
    try { localStorage.setItem(key, JSON.stringify(record)); }
    catch (_) { throw outboxError('无法保存命令记录', 'BW_OUTBOX_STORAGE'); }
    return key;
  }

  /* Missing means another window already completed the exact same mutation and is safe.
   * A changed raw value is never removed, even though mutationId collisions are cryptographically remote. */
  function removeExact(scope, record) {
    account.assertCurrent(scope.lease);
    var current = null;
    try { current = localStorage.getItem(record.storageKey); }
    catch (_) { return false; }
    if (current == null) return true;
    if (current !== record.raw) return false;
    try { localStorage.removeItem(record.storageKey); }
    catch (_) { return false; }
    return true;
  }

  function compareMutation(left, right) {
    var timeDelta = (Number(left.ts) || 0) - (Number(right.ts) || 0);
    if (timeDelta) return timeDelta;
    return String(left.mutationId).localeCompare(String(right.mutationId));
  }

  function latestByQueue(records) {
    var latest = Object.create(null);
    records.forEach(function (record) {
      var current = latest[record.queueKey];
      if (!current || compareMutation(record, current) > 0) {
        latest[record.queueKey] = record;
      }
    });
    return Object.keys(latest).map(function (key) { return latest[key]; });
  }

  function capturedSuperseded(records, selected) {
    return records.filter(function (record) {
      return (
        record.queueKey === selected.queueKey &&
        record.mutationId !== selected.mutationId &&
        compareMutation(record, selected) <= 0
      );
    });
  }

  function removeCaptured(scope, records) {
    for (var i = 0; i < records.length; i++) removeExact(scope, records[i]);
  }

  function moveToDeadLetter(scope, record, details) {
    account.assertCurrent(scope.lease);
    var current = null;
    try { current = localStorage.getItem(record.storageKey); } catch (_) { return false; }
    if (current == null) return true;
    if (current !== record.raw) return false;
    details = details || {};
    var dead = {
      contract: CONTRACT,
      recordType: DEAD_LETTER_AREA,
      ownerNamespace: scope.ownerNamespace,
      mutationId: record.mutationId,
      queueKey: record.queueKey,
      url: record.url,
      method: record.method,
      body: record.body == null ? null : record.body,
      ts: record.ts,
      status: Number(details.status) || 0,
      failedAt: Date.now()
    };
    if (details.reason) dead.reason = String(details.reason);
    if (details.supersededByMutationId) {
      dead.supersededByMutationId = String(details.supersededByMutationId);
    }
    writeRecord(scope, DEAD_LETTER_AREA, dead);
    if (!removeExact(scope, record)) return false;
    return true;
  }

  function rejectSnapshotGroup(scope, snapshot, selected, status) {
    var older = capturedSuperseded(snapshot, selected);
    /* Move older coalesced mutations first. If storage fills up, selected remains
     * pending/latest, so a later flush cannot replay an older state backwards. */
    for (var i = 0; i < older.length; i++) {
      if (!moveToDeadLetter(scope, older[i], {
        status: 0,
        reason: 'superseded-by-rejected-latest',
        supersededByMutationId: selected.mutationId
      })) return false;
    }
    if (!moveToDeadLetter(scope, selected, { status: status })) return false;
    return true;
  }

  function scheduleFlush(delay) {
    if (windowTimer) return;
    windowTimer = setTimeout(function () {
      windowTimer = null;
      flush();
    }, Math.max(0, Number(delay) || 0));
  }

  async function flush() {
    if (busy) return { ok: false, busy: true };
    var scope;
    try { scope = requireAccount(); }
    catch (error) {
      lastError = String(error && error.code || 'BW_OUTBOX_ACCOUNT_UNAVAILABLE');
      return { ok: false, error: lastError };
    }
    var startedEpoch = contextEpoch;
    busy = true;
    try {
      var snapshot = scanArea(scope, MUTATION_AREA);
      if (!snapshot.length) return { ok: true, sent: 0 };
      var selected = latestByQueue(snapshot).sort(compareMutation).slice(0, MAX_BATCH);
      account.assertCurrent(scope.lease);
      if (startedEpoch !== contextEpoch) {
        throw outboxError('账户上下文已变化', 'BW_ACCOUNT_CONTEXT_STALE');
      }
      var response;
      try {
        response = await originalFetch('/pdf/api/sync-batch', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          credentials: 'same-origin',
          cache: 'no-store',
          body: JSON.stringify({
            contract: CONTRACT,
            ownerNamespace: scope.ownerNamespace,
            generation: scope.lease.generation,
            ops: selected.map(function (record) {
              return {
                mutationId: record.mutationId,
                url: record.url,
                method: record.method,
                body: record.body == null ? null : record.body
              };
            })
          }),
          keepalive: selected.length <= 8
        });
      } catch (_) {
        return { ok: false, offline: true };
      }
      account.assertCurrent(scope.lease);
      if (startedEpoch !== contextEpoch) {
        throw outboxError('账户上下文已变化', 'BW_ACCOUNT_CONTEXT_STALE');
      }
      if (!response.ok) return { ok: false, status: response.status };
      var data = null;
      try { data = await response.json(); } catch (_) {}
      account.assertCurrent(scope.lease);
      if (
        startedEpoch !== contextEpoch ||
        !data ||
        data.ok !== true ||
        data.contract !== CONTRACT ||
        data.ownerNamespace !== scope.ownerNamespace
      ) {
        throw outboxError('同步响应 owner 或租约已失效', 'BW_ACCOUNT_CONTEXT_STALE');
      }
      var results = Array.isArray(data.results) ? data.results : [];
      for (var i = 0; i < selected.length; i++) {
        var status = Number(results[i] && results[i].status) || 0;
        var terminal = false;
        if (status >= 200 && status < 300) {
          terminal = removeExact(scope, selected[i]);
          if (terminal) {
            removeCaptured(scope, capturedSuperseded(snapshot, selected[i]));
          }
        } else if (status >= 400 && status < 500 && status !== 429) {
          terminal = rejectSnapshotGroup(scope, snapshot, selected[i], status);
        }
      }
      lastError = '';
      return { ok: true, sent: selected.length };
    } catch (error) {
      lastError = String(error && error.code || 'BW_OUTBOX_FLUSH');
      return { ok: false, stale: lastError === 'BW_ACCOUNT_CONTEXT_STALE', error: lastError };
    } finally {
      busy = false;
      if (startedEpoch !== contextEpoch) scheduleFlush(50);
    }
  }

  function beacon() {
    if (window.__bwReaderFetch || !navigator.sendBeacon) return false;
    var scope;
    try { scope = requireAccount(); } catch (_) { return false; }
    var startedEpoch = contextEpoch;
    try {
      var selected = latestByQueue(scanArea(scope, MUTATION_AREA))
        .sort(compareMutation)
        .slice(0, MAX_BATCH);
      if (!selected.length) return false;
      account.assertCurrent(scope.lease);
      if (startedEpoch !== contextEpoch) return false;
      return navigator.sendBeacon(
        '/pdf/api/sync-batch',
        new Blob([JSON.stringify({
          contract: CONTRACT,
          ownerNamespace: scope.ownerNamespace,
          generation: scope.lease.generation,
          ops: selected.map(function (record) {
            return {
              mutationId: record.mutationId,
              url: record.url,
              method: record.method,
              body: record.body == null ? null : record.body
            };
          })
        })], { type: 'application/json' })
      );
    } catch (_) {
      return false;
    }
  }

  function areaSize(area) {
    try { return scanArea(requireAccount(), area).length; } catch (_) { return 0; }
  }

  function size() {
    return areaSize(MUTATION_AREA);
  }

  function deadLetterSize() {
    return areaSize(DEAD_LETTER_AREA);
  }

  function status() {
    var snapshot = null;
    try { snapshot = account && account.snapshot ? account.snapshot() : null; } catch (_) {}
    return Object.freeze({
      contract: CONTRACT,
      active: !!(snapshot && snapshot.active),
      ownerNamespace: snapshot && snapshot.active ? snapshot.namespace : '',
      generation: snapshot ? snapshot.generation : 0,
      size: size(),
      deadLetterSize: deadLetterSize(),
      legacySize: legacySize(),
      flushing: busy,
      lastError: lastError
    });
  }

  RC.outbox = {
    contract: CONTRACT,
    send: function (kind, key, url, body, method) {
      var scope = requireAccount();
      var request = normalizeRequest(url, method || 'POST');
      var queueKey = normalizeKeyPart(kind, '类型') + ':' + normalizeKeyPart(key, '键');
      var mutationId = '';
      var storageKey = '';
      for (var attempt = 0; attempt < 4; attempt++) {
        mutationId = newMutationId();
        storageKey = recordKey(scope, MUTATION_AREA, mutationId);
        try {
          if (localStorage.getItem(storageKey) == null) break;
        } catch (_) {
          throw outboxError('无法检查命令记录', 'BW_OUTBOX_STORAGE');
        }
        mutationId = '';
      }
      if (!mutationId) {
        throw outboxError('无法分配唯一 mutationId', 'BW_OUTBOX_MUTATION_ID');
      }
      writeRecord(scope, MUTATION_AREA, {
        contract: CONTRACT,
        recordType: MUTATION_AREA,
        ownerNamespace: scope.ownerNamespace,
        mutationId: mutationId,
        queueKey: queueKey,
        url: request.url,
        method: request.method,
        body: body == null ? null : body,
        ts: Date.now()
      });
      var count = size();
      if (count >= 20) {
        if (windowTimer) { clearTimeout(windowTimer); windowTimer = null; }
        scheduleFlush(50);
      } else {
        scheduleFlush(30000);
      }
      return mutationId;
    },
    flush: flush,
    size: size,
    legacySize: legacySize,
    deadLetterSize: deadLetterSize,
    status: status
  };

  /* 高亮 PATCH/DELETE 与便签 CRUD 的调用点分散，继续在 fetch 层提供相同离线兜底；
   * 入队前仍必须同时通过账户租约与 interaction-policy 的 outbox 门禁。 */
  window.fetch = function (input, init) {
    var url = (typeof input === 'string') ? input : ((input && input.url) || '');
    var method = (((init && init.method) || (input && input.method) || 'GET') + '').toUpperCase();
    var parsedPath = '';
    try {
      var parsed = new URL(url, location.origin);
      if (parsed.origin === location.origin) parsedPath = parsed.pathname;
    } catch (_) {}
    var isHighlight = parsedPath === '/pdf/api/highlights' && (method === 'PATCH' || method === 'DELETE');
    var isNote = parsedPath === '/pdf/api/notes' && (method === 'POST' || method === 'PATCH' || method === 'DELETE');
    if (!isHighlight && !isNote) return originalFetch(input, init);
    return originalFetch(input, init).catch(function (networkError) {
      if (!(networkError && networkError.name === 'TypeError')) throw networkError;
      var body = null;
      try { body = (init && typeof init.body === 'string') ? JSON.parse(init.body) : null; } catch (_) {}
      var id = (body && body.id) || '';
      try { if (!id) id = new URL(url, location.origin).searchParams.get('id') || ''; } catch (_) {}
      if (isNote && method === 'POST' && !id && body) {
        id = 'c_' + Array.prototype.map.call(
          crypto.getRandomValues(new Uint8Array(8)),
          function (value) { return value.toString(16).padStart(2, '0'); }
        ).join('');
        body.id = id;
      }
      if (!id) throw networkError;
      var tag = isHighlight ? 'hl' : 'note';
      var queueKey = method === 'DELETE' ? (tag + 'd:' + id)
        : method === 'POST' ? (tag + 'c:' + id)
        : (tag + 'p:' + id + ':' + Object.keys(body || {}).sort().join(','));
      RC.outbox.send(tag, queueKey, url, body, method);
      var synthetic = { ok: true, queued: true, id: id };
      if (isHighlight && method === 'PATCH' && body) synthetic.highlight = body;
      if (isNote && (method === 'POST' || method === 'PATCH') && body) {
        var now = Math.floor(Date.now() / 1000);
        synthetic.note = Object.assign({
          color: '#fff8c5', w: 260, h: 180, collapsed: false, strokes: [],
          created: now, updated: now
        }, body);
      }
      return new Response(JSON.stringify(synthetic), {
        status: 200,
        headers: { 'Content-Type': 'application/json' }
      });
    });
  };

  if (account && typeof account.subscribe === 'function') {
    account.subscribe(function () {
      contextEpoch += 1;
      if (windowTimer) { clearTimeout(windowTimer); windowTimer = null; }
      var snapshot = null;
      try { snapshot = account.snapshot(); } catch (_) {}
      if (snapshot && snapshot.active) scheduleFlush(50);
    });
  }
  window.addEventListener('pagehide', beacon);
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState === 'hidden') beacon();
  });
  window.addEventListener('online', function () { scheduleFlush(800); });
  setInterval(function () { if (size()) flush(); }, 60000);
  scheduleFlush(2500);
})();
