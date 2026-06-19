function md(s) {
  s = String(s == null ? '' : s);
  if (window.marked && marked.parse) {
    try {
      // ① 先把数学公式整段抠出来换占位符再跑 marked,否则 marked 会把 $P(A_1)P(A_2)$ 里的
      //    _ 当斜体、* 当强调、\ 当转义拆坏 → 即便模型写了 $...$ 也渲染失败(本 bug 根因之一)。
      //    占位符 @@MJX{n}@@ 纯字母数字,marked 原样保留;渲染后再换回原公式交给 MathJax。
      const math = [];
      const hold = (m) => '@@MJX' + (math.push(m) - 1) + '@@';
      // ② CJK 与 markdown 强调标记紧贴时 marked 不识别(如 接**-ing**) → 插零宽空格给 ** / * / ` 留边界
      const t = s
        .replace(/\$\$[\s\S]+?\$\$/g, hold)               // 块级 $$..$$
        .replace(/\\\[[\s\S]+?\\\]/g, hold)               // 块级 \[..\]
        .replace(/\$(?!\s)(?:\\\$|[^$\n])+?\$/g, hold)     // 行内 $..$(去掉 $$ 后剩的;$ 后须非空白,避开 "$ 5")
        .replace(/\\\([\s\S]+?\\\)/g, hold)                // 行内 \(..\)
        .replace(/([一-鿿　-〿＀-￯])([*`])/g, '$1\u200b$2')
        .replace(/([*`])([一-鿿　-〿＀-￯])/g, '$1\u200b$2');
      let html = marked.parse(t);
      return html.replace(/@@MJX(\d+)@@/g, (_, i) => (math[+i] != null ? math[+i] : ''));   // ③ 还原公式
    } catch(_) {}
  }
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\n/g,'<br>');
}

// ── 查询结果历史（per-book localStorage；openResult/closeResult 快照，📜 历史 tab 回看）──
let _restoringHistory = false;
function _qhistKey() { return 'pdf-qhist-' + (FILE_REL || '_'); }
function _loadQueryHistory() { try { return JSON.parse(localStorage.getItem(_qhistKey()) || '[]'); } catch (_) { return []; } }
function _saveQueryHistory(h) { try { localStorage.setItem(_qhistKey(), JSON.stringify(h)); } catch (_) {} }
function _pushQueryHistory() {
  if (_restoringHistory) return;
  const cont = document.getElementById('result-content'); if (!cont) return;
  const html = cont.innerHTML || '';
  const title = cont.dataset.title || '';
  const src = cont.dataset.src || '';
  if (!title && !src) return;
  if (html.length < 40 || (/⏳|class="loading"/.test(html) && html.length < 120)) return;   // 跳过空/纯 loading
  let h = _loadQueryHistory();
  if (h.length && h[0].src === src && h[0].title === title) { h[0].html = html.slice(0, 60000); h[0].time = Date.now(); }
  else h.unshift({ id: 'qh_' + Date.now(), title, src, html: html.slice(0, 60000), time: Date.now() });
  _saveQueryHistory(h.slice(0, 40));
  if (document.querySelector('#side-tabs .side-tab[data-pane="hist"].active')) renderQueryHistory();
}
function _qhFmtTime(t) {
  const d = new Date(t), n = new Date();
  const hm = String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  return (d.toDateString() === n.toDateString() ? '今天 ' : ((d.getMonth() + 1) + '/' + d.getDate() + ' ')) + hm;
}
window.renderQueryHistory = () => {
  const box = document.getElementById('qhist-list'); if (!box) return;
  const h = _loadQueryHistory();
  if (!h.length) { box.innerHTML = '<div style="color:#5a6680;font-size:12px;padding:10px">还没有查询记录</div>'; return; }
  box.innerHTML = h.map(it =>
    '<div class="qhist-item" data-id="' + it.id + '"><div class="qhist-title">' + _esc(it.title) + '</div>'
    + '<div class="qhist-src">' + _esc((it.src || '').slice(0, 80)) + '</div>'
    + '<div class="qhist-time">' + _qhFmtTime(it.time) + '</div></div>').join('');
  box.querySelectorAll('.qhist-item').forEach(el => el.addEventListener('click', () => {
    const it = _loadQueryHistory().find(x => x.id === el.dataset.id); if (!it) return;
    _restoringHistory = true;
    try { openResult(it.title, it.src, it.html); } finally { _restoringHistory = false; }
    if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([document.getElementById('result-content')]).catch(() => {});
  }));
};

// AI 设置（per-browser localStorage，不动 server-config）
function _getAiOverrides() {
  try { return JSON.parse(localStorage.getItem('pdf-ai-overrides') || '{}'); }
  catch (_) { return {}; }
}
function _toggleAiModelRow() {
  const v = document.getElementById('set-sent-backend').value;
  document.getElementById('set-sent-ai-row').style.display = (v === 'ai') ? '' : 'none';
}
window._toggleAiModelRow = _toggleAiModelRow;

// 设置面板页内 tab 切换(AI·翻译 / 阅读 / 语法 / 高亮),记住上次所在 tab
window._setSettingsTab = (name) => {
  document.querySelectorAll('#settings-mask .set-tab').forEach(t => t.classList.toggle('active', t.dataset.pane === name));
  document.querySelectorAll('#settings-mask .set-pane').forEach(p => { p.style.display = (p.dataset.pane === name) ? '' : 'none'; });
  try { localStorage.setItem('pdf-set-tab', name); } catch (_) {}
};
window.openSettings = () => {
  try { _setSettingsTab(localStorage.getItem('pdf-set-tab') || 'ai'); } catch (_) {}
  const ov = _getAiOverrides();
  document.getElementById('set-model').value = ov.model || '';
  document.getElementById('set-effort').value = ov.effort || '';
  document.getElementById('set-debug').checked = (localStorage.getItem('pdf-debug') === '1');
  const gv = document.getElementById('set-grammar-view');
  if (gv) gv.value = _grammarViewMode;   // 长句结构显示模式
  try { window._populatePageOffsetUI && window._populatePageOffsetUI(); } catch (_) {}   // 页码对齐:填当前页/偏移
  renderGrammarTrackList();   // 拉语法跟踪节点列表
  // 拉句子翻译配置
  fetch('/pdf/api/translate-config').then(r => r.json()).then(d => {
    if (!d.ok) return;
    document.getElementById('set-sent-backend').value = d.backend || 'auto';
    document.getElementById('set-sent-model').value = d.model || 'haiku';
    document.getElementById('set-sent-effort').value = d.effort || 'low';
    _toggleAiModelRow();
  }).catch(()=>{});
  const vu = localStorage.getItem('pdf-vocab-underline');
  document.getElementById('set-vocab-underline').checked = (vu === null) ? true : (vu === '1');
  const ct = localStorage.getItem('pdf-click-translate-unmastered');
  document.getElementById('set-click-translate').checked = (ct === null) ? true : (ct === '1');
  { const e = document.getElementById('set-auto-orient'); if (e) e.checked = (localStorage.getItem('pdf-auto-orient') === '1'); }
  // 去边百分比(本书,从已加载的 _crop 回填)
  { const g = (id, v) => { const e = document.getElementById(id); if (e) e.value = v || 0; };
    g('set-crop-l', _crop.l); g('set-crop-r', _crop.r); g('set-crop-t', _crop.t); g('set-crop-b', _crop.b); }
  // 本书文本语言勾选(每本书独立,从 BOOK_LANGS 回填)
  document.querySelectorAll('#lang-checks input').forEach(c => { c.checked = (BOOK_LANGS || []).includes(c.value); });
  { const e = document.getElementById('set-figures'); if (e) e.checked = !!window.__figBookOn; }   // 本书插图描述开关(每本书独立)
  renderHlColorSetting();
  if (window._initCharOfsPanel) window._initCharOfsPanel();   // 文字层校准块状态
  document.getElementById('settings-mask').style.display = 'flex';
};
window._applyCropSettings = () => {
  const num = (id) => Math.max(0, Math.min(45, parseFloat(document.getElementById(id)?.value) || 0));
  const crop = {l: num('set-crop-l'), r: num('set-crop-r'), t: num('set-crop-t'), b: num('set-crop-b')};
  saveCropSettings(crop, true);   // 存后端 + 自动开启去边 + 重渲染
  closeSettings();
  _toast?.('去边已应用');
};
window.closeSettings = () => { document.getElementById('settings-mask').style.display = 'none'; };
window.saveSettings = async () => {
  window.dlog?.('saveSettings 开始');
  try {
    const ov = {
      model:  document.getElementById('set-model')?.value || '',
      effort: document.getElementById('set-effort')?.value || '',
    };
    try { localStorage.setItem('pdf-ai-overrides', JSON.stringify(ov)); } catch (_) {}
    const dbg = document.getElementById('set-debug')?.checked || false;
    try { localStorage.setItem('pdf-debug', dbg ? '1' : '0'); } catch (_) {}
    const vu = document.getElementById('set-vocab-underline')?.checked;
    if (vu !== undefined) {
      try { localStorage.setItem('pdf-vocab-underline', vu ? '1' : '0'); } catch (_) {}
    }
    const ct = document.getElementById('set-click-translate')?.checked;
    if (ct !== undefined) {
      try { localStorage.setItem('pdf-click-translate-unmastered', ct ? '1' : '0'); } catch (_) {}
    }
    const ao = document.getElementById('set-auto-orient')?.checked;
    if (ao !== undefined) {
      try { localStorage.setItem('pdf-auto-orient', ao ? '1' : '0'); } catch (_) {}
      if (ao) window._rememberOrientLayout?.();   // 刚开启 → 把当前布局记进当前方向作基线
    }
    // 句子翻译配置 POST 到服务端
    const sb = document.getElementById('set-sent-backend');
    const sm = document.getElementById('set-sent-model');
    const se = document.getElementById('set-sent-effort');
    if (sb && sm && se) {
      try {
        const r = await fetch('/pdf/api/translate-config', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({backend: sb.value, model: sm.value, effort: se.value}),
        });
        const d = await r.json();
        window.dlog?.('translate-config POST: ' + (d.ok ? 'OK' : 'FAIL ' + (d.error||'?')));
        if (!d.ok) _toast?.('句子翻译设置保存失败：' + (d.error||'?'));
      } catch (e) {
        window.dlog?.('translate-config POST exception: ' + e.message, '#ff6b6b');
        _toast?.('句子翻译设置保存失败：' + e.message);
      }
    }
    _applyDebugVisibility();
    closeSettings();
    if (pdfDoc) renderPage(currentPage);
    window.dlog?.('saveSettings 完成');
  } catch (ex) {
    window.dlog?.('saveSettings ERROR: ' + ex.message, '#ff6b6b');
    _toast?.('设置保存出错：' + ex.message);
  }
};
function _applyDebugVisibility() {
  const el = document.getElementById('debug-log');
  if (!el) return;
  el.style.display = (localStorage.getItem('pdf-debug') === '1') ? '' : 'none';
}
// 启动时按 localStorage 设 debug 显示（默认隐藏）
if (localStorage.getItem('pdf-debug') !== '1') {
  if (document.readyState !== 'loading') _applyDebugVisibility();
  else document.addEventListener('DOMContentLoaded', _applyDebugVisibility);
  setTimeout(_applyDebugVisibility, 0);
}

// 抗断连流式 AI:SSE 主路(桌面端流式打字),iPad 切后台/网抖断了 → 回退轮询 /api/ai-stream-result
// 拿后台线程已生成的完整文本(断点不丢)。onText(累计全文) 实时回调(内部~80ms 节流)。返回 {ok,text,error}。
// 后端 rid 路由:translate/explain/grammar POST body.rid;dict-jp-ai GET ?rid=。
async function _aiStream(url, opts) {
  opts = opts || {};
  const method = (opts.method || 'GET').toUpperCase();
  const rid = 'r' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  let furl = url, body = null;
  const headers = { 'Accept': 'text/event-stream' };
  if (method === 'GET') {
    furl += (url.indexOf('?') >= 0 ? '&' : '?') + 'rid=' + encodeURIComponent(rid);
  } else {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(Object.assign({}, opts.body || {}, { rid }));
  }
  const onTextRaw = opts.onText || (() => {});
  let acc = '', finished = false, usingPoll = false, lastCb = 0, cbTimer = null;
  const ctrl = new AbortController();
  return new Promise((resolve) => {
    const emit = (force) => {                  // onText 节流(~80ms),结束时强制
      if (finished && !force) return;
      const now = Date.now();
      if (force || now - lastCb >= 80) { lastCb = now; if (cbTimer) { clearTimeout(cbTimer); cbTimer = null; } try { onTextRaw(acc); } catch (_) {} }
      else if (!cbTimer) cbTimer = setTimeout(() => { cbTimer = null; lastCb = Date.now(); try { onTextRaw(acc); } catch (_) {} }, 80 - (now - lastCb));
    };
    const cleanup = () => { document.removeEventListener('visibilitychange', onVis); if (cbTimer) clearTimeout(cbTimer); try { ctrl.abort(); } catch (_) {} };
    const finish = (ok, error) => { if (finished) return; finished = true; try { onTextRaw(acc); } catch (_) {} cleanup(); resolve({ ok, text: acc, error: error || '' }); };
    // 回退轮询:后台线程仍在跑,拉它已生成的完整文本
    let pollN = 0;
    const poll = async () => {
      if (finished) return;
      pollN++;
      try {
        const r = await fetch('/pdf/api/ai-stream-result?id=' + encodeURIComponent(rid));
        const d = await r.json();
        if (typeof d.full === 'string' && d.full.length > acc.length) { acc = d.full; emit(); }
        if (d.status === 'done') return finish(true);
        if (d.status === 'error') return finish(false, d.error || 'AI 失败');
        if (d.status === 'unknown' && pollN > 3) return finish(!!acc, acc ? '' : '任务丢失(服务重启?)');
      } catch (_) { /* 网瞬断:继续 */ }
      if (pollN > 240) return finish(!!acc, acc ? '' : '轮询超时');   // ~5min 兜底
      setTimeout(poll, 1200);
    };
    const startPoll = () => { if (usingPoll || finished) return; usingPoll = true; try { ctrl.abort(); } catch (_) {} poll(); };
    // 切后台→回前台:iOS 挂起 JS 会让 SSE reader 卡死,主动转轮询
    const onVis = () => { if (document.visibilityState === 'visible' && !finished) startPoll(); };
    document.addEventListener('visibilitychange', onVis);
    // SSE 主路
    (async () => {
      try {
        const r = await fetch(furl, { method, headers, body, signal: ctrl.signal });
        const ct = r.headers.get('content-type') || '';
        if (!r.ok || !ct.includes('event-stream')) {   // 服务端没流式(多半错误 JSON) → 当普通 JSON
          let d = {}; try { d = await r.json(); } catch (_) {}
          if (d && d.ok && (d.translation || d.explanation)) { acc = d.translation || d.explanation; return finish(true); }
          return finish(false, (d && d.error) || ('HTTP ' + r.status));
        }
        const reader = r.body.getReader(), dec = new TextDecoder();
        let buf = '';
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          if (usingPoll || finished) return;
          buf += dec.decode(value, { stream: true });
          let idx;
          while ((idx = buf.indexOf('\n\n')) >= 0) {
            if (usingPoll || finished) return;
            const ev = buf.slice(0, idx); buf = buf.slice(idx + 2);
            if (/^event:\s*error/m.test(ev)) { let er = ''; const m = ev.match(/^data:\s*(.*)/m); try { er = JSON.parse(m[1]).error; } catch (_) {} return finish(false, er || 'AI 失败'); }
            if (/^event:\s*done/m.test(ev)) return finish(true);
            const m = ev.match(/^data:\s*(.*)/m);
            if (m) { try { const j = JSON.parse(m[1]); if (j.text) { acc += j.text; emit(); } } catch (_) {} }
          }
        }
        if (!finished && !usingPoll) startPoll();   // 流断了没收到 done → 转轮询补全
      } catch (e) {
        if (!finished && !usingPoll) startPoll();    // SSE 出错/abort → 后台线程仍在跑,转轮询
      }
    })();
  });
}

async function aiCall(path, body, label) {
  const ov = _getAiOverrides();
  if (ov.model)  body.model  = ov.model;
  if (ov.effort) body.effort = ov.effort;
  // 预览框显示「实际要处理的文本」(body.text，短选区已扩成整句) → 预览=输入，所见即所处理
  openResult(label, body.text || body.sentence || lastSelText, '<div class="loading">⏳ AI 处理中…</div>');
  const myReq = _resultReqId;   // 本次 AI 调用的请求序号；被新结果框作废后渲染一律丢弃
  const contentEl = document.getElementById('result-content');
  const render = (text) => {              // 渲染累计全文（marked + MathJax）
    if (myReq !== _resultReqId) return;   // 已被新结果框作废 → 不写回（防延迟流式覆盖新结果）
    contentEl.innerHTML = md(text || ' ');
    if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([contentEl]).catch(() => {});
  };
  try {
    // 抗断连：SSE 主路 + 切后台/网抖回退轮询（后台线程跑完，结果不丢）
    const res = await _aiStream(path, { method: 'POST', body, onText: render });
    if (myReq !== _resultReqId) return;
    render(res.text);
    if (!res.ok) contentEl.innerHTML += '<div style="color:#c00;margin-top:8px">✗ ' + (res.error || '失败') + '</div>';
    addResultPickers();   // 完成后给标题加 +
  } catch (e) {
    if (myReq === _resultReqId) contentEl.innerHTML = '<div style="color:#c00">✗ ' + e.message + '</div>';
  }
}

// 复制选中文本（char-layer 自定义选中，浏览器原生 selection 是空的）
window.onCopySel = async () => {
  const t = (lastSelText || '').trim();
  if (!t) { _toast?.('没有选中内容'); return; }
  try {
    await navigator.clipboard.writeText(t);
    _toast?.('已复制');
  } catch (e) {
    // 回退：临时 textarea + execCommand（http / 无权限场景）
    try {
      const ta = document.createElement('textarea');
      ta.value = t; ta.style.position = 'fixed'; ta.style.left = '-9999px';
      document.body.appendChild(ta); ta.select();
      document.execCommand('copy'); ta.remove();
      _toast?.('已复制');
    } catch (e2) { _toast?.('复制失败'); }
  }
};
// 选区重新识别：文字层坏掉(乱码/上标错/缺符号)时，把选区裁图发 Claude 视觉，拿回正确文字，
// 回填到 lastSelText + 预览 → 之后 复制/翻译/解释/对话 全用校正后的正确文字。
// 选中所在页(选区 char 层属于哪个 page-wrap)。连续滚动下视口居中页 currentPage ≠ 选中页,
// 凡「拿选中位置做事」(翻译浮层贴页/OCR 校正存页/笔记深链/查词日志页)都该用它,否则跨页选时定位到错页。
function _selPageNum() {
  const pw = _charSel && _charSel.pw;
  const n = pw && pw.dataset && parseInt(pw.dataset.pageNum, 10);
  return (n && n > 0) ? n : currentPage;
}
window._selPageNum = _selPageNum;

window.onOcrSel = async () => {
  const pw = _charSel && _charSel.pw;
  if (!pw || !pw.__charBoxes || !lastSelText) { (typeof _toast === 'function') && _toast('先选中文字'); return; }
  const chars = pw.__charBoxes;
  let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9, n = 0;   // 选区并集 bbox(PDF pt)
  for (let i = _charSel.startIdx; i <= _charSel.endIdx; i++) {
    const c = chars[i];
    if (!c || c.sp || c._x0 == null) continue;
    x0 = Math.min(x0, c._x0); y0 = Math.min(y0, c._y0);
    x1 = Math.max(x1, c._x1); y1 = Math.max(y1, c._y1); n++;
  }
  if (!n) { (typeof _toast === 'function') && _toast('选区无效'); return; }
  const selPage = _selPageNum();   // 选中所在页(不能用 currentPage,见 _selPageNum 注释)
  const prev = document.getElementById('sel-preview');
  const old = prev ? prev.innerHTML : '';
  if (prev) prev.innerHTML = '🔎 OCR 识别中…';
  try {
    const ov = _getAiOverrides();
    const r = await __safeFetch('/pdf/api/ocr-selection', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({file: FILE_REL, page: selPage, bbox: [x0, y0, x1, y1],
                            model: ov.model || '', effort: ov.effort || ''}),
    });
    const j = await r.json();
    if (j && j.ok && j.text) {
      lastSelText = j.text;                     // 校正后的文字回填,下游(复制/翻译/解释/对话)全用它
      if (prev) prev.innerHTML = _esc(j.text) + ' <span class="len">OCR ✓ 已写入</span>';
      // 持久化已落库:更新本页 cv(让重渲第一拉就命中新版)+ 立即重渲本页 → 校正注入字符层、永久生效
      if (j.cv) { try { localStorage.setItem('pdf-cv:' + FILE_REL + ':' + selPage, j.cv); } catch (_) {} }
      if (typeof _rerenderLoadedPages === 'function') { try { _rerenderLoadedPages(); } catch (_) {} }
      (typeof _toast === 'function') && _toast('已 OCR 校正并永久写入页面');
    } else {
      if (prev) prev.innerHTML = old;
      (typeof _toast === 'function') && _toast((j && j.error) || 'OCR 失败');
    }
  } catch (e) {
    if (prev) prev.innerHTML = old;
    (typeof _toast === 'function') && _toast('OCR 失败:' + (e && e.message || e));
  }
};

// Ctrl/Cmd+C：char-layer 有选中时，把选中文本写进剪贴板（绕过空的原生 selection）
document.addEventListener('copy', (e) => {
  const t = (lastSelText || '').trim();
  if (!t || !(window.getSelection && String(window.getSelection()).length === 0)) return;
  // 仅当原生 selection 为空(说明是 char-layer 选中)时接管
  e.clipboardData.setData('text/plain', t);
  e.preventDefault();
});

// 从选中 char 范围构造一个"句子标记"对象(几何同 vocab 句子，PDF pt)，供手动翻译标记用
function _buildSentenceFromSel(pw, sIdx, eIdx) {
  const chars = pw.__charBoxes;
  if (!chars || sIdx < 0 || eIdx >= chars.length || sIdx > eIdx) return null;
  // 跟选中预览 _selByCharRange 用**同款块过滤**：排序后选区首尾之间会交错进别气泡/别栏的字，
  // 翻译/解释必须只取起止块区间内的字（= 预览所见），否则译文混进左右气泡内容。
  const _blk = (c) => (c.bk != null && c.bk >= 0) ? c.bk : ((c.w == null || c.w < 0) ? -1 : Math.floor(c.w / 1000000));
  const _sb = _blk(chars[sIdx]), _eb = _blk(chars[eIdx]);
  const _bLo = Math.min(_sb, _eb), _bHi = Math.max(_sb, _eb);
  const _inBlk = (c) => { if (_sb < 0 || _eb < 0) return true; const b = _blk(c); return b < 0 || (b >= _bLo && b <= _bHi); };
  const rects = []; let cur = null, firstC = null, lastC = null, text = '';
  for (let i = sIdx; i <= eIdx; i++) {
    const c = chars[i];
    if (!_inBlk(c)) continue;   // 别块(别气泡/别栏)字符不计入，跟预览严格一致
    if (c.sp) { text += ' '; continue; }
    const x0 = c._x0, y0 = c._y0, x1 = c._x1, y1 = c._y1;
    if (x0 == null) continue;
    if (!firstC) firstC = [x0, y0, x1, y1];
    lastC = [x0, y0, x1, y1];
    text += c.c;
    if (cur && Math.abs(y0 - cur[1]) <= (y1 - y0) * 0.6) {
      cur[2] = Math.max(cur[2], x1); cur[1] = Math.min(cur[1], y0); cur[3] = Math.max(cur[3], y1);
    } else {
      if (cur) rects.push(cur.map(v => +v.toFixed(2)));
      cur = [x0, y0, x1, y1];
    }
  }
  if (cur) rects.push(cur.map(v => +v.toFixed(2)));
  text = text.replace(/\s+/g, ' ').trim();
  if (!rects.length || !text || !firstC) return null;
  return {text, rects, first_char: firstC.map(v => +v.toFixed(2)), last_char: lastC.map(v => +v.toFixed(2)),
          count: 0, lemmas: [], total_words: 0, manual: true};
}

window.onTranslate = async () => {
  if (!lastSelText) return;
  toolbar.classList.remove('open');
  const pw = _charSel && _charSel.pw;
  // 无 char 信息(罕见) → 退回大框 AI 翻译
  if (!pw || !pw.__charBoxes) {
    aiCall('/pdf/api/translate', {text: lastSelText, target_lang: '中文'}, '🌐 翻译');
    return;
  }
  // 选中句 → 生成句子标记(L框/box)，box 呼吸表示翻译中；译完存 sidecar + 自动弹译文浮层
  const sent = _buildSentenceFromSel(pw, _charSel.startIdx, _charSel.endIdx);
  if (!sent) { aiCall('/pdf/api/translate', {text: lastSelText, target_lang: '中文'}, '🌐 翻译'); return; }
  // **所译严格 = 预览(所选)文本**：sent.rects 只用作译文覆盖位置(geometry),要翻译的文字一律
  // 用工具栏预览那串 lastSelText，杜绝「翻译内容跟预览不一致」(_buildSentenceFromSel 重拼可能分歧)。
  { const _pv = (lastSelText || '').replace(/\s+/g, ' ').trim(); if (_pv) sent.text = _pv; }
  sent.__translating = true;
  pw.__vocabSentences = (pw.__vocabSentences || []).filter(s => s.text !== sent.text);
  pw.__vocabSentences.push(sent);
  renderVocabSentences(pw, pw.__vocabSentences);   // 呼吸 box
  try {
    const ov = _getAiOverrides();
    const r = await __safeFetch('/pdf/api/translate-sentence', {   // 幂等翻译:切后台被掐→回前台自动重试
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        text: sent.text, model: ov.model || '', effort: ov.effort || '',
        file: FILE_REL, sentence: {rects: sent.rects, first_char: sent.first_char, last_char: sent.last_char, page: _selPageNum()},
      }),
    }, {retries: 2});
    const d = await r.json();
    sent.__translating = false;
    if (d.ok && d.zh) {
      sent.zh = d.zh;
      renderVocabSentences(pw, pw.__vocabSentences);   // 停呼吸
      setTimeout(() => {   // 翻完自动画就地覆盖(直接画,不依赖找按钮 → 更可靠)
        const layer = pw.querySelector('.vocab-layer');
        if (!layer) return;
        const canvas = pw.querySelector('canvas');
        const cssW = canvas?.clientWidth || pw.clientWidth;
        const cssH = canvas?.clientHeight || pw.clientHeight;
        const sx = cssW / (pw.__pageWPt || cssW), sy = cssH / (pw.__pageHPt || cssH);
        const si = pw.__vocabSentences.indexOf(sent);
        const btn = pw.querySelector('.vocab-sentence-btn-l-start[data-sid="' + si + '"]')
          || {classList: {add() {}, remove() {}}};
        _drawSentenceOverlay(layer, sent, btn, sx, sy);
      }, 60);
    } else {
      renderVocabSentences(pw, pw.__vocabSentences);
      _toast('翻译失败：' + (d.error || '?'));
    }
  } catch (e) {
    sent.__translating = false;
    renderVocabSentences(pw, pw.__vocabSentences);
    _toast('翻译错误：' + e.message);
  }
};
window.onExplain = () => {
  if (!lastSelText) return;
  toolbar.classList.remove('open');
  let context = '';
  let explainText = lastSelText;   // 给 AI 解释的主体；短选区时换成所在整句
  if (_charSel && _charSel.pw && _charSel.pw.__charBoxes) {
    const chars = _charSel.pw.__charBoxes;
    const sLen = lastSelText.length;
    if (sLen < 50) {
      // 短选区(词/碎片，如跨行的 "at"+"or") → 以所在整句为解释主体，段落作上下文，
      // 避免 AI 纠结孤立碎词"内容不完整"
      const sR = _expandSentenceFromRange(chars, _charSel.startIdx, _charSel.endIdx);
      if (sR) {
        const sentence = _charsRangeToText(chars, sR.start, sR.end);
        if (sentence && sentence.length > sLen) explainText = sentence;
        const p1 = _paragraphExpandFromChar(chars, sR.start);
        const p2 = _paragraphExpandFromChar(chars, sR.end);
        if (p1 && p2) {
          const para = _charsRangeToText(chars, Math.min(p1.start, p2.start), Math.max(p1.end, p2.end));
          if (para.length > explainText.length) context = para;
        }
      }
    } else if (sLen < 300) {
      // 中选区 → 段落作上下文
      const p1 = _paragraphExpandFromChar(chars, _charSel.startIdx);
      const p2 = _paragraphExpandFromChar(chars, _charSel.endIdx);
      if (p1 && p2) {
        const cr = {start: Math.min(p1.start, p2.start), end: Math.max(p1.end, p2.end)};
        if (cr.start < _charSel.startIdx || cr.end > _charSel.endIdx)
          context = _charsRangeToText(chars, cr.start, cr.end);
      }
    }
  }
  _resultContext = _charSel ? {
    charSel: {pw: _charSel.pw, startIdx: _charSel.startIdx, endIdx: _charSel.endIdx},
    text: lastSelText, sentence: explainText, kind: 'explain',
  } : null;
  // 解释**不开面板**:选区建一个一直闪烁的琥珀高亮,AI 后台跑;点高亮才开解释页 + 移除高亮(一次点击)。
  const _ehl = (typeof _showExplainHighlight === 'function') ? _showExplainHighlight(_charSel && _charSel.pw, lastSelText) : null;
  if (!_ehl) { aiCall('/pdf/api/explain', {text: explainText, context}, '💡 AI 解释'); return; }   // 建不了高亮(罕见)→退回旧式直接开面板
  _ehl.title = '💡 AI 解释'; _ehl.src = explainText; _ehl.resultContext = _resultContext;
  _runExplainBg(_ehl, explainText, context);
};
// 后台跑解释:不开面板,把渲染好的 HTML 缓存进高亮;若用户中途点了高亮开了加载面板(panelReqId 匹配),实时/收尾填充
async function _runExplainBg(hl, text, context) {
  const ov = _getAiOverrides();
  const body = {text, context};
  if (ov.model)  body.model  = ov.model;
  if (ov.effort) body.effort = ov.effort;
  const _fillPanel = (innerHtml, pickers) => {
    if (hl.panelReqId == null || hl.panelReqId !== _resultReqId) return;   // 用户没点开等 / 已开别的结果 → 不填
    const el = document.getElementById('result-content'); if (!el) return;
    el.innerHTML = innerHtml;
    if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([el]).catch(() => {});
    if (pickers) { try { _resultContext = hl.resultContext; } catch (_) {} try { addResultPickers(); } catch (_) {} }
  };
  let acc = '';
  try {
    const res = await _aiStream('/pdf/api/explain', { method: 'POST', body, onText: (t) => { acc = t; _fillPanel(md(t || ' '), false); } });
    if (hl.canceled) return;   // 被新解释替换 → 丢弃
    const full = res.text || acc;
    hl.html = md(full || ' ') + (res.ok ? '' : '<div style="color:#c00;margin-top:8px">✗ ' + (res.error || '失败') + '</div>');
    hl.ready = true;
    _fillPanel(hl.html, true);
  } catch (e) {
    if (hl.canceled) return;
    hl.html = '<div style="color:#c00">✗ ' + (e.message || '失败') + '</div>'; hl.ready = true;
    _fillPanel(hl.html, false);
  }
}
// 多词选中「💬 对话」：开对话框，预填原文 + 句子/段落上下文(也作 AI 上下文)，底部追问框多轮问
window.onChat = () => {
  if (!lastSelText) return;
  toolbar.classList.remove('open');
  let context = '';
  if (_charSel && _charSel.pw && _charSel.pw.__charBoxes) {
    const chars = _charSel.pw.__charBoxes;
    const cr = _expandSentenceFromRange(chars, _charSel.startIdx, _charSel.endIdx);
    if (cr) {
      const p1 = _paragraphExpandFromChar(chars, cr.start);
      const p2 = _paragraphExpandFromChar(chars, cr.end);
      if (p1 && p2) context = _charsRangeToText(chars, Math.min(p1.start, p2.start), Math.max(p1.end, p2.end));
      else context = _charsRangeToText(chars, cr.start, cr.end);
    }
  }
  _resultContext = _charSel ? {
    charSel: {pw: _charSel.pw, startIdx: _charSel.startIdx, endIdx: _charSel.endIdx},
    text: lastSelText, sentence: context || lastSelText, kind: 'chat',
  } : null;
  let html = '<div style="font-size:12.5px;line-height:1.65">'
    + '<div style="color:#a8cdff;font-weight:600;margin-bottom:3px">📌 原文</div>'
    + '<div style="color:#cfe6ff;white-space:pre-wrap">' + _esc(lastSelText) + '</div>';
  if (context && context.trim() !== lastSelText.trim()) {
    html += '<div style="color:#a8cdff;font-weight:600;margin:10px 0 3px">📖 上下文</div>'
      + '<div style="color:#8a9bb4;white-space:pre-wrap">' + _esc(context) + '</div>';
  }
  html += '<div style="margin-top:12px;color:#5a6680">↓ 在下方输入问题，AI 会结合原文和上下文回答</div></div>';
  openResult('💬 AI 对话', lastSelText, html);
  setTimeout(() => {
    const i = document.getElementById('result-followup-input');
    if (i) { i.placeholder = '问 AI（已带原文 + 上下文）…'; i.focus(); }
  }, 120);
};
window.onToQA = () => {
  if (!lastSelText) return;
  toolbar.classList.remove('open');
  const question = prompt('针对选中内容你想问 AI 什么？（如：解释里面的术语 / 这步推导为何成立 / 给一个例子）');
  if (!question || !question.trim()) return;
  // 复用 aiCall → SSE 流式 + Markdown/MathJax 渲染
  const oldSel = lastSelText;
  lastSelText = '问：' + question + '\n\n（基于选中：' + oldSel.slice(0, 100) +
                (oldSel.length > 100 ? '…' : '') + '）';
  aiCall('/pdf/api/explain', {
    text: question,
    context: '用户选中的教材片段：\n' + oldSel.slice(0, 3000),
  }, '💬 问 AI');
  setTimeout(() => { lastSelText = oldSel; }, 200);
};
window.onToNote = async () => {
  if (!lastSelText) return;
  toolbar.classList.remove('open');
  const name = prompt('请输入笔记名（不含 .md）：', '');
  if (name === null) return;
  const trimmed = (name || '').trim();
  if (!trimmed) return;
  openResult('📝 创建笔记', lastSelText, '<div class="loading">⏳ 正在写入…</div>');
  try {
    const r = await fetch('/pdf/api/to-note', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({text: lastSelText, name: trimmed, file: FILE_REL, page: _selPageNum()}),
    });
    const d = await r.json();
    if (d.ok) {
      const safeUrl = (d.obsidian_url || '').replace(/'/g,"\\'");
      document.getElementById('result-content').innerHTML =
        '<div>✓ 笔记已创建：<code>' + d.note_path + '</code>（含 PDF 来源引用）</div>' +
        '<div style="margin-top:10px"><button onclick="location.href=\'' + safeUrl + '\'" style="background:#1a2540;border:1px solid #3b6db5;color:#cfe6ff;border-radius:6px;padding:6px 14px;cursor:pointer">📂 在 Obsidian 中打开</button></div>';
    } else {
      document.getElementById('result-content').innerHTML = '<div style="color:#c00">✗ ' + (d.error || '失败') + '</div>';
    }
  } catch (e) {
    document.getElementById('result-content').innerHTML = '<div style="color:#c00">✗ ' + e.message + '</div>';
  }
};

// ──────── 文字层校准(扫描/OCR 书:文字层没对齐时手动微调 + 可视化文字框) ────────
window._charOfsByPage = window._charOfsByPage || {};

// 重渲已加载页(图走 decode-first 缓存→快;会重新拉 page-chars 应用新偏移 + 重画/撤红框)
function _rerenderLoadedPages() {
  const pc = document.getElementById('page-container');
  if (!pc) return;
  let wraps = [...pc.querySelectorAll('.page-wrap[data-loaded="1"]')];
  if (!wraps.length && pc.dataset.loaded === '1') wraps = [pc];   // 单页模式:容器即 wrap
  wraps.forEach(w => {
    w.dataset.loaded = '0';
    const num = parseInt(w.dataset.pageNum || currentPage, 10);
    if (typeof _renderPageInto === 'function') _renderPageInto(num, w).catch(() => {});
  });
}

window._charboxToggle = (cb) => {
  try { localStorage.setItem('pdf-charbox', (cb && cb.checked) ? '1' : '0'); } catch (_) {}
  _rerenderLoadedPages();   // 红框立即出现/消失
};

async function _fetchCharOfs(page) {
  try {
    const r = await fetch(`/pdf/api/char-offset?file=${encodeURIComponent(FILE_REL)}&page=${page}`);
    const d = await r.json();
    if (d.ok) return d.offset;
  } catch (_) {}
  return { dx: 0, dy: 0, scale: 1 };
}
async function _saveCharOfs(page, ofs) {
  try {
    const r = await fetch('/pdf/api/char-offset', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file: FILE_REL, page, dx: ofs.dx, dy: ofs.dy, scale: ofs.scale }),
    });
    const d = await r.json();
    if (d && d.cv) { try { localStorage.setItem('pdf-cv:' + FILE_REL + ':' + page, d.cv); } catch (_) {} }  // 偏移变→更新 cv→重渲即取新(不靠 backstop)
    return d;
  } catch (_) { return null; }
}
function _charOfsStep() {
  const v = parseFloat(document.getElementById('charofs-step')?.value);
  return (isFinite(v) && v > 0) ? v : 2;
}
function _updateCharOfsLabel(page, ofs) {
  const el = document.getElementById('charofs-cur');
  if (el) el.textContent = `第 ${page} 页　dx ${(+ofs.dx).toFixed(1)} · dy ${(+ofs.dy).toFixed(1)}` +
    ((+ofs.scale !== 1) ? ` · ×${(+ofs.scale).toFixed(2)}` : '');
}
// dirx/diry: -1/0/1。注意 PDF y 向下为正 → "下移文字"= dy 增大
window._nudgeChars = async (dirx, diry) => {
  const page = currentPage;
  let ofs = window._charOfsByPage[page] || await _fetchCharOfs(page);
  const step = _charOfsStep();
  ofs = { dx: (ofs.dx || 0) + dirx * step, dy: (ofs.dy || 0) + diry * step, scale: ofs.scale || 1 };
  window._charOfsByPage[page] = ofs;
  _updateCharOfsLabel(page, ofs);
  await _saveCharOfs(page, ofs);
  _rerenderLoadedPages();
};
window._resetCharOffset = async () => {
  const page = currentPage;
  const ofs = { dx: 0, dy: 0, scale: 1 };
  window._charOfsByPage[page] = ofs;
  _updateCharOfsLabel(page, ofs);
  await _saveCharOfs(page, ofs);
  _rerenderLoadedPages();
};
window._initCharOfsPanel = async () => {
  const cb = document.getElementById('set-charbox');
  if (cb) { try { cb.checked = localStorage.getItem('pdf-charbox') === '1'; } catch (_) {} }
  const st = document.getElementById('reocr-status'); if (st) st.textContent = '';
  const ofs = await _fetchCharOfs(currentPage);
  window._charOfsByPage[currentPage] = ofs;
  _updateCharOfsLabel(currentPage, ofs);
};

// 单页重扫:对当前页用 Google Vision 重新 OCR → 覆盖文字层(修识别错/漏/整页歪)
window._reocrPage = async () => {
  const btn = document.getElementById('reocr-btn');
  const st = document.getElementById('reocr-status');
  if (btn) btn.disabled = true;
  if (st) st.textContent = '⏳ 重扫第 ' + currentPage + ' 页…(Google Vision,几秒)';
  try {
    const r = await fetch('/pdf/api/reocr-page', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file: FILE_REL, page: currentPage }),
    });
    const d = await r.json();
    if (d.cv) { try { localStorage.setItem('pdf-cv:' + FILE_REL + ':' + currentPage, d.cv); } catch (_) {} }  // 重扫后 cv 更新→重渲直接取新覆盖
    if (d.ok && d.chars > 0) {
      if (st) st.textContent = '✓ 第 ' + currentPage + ' 页重扫完成(' + d.chars + ' 字)';
      _rerenderLoadedPages();   // cv 变 → 重渲拿新文字层
    } else if (d.ok) {
      if (st) st.textContent = '⚠ 未识别到文字(空白页或扫描质量差)';
    } else if (st) { st.textContent = '✗ ' + (d.error || '失败'); }
  } catch (e) { if (st) st.textContent = '✗ 网络失败'; }
  finally { if (btn) btn.disabled = false; }
};
window._clearReocr = async () => {
  const st = document.getElementById('reocr-status');
  if (st) st.textContent = '撤销中…';
  try {
    const r = await fetch('/pdf/api/reocr-page/clear', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file: FILE_REL, page: currentPage }),
    });
    const d = await r.json();
    if (d.cv) { try { localStorage.setItem('pdf-cv:' + FILE_REL + ':' + currentPage, d.cv); } catch (_) {} }  // 撤销后 cv 更新→重渲取回原文字层
    if (st) st.textContent = d.cleared ? ('✓ 已撤销第 ' + currentPage + ' 页重扫') : '该页无重扫记录';
    _rerenderLoadedPages();
  } catch (e) { if (st) st.textContent = '✗ 网络失败'; }
};

// 键盘快捷键
document.addEventListener('keydown', (e) => {
  // 焦点在任何可输入控件(含侧栏 AI 的 textarea / 可编辑区)时,左右键给光标用,别翻页
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.isContentEditable) return;
  if (e.key === 'ArrowRight' || e.key === 'PageDown' || e.key === ' ') { e.preventDefault(); changePage(1); }
  else if (e.key === 'ArrowLeft' || e.key === 'PageUp') { e.preventDefault(); changePage(-1); }
  else if (e.key === 'Escape') { closeResult(); toolbar.classList.remove('open'); }
});

loadPdf();
