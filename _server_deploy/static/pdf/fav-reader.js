/* fav-reader.js — 收藏夹查看页驱动(阶段A)。设计:references/reader-userpages-favorites.md「二、收藏夹设计」。
 *
 * 本质:**跨书页面序列的只读阅读器**。按收藏夹 items 顺序渲染各条目——
 *   PDF 条目 = 原书页图(/pdf/api/page-image,file+page,现有端点直接复用);
 *   EPUB 条目 = 原书章节消毒 HTML(/pdf/api/epub-section,file+idx;img src 服务端已重写为绝对 URL)。
 * 条目间分隔条:书名 · 第N页/章名 + 「打开原书↗」(PDF=/pdf/view?page= / EPUB=/pdf/epub/view?sec=)+ ✕ 移出。
 * 即时类功能(照 html-reader.js 的精简接法):选中 → 复制/查词/翻译/解释/对话(rc-wordpop / rc-result);
 *   选区上下文取**条目所在元素**文本,条目元素挂 data-src-file/data-page|data-section → 查词/制卡记到对的书。
 * **零状态记录**:不写 LS.pos/最近阅读/书架排序(后端路由也不 _lastopen_touch)。
 * 阶段A 不做:高亮/便签/墨迹渲染(阶段B)。纯新增,不碰 PDF/EPUB 阅读器,不改 rc-*.js。ES5。
 */
(function () {
  'use strict';
  function ready(fn) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fn);
    else fn();
  }
  ready(init);

  function init() {
    var CFG = window.FAV_CFG || {};
    var FOLDER = CFG.folderId || '';
    var ITEMS = (CFG.items || []).slice();
    var $ = function (id) { return document.getElementById(id); };
    var elContent = $('fav-content');
    var selBar = $('fav-sel');
    if (!elContent || !selBar) return;

    function esc(s) { return (window.RC && RC.esc) ? RC.esc(s) : String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
    function toast(m) { if (window.RC && RC.toast) RC.toast(m); }

    // ════════════ 端点(AI 端点内容无关,直接复用 PDF/EPUB 那套)════════════
    var EP = {
      dict: '/pdf/api/dict', dictJp: '/pdf/api/dict-jp', dictJpAi: '/pdf/api/dict-jp-ai',
      translate: '/pdf/api/translate', explain: '/pdf/api/explain',
      toNote: '/pdf/api/to-note', snippetsTo: '/pdf/api/snippets-to-async', jobStatus: '/pdf/api/job-status',
      favorites: '/pdf/api/favorites', pageImage: '/pdf/api/page-image',
      epubSection: '/pdf/api/epub-section', epubManifest: '/pdf/api/epub-manifest'
    };

    // 当前选区快照(cur=最近一次;含所在条目的 srcFile/srcLoc,查词/制卡记到对的书)
    var cur = { text: '', ctx: '', rect: null, srcFile: '', srcUrl: '' };

    // ════════════ FavAdapter(最小实现;rc-* 主要走 per-call opts,这里只补 config/endpoints/选区)════════════
    var FavAdapter = {
      kind: 'fav',
      config: { isPDF: false, reflow: true, hasFigures: false, hasFormula: true, dictMode: 'sse', popupMode: 'fixed', clickWordDetect: true, anchorKind: 'none' },
      getEndpoints: function () { return EP; },
      fileInfo: function () { return { file: cur.srcFile || '', langs: [] }; },
      captureSelection: function () { return captureFromSelection(); },
      clearSelection: function () { clearNativeSel(); },
      currentChapterText: function () {
        var e = _entryAtCenter();
        try { return e ? (e.innerText || '').slice(0, 8000) : ''; } catch (er) { return ''; }
      }
    };
    try { if (window.RC && RC.use) RC.use(FavAdapter); } catch (e) {}

    // ════════════ 条目渲染 ════════════
    // 页图请求宽:内容列实宽 × dpr(与阅读器同语义;服务端有宽度容差回退,微小差异也命中缓存)
    var IMGW = Math.max(400, Math.min(2000, Math.round(Math.min(window.innerWidth, 860) * (window.devicePixelRatio || 1))));
    var _manifests = {};   // file → Promise<manifest>(章名标注用;每本书只拉一次)

    function manifestOf(file) {
      if (!_manifests[file]) {
        _manifests[file] = fetch(EP.epubManifest + '?file=' + encodeURIComponent(file))
          .then(function (r) { return r.json(); }).catch(function () { return null; });
      }
      return _manifests[file];
    }
    function chapLabel(file, sec) {
      // 章名 = toc 里 idx ≤ sec 的最近一条 label(toc 是稀疏的,收藏的 section 未必正好是章首)
      return manifestOf(file).then(function (d) {
        if (!d || !d.ok || !d.toc || !d.toc.length) return '';
        var best = '';
        for (var i = 0; i < d.toc.length; i++) {
          var t = d.toc[i];
          if (typeof t.idx === 'number' && t.idx <= sec && t.label) best = t.label;
          if (typeof t.idx === 'number' && t.idx > sec) break;
        }
        return best;
      });
    }
    function bookName(file) { return (file || '').split('/').pop(); }
    function openUrlOf(it) {
      return it.kind === 'pdf'
        ? '/pdf/view?file=' + encodeURIComponent(it.file) + '&page=' + it.page
        : '/pdf/epub/view?file=' + encodeURIComponent(it.file) + '&sec=' + it.section;
    }
    function locLabel(it) { return it.kind === 'pdf' ? ('第 ' + it.page + ' 页') : ('第 ' + (it.section + 1) + ' 节'); }

    function renderEmpty() {
      elContent.innerHTML = '<div class="fav-empty">这个收藏夹还是空的。<br>去阅读器里点顶栏 ⭐ 把当前页/章收进来。<br><a href="/pdf/">← 返回书架</a></div>';
    }

    function renderEntry(it) {
      var wrap = document.createElement('section');
      wrap.className = 'fav-entry';
      wrap.dataset.srcFile = it.file;
      wrap.dataset.kind = it.kind;
      if (it.kind === 'pdf') wrap.dataset.page = String(it.page);
      else wrap.dataset.section = String(it.section);

      // 分隔条:书名 · 位置 + 打开原书↗ + ✕ 移出
      var div = document.createElement('div'); div.className = 'fav-div';
      div.innerHTML = '<span class="bk"></span><span class="loc"></span><span class="sp"></span>' +
        '<a class="open" title="在原书阅读器中打开这一页(新窗口,可与本页搭配用)" target="_blank" rel="noopener">打开原书 ↗</a>' +
        '<button class="rm" title="从收藏夹移出(只删收藏,不影响原书)">✕</button>';
      div.querySelector('.bk').textContent = (it.kind === 'pdf' ? '📕 ' : '📘 ') + bookName(it.file);
      div.querySelector('.loc').textContent = ' · ' + locLabel(it);
      div.querySelector('.open').href = openUrlOf(it);
      div.querySelector('.rm').addEventListener('click', function () { removeEntry(it, wrap); });
      wrap.appendChild(div);

      var body = document.createElement('div'); body.className = 'fav-item ' + it.kind;
      wrap.appendChild(body);
      elContent.appendChild(wrap);

      if (it.kind === 'pdf') {
        // PDF 条目 = 原书页图(现有 /api/page-image 端点;lazy 只加载看到的)。阶段A 无字符层(选中查词对文本条目生效)。
        var img = document.createElement('img');
        img.className = 'fav-pgimg'; img.loading = 'lazy'; img.alt = bookName(it.file) + ' ' + locLabel(it);
        img.src = EP.pageImage + '?file=' + encodeURIComponent(it.file) + '&page=' + it.page + '&w=' + IMGW;
        img.addEventListener('error', function () { body.innerHTML = '<div class="fav-loading">✗ 页图加载失败(原书可能已改名/删除)</div>'; });
        body.appendChild(img);
      } else {
        // EPUB 条目 = 原书章节消毒 HTML(img src 服务端已重写为 /pdf/epub/file/<sha>/ 绝对路径)
        body.innerHTML = '<div class="fav-loading">⏳ 加载章节…</div>';
        fetch(EP.epubSection + '?file=' + encodeURIComponent(it.file) + '&idx=' + it.section)
          .then(function (r) { return r.json(); })
          .then(function (d) {
            if (d && d.ok) body.innerHTML = d.html || '<div class="fav-loading">(空章节)</div>';
            else body.innerHTML = '<div class="fav-loading">✗ 加载失败:' + esc((d && d.error) || '?') + '</div>';
          })
          .catch(function () { body.innerHTML = '<div class="fav-loading">✗ 网络错误,章节没加载出来</div>'; });
        chapLabel(it.file, it.section).then(function (lb) {
          if (lb) div.querySelector('.loc').textContent = ' · ' + lb;
        });
      }
    }

    function removeEntry(it, wrap) {
      if (!confirm('把这条从收藏夹移出?\n只删收藏,原书和书内标注都不受影响。')) return;
      var body = { folder: FOLDER, remove_item: { file: it.file, kind: it.kind } };
      if (it.kind === 'pdf') body.remove_item.page = it.page; else body.remove_item.section = it.section;
      RC.reqJson('PATCH', EP.favorites, body).then(function (d) {
        if (d && d.ok) {
          wrap.remove();
          ITEMS = ITEMS.filter(function (x) { return x !== it; });
          var c = $('fav-count'); if (c) c.textContent = ITEMS.length + ' 条';
          if (!ITEMS.length) renderEmpty();
          toast('已移出收藏夹');
        } else toast('移出失败:' + ((d && d.error) || '?'));
      }).catch(function () { toast('网络错误,没移出去'); });
    }

    if (!ITEMS.length) renderEmpty();
    else ITEMS.forEach(renderEntry);

    // ════════════ 选区桥接(照搬 html-reader.js;上下文/来源取条目所在元素)════════════
    function _entryOf(node) {
      var el = node && node.nodeType === 3 ? node.parentElement : node;
      return el && el.closest ? el.closest('.fav-entry') : null;
    }
    function _entryAtCenter() {
      var el = document.elementFromPoint(window.innerWidth / 2, window.innerHeight / 2);
      return el && el.closest ? el.closest('.fav-entry') : null;
    }
    function _srcOf(entry) {
      if (!entry) return { file: '', url: '' };
      var it = { file: entry.dataset.srcFile || '', kind: entry.dataset.kind || 'pdf' };
      if (it.kind === 'pdf') it.page = parseInt(entry.dataset.page || '1', 10); else it.section = parseInt(entry.dataset.section || '0', 10);
      return { file: it.file, url: location.origin + openUrlOf(it) };
    }

    function captureFromSelection() {
      try {
        var sel = window.getSelection();
        if (!sel || sel.rangeCount === 0) return null;
        var rng = sel.getRangeAt(0);
        if (!elContent.contains(rng.commonAncestorContainer)) return null;
        var txt = (sel.toString() || '').trim();
        if (!txt) return null;
        var rect = rng.getBoundingClientRect();
        var blk = rng.startContainer.nodeType === 3 ? rng.startContainer.parentElement : rng.startContainer;
        blk = blk && blk.closest ? blk.closest('p,li,td,blockquote,div,section,h1,h2,h3,h4') : null;
        var entry = _entryOf(rng.startContainer);
        var src = _srcOf(entry);
        var ctx = (blk ? (blk.textContent || '') : '').trim().slice(0, 1200);
        return {
          text: txt, context: ctx, ctx: ctx,
          rect: { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom },
          srcFile: src.file, srcUrl: src.url
        };
      } catch (e) { return null; }
    }
    function clearNativeSel() { try { var s = window.getSelection(); if (s) s.removeAllRanges(); } catch (e) {} hideSel(); }
    function captureAndShow() { var c = captureFromSelection(); if (!c) { hideSel(); return; } cur = c; showSel(); }

    var _lastDictTs = 0;
    function _dictGate() { var now = Date.now(); if (now - _lastDictTs < 500) return false; _lastDictTs = now; return true; }
    function pointFromEvent(e) {
      if (e.changedTouches && e.changedTouches[0]) { var t = e.changedTouches[0]; return { x: t.clientX, y: t.clientY }; }
      if (typeof e.clientX === 'number') return { x: e.clientX, y: e.clientY };
      return null;
    }
    function onPointerUp(e) { var pt = pointFromEvent(e); setTimeout(function () { handleUp(pt); }, 10); }
    function handleUp(pt) {
      var sel = window.getSelection();
      var txt = (sel && !sel.isCollapsed) ? (sel.toString() || '').trim() : '';
      if (txt) { captureAndShow(); return; }            // 拖选多词/句 → 工具栏
      hideSel();
      if (!pt) return;
      clickWord(pt.x, pt.y);                            // 单击词 → 直弹字典(文本条目;页图条目无文字层,点了没词就关)
    }
    elContent.addEventListener('mouseup', onPointerUp);
    elContent.addEventListener('touchend', onPointerUp);

    // 单击词查词(照搬 html-reader.js:caretRangeFromPoint 取词 → RC.wordpop.show)
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
    function clickWord(x, y) {
      if (!(window.RC && RC.wordpop)) return;
      if (!_dictGate()) return;
      var pos = caretFromPoint(x, y);
      if (!pos || !pos.node || pos.node.nodeType !== 3) { _closeWordPop(); return; }
      if (!_entryOf(pos.node)) { _closeWordPop(); return; }   // 分隔条/空白处不查
      var w = wordAt(pos.node, pos.offset);
      if (!w || !w.text) { _closeWordPop(); return; }
      var t = w.text, isEn = /^[A-Za-z][A-Za-z'’\-]*$/.test(t), isJa = /[぀-ヿ]/.test(t);
      if (!isEn && !isJa) { _closeWordPop(); return; }
      var rng = document.createRange();
      try { rng.setStart(w.node, w.start); rng.setEnd(w.node, w.end); } catch (e) { return; }
      var rr = rng.getBoundingClientRect();
      var rect = { left: rr.left, top: rr.top, right: rr.right, bottom: rr.bottom };
      var pblk = w.node.parentElement && w.node.parentElement.closest ? w.node.parentElement.closest('p,li,td,blockquote,h1,h2,h3,h4,div') : null;
      var pctx = (pblk ? (pblk.textContent || '') : '').trim().slice(0, 1200);
      var src = _srcOf(_entryOf(w.node));
      hideSel();
      RC.wordpop.show({
        word: t, rect: rect, ctx: pctx, file: src.file, langs: [],
        onFallback: function (word) { RC.result.aiCall(EP.translate, { text: word, target_lang: '中文' }, '🌐 翻译', mkResultOpts('note', pctx, src)); }
      });
    }
    function _closeWordPop() { try { var wp = document.getElementById('word-pop'); if (wp && wp.style.display !== 'none') wp.style.display = 'none'; } catch (e) {} }

    // ════════════ 选区工具栏分流(照搬 html-reader.js;阶段A 只留即时类)════════════
    function isWordSel(t) { t = t || ''; return t.length <= 30 && !/\s/.test(t) && (/^[A-Za-z][A-Za-z'’\-]*$/.test(t) || /[぀-ヿ]/.test(t)); }
    function showSel() {
      var word = isWordSel(cur.text);
      selBar.querySelectorAll('[data-grp]').forEach(function (b) {
        var g = b.dataset.grp, show = (g === 'both') || (word ? g === 'word' : g === 'multi');
        b.style.display = show ? '' : 'none';
      });
      var pv = $('fav-preview');
      if (pv) {
        var t = cur.text || '', disp = t.length > 120 ? (t.slice(0, 60) + '…' + t.slice(-40)) : t;
        var cnt = (/[A-Za-z]/.test(t) && /\s/.test(t)) ? (t.trim().split(/\s+/).filter(Boolean).length + ' 词') : (t.length + ' 字');
        pv.innerHTML = esc(disp) + '<span class="len">' + cnt + '</span>';
      }
      selBar.classList.add('open');
      selBar.style.left = '50%'; selBar.style.right = 'auto'; selBar.style.top = 'auto';
      selBar.style.bottom = 'calc(env(safe-area-inset-bottom, 0px) + 20px)'; selBar.style.transform = 'translateX(-50%)';
    }
    function hideSel() { selBar.classList.remove('open'); }

    function mkResultOpts(kind, sentence, src) {
      return {
        kind: kind, aiParams: function () { return {}; },
        // 制卡出处 = 条目的**原书** file + 打开原书 URL(收藏夹页只是视图,归属永远记原书)
        ankiSource: function () { return { file: (src && src.file) || cur.srcFile || '', sentence: sentence || '', sourceUrl: (src && src.url) || cur.srcUrl || '' }; },
        markHighlight: function () { toast('收藏夹视图暂不支持高亮(阶段B);去「打开原书↗」里标'); }
      };
    }
    function _execCopy(s) { try { var ta = document.createElement('textarea'); ta.value = s; ta.style.cssText = 'position:fixed;left:-9999px;top:0'; document.body.appendChild(ta); ta.select(); var ok = document.execCommand('copy'); document.body.removeChild(ta); return ok; } catch (e) { return false; } }
    function doCopy(txt) { var done = function (ok) { toast(ok ? '已复制' : '复制失败'); }; if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(txt).then(function () { done(true); }, function () { done(_execCopy(txt)); }); else done(_execCopy(txt)); }

    selBar.addEventListener('click', function (e) {
      var b = e.target.closest('button'); if (!b) return;
      var act = b.dataset.act, txt = cur.text; if (!txt) return;
      var selTxt = cur.text, selCtx = cur.ctx, selRect = cur.rect;
      var src = { file: cur.srcFile, url: cur.srcUrl };
      hideSel();
      if (act === 'copy') doCopy(selTxt);
      else if (act === 'dict') RC.wordpop.show({ word: selTxt, rect: selRect, ctx: selCtx, file: src.file, langs: [], onFallback: function (w) { RC.result.aiCall(EP.translate, { text: w, target_lang: '中文' }, '🌐 翻译', mkResultOpts('note', selCtx, src)); } });
      else if (act === 'translate') RC.result.aiCall(EP.translate, { text: selTxt, target_lang: '中文' }, '🌐 翻译', mkResultOpts('note', selCtx, src));
      else if (act === 'explain') RC.result.aiCall(EP.explain, { text: selTxt, context: selCtx }, '💡 AI 解释', mkResultOpts('explain', selCtx, src));
      else if (act === 'chat') RC.result.openChat(selTxt, selCtx, mkResultOpts('note', selCtx, src));
    });

    // ════════════ 接共享层:result 配置(草稿/制卡端点;draftKey 独立,不碰其它阅读器的草稿)════════════
    if (window.RC && RC.result && RC.result.config) {
      RC.result.config({
        draftKey: 'fav-drafts',
        snippetsEndpoint: EP.snippetsTo, jobStatusEndpoint: EP.jobStatus, toNoteEndpoint: EP.toNote,
        aiParams: function () { return {}; }
      });
    }
  }
})();
