// 普通网页的**字符层**：让网页拥有一份跟 PDF `page-chars` 同构的坐标系。
//
// 为什么不新造一种 bind kind：
//   `page-chars` 的锚是「序号 + 文本 + revision」，设计理由写在
//   `_server_deploy/reader_card_contract.py` 的注释里 —— 只用文本会因为
//   同一个词在页内重复而找不准，只用序号会因为换了一份字符层而全错，
//   所以两个都带。网页完全适用同一套推理：DOM 会变（SPA 重渲染、广告插入），
//   变了就等于"换了一份字符层"。
//   于是网页只要能给出一份稳定的字符层，`page-chars` **一个字都不用改**
//   就能用在网页上 —— 而白名单在这条链上有 17 份副本（8 源 + 5 生成 +
//   4 说明），少改一处就是一次静默失败。不改它是本设计最重要的一条。
//
// 页码恒为 1：网页没有分页。契约要求 `page >= 1`，给 1 是合法且诚实的
//   ——"整篇就是一页"，不是编造出来的页号。
//
// ⚠ 这一层走**整个 body**，不是正文根。正文/边栏的区分是**块上的标签**，
//   不是另一份文本。否则同一个词在"全页坐标"和"正文坐标"里下标不同，
//   AI 拿到的下标和钉卡时解析的下标就会各说各话 —— 那是最难查的一类错，
//   因为两边各自都自洽。
(function () {
  'use strict';
  if (window.__bwPwaProviderOnly || window.__bwPwaBridge || window.__bwWebTextLayer) return;

  var MAX_TEXT = 600000;      // 超大页只取前段，避免一次遍历卡住主线程
  var MAX_QUOTE = 200;        // 与 currentLockedPageBind 的 text.length 上限一致

  // ── 什么不算"正文字符" ───────────────────────────────────────────
  //
  // ⚠ 这份名单里最要紧的不是 script/style，而是**扩展自己插进正文流的东西**。
  //   2026-08-23 审计实测到两个 high：
  //     · 振假名（web-decorations.js 的 wrapTokens）往正文里插
  //       `<ruby>日本<rt>にほん</rt></ruby>` —— 不排除 `<rt>` 的话字符层会
  //       变成「日本にほん語ご」，此前钉的每一张卡当场按文本也找不回来。
  //     · 译页/沉浸翻译（web-immersive.js）把译文 append 进**同一个 `<p>` 内部**，
  //       带 `data-rc-tr="1"`（占位是 `data-rc-ph="1"`）。不排除的话
  //       AI 拿到的 segment 区间会横跨原文+译文，关掉翻译后这些锚永久失效。
  //
  // ⚠ 反过来同样要命：**绝不能排除 `.rc-tr-src`**。replace 样式下原文被裹进
  //   `.rc-tr-src.rc-tr-src-hidden` 收起来（不删），它仍然是正文本身；
  //   排除它会让所有锚朝另一个方向全错。判据是"这段字是不是页面原本的正文"，
  //   不是"它现在看不看得见"。
  //
  // 与 web-highlights.js 的 textIndex() 保持一致 —— 两边必须一致，否则
  // "高亮看到的文字"和"卡片锚到的文字"是两篇不同的文章。
  var EXCLUDE_SEL =
    'script,style,noscript,textarea,input,select,option,' +
    '[contenteditable="true"],#bw-reader-host,#bw-pin-root,' +
    'rt,rp,' +                       // 注音读音不是正文（本扩展的和页面原生的都是）
    '[data-rc-tr],[data-rc-ph]';     // 译文与"翻译中…"占位

  function excluded(node) {
    var p = node.parentElement;
    if (!p) return true;
    return !!p.closest(EXCLUDE_SEL);
  }

  function rawBuild() {
    var nodes = [], starts = [], text = '';
    var rootNode = document.body || document.documentElement;
    if (!rootNode) return { nodes: nodes, starts: starts, text: '', truncated: false };
    var walker = document.createTreeWalker(rootNode, NodeFilter.SHOW_TEXT, {
      acceptNode: function (n) {
        return (excluded(n) || !n.nodeValue)
          ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
      }
    });
    var n, truncated = false;
    while ((n = walker.nextNode())) {
      if (text.length >= MAX_TEXT) { truncated = true; break; }
      starts.push(text.length);
      nodes.push(n);
      text += n.nodeValue;
    }
    return { nodes: nodes, starts: starts, text: text, truncated: truncated };
  }

  // ── 构建结果缓存 ────────────────────────────────────────────────
  //
  // 实测（Wikipedia「World War II」，15812 个文本节点）：一次 build() 约 95ms，
  // 一次 revisionOf() 约 15ms。而原来 resolve() 一张卡要走两遍 build + 两遍
  // revision ≈ 220ms —— 钉几张卡就是肉眼可见的卡顿。
  //
  // 用 MutationObserver 只置一个脏标记（不做任何解析，代价极低），
  // DOM 没动就直接复用。这同时解决了另一个问题：译页是异步分批到达的，
  // 期间既没有 scroll 也没有 resize，靠事件重定位的角标会停在旧坐标上；
  // 现在改由 DOM 变化驱动。
  var _cache = null;
  var _dirty = true;
  var _watchers = [];

  function markDirty() {
    if (_dirty) return;
    _dirty = true;
    for (var i = 0; i < _watchers.length; i++) {
      try { _watchers[i](); } catch (_) {}
    }
  }

  if (typeof MutationObserver === 'function') {
    try {
      new MutationObserver(markDirty).observe(
        document.documentElement || document,
        { childList: true, subtree: true, characterData: true }
      );
    } catch (_) {}
  }

  function build() {
    if (!_dirty && _cache) return _cache;
    _cache = rawBuild();
    _cache.rev = revisionOf(_cache.text);
    // 节点 → 下标，给 boundaryOffset 用；indexOf 在 1.5 万个节点上是 O(n)
    if (typeof Map === 'function') {
      var m = new Map();
      for (var i = 0; i < _cache.nodes.length; i++) m.set(_cache.nodes[i], i);
      _cache.index = m;
    }
    _dirty = false;
    return _cache;
  }

  // revision：这份字符层的身份。DOM 变了它就变，于是 bind 里的下标自动失效
  // 并退回按文本重找 —— 这正是 page-chars 设计里 `rev` 的作用。
  // 不用加密哈希：它只需要"变了能看出来",不需要抗碰撞。
  function revisionOf(text) {
    var h1 = 0x811c9dc5, h2 = 0x01000193;
    for (var i = 0; i < text.length; i++) {
      var c = text.charCodeAt(i);
      h1 = (h1 ^ c) >>> 0; h1 = (h1 * 16777619) >>> 0;
      h2 = (h2 + c * (i % 61 + 1)) >>> 0;
    }
    return 'w' + h1.toString(36) + h2.toString(36) + '-' + text.length;
  }

  function nodeIndex(idx, node) {
    if (idx.index && idx.index.has(node)) return idx.index.get(node);
    return idx.nodes.indexOf(node);
  }

  function pointAt(idx, pos) {
    // 二分：大页面上线性回扫会成为翻页时的卡顿源
    var lo = 0, hi = idx.starts.length - 1, hit = -1;
    while (lo <= hi) {
      var mid = (lo + hi) >> 1;
      if (idx.starts[mid] <= pos) { hit = mid; lo = mid + 1; }
      else hi = mid - 1;
    }
    if (hit < 0) return null;
    var node = idx.nodes[hit];
    var off = Math.max(0, Math.min(node.nodeValue.length, pos - idx.starts[hit]));
    return { node: node, off: off };
  }

  function rangeFor(idx, from, to) {
    var a = pointAt(idx, from), b = pointAt(idx, to);
    if (!a || !b) return null;
    try {
      var r = document.createRange();
      r.setStart(a.node, a.off);
      r.setEnd(b.node, b.off);
      return r;
    } catch (_) { return null; }
  }

  /// 把一个 Range 边界点 (container, offset) 折成字符层里的下标。
  ///
  /// ⚠ 这里是 2026-08-23 审计抓到的一个 high，写清楚免得再犯：
  ///   **真实浏览器里 Range 的边界容器经常不是文本节点。**
  ///   Ctrl+A 全选给出 startContainer=<body>、offset=0；
  ///   三击选段（最常见的"选中这一段"手势）的 endContainer 是段落的父元素；
  ///   跨段落拖选拖过一段末尾时 endContainer 同样是元素。
  ///   原来只做 `nodes.indexOf(container)`，这三种输入一律得 -1 → 返回 null
  ///   → 「锁定元素」对整类最常见的选区静默失效。
  ///   而且 -1 只是**恰好**挡住了更坏的情况：若容器是元素却拿到了下标，
  ///   `starts[i] + offset` 会把**子节点序号**当成字符偏移相加，得到一个
  ///   完全错位却看起来合法的区间。
  ///
  /// 元素容器用文档序二分定位：找到第一个"起点不在该边界之前"的文本节点，
  /// 边界就落在它的起始处。找不到说明边界在全文之后。
  function boundaryOffset(idx, container, offset) {
    if (!container) return -1;
    if (container.nodeType === 3) {
      var i = nodeIndex(idx, container);
      if (i >= 0) {
        var len = container.nodeValue ? container.nodeValue.length : 0;
        return idx.starts[i] + Math.max(0, Math.min(offset, len));
      }
      // 文本节点被排除在层外（script/译文/rt…）：落到下面的位置比较兜底
    }
    var probe;
    try {
      probe = document.createRange();
      probe.setStart(container, offset);
      probe.collapse(true);
    } catch (_) { return -1; }

    var lo = 0, hi = idx.nodes.length - 1, first = idx.nodes.length;
    while (lo <= hi) {
      var mid = (lo + hi) >> 1;
      var cmp;
      try { cmp = probe.comparePoint(idx.nodes[mid], 0); } catch (_) { cmp = 1; }
      // -1 = 该节点起点在边界之前；0/1 = 在边界处或之后
      if (cmp < 0) lo = mid + 1;
      else { first = mid; hi = mid - 1; }
    }
    return first < idx.nodes.length ? idx.starts[first] : idx.text.length;
  }

  // 把 DOM 选区折成 page-chars 锚。
  function rangeToBind(range) {
    if (!range) return null;
    var idx = build();
    var from = boundaryOffset(idx, range.startContainer, range.startOffset);
    var to = boundaryOffset(idx, range.endContainer, range.endOffset);
    if (from < 0 || to < 0 || !(to > from)) return null;
    var quote = idx.text.slice(from, to);
    if (!quote.trim()) return null;
    return {
      kind: 'page-chars',
      page: 1,
      from: from,
      to: to,
      text: quote.slice(0, MAX_QUOTE),
      rev: idx.rev
    };
  }

  // 把 page-chars 锚解回 DOM Range。
  //
  // 两级，跟 PDF 侧同一套推理：
  //   ① rev 对得上 → 直接用下标（精确）
  //   ② 对不上 → 按 text 重新找，**用原下标挑最接近的那一处**（消歧）
  // 第 ② 步正是"同一个词在页内重复出现"时唯一能分辨的依据。
  //
  // `prebuilt` 让调用方复用同一份索引，避免一次操作里重复构建。
  function locate(bind, prebuilt) {
    if (!bind || bind.kind !== 'page-chars') return null;
    var from = Number(bind.from), to = Number(bind.to);
    var idx = prebuilt || build();
    var quote = String(bind.text || '');

    if (bind.rev && bind.rev === idx.rev &&
        Number.isFinite(from) && Number.isFinite(to) && to > from &&
        to <= idx.text.length) {
      // rev 一致时仍然核对一次文本 —— rev 相同而内容不同只可能是哈希碰撞，
      // 但碰撞的代价是把卡钉在完全无关的位置上，核对一次很便宜。
      if (!quote || idx.text.slice(from, to).indexOf(quote.slice(0, 24)) === 0) {
        var exact = rangeFor(idx, from, to);
        if (exact) return { range: exact, how: 'exact' };
      }
    }

    if (!quote) return null;
    var best = -1, bestDist = Infinity, at = 0;
    for (;;) {
      var hit = idx.text.indexOf(quote, at);
      if (hit < 0) break;
      var dist = Number.isFinite(from) ? Math.abs(hit - from) : 0;
      if (dist < bestDist) { bestDist = dist; best = hit; }
      at = hit + Math.max(1, quote.length);
    }
    if (best < 0) return null;
    var found = rangeFor(idx, best, best + quote.length);
    return found ? { range: found, how: 'refound' } : null;
  }

  window.__bwWebTextLayer = {
    build: build,
    revision: function () { return build().rev; },
    rangeToBind: rangeToBind,
    boundaryOffset: boundaryOffset,
    locate: locate,
    // DOM 变化时回调。译页是异步分批到达的，期间没有 scroll/resize ——
    // 靠事件重定位的角标会停在旧坐标上指向别的段落，所以要由 DOM 变化驱动。
    onChange: function (cb) { if (typeof cb === 'function') _watchers.push(cb); },
    // 给 page-text 用：整篇纯文本 + 这份字符层的 revision
    snapshot: function () {
      var idx = build();
      return {
        text: idx.text, rev: idx.rev,
        length: idx.text.length, truncated: !!idx.truncated
      };
    }
  };
})();
