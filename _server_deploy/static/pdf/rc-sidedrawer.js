/* rc-sidedrawer.js — 统一控制层:EPUB 右侧「单抽屉 + 顶部 tab」（照搬 PDF 阅读器 #grammar-panel + #side-tabs + #side-handle）
 *
 * 目的:把 EPUB 之前散落的三个右侧入口(✦助手 #ep-ai / 🖍高亮列表 / ☰目录 + 单独的 rc-knowledge 知识点抽屉)
 *       合并成 PDF 阅读器那样的**一个**磨砂玻璃抽屉:右边缘把手打开 → 顶部 4 个 tab(助手 / 知识点 / 高亮 / 目录)切换。
 *
 * 设计原则(跟 rc-md / rc-result / rc-knowledge 等共享层一致):
 *   · 自包含:injectCss() 注入抽屉 / 把手 / tab / pane / 挤压 / 知识点卡 的全部 CSS(照搬 PDF #grammar-panel 类名风格,换 ep- 前缀);
 *   · opts 驱动:不读全局变量;tab 的「填充」由调用方 onTab(name) 回调负责(知识点→RC.knowledge.load、高亮→fetch、目录→buildToc 已填);
 *   · 抽屉本体 #ep-side + 4 个 .ep-side-pane 由**模板**提供(助手 pane 直接放从旧 #ep-ai 搬来的 #ep-ai-body/#ep-asst-quick/#ep-ai-input
 *     → 助手 sendChat/mic 逻辑零改动);本模块只**建把手 + tab 栏**并 prepend 进 #ep-side,再接开关/切 pane 逻辑。
 *     (#ep-side 不存在时 init 也会兜底创建,便于移植;但 EPUB 走模板路径。)
 *   · 被 onclick 调的挂 window.(本模块对外只走 RC.sidedrawer.*,无内联 onclick);幂等:重复 init 不重复建。
 *
 * z-index(在 EPUB 梯度内,照搬 rc-knowledge 的选择,**都 < #ep-sel(78) 选中工具栏、< #result-mask(200) 结果模态**):
 *   抽屉 #ep-side = 120 / 把手 #ep-side-handle = 130(逐字对照 PDF #grammar-panel=120 / #side-handle=130)。
 *
 * API
 *   RC.sidedrawer.init(opts)
 *     opts.tabs       [{name,label,icon}]  省则用内置 4 个(asst/kg/hl/toc);name 须与模板 .ep-side-pane[data-pane] 对应
 *     opts.handleLabel 把手竖排文字(默认「助手 · 知识点」)
 *     opts.defaultTab  首次/无记忆时的 tab(默认 'asst')
 *     opts.onTab(name) 切到某 tab 时回调(懒填充:知识点 load / 高亮 fetch 等)
 *     opts.onLayoutChange(willOpen) 开/关抽屉挤压正文前回调;返回 restore 函数则重排后调它(阅读位置保持,
 *       锚点机制在宿主侧——epub-html.js 记「顶部可见节 + 节内比例」,这里只管调用时序)
 *   RC.sidedrawer.open(tab?)  打开抽屉(tab 省则用上次记忆的 tab)
 *   RC.sidedrawer.close()
 *   RC.sidedrawer.afterJump()  跳转类操作(目录/AI 链接/上下文卡)后调:宽屏保持开,抽屉≥90vw 才收起
 *   RC.sidedrawer.toggle()
 *   RC.sidedrawer.setTab(name)  只切 pane(不改开关态),并触发 onTab
 *   RC.sidedrawer.isOpen()
 */
(function () {
  'use strict';
  var RC = (window.RC = window.RC || {});
  if (RC.sidedrawer) return;

  var _opts = {};
  var _injected = false;
  var _built = false;
  var _curTab = '';
  var LS_KEY = 'ep-side-tab';
  // 侧栏外观持久化键(照搬 PDF reader.src/18-grammar.js 的 pdf-gp-{floating,blur};EPUB 不按排版分键 → 简单全局键)
  var LS_FLOAT = 'eph-gp-floating', LS_BLUR = 'eph-gp-blur';

  function _lsGet(k, def) { try { var v = localStorage.getItem(k); return v === null ? def : v; } catch (e) { return def; } }
  function _lsSet(k, v) { try { localStorage.setItem(k, v); } catch (e) {} }

  // ── 侧栏外观:悬浮显示 + 背景模糊度(照搬 PDF _gpSetFloating / _gpSetBlur / _gpApplyAppearance)──
  // 悬浮显示 → body.ep-side-floating(配合上面 CSS:抽屉开时不挤压正文/顶栏);手搓版正文 #ep-content 让位↔覆盖,
  // epub.js 版 #ep-viewer 本就不被挤(避免 rendition.resize 卡顿)、悬浮只是再去掉顶栏 #ep-top 的挤压。开关两版都生效。
  // 背景模糊度 → --gp-blur(抽屉 backdrop-filter:blur(var(--gp-blur,20px)) 读它)。
  // 泛化点(PDF 迁入,2026-07-07):
  //   opts.appearanceKeys(name)→存储键(PDF 按排版分档 pdf-gp-*-{mode};缺省=eph-gp-* 全局单键,EPUB 不变)
  //   opts.mirrorOpenClass / mirrorFloatingClass→开关/悬浮时在 body 上镜像的旧类名(PDF=grammar-open/grammar-floating,
  //     让 pdf-styles.css 既有挤压/悬浮/结果框规则原样生效,消费方 JS 也不用改)
  //   opts.onReflow→布局重排回调(PDF=_scheduleRefit;缺省=派发合成 resize 给 epub.js)
  //   opts.tabButtons→tab 栏 per-tab 动作按钮(PDF 的「🗑 清空分析」只在 grammar tab 显示)
  function _akey(name, fallback) { try { if (typeof _opts.appearanceKeys === 'function') { var k = _opts.appearanceKeys(name); if (k) return k; } } catch (e) {} return fallback; }
  function _mirror(cls, on) { if (cls) { try { document.body.classList.toggle(cls, !!on); } catch (e) {} } }
  function getFloating() { return _lsGet(_akey('floating', LS_FLOAT), '0') === '1'; }
  var _rfT = null;
  function _reflow() {   // 挤压↔悬浮切换 / 开关抽屉 → 正文宽度变 → 重排(EPUB=合成 resize 给 epub.js;PDF 经 opts.onReflow 走 _scheduleRefit)
    clearTimeout(_rfT);
    _rfT = setTimeout(function () {
      if (typeof _opts.onReflow === 'function') { try { _opts.onReflow(); } catch (e) {} return; }
      try { window.dispatchEvent(new Event('resize')); } catch (e) {}
    }, 430);
  }
  function setFloating(on) {
    on = !!on;
    _lsSet(_akey('floating', LS_FLOAT), on ? '1' : '0');
    document.body.classList.toggle('ep-side-floating', on);
    _mirror(_opts.mirrorFloatingClass, on);
    _reflow();
  }
  function getBlur() { var n = parseInt(_lsGet(_akey('blur', LS_BLUR), '20'), 10); return isNaN(n) ? 20 : n; }
  function setBlur(px) {
    var n = parseInt(px, 10); if (isNaN(n)) n = 20; n = Math.max(0, Math.min(40, n));
    _lsSet(_akey('blur', LS_BLUR), String(n));
    document.documentElement.style.setProperty('--gp-blur', n + 'px');
  }
  function applyAppearance() {   // 启动即应用持久化外观(等价 PDF _gpApplyAppearance,init 时调;PDF 切排版后也调=按新档重应用)
    var f = getFloating();
    document.body.classList.toggle('ep-side-floating', f);
    _mirror(_opts.mirrorFloatingClass, f);
    document.documentElement.style.setProperty('--gp-blur', getBlur() + 'px');
  }

  // 内置默认 4 tab(图标照搬 PDF .si 描边 SVG 风格 + 25-assistant 的 sparkles)
  var DEFAULT_TABS = [
    { name: 'asst', label: '助手',
      icon: '<svg class="si" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4l1.4 4.2L18 9.6l-4.6 1.4L12 16l-1.4-4.6L6 9.6l4.6-1.4L12 4z"/><path d="M18.6 14.5l.6 1.7 1.7.6-1.7.6-.6 1.7-.6-1.7-1.7-.6 1.7-.6.6-1.7z"/></svg>' },
    { name: 'vocab', label: '单词本',
      icon: '<svg class="si" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M6 4h11a1 1 0 0 1 1 1v15H8a2 2 0 0 1-2-2V4z"/><path d="M6 4a2 2 0 0 0-2 2v12a2 2 0 0 1 2-2h12"/></svg>' },
    { name: 'kg', label: '知识点',
      icon: '<svg class="si" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1.2" fill="currentColor" stroke="none"/><circle cx="4.5" cy="12" r="1.2" fill="currentColor" stroke="none"/><circle cx="4.5" cy="18" r="1.2" fill="currentColor" stroke="none"/></svg>' },
    { name: 'hl', label: '高亮',
      icon: '<svg class="si" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20h6M14 4l6 6-8.5 8.5H7v-4.5L14 4z"/></svg>' },
    { name: 'toc', label: '目录',
      icon: '<svg class="si" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h16M4 12h16M4 18h16"/></svg>' }
  ];

  function injectCss() {
    if (_injected) return; _injected = true;
    var st = document.createElement('style'); st.id = 'rc-sidedrawer-css';
    st.textContent = `
/* 右侧统一抽屉:照搬 PDF #grammar-panel 磨砂玻璃滑出。默认挤压 → EPUB 正文留左侧可读 */
#ep-side{position:fixed;top:0;right:0;bottom:0;width:min(38vw,560px);display:flex;flex-direction:column;z-index:120;
  background:linear-gradient(135deg,rgba(255,255,255,0.08),rgba(255,255,255,0.03)),rgba(14,20,40,0.62);
  backdrop-filter:blur(var(--gp-blur,20px)) saturate(150%) brightness(1.05);-webkit-backdrop-filter:blur(var(--gp-blur,20px)) saturate(150%) brightness(1.05);
  border-left:1px solid rgba(255,255,255,0.20);
  box-shadow:-14px 0 48px rgba(0,0,0,0.45),inset 1px 0 0 rgba(255,255,255,0.16);
  transform:translateX(102%);transition:transform 0.4s cubic-bezier(.4,0,.2,1);touch-action:pan-y;padding-top:env(safe-area-inset-top)}
/* 磨砂颗粒(同仪表盘 / PDF 抽屉) */
#ep-side::before{content:'';position:absolute;inset:0;pointer-events:none;opacity:0.28;mix-blend-mode:overlay;border-radius:inherit;
  background-image:url("data:image/svg+xml;utf8,%3Csvg xmlns='http://www.w3.org/2000/svg' width='180' height='180'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/%3E%3CfeColorMatrix values='0 0 0 0 1  0 0 0 0 1  0 0 0 0 1  0 0 0 0.5 0'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E")}
/* iOS 层叠坑:磨砂 ::before 在内容下层,子元素必须建独立层叠并抬到其上 */
#ep-side>*{position:relative;z-index:1}
#ep-side.open{transform:translateX(0)}
/* 右边缘把手:仿仪表盘磨砂 grip pill;面板展开时移到抽屉左缘(点它关) */
#ep-side-handle{position:fixed;right:0;top:50%;transform:translateY(-50%);z-index:130;writing-mode:vertical-rl;
  background:linear-gradient(135deg,rgba(255,255,255,0.10),rgba(255,255,255,0.04)),rgba(99,102,241,0.18);
  backdrop-filter:blur(12px) saturate(150%) brightness(1.04);-webkit-backdrop-filter:blur(12px) saturate(150%) brightness(1.04);
  border:1px solid rgba(255,255,255,0.22);border-right:none;border-radius:14px 0 0 14px;
  color:rgba(255,255,255,0.85);padding:16px 7px;font-size:11px;letter-spacing:2px;cursor:pointer;
  box-shadow:inset 1px 0 0 rgba(255,255,255,0.16),-2px 0 12px rgba(0,0,0,.35);user-select:none;-webkit-user-select:none;-webkit-tap-highlight-color:transparent;
  transition:right 0.4s cubic-bezier(.4,0,.2,1),background .15s,color .15s}
#ep-side-handle:hover{color:#fff;background:linear-gradient(135deg,rgba(255,255,255,0.15),rgba(255,255,255,0.06)),rgba(99,102,241,0.28)}
body.ep-side-open #ep-side-handle{right:min(38vw,560px)}
/* 抽屉展开时把 EPUB 正文(+顶栏)让出右侧空间(挤压,左侧仍可读,把手始终可见可关) */
/* 照搬 PDF body.grammar-open #main,#header padding-right:开抽屉时把顶栏+正文挤到抽屉左侧,按钮全可点 */
body.ep-side-open #ep-content,body.ep-side-open #ep-top{padding-right:calc(min(38vw,560px) + 12px)}
/* epub.js 正文 #ep-viewer / HTML 正文 #html-content:非悬浮时也挤窄(用 margin-right,配合派发 resize 让 epub.js 重排)*/
body.ep-side-open:not(.ep-side-floating) #ep-viewer,body.ep-side-open:not(.ep-side-floating) #html-content{margin-right:min(38vw,560px);transition:margin-right .4s cubic-bezier(.4,0,.2,1)}
/* 悬浮显示(eph-gp-floating → body.ep-side-floating):抽屉开时**不挤压**,纯磨砂盖在正文/顶栏上 */
/* 照搬 PDF body.grammar-open.grammar-floating #main,#header{padding-right:0}。手搓版正文=#ep-content;epub.js 版=#ep-viewer 本就不被挤(只挤 #ep-top),悬浮再去掉顶栏挤压 */
body.ep-side-open.ep-side-floating #ep-content,body.ep-side-open.ep-side-floating #ep-top{padding-right:0}
@media (max-width:900px){
  #ep-side{width:58vw;max-width:none}
  body.ep-side-open #ep-content,body.ep-side-open #ep-top{padding-right:calc(58vw + 10px)}
  body.ep-side-open.ep-side-floating #ep-content,body.ep-side-open.ep-side-floating #ep-top{padding-right:0}
  body.ep-side-open #ep-side-handle{right:58vw}
}
/* 顶部 tab 栏(照搬 PDF #side-tabs) */
#ep-side-tabs{flex:0 0 auto;display:flex;flex-wrap:wrap;align-items:center;gap:2px;padding:6px 8px;border-bottom:1px solid #2a3550}
#ep-side-tabs .ep-side-tab{background:transparent;border:none;color:#7a8497;font-size:12px;cursor:pointer;padding:5px 8px;border-radius:6px;white-space:nowrap;display:inline-flex;align-items:center;-webkit-tap-highlight-color:transparent}
#ep-side-tabs .ep-side-tab:hover{background:#1a2540;color:#cfe6ff}
#ep-side-tabs .ep-side-tab.active{background:#1a2540;color:#7dd3fc;font-weight:600}
#ep-side-tabs .ep-side-tab-sp{flex:1}
#ep-side-tabs .ep-side-x{background:transparent;border:none;color:#7a8497;font-size:16px;cursor:pointer;padding:3px 8px;line-height:1;-webkit-tap-highlight-color:transparent}
#ep-side-tabs .ep-side-x:hover{color:#fff}
/* Apple/SF 描边图标(照搬 PDF .si) */
#ep-side-tabs .si{width:15px;height:15px;vertical-align:-3px;margin-right:5px;flex:none}
/* 侧栏外观设置弹层(照搬 PDF #side-settings):⚙ 开,悬浮显示 + 背景模糊度 */
#ep-side-settings{position:absolute;top:42px;right:8px;z-index:10;background:#10162a;border:1px solid #2a3550;border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.6);padding:8px;min-width:210px;font-size:12px}
#ep-side-settings .ss-row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:7px 5px;color:#cfe6ff;cursor:pointer;border-radius:6px}
#ep-side-settings .ss-row:hover{background:#162045}
#ep-side-settings .ss-row.ss-col{flex-direction:column;align-items:stretch;gap:7px;cursor:default}
#ep-side-settings .ss-row.ss-col:hover{background:transparent}
#ep-side-settings input[type=range]{width:100%;accent-color:#3b6db5}
/* pane:默认隐藏,active 显示并撑满(照搬 PDF .side-pane) */
.ep-side-pane{display:none;flex:1 1 auto;overflow-y:auto;overflow-x:hidden;min-height:0;overscroll-behavior:contain;touch-action:pan-y;-webkit-overflow-scrolling:touch}
.ep-side-pane.active{display:block}
/* 助手 pane:列布局 → #ep-ai-body 自身 flex:1 滚动,#ep-asst-quick/#ep-ai-input 钉底(照搬 PDF #side-pane-asst.active) */
#ep-side-asst.active{display:flex;flex-direction:column;overflow:hidden;min-height:0}
/* 知识点 / 高亮 / 目录 pane 内边距 */
#ep-side-kg{padding:14px}
#ep-side-hl{padding:12px;display:flex;flex-direction:column;gap:8px}
#ep-side-toc{padding:0}
/* 单词本(照搬 PDF #side-pane-vocab,容器 id 换 ep- 前缀,.vocab-item 类名照搬并 scope 到 #ep-side-vocab) */
#ep-side-vocab{padding:12px}
#ep-vocab-scope-row{display:flex;align-items:center;gap:6px;padding-bottom:10px;border-bottom:1px solid #1c2740;margin-bottom:10px}
#ep-vocab-scope-row button{background:transparent;border:1px solid #2a3550;color:#7a8497;font-size:11px;border-radius:6px;padding:3px 12px;cursor:pointer;-webkit-tap-highlight-color:transparent}
#ep-vocab-scope-row button.active{background:#1a2540;color:#7dd3fc;border-color:#3b6db5;font-weight:600}
#ep-vocab-scope-row .vocab-count{margin-left:auto;font-size:10px;color:#5a6680}
#ep-side-vocab .vocab-item{border:1px solid #1f2740;border-radius:8px;padding:9px 11px;margin-bottom:8px;background:#0d1322}
#ep-side-vocab .vocab-item .vi-head{display:flex;align-items:center;gap:7px}
#ep-side-vocab .vocab-item .vi-word{font-size:14px;font-weight:600;color:#cfe6ff;cursor:pointer}
#ep-side-vocab .vocab-item .vi-word:hover{color:#7dd3fc;text-decoration:underline}
#ep-side-vocab .vocab-item .vi-phon{font-size:11px;color:#7a8497;font-family:monospace}
#ep-side-vocab .vocab-item .vi-audio{background:transparent;border:none;cursor:pointer;font-size:14px;padding:0 2px;line-height:1}
#ep-side-vocab .vocab-item .vi-audio:hover{transform:scale(1.15)}
#ep-side-vocab .vocab-item .vi-mastery-badge{margin-left:auto;font-size:9px;padding:1px 7px;border-radius:8px;white-space:nowrap}
#ep-side-vocab .vocab-item .vi-bar{height:4px;border-radius:2px;background:#1a2540;margin:6px 0;overflow:hidden}
#ep-side-vocab .vocab-item .vi-bar>div{height:100%;border-radius:2px;transition:width .2s}
#ep-side-vocab .vocab-item .vi-zh{font-size:11px;color:#9fb4d4;line-height:1.45;margin:3px 0}
#ep-side-vocab .vocab-item .vi-foot{display:flex;align-items:center;gap:8px;margin-top:5px}
#ep-side-vocab .vocab-item .vi-pages{font-size:10px;color:#7a8497;flex:1;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
#ep-side-vocab .vocab-item .vi-anki{flex:0 0 auto;font-size:10px;background:#1a2540;border:1px solid #2a3550;color:#cfe6ff;border-radius:5px;padding:3px 9px;cursor:pointer}
#ep-side-vocab .vocab-item .vi-anki:hover{background:#2c3e6a}
#ep-side-vocab .vocab-item .vi-anki.done{color:#22c55e;border-color:#22c55e;cursor:default}
/* 知识点节点卡(照搬 PDF .kg-node / rc-knowledge;embedded 模式下 rc-knowledge 不再自注 CSS,卡片样式由本模块负责) */
#ep-kg-nodes .kg-node{background:#0d1322;border:1px solid #1f2740;border-radius:6px;padding:8px 10px;margin-bottom:6px;font-size:12px;display:flex;align-items:flex-start;gap:8px}
#ep-kg-nodes .kg-node .kg-node-main{flex:1;min-width:0;cursor:pointer}
#ep-kg-nodes .kg-node:hover{background:#162045;border-color:#3b6db5}
#ep-kg-nodes .kg-track-btn{flex:none;align-self:center;background:transparent;border:1px solid #3a4456;color:#8a9bb4;border-radius:5px;padding:3px 8px;font-size:11px;cursor:pointer;white-space:nowrap}
#ep-kg-nodes .kg-track-btn:hover{border-color:#34d399;color:#cfe6ff}
#ep-kg-nodes .kg-track-btn.on{background:#13351f;border-color:#34d399;color:#34d399}
#ep-kg-nodes .kg-node .lbl{font-weight:600;color:#cfe6ff}
#ep-kg-nodes .kg-node .sum{color:#8a9bb4;font-size:11px;margin-top:3px;line-height:1.5}
#ep-kg-nodes .kg-node.mastered{border-left:3px solid #34d399}
#ep-kg-nodes .kg-node.unlockable{border-left:3px solid #60a5fa}
#ep-kg-nodes .kg-node.locked{border-left:3px solid #3a4456;opacity:.7}
#ep-kg-nodes .kg-empty{color:#5a6680;font-size:12px}`;
    document.head.appendChild(st);
  }

  // 建抽屉本体(模板没给则兜底创建空壳)+ 把手 + tab 栏(prepend 进抽屉)
  function buildChrome() {
    if (_built) return; _built = true;

    var side = document.getElementById('ep-side');
    if (!side) {                       // 移植兜底:模板未提供 #ep-side → 建空抽屉(无 pane,靠 onTab 自填)
      side = document.createElement('aside'); side.id = 'ep-side';
      document.body.appendChild(side);
    }

    // 把手(竖排 grip pill),点击 toggle
    if (!document.getElementById('ep-side-handle')) {
      var h = document.createElement('div');
      h.id = 'ep-side-handle';
      h.textContent = _opts.handleLabel || '助手 · 知识点';
      h.title = '展开侧栏:助手 / 知识点 / 高亮 / 目录';
      h.addEventListener('click', toggle);
      document.body.appendChild(h);
    }

    // tab 栏:prepend 到抽屉最上(pane 已在模板里)
    if (!document.getElementById('ep-side-tabs')) {
      var bar = document.createElement('div'); bar.id = 'ep-side-tabs';
      var tabs = (_opts.tabs && _opts.tabs.length) ? _opts.tabs : DEFAULT_TABS;
      tabs.forEach(function (t) {
        var b = document.createElement('button');
        b.className = 'ep-side-tab'; b.dataset.pane = t.name;
        b.innerHTML = (t.icon || '') + (t.label || t.name);
        b.addEventListener('click', function () { setTab(t.name); });
        bar.appendChild(b);
      });
      var sp = document.createElement('span'); sp.className = 'ep-side-tab-sp'; bar.appendChild(sp);
      // per-tab 动作按钮(泛化点:PDF「🗑 清空分析」只在 grammar tab 显示;setTab 里按 btn.tabs 切显隐)
      (_opts.tabButtons || []).forEach(function (tb) {
        var b = document.createElement('button'); b.className = 'ep-side-x ep-side-tabact';
        if (tb.id) b.id = tb.id;
        b.innerHTML = tb.icon || tb.label || '·'; b.title = tb.title || '';
        b.style.display = 'none';
        b.addEventListener('click', function (ev) { ev.stopPropagation(); try { tb.onClick && tb.onClick(); } catch (e) {} });
        b.__tabs = tb.tabs || [];
        bar.appendChild(b);
      });
      var setb = document.createElement('button'); setb.className = 'ep-side-x'; setb.id = 'ep-side-set-btn'; setb.title = '侧栏外观设置(悬浮 / 模糊度)';
      setb.innerHTML = '<svg class="si" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" style="margin:0;width:16px;height:16px;vertical-align:-3px"><circle cx="12" cy="12" r="3"/><path d="M12 3v2.5M12 18.5V21M21 12h-2.5M5.5 12H3M18.4 5.6l-1.8 1.8M7.4 16.6l-1.8 1.8M18.4 18.4l-1.8-1.8M7.4 7.4L5.6 5.6"/></svg>';
      setb.addEventListener('click', toggleSideSettings); bar.appendChild(setb);
      var x = document.createElement('button'); x.className = 'ep-side-x'; x.textContent = '✕'; x.title = '关闭侧栏';
      x.addEventListener('click', close); bar.appendChild(x);
      side.insertBefore(bar, side.firstChild);
    }
    if (!document.getElementById('ep-side-settings')) {
      var ss = document.createElement('div'); ss.id = 'ep-side-settings'; ss.style.display = 'none';
      ss.innerHTML = '<label class="ss-row"><span>悬浮显示(盖在正文上)</span><input type="checkbox" id="ep-gp-floating"></label>' +
        '<div class="ss-row ss-col"><span>背景模糊度 <small id="ep-gp-blur-val">20</small> px</span><input type="range" id="ep-gp-blur" min="0" max="40" step="1" value="20"></div>';
      side.appendChild(ss);
      var _f = ss.querySelector('#ep-gp-floating'); if (_f) _f.addEventListener('change', function () { setFloating(this.checked); });
      var _b = ss.querySelector('#ep-gp-blur'); if (_b) _b.addEventListener('input', function () { setBlur(this.value); var v = document.getElementById('ep-gp-blur-val'); if (v) v.textContent = this.value; });
      try { document.addEventListener('pointerdown', function (e) {
        var m = document.getElementById('ep-side-settings');
        if (m && m.style.display === 'block' && !m.contains(e.target) && !(e.target.closest && e.target.closest('#ep-side-set-btn'))) m.style.display = 'none';
      }, true); } catch (e) {}
    }
  }
  function _syncSettingsUI() {
    var f = document.getElementById('ep-gp-floating'); if (f) f.checked = getFloating();
    var b = document.getElementById('ep-gp-blur'); if (b) b.value = getBlur();
    var v = document.getElementById('ep-gp-blur-val'); if (v) v.textContent = getBlur();
  }
  function toggleSideSettings(ev) {
    if (ev) ev.stopPropagation();
    var m = document.getElementById('ep-side-settings'); if (!m) return;
    if (m.style.display === 'block') { m.style.display = 'none'; return; }
    _syncSettingsUI(); m.style.display = 'block';
  }

  // 只切 pane(toggle .active on tab + pane),并触发 onTab 懒填充。不改开关态。
  function setTab(name) {
    if (!name) return;
    // 记忆 tab 在本 reader 无对应 pane(如 EPUB 记了 PDF 专属 hist)→ 回退默认 tab,防空面板
    try {
      if (!document.querySelector('#ep-side .ep-side-pane[data-pane="' + name + '"]')) {
        var _fb = _opts.defaultTab || 'asst';
        if (name !== _fb) name = _fb;
      }
    } catch (e) {}
    _curTab = name;
    try { localStorage.setItem(LS_KEY, name); } catch (e) {}
    document.querySelectorAll('#ep-side-tabs .ep-side-tab').forEach(function (t) {
      t.classList.toggle('active', t.dataset.pane === name);
    });
    document.querySelectorAll('#ep-side .ep-side-pane').forEach(function (p) {
      p.classList.toggle('active', p.dataset.pane === name);
    });
    document.querySelectorAll('#ep-side-tabs .ep-side-tabact').forEach(function (b) {   // per-tab 动作按钮显隐
      b.style.display = (b.__tabs && b.__tabs.indexOf(name) >= 0) ? '' : 'none';
    });
    try { if (typeof _opts.onTab === 'function') _opts.onTab(name); } catch (e) {}
  }

  function _lastTab() {
    var t = '';
    try { t = localStorage.getItem(LS_KEY) || ''; } catch (e) {}
    return t || _opts.defaultTab || 'asst';
  }

  function isOpen() {
    var s = document.getElementById('ep-side');
    return !!(s && s.classList.contains('open'));
  }
  // 开/关抽屉会挤压正文(padding/margin)→ reflow 阅读位置。宿主可传 opts.onLayoutChange(willOpen):
  // 在类名切换**前**调用(此时还能按旧布局取阅读锚点),返回一个 restore 函数则在重排后调它滚回等效位置
  // (下一帧 + 过渡结束各补一次;非函数返回值忽略)。机制(锚点怎么取/怎么滚)在宿主侧,这里只管时序。
  function _layoutKeep(willOpen) {
    var fin = null;
    try { fin = (typeof _opts.onLayoutChange === 'function') ? _opts.onLayoutChange(willOpen) : null; } catch (e) { fin = null; }
    if (typeof fin !== 'function') return;
    requestAnimationFrame(function () { requestAnimationFrame(function () { try { fin(); } catch (e) {} }); });
    setTimeout(function () { try { fin(); } catch (e) {} }, 470);   // 挤压若带 .4s 过渡 → 过渡完再校一次
  }
  function open(tab) {
    var s = document.getElementById('ep-side'); if (!s) return;
    _layoutKeep(true);
    s.style.transform = '';   // 交还给 CSS(从 translateX(102%) → .open translateX(0) 滑入)
    s.classList.add('open');
    document.body.classList.add('ep-side-open');
    _mirror(_opts.mirrorOpenClass, true);
    setTab(tab || _lastTab());
    _reflow();
    // 滑入结束后去掉 transform(none)→ 根治 iOS 上「transform + backdrop-filter」元素的后代命中盒错位
    // (点输入框却触发上方按钮 = 整个抽屉内容命中测试相对合成层偏移;transform 撤掉即对齐,玻璃模糊不受影响)
    clearTimeout(s.__tfT);
    s.__tfT = setTimeout(function () { if (s.classList.contains('open')) s.style.transform = 'none'; }, 460);
  }
  function close() {
    var s = document.getElementById('ep-side');
    if (isOpen()) _layoutKeep(false);
    if (s) {
      clearTimeout(s.__tfT);
      s.style.transform = '';     // 清掉 none → 回到 CSS .open 的 translateX(0)(视觉不变)
      void s.offsetWidth;          // 强制 reflow 让浏览器认 translateX(0) 为滑出起点
      s.classList.remove('open');  // → CSS base translateX(102%),带过渡滑出
    }
    document.body.classList.remove('ep-side-open');
    _mirror(_opts.mirrorOpenClass, false);
    _reflow();
  }
  function toggle() { if (isOpen()) close(); else open(); }
  // 跳转类操作(目录点击 / AI 回复里的章节·页码链接 / 上下文卡)后的抽屉策略:**默认保持打开**——
  // 正文被挤到抽屉左侧仍可见,跳转结果看得到,还能连续点下一个;只有抽屉几乎盖满视口(≥90% 视宽,
  // 如极窄手机竖屏)时才自动收起,不然用户根本看不到跳转结果。各跳转点统一调这一个判定。
  function afterJump() {
    if (!isOpen()) return;
    var s = document.getElementById('ep-side');
    var w = 0;
    try { w = s ? s.getBoundingClientRect().width : 0; } catch (e) {}
    if (w >= (window.innerWidth || 0) * 0.9) close();
  }

  // 照搬 PDF #grammar-panel:抽屉常驻挤压正文,**点正文不关**(只由把手 toggle / 顶部 ✕ 关)。
  // (之前的「点外即关」会在点高亮/选词/翻页时误关,且 PDF 没有这设计 → 移除。)

  function init(opts) {
    _opts = opts || {};
    injectCss();
    buildChrome();
    applyAppearance();   // 载入即应用持久化侧栏外观(悬浮显示 + 模糊度)
    // 同步初始 tab 高亮到模板里默认 .active 的 pane(或上次记忆),但**不**打开抽屉
    var init0 = _lastTab();
    document.querySelectorAll('#ep-side-tabs .ep-side-tab').forEach(function (t) {
      t.classList.toggle('active', t.dataset.pane === init0);
    });
    document.querySelectorAll('#ep-side .ep-side-pane').forEach(function (p) {
      p.classList.toggle('active', p.dataset.pane === init0);
    });
    _curTab = init0;
    return RC.sidedrawer;
  }

  RC.sidedrawer = {
    init: init,
    open: open,
    close: close,
    toggle: toggle,
    setTab: setTab,
    isOpen: isOpen,
    afterJump: afterJump,   // 跳转类操作后调:宽屏保持开,抽屉≥90vw 才收起(判定一处共用)
    // 侧栏外观(供 rc-settings 的「侧边栏」两项直接驱动;setter 写 localStorage + 即时应用)
    setFloating: setFloating,
    getFloating: getFloating,
    setBlur: setBlur,
    getBlur: getBlur,
    applyAppearance: applyAppearance
  };
})();
