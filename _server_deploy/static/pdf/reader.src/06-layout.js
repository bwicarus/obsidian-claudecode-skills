function _updateModeButtons() {
  const m = document.getElementById('mode-toggle');
  if (m) m.textContent = readMode === 'continuous' ? '📚 连续' : (readMode === 'spread' ? '📄 单页' : '📄 单页');
  const s = document.getElementById('spread-toggle');
  if (s) s.classList.toggle('active', readMode === 'spread');
}
async function _applyModeChange(keepPage) {
  _pendingScrollY = 0;   // 清掉位置恢复残留，否则 setupContinuousMode 的定位会被跳过
  await _refitToWidth(true);   // 重算 scale(单页/连续=整宽,双页=半宽)+ 重建布局
  currentPage = keepPage;
  if (readMode === 'single') {
    await renderPage(keepPage);
  } else {
    const t = document.querySelector(`[data-page-num="${keepPage}"]`);
    if (t) setTimeout(() => t.scrollIntoView({block: 'start', behavior: 'auto'}), 80);
  }
  _saveLastPosition({page: currentPage, mode: readMode, scale});
}
window.toggleReadMode = async () => {
  const keepPage = currentPage;
  // 单页↔连续;若当前在双页,切回单页(双页用 ⊞ 按钮单独控制)
  readMode = readMode === 'single' ? 'continuous' : 'single';
  try { localStorage.setItem('pdf-read-mode', readMode); } catch (_) {}
  _updateModeButtons();
  await _applyModeChange(keepPage);
};
// 双页(spread)按钮：单页/连续切换已删,双页按钮兼任「进入/错开/退出」三态循环——
// 连续 → 双页(offset0) → 双页(offset1,facing 错开) → 连续。
window.toggleSpread = async () => {
  const keepPage = currentPage;
  if (readMode !== 'spread') { readMode = 'spread'; _spreadOffset = 0; }
  else if (_spreadOffset === 0) { _spreadOffset = 1; }
  else { readMode = 'continuous'; }      // 第三下回单列连续(取消单页后,这是退出双页的唯一入口)
  try {
    localStorage.setItem('pdf-read-mode', readMode);
    localStorage.setItem(_spreadKey(), String(_spreadOffset));
  } catch (_) {}
  _updateModeButtons();
  await _applyModeChange(keepPage);
};

// 容器宽度变化 → 重算 scale 并重渲染（解决 PDF 被 CSS 缩放拉伸/模糊）
let _refitDebounce = null, _refitBusy = false, _lastFitWidth = 0;
// #main 真实可用内容宽度（clientWidth 含 padding，开侧栏加 padding-right 后必须减掉真实 padding）
function _mainContentWidth() {
  const m = document.getElementById('main');
  const cs = getComputedStyle(m);
  return m.clientWidth - (parseFloat(cs.paddingLeft)||0) - (parseFloat(cs.paddingRight)||0);
}
// fit-width scale:始终按**宽度**拟合(用户明确要宽度适应,不要高度适应)。
// 单页/连续=一页铺满宽;双页=两页并排铺满宽(扣行内 gap),页太高就竖向滚(spread 本是连续滚动)。
// 去边时按可见宽占比换算(÷可见宽 → 让裁切后的可见区填满宽)。
function _computeFitScale(v0w, v0h) {
  const mainW = _mainContentWidth();
  const ppr = _pagesPerRow();
  const avail = mainW - (ppr > 1 ? 10 : 0);
  const s = avail / (v0w * _cropVisWFrac() * ppr);
  return Math.max(_ZOOM_MIN, Math.min(_scaleMax, s));
}
// 横向滚动锁:内容宽 ≤ 视口宽(适应/去边/缩小态,页面正好或不足铺满)→ overflow-x:hidden,
// 左右拖动不再让页面滑动(iOS overflow:auto 的横向橡皮筋也一并禁掉);只有真放大超宽时放开 auto。
// 先临时设 auto 量真实 scrollWidth(hidden 态会把溢出裁掉量不出),再据此决定锁不锁。
function _updateMainOverflowX() {
  const main = document.getElementById('main');
  if (!main) return;
  main.style.overflowX = 'auto';
  const overflow = main.scrollWidth > main.clientWidth + 2;
  main.style.overflowX = overflow ? 'auto' : 'hidden';
}
window._updateMainOverflowX = _updateMainOverflowX;
function _scheduleRefit(force) {
  if (_refitDebounce) clearTimeout(_refitDebounce);
  _refitDebounce = setTimeout(() => _refitToWidth(force), 180);
}
async function _refitToWidth(force) {
  if (_refitBusy || !pdfDoc) return;
  const main = document.getElementById('main');
  const mainW = _mainContentWidth();
  if (mainW <= 0) return;
  if (!force && Math.abs(mainW - _lastFitWidth) < 30) return;
  _refitBusy = true;
  try {
    const page1 = await pdfDoc.getPage(1);
    const v0 = page1.getViewport({scale: 1});
    const newScale = _computeFitScale(v0.width, v0.height);   // 双页含高度约束(整页可见)
    if (Math.abs(newScale - scale) < 0.01 && !force) return;
    // 保存当前滚动相对位置（按 page-container 高度比例）
    const container = document.getElementById('page-container');
    const ratio = container && container.offsetHeight
      ? main.scrollTop / Math.max(1, container.offsetHeight)
      : 0;
    scale = newScale;
    _lastFitWidth = mainW;
    if (readMode !== 'single') {   // 连续 / 双页 都重建滚动列表
      await setupContinuousMode();
    } else {
      // 单页模式：清 loaded 标记重 render
      const wrap = container.querySelector('.page-wrap') || container;
      wrap.dataset.loaded = '0';
      await renderPage(currentPage);
    }
    // 按比例恢复滚动
    requestAnimationFrame(() => {
      if (container && container.offsetHeight) {
        main.scrollTop = Math.floor(ratio * container.offsetHeight);
      }
      _updateMainOverflowX();   // 适应/去边后内容铺满宽 → 锁横向拖动
    });
  } finally {
    _refitBusy = false;
  }
}

// 监视 #main 宽度（窗口缩放、panel 打开 / 关闭、横竖屏）
function _setupResizeWatcher() {
  const main = document.getElementById('main');
  if (!main || main.dataset.resizeWatch === '1') return;
  main.dataset.resizeWatch = '1';
  if (window.ResizeObserver) {
    new ResizeObserver(() => _scheduleRefit(false)).observe(main);
  } else {
    window.addEventListener('resize', () => _scheduleRefit(false));
  }
}
if (document.readyState !== 'loading') _setupResizeWatcher();
else window.addEventListener('DOMContentLoaded', _setupResizeWatcher);

// ── 双指缩放：阅读器接管（禁浏览器 pinch 位图拉伸，改按新倍率重渲染 PDF + 笔迹）──
async function _applyZoom(newScale) {
  if (_refitBusy || !pdfDoc) return;
  newScale = Math.max(_ZOOM_MIN, Math.min(_scaleMax, newScale));   // 下限放宽:可缩到比 fit-width 更小
  if (Math.abs(newScale - scale) < 0.01) return;
  _refitBusy = true;
  try {
    const main = document.getElementById('main');
    const container = document.getElementById('page-container');
    const ratio = container && container.offsetHeight ? main.scrollTop / Math.max(1, container.offsetHeight) : 0;
    scale = newScale;
    _lastFitWidth = _mainContentWidth();   // 占住 fit 宽，避免 ResizeObserver 把 scale 拉回自适应
    if (readMode === 'continuous') {
      await setupContinuousMode();
    } else {
      const wrap = container.querySelector('.page-wrap') || container;
      if (wrap.dataset) wrap.dataset.loaded = '0';
      await renderPage(currentPage);
    }
    requestAnimationFrame(() => {
      if (container && container.offsetHeight) main.scrollTop = Math.floor(ratio * container.offsetHeight);
      _updateMainOverflowX();   // 缩放后:超宽放开横向 auto,缩回 fit 内则锁
    });
  } finally { _refitBusy = false; }
}
window._applyZoom = _applyZoom;

let _pinch = null;
function _setupPinchZoom() {
  const main = document.getElementById('main');
  if (!main || main.dataset.pinchWatch === '1') return;
  main.dataset.pinchWatch = '1';
  // 禁 iOS Safari 浏览器级页面缩放（否则双指仍是拉伸位图 → 糊）
  document.addEventListener('gesturestart', (e) => e.preventDefault());
  document.addEventListener('gesturechange', (e) => e.preventDefault());
  main.addEventListener('touchstart', (e) => {
    if (e.touches.length === 2) {
      const [a, b] = e.touches;
      _pinch = {
        d0: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) || 1,
        s0: scale, target: scale,
        cx: (a.clientX + b.clientX) / 2, cy: (a.clientY + b.clientY) / 2,
      };
      e.preventDefault();
    }
  }, { passive: false });
  main.addEventListener('touchmove', (e) => {
    if (_pinch && e.touches.length === 2) {
      e.preventDefault();
      const [a, b] = e.touches;
      const d = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
      _pinch.target = Math.max(_ZOOM_MIN, Math.min(_scaleMax, _pinch.s0 * (d / _pinch.d0)));
      // 实时预览：CSS transform 缩放 page-container（临时位图拉伸，松手后重渲染清晰）
      const pc = document.getElementById('page-container');
      const mr = main.getBoundingClientRect();
      pc.style.transformOrigin = (main.scrollLeft + _pinch.cx - mr.left) + 'px ' + (main.scrollTop + _pinch.cy - mr.top) + 'px';
      pc.style.transform = 'scale(' + (_pinch.target / _pinch.s0) + ')';
    }
  }, { passive: false });
  const endPinch = () => {
    if (!_pinch) return;
    const target = _pinch.target; _pinch = null;
    const pc = document.getElementById('page-container');
    pc.style.transform = ''; pc.style.transformOrigin = '';
    if (Math.abs(target - scale) > 0.02) _applyZoom(target);
  };
  main.addEventListener('touchend', (e) => { if (_pinch && e.touches.length < 2) endPinch(); }, { passive: false });
  main.addEventListener('touchcancel', endPinch, { passive: false });
}
if (document.readyState !== 'loading') _setupPinchZoom();
else window.addEventListener('DOMContentLoaded', _setupPinchZoom);

// 连续模式：所有页占位 + IntersectionObserver 懒加载
