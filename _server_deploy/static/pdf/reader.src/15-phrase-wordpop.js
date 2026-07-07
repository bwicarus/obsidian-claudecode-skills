// ──────── F6 词组：收藏（作分词依据）+ 词组详情面板 ────────
let _phraseFavSet = new Set();
let _phraseMarkSet = new Set();   // 已掌握词组(归一化键):标掌握后不再画生词下划线
const _phraseNorm = s => String(s || '').trim().replace(/\s+/g, ' ').toLowerCase();
async function _loadPhraseFavs() {
  try { const d = await (await fetch('/pdf/api/phrases')).json(); if (d.ok) _phraseFavSet = new Set(d.phrases || []); } catch (_) {}
  try { const d = await (await fetch('/pdf/api/phrase-mark')).json(); if (d.ok) _phraseMarkSet = new Set(d.mastered || []); } catch (_) {}
}
window.onPhrase = () => {
  const t = (lastSelText || '').trim();
  if (t) showPhrasePopover(t);
};
// 词组查询期间的呼吸高亮：**只在点「词组」按钮、查询进行中**出现（showPhrasePopover 开始时建、
// 结果出来即移除）。状态驱动(存 pt 坐标到 _activePhraseHl)→ 查询那 1-2s 内即便发生重渲染也不丢、
// 持续呼吸；不受点别处影响(独立层)。平时选中=普通选中(点别处照常消失)，这里不参与。
let _phraseHlTimer = null;
let _activePhraseHl = null;   // {page, text, rects:[[x0,y0,x1,y1]pt...]}
function _charRangeToPtRects(chars, s, e) {
  if (s > e) { const t = s; s = e; e = t; }
  // 块过滤:跟选中预览(_selByCharRange)/句子构造(_buildSentenceFromSel)严格一致——排序后选区首尾之间
  // 会交错进别气泡/别栏的字,不过滤就把别行的字也框进来(表现:选第2行却高亮第1、3行)。只取起止块区间内的字。
  const _blk = (c) => (c.bk != null && c.bk >= 0) ? c.bk : ((c.w == null || c.w < 0) ? -1 : Math.floor(c.w / 1000000));
  const sb = _blk(chars[s]), eb = _blk(chars[e]);
  const bLo = Math.min(sb, eb), bHi = Math.max(sb, eb);
  const inBlk = (c) => { if (sb < 0 || eb < 0) return true; const b = _blk(c); return b < 0 || (b >= bLo && b <= bHi); };
  const rects = []; let cur = null;
  for (let i = s; i <= e && i < chars.length; i++) {
    const c = chars[i];
    if (c.sp || c._x0 == null) continue;
    if (!inBlk(c)) continue;   // 别块字符不框,跟预览一致
    const lh = (c._y1 - c._y0) || 1;
    if (cur && Math.abs(c._y0 - cur[1]) <= lh * 0.6) {
      cur[2] = Math.max(cur[2], c._x1); cur[1] = Math.min(cur[1], c._y0); cur[3] = Math.max(cur[3], c._y1);
    } else { if (cur) rects.push(cur); cur = [c._x0, c._y0, c._x1, c._y1]; }
  }
  if (cur) rects.push(cur);
  return rects;
}
function renderPhraseHl(pw) {
  pw.querySelector('.phrase-hl-layer')?.remove();
  const a = _activePhraseHl;
  if (!a || !a.rects || !a.rects.length) return;
  if (parseInt(pw.dataset.pageNum || '0', 10) !== a.page) return;
  const canvas = pw.querySelector('canvas');
  const cssW = canvas?.clientWidth || pw.clientWidth, cssH = canvas?.clientHeight || pw.clientHeight;
  const pageWPt = pw.__pageWPt || cssW, pageHPt = pw.__pageHPt || cssH;
  if (!cssW || !cssH || !pageWPt || !pageHPt) return;
  const sx = cssW / pageWPt, sy = cssH / pageHPt;
  const layer = document.createElement('div');
  layer.className = 'phrase-hl-layer' + (a.solid ? '' : ' breathe');   // 查询中呼吸；出结果转常亮(a.solid)保持
  for (const r of a.rects) {
    const d = document.createElement('div'); d.className = 'hl';
    d.style.left = (r[0] * sx) + 'px'; d.style.top = (r[1] * sy) + 'px';
    d.style.width = ((r[2] - r[0]) * sx) + 'px'; d.style.height = ((r[3] - r[1]) * sy) + 'px';
    layer.appendChild(d);
  }
  // 点高亮：高亮消失 + 重新弹出该词组的翻译小框（不再建新高亮）
  layer.addEventListener('click', (e) => {
    e.stopPropagation();
    const txt = (_activePhraseHl && _activePhraseHl.text) || a.text;
    // 用高亮自身的屏幕矩形做锚点:重弹时原选区 .sel-overlay 早已清空 → _rectFromSel 退化成零尺寸页顶矩形,
    //   页面下滚后页顶为负 → 弹框定位到视口上方屏外 = 「点了不弹」。用高亮 rect 就锚在用户点的位置。
    let anchorRect = null;
    try {
      let L = Infinity, T = Infinity, R = -Infinity, B = -Infinity;
      layer.querySelectorAll('.hl').forEach(h => { const r = h.getBoundingClientRect(); if (r.width || r.height) { L = Math.min(L, r.left); T = Math.min(T, r.top); R = Math.max(R, r.right); B = Math.max(B, r.bottom); } });
      if (L < Infinity) anchorRect = { left: L, top: T, right: R, bottom: B, width: R - L, height: B - T };
    } catch (_) {}
    _removePhraseHighlight();
    showPhrasePopover(txt, {noHighlight: true, rect: anchorRect});
  });
  pw.appendChild(layer);
}
function _removePhraseHighlight() {
  _activePhraseHl = null;
  clearTimeout(_phraseHlTimer);
  document.querySelectorAll('.phrase-hl-layer').forEach(l => l.remove());
}
function _showPhraseHighlight(pw) {
  if (!pw || !_charSel || !pw.__charBoxes) return null;
  const text = (lastSelText || '').trim();
  if (!text) return null;
  const rects = _charRangeToPtRects(pw.__charBoxes, _charSel.startIdx, _charSel.endIdx);
  if (!rects.length) return null;
  document.querySelectorAll('.phrase-hl-layer').forEach(l => l.remove());   // 清掉别页残留的旧高亮
  _activePhraseHl = {page: parseInt(pw.dataset.pageNum || '0', 10) || currentPage, text, rects, solid: false};
  const sel = pw.querySelector('.sel-overlay'); if (sel) sel.innerHTML = '';   // 移交持久层，避免双重高亮
  renderPhraseHl(pw);
  return true;
}
// ──────── 解释高亮（与词组高亮平行：独立琥珀色，可与词组高亮共存）────────
// 设计:点「解释」**不开面板**,只在选区建一个**一直闪烁**的高亮,AI 在后台跑;
// 点高亮 → 打开解释页面(就绪=显缓存,未就绪=显加载中由后台填充) + **一次点击即移除高亮**。
// 单 active:再开新解释把旧 job 标 canceled 并替换高亮。
let _activeExplainHl = null;   // {page,text,rects,html,ready,canceled,panelReqId,title,src,resultContext}
function renderExplainHl(pw) {
  pw.querySelector('.explain-hl-layer')?.remove();
  const a = _activeExplainHl;
  if (!a || !a.rects || !a.rects.length) return;
  if (parseInt(pw.dataset.pageNum || '0', 10) !== a.page) return;
  const canvas = pw.querySelector('canvas');
  const cssW = canvas?.clientWidth || pw.clientWidth, cssH = canvas?.clientHeight || pw.clientHeight;
  const pageWPt = pw.__pageWPt || cssW, pageHPt = pw.__pageHPt || cssH;
  if (!cssW || !cssH || !pageWPt || !pageHPt) return;
  const sx = cssW / pageWPt, sy = cssH / pageHPt;
  const layer = document.createElement('div');
  layer.className = 'explain-hl-layer breathe';   // 一直闪烁,提示"点我看解释"
  for (const r of a.rects) {
    const d = document.createElement('div'); d.className = 'hl';
    d.style.left = (r[0] * sx) + 'px'; d.style.top = (r[1] * sy) + 'px';
    d.style.width = ((r[2] - r[0]) * sx) + 'px'; d.style.height = ((r[3] - r[1]) * sy) + 'px';
    layer.appendChild(d);
  }
  layer.addEventListener('click', (e) => { e.stopPropagation(); _reopenExplain(); });
  pw.appendChild(layer);
}
function _removeExplainHighlight() {
  _activeExplainHl = null;
  document.querySelectorAll('.explain-hl-layer').forEach(l => l.remove());
}
function _showExplainHighlight(pw, text) {
  if (!pw || !_charSel || !pw.__charBoxes) return null;
  const t = (text || lastSelText || '').trim();
  if (!t) return null;
  const rects = _charRangeToPtRects(pw.__charBoxes, _charSel.startIdx, _charSel.endIdx);
  if (!rects.length) return null;
  if (_activeExplainHl) _activeExplainHl.canceled = true;   // 旧 job 作废(结果丢弃,不再填面板)
  document.querySelectorAll('.explain-hl-layer').forEach(l => l.remove());
  _activeExplainHl = {
    page: parseInt(pw.dataset.pageNum || '0', 10) || currentPage,
    text: t, rects, html: null, ready: false, canceled: false, panelReqId: null,
    title: '💡 AI 解释', src: t, resultContext: null,
  };
  const sel = pw.querySelector('.sel-overlay'); if (sel) sel.innerHTML = '';   // 移交持久层,避免双重高亮
  renderExplainHl(pw);
  return _activeExplainHl;
}
function _reopenExplain() {
  const a = _activeExplainHl;
  if (!a) return;
  // 共享模式(__uiShared):结果框改走 rc-result,须用它暴露的 openResult/addResultPickers/_resultReqId
  // (window.openResult 已是 rc-result 版本;window.addResultPickers 需 rc-result 显式导出),否则本文件
  // 模块作用域内的裸 openResult/addResultPickers/_resultReqId 是 reader.js 自己另一份计数器/实现,
  // 两边互不作废对方的在途请求,会有极小概率的"旧结果覆盖新结果"竞态。非共享模式逐字不变。
  const shared = window.__uiShared && window.PdfAdapter;
  const _open = shared ? window.openResult : openResult;
  const _pickers = shared ? window.addResultPickers : addResultPickers;
  if (a.html) {
    // 已就绪 → 直接显缓存
    _open(a.title || '💡 AI 解释', a.src || a.text, a.html);
    try { if (a.resultContext) _resultContext = a.resultContext; } catch (_) {}
    try { _pickers && _pickers(); } catch (_) {}
  } else {
    // 还在后台跑 → 开加载面板,登记 reqId,完成时由 _runExplainBg 填充(并补 pickers)
    _open(a.title || '💡 AI 解释', a.src || a.text, '<div class="loading">⏳ AI 处理中…</div>');
    a.panelReqId = shared ? window._resultReqId : _resultReqId;   // openResult 已自增对应计数器,这里取新值
  }
  // 点高亮=打开页面 → 一次点击即移除高亮(后台 job 仍持自身引用,未 canceled 时继续填面板)
  document.querySelectorAll('.explain-hl-layer').forEach(l => l.remove());
  _activeExplainHl = null;
}
window.showPhrasePopover = async (text, opts) => {
  if (window.__uiShared && window.PdfAdapter) {   // 阶段2:词组 → rc-phrasepop(共享模式;else 走 _showPhrasePopoverNative 原逻辑,逐字不变)
    toolbar.classList.remove('open');
    PdfAdapter.lookupPhrase({ text, noHighlight: opts && opts.noHighlight, anchorRect: opts && opts.rect, fallback: () => _showPhrasePopoverNative(text, opts) });
    return;
  }
  return _showPhrasePopoverNative(text, opts);
};
async function _showPhrasePopoverNative(text, opts) {
  const pop = document.getElementById('word-pop');
  toolbar.classList.remove('open');
  _wordPopState = {word: text, ctx: '', lemma: text, phrase: true, reading: '', jp: false,
                   mastered: _phraseMarkSet.has(_phraseNorm(text))};
  pop.style.display = 'block';
  window._wordPopOpenAt = Date.now();
  pop.innerHTML = '<div style="padding:14px;color:#8a9bb4">⏳ 处理词组…</div>';
  _positionWordPop(pop);
  // **点了词组按钮**才把当前选区变持久呼吸高亮（查询中呼吸→出结果常亮保持，点高亮才消失）。
  // 点高亮重新弹框时 opts.noHighlight=true → 只弹框、不再建新高亮。
  if (_wordPopState.phrase && !(opts && opts.noHighlight)) _showPhraseHighlight(_charSel && _charSel.pw);
  const esc = s => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
  const isJa = _isJaWord(text);
  _wordPopState.jp = isJa;   // 掌握按钮按语言分流 store(jp-vocab-mark / vocab-mark)
  let zh = '', reading = '', accent = null;
  try {
    if (isJa) {
      const d = await (await fetch('/pdf/api/dict-jp?word=' + encodeURIComponent(text))).json();
      if (d.ok) { zh = d.zh || ''; reading = d.reading || ''; accent = (d.accent != null ? d.accent : null); }
    }
    if (!zh) {
      const d = await (await fetch('/pdf/api/translate-sentence', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text}),
      })).json();
      if (d.ok) zh = d.zh || '';
    }
  } catch (_) {}
  // 出结果 → 停呼吸转常亮**保持**（不移除）；只有点高亮本身才消失
  if (_activePhraseHl) {
    _activePhraseHl.solid = true;
    document.querySelector('.phrase-hl-layer')?.classList.remove('breathe');
  }
  _wordPopState.reading = reading;
  const phon = (isJa && reading && accent != null) ? _renderPitch(reading, accent)
    : (reading ? '<span class="wp-phon">' + esc(reading) + '</span>' : '');
  const fav = _phraseFavSet.has(text);
  pop.innerHTML =
    '<div class="wp-head"><span class="wp-word">' + esc(text) + '</span>' + phon +
    (reading ? '<button class="wp-speak" onclick="_speakCurWord()" title="发音">🔊</button>' : '') + '</div>' +
    '<div class="wp-def">' + (zh ? esc(zh) : '<span style="color:#8a9bb4">（无翻译）</span>') + '</div>' +
    '<div class="wp-actions">' +
    '<button id="phrase-fav-btn" class="' + (fav ? 'wp-anki' : '') + '" onclick="_phraseFav(this)">' +
    (fav ? '★ 已收藏' : '☆ 收藏为词组') + '</button>' +
    '<button id="wp-master-btn" class="' + (_wordPopState.mastered ? 'wp-anki' : '') + '" onclick="_wordPopMaster(this)" title="' + (_wordPopState.mastered ? '点击取消掌握（恢复生词下划线）' : '标记掌握 100（该词组不再标生词下划线）') + '">' +
    (_wordPopState.mastered ? '✓ 已掌握 100' : '☆ 标记掌握') + '</button>' +
    '<button onclick="onExplain()" title="详细解释这个词组">💡 解释</button>' +
    '</div>';
}
window._phraseFav = (btn) => {
  const s = _wordPopState; if (!s || !s.word) return;
  const t = s.word;
  const has = _phraseFavSet.has(t);
  if (btn) btn.disabled = true;
  fetch('/pdf/api/phrases', {
    method: has ? 'DELETE' : 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text: t}),
  }).then(r => r.json()).then(d => {
    if (d.ok) {
      _phraseFavSet = new Set(d.phrases || []);
      const nowFav = _phraseFavSet.has(t);
      if (btn) { btn.disabled = false; btn.textContent = nowFav ? '★ 已收藏' : '☆ 收藏为词组'; btn.classList.toggle('wp-anki', nowFav); }
      _toast?.(nowFav ? '已收藏，之后会作为一个词分词' : '已取消收藏');
      if (nowFav) _removePhraseHighlight();   // 收藏后该词组变成划线(分词单元),呼吸查询高亮自动消除
      refreshCharsWForAllPages();   // 让分词合并立即生效
    } else if (btn) { btn.disabled = false; }
  }).catch(() => { if (btn) btn.disabled = false; });
};
// 收藏/取消后重拉各已渲染页 chars 的 w（原地更新，不重建 char-layer）→ 单击立即按新词组边界选
// generation 守卫:快速收藏→立即取消两次调用交错时,旧轮迟到的响应不准写回(只让最新一轮落地);
// 距视口中心近的页先刷 + 并发 3 小池(页内 overlay→chars 的 cv 依赖仍串行;无界并发会打满服务端 gthread)。
let _charsWGen = 0;
async function _refreshOnePageCharsW(pw, num, gen) {
  // 先 overlay 拿新 cv(收藏词组改了→cv 变,避开 SW 缓存里旧 w)+ 新 vocab_marks
  let ov = null;
  try { ov = await (await fetch('/pdf/api/page-overlay?file=' + encodeURIComponent(FILE_REL) + '&page=' + num)).json(); } catch (_) {}
  if (gen !== _charsWGen) return;   // 已有更新一轮 → 本轮结果作废(连 localStorage cv 也不写,防旧值覆盖新)
  const cv = (ov && ov.ok && ov.cv) ? ov.cv : CHARS_VER;
  if (ov && ov.ok && ov.cv) { try { localStorage.setItem('pdf-cv:' + FILE_REL + ':' + num, ov.cv); } catch (_) {} }  // 词组变后更新各页 cv
  const d = await (await fetch('/pdf/api/page-chars?file=' + encodeURIComponent(FILE_REL) + '&page=' + num + '&v=' + CHARS_VER + '&cv=' + encodeURIComponent(cv))).json();
  if (gen !== _charsWGen) return;
  if (!d.ok || !d.chars) return;
  const newW = d.chars.map(c => (c.w == null ? -1 : c.w));
  for (const cb of pw.__charBoxes) { if (cb._oi != null && cb._oi < newW.length) cb.w = newW[cb._oi]; }
  if (d.furigana) {   // 收藏词组后:振假名已按整体读音合并(当試験→とうしけん 一条),刷新并重画 ruby
    pw.__furigana = d.furigana;
    try { if (_rubyEnabled()) renderRubyLayer(pw); } catch (_) {}
  }
  pw.__vocabMarks = (ov && ov.vocab_marks) || [];   // overlay 失败 → 清空(跟初加载降级一致,不留陈旧下划线)
  try { renderVocabUnderlines(pw, pw.__vocabMarks); } catch (_) {}
}
async function refreshCharsWForAllPages() {
  const gen = ++_charsWGen;   // 入口 bump:作废所有在途旧轮
  const wraps = [...document.querySelectorAll('[data-loaded="1"][data-page-num]')];
  // 距视口中心近的页优先(用户正看的页分词最先生效)
  const cy = (window.innerHeight || 0) / 2;
  const dist = (pw) => { const r = pw.getBoundingClientRect(); return Math.abs((r.top + r.bottom) / 2 - cy); };
  wraps.sort((a, b) => dist(a) - dist(b));
  let next = 0;
  const worker = async () => {
    while (next < wraps.length && gen === _charsWGen) {
      const pw = wraps[next++];
      const num = parseInt(pw.dataset.pageNum || '0', 10);
      if (!num || !pw.__charBoxes) continue;
      try { await _refreshOnePageCharsW(pw, num, gen); } catch (_) {}
    }
  };
  await Promise.all([worker(), worker(), worker()]);
}

// ── 单词小框：单击单词/点查词 → 先 ecdict 核心(秒回)，可展开完整大框，可主动制卡 ──
let _wordPopState = null;
let _wordPopSeq = 0;   // 查词请求序号:防竞态(快速点不同词,旧词响应晚到覆盖新词)
function _positionWordPop(pop, cs) {
  // 挂进 #main（不会被单页重渲染的 innerHTML='' 删掉）；用 page-wrap 布局坐标定位，
  // absolute 相对 #main → 随内容滚动。（之前挂 page-wrap，侧栏展开触发重渲染会把小框删掉 → 闪没）
  // cs：可传入查词时捕获的 charSel（慢词点高亮后定位用，防此时 _charSel 已变）。默认用当前 _charSel。
  cs = cs || _charSel;
  const pw = cs && cs.pw;
  const ch = pw && pw.__charBoxes && pw.__charBoxes[cs.startIdx];
  const main = document.getElementById('main');
  if (pw && ch && ch.left != null && main) {
    if (pop.parentElement !== main) main.appendChild(pop);
    pop.style.position = 'absolute';
    const left = pw.offsetLeft + ch.left;   // pw.offsetParent === #main（page-container static）
    pop.style.left = Math.max(4, left) + 'px';
    pop.style.top = (pw.offsetTop + ch.top + ch.height + 6) + 'px';
    // 渲染后按「可视视口」夹取(不是 main.scrollWidth)：#main 可横向滚动 / 缩放，
    // 固定宽 + scrollWidth 夹会把框推到视口外被裁(日语词框右侧 語法/例句/标记掌握 被切)。
    // pop 是 absolute-in-#main、无额外 CSS 缩放 → 视口空间位移量 == 内容坐标位移量。
    requestAnimationFrame(() => {
      if (pop.style.display === 'none') return;
      const pr = pop.getBoundingClientRect();
      if (!pr.width) return;
      const mainRect = main.getBoundingClientRect();
      const vr = (typeof _visRight === 'function') ? _visRight() : window.innerWidth;
      let dL = parseFloat(pop.style.left) || 0;
      // 左右互斥(框不会同时两边溢出，除非比可视区还宽 → 那时优先保右缘[动作按钮]在视野)
      if (pr.right > vr - 8) dL -= (pr.right - (vr - 8));                        // 右溢出 → 左移
      else if (pr.left < mainRect.left + 8) dL += (mainRect.left + 8 - pr.left); // 左溢出 → 右移
      pop.style.left = Math.max(4, dL) + 'px';
      // 竖直：max-height(CSS 80vh)已封顶高度 → 在视口空间把框夹进 [8, innerH-8]
      // (pop 是 absolute-in-#main、无 CSS 缩放 → 视口位移量 == top 内容坐标位移量)
      const pr2 = pop.getBoundingClientRect();
      let dT = 0;
      if (pr2.bottom > window.innerHeight - 8) dT -= (pr2.bottom - (window.innerHeight - 8)); // 底溢 → 上移
      if (pr2.top + dT < 8) dT += (8 - (pr2.top + dT));                                       // 顶溢 → 下移
      if (dT) pop.style.top = ((parseFloat(pop.style.top) || 0) + dT) + 'px';
    });
  } else {
    if (main && pop.parentElement !== main) main.appendChild(pop);
    pop.style.position = 'fixed';
    const m = (main || document.body).getBoundingClientRect();
    pop.style.left = (m.left + m.width / 2 - 170) + 'px';
    pop.style.top = (m.top + 70) + 'px';
  }
}
// 框外点击 → 自动关小框（用 pointerdown：原生指针不合成，避免 iOS 合成 mousedown 把刚弹出的框误关）
document.addEventListener('pointerdown', (e) => {
  const p = document.getElementById('word-pop');
  if (!p || p.style.display !== 'block') return;
  if (Date.now() - (window._wordPopOpenAt || 0) < 400) return;   // 刚弹出 400ms 内不关（挡打开那次 tap 的余波）
  if (!p.contains(e.target)) {
    p.style.display = 'none';
    // 词组模式：点别处只藏面板，**保留选中高亮**（用户要求「点击其他内容时这个词的高亮不消失」）。
    // 高亮只在「重新选词 / 点空白」时由 onStart 自然替换或清除；查词加载完会转常亮（独立于面板可见性）。
  }
}, true);
// 画日语声调(ピッチアクセント):读音拆拍 → 高/低 + 下降标记。
// accent: 0=平板(LHHH…不降),1=頭高(HLLL…),N=第N拍后下降(LH…H↓L)
function _renderPitch(reading, accent) {
  const small = 'ぁぃぅぇぉゃゅょゎァィゥェォャュョヮ';
  // 拆拍:小书写假名并入前一拍
  const mora = [];
  for (const ch of reading) {
    if (small.includes(ch) && mora.length) mora[mora.length - 1] += ch;
    else mora.push(ch);
  }
  const n = mora.length;
  if (!n) return '';
  // 每拍高低
  const hi = i => {            // i: 0-based 拍
    if (accent === 0) return i >= 1;          // 平板:第1拍低,其余高
    if (accent === 1) return i === 0;         // 頭高:第1拍高,其余低
    return i >= 1 && i < accent;              // 中高/尾高:2..accent 高
  };
  let html = '<span class="wp-pitch" title="声调型 ' +
    (accent === 0 ? '平板' : accent === 1 ? '頭高' : '第' + accent + '拍后降') + '">';
  for (let i = 0; i < n; i++) {
    const h = hi(i);
    const drop = (accent >= 1 && i + 1 === accent);   // 此拍后下降
    html += '<span class="pm' + (h ? ' hi' : '') + (drop ? ' drop' : '') + '">' + mora[i] + '</span>';
  }
  const tlabel = accent === 0 ? '平板' : accent === 1 ? '頭高' : '['+accent+']';
  html += '<span class="pm-type">' + tlabel + '</span></span>';
  return html;
}

// 日语变形分析 → HTML 行（原形 + 中文语法标签）。word-pop 和完整字典共用。
function _jpInflectHtml(inf, word) {
  if (!inf) return '';
  const esc = s => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
  const showBase = inf.base && inf.base !== (word || '');
  const b = showBase ? '原形 <b>' + esc(inf.base) + '</b>' : '';
  const m = (inf.marks || []).length ? '<span class="jp-inflect-mark">' + inf.marks.map(esc).join('・') + '</span>' : '';
  if (!b && !m) return '';
  return '<div class="jp-inflect">🔀 ' + [b, m].filter(Boolean).join('　') + '</div>';
}
// 英语原型 + 变形 → HTML 行（跟日语变形行同款样式）。clicked=用户点的词；lemma=ECDICT 还原的原型；
// forms=该词的各种屈折(复数/过去式/比较级…)。点的是变形词时显「原型 run」，并列出其余变形 chip。
function _enFormsHtml(lemma, forms, clicked) {
  const esc = s => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
  lemma = (lemma || '').toLowerCase();
  const c = (clicked || '').toLowerCase();
  const fs = [...new Set((forms || []).map(f => String(f || '').toLowerCase()).filter(Boolean))]
    .filter(f => f !== lemma).slice(0, 8);
  const b = (lemma && c && c !== lemma) ? '原型 <b>' + esc(lemma) + '</b>' : '';
  const m = fs.length ? '变形 <span class="jp-inflect-mark">' + fs.map(esc).join('・') + '</span>' : '';
  if (!b && !m) return '';
  return '<div class="jp-inflect">🔀 ' + [b, m].filter(Boolean).join('　') + '</div>';
}
// ── 单击查词的"等待"表现（照「解释」那套，多个可并存）──────────────────────────
// 快词(≤300ms 回，英语 ecdict / 已缓存日语)直接弹小框；慢词(日语 AI 等)不弹挡视线的"查词中"框，
// 而是给词建呼吸高亮当等待指示，就绪转常亮，**点高亮才出结果 + 高亮消失**。
// 多个查词可同时进行 → 多个呼吸高亮并存（`_wordHls` 数组），各自独立查、各自点开。
let _wordHlSeq = 0;          // 高亮 id 发号
let _wordHls = [];           // 并存的查词高亮 [{id,page,pw,rects,word,ctx,charSel,shown,ready,data,error,boxOpen}]
let _wordPopOwnerId = null;  // 当前 word-pop 小框归属的高亮 id（防并发查词回来填错框）
// 本会话查词结果缓存（word→dict-quick d）：已查过的词再点**直接秒显小框**(不发请求/不建高亮),
// 后台再打一次刷新暴露计数。用户诉求:"已有现成数据的词单击应直接出结果,不要先高亮再点"。
const _dictCache = new Map();
// 按词的 rects 从本页 furigana(日读音/英音标,page-chars 一直返回)取命中条目 → 查词等待时先标出来
function _furiHitsForRects(pw, rects) {
  const fg = pw && pw.__furigana;
  if (!fg || !fg.length || !rects || !rects.length) return [];
  let X0 = Infinity, Y0 = Infinity, X1 = -Infinity, Y1 = -Infinity;
  for (const r of rects) { X0 = Math.min(X0, r[0]); Y0 = Math.min(Y0, r[1]); X1 = Math.max(X1, r[2]); Y1 = Math.max(Y1, r[3]); }
  const hit = fg.filter(it => {
    if (!it.rt) return false;
    const cx = (it.x0 + it.x1) / 2, cy = (it.y0 + it.y1) / 2;
    return cx >= X0 - 1 && cx <= X1 + 1 && cy >= Y0 - 2 && cy <= Y1 + 2;
  });
  hit.sort((a, b) => (Math.abs(a.y0 - b.y0) > 4 ? a.y0 - b.y0 : a.x0 - b.x0));
  return hit;
}
function renderWordHl(pw) {   // 渲染该页所有查词高亮（多个并存；boxOpen 的不画）
  pw.querySelectorAll('.word-hl-layer').forEach(l => l.remove());
  const page = parseInt(pw.dataset.pageNum || '0', 10);
  const mine = _wordHls.filter(h => h.page === page && h.rects && h.rects.length && !h.boxOpen);
  if (!mine.length) return;
  const canvas = pw.querySelector('canvas');
  const cssW = canvas?.clientWidth || pw.clientWidth, cssH = canvas?.clientHeight || pw.clientHeight;
  const pageWPt = pw.__pageWPt || cssW, pageHPt = pw.__pageHPt || cssH;
  if (!cssW || !cssH || !pageWPt || !pageHPt) return;
  const sx = cssW / pageWPt, sy = cssH / pageHPt;
  for (const h of mine) {
    const layer = document.createElement('div');
    layer.className = 'word-hl-layer' + (h.ready ? '' : ' breathe');   // 查词中呼吸；就绪转常亮(点我看词义)
    for (const r of h.rects) {
      const d = document.createElement('div'); d.className = 'hl';
      d.style.left = (r[0] * sx) + 'px'; d.style.top = (r[1] * sy) + 'px';
      d.style.width = ((r[2] - r[0]) * sx) + 'px'; d.style.height = ((r[3] - r[1]) * sy) + 'px';
      layer.appendChild(d);
    }
    // 查词等待时:先把这处注音(日读音/英音标,本页 furigana)标在词上方——复用原振假名 .ruby-layer .rt 样式(大小/位置一致)
    const _hits = _furiHitsForRects(pw, h.rects);
    if (_hits.length) {
      const rl = document.createElement('div');
      rl.className = 'ruby-layer';   // pointer-events:none → 不挡高亮点击;.rt 样式靠这个作用域
      for (const it of _hits) { const sp = _makeRubySpan(it, sx, sy); if (sp) rl.appendChild(sp); }
      layer.appendChild(rl);
    }
    layer.addEventListener('click', (e) => { e.stopPropagation(); _wordHlClick(h); });
    pw.appendChild(layer);
  }
}
function _renderWordHlsFor(pw) { if (pw) { try { renderWordHl(pw); } catch (_) {} } }
function _removeWordHl(hl) {   // 移除单个高亮(点开/出错/查完后)
  _wordHls = _wordHls.filter(o => o !== hl);
  _renderWordHlsFor(hl.pw);
}
function _removeWordHighlight() {   // 清掉全部查词高亮(换书/大跳转时备用)
  _wordHls = [];
  document.querySelectorAll('.word-hl-layer').forEach(l => l.remove());
}
// 把一个慢词查词 hl 物化成呼吸高亮(加入 _wordHls 并渲染)。同范围去重防重复点同词叠两层。
function _materializeWordHl(hl) {
  const pw = hl.pw;
  if (!pw || !hl.charSel || !pw.__charBoxes) return;
  const rects = _charRangeToPtRects(pw.__charBoxes, hl.charSel.startIdx, hl.charSel.endIdx);
  if (!rects.length) return;
  hl.rects = rects; hl.shown = true;
  _wordHls = _wordHls.filter(o => !(o.page === hl.page && o.charSel && o.charSel.startIdx === hl.charSel.startIdx));
  _wordHls.push(hl);
  const sel = pw.querySelector('.sel-overlay'); if (sel) sel.innerHTML = '';   // 移交持久层(蓝选区→呼吸高亮)
  _renderWordHlsFor(pw);
}
// 点呼吸高亮：就绪→直接弹小框并移除该高亮；未就绪→用户主动开"查词中"小框(等 fetch 回来自动填)
function _wordHlClick(hl) {
  if (!hl) return;
  if (hl.ready) {
    if (hl.error) { _toast?.('查词失败'); _removeWordHl(hl); return; }
    _wordPopOwnerId = hl.id;
    _renderWordPop(hl.word, hl.ctx, hl.data, hl.charSel);
    _removeWordHl(hl);
  } else {
    hl.boxOpen = true; _wordPopOwnerId = hl.id;
    _wordPopState = {word: hl.word, ctx: hl.ctx, lemma: hl.word};
    const pop = document.getElementById('word-pop');
    pop.style.display = 'block'; window._wordPopOpenAt = Date.now();
    pop.innerHTML = '<div style="padding:14px;color:#8a9bb4">⏳ 查词中…</div>';
    _positionWordPop(pop, hl.charSel);
    _renderWordHlsFor(hl.pw);   // boxOpen=true → renderWordHl 过滤掉它,不再画呼吸高亮
  }
}
async function _lookupWordFetch(word, ctx) {
  const r = await fetch('/pdf/api/dict-quick?word=' + encodeURIComponent(word) +
    '&file=' + encodeURIComponent(FILE_REL || '') + '&page=' + ((typeof _selPageNum === 'function' ? _selPageNum() : currentPage) || 0) +
    '&context=' + encodeURIComponent(ctx || '') +
    '&langs=' + encodeURIComponent((BOOK_LANGS || []).join(',')));
  return await r.json();
}
// 渲染单词小框(已拿到 dict-quick 结果 d)。cs=查词时捕获的 charSel,用于定位(防之后选区变了定位飘)。
function _renderWordPop(word, ctx, d, cs) {
  const pop = document.getElementById('word-pop');
  _wordPopState = {word, ctx: ctx || '', lemma: word};
  if (!d || !d.ok) { pop.style.display = 'none'; _expandWordFull(word, ctx); return; }   // ecdict 没有 → 直接完整
  try { _dictCache.set(word, d); if (_dictCache.size > 600) _dictCache.delete(_dictCache.keys().next().value); } catch (_) {}
  _wordPopState.lemma = d.lemma || word;
  _wordPopState.jp = !!d.jp;                // 掌握按钮按语言分流(jp/en 不同 store)
  _wordPopState.reading = (d.jp && d.reading) ? d.reading : '';   // 日语:发音念假名读音(保证读对)
  _wordPopState.mastered = !!d.mastered;   // 掌握开关初始态(日英都返回 mastered)
  const esc = s => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
  const defLines = (d.translation || d.definition || '(无释义)').split('\n').filter(Boolean).slice(0, 3).map(esc).join('<br>');
  // 词性单独做暗色小标签，跟含义用颜色/字号区分（日语：名詞・サ变 等）
  const posTag = (d.pos ? '<span class="wp-pos-tag">' + esc(d.pos) + '</span>' : '');
  // 变形分析：日语=原形+语法标签（过去た/否定ない/て形…）；英语=原型+各种屈折变形
  const inflectHtml = d.jp ? _jpInflectHtml(d.inflect, word) : _enFormsHtml(d.lemma || word, d.forms, word);
  // 日语:画声调曲线(读音+ピッチアクセント);否则普通音标
  const phonHtml = (d.jp && d.reading && d.accent != null)
    ? _renderPitch(d.reading, d.accent)
    : (d.phonetic ? '<span class="wp-phon">' + esc(d.phonetic) + '</span>' : '');
  // 日语母语例句(Tanaka):直接展示在小框;zh 未翻译则回退英文
  let exHtml = '';
  if (d.jp && Array.isArray(d.examples) && d.examples.length) {
    exHtml = '<div class="wp-ex">' + d.examples.slice(0, 2).map(e =>
      '<div class="wp-ex-ja">' + esc(e.ja) + '</div>' +
      '<div class="wp-ex-zh">' + esc(e.zh || e.en || '') + '</div>'
    ).join('') + '</div>';
  }
  pop.style.display = 'block';
  window._wordPopOpenAt = Date.now();   // 框外关闭监听据此忽略刚弹出时的余波事件
  pop.innerHTML =
    '<div class="wp-head"><span class="wp-word">' + esc(d.lemma || word) + '</span>' +
    phonHtml +
    '<button class="wp-speak" onclick="_speakCurWord()" title="发音">🔊</button>' +
    (d.freq_bnc ? '<span class="wp-freq">BNC#' + d.freq_bnc + '</span>' : '') + '</div>' +
    inflectHtml +
    '<div class="wp-def" onclick="_expandWordFull()" title="点开看完整释义/例句">' + posTag + defLines +
    exHtml +
    '<div class="wp-more">点这里展开完整字典 ▾</div></div>' +
    '<div class="wp-actions">' +
    // 掌握 toggle:日英统一同一个按钮(onclick 内部按语言分流 store);✓掌握=下划线消失
    '<button id="wp-master-btn" class="' + (d.mastered ? 'wp-anki' : '') + '" onclick="_wordPopMaster(this)" title="' + (d.mastered ? '点击取消掌握（恢复生词下划线）' : '标记掌握 100（下划线消失）') + '">' + (d.mastered ? '✓ 已掌握 100' : '☆ 标记掌握') + '</button>' +
    '<button onclick="_wordPopGrammar()" title="对该词所在整句做语法分析（分词/结构/跟踪知识点）">📊 语法</button>' +
    '</div>';
  _positionWordPop(pop, cs);
  // 查过即记入生词库 → 刷新本页下划线（橙=新/黄=见过/淡绿=熟）
  try { refreshVocabUnderlinesForAllPages(); } catch (_) {}
}
// 「结果到了自动弹出」的取消信号:点词后只要页面被滚动/滚轮、或又点了别的词,
// 慢词结果回来就**不再**自动弹(退回旧行为:常亮高亮等用户点,位置才不会错)。
let _wordPopCancelSeq = 0;
(() => {
  const m = document.getElementById('main');
  if (m) {
    m.addEventListener('scroll', () => { _wordPopCancelSeq++; }, { passive: true });
    m.addEventListener('wheel',  () => { _wordPopCancelSeq++; }, { passive: true });
  }
})();

window.showWordPopover = async (word, ctx) => {
  word = (word || '').trim().toLowerCase();
  if (!word) return;
  const _cseq = ++_wordPopCancelSeq;   // 本次点词占位;同时取消上一个还没回来的词的自动弹出
  toolbar.classList.remove('open');
  const pw = _charSel && _charSel.pw;
  const cap = _charSel ? {pw, startIdx: _charSel.startIdx, endIdx: _charSel.endIdx} : null;
  // 已有现成数据(本会话查过)→ **直接秒显小框**,不发请求/不建高亮;后台再打一次刷新暴露计数+缓存
  const cached = _dictCache.get(word);
  if (cached) {
    _wordPopOwnerId = ++_wordHlSeq;   // 占新 owner id(框归属);没有 hl 会匹配它 → 别的并发慢词回来不会覆盖本框
    _renderWordPop(word, ctx, cached, cap);
    _lookupWordFetch(word, ctx).then(d => { if (d && d.ok) _dictCache.set(word, d); }).catch(() => {});
    return;
  }
  // 每次查词一个独立 hl;多个可并存(各自呼吸/各自点开)，**不清掉别的查词高亮**。
  // 慢词(>400ms 未回)才物化成呼吸高亮当等待指示;快词(含服务端已缓存)直接弹小框(全程不显示"查词中"框)。
  const hl = {
    id: ++_wordHlSeq, page: pw ? (parseInt(pw.dataset.pageNum || '0', 10) || currentPage) : currentPage,
    pw, word, ctx, charSel: cap, rects: null, shown: false, ready: false, data: null, error: null, boxOpen: false,
  };
  const hlTimer = setTimeout(() => { if (!hl.ready && cap) _materializeWordHl(hl); }, 400);
  let d = null, err = null;
  try { d = await _lookupWordFetch(word, ctx); } catch (e) { err = e; }
  clearTimeout(hlTimer);
  hl.ready = true; hl.data = d; hl.error = err;
  if (!hl.shown) {
    // 快路径:300ms 内回来,没物化高亮 → 直接弹小框(归属本 hl)
    if (err) { _toast?.('查词失败：' + err.message); return; }
    _wordPopOwnerId = hl.id;
    _renderWordPop(word, ctx, d, cap);
  } else {
    // 慢路径:呼吸高亮已在 → 重画(本 hl 转常亮)
    _renderWordHlsFor(hl.pw);
    if (hl.boxOpen) {   // 用户已点高亮开了"查词中"小框 → 查完就清掉该高亮;框仍归属本 hl 才填(否则被别的查词接管了)
      if (_wordPopOwnerId === hl.id) {
        if (err) { const p = document.getElementById('word-pop'); if (p) p.innerHTML = '<div style="padding:14px;color:#c00">查词失败：' + err.message + '</div>'; }
        else _renderWordPop(word, ctx, d, hl.charSel);
      }
      _removeWordHl(hl);
    } else if (!err && _wordPopCancelSeq === _cseq) {
      // 结果到了且期间**没滚动页面、也没点别的词** → 自动弹出,不用再点高亮
      // (位置安全:没滚动过 → charSel 矩形仍是点击时的屏幕位置)。滚动/再点词后保持旧行为。
      _wordPopOwnerId = hl.id;
      _renderWordPop(word, ctx, d, hl.charSel);
      _removeWordHl(hl);
    }
  }
};
// 判定是否日语词:含假名→是;含汉字时按本书语言声明(声明含 ja 才算,未声明默认按日语,
// 跟后端 dict-quick want_ja 一致)。中文书(只声明 zh)的汉字词不当日语。
function _isJaWord(w) {
  if (/[぀-ヿ]/.test(w)) return true;
  if (!/[㐀-鿿]/.test(w)) return false;
  const declared = (BOOK_LANGS || []).length > 0;
  return declared ? BOOK_LANGS.includes('ja') : true;
}
// 共享模式(__uiShared)下点词已直连 RC.wordpop.show(rc-wordpop.js 自己的 _expandWordFull 读它自己的 _wordPopState,
//   已正确接线);本文件(reader.js 拼接产物,最后加载)若无条件也赋值 window._expandWordFull,会覆盖掉 rc-wordpop.js
//   的版本——而这份读的是 reader.src 自己的 _wordPopState(共享模式下从未被填,PDF 早已不走原生 showWordPopover 那条路),
//   于是 word 恒为 undefined,`if (!word) return;` 直接短路退出,「展开完整词典」按钮点了没反应(比 _wordPopMaster/
//   _wordPopGrammar/_speakCurWord 那三个更隐蔽——内部本有 __uiShared 分支,但 word 校验在分支之前就已经 return 了)。
//   门控:共享模式下别赋值;legacy 模式(!__uiShared)保留原生行为不变。
if (!window.__uiShared) {
window._expandWordFull = (w, c) => {
  const s = _wordPopState;
  const word = w || (s && s.word);
  const ctx = (c != null ? c : (s && s.ctx)) || '';
  const pop = document.getElementById('word-pop'); if (pop) pop.style.display = 'none';
  if (!word) return;
  if (window.__uiShared && window.PdfAdapter) {   // 阶段2:全词典大框 → rc-wordpop(共享模式;else 为原 dictStreamJP/dictStream,逐字不变)
    PdfAdapter.openFullDict({ word, context: ctx, jp: _isJaWord(word), file: FILE_REL,
      page: (typeof _selPageNum === 'function' ? _selPageNum() : currentPage), langs: BOOK_LANGS,
      fallback: () => { if (_isJaWord(word)) dictStreamJP(word, ctx); else dictStream(word, ctx); } });
    return;
  }
  if (_isJaWord(word)) dictStreamJP(word, ctx);   // 日语完整字典(离线富内容+按需AI)
  else dictStream(word, ctx);                     // 英语三源大框(ecdict+free+mw+例句)
};
}
// 小框喇叭：读当前词（避开 onclick 内联传参的引号冲突），同步播有道(手势栈内)
// 共享模式(__uiShared)下 rc-wordpop.js 核心框的按钮 onclick 写死调这几个全局名,但本文件(reader.js 拼接产物,
//   最后加载)若无条件也赋值,会覆盖 rc-wordpop.js 刚设好的版本(读的是不同的 _wordPopState,导致按钮静默失效)。
//   门控:共享模式下别赋值,让 rc-wordpop.js 自己的版本生效;legacy 模式(!__uiShared)保留原生行为不变。
if (!window.__uiShared) {
window._speakCurWord = () => {
  const s = _wordPopState;
  if (!s) return;
  if (s.reading) { _ttsWord(s.reading, 'ja-JP'); return; }   // 日语:直接念假名读音
  const w = s.lemma || s.word;
  if (w) _speakOnline(w);
};
}
// 小框「掌握」toggle（日英统一）：未掌握 ↔ 掌握 100 来回切，不关框。
// 掌握 → 该词不再标生词下划线；按语言走不同 store：日语 jp-vocab-mark(mastered/unknown)，
// 英语 vocab-mark(known/unknown，写 vocab 笔记 frontmatter.user_mark + 锁 mastery)。
// 乐观更新:标掌握瞬间先把该词下划线从所有已加载页移掉(不等服务器写库+重算),服务器响应后 refresh 再校正。
// 大厂标配(optimistic UI):本地实例下尤其明显——画面立刻响应,不用等任何往返。
function _dropVocabUnderlineOptimistic(s) {
  const keys = new Set();
  for (const k of [s && s.lemma, s && s.word]) if (k) keys.add(String(k).toLowerCase());
  if (!keys.size) return function () {};
  const snaps = [];   // 记录被改的页(before/after)→ 返回 restore 回滚(失败恢复,契约对齐 EPUB __epubDeco.optimisticMaster)
  document.querySelectorAll('[data-loaded="1"][data-page-num]').forEach(pw => {
    if (!pw.__vocabMarks || !pw.__vocabMarks.length) return;
    const before = pw.__vocabMarks;
    const after = before.filter(m =>
      !(keys.has(String(m.lemma || '').toLowerCase()) || keys.has(String(m.word || '').toLowerCase())));
    if (after.length !== before.length) {
      pw.__vocabMarks = after; snaps.push({ pw, before, after });
      try { renderVocabUnderlines(pw, after); } catch (_) {}
    }
  });
  return function restore() {   // 只回滚仍是本次乐观结果的页(防覆盖期间别的刷新写入的新 marks)
    snaps.forEach(({ pw, before, after }) => {
      if (pw.__vocabMarks === after) { pw.__vocabMarks = before; try { renderVocabUnderlines(pw, before); } catch (_) {} }
    });
  };
}
// 共享版 rc-wordpop 的「☆ 标记掌握」乐观去下划线经此调用(PDF 字符层专属;EPUB 走 __epubDeco.optimisticMaster)。
try { window.__pdfDropVocabUnderline = _dropVocabUnderlineOptimistic; } catch (_) {}

if (!window.__uiShared) {
window._wordPopMaster = (btn) => {
  const s = _wordPopState; if (!s) return;
  const next = !s.mastered;
  // 词组(多词/含空格):走 phrase-mark store，不建 ghost vocab 笔记；标掌握后该词组不再画生词下划线
  if (s.phrase) {
    const t = s.word;
    if (btn) btn.disabled = true;
    // 乐观:标掌握瞬间先去掉该词组下划线(对齐下方非 phrase 分支);失败按快照回滚
    let _undoDrop = null;
    if (next) {
      const snaps = [];
      document.querySelectorAll('[data-loaded="1"][data-page-num]').forEach(pw => {
        if (pw.__vocabMarks && pw.__vocabMarks.length) snaps.push({pw, marks: pw.__vocabMarks});
      });
      _dropVocabUnderlineOptimistic(s);
      const changed = snaps.filter(sn => sn.pw.__vocabMarks !== sn.marks).map(sn => ({...sn, after: sn.pw.__vocabMarks}));
      // 只回滚仍是本次乐观结果的页(防覆盖期间其他刷新写入的新 marks)
      _undoDrop = () => changed.forEach(({pw, marks, after}) => {
        if (pw.__vocabMarks === after) { pw.__vocabMarks = marks; try { renderVocabUnderlines(pw, marks); } catch (_) {} }
      });
    }
    fetch('/pdf/api/phrase-mark', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text: t, mark: next ? 'mastered' : ''}),
    }).then(r => r.json()).then((d) => {
      if (d && d.ok === false) throw new Error(d.error || 'fail');
      s.mastered = next;
      _phraseMarkSet = new Set(d.mastered || []);
      if (btn) {
        btn.disabled = false;
        btn.textContent = s.mastered ? '✓ 已掌握 100' : '☆ 标记掌握';
        btn.title = s.mastered ? '点击取消掌握（恢复词组下划线）' : '标记掌握 100（该词组不再标生词下划线）';
        btn.classList.toggle('wp-anki', s.mastered);
      }
      refreshCharsWForAllPages();   // 重拉 w + 重画下划线(掌握→该词组下划线消失)
      _toast?.(s.mastered ? '已掌握，下划线消失' : '已取消掌握');
    }).catch(() => { if (_undoDrop) _undoDrop(); if (btn) btn.disabled = false; _toast?.('标记失败'); });
    return;
  }
  const w = s.lemma || s.word;
  const url = s.jp ? '/pdf/api/jp-vocab-mark' : '/pdf/api/vocab-mark';
  const mark = next ? 'known' : 'unknown';   // 日英统一口径(jp-vocab-mark 已接受 known/unknown)
  if (next) _dropVocabUnderlineOptimistic(s);   // 乐观:立刻去下划线,不等服务器
  if (btn) btn.disabled = true;
  fetch(url, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({word: w, mark}),
  }).then(r => r.json()).then((d) => {
    if (d && d.ok === false) throw new Error(d.error || 'fail');
    s.mastered = next;
    try { const c = _dictCache.get(s.word); if (c) c.mastered = next; } catch (_) {}   // 同步缓存,再点不显旧掌握态
    if (btn) {
      btn.disabled = false;
      btn.textContent = s.mastered ? '✓ 已掌握 100' : '☆ 标记掌握';
      btn.title = s.mastered ? '点击取消掌握（恢复生词下划线）' : '标记掌握 100（下划线消失）';
      btn.classList.toggle('wp-anki', s.mastered);
    }
    try { refreshVocabUnderlinesForAllPages(); } catch (_) {}
    _toast?.(s.mastered ? '已掌握 100，下划线消失' : '已设为未掌握');
  }).catch(() => { if (btn) btn.disabled = false; _toast?.('标记失败'); });
};
}
// 小框「📊 语法」：对该词所在整句做语法分析（复用 onGrammarAnalyze：当前 _charSel=单词→自动扩成整句）
if (!window.__uiShared) {
window._wordPopGrammar = () => {
  const p = document.getElementById('word-pop'); if (p) p.style.display = 'none';
  try { onGrammarAnalyze(); } catch (e) { window.dlog && window.dlog('grammar from word-pop fail: ' + (e && e.message)); }
};
}
// 工具栏「🔍 查词」：弹单词小框
window.onLookupWord = () => {
  if (!lastSelText) return;
  let ctx = '';
  if (_charSel && _charSel.pw && _charSel.pw.__charBoxes) {
    const chars = _charSel.pw.__charBoxes;
    const cr = _expandSentenceFromRange(chars, _charSel.startIdx, _charSel.endIdx);
    if (cr) ctx = _charsRangeToText(chars, cr.start, cr.end).slice(0, 400);
  }
  if (window.__uiShared && window.PdfAdapter) {
    PdfAdapter.lookupWord({ word: lastSelText, context: ctx, page: (typeof _selPageNum === 'function' ? _selPageNum() : currentPage), file: FILE_REL, langs: BOOK_LANGS, anchorRect: _charSel, fallback: (w, c) => showWordPopover(w, c) });
  } else {
    showWordPopover(lastSelText, ctx);
  }
};

// 拿点击/触摸位置对应的 (node, offset)，可跨 span
