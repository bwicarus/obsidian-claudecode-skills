/* rc-settings.js — 统一控制层(window.RC.settings):PDF / EPUB / HTML 阅读器共用的**一份**设置面板。
 * 规格 ground truth = PDF 原生面板(pdf_reader.html #settings-mask + reader.src/21-misc-ai.js):
 * 5 个 tab(AI·翻译 / 阅读 / 语法 / 高亮 / 便签)+ 底部「取消 / 保存」两段式;共有控件的 id / 文案 / 结构 / 内联样式
 * 逐字照搬原生,阅读器特有块经 host / opts 门控显隐(data-sec):
 *   PDF 特有:  页码对齐 / 本书插图描述 / 书籍目录 / 旋转自动排版 / 去边 / 文字层校准
 *              —— HTML 逐字复制原生模板,内联 onclick 直调原生 window.*(applyPageOffset / saveFigToggle /
 *              buildToc / _applyCropSettings / _charboxToggle / _nudgeChars / _reocrPage / …),机制零重写。
 *   EPUB 特有: 字号/主题/行距 / 侧边栏外观 / 插图徽标显隐 / 转 PDF(opts.getReadState / onConvertFull 提供才显示)。
 *
 * host 接线(存量 localStorage / 服务端键各归各,不迁数据):
 *   PDF(pdf-adapter.js openSettings 传 host:'pdf'):mask id 复用原生 'settings-mask',控件 id 全部用原生名
 *     (set-sent-* / set-debug / set-pg-* / set-toc-* / set-crop-* / set-charbox /
 *      charofs-* / reocr-* / lang-checks / set-grammar-view / set-grammar-list / set-hl-colors / set-hl-new)
 *     → 原生读写函数**零改动**直接复用:onFill = 21-misc-ai.js _fillSettings(原 openSettings 函数体),
 *     onSave = 原生 saveSettings(写 pdf-debug / … + POST translate-config + closeSettings),
 *     onCancel = 原生 closeSettings。原生模板 #settings-mask 由 pdf-adapter 在共享模式下移除(防 id 撞车),
 *     ?ui=legacy 不加载本文件,走原生模板面板逃生。
 *   EPUB / HTML(默认 host):mask id 'ep-settings-mask'(历史:退役已删除的 epub2-extra.js 曾靠它注入
 *     epubjs 版插图开关,勿改),内部实现读写 eph-* 键(eph-debug / eph-hl-colors /
 *     eph-vocab-underline / eph-click-translate / eph-grammar-view / eph-set-tab)。
 *     保存语义对齐 PDF 原生:debug / 生词下划线 / 点词翻译 / 句子翻译源 在「保存」时才落盘
 *     (原先改即存 + 关闭即 POST,为两边行为一致改成 PDF 的两段式;「取消」= 丢弃)。
 *     字号/主题/行距/侧栏/语言/高亮色/语法视图/插图徽标 即改即生效(跟 PDF 原生同类控件的即时语义一致)。
 *   AI tab(2026-07 收口):旧 model/effort 逐次覆盖下拉已删(pdf-ai-overrides / eph-ai-model 不再读写,
 *     aiParams() 恒返 {});tab 主体 = **内嵌**的按功能配置表(RC.assistant.renderModelSettings,跟助手 ⚙
 *     浮层同一实现、同组服务端端点),模型选择唯一真源 = 服务端 action 预设。
 *   便签 tab(2026-07-02):共享功能 → PDF/EPUB 两 host 都显示、不 gate。透明度滑块(30–100%)+
 *     「自动对比色」checkbox + 长按进入编辑滑块(200–800ms 步进 50,规格 v3),localStorage 设备级键
 *     rc-note-opacity / rc-note-autocontrast / rc-note-longpress(共享组件自己的键,
 *     不挂 pdf- / eph- 前缀;rc-stickynote 读取给默认 0.72/开/350)。回填在 open() host 无关执行(照
 *     _renderAiInline 先例,PDF 的 onFill=原生 _fillSettings 不认识这些控件);保存挂在「保存」按钮
 *     handler 顶部 host 无关执行(PDF host 走 opts.onSave 不进 saveInternal),落盘后调
 *     RC.stickynote.refreshStyle()(存在才调)即时应用到已挂载便签;「取消」= 丢弃(两段式)。
 *
 * API(对外不变):RC.settings.open(opts) / close() / aiParams() / hlColors() / injectCss()
 * opts(全部可选):
 *   tab                        打开时定位 tab('ai'|'read'|'grammar'|'hl')
 *   host:'pdf'                 PDF host-bind 模式(见上)
 *   ids:{mask,langChecks}      容器 id 覆盖(PDF 传 settings-mask / lang-checks)
 *   keys:{tab}                 tab 记忆键(PDF 传 pdf-set-tab;默认 eph-set-tab)
 *   onFill()                   host 自己回填(PDF=原生 _fillSettings);给了就跳过内部 EPUB 回填
 *   onSave() / onCancel()      host 自己保存/取消(PDF=原生 saveSettings / closeSettings)
 *   openModelSettings()        (已不用:AI tab 改内嵌配置表;opt 保留兼容旧 host 传参,不再消费)
 *   onGrammarView(v)           长句结构显示切换(PDF=原生 window.setGrammarView;EPUB=RC.grammar.setViewMode)
 *   grammarFile                给了 → 语法 pane 渲 KG 启用列表 RC.grammar.renderTrackList('set-grammar-list',{file})
 *   getReadState()->{fs,th,lh} / onFontSize(±10) / onLineHeight(±0.1) / onTheme(th)   EPUB 排版块
 *   onConvertFull(btn)         转 PDF 按钮
 *   getBookLangs() / onSaveLangs()   语言块回填 / 保存(PDF 由 pdf-adapter 传 onSaveLangs=原生 saveLangPicker)
 *   onVocabUnderline(on) / onClickTranslate(on)   「保存」时应用(EPUB;自行持久化 eph-* 键)
 *   onHlColors(colors)         高亮色增删后通知阅读器重渲工具栏色板
 *   onAddHlColor() / onResetHlColors()   PDF host 覆盖(原生 addHlColor / resetHlColors)
 *   onSideFloating(on) / onSideBlur(px)  侧栏外观兜底回调
 *
 * 约定:IIFE + addEventListener(共有控件;IIFE 作用域全局 onclick 找不到函数)。PDF 特有块保留原生内联
 * onclick(全是 window.* 全局名,只在 PDF 页显示)。
 */
(function () {
  if (!window.RC) window.RC = {};
  if (window.RC.settings) return;

  var DEFAULT_HL = ['#fff59d', '#a7f3d0', '#a3d4ff', '#fda4af'];   // 照搬 PDF DEFAULT_HL_COLORS
  // 注:旧 eph-ai-model / eph-ai-effort 键已废弃(2026-07 收口,唯一真源 = 服务端 action 预设),不再读写。
  var LS = { debug: 'eph-debug',
             hl: 'eph-hl-colors', tab: 'eph-set-tab', grammarView: 'eph-grammar-view',
             sideFloat: 'eph-gp-floating', sideBlur: 'eph-gp-blur' };   // 侧栏外观(与 rc-sidedrawer 同键)、grammarView(与 epub2-grammar 同键)

  var _opts = {};       // 当前 open() 传入的回调
  var _built = false;   // DOM 是否已建
  var mask = null, modal = null;
  var _host = '';                                                   // '' = EPUB/HTML(内部实现);'pdf' = host-bind 原生
  var _ids = { mask: 'ep-settings-mask', langChecks: 'eph-lang-checks' };
  var _tabKey = LS.tab;

  function $(id) { return document.getElementById(id); }
  function lsGet(k) { try { return localStorage.getItem(k); } catch (_) { return null; } }
  function lsSet(k, v) { try { localStorage.setItem(k, v); } catch (_) {} }
  function toast(m) { if (window.RC && RC.toast) RC.toast(m); }
  function call(name, arg) { try { if (typeof _opts[name] === 'function') _opts[name](arg); } catch (e) { toast('操作失败：' + (e && e.message)); } }

  // ── 公开纯函数(高亮色;PDF 用自己的 getHlColors,不经这里)──
  // aiParams:旧 per-request model/effort 覆盖已废弃(2026-07 收口)——模型选择唯一真源 = 服务端按功能
  // action 预设(AI tab 内嵌的「AI 模型设置」/ 助手 ⚙,服务端存储)。保留函数签名给存量调用点
  // (epub2-* / html-reader / rc-grammar / rc-result 等),恒返 {}(后端各端点也已不再读这两个参数)。
  function aiParams() { return {}; }
  function hlColors() {
    try { var a = JSON.parse(lsGet(LS.hl) || 'null'); return (Array.isArray(a) && a.length) ? a : DEFAULT_HL.slice(); }
    catch (_) { return DEFAULT_HL.slice(); }
  }
  function saveHlColors(a) { lsSet(LS.hl, JSON.stringify(a)); try { call('onHlColors', a); } catch (_) {} }   // 通知阅读器重渲色板

  // ── AI tab 内嵌「AI 模型设置」配置表(2026-07 收口):跟助手 ⚙ 浮层共用 RC.assistant.renderModelSettings
  //    (同一实现、同组服务端端点 /api/assistant/action-pref[s]);每次打开面板重渲(拉最新预设/型号状态)。──
  function _renderAiInline() {
    var el = $('rcset-ai-inline'); if (!el) return;
    if (window.RC && RC.assistant && RC.assistant.renderModelSettings) { RC.assistant.renderModelSettings(el); return; }
    el.innerHTML = '<div style="font-size:12px;color:#8a9bb4">助手模块(rc-assistant.js)未加载，无法显示模型设置，刷新重试。</div>';
  }

  // ── 便签 tab(共享设备级键;回填在 open() host 无关跑,保存挂「保存」按钮顶部 host 无关跑)──
  var NOTE_OP_KEY = 'rc-note-opacity', NOTE_AC_KEY = 'rc-note-autocontrast';   // 与 rc-stickynote.js 读取端一致
  var NOTE_LP_KEY = 'rc-note-longpress';   // 长按进入编辑时长(毫秒;rc-stickynote.lpMs 读,钳 200–800 缺省 350)
  var NOTE_BLUR_KEY = 'rc-note-blur';      // 磨砂强度(blur px;rc-stickynote.noteBlur 读,钳 0–24 缺省 10)
  function _fillNotePane() {
    var op = $('rcset-note-op'), ov = $('rcset-note-op-val'), ac = $('rcset-note-autoc');
    if (!op) return;
    var v = parseFloat(lsGet(NOTE_OP_KEY));
    if (isNaN(v)) v = 0.72;   // 默认值与 rc-stickynote.noteOpacity 一致
    var pct = Math.round(Math.max(0.3, Math.min(1, v)) * 100);
    op.value = pct;
    if (ov) ov.textContent = pct;
    if (ac) ac.checked = (lsGet(NOTE_AC_KEY) !== '0');   // 默认开
    var lp = $('rcset-note-lp'), lv = $('rcset-note-lp-val');
    if (lp) {
      var ms = parseInt(lsGet(NOTE_LP_KEY), 10);
      if (isNaN(ms)) ms = 350;   // 默认值与 rc-stickynote.lpMs 一致
      ms = Math.max(200, Math.min(800, ms));
      lp.value = ms;
      if (lv) lv.textContent = ms;
    }
    var bl = $('rcset-note-blur'), bv = $('rcset-note-blur-val');
    if (bl) {
      var px = parseInt(lsGet(NOTE_BLUR_KEY), 10);
      if (isNaN(px)) px = 10;   // 默认值与 rc-stickynote.noteBlur 一致
      px = Math.max(0, Math.min(24, px));
      bl.value = px;
      if (bv) bv.textContent = px;
    }
  }
  function _saveNotePane() {
    var op = $('rcset-note-op'), ac = $('rcset-note-autoc');
    if (!op) return;
    var pct = parseInt(op.value, 10);
    if (isNaN(pct)) pct = 72;
    lsSet(NOTE_OP_KEY, String(Math.max(30, Math.min(100, pct)) / 100));
    if (ac) lsSet(NOTE_AC_KEY, ac.checked ? '1' : '0');
    var lp = $('rcset-note-lp');
    if (lp) {
      var ms = parseInt(lp.value, 10);
      if (isNaN(ms)) ms = 350;
      lsSet(NOTE_LP_KEY, String(Math.max(200, Math.min(800, ms))));   // rc-stickynote 每次长按现读 lpMs(),无需 refresh
    }
    var bl = $('rcset-note-blur');
    if (bl) {
      var px = parseInt(bl.value, 10);
      if (isNaN(px)) px = 10;
      lsSet(NOTE_BLUR_KEY, String(Math.max(0, Math.min(24, px))));   // 下面 refreshStyle 里 applyColor 会重设 blur
    }
    // 即时应用到所有已挂载便签(rc-stickynote 未加载的页面(如 HTML 阅读器)只落盘,下次进书生效)
    try { if (window.RC && RC.stickynote && RC.stickynote.refreshStyle) RC.stickynote.refreshStyle(); } catch (_) {}
  }

  // ── 句子翻译源(逐字对照 PDF 21-misc-ai.js:_toggleAiModelRow + open GET / 保存 POST /pdf/api/translate-config)──
  function _toggleSentAiRow() {   // backend=ai 才显示 model/effort 行(等价 PDF _toggleAiModelRow)
    var b = $('set-sent-backend'), row = $('set-sent-ai-row');
    if (b && row) row.style.display = (b.value === 'ai') ? '' : 'none';
  }
  function _loadSentConfig() {   // open() 时拉服务端句子翻译配置回填(等价 PDF openSettings 里的 fetch translate-config)
    fetch('/pdf/api/translate-config').then(function (r) { return r.json(); }).then(function (d) {
      if (!d || !d.ok) return;
      var b = $('set-sent-backend'); if (b) b.value = d.backend || 'auto';
      var m = $('set-sent-model'); if (m) m.value = d.model || 'haiku';
      var e = $('set-sent-effort'); if (e) e.value = d.effort || 'low';
      _toggleSentAiRow();
    }).catch(function () {});
  }
  function _saveSentConfig() {   // 「保存」时把句子翻译配置 POST 回服务端(等价 PDF saveSettings)
    var b = $('set-sent-backend'), m = $('set-sent-model'), e = $('set-sent-effort');
    if (!b || !m || !e) return;
    try {
      fetch('/pdf/api/translate-config', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ backend: b.value, model: m.value, effort: e.value }),
      }).then(function (r) { return r.json(); }).then(function (d) {
        if (d && !d.ok) toast('句子翻译设置保存失败：' + (d.error || '?'));
      }).catch(function (er) { toast('句子翻译设置保存失败：' + (er && er.message)); });
    } catch (_) {}
  }

  // ── 调试日志可见浮窗(逐字对照 PDF 21-misc-ai.js _applyDebugVisibility + pdf_reader.html #debug-log)。
  //    PDF 页有自己的 #debug-log(原生 saveSettings/_applyDebugVisibility 管)→ 这里只在无原生浮窗的页生效。──
  function _applyDebugVisibility() {
    if (document.getElementById('debug-log')) return;   // PDF 页:原生浮窗在管,eph-debug 浮窗不掺和
    var el = $('ep-debug-log');
    if (!el) {
      if (lsGet(LS.debug) !== '1') return;   // 不需显示且没建过 → 不建
      el = document.createElement('div'); el.id = 'ep-debug-log';
      el.style.cssText = 'position:fixed;left:10px;bottom:10px;background:rgba(0,0,0,.85);color:#7be096;font-family:monospace;font-size:11px;padding:8px 12px;border-radius:6px;max-width:600px;max-height:200px;overflow:auto;z-index:9999';
      (document.body || document.documentElement).appendChild(el);
    }
    el.style.display = (lsGet(LS.debug) === '1') ? '' : 'none';
  }
  // 提供 window.dlog(若底座未定义):往可见浮窗追加一行,供 EPUB 各模块复用(对照 PDF window.dlog)
  if (typeof window.dlog !== 'function') {
    window.dlog = function (msg, color) {
      try {
        var el = document.getElementById('ep-debug-log');
        if (!el) { _applyDebugVisibility(); el = document.getElementById('ep-debug-log'); }
        if (!el) return;
        var line = document.createElement('div');
        if (color) line.style.color = color;
        line.textContent = '[' + new Date().toLocaleTimeString() + '] ' + msg;
        el.appendChild(line); el.scrollTop = el.scrollHeight;
      } catch (_) {}
    };
  }

  // ── CSS 注入(scope 到 .rc-set-mask;规则值逐字照搬 pdf_reader.html 的 .set-tabs/.set-tab/.set-hl-row,
  //    EPUB 特有控件样式沿用本文件旧版已验证的 stepper/seg 等)──
  function injectCss() {
    if ($('rc-settings-css')) return;
    var css =
      '.rc-set-mask{position:fixed;inset:0;background:rgba(0,0,0,.55);display:none;align-items:center;justify-content:center;z-index:250}' +
      '.rc-set-mask .ep-set-modal{background:#10162a;border:1px solid #2a3550;border-radius:10px;padding:16px 20px;width:420px;max-width:92vw;max-height:88vh;display:flex;flex-direction:column}' +
      '.rc-set-mask .ep-set-h3{margin:0 0 10px;font-size:15px;color:#cfe6ff}' +
      '.rc-set-mask .set-tabs{display:flex;gap:2px;border-bottom:1px solid #2a3550;flex-wrap:wrap}' +
      '.rc-set-mask .set-tab{background:transparent;border:none;border-bottom:2px solid transparent;color:#8a9bb4;font-size:13px;padding:7px 13px;cursor:pointer;margin-bottom:-1px}' +
      '.rc-set-mask .set-tab.active{color:#cfe6ff;border-bottom-color:#3b6db5;font-weight:600}' +
      '.rc-set-mask .ep-set-body{overflow-y:auto;flex:1;min-height:0;padding:12px 4px 2px}' +
      '.rc-set-mask .set-lbl{display:block;font-size:12px;color:#8a9bb4;margin-bottom:4px}' +
      '.rc-set-mask .ep-set-sel{width:100%;background:#0d1322;border:1px solid #2a3550;color:#e6e6f0;border-radius:6px;padding:7px 10px;font-size:13px;margin-bottom:14px}' +
      '.rc-set-mask .ep-set-row{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:9px 0}' +
      '.rc-set-mask .ep-set-row>label{font-size:13px;color:#cfe6ff}' +
      '.rc-set-mask .stepper{display:flex;align-items:center;gap:6px}' +
      '.rc-set-mask .stepper button{width:30px;height:30px;border-radius:7px;background:#16203a;border:1px solid #2a3a63;color:#cfe0ff;font-size:16px;cursor:pointer}' +
      '.rc-set-mask .stepper .v{font-size:13px;color:#cfe0ff;min-width:46px;text-align:center}' +
      '.rc-set-mask .seg{display:flex;border:1px solid #2a3a63;border-radius:8px;overflow:hidden}' +
      '.rc-set-mask .seg button{background:#0d1426;border:0;color:#9fb0d6;padding:6px 12px;font-size:12px;cursor:pointer}' +
      '.rc-set-mask .seg button.on{background:#2563eb;color:#fff}' +
      '.rc-set-mask .ep-set-chk{display:flex;align-items:center;gap:8px;font-size:13px;color:#cfe6ff;margin-bottom:6px;cursor:pointer}' +
      '.rc-set-mask .ep-set-chk input{width:16px;height:16px}' +
      '.rc-set-mask .ep-set-note{font-size:11px;color:#7a8497;margin-bottom:14px;line-height:1.5}' +
      '.rc-set-mask .ep-set-hr{border:none;border-top:1px solid #2a3550;margin:14px 0}' +
      '.rc-set-mask .ep-full-btn{width:100%;background:#16203a;border:1px solid #2a3a63;color:#cfe0ff;border-radius:8px;padding:9px;font-size:13px;cursor:pointer}' +
      // 颜色管理(照搬 PDF .set-hl-row)
      '.rc-set-mask .set-hl-row{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px}' +
      '.rc-set-mask .set-hl-row .swatch-w{position:relative;display:inline-block}' +
      '.rc-set-mask .set-hl-row .swatch{width:28px;height:28px;border-radius:50%;border:2px solid #2a3550;cursor:default;display:block}' +
      '.rc-set-mask .set-hl-row .del{position:absolute;right:-5px;top:-5px;width:16px;height:16px;border-radius:50%;background:#7a2828;border:1px solid #4a1a1a;color:#ffb4b4;font-size:10px;line-height:13px;cursor:pointer;padding:0;text-align:center}' +
      '.rc-set-mask .set-hl-row .del:hover{background:#9a3232}' +
      // 侧栏外观:模糊度 slider 行(照搬 PDF #side-settings .ss-row.ss-col + input[type=range])
      '.rc-set-mask .ep-set-slrow{display:flex;flex-direction:column;align-items:stretch;gap:7px;margin:9px 0}' +
      '.rc-set-mask .ep-set-slrow>span{font-size:13px;color:#cfe6ff}' +
      '.rc-set-mask .ep-set-slrow small{color:#7a8497}' +
      '.rc-set-mask .ep-set-slrow input[type=range]{width:100%;accent-color:#3b6db5}';
    var st = document.createElement('style');
    st.id = 'rc-settings-css';
    st.textContent = css;
    (document.head || document.documentElement).appendChild(st);
  }

  // ── DOM 建造(一次;共有块 = PDF 原生 HTML 逐字,内联 onclick 改 addEventListener;PDF 特有块 = 原生 HTML
  //    逐字含内联 onclick(window.* 全局,仅 PDF 页显示);EPUB 特有块 = 本文件旧版已验证结构)──
  function ensureDom() {
    if (_built) return;
    injectCss();
    var SEL = 'width:100%;background:#0d1322;border:1px solid #2a3550;color:#e6e6f0;border-radius:6px;padding:7px 10px;font-size:13px;margin-bottom:14px';   // 原生 select 内联样式
    var LBL = 'display:block;font-size:12px;color:#8a9bb4;margin-bottom:4px';   // 原生 label 内联样式
    var HR = '<hr style="border:none;border-top:1px solid #2a3550;margin:14px 0">';   // 原生分隔线

    // ════ pane: AI·翻译(2026-07 收口:旧 model/effort 逐次覆盖下拉已删——唯一真源 = 服务端按功能
    //      action 预设;配置表由 RC.assistant.renderModelSettings **内嵌**渲染,跟助手 ⚙ 浮层同一实现)════
    var paneAi =
      '<div class="set-pane" data-pane="ai">' +
        '<div style="background:#11203a;border:1px solid #2a3550;border-radius:8px;padding:12px;margin-bottom:16px">' +
          '<div style="font-size:13px;color:#cfe0ff;font-weight:600;margin-bottom:4px">🤖 AI 模型（按功能配 后端 / 型号 / 深度）</div>' +
          '<div style="font-size:11px;color:#8a9bb4;line-height:1.6;margin-bottom:10px">解释 / 问 AI / 翻译・例句 / 字典 AI / 语法分析 / 助手 等所有 AI 调用，都走同一套脱壳 Claude + Gemini 双后端（一边失败自动切另一边）。下面按功能分别选：改完即时生效、服务端保存全设备共用；Gemini 免费档优先、过载自动落付费，「💰仅付费」型号（如 3.1-pro）每次调用按量计费。</div>' +
          '<div id="rcset-ai-inline"></div>' +
        '</div>' +
        '<label style="' + LBL + '">🌐 句子翻译源</label>' +
        '<select id="set-sent-backend" style="width:100%;background:#0d1322;border:1px solid #2a3550;color:#e6e6f0;border-radius:6px;padding:7px 10px;font-size:13px;margin-bottom:8px">' +
          '<option value="auto">auto（DeepL 有 key → MyMemory）</option>' +
          '<option value="mymemory">MyMemory（5K/天匿名，50K/天 email 认证）</option>' +
          '<option value="deepl">DeepL（需 dict.deepl_key）</option>' +
          '<option value="ai">AI（用主 AI 后端，按下面 model/effort）</option>' +
        '</select>' +
        '<div id="set-sent-ai-row" style="display:none;margin-bottom:14px">' +
          '<label style="display:block;font-size:12px;color:#8a9bb4;margin:8px 0 4px">AI 模型</label>' +
          '<select id="set-sent-model" style="width:100%;background:#0d1322;border:1px solid #2a3550;color:#e6e6f0;border-radius:6px;padding:7px 10px;font-size:13px;margin-bottom:6px">' +
            '<option value="haiku">haiku（最快最便宜，句子翻译够用）</option>' +
            '<option value="sonnet">sonnet（平衡）</option>' +
            '<option value="opus">opus（最强）</option>' +
            '<option value="gpt-5">gpt-5</option>' +
            '<option value="gpt-5.5">gpt-5.5</option>' +
          '</select>' +
          '<label style="display:block;font-size:12px;color:#8a9bb4;margin:4px 0">思考深度</label>' +
          '<select id="set-sent-effort" style="width:100%;background:#0d1322;border:1px solid #2a3550;color:#e6e6f0;border-radius:6px;padding:7px 10px;font-size:13px">' +
            '<option value="low">low（翻译用 low 足够）</option>' +
            '<option value="medium">medium</option>' +
            '<option value="high">high</option>' +
            '<option value="max">max</option>' +
          '</select>' +
        '</div>' +
        // 143(用户设计):语音工具的**调用前垫话**总策略。单个工具可在「长按工具卡 → 详情窗」里单独覆盖。
        //   实测垫话和 function_call 同属一个 response ⇒ 这是体验旋钮不是省钱旋钮(见 REALTIME_2_1_API_GUIDE)。
        '<div style="background:#11203a;border:1px solid #2a3550;border-radius:8px;padding:12px;margin:16px 0">' +
          '<div style="font-size:13px;color:#cfe0ff;font-weight:600;margin-bottom:4px">🗣 语音·调用前垫话</div>' +
          '<div style="font-size:11px;color:#8a9bb4;line-height:1.6;margin-bottom:10px">AI 调工具前要不要先说一句「我去查一下」。<b>自动</b> = 按这个工具在账本里的<b>真实中位耗时</b>判：慢过阈值才垫话，秒回的静默直接调（免得啰嗦）。单个工具想固定，长按它的工具卡 → 详情窗里单独设。</div>' +
          '<select id="set-filler-mode" style="width:100%;background:#0d1322;border:1px solid #2a3550;color:#e6e6f0;border-radius:6px;padding:7px 10px;font-size:13px;margin-bottom:8px">' +
            '<option value="auto">自动（按实测耗时判，推荐）</option>' +
            '<option value="always">总是说（每个工具都先垫一句）</option>' +
            '<option value="never">全部静默（工具调用不吭声）</option>' +
          '</select>' +
          '<div id="set-filler-th-row">' +
            '<label style="display:block;font-size:12px;color:#8a9bb4;margin:4px 0">自动的耗时阈值（秒）：慢于它才垫话</label>' +
            '<input id="set-filler-th" type="number" step="0.5" min="0.5" max="30" style="width:100%;background:#0d1322;border:1px solid #2a3550;color:#e6e6f0;border-radius:6px;padding:7px 10px;font-size:13px">' +
          '</div>' +
        '</div>' +
        '<label style="display:flex;align-items:center;gap:8px;font-size:13px;color:#cfe6ff;margin-bottom:6px;cursor:pointer">' +
          '<input type="checkbox" id="set-debug" style="width:16px;height:16px"> 显示调试日志（左下角浮窗）' +
        '</label>' +
      '</div>';

    // ════ pane: 阅读(共有块逐字照搬 PDF;PDF/EPUB 特有块 data-sec 门控)════
    var paneRead =
      '<div class="set-pane" data-pane="read" style="display:none">' +
        // [EPUB] 排版
        '<div data-sec="epub-typo">' +
          '<div class="ep-set-row"><label>字号</label><div class="stepper"><button id="eph2-fs-dn">A−</button><span class="v" id="eph2-fs-v">100%</span><button id="eph2-fs-up">A+</button></div></div>' +
          '<div class="ep-set-row"><label>主题</label><div class="seg" id="eph2-theme"><button type="button" data-th="paper" class="on">纸</button><button type="button" data-th="sepia">褐</button><button type="button" data-th="night">夜</button></div></div>' +
          '<div class="ep-set-row"><label>行距</label><div class="stepper"><button id="eph2-lh-dn">−</button><span class="v" id="eph2-lh-v">1.7</span><button id="eph2-lh-up">+</button></div></div>' +
          '<hr class="ep-set-hr">' +
        '</div>' +
        // [EPUB] 侧边栏外观(直接驱动 RC.sidedrawer)
        '<div data-sec="epub-side">' +
          '<label class="set-lbl">🪟 侧边栏</label>' +
          '<label class="ep-set-chk"><input type="checkbox" id="eph2-side-floating"> 悬浮显示（盖在正文上，不挤压正文）</label>' +
          '<div class="ep-set-slrow"><span>背景模糊度 <small id="eph2-side-blur-val">20</small> px</span><input type="range" id="eph2-side-blur" min="0" max="40" step="1" value="20"></div>' +
          '<div class="ep-set-note" style="margin:-2px 0 8px">关=抽屉挤压正文（左侧仍可读）；开=磨砂抽屉悬浮盖在正文上。模糊度调抽屉背后的虚化强度。</div>' +
          '<hr class="ep-set-hr">' +
        '</div>' +
        // [PDF] 页码对齐(逐字照搬,内联 onclick=原生 window.applyPageOffset)
        '<div data-sec="pdf-pageoffset">' +
          '<div style="font-size:13px;color:#cfe6ff;margin-bottom:4px">📖 页码对齐</div>' +
          '<div style="font-size:11px;color:#7a8497;margin-bottom:8px;line-height:1.5">让阅读器显示的页码＝书上印的页码（PDF 前几页常是封面/目录）。翻到当前这页，把它在书上印的页码填进去点「对齐」即可。<b>每本书独立、自动跨设备同步</b>。</div>' +
          '<div style="display:flex;align-items:center;gap:6px;font-size:13px;color:#cfe6ff;margin-bottom:6px;flex-wrap:wrap">' +
            '当前 PDF 第 <b id="set-pg-pdf">–</b> 页 ＝ 书上第' +
            '<input type="number" id="set-pg-printed" style="width:64px;background:#0d1322;border:1px solid #2a3550;color:#e6e6f0;border-radius:6px;padding:5px 8px;font-size:13px"> 页' +
            '<button type="button" onclick="applyPageOffset()" style="background:#1f6feb;border:none;color:#fff;border-radius:6px;padding:6px 12px;font-size:13px;cursor:pointer">对齐</button>' +
          '</div>' +
          '<div style="font-size:11px;color:#7a8497;margin-bottom:14px"><span id="set-pg-cur-off">当前偏移：0</span> · <a href="javascript:void 0" onclick="applyPageOffset(0)" style="color:#7dd3fc">重置</a></div>' +
          HR +
        '</div>' +
        // [PDF] 本书插图描述(逐字照搬,onchange=原生 saveFigToggle)
        '<div data-sec="pdf-figures">' +
          '<label style="display:flex;align-items:center;gap:8px;font-size:13px;color:#cfe6ff;margin-bottom:6px;cursor:pointer">' +
            '<input type="checkbox" id="set-figures" onchange="saveFigToggle(this.checked)" style="width:16px;height:16px"> 📷 本书插图描述（图区放徽标，点开看 AI 说明）' +
          '</label>' +
          '<div style="font-size:11px;color:#7a8497;margin-bottom:14px;line-height:1.5">默认<b>关闭</b>。开启后翻到的页会<b>逐页让 AI 识别插图并描述</b>（首次每页几秒、消耗 AI 配额），描述结果存服务器跨端共用。不需要插图说明的书保持关闭即可。<b>每本书独立</b>。</div>' +
          // [PDF] 概念网按书开火(用户定:读哪本书时决定哪本;默认隐藏,PDF 的 _fillSettings 揭示+回填)
          '<div data-sec="pdf-conceptnet" style="display:none">' +
            '<label style="display:flex;align-items:center;gap:8px;font-size:13px;color:#cfe6ff;margin-bottom:6px;cursor:pointer">' +
              '<input type="checkbox" id="set-conceptnet" onchange="window.saveConceptNetToggle&&saveConceptNetToggle(this.checked)" style="width:16px;height:16px"> 🌱 本书自动生长概念笔记（概念网）' +
            '</label>' +
            '<div style="font-size:11px;color:#7a8497;margin-bottom:14px;line-height:1.5">默认<b>关闭</b>。开启后夜间流水线对你在本书反复关注的<b>学科概念</b>自动生成概念笔记（引原文定义+自动连边,单词仍归词汇本）。<b>每本书独立</b>,即改即存。</div>' +
          '</div>' +
          HR +
        '</div>' +
        // [PDF] 书籍目录(逐字照搬,onclick=原生 buildToc/showTocBuild;状态区由原生 loadTocStatus 填)
        '<div data-sec="pdf-toc">' +
          '<label style="display:block;font-size:13px;color:#cfe6ff;margin-bottom:6px">📖 书籍目录（章节 provenance，供图描述/助手定位「书的哪一章节」）</label>' +
          '<div id="set-toc-status" style="font-size:11px;color:#7a8497;margin-bottom:8px;line-height:1.5">检查中…</div>' +
          '<div id="set-toc-build" style="display:none">' +
            '<div style="font-size:11px;color:#8a9bb4;margin-bottom:6px;line-height:1.5">填目录所在的 PDF 页范围（不是书上印的页码，是阅读器顶部显示的 PDF 第几页），AI 整页识图抽出目录（覆盖原生目录）。</div>' +
            '<div style="display:flex;gap:8px;margin-bottom:8px;align-items:center">' +
              '<label style="flex:1;font-size:11px;color:#8a9bb4">起始 PDF 页<input type="number" id="set-toc-start" min="1" step="1" style="width:100%;background:#0d1322;border:1px solid #2a3550;color:#e6e6f0;border-radius:6px;padding:6px 8px;font-size:13px;margin-top:2px"></label>' +
              '<label style="flex:1;font-size:11px;color:#8a9bb4">结束 PDF 页<input type="number" id="set-toc-end" min="1" step="1" style="width:100%;background:#0d1322;border:1px solid #2a3550;color:#e6e6f0;border-radius:6px;padding:6px 8px;font-size:13px;margin-top:2px"></label>' +
            '</div>' +
            '<button id="set-toc-btn" onclick="buildToc()" style="width:100%;background:#1a2540;border:1px solid #3b6db5;color:#9fcbff;border-radius:6px;padding:8px;font-size:13px;cursor:pointer">建立目录</button>' +
          '</div>' +
          HR +
        '</div>' +
        // [共有] 生词下划线 / 点词翻译(逐字照搬 PDF;保存时落盘)
        '<label style="display:flex;align-items:center;gap:8px;font-size:13px;color:#cfe6ff;margin-bottom:14px;cursor:pointer">' +
          '<input type="checkbox" id="set-vocab-underline" style="width:16px;height:16px"> 生词下划线（按掌握度着色：橙=新 / 黄=见过 / 淡绿=熟）' +
        '</label>' +
        '<label style="display:flex;align-items:center;gap:8px;font-size:13px;color:#cfe6ff;margin-bottom:14px;cursor:pointer">' +
          '<input type="checkbox" id="set-click-translate" style="width:16px;height:16px"> 点击未掌握单词直接显示翻译（不弹工具栏）' +
        '</label>' +
        // [EPUB] 插图徽标显隐(纯 UI,即改即生效,window.toggleFigBadge)
        '<div data-sec="epub-figbadge">' +
          '<label class="ep-set-chk" style="margin-bottom:14px"><input type="checkbox" id="eph2-fig-badge"> 📷 显示插图说明徽标（关闭只隐藏徽标 UI，不影响 AI 描述功能本身）</label>' +
        '</div>' +
        // [PDF] 旋转屏幕自动切换排版(逐字照搬;保存时由原生 saveSettings 读)
        '<div data-sec="pdf-orient">' +
          '<label style="display:flex;align-items:center;gap:8px;font-size:13px;color:#cfe6ff;margin-bottom:6px;cursor:pointer">' +
            '<input type="checkbox" id="set-auto-orient" style="width:16px;height:16px"> 旋转屏幕自动切换排版（每本书记住横/竖屏各自的「排版+去边」）' +
          '</label>' +
          '<div style="font-size:11px;color:#7a8497;margin-bottom:14px;line-height:1.5">开启后：在某方向改了排版(连续/双页)或去边开关，会记进该方向；旋转回来自动套用那个方向上次的设置。</div>' +
        '</div>' +
        // [PDF] 去边(逐字照搬,onclick=原生 _applyCropSettings)
        '<div data-sec="pdf-crop">' +
          HR +
          '<label style="display:block;font-size:12px;color:#8a9bb4;margin-bottom:6px">✂️ 去边阅读（本书每页隐藏的边距 %，工具栏「去边」开关切换）</label>' +
          '<div style="display:flex;gap:8px;margin-bottom:8px">' +
            '<label style="flex:1;font-size:11px;color:#8a9bb4">左<input type="number" id="set-crop-l" min="0" max="45" step="1" style="width:100%;background:#0d1322;border:1px solid #2a3550;color:#e6e6f0;border-radius:6px;padding:6px 8px;font-size:13px;margin-top:2px"></label>' +
            '<label style="flex:1;font-size:11px;color:#8a9bb4">右<input type="number" id="set-crop-r" min="0" max="45" step="1" style="width:100%;background:#0d1322;border:1px solid #2a3550;color:#e6e6f0;border-radius:6px;padding:6px 8px;font-size:13px;margin-top:2px"></label>' +
            '<label style="flex:1;font-size:11px;color:#8a9bb4">上<input type="number" id="set-crop-t" min="0" max="45" step="1" style="width:100%;background:#0d1322;border:1px solid #2a3550;color:#e6e6f0;border-radius:6px;padding:6px 8px;font-size:13px;margin-top:2px"></label>' +
            '<label style="flex:1;font-size:11px;color:#8a9bb4">下<input type="number" id="set-crop-b" min="0" max="45" step="1" style="width:100%;background:#0d1322;border:1px solid #2a3550;color:#e6e6f0;border-radius:6px;padding:6px 8px;font-size:13px;margin-top:2px"></label>' +
          '</div>' +
          '<button onclick="_applyCropSettings()" style="width:100%;background:#1a2540;border:1px solid #3b6db5;color:#9fcbff;border-radius:6px;padding:8px;font-size:13px;cursor:pointer;margin-bottom:14px">应用去边并开启</button>' +
        '</div>' +
        // [PDF] 文字层校准(逐字照搬,onclick/onchange=原生 _charboxToggle/_nudgeChars/_resetCharOffset/_reocrPage/_clearReocr)
        '<div data-sec="pdf-charofs">' +
          HR +
          '<label style="display:block;font-size:12px;color:#8a9bb4;margin-bottom:6px">🔧 文字层校准（扫描/OCR 书的文字层跟画面没对齐时用；<b>每页独立</b>）</label>' +
          '<label style="display:flex;align-items:center;gap:8px;font-size:13px;color:#cfe6ff;cursor:pointer;margin-bottom:8px">' +
            '<input type="checkbox" id="set-charbox" onchange="_charboxToggle(this)" style="width:16px;height:16px"> 可视化文字框（红框叠在页面上，直观看哪里偏）' +
          '</label>' +
          '<div style="display:flex;align-items:center;gap:6px;margin-bottom:6px;flex-wrap:wrap">' +
            '<span style="font-size:12px;color:#8a9bb4">微调当前页：</span>' +
            '<button onclick="_nudgeChars(-1,0)" title="文字层左移" style="background:#1a2540;border:1px solid #2a3550;color:#cfe6ff;border-radius:6px;width:34px;padding:6px 0;font-size:14px;cursor:pointer">←</button>' +
            '<button onclick="_nudgeChars(0,-1)" title="文字层上移" style="background:#1a2540;border:1px solid #2a3550;color:#cfe6ff;border-radius:6px;width:34px;padding:6px 0;font-size:14px;cursor:pointer">↑</button>' +
            '<button onclick="_nudgeChars(0,1)" title="文字层下移" style="background:#1a2540;border:1px solid #2a3550;color:#cfe6ff;border-radius:6px;width:34px;padding:6px 0;font-size:14px;cursor:pointer">↓</button>' +
            '<button onclick="_nudgeChars(1,0)" title="文字层右移" style="background:#1a2540;border:1px solid #2a3550;color:#cfe6ff;border-radius:6px;width:34px;padding:6px 0;font-size:14px;cursor:pointer">→</button>' +
            '<label style="font-size:11px;color:#8a9bb4">步长<input type="number" id="charofs-step" value="2" min="0.5" step="0.5" style="width:50px;background:#0d1322;border:1px solid #2a3550;color:#e6e6f0;border-radius:6px;padding:5px 6px;font-size:13px;margin-left:3px">pt</label>' +
            '<button onclick="_resetCharOffset()" style="background:#2a1a1a;border:1px solid #5a3030;color:#e6b0b0;border-radius:6px;padding:6px 10px;font-size:12px;cursor:pointer">重置本页</button>' +
          '</div>' +
          '<div id="charofs-cur" style="font-size:11px;color:#7a8497;margin-bottom:8px">第 — 页　dx 0.0 · dy 0.0</div>' +
          '<div style="display:flex;gap:6px;margin-bottom:6px;flex-wrap:wrap;align-items:center">' +
            '<button onclick="_reocrPage()" id="reocr-btn" style="background:#1a2540;border:1px solid #3b6db5;color:#9fcbff;border-radius:6px;padding:6px 10px;font-size:12px;cursor:pointer">🔁 单页重扫(Google Vision)</button>' +
            '<button onclick="_clearReocr()" style="background:#2a1a1a;border:1px solid #5a3030;color:#e6b0b0;border-radius:6px;padding:6px 10px;font-size:12px;cursor:pointer">撤销重扫</button>' +
            '<span id="reocr-status" style="font-size:11px;color:#7a8497"></span>' +
          '</div>' +
          '<div style="font-size:11px;color:#7a8497;margin-bottom:14px;line-height:1.5">没对齐：先「可视化文字框」看差多少 → 方向键微调推齐。识别错/漏/整页歪：「单页重扫」用 Google Vision 对当前页重新 OCR（~几秒，重扫后文字层即时更新）。</div>' +
        '</div>' +
        // [共有] 需要翻译的语言(文案逐字照搬 PDF;容器 id 按 host:PDF=lang-checks(原生 saveLangPicker 读),EPUB=eph-lang-checks)
        '<div data-sec="langs">' +
          HR +
          '<label style="display:block;font-size:12px;color:#8a9bb4;margin-bottom:6px">🌐 需要翻译的语言（勾选你想被<b>翻译/查词辅助</b>的语言：日语→中日词典+振假名，英语→英汉词典+语法）<br><b>没勾的语言（如中文母语）视为你已掌握 → 免于翻译查词</b>。每本书独立。</label>' +
          '<div id="' + _ids.langChecks + '" style="display:flex;gap:16px;font-size:13px;color:#cfe6ff;margin-bottom:8px">' +
            '<label style="cursor:pointer"><input type="checkbox" value="en" style="width:15px;height:15px;vertical-align:middle;margin-right:4px">英语</label>' +
            '<label style="cursor:pointer"><input type="checkbox" value="ja" style="width:15px;height:15px;vertical-align:middle;margin-right:4px">日语</label>' +
          '</div>' +
          '<button type="button" id="rcset-lang-save" style="width:100%;background:#1a2540;border:1px solid #3b6db5;color:#9fcbff;border-radius:6px;padding:8px;font-size:13px;cursor:pointer">保存</button>' +
        '</div>' +
        // [EPUB] 转 PDF
        '<div data-sec="epub-convert">' +
          '<hr class="ep-set-hr">' +
          '<button id="eph2-full-btn" class="ep-full-btn">📄 完整功能版（转 PDF）</button>' +
          '<div class="ep-set-note" style="margin-top:6px">需要 OCR / 手写 等 PDF 专属功能时，转成 PDF 用完整阅读器打开（后台转换，可关页面）。</div>' +
        '</div>' +
      '</div>';

    // ════ pane: 语法(逐字照搬 PDF:显示模式下拉 + KG 启用列表;列表 PDF 由原生 renderGrammarTrackList →
    //      RC.grammar.renderTrackList 填,EPUB 由 open() 按 opts.grammarFile 填同一容器)════
    var paneGrammar =
      '<div class="set-pane" data-pane="grammar" style="display:none">' +
        '<label style="' + LBL + '">📐 长句结构显示</label>' +
        '<select id="set-grammar-view" style="' + SEL + '">' +
          '<option value="tree">成分树（成分名+颜色+层次缩进+折叠，融合）</option>' +
          '<option value="components">成分分块（主谓宾定状从句彩色块）</option>' +
          '<option value="skeleton">主干+修饰折叠（骨架先行，修饰点开）</option>' +
          '<option value="deps">依存关系图（displaCy 弧线 + 从句切段）</option>' +
        '</select>' +
        HR +
        '<label style="' + LBL + '">📊 启用语法分析（per-书）</label>' +
        '<div style="font-size:10px;color:#7a8497;margin-bottom:6px;line-height:1.5">' +
          '勾选本书用哪些语法 KG。具体跟踪哪些语法点请去' +
          '<a href="/skilltree/grammar-demo/" target="_blank" style="color:#60a5fa">技能树页面</a>' +
          '点节点详情里的「👁 跟踪」按钮设置。' +
        '</div>' +
        '<div id="set-grammar-list" style="max-height:240px;overflow-y:auto;background:#0d1322;border:1px solid #2a3550;border-radius:6px;padding:8px;font-size:12px">' +
          '<div style="color:#7a8497">加载中…</div>' +
        '</div>' +
      '</div>';

    // ════ pane: 高亮(逐字照搬 PDF)════
    var paneHl =
      '<div class="set-pane" data-pane="hl" style="display:none">' +
        '<label style="display:block;font-size:12px;color:#8a9bb4;margin-bottom:6px">🖌 高亮颜色（点击 ✕ 删除）</label>' +
        '<div id="set-hl-colors" class="set-hl-row"></div>' +
        '<div style="display:flex;gap:6px;margin-bottom:14px;align-items:center">' +
          '<input type="color" id="set-hl-new" value="#ffd166" style="width:42px;height:30px;border:none;background:transparent;cursor:pointer;padding:0">' +
          '<button id="rcset-hl-add" style="background:#1a2540;border:1px solid #2a3550;color:#cfe6ff;border-radius:4px;padding:5px 12px;cursor:pointer;font-size:12px">＋ 添加</button>' +
          '<button id="rcset-hl-reset" style="background:transparent;border:1px solid #2a3550;color:#7a8497;border-radius:4px;padding:5px 12px;cursor:pointer;font-size:12px">恢复默认</button>' +
        '</div>' +
      '</div>';

    // ════ pane: 便签(共享功能 rc-stickynote:PDF/EPUB 都显示,不 gate;设备级 localStorage rc-note-* 键,
    //      「保存」时落盘 + RC.stickynote.refreshStyle() 即时应用,「取消」丢弃)════
    var paneNote =
      '<div class="set-pane" data-pane="note" style="display:none">' +
        '<label style="' + LBL + '">🗒 便签外观（本设备）</label>' +
        '<div class="ep-set-slrow"><span>底色不透明度 <small id="rcset-note-op-val">72</small>%</span><input type="range" id="rcset-note-op" min="30" max="100" step="1" value="72"></div>' +
        '<div class="ep-set-note" style="margin:-2px 0 12px">便签底是<b>半透明磨砂玻璃</b>，能隐约看到下方正文；越低越透。上方操作条保持基本不透明。保存后立即应用到页面上已有的便签。</div>' +
        '<div class="ep-set-slrow"><span>磨砂强度 <small id="rcset-note-blur-val">10</small> px</span><input type="range" id="rcset-note-blur" min="0" max="24" step="2" value="10"></div>' +
        '<div class="ep-set-note" style="margin:-2px 0 12px">透过便签看到的下方正文的模糊程度：0 = 不模糊（纯半透明），越大越朦胧。</div>' +
        '<label class="ep-set-chk"><input type="checkbox" id="rcset-note-autoc"> 文字 / 手写笔自动对比色</label>' +
        '<div class="ep-set-note">开：按便签底色深浅自动选前景色——浅色便签配深字深笔，深色便签（石墨/墨绿）配浅字浅笔；<b>已画的笔迹不改色</b>，只影响文字显示和新笔画。关：固定深色文字＋红笔。</div>' +
        '<hr class="ep-set-hr">' +
        '<div class="ep-set-slrow"><span>长按进入编辑 <small id="rcset-note-lp-val">350</small> ms</span><input type="range" id="rcset-note-lp" min="200" max="800" step="50" value="350"></div>' +
        '<div class="ep-set-note" style="margin:-2px 0 0">按住便签（任意部分）多久进入编辑模式（移动 / 缩放 / 换色 / 删除）。越短越灵敏，太短容易误触。保存后下次长按生效。</div>' +
      '</div>';

    mask = document.createElement('div');
    mask.id = _ids.mask;
    mask.className = 'rc-set-mask';
    mask.setAttribute('data-rc', '1');   // pdf-adapter 据此区分 rc 面板 vs 原生模板面板(移除后者)
    mask.innerHTML =
      '<div class="ep-set-modal">' +
        '<h3 class="ep-set-h3">⚙️ 设置</h3>' +
        '<div class="set-tabs">' +
          '<button type="button" class="set-tab active" data-pane="ai">AI·翻译</button>' +
          '<button type="button" class="set-tab" data-pane="read">阅读</button>' +
          '<button type="button" class="set-tab" data-pane="grammar">语法</button>' +
          '<button type="button" class="set-tab" data-pane="hl">高亮</button>' +
          '<button type="button" class="set-tab" data-pane="note">便签</button>' +
        '</div>' +
        '<div class="ep-set-body">' + paneAi + paneRead + paneGrammar + paneHl + paneNote + '</div>' +
        '<div style="display:flex;gap:8px;justify-content:flex-end;padding-top:12px;border-top:1px solid #2a3550;margin-top:2px">' +
          '<button id="rcset-cancel" style="background:#1a2540;border:1px solid #2a3550;color:#cfe6ff;border-radius:6px;padding:7px 16px;cursor:pointer;font-size:13px">取消</button>' +
          '<button id="rcset-save" style="background:#244470;border:1px solid #3b6db5;color:#fff;border-radius:6px;padding:7px 16px;cursor:pointer;font-size:13px">保存</button>' +
        '</div>' +
      '</div>';
    document.body.appendChild(mask);
    modal = mask.querySelector('.ep-set-modal');

    // 点遮罩空白处 = 取消(对齐 PDF 原生 mask onclick closeSettings)
    mask.addEventListener('click', function (e) { if (e.target === mask) cancel(); });
    $('rcset-cancel').addEventListener('click', cancel);
    $('rcset-save').addEventListener('click', function () {
      _saveNotePane();   // 便签设置:共享设备级 rc-note-* 键,两 host 都在「保存」时落盘(PDF 的 onSave 不认识这些控件)
      if (typeof _opts.onSave === 'function') { try { _opts.onSave(); } catch (e) { toast('保存失败：' + (e && e.message)); } hide(); return; }   // PDF:原生 saveSettings(自带 closeSettings)
      saveInternal();
    });

    // tab 切换
    modal.querySelectorAll('.set-tab').forEach(function (t) {
      t.addEventListener('click', function () { setTab(t.dataset.pane); });
    });

    // 句子翻译源:backend 改 → 切 model/effort 行可见(等价 PDF _toggleAiModelRow;POST 在「保存」时统一发)
    $('set-sent-backend').addEventListener('change', _toggleSentAiRow);

    // 语法:长句结构显示切换即时生效(等价 PDF 原生 onchange="setGrammarView(this.value)")
    $('set-grammar-view').addEventListener('change', function () {
      var v = this.value;
      if (typeof _opts.onGrammarView === 'function') { call('onGrammarView', v); return; }
      lsSet(LS.grammarView, v);
      try { if (window.setGrammarView) window.setGrammarView(v); } catch (e) {}
    });

    // [EPUB] 阅读:控件渲染在这,apply 逻辑回调给驱动,回调后重读 getReadState 刷新显示
    $('eph2-fs-up').addEventListener('click', function () { call('onFontSize', 10); refreshRead(); });
    $('eph2-fs-dn').addEventListener('click', function () { call('onFontSize', -10); refreshRead(); });
    $('eph2-lh-up').addEventListener('click', function () { call('onLineHeight', 0.1); refreshRead(); });
    $('eph2-lh-dn').addEventListener('click', function () { call('onLineHeight', -0.1); refreshRead(); });
    modal.querySelectorAll('#eph2-theme button').forEach(function (b) {
      b.addEventListener('click', function () { call('onTheme', b.dataset.th); refreshRead(); });
    });
    // [EPUB] 插图徽标显隐:纯客户端 UI(localStorage + body class),直调全局 window.toggleFigBadge(epub-html.js 提供)
    var _fb = $('eph2-fig-badge'); if (_fb) _fb.addEventListener('change', function () { try { window.toggleFigBadge && window.toggleFigBadge(this.checked); } catch (_) {} });
    $('eph2-full-btn').addEventListener('click', function () { call('onConvertFull', $('eph2-full-btn')); });

    // [EPUB] 侧边栏外观:直接驱动 RC.sidedrawer(setter 写 localStorage + 即时应用);另兜底触发可选回调
    var _sFloat = $('eph2-side-floating');
    if (_sFloat) _sFloat.addEventListener('change', function () {
      var on = this.checked;
      if (window.RC && RC.sidedrawer && RC.sidedrawer.setFloating) RC.sidedrawer.setFloating(on);
      else lsSet(LS.sideFloat, on ? '1' : '0');   // 抽屉模块未就绪也先存,init 时 applyAppearance 会读
      call('onSideFloating', on);
    });
    var _sBlur = $('eph2-side-blur');
    if (_sBlur) _sBlur.addEventListener('input', function () {
      var v = parseInt(this.value, 10); if (isNaN(v)) v = 20;
      var bv = $('eph2-side-blur-val'); if (bv) bv.textContent = v;
      if (window.RC && RC.sidedrawer && RC.sidedrawer.setBlur) RC.sidedrawer.setBlur(v);
      else { lsSet(LS.sideBlur, String(v)); document.documentElement.style.setProperty('--gp-blur', v + 'px'); }
      call('onSideBlur', v);
    });

    // [便签] 透明度/长按时长滑块:拖动只更新旁边的数值显示(面板内暂存,「保存」才落盘+应用,两段式)
    var _nop = $('rcset-note-op');
    if (_nop) _nop.addEventListener('input', function () {
      var v = $('rcset-note-op-val'); if (v) v.textContent = this.value;
    });
    var _nlp = $('rcset-note-lp');
    if (_nlp) _nlp.addEventListener('input', function () {
      var v = $('rcset-note-lp-val'); if (v) v.textContent = this.value;
    });
    var _nbl = $('rcset-note-blur');
    if (_nbl) _nbl.addEventListener('input', function () {
      var v = $('rcset-note-blur-val'); if (v) v.textContent = this.value;
    });

    // 语言「保存」:PDF=原生 saveLangPicker(读 #lang-checks);EPUB=onSaveLangs(epub 版 saveLangPicker 读 #eph-lang-checks)
    $('rcset-lang-save').addEventListener('click', function () {
      if (_host === 'pdf') { try { window.saveLangPicker && window.saveLangPicker(); } catch (e) {} return; }
      call('onSaveLangs');
    });

    // 高亮:颜色管理(PDF=原生 addHlColor/resetHlColors 经 opts,存 pdf 键 + 重渲原生色板;EPUB=内部 eph-hl-colors)
    $('rcset-hl-add').addEventListener('click', function () {
      if (typeof _opts.onAddHlColor === 'function') { call('onAddHlColor'); return; }
      var v = ($('set-hl-new').value || '').trim();
      if (!/^#[0-9a-fA-F]{3,8}$/.test(v)) { toast('颜色格式应为 #rrggbb'); return; }
      var arr = hlColors();
      if (arr.indexOf(v) >= 0) return;
      arr.push(v); saveHlColors(arr); renderHl();
    });
    $('rcset-hl-reset').addEventListener('click', function () {
      if (typeof _opts.onResetHlColors === 'function') { call('onResetHlColors'); return; }
      saveHlColors(DEFAULT_HL.slice()); renderHl();
    });

    _built = true;
  }

  // ── 区块门控(阅读器特有项显隐;共有块永远显示)──
  function _show(sec, on) {
    if (!modal) return;
    modal.querySelectorAll('[data-sec="' + sec + '"]').forEach(function (el) { el.style.display = on ? '' : 'none'; });
  }
  function gateSections() {
    var pdf = (_host === 'pdf');
    _show('epub-typo', !pdf && typeof _opts.getReadState === 'function');
    _show('epub-side', !pdf);
    _show('epub-figbadge', !pdf && typeof window.toggleFigBadge === 'function');
    _show('epub-convert', !pdf && typeof _opts.onConvertFull === 'function');
    ['pdf-pageoffset', 'pdf-figures', 'pdf-toc', 'pdf-orient', 'pdf-crop', 'pdf-charofs'].forEach(function (s) { _show(s, pdf); });
    _show('langs', pdf || typeof _opts.getBookLangs === 'function');
  }

  // ── tab 切换 + 记忆 ──
  function setTab(name) {
    if (!modal) return;
    modal.querySelectorAll('.set-tab').forEach(function (t) { t.classList.toggle('active', t.dataset.pane === name); });
    modal.querySelectorAll('.set-pane').forEach(function (p) { p.style.display = (p.dataset.pane === name) ? '' : 'none'; });
    lsSet(_tabKey, name);
  }

  // ── 阅读 pane:从 opts.getReadState 回填显示 ──
  function refreshRead() {
    var rs = {};
    try { if (typeof _opts.getReadState === 'function') rs = _opts.getReadState() || {}; } catch (_) {}
    var fs = (rs.fs != null) ? rs.fs : 100, lh = (rs.lh != null) ? rs.lh : 1.7, th = rs.th || 'paper';
    var fv = $('eph2-fs-v'); if (fv) fv.textContent = fs + '%';
    var lv = $('eph2-lh-v'); if (lv) lv.textContent = (+lh).toFixed(1);
    if (modal) modal.querySelectorAll('#eph2-theme button').forEach(function (b) { b.classList.toggle('on', b.dataset.th === th); });
  }

  // ── 高亮 pane:渲染色板 + 删除(照搬 PDF renderHlColorSetting;仅 EPUB 内部路径,PDF 由原生渲同一容器)──
  function renderHl() {
    var c = $('set-hl-colors'); if (!c) return;
    c.innerHTML = '';
    hlColors().forEach(function (col) {
      var w = document.createElement('div'); w.className = 'swatch-w';
      var sw = document.createElement('span'); sw.className = 'swatch'; sw.style.background = col; w.appendChild(sw);
      var del = document.createElement('button'); del.className = 'del'; del.textContent = '×'; del.title = '删除';
      del.addEventListener('click', function () {
        var cur = hlColors().filter(function (x) { return x !== col; });
        saveHlColors(cur.length ? cur : DEFAULT_HL.slice());
        renderHl();
      });
      w.appendChild(del); c.appendChild(w);
    });
  }

  // ── 语法 pane:KG 启用列表(EPUB 路径;PDF 由原生 renderGrammarTrackList 填同一容器)──
  function fillGrammarList() {
    var box = $('set-grammar-list'); if (!box) return;
    if (_opts.grammarFile && window.RC && RC.grammar && RC.grammar.renderTrackList) {
      RC.grammar.renderTrackList('set-grammar-list', { file: _opts.grammarFile });
    } else {
      box.innerHTML = '<div style="color:#7a8497">勾选本书用哪些语法 KG、跟踪哪些语法点，请在右侧抽屉「语法」tab 顶部的「⚙ 启用语法 KG」里设置（去技能树点节点的「👁 跟踪」开）。</div>';
    }
  }

  // ── EPUB 内部回填(PDF 走 opts.onFill = 原生 _fillSettings,不进这里)──
  function fillInternal() {
    _loadSentConfig();   // 句子翻译源:从服务端拉 backend/model/effort 回填(等价 PDF openSettings)
    var dbg = $('set-debug'); if (dbg) dbg.checked = (lsGet(LS.debug) === '1');
    _applyDebugVisibility();
    var gv = $('set-grammar-view');
    if (gv) gv.value = (window.RC && RC.grammar && RC.grammar.getViewMode) ? RC.grammar.getViewMode(LS.grammarView) : (lsGet(LS.grammarView) || 'components');
    fillGrammarList();
    var curLangs = (typeof _opts.getBookLangs === 'function' ? (_opts.getBookLangs() || []) : []);
    var lc = document.getElementById(_ids.langChecks);
    if (lc) lc.querySelectorAll('input').forEach(function (c) { c.checked = curLangs.indexOf(c.value) >= 0; });
    var ct = $('set-click-translate'); if (ct) ct.checked = (lsGet('eph-click-translate') !== '0');   // 默认开
    var vu = $('set-vocab-underline'); if (vu) vu.checked = (lsGet('eph-vocab-underline') !== '0');   // 默认开
    var fb = $('eph2-fig-badge'); if (fb) fb.checked = (lsGet('eph-fig-badge') !== '0');   // 默认开
    // 侧边栏外观回填(优先读 RC.sidedrawer 的 getter,回退 localStorage;默认 不悬浮 / 20px)
    var _sd = (window.RC && RC.sidedrawer) ? RC.sidedrawer : null;
    var sf = $('eph2-side-floating');
    if (sf) sf.checked = _sd && _sd.getFloating ? _sd.getFloating() : (lsGet(LS.sideFloat) === '1');
    var sbVal = _sd && _sd.getBlur ? _sd.getBlur() : parseInt(lsGet(LS.sideBlur) || '20', 10);
    if (isNaN(sbVal)) sbVal = 20;
    var sb = $('eph2-side-blur'); if (sb) sb.value = sbVal;
    var sbv = $('eph2-side-blur-val'); if (sbv) sbv.textContent = sbVal;
    refreshRead();
    renderHl();
  }

  // ── EPUB 内部保存(语义逐字对照 PDF saveSettings:model/effort/debug/生词下划线/点词翻译 落盘 +
  //    POST 句子翻译配置 + 关面板;PDF 走 opts.onSave = 原生 saveSettings,不进这里)──
  function saveInternal() {
    try {
      var dbg = $('set-debug');
      if (dbg) { lsSet(LS.debug, dbg.checked ? '1' : '0'); _applyDebugVisibility(); }
      var vu = $('set-vocab-underline');
      if (vu) {
        if (typeof _opts.onVocabUnderline === 'function') call('onVocabUnderline', vu.checked);   // 驱动自行持久化 eph-vocab-underline + 重画
        else lsSet('eph-vocab-underline', vu.checked ? '1' : '0');
      }
      var ct = $('set-click-translate');
      if (ct) {
        if (typeof _opts.onClickTranslate === 'function') call('onClickTranslate', ct.checked);   // 驱动自行持久化 eph-click-translate
        else lsSet('eph-click-translate', ct.checked ? '1' : '0');
      }
      _saveSentConfig();
    } catch (ex) { toast('设置保存出错：' + (ex && ex.message)); }
    hide();
  }

  function hide() { if (mask) mask.style.display = 'none'; }
  function cancel() {   // 取消/点遮罩:不保存(对齐 PDF 原生:只有「保存」才落盘)
    if (typeof _opts.onCancel === 'function') { try { _opts.onCancel(); } catch (e) {} }
    hide();
  }

  // ── 打开 ──
  // ── 143:语音·调用前垫话 总策略(服务端 /api/assistant/tool-prompt?tool=_global;单工具覆盖在详情窗)──
  function _fillFiller() {
    var sel = $('set-filler-mode'), th = $('set-filler-th'), row = $('set-filler-th-row');
    if (!sel || !th) return;
    function sync() { if (row) row.style.display = (sel.value === 'auto') ? '' : 'none'; }
    fetch('/api/assistant/tool-prompt?tool=_global').then(function (r) { return r.json(); }).then(function (d) {
      var g = (d && d.filler && d.filler.global) || {};
      sel.value = g.mode || 'auto';
      th.value = g.threshold_s != null ? g.threshold_s : 2.5;
      sync();
    }).catch(function () {});
    if (sel._b) return;   // 只绑一次(面板 DOM 复用)
    sel._b = 1;
    function save() {
      sync();
      fetch('/api/assistant/tool-prompt', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tool: '_global', op: 'filler_global', mode: sel.value, threshold_s: parseFloat(th.value) }) })
        .then(function (r) { return r.json(); })
        .then(function (r) { if (!r || !r.ok) toast('垫话策略保存失败'); })
        .catch(function () { toast('垫话策略保存失败'); });
    }
    sel.addEventListener('change', save);
    th.addEventListener('change', save);
  }

  function open(opts) {
    _opts = opts || {};
    if (_opts.host) _host = _opts.host;
    if (_opts.ids) { for (var k in _opts.ids) if (_opts.ids[k]) _ids[k] = _opts.ids[k]; }
    if (_opts.keys && _opts.keys.tab) _tabKey = _opts.keys.tab;
    ensureDom();
    gateSections();
    _renderAiInline();   // AI tab 内嵌模型配置表(host 无关,数据在服务端;PDF 的 onFill 不管这块)
    _fillFiller();       // 143:语音垫话总策略(同上——服务端数据,PDF 的 onFill 也不管)
    _fillNotePane();     // 便签 tab 回填(host 无关,设备级 rc-note-* 键;PDF 的 onFill 同样不管这块)
    if (typeof _opts.onFill === 'function') {
      try { _opts.onFill(); } catch (e) { toast('设置回填失败：' + (e && e.message)); }   // PDF:原生 _fillSettings(同名 id 全量回填)
    } else {
      fillInternal();
    }
    setTab(_opts.tab || lsGet(_tabKey) || 'ai');
    mask.style.display = 'flex';
  }
  function close() { cancel(); }   // 对外 close 保留;语义=取消(不保存,对齐 PDF closeSettings)

  // 启动即按持久化的 debug 开关显隐左下角浮窗(等价 PDF setTimeout(_applyDebugVisibility, 0);PDF 页内部自动跳过)
  try { setTimeout(_applyDebugVisibility, 0); } catch (_) {}

  window.RC.settings = {
    open: open, close: close,
    aiParams: aiParams, hlColors: hlColors,
    injectCss: injectCss
  };
})();
