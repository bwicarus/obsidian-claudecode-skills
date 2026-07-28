// ──────── 旧 textLayer 事件机制（保留备用，但 char-layer 接管后不会触发）────────
let _lastClickSpan = null, _legacyClickTime = 0, _legacyClickCount = 0;

function _spansInSameLine(targetSpan, textLayerDiv) {
  // 同一行 = Y 中心相差 < 半行高
  const tRect = targetSpan.getBoundingClientRect();
  if (!tRect.height) return [targetSpan];
  const tMid = tRect.top + tRect.height / 2;
  const tol  = tRect.height * 0.5;
  const out = [];
  textLayerDiv.querySelectorAll('span').forEach(s => {
    if (!s.firstChild) return;   // 跳过 marked-content / endOfContent
    const r = s.getBoundingClientRect();
    if (!r.height) return;
    if (Math.abs((r.top + r.height/2) - tMid) <= tol) out.push(s);
  });
  out.sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
  return out.length ? out : [targetSpan];
}

function _spansInParagraph(targetSpan, textLayerDiv) {
  // 段落 = 当前行向上下扩展直到行间距明显变大 / 没有 spans
  const allSpans = Array.from(textLayerDiv.querySelectorAll('span'))
    .filter(s => s.firstChild && s.getBoundingClientRect().height);
  if (!allSpans.length) return [targetSpan];
  // 按 Y 排序
  const items = allSpans.map(s => {
    const r = s.getBoundingClientRect();
    return {span: s, top: r.top, bot: r.bottom, mid: r.top + r.height/2, h: r.height};
  }).sort((a, b) => a.top - b.top);
  // 分行（Y 中心相近合并）
  const lines = [];
  for (const it of items) {
    const last = lines[lines.length - 1];
    if (last && Math.abs(it.mid - last.midAvg) <= it.h * 0.5) {
      last.items.push(it);
      last.midAvg = (last.midAvg * (last.items.length - 1) + it.mid) / last.items.length;
    } else {
      lines.push({items: [it], midAvg: it.mid, h: it.h});
    }
  }
  // 找 targetSpan 所在行
  let idx = lines.findIndex(L => L.items.some(it => it.span === targetSpan));
  if (idx < 0) return [targetSpan];
  // 向上/下扩展：行间距 < 2.2x avg lineHeight 算同一段
  const avgH = lines[idx].h;
  let lo = idx, hi = idx;
  while (lo > 0) {
    const gap = lines[lo].midAvg - lines[lo-1].midAvg;
    if (gap > avgH * 2.2) break;
    lo--;
  }
  while (hi < lines.length - 1) {
    const gap = lines[hi+1].midAvg - lines[hi].midAvg;
    if (gap > avgH * 2.2) break;
    hi++;
  }
  const out = [];
  for (let i = lo; i <= hi; i++) {
    lines[i].items.sort((a, b) => a.span.getBoundingClientRect().left - b.span.getBoundingClientRect().left);
    out.push(...lines[i].items.map(it => it.span));
  }
  return out;
}

function _selectSpans(spans) {
  if (!spans.length) return;
  const range = document.createRange();
  const firstTxt = spans[0].firstChild;
  const lastTxt  = spans[spans.length - 1].firstChild;
  if (!firstTxt || !lastTxt) return;
  range.setStart(firstTxt, 0);
  range.setEnd(lastTxt, (lastTxt.textContent || '').length);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
  lastSelText = spans.map(s => s.textContent || '').join('').trim();
  _updateSelPreview(lastSelText);
  // overlay：用 itemBoxes（跟 canvas 渲染对齐）
  document.querySelectorAll('.sel-overlay').forEach(ov => ov.innerHTML = '');
  let pw = spans[0].parentElement;
  while (pw && !pw.classList?.contains('page-wrap')) pw = pw.parentElement;
  if (pw) _paintWithItemBoxes(pw, range);
  // 工具栏位置：用 overlay 内最后一个高亮 div 的位置
  if (pw) {
    const ov = pw.querySelector('.sel-overlay');
    const lastHl = ov?.lastElementChild;
    if (lastHl) {
      const pwRect = pw.getBoundingClientRect();
      const mainEl = document.getElementById('main');
      const mainRect = mainEl.getBoundingClientRect();
      const left = pwRect.left - mainRect.left + mainEl.scrollLeft + parseFloat(lastHl.style.left);
      const top  = pwRect.top  - mainRect.top  + mainEl.scrollTop  + parseFloat(lastHl.style.top) + parseFloat(lastHl.style.height) + 6;
      toolbar.style.left = Math.max(8, left) + 'px';
      toolbar.style.top  = top + 'px';
      toolbar.classList.add('open');
      return;
    }
  }
  const rect = range.getBoundingClientRect();
  const mainEl = document.getElementById('main');
  const mainRect = mainEl.getBoundingClientRect();
  toolbar.style.left = Math.max(8, rect.left - mainRect.left + mainEl.scrollLeft) + 'px';
  toolbar.style.top  = (rect.bottom - mainRect.top + mainEl.scrollTop + 6) + 'px';
  toolbar.classList.add('open');
}

function paintSelectionFromSpans(spans) {
  document.querySelectorAll('.sel-overlay').forEach(ov => ov.innerHTML = '');
  if (!spans.length) return;
  // 找 spans 所在的 page-wrap
  let pw = spans[0].parentElement;
  while (pw && !pw.classList?.contains('page-wrap')) pw = pw.parentElement;
  if (!pw) return;
  const ov = pw.querySelector('.sel-overlay');
  if (!ov) return;
  const pwRect = pw.getBoundingClientRect();
  for (const s of spans) {
    const r = s.getBoundingClientRect();
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

// 点击坐标 → span 内字符 offset（用 caret API，兼容新/旧浏览器）
function _spanOffsetFromPoint(span, x, y) {
  const node = span.firstChild;
  if (!node || node.nodeType !== 3) return null;
  // 新 API (Firefox / Chrome 128+)
  if (document.caretPositionFromPoint) {
    const pos = document.caretPositionFromPoint(x, y);
    if (pos && pos.offsetNode === node) return pos.offset;
  }
  // 旧 API (Safari / 老 Chrome)
  if (document.caretRangeFromPoint) {
    const r = document.caretRangeFromPoint(x, y);
    if (r && r.startContainer === node) return r.startOffset;
  }
  return null;
}

// 从 offset 向左/右扩展到词边界
function _wordBoundsAt(text, offset) {
  if (!text) return null;
  // 中日韩字符 / 字母数字下划线 / 部分常用符号算 "词字符"
  const isWord = (c) => /[\w一-鿿぀-ヿ㐀-䶿가-힯]/.test(c);
  let lo = Math.max(0, Math.min(offset, text.length));
  let hi = lo;
  // 点击点不在 word 字符上 → 偏左一格再判
  if (lo < text.length && !isWord(text[lo])) {
    if (lo > 0 && isWord(text[lo - 1])) lo--;
    else return null;   // 点在标点/空白
  }
  while (lo > 0 && isWord(text[lo - 1])) lo--;
  hi = Math.max(hi, offset);
  while (hi < text.length && isWord(text[hi])) hi++;
  if (hi <= lo) return null;
  // 中文逐字选中（每字一"词"）：如果是 CJK 范围，缩小到点击的那一个字
  const cjk = (c) => /[一-鿿㐀-䶿]/.test(c);
  if (offset < text.length && cjk(text[offset])) {
    return {start: offset, end: offset + 1};
  }
  if (offset > 0 && cjk(text[offset - 1])) {
    return {start: offset - 1, end: offset};
  }
  return {start: lo, end: hi};
}

// 高亮整段 textDivs 范围（不画字符级范围 — PDF.js 集成限制让字符级位置不准）
// 用户精确选中的内容看工具栏 preview，这里只显示"涉及哪些 textDiv 段"
function _paintWithItemBoxes(pw, range) {
  const ov = pw.querySelector('.sel-overlay');
  if (!ov) return;
  ov.innerHTML = '';
  if (!pw.__textDivs || !pw.__getSpanIndex) return;
  const startSpan = (range.startContainer.nodeType === 3) ? range.startContainer.parentElement : range.startContainer;
  const endSpan   = (range.endContainer.nodeType === 3) ? range.endContainer.parentElement : range.endContainer;
  let sIdx = pw.__getSpanIndex(startSpan);
  let eIdx = pw.__getSpanIndex(endSpan);
  if (sIdx < 0 || eIdx < 0) return;
  if (sIdx > eIdx) { const t = sIdx; sIdx = eIdx; eIdx = t; }
  const pwRect = pw.getBoundingClientRect();
  for (let i = sIdx; i <= eIdx; i++) {
    const td = pw.__textDivs[i];
    if (!td) continue;
    const r = td.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const hl = document.createElement('div');
    hl.className = 'hl';
    hl.style.left   = (r.left - pwRect.left) + 'px';
    hl.style.top    = (r.top  - pwRect.top)  + 'px';
    hl.style.width  = r.width  + 'px';
    hl.style.height = r.height + 'px';
    ov.appendChild(hl);
  }
}

// 选中 span 内的子串 [start, end)，画 overlay，浮工具栏
function _selectSpanRange(span, start, end) {
  const node = span.firstChild;
  if (!node) return;
  const range = document.createRange();
  range.setStart(node, start);
  range.setEnd(node, Math.min(end, (node.textContent || '').length));
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
  lastSelText = (node.textContent || '').slice(start, end);
  _updateSelPreview(lastSelText);
  // overlay：用 itemBoxes（PDF.js 原始坐标）
  document.querySelectorAll('.sel-overlay').forEach(ov => ov.innerHTML = '');
  let pw = span.parentElement;
  while (pw && !pw.classList?.contains('page-wrap')) pw = pw.parentElement;
  if (pw) _paintWithItemBoxes(pw, range);
  // 工具栏：item box 算位置（跟 overlay 同坐标系，对齐 PDF）
  if (pw && pw.__itemBoxes && pw.__getSpanIndex) {
    const idx = pw.__getSpanIndex(span);
    const box = idx >= 0 ? pw.__itemBoxes[idx] : null;
    if (box) {
      const pwRect = pw.getBoundingClientRect();
      const mainEl = document.getElementById('main');
      const mainRect = mainEl.getBoundingClientRect();
      const charW = box.w / Math.max(1, box.str.length);
      const left = pwRect.left - mainRect.left + mainEl.scrollLeft + box.x + (start * charW);
      const top  = pwRect.top  - mainRect.top  + mainEl.scrollTop  + box.y + box.h + 6;
      toolbar.style.left = Math.max(8, left) + 'px';
      toolbar.style.top  = top + 'px';
      toolbar.classList.add('open');
      return;
    }
  }
  // fallback：用 range rect
  const rect = range.getBoundingClientRect();
  const mainEl = document.getElementById('main');
  const mainRect = mainEl.getBoundingClientRect();
  toolbar.style.left = Math.max(8, rect.left - mainRect.left + mainEl.scrollLeft) + 'px';
  toolbar.style.top  = (rect.bottom - mainRect.top + mainEl.scrollTop + 6) + 'px';
  toolbar.classList.add('open');
}

// 更新工具栏内 preview 文本（让用户 verify 选中内容；视觉高亮可能跟 canvas 错位时这是 ground truth）
function _updateSelPreview(text) {
  const el = document.getElementById('sel-preview');
  text = (text || '').trim();
  // 出向选区/焦点同步(2026-07-28 修):PDF 的真实选中走 char-layer(画在 sel-overlay,
  // **不产生原生 selection**),而 `checkSelection` 只挂在 mouseup/touchend/selectionchange 上
  // → char-layer 选中完成后没有任何事件通知出向漏斗,结果 UI 有选中、journal 无 focus,
  //   甚至因 `_charSel` 被滚动/翻页判定清空而误发 cancel(用户实测:p23 选中后 selection='')。
  // 这里是**选中变更的唯一全覆盖通知点**(提交与清空两条路径都经过它),所以挂在此处一处即可,
  // 不新建第二套选择机制。_ctxSelReport 内部已做:空串=显式取消、非空=focus('text')。
  try { if (typeof _ctxSelReport === 'function') _ctxSelReport(text); } catch (_) {}
  // 选中元数据(所在页 + 时戳),给语音/侧栏助手 __voiceContext 做「跨页陈旧选中」校验:
  // 翻到别页后旧选中不再当成"现在在问的内容"。每次选中变化都先清空所在句(char-layer 路径随后会补)。
  try {
    window.__lastSelSentence = '';
    window.__lastSelMeta = text
      ? { page: (typeof currentPage !== 'undefined' ? currentPage : 0), t: Date.now() }
      : null;
  } catch (_) {}
  if (!el) return;
  if (!text) { el.textContent = '—'; return; }
  const max = 120;
  const display = text.length > max
    ? text.slice(0, 60) + ' … ' + text.slice(-40)
    : text;
  // 提示用户：虚线框是 textDiv 段范围（粗略），实际选中以下面文字为准
  el.innerHTML = '<b>已选：</b>' + display.replace(/&/g,'&amp;').replace(/</g,'&lt;') +
                 '<span class="len">（' + text.length + ' 字）</span>';
  _updateToolbarMode(text);
}

// 选中后按「单词 vs 多词」切换工具栏按钮组：单词→查词；多词→翻译+解释
function _updateToolbarMode(text) {
  const t = (text || '').trim();
  const isWord = t.length > 0 && t.length <= 30 && /^[A-Za-z][A-Za-z'’\-]*$/.test(t);
  const w = document.getElementById('sel-btns-word');
  const m = document.getElementById('sel-btns-multi');
  if (w) w.style.display = isWord ? 'flex' : 'none';
  if (m) m.style.display = isWord ? 'none' : 'flex';
  // 短词组（F6）：只显示「📘词组」按钮(呼吸提示)。选中高亮本身=普通选中，点别处照常消失；
  // 只有点了「词组」按钮、在查询期间才把选区变持久呼吸高亮(见 showPhrasePopover)。
  const phrase = !isWord && _isShortPhrase(t);
  const pb = document.getElementById('sel-phrase-btn');
  if (pb) { pb.style.display = phrase ? '' : 'none'; pb.classList.toggle('breathe', phrase); }
}

// 短词组判定：中日 2-8 字(无句末标点) / 拉丁 2-5 词且不太长(无句末标点)
function _isShortPhrase(text) {
  const t = (text || '').trim();
  if (!t) return false;
  if (/[。！？、，.!?]$/.test(t)) return false;
  if (/[぀-ヿ㐀-鿿]/.test(t)) {
    return t.length >= 2 && t.length <= 8 && !/[。！？、，.!?]/.test(t);
  }
  const words = t.split(/\s+/).filter(Boolean);
  return words.length >= 2 && words.length <= 5 && t.length <= 40;
}
let _phraseBreatheTimer = null;
function _setSelPhraseBreathe(on) {
  clearTimeout(_phraseBreatheTimer);
  document.querySelectorAll('.sel-overlay').forEach(o => o.classList.toggle('phrase-breathe', on));
  if (on) {   // 呼吸 1.6s 后转常亮（去掉动画类，高亮保持）；按钮也停止呼吸
    _phraseBreatheTimer = setTimeout(() => {
      document.querySelectorAll('.sel-overlay.phrase-breathe').forEach(o => o.classList.remove('phrase-breathe'));
      document.getElementById('sel-phrase-btn')?.classList.remove('breathe');
    }, 1600);
  }
}


window._updateToolbarMode = _updateToolbarMode;   // 实况网页(web-adapter)复用同一套按钮组开关(审计 #12)
