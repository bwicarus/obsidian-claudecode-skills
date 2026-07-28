/* rc-highlight.js — 统一控制层:高亮编辑浮层 + 高亮列表(共享,PDF/EPUB 通用)。
 * 内容无关部分在这里:色板互斥单选 + 备注 textarea + 删除浮层(openEditor)+ 列表行(色点/文字/备注/跳转/删除)(renderList)。
 * 弹层生命周期照搬 rc-figures.js:首用 injectCss(rc-hl-* 独立类名,不跟 PDF .hl-saved / EPUB .ep-hl-item 冲突)、
 *   再点同锚关、点外关(pointerdown capture)、定位一次。
 * 底座耦合一律走 opts(适配器):锚点格式 / 把颜色改进 DOM / 后端端点 / 跳转 —— 模块绝不直接碰 epub-html.js 内部或 PDF char 层。
 *   openEditor: onColor(color) / onNote(text) / onDelete() + anchorEl / anchorSelector(点外关忽略) / placeBelow(定位模式)。
 *   renderList: onJump(h) / onDelete(h) + getText/getNote/getColor(默认读 h.text/h.note/h.color)。
 * 视觉照搬 epub_html_reader.html 的 #ep-hlpop / .ep-hl-item CSS。 */
(function () {
  if (!window.RC) window.RC = {};
  if (window.RC.highlight) return;

  function esc(s) { return (window.RC && RC.esc) ? RC.esc(s) : String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
  function toast(m) { if (window.RC && RC.toast) RC.toast(m); }

  var injected = false;
  function injectCss() {
    if (injected) return; injected = true;
    var css = document.createElement('style'); css.id = 'rc-hl-css';
    css.textContent =
      '.rc-hl-pop{position:fixed;z-index:140;min-width:min(280px,92vw);max-width:min(380px,92vw);box-sizing:border-box;background:var(--rc-bg-popover,#0f1830);border:1px solid var(--rc-border-popover,#2f4a7d);border-radius:var(--rc-radius-popover,11px);box-shadow:var(--rc-shadow-pop,0 10px 30px rgba(0,0,0,.6));padding:10px;color:#e6edf3;-webkit-tap-highlight-color:transparent}' +
      '.rc-hl-pop .rc-hl-prev{font-size:12.5px;line-height:1.5;color:#cfe0ff;background:rgba(0,0,0,.25);border-left:2px solid #60a5fa;border-radius:4px;padding:6px 9px;margin-bottom:8px;word-break:break-word}' +
      '.rc-hl-pop .rc-hl-sent{font-size:11.5px;line-height:1.5;color:#9fb0d6;margin-bottom:8px;word-break:break-word}' +
      // AI 译文/解释正文行(照搬 PDF #hl-popover .hl-snip-row.body:带 kind 标签 译文/解释/备注)
      '.rc-hl-pop .rc-hl-body{font-size:12px;line-height:1.5;color:#cfe0ff;margin-bottom:8px;word-break:break-word}' +
      // 预览块单行省略 + 点击展开(照搬 PDF #hl-popover .hl-snip-content / .expanded)
      '.rc-hl-pop .rc-hl-prevbox{cursor:pointer}' +
      '.rc-hl-pop .rc-hl-prevbox .rc-hl-prev,.rc-hl-pop .rc-hl-prevbox .rc-hl-sent,.rc-hl-pop .rc-hl-prevbox .rc-hl-body{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}' +
      '.rc-hl-pop .rc-hl-prevbox.expanded .rc-hl-prev,.rc-hl-pop .rc-hl-prevbox.expanded .rc-hl-sent,.rc-hl-pop .rc-hl-prevbox.expanded .rc-hl-body{white-space:normal;overflow:visible;text-overflow:clip}' +
      // 色板行:照搬 PDF #hl-popover .row(align-items:center)+ .row-lbl(11px/#7a8497/margin-right 2px)
      '.rc-hl-pop .rc-hl-sw{display:flex;align-items:center;gap:8px;margin-bottom:8px;flex-wrap:wrap;row-gap:8px;width:auto;height:auto;min-height:0}' +
      '.rc-hl-pop .rc-hl-sw-lbl{font-size:11px;color:#7a8497;margin-right:2px}' +
      '.rc-hl-pop .rc-hl-sw-i{flex:0 0 auto;box-sizing:border-box;width:28px;height:28px;min-width:28px;min-height:28px;aspect-ratio:1/1;border-radius:50%;cursor:pointer;border:2px solid transparent;display:inline-block;touch-action:manipulation}' +
      '.rc-hl-pop .rc-hl-sw-i.on{border-color:#fff}' +
      '.rc-hl-pop .rc-hl-note{width:100%;min-height:48px;background:var(--rc-bg-field,#0e1525);border:1px solid var(--rc-border-control,#2a3a63);color:#e6edf3;border-radius:var(--rc-radius-md,8px);padding:7px 9px;font-size:13px;resize:vertical;font-family:inherit;display:block;box-sizing:border-box}' +
      '.rc-hl-pop .rc-hl-row{display:flex;gap:8px;margin-top:8px;justify-content:flex-end}' +
      '.rc-hl-pop .rc-hl-row button{background:var(--rc-bg-control,#16203a);border:1px solid var(--rc-border-control,#2a3a63);color:#cfe0ff;border-radius:7px;padding:5px 11px;font-size:13px;cursor:pointer}' +
      '.rc-hl-pop .rc-hl-row button.rc-hl-del{background:var(--rc-danger,#7a2828);border-color:#9a3a3a;color:var(--rc-danger-text,#ffdede)}' +
      // iOS Mail 式左滑删除(照搬 PDF reader.src/19-dict.js _attachSnipBehavior 的三个 CSS 点:
      //   ① 滑动内容 .rc-hl-slide(transform + transition);② 背后绝对定位删除条 .rc-hl-swipe-del(visibility:hidden);
      //   ③ .swiped 切换 → slide translateX(-64) + 删除条 visible)。
      '.rc-hl-item{position:relative;overflow:hidden;border-radius:10px;margin-bottom:8px}' +
      '.rc-hl-item .rc-hl-slide{display:flex;align-items:flex-start;gap:9px;background:#11192c;border:1px solid #243056;border-radius:10px;padding:9px 10px;position:relative;z-index:2;transition:transform .16s ease;will-change:transform}' +
      '.rc-hl-item.swiped .rc-hl-slide{transform:translateX(-64px)}' +
      '.rc-hl-item .rc-hl-swipe-del{position:absolute;right:0;top:0;bottom:0;width:64px;background:#7a2828;color:#ffdede;border:none;font-size:17px;cursor:pointer;display:flex;align-items:center;justify-content:center;z-index:1;visibility:hidden}' +
      '.rc-hl-item.swiped .rc-hl-swipe-del{visibility:visible}' +
      '.rc-hl-item .rc-hl-dot{flex:0 0 auto;width:14px;height:14px;border-radius:4px;margin-top:2px;cursor:pointer}' +
      '.rc-hl-item .rc-hl-tx{flex:1;min-width:0;font-size:13px;color:#dbe7ff;line-height:1.5;word-break:break-word}' +
      '.rc-hl-item .rc-hl-tx .rc-hl-nt{display:block;color:#9fb0d6;font-size:12px;margin-top:3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer}' +
      '.rc-hl-item .rc-hl-tx .rc-hl-nt.expanded{white-space:normal;overflow:visible;text-overflow:clip}' +
      '.rc-hl-item .rc-hl-ops{flex:0 0 auto;display:flex;flex-direction:column;gap:5px}' +
      '.rc-hl-item .rc-hl-ops button{background:#16203a;border:1px solid #2a3a63;color:#cfe0ff;border-radius:6px;padding:3px 8px;font-size:12px;cursor:pointer}' +
      '.rc-hl-item .rc-hl-ops button.rc-hl-del{background:#7a2828;border-color:#9a3a3a;color:#ffdede}' +
      '.rc-hl-empty{color:#7c93c4;font-size:13px;text-align:center;padding:20px;line-height:1.6}';
    document.head.appendChild(css);
  }

  // ── 编辑浮层 ──
  var _anchorEl = null, _outside = null;
  function closeEditor() {
    var p = document.getElementById('rc-hl-pop'); if (p) p.remove();
    if (_outside) { document.removeEventListener('pointerdown', _outside, true); _outside = null; }
    _anchorEl = null;
  }
  function positionPop(pop, anchorEl, placeBelow) {
    var w = pop.offsetWidth || 230, h = pop.offsetHeight || 120;
    if (placeBelow && anchorEl) {
      var r = anchorEl.getBoundingClientRect();
      var left = Math.min(Math.max(8, r.left), window.innerWidth - w - 8);
      var top = r.bottom + 6;
      if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 6);
      pop.style.left = left + 'px'; pop.style.top = top + 'px';
    } else {
      // EPUB 原行为:屏幕水平居中 + 上 22%(下界 56,避开顶栏)
      pop.style.left = Math.max(8, (window.innerWidth - w) / 2) + 'px';
      pop.style.top = Math.max(56, Math.round(window.innerHeight * 0.22)) + 'px';
    }
  }
  // opts: {colors:[..], current:'#hex', note:'', preview?, sentence?, body?, kind?,
  //        anchorEl?, anchorSelector?, placeBelow?, silent?, onColor(color), onNote(text), onDelete()}
  //   silent=true → 保存不弹「已保存」(host 自己弹,避免双 toast);onDelete 返回 false → 不关浮层(删除被取消)
  //   preview=高亮原文、sentence=所在句、body=AI 译文/解释正文、kind=类型(translate/explain/note)→ 决定 body 行标签
  function openEditor(opts) {
    opts = opts || {}; injectCss();
    var anchorEl = opts.anchorEl || null;
    // 再点同一个锚 → 关(照搬 rc-figures 再点关)
    if (anchorEl && _anchorEl === anchorEl && document.getElementById('rc-hl-pop')) { closeEditor(); return; }
    closeEditor(); _anchorEl = anchorEl;
    var colors = opts.colors || [], current = opts.current || '', note = opts.note || '';
    var sw = colors.map(function (c) {
      return '<i data-c="' + esc(c) + '" class="rc-hl-sw-i' + (c === current ? ' on' : '') + '" style="background:' + esc(c) + '"></i>';
    }).join('');
    var pop = document.createElement('div'); pop.id = 'rc-hl-pop'; pop.className = 'rc-hl-pop';
    // 只读预览块(照搬 PDF 高亮 popover 的 .hl-snip:看到高亮的原文 / 所在句 / AI 译文解释)。
    // 默认单行省略,点击 .rc-hl-prevbox 展开/收起全文(照搬 PDF .hl-snip-content.expanded)。
    // body 行带 kind 标签(照搬 PDF openHlPopover kindLbl:translate→🌐 翻译 / explain→💡 解释 / 其它→📝 备注)。
    var prev = '';
    if (opts.preview || (opts.sentence && opts.sentence !== opts.preview) || opts.body) {
      var rows = '';
      if (opts.preview) { var pv = String(opts.preview); rows += '<div class="rc-hl-prev">' + esc(pv.length > 800 ? pv.slice(0, 800) + '…' : pv) + '</div>'; }
      if (opts.sentence && opts.sentence !== opts.preview) { var st = String(opts.sentence); rows += '<div class="rc-hl-sent">📖 ' + esc(st.length > 800 ? st.slice(0, 800) + '…' : st) + '</div>'; }
      if (opts.body) {
        var bd = String(opts.body);
        var kindLbl = opts.kind === 'translate' ? '🌐 翻译' : opts.kind === 'explain' ? '💡 解释' : '📝 备注';
        rows += '<div class="rc-hl-body">' + kindLbl + ' ' + esc(bd.length > 800 ? bd.slice(0, 800) + '…' : bd) + '</div>';
      }
      prev = '<div class="rc-hl-prevbox" title="点击展开 / 收起原文">' + rows + '</div>';
    }
    // 照搬 PDF #hl-popover 的 <div class="row"><span class="row-lbl">🎨 颜色</span>...色板...</div>
    pop.innerHTML = prev + '<div class="rc-hl-sw"><span class="rc-hl-sw-lbl">🎨 颜色</span>' + sw + '</div>' +
      '<textarea class="rc-hl-note" placeholder="备注(可选)">' + esc(note) + '</textarea>' +
      '<div class="rc-hl-row"><button class="rc-hl-del">🗑 删除</button><button class="rc-hl-save">保存</button></div>';
    document.body.appendChild(pop);
    // 预览块点击展开/收起(单行省略 ↔ 全文)
    var pbox = pop.querySelector('.rc-hl-prevbox');
    if (pbox) pbox.addEventListener('click', function () { pbox.classList.toggle('expanded'); });
    // 色板互斥单选:点 → 清其它 .on + 自己 .on + onColor(底座 PATCH + 重画)。不关浮层(照搬 PDF
    //   _openHlPopoverNative 「切换到新色：立即 PATCH，不用等 [保存]」分支,同样不关浮层/不 toast)。
    // 再点已激活的当前色 → 取消颜色(no-color 虚框模式):逐字照搬 native 290-310 的 hasNote 判断
    //   (note/body/sentence 都空 → 直接删该高亮;有备注 → 保留为无色虚框)。hasNote 为真的分支只让
    //   host 决定业务(onColor(''):PATCH color='' + toast),关浮层这件事 native 自己总是做,故这里
    //   照搬做;hasNote 为假则直接复用 onDelete(与 🗑 按钮同一个回调/同一条 confirm 文案),沿用它已有
    //   的「返回 false(用户取消删除确认)→ 不关浮层」协议,不再各自为政。
    pop.querySelectorAll('.rc-hl-sw-i').forEach(function (i) {
      i.onclick = function (e) {
        e.stopPropagation();
        if (i.classList.contains('on')) {
          var hasNote = (opts.note || '').trim() || (opts.body || '').trim() || (opts.sentence || '').trim();
          if (!hasNote) {
            Promise.resolve(opts.onDelete ? opts.onDelete() : undefined).then(function (ok) { if (ok !== false) closeEditor(); }).catch(function () {});
          } else {
            if (opts.onColor) opts.onColor('');
            closeEditor();
          }
          return;
        }
        pop.querySelectorAll('.rc-hl-sw-i').forEach(function (x) { x.classList.remove('on'); });
        i.classList.add('on');
        if (opts.onColor) opts.onColor(i.dataset.c);
      };
    });
    // 删除:onDelete(底座删后端 + 自带 toast)。删成功才关浮层——onDelete 返回 false(如 confirm 取消)则**不**关,
    //   让用户回到编辑态(修复:取消删除确认后浮层仍被关掉)。EPUB onDelete 返回 undefined → 照旧关,无回归。
    pop.querySelector('.rc-hl-del').onclick = function () {
      if (!opts.onDelete) { closeEditor(); return; }
      Promise.resolve(opts.onDelete()).then(function (ok) { if (ok !== false) closeEditor(); }).catch(function () {});
    };
    // 保存:onNote(底座 PATCH note) + 关浮层 + toast。opts.silent 时不 toast(PDF host 的 _hlUpdate 会弹「已保存」,避免双重);
    //   EPUB 不传 silent → 保留本层 toast(EPUB patchHl 有意不弹 note 的「已保存」,靠这里)。
    pop.querySelector('.rc-hl-save').onclick = function () { if (opts.onNote) opts.onNote(pop.querySelector('.rc-hl-note').value); closeEditor(); if (!opts.silent) toast('已保存'); };
    positionPop(pop, anchorEl, opts.placeBelow);
    // 点外关(pointerdown capture):浮层内 / 锚元素(anchorSelector)上的点击都忽略,
    //   让锚的点击事件去触发再点关 / 切到另一条,避免按下时就先把自己关掉。
    var anchorSel = opts.anchorSelector || '';
    _outside = function (ev) {
      var t = ev.target;
      if (t && t.closest && (t.closest('#rc-hl-pop') || (anchorSel && t.closest(anchorSel)))) return;
      closeEditor();
    };
    document.addEventListener('pointerdown', _outside, true);
  }

  // ── 高亮列表 ──
  // opts: {getText(h)->str, getNote(h)->str, getColor(h)->str, onJump(h), onDelete(h), reverse?, emptyHtml?}
  function renderList(container, highlights, opts) {
    if (!container) return; injectCss(); opts = opts || {}; highlights = highlights || [];
    if (!highlights.length) {
      container.innerHTML = '<div class="rc-hl-empty">' + (opts.emptyHtml || '还没有高亮。<br>选中文字 → 高亮') + '</div>';
      return;
    }
    container.innerHTML = '';
    var getText = opts.getText || function (h) { return h.text || ''; };
    var getNote = opts.getNote || function (h) { return h.note || ''; };
    var getColor = opts.getColor || function (h) { return h.color || ''; };
    var list = opts.reverse ? highlights.slice().reverse() : highlights;
    list.forEach(function (h) {
      var row = document.createElement('div'); row.className = 'rc-hl-item';
      var txt = String(getText(h) || ''), note = String(getNote(h) || '');
      // 结构:.rc-hl-slide(可滑动卡片:色点 + 文字/备注 + 跳转)+ 背后 .rc-hl-swipe-del(左滑露出的删除条)
      row.innerHTML = '<div class="rc-hl-slide">' +
          '<span class="rc-hl-dot" title="点这里 / 左滑显示删除" style="background:' + esc(getColor(h)) + '"></span>' +
          '<div class="rc-hl-tx">' + esc(txt.slice(0, 120)) + (txt.length > 120 ? '…' : '') +
          (note ? '<span class="rc-hl-nt">📝 ' + esc(note) + '</span>' : '') + '</div>' +
          '<div class="rc-hl-ops"><button class="rc-hl-go">跳转</button></div>' +
        '</div>' +
        '<button class="rc-hl-swipe-del" type="button" title="删除高亮">🗑</button>';
      var slide = row.querySelector('.rc-hl-slide');
      row.querySelector('.rc-hl-go').onclick = function (e) { e.stopPropagation(); if (opts.onJump) opts.onJump(h); };
      row.querySelector('.rc-hl-swipe-del').onclick = function (e) { e.stopPropagation(); if (opts.onDelete) opts.onDelete(h); row.remove(); };
      // 色点 = 显/隐删除条把手(桌面单击切换;移动端配合触屏左滑)
      var dot = row.querySelector('.rc-hl-dot');
      if (dot) dot.onclick = function (e) { e.stopPropagation(); row.classList.toggle('swiped'); slide.style.transform = ''; };
      // 备注单行省略 → 点击展开/收起
      var nt = row.querySelector('.rc-hl-nt');
      if (nt) nt.onclick = function (e) { e.stopPropagation(); nt.classList.toggle('expanded'); };
      _attachListSwipe(row, slide);
      container.appendChild(row);
    });
  }

  // iOS Mail 式触屏左滑:在 .rc-hl-slide 上 translateX 跟手,松手按位移定 reveal/reset(照搬 PDF _attachSnipBehavior 触屏分支)。
  // 仅横向主导才拦(纵向交回抽屉滚动);passive 不 preventDefault → 不影响竖滑。删除条点击删,不在此处理。
  function _attachListSwipe(row, slide) {
    var sx = 0, sy = 0, dx = 0, axis = '', sw = false;
    function reveal() { row.classList.add('swiped'); slide.style.transform = ''; }
    function reset() { row.classList.remove('swiped'); slide.style.transform = ''; }
    slide.addEventListener('touchstart', function (e) { var t = e.touches[0]; sx = t.clientX; sy = t.clientY; dx = 0; axis = ''; sw = true; }, { passive: true });
    slide.addEventListener('touchmove', function (e) {
      if (!sw) return; var t = e.touches[0]; dx = t.clientX - sx; var dy = t.clientY - sy;
      if (!axis) { if (Math.abs(dx) > 6 || Math.abs(dy) > 6) axis = Math.abs(dx) > Math.abs(dy) ? 'x' : 'y'; }
      if (axis === 'x') {
        if (dx < 0) slide.style.transform = 'translateX(' + Math.max(dx, -80) + 'px)';
        else if (row.classList.contains('swiped')) slide.style.transform = 'translateX(' + Math.min(dx, 30) + 'px)';
      }
    }, { passive: true });
    slide.addEventListener('touchend', function () {
      sw = false;
      if (axis === 'x') { if (dx < -40) reveal(); else if (dx > 30 || !row.classList.contains('swiped')) reset(); else reveal(); }
      dx = 0; axis = '';
    });
  }

  // 高亮统一手势(PDF/EPUB 共用):长按开框 + 双击(助手开着时)入上下文。绑定无关的纯状态机——
  // host 在自己的 pointer 事件里喂 down/move/up/cancel,内部管长按计时、双击窗口、移动取消。
  // cfg: { longPressMs?, doubleTapMs?, moveTol?, onLongPress(key), onDoubleTap(key) }
  // 用法:g=RC.highlight.gesture(cfg);  pointerdown→g.down(key,x,y); pointermove→g.move(x,y); pointerup→g.up(key); pointercancel→g.cancel()
  function gesture(cfg) {
    cfg = cfg || {};
    var LP = cfg.longPressMs || 460, DT = cfg.doubleTapMs || 420, MT = cfg.moveTol || 12;
    var st = null, lpT = null, lastT = 0, lastK = '';
    function clr() { if (lpT) { clearTimeout(lpT); lpT = null; } }
    return {
      down: function (key, x, y) {
        st = { key: key, x: x, y: y, fired: false }; clr();
        lpT = setTimeout(function () {
          if (!st) return; st.fired = true; clr();
          try { var s = window.getSelection(); if (s && s.removeAllRanges) s.removeAllRanges(); } catch (e) {}
          if (cfg.onLongPress) cfg.onLongPress(key);
        }, LP);
      },
      move: function (x, y) { if (!st) return; if (Math.abs(x - st.x) > MT || Math.abs(y - st.y) > MT) { clr(); st = null; } },
      up: function (key) {
        if (!st) return; var s = st; st = null; clr();
        if (s.fired) return;                       // 长按已触发 → 本次不再当 tap
        var now = (window.Date ? Date.now() : 0);
        if (now - lastT < DT && lastK === key && key) { lastT = 0; lastK = ''; if (cfg.onDoubleTap) cfg.onDoubleTap(key); return; }
        lastT = now; lastK = key;                  // 记为可能的双击首击;单击本身不开框(开框改长按)
      },
      cancel: function () {
        // iOS 常把「点在可滚动区域」的触摸先判成滚动候选而发 pointercancel。若未超位移容差且长按未触发,
        // 仍把它记为可能的双击首击,否则用户在 iPad 上会出现「点了没反应、要多点一次」的体感。
        if (st && !st.fired && st.key) { lastT = (window.Date ? Date.now() : 0); lastK = st.key; }
        clr(); st = null;
      }
    };
  }

  window.RC.highlight = {
    openEditor: openEditor,
    closeEditor: closeEditor,
    renderList: renderList,
    gesture: gesture
  };
})();
