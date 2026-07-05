/* epub2-extra.js — epub.js 版 EPUB 阅读器:🔍全文搜索 + 译页(整页翻译) + 图描述徽标 + 知识点 pane
 *
 * 自包含模块,等 window.__epub.rendition + window.RC ready 后接入(epub2.js 之外的「第二批」功能)。
 * 复用共享控制层 rc-figures.js / rc-knowledge.js + 现有后端端点,对照手搓版 epub-html.js 的对应实现。
 *
 * ── epub.js 底座的关键差异:内容跑在每个章节的 <iframe> 里 ──
 *   epub-html.js 是「服务端消毒 HTML 渲进主文档」,装饰/图/翻译直接遍历 #ep-col 主文档节点。
 *   epub.js 版内容在 iframe document 里,所以本模块所有「遍历内容」的操作都改成遍历章节 iframe doc:
 *     · 译页 / 图徽标:R.getContents() / rendition.on('rendered') 拿到 contents.document,在 iframe doc 里操作;
 *     · iframe 不继承父文档 <style> → 凡是注入进 iframe 的元素(图徽标 wrap/badge、译文块 .ep-tr-rt)的 CSS
 *       必须再注一份到该 iframe 的 <head>(injectIframeCss,幂等);
 *     · 图徽标弹框(#rc-fig-pop 由 rc-figures 建在「主文档」body)定位用的是 badge.getBoundingClientRect(),
 *       而 badge 在 iframe 里 → 拿到的是 iframe 内坐标系;须叠加 iframe 在主视口的偏移才正确
 *       (rc-figures 跨不了 iframe 边界,这点由本模块在弹框出现后纠正,见 repositionFigPop)。
 *
 * ── 各功能用了什么 ──
 *   1) 🔍 搜索   = epub.js **库级 section.find**(跨章,不走后端):遍历 book.spine.spineItems,逐章
 *                  section.load → section.find(q) → section.unload,增量渲染结果;点结果 rendition.display(cfi)
 *                  + rendition.annotations 临时高亮命中 6s。面板 DOM/CSS 照搬手搓版 #ep-search/.ep-sr。
 *                  (后端也有 /pdf/api/epub-search 可用,更快;但按要求优先库级 section.find。)
 *   2) 译页      = 逐字照搬手搓版 _pagetrApplySection,遍历对象从主文档 secEl 改成 iframe doc;调
 *                  /pdf/api/epub-translate-section,在每个叶子块后插入 .ep-tr-rt 译文块。开关。
 *   3) 图徽标    = rendition.on('rendered') → RC.figures.decorate(iframeDoc.body, opts);
 *                  适配器 getContext/describe/缓存 照搬手搓版 decorateFigures(端点 /pdf/api/epub-img-describe)。
 *   4) 知识点    = RC.knowledge.init({embedded:true, fetchNodes:/pdf/api/epub-nodes}) + 监听抽屉「知识点」pane
 *                  变 active 时 RC.knowledge.load()(覆盖 epub2.js 的占位)。照搬手搓版 RC.knowledge 接法。
 */
(function () {
  'use strict';
  if (window.__epub2Extra) return;

  function ready(fn) {
    if (window.__epub && window.__epub.rendition && window.__epub.book && window.RC) fn();
    else setTimeout(function () { ready(fn); }, 120);
  }
  ready(init);

  function init() {
    if (window.__epub2Extra) return; window.__epub2Extra = true;

    var R = window.__epub.rendition, B = window.__epub.book, CFG = window.__epub.cfg || {};
    var $ = function (id) { return document.getElementById(id); };
    var FREL = CFG.fileRel || '';
    var BASE = CFG.base || '';
    var SHA = (BASE.match(/\/pdf\/epub\/file\/([a-z0-9]+)\//) || [])[1] || '';

    function toast(m) { if (window.RC && RC.toast) RC.toast(m); }
    function esc(s) { return (window.RC && RC.esc) ? RC.esc(s) : String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }

    // ── 找到某 iframe document 对应的 <iframe> 元素(verbatim 搬自 epub2.js findIframe),
    //    用于把 iframe 内坐标换算到主视口坐标(图徽标弹框定位纠偏)。
    function findIframe(doc) {
      try { if (doc.defaultView && doc.defaultView.frameElement) return doc.defaultView.frameElement; } catch (e) {}
      var ifrs = document.querySelectorAll('#ep-viewer iframe');
      for (var i = 0; i < ifrs.length; i++) { try { if (ifrs[i].contentDocument === doc) return ifrs[i]; } catch (e) {} }
      return null;
    }

    // ── 注入进「章节 iframe doc」的 CSS(iframe 不继承父文档样式)。幂等。
    //    包含:图徽标 wrap/badge(照搬 rc-figures.js 的视觉)+ 译页译文块 .ep-tr-rt(照搬手搓版)。
    //    弹框 #rc-fig-pop 由 rc-figures 建在主文档,其 CSS 在主文档,不在这里。
    var IFRAME_CSS =
      '.rc-fig-wrap{position:relative;display:inline-block;max-width:100%}' +
      '.rc-fig-badge{position:absolute;top:7px;right:7px;width:26px;height:26px;border-radius:50%;z-index:6;cursor:pointer;' +
      'display:flex;align-items:center;justify-content:center;color:#fff;opacity:.55;background:rgba(10,132,255,.62);' +
      '-webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px);box-shadow:0 2px 7px rgba(0,0,0,.2),inset 0 0 0 .5px rgba(255,255,255,.3);' +
      '-webkit-tap-highlight-color:transparent;transition:transform .12s,opacity .12s}' +
      '.rc-fig-badge:active{transform:scale(.88)}.rc-fig-badge:hover{opacity:.95}.rc-fig-badge svg{width:15px;height:15px;display:block}' +
      // 译页译文块:配色逐字对齐 PDF .page-tr-rt(译文色 #0b3d91 + 白底 rgba(255,255,255,.86) + font-weight:600 + border-radius:2px),
      // 白底确保深色 iframe 主题下也可读(等价 PDF 在原文上覆白底中文)。块级行间对照,插在原文段后。
      '.ep-tr-rt{display:block;font-size:.86em;line-height:1.55;color:#0b3d91;font-weight:600;' +
      'background:rgba(255,255,255,.86);border-radius:2px;padding:2px 6px;margin:.15em 0 .8em}';
    function injectIframeCss(doc) {
      try {
        if (!doc || doc.__epExtraCss) return; doc.__epExtraCss = true;
        var st = doc.createElement('style'); st.textContent = IFRAME_CSS;
        (doc.head || doc.documentElement).appendChild(st);
      } catch (e) {}
    }

    // ── 章节标签映射(toc href → spine idx,搜索结果显示「在第几章」;照搬 epub2.js buildTocMarks/chapLabel)
    var _tocMarks = [];
    function flatToc(items, out) { (items || []).forEach(function (it) { out.push(it); if (it.subitems && it.subitems.length) flatToc(it.subitems, out); }); return out; }
    function chapLabel(idx) { var lab = ''; for (var i = 0; i < _tocMarks.length; i++) { if (_tocMarks[i].idx <= idx) lab = _tocMarks[i].label; else break; } return lab; }
    try {
      B.loaded.navigation.then(function (nav) {
        flatToc(nav.toc || [], []).forEach(function (it) {
          try { var href = (it.href || '').split('#')[0]; var sp = B.spine.get(href); if (sp && typeof sp.index === 'number') _tocMarks.push({ idx: sp.index, label: (it.label || '').trim() }); } catch (e) {}
        });
        _tocMarks.sort(function (a, b) { return a.idx - b.idx; });
      });
    } catch (e) {}

    // ════════════════════════════════════════════════════════════════════
    // 1) 🔍 全文搜索 —— epub.js 库级 section.find(跨章),增量渲染 + 临时高亮命中
    // ════════════════════════════════════════════════════════════════════
    // 面板 CSS 照搬手搓版 epub_html_reader.html 的 #ep-search/.ep-sr 系列(epub_reader.html 模板没带这套样式 → 这里补注主文档)
    (function injectSearchCss() {
      if (document.getElementById('ep2x-search-css')) return;
      var st = document.createElement('style'); st.id = 'ep2x-search-css';
      st.textContent =
        '#ep-search{display:none;position:fixed;top:calc(54px + env(safe-area-inset-top));left:50%;transform:translateX(-50%);z-index:260;width:min(520px,94vw);background:#10162a;border:1px solid #3b6db5;border-radius:12px;box-shadow:0 10px 36px rgba(0,0,0,.65);overflow:hidden;flex-direction:column}' +
        '#ep-search.open{display:flex}' +
        '#ep-search-bar{flex:0 0 auto;display:flex;align-items:center;gap:8px;padding:10px 12px;border-bottom:1px solid #2a3550}' +
        '#ep-search-in{flex:1;background:#0d1322;border:1px solid #2a3550;color:#e6e6f0;border-radius:7px;padding:8px 11px;font-size:14px;outline:none}' +
        '#ep-search-in:focus{border-color:#3b6db5}' +
        '#ep-search-stat{color:#7a8497;font-size:11px;white-space:nowrap;min-width:48px;text-align:right}' +
        '#ep-search-x{background:transparent;border:none;color:#7a8497;font-size:18px;cursor:pointer;padding:2px 6px}' +
        '#ep-search-x:hover{color:#fff}' +
        '#ep-search-res{max-height:min(60vh,440px);overflow-y:auto;-webkit-overflow-scrolling:touch}' +
        '.ep-sr{padding:9px 13px;border-bottom:1px solid #1b2336;cursor:pointer;display:flex;gap:10px;align-items:baseline}' +
        '.ep-sr:hover{background:#162045}' +
        '.ep-sr .loc{color:#7dd3fc;font-size:12px;white-space:nowrap;flex-shrink:0;max-width:40%;overflow:hidden;text-overflow:ellipsis}' +
        '.ep-sr .ex{color:#cfe6ff;font-size:12.5px;line-height:1.5;overflow:hidden}.ep-sr .ex b{background:#3b6db5;color:#fff;border-radius:2px;padding:0 1px}' +
        '.ep-sr-empty{padding:16px;color:#5a6680;font-size:12px;text-align:center}' +
        // 结果行入场动画(照搬 PDF mfx.css #search-results>* 的 mfx-rise + nth-child 阶梯延时;EPUB 不载 mfx.css → 自带一份)
        '@keyframes ep2srRise{from{transform:translateY(10px)}to{transform:none}}' +
        '#ep-search-res .ep-sr{animation:ep2srRise .45s cubic-bezier(.22,1,.36,1) both}' +
        '#ep-search-res .ep-sr:nth-child(2){animation-delay:.05s}#ep-search-res .ep-sr:nth-child(3){animation-delay:.10s}' +
        '#ep-search-res .ep-sr:nth-child(4){animation-delay:.15s}#ep-search-res .ep-sr:nth-child(5){animation-delay:.20s}' +
        '#ep-search-res .ep-sr:nth-child(6){animation-delay:.25s}#ep-search-res .ep-sr:nth-child(7){animation-delay:.30s}' +
        '#ep-search-res .ep-sr:nth-child(8){animation-delay:.35s}#ep-search-res .ep-sr:nth-child(9){animation-delay:.40s}';
      document.head.appendChild(st);
    })();
    // 面板 DOM(模板只有 #ep-search-btn 按钮,无面板 → 动态建,照搬手搓版 markup)
    var sp = $('ep-search');
    if (!sp) {
      sp = document.createElement('div'); sp.id = 'ep-search';
      sp.innerHTML =
        '<div id="ep-search-bar">' +
        '<input id="ep-search-in" type="search" placeholder="全文搜索（全书）…" autocomplete="off" enterkeyhint="search">' +
        '<span id="ep-search-stat"></span>' +
        '<button id="ep-search-x" title="关闭">✕</button>' +
        '</div><div id="ep-search-res"></div>';
      document.body.appendChild(sp);
    }
    function openSearch() {
      sp.classList.add('open');
      setTimeout(function () {
        var i = $('ep-search-in'); if (!i) return;
        i.focus(); try { i.select(); } catch (e) {}   // 照搬 PDF openSearch:focus + select(选中已有词,便于改写)
        if ((i.value || '').trim()) doSearch();        // 有值 → 重跑(照搬 PDF「if (inp.value.trim()) _runSearch()」)
      }, 100);
    }
    function closeSearch() { sp.classList.remove('open'); }

    var _spineReady = false;
    try { B.ready.then(function () { _spineReady = true; }); } catch (e) {}

    // 临时高亮命中(epub.js annotations,SVG 叠层),6s 自动撤
    // 配色照搬 PDF .search-hl:填 rgba(250,204,21,.45)=#facc15@.45 + 描边 rgba(234,179,8,.65)=#eab308 + multiply 混合;
    // R.display(cfi) 已把命中滚到视口居中(对照 PDF first.scrollIntoView block:center)。
    function tempHighlight(cfi) {
      if (!cfi || !R.annotations) return;
      var styles = { fill: '#facc15', 'fill-opacity': '0.45', stroke: '#eab308', 'stroke-opacity': '0.65', 'stroke-width': '1', 'mix-blend-mode': 'multiply' };
      try {
        if (R.annotations.highlight) R.annotations.highlight(cfi, {}, function () {}, 'ep-search-hit', styles);
        else R.annotations.add('highlight', cfi, {}, function () {}, 'ep-search-hit', styles);
      } catch (e) { return; }
      setTimeout(function () { try { R.annotations.remove(cfi, 'highlight'); } catch (e) {} }, 6000);
    }

    var _searchToken = 0;
    function doSearch() {
      var inp = $('ep-search-in'); if (!inp) return;
      var q = (inp.value || '').trim();
      var res = $('ep-search-res'), stat = $('ep-search-stat');
      _searchToken++; var token = _searchToken;          // 任何新搜索作废上一轮(防交叉渲染)
      if (!q) { if (res) res.innerHTML = ''; if (stat) stat.textContent = ''; return; }
      var items = (_spineReady && B.spine && B.spine.spineItems) || [];
      if (!items.length) { if (res) res.innerHTML = '<div class="ep-sr-empty">书目尚未载入，稍候重试</div>'; return; }
      // 搜索中占位 + stat 文案逐字照搬 PDF 11-search.js(_runSearch:'⏳ 首次搜索本书需建索引（约几秒）…')
      if (res) res.innerHTML = '<div class="ep-sr-empty">⏳ 首次搜索本书需建索引（约几秒）…</div>';
      if (stat) stat.textContent = '搜索中…';
      var rx = new RegExp('(' + q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi');
      var hits = 0, i = 0, CAP = 200, pageSet = {}, pages = 0;   // CAP 200(对齐 PDF limit=200);pages = 有命中的章节数(对齐 stat「/ N 页」)
      // 每个有命中的章节渲**一行**(对齐 PDF 每页一行 sr-item),loc 末尾带该章命中数(对齐 PDF 'P{page}·{count}')
      function addRow(found, idx) {
        if (!res || !found.length) return;
        var empty = res.querySelector('.ep-sr-empty'); if (empty) empty.remove();
        var count = found.length, m = found[0];
        var loc = (chapLabel(idx) || ('第' + (idx + 1) + '节')) + (count > 1 ? ' ·' + count : '');
        var el = document.createElement('div'); el.className = 'ep-sr';
        el.innerHTML = '<div class="loc">' + esc(loc) + '</div>' +
          '<div class="ex">' + esc(m.excerpt || '').replace(rx, '<b>$1</b>') + '</div>';
        el.addEventListener('click', function () {
          closeSearch();
          if (m.cfi) { try { R.display(m.cfi).then(function () { tempHighlight(m.cfi); }).catch(function () {}); } catch (e) {} }
        });
        res.appendChild(el);
      }
      function finish() {
        if (token !== _searchToken) return;
        if (stat) stat.textContent = hits + ' 处 / ' + pages + ' 页';   // 逐字对齐 PDF:'{total} 处 / {pages} 页'
        if (!hits && res) res.innerHTML = '<div class="ep-sr-empty">未找到「' + esc(q) + '」</div>';   // 逐字对齐 PDF:'未找到「q」'
      }
      (function step() {
        if (token !== _searchToken) return;                       // 被新搜索取代 → 停
        if (i >= items.length || hits >= CAP) { finish(); return; }
        var item = items[i++]; var scanned = i;
        if (stat) stat.textContent = hits + ' 处 · 扫 ' + scanned + '/' + items.length;
        var next = function () { setTimeout(step, 0); };          // 让出主线程,逐章不冻 UI
        try {
          Promise.resolve(item.load(B.load.bind(B))).then(function () {
            var found = [];
            try { found = item.find(q) || []; } catch (e) { found = []; }
            try { item.unload(); } catch (e) {}
            if (token !== _searchToken) return;
            if (found.length) {
              addRow(found, item.index);
              hits += found.length;
              if (!pageSet[item.index]) { pageSet[item.index] = 1; pages++; }
            }
            next();
          }).catch(function () { try { item.unload(); } catch (e) {} next(); });
        } catch (e) { next(); }
      })();
    }
    var _searchDeb = (window.RC && RC.debounce) ? RC.debounce(doSearch, 320) : doSearch;

    var _searchBtn = $('ep-search-btn');
    if (_searchBtn) {
      _searchBtn.title = '全文搜索（全书）';   // 逐字对齐 PDF 🔍按钮 title
      _searchBtn.addEventListener('click', function () { if (sp.classList.contains('open')) closeSearch(); else openSearch(); });
    }
    var _si = $('ep-search-in'); if (_si) {
      _si.addEventListener('input', _searchDeb);
      _si.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); doSearch(); } else if (e.key === 'Escape') { closeSearch(); } });
    }
    var _sx = $('ep-search-x'); if (_sx) _sx.addEventListener('click', closeSearch);

    // ════════════════════════════════════════════════════════════════════
    // 2) 译页(整页翻译)—— 逐字照搬手搓版 _pagetrApplySection,遍历对象换成 iframe doc
    // ════════════════════════════════════════════════════════════════════
    var _pagetrOn = false;
    function pagetrDoc(doc) {
      if (!_pagetrOn || !doc || doc.__epTrBusy) return;
      injectIframeCss(doc);
      // 叶子块(p/li/blockquote,不含嵌套同类、有文字、未译过)。照搬手搓版 blocks 过滤
      var blocks = [].slice.call(doc.querySelectorAll('p,li,blockquote')).filter(function (b) {
        return !b.dataset.epTr && !b.querySelector('p,li,blockquote') && (b.textContent || '').trim();
      });
      if (!blocks.length) return;
      doc.__epTrBusy = true;
      blocks.forEach(function (b) { b.dataset.epTr = 'pending'; });
      RC.reqJson('POST', '/pdf/api/epub-translate-section', { file: FREL, texts: blocks.map(function (b) { return (b.textContent || '').trim(); }) })
        .then(function (d) {
          doc.__epTrBusy = false;
          if (!_pagetrOn) { blocks.forEach(function (b) { if (b.dataset.epTr === 'pending') b.removeAttribute('data-ep-tr'); }); return; }
          var tr = (d && d.translations) || [];
          blocks.forEach(function (b, i) {
            if (b.dataset.epTr === '1') return;
            var zh = (tr[i] || '').trim();
            if (!zh) { b.removeAttribute('data-ep-tr'); return; }
            b.dataset.epTr = '1';
            var div = doc.createElement('div'); div.className = 'ep-tr-rt'; div.textContent = zh;
            b.parentNode.insertBefore(div, b.nextSibling);
          });
        })
        .catch(function () { doc.__epTrBusy = false; blocks.forEach(function (b) { if (b.dataset.epTr === 'pending') b.removeAttribute('data-ep-tr'); }); });
    }
    function pagetrAll() { (R.getContents() || []).forEach(function (c) { if (c && c.document) pagetrDoc(c.document); }); }
    function pagetrClear() {
      (R.getContents() || []).forEach(function (c) {
        var doc = c && c.document; if (!doc) return;
        [].forEach.call(doc.querySelectorAll('.ep-tr-rt'), function (el) { el.remove(); });
        [].forEach.call(doc.querySelectorAll('[data-ep-tr]'), function (b) { b.removeAttribute('data-ep-tr'); });
        doc.__epTrBusy = false;
      });
    }
    var _ptBtn = $('ep-pagetr');
    if (_ptBtn) {
      _ptBtn.title = '整页翻译：当前页所有句子就地显示中文（再按关闭）';   // 逐字对齐 PDF #pagetr-toggle title
      _ptBtn.addEventListener('click', function () {
        _pagetrOn = !_pagetrOn; this.classList.toggle('active', _pagetrOn);
        if (_pagetrOn) { toast('整页翻译开启，翻译中…'); pagetrAll(); } else pagetrClear();   // toast 逐字对齐 PDF '整页翻译开启，翻译中…'
      });
    }

    // ════════════════════════════════════════════════════════════════════
    // 3) 图描述徽标 —— RC.figures.decorate(iframeDoc.body),适配器照搬手搓版 decorateFigures
    // ════════════════════════════════════════════════════════════════════
    function figContext(im) {
      var blk = im.closest('p,div,figure,li') || im.parentElement;
      var caption = (im.getAttribute('alt') || '').trim();
      var nb = blk && blk.nextElementSibling;
      if (nb) { var t = (nb.textContent || '').trim(); if (t && (t.length < 60 || /^(图|圖|fig|figure)/i.test(t))) caption = caption || t; }
      var parts = [];
      var pv = blk && blk.previousElementSibling; if (pv) parts.push((pv.textContent || '').trim());
      var nx = (nb && nb.nextElementSibling) || nb; if (nx) parts.push((nx.textContent || '').trim());
      return { caption: caption.slice(0, 200), context: parts.filter(Boolean).join('\n').slice(0, 1500) };
    }
    // 弹框定位纠偏:rc-figures 把 #rc-fig-pop 建在主文档,定位用 badge.getBoundingClientRect(),
    // 但 badge 在 iframe 里 → 拿到的是 iframe 内坐标;这里叠加 iframe 在主视口的偏移后重定位(mirror openPop 的摆放算法)。
    var _figClick = null;
    function attachFigClickCapture(doc) {
      if (!doc || doc.__epFigClick) return; doc.__epFigClick = true;
      doc.addEventListener('click', function (e) {
        var b = e.target && e.target.closest ? e.target.closest('.rc-fig-badge') : null;
        if (b) _figClick = { badge: b, iframe: findIframe(doc) };
      }, true);
    }
    function repositionFigPop(pop) {
      if (!_figClick || !_figClick.badge || !_figClick.iframe) return;
      var doIt = function () {
        try {
          var br0 = _figClick.badge.getBoundingClientRect();
          var ib = _figClick.iframe.getBoundingClientRect();
          if (!br0 || (!br0.width && !br0.height)) return;
          var br = { left: ib.left + br0.left, right: ib.left + br0.right, top: ib.top + br0.top, bottom: ib.top + br0.bottom };
          var rect = pop.getBoundingClientRect(), ph = rect.height, pw = rect.width;
          var below = (br.bottom + 8 + ph) <= (window.innerHeight - 8);
          // 公式同步 rc-figures.js::openPop(本session修复,原样照抄,别再让这份手工镜像跟共享层公式漂移):
          //   去掉 "-pw+26" 偏移 + 去掉 else 分支多余的 Math.max(8,...) 包装。
          pop.style.left = Math.min(Math.max(8, br.left), window.innerWidth - pw - 8) + 'px';
          pop.style.top = (below ? br.bottom + 8 : br.top - ph - 8) + 'px';
        } catch (e) {}
      };
      doIt(); if (window.requestAnimationFrame) requestAnimationFrame(doIt);
    }
    // ── 图框焦点高亮(对照 PDF 26-figures.js openPop→showHl):描述弹框出现时,在主视口画出该图的 bbox 范围。
    //    EPUB 图在 iframe 里 → 图框 = <img> 的 rect,坐标换算 = iframe.getBoundingClientRect() + img rect(同 repositionFigPop)。
    var _figFocusEl = null;
    function clearFigFocus() { if (_figFocusEl) { try { _figFocusEl.remove(); } catch (e) {} _figFocusEl = null; } }
    // 在主视口画出某 iframe 内 <img> 的范围框(坐标 = iframe.rect + img.rect;对照 PDF 26-figures showHl 的纯视觉高亮)。
    function drawFocusBox(img, iframe) {
      clearFigFocus();
      if (!img || !iframe) return;
      try {
        var ir = img.getBoundingClientRect(), ib = iframe.getBoundingClientRect();
        if (!ir.width && !ir.height) return;
        var box = document.createElement('div'); box.id = 'ep2-fig-focus';
        box.style.cssText = 'position:fixed;z-index:125;pointer-events:none;border:2.5px solid rgba(10,132,255,.92);' +
          'background:rgba(10,132,255,.10);border-radius:7px;box-shadow:0 0 0 2px rgba(10,132,255,.18);animation:ep2FigHlIn .22s ease-out';
        box.style.left = (ib.left + ir.left) + 'px'; box.style.top = (ib.top + ir.top) + 'px';
        box.style.width = ir.width + 'px'; box.style.height = ir.height + 'px';
        document.body.appendChild(box); _figFocusEl = box;
      } catch (e) {}
    }
    // 描述弹框(点徽标)出现时,在主视口画出该图范围 —— 纯视觉,不 toast(对照 PDF openPop→showHl 静默)。
    function showFigFocus() {
      if (!_figClick || !_figClick.badge || !_figClick.iframe) { clearFigFocus(); return; }
      var wrap = _figClick.badge.closest ? _figClick.badge.closest('.rc-fig-wrap') : _figClick.badge.parentNode;
      var img = wrap && wrap.querySelector ? wrap.querySelector('img') : null;
      drawFocusBox(img, _figClick.iframe);
    }
    (function injectFigFocusCss() {
      if (document.getElementById('ep2-figfocus-css')) return;
      var st = document.createElement('style'); st.id = 'ep2-figfocus-css';
      st.textContent = '@keyframes ep2FigHlIn{from{opacity:0;transform:scale(1.03)}to{opacity:1;transform:scale(1)}}';
      document.head.appendChild(st);
    })();
    // 图框焦点会随滚动漂移(reflow 滚动模式)→ 滚动/缩放即清掉,避免错位
    window.addEventListener('scroll', clearFigFocus, true);
    window.addEventListener('resize', clearFigFocus);

    // ════════════════════════════════════════════════════════════════════
    // 图进助手 —— 长按图拖进右侧助手抽屉 / 轻点图设焦点;维护 window.__figAttached(多图)
    //   对照 PDF 26-figures.js:setFigFocus / _attachFig / _renderChips / _bindFigHit 的门控拖拽。
    //   EPUB 图是 iframe 内真实 <img>(有 src URL)→ __figAttached 项 = {id,src,caption,desc,file_rel}(契约:数组 {src,...});
    //   助手侧(epub2-assist runAssistant)读 window.__figAttached 作 context.figures 随请求发(跨文件契约,本模块只负责设置它 + 拖拽 UI)。
    //   drop 目标 = 右侧统一抽屉 #ep-side(RC.sidedrawer);落下后切到「助手」pane。坐标:iframe 内 e.client* + iframe 在主视口偏移。
    // ════════════════════════════════════════════════════════════════════
    (function injectFigDragCss() {
      if (document.getElementById('ep2-figdrag-css')) return;
      var st = document.createElement('style'); st.id = 'ep2-figdrag-css';
      st.textContent =
        // 拖拽 ghost / drop 高亮 / ＋:照搬 PDF .fig-drag-ghost / #grammar-panel.fig-drop-* / #fig-drop-plus(drop 目标换 #ep-side、前缀换 ep-)
        '.ep-fig-drag-ghost{position:fixed;z-index:240;width:118px;max-height:150px;object-fit:contain;opacity:.6;' +
        'border:2px solid rgba(10,132,255,.85);border-radius:9px;box-shadow:0 10px 28px rgba(0,0,0,.55);' +
        'transform:translate(-50%,-50%);pointer-events:none;background:#fff}' +
        '#ep-side.ep-fig-drop-ready{outline:2px dashed rgba(10,132,255,.5);outline-offset:-4px}' +
        '#ep-side.ep-fig-drop-over{outline:3px solid rgba(10,132,255,.95);background:rgba(10,132,255,.07)}' +
        '#ep-fig-drop-plus{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);z-index:131;' +
        'font-size:64px;font-weight:300;color:rgba(10,132,255,.6);pointer-events:none;text-shadow:0 2px 8px rgba(0,0,0,.4)}' +
        // 助手对话上方「已带入的图」附件条(照搬 PDF #asst-fig-chips / .asst-fig-chip,前缀换 ep-)
        '#ep-asst-fig-chips{display:flex;flex-wrap:wrap;gap:6px;padding:6px 10px 0}' +
        '.ep-asst-fig-chip{display:flex;align-items:center;gap:6px;padding:4px 6px;background:#16203a;' +
        'border:1px solid #2a3a63;border-radius:9px;max-width:100%}' +
        '.ep-asst-fig-chip img{width:38px;height:38px;object-fit:cover;border-radius:5px;border:1px solid #3b6db5;background:#fff;flex:none}' +
        '.ep-asst-fig-chip .afc-cap{font-size:11px;color:#cfe6ff;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:130px}' +
        '.ep-asst-fig-chip .afc-x{background:transparent;border:none;color:#9ab;font-size:13px;cursor:pointer;flex:none;padding:0 2px}';
      document.head.appendChild(st);
    })();

    function epSideEl() { return document.getElementById('ep-side'); }
    function epAsstOpen() {   // 助手抽屉开着且「助手」pane active(对照 PDF __asstOpen:panel.open + asst pane active)
      try {
        var s = epSideEl();
        if (!(s && s.classList.contains('open'))) return false;
        var pane = s.querySelector('.ep-side-pane[data-pane="asst"]');
        return !!(pane && pane.classList.contains('active'));
      } catch (e) { return false; }
    }
    function imgSrc(im) { return (im && (im.src || im.getAttribute('src'))) || ''; }

    // ── window.__figAttached:多图带入列表(跨文件契约,助手侧读) + 附件条渲染 ──
    function attachFig(im) {
      var src = imgSrc(im); if (!src) return;
      if (!window.__figAttached) window.__figAttached = [];
      if (!window.__figAttached.some(function (a) { return a.id === src; })) {
        window.__figAttached.push({
          id: src, src: src, file_rel: FREL,
          caption: (im.getAttribute('alt') || '').trim(),
          desc: (im.dataset && im.dataset.figdesc) || ''
        });
      }
      renderChips();
    }
    function renderChips() {
      try {
        var input = document.getElementById('ep-ai-input');   // 助手输入行容器;附件条插它前面(对照 PDF 插 #asst-input 前)
        var list = window.__figAttached || [];
        var wrap = document.getElementById('ep-asst-fig-chips');
        if (!list.length) { if (wrap) wrap.remove(); return; }
        if (!input) return;       // 助手还没建/没开 → 列表仍在上下文里,开了再渲
        if (!wrap) { wrap = document.createElement('div'); wrap.id = 'ep-asst-fig-chips'; input.parentNode.insertBefore(wrap, input); }
        wrap.innerHTML = '';
        list.forEach(function (a) {
          var chip = document.createElement('div'); chip.className = 'ep-asst-fig-chip';
          var img = document.createElement('img'); img.src = a.src; img.alt = '';
          var cap = document.createElement('span'); cap.className = 'afc-cap'; cap.textContent = a.caption || '图';
          var x = document.createElement('button'); x.className = 'afc-x'; x.textContent = '✕';
          x.addEventListener('click', function () { window.__figAttached = (window.__figAttached || []).filter(function (z) { return z.id !== a.id; }); renderChips(); });
          chip.appendChild(img); chip.appendChild(cap); chip.appendChild(x); wrap.appendChild(chip);
        });
      } catch (e) {}
    }
    window.__renderFigChips = renderChips;       // 助手打开时补渲一次(图在开助手前点的情况;对照 PDF window.__renderFigChips)
    window.__clearFigAttached = function () { window.__figAttached = []; renderChips(); clearFigFocus(); };

    // 轻点图 → 焦点框 +(助手开着才)带入(对照 PDF setFigFocus:highlight 总画,attach 仅 __asstOpen 时)
    function setImgFocus(im, iframe) {
      drawFocusBox(im, iframe);              // 高亮范围(纯视觉,跟 AI 无关,保留)
      if (!epAsstOpen()) return;             // 助手没开 → 不带入对话(选中/查词不受影响)
      attachFig(im);
      toast('已带入这张图');                  // 文案对照 PDF setFigFocus(EPUB 无图组 → 固定单图)
    }

    // ── 长按拖动:ghost 跟手(主视口坐标 = iframe 偏移 + iframe 内坐标)+ #ep-side 冒「＋」→ 拖进去带入 ──
    var _figDrag = null;
    function ptMain(iframe, e) { var ib = iframe.getBoundingClientRect(); return { x: ib.left + e.clientX, y: ib.top + e.clientY }; }
    function overSide(x, y) {   // 对照 PDF _overAsst:抽屉没开(width<10)→ 非 drop 区
      var s = epSideEl(); if (!s || !s.classList.contains('open')) return false;
      var r = s.getBoundingClientRect();
      if (r.width < 10) return false;
      return x >= r.left - 24 && x <= r.right + 4 && y >= r.top && y <= r.bottom;
    }
    function dragStart(im, iframe, e) {
      dragCancel();
      var p = ptMain(iframe, e);
      var g = document.createElement('img'); g.className = 'ep-fig-drag-ghost'; g.src = imgSrc(im); g.alt = '';
      g.style.left = p.x + 'px'; g.style.top = p.y + 'px';
      document.body.appendChild(g);
      _figDrag = { im: im, iframe: iframe, ghost: g };
      var s = epSideEl();
      if (s && s.classList.contains('open')) {
        s.classList.add('ep-fig-drop-ready');
        var plus = document.createElement('div'); plus.id = 'ep-fig-drop-plus'; plus.textContent = '＋';
        s.appendChild(plus);
      }
      if (navigator.vibrate) { try { navigator.vibrate(14); } catch (_) {} }
    }
    function dragMove(e) {
      if (!_figDrag) return;
      var p = ptMain(_figDrag.iframe, e);
      _figDrag.ghost.style.left = p.x + 'px'; _figDrag.ghost.style.top = p.y + 'px';
      var s = epSideEl(); if (s) s.classList.toggle('ep-fig-drop-over', overSide(p.x, p.y));
    }
    function dragEnd(im, iframe, e) {
      var p = ptMain(iframe, e); var over = overSide(p.x, p.y);
      dragCancel();
      if (over) {
        attachFig(im);
        try { if (window.RC && RC.sidedrawer) RC.sidedrawer.open('asst'); } catch (_) {}   // 切到助手 pane
        renderChips();
        toast('📷 已带进助手对话');             // 文案逐字对齐 PDF _dragEnd
      }
    }
    function dragCancel() {
      if (_figDrag && _figDrag.ghost) { try { _figDrag.ghost.remove(); } catch (_) {} }
      _figDrag = null;
      var s = epSideEl();
      if (s) { s.classList.remove('ep-fig-drop-ready'); s.classList.remove('ep-fig-drop-over'); var pl = document.getElementById('ep-fig-drop-plus'); if (pl) pl.remove(); }
    }

    // 给图绑:轻点=设焦点;长按(380ms 不动)=拖进助手(门控逻辑逐字照搬 PDF _bindFigHit:8px 移动阈值 / 600ms 轻点窗口 / pan-y)。幂等。
    function bindFigImg(im, iframe) {
      if (!im || im.__epFigBound || !iframe) return; im.__epFigBound = true;
      try { im.style.touchAction = 'pan-y'; } catch (e) {}     // 竖滑照常滚;长按才发起拖动
      var sx = 0, sy = 0, st = 0, lp = null, moved = false, dragging = false, pid = null;
      im.addEventListener('pointerdown', function (e) {
        if (e.pointerType === 'mouse' && e.button !== 0) return;
        sx = e.clientX; sy = e.clientY; st = Date.now(); moved = false; dragging = false; pid = e.pointerId;
        lp = setTimeout(function () {
          if (!moved) { dragging = true; dragStart(im, iframe, e); try { im.setPointerCapture(pid); } catch (_) {} }
        }, 380);
      });
      im.addEventListener('pointermove', function (e) {
        if (!moved && (Math.abs(e.clientX - sx) > 8 || Math.abs(e.clientY - sy) > 8)) moved = true;
        if (dragging) { dragMove(e); e.preventDefault(); }
        else if (moved && lp) { clearTimeout(lp); lp = null; }   // 长按前就移动 = 滚动/划过 → 放弃拖动,让页面正常滚
      });
      im.addEventListener('pointerup', function (e) {
        if (lp) { clearTimeout(lp); lp = null; }
        if (dragging) { dragging = false; try { im.releasePointerCapture(pid); } catch (_) {} dragEnd(im, iframe, e); return; }
        if (!moved && Date.now() - st < 600) setImgFocus(im, iframe);   // 轻点 → 设焦点
      });
      im.addEventListener('pointercancel', function () { if (lp) { clearTimeout(lp); lp = null; } if (dragging) { dragging = false; dragCancel(); } });
    }

    // ── 本书插图描述开关(对照 PDF __figBookOn:per-book,后端 /pdf/api/book-figures,by FILE_REL;默认关)──
    window.__epubFigOn = false;
    function reRenderFigs() { try { (R.getContents() || []).forEach(function (c) { if (c && c.document) decorateFigures(c.document); }); } catch (e) {} }
    function clearFiguresDoc(doc) {
      try {
        if (!doc || !doc.body) return;
        [].forEach.call(doc.querySelectorAll('.rc-fig-wrap'), function (wrap) {
          var im = wrap.querySelector('img');
          if (im) { im.dataset.rcfig = ''; if (wrap.parentNode) wrap.parentNode.insertBefore(im, wrap); }   // 还原 img,清标记 → 再开能重挂
          if (wrap.parentNode) wrap.parentNode.removeChild(wrap);
        });
      } catch (e) {}
    }
    function clearAllFigs() { try { (R.getContents() || []).forEach(function (c) { if (c && c.document) clearFiguresDoc(c.document); }); } catch (e) {} clearFigFocus(); }
    function syncFigChk() { var c = document.getElementById('ep2-fig-chk'); if (c) c.checked = !!window.__epubFigOn; }
    function loadBookFig() {
      fetch('/pdf/api/book-figures?file=' + encodeURIComponent(FREL)).then(function (r) { return r.json(); }).then(function (d) {
        window.__epubFigOn = !!(d && d.ok && d.enabled);
        if (window.__epubFigOn) reRenderFigs();   // 进书时本书已开 → 立刻把已渲染章节的徽标挂上
        syncFigChk();
      }).catch(function () { window.__epubFigOn = false; });
    }
    function saveFigToggle(on) {
      RC.reqJson('POST', '/pdf/api/book-figures', { file: FREL, enabled: !!on }).then(function (d) {
        window.__epubFigOn = !!(d && d.ok && d.enabled);
        // toast 文案逐字对齐 PDF 01-boot.js saveFigToggle
        toast(window.__epubFigOn ? '已开启本书插图描述（翻页后逐页生成，首次需点 AI 几秒）' : '已关闭本书插图描述');
        if (window.__epubFigOn) reRenderFigs(); else clearAllFigs();
        syncFigChk();
      }).catch(function () { toast('保存失败'); });
    }
    // 设置面板没法改模板/rc-settings.js → 动态把开关注入「阅读」pane(模态首次打开时由 MutationObserver 捕获注入,模态复用故幂等)。
    function injectFigSetting(mask) {
      try {
        if (!mask || mask.querySelector('#ep2-fig-chk-row')) return;
        var pane = mask.querySelector('.set-pane[data-pane="read"]'); if (!pane) return;
        var row = document.createElement('label'); row.className = 'ep-set-chk'; row.id = 'ep2-fig-chk-row';
        row.innerHTML = '<input type="checkbox" id="ep2-fig-chk"> 📷 本书插图描述（图区放徽标，点开看 AI 说明）';   // label 逐字对齐 PDF set-figures
        pane.appendChild(row);
        var chk = row.querySelector('#ep2-fig-chk'); chk.checked = !!window.__epubFigOn;
        chk.addEventListener('change', function () { saveFigToggle(this.checked); });
      } catch (e) {}
    }
    if (window.MutationObserver) {
      new MutationObserver(function (muts) {
        for (var i = 0; i < muts.length; i++) {
          var added = muts[i].addedNodes, removed = muts[i].removedNodes;
          for (var j = 0; j < added.length; j++) {
            var n = added[j]; if (n.nodeType !== 1) continue;
            if (n.id === 'rc-fig-pop') { repositionFigPop(n); showFigFocus(); }   // 描述弹框出现 → 定位 + 画图框焦点
            else if (n.id === 'ep-settings-mask') injectFigSetting(n);             // 设置模态出现 → 注入插图描述开关
          }
          for (var k = 0; k < removed.length; k++) { var rn = removed[k]; if (rn.nodeType === 1 && rn.id === 'rc-fig-pop') clearFigFocus(); }   // 弹框关 → 清焦点框
        }
      }).observe(document.body, { childList: true });
    }
    var _existMask = document.getElementById('ep-settings-mask'); if (_existMask) injectFigSetting(_existMask);
    loadBookFig();

    function decorateFigures(doc) {
      if (!window.__epubFigOn) return;   // 本书未开插图描述 → 不挂徽标(对照 PDF __figBookOn:开了才出图徽标)
      if (!doc || !doc.body || !(window.RC && RC.figures)) return;
      injectIframeCss(doc);
      attachFigClickCapture(doc);
      // 纵横比:大图(宽>半栏)按比例缩放(照搬手搓版 decorateFigures 的 fix)
      var colW = (doc.body.clientWidth) || 600;
      var figMin = Math.max(120, colW * 0.5);
      var _ifr = findIframe(doc);
      [].forEach.call(doc.querySelectorAll('img'), function (im) {
        // 图(宽≥半栏,与徽标 minWidth 同阈值)→ 纵横比修正 + 绑「长按拖进助手 / 轻点设焦点」(幂等)
        var fix = function () { if ((im.getBoundingClientRect().width || im.naturalWidth || 0) >= figMin) { im.style.height = 'auto'; bindFigImg(im, _ifr); } };
        if (im.complete) fix(); else im.addEventListener('load', fix);
        setTimeout(fix, 1200);
      });
      RC.figures.decorate(doc.body, {
        minWidth: figMin,
        getContext: figContext,
        getCached: function (im) { return im.dataset.figdesc != null ? im.dataset.figdesc : null; },
        setCached: function (im, desc) { im.dataset.figdesc = desc || ''; },
        describe: function (im, ctx) {
          // epub.js 从目录底座(/pdf/epub/file/<sha>/)直载图,im.src 即 .../pdf/epub/file/<sha>/<路径>;按手搓版正则取 sha+path
          var src = im.src || im.getAttribute('src') || '';
          var m = src.match(/\/pdf\/epub\/file\/([a-z0-9]+)\/([^?#]+)/);
          var sha = m ? m[1] : SHA;
          if (!m) return Promise.reject('无法定位图片路径');
          return RC.reqJson('POST', '/pdf/api/epub-img-describe', { sha: sha, path: decodeURIComponent(m[2]), caption: ctx.caption, context: ctx.context })
            .then(function (d) { if (!d || !d.ok) throw ((d && d.error) || '失败'); return d.desc || ''; });
        }
      });
    }

    // ── 在「已渲染 + 后续渲染」的章节 iframe 上挂译页/图徽标 ──
    function onContents(contents) {
      try { var doc = contents && contents.document; if (!doc) return; decorateFigures(doc); if (_pagetrOn) pagetrDoc(doc); } catch (e) {}
    }
    try { R.hooks.content.register(function (contents) { onContents(contents); }); } catch (e) {}
    try { R.on('rendered', function (section, view) { try { if (view && view.contents) onContents(view.contents); } catch (e) {} }); } catch (e) {}
    try { (R.getContents() || []).forEach(onContents); } catch (e) {}
    // 兜底:开书初期反复扫已渲染章节(幂等;同 epub2.js attachScan,修首屏渲染早于本模块挂载的竞态)
    (function scan(n) { try { (R.getContents() || []).forEach(onContents); } catch (e) {} if (n < 16) setTimeout(function () { scan(n + 1); }, 320); })(0);

    // ════════════════════════════════════════════════════════════════════
    // 4) 知识点 pane —— RC.knowledge(embedded)+ 监听抽屉「知识点」pane 变 active 即 load
    // ════════════════════════════════════════════════════════════════════
    if (window.RC && RC.knowledge) {
      RC.knowledge.init({
        embedded: true,   // 不自建抽屉/把手(抽屉由 rc-sidedrawer 提供),只把节点卡渲进 #ep-kg-nodes
        fetchNodes: function () {
          return fetch('/pdf/api/epub-nodes?file=' + encodeURIComponent(FREL))
            .then(function (r) { return r.json(); }).then(function (d) { return (d && d.nodes) || []; });
        }
      });
      // epub2.js 的 sidedrawer onTab('kg') 只填占位;这里在 kg pane 变 active 时 load() 覆盖之。
      // 用 MutationObserver 监 pane 的 class → 覆盖所有进 kg 的路径(点 tab / 开抽屉默认 tab / setTab)。
      (function wireKnowledge(tries) {
        var pane = document.querySelector('#ep-side .ep-side-pane[data-pane="kg"]');
        var tab = document.querySelector('#ep-side-tabs .ep-side-tab[data-pane="kg"]');
        if (!pane && !tab) { if (tries < 40) setTimeout(function () { wireKnowledge(tries + 1); }, 250); return; }
        var load = function () { setTimeout(function () { try { RC.knowledge.load(); } catch (e) {} }, 0); };
        if (pane && window.MutationObserver) {
          new MutationObserver(function () { if (pane.classList.contains('active')) load(); })
            .observe(pane, { attributes: true, attributeFilter: ['class'] });
          if (pane.classList.contains('active')) load();
        } else if (tab) {
          tab.addEventListener('click', load);   // 无 MutationObserver 的兜底
        }
      })(0);
    }
  }
})();
