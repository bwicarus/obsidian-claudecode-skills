// 书籍 PWA 适配器：扩展拥有共享 UI/网络；页面只提供当前 DocumentHost
// 明确公开的本地能力。PDF/EPUB/HTML/收藏入口都由 mode+capabilities 描述，
// 不再把网页阅读器伪装成 PDF，也不保留 web host 分支。
(function () {
  'use strict';
  if (window.__bwPwaProviderOnly) return;
  var RC = window.RC, bridge = window.__bwPwaBridge;
  if (!RC || !RC.use || !bridge) return;
  var shadow = window.__bwShadow;
  var cachedContext = null;
  var hostReady = false;
  var shellReady = false;
  var takeoverStarted = false;
  var shellWatchTimer = 0;
  var BOOK_MODES = new Set(['pdf', 'epub', 'html', 'favorite']);
  var hostMode = (function () {
    try {
      var value = document.querySelector('meta[name="bw-reader-app"]')?.getAttribute('content');
      return BOOK_MODES.has(value) ? value : 'pdf';
    } catch (_) { return 'pdf'; }
  })();
  var hostCapabilities = {};

  function hasCapability(name) {
    return !!hostCapabilities[String(name || '')];
  }
  function canNavigate() {
    return hasCapability('navigation') ||
      hasCapability('jumpPage') ||
      hasCapability('jumpLocation');
  }
  function locationPayload(value) {
    if (value && typeof value === 'object') return value;
    var numeric = Number(value);
    if (Number.isFinite(numeric)) {
      return { index: Math.max(0, Math.floor(numeric) - 1) };
    }
    return { anchor: String(value || '') };
  }
  function adapterConfig() {
    var isPdf = hostMode === 'pdf';
    return {
      isPDF: isPdf,
      reflow: !isPdf,
      hasFigures: hasCapability('figures') || isPdf,
      hasFormula: true,
      renderRegion: hasCapability('renderRegion') || isPdf,
      dictMode: 'sse',
      popupMode: 'fixed',
      clickWordDetect: false,
      anchorKind: isPdf ? 'pdf-char' : (hostMode === 'epub' ? 'epub-cfi' : 'book-quote'),
      supportsVoice: true
    };
  }
  function applyHostState(st) {
    st = st || {};
    hostMode = BOOK_MODES.has(st.mode) ? st.mode : hostMode;
    hostCapabilities = st.capabilities && typeof st.capabilities === 'object'
      ? Object.assign({}, st.capabilities) : {};
    PwaAdapter.kind = 'pwa-' + hostMode;
    PwaAdapter.config = adapterConfig();
    configureVoiceLog();
  }

  function refreshContext() {
    bridge.context().then(function (c) {
      cachedContext = c || cachedContext;
      var location = c?.current_location || c?.currentLocation;
      if (location && bridge.state) bridge.state.currentLocation = location;
    }).catch(function () {});
  }
  function sel() {
    var s = bridge.selection;
    if (!s || !s.text) return null;
    return RC.contract.selection({
      text: s.text, sentence: s.sentence || '', context: s.context || s.sentence || '',
      anchor: s.anchor || null, rect: s.rect || null, page: s.page || 0,
      file: s.file || '', langs: s.langs || [], kind: s.kind || 'multi', shortPhrase: !!s.shortPhrase
    });
  }
  function fileInfo() {
    var s = bridge.selection || {}, st = bridge.state || {};
    return { file: s.file || String(st.file || ''), langs: s.langs || [] };
  }
  function openTab(name) { try { RC.sidedrawer.open(name); } catch (_) {} }
  function noop() {}
  function positiveRect(element) {
    if (!element || element.isConnected === false || typeof element.getBoundingClientRect !== 'function') return false;
    try {
      var rect = element.getBoundingClientRect();
      return Number(rect.width) > 0 && Number(rect.height) > 0;
    } catch (_) { return false; }
  }
  function shellSurfaceReady() {
    if (!shadow) return false;
    var header = shadow.getElementById('header');
    var pill = shadow.getElementById('bw-top-pill');
    if (!header || !pill || header.isConnected === false || pill.isConnected === false) return false;
    // A deliberately collapsed header has zero height; its visible pill is the
    // complete recovery surface. Otherwise both controls need real geometry.
    return positiveRect(pill) && (
      header.classList?.contains('rc-topbar-collapsed') || positiveRect(header)
    );
  }
  function checkShellSurface() {
    if (document.visibilityState && document.visibilityState !== 'visible') return;
    shellReady = shellSurfaceReady();
    if (!shellReady && bridge.takenOver) {
      bridge.release();
      takeoverStarted = false;
      if (window.__bwRoot) window.__bwRoot.dataset.pwaTakeover = 'shell-lost';
      return;
    }
    if (shellReady && hostReady && !bridge.takenOver) tryTakeover();
  }
  function ensureShellWatch() {
    if (shellWatchTimer || typeof setInterval !== 'function') return;
    shellWatchTimer = setInterval(checkShellSurface, 1000);
  }
  function tryTakeover() {
    shellReady = shellSurfaceReady();
    if (!hostReady || !shellReady || takeoverStarted || typeof bridge.takeover !== 'function') return;
    takeoverStarted = true;
    bridge.takeover().then(function () {
      if (!shellSurfaceReady()) {
        bridge.release();
        takeoverStarted = false;
        if (window.__bwRoot) window.__bwRoot.dataset.pwaTakeover = 'shell-lost';
        return;
      }
      ensureShellWatch();
      if (window.__bwRoot) window.__bwRoot.dataset.pwaTakeover = 'ready';
      document.dispatchEvent(new CustomEvent('bw:pwa-takeover-ready'));
    }).catch(function (error) {
      takeoverStarted = false;
      if (window.__bwRoot) {
        window.__bwRoot.dataset.pwaTakeover = 'failed';
        window.__bwRoot.dataset.pwaTakeoverError = String(error?.message || error).slice(0, 240);
      }
      // TAKEOVER 失败时页面仍保留原生共享 UI；不要制造“双边都隐藏”的空白状态。
      try { RC.toast('扩展接管未完成，已保留阅读器界面'); } catch (_) {}
    });
  }

  function highlightUrl() {
    if (hostMode === 'epub' || hostMode === 'favorite') return '/pdf/api/epub-highlights';
    if (hostMode === 'html') return '/pdf/api/html-highlights';
    return '/pdf/api/highlights';
  }
  function assistantUrl(kind) {
    var epub = hostMode === 'epub' || hostMode === 'favorite';
    var file = encodeURIComponent(fileInfo().file || '');
    if (kind === 'chat') return epub ? '/pdf/api/epub-assistant' : '/api/assistant/chat';
    if (kind === 'history') return epub ? '/pdf/api/epub-convo?file=' + file : '/api/assistant/history';
    return epub ? '/pdf/api/epub-convo/clear?file=' + file : '/api/assistant/clear';
  }
  function epubVoiceLog(q, a, page) {
    var send = window.__bwReaderFetch || window.fetch;
    if (typeof send !== 'function') return;
    [['user', q], ['assistant', a]].forEach(function (part) {
      if (!part[1]) return;
      try {
        Promise.resolve(send.call(window, '/pdf/api/epub-convo/append', {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          keepalive: true,
          body: JSON.stringify({
            file: fileInfo().file || '',
            role: part[0],
            content: String(part[1]).slice(0, 4000),
            section: (page || 1) - 1
          })
        })).catch(noop);
      } catch (_) {}
    });
  }
  function configureVoiceLog() {
    var asst = PwaAdapter && PwaAdapter._host && PwaAdapter._host.asst;
    if (!asst) return;
    if (hostMode === 'epub' || hostMode === 'favorite') {
      // EPUB/Favorite 的历史按书保存，复用原生阅读器既有 append 端点。
      asst.voiceLog = epubVoiceLog;
    } else {
      // PDF/HTML 不提供自定义钩子：让 rc-assistant 的默认 /api/assistant/log
      // 保存 user + assistant + turn_id + parts。truthy noop 会把这条完整路径吞掉。
      delete asst.voiceLog;
    }
  }
  var PwaAdapter = {
    kind: 'pwa-' + hostMode,
    config: adapterConfig(),
    getEndpoints: function () {
      return RC.contract.endpoints({ highlights: highlightUrl() });
    },
    fileInfo: fileInfo,
    captureSelection: sel,
    clearSelection: function () { bridge.clearSelection().catch(noop); },
    getContext: function () {
      var c = cachedContext ? Object.assign({}, cachedContext) : {};
      var s = sel();
      if (s) {
        c.selection = s.text; c.selection_sentence = s.sentence || s.context || '';
        c.selection_anchor = s.anchor || undefined;
      }
      c.file = c.file || fileInfo().file;
      c.book = c.book || document.title; c.book_name = c.book_name || document.title;
      return c;
    },
    currentLocation: function () {
      var s = sel();
      var c = cachedContext || {};
      if (bridge.state?.currentLocation) {
        return Object.assign({}, bridge.state.currentLocation);
      }
      if (c.current_location && typeof c.current_location === 'object') {
        return Object.assign({}, c.current_location);
      }
      return {
        unit: hostMode === 'pdf' ? 'page' : 'section',
        index: hostMode === 'pdf'
          ? Math.max(0, Number((s && s.page) || c.page || 1) - 1)
          : Number(c.index || c.section || 0),
        total: Number(c.total_pages || c.total_sections || c.total_locations || 0)
      };
    },
    collectFigures: function () { return (cachedContext && cachedContext.figures) || []; },
    localAction: function (name, payload) { return bridge.local(name, payload); },
    _host: { asst: {
      md: function (t) { return RC.md ? RC.md(t) : String(t || ''); },
      toast: function (m) { try { RC.toast(m); } catch (_) {} },
      fmtTime: function () { return ''; }, fileRel: function () { return fileInfo().file; },
      pdfNumPages: function () { return (cachedContext && cachedContext.total_pages) || 0; },
      locCount: function () {
        return (cachedContext && (
          cachedContext.total_pages ||
          cachedContext.total_sections ||
          cachedContext.total_locations
        )) || 0;
      },
      dispPage: function (p) { return p; }, pdfFromDisp: function (p) { return p; },
      goTo: function (p) {
        if (!canNavigate()) return;
        if (hostMode === 'pdf') bridge.local('jump_page', {page:p}).catch(noop);
        else bridge.local('jump_location', {location:locationPayload(p)}).catch(noop);
      },
      goToInBook: function (file, p) {
        if (canNavigate()) {
          var context = {file:file,page:p};
          bridge.local('jump_context', {context:context,record:context}).catch(noop);
        }
      },
      changePage: function (d) {
        if (canNavigate() && ['pdf', 'epub', 'favorite'].includes(hostMode)) {
          bridge.local('change_page', {delta:d}).catch(noop);
        }
      },
      fitWidth: function () { if (hasCapability('zoom')) bridge.local('fit_width').catch(noop); },
      zoomBy: function (d) { if (hasCapability('zoom')) bridge.local('zoom_by', {delta:d}).catch(noop); },
      toggleTranslate: function () { if (hasCapability('pageTranslate')) bridge.local('toggle_page_translate').catch(noop); },
      openDrawer: function () { openTab('asst'); }, switchTab: openTab,
      asstOpen: function () { try { return !!shadow.querySelector('.ep-side-pane[data-pane="asst"].active'); } catch (_) { return false; } },
      voiceContext: function () { return cachedContext; },
      setFocusSel: function (t) { window.__focusSel = t ? { text: t } : null; },
      focusSel: function () { return window.__focusSel || null; },
      clearFigFocus: noop, figThumb: noop,
      noteAttached: function () { return []; }, clearNoteAttached: noop, renderNoteChips: noop,
      notesReload: noop, noteInject: function () { return false; },
      reloadHighlights: noop, loadAllHighlights: noop, renderHighlightsOnPage: noop, showHlPicker: noop,
      assistEdit: noop, renderPhraseHl: noop, removePhraseHighlight: noop,
      activePhraseHl: function () { return null; }, setActivePhraseHl: noop,
      charsRangeToText: function () { return ''; }, charRangeToPtRects: function () { return []; },
      flashSelOnPage: function (p,t) { if (canNavigate()) bridge.local('flash_selection', {page:p,text:t}).catch(noop); },
      noteNearText: function () { return ''; }, jumpToCtx: function (m) {
        if (canNavigate()) bridge.local('jump_context', {context:m||{},record:m||{}}).catch(noop);
      },
      prewarm: function (off) { try { window.__bwReaderFetch('/api/assistant/prewarm', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(off ? {off:1} : {}) }); } catch (_) {} },
      getPaidNoted: function () { return !!window.__paidNoted; }, setPaidNoted: function (v) { window.__paidNoted = v; },
      showAction: function () { return null; }, queueAction: noop, taskAction: noop,
      mountPanel: function () { return shadow ? shadow.getElementById('ep-side') : null; },
      mountTabs: function () { return shadow ? shadow.getElementById('ep-side-tabs') : null; },
      hlUrl: highlightUrl,
      notesUrl: function () { return '/pdf/api/notes'; },
      noteCompositeUrl: function () { return '/pdf/api/note-composite'; },
      chatUrl: function () { return assistantUrl('chat'); },
      historyUrl: function () { return assistantUrl('history'); },
      clearUrl: function () { return assistantUrl('clear'); }
    }}
  };
  configureVoiceLog();

  function bindBridgeActions() {
    if (hasCapability('highlight') || hasCapability('pdfHighlight')) RC.actions.bind('highlight.save', function (p) { return bridge.local('highlight', p); }, { owner: 'pwa', runtime: 'extension', storage: 'book-sidecar' });
    if (hasCapability('ink')) RC.actions.bind('ink.toggle', function () { return bridge.local('toggle_ink'); }, { owner: 'pwa', runtime: 'extension', storage: 'book-sidecar' });
    if (hasCapability('stickyNote')) RC.actions.bind('note.create', function () { return bridge.local('create_sticky'); }, { owner: 'pwa', runtime: 'extension', storage: 'book-sidecar' });
    if (hasCapability('ruby')) RC.actions.bind('reading.ruby.toggle', function () { return bridge.local('toggle_ruby'); }, { owner: 'pwa', runtime: 'extension', storage: 'device-local' });
    if (hasCapability('pageTranslate')) RC.actions.bind('translation.page.toggle', function () { return bridge.local('toggle_page_translate'); }, { owner: 'pwa', runtime: 'extension', storage: 'device-local' });
  }

  function ready(st) {
    bridge.state = st || bridge.state;
    applyHostState(bridge.state);
    RC.use(PwaAdapter); bindBridgeActions(); refreshContext();
    if (window.__bwRoot) window.__bwRoot.dataset.pwaBridge = 'ready';
    // rc-flashcard 仍拥有卡片 UI；“钉到书页”的实际存储/锚点交回 PDF 的 rc-stickynote。
    RC.stickynote = RC.stickynote || {};
    if (hasCapability('pinCard')) {
      RC.actions.bind('pin.card', function (p) {
        if (!p.gid) p.gid = 'fcg_' + ((RC.voiceCard && RC.voiceCard.mkCid) ? RC.voiceCard.mkCid() : Date.now().toString(36));
        p.cid = p.gid;   // 学习卡外壳和状态机使用同一主键
        bridge.local('pin_card', p).catch(function (e) { RC.toast(e.message || '钉住失败'); });
        return true;
      }, { owner: 'pwa', runtime: 'extension', storage: 'book-sidecar' });
      RC.stickynote.createCardAt = function (x, y, cards, gid) {
        var stable = gid || ('fcg_' + ((RC.voiceCard && RC.voiceCard.mkCid) ? RC.voiceCard.mkCid() : Date.now().toString(36)));
        return RC.actions.run('pin.card', { x: x, y: y, cards: cards, gid: stable, cid: stable });
      };
    }
    if (hasCapability('pinHtmlCard')) {
      RC.actions.bind('pin.html', function (p) {
        if (!p.html || !p.html.content) return false;
        p.html.cid = p.html.cid || p.cid || ((RC.voiceCard && RC.voiceCard.mkCid) ? RC.voiceCard.mkCid() : ('c' + Date.now().toString(36)));
        p.cid = p.html.cid;
        bridge.local('pin_html', p).catch(function (e) { RC.toast(e.message || '工具卡粘贴失败'); });
        return true;
      }, { owner: 'pwa', runtime: 'extension', storage: 'book-sidecar' });
      RC.stickynote.createHtmlAt = function (x, y, htmlObj) {
        return RC.actions.run('pin.html', { x: x, y: y, html: htmlObj });
      };
    }
    // 侧栏卡拖过 PDF 时，把实时落点反馈转交给页面本体的精确字符/行锚层。
    var fxRaf = 0;
    var fxInFlight = false;
    var fxPendingShow = null;
    var fxHidePending = false;
    function fxSchedule() {
      if (fxInFlight || fxRaf || (!fxPendingShow && !fxHidePending)) return;
      fxRaf = requestAnimationFrame(fxDrain);
    }
    function fxFinished() {
      fxInFlight = false;
      // show 坐标只保留最新一份；若拖拽已经结束，则在最新 show 送达后再送最终 hide。
      // 成功和失败都继续排空，避免一次桥接错误把页面光带永久留住。
      fxSchedule();
    }
    function fxDrain() {
      fxRaf = 0;
      if (fxInFlight) return;
      var payload = null;
      if (fxPendingShow) {
        payload = fxPendingShow;
        fxPendingShow = null;
      } else if (fxHidePending) {
        payload = { show: false };
        fxHidePending = false;
      }
      if (!payload) return;
      fxInFlight = true;
      try {
        Promise.resolve(bridge.local('anchor_fx', payload)).then(fxFinished, fxFinished);
      } catch (_) {
        fxFinished();
      }
    }
    if (hasCapability('anchorFx')) RC.actions.bind('pin.anchorFx', function (p) {
      if (p.show) {
        fxPendingShow = { show: true, x: p.x, y: p.y };
        // 新 show 表示另一段拖拽仍在进行；尚未发出的旧 hide 已被最新可见状态取代。
        fxHidePending = false;
      } else {
        // 不丢弃尚未送达的最新坐标；它完成后必须再串行发送一次 hide。
        fxHidePending = true;
      }
      fxSchedule();
      return true;
    }, { owner: 'pwa', runtime: 'extension', storage: 'none' });
    if (hasCapability('anchorFx')) {
      RC.stickynote.anchorFx = {
        show: function (x, y) { return RC.actions.run('pin.anchorFx', { show: true, x: x, y: y }); },
        hide: function () { return RC.actions.run('pin.anchorFx', { show: false }); }
      };
    }
    document.dispatchEvent(new CustomEvent('bw:adapter-ready'));
    hostReady = true;
    tryTakeover();
  }
  // 先占住统一适配器槽，避免极慢设备上 shell 在 READY 前误挂到空适配器；数据随后由 ready() 刷新。
  RC.use(PwaAdapter); bindBridgeActions();
  bridge.on('SELECTION', function () { refreshContext(); });
  document.addEventListener('bw:shell-ready', function () {
    shellReady = shellSurfaceReady();
    ensureShellWatch();
    tryTakeover();
  });
  document.addEventListener('visibilitychange', checkShellSurface);
  if (typeof window.addEventListener === 'function') {
    window.addEventListener('pagehide', function () {
      if (shellWatchTimer) clearInterval(shellWatchTimer);
      shellWatchTimer = 0;
    }, { once: true });
  }
  if (bridge.ready) ready(bridge.state); else bridge.on('READY', ready);
  window.__pwaAdapter = PwaAdapter;
})();
