/* pdf-tail.js — 从 pdf_reader.html 抽出的尾部内联 JS(同 pdf-uishared.js 的架构优化)。
 * 模板最后引入(reader.js 之后),依赖 window.__PDF_CFG/reader.js 全局。*/
// ══════════════ 手写笔（Apple Pencil 自动 + 桌面/触屏模式开关）══════════════
// 坐标全部归一化 0-1（相对页 css 尺寸），跨缩放/设备稳定；线宽固定 css px。
// canvas 永远 pointer-events:none（纯显示），绘制靠 page-wrap capture 拦截 pointerdown。
// ⚠ FILE_REL:手写保存/加载全靠它。历史上只被使用、从未定义(undefined)→ _inkSave/_inkFlushBeacon/
//   _inkLoadAll 首行 `if(!FILE_REL)return` 全部永远早退 → 手写能画能显示但从不 POST、重开从不加载 =
//   "手写重开消失"的真根因(快照修复/sendBeacon 兜底都排在这早退之后,全白搭)。来源同页面其它模块(__PDF_CFG.file_rel)。
const FILE_REL = (window.__PDF_CFG && __PDF_CFG.file_rel) || '';
const _ink = {
  tool: 'pen', color: '#e74c3c', width: 2.5,
  mode: false,          // 桌面/触屏手写模式（鼠标/手指接管）；Apple Pencil 始终可画
  visible: true,
  byPage: {},           // {num: [stroke,...]} 服务器加载
  drawing: null,        // {pw, num, stroke|eraser}
  lastPw: null,         // 最近绘制的页（撤销/清空作用对象）
  saveTimers: {},
  _lastTap: null,       // 上一下「极短 tap」的时间/位置（Pencil 双击切换检测用）
  quickErase: false,    // 双击进入的「临时橡皮」：空闲自动回笔（区别于工具栏手动点的长期橡皮）
  _revertT: null,       // 临时橡皮的自动回笔定时器
  _prevTool: 'pen',     // 进临时橡皮前的工具（回笔时还原它，而非死板回 pen）
};
window._ink = _ink;

function _inkActivePw() {
  return _ink.lastPw || document.querySelector(`.page-wrap[data-page-num="${currentPage}"]`);
}
function _inkStrokesOf(pw) { if (!pw.__inkStrokes) pw.__inkStrokes = []; return pw.__inkStrokes; }

// 几何/渲染/命中/撤销栈 = 共享核心 rc-ink.js(RCInk,三阅读器唯一实现);这里只留绑定本阅读器
// canvas 属性(__inkCanvas)与状态(_ink.visible)的薄 wrapper,函数名/签名不变。
function _inkNorm(pw, cx, cy) { return RCInk.norm(pw.__inkCanvas, cx, cy); }

function _inkRedraw(pw) {
  if (!pw || !pw.__inkCanvas) return;
  RCInk.redraw(pw.__inkCanvas, pw.__inkStrokes, _ink.visible);
}
window._inkRedraw = _inkRedraw;

function _inkDrawStroke(ctx, s, W, H, dpr) { RCInk.drawStroke(ctx, s, W, H, dpr); }

// ── 橡皮命中检测（归一化坐标）──
function _inkPtSeg(p, a, b) { return RCInk.ptSeg(p, a, b); }
function _inkHit(s, pt, thr) { return RCInk.hit(s, pt, thr); }
function _inkEraseAt(pw, pt) {
  const removed = RCInk.eraseAt(_inkStrokesOf(pw), pt, 0.014);
  if (removed) _inkRedraw(pw);
  return removed;
}

// ── undo / redo（每页）──
function _inkPushUndo(pw) { RCInk.pushUndo(pw); }

// ── 指针绘制 ──
// 绘制中：先 putImageData 还原「已完成笔画」快照，再用一条连续 quadratic 曲线重绘当前笔画
// → 单次 stroke 抗锯齿光滑（无增量段 round cap 毛边），且每帧只重绘一条 → 流畅
function _inkDrawCurrentOnSnap(d) {
  const pw = d.pw, cv = pw && pw.__inkCanvas; if (!cv) return;
  const ctx = cv.getContext('2d');
  const dpr = (cv.width / (parseFloat(cv.style.width) || cv.width)) || 1;
  if (d.snap) {
    ctx.putImageData(d.snap, 0, 0);
  } else {   // 快照不可用（极少）→ 回退重绘除当前外的全部
    ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, cv.width, cv.height);
    if (_ink.visible) for (const s of (pw.__inkStrokes || [])) if (s !== d.stroke) _inkDrawStroke(ctx, s, cv.width, cv.height, dpr);
  }
  _inkDrawStroke(ctx, d.stroke, cv.width, cv.height, dpr);
}

// 手写 FAB 图标(SF 线条 SVG，跟随按钮 currentColor：默认/on/erasing 各态色都自动跟)：笔态 / 橡皮态
const RC_INK_PEN = '<svg class="rc-fabi" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M19.3 7.3L16.7 4.7L4.7 16.7L4 20L7.3 19.3Z"/><path d="M7.3 19.3L4.7 16.7"/></svg>';
const RC_INK_ERASER = '<svg class="rc-fabi" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M19 20h-10.5l-4.21-4.3a1 1 0 0 1 0-1.41l10-10a1 1 0 0 1 1.41 0l5 5a1 1 0 0 1 0 1.41l-9.2 9.3"/><path d="M18 13.3l-6.3-6.3"/></svg>';
// 工具变更后同步「下方指示」：工具栏按钮高亮 + FAB 图标(笔/橡皮,工具栏隐藏时也看得到) + 临时橡皮脉冲环
function _inkUpdateToolUI() {
  const t = _ink.tool;
  document.querySelectorAll('#ink-toolbar button[data-tool]').forEach(b => b.classList.toggle('on', b.dataset.tool === t));
  const fb = document.getElementById('ink-fab');
  if (fb) {
    if (fb.__t) { clearTimeout(fb.__t); fb.__t = null; }     // 退场旧的瞬时反馈逻辑：现在是持久指示
    fb.innerHTML = (t === 'eraser') ? RC_INK_ERASER : RC_INK_PEN;
    fb.classList.toggle('ink-erasing', t === 'eraser' && _ink.quickErase);
  }
}
// 临时橡皮的自动回笔定时器：ms 后若不在擦(已抬笔)就回笔；正在擦则稍后再判
function _inkArmRevert(ms) {
  clearTimeout(_ink._revertT);
  _ink._revertT = setTimeout(() => {
    if (_ink.drawing && _ink.drawing.eraser) { _inkArmRevert(400); return; }   // 正擦中 → 不打断,稍后重判
    _inkExitQuickErase(true, true);
  }, ms);
}
// 退出临时橡皮。toPen=是否回到上一支工具；notify=自动回笔时给个明确提示(防「悄悄回笔又画上线」)
function _inkExitQuickErase(toPen, notify) {
  clearTimeout(_ink._revertT); _ink._revertT = null;
  const was = _ink.quickErase;
  _ink.quickErase = false;
  if (toPen) {
    _ink.tool = _ink._prevTool || 'pen';
    _inkUpdateToolUI();
    if (was && notify) { try { window.mfxToast && window.mfxToast('✏️ 已回到笔', { duration: 1100 }); } catch (_) {} }
    window.dlog && window.dlog('临时橡皮空闲 → 自动回笔 ' + _ink.tool);
  } else { _inkUpdateToolUI(); }
}
// 手指快速双击画布切换 笔 ↔ 临时橡皮（替代浏览器拿不到的 Apple Pencil 双击笔身手势）
function _inkDoubleTapSwitch(pw, skipUndo) {
  if (!skipUndo && _ink.tool !== 'eraser') {   // 仅笔模式 pen 误点才需撤销；手指双击无误点
    const arr = pw.__inkStrokes || [];
    if (arr.length) arr.pop();
    _inkRedraw(pw);
    _inkScheduleSave(pw, parseInt(pw.dataset.pageNum));
  }
  if (_ink.tool === 'eraser') {                 // 再次双击 → 立刻回笔(还原上一支工具)
    _inkExitQuickErase(true, false);
    window.dlog && window.dlog('双击 → 回笔 ' + _ink.tool);
  } else {                                       // 进入临时橡皮:记住上一支工具,亮起指示,武装「没擦 2.5s 回笔」
    _ink._prevTool = _ink.tool;
    _ink.tool = 'eraser';
    _ink.quickErase = true;
    _inkUpdateToolUI();
    _inkArmRevert(2500);
    window.dlog && window.dlog('双击 → 🧹 临时橡皮(空闲自动回笔)');
  }
}

// ── 便签跨界路由辅助(rc-stickynote:笔尖实时位置决定写便签还是写页面,一条笔画在边界切段)──
// 页面段收尾(切进便签前):擦边单点毛段丢弃,已画部分落主 canvas + debounce 保存。
function _inkEndPageSeg(d) {
  if (d.raf) { cancelAnimationFrame(d.raf); d.raf = null; }
  if (!d.eraser && d.stroke) {
    if (d.stroke.t === 'pen' && d.stroke.p.length < 2) { const arr = _inkStrokesOf(d.pw), i = arr.indexOf(d.stroke); if (i >= 0) arr.splice(i, 1); }
    d.stroke = null; d.snap = null;
  }
  if (d.pw) { _inkRedraw(d.pw); _inkScheduleSave(d.pw, d.num); }
}
// 页面开新段(出便签后):按当前点重新找页锚(可能已跨页);不在任何已渲染页上 → 悬空(dangling),后续 move 再试。
function _inkBeginPageSegAt(d, e) {
  const t = document.elementFromPoint(e.clientX, e.clientY);
  const pw = t && t.closest ? t.closest('.page-wrap') : null;
  if (!pw || !pw.__inkCanvas || pw.dataset.loaded !== '1') { d.dangling = true; return false; }
  const pt = _inkNorm(pw, e.clientX, e.clientY); if (!pt) { d.dangling = true; return false; }
  d.dangling = false;
  d.pw = pw; d.num = parseInt(pw.dataset.pageNum) || (typeof currentPage !== 'undefined' ? currentPage : 0); d.pageTouched = true; _ink.lastPw = pw;
  _inkPushUndo(pw);
  if (d.eraser) { _inkEraseAt(pw, pt); return true; }
  const cv = pw.__inkCanvas;
  _inkRedraw(pw);
  let snap = null;
  try { snap = cv.getContext('2d').getImageData(0, 0, cv.width, cv.height); } catch (_) {}
  const stroke = { t: 'pen', c: _ink.color, w: parseFloat(_ink.width) || 2.5, p: [pt] };
  _inkStrokesOf(pw).push(stroke);
  d.stroke = stroke; d.snap = snap;
  _inkDrawCurrentOnSnap(d);
  return true;
}

function _inkBeginGuard() {   // 笔一落就:①开手写守卫窗口(+1s)②清掉任何已有选中/查词框
  //   —— 补"第一下"空窗(守卫原来要等 move 才建);也治 palm 抢在笔之前落下已经选中了的情况(用户点子:画笔工作时全部取消选中)。
  try { window.__inkGuardUntil = Date.now() + 1000; } catch (_) {}
  try { if (window.__clearContentSelection) window.__clearContentSelection(); } catch (_) {}
}
function _inkPointerDown(e) {
  const pw = e.currentTarget; if (!pw || !pw.__inkCanvas) return;
  if (document.body.classList.contains('up-editing')) return;   // 插入页就地编辑期:禁用页面手写(覆盖层挡不住 .page-wrap 的捕获 pointerdown)
  // ★纸上的按钮/交互元素(「让 AI 检查」等)→ 放行让它收 click,绝不当画笔起点:否则 Apple Pencil 落在
  //   按钮上会被本 capture 的 preventDefault 吞掉 click,点了没反应(用户实测「点检查一直没出结果」根因)。
  if (e.target && e.target.closest && e.target.closest('.up2-b-btn,[role="button"],button,a')) return;
  // 便签 gate:手指落在便签上(聚焦文字/双击橡皮/长按样式)全归便签自己,页面 ink 不掺和
  const noteEl = e.target && e.target.closest ? e.target.closest('.rc-note') : null;
  if (noteEl && e.pointerType === 'touch') return;
  // 手指(touch)快速双击 → 切换 笔↔橡皮。仅「手写激活」时(开了工具栏 或 本次已画过笔)，
  // 不破坏纯阅读时手指双击选段；单指 tap/滑不拦截，照常选词/选段/滚动。
  // 笔正在画时忽略手指（挡掉写字时手掌/另一手误触切橡皮）；窗口/距离收紧防误判
  if (e.pointerType === 'touch' && !_ink.drawing && (_ink.mode || _ink.lastPw)) {
    const now = Date.now(), lt = _ink._lastTap;
    if (lt && now - lt.t < 350 && Math.hypot(e.clientX - lt.x, e.clientY - lt.y) < 32) {
      e.preventDefault(); e.stopPropagation();
      _ink._lastTap = null;
      _inkDoubleTapSwitch(pw, true);
      return;
    }
    _ink._lastTap = { t: now, x: e.clientX, y: e.clientY };
  }
  // 绘制：Apple Pencil 始终；鼠标仅桌面手写模式；手指永不画（只滚动/双击切工具）
  if (!(e.pointerType === 'pen' || (e.pointerType === 'mouse' && _ink.mode))) return;
  _inkBeginGuard();   // ★笔一落即开守卫 + 清已有选中(覆盖便签路由与普通两条画笔路径的公共点)
  // 便签路由:笔落在展开便签 body 上 → 整条手势仍由本模块主持,但这一段经 rc-stickynote 的
  // penBegin/penMove/penEnd 写进便签(跨界切割在 _inkPointerMove 处理)。落在 handle/工具等部位 →
  // 直接放行(不 stopPropagation),让便签自身手势(单击折叠/长按移动)接管;两种情况都不画页面
  // ——这就是 v1「pen 穿透便签写到书页」的根治点(本 capture 监听在 pw 层,先于便签自己的监听)。
  if (noteEl) {
    const RS0 = window.RC && RC.stickynote;
    const overBody = RS0 && RS0.penRoute && RS0.penRoute(e.clientX, e.clientY) &&
      !(e.target.closest('.rc-note-handle') || e.target.closest('.rc-note-tools') || e.target.closest('.rc-note-rs') || e.target.closest('.rc-note-del'));
    if (!overBody) return;
    e.preventDefault(); e.stopPropagation();
    try { pw.setPointerCapture(e.pointerId); } catch (_) {}
    RS0.penBegin(e, { eraser: _ink.tool === 'eraser' });
    _ink.drawing = { pw, num: parseInt(pw.dataset.pageNum) || (typeof currentPage !== 'undefined' ? currentPage : 0), noteRoute: true, eraser: _ink.tool === 'eraser' };
    document.addEventListener('pointermove', _inkPointerMove, true);
    document.addEventListener('pointerup', _inkPointerUp, true);
    document.addEventListener('pointercancel', _inkPointerUp, true);
    return;
  }
  e.preventDefault(); e.stopPropagation();               // 挡掉 char-layer 选词
  try { pw.setPointerCapture(e.pointerId); } catch (_) {}
  const num = parseInt(pw.dataset.pageNum) || (typeof currentPage !== 'undefined' ? currentPage : 0);
  _ink.lastPw = pw;
  const pt = _inkNorm(pw, e.clientX, e.clientY); if (!pt) return;
  if (_ink.tool === 'eraser') {
    if (_ink.quickErase) clearTimeout(_ink._revertT);    // 正在擦 → 暂停自动回笔计时(抬笔再重启)
    _inkPushUndo(pw); _inkEraseAt(pw, pt);
    _ink.drawing = { pw, num, eraser: true, pageTouched: true };
  } else {
    _inkPushUndo(pw);
    const cv = pw.__inkCanvas, ctx = cv.getContext('2d');
    _inkRedraw(pw);                                        // 画已完成笔画（此刻还不含当前笔画）
    let snap = null;
    try { snap = ctx.getImageData(0, 0, cv.width, cv.height); } catch (_) {}   // 拍快照
    const stroke = { t: _ink.tool, c: _ink.color, w: parseFloat(_ink.width) || 2.5, p: [pt] };
    _inkStrokesOf(pw).push(stroke);
    _ink.drawing = { pw, num, stroke, snap, raf: null, pageTouched: true };
    _inkDrawCurrentOnSnap(_ink.drawing);                  // 画当前起点
  }
  document.addEventListener('pointermove', _inkPointerMove, true);
  document.addEventListener('pointerup', _inkPointerUp, true);
  document.addEventListener('pointercancel', _inkPointerUp, true);
}
function _inkPointerMove(e) {
  const d = _ink.drawing; if (!d) return;
  try { window.__inkGuardUntil = Date.now() + 1000; } catch (_) {}   // 手写守卫:活动时刷到 +1s → 绘制中+抬笔后 1s 内屏蔽内容层查词/选中(界面操作不受影响)
  e.preventDefault();
  // ── 便签跨界路由(规格:笔尖实时位置决定写哪层;pen/橡皮参与切割,line/arrow/rect 形状不切)──
  const RS = window.RC && RC.stickynote;
  if (RS && RS.penRoute && (d.noteRoute || d.dangling || d.eraser || (d.stroke && d.stroke.t === 'pen'))) {
    const over = RS.penRoute(e.clientX, e.clientY);
    if (d.noteRoute) {
      if (over) { RS.penMove(e); return; }
      RS.penEnd({ boundary: true });        // 出便签:便签段收尾(擦边单点毛段丢弃)
      d.noteRoute = false;
      _inkBeginPageSegAt(d, e);             // 页面开新段(不在任何已渲染页上 → 悬空,后续 move 再试)
      return;
    }
    if (over) {                              // 进便签:页面段收尾 → 便签段开始
      _inkEndPageSeg(d);
      RS.penBegin(e, { eraser: !!d.eraser });
      d.noteRoute = true;
      return;
    }
    if (d.dangling) { _inkBeginPageSegAt(d, e); return; }
  }
  if (d.eraser) { const pt = _inkNorm(d.pw, e.clientX, e.clientY); if (pt) _inkEraseAt(d.pw, pt); return; }
  if (!d.stroke) return;   // 防御:路由暂态(悬空段等)下无页面笔画
  const s = d.stroke;
  if (s.t === 'pen') {
    const evs = e.getCoalescedEvents ? e.getCoalescedEvents() : [e];
    for (const ce of evs) {
      const p = _inkNorm(d.pw, ce.clientX, ce.clientY); if (!p) continue;
      const last = s.p[s.p.length - 1];
      // 抽稀：丢弃与上一点过近的采样点（120Hz Pencil 点太密 → quadratic 退化成折线、边缘发毛）
      if (last) { const dx = p[0] - last[0], dy = p[1] - last[1]; if (dx * dx + dy * dy < 6e-6) continue; }
      s.p.push(p);
    }
  } else {
    const pt = _inkNorm(d.pw, e.clientX, e.clientY); if (pt) s.p[1] = pt;
  }
  // rAF 节流：每帧最多重绘一次（快照还原 + 当前笔画一条）→ 平滑 + 流畅
  if (!d.raf) d.raf = requestAnimationFrame(() => { d.raf = null; if (_ink.drawing === d) _inkDrawCurrentOnSnap(d); });
}
function _inkPointerUp(e) {
  const d = _ink.drawing; if (!d) return;
  try { window.__inkGuardUntil = Date.now() + 1000; } catch (_) {}   // 抬笔:守卫再延 1s(手掌残留触摸不误触内容层)
  if (d.raf) { cancelAnimationFrame(d.raf); d.raf = null; }
  document.removeEventListener('pointermove', _inkPointerMove, true);
  document.removeEventListener('pointerup', _inkPointerUp, true);
  document.removeEventListener('pointercancel', _inkPointerUp, true);
  const RS = window.RC && RC.stickynote;
  if (d.noteRoute) { if (RS && RS.penEnd) RS.penEnd(); }   // 便签内抬笔:便签段收尾(单点=有意的点,保留)
  else if (!d.eraser && d.stroke) {
    const s = d.stroke;
    if (s.t !== 'pen' && s.p.length < 2) {   // 形状没拖动 → 丢弃
      const arr = _inkStrokesOf(d.pw), i = arr.indexOf(s); if (i >= 0) arr.splice(i, 1);
    }
  }
  const wasEraser = d.eraser;
  d.snap = null;
  _ink.drawing = null;
  if (d.pageTouched && d.pw) {
    _inkRedraw(d.pw);              // 最终全量平滑重绘
    _inkScheduleSave(d.pw, d.num);
  }
  if (wasEraser && _ink.quickErase) _inkArmRevert(900);   // 临时橡皮:擦完抬笔,停 0.9s 没再擦 → 自动回笔
}
window._inkPointerDown = _inkPointerDown;

// ── 保存 / 加载 ──
// ⚠ 900ms 防抖窗口内关页/切后台/整页 reload(插入页 job 完成后)会丢最后一批笔画 →
//   dirty 集合 + pagehide/切后台 sendBeacon 立即补发(后端 get_json 可读 Blob application/json)。
function _inkScheduleSave(pw, num) {
  // 插入页虚拟元素(.pdf-upage,有 __upRec):墨迹走边路径 —— 未绑真 id 只缓冲 el.__inkStrokes(_upInkPersist 内部早退),
  //   绑真 id 后 POST 到 realPage。绝不按 num 走下方 page-num POST(虚拟元素 num=NaN/currentPage 会污染别页墨迹)。
  //   ⚠ 只对有 __upRec 的虚拟元素生效;真 .page-wrap 永不带 __upRec → 全部照旧(legacy/普通页零影响)。
  //   #50 串页根因加固:某些路径(便签跨界/悬空重锚)解析出的 pw 可能是插入页祖先 .pdf-upage 但 __upRec 一时没挂,
  //   这时若走下方 byPage[num] 分支,就会把插入页笔画写进 currentPage → 未重编号的旧同名页(插入页下一页)显示它。
  //   → 判定放宽:只要 pw 自身或祖先是 .pdf-upage(插入页),一律走边路径,绝不写 byPage。
  var _up = null;
  if (pw) { _up = pw.__upRec ? pw : (pw.closest ? pw.closest('.pdf-upage') : null); }
  if (_up) { if (window._upInkPersist) window._upInkPersist(_up); return; }
  // ⚠ 捕获数组引用**快照**,绝不能在 setTimeout 里延迟读 pw.__inkStrokes:页面滚出视口/重渲染时
  //   04-render 会把 pw.__inkStrokes 置 null(见 _renderPageInto)→ 延迟读会把 null→[] 存进服务器,
  //   覆盖掉真笔画 = 手写重开消失的真根因。快照指向原数组(有笔画),不受 pw 后续被置 null 影响;
  //   继续画是往同一数组 push(快照同步可见),只有"重新赋值 pw.__inkStrokes"才分离(正是要防的)。
  const strokes = pw.__inkStrokes;
  _ink.byPage[num] = strokes;
  (_ink.dirty = _ink.dirty || {})[num] = true;
  (_ink.pend = _ink.pend || {})[num] = 1;   // 有新一批待存(save 成功清 dirty 前检查它:期间又画了 → 保持 dirty,防同步覆盖)
  clearTimeout(_ink.saveTimers[num]);
  _ink.saveTimers[num] = setTimeout(() => { if (_ink.pend) delete _ink.pend[num]; _inkSave(num, strokes); }, 900);
}
async function _inkSave(num, strokes) {
  if (!FILE_REL) return;
  try {
    const r = await fetch('/pdf/api/ink', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file: FILE_REL, page: num, strokes: strokes || [] }),
    });
    // POST **落地后**才清 dirty(审查:在途窗口清了会被对侧事件用 pre-POST 旧值覆盖本地);期间又画了(pend)→ 保持
    if (r && r.ok) { (_ink.echo = _ink.echo || {})[num] = Date.now(); }   // 55:记自存指纹,SSE 自回声 3s 内忽略
    if (r && r.ok && _ink.dirty && !(_ink.pend && _ink.pend[num])) delete _ink.dirty[num];
    if (!(r && r.ok)) (_ink.dirty = _ink.dirty || {})[num] = true;
  } catch (_) { (_ink.dirty = _ink.dirty || {})[num] = true; }   // 失败重标脏,flush 兜底还有机会补
}
function _inkFlushBeacon() {
  if (!FILE_REL || !_ink.dirty) return;
  for (const k in _ink.dirty) {
    const num = parseInt(k, 10);
    clearTimeout(_ink.saveTimers[num]);
    let ok = false;
    try {
      ok = !!(navigator.sendBeacon && navigator.sendBeacon('/pdf/api/ink',
        new Blob([JSON.stringify({ file: FILE_REL, page: num, strokes: _ink.byPage[num] || [] })], { type: 'application/json' })));
    } catch (_) {}
    if (ok) delete _ink.dirty[k];   // 送出去了才清(审查:sendBeacon 返回 false=没发出,清了 dirty 就没兜底、回前台还会被旧值覆盖)
  }
}
window.addEventListener('pagehide', _inkFlushBeacon);
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') _inkFlushBeacon(); });
async function _inkLoadAll() {
  if (!FILE_REL) return;
  try {
    const r = await fetch('/pdf/api/ink?file=' + encodeURIComponent(FILE_REL));
    const d = await r.json();
    if (d.ok && d.pages) {
      const hold = {};   // 本地有待存的页 → 保留本地值(审查:回前台全量重拉会用服务端旧值盖掉 pending 笔画)
      if (_ink.dirty) for (const dk in _ink.dirty) if (_ink.byPage[dk]) hold[dk] = _ink.byPage[dk];
      _ink.byPage = {};
      for (const k in d.pages) _ink.byPage[parseInt(k)] = d.pages[k];
      for (const dk in hold) _ink.byPage[parseInt(dk, 10)] = hold[dk];
      // ★#4 根治:会话内**插入页占用的页号**(乐观插入未重编号,DOM 里同号的是陈旧真页),
      //   其服务器墨迹归**插入页自己**(el.__inkStrokes)。删掉这些键,陈旧真页就拿不到插入页墨迹
      //   → 不再串页。重开书 PDF 已烧页、页号天然对齐,byPage 正常映射(那时 _upClaimed 为空)。
      try { const _cl = window._upClaimed || {}; for (const ck in _cl) delete _ink.byPage[parseInt(ck, 10)]; } catch (e) {}
      document.querySelectorAll('.page-wrap[data-loaded="1"]').forEach(pw => {
        const n = parseInt(pw.dataset.pageNum);
        if (_ink.dirty && _ink.dirty[n]) return;   // 待存页:画面保持本地
        if (window._upClaimed && window._upClaimed[n]) return;   // 该页号归插入页 → 陈旧真页不贴
        if (_ink.byPage[n]) { pw.__inkStrokes = JSON.parse(JSON.stringify(_ink.byPage[n])); _inkRedraw(pw); }
      });
    }
  } catch (_) {}
}
window._inkLoadAll = _inkLoadAll;
// ── 阅读器实时事件(SSE):别的视图(收藏夹/其它设备)画了本书墨迹 → ~1s 更新该页(同一张纸,与 EPUB 侧同一事件总线)──
(function () {
  if (typeof EventSource === 'undefined' || !FILE_REL) return;
  var es = null, _retry = 0;
  // 133:退避+抖动。原来 onerror 恒定 3s 重连——SSE 舱壁满时返 503,而 EventSource 按规范
  // 对非 200 **直接判失败、不自己重连**,于是全靠这里的定时器,变成每 3s 硬刷一次(风暴)。
  function _backoff() { return Math.min(30000, 3000 * Math.pow(2, Math.min(_retry, 4))) * (0.7 + Math.random() * 0.6); }
  function connect() {
    if (es) return;
    try {
      es = new EventSource('/pdf/api/reader-events');
      es.addEventListener('open', function () { _retry = 0; });   // 接通即清退避
      es.addEventListener('change', function (e) {
        if (document.visibilityState !== 'visible') return;
        var ev; try { ev = JSON.parse(e.data); } catch (_) { return; }
        if (ev && ev.kind === 'client-action' && ev.action && (!ev.file || ev.file === FILE_REL)) {   // MCP 遥控:统一走 RC.execRemote(共享层);rc-assistant 未载(legacy)回退 window 直调
          try { var _ra = ev.action; if (window.RC && RC.execRemote) RC.execRemote(_ra); else if (_ra && typeof window[_ra.fn] === 'function') window[_ra.fn].apply(null, _ra.args || []); } catch (_) {}
          return;
        }
        // ★ 任务运行时:纸内容变了(检查结果写回 / 块显隐)→ 重画那张用户页。
        //   之前这里只认 ink,text/run 事件被下面那行 return 掉 → 「让 AI 检查」结果写进了 sidecar 却不显示(用户实测卡住)。
        if (ev && ev.kind === 'text' && ev.file === FILE_REL) {
          try { if (window.__upRerender) window.__upRerender(ev.uid); } catch (_) {}
          return;
        }
        if (ev && ev.kind === 'run' && ev.file === FILE_REL && ev.run) {
          try { if (window.__upRunProgress) window.__upRunProgress(ev.run); } catch (_) {}
          return;
        }
        if (!ev || ev.kind !== 'ink' || ev.file !== FILE_REL) return;
        var num = parseInt(ev.uid, 10); if (!num) return;
        if (window._upClaimed && window._upClaimed[num]) return;             // #4 该页号归会话内插入页,别贴给陈旧真页
        if (_ink.drawing && _ink.drawing.num === num) return;               // 正在画这页 → 不打断
        if (_ink.dirty && _ink.dirty[num]) return;                          // 本地有待存 → 别用服务端旧值覆盖
        if (_ink.echo && _ink.echo[num] && Date.now() - _ink.echo[num] < 3000) return;   // 55 自回声抑制:自己刚存的不用回放——
        // 插入页场景它还会按页号命中"未重编号的旧下一页"把墨迹串过去(publish 无发起端排除,前端兜)
        _ink._sseT = _ink._sseT || {};
        clearTimeout(_ink._sseT[num]);                                       // 每页 250ms 合并(防事件风暴每笔一次全书 GET)
        _ink._sseT[num] = setTimeout(function () {
          fetch('/pdf/api/ink?file=' + encodeURIComponent(FILE_REL)).then(function (r) { return r.json(); }).then(function (d) {
            if (!(d && d.ok)) return;
            // ⚠ 响应落地时**复查**守卫(审查 high:fetch 在途用户可能起笔,只在事件到达时查会把进行中笔画覆盖丢掉)
            if (_ink.drawing && _ink.drawing.num === num) return;
            if (_ink.dirty && _ink.dirty[num]) return;
            var fresh = (d.pages || {})[String(num)] || [];
            if (JSON.stringify(fresh) === JSON.stringify(_ink.byPage[num] || [])) return;
            _ink.byPage[num] = JSON.parse(JSON.stringify(fresh));
            var pw = document.querySelector('.page-wrap[data-page-num="' + num + '"]');
            if (pw) { pw.__inkStrokes = JSON.parse(JSON.stringify(fresh)); if (pw.__inkCanvas) _inkRedraw(pw); }
          }).catch(function () {});
        }, 250);
      });
      es.onerror = function () { if (es && es.readyState === 2) { es = null; _retry++; setTimeout(connect, _backoff()); } };
    } catch (_) { es = null; }
  }
  connect();
  document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'visible') { connect(); try { _inkLoadAll(); } catch (_) {} } });
})();

// ── 工具栏 ──
window.inkToggle = () => {
  _ink.mode = !_ink.mode;
  document.body.classList.toggle('ink-mode', _ink.mode);
  document.getElementById('ink-fab').classList.toggle('on', _ink.mode);
  document.getElementById('ink-toolbar').classList.toggle('show', _ink.mode);
};
window.inkSetTool = (t, btn) => {
  // 工具栏手动选 = 长期工具(含手动点橡皮):清掉临时橡皮态 + 自动回笔计时器,不自动回笔
  clearTimeout(_ink._revertT); _ink._revertT = null; _ink.quickErase = false;
  _ink.tool = t;
  _inkUpdateToolUI();
};
window.inkSetColor = (c, btn) => {
  _ink.color = c;
  document.querySelectorAll('#ink-toolbar .ink-color').forEach(b => b.classList.toggle('on', b === btn));
};
window.inkSetWidth = (v) => { _ink.width = parseFloat(v) || 2.5; };
window.inkToggleVisible = () => {
  _ink.visible = !_ink.visible;
  const eye = document.getElementById('ink-eye'); if (eye) eye.style.opacity = _ink.visible ? '1' : '.4';
  document.querySelectorAll('.page-wrap[data-loaded="1"]').forEach(pw => _inkRedraw(pw));
};
window.inkUndo = () => {
  const pw = _inkActivePw(); if (!pw || !pw.__inkUndo || !pw.__inkUndo.length) return;
  if (!pw.__inkRedo) pw.__inkRedo = [];
  pw.__inkRedo.push(JSON.stringify(pw.__inkStrokes || []));
  pw.__inkStrokes = JSON.parse(pw.__inkUndo.pop());
  _ink.lastPw = pw; _inkRedraw(pw); _inkScheduleSave(pw, parseInt(pw.dataset.pageNum));
};
window.inkRedo = () => {
  const pw = _inkActivePw(); if (!pw || !pw.__inkRedo || !pw.__inkRedo.length) return;
  if (!pw.__inkUndo) pw.__inkUndo = [];
  pw.__inkUndo.push(JSON.stringify(pw.__inkStrokes || []));
  pw.__inkStrokes = JSON.parse(pw.__inkRedo.pop());
  _ink.lastPw = pw; _inkRedraw(pw); _inkScheduleSave(pw, parseInt(pw.dataset.pageNum));
};
window.inkClearPage = () => {
  const pw = _inkActivePw(); if (!pw || !(pw.__inkStrokes && pw.__inkStrokes.length)) return;
  _inkPushUndo(pw); pw.__inkStrokes = []; _ink.lastPw = pw;
  _inkRedraw(pw); _inkScheduleSave(pw, parseInt(pw.dataset.pageNum));
};

