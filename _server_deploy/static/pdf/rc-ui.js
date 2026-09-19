/* rc-ui.js — Reader UI Kit 地基：共享视觉令牌与无布局耦合的基础组件。
 * 只决定颜色、字体、圆角、阴影和交互状态；定位、尺寸、锚点、存储仍归各 reader/adapter。
 * 在普通页面中 :root 生效；经扩展 document facade 包装后，:host/#bw-root 及卡片 pin shadow 同步生效。 */
(function () {
  if (!window.RC) window.RC = {};
  if (window.RC.ui) return;
  var RC = window.RC;

  // 视觉语言：iOS（用户 2026-09-20 拍板「改成苹果的视觉效果」）。
  //
  // 取的是 Apple 深色模式的系统值，不是我调出来的近似色：
  //   背景三档 = systemBackground / secondary / tertiary（#000 / #1C1C1E / #2C2C2E）
  //   文字三档 = label / secondaryLabel / tertiaryLabel（白 / 62% / 38%）
  //   分隔线   = separator（半透明，叠在什么上跟着变，不是一个固定灰）
  //   强调色   = 深色版 systemBlue / systemCyan / systemGreen / systemRed
  // ⚠ 半透明是**有意的**：苹果的层级靠透明度和材质表达，不靠不同的固定灰。
  //   把它们换回不透明色会让毛玻璃层级整个塌掉。
  var TOKENS = {
    bgCanvas: '#000000', bgSurface: '#1c1c1e', bgRaised: '#2c2c2e',
    bgActive: 'rgba(10,132,255,.26)', bgHover: 'rgba(120,120,128,.24)',
    bgPopover: 'rgba(30,30,32,.78)', bgField: '#1c1c1e', bgControl: 'rgba(120,120,128,.20)',
    border: 'rgba(84,84,88,.62)', borderAccent: 'rgba(10,132,255,.58)',
    borderPopover: 'rgba(255,255,255,.14)', borderControl: 'rgba(84,84,88,.46)',
    text: '#ffffff', textStrong: '#ffffff',
    textMuted: 'rgba(235,235,245,.62)', textDim: 'rgba(235,235,245,.38)',
    accent: '#0a84ff', accentCyan: '#64d2ff', success: '#30d158',
    danger: 'rgba(255,69,58,.20)', dangerText: '#ff453a',
    warn: '#ff9f0a', purple: '#bf5af2'
  };
  var injected = false;
  function inject() {
    if (injected) return; injected = true;
    var s = document.createElement('style'); s.id = 'rc-ui-kit';
    s.textContent = `
:host,:root,#bw-root,#bw-pin-root{
  color-scheme:dark;
  --rc-font-ui:-apple-system,BlinkMacSystemFont,"SF Pro Text","PingFang SC",system-ui,sans-serif;
  --rc-bg-canvas:#000000;--rc-bg-surface:#1c1c1e;--rc-bg-raised:#2c2c2e;
  --rc-bg-active:rgba(10,132,255,.26);--rc-bg-hover:rgba(120,120,128,.24);
  --rc-bg-popover:rgba(30,30,32,.78);--rc-bg-field:#1c1c1e;--rc-bg-control:rgba(120,120,128,.20);
  --rc-border:rgba(84,84,88,.62);--rc-border-accent:rgba(10,132,255,.58);
  --rc-border-popover:rgba(255,255,255,.14);--rc-border-control:rgba(84,84,88,.46);
  --rc-text:#ffffff;--rc-text-strong:#ffffff;
  --rc-text-muted:rgba(235,235,245,.62);--rc-text-dim:rgba(235,235,245,.38);
  --rc-accent:#0a84ff;--rc-accent-cyan:#64d2ff;--rc-success:#30d158;
  --rc-danger:rgba(255,69,58,.20);--rc-danger-text:#ff453a;
  --rc-warn:#ff9f0a;--rc-purple:#bf5af2;
  /* 材质：iOS 的四档毛玻璃。用 backdrop-filter 实现，所以底色必须是半透明的。 */
  --rc-material-ultrathin:rgba(30,30,32,.55);--rc-material-thin:rgba(30,30,32,.68);
  --rc-material-regular:rgba(30,30,32,.80);--rc-material-thick:rgba(30,30,32,.92);
  --rc-blur:saturate(180%) blur(20px);
  /* 圆角：贴 iOS 的档位（按钮 ~9、弹窗 13、卡片 18） */
  --rc-radius-xs:5px;--rc-radius-sm:7px;--rc-radius-md:9px;--rc-radius-lg:12px;--rc-radius-popover:13px;--rc-radius-xl:14px;--rc-radius-card:18px;
  /* 阴影：软而宽。苹果靠层级和材质表达深度，不靠硬描边。 */
  --rc-shadow-float:0 4px 14px rgba(0,0,0,.36);--rc-shadow-pop:0 12px 34px rgba(0,0,0,.46);--rc-shadow-panel:0 10px 28px rgba(0,0,0,.42);
  /* 动效：UIKit 那条平滑曲线（弹出/收起都用它，出场再快一点） */
  --rc-motion-fast:.2s;--rc-motion-normal:.35s;
  --rc-ease:cubic-bezier(.32,.72,0,1);--rc-ease-out:cubic-bezier(.16,1,.3,1);
  /* HIG 的 44pt 最小命中区 */
  --rc-hit:44px;
}
/* 系统级观感：抗锯齿、禁止 iOS 自动放大字号、点击不闪灰块。 */
:root,#bw-root{-webkit-font-smoothing:antialiased;-webkit-text-size-adjust:100%}
.rc-ui-surface{background:var(--rc-bg-surface);border:1px solid var(--rc-border);color:var(--rc-text)}
/* 卡片与弹窗改用材质：半透明 + 背景模糊，底下的内容会透上来一点。
   这是 iOS 观感里最容易辨认的一件事 —— 层级来自材质，而不是更亮的一块灰。 */
.rc-ui-card{background:var(--rc-material-regular);backdrop-filter:var(--rc-blur);-webkit-backdrop-filter:var(--rc-blur);border:.5px solid var(--rc-border-popover);border-radius:var(--rc-radius-card);box-shadow:var(--rc-shadow-panel);color:var(--rc-text)}
.rc-ui-popover{background:var(--rc-material-thick);backdrop-filter:var(--rc-blur);-webkit-backdrop-filter:var(--rc-blur);border:.5px solid var(--rc-border-popover);border-radius:var(--rc-radius-popover);box-shadow:var(--rc-shadow-pop);color:var(--rc-text)}
/* 按钮：iOS 的次级按钮是**填充无描边**（tertiarySystemFill），按下去缩一点。 */
.rc-ui-button{font-family:var(--rc-font-ui);background:var(--rc-bg-control);border:0;border-radius:var(--rc-radius-md);color:var(--rc-accent);cursor:pointer;touch-action:manipulation;-webkit-tap-highlight-color:transparent;transition:background var(--rc-motion-fast) var(--rc-ease),transform var(--rc-motion-fast) var(--rc-ease)}
.rc-ui-button:hover{background:var(--rc-bg-hover)}
.rc-ui-button:active{transform:scale(.96)}
.rc-ui-button.active{background:var(--rc-bg-active);color:#fff}
/* 触摸设备上给足 44pt —— HIG 的最小命中区。鼠标设备不强加，免得工具条变胖。 */
@media (pointer:coarse){.rc-ui-button{min-height:var(--rc-hit);min-width:var(--rc-hit)}}
.rc-ui-input{box-sizing:border-box;font-family:var(--rc-font-ui);background:var(--rc-bg-control);border:0;border-radius:var(--rc-radius-md);color:var(--rc-text)}
.rc-ui-input:focus{outline:2px solid var(--rc-border-accent);outline-offset:-2px}
/* 需要材质的地方直接挂这个类。 */
.rc-ui-material{background:var(--rc-material-regular);backdrop-filter:var(--rc-blur);-webkit-backdrop-filter:var(--rc-blur)}
.rc-ui-material-thin{background:var(--rc-material-thin);backdrop-filter:var(--rc-blur);-webkit-backdrop-filter:var(--rc-blur)}
.rc-ui-dragging{will-change:transform;transition:none!important;filter:drop-shadow(0 16px 25px rgba(0,0,0,.42))}
/* 4B：PDF / EPUB / HTML / 扩展共用同一套可收起顶栏。宿主只提供 bar 和挂载点。 */
.rc-topbar-collapsible{transform-origin:top center;transition:height var(--rc-motion-normal) var(--rc-ease),transform var(--rc-motion-normal) var(--rc-ease),opacity var(--rc-motion-fast) ease!important}
.rc-topbar-collapsible.rc-topbar-collapsed{flex-basis:0!important;height:0!important;min-height:0!important;padding-top:0!important;padding-bottom:0!important;border-bottom-width:0!important;transform:translateY(-105%)!important;opacity:0!important;overflow:hidden!important;pointer-events:none!important}
.rc-topbar-pill{position:fixed;left:50%;top:calc(48px + env(safe-area-inset-top,0px));transform:translate(-50%,-6px);z-index:139;min-width:68px;height:24px;padding:0 13px;border:.5px solid var(--rc-border-popover);border-top:0;border-radius:0 0 12px 12px;background:var(--rc-material-regular);color:rgba(235,242,255,.88);font:11px/1 var(--rc-font-ui);letter-spacing:.08em;cursor:pointer;box-shadow:0 5px 18px rgba(0,0,0,.32);backdrop-filter:blur(12px) saturate(145%);-webkit-backdrop-filter:blur(12px) saturate(145%);touch-action:manipulation;-webkit-tap-highlight-color:transparent;transition:top var(--rc-motion-normal) var(--rc-ease),transform var(--rc-motion-normal) var(--rc-ease),background var(--rc-motion-fast),color var(--rc-motion-fast)}
.rc-topbar-pill:hover{color:#fff;background:linear-gradient(135deg,rgba(255,255,255,.16),rgba(255,255,255,.07)),rgba(83,72,156,.72)}
.rc-topbar-pill[data-collapsed="1"]{top:0;transform:translate(-50%,0);min-width:86px}
body.fs-mode .rc-topbar-pill{display:none!important}
/* 2A：共享选区浮条只规定外观；PDF 内容坐标 / EPUB offset / 网页 DOM rect 仍由宿主适配。 */
/* 选区浮条：对标 iOS 的编辑菜单 —— 材质底、细描边、更圆。 */
.rc-selection-toolbar{background:var(--rc-material-thick)!important;backdrop-filter:var(--rc-blur);-webkit-backdrop-filter:var(--rc-blur);border:.5px solid var(--rc-border-popover)!important;border-radius:var(--rc-radius-popover)!important;box-shadow:var(--rc-shadow-pop)!important;color:var(--rc-text)!important}
`;
    document.head.appendChild(s);
  }
  function placeSelectionToolbar(el, rect, opts) {
    if (!el) return null; opts = opts || {}; rect = rect || {};
    el.classList.add('rc-selection-toolbar');
    el.style.position = 'fixed'; el.style.right = 'auto'; el.style.bottom = 'auto'; el.style.transform = 'none';
    var box = el.getBoundingClientRect(), gap = opts.gap == null ? 8 : Number(opts.gap);
    var vw = Math.max(1, window.innerWidth || document.documentElement.clientWidth || 1);
    var vh = Math.max(1, window.innerHeight || document.documentElement.clientHeight || 1);
    var minX = opts.margin == null ? 6 : Number(opts.margin), maxX = vw - minX;
    try {
      if (document.body.classList.contains('ep-side-open') && !(RC.sidedrawer && RC.sidedrawer.getFloating && RC.sidedrawer.getFloating())) {
        maxX -= (RC.sidedrawer && RC.sidedrawer.getWidth ? RC.sidedrawer.getWidth() : 0);
      }
    } catch (e) {}
    if (maxX - minX < Math.min(220, box.width)) maxX = vw - minX;
    var left = Number(rect.left); if (!isFinite(left)) left = (maxX - box.width + minX) / 2;
    var bottom = Number(rect.bottom), topEdge = Number(rect.top);
    var top = isFinite(bottom) ? bottom + gap : vh - box.height - 18;
    left = Math.max(minX, Math.min(left, maxX - box.width));
    if (top + box.height > vh - minX && isFinite(topEdge)) top = topEdge - box.height - gap;
    top = Math.max(minX, Math.min(top, vh - box.height - minX));
    el.style.left = Math.round(left) + 'px'; el.style.top = Math.round(top) + 'px';
    return { left: left, top: top, width: box.width, height: box.height };
  }

  // EPUB / HTML 当前一致的查词分流规则。PDF 仍保留自己的英文词规则，避免把尚未裁决的
  // 日文选区行为一并改掉。
  function isDictionaryWord(value) {
    var text = value || '';
    return text.length <= 30 && !/\s/.test(text) &&
      (/^[A-Za-z][A-Za-z'’\-]*$/.test(text) || /[぀-ヿ]/.test(text));
  }

  // caretRangeFromPoint / caretPositionFromPoint 返回的是「离坐标最近的插入点」，并不保证
  // 坐标真的落在文字上。所有 DOM 文字宿主先生成候选 Range，再用本函数按可见行盒验收，
  // 避免点段落空白、词间空隙时吸附到最近单词。必须逐个 client rect 判断，不能用跨行
  // range 的 union bounding rect（其中会包含大片并非文字的空白）。
  function rangeHitTest(range, x, y, opts) {
    opts = opts || {};
    x = Number(x); y = Number(y);
    if (!range || !isFinite(x) || !isFinite(y) || typeof range.getClientRects !== 'function') return false;
    var kind = String(opts.pointerType || 'mouse').toLowerCase();
    var def = kind === 'touch' ? { x: 3, y: 5 } : (kind === 'pen' ? { x: 2, y: 3 } : { x: 1, y: 2 });
    var both = opts.slop == null ? null : Number(opts.slop);
    var sx = opts.slopX == null ? (both == null ? def.x : both) : Number(opts.slopX);
    var sy = opts.slopY == null ? (both == null ? def.y : both) : Number(opts.slopY);
    sx = isFinite(sx) ? Math.max(0, Math.min(12, sx)) : def.x;
    sy = isFinite(sy) ? Math.max(0, Math.min(12, sy)) : def.y;
    var rects;
    try { rects = range.getClientRects(); } catch (e) { return false; }
    if (!rects) return false;
    for (var i = 0; i < rects.length; i++) {
      var r = rects[i];
      var left = Number(r.left), top = Number(r.top), right = Number(r.right), bottom = Number(r.bottom);
      if (![left, top, right, bottom].every(isFinite) || right <= left || bottom <= top) continue;
      if (x >= left - sx && x <= right + sx && y >= top - sy && y <= bottom + sy) return true;
    }
    return false;
  }

  function mountCollapsibleTopbar(opts) {
    opts = opts || {};
    var bar = typeof opts.bar === 'string' ? document.querySelector(opts.bar) : opts.bar;
    if (!bar) return null;
    if (bar.__rcTopbar) return bar.__rcTopbar;
    var mount = opts.mount || bar.parentNode || document.body;
    var key = opts.storageKey || ('rc-topbar-collapsed:' + (bar.id || 'reader'));
    var pill = document.createElement('button'); pill.type = 'button'; pill.className = 'rc-topbar-pill';
    if (opts.pillId) pill.id = opts.pillId;
    if (opts.nativePill) pill.dataset.rcNativeTopbarPill = '1';
    pill.title = '展开 / 收起阅读工具栏';
    mount.appendChild(pill); bar.classList.add('rc-topbar-collapsible');
    var read = function () {
      if (typeof opts.readCollapsed === 'function') {
        try {
          var custom = opts.readCollapsed();
          if (custom === true || custom === false) return custom;
        } catch (e) {}
        return !!opts.defaultCollapsed;
      }
      if (opts.persist === false) return !!opts.defaultCollapsed;
      try { var v = localStorage.getItem(key); return v === null ? !!opts.defaultCollapsed : v === '1'; }
      catch (e) { return !!opts.defaultCollapsed; }
    };
    var persist = function (v) {
      if (typeof opts.writeCollapsed === 'function') {
        try { opts.writeCollapsed(!!v); } catch (e) {}
        return;
      }
      if (opts.persist === false) return;
      try { localStorage.setItem(key, v ? '1' : '0'); } catch (e) {}
    };
    // A document navigation cannot preserve a real Fullscreen API session. Older
    // builds nevertheless restored the visual fs-mode class from localStorage,
    // leaving the bar hidden while its expand pill appeared to do nothing.
    // Expanding is an explicit recovery action: remove only that stale visual
    // mode, never an actually active browser fullscreen session.
    function clearStaleFullscreen() {
      var doc = bar.ownerDocument || document;
      var browserFullscreen = false;
      try { browserFullscreen = !!(doc.fullscreenElement || doc.webkitFullscreenElement); } catch (e) {}
      if (browserFullscreen) return false;
      var cleared = false;
      try {
        [doc.documentElement, doc.body].forEach(function (node) {
          if (node && node.classList && node.classList.contains('fs-mode')) {
            node.classList.remove('fs-mode'); cleared = true;
          }
        });
      } catch (e) {}
      if (!cleared) return false;
      try { localStorage.setItem('pdf-fullscreen', '0'); } catch (e) {}
      try { localStorage.setItem('eph-fs-mode', '0'); } catch (e) {}
      try {
        var fsButton = doc.getElementById && (doc.getElementById('fs-toggle') || doc.getElementById('bw-book-fullscreen'));
        if (fsButton) fsButton.classList.remove('active');
      } catch (e) {}
      try { window.dispatchEvent(new CustomEvent('bw:topbar-fullscreen-recovered')); } catch (e) {}
      return true;
    }
    var collapsed = false;
    function setCollapsed(on, save) {
      if (!on && opts.recoverFullscreen !== false) clearStaleFullscreen();
      collapsed = !!on; bar.classList.toggle('rc-topbar-collapsed', collapsed);
      pill.dataset.collapsed = collapsed ? '1' : '0'; pill.textContent = collapsed ? '⌄ ' + (opts.label || '伴读') : '⌃';
      pill.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
      if (save !== false) persist(collapsed);
      try { if (typeof opts.onChange === 'function') opts.onChange(collapsed); } catch (e) {}
      try { window.dispatchEvent(new Event('resize')); } catch (e) {}
      return collapsed;
    }
    pill.addEventListener('click', function () { setCollapsed(!collapsed, true); });
    var api = { bar: bar, pill: pill, setCollapsed: setCollapsed, isCollapsed: function () { return collapsed; }, open: function () { return setCollapsed(false, true); }, close: function () { return setCollapsed(true, true); } };
    bar.__rcTopbar = api; setCollapsed(read(), false); return api;
  }

  function appendToolCard(host, opts) {
    opts = opts || {}; if (!host) return null;
    try {
      if (RC.voiceCard && RC.voiceCard.renderInflow) {
        var cid = opts.cid || (RC.voiceCard.mkCid && RC.voiceCard.mkCid()) || ('c' + Date.now().toString(36));
        var r = RC.voiceCard.renderInflow(host, { label: opts.label || '工具结果', type: opts.type || '#b9a8ff', icon: opts.icon || '工具', form: opts.form || 'full', cid: cid, mount: function (bd) { bd.innerHTML = opts.loadingHtml || '<span class="ep-spin"></span>'; } });
        if (r && r.el) {
          r.el.classList.add('rc-ui-tool-result');
          try {
            if (RC.voiceCard.pinBind) RC.voiceCard.pinBind(r.el, opts.label || '工具结果', function () { return (r.bd && r.bd.textContent) || ''; });
            if (RC.voiceCard.dragToDock) RC.voiceCard.dragToDock(r.el, function () {
              return { label: opts.label || '工具结果', kind: opts.kind || 'tool', raw: (r.bd && r.bd.innerHTML) || '', isHtml: true, text: (r.bd && r.bd.textContent) || '', cid: cid };
            });
          } catch (e2) {}
          return r.bd;
        }
      }
    } catch (e) {}
    var card = document.createElement('div'); card.className = 'ep-card rc-ui-tool-result';
    card.innerHTML = '<div class="h">' + (RC.esc ? RC.esc(opts.label || '工具结果') : String(opts.label || '工具结果')) + '</div><div class="c">' + (opts.loadingHtml || '<span class="ep-spin"></span>') + '</div>';
    host.appendChild(card); return card.querySelector('.c');
  }

  // 5V：选区“对话”不再另开结果窗口，而是把选中文字钉成助手上下文，进入同一条
  // 普通气泡 / 工具轮次 / 三态工具卡对话流。各宿主只负责提供 adapter 与抽屉挂载点。
  function openSelectionChat(text, context) {
    text = String(text || '').trim(); if (!text) return false;
    try {
      if (typeof window.__setFocusSel === 'function') window.__setFocusSel(text, /\$/.test(text) ? 'formula' : 'text');
      else {
        var ad = RC.adapter && RC.adapter(), host = ad && ad._host && ad._host.asst;
        if (host && host.setFocusSel) host.setFocusSel(text, 'text');
        else window.__focusSel = { text: text, sentence: String(context || '') };
      }
      if (RC.sidedrawer && RC.sidedrawer.open) RC.sidedrawer.open('asst');
      setTimeout(function () { var ta = document.getElementById('asst-ta'); if (ta) ta.focus(); }, 80);
      return true;
    } catch (e) { return false; }
  }

  window.RC.ui = {
    version: 'reader-ui/2', tokens: TOKENS,
    inject: inject,
    token: function (name) { return TOKENS[name]; },
    isDictionaryWord: isDictionaryWord,
    rangeHitTest: rangeHitTest,
    placeSelectionToolbar: placeSelectionToolbar,
    mountCollapsibleTopbar: mountCollapsibleTopbar,
    appendToolCard: appendToolCard,
    openSelectionChat: openSelectionChat
  };
  inject();
  function autoTopbars() {
    ['sel-toolbar','ep-sel','html-sel'].forEach(function (id) { var tb = document.getElementById(id); if (tb) tb.classList.add('rc-selection-toolbar'); });
    [['header','pdf'],['ep-top','epub'],['html-top','html']].forEach(function (it) {
      var el = document.getElementById(it[0]); if (!el || el.__rcTopbar) return;
      mountCollapsibleTopbar({ bar: el, mount: el.parentNode, storageKey: 'rc-topbar-collapsed:' + it[1], label: '伴读', defaultCollapsed: false, nativePill: true });
    });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', autoTopbars, { once: true }); else autoTopbars();
})();
