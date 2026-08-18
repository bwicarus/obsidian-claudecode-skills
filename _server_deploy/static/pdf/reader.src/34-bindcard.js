// ══════════ 卡片绑到**书页正文**的字符锚（用户设计 C15 第二版，2026-08-19）══════════
//
// 第一版只能把卡钉到自建页的格子块（`bind.kind === 'upage-block'`）。用户的原话是
// 「我们的卡片是通过绑定元素来保证自己的位置和**自身的数据嵌入上下文的位置**的」——
// 那就不能只在自己造的纸上成立，真书正文里也得能钉。
//
// ## 锚为什么是「序号 + 文本 + revision」三件套
//
// - **只用文本不行**：用户明确提过「要考虑单词重复出现」。同一个词在一页里出现好
//   几次时，光靠文本说不清是哪一处。
// - **只用序号不行**：序号是某一份字符层里的下标。同一本书可以有多份文字层
//   （PDF 原文字层 / Pi / PC，书库里能切换），换一份序号就全变。
// - 所以：`rev` 对得上就按序号精确定位；对不上就按文本重新找，再用**原序号**挑
//   最接近的那一处消歧。两条都失败时 `text` 仍然说得清这张卡当初钉在哪句话上。
//
// ## 坐标系
//
// 跟高亮同一套：页内绝对定位，位置由 charBoxes 的 left/top/width/height 直接给出
// （它们已经是渲染后的 CSS 像素）。**不要**用 fixed + JS 跟滚 —— 那是便签系统早就
// 写下的禁令（references/sticky-notes-design.md），页面一缩放就散架。
//
// ## 生命周期
//
// 与自建页那条一致：落下时展开、非活跃计时、到点收成球留在锚点上。可见才计时
// （IntersectionObserver），切后台停表，重渲染前拆掉旧计时器 —— 这三条都是
// _upBindCardForm 上踩出来的（见那边的注释）。

(function () {
  'use strict';

  var _pageBindPending = [];   // 绑不上的先存着：最常见的失败是"那页还没渲染"

  function _bindIdleMs() {
    try {
      if (localStorage.getItem('rc-voice-card-hide') === '0') return null;
      var v = parseInt(localStorage.getItem('rc-voice-card-secs') || '20', 10) || 20;
      return Math.max(5, Math.min(60, v)) * 1000;
    } catch (e) { return 20000; }
  }

  /// 在这一页的字符层里定位一段字符。返回 {from,to} 或 null。
  ///
  /// ⚠ 不靠 revision 号判断"序号还作不作数"，而是**直接把那段字取出来跟 text 比**。
  ///   版本号只是"可能变了"的间接证据，取出来一比是直接证据；而且页面这一侧根本
  ///   没有可靠的字符层 revision 可读（书库那边切层是另一条链路）。没带 text 时
  ///   只能信序号 —— 那是调用方自己放弃了纠错能力。
  function _resolveRange(boxes, want) {
    var from = want.from | 0, to = Math.max(want.from | 0, want.to | 0);
    var text = String(want.text || '');
    if (from < boxes.length && to < boxes.length) {
      if (!text) return { from: from, to: to };
      var got = '';
      for (var k = from; k <= to && k < boxes.length; k++) {
        if (boxes[k] && !boxes[k].sp && boxes[k].c) got += boxes[k].c;
      }
      if (got === text) return { from: from, to: to };   // 序号仍然作数
    }
    // 序号对不上了（换过文字层 / 越界 / 那段字变了）→ 按文本重新找，
    // 用原序号挑最近的一处 —— 这就是"同一个词重复出现"时的消歧依据。
    if (!text) return null;
    var joined = '';
    var index = [];   // joined 里每个字符对应的 box 下标
    for (var i = 0; i < boxes.length; i++) {
      var c = boxes[i] && boxes[i].c;
      if (!c || boxes[i].sp) continue;
      joined += c;
      index.push(i);
    }
    var best = -1, bestDist = Infinity, at = joined.indexOf(text);
    while (at >= 0) {
      var start = index[at];
      var dist = Math.abs(start - from);
      if (dist < bestDist) { bestDist = dist; best = at; }
      at = joined.indexOf(text, at + 1);
    }
    if (best < 0) return null;
    var lo = index[best];
    var hi = index[Math.min(best + text.length - 1, index.length - 1)];
    return { from: lo, to: hi };
  }

  /// 把一段字符的包围盒算出来（可能跨行 → 取整体外接矩形，卡就挂在它下方）。
  function _rangeRect(boxes, range) {
    var x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity, seen = 0;
    for (var i = range.from; i <= range.to && i < boxes.length; i++) {
      var b = boxes[i];
      if (!b) continue;
      x0 = Math.min(x0, b.left); y0 = Math.min(y0, b.top);
      x1 = Math.max(x1, b.left + b.width); y1 = Math.max(y1, b.top + b.height);
      seen++;
    }
    if (!seen || !isFinite(x0)) return null;
    return { x0: x0, y0: y0, x1: x1, y1: y1 };
  }

  /// 给一张钉在书页上的卡装上「可见才计时 → 到点收球」。
  function _armBindCard(el) {
    try { if (el.__bwBindTeardown) el.__bwBindTeardown(); } catch (e) {}
    var collapsed = el.classList.contains('pgbind-dot');
    var timer = null, io = null, visible = false;
    function paint() {
      el.classList.toggle('pgbind-dot', collapsed);
      el.title = collapsed ? '展开这张卡' : '';
    }
    function stop() { try { clearTimeout(timer); } catch (e) {} timer = null; }
    function arm() {
      stop();
      if (collapsed || !visible) return;
      if (document.visibilityState === 'hidden') return;
      var ms = _bindIdleMs();
      if (ms === null) return;
      timer = setTimeout(function () { collapsed = true; paint(); }, ms);
    }
    function onVis() { if (document.visibilityState === 'visible') arm(); else stop(); }
    el.addEventListener('click', function (ev) {
      ev.stopPropagation();
      if (!collapsed) { arm(); return; }
      collapsed = false; paint(); arm();
    });
    try {
      if (window.IntersectionObserver) {
        io = new IntersectionObserver(function (es) {
          for (var i = 0; i < es.length; i++) {
            visible = es[i].isIntersecting;
            if (visible) arm(); else stop();
          }
        }, { threshold: 0.15 });
        io.observe(el);
      } else { visible = true; arm(); }
    } catch (e) { visible = true; arm(); }
    document.addEventListener('visibilitychange', onVis);
    el.__bwBindTeardown = function () {
      stop();
      try { if (io) io.disconnect(); } catch (e) {}
      document.removeEventListener('visibilitychange', onVis);
      el.__bwBindTeardown = null;
    };
    paint();
  }

  /// 把卡钉到书页正文的一段字符上。绑不上返回 false —— 调用方据此退回浮层，
  /// **绝不把卡丢掉**：位置没了还能补，内容没了就真没了。
  window.__pageBindCard = function (bind, payload) {
    try {
      if (!bind || bind.kind !== 'page-chars') return false;
      var page = parseInt(bind.page, 10);
      if (!(page > 0)) return false;
      var pw = document.querySelector('.page-wrap[data-page-num="' + page + '"]');
      if (!pw || pw.dataset.loaded !== '1') return false;   // 那页还没渲染
      var boxes = pw.__charBoxes;
      if (!boxes || !boxes.length) return false;            // 字符层还没到
      var range = _resolveRange(boxes, {
        from: parseInt(bind.from, 10) || 0,
        to: parseInt(bind.to, 10) || 0,
        text: bind.text || ''
      });
      if (!range) return false;
      var rect = _rangeRect(boxes, range);
      if (!rect) return false;

      var layer = (typeof ensurePageLayer === 'function')
        ? ensurePageLayer(pw, 'pgbind-layer') : null;
      if (!layer) return false;
      pw.appendChild(layer);   // 排在 char-layer 之后，卡片能接到点击

      // 同一处已经有卡就替换，别叠罗汉
      var key = 'b' + range.from + '_' + range.to;
      var old = layer.querySelector('[data-bindkey="' + key + '"]');
      if (old) { try { old.__bwBindTeardown && old.__bwBindTeardown(); } catch (e) {} old.remove(); }

      var el = document.createElement('div');
      el.className = 'pgbind-card';
      el.dataset.bindkey = key;
      // 挂在锚下方一点；宽度跟着锚走但有下限，免得钉在一个字上时挤成一条缝。
      var w = Math.max(rect.x1 - rect.x0, 180);
      el.style.cssText =
        'position:absolute;left:' + rect.x0 + 'px;top:' + (rect.y1 + 4) + 'px;' +
        'width:' + w + 'px;z-index:6';
      el.innerHTML = '<div class="pgbind-hd">' +
        (window.RC && RC.esc ? RC.esc(payload.label || '卡片') : (payload.label || '卡片')) +
        '</div><div class="pgbind-bd">' +
        (payload.isHtml ? (payload.raw || '') :
          (window.RC && RC.esc ? RC.esc(payload.text || '') : (payload.text || ''))) +
        '</div>';
      layer.appendChild(el);
      _armBindCard(el);
      return true;
    } catch (e) {
      try { console.warn('[bind] __pageBindCard 失败', e); } catch (e2) {}
      return false;
    }
  };

  /// 绑不上的卡记下来，等那一页真的渲染出来再接回去。
  window.__pageBindDefer = function (bind, payload, card) {
    _pageBindPending.push({ bind: bind, payload: payload, card: card });
  };

  window.__pageBindRetry = function (pageNum) {
    if (!_pageBindPending.length) return;
    var rest = [];
    _pageBindPending.forEach(function (item) {
      if (parseInt(item.bind.page, 10) !== parseInt(pageNum, 10)) { rest.push(item); return; }
      var ok = false;
      try { ok = window.__pageBindCard(item.bind, item.payload); } catch (e) {}
      if (!ok) { rest.push(item); return; }
      // 补上了就把浮层那份关掉，否则同一内容两处并存。
      try {
        if (item.card && window.__vcCardClose) window.__vcCardClose(item.card);
      } catch (e) {}
    });
    _pageBindPending = rest;
  };
})();
