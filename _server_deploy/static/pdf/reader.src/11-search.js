// ──────── 全文搜索（F4） ────────
let _searchTimer = null, _searchSeq = 0;
async function _queryBookSearch(q, options) {
  // @interaction reader.document.search
  const r = await fetch('/pdf/api/search?file=' + encodeURIComponent(FILE_REL) +
    '&q=' + encodeURIComponent(q) + '&limit=200', { signal: options?.signal });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return await r.json();
}
window.RC = window.RC || {};
window.RC.readerSearch = {
  search: async (query, options) => {
    const q = String(query || '').trim();
    if (!q) return { total: 0, pages: 0, incomplete: false, results: [] };
    const data = await _queryBookSearch(q, options);
    if (!data || data.ok !== true) throw new Error(data?.error || '搜索失败');
    return { total: data.total, pages: data.pages, incomplete: !!data.incomplete,
      results: (data.matches || []).map(m => ({ locator: m.page,
        label: 'P' + (window._dispPage ? window._dispPage(m.page) : m.page),
        excerpt: String(m.snippet || ''), count: m.count || 1 })) };
  },
  jump: (result, query) => {
    if (!Number.isInteger(result?.locator) || result.locator < 1) throw new Error('搜索位置已失效');
    _jumpSearchPage(result.locator, query);
  }
};
window.openSearch = () => {
  const p = document.getElementById('search-panel');
  p.classList.add('open');
  const inp = document.getElementById('search-input');
  inp.focus(); inp.select();
  if (inp.value.trim()) _runSearch();
};
window.closeSearch = () => { document.getElementById('search-panel').classList.remove('open'); };
window._searchDebounced = () => {
  clearTimeout(_searchTimer);
  _searchTimer = setTimeout(_runSearch, 320);
};
window._runSearch = async () => {
  const q = (document.getElementById('search-input').value || '').trim();
  const stat = document.getElementById('search-stat');
  const box = document.getElementById('search-results');
  if (!q) { box.innerHTML = ''; stat.textContent = ''; return; }
  const seq = ++_searchSeq;
  stat.textContent = '搜索中…';
  box.innerHTML = '<div class="sr-empty">⏳ 首次搜索本书需建索引（约几秒）…</div>';
  try {
    const d = await _queryBookSearch(q);
    if (seq !== _searchSeq) return;   // 已被更新的查询取代
    if (!d.ok) { box.innerHTML = '<div class="sr-empty">搜索失败：' + (d.error || '?') + '</div>'; stat.textContent = ''; return; }
    stat.textContent = d.total + ' 处 / ' + d.pages + ' 页' + (d.incomplete ? ' · 部分页待识别' : '');
    if (!d.matches.length) {
      box.innerHTML = '<div class="sr-empty">' + (d.incomplete
        ? '已完成页面暂未找到「' + _esc(q) + '」；仍有页面待识别'
        : '未找到「' + _esc(q) + '」') + '</div>';
      return;
    }
    const ql = q.toLowerCase();
    box.innerHTML = d.matches.map(m => {
      // snippet 高亮所有 q 出现处（大小写不敏感）
      let html = '', s = m.snippet || '', sl = s.toLowerCase(), i = 0;
      while (i < s.length) {
        const j = sl.indexOf(ql, i);
        if (j < 0) { html += _esc(s.slice(i)); break; }
        html += _esc(s.slice(i, j)) + '<mark>' + _esc(s.slice(j, j + q.length)) + '</mark>';
        i = j + q.length;
      }
      return '<div class="sr-item" onclick="_searchJump(' + m.page + ')">' +
        '<span class="sr-pg">P' + (window._dispPage ? window._dispPage(m.page) : m.page) + (m.count > 1 ? '·' + m.count : '') + '</span>' +
        '<span class="sr-snip">' + html + '</span></div>';
    }).join('');
  } catch (e) {
    if (seq !== _searchSeq) return;
    box.innerHTML = '<div class="sr-empty">网络错误：' + _esc(e.message) + '</div>';
    stat.textContent = '';
  }
};
window._pendingSearchHighlight = null;   // {query, page}：跳转后等该页 chars ready 再高亮原文位置
window._searchJump = (pg) => {
  const q = (document.getElementById('search-input').value || '').trim();
  closeSearch();
  _jumpSearchPage(pg, q);
};
function _jumpSearchPage(pg, q) {
  window._pendingSearchHighlight = q ? {query: q, page: pg} : null;
  goToPage(pg);
  _applyPendingSearchHighlight();   // 已加载的页立即高亮；未加载则轮询等待
}
// 轮询等目标页 __charBoxes 就绪（单页/连续模式通用），就绪后画命中高亮
function _applyPendingSearchHighlight(tries) {
  tries = tries || 0;
  const ph = window._pendingSearchHighlight;
  if (!ph) return;
  // ⚠ 原生正文接管时这条路走不通：它等的是 `dataset.loaded === '1'` + __charBoxes，
  //   而那时网页根本不渲页 —— 轮询 4.8 秒后把标记清掉，命中就永远不会亮。
  //   把标记留在原地，交给原生那侧消费（见 __bwReaderPageOverlay 的 searchQuery）。
  try {
    if (window.RC?.readerNavigation?.nativeViewport) return;
  } catch (_) {}
  const wrap = document.querySelector('[data-page-num="' + ph.page + '"]');
  if (wrap && wrap.dataset.loaded === '1' && wrap.__charBoxes && wrap.__charBoxes.length) {
    try { _highlightSearchResultsOnPage(wrap, ph.query); } catch (_) {}
    window._pendingSearchHighlight = null;
    return;
  }
  if (tries < 30) setTimeout(() => _applyPendingSearchHighlight(tries + 1), 160);   // 最多 ~4.8s
  else window._pendingSearchHighlight = null;
}
// 从 __charBoxes 合并同行相邻字符成矩形（viewport px）
function _buildRectsFromCharRange(chars, s, e) {
  if (s >= e || s < 0 || e > chars.length) return [];
  const rects = []; let cur = null;
  for (let i = s; i < e; i++) {
    const c = chars[i];
    if (c.left == null) continue;
    const lineH = c.height || 1;
    if (cur && Math.abs((c.top + c.height) - (cur.top + cur.height)) <= lineH * 0.6 && c.left >= cur.left - 2) {
      cur.width = Math.max(cur.left + cur.width, c.left + c.width) - cur.left;
      cur.top = Math.min(cur.top, c.top);
      cur.height = Math.max(cur.height, c.height);
    } else { if (cur) rects.push(cur); cur = {left: c.left, top: c.top, width: c.width, height: c.height}; }
  }
  if (cur) rects.push(cur);
  return rects;
}
// 在页面上把 query 所有命中处画黄色高亮（子串、大小写不敏感），滚到第一处，6s 后淡出
function _highlightSearchResultsOnPage(wrap, query) {
  if (!wrap || !wrap.__charBoxes || !query) return;
  wrap.querySelector('.search-hl-layer')?.remove();
  const chars = wrap.__charBoxes;
  const fullLower = chars.map(c => (c.c || '')).join('').toLowerCase();
  const ql = query.toLowerCase();
  if (!ql) return;
  const matches = []; let pos = 0;
  while (true) { const i = fullLower.indexOf(ql, pos); if (i < 0) break; matches.push(i); pos = i + Math.max(1, ql.length); }
  if (!matches.length) return;
  const layer = document.createElement('div');
  layer.className = 'search-hl-layer';
  wrap.appendChild(layer);
  let first = null;
  for (let m = 0; m < Math.min(matches.length, 40); m++) {
    for (const r of _buildRectsFromCharRange(chars, matches[m], matches[m] + ql.length)) {
      const div = document.createElement('div');
      div.className = 'search-hl';
      div.style.left = r.left + 'px'; div.style.top = r.top + 'px';
      div.style.width = r.width + 'px'; div.style.height = r.height + 'px';
      layer.appendChild(div);
      if (!first) first = div;
    }
  }
  if (first) { try { first.scrollIntoView({block: 'center', behavior: 'auto'}); } catch (_) {} }
  setTimeout(() => { layer.style.transition = 'opacity 1.2s'; layer.style.opacity = '0'; setTimeout(() => layer.remove(), 1300); }, 6000);
}

// 乐观下划线：查词 ECDICT 秒回 lemma+forms 后，立刻在所在页把这些词形画上下划线(橙=new)，
// 不等后台笔记生成 + refresh。后续真实 refresh 会用真实 mastery 颜色覆盖。
