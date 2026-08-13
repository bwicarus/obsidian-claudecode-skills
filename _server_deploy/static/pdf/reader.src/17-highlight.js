// ─────────── PDF 高亮：sidecar JSON 持久化 + 渲染 + popover ───────────
const DEFAULT_HL_COLORS = ['#fff59d','#a7f3d0','#a3d4ff','#fda4af'];
function getHlColors() {
  try {
    const arr = JSON.parse(localStorage.getItem('pdf-hl-colors') || 'null');
    return (Array.isArray(arr) && arr.length) ? arr : DEFAULT_HL_COLORS;
  } catch { return DEFAULT_HL_COLORS; }
}
function saveHlColors(arr) {
  localStorage.setItem('pdf-hl-colors', JSON.stringify(arr));
}
let _lastHlColor = localStorage.getItem('pdf-hl-last-color') || DEFAULT_HL_COLORS[0];
// ── 高亮触摸手势:全阅读器**唯一实例**(照 EPUB epub-html.js 的 document 委托做法)。
//    key = 高亮 id,故同一条高亮的多个重叠 rect 上的两次点会正确配对成双击。
let _hlOpenedAt = 0;
function _openHlEditorById(id) {
  const now = Date.now(); if (now - _hlOpenedAt < 520) return;   // 去重:iOS 的 pointerup 与原生 dblclick 可能双触发
  const h = _allHighlights.find((x) => x.id === id); if (!h) return;
  const div = document.querySelector('.hl-saved[data-id="' + (window.CSS && CSS.escape ? CSS.escape(id) : id) + '"]');
  const pw = div && div.closest ? div.closest('.page-wrap') : null;
  if (!div || !pw) return;
  _hlOpenedAt = now; openHlPopover(h, div, pw);
}
let _hlGestureSingleton = null;
function _hlGesture() {
  if (_hlGestureSingleton) return _hlGestureSingleton;
  if (!(window.RC && RC.highlight && RC.highlight.gesture)) return null;
  _hlGestureSingleton = RC.highlight.gesture({
    onLongPress: (id) => {
      const h = _allHighlights.find((x) => x.id === id);
      if (h && window.__asstOpen && window.__asstOpen()) _pdfHlToAsst(h);
    },
    doubleTapMs: 420, moveTol: 12,
    onDoubleTap: (id) => _openHlEditorById(id)
  });
  // 原生 dblclick 兜底:两次点落在不同 rect 时 dblclick 派发到公共祖先,故委托在 document 上按落点反查。
  document.addEventListener('dblclick', (e) => {
    const el = document.elementFromPoint(e.clientX, e.clientY);
    const hit = el && el.closest ? el.closest('.hl-saved') : null;
    if (!hit || !hit.dataset.id) return;
    e.preventDefault(); e.stopPropagation();
    _openHlEditorById(hit.dataset.id);
  }, true);
  return _hlGestureSingleton;
}
let _allHighlights = [];
let _hlByPage = {};
let _resultContext = null;   // {charSel, text, sentence, kind} 由 onTranslate/onExplain 入口存
let _resultReqId = 0;        // 结果框请求序号：每开一次新框 +1，异步回调写入前比对，过期(被新任务覆盖)就丢弃

function escHtml(s) { return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function _toast(msg) {
  let t = document.getElementById('hl-toast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'hl-toast';
    t.style.cssText = 'position:fixed;left:50%;bottom:30px;transform:translateX(-50%);background:#10162a;border:1px solid #3b6db5;color:#cfe6ff;padding:9px 18px;border-radius:8px;font-size:13px;z-index:500;box-shadow:0 6px 16px rgba(0,0,0,.6);pointer-events:none';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.style.display = 'block';
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.style.display = 'none'; }, 1800);
}

async function loadAllHighlights() {
  try {
    const r = await fetch('/pdf/api/highlights?file=' + encodeURIComponent(FILE_REL), { cache: 'no-store' });   // no-store:撤销/改高亮后必须拿最新,否则命中浏览器缓存看不到变化
    const d = await r.json();
    if (!d.ok) return;
    _allHighlights = d.highlights || [];
    _hlByPage = {};
    for (const h of _allHighlights) (_hlByPage[h.page] ||= []).push(h);
    document.querySelectorAll('.page-wrap[data-loaded="1"]').forEach(pw => {
      const n = parseInt(pw.dataset.pageNum || '0');
      if (n) renderHighlightsOnPage(pw, n);
    });
    window.dlog?.('高亮已加载：' + _allHighlights.length + ' 条');
  } catch (e) { window.dlog?.('hl load fail: ' + e.message, '#ff6b6b'); }
}

// 高亮底色用**半透明** rgba(不是实色):字一定透得出来(不被实色块盖死);配合 .hl-saved 的 mix-blend-mode:multiply,
// 能 multiply 到页图上时字更锐(黄×黑字=黑),万一某些页 multiply 被隔离也只是「半透明色」不会糊死/盖死正文。
function _hlRgba(hex, a) {
  const m = /^#?([0-9a-fA-F]{6})$/.exec((hex || '').trim());
  if (!m) return hex;   // 非 #rrggbb(已是 rgba/命名色)→ 原样
  const n = parseInt(m[1], 16);
  return 'rgba(' + (n >> 16 & 255) + ',' + (n >> 8 & 255) + ',' + (n & 255) + ',' + a + ')';
}
function renderHighlightsOnPage(pw, pageNum) {
  if (!pw) return;
  const layer = ensurePageLayer(pw, 'hl-layer');
  // 永远把 hl-layer append 到最后，让它在 DOM 顺序上晚于 char-layer
  // （配合 z-index:5 双保险）→ hl-saved 在最上层接收点击
  pw.appendChild(layer);
  layer.innerHTML = '';
  const list = _hlByPage[pageNum] || [];
  const canvas = pw.querySelector('canvas');
  const cssW = canvas?.clientWidth || pw.clientWidth;
  const cssH = canvas?.clientHeight || pw.clientHeight;
  if (!cssW || !cssH) return;
  for (const h of list) {
    const pw_pt = h.page_w || pw.__pageWPt || cssW;
    const ph_pt = h.page_h || pw.__pageHPt || cssH;
    const sx = cssW / pw_pt;
    const sy = cssH / ph_pt;
    for (const r of (h.rects || [])) {
      const [x0, y0, x1, y1] = r;
      const div = document.createElement('div');
      const hasColor = !!(h.color && h.color.trim());
      const hasNote  = !!((h.note||'').trim() || (h.body||'').trim() || (h.sentence||'').trim());
      div.className = 'hl-saved'
        + (hasNote ? ' has-note' : '')
        + (hasColor ? '' : ' no-color');
      div.dataset.id = h.id;
      div.style.left = (x0 * sx) + 'px';
      div.style.top = (y0 * sy) + 'px';
      div.style.width = ((x1 - x0) * sx) + 'px';
      div.style.height = ((y1 - y0) * sy) + 'px';
      if (hasColor) div.style.background = _hlRgba(h.color, 0.4);   // 半透明底色:字透出来,不被实色盖死
      div.title = (h.note || h.body || h.sentence || h.text || '').slice(0, 200);
      // 用 capture phase 拦事件，确保不被 char-layer 拖选逻辑捕获
      const stop = (e) => { e.stopPropagation(); };
      div.addEventListener('mousedown',  stop, true);
      div.addEventListener('mouseup',    stop, true);
      div.addEventListener('touchstart', stop, {passive:true, capture:true});
      div.addEventListener('touchend',   stop, {passive:true, capture:true});
      // 交互(2026-07-05;2026-07-21 用户要求长按↔双击对调):长按 → 高亮内容加入对话上下文(助手开着时);双击 → 高亮弹框。单击不再开框。
      // 走共享 RC.highlight.gesture 的**唯一实例**(与 EPUB 的 document 委托同构)。
      // ⚠ 手势状态必须按 h.id 跨 rect 共享:一条高亮有多个**互相重叠**的 rect div,若每个 div 各持
      //   一个 gesture 实例,两次点落在不同 rect 上就各自算「第一击」,原生 dblclick 又因 target
      //   不同而派发到公共祖先 → iPad 上必须点三次才弹框(用户实测)。
      const _hg = _hlGesture();
      if (_hg) {
        div.addEventListener('pointerdown',   (e) => { e.stopPropagation(); _hg.down(h.id, e.clientX, e.clientY); }, true);
        div.addEventListener('pointermove',   (e) => { _hg.move(e.clientX, e.clientY); }, true);
        div.addEventListener('pointerup',     (e) => { e.stopPropagation(); _hg.up(h.id); }, true);
        div.addEventListener('pointercancel', () => _hg.cancel(), true);
      } else {
        div.addEventListener('click', (e) => { e.stopPropagation(); openHlPopover(h, div, pw); }, true);
      }
      layer.appendChild(div);
    }
  }
}

// 双击高亮(助手开着)→ 复用助手输入框上方的「焦点选中」chip(window.__setFocusSel:带 ✕、可随时取消、一眼看到已选中),
// 不弹 toast。__focusSel 经 __voiceContext.focus_sel 带给助手。跟公式/段落焦点选区同一套 UI。
function _pdfHlToAsst(h) {
  try {
    const txt = ((h.text || h.body || h.sentence || '') + '').trim(); if (!txt) return;
    // 复用助手输入框上方的「焦点选中」chip(带 ✕,可随时取消 + 一眼看到已选中),不弹 toast。__focusSel 进 __voiceContext.focus_sel。
    if (window.__setFocusSel) window.__setFocusSel(txt.slice(0, 400), 'text');
  } catch (e) {}
}

// 把 chars[s..e] 合并成连续 PDF pt rects（同行合并）
function _charsRangeToRects(chars, sIdx, eIdx) {
  if (sIdx > eIdx) [sIdx, eIdx] = [eIdx, sIdx];
  const rects = [];
  let cur = null;
  for (let i = sIdx; i <= eIdx; i++) {
    const c = chars[i];
    if (c.sp && (!c.width || c.width < 0.5)) {
      if (cur && c._x1 > cur.x1) cur.x1 = c._x1;
      continue;
    }
    const x0 = c._x0, y0 = c._y0, x1 = c._x1, y1 = c._y1;
    const lineH = y1 - y0;
    const _sameBk = cur && c.bk != null && cur.bk != null && c.bk >= 0 && c.bk === cur.bk;   // #56:同排版块=同视觉行(OCR justified),水平相邻即合并,不受块内字符 top 抖动分段(治句子/下划线在括号处断)
    if (cur &&
        (_sameBk || Math.abs(y0 - cur.y0) <= Math.max(2, Math.max(cur.y1 - cur.y0, y1 - y0) * 0.6)) &&   // 跨块才按 y0 判行(跨行 y0 差>字高分段)
        x0 <= cur.x1 + Math.max(2, lineH * 0.6)) {
      cur.x1 = Math.max(cur.x1, x1);
      cur.y0 = Math.min(cur.y0, y0);
      cur.y1 = Math.max(cur.y1, y1);
    } else {
      if (cur) rects.push([cur.x0, cur.y0, cur.x1, cur.y1]);
      cur = {x0, y0, x1, y1, bk: c.bk};
    }
  }
  if (cur) rects.push([cur.x0, cur.y0, cur.x1, cur.y1]);
  return rects;
}

async function saveHighlight({pw, sIdx, eIdx, color, kind='note', sentence='', body='', note='', id='', silent=false}) {
  if (!pw || !pw.__charBoxes) {
    if (silent) throw new Error('BW_READER_HIGHLIGHT_TEXT_LAYER_UNAVAILABLE');
    alert('请先在 PDF 上选中再标记'); return null;
  }
  const chars = pw.__charBoxes;
  if (sIdx < 0 || eIdx >= chars.length) {
    if (silent) throw new Error('BW_READER_HIGHLIGHT_TEXT_RANGE_INVALID');
    return null;
  }
  const rects = _charsRangeToRects(chars, sIdx, eIdx);
  if (!rects.length) {
    if (silent) throw new Error('BW_READER_HIGHLIGHT_TEXT_RECTS_EMPTY');
    return null;
  }
  const pageNum = parseInt(pw.dataset.pageNum || '0');
  const text = _charsRangeToText(chars, Math.min(sIdx,eIdx), Math.max(sIdx,eIdx));
  const payload = {
    file: FILE_REL, page: pageNum, rects, color, text,
    kind, sentence, body, note,
    page_w: pw.__pageWPt, page_h: pw.__pageHPt,
  };
  if (/^c_[a-f0-9]{8,32}$/.test(id || '')) payload.id = id;
  try {
    let d;
    const nativeRuntime = window.__BW_READER_RUNTIME__;
    const directLocal = silent && payload.id && nativeRuntime &&
      typeof nativeRuntime.savePDFHighlight === 'function';
    if (directLocal) {
      window.dlog?.('精确高亮: 本地独立写入开始');
      let timer = 0;
      try {
        d = await Promise.race([
          nativeRuntime.savePDFHighlight(payload),
          new Promise((_, reject) => {
            timer = setTimeout(() => reject(new Error(
              'BW_READER_HIGHLIGHT_LOCAL_WRITE_TIMEOUT'
            )), 6000);
          })
        ]);
      } finally {
        if (timer) clearTimeout(timer);
      }
      window.dlog?.('精确高亮: 本地写入完成');
    } else {
      const r = await fetch('/pdf/api/highlights', {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify(payload),
      });
      d = await r.json();
    }
    if (!d.ok) {
      if (silent) throw new Error('BW_READER_HIGHLIGHT_SAVE_REJECTED:' + (d.error || '?'));
      alert('保存高亮失败：' + (d.error || '?')); return null;
    }
    if (d.highlight && d.highlight.id) {
      _allHighlights = _allHighlights.filter((h) => h && h.id !== d.highlight.id);
      _hlByPage[pageNum] = (_hlByPage[pageNum] || []).filter((h) => h && h.id !== d.highlight.id);
    }
    _allHighlights.push(d.highlight);
    (_hlByPage[pageNum] ||= []).push(d.highlight);
    renderHighlightsOnPage(pw, pageNum);
    _lastHlColor = color;
    localStorage.setItem('pdf-hl-last-color', color);
    return d.highlight;
  } catch (e) {
    // 网络不通 + outbox → local-first:客户端生成 id(c_ 前缀),本地即时渲染,
    // 入队恢复后补投(服务端 POST 幂等 upsert 同 id,重放安全)。2026-07-20 outbox 第二批。
    if (window.RC && RC.outbox && e && e.name === 'TypeError') {
      const cid = /^c_[a-f0-9]{8,32}$/.test(id || '') ? id : ('c_' + Array.from(crypto.getRandomValues(new Uint8Array(8))).map(b => b.toString(16).padStart(2, '0')).join(''));
      const h = Object.assign({ id: cid, time: Math.floor(Date.now() / 1000) }, payload);
      _allHighlights = _allHighlights.filter((item) => item && item.id !== cid);
      _hlByPage[pageNum] = (_hlByPage[pageNum] || []).filter((item) => item && item.id !== cid);
      _allHighlights.push(h);
      (_hlByPage[pageNum] ||= []).push(h);
      renderHighlightsOnPage(pw, pageNum);
      _lastHlColor = color;
      try { localStorage.setItem('pdf-hl-last-color', color); } catch (_) {}
      RC.outbox.send('hl', cid, '/pdf/api/highlights', Object.assign({ id: cid }, payload));
      if (RC.toast) RC.toast('已高亮(离线,恢复后自动同步)');
      return h;
    }
    if (silent) throw e;
    alert('保存高亮异常：' + e.message);
    return null;
  }
}

function _pdfExactTextProjection(chars) {
  let text = '', map = [], last = null;
  const cjk = (value) => /[぀-ヿ㐀-鿿　-〿＀-￯]/.test(value || '');
  const append = (value, index) => {
    for (let k = 0; k < value.length; k++) { text += value[k]; map.push(index); }
  };
  for (let i = 0; i < (chars || []).length; i++) {
    const ch = chars[i] || {};
    if (last) {
      const cjkPair = cjk(ch.c) && cjk(last.c);
      const dy = Math.abs(Number(ch.top || 0) - Number(last.top || 0));
      if (dy > Number(ch.height || 0) * 0.5) {
        if (!cjkPair) append(' ', i);
      } else {
        const gap = Number(ch.left || 0) - (Number(last.left || 0) + Number(last.width || 0));
        const ref = Math.min(Number(ch.height || 0), Number(last.height || 0));
        if (!cjkPair && gap > ref * ((/[A-Za-z]/.test(ch.c || '') && /[A-Za-z]/.test(last.c || '')) ? 1.3 : 0.6) && !last.sp && !ch.sp) append(' ', i);
      }
    }
    append(ch.sp ? ' ' : String(ch.c || ''), i);
    last = ch;
  }
  let folded = '', foldedMap = [], space = false;
  for (let i = 0; i < text.length; i++) {
    if (/\s/.test(text[i])) {
      if (space) continue;
      folded += ' '; foldedMap.push(map[i]); space = true;
    } else {
      folded += text[i]; foldedMap.push(map[i]); space = false;
    }
  }
  return { text: folded.trim(), map: foldedMap.slice(folded.length - folded.trimStart().length) };
}

function _pdfExactTextRange(chars, sourceText) {
  const projected = _pdfExactTextProjection(chars);
  const query = String(sourceText || '').replace(/\s+/g, ' ').trim();
  if (!query) throw new Error('BW_READER_HIGHLIGHT_TEXT_EMPTY');
  const first = projected.text.indexOf(query);
  if (first < 0) throw new Error('BW_READER_HIGHLIGHT_TEXT_NOT_FOUND');
  if (projected.text.indexOf(query, first + 1) >= 0) throw new Error('BW_READER_HIGHLIGHT_TEXT_AMBIGUOUS');
  const start = projected.map[first];
  const end = projected.map[first + query.length - 1];
  if (!Number.isInteger(start) || !Number.isInteger(end)) throw new Error('BW_READER_HIGHLIGHT_TEXT_RANGE_INVALID');
  return { start, end };
}

async function _pdfExactTextPage(targetPage) {
  const page = Number(targetPage);
  if (!Number.isInteger(page) || page < 1 || !pdfDoc || page > pdfDoc.numPages) throw new Error('BW_READER_HIGHLIGHT_PAGE_INVALID');
  const readyPage = () => {
    const pw = document.querySelector('.page-wrap[data-page-num="' + page + '"]');
    return pw && pw.dataset.loaded === '1' && Array.isArray(pw.__charBoxes) && pw.__charBoxes.length
      ? pw : null;
  };
  // 精确高亮最常见的目标就是用户眼前这一页。旧实现无条件重新 goToPage，
  // 一旦 PDF 重渲染或原生文字层请求卡住，当前已经可用的文字层也被一起
  // 阻塞，Windows 最终只能得到回执超时。先消费现成页面，不做多余导航。
  const current = Number(currentPage) === page ? readyPage() : null;
  if (current) return current;
  let navigationError = null;
  try {
    Promise.resolve(window.goToPage(page)).catch((error) => { navigationError = error; });
  } catch (error) {
    navigationError = error;
  }
  // 不 await goToPage：页面是否真正可用由同一个 DOM/文字层条件判定；这样
  // 即使导航 Promise 本身失联，也会在有界时间内明确失败而不是永久处理中。
  for (let tries = 0; tries < 80; tries++) {
    const pw = readyPage();
    if (pw) return pw;
    if (navigationError) throw navigationError;
    await new Promise((resolve) => setTimeout(resolve, 120));
  }
  throw new Error('BW_READER_HIGHLIGHT_TEXT_LAYER_UNAVAILABLE');
}

async function _pdfWaitForHighlightVisible(pw, page, id) {
  for (let tries = 0; tries < 40; tries++) {
    renderHighlightsOnPage(pw, page);
    const rendered = Array.from(pw.querySelectorAll('.hl-saved')).find((node) =>
      node.dataset.id === id && parseFloat(node.style.width || '0') > 0 &&
      parseFloat(node.style.height || '0') > 0
    );
    if (rendered) return rendered;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error('BW_READER_HIGHLIGHT_NOT_RENDERED');
}

window.__bwReaderHighlightExactText = async function (request) {
  request = request || {};
  if (request.file !== FILE_REL) throw new Error('BW_READER_HIGHLIGHT_WRONG_BOOK');
  if (!request.target || request.target.kind !== 'pdf') throw new Error('BW_READER_HIGHLIGHT_TARGET_KIND');
  const colors = { yellow:'#fff59d', green:'#a7f3d0', blue:'#a3d4ff', pink:'#fda4af' };
  if (!colors[request.color]) throw new Error('BW_READER_HIGHLIGHT_COLOR_INVALID');
  if (!/^c_[a-f0-9]{8,32}$/.test(request.mutationId || '')) throw new Error('BW_READER_HIGHLIGHT_MUTATION_ID');
  const pw = await _pdfExactTextPage(request.target.page);
  const range = _pdfExactTextRange(pw.__charBoxes, request.text);
  const highlight = await saveHighlight({
    pw, sIdx: range.start, eIdx: range.end, color: colors[request.color],
    note: request.note || '', id: request.mutationId, silent: true
  });
  await _pdfWaitForHighlightVisible(
    pw, Number(request.target.page), highlight.id
  );
  return { ok: true, status: 'highlight_saved', id: highlight.id, page: Number(request.target.page), text: highlight.text };
};

window.__bwReaderValidateExactSource = async function (request) {
  request = request || {};
  if (request.file !== FILE_REL) throw new Error('BW_READER_SOURCE_WRONG_BOOK');
  if (!request.target || request.target.kind !== 'pdf') throw new Error('BW_READER_SOURCE_TARGET_KIND');
  const pw = await _pdfExactTextPage(request.target.page);
  const range = _pdfExactTextRange(pw.__charBoxes, request.sourceText);
  return { ok: true, page: Number(request.target.page), start: range.start, end: range.end };
};

// 当前激活颜色（互斥单选；持久化）。空 = 没激活 → 「🖌 标记」按钮禁用
let _activeHlColor = localStorage.getItem('pdf-hl-active') || '';

// 工具栏色板：只有 [○ ○ ○ ○]；点色 = 立刻标记 + 互斥激活外框（记最近用过的色）
function renderHlPicker() {
  const c = document.getElementById('hl-color-picker');
  if (!c) return;
  c.innerHTML = '';
  for (const col of getHlColors()) {
    const sw = document.createElement('div');
    sw.className = 'swatch' + (col === _activeHlColor ? ' active' : '');
    sw.style.background = col;
    sw.title = '用此色标记';
    sw.onclick = (e) => { e.stopPropagation(); onPickColor(col); };
    c.appendChild(sw);
  }
}
async function onPickColor(col) {
  // 互斥激活：再点同色 = 取消激活；点其他色 = 切到该色（同时立刻标记）
  if (col === _activeHlColor) {
    _activeHlColor = '';
    try { localStorage.setItem('pdf-hl-active', ''); } catch(_) {}
    renderHlPicker();
    return;
  }
  _activeHlColor = col;
  try { localStorage.setItem('pdf-hl-active', col); } catch(_) {}
  renderHlPicker();
  if (!_charSel) { _toast('已选定颜色（先选中文字）'); return; }
  await saveHighlight({
    pw: _charSel.pw, sIdx: _charSel.startIdx, eIdx: _charSel.endIdx,
    color: col, kind: 'note',
  });
  _charSel.pw.querySelector('.sel-overlay')?.replaceChildren();
  _charSel = null;
  lastSelText = '';
  _updateSelPreview('');
  toolbar.classList.remove('open');
  _toast('已标记 🖌');
}

// 从 result-modal「🖌 标记到 PDF」入口：基于 _resultContext 保存
async function markFromResult() {
  if (!_resultContext || !_resultContext.charSel) {
    alert('找不到原始 PDF 选中位置（先在 PDF 上选中再翻译/解释，再点这里）');
    return;
  }
  const md = document.getElementById('result-content');
  const clone = md.cloneNode(true);
  clone.querySelectorAll('.head-pick,.reply-pick-all-result,.pick-btn').forEach(b => b.remove());
  const body = (clone.dataset.raw || clone.textContent || '').trim();
  const cs = _resultContext.charSel;
  const useColor = _activeHlColor || _lastHlColor || (getHlColors()[0] || '#fff59d');
  const h = await saveHighlight({
    pw: cs.pw, sIdx: cs.startIdx, eIdx: cs.endIdx,
    color: useColor,
    kind: _resultContext.kind || 'note',
    sentence: _resultContext.sentence || '',
    body,
  });
  if (h) { closeResult(); _toast('已加标记 🖌'); }
}
window.markFromResult = markFromResult;

// 从 result-modal「🎴 制 Anki」：把「选中原文 + 上下文 + 这条解释」做成 Anki 卡（后台，复用进度条）
async function ankiFromResult() {
  const sel = (_resultContext && _resultContext.text) || lastSelText || '';
  const md = document.getElementById('result-content');
  const clone = md.cloneNode(true);
  clone.querySelectorAll('.head-pick,.reply-pick-all-result,.pick-btn').forEach(b => b.remove());
  const body = (clone.dataset.raw || clone.textContent || '').trim();
  if (!sel && !body) { _toast('没有可制卡的内容'); return; }
  const sentence = (_resultContext && _resultContext.sentence) || '';
  // 原句导航链接：卡片背面可点回到 PDF 原文页
  const text = `【原文】${sel}` + (sentence && sentence !== sel ? `\n【上下文】${sentence}` : '') + `\n【解释】${body}`;
  closeResult();
  const jobUi = _startBgJob('制 Anki 中…');
  try {
    const ov = _getAiOverrides();
    const r = await fetch('/pdf/api/snippets-to-async', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        file: FILE_REL || '',
        page: currentPage || 1,
        source: { kind: 'pdf', page: currentPage || 1 },
        snippets: [{text, source: sel || sentence}],
        make_note: false, make_anki: true, note_name: '',
        model: ov.model || '', effort: ov.effort || '',
      }),
    });
    const d = await r.json();
    if (!d.ok || !d.job_id) { _failBgJob(jobUi, d.error || '提交失败', null); return; }
    _pollJob(d.job_id, jobUi, null);
  } catch (e) { _failBgJob(jobUi, e.message, null); }
}
window.ankiFromResult = ankiFromResult;

// 解释结果框底部「追问」：基于已有内容继续问 AI，流式追加到同一框（多轮对话）
window._followupAsk = async () => {
  const inp = document.getElementById('result-followup-input');
  const q = (inp.value || '').trim();
  if (!q) return;
  inp.value = '';
  const contentEl = document.getElementById('result-content');
  const history = (contentEl.textContent || '').slice(0, 4000);
  const qDiv = document.createElement('div');
  qDiv.style.cssText = 'margin-top:12px;padding-top:10px;border-top:1px solid #2a3550;color:#a8cdff;font-size:12px;font-weight:600';
  qDiv.textContent = '问：' + q;
  contentEl.appendChild(qDiv);
  const aDiv = document.createElement('div');
  aDiv.style.cssText = 'margin-top:6px;color:#e6e6f0';
  aDiv.innerHTML = '<span class="loading">⏳</span>';
  contentEl.appendChild(aDiv);
  contentEl.scrollTop = contentEl.scrollHeight;
  const myReq = _resultReqId;
  try {
    const ov = _getAiOverrides();
    const render = (text) => {
      if (myReq !== _resultReqId) return;   // 结果框已被新内容作废
      aDiv.innerHTML = md(text || ' ');
      if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([aDiv]).catch(() => {});
      contentEl.scrollTop = contentEl.scrollHeight;
    };
    const res = await _aiStream('/pdf/api/explain', {
      method: 'POST', onText: render,
      body: {text: q, context: '基于之前的解释/对话继续回答：\n' + history, model: ov.model || '', effort: ov.effort || ''},
    });
    if (myReq !== _resultReqId) return;
    if (res.ok && res.text) render(res.text);
    else if (!res.ok) aDiv.innerHTML = '<span style="color:#c00">✗ ' + (res.error || '失败') + '</span>';
    else aDiv.innerHTML = '(无回答)';
  } catch (e) { aDiv.innerHTML = '<span style="color:#c00">✗ ' + e.message + '</span>'; }
  contentEl.scrollTop = contentEl.scrollHeight;
  try { addResultPickers(); } catch (_) {}   // 追问回答也加「+ 选段」，制 Anki(ankiFromResult)含全框选中
};
