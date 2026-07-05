/* epub-ai.js — EPUB 阅读器 Phase 2:选中工具栏 + 核心 AI(查词/翻译/解释/对话) + AI 侧栏
 * 复用 PDF 阅读器现有端点(/pdf/api/dict[-jp]/translate/explain/epub-chat)。
 * epub 内容在 iframe 里,用 epub.js 的 selected 事件桥接选中文 + 坐标。 */
(function () {
  'use strict';
  function ready(fn) { if (window.__epub && window.__epub.rendition) fn(); else setTimeout(function () { ready(fn); }, 120); }
  ready(init);

  function init() {
    var R = window.__epub.rendition, B = window.__epub.book, CFG = window.__epub.cfg || {};
    var $ = function (id) { return document.getElementById(id); };
    var selBar = $('ep-sel'), dictBox = $('ep-dict'), ai = $('ep-ai'), aiBody = $('ep-ai-body');
    var cur = { text: '', ctx: '', rect: null };   // 当前选中
    var chat = [];                                  // 侧栏对话历史

    // ── 调试条(默认关;url 加 ?dbg=1 开)──诊断"选中不弹"等问题用 ──
    var DBG = location.search.indexOf('dbg=1') >= 0;
    var dbgEl = null, dbgN = 0;
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
      // 同时回传服务器(不靠截图也能远程读)
      try { fetch('/pdf/api/epub-dbg', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ msg: msg }), keepalive: true }).catch(function () {}); } catch (e) {}
    }
    dbg('init ok; hooks=' + (R && R.hooks ? 'y' : 'n') + ' getContents=' + (R && R.getContents ? 'y' : 'n'));

    // ── markdown + 数学 ──
    function renderMd(text) {
      var math = [];
      var t = String(text || '')
        .replace(/\$\$([\s\S]+?)\$\$/g, function (m) { math.push(m); return '@@M' + (math.length - 1) + '@@'; })
        .replace(/\$([^\$\n]+?)\$/g, function (m) { math.push(m); return '@@M' + (math.length - 1) + '@@'; });
      var html = window.marked ? window.marked.parse(t) : t.replace(/\n/g, '<br>');
      html = html.replace(/@@M(\d+)@@/g, function (_, i) { return math[+i]; });
      return html;
    }
    function typeset(el) { try { if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([el]); } catch (e) {} }
    function setMd(el, text) { el.innerHTML = renderMd(text); typeset(el); }

    // ── 选中桥接:直接在每个章节 iframe 里监听(不依赖 epub.js 的 selected 事件,
    //    它在连续模式 / iPad 触摸选择下常不触发)。mouseup/touchend/selectionchange 三管齐下。──
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
        var deb;
        var fire = function () { setTimeout(function () { captureSelection(win, doc); }, 10); };
        doc.addEventListener('mouseup', fire, true);
        doc.addEventListener('touchend', fire, true);
        doc.addEventListener('selectionchange', function () {
          clearTimeout(deb);
          deb = setTimeout(function () {
            var s = win.getSelection();
            if (!s || !s.toString().trim()) hideSel(); else captureSelection(win, doc);
          }, 220);
        });
        dbg('attachSel ok (listeners on iframe doc)');
      } catch (e) { dbg('attachSel ERR: ' + (e && e.message)); }
    }
    // 已渲染的章节立刻挂,后续加载的章节经 hook 挂
    try { R.hooks.content.register(attachSel); dbg('hook registered'); } catch (e) { dbg('hook reg ERR: ' + (e && e.message)); }
    try { var gc = R.getContents() || []; dbg('getContents n=' + gc.length); gc.forEach(attachSel); } catch (e) { dbg('getContents ERR: ' + (e && e.message)); }
    try { R.on('selected', function (cfiRange, contents) { dbg('epubjs selected fired'); captureSelection(contents.window, contents.document); }); } catch (e) {}

    // ── 兜底:轮询选区(iOS Safari + epub.js iframe 里 selectionchange/touchend 常不触发,事件靠不住)──
    var _pollLast = '', _emptyCnt = 0;
    setInterval(function () {
      try {
        var cs = R.getContents() || [];
        var found = null, fwin = null, fdoc = null;
        for (var i = 0; i < cs.length; i++) {
          try {
            var s = cs[i].window.getSelection();
            var t = s && s.toString().trim();
            if (t) { found = t; fwin = cs[i].window; fdoc = cs[i].document; break; }
          } catch (e) {}
        }
        if (found) {
          _emptyCnt = 0;
          if (found !== _pollLast) { _pollLast = found; captureSelection(fwin, fdoc); }
        } else {
          _pollLast = '';
          if (selBar.classList.contains('open')) { _emptyCnt++; if (_emptyCnt >= 2) hideSel(); }
        }
      } catch (e) {}
    }, 350);
    dbg('poll started');

    function showSel(rect) {
      selBar.classList.add('open');
      // 固定在屏幕底部居中:iOS 原生选择菜单浮在选区附近(屏幕中部),我们错到底部 → 永不重叠
      selBar.style.left = '50%';
      selBar.style.right = 'auto';
      selBar.style.top = 'auto';
      selBar.style.bottom = 'calc(env(safe-area-inset-bottom, 0px) + 20px)';
      selBar.style.transform = 'translateX(-50%)';
    }
    function hideSel() { selBar.classList.remove('open'); }
    function hideDict() { dictBox.classList.remove('open'); }

    document.addEventListener('click', function (e) {
      if (!e.target.closest('#ep-sel') && !e.target.closest('#ep-dict')) hideDict();
    });

    // ── 工具栏动作 ──
    selBar.addEventListener('click', function (e) {
      var b = e.target.closest('button'); if (!b) return;
      var act = b.dataset.act, txt = cur.text;
      if (!txt) return;
      hideSel();
      if (act === 'hl') {
        saveHighlight(txt, cur.cfi);
      } else if (act === 'copy') {
        (navigator.clipboard ? navigator.clipboard.writeText(txt) : Promise.reject()).catch(function () {});
        toast('已复制');
      } else if (act === 'dict') {
        doDict(txt, cur.rect);
      } else if (act === 'translate') {
        openAi('翻译'); var card = addCard('🌐 翻译', txt);
        postJson('/pdf/api/translate', { text: txt }, function (d) {
          setMd(card, d.translation || '(空)');
        }, function (err) { card.textContent = '✗ ' + err; });
      } else if (act === 'explain') {
        openAi('解释'); var card2 = addCard('💡 解释', txt);
        streamInto(card2, '/pdf/api/explain', { text: txt, context: cur.ctx });
      } else if (act === 'chat') {
        openAi('对话'); cur._pending = txt;
        $('ep-ai-ta').focus();
        showSelChip(txt);
      }
    });

    // ── 字典浮层 ──
    function doDict(word, rect) {
      word = word.trim();
      var isJa = /[぀-ヿ]/.test(word);                 // 含假名 → 日语
      var isEn = /^[A-Za-z][A-Za-z'\-]*$/.test(word);          // 纯英文词
      if (!isJa && !isEn) {                                    // 中文等 → 没有对应词典,转翻译
        openAi('翻译'); var c = addCard('🌐 翻译', word);
        postJson('/pdf/api/translate', { text: word }, function (d) { setMd(c, d.translation || ''); }, function (e) { c.textContent = '✗ ' + e; });
        return;
      }
      dictBox.innerHTML = '<span class="cls">✕</span><div class="w">' + esc(word) + '</div><div class="tr"><span class="ep-spin"></span> 查询中…</div>';
      dictBox.classList.add('open');
      posDict(rect);
      dictBox.querySelector('.cls').onclick = hideDict;
      var url = (isJa ? '/pdf/api/dict-jp' : '/pdf/api/dict') + '?word=' + encodeURIComponent(word) +
        '&file=' + encodeURIComponent(CFG.fileRel || '');
      fetch(url, { headers: { 'Accept': 'application/json' } }).then(function (r) { return r.json(); }).then(function (d) {
        if (!d || !d.ok) { dictBox.querySelector('.tr').textContent = '未找到'; return; }
        var h = '<span class="cls">✕</span><div class="w">' + esc(d.word || word) + '</div>';
        if (isJa) {
          if (d.reading) h += '<span class="ph">' + esc(d.reading) + (d.romaji ? ' · ' + esc(d.romaji) : '') + '</span>';
          if (d.zh) h += '<div class="tr">' + esc(d.zh) + '</div>';
          if (d.pos) h += '<div class="df">' + esc(d.pos) + '</div>';
        } else {
          if (d.phonetic) h += '<span class="ph">' + esc(d.phonetic) + '</span>';
          if (d.translation) h += '<div class="tr">' + esc(d.translation) + '</div>';
          if (d.definition) h += '<div class="df">' + esc(String(d.definition).slice(0, 400)) + '</div>';
        }
        dictBox.innerHTML = h;
        dictBox.querySelector('.cls').onclick = hideDict;
        posDict(rect);
      }).catch(function () { var t = dictBox.querySelector('.tr'); if (t) t.textContent = '查询失败'; });
    }
    function posDict(rect) {
      var w = dictBox.offsetWidth || 320, h = dictBox.offsetHeight || 80;
      var left = Math.min(Math.max(8, rect.left), window.innerWidth - w - 8);
      var top = rect.bottom + 8;
      if (top + h > window.innerHeight - 8) top = Math.max(54, rect.top - h - 8);
      dictBox.style.left = left + 'px'; dictBox.style.top = top + 'px';
    }

    // ── AI 侧栏 ──
    function openAi(title) { ai.classList.add('open'); $('ep-ai-head').querySelector('.t').textContent = title || 'AI 助手'; }
    function closeAi() { ai.classList.remove('open'); }
    $('ep-ai-close').addEventListener('click', closeAi);
    $('ep-ai-clear').addEventListener('click', function () { aiBody.innerHTML = ''; chat = []; });

    function addCard(head, sub) {
      var card = document.createElement('div'); card.className = 'ep-card';
      var h = '<div class="h">' + head + (sub ? '<span class="ep-sel-chip">' + esc(sub.slice(0, 40)) + (sub.length > 40 ? '…' : '') + '</span>' : '') + '</div>';
      card.innerHTML = h + '<div class="c"><span class="ep-spin"></span></div>';
      aiBody.appendChild(card); aiBody.scrollTop = aiBody.scrollHeight;
      return card.querySelector('.c');
    }
    function showSelChip(txt) {
      var card = document.createElement('div'); card.className = 'ep-card';
      card.innerHTML = '<div class="h">💬 就选中内容提问<span class="ep-sel-chip">' + esc(txt.slice(0, 48)) + (txt.length > 48 ? '…' : '') + '</span></div>';
      aiBody.appendChild(card); aiBody.scrollTop = aiBody.scrollHeight;
    }

    // ── 侧栏对话 ──
    function currentChapterText() {
      try {
        var cs = R.getContents();
        var c = cs && cs[0];
        return c ? (c.document.body.innerText || '').slice(0, 4000) : '';
      } catch (e) { return ''; }
    }
    function sendChat() {
      var ta = $('ep-ai-ta'), msg = (ta.value || '').trim();
      if (!msg) return;
      ta.value = ''; ta.style.height = 'auto';
      var um = document.createElement('div'); um.className = 'ep-msg u'; um.textContent = msg;
      aiBody.appendChild(um);
      var am = document.createElement('div'); am.className = 'ep-msg a'; am.innerHTML = '<span class="ep-spin"></span>';
      aiBody.appendChild(am); aiBody.scrollTop = aiBody.scrollHeight;
      var sel = cur._pending || ''; cur._pending = '';
      chat.push({ role: 'user', content: msg });
      var body = { message: msg, selection: sel, chapter: currentChapterText(), book: CFG.fileName || '', history: chat.slice(0, -1), rid: 'e' + Date.now() };
      var acc = '';
      sse('/pdf/api/epub-chat', body, function (t) { acc += t; setMd(am, acc); aiBody.scrollTop = aiBody.scrollHeight; },
        function () { chat.push({ role: 'assistant', content: acc }); },
        function (err) { am.textContent = '✗ ' + err; });
    }
    $('ep-ai-send').addEventListener('click', sendChat);
    $('ep-ai-ta').addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendChat(); }
    });
    $('ep-ai-ta').addEventListener('input', function () { this.style.height = 'auto'; this.style.height = Math.min(120, this.scrollHeight) + 'px'; });

    function streamInto(card, url, body) {
      body.rid = 'e' + Date.now(); var acc = '';
      sse(url, body, function (t) { acc += t; setMd(card, acc); aiBody.scrollTop = aiBody.scrollHeight; },
        function () { if (!acc) card.textContent = '(空)'; }, function (err) { card.textContent = '✗ ' + err; });
    }

    // ── 高亮(CFI 文本锚,经 epub.js annotations 渲染到 iframe 内)──
    var HL_COLORS = ['#ffd54a', '#7ee787', '#79c0ff', '#ff7b9c'];
    var _hls = {};
    function renderHl(h) {
      _hls[h.id] = h;
      try {
        R.annotations.add('highlight', h.cfi, { id: h.id }, function () { openHlPop(h); }, '',
          { 'fill': h.color, 'fill-opacity': '0.32' });
      } catch (e) { dbg('annot add err: ' + (e && e.message)); }
    }
    function unrenderHl(h) { try { R.annotations.remove(h.cfi, 'highlight'); } catch (e) {} }
    function saveHighlight(text, cfi, color) {
      if (!cfi) { toast('无法定位选区'); return; }
      reqJson('POST', '/pdf/api/epub-highlights', { file: CFG.fileRel, cfi: cfi, text: text, color: color || HL_COLORS[0] },
        function (d) { renderHl(d.highlight); toast('已高亮'); clearNativeSel(); },
        function (err) { toast('高亮失败:' + err); });
    }
    function delHl(h) {
      reqJson('DELETE', '/pdf/api/epub-highlights?file=' + encodeURIComponent(CFG.fileRel) + '&id=' + encodeURIComponent(h.id), null,
        function () { unrenderHl(h); delete _hls[h.id]; toast('已删除'); }, function () {});
    }
    function patchHl(h, fields) {
      var body = { file: CFG.fileRel, id: h.id };
      if ('color' in fields) body.color = fields.color;
      if ('note' in fields) body.note = fields.note;
      reqJson('PATCH', '/pdf/api/epub-highlights', body, function (d) {
        if (fields.color && fields.color !== h.color) { unrenderHl(h); h.color = d.highlight.color; renderHl(h); }
        h.note = d.highlight.note;
      }, function () {});
    }
    function openHlPop(h) {
      var pop = $('ep-hlpop');
      var sw = HL_COLORS.map(function (c) { return '<i data-c="' + c + '" class="' + (c === h.color ? 'on' : '') + '" style="background:' + c + '"></i>'; }).join('');
      pop.innerHTML = '<div class="sw">' + sw + '</div><textarea placeholder="备注(可选)">' + esc(h.note || '') + '</textarea>' +
        '<div class="row"><button class="del">🗑 删除</button><button class="save">保存</button></div>';
      pop.classList.add('open');
      var w = pop.offsetWidth || 230, ht = pop.offsetHeight || 150;
      pop.style.left = Math.max(8, (window.innerWidth - w) / 2) + 'px';
      pop.style.top = Math.max(56, Math.round(window.innerHeight * 0.22)) + 'px';
      pop.querySelectorAll('.sw i').forEach(function (i) {
        i.onclick = function () { pop.querySelectorAll('.sw i').forEach(function (x) { x.classList.remove('on'); }); i.classList.add('on'); patchHl(h, { color: i.dataset.c }); };
      });
      pop.querySelector('.del').onclick = function () { delHl(h); pop.classList.remove('open'); };
      pop.querySelector('.save').onclick = function () { patchHl(h, { note: pop.querySelector('textarea').value }); pop.classList.remove('open'); toast('已保存'); };
    }
    function clearNativeSel() { try { (R.getContents() || []).forEach(function (c) { var s = c.window.getSelection(); if (s) s.removeAllRanges(); }); } catch (e) {} hideSel(); }
    document.addEventListener('click', function (e) { if (!e.target.closest('#ep-hlpop')) $('ep-hlpop').classList.remove('open'); });
    // 点高亮(epub.js 内部点击 → markClicked)兜底
    try { R.on('markClicked', function (cfi) { var h = Object.values(_hls).filter(function (x) { return x.cfi === cfi; })[0]; if (h) openHlPop(h); }); } catch (e) {}
    // 加载已存高亮
    fetch('/pdf/api/epub-highlights?file=' + encodeURIComponent(CFG.fileRel)).then(function (r) { return r.json(); })
      .then(function (d) { (d.highlights || []).forEach(renderHl); dbg('loaded hl: ' + ((d.highlights || []).length)); }).catch(function () {});

    // ── ✦ 开侧栏 / 📄 总结本章 ──
    $('ep-ai-open').addEventListener('click', function () { openAi('AI 助手'); setTimeout(function () { $('ep-ai-ta').focus(); }, 100); });
    $('ep-ai-sum').addEventListener('click', function () {
      openAi('本章总结'); var card = addCard('📄 本章总结', '');
      var ch = currentChapterText();
      if (!ch) { card.textContent = '没拿到本章文本(翻一下页再试)'; return; }
      streamInto(card, '/pdf/api/epub-chat', { message: '请总结这一章的要点,分点列出,简洁。', chapter: ch, book: CFG.fileName || '' });
    });

    // ── 🖍 高亮列表(可靠的取消/跳转:不依赖 iframe 点击)──
    $('ep-hl-btn').addEventListener('click', openHlList);
    function openHlList() {
      openAi('高亮'); aiBody.innerHTML = '<div class="ep-empty"><span class="ep-spin"></span> 加载…</div>';
      fetch('/pdf/api/epub-highlights?file=' + encodeURIComponent(CFG.fileRel)).then(function (r) { return r.json(); }).then(function (d) {
        var hs = (d && d.highlights) || [];
        if (!hs.length) { aiBody.innerHTML = '<div class="ep-empty">还没有高亮。<br>选中文字 → 底部「🖍 高亮」</div>'; return; }
        aiBody.innerHTML = '';
        hs.slice().reverse().forEach(function (h) {
          _hls[h.id] = h;
          var row = document.createElement('div'); row.className = 'ep-hl-item';
          row.innerHTML = '<span class="dot" style="background:' + h.color + '"></span>' +
            '<div class="tx">' + esc((h.text || '').slice(0, 120)) + ((h.text || '').length > 120 ? '…' : '') +
            (h.note ? '<span class="nt">📝 ' + esc(h.note) + '</span>' : '') + '</div>' +
            '<div class="ops"><button class="go">跳转</button><button class="del">🗑</button></div>';
          row.querySelector('.go').onclick = function () { try { R.display(h.cfi); closeAi(); } catch (e) {} };
          row.querySelector('.del').onclick = function () { delHl(h); row.remove(); };
          aiBody.appendChild(row);
        });
      }).catch(function () { aiBody.innerHTML = '<div class="ep-empty">加载失败</div>'; });
    }

    // ── 🔍 全文搜索 ──
    var searchPanel = $('ep-search');
    $('ep-search-btn').addEventListener('click', function () { searchPanel.classList.add('open'); setTimeout(function () { $('ep-search-in').focus(); }, 100); });
    $('ep-search-x').addEventListener('click', function () { searchPanel.classList.remove('open'); });
    function doSearch() {
      var q = ($('ep-search-in').value || '').trim(), res = $('ep-search-res');
      if (!q) { res.innerHTML = ''; return; }
      res.innerHTML = '<div class="ep-empty"><span class="ep-spin"></span> 搜索中…</div>';
      fetch('/pdf/api/epub-search?file=' + encodeURIComponent(CFG.fileRel) + '&q=' + encodeURIComponent(q)).then(function (r) { return r.json(); }).then(function (d) {
        var rs = (d && d.results) || [];
        if (!rs.length) { res.innerHTML = '<div class="ep-empty">没找到「' + esc(q) + '」</div>'; return; }
        res.innerHTML = '';
        var rx = new RegExp('(' + q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi');
        rs.forEach(function (m) {
          var el = document.createElement('div'); el.className = 'ep-sr';
          el.innerHTML = '<div class="loc">' + esc(m.loc || '') + '</div><div class="ex">' + esc(m.excerpt).replace(rx, '<b>$1</b>') + '</div>';
          el.onclick = function () { try { R.display(m.href); searchPanel.classList.remove('open'); } catch (e) {} };
          res.appendChild(el);
        });
        if (d.truncated) { var t = document.createElement('div'); t.className = 'ep-empty'; t.textContent = '结果较多,只显示前 80 条'; res.appendChild(t); }
      }).catch(function () { res.innerHTML = '<div class="ep-empty">搜索失败</div>'; });
    }
    $('ep-search-go').addEventListener('click', doSearch);
    $('ep-search-in').addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); doSearch(); } });

    // ── 📄 完整功能版(转 PDF 底座 → 现有 PDF 阅读器,全套控制层)──
    var fullBtn = $('ep-full-btn');
    if (fullBtn) fullBtn.addEventListener('click', function () {
      fullBtn.disabled = true; fullBtn.textContent = '⏳ 处理中…';
      reqJson('POST', '/pdf/api/epub-to-full', { file: CFG.fileRel }, function (d) {
        if (d.ready) { fullBtn.textContent = '✓ 打开完整版'; location.href = d.view_url; return; }
        pollFull(d.job, d.view_url);
      }, function (err) { fullBtn.textContent = '✗ ' + err; fullBtn.disabled = false; });
    });
    function pollFull(job, viewUrl) {
      var iv = setInterval(function () {
        fetch('/pdf/api/ebook-convert-status?job=' + encodeURIComponent(job)).then(function (r) { return r.json(); }).then(function (st) {
          if (st.status === 'done') { clearInterval(iv); fullBtn.textContent = '✓ 打开完整版'; fullBtn.disabled = false; fullBtn.onclick = function () { location.href = st.view_url || viewUrl; }; location.href = st.view_url || viewUrl; }
          else if (st.status === 'error') { clearInterval(iv); fullBtn.textContent = '✗ 转换失败:' + (st.error || ''); fullBtn.disabled = false; }
          else { fullBtn.textContent = '⏳ 转换中(后台,可关页面)…'; }
        }).catch(function () {});
      }, 5000);
    }

    // ── 网络 ──
    function reqJson(method, url, body, ok, err) {
      var opt = { method: method, headers: {} };
      if (body) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
      fetch(url, opt).then(function (r) { return r.json(); })
        .then(function (d) { if (d && d.ok) ok(d); else err((d && d.error) || '失败'); })
        .catch(function (e) { err(e.message || '网络错误'); });
    }
    function postJson(url, body, ok, err) {
      fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
        .then(function (r) { return r.json(); })
        .then(function (d) { if (d && d.ok) ok(d); else err((d && d.error) || '失败'); })
        .catch(function (e) { err(e.message || '网络错误'); });
    }
    async function sse(url, body, onDelta, onDone, onErr) {
      try {
        var res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' }, body: JSON.stringify(body) });
        if (!res.ok || !res.body) { onErr('HTTP ' + res.status); return; }
        var reader = res.body.getReader(), dec = new TextDecoder(), buf = '';
        while (true) {
          var rd = await reader.read();
          if (rd.done) break;
          buf += dec.decode(rd.value, { stream: true });
          var idx;
          while ((idx = buf.indexOf('\n\n')) >= 0) {
            var chunk = buf.slice(0, idx); buf = buf.slice(idx + 2);
            var ev = 'message', data = '';
            chunk.split('\n').forEach(function (line) {
              if (line.indexOf('event:') === 0) ev = line.slice(6).trim();
              else if (line.indexOf('data:') === 0) data += line.slice(5).trim();
            });
            if (ev === 'error') { try { onErr(JSON.parse(data).error); } catch (e) { onErr('AI 失败'); } return; }
            if (ev === 'done') { onDone(); return; }
            if (data) { try { var d = JSON.parse(data); if (d.text) onDelta(d.text); } catch (e) {} }
          }
        }
        onDone();
      } catch (e) { onErr(e.message || '连接失败'); }
    }

    // ── 小工具 ──
    function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
    var _toastT;
    function toast(msg) {
      var el = $('ep-toast');
      if (!el) { el = document.createElement('div'); el.id = 'ep-toast'; el.style.cssText = 'position:fixed;left:50%;bottom:40px;transform:translateX(-50%);background:#10162a;border:1px solid #3b6db5;color:#cfe6ff;padding:8px 16px;border-radius:8px;font-size:13px;z-index:95;box-shadow:0 6px 16px rgba(0,0,0,.6);transition:opacity .2s'; document.body.appendChild(el); }
      el.textContent = msg; el.style.opacity = '1';
      clearTimeout(_toastT); _toastT = setTimeout(function () { el.style.opacity = '0'; }, 1400);
    }
  }
})();
