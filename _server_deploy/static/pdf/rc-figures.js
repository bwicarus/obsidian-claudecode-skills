/* rc-figures.js — 统一控制层:插图徽标 + 上下文描述弹框(共享,PDF/EPUB 通用)。
 * 内容无关部分在这里:相机徽标(苹果风磨砂玻璃)+ openPop 生命周期(再点关/点外关/定位一次/RC.md 渲染/原地更新)+ 缓存编排。
 * 底座耦合走 opts(适配器):getContext(im)→{caption,context}、describe(im,ctx)→Promise<desc>、getCached/setCached。
 * 自带 CSS(rc-fig-* 独立类名,不跟 PDF 现有 .fig-badge/.fig-pop 冲突),只注入一次。徽标照搬 26-figures.js 视觉。
 */
(function () {
  if (!window.RC) window.RC = {};
  if (window.RC.figures) return;
  var injected = false;
  function injectCss() {
    if (injected) return; injected = true;
    var css = document.createElement('style'); css.id = 'rc-fig-css';
    css.textContent =
      '.rc-fig-wrap{position:relative;display:inline-block;max-width:100%}' +
      '.rc-fig-badge{position:absolute;top:7px;right:7px;width:26px;height:26px;border-radius:50%;z-index:6;cursor:pointer;' +
      'display:flex;align-items:center;justify-content:center;color:#fff;opacity:.5;background:rgba(10,132,255,.62);' +
      '-webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px);box-shadow:0 2px 7px rgba(0,0,0,.2),inset 0 0 0 .5px rgba(255,255,255,.3);' +
      '-webkit-tap-highlight-color:transparent;transition:transform .12s,opacity .12s}' +
      '.rc-fig-badge:active{transform:scale(.88)}.rc-fig-badge:hover{opacity:.95}.rc-fig-badge svg{width:15px;height:15px;display:block}' +
      '.rc-fig-pop{position:fixed;z-index:160;max-width:min(86vw,440px);background:#11192c;color:#e8eeff;border:1px solid #2a3a63;' +
      'border-radius:16px;box-shadow:0 12px 40px rgba(0,0,0,.5);padding:14px 16px;font-size:14px;line-height:1.6;max-height:60vh;overflow-y:auto;-webkit-overflow-scrolling:touch}' +
      '.rc-fig-pop h4{margin:0 0 6px;font-size:14px;color:#7fb0ff;font-weight:600;padding-right:18px}' +
      '.rc-fig-pop .rc-fig-x{position:absolute;top:8px;right:10px;color:#8aa;cursor:pointer;font-size:16px;line-height:1}' +
      '.rc-fig-pop p{margin:.35em 0}.rc-fig-pop .b p:first-child{margin-top:0}.rc-fig-pop .b p:last-child{margin-bottom:0}' +
      '.rc-spin{display:inline-block;width:13px;height:13px;border:2px solid #2b3f6e;border-top-color:#7dd3fc;border-radius:50%;animation:rcSpin .8s linear infinite;vertical-align:-2px}' +
      '@keyframes rcSpin{to{transform:rotate(360deg)}}';
    document.head.appendChild(css);
  }
  var PHOTO_SVG = '<svg viewBox="0 0 24 24" fill="none"><rect x="3" y="5" width="18" height="14" rx="3" stroke="currentColor" stroke-width="1.7"/><circle cx="8.5" cy="10" r="1.6" fill="currentColor"/><path d="M5 17l4.5-4.5a1.5 1.5 0 0 1 2 0L17 18" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  var _badge = null, _outside = null;
  function closePop() {
    var p = document.getElementById('rc-fig-pop'); if (p) p.remove();
    if (_outside) { document.removeEventListener('pointerdown', _outside, true); _outside = null; }
    _badge = null;
  }
  // chrome(定位/toggle/点外关)对齐 PDF 原生 26-figures.js::_openFigPopNative 的摆放算法与 toggle 判据。
  // 注:native 还有「continuous reposition(scroll/resize 持续跟徽标)+ 徽标滚出视口 3s 后自动关」这套,
  //   这里**没有**照搬 —— 必要的架构差异:rc-figures 的 badge.getBoundingClientRect() 拿不到跨 iframe 偏移
  //   (旧 epub.js 版阅读器 epub2-extra.js 的内容在 <iframe> 里,该文件自己在弹框首次出现时做一次性的
  //   iframe 偏移纠偏 repositionFigPop,见该文件头注释);若这里持续用未纠偏的坐标覆盖,会在每次 scroll
  //   把该阅读器的弹框重新定位到错误位置(比现状「定完不跟」更糟)。摆放算法本身(阈值/公式)逐字对齐原生。
  function openPop(badge, caption, bodyHtml, opts) {
    injectCss();   // 幂等:PDF ?ui=shared 经 PdfAdapter 直接调 openPop(不走 decorate/attach)→ 自己保证 CSS 注入,否则 .rc-fig-pop 无样式不可见
    if (_badge === badge) { closePop(); return; }   // 再点同一徽标 → 关
    closePop(); _badge = badge;
    var esc = (window.RC && RC.esc) || function (s) { return s == null ? '' : String(s); };
    var pop = document.createElement('div'); pop.id = 'rc-fig-pop'; pop.className = 'rc-fig-pop';
    pop.innerHTML = '<span class="rc-fig-x">✕</span><h4>' + esc(caption || '图') + '</h4><div class="b">' + bodyHtml + '</div>';
    document.body.appendChild(pop);
    pop.querySelector('.rc-fig-x').addEventListener('click', closePop);
    // ignoreSelector(可选,宿主给):点这些元素也不关弹层。PDF 徽标是 .fig-badge(非 .rc-fig-badge),
    //   不放行 → 再点同徽标时 pointerdown 先被外部关、pointerup 又开 = 无法 toggle;放行后由顶部 _badge===badge 正常切换。
    var igSel = (opts && opts.ignoreSelector) || '';
    _outside = function (ev) { var t = ev.target; if (t.closest && (t.closest('#rc-fig-pop') || t.closest('.rc-fig-badge') || (igSel && t.closest(igSel)))) return; closePop(); };
    document.addEventListener('pointerdown', _outside, true);
    // 上下方按阈值定位一次;左右跟徽标(夹进视口)——公式逐字对齐 native 的 reposition()
    var ph = pop.getBoundingClientRect().height;
    var br = badge.getBoundingClientRect(), pw = pop.getBoundingClientRect().width;
    var below = (br.bottom + 8 + ph) <= (window.innerHeight - 8);
    pop.style.left = Math.min(Math.max(8, br.left), window.innerWidth - pw - 8) + 'px';
    pop.style.top = (below ? br.bottom + 8 : br.top - pop.getBoundingClientRect().height - 8) + 'px';
    if (window.RC && RC.typeset) RC.typeset(pop.querySelector('.b'));
  }
  function run(im, badge, opts) {
    var ctx = (opts.getContext && opts.getContext(im)) || { caption: '图', context: '' };
    var cached = opts.getCached && opts.getCached(im);
    var md = (window.RC && RC.md) || function (s) { return s; };
    if (cached != null) { openPop(badge, ctx.caption || '图', md(cached)); return; }
    openPop(badge, ctx.caption || '图', '<span class="rc-spin"></span> AI 看图(结合上下文)…');
    opts.describe(im, ctx).then(function (desc) {
      if (opts.setCached) opts.setCached(im, desc);
      var pop = document.getElementById('rc-fig-pop'); if (_badge === badge && pop) { var b = pop.querySelector('.b'); if (b) { b.innerHTML = md(desc || '(空)'); if (RC.typeset) RC.typeset(b); } }
    }).catch(function (er) {
      var pop = document.getElementById('rc-fig-pop'); if (_badge === badge && pop) { var b = pop.querySelector('.b'); if (b) b.textContent = '✗ ' + er; }
    });
  }

  window.RC.figures = {
    PHOTO_SVG: PHOTO_SVG,
    closePop: closePop,
    // 描述浮层 chrome(再点关/点外关/定位/RC.md+typeset)。PDF 徽标几何留底座,只把弹层 chrome 统一到这里:
    //   openPop(badge, caption, bodyHtml, {ignoreSelector?})。EPUB 走 attach/decorate 内部用,不直接调它。
    openPop: openPop,
    // 给一个 <img> 挂徽标。opts:{minWidth?, getContext(im)->{caption,context}, describe(im,ctx)->Promise<desc>, getCached(im)->desc|null, setCached(im,desc)}
    attach: function (im, opts) {
      injectCss();
      if (im.dataset.rcfig) return;
      var go = function () {
        if (im.dataset.rcfig) return;
        var w = im.getBoundingClientRect().width || im.naturalWidth || 0;
        if (w < (opts.minWidth || 120)) return;   // 行内数学小符号不挂徽标
        im.dataset.rcfig = '1';
        var wrap = document.createElement('span'); wrap.className = 'rc-fig-wrap';
        if (getComputedStyle(im).display === 'block') wrap.style.display = 'block';
        im.parentNode.insertBefore(wrap, im); wrap.appendChild(im);
        var b = document.createElement('span'); b.className = 'rc-fig-badge'; b.innerHTML = PHOTO_SVG; b.title = '图说明（结合上下文）';
        b.addEventListener('click', function (ev) { ev.stopPropagation(); ev.preventDefault(); run(im, b, opts); });
        wrap.appendChild(b);
      };
      if (im.complete) go(); else im.addEventListener('load', go);
      setTimeout(go, 1600);
    },
    // 给容器内所有 <img> 挂(便捷)
    decorate: function (container, opts) { container.querySelectorAll('img').forEach(function (im) { RC.figures.attach(im, opts); }); }
  };
})();
