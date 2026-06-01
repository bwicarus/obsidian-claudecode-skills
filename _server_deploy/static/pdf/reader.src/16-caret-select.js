function _caretAtPoint(x, y) {
  if (document.caretPositionFromPoint) {
    const p = document.caretPositionFromPoint(x, y);
    if (p && p.offsetNode) return {node: p.offsetNode, offset: p.offset};
  }
  if (document.caretRangeFromPoint) {
    const r = document.caretRangeFromPoint(x, y);
    if (r) return {node: r.startContainer, offset: r.startOffset};
  }
  return null;
}

// 把 textNode 的 offset 扩展到词边界（英文按 \w 扩展，CJK 按字粒度不变）
function _expandToWordBoundary(node, offset, direction) {
  if (!node || node.nodeType !== 3) return offset;
  const text = node.textContent || '';
  const isWord = (c) => /[A-Za-z0-9_]/.test(c);   // 仅扩展英文/数字（CJK 按字符）
  if (direction === 'start') {
    // 向左扩：如果当前位置在英文词内部，回退到词首
    let i = offset;
    if (i > 0 && i <= text.length && isWord(text[i - 1])) {
      while (i > 0 && isWord(text[i - 1])) i--;
    }
    return i;
  } else {
    // 向右扩：如果当前位置在英文词内部，前进到词尾
    let i = offset;
    if (i < text.length && isWord(text[i])) {
      while (i < text.length && isWord(text[i])) i++;
    }
    return i;
  }
}

// 用 (start, end) 两个 caret 设 selection、画 overlay、浮工具栏
function _applyRangeFromCarets(startC, endC, textLayerDiv) {
  if (!startC || !endC || !startC.node || !endC.node) return false;
  // 限定都在 textLayer 内
  if (!textLayerDiv.contains(startC.node) || !textLayerDiv.contains(endC.node)) return false;
  // 方向修正
  let s = startC, e = endC;
  if (s.node === e.node) {
    if (s.offset > e.offset) [s, e] = [e, s];
  } else {
    const pos = s.node.compareDocumentPosition(e.node);
    if (!(pos & Node.DOCUMENT_POSITION_FOLLOWING)) [s, e] = [e, s];
  }
  // 自动对齐词边界：起点向左、终点向右
  const startOffset = _expandToWordBoundary(s.node, s.offset, 'start');
  const endOffset   = _expandToWordBoundary(e.node, e.offset, 'end');
  const range = document.createRange();
  try {
    range.setStart(s.node, startOffset);
    range.setEnd(e.node, endOffset);
  } catch (_) { return false; }
  if (range.collapsed) return false;
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
  lastSelText = range.toString();
  _updateSelPreview(lastSelText);
  if (!lastSelText.trim()) { toolbar.classList.remove('open'); return false; }
  // overlay：用 PDF.js itemBoxes（PDF 原始坐标，跟 canvas 渲染同源，最准）
  document.querySelectorAll('.sel-overlay').forEach(ov => ov.innerHTML = '');
  let pw = (s.node.nodeType === 3 ? s.node.parentElement : s.node);
  while (pw && !pw.classList?.contains('page-wrap')) pw = pw.parentElement;
  if (pw) _paintWithItemBoxes(pw, range);
  const rect = range.getBoundingClientRect();
  const mainEl = document.getElementById('main');
  const mainRect = mainEl.getBoundingClientRect();
  toolbar.style.left = Math.max(8, rect.left - mainRect.left + mainEl.scrollLeft) + 'px';
  toolbar.style.top  = (rect.bottom - mainRect.top + mainEl.scrollTop + 6) + 'px';
  toolbar.classList.add('open');
  return true;
}

// 旧 textLayer 拖选状态（已被 char-layer 取代，保留声明避免引用报错）
let _dragStart = null;
let _legacyDragMoved = false;
let _dragMoveRaf = null;

function bindTextLayerClick(textLayerDiv) {
  // 单击/双击/三击逻辑（移动距离 < 8px 走这里）
  const handleClick = (span, clientX, clientY) => {
    if (!span || !textLayerDiv.contains(span) || !span.firstChild) return;
    const now = Date.now();
    if (_lastClickSpan === span && now - _lastClickTime < 380) {
      _clickCount = (_clickCount % 3) + 1;
    } else {
      _clickCount = 1;
      _lastClickSpan = span;
    }
    _lastClickTime = now;
    if (_clickCount === 1) {
      const offset = _spanOffsetFromPoint(span, clientX, clientY);
      const text = span.textContent || '';
      if (offset !== null) {
        const wb = _wordBoundsAt(text, offset);
        if (wb) { _selectSpanRange(span, wb.start, wb.end); return; }
      }
      _selectSpans([span]);
    } else if (_clickCount === 2) {
      _selectSpans(_spansInSameLine(span, textLayerDiv));
    } else {
      _selectSpans(_spansInParagraph(span, textLayerDiv));
    }
  };

  // 起始：mousedown / touchstart
  const onStart = (x, y, target) => {
    const span = target?.closest && target.closest('span');
    if (!span || !textLayerDiv.contains(span)) { _dragStart = null; window.dlog?.('START:miss span'); return; }
    const caret = _caretAtPoint(x, y);
    _dragStart = { x, y, caret, span, time: Date.now() };
    _legacyDragMoved = false;
    window.dlog?.('START x=' + Math.round(x) + ' y=' + Math.round(y) + ' caret=' + (caret ? (caret.node?.nodeName + '@' + caret.offset) : 'null'));
  };

  // 移动：mousemove / touchmove —— 实时更新选区
  const onMove = (x, y, ev) => {
    if (!_dragStart) return;
    const dx = Math.abs(x - _dragStart.x);
    const dy = Math.abs(y - _dragStart.y);
    if (dx + dy < 8) return;
    if (!_legacyDragMoved) {
      _legacyDragMoved = true;
      window.dlog?.('MOVE started, dx+dy=' + (dx+dy));
    }
    // touchmove 时 preventDefault 阻止页面滚动（让拖选生效）
    if (ev && ev.cancelable) ev.preventDefault();
    // RAF 节流
    if (_dragMoveRaf) return;
    _dragMoveRaf = requestAnimationFrame(() => {
      _dragMoveRaf = null;
      if (!_dragStart) return;
      const endC = _caretAtPoint(x, y);
      const ok = endC && _applyRangeFromCarets(_dragStart.caret, endC, textLayerDiv);
      if (!ok) window.dlog?.('MOVE caret/apply fail @ ' + Math.round(x)+','+Math.round(y) + ' endNode=' + (endC?.node?.nodeName || 'null'));
    });
  };

  // 结束：mouseup / touchend
  const onEnd = (x, y, target) => {
    if (!_dragStart) return;
    const start = _dragStart;
    _dragStart = null;
    if (_legacyDragMoved) {
      // 拖选：用结束位置 caret + 起始 caret 画范围
      const endC = _caretAtPoint(x, y);
      if (endC) _applyRangeFromCarets(start.caret, endC, textLayerDiv);
    } else {
      // 单击：走单/双/三击逻辑
      handleClick(start.span, x, y);
    }
  };

  // 鼠标
  textLayerDiv.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    onStart(e.clientX, e.clientY, e.target);
  });
  document.addEventListener('mousemove', (e) => onMove(e.clientX, e.clientY, null));
  document.addEventListener('mouseup', (e) => {
    if (!_dragStart) return;
    e.preventDefault();
    onEnd(e.clientX, e.clientY, e.target);
  });

  // 触屏
  textLayerDiv.addEventListener('touchstart', (e) => {
    if (e.touches.length !== 1) { _dragStart = null; return; }
    const t = e.touches[0];
    onStart(t.clientX, t.clientY, e.target);
  }, {passive: true});
  textLayerDiv.addEventListener('touchmove', (e) => {
    if (e.touches.length !== 1) return;
    const t = e.touches[0];
    onMove(t.clientX, t.clientY, e);
  }, {passive: false});   // 必须 passive:false 才能 preventDefault 阻滚动
  textLayerDiv.addEventListener('touchend', (e) => {
    if (!_dragStart) return;
    const t = e.changedTouches[0];
    if (!t) { _dragStart = null; return; }
    e.preventDefault();
    onEnd(t.clientX, t.clientY, e.target);
  });
  textLayerDiv.addEventListener('touchcancel', () => { _dragStart = null; });

  // 屏蔽 textLayer 内的 click 默认行为（避免双击/三击逻辑被 mouseup 已处理后又跑一遍）
  textLayerDiv.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); });
}

function paintSelectionOverlay() {
  // 清空所有 page-wrap 内的 sel-overlay 高亮
  document.querySelectorAll('.sel-overlay').forEach(ov => ov.innerHTML = '');
  const sel = window.getSelection();
  if (!sel.rangeCount) return;
  const rng = sel.getRangeAt(0);
  if (rng.collapsed) return;
  // 拿到选区在 textLayer 内的所有矩形（多行选中会有多个）
  const rects = rng.getClientRects();
  if (!rects.length) return;
  // 找到选区起点所在的 page-wrap（多页选中暂只画当前页）
  let pw = rng.startContainer.parentElement;
  while (pw && !pw.classList?.contains('page-wrap')) pw = pw.parentElement;
  if (!pw) return;
  const ov = pw.querySelector('.sel-overlay');
  if (!ov) return;
  const pwRect = pw.getBoundingClientRect();
  for (const r of rects) {
    if (!r.width || !r.height) continue;
    const div = document.createElement('div');
    div.className = 'hl';
    div.style.left   = (r.left - pwRect.left) + 'px';
    div.style.top    = (r.top  - pwRect.top)  + 'px';
    div.style.width  = r.width  + 'px';
    div.style.height = r.height + 'px';
    ov.appendChild(div);
  }
}

function checkSelection() {
  const sel = window.getSelection();
  const txt = (sel.toString() || '').trim();
  if (!txt || txt.length < 2) {
    // 无原生 selection：char-layer 自定义选中(画在 sel-overlay、不走原生 selection)还在的话别清掉它
    if (!(_charSel && lastSelText)) paintSelectionOverlay();
    return;
  }
  paintSelectionOverlay();
  lastSelText = txt;
    _updateSelPreview(lastSelText);
  try {
    const rng = sel.getRangeAt(0);
    const rect = rng.getBoundingClientRect();
    if (!rect.width && !rect.height) return;
    const mainEl = document.getElementById('main');
    const mainRect = mainEl.getBoundingClientRect();
    toolbar.style.left = Math.max(8, (rect.left - mainRect.left + mainEl.scrollLeft)) + 'px';
    toolbar.style.top  = (rect.bottom - mainRect.top + mainEl.scrollTop + 6) + 'px';
    toolbar.classList.add('open');
  } catch (_) {}
}
function scheduleCheck(delay) {
  clearTimeout(_selTimer);
  _selTimer = setTimeout(checkSelection, delay || 50);
}
document.addEventListener('mouseup', () => scheduleCheck(20));
document.addEventListener('touchend', () => scheduleCheck(250));   // iPad 长按完释放
document.addEventListener('selectionchange', () => scheduleCheck(200));
// 点 toolbar 外 + 既无 native selection 也无 char-layer 选中 + 不在 char-layer 上 → 关 toolbar
function _shouldCloseToolbar(target) {
  if (toolbar.contains(target)) return false;            // 点在 toolbar 内
  if (target?.closest?.('.char-layer')) return false;    // 点在 char-layer 上（char-layer 自己处理）
  const t = (window.getSelection().toString() || '').trim();
  if (t) return false;
  if (lastSelText && lastSelText.trim()) return false;   // char-layer 选中状态
  return true;
}
document.addEventListener('mousedown', (e) => {
  if (_shouldCloseToolbar(e.target)) {
    toolbar.classList.remove('open');
    document.querySelectorAll('.sel-overlay').forEach(ov => ov.innerHTML = '');
    lastSelText = '';
    _updateSelPreview('');
  }
});
document.addEventListener('touchstart', (e) => {
  if (_shouldCloseToolbar(e.target)) {
    toolbar.classList.remove('open');
    document.querySelectorAll('.sel-overlay').forEach(ov => ov.innerHTML = '');
    lastSelText = '';
    _updateSelPreview('');
  }
}, {passive: true});

