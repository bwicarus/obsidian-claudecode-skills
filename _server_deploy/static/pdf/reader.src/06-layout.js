function _updateModeButtons() {
  // 双页按钮文字随形态变化:单页 / 双页1|2 / 双页2|3(点击循环切换,标签显示当前所在形态)
  const s = document.getElementById('spread-toggle');
  if (!s) return;
  s.classList.toggle('active', readMode === 'spread');
  s.textContent = (readMode === 'spread') ? (_spreadOffset ? '⊞ 双页 2|3' : '⊞ 双页 1|2') : '📄 单页';
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
  window._rememberOrientLayout?.();   // 记进当前方向(若开了旋转自动切换)
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

// ── 旋转自动切换排版：每本 PDF 按 横/竖屏 各记一套 {排版 readMode, 去边开关 _cropOn, 双页错位 _spreadOffset} ──
function _autoOrientOn() { try { return localStorage.getItem('pdf-auto-orient') === '1'; } catch (_) { return false; } }
function _orient() {
  // 优先用 matchMedia(旋转时比 innerWidth/Height 更早、更稳定地翻转);取不到再退回尺寸比较
  try { if (window.matchMedia) return window.matchMedia('(orientation: portrait)').matches ? 'port' : 'land'; } catch (_) {}
  return (window.innerWidth >= window.innerHeight) ? 'land' : 'port';
}
function _orientKey(o) { return 'pdf-layout:' + FILE_REL + ':' + o; }
function _saveOrientLayout(o) {
  // 也存 scale(当前缩放=是否宽度适应/手动放大),恢复时按该方向上次的缩放还原
  try { localStorage.setItem(_orientKey(o), JSON.stringify({ mode: readMode, crop: _cropOn ? 1 : 0, off: _spreadOffset || 0, scale: +scale.toFixed(3) })); } catch (_) {}
}
function _loadOrientLayout(o) {
  try { const s = localStorage.getItem(_orientKey(o)); return s ? JSON.parse(s) : null; } catch (_) { return null; }
}
let _orientPendingScale = 0;   // 待套用的缩放(渲染完后由 _applyPendingOrientScale 处理)
function _applyOrientLayoutVars(lay) {   // 套到当前变量(不重渲染,调用方负责),返回是否套了
  if (!lay) return false;
  readMode = (lay.mode === 'spread') ? 'spread' : 'continuous';
  _spreadOffset = lay.off ? 1 : 0;
  _cropOn = !!lay.crop;
  _orientPendingScale = (lay.scale > 0) ? lay.scale : 0;
  try {
    localStorage.setItem('pdf-read-mode', readMode);
    localStorage.setItem(_cropKey(), _cropOn ? '1' : '0');
    localStorage.setItem(_spreadKey(), String(_spreadOffset));
  } catch (_) {}
  return true;
}
// 重排渲染后套用记住的缩放:与当前(=宽度适应)差得多才套(说明该方向上次是手动放大;一致=就是适应,免重渲)
async function _applyPendingOrientScale() {
  const t = _orientPendingScale; _orientPendingScale = 0;
  if (t > 0 && Math.abs(t - scale) > 0.02 && typeof _applyZoom === 'function') await _applyZoom(t);
}
// 手动改排版/去边后调用：开了自动切换就把当前布局记进当前方向
window._rememberOrientLayout = function () { if (_autoOrientOn()) _saveOrientLayout(_orient()); };
let _lastOrient = _orient();
let _orientBusy = false;
async function _onOrientChange() {
  const now = _orient();
  if (now === _lastOrient) return;       // 没真转(只是窗口宽变) → 不动
  window.dlog && window.dlog('orient change: ' + _lastOrient + '→' + now + ' auto=' + _autoOrientOn());
  if (!_autoOrientOn()) { _lastOrient = now; return; }
  if (_orientBusy) return;               // 旋转动画期多次 resize → 防重入
  _orientBusy = true;
  try {
    _saveOrientLayout(_lastOrient);      // 先存离开方向的当前布局
    _lastOrient = now;
    const lay = _loadOrientLayout(now);
    if (!lay) { window.dlog && window.dlog('orient: ' + now + ' 无存档,保持当前'); return; }
    if (_applyOrientLayoutVars(lay)) {
      window.dlog && window.dlog('orient: 套用 ' + now + ' = ' + JSON.stringify(lay));
      _updateModeButtons(); _updateCropBtn();
      await _applyModeChange(currentPage);
      await _applyPendingOrientScale();  // 还原该方向上次的缩放(宽度适应/手动放大)
    }
  } finally { _orientBusy = false; }
}
window.addEventListener('orientationchange', () => setTimeout(_onOrientChange, 300));   // 等尺寸稳定再判
try { window.matchMedia('(orientation: portrait)').addEventListener('change', () => setTimeout(_onOrientChange, 300)); } catch (_) {}
// 兜底:iPad 上 orientationchange/matchMedia 偶尔不触发,但旋转必触发 resize。debounce 后判方向(同样靠 now!==_lastOrient 守卫,只在真转时动)
let _orientResizeT = 0;
window.addEventListener('resize', () => { clearTimeout(_orientResizeT); _orientResizeT = setTimeout(_onOrientChange, 280); });

// ── 双指缩放：阅读器接管（禁浏览器 pinch 位图拉伸，改按新倍率重渲染 PDF → 清晰）──
// focal = {fx,fy,cx,cy,s0}:fx/fy=捏合焦点在内容坐标(旧 s0 布局下);cx/cy=该点的屏幕位置;
// 重渲后把它放回同一屏幕位置(焦点保持,缩放不跳)。无 focal(旋转恢复)→ 按相对位置保持。
async function _applyZoom(newScale, focal) {
  if (_refitBusy || !pdfDoc) return;
  newScale = Math.max(_ZOOM_MIN, Math.min(_scaleMax, newScale));   // 下限放宽:可缩到比 fit-width 更小
  if (Math.abs(newScale - scale) < 0.005) return;
  _refitBusy = true;
  try {
    const main = document.getElementById('main');
    const container = document.getElementById('page-container');
    const ratio = container && container.offsetHeight ? main.scrollTop / Math.max(1, container.offsetHeight) : 0;
    scale = newScale;
    _lastFitWidth = _mainContentWidth();   // 占住 fit 宽，避免 ResizeObserver 把 scale 拉回自适应
    if (readMode !== 'single') {           // 连续 + 双页 都重建滚动列表(原来只判 continuous 漏了 spread)
      await setupContinuousMode();
    } else {
      const wrap = container.querySelector('.page-wrap') || container;
      if (wrap.dataset) wrap.dataset.loaded = '0';
      await renderPage(currentPage);
    }
    requestAnimationFrame(() => {
      if (focal && focal.s0 > 0) {
        const mr = main.getBoundingClientRect();
        const k = newScale / focal.s0;   // 内容坐标随 scale 等比放大
        main.scrollLeft = Math.max(0, focal.fx * k - (focal.cx - mr.left));
        main.scrollTop  = Math.max(0, focal.fy * k - (focal.cy - mr.top));
      } else if (container && container.offsetHeight) {
        main.scrollTop = Math.floor(ratio * container.offsetHeight);   // 无焦点:保持相对竖直位置
      }
      _updateMainOverflowX();   // 缩放后:超宽放开横向 auto,缩回 fit 内则锁
      window._rememberOrientLayout?.();   // 手动缩放记进当前方向(若开了旋转自动切换)
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
      const mr = main.getBoundingClientRect();
      const cx = (a.clientX + b.clientX) / 2, cy = (a.clientY + b.clientY) / 2;
      // 焦点固定在**起始**两指中点:内容坐标(含滚动;#main padding=0 → 内容坐标=page-container 坐标)
      _pinch = {
        d0: Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) || 1,
        s0: scale, target: scale,
        fx: main.scrollLeft + (cx - mr.left),
        fy: main.scrollTop + (cy - mr.top),
        cx, cy,
      };
      const pc = document.getElementById('page-container');
      pc.style.transformOrigin = _pinch.fx + 'px ' + _pinch.fy + 'px';   // 整个手势固定不变 → 焦点视觉锁定
      e.preventDefault();
    }
  }, { passive: false });
  main.addEventListener('touchmove', (e) => {
    if (_pinch && e.touches.length === 2) {
      e.preventDefault();
      const [a, b] = e.touches;
      const d = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
      _pinch.target = Math.max(_ZOOM_MIN, Math.min(_scaleMax, _pinch.s0 * (d / _pinch.d0)));
      // 实时预览:只改 scale(origin 起始已定),焦点点视觉不动;松手后按新倍率重渲染→清晰
      document.getElementById('page-container').style.transform = 'scale(' + (_pinch.target / _pinch.s0) + ')';
    }
  }, { passive: false });
  const endPinch = () => {
    if (!_pinch) return;
    const p = _pinch; _pinch = null;
    const pc = document.getElementById('page-container');
    pc.style.transform = ''; pc.style.transformOrigin = '';
    if (Math.abs(p.target - scale) > 0.01) {
      // 焦点保持:把起始焦点内容点放回起始屏幕位置(cx,cy),缩放不跳
      _applyZoom(p.target, { fx: p.fx, fy: p.fy, cx: p.cx, cy: p.cy, s0: p.s0 });
    }
  };
  main.addEventListener('touchend', (e) => { if (_pinch && e.touches.length < 2) endPinch(); }, { passive: false });
  main.addEventListener('touchcancel', endPinch, { passive: false });
}
if (document.readyState !== 'loading') _setupPinchZoom();
else window.addEventListener('DOMContentLoaded', _setupPinchZoom);

// 连续模式：所有页占位 + IntersectionObserver 懒加载
