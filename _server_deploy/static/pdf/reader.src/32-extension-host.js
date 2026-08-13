// ═══════════ 32-extension-host.js — PDF 书籍宿主能力白名单 ═══════════
// 仍在 reader.js 模块作用域内，因此只做薄适配，复用现有 PDF 几何、sidecar、墨迹、
// 便签和导航函数。旧 PWA 网页壳明确不登记为书籍宿主；普通网页由扩展直接处理。
(() => {
  if (window.__PDF_CFG && window.__PDF_CFG.web_url) return;
  if (!window.BWReaderBookHost || window.__bwReaderLocalApi) return;

  function selection() {
    try {
      const adapter = window.PdfAdapter;
      const s = adapter && adapter.captureSelection ? adapter.captureSelection() : null;
      if (!s || !s.text) return null;
      const text = String(s.text || '').trim();
      const sentence = String(s.context || s.ctx || s.sentence || window.__lastSelSentence || '').trim();
      return {
        text,
        sentence,
        context: sentence || text,
        rect: s.rect && (s.rect.client || s.rect),
        anchor: s.anchor || null,
        file: typeof FILE_REL !== 'undefined' ? FILE_REL : '',
        book: document.title || '',
        langs: (typeof BOOK_LANGS !== 'undefined' && Array.isArray(BOOK_LANGS)) ? BOOK_LANGS.slice() : [],
        page: (s.anchor && Number(s.anchor.page))
          || (typeof _selPageNum === 'function' ? _selPageNum() : currentPage) || 0,
      };
    } catch (_) { return null; }
  }

  function context() {
    try {
      const adapter = window.PdfAdapter;
      const value = adapter && adapter.getContext ? adapter.getContext() : null;
      return value ? JSON.parse(JSON.stringify(value)) : null;
    } catch (_) { return null; }
  }

  function currentLocation() {
    let total = 0;
    try { total = Number(pdfDoc && pdfDoc.numPages) || 0; } catch (_) {}
    return { unit: 'page', index: Math.max(0, (Number(currentPage) || 1) - 1), total };
  }

  function _bookRemoveHighlightProjection(id) {
    id = String(id || '');
    if (!id) return { ok: false, error: '缺少高亮 id' };
    const before = _allHighlights.length;
    _allHighlights = _allHighlights.filter((item) => item && String(item.id) !== id);
    Object.keys(_hlByPage).forEach((page) => {
      _hlByPage[page] = (_hlByPage[page] || []).filter((item) => item && String(item.id) !== id);
    });
    document.querySelectorAll('.hl-saved[data-id]').forEach((node) => {
      if (String(node.dataset.id || '') === id) node.remove();
    });
    return { ok: true, id, removed: before !== _allHighlights.length };
  }

  async function action(name, payload) {
    payload = payload || {};
    switch (name) {
      case 'ocr':
        if (window.onOcrSel) await window.onOcrSel();
        return { selection: selection() };
      case 'highlight': {
        if (!_charSel || !_charSel.pw) throw new Error('没有可标记的 PDF 选区');
        const colors = getHlColors();
        const color = String(payload.color || _activeHlColor || _lastHlColor || colors[0] || '#fff59d');
        const h = await saveHighlight({
          pw: _charSel.pw,
          sIdx: _charSel.startIdx,
          eIdx: _charSel.endIdx,
          color,
          kind: String(payload.kind || 'note'),
          sentence: String(payload.sentence || ''),
          body: String(payload.body || ''),
          note: String(payload.note || ''),
        });
        if (h) {
          _charSel.pw.querySelector('.sel-overlay')?.replaceChildren();
          _charSel = null;
          lastSelText = '';
          _updateSelPreview('');
          toolbar.classList.remove('open');
          _toast('已标记 🖌');
        }
        return {
          ok: !!h,
          highlight: h ? { id: h.id, page: h.page, color: h.color, text: h.text } : null,
        };
      }
      case 'remove_highlight':
        return _bookRemoveHighlightProjection(payload.id);
      case 'open_search':
        if (window.openSearch) window.openSearch();
        return { ok: true };
      case 'toggle_ruby':
        if (window.toggleRuby) window.toggleRuby();
        return {
          ok: true,
          ruby: typeof _rubyEnabled === 'function' ? !!_rubyEnabled() : false,
          translate: typeof _pageTrOn !== 'undefined' ? !!_pageTrOn : false,
        };
      case 'toggle_page_translate':
        if (window.togglePageTranslate) window.togglePageTranslate();
        return {
          ok: true,
          ruby: typeof _rubyEnabled === 'function' ? !!_rubyEnabled() : false,
          translate: typeof _pageTrOn !== 'undefined' ? !!_pageTrOn : false,
        };
      case 'create_sticky':
        if (!window._noteCreateAtCenter) throw new Error('PDF 便签层尚未就绪');
        window._noteCreateAtCenter();
        return { ok: true };
      case 'toggle_ink':
        if (!window.inkToggle) throw new Error('PDF 绘图层尚未就绪');
        window.inkToggle();
        return { ok: true, active: document.body.classList.contains('ink-mode') };
      case 'anchor_fx':
        if (window.RC && RC.stickynote && RC.stickynote.anchorFx) {
          if (payload.show) RC.stickynote.anchorFx.show(Number(payload.x) || 0, Number(payload.y) || 0);
          else RC.stickynote.anchorFx.hide();
        }
        return { ok: true };
      case 'jump_page':
      case 'jump_location': {
        const locationPayload = payload.location || payload;
        const page = Math.max(1, Number(locationPayload.page)
          || (Number.isFinite(Number(locationPayload.index)) ? Number(locationPayload.index) + 1 : 1));
        if (window.jumpWithBack) window.jumpWithBack(page);
        else if (window.goToPage) window.goToPage(page);
        return { ok: true, page };
      }
      case 'change_page':
        if (window.changePage) window.changePage(Number(payload.delta) || 0);
        return { ok: true };
      case 'fit_width':
        if (window.fitWidth) window.fitWidth();
        return { ok: true };
      case 'zoom_by':
        if (window.zoomChange) window.zoomChange(Number(payload.delta) || 0);
        return { ok: true };
      case 'jump_context': {
        const target = payload.context || payload || {};
        const page = Math.max(1, Number(target.page || target.pdf_page || 1));
        const file = String(target.file || target.file_rel || '');
        if (file && typeof FILE_REL !== 'undefined' && file !== FILE_REL && window.openBookAt) {
          window.openBookAt(file, page);
        } else if (window.jumpWithBack) {
          window.jumpWithBack(page);
        }
        return { ok: true };
      }
      case 'flash_selection':
        if (typeof _flashSelOnPage === 'function') {
          _flashSelOnPage(Number(payload.page) || currentPage, String(payload.text || ''));
        }
        return { ok: true };
      case 'pin_card': {
        const cards = Array.isArray(payload.cards) ? payload.cards.slice(0, 50) : [];
        if (!cards.length) throw new Error('没有可钉住的卡片');
        if (!(window.RC && RC.stickynote && RC.stickynote.createCardAt)) {
          throw new Error('PDF 卡片便签尚未就绪');
        }
        const x = Number(payload.x) || (window.innerWidth || 1024) / 2;
        const y = Number(payload.y) || (window.innerHeight || 768) / 2;
        RC.stickynote.createCardAt(x, y, cards, String(payload.gid || ''));
        _toast('📌 已钉到书页');
        return { ok: true };
      }
      case 'pin_html': {
        const html = payload.html || {};
        const content = String(html.content || '');
        if (!content) throw new Error('没有可粘贴的工具卡内容');
        if (!(window.RC && RC.stickynote && RC.stickynote.createHtmlAt)) {
          throw new Error('PDF 工具卡便签尚未就绪');
        }
        const x = Number(payload.x) || (window.innerWidth || 1024) / 2;
        const y = Number(payload.y) || (window.innerHeight || 768) / 2;
        const ok = RC.stickynote.createHtmlAt(x, y, {
          content,
          isHtml: !!html.isHtml,
          label: String(html.label || '卡片'),
          type: String(html.type || ''),
          icon: String(html.icon || ''),
          form: String(html.form || 'full'),
          cid: String(html.cid || payload.cid || ''),
        });
        if (!ok) throw new Error('请把工具卡放到 PDF 正文上再松手');
        return { ok: true };
      }
      case 'toggle_fullscreen':
        if (window.toggleFullscreen) window.toggleFullscreen();
        return { ok: true };
      case 'open_settings':
        if (window.openSettings) window.openSettings();
        return { ok: true };
      case 'open_favorite':
        if (!window._favOpenPicker) throw new Error('收藏组件尚未就绪');
        window._favOpenPicker();
        return { ok: true };
      case 'create_user_page':
        if (!window._upCreate) throw new Error('插入页组件尚未就绪');
        window._upCreate();
        return { ok: true };
      case 'toggle_layout':
        if (window.toggleSpread) window.toggleSpread();
        return { ok: true };
      case 'toggle_crop':
        if (window.toggleCrop) window.toggleCrop();
        return { ok: true };
      case 'clear_selection':
        if (window.PdfAdapter && PdfAdapter.clearSelection) PdfAdapter.clearSelection();
        return { ok: true };
      default:
        throw new Error('不允许的 PDF 本地命令：' + name);
    }
  }

  const names = [
    'ocr', 'highlight', 'remove_highlight', 'open_search', 'toggle_ruby', 'toggle_page_translate',
    'create_sticky', 'toggle_ink', 'anchor_fx', 'jump_page', 'jump_location',
    'change_page', 'fit_width', 'zoom_by', 'jump_context', 'flash_selection',
    'pin_card', 'pin_html', 'toggle_fullscreen', 'open_settings', 'open_favorite',
    'create_user_page', 'toggle_layout', 'toggle_crop', 'clear_selection',
  ];
  const actions = {};
  names.forEach((name) => { actions[name] = (payload) => action(name, payload); });

  const localApi = BWReaderBookHost.register({
    mode: 'pdf',
    file: typeof FILE_REL !== 'undefined' ? FILE_REL : '',
    title: document.title || '',
    langs: (typeof BOOK_LANGS !== 'undefined' && Array.isArray(BOOK_LANGS)) ? BOOK_LANGS.slice() : [],
    selection,
    context,
    currentLocation,
    actions,
    capabilities: {
      selection: true,
      context: true,
      highlight: true,
      pdfHighlight: true,
      selectionOcr: true,
      bookSearch: true,
      ruby: true,
      pageTranslate: true,
      stickyNote: true,
      ink: true,
      anchorFx: true,
      pinCard: true,
      pinHtmlCard: true,
      jumpPage: true,
      navigation: true,
      zoom: true,
      layout: true,
      crop: true,
      fullscreen: true,
      bookSettings: true,
      favorite: true,
      userPage: true,
    },
  });

  // 无扩展时 PWA 也经同一动作名调用真实宿主；扩展接管后同名动作由桥转发回来。
  try {
    if (window.RC && RC.actions && window.PdfAdapter && RC.adapter && RC.adapter() === PdfAdapter) {
      const meta = (storage) => ({ owner: 'pwa', runtime: 'native', storage });
      RC.actions.bind('highlight.save', (payload) => localApi.localAction('highlight', payload), meta('book-sidecar'));
      RC.actions.bind('ink.toggle', () => localApi.localAction('toggle_ink', {}), meta('book-sidecar'));
      RC.actions.bind('note.create', () => localApi.localAction('create_sticky', {}), meta('book-sidecar'));
      RC.actions.bind('reading.ruby.toggle', () => localApi.localAction('toggle_ruby', {}), meta('device-local'));
      RC.actions.bind('translation.page.toggle', () => localApi.localAction('toggle_page_translate', {}), meta('device-local'));
      RC.actions.bind('pin.card', (payload) => localApi.localAction('pin_card', payload), meta('book-sidecar'));
      RC.actions.bind('pin.html', (payload) => localApi.localAction('pin_html', payload), meta('book-sidecar'));
      RC.actions.bind('pin.anchorFx', (payload) => localApi.localAction('anchor_fx', payload), meta('none'));
    }
  } catch (_) {}
})();
