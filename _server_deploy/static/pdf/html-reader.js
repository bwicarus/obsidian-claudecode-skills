/* html-reader.js — 统一 HTML 阅读器驱动 + HtmlAdapter（架构验收）。
 *
 * 目标:证明「给一个新阅读器写个 adapter,共享功能就全有」。HTML 内容直接渲在主文档(#html-content,无 iframe),
 *   通过 HtmlAdapter 接入共享控制层 rc-*.js,于是 选区/查词/翻译/解释/对话/笔记/制卡/高亮 全部可用——
 *   业务逻辑零重写(全部走 RC.wordpop / RC.result / RC.snippets / RC.highlight / RC.sidedrawer / RC.settings)。
 *
 * 比 EPUB(epub2.js)简单:无 iframe → 原生 window.getSelection() 就行,坐标无需跨 iframe 换算,
 *   不必搞 EPUB 那套折叠光标轮询 / 父文档分词浮层(那是 iframe 专属坑)。
 *
 * 锚:字符偏移(相对 #html-content 的文本偏移,用 Range 计长 + TreeWalker 还原)。
 *   高亮存独立 sidecar(/pdf/api/html-highlights → state/html-highlights/<sha>.json)。
 *
 * 纯新增,不碰 PDF/EPUB 阅读器,不改 rc-*.js。ES5。
 */
(function () {
  'use strict';
  function ready(fn) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fn);
    else fn();
  }
  ready(init);

  function init() {
    var CFG = window.HTML_CFG || {};
    var FREL = CFG.fileRel || '';
    var $ = function (id) { return document.getElementById(id); };
    var elContent = $('html-content');
    var selBar = $('html-sel');
    if (!elContent || !selBar) return;

    function esc(s) { return (window.RC && RC.esc) ? RC.esc(s) : String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
    function toast(m) { if (window.RC && RC.toast) RC.toast(m); }
    function aiParams() { return (window.RC && RC.settings && RC.settings.aiParams) ? RC.settings.aiParams() : {}; }
    function hlColors() { return (window.RC && RC.settings && RC.settings.hlColors) ? RC.settings.hlColors() : ['#fff59d', '#a7f3d0', '#a3d4ff', '#fda4af']; }

    // 当前选区快照(cur=最近一次;_lastSel=供结果模态「🖌 标记」事后取锚)。rect 为视口坐标。
    var cur = { text: '', ctx: '', anchor: null, rect: null };
    var _lastSel = null;
    var _hls = [];   // 高亮列表 [{id,start,end,text,color,note,sentence}]

    // ════════════ 端点(AI 端点内容无关,直接复用 PDF/EPUB 那套;highlights 用新建的 html sidecar)════════════
    var EP = RC.contract.endpoints({ highlights: '/pdf/api/html-highlights' });

    // ════════════ HtmlAdapter:把 HTML 主文档底座收敛成统一 RC.adapter 契约(架构 P2)════════════
    var HtmlAdapter = {
      kind: 'html',
      config: RC.contract.adapterConfig('web'),
      getEndpoints: function () { return EP; },
      fileInfo: function () { return { file: FREL, langs: [] }; },
      captureSelection: function () { return captureFromSelection(); },
      clearSelection: function () { clearNativeSel(); },
      jumpToAnchor: function (a) {
        try {
          if (a && typeof a.start === 'number') {
            var r = _rangeFromOffsets(a.start, a.end || a.start);
            if (r) { var el = r.startContainer.nodeType === 3 ? r.startContainer.parentElement : r.startContainer; if (el && el.scrollIntoView) el.scrollIntoView({ block: 'center' }); }
          }
        } catch (e) {}
      },
      navigate: function (target) {
        var a = target && target.data ? target.data : (target || {});
        if (a && typeof a.start === 'number') { HtmlAdapter.jumpToAnchor(a); return true; }
        return false;
      },
      currentChapterText: function () { try { return (elContent.innerText || '').slice(0, 8000); } catch (e) { return ''; } },
      // ════ 2026-07-19 用户实锤"什么功能都没有":demo 级 adapter 缺 getContext/_host.asst,
      //      AI 侧栏拿不到上下文、大半工具哑。补齐成一等信息来源(镜像 epub-html.js 口径)。════
      getContext: function (opts) {
        opts = opts || {}; var sel = opts.selection || {};
        if (!sel.sel) {
          try {
            var c0 = captureFromSelection();
            if (window.__focusSel && window.__focusSel.text) sel = { sel: window.__focusSel.text, sent: '' };
            else if (c0 && c0.text) sel = { sel: c0.text, sent: c0.context || '' };
          } catch (e) {}
        }
        return {
          file: FREL, book: document.title.replace(/ ·.*$/, ''),
          langs: _docLangs(),                       // 网页语言按内容检测(无书级配置)
          visible_text: _visibleText(),             // 视口内正文=注意力焦点
          current_section_idx: 0, total_sections: 1,
          selection: sel.sel || '', selection_sentence: sel.sent || '',
          selection_anchor: sel.anchor || undefined
        };
      },
      currentLocation: function () { return { unit: 'page', index: 0, total: 1 }; },
      _host: {
        asst: {
          md: function (t) { return (window.RC && RC.md && RC.md.render) ? RC.md.render(t) : String(t || ''); },
          toast: function (m) { toast(m); },
          fmtTime: function (ms) { try { var s2 = Math.round((Date.now() - (ms || 0)) / 1000); return s2 < 60 ? (s2 + '秒前') : (s2 < 3600 ? (Math.round(s2 / 60) + '分钟前') : (Math.round(s2 / 3600) + '小时前')); } catch (e) { return ''; } },
          fileRel: function () { return FREL; },
          pdfNumPages: function () { return 1; },
          locCount: function () { return 1; },
          dispPage: function (p) { return p; }, pdfFromDisp: function (d) { return d; },
          changePage: function () {}, fitWidth: function () {}, zoomBy: function () {}, toggleTranslate: function () {},
          openDrawer: function () { try { RC.sidedrawer.open('asst'); } catch (e) {} },
          switchTab: function (n) { try { RC.sidedrawer.open(n); } catch (e) {} },
          asstOpen: function () { try { return !!document.querySelector('.ep-side-pane[data-pane="asst"].active'); } catch (e) { return false; } },
          voiceContext: function () { return null; },
          setFocusSel: function (t) { try { window.__focusSel = t ? { text: t } : null; } catch (e) {} },
          focusSel: function () { return window.__focusSel || null; },
          clearFigFocus: function () {}, figThumb: function () {},
          locLabel: function () { return ''; }, locNoun: function () { return '页'; },
          noteAttached: function () { return []; }, clearNoteAttached: function () {}, renderNoteChips: function () {},
          notesReload: function () {}, noteInject: function () { return false; },
          reloadHighlights: function () { try { loadHighlights(); } catch (e) {} },
          loadAllHighlights: function () { try { loadHighlights(); } catch (e) {} },
          renderHighlightsOnPage: function () {}, showHlPicker: function () {},
          assistEdit: function () { try { loadHighlights(); } catch (e) {} },   // AI 批量高亮后刷新
          renderPhraseHl: function () {}, removePhraseHighlight: function () {},
          activePhraseHl: function () { return null; }, setActivePhraseHl: function () {},
          charsRangeToText: function () { return ''; }, charRangeToPtRects: function () { return []; },
          flashSelOnPage: function (loc, text) {
            try {   // 定位到文中一句:找文本首次出现的偏移 → 滚过去(单文档语义)
              var t0 = (elContent.textContent || ''), i = text ? t0.indexOf(text.slice(0, 40)) : -1;
              if (i >= 0) { var r = _rangeFromOffsets(i, i + Math.min(40, (text || '').length)); if (r) { var el = r.startContainer.parentElement; el && el.scrollIntoView({ block: 'center' }); } }
            } catch (e) {}
          },
          noteNearText: function () { return ''; },
          jumpToCtx: function () { try { RC.sidedrawer.close(); } catch (e) {} },
          prewarm: function (off) { try { fetch('/api/assistant/prewarm', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(off ? { off: 1 } : {}), keepalive: true }); } catch (e) {} },
          getPaidNoted: function () { return !!window.__paidNoted; }, setPaidNoted: function (v) { window.__paidNoted = v; },
          hlUrl: function () { return '/pdf/api/html-highlights'; },
          showAction: function () { return null; }, queueAction: function () {}, taskAction: function () {},
          voiceLog: function () {},
          mountPanel: function () { return document.getElementById('ep-side'); },
          mountTabs: function () { return document.getElementById('ep-side-tabs') || document.getElementById('ep-side'); }
        }
      }
    };
    // 网页语言检测(内容采样;供查词路由/AI meta——网页没有书级语言配置)
    var _langsCache = null;
    function _docLangs() {
      if (_langsCache) return _langsCache;
      try {
        var t = (elContent.innerText || '').slice(0, 4000);
        var kana = (t.match(/[぀-ヿ]/g) || []).length, han = (t.match(/[㐀-鿿]/g) || []).length,
            lat = (t.match(/[A-Za-z]/g) || []).length, out = [];
        if (kana > 20) out.push('ja');
        if (lat > Math.max(han, kana) * 2 && lat > 200) out.push('en');
        _langsCache = out;
      } catch (e) { _langsCache = []; }
      return _langsCache;
    }
    function _visibleText() {
      try {   // 视口内文本:找可见区块拼接(注意力焦点口径,镜像 EPUB _visibleText 简版)
        var sc = document.getElementById('html-scroll'), top = sc.scrollTop, bot = top + sc.clientHeight;
        var out = [], nodes = elContent.querySelectorAll('p,li,h1,h2,h3,h4,blockquote,td');
        for (var i = 0; i < nodes.length; i++) {
          var el = nodes[i], y = el.offsetTop;
          if (y + el.offsetHeight < top) continue;
          if (y > bot) break;
          out.push(el.innerText || '');
          if (out.join('').length > 1600) break;
        }
        return out.join('\n').slice(0, 1600);
      } catch (e) { return ''; }
    }
    try { if (window.RC && RC.use) RC.use(HtmlAdapter); window.__htmlAdapter = HtmlAdapter; } catch (e) {}
    // 双向上下文同步:HTML 宿主没有 PDF/EPUB 那样的翻页漏斗可蹭,所以只在开关**已开**时
    // 开机上报一次(整篇单文档,页码无意义);之后 ts 由共享层的可见性心跳刷新。
    // 开关关着就连这一次都不发,更不挂监听——符合「关闭=零开销」。
    try {
      if (window.RC && RC.ctxSync && RC.ctxSync.enabled()) {
        RC.ctxSync.report({ kind: 'html', file: FREL, title: document.title.replace(/ ·.*$/, '') });
      }
      // HTML 宿主没有墨迹层,不调 drawingTouched —— 能力缺失即自然降级,不写宿主分支。
      // 文字焦点复用共享层同一入口。
      document.addEventListener('selectionchange', function () {
        if (!(window.RC && RC.outgoing)) return;
        var t = '';
        try { t = (window.getSelection() || {}).toString().trim(); } catch (e) {}
        // page 必填,Windows 侧的 CopyFocusReference 缺了它直接 throw、整条
        // 选区事件被丢掉。HTML 阅读器本来就没有"页"(html_reader.py 明确
        // 说明网页阅读独立状态、不进书的 reading-pos)——给固定的 1 不是
        // 编造页码,是如实反映"这份文档只有一页"。
        if (t && t.length >= 2) RC.outgoing.focus('text', { file: FREL, text: t.slice(0, 200), page: 1 });
        else RC.outgoing.cancel();
      });
    } catch (e) {}

    // PWA 书籍宿主白名单。HTML/Markdown 是本地书籍，不是“抓取网页”的 Web 宿主；
    // 扩展只得到本页实际具备的 offset 锚、高亮和阅读设置能力。
    try {
      if (window.BWReaderBookHost && !window.__bwReaderLocalApi) {
        function _bookSelection() {
          var value = HtmlAdapter.captureSelection();
          if (!value && cur && cur.text && cur.anchor) {
            value = {
              text: cur.text,
              context: cur.ctx || '',
              anchor: cur.anchor,
              rect: cur.rect || null
            };
          }
          if (!value) return null;
          value.file = FREL;
          value.book = (CFG && CFG.fileName) || document.title || '';
          value.langs = _docLangs();
          value.location = { unit: 'document', index: 0, total: 1 };
          return value;
        }
        function _bookAction(name, payload) {
          payload = payload || {};
          if (name === 'clear_selection') {
            HtmlAdapter.clearSelection();
            return { ok: true };
          }
          if (name === 'highlight') {
            var selected = _bookSelection();
            if (!selected || !selected.anchor) return Promise.reject(new Error('没有可标记的 HTML 书籍选区'));
            return createHighlight(
              selected.anchor,
              selected.text,
              selected.context || selected.sentence || '',
              String(payload.note || '')
            ).then(function (record) {
              if (!record) throw new Error('HTML 书籍高亮保存失败');
              return { ok: true, highlight: record };
            });
          }
          if (name === 'remove_highlight') return _bookRemoveHighlightProjection(payload.id);
          if (name === 'jump_location') {
            var target = payload.location || payload || {};
            var anchor = target.anchor || target.data || target;
            if (!HtmlAdapter.navigate(anchor)) throw new Error('HTML 书籍位置无效');
            return { ok: true };
          }
          if (name === 'jump_context') {
            var ctx = payload.context || payload || {};
            if (ctx.anchor) HtmlAdapter.jumpToAnchor(ctx.anchor.data || ctx.anchor);
            else if (ctx.start != null) HtmlAdapter.jumpToAnchor(ctx);
            else {
              var text = String(ctx.text || '');
              var raw = elContent.textContent || '';
              var start = text ? raw.indexOf(text.slice(0, 80)) : -1;
              if (start >= 0) HtmlAdapter.jumpToAnchor({ start: start, end: start + Math.min(text.length, 80) });
            }
            return { ok: true };
          }
          if (name === 'flash_selection') {
            HtmlAdapter._host.asst.flashSelOnPage(0, String(payload.text || ''));
            return { ok: true };
          }
          if (name === 'open_settings') {
            openSettings();
            return { ok: true };
          }
          throw new Error('不允许的 HTML 书籍本地命令：' + name);
        }
        var _bookActions = {};
        ['clear_selection', 'highlight', 'remove_highlight', 'jump_location', 'jump_context', 'flash_selection', 'open_settings']
          .forEach(function (name) {
            _bookActions[name] = function (payload) { return _bookAction(name, payload); };
          });
        var _bookLocalApi = BWReaderBookHost.register({
          mode: 'html',
          file: FREL,
          title: (CFG && CFG.fileName) || document.title || '',
          langs: _docLangs(),
          selection: _bookSelection,
          context: function () { return HtmlAdapter.getContext(); },
          currentLocation: function () { return HtmlAdapter.currentLocation(); },
          actions: _bookActions,
          capabilities: {
            selection: true, context: true, highlight: true,
            navigation: true, bookSettings: true
          }
        });
        if (window.RC && RC.actions) {
          RC.actions.bind('highlight.save', function (p) {
            return _bookLocalApi.localAction('highlight', p);
          }, { owner: 'pwa', runtime: 'native', storage: 'book-sidecar' });
        }
      }
    } catch (e) {
      try { console.warn('[BW] HTML 书籍宿主登记失败', e); } catch (_) {}
    }

    // ════════════ 字符偏移工具(相对 #html-content)════════════
    // 偏移 = 从容器起点到边界的文本字符数(Range.toString().length,= 包含文本节点的 textContent);
    // 还原 = TreeWalker 累计 text 节点 nodeValue 长度,二者口径一致(都基于 textContent)。
    function _charOffset(node, offset) {
      try {
        var r = document.createRange();
        r.setStart(elContent, 0);
        r.setEnd(node, offset);
        return r.toString().length;
      } catch (e) { return 0; }
    }
    function _rangeFromOffsets(start, end) {
      var walker = document.createTreeWalker(elContent, NodeFilter.SHOW_TEXT, null);
      var pos = 0, tn, sNode = null, sOff = 0, eNode = null, eOff = 0;
      while ((tn = walker.nextNode())) {
        var len = tn.nodeValue.length;
        if (sNode === null && pos + len >= start) { sNode = tn; sOff = start - pos; }
        if (pos + len >= end) { eNode = tn; eOff = end - pos; break; }
        pos += len;
      }
      if (sNode === null || eNode === null) return null;
      try { var r = document.createRange(); r.setStart(sNode, sOff); r.setEnd(eNode, eOff); return r; } catch (e) { return null; }
    }
    // 把 [start,end) 区间内的每个文本节点片段包进 <mark>(跨多节点也行;splitText 逐段切)。
    function _markRange(start, end, h) {
      if (end <= start) return;
      var walker = document.createTreeWalker(elContent, NodeFilter.SHOW_TEXT, null);
      var pos = 0, tn, segs = [];
      while ((tn = walker.nextNode())) {
        var len = tn.nodeValue.length, a = pos, b = pos + len;
        if (b > start && a < end) segs.push({ node: tn, s: Math.max(0, start - a), e: Math.min(len, end - a) });
        pos = b;
        if (pos >= end) break;
      }
      segs.forEach(function (seg) {
        var node = seg.node, s = seg.s, e = seg.e;
        if (e <= s) return;
        try {
          var mid = (s > 0) ? node.splitText(s) : node;
          if ((e - s) < mid.nodeValue.length) mid.splitText(e - s);
          var mk = document.createElement('mark');
          mk.className = 'rc-html-hl'; mk.setAttribute('data-hid', h.id);
          if (h.color) mk.style.background = h.color;
          mid.parentNode.insertBefore(mk, mid); mk.appendChild(mid);
        } catch (er) {}
      });
    }
    function _marksOf(hid) { return elContent.querySelectorAll('mark.rc-html-hl[data-hid="' + hid + '"]'); }
    function _unwrapMarks(hid) {
      var ms = _marksOf(hid);
      Array.prototype.forEach.call(ms, function (m) { var p = m.parentNode; if (!p) return; while (m.firstChild) p.insertBefore(m.firstChild, m); p.removeChild(m); try { p.normalize(); } catch (e) {} });
    }
    function _hlById(id) { for (var i = 0; i < _hls.length; i++) if (_hls[i].id === id) return _hls[i]; return null; }

    // ════════════ 选区桥接(原生主文档:mouseup/touchend → 选区/单击词分流)════════════
    function captureFromSelection() {
      try {
        var sel = window.getSelection();
        if (!sel || sel.rangeCount === 0) return null;
        var rng = sel.getRangeAt(0);
        if (!elContent.contains(rng.commonAncestorContainer)) return null;
        var txt = (sel.toString() || '').trim();
        if (!txt) return null;
        var rect = rng.getBoundingClientRect();
        var start = _charOffset(rng.startContainer, rng.startOffset);
        var end = _charOffset(rng.endContainer, rng.endOffset);
        if (end < start) { var t = start; start = end; end = t; }
        var blk = rng.startContainer.nodeType === 3 ? rng.startContainer.parentElement : rng.startContainer;
        blk = blk && blk.closest ? blk.closest('p,li,td,blockquote,div,section,h1,h2,h3,h4') : null;
        return RC.contract.selection({
          text: txt, context: (blk ? (blk.textContent || '') : '').trim().slice(0, 1200), ctx: (blk ? (blk.textContent || '') : '').trim().slice(0, 1200),
          anchor: { start: start, end: end },
          rect: { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom }
        });
      } catch (e) { return null; }
    }
    function clearNativeSel() { try { var s = window.getSelection(); if (s) s.removeAllRanges(); } catch (e) {} hideSel(); }

    function captureAndShow() {
      var c = captureFromSelection();
      if (!c) { hideSel(); return; }
      cur = c; _lastSel = c;
      // 扩展接管时保留稳定 selection/anchor 快照给 book-host，但停用重复的 PWA 工具栏。
      if (document.documentElement.dataset.bwReaderExtensionActive === '1') { hideSel(); return; }
      showSel();
    }

    var _lastDictTs = 0;
    function _dictGate() { var now = Date.now(); if (now - _lastDictTs < 500) return false; _lastDictTs = now; return true; }

    function pointFromEvent(e) {
      if (e.changedTouches && e.changedTouches[0]) { var t = e.changedTouches[0]; return { x: t.clientX, y: t.clientY, pointerType: 'touch' }; }
      if (typeof e.clientX === 'number') return { x: e.clientX, y: e.clientY, pointerType: e.pointerType || 'mouse' };
      return null;
    }
    function onPointerUp(e) { var pt = pointFromEvent(e); setTimeout(function () { handleUp(pt); }, 10); }
    function handleUp(pt) {
      var sel = window.getSelection();
      var txt = (sel && !sel.isCollapsed) ? (sel.toString() || '').trim() : '';
      if (txt) { captureAndShow(); return; }            // 拖选多词/句 → 工具栏
      hideSel();
      if (document.documentElement.dataset.bwReaderExtensionActive === '1') return;   // 点词交给扩展；PWA 不再重复开字典/发网络请求
      if (!pt) return;
      var tgt = document.elementFromPoint(pt.x, pt.y);
      if (tgt && tgt.closest && tgt.closest('mark.rc-html-hl')) return;   // 点高亮 → 由 mark click 开编辑浮层
      clickWord(pt.x, pt.y, pt.pointerType);             // 单击词 → 直弹字典
    }
    elContent.addEventListener('mouseup', onPointerUp);
    elContent.addEventListener('touchend', onPointerUp);

    // 单击词查词:caretRangeFromPoint 取词 → RC.wordpop.show(英/日);纯中文等 → 关浮层
    function caretFromPoint(x, y) {
      if (document.caretRangeFromPoint) { var r = document.caretRangeFromPoint(x, y); return r ? { node: r.startContainer, offset: r.startOffset } : null; }
      if (document.caretPositionFromPoint) { var p = document.caretPositionFromPoint(x, y); return p ? { node: p.offsetNode, offset: p.offset } : null; }
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
    function clickWord(x, y, pointerType) {
      if (!(window.RC && RC.wordpop)) return;
      var pos = caretFromPoint(x, y);
      if (!pos || !pos.node || pos.node.nodeType !== 3) { _closeWordPop(); return; }
      var w = wordAt(pos.node, pos.offset);
      if (!w || !w.text) { _closeWordPop(); return; }
      var t = w.text, isEn = /^[A-Za-z][A-Za-z'’\-]*$/.test(t), isJa = /[぀-ヿ]/.test(t);
      if (!isEn && !isJa) { _closeWordPop(); return; }   // 纯中文等 → 不查
      var rng = document.createRange();
      try { rng.setStart(w.node, w.start); rng.setEnd(w.node, w.end); } catch (e) { return; }
      if (!(RC.ui && RC.ui.rangeHitTest && RC.ui.rangeHitTest(rng, x, y, { pointerType: pointerType || 'mouse' }))) {
        _closeWordPop(); return;   // caret API 会吸到最近文字；空白落点不能消费查词门槛或打开最近词
      }
      if (!_dictGate()) return;
      var rr = rng.getBoundingClientRect();
      var rect = { left: rr.left, top: rr.top, right: rr.right, bottom: rr.bottom };
      var pblk = w.node.parentElement && w.node.parentElement.closest ? w.node.parentElement.closest('p,li,td,blockquote,h1,h2,h3,h4,div') : null;
      var pctx = (pblk ? (pblk.textContent || '') : '').trim().slice(0, 1200);
      hideSel();
      RC.wordpop.show({
        word: t, rect: rect, ctx: pctx, file: FREL, langs: [],
        onFallback: function (word) { RC.result.aiCall(EP.translate, { text: word, target_lang: '中文' }, '🌐 翻译', mkResultOpts('note', pctx)); }
      });
    }
    function _closeWordPop() { try { var wp = document.getElementById('word-pop'); if (wp && wp.style.display !== 'none') wp.style.display = 'none'; } catch (e) {} }

    // ════════════ 选区工具栏分流(照搬 epub2.js 的 selBar handler)════════════
    function showSel() {
      var word = RC.ui.isDictionaryWord(cur.text);
      selBar.querySelectorAll('[data-grp]').forEach(function (b) {
        var g = b.dataset.grp, show = (g === 'both') || (word ? g === 'word' : g === 'multi');
        b.style.display = show ? '' : 'none';
      });
      var pv = $('html-preview');
      if (pv) {
        var t = cur.text || '', disp = t.length > 120 ? (t.slice(0, 60) + '…' + t.slice(-40)) : t;
        var cnt = (/[A-Za-z]/.test(t) && /\s/.test(t)) ? (t.trim().split(/\s+/).filter(Boolean).length + ' 词') : (t.length + ' 字');
        pv.innerHTML = esc(disp) + '<span class="len">' + cnt + '</span>';
      }
      selBar.classList.add('open');
      // 2A：与 PDF / EPUB / 扩展共用紧贴选区的横向浮条；HTML 只负责提供 DOM Range rect。
      if (window.RC && RC.ui && RC.ui.placeSelectionToolbar) RC.ui.placeSelectionToolbar(selBar, cur.rect, { gap: 8 });
    }
    function hideSel() { selBar.classList.remove('open'); }

    function mkResultOpts(kind, sentence) {
      return {
        kind: kind, aiParams: aiParams,
        ankiSource: function () { return { file: FREL, sentence: sentence || '', sourceUrl: location.origin + '/pdf/html/view' + (FREL ? ('?file=' + encodeURIComponent(FREL)) : '') }; },
        // 结果模态「🖌 标记」整条回答 → 按上一次选区锚建高亮 + 备注塞 AI 正文
        markHighlight: function (text, body, sent, k) { if (_lastSel && _lastSel.anchor) createHighlight(_lastSel.anchor, _lastSel.text, _lastSel.ctx, (body || sent || '').slice(0, 400)); else toast('请先选中要标记的文字'); }
      };
    }
    function snipOpts(txt) {
      return {
        text: txt, file: FREL,
        getNoteName: function () { return prompt('新笔记名(可不带 .md):', (txt || '').slice(0, 18).replace(/\s+/g, ' ')); },
        showCard: function (head, sub) { if (window.RC && RC.sidedrawer) RC.sidedrawer.open('asst'); return addCard(head, sub); }
      };
    }
    function addCard(head, sub) {
      var body = $('asst-thread') || $('ep-ai-body'); if (!body) return null;
      // 5V：工具输出统一走语音对话已有的三态工具卡；普通 AI 文本仍保持气泡。
      var label = String(head || '工具结果').replace(/<[^>]+>/g, '');
      if (sub) label += ' · ' + String(sub).slice(0, 40) + (String(sub).length > 40 ? '…' : '');
      var out = (window.RC && RC.ui && RC.ui.appendToolCard) ? RC.ui.appendToolCard(body, { label: label, type: '#b9a8ff', form: 'full' }) : null;
      if (!out) { var card = document.createElement('div'); card.className = 'ep-card'; card.innerHTML = '<div class="h">' + head + (sub ? '<span class="ep-sel-chip">' + esc(sub.slice(0, 40)) + (sub.length > 40 ? '…' : '') + '</span>' : '') + '</div><div class="c"><span class="ep-spin"></span></div>'; body.appendChild(card); out = card.querySelector('.c'); }
      body.scrollTop = body.scrollHeight; return out;
    }
    function _execCopy(s) { try { var ta = document.createElement('textarea'); ta.value = s; ta.style.cssText = 'position:fixed;left:-9999px;top:0'; document.body.appendChild(ta); ta.select(); var ok = document.execCommand('copy'); document.body.removeChild(ta); return ok; } catch (e) { return false; } }
    function doCopy(txt) { var done = function (ok) { toast(ok ? '已复制' : '复制失败'); }; if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(txt).then(function () { done(true); }, function () { done(_execCopy(txt)); }); else done(_execCopy(txt)); }

    selBar.addEventListener('click', function (e) {
      var b = e.target.closest('button'); if (!b) return;
      var act = b.dataset.act, txt = cur.text; if (!txt) return;
      var selTxt = cur.text, selCtx = cur.ctx, selRect = cur.rect, selAnchor = cur.anchor;
      hideSel();
      if (act === 'copy') doCopy(selTxt);
      else if (act === 'dict') RC.wordpop.show({ word: selTxt, rect: selRect, ctx: selCtx, file: FREL, langs: [], onFallback: function (w) { RC.result.aiCall(EP.translate, { text: w, target_lang: '中文' }, '🌐 翻译', mkResultOpts('note', selCtx)); } });
      else if (act === 'translate') RC.result.aiCall(EP.translate, { text: selTxt, target_lang: '中文' }, '🌐 翻译', mkResultOpts('note', selCtx));
      else if (act === 'explain') RC.result.aiCall(EP.explain, { text: selTxt, context: selCtx }, '💡 AI 解释', mkResultOpts('explain', selCtx));
      else if (act === 'chat') { if (!(RC.ui && RC.ui.openSelectionChat && RC.ui.openSelectionChat(selTxt, selCtx))) RC.result.openChat(selTxt, selCtx, mkResultOpts('note', selCtx)); }
      else if (act === 'note') RC.snippets.toNote(snipOpts(selTxt));
      else if (act === 'anki') RC.snippets.toAnki(snipOpts(selTxt));
      else if (act === 'highlight') createHighlight(selAnchor, selTxt, selCtx, '');
    });

    // ════════════ 高亮 CRUD(offset sidecar)════════════
    function createHighlight(anchor, text, sentence, note) {
      if (!anchor || typeof anchor.start !== 'number') { toast('无法定位选区'); return Promise.resolve(null); }
      var color = localStorage.getItem('html-hl-color') || hlColors()[0];
      try { localStorage.setItem('html-hl-color', color); } catch (e) {}
      return RC.reqJson('POST', EP.highlights, { file: FREL, start: anchor.start, end: anchor.end, text: text || '', color: color, sentence: sentence || '', note: note || '' })
        .then(function (d) {
          if (d && d.ok && d.highlight) {
            _hls.push(d.highlight); _markRange(d.highlight.start, d.highlight.end, d.highlight); toast('已高亮'); clearNativeSel();
            return d.highlight;
          }
          toast('高亮失败:' + ((d && d.error) || '?'));
          return null;
        }).catch(function () { toast('高亮失败'); return null; });
    }
    function patchHl(h, f) {
      return RC.reqJson('PATCH', EP.highlights, Object.assign({ file: FREL, id: h.id }, f)).then(function (d) {
        if (!d || !d.ok || !d.highlight) { toast('高亮保存失败：' + ((d && d.error) || '服务未确认')); return false; }
        if ('color' in f) { h.color = d.highlight.color; Array.prototype.forEach.call(_marksOf(h.id), function (m) { m.style.background = h.color; }); }
        if ('note' in f) h.note = d.highlight.note;
        return true;
      }).catch(function (e) { toast('高亮保存失败：' + ((e && e.message) || '网络错误')); return false; });
    }
    function delHl(h) {
      return RC.reqJson('DELETE', EP.highlights + '?file=' + encodeURIComponent(FREL) + '&id=' + encodeURIComponent(h.id), null)
        .then(function (d) {
          if (!(d && d.ok)) { toast('删除失败：' + ((d && d.error) || '服务未确认')); return false; }
          _unwrapMarks(h.id); _hls = _hls.filter(function (x) { return x.id !== h.id; }); toast('已删除'); return true;
        }).catch(function (e) { toast('删除失败：' + ((e && e.message) || '网络错误')); return false; });
    }
    function _bookRemoveHighlightProjection(id) {
      id = String(id || '');
      if (!id) return { ok: false, error: '缺少高亮 id' };
      var before = _hls.length;
      _unwrapMarks(id);
      _hls = _hls.filter(function (item) { return item && String(item.id) !== id; });
      return { ok: true, id: id, removed: before !== _hls.length };
    }
    function openHlEditor(h) {
      if (!(window.RC && RC.highlight)) { toast('编辑层未就绪'); return; }
      RC.highlight.openEditor({
        colors: hlColors(), current: h.color, note: h.note || '', preview: h.text || '', sentence: h.sentence || '',
        onColor: function (c) { return patchHl(h, { color: c }); }, onNote: function (t) { return patchHl(h, { note: t }); }, onDelete: function () { return delHl(h); }
      });
    }
    // 点高亮 → 编辑浮层
    elContent.addEventListener('click', function (e) {
      var mk = e.target && e.target.closest && e.target.closest('mark.rc-html-hl'); if (!mk) return;
      var h = _hlById(mk.getAttribute('data-hid')); if (h) openHlEditor(h);
    });
    // 抽屉「高亮」pane:列表(点跳转 + 删除)
    function loadHlPane() {
      var box = $('ep-side-hl'); if (!box) return;
      if (!(window.RC && RC.highlight)) { box.innerHTML = '<div class="ep-empty">编辑层未就绪</div>'; return; }
      RC.highlight.renderList(box, _hls.slice(), {
        reverse: true, emptyHtml: '还没有高亮。<br>选中文字 → 底部「🖍 高亮」',
        onJump: function (h) { var m = _marksOf(h.id)[0]; if (m && m.scrollIntoView) m.scrollIntoView({ block: 'center' }); if (window.RC && RC.sidedrawer) RC.sidedrawer.close(); },
        onDelete: function (h) { return delHl(h); }
      });
    }
    function loadHighlights() {
      RC.reqJson('GET', EP.highlights + '?file=' + encodeURIComponent(FREL)).then(function (d) {
        _hls = (d && d.highlights) || [];
        _hls.slice().sort(function (a, b) { return a.start - b.start; }).forEach(function (h) { try { _markRange(h.start, h.end, h); } catch (e) {} });
      }).catch(function () {});
    }

    // ════════════ 设置面板(RC.settings:字号/行距/主题 → #html-content;model/effort 落 localStorage)════════════
    function _readState() {
      return {
        fs: parseInt(localStorage.getItem('html-fs-pct') || '100', 10),
        lh: parseFloat(localStorage.getItem('html-lh') || '1.75'),
        th: localStorage.getItem('html-th') || 'paper'
      };
    }
    function _applyRead() {
      var s = _readState();
      elContent.style.fontSize = (18 * s.fs / 100).toFixed(1) + 'px';
      elContent.style.lineHeight = String(s.lh);
      document.body.setAttribute('data-theme', s.th);
    }
    function openSettings() {
      if (!(window.RC && RC.settings)) { toast('设置未就绪,刷新重试'); return; }
      RC.settings.open({
        getReadState: _readState,
        onFontSize: function (d) { var v = Math.max(60, Math.min(220, _readState().fs + d)); try { localStorage.setItem('html-fs-pct', String(v)); } catch (e) {} _applyRead(); },
        onLineHeight: function (d) { var v = Math.max(1.2, Math.min(2.6, Math.round((_readState().lh + d) * 10) / 10)); try { localStorage.setItem('html-lh', String(v)); } catch (e) {} _applyRead(); },
        onTheme: function (th) { try { localStorage.setItem('html-th', th); } catch (e) {} _applyRead(); },
        getBookLangs: function () { return []; },
        onHlColors: function () {}
      });
    }
    var _setBtn = $('html-set-btn'); if (_setBtn) _setBtn.addEventListener('click', openSettings);

    // ════════════ 接共享层:result 配置 + 统一抽屉 ════════════
    if (window.RC && RC.result && RC.result.config) {
      RC.result.config({
        draftKey: 'html-drafts',
        snippetsEndpoint: EP.snippetsTo, jobStatusEndpoint: EP.jobStatus, toNoteEndpoint: EP.toNote,
        aiParams: aiParams
      });
    }
    if (window.RC && RC.sidedrawer && RC.sidedrawer.init) {
      RC.sidedrawer.init({
        handleLabel: '助手 · 高亮', defaultTab: 'asst',
        tabs: [
          { name: 'asst', label: '助手', icon: '✦ ' },
          { name: 'hl', label: '高亮', icon: '🖍 ' }
        ],
        onTab: function (name) { if (name === 'hl') loadHlPane(); }
      });
    }
    // 与 PDF / EPUB / 扩展使用同一个助手实现；先摘掉 HTML 的旧占位 pane/tab，避免两套 asst 冲突。
    try {
      if (window.RC && RC.assistant && RC.assistant.mountPdfSidebar) {
        var _op = document.getElementById('ep-side-asst');
        if (_op && _op.parentNode) _op.parentNode.removeChild(_op);
        var _ot = document.querySelector('#ep-side-tabs .ep-side-tab[data-pane="asst"]');
        if (_ot && _ot.parentNode) _ot.parentNode.removeChild(_ot);
        RC.assistant.mountPdfSidebar();
        var _nt = document.querySelector('#ep-side-tabs .side-tab[data-pane="asst"]');
        if (_nt) { _nt.classList.remove('side-tab'); _nt.classList.add('ep-side-tab'); }
        var _np = document.getElementById('side-pane-asst');
        if (_np) _np.classList.add('ep-side-pane');
      }
    } catch (e) {}
    // 助手 pane 的「🗑 清空」按钮
    var _quick = $('html-asst-quick');
    if (_quick) _quick.addEventListener('click', function (e) { var b = e.target.closest('button'); if (b && b.dataset.q === 'clear') { var body = $('ep-ai-body'); if (body) body.innerHTML = ''; } });

    _applyRead();

    // ════════════ 启动:等 MathJax 首次排版完(公式把 $..$ 文本换成容器,会改字符偏移口径)再加载高亮,
    //   保证「存高亮时」和「重渲高亮时」的 DOM 文本口径一致(都在 MathJax 排版后)。无公式的文档 ~即时。
    function whenMathReady(cb) {
      var t0 = Date.now();
      (function poll() {
        try { if (window.MathJax && MathJax.startup && MathJax.startup.promise) { MathJax.startup.promise.then(cb).catch(cb); return; } } catch (e) {}
        if (Date.now() - t0 > 4000) { cb(); return; }
        setTimeout(poll, 100);
      })();
    }
    whenMathReady(loadHighlights);
  }
})();
