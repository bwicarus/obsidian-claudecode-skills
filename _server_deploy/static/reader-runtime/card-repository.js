/* card-repository.js — Reader 卡片的共享、本地权威仓库。
 *
 * 一个 card_* (id === cid === gid) 表示现有 Reader 语义中的一批 cards；批内
 * 卡片状态按稳定 index 保存。card-entities 只放内容与来源，card-states 只放
 * 可变状态。Anki note/card id 只能出现在某个 index 的 projection receipt 中，
 * 不得成为 Reader 的业务身份。底层 DataStore 负责 revision、mutation receipt、
 * journal 与 tombstone；本层用 batch 保证两个 collection 原子变化。
 */
(function (root, factory) {
  var api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.BWReaderRuntime = root.BWReaderRuntime || {};
  root.BWReaderRuntime.cardRepository = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function (root) {
  'use strict';

  var CONTRACT = 'card-repository/1';
  var ENTITY_CONTRACT = 'card-entity/1';
  var STATE_CONTRACT = 'card-state/1';
  var ENTITY_COLLECTION = 'card-entities';
  var STATE_COLLECTION = 'card-states';
  var RECORD_SCHEMA = 1;
  var CARD_ID_RE = /^card_[a-f0-9]{4,64}$/;
  var SAFE_KEY_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/;
  var MAX_CARDS = 256;
  var MAX_TAGS = 32;
  var MAX_MUTATION_BYTES = 470;
  var MAX_SOURCE_BYTES = 128 * 1024;
  var MAX_ENTITY_BYTES = 2 * 1024 * 1024;
  var CARD_FIELDS = {
    type: true, front: true, back: true, cloze: true, text: true,
    deck: true, tags: true, reason: true
  };
  var SOURCE_FIELDS = {
    kind: true, sourceId: true, documentId: true, bookId: true, url: true,
    title: true, quote: true, context: true, tool: true, draftId: true,
    sourceInstanceId: true, requirement: true, location: true, anchor: true,
    selection: true, legacy: true
  };
  var SOURCE_LIMITS = {
    kind: 80, sourceId: 4096, documentId: 4096, bookId: 4096,
    url: 8192, title: 1024, quote: 32768, context: 65536, tool: 160,
    draftId: 512, sourceInstanceId: 512, requirement: 32768
  };
  var REVIEW_FIELDS = {
    status: true, dueAt: true, lastReviewedAt: true, intervalDays: true,
    ease: true, reps: true, lapses: true
  };
  var REVIEW_STATUS = {
    unavailable: true, new: true, learning: true, review: true,
    relearning: true, suspended: true, buried: true
  };
  var RECEIPT_STATUS = {
    pending: true, succeeded: true, failed: true, unknown: true
  };

  function CardRepositoryError(message, code, details) {
    this.name = 'CardRepositoryError';
    this.code = code || 'BW_CARD_REPOSITORY';
    this.message = String(message || '卡片仓库错误');
    this.details = details || null;
    if (Error.captureStackTrace) Error.captureStackTrace(this, CardRepositoryError);
  }
  CardRepositoryError.prototype = Object.create(Error.prototype);
  CardRepositoryError.prototype.constructor = CardRepositoryError;

  function own(value, key) {
    return Object.prototype.hasOwnProperty.call(value || {}, key);
  }
  function plain(value) {
    if (!value || Object.prototype.toString.call(value) !== '[object Object]') return false;
    var proto = Object.getPrototypeOf(value);
    return proto === Object.prototype || proto === null;
  }
  function clone(value) {
    return value == null ? value : JSON.parse(JSON.stringify(value));
  }
  function utf8Bytes(value) {
    value = String(value == null ? '' : value);
    if (typeof TextEncoder !== 'undefined') return new TextEncoder().encode(value).byteLength;
    return unescape(encodeURIComponent(value)).length;
  }
  function rejectNul(value, label) {
    if (String(value).indexOf('\u0000') >= 0) {
      throw new CardRepositoryError(label + ' 含 NUL', 'BW_CARD_REPOSITORY_INPUT', {
        field: label
      });
    }
  }
  function text(value, label, maximum, required) {
    value = String(value == null ? '' : value).replace(/\r\n?/g, '\n').trim();
    rejectNul(value, label);
    if ((required && !value) || utf8Bytes(value) > maximum) {
      throw new CardRepositoryError(
        label + (required && !value ? ' 不能为空' : ' 超出长度上限'),
        'BW_CARD_REPOSITORY_INPUT',
        { field: label, maximumBytes: maximum }
      );
    }
    return value;
  }
  function integer(value, label, minimum, nullable) {
    if (nullable && value == null) return null;
    var number = Number(value);
    if (!Number.isSafeInteger(number) || number < minimum) {
      throw new CardRepositoryError(label + ' 无效', 'BW_CARD_REPOSITORY_INPUT');
    }
    return number;
  }
  function number(value, label, minimum) {
    var result = Number(value);
    if (!Number.isFinite(result) || result < minimum) {
      throw new CardRepositoryError(label + ' 无效', 'BW_CARD_REPOSITORY_INPUT');
    }
    return result;
  }
  function allowedFields(value, allowed, label) {
    Object.keys(value || {}).forEach(function (key) {
      if (!allowed[key]) {
        throw new CardRepositoryError(
          label + ' 含未声明字段：' + key,
          'BW_CARD_REPOSITORY_INPUT',
          { field: label + '.' + key }
        );
      }
    });
  }
  function assertJSON(value, label, seen) {
    label = label || 'value';
    if (value === null || typeof value === 'boolean') return;
    if (typeof value === 'string') { rejectNul(value, label); return; }
    if (typeof value === 'number') {
      if (!Number.isFinite(value)) {
        throw new CardRepositoryError(label + ' 含非有限数字', 'BW_CARD_REPOSITORY_INPUT');
      }
      return;
    }
    if (typeof value !== 'object' || (!Array.isArray(value) && !plain(value))) {
      throw new CardRepositoryError(label + ' 必须是 JSON 值', 'BW_CARD_REPOSITORY_INPUT');
    }
    seen = seen || [];
    if (seen.indexOf(value) >= 0) {
      throw new CardRepositoryError(label + ' 含循环引用', 'BW_CARD_REPOSITORY_INPUT');
    }
    seen.push(value);
    if (Array.isArray(value)) {
      for (var i = 0; i < value.length; i += 1) {
        if (!own(value, i)) {
          throw new CardRepositoryError(label + ' 含数组空洞', 'BW_CARD_REPOSITORY_INPUT');
        }
        assertJSON(value[i], label + '[' + i + ']', seen);
      }
    } else {
      Object.keys(value).forEach(function (key) {
        rejectNul(key, label + ' key');
        assertJSON(value[key], label + '.' + key, seen);
      });
    }
    seen.pop();
  }
  function boundedJSON(value, label, maximum) {
    assertJSON(value, label);
    var bytes = utf8Bytes(JSON.stringify(value));
    if (bytes > maximum) {
      throw new CardRepositoryError(label + ' 超出大小上限', 'BW_CARD_REPOSITORY_INPUT', {
        actualBytes: bytes,
        maximumBytes: maximum
      });
    }
  }
  function same(left, right) {
    return JSON.stringify(left) === JSON.stringify(right);
  }

  function secureId() {
    var cryptoApi = root.crypto;
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
      throw new CardRepositoryError(
        '无法安全生成本地卡组编号',
        'BW_CARD_REPOSITORY_RANDOM'
      );
    }
    return 'card_' + hex;
  }
  function normalizeId(value) {
    value = String(value == null ? '' : value).trim().toLowerCase();
    rejectNul(value, 'cardId');
    if (!CARD_ID_RE.test(value)) {
      throw new CardRepositoryError(
        '卡组编号必须是稳定的 card_ 十六进制编号',
        'BW_CARD_REPOSITORY_ID',
        { id: value }
      );
    }
    return value;
  }
  function identityOf(input, idFactory, generate) {
    input = input || {};
    var ids = ['id', 'cid', 'gid', 'cardId'].filter(function (key) {
      return input[key] != null && String(input[key]).trim();
    }).map(function (key) { return normalizeId(input[key]); });
    if (!ids.length) {
      if (!generate) {
        throw new CardRepositoryError('缺少 card_* gid', 'BW_CARD_REPOSITORY_ID');
      }
      return normalizeId(typeof idFactory === 'function' ? idFactory() : secureId());
    }
    if (ids.some(function (id) { return id !== ids[0]; })) {
      throw new CardRepositoryError(
        'id/cid/gid 必须指向同一个 Reader 卡组实体',
        'BW_CARD_REPOSITORY_ID'
      );
    }
    return ids[0];
  }
  function normalizeTags(value) {
    if (value == null) return [];
    if (!Array.isArray(value) || value.length > MAX_TAGS) {
      throw new CardRepositoryError('tags 必须是有限数组', 'BW_CARD_REPOSITORY_INPUT');
    }
    var result = [];
    value.forEach(function (item) {
      item = text(item, 'tags[]', 128, true);
      if (/\s/.test(item)) {
        throw new CardRepositoryError('tag 不得含空白', 'BW_CARD_REPOSITORY_INPUT');
      }
      if (result.indexOf(item) < 0) result.push(item);
    });
    return result.sort();
  }
  function normalizeCard(value) {
    if (!plain(value)) {
      throw new CardRepositoryError('card 必须是对象', 'BW_CARD_REPOSITORY_INPUT');
    }
    allowedFields(value, CARD_FIELDS, 'card');
    var type = text(value.type, 'card.type', 32, true).toLowerCase();
    var output = { type: type };
    if (type === 'basic') {
      if (value.cloze != null || value.text != null) {
        throw new CardRepositoryError(
          'basic 卡不得携带 cloze/text',
          'BW_CARD_REPOSITORY_CARD_SHAPE'
        );
      }
      output.front = text(value.front, 'card.front', 32768, true);
      output.back = text(value.back, 'card.back', 65536, true);
    } else if (type === 'cloze') {
      if (value.front != null || value.back != null) {
        throw new CardRepositoryError(
          'cloze 卡不得携带 front/back',
          'BW_CARD_REPOSITORY_CARD_SHAPE'
        );
      }
      if (value.cloze != null && value.text != null) {
        throw new CardRepositoryError(
          'cloze 卡不得同时携带 cloze/text',
          'BW_CARD_REPOSITORY_CARD_SHAPE'
        );
      }
      output.cloze = text(
        value.cloze != null ? value.cloze : value.text,
        'card.cloze',
        98304,
        true
      );
      if (!/\{\{c[1-9]\d*::[\s\S]+?\}\}/.test(output.cloze)) {
        throw new CardRepositoryError(
          'cloze 卡至少需要一个 {{c1::…}} 挖空',
          'BW_CARD_REPOSITORY_CARD_SHAPE'
        );
      }
    } else {
      throw new CardRepositoryError(
        'Reader 本地卡仓只接受 basic 或 cloze',
        'BW_CARD_REPOSITORY_CARD_SHAPE'
      );
    }
    if (value.deck != null) output.deck = text(value.deck, 'card.deck', 512, false);
    if (value.reason != null) output.reason = text(value.reason, 'card.reason', 4096, false);
    if (value.tags != null) output.tags = normalizeTags(value.tags);
    return output;
  }
  function normalizeCards(value) {
    if (!Array.isArray(value) || !value.length || value.length > MAX_CARDS) {
      throw new CardRepositoryError(
        'cards 必须包含 1-' + MAX_CARDS + ' 张卡',
        'BW_CARD_REPOSITORY_CARD_SHAPE'
      );
    }
    return value.map(normalizeCard);
  }
  function cardsOf(input, required) {
    if (Array.isArray(input && input.cards)) return normalizeCards(input.cards);
    if (plain(input && input.card)) return [normalizeCard(input.card)];
    if (required) {
      throw new CardRepositoryError('缺少 cards', 'BW_CARD_REPOSITORY_CARD_SHAPE');
    }
    return null;
  }
  function normalizeSource(value) {
    if (!plain(value)) {
      throw new CardRepositoryError(
        'source 必须是带稳定来源的对象',
        'BW_CARD_REPOSITORY_SOURCE'
      );
    }
    allowedFields(value, SOURCE_FIELDS, 'source');
    var output = {};
    Object.keys(SOURCE_LIMITS).forEach(function (key) {
      if (value[key] != null) {
        output[key] = text(value[key], 'source.' + key, SOURCE_LIMITS[key], key === 'kind');
      }
    });
    if (!output.kind) {
      throw new CardRepositoryError('source.kind 不能为空', 'BW_CARD_REPOSITORY_SOURCE');
    }
    ['location', 'anchor', 'selection', 'legacy'].forEach(function (key) {
      if (value[key] == null) return;
      if (!plain(value[key])) {
        throw new CardRepositoryError(
          'source.' + key + ' 必须是对象',
          'BW_CARD_REPOSITORY_SOURCE'
        );
      }
      assertJSON(value[key], 'source.' + key);
      output[key] = clone(value[key]);
    });
    if (![
      output.sourceId, output.documentId, output.bookId, output.url,
      output.draftId, output.sourceInstanceId
    ].some(Boolean)) {
      throw new CardRepositoryError(
        'source 至少需要一个稳定来源编号或文档地址',
        'BW_CARD_REPOSITORY_SOURCE'
      );
    }
    boundedJSON(output, 'source', MAX_SOURCE_BYTES);
    return output;
  }

  function defaultReview(phase) {
    return {
      status: phase === 'confirmed' ? 'new' : 'unavailable',
      dueAt: null,
      lastReviewedAt: null,
      intervalDays: 0,
      ease: 0,
      reps: 0,
      lapses: 0
    };
  }
  function normalizeExactState(value) {
    if (value == null) return {};
    if (!plain(value)) {
      throw new CardRepositoryError(
        'exactState 必须是旧 rc-flashcard 状态对象',
        'BW_CARD_REPOSITORY_STATE'
      );
    }
    boundedJSON(value, 'exactState', 128 * 1024);
    return clone(value);
  }
  function normalizeReview(value, previous) {
    if (value == null) return clone(previous || defaultReview('draft'));
    if (!plain(value)) {
      throw new CardRepositoryError('review 必须是对象', 'BW_CARD_REPOSITORY_STATE');
    }
    allowedFields(value, REVIEW_FIELDS, 'review');
    var output = Object.assign({}, previous || defaultReview('draft'));
    if (own(value, 'status')) {
      var status = text(value.status, 'review.status', 32, true).toLowerCase();
      if (!REVIEW_STATUS[status]) {
        throw new CardRepositoryError('review.status 无效', 'BW_CARD_REPOSITORY_STATE');
      }
      output.status = status;
    }
    ['dueAt', 'lastReviewedAt'].forEach(function (key) {
      if (own(value, key)) output[key] = integer(value[key], 'review.' + key, 0, true);
    });
    ['reps', 'lapses'].forEach(function (key) {
      if (own(value, key)) output[key] = integer(value[key], 'review.' + key, 0, false);
    });
    ['intervalDays', 'ease'].forEach(function (key) {
      if (own(value, key)) output[key] = number(value[key], 'review.' + key, 0);
    });
    return output;
  }
  function normalizeFlags(value, previous) {
    var output = Object.assign({ favorite: false, archived: false }, previous || {});
    if (value == null) return output;
    if (!plain(value)) {
      throw new CardRepositoryError('flags 必须是对象', 'BW_CARD_REPOSITORY_STATE');
    }
    allowedFields(value, { favorite: true, archived: true }, 'flags');
    ['favorite', 'archived'].forEach(function (key) {
      if (!own(value, key)) return;
      if (typeof value[key] !== 'boolean') {
        throw new CardRepositoryError('flags.' + key + ' 必须是布尔值', 'BW_CARD_REPOSITORY_STATE');
      }
      output[key] = value[key];
    });
    return output;
  }
  function externalId(value, label) {
    if (typeof value === 'number') {
      if (!Number.isSafeInteger(value) || value < 0) {
        throw new CardRepositoryError(label + ' 无效', 'BW_CARD_REPOSITORY_RECEIPT');
      }
      return value;
    }
    if (typeof value === 'string') return text(value, label, 256, true);
    throw new CardRepositoryError(label + ' 无效', 'BW_CARD_REPOSITORY_RECEIPT');
  }
  function normalizeReceipt(value, previous) {
    if (!plain(value)) {
      throw new CardRepositoryError('Anki receipt 必须是对象', 'BW_CARD_REPOSITORY_RECEIPT');
    }
    allowedFields(value, {
      status: true, mutationId: true, noteIds: true, cardIds: true,
      exportedAt: true, updatedAt: true, error: true, detail: true
    }, 'ankiReceipt');
    var output = Object.assign({
      status: 'pending', mutationId: '', noteIds: [], cardIds: [],
      exportedAt: null, updatedAt: null, error: ''
    }, clone(previous || {}));
    if (own(value, 'status')) {
      var status = text(value.status, 'ankiReceipt.status', 32, true).toLowerCase();
      if (!RECEIPT_STATUS[status]) {
        throw new CardRepositoryError('ankiReceipt.status 无效', 'BW_CARD_REPOSITORY_RECEIPT');
      }
      output.status = status;
    }
    if (own(value, 'mutationId')) {
      output.mutationId = text(value.mutationId, 'ankiReceipt.mutationId', 512, false);
    }
    ['noteIds', 'cardIds'].forEach(function (key) {
      if (!own(value, key)) return;
      if (!Array.isArray(value[key]) || value[key].length > 128) {
        throw new CardRepositoryError('ankiReceipt.' + key + ' 无效', 'BW_CARD_REPOSITORY_RECEIPT');
      }
      output[key] = value[key].map(function (item, index) {
        return externalId(item, 'ankiReceipt.' + key + '[' + index + ']');
      });
    });
    ['exportedAt', 'updatedAt'].forEach(function (key) {
      if (own(value, key)) output[key] = integer(value[key], 'ankiReceipt.' + key, 0, true);
    });
    if (own(value, 'error')) output.error = text(value.error, 'ankiReceipt.error', 4096, false);
    if (own(value, 'detail')) {
      boundedJSON(value.detail, 'ankiReceipt.detail', 16384);
      output.detail = clone(value.detail);
    }
    return output;
  }
  function normalizeProjections(value, previous) {
    var output = clone(previous || { anki: {} });
    if (!plain(output.anki)) output.anki = {};
    if (value == null) return output;
    if (!plain(value)) {
      throw new CardRepositoryError('projections 必须是对象', 'BW_CARD_REPOSITORY_STATE');
    }
    allowedFields(value, { anki: true }, 'projections');
    if (value.anki != null) {
      if (!plain(value.anki)) {
        throw new CardRepositoryError('projections.anki 必须是对象', 'BW_CARD_REPOSITORY_STATE');
      }
      Object.keys(value.anki).forEach(function (target) {
        if (!SAFE_KEY_RE.test(target)) {
          throw new CardRepositoryError('Anki target 无效', 'BW_CARD_REPOSITORY_RECEIPT');
        }
        output.anki[target] = normalizeReceipt(value.anki[target], output.anki[target]);
      });
    }
    return output;
  }
  function cardState(phase, confirmedAt, review, flags, projections, exactState, removed) {
    if (phase !== 'draft' && phase !== 'confirmed') {
      throw new CardRepositoryError('phase 无效', 'BW_CARD_REPOSITORY_STATE');
    }
    if (removed != null && typeof removed !== 'boolean') {
      throw new CardRepositoryError('state.removed 必须是布尔值', 'BW_CARD_REPOSITORY_STATE');
    }
    return {
      phase: phase,
      removed: removed === true,
      confirmedAt: phase === 'confirmed'
        ? integer(confirmedAt, 'state.confirmedAt', 0, false)
        : null,
      review: normalizeReview(review, defaultReview(phase)),
      flags: normalizeFlags(flags),
      projections: normalizeProjections(projections),
      exactState: normalizeExactState(exactState)
    };
  }
  function draftState() {
    return cardState('draft', null, null, null, null);
  }
  function normalizeStateMap(value, cardCount) {
    if (!plain(value)) {
      throw new CardRepositoryError('states 必须是按 index 的对象', 'BW_CARD_REPOSITORY_STATE');
    }
    var keys = Object.keys(value);
    if (keys.length !== cardCount) {
      throw new CardRepositoryError('states 数量与 cards 不一致', 'BW_CARD_REPOSITORY_CORRUPT');
    }
    var output = {};
    for (var index = 0; index < cardCount; index += 1) {
      var key = String(index);
      var current = value[key];
      if (!plain(current)) {
        throw new CardRepositoryError('缺少 states.' + key, 'BW_CARD_REPOSITORY_CORRUPT');
      }
      output[key] = cardState(
        current.phase,
        current.confirmedAt,
        current.review,
        current.flags,
        current.projections,
        current.exactState,
        current.removed
      );
    }
    return output;
  }
  function freshStateMap(cardCount) {
    var result = {};
    for (var index = 0; index < cardCount; index += 1) result[String(index)] = draftState();
    return result;
  }
  function entityValue(id, cards, source, createdAt, contentUpdatedAt) {
    var output = {
      contract: ENTITY_CONTRACT,
      schema: RECORD_SCHEMA,
      id: id,
      cid: id,
      gid: id,
      cards: normalizeCards(cards),
      source: normalizeSource(source),
      createdAt: integer(createdAt, 'entity.createdAt', 0, false),
      contentUpdatedAt: integer(contentUpdatedAt, 'entity.contentUpdatedAt', 0, false)
    };
    boundedJSON(output, 'card entity', MAX_ENTITY_BYTES);
    return output;
  }
  function stateValue(id, states, cardCount) {
    return {
      contract: STATE_CONTRACT,
      schema: RECORD_SCHEMA,
      id: id,
      cid: id,
      gid: id,
      states: normalizeStateMap(states, cardCount)
    };
  }
  function legacyProjection(exact, timestamp) {
    var noteIds = exact._nid == null ? [] : [externalId(exact._nid, 'legacy._nid')];
    var rawCardId = exact.card_id != null ? exact.card_id : exact.id;
    var cardIds = rawCardId == null ? [] : [externalId(rawCardId, 'legacy.card_id')];
    if (!noteIds.length && !cardIds.length) return { anki: {} };
    return {
      anki: {
        'pi-legacy': normalizeReceipt({
          status: 'succeeded',
          noteIds: noteIds,
          cardIds: cardIds,
          exportedAt: timestamp || null,
          updatedAt: timestamp || null
        })
      }
    };
  }
  function legacyState(exact, timestamp) {
    exact = normalizeExactState(exact);
    var phase = exact._st && exact._st !== 'draft' ? 'confirmed' : 'draft';
    var review = defaultReview(phase);
    if (phase === 'confirmed') {
      review.status = exact._st === 'done' ? 'review' : 'learning';
    }
    return cardState(
      phase,
      phase === 'confirmed' ? (timestamp || 1) : null,
      review,
      null,
      legacyProjection(exact, timestamp),
      exact
    );
  }
  function legacySource(record, id) {
    var ref = text(
      record.source_ref != null ? record.source_ref : record.src,
      'legacy.source_ref',
      8192,
      false
    );
    var legacy = {};
    Object.keys(record).forEach(function (key) {
      if ({
        id: true, cid: true, gid: true, kind: true, cards: true,
        data: true, states: true, source_ref: true, src: true, req: true
      }[key]) return;
      legacy[key] = clone(record[key]);
    });
    if (ref) legacy.source_ref = ref;
    if (record.req != null) legacy.req = clone(record.req);
    var output = {
      kind: 'pi-legacy-card-registry',
      sourceId: ref || ('pi-card-registry:' + id)
    };
    if (record.req != null) {
      output.requirement = typeof record.req === 'string'
        ? record.req
        : JSON.stringify(record.req);
    }
    if (Object.keys(legacy).length) output.legacy = legacy;
    return normalizeSource(output);
  }
  function normalizeLegacyRecord(record) {
    if (!plain(record)) {
      throw new CardRepositoryError('legacy record 必须是对象', 'BW_CARD_REPOSITORY_LEGACY');
    }
    var id = identityOf(record, null, false);
    if (record.kind != null && String(record.kind) !== 'cards') {
      throw new CardRepositoryError('legacy record.kind 不是 cards', 'BW_CARD_REPOSITORY_LEGACY');
    }
    var cards = normalizeCards(Array.isArray(record.cards) ? record.cards : record.data);
    var rawStates = record.states == null ? {} : record.states;
    if (!plain(rawStates)) {
      throw new CardRepositoryError('legacy states 必须是对象', 'BW_CARD_REPOSITORY_LEGACY');
    }
    var timestamp = 0;
    if (record.ts != null) {
      timestamp = Number(record.ts);
      if (!Number.isFinite(timestamp) || timestamp < 0) {
        throw new CardRepositoryError('legacy ts 无效', 'BW_CARD_REPOSITORY_LEGACY');
      }
      if (timestamp < 100000000000) timestamp = Math.round(timestamp * 1000);
      timestamp = integer(timestamp, 'legacy ts', 0, false);
    }
    var states = freshStateMap(cards.length);
    Object.keys(rawStates).forEach(function (key) {
      if (!/^(?:0|[1-9]\d*)$/.test(key)) {
        throw new CardRepositoryError('legacy state index 无效', 'BW_CARD_REPOSITORY_LEGACY');
      }
      var index = Number(key);
      if (index >= cards.length) {
        throw new CardRepositoryError('legacy state index 超出 cards', 'BW_CARD_REPOSITORY_LEGACY');
      }
      states[key] = legacyState(rawStates[key], timestamp);
    });
    return {
      id: id,
      cards: cards,
      source: legacySource(record, id),
      states: states,
      timestamp: timestamp
    };
  }
  function emptyObject(value) {
    return plain(value) && Object.keys(value).length === 0;
  }
  function mergeProjections(base, addition) {
    var output = normalizeProjections(base);
    addition = normalizeProjections(addition);
    Object.keys(addition.anki).forEach(function (target) {
      if (!own(output.anki, target)) output.anki[target] = clone(addition.anki[target]);
    });
    return output;
  }
  function storedIdentity(value, expected, contract, label) {
    if (!plain(value) || value.contract !== contract || value.schema !== RECORD_SCHEMA) {
      throw new CardRepositoryError(label + ' schema 损坏', 'BW_CARD_REPOSITORY_CORRUPT');
    }
    if (identityOf(value, null, false) !== expected) {
      throw new CardRepositoryError(label + ' 身份不一致', 'BW_CARD_REPOSITORY_CORRUPT');
    }
  }
  function ensureStore(store) {
    ['get', 'list', 'put', 'remove', 'batch', 'subscribe'].forEach(function (method) {
      if (!store || typeof store[method] !== 'function') {
        throw new CardRepositoryError(
          '本地卡仓存储缺少 ' + method,
          'BW_CARD_REPOSITORY_UNAVAILABLE'
        );
      }
    });
    return store;
  }
  function mutationBase(value, operation, id, factory) {
    if (value != null) {
      value = String(value).trim();
      rejectNul(value, 'mutationId');
      if (!value || utf8Bytes(value) > MAX_MUTATION_BYTES) {
        throw new CardRepositoryError('mutationId 无效', 'BW_CARD_REPOSITORY_MUTATION');
      }
      return value;
    }
    var suffix = typeof factory === 'function'
      ? String(factory())
      : Date.now().toString(36) + Math.random().toString(36).slice(2, 14);
    rejectNul(suffix, 'mutationId');
    return 'cardrepo:' + operation + ':' + id + ':' + suffix;
  }

  function createCardRepository(options) {
    options = options || {};
    var store = options.store;
    if (!store && root.__BW_READER_RUNTIME__ &&
        typeof root.__BW_READER_RUNTIME__.storage === 'function') {
      store = root.__BW_READER_RUNTIME__.storage();
    }
    store = ensureStore(store);
    var idFactory = options.idFactory;
    var mutationFactory = options.mutationFactory;
    var clock = typeof options.clock === 'function' ? options.clock : Date.now;
    var queue = Promise.resolve();

    function serialize(work) {
      var result = queue.then(work);
      queue = result.catch(function () {});
      return result;
    }
    function now() { return integer(clock(), 'timestamp', 0, false); }
    function newCardId() { return identityOf({}, idFactory, true); }
    function pair(id) {
      return Promise.all([
        store.get(ENTITY_COLLECTION, id, { includeDeleted: true }),
        store.get(STATE_COLLECTION, id, { includeDeleted: true })
      ]).then(function (records) { return { entity: records[0], state: records[1] }; });
    }
    function revision(record, supplied, label) {
      if (supplied != null) supplied = integer(supplied, label, 0, false);
      var actual = Number(record && record.rev || 0);
      if (supplied != null && supplied !== actual) {
        throw new CardRepositoryError(label + ' 与当前版本不一致', 'BW_CARD_REPOSITORY_CONFLICT', {
          expectedRev: supplied,
          actualRev: actual
        });
      }
      return actual;
    }
    function rejectDeleted(records, id) {
      if (records.entity && records.entity.deleted || records.state && records.state.deleted) {
        throw new CardRepositoryError(
          '卡组已删除，不能隐式复活：' + id,
          'BW_CARD_REPOSITORY_TOMBSTONED',
          { id: id }
        );
      }
    }
    function project(records, includeDeleted) {
      var entity = records && records.entity;
      var state = records && records.state;
      if (!entity && !state) return null;
      if (!entity || !state) {
        throw new CardRepositoryError('卡组内容与状态不完整', 'BW_CARD_REPOSITORY_PARTIAL');
      }
      var id = normalizeId(entity.id);
      if (normalizeId(state.id) !== id || entity.deleted !== state.deleted) {
        throw new CardRepositoryError('卡组 collection 身份或墓碑不一致', 'BW_CARD_REPOSITORY_PARTIAL');
      }
      if (entity.deleted) {
        return includeDeleted ? {
          contract: CONTRACT,
          id: id, cid: id, gid: id, deleted: true,
          entityRev: Number(entity.rev) || 0,
          stateRev: Number(state.rev) || 0
        } : null;
      }
      storedIdentity(entity.value, id, ENTITY_CONTRACT, ENTITY_COLLECTION);
      storedIdentity(state.value, id, STATE_CONTRACT, STATE_COLLECTION);
      var normalizedEntity = entityValue(
        id,
        entity.value.cards,
        entity.value.source,
        entity.value.createdAt,
        entity.value.contentUpdatedAt
      );
      var normalizedState = stateValue(id, state.value.states, normalizedEntity.cards.length);
      return {
        contract: CONTRACT,
        id: id,
        cid: id,
        gid: id,
        deleted: false,
        entityRev: Number(entity.rev) || 0,
        stateRev: Number(state.rev) || 0,
        cards: clone(normalizedEntity.cards),
        source: clone(normalizedEntity.source),
        states: clone(normalizedState.states),
        createdAt: normalizedEntity.createdAt,
        contentUpdatedAt: normalizedEntity.contentUpdatedAt
      };
    }
    function verifyWrite(record, expected, label) {
      if (!record || record.deleted || !same(record.value, expected)) {
        throw new CardRepositoryError(
          label + ' mutationId 已被不同内容使用',
          'BW_CARD_REPOSITORY_MUTATION_REUSED'
        );
      }
      return record;
    }
    function batchPut(id, records, entity, state, operation, operationOptions) {
      operationOptions = operationOptions || {};
      var base = mutationBase(operationOptions.mutationId, operation, id, mutationFactory);
      return store.batch([
        {
          collection: ENTITY_COLLECTION,
          value: entity,
          options: {
            id: id,
            ifRev: revision(records.entity, operationOptions.ifEntityRev, 'ifEntityRev'),
            mutationId: base + ':entity'
          }
        },
        {
          collection: STATE_COLLECTION,
          value: state,
          options: {
            id: id,
            ifRev: revision(records.state, operationOptions.ifStateRev, 'ifStateRev'),
            mutationId: base + ':state'
          }
        }
      ]).then(function (written) {
        return project({
          entity: verifyWrite(written[0], entity, ENTITY_COLLECTION),
          state: verifyWrite(written[1], state, STATE_COLLECTION)
        }, false);
      });
    }

    function registerDraft(input, operationOptions) {
      input = input || {};
      var id;
      var cards;
      var source;
      try {
        id = identityOf(input, idFactory, true);
        cards = cardsOf(input, true);
        source = normalizeSource(input.source);
      } catch (error) { return Promise.reject(error); }
      return serialize(function () {
        return pair(id).then(function (records) {
          rejectDeleted(records, id);
          if (!!records.entity !== !!records.state) {
            throw new CardRepositoryError(
              '草稿注册遇到半条本地卡组',
              'BW_CARD_REPOSITORY_PARTIAL'
            );
          }
          var previousEntity = records.entity && records.entity.value;
          var previousState = records.state && records.state.value;
          if (previousEntity && previousState) {
            storedIdentity(previousEntity, id, ENTITY_CONTRACT, ENTITY_COLLECTION);
            storedIdentity(previousState, id, STATE_CONTRACT, STATE_COLLECTION);
            normalizeStateMap(
              previousState.states,
              previousEntity.cards && previousEntity.cards.length
            );
            var expectedDraftId = String(source.draftId || '');
            var storedDraftId = String(previousEntity.source &&
              previousEntity.source.draftId || '');
            if (operationOptions && operationOptions.requireDraftIdForReplay &&
                (!expectedDraftId || !storedDraftId ||
                  expectedDraftId !== storedDraftId)) {
              throw new CardRepositoryError(
                '已有卡组必须用相同 draftId 显式重放',
                'BW_CARD_REPOSITORY_SOURCE_CONFLICT',
                { field: 'source.draftId' }
              );
            }
            if (!same(previousEntity.cards, cards)) {
              throw new CardRepositoryError(
                '相同 gid 的草稿 cards 发生分叉',
                'BW_CARD_REPOSITORY_CONTENT_CONFLICT',
                { field: 'cards' }
              );
            }
            if (!same(previousEntity.source, source)) {
              throw new CardRepositoryError(
                '相同 gid 的草稿 source 发生分叉',
                'BW_CARD_REPOSITORY_SOURCE_CONFLICT',
                { field: 'source' }
              );
            }
            return project(records, false);
          }
          var at = now();
          var entity = entityValue(
            id,
            cards,
            source,
            at,
            at
          );
          var state = stateValue(
            id,
            freshStateMap(cards.length),
            cards.length
          );
          return batchPut(id, records, entity, state, 'draft', operationOptions);
        });
      });
    }

    function saveConfirmedCard(input, operationOptions) {
      input = input || {};
      var id;
      try { id = identityOf(input, idFactory, true); }
      catch (error) { return Promise.reject(error); }
      return serialize(function () {
        return pair(id).then(function (records) {
          rejectDeleted(records, id);
          if (!!records.entity !== !!records.state) {
            throw new CardRepositoryError(
              '确认入库遇到半条本地卡组',
              'BW_CARD_REPOSITORY_PARTIAL'
            );
          }
          var previousEntity = records.entity && records.entity.value;
          var previousState = records.state && records.state.value;
          var cards;
          var source;
          try {
            cards = previousEntity
              ? (Array.isArray(input.cards)
                ? normalizeCards(input.cards)
                : normalizeCards(previousEntity.cards))
              : cardsOf(input, true);
            if (previousEntity && Array.isArray(input.cards) &&
                cards.length !== previousEntity.cards.length) {
              throw new CardRepositoryError(
                '确认阶段不得改变批内卡片数量',
                'BW_CARD_REPOSITORY_TRANSITION'
              );
            }
            source = input.source ? normalizeSource(input.source) :
              normalizeSource(previousEntity && previousEntity.source);
          } catch (error) { throw error; }
          var index = input.cardIndex == null ? (cards.length === 1 ? 0 : null) :
            integer(input.cardIndex, 'cardIndex', 0, false);
          if (index == null || index >= cards.length) {
            throw new CardRepositoryError(
              'saveConfirmedCard 必须提供有效 cardIndex',
              'BW_CARD_REPOSITORY_CARD_INDEX'
            );
          }
          if (plain(input.card) && previousEntity) cards[index] = normalizeCard(input.card);
          var states = previousState
            ? normalizeStateMap(previousState.states, previousEntity.cards.length)
            : freshStateMap(cards.length);
          var current = states[String(index)] || draftState();
          if (current.removed) {
            throw new CardRepositoryError(
              '已删除的批内卡片不能隐式复活',
              'BW_CARD_REPOSITORY_CARD_REMOVED',
              { id: id, cardIndex: index }
            );
          }
          var at = now();
          states[String(index)] = cardState(
            'confirmed',
            current.confirmedAt || at,
            !current.review || current.review.status === 'unavailable'
              ? defaultReview('confirmed')
              : current.review,
            current.flags,
            current.projections,
            current.exactState,
            false
          );
          var entity = entityValue(
            id,
            cards,
            source,
            previousEntity ? previousEntity.createdAt : at,
            previousEntity && same(previousEntity.cards, cards) && same(previousEntity.source, source)
              ? previousEntity.contentUpdatedAt
              : at
          );
          var state = stateValue(id, states, cards.length);
          if (records.entity && records.state &&
              same(previousEntity, entity) && same(previousState, state)) {
            return project(records, false);
          }
          return batchPut(id, records, entity, state, 'confirm-' + index, operationOptions);
        });
      });
    }

    function removeDraftCard(idValue, cardIndex, operationOptions) {
      operationOptions = operationOptions || {};
      var id;
      var index;
      try {
        id = normalizeId(idValue);
        index = integer(cardIndex, 'cardIndex', 0, false);
      } catch (error) { return Promise.reject(error); }
      return serialize(function () {
        return pair(id).then(function (records) {
          rejectDeleted(records, id);
          if (!records.entity || !records.state) {
            throw new CardRepositoryError('卡组不存在', 'BW_CARD_REPOSITORY_NOT_FOUND');
          }
          var cards = normalizeCards(records.entity.value.cards);
          if (index >= cards.length) {
            throw new CardRepositoryError('cardIndex 超出卡组', 'BW_CARD_REPOSITORY_CARD_INDEX');
          }
          var states = normalizeStateMap(records.state.value.states, cards.length);
          var current = states[String(index)];
          if (current.phase !== 'draft') {
            throw new CardRepositoryError(
              '已确认卡片不能按草稿删除',
              'BW_CARD_REPOSITORY_TRANSITION',
              { id: id, cardIndex: index }
            );
          }
          if (current.removed) return project(records, false);
          states[String(index)] = cardState(
            current.phase,
            current.confirmedAt,
            current.review,
            current.flags,
            current.projections,
            current.exactState,
            true
          );
          var next = stateValue(id, states, cards.length);
          var base = mutationBase(
            operationOptions.mutationId,
            'remove-draft-' + index,
            id,
            mutationFactory
          );
          return store.put(STATE_COLLECTION, next, {
            id: id,
            ifRev: revision(records.state, operationOptions.ifStateRev, 'ifStateRev'),
            mutationId: base + ':state'
          }).then(function (stateRecord) {
            return project({
              entity: records.entity,
              state: verifyWrite(stateRecord, next, STATE_COLLECTION)
            }, false);
          });
        });
      });
    }

    function patchState(idValue, cardIndex, patch, operationOptions) {
      operationOptions = operationOptions || {};
      var id;
      var index;
      try {
        id = normalizeId(idValue);
        index = integer(cardIndex, 'cardIndex', 0, false);
        if (!plain(patch)) {
          throw new CardRepositoryError('state patch 必须是对象', 'BW_CARD_REPOSITORY_STATE');
        }
        allowedFields(patch, {
          review: true, flags: true, projections: true,
          ankiReceipt: true, exactState: true
        }, 'state patch');
        if (!Object.keys(patch).length) {
          throw new CardRepositoryError('state patch 不能为空', 'BW_CARD_REPOSITORY_STATE');
        }
      } catch (error) { return Promise.reject(error); }
      return serialize(function () {
        return pair(id).then(function (records) {
          rejectDeleted(records, id);
          if (!records.entity || !records.state) {
            throw new CardRepositoryError('卡组不存在', 'BW_CARD_REPOSITORY_NOT_FOUND');
          }
          var cards = normalizeCards(records.entity.value.cards);
          if (index >= cards.length) {
            throw new CardRepositoryError('cardIndex 超出卡组', 'BW_CARD_REPOSITORY_CARD_INDEX');
          }
          var states = normalizeStateMap(records.state.value.states, cards.length);
          var current = states[String(index)];
          if (current.removed) {
            throw new CardRepositoryError(
              '已删除的批内卡片不能再修改状态',
              'BW_CARD_REPOSITORY_CARD_REMOVED',
              { id: id, cardIndex: index }
            );
          }
          var projectionsPatch = patch.projections;
          if (patch.ankiReceipt != null) {
            if (!plain(patch.ankiReceipt)) {
              throw new CardRepositoryError('ankiReceipt 必须是对象', 'BW_CARD_REPOSITORY_RECEIPT');
            }
            var target = String(patch.ankiReceipt.target || '').trim();
            if (!SAFE_KEY_RE.test(target)) {
              throw new CardRepositoryError('ankiReceipt.target 无效', 'BW_CARD_REPOSITORY_RECEIPT');
            }
            var receipt = clone(patch.ankiReceipt);
            delete receipt.target;
            projectionsPatch = { anki: {} };
            projectionsPatch.anki[target] = receipt;
          }
          states[String(index)] = cardState(
            current.phase,
            current.confirmedAt,
            normalizeReview(patch.review, current.review),
            normalizeFlags(patch.flags, current.flags),
            normalizeProjections(projectionsPatch, current.projections),
            own(patch, 'exactState')
              ? normalizeExactState(patch.exactState)
              : current.exactState,
            false
          );
          var next = stateValue(id, states, cards.length);
          if (same(records.state.value, next)) return project(records, false);
          var base = mutationBase(
            operationOptions.mutationId,
            'state-' + index,
            id,
            mutationFactory
          );
          return store.put(STATE_COLLECTION, next, {
            id: id,
            ifRev: revision(records.state, operationOptions.ifStateRev, 'ifStateRev'),
            mutationId: base + ':state'
          }).then(function (stateRecord) {
            return project({
              entity: records.entity,
              state: verifyWrite(stateRecord, next, STATE_COLLECTION)
            }, false);
          });
        });
      });
    }
    function recordAnkiReceipt(id, cardIndex, target, receipt, operationOptions) {
      return patchState(id, cardIndex, {
        ankiReceipt: Object.assign({}, receipt || {}, { target: target })
      }, operationOptions);
    }
    function importLegacyBatch(inputRecords, operationOptions) {
      operationOptions = operationOptions || {};
      var specs;
      try {
        if (!Array.isArray(inputRecords) || !inputRecords.length || inputRecords.length > 500) {
          throw new CardRepositoryError(
            'legacy batch 必须包含 1-500 条记录',
            'BW_CARD_REPOSITORY_LEGACY'
          );
        }
        specs = inputRecords.map(normalizeLegacyRecord);
        var seen = {};
        specs.forEach(function (spec) {
          if (seen[spec.id]) {
            throw new CardRepositoryError(
              'legacy batch 含重复 gid：' + spec.id,
              'BW_CARD_REPOSITORY_LEGACY'
            );
          }
          seen[spec.id] = true;
        });
      } catch (error) { return Promise.reject(error); }
      return serialize(function () {
        return Promise.all(specs.map(function (spec) { return pair(spec.id); }))
          .then(function (loaded) {
            var at = now();
            var plans = specs.map(function (spec, index) {
              var records = loaded[index];
              if (!!records.entity !== !!records.state) {
                throw new CardRepositoryError(
                  'legacy 导入遇到半条本地卡组：' + spec.id,
                  'BW_CARD_REPOSITORY_PARTIAL'
                );
              }
              /* Bootstrap is create-if-absent.  Re-check inside the serialized
               * repository operation so a local save or tombstone that races
               * the caller's preflight remains authoritative without reading
               * or comparing the older legacy payload. */
              if (records.entity && operationOptions.missingOnly === true) {
                return {
                  spec: spec,
                  records: records,
                  entity: records.entity.value,
                  state: records.state.value,
                  writeEntity: false,
                  writeState: false
                };
              }
              rejectDeleted(records, spec.id);
              if (!records.entity) {
                return {
                  spec: spec,
                  records: records,
                  entity: entityValue(
                    spec.id,
                    spec.cards,
                    spec.source,
                    spec.timestamp || at,
                    spec.timestamp || at
                  ),
                  state: stateValue(spec.id, spec.states, spec.cards.length),
                  writeEntity: true,
                  writeState: true
                };
              }
              var current = project(records, false);
              if (!same(current.cards, spec.cards)) {
                throw new CardRepositoryError(
                  'legacy cards 与本地同 gid 内容分叉：' + spec.id,
                  'BW_CARD_REPOSITORY_LEGACY_CONFLICT',
                  { id: spec.id, field: 'cards' }
                );
              }
              var currentLegacyRef = current.source.legacy &&
                String(current.source.legacy.source_ref || '');
              var incomingLegacyRef = spec.source.legacy &&
                String(spec.source.legacy.source_ref || '');
              if (current.source.kind === 'pi-legacy-card-registry' &&
                  currentLegacyRef && incomingLegacyRef &&
                  currentLegacyRef !== incomingLegacyRef) {
                throw new CardRepositoryError(
                  'legacy source_ref 与本地同 gid 来源分叉：' + spec.id,
                  'BW_CARD_REPOSITORY_LEGACY_CONFLICT',
                  { id: spec.id, field: 'source_ref' }
                );
              }
              var mergedStates = clone(current.states);
              var changed = false;
              Object.keys(spec.states).forEach(function (key) {
                var localState = mergedStates[key];
                var incomingState = spec.states[key];
                var localExact = localState.exactState || {};
                var incomingExact = incomingState.exactState || {};
                if (!emptyObject(localExact) && !emptyObject(incomingExact) &&
                    !same(localExact, incomingExact)) {
                  throw new CardRepositoryError(
                    'legacy state 与本地同 gid/index 状态分叉：' + spec.id + '/' + key,
                    'BW_CARD_REPOSITORY_LEGACY_CONFLICT',
                    { id: spec.id, cardIndex: Number(key), field: 'exactState' }
                  );
                }
                var merged = clone(localState);
                if (emptyObject(localExact) && !emptyObject(incomingExact)) {
                  merged.exactState = clone(incomingExact);
                  if (localState.phase === 'draft') {
                    merged.phase = incomingState.phase;
                    merged.confirmedAt = incomingState.confirmedAt;
                    merged.review = clone(incomingState.review);
                  }
                  changed = true;
                }
                var projections = mergeProjections(
                  merged.projections,
                  incomingState.projections
                );
                if (!same(projections, merged.projections)) {
                  merged.projections = projections;
                  changed = true;
                }
                mergedStates[key] = cardState(
                  merged.phase,
                  merged.confirmedAt,
                  merged.review,
                  merged.flags,
                  merged.projections,
                  merged.exactState,
                  merged.removed
                );
              });
              return {
                spec: spec,
                records: records,
                entity: records.entity.value,
                state: stateValue(spec.id, mergedStates, spec.cards.length),
                writeEntity: false,
                writeState: changed
              };
            });
            var base = mutationBase(
              operationOptions.mutationId,
              'legacy-batch',
              'batch',
              mutationFactory
            );
            var mutations = [];
            var slots = [];
            plans.forEach(function (plan, planIndex) {
              if (plan.writeEntity) {
                slots.push({ planIndex: planIndex, side: 'entity', expected: plan.entity });
                mutations.push({
                  collection: ENTITY_COLLECTION,
                  value: plan.entity,
                  options: {
                    id: plan.spec.id,
                    ifRev: revision(plan.records.entity, null, 'ifEntityRev'),
                    mutationId: base + ':' + plan.spec.id + ':entity'
                  }
                });
              }
              if (plan.writeState) {
                slots.push({ planIndex: planIndex, side: 'state', expected: plan.state });
                mutations.push({
                  collection: STATE_COLLECTION,
                  value: plan.state,
                  options: {
                    id: plan.spec.id,
                    ifRev: revision(plan.records.state, null, 'ifStateRev'),
                    mutationId: base + ':' + plan.spec.id + ':state'
                  }
                });
              }
            });
            function finish(written) {
              written = written || [];
              slots.forEach(function (slot, writeIndex) {
                var plan = plans[slot.planIndex];
                var record = verifyWrite(
                  written[writeIndex],
                  slot.expected,
                  slot.side === 'entity' ? ENTITY_COLLECTION : STATE_COLLECTION
                );
                plan.records[slot.side] = record;
              });
              return plans.map(function (plan) { return project(plan.records, false); });
            }
            return mutations.length ? store.batch(mutations).then(finish) : finish([]);
          });
      });
    }
    function load(idValue, query) {
      var id;
      try { id = normalizeId(idValue); }
      catch (error) { return Promise.reject(error); }
      return queue.then(function () {
        return pair(id).then(function (records) {
          return project(records, !!(query && query.includeDeleted));
        });
      });
    }
    function listAll(collection, includeDeleted) {
      var output = [];
      function page(offset) {
        return store.list(collection, {
          includeDeleted: includeDeleted,
          offset: offset,
          limit: 200
        }).then(function (records) {
          records = Array.isArray(records) ? records : [];
          Array.prototype.push.apply(output, records);
          return records.length < 200 ? output : page(offset + records.length);
        });
      }
      return page(0);
    }
    function snapshot(query) {
      query = query || {};
      var includeDeleted = query.includeDeleted === true;
      return queue.then(function () {
        return Promise.all([
          listAll(ENTITY_COLLECTION, includeDeleted),
          listAll(STATE_COLLECTION, includeDeleted)
        ]).then(function (lists) {
          var entities = new Map();
          var states = new Map();
          lists[0].forEach(function (record) { entities.set(String(record.id), record); });
          lists[1].forEach(function (record) { states.set(String(record.id), record); });
          var ids = Array.from(new Set(
            Array.from(entities.keys()).concat(Array.from(states.keys()))
          )).sort();
          return ids.map(function (id) {
            return project({ entity: entities.get(id), state: states.get(id) }, includeDeleted);
          }).filter(Boolean);
        });
      });
    }
    function tombstone(idValue, operationOptions) {
      operationOptions = operationOptions || {};
      var id;
      try { id = normalizeId(idValue); }
      catch (error) { return Promise.reject(error); }
      return serialize(function () {
        return pair(id).then(function (records) {
          if (!records.entity && !records.state) {
            throw new CardRepositoryError('卡组不存在', 'BW_CARD_REPOSITORY_NOT_FOUND');
          }
          if (!records.entity || !records.state) {
            throw new CardRepositoryError('卡组内容与状态不完整', 'BW_CARD_REPOSITORY_PARTIAL');
          }
          if (records.entity.deleted && records.state.deleted) return project(records, true);
          rejectDeleted(records, id);
          var base = mutationBase(operationOptions.mutationId, 'remove', id, mutationFactory);
          return store.batch([
            {
              operation: 'remove', collection: ENTITY_COLLECTION, id: id,
              options: {
                ifRev: revision(records.entity, operationOptions.ifEntityRev, 'ifEntityRev'),
                mutationId: base + ':entity'
              }
            },
            {
              operation: 'remove', collection: STATE_COLLECTION, id: id,
              options: {
                ifRev: revision(records.state, operationOptions.ifStateRev, 'ifStateRev'),
                mutationId: base + ':state'
              }
            }
          ]).then(function (written) {
            if (!written[0] || !written[0].deleted || !written[1] || !written[1].deleted) {
              throw new CardRepositoryError(
                '删除 mutationId 已被不同操作使用',
                'BW_CARD_REPOSITORY_MUTATION_REUSED'
              );
            }
            return project({ entity: written[0], state: written[1] }, true);
          });
        });
      });
    }
    function subscribe(listener, query) {
      if (typeof listener !== 'function') {
        throw new CardRepositoryError('listener 必须是函数', 'BW_CARD_REPOSITORY_LISTENER');
      }
      query = query || {};
      var stopped = false;
      var pending = new Map();
      function receive(change) {
        if (stopped || !change || !change.record) return;
        var id;
        try { id = normalizeId(change.record.id); } catch (_) { return; }
        pending.set(id, clone(change));
        Promise.resolve().then(function () {
          if (stopped || !pending.has(id)) return;
          var cause = pending.get(id);
          pending.delete(id);
          load(id, { includeDeleted: true }).then(function (record) {
            if (stopped) return;
            try {
              listener({
                contract: CONTRACT,
                cardId: id,
                source: 'storage',
                cause: cause,
                record: record
              });
            } catch (_) {}
          }).catch(function () {});
        });
      }
      var offEntity = store.subscribe({ collection: ENTITY_COLLECTION }, receive);
      var offState = store.subscribe({ collection: STATE_COLLECTION }, receive);
      if (query.emitSnapshot === true) {
        snapshot({ includeDeleted: query.includeDeleted === true }).then(function (items) {
          if (stopped) return;
          items.forEach(function (record) {
            try {
              listener({
                contract: CONTRACT,
                cardId: record.id,
                source: 'snapshot',
                cause: null,
                record: record
              });
            } catch (_) {}
          });
        }).catch(function () {});
      }
      return function () {
        stopped = true;
        pending.clear();
        try { if (typeof offEntity === 'function') offEntity(); } catch (_) {}
        try { if (typeof offState === 'function') offState(); } catch (_) {}
      };
    }

    return Object.freeze({
      contract: CONTRACT,
      newCardId: newCardId,
      registerDraft: registerDraft,
      saveConfirmedCard: saveConfirmedCard,
      removeDraftCard: removeDraftCard,
      patchState: patchState,
      recordAnkiReceipt: recordAnkiReceipt,
      importLegacyBatch: importLegacyBatch,
      load: load,
      snapshot: snapshot,
      subscribe: subscribe,
      tombstone: tombstone,
      status: function () {
        return {
          contract: CONTRACT,
          available: true,
          entityCollection: ENTITY_COLLECTION,
          stateCollection: STATE_COLLECTION
        };
      }
    });
  }

  var defaultInstance = null;
  function defaultRepository() {
    if (!defaultInstance) defaultInstance = createCardRepository({});
    return defaultInstance;
  }
  function delegate(name) {
    return function () {
      var repo;
      try { repo = defaultRepository(); }
      catch (error) { return Promise.reject(error); }
      return repo[name].apply(repo, arguments);
    };
  }

  return Object.freeze({
    CONTRACT: CONTRACT,
    ENTITY_CONTRACT: ENTITY_CONTRACT,
    STATE_CONTRACT: STATE_CONTRACT,
    ENTITY_COLLECTION: ENTITY_COLLECTION,
    STATE_COLLECTION: STATE_COLLECTION,
    RECORD_SCHEMA: RECORD_SCHEMA,
    CardRepositoryError: CardRepositoryError,
    createCardRepository: createCardRepository,
    normalizeCard: normalizeCard,
    normalizeCards: normalizeCards,
    normalizeSource: normalizeSource,
    newCardId: secureId,
    registerDraft: delegate('registerDraft'),
    saveConfirmedCard: delegate('saveConfirmedCard'),
    removeDraftCard: delegate('removeDraftCard'),
    patchState: delegate('patchState'),
    recordAnkiReceipt: delegate('recordAnkiReceipt'),
    importLegacyBatch: delegate('importLegacyBatch'),
    load: delegate('load'),
    snapshot: delegate('snapshot'),
    tombstone: delegate('tombstone'),
    subscribe: function (listener, query) {
      return defaultRepository().subscribe(listener, query);
    },
    status: function () {
      try { return defaultRepository().status(); }
      catch (error) {
        return {
          contract: CONTRACT,
          available: false,
          code: String(error && error.code || 'BW_CARD_REPOSITORY_UNAVAILABLE')
        };
      }
    }
  });
});
