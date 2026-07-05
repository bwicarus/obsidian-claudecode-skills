/* epub2-grammar.js — epub.js 版 EPUB 阅读器:语法分析(全套)。
 *
 * 移植源:PDF 阅读器 reader.src/18-grammar.js(依存图/成分块/主干/弧线四模式 + 启用语法 KG 设置 +
 *   流式翻译/语法点 + 历史持久化 + 追问 + 制卡)。**逐字照搬其纯渲染逻辑**(_renderStructInto / _renderTree /
 *   _renderSkeleton / _renderComponents / _renderDepSvg / _renderDepTree / POS 配色),只把 PDF 全局换成 EPUB 底座:
 *
 *   PDF 全局            → EPUB 底座
 *   ─────────────────────────────────────────────
 *   FILE_REL            → FREL (window.__epub.cfg.fileRel)
 *   md(...)             → RC.md(...)
 *   _esc/_toast         → RC.esc / RC.toast
 *   _getAiOverrides()   → RC.settings.aiParams()
 *   _aiStream(...)      → RC.result.aiStream(...)   (签名一致:url,{method,body,onText}→{ok,text,error})
 *   __safeFetch(...)    → safeFetch(...)            (网络错误重试)
 *   #grammar-panel-body → #ep-grammar-body
 *   openGrammarPanel/switchSideTab('grammar') → RC.sidedrawer.open('grammar')
 *   _startBgJob/_pollJob/_failBgJob → 本模块自带(rc-result 未暴露这些私有函数)
 *   currentPage 出处链接 → location.origin + '/pdf/epub/view?file=' + FREL
 *
 * ── 选区/句子来源(EPUB 没有 PDF 的字符层)──
 *   两个入口都汇到 window.onGrammarAnalyze(opts):
 *     (a) 字典框「📊 语法」→ epub2.js 的 RC.wordpop.show({..., onGrammar}) 回调 → onGrammarAnalyze({word, ctx})
 *         focus=点的词,ctx=该词所在段落文本。
 *     (b) 选区工具栏「📊 语法」→ epub2.js selBar handler act==='grammar' → onGrammarAnalyze({fromSelection:true})
 *         读 window.__epub.curSel() 拿 {text,ctx};focus=选区文本,ctx=所在块文本。
 *   句子抽取 helper:把 ctx 按句末标点(.!?。！？… + 引号/换行)切句,返回包含 focus 的那一句(找不到退化用整 ctx 截断)。
 *
 * ── 抽屉 grammar pane ──
 *   pane #ep-side-grammar(模板提供)= 顶部「⚙ 启用语法 KG」可折叠区(#ep-grammar-set / #ep-grammar-kglist) +
 *   分析块容器 #ep-grammar-body。tab 由本模块动态插进 rc-sidedrawer 的 #ep-side-tabs(class ep-side-tab,
 *   data-pane="grammar"),click → RC.sidedrawer.setTab('grammar');pane 变 active 由 MutationObserver 触发懒加载
 *   (照搬 epub2-extra.js 接知识点 pane 的做法)。
 */
(function () {
  'use strict';
  if (window.__epub2Grammar) return;

  function ready(fn) {
    if (window.__epub && window.__epub.rendition && window.__epub.book && window.RC &&
        window.RC.sidedrawer && window.RC.result && window.RC.wordpop && window.RC.settings) fn();
    else setTimeout(function () { ready(fn); }, 120);
  }
  ready(init);

  function init() {
    if (window.__epub2Grammar) return; window.__epub2Grammar = true;

    var CFG = window.__epub.cfg || {};
    var FREL = CFG.fileRel || '';
    var $ = function (id) { return document.getElementById(id); };
    var RC = window.RC;

    function _esc(s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;'); }
    function md(s) { return RC.md ? RC.md(s) : _esc(s); }
    function toast(m) { if (RC.toast) RC.toast(m); }
    function aiParams() { try { return (RC.settings && RC.settings.aiParams) ? (RC.settings.aiParams() || {}) : {}; } catch (e) { return {}; } }
    function aiStream(url, opts) { return RC.result.aiStream(url, opts); }
    // 网络错误重试(对照 PDF __safeFetch:幂等计算,切后台被掐→回前台自动重试不重复副作用)
    function safeFetch(url, init, opts) {
      opts = opts || {}; var retries = opts.retries || 0;
      return fetch(url, init).catch(function (e) {
        if (retries > 0) return new Promise(function (res) { setTimeout(res, 600); }).then(function () { return safeFetch(url, init, { retries: retries - 1 }); });
        throw e;
      });
    }

    // ════════════════════════════════════════════════════════════════════
    // CSS 注入(照搬 pdf_reader.html 的 grammar CSS,scope 到 #ep-side-grammar / #ep-grammar-body;幂等)
    // ════════════════════════════════════════════════════════════════════
    (function injectCss() {
      if ($('ep2-grammar-css')) return;
      var st = document.createElement('style'); st.id = 'ep2-grammar-css';
      st.textContent = `
/* ── pane 自身 chrome:设置区(启用 KG)钉顶 + 分析块容器滚动 ── */
#ep-side-grammar{padding:0}
#ep-grammar-set{flex:0 0 auto;border-bottom:1px solid #1c2740;background:#0b1020}
#ep-grammar-set-head{display:flex;align-items:center;gap:6px;padding:9px 12px;cursor:pointer;font-size:12px;color:#9fb4d4;user-select:none}
#ep-grammar-set-head:hover{background:#141d33}
#ep-grammar-set-head .gset-caret{margin-left:auto;color:#7a8497;font-size:10px;transition:transform .15s}
#ep-grammar-set.open #ep-grammar-set-head .gset-caret{transform:rotate(90deg)}
#ep-grammar-kglist{display:none;padding:4px 10px 10px;max-height:42vh;overflow-y:auto;-webkit-overflow-scrolling:touch}
#ep-grammar-set.open #ep-grammar-kglist{display:block}
#ep-grammar-body{flex:1 1 auto;min-height:0;overflow-y:auto;overflow-x:hidden;-webkit-overflow-scrolling:touch;padding:10px 10px 40px}
#ep-grammar-body .gb-empty{color:#5a6680;font-size:12px;padding:18px 12px;text-align:center;line-height:1.6}

/* ── 一次分析 = 一个可折叠卡片,文档流堆叠(照搬 pdf_reader.html .grammar-block 系) ── */
#ep-side-grammar .grammar-block{position:relative;margin-bottom:12px;border:1px solid #2a3550;border-radius:8px;background:#0d1322;overflow:hidden}
#ep-side-grammar .grammar-block.focus{border-color:#fbbf24;box-shadow:0 0 0 1px rgba(251,191,36,.3)}
#ep-side-grammar .grammar-block .gb-header{display:flex;align-items:center;gap:7px;padding:10px 11px;cursor:pointer;user-select:none}
#ep-side-grammar .grammar-block .gb-header:hover{background:#141d33}
#ep-side-grammar .grammar-block .gb-header .gb-title{flex:1;min-width:0;font-size:12px;color:#cfe6ff;line-height:1.4;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#ep-side-grammar .grammar-block .gb-header .gb-badge{flex:0 0 auto;font-size:9px;color:#7dd3fc;background:#16243f;border-radius:8px;padding:1px 7px}
#ep-side-grammar .grammar-block .gb-header .gb-del{flex:0 0 auto;color:#5a6680;font-size:12px;cursor:pointer;padding:0 3px;opacity:.7}
#ep-side-grammar .grammar-block .gb-header .gb-del:hover{color:#ef4444;opacity:1}
#ep-side-grammar .grammar-block .gb-header .gb-caret{flex:0 0 auto;color:#7a8497;font-size:11px;transition:transform .15s}
#ep-side-grammar .grammar-block.open .gb-header .gb-caret{transform:rotate(90deg)}
#ep-side-grammar .grammar-block .gb-content{display:none}
#ep-side-grammar .grammar-block.open .gb-content{display:block}
#ep-side-grammar .grammar-block .gb-fu-answers{display:none;padding:0 12px}
#ep-side-grammar .grammar-block.open .gb-fu-answers{display:block}
#ep-side-grammar .grammar-block .gb-fu-q{margin-top:10px;padding-top:8px;border-top:1px solid #2a3550;color:#a8cdff;font-size:12px;font-weight:600}
#ep-side-grammar .grammar-block .gb-fu-a{margin-top:5px;color:#e6e6f0;font-size:12.5px;line-height:1.6}
#ep-side-grammar .grammar-block .gb-followup{display:none;gap:6px;padding:8px 12px 12px}
#ep-side-grammar .grammar-block.open .gb-followup{display:flex}
#ep-side-grammar .grammar-block .gb-fu-input{flex:1;min-width:0;background:#0d1322;border:1px solid #2a3550;color:#cfe6ff;border-radius:6px;padding:7px 10px;font-size:12px}
#ep-side-grammar .grammar-block .gb-fu-input:focus{outline:none;border-color:#3b6db5}
#ep-side-grammar .grammar-block .gb-followup button{background:#244470;border:1px solid #3b6db5;color:#fff;border-radius:6px;padding:7px 14px;cursor:pointer;font-size:12px;white-space:nowrap}
#ep-side-grammar .grammar-block .gb-followup button.gb-anki-btn{background:#13351f;border-color:#34d399;color:#7ee2b8;padding:7px 10px}
#ep-side-grammar .grammar-block .gb-trans{padding:11px 12px;font-size:12px;color:#cfe6ff;line-height:1.55;border-top:1px solid #1c2740}
#ep-side-grammar .grammar-block .gb-loading{padding:14px;text-align:center;color:#7a8497;font-style:italic;font-size:12px}
#ep-side-grammar .grammar-block .gb-pending{opacity:.65;font-style:italic;animation:ep2gbpulse 1.2s ease-in-out infinite}
@keyframes ep2gbpulse{0%,100%{opacity:.4}50%{opacity:.8}}

/* 句子结构图:默认收起的二级折叠区 */
#ep-side-grammar .gb-diagram-wrap{border-top:1px solid #1c2740}
#ep-side-grammar .gb-diagram-toggle{display:flex;align-items:center;gap:6px;padding:9px 12px;cursor:pointer;font-size:11px;color:#9fb4d4;user-select:none}
#ep-side-grammar .gb-diagram-toggle:hover{background:#141d33}
#ep-side-grammar .gb-diagram-toggle .gv-switch{display:inline-flex;gap:2px;margin-left:8px}
#ep-side-grammar .gb-diagram-toggle .gv-switch button{background:#0d1322;border:1px solid #2a3550;color:#7a8497;font-size:10px;border-radius:4px;padding:1px 6px;cursor:pointer;line-height:1.5}
#ep-side-grammar .gb-diagram-toggle .gv-switch button:hover{color:#cfe6ff;border-color:#3b6db5}
#ep-side-grammar .gb-diagram-toggle .gv-switch button.active{background:#1a2540;color:#7dd3fc;border-color:#3b6db5;font-weight:600}
#ep-side-grammar .gb-diagram-toggle .dg-caret{margin-left:auto;color:#7a8497;font-size:10px;transition:transform .15s}
#ep-side-grammar .gb-diagram-wrap.dgram-open .gb-diagram-toggle .dg-caret{transform:rotate(90deg)}
#ep-side-grammar .grammar-block .gb-diagram{display:none;overflow-x:auto;overflow-y:hidden;padding:6px 10px 12px;-webkit-overflow-scrolling:touch}
#ep-side-grammar .gb-diagram-wrap.dgram-open .gb-diagram{display:block}
#ep-side-grammar .grammar-block .gb-diagram svg{display:block}
/* 长句按从句分段 */
#ep-side-grammar .dep-clause{margin-bottom:12px}
#ep-side-grammar .dep-clause:last-child{margin-bottom:0}
#ep-side-grammar .dep-clause-label{display:inline-block;font-size:10px;font-weight:600;color:#0d1322;background:#7dd3fc;border-radius:4px;padding:1px 7px;margin:0 0 3px 2px}
/* 成分分块(主谓宾定状从句,彩色块) */
#ep-side-grammar .comp-blocks{display:flex;flex-direction:column;gap:5px;padding:2px 0}
#ep-side-grammar .comp-block{border-left:3px solid #64748b;border-radius:0 6px 6px 0;background:#0d1322;padding:6px 9px;line-height:1.5}
#ep-side-grammar .comp-block .comp-label{display:inline-block;font-size:9px;font-weight:600;color:#0d1322;border-radius:4px;padding:1px 7px;margin-right:7px;vertical-align:middle}
#ep-side-grammar .comp-block .comp-text{font-size:13px;color:#e6e6f0}
/* 主干+修饰折叠 */
#ep-side-grammar .sk-blocks{font-size:14px;line-height:2;color:#e6e6f0;padding:2px 0}
#ep-side-grammar .sk-core{color:#e6e6f0}
#ep-side-grammar .sk-mod{display:inline-block;font-size:11px;border:1px dashed currentColor;border-radius:5px;padding:0 6px;margin:0 2px;cursor:pointer;opacity:.85;vertical-align:middle;white-space:normal}
#ep-side-grammar .sk-mod:hover{opacity:1;background:rgba(255,255,255,.06)}
#ep-side-grammar .sk-mod.open{border-style:solid}
/* 成分树(融合) */
#ep-side-grammar .ctree{font-size:13px;padding:2px 0}
#ep-side-grammar .ctree .ct-row{display:flex;align-items:baseline;gap:6px;padding:3px 0}
#ep-side-grammar .ctree .ct-label{flex:0 0 auto;font-size:9px;font-weight:600;color:#0d1322;border-radius:4px;padding:1px 6px}
#ep-side-grammar .ctree .ct-text{color:#e6e6f0;line-height:1.45}
#ep-side-grammar .ctree .ct-caret{flex:0 0 auto;margin-left:auto;color:#7a8497;font-size:10px;cursor:pointer;padding:0 4px;user-select:none}
#ep-side-grammar .ctree .ct-children{margin-left:13px;border-left:1px dashed #2a3550;padding-left:9px}
#ep-side-grammar .ctree .ct-text.ct-core{color:#9fb4d4}
/* displaCy 词 / 词性标签 / 弧线 */
#ep-side-grammar .dep-token{cursor:pointer}
#ep-side-grammar .dep-word{font-size:15px;font-weight:600}
#ep-side-grammar .dep-pos{font-size:9px;font-family:monospace;fill:#8a9bb4}
#ep-side-grammar .dep-arc{fill:none;stroke:#5a6e94;stroke-width:1.5}
#ep-side-grammar .dep-arrow{fill:#5a6e94}
#ep-side-grammar .dep-label{font-size:9px;fill:#7dd3fc;font-weight:600}
#ep-side-grammar .dep-tree .dep-tscroll{overflow-x:auto;overflow-y:hidden;-webkit-overflow-scrolling:touch}
#ep-side-grammar .dep-tree .dep-tsub{margin:4px 0 8px 14px;border-left:1px dashed #2a3550;padding-left:9px}
#ep-side-grammar .dep-token.dep-ph .dep-word{text-decoration:underline dotted}
/* 跟踪语法点列表 */
#ep-side-grammar .gb-analyses{border-top:1px solid #1c2740;padding:2px 0}
#ep-side-grammar .gb-ana{padding:9px 12px;border-bottom:1px solid #141c30;cursor:pointer}
#ep-side-grammar .gb-ana:last-child{border-bottom:none}
#ep-side-grammar .gb-ana .a-head{font-size:12px;font-weight:600;color:#a8cdff}
#ep-side-grammar .gb-ana .a-phrase{font-size:10px;color:#facc15;font-family:monospace;margin-top:3px}
#ep-side-grammar .gb-ana .a-body{display:none;font-size:11px;color:#cfe6ff;line-height:1.55;margin-top:6px}
#ep-side-grammar .gb-ana.open .a-body{display:block}
#ep-side-grammar .gb-ana .a-ex{margin:5px 0 0 16px;padding:0;font-size:10px;color:#7a8497;line-height:1.5}
/* 启用语法 KG 列表项 */
#ep-grammar-kglist .gk-empty{color:#7a8497;font-size:12px;padding:6px 2px;line-height:1.6}
#ep-grammar-kglist label{display:flex;align-items:center;gap:8px;padding:6px 4px;cursor:pointer;color:#cfe6ff;border-radius:3px;border-bottom:1px solid #1f2740}
/* 依存图悬停提示(浮在主文档 body,不在 pane 内) */
#ep-dep-tip{position:fixed;z-index:260;background:#1a2540;border:1px solid #3b6db5;border-radius:6px;padding:5px 9px;font-size:11px;color:#e6e6f0;pointer-events:none;max-width:220px;box-shadow:0 4px 14px rgba(0,0,0,.5);display:none}`;
      document.head.appendChild(st);
    })();

    // ════════════════════════════════════════════════════════════════════
    // 抽屉接线:动态插「语法」tab 进 rc-sidedrawer 的 #ep-side-tabs + grammar pane 懒加载
    // ════════════════════════════════════════════════════════════════════
    var GRAMMAR_TAB_ICON =
      '<svg class="si" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">' +
      '<path d="M5 4v6a3 3 0 0 0 3 3h8M19 4v6a3 3 0 0 1-3 3"/>' +
      '<circle cx="5" cy="3.5" r="1.4" fill="currentColor" stroke="none"/>' +
      '<circle cx="19" cy="3.5" r="1.4" fill="currentColor" stroke="none"/>' +
      '<circle cx="12" cy="20" r="1.4" fill="currentColor" stroke="none"/>' +
      '<path d="M12 13v5"/></svg>';
    (function wireTab(tries) {
      var bar = $('ep-side-tabs');
      if (!bar) { if (tries < 60) setTimeout(function () { wireTab(tries + 1); }, 250); return; }
      if (bar.querySelector('.ep-side-tab[data-pane="grammar"]')) return;   // 幂等
      var btn = document.createElement('button');
      btn.className = 'ep-side-tab'; btn.dataset.pane = 'grammar';
      btn.innerHTML = GRAMMAR_TAB_ICON + '语法';
      btn.addEventListener('click', function () { try { RC.sidedrawer.setTab('grammar'); } catch (e) {} });
      var sp = bar.querySelector('.ep-side-tab-sp');   // 插在空白撑条前(= tab 列表末尾,✕ 之前)
      if (sp) bar.insertBefore(btn, sp); else bar.appendChild(btn);
      // 边角:若 grammar pane 已是 active(localStorage 记忆 + rc-sidedrawer init 先于本 tab 建好)→ 同步高亮本 tab
      var gp = $('ep-side-grammar'); if (gp && gp.classList.contains('active')) btn.classList.add('active');
    })(0);

    // grammar pane 变 active → 懒加载(每次刷启用列表,历史只载一次)。MutationObserver 覆盖所有进 pane 的路径
    // (点 tab / open('grammar') / setTab),照搬 epub2-extra.js 接知识点 pane 的做法。
    (function wirePane(tries) {
      var pane = $('ep-side-grammar');
      if (!pane) { if (tries < 60) setTimeout(function () { wirePane(tries + 1); }, 250); return; }
      var onActive = function () { setTimeout(function () { onGrammarPaneActive(); }, 0); };
      if (window.MutationObserver) {
        new MutationObserver(function () { if (pane.classList.contains('active')) onActive(); })
          .observe(pane, { attributes: true, attributeFilter: ['class'] });
        if (pane.classList.contains('active')) onActive();
      }
      // 设置区折叠开关
      var head = $('ep-grammar-set-head');
      if (head) head.addEventListener('click', function () { var s = $('ep-grammar-set'); if (s) s.classList.toggle('open'); });
    })(0);

    function onGrammarPaneActive() {
      try { renderGrammarTrackList(); } catch (e) {}             // 刷新启用 KG 列表(内部会 loadGrammarTracked)
      if (!_grammarHistLoaded) { try { loadGrammarHistory(); } catch (e) {} }
    }
    window._openGrammarSet = function () { var s = $('ep-grammar-set'); if (s) s.classList.add('open'); };

    // 选区工具栏「📊 语法」按钮:未跟踪 → 隐藏(照搬 PDF _updateGrammarBtnVisibility,改"点击才拦"为"直接隐藏")。
    // epub2.js 选区刷新会把它按 data-grp 显回来 → MutationObserver 监 #ep-sel 子树属性变更即时再隐 + setInterval 兜底。
    // 只在 !_grammarHasTracked 时强制 display:none;tracked 时不强行显示(交回 epub2.js data-grp 决定)。
    var _grammarSelApply = null;
    (function wireSelBtn(tries) {
      var sel = $('ep-sel');
      if (!sel) { if (tries < 60) setTimeout(function () { wireSelBtn(tries + 1); }, 250); return; }
      var apply = function () {
        var btn = sel.querySelector('[data-act="grammar"]'); if (!btn) return;
        if (!_grammarHasTracked && btn.style.display !== 'none') btn.style.display = 'none';   // 仅在需要改时写,避免触发自身 MutationObserver 死循环
      };
      _grammarSelApply = apply;
      if (window.MutationObserver) new MutationObserver(apply).observe(sel, { attributes: true, attributeFilter: ['style', 'class'], subtree: true });
      setInterval(apply, 500);
      apply();
    })(0);

    // ════════════════════════════════════════════════════════════════════
    // 启用语法 KG(per-EPUB)+ 跟踪状态(照搬 18-grammar.js)
    // ════════════════════════════════════════════════════════════════════
    var _grammarEnabledBooks = [];   // 本书启用的 grammar KG(books)
    var _grammarHasTracked = false;  // 启用书中是否至少一个含 tracked 节点
    var _grammarLoaded = false;      // 是否已拉过一次启用列表

    function loadGrammarTracked() {
      return fetch('/pdf/api/grammar-tracked?file=' + encodeURIComponent(FREL))
        .then(function (r) { return r.json(); })
        .then(function (d) { _grammarEnabledBooks = (d && d.enabled_books) || []; })
        .catch(function () { _grammarEnabledBooks = []; })
        .then(function () { _grammarLoaded = true; return _refreshGrammarHasTracked(); })
        .then(function () { if (_grammarSelApply) _grammarSelApply(); return _grammarEnabledBooks; });
    }
    function _refreshGrammarHasTracked() {
      if (!_grammarEnabledBooks.length) { _grammarHasTracked = false; return Promise.resolve(); }
      return fetch('/pdf/api/grammar-books').then(function (r) { return r.json(); }).then(function (d) {
        var enabledSet = {};
        _grammarEnabledBooks.forEach(function (b) { enabledSet[b] = 1; });
        _grammarHasTracked = ((d && d.books) || []).some(function (b) { return enabledSet[b.book] && (b.tracked_count || 0) > 0; });
      }).catch(function () { _grammarHasTracked = false; });
    }
    function saveGrammarEnabledBooks(books) {
      _grammarEnabledBooks = books;
      return fetch('/pdf/api/grammar-tracked', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file: FREL, enabled_books: books }),
      }).catch(function () {}).then(function () { return _refreshGrammarHasTracked(); }).then(function () { if (_grammarSelApply) _grammarSelApply(); });
    }
    function renderGrammarTrackList() {
      var wrap = $('ep-grammar-kglist'); if (!wrap) return;
      return fetch('/pdf/api/grammar-books').then(function (r) { return r.json(); }).then(function (d) {
        var books = (d && d.books) || [];
        return loadGrammarTracked().then(function () {
          var enabledSet = {};
          _grammarEnabledBooks.forEach(function (b) { enabledSet[b] = 1; });
          if (!books.length) {
            wrap.innerHTML = '<div class="gk-empty">还没有语法 KG。新建一本 kind=grammar 的书后会出现。</div>';
            return;
          }
          var html = '';
          for (var i = 0; i < books.length; i++) {
            var b = books[i];
            var checked = enabledSet[b.book] ? 'checked' : '';
            var hot = (b.tracked_count > 0)
              ? '<span style="color:#34d399;margin-left:4px">' + b.tracked_count + ' 已跟踪</span>'
              : '<span style="color:#7a8497;margin-left:4px">无跟踪（去技能树点节点开）</span>';
            html += '<label title="共 ' + b.total_l2 + ' 个 level-2 语法点">' +
              '<input type="checkbox" value="' + _esc(b.book) + '" ' + checked + ' onchange="_onGrammarBookToggle()" style="margin:0">' +
              '<div style="flex:1;min-width:0">' +
              '<div style="font-size:12px">' + _esc(b.title) + '</div>' +
              '<div style="font-size:10px;color:#7a8497">' + b.total_l2 + ' 个语法点 · ' + hot + '</div>' +
              '</div>' +
              '<a href="/skilltree/' + encodeURIComponent(b.book) + '/" target="_blank" onclick="event.stopPropagation()" style="color:#60a5fa;font-size:11px;text-decoration:none">技能树 →</a>' +
              '</label>';
          }
          wrap.innerHTML = html;
        });
      }).catch(function () {});
    }
    window._onGrammarBookToggle = function () {
      var books = [].slice.call(document.querySelectorAll('#ep-grammar-kglist input[type=checkbox]:checked')).map(function (cb) { return cb.value; });
      saveGrammarEnabledBooks(books);
    };

    // ════════════════════════════════════════════════════════════════════
    // 句子抽取:把 ctx 按句末标点切句,返回包含 focus 的那一句(找不到退化用整 ctx 截断)
    // ════════════════════════════════════════════════════════════════════
    function _splitSentences(text) {
      var ENDERS = '.!?。！？…';
      var CLOSERS = '”’"\'）)」』】>';
      var out = [], cur = '';
      for (var i = 0; i < text.length; i++) {
        var ch = text[i];
        if (ch === '\n' || ch === '\r') { if (cur.trim()) out.push(cur); cur = ''; continue; }
        cur += ch;
        if (ENDERS.indexOf(ch) >= 0) {
          while (i + 1 < text.length && CLOSERS.indexOf(text[i + 1]) >= 0) { cur += text[++i]; }   // 收尾引号
          out.push(cur); cur = '';
        }
      }
      if (cur.trim()) out.push(cur);
      return out.map(function (s) { return s.trim(); }).filter(Boolean);
    }
    function extractSentence(ctx, focus) {
      ctx = String(ctx == null ? '' : ctx).replace(/ /g, ' ').replace(/\s+/g, ' ').trim();
      focus = String(focus == null ? '' : focus).replace(/\s+/g, ' ').trim();
      if (!ctx) return focus;
      var sents = _splitSentences(ctx);
      if (focus) {
        // 大小写不敏感(字典框传来的 word 被 rc-wordpop 转成小写,而正文里可能首字母大写/句首)
        var fl = focus.toLowerCase();
        for (var i = 0; i < sents.length; i++) { if (sents[i].toLowerCase().indexOf(fl) >= 0) return sents[i]; }
        // focus 本身跨句(含句末标点)→ 用 focus 自己当一句(截断防过长)
        if (/[.!?。！？…]/.test(focus)) return focus.slice(0, 400);
      }
      if (sents.length === 1) return sents[0];
      return ctx.slice(0, 400);   // 找不到 → 整 ctx 截断(退化)
    }

    // ════════════════════════════════════════════════════════════════════
    // 入口:onGrammarAnalyze(opts) —— (a) 字典框 {word,ctx} (b) 选区 {fromSelection:true}
    // ════════════════════════════════════════════════════════════════════
    window.onGrammarAnalyze = function (opts) {
      opts = opts || {};
      var focus, ctx;
      if (opts.fromSelection) {
        var sel = (window.__epub && window.__epub.curSel) ? window.__epub.curSel() : null;
        focus = sel ? (sel.text || '') : '';
        ctx = sel ? (sel.ctx || '') : '';
      } else {
        focus = opts.word || '';
        ctx = opts.ctx || '';
      }
      focus = String(focus || '').trim();
      if (!focus) { toast('没有选中内容'); return; }

      var run = function () {
        if (!_grammarEnabledBooks.length) {
          toast('请在 PDF 设置中启用至少一个语法 KG');
          try { RC.sidedrawer.open('grammar'); } catch (e) {}
          window._openGrammarSet();
          return;
        }
        if (!_grammarHasTracked) {
          toast('已启用的 KG 中没有节点被跟踪，去技能树详情面板点「👁 跟踪」');
          try { RC.sidedrawer.open('grammar'); } catch (e) {}
          window._openGrammarSet();
          return;
        }
        var sentence = extractSentence(ctx, focus);
        if (!sentence || sentence.length < 6) { toast('句子太短'); return; }
        try { RC.sidedrawer.open('grammar'); } catch (e) {}   // 开抽屉 + 切语法 tab(顺带触发历史懒加载)
        _doAnalyze(sentence, focus);
      };
      if (_grammarLoaded) run(); else loadGrammarTracked().then(run);
    };

    function _doAnalyze(sentence, focus) {
      var blockId = 'gb_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6);
      var block = _addLoadingBlock(blockId, sentence, focus);
      safeFetch('/pdf/api/grammar-analyze', {   // 幂等计算:切后台被掐→回前台自动重试(不重复副作用)
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text: focus, sentence: sentence, file: FREL, enabled_books: _grammarEnabledBooks }),
      }, { retries: 2 }).then(function (r) {
        if (!r.ok) {
          var p = r.json().then(function (err) { return (err && err.error) || ('HTTP ' + r.status); }).catch(function () { return 'HTTP ' + r.status; });
          return p.then(function (msg) { _fillBlockError(block, msg); });
        }
        return r.json().then(function (d) {
          if (!d.ok) { _fillBlockError(block, d.error || '?'); return; }
          _fillGrammarBlock(block, d, sentence);                         // spaCy 依存图秒出(翻译/语法点先占位)
          if (d.engine === 'spacy') _streamGrammar(block, sentence, focus);  // AI 流式补翻译(先)+语法点
        });
      }).catch(function (e) { _fillBlockError(block, e.message); });
    }

    // AI 流式:先收翻译([[TRANS]]..[[/TRANS]] 先出→立刻显示),再收语法点([[POINTS]] JSON [[/POINTS]])
    function _streamGrammar(block, sentence, text) {
      if (!block) return;
      var acc = '', transDone = false, pointsDone = false;
      var tryParse = function () {
        if (!transDone) {
          var tm = acc.match(/\[\[TRANS\]\]([\s\S]*?)\[\[\/TRANS\]\]/);
          if (tm) { _setBlockTrans(block, tm[1].trim()); transDone = true; }
        }
        if (!pointsDone) {
          var pm = acc.match(/\[\[POINTS\]\]([\s\S]*?)\[\[\/POINTS\]\]/);
          if (pm) { _setBlockPoints(block, pm[1].trim()); pointsDone = true; }
        }
      };
      aiStream('/pdf/api/grammar-stream', {
        method: 'POST',
        body: { sentence: sentence, text: text, file: FREL, enabled_books: _grammarEnabledBooks },
        onText: function (t) { acc = t; tryParse(); },
      }).then(function (res) {
        if (!res.ok && !acc) { _setBlockTransFail(block); return; }
        acc = res.text || acc; tryParse();
        if (!transDone) {
          var tm = acc.match(/\[\[TRANS\]\]([\s\S]*?)(\[\[\/TRANS\]\]|\[\[POINTS\]\]|$)/);
          if (tm && tm[1].trim()) _setBlockTrans(block, tm[1].trim()); else _setBlockTransFail(block);
        }
        if (!pointsDone) {
          var pm = acc.match(/\[\[POINTS\]\]([\s\S]*?)(\[\[\/POINTS\]\]|$)/);
          _setBlockPoints(block, pm ? pm[1].trim() : '[]');
        }
        // 分析完成 → 保存到该书历史
        try {
          var sp = block.__spacy || {};
          fetch('/pdf/api/grammar-history-save', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ file: FREL, item: {
              sentence: sentence, text: text,
              sentence_zh: block.__zh || '',
              tokens: sp.tokens || [], deps: sp.deps || [], clauses: sp.clauses || [],
              components: sp.components || [], clause_tree: sp.clause_tree || null,
              analyses: block.__points || [],
            } }),
          }).catch(function () {});
        } catch (e) {}
      });
    }
    function _setBlockTrans(block, zh) {
      block.__zh = zh;
      var el = block.querySelector('.gb-trans');
      if (el) { el.classList.remove('gb-pending'); el.textContent = '🌐 ' + zh; }
    }
    function _setBlockTransFail(block) {
      var el = block.querySelector('.gb-trans');
      if (el) { el.classList.remove('gb-pending'); el.textContent = '🌐 （翻译失败，可重试）'; }
    }
    function _setBlockPoints(block, jsonStr) {
      var wrap = block.querySelector('.gb-analyses');
      if (!wrap) return;
      var arr = [];
      try { arr = JSON.parse(jsonStr) || []; } catch (e) { arr = []; }
      block.__points = arr;
      if (!arr.length) { wrap.remove(); return; }
      wrap.classList.remove('gb-pending');
      wrap.innerHTML = arr.map(function (a) {
        return '<div class="gb-ana">' +
          '<div class="a-head">📊 ' + _esc(a.point || a.node_name || '') + '</div>' +
          (a.phrase ? '<div class="a-phrase">📍 ' + _esc(a.phrase) + '</div>' : '') +
          '<div class="a-body">' + _esc(a.explanation || '') + ((a.examples || []).length ? '<ul class="a-ex">' + (a.examples || []).map(function (e) { return '<li>' + _esc(e) + '</li>'; }).join('') + '</ul>' : '') + '</div>' +
          '</div>';
      }).join('');
      wrap.querySelectorAll('.gb-ana').forEach(function (el) { el.addEventListener('click', function () { el.classList.toggle('open'); }); });
      var headEl = block.querySelector('.gb-header');
      if (headEl && !headEl.querySelector('.gb-badge')) {
        var badge = document.createElement('span');
        badge.className = 'gb-badge'; badge.textContent = '语法点 ' + arr.length;
        headEl.insertBefore(badge, headEl.querySelector('.gb-del'));
      }
    }

    // ════════════════════════════════════════════════════════════════════
    // 后台制卡进度条(rc-result 的 _startBgJob/_pollJob 未暴露 → 本模块自带等价实现)
    // ════════════════════════════════════════════════════════════════════
    var _gjSeq = 0;
    function _gjEnsure() {
      var c = $('bg-jobs');   // 复用 rc-result 的容器 id(存在则共栈,否则建一份同样式)
      if (!c) { c = document.createElement('div'); c.id = 'bg-jobs'; c.style.cssText = 'position:fixed;right:18px;bottom:80px;display:flex;flex-direction:column;gap:6px;z-index:520;align-items:flex-end'; document.body.appendChild(c); }
      return c;
    }
    function _gjStart(text) {
      var id = 'egj' + (++_gjSeq);
      var el = document.createElement('div'); el.id = id;
      el.style.cssText = 'background:#10162a;border:1px solid #3b6db5;color:#cfe6ff;padding:7px 12px;border-radius:8px;font-size:12px;box-shadow:0 4px 12px rgba(0,0,0,.5);max-width:280px';
      el.textContent = '⏳ ' + text;
      _gjEnsure().appendChild(el);
      return id;
    }
    function _gjFinish(id, text, url) {
      var el = $(id); if (!el) return;
      el.style.borderColor = '#34d399'; el.style.color = '#34d399';
      el.textContent = '✓ ' + text + (url ? ' · 点击打开' : '');
      if (url) { el.style.cursor = 'pointer'; el.onclick = function () { location.href = url; }; }
      setTimeout(function () { el.style.transition = 'opacity .4s'; el.style.opacity = '0'; setTimeout(function () { el.remove(); }, 400); }, url ? 8000 : 4500);
    }
    function _gjFail(id, text) {
      var el = $(id); if (!el) return;
      el.style.borderColor = '#f87171'; el.style.color = '#f87171'; el.style.cursor = 'pointer';
      el.textContent = '✗ ' + text + ' · 点关闭';
      el.onclick = function () { el.remove(); };
    }
    function _gjPoll(jobId, ui) {
      var tries = 0, unknown = 0;
      var iv = setInterval(function () {
        tries++;
        fetch('/pdf/api/job-status?id=' + encodeURIComponent(jobId)).then(function (r) { return r.json(); }).then(function (d) {
          if (d.status === 'done') {
            clearInterval(iv);
            var out = d.result || {};
            if (out.ok) {
              var parts = [];
              if (out.note_path) parts.push('笔记已建');
              if (out.anki_added) parts.push('Anki ' + out.anki_added + ' 张');
              _gjFinish(ui, parts.join(' · ') || '完成', out.obsidian_url || '');
            } else { _gjFail(ui, out.error || '失败'); }
          } else if (d.status === 'error') {
            clearInterval(iv); _gjFail(ui, d.error || '失败');
          } else if (d.status === 'unknown') {
            if (++unknown >= 3) { clearInterval(iv); _gjFail(ui, '任务丢失(服务重启?)'); }
          }
        }).catch(function () { if (tries > 180) { clearInterval(iv); _gjFail(ui, '轮询超时'); } });
      }, 2000);
    }

    // ════════════════════════════════════════════════════════════════════
    // 分析块:标题栏(可折叠)+ 内容区;流式堆叠,最新插到最上(照搬 18-grammar.js)
    // ════════════════════════════════════════════════════════════════════
    function _addLoadingBlock(id, sentence, text) {
      var body = $('ep-grammar-body');
      var ph = body.querySelector('.gb-empty'); if (ph) ph.remove();   // 去首屏占位
      var block = document.createElement('div');
      block.className = 'grammar-block focus open';   // loading 时默认展开看进度
      block.id = id;
      var summary = sentence.slice(0, 60) + (sentence.length > 60 ? '…' : '');
      block.dataset.src = sentence;
      block.dataset.text = text || '';
      block.innerHTML =
        '<div class="gb-header">' +
          '<span class="gb-title" title="' + _esc(sentence) + '">' + _esc(summary) + '</span>' +
          '<span class="gb-del" title="删除这条（同句下次重新分析）">🗑</span>' +
          '<span class="gb-caret">▶</span>' +
        '</div>' +
        '<div class="gb-trans gb-pending">🌐 翻译中…</div>' +
        '<div class="gb-content"><div class="gb-loading">⏳ 结构 / 语法分析中…</div></div>' +
        '<div class="gb-fu-answers"></div>' +
        '<div class="gb-followup">' +
          '<input class="gb-fu-input" placeholder="继续追问这句的语法…" onkeydown="if(event.key===\'Enter\'){event.preventDefault();_grammarFollowup(\'' + id + '\');}">' +
          '<button onclick="_grammarFollowup(\'' + id + '\')">追问</button>' +
          '<button class="gb-anki-btn" onclick="_grammarAnki(\'' + id + '\')" title="整句+译文+分析做成 Anki 卡">🎴</button>' +
        '</div>';
      block.querySelector('.gb-header').addEventListener('click', function () { block.classList.toggle('open'); });
      block.querySelector('.gb-del').addEventListener('click', function (e) {
        e.stopPropagation();
        fetch('/pdf/api/grammar-forget', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ sentence: block.dataset.src || '', text: block.dataset.text || '', file: FREL, enabled_books: _grammarEnabledBooks }),
        }).catch(function () {});
        block.remove();
      });
      body.querySelectorAll('.grammar-block').forEach(function (b) { if (b !== block && b.dataset.src === sentence) b.remove(); });   // 去同句旧卡
      body.insertBefore(block, body.firstChild);   // 最新在最上
      body.scrollTop = 0;
      return block;
    }
    function _fillBlockError(block, msg) {
      if (!block) return;
      var content = block.querySelector('.gb-content') || block;
      content.innerHTML = '<div class="gb-loading" style="color:#ef4444">分析失败：' + _esc(msg) + '</div>';
    }
    // 语法卡片「🎴」:整句 + 译文 + 分析 + 追问 → 一张 Anki 卡(后台,带原文出处链接)
    window._grammarAnki = function (blockId) {
      var block = $(blockId); if (!block) return;
      var sentence = block.dataset.src || '';
      var zh = (block.querySelector('.gb-trans') ? block.querySelector('.gb-trans').textContent : '').replace(/^🌐\s*/, '').trim();
      var analysis = (block.querySelector('.gb-content') ? block.querySelector('.gb-content').textContent : '').trim();
      var fu = (block.querySelector('.gb-fu-answers') ? block.querySelector('.gb-fu-answers').textContent : '').trim();
      if (!sentence && !analysis) { toast('没有可制卡的内容'); return; }
      var srcUrl = FREL ? (location.origin + '/pdf/epub/view?file=' + encodeURIComponent(FREL)) : '';
      var text = '【句子】' + sentence + (zh ? '\n【译文】' + zh : '') + (analysis ? '\n【语法分析】' + analysis : '') +
        (fu ? '\n【追问】' + fu : '') + (srcUrl ? '\n【原文出处链接（务必原样放进卡片背面，做成可点链接）】' + srcUrl : '');
      var jobUi = _gjStart('制 Anki 中…');
      var ov = aiParams();
      fetch('/pdf/api/snippets-to-async', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ snippets: [{ text: text, source: sentence }], make_note: false, make_anki: true, note_name: '', model: ov.model || '', effort: ov.effort || '' }),
      }).then(function (r) { return r.json(); }).then(function (d) {
        if (!d.ok || !d.job_id) { _gjFail(jobUi, d.error || '提交失败'); return; }
        _gjPoll(d.job_id, jobUi);
      }).catch(function (e) { _gjFail(jobUi, e.message); });
    };
    // 语法卡片底部「继续追问」:带原句 + 译文 + 已有分析作上下文,流式回答追加到卡片内
    window._grammarFollowup = function (blockId) {
      var block = $(blockId); if (!block) return;
      var inp = block.querySelector('.gb-fu-input');
      var q = (inp && inp.value || '').trim(); if (!q) return;
      inp.value = '';
      var sentence = block.dataset.src || '';
      var trans = (block.querySelector('.gb-trans') ? block.querySelector('.gb-trans').textContent : '').replace(/^🌐\s*/, '').trim();
      var analysis = (block.querySelector('.gb-content') ? block.querySelector('.gb-content').textContent : '').slice(0, 3000);
      var prev = (block.querySelector('.gb-fu-answers') ? block.querySelector('.gb-fu-answers').textContent : '').slice(-1500);
      var context = '【句子】' + sentence + (trans ? '\n【译文】' + trans : '') +
        '\n【已有语法分析】\n' + analysis + (prev ? '\n【之前的追问】\n' + prev : '');
      var ans = block.querySelector('.gb-fu-answers');
      var qDiv = document.createElement('div'); qDiv.className = 'gb-fu-q'; qDiv.textContent = '问：' + q; ans.appendChild(qDiv);
      var aDiv = document.createElement('div'); aDiv.className = 'gb-fu-a'; aDiv.innerHTML = '<span class="gb-loading">⏳</span>'; ans.appendChild(aDiv);
      var ov = aiParams();
      var render = function (t) { aDiv.innerHTML = md(t || ' '); if (RC.typeset) RC.typeset(aDiv); };
      aiStream('/pdf/api/explain', {
        method: 'POST', onText: render,
        body: { text: q, context: '基于这句的语法分析继续回答：\n' + context, model: ov.model || '', effort: ov.effort || '' },
      }).then(function (res) {
        if (res.ok && res.text) render(res.text);
        else if (!res.ok) aDiv.innerHTML = '追问失败：' + _esc(res.error || '失败');
        else aDiv.innerHTML = '(无回答)';
      }).catch(function (e) { aDiv.innerHTML = '追问失败：' + _esc(e.message); });
    };

    function _fillGrammarBlock(block, d, sentence) {
      if (!block) return;
      var tokens = d.tokens || [];
      var deps = d.deps || [];
      var sentence_zh = d.sentence_zh || '';
      var analyses = d.analyses || [];
      var components = d.components || [];
      if (!tokens.length && !sentence_zh && !analyses.length && !components.length) {
        _fillBlockError(block, d.raw ? 'AI 未返回结构化结果' : '无结果');
        return;
      }
      block.__spacy = { tokens: tokens, deps: deps, clauses: d.clauses || [], components: components, clause_tree: d.clause_tree };
      var isSpacy = d.engine === 'spacy';
      var transEl = block.querySelector('.gb-trans');
      if (transEl) {
        if (sentence_zh) { transEl.classList.remove('gb-pending'); transEl.textContent = '🌐 ' + sentence_zh; }
        else if (!isSpacy) { transEl.remove(); }
      }
      var headEl = block.querySelector('.gb-header');
      if (headEl && analyses.length && !headEl.querySelector('.gb-badge')) {
        var badge = document.createElement('span');
        badge.className = 'gb-badge'; badge.textContent = '语法点 ' + analyses.length;
        headEl.insertBefore(badge, headEl.querySelector('.gb-del'));
      }
      var hasStruct = components.length || tokens.length;
      var gvHtml = GV_MODES.map(function (m) { return '<button type="button" data-gv="' + m[0] + '" class="' + (m[0] === _grammarViewMode ? 'active' : '') + '">' + m[1] + '</button>'; }).join('');
      var diagramHtml = hasStruct
        ? '<div class="gb-diagram-wrap"><div class="gb-diagram-toggle">📐 句子结构<span class="gv-switch">' + gvHtml + '</span><span class="dg-caret">▶</span></div><div class="gb-diagram"></div></div>'
        : '';
      var anaHtml = analyses.length
        ? '<div class="gb-analyses">' + analyses.map(function (a, i) {
            return '<div class="gb-ana" data-i="' + i + '">' +
              '<div class="a-head">📊 ' + _esc(a.node_name || a.node_id || '') + '</div>' +
              (a.phrase ? '<div class="a-phrase">📍 ' + _esc(a.phrase) + '</div>' : '') +
              '<div class="a-body">' + _esc(a.explanation || '') + ((a.examples || []).length ? '<ul class="a-ex">' + (a.examples || []).map(function (e) { return '<li>' + _esc(e) + '</li>'; }).join('') + '</ul>' : '') + '</div>' +
              '</div>';
          }).join('') + '</div>'
        : (isSpacy ? '<div class="gb-analyses gb-pending"><div class="gb-ana"><div class="a-head" style="color:#7a8497;font-weight:400">⏳ 语法点分析中…</div></div></div>' : '');
      var content = block.querySelector('.gb-content');
      content.innerHTML = diagramHtml + anaHtml;
      if (hasStruct) {
        var wrap = content.querySelector('.gb-diagram-wrap');
        var diag = content.querySelector('.gb-diagram');
        _renderStructInto(diag, wrap, { tokens: tokens, deps: deps, clauses: d.clauses || [], components: components });
        content.querySelector('.gb-diagram-toggle').addEventListener('click', function () { wrap.classList.toggle('dgram-open'); });
        content.querySelectorAll('.gv-switch button').forEach(function (btn) { btn.addEventListener('click', function (e) { e.stopPropagation(); setGrammarView(btn.dataset.gv); }); });
      }
      content.querySelectorAll('.gb-ana').forEach(function (el) { el.addEventListener('click', function () { el.classList.toggle('open'); }); });
      setTimeout(function () { block.classList.remove('focus'); }, 5000);
    }

    // 结构显示模式
    var GV_MODES = [['tree', '树'], ['components', '块'], ['skeleton', '主干'], ['deps', '弧线']];
    var _grammarViewMode = localStorage.getItem('eph-grammar-view') || 'components';
    window.setGrammarView = function (mode) {
      _grammarViewMode = ['deps', 'skeleton', 'components', 'tree'].indexOf(mode) >= 0 ? mode : 'components';
      try { localStorage.setItem('eph-grammar-view', _grammarViewMode); } catch (e) {}
      document.querySelectorAll('#ep-grammar-body .grammar-block').forEach(function (b) {
        if (!b.__spacy) return;
        var diag = b.querySelector('.gb-diagram'), wrap = b.querySelector('.gb-diagram-wrap');
        if (diag) _renderStructInto(diag, wrap, b.__spacy);
      });
      document.querySelectorAll('#ep-side-grammar .gv-switch button').forEach(function (b) { b.classList.toggle('active', b.dataset.gv === _grammarViewMode); });
    };
    function _renderStructInto(diag, wrap, sp) {
      diag.innerHTML = '';
      var comps = sp.components || [], toks = sp.tokens || [], deps = sp.deps || [], clauses = sp.clauses || [];
      if (_grammarViewMode === 'tree' && comps.length) {
        diag.appendChild(_renderTree(comps)); if (wrap) wrap.classList.add('dgram-open');
      } else if (_grammarViewMode === 'skeleton' && comps.length) {
        diag.appendChild(_renderSkeleton(comps)); if (wrap) wrap.classList.add('dgram-open');
      } else if (_grammarViewMode === 'components' && comps.length) {
        diag.appendChild(_renderComponents(comps)); if (wrap) wrap.classList.add('dgram-open');
      } else if (sp.clause_tree) {
        diag.appendChild(_renderDepTree(sp.clause_tree));
      } else if (toks.length) {
        if (clauses.length > 1) {
          for (var ci = 0; ci < clauses.length; ci++) {
            var c = clauses[ci];
            if (!(c.tokens || []).length) continue;
            var seg = document.createElement('div'); seg.className = 'dep-clause';
            var lbl = document.createElement('div'); lbl.className = 'dep-clause-label'; lbl.textContent = c.label || '从句';
            seg.appendChild(lbl); seg.appendChild(_renderDepSvg(c.tokens, c.deps || [])); diag.appendChild(seg);
          }
        } else {
          diag.appendChild(_renderDepSvg(toks, deps));
        }
        diag.addEventListener('wheel', function (e) {
          if (diag.scrollWidth <= diag.clientWidth) return;
          var dy = Math.abs(e.deltaY) > Math.abs(e.deltaX) ? e.deltaY : e.deltaX;
          diag.scrollLeft += dy; e.preventDefault();
        }, { passive: false });
      }
    }
    function _renderSkeleton(comps) {
      var CORE = { '主语': 1, '谓语': 1, '宾语': 1, '间接宾语': 1, '表语': 1, '宾语补足语': 1, '形式主语': 1 };
      var wrap = document.createElement('div'); wrap.className = 'sk-blocks';
      for (var i = 0; i < comps.length; i++) {
        var c = comps[i], label = c.label || '';
        if (CORE[label]) {
          var s = document.createElement('span'); s.className = 'sk-core'; s.textContent = c.text || ''; wrap.appendChild(s);
        } else {
          var chip = document.createElement('span'); chip.className = 'sk-mod';
          chip.style.color = _compColor(label); chip.dataset.text = c.text || ''; chip.dataset.label = label;
          chip.textContent = '[' + label + ' …]';
          chip.addEventListener('click', (function (chip, label) {
            return function () { var open = chip.classList.toggle('open'); chip.textContent = open ? ('[' + label + '：' + chip.dataset.text + ']') : ('[' + label + ' …]'); };
          })(chip, label));
          wrap.appendChild(chip);
        }
        wrap.appendChild(document.createTextNode(' '));
      }
      return wrap;
    }
    function _renderTree(comps) {
      var children = {};
      comps.forEach(function (c, i) { var p = (c.parent == null ? -1 : c.parent); (children[p] = children[p] || []).push(i); });
      var fullText = function (idx) {
        var acc = [];
        var collect = function (i) { acc.push(comps[i]); (children[i] || []).forEach(collect); };
        collect(idx);
        acc.sort(function (a, b) { return (a.start || 0) - (b.start || 0); });
        return acc.map(function (c) { return c.text || ''; }).join(' ');
      };
      var build = function (idx) {
        var c = comps[idx], col = _compColor(c.label || '');
        var kids = children[idx] || [];
        var node = document.createElement('div'); node.className = 'ctree-node';
        var row = document.createElement('div'); row.className = 'ct-row';
        var txt = document.createElement('span'); txt.className = 'ct-text';
        txt.textContent = kids.length ? fullText(idx) : (c.text || '');
        row.innerHTML = '<span class="ct-label" style="background:' + col + '">' + _esc(c.label || '') + '</span>';
        row.appendChild(txt); node.appendChild(row);
        var isClause = (c.label || '').indexOf('从句') >= 0;
        if (kids.length) {
          var car = document.createElement('span'); car.className = 'ct-caret'; car.textContent = '▸';
          row.appendChild(car);
          var box = document.createElement('div'); box.className = 'ct-children'; box.style.display = 'none';
          if (isClause && (c.text || '').trim()) {
            var pred = document.createElement('div'); pred.className = 'ctree-node';
            pred.innerHTML = '<div class="ct-row"><span class="ct-label" style="background:' + _compColor('谓语') + '">谓语</span><span class="ct-text">' + _esc(c.text) + '</span></div>';
            box.appendChild(pred);
          }
          kids.forEach(function (k) { box.appendChild(build(k)); });
          node.appendChild(box);
          var toggle = function (e) {
            e.stopPropagation();
            var open = box.style.display === 'none';
            box.style.display = open ? '' : 'none';
            car.textContent = open ? '▾' : '▸';
            txt.textContent = open ? (isClause ? '' : (c.text || '')) : fullText(idx);
            txt.classList.toggle('ct-core', open && !isClause);
          };
          car.addEventListener('click', toggle);
          txt.style.cursor = 'pointer';
          txt.addEventListener('click', toggle);
        }
        return node;
      };
      var wrap = document.createElement('div'); wrap.className = 'ctree';
      (children[-1] || []).forEach(function (i) { wrap.appendChild(build(i)); });
      return wrap;
    }
    function _compColor(label) {
      if (label.indexOf('谓语') >= 0) return '#ef4444';
      if (label.indexOf('主语') >= 0) return '#3b82f6';
      if (label.indexOf('宾语') >= 0) return '#22c55e';
      if (label.indexOf('定语') >= 0) return '#06b6d4';
      if (label.indexOf('状语') >= 0) return '#a855f7';
      if (label.indexOf('表语') >= 0 || label.indexOf('补语') >= 0) return '#eab308';
      if (label.indexOf('并列') >= 0) return '#94a3b8';
      return '#8a9bb4';
    }
    function _renderComponents(comps) {
      var wrap = document.createElement('div'); wrap.className = 'comp-blocks';
      for (var i = 0; i < comps.length; i++) {
        var c = comps[i], col = _compColor(c.label || '');
        var b = document.createElement('div'); b.className = 'comp-block'; b.style.borderLeftColor = col;
        b.innerHTML = '<span class="comp-label" style="background:' + col + '">' + _esc(c.label || '') + '</span><span class="comp-text">' + _esc(c.text || '') + '</span>';
        wrap.appendChild(b);
      }
      return wrap;
    }
    // displaCy 依存图(弧线在词上方、词性着色、词性中文标在词下)
    function _renderDepSvg(tokens, deps, onNodeClick) {
      var NS = 'http://www.w3.org/2000/svg';
      var PAD_X = 18, GAP = 40, FONT = 16, POS_GAP = 18;
      var ARC_BASE = 24, ARC_STEP = 19;
      var meas = document.createElement('canvas').getContext('2d');
      meas.font = '600 ' + FONT + 'px -apple-system,system-ui,sans-serif';
      var widths = tokens.map(function (t) { return Math.max(meas.measureText(t.text || '').width, 12); });
      var cx = []; var x = PAD_X;
      for (var i = 0; i < tokens.length; i++) { cx.push(x + widths[i] / 2); x += widths[i] + GAP; }
      var totalW = Math.max(x - GAP + PAD_X, 60);
      var arcs = deps.map(function (dp) { return Object.assign({}, dp, { span: Math.abs(dp.head - dp.child) }); }).sort(function (a, b) { return a.span - b.span; });
      var maxSpan = arcs.length ? Math.max.apply(null, arcs.map(function (a) { return a.span; })) : 1;
      var wordBaseY = ARC_BASE + maxSpan * ARC_STEP + 26;
      var svgH = wordBaseY + POS_GAP + 8;
      var svg = document.createElementNS(NS, 'svg');
      svg.setAttribute('width', totalW); svg.setAttribute('height', svgH);
      svg.setAttribute('viewBox', '0 0 ' + totalW + ' ' + svgH);
      var arcBottomY = wordBaseY - FONT - 4;
      for (var ai = 0; ai < arcs.length; ai++) {
        var a = arcs[ai];
        var x1 = cx[a.head], x2 = cx[a.child];
        var dir = x2 > x1 ? 1 : -1;
        var top = arcBottomY - (ARC_BASE + a.span * ARC_STEP);
        var sx = x1 + dir * 3, ex = x2 - dir * 3;
        var path = document.createElementNS(NS, 'path');
        path.setAttribute('d', 'M ' + sx + ' ' + arcBottomY + ' C ' + sx + ' ' + top + ', ' + ex + ' ' + top + ', ' + ex + ' ' + arcBottomY);
        path.setAttribute('class', 'dep-arc'); svg.appendChild(path);
        var arrow = document.createElementNS(NS, 'path');
        arrow.setAttribute('d', 'M ' + (ex - 3) + ' ' + (arcBottomY - 5) + ' L ' + (ex + 3) + ' ' + (arcBottomY - 5) + ' L ' + ex + ' ' + arcBottomY + ' Z');
        arrow.setAttribute('class', 'dep-arrow'); svg.appendChild(arrow);
        if (a.label) {
          var lbl = document.createElementNS(NS, 'text');
          lbl.setAttribute('x', (sx + ex) / 2); lbl.setAttribute('y', top - 2);
          lbl.setAttribute('text-anchor', 'middle'); lbl.setAttribute('class', 'dep-label');
          lbl.textContent = a.label; svg.appendChild(lbl);
        }
      }
      for (var ti = 0; ti < tokens.length; ti++) {
        var t = tokens[ti];
        var isClause = (t.pos === 'clause') || (t.ref != null);
        var col = isClause ? '#7dd3fc' : _posColor(t.pos);
        var g = document.createElementNS(NS, 'g');
        g.setAttribute('class', 'dep-token' + (isClause ? ' dep-ph' : ''));
        var bw = widths[ti] + 8;
        var bg = document.createElementNS(NS, 'rect');
        bg.setAttribute('x', cx[ti] - bw / 2); bg.setAttribute('y', wordBaseY - FONT);
        bg.setAttribute('width', bw); bg.setAttribute('height', FONT + 5);
        bg.setAttribute('rx', 4); bg.setAttribute('fill', col); bg.setAttribute('opacity', isClause ? '0.22' : '0.16');
        if (isClause) { bg.setAttribute('stroke', col); bg.setAttribute('stroke-dasharray', '3 2'); bg.setAttribute('stroke-width', '1'); }
        g.appendChild(bg);
        var w = document.createElementNS(NS, 'text');
        w.setAttribute('x', cx[ti]); w.setAttribute('y', wordBaseY - 2);
        w.setAttribute('text-anchor', 'middle'); w.setAttribute('class', 'dep-word'); w.setAttribute('fill', col);
        w.textContent = t.text || ''; g.appendChild(w);
        var p = document.createElementNS(NS, 'text');
        p.setAttribute('x', cx[ti]); p.setAttribute('y', wordBaseY + POS_GAP - 2);
        p.setAttribute('text-anchor', 'middle'); p.setAttribute('class', 'dep-pos');
        p.textContent = isClause ? '点开▾' : (POS_LABEL[(t.pos || '').toLowerCase()] || t.pos || '');
        g.appendChild(p);
        if (isClause && onNodeClick) {
          g.style.cursor = 'pointer';
          (function (ti, g) { g.addEventListener('click', function (ev) { ev.stopPropagation(); onNodeClick(ti, g); }); })(ti, g);
        } else if (t.zh) {
          var tip = (t.text || '') + '　' + t.zh;
          (function (tip) {
            g.addEventListener('mouseenter', function (ev) { _showDepTip(ev, tip); });
            g.addEventListener('mousemove', _moveDepTip);
            g.addEventListener('mouseleave', _hideDepTip);
            g.addEventListener('click', function (ev) { _showDepTip(ev, tip); setTimeout(_hideDepTip, 2600); });
          })(tip);
        }
        svg.appendChild(g);
      }
      return svg;
    }
    function _renderDepTree(tree) {
      var wrap = document.createElement('div'); wrap.className = 'dep-tree';
      var renderClause = function (clause, container) {
        var seg = document.createElement('div'); seg.className = 'dep-tlevel';
        var childBox = document.createElement('div'); childBox.className = 'dep-tchildren';
        var svg = _renderDepSvg(clause.nodes || [], clause.deps || [], function (i) {
          var node = (clause.nodes || [])[i];
          if (!node || node.ref == null) return;
          var exist = childBox.querySelector(':scope > [data-ref="' + node.ref + '"]');
          if (exist) { exist.remove(); return; }
          var sub = document.createElement('div'); sub.dataset.ref = node.ref; sub.className = 'dep-tsub';
          var lbl = document.createElement('div'); lbl.className = 'dep-clause-label';
          lbl.textContent = (clause.children[node.ref] || {}).label || '从句';
          sub.appendChild(lbl);
          renderClause(clause.children[node.ref], sub);
          childBox.appendChild(sub);
        });
        var sc = document.createElement('div'); sc.className = 'dep-tscroll'; sc.appendChild(svg);
        sc.addEventListener('wheel', function (e) {
          if (sc.scrollWidth <= sc.clientWidth) return;
          sc.scrollLeft += (Math.abs(e.deltaY) > Math.abs(e.deltaX) ? e.deltaY : e.deltaX);
          e.preventDefault();
        }, { passive: false });
        seg.appendChild(sc); seg.appendChild(childBox); container.appendChild(seg);
      };
      renderClause(tree, wrap);
      return wrap;
    }

    // ── POS 配色 + 中文短标签(displaCy 风格)──
    var POS_COLORS = {
      noun: '#3b82f6', verb: '#ef4444', adj: '#22c55e', adv: '#a855f7',
      pron: '#ec4899', prep: '#06b6d4', det: '#64748b', conj: '#eab308',
      aux: '#f97316', num: '#14b8a6', part: '#8b5cf6', intj: '#f43f5e', punct: '#475569',
    };
    var POS_LABEL = {
      noun: '名', verb: '动', adj: '形', adv: '副', pron: '代', prep: '介',
      det: '限', conj: '连', aux: '助', num: '数', part: '小品', intj: '叹', punct: '标',
    };
    function _posColor(p) { return POS_COLORS[(p || '').toLowerCase()] || '#64748b'; }

    // ── 依存图悬停提示 ──
    var _depTipEl = null;
    function _ensureDepTip() { if (!_depTipEl) { _depTipEl = $('ep-dep-tip'); if (!_depTipEl) { _depTipEl = document.createElement('div'); _depTipEl.id = 'ep-dep-tip'; document.body.appendChild(_depTipEl); } } return _depTipEl; }
    function _showDepTip(ev, text) { var el = _ensureDepTip(); el.textContent = text; el.style.display = 'block'; _moveDepTip(ev); }
    function _moveDepTip(ev) { if (!_depTipEl) return; _depTipEl.style.left = ((ev.clientX || 0) + 12) + 'px'; _depTipEl.style.top = ((ev.clientY || 0) + 14) + 'px'; }
    function _hideDepTip() { if (_depTipEl) _depTipEl.style.display = 'none'; }

    // ════════════════════════════════════════════════════════════════════
    // 历史:按书本持久(state/grammar-history/<sha>.json),新旧倒序(最新在上)
    // ════════════════════════════════════════════════════════════════════
    var _grammarHistLoaded = false;
    function loadGrammarHistory() {
      _grammarHistLoaded = true;
      return fetch('/pdf/api/grammar-history?file=' + encodeURIComponent(FREL || ''))
        .then(function (r) { return r.json(); })
        .then(function (d) { var items = (d && d.items) || []; for (var i = 0; i < items.length; i++) _addHistoryBlock(items[i]); })
        .catch(function () {});
    }
    window.loadGrammarHistory = loadGrammarHistory;
    function _addHistoryBlock(item) {
      var body = $('ep-grammar-body'); if (!body) return;
      var ph = body.querySelector('.gb-empty'); if (ph) ph.remove();   // 去首屏占位
      var sentence = item.sentence || '', text = item.text || '';
      var block = document.createElement('div'); block.className = 'grammar-block';   // 历史卡默认折叠
      var hid = 'gbh_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6);
      block.id = hid; block.dataset.src = sentence; block.dataset.text = text;
      var summary = sentence.slice(0, 60) + (sentence.length > 60 ? '…' : '');
      block.innerHTML =
        '<div class="gb-header">' +
          '<span class="gb-title" title="' + _esc(sentence) + '">' + _esc(summary) + '</span>' +
          '<span class="gb-del" title="删除这条（同句下次重新分析）">🗑</span>' +
          '<span class="gb-caret">▶</span>' +
        '</div>' +
        '<div class="gb-trans"></div>' +
        '<div class="gb-content"></div>' +
        '<div class="gb-fu-answers"></div>' +
        '<div class="gb-followup">' +
          '<input class="gb-fu-input" placeholder="继续追问这句的语法…" onkeydown="if(event.key===\'Enter\'){event.preventDefault();_grammarFollowup(\'' + hid + '\');}">' +
          '<button onclick="_grammarFollowup(\'' + hid + '\')">追问</button>' +
          '<button class="gb-anki-btn" onclick="_grammarAnki(\'' + hid + '\')" title="整句+译文+分析做成 Anki 卡">🎴</button>' +
        '</div>';
      block.querySelector('.gb-header').addEventListener('click', function () { block.classList.toggle('open'); });
      block.querySelector('.gb-del').addEventListener('click', function (e) {
        e.stopPropagation();
        fetch('/pdf/api/grammar-forget', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ sentence: block.dataset.src || '', text: block.dataset.text || '', file: FREL, enabled_books: _grammarEnabledBooks }),
        }).catch(function () {});
        block.remove();
      });
      body.appendChild(block);
      _fillGrammarBlock(block, item, sentence);   // item 无 engine → 译文行 + 语法点 + 依存图(不流式,_fillGrammarBlock 内填 gb-trans)
    }

    // 清空侧栏内全部分析卡(parity)
    window.clearGrammarBlocks = function () { document.querySelectorAll('#ep-grammar-body .grammar-block').forEach(function (b) { b.remove(); }); };

    // 启动即拉一次启用列表(让 onGrammarAnalyze 第一次就有判据;不打开抽屉)
    loadGrammarTracked();
  }
})();
