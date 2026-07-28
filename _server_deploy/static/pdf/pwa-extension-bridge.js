/* pwa-extension-bridge.js — PWA 书籍宿主页世界 ↔ 扩展隔离世界。
 *
 * 两阶段接管：
 *   HELLO    只读取书籍宿主状态，绝不隐藏 PWA；
 *   TAKEOVER 仅在扩展的 shell + adapter 都挂载成功后切换 UI 所有权。
 * TAKEOVER 后扩展每 5 秒 HEARTBEAT；15 秒失联或 GOODBYE 会恢复 PWA。
 *
 * 普通网页不走本桥。它只接受 book-host/1 注册的 pdf/epub/html/favorite，
 * 页面仍拥有书籍渲染、坐标、锚、高亮投影、墨迹、书籍便签和落点。 */
(() => {
  'use strict';

  const PROTOCOL = 'bw-reader-pwa/1';
  const BOOK_CONTRACT = 'book-host/1';
  const TO_EXTENSION = 'to-extension';
  const TO_PAGE = 'to-page';
  const ALLOWED_MODES = new Set(['pdf', 'epub', 'html', 'favorite']);
  // LOCAL_ACTION 完成后只广播该动作确实可能改变的轻量状态。尤其 anchor_fx 是
  // 高频拖拽预览，若每帧重读 selection/currentLocation，会把落点反馈变成整页查询。
  const SELECTION_SIDE_EFFECT_ACTIONS = new Set([
    'clear_selection', 'ocr', 'highlight', 'flash_selection'
  ]);
  const LOCATION_SIDE_EFFECT_ACTIONS = new Set([
    'jump_page', 'jump_location', 'change_page', 'jump_context',
    'fit_width', 'zoom_by', 'toggle_layout', 'toggle_crop',
    'flash_selection'
  ]);
  const LEASE_MS = 15000;
  const root = document.documentElement;
  let active = false;
  let leaseAt = 0;
  let leaseTimer = 0;
  let lastSelectionJson = '';
  let lastLocationJson = '';
  let locationTimer = 0;
  let patchedAdapter = null;
  let selectionListening = false;
  const patchedMethods = new Map();

  function post(type, payload, id) {
    window.postMessage({
      protocol: PROTOCOL,
      direction: TO_EXTENSION,
      type,
      payload: payload == null ? null : payload,
      id: id || null
    }, location.origin);
  }
  function api() {
    const value = window.__bwReaderLocalApi || null;
    if (!value || value.contract !== BOOK_CONTRACT || !ALLOWED_MODES.has(String(value.mode || ''))) return null;
    return value;
  }
  function adapter() {
    try { return window.RC && RC.adapter ? RC.adapter() : null; }
    catch (_) { return null; }
  }
  function state() {
    const host = api();
    if (!host) return null;
    let selection = null;
    let context = null;
    let currentLocation = null;
    try { selection = host.selection(); } catch (_) {}
    try { context = host.context(); } catch (_) {}
    try { currentLocation = host.currentLocation(); } catch (_) {}
    return {
      contract: host.contract,
      mode: String(host.mode),
      file: String(host.file || (context && context.file) || ''),
      url: location.href,
      title: String(host.title || document.title || ''),
      selection,
      context,
      currentLocation,
      capabilities: host.capabilities || {}
    };
  }
  function emitLocation(force) {
    const host = api();
    let currentLocation = null;
    try { currentLocation = host && host.currentLocation ? host.currentLocation() : null; } catch (_) {}
    let now = '';
    try { now = JSON.stringify(currentLocation); } catch (_) {}
    if (!force && now === lastLocationJson) return;
    lastLocationJson = now;
    post('LOCATION', currentLocation);
  }
  function emitSelection(force) {
    const host = api();
    let selection = null;
    try { selection = host && host.selection ? host.selection() : null; } catch (_) {}
    let now = '';
    try { now = JSON.stringify(selection); } catch (_) {}
    if (!force && now === lastSelectionJson) return;
    lastSelectionJson = now;
    post('SELECTION', selection);
  }
  function cleanOptions(options) {
    options = options || {};
    const out = {};
    ['word', 'text', 'context', 'file', 'page', 'jp', 'noHighlight', 'noBreathe', 'focusAction']
      .forEach((key) => {
        const value = options[key];
        if (value == null || typeof value === 'function' || typeof value === 'object') return;
        out[key] = value;
      });
    if (Array.isArray(options.langs)) out.langs = options.langs.slice(0, 8).map(String);
    return out;
  }

  function handoffAdapter() {
    const current = adapter();
    if (!current || patchedAdapter === current) return;
    restoreAdapter();
    patchedAdapter = current;
    [
      'lookupWord', 'lookupPhrase', 'translate', 'explain',
      'chat', 'openFullDict', 'openModelSettings'
    ].forEach((name) => {
      if (typeof current[name] !== 'function') return;
      const original = current[name];
      const replacement = function (options) {
        let selection = null;
        try { selection = api()?.selection() || null; } catch (_) {}
        post('ACTION', {
          action: name,
          options: cleanOptions(options),
          selection
        });
      };
      patchedMethods.set(name, { original, replacement });
      current[name] = replacement;
    });
    current.__bwExtensionHandoff = true;
  }
  function restoreAdapter() {
    if (!patchedAdapter) return;
    patchedMethods.forEach((record, name) => {
      if (patchedAdapter[name] === record.replacement) patchedAdapter[name] = record.original;
    });
    try { delete patchedAdapter.__bwExtensionHandoff; } catch (_) {}
    patchedMethods.clear();
    patchedAdapter = null;
  }

  function installCss() {
    if (document.getElementById('bw-extension-handoff-css')) return;
    const css = document.createElement('style');
    css.id = 'bw-extension-handoff-css';
    css.textContent = `
html[data-bw-reader-extension-active="1"] #header,
html[data-bw-reader-extension-active="1"] #ep-top,
html[data-bw-reader-extension-active="1"] #html-top,
html[data-bw-reader-extension-active="1"] #fs-restore,
html[data-bw-reader-extension-active="1"] #ink-fab,
html[data-bw-reader-extension-active="1"] #sel-toolbar,
html[data-bw-reader-extension-active="1"] #ep-sel,
html[data-bw-reader-extension-active="1"] #html-sel,
html[data-bw-reader-extension-active="1"] #result-mask,
html[data-bw-reader-extension-active="1"] #word-pop,
html[data-bw-reader-extension-active="1"] #phrase-pop,
html[data-bw-reader-extension-active="1"] #grammar-panel,
html[data-bw-reader-extension-active="1"] #side-handle,
html[data-bw-reader-extension-active="1"] #ep-side,
html[data-bw-reader-extension-active="1"] #ep-side-handle,
html[data-bw-reader-extension-active="1"] #draft-badge,
html[data-bw-reader-extension-active="1"] #draft-mask,
html[data-bw-reader-extension-active="1"] .rc-topbar-pill[data-rc-native-topbar-pill="1"] {
  display:none !important;
}
html[data-bw-reader-extension-active="1"] body.grammar-open #main,
html[data-bw-reader-extension-active="1"] body.grammar-open #header {
  padding-right:0 !important;
}
`;
    document.head.appendChild(css);
  }
  function closeOverlappingUi() {
    try { if (window.RC && RC.sidedrawer && RC.sidedrawer.close) RC.sidedrawer.close(); } catch (_) {}
    try { document.body.classList.remove('grammar-open'); } catch (_) {}
    try { document.getElementById('sel-toolbar')?.classList.remove('open'); } catch (_) {}
    try { document.getElementById('ep-sel')?.classList.remove('open'); } catch (_) {}
    try { document.getElementById('html-sel')?.classList.remove('open'); } catch (_) {}
  }
  function onSelectionEvent() {
    if (!active) return;
    setTimeout(() => emitSelection(false), 0);
  }
  function onLocationEvent() {
    if (!active || locationTimer) return;
    locationTimer = window.setTimeout(() => {
      locationTimer = 0;
      emitLocation(false);
    }, 80);
  }
  function listenSelection() {
    if (selectionListening) return;
    selectionListening = true;
    document.addEventListener('selectionchange', onSelectionEvent, true);
    document.addEventListener('pointerup', onSelectionEvent, true);
    document.addEventListener('keyup', onSelectionEvent, true);
    // PDF/EPUB 都可能在内部滚动容器中改变当前位置；捕获 scroll 后只广播轻量
    // currentLocation，不重复传 figures/context 等大对象。
    document.addEventListener('scroll', onLocationEvent, true);
  }
  function unlistenSelection() {
    if (!selectionListening) return;
    selectionListening = false;
    document.removeEventListener('selectionchange', onSelectionEvent, true);
    document.removeEventListener('pointerup', onSelectionEvent, true);
    document.removeEventListener('keyup', onSelectionEvent, true);
    document.removeEventListener('scroll', onLocationEvent, true);
    if (locationTimer) window.clearTimeout(locationTimer);
    locationTimer = 0;
  }
  function renewLease() {
    leaseAt = Date.now();
    if (leaseTimer) return;
    leaseTimer = window.setInterval(() => {
      if (active && Date.now() - leaseAt > LEASE_MS) deactivate('lease-expired');
    }, 2500);
  }
  function stopLease() {
    leaseAt = 0;
    if (leaseTimer) window.clearInterval(leaseTimer);
    leaseTimer = 0;
  }

  function activate() {
    const host = api();
    if (!host || !root.dataset.bwReaderExtension) return false;
    renewLease();
    if (active) return true;
    installCss();
    closeOverlappingUi();
    handoffAdapter();
    listenSelection();
    active = true;
    root.dataset.bwReaderExtensionActive = '1';
    root.dataset.bwReaderUiOwner = 'extension';
    window.__BW_EXTENSION_HANDOFF__ = Object.freeze({
      protocol: PROTOCOL,
      contract: BOOK_CONTRACT,
      active: true,
      mode: host.mode,
      capabilities: host.capabilities
    });
    document.dispatchEvent(new CustomEvent('bw:book-ui-owner-changed', {
      detail: { owner: 'extension', mode: host.mode }
    }));
    emitSelection(true);
    emitLocation(true);
    return true;
  }
  function deactivate(reason) {
    if (!active) {
      stopLease();
      return;
    }
    active = false;
    stopLease();
    unlistenSelection();
    restoreAdapter();
    delete root.dataset.bwReaderExtensionActive;
    root.dataset.bwReaderUiOwner = 'pwa';
    try { delete window.__BW_EXTENSION_HANDOFF__; } catch (_) {}
    document.dispatchEvent(new CustomEvent('bw:book-ui-owner-changed', {
      detail: { owner: 'pwa', reason: String(reason || 'disconnected') }
    }));
  }

  async function command(message) {
    const id = message.id || null;
    try {
      const host = api();
      if (!host) throw new Error('当前页面没有可接管的书籍宿主');
      let result = null;
      if (message.type === 'HELLO' || message.type === 'GET_STATE') {
        result = state();
        if (message.type === 'HELLO') post('READY', result, id);
        else post('RESULT', { ok: true, result }, id);
        return;
      }
      if (message.type === 'TAKEOVER') {
        if (!activate()) throw new Error('扩展标记或书籍宿主尚未就绪');
        result = { owner: 'extension', leaseMs: LEASE_MS, state: state() };
      } else if (message.type === 'HEARTBEAT') {
        if (!active) throw new Error('扩展尚未取得书籍 UI 所有权');
        renewLease();
        result = { owner: 'extension', leaseMs: LEASE_MS };
      } else if (message.type === 'GOODBYE') {
        deactivate('extension-goodbye');
        result = { owner: 'pwa' };
      } else {
        if (!active) throw new Error('扩展尚未取得书籍 UI 所有权');
        renewLease();
        if (message.type === 'GET_CONTEXT') {
          const context = host.context() || {};
          let currentLocation = null;
          try { currentLocation = host.currentLocation(); } catch (_) {}
          result = Object.assign({}, context, {
            current_location: currentLocation,
            currentLocation
          });
        }
        else if (message.type === 'CLEAR_SELECTION') result = await host.localAction('clear_selection', {});
        else if (message.type === 'LOCAL_ACTION') {
          const payload = message.payload || {};
          const action = String(payload.action || '');
          result = await host.localAction(action, payload.payload || {});
          if (SELECTION_SIDE_EFFECT_ACTIONS.has(action)) emitSelection(true);
          if (LOCATION_SIDE_EFFECT_ACTIONS.has(action)) emitLocation(true);
        } else {
          return;
        }
      }
      post('RESULT', { ok: true, result }, id);
    } catch (error) {
      post('RESULT', { ok: false, error: String(error?.message || error) }, id);
    }
  }

  window.addEventListener('message', (event) => {
    if (event.source !== window || event.origin !== location.origin) return;
    const message = event.data;
    if (!message || message.protocol !== PROTOCOL || message.direction !== TO_PAGE) return;
    command(message);
  });
  window.addEventListener('pagehide', () => deactivate('pagehide'), { once: true });
  document.addEventListener('bw:reader-local-api-ready', () => {
    if (root.dataset.bwReaderExtension) post('HOST_READY', state());
  });
  if (root.dataset.bwReaderExtension && api()) post('HOST_READY', state());
})();
