/* web-adapter.js — 实况网页 → 统一控制层的**中间层**(用户拍板 2026-07-19)。
 *
 * 铁律(references/unified-control-layer.md):让中间层去适应旧代码,**不为新形态另造上层建筑**。
 * 所以本文件只做一件事:把「同源代理 iframe 里的真实网页」翻译成 RC.adapter 契约,
 * 顶栏(#ep-top)/侧栏(#ep-side)/全部 rc-* 共享层原样复用 EPUB 阅读器那一套——
 * 网页只是**又一种内容源**,和 PDF 页、EPUB 章节平级。
 *
 * 与其它 adapter 的唯一结构差异:内容不在本文档里,而在 iframe(同源)。因此选区/正文
 * 经 postMessage 桥接(注入脚本在 html_reader.py::_PROXY_INJECT),其余全同。
 */
(function () {
  if (!window.__WEB_CFG) return;
  var CUR = window.__WEB_CFG.url || '';
  var FREL = 'web:' + CUR;          // 统一的"材料标识":高亮/对话/注意力都按它归档
  var _hist = [];
  var _sel = { text: '', ctx: '', rect: null };
  var _pageText = '';
  var _title = '';

  function frame() { return document.getElementById('wl-frame'); }
  function toast(m) { try { window.RC && RC.toast && RC.toast(m); } catch (e) {} }
  function hideSel() { var b = document.getElementById('ep-sel'); if (b) b.style.display = 'none'; }

  // ── 导航(顶栏地址栏 / iframe 内链接点击)──
  function wlGo(u, push) {
    if (!u) return;
    if (!/^https?:\/\//.test(u)) u = 'https://' + u;
    if (push !== false && CUR) _hist.push(CUR);
    CUR = u; FREL = 'web:' + u;
    var inp = document.getElementById('wl-url'); if (inp) inp.value = u;
    var ld = document.getElementById('wl-load'); if (ld) { ld.style.display = 'flex'; ld.textContent = '🌐 加载中…'; }
    frame().src = '/pdf/web/proxy?url=' + encodeURIComponent(u);
    try { history.replaceState(null, '', '/pdf/web/live?url=' + encodeURIComponent(u)); } catch (e) {}
    _pageText = '';
    setTimeout(askText, 1200);
  }
  window.wlGo = wlGo;
  window.wlBack = function () { var p = _hist.pop(); if (p) wlGo(p, false); else location.href = '/pdf/web?home=1'; };

  // ── 与 iframe(同源代理页)的桥接 ──
  function askText() { try { frame().contentWindow.postMessage({ __rcweb: 'getText' }, '*'); } catch (e) {} }
  window.addEventListener('message', function (e) {
    var d = e.data || {};
    if (d.__rcweb === 'nav') { wlGo(d.url); return; }
    if (d.__rcweb === 'ready') {
      var ld = document.getElementById('wl-load'); if (ld) ld.style.display = 'none';
      _title = d.title || ''; askText(); return;
    }
    if (d.__rcweb === 'text') { _pageText = (d.text || '').slice(0, 120000); _title = d.title || _title; return; }
    if (d.__rcweb === 'sel') {
      _sel = { text: d.text || '', ctx: d.ctx || '', rect: d.rect };
      var bar = document.getElementById('ep-sel');
      if (!bar) return;
      if (!_sel.text) { bar.style.display = 'none'; return; }
      var fr = frame().getBoundingClientRect(), r = d.rect || { left: 20, bottom: 80 };
      bar.style.display = 'flex';
      var bw = bar.offsetWidth || 340;
      bar.style.left = Math.max(8, Math.min(window.innerWidth - bw - 8, fr.left + r.left)) + 'px';
      bar.style.top = Math.min(window.innerHeight - 56, fr.top + r.bottom + 8) + 'px';
    }
  });

  // ── WebAdapter:RC.adapter 契约(镜像 epub-html.js 的字段口径)──
  var EP = { dict: '/pdf/api/dict', translate: '/pdf/api/translate', explain: '/pdf/api/explain',
             highlights: '/pdf/api/html-highlights' };
  var WebAdapter = {
    kind: 'web',
    config: { isPDF: false, reflow: true, hasFigures: false, hasFormula: false,
              dictMode: 'sse', popupMode: 'fixed', clickWordDetect: false, anchorKind: 'none' },
    getEndpoints: function () { return EP; },
    fileInfo: function () { return { file: FREL, langs: langs() }; },
    captureSelection: function () {
      return _sel.text ? { text: _sel.text, context: _sel.ctx, ctx: _sel.ctx, rect: _sel.rect } : null;
    },
    clearSelection: function () { _sel = { text: '', ctx: '', rect: null }; hideSel(); },
    jumpToAnchor: function () {},
    currentChapterText: function () { return _pageText.slice(0, 8000); },
    currentLocation: function () { return { unit: 'page', index: 0, total: 1 }; },
    collectFigures: function () { return []; },
    getContext: function (opts) {
      opts = opts || {}; var s = opts.selection || {};
      if (!s.sel && _sel.text) s = { sel: _sel.text, sent: _sel.ctx };
      return {
        file: FREL, book: _title || CUR, url: CUR,
        langs: langs(),
        visible_text: _pageText.slice(0, 4000),   // 网页整页正文(iframe 回传)
        current_section_idx: 0, total_sections: 1,
        selection: s.sel || '', selection_sentence: s.sent || ''
      };
    },
    _host: { asst: hostAsst() }
  };

  var _lang = null;
  function langs() {   // 网页语言按正文检测(没有书级配置)
    if (_lang) return _lang;
    var t = _pageText.slice(0, 4000), out = [];
    if ((t.match(/[ぁ-んァ-ヶ]/g) || []).length > 20) out.push('ja');
    var lat = (t.match(/[A-Za-z]/g) || []).length, han = (t.match(/[一-鿿]/g) || []).length;
    if (lat > Math.max(han, 1) * 2 && lat > 200) out.push('en');
    if (t.length > 200) _lang = out;
    return out;
  }

  function hostAsst() {
    return {
      md: function (t) { return (window.RC && RC.md && RC.md.render) ? RC.md.render(t) : String(t || ''); },
      toast: toast,
      fmtTime: function (ms) { var s = Math.round((Date.now() - (ms || 0)) / 1000);
        return s < 60 ? (s + '秒前') : (s < 3600 ? (Math.round(s / 60) + '分钟前') : (Math.round(s / 3600) + '小时前')); },
      fileRel: function () { return FREL; },
      pdfNumPages: function () { return 1; }, locCount: function () { return 1; },
      dispPage: function (p) { return p; }, pdfFromDisp: function (d) { return d; },
      locNoun: function () { return '页'; }, locLabel: function () { return _title || ''; },
      changePage: function () {}, fitWidth: function () {}, zoomBy: function () {}, toggleTranslate: function () {},
      openDrawer: function () { try { RC.sidedrawer.open('asst'); } catch (e) {} },
      switchTab: function (n) { try { RC.sidedrawer.open(n); } catch (e) {} },
      asstOpen: function () { try { return !!document.querySelector('.ep-side-pane[data-pane="asst"].active,#side-pane-asst.active'); } catch (e) { return false; } },
      voiceContext: function () { return null; },
      setFocusSel: function (t) { window.__focusSel = t ? { text: t } : null; },
      focusSel: function () { return window.__focusSel || null; },
      clearFigFocus: function () {}, figThumb: function () {},
      noteAttached: function () { return []; }, clearNoteAttached: function () {}, renderNoteChips: function () {},
      notesReload: function () {}, noteInject: function () { return false; },
      reloadHighlights: function () {}, loadAllHighlights: function () {}, renderHighlightsOnPage: function () {},
      showHlPicker: function () {}, assistEdit: function () {},
      renderPhraseHl: function () {}, removePhraseHighlight: function () {},
      activePhraseHl: function () { return null; }, setActivePhraseHl: function () {},
      charsRangeToText: function () { return ''; }, charRangeToPtRects: function () { return []; },
      flashSelOnPage: function () {}, noteNearText: function () { return ''; },
      jumpToCtx: function () { try { RC.sidedrawer.close(); } catch (e) {} },
      prewarm: function (off) { try { fetch('/api/assistant/prewarm', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(off ? { off: 1 } : {}), keepalive: true }); } catch (e) {} },
      getPaidNoted: function () { return !!window.__paidNoted; },
      setPaidNoted: function (v) { window.__paidNoted = v; },
      hlUrl: function () { return EP.highlights; },
      showAction: function () { return null; }, queueAction: function () {}, taskAction: function () {},
      voiceLog: function () {}
    };
  }

  // ── 选区工具条动作:全部走共享层(与书里同一份代码路径)──
  function act(a) {
    var t = _sel.text, c = _sel.ctx;
    if (!t) { toast('先选中文字'); return; }
    var opts = { file: FREL, ctx: c, source: CUR };
    hideSel();
    if (a === 'dict') RC.wordpop.show({ word: t, rect: _sel.rect, ctx: c, file: FREL, langs: langs(),
      onFallback: function (w) { RC.result.aiCall(EP.translate, { text: w, target_lang: '中文' }, '🌐 翻译', opts); } });
    else if (a === 'translate') RC.result.aiCall(EP.translate, { text: t, target_lang: '中文' }, '🌐 翻译', opts);
    else if (a === 'explain') RC.result.aiCall(EP.explain, { text: t, context: c }, '💡 AI 解释', opts);
    else if (a === 'chat') RC.result.openChat(t, c, opts);
    else if (a === 'note') RC.snippets.toNote({ text: t, source: CUR, ctx: c });
    else if (a === 'anki') RC.snippets.toAnki({ text: t, source: CUR, ctx: c });
    else if (a === 'grammar') {
      if (window.RC && RC.grammar && RC.grammar.analyze) { RC.grammar.analyze({ sentence: c || t, text: t, file: FREL }); RC.sidedrawer.open('grammar'); }
      else toast('语法层未就绪');
    }
  }

  document.addEventListener('DOMContentLoaded', function () {
    var bar = document.getElementById('ep-sel');
    if (bar) bar.addEventListener('click', function (e) {
      var b = e.target.closest('button[data-act]'); if (b) act(b.getAttribute('data-act'));
    });
    var inp = document.getElementById('wl-url');
    if (inp) inp.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter') return;
      var v = (this.value || '').trim(); if (!v) return;
      var isUrl = v.indexOf('http') === 0 || /^[\w-]+(\.[\w-]+)+([/]|$)/.test(v);
      if (isUrl) wlGo(v); else location.href = '/pdf/web?q=' + encodeURIComponent(v);
    });
    var rd = document.getElementById('wl-reader');
    if (rd) rd.addEventListener('click', function () {   // 📄 阅读模式:抽正文进 HTML 阅读器(可高亮/存 vault)
      var ld = document.getElementById('wl-load');
      if (ld) { ld.style.display = 'flex'; ld.textContent = '📄 抽取正文中…'; }
      fetch('/pdf/api/web-fetch', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url: CUR }) }).then(function (r) { return r.json(); }).then(function (d) {
          if (d.ok && d.file) location.href = '/pdf/html/view?file=' + encodeURIComponent(d.file);
          else if (ld) { ld.textContent = '✗ ' + (d.error || '抽取失败'); setTimeout(function () { ld.style.display = 'none'; }, 1800); }
        }).catch(function () { if (ld) ld.style.display = 'none'; });
    });
    var fr = frame();
    if (fr) fr.addEventListener('load', function () { var ld = document.getElementById('wl-load'); if (ld) ld.style.display = 'none'; askText(); });

    // 接进统一控制层 + 挂共享侧栏(与 EPUB 同一份代码:rc-assistant 建 asst pane、rc-sidedrawer 注入 tab)
    try { if (window.RC && RC.use) RC.use(WebAdapter); window.__webAdapter = WebAdapter; } catch (e) {}
    try { if (window.RC && RC.sidedrawer && RC.sidedrawer.init) RC.sidedrawer.init(); } catch (e) {}
    try { if (window.RC && RC.assistant && RC.assistant.mountPdfSidebar) RC.assistant.mountPdfSidebar(); } catch (e) {}
    try { if (window.RC && RC.settings && RC.settings.init) RC.settings.init({ target: null, file: FREL }); } catch (e) {}
    setTimeout(askText, 1500);
  });
})();
