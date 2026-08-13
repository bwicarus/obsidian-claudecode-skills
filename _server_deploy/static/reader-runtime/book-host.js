/* book-host.js — PWA 书籍阅读器对扩展公开的最小白名单宿主。
 *
 * 这不是普通网页适配器。它只接受 PDF / EPUB / HTML/Markdown / 收藏集四种
 * “真正的书”宿主；普通网页由扩展自己的 WebDocumentHost 负责。
 *
 * 页面仍拥有书籍渲染、坐标、锚点、高亮投影、墨迹和书籍便签。扩展只能通过
 * capabilities 中已声明、且 ACTION_CAPABILITY 中有固定映射的命令调用它们。
 * 任意函数名、DOM 节点、token、闭包和存储对象都不会穿过 postMessage 边界。 */
(function (root, factory) {
  var api = factory(root);
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.BWReaderBookHost = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function (root) {
  'use strict';

  var CONTRACT = 'book-host/1';
  var MODES = ['pdf', 'epub', 'html', 'favorite'];
  var ACTION_CAPABILITY = Object.freeze({
    clear_selection: 'selection',
    ocr: 'selectionOcr',
    highlight: 'highlight',
    // projection-only:持久化由调用方先完成；宿主只清自己的内存与可见叠层。
    remove_highlight: 'highlight',
    open_search: 'bookSearch',
    toggle_ruby: 'ruby',
    toggle_page_translate: 'pageTranslate',
    create_sticky: 'stickyNote',
    toggle_ink: 'ink',
    anchor_fx: 'anchorFx',
    jump_page: 'navigation',
    jump_location: 'navigation',
    change_page: 'navigation',
    jump_context: 'navigation',
    flash_selection: 'navigation',
    fit_width: 'zoom',
    zoom_by: 'zoom',
    pin_card: 'pinCard',
    pin_html: 'pinHtmlCard',
    toggle_fullscreen: 'fullscreen',
    open_settings: 'bookSettings',
    open_favorite: 'favorite',
    create_user_page: 'userPage',
    toggle_layout: 'layout',
    toggle_crop: 'crop'
  });
  var current = null;

  function own(value, key) {
    return Object.prototype.hasOwnProperty.call(value || {}, key);
  }
  function clone(value) {
    if (value == null) return value;
    return JSON.parse(JSON.stringify(value));
  }
  function text(value, max) {
    return String(value == null ? '' : value).slice(0, max || 8192);
  }
  function plainRect(rect) {
    if (!rect) return null;
    var left = Number(rect.left) || 0;
    var top = Number(rect.top) || 0;
    var right = Number(rect.right) || 0;
    var bottom = Number(rect.bottom) || 0;
    return {
      left: left,
      top: top,
      right: right,
      bottom: bottom,
      width: Number(rect.width) || Math.max(0, right - left),
      height: Number(rect.height) || Math.max(0, bottom - top)
    };
  }
  function normalizeSelection(value, spec) {
    if (!value || !String(value.text || '').trim()) return null;
    var compact = String(value.text || '').replace(/\s+/g, ' ').trim();
    var word = !/\s/.test(compact) && compact.length <= 80;
    var context = text(value.context || value.ctx || value.sentence || '', 12000);
    var out = {
      text: text(value.text, 12000),
      sentence: context,
      context: context || text(value.text, 12000),
      rect: plainRect(value.rect && (value.rect.client || value.rect)),
      anchor: value.anchor ? clone(value.anchor) : null,
      file: text(value.file || spec.file, 2048),
      book: text(value.book || spec.title, 512),
      langs: Array.isArray(value.langs) ? value.langs.slice(0, 8).map(String) : spec.langs.slice(),
      kind: value.kind || (word ? 'word' : 'multi'),
      shortPhrase: own(value, 'shortPhrase')
        ? !!value.shortPhrase
        : (!word && compact.length <= 80 && compact.split(/\s+/).length <= 8)
    };
    if (value.page != null) out.page = Math.max(0, Number(value.page) || 0);
    if (value.location != null) out.location = clone(value.location);
    return out;
  }
  function normalizeCapabilities(input, actions) {
    var out = {};
    Object.keys(input || {}).forEach(function (name) {
      if (/^[A-Za-z][A-Za-z0-9]{0,63}$/.test(name)) out[name] = input[name] === true;
    });
    out.selection = typeof actions.clear_selection === 'function';
    out.context = true;
    return Object.freeze(out);
  }
  function normalizeActions(input) {
    var out = {};
    Object.keys(ACTION_CAPABILITY).forEach(function (name) {
      if (typeof (input && input[name]) === 'function') out[name] = input[name];
    });
    return out;
  }
  function modeFrom(spec) {
    var mode = String(spec.mode || '').trim();
    if (MODES.indexOf(mode) < 0) {
      throw new Error('书籍宿主 mode 必须是 pdf / epub / html / favorite');
    }
    return mode;
  }
  function emitReady(api) {
    try {
      if (!root.document || !root.document.dispatchEvent) return;
      root.document.dispatchEvent(new CustomEvent('bw:reader-local-api-ready', {
        detail: {
          contract: CONTRACT,
          mode: api.mode,
          file: api.file,
          capabilities: clone(api.capabilities)
        }
      }));
      if (api.mode === 'pdf') {
        root.document.dispatchEvent(new CustomEvent('bw:pdf-local-api-ready'));
      }
    } catch (_) {}
  }

  function register(input) {
    input = input || {};
    var mode = modeFrom(input);
    var actions = normalizeActions(input.actions || {});
    var spec = {
      mode: mode,
      file: text(input.file, 2048),
      title: text(input.title || (root.document && root.document.title), 512),
      langs: Array.isArray(input.langs) ? input.langs.slice(0, 8).map(String) : [],
      selection: typeof input.selection === 'function' ? input.selection : function () { return null; },
      context: typeof input.context === 'function' ? input.context : function () { return null; },
      currentLocation: typeof input.currentLocation === 'function' ? input.currentLocation : function () { return null; }
    };
    var capabilities = normalizeCapabilities(input.capabilities || {}, actions);

    async function localAction(name, payload) {
      name = String(name || '');
      var capability = ACTION_CAPABILITY[name];
      if (!capability || typeof actions[name] !== 'function') {
        throw new Error('书籍宿主不允许本地命令：' + name);
      }
      if (!capabilities[capability]) {
        throw new Error('书籍宿主未声明能力：' + capability);
      }
      return actions[name](clone(payload || {}));
    }

    var api = Object.freeze({
      contract: CONTRACT,
      version: 2,
      mode: mode,
      file: spec.file,
      title: spec.title,
      capabilities: capabilities,
      selection: function () {
        return normalizeSelection(spec.selection(), spec);
      },
      context: function () {
        return clone(spec.context());
      },
      currentLocation: function () {
        return clone(spec.currentLocation());
      },
      localAction: localAction
    });
    current = api;
    root.__bwReaderLocalApi = api;
    if (mode === 'pdf') root.__bwPdfLocalApi = api;
    else {
      try { delete root.__bwPdfLocalApi; } catch (_) { root.__bwPdfLocalApi = null; }
    }
    emitReady(api);
    return api;
  }

  return Object.freeze({
    CONTRACT: CONTRACT,
    MODES: MODES.slice(),
    ACTION_CAPABILITY: ACTION_CAPABILITY,
    register: register,
    current: function () { return current; }
  });
});
