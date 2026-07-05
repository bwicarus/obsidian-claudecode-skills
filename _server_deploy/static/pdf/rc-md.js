/* rc-md.js — 共享 markdown + 数学渲染。零底座耦合(纯函数),最大最安全的共享点。
 * RC.md(s) 逐字搬自 PDF 阅读器 reader.src/21-misc-ai.js::md()(EPUB 的 renderMd 与之同构,本就该用同一份):
 *   先把数学公式整段抠出换占位符再跑 marked(否则 marked 把 $P(A_1)$ 里的 _ 当斜体/* 当强调/\ 当转义拆坏);
 *   CJK 与 markdown 强调标记紧贴时插零宽空格给 ** / * / ` 留边界;占位符 @@MJX{n}@@ 纯字母数字,marked 原样保留。
 * RC.typeset(el) 只对**已挂载**的容器 typesetPromise(时机由调用点控制,本层不猜)。
 */
(function () {
  if (!window.RC) window.RC = {};
  window.RC.md = function (s) {
    s = String(s == null ? '' : s);
    if (window.marked && marked.parse) {
      try {
        var math = [];
        var hold = function (m) { return '@@MJX' + (math.push(m) - 1) + '@@'; };
        var t = s
          .replace(/\$\$[\s\S]+?\$\$/g, hold)               // 块级 $$..$$
          .replace(/\\\[[\s\S]+?\\\]/g, hold)               // 块级 \[..\]
          .replace(/\$(?!\s)(?:\\\$|[^$\n])+?\$/g, hold)     // 行内 $..$($ 后须非空白,避开 "$ 5")
          .replace(/\\\([\s\S]+?\\\)/g, hold)                // 行内 \(..\)
          .replace(/([一-鿿　-〿＀-￯])([*`])/g, '$1​$2')
          .replace(/([*`])([一-鿿　-〿＀-￯])/g, '$1​$2');
        var html = marked.parse(t);
        return html.replace(/@@MJX(\d+)@@/g, function (_, i) { return (math[+i] != null ? math[+i] : ''); });   // 还原公式
      } catch (_) {}
    }
    return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\n/g, '<br>');
  };
  window.RC.typeset = function (el) {
    try { if (el && window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([el]).catch(function () {}); } catch (e) {}
  };
})();
