function _updateModeButtons() {
  // 双页按钮文字随形态变化:单页 / 双页1|2 / 双页2|3(点击循环切换,标签显示当前所在形态)
  const s = document.getElementById('spread-toggle');
  if (!s) return;
  s.classList.toggle('active', readMode === 'spread');
  s.textContent = (readMode === 'spread') ? (_spreadOffset ? '⊞ 双页 2|3' : '⊞ 双页 1|2') : '📄 单页';
}
async function _applyModeChange(keepPage) {
  _pendingScrollY = 0;   // 清掉位置恢复残留，否则 setupContinuousMode 的定位会被跳过
  currentPage = keepPage;
  // 连续↔双页:优先**原地 reparent** 已渲染页(不重渲→不"重新加载"),再原地重标尺到新 fit 宽;失败才整列重建
  if (readMode !== 'single' && _remodeListInPlace()) {
    await _refitToWidth(true, false);   // 结构已与新 readMode 匹配 → 走原地重标尺(instant-resize + 后台高清化)
    const t = document.querySelector(`[data-page-num="${keepPage}"]`);
    // rAF 排在 _refitToWidth 内部按比例恢复滚动的 rAF 之后 → keepPage 定位最终生效(覆盖比例恢复)
    if (t) requestAnimationFrame(() => t.scrollIntoView({block: 'start', behavior: 'auto'}));
  } else {
    await _refitToWidth(true, true);   // 回退:整列重建(列表不完整 / single 残留)
    if (readMode === 'single') {
      await renderPage(keepPage);
    } else {
      const t = document.querySelector(`[data-page-num="${keepPage}"]`);
      if (t) setTimeout(() => t.scrollIntoView({block: 'start', behavior: 'auto'}), 80);
    }
  }
  _saveLastPosition({page: currentPage, mode: readMode, scale});
  window._auditScales && window._auditScales('mode');
  window._auditScales && setTimeout(() => window._auditScales('mode+1.2s'), 1200);
}
window.toggleReadMode = async () => {
  const keepPage = currentPage;
  // 单页↔连续;若当前在双页,切回单页(双页用 ⊞ 按钮单独控制)
  readMode = readMode === 'single' ? 'continuous' : 'single';
  try { localStorage.setItem('pdf-read-mode', readMode); } catch (_) {}
  _updateModeButtons();
  await _applyModeChange(keepPage);
  window._rememberOrientLayout?.();   // 把单页/连续选择记进当前方向(同 toggleSpread)。漏了的话切到单页后
                                      // 旋转/iOS resize 触发 _onOrientChange 会按旧存档把模式还原回双页→"单页缩放变多页"
};
// 双页(spread)按钮：单页/连续切换已删,双页按钮兼任「进入/错开/退出」三态循环——
// 连续 → 双页(offset0) → 双页(offset1,facing 错开) → 连续。
window.toggleSpread = async () => {
  const keepPage = currentPage;
  _spreadBeforePanel = null;   // 手动切模式 → 取消"关栏还原双页"(以用户手动选择为准)
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
  const avail = mainW - (ppr > 1 ? 4 : 0);   // 4 = .spread-row 两页间 gap(须与 CSS 一致)
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
// 适应兜底:渲染落定后若**实测**仍横向溢出(设备相关:去边比例取整 / dpr / 安全区让算出的 fit
// scale 偏大一点点 → 双页两栏并排刚好超出几 px),按实测比例再缩,保证"适应"一定铺满不溢出。
// 只在适应路径(_refitToWidth)末尾调,不影响用户主动放大。返回是否改了 scale。
function _fixHOverflow() {
  const main = document.getElementById('main');
  if (!main) return false;
  const cw = main.clientWidth, sw = main.scrollWidth;
  if (sw <= cw + 2) return false;               // 没溢出
  const k = cw / sw;
  if (k > 0.998) return false;                  // 溢出可忽略(<0.2%)
  const ns = Math.max(_ZOOM_MIN, scale * k);
  if (Math.abs(ns - scale) < 0.002) return false;
  scale = ns;
  _lastFitWidth = _mainContentWidth();
  return true;
}
async function _runFitOverflowGuard() {
  if (_refitBusy || !pdfDoc) return;
  if (!_fixHOverflow()) return;
  _refitBusy = true;
  try {
    // 统一:单页/连续/双页都用 wrap 路径(单页=唯一 wrap)→ _rescaleContinuousInPlace 原地重排;失败再按模式重建
    if (!(await _rescaleContinuousInPlace())) { if (readMode === 'single') await renderPage(currentPage); else await setupContinuousMode(); }
  } finally { _refitBusy = false; }
  requestAnimationFrame(() => window._updateMainOverflowX && window._updateMainOverflowX());
}
async function _refitToWidth(force, rebuild) {
  if (_refitBusy || !pdfDoc) return;
  const main = document.getElementById('main');
  const mainW = _mainContentWidth();
  if (mainW <= 0) return;
  if (!force && Math.abs(mainW - _lastFitWidth) < 30) return;
  _refitBusy = true;
  try {
    // 清掉双指捏合残留的预览 transform/zoom：手势异常结束(iPad 上一指先抬 / touchend·cancel 没正常触发)
    // 时 #page-container 会定格在 scale() 放大态。「适应」原来只重算 scale 没清它 → 视觉仍放大+横向溢出
    // (用户报「双指缩放后按适应失效，应取消缩放」)。同 _applyZoom 的防御,补到适应路径。
    // 清缩放残留:transform(捏合预览)+ zoom(单页缩放靠 page-container 的 CSS zoom 撑大)。必须显式清,
    // 别只靠重渲 —— iPad 上适应的重渲被触摸/竞态打断时残留不清 → 适应一瞬生效又弹回放大态(用户报的
    // 「单页适应后回弹」)。单页时 page-container 即 wrap,清它的 zoom 即解决;连续/双页 page-container 非
    // wrap(子 .page-wrap 的过渡 zoom 由 _rescaleContinuousInPlace 重设),清它无副作用。
    _pinch = null;
    { const _pc = document.getElementById('page-container');
      if (_pc) { _pc.style.transform = ''; _pc.style.transformOrigin = ''; _pc.style.zoom = ''; } }
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
    // 统一:三模式都走 wrap 原地重排(单页=唯一 wrap);rebuild 或原地失败才按模式重建(单页→renderPage 重建唯一 wrap)
    if (rebuild || !(await _rescaleContinuousInPlace())) {
      if (readMode === 'single') await renderPage(currentPage);
      else await setupContinuousMode();
    }
    // 按比例恢复滚动
    requestAnimationFrame(() => {
      if (container && container.offsetHeight) {
        main.scrollTop = Math.floor(ratio * container.offsetHeight);
      }
      _updateMainOverflowX();   // 适应/去边后内容铺满宽 → 锁横向拖动
      setTimeout(_runFitOverflowGuard, 160);   // 渲染落定后兜底:仍横向溢出(设备微差)则再缩到铺满
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
  // 记 fit 标志:当前是否=宽度适应(相对值,随容器宽变)。是 → 恢复时重算适应而非套旧 scale(否则换了
  // 容器宽/方向后旧绝对 scale 会盖掉新算的适应,表现为"宽度适应没应用")。否(手动放大过) → 才存绝对 scale 还原。
  let fit = 1;
  try { if (_fitPageW > 0) fit = Math.abs(scale - _computeFitScale(_fitPageW, _fitPageH)) < 0.025 ? 1 : 0; } catch (_) {}
  try { localStorage.setItem(_orientKey(o), JSON.stringify({ mode: readMode, crop: _cropOn ? 1 : 0, off: _spreadOffset || 0, fit, scale: +scale.toFixed(3) })); } catch (_) {}
}
function _loadOrientLayout(o) {
  try { const s = localStorage.getItem(_orientKey(o)); return s ? JSON.parse(s) : null; } catch (_) { return null; }
}
let _orientPendingScale = 0;   // 待套用的缩放(渲染完后由 _applyPendingOrientScale 处理)
function _applyOrientLayoutVars(lay) {   // 套到当前变量(不重渲染,调用方负责),返回是否套了
  if (!lay) return false;
  // 三态都要还原:旧实现只认 spread,把 single 误并成 continuous → 切单页后旋转/方向重判会变多页(用户报的
  // 「单页缩放变多页」)。single/spread/continuous 各自保留。
  readMode = (lay.mode === 'spread') ? 'spread' : (lay.mode === 'single' ? 'single' : 'continuous');
  _spreadOffset = lay.off ? 1 : 0;
  _cropOn = !!lay.crop;
  // fit=1(存档时是宽度适应) → 不套绝对 scale,让重排后的 _refitToWidth 重算适应(适应是相对值);
  // fit=0(手动放大过) → 套回绝对 scale。旧存档无 fit 字段时退回老行为(套 scale)。
  _orientPendingScale = (lay.fit === 1) ? 0 : ((lay.scale > 0) ? lay.scale : 0);
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
    // 不在此重存"离开方向"——此刻 mainW 已是新方向,算 fit 会用错宽度。离开方向的布局已由
    // 用户在该方向里的每次改动(toggleCrop/toggleSpread/fitWidth/_applyZoom 各自调 _rememberOrientLayout,
    // 当时宽度正确)以及载入基线存好了。
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
  { const _pc = document.getElementById('page-container'); if (_pc && _pc.style.transform) { _pc.style.transform = ''; _pc.style.transformOrigin = ''; } }  // 防御:清掉任何残留的捏合预览 transform(否则跟栅格缩放叠成两层)
  newScale = Math.max(_ZOOM_MIN, Math.min(_scaleMax, newScale));   // 下限放宽:可缩到比 fit-width 更小
  if (Math.abs(newScale - scale) < 0.005) return;
  _refitBusy = true;
  try {
    const main = document.getElementById('main');
    const container = document.getElementById('page-container');
    const ratio = container && container.offsetHeight ? main.scrollTop / Math.max(1, container.offsetHeight) : 0;
    scale = newScale;
    _lastFitWidth = _mainContentWidth();   // 占住 fit 宽，避免 ResizeObserver 把 scale 拉回自适应
    // 统一:三模式都走 wrap 原地重标尺(单页=唯一 wrap,跟连续同一套 CSS-zoom 瞬时缩放 + 后台重栅格化);失败按模式重建
    if (!(await _rescaleContinuousInPlace())) { if (readMode === 'single') await renderPage(currentPage); else await setupContinuousMode(); }
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
      window._auditScales && window._auditScales('zoom');
      window._auditScales && setTimeout(() => window._auditScales('zoom+1.2s'), 1200);
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
  // 桌面:Ctrl+滚轮 / 触控板双指捏合(Chrome 发 wheel+ctrlKey) → 接管成阅读器缩放(绕 cursor),
  // 否则走浏览器原生页面缩放(连工具栏一起缩 / 跟阅读器缩放叠加)。触摸屏走上面的 touch 分支。
  main.addEventListener('wheel', (e) => {
    if (!e.ctrlKey) return;
    e.preventDefault();
    if (_refitBusy || !pdfDoc) return;
    const mr = main.getBoundingClientRect();
    const factor = e.deltaY < 0 ? 1.12 : 0.89;
    const target = Math.max(_ZOOM_MIN, Math.min(_scaleMax, scale * factor));
    if (Math.abs(target - scale) < 0.005) return;
    _applyZoom(target, { fx: main.scrollLeft + (e.clientX - mr.left), fy: main.scrollTop + (e.clientY - mr.top), cx: e.clientX, cy: e.clientY, s0: scale });
  }, { passive: false });
}
if (document.readyState !== 'loading') _setupPinchZoom();
else window.addEventListener('DOMContentLoaded', _setupPinchZoom);

// ── 全屏阅读：隐藏顶栏腾空间（class 挂 <html>，<head> 内联脚本已在渲染前应用持久值）──
function _fsEnabled() { try { return localStorage.getItem('pdf-fullscreen') === '1'; } catch (_) { return false; } }
function _applyFullscreen(on) {
  document.documentElement.classList.toggle('fs-mode', on);
  const b = document.getElementById('fs-toggle'); if (b) b.classList.toggle('active', on);
}
// 浏览器/PWA 真·全屏(Fullscreen API)助手。iOS Safari 文档级不支持 → 静默失败,只保留页内全屏
function _browserFsActive() { return !!(document.fullscreenElement || document.webkitFullscreenElement); }
function _reqBrowserFs() {
  const el = document.documentElement, fn = el.requestFullscreen || el.webkitRequestFullscreen;
  if (fn && !_browserFsActive()) { try { Promise.resolve(fn.call(el)).catch(() => {}); } catch (_) {} }
}
function _exitBrowserFs() {
  const fn = document.exitFullscreen || document.webkitExitFullscreen;
  if (fn && _browserFsActive()) { try { Promise.resolve(fn.call(document)).catch(() => {}); } catch (_) {} }
}
window.toggleFullscreen = function () {
  const on = !document.documentElement.classList.contains('fs-mode');
  _applyFullscreen(on);
  try { localStorage.setItem('pdf-fullscreen', on ? '1' : '0'); } catch (_) {}
  if (on) _reqBrowserFs(); else _exitBrowserFs();   // 同时切浏览器/PWA 整窗真·全屏
  _toast?.(on ? '全屏阅读：点右上角 ⤢ 恢复' : '已退出全屏');
};
// 用户用 Esc/F11 退出浏览器全屏 → 同步退出页内全屏(顶栏恢复),两边状态不打架
['fullscreenchange', 'webkitfullscreenchange'].forEach(ev =>
  document.addEventListener(ev, () => {
    if (!_browserFsActive() && document.documentElement.classList.contains('fs-mode')) {
      _applyFullscreen(false);
      try { localStorage.setItem('pdf-fullscreen', '0'); } catch (_) {}
    }
  }));
_applyFullscreen(_fsEnabled());   // 载入同步页内全屏态(浏览器全屏须用户手势,不在此触发)

// 连续模式：所有页占位 + IntersectionObserver 懒加载
