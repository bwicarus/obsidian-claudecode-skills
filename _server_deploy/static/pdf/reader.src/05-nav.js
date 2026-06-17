async function loadPageNodes(num) {
  try {
    const r = await fetch(`/pdf/api/page-nodes?file=${encodeURIComponent(FILE_REL)}&page=${num}`);
    const d = await r.json();
    const list = d.nodes || [];
    window.__lastPageNodes = list;   // 给语音助手 __voiceContext 做谐音纠错/上下文
    const c = document.getElementById('kg-nodes');
    if (!list.length) {
      c.innerHTML = '<div style="color:#5a6680;font-size:12px">该页无 KG 节点（可能这本书没扫过/或本页不是知识点页）</div>';
      return;
    }
    c.innerHTML = list.map(n => {
      // 只有 grammar KG 的节点能跟踪（跟技能树 toggle-tracked 规则一致）
      const trackBtn = (n.kind === 'grammar')
        ? `<button class="kg-track-btn ${n.tracked ? 'on' : ''}" title="加入/取消语法跟踪"
             onclick="event.stopPropagation(); toggleNodeTrack('${n.book}','${n.id}', this)">${n.tracked ? '★ 跟踪中' : '☆ 跟踪'}</button>`
        : '';
      const lbl = n.numeric_label ? `[${n.numeric_label}] ` : '';
      return `<div class="kg-node ${n.state}">
        <div class="kg-node-main" onclick="window.open('/skilltree/${encodeURIComponent(n.book)}/#${encodeURIComponent('f.'+n.id)}','_blank')">
          <div class="lbl">${lbl}${n.name}</div>
          <div class="sum">${n.summary}</div>
        </div>
        ${trackBtn}
      </div>`;
    }).join('');
  } catch (e) {
    document.getElementById('kg-nodes').innerHTML = '<div style="color:#c00;font-size:12px">加载失败</div>';
  }
  window._refreshVocabIfPage?.();   // 翻页时若单词本在「本页」scope，同步刷新
}

// 知识点卡上的「跟踪」按钮：直接 toggle 该 grammar 节点的 tracked（不用回技能树页面）
window.toggleNodeTrack = async (book, nodeId, btn) => {
  btn.disabled = true;
  try {
    const r = await fetch(`/skilltree/${encodeURIComponent(book)}/api/toggle-tracked`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({node_id: nodeId}),
    });
    const d = await r.json();
    if (d.ok) {
      btn.classList.toggle('on', d.tracked);
      btn.textContent = d.tracked ? '★ 跟踪中' : '☆ 跟踪';
      loadGrammarTracked();   // 刷新工具栏「📊 语法分析」按钮的可用状态
    } else {
      _toast?.(d.error || '操作失败');
    }
  } catch (e) {
    _toast?.('网络错误');
  } finally {
    btn.disabled = false;
  }
};

window.changePage = async (delta) => {
  await renderPage(currentPage + delta);
  _saveLastPosition({page: currentPage, mode: readMode, scale});
};
window.goToPage = async (n) => {
  await renderPage(n);
  _saveLastPosition({page: currentPage, mode: readMode, scale});
};

// ── 助手聊天里点页码 → 跳页 + PDF 底部「↩ 回到第 X 页」(多次跳转仍回最早那页)──
window.__pageBackAnchor = null;
window.jumpWithBack = function (target) {
  target = parseInt(target, 10);
  if (!target || target < 1) return;
  const cur = (typeof currentPage !== 'undefined') ? currentPage : 1;
  if (window.__pageBackAnchor == null && target !== cur) window.__pageBackAnchor = cur;  // 第一次跳:记最早的来处
  goToPage(target);
  if (window.__pageBackAnchor != null && window.__pageBackAnchor !== target) _showPageBackBar(window.__pageBackAnchor);
  else _hidePageBackBar();
};
window.pageGoBack = function () {
  const b = window.__pageBackAnchor;
  window.__pageBackAnchor = null;
  _hidePageBackBar();
  if (b != null) goToPage(b);
};
function _showPageBackBar(p) {
  let bar = document.getElementById('page-back-bar');
  if (!bar) {
    bar = document.createElement('div'); bar.id = 'page-back-bar';
    bar.addEventListener('click', function () { window.pageGoBack(); });
    document.body.appendChild(bar);
    if (!document.getElementById('page-back-bar-css')) {
      const st = document.createElement('style'); st.id = 'page-back-bar-css';
      st.textContent =
        '#page-back-bar{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);z-index:140;display:none;' +
        'background:#1a2540;border:1px solid #3b6db5;color:#cfe6ff;padding:9px 18px;border-radius:20px;font-size:14px;' +
        'cursor:pointer;box-shadow:0 6px 18px rgba(0,0,0,.5);-webkit-tap-highlight-color:transparent;white-space:nowrap}' +
        '#page-back-bar:active{transform:translateX(-50%) scale(.95)}' +
        '.asst-pagelink{color:#7dd3fc;cursor:pointer;border-bottom:1px dashed rgba(125,211,252,.6);padding:0 1px}' +
        '.asst-pagelink:active{color:#bae6fd}';
      document.head.appendChild(st);
    }
  }
  bar.textContent = '↩ 回到第 ' + p + ' 页';
  bar.style.display = 'block';
}
function _hidePageBackBar() { const bar = document.getElementById('page-back-bar'); if (bar) bar.style.display = 'none'; }
// 页码 scrubber:左右拖动快速跳页(全宽≈整本),实时预览;点击(没拖)→ 输入页码
function _setupPageScrub() {
  const el = document.getElementById('page-scrub');
  const pop = document.getElementById('page-scrub-pop');
  if (!el || !pop) return;
  let st = null;
  const numEl = () => pop.querySelector('.psp-num');
  const fillEl = () => pop.querySelector('.psp-fill');
  el.addEventListener('pointerdown', (e) => {
    if (!pdfDoc) return;
    st = {x: e.clientX, p: currentPage, total: pdfDoc.numPages, moved: false, tgt: currentPage};
    try { el.setPointerCapture(e.pointerId); } catch (_) {}
    e.preventDefault();
  });
  el.addEventListener('pointermove', (e) => {
    if (!st) return;
    const dx = e.clientX - st.x;
    if (Math.abs(dx) > 4) st.moved = true;
    if (!st.moved) return;
    const w = Math.max(220, (document.getElementById('main')?.clientWidth || window.innerWidth) * 0.7);
    let tgt = Math.round(st.p + dx / w * st.total);
    tgt = Math.max(1, Math.min(st.total, tgt));
    st.tgt = tgt;
    pop.style.display = 'flex';
    numEl().textContent = tgt + ' / ' + st.total;
    fillEl().style.width = (tgt / st.total * 100) + '%';
    const pc = document.getElementById('page-cur'); if (pc) pc.textContent = tgt;
    e.preventDefault();
  });
  const end = () => {
    if (!st) return;
    const s = st; st = null;
    pop.style.display = 'none';
    if (s.moved) { goToPage(s.tgt); }
    else {
      const n = prompt('跳到第几页 (1-' + s.total + ')', currentPage);
      const v = parseInt(n);
      if (v) goToPage(Math.max(1, Math.min(s.total, v)));
      else { const pc = document.getElementById('page-cur'); if (pc) pc.textContent = currentPage; }
    }
  };
  el.addEventListener('pointerup', end);
  el.addEventListener('pointercancel', () => { st = null; pop.style.display = 'none'; });
}
// ⚠ 本 module 顶部有 top-level await(import pdf.mjs),它**不延迟 DOMContentLoaded** →
// await 之后(本行)再绑 DOMContentLoaded 已晚、永不触发。故:DOM 已就绪就直接调。
if (document.readyState !== 'loading') _setupPageScrub();
else window.addEventListener('DOMContentLoaded', _setupPageScrub);
window.zoomChange = async (delta) => {
  scale = Math.max(_ZOOM_MIN, Math.min(_scaleMax, scale + delta));
  // +/- 缩放跟双指缩放(_applyZoom)一致:三模式都原地重排(单页=唯一 wrap),原地失败才按模式重建
  if (!(await _rescaleContinuousInPlace())) { if (readMode === 'single') await renderPage(currentPage); else await setupContinuousMode(); }
};
// 宽适应：按 #main 可用宽度重算 scale（取消 ＋/－ 或双指缩放，回到一页刚好铺满宽度）
window.fitWidth = async () => { await _refitToWidth(true); window._rememberOrientLayout?.(); };   // 适应也记进当前方向
// 「📋 知识点」按钮：打开统一面板并切到知识点 tab（再点同 tab 则关闭）
window.toggleSidebar = () => {
  const p = document.getElementById('grammar-panel');
  const onKg = document.querySelector('#side-tabs .side-tab[data-pane="kg"]')?.classList.contains('active');
  if (p.classList.contains('open') && onKg) { closeGrammarPanel(); return; }
  openGrammarPanel();
  switchSideTab('kg');
};
// 切换 tab：语法分析 / 知识点
window.switchSideTab = (pane) => {
  document.querySelectorAll('#side-tabs .side-tab').forEach(t => t.classList.toggle('active', t.dataset.pane === pane));
  document.querySelectorAll('#grammar-panel .side-pane').forEach(p => p.classList.toggle('active', p.dataset.pane === pane));
  // 🗑 清空按钮只在语法 tab 显示
  const clr = document.getElementById('side-clear');
  if (clr) clr.style.display = (pane === 'grammar') ? '' : 'none';
  if (pane === 'vocab' && !_vocabLoaded) loadVocabList();   // 首次进单词本自动载
  if (pane === 'grammar' && !_grammarHistLoaded) loadGrammarHistory();   // 首次进语法载历史
  if (pane === 'kg') loadPageNodes(currentPage);   // 进知识点 tab → 主动拉当前页（连续模式下不靠滚动触发，免得抽屉空要滑动才出）
  if (pane === 'hist') renderQueryHistory();   // 进查询结果历史 tab
};
// 语法分析历史：按书本持久，新旧倒序（最新在上）
let _grammarHistLoaded = false;
window.loadGrammarHistory = async () => {
  _grammarHistLoaded = true;
  try {
    const r = await fetch('/pdf/api/grammar-history?file=' + encodeURIComponent(FILE_REL || ''));
    const d = await r.json();
    const items = d.items || [];   // 后端已按 ts 倒序（新在前）
    for (const it of items) _addHistoryBlock(it);   // append → 新的在最上
  } catch (e) { window.dlog?.('grammar history load fail: ' + e.message); }
};
function _addHistoryBlock(item) {
  const body = document.getElementById('grammar-panel-body');
  if (!body) return;
  const sentence = item.sentence || '', text = item.text || '';
  const block = document.createElement('div');
  block.className = 'grammar-block';   // 历史卡默认折叠
  const _hid = 'gbh_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6);
  block.id = _hid;
  block.dataset.src = sentence; block.dataset.text = text;
  const summary = sentence.slice(0, 60) + (sentence.length > 60 ? '…' : '');
  block.innerHTML =
    `<div class="gb-header">
       <span class="gb-title" title="${_esc(sentence)}">${_esc(summary)}</span>
       <span class="gb-del" title="删除这条（同句下次重新分析）">🗑</span>
       <span class="gb-caret">▶</span>
     </div>
     <div class="gb-trans"></div>
     <div class="gb-content"></div>
     <div class="gb-fu-answers"></div>
     <div class="gb-followup">
       <input class="gb-fu-input" placeholder="继续追问这句的语法…" onkeydown="if(event.key==='Enter'){event.preventDefault();_grammarFollowup('${_hid}');}">
       <button onclick="_grammarFollowup('${_hid}')">追问</button>
       <button class="gb-anki-btn" onclick="_grammarAnki('${_hid}')" title="整句+译文+分析做成 Anki 卡">🎴</button>
     </div>`;
  block.querySelector('.gb-header').addEventListener('click', () => block.classList.toggle('open'));
  block.querySelector('.gb-del').addEventListener('click', (e) => {
    e.stopPropagation();
    fetch('/pdf/api/grammar-forget', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({sentence: block.dataset.src || '', text: block.dataset.text || '', file: FILE_REL, enabled_books: _grammarEnabledBooks})}).catch(() => {});
    block.remove();
  });
  body.appendChild(block);
  _fillGrammarBlock(block, item, sentence);   // item 无 engine → 直接渲染翻译+语法点+依存图(不流式)
}
// 「📒 单词本」按钮入口（顶栏没放按钮，但保留以备调用）
window.toggleVocab = () => {
  const p = document.getElementById('grammar-panel');
  const on = document.querySelector('#side-tabs .side-tab[data-pane="vocab"]')?.classList.contains('active');
  if (p.classList.contains('open') && on) { closeGrammarPanel(); return; }
  openGrammarPanel(); switchSideTab('vocab');
};

// ── 单词本：列表 + 发音 + 加 Anki + 定位页 + 点开释义 ──
let _vocabScope = 'book', _vocabLoaded = false;
window.loadVocabList = async (scope) => {
  if (scope) _vocabScope = scope;
  _vocabLoaded = true;
  const listEl = document.getElementById('vocab-list');
  const cntEl = document.getElementById('vocab-count');
  if (!listEl) return;
  document.querySelectorAll('#vocab-scope-row button').forEach(b => b.classList.toggle('active', b.dataset.scope === _vocabScope));
  listEl.innerHTML = '<div style="color:#5a6680;font-size:12px;padding:10px">加载中…</div>';
  if (cntEl) cntEl.textContent = '';
  try {
    let url = '/pdf/api/vocab-list?file=' + encodeURIComponent(FILE_REL || '') + '&scope=' + _vocabScope;
    if (_vocabScope === 'page') url += '&page=' + (currentPage || 0);
    const r = await fetch(url);
    const d = await r.json();
    const items = d.items || [];
    window.__lastVocab = items;   // 给语音助手 __voiceContext 做谐音纠错/上下文
    if (cntEl) cntEl.textContent = items.length ? (items.length + ' 词') : '';
    if (!items.length) {
      const empty = {page: '本页没查过单词', book: '这本书还没查过单词', all: '单词库为空'}[_vocabScope] || '没有单词';
      listEl.innerHTML = '<div style="color:#5a6680;font-size:12px;padding:10px">' + empty + '</div>';
      return;
    }
    listEl.innerHTML = '';
    for (const it of items) listEl.appendChild(_renderVocabItem(it));
  } catch (e) {
    listEl.innerHTML = '<div style="color:#ef4444;font-size:12px;padding:10px">加载失败：' + e.message + '</div>';
  }
};
window._refreshVocabIfPage = function() {
  const pane = document.getElementById('side-pane-vocab');
  if (pane && pane.classList.contains('active') && _vocabScope === 'page') loadVocabList();
};
function _masteryColor(m) {
  if (m >= 0.8) return '#22c55e';
  if (m >= 0.5) return '#eab308';
  if (m >= 0.2) return '#f97316';
  return '#ef4444';
}
function _speakWord(lemma, audio) {
  if (audio) {
    try {
      const a = new Audio('/pdf/api/vocab-audio?path=' + encodeURIComponent(audio));
      a.play().catch(() => _speakOnline(lemma));
      return;
    } catch (e) {}
  }
  _speakOnline(lemma);
}
// 发音:日语词用浏览器原生日语语音(iPad 自带 Kyoko/Otoya,有道英语库没有日语 → 之前无声);
// 英语词用有道真人 mp3(免 key,覆盖广),失败退化浏览器 en-US TTS。
function _speakOnline(w) {
  if (!w) return;
  if (typeof _isJaWord === 'function' && _isJaWord(w)) { _ttsWord(w, 'ja-JP'); return; }
  try {
    const a = new Audio('https://dict.youdao.com/dictvoice?type=2&audio=' + encodeURIComponent(w));
    a.play().catch(() => _ttsWord(w, 'en-US'));
  } catch (e) { _ttsWord(w, 'en-US'); }
}
function _ttsWord(w, lang) {
  lang = lang || 'en-US';
  try {
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(w);
    u.lang = lang;
    const pref = lang.slice(0, 2).toLowerCase();
    const norm = s => (s || '').toLowerCase().replace('_', '-');
    const vs = speechSynthesis.getVoices() || [];   // iOS 首次可能为空，getVoices 触发加载
    const v = vs.find(x => norm(x.lang) === lang.toLowerCase())
           || vs.find(x => norm(x.lang).startsWith(pref));
    if (v) u.voice = v;
    speechSynthesis.speak(u);
  } catch (e) {}
}
// 暴露到全局:HTML 内联 onclick 在全局作用域执行,模块内的函数声明它够不到(否则按钮无声)
window._ttsWord = _ttsWord;
window._speakOnline = _speakOnline;
window._speakWord = _speakWord;
async function _vocabAddAnki(lemma, btn) {
  const old = btn.textContent;
  btn.textContent = '…'; btn.disabled = true;
  try {
    const r = await fetch('/pdf/api/vocab-anki', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({word: lemma}),
    });
    const d = await r.json();
    if (d.ok) { btn.textContent = '✓ 已加'; btn.classList.add('done'); }
    else { btn.textContent = old; btn.disabled = false; _toast?.('加卡失败：' + (d.error || '?')); }
  } catch (e) { btn.textContent = old; btn.disabled = false; _toast?.('加卡失败：' + e.message); }
}
function _renderVocabItem(it) {
  const div = document.createElement('div');
  div.className = 'vocab-item';
  const pct = Math.round((it.mastery || 0) * 100);
  const col = _masteryColor(it.mastery || 0);
  const pagesHtml = (it.pages || []).slice(0, 12).map(p => `<a data-page="${p}">p${p}</a>`).join('');
  div.innerHTML = `
    <div class="vi-head">
      <span class="vi-word">${_esc(it.lemma)}</span>
      ${it.phonetic ? `<span class="vi-phon">${_esc(it.phonetic)}</span>` : ''}
      <button class="vi-audio" title="发音">🔊</button>
      <span class="vi-mastery-badge" style="background:${col}22;color:${col}">${_esc(it.mastery_label || (pct + '%'))}</span>
    </div>
    <div class="vi-bar"><div style="width:${pct}%;background:${col}"></div></div>
    ${it.zh ? `<div class="vi-zh">${_esc(it.zh)}</div>` : ''}
    <div class="vi-foot">
      <span class="vi-pages">${pagesHtml}</span>
      <button class="vi-anki">${it.has_card ? '✓ 已加' : '📇 加卡'}</button>
    </div>`;
  if (it.has_card) div.querySelector('.vi-anki').classList.add('done');
  div.querySelector('.vi-word').addEventListener('click', () => dictStream(it.lemma, ''));
  div.querySelector('.vi-audio').addEventListener('click', (e) => { e.stopPropagation(); _speakWord(it.lemma, it.audio); });
  div.querySelector('.vi-anki').addEventListener('click', (e) => {
    e.stopPropagation();
    const b = e.currentTarget;
    if (!b.classList.contains('done')) _vocabAddAnki(it.lemma, b);
  });
  div.querySelectorAll('.vi-pages a').forEach(a => a.addEventListener('click', () => {
    const pg = parseInt(a.dataset.page); if (pg) goToPage(pg);
  }));
  return div;
}

// 语音助手上下文:把「当前页能看到的实体」(书目/知识点/生词 + 当前页/书)报给 voice.js,
// 供后端做 speechContexts 发音偏置 + 大脑谐音纠错(按读音映射到真实项)。模块作用域可见 FILE_REL/currentPage/pdfDoc/readMode/BOOK_LANGS。
window.__voiceContext = function () {
  try {
    let sel = '', selSentence = '';
    // 优先 char-layer 选中(lastSelText:阅读器自绘选中,如漫画/PDF 的 OCR 文字层,原生 getSelection 常为空)
    // → 助手才拿得到"用户选中的内容"(修:开助手/点输入框后原生选区被清,但 lastSelText 仍在)。
    // 失效校验:char-layer 选中只认「当前页 + 10 分钟内」的,否则翻到别页后旧选中会被误当成现在在问的内容。
    // 原生选区(getSelection)是实时的,无陈旧问题,作回退。
    try {
      const ls = (typeof lastSelText === 'string' ? lastSelText : '').trim();
      const meta = window.__lastSelMeta;
      const curP = (typeof currentPage !== 'undefined' ? currentPage : -1);
      const fresh = ls && meta && meta.page === curP && (Date.now() - (meta.t || 0) < 600000);
      if (fresh) {
        sel = ls.slice(0, 400);
        selSentence = (typeof window.__lastSelSentence === 'string' ? window.__lastSelSentence : '').trim().slice(0, 600);
      } else {
        const nat = (window.getSelection ? getSelection().toString() : '').trim();
        if (nat) sel = nat.slice(0, 400);
      }
    } catch (_) {}
    let books = [];
    try {
      const c = JSON.parse(localStorage.getItem('pdf-bookshelf-cache') || '[]');
      if (Array.isArray(c)) books = c.map(p => ({ name: p.name, rel: p.rel })).slice(0, 120);
    } catch (_) {}
    const nodes = (window.__lastPageNodes || []).map(n => ({ id: n.id, name: n.name, book: n.book })).slice(0, 60);
    const vocab = (window.__lastVocab || []).map(v => v.lemma).filter(Boolean).slice(0, 80);
    // 带入的图(点/拖进来的 YOLO 图,可多张)。显式带入 → 保留到点 ✕,不做跨页过期。
    // 笔迹**发消息时实时收集**(画在 attach 之后也算),随图带给助手做合成,不依赖服务端墨迹保存时机
    let figures = [];
    try {
      figures = (window.__figAttached || []).filter(a => a && a.box).slice(0, 6).map(a => {
        const ink = (typeof window.__figInk === 'function') ? window.__figInk(a.page, a.box) : [];
        return {
          page: a.page, box: a.box, caption: (a.caption || '').slice(0, 80),
          desc: (a.desc || '').slice(0, 500), group: !!a.group,
          has_ink: ink.length > 0, ink: ink
        };
      });
    } catch (_) {}
    return {
      page_type: 'pdf',
      file_rel: (typeof FILE_REL !== 'undefined' ? FILE_REL : ''),
      book_name: (typeof FILE_REL !== 'undefined' && FILE_REL) ? FILE_REL.split('/').pop() : '',
      page: (typeof currentPage !== 'undefined' ? currentPage : 0),
      pages: (function () {   // 双页模式报当前可见的两页(offset0:1|2,3|4…; offset1:1,2|3,4|5…),否则单页
        try {
          var cp = currentPage;
          if (typeof readMode === 'undefined' || readMode !== 'spread') return [cp];
          var off = (typeof _spreadOffset !== 'undefined') ? _spreadOffset : 0, a;
          if (off === 0) a = (cp % 2) ? cp : cp - 1;
          else a = (cp <= 1) ? 1 : ((cp % 2) ? cp - 1 : cp);
          if (off === 1 && a === 1) return [1];
          return [a, a + 1];
        } catch (_) { return [currentPage]; }
      })(),
      total: (typeof pdfDoc !== 'undefined' && pdfDoc) ? pdfDoc.numPages : 0,
      read_mode: (typeof readMode !== 'undefined' ? readMode : ''),
      langs: (typeof BOOK_LANGS !== 'undefined' ? BOOK_LANGS : []),
      selection: sel,
      selection_sentence: selSentence,
      figures: figures,
      visible_kg_nodes: nodes,
      visible_vocab: vocab,
      books: books,
    };
  } catch (e) { return { page_type: 'pdf', url: location.pathname }; }
};
