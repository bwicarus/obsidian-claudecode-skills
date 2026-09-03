/* anki-mobile-export.js — optional AnkiMobile projection for Reader cards.
 *
 * Reader's local card repository remains authoritative.  This module only
 * launches AnkiMobile's documented x-callback-url surface and records a
 * projection receipt on the already-confirmed local card.  Opening the app is
 * not proof that a note was added: only the nonce-bound callback may mark the
 * receipt succeeded, and a missing callback is never retried automatically.
 */
(function (root, factory) {
  var api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.BWReaderRuntime = root.BWReaderRuntime || {};
  root.BWReaderRuntime.ankiMobileExport = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function (root) {
  'use strict';

  var CONTRACT = 'anki-mobile-export/1';
  var TARGET = 'ankimobile-ipad';
  var MAX_URL_BYTES = 32 * 1024;
  var PENDING_TTL_MS = 10 * 60 * 1000;
  var EXPIRY_RETRY_MS = 30 * 1000;
  var MAX_TIMER_DELAY_MS = 0x7fffffff;
  var CARD_ID_RE = /^card_[a-f0-9]{4,64}$/;
  var NONCE_RE = /^[a-f0-9]{32}$/;

  function ExportError(message, code, details) {
    this.name = 'AnkiMobileExportError';
    this.code = code || 'BW_ANKIMOBILE_EXPORT';
    this.message = String(message || 'AnkiMobile 导出失败');
    this.details = details || null;
    if (Error.captureStackTrace) Error.captureStackTrace(this, ExportError);
  }
  ExportError.prototype = Object.create(Error.prototype);
  ExportError.prototype.constructor = ExportError;

  function own(value, key) {
    return Object.prototype.hasOwnProperty.call(value || {}, key);
  }
  function plain(value) {
    if (!value || Object.prototype.toString.call(value) !== '[object Object]') {
      return false;
    }
    var proto = Object.getPrototypeOf(value);
    return proto === Object.prototype || proto === null;
  }
  function utf8Bytes(value) {
    value = String(value == null ? '' : value);
    if (typeof TextEncoder !== 'undefined') {
      return new TextEncoder().encode(value).byteLength;
    }
    return unescape(encodeURIComponent(value)).length;
  }
  function rejectNul(value, label) {
    if (String(value).indexOf('\u0000') >= 0) {
      throw new ExportError(label + ' 含 NUL', 'BW_ANKIMOBILE_INPUT');
    }
  }
  function gidOf(value) {
    value = String(value == null ? '' : value).trim().toLowerCase();
    rejectNul(value, 'gid');
    if (!CARD_ID_RE.test(value)) {
      throw new ExportError('gid 不是稳定的 Reader card_* 编号', 'BW_ANKIMOBILE_ID');
    }
    return value;
  }
  function indexOf(value) {
    var index = Number(value);
    if (!Number.isSafeInteger(index) || index < 0 || index > 255) {
      throw new ExportError('卡片 index 无效', 'BW_ANKIMOBILE_INDEX');
    }
    return index;
  }
  function exactKeys(value, allowed, label) {
    if (!plain(value)) {
      throw new ExportError(label + ' 必须是对象', 'BW_ANKIMOBILE_INPUT');
    }
    Object.keys(value).forEach(function (key) {
      if (!allowed[key]) {
        throw new ExportError(
          label + ' 含未知字段：' + key,
          'BW_ANKIMOBILE_INPUT'
        );
      }
    });
  }
  function strictEncode(value) {
    rejectNul(value, 'URL 参数');
    return encodeURIComponent(String(value)).replace(/[!'()*]/g, function (char) {
      return '%' + char.charCodeAt(0).toString(16).toUpperCase();
    });
  }
  function query(items) {
    return items.map(function (item) {
      return strictEncode(item[0]) + '=' + strictEncode(item[1]);
    }).join('&');
  }
  function htmlField(value, label) {
    value = String(value == null ? '' : value).replace(/\r\n?/g, '\n').trim();
    rejectNul(value, label);
    if (!value) {
      throw new ExportError(label + ' 不能为空', 'BW_ANKIMOBILE_CARD');
    }
    // AnkiMobile interprets fld* values as HTML. Preserve intentional markup,
    // while making line breaks explicit instead of relying on plain-text URL
    // handling that differs between clients.
    return value.replace(/\n/g, '<br>');
  }
  function tagsFor(gid, index, card) {
    var tags = [
      'bwreader',
      'bwgid_' + gid,
      'bwindex_' + index
    ];
    if (card.tags != null) {
      if (!Array.isArray(card.tags) || card.tags.length > 32) {
        throw new ExportError('card.tags 无效', 'BW_ANKIMOBILE_CARD');
      }
      card.tags.forEach(function (tag) {
        tag = String(tag == null ? '' : tag).trim();
        rejectNul(tag, 'card.tags[]');
        if (!tag || /\s/.test(tag) || utf8Bytes(tag) > 128) {
          throw new ExportError('card.tags[] 无效', 'BW_ANKIMOBILE_CARD');
        }
        if (tags.indexOf(tag) < 0) tags.push(tag);
      });
    }
    return tags.join(' ');
  }
  function secureNonce(environment) {
    var cryptoApi = environment && environment.crypto;
    var hex = '';
    try {
      if (cryptoApi && typeof cryptoApi.randomUUID === 'function') {
        var uuid = String(cryptoApi.randomUUID() || '').toLowerCase();
        if (/^[a-f0-9-]{36}$/.test(uuid)) hex = uuid.replace(/-/g, '');
      }
      if (!hex && cryptoApi && typeof cryptoApi.getRandomValues === 'function') {
        var bytes = new Uint8Array(16);
        cryptoApi.getRandomValues(bytes);
        hex = Array.prototype.map.call(bytes, function (byte) {
          return byte.toString(16).padStart(2, '0');
        }).join('');
      }
    } catch (_) {}
    if (!/^[a-f0-9]{32}$/.test(hex)) {
      throw new ExportError(
        '无法安全生成 AnkiMobile 回调 nonce',
        'BW_ANKIMOBILE_RANDOM'
      );
    }
    return hex;
  }
  function normalizedCard(card) {
    if (!plain(card)) {
      throw new ExportError('本地卡片数据无效', 'BW_ANKIMOBILE_CARD');
    }
    var type = String(card.type || '').toLowerCase();
    if (type === 'basic') {
      exactKeys(card, {
        type: true, front: true, back: true, deck: true,
        tags: true, reason: true
      }, 'card');
      return {
        type: 'Basic',
        fields: [
          ['fldFront', htmlField(card.front, 'card.front')],
          ['fldBack', htmlField(card.back, 'card.back')]
        ],
        tags: card.tags || []
      };
    }
    if (type === 'cloze') {
      exactKeys(card, {
        type: true, cloze: true, deck: true, tags: true, reason: true
      }, 'card');
      var cloze = htmlField(card.cloze, 'card.cloze');
      if (!/\{\{c[1-9]\d*::[\s\S]+?\}\}/.test(cloze)) {
        throw new ExportError('cloze 卡缺少有效挖空', 'BW_ANKIMOBILE_CARD');
      }
      return {
        type: 'Cloze',
        fields: [['fldText', cloze]],
        tags: card.tags || []
      };
    }
    throw new ExportError(
      'AnkiMobile 投影只支持 Basic 或 Cloze',
      'BW_ANKIMOBILE_CARD'
    );
  }
  function addNoteURL(gid, index, card, nonce) {
    var normalized = normalizedCard(card);
    var callback = 'bwreader://anki-export-success?' + query([
      ['gid', gid],
      ['index', String(index)],
      ['nonce', nonce]
    ]);
    var items = [
      ['type', normalized.type],
      ['deck', 'BW Reader']
    ].concat(normalized.fields, [
      ['tags', tagsFor(gid, index, normalized)],
      ['x-success', callback]
    ]);
    var url = 'anki://x-callback-url/addnote?' + query(items);
    var bytes = utf8Bytes(url);
    if (bytes > MAX_URL_BYTES) {
      throw new ExportError(
        'AnkiMobile URL 超出 32KB，未打开外部 App',
        'BW_ANKIMOBILE_URL_TOO_LARGE',
        { actualBytes: bytes, maximumBytes: MAX_URL_BYTES }
      );
    }
    return { url: url, urlBytes: bytes };
  }

  function createAnkiMobileExport(options) {
    options = options || {};
    var environment = options.root || root;
    var clock = typeof options.clock === 'function' ? options.clock : Date.now;
    var pending = new Map();
    var sequence = 0;
    var restorePromise = null;
    var expiryTimer = null;

    function repository() {
      var repo = options.repository || (
        environment.BWReaderRuntime &&
        environment.BWReaderRuntime.cardRepository
      );
      if (!repo || typeof repo.load !== 'function' ||
          typeof repo.snapshot !== 'function' ||
          typeof repo.recordAnkiReceipt !== 'function') {
        throw new ExportError(
          'Reader 本地卡仓尚未准备好',
          'BW_ANKIMOBILE_REPOSITORY_UNAVAILABLE'
        );
      }
      return repo;
    }
    function bridge() {
      return options.bridge || environment.__bwNativeAnkiMobile;
    }
    function available() {
      var nativeBridge = bridge();
      var repo;
      try { repo = repository(); } catch (_) { return false; }
      return environment.__BW_NATIVE_LOCAL_READER__ === true &&
        !!repo && !!nativeBridge && typeof nativeBridge.request === 'function';
    }
    function attemptId(gid, index, at) {
      sequence += 1;
      return 'ankimobile:' + gid + ':' + index + ':' + at + ':' + sequence;
    }
    function receiptMutation(kind, gid, index, at) {
      sequence += 1;
      return 'anki-mobile-' + kind + ':' + gid + ':' + index + ':' + at + ':' + sequence;
    }
    function keyOf(gid, index) { return gid + ':' + index; }
    function log(message) {
      try {
        if (typeof environment.dlog === 'function') {
          environment.dlog('[anki-mobile] ' + String(message));
        }
      } catch (_) {}
    }
    function timerSet(callback, delay) {
      var target = options.setTimeout || environment.setTimeout;
      if (typeof target !== 'function') return null;
      return target.call(environment, callback, delay);
    }
    function timerClear(timer) {
      var target = options.clearTimeout || environment.clearTimeout;
      if (timer != null && typeof target === 'function') {
        target.call(environment, timer);
      }
    }
    function scheduleExpiry() {
      timerClear(expiryTimer);
      expiryTimer = null;
      var due = Infinity;
      pending.forEach(function (entry) {
        var candidate = entry.nextRetryAt || entry.expiresAt;
        if (candidate < due) due = candidate;
      });
      if (!Number.isFinite(due)) return;
      var delay = Math.max(0, due - Number(clock()));
      expiryTimer = timerSet(function () {
        expiryTimer = null;
        expirePending().catch(function (error) {
          log('清理超期回调失败：' + String(error && error.message || error));
        });
      }, Math.min(delay, MAX_TIMER_DELAY_MS));
    }
    function pendingReceipt(group, cardIndex) {
      if (!plain(group) || group.deleted === true || !plain(group.states)) {
        return null;
      }
      var gid = gidOf(group.gid || group.id);
      var index = indexOf(cardIndex);
      var state = group.states[String(cardIndex)];
      var receipt = state && state.projections && state.projections.anki &&
        state.projections.anki[TARGET];
      if (!plain(receipt) || receipt.status !== 'pending' ||
          typeof receipt.mutationId !== 'string' || !receipt.mutationId ||
          !plain(receipt.detail)) return null;
      var detail = receipt.detail;
      exactKeys(detail, {
        channel: true, callbackExpected: true, callbackReceived: true,
        urlBytes: true, callbackNonce: true, callbackExpiresAt: true
      }, 'pending receipt detail');
      var nonce = String(detail.callbackNonce || '');
      var expiresAt = Number(detail.callbackExpiresAt);
      var updatedAt = Number(receipt.updatedAt);
      var attemptPrefix = 'ankimobile:' + gid + ':' + index + ':' + updatedAt + ':';
      if (detail.channel !== 'x-callback-url' ||
          detail.callbackExpected !== true ||
          detail.callbackReceived !== false ||
          !NONCE_RE.test(nonce) ||
          !Number.isSafeInteger(updatedAt) || updatedAt < 0 ||
          !Number.isSafeInteger(expiresAt) ||
          expiresAt !== updatedAt + PENDING_TTL_MS ||
          receipt.mutationId.indexOf(attemptPrefix) !== 0) return null;
      return {
        gid: gid,
        index: index,
        nonce: nonce,
        attemptId: receipt.mutationId,
        expiresAt: expiresAt,
        settling: '',
        nextRetryAt: 0
      };
    }
    function succeededReceiptMatches(group, cardIndex, nonce) {
      if (!plain(group) || group.deleted === true || !plain(group.states)) {
        return false;
      }
      var state = group.states[String(cardIndex)];
      var receipt = state && state.projections && state.projections.anki &&
        state.projections.anki[TARGET];
      var detail = receipt && receipt.detail;
      if (!plain(receipt) || receipt.status !== 'succeeded' || !plain(detail)) {
        return false;
      }
      try {
        exactKeys(detail, {
          channel: true, callbackExpected: true, callbackReceived: true,
          callbackNonce: true, callbackExpiresAt: true
        }, 'succeeded receipt detail');
      } catch (_) { return false; }
      return detail.channel === 'x-callback-url' &&
        detail.callbackExpected === true &&
        detail.callbackReceived === true &&
        detail.callbackNonce === nonce &&
        Number.isSafeInteger(Number(detail.callbackExpiresAt));
    }
    async function restorePending() {
      var groups = await repository().snapshot({ includeDeleted: false });
      if (!Array.isArray(groups)) {
        throw new ExportError(
          'Reader 本地卡仓快照无效',
          'BW_ANKIMOBILE_REPOSITORY_UNAVAILABLE'
        );
      }
      groups.forEach(function (group) {
        if (!plain(group) || !plain(group.states)) return;
        Object.keys(group.states).forEach(function (indexValue) {
          var entry;
          try { entry = pendingReceipt(group, indexValue); }
          catch (_) { entry = null; }
          if (!entry) return;
          var key = keyOf(entry.gid, entry.index);
          if (!pending.has(key)) pending.set(key, entry);
        });
      });
      await expirePending();
      scheduleExpiry();
      return true;
    }
    function ensureRestored() {
      if (!restorePromise) {
        restorePromise = restorePending().catch(function (error) {
          restorePromise = null;
          throw error;
        });
      }
      return restorePromise;
    }
    async function expirePending() {
      var at = Number(clock());
      var tasks = [];
      pending.forEach(function (entry, key) {
        if (entry.expiresAt > at || entry.nextRetryAt > at || entry.settling) {
          return;
        }
        entry.settling = 'expiry';
        tasks.push(repository().recordAnkiReceipt(
          entry.gid,
          entry.index,
          TARGET,
          {
            status: 'unknown',
            mutationId: entry.attemptId,
            updatedAt: at,
            error: '',
            detail: {
              channel: 'x-callback-url',
              callbackExpected: true,
              callbackReceived: false,
              reason: 'callback-expired'
            }
          },
          { mutationId: receiptMutation('unknown', entry.gid, entry.index, at) }
        ).then(function () {
          if (pending.get(key) === entry) pending.delete(key);
        }, function (error) {
          entry.settling = '';
          entry.nextRetryAt = at + EXPIRY_RETRY_MS;
          log('记录未回调状态失败：' + String(error && error.message || error));
        }));
      });
      await Promise.all(tasks);
      scheduleExpiry();
    }
    async function recordFailure(repo, gid, index, id, message) {
      var at = Number(clock());
      await repo.recordAnkiReceipt(gid, index, TARGET, {
        status: 'failed',
        mutationId: id,
        updatedAt: at,
        error: String(message || 'AnkiMobile 无法打开').slice(0, 4096),
        detail: {
          channel: 'x-callback-url',
          callbackExpected: true,
          callbackReceived: false
        }
      }, { mutationId: receiptMutation('failed', gid, index, at) });
    }

    async function exportCard(gid, cardIndex) {
      if (arguments.length !== 2) {
        throw new ExportError(
          'exportCard 只接受 gid 与 index',
          'BW_ANKIMOBILE_INPUT'
        );
      }
      gid = gidOf(gid);
      cardIndex = indexOf(cardIndex);
      if (!available()) {
        throw new ExportError(
          'AnkiMobile 投影仅在 BWReader App 内可用',
          'BW_ANKIMOBILE_UNAVAILABLE'
        );
      }
      await ensureRestored();
      await expirePending();
      var pendingKey = keyOf(gid, cardIndex);
      if (pending.has(pendingKey)) {
        throw new ExportError(
          '这张卡仍在等待 AnkiMobile 回调，未重复打开',
          'BW_ANKIMOBILE_PENDING'
        );
      }
      var repo = repository();
      var group = await repo.load(gid);
      if (!group || group.deleted === true || !Array.isArray(group.cards) ||
          !plain(group.states) || !group.cards[cardIndex] ||
          !group.states[String(cardIndex)]) {
        throw new ExportError('本地卡仓找不到这张卡', 'BW_ANKIMOBILE_NOT_FOUND');
      }
      if (group.states[String(cardIndex)].phase !== 'confirmed') {
        throw new ExportError(
          '卡片必须先确认保存到 Reader 本地卡仓',
          'BW_ANKIMOBILE_NOT_CONFIRMED'
        );
      }
      var nonce = secureNonce(environment);
      var prepared = addNoteURL(gid, cardIndex, group.cards[cardIndex], nonce);
      var at = Number(clock());
      var id = attemptId(gid, cardIndex, at);

      // The local receipt is durable before the external scheme is opened.
      var expiresAt = at + PENDING_TTL_MS;
      await repo.recordAnkiReceipt(gid, cardIndex, TARGET, {
        status: 'pending',
        mutationId: id,
        updatedAt: at,
        error: '',
        detail: {
          channel: 'x-callback-url',
          callbackExpected: true,
          callbackReceived: false,
          urlBytes: prepared.urlBytes,
          callbackNonce: nonce,
          callbackExpiresAt: expiresAt
        }
      }, { mutationId: receiptMutation('pending', gid, cardIndex, at) });

      pending.set(pendingKey, {
        gid: gid,
        index: cardIndex,
        nonce: nonce,
        attemptId: id,
        expiresAt: expiresAt,
        settling: '',
        nextRetryAt: 0
      });
      scheduleExpiry();
      var result;
      try {
        result = await bridge().request({
          action: 'open',
          gid: gid,
          index: cardIndex,
          nonce: nonce,
          expiresAt: expiresAt,
          url: prepared.url
        });
      } catch (error) {
        try {
          await recordFailure(repo, gid, cardIndex, id, error && error.message || error);
          pending.delete(pendingKey);
          scheduleExpiry();
        } catch (recordError) {
          log('记录打开失败状态失败：' +
            String(recordError && recordError.message || recordError));
        }
        throw new ExportError(
          'AnkiMobile 打开失败：' + String(error && error.message || error),
          'BW_ANKIMOBILE_OPEN_FAILED'
        );
      }
      if (!result || result.ok !== true || result.opened !== true) {
        var reason = String(result && result.error || 'AnkiMobile 未安装或无法打开');
        try {
          await recordFailure(repo, gid, cardIndex, id, reason);
          pending.delete(pendingKey);
          scheduleExpiry();
        } catch (recordError) {
          log('记录打开失败状态失败：' +
            String(recordError && recordError.message || recordError));
        }
        throw new ExportError(reason, 'BW_ANKIMOBILE_OPEN_FAILED');
      }
      return {
        ok: true,
        status: 'pending',
        gid: gid,
        index: cardIndex,
        callbackExpected: true
      };
    }

    async function requestSync() {
      if (arguments.length !== 0) {
        throw new ExportError('requestSync 不接受参数', 'BW_ANKIMOBILE_INPUT');
      }
      if (!available()) {
        throw new ExportError(
          'AnkiMobile 同步仅在 BWReader App 内可请求',
          'BW_ANKIMOBILE_UNAVAILABLE'
        );
      }
      var result;
      try {
        result = await bridge().request({
          action: 'sync',
          url: 'anki://x-callback-url/sync'
        });
      } catch (error) {
        throw new ExportError(
          '无法请求 AnkiMobile 同步：' + String(error && error.message || error),
          'BW_ANKIMOBILE_SYNC_OPEN_FAILED'
        );
      }
      if (!result || result.ok !== true || result.opened !== true) {
        throw new ExportError(
          String(result && result.error || 'AnkiMobile 未安装或无法打开'),
          'BW_ANKIMOBILE_SYNC_OPEN_FAILED'
        );
      }
      // The official scheme only requests the same action as tapping Sync; it
      // does not provide a completion result, so never report success here.
      return { ok: true, status: 'requested' };
    }

    async function handleNativeCallback(detail) {
      try {
        exactKeys(detail, {
          status: true, gid: true, index: true, nonce: true
        }, 'callback');
      } catch (_) { return { ok: false, durable: false }; }
      if (detail.status !== 'succeeded') return { ok: false, durable: false };
      var gid;
      var cardIndex;
      var nonce;
      try {
        gid = gidOf(detail.gid);
        cardIndex = indexOf(detail.index);
        rejectNul(detail.nonce, 'nonce');
        nonce = String(detail.nonce);
        if (!NONCE_RE.test(nonce)) throw new Error('invalid nonce');
      } catch (_) { return { ok: false, durable: false }; }
      try { await ensureRestored(); }
      catch (_) { return { ok: false, durable: false }; }
      await expirePending();
      var pendingKey = keyOf(gid, cardIndex);
      var entry = pending.get(pendingKey);
      if (!entry) {
        var persisted;
        try { persisted = await repository().load(gid); }
        catch (_) { return { ok: false, durable: false }; }
        if (succeededReceiptMatches(persisted, cardIndex, nonce)) {
          return { ok: true, durable: true };
        }
        try { entry = pendingReceipt(persisted, cardIndex); }
        catch (_) { entry = null; }
        if (entry) pending.set(pendingKey, entry);
      }
      if (!entry || entry.nonce !== nonce || entry.settling ||
          entry.expiresAt <= Number(clock())) {
        await expirePending();
        return { ok: false, durable: false };
      }
      entry.settling = 'callback';
      var at = Number(clock());
      try {
        await repository().recordAnkiReceipt(gid, cardIndex, TARGET, {
          status: 'succeeded',
          mutationId: entry.attemptId,
          exportedAt: at,
          updatedAt: at,
          error: '',
          detail: {
            channel: 'x-callback-url',
            callbackExpected: true,
            callbackReceived: true,
            callbackNonce: entry.nonce,
            callbackExpiresAt: entry.expiresAt
          }
        }, { mutationId: receiptMutation('succeeded', gid, cardIndex, at) });
        if (pending.get(pendingKey) === entry) pending.delete(pendingKey);
        scheduleExpiry();
        log('AnkiMobile 已确认加入卡片');
        return { ok: true, durable: true };
      } catch (error) {
        entry.settling = '';
        scheduleExpiry();
        log('AnkiMobile 回调已验证，但本地回执写入失败：' +
          String(error && error.message || error));
        return { ok: false, durable: false };
      }
    }

    if (environment && typeof environment.addEventListener === 'function') {
      environment.addEventListener('bw-native-anki-mobile-callback', function (event) {
        handleNativeCallback(event && event.detail).catch(function (error) {
          log('处理 AnkiMobile 回调失败：' + String(error && error.message || error));
        });
      });
      environment.addEventListener('bw-native-reader-foreground', function (event) {
        if (!event || !event.detail || event.detail.active !== true) return;
        ensureRestored().then(expirePending).catch(function (error) {
          log('前台恢复 pending 失败：' + String(error && error.message || error));
        });
      });
    }
    if (environment && environment.document &&
        typeof environment.document.addEventListener === 'function') {
      environment.document.addEventListener('visibilitychange', function () {
        if (environment.document.visibilityState !== 'visible') return;
        ensureRestored().then(expirePending).catch(function (error) {
          log('页面恢复 pending 失败：' + String(error && error.message || error));
        });
      });
    }
    // 开页第一拍卡仓可能还没接上本地存储(2026-09-03 App 日志:「本地卡仓存储缺少 get」比「连接本地状态」早 60ms),
    // 失败一次就放弃会让 pending 回执一直等到下次前台切换;这里对"仓未就绪"类错误短退避重试几次。
    var restoreAttempt = 0;
    function restoreAtStartup() {
      return ensureRestored().catch(function (error) {
        var code = String(error && error.code || '');
        var unavailable = code === 'BW_CARD_REPOSITORY_UNAVAILABLE' ||
          code === 'BW_ANKIMOBILE_REPOSITORY_UNAVAILABLE';
        if (unavailable && restoreAttempt < 6) {
          restoreAttempt += 1;
          return new Promise(function (resolve) {
            setTimeout(resolve, 400 * restoreAttempt);
          }).then(restoreAtStartup);
        }
        log('恢复 AnkiMobile pending 失败：' + String(error && error.message || error) +
          (restoreAttempt ? '（已重试 ' + restoreAttempt + ' 次）' : ''));
      });
    }
    Promise.resolve().then(restoreAtStartup);

    return Object.freeze({
      contract: CONTRACT,
      available: available,
      exportCard: exportCard,
      requestSync: requestSync,
      handleNativeCallback: handleNativeCallback
    });
  }

  var defaultInstance = createAnkiMobileExport({});
  return Object.freeze({
    CONTRACT: CONTRACT,
    TARGET: TARGET,
    MAX_URL_BYTES: MAX_URL_BYTES,
    AnkiMobileExportError: ExportError,
    createAnkiMobileExport: createAnkiMobileExport,
    available: defaultInstance.available,
    exportCard: defaultInstance.exportCard,
    requestSync: defaultInstance.requestSync,
    handleNativeCallback: defaultInstance.handleNativeCallback
  });
});
