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
  var BLOCK_SEL = 'p,li,blockquote,h1,h2,h3,h4,h5,h6,td,th,figcaption,pre,dd,dt';

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

  // 块在字符层里的闭区间。用块内首/末文本节点定位 —— 与选区走的是同一份
  // 索引，所以 AI 看到的区间可以直接拿去 bind。
  function rangeOfBlock(el, idx) {
    var first = -1, last = -1;
    for (var i = 0; i < idx.nodes.length; i++) {
      var n = idx.nodes[i];
      var inside = false;
      try { inside = el.contains(n); } catch (_) { inside = false; }
      if (!inside) { if (first >= 0) break; continue; }
      if (first < 0) first = idx.starts[i];
      last = idx.starts[i] + n.nodeValue.length;
    }
    return first >= 0 && last > first ? { from: first, to: last } : null;
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

    var lines = [];
    var segments = [];
    var n = 0;
    var truncated = false;
    for (var i = 0; i < els.length && n < MAX_BLOCKS; i++) {
      var el = els[i];
      // 嵌套块只取最内层：<li><p>…</p></li> 取 p，否则同一段文字会出现两次
      // 而且两条区间互相包含，AI 无从选择。
      var hasInner = false;
      try { hasInner = !!el.querySelector(BLOCK_SEL); } catch (_) {}
      if (hasInner) continue;
      var span = rangeOfBlock(el, idx);
      if (!span) continue;
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
      // 这份字符层的身份。AI 把它原样放进 bind.rev，页面变了就能察觉。
      rev: TL.snapshot().rev,
      page: 1
    };
  }

  window.__bwWebPageText = { read: read };
})();
