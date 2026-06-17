// ── 26-figures.js:页级图注(扫描书插图的 AI 描述)。Apple 风格图区徽标 → 点开描述浮层。──
// 后端 /pdf/api/page-figures 懒描述+预取(见 pdf_reader._fig_*)。徽标定位用 Claude 给的归一 bbox。
(function () {
  if (window.__figLoaded) return; window.__figLoaded = true;

  var _cache = {};       // page -> {figs:[...], pending:bool}
  var _poll = {};        // page -> 轮询次数

  var css = document.createElement('style');
  css.textContent =
    // 苹果风格:磨砂玻璃圆形徽标,轻投影,极简「照片」符号
    '.fig-badge{position:absolute;width:26px;height:26px;border-radius:50%;z-index:6;cursor:pointer;' +
    'display:flex;align-items:center;justify-content:center;color:#fff;opacity:.82;' +
    'background:rgba(10,132,255,.9);-webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px);' +
    'box-shadow:0 2px 7px rgba(0,0,0,.26),inset 0 0 0 .5px rgba(255,255,255,.35);' +
    '-webkit-tap-highlight-color:transparent;transition:transform .12s,opacity .12s}' +
    '.fig-badge:active{transform:scale(.88)}.fig-badge:hover{opacity:1}' +
    '.fig-badge svg{width:15px;height:15px;display:block}' +
    '.fig-pop{position:fixed;z-index:130;max-width:min(86vw,440px);background:#11192c;color:#e8eeff;' +
    'border:1px solid #2a3a63;border-radius:16px;box-shadow:0 12px 40px rgba(0,0,0,.5);' +
    'padding:14px 16px;font-size:14px;line-height:1.6;max-height:60vh;overflow-y:auto;-webkit-overflow-scrolling:touch}' +
    '.fig-pop h4{margin:0 0 6px;font-size:14px;color:#7fb0ff;font-weight:600}' +
    '.fig-pop .fig-x{position:absolute;top:8px;right:10px;color:#8aa;cursor:pointer;font-size:16px;line-height:1}' +
    '.fig-pop p{margin:.35em 0}.fig-pop code{background:#0b1220;padding:1px 4px;border-radius:4px}' +
    '.fig-pop-mask{position:fixed;inset:0;z-index:129;background:transparent}';
  document.head.appendChild(css);

  var PHOTO_SVG = '<svg viewBox="0 0 24 24" fill="none"><rect x="3" y="5" width="18" height="14" rx="3" stroke="currentColor" stroke-width="1.7"/>' +
    '<circle cx="8.5" cy="10" r="1.6" fill="currentColor"/>' +
    '<path d="M5 17l4.5-4.5a1.5 1.5 0 0 1 2 0L17 18" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  function md(t) {
    try { return (typeof window.md === 'function') ? window.md(t || '') : null; } catch (_) { return null; }
  }
  function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }

  var _popTimer = null, _popRepos = null, _popBadge = null;
  function closePop() {
    var p = document.getElementById('fig-pop'); if (p) p.remove();
    if (_popTimer) { clearTimeout(_popTimer); _popTimer = null; }
    if (_popRepos) { window.removeEventListener('scroll', _popRepos, true); window.removeEventListener('resize', _popRepos); _popRepos = null; }
    _popBadge = null;
  }
  function openPop(badge, fig) {
    if (_popBadge === badge) { closePop(); return; }   // 再点同一徽标 → 关
    closePop();
    _popBadge = badge;
    var pop = document.createElement('div'); pop.id = 'fig-pop'; pop.className = 'fig-pop';
    var body = md(fig.desc);
    pop.innerHTML = '<span class="fig-x">✕</span>' +
      (fig.caption ? '<h4>' + esc(fig.caption) + '</h4>' : '<h4>图</h4>') +
      '<div class="fig-body">' + (body != null ? body : ('<p>' + esc(fig.desc).replace(/\n/g, '<br>') + '</p>')) + '</div>';
    document.body.appendChild(pop);
    pop.querySelector('.fig-x').addEventListener('click', closePop);
    // 上下方定一次(防滚动时反复翻转抖动);左右跟徽标(夹进视口)
    var ph = pop.getBoundingClientRect().height;
    var placeBelow = (badge.getBoundingClientRect().bottom + 8 + ph) <= (window.innerHeight - 8);
    function reposition() {
      if (!badge.isConnected) { closePop(); return; }
      var br = badge.getBoundingClientRect(), pw = pop.getBoundingClientRect().width;
      pop.style.left = Math.min(Math.max(8, br.left), window.innerWidth - pw - 8) + 'px';
      pop.style.top = (placeBelow ? br.bottom + 8 : br.top - pop.getBoundingClientRect().height - 8) + 'px';
      // 徽标(图)滚出视口=显示不了 → 几秒后自动关;滚回可见 → 取消关闭。可见时一直跟着图,不关。
      if (br.bottom <= 0 || br.top >= window.innerHeight) { if (!_popTimer) _popTimer = setTimeout(closePop, 3000); }
      else if (_popTimer) { clearTimeout(_popTimer); _popTimer = null; }
    }
    reposition();
    var _raf = 0;
    _popRepos = function () { if (!_raf) _raf = requestAnimationFrame(function () { _raf = 0; reposition(); }); };
    window.addEventListener('scroll', _popRepos, true);   // capture:任何滚动容器(连续模式 #main/window)都跟
    window.addEventListener('resize', _popRepos);
    try { if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([pop]).catch(function () {}); } catch (_) {}
  }

  function draw(pw, num) {
    var rec = _cache[num]; if (!rec || !rec.figs || !rec.figs.length) return;
    var layer = (typeof ensurePageLayer === 'function') ? ensurePageLayer(pw, 'fig-layer') : null;
    if (!layer) return;
    layer.style.pointerEvents = 'none';   // 层穿透,只徽标可点
    layer.innerHTML = '';
    var canvas = pw.querySelector('canvas');
    var cssW = (canvas && canvas.clientWidth) || pw.clientWidth, cssH = (canvas && canvas.clientHeight) || pw.clientHeight;
    if (!cssW || !cssH) return;
    // 徽标贴在**图自己的某个角**(离图心远、在图上而非正文上),用 char boxes 选一个**不压字**的角。
    // 图区本身没有 OCR 文字 → 图内的角天然空;text 检查主要挡 bbox 略大溢到邻近正文的情况。
    var boxes = pw.__charBoxes || [];   // {left,top,width,height} CSS px(08-charlayer 设)
    var S = 26, M = 3;
    function hitsText(x, y) {
      for (var i = 0; i < boxes.length; i++) {
        var bx = boxes[i]; if (bx.sp) continue;
        if (bx.left < x + S && bx.left + bx.width > x && bx.top < y + S && bx.top + bx.height > y) return true;
      }
      return false;
    }
    function clampX(x) { return Math.max(1, Math.min(cssW - S - 1, x)); }
    function clampY(y) { return Math.max(1, Math.min(cssH - S - 1, y)); }
    rec.figs.forEach(function (f) {
      var pos = null;
      // 服务端预算好的锚点(归一,徽标中心)= 贴着图的空白角,跨加载位置一致 → 直接用
      if (f.badge && f.badge.length === 2) {
        pos = [clampX(f.badge[0] * cssW - S / 2), clampY(f.badge[1] * cssH - S / 2)];
      } else {
        // 回退:旧启发(图框四角避开正文)。badge 缺(后端还没算好)时临时用。优先用收紧后的真实图框 fbox
        var rawb = (f.fbox && f.fbox.length === 4) ? f.fbox : f.bbox;
        var bb = (rawb && rawb.length === 4 && rawb[2] > rawb[0] && rawb[3] > rawb[1]) ? rawb : [0.02, 0.03, 0.1, 0.1];
        var fx0 = bb[0] * cssW, fy0 = bb[1] * cssH, fx1 = bb[2] * cssW, fy1 = bb[3] * cssH;
        var cands = [[fx1 - S - M, fy0 + M], [fx0 + M, fy0 + M], [fx1 - S - M, fy1 - S - M], [fx0 + M, fy1 - S - M]];
        for (var i = 0; i < cands.length; i++) {
          var x = clampX(cands[i][0]), y = clampY(cands[i][1]);
          if (!hitsText(x, y)) { pos = [x, y]; break; }
        }
        if (!pos) { pos = [clampX((fx0 + fx1) / 2 - S / 2), clampY((fy0 + fy1) / 2 - S / 2)]; }
      }
      var b = document.createElement('div'); b.className = 'fig-badge'; b.innerHTML = PHOTO_SVG;
      b.style.left = pos[0] + 'px'; b.style.top = pos[1] + 'px'; b.style.pointerEvents = 'auto';
      b.title = f.caption || '图说明';
      b.addEventListener('click', function (e) { e.stopPropagation(); openPop(b, f); });
      layer.appendChild(b);
    });
  }

  function _fetchFigs(pw, num) {
    fetch('/pdf/api/page-figures?file=' + encodeURIComponent(FILE_REL) + '&page=' + num)
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) return;
        _cache[num] = { figs: d.figures || [], pending: !!d.pending };
        draw(pw, num);
        if (d.pending) schedulePoll(pw, num);   // 还没描述完(或上次失败,后端会再触发)→ 轮询拿
      }).catch(function () {});
  }

  window.renderFiguresOnPage = function (pw, num) {
    if (!pw || !num || typeof FILE_REL === 'undefined' || !FILE_REL) return;
    if (!window.__figBookOn) return;   // 本书未开插图描述 → 不拉 page-figures、不画徽标、不烧 AI
    var rec = _cache[num];
    if (rec && !rec.pending) { draw(pw, num); return; }   // 已确定(有图/NONE)→ 直接画,不再打扰后端
    // 没拉过 或 还 pending(含上次描述失败的页)→ 重新拉,重置轮询给新机会(回看/重渲会重试)
    _poll[num] = 0;
    _fetchFigs(pw, num);
  };

  function schedulePoll(pw, num) {       // 后台描述 ~8-15s,轮询几次拿结果(只在该页仍在 DOM 时)
    if ((_poll[num] || 0) >= 8) return;
    _poll[num] = (_poll[num] || 0) + 1;
    setTimeout(function () { if (pw.isConnected) _fetchFigs(pw, num); }, 4500);
  }
})();
