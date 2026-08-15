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

  // node → 可读片段。ShadowRoot 的 nodeType 是 11(DOCUMENT_FRAGMENT_NODE),
  // 不是 1——第一版把它跟真正的文本节点混着标成 "#text",第一次真机截图里
  // 那个看着莫名其妙的 "#text" 就是它,不是路径里真出现了文本节点。
  function nodeLabel(n) {
    if (!n) return String(n);
    if (n instanceof ShadowRoot) return "[shadow-root]";
    if (!n.nodeType) return String(n).slice(0, 12);
    if (n.nodeType !== 1) return "#text";
    const id = n.id ? "#" + n.id : "";
    const cls = n.className && typeof n.className === "string"
      ? "." + n.className.split(/\s+/).slice(0, 2).join(".") : "";
    return n.tagName.toLowerCase() + id + cls;
  }

  function shortPath(cp) {
    return cp.slice(0, 4).map(nodeLabel).join(">");
  }

  // composedPath() 里第一个真正的元素节点——不是 ShadowRoot、不是 document——
  // 就是浏览器自己判定"事件目标"的那个东西。它是不是好使,看它自己的
  // getBoundingClientRect() 是否真的包住了这次点击的坐标。
  function firstElement(cp) {
    for (const n of cp) {
      if (n && n.nodeType === 1) return n;
    }
    return null;
  }

  // 命中点核实,两条独立证据:
  //   ① 目标自己的可点区域是否真的盖住了这次点击——用户说"贴着按钮下边缘
  //      才能点中",这条直接把这句话量化成像素级的 dx/dy。
  //   ② 从纯几何角度(不看事件分发,只看渲染树)穿透 Shadow DOM 逐层
  //      elementFromPoint,得到的元素跟浏览器判定的目标是不是同一个——
  //      不一致就说明是"分发算的目标"和"看起来在那儿的元素"根本不是一回事。
  function rectCheck(target, x, y) {
    if (!target || typeof target.getBoundingClientRect !== "function") {
      return "no-rect-target";
    }
    const r = target.getBoundingClientRect();
    const inside = x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
    if (inside) return "inside-own-rect";
    // 到最近边的有向距离:正值=点在矩形外该方向那一侧。
    const dx = x < r.left ? r.left - x : (x > r.right ? x - r.right : 0);
    const dy = y < r.top ? r.top - y : (y > r.bottom ? y - r.bottom : 0);
    return "OUTSIDE-own-rect dx=" + fmt(dx) + " dy=" + fmt(dy)
      + " rect=" + fmt(r.left) + "," + fmt(r.top) + "-" + fmt(r.right) + "," + fmt(r.bottom);
  }

  function deepElementFromPoint(x, y) {
    let el;
    try { el = document.elementFromPoint(x, y); } catch (_) { return null; }
    let guard = 0;
    while (el && el.shadowRoot && guard++ < 8) {
      let inner;
      try { inner = el.shadowRoot.elementFromPoint(x, y); } catch (_) { break; }
      if (!inner || inner === el) break;
      el = inner;
    }
    return el;
  }

  function inkDiag() {
    try {
      return window.__bwWebInk && typeof window.__bwWebInk.diag === "function"
        ? window.__bwWebInk.diag() : null;
    } catch (_) { return null; }
  }

  function record(kind, e) {
    const x = e.clientX ?? (e.touches && e.touches[0] && e.touches[0].clientX);
    const y = e.clientY ?? (e.touches && e.touches[0] && e.touches[0].clientY);
    const cp = typeof e.composedPath === "function" ? e.composedPath() : [];
    const target = firstElement(cp);
    const ink = inkDiag();
    let line = kind + " path=" + shortPath(cp)
      + " type=" + (e.pointerType || (e.touches ? "touch-legacy" : "-"))
      + " xy=" + fmt(x) + "," + fmt(y);
    if (typeof x === "number") {
      line += " " + rectCheck(target, x, y);
      const deep = deepElementFromPoint(x, y);
      // 分发目标(target,来自 composedPath)跟纯几何穿透算出来的元素
      // 不是同一个,才值得单独报一行——多数时候两者一致,不必每条都印。
      if (deep && target && deep !== target) {
        line += " geomHit=" + nodeLabel(deep) + "≠dispatchTarget";
      }
    } else {
      line += " no-coords";
    }
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

  // 顶栏按钮的**静态自检**:不依赖用户点中任何东西。
  //
  // 前两轮的面板只在收到事件时才写一行,可这次的故障很可能正是"事件根本
  // 没到" —— 那种情况下面板一行都不出,看起来像诊断没装上。所以这里主动
  // 量一次:按钮在不在、它的矩形在哪、以及**那个矩形的中心点到底命中了谁**。
  //
  // 最后一项是关键。命中的若不是按钮自己,返回的元素名就直接指认了盖在
  // 上面的那一层是什么 —— 这正是三轮猜测都想知道而始终没拿到的那条证据。
  function probeTopbar() {
    const shadow = window.__bwShadow;
    if (!shadow) { log("自检: 无 __bwShadow(扩展 UI 未装载)"); return; }
    const header = shadow.querySelector("#header");
    if (!header) { log("自检: shadow 内无 #header"); return; }
    const hr = header.getBoundingClientRect();
    log("自检 #header rect=" + fmt(hr.left) + "," + fmt(hr.top)
      + "-" + fmt(hr.right) + "," + fmt(hr.bottom));
    const buttons = header.querySelectorAll("button");
    log("自检 顶栏按钮数=" + buttons.length);
    let reported = 0;
    for (const b of buttons) {
      if (reported >= 3) break;            // 三个够定性,不刷屏
      const r = b.getBoundingClientRect();
      if (!r.width || !r.height) continue;
      const cx = r.left + r.width / 2;
      const cy = r.top + r.height / 2;
      const hit = deepElementFromPoint(cx, cy);
      const own = hit === b || (b.contains && hit && b.contains(hit));
      log("自检 " + (b.id ? "#" + b.id : b.textContent.trim().slice(0, 6))
        + " rect=" + fmt(r.left) + "," + fmt(r.top) + "-" + fmt(r.right) + "," + fmt(r.bottom)
        + " 中心命中=" + nodeLabel(hit) + (own ? " ✓自己" : " ✗被挡"));
      reported++;
    }
  }
  // 顶部死区实测:每次 pointerdown 记下最小的 y。iOS Safari 会截走贴顶那条
  // 带里的触摸,页面完全收不到事件 —— 所以"页面见过的最高一次触摸"就是死区
  // 下沿的上界。用户在顶栏上反复点几次,这个数字就逼近真实高度,
  // 让位高度不必再靠描述推算。
  let minY = null;
  window.addEventListener("pointerdown", (e) => {
    if (typeof e.clientY !== "number") return;
    if (minY === null || e.clientY < minY) {
      minY = e.clientY;
      log("最高触摸 y=" + fmt(minY) + "(顶部死区不超过这个值)");
    }
  }, { capture: true, passive: true });

  // 延后一拍:facade/shell 都在 document_idle 之后才建好 shadow 与顶栏。
  setTimeout(probeTopbar, 1500);

  window.__bwHitTestDiag = { lines, log, probeTopbar };
})();
