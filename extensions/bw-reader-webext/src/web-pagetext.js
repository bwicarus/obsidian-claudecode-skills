// 普通网页的 `reader_page_text`：给 AI 一份**带块编号和区域标签**的页面视图。
//
// 用户 2026-08-23：「我希望网页也可以用现在的这种 markdown 框的方式为 ai
// 区分正文，边栏等块区」。
//
// 形状与 App 版 `_nativeReaderPageText` 完全同构 —— `{ok, text, segments}` ——
// 这样上游（rc-computer-voice 的 query 表、C# 的 reader_page_text 工具、
// AI 的用法说明）**一处都不用为网页开分支**。
//
// ⚠ 两套坐标不能混，这是去掉 ANCHOR_MAP 时用一整轮换来的教训：
//   · `text` 是**排版投影**（Markdown），它自己的字符位置**不能**当 bind 下标；
//   · `segments[].from/to` 才是真坐标（web-textlayer 的字符层，闭区间语义
//     与 PDF 的 pageChars 一致）。
//   所以块编号 `[NN]` 只用来让人和 AI 互相指认"第几块"，钉卡片必须走 segments。
(function () {
  'use strict';
  if (window.__bwPwaProviderOnly || window.__bwPwaBridge || window.__bwWebPageText) return;

  var TL = window.__bwWebTextLayer;
  if (!TL) return;

  var MAX_TEXT = 1500;          // 与 App 版同一上限，避免两个表面给 AI 的量不同
  var MAX_SEGMENTS = 400;       // 同上
  var MAX_BLOCKS = 200;
  var MAX_MATCHES = 8;          // 超过这个数说明这句话根本不足以定位

  // ⚠ 必须包含 div/section/article。
  //
  //   2026-08-23 审计抓到的 high：原来只有 p/li/h*/td 那些语义标签，
  //   而 x.com 的推文正文是 `<div data-testid="tweetText"><span>…</span></div>`，
  //   多数 React/SPA 应用和 Gmail 正文同理 —— 一个块都选不出来。
  //   而字符层是满的，所以"页面为空"的早退不会触发，函数一路返回
  //   `ok:true` + 空 text + `truncated:false`，等于**向 AI 谎称"整页就这些"**。
  //   AI 会据此下结论说这页没内容。这比报错糟得多。
  //
  //   加了 div 不会导致"每层包裹都出一块"：下面的 hasInner 只取最内层。
  //   ⚠ 还必须含 nav/header/footer/aside：真机实测发现 `<nav>HOME ABOUT</nav>`
  //   的文字**在字符层里，却不属于任何块** —— 于是 AI 根本看不见它，
  //   也就无从把它标成「导航」。而用户要的正是"为 ai 区分正文、边栏等块区"：
  //   看不见的东西没法区分。它们同时也是 CHROME_SEL 的成员，所以一旦成块
  //   就会被正确标上导航/页眉/页脚。
  var BLOCK_SEL =
    'p,li,blockquote,h1,h2,h3,h4,h5,h6,td,th,figcaption,pre,dd,dt,' +
    'div,section,article,nav,header,footer,aside,main';

  // 区域标签。判据全部来自 content.js 已有的那套（导出复用，不另写一份 ——
  // 两个分类器迟早会给出不同答案，而"两边各自都自洽"是最难查的一类错）。
  var CHROME_SEL =
    '[role=navigation],[role=banner],[role=contentinfo],[role=search],' +
    '[role=menu],[role=menubar],[role=toolbar],[role=tablist],' +
    '[aria-hidden="true"],nav,header,footer,aside';

  function articleRoot() {
    try {
      if (typeof window.__bwArticleRoot === 'function') return window.__bwArticleRoot();
    } catch (_) {}
    return document.body || null;
  }

  function regionOf(el, root) {
    try {
      if (el.closest(CHROME_SEL)) {
        var chrome = el.closest(CHROME_SEL);
        var tag = (chrome.tagName || '').toLowerCase();
        if (tag === 'nav' || chrome.getAttribute('role') === 'navigation') return '导航';
        if (tag === 'header' || chrome.getAttribute('role') === 'banner') return '页眉';
        if (tag === 'footer' || chrome.getAttribute('role') === 'contentinfo') return '页脚';
        return '边栏';
      }
      if (root && root !== document.body && root.contains(el)) return '正文';
      if (root && root === document.body) return '正文';
      return '其它';
    } catch (_) { return '其它'; }
  }

  // 块前缀沿用 Markdown 语义，让 AI 一眼看出层级。
  function prefixOf(el) {
    var tag = (el.tagName || '').toLowerCase();
    if (tag === 'h1') return '# ';
    if (tag === 'h2') return '## ';
    if (tag === 'h3') return '### ';
    if (tag === 'h4' || tag === 'h5' || tag === 'h6') return '#### ';
    if (tag === 'li' || tag === 'dd' || tag === 'dt') return '- ';
    if (tag === 'blockquote') return '> ';
    if (tag === 'pre') return '    ';
    if (tag === 'td' || tag === 'th') return '| ';
    return '';
  }

  /// 一次遍历算出每个块在字符层里的闭区间。
  ///
  /// ⚠ 原来是"对每个块遍历全部文本节点"，块数 × 节点数 —— 在一篇维基长条目上
  ///   是 200 × 15000 = 三百万次 contains 调用。改成对每个文本节点向上找最近的
  ///   块祖先，代价降到 节点数 × 层深。
  function blockRanges(els, idx) {
    var set = typeof Set === 'function' ? new Set(els) : null;
    var out = typeof Map === 'function' ? new Map() : null;
    if (!set || !out) return null;
    for (var i = 0; i < idx.nodes.length; i++) {
      var node = idx.nodes[i];
      var el = node.parentElement;
      var guard = 0;
      while (el && !set.has(el) && guard++ < 64) el = el.parentElement;
      if (!el || !set.has(el)) continue;
      var end = idx.starts[i] + (node.nodeValue ? node.nodeValue.length : 0);
      var rec = out.get(el);
      if (rec) rec.to = end;
      else out.set(el, { from: idx.starts[i], to: end });
    }
    return out;
  }

  // 覆盖率只算**非空白**字符：块与块之间的 HTML 缩进换行本来就不属于任何块，
  // 把它算成"AI 看不见的内容"会让这个信号一直停在 0.89 左右，从而失去意义 ——
  // 一个永远不到 1 的指标，真出问题时也没人会注意到。
  function nonWs(s) { return String(s || '').replace(/\s/g, '').length; }

  /// 按 `contains` 把整页收窄成"你要的那几条"。
  ///
  /// 为什么要有它（用户 2026-08-23 一问点破的）：旧的内联 ANCHOR_MAP 是
  /// `segments:[[from,to,text] x 370]`、5718 字符 —— 而 `reader_page_text`
  /// 返回的**是同一份东西**。删掉内联表只解决了"每轮重发"，没解决"真要钉的
  /// 那一轮仍然吃掉几千字"。AI 明明知道自己要钉哪句话，不该整页拉回来自己找。
  ///
  /// ⚠ 命中多处时**必须报出来**。只回第一处会静默锚到用户没在看的那一段；
  ///   而"这句话在本页出现了几次"只有这里知道，AI 无从自己判断。
  function narrow(idx, segments, needle) {
    var text = idx.text;
    var matches = [];
    var at = 0;
    while (matches.length < MAX_MATCHES) {
      var hit = text.indexOf(needle, at);
      if (hit < 0) break;
      matches.push({ from: hit, to: hit + needle.length });
      at = hit + Math.max(1, needle.length);
    }
    if (!matches.length) return { matches: [], matchCount: 0, segments: [] };
    // 只留与命中区间相交的 segment，AI 据此拼 from/to 仍走原来那套。
    var keep = [];
    for (var i = 0; i < segments.length; i++) {
      var seg = segments[i];
      for (var m = 0; m < matches.length; m++) {
        if (seg.to >= matches[m].from && seg.from <= matches[m].to) {
          keep.push(seg);
          break;
        }
      }
    }
    return { matches: matches, matchCount: matches.length, segments: keep };
  }

  function read(params) {
    var idx = TL.build();
    if (!idx.text.length) {
      return { ok: false, code: 'BW_PAGE_TEXT_EMPTY', message: '页面还没有可读文字', retryable: true };
    }
    var root = articleRoot();
    var els = [];
    try { els = Array.prototype.slice.call(document.querySelectorAll(BLOCK_SEL)); }
    catch (_) { els = []; }

    // 嵌套块只取最内层：<li><p>…</p></li> 取 p，否则同一段文字会出现两次
    // 而且两条区间互相包含，AI 无从选择。
    var leaves = [];
    for (var i = 0; i < els.length; i++) {
      var hasInner = false;
      try { hasInner = !!els[i].querySelector(BLOCK_SEL); } catch (_) {}
      if (!hasInner) leaves.push(els[i]);
    }

    var spans = blockRanges(leaves, idx);
    var lines = [];
    var segments = [];
    var n = 0;
    var covered = 0;
    var truncated = !!idx.truncated;

    for (var j = 0; j < leaves.length && n < MAX_BLOCKS; j++) {
      var el = leaves[j];
      var span = spans && spans.get(el);
      if (!span || !(span.to > span.from)) continue;
      var raw = idx.text.slice(span.from, span.to).replace(/\s+/g, ' ').trim();
      if (!raw) continue;
      n += 1;
      covered += nonWs(raw);
      var no = (n < 10 ? '0' : '') + n;
      var region = regionOf(el, root);
      lines.push('[' + no + '] ' + region + ' ' + prefixOf(el) + raw.slice(0, 200));
      if (segments.length < MAX_SEGMENTS) {
        segments.push({
          from: span.from, to: span.to,
          text: raw.slice(0, 120),
          block: n, region: region
        });
      }
    }

    // ⚠ 字符层不空却一个块都没认出来 —— **绝不能返回"成功且空"**。
    //   那等于告诉 AI"整页就这些"，而 AI 会据此下结论。宁可给一份没有块结构的
    //   降级视图，也要让它拿到真实内容，并明确说出这是降级。
    //   见 references/silent-failure-lessons.md。
    var degraded = false;
    if (!n) {
      degraded = true;
      var flat = idx.text.replace(/\s+/g, ' ').trim();
      lines.push('[01] 其它 ' + flat.slice(0, MAX_TEXT));
      segments.push({
        from: 0, to: idx.text.length,
        text: flat.slice(0, 120), block: 1, region: '其它'
      });
    }

    var text = lines.join('\n');
    // 收窄：AI 已经知道要钉哪句话时，不该把整页几百条都发回去。
    var needle = String((params && params.contains) || '').trim();
    var narrowed = null;
    if (needle) {
      narrowed = narrow(idx, segments, needle);
      segments = narrowed.segments;
    }
    if (text.length > MAX_TEXT) { text = text.slice(0, MAX_TEXT); truncated = true; }
    if (n >= MAX_BLOCKS || segments.length >= MAX_SEGMENTS) truncated = true;

    return {
      ok: true,
      text: text,
      segments: segments,
      // 明确说出"截断了"。省略的话 AI 会把"只有这些"当成"全部就这些"，
      // 然后据此下结论 —— 见 references/silent-failure-lessons.md。
      truncated: truncated,
      // 这一页没能分出块结构，上面那条是整页平铺的降级视图。
      degraded: degraded,
      // 块一共盖住了字符层的多大比例（0~1）。
      // ⚠ 没盖住的部分是"确实存在但 AI 看不见"的文字 —— 不报出来的话，
      //   AI 会把「我看到的这些」当成「页面就这些」。真机实测就抓到过：
      //   <nav> 的文字在层里却不属于任何块，静默消失。
      coverage: (function () {
        var total = nonWs(idx.text);
        if (!total) return 0;
        return Math.round((degraded ? total : covered) / total * 100) / 100;
      })(),
      // 这份字符层的身份。AI 把它原样放进 bind.rev，页面变了就能察觉。
      rev: idx.rev,
      page: 1,
      // contains 给了才有。matchCount > 1 = 这句话在本页出现多次，
      // 光凭它定位不了 —— AI 应该把引用加长，而不是猜用户指的是哪一处。
      matches: narrowed ? narrowed.matches : undefined,
      matchCount: narrowed ? narrowed.matchCount : undefined
    };
  }

  /// 第 n 块（正文里印的 [NN]）在字符层里的闭区间。
  /// bind 带 block 时用它把按文本找的范围锁住。
  ///
  /// ⚠ 必须与 read() 用**同一套**块划分，否则助手看到的 [03] 和这里算出的
  ///   第 3 块不是同一块 —— 两边各自都自洽，是最难查的一类错。
  ///   所以这里直接复用 read()，不另写一份遍历。
  function blockRange(n) {
    n = parseInt(n, 10);
    if (!(n >= 1)) return null;
    var out = read({});
    if (!out || !out.ok) return null;
    for (var i = 0; i < out.segments.length; i++) {
      if (out.segments[i].block === n) {
        return { from: out.segments[i].from, to: out.segments[i].to };
      }
    }
    return null;
  }

  window.__bwWebPageText = { read: read, blockRange: blockRange };
})();
