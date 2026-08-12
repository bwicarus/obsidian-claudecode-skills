/* pdf-adapter.js — PDF 阅读器接统一控制层(window.RC)的适配器。独立 classic 脚本,
 * 仅在 ?ui=shared 时由模板条件加载;模块局部量由门控点当 opts 传入(不触 reader.js 作用域)。
 * 单词查词(lookupWord)直连 RC.wordpop.show(核心小框),与 EPUB 调同一个函数、同一套参数形状,
 * 不再经 RC.dict(轻框)两段式中转;其余阶段见各方法上的分段注释。 */
(function () {
  if (window.PdfAdapter) return;

  // ── ui=shared 运行时开关(优先读后端注入的 __PDF_CFG.ui_shared,回退 URL) ──
  window.__uiShared = (function () {
    try {
      if (window.__PDF_CFG && window.__PDF_CFG.ui_shared) return true;
      return new URLSearchParams(location.search).get('ui') === 'shared';
    } catch (e) { return false; }
  })();

  // 选区视口 rect:取已渲染的 .sel-overlay .hl 的并集(getBoundingClientRect 已含全部 CSS zoom,
  // 避免重抄 __viewportScale/ptToLocal 烘焙坑)。无高亮时回退页 wrap 左上点。
  function _rectFromSel(cs) {
    if (!cs || !cs.pw) return null;
    var hls = cs.pw.querySelectorAll('.sel-overlay .hl');
    if (hls && hls.length) {
      var L = Infinity, T = Infinity, R = -Infinity, B = -Infinity;
      hls.forEach(function (h) {
        var r = h.getBoundingClientRect();
        if (r.width || r.height) { L = Math.min(L, r.left); T = Math.min(T, r.top); R = Math.max(R, r.right); B = Math.max(B, r.bottom); }
      });
      if (L < Infinity) return { left: L, top: T, right: R, bottom: B, width: R - L, height: B - T };
    }
    var pr = cs.pw.getBoundingClientRect();
    return { left: pr.left, top: pr.top, right: pr.left, bottom: pr.top, width: 0, height: 0 };
  }

  // 视口里带手写的**插入页**(.pdf-upage,PDF 文件本身空白、服务端裁图两手空空)→ 返回它,给 see_ink 截前端图。
  function _pdfInkUpageEl() {
    var ups = document.querySelectorAll('.pdf-upage');
    for (var i = 0; i < ups.length; i++) {
      var el = ups[i], r = el.getBoundingClientRect();
      if (r.bottom > 0 && r.top < (window.innerHeight || 0) && el.__inkCanvas &&
          el.__inkStrokes && el.__inkStrokes.length) return el;   // 在视口内 + 有手写
    }
    return null;
  }
  // 视口里的自建页(**不要求有墨迹**)—— 让 read_page 遇自建页能返回前端渲染图(题目+手写,所见即所得)。
  function _pdfUpageElAny() {
    var ups = document.querySelectorAll('.pdf-upage');
    for (var i = 0; i < ups.length; i++) {
      var el = ups[i], r = el.getBoundingClientRect();
      if (r.bottom > 0 && r.top < (window.innerHeight || 0)) return el;
    }
    return null;
  }

  var PdfAdapter = window.PdfAdapter = {
    kind: 'pdf',
    _host: null,
    __shotEl: null,

    // 阶段2 用:reader.src 末尾桥接段喂模块内部量。阶段1 lookupWord 不依赖它。
    bind: function (host) { PdfAdapter._host = host || null; return PdfAdapter; },

    // ── 统一中间层契约(设计见 /reader-middlelayer-design.md):PDF 也接进 RC._adapter(27-rc-adapter.js 里 RC.use),
    //    让助手经 RC.adapter().getContext() 取上下文,与 EPUB 用完全一样的方式,不再直连各自阅读器。──
    // 能力位(照 HtmlAdapter/EpubAdapter.config 形状):PDF 有页、后端可 PyMuPDF 渲任意区域。
    config: { isPDF: true, reflow: false, hasFigures: true, hasFormula: true, hasImages: false,
              renderRegion: true, dictMode: 'sse', popupMode: 'char-layer', clickWordDetect: true,
              anchorKind: 'pdf-char', supportsVoice: true },
    // 助手上下文:**只读**地包 window.__voiceContext()(它同时被前端语音助手 voice.js 共用 → 绝不改它本体/字段)。
    // 便签合并留在 25-assistant.js ctx() 消费侧(读 __noteAttached);本方法只负责"取阅读器当前上下文"。
    getContext: function () {
      try {
        var c = (typeof window.__voiceContext === 'function') ? window.__voiceContext() : null;
        // 给每张图补统一 opaque ref(设计 §8):助手/后端只透传 ref、不看 page/box。note 类不加(有 note_id)。旧字段保留兜底。
        if (c && c.figures && c.figures.length) {
          c.figures.forEach(function (f) { if (f && !f.ref && f.box && f.kind !== 'note') f.ref = { kind: 'pdf', page: f.page, box: f.box }; });
        }
        // 用户点子:视口里带手写的**插入页**(.pdf-upage,服务端 PDF 空白渲不出)→ 声明 want_viewshot,
        //   共享 send 会 await 一张 captureShot(截那张插入页元素,所见即所得)喂给 see_ink,不再两手空空。
        //   普通 PDF 页有真墨迹的仍走服务端精确裁图(_ink_focus_image,快、无往返),不设此标志。
        try {
          var _inkEl = _pdfInkUpageEl() || _pdfUpageElAny();   // 有墨迹的优先;没墨迹的自建页也截(供 read_page 返回渲染图)
          if (_inkEl && c) { c.want_viewshot = true; PdfAdapter.__shotEl = _inkEl; }
        } catch (e) {}
        // recent_check 专线已退役:上下文告知统一走服务端**创造物库**(_sys_prompt 直读
        //   _creations_recent_line,纸/报告/搜索/翻译一并覆盖),前端不再逐项拼。
        return c || null;
      } catch (e) { return null; }
    },
    // want_viewshot 的实际截图:截当前那张插入页元素(RC.captureEl 通用原语);没记到就整视口兜底。
    captureShot: function () {
      var el = PdfAdapter.__shotEl;
      if (el && window.RC && RC.captureEl) return RC.captureEl(el);
      return (window.RC && RC.captureView) ? RC.captureView() : Promise.resolve(null);
    },
    collectFigures: function () { try { var c = (typeof window.__voiceContext === 'function') ? window.__voiceContext() : null; return (c && c.figures) || []; } catch (e) { return []; } },

    /* lookupWord(opts) — 单词查询 LIVE 入口:直连 RC.wordpop.show(核心小框),与 EPUB(epub-html.js
     * 的 dict 分支)调同一个函数、同一套参数形状,不再经 RC.dict(轻框)两段式中转。门控点传全量 opts:
     *   word(必填) / context(所在句,≤400) / page / file(FILE_REL) / langs(BOOK_LANGS)
     *   anchorRect: _charSel({pw,startIdx,endIdx}) 或一个 DOMRect-like
     *   fallback(word,ctx): RC 不可用 / 非英日(rc-wordpop onFallback)时退回原 showWordPopover
     * 不传 markHighlight/onMastered:PDF 已有独立高亮入口(工具栏色板)+ 掌握后下划线刷新走
     *   _refreshUnderlines 对同名全局 window.refreshVocabUnderlinesForAllPages 的自动调用,无需额外接线。
     * onGrammar 照抄 PDF 原生小框(reader.src/15-phrase-wordpop.js)已有写法:调 window.onGrammarAnalyze()
     *   忽略参数,内部自己读闭包里的 lastSelText/_charSel 拼句子。 */
    lookupWord: function (opts) {
      opts = opts || {};
      var word = String(opts.word || '').trim();
      if (!word) return;
      if (!(window.RC && RC.wordpop && RC.wordpop.show)) {   // RC 没就绪 → 退原逻辑,绝不吞掉查词
        if (opts.fallback) opts.fallback(word, opts.context || '');
        return;
      }
      var rect = _rectFromSel(opts.anchorRect);
      if (!rect && opts.anchorRect && opts.anchorRect.left != null) rect = opts.anchorRect;
      // 呼吸高亮 host-bind:门控点传的 anchorRect 就是查词时刻的 _charSel({pw,startIdx,endIdx})→ 捕获给
      // wordHlWrap(27-rc-adapter.js,照抄原生 renderWordHl 页面坐标系渲染,层锚在 pw 内随内容滚动零漂移)。
      // 没有 host 方法/捕获失败 → 不传 breathe,共享层落 position:fixed 兜底(会漂移,仅极端兜底)。
      var cap = (opts.anchorRect && opts.anchorRect.pw && opts.anchorRect.startIdx != null)
        ? { pw: opts.anchorRect.pw, startIdx: opts.anchorRect.startIdx, endIdx: opts.anchorRect.endIdx } : null;
      var canWrap = cap && PdfAdapter._host && PdfAdapter._host.wordHlWrap;
      RC.wordpop.show({
        word: word,
        rect: rect,
        ctx: opts.context || '',
        file: opts.file || '',
        page: opts.page || 0,
        langs: opts.langs || [],                         // 纯汉字词判英/日与门控 _isJaWord 对齐
        noBreathe: !!opts.noBreathe,                     // 已知词(有生词下划线)→ 不呼吸,直接弹占位框秒填
        ignoreSelector: '#sel-toolbar',                  // 「🔍查词」按钮点开小框时 toolbar 仍开着,点 toolbar 其余按钮不误关小框
        showAnki: false,                                 // PDF 原生小框只有「掌握+语法」两个按钮,没有🎴Anki(选中工具栏已有🎴制卡入口)
        onGrammar: function () { if (window.onGrammarAnalyze) window.onGrammarAnalyze(); },
        onFallback: function (w) { if (opts.fallback) opts.fallback(w, opts.context || ''); },
        breathe: canWrap ? {
          wrap: function () { return PdfAdapter._host.wordHlWrap(cap); },
          unwrap: function (el) { try { el.remove(); } catch (_) {} }
        } : null,
        // 框定位也复用原生 _positionWordPop(absolute-in-#main,开着框滚动时框随内容走;共享层默认 fixed 不跟滚)
        positionPop: (cap && PdfAdapter._host && PdfAdapter._host.positionWordPop)
          ? function (pop) { PdfAdapter._host.positionWordPop(pop, cap); } : null
      });
    },

    // ════════════════ 阶段2:解释/翻译/对话 → rc-result;全词典 → rc-wordpop;词组 → rc-phrasepop ════════════════
    // 共享模式下 reader.js 后加载、赢全局 → 结果框底部「🖌 标记 / 🎴 制 Anki / 追问」仍是 reader.js 的全局
    // (用 _resultContext + saveHighlight),故**不**给 rc-result 传 markHighlight/ankiSource(避免双实现);端点全
    // 默认 /pdf/api/*(正确)。门控点(reader.src)在调用前已设好 reader.js 的 _resultContext(标记/制卡锚)。
    // RC 任一不可用 → opts.fallback(门控点给的原 PDF 行为),绝不吞功能。
    _resultCfgDone: false,
    // 旧 per-request model/effort 覆盖(pdf-ai-overrides)已废弃(2026-07 收口,唯一真源 = 服务端
    // action 预设);保留注入点给 rc-result(签名不变),恒返 {}(后端各端点也已不读这两个参数)。
    _aiParams: function () { return {}; },
    _ensureResultCfg: function () {                    // 一次性:草稿键对齐 PDF('pdf-drafts',跟 reader.js 同 localStorage)+ AI 覆盖 + 开框前快照历史
      if (PdfAdapter._resultCfgDone) return;
      if (!(window.RC && RC.result && RC.result.config)) return;
      RC.result.config({
        draftKey: 'pdf-drafts', aiParams: PdfAdapter._aiParams,
        // 每次 rc-result.openResult 前快照上一条结果进「📜 历史」(等价 native openResult 顶部的 _pushQueryHistory)
        beforeOpen: function () { try { if (window._pushQueryHistory) window._pushQueryHistory(); } catch (e) {} },
      });
      PdfAdapter._resultCfgDone = true;
    },
    // 照抄 PDF 原生 onExplain 尾段(reader.src/21-misc-ai.js):解释**不开面板**,选区建一个一直闪烁的
    // 琥珀高亮,AI 后台跑;点高亮才开解释页 + 移除高亮(一次点击)。高亮的建立/移除/背景任务 100% 复用
    // host 里绑定的原生实现(未改一字),这里只是把调用点从 native onExplain 接到 PdfAdapter.explain。
    // 建不了高亮(罕见,如没有 charSel)→ 退回旧式直接开面板(RC.result.aiCall)。
    explain: function (opts) {
      opts = opts || {};
      PdfAdapter._ensureResultCfg();
      var h = PdfAdapter._host;
      if (h && h.explainHighlight) {
        var ehl = null;
        try { ehl = h.explainHighlight(opts.text || ''); } catch (e) { ehl = null; }
        if (ehl) {
          if (h.runExplainBg) { try { h.runExplainBg(ehl, opts.text || '', opts.context || ''); } catch (e) {} }
          return;
        }
      }
      if (!(window.RC && RC.result && RC.result.aiCall)) { if (opts.fallback) opts.fallback(); return; }
      RC.result.aiCall('/pdf/api/explain', { text: opts.text || '', context: opts.context || '' }, '💡 AI 解释', { kind: 'explain' });
    },
    // 翻译:走 rc-result 大框 AI 翻译(共享模式不再就地浮层;字符层几何那套留 PDF 非共享路径)。
    translate: function (opts) {
      opts = opts || {};
      if (!(window.RC && RC.result && RC.result.aiCall)) { if (opts.fallback) opts.fallback(); return; }
      PdfAdapter._ensureResultCfg();
      RC.result.aiCall('/pdf/api/translate', { text: opts.text || '', target_lang: '中文' }, '🌐 翻译', {});
    },
    // 对话:选区钉入统一助手；旧结果窗口只作共享助手未挂载时的兜底。
    chat: function (opts) {
      opts = opts || {};
      if (window.RC && RC.ui && RC.ui.openSelectionChat && RC.ui.openSelectionChat(opts.text || '', opts.context || '')) return;
      if (!(window.RC && RC.result && RC.result.openChat)) { if (opts.fallback) opts.fallback(); return; }
      PdfAdapter._ensureResultCfg();
      RC.result.openChat(opts.text || '', opts.context || '', { kind: 'chat' });
    },
    // 全词典大框:跳过核心小框,直接 rc-wordpop 完整词条(英三源 SSE / 日完整页)。
    openFullDict: function (opts) {
      opts = opts || {};
      if (!(window.RC && RC.wordpop && RC.wordpop.openFull)) { if (opts.fallback) opts.fallback(); return; }
      PdfAdapter._ensureResultCfg();   // 让完整词典框(经 rc-wordpop→rc-result.openResult)也触发 beforeOpen 快照历史
      RC.wordpop.openFull({ word: opts.word || '', ctx: opts.context || '', jp: !!opts.jp,
        file: opts.file || '', page: opts.page || 0, langs: opts.langs || [] });
    },
    // 词组小框:查询+渲染交给 rc-phrasepop;呼吸高亮层是 PDF 字符层几何 → 经 host(bind)留底座。
    // 设计(2026-07,用户拍板):查询期**不自动弹框**,只呼吸高亮 + 后台 fetch,结果存到高亮对象上;
    //   点常亮高亮才用已存结果**秒开**弹框 + 消高亮。缓存快返回(<400ms)则不呼吸、直接弹框。
    lookupPhrase: function (opts) {
      opts = opts || {};
      var h = PdfAdapter._host;
      var text = String(opts.text || (h && h.lastSelText && h.lastSelText()) || '').trim();
      if (!text) return;
      if (!(window.RC && RC.phrasepop && RC.phrasepop.show)) { if (opts.fallback) opts.fallback(); return; }
      var langs = (h && h.bookLangs && h.bookLangs()) || [];
      var file = (h && h.fileRel && h.fileRel()) || '';
      var page = (h && h.selPageNum && h.selPageNum()) ||
        (h && h.currentPage && h.currentPage()) || 0;
      // 弹框(点击/缓存快开)共用的按钮回调 —— phl 传引用,onFav 精确删它
      var _showOpts = function (rect, result, phl) {
        return {
          text: text, rect: rect, result: result || null, file: file,
          context: opts.context || '',
          page: page, langs: langs, ignoreSelector: '#sel-toolbar',
          // 收藏时无需再删高亮:点击模式弹框前已 _removePhraseHighlight(a),快返回模式根本没建高亮 → 只刷新分词。
          //   (旧 removePhraseHighlight(phl||t) 里 phl 恒 null → 按文本删会误删并存的同文本高亮。)
          onFav: function (t, nowFav) {
            if (!h) return;
            try {
              if (h.phraseFavoriteUpdate) h.phraseFavoriteUpdate(t, nowFav);
              else if (h.phraseRefresh) h.phraseRefresh();
            } catch (e) {}
          },
          onMastered: function (t, mastered) {
            if (!h) return;
            try {
              if (h.phraseMasteryUpdate) h.phraseMasteryUpdate(t, mastered);
              else if (h.phraseRefresh) h.phraseRefresh();
            } catch (e) {}
          },
          onExplain: function () { if (h && h.onExplain) { try { h.onExplain(); } catch (e) {} } }
        };
      };
      // ── 点击已有常亮高亮重弹:anchorRect(高亮屏幕矩形)+ 已存结果秒开(无则 fetch 兜底) ──
      if (opts.noHighlight) {
        var cr = h && h.charSel && h.charSel();
        RC.phrasepop.show(_showOpts(opts.anchorRect || _rectFromSel(cr), opts.result || null, opts.phl || null));
        return;
      }
      // ── 查询模式:先算选区 rect(缓存快开时用);400ms 后才物化呼吸高亮(缓存快返回则不呼吸) ──
      var cs = h && h.charSel && h.charSel();
      var selRect = _rectFromSel(cs);   // 必须在 phraseHighlight() 清 .sel-overlay 之前算
      var phl = null, resolved = false, hlTimer = null;
      hlTimer = setTimeout(function () {
        hlTimer = null;
        if (resolved) return;
        if (h && h.phraseHighlight) { try { phl = h.phraseHighlight(); } catch (e) {} }   // 慢:才建呼吸高亮
      }, 400);
      RC.phrasepop.show({
        text: text, noDisplay: true, file: file, page: page, langs: langs,
        context: opts.context || '',
        onResult: function (data) {
          resolved = true;
          if (hlTimer) { clearTimeout(hlTimer); hlTimer = null; }
          if (phl) { phl.result = data; }   // 慢:结果存到高亮 → 等点击秒开
          else { try { RC.phrasepop.show(_showOpts(selRect, data, null)); } catch (e) {} }   // 快/缓存:没建高亮 → 直接弹结果框
        },
        onSolid: function () { if (phl && h && h.phraseSolid) { try { h.phraseSolid(phl); } catch (e) {} } }
      });
    },

    // ════════════════ 阶段3:高亮编辑/列表 → rc-highlight;图描述浮层 → rc-figures ════════════════
    // 高亮**叠层渲染 / 新建 / 删除 overlay** 是 PDF 字符层归一几何(rc-highlight 无对应能力)→ 留 reader.js 底座;
    // 这里只接管 rc-highlight 真正提供的两件事:「编辑浮层」openHlEditor + 「列表」renderHighlightList。
    // 图**徽标定位**也是 PDF 归一坐标几何(rc-figures.decorate/attach 是 <img> 版,不适配 canvas 页徽标)→ 留底座;
    // 只「描述浮层 chrome」走 figurePop(RC.figures.openPop)。RC 任一不可用 → opts.fallback(门控点给的原 PDF 行为),绝不吞功能。
    openHlEditor: function (opts) {
      opts = opts || {};
      if (!(window.RC && RC.highlight && RC.highlight.openEditor)) { if (opts.fallback) opts.fallback(); return; }
      RC.highlight.openEditor({
        colors: opts.colors || [], current: opts.current || '', note: opts.note || '',
        preview: opts.preview || '', sentence: opts.sentence || '', body: opts.body || '', kind: opts.kind,
        anchorEl: opts.anchorEl || null, anchorSelector: opts.anchorSelector || '', placeBelow: !!opts.placeBelow,
        silent: !!opts.silent,   // PDF host 的 _hlUpdate 会弹「已保存」→ 传 silent 抑制本层重复 toast
        onColor: opts.onColor, onNote: opts.onNote, onDelete: opts.onDelete
      });
    },
    // 描述浮层:把 PDF 徽标 + caption + 已渲染 body(html)交给 rc-figures.openPop;ignoreSelector 让点 PDF 徽标/命中层不误关。
    figurePop: function (opts) {
      opts = opts || {};
      if (!(window.RC && RC.figures && RC.figures.openPop)) { if (opts.fallback) opts.fallback(); return; }
      RC.figures.openPop(opts.badge, opts.caption || '图', opts.body || '', { ignoreSelector: opts.ignoreSelector || '' });
    },
    // 高亮列表(为「高亮侧栏抽屉」就位;PDF 暂无该抽屉 → 无 live 调用方,数据/跳转/删除靠 bind 喂,未 bind 安全降级)
    renderHighlightList: function (container) {
      var h = PdfAdapter._host;
      if (!container || !h) return false;
      if (!(window.RC && RC.highlight && RC.highlight.renderList)) return false;
      RC.highlight.renderList(container, (h.allHighlights && h.allHighlights()) || [], {
        reverse: true,
        onJump: function (hl) { if (h.jumpToHl) h.jumpToHl(hl); },
        onDelete: function (hl) { if (h.hlDelete) h.hlDelete(hl); }
      });
      return true;
    },

    // ════════════════ 阶段4:知识点面板(右侧抽屉「知识点」tab)→ rc-knowledge ════════════════
    // 抽屉本体(#grammar-panel/#side-tabs/#side-pane-kg/#kg-nodes/把手/外观)**留 PDF 原版,本阶段不迁**;
    // 只把 loadPageNodes 的「取数 + 建卡 + 渲染 + 跟踪」收口:取数(page-nodes,页作用域)+ __lastPageNodes(语音上下文)
    // 由本方法负责,卡片渲染交给 rc-knowledge.renderInto(渲进 PDF 的 #kg-nodes,沿用 PDF 自带 .kg-node CSS → 视觉逐字一致);
    // 点开 skilltree / ☆跟踪 用 rc-knowledge 默认行为(POST /skilltree/<book>/api/toggle-tracked + loadGrammarTracked,
    // 与 PDF toggleNodeTrack 逐字一致 → 不必传 onOpenNode/onToggleTrack)。RC.knowledge 或容器缺 → opts.fallback(原 _loadPageNodesNative)。
    // 注:rc-knowledge embedded 模式写死容器 #ep-kg-nodes / 全局 __lastBookNodes,故**不**走 init/load,改用新增的 renderInto(容器/作用域/全局量全由本侧掌控,绕开三处错配)。
    renderPageNodes: function (opts) {
      opts = opts || {};
      var fb = function () { if (opts.fallback) opts.fallback(); };
      if (!(window.RC && RC.knowledge && RC.knowledge.renderInto)) { fb(); return; }
      var cont = document.getElementById(opts.container || 'kg-nodes');
      if (!cont) { fb(); return; }
      var after = function () { if (opts.onAfter) { try { opts.onAfter(); } catch (e) {} } };   // 等价 PDF loadPageNodes 尾部 _refreshVocabIfPage(成功/失败都跑)
      return fetch('/pdf/api/page-nodes?file=' + encodeURIComponent(opts.file || '') + '&page=' + (opts.page || 0))
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var list = (d && d.nodes) || [];
          window.__lastPageNodes = list;   // 语音助手 __voiceContext 读它做谐音纠错/上下文(沿用 PDF 同名全局)
          RC.knowledge.renderInto(cont, list, {
            emptyHtml: '<div style="color:#5a6680;font-size:12px">该页无 KG 节点（可能这本书没扫过/或本页不是知识点页）</div>'
          });
          after();
        })
        .catch(function () {
          cont.innerHTML = '<div style="color:#c00;font-size:12px">加载失败</div>';
          after();
        });
    },

    // ── 以下两方法阶段1 无 live 调用方(工具栏留 PDF 原版),靠 bind 喂内部量,为阶段2 就位;未 bind 安全降级 ──
    captureSelection: function () {
      var h = PdfAdapter._host; if (!h) return null;
      var cs = h.charSel && h.charSel();
      var text = (h.lastSelText && h.lastSelText()) || '';
      if (!cs || !text) return null;
      return RC.contract.selection({
        text: text,
        sentence: (h.sentence && h.sentence(cs)) || '',
        anchor: { kind: 'pdf-char', file: (h.fileRel && h.fileRel()) || '', page: (h.selPageNum && h.selPageNum()) || 0, startIdx: cs.startIdx, endIdx: cs.endIdx },
        rect: { client: _rectFromSel(cs) }
      });
    },
    clearSelection: function () { var h = PdfAdapter._host; if (h && h.clear) h.clear(); },
    // 只补统一契约的只读位置描述；页码与总页数仍由 PDF 底座计算和拥有。
    currentLocation: function () {
      var h = PdfAdapter._host;
      return {
        unit: 'page',
        index: (h && h.currentPage && h.currentPage()) || 0,
        total: (h && h.pageCount && h.pageCount()) || 0
      };
    },
    // DocumentHost 迁移入口：只把“去哪里”交给 PDF 原生导航，不解释 EPUB/Web 锚点。
    navigate: function (target) {
      var h = PdfAdapter._host, a = target && target.data ? target.data : (target || {});
      var page = a.page != null ? a.page : (a.index != null ? a.index : target);
      if (h && h.asst && h.asst.goTo && page != null) { h.asst.goTo(page); return true; }
      return false;
    },

    // ════════════════ 阶段5:助手薄增量 → rc-assistant ════════════════
    // 助手主体(SSE 流式 / rid 重连 / agentic 工具循环 / 撤销重做卡 / 历史 / mic / 上下文采集 /
    //   _ctxCard 图缩略图实时合成)全是 per-reader 强绑 PDF state/路由 → 留 25-assistant.js,不迁。
    // 只接 rc-assistant 真·无耦合的两件:
    //   · openModelSettings:⚙ 模型设置面板(自建 .ams-mask 浮层、命中同组端点 /api/assistant/action-pref[s]、
    //     同组 action 名,无 PDF DOM 耦合;PDF 内联本是 ~140 行逐字重复 → 有 drift 风险,迁来消重)。
    //   · splitFollowups:[[FOLLOWUP]] 解析(纯函数,无 DOM)。
    // **不迁** renderFollowups:PDF 的 _fadeInAfter 把错峰淡入绑死在 .asst-followups class 上,
    //   rc 版产出 .rc-fu-box → 迁了淡入动画会丢,且 PDF 版还多 scrollDown + onPick 已内联,故渲染留 native。
    // **不迁** contextCard:PDF _ctxCard 是图缩略图(__figThumb)实时合成超集,rc 版纯文本会丢图。
    // RC 任一不可用 → fallback(门控点给的原 PDF native),绝不吞功能。
    openModelSettings: function (opts) {
      opts = opts || {};
      if (!(window.RC && RC.assistant && RC.assistant.openModelSettings)) { if (opts.fallback) opts.fallback(); return; }
      RC.assistant.openModelSettings(opts.focusAction);
    },
    splitFollowups: function (text, fallback) {
      if (window.RC && RC.assistant && RC.assistant.splitFollowups) return RC.assistant.splitFollowups(text);
      return fallback ? fallback(text) : null;
    },

    // ════════════════ 阶段7:⚙ 总设置面板 → rc-settings(统一面板,跟 EPUB 同一份内容/行为) ════════════════
    // rc 面板复用**原生 id 全套**(mask 也叫 settings-mask)→ 原生模板的 #settings-mask 必须先移除,
    //   否则 getElementById 命中先出现的旧模板(document 顺序),原生回填/保存全写错地方。
    // 原生函数零改动复用:opts.fill = 21-misc-ai.js _fillSettings(原 openSettings 回填体),onSave = 原生
    //   saveSettings(写 pdf-debug/… + POST translate-config + closeSettings),onCancel =
    //   原生 closeSettings;PDF 特有块(页码对齐/插图/目录/去边/文字校准)的内联 onclick 直调原生 window.*。
    // RC 不可用 → fallback(原生模板面板,此时未被移除),绝不吞功能;?ui=legacy 不加载本文件,天然原生。
    openSettings: function (opts) {
      opts = opts || {};
      if (!(window.RC && RC.settings && RC.settings.open)) { if (opts.fallback) opts.fallback(); return; }
      var nm = document.getElementById('settings-mask');
      if (nm && !nm.hasAttribute('data-rc')) { try { nm.parentNode.removeChild(nm); } catch (e) {} }
      RC.settings.open({
        host: 'pdf',
        ids: { mask: 'settings-mask', langChecks: 'lang-checks' },
        keys: { tab: 'pdf-set-tab' },
        onFill: opts.fill || null,
        onSave: function () { window.saveSettings(); },
        onCancel: function () { try { window.closeSettings(); } catch (e) {} },
        openModelSettings: function () {
          // 原生模板此按钮的兜底是 window._toast(实际是 reader.js 模块作用域,window 上没有 → 原生也静默);
          // 共享模式下有 RC.toast 可用,兜底提示走它(window.openModelSettings 由 25-assistant.js 常规提供,极少走到)。
          try { window.openModelSettings ? window.openModelSettings() : (window.RC && RC.toast && RC.toast('助手模块未就绪，刷新重试')); } catch (e) {}
        },
        onGrammarView: function (v) { try { window.setGrammarView && window.setGrammarView(v); } catch (e) {} },
        onAddHlColor: function () { try { window.addHlColor && window.addHlColor(); } catch (e) {} },
        onResetHlColors: function () { try { window.resetHlColors && window.resetHlColors(); } catch (e) {} }
      });
    }
  };
})();
