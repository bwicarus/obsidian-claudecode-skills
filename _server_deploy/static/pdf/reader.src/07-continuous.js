async function setupContinuousMode() {
  const container = document.getElementById('page-container');
  container.innerHTML = '';
  if (_contIO) { _contIO.disconnect(); _contIO = null; }
  // 占位 size：用第 1 页尺寸估算所有页（同一本书各页尺寸基本一致），避免逐页 getPage 阻塞主线程。
  // 渲染该页时 _renderPageInto 会清掉固定高、按真实 viewport 撑开 → 尺寸自动修正。
  const p1 = await pdfDoc.getPage(1);
  const v1 = p1.getViewport({scale});
  // 去边时占位尺寸也按可见(裁切后)宽高,跟渲染后的 wrap 一致 → 未渲染页不会比已渲染页宽
  const estW = Math.floor(v1.width * _cropVisWFrac()), estH = Math.floor(v1.height * _cropVisHFrac());
  const _mkPh = (num, marg) => {
    const ph = document.createElement('div');
    ph.className = 'page-wrap';
    ph.dataset.pageNum = num;
    ph.dataset.loaded = '0';
    ph.style.width  = estW + 'px';
    ph.style.height = estH + 'px';
    ph.style.background = '#fff';
    ph.style.color = '#888';
    ph.style.display = 'flex';
    ph.style.alignItems = 'center';
    ph.style.justifyContent = 'center';
    ph.style.margin = marg;
    ph.textContent = '… 第 ' + num + ' 页';
    return ph;
  };
  const mainEl = document.getElementById('main');
  // IntersectionObserver 先建好,占位**边建边 observe**(把 O(N) 的 observe 也分摊掉,不再一次性 observe 几千个)。
  _contIO = new IntersectionObserver((entries) => {
    entries.forEach(e => {
      if (e.isIntersecting && e.target.dataset.loaded === '0') {
        _renderPageInto(parseInt(e.target.dataset.pageNum), e.target);
      }
    });
  }, {rootMargin: '3000px', root: mainEl});   // 提前渲染 ~2-3 页(大图书 644KB/页,留足提前量防翻页卡)
  // 「打开即可用」:目标页占位一就位 → 滚过去 + 显式渲染 + **立刻撤加载遮罩**(不必等全书几千页占位都建完)。
  let _targetReady = false;
  const _afterTargetReady = () => {
    if (_targetReady) return;
    const targetPh = container.querySelector(`[data-page-num="${currentPage}"]`);
    if (!targetPh) return;
    _targetReady = true;
    targetPh.scrollIntoView({block: 'start', behavior: 'auto'});  // _pendingScrollY 时 _restoreScrollAfterRender 会再精修
    _renderPageInto(currentPage, targetPh).catch(() => {});        // 目标页图像后台渲染、随后弹出
    pdfLoadHide();   // 目标页就位即撤遮罩 → 余下占位继续后台分批建,用户已可读/可返回
  };
  // ⚡ 根治「打开新文件时点不动返回」:**分批建占位,每批 setTimeout(0) 让出事件循环**。
  //   旧版同步 for 给全书每页建占位(几千页)冻主线程 2-5s,期间事件队列停摆,连原生 <a>/标题返回都点不动。
  //   分批后主线程每批只忙几 ms,加载中也能点「返回书架」/标题返回。
  const CHUNK = 80;
  if (readMode === 'spread') {
    // 双页：每行一个 .spread-row 容器,内含 1–2 个 page-wrap 并排;行间距交给 .spread-row
    const rows = Array.from(_spreadRows(pdfDoc.numPages, _spreadOffset));
    for (let i = 0; i < rows.length; i += CHUNK) {
      const frag = document.createDocumentFragment(), phs = [];
      for (const row of rows.slice(i, i + CHUNK)) {
        const rowEl = document.createElement('div');
        rowEl.className = 'spread-row';
        for (const num of row) { const ph = _mkPh(num, '0'); phs.push(ph); rowEl.appendChild(ph); }
        frag.appendChild(rowEl);
      }
      container.appendChild(frag);
      _afterTargetReady();
      phs.forEach(ph => _contIO.observe(ph));
      if (i + CHUNK < rows.length) await new Promise(r => setTimeout(r, 0));   // 让出事件循环
    }
  } else {
    const total = pdfDoc.numPages;
    for (let start = 1; start <= total; start += CHUNK) {
      const frag = document.createDocumentFragment(), phs = [];
      const end = Math.min(start + CHUNK - 1, total);
      for (let num = start; num <= end; num++) { const ph = _mkPh(num, '0 auto 12px'); phs.push(ph); frag.appendChild(ph); }
      container.appendChild(frag);
      _afterTargetReady();
      phs.forEach(ph => _contIO.observe(ph));
      if (end < total) await new Promise(r => setTimeout(r, 0));   // 让出事件循环
    }
  }
  _afterTargetReady();   // 兜底(目标页号异常等极端情况;正常路径上面已触发)
  mainEl.addEventListener('scroll', _onContinuousScroll, {passive: true});
}

// 模式切换(连续↔双页,含双页 offset 1|2 ↔ 2|3 变化)**原地重排**:把已渲染的 .page-wrap 节点直接 reparent
// (移动 DOM 不重渲,图像/选词层/墨迹/loaded 状态全保留),只重组 行/列 结构——
// 连续=各 wrap 直挂 container;双页=每行一个 .spread-row 裹 1~2 个 wrap。
// 之后调用方走 _refitToWidth(true,false) 原地重标尺(instant-resize 到新 fit 宽 + 后台高清化),全程不"重新加载"。
// 返回 false(列表不完整,例如来自已废弃的 single 残留)→ 调用方回退 setupContinuousMode 整列重建。
function _remodeListInPlace() {
  const container = document.getElementById('page-container');
  if (!container || !pdfDoc) return false;
  const wraps = [...container.querySelectorAll('.page-wrap')];
  if (wraps.length !== pdfDoc.numPages) return false;   // 必须是完整页列表,否则回退重建(防缺页)
  wraps.sort((a, b) => (parseInt(a.dataset.pageNum, 10) || 0) - (parseInt(b.dataset.pageNum, 10) || 0));
  const byNum = new Map(wraps.map(w => [parseInt(w.dataset.pageNum, 10), w]));
  const frag = document.createDocumentFragment();
  if (readMode === 'spread') {
    for (const row of _spreadRows(pdfDoc.numPages, _spreadOffset)) {
      const rowEl = document.createElement('div');
      rowEl.className = 'spread-row';
      for (const num of row) { const w = byNum.get(num); if (w) { w.style.margin = '0'; rowEl.appendChild(w); } }   // appendChild=移动节点(含已渲染内容)
      frag.appendChild(rowEl);
    }
  } else {   // continuous:各 wrap 直挂,恢复单列行距
    for (const w of wraps) { w.style.margin = '0 auto 12px'; frag.appendChild(w); }
  }
  container.innerHTML = '';   // wraps 已被 frag 接管(已 reparent);此处只清掉空的旧 .spread-row 壳。与下一行同步执行→无空容器 painted 帧
  container.appendChild(frag);
  return true;
}
window._remodeListInPlace = _remodeListInPlace;

// ── 缩放/切模式诊断:列出每页 __renderScale + 图宽分布,定位"哪些页停在旧 scale"。debug 开时打到 #debug-log。──
const READER_BUILD = 'reader-fix-74';
window._auditScales = function (tag) {
  try {
    const wraps = [...document.querySelectorAll('.page-wrap')];
    const loaded = wraps.filter(w => w.dataset.loaded === '1');
    const rs = {}, iw = {}, ww = {};
    for (const w of loaded) {
      const k = (w.__renderScale || 0).toFixed(3); rs[k] = (rs[k] || 0) + 1;
      const im = w.querySelector('.page-img, canvas');
      const wv = im ? Math.round(parseFloat(im.style.width) || im.offsetWidth || 0) : 0; iw[wv] = (iw[wv] || 0) + 1;
      const wpw = Math.round(w.offsetWidth || 0); ww[wpw] = (ww[wpw] || 0) + 1;
    }
    window.dlog && window.dlog('[' + READER_BUILD + '|' + tag + '] mode=' + readMode + ' scale=' + (+scale).toFixed(3)
      + ' loaded=' + loaded.length + '/' + wraps.length
      + ' renderScale=' + JSON.stringify(rs) + ' imgW=' + JSON.stringify(iw) + ' wrapW=' + JSON.stringify(ww));
  } catch (e) { window.dlog && window.dlog('audit err: ' + (e && e.message)); }
};

// 缩放时「原地重标尺」——两段式,**绝不串行阻塞手势**(双指松手只调一次,旧实现逐页 await decode → 放大要等 N 页 fetch 串行 1~2s = "失效",缩小先 snap 回旧大小再换 = "抽搐"):
//   ① 瞬时(同步):用各页**现有位图**按新 scale 做 CSS 缩放到精确新尺寸(__pageWPt×scale = 服务端权威点尺寸)。
//      布局即刻正确(focal 复位/滚动读到的 offsetHeight 立即准)、无白闪、不 snap 回旧大小;暂时由旧栅格放缩→略软。
//   ② 后台(不 await):并发重栅格化到新 scale 高清图,各自 decode-first 换入(缩小走缓存秒回;放大并发一次 fetch ≈300ms)。
//      重入由 _renderPageImg 的 __imgGen 守卫(最后发起的赢),所以期间用户再捏合也安全。
// 返回 false(没建过列表 / 结构不符)→ 调用方回退 setupContinuousMode。global scale 已由调用方设好。
async function _rescaleContinuousInPlace() {
  const container = document.getElementById('page-container');
  const wraps = [...container.querySelectorAll('.page-wrap')];
  if (!wraps.length) return false;
  // 结构必须匹配 readMode(双页有 .spread-row 行,单列没有);不匹配=列数/排版真变了→回退重建
  if ((readMode === 'spread') !== !!container.querySelector('.spread-row')) return false;
  let estW = 0, estH = 0;
  const _meta = window.__imgMeta;
  if (_imgMode && _meta && _meta.page_w) {   // 图片模式:用页 meta 同步算占位尺寸,免去 await getPage → 整个函数全同步,_refitBusy 立刻释放(手势不卡)
    estW = Math.floor(_meta.page_w * scale * _cropVisWFrac()); estH = Math.floor(_meta.page_h * scale * _cropVisHFrac());
  } else {
    try {
      const v1 = (await pdfDoc.getPage(1)).getViewport({ scale });
      estW = Math.floor(v1.width * _cropVisWFrac()); estH = Math.floor(v1.height * _cropVisHFrac());
    } catch (_) {}
  }
  for (const w of wraps) {
    // loaded==='1' 已结算页;loaded==='0' 但有 .page-img/canvas = 上次缩放的后台重渲仍 in-flight
    // (decode-first 期间旧图还挂着)。这类页也必须重标尺并重发渲染:否则连续多次捏合时它被当占位跳过,
    // 上一次的后台渲染(用旧 scale 的 viewport)随后完成、把它定格在旧 scale,本次又没碰它
    // → 页面 scale 混杂(部分页不跟着缩放)。重发的 _renderPageInto 会 bump __imgGen 让旧 in-flight 作废。
    if (w.dataset.loaded === '1' || w.querySelector('.page-img, canvas')) {
      // ① 瞬时:CSS zoom 把现有位图(renderScale 像素)按比例缩到当前 scale。zoom **同时缩放内容+布局占位**
      //    (不像 transform 不影响布局)→ 滚动/行距即刻精确;页内仍是 renderScale 像素坐标 → 稳态 zoom=1 时
      //    选中/高亮/去边坐标完全不变。所有页一次过同步设 zoom → 行间 scale 永远一致;就算某页后台重渲漏了/失败,
      //    zoom 也一直顶着正确视觉尺寸 → 结构上不可能出现"部分页不跟着缩放"。(PDF.js cssTransform 同款思路)
      const rs = w.__renderScale || 0;
      if (rs > 0) w.style.zoom = (scale / rs);
      // ② 后台按新 scale 重栅格化(decode-first 无闪 + __imgGen 防叠加);完成时 _renderPageImg 把 zoom 归 1(原生清晰)。
      w.dataset.loaded = '0';
      _renderPageInto(parseInt(w.dataset.pageNum, 10), w).catch(() => {});
    } else if (estW) {
      w.style.width = estW + 'px'; w.style.height = estH + 'px';
    }
  }
  return true;
}
window._rescaleContinuousInPlace = _rescaleContinuousInPlace;

// ── 内存控制:滚出视口足够远的已渲染页自动卸载(释放 canvas/各叠层),保留占位高度→滚动不跳。 ──
// iPad 内存吃紧时 iOS 会把整个 Safari 标签回收(回来要重载)。连续模式若把访问过的几百页 canvas 全留
// 在 DOM,内存无上限增长 → 更早被回收。卸载后 dataset.loaded='0',滚回视口由 IntersectionObserver 重渲。
// 卸载阈值(5000px)必须 > IO 的 rootMargin(3000px),留 2000px 缓冲,避免边界来回抖动反复卸载/重渲。
const _KEEP_DIST_PX = 5000;
// preRect: 调用方批量预读好的 getBoundingClientRect(读写分离,免逐页 读→写→读 强制 reflow)
function _unloadPage(w, preRect) {
  const num = w.dataset.pageNum;
  // 占位尺寸用 **fit 公式**(页 meta × 当前 scale × 去边可见比),跟 setupContinuousMode 的 estW 一致 →
  // 永远精确 fit-width;**不**用 getBoundingClientRect 测量(若卸载瞬间该页还带补偿 zoom,测到的是放大/缩小后的
  // 错误宽,定格进占位 → 滚动经过时整列宽度乱 = "适应失效")。meta 缺失才退回测量。
  let wd, h;
  const _meta = window.__imgMeta;
  if (_imgMode && _meta && _meta.page_w && scale) {
    wd = Math.floor(_meta.page_w * scale * _cropVisWFrac());
    h  = Math.floor(_meta.page_h * scale * _cropVisHFrac());
  } else {
    const _r = preRect || w.getBoundingClientRect();
    wd = Math.round(_r.width); h = Math.round(_r.height);
  }
  w.__charLayer = null; w.__charBoxes = null; w.__inkStrokes = null; w.__inkCanvas = null;
  w.__vocabMarks = null; w.__vocabSentences = null; w.__furigana = null;
  w.__pageWPt = null; w.__pageHPt = null; w.__renderScale = null;
  w.__imgGen = (w.__imgGen || 0) + 1;   // 作废可能在途的旧渲染:decode 回来发现 gen 变了→放弃,不会把已卸载页又渲染回来
  w.querySelectorAll('canvas').forEach(c => { c.width = 0; c.height = 0; });   // 显式清零,iOS 释放 backing 更彻底
  w.innerHTML = '';
  w.classList.remove('crop-on');
  w.style.zoom = '';   // 撤掉缩放过渡期的补偿 zoom → 占位盒按显式视觉尺寸,不再被 zoom 二次缩放
  w.style.width = wd + 'px'; w.style.height = h + 'px';
  w.style.background = '#fff'; w.style.color = '#888';
  w.style.display = 'flex'; w.style.alignItems = 'center'; w.style.justifyContent = 'center';
  w.textContent = '… 第 ' + num + ' 页';
  w.dataset.loaded = '0';
}
// wraps 可由调用方(_onContinuousScroll)传入复用,免二次全量 querySelectorAll
function _unloadFarPages(wraps) {
  const mainEl = document.getElementById('main');
  if (!mainEl) return;
  const mr = mainEl.getBoundingClientRect(), vh = mainEl.clientHeight;
  wraps = wraps || document.querySelectorAll('#page-container .page-wrap');
  // 读写分离:先一轮只读 rect 收集待卸载页(rect 一并传给 _unloadPage 当回退测量),
  // 再统一写 → 不再 读rect→写样式→读rect 交错触发逐页强制 reflow(占位尺寸不变,预读 rect 仍有效)
  const toUnload = [];
  for (const w of wraps) {
    if (w.dataset.loaded !== '1') continue;   // 占位页便宜跳过(不调 getBoundingClientRect)
    const r = w.getBoundingClientRect();
    const relTop = r.top - mr.top, relBot = r.bottom - mr.top;
    if (relBot < -_KEEP_DIST_PX || relTop > vh + _KEEP_DIST_PX) toUnload.push([w, r]);
  }
  for (const [w, r] of toUnload) _unloadPage(w, r);
}

let _scrollTimer = null;
function _onContinuousScroll() {
  if (_scrollTimer) return;
  _scrollTimer = setTimeout(() => {
    _scrollTimer = null;
    const mainEl = document.getElementById('main');
    const mainTop = mainEl.getBoundingClientRect().top;
    const viewportH = mainEl.clientHeight;
    // 找视口中线穿过的页面——wraps 文档序=页序、top 竖向单调非降 → 二分代替全书 O(N) rect 扫描
    // (千页书每 tick 5-15ms → <1ms)。语义与原 for 扫描一致:
    //   · center 落页间隙(bottom 校验不过)→ 不更新页码(否则 URL ?page=/KG 拉取/预取漂移)
    //   · spread 同行两页同 top,二分落组尾(右页)→ 回退到同 top 组首,组内按 DOM 序取第一个
    //     bottom>=center 者(等高=左页,与原行为相同)
    const center = mainTop + viewportH / 2;
    const wraps = document.querySelectorAll('#page-container .page-wrap');
    let lo = 0, hi = wraps.length - 1, hit = -1;
    while (lo <= hi) {   // 找最后一个 top<=center 的 wrap(空 NodeList 时 hi=-1 自然跳过)
      const mid = (lo + hi) >> 1;
      if (wraps[mid].getBoundingClientRect().top <= center) { hit = mid; lo = mid + 1; }
      else hi = mid - 1;
    }
    let target = null;
    if (hit >= 0) {
      const hitTop = wraps[hit].getBoundingClientRect().top;
      let g = hit;   // 回退到同 top 组首(±1px 容差吸收子像素;单列页距数百 px 不会误并)
      while (g > 0 && Math.abs(wraps[g - 1].getBoundingClientRect().top - hitTop) <= 1) g--;
      for (let i = g; i <= hit; i++) {
        if (wraps[i].getBoundingClientRect().bottom >= center) { target = wraps[i]; break; }
      }
    }
    if (target) {
      const num = parseInt(target.dataset.pageNum);
      if (num !== currentPage) {
        currentPage = num;
        { const _pc = document.getElementById('page-cur'); if (_pc) _pc.textContent = (window._dispPage ? window._dispPage(num) : num); }
        // 同步 URL + 拉 KG 节点
        const u = new URL(location.href);
        u.searchParams.set('page', num);
        history.replaceState(null, '', u);
        loadPageNodes(num);
        if (window._prefetchAround) window._prefetchAround(num);   // 翻到新页 → 后台预取前后页(read-ahead)
      }
    }
    _unloadFarPages(wraps);   // 顺带卸载远处页,封顶内存(复用已取好的 wraps)
  }, 200);
}

// 选中浮出工具栏 —— 跨桌面（mouseup）+ 触屏（touchend）+ 通用（selectionchange）
const toolbar = document.getElementById('sel-toolbar');
let lastSelText = '';
     _updateSelPreview('');
let _selTimer = null;
