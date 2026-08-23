// 普通网页的**卡片锚定**：让"加入卡片"和"锁定元素"在网页上跟书里一样能用。
//
// 用户 2026-08-23：「新版的加入卡片和锁定元素的功能要确保网页端也可以使用」。
//
// 设计上只做"中间层适配"，不另造一套上层建筑（`references/unified-control-layer.md`
// 的铁律）：
//   · 锚仍是 `page-chars`，**不新增 bind kind** —— 那份白名单在链路上有 17 份
//     副本（8 源 + 5 生成 + 4 说明），少改一处就是一次静默失败。网页的字符层
//     由 web-textlayer.js 提供，page 恒为 1。
//   · 落库仍走共享层 `RC.stickynote.persistBoundCard` —— 仓库写入、幂等、
//     tombstone 全是原来那份，这里只负责"把 bind 解成屏幕点"。
//
// ⚠ 一条网页独有的硬约束：**标记不能往正文流里插 DOM**。
//   插一个 <mark> 或角标 <span> 就会改变字符层，于是所有已存在的锚点
//   当场偏移 —— 而且偏移的是**别的卡**，查起来像"随机错位"。
//   所以：描边走 CSS Custom Highlight API（零 DOM 变更），角标放进
//   `#bw-pin-root`（那个容器本来就被字符层排除在外）。
(function () {
  'use strict';
  if (window.__bwPwaProviderOnly || window.__bwPwaBridge || window.__bwWebBind) return;

  var TL = window.__bwWebTextLayer;
  var RC = window.RC;
  if (!TL || !RC) return;

  var MAX_CTX = 320;
  var marks = Object.create(null);     // key → {badge, hlName, at, range}
  var css = null;

  function ensureCss() {
    if (css) return css;
    css = document.createElement('style');
    css.id = 'bw-web-bind-css';
    css.textContent =
      '.bw-bindmark-n{position:absolute;z-index:2147483000;font:600 10px/1.4 system-ui,sans-serif;' +
      'min-width:14px;height:14px;padding:0 3px;border-radius:7px;text-align:center;' +
      'color:#fff;background:#64748b;box-shadow:0 1px 3px rgba(0,0,0,.35);' +
      'cursor:pointer;user-select:none;pointer-events:auto}' +
      '.bw-bindmark-n.on{outline:2px solid currentColor;outline-offset:1px}';
    (window.__bwHead || document.head).appendChild(css);
    return css;
  }

  function pinRoot() { return window.__bwPinRoot || null; }

  function toneColor(opts) {
    var t = String((opts && (opts.tone || opts.color)) || '');
    var m = t.match(/#[0-9a-fA-F]{3,8}/);
    return m ? m[0] : '#64748b';
  }

  function paintRange(key, range, color) {
    var name = 'bwbind_' + key.replace(/[^A-Za-z0-9_-]/g, '');
    if (window.CSS && CSS.highlights && window.Highlight) {
      try {
        CSS.highlights.set(name, new Highlight(range));
        ensureCss().textContent +=
          '::highlight(' + name + '){text-decoration:underline wavy ' + color +
          ' 1.5px;text-underline-offset:3px}';
        return name;
      } catch (_) {}
    }
    // 没有 Highlight API 就不画描边 —— **绝不退回 surroundContents**，
    // 那会往正文流里插节点，把字符层整体推移。角标仍然有，卡片照样点得开。
    return '';
  }

  function clearMark(key) {
    var rec = marks[key];
    if (!rec) return;
    try { if (rec.hlName && window.CSS && CSS.highlights) CSS.highlights.delete(rec.hlName); } catch (_) {}
    try { rec.badge && rec.badge.remove(); } catch (_) {}
    delete marks[key];
  }

  function placeBadge(badge, range) {
    try {
      var r = range.getBoundingClientRect();
      if (!r || (!r.width && !r.height)) return false;
      badge.style.left = (window.scrollX + r.right + 2) + 'px';
      badge.style.top = (window.scrollY + r.top - 6) + 'px';
      return true;
    } catch (_) { return false; }
  }

  function renumber() {
    // 序号是它在**本页**的次序，不是身份 —— 跟书里同一套语义，
    // 所以人和 AI 都能说「把第 3 个删掉」。
    var list = [];
    for (var k in marks) if (marks[k] && marks[k].badge) list.push(marks[k]);
    list.sort(function (a, b) { return (a.at || 0) - (b.at || 0); });
    for (var i = 0; i < list.length; i++) list[i].badge.textContent = String(i + 1);
  }

  /// 把 bind 解成 DOM Range。失败原因分两类，含义与 34-bindcard 对齐：
  ///   · 'no-char-layer' = 暂时性（页面还没稳定），上层继续藏着等重试
  ///   · 其它            = 这页当前状态下就是不成，上层退回浮层显示
  function resolve(bind) {
    if (!bind || bind.kind !== 'page-chars') return { ok: false, why: 'not-page-chars' };
    var page = parseInt(bind.page, 10);
    if (!(page > 0)) return { ok: false, why: 'bad-page' };
    if (page !== 1) return { ok: false, why: 'bad-page' };   // 网页整篇即一页
    var snap = TL.snapshot();
    if (!snap.length) return { ok: false, why: 'no-char-layer' };
    var hit = null;
    try { hit = TL.locate(bind); } catch (e) {
      return { ok: false, why: 'exception', detail: { name: (e && e.name) || '' } };
    }
    if (!hit) return { ok: false, why: 'range-unresolved' };
    return { ok: true, range: hit.range, how: hit.how };
  }

  window.__pageBindCard = function (bind, opts) {
    opts = opts || {};
    var uid = opts.uid ? String(opts.uid).replace(/[^A-Za-z0-9_-]/g, '') : '';
    var g;
    try { g = resolve(bind); } catch (e) {
      return { ok: false, why: 'exception', detail: { name: (e && e.name) || '' } };
    }
    if (!g.ok) { if (uid) clearMark('u' + uid); return g; }

    // 身份来自调用方（便签传 note.id / AI 传 cid），不用区间当 key ——
    // 否则同一个词上的第二张卡会把第一张的标记抹掉，而第一张此时已被
    // 上层设成 display:none，从此看不见也点不开。
    var lo = parseInt(bind.from, 10) || 0;
    var key = uid ? ('u' + uid) : ('p1b' + lo + '_' + (parseInt(bind.to, 10) || 0));
    clearMark(key);

    var root = pinRoot();
    if (!root) return { ok: false, why: 'no-layer' };

    var color = toneColor(opts);
    var hlName = paintRange(key, g.range, color);

    var badge = document.createElement('span');
    badge.className = 'bw-bindmark-n';
    badge.dataset.bindkey = key;          // rc-stickynote 靠这个加 .on
    badge.style.background = color;
    badge.title = String(opts.label || '卡片');
    if (!placeBadge(badge, g.range)) {
      try { if (hlName && window.CSS && CSS.highlights) CSS.highlights.delete(hlName); } catch (_) {}
      return { ok: false, why: 'no-rect' };
    }
    badge.addEventListener('click', function (ev) {
      ev.stopPropagation();
      if (typeof opts.onToggle === 'function') {
        opts.onToggle({ source: badge, bindKey: key, category: opts.category || 'general' });
      }
    });
    root.appendChild(badge);
    ensureCss();

    marks[key] = { badge: badge, hlName: hlName, at: lo, range: g.range };
    renumber();
    return { ok: true, key: key, how: g.how };
  };

  /// AI page-chars 卡在网页上的唯一持久化入口。
  /// 只有共享层便签仓真的提交之后才 resolve ok:true —— 绝不先画标记再谎报 bound。
  window.__pageBindPersist = function (bind, payload) {
    var g;
    try { g = resolve(bind); } catch (e) {
      return Promise.resolve({ ok: false, why: 'exception', detail: { name: (e && e.name) || '' } });
    }
    if (!g.ok) return Promise.resolve(g);
    if (!(RC.stickynote && typeof RC.stickynote.persistBoundCard === 'function')) {
      return Promise.resolve({ ok: false, why: 'persistence-unavailable' });
    }
    var rect = null;
    try { rect = g.range.getBoundingClientRect(); } catch (_) { rect = null; }
    if (!rect || (!rect.width && !rect.height)) {
      return Promise.resolve({ ok: false, why: 'no-rect' });
    }
    payload = payload || {};
    var normalized = {
      uid: payload.uid || '', label: payload.label || '卡片',
      text: payload.text || '', raw: payload.raw || '', isHtml: !!payload.isHtml,
      icon: payload.icon || '', category: payload.category || 'general',
      tone: toneColor(payload)
    };
    var point = { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
    return Promise.resolve(RC.stickynote.persistBoundCard(bind, normalized, point))
      .then(function (result) {
        if (!result || result.ok !== true) return result || { ok: false, why: 'persistence-failed' };
        return {
          ok: true, page: 1,
          from: parseInt(bind.from, 10) || 0, to: parseInt(bind.to, 10) || 0,
          noteId: result.noteId || '', persisted: true
        };
      }, function (error) {
        return { ok: false, why: 'persistence-failed',
                 detail: { name: (error && error.name) || '' } };
      });
  };

  // ── 锁定元素：把当前 DOM 选区折成 page-chars 锚 ─────────────────
  //   rc-stickynote.currentLockedPageBind 已经接受 kind==='page-chars'
  //   （vendor/rc-stickynote.js:1391），所以共享层**一行都不用改**。
  function currentSelection() {
    var sel = null;
    try { sel = window.getSelection(); } catch (_) { return null; }
    if (!sel || sel.rangeCount === 0 || sel.isCollapsed) return null;
    var range = null;
    try { range = sel.getRangeAt(0); } catch (_) { return null; }
    var host = window.__bwReaderHost;
    // 扩展自身 UI 里的选区不算 —— 否则在侧栏里选一段字会被当成正文锚。
    try {
      if (host && host.contains && host.contains(range.commonAncestorContainer)) return null;
      var pr = pinRoot();
      if (pr && pr.contains && pr.contains(range.commonAncestorContainer)) return null;
    } catch (_) {}
    var text = String(sel.toString() || '').trim();
    if (!text) return null;
    var anchor = null;
    try { anchor = TL.rangeToBind(range); } catch (_) { anchor = null; }
    if (!anchor) return null;
    var ctx = '';
    try {
      var blk = range.startContainer.nodeType === 3
        ? range.startContainer.parentElement : range.startContainer;
      blk = blk && blk.closest ? blk.closest('p,li,td,blockquote,section,h1,h2,h3,h4,div') : null;
      ctx = String((blk && blk.textContent) || '').trim().slice(0, MAX_CTX);
    } catch (_) {}
    return { text: text, anchor: anchor, context: ctx, ctx: ctx, sentence: ctx };
  }

  window.__bwSelectionController = { current: currentSelection };

  // 页面重排/滚动后角标要跟着走。锚本身不动（它在字符层坐标里），
  // 只有屏幕位置要重算 —— 所以这里不重解析 bind，只挪角标。
  var raf = 0;
  function reposition() {
    if (raf) return;
    raf = requestAnimationFrame(function () {
      raf = 0;
      for (var k in marks) {
        var m = marks[k];
        if (m && m.badge && m.range) placeBadge(m.badge, m.range);
      }
    });
  }
  window.addEventListener('scroll', reposition, { passive: true });
  window.addEventListener('resize', reposition, { passive: true });

  window.__bwWebBind = {
    resolve: resolve,
    clearMark: clearMark,
    currentSelection: currentSelection,
    _marks: marks
  };
})();
