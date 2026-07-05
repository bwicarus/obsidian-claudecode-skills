/* epub-reader.js — EPUB 原生 reflow 阅读器(Phase 1:渲染+目录+进度+设置+续读)
 * 用 epub.js(自托管),连续滚动懒加载(大书/多卷集内存安全)。AI/选中在后续阶段接入。 */
(function () {
  'use strict';
  var CFG = window.EPUB_CFG || {};
  var $ = function (id) { return document.getElementById(id); };

  // ── 设置持久化 ──
  var LS = {
    fs: 'epub-fs', th: 'epub-theme', lh: 'epub-lh',
    pos: 'epub-pos:' + (CFG.fileRel || '')
  };
  var state = {
    fs: parseInt(localStorage.getItem(LS.fs) || '100', 10) || 100,
    th: localStorage.getItem(LS.th) || 'paper',
    lh: parseFloat(localStorage.getItem(LS.lh) || '1.6') || 1.6
  };

  var THEMES = {
    paper: { bg: '#f6f3ea', fg: '#1b1b1b', link: '#2a5db0' },
    sepia: { bg: '#e9ddc4', fg: '#5b4636', link: '#8a5a2b' },
    night: { bg: '#15181d', fg: '#c7ccd1', link: '#6fa8ff' }
  };

  if (!CFG.base) { showErr('缺少 EPUB 地址'); return; }
  if (typeof ePub === 'undefined') { showErr('epub.js 未加载'); return; }

  // 缓存绕过:之前章节 HTML 被浏览器按 max-age=1天 缓存了(那版无 img 尺寸 → 图片加载回流抽搐)。
  // 给 epub.js 取的所有 /pdf/epub/file/ 资源 URL 追加版本参数,强制取注入了 img width/height 的新 HTML。
  // 只动 epub 资源 URL,其它 fetch/XHR(助手 SSE、API)原样放行。
  (function () {
    var EV = '_ev=2';
    function bust(u) { try { if (typeof u === 'string' && u.indexOf('/pdf/epub/file/') >= 0 && u.indexOf('_ev=') < 0) return u + (u.indexOf('?') >= 0 ? '&' : '?') + EV; } catch (e) {} return u; }
    try { var _xo = XMLHttpRequest.prototype.open; XMLHttpRequest.prototype.open = function (m, u) { try { if (arguments.length >= 2) arguments[1] = bust(u); } catch (e) {} return _xo.apply(this, arguments); }; } catch (e) {}
    try { var _f = window.fetch; if (_f) window.fetch = function (i, init) { try { if (typeof i === 'string') { var b = bust(i); if (b !== i) i = b; } else if (i && i.url) { var b2 = bust(i.url); if (b2 !== i.url) i = new Request(b2, i); } } catch (e) {} return _f.call(this, i, init); }; } catch (e) {}
  })();

  var book = ePub(CFG.base);
  var rendition = book.renderTo('ep-viewer', {
    manager: 'continuous',     // 按章懒加载 + 卸载离屏 → 2900 页也不爆内存
    flow: 'scrolled',          // 连续竖向滚动(不分页)
    width: '100%',
    height: '100%',
    spread: 'none',
    allowScriptedContent: false
  });

  // 图片占位框:在 epub.js 测章高之前(content hook),用注入的 width/height 给每张图强制 aspect-ratio,
  // 把位置和大小的框先占死 → 图片加载填进去、布局零位移 → 不触发 epub.js 在图 load 时 re-measure 重渲(抽搐根治)。
  var _imgDbgN = 0;
  try {
    rendition.hooks.content.register(function (contents) {
      try {
        var doc = contents.document, imgs = doc.querySelectorAll('img'), info = [], reserved = 0;
        for (var i = 0; i < imgs.length; i++) {
          var img = imgs[i], w = parseInt(img.getAttribute('width'), 10), h = parseInt(img.getAttribute('height'), 10);
          if (w && h) {
            img.style.aspectRatio = w + ' / ' + h;   // 内联强制,胜过样式表 height:auto
            if (!img.style.height) img.style.height = 'auto';
            reserved++;
          }
          if (imgs.length && _imgDbgN < 8) info.push((w || '?') + 'x' + (h || '?') + '@' + Math.round(img.getBoundingClientRect().height));
        }
        if (imgs.length && _imgDbgN < 8) { _imgDbgN++; fetch('/pdf/api/epub-dbg', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ msg: '[img] imgs=' + imgs.length + ' reserved=' + reserved + ' [' + info.join(',') + ']' }), keepalive: true }).catch(function () {}); }
      } catch (e) {}
    });
  } catch (e) {}

  // ── 主题 / 字号 / 行距 ──
  function applyTheme() {
    var t = THEMES[state.th] || THEMES.paper;
    document.getElementById('ep-viewer').style.background = t.bg;
    rendition.themes.register('cur', {
      'html': { 'background': t.bg + ' !important' },
      'body': {
        'background': t.bg + ' !important', 'color': t.fg + ' !important',
        'line-height': state.lh + ' !important',
        'max-width': '42em', 'margin': '0 auto !important',
        'padding': '14px 6% 40px !important',
        'font-family': '-apple-system,"PingFang SC","Microsoft YaHei",serif !important',
        '-webkit-text-size-adjust': '100%'
      },
      'p': { 'line-height': state.lh + ' !important' },
      'a': { 'color': t.link + ' !important' },
      'a:link': { 'color': t.link + ' !important' },
      'img, svg': { 'max-width': '100% !important', 'height': 'auto !important' },
      '::selection': { 'background': 'rgba(120,170,255,.45)' }
    });
    rendition.themes.select('cur');
    rendition.themes.fontSize(state.fs + '%');
  }

  function refreshSettingUI() {
    $('ep-fs-v').textContent = state.fs + '%';
    $('ep-lh-v').textContent = state.lh.toFixed(1);
    [].forEach.call($('ep-theme').children, function (b) {
      b.classList.toggle('on', b.dataset.th === state.th);
    });
  }

  // ── 进度(按 spine 索引粗算;精确 % 留后续 locations) ──
  var totalSpine = 1;
  function setPct(idx) {
    var pct = totalSpine > 1 ? Math.round((idx / (totalSpine - 1)) * 100) : 0;
    pct = Math.max(0, Math.min(100, pct));
    $('ep-pct').textContent = pct + '%';
    $('ep-bar').style.width = pct + '%';
  }

  // ── TOC 抽屉 ──
  function buildToc(toc) {
    var box = $('ep-toc-list'); box.innerHTML = '';
    function add(items, sub) {
      items.forEach(function (it) {
        var a = document.createElement('a');
        a.className = 'ep-toc-i' + (sub ? ' sub' : '');
        a.textContent = it.label.trim();
        a.href = 'javascript:void 0';
        a.addEventListener('click', function () {
          closeToc();
          rendition.display(it.href);
        });
        box.appendChild(a);
        if (it.subitems && it.subitems.length) add(it.subitems, true);
      });
    }
    add(toc, false);
  }
  function openToc() { $('ep-toc').classList.add('open'); $('ep-mask').classList.add('open'); }
  function closeToc() { $('ep-toc').classList.remove('open'); $('ep-mask').classList.remove('open'); }

  // ── 设置面板 ──
  function toggleSet() { $('ep-set').classList.toggle('open'); }

  function bindUI() {
    $('ep-toc-btn').addEventListener('click', openToc);
    $('ep-mask').addEventListener('click', closeToc);
    $('ep-set-btn').addEventListener('click', toggleSet);
    document.addEventListener('click', function (e) {
      if (!e.target.closest('#ep-set') && e.target.id !== 'ep-set-btn') $('ep-set').classList.remove('open');
    });
    $('ep-fs-up').addEventListener('click', function () { state.fs = Math.min(220, state.fs + 10); localStorage.setItem(LS.fs, state.fs); applyTheme(); refreshSettingUI(); });
    $('ep-fs-dn').addEventListener('click', function () { state.fs = Math.max(70, state.fs - 10); localStorage.setItem(LS.fs, state.fs); applyTheme(); refreshSettingUI(); });
    $('ep-lh-up').addEventListener('click', function () { state.lh = Math.min(2.4, +(state.lh + 0.1).toFixed(1)); localStorage.setItem(LS.lh, state.lh); applyTheme(); refreshSettingUI(); });
    $('ep-lh-dn').addEventListener('click', function () { state.lh = Math.max(1.0, +(state.lh - 0.1).toFixed(1)); localStorage.setItem(LS.lh, state.lh); applyTheme(); refreshSettingUI(); });
    [].forEach.call($('ep-theme').children, function (b) {
      b.addEventListener('click', function () { state.th = b.dataset.th; localStorage.setItem(LS.th, state.th); applyTheme(); refreshSettingUI(); });
    });
  }

  function showErr(msg) {
    var el = $('ep-load');
    if (el) el.innerHTML = '<div style="color:#ff9a9a;font-size:14px">✗ ' + msg + '</div>' +
      '<a href="/pdf/">← 返回书架</a>';
  }
  function hideLoad() { var el = $('ep-load'); if (el) el.style.display = 'none'; }

  // ── 启动 ──(顶栏 / 设置 / 目录 / 进度 / scrubber / 全屏 全部由 epub2.js 接管统一控制层 RC;
  //   这里只负责 epub.js 渲染 + 主题 + 续读;字号/主题/行距经 window.__epub.controls 暴露给 epub2 驱动)
  applyTheme();

  var savedCfi = localStorage.getItem(LS.pos) || undefined;

  rendition.display(savedCfi).then(function () {
    hideLoad();
  }).catch(function (e) {
    // 续读位置失效 → 从头开
    rendition.display().then(hideLoad).catch(function (e2) { showErr('渲染失败：' + (e2 && e2.message || e2)); });
  });

  book.ready.then(function () {
    try { totalSpine = (book.spine && book.spine.items && book.spine.items.length) || 1; } catch (e) {}
  });

  // 目录交给 epub2.js(渲进统一抽屉的「目录」pane,经 book.loaded.navigation),这里不再自建旧抽屉 TOC,避免双份。

  rendition.on('relocate', function (loc) {
    // 进度 / 章节 scrubber 由 epub2.js 自挂的 relocate 处理;这里只存续读 cfi。
    try { if (loc && loc.start && loc.start.cfi) localStorage.setItem(LS.pos, loc.start.cfi); } catch (e) {}
  });

  // 暴露给 epub2.js(选区桥接 / AI / chrome 接管)用。
  // controls:统一控制层 RC.settings 的回调底座(字号/主题/行距落到 rendition.themes;跳章 / spine 数)。
  window.__epub = {
    book: book, rendition: rendition, cfg: CFG, state: state, applyTheme: applyTheme,
    controls: {
      getReadState: function () { return { fs: state.fs, th: state.th, lh: state.lh }; },
      onFontSize: function (d) { state.fs = Math.min(220, Math.max(70, state.fs + d)); try { localStorage.setItem(LS.fs, state.fs); } catch (e) {} applyTheme(); },
      onLineHeight: function (d) { state.lh = Math.min(2.4, Math.max(1.0, +(state.lh + d).toFixed(1))); try { localStorage.setItem(LS.lh, state.lh); } catch (e) {} applyTheme(); },
      onTheme: function (th) { state.th = th; try { localStorage.setItem(LS.th, state.th); } catch (e) {} applyTheme(); },
      gotoIndex: function (i) { try { rendition.display(i); } catch (e) {} },
      spineCount: function () { return totalSpine; }
    }
  };
})();
