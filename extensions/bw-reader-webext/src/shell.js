// shell.js — 扩展外壳:顶栏 + 右侧抽屉骨架(在 facade.js + vendor/rc-*.js 之后加载)。
//
// 版面/按钮照搬 PDF 阅读器(用户拍板:尽量和现有阅读器一致,iPad 为主):
//   · 顶栏 = pdf_reader.html #header 的暗色 flex 条(样式逐字来自 pdf-styles.css,
//     去掉给全站 nav.js 预留的 52px、加 fixed+滑入滑出);可展开/关闭/📌钉住。
//   · 抽屉 = rc-sidedrawer.js **原文件**(vendor/ 逐字包装),这里只提供 #ep-side 骨架
//     + pane 占位 + init opts——和 EPUB 阅读器接它的方式一模一样。
//   · PDF 专属按钮(双页/页码滑块/去边/插入页)按测绘结论丢弃；振假名/译页/搜索/设置
//     在 PDF 中走本地白名单，在普通网页中走 DOM 可逆装饰。
(() => {
  "use strict";
  if (window.__bwPwaProviderOnly) return;
  const RC = window.RC;
  const root = window.__bwRoot, headEl = window.__bwHead;
  if (!RC || !RC.sidedrawer || !root) return;   // vendor 未载入(构建缺失)则静默退出
  if (root.querySelector("#header")) return;    // 幂等

  const TOPBAR_PREFS_KEY = "bwTopbarPreferencesV1";
  const TOPBAR_SESSION_KEY = "bw-topbar-collapsed-session-v1";
  const lsGet = (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } };
  const sessionCollapsed = () => {
    try {
      const value = sessionStorage.getItem(TOPBAR_SESSION_KEY);
      return value === null ? null : value === "1";
    } catch (_) { return null; }
  };
  const writeSessionCollapsed = (value) => {
    try { sessionStorage.setItem(TOPBAR_SESSION_KEY, value ? "1" : "0"); } catch (_) {}
  };

  // ── 样式:#header 系逐字来自 pdf-styles.css(标注差异);pill 沿用抽屉把手的磨砂语言 ──
  // iOS Safari 顶部手势带的让位高度。只在**扩展跑在 Safari 网页里**时给值:
  //  · App 内的阅读器由原生壳承载,没有这条手势带,给了反而白白让出一截;
  //  · 桌面浏览器同理。
  // 44px 是量出来的:用户报告"几乎贴着按钮下边缘才能点中",而按钮高 36px、
  // 顶栏上边距若干 —— 能点中的那一线正好在 40px 上下。取 44 留一点余量,
  // 同时仍在 env(safe-area-inset-top) 的量级内,不至于把顶栏推到显眼的位置。
  try {
    const ua = navigator.userAgent || "";
    const iOSLike = /iPad|iPhone|iPod/.test(ua) ||
      (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
    // 扩展在 App 内的 WKWebView 里也会跑(书籍 PWA 场景),那时 __bwPwaBridge
    // 存在;只有真正的 Safari 网页才需要让位。
    if (iOSLike && !window.__bwPwaBridge && !window.__bwRootNative) {
      root.style.setProperty("--bw-ios-gesture-inset", "44px");
    }
  } catch (_) {}

  const st = document.createElement("style");
  st.id = "bw-shell-css";
  st.textContent = `
*{box-sizing:border-box}
button{-webkit-appearance:none;appearance:none}
	#bw-root{font-family:var(--rc-font-ui);font-size:14px;color:var(--rc-text,#e6e6f0);line-height:normal}
	#bw-root>*{pointer-events:auto}
	#bw-root>.bw-ink-dbg{pointer-events:none}
/* ↓ pdf-styles.css #header 逐字;差异:52px nav 预留→14px、加 fixed 顶置；收起由 rc-ui 唯一控制器负责 */
#header{position:fixed;top:0;left:0;right:0;z-index:119;height:calc(48px + env(safe-area-inset-top));box-sizing:border-box;display:flex;align-items:center;padding:0 14px;padding-top:env(safe-area-inset-top);background:var(--rc-bg-surface,#10162a);border-bottom:1px solid var(--rc-border,#2a3550);gap:8px;flex-wrap:nowrap;overflow-x:auto;-webkit-overflow-scrolling:touch;
  box-shadow:0 6px 24px rgba(0,0,0,.35)}
#header h1{font-size:14px;margin:0;color:var(--rc-text-strong,#cfe6ff);flex:0 1 auto;min-width:0;max-width:28vw;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#header button{background:var(--rc-bg-raised,#1a2540);border:1px solid var(--rc-border,#2a3550);color:var(--rc-text-strong,#cfe6ff);border-radius:7px;height:36px;min-width:38px;padding:0 12px;cursor:pointer;font-size:15px;line-height:1;display:inline-flex;align-items:center;justify-content:center;white-space:nowrap;flex:none;touch-action:manipulation;-webkit-tap-highlight-color:transparent}
#header button[hidden]{display:none!important}
#header button:hover{background:var(--rc-bg-hover,#2c3e6a);border-color:var(--rc-border-accent,#3b6db5)}
#header #ruby-toggle.active,#header #pagetr-toggle.active,#header #bw-ink-btn.active{background:var(--rc-bg-active,#244470);border-color:var(--rc-accent,#60a5fa);color:#dbeafe}
#header button svg.rc-tbi{width:20px;height:20px;display:block}
#header .bw-book-scrub{width:112px;min-width:80px;accent-color:var(--rc-accent,#60a5fa);flex:none}
#header .bw-book-scrub[hidden]{display:none!important}
#header .bw-sp{flex:1}
#bw-pin.active{background:var(--rc-bg-active,#244470);border-color:var(--rc-border-accent,#3b6db5);color:var(--rc-accent-cyan,#7dd3fc)}
/* 逐字搬自 epub-styles.css:238 —— 抽屉 pane 互斥:rc-sidedrawer 的 #ep-side-hl{display:flex}(ID)
   比 .ep-side-pane{display:none}(类)特异性高,会让 hl pane 常驻显示;这条 (1,2,0) 把非 active 压下去 */
#ep-side .ep-side-pane:not(.active){display:none}
/* 补偿:rc-sidedrawer CSS 的 body.ep-side-open 选择器在门面下落在 #bw-root 上 → 把手随抽屉左移这条在此复述 */
#bw-root.ep-side-open #ep-side-handle{right:var(--ep-side-width,min(38vw,560px))}
/* ── iOS Safari 顶部系统手势带:贴着屏幕顶边的元素收不到触摸 ──────────────
   真机证据(1.1.58 build 330):顶栏按钮与侧栏 tab 全部点不动,而中部的抽屉
   把手一切正常;点击时连 window 捕获阶段的诊断都一行不出 —— 事件根本没有
   进入页面,不是被谁吞掉。两者唯一的共同点就是 top:0。Safari 自己要用最
   上面那条带做下拉/地址栏手势,落在里面的网页元素拿不到 pointer 事件。

   这不是"扩展 UI 有 bug",是它站错了位置。App 内的阅读器没有这个问题
   (原生壳没有 Safari 的手势带),所以修必须只作用于扩展的网页场景 ——
   rc-sidedrawer.js 是 App/扩展共用的原件,绝不能为此改动它。

   之前三轮都在改命中逻辑(代际、composedPath、宿主几何),而位置才是原因。 */
/* 修法不是把整条顶栏往下推 —— 那样看着就是"悬在半空",用户第一眼就说怪。
   顶栏**仍然贴 top:0**,背景一路铺到屏幕最顶端,外观与 App 内一致;
   只是把里面的**可点内容**用 padding 压到死区之下。死区那几十像素由顶栏
   自己的背景填满,看不出让位,按钮却落在能收到触摸的区域里。
   (Safari 的顶部手势带归系统所有,网页无法用 touch-action 之类夺回 ——
   App 里没这问题是因为它自己拥有 WebView,可以设
   contentInsetAdjustmentBehavior=.never;扩展寄居在别人的标签页里没这个权力。) */
#header{padding-top:calc(env(safe-area-inset-top) + var(--bw-ios-gesture-inset,0px))!important;
        height:calc(48px + env(safe-area-inset-top) + var(--bw-ios-gesture-inset,0px))!important}
/* 侧栏同理:面板本体照旧铺满整个右侧,只把 tab 栏压到死区之下。 */
#ep-side-tabbar{padding-top:calc(6px + var(--bw-ios-gesture-inset,0px))!important}
/* 收起顶栏后用来重新展开的 pill:rc-ui.js:46 在收起态把它设成 top:0 —— 正好
   落回死区。真机实测(用户报告):顶栏一收起就再也打不开,界面被锁死。
   它是个独立小控件、没有背景条可以填充,只能整体下移。 */
.rc-topbar-pill[data-collapsed="1"]{top:var(--bw-ios-gesture-inset,0px)!important}
/* pane 占位文案 */
.bw-pane-todo{padding:18px;color:var(--rc-text-muted,#8a9bb4);font-size:13px;line-height:1.8}
.bw-pane-todo b{color:var(--rc-text-strong,#cfe6ff)}
.bw-pane-head{padding:11px 13px;border-bottom:1px solid #1f2740;color:#aebfe0;font-size:13px;font-weight:600}
.bw-pane-list{padding:10px 12px;overflow:auto}.bw-empty{padding:12px;color:#5a6680;font-size:12px;line-height:1.6}
.bw-vocab-item{padding:9px 8px;border-bottom:1px solid #1f2740;cursor:pointer}.bw-vocab-item:hover{background:#162045}
.bw-vocab-item b{color:#dbeafe}.bw-vocab-item small{display:block;color:#8a9bb4;margin-top:3px}
.bw-toc-item{display:block;width:100%;text-align:left;background:transparent;border:0;color:#cfe0ff;padding:7px 9px;border-radius:var(--rc-radius-sm,6px);cursor:pointer;font-size:12px}
.bw-toc-item:hover{background:var(--rc-bg-raised,#1a2540)}
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

  // ── 抽屉骨架:共享数据 pane；助手 pane 随后由 rc-assistant 原件替换。──
  const side = document.createElement("aside");
  side.id = "ep-side";
  const PANES = [
    ["asst", "ep-side-asst", '<div class="bw-empty">AI 助手加载中…</div>'],
    ["vocab", "ep-side-vocab", '<div class="bw-pane-head">单词本</div><div id="bw-vocab-list" class="bw-pane-list"><div class="bw-empty">点开载入…</div></div>'],
    ["kg", "ep-side-kg", '<div class="bw-pane-head">当前内容对应知识点</div><div id="bw-kg-list" class="bw-pane-list"><div class="bw-empty">点开载入…</div></div>'],
    ["hl", "ep-side-hl", '<div class="bw-pane-head">高亮</div><div id="bw-hl-list" class="bw-pane-list"><div class="bw-empty">点开载入…</div></div>'],
    ["toc", "ep-side-toc", '<div class="bw-pane-head">目录</div><div id="bw-toc-list" class="bw-pane-list"><div class="bw-empty">点开载入…</div></div>'],
    ["grammar", "ep-side-grammar", '<div class="bw-pane-head">语法分析</div><div id="bw-grammar-list" class="bw-pane-list"><div class="gb-empty bw-empty">选中文字后点“📊 语法”</div></div>'],
  ];
  for (const [pane, id, html] of PANES) {
    const d = document.createElement("div");
    d.className = "ep-side-pane";
    d.dataset.pane = pane;
    d.id = id;
    d.innerHTML = html;
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
    <button id="bw-book-prev" data-pwa-capability="navigation" data-pwa-modes="pdf,epub,favorite" title="上一页">←</button>
    <button id="bw-book-jump" data-pwa-capability="navigation" data-pwa-modes="pdf,epub,favorite" title="当前页 / 位置；点击跳转">—/—</button>
    <input id="bw-book-scrub" class="bw-book-scrub" type="range" min="1" max="1" value="1" data-pwa-capability="navigation" data-pwa-modes="pdf,epub,favorite" title="拖动跳转">
    <button id="bw-book-next" data-pwa-capability="navigation" data-pwa-modes="pdf,epub,favorite" title="下一页">→</button>
    <button id="bw-book-zoom-out" data-pwa-capability="zoom" title="缩小">−</button>
    <button id="bw-book-fit" data-pwa-capability="zoom" title="适合宽度">宽</button>
    <button id="bw-book-zoom-in" data-pwa-capability="zoom" title="放大">＋</button>
    <button id="bw-book-layout" data-pwa-capability="layout" title="切换单双页 / 排版">版</button>
    <button id="bw-book-crop" data-pwa-capability="crop" title="切换去边">裁</button>
    <button id="bw-book-fullscreen" data-pwa-capability="fullscreen" title="全屏">⛶</button>
    <button id="bw-book-settings" data-pwa-capability="bookSettings" title="书籍设置">书设</button>
    <button id="bw-book-favorite" data-pwa-capability="favorite" title="收藏">☆</button>
    <button id="bw-book-user-page" data-pwa-capability="userPage" title="创建用户页">＋页</button>
    <button id="ruby-toggle" data-pwa-capability="ruby" title="振假名 / 英文音标叠加(汉字上方注假名、英文词上方注音标)">あ</button>
    <button id="pagetr-toggle" data-pwa-capability="pageTranslate" title="沉浸式翻译：滚到哪译到哪，再按关闭">译页</button>
    <button id="bw-ink-btn" data-pwa-capability="ink" title="在页面上绘图">✏️</button>
    <button id="bw-note-btn" data-pwa-capability="stickyNote" title="在视野中央新建便签">🗒</button>
    <button id="bw-search-btn" data-pwa-capability="bookSearch" title="页内搜索">${SVG_SEARCH}</button>
    <button id="bw-set-btn" title="AI 设置">${SVG_GEAR}</button>
    <span class="bw-sp"></span>
    <button id="bw-asst-btn" title="AI 助手侧栏">${SVG_ASST}</button>
    <button id="bw-pin" title="钉住顶栏(每个页面自动展开)">📌</button>
    <button id="bw-top-close" title="收起顶栏">✕</button>`;
  root.appendChild(bar);
  bar.querySelector("h1").textContent = document.title || location.host;

  // ── 交互：4B 与 PDF / EPUB / HTML 共用 rc-ui 顶栏控制器。──
  const topbar = RC.ui?.mountCollapsibleTopbar?.({
    bar, mount: root, pillId: "bw-top-pill", label: "伴读",
    defaultCollapsed: false,
    readCollapsed: sessionCollapsed,
    writeCollapsed: writeSessionCollapsed,
    // Ordinary websites may use their own generic `fs-mode` class. Only a
    // Reader PWA bridge authorizes the shared toolbar to recover that state.
    recoverFullscreen: !!window.__bwPwaBridge
  });
  const openTop = () => topbar?.setCollapsed(false, true);
  const closeTop = () => topbar?.setCollapsed(true, true);
  bar.querySelector("#bw-top-close").addEventListener("click", closeTop);

  const pinBtn = bar.querySelector("#bw-pin");
  let topbarPinned = false;
  let pinChangedLocally = false;
  const syncPin = () => pinBtn.classList.toggle("active", topbarPinned);
  pinBtn.addEventListener("click", () => {
    pinChangedLocally = true;
    topbarPinned = !topbarPinned;
    syncPin();
    if (topbarPinned) openTop();
    Promise.resolve(window.__bwExtensionStore?.set(TOPBAR_PREFS_KEY, {
      schema: 1,
      pinned: topbarPinned
    })).catch(() => RC.toast("顶栏固定状态未能保存"));
    RC.toast(topbarPinned ? "已钉住:每个页面自动展开顶栏" : "已取消钉住");
  });
  syncPin();
  Promise.resolve(window.__bwExtensionStore?.get(TOPBAR_PREFS_KEY)).then((value) => {
    if (pinChangedLocally) return;
    topbarPinned = value?.schema === 1 && value?.pinned === true;
    syncPin();
    if (topbarPinned) openTop();
  }).catch(() => {});

  bar.querySelector("#bw-asst-btn").addEventListener("click", () => RC.sidedrawer.toggle());
  const pwaBridge = window.__bwPwaBridge;
  const pwaCapability = (name, state) => {
    if (!pwaBridge) return true;   // 普通网页扩展模式继续使用自身的 DOM 实现。
    const caps = (state || pwaBridge.state)?.capabilities;
    return !!(caps && caps[name]);
  };
  const WEB_CAPABILITIES = new Set([
    'ruby', 'pageTranslate', 'ink', 'stickyNote', 'bookSearch'
  ]);
  const syncPwaCapabilityButtons = (state) => {
    bar.querySelectorAll("[data-pwa-capability]").forEach((button) => {
      const capability = button.dataset.pwaCapability;
      const mode = String((state || pwaBridge?.state)?.mode || '');
      const modes = String(button.dataset.pwaModes || '')
        .split(',').map(value => value.trim()).filter(Boolean);
      const allowed = pwaBridge
        ? pwaCapability(capability, state) &&
          (!modes.length || modes.includes(mode))
        : WEB_CAPABILITIES.has(capability);
      button.hidden = !allowed;
      button.disabled = !allowed;   // hidden 外再 fail closed，防脚本/辅助技术误触。
      button.setAttribute("aria-hidden", allowed ? "false" : "true");
    });
  };
  syncPwaCapabilityButtons();
  if (pwaBridge?.on) pwaBridge.on("READY", syncPwaCapabilityButtons);
  const ad = () => { try { return RC.adapter && RC.adapter(); } catch (_) { return null; } };
  const finfo = () => { try { return ad()?.fileInfo?.() || {}; } catch (_) { return {}; } };
  const hostAction = (name, payload, fallback) => RC.actions.run(name, payload || {}, fallback);
  const bwFetch = window.__bwReaderFetch || window.fetch.bind(window);
  const bookLocal = (action, payload = {}) => {
    if (!pwaBridge) return Promise.resolve(false);
    return pwaBridge.local(action, payload).catch(error => {
      RC.toast(error?.message || '书籍命令执行失败');
      return false;
    });
  };
  const positionButton = bar.querySelector("#bw-book-jump");
  const positionScrub = bar.querySelector("#bw-book-scrub");
  let lastBookLocation = null;
  const renderBookPosition = (location) => {
    if (!location || typeof location !== 'object') return false;
    lastBookLocation = location;
    const total = Math.max(0, Number(location.total || 0));
    const current = Math.max(
      1,
      Number(location.display || location.page || 0) ||
        (Number(location.index || 0) + 1)
    );
    positionButton.textContent = total ? `${current}/${total}` : String(current);
    if (total) {
      positionScrub.min = '1';
      positionScrub.max = String(total);
      positionScrub.value = String(Math.min(total, current));
    }
    return true;
  };
  const refreshBookPosition = async (stateOrLocation) => {
    if (!pwaBridge?.ready || !pwaCapability('navigation')) return;
    const direct = stateOrLocation?.currentLocation || stateOrLocation;
    if (renderBookPosition(direct)) return;
    if (renderBookPosition(pwaBridge.state?.currentLocation)) return;
    try {
      const context = await pwaBridge.context();
      renderBookPosition(context?.current_location || context?.currentLocation);
    } catch (_) {}
  };
  const jumpBookPosition = (value) => {
    const mode = String(pwaBridge?.state?.mode || '');
    const target = Math.max(1, Math.floor(Number(value) || 1));
    if (mode === 'pdf') return bookLocal('jump_page', {page:target});
    return bookLocal('jump_location', {
      location: { index: target - 1 }
    });
  };
  bar.querySelector("#bw-book-prev").addEventListener("click", () => bookLocal('change_page', {delta:-1}));
  bar.querySelector("#bw-book-next").addEventListener("click", () => bookLocal('change_page', {delta:1}));
  bar.querySelector("#bw-book-jump").addEventListener("click", () => {
    const mode = String(pwaBridge?.state?.mode || '');
    const current = lastBookLocation
      ? (Number(lastBookLocation.display || lastBookLocation.page || 0) ||
        Number(lastBookLocation.index || 0) + 1)
      : '';
    const value = prompt(mode === 'pdf' ? '跳转到页码：' : '跳转到章节位置：', String(current));
    if (value == null || !String(value).trim()) return;
    if (!Number.isFinite(Number(value)) || Number(value) < 1) return RC.toast('请输入有效位置');
    jumpBookPosition(value);
  });
  positionScrub.addEventListener('change', () => jumpBookPosition(positionScrub.value));
  bar.querySelector("#bw-book-zoom-out").addEventListener("click", () => bookLocal('zoom_by', {delta:-0.1}));
  bar.querySelector("#bw-book-fit").addEventListener("click", () => bookLocal('fit_width'));
  bar.querySelector("#bw-book-zoom-in").addEventListener("click", () => bookLocal('zoom_by', {delta:0.1}));
  bar.querySelector("#bw-book-layout").addEventListener("click", () => bookLocal('toggle_layout'));
  bar.querySelector("#bw-book-crop").addEventListener("click", () => bookLocal('toggle_crop'));
  bar.querySelector("#bw-book-fullscreen").addEventListener("click", () => bookLocal('toggle_fullscreen'));
  bar.querySelector("#bw-book-settings").addEventListener("click", () => bookLocal('open_settings'));
  bar.querySelector("#bw-book-favorite").addEventListener("click", () => bookLocal('open_favorite'));
  bar.querySelector("#bw-book-user-page").addEventListener("click", () => bookLocal('create_user_page'));
  if (pwaBridge) {
    bar.querySelector('h1').style.cursor = 'pointer';
    bar.querySelector('h1').title = '返回书架';
    bar.querySelector('h1').addEventListener('click', () => location.assign('/pdf/'));
    pwaBridge.on?.('READY', refreshBookPosition);
    pwaBridge.on?.('LOCATION', renderBookPosition);
    refreshBookPosition();
  }
  const syncDecorations = s => {
    if (!s) return;
    bar.querySelector('#ruby-toggle').classList.toggle('active', !!s?.ruby);
    bar.querySelector('#pagetr-toggle').classList.toggle('active', !!s?.translate);
  };
  const runDecoration = (name, fallback) => {
    try {
      const out = hostAction(name, {}, fallback);
      if (out && typeof out.then === 'function') out.then(syncDecorations).catch(e => RC.toast(e.message));
      else syncDecorations(out);
    } catch (e) { RC.toast(e.message || '阅读装饰功能尚未就绪'); }
  };
  bar.querySelector("#ruby-toggle").addEventListener("click", () => {
    if (!pwaCapability("ruby")) return;
    runDecoration('reading.ruby.toggle', () => window.__bwWebDecorations?.toggleRuby?.());
  });
  bar.querySelector("#pagetr-toggle").addEventListener("click", () => {
    if (!pwaCapability("pageTranslate")) return;
    runDecoration('translation.page.toggle', () => window.__bwWebDecorations?.toggleTranslate?.());
  });
  bar.querySelector("#bw-ink-btn").addEventListener("click", () => {
    if (!pwaCapability("ink")) return;
    try {
      const result = hostAction('ink.toggle', {}, () => {
        if (window.__bwWebInk?.toggle) {
          return {ok:true, active:window.__bwWebInk.toggle()};
        }
        throw new Error('绘图模块尚未就绪');
      });
      const syncInkButton = value => {
        const active = value && typeof value === 'object' && 'active' in value
          ? !!value.active
          : !!value;
        bar.querySelector('#bw-ink-btn').classList.toggle('active', active);
      };
      if (result && typeof result.then === 'function') {
        result.then(syncInkButton).catch(e => RC.toast(e.message));
      } else {
        syncInkButton(result);
      }
    } catch (e) { RC.toast(e.message || '绘图模块尚未就绪'); }
  });
  bar.querySelector("#bw-note-btn").addEventListener("click", () => {
    if (!pwaCapability("stickyNote")) return;
    try {
      const result = hostAction('note.create', {}, () => {
        if (window.__bwWebNotes?.create) return window.__bwWebNotes.create();
        throw new Error('便签模块尚未就绪');
      });
      if (result && typeof result.then === 'function') result.catch(e => RC.toast(e.message));
    } catch (e) { RC.toast(e.message || '便签模块尚未就绪'); }
  });
  bar.querySelector("#bw-search-btn").addEventListener("click", () => {
    if (pwaBridge) {
      if (!pwaCapability("bookSearch")) return;
      pwaBridge.local('open_search').catch(e => RC.toast(e.message));
      return;
    }
    const q = prompt('在当前网页查找：', ''); if (q) window.find(q, false, false, true, false, true, false);
  });
  bar.querySelector("#bw-set-btn").addEventListener("click", () => {
    // 完整 5-tab 设置面板(rc-settings)为主入口——此前被 openModelSettings 短路,网页只见 AI 模型浮层。
    // host:'web' 让 PDF/EPUB 专属区块自动隐藏;AI tab 内嵌的就是同一张模型配置表,不丢功能。
    if (RC.settings?.open) RC.settings.open({
      host: 'web', keys: { tab: 'bw-set-tab' },
      grammarFile: 'web:__grammar__',   // 与 content.js grammar() analyze 的 file 同一身份 → KG 启用列表渲染 + 分析门槛对齐
      onGrammarView: (v) => { try { const p = window.__bwShadow.getElementById('bw-grammar-list'); if (p && RC.grammar) RC.grammar.setViewMode(p, v, 'eph-grammar-view'); } catch (_) {} },   // 改显示方式即时重渲抽屉里已有的网页语法块 + 落盘 eph-grammar-view
      onHlColors: () => { try { window.__bwRenderHlSwatches && window.__bwRenderHlSwatches(); } catch (_) {} }   // 高亮色增删 → 重渲选区工具条色板
    });
    else if (RC.assistant?.openModelSettings) RC.assistant.openModelSettings();
  });

  async function loadVocab() {
    const box = shadowEl.getElementById('bw-vocab-list'); if (!box) return;
    box.innerHTML = '<div class="bw-empty">加载中…</div>';
    try {
      const f = finfo(), r = await bwFetch('/pdf/api/vocab-list?file=' + encodeURIComponent(f.file || '') + '&scope=all'), d = await r.json(), items = d.items || [];
      if (!items.length) { box.innerHTML = '<div class="bw-empty">单词库为空</div>'; return; }
      box.innerHTML = '';
      items.forEach(it => {
        const row = document.createElement('div'); row.className = 'bw-vocab-item';
        row.innerHTML = '<b>' + RC.esc(it.lemma || it.word || '') + '</b><small>' + RC.esc(it.zh || it.translation || it.mastery_label || '') + '</small>';
        row.onclick = () => RC.wordpop.show({word:it.lemma || it.word, ctx:'', file:f.file || '', langs:f.langs || [], rect:row.getBoundingClientRect(), showAnki:true});
        box.appendChild(row);
      });
    } catch (e) { box.innerHTML = '<div class="bw-empty" style="color:#ef4444">加载失败：' + RC.esc(e.message) + '</div>'; }
  }
  async function loadKg() {
    const box = shadowEl.getElementById('bw-kg-list'); if (!box) return;
    box.innerHTML = '<div class="bw-empty">加载中…</div>';
    try {
      const f = finfo(), s = ad()?.captureSelection?.(), page = s?.page || s?.anchor?.page || 0;
      if (!f.file || !page) { box.innerHTML = '<div class="bw-empty">普通网页暂无书页 KG；PDF 选区会自动带当前页。</div>'; return; }
      const d = await (await bwFetch('/pdf/api/page-nodes?file=' + encodeURIComponent(f.file) + '&page=' + page)).json();
      RC.knowledge.renderInto(box, d.nodes || []);
    } catch (e) { box.innerHTML = '<div class="bw-empty" style="color:#ef4444">加载失败</div>'; }
  }
  function removePwaHighlightDom(mode, id) {
    const highlightId = String(id || '');
    if (!highlightId) return false;
    const selector = mode === 'pdf'
      ? '.hl-saved[data-id]'
      : (mode === 'html' ? 'mark.rc-html-hl[data-hid]' : 'mark.ep-hl[data-id]');
    let removed = false;
    document.querySelectorAll(selector).forEach(node => {
      const nodeId = mode === 'html' ? node.dataset.hid : node.dataset.id;
      if (String(nodeId || '') !== highlightId) return;
      removed = true;
      if (mode === 'pdf') {
        node.remove();
        return;
      }
      // EPUB/HTML 的高亮是包住正文的 mark；只删节点会连正文一起删掉，
      // 必须与各自宿主的 unapply/unwarp 语义一致，把子节点移回原父节点。
      const parent = node.parentNode;
      if (!parent) return;
      while (node.firstChild) parent.insertBefore(node.firstChild, node);
      parent.removeChild(node);
      parent.normalize?.();
    });
    return removed;
  }
  async function syncPwaHighlightRemoval(mode, highlight) {
    const id = String(highlight?.id || '');
    if (!id) return false;
    // DELETE 已由扩展完成；这个动作只让页面世界清自己的内存与叠层，绝不再次写后端。
    try {
      const result = await pwaBridge.local('remove_highlight', {id});
      return !!(result && result.ok === true);
    } catch (error) {
      // 仅兼容尚未带 remove_highlight 白名单的旧 PWA bundle；其他宿主错误不能
      // 冒充成功。旧宿主只能清当前可见 DOM，下一次完整加载仍以已删除的存档为准。
      if (!/不允许.*(?:本地)?命令|不允许本地命令/.test(String(error?.message || ''))) return false;
      return removePwaHighlightDom(mode, id);
    }
  }
  async function deletePwaHighlight(endpoint, file, mode, highlight) {
    try {
      const response = await bwFetch(
        endpoint + '?file=' + encodeURIComponent(file || '') +
          '&id=' + encodeURIComponent(highlight?.id || ''),
        {method:'DELETE'}
      );
      let payload = null;
      try { payload = await response.json(); }
      catch (_) { RC.toast('删除未确认：响应无法解析'); return false; }
      if (!response.ok || !payload || payload.ok !== true) {
        RC.toast('删除失败：' + (payload?.error || (response.ok ? '服务未确认' : ('HTTP ' + response.status))));
        return false;
      }
      const projected = await syncPwaHighlightRemoval(mode, highlight);
      if (projected !== true) {
        RC.toast('删除已写入，但页面叠层未确认刷新');
        return false;
      }
      RC.toast('已删除');
      return true;
    } catch (error) {
      RC.toast('删除未确认：' + (error?.message || '无响应'));
      return false;
    }
  }
  async function loadHighlights() {
    const box = shadowEl.getElementById('bw-hl-list'); if (!box) return;
    box.innerHTML = '<div class="bw-empty">加载中…</div>';
    if (!pwaBridge) {
      const hs = window.__bwWebHighlights?.list?.() || [];
      RC.highlight.renderList(box, hs, {emptyHtml:'当前网页还没有高亮。',
        onJump:h=>window.__bwWebHighlights?.jump?.(h.id),
        onDelete:h=>window.__bwWebHighlights?.remove?.(h.id)}); return;
    }
    try {
      const mode = String(pwaBridge?.state?.mode || 'pdf');
      const endpoint = (mode === 'epub' || mode === 'favorite')
        ? '/pdf/api/epub-highlights'
        : (mode === 'html' ? '/pdf/api/html-highlights' : '/pdf/api/highlights');
      const f = finfo(), d = await (await bwFetch(endpoint + '?file=' + encodeURIComponent(f.file || ''))).json();
      RC.highlight.renderList(box, d.highlights || [], {
        emptyHtml:'这本书还没有高亮。',
        onJump:h => {
          if (mode === 'pdf') return pwaBridge.local('jump_page', {page:h.page}).catch(e => RC.toast(e.message));
          const location = h.anchor && typeof h.anchor === 'object'
            ? h.anchor
            : { section: h.section ?? h.page ?? 0 };
          return pwaBridge.local('jump_location', {location}).catch(e => RC.toast(e.message));
        },
        onDelete:h => deletePwaHighlight(endpoint, f.file || '', mode, h)
      });
    } catch (e) { box.innerHTML = '<div class="bw-empty" style="color:#ef4444">加载失败</div>'; }
  }
  async function loadToc() {
    const box = shadowEl.getElementById('bw-toc-list'); if (!box) return;
    box.innerHTML = '';
    if (!pwaBridge) {
      const hs = Array.from(document.querySelectorAll('h1,h2,h3,h4')).filter(x => !window.__bwReaderHost.contains(x)).slice(0, 160);
      if (!hs.length) { box.innerHTML = '<div class="bw-empty">当前网页没有标题大纲</div>'; return; }
      hs.forEach(h => { const b=document.createElement('button'); b.className='bw-toc-item'; b.style.paddingLeft=(8+(Number(h.tagName.slice(1))-1)*12)+'px'; b.textContent=h.textContent.trim(); b.onclick=()=>{h.scrollIntoView({behavior:'smooth',block:'start'});RC.sidedrawer.afterJump?.();}; box.appendChild(b); });
      return;
    }
    // 目录 API 目前只定义了 PDF 页码语义。EPUB/HTML/favorite 即使宿主误报
    // `toc` 能力，也不能套用 PDF endpoint 和 jump_page；没有统一契约前必须
    // fail closed，避免把用户跳到错误位置。
    if (String(pwaBridge.state?.mode || 'pdf') !== 'pdf') {
      box.innerHTML = '<div class="bw-empty">当前书籍宿主尚未公开统一目录接口。</div>';
      return;
    }
    try {
      const f=finfo(), d=await (await bwFetch('/pdf/api/toc?file='+encodeURIComponent(f.file||'')+'&entries=1')).json(), es=d.entries||[];
      if (!es.length) { box.innerHTML='<div class="bw-empty">这本书还没有目录</div>'; return; }
      es.forEach(it=>{const b=document.createElement('button');b.className='bw-toc-item';b.style.paddingLeft=(8+Math.max(0,(it.level||1)-1)*14)+'px';b.textContent=it.title||'';b.onclick=()=>pwaBridge.local('jump_page',{page:it.page}).catch(e=>RC.toast(e.message));box.appendChild(b);});
    } catch(e){box.innerHTML='<div class="bw-empty" style="color:#ef4444">目录加载失败</div>';}
  }

  // ── 普通网页布局接管：关闭“悬浮显示”时，真实网页根节点让出侧栏宽度并触发 responsive 重排。──
  // 用属性样式而非覆盖网站原有 inline style；恢复悬浮/关闭侧栏时只移除本扩展自己的属性。
  const pageLayoutStyle = document.createElement('style');
  pageLayoutStyle.id = 'bw-side-page-layout';
  pageLayoutStyle.textContent = 'html[data-bw-side-docked="1"]{width:calc(100% - var(--bw-side-reserve))!important;max-width:calc(100% - var(--bw-side-reserve))!important;min-width:0!important;box-sizing:border-box!important}';
  (document.head || document.documentElement).appendChild(pageLayoutStyle);
  let pageLayoutRaf = 0, pageLayoutResizeOnFrame = false;
  let pageLayoutPreviewWidth = null;
  const applyPageSideLayout = (commitResize = false, previewWidth = null) => {
    if (commitResize) pageLayoutResizeOnFrame = true;
    const numericPreview = Number(previewWidth);
    if (Number.isFinite(numericPreview) && numericPreview > 0) {
      pageLayoutPreviewWidth = numericPreview;
    }
    // pointermove 可能比刷新率更快。反复 cancel 会让网页预留宽度的回调一直
    // 饿到 pointerup，表现为侧栏自己在动、正文却最后一刻才跳。每帧只排一个
    // 回调即可；回调执行时读取最新侧栏宽度，天然是 latest-wins。
    if (pageLayoutRaf) return;
    pageLayoutRaf = requestAnimationFrame(() => {
      pageLayoutRaf = 0;
      const notifyResize = pageLayoutResizeOnFrame;
      pageLayoutResizeOnFrame = false;
      const preview = pageLayoutPreviewWidth;
      pageLayoutPreviewWidth = null;
      const docked = RC.sidedrawer.isOpen() && !RC.sidedrawer.getFloating();
      if (docked) {
        // persist=false 的拖拽预览不会写 localStorage；必须优先使用本帧传入
        // 的宽度，否则正文永远读到松手前的旧值。
        const w = preview || RC.sidedrawer.getWidth?.() ||
          side.getBoundingClientRect().width || 360;
        document.documentElement.style.setProperty('--bw-side-reserve', Math.round(w) + 'px');
        document.documentElement.dataset.bwSideDocked = '1';
      } else {
        delete document.documentElement.dataset.bwSideDocked;
        document.documentElement.style.removeProperty('--bw-side-reserve');
      }
      // 拖宽预览只改 CSS，让浏览器实时重排；合成 resize 只在 pointerup/其它提交动作发一次。
      if (notifyResize) {
        try { window.dispatchEvent(new Event('resize')); } catch (_) {}
      }
    });
  };

  // ── 抽屉 init:接法与 EPUB 阅读器一致；外观、宽度和 PWA 使用同一份 rc-sidedrawer。──
  const sideIcon = (paths) => '<svg class="si" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">' + paths + '</svg>';
  RC.sidedrawer.init({
    handleLabel: "伴读 · 助手",
    defaultTab: "asst",
    tabs: [
      {name:'asst',label:'助手',icon:sideIcon('<path d="M12 4l1.4 4.2L18 9.6l-4.6 1.4L12 16l-1.4-4.6L6 9.6l4.6-1.4L12 4z"/><path d="M18.6 14.5l.6 1.7 1.7.6-1.7.6-.6 1.7-.6-1.7-1.7-.6 1.7-.6.6-1.7z"/>')},
      {name:'vocab',label:'单词本',icon:sideIcon('<path d="M6 4h11a1 1 0 0 1 1 1v15H8a2 2 0 0 1-2-2V4z"/><path d="M6 4a2 2 0 0 0-2 2v12a2 2 0 0 1 2-2h12"/>')},
      {name:'kg',label:'知识点',icon:sideIcon('<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1.2" fill="currentColor" stroke="none"/><circle cx="4.5" cy="12" r="1.2" fill="currentColor" stroke="none"/><circle cx="4.5" cy="18" r="1.2" fill="currentColor" stroke="none"/>')},
      {name:'hl',label:'高亮',icon:sideIcon('<path d="M4 20h6M14 4l6 6-8.5 8.5H7v-4.5L14 4z"/>')},
      {name:'toc',label:'目录',icon:sideIcon('<path d="M4 6h16M4 12h16M4 18h16"/>')},
      {name:'grammar',label:'语法',icon:sideIcon('<path d="M5 4v6a3 3 0 0 0 3 3h8M19 4v6a3 3 0 0 1-3 3"/><circle cx="5" cy="3.5" r="1.4" fill="currentColor" stroke="none"/><circle cx="19" cy="3.5" r="1.4" fill="currentColor" stroke="none"/><circle cx="12" cy="20" r="1.4" fill="currentColor" stroke="none"/><path d="M12 13v5"/>')}
    ],
    onTab: (name) => { if(name==='vocab')loadVocab();else if(name==='kg')loadKg();else if(name==='hl')loadHighlights();else if(name==='toc')loadToc(); },
    onReflow: () => applyPageSideLayout(true),
    onWidthPreview: (width) => applyPageSideLayout(false, width),
    onWidthChange: (width) => applyPageSideLayout(true, width),
  });
  // 任意网页没有可挤压的正文 → 首次默认「悬浮显示」(用户改过则尊重持久化)
  if (lsGet("eph-gp-floating") === null) RC.sidedrawer.setFloating(true);
  applyPageSideLayout();

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
  document.dispatchEvent(new CustomEvent('bw:shell-ready'));
})();
