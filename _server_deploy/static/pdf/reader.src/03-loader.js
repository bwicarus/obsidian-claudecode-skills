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

async function loadPdf() {
  pdfLoadShow('📄 打开 PDF…', '大文件首次加载需几秒,正在流式下载结构');
  pdfLoadBar(null);
  try {
    window.dlog('开始 getDocument...');
    const task = pdfjsLib.getDocument({
      url: PDF_URL,
      cMapUrl: '/static/pdfjs/cmaps/',
      cMapPacked: true,
      standardFontDataUrl: '/static/pdfjs/standard_fonts/',
      // 大文件关键:只对实际翻到的页发 Range 请求,不在后台把整本下完。
      // 服务器 /pdf/file 支持 byte-range(conditional=True),几百 MB 的 PDF
      // 浏览器只持有当前页,iPad Safari 不再 OOM。
      // ⚠ PDF.js 官方:disableAutoFetch(禁后台预取)必须 **同时** disableStream:true 才生效!
      // 之前 disableStream:false → 仍后台流式下载整本 408MB(进度条跑的就是整本),
      // disableAutoFetch 形同虚设 → 打开慢。range 已确认 206 可用,关 stream 纯按需取。
      disableAutoFetch: true,
      disableStream: true,
      rangeChunkSize: 262144,   // 256KB 一块
    });
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
    window.dlog('✓ PDF 加载完成，共 ' + pdfDoc.numPages + ' 页');
    document.getElementById('page-total').textContent = '/ ' + pdfDoc.numPages;
    await loadBookCrop();   // 先拉去边配置(_crop/_cropOn)→ 下面 fit-width scale 才能按可见宽算
    // 自适应宽度：让 PDF 渲染宽度 ≈ #main 可用宽度（防超屏横向 scroll）
    const page1 = await pdfDoc.getPage(1);
    const v0 = page1.getViewport({scale: 1});
    const mainW = _mainContentWidth();
    const _dpr0 = window.devicePixelRatio || 1;
    _scaleMax = Math.min(3.5, 4000 / (v0.height * _dpr0));   // 防 canvas backing 高超 iOS ~4096 限制
    // 去边:可见区填满宽(÷可见宽占比);双页:每行 2 页并排(÷2,每页占半宽)
    scale = Math.max(0.5, Math.min(_scaleMax, mainW / (v0.width * _cropVisWFrac() * _pagesPerRow())));
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
    _loadPhraseFavs();      // 拉收藏词组（词组按钮收藏态 + 分词依据）
    _maybeRestoreLastPos();   // URL 未带 page 时跳到上次位置
    if (readMode !== 'single') {   // 连续 / 双页 都走 setupContinuousMode(内部按 spread 分行)
      await setupContinuousMode();
    } else {
      await renderPage(currentPage);
    }
    _restoreScrollAfterRender();   // 两种模式都恢复 scrollY（_pendingScrollY=0 时 no-op）
    _attachScrollSaver();   // 滚动时持续保存位置
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
  if (!(_crop.l || _crop.r || _crop.t || _crop.b)) {   // 没配过裁切 → 直接开设置面板
    _toast?.('先在 ⚙ 设置里设定左右上下隐藏百分比');
    window.openSettings?.();
    return;
  }
  _cropOn = !_cropOn;
  try { localStorage.setItem(_cropKey(), _cropOn ? '1' : '0'); } catch (_) {}
  _updateCropBtn();
  _refitToWidth(true);   // 重算 fit-width scale(按可见宽)+ 重渲染所有页(应用/撤销裁切)
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

