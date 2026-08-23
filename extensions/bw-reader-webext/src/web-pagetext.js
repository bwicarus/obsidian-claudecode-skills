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
  var BLOCK_SEL =
    'p,li,blockquote,h1,h2,h3,h4,h5,h6,td,th,figcaption,pre,dd,dt,' +
    'div,section,article';

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

  function read() {
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
    var truncated = !!idx.truncated;

    for (var j = 0; j < leaves.length && n < MAX_BLOCKS; j++) {
      var el = leaves[j];
      var span = spans && spans.get(el);
      if (!span || !(span.to > span.from)) continue;
      var raw = idx.text.slice(span.from, span.to).replace(/\s+/g, ' ').trim();
      if (!raw) continue;
      n += 1;
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
      // 这份字符层的身份。AI 把它原样放进 bind.rev，页面变了就能察觉。
      rev: idx.rev,
      page: 1
    };
  }

  window.__bwWebPageText = { read: read };
})();
