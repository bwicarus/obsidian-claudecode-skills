// ──────── char-layer：PyMuPDF char-level bbox 驱动的精确选中 ────────

let _charSel = null;   // {pw, startIdx, endIdx, dragging}

// chars → charBoxes(坐标映射 + reading-order 排序)。初次建层 + cv 校正重取 都用它。
// PyMuPDF rawdict bbox 已是 image coordinate(y 向下,原点左上),不做 y 翻转(翻了会上下颠倒)。
function _mapCharBoxes(chars, scale) {
  const cb = chars.map((ch, _oi) => ({
    c: ch.c, _oi,
    w: (ch.w == null ? -1 : ch.w),
    bk: (ch.bk == null ? -1 : ch.bk),
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
  const scale = viewport.scale;
  const cvKey = 'pdf-cv:' + FILE_REL + ':' + num;
  let cvGuess; try { cvGuess = localStorage.getItem(cvKey) || ('v' + CHARS_VER); } catch (_) { cvGuess = 'v' + CHARS_VER; }
  const charsUrl = (cv) => `/pdf/api/page-chars?file=${encodeURIComponent(FILE_REL)}&page=${num}&v=${CHARS_VER}&cv=${encodeURIComponent(cv)}`;
  // overlay(生词/句子/真 cv)**并行**拉,不阻塞选词层;chars 用上次 cv 猜测 → 命中 SW 缓存秒回 → 选词立即可用
  const ovP = fetch(`/pdf/api/page-overlay?file=${encodeURIComponent(FILE_REL)}&page=${num}`).then(r => r.json()).catch(() => null);
  let d = null;
  try { d = await (await fetch(charsUrl(cvGuess))).json(); } catch (e) { d = null; }
  if (!d || !d.ok) {
    if (_retry < 2 && wrap.isConnected) {
      await new Promise(res => setTimeout(res, 500 + _retry * 600));
      return loadCharsAndBindLayer(num, wrap, viewport, _retry + 1);
    }
    window.dlog?.('chars api fail: ' + ((d && d.error) || 'fetch') + ' (retry ' + _retry + ')', '#ff6b6b');
    return;
  }
  const charBoxes = _mapCharBoxes(d.chars, scale);
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
    if (!wrap.isConnected) return;
    if (ov && ov.ok && ov.cv) {
      try { localStorage.setItem(cvKey, ov.cv); } catch (_) {}   // 记真 cv,下次秒命中缓存
      if (ov.cv !== cvGuess) {
        // cvGuess 猜错(内容自上次起变过)→ 刚 chars 可能来自旧 SW 缓存 → 用真 cv 重取并刷新选词数据
        try {
          const d2 = await (await fetch(charsUrl(ov.cv))).json();
          if (d2 && d2.ok && wrap.isConnected) {
            wrap.__charBoxes = _mapCharBoxes(d2.chars, viewport.scale);
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
