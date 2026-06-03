// ──────── F6 词组：收藏（作分词依据）+ 词组详情面板 ────────
let _phraseFavSet = new Set();
async function _loadPhraseFavs() {
  try { const d = await (await fetch('/pdf/api/phrases')).json(); if (d.ok) _phraseFavSet = new Set(d.phrases || []); } catch (_) {}
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
  const rects = []; let cur = null;
  for (let i = s; i <= e && i < chars.length; i++) {
    const c = chars[i];
    if (c.sp || c._x0 == null) continue;
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
    _removePhraseHighlight();
    showPhrasePopover(txt, {noHighlight: true});
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
// ──────── 解释持久高亮（与词组高亮平行：独立琥珀色 + 独立状态，可与词组高亮共存）────────
// 点「解释」时在选区建高亮：AI 加载中呼吸 → 出结果转常亮**保持**(不自动取消);
// 点高亮 → 用缓存的解释 HTML 重开结果面板(沿用现有面板)并移除该高亮。单 active(再开新解释替换旧)。
let _activeExplainHl = null;   // {page,text,rects,solid,html,title,src,resultContext}
let _explainHlPending = null;  // onExplain 设;aiCall 完成时认领并把结果缓存进高亮
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
  layer.className = 'explain-hl-layer' + (a.solid ? '' : ' breathe');
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
  document.querySelectorAll('.explain-hl-layer').forEach(l => l.remove());
  _activeExplainHl = {
    page: parseInt(pw.dataset.pageNum || '0', 10) || currentPage,
    text: t, rects, solid: false, html: '', title: '💡 AI 解释', src: t, resultContext: null,
  };
  const sel = pw.querySelector('.sel-overlay'); if (sel) sel.innerHTML = '';   // 移交持久层,避免双重高亮
  renderExplainHl(pw);
  return _activeExplainHl;
}
function _reopenExplain() {
  const a = _activeExplainHl;
  if (!a) return;
  if (a.html) {
    openResult(a.title || '💡 AI 解释', a.src || a.text, a.html);
    try { if (a.resultContext) _resultContext = a.resultContext; } catch (_) {}   // 恢复加号选段/草稿上下文
    try { addResultPickers(); } catch (_) {}
  }
  _removeExplainHighlight();   // 点高亮=打开页面,随即移除(同词组)
}
window.showPhrasePopover = async (text, opts) => {
  const pop = document.getElementById('word-pop');
  toolbar.classList.remove('open');
  _wordPopState = {word: text, ctx: '', lemma: text, phrase: true, reading: '', jp: false, mastered: false};
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
};
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
      refreshCharsWForAllPages();   // 让分词合并立即生效
    } else if (btn) { btn.disabled = false; }
  }).catch(() => { if (btn) btn.disabled = false; });
};
// 收藏/取消后重拉各已渲染页 chars 的 w（原地更新，不重建 char-layer）→ 单击立即按新词组边界选
async function refreshCharsWForAllPages() {
  const wraps = document.querySelectorAll('[data-loaded="1"][data-page-num]');
  for (const pw of wraps) {
    const num = parseInt(pw.dataset.pageNum || '0', 10);
    if (!num || !pw.__charBoxes) continue;
    try {
      const d = await (await fetch('/pdf/api/page-chars?file=' + encodeURIComponent(FILE_REL) + '&page=' + num + '&v=' + CHARS_VER)).json();
      if (!d.ok || !d.chars) continue;
      const newW = d.chars.map(c => (c.w == null ? -1 : c.w));
      for (const cb of pw.__charBoxes) { if (cb._oi != null && cb._oi < newW.length) cb.w = newW[cb._oi]; }
      pw.__vocabMarks = d.vocab_marks || pw.__vocabMarks;
      try { renderVocabUnderlines(pw, pw.__vocabMarks); } catch (_) {}
    } catch (_) {}
  }
}

// ── 单词小框：单击单词/点查词 → 先 ecdict 核心(秒回)，可展开完整大框，可主动制卡 ──
let _wordPopState = null;
let _wordPopSeq = 0;   // 查词请求序号:防竞态(快速点不同词,旧词响应晚到覆盖新词)
function _positionWordPop(pop) {
  // 挂进 #main（不会被单页重渲染的 innerHTML='' 删掉）；用 page-wrap 布局坐标定位，
  // absolute 相对 #main → 随内容滚动。（之前挂 page-wrap，侧栏展开触发重渲染会把小框删掉 → 闪没）
  const pw = _charSel && _charSel.pw;
  const ch = pw && pw.__charBoxes && pw.__charBoxes[_charSel.startIdx];
  const main = document.getElementById('main');
  if (pw && ch && ch.left != null && main) {
    if (pop.parentElement !== main) main.appendChild(pop);
    pop.style.position = 'absolute';
    const W = 340;
    let left = pw.offsetLeft + ch.left;   // pw.offsetParent === #main（page-container static）
    const maxLeft = Math.max(4, main.scrollWidth - W - 4);
    if (left > maxLeft) left = maxLeft;
    if (left < 4) left = 4;
    pop.style.left = left + 'px';
    pop.style.top = (pw.offsetTop + ch.top + ch.height + 6) + 'px';
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
window.showWordPopover = async (word, ctx) => {
  word = (word || '').trim().toLowerCase();
  if (!word) return;
  const myseq = ++_wordPopSeq;   // 本次查词序号;await 回来若已被新查词覆盖则放弃渲染
  toolbar.classList.remove('open');
  _wordPopState = {word, ctx: ctx || '', lemma: word};
  const pop = document.getElementById('word-pop');
  pop.style.display = 'block';
  window._wordPopOpenAt = Date.now();   // 框外关闭监听据此忽略刚弹出时的余波事件
  pop.innerHTML = '<div style="padding:14px;color:#8a9bb4">⏳ 查词中…</div>';
  _positionWordPop(pop);
  try {
    const r = await fetch('/pdf/api/dict-quick?word=' + encodeURIComponent(word) +
      '&file=' + encodeURIComponent(FILE_REL || '') + '&page=' + (currentPage || 0) +
      '&context=' + encodeURIComponent(ctx || '') +
      '&langs=' + encodeURIComponent((BOOK_LANGS || []).join(',')));
    const d = await r.json();
    if (myseq !== _wordPopSeq) return;   // 期间点了别的词 → 这是旧响应,丢弃(防覆盖新词)
    if (!d.ok) { pop.style.display = 'none'; _expandWordFull(word, ctx); return; }   // ecdict 没有 → 直接完整
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
    // 查过即记入生词库 → 刷新本页下划线（橙=新/黄=见过/淡绿=熟）
    try { refreshVocabUnderlinesForAllPages(); } catch (_) {}
  } catch (e) {
    if (myseq !== _wordPopSeq) return;   // 旧请求出错也不覆盖当前词
    pop.innerHTML = '<div style="padding:14px;color:#c00">查词失败：' + e.message + '</div>';
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
window._expandWordFull = (w, c) => {
  const s = _wordPopState;
  const word = w || (s && s.word);
  const ctx = (c != null ? c : (s && s.ctx)) || '';
  const pop = document.getElementById('word-pop'); if (pop) pop.style.display = 'none';
  if (!word) return;
  if (_isJaWord(word)) dictStreamJP(word, ctx);   // 日语完整字典(离线富内容+按需AI)
  else dictStream(word, ctx);                     // 英语三源大框(ecdict+free+mw+例句)
};
// 小框喇叭：读当前词（避开 onclick 内联传参的引号冲突），同步播有道(手势栈内)
window._speakCurWord = () => {
  const s = _wordPopState;
  if (!s) return;
  if (s.reading) { _ttsWord(s.reading, 'ja-JP'); return; }   // 日语:直接念假名读音
  const w = s.lemma || s.word;
  if (w) _speakOnline(w);
};
// 小框「掌握」toggle（日英统一）：未掌握 ↔ 掌握 100 来回切，不关框。
// 掌握 → 该词不再标生词下划线；按语言走不同 store：日语 jp-vocab-mark(mastered/unknown)，
// 英语 vocab-mark(known/unknown，写 vocab 笔记 frontmatter.user_mark + 锁 mastery)。
window._wordPopMaster = (btn) => {
  const s = _wordPopState; if (!s) return;
  const w = s.lemma || s.word;
  const next = !s.mastered;
  const url = s.jp ? '/pdf/api/jp-vocab-mark' : '/pdf/api/vocab-mark';
  const mark = next ? 'known' : 'unknown';   // 日英统一口径(jp-vocab-mark 已接受 known/unknown)
  if (btn) btn.disabled = true;
  fetch(url, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({word: w, mark}),
  }).then(r => r.json()).then((d) => {
    if (d && d.ok === false) throw new Error(d.error || 'fail');
    s.mastered = next;
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
// 小框「📊 语法」：对该词所在整句做语法分析（复用 onGrammarAnalyze：当前 _charSel=单词→自动扩成整句）
window._wordPopGrammar = () => {
  const p = document.getElementById('word-pop'); if (p) p.style.display = 'none';
  try { onGrammarAnalyze(); } catch (e) { window.dlog && window.dlog('grammar from word-pop fail: ' + (e && e.message)); }
};
// 工具栏「🔍 查词」：弹单词小框
window.onLookupWord = () => {
  if (!lastSelText) return;
  let ctx = '';
  if (_charSel && _charSel.pw && _charSel.pw.__charBoxes) {
    const chars = _charSel.pw.__charBoxes;
    const cr = _expandSentenceFromRange(chars, _charSel.startIdx, _charSel.endIdx);
    if (cr) ctx = _charsRangeToText(chars, cr.start, cr.end).slice(0, 400);
  }
  showWordPopover(lastSelText, ctx);
};

// 拿点击/触摸位置对应的 (node, offset)，可跨 span
