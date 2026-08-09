// ──────── char-layer：PyMuPDF char-level bbox 驱动的精确选中 ────────

let _charSel = null;   // {pw, startIdx, endIdx, dragging}

let _nativePageTextLoaderDoc = null;
function _embeddedWordSegments(text) {
  const locale = (typeof BOOK_LANGS !== 'undefined' && BOOK_LANGS && BOOK_LANGS[0]) || undefined;
  try {
    if (typeof Intl !== 'undefined' && typeof Intl.Segmenter === 'function') {
      const segmenter = new Intl.Segmenter(locale, { granularity: 'word' });
      let word = 0;
      return Array.from(segmenter.segment(text)).map((part) => ({
        start: part.index,
        end: part.index + part.segment.length,
        w: part.isWordLike ? word++ : -1,
      }));
    }
  } catch (_) {}
  // No fake whitespace grouping: without a real tokenizer the conservative
  // fallback leaves word identity unknown and preserves character selection.
  return [];
}
function _embeddedPageText(textContent, viewport, page) {
  const chars = [];
  let wordBase = 0;
  const items = (textContent && textContent.items || []).filter((item) => item && item.str !== undefined);
  items.forEach((item, blockIndex) => {
    const text = String(item.str || '');
    const units = Array.from(text);
    if (!units.length || !item.transform) return;
    const segments = _embeddedWordSegments(text);
    const [a, b, c, d, e, f] = item.transform;
    const [vx, vy] = viewport.convertToViewportPoint(e, f);
    const vertical = item.dir === 'ttb';
    const itemWidth = Math.max(0.5, Number(item.width) || Math.abs(a) || 1);
    const itemHeight = Math.max(0.5, Number(item.height) || Math.abs(d) || 1);
    let codeUnitOffset = 0;
    units.forEach((unit, unitIndex) => {
      const startOffset = codeUnitOffset;
      codeUnitOffset += unit.length;
      const segment = segments.find((candidate) =>
        startOffset >= candidate.start && startOffset < candidate.end);
      const w = segment && segment.w >= 0 ? wordBase + segment.w : -1;
      const fraction0 = unitIndex / units.length;
      const fraction1 = (unitIndex + 1) / units.length;
      let x0, y0, x1, y1;
      if (vertical) {
        x0 = vx - itemWidth;
        x1 = vx;
        y0 = Math.max(0, vy - itemHeight + itemHeight * fraction0);
        y1 = Math.max(y0 + 0.5, vy - itemHeight + itemHeight * fraction1);
      } else {
        x0 = Math.max(0, vx + itemWidth * fraction0);
        x1 = Math.max(x0 + 0.5, vx + itemWidth * fraction1);
        y0 = Math.max(0, vy - itemHeight);
        y1 = Math.max(y0 + 0.5, vy);
      }
      chars.push({
        c: unit,
        x0, y0, x1, y1,
        w,
        bk: blockIndex,
        sp: /^\s$/u.test(unit),
      });
    });
    const wordCount = segments.reduce((max, segment) => Math.max(max, segment.w + 1), 0);
    wordBase += wordCount;
  });
  return {
    chars,
    pageWidth: viewport.width,
    pageHeight: viewport.height,
    revision: 'embedded-v1-' + CHARS_VER + '-' + page + '-' + items.length,
  };
}
function _nativePageTextProvider() {
  try {
    const provider = window.BWReaderRuntime && window.BWReaderRuntime.pageTextProvider;
    return provider && provider.contract === 'reader-page-text-provider/1' ? provider : null;
  } catch (_) { return null; }
}
function _bindNativeEmbeddedPageLoader() {
  const provider = _nativePageTextProvider();
  if (!provider || !pdfDoc || _nativePageTextLoaderDoc === pdfDoc) return provider;
  provider.setEmbeddedPageLoader(async (pageNumber) => {
    const page = await pdfDoc.getPage(pageNumber);
    const content = await page.getTextContent();
    return _embeddedPageText(content, page.getViewport({ scale: 1 }), pageNumber);
  }, pdfDoc.numPages);
  _nativePageTextLoaderDoc = pdfDoc;
  return provider;
}

if (!window.__bwPageTextRefreshBound) {
  window.__bwPageTextRefreshBound = true;
  window.addEventListener('bw:page-text-updated', (event) => {
    const detail = event && event.detail;
    if (!detail || detail.contract !== 'reader-page-text-provider/1') return;
    const page = Number(detail.page);
    if (!Number.isInteger(page) || page < 1) return;
    document.querySelectorAll('.page-wrap[data-page-num="' + page + '"]').forEach((wrap) => {
      const viewport = wrap.__pageTextViewport;
      if (!viewport || !wrap.isConnected) return;
      loadCharsAndBindLayer(page, wrap, viewport, 0).catch((error) =>
        window.dlog?.('chars refresh fail: ' + (error && error.message), '#ff6b6b'));
    });
  });
}

function _selectionUsesBlockFilter(source, revision, characterGeometry) {
  const src = String(source || '').toLowerCase();
  const rev = String(revision || '').toLowerCase();
  const geometry = String(characterGeometry || '').toLowerCase();
  // Native embedded text and ordinary Pi OCR already have a continuous
  // reading order. Applying the manga block graph to them punches holes in a
  // drag range whenever PDF items/words happen to carry different bk values.
  if (src === 'embedded' || rev.startsWith('embedded-')
      || rev.includes('pdfkit-embedded-text/')) return false;
  if (src === 'pi') {
    if (rev.startsWith('pi-manga/')) return true;
    if (rev.startsWith('pi-vision/')) return false;
    // Compatibility with app builds whose revision was an opaque digest.
    if (geometry === 'exact') return false;
    if (geometry === 'estimated') return true;
  }
  // Apple Vision and legacy web/PWA responses have no explicit layout mode.
  // Preserve their existing bubble/column isolation until they do.
  return true;
}

// chars → charBoxes(坐标映射 + reading-order 排序)。初次建层 + cv 校正重取 都用它。
// PyMuPDF rawdict bbox 已是 image coordinate(y 向下,原点左上),不做 y 翻转(翻了会上下颠倒)。
function _mapCharBoxes(chars, scale, source, revision, characterGeometry) {
  const useBlockFilter = _selectionUsesBlockFilter(source, revision, characterGeometry);
  const cb = chars.map((ch, _oi) => ({
    c: ch.c, _oi,
    w: (ch.w == null ? -1 : ch.w),
    bk: (ch.bk == null ? -1 : ch.bk),
    _selectionBlockFilter: useBlockFilter,
    left: ch.x0 * scale, top: ch.y0 * scale,
    width: (ch.x1 - ch.x0) * scale, height: (ch.y1 - ch.y0) * scale,
    sp: !!ch.sp,
    fml: !!ch.fml, flx: ch.flx || '',   // 公式注入字符:fml 标记 + 首字符带原始 latex(flx)
    _x0: ch.x0, _y0: ch.y0, _x1: ch.x1, _y1: ch.y1,
  }));
  cb.sort((a, b) => {
    const aBase = a.top + a.height, bBase = b.top + b.height;
    const ref = Math.max(a.height, b.height) || 1;
    if (Math.abs(aBase - bBase) > ref * 0.8) return aBase - bBase;
    if (a.w !== -1 && a.w === b.w) return a._oi - b._oi;   // 同词内严格 reading order(连字/紧排根治)
    if (Math.abs(a.left - b.left) < ref * 0.3) return a._oi - b._oi;
    return a.left - b.left;
  });
  return cb;
}

async function loadCharsAndBindLayer(num, wrap, viewport, _retry) {
  _retry = _retry || 0;
  if (!wrap.isConnected) return;   // 页已被连续模式释放 → 放弃
  const loadSeq = wrap.__pageTextLoadSeq = (wrap.__pageTextLoadSeq || 0) + 1;
  wrap.__pageTextViewport = viewport;
  const scale = viewport.scale;
  const cvKey = 'pdf-cv:' + FILE_REL + ':' + num;
  let cvGuess; try { cvGuess = localStorage.getItem(cvKey) || ('v' + CHARS_VER); } catch (_) { cvGuess = 'v' + CHARS_VER; }
  const charsUrl = (cv) => `/pdf/api/page-chars?file=${encodeURIComponent(FILE_REL)}&page=${num}&v=${CHARS_VER}&cv=${encodeURIComponent(cv)}`;
  // overlay(生词/句子/真 cv)**并行**拉,不阻塞选词层;chars 用上次 cv 猜测 → 命中 SW 缓存秒回 → 选词立即可用
  const ovP = fetch(`/pdf/api/page-overlay?file=${encodeURIComponent(FILE_REL)}&page=${num}`).then(r => r.json()).catch(() => null);
  let d = null;
  try { d = await (await fetch(charsUrl(cvGuess))).json(); } catch (e) { d = null; }
  if (!wrap.isConnected || wrap.__pageTextLoadSeq !== loadSeq) return;
  if (!d || !d.ok) {
    wrap.__pageTextState = (d && d.state) || 'failed';
    wrap.__pageTextSource = (d && d.source) || null;
    wrap.__wordSegmentation = (d && d.word_segmentation) || 'unavailable';
    wrap.__characterGeometry = (d && d.character_geometry) || 'unavailable';
    wrap.__formulaCoverage = (d && d.formula_coverage) || 'unknown';
    wrap.__formulaRegions = d && Array.isArray(d.formula_regions) ? d.formula_regions : [];
    if (d && d.state === 'pending') {
      window.dlog?.('chars pending: page ' + num);
      return;
    }
    if (d && (d.state === 'failed' || d.state === 'idle')) {
      window.dlog?.('chars unavailable: ' + (d.error || d.state) + ' on page ' + num, '#ffb454');
      return;
    }
    if (_retry < 2 && wrap.isConnected) {
      await new Promise(res => setTimeout(res, 500 + _retry * 600));
      return loadCharsAndBindLayer(num, wrap, viewport, _retry + 1);
    }
    window.dlog?.('chars api fail: ' + ((d && d.error) || 'fetch') + ' (retry ' + _retry + ')', '#ff6b6b');
    return;
  }
  const charBoxes = _mapCharBoxes(
    d.chars, scale, d.source, d.revision, d.character_geometry);
  wrap.__pageTextState = d.state || (charBoxes.length ? 'ready' : 'readyEmpty');
  wrap.__pageTextSource = d.source || null;
  wrap.__pageTextRevision = d.revision || '';
  wrap.__wordSegmentation = d.word_segmentation || 'unavailable';
  wrap.__characterGeometry = d.character_geometry || 'unavailable';
  wrap.__formulaCoverage = d.formula_coverage || 'unknown';
  wrap.__formulaRegions = Array.isArray(d.formula_regions) ? d.formula_regions : [];
  wrap.__pageWPt = d.page_w;
  wrap.__pageHPt = d.page_h;
  wrap.__viewportScale = scale;
  // 可视化文字框(URL ?dbg=1 或 设置开关)
  let _cbOn = new URLSearchParams(location.search).get('dbg') === '1';
  try { _cbOn = _cbOn || localStorage.getItem('pdf-charbox') === '1'; } catch (_) {}
  if (_cbOn) {
    const dbgLayer = ensurePageLayer(wrap, 'char-dbg-layer');
    dbgLayer.innerHTML = '';
    dbgLayer.style.cssText = 'position:absolute;inset:0;pointer-events:none;z-index:5';
    charBoxes.forEach((c) => {
      const e = document.createElement('div');
      e.style.cssText = `position:absolute;left:${c.left}px;top:${c.top}px;width:${c.width}px;height:${c.height}px;border:1px solid rgba(255,0,0,.4);font-size:9px;color:rgba(255,0,0,.8);line-height:${c.height}px;text-align:center;font-family:monospace`;
      e.textContent = c.sp ? '·' : c.c;
      dbgLayer.appendChild(e);
    });
    wrap.appendChild(dbgLayer);
  }
  wrap.__charBoxes = charBoxes;
  wrap.__charsBaseW = wrap.classList.contains('crop-on') ? (parseFloat(wrap.style.getPropertyValue('--full-w')) || wrap.clientWidth || 0) : (wrap.clientWidth || 0);   // #51:建层整页布局宽基准(去边=整页 --full-w,charBox 是整页坐标;非去边=clientWidth);重渲后按 char-layer 实时 BCR/baseW 换算
  try { window.__applyPhraseMergesLocal && window.__applyPhraseMergesLocal(wrap); } catch (_) {}   // 本地词组合并(收藏集驱动,教义:本地算)
  window.dlog?.('chars: ' + charBoxes.length + ' on page ' + num);
  // 创建 char-layer（透明覆盖整个 page-wrap）→ 绑定后**选词此刻即可用**(不等 overlay)
  const cl = ensurePageLayer(wrap, 'char-layer');
  wrap.__charLayer = cl;
  _bindCharLayer(cl, wrap);
  try { renderHighlightsOnPage(wrap, num); } catch(e) { window.dlog?.('hl render fail: '+e.message,'#ff6b6b'); }
  // 振假名/音标（chars 那条带的 furigana）
  wrap.__furigana = d.furigana || [];
  wrap.__furiVerified = false;
  try { renderRubyLayer(wrap); } catch(e) { window.dlog?.('ruby fail: '+e.message,'#ff6b6b'); }
  if (_rubyEnabled()) _verifyFurigana(wrap);
  try { renderPhraseHl(wrap); } catch(_) {}
  try { renderExplainHl(wrap); } catch(_) {}
  try { renderWordHl(wrap); } catch(_) {}
  try { window.renderFiguresOnPage && renderFiguresOnPage(wrap, num); } catch(_) {}   // 页级图注 Apple 徽标
  if (_pageTrOn) { wrap.__pageTrSeq = null; _pageTranslatePage(wrap); }
  if (window._pendingSearchHighlight && window._pendingSearchHighlight.page === num
      && wrap.__charBoxes && wrap.__charBoxes.length) {
    try { _highlightSearchResultsOnPage(wrap, window._pendingSearchHighlight.query); } catch (_) {}
    window._pendingSearchHighlight = null;
  }
  // —— overlay 到了(慢:服务端跑分词/生词,不阻塞上面的选词)：渲染生词/句子 + 校正 cv ——
  ovP.then(async (ov) => {
    if (!wrap.isConnected || wrap.__pageTextLoadSeq !== loadSeq) return;
    if (ov && Array.isArray(ov.formula_regions)) wrap.__formulaRegions = ov.formula_regions;
    if (ov && ov.ok && ov.cv) {
      try { localStorage.setItem(cvKey, ov.cv); } catch (_) {}   // 记真 cv,下次秒命中缓存
      if (ov.cv !== cvGuess) {
        // cvGuess 猜错(内容自上次起变过)→ 刚 chars 可能来自旧 SW 缓存 → 用真 cv 重取并刷新选词数据
        try {
          const d2 = await (await fetch(charsUrl(ov.cv))).json();
          if (d2 && d2.ok && wrap.isConnected && wrap.__pageTextLoadSeq === loadSeq) {
            wrap.__charBoxes = _mapCharBoxes(
              d2.chars, viewport.scale, d2.source, d2.revision, d2.character_geometry);
            wrap.__charsBaseW = wrap.classList.contains('crop-on') ? (parseFloat(wrap.style.getPropertyValue('--full-w')) || wrap.clientWidth || 0) : (wrap.clientWidth || 0);   // #51:cv 校正重建同步基准宽(整页布局宽)
            wrap.__pageWPt = d2.page_w; wrap.__pageHPt = d2.page_h;
            wrap.__furigana = d2.furigana || [];
            try { renderRubyLayer(wrap); } catch (_) {}
          }
        } catch (_) {}
      }
    }
    wrap.__vocabMarks = (ov && ov.vocab_marks) || [];
    wrap.__vocabSentences = (ov && ov.vocab_sentences) || [];
    wrap.__masteredFuri = new Set((ov && ov.mastered_furi) || []);   // 已掌握词面 → ruby 跳过其注音
    try { renderVocabUnderlines(wrap, wrap.__vocabMarks); } catch(e) { window.dlog?.('vocab underline fail: '+e.message,'#ff6b6b'); }
    try { renderRubyLayer(wrap); } catch (_) {}   // overlay 到了(含已掌握词集)→ 重画 ruby(首渲时还没这个集,此刻把已掌握词的注音去掉)
    try { renderVocabSentences(wrap, wrap.__vocabSentences); } catch(e) { window.dlog?.('vocab sentence fail: '+e.message,'#ff6b6b'); }
  });
}

// 查 char idx 是否在某 vocab mark 范围（rects PDF pt 坐标）内；返回 mark 或 null
function _findVocabMarkAt(pw, charIdx) {
  if (!pw || !pw.__vocabMarks || !pw.__charBoxes) return null;
  const c = pw.__charBoxes[charIdx];
  if (!c || c._x0 === undefined) return null;
  const cx = (c._x0 + c._x1) / 2;
  const cy = (c._y0 + c._y1) / 2;
  for (const m of pw.__vocabMarks) {
    for (const r of (m.rects || [])) {
      if (cx >= r[0] && cx <= r[2] && cy >= r[1] && cy <= r[3]) return m;
    }
  }
  return null;
}
function _clickTranslateEnabled() {
  const v = localStorage.getItem('pdf-click-translate-unmastered');
  return v === null ? true : (v === '1');
}

// 共享 vocabulary-state 只参与可见投影；业务写入仍由 rc-wordpop/phrasepop 负责。
// 仓库缺失、尚未 hydrate 或没有该词记录时返回 false，后面的本地镜像/服务端 label
// 继续作为兼容 fallback。
function _vocabularyStateRepo() {
  try {
    const repo = window.BWReaderRuntime && window.BWReaderRuntime.vocabularyState;
    return repo &&
      repo.CONTRACT === 'vocabulary-state/1' &&
      typeof repo.isMastered === 'function'
      ? repo
      : null;
  } catch (_) { return null; }
}
function _vocabularyStateMarkMastered(mark) {
  const repo = _vocabularyStateRepo();
  if (!repo || !mark) return false;
  const word = String(mark.word || mark.surface || '').trim();
  const lemma = String(mark.lemma || word).trim();
  if (!lemma && !word) return false;
  try {
    return repo.isMastered({
      kind: 'word',
      language: mark.language || mark.lang || (mark.jp ? 'ja' : 'en'),
      lemma: lemma || word,
      word: word || lemma,
      surface: word || lemma,
      forms: Array.isArray(mark.forms) ? mark.forms : []
    });
  } catch (_) { return false; }
}

function renderVocabUnderlines(pw, marks) {
  if (!_vocabUnderlineEnabled()) return;
  // §18.5 local-first:服务端回**全候选**(含已掌握,label_slug='mastered'),渲染时本地过滤。
  // 共享仓库优先补充已掌握事实；旧 __masteredLocal/__vocabOverride 与服务端 label
  // 继续兜底，且 dirty 标记只能在真正收到服务器 mastery snapshot 时收敛。
  try {
    const _ovr = window.__vocabOverride;
    marks = (marks || []).filter((m) => {
      const k = String(m.lemma || m.word || '').toLowerCase();
      if (_vocabularyStateMarkMastered(m)) return false;
      if (_ovr && _ovr.has(k)) return !_ovr.get(k);
      if (window.__masteredLocal) return !window.__masteredLocal.has(k);
      return m.label_slug !== 'mastered';
    });
  } catch (_) {}
  // 确保有 layer（即使 marks 空也要清旧残留）
  let layer = pw.querySelector('.vocab-layer');
  if (!layer && marks && marks.length) {
    layer = ensurePageLayer(pw, 'vocab-layer');
  }
  if (!layer) return;
  layer.innerHTML = '';
  if (!marks || !marks.length) return;
  const canvas = pw.querySelector('canvas');
  const cssW = canvas?.clientWidth || pw.clientWidth;
  const cssH = canvas?.clientHeight || pw.clientHeight;
  const pageWPt = pw.__pageWPt || cssW;
  const pageHPt = pw.__pageHPt || cssH;
  if (!cssW || !cssH || !pageWPt || !pageHPt) return;
  const sx = cssW / pageWPt;
  const sy = cssH / pageHPt;
  for (const m of marks) {
    for (const r of (m.rects || [])) {
      const x0 = r[0], y0 = r[1], x1 = r[2], y1 = r[3];
      const div = document.createElement('div');
      div.className = 'vocab-underline m-' + m.label_slug;
      div.style.left = (x0 * sx) + 'px';
      div.style.top = (y1 * sy + 1) + 'px';   // 字底 + 1px
      div.style.width = ((x1 - x0) * sx) + 'px';
      layer.appendChild(div);
    }
  }
  // 翻到本页 → 后台空闲预热这些生词的释义进查词缓存(点开秒显;prewarm=1 纯读,不 bump 暴露/不建笔记,不污染掌握度)。
  //   切书=整页重载自动清缓存;600 上限自然淘汰旧页的词。
  try { if (window.RC && RC.wordpop && RC.wordpop.prewarm) RC.wordpop.prewarm(marks.map(m => m.word).filter(Boolean)); } catch (_) {}
}
function _vocabUnderlineEnabled() {
  // localStorage 默认开
  const v = localStorage.getItem('pdf-vocab-underline');
  return v === null ? true : (v === '1');
}
