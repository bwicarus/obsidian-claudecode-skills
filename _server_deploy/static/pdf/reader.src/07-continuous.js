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
  const frag = document.createDocumentFragment();
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
  if (readMode === 'spread') {
    // 双页：每行一个 .spread-row 容器,内含 1–2 个 page-wrap 并排;行间距交给 .spread-row
    for (const row of _spreadRows(pdfDoc.numPages, _spreadOffset)) {
      const rowEl = document.createElement('div');
      rowEl.className = 'spread-row';
      for (const num of row) rowEl.appendChild(_mkPh(num, '0'));
      frag.appendChild(rowEl);
    }
  } else {
    for (let num = 1; num <= pdfDoc.numPages; num++) frag.appendChild(_mkPh(num, '0 auto 12px'));
  }
  container.appendChild(frag);   // 一次性插入，避免 N 次 reflow
  const mainEl = document.getElementById('main');
  // 「打开即可用」关键:先把视口定位到目标页(占位高已按首页尺寸估算、各页同尺寸→定位准),
  // 再 **显式渲染目标页并等它完成**(图像+选词层都就绪=可用),最后才 observe 其余页懒加载。
  // 旧逻辑是先 observe(IO 立刻渲染页1-3,白白浪费+抢带宽)→ 50ms 后才滚到目标页。
  const targetPh = container.querySelector(`[data-page-num="${currentPage}"]`);
  if (targetPh) {
    targetPh.scrollIntoView({block: 'start', behavior: 'auto'});  // _pendingScrollY 时 _restoreScrollAfterRender 会再精修
    // 不 await:占位建好 + 滚到目标页后就让加载遮罩撤掉(loadPdf 里 pdfLoadHide),
    // 目标页图像在后台渲染、随后弹出(用户先看到占位"…第N页",几百ms 后图片出来)。
    _renderPageInto(currentPage, targetPh).catch(() => {});
  }
  // IntersectionObserver:此时视口已在目标页 → 只渲染其附近 ±1400px 的页(目标页已 loaded=1 会跳过)
  _contIO = new IntersectionObserver((entries) => {
    entries.forEach(e => {
      if (e.isIntersecting && e.target.dataset.loaded === '0') {
        _renderPageInto(parseInt(e.target.dataset.pageNum), e.target);
      }
    });
  }, {rootMargin: '3000px', root: mainEl});   // 提前渲染 ~2-3 页(大图书 644KB/页,留足提前量防翻页卡)
  container.querySelectorAll('.page-wrap').forEach(ph => _contIO.observe(ph));
  mainEl.addEventListener('scroll', _onContinuousScroll, {passive: true});
}

// ── 内存控制:滚出视口足够远的已渲染页自动卸载(释放 canvas/各叠层),保留占位高度→滚动不跳。 ──
// iPad 内存吃紧时 iOS 会把整个 Safari 标签回收(回来要重载)。连续模式若把访问过的几百页 canvas 全留
// 在 DOM,内存无上限增长 → 更早被回收。卸载后 dataset.loaded='0',滚回视口由 IntersectionObserver 重渲。
// 卸载阈值(5000px)必须 > IO 的 rootMargin(3000px),留 2000px 缓冲,避免边界来回抖动反复卸载/重渲。
const _KEEP_DIST_PX = 5000;
function _unloadPage(w) {
  const h = w.offsetHeight, wd = w.offsetWidth, num = w.dataset.pageNum;   // 记当前尺寸,占位沿用→不跳
  w.__charLayer = null; w.__charBoxes = null; w.__inkStrokes = null; w.__inkCanvas = null;
  w.__vocabMarks = null; w.__vocabSentences = null; w.__furigana = null;
  w.__pageWPt = null; w.__pageHPt = null;
  w.querySelectorAll('canvas').forEach(c => { c.width = 0; c.height = 0; });   // 显式清零,iOS 释放 backing 更彻底
  w.innerHTML = '';
  w.classList.remove('crop-on');
  w.style.width = wd + 'px'; w.style.height = h + 'px';
  w.style.background = '#fff'; w.style.color = '#888';
  w.style.display = 'flex'; w.style.alignItems = 'center'; w.style.justifyContent = 'center';
  w.textContent = '… 第 ' + num + ' 页';
  w.dataset.loaded = '0';
}
function _unloadFarPages() {
  const mainEl = document.getElementById('main');
  if (!mainEl) return;
  const mr = mainEl.getBoundingClientRect(), vh = mainEl.clientHeight;
  const wraps = document.querySelectorAll('#page-container .page-wrap');
  for (const w of wraps) {
    if (w.dataset.loaded !== '1') continue;   // 占位页便宜跳过(不调 getBoundingClientRect)
    const r = w.getBoundingClientRect();
    const relTop = r.top - mr.top, relBot = r.bottom - mr.top;
    if (relBot < -_KEEP_DIST_PX || relTop > vh + _KEEP_DIST_PX) _unloadPage(w);
  }
}

let _scrollTimer = null;
function _onContinuousScroll() {
  if (_scrollTimer) return;
  _scrollTimer = setTimeout(() => {
    _scrollTimer = null;
    const mainEl = document.getElementById('main');
    const mainTop = mainEl.getBoundingClientRect().top;
    const viewportH = mainEl.clientHeight;
    // 找视口中线穿过的页面
    const center = mainTop + viewportH / 2;
    const wraps = document.querySelectorAll('#page-container .page-wrap');
    for (const w of wraps) {
      const r = w.getBoundingClientRect();
      if (r.top <= center && r.bottom >= center) {
        const num = parseInt(w.dataset.pageNum);
        if (num !== currentPage) {
          currentPage = num;
          { const _pc = document.getElementById('page-cur'); if (_pc) _pc.textContent = num; }
          // 同步 URL + 拉 KG 节点
          const u = new URL(location.href);
          u.searchParams.set('page', num);
          history.replaceState(null, '', u);
          loadPageNodes(num);
          if (window._prefetchAround) window._prefetchAround(num);   // 翻到新页 → 后台预取前后页(read-ahead)
        }
        break;
      }
    }
    _unloadFarPages();   // 顺带卸载远处页,封顶内存
  }, 200);
}

// 选中浮出工具栏 —— 跨桌面（mouseup）+ 触屏（touchend）+ 通用（selectionchange）
const toolbar = document.getElementById('sel-toolbar');
let lastSelText = '';
     _updateSelPreview('');
let _selTimer = null;
