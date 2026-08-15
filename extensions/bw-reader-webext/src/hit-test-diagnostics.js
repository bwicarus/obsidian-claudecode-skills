// hit-test-diagnostics.js — 临时排查专用:iPad 顶栏/侧栏按钮全部点不动。
//
// 背景:1.1.58(317/320)两轮修法在桌面 Chromium/Playwright WebKit 都测过、
// 也确实修了些真问题(代际升级、composedPath 豁免、显式几何),但真机仍然失败。
// 手上没有配对 Mac 的 Safari Web Inspector,也没有别的远程调试通道——每一轮
// 信息只能靠"改代码→出包→用户手测→文字转述"这条链路,一次一个比特,而且
// 转述会丢掉时序/坐标这类没法用语言描述的细节。
//
// 这个文件不修任何东西,只让下一轮真机测试从"点了没反应"变成一份能读的记录。
// 面板必须在**任何点击都不成功的前提下依然看得见**——这是它存在的全部意义,
// 所以它自己绝不能是又一个需要被点击才能打开的东西:默认展开、pointer-events:none
// (它自己不拦任何事件,也不会成为下一个"点不动"的怪圈)。
//
// 用完即删:这不是产品功能,是一次性探针。定位到根因后随下一版一起摘掉。
(() => {
  "use strict";
  if (window.__bwPwaProviderOnly) return;
  if (window.__bwHitTestDiag) return;   // 幂等

  const MAX_LINES = 22;
  const lines = [];
  let panel = null;
  let raf = 0;

  function fmt(n) {
    return typeof n === "number" ? Math.round(n) : n;
  }

  // 环境只需要记一次:iPadOS/Safari 版本、视口与 layout 是否一致——这些原本
  // 要另开一轮问答才能问到用户,现在直接印在面板顶部,省一轮真机测试。
  function envLine() {
    const vv = window.visualViewport;
    return [
      "UA: " + navigator.userAgent.slice(0, 90),
      "inner=" + fmt(innerWidth) + "x" + fmt(innerHeight)
        + " visualVP=" + (vv ? fmt(vv.width) + "x" + fmt(vv.height)
          + "@" + fmt(vv.offsetLeft) + "," + fmt(vv.offsetTop)
          + " scale=" + vv.scale : "n/a")
        + " dpr=" + devicePixelRatio,
    ].join("\n");
  }

  function render() {
    raf = 0;
    if (!panel) return;
    panel.textContent = envLine() + "\n──\n" + lines.join("\n");
  }

  function log(line) {
    lines.push(line);
    if (lines.length > MAX_LINES) lines.shift();
    if (!raf) raf = requestAnimationFrame(render);
  }

  function shortPath(e) {
    const cp = typeof e.composedPath === "function" ? e.composedPath() : [];
    return cp.slice(0, 4).map((n) => {
      if (!n || !n.nodeType) return String(n).slice(0, 12);
      if (n.nodeType !== 1) return "#text";
      const id = n.id ? "#" + n.id : "";
      const cls = n.className && typeof n.className === "string"
        ? "." + n.className.split(/\s+/).slice(0, 2).join(".") : "";
      return n.tagName.toLowerCase() + id + cls;
    }).join(">");
  }

  function inkDiag() {
    try {
      return window.__bwWebInk && typeof window.__bwWebInk.diag === "function"
        ? window.__bwWebInk.diag() : null;
    } catch (_) { return null; }
  }

  // 命中点核实:visually-at 与 elementFromPoint 是否指向同一元素。二者不一致
  // 直接证明是几何/视口错位而不是事件被业务代码吞掉——这是最想要的那条证据。
  function hitCheck(x, y) {
    let el;
    try { el = document.elementFromPoint(x, y); } catch (_) { return "elementFromPoint 异常"; }
    if (!el) return "elementFromPoint=null(命中点落在可视区域外)";
    const id = el.id ? "#" + el.id : "";
    const inShadowRoot = !!(el.getRootNode && el.getRootNode() instanceof ShadowRoot);
    return el.tagName.toLowerCase() + id
      + (inShadowRoot ? "[in-shadow]" : "[light-dom]");
  }

  function record(kind, e) {
    const x = e.clientX ?? (e.touches && e.touches[0] && e.touches[0].clientX);
    const y = e.clientY ?? (e.touches && e.touches[0] && e.touches[0].clientY);
    const at = typeof x === "number" ? hitCheck(x, y) : "no-coords";
    const ink = inkDiag();
    let line = kind + " path=" + shortPath(e)
      + " type=" + (e.pointerType || (e.touches ? "touch-legacy" : "-"))
      + " xy=" + fmt(x) + "," + fmt(y)
      + " hit=" + at;
    if (ink) {
      line += " ink[strokes=" + ink.strokesLen
        + " tap=" + ink.touchTapActive
        + " suppress=" + (ink.suppressArmed ? fmt(ink.suppressAgeMs) + "ms前武装" : "off") + "]";
    }
    log(line);
    // defaultPrevented/cancelBubble 要等本轮事件分发完全走完才读得准——同步读
    // 只能看见"我自己之前的监听器"改过的状态,读不到后面 document/元素层的动作。
    setTimeout(() => {
      log("  ↳ " + kind + " 分发后: defaultPrevented=" + e.defaultPrevented
        + " cancelBubble=" + e.cancelBubble);
    }, 0);
  }

  for (const type of ["pointerdown", "pointerup", "touchstart", "click"]) {
    window.addEventListener(type, (e) => record(type, e), { capture: true, passive: true });
  }

  function mount() {
    if (panel) return;
    panel = document.createElement("div");
    panel.id = "bw-hittest-diag";
    // fixed + pointer-events:none:自己绝不拦截任何东西,也不需要被点击才能看到。
    // z-index 顶格,避免被其它 fixed 层盖住——这次要看的正是"顶层的东西到底
    // 有没有收到事件",诊断面板本身被遮住就白做了。
    // pointer-events:none 是绝对的:user-select 需要指针交互才能拖出选区,
    // 跟 none 并存不会真的可选——干脆别在样式里假装能选,读法就是截图。
    panel.style.cssText = "position:fixed;left:4px;bottom:4px;right:4px;"
      + "max-height:46vh;overflow:hidden;z-index:2147483647;"
      + "pointer-events:none;background:rgba(0,0,0,.82);color:#7dffb0;"
      + "font:9px/1.35 ui-monospace,monospace;padding:6px 8px;"
      + "white-space:pre-wrap;word-break:break-all;border-radius:6px;";
    document.documentElement.appendChild(panel);
    render();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mount, { once: true });
  } else {
    mount();
  }

  window.__bwHitTestDiag = { lines, log };
})();
