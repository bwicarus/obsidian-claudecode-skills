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
  var DEFAULT_TONE = '#64748b';
  var marks = Object.create(null);     // key → {badge, color, range, at}
  var css = null;                      // 角标样式：在 __bwHead（随 pin 影子树镜像）
  var hlCss = null;                    // ::highlight 规则：必须在 document.head
  var hlByColor = Object.create(null);  // color → Highlight（每色一个，规则只写一次）

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

  /// ⚠ `::highlight()` 的规则**必须注进主文档的 head**，不能进影子树。
  ///
  /// 2026-08-23 审计抓到的 high：原来这条规则跟角标样式共用一个 style，
  /// 而那个 style 挂在 `window.__bwHead` —— 也就是 `#bw-reader-host` 影子树里的
  /// `#bw-head`（facade.js 无条件这么设，所以 `|| document.head` 是死代码）。
  /// 被高亮的文本节点在主文档树里，影子树的样式规则匹配不到它；自定义 highlight
  /// 又没有任何默认样式，于是**一个像素都不画**，而 `CSS.highlights.set` 照常成功、
  /// 函数一路返回 ok:true。用户看到的是"角标出来了、卡也点得开，就是不知道钉在哪句话上"，
  /// 链路上没有一处能看出失败。
  ///
  /// 仓库里两处已在真机跑过的同类实现（web-highlights.js、web-immersive.js）
  /// 都是注进 document.head 的。角标样式则相反 —— 它在 pinRoot 里，
  /// 靠 facade 的 syncPinStyles 从 __bwHead 镜像过去，所以两者必须分开放。
  function ensureHlCss() {
    if (hlCss) return hlCss;
    hlCss = document.createElement('style');
    hlCss.id = 'bw-web-bind-highlight-css';
    (document.head || document.documentElement).appendChild(hlCss);
    return hlCss;
  }

  function pinRoot() { return window.__bwPinRoot || null; }

  function toneColor(opts) {
    var t = String((opts && (opts.tone || opts.color)) || '');
    var m = t.match(/#[0-9a-fA-F]{3,8}/);
    return m ? m[0] : DEFAULT_TONE;
  }

  function hlNameFor(color) {
    return 'bwbind_' + color.replace(/[^A-Za-z0-9]/g, '');
  }

  /// 每种颜色一个 Highlight 对象、一条 CSS 规则。
  ///
  /// ⚠ 原来是"每个标记一条规则、`style.textContent +=` 追加、从不回收"：
  ///   clearMark 只删 Highlight 条目和角标，规则永远留着；而 __pageBindCard
  ///   自己先 clearMark 再 paintRange，同一条规则被反复追加。加上便签侧由
  ///   MutationObserver 每次 DOM 变动就重挂一遍，样式表会无界膨胀，
  ///   追加代价随表长二次增长（实测 0.04ms → 4.19ms/次）。
  ///   改成按颜色分桶后，规则数等于用到的颜色数，增删只动 Highlight 里的 range。
  function highlightFor(color) {
    var name = hlNameFor(color);
    if (hlByColor[name]) return hlByColor[name];
    if (!(window.CSS && CSS.highlights && window.Highlight)) return null;
    var h;
    try { h = new Highlight(); } catch (_) { return null; }
    try { CSS.highlights.set(name, h); } catch (_) { return null; }
    ensureHlCss().textContent +=
      '::highlight(' + name + '){text-decoration:underline wavy ' + color +
      ' 1.5px;text-underline-offset:3px}';
    hlByColor[name] = h;
    return h;
  }

  function paintRange(range, color) {
    var h = highlightFor(color);
    if (!h) return false;
    // 没有 Highlight API 就不画描边 —— **绝不退回 surroundContents**，
    // 那会往正文流里插节点，把字符层整体推移。角标仍然有，卡片照样点得开。
    try { h.add(range); return true; } catch (_) { return false; }
  }

  function unpaintRange(range, color) {
    var h = hlByColor[hlNameFor(color || DEFAULT_TONE)];
    if (!h || !range) return;
    try { h.delete(range); } catch (_) {}
  }

  function clearMark(key) {
    var rec = marks[key];
    if (!rec) return;
    unpaintRange(rec.range, rec.color);
    try { rec.badge && rec.badge.remove(); } catch (_) {}
    delete marks[key];
  }

  function measure(range) {
    try {
      var r = range.getBoundingClientRect();
      if (!r || (!r.width && !r.height)) return null;
      return r;
    } catch (_) { return null; }
  }

  function applyBadgePos(badge, rect) {
    badge.style.left = (window.scrollX + rect.right + 2) + 'px';
    badge.style.top = (window.scrollY + rect.top - 6) + 'px';
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
    // 只构建一次索引，locate 复用它 —— 原来 snapshot()+locate() 是两遍
    // 遍历加两遍哈希（实测 ~220ms/卡）。
    var idx = TL.build();
    if (!idx.text.length) return { ok: false, why: 'no-char-layer' };
    // 带了块号就把搜索范围锁在那一块里。块区间由 page-text 用**同一套**
    // 块划分给出，保证助手看到的 [NN] 与这里算的是同一块。
    var scope = null;
    if (bind.block && window.__bwWebPageText &&
        typeof window.__bwWebPageText.blockRange === 'function') {
      try { scope = window.__bwWebPageText.blockRange(bind.block); } catch (_) {}
    }
    var hit = null;
    try { hit = TL.locate(bind, idx, scope); } catch (e) {
      return { ok: false, why: 'exception', detail: { name: (e && e.name) || '' } };
    }
    if (!hit) return { ok: false, why: 'range-unresolved' };
    return { ok: true, range: hit.range, how: hit.how };
  }

  function markKey(bind, uid) {
    var clean = uid ? String(uid).replace(/[^A-Za-z0-9_-]/g, '') : '';
    if (clean) return 'u' + clean;
    return 'p1b' + (parseInt(bind && bind.from, 10) || 0) +
           '_' + (parseInt(bind && bind.to, 10) || 0);
  }

  window.__pageBindCard = function (bind, opts) {
    opts = opts || {};
    var key = markKey(bind, opts.uid);
    var g;
    try { g = resolve(bind); } catch (e) {
      return { ok: false, why: 'exception', detail: { name: (e && e.name) || '' } };
    }
    if (!g.ok) { clearMark(key); return g; }

    // 身份来自调用方（便签传 note.id / AI 传 cid），不用区间当 key ——
    // 否则同一个词上的第二张卡会把第一张的标记抹掉，而第一张此时已被
    // 上层设成 display:none，从此看不见也点不开。
    clearMark(key);

    var root = pinRoot();
    if (!root) return { ok: false, why: 'no-layer' };

    var rect = measure(g.range);
    if (!rect) return { ok: false, why: 'no-rect' };

    var color = toneColor(opts);
    paintRange(g.range, color);

    var badge = document.createElement('span');
    badge.className = 'bw-bindmark-n';
    badge.dataset.bindkey = key;          // rc-stickynote 靠这个加 .on
    badge.style.background = color;
    badge.title = String(opts.label || '卡片');
    applyBadgePos(badge, rect);
    badge.addEventListener('click', function (ev) {
      ev.stopPropagation();
      if (typeof opts.onToggle === 'function') {
        opts.onToggle({ source: badge, bindKey: key, category: opts.category || 'general' });
      }
    });
    root.appendChild(badge);
    ensureCss();

    marks[key] = {
      badge: badge, color: color, range: g.range,
      at: parseInt(bind.from, 10) || 0
    };
    renumber();
    return { ok: true, key: key, how: g.how };
  };

  /// 撤销一个锚定标记。
  ///
  /// ⚠ 2026-08-23 审计抓到的 high：这个函数**原本不存在**。
  ///   rc-stickynote 有三处撤销点都写成 `if (_b && window.__pageBindRemove)`，
  ///   函数不在就**整句跳过、不报错**。结果是便签 DOM 被删了，而角标和
  ///   Highlight 条目永远留在页面上，点它调用的是已经死掉的回调，
  ///   而且后续序号全部错位。
  window.__pageBindRemove = function (bind, uid) {
    var key = markKey(bind, uid);
    var had = !!marks[key];
    clearMark(key);
    renumber();
    return { ok: true, removed: had };
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
    var rect = measure(g.range);
    if (!rect) return Promise.resolve({ ok: false, why: 'no-rect' });
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
          noteId: result.noteId || '', persisted: true,
          // 如实说出走的哪条：exact / by-block / by-text /
          // by-text-block-missed（带了块号却没在块里找到 —— 块对不上了）。
          // 不报出来的话，降级是完全沉默的。
          how: g.how
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

  // ── 角标跟随 ────────────────────────────────────────────────────
  //
  // 锚本身不动（它在字符层坐标里），只有屏幕位置要重算，所以这里不重解析 bind。
  //
  // ⚠ 两个实测问题一起修：
  //   · **布局抖动**：原来是"读一个 rect → 写一个 style → 再读下一个"，
  //     每次写都让下一次读强制同步布局。实测 9.3ms/角标/帧，2 张卡就爆帧预算。
  //     改成先把所有 rect 读完、再统一写。
  //   · **只挂 scroll/resize 不够**：译页是异步分批到达的，每段插入译文都会把
  //     后面的内容整体下移，期间既没有 scroll 也没有 resize —— 角标会停在旧坐标
  //     上指向别的段落。所以另外订阅字符层的 DOM 变化。
  var raf = 0;
  function reposition() {
    if (raf) return;
    raf = requestAnimationFrame(function () {
      raf = 0;
      var list = [], i;
      for (var k in marks) if (marks[k] && marks[k].badge && marks[k].range) list.push(marks[k]);
      var rects = new Array(list.length);
      for (i = 0; i < list.length; i++) rects[i] = measure(list[i].range);   // 只读
      for (i = 0; i < list.length; i++) {                                    // 再写
        if (rects[i]) applyBadgePos(list[i].badge, rects[i]);
      }
    });
  }
  window.addEventListener('scroll', reposition, { passive: true });
  window.addEventListener('resize', reposition, { passive: true });
  if (typeof TL.onChange === 'function') TL.onChange(reposition);

  window.__bwWebBind = {
    resolve: resolve,
    clearMark: clearMark,
    currentSelection: currentSelection,
    _marks: marks
  };
})();
