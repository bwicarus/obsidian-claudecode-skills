// 27-rc-adapter.js — 把模块作用域内部量喂给独立的 pdf-adapter.js。
// 仅 ui=shared 加载了 pdf-adapter.js → window.PdfAdapter 存在时执行;默认无 flag 时整段跳过。
// 阶段1 的 lookupWord 由门控点直接传 opts,不依赖此 bind;此段只为 captureSelection/clearSelection(阶段2)就位。
if (window.PdfAdapter && PdfAdapter.bind) {
  // PDF 也接进统一中间层 RC._adapter(设计见 /reader-middlelayer-design.md);助手经 RC.adapter().getContext() 取上下文,
  // 与 EPUB 用完全一样的方式。仅 ui=shared(window.PdfAdapter 存在)时执行;legacy 模式整段跳过 → RC._adapter 保持空、ctx() 回退直连 __voiceContext。
  try { if (window.RC && RC.use) RC.use(PdfAdapter); } catch (e) {}
  PdfAdapter.bind({
    charSel: () => _charSel,
    lastSelText: () => lastSelText,
    selPageNum: () => (typeof _selPageNum === 'function' ? _selPageNum() : currentPage),
    fileRel: () => FILE_REL,
    bookLangs: () => BOOK_LANGS,
    sentence: (cs) => {
      try { const ch = cs.pw.__charBoxes; const r = _expandSentenceFromRange(ch, cs.startIdx, cs.endIdx); return r ? _charsRangeToText(ch, r.start, r.end).slice(0, 600) : ''; }
      catch (_) { return ''; }
    },
    clear: () => {
      try { _charSel = null; lastSelText = ''; _updateSelPreview(''); document.querySelectorAll('.sel-overlay').forEach(o => o.innerHTML = ''); toolbar.classList.remove('open'); }
      catch (_) {}
    },
    // 查词呼吸高亮(rc-wordpop opts.breathe):照抄原生 _materializeWordHl+renderWordHl 单个 hl 的渲染路径——
    // 页面坐标系 rects 渲进 pw 内的 .word-hl-layer(pw 随 #main 滚动,层天然跟滚零漂移,这正是原生不会错位的原因;
    // 共享层此前用 position:fixed 兜底,滚动手动平移必有窗口期漂移)。cap=查词时捕获的 {pw,startIdx,endIdx}
    // (对象字段不会被后续选中改写,_charSel 变化是换引用)。呼吸/常亮由共享层切 layer 的 .breathe class,
    // 正好对齐原生 CSS `.word-hl-layer.breathe .hl`(renderWordHl 的 `h.ready ? '' : ' breathe'` 同一套类名)。
    // furigana 注音(原生 renderWordHl 426-433:等待时把日读音/英音标标在词上方)一并照搬。不进原生 _wordHls
    // 数组(生命周期由共享层 _wordHls 管),原生数组在共享模式下保持空,互不冲突。
    wordHlWrap: (cap) => {
      try {
        const pw = cap && cap.pw;
        if (!pw || !pw.__charBoxes) return null;
        const rects = _charRangeToPtRects(pw.__charBoxes, cap.startIdx, cap.endIdx);
        if (!rects.length) return null;
        const canvas = pw.querySelector('canvas');
        const cssW = canvas?.clientWidth || pw.clientWidth, cssH = canvas?.clientHeight || pw.clientHeight;
        const pageWPt = pw.__pageWPt || cssW, pageHPt = pw.__pageHPt || cssH;
        if (!cssW || !cssH || !pageWPt || !pageHPt) return null;
        const sx = cssW / pageWPt, sy = cssH / pageHPt;
        const layer = document.createElement('div');
        layer.className = 'word-hl-layer';
        for (const r of rects) {
          const d = document.createElement('div'); d.className = 'hl';
          d.style.left = (r[0] * sx) + 'px'; d.style.top = (r[1] * sy) + 'px';
          d.style.width = ((r[2] - r[0]) * sx) + 'px'; d.style.height = ((r[3] - r[1]) * sy) + 'px';
          layer.appendChild(d);
        }
        const _hits = _furiHitsForRects(pw, rects);
        if (_hits.length) {
          const rl = document.createElement('div');
          rl.className = 'ruby-layer';
          for (const it of _hits) { const sp = _makeRubySpan(it, sx, sy); if (sp) rl.appendChild(sp); }
          layer.appendChild(rl);
        }
        const sel = pw.querySelector('.sel-overlay'); if (sel) sel.innerHTML = '';   // 移交持久层(蓝选区→呼吸高亮,原生 456)
        pw.appendChild(layer);
        return layer;
      } catch (_) { return null; }
    },
    // 查词小框定位:直接复用原生 _positionWordPop(absolute-in-#main,随内容滚动;fixed 视口定位不跟滚)。
    // cs = 查词时捕获的 charSel 快照(pdf-adapter 的 positionPop hook 闭包传入)。
    positionWordPop: (pop, cs) => { try { _positionWordPop(pop, cs); } catch (_) {} },
    // 便签(rc-stickynote,设计见 references/sticky-notes-design.md「规格 v4」):挂载/锚定 per-reader——
    // mount=对应 pw(页未渲染→null,由 04-render 渲染完成点 mountPending 补挂);锚点=page+归一化 x/y(PDF 不改)。
    // v4 契约:mount 返回容器内**像素** left/top(定位机制归 host,组件只应用)。PDF=x·clientWidth/y·clientHeight,
    // 行为等价旧组件自算 %;页面重渲/缩放后 04-render 两处 mountPending → ensureMounted 用新 clientWidth 重算。
    noteMount: (anchor) => {
      if (!anchor || anchor.kind !== 'pdf') return null;
      const pw = document.querySelector('.page-wrap[data-page-num="' + anchor.page + '"]');
      if (!pw || pw.dataset.loaded !== '1') return null;
      const x = Math.max(0, Math.min(1, anchor.x || 0)), y = Math.max(0, Math.min(1, anchor.y || 0));
      return { el: pw, left: x * pw.clientWidth, top: y * pw.clientHeight };
    },
    noteWordRect: (x, y) => {
      // #51 粒度=单词(用户设计):最近字符→同 w 词聚合精确框;dist 给调用方判"超范围→横线"
      const t = document.elementFromPoint(x, y);
      const pw = t && t.closest ? t.closest('.page-wrap') : null;
      const cbs = pw && pw.__charBoxes;
      if (!cbs || !cbs.length) return null;
      const r = pw.getBoundingClientRect();
      // #51 词框错位根因:charBoxes 坐标=建层时页尺寸;页重渲(缩放/适应)后尺寸变了坐标没跟 →
      // 探测选错字+词框画错位(松手锚定用比例不受影响=「拖动显示与松手不一致」)。按 k 实时换算。
      const k = (pw.__charsBaseW && pw.clientWidth) ? (pw.clientWidth / pw.__charsBaseW) : 1;
      const px = x - r.left, py = y - r.top;
      let best = null, bd = 1e18, bestRow = null, brd = 1e18;
      for (const cb0 of cbs) {
        if (cb0.sp || !cb0.width) continue;
        const cb = { left: cb0.left * k, top: cb0.top * k, width: cb0.width * k, height: cb0.height * k, w: cb0.w, sp: cb0.sp };
        const cx = cb.left + cb.width / 2, cy = cb.top + cb.height / 2;
        // 行优先(用户拍板 2026-07-21:锁**左上角左侧同行**文字,非斜上方——锚语义"插在这段文字之后"):
        // 字符行高带内(±0.75字高)且在探测点左侧 → 按水平距离取最近;同行没有才退全局欧氏最近
        const rowOk = Math.abs(cy - py) <= Math.max(cb.height, 14) * 0.75;
        if (rowOk && cx <= px) {
          const dh = px - cx;
          if (dh < brd) { brd = dh; bestRow = cb; }
        }
        const d = (cx - px) * (cx - px) + (cy - py) * (cy - py);
        if (d < bd) { bd = d; best = cb; }
      }
      if (bestRow) { best = bestRow; bd = brd * brd; }
      if (!best) return null;
      let L = best.left, T = best.top, R2 = best.left + best.width, B = best.top + best.height;
      if (best.w !== -1) for (const cb0 of cbs) {
        if (cb0.w !== best.w || cb0.sp) continue;
        L = Math.min(L, cb0.left * k); T = Math.min(T, cb0.top * k);
        R2 = Math.max(R2, (cb0.left + cb0.width) * k); B = Math.max(B, (cb0.top + cb0.height) * k);
      }
      return { el: pw, left: L, top: T, width: R2 - L, height: B - T, dist: Math.sqrt(bd) };
    },
    noteAnchorFromPoint: (x, y) => {
      const t = document.elementFromPoint(x, y);
      let pw = t && t.closest ? t.closest('.page-wrap') : null;
      if (!pw || pw.dataset.loaded !== '1') {
        // 任何位置都能钉(用户拍板 2026-07-20):点不在页上(页缝/灰区/被浮层元素挡)→ 找**最近的已渲染页**,
        // 坐标 clamp 进页——钉页缝=贴上一页底部、水平位置保持(="内容排到上方最后的内容之后")
        pw = null; let best = 1e18;
        document.querySelectorAll('.page-wrap[data-loaded="1"]').forEach((p2) => {
          const r2 = p2.getBoundingClientRect();
          if (!r2.width || !r2.height) return;
          const dx = x < r2.left ? r2.left - x : (x > r2.right ? x - r2.right : 0);
          const dy = y < r2.top ? r2.top - y : (y > r2.bottom ? y - r2.bottom : 0);
          const d2 = dx * dx + dy * dy;
          if (d2 < best) { best = d2; pw = p2; }
        });
        if (!pw) return null;
      }
      const r = pw.getBoundingClientRect();
      if (!r.width || !r.height) return null;
      const _cl = !(t && t.closest && t.closest('.page-wrap') === pw);   // 走了最近页 fallback=clamped(反馈层画插入横线)
      const a0 = { kind: 'pdf', page: parseInt(pw.dataset.pageNum || '0', 10) || 0,
               x: Math.max(0, Math.min(1, (x - r.left) / r.width)),
               y: Math.max(0, Math.min(1, (y - r.top) / r.height)) };
      if (_cl) a0.clamped = 1;
      return a0;
    },
    // 阶段2 词组(rc-phrasepop):呼吸高亮层是 PDF 字符层几何 → 留底座,adapter 只接管查询+小框渲染。
    phraseHighlight: () => { try { return _showPhraseHighlight(_charSel && _charSel.pw); } catch (_) { return null; } },   // 返回本高亮 → onSolid 精确标它(并发多查询各标各的)
    phraseSolid: (hl) => { try { if (!hl) return; hl.solid = true; const _l = document.querySelector('.phrase-hl-layer[data-phid="' + hl.id + '"]'); if (_l) { _l.classList.remove('breathe'); _l.querySelectorAll('.hl').forEach(el => el.style.pointerEvents = 'auto'); } } catch (_) {} },   // 不回退标"最后一个"(并发查询会误标最新那个)
    phraseRefresh: () => { try { refreshCharsWForAllPages(); } catch (_) {} },
    removePhraseHighlight: (arg) => { try { _removePhraseHighlight(arg); } catch (_) {} },
    // 词组框「💡 解释」→ 复用 PDF onExplain(它自身也被门控,共享模式下转 rc-result)。
    onExplain: () => { try { window.onExplain && window.onExplain(); } catch (_) {} },
    // 阶段2(补丁):「解释」的琥珀色呼吸高亮 + 后台跑 + 点高亮才开面板 —— 100% 原样复用 PDF 原生
    //   _showExplainHighlight/_runExplainBg(15-phrase-wordpop.js / 21-misc-ai.js,函数体一字未改;
    //   两者内部已按 __uiShared 分流到 rc-result 的 openResult/addResultPickers/_resultReqId)。
    //   这层只是把 PdfAdapter.explain 的调用点接过去,等价 native onExplain 共享分支之前本该执行的那两行。
    explainHighlight: (text) => {
      try {
        const ehl = _showExplainHighlight(_charSel && _charSel.pw, text);
        if (ehl) { ehl.title = '💡 AI 解释'; ehl.src = text; ehl.resultContext = _resultContext; }
        return ehl;
      } catch (_) { return null; }
    },
    runExplainBg: (hl, text, context) => { try { _runExplainBg(hl, text, context); } catch (_) {} },
    // 阶段3 高亮列表(PdfAdapter.renderHighlightList)就位:数据/跳转/删除经底座(高亮叠层渲染本身留 PDF 字符层几何,不迁;
    //   PDF 暂无「高亮抽屉」UI → 这几个钩子目前无 live 调用方,先就位)。编辑/图描述浮层走 per-call opts,不依赖此 bind。
    allHighlights: () => (typeof _allHighlights !== 'undefined' ? _allHighlights : []),
    jumpToHl: (hl) => { try { if (hl && hl.page && window.goToPage) window.goToPage(hl.page); } catch (_) {} },
    hlDelete: (hl) => { try { var pw = document.querySelector('.page-wrap[data-page-num="' + (hl && hl.page) + '"]'); if (typeof _hlDelete === 'function') _hlDelete(hl, pw); } catch (_) {} },
    // ── 助手侧栏共享化(②a):把 25-assistant.js 依赖的**全部宿主符号**收进 asst host 袋,供未来搬进
    //    rc-assistant.js 的共享侧栏经 RC.adapter()._host.asst 取用(EPUB 提供同名袋 → 复用整份侧栏)。
    //    全是**纯转发**到 PDF 现有 window.* / reader.js 作用域符号(本文件同在 reader.js IIFE)→ PDF 行为零变化;
    //    typeof 守卫保证任一符号缺失也不炸。live 值(fileRel/pdfNumPages/noteAttached/activePhraseHl/focusSel)用 getter。
    asst: {
      md: (t) => { try { return md(t); } catch (_) { return (t == null ? '' : String(t)); } },
      toast: (m) => { try { if (typeof _toast === 'function') _toast(m); } catch (_) {} },
      fmtTime: (ms) => { try { return _qhFmtTime(ms); } catch (_) { return ''; } },
      fileRel: () => (typeof FILE_REL !== 'undefined' ? FILE_REL : ''),
      pdfNumPages: () => { try { return pdfDoc ? pdfDoc.numPages : 0; } catch (_) { return 0; } },
      goTo: (p) => { try { window.jumpWithBack && window.jumpWithBack(p); } catch (_) {} },
      goToInBook: (fr, p) => { try { if (window.openBookAt) window.openBookAt(fr, p); else window.location.href = '/pdf/view?file=' + encodeURIComponent(fr) + '&page=' + p; } catch (_) {} },
      dispPage: (p) => { try { return window._dispPage ? window._dispPage(p) : p; } catch (_) { return p; } },
      pdfFromDisp: (d) => { try { return window._pdfFromDisp ? window._pdfFromDisp(d) : d; } catch (_) { return d; } },
      locCount: () => { try { return pdfDoc ? pdfDoc.numPages : 0; } catch (_) { return 0; } },
      changePage: (d) => { try { window.changePage && window.changePage(d); } catch (_) {} },
      fitWidth: () => { try { window.fitWidth && window.fitWidth(); } catch (_) {} },
      zoomBy: (d) => { try { window.zoomChange && window.zoomChange(d); } catch (_) {} },
      toggleTranslate: () => { try { window.togglePageTranslate && window.togglePageTranslate(); } catch (_) {} },
      openDrawer: () => { try { if (typeof openGrammarPanel === 'function') openGrammarPanel(); } catch (_) {} },
      switchTab: (n) => { try { window.switchSideTab && window.switchSideTab(n); } catch (_) {} },
      asstOpen: () => { try { return !!(window.__asstOpen && window.__asstOpen()); } catch (_) { return false; } },
      voiceContext: () => { try { return window.__voiceContext ? window.__voiceContext() : null; } catch (_) { return null; } },
      setFocusSel: (t, k) => { try { window.__setFocusSel && window.__setFocusSel(t, k); } catch (_) {} },
      focusSel: () => { try { return window.__focusSel || null; } catch (_) { return null; } },
      clearFigFocus: () => { try { window.__clearFigFocus && window.__clearFigFocus(); } catch (_) {} },
      figThumb: (d, img, live) => { try { window.__figThumb && window.__figThumb(d, img, live); } catch (_) {} },
      noteAttached: () => { try { return window.__noteAttached || []; } catch (_) { return []; } },
      clearNoteAttached: () => { try { window.__clearNoteAttached && window.__clearNoteAttached(); } catch (_) {} },
      renderNoteChips: () => { try { window.__renderNoteChips && window.__renderNoteChips(); } catch (_) {} },
      notesReload: () => { try { window.notesReload && window.notesReload(); } catch (_) {} },
      noteInject: (n) => { try { return !!(window.__noteInject && window.__noteInject(n)); } catch (_) { return false; } },
      reloadHighlights: () => { try { window._reloadHighlights && window._reloadHighlights(); } catch (_) {} },
      loadAllHighlights: () => { try { if (typeof loadAllHighlights === 'function') loadAllHighlights(); } catch (_) {} },
      renderHighlightsOnPage: (pw, n) => { try { if (typeof renderHighlightsOnPage === 'function') renderHighlightsOnPage(pw, n); } catch (_) {} },
      showHlPicker: (d) => { try { window._showHlPicker && window._showHlPicker(d); } catch (_) {} },
      assistEdit: (d) => { try { window._assistEdit && window._assistEdit(d); } catch (_) {} },
      renderPhraseHl: (w) => { try { if (typeof renderPhraseHl === 'function') renderPhraseHl(w); } catch (_) {} },
      removePhraseHighlight: (arg) => { try { if (typeof _removePhraseHighlight === 'function') _removePhraseHighlight(arg); } catch (_) {} },
      activePhraseHl: () => { try { return _phraseHls.length ? _phraseHls[_phraseHls.length - 1] : null; } catch (_) { return null; } },   // 多高亮:返回最近一个
      setActivePhraseHl: (v) => { try { if (v) { if (v.id == null) v.id = ++_phraseHlSeq; _phraseHls.push(v); } } catch (_) {} },   // ②b:侧栏 _flashSelOnPage 追加一个高亮(不再覆盖单例)
      charsRangeToText: (ch, a, b) => { try { return _charsRangeToText(ch, a, b); } catch (_) { return ''; } },
      charRangeToPtRects: (ch, a, b) => { try { return _charRangeToPtRects(ch, a, b); } catch (_) { return []; } },
      flashSelOnPage: (p, t) => { try { if (typeof _flashSelOnPage === 'function') _flashSelOnPage(p, t); } catch (_) {} },
      noteNearText: (a) => { try { return typeof _noteNearText === 'function' ? _noteNearText(a) : ''; } catch (_) { return ''; } },
      jumpToCtx: (m) => { try { if (typeof _jumpToCtx === 'function') _jumpToCtx(m); } catch (_) {} },
      prewarm: (off) => { try { window.__asstPrewarm && window.__asstPrewarm(off); } catch (_) {} },
      getPaidNoted: () => { try { return !!window.__paidNoted; } catch (_) { return false; } },
      setPaidNoted: (v) => { try { window.__paidNoted = v; } catch (_) {} },
      // ③-2:注解 CRUD 端点(reader 专属;EPUB 提供自己的)。body 语义(PDF rects vs EPUB cfi)差异由各 reader 后端/guard 处理;
      //   DELETE(id+file)/note-composite(file+id)两侧一致,POST create 的 rects 仅 PDF(EPUB items 无 rects→侧栏 guard 跳过)。
      hlUrl: () => '/pdf/api/highlights',
      notesUrl: () => '/pdf/api/notes',
      noteCompositeUrl: () => '/pdf/api/note-composite',
      // ③-4b:chat/history/clear 端点(PDF 默认=原字面量;EPUB host 覆盖成 epub-* 端点)
      chatUrl: () => '/api/assistant/chat',
      historyUrl: () => '/api/assistant/history',
      clearUrl: () => '/api/assistant/clear',
      // ③-3:挂载点容器(侧栏往里建 tab/pane/DOM)。PDF=右侧抽屉 #grammar-panel + tab 栏 #side-tabs;
      //   EPUB 提供 #ep-side + 其 tab 栏。抽屉可见性类(.side-pane/.side-tab)仍 PDF 专属,③-4 再抽 EPUB 集成。
      mountPanel: () => document.getElementById('ep-side') || document.getElementById('grammar-panel'),   // 唯一抽屉(drawer=shared,28-shared-drawer 把 #grammar-panel 改名 #ep-side)优先
      mountTabs: () => document.getElementById('ep-side-tabs') || document.getElementById('side-tabs')
    }
  });
  // 便签初始化(共享组件;opts 经上面 host-bind 的 noteMount/noteAnchorFromPoint;🗒 按钮在模板 ui_shared 块内)
  try {
    if (window.RC && RC.stickynote) {
      RC.stickynote.init({
        file: FILE_REL,
        mount: (a) => PdfAdapter._host.noteMount(a),
        anchorFromPoint: (x, y) => PdfAdapter._host.noteAnchorFromPoint(x, y),
        noteWordRect: (x, y) => { try { return PdfAdapter._host.noteWordRect(x, y); } catch (_) { return null; } },   // #51 词粒度反馈
        // 阶段3 AI 注入:双击便签 → 25-assistant __noteInject(助手开着才处理:无笔画走文本通道,有笔画走合成图/视觉通道)
        onDoubleTap: (note) => { try { return !!(window.__noteInject && window.__noteInject(note)); } catch (_) { return false; } },
        toast: (m) => { try { _toast?.(m); } catch (_) {} }
      });
      window._noteCreateAtCenter = () => { try { RC.stickynote.createAtCenter(); } catch (_) {} };
    }
  } catch (_) {}
  // ②b 收尾:shared 模式(本块只在 ui=shared 下执行)bind 完(HOST 就绪)后**无条件**挂 rc-assistant 的共享侧栏;
  //     老 25-assistant 已在 __uiShared 下自退,不会双挂。legacy(?ui=legacy)本块不执行 → 走老 25-assistant 兜底。
  try {
    if (window.RC && RC.assistant && RC.assistant.mountPdfSidebar) RC.assistant.mountPdfSidebar();
  } catch (_) {}
}
