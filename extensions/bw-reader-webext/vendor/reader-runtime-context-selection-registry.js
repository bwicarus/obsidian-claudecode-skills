/* context-selection-registry.js — 跨 PWA / 扩展的显式上下文选择状态中心。
 *
 * 设计约束：
 *   1. id 是语义实体编号，不是 DOM 实例编号；同 cid 的多个视图必须登记同一个 id。
 *   2. parentId / covers 描述语义包含关系。快照只导出“最大选中节点”：
 *      整卡已选时，其内部段落仍可保留原始选中态，但不会重复进入 AI 上下文。
 *   3. 快照按 id 稳定排序、对象键稳定序列化且不含时间戳/瞬时 revision。
 *      选择变化只应放在请求的动态上下文尾部，不得据此重建 system prompt 或工具表。
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.BWReaderRuntime = root.BWReaderRuntime || {};
  root.BWReaderRuntime.contextSelection = api;
  if (!root.BWReaderRuntime.contextSelections) {
    root.BWReaderRuntime.contextSelections = api.createRegistry();
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var CONTRACT = 'context-selection/1';

  function ContextSelectionError(message, details) {
    this.name = 'ContextSelectionError';
    this.code = 'BW_CONTEXT_SELECTION_INVALID';
    this.message = message;
    this.details = details || null;
    if (Error.captureStackTrace) Error.captureStackTrace(this, ContextSelectionError);
  }
  ContextSelectionError.prototype = Object.create(Error.prototype);
  ContextSelectionError.prototype.constructor = ContextSelectionError;

  function own(value, key) {
    return Object.prototype.hasOwnProperty.call(value || {}, key);
  }

  function cleanId(value, field, optional) {
    var out = String(value == null ? '' : value).trim();
    if (!out && !optional) {
      throw new ContextSelectionError(field + ' 不能为空', { field: field });
    }
    if (out.length > 512 || /[\u0000-\u001f\u007f]/.test(out)) {
      throw new ContextSelectionError(field + ' 非法', { field: field });
    }
    return out;
  }

  function finiteLimit(value, fallback) {
    if (value == null) return fallback;
    value = Number(value);
    if (!isFinite(value)) return fallback;
    return Math.max(0, Math.floor(value));
  }

  function stableClone(value, seen) {
    if (value == null || typeof value === 'string' || typeof value === 'boolean') return value;
    if (typeof value === 'number') {
      if (!isFinite(value)) throw new ContextSelectionError('上下文元数据必须是有限数字');
      return value;
    }
    if (typeof value !== 'object') {
      throw new ContextSelectionError('上下文元数据必须是 JSON 值');
    }
    seen = seen || [];
    if (seen.indexOf(value) >= 0) throw new ContextSelectionError('上下文元数据不能循环引用');
    seen.push(value);
    var out;
    if (Array.isArray(value)) {
      out = value.map(function (item) { return stableClone(item, seen); });
    } else {
      var proto = Object.getPrototypeOf(value);
      if (proto !== Object.prototype && proto !== null) {
        seen.pop();
        throw new ContextSelectionError('上下文元数据只能使用普通对象');
      }
      out = {};
      Object.keys(value).sort().forEach(function (key) {
        if (value[key] !== undefined) out[key] = stableClone(value[key], seen);
      });
    }
    seen.pop();
    return out;
  }

  function stableStringify(value) {
    return JSON.stringify(stableClone(value));
  }

  function normalizeIds(value, selfId) {
    var seen = Object.create(null);
    var out = [];
    (Array.isArray(value) ? value : []).forEach(function (item) {
      var id = cleanId(item, 'covers', true);
      if (!id || id === selfId || seen[id]) return;
      seen[id] = true;
      out.push(id);
    });
    return out.sort();
  }

  function normalizeRecord(input, previous) {
    input = input || {};
    previous = previous || null;
    var id = cleanId(input.id != null ? input.id : (previous && previous.id), 'id');
    var parentId = own(input, 'parentId')
      ? cleanId(input.parentId, 'parentId', true)
      : String((previous && previous.parentId) || '');
    if (parentId === id) {
      throw new ContextSelectionError('parentId 不能指向自身', { id: id });
    }
    var covers = own(input, 'covers')
      ? normalizeIds(input.covers, id)
      : ((previous && previous.covers) || []).slice();
    return {
      id: id,
      kind: String(own(input, 'kind') ? (input.kind || 'context') : ((previous && previous.kind) || 'context')).slice(0, 80),
      label: String(own(input, 'label') ? (input.label || '') : ((previous && previous.label) || '')).slice(0, 240),
      text: String(own(input, 'text') ? (input.text || '') : ((previous && previous.text) || '')),
      parentId: parentId,
      covers: covers,
      source: stableClone(own(input, 'source') ? (input.source || {}) : ((previous && previous.source) || {})),
      meta: stableClone(own(input, 'meta') ? (input.meta || {}) : ((previous && previous.meta) || {}))
    };
  }

  function cloneRecord(record, maxText) {
    return {
      id: record.id,
      kind: record.kind,
      label: record.label,
      text: record.text.slice(0, maxText),
      parentId: record.parentId,
      covers: record.covers.slice(),
      source: stableClone(record.source),
      meta: stableClone(record.meta)
    };
  }

  function createRegistry(options) {
    options = options || {};
    var defaultMaxText = finiteLimit(options.maxText, 2500);
    var nodes = Object.create(null);
    var selected = Object.create(null);
    var listeners = [];
    var version = 0; // 仅供本地视图刷新；不会进入 AI 请求快照。

    function emit(type, id) {
      version += 1;
      listeners.slice().forEach(function (listener) {
        try { listener({ type: type, id: id || '', version: version }); } catch (_) {}
      });
    }

    function upsert(input) {
      var before = input && input.id ? nodes[String(input.id)] : null;
      var record = normalizeRecord(input, before);
      var changed = !before || stableStringify(before) !== stableStringify(record);
      nodes[record.id] = record;
      if (input && own(input, 'selected')) {
        var nextSelected = !!input.selected;
        if (!!selected[record.id] !== nextSelected) {
          if (nextSelected) selected[record.id] = true;
          else delete selected[record.id];
          changed = true;
        }
      }
      if (changed) emit('upsert', record.id);
      return cloneRecord(record, record.text.length);
    }

    function remove(id) {
      id = cleanId(id, 'id');
      if (!nodes[id] && !selected[id]) return false;
      delete nodes[id];
      delete selected[id];
      emit('remove', id);
      return true;
    }

    function setSelected(id, on) {
      id = cleanId(id, 'id');
      if (!nodes[id]) return false;
      on = !!on;
      if (!!selected[id] === on) return false;
      if (on) selected[id] = true;
      else delete selected[id];
      emit(on ? 'select' : 'deselect', id);
      return true;
    }

    function select(input, on) {
      var id;
      if (input && typeof input === 'object') {
        id = upsert(input).id;
      } else {
        id = cleanId(input, 'id');
      }
      setSelected(id, on !== false);
      return isSelected(id);
    }

    function toggle(input) {
      var id;
      if (input && typeof input === 'object') id = upsert(input).id;
      else id = cleanId(input, 'id');
      setSelected(id, !selected[id]);
      return !!selected[id];
    }

    function isSelected(id) {
      return !!selected[String(id || '')];
    }

    function ancestry(id) {
      var out = Object.create(null);
      var cursor = String(id || '');
      while (cursor && !out[cursor]) {
        out[cursor] = true;
        cursor = nodes[cursor] ? nodes[cursor].parentId : '';
      }
      return out;
    }

    function covers(covererId, candidateId) {
      covererId = String(covererId || '');
      candidateId = String(candidateId || '');
      if (!covererId || !candidateId || covererId === candidateId || !nodes[covererId]) return false;
      var candidateTree = ancestry(candidateId);
      if (candidateTree[covererId]) return true;
      var seen = Object.create(null);
      var queue = (nodes[covererId].covers || []).slice();
      while (queue.length) {
        var current = queue.shift();
        if (!current || seen[current]) continue;
        seen[current] = true;
        if (candidateTree[current]) return true;
        if (nodes[current] && nodes[current].covers) {
          queue = queue.concat(nodes[current].covers);
        }
      }
      return false;
    }

    function effectiveIds() {
      var ids = Object.keys(selected).filter(function (id) { return !!nodes[id]; }).sort();
      return ids.filter(function (candidateId) {
        for (var i = 0; i < ids.length; i++) {
          var covererId = ids[i];
          if (covererId === candidateId || !covers(covererId, candidateId)) continue;
          // 非法/暂态环不允许把两端都吃掉；稳定地保留字典序较小的一端。
          if (!covers(candidateId, covererId) || covererId < candidateId) return false;
        }
        return true;
      });
    }

    function isEffective(id) {
      return effectiveIds().indexOf(String(id || '')) >= 0;
    }

    function snapshot(snapshotOptions) {
      snapshotOptions = snapshotOptions || {};
      var maxText = finiteLimit(snapshotOptions.maxText, defaultMaxText);
      var limit = finiteLimit(
        snapshotOptions.limit != null ? snapshotOptions.limit : snapshotOptions.maxItems,
        Number.MAX_SAFE_INTEGER || 9007199254740991
      );
      return {
        contract: CONTRACT,
        items: effectiveIds().slice(0, limit).map(function (id) {
          return cloneRecord(nodes[id], maxText);
        })
      };
    }

    function serialize(snapshotOptions) {
      return stableStringify(snapshot(snapshotOptions));
    }

    function toLegacy(snapshotOptions) {
      var items = snapshot(snapshotOptions).items;
      var labels = [];
      var map = {};
      items.forEach(function (item) {
        var base = item.label || item.id;
        var label = base;
        var n = 2;
        while (own(map, label)) label = base + '·' + n++;
        labels.push(label);
        map[label] = item.text;
      });
      return {
        labels: labels,
        map: map,
        items: items,
        serialized: serialize(snapshotOptions)
      };
    }

    function clear() {
      if (!Object.keys(selected).length) return false;
      selected = Object.create(null);
      emit('clear', '');
      return true;
    }

    function get(id) {
      id = String(id || '');
      return nodes[id] ? cloneRecord(nodes[id], nodes[id].text.length) : null;
    }

    function subscribe(listener) {
      if (typeof listener !== 'function') throw new ContextSelectionError('subscribe 需要函数');
      listeners.push(listener);
      return function () {
        var index = listeners.indexOf(listener);
        if (index >= 0) listeners.splice(index, 1);
      };
    }

    return {
      contract: CONTRACT,
      upsert: upsert,
      remove: remove,
      select: select,
      deselect: function (id) { return setSelected(id, false); },
      toggle: toggle,
      clear: clear,
      get: get,
      isSelected: isSelected,
      isEffective: isEffective,
      covers: covers,
      snapshot: snapshot,
      serialize: serialize,
      toLegacy: toLegacy,
      subscribe: subscribe,
      version: function () { return version; }
    };
  }

  return {
    CONTRACT: CONTRACT,
    ContextSelectionError: ContextSelectionError,
    createRegistry: createRegistry,
    stableStringify: stableStringify
  };
});
