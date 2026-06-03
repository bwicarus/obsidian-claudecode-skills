function _markVocabOptimistic(pw, lemma, forms) {
  if (!_vocabUnderlineEnabled() || !pw || !pw.__charBoxes) return;
  const fset = new Set([lemma, ...(forms || [])].filter(Boolean).map(f => String(f).toLowerCase()));
  if (!fset.size) return;
  let layer = pw.querySelector('.vocab-layer');
  if (!layer) { layer = document.createElement('div'); layer.className = 'vocab-layer'; pw.appendChild(layer); }
  const chars = pw.__charBoxes;
  let i = 0;
  while (i < chars.length) {
    const c = chars[i];
    if (c.sp || !/[A-Za-z]/.test(c.c || '')) { i++; continue; }
    let j = i, word = '';
    while (j < chars.length && !chars[j].sp && /[A-Za-z'’\-]/.test(chars[j].c || '')) { word += chars[j].c; j++; }
    if (fset.has(word.toLowerCase())) {
      const a = chars[i], b = chars[j - 1];
      if (a.left != null && a.top != null) {
        const div = document.createElement('div');
        div.className = 'vocab-underline m-new';
        div.style.left = a.left + 'px';
        div.style.top = (a.top + a.height + 1) + 'px';
        div.style.width = Math.max(4, (b.left + b.width - a.left)) + 'px';
        layer.appendChild(div);
      }
    }
    i = j;
  }
}

// 查完一个词后，刷新所有已渲染页的下划线（让新查的词立刻出现下划线）
async function refreshVocabUnderlinesForAllPages() {
  if (!_vocabUnderlineEnabled() || !pdfDoc) return;
  // 单页模式当前页是 #page-container 自身(无 .page-wrap class)，故按 data-loaded+page-num 选，覆盖两种模式
  const wraps = document.querySelectorAll('[data-loaded="1"][data-page-num]');
  for (const pw of wraps) {
    const pn = parseInt(pw.dataset.pageNum || '0', 10);
    if (!pn) continue;
    try {
      const r = await fetch('/pdf/api/page-vocab-marks?file=' + encodeURIComponent(FILE_REL) + '&page=' + pn);
      const d = await r.json();
      if (!d.ok) continue;
      pw.__vocabMarks = d.vocab_marks || [];
      pw.__vocabSentences = d.vocab_sentences || [];
      renderVocabUnderlines(pw, pw.__vocabMarks);
      renderVocabSentences(pw, pw.__vocabSentences);
    } catch (e) { window.dlog?.('vocab refresh p.' + pn + ' fail: ' + e.message, '#ff6b6b'); }
  }
}

// 句子颜色 palette：[stroke, fill]；按 sid 轮替
const SENT_COLORS = [
  ['#d97706', 'rgba(245,158,11,.18)'],   // 橙
  ['#059669', 'rgba(16,185,129,.18)'],   // 绿
  ['#2563eb', 'rgba(59,130,246,.18)'],   // 蓝
  ['#9333ea', 'rgba(168,85,247,.18)'],   // 紫
  ['#db2777', 'rgba(236,72,153,.18)'],   // 粉
  ['#0891b2', 'rgba(20,184,166,.18)'],   // 青
];

// vocab 句子 L 按钮：既能点击翻译整句，也能从其上转发拖选（绕开 pointer-events:auto 挡选中的问题）。
// mousedown/touch 时把坐标转发给所在页 char-layer 的拖选 API；命中字符则标记 _fromLBtn，
// 单击(无拖)由 click handler 翻译、onEnd 跳过查词；拖动则正常 char 选中、click 被 _dragMoved 拦下。
function _bindSentBtnDrag(btnEl, layer) {
  const getApi = () => {
    const pw = btnEl.closest('[data-page-num]') || layer.parentElement;
    return pw && pw.__charDrag;
  };
  btnEl.addEventListener('mousedown', (e) => {
    e.preventDefault();   // 对齐 cl mousedown：阻止产生原生 selection，否则 checkSelection 会覆盖 char-layer 选中
    e.stopPropagation();
    const api = getApi(); if (!api) return;
    const p = api.ptToLocal(e.clientX, e.clientY);
    if (api.onStart(p.x, p.y)) _fromLBtn = true;
  });
  btnEl.addEventListener('touchstart', (e) => {
    e.stopPropagation();
    const api = getApi(); if (!api || e.touches.length !== 1) return;
    const t = e.touches[0]; const p = api.ptToLocal(t.clientX, t.clientY);
    if (api.onStart(p.x, p.y)) _fromLBtn = true;
  }, {passive: true});
  btnEl.addEventListener('touchmove', (e) => {
    const api = getApi(); if (!api || e.touches.length !== 1) return;
    const t = e.touches[0]; const p = api.ptToLocal(t.clientX, t.clientY);
    api.onMove(p.x, p.y, e);
  }, {passive: false});
  btnEl.addEventListener('touchend', (e) => {
    const api = getApi(); if (!api) return;
    const t = e.changedTouches[0]; if (!t) return;
    const p = api.ptToLocal(t.clientX, t.clientY);
    api.onEnd(p.x, p.y);
  });
}

function renderVocabSentences(pw, sentences) {
  if (!_vocabUnderlineEnabled()) return;
  let layer = pw.querySelector('.vocab-layer');
  if (!layer && sentences && sentences.length) {
    layer = document.createElement('div');
    layer.className = 'vocab-layer';
    pw.appendChild(layer);
  }
  if (!layer) return;
  layer.querySelectorAll('.vocab-sentence-box, [class*="vocab-sentence-btn"]').forEach(el => el.remove());   // 含 btn-l / btn-l-start，否则删除/重渲染时 L 按钮残留
  if (!sentences || !sentences.length) return;
  const canvas = pw.querySelector('canvas');
  const cssW = canvas?.clientWidth || pw.clientWidth;
  const cssH = canvas?.clientHeight || pw.clientHeight;
  const pageWPt = pw.__pageWPt || cssW;
  const pageHPt = pw.__pageHPt || cssH;
  if (!cssW || !cssH || !pageWPt || !pageHPt) return;
  const sx = cssW / pageWPt;
  const sy = cssH / pageHPt;
  for (let si = 0; si < sentences.length; si++) {
    const s = sentences[si];
    const sid = String(si);
    const [stroke, fill] = SENT_COLORS[si % SENT_COLORS.length];
    const rects = s.rects || [];
    // 竖排文字（书页边竖排注释等）：每行 rect 高>宽，句子框/L按钮的几何按横排算会乱 → 跳过
    if (rects.length && rects.filter(r => (r[3]-r[1]) > (r[2]-r[0])).length > rects.length / 2) continue;
    // 缓存颜色到 sentence 对象（覆盖层 / Anki 加卡时复用）
    s.__stroke = stroke; s.__fill = fill;
    // hatch 排线 (135° 斜细线)。默认**淡**(alpha≈0x2e≈18%,细 1px,间距 5px)→ 整页多句也不刺眼；
    // 「翻译中」用加深版(strong, alpha 0x88) 配呼吸。
    const hatch = `repeating-linear-gradient(135deg, ${stroke}55 0 1px, transparent 1px 4px)`;
    const hatchStrong = `repeating-linear-gradient(135deg, ${stroke}88 0 1.2px, transparent 1.2px 4px)`;
    for (let ri = 0; ri < rects.length; ri++) {
      const r = rects[ri];
      const [x0, y0, x1, y1] = r;
      const box = document.createElement('div');
      box.className = 'vocab-sentence-box' + (s.__translating ? ' translating' : '');
      box.dataset.sid = sid;
      box.style.color = stroke;
      box.style.setProperty('--sent-fill', fill);
      box.style.setProperty('--sent-hatch', hatch);
      box.style.setProperty('--sent-hatch-strong', hatchStrong);
      box.style.left = (x0 * sx - 2) + 'px';
      box.style.top = (y0 * sy - 1) + 'px';
      box.style.width = ((x1 - x0) * sx + 4) + 'px';
      box.style.height = ((y1 - y0) * sy + 2) + 'px';
      layer.appendChild(box);
    }
    // L 形按钮（句首）：包裹第一个字符（border-left + border-top 4px）
    const fc = s.first_char;
    if (fc) {
      const btn0 = document.createElement('button');
      btn0.className = 'vocab-sentence-btn-l-start';
      btn0.type = 'button';
      btn0.dataset.sid = sid;
      btn0.title = `翻译整句（含 ${s.count} 个未掌握词）：${(s.text||'').slice(0,80)}…`;
      btn0.style.color = stroke;
      btn0.style.setProperty('--sent-fill', fill);
      const charW0 = (fc[2] - fc[0]) * sx;
      const charH0 = (fc[3] - fc[1]) * sy;
      const left0 = fc[0] * sx - 2;
      // clamp 到句首所在行的文本右边界(rects[0][2])，不伸进纸张右 margin → 不再画出延伸到页边的线
      const lineRight = (rects[0] ? rects[0][2] * sx : cssW);
      const maxW0 = Math.max(charW0, lineRight - left0);
      const wantW0 = Math.min(Math.max(charW0 * 6, 48), maxW0);
      btn0.style.left = left0 + 'px';     // 句首字符 x0 减 padding
      btn0.style.top = (fc[1] * sy - 2) + 'px';
      btn0.style.width = wantW0 + 'px';
      btn0.style.height = charH0 + 'px';
      btn0.addEventListener('click', (e) => {
        e.stopPropagation();
        if (_dragMoved) return;   // 刚从 L 按钮拖选 → 不触发整句翻译
        toggleSentenceOverlay(layer, s, btn0, sx, sy);
      });
      _bindSentBtnDrag(btn0, layer);
      btn0.addEventListener('mouseenter', () => {
        layer.querySelectorAll(`.vocab-sentence-box[data-sid="${sid}"]`).forEach(b => b.classList.add('highlight'));
      });
      btn0.addEventListener('mouseleave', () => {
        layer.querySelectorAll(`.vocab-sentence-box[data-sid="${sid}"].highlight`).forEach(b => b.classList.remove('highlight'));
      });
      layer.appendChild(btn0);
    }
    // L 形按钮（句末）：包裹最后一个字符（border-right + border-bottom 4px）
    const lc = s.last_char;
    if (lc) {
      const btn = document.createElement('button');
      btn.className = 'vocab-sentence-btn-l';
      btn.type = 'button';
      btn.dataset.sid = sid;
      btn.title = `翻译整句（含 ${s.count} 个未掌握词）：${(s.text||'').slice(0,80)}…`;
      btn.style.color = stroke;
      btn.style.setProperty('--sent-fill', fill);
      // 加宽：L 形左缘向左延伸（更易点击）
      const charW = (lc[2] - lc[0]) * sx;
      const charH = (lc[3] - lc[1]) * sy;
      const wantW = Math.max(charW * 6, 48);   // 至少 48px 宽（约 5-6 个字符）
      const extraLeft = wantW - charW;
      let leftE = lc[0] * sx - extraLeft - 2;
      let widthE = wantW;
      if (leftE < 0) { widthE = Math.max(charW, widthE + leftE); leftE = 0; }   // 句末在行首时向左延伸会溢出页左 → 截掉
      btn.style.left = leftE + 'px';
      btn.style.top = (lc[1] * sy - 2) + 'px';
      btn.style.width = widthE + 'px';
      btn.style.height = charH + 'px';
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        if (_dragMoved) return;   // 刚从 L 按钮拖选 → 不触发整句翻译
        toggleSentenceOverlay(layer, s, btn, sx, sy);
      });
      _bindSentBtnDrag(btn, layer);
      btn.addEventListener('mouseenter', () => {
        layer.querySelectorAll(`.vocab-sentence-box[data-sid="${sid}"]`).forEach(b => b.classList.add('highlight'));
      });
      btn.addEventListener('mouseleave', () => {
        layer.querySelectorAll(`.vocab-sentence-box[data-sid="${sid}"].highlight`).forEach(b => b.classList.remove('highlight'));
      });
      _bindSentBtnLongPress(btn, s, pw);   // 所有句(自动+手动)L 按钮:长按弹菜单(重新翻译[+删除,仅手动])
      layer.appendChild(btn);
    }
    if (s.first_char) {
      const b0 = layer.querySelector(`.vocab-sentence-btn-l-start[data-sid="${sid}"]`);
      if (b0) _bindSentBtnLongPress(b0, s, pw);
    }
  }
}

// L 框长按 → 弹菜单(🔄 重新翻译 [+ 🗑 删除,仅手动框])。短按仍是显示/隐藏译文(不重译,防误触重译干扰)。
// 与短按/拖选共存：移动或短按则取消；触发后吃掉随后的 click。
function _bindSentBtnLongPress(btn, s, pw) {
  let timer = null, x0 = 0, y0 = 0, fired = false;
  btn.addEventListener('pointerdown', (e) => {
    x0 = e.clientX; y0 = e.clientY; fired = false;
    clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null; fired = true;
      if (navigator.vibrate) { try { navigator.vibrate(30); } catch (_) {} }
      _showSentMenu(btn, s, pw);
    }, 550);
  });
  const cancel = (e) => {
    if (timer && e && e.type === 'pointermove' && Math.hypot(e.clientX - x0, e.clientY - y0) < 12) return;
    clearTimeout(timer); timer = null;
  };
  btn.addEventListener('pointermove', cancel);
  btn.addEventListener('pointerup', cancel);
  btn.addEventListener('pointercancel', cancel);
  // 长按已弹菜单 → 吃掉随后的 click，避免又触发整句翻译/显示
  btn.addEventListener('click', (e) => { if (fired) { fired = false; e.stopPropagation(); e.preventDefault(); } }, true);
}
// 句子 L 按钮长按菜单
function _showSentMenu(btn, s, pw) {
  document.querySelectorAll('.sent-menu').forEach(m => m.remove());
  const menu = document.createElement('div');
  menu.className = 'sent-menu';
  let html = '<button type="button" data-act="re">🔄 重新翻译</button>';
  if (s.manual) html += '<button type="button" data-act="del">🗑 删除标记</button>';
  menu.innerHTML = html;
  document.body.appendChild(menu);
  const r = btn.getBoundingClientRect();
  menu.style.left = Math.max(6, Math.min(r.left, window.innerWidth - menu.offsetWidth - 6)) + 'px';
  menu.style.top = Math.min(r.bottom + 4, window.innerHeight - menu.offsetHeight - 6) + 'px';
  menu.addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return;
    e.stopPropagation(); e.preventDefault();
    const act = b.dataset.act; menu.remove();
    if (act === 're') _sentRetranslate(s, pw);
    else if (act === 'del') { if (confirm('删除这个翻译框选？（只去掉框线/译文标记，不影响原文）')) _sentDismiss(s, pw); }
  });
  setTimeout(() => {
    const close = (ev) => { if (!menu.contains(ev.target)) { menu.remove(); document.removeEventListener('pointerdown', close, true); } };
    document.addEventListener('pointerdown', close, true);
  }, 0);
}
// 强制重新翻译该句(单句翻译后端不缓存 → 必出新结果),重画译文浮层
function _sentRetranslate(s, pw) {
  if (!s || !pw) return;
  const lay0 = pw.querySelector('.vocab-layer');
  if (lay0) lay0.querySelectorAll('.vocab-sentence-overlay').forEach(el => el.remove());   // 关旧译文
  s.zh = ''; s.__translating = true;
  try { renderVocabSentences(pw, pw.__vocabSentences); } catch (_) {}   // 呼吸表示翻译中
  fetch('/pdf/api/translate-sentence', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text: s.text }),
  }).then(r => r.json()).then(d => {
    s.__translating = false;
    if (d.ok && d.zh) { s.zh = d.zh; try { renderVocabSentences(pw, pw.__vocabSentences); } catch (_) {} _reopenSentOverlay(pw, s); _toast?.('已重新翻译'); }
    else { try { renderVocabSentences(pw, pw.__vocabSentences); } catch (_) {} _toast?.('翻译失败：' + (d.error || '?')); }
  }).catch(e => { s.__translating = false; try { renderVocabSentences(pw, pw.__vocabSentences); } catch (_) {} _toast?.('网络错误：' + e.message); });
}
// 重画某句的就地译文浮层(renderVocabSentences 重建按钮后,按 sid 找回 L 按钮 + sx/sy)
function _reopenSentOverlay(pw, s) {
  const layer = pw.querySelector('.vocab-layer'); if (!layer || !s.zh) return;
  const canvas = pw.querySelector('canvas');
  const cssW = canvas?.clientWidth || pw.clientWidth, cssH = canvas?.clientHeight || pw.clientHeight;
  const pageWPt = pw.__pageWPt || cssW, pageHPt = pw.__pageHPt || cssH;
  if (!cssW || !cssH || !pageWPt || !pageHPt) return;
  const sx = cssW / pageWPt, sy = cssH / pageHPt;
  const si = (pw.__vocabSentences || []).indexOf(s);
  const btn = layer.querySelector(`.vocab-sentence-btn-l[data-sid="${si}"]`)
           || layer.querySelector(`.vocab-sentence-btn-l-start[data-sid="${si}"]`);
  if (btn) _drawSentenceOverlay(layer, s, btn, sx, sy);
}
function _sentDismiss(s, pw) {
  if (!s || !pw) return;
  const txt = (s.text || '').trim();
  if (pw.__vocabSentences) {
    pw.__vocabSentences = pw.__vocabSentences.filter(x => x !== s && (x.text || '').trim() !== txt);
    try { renderVocabSentences(pw, pw.__vocabSentences); } catch (_) {}
  }
  try { closeSentPopover && closeSentPopover(); } catch (_) {}
  if (FILE_REL && txt) {
    fetch('/pdf/api/sentence-dismiss', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file: FILE_REL, text: txt }),
    }).catch(() => {});
  }
}

// 在原句位置叠覆盖层显示中文（再次点击或点 overlay 关闭）
function toggleSentenceOverlay(layer, s, btn, sx, sy) {
  // 就地覆盖(用户原设计):逐行白条贴合原文行,中文按各行宽度比例分配填入。
  const existing = layer.querySelector('.vocab-sentence-overlay[data-active="1"]');
  if (existing && existing.dataset.sentText === s.text) {   // 再点同句 → 关
    layer.querySelectorAll('.vocab-sentence-overlay').forEach(el => el.remove());
    btn.classList.remove('active');
    return;
  }
  layer.querySelectorAll('.vocab-sentence-overlay').forEach(el => el.remove());
  layer.querySelectorAll('.vocab-sentence-btn-l.active, .vocab-sentence-btn-l-start.active')
    .forEach(b => b.classList.remove('active'));
  if (s.zh) { _drawSentenceOverlay(layer, s, btn, sx, sy); return; }
  // 没译过 → 现场翻再画
  btn.classList.add('active');
  fetch('/pdf/api/translate-sentence', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text: s.text}),
  }).then(r => r.json()).then(d => {
    if (d.ok && d.zh) { s.zh = d.zh; _drawSentenceOverlay(layer, s, btn, sx, sy); }
    else { btn.classList.remove('active'); _toast?.('翻译失败：' + (d.error || '?')); }
  }).catch(e => { btn.classList.remove('active'); _toast?.('网络错误：' + e.message); });
}

function _drawSentenceOverlay(layer, s, btn, sx, sy) {
  // 逐行盒子(每行=原文那行的精确 rect 位置/宽度),中文**贪心自然填充**:
  // 填满一行换下一行(像正常文字排版,标点跟着走、不躲逗号、不强行平均切),
  // **字号 = 原文字符高**(跟原句一致)。→ 中文严格落在原句每行位置上、该分行就分行。
  const raw = (s.rects || []).filter(r => (r[2] - r[0]) > 1 && (r[3] - r[1]) > 1);
  const zh = (s.zh || '').trim();
  if (!raw.length || !zh) return;
  layer.querySelectorAll('.vocab-sentence-overlay').forEach(el => el.remove());
  // 关键:s.rects 在逗号/间隙处被切成碎块 → 先合并成「整行一个 rect」。
  // ⚠ 必须按字符**垂直中心**聚类,不能按顶部 y0:日语逗号「，」的 y0 比汉字低很多、
  //   不同汉字 y0 也差几 px(笔画高低不同)→ 按 y0 会把逗号/汉字拆成不同行。
  //   中心则一致(逗号中心≈汉字中心),参考行高取最大字符高(避开逗号的小高)。
  const rects = (() => {
    const refH = Math.max.apply(null, raw.map(r => r[3] - r[1])) || 1;
    const cy = r => (r[1] + r[3]) / 2;
    const sorted = raw.slice().sort((a, b) => (cy(a) - cy(b)) || (a[0] - b[0]));
    const lines = [];
    for (const r of sorted) {
      const last = lines[lines.length - 1];
      if (last && Math.abs(cy(r) - cy(last)) <= refH * 0.5) {   // 同一视觉行(按中心)
        last[0] = Math.min(last[0], r[0]); last[1] = Math.min(last[1], r[1]);
        last[2] = Math.max(last[2], r[2]); last[3] = Math.max(last[3], r[3]);
      } else {
        lines.push([r[0], r[1], r[2], r[3]]);
      }
    }
    lines.sort((a, b) => a[1] - b[1]);   // 行按 top 排序 = 阅读顺序
    return lines;
  })();
  // 字号与原文一致:PyMuPDF char bbox(charH)比实际字号略大(含 ascent/descent 留白),
  // ×0.72 才视觉等于原文字符大小(0.95 实测明显偏大、行间挤);取各行最小行高更稳(避免某行有高字符拉大)
  const charH = Math.min(...rects.map(r => (r[3] - r[1]) * sy));
  const cps = Array.from(zh);                              // 按码点切
  const N = cps.length;
  // 字号默认=原文字高(×0.72);译文比原文长时按「各行总宽 / 字数」缩小,保证整句塞得下不被切
  // (+rects.length 留 floor 取整余量 → 各行 cap 之和 ≥ N,末行收尾不溢出 overflow:hidden)
  const Wtot = rects.reduce((s, r) => s + (r[2] - r[0]) * sx, 0);
  const fontPx = Math.max(9, Math.min(Math.round(charH * 0.72), Math.floor(Wtot / (N + rects.length))));
  let idx = 0;
  rects.forEach((r, i) => {
    const w = (r[2] - r[0]) * sx, h = (r[3] - r[1]) * sy;
    const cap = Math.max(1, Math.floor(w / fontPx));       // 该行能放几个汉字(≈方块宽=字号)
    const n = (i === rects.length - 1) ? (N - idx)         // 末行收尾(余下全给它)
      : Math.max(0, Math.min(cap, N - idx));
    const slice = cps.slice(idx, idx + n).join('');
    idx += n;
    const ov = document.createElement('div');
    ov.className = 'vocab-sentence-overlay';
    ov.dataset.active = '1';
    ov.dataset.sentText = s.text;
    if (s.__stroke) {
      ov.style.setProperty('--sent-stroke', s.__stroke);
      ov.style.setProperty('--sent-hatch',
        `repeating-linear-gradient(135deg, ${s.__stroke}33 0 1.2px, transparent 1.2px 4px)`);
    }
    if (s.__fill) ov.style.setProperty('--sent-fill', s.__fill);
    ov.style.left = (r[0] * sx) + 'px';                    // 贴该行原文位置(首行从行中间起也对)
    ov.style.top = (r[1] * sy) + 'px';
    ov.style.width = w + 'px';
    ov.style.fontSize = fontPx + 'px';
    ov.style.textAlign = 'left';
    ov.style.paddingLeft = '1px';
    // 极端:单行原文 + 超长译文,字号触底仍放不下 → 该框换行向下展开(不硬切)
    if (slice.length * fontPx > w + 0.5) {
      ov.style.minHeight = h + 'px';
      ov.style.whiteSpace = 'normal';
      ov.style.wordBreak = 'break-all';
      ov.style.lineHeight = (fontPx * 1.18) + 'px';
      ov.style.overflow = 'visible';
      ov.style.paddingTop = Math.max(0, (h - fontPx * 1.18) / 2) + 'px';
    } else {
      ov.style.height = h + 'px';
      ov.style.lineHeight = h + 'px';                      // 单行垂直居中
      ov.style.whiteSpace = 'nowrap';
    }
    ov.textContent = slice;
    ov.addEventListener('click', (e) => {
      e.stopPropagation();
      layer.querySelectorAll('.vocab-sentence-overlay').forEach(el => el.remove());
      btn.classList.remove('active');
    });
    ov.addEventListener('mousedown', (e) => e.stopPropagation());
    ov.addEventListener('touchstart', (e) => e.stopPropagation(), {passive: true});
    layer.appendChild(ov);
  });
  btn.classList.add('active');
}

// 翻译 popover
// 可视区右边界：侧栏展开时扣掉侧栏宽度，避免浮层被 clamp 到侧栏底下
function _visRight() {
  const panel = document.getElementById('grammar-panel');
  if (panel && document.body.classList.contains('grammar-open')) {
    const r = panel.getBoundingClientRect();
    if (r.width) return r.left;   // 侧栏左边缘 = 可视区右边界
  }
  return window.innerWidth;
}
function _ensureSentPopover() {
  let pop = document.getElementById('sent-popover');
  if (pop) return pop;
  pop = document.createElement('div');
  pop.id = 'sent-popover';
  pop.innerHTML = `
    <button class="sent-close" type="button" onclick="closeSentPopover()">×</button>
    <div class="sent-en"></div>
    <div class="sent-zh"></div>`;
  document.body.appendChild(pop);
  document.addEventListener('click', (e) => {
    if (!e.target.closest('#sent-popover') && !e.target.closest('.vocab-sentence-btn')) {
      closeSentPopover();
    }
  }, true);
  return pop;
}
window.closeSentPopover = () => {
  document.getElementById('sent-popover')?.classList.remove('open');
};

async function showSentenceTranslation(text, anchorBtn, preZh) {
  const pop = _ensureSentPopover();
  pop.querySelector('.sent-en').textContent = text;
  const zhEl = pop.querySelector('.sent-zh');
  // 定位
  const r = anchorBtn.getBoundingClientRect();
  pop.style.left = (r.right + window.scrollX + 6) + 'px';
  pop.style.top = (r.top + window.scrollY - 4) + 'px';
  pop.classList.add('open');
  requestAnimationFrame(() => {
    const pr = pop.getBoundingClientRect();
    if (pr.right > _visRight() - 8) {
      pop.style.left = Math.max(8, r.left + window.scrollX - pr.width / 2) + 'px';
      pop.style.top = (r.bottom + window.scrollY + 6) + 'px';
    }
  });
  // 已有预翻译 → 立即显示
  if (preZh) {
    zhEl.textContent = '🇨🇳 ' + preZh;
    zhEl.className = 'sent-zh';
    return;
  }
  // fallback：现场调
  zhEl.textContent = '⏳ 翻译中…';
  zhEl.className = 'sent-zh loading';
  try {
    const r2 = await fetch('/pdf/api/translate-sentence', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({text}),
    });
    const d = await r2.json();
    if (d.ok && d.zh) {
      zhEl.textContent = '🇨🇳 ' + d.zh;
      zhEl.className = 'sent-zh';
    } else {
      zhEl.textContent = '翻译失败：' + (d.error || '?');
      zhEl.className = 'sent-zh';
    }
  } catch (e) {
    zhEl.textContent = '网络错误：' + e.message;
    zhEl.className = 'sent-zh';
  }
}
window.showSentenceTranslation = showSentenceTranslation;
window.refreshVocabUnderlinesForAllPages = refreshVocabUnderlinesForAllPages;

// 找点击位置最近的非空格 char index
