// ──────── 整页翻译（F3）：逐句就地白底中文覆盖 ────────
let _pageTrOn = false;
function _clearPageTranslate(pw) { pw.querySelector('.page-tr-layer')?.remove(); }
// rects[[x0,y0,x1,y1]...] → 按垂直中心聚类合并成「每视觉行一个 rect」(同 _drawSentenceOverlay)
function _mergeLines(rects) {
  const raw = rects.filter(r => (r[2] - r[0]) > 0.5 && (r[3] - r[1]) > 0.5);
  if (!raw.length) return [];
  const refH = Math.max.apply(null, raw.map(r => r[3] - r[1])) || 1;
  const cy = r => (r[1] + r[3]) / 2;
  const sorted = raw.slice().sort((a, b) => (cy(a) - cy(b)) || (a[0] - b[0]));
  const lines = [];
  for (const r of sorted) {
    const last = lines[lines.length - 1];
    if (last && Math.abs(cy(r) - cy(last)) <= refH * 0.5) {
      last[0] = Math.min(last[0], r[0]); last[1] = Math.min(last[1], r[1]);
      last[2] = Math.max(last[2], r[2]); last[3] = Math.max(last[3], r[3]);
    } else lines.push([r[0], r[1], r[2], r[3]]);
  }
  lines.sort((a, b) => a[1] - b[1]);
  return lines;
}
// 译文切片：把每句译文按其视觉行切开，算出每片的位置与字号 —— **一律用点坐标**。
// 原生正文那侧要画同样的东西，切分/分配/字号这套判据只该有一处；它按自己的缩放
// 乘一次即可（点坐标是页面的固有尺寸，跟谁在画无关）。
//
// 行间对照翻译:不遮原文。每句译文按其各行分配,每行片段放到该行**字框顶部留白**里(完全照搬
// 振假名定位:top=y0-fs*0.34,字号≈行高×0.4)。char bbox 含上下留白(字形只占中段),所以这块
// 留白视觉上是空的,小字落进去基本不压字形,也是振假名能和密排正文共存的原因。注音与译页互斥。
function _pageTranslateSlices(sentences) {
  const out = [];
  for (const s of (sentences || [])) {
    const zh = (s.zh || '').trim(); if (!zh) continue;
    const raw = (s.rects || []).filter(r => (r[2] - r[0]) > 1 && (r[3] - r[1]) > 1);
    if (!raw.length) continue;
    const lines = _mergeLines(raw);
    const cps = Array.from(zh); const N = cps.length; let idx = 0;
    lines.forEach((r, i) => {
      const w = r[2] - r[0], h = r[3] - r[1];
      const annFs = Math.max(7, h * 0.40);                 // 行间小字档(≈原文行高×0.4,略大于振假名 0.36)
      const cap = Math.max(1, Math.floor(w / annFs));      // 该行能放几个汉字(分配用)
      const n = (i === lines.length - 1) ? (N - idx) : Math.max(0, Math.min(cap, N - idx));
      const slice = cps.slice(idx, idx + n).join(''); idx += n;
      if (!slice) return;
      const fs = Math.max(7, Math.min(annFs, w / slice.length));   // 不超小字档 + 塞进行宽单行不外溢
      out.push({ x: r[0], y: r[1] - fs * 0.34, w: w, fontSize: fs, text: slice });
    });
  }
  return out;
}
function _drawPageTranslate(pw, sentences) {
  _clearPageTranslate(pw);
  const canvas = pw.querySelector('canvas');
  const cssW = canvas?.clientWidth || pw.clientWidth;
  const cssH = canvas?.clientHeight || pw.clientHeight;
  const pageWPt = pw.__pageWPt || cssW, pageHPt = pw.__pageHPt || cssH;
  if (!cssW || !cssH || !pageWPt || !pageHPt) return;
  const sx = cssW / pageWPt, sy = cssH / pageHPt;
  const layer = document.createElement('div');
  layer.className = 'page-tr-layer';
  pw.appendChild(layer);
  for (const sl of _pageTranslateSlices(sentences)) {
    const rt = document.createElement('div');
    rt.className = 'page-tr-rt';
    rt.style.left = (sl.x * sx) + 'px';
    rt.style.width = (sl.w * sx) + 'px';
    rt.style.top = (sl.y * sy) + 'px';
    rt.style.fontSize = (sl.fontSize * sy).toFixed(1) + 'px';
    rt.textContent = sl.text;
    layer.appendChild(rt);
  }
}
// 原生正文那侧取这一页的译文切片。开关状态、端点、切分全走这里一处；关着就回空。
// ⚠ 不做「已翻过就跳过」那种去重：那是 DOM 路径靠 pw.__pageTrSeq 记在元素上的，
// 原生每次重取叠加数据都要拿到完整结果，跳过等于第二次翻到这页就空白。
window.__bwReaderPageTranslateSlices = async function (page) {
  page = Number(page) || 0;
  if (!_pageTrOn || !page) return [];
  try {
    // @interaction document.page-translate.read
    const r = await fetch('/pdf/api/page-translate?file=' + encodeURIComponent(FILE_REL || '') +
      '&page=' + page);
    const d = await r.json();
    if (!_pageTrOn || !d || d.ok !== true) return [];
    return _pageTranslateSlices(d.sentences || []).slice(0, 400);
  } catch (e) {
    window.dlog?.('page-tr p.' + page + ' fail: ' + e.message, '#ff453a');
    return [];
  }
};
window.__bwReaderPageTranslateOn = () => _pageTrOn;
async function _pageTranslatePage(pw) {
  if (!_pageTrOn || !pw) return;
  const num = parseInt(pw.dataset.pageNum || '0', 10); if (!num) return;
  if (pw.__pageTrSeq === num) return;   // 该页已翻过（防重复请求）
  pw.__pageTrSeq = num;
  try {
    const r = await fetch('/pdf/api/page-translate?file=' + encodeURIComponent(FILE_REL) + '&page=' + num);
    const d = await r.json();
    if (!_pageTrOn) return;             // 请求途中被关掉
    if (d.ok && d.sentences) _drawPageTranslate(pw, d.sentences);
  } catch (e) {
    window.dlog?.('page-tr p.' + num + ' fail: ' + e.message, '#ff453a');
    pw.__pageTrSeq = null;             // 失败允许重试
  }
}
function _pageTranslateApplyAll() {
  document.querySelectorAll('[data-loaded="1"][data-page-num]').forEach(pw => _pageTranslatePage(pw));
}
window.togglePageTranslate = () => {
  _pageTrOn = !_pageTrOn;
  const b = document.getElementById('pagetr-toggle');
  if (b) b.classList.toggle('active', _pageTrOn);
  if (_pageTrOn) {
    if (_rubyEnabled()) {   // 互斥:开译页 → 关注音(都占行上方空隙)
      try { localStorage.setItem('pdf-ruby', '0'); } catch (_) {}
      document.getElementById('ruby-toggle')?.classList.remove('active');
      document.querySelectorAll('.ruby-layer').forEach(el => el.remove());
    }
    _toast?.('整页翻译开启，翻译中…');
    _pageTranslateApplyAll();
  } else {
    document.querySelectorAll('.page-tr-layer').forEach(el => el.remove());
    document.querySelectorAll('[data-page-num]').forEach(pw => { pw.__pageTrSeq = null; });
  }
};

