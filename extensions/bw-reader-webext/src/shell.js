// shell.js — 扩展外壳:顶栏 + 右侧抽屉骨架(在 facade.js + vendor/rc-*.js 之后加载)。
//
// 版面/按钮照搬 PDF 阅读器(用户拍板:尽量和现有阅读器一致,iPad 为主):
//   · 顶栏 = pdf_reader.html #header 的暗色 flex 条(样式逐字来自 pdf-styles.css,
//     去掉给全站 nav.js 预留的 52px、加 fixed+滑入滑出);可展开/关闭/📌钉住。
//   · 抽屉 = rc-sidedrawer.js **原文件**(vendor/ 逐字包装),这里只提供 #ep-side 骨架
//     + pane 占位 + init opts——和 EPUB 阅读器接它的方式一模一样。
//   · PDF 专属按钮(双页/页码滑块/去边/插入页)按测绘结论丢弃;振假名/译页/搜索/设置
//     保留占位(里程碑 2+ 接线),点了 toast 说明。
(() => {
  "use strict";
  const RC = window.RC;
  const root = window.__bwRoot, headEl = window.__bwHead;
  if (!RC || !RC.sidedrawer || !root) return;   // vendor 未载入(构建缺失)则静默退出
  if (root.querySelector("#header")) return;    // 幂等

  const LS_PIN = "bw-top-pinned";
  const lsGet = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
  const lsSet = (k, v) => { try { localStorage.setItem(k, v); } catch (e) {} };

  // ── 样式:#header 系逐字来自 pdf-styles.css(标注差异);pill 沿用抽屉把手的磨砂语言 ──
  const st = document.createElement("style");
  st.id = "bw-shell-css";
  st.textContent = `
*{box-sizing:border-box}
button{-webkit-appearance:none;appearance:none}
#bw-root{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:14px;color:#e6e6f0;line-height:normal}
/* ↓ pdf-styles.css #header 逐字;差异:52px nav 预留→14px、加 fixed 顶置 + 滑入滑出 */
#header{position:fixed;top:0;left:0;right:0;z-index:119;height:calc(48px + env(safe-area-inset-top));box-sizing:border-box;display:flex;align-items:center;padding:0 14px;padding-top:env(safe-area-inset-top);background:#10162a;border-bottom:1px solid #2a3550;gap:8px;flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch;
  transform:translateY(-102%);transition:transform .3s cubic-bezier(.4,0,.2,1);box-shadow:0 6px 24px rgba(0,0,0,.35)}
#bw-root.bw-top-open #header{transform:translateY(0)}
#header h1{font-size:14px;margin:0;color:#cfe6ff;flex:0 1 auto;min-width:0;max-width:28vw;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#header button{background:#1a2540;border:1px solid #2a3550;color:#cfe6ff;border-radius:7px;height:36px;min-width:38px;padding:0 12px;cursor:pointer;font-size:15px;line-height:1;display:inline-flex;align-items:center;justify-content:center;white-space:nowrap;flex:none;touch-action:manipulation;-webkit-tap-highlight-color:transparent}
#header button:hover{background:#2c3e6a;border-color:#3b6db5}
#header button svg.rc-tbi{width:20px;height:20px;display:block}
#header .bw-sp{flex:1}
#bw-pin.active{background:#244470;border-color:#3b6db5;color:#7dd3fc}
/* 顶栏收起时的展开小把手(顶部居中,磨砂 pill,与抽屉把手同语言) */
#bw-top-pill{position:fixed;top:0;left:50%;transform:translateX(-50%);z-index:118;
  background:linear-gradient(135deg,rgba(255,255,255,0.10),rgba(255,255,255,0.04)),rgba(99,102,241,0.18);
  backdrop-filter:blur(12px) saturate(150%) brightness(1.04);-webkit-backdrop-filter:blur(12px) saturate(150%) brightness(1.04);
  border:1px solid rgba(255,255,255,0.22);border-top:none;border-radius:0 0 12px 12px;
  color:rgba(255,255,255,0.85);padding:3px 18px 5px;font-size:11px;letter-spacing:2px;cursor:pointer;
  box-shadow:0 2px 12px rgba(0,0,0,.35);user-select:none;-webkit-user-select:none;-webkit-tap-highlight-color:transparent;transition:background .15s,color .15s}
#bw-top-pill:hover{color:#fff;background:linear-gradient(135deg,rgba(255,255,255,0.15),rgba(255,255,255,0.06)),rgba(99,102,241,0.28)}
#bw-root.bw-top-open #bw-top-pill{display:none}
/* 逐字搬自 epub-styles.css:238 —— 抽屉 pane 互斥:rc-sidedrawer 的 #ep-side-hl{display:flex}(ID)
   比 .ep-side-pane{display:none}(类)特异性高,会让 hl pane 常驻显示;这条 (1,2,0) 把非 active 压下去 */
#ep-side .ep-side-pane:not(.active){display:none}
/* 补偿:rc-sidedrawer CSS 的 body.ep-side-open 选择器在门面下落在 #bw-root 上 → 把手随抽屉左移这条在此复述 */
#bw-root.ep-side-open #ep-side-handle{right:min(38vw,560px)}
@media (max-width:900px){#bw-root.ep-side-open #ep-side-handle{right:58vw}}
/* pane 占位文案 */
.bw-pane-todo{padding:18px;color:#8a9bb4;font-size:13px;line-height:1.8}
.bw-pane-todo b{color:#cfe6ff}
`;
  headEl.appendChild(st);

  // ── inline onclick 垫片:vendor 组件(rc-wordpop 等)模板里的 onclick="fn(this)" 属性会在
  //    页面世界编译执行,找不到隔离世界的全局函数 → 静默哑火(实锤:☆标记掌握点了没反应)。
  //    这里在 shadow 捕获阶段解析属性,调隔离世界的同名 window.fn。支持 fn()/fn(this)/
  //    fn('str'[,this])/数字参,外加 new Audio('url').play() 特例。页面世界那份照常报它的错,无碍。
  const shadowEl = window.__bwShadow;
  shadowEl.addEventListener("click", (e) => {
    let el = e.target;
    while (el && el !== shadowEl && !(el.getAttribute && el.getAttribute("onclick"))) el = el.parentNode;
    if (!el || el === shadowEl) return;
    const code = el.getAttribute("onclick") || "";
    const au = code.match(/^\s*new Audio\('((?:[^'\\]|\\.)*)'\)\.play\(\)\s*;?\s*$/);
    if (au) { try { new Audio(au[1].replace(/\\'/g, "'")).play(); } catch (_) {} return; }
    const m = code.match(/^\s*(?:window\.)?([A-Za-z_$][\w$]*)\s*\((.*)\)\s*;?\s*$/s);
    if (!m) return;
    const fn = window[m[1]];
    if (typeof fn !== "function") return;
    const args = [];
    const raw = m[2].trim();
    if (raw) {
      for (const tok of raw.match(/(?:'(?:[^'\\]|\\.)*'|[^,])+/g) || []) {
        const t = tok.trim();
        if (t === "this") args.push(el);
        else if (t[0] === "'") args.push(t.slice(1, -1).replace(/\\'/g, "'"));
        else if (!isNaN(+t)) args.push(+t);
        else return;   // 复杂表达式不硬猜,放弃(页面世界报错可见)
      }
    }
    try { fn.apply(el, args); } catch (err) { try { console.warn("[bw] onclick 垫片:", m[1], err); } catch (_) {} }
  }, true);

  // ── 抽屉骨架:#ep-side + 5 个 pane(与 rc-sidedrawer DEFAULT_TABS 对应;内容里程碑 2+ 填)──
  const side = document.createElement("aside");
  side.id = "ep-side";
  const PANES = [
    ["asst", "ep-side-asst", "AI 助手(阅读器同款对话 + SSE 流式)"],
    ["vocab", "ep-side-vocab", "单词本(本页生词 / 查词记录)"],
    ["kg", "ep-side-kg", "知识点(KG 关联)"],
    ["hl", "ep-side-hl", "高亮列表(网页持久高亮)"],
    ["toc", "ep-side-toc", "目录(本页标题大纲)"],
  ];
  for (const [pane, id, desc] of PANES) {
    const d = document.createElement("div");
    d.className = "ep-side-pane";
    d.dataset.pane = pane;
    d.id = id;
    d.innerHTML = `<div class="bw-pane-todo"><b>${desc.split("(")[0]}</b><br>${desc}<br><br>里程碑 2 接线中——外壳先行验证版。</div>`;
    side.appendChild(d);
  }
  root.appendChild(side);

  // ── 顶栏:按钮/图标照搬 pdf_reader.html #header(PDF 专属项已按测绘结论去除)──
  const SVG_SEARCH = '<svg class="rc-tbi" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="11" cy="11" r="6.2"/><path d="M15.6 15.6L20 20"/></svg>';
  const SVG_GEAR = '<svg class="rc-tbi" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><circle cx="12" cy="12" r="3"/><path d="M12 3v2.5M12 18.5V21M21 12h-2.5M5.5 12H3M18.4 5.6l-1.8 1.8M7.4 16.6l-1.8 1.8M18.4 18.4l-1.8-1.8M7.4 7.4L5.6 5.6"/></svg>';
  const SVG_ASST = '<svg class="rc-tbi" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4l1.4 4.2L18 9.6l-4.6 1.4L12 16l-1.4-4.6L6 9.6l4.6-1.4L12 4z"/><path d="M18.6 14.5l.6 1.7 1.7.6-1.7.6-.6 1.7-.6-1.7-1.7-.6 1.7-.6.6-1.7z"/></svg>';

  const bar = document.createElement("div");
  bar.id = "header";
  bar.innerHTML = `
    <h1 title="当前页面"></h1>
    <button id="ruby-toggle" title="振假名 / 英文音标叠加(汉字上方注假名、英文词上方注音标)">あ</button>
    <button id="pagetr-toggle" title="整页翻译:本页所有段落就地显示中文(再按关闭)">译页</button>
    <button id="bw-search-btn" title="页内搜索">${SVG_SEARCH}</button>
    <button id="bw-set-btn" title="AI 设置">${SVG_GEAR}</button>
    <span class="bw-sp"></span>
    <button id="bw-asst-btn" title="AI 助手侧栏">${SVG_ASST}</button>
    <button id="bw-pin" title="钉住顶栏(每个页面自动展开)">📌</button>
    <button id="bw-top-close" title="收起顶栏">✕</button>`;
  root.appendChild(bar);
  bar.querySelector("h1").textContent = document.title || location.host;

  const pill = document.createElement("button");
  pill.id = "bw-top-pill";
  pill.textContent = "▾ 伴读";
  pill.title = "展开伴读顶栏";
  root.appendChild(pill);

  // ── 交互 ──
  const openTop = () => root.classList.add("bw-top-open");
  const closeTop = () => root.classList.remove("bw-top-open");
  pill.addEventListener("click", openTop);
  bar.querySelector("#bw-top-close").addEventListener("click", closeTop);

  const pinBtn = bar.querySelector("#bw-pin");
  const syncPin = () => pinBtn.classList.toggle("active", lsGet(LS_PIN) === "1");
  pinBtn.addEventListener("click", () => {
    lsSet(LS_PIN, lsGet(LS_PIN) === "1" ? "0" : "1");
    syncPin();
    RC.toast(lsGet(LS_PIN) === "1" ? "已钉住:每个页面自动展开顶栏" : "已取消钉住");
  });
  syncPin();
  if (lsGet(LS_PIN) === "1") openTop();

  bar.querySelector("#bw-asst-btn").addEventListener("click", () => RC.sidedrawer.toggle());
  const todo = (msg) => () => RC.toast(msg + "——里程碑 2 接线中");
  bar.querySelector("#ruby-toggle").addEventListener("click", todo("振假名/音标"));
  bar.querySelector("#pagetr-toggle").addEventListener("click", todo("整页翻译"));
  bar.querySelector("#bw-search-btn").addEventListener("click", todo("页内搜索"));
  bar.querySelector("#bw-set-btn").addEventListener("click", todo("AI 设置"));

  // ── 抽屉 init:接法与 EPUB 阅读器一致;onReflow 传 no-op(悬浮覆盖,绝不打扰宿主页布局)──
  RC.sidedrawer.init({
    handleLabel: "伴读 · 助手",
    defaultTab: "asst",
    onTab: () => {},          // 里程碑 2:懒填充各 pane
    onReflow: () => {},       // 不派发 resize 给宿主页(阅读器里是给 epub.js 重排用的)
  });
  // 任意网页没有可挤压的正文 → 首次默认「悬浮显示」(用户改过则尊重持久化)
  if (lsGet("eph-gp-floating") === null) RC.sidedrawer.setFloating(true);

  // ── MathJax × Shadow DOM 垫片:MathJax(未包装,真 document 上下文)把 CHTML 布局样式注入
  //    真 document.head,shadow 里看不见 → 每次排版后把 MJX-* 样式克隆镜像进 shadow。
  //    @font-face 留在真 head(字体注册是全局的,shadow 内可用;反之 @font-face 在 shadow 里不生效)。
  const mirrorMjx = () => {
    try {
      document.head.querySelectorAll('style[id^="MJX-"]').forEach((s) => {
        const old = headEl.querySelector('style[data-mjx-mirror="' + s.id + '"]');
        if (old && old.textContent === s.textContent) return;
        const c = document.createElement("style");
        c.setAttribute("data-mjx-mirror", s.id);
        c.textContent = s.textContent;
        if (old) old.replaceWith(c); else headEl.appendChild(c);
      });
    } catch (e) {}
  };
  const _origTypeset = RC.typeset;
  RC.typeset = function (el) {
    try {
      if (window.MathJax && MathJax.typesetPromise) {
        MathJax.typesetPromise([el]).then(mirrorMjx).catch(() => {});
        return;
      }
    } catch (e) {}
    try { _origTypeset && _origTypeset(el); } catch (e) {}
  };

  // ── AI 助手真身:逐字照搬 epub-html.js:4154-4165 的共享侧栏挂载三步 ──
  //   ① 摘掉占位 asst pane + RC.sidedrawer 建的 asst tab(避免 data-pane="asst" 撞两份)
  //   ② mountPdfSidebar() 自建共享 tab+pane 进 #ep-side-tabs/#ep-side(经 adapter mountPanel/mountTabs)
  //   ③ 把共享 tab/pane 的 PDF class(side-tab/side-pane)补成抽屉 class → setTab() 认得
  try {
    var shadow = window.__bwShadow;
    if (RC.assistant && RC.assistant.mountPdfSidebar && shadow) {
      var _op = shadow.getElementById("ep-side-asst");
      if (_op && _op.parentNode) _op.parentNode.removeChild(_op);
      var _ot = shadow.querySelector('#ep-side-tabs .ep-side-tab[data-pane="asst"]');
      if (_ot && _ot.parentNode) _ot.parentNode.removeChild(_ot);
      RC.assistant.mountPdfSidebar();
      var _nt = shadow.querySelector('#ep-side-tabs .side-tab[data-pane="asst"]');
      if (_nt) { _nt.classList.remove("side-tab"); _nt.classList.add("ep-side-tab"); }
      var _np = shadow.getElementById("side-pane-asst");
      if (_np) _np.classList.add("ep-side-pane");
    }
  } catch (e) {}
})();
