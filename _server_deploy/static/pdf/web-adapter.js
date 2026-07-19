/* web-adapter.js — 实况网页 = PDF 阅读器的一种**内容源**(用户拍板 2026-07-19:
 * "直接用 pdf 的页面啊,就只是把书页的展示窗口换成网页罢了")。
 *
 * 所以本文件**不建任何 UI**:顶栏 #pdf-top / 侧栏 #grammar-panel / 全部 rc-* / reader.js
 * 全部是 PDF 阅读器原样那一套(模板就是 pdf_reader.html)。这里只做三件事:
 *   ① 顶栏标题位插一个地址栏(输网址/搜索、后退、📄阅读模式)
 *   ② 与同源代理 iframe 桥接:选区 → PDF 的选区工具条;整页正文 → AI 上下文
 *   ③ 覆写 PdfAdapter 的取上下文/选区两个点(其余契约原样继承 → 侧栏/助手/查词零改动)
 * 加载在 reader.js 之后(模板脚本序尾),此时 RC/PdfAdapter 已就位。
 */
(function () {
  var CFG = window.__PDF_CFG || {};
  if (!CFG.web_url) return;                      // 普通 PDF:本文件完全不生效
  document.body.classList.add('web-mode');

  var CUR = CFG.web_url;
  var _hist = [];
  var _sel = { text: '', ctx: '', rect: null };
  var _pageText = '', _title = '';

  function frame() { return document.getElementById('wl-frame'); }
  function toast(m) { try { window.RC && RC.toast ? RC.toast(m) : 0; } catch (e) {} }

  // ── ① 顶栏地址栏(替换书名位;其余按钮=PDF 原样)──
  function mountBar() {
    // ⚠ 审计 #10 实锤:模板顶栏是 `#header > h1`(不是 #pdf-top/#pdf-title)——首版三个选择器
    //   全 0 命中、每次 if(!top) return 早退,地址栏/后退/📄 从未存在过,h1 还一直显示整条 URL。
    var top = document.getElementById('header');
    if (!top) return;
    var title = top.querySelector('h1');
    var box = document.createElement('input');
    box.id = 'wl-url'; box.value = CUR; box.spellcheck = false; box.autocomplete = 'off';
    box.title = '地址栏:输网址直接打开,输词走搜索';
    box.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter') return;
      var v = (this.value || '').trim(); if (!v) return;
      var isUrl = v.indexOf('http') === 0 || /^[\w-]+(\.[\w-]+)+([/]|$)/.test(v);
      if (isUrl) go(v); else location.href = '/pdf/web?q=' + encodeURIComponent(v);
    });
    var back = document.createElement('button');
    back.textContent = '←'; back.title = '后退';
    back.onclick = function () { var p = _hist.pop(); if (p) go(p, false); else location.href = '/pdf/web?home=1'; };
    var rd = document.createElement('button');
    rd.textContent = '📄'; rd.title = '阅读模式:抽正文进阅读器(可高亮/存 vault/进搜索与概念网)';
    rd.onclick = readerMode;
    if (title) {
      title.style.display = 'none';
      title.parentNode.insertBefore(back, title);
      title.parentNode.insertBefore(box, title);
      title.parentNode.insertBefore(rd, title.nextSibling);
    } else {
      top.insertBefore(rd, top.firstChild); top.insertBefore(box, top.firstChild); top.insertBefore(back, top.firstChild);
    }
  }

  function go(u, push) {
    if (!u) return;
    if (!/^https?:\/\//.test(u)) u = 'https://' + u;
    if (push !== false && CUR) _hist.push(CUR);
    CUR = u; CFG.web_url = u; _pageText = '';
    var b = document.getElementById('wl-url'); if (b) b.value = u;
    frame().src = '/pdf/web/proxy?url=' + encodeURIComponent(u);
    try { history.replaceState(null, '', '/pdf/web/live?url=' + encodeURIComponent(u)); } catch (e) {}
    setTimeout(askText, 1200);
  }
  window.wlGo = go;

  function readerMode() {
    toast('抽取正文中…');
    fetch('/pdf/api/web-fetch', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: CUR }) }).then(function (r) { return r.json(); }).then(function (d) {
        if (d.ok && d.file) location.href = '/pdf/html/view?file=' + encodeURIComponent(d.file);
        else toast('✗ ' + (d.error || '抽取失败'));
      }).catch(function () { toast('网络错误'); });
  }

  // ── ② 与代理 iframe 桥接 ──
  function askText() { try { frame().contentWindow.postMessage({ __rcweb: 'getText' }, '*'); } catch (e) {} }
  window.addEventListener('message', function (e) {
    var d = e.data || {};
    if (d.__rcweb === 'nav') { go(d.url); return; }
    if (d.__rcweb === 'ready') { _title = d.title || ''; askText(); return; }
    if (d.__rcweb === 'text') { _pageText = (d.text || '').slice(0, 120000); _title = d.title || _title; return; }
    if (d.__rcweb === 'sel') {
      _sel = { text: d.text || '', ctx: d.ctx || '', rect: d.rect };
      // 复用 PDF **原有的**选区工具条(#sel-toolbar):它认 lastSelText/__lastSelMeta
      try {
        window.lastSelText = _sel.text;
        window.__lastSelSentence = _sel.ctx;
        window.__lastSelMeta = { page: 1, t: Date.now() };
      } catch (_) {}
      var bar = document.getElementById('sel-toolbar');
      if (!bar) return;
      // 审计 #11:工具条由 `.open` class 控制显示(CSS 里 #sel-toolbar{display:none}),
      //   首版用 style.display='' 只是回落到 none → 网页里选中文字工具条根本不出现。
      if (!_sel.text) { bar.classList.remove('open'); return; }
      var fr = frame().getBoundingClientRect(), r = d.rect || { left: 20, bottom: 80 };
      var pv = document.getElementById('sel-preview');
      if (pv) pv.textContent = _sel.text.slice(0, 60);
      // 审计 #12:按钮组(词/多选/词组)默认 display:none,唯一开关是 _updateToolbarMode —— 不调
      //   的话即使工具条出来了也是空条。
      try { if (window._updateToolbarMode) window._updateToolbarMode(_sel.text); } catch (_) {}
      bar.style.position = 'fixed';
      bar.style.left = Math.max(8, Math.min(window.innerWidth - (bar.offsetWidth || 320) - 8, fr.left + r.left)) + 'px';
      bar.style.top = Math.min(window.innerHeight - 60, fr.top + r.bottom + 8) + 'px';
      bar.style.zIndex = 900;
      bar.classList.add('open');
    }
  });

  // ── ③ 覆写 PdfAdapter 的两个取值点(其余契约原样继承)──
  document.addEventListener('DOMContentLoaded', function () {
    mountBar();
    var fr = frame();
    if (fr) fr.addEventListener('load', askText);
    setTimeout(askText, 1500);

    var ad = null;
    try { ad = window.RC && RC.adapter && RC.adapter(); } catch (e) {}
    if (!ad) return;
    var _origCtx = ad.getContext;
    ad.getContext = function (opts) {
      var c = null;
      try { c = _origCtx ? _origCtx.call(ad, opts) : null; } catch (e) {}
      c = c || {};
      c.file = 'web:' + CUR;                 // 材料标识:高亮/对话/注意力都按它归档
      c.book = _title || CUR;
      c.book_name = _title || CUR;           // 审计 #8:后端 _sys_prompt 读的是 book_name/total
      c.total = 1;
      c.url = CUR;
      c.visible_text = _pageText.slice(0, 4000);   // 网页整页正文(iframe 回传)
      c.total_pages = 1; c.page = 1; c.pages = [1];
      if (!c.selection && _sel.text) { c.selection = _sel.text; c.selection_sentence = _sel.ctx; }
      c.langs = langs();
      return c;
    };
    ad.captureSelection = function () {
      return _sel.text ? { text: _sel.text, context: _sel.ctx, ctx: _sel.ctx, rect: _sel.rect } : null;
    };
    ad.currentChapterText = function () { return _pageText.slice(0, 8000); };
    try {   // 审计 #2:网页高亮走字符偏移 sidecar(/pdf/api/html-highlights),不是 PDF 的几何锚
      if (ad._host && ad._host.asst) ad._host.asst.hlUrl = function () { return '/pdf/api/html-highlights'; };
      ad.getEndpoints = function () { return { dict: '/pdf/api/dict', translate: '/pdf/api/translate',
                                               explain: '/pdf/api/explain', highlights: '/pdf/api/html-highlights' }; };
    } catch (e) {}
  });

  var _lang = null;
  function langs() {
    if (_lang) return _lang;
    var t = _pageText.slice(0, 4000), out = [];
    if ((t.match(/[ぁ-んァ-ヶ]/g) || []).length > 20) out.push('ja');
    var lat = (t.match(/[A-Za-z]/g) || []).length, han = (t.match(/[一-鿿]/g) || []).length;
    if (lat > Math.max(han, 1) * 2 && lat > 200) out.push('en');
    if (t.length > 200) _lang = out;
    return out;
  }
})();
