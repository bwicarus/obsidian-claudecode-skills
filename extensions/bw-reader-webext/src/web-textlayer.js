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

  // 与 web-highlights 的排除规则保持一致：不可见/不可选/扩展自身 UI 不计入。
  // 两边必须一致，否则"高亮看到的文字"和"卡片锚到的文字"是两篇不同的文章。
  function excluded(node) {
    var p = node.parentElement;
    if (!p) return true;
    return !!p.closest(
      'script,style,noscript,textarea,input,select,option,' +
      '[contenteditable="true"],#bw-reader-host,#bw-pin-root'
    );
  }

  function build() {
    var nodes = [], starts = [], text = '';
    var rootNode = document.body || document.documentElement;
    if (!rootNode) return { nodes: nodes, starts: starts, text: '' };
    var walker = document.createTreeWalker(rootNode, NodeFilter.SHOW_TEXT, {
      acceptNode: function (n) {
        return (excluded(n) || !n.nodeValue)
          ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT;
      }
    });
    var n;
    while ((n = walker.nextNode())) {
      if (text.length >= MAX_TEXT) break;
      starts.push(text.length);
      nodes.push(n);
      text += n.nodeValue;
    }
    return { nodes: nodes, starts: starts, text: text };
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

  // 把 DOM 选区折成 page-chars 锚。
  function rangeToBind(range) {
    if (!range) return null;
    var idx = build();
    var s = idx.nodes.indexOf(range.startContainer);
    var e = idx.nodes.indexOf(range.endContainer);
    if (s < 0 || e < 0) return null;
    var from = idx.starts[s] + range.startOffset;
    var to = idx.starts[e] + range.endOffset;
    if (!(to > from)) return null;
    var quote = idx.text.slice(from, to);
    if (!quote.trim()) return null;
    return {
      kind: 'page-chars',
      page: 1,
      from: from,
      to: to,
      text: quote.slice(0, MAX_QUOTE),
      rev: revisionOf(idx.text)
    };
  }

  // 把 page-chars 锚解回 DOM Range。
  //
  // 两级，跟 PDF 侧同一套推理：
  //   ① rev 对得上 → 直接用下标（精确）
  //   ② 对不上 → 按 text 重新找，**用原下标挑最接近的那一处**（消歧）
  // 第 ② 步正是"同一个词在页内重复出现"时唯一能分辨的依据。
  function locate(bind) {
    if (!bind || bind.kind !== 'page-chars') return null;
    var from = Number(bind.from), to = Number(bind.to);
    var idx = build();
    var quote = String(bind.text || '');

    if (bind.rev && bind.rev === revisionOf(idx.text) &&
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
    revision: function () { return revisionOf(build().text); },
    rangeToBind: rangeToBind,
    locate: locate,
    // 给 page-text 用：整篇纯文本 + 这份字符层的 revision
    snapshot: function () {
      var idx = build();
      return { text: idx.text, rev: revisionOf(idx.text), length: idx.text.length };
    }
  };
})();
