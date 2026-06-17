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
    'display:flex;align-items:center;justify-content:center;color:#fff;opacity:.5;' +
    'background:rgba(10,132,255,.62);-webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px);' +
    'box-shadow:0 2px 7px rgba(0,0,0,.2),inset 0 0 0 .5px rgba(255,255,255,.3);' +
    '-webkit-tap-highlight-color:transparent;transition:transform .12s,opacity .12s;touch-action:none}' +
    '.fig-badge:active{transform:scale(.88)}.fig-badge:hover{opacity:.95}' +
    '.fig-badge svg{width:15px;height:15px;display:block}' +
    '.fig-pop{position:fixed;z-index:130;max-width:min(86vw,440px);background:#11192c;color:#e8eeff;' +
    'border:1px solid #2a3a63;border-radius:16px;box-shadow:0 12px 40px rgba(0,0,0,.5);' +
    'padding:14px 16px;font-size:14px;line-height:1.6;max-height:60vh;overflow-y:auto;-webkit-overflow-scrolling:touch}' +
    '.fig-pop h4{margin:0 0 6px;font-size:14px;color:#7fb0ff;font-weight:600}' +
    '.fig-pop .fig-x{position:absolute;top:8px;right:10px;color:#8aa;cursor:pointer;font-size:16px;line-height:1}' +
    '.fig-pop p{margin:.35em 0}.fig-pop code{background:#0b1220;padding:1px 4px;border-radius:4px}' +
    '.fig-pop-mask{position:fixed;inset:0;z-index:129;background:transparent}' +
    // 点徽标时高亮该图的 YOLO 框范围
    '.fig-hl{position:absolute;z-index:5;pointer-events:none;border:2.5px solid rgba(10,132,255,.92);' +
    'background:rgba(10,132,255,.10);border-radius:7px;box-shadow:0 0 0 2px rgba(10,132,255,.18);' +
    'animation:figHlIn .22s ease-out}' +
    '@keyframes figHlIn{from{opacity:0;transform:scale(1.03)}to{opacity:1;transform:scale(1)}}' +
    // 图区命中层(透明可点;touch-action:auto 不挡阅读滚动,拖拽改用徽标当把手)
    '.fig-hit{position:absolute;z-index:5;cursor:pointer;-webkit-tap-highlight-color:transparent}' +
    '.fig-drag-ghost{position:fixed;z-index:240;width:118px;max-height:150px;object-fit:contain;opacity:.6;' +
    'border:2px solid rgba(10,132,255,.85);border-radius:9px;box-shadow:0 10px 28px rgba(0,0,0,.55);' +
    'transform:translate(-50%,-50%);pointer-events:none;background:#fff}' +
    '#grammar-panel.fig-drop-ready{outline:2px dashed rgba(10,132,255,.5);outline-offset:-4px}' +
    '#grammar-panel.fig-drop-over{outline:3px solid rgba(10,132,255,.95);background:rgba(10,132,255,.07)}' +
    '#fig-drop-plus{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);z-index:131;' +
    'font-size:64px;font-weight:300;color:rgba(10,132,255,.6);pointer-events:none;text-shadow:0 2px 8px rgba(0,0,0,.4)}' +
    // 助手对话里「已带入的图」附件条(缩略图 + 图注 + ✕)
    '#asst-fig-chip{display:flex;align-items:center;gap:8px;padding:6px 8px;margin:0 10px 6px;background:#16203a;' +
    'border:1px solid #2a3a63;border-radius:10px}' +
    '#asst-fig-chip .afc-thumb{width:46px;height:46px;object-fit:cover;border-radius:6px;border:1px solid #3b6db5;background:#fff;flex:none}' +
    '#asst-fig-chip .afc-cap{flex:1;font-size:12px;color:#cfe6ff;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
    '#asst-fig-chip .afc-x{background:transparent;border:none;color:#9ab;font-size:15px;cursor:pointer;flex:none;padding:0 4px}';
  document.head.appendChild(css);

  var PHOTO_SVG = '<svg viewBox="0 0 24 24" fill="none"><rect x="3" y="5" width="18" height="14" rx="3" stroke="currentColor" stroke-width="1.7"/>' +
    '<circle cx="8.5" cy="10" r="1.6" fill="currentColor"/>' +
    '<path d="M5 17l4.5-4.5a1.5 1.5 0 0 1 2 0L17 18" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  function md(t) {
    try { return (typeof window.md === 'function') ? window.md(t || '') : null; } catch (_) { return null; }
  }
  function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }

  var _popTimer = null, _popRepos = null, _popBadge = null, _hlEl = null;
  function clearHl() { if (_hlEl) { _hlEl.remove(); _hlEl = null; } }
  function showHl(elOrPw, fig) {           // 在页面上画出该图 YOLO 框(fbox)的范围
    clearHl();
    var bb = (fig.fbox && fig.fbox.length === 4) ? fig.fbox : fig.bbox;
    if (!bb || bb.length !== 4 || !elOrPw) return;
    var pw = (elOrPw.classList && elOrPw.classList.contains('page-wrap')) ? elOrPw
             : (elOrPw.closest && elOrPw.closest('.page-wrap'));
    if (!pw) return;
    var layer = pw.querySelector('.fig-layer'); if (!layer) return;
    var canvas = pw.querySelector('canvas');
    var cssW = (canvas && canvas.clientWidth) || pw.clientWidth, cssH = (canvas && canvas.clientHeight) || pw.clientHeight;
    if (!cssW || !cssH) return;
    var el = document.createElement('div'); el.className = 'fig-hl';
    el.style.left = (bb[0] * cssW) + 'px'; el.style.top = (bb[1] * cssH) + 'px';
    el.style.width = Math.max(2, (bb[2] - bb[0]) * cssW) + 'px';
    el.style.height = Math.max(2, (bb[3] - bb[1]) * cssH) + 'px';
    layer.appendChild(el); _hlEl = el;
  }
  function closePop() {
    var p = document.getElementById('fig-pop'); if (p) p.remove();
    if (_popTimer) { clearTimeout(_popTimer); _popTimer = null; }
    if (_popRepos) { window.removeEventListener('scroll', _popRepos, true); window.removeEventListener('resize', _popRepos); _popRepos = null; }
    clearHl();
    _popBadge = null;
  }
  function openPop(badge, fig) {
    if (_popBadge === badge) { closePop(); return; }   // 再点同一徽标 → 关
    closePop();
    _popBadge = badge;
    showHl(badge, fig);                                // 高亮该图范围
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
      // 图区命中层:点/长按图(非徽标)→ 设为「焦点图」+ 高亮范围(不弹描述);供助手据图回答 + 拖进对话
      var hb = (f.fbox && f.fbox.length === 4) ? f.fbox : f.bbox;
      if (hb && hb.length === 4 && hb[2] > hb[0] && hb[3] > hb[1]) {
        var hit = document.createElement('div'); hit.className = 'fig-hit';
        hit.style.left = (hb[0] * cssW) + 'px'; hit.style.top = (hb[1] * cssH) + 'px';
        hit.style.width = Math.max(8, (hb[2] - hb[0]) * cssW) + 'px';
        hit.style.height = Math.max(8, (hb[3] - hb[1]) * cssH) + 'px';
        hit.style.pointerEvents = 'auto';
        _bindFigHit(hit, f, num);
        layer.appendChild(hit);
      }
      var b = document.createElement('div'); b.className = 'fig-badge'; b.innerHTML = PHOTO_SVG;
      b.style.left = pos[0] + 'px'; b.style.top = pos[1] + 'px'; b.style.pointerEvents = 'auto';
      b.title = f.caption || '图说明';
      _bindBadge(b, f, num);    // 轻点=描述浮层;长按=拖进助手对话(徽标当拖拽把手,小不挡滚动)
      layer.appendChild(b);
    });
  }

  // ── 焦点图:点/长按图区 → 设 window.__figFocus + 高亮;长按拖动 → ghost 拖进右侧助手栏入上下文 ──
  function _figHasInk(num, bb) {
    try {
      var sp = (window._ink && window._ink.byPage && window._ink.byPage[num]) || [];
      for (var i = 0; i < sp.length; i++) {
        var ps = sp[i].p || [];
        for (var j = 0; j < ps.length; j++) {
          if (ps[j][0] >= bb[0] && ps[j][0] <= bb[2] && ps[j][1] >= bb[1] && ps[j][1] <= bb[3]) return true;
        }
      }
    } catch (_) {}
    return false;
  }
  function setFigFocus(fig, anchorEl, num) {
    var bb = (fig.fbox && fig.fbox.length === 4) ? fig.fbox : fig.bbox;
    if (!bb || bb.length !== 4) return;
    window.__figFocus = {
      file_rel: (typeof FILE_REL !== 'undefined' ? FILE_REL : ''), page: num, box: bb,
      caption: fig.caption || '', desc: fig.desc || '', group: !!fig.group,
      has_ink: _figHasInk(num, bb), t: Date.now()
    };
    closePop();                 // 关掉可能开着的描述浮层
    var pw = (anchorEl && anchorEl.closest) ? anchorEl.closest('.page-wrap')
             : document.querySelector('.page-wrap[data-page-num="' + num + '"]');
    showHl(pw, fig);            // 只高亮范围,不弹描述
    _setFigChip(fig, num);      // 助手对话里挂个缩略图附件条(看得见带了哪张图)
    if (typeof _toast === 'function') _toast(fig.group ? '已聚焦这个图组,问助手会带上它' : '已聚焦这张图,问助手会带上它');
  }
  window.__setFigFocus = setFigFocus;

  // 助手对话上方「已带入的图」附件条:缩略图 + 图注 + ✕。__figFocus 是上下文真值,附件条是它的可视镜像
  function _setFigChip(fig, num) {
    try {
      var pane = document.getElementById('side-pane-asst');
      var input = pane && pane.querySelector('#asst-input');
      if (!input) return;       // 助手还没建/没开 → 先不挂(开助手后 __figFocus 仍在上下文里)
      var chip = document.getElementById('asst-fig-chip');
      if (!chip) { chip = document.createElement('div'); chip.id = 'asst-fig-chip'; input.parentNode.insertBefore(chip, input); }
      chip.innerHTML = '';
      var img = document.createElement('img'); img.className = 'afc-thumb'; img.src = _figCropUrl(fig, num);
      var cap = document.createElement('span'); cap.className = 'afc-cap'; cap.textContent = (fig.group ? '图组 · ' : '') + (fig.caption || '这张图');
      var x = document.createElement('button'); x.className = 'afc-x'; x.textContent = '✕';
      x.addEventListener('click', function () { _clearFigChip(); window.__figFocus = null; clearHl(); });
      chip.appendChild(img); chip.appendChild(cap); chip.appendChild(x);
    } catch (_) {}
  }
  function _clearFigChip() { var c = document.getElementById('asst-fig-chip'); if (c) c.remove(); }
  window.__clearFigFocus = function () { _clearFigChip(); window.__figFocus = null; clearHl(); };

  // 点图/徽标/浮层/助手栏 之外 → 取消高亮框(__figFocus 上下文保留,由附件条 ✕ 才清)
  document.addEventListener('pointerdown', function (e) {
    if (!_hlEl) return;
    var t = e.target;
    if (t && t.closest && (t.closest('.fig-hit') || t.closest('.fig-badge') || t.closest('.fig-pop') ||
        t.closest('#side-pane-asst') || t.closest('#grammar-panel'))) return;
    clearHl();
  }, true);

  // ── 长按拖动 → ghost 跟手 + 右侧助手栏冒「+」→ 拖进去入上下文 ──
  var _drag = null;
  function _figCropUrl(fig, num) {
    var bb = (fig.fbox && fig.fbox.length === 4) ? fig.fbox : fig.bbox;
    return '/pdf/api/figure-crop?file=' + encodeURIComponent(FILE_REL) + '&page=' + num +
           '&box=' + bb.map(function (v) { return (+v).toFixed(4); }).join(',') +
           (_figHasInk(num, bb) ? '&ink=1' : '');
  }
  function _asstPanel() { return document.getElementById('grammar-panel'); }
  function _overAsst(x, y) {
    var dp = _asstPanel(); if (!dp) return false;
    var r = dp.getBoundingClientRect();
    if (r.width < 10 || r.right < 10 || r.left > window.innerWidth - 4) return false;   // 抽屉没开
    return x >= r.left - 24 && x <= r.right + 4 && y >= r.top && y <= r.bottom;
  }
  function _dragStart(fig, num, e) {
    _dragCancel();
    var g = document.createElement('img'); g.className = 'fig-drag-ghost';
    g.src = _figCropUrl(fig, num); g.alt = '';
    document.body.appendChild(g);
    _drag = { fig: fig, num: num, ghost: g };
    var dp = _asstPanel();
    if (dp) {
      dp.classList.add('fig-drop-ready');
      var plus = document.createElement('div'); plus.id = 'fig-drop-plus'; plus.textContent = '＋';
      dp.appendChild(plus);
    }
    _positionGhost(e);
    if (navigator.vibrate) { try { navigator.vibrate(14); } catch (_) {} }
  }
  function _positionGhost(e) { if (_drag && _drag.ghost) { _drag.ghost.style.left = e.clientX + 'px'; _drag.ghost.style.top = e.clientY + 'px'; } }
  function _dragMove(e) {
    if (!_drag) return; _positionGhost(e);
    var dp = _asstPanel(); if (dp) dp.classList.toggle('fig-drop-over', _overAsst(e.clientX, e.clientY));
  }
  function _dragEnd(fig, num, e) {
    var over = _overAsst(e.clientX, e.clientY);
    _dragCancel();
    if (over) {
      setFigFocus(fig, null, num);
      try { if (window.switchSideTab) window.switchSideTab('asst'); } catch (_) {}
      if (typeof _toast === 'function') _toast('📷 已把这张图带进助手对话');
    }
  }
  function _dragCancel() {
    if (_drag && _drag.ghost) _drag.ghost.remove();
    _drag = null;
    var dp = _asstPanel();
    if (dp) { dp.classList.remove('fig-drop-ready'); dp.classList.remove('fig-drop-over'); var p = document.getElementById('fig-drop-plus'); if (p) p.remove(); }
  }

  // 图区命中层:只管轻点 → 设焦点(放开滚动,不拦拖拽)。拖拽统一走徽标(_bindBadge)
  function _bindFigHit(hit, fig, num) {
    var sx = 0, sy = 0, st = 0, moved = false;
    hit.addEventListener('pointerdown', function (e) {
      if (e.pointerType === 'mouse' && e.button !== 0) return;
      sx = e.clientX; sy = e.clientY; st = Date.now(); moved = false;
    });
    hit.addEventListener('pointermove', function (e) {
      if (!moved && (Math.abs(e.clientX - sx) > 8 || Math.abs(e.clientY - sy) > 8)) moved = true;
    });
    hit.addEventListener('pointerup', function (e) {
      if (!moved && Date.now() - st < 600) setFigFocus(fig, hit, num);   // 轻点/单击 → 设焦点
    });
  }

  // 徽标:轻点 → 描述浮层;长按(380ms 不动)→ 拖拽进助手对话。徽标 touch-action:none + setPointerCapture → 拖拽稳,不会被滚动取消
  function _bindBadge(b, fig, num) {
    var sx = 0, sy = 0, st = 0, lp = null, moved = false, dragging = false, pid = null;
    b.addEventListener('pointerdown', function (e) {
      if (e.pointerType === 'mouse' && e.button !== 0) return;
      sx = e.clientX; sy = e.clientY; st = Date.now(); moved = false; dragging = false; pid = e.pointerId;
      lp = setTimeout(function () {
        if (!moved) { dragging = true; _dragStart(fig, num, e); try { b.setPointerCapture(pid); } catch (_) {} }
      }, 380);
    });
    b.addEventListener('pointermove', function (e) {
      if (!moved && (Math.abs(e.clientX - sx) > 7 || Math.abs(e.clientY - sy) > 7)) moved = true;
      if (dragging) { _dragMove(e); e.preventDefault(); }
      else if (moved && lp) { clearTimeout(lp); lp = null; }
    });
    b.addEventListener('pointerup', function (e) {
      if (lp) { clearTimeout(lp); lp = null; }
      if (dragging) { dragging = false; try { b.releasePointerCapture(pid); } catch (_) {} _dragEnd(fig, num, e); return; }
      if (!moved && Date.now() - st < 500) { e.stopPropagation(); openPop(b, fig); }   // 轻点 → 描述
    });
    b.addEventListener('pointercancel', function () { if (lp) { clearTimeout(lp); lp = null; } if (dragging) { dragging = false; _dragCancel(); } });
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
