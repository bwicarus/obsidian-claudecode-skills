// ──────── char-layer：PyMuPDF char-level bbox 驱动的精确选中 ────────

let _charSel = null;   // {pw, startIdx, endIdx, dragging}

async function loadCharsAndBindLayer(num, wrap, viewport, _retry) {
  _retry = _retry || 0;
  if (!wrap.isConnected) return;   // 页已被连续模式释放 → 放弃
  let d = null;
  try {
    const r = await fetch(`/pdf/api/page-chars?file=${encodeURIComponent(FILE_REL)}&page=${num}&v=${CHARS_VER}`);
    d = await r.json();
  } catch (e) { d = null; }
  if (!d || !d.ok) {
    // 偶尔失败(并发/超时) → 退避重试，避免整页无 char-layer(点选失灵)/无 L 框
    if (_retry < 2 && wrap.isConnected) {
      await new Promise(res => setTimeout(res, 500 + _retry * 600));
      return loadCharsAndBindLayer(num, wrap, viewport, _retry + 1);
    }
    window.dlog?.('chars api fail: ' + ((d && d.error) || 'fetch') + ' (retry ' + _retry + ')', '#ff6b6b');
    return;
  }
  const scale = viewport.scale;
  const pageH = d.page_h;
  // PDF 坐标 → viewport CSS px。PDF y 向上为正，需翻转
  // 关键修正：PyMuPDF rawdict 的 bbox 已经是 image coordinate (y 向下, 原点左上)
  // 之前误做 (pageH - y1) y 翻转 → 上下颠倒，整个 chars overlay 错位
  const charBoxes = d.chars.map((ch, _oi) => ({
    c: ch.c,
    _oi,   // PyMuPDF 原始顺序(=reading order)；sort 时 x 几乎重叠的字符靠它保序
    w: (ch.w == null ? -1 : ch.w),   // 所属词 id（PyMuPDF 分词）：判词边界用，根治连字/紧排
    bk: (ch.bk == null ? -1 : ch.bk),   // rawdict 块号：句子扩展按块切（斜体 w=-1 也不丢块）
    left:   ch.x0 * scale,
    top:    ch.y0 * scale,
    width:  (ch.x1 - ch.x0) * scale,
    height: (ch.y1 - ch.y0) * scale,
    sp: !!ch.sp,
    // raw PDF 坐标（pt）— 用于持久化高亮 rects（不随 zoom 变）
    _x0: ch.x0, _y0: ch.y0, _x1: ch.x1, _y1: ch.y1,
  }));
  wrap.__pageWPt = d.page_w;
  wrap.__pageHPt = d.page_h;
  wrap.__viewportScale = scale;
  // sort 让 idx 顺序 = visual reading order：用 baseline (top + height) + 宽松阈值
  // ref 用 max height 避开 subscript/superscript（小字符）误判到不同行
  charBoxes.sort((a, b) => {
    const aBase = a.top + a.height;
    const bBase = b.top + b.height;
    const ref = Math.max(a.height, b.height) || 1;
    if (Math.abs(aBase - bBase) > ref * 0.8) return aBase - bBase;
    // 同一个词内(PyMuPDF 分词)→ 严格按原始 reading order，绝不按 left 排乱(连字/紧排 often→etn 的根治)
    if (a.w !== -1 && a.w === b.w) return a._oi - b._oi;
    // 词信息缺失(旧数据/符号)的兜底：x 几乎重叠也保原序
    if (Math.abs(a.left - b.left) < ref * 0.3) return a._oi - b._oi;
    return a.left - b.left;
  });
  // 调试模式（URL 加 ?dbg=1）：显示 char bbox + 内容，验证 PyMuPDF 提取跟 PDF 视觉是否对齐
  if (new URLSearchParams(location.search).get('dbg') === '1') {
    const dbgLayer = document.createElement('div');
    dbgLayer.style.cssText = 'position:absolute;inset:0;pointer-events:none;z-index:5';
    charBoxes.forEach((c, i) => {
      const d = document.createElement('div');
      d.style.cssText = `position:absolute;left:${c.left}px;top:${c.top}px;width:${c.width}px;height:${c.height}px;border:1px solid rgba(255,0,0,.4);font-size:9px;color:rgba(255,0,0,.8);line-height:${c.height}px;text-align:center;font-family:monospace`;
      d.textContent = c.sp ? '·' : c.c;
      d.title = `idx=${i} c=${JSON.stringify(c.c)}`;
      dbgLayer.appendChild(d);
    });
    wrap.appendChild(dbgLayer);
  }
  wrap.__charBoxes = charBoxes;
  window.dlog?.('chars: ' + charBoxes.length + ' on page ' + num);
  // 创建 char-layer（透明覆盖整个 page-wrap）
  const cl = document.createElement('div');
  cl.className = 'char-layer';
  wrap.appendChild(cl);
  wrap.__charLayer = cl;
  _bindCharLayer(cl, wrap);
  // 已保存高亮：渲染到该页
  try { renderHighlightsOnPage(wrap, num); } catch(e) { window.dlog?.('hl render fail: '+e.message,'#ff6b6b'); }
  // 单词下划线（mastery 着色）；后端 page-chars 已返回 vocab_marks
  wrap.__vocabMarks = d.vocab_marks || [];
  wrap.__vocabSentences = d.vocab_sentences || [];
  try { renderVocabUnderlines(wrap, wrap.__vocabMarks); } catch(e) { window.dlog?.('vocab underline fail: '+e.message,'#ff6b6b'); }
  try { renderVocabSentences(wrap, wrap.__vocabSentences); } catch(e) { window.dlog?.('vocab sentence fail: '+e.message,'#ff6b6b'); }
  // 振假名/音标叠加（后端 page-chars 已返回 furigana：日语 unidic 读音 + 英文 ECDICT 音标）
  wrap.__furigana = d.furigana || [];
  wrap.__furiVerified = false;
  try { renderRubyLayer(wrap); } catch(e) { window.dlog?.('ruby fail: '+e.message,'#ff6b6b'); }
  if (_rubyEnabled()) _verifyFurigana(wrap);   // 振假名读音 AI 上下文校正(后台,不阻塞)
  try { renderPhraseHl(wrap); } catch(_) {}   // 词组持久高亮：重渲染后从状态恢复（防"自动消失"）
  try { renderExplainHl(wrap); } catch(_) {}   // 解释持久高亮：同上,重渲染后恢复
  // 整页翻译模式开着 → 新渲染/滚入的页自动翻译
  if (_pageTrOn) { wrap.__pageTrSeq = null; _pageTranslatePage(wrap); }
  // 全文搜索跳转：本页刚好是待高亮页 → 此处 __charBoxes 已赋值，直接画（不走轮询，秒级到位）
  if (window._pendingSearchHighlight && window._pendingSearchHighlight.page === num
      && wrap.__charBoxes && wrap.__charBoxes.length) {
    try { _highlightSearchResultsOnPage(wrap, window._pendingSearchHighlight.query); } catch (_) {}
    window._pendingSearchHighlight = null;
  }
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

function renderVocabUnderlines(pw, marks) {
  if (!_vocabUnderlineEnabled()) return;
  // 确保有 layer（即使 marks 空也要清旧残留）
  let layer = pw.querySelector('.vocab-layer');
  if (!layer && marks && marks.length) {
    layer = document.createElement('div');
    layer.className = 'vocab-layer';
    pw.appendChild(layer);
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
}
function _vocabUnderlineEnabled() {
  // localStorage 默认开
  const v = localStorage.getItem('pdf-vocab-underline');
  return v === null ? true : (v === '1');
}

