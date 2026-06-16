// ── 26-figures.js:页级图注(扫描书插图的 AI 描述)。Apple 风格图区徽标 → 点开描述浮层。──
// 后端 /pdf/api/page-figures 懒描述+预取(见 pdf_reader._fig_*)。徽标定位用 Claude 给的归一 bbox。
(function () {
  if (window.__figLoaded) return; window.__figLoaded = true;

  var _cache = {};       // page -> {figs:[...], pending:bool}
  var _poll = {};        // page -> 轮询次数

  var css = document.createElement('style');
  css.textContent =
    // 苹果风格:磨砂玻璃圆形徽标,轻投影,极简「照片」符号
    '.fig-badge{position:absolute;width:30px;height:30px;border-radius:50%;z-index:6;cursor:pointer;' +
    'display:flex;align-items:center;justify-content:center;color:#fff;' +
    'background:rgba(10,132,255,.92);-webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px);' +
    'box-shadow:0 2px 8px rgba(0,0,0,.28),inset 0 0 0 .5px rgba(255,255,255,.35);' +
    '-webkit-tap-highlight-color:transparent;transition:transform .12s}' +
    '.fig-badge:active{transform:scale(.88)}' +
    '.fig-badge svg{width:17px;height:17px;display:block}' +
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

  function closePop() {
    var p = document.getElementById('fig-pop'); if (p) p.remove();
    var m = document.getElementById('fig-pop-mask'); if (m) m.remove();
  }
  function openPop(badge, fig) {
    closePop();
    var mask = document.createElement('div'); mask.id = 'fig-pop-mask'; mask.className = 'fig-pop-mask';
    mask.addEventListener('click', closePop); document.body.appendChild(mask);
    var pop = document.createElement('div'); pop.id = 'fig-pop'; pop.className = 'fig-pop';
    var body = md(fig.desc);
    pop.innerHTML = '<span class="fig-x">✕</span>' +
      (fig.caption ? '<h4>' + esc(fig.caption) + '</h4>' : '<h4>图</h4>') +
      '<div class="fig-body">' + (body != null ? body : ('<p>' + esc(fig.desc).replace(/\n/g, '<br>') + '</p>')) + '</div>';
    document.body.appendChild(pop);
    pop.querySelector('.fig-x').addEventListener('click', closePop);
    // 定位:徽标下方,超出则上方/夹到视口内
    var br = badge.getBoundingClientRect(), pr = pop.getBoundingClientRect();
    var left = Math.min(Math.max(8, br.left), window.innerWidth - pr.width - 8);
    var top = br.bottom + 8; if (top + pr.height > window.innerHeight - 8) top = Math.max(8, br.top - pr.height - 8);
    pop.style.left = left + 'px'; pop.style.top = top + 'px';
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
    rec.figs.forEach(function (f) {
      var bb = (f.bbox && f.bbox.length === 4) ? f.bbox : [0.86, 0.04, 0.98, 0.12];  // 无 bbox → 右上角
      var bx = Math.max(0, Math.min(1, bb[0])) * cssW, by = Math.max(0, Math.min(1, bb[1])) * cssH;
      var b = document.createElement('div'); b.className = 'fig-badge'; b.innerHTML = PHOTO_SVG;
      b.style.left = Math.max(2, Math.min(cssW - 32, bx + 4)) + 'px';
      b.style.top = Math.max(2, Math.min(cssH - 32, by + 4)) + 'px';
      b.style.pointerEvents = 'auto';
      b.title = f.caption || '图说明';
      b.addEventListener('click', function (e) { e.stopPropagation(); openPop(b, f); });
      layer.appendChild(b);
    });
  }

  window.renderFiguresOnPage = function (pw, num) {
    if (!pw || !num || typeof FILE_REL === 'undefined' || !FILE_REL) return;
    if (_cache[num]) { draw(pw, num); if (_cache[num].pending) schedulePoll(pw, num); return; }
    fetch('/pdf/api/page-figures?file=' + encodeURIComponent(FILE_REL) + '&page=' + num)
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) return;
        _cache[num] = { figs: d.figures || [], pending: !!d.pending };
        draw(pw, num);
        if (d.pending) schedulePoll(pw, num);
      }).catch(function () {});
  };

  function schedulePoll(pw, num) {       // 后台描述 ~8s,轮询几次拿结果(只在该页仍在 DOM 时)
    if ((_poll[num] || 0) >= 6) return;
    _poll[num] = (_poll[num] || 0) + 1;
    setTimeout(function () {
      if (!pw.isConnected) return;
      fetch('/pdf/api/page-figures?file=' + encodeURIComponent(FILE_REL) + '&page=' + num)
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d || !d.ok) return;
          _cache[num] = { figs: d.figures || [], pending: !!d.pending };
          draw(pw, num);
          if (d.pending) schedulePoll(pw, num);
        }).catch(function () {});
    }, 4500);
  }
})();
