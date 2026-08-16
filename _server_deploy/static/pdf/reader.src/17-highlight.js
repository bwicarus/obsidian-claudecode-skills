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
//
// 必须和蓝色实时选区用同一个块过滤。选区是一段**字符索引区间**,而索引顺序
// 不等于阅读顺序 —— 竖排漫画一页上,右侧气泡的字符索引可能正好夹在用户选中
// 那一段的首尾之间。蓝色 overlay 和 _charsRangeToText 都过 _charRangeBlockFilter
// 把这些别块字符挡掉,只有这里没过:于是用户看到蓝色框住的是一段,保存后黄色
// 高亮却连右边的对话一起涂了。
//
// 2026-08-16 用户实测报告:「第一张图的蓝色选中范围和高亮这一段后实际黄色
// 高亮的范围不同,右侧的对话也被高亮了」。
function _charsRangeToRects(chars, sIdx, eIdx) {
  if (sIdx > eIdx) [sIdx, eIdx] = [eIdx, sIdx];
  const _inBlk = _charRangeBlockFilter(chars, sIdx, eIdx);
  const rects = [];
  let cur = null;
  for (let i = sIdx; i <= eIdx; i++) {
    const c = chars[i];
    if (!_inBlk(c)) continue;   // 别块(另一个气泡/另一栏)不画高亮,与蓝色选区一致
    if (c.sp && (!c.width || c.width < 0.5)) {
      if (cur && c._x1 > cur.x1) cur.x1 = c._x1;
      continue;
    }
    const x0 = c._x0, y0 = c._y0, x1 = c._x1, y1 = c._y1;
    const lineH = y1 - y0;
    const _sameBk = cur && c.bk != null && cur.bk != null && c.bk >= 0 && c.bk === cur.bk;   // #56:同排版块内字符 top 会抖动(括号/标点),不能一抖就分段
    // 同块**不等于**同一视觉行。#56 原本让同块直接跳过换行判断,前提是
    // "一个块 = 一个视觉行"(OCR justified 的常见形态)。这个前提不普遍成立:
    // 实测《料理师》part1 第 27 页,块 30 一个块装了两行,于是两行被并成一个
    // 矩形 —— 用户看到的就是"选到半行,高亮却是一整个方块"。
    //
    // 所以同块时放宽的应该是 top 抖动的**容差**,而不是取消判断本身:仍要求
    // 两者在垂直方向实质重叠。同一行的字互相盖住大半,下一行的字几乎不盖,
    // 这个量能同时容忍括号抖动、又挡住换行。
    const _vOverlap = cur
      ? Math.max(0, Math.min(y1, cur.y1) - Math.max(y0, cur.y0)) /
        Math.max(1, Math.min(y1 - y0, cur.y1 - cur.y0))
      : 0;
    const _sameLine = !!cur && (
      (_sameBk && _vOverlap >= 0.35) ||
      Math.abs(y0 - cur.y0) <= Math.max(2, Math.max(cur.y1 - cur.y0, y1 - y0) * 0.6)   // 跨块按 y0 判行(跨行 y0 差>字高分段)
    );
    if (cur && _sameLine &&
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
  let text = '', boundaries = [0], last = null;
  const cjk = (value) => /[぀-ヿ㐀-鿿　-〿＀-￯]/.test(value || '');
  // Keep projected *boundaries* rather than mapping every displayed code unit
  // to one native character. Synthetic layout spaces occupy no PDF character:
  // both of their boundaries point at the next native index. This prevents a
  // marker ending after a row/gap space from accidentally including the first
  // character of the next word.
  const append = (value, startIndex, endIndex) => {
    for (let k = 0; k < value.length; k++) {
      text += value[k];
      boundaries.push(k === value.length - 1 ? endIndex : startIndex);
    }
  };
  for (let i = 0; i < (chars || []).length; i++) {
    const ch = chars[i] || {};
    if (last) {
      const cjkPair = cjk(ch.c) && cjk(last.c);
      const dy = Math.abs(Number(ch.top || 0) - Number(last.top || 0));
      if (dy > Number(ch.height || 0) * 0.5) {
        if (!cjkPair) append(' ', i, i);
      } else {
        const gap = Number(ch.left || 0) - (Number(last.left || 0) + Number(last.width || 0));
        const ref = Math.min(Number(ch.height || 0), Number(last.height || 0));
        if (!cjkPair && gap > ref * ((/[A-Za-z]/.test(ch.c || '') && /[A-Za-z]/.test(last.c || '')) ? 1.3 : 0.6) && !last.sp && !ch.sp) append(' ', i, i);
      }
    }
    append(ch.sp ? ' ' : String(ch.c || ''), i, i + 1);
    last = ch;
  }
  let first = 0, lastText = text.length;
  while (first < lastText && /\s/.test(text[first])) first += 1;
  while (lastText > first && /\s/.test(text[lastText - 1])) lastText -= 1;
  let folded = '', foldedBoundaries = [];
  for (let i = first; i < lastText;) {
    if (/\s/.test(text[i])) {
      let end = i + 1;
      while (end < lastText && /\s/.test(text[end])) end += 1;
      if (!foldedBoundaries.length) foldedBoundaries.push(boundaries[i]);
      folded += ' ';
      foldedBoundaries.push(boundaries[end]);
      i = end;
      continue;
    }
    if (!foldedBoundaries.length) foldedBoundaries.push(boundaries[i]);
    folded += text[i];
    foldedBoundaries.push(boundaries[i + 1]);
    i += 1;
  }
  return { text: folded, boundaries: foldedBoundaries };
}

// ── reader-highlight-source/1 ──────────────────────────────────────────────
//
// Assistant highlights must not locate a returned quote with indexOf().  The
// text shown to the assistant and the PDF geometry must come from the same
// authoritative __charBoxes projection.  A short-lived snapshot exposes only
// opaque boundary markers; their real projected/native offsets stay in this
// page realm.  Nothing is inserted into the PDF text layer or DOM.
const _READER_HIGHLIGHT_SOURCE_CONTRACT = 'reader-highlight-source/1';
const _READER_SOURCE_RANGE_CONTRACT = 'reader-source-range/1';
const _READER_SOURCE_TTL_MS = 5 * 60 * 1000;
const _READER_SOURCE_MAX_TEXT = 16384;
const _READER_SOURCE_MAX_MARKERS = 2048;
const _pdfReaderSourceSnapshots = new Map();
let _pdfReaderSourceFallbackNonce = 0;

function _readerSourceDigest(value) {
  const text = String(value || '');
  let a = 0x811c9dc5, b = 0x9e3779b9;
  for (let i = 0; i < text.length; i++) {
    const code = text.charCodeAt(i);
    a ^= code; a = Math.imul(a, 0x01000193) >>> 0;
    b ^= code + ((i + 1) * 0x45d9f3b); b = Math.imul(b, 0x27d4eb2d) >>> 0;
  }
  return 'rsd1_' + text.length.toString(16).padStart(8, '0') + '_' +
    a.toString(16).padStart(8, '0') + b.toString(16).padStart(8, '0');
}

function _readerSourceSnapshotId() {
  try {
    const bytes = new Uint8Array(12);
    window.crypto.getRandomValues(bytes);
    return 'hrs_' + Array.from(bytes, (v) => v.toString(16).padStart(2, '0')).join('');
  } catch (_) {
    _pdfReaderSourceFallbackNonce += 1;
    const seed = Date.now().toString(16).padStart(12, '0') + ':' +
      _pdfReaderSourceFallbackNonce + ':' + Math.random();
    return 'hrs_' + _readerSourceDigest(seed).slice(-16) +
      (Date.now() >>> 0).toString(16).padStart(8, '0');
  }
}

function _readerSourcePieces(text) {
  text = String(text || '');
  if (!text) throw new Error('BW_READER_SOURCE_EMPTY');
  if (text.length > _READER_SOURCE_MAX_TEXT) throw new Error('BW_READER_SOURCE_TOO_LARGE');
  const pieces = [];
  // Modern WebKit ships Unicode word segmentation. It gives Japanese word
  // boundaries without shipping another tokenizer and keeps the model-facing
  // marker list much smaller than one object per CJK character. The opaque
  // range remains authoritative even when an older host uses the fallback.
  try {
    if (typeof Intl !== 'undefined' && typeof Intl.Segmenter === 'function') {
      const segments = new Intl.Segmenter(undefined, { granularity: 'word' }).segment(text);
      for (const item of segments) {
        const segment = String(item && item.segment || '');
        for (let i = 0; i < segment.length;) {
          let end = Math.min(segment.length, i + 512);
          if (end < segment.length && /[\uD800-\uDBFF]/.test(segment[end - 1]) &&
              /[\uDC00-\uDFFF]/.test(segment[end])) end += 1;
          pieces.push(segment.slice(i, end));
          i = end;
        }
      }
    }
  } catch (_) { pieces.length = 0; }
  if (!pieces.length || pieces.join('') !== text) {
    pieces.length = 0;
    for (let i = 0; i < text.length;) {
      const cp = text.codePointAt(i);
      const unit = String.fromCodePoint(cp);
      const asciiWord = /[A-Za-z0-9_'\u2019\-]/.test(unit);
      const whitespace = /\s/.test(unit);
      let end = i + unit.length;
      if (asciiWord || whitespace) {
        while (end < text.length && end - i < 512) {
          const next = String.fromCodePoint(text.codePointAt(end));
          if ((asciiWord && !/[A-Za-z0-9_'\u2019\-]/.test(next)) ||
              (whitespace && !/\s/.test(next))) break;
          end += next.length;
        }
      }
      pieces.push(text.slice(i, end));
      i = end;
    }
  }
  // Dense CJK pages can otherwise spend most of the 128 KiB snapshot budget on
  // marker objects.  Merge adjacent natural pieces only when necessary; never
  // truncate source text and never expose the resulting offsets.
  if (pieces.length + 1 > _READER_SOURCE_MAX_MARKERS) {
    const compact = [];
    const chunkSize = Math.ceil(text.length / (_READER_SOURCE_MAX_MARKERS - 1));
    for (let i = 0; i < text.length;) {
      let end = Math.min(text.length, i + chunkSize);
      if (end < text.length && /[\uD800-\uDBFF]/.test(text[end - 1]) &&
          /[\uDC00-\uDFFF]/.test(text[end])) end += 1;
      compact.push(text.slice(i, end));
      i = end;
    }
    return compact;
  }
  return pieces;
}

function _readerSourceMarkerBundle(text) {
  const pieces = _readerSourcePieces(text);
  const markers = [], offsets = Object.create(null);
  let offset = 0;
  for (let i = 0; i < pieces.length; i++) {
    const marker = 'm_' + i.toString(36);
    offsets[marker] = offset;
    markers.push({ marker, text: pieces[i] });
    offset += pieces[i].length;
  }
  const terminal = 'm_' + pieces.length.toString(36);
  offsets[terminal] = offset;
  markers.push({ marker: terminal, text: '' });
  return { markers, offsets };
}

function _readerSourceRemember(cache, snapshot) {
  const now = Date.now();
  for (const [id, value] of cache) {
    if (!value || value.expiresAt <= now) cache.delete(id);
  }
  while (cache.size >= 12) cache.delete(cache.keys().next().value);
  cache.set(snapshot.snapshotId, snapshot);
}

function _readerSourceExisting(cache, documentId, target, sourceDigest, revision, text) {
  const now = Date.now();
  for (const [id, value] of cache) {
    if (!value || value.expiresAt <= now) { cache.delete(id); continue; }
    // Do not hand a model an identity that can expire while it is choosing the
    // two markers. Mint the next snapshot before the final 30-second window.
    if (value.expiresAt - now <= 30000) { cache.delete(id); continue; }
    if (value.documentId === documentId &&
        value.target && value.target.kind === target.kind &&
        Number(value.target.page) === Number(target.page) &&
        value.sourceDigest === sourceDigest && value.revision === revision &&
        value.text === text) return value;
  }
  return null;
}

function _readerSourceMutationId(request, ref) {
  if (request.mutationId != null && request.mutationId !== '') {
    if (!/^c_[a-f0-9]{8,32}$/.test(request.mutationId)) {
      throw new Error('BW_READER_HIGHLIGHT_MUTATION_ID');
    }
    return request.mutationId;
  }
  return 'c_' + _readerSourceDigest(
    ref.snapshotId + ':' + ref.startMarker + ':' + ref.endMarker
  ).slice(-16);
}

function _pdfSourceRevision(pw, page, digest) {
  const nativeRevision = String((pw && pw.__pageTextRevision) || '');
  return nativeRevision
    ? ('pdfrev_' + _readerSourceDigest(nativeRevision).slice(-16))
    : ('pdf_' + page + '_' + digest);
}

function _pdfRangeRef(request) {
  const ref = request && request.rangeRef;
  const refKeys = ref && typeof ref === 'object' ? Object.keys(ref).sort() : [];
  const exactRefKeys = [
    'contract', 'documentId', 'endMarker', 'revision', 'snapshotId',
    'sourceDigest', 'startMarker', 'target'
  ];
  if (!ref || ref.contract !== _READER_SOURCE_RANGE_CONTRACT ||
      refKeys.length !== exactRefKeys.length ||
      refKeys.some((key, index) => key !== exactRefKeys[index])) {
    throw new Error('BW_READER_RANGE_CONTRACT_INVALID');
  }
  if (!/^hrs_[0-9a-f]{24}$/.test(String(ref.snapshotId || ''))) {
    throw new Error('BW_READER_RANGE_SNAPSHOT_STALE');
  }
  const snapshot = _pdfReaderSourceSnapshots.get(ref.snapshotId);
  if (!snapshot || snapshot.expiresAt <= Date.now()) {
    _pdfReaderSourceSnapshots.delete(ref.snapshotId);
    throw new Error('BW_READER_RANGE_SNAPSHOT_STALE');
  }
  const targetKeys = ref.target && typeof ref.target === 'object'
    ? Object.keys(ref.target).sort() : [];
  if (ref.documentId !== FILE_REL || snapshot.documentId !== FILE_REL ||
      !ref.target || ref.target.kind !== 'pdf' ||
      targetKeys.length !== 2 || targetKeys[0] !== 'kind' || targetKeys[1] !== 'page' ||
      Number(ref.target.page) !== snapshot.target.page) {
    throw new Error('BW_READER_RANGE_SOURCE_STALE');
  }
  if (ref.sourceDigest !== snapshot.sourceDigest || ref.revision !== snapshot.revision) {
    throw new Error('BW_READER_RANGE_SOURCE_STALE');
  }
  const start = snapshot.offsets[String(ref.startMarker || '')];
  const end = snapshot.offsets[String(ref.endMarker || '')];
  if (!Number.isInteger(start) || !Number.isInteger(end)) {
    throw new Error('BW_READER_RANGE_MARKER_INVALID');
  }
  if (start < 0 || end > snapshot.text.length || start >= end) {
    throw new Error('BW_READER_RANGE_INVALID');
  }
  return { ref, snapshot, start, end };
}

function _pdfExactTextRange(chars, sourceText) {
  const projected = _pdfExactTextProjection(chars);
  const query = String(sourceText || '').replace(/\s+/g, ' ').trim();
  if (!query) throw new Error('BW_READER_HIGHLIGHT_TEXT_EMPTY');
  const first = projected.text.indexOf(query);
  if (first < 0) throw new Error('BW_READER_HIGHLIGHT_TEXT_NOT_FOUND');
  if (projected.text.indexOf(query, first + 1) >= 0) throw new Error('BW_READER_HIGHLIGHT_TEXT_AMBIGUOUS');
  const start = projected.boundaries[first];
  const endExclusive = projected.boundaries[first + query.length];
  if (!Number.isInteger(start) || !Number.isInteger(endExclusive) || start >= endExclusive) {
    throw new Error('BW_READER_HIGHLIGHT_TEXT_RANGE_INVALID');
  }
  return { start, end: endExclusive - 1 };
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

window.__bwReaderHighlightSource = async function (request) {
  request = request || {};
  if (request.file !== FILE_REL) throw new Error('BW_READER_SOURCE_WRONG_BOOK');
  if (!request.target || request.target.kind !== 'pdf') throw new Error('BW_READER_SOURCE_TARGET_KIND');
  const page = Number(request.target.page);
  const pw = await _pdfExactTextPage(page);
  const projected = _pdfExactTextProjection(pw.__charBoxes);
  if (!projected.text) throw new Error('BW_READER_SOURCE_EMPTY');
  const sourceDigest = _readerSourceDigest(projected.text);
  const revision = _pdfSourceRevision(pw, page, sourceDigest);
  const existing = _readerSourceExisting(
    _pdfReaderSourceSnapshots, FILE_REL, { kind: 'pdf', page }, sourceDigest, revision,
    projected.text
  );
  if (existing) {
    return {
      contract: _READER_HIGHLIGHT_SOURCE_CONTRACT,
      snapshotId: existing.snapshotId,
      documentId: existing.documentId,
      target: { kind: 'pdf', page },
      sourceDigest: existing.sourceDigest,
      revision: existing.revision,
      expiresAt: existing.expiresAt,
      markers: existing.markers.map((item) => ({ marker: item.marker, text: item.text }))
    };
  }
  const snapshotId = _readerSourceSnapshotId();
  const expiresAt = Date.now() + _READER_SOURCE_TTL_MS;
  const bundle = _readerSourceMarkerBundle(projected.text);
  _readerSourceRemember(_pdfReaderSourceSnapshots, {
    snapshotId,
    documentId: FILE_REL,
    target: { kind: 'pdf', page },
    sourceDigest,
    revision,
    expiresAt,
    text: projected.text,
    offsets: bundle.offsets,
    markers: bundle.markers.map((item) => ({ marker: item.marker, text: item.text }))
  });
  return {
    contract: _READER_HIGHLIGHT_SOURCE_CONTRACT,
    snapshotId,
    documentId: FILE_REL,
    target: { kind: 'pdf', page },
    sourceDigest,
    revision,
    expiresAt,
    markers: bundle.markers.map((item) => ({ marker: item.marker, text: item.text }))
  };
};

window.__bwReaderHighlightRange = async function (request) {
  request = request || {};
  const colors = { yellow:'#fff59d', green:'#a7f3d0', blue:'#a3d4ff', pink:'#fda4af' };
  if (!colors[request.color]) throw new Error('BW_READER_HIGHLIGHT_COLOR_INVALID');
  const resolved = _pdfRangeRef(request);
  const page = resolved.snapshot.target.page;
  const pw = await _pdfExactTextPage(page);
  const projected = _pdfExactTextProjection(pw.__charBoxes);
  const sourceDigest = _readerSourceDigest(projected.text);
  const revision = _pdfSourceRevision(pw, page, sourceDigest);
  if (sourceDigest !== resolved.snapshot.sourceDigest ||
      revision !== resolved.snapshot.revision ||
      projected.text !== resolved.snapshot.text) {
    throw new Error('BW_READER_RANGE_SOURCE_STALE');
  }
  const startIndex = projected.boundaries[resolved.start];
  const endExclusive = projected.boundaries[resolved.end];
  if (!Number.isInteger(startIndex) || !Number.isInteger(endExclusive) || startIndex >= endExclusive) {
    throw new Error('BW_READER_RANGE_INVALID');
  }
  const mutationId = _readerSourceMutationId(request, resolved.ref);
  const highlight = await saveHighlight({
    pw,
    sIdx: startIndex,
    eIdx: endExclusive - 1,
    color: colors[request.color],
    note: request.note || '',
    id: mutationId,
    silent: true
  });
  if (!highlight || !highlight.id) throw new Error('BW_READER_HIGHLIGHT_SAVE_INVALID');
  await _pdfWaitForHighlightVisible(pw, page, highlight.id);
  return {
    ok: true,
    status: 'highlight_saved',
    id: highlight.id,
    target: { kind: 'pdf', page },
    text: highlight.text
  };
};

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
