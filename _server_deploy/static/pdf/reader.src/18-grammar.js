// ─── 语法分析（per-PDF 启用语法 KG，节点跟踪在技能树页面切换）────────────
let _grammarEnabledBooks = [];   // 本 PDF 启用的 grammar KG list（books）
let _grammarHasTracked = false;  // 是否至少有一个启用书中含 tracked 节点

async function loadGrammarTracked() {
  try {
    const r = await fetch('/pdf/api/grammar-tracked?file=' + encodeURIComponent(FILE_REL));
    const d = await r.json();
    _grammarEnabledBooks = d.enabled_books || [];
  } catch (e) { _grammarEnabledBooks = []; }
  await _refreshGrammarHasTracked();
  _updateGrammarBtnVisibility();
  return _grammarEnabledBooks;
}
async function _refreshGrammarHasTracked() {
  if (!_grammarEnabledBooks.length) { _grammarHasTracked = false; return; }
  try {
    const r = await fetch('/pdf/api/grammar-books');
    const d = await r.json();
    const enabledSet = new Set(_grammarEnabledBooks);
    _grammarHasTracked = (d.books || []).some(b => enabledSet.has(b.book) && (b.tracked_count || 0) > 0);
  } catch (e) { _grammarHasTracked = false; }
}
async function saveGrammarEnabledBooks(books) {
  _grammarEnabledBooks = books;
  try {
    await fetch('/pdf/api/grammar-tracked', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({file: FILE_REL, enabled_books: books}),
    });
  } catch (e) {}
  await _refreshGrammarHasTracked();
  _updateGrammarBtnVisibility();
}
function _updateGrammarBtnVisibility() {
  const row = document.getElementById('grammar-btn-row');
  if (!row) return;
  row.style.display = (_grammarHasTracked && lastSelText) ? '' : 'none';
}
async function renderGrammarTrackList() {
  const wrap = document.getElementById('set-grammar-list');
  if (!wrap) return;
  let books = [];
  try {
    const r = await fetch('/pdf/api/grammar-books');
    const d = await r.json();
    books = d.books || [];
  } catch (e) { books = []; }
  await loadGrammarTracked();
  const enabledSet = new Set(_grammarEnabledBooks);
  if (!books.length) {
    wrap.innerHTML = '<div style="color:#7a8497">还没有语法 KG。新建一本 kind=grammar 的书后会出现。</div>';
    return;
  }
  let html = '';
  for (const b of books) {
    const checked = enabledSet.has(b.book) ? 'checked' : '';
    const hot = (b.tracked_count > 0) ? `<span style="color:#34d399;margin-left:4px">${b.tracked_count} 已跟踪</span>` : `<span style="color:#7a8497;margin-left:4px">无跟踪（去技能树点节点开）</span>`;
    html += `<label style="display:flex;align-items:center;gap:8px;padding:6px 4px;cursor:pointer;color:#cfe6ff;border-radius:3px;border-bottom:1px solid #1f2740" title="共 ${b.total_l2} 个 level-2 语法点">
      <input type="checkbox" value="${b.book}" ${checked} onchange="_onGrammarBookToggle()" style="margin:0">
      <div style="flex:1;min-width:0">
        <div style="font-size:12px">${b.title.replace(/</g,'&lt;')}</div>
        <div style="font-size:10px;color:#7a8497">${b.total_l2} 个语法点 · ${hot}</div>
      </div>
      <a href="/skilltree/${encodeURIComponent(b.book)}/" target="_blank" onclick="event.stopPropagation()" style="color:#60a5fa;font-size:11px;text-decoration:none">技能树 →</a>
    </label>`;
  }
  wrap.innerHTML = html;
}
window._onGrammarBookToggle = function() {
  const books = Array.from(document.querySelectorAll('#set-grammar-list input[type=checkbox]:checked'))
    .map(cb => cb.value);
  window.dlog?.('grammar book toggle → ' + books.join(','));
  saveGrammarEnabledBooks(books);
};

// 选中工具栏的「📊 分析」按钮 → 分析所在完整句子，结果放右侧抽屉
window.onGrammarAnalyze = async () => {
  if (!lastSelText) return;
  if (!_grammarEnabledBooks.length) { _toast?.('请在 PDF 设置中启用至少一个语法 KG'); return; }
  if (!_grammarHasTracked) { _toast?.('已启用的 KG 中没有节点被跟踪，去技能树详情面板点「👁 跟踪」'); return; }
  if (!_charSel || !_charSel.pw || !_charSel.pw.__charBoxes) { _toast?.('找不到选中位置'); return; }
  const pw = _charSel.pw;
  const chars = pw.__charBoxes;
  const ctxRange = _expandSentenceFromRange(chars, _charSel.startIdx, _charSel.endIdx);
  if (!ctxRange) { _toast?.('无法识别完整句子'); return; }
  const sentence = _charsRangeToText(chars, ctxRange.start, ctxRange.end);
  if (sentence.length < 6) { _toast?.('句子太短'); return; }
  const text = lastSelText.trim();   // 用户选中的子串（焦点）
  toolbar.classList.remove('open');
  openGrammarPanel();
  switchSideTab('grammar');   // 分析时切到语法 tab
  const blockId = 'gb_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6);
  const block = _addLoadingBlock(blockId, sentence, text);
  try {
    const r = await fetch('/pdf/api/grammar-analyze', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text, sentence, file: FILE_REL, enabled_books: _grammarEnabledBooks}),
    });
    if (!r.ok) {
      let msg = `HTTP ${r.status}`;
      try { const err = await r.json(); if (err?.error) msg = err.error; } catch {}
      _fillBlockError(block, msg);
      return;
    }
    const d = await r.json();
    if (!d.ok) { _fillBlockError(block, d.error || '?'); return; }
    _fillGrammarBlock(block, d, sentence);          // spaCy 依存图秒出（翻译/语法点先占位）
    if (d.engine === 'spacy') _streamGrammar(block, sentence, text);  // AI 流式补翻译(先)+语法点
  } catch (e) {
    _fillBlockError(block, e.message);
  }
};

// AI 流式：先收翻译（[[TRANS]]..[[/TRANS]] 标志先出 → 立刻显示），再收语法点（[[POINTS]] JSON [[/POINTS]]）
async function _streamGrammar(block, sentence, text) {
  if (!block) return;
  let acc = '', transDone = false, pointsDone = false;
  const tryParse = () => {
    if (!transDone) {
      const tm = acc.match(/\[\[TRANS\]\]([\s\S]*?)\[\[\/TRANS\]\]/);
      if (tm) { _setBlockTrans(block, tm[1].trim()); transDone = true; }
    }
    if (!pointsDone) {
      const pm = acc.match(/\[\[POINTS\]\]([\s\S]*?)\[\[\/POINTS\]\]/);
      if (pm) { _setBlockPoints(block, pm[1].trim()); pointsDone = true; }
    }
  };
  // 抗断连：SSE 主路 + 切后台/网抖回退轮询（后台线程跑完，标志解析照常）
  const res = await _aiStream('/pdf/api/grammar-stream', {
    method: 'POST',
    body: {sentence, text, file: FILE_REL, enabled_books: _grammarEnabledBooks},
    onText: (t) => { acc = t; tryParse(); },
  });
  if (!res.ok && !acc) { _setBlockTransFail(block); return; }
  acc = res.text || acc; tryParse();
  // 兜底：流结束仍未解析到标志，尽量从残文里抠
  if (!transDone) {
    const tm = acc.match(/\[\[TRANS\]\]([\s\S]*?)(\[\[\/TRANS\]\]|\[\[POINTS\]\]|$)/);
    if (tm && tm[1].trim()) _setBlockTrans(block, tm[1].trim());
    else _setBlockTransFail(block);
  }
  if (!pointsDone) {
    const pm = acc.match(/\[\[POINTS\]\]([\s\S]*?)(\[\[\/POINTS\]\]|$)/);
    _setBlockPoints(block, pm ? pm[1].trim() : '[]');
  }
  // 分析完成 → 保存到该 PDF 的历史（按书本持久化）
  try {
    const sp = block.__spacy || {};
    fetch('/pdf/api/grammar-history-save', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({file: FILE_REL, item: {
        sentence, text,
        sentence_zh: block.__zh || '',
        tokens: sp.tokens || [], deps: sp.deps || [], clauses: sp.clauses || [],
        components: sp.components || [], clause_tree: sp.clause_tree || null,
        analyses: block.__points || [],
      }}),
    }).catch(() => {});
  } catch (e) {}
}
function _setBlockTrans(block, zh) {
  // 只填常驻翻译行；标题保持原文不动
  block.__zh = zh;
  const el = block.querySelector('.gb-trans');
  if (el) { el.classList.remove('gb-pending'); el.textContent = '🌐 ' + zh; }
}
function _setBlockTransFail(block) {
  const el = block.querySelector('.gb-trans');
  if (el) { el.classList.remove('gb-pending'); el.textContent = '🌐 （翻译失败，可重试）'; }
}
function _setBlockPoints(block, jsonStr) {
  const wrap = block.querySelector('.gb-analyses');
  if (!wrap) return;
  let arr = [];
  try { arr = JSON.parse(jsonStr) || []; } catch { arr = []; }
  block.__points = arr;   // 存下来供历史保存
  if (!arr.length) { wrap.remove(); return; }   // 没语法点就移除占位
  wrap.classList.remove('gb-pending');
  wrap.innerHTML = arr.map(a => `
    <div class="gb-ana">
      <div class="a-head">📊 ${_esc(a.point || a.node_name || '')}</div>
      ${a.phrase ? `<div class="a-phrase">📍 ${_esc(a.phrase)}</div>` : ''}
      <div class="a-body">${_esc(a.explanation || '')}${(a.examples||[]).length ? `<ul class="a-ex">${(a.examples||[]).map(e=>'<li>'+_esc(e)+'</li>').join('')}</ul>` : ''}</div>
    </div>`).join('');
  wrap.querySelectorAll('.gb-ana').forEach(el => el.addEventListener('click', () => el.classList.toggle('open')));
  // 标题栏角标
  const headEl = block.querySelector('.gb-header');
  if (headEl && !headEl.querySelector('.gb-badge')) {
    const badge = document.createElement('span');
    badge.className = 'gb-badge';
    badge.textContent = '语法点 ' + arr.length;
    headEl.insertBefore(badge, headEl.querySelector('.gb-del'));
  }
}

// ── 抽屉开关（流式堆叠，无左右对齐/滚动同步）──
function openGrammarPanel() {
  const p = document.getElementById('grammar-panel');
  if (!p) return;
  if (!p.classList.contains('open')) {
    p.classList.add('open');
    document.body.classList.add('grammar-open');
    // 双页模式下侧栏挤窄 → 两页并排太小 → **临时**切单列(不写 localStorage/orient),关栏自动还原双页。
    // 但悬浮显示模式抽屉盖在正文上、不挤压 → 页面不变窄 → 保持双页不切。
    if (readMode === 'spread' && !document.body.classList.contains('grammar-floating')) {
      _spreadBeforePanel = _spreadOffset;
      readMode = 'continuous';
      _updateModeButtons();
    }
    // 打开侧栏让 #main 变窄 → 重算 scale(debounce,挪出动画帧 + 与 ResizeObserver 去重)。
    // 悬浮模式 #main 宽度不变 → 不重排(重排会让背后 PDF 重渲染→闪烁);仅挤压模式重排。
    if (!document.body.classList.contains('grammar-floating')) _scheduleRefit(true);
  }
  // 展开时若停在语法 tab 且历史未载 → 主动载（刷新后默认语法 tab，不点切换也能显示记录）
  const _onGr = document.querySelector('#side-tabs .side-tab[data-pane="grammar"]')?.classList.contains('active');
  if (_onGr && !_grammarHistLoaded && typeof loadGrammarHistory === 'function') loadGrammarHistory();
}
window.closeGrammarPanel = () => {
  document.getElementById('grammar-panel')?.classList.remove('open');
  document.body.classList.remove('grammar-open');
  _hideDepTip();
  // 还原侧栏打开时临时切走的双页
  if (_spreadBeforePanel != null) {
    readMode = 'spread';
    _spreadOffset = _spreadBeforePanel;
    _spreadBeforePanel = null;
    _updateModeButtons();
  }
  if (!document.body.classList.contains('grammar-floating')) _scheduleRefit(true);   // 悬浮模式宽度不变→不重排(免闪);挤压才重排
};

// ── 右栏外观设置：悬浮显示 + 背景模糊度。**按排版(continuous/spread)分别记忆**(用户要两种排版各存一套)──
// 键：pdf-gp-{floating,blur}-{continuous|spread}；缺则回退老的全局键(老用户迁移),再回退默认。切排版/转屏后重应用。
function _gpMode() { return (typeof readMode !== 'undefined' && readMode === 'spread') ? 'spread' : 'continuous'; }
function _gpGet(name, def) {
  let v = localStorage.getItem('pdf-gp-' + name + '-' + _gpMode());
  if (v === null) v = localStorage.getItem('pdf-gp-' + name);   // 迁移:老全局键
  return v === null ? def : v;
}
function _gpSyncUI() {
  const f = document.getElementById('gp-floating'); if (f) f.checked = _gpGet('floating', '0') === '1';
  const b = parseInt(_gpGet('blur', '20'), 10);
  const bi = document.getElementById('gp-blur'), bv = document.getElementById('gp-blur-val');
  if (bi) bi.value = b; if (bv) bv.textContent = b;
}
function _gpApplyAppearance() {
  document.body.classList.toggle('grammar-floating', _gpGet('floating', '0') === '1');
  document.documentElement.style.setProperty('--gp-blur', parseInt(_gpGet('blur', '20'), 10) + 'px');
  _gpSyncUI();
}
window._gpApplyAppearance = _gpApplyAppearance;   // 切排版(toggleReadMode/toggleSpread)/旋转后调,重应用本排版的侧栏外观
window._gpSetFloating = (on) => {
  localStorage.setItem('pdf-gp-floating-' + _gpMode(), on ? '1' : '0');
  document.body.classList.toggle('grammar-floating', !!on);
  if (typeof _scheduleRefit === 'function') _scheduleRefit(true);   // 悬浮↔挤压 → #main 宽度变 → 重排
};
window._gpSetBlur = (v) => {
  localStorage.setItem('pdf-gp-blur-' + _gpMode(), String(v));
  document.documentElement.style.setProperty('--gp-blur', v + 'px');
  const el = document.getElementById('gp-blur-val'); if (el) el.textContent = v;
};
window.toggleSideSettings = (ev) => {
  if (ev) ev.stopPropagation();
  const m = document.getElementById('side-settings'); if (!m) return;
  if (m.style.display === 'block') { m.style.display = 'none'; return; }
  _gpSyncUI();
  m.style.display = 'block';
};
document.addEventListener('pointerdown', (e) => {   // 点弹层外部 → 关
  const m = document.getElementById('side-settings');
  if (m && m.style.display === 'block' && !m.contains(e.target) && !e.target.closest('#side-settings-btn')) {
    m.style.display = 'none';
  }
}, true);
_gpApplyAppearance();   // 载入即应用持久化设置
// 顶栏「📊 语法」按钮：打开统一面板并切到语法 tab（再点同 tab 则关闭）
window.toggleGrammarPanel = () => {
  const p = document.getElementById('grammar-panel');
  if (!p) return;
  // 默认开「助手」tab(用户要求):开着且已在助手 → 关;否则开 + 切到助手
  const onAsst = document.querySelector('#side-tabs .side-tab[data-pane="asst"]')?.classList.contains('active');
  if (p.classList.contains('open') && onAsst) { closeGrammarPanel(); return; }
  openGrammarPanel();
  switchSideTab('asst');
  try { window.__renderFigChips && window.__renderFigChips(); } catch (_) {}   // 补渲已带入的图附件条
  try { window.__renderFocusSel && window.__renderFocusSel(); } catch (_) {}   // 补渲焦点选区(公式/段落)chip
};
// 清空侧栏内全部分析卡
window.clearGrammarBlocks = () => {
  document.querySelectorAll('#grammar-panel-body .grammar-block').forEach(b => b.remove());
};

// ── POS 配色 + 中文短标签（displaCy 风格）──
const POS_COLORS = {
  noun:'#3b82f6', verb:'#ef4444', adj:'#22c55e', adv:'#a855f7',
  pron:'#ec4899', prep:'#06b6d4', det:'#64748b', conj:'#eab308',
  aux:'#f97316', num:'#14b8a6', part:'#8b5cf6', intj:'#f43f5e', punct:'#475569',
};
const POS_LABEL = {
  noun:'名', verb:'动', adj:'形', adv:'副', pron:'代', prep:'介',
  det:'限', conj:'连', aux:'助', num:'数', part:'小品', intj:'叹', punct:'标',
};
function _posColor(p){ return POS_COLORS[(p||'').toLowerCase()] || '#64748b'; }
function _esc(s){ return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

// ── 分析块：标题栏（可点击折叠/展开）+ 内容区；流式堆叠，最新插到最上 ──
function _addLoadingBlock(id, sentence, text) {
  const body = document.getElementById('grammar-panel-body');
  const block = document.createElement('div');
  block.className = 'grammar-block focus open';   // loading 时默认展开看进度
  block.id = id;
  const summary = sentence.slice(0, 60) + (sentence.length > 60 ? '…' : '');
  block.dataset.src = sentence;          // 原文，标题永远显示它
  block.dataset.text = text || '';       // 用户选中的焦点子串（删卡清缓存要用）
  block.innerHTML =
    `<div class="gb-header">
       <span class="gb-title" title="${_esc(sentence)}">${_esc(summary)}</span>
       <span class="gb-del" title="删除这条（同句下次重新分析）">🗑</span>
       <span class="gb-caret">▶</span>
     </div>
     <div class="gb-trans gb-pending">🌐 翻译中…</div>
     <div class="gb-content"><div class="gb-loading">⏳ 结构 / 语法分析中…</div></div>
     <div class="gb-fu-answers"></div>
     <div class="gb-followup">
       <input class="gb-fu-input" placeholder="继续追问这句的语法…" onkeydown="if(event.key==='Enter'){event.preventDefault();_grammarFollowup('${id}');}">
       <button onclick="_grammarFollowup('${id}')">追问</button>
       <button class="gb-anki-btn" onclick="_grammarAnki('${id}')" title="整句+译文+分析做成 Anki 卡">🎴</button>
     </div>`;
  block.querySelector('.gb-header').addEventListener('click', () => block.classList.toggle('open'));
  block.querySelector('.gb-del').addEventListener('click', (e) => {
    e.stopPropagation();
    // 真删除：清后端语法分析缓存 + 历史，下次同句从头生成
    fetch('/pdf/api/grammar-forget', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        sentence: block.dataset.src || '', text: block.dataset.text || '',
        file: FILE_REL, enabled_books: _grammarEnabledBooks,
      }),
    }).catch(() => {});
    block.remove();
  });
  // 移除同句旧卡（历史已渲染过的同句，避免重复）
  body.querySelectorAll('.grammar-block').forEach(b => { if (b !== block && b.dataset.src === sentence) b.remove(); });
  body.insertBefore(block, body.firstChild);   // 最新在最上
  body.scrollTop = 0;
  return block;
}
function _fillBlockError(block, msg) {
  if (!block) return;
  const content = block.querySelector('.gb-content') || block;
  content.innerHTML = `<div class="gb-loading" style="color:#ef4444">分析失败：${_esc(msg)}</div>`;
}
// 语法卡片「🎴」：整句 + 译文 + 分析 + 追问 → 一张 Anki 卡（复用后台制卡，带原文出处链接）
window._grammarAnki = async (blockId) => {
  const block = document.getElementById(blockId); if (!block) return;
  const sentence = block.dataset.src || '';
  const zh = (block.querySelector('.gb-trans')?.textContent || '').replace(/^🌐\s*/, '').trim();
  const analysis = (block.querySelector('.gb-content')?.textContent || '').trim();
  const fu = (block.querySelector('.gb-fu-answers')?.textContent || '').trim();
  if (!sentence && !analysis) { _toast && _toast('没有可制卡的内容'); return; }
  const srcUrl = FILE_REL ? (location.origin + '/pdf/view?file=' + encodeURIComponent(FILE_REL) + '&page=' + (currentPage || 1)) : '';
  const text = `【句子】${sentence}` + (zh ? `\n【译文】${zh}` : '') + (analysis ? `\n【语法分析】${analysis}` : '')
    + (fu ? `\n【追问】${fu}` : '') + (srcUrl ? `\n【原文出处链接（务必原样放进卡片背面，做成可点链接）】${srcUrl}` : '');
  const jobUi = _startBgJob('制 Anki 中…');
  try {
    const ov = _getAiOverrides();
    const r = await fetch('/pdf/api/snippets-to-async', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ snippets: [{text, source: sentence}], make_note: false, make_anki: true, note_name: '', model: ov.model || '', effort: ov.effort || '' }),
    });
    const d = await r.json();
    if (!d.ok || !d.job_id) { _failBgJob(jobUi, d.error || '提交失败', null); return; }
    _pollJob(d.job_id, jobUi, null);
  } catch (e) { _failBgJob(jobUi, e.message, null); }
};
// 语法卡片底部「继续追问」：带原句 + 译文 + 已有分析作上下文，流式回答追加到卡片内
window._grammarFollowup = async (blockId) => {
  const block = document.getElementById(blockId); if (!block) return;
  const inp = block.querySelector('.gb-fu-input');
  const q = (inp && inp.value || '').trim(); if (!q) return;
  inp.value = '';
  const sentence = block.dataset.src || '';
  const trans = (block.querySelector('.gb-trans')?.textContent || '').replace(/^🌐\s*/, '').trim();
  const analysis = (block.querySelector('.gb-content')?.textContent || '').slice(0, 3000);
  const prev = (block.querySelector('.gb-fu-answers')?.textContent || '').slice(-1500);
  const context = '【句子】' + sentence + (trans ? '\n【译文】' + trans : '')
    + '\n【已有语法分析】\n' + analysis + (prev ? '\n【之前的追问】\n' + prev : '');
  const ans = block.querySelector('.gb-fu-answers');
  const qDiv = document.createElement('div'); qDiv.className = 'gb-fu-q'; qDiv.textContent = '问：' + q; ans.appendChild(qDiv);
  const aDiv = document.createElement('div'); aDiv.className = 'gb-fu-a'; aDiv.innerHTML = '<span class="gb-loading">⏳</span>'; ans.appendChild(aDiv);
  try {
    const ov = _getAiOverrides();
    // 为什么:原手写 SSE 循环每个 chunk 都全文重渲+MathJax 排版(零节流卡主线程,且断连丢结果);
    // 改用 _aiStream(同 17-highlight _followupAsk):自带 80ms 节流 + 非 SSE JSON 兜底 + rid 断连轮询。
    const render = (t) => {
      aDiv.innerHTML = md(t || ' ');
      if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([aDiv]).catch(() => {});
    };
    const res = await _aiStream('/pdf/api/explain', {
      method: 'POST', onText: render,
      body: {text: q, context: '基于这句的语法分析继续回答：\n' + context, model: ov.model || '', effort: ov.effort || ''},
    });
    if (res.ok && res.text) render(res.text);
    else if (!res.ok) aDiv.innerHTML = '追问失败：' + _esc(res.error || '失败');
    else aDiv.innerHTML = '(无回答)';
  } catch (e) { aDiv.innerHTML = '追问失败：' + _esc(e.message); }
};
function _fillGrammarBlock(block, d, sentence) {
  if (!block) return;
  const tokens = d.tokens || [];
  const deps = d.deps || [];
  const sentence_zh = d.sentence_zh || '';
  const analyses = d.analyses || [];
  const components = d.components || [];
  if (!tokens.length && !sentence_zh && !analyses.length && !components.length) {
    _fillBlockError(block, d.raw ? 'AI 未返回结构化结果' : '无结果');
    return;
  }
  block.__spacy = {tokens, deps, clauses: d.clauses || [], components, clause_tree: d.clause_tree};   // 存供历史保存
  const isSpacy = d.engine === 'spacy';   // spaCy 路径：翻译/语法点稍后由 SSE 流式填
  // 标题永远是原文（不被翻译覆盖）；翻译填到 header 外的常驻 gb-trans 行（折叠也可见）
  const transEl = block.querySelector('.gb-trans');
  if (transEl) {
    if (sentence_zh) { transEl.classList.remove('gb-pending'); transEl.textContent = '🌐 ' + sentence_zh; }
    else if (!isSpacy) { transEl.remove(); }   // AI 兜底路径但无翻译 → 移除占位；spaCy 保持「翻译中」等 SSE
  }
  const headEl = block.querySelector('.gb-header');
  if (headEl && analyses.length && !headEl.querySelector('.gb-badge')) {
    const badge = document.createElement('span');
    badge.className = 'gb-badge';
    badge.textContent = '语法点 ' + analyses.length;
    headEl.insertBefore(badge, headEl.querySelector('.gb-del'));
  }
  // 句子成分分块（主谓宾定状从句彩色块）；无 components 时回退依存图
  const hasStruct = components.length || tokens.length;
  const gvHtml = GV_MODES.map(([m, l]) => `<button type="button" data-gv="${m}" class="${m === _grammarViewMode ? 'active' : ''}">${l}</button>`).join('');
  const diagramHtml = hasStruct
    ? `<div class="gb-diagram-wrap"><div class="gb-diagram-toggle">📐 句子结构<span class="gv-switch">${gvHtml}</span><span class="dg-caret">▶</span></div><div class="gb-diagram"></div></div>`
    : '';
  // 语法点区：AI 路径直接渲染；spaCy 路径先占位等 SSE
  const anaHtml = analyses.length
    ? `<div class="gb-analyses">${analyses.map((a,i)=>`
        <div class="gb-ana" data-i="${i}">
          <div class="a-head">📊 ${_esc(a.node_name||a.node_id||'')}</div>
          ${a.phrase?`<div class="a-phrase">📍 ${_esc(a.phrase)}</div>`:''}
          <div class="a-body">${_esc(a.explanation||'')}${(a.examples||[]).length?`<ul class="a-ex">${(a.examples||[]).map(e=>'<li>'+_esc(e)+'</li>').join('')}</ul>`:''}</div>
        </div>`).join('')}</div>`
    : (isSpacy ? `<div class="gb-analyses gb-pending"><div class="gb-ana"><div class="a-head" style="color:#7a8497;font-weight:400">⏳ 语法点分析中…</div></div></div>` : '');
  const content = block.querySelector('.gb-content');
  content.innerHTML = diagramHtml + anaHtml;
  if (hasStruct) {
    const wrap = content.querySelector('.gb-diagram-wrap');
    const diag = content.querySelector('.gb-diagram');
    _renderStructInto(diag, wrap, {tokens, deps, clauses: d.clauses || [], components});
    content.querySelector('.gb-diagram-toggle').addEventListener('click', () => wrap.classList.toggle('dgram-open'));
    // 标题栏内的模式切换按钮（树/块/干/弧），点击切全局模式、不触发折叠
    content.querySelectorAll('.gv-switch button').forEach(btn => btn.addEventListener('click', (e) => {
      e.stopPropagation();
      setGrammarView(btn.dataset.gv);
    }));
  }
  content.querySelectorAll('.gb-ana').forEach(el => el.addEventListener('click', () => el.classList.toggle('open')));
  // 刚分析的块保持展开 + 高亮 5s，之后取消高亮（仍保持用户的展开/折叠状态）
  setTimeout(() => block.classList.remove('focus'), 5000);
}

// 长句结构显示模式：'components' 成分分块 / 'deps' 依存弧线图
const GV_MODES = [['tree', '树'], ['components', '块'], ['skeleton', '主干'], ['deps', '弧线']];
let _grammarViewMode = localStorage.getItem('pdf-grammar-view') || 'components';
window.setGrammarView = (mode) => {
  _grammarViewMode = ['deps', 'skeleton', 'components', 'tree'].includes(mode) ? mode : 'components';
  try { localStorage.setItem('pdf-grammar-view', _grammarViewMode); } catch (_) {}
  // 立即用新模式重渲染已显示的所有语法卡的结构区
  document.querySelectorAll('#grammar-panel-body .grammar-block').forEach(b => {
    if (!b.__spacy) return;
    const diag = b.querySelector('.gb-diagram'), wrap = b.querySelector('.gb-diagram-wrap');
    if (diag) _renderStructInto(diag, wrap, b.__spacy);
  });
  // 同步所有卡标题栏的模式按钮高亮 + 设置面板下拉
  document.querySelectorAll('.gv-switch button').forEach(b => b.classList.toggle('active', b.dataset.gv === _grammarViewMode));
  const sel = document.getElementById('set-grammar-view');
  if (sel) sel.value = _grammarViewMode;
};
// 统一结构渲染：按 _grammarViewMode 选 成分分块 / 主干折叠 / 依存图
function _renderStructInto(diag, wrap, sp) {
  diag.innerHTML = '';
  const comps = sp.components || [], toks = sp.tokens || [], deps = sp.deps || [], clauses = sp.clauses || [];
  if (_grammarViewMode === 'tree' && comps.length) {
    diag.appendChild(_renderTree(comps)); wrap?.classList.add('dgram-open');
  } else if (_grammarViewMode === 'skeleton' && comps.length) {
    diag.appendChild(_renderSkeleton(comps)); wrap?.classList.add('dgram-open');
  } else if (_grammarViewMode === 'components' && comps.length) {
    diag.appendChild(_renderComponents(comps)); wrap?.classList.add('dgram-open');
  } else if (sp.clause_tree) {
    // 可逐级展开的弧线：主句弧线 + 占位节点点开看从句弧线
    diag.appendChild(_renderDepTree(sp.clause_tree));
  } else if (toks.length) {
    // 旧数据(无 clause_tree)回退：整句单图 / 从句平铺分段
    if (clauses.length > 1) {
      for (const c of clauses) {
        if (!(c.tokens || []).length) continue;
        const seg = document.createElement('div');
        seg.className = 'dep-clause';
        const lbl = document.createElement('div');
        lbl.className = 'dep-clause-label';
        lbl.textContent = c.label || '从句';
        seg.appendChild(lbl);
        seg.appendChild(_renderDepSvg(c.tokens, c.deps || []));
        diag.appendChild(seg);
      }
    } else {
      diag.appendChild(_renderDepSvg(toks, deps));
    }
    diag.addEventListener('wheel', (e) => {
      if (diag.scrollWidth <= diag.clientWidth) return;
      const dy = Math.abs(e.deltaY) > Math.abs(e.deltaX) ? e.deltaY : e.deltaX;
      diag.scrollLeft += dy; e.preventDefault();
    }, {passive: false});
  }
}
// 主干+修饰折叠：核心成分(主谓宾表)行内显示，修饰/从句折叠成可点 chip
function _renderSkeleton(comps) {
  const CORE = new Set(['主语', '谓语', '宾语', '间接宾语', '表语', '宾语补足语', '形式主语']);
  const wrap = document.createElement('div');
  wrap.className = 'sk-blocks';
  for (const c of comps) {
    const label = c.label || '';
    if (CORE.has(label)) {
      const s = document.createElement('span');
      s.className = 'sk-core';
      s.textContent = c.text || '';
      wrap.appendChild(s);
    } else {
      const chip = document.createElement('span');
      chip.className = 'sk-mod';
      chip.style.color = _compColor(label);
      chip.dataset.text = c.text || '';
      chip.dataset.label = label;
      chip.textContent = '[' + label + ' …]';
      chip.addEventListener('click', () => {
        const open = chip.classList.toggle('open');
        chip.textContent = open ? ('[' + label + '：' + chip.dataset.text + ']') : ('[' + label + ' …]');
      });
      wrap.appendChild(chip);
    }
    wrap.appendChild(document.createTextNode(' '));
  }
  return wrap;
}

// ── 成分树（融合）：折叠看整句、点开逐级展开成分细节 ──
//   折叠态：显示该成分的「整体文本」(自己+所有后代按位置拼) + 成分标签
//   展开态：显示「自有核心」+ 缩进的下级成分(各自又可再展开)
function _renderTree(comps) {
  const children = {};
  comps.forEach((c, i) => {
    const p = (c.parent == null ? -1 : c.parent);
    (children[p] = children[p] || []).push(i);
  });
  const fullText = (idx) => {   // idx 及其所有后代的自有文本，按出现位置拼成整句片段
    const acc = [];
    const collect = (i) => { acc.push(comps[i]); (children[i] || []).forEach(collect); };
    collect(idx);
    acc.sort((a, b) => (a.start || 0) - (b.start || 0));
    return acc.map(c => c.text || '').join(' ');
  };
  const build = (idx) => {
    const c = comps[idx], col = _compColor(c.label || '');
    const kids = children[idx] || [];
    const node = document.createElement('div');
    node.className = 'ctree-node';
    const row = document.createElement('div');
    row.className = 'ct-row';
    const txt = document.createElement('span');
    txt.className = 'ct-text';
    txt.textContent = kids.length ? fullText(idx) : (c.text || '');   // 有子→折叠显示整体
    row.innerHTML = `<span class="ct-label" style="background:${col}">${_esc(c.label || '')}</span>`;
    row.appendChild(txt);
    node.appendChild(row);
    const isClause = (c.label || '').includes('从句');
    if (kids.length) {
      const car = document.createElement('span');
      car.className = 'ct-caret'; car.textContent = '▸';   // 默认折叠
      row.appendChild(car);
      const box = document.createElement('div');
      box.className = 'ct-children';
      box.style.display = 'none';
      // 从句类节点：展开后把从句谓语(自有核心)作为「谓语」子项，与主/宾/状平级
      if (isClause && (c.text || '').trim()) {
        const pred = document.createElement('div');
        pred.className = 'ctree-node';
        pred.innerHTML = `<div class="ct-row"><span class="ct-label" style="background:${_compColor('谓语')}">谓语</span><span class="ct-text">${_esc(c.text)}</span></div>`;
        box.appendChild(pred);
      }
      kids.forEach(k => box.appendChild(build(k)));
      node.appendChild(box);
      const toggle = (e) => {
        e.stopPropagation();
        const open = box.style.display === 'none';
        box.style.display = open ? '' : 'none';
        car.textContent = open ? '▾' : '▸';
        // 从句类展开后自身只留标签(谓语已挪进子)；短语类展开看核心
        txt.textContent = open ? (isClause ? '' : (c.text || '')) : fullText(idx);
        txt.classList.toggle('ct-core', open && !isClause);
      };
      car.addEventListener('click', toggle);
      txt.style.cursor = 'pointer';
      txt.addEventListener('click', toggle);
    }
    return node;
  };
  const wrap = document.createElement('div');
  wrap.className = 'ctree';
  (children[-1] || []).forEach(i => wrap.appendChild(build(i)));
  return wrap;
}

// ── 句子成分分块：主谓宾定状从句彩色块（无弧线、可换行，长句清晰）──
function _compColor(label) {
  if (label.includes('谓语')) return '#ef4444';
  if (label.includes('主语')) return '#3b82f6';
  if (label.includes('宾语')) return '#22c55e';
  if (label.includes('定语')) return '#06b6d4';
  if (label.includes('状语')) return '#a855f7';
  if (label.includes('表语') || label.includes('补语')) return '#eab308';
  if (label.includes('并列')) return '#94a3b8';
  return '#8a9bb4';
}
function _renderComponents(comps) {
  const wrap = document.createElement('div');
  wrap.className = 'comp-blocks';
  for (const c of comps) {
    const col = _compColor(c.label || '');
    const b = document.createElement('div');
    b.className = 'comp-block';
    b.style.borderLeftColor = col;
    b.innerHTML = `<span class="comp-label" style="background:${col}">${_esc(c.label || '')}</span><span class="comp-text">${_esc(c.text || '')}</span>`;
    wrap.appendChild(b);
  }
  return wrap;
}

// ── displaCy 依存图：弧线在词上方、词性着色、词性中文标在词下（无 components 时回退）──
function _renderDepSvg(tokens, deps, onNodeClick) {
  const NS = 'http://www.w3.org/2000/svg';
  const PAD_X = 18, GAP = 40, FONT = 16, POS_GAP = 18;   // 词距/字号加大，更舒展不挤
  const ARC_BASE = 24, ARC_STEP = 19;
  // 量词宽
  const meas = document.createElement('canvas').getContext('2d');
  meas.font = `600 ${FONT}px -apple-system,system-ui,sans-serif`;
  const widths = tokens.map(t => Math.max(meas.measureText(t.text||'').width, 12));
  // 词中心 x
  const cx = []; let x = PAD_X;
  for (let i=0;i<tokens.length;i++){ cx.push(x + widths[i]/2); x += widths[i] + GAP; }
  const totalW = Math.max(x - GAP + PAD_X, 60);
  // 弧线按跨度排序（短弧靠下）
  const arcs = deps.map(dp => ({...dp, span: Math.abs(dp.head - dp.child)})).sort((a,b)=>a.span-b.span);
  const maxSpan = arcs.length ? Math.max(...arcs.map(a=>a.span)) : 1;
  const wordBaseY = ARC_BASE + maxSpan*ARC_STEP + 26;
  const svgH = wordBaseY + POS_GAP + 8;
  const svg = document.createElementNS(NS,'svg');
  svg.setAttribute('width', totalW);
  svg.setAttribute('height', svgH);
  svg.setAttribute('viewBox', `0 0 ${totalW} ${svgH}`);
  const arcBottomY = wordBaseY - FONT - 4;   // 弧线落脚（词上边）
  for (const a of arcs) {
    const x1 = cx[a.head], x2 = cx[a.child];
    const dir = x2 > x1 ? 1 : -1;
    const top = arcBottomY - (ARC_BASE + a.span*ARC_STEP);
    const sx = x1 + dir*3, ex = x2 - dir*3;
    const path = document.createElementNS(NS,'path');
    path.setAttribute('d', `M ${sx} ${arcBottomY} C ${sx} ${top}, ${ex} ${top}, ${ex} ${arcBottomY}`);
    path.setAttribute('class','dep-arc');
    svg.appendChild(path);
    // 箭头指向 child（ex 端）
    const arrow = document.createElementNS(NS,'path');
    arrow.setAttribute('d', `M ${ex-3} ${arcBottomY-5} L ${ex+3} ${arcBottomY-5} L ${ex} ${arcBottomY} Z`);
    arrow.setAttribute('class','dep-arrow');
    svg.appendChild(arrow);
    // 关系标签
    if (a.label) {
      const lbl = document.createElementNS(NS,'text');
      lbl.setAttribute('x',(sx+ex)/2); lbl.setAttribute('y', top-2);
      lbl.setAttribute('text-anchor','middle'); lbl.setAttribute('class','dep-label');
      lbl.textContent = a.label;
      svg.appendChild(lbl);
    }
  }
  for (let i=0;i<tokens.length;i++){
    const t = tokens[i];
    const isClause = (t.pos === 'clause') || (t.ref != null);   // 子从句占位节点
    const col = isClause ? '#7dd3fc' : _posColor(t.pos);
    const g = document.createElementNS(NS,'g');
    g.setAttribute('class', 'dep-token' + (isClause ? ' dep-ph' : ''));
    const bw = widths[i] + 8;
    const bg = document.createElementNS(NS,'rect');
    bg.setAttribute('x', cx[i]-bw/2); bg.setAttribute('y', wordBaseY-FONT);
    bg.setAttribute('width', bw); bg.setAttribute('height', FONT+5);
    bg.setAttribute('rx',4); bg.setAttribute('fill', col); bg.setAttribute('opacity', isClause ? '0.22' : '0.16');
    if (isClause) { bg.setAttribute('stroke', col); bg.setAttribute('stroke-dasharray','3 2'); bg.setAttribute('stroke-width','1'); }
    g.appendChild(bg);
    const w = document.createElementNS(NS,'text');
    w.setAttribute('x', cx[i]); w.setAttribute('y', wordBaseY-2);
    w.setAttribute('text-anchor','middle'); w.setAttribute('class','dep-word');
    w.setAttribute('fill', col);
    w.textContent = t.text || '';
    g.appendChild(w);
    const p = document.createElementNS(NS,'text');
    p.setAttribute('x', cx[i]); p.setAttribute('y', wordBaseY+POS_GAP-2);
    p.setAttribute('text-anchor','middle'); p.setAttribute('class','dep-pos');
    p.textContent = isClause ? '点开▾' : (POS_LABEL[(t.pos||'').toLowerCase()] || t.pos || '');
    g.appendChild(p);
    if (isClause && onNodeClick) {
      g.style.cursor = 'pointer';
      g.addEventListener('click', ev => { ev.stopPropagation(); onNodeClick(i, g); });
    } else if (t.zh) {
      const tip = `${t.text}　${t.zh}`;
      g.addEventListener('mouseenter', ev => _showDepTip(ev, tip));
      g.addEventListener('mousemove', _moveDepTip);
      g.addEventListener('mouseleave', _hideDepTip);
      g.addEventListener('click', ev => { _showDepTip(ev, tip); setTimeout(_hideDepTip, 2600); });
    }
    svg.appendChild(g);
  }
  return svg;
}

// 可逐级展开的弧线树：主句弧线图 + 占位节点点开展开子从句弧线（递归）
function _renderDepTree(tree) {
  const wrap = document.createElement('div');
  wrap.className = 'dep-tree';
  const renderClause = (clause, container) => {
    const seg = document.createElement('div');
    seg.className = 'dep-tlevel';
    const childBox = document.createElement('div');
    childBox.className = 'dep-tchildren';
    const svg = _renderDepSvg(clause.nodes || [], clause.deps || [], (i) => {
      const node = (clause.nodes || [])[i];
      if (!node || node.ref == null) return;
      const exist = childBox.querySelector(`:scope > [data-ref="${node.ref}"]`);
      if (exist) { exist.remove(); return; }   // 再点收起
      const sub = document.createElement('div');
      sub.dataset.ref = node.ref;
      sub.className = 'dep-tsub';
      const lbl = document.createElement('div');
      lbl.className = 'dep-clause-label';
      lbl.textContent = (clause.children[node.ref] || {}).label || '从句';
      sub.appendChild(lbl);
      renderClause(clause.children[node.ref], sub);
      childBox.appendChild(sub);
    });
    // 图比容器宽时横向滚
    const sc = document.createElement('div');
    sc.className = 'dep-tscroll';
    sc.appendChild(svg);
    sc.addEventListener('wheel', (e) => {
      if (sc.scrollWidth <= sc.clientWidth) return;
      sc.scrollLeft += (Math.abs(e.deltaY) > Math.abs(e.deltaX) ? e.deltaY : e.deltaX);
      e.preventDefault();
    }, {passive: false});
    seg.appendChild(sc);
    seg.appendChild(childBox);
    container.appendChild(seg);
  };
  renderClause(tree, wrap);
  return wrap;
}

let _depTipEl = null;
function _showDepTip(ev, text){
  _depTipEl = _depTipEl || document.getElementById('dep-tip');
  if (!_depTipEl) return;
  _depTipEl.textContent = text;
  _depTipEl.style.display = 'block';
  _moveDepTip(ev);
}
function _moveDepTip(ev){
  if (!_depTipEl) return;
  _depTipEl.style.left = ((ev.clientX||0)+12) + 'px';
  _depTipEl.style.top  = ((ev.clientY||0)+14) + 'px';
}
function _hideDepTip(){ if (_depTipEl) _depTipEl.style.display='none'; }

