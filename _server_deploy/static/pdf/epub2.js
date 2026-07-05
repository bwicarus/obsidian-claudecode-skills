/* epub2.js — epub.js 版 EPUB 阅读器:统一控制层(window.RC)驱动(Phase 1+2)
 *
 * 目标:在 epub.js 渲染地基(epub-reader.js,内容跑在每个章节 iframe 里)之上,接 rc-*.js 共享控制层,
 * 实现「选区工具栏 + 查词 + 翻译/解释/对话 + 选段制卡」。**替代 epub-ai.js**(它手搓了一套 AI;这里改用共享层)。
 *
 * 复用两块现成的金子(不重造):
 *   (a) epub.js iframe 选区桥接 —— captureSelection / attachSel / 轮询 / findIframe / showSel / hideSel 定位
 *       **verbatim 搬自 epub-ai.js(第 47-139 行)**,只在 attachSel 里多挂一个「单击词」监听(见下)。
 *       维护 cur = {text, cfi, ctx, rect}(rect 已从 iframe 坐标换算到父视口)。
 *   (b) rc-*.js 控制层 —— 调法照搬手搓底座 epub-html.js 的 selBar handler / 单击直翻,
 *       只把底座从「主文档 char 层」换成「epub.js iframe 选区(cur)」:
 *         dict      → RC.wordpop.show(...)
 *         translate → RC.result.aiCall('/pdf/api/translate', ...)
 *         explain   → RC.result.aiCall('/pdf/api/explain', ...)
 *         chat      → RC.result.openChat(...)
 *         note/anki → RC.snippets.toNote / toAnki
 *
 * 本阶段**不含**:CFI 高亮 / 本章总结 / 全文搜索 / 转 PDF / agentic 助手 —— 那些是 epub-ai.js 的旧功能,留后续 phase。
 */
(function () {
  'use strict';
  function ready(fn) { if (window.__epub && window.__epub.rendition) fn(); else setTimeout(function () { ready(fn); }, 120); }
  ready(init);

  function init() {
    var R = window.__epub.rendition, B = window.__epub.book, CFG = window.__epub.cfg || {};
    var $ = function (id) { return document.getElementById(id); };
    var FREL = CFG.fileRel || '';
    var selBar = $('ep-sel');
    var cur = { text: '', cfi: '', ctx: '', rect: null };   // 当前选中(rect 为父视口坐标)
    // 暴露选区快照给 epub2-highlight.js(高亮 P3):它在工具栏点「🖍 高亮」时要 cur.cfi/text/ctx,
    //   读这个快照比点按钮那刻再从 live selection 取更稳(iOS 上原生选区那刻常已被收起)。
    try { window.__epub.curSel = function () { return cur; }; } catch (e) {}

    // ════════ EpubAdapter:把 EPUB(epub.js iframe)底座收敛成统一 RC.adapter 契约(架构 P1)════════
    //   功能模块以后只通过 RC.adapter() 取 I/O,不再各自摸 iframe/选区/坐标。每种阅读器实现这套契约一次 →
    //   一套功能模块、所有阅读器(EPUB/PDF/未来 HTML)共用。PDF 在 EPUB 验证成熟后也迁来(PdfAdapter),不永久特殊。
    var EpubAdapter = {
      kind: 'epub',
      config: { isPDF: false, reflow: true, hasFigures: true, hasFormula: false, dictMode: 'sse', supportsVoice: true, popupMode: 'fixed', clickWordDetect: true, anchorKind: 'cfi' },
      getEndpoints: function () {
        return { dict: '/pdf/api/dict', dictJp: '/pdf/api/dict-jp', dictJpAi: '/pdf/api/dict-jp-ai', translate: '/pdf/api/translate', explain: '/pdf/api/explain',
          chat: '/pdf/api/epub-chat', assistant: '/pdf/api/epub-assistant', convo: '/pdf/api/epub-convo', highlights: '/pdf/api/epub-highlights',
          vocabMap: '/pdf/api/vocab-mastery-map', vocabAnki: '/pdf/api/vocab-anki', bookLangs: '/pdf/api/book-langs', furigana: '/pdf/api/epub-furigana',
          phrases: '/pdf/api/phrases', phraseMark: '/pdf/api/phrase-mark', translateSentence: '/pdf/api/translate-sentence', search: '/pdf/api/epub-search',
          nodes: '/pdf/api/epub-nodes', imgDescribe: '/pdf/api/epub-img-describe', pageTranslate: '/pdf/api/epub-translate-section' };
      },
      fileInfo: function () { return { file: FREL, langs: (window.__epubDeco ? __epubDeco.bookLangs() : []) }; },
      captureSelection: function () { return { text: cur.text, context: cur.ctx, anchor: { cfi: cur.cfi }, rect: cur.rect }; },
      clearSelection: function () { try { (R.getContents() || []).forEach(function (c) { try { c.window.getSelection().removeAllRanges(); } catch (e) {} }); } catch (e) {} hideSel(); },
      jumpToAnchor: function (anchor) { try { if (anchor && anchor.cfi != null) R.display(anchor.cfi); else if (typeof anchor === 'number') R.display(anchor); } catch (e) {} },
      eachContentDoc: function (cb) { try { (R.getContents() || []).forEach(function (c) { try { cb(c.document, c.window, c); } catch (e) {} }); } catch (e) {} },
      onContentRendered: function (cb) { try { R.on('rendered', function (sec, view) { try { if (view && view.contents) cb(view.contents.document, view.contents.window, view.contents); } catch (e) {} }); R.hooks.content.register(function (c) { try { cb(c.document, c.window, c); } catch (e) {} }); } catch (e) {} },
      currentChapterText: function () { try { var cs = R.getContents() || [], vh = window.innerHeight || 800; for (var i = 0; i < cs.length; i++) { var doc = cs[i].document, ifr = doc && findIframe(doc); if (!ifr) continue; var r = ifr.getBoundingClientRect(); if (r.bottom > 60 && r.top < vh) return (doc.body.innerText || '').slice(0, 8000); } return cs[0] ? (cs[0].document.body.innerText || '').slice(0, 8000) : ''; } catch (e) { return ''; } }
    };
    try { if (window.RC && RC.use) RC.use(EpubAdapter); window.__epubAdapter = EpubAdapter; } catch (e) {}

    var _lastDictTs = 0, _tapMark = null, _markedSpan = null;
    function _clearTapMark() { try { var m = _tapMark; _tapMark = null; if (m && m.parentNode) { var p = m.parentNode; while (m.firstChild) p.insertBefore(m.firstChild, m); p.removeChild(m); p.normalize(); } } catch (e) {} if (_markedSpan) { try { _markedSpan.classList.remove('ep-w-on'); } catch (e) {} _markedSpan = null; } }
    function _ensureMarkCss(d) { try { if (d.__epTapCss) return; d.__epTapCss = 1; var st = d.createElement('style'); st.textContent = '.ep-tap-mark{background:rgba(120,170,255,.45);border-radius:2px}.ep-w{cursor:pointer;-webkit-tap-highlight-color:rgba(120,170,255,.35)}.ep-vocab-und{cursor:pointer}.ep-w-on{background:rgba(120,170,255,.5);border-radius:2px}'; (d.head || d.documentElement).appendChild(st); } catch (e) {} }
    // ── 目标语言分词浮层:英文词包成可点 .ep-w span(iOS 对可点元素 cursor:pointer 会派发 click → 单击精确查词,绕开 iframe 点文字不发事件 + 光标误选的坑;原生拖选多词照常)。日语分词后续。──
    function wrapWords(doc) {
      try {
        if (doc.__epWordsWrapped) return;
        var langs = (window.__epubDeco && __epubDeco.bookLangs) ? __epubDeco.bookLangs() : [];
        if (langs.indexOf('en') < 0) return;
        doc.__epWordsWrapped = 1; _ensureMarkCss(doc);
        var body = doc.body; if (!body) return;
        var w = doc.createTreeWalker(body, NodeFilter.SHOW_TEXT, null), tn, nodes = [];
        while ((tn = w.nextNode())) { var p = tn.parentElement; if (p && p.closest && !p.closest('.ep-w,.ep-vocab-und,.ep-tr-rt,rt,a,script,style,code,pre') && /[A-Za-z]/.test(tn.nodeValue)) nodes.push(tn); }
        nodes.forEach(function (node) {
          var txt = node.nodeValue, re = /[A-Za-z][A-Za-z'’\-]*/g, m, last = 0, frag = doc.createDocumentFragment(), any = false;
          while ((m = re.exec(txt))) {
            if (m.index > last) frag.appendChild(doc.createTextNode(txt.slice(last, m.index)));
            var sp = doc.createElement('span'); sp.className = 'ep-w'; sp.textContent = m[0]; frag.appendChild(sp); any = true; last = m.index + m[0].length;
          }
          if (any) { if (last < txt.length) frag.appendChild(doc.createTextNode(txt.slice(last))); try { node.parentNode.replaceChild(frag, node); } catch (e) {} }
        });
      } catch (e) {}
    }
    window.__epubRewrapWords = function () { try { (R.getContents() || []).forEach(function (c) { try { c.document.__epWordsWrapped = 0; wrapWords(c.document); } catch (e) {} }); } catch (e) {} if (window.__epubRebuildWordOv) window.__epubRebuildWordOv(); };
    function _dictForWordSpan(sp, win, doc) {
      try {
        var t; if (sp.tagName === 'RUBY') { var _cl = sp.cloneNode(true); _cl.querySelectorAll('rt').forEach(function (e) { e.remove(); }); t = (_cl.textContent || '').trim(); } else { t = (sp.textContent || '').trim(); }
        if (!t) return;
        var r = sp.getBoundingClientRect(), ifr = findIframe(doc), ib = ifr ? ifr.getBoundingClientRect() : { left: 0, top: 0 };
        var rect = { left: ib.left + r.left, top: ib.top + r.top, right: ib.left + r.right, bottom: ib.top + r.bottom };
        var pblk = sp.closest ? sp.closest('p,li,td,blockquote,h1,h2,h3,h4,div') : null;
        var pctx = (pblk ? (pblk.textContent || '') : '').trim().slice(0, 1200);
        _clearTapMark(); try { win.getSelection().removeAllRanges(); } catch (_) {}
        try { sp.classList.add('ep-w-on'); _markedSpan = sp; } catch (e) {}
        hideSel(); if (!_dictGate()) return;
        _tlog('wordSpan dict ' + t);
        var _wcfi = ''; try { var _wc = (R.getContents() || []).filter(function (c) { return c.document === doc; })[0]; if (_wc && _wc.cfiFromRange) { var _wr = doc.createRange(); _wr.selectNode(sp); _wcfi = _wc.cfiFromRange(_wr); } } catch (e) {}
        RC.wordpop.show({ word: t, rect: rect, ctx: pctx, file: FREL, langs: (window.__epubDeco ? __epubDeco.bookLangs() : []),
          markHighlight: (_wcfi ? function () { if (window.epubHl && window.epubHl.markFromSel) window.epubHl.markFromSel({ cfi: _wcfi, text: t, ctx: pctx }, ''); } : null), onGrammar: function (w) { if (window.onGrammarAnalyze) window.onGrammarAnalyze({ word: w, ctx: pctx }); }, onMastered: function () { if (window.refreshVocabUnderlinesForAllPages) window.refreshVocabUnderlinesForAllPages(); },
          onFallback: function (word) { RC.result.aiCall('/pdf/api/translate', { text: word, target_lang: '中文' }, '🌐 翻译', { aiParams: function () { return (window.RC && RC.settings) ? RC.settings.aiParams() : {}; } }); } });
      } catch (e) { _tlog('wordSpan ERR ' + (e && e.message)); }
    }
    function _closeWordPop() { try { var wp = document.getElementById('word-pop'); if (wp && wp.style.display !== 'none') wp.style.display = 'none'; } catch (e) {} }
    function _dictGate() { var now = Date.now(); if (now - _lastDictTs < 500) return false; _lastDictTs = now; return true; }
    function _tlog(m) { if (!window.__epDbg) return; try { fetch('/pdf/api/epub-dbg', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ msg: '[tap] ' + m }), keepalive: true }).catch(function () {}); } catch (e) {} }

    function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
    function toast(m) { if (window.RC && RC.toast) RC.toast(m); }
    function aiParams() { return (window.RC && RC.settings && RC.settings.aiParams) ? RC.settings.aiParams() : {}; }

    // ── 调试条(默认关;url 加 ?dbg=1 开)──诊断"选中不弹"用 ──
    var DBG = location.search.indexOf('dbg=1') >= 0, dbgEl = null, dbgN = 0;
    function dbg(msg) {
      if (!DBG) return;
      if (!dbgEl) {
        dbgEl = document.createElement('div');
        dbgEl.style.cssText = 'position:fixed;left:6px;bottom:6px;z-index:9999;max-width:60vw;max-height:30vh;overflow:auto;' +
          'background:rgba(8,12,24,.92);color:#7dffa0;font:11px/1.4 monospace;padding:6px 8px;border:1px solid #2a3a63;border-radius:7px;white-space:pre-wrap;pointer-events:none';
        document.body.appendChild(dbgEl);
      }
      dbgN++;
      dbgEl.textContent = ('[' + dbgN + '] ' + msg + '\n' + dbgEl.textContent).slice(0, 1200);
    }
    dbg('epub2 init; hooks=' + (R && R.hooks ? 'y' : 'n') + ' getContents=' + (R && R.getContents ? 'y' : 'n'));

    // ════════════════════════════════════════════════════════════════════════
    // (a) 选区桥接 —— verbatim 搬自 epub-ai.js(findIframe / captureSelection / attachSel /
    //     R.hooks.content.register / 已渲染章节立刻挂 / R.on('selected') / 350ms 轮询 / showSel / hideSel)。
    //     新增:幂等去重(__epSelAttached)+ rendered/轮询补挂 + attachSel 里挂 pointerdown/up「无位移 tap」→ onIframeTap(单击词直弹字典,见 (c))。
    // ════════════════════════════════════════════════════════════════════════
    function findIframe(doc) {
      try { if (doc.defaultView && doc.defaultView.frameElement) return doc.defaultView.frameElement; } catch (e) {}
      var ifrs = document.querySelectorAll('#ep-viewer iframe');
      for (var i = 0; i < ifrs.length; i++) { try { if (ifrs[i].contentDocument === doc) return ifrs[i]; } catch (e) {} }
      return null;
    }
    function captureSelection(win, doc) {
      try {
        var sel = win.getSelection();
        if (!sel || !sel.rangeCount) { hideSel(); return; }
        var txt = (sel.toString() || '').trim();
        dbg('capture: sel="' + txt.slice(0, 20) + '" len=' + txt.length);
        if (!txt) { hideSel(); return; }
        var rng = sel.getRangeAt(0);
        var r = rng.getBoundingClientRect();
        if (!r || (!r.width && !r.height)) { hideSel(); return; }
        var ifr = findIframe(doc);
        var ib = ifr ? ifr.getBoundingClientRect() : { left: 0, top: 0 };
        var node = sel.anchorNode;
        var blk = node ? (node.nodeType === 3 ? node.parentElement : node) : null;
        blk = blk && blk.closest ? blk.closest('p,li,td,blockquote,div,section,h1,h2,h3,h4') : null;
        var cfi = '';
        try {
          var cnts = (R.getContents() || []).filter(function (c) { return c.document === doc; })[0];
          if (cnts && cnts.cfiFromRange) cfi = cnts.cfiFromRange(rng);
        } catch (e) {}
        cur = {
          text: txt, cfi: cfi,
          ctx: (blk ? (blk.textContent || '') : '').trim().slice(0, 1200),
          rect: { left: ib.left + r.left, top: ib.top + r.top, right: ib.left + r.right, bottom: ib.top + r.bottom }
        };
        showSel(cur.rect);
      } catch (e) {}
    }
    function attachSel(contents) {
      try {
        var doc = contents && contents.document, win = contents && contents.window;
        if (!doc || !win) { dbg('attachSel: no doc/win'); return; }
        if (doc.__epSelAttached) return;        // 幂等:hook + getContents + rendered + 轮询 可能对同一 doc 多次调用
        doc.__epSelAttached = true;
        var deb;
        var fire = function () { setTimeout(function () { captureSelection(win, doc); }, 10); };
        doc.addEventListener('mouseup', fire, true);
        doc.addEventListener('touchend', fire, true);
        doc.addEventListener('selectionchange', function () {
          clearTimeout(deb);
          deb = setTimeout(function () {
            var s = win.getSelection();
            if (s && !s.isCollapsed && (s.toString() || '').trim()) { captureSelection(win, doc); return; }   // 真选区 → 工具栏
            // iOS 在 iframe 里单击文字「只触发 selectionchange + 放折叠光标」(不触发 click/touch)→ 用光标位置当单击词弹字典
            else if (s && s.isCollapsed && s.anchorNode) { if (Date.now() - _lastDictTs > 600 && Date.now() - _justDragged > 600) { _closeWordPop(); _clearTapMark(); } }   // 抛弃轮询查词:折叠光标只用来「点别处关框」(英日都走父文档浮层按钮)
            else hideSel();
          }, 250);
        });
        // ── 单击词直弹字典 ──
        // 根因(旧实现只挂 'click'):章节内容在 epub.js 的 iframe 里,iOS Safari 下 iframe 的 `click` 经常不触发
        // (被原生选区/手势吞掉)→「单击无效」。改用 pointerdown→pointerup 的「无位移 tap」检测(覆盖鼠标 + 触摸,
        // 比 click 可靠;同 epub-html.js 给 mark 用的那套),命中英/日词 → RC.wordpop.show。
        // tap 检测:pointer + touch 双管齐下(iOS Safari 在 iframe 里 pointerup 常不触发,touchend 才可靠)+ 去重
        var _tapDone = false;
        function fireTap(cx, cy) {
          if (_tapDone) { _tlog('fireTap: deduped'); return; } _tapDone = true; setTimeout(function () { _tapDone = false; }, 700);   // 700ms 去重窗口对齐 PDF(_clLastTouchAt):一次物理 tap 的 pointer/touch/click 只查一次词;双/三击的后续 tap 不再重查单词,交给 native click 多击选行/段
          _tlog('fireTap ' + Math.round(cx) + ',' + Math.round(cy));
          setTimeout(function () { onIframeTap(cx, cy, win, doc); }, 0);
        }
        var _tap = null;
        doc.addEventListener('pointerdown', function (e) { _tap = { x: e.clientX, y: e.clientY, t: Date.now() }; }, true);
        doc.addEventListener('pointerup', function (e) {
          var d = _tap; _tap = null; if (!d) return;
          if (Math.abs(e.clientX - d.x) > 8 || Math.abs(e.clientY - d.y) > 8) return;   // 拖动 → 不是 tap
          if (Date.now() - d.t > 600) return;                                            // 长按 → 不是 tap
          fireTap(e.clientX, e.clientY);
        }, true);
        var _tt = null;   // touch 路径(iOS 主力):touchstart 记位 + touchend 无位移/非长按 → tap
        doc.addEventListener('touchstart', function (e) { if (e.touches && e.touches.length === 1) { var t = e.touches[0]; _tt = { x: t.clientX, y: t.clientY, ts: Date.now() }; } else _tt = null; }, true);
        doc.addEventListener('touchend', function (e) {
          var d = _tt; _tt = null; if (!d) { _tlog('touchend: no _tt'); return; }
          var t = e.changedTouches && e.changedTouches[0]; if (!t) { _tlog('touchend: no changedTouches'); return; }
          var mv = Math.max(Math.abs(t.clientX - d.x), Math.abs(t.clientY - d.y)), dt = Date.now() - d.ts;
          _tlog('touchend move=' + Math.round(mv) + ' dt=' + dt);
          if (mv > 10) return;
          if (dt > 500) return;
          fireTap(t.clientX, t.clientY);
        }, true);
        // click 通用兜底(部分环境 pointer/touch 都没给到 tap)
        doc.addEventListener('click', function (e) { fireTap(e.clientX, e.clientY); }, true);
        dbg('attachSel ok (listeners on iframe doc)'); _tlog('attachSel: listeners on iframe doc');
        wrapWords(doc);
        doc.addEventListener('click', function (e) { var sp = e.target && e.target.closest && e.target.closest('.ep-w, .ep-vocab-und, ruby[data-eph]'); if (sp) { e.preventDefault(); _tlog('iframe CLICK word-span "' + (sp.textContent||'').slice(0,12) + '"'); _dictForWordSpan(sp, win, doc); } }, true);
        doc.addEventListener('touchend', function (e) { var t = (e.changedTouches && e.changedTouches[0]); var el = t ? doc.elementFromPoint(t.clientX, t.clientY) : e.target; var sp = el && el.closest && el.closest('.ep-w, .ep-vocab-und, ruby[data-eph]'); if (sp) { _tlog('iframe TOUCHEND word-span "' + (sp.textContent||'').slice(0,12) + '"'); _dictForWordSpan(sp, win, doc); } }, true);
        // ── 双击选行 / 三击选段(对照 PDF 13-selection 多击)──
        // native click 每个独立物理 tap 只发一次(浏览器已按同点合成 dblclick),用 380ms+24px 窗口数连击 →
        // 2=选当前视觉行、3=选所在段落块,设原生选区后走 captureSelection 出多词工具栏。第 1 击查词已由
        // tap 路径触发,_tapDone(700ms) 保证后续连击不重查单词。桌面可靠;iOS iframe click 不稳 → 触摸多选靠原生长按选区兜底。
        var _clkN = 0, _clkT = 0, _clkX = 0, _clkY = 0;
        doc.addEventListener('click', function (e) {
          var now = Date.now();
          if (now - _clkT < 380 && Math.abs(e.clientX - _clkX) < 24 && Math.abs(e.clientY - _clkY) < 24) _clkN = (_clkN % 3) + 1;
          else _clkN = 1;
          _clkT = now; _clkX = e.clientX; _clkY = e.clientY;
          if (_clkN >= 2) _epMultiClick(_clkN, e.clientX, e.clientY, win, doc);
        }, true);
      } catch (e) { dbg('attachSel ERR: ' + (e && e.message)); }
    }
    // 已渲染的章节立刻挂,后续加载的章节经 hook / rendered 挂(幂等,__epSelAttached 去重)
    try { R.hooks.content.register(attachSel); dbg('hook registered'); } catch (e) { dbg('hook reg ERR: ' + (e && e.message)); }
    try { var gc = R.getContents() || []; dbg('getContents n=' + gc.length); gc.forEach(attachSel); } catch (e) { dbg('getContents ERR: ' + (e && e.message)); }
    try { R.on('rendered', function (section, view) { try { if (view && view.contents) attachSel(view.contents); } catch (e) {} }); } catch (e) {}
    try { R.on('selected', function (cfiRange, contents) { dbg('epubjs selected fired'); captureSelection(contents.window, contents.document); }); } catch (e) {}
    // 兜底:开书初期反复扫描已渲染章节挂监听(幂等),修「首屏 view 在 hook 注册/快照之间渲染 → 漏挂 → 单击无效」的竞态
    (function attachScan(n) { try { (R.getContents() || []).forEach(attachSel); } catch (e) {} if (n < 20) setTimeout(function () { attachScan(n + 1); }, 300); })(0);

    // ── 兜底:轮询选区(iOS Safari + epub.js iframe 里 selectionchange/touchend 常不触发,事件靠不住)──
    var _pollLast = '', _emptyCnt = 0, _lastCaretNode = null, _lastCaretOff = -1;
    setInterval(function () {
      try {
        var cs = R.getContents() || [];
        var found = null, fwin = null, fdoc = null, caretSel = null, cwin = null, cdoc = null;
        for (var i = 0; i < cs.length; i++) {
          try {
            var s = cs[i].window.getSelection();
            var t = s && s.toString().trim();
            if (t) { found = t; fwin = cs[i].window; fdoc = cs[i].document; break; }
            // 折叠光标(单击放的):iOS 在 iframe 里单击不派发任何事件,只能靠轮询抓 getSelection 的光标
            if (!caretSel && s && s.isCollapsed && s.anchorNode && s.anchorNode.nodeType === 3) { caretSel = s; cwin = cs[i].window; cdoc = cs[i].document; }
          } catch (e) {}
        }
        if (found) {
          _emptyCnt = 0; _lastCaretNode = null; _lastCaretOff = -1;
          if (found !== _pollLast) { _pollLast = found; captureSelection(fwin, fdoc); }
        } else if (caretSel && (caretSel.anchorNode !== _lastCaretNode || caretSel.anchorOffset !== _lastCaretOff)) {
          _lastCaretNode = caretSel.anchorNode; _lastCaretOff = caretSel.anchorOffset;
          // 抛弃轮询查词:折叠光标只用来「点别处关字典框」,不再取词(英日单击都走父文档浮层按钮,精确无误选)
          if (Date.now() - _lastDictTs > 600 && Date.now() - _justDragged > 600) { _closeWordPop(); _clearTapMark(); }
        } else {
          _pollLast = '';
          if (!caretSel) { _lastCaretNode = null; _lastCaretOff = -1; }
          if (selBar.classList.contains('open') && !_customSel) { _emptyCnt++; if (_emptyCnt >= 2) hideSel(); }
        }
      } catch (e) {}
    }, 350);
    dbg('poll started');

    // ════ 父文档分词浮层按钮(iOS 在 iframe 里对行内文字/span 连 touchend 都不发 → 唯一可行:可点元素放父文档盖在词上)════
    var _wordOv = null, _wordOvT = null, _ovDrag = null, _justDragged = 0, _customSel = false;
    function _ensureWordOv() {
      if (_wordOv) return _wordOv;
      _wordOv = document.createElement('div'); _wordOv.id = 'ep-word-ov';
      _wordOv.style.cssText = 'position:fixed;left:0;top:0;right:0;bottom:0;pointer-events:none;z-index:45';
      document.body.appendChild(_wordOv);
      // 单击词 → 查字典(拖选刚结束 400ms 内不触发,防拖完误查)
      var hit = function (e) { if (Date.now() - _justDragged < 400) return; var b = e.target && e.target.closest && e.target.closest('.ep-wbtn'); if (b && b.__sp) { e.preventDefault(); _tlog('wordOv tap "' + (b.__word || '').slice(0, 12) + '"'); _dictForWordSpan(b.__sp, b.__win, b.__doc); } };
      _wordOv.addEventListener('click', hit, false);
      _wordOv.addEventListener('touchend', hit, false);
      // 拖选多词起手(照搬 PDF 自定义拖选:按下记起点 → 文档级 move 跟踪划过的词 → 松手设原生选区 → 轮询出多词工具栏)
      _wordOv.addEventListener('touchstart', _ovDown, { passive: true });
      _wordOv.addEventListener('mousedown', _ovDown);
      return _wordOv;
    }
    function _ovDown(e) {
      var b = e.target && e.target.closest && e.target.closest('.ep-wbtn'); if (!b || !b.__sp) { _ovDrag = null; return; }
      _clearCustomSel();
      var t = (e.touches && e.touches[0]) || e;
      _ovDrag = { startSp: b.__sp, win: b.__win, doc: b.__doc, endSp: b.__sp, moved: false, sx: t.clientX, sy: t.clientY };
      document.addEventListener('touchmove', _ovMove, { passive: false });
      document.addEventListener('mousemove', _ovMove);
      document.addEventListener('touchend', _ovUp);
      document.addEventListener('mouseup', _ovUp);
    }
    function _ovMove(e) {
      if (!_ovDrag) return;
      var t = (e.touches && e.touches[0]) || e;
      if (!_ovDrag.moved && Math.abs(t.clientX - _ovDrag.sx) < 8 && Math.abs(t.clientY - _ovDrag.sy) < 8) return;
      _ovDrag.moved = true; _justDragged = Date.now();   // 一开始拖就标记 → 松手时浮层 touchend 的单击查词被拦(防多词选区还弹首词字典)
      if (e.cancelable) { try { e.preventDefault(); } catch (_) {} }   // 拖选时别滚动
      var el = document.elementFromPoint(t.clientX, t.clientY);
      var b = el && el.closest && el.closest('.ep-wbtn');
      if (b && b.__sp && b.__doc === _ovDrag.doc) { _ovDrag.endSp = b.__sp; _ovHighlight(); }
    }
    function _ovUp() {
      document.removeEventListener('touchmove', _ovMove); document.removeEventListener('mousemove', _ovMove);
      document.removeEventListener('touchend', _ovUp); document.removeEventListener('mouseup', _ovUp);
      _setIframeSelectable(true);   // 恢复原生选区(中文长按选区照常)
      var d = _ovDrag; _ovDrag = null;
      if (!d || !d.moved || d.startSp === d.endSp) { _ovClearHl(); return; }
      _justDragged = Date.now();
      try {
        var a = d.startSp, b2 = d.endSp;
        if (a.compareDocumentPosition(b2) & Node.DOCUMENT_POSITION_PRECEDING) { var tmp = a; a = b2; b2 = tmp; }   // 保证 a 在前
        var rng = d.doc.createRange(); rng.setStartBefore(a); rng.setEndAfter(b2);
        var txt = (rng.toString() || '').trim(); if (!txt) { _ovClearHl(); return; }
        var ifr = findIframe(d.doc), ib = ifr ? ifr.getBoundingClientRect() : { left: 0, top: 0 }, r = rng.getBoundingClientRect();
        var cfi = ''; try { var cnts = (R.getContents() || []).filter(function (c) { return c.document === d.doc; })[0]; if (cnts && cnts.cfiFromRange) cfi = cnts.cfiFromRange(rng); } catch (e) {}
        var blk = a.closest ? a.closest('p,li,td,blockquote,div,section,h1,h2,h3,h4') : null;
        cur = { text: txt, cfi: cfi, ctx: (blk ? (blk.textContent || '') : '').trim().slice(0, 1200),
          rect: { left: ib.left + r.left, top: ib.top + r.top, right: ib.left + r.right, bottom: ib.top + r.bottom } };
        _customSel = true;   // 自定义多词选区(不设原生选区→无 iOS 系统菜单,跟 PDF 版一致);蓝色高亮=选区视觉,轮询不许清
        showSel(cur.rect);
        _tlog('wordOv multi "' + txt.slice(0, 24) + '"');
      } catch (er) { _tlog('wordOv multi ERR ' + (er && er.message)); _ovClearHl(); }
    }
    function _clearCustomSel() { if (_customSel) { _customSel = false; _ovClearHl(); hideSel(); } }
    function _ovHighlight() {
      try {
        var d = _ovDrag; if (!d || !_wordOv) return;
        var a = d.startSp, b = d.endSp, fwd = !(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_PRECEDING);
        var lo = fwd ? a : b, hi = fwd ? b : a, kids = _wordOv.children;
        for (var i = 0; i < kids.length; i++) {
          var bt = kids[i]; if (!bt.__sp || bt.style.display === 'none') continue;
          var sp = bt.__sp;
          var inR = (sp === lo || sp === hi) || ((lo.compareDocumentPosition(sp) & Node.DOCUMENT_POSITION_FOLLOWING) && (hi.compareDocumentPosition(sp) & Node.DOCUMENT_POSITION_PRECEDING));
          bt.style.background = inR ? 'rgba(120,170,255,.35)' : 'transparent';
        }
      } catch (e) {}
    }
    function _ovClearHl() { try { if (!_wordOv) return; var kids = _wordOv.children; for (var i = 0; i < kids.length; i++) kids[i].style.background = 'transparent'; } catch (e) {} }
    function _setIframeSelectable(on) { try { (R.getContents() || []).forEach(function (c) { try { var st = c.document.body.style; st.webkitUserSelect = on ? '' : 'none'; st.userSelect = on ? '' : 'none'; } catch (e) {} }); } catch (e) {} }
    function _rebuildWordOv() {
      try {
        var ov = _ensureWordOv();
        if (!_clickTranslate()) { while (ov.firstChild) ov.removeChild(ov.firstChild); return; }
        var vh = window.innerHeight, kids = ov.children, ei = 0;
        (R.getContents() || []).forEach(function (c) {
          var ifr = findIframe(c.document); if (!ifr) return;
          var ib = ifr.getBoundingClientRect(), sps = c.document.querySelectorAll('.ep-w, .ep-vocab-und, ruby[data-eph]');
          for (var i = 0; i < sps.length; i++) {
            var sp = sps[i], r = sp.getBoundingClientRect();
            if (r.width <= 0) continue;
            var top = ib.top + r.top, left = ib.left + r.left;
            if (top > vh + 80 || top + r.height < -80) continue;   // 只建视口附近
            var b = kids[ei];
            if (!b) { b = document.createElement('div'); b.className = 'ep-wbtn'; b.style.cssText = 'position:absolute;pointer-events:auto;cursor:pointer;background:transparent;touch-action:manipulation;-webkit-user-select:none;user-select:none;-webkit-touch-callout:none'; ov.appendChild(b); }
            b.style.left = left + 'px'; b.style.top = top + 'px'; b.style.width = r.width + 'px'; b.style.height = r.height + 'px'; b.style.display = '';
            b.__word = sp.textContent || ''; b.__sp = sp; b.__win = c.window; b.__doc = c.document;
            ei++;
          }
        });
        for (var k = kids.length - 1; k >= ei; k--) kids[k].style.display = 'none';
      } catch (e) {}
    }
    function _scheduleWordOv() { clearTimeout(_wordOvT); _wordOvT = setTimeout(_rebuildWordOv, 90); }
    window.__epubRebuildWordOv = _scheduleWordOv;
    try { R.on('rendered', _scheduleWordOv); } catch (e) {}
    try { R.on('relocate', _scheduleWordOv); } catch (e) {}
    try { window.addEventListener('resize', _scheduleWordOv); } catch (e) {}
    try { var _vp = document.getElementById('ep-viewer'); if (_vp) _vp.addEventListener('scroll', _scheduleWordOv, true); } catch (e) {}
    _scheduleWordOv();

    // showSel:定位 verbatim 搬自 epub-ai.js(固定屏幕底部居中,错开 iOS 原生选择菜单);
    //         **新增**单词↔多词分流 + preview(调法照搬 epub-html.js showSel)。
    // 点工具栏外即关闭选中工具栏(健壮版:父文档 + 各 iframe 捕获阶段;排除工具栏/词按钮/抽屉;同时清原生选区——custom 与原生选区都覆盖)
    var _outsideTapH = null;
    function _bindOutsideDismiss() {
      if (_outsideTapH) return;
      _outsideTapH = function (e) {
        try {
          var t = e.target;
          if (t && t.closest && (t.closest('#ep-sel') || t.closest('.ep-wbtn') || t.closest('#ep-side') || t.closest('#word-pop'))) return;
          _unbindOutsideDismiss();
          _clearCustomSel();
          try { (R.getContents() || []).forEach(function (c) { try { c.window.getSelection().removeAllRanges(); } catch (_) {} }); } catch (_) {}
          hideSel(); _closeWordPop(); _clearTapMark();
        } catch (_) {}
      };
      document.addEventListener('pointerdown', _outsideTapH, true);
      document.addEventListener('touchstart', _outsideTapH, true);
      try { (R.getContents() || []).forEach(function (c) { try { c.document.addEventListener('pointerdown', _outsideTapH, true); c.document.addEventListener('touchstart', _outsideTapH, true); } catch (_) {} }); } catch (_) {}
    }
    function _unbindOutsideDismiss() {
      var h = _outsideTapH; if (!h) return; _outsideTapH = null;
      try { document.removeEventListener('pointerdown', h, true); document.removeEventListener('touchstart', h, true); } catch (_) {}
      try { (R.getContents() || []).forEach(function (c) { try { c.document.removeEventListener('pointerdown', h, true); c.document.removeEventListener('touchstart', h, true); } catch (_) {} }); } catch (_) {}
    }
    function showSel() {
      // 按词/多词分流(照搬 epub-html):单个英/日词 → word 组(查词);多词/中文 → multi 组(翻译/解释/对话);
      // 复制/笔记/制卡 两者都有(both)。
      var word = isWordSel(cur.text);
      selBar.querySelectorAll('[data-grp]').forEach(function (b) {
        var g = b.dataset.grp;
        var show = (g === 'both') || (word ? g === 'word' : g === 'multi');
        b.style.display = show ? '' : 'none';
      });
      var pv = $('ep-preview');
      if (pv) {
        var t = cur.text || '', tr = t.trim();
        // 文案/截断对齐 PDF 13-selection 的 _updateSelPreview:>120 字 → 全角「slice(0,60) … slice(-40)」;「已选：」前缀 +「（N 字）」
        var disp = t.length > 120 ? (t.slice(0, 60) + ' … ' + t.slice(-40)) : t;
        var cnt = (/[A-Za-z]/.test(tr) && /\s/.test(tr)) ? (tr.split(/\s+/).filter(Boolean).length + ' 词') : (t.length + ' 字');   // 英文多词→词数;中日→字数
        pv.innerHTML = '<b>已选：</b>' + esc(disp) + '<span class="len">（' + cnt + '）</span>';
      }
      selBar.classList.add('open');
      // 固定在屏幕底部居中:iOS 原生选择菜单浮在选区附近(屏幕中部),我们错到底部 → 永不重叠(verbatim)
      selBar.style.left = '50%';
      selBar.style.right = 'auto';
      selBar.style.top = 'auto';
      selBar.style.bottom = 'calc(env(safe-area-inset-bottom, 0px) + 20px)';
      selBar.style.transform = 'translateX(-50%)';
      _bindOutsideDismiss();
    }
    function hideSel() { _unbindOutsideDismiss(); selBar.classList.remove('open'); }
    function isWordSel(t) { t = t || ''; return t.length <= 30 && !/\s/.test(t) && (/^[A-Za-z][A-Za-z'’\-]*$/.test(t) || /[぀-ヿ]/.test(t)); }

    // ════════════════════════════════════════════════════════════════════════
    // (b) 工具栏分流 —— 调法照搬 epub-html.js 的 selBar handler(resultOpts/snipOpts 同构),
    //     底座从 char-layer anchor 换成 epub.js 选区 cur。本阶段无 CFI 高亮 → 不传 markHighlight。
    // ════════════════════════════════════════════════════════════════════════
    // 结果模态适配器:aiParams 透传 model/effort;ankiSource 给「🎴 制 Anki」出处链接(epub view url)。
    // 无 markHighlight(高亮留后续 phase)→ 结果模态「🖌 标记」会提示缺适配器,不建高亮。
    function mkResultOpts(kind, sentence) {
      var capSel = { cfi: (cur && cur.cfi) || '', text: (cur && cur.text) || '', ctx: (cur && cur.ctx) || sentence || '' };   // 捕获触发时的选区(结果框稍后才显示,选区可能已清)
      return {
        kind: kind,
        aiParams: aiParams,
        markHighlight: function (text, body, sent, k) {   // 结果框「🖌 标记」→ 选区 + AI 正文存成高亮(接 epub2-highlight)
          if (window.epubHl && window.epubHl.markFromSel) window.epubHl.markFromSel(capSel, body || '');
          else if (window.RC && RC.toast) RC.toast('高亮模块未就绪');
        },
        ankiSource: function () {
          return { file: FREL, sentence: sentence || '', sourceUrl: location.origin + '/pdf/epub/view?file=' + encodeURIComponent(FREL) };
        }
      };
    }
    // 选段→笔记/Anki:用共享层 RC.snippets;showCard 复用本页 #ep-ai 侧栏(openAi + addCard,同 epub-html)。
    function snipOpts(txt) {
      return {
        text: txt, file: FREL,
        getNoteName: function () { return prompt('新笔记名(可不带 .md):', (txt || '').slice(0, 18).replace(/\s+/g, ' ')); },
        showCard: function (head, sub) { openAi(head.replace(/^[^\s]+\s+/, '')); return addCard(head, sub); }
      };
    }

    selBar.addEventListener('click', function (e) {
      var b = e.target.closest('button'); if (!b) return;
      var act = b.dataset.act;
      var txt = cur.text; if (!txt) return;
      // 选区快照:结果模态的闭包稍后才执行,cur 会被下次选中覆盖(照搬 epub-html 存上下文)
      var selTxt = cur.text, selCtx = cur.ctx, selRect = cur.rect;
      hideSel();
      if (act === 'copy') {
        doCopy(selTxt);
      } else if (act === 'dict') {
        RC.wordpop.show({
          word: selTxt, rect: selRect, ctx: selCtx, file: FREL, langs: [],
          onGrammar: function (w) { if (window.onGrammarAnalyze) window.onGrammarAnalyze({ word: w, ctx: selCtx }); },
          onMastered: function () { if (window.refreshVocabUnderlinesForAllPages) window.refreshVocabUnderlinesForAllPages(); },
          // 非英日(纯中文等)→ 保留转译走结果模态(同 epub-html)
          onFallback: function (word) { RC.result.aiCall('/pdf/api/translate', { text: word, target_lang: '中文' }, '🌐 翻译', mkResultOpts('note', selCtx)); }
        });
      } else if (act === 'translate') {
        RC.result.aiCall('/pdf/api/translate', { text: selTxt, target_lang: '中文' }, '🌐 翻译', mkResultOpts('note', selCtx));
      } else if (act === 'explain') {
        RC.result.aiCall('/pdf/api/explain', { text: selTxt, context: selCtx }, '💡 AI 解释', mkResultOpts('explain', selCtx));
      } else if (act === 'chat') {
        RC.result.openChat(selTxt, selCtx, mkResultOpts('note', selCtx));
      } else if (act === 'note') {
        RC.snippets.toNote(snipOpts(selTxt));
      } else if (act === 'anki') {
        RC.snippets.toAnki(snipOpts(selTxt));
      } else if (act === 'grammar') {
        // 多词选区 → 语法分析(读 window.__epub.curSel() 拿 text+ctx,见 epub2-grammar.js)
        if (window.onGrammarAnalyze) window.onGrammarAnalyze({ fromSelection: true });
      }
    });

    // ── 助手侧栏 = 统一抽屉(RC.sidedrawer)的「助手」pane:snippets 结果/进度卡渲进 #ep-ai-body ──
    function openAi(title) { if (window.RC && RC.sidedrawer) RC.sidedrawer.open('asst'); }
    function closeAi() { if (window.RC && RC.sidedrawer) RC.sidedrawer.close(); }
    function addCard(head, sub) {
      var aiBody = $('ep-ai-body'); if (!aiBody) return null;
      var card = document.createElement('div'); card.className = 'ep-card';
      card.innerHTML = '<div class="h">' + head + (sub ? '<span class="ep-sel-chip">' + esc(sub.slice(0, 40)) + (sub.length > 40 ? '…' : '') + '</span>' : '') + '</div><div class="c"><span class="ep-spin"></span></div>';
      aiBody.appendChild(card); aiBody.scrollTop = aiBody.scrollHeight;
      return card.querySelector('.c');
    }

    // 复制:Clipboard API + execCommand 兜底(照搬 epub-html _execCopy)
    function _execCopy(s) {
      try {
        var ta = document.createElement('textarea'); ta.value = s; ta.style.cssText = 'position:fixed;left:-9999px;top:0';
        document.body.appendChild(ta); ta.select(); var ok = document.execCommand('copy'); document.body.removeChild(ta); return ok;
      } catch (e) { return false; }
    }
    function doCopy(txt) {
      var done = function (ok) { toast(ok ? '已复制' : '复制失败'); };
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(txt).then(function () { done(true); }, function () { done(_execCopy(txt)); });
      else done(_execCopy(txt));
    }

    // ════════════════════════════════════════════════════════════════════════
    // (c) 单击词直弹字典 —— 照搬 epub-html.js 单击直翻,适配 epub.js iframe:
    //     · 由 attachSel 的 pointerdown/up「无位移 tap」触发(比 iframe `click` 在 iOS 上可靠),坐标用 iframe 内坐标系。
    //     · caret 兼容:Chrome/Safari = caretRangeFromPoint,Firefox = caretPositionFromPoint。
    //     · 命中矩形换算同 captureSelection(iframe getBoundingClientRect 偏移 + 词 range rect → 父视口坐标)。
    //     · 命中英/日词 → 直接 RC.wordpop.show(不弹工具栏);纯中文等不做事。
    // ════════════════════════════════════════════════════════════════════════
    function _clickTranslate() { var v = localStorage.getItem('eph-click-translate'); return v === null ? true : v === '1'; }
    function caretFromPoint(doc, x, y) {
      if (doc.caretRangeFromPoint) { var r = doc.caretRangeFromPoint(x, y); return r ? { node: r.startContainer, offset: r.startOffset } : null; }
      if (doc.caretPositionFromPoint) { var p = doc.caretPositionFromPoint(x, y); return p ? { node: p.offsetNode, offset: p.offset } : null; }
      return null;
    }
    function wordAt(node, off) {
      var s = node.nodeValue || ''; if (!s) return null;
      var isW = function (c) { return /[A-Za-z0-9'’\-]/.test(c) || /[぀-ヿ㐀-鿿가-힯一-鿿]/.test(c); };
      var i = off; if (i >= s.length) i = s.length - 1; if (i < 0) return null;
      if (!isW(s[i])) { if (i > 0 && isW(s[i - 1])) i--; else return null; }
      var lo = i, hi = i + 1;
      while (lo > 0 && isW(s[lo - 1])) lo--;
      while (hi < s.length && isW(s[hi])) hi++;
      return { node: node, start: lo, end: hi, text: s.slice(lo, hi) };
    }
    function onCaretTapWord(node, offset, win, doc) {   // iOS iframe 单击:用 selectionchange 放的折叠光标取词弹字典(touch/click 在 iframe 里不触发)
      try {
        if (Date.now() - _lastDictTs < 700 || Date.now() - _justDragged < 700) return;   // 浮层刚查词/刚拖选 → 别重复、别清
        _clearCustomSel();   // 点别处(空白/中文)→ 清掉多词自定义选区
        var w = wordAt(node, offset);
        if (!w || !w.text) { hideSel(); _clearTapMark(); _closeWordPop(); return; }
        var t = w.text, isJa = /[぀-ヿ]/.test(t);
        if (!isJa) { try { win.getSelection().removeAllRanges(); } catch (_) {} hideSel(); _clearTapMark(); _closeWordPop(); return; }   // 英文走父文档浮层按钮(精确);中文/空白光标 → 只关框,杜绝「点空白光标锁别处→误查」
        var rng; try { rng = doc.createRange(); rng.setStart(w.node, w.start); rng.setEnd(w.node, w.end); } catch (er) { hideSel(); return; }   // 仅日语:暂无分词浮层 → 轮询折叠光标取词
        var r = rng.getBoundingClientRect(), ifr = findIframe(doc), ib = ifr ? ifr.getBoundingClientRect() : { left: 0, top: 0 };
        var rect = { left: ib.left + r.left, top: ib.top + r.top, right: ib.left + r.right, bottom: ib.top + r.bottom };
        var pblk = w.node.parentElement && w.node.parentElement.closest ? w.node.parentElement.closest('p,li,td,blockquote,h1,h2,h3,h4,div') : null;
        var pctx = (pblk ? (pblk.textContent || '') : '').trim().slice(0, 1200);
        // 临时高亮标记选中词(span 包,不用原生选区 → 不触发 iOS 原生菜单;下次单击/取词时清掉)
        _clearTapMark();
        try { _ensureMarkCss(doc); var _mk = doc.createElement('span'); _mk.className = 'ep-tap-mark'; rng.surroundContents(_mk); _tapMark = _mk; } catch (e) {}
        try { win.getSelection().removeAllRanges(); } catch (_) {}   // 清折叠光标(防轮询再触发 + 防 iOS 菜单)
        hideSel();
        if (!_dictGate()) { _tlog('caretTap deduped'); return; }
        _tlog('caretTap CALL wordpop ' + t);
        RC.wordpop.show({
          word: t, rect: rect, ctx: pctx, file: FREL, langs: (window.__epubDeco ? __epubDeco.bookLangs() : []),
          markHighlight: function () {}, onGrammar: function (w) { if (window.onGrammarAnalyze) window.onGrammarAnalyze({ word: w, ctx: pctx }); }, onMastered: function () { if (window.refreshVocabUnderlinesForAllPages) window.refreshVocabUnderlinesForAllPages(); },
          onFallback: function (word) { RC.result.aiCall('/pdf/api/translate', { text: word, target_lang: '中文' }, '🌐 翻译', { aiParams: function () { return (window.RC && RC.settings) ? RC.settings.aiParams() : {}; } }); }
        });
      } catch (e) { _tlog('caretTap ERR ' + (e && e.message)); }
    }
    function onIframeTap(x, y, win, doc) {
      try {
        var sel = win.getSelection();
        _tlog('onTap enter; selCollapsed=' + (sel ? sel.isCollapsed : 'nosel'));
        if (sel && !sel.isCollapsed && (sel.toString() || '').trim()) { _tlog('onTap: has selection, skip'); return; }
        if (_customSel) _clearCustomSel();   // 点内容(空白/新词)→ 关掉上次多词自定义选区工具栏(自定义选区无原生 range,空轮询故意不清它 → 必须这里显式清)
        var tgt = (doc.elementFromPoint ? doc.elementFromPoint(x, y) : null);
        if (tgt && tgt.closest && tgt.closest('a, img')) return;   // 链接/图片不当词处理
        var pos = caretFromPoint(doc, x, y);
        _tlog('caret=' + (pos && pos.node ? ('node nt=' + pos.node.nodeType + ' off=' + pos.offset) : 'NULL'));
        if (!pos || !pos.node || pos.node.nodeType !== 3) { _tlog('onTap: caret not text, abort'); return; }
        var w = wordAt(pos.node, pos.offset); _tlog('wordAt=' + (w ? '"' + w.text + '"' : 'NULL'));
        if (!w || !w.text) return;
        var t = w.text, isEn = /^[A-Za-z][A-Za-z'’\-]*$/.test(t);
        var isJa = /[぀-ヿ]/.test(t);
        _tlog('word="' + t + '" isEn=' + isEn + ' isJa=' + isJa);
        var rng = doc.createRange();
        try { rng.setStart(w.node, w.start); rng.setEnd(w.node, w.end); } catch (er) { return; }
        if (!isJa && !isEn) { try { sel.removeAllRanges(); } catch (_) {} hideSel(); return; }   // 纯中文等 → 单击不做事 + 清掉
        // 单击词「直翻」关(eph-click-translate=0)→ 选中该词 + 弹工具栏(查词);开(默认)→ 直接弹字典小框
        if (!_clickTranslate()) {
          try { sel.removeAllRanges(); sel.addRange(rng); } catch (er2) { return; }
          setTimeout(function () { captureSelection(win, doc); }, 0);
          return;
        }
        var r = rng.getBoundingClientRect();
        var ifr = findIframe(doc), ib = ifr ? ifr.getBoundingClientRect() : { left: 0, top: 0 };
        var rect = { left: ib.left + r.left, top: ib.top + r.top, right: ib.left + r.right, bottom: ib.top + r.bottom };
        var pblk = w.node.parentElement && w.node.parentElement.closest ? w.node.parentElement.closest('p,li,td,blockquote,h1,h2,h3,h4,div') : null;
        var pctx = (pblk ? (pblk.textContent || '') : '').trim().slice(0, 1200);
        try { sel.removeAllRanges(); } catch (_) {}   // 不留视觉选中(字典框自定位)
        hideSel();
        if (!_dictGate()) { _tlog('onTap deduped'); return; }
        _tlog('CALL RC.wordpop.show word=' + t);
        RC.wordpop.show({
          word: t, rect: rect, ctx: pctx, file: FREL, langs: [],
          onGrammar: function (w) { if (window.onGrammarAnalyze) window.onGrammarAnalyze({ word: w, ctx: pctx }); },
          onMastered: function () { if (window.refreshVocabUnderlinesForAllPages) window.refreshVocabUnderlinesForAllPages(); },
          onFallback: function (word) { RC.result.aiCall('/pdf/api/translate', { text: word, target_lang: '中文' }, '🌐 翻译', mkResultOpts('note', pctx)); }
        });
      } catch (_) {}
    }

    // ── 双击选行 / 三击选段(对照 PDF 13-selection 的 _lineExpandFromChar / _paragraphExpandFromChar)──
    // reflow 没有 PDF 那种 char bbox 表 → 直接在 iframe DOM 上算:行=同一视觉行(getClientRects 的 y 同行容差),段=所在块元素。
    function _blkOf(node) { var el = (node && node.nodeType === 3) ? node.parentElement : node; return (el && el.closest) ? el.closest('p,li,td,blockquote,div,section,h1,h2,h3,h4') : el; }
    function _charRectAt(doc, node, off) {
      try { var s = node.nodeValue || ''; if (!s) return null; var i = Math.min(off, s.length - 1); if (i < 0) i = 0; var r = doc.createRange(); r.setStart(node, i); r.setEnd(node, Math.min(i + 1, s.length)); var rc = r.getClientRects(); return (rc && rc[0]) ? rc[0] : null; } catch (e) { return null; }
    }
    function _epLineRange(doc, node, off) {   // 选「同一视觉行」:扫块内所有字符,纳入 midY 落在点中行 ±0.6 行高内的连续段
      try {
        var blk = _blkOf(node); if (!blk) return null;
        var cr = _charRectAt(doc, node, off); if (!cr) return null;
        var midY = (cr.top + cr.bottom) / 2, lh = (cr.bottom - cr.top) || 16, tol = lh * 0.6;
        var w = doc.createTreeWalker(blk, NodeFilter.SHOW_TEXT, null), tn, sN = null, sO = 0, eN = null, eO = 0;
        while ((tn = w.nextNode())) {
          var s = tn.nodeValue || '';
          for (var i = 0; i < s.length; i++) {
            var rg = doc.createRange(); rg.setStart(tn, i); rg.setEnd(tn, i + 1);
            var rc = rg.getClientRects()[0]; if (!rc || (!rc.width && !rc.height)) continue;
            if (Math.abs((rc.top + rc.bottom) / 2 - midY) <= tol) { if (!sN) { sN = tn; sO = i; } eN = tn; eO = i + 1; }
          }
        }
        if (!sN) return null;
        var out = doc.createRange(); out.setStart(sN, sO); out.setEnd(eN, eO); return out;
      } catch (e) { return null; }
    }
    function _epBlockRange(doc, node) { try { var blk = _blkOf(node); if (!blk) return null; var r = doc.createRange(); r.selectNodeContents(blk); return r; } catch (e) { return null; } }
    function _epMultiClick(count, x, y, win, doc) {
      try {
        var tgt = doc.elementFromPoint ? doc.elementFromPoint(x, y) : null;
        if (tgt && tgt.closest && tgt.closest('a, img')) return;   // 链接/图片不当文本处理
        var pos = caretFromPoint(doc, x, y);
        if (!pos || !pos.node || pos.node.nodeType !== 3) return;
        var range = (count === 2) ? _epLineRange(doc, pos.node, pos.offset) : _epBlockRange(doc, pos.node);
        if (!range || !(range.toString() || '').trim()) return;
        _closeWordPop(); _clearTapMark(); _clearCustomSel();
        var sel = win.getSelection(); try { sel.removeAllRanges(); sel.addRange(range); } catch (_) {}
        setTimeout(function () { captureSelection(win, doc); }, 0);   // 走选区桥接 → 多词工具栏
      } catch (e) {}
    }

    // ════════════════════════════════════════════════════════════════════════
    // (d) 顶栏 / 设置 / scrubber / 全屏 / 统一抽屉 —— 镜像手搓版 epub_html_reader.html(=PDF 阅读器)的 chrome,
    //     底座换成 epub.js:进度走 rendition 'relocate' + spine;跳章走 rendition.display(spineIdx);
    //     字号/主题/行距落到 window.__epub.controls(epub-reader.js 的 rendition.themes)。
    //     未迁移功能(振假名 / 译页 / 搜索 / 单词本 / 知识点 / 高亮 / 助手对话)→ 占位 toast,不报错。
    // ════════════════════════════════════════════════════════════════════════
    var CONTROLS = (window.__epub && window.__epub.controls) || null;
    var COUNT = 0, curIdx = 0, _tocMarks = [];
    var MIGRATING = '该功能正在迁移到 epub.js 版';
    // ── 单词本 pane(照搬手搓版/PDF reader.src loadVocabPane;本章 scope 的章节文本走 RC.adapter().currentChapterText)──
    var _vocabScope = 'book', _vocabPaneLoaded = false, _vocabRefreshT = null;
    function _vMColor(m) { if (m >= 0.8) return '#22c55e'; if (m >= 0.5) return '#eab308'; if (m >= 0.2) return '#f97316'; return '#ef4444'; }
    function _vIsJa(w) { return /[぀-ヿ㐀-鿿一-鿿]/.test(w || ''); }
    function _vTts(w, lang) { lang = lang || 'en-US'; try { speechSynthesis.cancel(); var u = new SpeechSynthesisUtterance(w); u.lang = lang; var pref = lang.slice(0, 2).toLowerCase(), nm = function (x) { return (x || '').toLowerCase().replace('_', '-'); }; var vs = speechSynthesis.getVoices() || []; var v = vs.find(function (x) { return nm(x.lang) === lang.toLowerCase(); }) || vs.find(function (x) { return nm(x.lang).indexOf(pref) === 0; }); if (v) u.voice = v; speechSynthesis.speak(u); } catch (e) {} }
    function _vSpeakOnline(w) { if (!w) return; if (_vIsJa(w)) { _vTts(w, 'ja-JP'); return; } try { var a = new Audio('https://dict.youdao.com/dictvoice?type=2&audio=' + encodeURIComponent(w)); a.play().catch(function () { _vTts(w, 'en-US'); }); } catch (e) { _vTts(w, 'en-US'); } }
    function _vSpeak(lemma, audio) { if (audio) { try { var a = new Audio('/pdf/api/vocab-audio?path=' + encodeURIComponent(audio)); a.play().catch(function () { _vSpeakOnline(lemma); }); return; } catch (e) {} } _vSpeakOnline(lemma); }
    function _vAddAnki(lemma, btn) { var old = btn.textContent; btn.textContent = '…'; btn.disabled = true; fetch('/pdf/api/vocab-anki', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ word: lemma }) }).then(function (r) { return r.json(); }).then(function (d) { if (d && d.ok) { btn.textContent = '✓ 已加'; btn.classList.add('done'); } else { btn.textContent = old; btn.disabled = false; toast('加卡失败:' + ((d && d.error) || '?')); } }).catch(function (e) { btn.textContent = old; btn.disabled = false; toast('加卡失败:' + (e.message || '网络错误')); }); }
    function _vRenderItem(it) {
      var div = document.createElement('div'); div.className = 'vocab-item';
      var pct = Math.round((it.mastery || 0) * 100), col = _vMColor(it.mastery || 0);
      div.innerHTML = '<div class="vi-head"><span class="vi-word">' + esc(it.lemma) + '</span>' +
        (it.phonetic ? '<span class="vi-phon">' + esc(it.phonetic) + '</span>' : '') +
        '<button class="vi-audio" title="发音">🔊</button>' +
        '<span class="vi-mastery-badge" style="background:' + col + '22;color:' + col + '">' + esc(it.mastery_label || (pct + '%')) + '</span></div>' +
        '<div class="vi-bar"><div style="width:' + pct + '%;background:' + col + '"></div></div>' +
        (it.zh ? '<div class="vi-zh">' + esc(it.zh) + '</div>' : '') +
        '<div class="vi-foot"><span class="vi-pages"></span><button class="vi-anki">' + (it.has_card ? '✓ 已加' : '📇 加卡') + '</button></div>';
      if (it.has_card) div.querySelector('.vi-anki').classList.add('done');
      div.querySelector('.vi-word').addEventListener('click', function () { var rect = this.getBoundingClientRect(); if (window.RC && RC.wordpop) RC.wordpop.show({ word: it.lemma, rect: rect, file: FREL, langs: (window.__epubDeco ? __epubDeco.bookLangs() : []), onGrammar: function (w) { if (window.onGrammarAnalyze) window.onGrammarAnalyze({ word: w, ctx: '' }); } }); });
      div.querySelector('.vi-audio').addEventListener('click', function (e) { e.stopPropagation(); _vSpeak(it.lemma, it.audio); });
      div.querySelector('.vi-anki').addEventListener('click', function (e) { e.stopPropagation(); var b = e.currentTarget; if (!b.classList.contains('done')) _vAddAnki(it.lemma, b); });
      return div;
    }
    function _vFilterChapter(items) {
      var txt = ''; try { var ad = window.RC && RC.adapter && RC.adapter(); if (ad && ad.currentChapterText) txt = ad.currentChapterText() || ''; } catch (e) {}
      if (!txt) return [];
      var lc = txt.toLowerCase();
      return items.filter(function (it) {
        var lem = (it.lemma || '').trim(); if (!lem) return false;
        if (/^[a-z][a-z'’\-]*$/i.test(lem)) { var re = lem.toLowerCase().replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); return new RegExp('\\b' + re + '\\b', 'i').test(lc); }
        return txt.indexOf(lem) >= 0;
      });
    }
    window.loadVocabPane = function (scope) {
      if (scope) _vocabScope = scope;
      _vocabPaneLoaded = true;
      var listEl = $('ep-vocab-list'), cntEl = $('ep-vocab-count'); if (!listEl) return;
      document.querySelectorAll('#ep-vocab-scope-row button').forEach(function (b) { b.classList.toggle('active', b.dataset.scope === _vocabScope); });
      listEl.innerHTML = '<div style="color:#5a6680;font-size:12px;padding:10px">加载中…</div>'; if (cntEl) cntEl.textContent = '';
      var backendScope = (_vocabScope === 'all') ? 'all' : 'book';
      fetch('/pdf/api/vocab-list?file=' + encodeURIComponent(FREL || '') + '&scope=' + backendScope).then(function (r) { return r.json(); }).then(function (d) {
        var items = (d && d.items) || [];
        if (_vocabScope === 'chapter') items = _vFilterChapter(items);
        window.__lastVocab = items;
        if (cntEl) cntEl.textContent = items.length ? (items.length + ' 词') : '';
        if (!items.length) { var emp = { chapter: '本章没出现已查过的单词', book: '这本书还没查过单词', all: '单词库为空' }[_vocabScope] || '没有单词'; listEl.innerHTML = '<div style="color:#5a6680;font-size:12px;padding:10px">' + emp + '</div>'; return; }
        listEl.innerHTML = ''; items.forEach(function (it) { listEl.appendChild(_vRenderItem(it)); });
      }).catch(function (e) { listEl.innerHTML = '<div style="color:#ef4444;font-size:12px;padding:10px">加载失败:' + esc(e.message || '网络错误') + '</div>'; });
    };
    function _refreshVocabIfChapter() { var pane = $('ep-side-vocab'); if (!(pane && pane.classList.contains('active') && _vocabScope === 'chapter')) return; clearTimeout(_vocabRefreshT); _vocabRefreshT = setTimeout(function () { window.loadVocabPane(); }, 350); }
    try { R.on('relocate', _refreshVocabIfChapter); } catch (e) {}

    // 给每章 iframe doc 标真实 section idx(rendered 时),scrub/进度从「视口顶部可见章」取(连续模式 relocate.start.index 给的是第一渲染章=封面,不准)
    try { R.on('rendered', function (section, view) { try { if (section && view && view.contents && view.contents.document && typeof section.index === 'number') view.contents.document.__epSecIdx = section.index; } catch (e) {} }); } catch (e) {}
    function _liveCurIdxE() {
      try {
        var cs = R.getContents() || [], vh = window.innerHeight || 800;
        for (var k = 0; k < cs.length; k++) {
          var d = cs[k].document; if (!d || typeof d.__epSecIdx !== 'number') continue;
          var ifr = findIframe(d); if (!ifr) continue;   // 父文档侧找 iframe(沙箱 iframe 里 frameElement 为 null;findIframe 有 querySelector 兜底)
          var r = ifr.getBoundingClientRect();
          if (r.bottom > 60 && r.top < vh) return d.__epSecIdx;   // 该章 iframe 底部还在视口里 = 顶部可见章
        }
      } catch (e) {}
      return null;
    }
    function updateProgress(i) {
      var c = $('ep-page-cur'), t = $('ep-page-total'), bar = $('ep-bar');
      if (c) c.textContent = (i + 1);
      if (t) t.textContent = '/ ' + (COUNT || '–');
      if (bar) bar.style.width = (COUNT > 1 ? Math.round(i / (COUNT - 1) * 100) : 0) + '%';
    }
    function gotoIndex(i) { if (CONTROLS && CONTROLS.gotoIndex) CONTROLS.gotoIndex(i); else { try { R.display(i); } catch (e) {} } }
    try { B.ready.then(function () {
      try { COUNT = (B.spine && B.spine.items && B.spine.items.length) || (B.spine && B.spine.length) || 0; } catch (e) {}
      updateProgress(curIdx);
    }); } catch (e) {}
    try { R.on('relocate', function (loc) {
      try { var lv = _liveCurIdxE(); var i = (lv != null) ? lv : ((loc && loc.start && typeof loc.start.index === 'number') ? loc.start.index : curIdx); curIdx = i; updateProgress(i); } catch (e) {}
    }); } catch (e) {}
    // 连续模式 relocate 不保证每次滚动都发 → 页码也挂滚动监听(rAF 节流)
    var _scrubRaf = 0;
    function _scrubFromScroll() { if (_scrubRaf) return; _scrubRaf = requestAnimationFrame(function () { _scrubRaf = 0; try { var i = _liveCurIdxE(); if (i != null && i !== curIdx) { curIdx = i; updateProgress(i); } } catch (e) {} }); }
    try { var _vpS = document.getElementById('ep-viewer'); if (_vpS) _vpS.addEventListener('scroll', _scrubFromScroll, true); } catch (e) {}
    try { window.addEventListener('scroll', _scrubFromScroll, true); } catch (e) {}

    // ── 目录:渲进统一抽屉「目录」pane(#ep-toc-list)+ scrubber 章名映射(toc href → spine idx)──
    function _flatToc(items, out) { (items || []).forEach(function (it) { out.push(it); if (it.subitems && it.subitems.length) _flatToc(it.subitems, out); }); return out; }
    function buildTocList(toc) {
      var box = $('ep-toc-list'); if (!box) return; box.innerHTML = '';
      var arr = _flatToc(toc, []);
      arr.forEach(function (it) {
        var a = document.createElement('div'); a.className = 'ep-toc-i'; a.textContent = (it.label || '').trim();
        a.addEventListener('click', function () { if (window.RC && RC.sidedrawer) RC.sidedrawer.close(); try { R.display(it.href); } catch (e) {} });
        box.appendChild(a);
      });
      if (!arr.length) box.innerHTML = '<div class="ep-empty">无目录</div>';
    }
    function buildTocMarks(toc) {
      _tocMarks = [];
      _flatToc(toc, []).forEach(function (it) {
        try { var href = (it.href || '').split('#')[0]; var sp = B.spine.get(href); if (sp && typeof sp.index === 'number') _tocMarks.push({ idx: sp.index, label: (it.label || '').trim() }); } catch (e) {}
      });
      _tocMarks.sort(function (a, b) { return a.idx - b.idx; });
    }
    function chapLabel(idx) { var lab = ''; for (var i = 0; i < _tocMarks.length; i++) { if (_tocMarks[i].idx <= idx) lab = _tocMarks[i].label; else break; } return lab; }
    try { B.loaded.navigation.then(function (nav) { try { buildTocList(nav.toc || []); } catch (e) {} try { buildTocMarks(nav.toc || []); } catch (e) {} }); } catch (e) {}

    // ── ⚙ 设置(RC.settings.open;字号/主题/行距 → window.__epub.controls → rendition.themes;model/effort/转PDF 真能用)──
    function convertToFull(btn) {
      if (!btn) return; btn.disabled = true; btn.textContent = '⏳ 处理中…';
      fetch('/pdf/api/epub-to-full', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ file: FREL }) })
        .then(function (r) { return r.json(); }).then(function (d) {
          if (!d) { btn.textContent = '✗ 失败'; btn.disabled = false; return; }
          if (d.ready) { location.href = d.view_url; return; }
          var iv = setInterval(function () {
            fetch('/pdf/api/ebook-convert-status?job=' + encodeURIComponent(d.job)).then(function (r) { return r.json(); }).then(function (s) {
              if (s.status === 'done') { clearInterval(iv); location.href = s.view_url || d.view_url; }
              else if (s.status === 'error') { clearInterval(iv); btn.textContent = '✗ 转换失败'; btn.disabled = false; }
              else btn.textContent = '⏳ 转换中(可关页面)…';
            }).catch(function () {});
          }, 5000);
        }).catch(function (e) { btn.textContent = '✗ ' + (e && e.message || '网络错误'); btn.disabled = false; });
    }
    function openSettings(tab) {
      if (!(window.RC && RC.settings)) { toast('设置未就绪,刷新重试'); return; }
      RC.settings.open({
        tab: tab,
        getReadState: function () { return CONTROLS ? CONTROLS.getReadState() : { fs: 100, th: 'paper', lh: 1.6 }; },
        onFontSize: function (d) { if (CONTROLS) CONTROLS.onFontSize(d); },
        onLineHeight: function (d) { if (CONTROLS) CONTROLS.onLineHeight(d); },
        onTheme: function (th) { if (CONTROLS) CONTROLS.onTheme(th); },
        onConvertFull: function (btn) { convertToFull(btn); },
        getBookLangs: function () { return (window.__epubDeco && __epubDeco.bookLangs()) || []; },
        onSaveLangs: function () { if (window.saveLangPicker) window.saveLangPicker(); },
        onVocabUnderline: function (on) { if (window.__epubDeco) __epubDeco.setVocabUnderline(on); },
        onClickTranslate: function (on) { try { localStorage.setItem('eph-click-translate', on ? '1' : '0'); } catch (e) {} },  // 真生效(控制单击 tap 行为)
        onHlColors: function () {}
      });
    }
    var _setBtn = $('ep-set-btn'); if (_setBtn) _setBtn.addEventListener('click', function () { openSettings(); });

    // ── 章节 scrubber(横拖跳章 + 浮层;reflow 按 spine idx)。交互逐字对齐 PDF 05-nav 的 _setupPageScrub:
    //    pointermove 加「!moved return」4px gate(微动不闪浮层)、浮层/page-cur 显页码(idx+1 / COUNT)而非章名、
    //    点击(没拖)→ prompt「跳到第几页…」、取消/非法 → _refreshCur 复位。EPUB 无印刷页偏移概念,「页」= 章 idx+1。──
    (function setupScrub() {
      var sc = $('ep-scrub'), pop = $('ep-scrub-pop'); if (!sc || !pop) return;
      var drag = null;
      function vw() { var v = $('ep-viewer'); return Math.max(220, ((v && v.clientWidth) || window.innerWidth) * 0.7); }
      function _refreshCur() { var c = $('ep-page-cur'); if (c) c.textContent = (curIdx + 1); }   // 对齐 PDF window._refreshPageCur:复位 page-cur 到当前页
      function showPop(i) { pop.classList.add('on'); var n = pop.querySelector('.psp-num'); if (n) n.textContent = (i + 1) + ' / ' + COUNT; var f = pop.querySelector('.psp-fill'); if (f) f.style.width = (COUNT > 1 ? i / (COUNT - 1) * 100 : 0) + '%'; }
      sc.addEventListener('pointerdown', function (e) { if (!COUNT) return; drag = { x: e.clientX, start: curIdx, moved: false, tgt: curIdx }; try { sc.setPointerCapture(e.pointerId); } catch (er) {} e.preventDefault(); });
      sc.addEventListener('pointermove', function (e) {
        if (!drag) return;
        var dx = e.clientX - drag.x;
        if (Math.abs(dx) > 4) drag.moved = true;
        if (!drag.moved) return;   // 4px gate:微动不闪浮层(对齐 PDF)
        var tgt = Math.max(0, Math.min(COUNT - 1, Math.round(drag.start + dx / vw() * COUNT)));
        drag.tgt = tgt;
        var c = $('ep-page-cur'); if (c) c.textContent = (tgt + 1);   // 拖动时 page-cur 显目标页码
        showPop(tgt);
      });
      sc.addEventListener('pointerup', function (e) {
        if (!drag) return; var d = drag; drag = null; pop.classList.remove('on'); try { sc.releasePointerCapture(e.pointerId); } catch (er) {}
        if (d.moved) { gotoIndex(d.tgt); }
        else {
          var v = prompt('跳到第几页(按书上印的页码,共 ' + COUNT + ')', String(curIdx + 1));
          var p = parseInt(v, 10);
          if (p) gotoIndex(Math.max(0, Math.min(COUNT - 1, p - 1)));   // 输入是 1-based 页码 → 转回 0-based 章 idx
          else _refreshCur();
        }
      });
      sc.addEventListener('pointercancel', function () { drag = null; pop.classList.remove('on'); _refreshCur(); });
    })();

    // ── ⛶ 全屏(body.fs-mode 隐顶栏 + Fullscreen API + 记忆;照搬手搓版 setupFs;顶栏显隐改了 #ep-viewer 高度 → R.resize 重排)──
    (function setupFs() {
      var btn = $('fs-toggle'), rest = $('fs-restore'); if (!btn) return;
      function reflow() { try { setTimeout(function () { R.resize(); }, 80); } catch (e) {} }
      // 浏览器/PWA 真·全屏助手(webkit 前缀回退;iOS Safari 文档级不支持 → 静默失败,只保留页内全屏),对齐 PDF 06-layout
      function _fsActive() { return !!(document.fullscreenElement || document.webkitFullscreenElement); }
      function _reqFs() { var el = document.documentElement, fn = el.requestFullscreen || el.webkitRequestFullscreen; if (fn && !_fsActive()) { try { Promise.resolve(fn.call(el)).catch(function () {}); } catch (e) {} } }
      function _exitFs() { var fn = document.exitFullscreen || document.webkitExitFullscreen; if (fn && _fsActive()) { try { Promise.resolve(fn.call(document)).catch(function () {}); } catch (e) {} } }
      function set(on) {
        document.body.classList.toggle('fs-mode', on); btn.classList.toggle('active', on);
        try { localStorage.setItem('eph-fs-mode', on ? '1' : '0'); } catch (e) {}
        if (on) _reqFs(); else _exitFs();
        reflow();
        toast(on ? '全屏阅读：点右上角 ⤢ 恢复' : '已退出全屏');
      }
      btn.addEventListener('click', function () { set(!document.body.classList.contains('fs-mode')); });
      if (rest) rest.addEventListener('click', function () { set(false); });
      // 用户用 Esc/F11 退出浏览器全屏 → 同步退出页内全屏(顶栏恢复),防顶栏卡死(对齐 PDF fullscreenchange)
      ['fullscreenchange', 'webkitfullscreenchange'].forEach(function (ev) {
        document.addEventListener(ev, function () { if (!_fsActive() && document.body.classList.contains('fs-mode')) set(false); });
      });
      if (localStorage.getItem('eph-fs-mode') === '1') { document.body.classList.add('fs-mode'); btn.classList.add('active'); reflow(); }
    })();

    // ── 双指捏合缩放(对照 PDF 06-layout _setupPinchZoom):EPUB reflow 无栅格 → 捏合映射到字号步进(onFontSize) ──
    // document + 各章 iframe doc 都挂 gesturestart/gesturechange preventDefault 挡 iOS 原生位图缩放(糊);
    // 触摸落在 iframe 里不冒泡到父 #ep-viewer → 同款 touch/wheel 监听也挂进每章 doc。desktop Ctrl+滚轮 同映射(±10 字号,与设置面板 +/- 一致)。
    (function setupEpubPinch() {
      var viewer = $('ep-viewer'), STEP = 10;
      function _noGesture(e) { try { e.preventDefault(); } catch (_) {} }
      try { document.addEventListener('gesturestart', _noGesture, { passive: false }); document.addEventListener('gesturechange', _noGesture, { passive: false }); } catch (e) {}
      function applyPinch(d0, d1) {
        if (!CONTROLS || !CONTROLS.onFontSize || !d0 || !d1) return;
        var ratio = d1 / d0, steps = 0;
        if (ratio > 1.05) steps = Math.round(Math.log(ratio) / Math.log(1.15));
        else if (ratio < 0.95) steps = -Math.round(Math.log(1 / ratio) / Math.log(1.15));
        if (steps) CONTROLS.onFontSize(steps * STEP);   // onFontSize 内部已 clamp 70-220
      }
      function attachTouch(target) {
        var d0 = 0, dLast = 0;
        target.addEventListener('touchstart', function (e) { if (e.touches && e.touches.length === 2) { var a = e.touches[0], b = e.touches[1]; d0 = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) || 0; dLast = d0; } }, { passive: true });
        target.addEventListener('touchmove', function (e) { if (d0 && e.touches && e.touches.length === 2) { var a = e.touches[0], b = e.touches[1]; dLast = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) || dLast; if (e.cancelable) { try { e.preventDefault(); } catch (_) {} } } }, { passive: false });
        var endf = function () { if (d0 && dLast) applyPinch(d0, dLast); d0 = 0; dLast = 0; };
        target.addEventListener('touchend', function (e) { if (d0 && (!e.touches || e.touches.length < 2)) endf(); }, { passive: true });
        target.addEventListener('touchcancel', endf, { passive: true });
      }
      function attachWheel(target) { target.addEventListener('wheel', function (e) { if (!e.ctrlKey) return; if (e.cancelable) { try { e.preventDefault(); } catch (_) {} } if (CONTROLS && CONTROLS.onFontSize) CONTROLS.onFontSize(e.deltaY < 0 ? STEP : -STEP); }, { passive: false }); }
      if (viewer) { attachTouch(viewer); attachWheel(viewer); }
      function attachDoc(doc) {
        if (!doc || doc.__epPinch) return; doc.__epPinch = 1;
        try { doc.addEventListener('gesturestart', _noGesture, { passive: false }); doc.addEventListener('gesturechange', _noGesture, { passive: false }); } catch (e) {}
        attachTouch(doc); attachWheel(doc);
      }
      try { (R.getContents() || []).forEach(function (c) { attachDoc(c.document); }); } catch (e) {}
      try { R.on('rendered', function (sec, view) { try { if (view && view.contents) attachDoc(view.contents.document); } catch (e) {} }); } catch (e) {}
      try { R.hooks.content.register(function (c) { try { attachDoc(c.document); } catch (e) {} }); } catch (e) {}
    })();

    // ── 顶栏未迁移按钮(布局与 PDF 一致,功能在后续 phase 搬):占位 toast,不报错 ──
    [].forEach(function (id) { var b = $(id); if (b) b.addEventListener('click', function () { toast(MIGRATING); }); });

    // ── 右侧统一抽屉(RC.sidedrawer):助手(snippets 卡)/ 单词本 / 知识点 / 高亮 / 目录 ──
    if (window.RC && RC.sidedrawer) {
      RC.sidedrawer.init({
        handleLabel: '助手 · 知识点', defaultTab: 'asst',
        onTab: function (name) {
          if (name === 'vocab') { if (!_vocabPaneLoaded) window.loadVocabPane(); }
          else if (name === 'hl') { if (window.epubHl && window.epubHl.loadPane) window.epubHl.loadPane(); }   // 高亮列表由 epub2-highlight.js 渲染(P3)
          // toc 由 buildTocList 填好;asst pane snippets 卡渲进 #ep-ai-body
        }
      });
    }
    // 助手对话/语音/快捷/感叹号 由 epub2-assist.js 接管(P8)
  }
})();
