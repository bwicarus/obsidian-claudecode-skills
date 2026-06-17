// ───────────── 加载指示 ─────────────
(function(){ const st=document.createElement('style');
  st.textContent='@keyframes pdfspin{to{transform:rotate(360deg)}}'; document.head.appendChild(st); })();
let _pdfInitDone = false;   // 初次加载+首页渲染完后置 true;之后 onProgress(翻页取字节)不再弹加载层
function pdfLoadShow(text, hint) {
  if(_pdfInitDone) return;   // 已过初次加载,绝不再遮挡已渲染内容
  const box=document.getElementById('pdf-loading');
  if(!box) return;
  box.style.display='flex';
  if(text) document.getElementById('pdf-loading-text').textContent=text;
  if(hint!==undefined) document.getElementById('pdf-loading-hint').textContent=hint||'';
}
function pdfLoadBar(pct) {  // pct null = 不确定(只转圈)
  const bar=document.getElementById('pdf-loading-bar');
  if(!bar) return;
  if(pct==null){ bar.style.width='40%'; bar.style.opacity='0.4'; }
  else { bar.style.opacity='1'; bar.style.width=Math.max(2,Math.min(100,pct))+'%'; }
}
function pdfLoadHide() {
  _pdfInitDone = true;   // 锁死:此后任何 onProgress/pdfLoadShow 都不再显示加载层
  const box=document.getElementById('pdf-loading');
  if(box) box.style.display='none';
}
// 返回书架:**阅读器内浮层秒开**(23-bookshelf.js),不再整页跳 /pdf/(慢到要靠过场动画硬撑)。
// 浮层挂了/异常 → 退回老的整页跳转。module 作用域,挂 window 供 h1/链接的内联 onclick 调用。
window.goPdfList = function () {
  try { _openBookshelf(); return; } catch (_) {}
  location.href = '/pdf/';
};

// ── PDF 整本本地缓存（IndexedDB）──
// 首次打开走流式(线性化后首页秒出)+ 后台把整本下到设备 IndexedDB;第 2 次起直接读本地缓存
// 喂给 PDF.js({data}),零网络延迟秒开。key=FILE_REL,value={v,buf};v 跟 ?v=<mtime> 绑定→PDF
// 变了自动失效重下。>_PDF_CACHE_MAX 的书不整本缓存(防 iPad Safari 单页内存炸),仍走流式 range。
const _PDF_DB = 'pdf-blob-cache', _PDF_STORE = 'pdfs';
const _PDF_CACHE_MAX = 360 * 1024 * 1024;   // 360MB 上限(M4 iPad 内存足):≤此整本缓存→{data}喂 PDF.js→永不重拉;>此才走 range(会反复重拉,建议开压缩版)
const _PDF_VER = (String(PDF_URL).match(/[?&]v=(\d+)/) || [])[1] || '0';
// 请求"持久化存储":iOS Safari 普通标签页默认 best-effort(7 天没访问 ITP 清掉 + 配额满 LRU 驱逐),
// persist() 求系统别清(加到主屏=独立 PWA 时 iOS 自动授予 → 缓存真正长存,见下方给用户的提示)。
(async () => {
  try {
    if (navigator.storage?.persist) {
      const ok = await navigator.storage.persisted?.() || await navigator.storage.persist();
      const est = navigator.storage.estimate ? await navigator.storage.estimate() : null;
      window.dlog?.('存储持久化=' + ok + (est ? ` 用量 ${Math.round((est.usage||0)/1048576)}/${Math.round((est.quota||0)/1048576)}MB` : ''));
    }
  } catch (_) {}
})();
function _idb() {
  return new Promise((res, rej) => {
    let r = indexedDB.open(_PDF_DB, 1);
    r.onupgradeneeded = () => { r.result.createObjectStore(_PDF_STORE); };
    r.onsuccess = () => res(r.result);
    r.onerror = () => rej(r.error);
  });
}
async function _idbGet(k) {
  try { const db = await _idb(); return await new Promise(res => {
    const q = db.transaction(_PDF_STORE).objectStore(_PDF_STORE).get(k);
    q.onsuccess = () => res(q.result || null); q.onerror = () => res(null);
  }); } catch (_) { return null; }
}
async function _idbPut(k, v) {
  try { const db = await _idb(); await new Promise((res, rej) => {
    const tx = db.transaction(_PDF_STORE, 'readwrite');
    tx.objectStore(_PDF_STORE).put(v, k); tx.oncomplete = res; tx.onerror = () => rej(tx.error);
  }); return true; } catch (_) { return false; }
}
// 后台下整本存 IndexedDB(给"下次秒开"用)。**关键**:Tailscale 是单链路,这个整本下载会抢
// 首开/翻页的 range 带宽 → 大文件首开明显变慢(2026-06 实测:打开 138MB 书后台立刻下整本占满链路)。
// 两道防护:① 延后 20s 让首开/初翻彻底过去;② fetch priority:'low' 让浏览器把它排在交互 range
// 请求之后(只填空闲带宽,翻页时自动让路)。超上限/失败静默跳过,下次仍走流式 range。
function _cachePdfInBackground() {
  setTimeout(async () => {
    try {
      if (!PDF_SIZE || PDF_SIZE > _PDF_CACHE_MAX) { window.dlog?.('PDF ' + Math.round(PDF_SIZE/1048576) + 'MB 超缓存上限,不整本缓存(走流式 range;想秒开请开压缩版)'); return; }
      // 分块续传(抗弱网中断)+ silent(不动加载 UI)+ low(不抢当前阅读的交互 range 带宽)
      const buf = await _fetchFullWithProgress(PDF_URL, { silent: true, priority: 'low' });
      await _idbPut(FILE_REL, { v: _PDF_VER, buf });
      window.dlog?.('✓ 后台已缓存到本地 (' + Math.round(buf.byteLength/1048576) + 'MB)，下次秒开');
    } catch (e) { window.dlog?.('后台缓存失败(不影响阅读): ' + (e && e.message)); }
  }, 20000);   // 延后 20s:让首开 + 初次翻页彻底过去再后台下整本,绝不抢首开带宽
}

// 整本下载 + 真实进度。**分块(6MB) Range 顺序拉 + 每块失败退避重试**:iPad 在弱/断续网络下,
// 单次大 fetch 一被中断(iOS 切后台/抖动)就从 0 重来、永远下不完;改成断了只重试**当前块**、已下的不丢,
// 切后台回来也从上次位置续。需 total(文件大小);拿不到则回退单次流式。返回 ArrayBuffer。
async function _fetchFullWithProgress(url, opts) {
  const silent = !!(opts && opts.silent);          // 后台缓存:不动加载 UI
  const prio = (opts && opts.priority) || 'auto';  // 后台用 low,不抢交互 range 带宽
  const total = PDF_SIZE || 0;
  const _showPct = (got) => {
    if (silent || !total) return;
    const pct = Math.max(1, Math.min(99, Math.round(got / total * 100)));
    pdfLoadBar(pct);
    pdfLoadShow('📄 下载中… ' + pct + '%  (' + Math.round(got / 1048576) + '/' + Math.round(total / 1048576) + 'MB)');
  };
  if (!total) {   // 不知道大小 → 单次流式(无续传)
    const resp = await fetch(url, { priority: prio }); if (!resp.ok) throw new Error('HTTP ' + resp.status);
    return await resp.arrayBuffer();
  }
  const CHUNK = 6 * 1024 * 1024;
  const out = new Uint8Array(total);
  let pos = 0;
  while (pos < total) {
    const end = Math.min(pos + CHUNK, total) - 1;
    let ok = false, lastErr = null;
    for (let tries = 0; tries < 6 && !ok; tries++) {
      if (tries) await new Promise(r => setTimeout(r, 700 * tries));   // 退避重试同一块
      try {
        const resp = await fetch(url, { headers: { Range: 'bytes=' + pos + '-' + end }, priority: prio });
        if (resp.status !== 206 && resp.status !== 200) throw new Error('HTTP ' + resp.status);
        const part = new Uint8Array(await resp.arrayBuffer());
        if (!part.length) throw new Error('empty chunk');
        out.set(part.subarray(0, Math.min(part.length, total - pos)), pos);
        pos += part.length; ok = true;
      } catch (e) { lastErr = e; }
    }
    if (!ok) throw lastErr || new Error('chunk failed @' + pos);
    _showPct(pos);
  }
  return out.buffer;
}
async function loadPdf() {
  pdfLoadShow('📄 打开 PDF…', '大文件首次加载需几秒,正在流式下载结构');
  pdfLoadBar(null);
  // 加载 13s 还没出首页 + 有压缩版 + 当前没在用压缩版 → 在加载层显示「切换压缩版」按钮(慢网救急)
  setTimeout(() => {
    if (!_pdfInitDone && PDF_COMP_AVAIL && !PDF_COMPRESSED) {
      const b = document.getElementById('pdf-loading-switch'); if (b) b.style.display = '';
    }
  }, 13000);
  try {
    if (_imgMode) {
      // 图片模式(成熟方案):不下载 PDF、不解析整本,只取书元数据建 pdfDoc shim(页数+尺寸),
      // 每页按需取服务端渲染好的图(/api/page-image)。其余代码靠 shim 的 getPage().getViewport() 照常工作。
      window.dlog('图片模式:取 book-meta(不下载 PDF)');
      const meta = await (await fetch('/pdf/api/book-meta?file=' + encodeURIComponent(FILE_REL))).json();
      if (!meta.ok) throw new Error('book-meta 失败:' + (meta.error || ''));
      window.__imgMeta = meta;
      pdfDoc = {
        numPages: meta.page_count, _img: true,
        getPage: (n) => Promise.resolve({ getViewport: (o) => { const s = (o && o.scale) || 1; return { width: meta.page_w * s, height: meta.page_h * s, scale: s }; } }),
      };
    } else {
    window.dlog('开始 getDocument...');
    const _common = {
      cMapUrl: '/static/pdfjs/cmaps/',
      cMapPacked: true,
      standardFontDataUrl: '/static/pdfjs/standard_fonts/',
    };
    // 大文件关键:默认只对实际翻到的页发 Range 请求,不在后台把整本下完(iPad Safari 不 OOM)。
    // ⚠ PDF.js 官方:disableAutoFetch 必须 **同时** disableStream:true 才生效。
    const _rangeOpts = { url: PDF_URL, disableAutoFetch: true, disableStream: true, rangeChunkSize: 1048576 };  // 1MB 块:减少 Tailscale 高延迟下的往返次数
    // 本地缓存优先:命中(且版本一致)→ 从 IndexedDB 直接喂字节,零网络、秒开;
    // 未命中 → 走流式(线性化后首页快)+ 后台把整本下到本地,下次秒开。
    let _src = _rangeOpts, _haveBuf = false;
    try {
      const c = await _idbGet(FILE_REL);
      // 校验缓存字节数 == 期望大小:防之前(transfer 竞态等)存进坏/被 detach 的 buffer → 命中坏缓存就**永远加载不出**。
      if (c && c.v === _PDF_VER && c.buf && (!PDF_SIZE || c.buf.byteLength === PDF_SIZE)) {
        _src = { data: c.buf }; _haveBuf = true; window.dlog('✓ 命中本地缓存,秒开');
      } else if (c && c.buf) { window.dlog('缓存失效(版本/大小不符:' + (c.buf.byteLength||0) + ' vs ' + PDF_SIZE + '),丢弃重取'); }
    } catch (_) {}
    // 未缓存且 ≤360MB:**下载一次(分块续传+进度)→ 缓存 → {data} 喂 PDF.js**,之后从内存读、永不重拉。
    // 为何不用 range「边读边拉」:PDF.js 对这些扫描书的 disableAutoFetch 挡不住,range 模式每次打开都
    // 反复重拉整本(实测累计 >10GB),慢网下灾难。整本下一次后缓存,后续秒开、零网络。>360MB 才 range(装不下内存→建议压缩版)。
    if (!_haveBuf && PDF_SIZE > 0 && PDF_SIZE < _PDF_CACHE_MAX) {
      try {
        pdfLoadShow('📄 下载中…', PDF_SIZE > 30 * 1048576 ? '首次整本下载,下完缓存 → 之后秒开、不再重拉' : '');
        const buf = await _fetchFullWithProgress(PDF_URL);
        try { await _idbPut(FILE_REL, { v: _PDF_VER, buf }); } catch (_) {}   // 缓存(IndexedDB clone 不 detach buf),再喂 PDF.js
        _src = { data: buf }; _haveBuf = true;
      } catch (e) { window.dlog('整本取失败,回落 range: ' + (e && e.message)); _src = _rangeOpts; _haveBuf = false; }
    }
    // >360MB / 整本取失败 → range(会反复重拉)+ 后台缓存(>360 跳过)
    if (!_haveBuf) _cachePdfInBackground();
    const task = pdfjsLib.getDocument({ ..._common, ..._src });
    task.onProgress = (p) => {
      if (p.total) {
        const pct = Math.round(p.loaded / p.total * 100);
        window.dlog('加载 ' + pct + '%');
        pdfLoadBar(pct);
        pdfLoadShow('📄 加载中… ' + pct + '%');
      }
    };
    pdfLoadShow('📄 渲染首页…', '');
    pdfLoadBar(100);
    pdfDoc = await task.promise;
    }
    window.dlog('✓ PDF 加载完成，共 ' + pdfDoc.numPages + ' 页');
    document.getElementById('page-total').textContent = '/ ' + pdfDoc.numPages;
    await loadBookCrop();   // 先拉去边配置(_crop/_cropOn)→ 下面 fit-width scale 才能按可见宽算
    // 旋转自动切换排版：开了的话,按当前横/竖屏套用该方向上次存的 {排版+去边开关+双页错位}
    if (typeof _autoOrientOn === 'function' && _autoOrientOn()) {
      const _lay = _loadOrientLayout(_orient());
      if (_lay) _applyOrientLayoutVars(_lay);
    }
    // 自适应宽度：让 PDF 渲染宽度 ≈ #main 可用宽度（防超屏横向 scroll）
    const page1 = await pdfDoc.getPage(1);
    const v0 = page1.getViewport({scale: 1});
    _fitPageW = v0.width; _fitPageH = v0.height;   // 缓存供 _saveOrientLayout 判是否宽度适应
    const mainW = _mainContentWidth();
    const _dpr0 = window.devicePixelRatio || 1;
    _scaleMax = 4.0;   // 放大上限(绝对倍率)。backing≤4096 由 _renderPageInto 动态 outputScale 兜住,
                       // 不再用页高卡死缩放(旧 min(3.5,4000/(页高×dpr)) 对高页只有 ~0.7)
    // 去边:可见区填满宽;双页:两页并排(扣 gap)+ 受高度约束(整页可见)。统一走 _computeFitScale
    scale = _computeFitScale(v0.width, v0.height);
    _lastFitWidth = mainW;
    window.dlog('autoscale: ' + scale.toFixed(2) + ' (mainW=' + mainW + ', pageW@1=' + v0.width.toFixed(0) + ')');
    _updateModeButtons();   // 模式按钮文字 + 双页按钮高亮态(readMode 可能是 spread)
    { const rb = document.getElementById('ruby-toggle'); if (rb) rb.classList.toggle('active', _rubyEnabled()); }   // 振假名按钮恢复上次开关态
    // 高亮：先拉一次（后续渲染完页面 loadCharsAndBindLayer 自动贴到 page-wrap）
    loadAllHighlights();
    if (window._inkLoadAll) window._inkLoadAll();   // 手写墨迹：拉一次，渲染完页面再贴
    renderHlPicker();
    loadGrammarTracked();   // 拉该 PDF 的语法跟踪节点（影响工具栏按钮显示）
    loadBookLangs();        // 拉本书语言声明(影响点词查词典路由)
    loadBookFig();          // 拉本书「插图描述/徽标」开关(默认关 → 不画徽标不烧 AI)
    _loadPhraseFavs();      // 拉收藏词组（词组按钮收藏态 + 分词依据）
    _maybeRestoreLastPos();   // URL 未带 page 时跳到上次位置
    if (readMode !== 'single') {   // 连续 / 双页 都走 setupContinuousMode(内部按 spread 分行)
      await setupContinuousMode();
    } else {
      await renderPage(currentPage);
    }
    if (typeof _applyPendingOrientScale === 'function') await _applyPendingOrientScale();   // 旋转记忆:套用该方向上次缩放
    if (typeof _rememberOrientLayout === 'function') _rememberOrientLayout();   // 存当前方向基线(开了自动切换才存),保证从未改过的方向也有存档可切回
    _restoreScrollAfterRender();   // 两种模式都恢复 scrollY（_pendingScrollY=0 时 no-op）
    _attachScrollSaver();   // 滚动时持续保存位置
    requestAnimationFrame(() => window._updateMainOverflowX && window._updateMainOverflowX());   // 初始按内容宽锁横向滚动
    pdfLoadHide();   // 首页已渲染,撤加载层
  } catch (e) {
    window.dlog('❌ getDocument FAILED: ' + e.message, '#ff6b6b');
    pdfLoadHide();
    document.getElementById('page-container').innerHTML =
      '<div style="color:#c00;padding:20px">加载 PDF 失败：' + e.message + '</div>';
  }
}

// ── 去边阅读模式 ──
async function loadBookCrop() {
  try {
    const d = await (await fetch('/pdf/api/book-crop?file=' + encodeURIComponent(FILE_REL))).json();
    if (d && d.ok && d.crop) {
      _crop = {l: +d.crop.l || 0, r: +d.crop.r || 0, t: +d.crop.t || 0, b: +d.crop.b || 0};
    }
  } catch (_) {}
  _cropOn = localStorage.getItem(_cropKey()) === '1';
  _updateCropBtn();
}
function _updateCropBtn() {
  const b = document.getElementById('crop-toggle');
  if (b) b.classList.toggle('active', _cropActive());
}
window.toggleCrop = () => {
  _cropOn = !_cropOn;   // 直接切换开/关(去边百分比只在 ⚙ 设置里配,不再点按钮就跳设置)
  try { localStorage.setItem(_cropKey(), _cropOn ? '1' : '0'); } catch (_) {}
  _updateCropBtn();
  if (_cropOn && !(_crop.l || _crop.r || _crop.t || _crop.b)) _toast?.('去边已开,但还没设隐藏百分比 → 在 ⚙ 设置里配');
  _refitToWidth(true);   // 重算 fit-width scale(按可见宽)+ 重渲染所有页(应用/撤销裁切)
  window._rememberOrientLayout?.();   // 记进当前方向(若开了旋转自动切换)
};
// 设置面板保存:写后端 + 本地刷新。autoOn:首次设置非零值时自动打开去边
async function saveCropSettings(crop, autoOn) {
  _crop = {l: +crop.l || 0, r: +crop.r || 0, t: +crop.t || 0, b: +crop.b || 0};
  try {
    await fetch('/pdf/api/book-crop', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({file: FILE_REL, crop: _crop}),
    });
  } catch (_) {}
  if (autoOn && _cropActive()) { _cropOn = true; try { localStorage.setItem(_cropKey(), '1'); } catch (_) {} }
  _updateCropBtn();
  _refitToWidth(true);
}

