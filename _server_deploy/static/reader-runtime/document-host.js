/* document-host.js — PDF / EPUB / HTML 书籍 / 普通网页的统一内容端口。
 *
 * 这里不渲染 UI、不读写业务数据，也不转换宿主坐标。Anchor.data 永远由宿主拥有。
 * 旧 RC adapter 可通过 createLegacyDocumentHost() 逐步接入；未迁能力必须明确标成
 * pending / unsupported，禁止用空函数冒充 supported。
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.BWReaderRuntime = root.BWReaderRuntime || {};
  root.BWReaderRuntime.documentHost = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var CONTRACT = 'document-host/1';
  var KINDS = ['pdf', 'epub', 'html', 'favorite', 'web'];
  var STATUSES = ['supported', 'pending', 'unsupported'];
  var CAPABILITY_METHODS = {
    selection: ['getSelection', 'clearSelection'],
    visibleContent: ['getVisibleContent'],
    location: ['getCurrentLocation'],
    anchors: ['createAnchor', 'resolveAnchor'],
    read: ['read'],
    navigation: ['navigate'],
    search: ['search'],
    highlights: ['renderHighlight', 'removeHighlight']
  };
  var METHOD_CAPABILITY = {};
  Object.keys(CAPABILITY_METHODS).forEach(function (capability) {
    CAPABILITY_METHODS[capability].forEach(function (method) {
      METHOD_CAPABILITY[method] = capability;
    });
  });

  function clone(value) {
    if (value == null) return value;
    return JSON.parse(JSON.stringify(value));
  }
  function isFn(value) { return typeof value === 'function'; }
  function own(obj, key) { return Object.prototype.hasOwnProperty.call(obj || {}, key); }
  function clamp(value, low, high) {
    value = Number(value);
    if (!isFinite(value)) value = low;
    return Math.max(low, Math.min(high, value));
  }

  function ContractError(message, details) {
    this.name = 'DocumentHostContractError';
    this.code = 'BW_DOCUMENT_HOST_CONTRACT';
    this.message = message;
    this.details = details || null;
    if (Error.captureStackTrace) Error.captureStackTrace(this, ContractError);
  }
  ContractError.prototype = Object.create(Error.prototype);
  ContractError.prototype.constructor = ContractError;

  function CapabilityError(capability, status, reason) {
    this.name = 'DocumentCapabilityError';
    this.code = status === 'pending' ? 'BW_CAPABILITY_PENDING' : 'BW_CAPABILITY_UNSUPPORTED';
    this.capability = capability;
    this.status = status;
    this.reason = reason || '';
    this.message = '阅读宿主能力 ' + capability + ' 当前为 ' + status + (reason ? '：' + reason : '');
    if (Error.captureStackTrace) Error.captureStackTrace(this, CapabilityError);
  }
  CapabilityError.prototype = Object.create(Error.prototype);
  CapabilityError.prototype.constructor = CapabilityError;

  function normalizeCapability(name, input) {
    if (input === true) input = { status: 'supported' };
    if (input === false) input = { status: 'unsupported', reason: '宿主明确声明不支持' };
    input = input || {};
    var status = String(input.status || 'pending');
    if (STATUSES.indexOf(status) < 0) {
      throw new ContractError('未知能力状态：' + status, { capability: name });
    }
    return {
      status: status,
      owner: String(input.owner || 'pwa'),
      reason: String(input.reason || ''),
      details: clone(input.details || {})
    };
  }

  function normalizeCapabilities(input) {
    var out = {};
    Object.keys(CAPABILITY_METHODS).forEach(function (name) {
      out[name] = normalizeCapability(name, own(input, name) ? input[name] : {
        status: 'pending',
        reason: '尚未完成该宿主的能力判定'
      });
    });
    return out;
  }

  function normalizeLocation(input, documentId) {
    input = input || {};
    var total = Math.max(0, Number(input.total) || 0);
    var index = Math.max(0, Number(input.index) || 0);
    var progress = input.progress;
    if (progress == null) {
      progress = total > 1 ? index / (total - 1) : (total === 1 ? 1 : 0);
    }
    return {
      documentId: String(input.documentId || documentId || ''),
      unit: String(input.unit || 'location'),
      index: index,
      total: total,
      progress: clamp(progress, 0, 1),
      data: clone(input.data || {})
    };
  }

  function normalizeAnchor(input, documentId, fallbackKind) {
    if (!input) return null;
    if (input.documentId && input.data && input.kind) {
      return {
        documentId: String(input.documentId),
        kind: String(input.kind),
        revision: Math.max(1, Number(input.revision) || 1),
        data: clone(input.data)
      };
    }
    return {
      documentId: String(documentId || ''),
      kind: String(input.kind || fallbackKind || 'opaque'),
      revision: Math.max(1, Number(input.revision) || 1),
      data: clone(input.data || input)
    };
  }

  function normalizeSelection(input, documentId, anchorKind) {
    if (!input) return null;
    var text = String(input.text == null ? '' : input.text).trim();
    if (!text) return null;
    var context = input.context != null ? input.context
      : (input.ctx != null ? input.ctx : (input.sentence != null ? input.sentence : ''));
    return {
      documentId: String(input.documentId || documentId || ''),
      text: text,
      context: String(context || ''),
      anchor: normalizeAnchor(input.anchor, documentId, anchorKind),
      rect: clone(input.rect || null),
      data: clone(input.data || {})
    };
  }

  function normalizeVisibleContent(input, documentId, kind) {
    if (typeof input === 'string') input = { text: input };
    input = input || {};
    return {
      documentId: String(input.documentId || documentId || ''),
      kind: String(input.kind || kind || 'web'),
      text: String(input.text || ''),
      location: input.location ? normalizeLocation(input.location, documentId) : null,
      blocks: Array.isArray(input.blocks) ? clone(input.blocks) : [],
      data: clone(input.data || {})
    };
  }

  function auditDocumentHost(input) {
    var spec = input && input.__spec ? input.__spec : (input || {});
    var methods = spec.methods || {};
    var capabilities;
    try {
      capabilities = normalizeCapabilities(spec.capabilities || {});
    } catch (error) {
      return {
        contract: CONTRACT,
        valid: false,
        kind: String(spec.kind || 'unknown'),
        documentId: String(spec.documentId || ''),
        missing: {},
        errors: [error.message]
      };
    }
    var missing = {};
    Object.keys(capabilities).forEach(function (name) {
      if (capabilities[name].status !== 'supported') return;
      var absent = CAPABILITY_METHODS[name].filter(function (method) { return !isFn(methods[method]); });
      if (absent.length) missing[name] = absent;
    });
    var errors = [];
    if (KINDS.indexOf(String(spec.kind || '')) < 0) {
      errors.push('kind 必须是 pdf / epub / html / favorite / web');
    }
    if (!String(spec.documentId || '').trim()) errors.push('documentId 不能为空');
    if (Object.keys(missing).length) errors.push('supported 能力缺少真实实现');
    return {
      contract: CONTRACT,
      valid: errors.length === 0,
      kind: String(spec.kind || 'unknown'),
      documentId: String(spec.documentId || ''),
      capabilities: clone(capabilities),
      missing: missing,
      errors: errors
    };
  }

  function createDocumentHost(spec) {
    spec = spec || {};
    var audit = auditDocumentHost(spec);
    if (!audit.valid) throw new ContractError('DocumentHost 契约无效', audit);
    var methods = spec.methods || {};
    var capabilities = normalizeCapabilities(spec.capabilities || {});
    var documentId = String(spec.documentId);
    var kind = String(spec.kind);
    var anchorKind = String(spec.anchorKind || kind);

    function capability(name) {
      if (!own(capabilities, name)) throw new ContractError('未知能力：' + name);
      return clone(capabilities[name]);
    }
    function invoke(capabilityName, methodName, args) {
      var cap = capabilities[capabilityName];
      if (!cap || cap.status !== 'supported') {
        return Promise.reject(new CapabilityError(
          capabilityName,
          cap ? cap.status : 'unsupported',
          cap ? cap.reason : '能力未登记'
        ));
      }
      var method = methods[methodName];
      if (!isFn(method)) {
        return Promise.reject(new ContractError('能力声明与实现不一致：' + methodName, {
          capability: capabilityName,
          method: methodName
        }));
      }
      try { return Promise.resolve(method.apply(spec.context || null, args || [])); }
      catch (error) { return Promise.reject(error); }
    }

    var host = {
      contract: CONTRACT,
      documentId: documentId,
      kind: kind,
      anchorKind: anchorKind,
      capabilities: clone(capabilities),
      __spec: spec,
      capability: capability,
      supports: function (name) { return !!(capabilities[name] && capabilities[name].status === 'supported'); },
      audit: function () { return auditDocumentHost(spec); },
      invoke: invoke
    };

    host.getSelection = function () {
      return invoke('selection', 'getSelection', []).then(function (value) {
        return normalizeSelection(value, documentId, anchorKind);
      });
    };
    host.clearSelection = function () { return invoke('selection', 'clearSelection', []); };
    host.getVisibleContent = function (request) {
      return invoke('visibleContent', 'getVisibleContent', [request || {}]).then(function (value) {
        return normalizeVisibleContent(value, documentId, kind);
      });
    };
    host.getCurrentLocation = function () {
      return invoke('location', 'getCurrentLocation', []).then(function (value) {
        return normalizeLocation(value, documentId);
      });
    };
    host.createAnchor = function (source) {
      return invoke('anchors', 'createAnchor', [source || {}]).then(function (value) {
        return normalizeAnchor(value, documentId, anchorKind);
      });
    };
    host.resolveAnchor = function (anchor, options) {
      var normalized = normalizeAnchor(anchor, documentId, anchorKind);
      return invoke('anchors', 'resolveAnchor', [normalized, options || {}]);
    };
    host.read = function (request) {
      return invoke('read', 'read', [request || {}]).then(function (value) {
        return normalizeVisibleContent(value, documentId, kind);
      });
    };
    host.navigate = function (target, options) {
      return invoke('navigation', 'navigate', [target || {}, options || {}]);
    };
    host.search = function (query, options) {
      return invoke('search', 'search', [String(query || ''), options || {}]);
    };
    host.renderHighlight = function (highlight) {
      return invoke('highlights', 'renderHighlight', [clone(highlight || {})]);
    };
    host.removeHighlight = function (id) {
      return invoke('highlights', 'removeHighlight', [String(id || '')]);
    };
    return host;
  }

  function legacyKind(adapter, options) {
    var raw = String((options && options.kind) || adapter.kind || '');
    if (raw === 'favorite' || raw.indexOf('favorite') >= 0) return 'favorite';
    if (raw.indexOf('pdf') >= 0) return 'pdf';
    if (raw.indexOf('epub') >= 0) return 'epub';
    if (raw.indexOf('html') >= 0) return 'html';
    return 'web';
  }

  function legacyDocumentId(adapter, options) {
    if (options && options.documentId) return String(options.documentId);
    try {
      var info = isFn(adapter.fileInfo) ? adapter.fileInfo() : null;
      if (info && info.file) return String(info.file);
    } catch (_) {}
    try {
      var context = isFn(adapter.getContext) ? adapter.getContext() : null;
      if (context && context.file) return String(context.file);
    } catch (_) {}
    if (typeof location !== 'undefined' && location.href) return 'web:' + String(location.href).split('#')[0];
    return 'legacy:' + String(adapter.kind || 'unknown');
  }

  function findLegacyNavigate(adapter) {
    if (isFn(adapter.navigate)) return function (target, options) { return adapter.navigate(target, options); };
    if (isFn(adapter.jumpToAnchor)) return function (target) {
      return adapter.jumpToAnchor(target && target.data ? target.data : target);
    };
    var assistant = adapter._host && adapter._host.asst;
    if (assistant && isFn(assistant.goTo)) return function (target) {
      var data = target && target.data ? target.data : (target || {});
      var value = data.index != null ? data.index
        : (data.page != null ? data.page : (data.section != null ? data.section : target));
      return assistant.goTo(value);
    };
    return null;
  }

  function findLegacyAnchorResolver(adapter) {
    if (isFn(adapter.resolveAnchor)) return function (anchor, options) {
      var target = anchor && anchor.data ? anchor.data : anchor;
      return Promise.resolve(adapter.resolveAnchor(target, options)).then(function (result) {
        if (result && typeof result === 'object' && own(result, 'resolved')) return result;
        return { anchor: anchor, resolved: result !== false };
      });
    };
    if (isFn(adapter.jumpToAnchor)) return function (anchor, options) {
      var target = anchor && anchor.data ? anchor.data : anchor;
      return Promise.resolve(adapter.jumpToAnchor(target, options)).then(function (result) {
        return { anchor: anchor, resolved: result !== false };
      });
    };
    return null;
  }

  function status(supported, pendingReason) {
    return supported
      ? { status: 'supported', owner: 'pwa' }
      : { status: 'pending', owner: 'pwa', reason: pendingReason };
  }

  function createLegacyDocumentHost(adapter, options) {
    adapter = adapter || {};
    options = options || {};
    var kind = legacyKind(adapter, options);
    var documentId = legacyDocumentId(adapter, options);
    var navigate = findLegacyNavigate(adapter);
    var canSelection = isFn(adapter.captureSelection) && isFn(adapter.clearSelection);
    var canVisible = isFn(adapter.getContext) || isFn(adapter.currentChapterText);
    var canLocation = isFn(adapter.currentLocation);
    var anchorResolver = findLegacyAnchorResolver(adapter);
    // 普通 navigate 只证明“能去某处”，不证明能解析 selection anchor。典型反例是
    // Web-in-PWA：navigate 只接受 URL，DOM/quote 锚尚未实现，必须保持 pending。
    var canAnchor = !!anchorResolver && (isFn(adapter.createAnchor) || canSelection);
    var canSearch = isFn(adapter.search);
    var canHighlights = isFn(adapter.renderHighlight) && isFn(adapter.removeHighlight);

    var methods = {};
    if (canSelection) {
      methods.getSelection = function () { return adapter.captureSelection(); };
      methods.clearSelection = function () { return adapter.clearSelection(); };
    }
    if (canVisible) {
      methods.getVisibleContent = function () {
        var context = isFn(adapter.getContext) ? (adapter.getContext() || {}) : {};
        var text = context.visible_text || context.text || '';
        if (!text && isFn(adapter.currentChapterText)) text = adapter.currentChapterText();
        return { text: text || '', data: { context: context } };
      };
      methods.read = methods.getVisibleContent;
    }
    if (canLocation) methods.getCurrentLocation = function () { return adapter.currentLocation(); };
    if (canAnchor) {
      methods.createAnchor = function (source) {
        if (isFn(adapter.createAnchor)) return adapter.createAnchor(source);
        if (source && source.anchor) return source.anchor;
        var selection = adapter.captureSelection();
        return selection && selection.anchor;
      };
      methods.resolveAnchor = anchorResolver;
    }
    if (navigate) methods.navigate = navigate;
    if (canSearch) methods.search = function (query, searchOptions) { return adapter.search(query, searchOptions); };
    if (canHighlights) {
      methods.renderHighlight = function (highlight) { return adapter.renderHighlight(highlight); };
      methods.removeHighlight = function (id) { return adapter.removeHighlight(id); };
    }

    var capabilities = options.capabilities || {
      selection: status(canSelection, '旧宿主尚未同时暴露 captureSelection / clearSelection'),
      visibleContent: status(canVisible, '旧宿主尚未暴露可见内容读取入口'),
      location: status(canLocation, '旧宿主尚未暴露统一位置入口'),
      anchors: status(canAnchor, '旧宿主尚未同时提供可生成的选区锚与可验证的解析/跳转闭环'),
      read: status(canVisible, '读取仍绑在宿主助手上下文中，尚未迁入统一 read'),
      navigation: status(!!navigate, '旧宿主尚未暴露统一 navigate'),
      search: status(canSearch, '搜索实现仍留在各阅读器内部，尚未迁入统一接口'),
      highlights: status(canHighlights, '高亮 UI 已共享，但渲染/删除仍未暴露为统一宿主方法')
    };
    return createDocumentHost({
      kind: kind,
      documentId: documentId,
      anchorKind: String((adapter.config && adapter.config.anchorKind) || kind),
      capabilities: capabilities,
      methods: methods,
      legacyAdapter: adapter
    });
  }

  return {
    CONTRACT: CONTRACT,
    CAPABILITY_METHODS: clone(CAPABILITY_METHODS),
    ContractError: ContractError,
    CapabilityError: CapabilityError,
    normalizeAnchor: normalizeAnchor,
    normalizeLocation: normalizeLocation,
    normalizeSelection: normalizeSelection,
    normalizeVisibleContent: normalizeVisibleContent,
    auditDocumentHost: auditDocumentHost,
    createDocumentHost: createDocumentHost,
    createLegacyDocumentHost: createLegacyDocumentHost
  };
});
