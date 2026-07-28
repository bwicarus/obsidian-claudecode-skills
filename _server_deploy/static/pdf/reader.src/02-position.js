// ─── per-PDF 位置记忆（localStorage pdf-last-positions） ───
const LAST_POS_KEY = 'pdf-last-positions';
function _loadLastPositions() {
  try { return JSON.parse(localStorage.getItem(LAST_POS_KEY) || '{}'); }
  catch { return {}; }
}
let _ogLastPage = null;
function _saveLastPosition(patch) {
  const all = _loadLastPositions();
  all[FILE_REL] = {...(all[FILE_REL] || {}), ...patch, ts: Date.now()};
  // 最多保留 200 个 PDF 的记忆，按时间淘汰
  const entries = Object.entries(all);
  if (entries.length > 200) {
    entries.sort((a, b) => (b[1].ts || 0) - (a[1].ts || 0));
    const trimmed = Object.fromEntries(entries.slice(0, 200));
    try { localStorage.setItem(LAST_POS_KEY, JSON.stringify(trimmed)); } catch {}
  } else {
    try { localStorage.setItem(LAST_POS_KEY, JSON.stringify(all)); } catch {}
  }
  // 双向上下文同步:借用这个已有的翻页漏斗上报「当前活动文档」,不新增任何监听器。
  // 开关关着时 RC.ctxSync.report 立即返回 false(零网络);开着时由共享层合并 + 1s trailing。
  // 切页 → 丢弃绘图焦点(上一页的绘图区不再是"当前")。只在当前焦点确实是绘图时才发。
  try { if ((patch && patch.page) && patch.page !== _ogLastPage) { _ogLastPage = patch.page;
        window.RC?.outgoing?.dropDrawingFocus(); } } catch (_) {}
  try {
    window.RC?.ctxSync?.report({
      kind: 'pdf', file: FILE_REL,
      pos: (patch && patch.page) || currentPage,
      total: (window.__GRP ? window.__GRP.total : (window.pdfDoc && pdfDoc.numPages)) || undefined,
      title: document.title.replace(/ ·.*$/, '')
    });
  } catch (_) {}
}
function _getLastPosition() {
  return _loadLastPositions()[FILE_REL] || null;
}
function _maybeRestoreLastPos() {
  // URL 显式带 page > 1 时尊重 URL；否则恢复
  const u = new URL(location.href);
  const urlPage = parseInt(u.searchParams.get('page') || '0');
  if (urlPage > 1 || u.searchParams.get('mode')) return;   // URL 显式带 page/mode 时尊重 URL，不恢复上次位置
  const pos = _getLastPosition();
  // 排版模式是设备偏好:无论哪边胜都沿用本机上次的 mode(不影响页码仲裁)
  if (pos && pos.mode && pos.mode !== readMode) {
    localStorage.setItem('pdf-read-mode', pos.mode);
    readMode = pos.mode;
  }
  // ── 单真相源仲裁(2026-07-06,大厂 Kindle/Books 模型;修「每次开在固定位置」):服务端续读(CFG.page/page_ts)
  //   与本地 localStorage 记录**按时间戳取新者**。此前两套系统无仲裁、LS 总是无条件覆盖 → 冻结的旧 LS 每次都赢。
  const srvTs = ((window.__PDF_CFG && +__PDF_CFG.page_ts) || 0) * 1000;   // 服务端 epoch秒 → ms
  if (!pos || !pos.page || (srvTs && srvTs >= (pos.ts || 0))) {
    // 服务端更新鲜(或无本地记录)→ 页码信服务端(currentPage 已=CFG.page)。
    // 但**同页时仍用本地的页内偏移**(审计 BUG#4:服务端只存页码不存 frac;5s 上报常晚于 600ms LS 存,
    // server 胜是常态——若因此把 frac 丢掉,每次都开在页顶,反而比仲裁前更差):页码用仲裁、页内偏移用本地。
    if (pos && pos.page === currentPage) {
      _pendingPage = currentPage;
      _pendingFrac = (pos.frac > 0 && pos.frac <= 1) ? pos.frac : 0;
      _pendingScrollY = pos.scrollY || 0;
    } else if (srvTs) {
      try { _saveLastPosition({ page: currentPage, scrollY: 0, frac: 0, mode: readMode }); } catch (_) {}   // 换页了才对齐本地(防旧 frac 錯页套用)
    }
    window.dlog?.('续读仲裁:server 胜 p.' + currentPage + (_pendingFrac ? ' (+本地frac)' : ''));
    return;
  }
  // 本地更新鲜(离线读过/上报失败过)→ 用本地,并把服务端记录「治愈」到本地页(别的设备才拿得到新进度)
  currentPage = pos.page;
  _pendingPage = pos.page;   // 闭包捕获目标页:三连 apply 不读活的 currentPage(会被滚动处理器反馈式改掉 → 恢复位置逐次后爬,审计 BUG#3)
  _pendingFrac = (pos.frac > 0 && pos.frac <= 1) ? pos.frac : 0;
  _pendingScrollY = pos.scrollY || 0;
  try {
    fetch('/pdf/api/reading-pos', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file: FILE_REL, kind: 'pdf', pos: pos.page }), keepalive: true }).catch(() => {});
  } catch (_) {}
  window.dlog?.('续读仲裁:local 胜 p.' + currentPage + (pos.frac ? ' frac=' + pos.frac.toFixed(3) : (pos.scrollY ? ' scrollY=' + pos.scrollY : '')));
  _toast?.('⏯ 已恢复到上次位置 p.' + currentPage);
}
function _restoreScrollAfterRender() {
  if (!_pendingScrollY && !_pendingFrac) return;
  const main = document.getElementById('main');
  if (!main) return;
  // 页内比例优先(布局无关:scale/旋转/占位高度变化都不跑偏);老记录无 frac 才退回绝对 scrollY
  const tgtPage = _pendingPage || currentPage;   // 捕获目标页(BUG#3:活的 currentPage 会被滚动处理器按视口中线改掉,三连 apply 逐次后爬)
  const apply = () => {
    if (_pendingFrac > 0) {
      const pw = document.querySelector(`.page-wrap[data-page-num="${tgtPage}"]`);
      if (pw && pw.offsetHeight) { main.scrollTop = pw.offsetTop + _pendingFrac * pw.offsetHeight; return; }
    }
    if (_pendingScrollY) main.scrollTop = _pendingScrollY;
  };
  // 多次 apply 直到稳定（连续模式占位高度逐步算准 / 真渲染替换占位时高度可能变）
  setTimeout(apply, 100);
  setTimeout(apply, 400);
  setTimeout(apply, 1000);
  setTimeout(() => { _pendingScrollY = 0; _pendingFrac = 0; _pendingPage = 0; }, 1100);   // 最后一次后清空（避免重复 apply 干扰用户主动滚动）
}
function _attachScrollSaver() {
  const main = document.getElementById('main');
  if (!main || main.__savedAttached) return;
  main.__savedAttached = true;
  main.addEventListener('scroll', () => {
    if (_scrollSaveTimer) clearTimeout(_scrollSaveTimer);
    _scrollSaveTimer = setTimeout(() => {
      let frac = 0;   // 页内比例(布局无关);算不出(页未渲)存 0,恢复时退回 scrollY
      try {
        const pw = document.querySelector(`.page-wrap[data-page-num="${currentPage}"]`);
        if (pw && pw.offsetHeight) frac = Math.max(0, Math.min(1, (main.scrollTop - pw.offsetTop) / pw.offsetHeight));
      } catch (_) {}
      _saveLastPosition({page: currentPage, scrollY: main.scrollTop, frac, mode: readMode, scale});
    }, 600);
  }, {passive: true});
}


