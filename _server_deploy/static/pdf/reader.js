// PDF 阅读器主模块(从 pdf_reader.html 内联 <script type="module"> 抽出,2026-06)。
// 配置经 window.__PDF_CFG(模板内联 script 注入:pdf_url/file_rel/page)。架构/全局未变,纯物理拆分。
window.dlog('module script 开始执行（ES module 加载 OK）');
// cache buster：避开浏览器/代理的 mime 缓存（之前 nginx 错把 .mjs 当 octet-stream，已修但缓存还在）
const PDFJS_V = '20260526a';
let pdfjsLib;
try {
  pdfjsLib = await import('/static/pdfjs/pdf.mjs?v=' + PDFJS_V);
  window.dlog('✓ pdf.mjs imported, version=' + (pdfjsLib.version || '?'));
} catch (e) {
  window.dlog('❌ import pdf.mjs FAILED: ' + e.message, '#ff6b6b');
  throw e;
}
try {
  pdfjsLib.GlobalWorkerOptions.workerSrc = '/static/pdfjs/pdf.worker.mjs?v=' + PDFJS_V;
  window.dlog('✓ workerSrc set');
} catch (e) {
  window.dlog('❌ workerSrc failed: ' + e.message, '#ff6b6b');
}

const PDF_URL = window.__PDF_CFG.pdf_url;
const FILE_REL = window.__PDF_CFG.file_rel;
// page-chars 缓存版本:取 PDF mtime(PDF_URL 里的 ?v=)做 cache key,既在 PDF 变更时刷新,
// 又跟旧的无版本 URL 区分开 → iOS Safari 已缓存的旧分词数据立即失效(配合后端 no-store)
const CHARS_VER = (String(PDF_URL).match(/[?&]v=(\d+)/) || [])[1] || '2';
let BOOK_LANGS = [];   // 本书声明的语言,如 ['en','ja'];影响点词查哪本词典
async function loadBookLangs() {
  try {
    const r = await fetch('/pdf/api/book-langs?file=' + encodeURIComponent(FILE_REL || ''));
    const d = await r.json();
    if (d.ok) BOOK_LANGS = d.langs || [];
  } catch (e) {}
}
window.openLangPicker = function() {
  document.querySelectorAll('#lang-checks input').forEach(c => { c.checked = BOOK_LANGS.includes(c.value); });
  document.getElementById('lang-mask').style.display = 'flex';
};
window.saveLangPicker = async function() {
  const langs = Array.from(document.querySelectorAll('#lang-checks input:checked')).map(c => c.value);
  try {
    const r = await fetch('/pdf/api/book-langs', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ file: FILE_REL, langs }),
    });
    const d = await r.json();
    if (d.ok) BOOK_LANGS = d.langs || langs;
  } catch (e) {}
  document.getElementById('lang-mask').style.display = 'none';
};
window.dlog('PDF_URL = ' + PDF_URL);
let pdfDoc = null;
let currentPage = window.__PDF_CFG.page;
let scale = 1.4;
let _scaleMax = 3.0;   // scale 上限：loadPdf 按页高×dpr 动态算（防 canvas backing 高超 iOS ~4096）
let readMode = (() => {
  const m = new URLSearchParams(location.search).get('mode');   // 技能树书本图标可带 ?mode=continuous
  return (m === 'continuous' || m === 'single') ? m : (localStorage.getItem('pdf-read-mode') || 'single');
})();   // 'single' | 'continuous'
let _contIO = null;   // IntersectionObserver for 连续模式
let _pendingScrollY = 0;   // 上次位置恢复用
let _scrollSaveTimer = null;

// ─── per-PDF 位置记忆（localStorage pdf-last-positions） ───
const LAST_POS_KEY = 'pdf-last-positions';
function _loadLastPositions() {
  try { return JSON.parse(localStorage.getItem(LAST_POS_KEY) || '{}'); }
  catch { return {}; }
}
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
  if (!pos) return;
  if (pos.mode && pos.mode !== readMode) {
    localStorage.setItem('pdf-read-mode', pos.mode);
    readMode = pos.mode;
  }
  if (pos.page && pos.page > 0) currentPage = pos.page;
  _pendingScrollY = pos.scrollY || 0;
  window.dlog?.('恢复上次位置 p.' + currentPage + (pos.scrollY ? ' scrollY=' + pos.scrollY : ''));
  _toast?.('⏯ 已恢复到上次位置 p.' + currentPage);
}
function _restoreScrollAfterRender() {
  if (!_pendingScrollY) return;
  const main = document.getElementById('main');
  if (!main) return;
  const target = _pendingScrollY;
  // 多次 apply 直到稳定（连续模式占位高度逐步算准 / 真渲染替换占位时高度可能变）
  const apply = () => { main.scrollTop = target; };
  setTimeout(apply, 100);
  setTimeout(apply, 400);
  setTimeout(apply, 1000);
  setTimeout(() => { _pendingScrollY = 0; }, 1100);   // 最后一次后清空（避免重复 apply 干扰用户主动滚动）
}
function _attachScrollSaver() {
  const main = document.getElementById('main');
  if (!main || main.__savedAttached) return;
  main.__savedAttached = true;
  main.addEventListener('scroll', () => {
    if (_scrollSaveTimer) clearTimeout(_scrollSaveTimer);
    _scrollSaveTimer = setTimeout(() => {
      _saveLastPosition({page: currentPage, scrollY: main.scrollTop, mode: readMode, scale});
    }, 600);
  }, {passive: true});
}


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
    // 自适应宽度：让 PDF 渲染宽度 ≈ #main 可用宽度（防超屏横向 scroll）
    const page1 = await pdfDoc.getPage(1);
    const v0 = page1.getViewport({scale: 1});
    const mainW = _mainContentWidth();
    const _dpr0 = window.devicePixelRatio || 1;
    _scaleMax = Math.min(3.5, 4000 / (v0.height * _dpr0));   // 防 canvas backing 高超 iOS ~4096 限制
    scale = Math.max(0.5, Math.min(_scaleMax, mainW / v0.width));
    _lastFitWidth = mainW;
    window.dlog('autoscale: ' + scale.toFixed(2) + ' (mainW=' + mainW + ', pageW@1=' + v0.width.toFixed(0) + ')');
    document.getElementById('mode-toggle').textContent = readMode === 'continuous' ? '📚 连续' : '📄 单页';
    { const rb = document.getElementById('ruby-toggle'); if (rb) rb.classList.toggle('active', _rubyEnabled()); }   // 振假名按钮恢复上次开关态
    // 高亮：先拉一次（后续渲染完页面 loadCharsAndBindLayer 自动贴到 page-wrap）
    loadAllHighlights();
    if (window._inkLoadAll) window._inkLoadAll();   // 手写墨迹：拉一次，渲染完页面再贴
    renderHlPicker();
    loadGrammarTracked();   // 拉该 PDF 的语法跟踪节点（影响工具栏按钮显示）
    loadBookLangs();        // 拉本书语言声明(影响点词查词典路由)
    _loadPhraseFavs();      // 拉收藏词组（词组按钮收藏态 + 分词依据）
    _maybeRestoreLastPos();   // URL 未带 page 时跳到上次位置
    if (readMode === 'continuous') {
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

async function renderPage(num) {
  if (!pdfDoc) return;
  num = Math.max(1, Math.min(pdfDoc.numPages, parseInt(num) || 1));
  currentPage = num;
  { const _pc = document.getElementById('page-cur'); if (_pc) _pc.textContent = num; }
  if (readMode === 'continuous') {
    // 连续模式：滚到对应页占位 + 立即渲染目标页(别等 IO,跳页/翻页不卡)
    const ph = document.querySelector(`[data-page-num="${num}"]`);
    if (ph) {
      ph.scrollIntoView({block: 'start', behavior: 'auto'});
      if (ph.dataset.loaded === '0') _renderPageInto(num, ph).catch(() => {});
    }
    return;
  }
  await _renderPageInto(num, document.getElementById('page-container'), true);
}

async function _renderPageInto(num, wrap) {
  if (!pdfDoc) return;
  if (wrap.dataset.loaded === '1') return;
  const page = await pdfDoc.getPage(num);
  const viewport = page.getViewport({scale});
  // 清空 wrap（placeholder 内容或上次的渲染），不动 wrap 本身的 className/dataset
  wrap.innerHTML = '';
  wrap.__charLayer = null; wrap.__charBoxes = null;   // 清残留引用，避免重渲染后指向已删除的旧层
  wrap.__inkStrokes = null;
  wrap.style.width = ''; wrap.style.height = '';
  wrap.style.background = ''; wrap.style.color = '';
  wrap.style.display = ''; wrap.style.alignItems = ''; wrap.style.justifyContent = '';
  wrap.dataset.pageNum = num;
  const canvas = document.createElement('canvas');
  // PDF.js v4 推荐：canvas backing store = viewport * devicePixelRatio（retina 清晰）
  // 但 CSS 显示尺寸 = viewport（跟 textLayer 一致 → spans 跟文字对齐）
  const outputScale = window.devicePixelRatio || 1;
  const cw = Math.floor(viewport.width);
  const ch = Math.floor(viewport.height);
  canvas.width  = Math.floor(viewport.width  * outputScale);
  canvas.height = Math.floor(viewport.height * outputScale);
  canvas.style.width  = cw + 'px';
  canvas.style.height = ch + 'px';
  wrap.appendChild(canvas);
  const textLayerDiv = document.createElement('div');
  textLayerDiv.className = 'textLayer';
  textLayerDiv.style.width  = cw + 'px';
  textLayerDiv.style.height = ch + 'px';
  textLayerDiv.style.setProperty('--scale-factor', viewport.scale);
  // PDF.js v4 还可能用 --total-scale-factor 算 spans（含 dpr），保险一起设
  textLayerDiv.style.setProperty('--total-scale-factor', viewport.scale);
  wrap.appendChild(textLayerDiv);
  // 手绘选中高亮 overlay（同 CSS 尺寸，对齐 canvas + textLayer）
  const selOverlay = document.createElement('div');
  selOverlay.className = 'sel-overlay';
  selOverlay.style.width  = cw + 'px';
  selOverlay.style.height = ch + 'px';
  wrap.appendChild(selOverlay);

  // 手写墨迹 canvas（z7，纯显示；绘制靠 page-wrap capture 拦截 pen / 桌面手写模式）
  const inkCanvas = document.createElement('canvas');
  inkCanvas.className = 'ink-layer';
  inkCanvas.style.width = cw + 'px';
  inkCanvas.style.height = ch + 'px';
  const _inkDpr = window.devicePixelRatio || 1;
  inkCanvas.width  = Math.floor(cw * _inkDpr);
  inkCanvas.height = Math.floor(ch * _inkDpr);
  wrap.appendChild(inkCanvas);
  wrap.__inkCanvas = inkCanvas;
  wrap.addEventListener('pointerdown', (e) => { if (window._inkPointerDown) window._inkPointerDown(e); }, true);
  // iOS：Apple Pencil 触摸(touchType=stylus)阻止其默认滚动，手指(direct)放行 → 笔不滚页、手指照常滚
  const _inkBlockStylusScroll = (e) => {
    for (const t of e.touches) { if (t.touchType === 'stylus') { e.preventDefault(); break; } }
  };
  wrap.addEventListener('touchstart', _inkBlockStylusScroll, { passive: false });
  wrap.addEventListener('touchmove', _inkBlockStylusScroll, { passive: false });

  // 渲染 canvas（用 transform 把 viewport 坐标 scale 到 backing store）
  const ctx = canvas.getContext('2d');
  const renderTransform = outputScale !== 1 ? [outputScale, 0, 0, outputScale, 0, 0] : null;
  await page.render({canvasContext: ctx, viewport, transform: renderTransform}).promise;

  // 渲染 text layer（让用户选中文本）
  const textContent = await page.getTextContent();
  const tl = new pdfjsLib.TextLayer({
    textContentSource: textContent,
    container: textLayerDiv,
    viewport,
  });
  await tl.render();

  // 把 textContent.items 的 PDF 坐标 → viewport 坐标缓存到 page-wrap
  // **关键**：用 PDF.js 暴露的 tl.textDivs（1:1 对应 textContent.items 中文本项）
  // 避免 querySelectorAll('span') 包含 markedContent 嵌套 spans 导致 index 错位
  const textItems = textContent.items.filter(it => it.str !== undefined);
  const itemBoxes = textItems.map(item => {
    if (!item.transform) return null;
    const [a, b, c, d, e, f] = item.transform;
    const [vx, vy] = viewport.convertToViewportPoint(e, f);
    const fontH = Math.abs(d) * viewport.scale;
    const w = (item.width || 0) * viewport.scale;
    return {
      x: vx,
      y: vy - fontH,
      w: w,
      h: fontH || (item.height || 0) * viewport.scale,
      str: item.str || '',
    };
  });
  wrap.__itemBoxes = itemBoxes;
  wrap.__textLayerDiv = textLayerDiv;
  // 用 PDF.js 的 textDivs 数组（按 items 顺序，1:1 对应 itemBoxes）
  const textDivs = (tl.textDivs && tl.textDivs.length) ? tl.textDivs : [];
  wrap.__textDivs = textDivs;
  wrap.__getSpanIndex = (span) => {
    let cur = span;
    while (cur && cur !== textLayerDiv) {
      const idx = textDivs.indexOf(cur);
      if (idx >= 0) return idx;
      cur = cur.parentElement;
    }
    return -1;
  };
  wrap.__getTextNode = (textDivIdx) => {
    const div = textDivs[textDivIdx];
    if (!div) return null;
    if (div.firstChild?.nodeType === 3) return div.firstChild;
    const walker = document.createTreeWalker(div, NodeFilter.SHOW_TEXT, null);
    return walker.nextNode();
  };
  window.dlog?.('page rendered: items=' + textItems.length + ' textDivs=' + textDivs.length);
  // 诊断：对比 itemBoxes[0] 跟 textDivs[0].getBoundingClientRect()
  if (textDivs[0] && itemBoxes[0]) {
    const r = textDivs[0].getBoundingClientRect();
    const pwR = wrap.getBoundingClientRect();
    const physX = r.left - pwR.left;
    const physY = r.top  - pwR.top;
    window.dlog?.('item[0] str='+JSON.stringify((itemBoxes[0].str||'').slice(0,15)));
    window.dlog?.('  itemBox:  x='+itemBoxes[0].x.toFixed(1)+' y='+itemBoxes[0].y.toFixed(1)+' w='+itemBoxes[0].w.toFixed(1)+' h='+itemBoxes[0].h.toFixed(1));
    window.dlog?.('  textDiv:  x='+physX.toFixed(1)+' y='+physY.toFixed(1)+' w='+r.width.toFixed(1)+' h='+r.height.toFixed(1));
    const dx = itemBoxes[0].x - physX, dy = itemBoxes[0].y - physY;
    window.dlog?.('  delta: dx='+dx.toFixed(1)+' dy='+dy.toFixed(1));
  }

  // 关掉 textLayer 的点击交互（保留它仅用作 PDF.js 自身字体度量；选中事件由 char-layer 接管）
  textLayerDiv.style.pointerEvents = 'none';

  // 加载 PyMuPDF 提取的 char-level 精确 bbox + 创建 char-layer 接管选中
  loadCharsAndBindLayer(num, wrap, viewport).catch(e => window.dlog?.('chars load fail: ' + e.message, '#ff6b6b'));
  // 加载该页已存墨迹并重绘
  wrap.__inkStrokes = (window._ink && window._ink.byPage[num]) ? JSON.parse(JSON.stringify(window._ink.byPage[num])) : [];
  if (window._inkRedraw) window._inkRedraw(wrap);
  wrap.dataset.loaded = '1';

  // 同步 URL + 拉 KG 节点：只在单页模式做
  if (readMode === 'single') {
    const u = new URL(location.href);
    u.searchParams.set('page', num);
    history.replaceState(null, '', u);
    loadPageNodes(num);
  }
}

async function loadPageNodes(num) {
  try {
    const r = await fetch(`/pdf/api/page-nodes?file=${encodeURIComponent(FILE_REL)}&page=${num}`);
    const d = await r.json();
    const list = d.nodes || [];
    const c = document.getElementById('kg-nodes');
    if (!list.length) {
      c.innerHTML = '<div style="color:#5a6680;font-size:12px">该页无 KG 节点（可能这本书没扫过/或本页不是知识点页）</div>';
      return;
    }
    c.innerHTML = list.map(n => {
      // 只有 grammar KG 的节点能跟踪（跟技能树 toggle-tracked 规则一致）
      const trackBtn = (n.kind === 'grammar')
        ? `<button class="kg-track-btn ${n.tracked ? 'on' : ''}" title="加入/取消语法跟踪"
             onclick="event.stopPropagation(); toggleNodeTrack('${n.book}','${n.id}', this)">${n.tracked ? '★ 跟踪中' : '☆ 跟踪'}</button>`
        : '';
      const lbl = n.numeric_label ? `[${n.numeric_label}] ` : '';
      return `<div class="kg-node ${n.state}">
        <div class="kg-node-main" onclick="window.open('/skilltree/${encodeURIComponent(n.book)}/#${encodeURIComponent('f.'+n.id)}','_blank')">
          <div class="lbl">${lbl}${n.name}</div>
          <div class="sum">${n.summary}</div>
        </div>
        ${trackBtn}
      </div>`;
    }).join('');
  } catch (e) {
    document.getElementById('kg-nodes').innerHTML = '<div style="color:#c00;font-size:12px">加载失败</div>';
  }
  window._refreshVocabIfPage?.();   // 翻页时若单词本在「本页」scope，同步刷新
}

// 知识点卡上的「跟踪」按钮：直接 toggle 该 grammar 节点的 tracked（不用回技能树页面）
window.toggleNodeTrack = async (book, nodeId, btn) => {
  btn.disabled = true;
  try {
    const r = await fetch(`/skilltree/${encodeURIComponent(book)}/api/toggle-tracked`, {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({node_id: nodeId}),
    });
    const d = await r.json();
    if (d.ok) {
      btn.classList.toggle('on', d.tracked);
      btn.textContent = d.tracked ? '★ 跟踪中' : '☆ 跟踪';
      loadGrammarTracked();   // 刷新工具栏「📊 语法分析」按钮的可用状态
    } else {
      _toast?.(d.error || '操作失败');
    }
  } catch (e) {
    _toast?.('网络错误');
  } finally {
    btn.disabled = false;
  }
};

window.changePage = async (delta) => {
  await renderPage(currentPage + delta);
  _saveLastPosition({page: currentPage, mode: readMode, scale});
};
window.goToPage = async (n) => {
  await renderPage(n);
  _saveLastPosition({page: currentPage, mode: readMode, scale});
};
// 页码 scrubber:左右拖动快速跳页(全宽≈整本),实时预览;点击(没拖)→ 输入页码
function _setupPageScrub() {
  const el = document.getElementById('page-scrub');
  const pop = document.getElementById('page-scrub-pop');
  if (!el || !pop) return;
  let st = null;
  const numEl = () => pop.querySelector('.psp-num');
  const fillEl = () => pop.querySelector('.psp-fill');
  el.addEventListener('pointerdown', (e) => {
    if (!pdfDoc) return;
    st = {x: e.clientX, p: currentPage, total: pdfDoc.numPages, moved: false, tgt: currentPage};
    try { el.setPointerCapture(e.pointerId); } catch (_) {}
    e.preventDefault();
  });
  el.addEventListener('pointermove', (e) => {
    if (!st) return;
    const dx = e.clientX - st.x;
    if (Math.abs(dx) > 4) st.moved = true;
    if (!st.moved) return;
    const w = Math.max(220, (document.getElementById('main')?.clientWidth || window.innerWidth) * 0.7);
    let tgt = Math.round(st.p + dx / w * st.total);
    tgt = Math.max(1, Math.min(st.total, tgt));
    st.tgt = tgt;
    pop.style.display = 'flex';
    numEl().textContent = tgt + ' / ' + st.total;
    fillEl().style.width = (tgt / st.total * 100) + '%';
    const pc = document.getElementById('page-cur'); if (pc) pc.textContent = tgt;
    e.preventDefault();
  });
  const end = () => {
    if (!st) return;
    const s = st; st = null;
    pop.style.display = 'none';
    if (s.moved) { goToPage(s.tgt); }
    else {
      const n = prompt('跳到第几页 (1-' + s.total + ')', currentPage);
      const v = parseInt(n);
      if (v) goToPage(Math.max(1, Math.min(s.total, v)));
      else { const pc = document.getElementById('page-cur'); if (pc) pc.textContent = currentPage; }
    }
  };
  el.addEventListener('pointerup', end);
  el.addEventListener('pointercancel', () => { st = null; pop.style.display = 'none'; });
}
// ⚠ 本 module 顶部有 top-level await(import pdf.mjs),它**不延迟 DOMContentLoaded** →
// await 之后(本行)再绑 DOMContentLoaded 已晚、永不触发。故:DOM 已就绪就直接调。
if (document.readyState !== 'loading') _setupPageScrub();
else window.addEventListener('DOMContentLoaded', _setupPageScrub);
window.zoomChange = async (delta) => {
  scale = Math.max(0.6, Math.min(_scaleMax, scale + delta));
  if (readMode === 'continuous') await setupContinuousMode();
  else renderPage(currentPage);
};
// 宽适应：按 #main 可用宽度重算 scale（取消 ＋/－ 或双指缩放，回到一页刚好铺满宽度）
window.fitWidth = () => { _refitToWidth(true); };
// 「📋 知识点」按钮：打开统一面板并切到知识点 tab（再点同 tab 则关闭）
window.toggleSidebar = () => {
  const p = document.getElementById('grammar-panel');
  const onKg = document.querySelector('#side-tabs .side-tab[data-pane="kg"]')?.classList.contains('active');
  if (p.classList.contains('open') && onKg) { closeGrammarPanel(); return; }
  openGrammarPanel();
  switchSideTab('kg');
};
// 切换 tab：语法分析 / 知识点
window.switchSideTab = (pane) => {
  document.querySelectorAll('#side-tabs .side-tab').forEach(t => t.classList.toggle('active', t.dataset.pane === pane));
  document.querySelectorAll('#grammar-panel .side-pane').forEach(p => p.classList.toggle('active', p.dataset.pane === pane));
  // 🗑 清空按钮只在语法 tab 显示
  const clr = document.getElementById('side-clear');
  if (clr) clr.style.display = (pane === 'grammar') ? '' : 'none';
  if (pane === 'vocab' && !_vocabLoaded) loadVocabList();   // 首次进单词本自动载
  if (pane === 'grammar' && !_grammarHistLoaded) loadGrammarHistory();   // 首次进语法载历史
  if (pane === 'kg') loadPageNodes(currentPage);   // 进知识点 tab → 主动拉当前页（连续模式下不靠滚动触发，免得抽屉空要滑动才出）
  if (pane === 'hist') renderQueryHistory();   // 进查询结果历史 tab
};
// 语法分析历史：按书本持久，新旧倒序（最新在上）
let _grammarHistLoaded = false;
window.loadGrammarHistory = async () => {
  _grammarHistLoaded = true;
  try {
    const r = await fetch('/pdf/api/grammar-history?file=' + encodeURIComponent(FILE_REL || ''));
    const d = await r.json();
    const items = d.items || [];   // 后端已按 ts 倒序（新在前）
    for (const it of items) _addHistoryBlock(it);   // append → 新的在最上
  } catch (e) { window.dlog?.('grammar history load fail: ' + e.message); }
};
function _addHistoryBlock(item) {
  const body = document.getElementById('grammar-panel-body');
  if (!body) return;
  const sentence = item.sentence || '', text = item.text || '';
  const block = document.createElement('div');
  block.className = 'grammar-block';   // 历史卡默认折叠
  const _hid = 'gbh_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6);
  block.id = _hid;
  block.dataset.src = sentence; block.dataset.text = text;
  const summary = sentence.slice(0, 60) + (sentence.length > 60 ? '…' : '');
  block.innerHTML =
    `<div class="gb-header">
       <span class="gb-title" title="${_esc(sentence)}">${_esc(summary)}</span>
       <span class="gb-del" title="删除这条（同句下次重新分析）">🗑</span>
       <span class="gb-caret">▶</span>
     </div>
     <div class="gb-trans"></div>
     <div class="gb-content"></div>
     <div class="gb-fu-answers"></div>
     <div class="gb-followup">
       <input class="gb-fu-input" placeholder="继续追问这句的语法…" onkeydown="if(event.key==='Enter'){event.preventDefault();_grammarFollowup('${_hid}');}">
       <button onclick="_grammarFollowup('${_hid}')">追问</button>
       <button class="gb-anki-btn" onclick="_grammarAnki('${_hid}')" title="整句+译文+分析做成 Anki 卡">🎴</button>
     </div>`;
  block.querySelector('.gb-header').addEventListener('click', () => block.classList.toggle('open'));
  block.querySelector('.gb-del').addEventListener('click', (e) => {
    e.stopPropagation();
    fetch('/pdf/api/grammar-forget', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({sentence: block.dataset.src || '', text: block.dataset.text || '', file: FILE_REL, enabled_books: _grammarEnabledBooks})}).catch(() => {});
    block.remove();
  });
  body.appendChild(block);
  _fillGrammarBlock(block, item, sentence);   // item 无 engine → 直接渲染翻译+语法点+依存图(不流式)
}
// 「📒 单词本」按钮入口（顶栏没放按钮，但保留以备调用）
window.toggleVocab = () => {
  const p = document.getElementById('grammar-panel');
  const on = document.querySelector('#side-tabs .side-tab[data-pane="vocab"]')?.classList.contains('active');
  if (p.classList.contains('open') && on) { closeGrammarPanel(); return; }
  openGrammarPanel(); switchSideTab('vocab');
};

// ── 单词本：列表 + 发音 + 加 Anki + 定位页 + 点开释义 ──
let _vocabScope = 'book', _vocabLoaded = false;
window.loadVocabList = async (scope) => {
  if (scope) _vocabScope = scope;
  _vocabLoaded = true;
  const listEl = document.getElementById('vocab-list');
  const cntEl = document.getElementById('vocab-count');
  if (!listEl) return;
  document.querySelectorAll('#vocab-scope-row button').forEach(b => b.classList.toggle('active', b.dataset.scope === _vocabScope));
  listEl.innerHTML = '<div style="color:#5a6680;font-size:12px;padding:10px">加载中…</div>';
  if (cntEl) cntEl.textContent = '';
  try {
    let url = '/pdf/api/vocab-list?file=' + encodeURIComponent(FILE_REL || '') + '&scope=' + _vocabScope;
    if (_vocabScope === 'page') url += '&page=' + (currentPage || 0);
    const r = await fetch(url);
    const d = await r.json();
    const items = d.items || [];
    if (cntEl) cntEl.textContent = items.length ? (items.length + ' 词') : '';
    if (!items.length) {
      const empty = {page: '本页没查过单词', book: '这本书还没查过单词', all: '单词库为空'}[_vocabScope] || '没有单词';
      listEl.innerHTML = '<div style="color:#5a6680;font-size:12px;padding:10px">' + empty + '</div>';
      return;
    }
    listEl.innerHTML = '';
    for (const it of items) listEl.appendChild(_renderVocabItem(it));
  } catch (e) {
    listEl.innerHTML = '<div style="color:#ef4444;font-size:12px;padding:10px">加载失败：' + e.message + '</div>';
  }
};
window._refreshVocabIfPage = function() {
  const pane = document.getElementById('side-pane-vocab');
  if (pane && pane.classList.contains('active') && _vocabScope === 'page') loadVocabList();
};
function _masteryColor(m) {
  if (m >= 0.8) return '#22c55e';
  if (m >= 0.5) return '#eab308';
  if (m >= 0.2) return '#f97316';
  return '#ef4444';
}
function _speakWord(lemma, audio) {
  if (audio) {
    try {
      const a = new Audio('/pdf/api/vocab-audio?path=' + encodeURIComponent(audio));
      a.play().catch(() => _speakOnline(lemma));
      return;
    } catch (e) {}
  }
  _speakOnline(lemma);
}
// 发音:日语词用浏览器原生日语语音(iPad 自带 Kyoko/Otoya,有道英语库没有日语 → 之前无声);
// 英语词用有道真人 mp3(免 key,覆盖广),失败退化浏览器 en-US TTS。
function _speakOnline(w) {
  if (!w) return;
  if (typeof _isJaWord === 'function' && _isJaWord(w)) { _ttsWord(w, 'ja-JP'); return; }
  try {
    const a = new Audio('https://dict.youdao.com/dictvoice?type=2&audio=' + encodeURIComponent(w));
    a.play().catch(() => _ttsWord(w, 'en-US'));
  } catch (e) { _ttsWord(w, 'en-US'); }
}
function _ttsWord(w, lang) {
  lang = lang || 'en-US';
  try {
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(w);
    u.lang = lang;
    const pref = lang.slice(0, 2).toLowerCase();
    const norm = s => (s || '').toLowerCase().replace('_', '-');
    const vs = speechSynthesis.getVoices() || [];   // iOS 首次可能为空，getVoices 触发加载
    const v = vs.find(x => norm(x.lang) === lang.toLowerCase())
           || vs.find(x => norm(x.lang).startsWith(pref));
    if (v) u.voice = v;
    speechSynthesis.speak(u);
  } catch (e) {}
}
// 暴露到全局:HTML 内联 onclick 在全局作用域执行,模块内的函数声明它够不到(否则按钮无声)
window._ttsWord = _ttsWord;
window._speakOnline = _speakOnline;
window._speakWord = _speakWord;
async function _vocabAddAnki(lemma, btn) {
  const old = btn.textContent;
  btn.textContent = '…'; btn.disabled = true;
  try {
    const r = await fetch('/pdf/api/vocab-anki', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({word: lemma}),
    });
    const d = await r.json();
    if (d.ok) { btn.textContent = '✓ 已加'; btn.classList.add('done'); }
    else { btn.textContent = old; btn.disabled = false; _toast?.('加卡失败：' + (d.error || '?')); }
  } catch (e) { btn.textContent = old; btn.disabled = false; _toast?.('加卡失败：' + e.message); }
}
function _renderVocabItem(it) {
  const div = document.createElement('div');
  div.className = 'vocab-item';
  const pct = Math.round((it.mastery || 0) * 100);
  const col = _masteryColor(it.mastery || 0);
  const pagesHtml = (it.pages || []).slice(0, 12).map(p => `<a data-page="${p}">p${p}</a>`).join('');
  div.innerHTML = `
    <div class="vi-head">
      <span class="vi-word">${_esc(it.lemma)}</span>
      ${it.phonetic ? `<span class="vi-phon">${_esc(it.phonetic)}</span>` : ''}
      <button class="vi-audio" title="发音">🔊</button>
      <span class="vi-mastery-badge" style="background:${col}22;color:${col}">${_esc(it.mastery_label || (pct + '%'))}</span>
    </div>
    <div class="vi-bar"><div style="width:${pct}%;background:${col}"></div></div>
    ${it.zh ? `<div class="vi-zh">${_esc(it.zh)}</div>` : ''}
    <div class="vi-foot">
      <span class="vi-pages">${pagesHtml}</span>
      <button class="vi-anki">${it.has_card ? '✓ 已加' : '📇 加卡'}</button>
    </div>`;
  if (it.has_card) div.querySelector('.vi-anki').classList.add('done');
  div.querySelector('.vi-word').addEventListener('click', () => dictStream(it.lemma, ''));
  div.querySelector('.vi-audio').addEventListener('click', (e) => { e.stopPropagation(); _speakWord(it.lemma, it.audio); });
  div.querySelector('.vi-anki').addEventListener('click', (e) => {
    e.stopPropagation();
    const b = e.currentTarget;
    if (!b.classList.contains('done')) _vocabAddAnki(it.lemma, b);
  });
  div.querySelectorAll('.vi-pages a').forEach(a => a.addEventListener('click', () => {
    const pg = parseInt(a.dataset.page); if (pg) goToPage(pg);
  }));
  return div;
}
window.toggleReadMode = async () => {
  const keepPage = currentPage;   // 记住切换前的页，切换后强制回到它（别跳回第一页）
  readMode = readMode === 'single' ? 'continuous' : 'single';
  try { localStorage.setItem('pdf-read-mode', readMode); } catch (_) {}
  document.getElementById('mode-toggle').textContent = readMode === 'continuous' ? '📚 连续' : '📄 单页';
  _pendingScrollY = 0;   // 清掉位置恢复残留，否则 setupContinuousMode 的定位会被跳过
  if (readMode === 'continuous') {
    await setupContinuousMode();
    currentPage = keepPage;
    // 占位高度算准后再滚一次到原页（setupContinuousMode 内部那次可能早于布局稳定）
    const t = document.querySelector(`[data-page-num="${keepPage}"]`);
    if (t) setTimeout(() => t.scrollIntoView({block: 'start', behavior: 'auto'}), 80);
  } else {
    currentPage = keepPage;
    await renderPage(keepPage);
  }
  _saveLastPosition({page: currentPage, mode: readMode, scale});
};

// 容器宽度变化 → 重算 scale 并重渲染（解决 PDF 被 CSS 缩放拉伸/模糊）
let _refitDebounce = null, _refitBusy = false, _lastFitWidth = 0;
// #main 真实可用内容宽度（clientWidth 含 padding，开侧栏加 padding-right 后必须减掉真实 padding）
function _mainContentWidth() {
  const m = document.getElementById('main');
  const cs = getComputedStyle(m);
  return m.clientWidth - (parseFloat(cs.paddingLeft)||0) - (parseFloat(cs.paddingRight)||0);
}
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
    const newScale = Math.max(0.5, Math.min(_scaleMax, mainW / v0.width));
    if (Math.abs(newScale - scale) < 0.01 && !force) return;
    // 保存当前滚动相对位置（按 page-container 高度比例）
    const container = document.getElementById('page-container');
    const ratio = container && container.offsetHeight
      ? main.scrollTop / Math.max(1, container.offsetHeight)
      : 0;
    scale = newScale;
    _lastFitWidth = mainW;
    if (readMode === 'continuous') {
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
  newScale = Math.max(0.5, Math.min(_scaleMax, newScale));
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
    requestAnimationFrame(() => { if (container && container.offsetHeight) main.scrollTop = Math.floor(ratio * container.offsetHeight); });
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
      _pinch.target = Math.max(0.5, Math.min(_scaleMax, _pinch.s0 * (d / _pinch.d0)));
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
async function setupContinuousMode() {
  const container = document.getElementById('page-container');
  container.innerHTML = '';
  if (_contIO) { _contIO.disconnect(); _contIO = null; }
  // 占位 size：用第 1 页尺寸估算所有页（同一本书各页尺寸基本一致），避免逐页 getPage 阻塞主线程。
  // 渲染该页时 _renderPageInto 会清掉固定高、按真实 viewport 撑开 → 尺寸自动修正。
  const p1 = await pdfDoc.getPage(1);
  const v1 = p1.getViewport({scale});
  const estW = Math.floor(v1.width), estH = Math.floor(v1.height);
  const frag = document.createDocumentFragment();
  for (let num = 1; num <= pdfDoc.numPages; num++) {
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
    ph.style.margin = '0 auto 12px';
    ph.textContent = '… 第 ' + num + ' 页';
    frag.appendChild(ph);
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
        }
        break;
      }
    }
  }, 200);
}

// 选中浮出工具栏 —— 跨桌面（mouseup）+ 触屏（touchend）+ 通用（selectionchange）
const toolbar = document.getElementById('sel-toolbar');
let lastSelText = '';
     _updateSelPreview('');
let _selTimer = null;
// ──────── char-layer：PyMuPDF char-level bbox 驱动的精确选中 ────────

let _charSel = null;   // {pw, startIdx, endIdx, dragging}

async function loadCharsAndBindLayer(num, wrap, viewport, _retry) {
  _retry = _retry || 0;
  if (!wrap.isConnected) return;   // 页已被连续模式释放 → 放弃
  let d = null;
  try {
    const r = await fetch(`/pdf/api/page-chars?file=${encodeURIComponent(FILE_REL)}&page=${num}&v=${CHARS_VER}`);
    d = await r.json();
  } catch (e) { d = null; }
  if (!d || !d.ok) {
    // 偶尔失败(并发/超时) → 退避重试，避免整页无 char-layer(点选失灵)/无 L 框
    if (_retry < 2 && wrap.isConnected) {
      await new Promise(res => setTimeout(res, 500 + _retry * 600));
      return loadCharsAndBindLayer(num, wrap, viewport, _retry + 1);
    }
    window.dlog?.('chars api fail: ' + ((d && d.error) || 'fetch') + ' (retry ' + _retry + ')', '#ff6b6b');
    return;
  }
  const scale = viewport.scale;
  const pageH = d.page_h;
  // PDF 坐标 → viewport CSS px。PDF y 向上为正，需翻转
  // 关键修正：PyMuPDF rawdict 的 bbox 已经是 image coordinate (y 向下, 原点左上)
  // 之前误做 (pageH - y1) y 翻转 → 上下颠倒，整个 chars overlay 错位
  const charBoxes = d.chars.map((ch, _oi) => ({
    c: ch.c,
    _oi,   // PyMuPDF 原始顺序(=reading order)；sort 时 x 几乎重叠的字符靠它保序
    w: (ch.w == null ? -1 : ch.w),   // 所属词 id（PyMuPDF 分词）：判词边界用，根治连字/紧排
    bk: (ch.bk == null ? -1 : ch.bk),   // rawdict 块号：句子扩展按块切（斜体 w=-1 也不丢块）
    left:   ch.x0 * scale,
    top:    ch.y0 * scale,
    width:  (ch.x1 - ch.x0) * scale,
    height: (ch.y1 - ch.y0) * scale,
    sp: !!ch.sp,
    // raw PDF 坐标（pt）— 用于持久化高亮 rects（不随 zoom 变）
    _x0: ch.x0, _y0: ch.y0, _x1: ch.x1, _y1: ch.y1,
  }));
  wrap.__pageWPt = d.page_w;
  wrap.__pageHPt = d.page_h;
  wrap.__viewportScale = scale;
  // sort 让 idx 顺序 = visual reading order：用 baseline (top + height) + 宽松阈值
  // ref 用 max height 避开 subscript/superscript（小字符）误判到不同行
  charBoxes.sort((a, b) => {
    const aBase = a.top + a.height;
    const bBase = b.top + b.height;
    const ref = Math.max(a.height, b.height) || 1;
    if (Math.abs(aBase - bBase) > ref * 0.8) return aBase - bBase;
    // 同一个词内(PyMuPDF 分词)→ 严格按原始 reading order，绝不按 left 排乱(连字/紧排 often→etn 的根治)
    if (a.w !== -1 && a.w === b.w) return a._oi - b._oi;
    // 词信息缺失(旧数据/符号)的兜底：x 几乎重叠也保原序
    if (Math.abs(a.left - b.left) < ref * 0.3) return a._oi - b._oi;
    return a.left - b.left;
  });
  // 调试模式（URL 加 ?dbg=1）：显示 char bbox + 内容，验证 PyMuPDF 提取跟 PDF 视觉是否对齐
  if (new URLSearchParams(location.search).get('dbg') === '1') {
    const dbgLayer = document.createElement('div');
    dbgLayer.style.cssText = 'position:absolute;inset:0;pointer-events:none;z-index:5';
    charBoxes.forEach((c, i) => {
      const d = document.createElement('div');
      d.style.cssText = `position:absolute;left:${c.left}px;top:${c.top}px;width:${c.width}px;height:${c.height}px;border:1px solid rgba(255,0,0,.4);font-size:9px;color:rgba(255,0,0,.8);line-height:${c.height}px;text-align:center;font-family:monospace`;
      d.textContent = c.sp ? '·' : c.c;
      d.title = `idx=${i} c=${JSON.stringify(c.c)}`;
      dbgLayer.appendChild(d);
    });
    wrap.appendChild(dbgLayer);
  }
  wrap.__charBoxes = charBoxes;
  window.dlog?.('chars: ' + charBoxes.length + ' on page ' + num);
  // 创建 char-layer（透明覆盖整个 page-wrap）
  const cl = document.createElement('div');
  cl.className = 'char-layer';
  wrap.appendChild(cl);
  wrap.__charLayer = cl;
  _bindCharLayer(cl, wrap);
  // 已保存高亮：渲染到该页
  try { renderHighlightsOnPage(wrap, num); } catch(e) { window.dlog?.('hl render fail: '+e.message,'#ff6b6b'); }
  // 单词下划线（mastery 着色）；后端 page-chars 已返回 vocab_marks
  wrap.__vocabMarks = d.vocab_marks || [];
  wrap.__vocabSentences = d.vocab_sentences || [];
  try { renderVocabUnderlines(wrap, wrap.__vocabMarks); } catch(e) { window.dlog?.('vocab underline fail: '+e.message,'#ff6b6b'); }
  try { renderVocabSentences(wrap, wrap.__vocabSentences); } catch(e) { window.dlog?.('vocab sentence fail: '+e.message,'#ff6b6b'); }
  // 振假名/音标叠加（后端 page-chars 已返回 furigana：日语 unidic 读音 + 英文 ECDICT 音标）
  wrap.__furigana = d.furigana || [];
  wrap.__furiVerified = false;
  try { renderRubyLayer(wrap); } catch(e) { window.dlog?.('ruby fail: '+e.message,'#ff6b6b'); }
  if (_rubyEnabled()) _verifyFurigana(wrap);   // 振假名读音 AI 上下文校正(后台,不阻塞)
  try { renderPhraseHl(wrap); } catch(_) {}   // 词组持久高亮：重渲染后从状态恢复（防"自动消失"）
  // 整页翻译模式开着 → 新渲染/滚入的页自动翻译
  if (_pageTrOn) { wrap.__pageTrSeq = null; _pageTranslatePage(wrap); }
  // 全文搜索跳转：本页刚好是待高亮页 → 此处 __charBoxes 已赋值，直接画（不走轮询，秒级到位）
  if (window._pendingSearchHighlight && window._pendingSearchHighlight.page === num
      && wrap.__charBoxes && wrap.__charBoxes.length) {
    try { _highlightSearchResultsOnPage(wrap, window._pendingSearchHighlight.query); } catch (_) {}
    window._pendingSearchHighlight = null;
  }
}

// 查 char idx 是否在某 vocab mark 范围（rects PDF pt 坐标）内；返回 mark 或 null
function _findVocabMarkAt(pw, charIdx) {
  if (!pw || !pw.__vocabMarks || !pw.__charBoxes) return null;
  const c = pw.__charBoxes[charIdx];
  if (!c || c._x0 === undefined) return null;
  const cx = (c._x0 + c._x1) / 2;
  const cy = (c._y0 + c._y1) / 2;
  for (const m of pw.__vocabMarks) {
    for (const r of (m.rects || [])) {
      if (cx >= r[0] && cx <= r[2] && cy >= r[1] && cy <= r[3]) return m;
    }
  }
  return null;
}
function _clickTranslateEnabled() {
  const v = localStorage.getItem('pdf-click-translate-unmastered');
  return v === null ? true : (v === '1');
}

function renderVocabUnderlines(pw, marks) {
  if (!_vocabUnderlineEnabled()) return;
  // 确保有 layer（即使 marks 空也要清旧残留）
  let layer = pw.querySelector('.vocab-layer');
  if (!layer && marks && marks.length) {
    layer = document.createElement('div');
    layer.className = 'vocab-layer';
    pw.appendChild(layer);
  }
  if (!layer) return;
  layer.innerHTML = '';
  if (!marks || !marks.length) return;
  const canvas = pw.querySelector('canvas');
  const cssW = canvas?.clientWidth || pw.clientWidth;
  const cssH = canvas?.clientHeight || pw.clientHeight;
  const pageWPt = pw.__pageWPt || cssW;
  const pageHPt = pw.__pageHPt || cssH;
  if (!cssW || !cssH || !pageWPt || !pageHPt) return;
  const sx = cssW / pageWPt;
  const sy = cssH / pageHPt;
  for (const m of marks) {
    for (const r of (m.rects || [])) {
      const x0 = r[0], y0 = r[1], x1 = r[2], y1 = r[3];
      const div = document.createElement('div');
      div.className = 'vocab-underline m-' + m.label_slug;
      div.style.left = (x0 * sx) + 'px';
      div.style.top = (y1 * sy + 1) + 'px';   // 字底 + 1px
      div.style.width = ((x1 - x0) * sx) + 'px';
      layer.appendChild(div);
    }
  }
}
function _vocabUnderlineEnabled() {
  // localStorage 默认开
  const v = localStorage.getItem('pdf-vocab-underline');
  return v === null ? true : (v === '1');
}

// ──────── 振假名 / 音标叠加（ruby） ────────
function _rubyEnabled() { return localStorage.getItem('pdf-ruby') === '1'; }   // 默认关
function renderRubyLayer(pw) {
  let layer = pw.querySelector('.ruby-layer');
  if (!_rubyEnabled()) { if (layer) layer.remove(); return; }
  const items = pw.__furigana || [];
  if (!layer) { layer = document.createElement('div'); layer.className = 'ruby-layer'; pw.appendChild(layer); }
  layer.innerHTML = '';
  if (!items.length) return;
  const canvas = pw.querySelector('canvas');
  const cssW = canvas?.clientWidth || pw.clientWidth;
  const cssH = canvas?.clientHeight || pw.clientHeight;
  const pageWPt = pw.__pageWPt || cssW;
  const pageHPt = pw.__pageHPt || cssH;
  if (!cssW || !cssH || !pageWPt || !pageHPt) return;
  const sx = cssW / pageWPt, sy = cssH / pageHPt;
  for (const it of items) {
    const rt = it.rt || ''; if (!rt) continue;
    const x0 = it.x0 * sx, y0 = it.y0 * sy, x1 = it.x1 * sx, y1 = it.y1 * sy;
    const w = Math.max(6, x1 - x0), h = Math.max(6, y1 - y0);
    // 字号：约词高 36%；并受「词宽/读音字数」约束使读音横向不超过词宽（系数 1.0，
    // 之前 1.4 → 读音比词宽 40% + overflow:hidden 把末尾假名切掉，故收到 1.0 + 配 overflow:visible）
    const fs = Math.max(7, Math.min(h * 0.36, w / Math.max(1, rt.length) * 1.0));
    const sp = document.createElement('span');
    sp.className = 'rt';
    sp.textContent = rt;
    sp.style.left = x0 + 'px';
    sp.style.width = w + 'px';
    sp.style.fontSize = fs.toFixed(1) + 'px';
    // ruby 略偏下贴近本行汉字：只 1/3 露在框顶之上，2/3 落进本行字框顶部 padding(=视觉空隙)。
    // （OCR 字框顶有 padding，框比实际字高；偏下放更贴自己这行、更不碰上一行。用户要求再向下些。）
    sp.style.top = Math.max(0, y0 - fs * 0.34) + 'px';
    layer.appendChild(sp);
  }
}
function refreshRubyAllPages() {
  document.querySelectorAll('[data-loaded="1"][data-page-num]').forEach(pw => {
    try { renderRubyLayer(pw); } catch (_) {}
    if (_rubyEnabled()) _verifyFurigana(pw);
  });
}
// 振假名读音 AI 上下文校正：后台调 /api/furigana-verify，拿纠正(计数器/熟字训/多音字)原地重画。
// 每页只调一次（结果后端按页永久缓存）；不阻塞渲染。
async function _verifyFurigana(pw) {
  if (!_rubyEnabled() || !pw || pw.__furiVerified || !(pw.__furigana || []).length) return;
  const num = parseInt(pw.dataset.pageNum || '0', 10); if (!num) return;
  pw.__furiVerified = true;
  try {
    const d = await (await fetch('/pdf/api/furigana-verify?file=' + encodeURIComponent(FILE_REL) + '&page=' + num)).json();
    if (!d.ok || !(d.fixes || []).length) return;
    let changed = false;
    for (const f of d.fixes) {
      if (pw.__furigana && pw.__furigana[f.i] && pw.__furigana[f.i].rt !== f.r) {
        pw.__furigana[f.i].rt = f.r; changed = true;
      }
    }
    if (changed && _rubyEnabled()) renderRubyLayer(pw);   // 用纠正后的读音重画
  } catch (_) { pw.__furiVerified = false; }
}
window.toggleRuby = () => {
  const on = !_rubyEnabled();
  try { localStorage.setItem('pdf-ruby', on ? '1' : '0'); } catch (_) {}
  const b = document.getElementById('ruby-toggle');
  if (b) b.classList.toggle('active', on);
  if (on && _pageTrOn) {   // 互斥:开注音 → 关译页(都占行上方空隙)
    _pageTrOn = false;
    document.getElementById('pagetr-toggle')?.classList.remove('active');
    document.querySelectorAll('.page-tr-layer').forEach(el => el.remove());
    document.querySelectorAll('[data-page-num]').forEach(pw => { pw.__pageTrSeq = null; });
  }
  refreshRubyAllPages();
};

// ──────── 整页翻译（F3）：逐句就地白底中文覆盖 ────────
let _pageTrOn = false;
function _clearPageTranslate(pw) { pw.querySelector('.page-tr-layer')?.remove(); }
// rects[[x0,y0,x1,y1]...] → 按垂直中心聚类合并成「每视觉行一个 rect」(同 _drawSentenceOverlay)
function _mergeLines(rects) {
  const raw = rects.filter(r => (r[2] - r[0]) > 0.5 && (r[3] - r[1]) > 0.5);
  if (!raw.length) return [];
  const refH = Math.max.apply(null, raw.map(r => r[3] - r[1])) || 1;
  const cy = r => (r[1] + r[3]) / 2;
  const sorted = raw.slice().sort((a, b) => (cy(a) - cy(b)) || (a[0] - b[0]));
  const lines = [];
  for (const r of sorted) {
    const last = lines[lines.length - 1];
    if (last && Math.abs(cy(r) - cy(last)) <= refH * 0.5) {
      last[0] = Math.min(last[0], r[0]); last[1] = Math.min(last[1], r[1]);
      last[2] = Math.max(last[2], r[2]); last[3] = Math.max(last[3], r[3]);
    } else lines.push([r[0], r[1], r[2], r[3]]);
  }
  lines.sort((a, b) => a[1] - b[1]);
  return lines;
}
function _drawPageTranslate(pw, sentences) {
  _clearPageTranslate(pw);
  const canvas = pw.querySelector('canvas');
  const cssW = canvas?.clientWidth || pw.clientWidth;
  const cssH = canvas?.clientHeight || pw.clientHeight;
  const pageWPt = pw.__pageWPt || cssW, pageHPt = pw.__pageHPt || cssH;
  if (!cssW || !cssH || !pageWPt || !pageHPt) return;
  const sx = cssW / pageWPt, sy = cssH / pageHPt;
  const layer = document.createElement('div');
  layer.className = 'page-tr-layer';
  pw.appendChild(layer);

  // 行间对照翻译:不遮原文。每句译文按其各行分配,每行片段放到该行**字框顶部留白**里(完全照搬
  // 振假名定位:top=y0-fs*0.34,字号≈行高×0.4)。char bbox 含上下留白(字形只占中段),所以这块
  // 留白视觉上是空的,小字落进去基本不压字形,也是振假名能和密排正文共存的原因。注音与译页互斥。
  for (const s of sentences) {
    const zh = (s.zh || '').trim(); if (!zh) continue;
    const raw = (s.rects || []).filter(r => (r[2] - r[0]) > 1 && (r[3] - r[1]) > 1);
    if (!raw.length) continue;
    const lines = _mergeLines(raw);
    const cps = Array.from(zh); const N = cps.length; let idx = 0;
    lines.forEach((r, i) => {
      const x0 = r[0] * sx, y0 = r[1] * sy, w = (r[2] - r[0]) * sx, h = (r[3] - r[1]) * sy;
      const annFs = Math.max(7, h * 0.40);                 // 行间小字档(≈原文行高×0.4,略大于振假名 0.36)
      const cap = Math.max(1, Math.floor(w / annFs));      // 该行能放几个汉字(分配用)
      const n = (i === lines.length - 1) ? (N - idx) : Math.max(0, Math.min(cap, N - idx));
      const slice = cps.slice(idx, idx + n).join(''); idx += n;
      if (!slice) return;
      const fs = Math.max(7, Math.min(annFs, w / slice.length));   // 不超小字档 + 塞进行宽单行不外溢
      const rt = document.createElement('div');
      rt.className = 'page-tr-rt';
      rt.style.left = x0 + 'px';
      rt.style.width = w + 'px';
      rt.style.top = (y0 - fs * 0.34) + 'px';   // = 振假名位置:落在本行字框顶部留白
      rt.style.fontSize = fs.toFixed(1) + 'px';
      rt.textContent = slice;
      layer.appendChild(rt);
    });
  }
}
async function _pageTranslatePage(pw) {
  if (!_pageTrOn || !pw) return;
  const num = parseInt(pw.dataset.pageNum || '0', 10); if (!num) return;
  if (pw.__pageTrSeq === num) return;   // 该页已翻过（防重复请求）
  pw.__pageTrSeq = num;
  try {
    const r = await fetch('/pdf/api/page-translate?file=' + encodeURIComponent(FILE_REL) + '&page=' + num);
    const d = await r.json();
    if (!_pageTrOn) return;             // 请求途中被关掉
    if (d.ok && d.sentences) _drawPageTranslate(pw, d.sentences);
  } catch (e) {
    window.dlog?.('page-tr p.' + num + ' fail: ' + e.message, '#ff6b6b');
    pw.__pageTrSeq = null;             // 失败允许重试
  }
}
function _pageTranslateApplyAll() {
  document.querySelectorAll('[data-loaded="1"][data-page-num]').forEach(pw => _pageTranslatePage(pw));
}
window.togglePageTranslate = () => {
  _pageTrOn = !_pageTrOn;
  const b = document.getElementById('pagetr-toggle');
  if (b) b.classList.toggle('active', _pageTrOn);
  if (_pageTrOn) {
    if (_rubyEnabled()) {   // 互斥:开译页 → 关注音(都占行上方空隙)
      try { localStorage.setItem('pdf-ruby', '0'); } catch (_) {}
      document.getElementById('ruby-toggle')?.classList.remove('active');
      document.querySelectorAll('.ruby-layer').forEach(el => el.remove());
    }
    _toast?.('整页翻译开启，翻译中…');
    _pageTranslateApplyAll();
  } else {
    document.querySelectorAll('.page-tr-layer').forEach(el => el.remove());
    document.querySelectorAll('[data-page-num]').forEach(pw => { pw.__pageTrSeq = null; });
  }
};

// ──────── 全文搜索（F4） ────────
let _searchTimer = null, _searchSeq = 0;
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
    const r = await fetch('/pdf/api/search?file=' + encodeURIComponent(FILE_REL) +
      '&q=' + encodeURIComponent(q) + '&limit=200');
    const d = await r.json();
    if (seq !== _searchSeq) return;   // 已被更新的查询取代
    if (!d.ok) { box.innerHTML = '<div class="sr-empty">搜索失败：' + (d.error || '?') + '</div>'; stat.textContent = ''; return; }
    stat.textContent = d.total + ' 处 / ' + d.pages + ' 页';
    if (!d.matches.length) { box.innerHTML = '<div class="sr-empty">未找到「' + _esc(q) + '」</div>'; return; }
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
        '<span class="sr-pg">P' + m.page + (m.count > 1 ? '·' + m.count : '') + '</span>' +
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
  window._pendingSearchHighlight = q ? {query: q, page: pg} : null;
  goToPage(pg);
  _applyPendingSearchHighlight();   // 已加载的页立即高亮；未加载则轮询等待
};
// 轮询等目标页 __charBoxes 就绪（单页/连续模式通用），就绪后画命中高亮
function _applyPendingSearchHighlight(tries) {
  tries = tries || 0;
  const ph = window._pendingSearchHighlight;
  if (!ph) return;
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
function _markVocabOptimistic(pw, lemma, forms) {
  if (!_vocabUnderlineEnabled() || !pw || !pw.__charBoxes) return;
  const fset = new Set([lemma, ...(forms || [])].filter(Boolean).map(f => String(f).toLowerCase()));
  if (!fset.size) return;
  let layer = pw.querySelector('.vocab-layer');
  if (!layer) { layer = document.createElement('div'); layer.className = 'vocab-layer'; pw.appendChild(layer); }
  const chars = pw.__charBoxes;
  let i = 0;
  while (i < chars.length) {
    const c = chars[i];
    if (c.sp || !/[A-Za-z]/.test(c.c || '')) { i++; continue; }
    let j = i, word = '';
    while (j < chars.length && !chars[j].sp && /[A-Za-z'’\-]/.test(chars[j].c || '')) { word += chars[j].c; j++; }
    if (fset.has(word.toLowerCase())) {
      const a = chars[i], b = chars[j - 1];
      if (a.left != null && a.top != null) {
        const div = document.createElement('div');
        div.className = 'vocab-underline m-new';
        div.style.left = a.left + 'px';
        div.style.top = (a.top + a.height + 1) + 'px';
        div.style.width = Math.max(4, (b.left + b.width - a.left)) + 'px';
        layer.appendChild(div);
      }
    }
    i = j;
  }
}

// 查完一个词后，刷新所有已渲染页的下划线（让新查的词立刻出现下划线）
async function refreshVocabUnderlinesForAllPages() {
  if (!_vocabUnderlineEnabled() || !pdfDoc) return;
  // 单页模式当前页是 #page-container 自身(无 .page-wrap class)，故按 data-loaded+page-num 选，覆盖两种模式
  const wraps = document.querySelectorAll('[data-loaded="1"][data-page-num]');
  for (const pw of wraps) {
    const pn = parseInt(pw.dataset.pageNum || '0', 10);
    if (!pn) continue;
    try {
      const r = await fetch('/pdf/api/page-vocab-marks?file=' + encodeURIComponent(FILE_REL) + '&page=' + pn);
      const d = await r.json();
      if (!d.ok) continue;
      pw.__vocabMarks = d.vocab_marks || [];
      pw.__vocabSentences = d.vocab_sentences || [];
      renderVocabUnderlines(pw, pw.__vocabMarks);
      renderVocabSentences(pw, pw.__vocabSentences);
    } catch (e) { window.dlog?.('vocab refresh p.' + pn + ' fail: ' + e.message, '#ff6b6b'); }
  }
}

// 句子颜色 palette：[stroke, fill]；按 sid 轮替
const SENT_COLORS = [
  ['#d97706', 'rgba(245,158,11,.18)'],   // 橙
  ['#059669', 'rgba(16,185,129,.18)'],   // 绿
  ['#2563eb', 'rgba(59,130,246,.18)'],   // 蓝
  ['#9333ea', 'rgba(168,85,247,.18)'],   // 紫
  ['#db2777', 'rgba(236,72,153,.18)'],   // 粉
  ['#0891b2', 'rgba(20,184,166,.18)'],   // 青
];

// vocab 句子 L 按钮：既能点击翻译整句，也能从其上转发拖选（绕开 pointer-events:auto 挡选中的问题）。
// mousedown/touch 时把坐标转发给所在页 char-layer 的拖选 API；命中字符则标记 _fromLBtn，
// 单击(无拖)由 click handler 翻译、onEnd 跳过查词；拖动则正常 char 选中、click 被 _dragMoved 拦下。
function _bindSentBtnDrag(btnEl, layer) {
  const getApi = () => {
    const pw = btnEl.closest('[data-page-num]') || layer.parentElement;
    return pw && pw.__charDrag;
  };
  btnEl.addEventListener('mousedown', (e) => {
    e.preventDefault();   // 对齐 cl mousedown：阻止产生原生 selection，否则 checkSelection 会覆盖 char-layer 选中
    e.stopPropagation();
    const api = getApi(); if (!api) return;
    const p = api.ptToLocal(e.clientX, e.clientY);
    if (api.onStart(p.x, p.y)) _fromLBtn = true;
  });
  btnEl.addEventListener('touchstart', (e) => {
    e.stopPropagation();
    const api = getApi(); if (!api || e.touches.length !== 1) return;
    const t = e.touches[0]; const p = api.ptToLocal(t.clientX, t.clientY);
    if (api.onStart(p.x, p.y)) _fromLBtn = true;
  }, {passive: true});
  btnEl.addEventListener('touchmove', (e) => {
    const api = getApi(); if (!api || e.touches.length !== 1) return;
    const t = e.touches[0]; const p = api.ptToLocal(t.clientX, t.clientY);
    api.onMove(p.x, p.y, e);
  }, {passive: false});
  btnEl.addEventListener('touchend', (e) => {
    const api = getApi(); if (!api) return;
    const t = e.changedTouches[0]; if (!t) return;
    const p = api.ptToLocal(t.clientX, t.clientY);
    api.onEnd(p.x, p.y);
  });
}

function renderVocabSentences(pw, sentences) {
  if (!_vocabUnderlineEnabled()) return;
  let layer = pw.querySelector('.vocab-layer');
  if (!layer && sentences && sentences.length) {
    layer = document.createElement('div');
    layer.className = 'vocab-layer';
    pw.appendChild(layer);
  }
  if (!layer) return;
  layer.querySelectorAll('.vocab-sentence-box, [class*="vocab-sentence-btn"]').forEach(el => el.remove());   // 含 btn-l / btn-l-start，否则删除/重渲染时 L 按钮残留
  if (!sentences || !sentences.length) return;
  const canvas = pw.querySelector('canvas');
  const cssW = canvas?.clientWidth || pw.clientWidth;
  const cssH = canvas?.clientHeight || pw.clientHeight;
  const pageWPt = pw.__pageWPt || cssW;
  const pageHPt = pw.__pageHPt || cssH;
  if (!cssW || !cssH || !pageWPt || !pageHPt) return;
  const sx = cssW / pageWPt;
  const sy = cssH / pageHPt;
  for (let si = 0; si < sentences.length; si++) {
    const s = sentences[si];
    const sid = String(si);
    const [stroke, fill] = SENT_COLORS[si % SENT_COLORS.length];
    const rects = s.rects || [];
    // 竖排文字（书页边竖排注释等）：每行 rect 高>宽，句子框/L按钮的几何按横排算会乱 → 跳过
    if (rects.length && rects.filter(r => (r[3]-r[1]) > (r[2]-r[0])).length > rects.length / 2) continue;
    // 缓存颜色到 sentence 对象（覆盖层 / Anki 加卡时复用）
    s.__stroke = stroke; s.__fill = fill;
    // hatch 排线 (135° 斜细线)。默认**淡**(alpha≈0x2e≈18%,细 1px,间距 5px)→ 整页多句也不刺眼；
    // 「翻译中」用加深版(strong, alpha 0x88) 配呼吸。
    const hatch = `repeating-linear-gradient(135deg, ${stroke}55 0 1px, transparent 1px 4px)`;
    const hatchStrong = `repeating-linear-gradient(135deg, ${stroke}88 0 1.2px, transparent 1.2px 4px)`;
    for (let ri = 0; ri < rects.length; ri++) {
      const r = rects[ri];
      const [x0, y0, x1, y1] = r;
      const box = document.createElement('div');
      box.className = 'vocab-sentence-box' + (s.__translating ? ' translating' : '');
      box.dataset.sid = sid;
      box.style.color = stroke;
      box.style.setProperty('--sent-fill', fill);
      box.style.setProperty('--sent-hatch', hatch);
      box.style.setProperty('--sent-hatch-strong', hatchStrong);
      box.style.left = (x0 * sx - 2) + 'px';
      box.style.top = (y0 * sy - 1) + 'px';
      box.style.width = ((x1 - x0) * sx + 4) + 'px';
      box.style.height = ((y1 - y0) * sy + 2) + 'px';
      layer.appendChild(box);
    }
    // L 形按钮（句首）：包裹第一个字符（border-left + border-top 4px）
    const fc = s.first_char;
    if (fc) {
      const btn0 = document.createElement('button');
      btn0.className = 'vocab-sentence-btn-l-start';
      btn0.type = 'button';
      btn0.dataset.sid = sid;
      btn0.title = `翻译整句（含 ${s.count} 个未掌握词）：${(s.text||'').slice(0,80)}…`;
      btn0.style.color = stroke;
      btn0.style.setProperty('--sent-fill', fill);
      const charW0 = (fc[2] - fc[0]) * sx;
      const charH0 = (fc[3] - fc[1]) * sy;
      const left0 = fc[0] * sx - 2;
      // clamp 到句首所在行的文本右边界(rects[0][2])，不伸进纸张右 margin → 不再画出延伸到页边的线
      const lineRight = (rects[0] ? rects[0][2] * sx : cssW);
      const maxW0 = Math.max(charW0, lineRight - left0);
      const wantW0 = Math.min(Math.max(charW0 * 6, 48), maxW0);
      btn0.style.left = left0 + 'px';     // 句首字符 x0 减 padding
      btn0.style.top = (fc[1] * sy - 2) + 'px';
      btn0.style.width = wantW0 + 'px';
      btn0.style.height = charH0 + 'px';
      btn0.addEventListener('click', (e) => {
        e.stopPropagation();
        if (_dragMoved) return;   // 刚从 L 按钮拖选 → 不触发整句翻译
        toggleSentenceOverlay(layer, s, btn0, sx, sy);
      });
      _bindSentBtnDrag(btn0, layer);
      btn0.addEventListener('mouseenter', () => {
        layer.querySelectorAll(`.vocab-sentence-box[data-sid="${sid}"]`).forEach(b => b.classList.add('highlight'));
      });
      btn0.addEventListener('mouseleave', () => {
        layer.querySelectorAll(`.vocab-sentence-box[data-sid="${sid}"].highlight`).forEach(b => b.classList.remove('highlight'));
      });
      layer.appendChild(btn0);
    }
    // L 形按钮（句末）：包裹最后一个字符（border-right + border-bottom 4px）
    const lc = s.last_char;
    if (lc) {
      const btn = document.createElement('button');
      btn.className = 'vocab-sentence-btn-l';
      btn.type = 'button';
      btn.dataset.sid = sid;
      btn.title = `翻译整句（含 ${s.count} 个未掌握词）：${(s.text||'').slice(0,80)}…`;
      btn.style.color = stroke;
      btn.style.setProperty('--sent-fill', fill);
      // 加宽：L 形左缘向左延伸（更易点击）
      const charW = (lc[2] - lc[0]) * sx;
      const charH = (lc[3] - lc[1]) * sy;
      const wantW = Math.max(charW * 6, 48);   // 至少 48px 宽（约 5-6 个字符）
      const extraLeft = wantW - charW;
      let leftE = lc[0] * sx - extraLeft - 2;
      let widthE = wantW;
      if (leftE < 0) { widthE = Math.max(charW, widthE + leftE); leftE = 0; }   // 句末在行首时向左延伸会溢出页左 → 截掉
      btn.style.left = leftE + 'px';
      btn.style.top = (lc[1] * sy - 2) + 'px';
      btn.style.width = widthE + 'px';
      btn.style.height = charH + 'px';
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        if (_dragMoved) return;   // 刚从 L 按钮拖选 → 不触发整句翻译
        toggleSentenceOverlay(layer, s, btn, sx, sy);
      });
      _bindSentBtnDrag(btn, layer);
      btn.addEventListener('mouseenter', () => {
        layer.querySelectorAll(`.vocab-sentence-box[data-sid="${sid}"]`).forEach(b => b.classList.add('highlight'));
      });
      btn.addEventListener('mouseleave', () => {
        layer.querySelectorAll(`.vocab-sentence-box[data-sid="${sid}"].highlight`).forEach(b => b.classList.remove('highlight'));
      });
      if (s.manual) _bindSentBtnLongPress(btn, s, pw);   // 仅翻译功能形成的框选可长按删；自动生词框不可
      layer.appendChild(btn);
    }
    if (s.first_char && s.manual) {
      const b0 = layer.querySelector(`.vocab-sentence-btn-l-start[data-sid="${sid}"]`);
      if (b0) _bindSentBtnLongPress(b0, s, pw);
    }
  }
}

// L 框长按 → 删除该句标记（仅翻译框选）。与短按(翻译)/拖选共存：移动或短按则取消；触发后弹确认
function _bindSentBtnLongPress(btn, s, pw) {
  let timer = null, x0 = 0, y0 = 0, fired = false;
  btn.addEventListener('pointerdown', (e) => {
    x0 = e.clientX; y0 = e.clientY; fired = false;
    clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null; fired = true;
      if (navigator.vibrate) { try { navigator.vibrate(30); } catch (_) {} }
      if (confirm('删除这个翻译框选？（只去掉框线/译文标记，不影响原文）')) _sentDismiss(s, pw);
    }, 600);
  });
  const cancel = (e) => {
    if (timer && e && e.type === 'pointermove' && Math.hypot(e.clientX - x0, e.clientY - y0) < 12) return;
    clearTimeout(timer); timer = null;
  };
  btn.addEventListener('pointermove', cancel);
  btn.addEventListener('pointerup', cancel);
  btn.addEventListener('pointercancel', cancel);
  // 长按已删 → 吃掉随后的 click，避免触发整句翻译
  btn.addEventListener('click', (e) => { if (fired) { fired = false; e.stopPropagation(); e.preventDefault(); } }, true);
}
function _sentDismiss(s, pw) {
  if (!s || !pw) return;
  const txt = (s.text || '').trim();
  if (pw.__vocabSentences) {
    pw.__vocabSentences = pw.__vocabSentences.filter(x => x !== s && (x.text || '').trim() !== txt);
    try { renderVocabSentences(pw, pw.__vocabSentences); } catch (_) {}
  }
  try { closeSentPopover && closeSentPopover(); } catch (_) {}
  if (FILE_REL && txt) {
    fetch('/pdf/api/sentence-dismiss', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file: FILE_REL, text: txt }),
    }).catch(() => {});
  }
}

// 在原句位置叠覆盖层显示中文（再次点击或点 overlay 关闭）
function toggleSentenceOverlay(layer, s, btn, sx, sy) {
  // 就地覆盖(用户原设计):逐行白条贴合原文行,中文按各行宽度比例分配填入。
  const existing = layer.querySelector('.vocab-sentence-overlay[data-active="1"]');
  if (existing && existing.dataset.sentText === s.text) {   // 再点同句 → 关
    layer.querySelectorAll('.vocab-sentence-overlay').forEach(el => el.remove());
    btn.classList.remove('active');
    return;
  }
  layer.querySelectorAll('.vocab-sentence-overlay').forEach(el => el.remove());
  layer.querySelectorAll('.vocab-sentence-btn-l.active, .vocab-sentence-btn-l-start.active')
    .forEach(b => b.classList.remove('active'));
  if (s.zh) { _drawSentenceOverlay(layer, s, btn, sx, sy); return; }
  // 没译过 → 现场翻再画
  btn.classList.add('active');
  fetch('/pdf/api/translate-sentence', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text: s.text}),
  }).then(r => r.json()).then(d => {
    if (d.ok && d.zh) { s.zh = d.zh; _drawSentenceOverlay(layer, s, btn, sx, sy); }
    else { btn.classList.remove('active'); _toast?.('翻译失败：' + (d.error || '?')); }
  }).catch(e => { btn.classList.remove('active'); _toast?.('网络错误：' + e.message); });
}

function _drawSentenceOverlay(layer, s, btn, sx, sy) {
  // 逐行盒子(每行=原文那行的精确 rect 位置/宽度),中文**贪心自然填充**:
  // 填满一行换下一行(像正常文字排版,标点跟着走、不躲逗号、不强行平均切),
  // **字号 = 原文字符高**(跟原句一致)。→ 中文严格落在原句每行位置上、该分行就分行。
  const raw = (s.rects || []).filter(r => (r[2] - r[0]) > 1 && (r[3] - r[1]) > 1);
  const zh = (s.zh || '').trim();
  if (!raw.length || !zh) return;
  layer.querySelectorAll('.vocab-sentence-overlay').forEach(el => el.remove());
  // 关键:s.rects 在逗号/间隙处被切成碎块 → 先合并成「整行一个 rect」。
  // ⚠ 必须按字符**垂直中心**聚类,不能按顶部 y0:日语逗号「，」的 y0 比汉字低很多、
  //   不同汉字 y0 也差几 px(笔画高低不同)→ 按 y0 会把逗号/汉字拆成不同行。
  //   中心则一致(逗号中心≈汉字中心),参考行高取最大字符高(避开逗号的小高)。
  const rects = (() => {
    const refH = Math.max.apply(null, raw.map(r => r[3] - r[1])) || 1;
    const cy = r => (r[1] + r[3]) / 2;
    const sorted = raw.slice().sort((a, b) => (cy(a) - cy(b)) || (a[0] - b[0]));
    const lines = [];
    for (const r of sorted) {
      const last = lines[lines.length - 1];
      if (last && Math.abs(cy(r) - cy(last)) <= refH * 0.5) {   // 同一视觉行(按中心)
        last[0] = Math.min(last[0], r[0]); last[1] = Math.min(last[1], r[1]);
        last[2] = Math.max(last[2], r[2]); last[3] = Math.max(last[3], r[3]);
      } else {
        lines.push([r[0], r[1], r[2], r[3]]);
      }
    }
    lines.sort((a, b) => a[1] - b[1]);   // 行按 top 排序 = 阅读顺序
    return lines;
  })();
  // 字号与原文一致:PyMuPDF char bbox(charH)比实际字号略大(含 ascent/descent 留白),
  // ×0.72 才视觉等于原文字符大小(0.95 实测明显偏大、行间挤);取各行最小行高更稳(避免某行有高字符拉大)
  const charH = Math.min(...rects.map(r => (r[3] - r[1]) * sy));
  const cps = Array.from(zh);                              // 按码点切
  const N = cps.length;
  // 字号默认=原文字高(×0.72);译文比原文长时按「各行总宽 / 字数」缩小,保证整句塞得下不被切
  // (+rects.length 留 floor 取整余量 → 各行 cap 之和 ≥ N,末行收尾不溢出 overflow:hidden)
  const Wtot = rects.reduce((s, r) => s + (r[2] - r[0]) * sx, 0);
  const fontPx = Math.max(9, Math.min(Math.round(charH * 0.72), Math.floor(Wtot / (N + rects.length))));
  let idx = 0;
  rects.forEach((r, i) => {
    const w = (r[2] - r[0]) * sx, h = (r[3] - r[1]) * sy;
    const cap = Math.max(1, Math.floor(w / fontPx));       // 该行能放几个汉字(≈方块宽=字号)
    const n = (i === rects.length - 1) ? (N - idx)         // 末行收尾(余下全给它)
      : Math.max(0, Math.min(cap, N - idx));
    const slice = cps.slice(idx, idx + n).join('');
    idx += n;
    const ov = document.createElement('div');
    ov.className = 'vocab-sentence-overlay';
    ov.dataset.active = '1';
    ov.dataset.sentText = s.text;
    if (s.__stroke) {
      ov.style.setProperty('--sent-stroke', s.__stroke);
      ov.style.setProperty('--sent-hatch',
        `repeating-linear-gradient(135deg, ${s.__stroke}33 0 1.2px, transparent 1.2px 4px)`);
    }
    if (s.__fill) ov.style.setProperty('--sent-fill', s.__fill);
    ov.style.left = (r[0] * sx) + 'px';                    // 贴该行原文位置(首行从行中间起也对)
    ov.style.top = (r[1] * sy) + 'px';
    ov.style.width = w + 'px';
    ov.style.fontSize = fontPx + 'px';
    ov.style.textAlign = 'left';
    ov.style.paddingLeft = '1px';
    // 极端:单行原文 + 超长译文,字号触底仍放不下 → 该框换行向下展开(不硬切)
    if (slice.length * fontPx > w + 0.5) {
      ov.style.minHeight = h + 'px';
      ov.style.whiteSpace = 'normal';
      ov.style.wordBreak = 'break-all';
      ov.style.lineHeight = (fontPx * 1.18) + 'px';
      ov.style.overflow = 'visible';
      ov.style.paddingTop = Math.max(0, (h - fontPx * 1.18) / 2) + 'px';
    } else {
      ov.style.height = h + 'px';
      ov.style.lineHeight = h + 'px';                      // 单行垂直居中
      ov.style.whiteSpace = 'nowrap';
    }
    ov.textContent = slice;
    ov.addEventListener('click', (e) => {
      e.stopPropagation();
      layer.querySelectorAll('.vocab-sentence-overlay').forEach(el => el.remove());
      btn.classList.remove('active');
    });
    ov.addEventListener('mousedown', (e) => e.stopPropagation());
    ov.addEventListener('touchstart', (e) => e.stopPropagation(), {passive: true});
    layer.appendChild(ov);
  });
  btn.classList.add('active');
}

// 翻译 popover
// 可视区右边界：侧栏展开时扣掉侧栏宽度，避免浮层被 clamp 到侧栏底下
function _visRight() {
  const panel = document.getElementById('grammar-panel');
  if (panel && document.body.classList.contains('grammar-open')) {
    const r = panel.getBoundingClientRect();
    if (r.width) return r.left;   // 侧栏左边缘 = 可视区右边界
  }
  return window.innerWidth;
}
function _ensureSentPopover() {
  let pop = document.getElementById('sent-popover');
  if (pop) return pop;
  pop = document.createElement('div');
  pop.id = 'sent-popover';
  pop.innerHTML = `
    <button class="sent-close" type="button" onclick="closeSentPopover()">×</button>
    <div class="sent-en"></div>
    <div class="sent-zh"></div>`;
  document.body.appendChild(pop);
  document.addEventListener('click', (e) => {
    if (!e.target.closest('#sent-popover') && !e.target.closest('.vocab-sentence-btn')) {
      closeSentPopover();
    }
  }, true);
  return pop;
}
window.closeSentPopover = () => {
  document.getElementById('sent-popover')?.classList.remove('open');
};

async function showSentenceTranslation(text, anchorBtn, preZh) {
  const pop = _ensureSentPopover();
  pop.querySelector('.sent-en').textContent = text;
  const zhEl = pop.querySelector('.sent-zh');
  // 定位
  const r = anchorBtn.getBoundingClientRect();
  pop.style.left = (r.right + window.scrollX + 6) + 'px';
  pop.style.top = (r.top + window.scrollY - 4) + 'px';
  pop.classList.add('open');
  requestAnimationFrame(() => {
    const pr = pop.getBoundingClientRect();
    if (pr.right > _visRight() - 8) {
      pop.style.left = Math.max(8, r.left + window.scrollX - pr.width / 2) + 'px';
      pop.style.top = (r.bottom + window.scrollY + 6) + 'px';
    }
  });
  // 已有预翻译 → 立即显示
  if (preZh) {
    zhEl.textContent = '🇨🇳 ' + preZh;
    zhEl.className = 'sent-zh';
    return;
  }
  // fallback：现场调
  zhEl.textContent = '⏳ 翻译中…';
  zhEl.className = 'sent-zh loading';
  try {
    const r2 = await fetch('/pdf/api/translate-sentence', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({text}),
    });
    const d = await r2.json();
    if (d.ok && d.zh) {
      zhEl.textContent = '🇨🇳 ' + d.zh;
      zhEl.className = 'sent-zh';
    } else {
      zhEl.textContent = '翻译失败：' + (d.error || '?');
      zhEl.className = 'sent-zh';
    }
  } catch (e) {
    zhEl.textContent = '网络错误：' + e.message;
    zhEl.className = 'sent-zh';
  }
}
window.showSentenceTranslation = showSentenceTranslation;
window.refreshVocabUnderlinesForAllPages = refreshVocabUnderlinesForAllPages;

// 找点击位置最近的非空格 char index
function _findCharAt(charBoxes, x, y) {
  // 先尝试落在某 char bbox 内（优先 non-space）
  for (let i = 0; i < charBoxes.length; i++) {
    const c = charBoxes[i];
    if (c.sp) continue;
    if (x >= c.left && x <= c.left + c.width && y >= c.top && y <= c.top + c.height) {
      return i;
    }
  }
  let best = -1, bestD = Infinity;
  // 同行内 X 距离最近的 non-space
  for (let i = 0; i < charBoxes.length; i++) {
    const c = charBoxes[i];
    if (c.sp) continue;
    const yIn = (y >= c.top - 2 && y <= c.top + c.height + 2);
    if (yIn) {
      const dx = (x < c.left) ? (c.left - x) : (x > c.left + c.width) ? (x - c.left - c.width) : 0;
      if (dx < bestD) { bestD = dx; best = i; }
    }
  }
  if (best >= 0) return best;
  // 整体 Manhattan 距离兜底
  for (let i = 0; i < charBoxes.length; i++) {
    const c = charBoxes[i];
    if (c.sp) continue;
    const cx = c.left + c.width / 2, cy = c.top + c.height / 2;
    const d = Math.abs(x - cx) + Math.abs(y - cy) * 3;
    if (d < bestD) { bestD = d; best = i; }
  }
  return best;
}

// chars[s..e] 拼成文本（含 X gap 智能空格 + 跨行换行；跟 _selByCharRange 同逻辑）
function _charsRangeToText(chars, sIdx, eIdx) {
  if (sIdx < 0 || eIdx >= chars.length || sIdx > eIdx) return '';
  // 块过滤：跳掉 reading order 落在区间中间、却属于别块的字符（双栏交错时把另一栏串进来）
  const _blk = (c) => (c.bk != null && c.bk >= 0) ? c.bk : ((c.w == null || c.w < 0) ? -1 : Math.floor(c.w / 1000000));
  const _sb = _blk(chars[sIdx]), _eb = _blk(chars[eIdx]);
  const _bLo = Math.min(_sb, _eb), _bHi = Math.max(_sb, _eb);
  const _inBlk = (c) => { if (_sb < 0 || _eb < 0) return true; const b = _blk(c); return b < 0 || (b >= _bLo && b <= _bHi); };
  let text = '', lastChar = null;
  const _cjk = s => /[぀-ヿ㐀-鿿　-〿＀-￯]/.test(s || '');
  for (let i = sIdx; i <= eIdx; i++) {
    const c = chars[i];
    if (!_inBlk(c)) continue;   // 别块(另一栏/题号)不计入
    if (lastChar) {
      // 两边都是 CJK(日/中,无词间空格)→ 跨行/间隙都直接拼,不插换行或空格(否则 公表する 跨行被拆成「公 表する」无法识别)
      const cjkPair = _cjk(c.c) && _cjk(lastChar.c);
      const dy = Math.abs(c.top - lastChar.top);
      if (dy > c.height * 0.5) { if (!cjkPair) text += '\n'; }
      else {
        const gap = c.left - (lastChar.left + lastChar.width);
        const ref = Math.min(c.height, lastChar.height);
        if (!cjkPair && gap > ref * 0.6 && !lastChar.sp && !c.sp) text += ' ';   // 0.6 防 justified 词内字距拉伸误拆(如 between→be tween)
      }
    }
    text += c.sp ? ' ' : c.c;
    lastChar = c;
  }
  return text.replace(/[ \t]+/g, ' ').replace(/ ?\n ?/g, '\n').trim();
}

// 找选中范围所在的句子（左/右扩到 . ! ? 。！？ 之后 / 段落起止）
function _expandSentenceFromRange(chars, sIdx, eIdx) {
  // 句子边界 = 句末标点(。！？.!?)优先。
  // ⚠ rawdict 块(bk)只在「跨块 **且** 跨视觉行」时才断:
  //   - 日语 justified 排版会把同一视觉行拆成多个块(如同行的「…解き,」「それら…」),
  //     这种同行拆块绝不能断,否则句子在逗号处截断、到不了句号(用户报的 bug)
  //   - 标题/邻段在不同视觉行+不同块 → 断(不把标题并进句子)
  const isSentEnd = (c) => /[.!?。！？]/.test(c);
  const _bk = (a, b) => a && b && a.bk != null && b.bk != null && a.bk >= 0 && b.bk >= 0 && a.bk !== b.bk;
  const _lineChanged = (a, b) => Math.abs(a.top - b.top) > Math.max(a.height, b.height) * 0.5;
  const _paraGap = (a, b) => Math.abs(a.top - b.top) > Math.max(a.height, b.height) * 1.5;
  const _stop = (a, b) => (_bk(a, b) && _lineChanged(a, b)) || _paraGap(a, b);
  let s = sIdx;
  while (s > 0) {
    if (isSentEnd(chars[s - 1].c)) break;
    if (_stop(chars[s - 1], chars[s])) break;
    s--;
  }
  let e = eIdx;
  while (e < chars.length - 1) {
    if (isSentEnd(chars[e].c)) break;
    if (_stop(chars[e], chars[e + 1])) break;
    e++;
  }
  return {start: s, end: e};
}

function _expandToWordStart(chars, idx) {
  if (idx < 0 || idx >= chars.length) return idx;
  const isWord = (c) => /[A-Za-z0-9_]/.test(c);
  const isCJK = (c) => /[　-〿぀-ゟ゠-ヿ㐀-䶿一-鿿＀-￯]/.test(c);   // 汉字+平/片假名+全角+CJK标点
  // 优先用 PyMuPDF 词边界：同一个词 id 直接扩到词首（根治连字/紧排/装饰编号粘连）
  if (chars[idx].w !== -1) {
    const wid = chars[idx].w;
    while (idx > 0 && chars[idx - 1].w === wid && !'/|／'.includes(chars[idx - 1].c)) idx--;
    // 跳过词首标点（PyMuPDF 把 “conditional 的弯引号并入同 word）
    while (idx < chars.length - 1 && chars[idx + 1].w === wid && !isWord(chars[idx].c) && !isCJK(chars[idx].c)) idx++;
    return idx;
  }
  if (!isWord(chars[idx].c)) return idx;
  while (idx > 0 && isWord(chars[idx - 1].c) &&
         Math.abs(chars[idx - 1].top - chars[idx].top) <= chars[idx].height * 0.5 &&
         (chars[idx].left - (chars[idx - 1].left + chars[idx - 1].width)) <= chars[idx].height * 0.8) {
    idx--;   // 词信息缺失兜底：大水平间隙(>0.8字高)=跨排版块，不并入同一词
  }
  return idx;
}
function _expandToWordEnd(chars, idx) {
  if (idx < 0 || idx >= chars.length) return idx;
  const isWord = (c) => /[A-Za-z0-9_]/.test(c);
  const isCJK = (c) => /[　-〿぀-ゟ゠-ヿ㐀-䶿一-鿿＀-￯]/.test(c);   // 汉字+平/片假名+全角+CJK标点
  if (chars[idx].w !== -1) {
    const wid = chars[idx].w;
    while (idx < chars.length - 1 && chars[idx + 1].w === wid && !'/|／'.includes(chars[idx + 1].c)) idx++;
    // 跳过词尾标点（more. 的句号、conditional” 的弯引号）
    while (idx > 0 && chars[idx - 1].w === wid && !isWord(chars[idx].c) && !isCJK(chars[idx].c)) idx--;
    return idx;
  }
  if (!isWord(chars[idx].c)) return idx;
  while (idx < chars.length - 1 && isWord(chars[idx + 1].c) &&
         Math.abs(chars[idx + 1].top - chars[idx].top) <= chars[idx].height * 0.5 &&
         (chars[idx + 1].left - (chars[idx].left + chars[idx].width)) <= chars[idx].height * 0.8) {
    idx++;
  }
  return idx;
}

function _selByCharRange(pw, sIdx, eIdx) {
  if (!pw || !pw.__charBoxes) return;
  if (sIdx > eIdx) { const t = sIdx; sIdx = eIdx; eIdx = t; }
  const chars = pw.__charBoxes;
  if (sIdx < 0 || eIdx >= chars.length) return;
  // 拖选两端自动对齐词边界（英文 \w 词；CJK 字符不动 - isWord 不匹配自动跳过）
  sIdx = _expandToWordStart(chars, sIdx);
  eIdx = _expandToWordEnd(chars, eIdx);
  // 按块过滤：只保留 block 序号在 [起点块, 终点块] 之间的字符，排除中间插入的别块
  // （双栏选一栏不带另一栏、题号不粘进指令；跨段落=相邻块仍能选）。w=-1(无块信息)时不过滤
  // 用 rawdict 块号 bk 过滤(斜体词 w=-1 也有块号)；bk 缺失才回退 w//1e6
  const _blk = (c) => (c.bk != null && c.bk >= 0) ? c.bk : ((c.w == null || c.w < 0) ? -1 : Math.floor(c.w / 1000000));
  const _sb = _blk(chars[sIdx]), _eb = _blk(chars[eIdx]);
  const _bLo = Math.min(_sb, _eb), _bHi = Math.max(_sb, _eb);
  const _inBlk = (c) => { if (_sb < 0 || _eb < 0) return true; const b = _blk(c); return b < 0 || (b >= _bLo && b <= _bHi); };  // b<0(空格/无词归属)保留，只排明确别块
  // 拼出选中文本：跨行加 \n；同行按物理 X gap 智能补空格（应对 PDF 数轴等
  // TJ 间隔但无空格 char 的情况，如 '0 1 2 3 4' 在 PyMuPDF rawdict 里没空格 char）
  let text = '';
  let lastChar = null;
  const _cjk = s => /[぀-ヿ㐀-鿿　-〿＀-￯]/.test(s || '');
  for (let i = sIdx; i <= eIdx; i++) {
    const c = chars[i];
    if (!_inBlk(c)) continue;   // 跨块过滤：别块(题号/另一栏)字符不计入选中文本
    if (lastChar) {
      // 两边都是 CJK(日/中,无词间空格)→ 跨行/间隙都直接拼,不插换行或空格(公表する 跨行不再被拆「公 表する」)
      const cjkPair = _cjk(c.c) && _cjk(lastChar.c);
      const dy = Math.abs(c.top - lastChar.top);
      if (dy > c.height * 0.5) {
        if (!cjkPair) text += '\n';
      } else {
        // 同行：按 X gap 判断要不要加空格
        const gap = c.left - (lastChar.left + lastChar.width);
        const ref = Math.min(c.height, lastChar.height);
        if (!cjkPair && gap > ref * 0.6 && !lastChar.sp && !c.sp) text += ' ';   // 0.6 防 justified 词内字距拉伸误拆(如 between→be tween)
      }
    }
    // 真实空格 char 直接保留；其它 char 写入
    text += c.sp ? ' ' : c.c;
    lastChar = c;
  }
  // 压缩多余空格 / trim
  text = text.replace(/[ \t]+/g, ' ').replace(/ ?\n ?/g, '\n').trim();
  lastSelText = text;
  _updateSelPreview(lastSelText);
  if (typeof _updateGrammarBtnVisibility === 'function') _updateGrammarBtnVisibility();
  _charSel = {pw, startIdx: sIdx, endIdx: eIdx, dragging: _charSel?.dragging || false};
  // 高亮：合并同行 chars 成连续矩形（空格按行高估算占位，让单词间高亮连贯）
  const ov = pw.querySelector('.sel-overlay');
  if (ov) {
    ov.innerHTML = '';
    let cur = null;
    for (let i = sIdx; i <= eIdx; i++) {
      const c = chars[i];
      if (!_inBlk(c)) continue;   // 跨块过滤：别块字符不画选中高亮
      // 空格如果 bbox 缺失，用前一字符的位置 + 估算 width
      let cleft = c.left, ctop = c.top, cw = c.width, ch = c.height;
      if (c.sp && (!cw || cw < 0.5) && i > sIdx) {
        const prev = chars[i - 1];
        cleft = prev.left + prev.width;
        ctop = prev.top;
        ch = prev.height;
        cw = ch * 0.3;   // 估算空格宽度
      }
      if (cur && Math.abs(ctop - cur.top) <= ch * 0.4 && cleft <= cur.left + cur.width + ch * 0.5) {
        cur.width = Math.max(cur.left + cur.width, cleft + cw) - cur.left;
        cur.height = Math.max(cur.height, ch);
      } else {
        if (cur) {
          const div = document.createElement('div');
          div.className = 'hl';
          div.style.left = cur.left + 'px';
          div.style.top = cur.top + 'px';
          div.style.width = cur.width + 'px';
          div.style.height = cur.height + 'px';
          ov.appendChild(div);
        }
        cur = {left: cleft, top: ctop, width: cw, height: ch};
      }
    }
    if (cur) {
      const div = document.createElement('div');
      div.className = 'hl';
      div.style.left = cur.left + 'px';
      div.style.top = cur.top + 'px';
      div.style.width = cur.width + 'px';
      div.style.height = cur.height + 'px';
      ov.appendChild(div);
    }
  }
  // 工具栏位置：选区底部
  const pwRect = pw.getBoundingClientRect();
  const mainEl = document.getElementById('main');
  const mainRect = mainEl.getBoundingClientRect();
  const endChar = chars[eIdx];
  toolbar.style.left = Math.max(8, pwRect.left - mainRect.left + mainEl.scrollLeft + chars[sIdx].left) + 'px';
  toolbar.style.top  = (pwRect.top - mainRect.top + mainEl.scrollTop + endChar.top + endChar.height + 6) + 'px';
  toolbar.classList.add('open');
}

// 按 char 扩展到词边界（英文 \w / CJK 逐字）。空格视作非词字符
function _wordExpandFromChar(chars, idx) {
  if (idx < 0 || idx >= chars.length) return null;
  const isWord = (c) => /[A-Za-z0-9_]/.test(c);
  const isCJK  = (c) => /[　-〿぀-ゟ゠-ヿ㐀-䶿一-鿿＀-￯]/.test(c);
  const c = chars[idx].c;
  // 优先用词边界(英语 PyMuPDF + 日语 fugashi)：同一个 w 扩成整词；
  // 之前 CJK 在这里 hardcoded 返回 single,阻止了 fugashi 分词生效。
  if (chars[idx].w !== -1) {
    const wid = chars[idx].w;
    let s = idx, e = idx;
    while (s > 0 && chars[s - 1].w === wid && !'/|／'.includes(chars[s - 1].c)) s--;
    while (e < chars.length - 1 && chars[e + 1].w === wid && !'/|／'.includes(chars[e + 1].c)) e++;
    while (s < e && !isWord(chars[s].c) && !isCJK(chars[s].c)) s++;   // 去词首标点
    while (e > s && !isWord(chars[e].c) && !isCJK(chars[e].c)) e--;   // 去词尾标点(如 often. 的句号)
    return {start: s, end: e};
  }
  // 没词 id 时:CJK 字符无分词信息 → 只选 1 字(避免整页扩),英文按 isWord 扩
  if (isCJK(c)) return {start: idx, end: idx};
  if (!isWord(c)) return {start: idx, end: idx};
  let s = idx;
  while (s > 0 && isWord(chars[s - 1].c) &&
         Math.abs(chars[s - 1].top - chars[idx].top) <= chars[idx].height * 0.5 &&
         (chars[s].left - (chars[s - 1].left + chars[s - 1].width)) <= chars[idx].height * 0.8) {
    s--;   // 大水平间隙=跨排版块(如 Unit 编号↔标题)，不并入同一词
  }
  let e = idx;
  while (e < chars.length - 1 && isWord(chars[e + 1].c) &&
         Math.abs(chars[e + 1].top - chars[idx].top) <= chars[idx].height * 0.5 &&
         (chars[e + 1].left - (chars[e].left + chars[e].width)) <= chars[idx].height * 0.8) {
    e++;   // 大水平间隙=跨排版块，不并入同一词
  }
  return {start: s, end: e};
}

// 同行扩展（双击）—— 含空格
function _lineExpandFromChar(chars, idx) {
  if (idx < 0 || idx >= chars.length) return null;
  const refTop = chars[idx].top;
  const refH = chars[idx].height;
  let s = idx, e = idx;
  while (s > 0 && Math.abs(chars[s - 1].top - refTop) <= refH * 0.4) s--;
  while (e < chars.length - 1 && Math.abs(chars[e + 1].top - refTop) <= refH * 0.4) e++;
  // 去掉两端空格
  while (s < e && chars[s].sp) s++;
  while (e > s && chars[e].sp) e--;
  return {start: s, end: e};
}

// 段扩展（三击）：连续行 + 行间距 < 2.2× 行高
function _paragraphExpandFromChar(chars, idx) {
  if (idx < 0 || idx >= chars.length) return null;
  const refH = chars[idx].height;
  const _bkOf = (c) => (c.bk != null && c.bk >= 0) ? c.bk : -1;
  const _bk0 = _bkOf(chars[idx]);   // 限本块(所在段落)：不跨栏/跨段/跨页，避免上下文吃到整页
  let s = idx, e = idx;
  let curTop = chars[idx].top;
  // 向左/上
  while (s > 0) {
    if (_bk0 >= 0 && _bkOf(chars[s - 1]) >= 0 && _bkOf(chars[s - 1]) !== _bk0) break;
    const t = chars[s - 1].top;
    if (Math.abs(t - curTop) > refH * 2.2) break;
    s--;
    if (Math.abs(t - curTop) > refH * 0.4) curTop = t;
  }
  curTop = chars[idx].top;
  while (e < chars.length - 1) {
    if (_bk0 >= 0 && _bkOf(chars[e + 1]) >= 0 && _bkOf(chars[e + 1]) !== _bk0) break;
    const t = chars[e + 1].top;
    if (Math.abs(t - curTop) > refH * 2.2) break;
    e++;
    if (Math.abs(t - curTop) > refH * 0.4) curTop = t;
  }
  return {start: s, end: e};
}

let _dragStartCharIdx = null, _dragMoved = false, _dragStartXY = null, _fromLBtn = false;
let _dragDir = null;   // 触摸拖动首次动够时锁定:'scroll'(竖直为主→翻页) / 'select'(水平为主→选字)
let _swipeStart = null;   // 单页模式：起点在空白处的横滑 → 翻页（起点在字上仍走拖选）
let _lastClickCharIdx = -1, _lastClickTime = 0, _clickCount = 0;

function _bindCharLayer(cl, pw) {
  const ptToLocal = (clientX, clientY) => {
    const r = cl.getBoundingClientRect();
    return {x: clientX - r.left, y: clientY - r.top};
  };

  // 严格 bbox 命中 + 同行最近 char fallback。
  // 日语扫描书走 visual segmentation 定位字符,char bbox 宽 ≈ 0.78 × spacing →
  // 字符之间有 ~22% 空隙；严格命中空隙时回落"y 在行内 + x 离 char center 最近"防止点了没反应。
  const _findCharStrict = (x, y) => {
    for (let i = 0; i < pw.__charBoxes.length; i++) {
      const c = pw.__charBoxes[i];
      if (c.sp) continue;
      if (x >= c.left && x <= c.left + c.width &&
          y >= c.top  && y <= c.top  + c.height) return i;
    }
    // fallback:y 严格在行高内,取 x 距 char center 最近的(距离阈值 < 1× char height,
    // 避免远处空白也错命中)
    let best = -1, bestDist = Infinity;
    for (let i = 0; i < pw.__charBoxes.length; i++) {
      const c = pw.__charBoxes[i];
      if (c.sp) continue;
      if (y < c.top || y > c.top + c.height) continue;
      const cx = c.left + c.width / 2;
      const d = Math.abs(x - cx);
      if (d < bestDist) { bestDist = d; best = i; }
    }
    if (best >= 0 && bestDist <= (pw.__charBoxes[best].height || 30) * 1.0) return best;
    return -1;
  };
  const onStart = (x, y) => {
    _fromLBtn = false;   // 普通 char-layer 起点（非 L 按钮转发）
    // 诊断：每次按下输出 char-layer rect + 点击位置
    if (!window._loggedRect) {
      const r = cl.getBoundingClientRect();
      window.dlog?.(`cl rect: l=${r.left.toFixed(0)} t=${r.top.toFixed(0)} w=${r.width.toFixed(0)} h=${r.height.toFixed(0)}`);
      window._loggedRect = true;
    }
    window.dlog?.(`tap local: x=${x.toFixed(0)} y=${y.toFixed(0)}`);
    const idx = _findCharStrict(x, y);
    if (idx < 0) {
      _dragStartCharIdx = null;
      // 点空白 → 关 toolbar + 清选区（用户期望取消选中状态）
      toolbar.classList.remove('open');
      lastSelText = '';
      _updateSelPreview('');
      document.querySelectorAll('.sel-overlay').forEach(ov => ov.innerHTML = '');
      return false;
    }
    _dragStartCharIdx = idx;
    _dragStartXY = {x, y};
    _dragMoved = false;
    _dragDir = null;   // 方向未定;首次动够时锁定
    _charSel = {pw, startIdx: idx, endIdx: idx, dragging: true};
    // 拖选期间禁用 vocab-layer 拦截：否则拖到/松手在 L 按钮(pointer-events:auto)上会丢 move/up → 卡住/全选
    const vl = pw.querySelector('.vocab-layer'); if (vl) vl.style.pointerEvents = 'none';
    return true;
  };

  const onMove = (x, y, ev) => {
    if (_dragStartCharIdx == null) return;
    if (_charSel && _charSel.pw !== pw) return;   // 多页/翻页累积的 document 监听：只处理拖选起点页
    if (!_dragStartXY) return;
    const dx = Math.abs(x - _dragStartXY.x), dy = Math.abs(y - _dragStartXY.y);
    if (dx + dy < 8) return;
    // 触摸:首次动够时锁定方向。竖直为主(dy>dx) → 当作翻页/滚动:放弃选字、不拦默认滚动。
    // 鼠标(ev=null)不受此限,竖直拖仍可选。多行选择只要**起手横向**就锁成 select、后续往下拉照常选。
    const isTouch = !!(ev && ev.touches);
    if (isTouch && _dragDir === null) {
      _dragDir = (dy > dx) ? 'scroll' : 'select';
      if (_dragDir === 'scroll') {
        _dragStartCharIdx = null;   // 放弃这次拖选 → 后续 move/end 直接 return,页面正常上下滚
        _charSel = null;
        pw.querySelector('.sel-overlay')?.replaceChildren();
        const vl = pw.querySelector('.vocab-layer'); if (vl) vl.style.pointerEvents = '';
        return;   // 不 preventDefault
      }
    }
    _dragMoved = true;
    if (ev && ev.cancelable) ev.preventDefault();
    const idx = _findCharAt(pw.__charBoxes, x, y);
    if (idx < 0) return;
    _selByCharRange(pw, _dragStartCharIdx, idx);
  };

  const onEnd = (x, y) => {
    if (_dragStartCharIdx == null) return;
    if (_charSel && _charSel.pw !== pw) return;   // 多页/翻页累积的 document 监听：只处理拖选起点页
    const vl = pw.querySelector('.vocab-layer'); if (vl) vl.style.pointerEvents = '';  // 恢复 L 按钮可点
    const startIdx = _dragStartCharIdx;
    _dragStartCharIdx = null;
    if (_dragMoved) {
      const idx = _findCharAt(pw.__charBoxes, x, y);
      if (idx >= 0) _selByCharRange(pw, startIdx, idx);
      // 选中=普通选中(点别处照常消失)。持久呼吸高亮只在点「词组」按钮查询期间出现(showPhrasePopover)
    } else if (_fromLBtn) {
      // 从 L 按钮起点且没拖动 = 单击 L 按钮 → 交给 L 按钮 click 处理整句翻译，这里不查词
      _fromLBtn = false;
    } else {
      // 单/双/三击
      const now = Date.now();
      if (_lastClickCharIdx === startIdx && now - _lastClickTime < 380) {
        _clickCount = (_clickCount % 3) + 1;
      } else {
        _clickCount = 1;
        _lastClickCharIdx = startIdx;
      }
      _lastClickTime = now;
      let bounds = null;
      if (_clickCount === 1) bounds = _wordExpandFromChar(pw.__charBoxes, startIdx);
      else if (_clickCount === 2) bounds = _lineExpandFromChar(pw.__charBoxes, startIdx);
      else bounds = _paragraphExpandFromChar(pw.__charBoxes, startIdx);
      if (bounds) {
        _selByCharRange(pw, bounds.start, bounds.end);
        // 单击单词 → 弹单词小框查词
        if (_clickCount === 1) {
          const _t = (lastSelText || '').trim();
          const _cr = _expandSentenceFromRange(pw.__charBoxes, bounds.start, bounds.end);
          const _ctx = _cr ? _charsRangeToText(pw.__charBoxes, _cr.start, _cr.end).slice(0, 400) : '';
          const hasKana  = s => /[぀-ゟ゠-ヿ]/.test(s);      // 平/片假名 = 铁定日语
          const hasKanji = s => /[一-鿿㐀-䶿]/.test(s);      // CJK 汉字(日中共用,光看词分不出)
          const isEng = /^[A-Za-z][A-Za-z'’\-]*$/.test(_t);
          const declared = BOOK_LANGS.length > 0;
          // 日语判定:优先用本书语言声明;没声明则回退启发(假名铁定/纯汉字看上下文假名)
          let isJa;
          if (declared) {
            isJa = BOOK_LANGS.includes('ja') && (hasKana(_t) || hasKanji(_t));
          } else {
            isJa = hasKana(_t) || (hasKanji(_t) && hasKana(_ctx));
          }
          // 英文:沿用「点击翻译」开关;若声明了语言且没勾英语则不弹
          const engOk = isEng && _clickTranslateEnabled() && (!declared || BOOK_LANGS.includes('en'));
          if (_t && _t.length <= 30 && (isJa || engOk)) {
            setTimeout(() => { try { showWordPopover(_t, _ctx); } catch(_){} }, 30);
          }
        }
      }
    }
  };

  cl.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    if (Date.now() - (window._clLastTouchAt || 0) < 700) return;   // 忽略 touch 后 iOS 合成的 mousedown（否则 onStart 双触发→假双击→刚弹的小框被冲掉）
    e.preventDefault(); e.stopPropagation();   // 阻止旧 document.mousedown 清 toolbar
    const p = ptToLocal(e.clientX, e.clientY);
    onStart(p.x, p.y);
  });
  document.addEventListener('mousemove', (e) => {
    if (_dragStartCharIdx == null) return;
    const p = ptToLocal(e.clientX, e.clientY);
    onMove(p.x, p.y, null);
  });
  document.addEventListener('mouseup', (e) => {
    if (_dragStartCharIdx == null) return;
    const p = ptToLocal(e.clientX, e.clientY);
    onEnd(p.x, p.y);
  });
  cl.addEventListener('touchstart', (e) => {
    if (e.touches.length !== 1) { _dragStartCharIdx = null; _swipeStart = null; return; }
    window._clLastTouchAt = Date.now();   // 标记触摸：后续 iOS 合成 mousedown 忽略
    e.stopPropagation();   // 阻止旧 document.touchstart 清 toolbar
    const t = e.touches[0];
    const p = ptToLocal(t.clientX, t.clientY);
    onStart(p.x, p.y);
    // 单页模式：整页任意处都可横滑翻页（不限空白）。tap=选词 / 横滑=翻页 / 竖滑=滚动；单页不做拖选。
    _swipeStart = (readMode === 'single')
      ? {x: t.clientX, y: t.clientY, lastX: t.clientX, decided: false, h: false}
      : null;
  }, {passive: true});
  cl.addEventListener('touchmove', (e) => {
    if (e.touches.length !== 1) return;
    if (_swipeStart) {   // 单页模式：横滑翻页 / 竖滑滚动 / 移动即放弃选词
      const t = e.touches[0];
      const dx = t.clientX - _swipeStart.x, dy = t.clientY - _swipeStart.y;
      _swipeStart.lastX = t.clientX;
      if (!_swipeStart.decided && (Math.abs(dx) + Math.abs(dy)) > 8) {
        _swipeStart.decided = true;
        _swipeStart.h = Math.abs(dx) > Math.abs(dy);   // 横向主导=翻页，否则=滚动
        // 一旦移动 → 放弃 tap 选词（拖动不在单页里选字）
        _dragStartCharIdx = null; _charSel = null;
        pw.querySelector('.sel-overlay')?.replaceChildren();
        const vl = pw.querySelector('.vocab-layer'); if (vl) vl.style.pointerEvents = '';
        if (!_swipeStart.h) _swipeStart = null;   // 竖滑 → 交回原生滚动，不再拦
      }
      // 横滑：每次 move 都 preventDefault 抢下手势，防浏览器判滚动→touchcancel→翻不了页（F2 根因）
      if (_swipeStart && _swipeStart.h && e.cancelable) e.preventDefault();
      return;
    }
    if (_dragStartCharIdx == null) return;
    e.stopPropagation();
    const t = e.touches[0];
    const p = ptToLocal(t.clientX, t.clientY);
    onMove(p.x, p.y, e);
  }, {passive: false});
  cl.addEventListener('touchend', (e) => {
    // 单页横滑翻页：右滑→上一页，左滑→下一页（阈值 40px）；没构成横滑则落到下面 tap 选词
    if (_swipeStart) {
      const sw = _swipeStart; _swipeStart = null;
      if (sw.h) {
        const t0 = e.changedTouches[0];
        const dx = (t0 ? t0.clientX : sw.lastX) - sw.x;
        if (Math.abs(dx) >= 40) { e.preventDefault(); window.changePage(dx > 0 ? -1 : 1); return; }
        return;   // 横向移动了但不够阈值 → 不翻也不选
      }
    }
    if (_dragStartCharIdx == null) return;
    e.preventDefault(); e.stopPropagation();
    const t = e.changedTouches[0];
    if (!t) return;
    const p = ptToLocal(t.clientX, t.clientY);
    onEnd(p.x, p.y);
  });
  cl.addEventListener('touchcancel', () => { _dragStartCharIdx = null; _swipeStart = null; });
  // 暴露给 vocab L 按钮：从 L 按钮上也能转发拖选（既能点翻译，也能从其上拖选）
  pw.__charDrag = { onStart, onMove, onEnd, ptToLocal };
}

// ──────── 旧 textLayer 事件机制（保留备用，但 char-layer 接管后不会触发）────────
let _lastClickSpan = null, _legacyClickTime = 0, _legacyClickCount = 0;

function _spansInSameLine(targetSpan, textLayerDiv) {
  // 同一行 = Y 中心相差 < 半行高
  const tRect = targetSpan.getBoundingClientRect();
  if (!tRect.height) return [targetSpan];
  const tMid = tRect.top + tRect.height / 2;
  const tol  = tRect.height * 0.5;
  const out = [];
  textLayerDiv.querySelectorAll('span').forEach(s => {
    if (!s.firstChild) return;   // 跳过 marked-content / endOfContent
    const r = s.getBoundingClientRect();
    if (!r.height) return;
    if (Math.abs((r.top + r.height/2) - tMid) <= tol) out.push(s);
  });
  out.sort((a, b) => a.getBoundingClientRect().left - b.getBoundingClientRect().left);
  return out.length ? out : [targetSpan];
}

function _spansInParagraph(targetSpan, textLayerDiv) {
  // 段落 = 当前行向上下扩展直到行间距明显变大 / 没有 spans
  const allSpans = Array.from(textLayerDiv.querySelectorAll('span'))
    .filter(s => s.firstChild && s.getBoundingClientRect().height);
  if (!allSpans.length) return [targetSpan];
  // 按 Y 排序
  const items = allSpans.map(s => {
    const r = s.getBoundingClientRect();
    return {span: s, top: r.top, bot: r.bottom, mid: r.top + r.height/2, h: r.height};
  }).sort((a, b) => a.top - b.top);
  // 分行（Y 中心相近合并）
  const lines = [];
  for (const it of items) {
    const last = lines[lines.length - 1];
    if (last && Math.abs(it.mid - last.midAvg) <= it.h * 0.5) {
      last.items.push(it);
      last.midAvg = (last.midAvg * (last.items.length - 1) + it.mid) / last.items.length;
    } else {
      lines.push({items: [it], midAvg: it.mid, h: it.h});
    }
  }
  // 找 targetSpan 所在行
  let idx = lines.findIndex(L => L.items.some(it => it.span === targetSpan));
  if (idx < 0) return [targetSpan];
  // 向上/下扩展：行间距 < 2.2x avg lineHeight 算同一段
  const avgH = lines[idx].h;
  let lo = idx, hi = idx;
  while (lo > 0) {
    const gap = lines[lo].midAvg - lines[lo-1].midAvg;
    if (gap > avgH * 2.2) break;
    lo--;
  }
  while (hi < lines.length - 1) {
    const gap = lines[hi+1].midAvg - lines[hi].midAvg;
    if (gap > avgH * 2.2) break;
    hi++;
  }
  const out = [];
  for (let i = lo; i <= hi; i++) {
    lines[i].items.sort((a, b) => a.span.getBoundingClientRect().left - b.span.getBoundingClientRect().left);
    out.push(...lines[i].items.map(it => it.span));
  }
  return out;
}

function _selectSpans(spans) {
  if (!spans.length) return;
  const range = document.createRange();
  const firstTxt = spans[0].firstChild;
  const lastTxt  = spans[spans.length - 1].firstChild;
  if (!firstTxt || !lastTxt) return;
  range.setStart(firstTxt, 0);
  range.setEnd(lastTxt, (lastTxt.textContent || '').length);
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
  lastSelText = spans.map(s => s.textContent || '').join('').trim();
  _updateSelPreview(lastSelText);
  // overlay：用 itemBoxes（跟 canvas 渲染对齐）
  document.querySelectorAll('.sel-overlay').forEach(ov => ov.innerHTML = '');
  let pw = spans[0].parentElement;
  while (pw && !pw.classList?.contains('page-wrap')) pw = pw.parentElement;
  if (pw) _paintWithItemBoxes(pw, range);
  // 工具栏位置：用 overlay 内最后一个高亮 div 的位置
  if (pw) {
    const ov = pw.querySelector('.sel-overlay');
    const lastHl = ov?.lastElementChild;
    if (lastHl) {
      const pwRect = pw.getBoundingClientRect();
      const mainEl = document.getElementById('main');
      const mainRect = mainEl.getBoundingClientRect();
      const left = pwRect.left - mainRect.left + mainEl.scrollLeft + parseFloat(lastHl.style.left);
      const top  = pwRect.top  - mainRect.top  + mainEl.scrollTop  + parseFloat(lastHl.style.top) + parseFloat(lastHl.style.height) + 6;
      toolbar.style.left = Math.max(8, left) + 'px';
      toolbar.style.top  = top + 'px';
      toolbar.classList.add('open');
      return;
    }
  }
  const rect = range.getBoundingClientRect();
  const mainEl = document.getElementById('main');
  const mainRect = mainEl.getBoundingClientRect();
  toolbar.style.left = Math.max(8, rect.left - mainRect.left + mainEl.scrollLeft) + 'px';
  toolbar.style.top  = (rect.bottom - mainRect.top + mainEl.scrollTop + 6) + 'px';
  toolbar.classList.add('open');
}

function paintSelectionFromSpans(spans) {
  document.querySelectorAll('.sel-overlay').forEach(ov => ov.innerHTML = '');
  if (!spans.length) return;
  // 找 spans 所在的 page-wrap
  let pw = spans[0].parentElement;
  while (pw && !pw.classList?.contains('page-wrap')) pw = pw.parentElement;
  if (!pw) return;
  const ov = pw.querySelector('.sel-overlay');
  if (!ov) return;
  const pwRect = pw.getBoundingClientRect();
  for (const s of spans) {
    const r = s.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const div = document.createElement('div');
    div.className = 'hl';
    div.style.left   = (r.left - pwRect.left) + 'px';
    div.style.top    = (r.top  - pwRect.top)  + 'px';
    div.style.width  = r.width  + 'px';
    div.style.height = r.height + 'px';
    ov.appendChild(div);
  }
}

// 点击坐标 → span 内字符 offset（用 caret API，兼容新/旧浏览器）
function _spanOffsetFromPoint(span, x, y) {
  const node = span.firstChild;
  if (!node || node.nodeType !== 3) return null;
  // 新 API (Firefox / Chrome 128+)
  if (document.caretPositionFromPoint) {
    const pos = document.caretPositionFromPoint(x, y);
    if (pos && pos.offsetNode === node) return pos.offset;
  }
  // 旧 API (Safari / 老 Chrome)
  if (document.caretRangeFromPoint) {
    const r = document.caretRangeFromPoint(x, y);
    if (r && r.startContainer === node) return r.startOffset;
  }
  return null;
}

// 从 offset 向左/右扩展到词边界
function _wordBoundsAt(text, offset) {
  if (!text) return null;
  // 中日韩字符 / 字母数字下划线 / 部分常用符号算 "词字符"
  const isWord = (c) => /[\w一-鿿぀-ヿ㐀-䶿가-힯]/.test(c);
  let lo = Math.max(0, Math.min(offset, text.length));
  let hi = lo;
  // 点击点不在 word 字符上 → 偏左一格再判
  if (lo < text.length && !isWord(text[lo])) {
    if (lo > 0 && isWord(text[lo - 1])) lo--;
    else return null;   // 点在标点/空白
  }
  while (lo > 0 && isWord(text[lo - 1])) lo--;
  hi = Math.max(hi, offset);
  while (hi < text.length && isWord(text[hi])) hi++;
  if (hi <= lo) return null;
  // 中文逐字选中（每字一"词"）：如果是 CJK 范围，缩小到点击的那一个字
  const cjk = (c) => /[一-鿿㐀-䶿]/.test(c);
  if (offset < text.length && cjk(text[offset])) {
    return {start: offset, end: offset + 1};
  }
  if (offset > 0 && cjk(text[offset - 1])) {
    return {start: offset - 1, end: offset};
  }
  return {start: lo, end: hi};
}

// 高亮整段 textDivs 范围（不画字符级范围 — PDF.js 集成限制让字符级位置不准）
// 用户精确选中的内容看工具栏 preview，这里只显示"涉及哪些 textDiv 段"
function _paintWithItemBoxes(pw, range) {
  const ov = pw.querySelector('.sel-overlay');
  if (!ov) return;
  ov.innerHTML = '';
  if (!pw.__textDivs || !pw.__getSpanIndex) return;
  const startSpan = (range.startContainer.nodeType === 3) ? range.startContainer.parentElement : range.startContainer;
  const endSpan   = (range.endContainer.nodeType === 3) ? range.endContainer.parentElement : range.endContainer;
  let sIdx = pw.__getSpanIndex(startSpan);
  let eIdx = pw.__getSpanIndex(endSpan);
  if (sIdx < 0 || eIdx < 0) return;
  if (sIdx > eIdx) { const t = sIdx; sIdx = eIdx; eIdx = t; }
  const pwRect = pw.getBoundingClientRect();
  for (let i = sIdx; i <= eIdx; i++) {
    const td = pw.__textDivs[i];
    if (!td) continue;
    const r = td.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    const hl = document.createElement('div');
    hl.className = 'hl';
    hl.style.left   = (r.left - pwRect.left) + 'px';
    hl.style.top    = (r.top  - pwRect.top)  + 'px';
    hl.style.width  = r.width  + 'px';
    hl.style.height = r.height + 'px';
    ov.appendChild(hl);
  }
}

// 选中 span 内的子串 [start, end)，画 overlay，浮工具栏
function _selectSpanRange(span, start, end) {
  const node = span.firstChild;
  if (!node) return;
  const range = document.createRange();
  range.setStart(node, start);
  range.setEnd(node, Math.min(end, (node.textContent || '').length));
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
  lastSelText = (node.textContent || '').slice(start, end);
  _updateSelPreview(lastSelText);
  // overlay：用 itemBoxes（PDF.js 原始坐标）
  document.querySelectorAll('.sel-overlay').forEach(ov => ov.innerHTML = '');
  let pw = span.parentElement;
  while (pw && !pw.classList?.contains('page-wrap')) pw = pw.parentElement;
  if (pw) _paintWithItemBoxes(pw, range);
  // 工具栏：item box 算位置（跟 overlay 同坐标系，对齐 PDF）
  if (pw && pw.__itemBoxes && pw.__getSpanIndex) {
    const idx = pw.__getSpanIndex(span);
    const box = idx >= 0 ? pw.__itemBoxes[idx] : null;
    if (box) {
      const pwRect = pw.getBoundingClientRect();
      const mainEl = document.getElementById('main');
      const mainRect = mainEl.getBoundingClientRect();
      const charW = box.w / Math.max(1, box.str.length);
      const left = pwRect.left - mainRect.left + mainEl.scrollLeft + box.x + (start * charW);
      const top  = pwRect.top  - mainRect.top  + mainEl.scrollTop  + box.y + box.h + 6;
      toolbar.style.left = Math.max(8, left) + 'px';
      toolbar.style.top  = top + 'px';
      toolbar.classList.add('open');
      return;
    }
  }
  // fallback：用 range rect
  const rect = range.getBoundingClientRect();
  const mainEl = document.getElementById('main');
  const mainRect = mainEl.getBoundingClientRect();
  toolbar.style.left = Math.max(8, rect.left - mainRect.left + mainEl.scrollLeft) + 'px';
  toolbar.style.top  = (rect.bottom - mainRect.top + mainEl.scrollTop + 6) + 'px';
  toolbar.classList.add('open');
}

// 更新工具栏内 preview 文本（让用户 verify 选中内容；视觉高亮可能跟 canvas 错位时这是 ground truth）
function _updateSelPreview(text) {
  const el = document.getElementById('sel-preview');
  if (!el) return;
  text = (text || '').trim();
  if (!text) { el.textContent = '—'; return; }
  const max = 120;
  const display = text.length > max
    ? text.slice(0, 60) + ' … ' + text.slice(-40)
    : text;
  // 提示用户：虚线框是 textDiv 段范围（粗略），实际选中以下面文字为准
  el.innerHTML = '<b>已选：</b>' + display.replace(/&/g,'&amp;').replace(/</g,'&lt;') +
                 '<span class="len">（' + text.length + ' 字）</span>';
  _updateToolbarMode(text);
}

// 选中后按「单词 vs 多词」切换工具栏按钮组：单词→查词；多词→翻译+解释
function _updateToolbarMode(text) {
  const t = (text || '').trim();
  const isWord = t.length > 0 && t.length <= 30 && /^[A-Za-z][A-Za-z'’\-]*$/.test(t);
  const w = document.getElementById('sel-btns-word');
  const m = document.getElementById('sel-btns-multi');
  if (w) w.style.display = isWord ? 'flex' : 'none';
  if (m) m.style.display = isWord ? 'none' : 'flex';
  // 短词组（F6）：只显示「📘词组」按钮(呼吸提示)。选中高亮本身=普通选中，点别处照常消失；
  // 只有点了「词组」按钮、在查询期间才把选区变持久呼吸高亮(见 showPhrasePopover)。
  const phrase = !isWord && _isShortPhrase(t);
  const pb = document.getElementById('sel-phrase-btn');
  if (pb) { pb.style.display = phrase ? '' : 'none'; pb.classList.toggle('breathe', phrase); }
}

// 短词组判定：中日 2-8 字(无句末标点) / 拉丁 2-5 词且不太长(无句末标点)
function _isShortPhrase(text) {
  const t = (text || '').trim();
  if (!t) return false;
  if (/[。！？、，.!?]$/.test(t)) return false;
  if (/[぀-ヿ㐀-鿿]/.test(t)) {
    return t.length >= 2 && t.length <= 8 && !/[。！？、，.!?]/.test(t);
  }
  const words = t.split(/\s+/).filter(Boolean);
  return words.length >= 2 && words.length <= 5 && t.length <= 40;
}
let _phraseBreatheTimer = null;
function _setSelPhraseBreathe(on) {
  clearTimeout(_phraseBreatheTimer);
  document.querySelectorAll('.sel-overlay').forEach(o => o.classList.toggle('phrase-breathe', on));
  if (on) {   // 呼吸 1.6s 后转常亮（去掉动画类，高亮保持）；按钮也停止呼吸
    _phraseBreatheTimer = setTimeout(() => {
      document.querySelectorAll('.sel-overlay.phrase-breathe').forEach(o => o.classList.remove('phrase-breathe'));
      document.getElementById('sel-phrase-btn')?.classList.remove('breathe');
    }, 1600);
  }
}

// ──────── F6 词组：收藏（作分词依据）+ 词组详情面板 ────────
let _phraseFavSet = new Set();
async function _loadPhraseFavs() {
  try { const d = await (await fetch('/pdf/api/phrases')).json(); if (d.ok) _phraseFavSet = new Set(d.phrases || []); } catch (_) {}
}
window.onPhrase = () => {
  const t = (lastSelText || '').trim();
  if (t) showPhrasePopover(t);
};
// 词组查询期间的呼吸高亮：**只在点「词组」按钮、查询进行中**出现（showPhrasePopover 开始时建、
// 结果出来即移除）。状态驱动(存 pt 坐标到 _activePhraseHl)→ 查询那 1-2s 内即便发生重渲染也不丢、
// 持续呼吸；不受点别处影响(独立层)。平时选中=普通选中(点别处照常消失)，这里不参与。
let _phraseHlTimer = null;
let _activePhraseHl = null;   // {page, text, rects:[[x0,y0,x1,y1]pt...]}
function _charRangeToPtRects(chars, s, e) {
  if (s > e) { const t = s; s = e; e = t; }
  const rects = []; let cur = null;
  for (let i = s; i <= e && i < chars.length; i++) {
    const c = chars[i];
    if (c.sp || c._x0 == null) continue;
    const lh = (c._y1 - c._y0) || 1;
    if (cur && Math.abs(c._y0 - cur[1]) <= lh * 0.6) {
      cur[2] = Math.max(cur[2], c._x1); cur[1] = Math.min(cur[1], c._y0); cur[3] = Math.max(cur[3], c._y1);
    } else { if (cur) rects.push(cur); cur = [c._x0, c._y0, c._x1, c._y1]; }
  }
  if (cur) rects.push(cur);
  return rects;
}
function renderPhraseHl(pw) {
  pw.querySelector('.phrase-hl-layer')?.remove();
  const a = _activePhraseHl;
  if (!a || !a.rects || !a.rects.length) return;
  if (parseInt(pw.dataset.pageNum || '0', 10) !== a.page) return;
  const canvas = pw.querySelector('canvas');
  const cssW = canvas?.clientWidth || pw.clientWidth, cssH = canvas?.clientHeight || pw.clientHeight;
  const pageWPt = pw.__pageWPt || cssW, pageHPt = pw.__pageHPt || cssH;
  if (!cssW || !cssH || !pageWPt || !pageHPt) return;
  const sx = cssW / pageWPt, sy = cssH / pageHPt;
  const layer = document.createElement('div');
  layer.className = 'phrase-hl-layer' + (a.solid ? '' : ' breathe');   // 查询中呼吸；出结果转常亮(a.solid)保持
  for (const r of a.rects) {
    const d = document.createElement('div'); d.className = 'hl';
    d.style.left = (r[0] * sx) + 'px'; d.style.top = (r[1] * sy) + 'px';
    d.style.width = ((r[2] - r[0]) * sx) + 'px'; d.style.height = ((r[3] - r[1]) * sy) + 'px';
    layer.appendChild(d);
  }
  // 点高亮：高亮消失 + 重新弹出该词组的翻译小框（不再建新高亮）
  layer.addEventListener('click', (e) => {
    e.stopPropagation();
    const txt = (_activePhraseHl && _activePhraseHl.text) || a.text;
    _removePhraseHighlight();
    showPhrasePopover(txt, {noHighlight: true});
  });
  pw.appendChild(layer);
}
function _removePhraseHighlight() {
  _activePhraseHl = null;
  clearTimeout(_phraseHlTimer);
  document.querySelectorAll('.phrase-hl-layer').forEach(l => l.remove());
}
function _showPhraseHighlight(pw) {
  if (!pw || !_charSel || !pw.__charBoxes) return null;
  const text = (lastSelText || '').trim();
  if (!text) return null;
  const rects = _charRangeToPtRects(pw.__charBoxes, _charSel.startIdx, _charSel.endIdx);
  if (!rects.length) return null;
  document.querySelectorAll('.phrase-hl-layer').forEach(l => l.remove());   // 清掉别页残留的旧高亮
  _activePhraseHl = {page: parseInt(pw.dataset.pageNum || '0', 10) || currentPage, text, rects, solid: false};
  const sel = pw.querySelector('.sel-overlay'); if (sel) sel.innerHTML = '';   // 移交持久层，避免双重高亮
  renderPhraseHl(pw);
  return true;
}
window.showPhrasePopover = async (text, opts) => {
  const pop = document.getElementById('word-pop');
  toolbar.classList.remove('open');
  _wordPopState = {word: text, ctx: '', lemma: text, phrase: true, reading: ''};
  pop.style.display = 'block';
  window._wordPopOpenAt = Date.now();
  pop.innerHTML = '<div style="padding:14px;color:#8a9bb4">⏳ 处理词组…</div>';
  _positionWordPop(pop);
  // **点了词组按钮**才把当前选区变持久呼吸高亮（查询中呼吸→出结果常亮保持，点高亮才消失）。
  // 点高亮重新弹框时 opts.noHighlight=true → 只弹框、不再建新高亮。
  if (_wordPopState.phrase && !(opts && opts.noHighlight)) _showPhraseHighlight(_charSel && _charSel.pw);
  const esc = s => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
  const isJa = _isJaWord(text);
  let zh = '', reading = '', accent = null;
  try {
    if (isJa) {
      const d = await (await fetch('/pdf/api/dict-jp?word=' + encodeURIComponent(text))).json();
      if (d.ok) { zh = d.zh || ''; reading = d.reading || ''; accent = (d.accent != null ? d.accent : null); }
    }
    if (!zh) {
      const d = await (await fetch('/pdf/api/translate-sentence', {
        method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text}),
      })).json();
      if (d.ok) zh = d.zh || '';
    }
  } catch (_) {}
  // 出结果 → 停呼吸转常亮**保持**（不移除）；只有点高亮本身才消失
  if (_activePhraseHl) {
    _activePhraseHl.solid = true;
    document.querySelector('.phrase-hl-layer')?.classList.remove('breathe');
  }
  _wordPopState.reading = reading;
  const phon = (isJa && reading && accent != null) ? _renderPitch(reading, accent)
    : (reading ? '<span class="wp-phon">' + esc(reading) + '</span>' : '');
  const fav = _phraseFavSet.has(text);
  pop.innerHTML =
    '<div class="wp-head"><span class="wp-word">' + esc(text) + '</span>' + phon +
    (reading ? '<button class="wp-speak" onclick="_speakCurWord()" title="发音">🔊</button>' : '') + '</div>' +
    '<div class="wp-def">' + (zh ? esc(zh) : '<span style="color:#8a9bb4">（无翻译）</span>') + '</div>' +
    '<div class="wp-actions">' +
    '<button id="phrase-fav-btn" class="' + (fav ? 'wp-anki' : '') + '" onclick="_phraseFav(this)">' +
    (fav ? '★ 已收藏' : '☆ 收藏为词组') + '</button>' +
    '<button onclick="onExplain()" title="详细解释这个词组">💡 解释</button>' +
    '</div>';
};
window._phraseFav = (btn) => {
  const s = _wordPopState; if (!s || !s.word) return;
  const t = s.word;
  const has = _phraseFavSet.has(t);
  if (btn) btn.disabled = true;
  fetch('/pdf/api/phrases', {
    method: has ? 'DELETE' : 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text: t}),
  }).then(r => r.json()).then(d => {
    if (d.ok) {
      _phraseFavSet = new Set(d.phrases || []);
      const nowFav = _phraseFavSet.has(t);
      if (btn) { btn.disabled = false; btn.textContent = nowFav ? '★ 已收藏' : '☆ 收藏为词组'; btn.classList.toggle('wp-anki', nowFav); }
      _toast?.(nowFav ? '已收藏，之后会作为一个词分词' : '已取消收藏');
      refreshCharsWForAllPages();   // 让分词合并立即生效
    } else if (btn) { btn.disabled = false; }
  }).catch(() => { if (btn) btn.disabled = false; });
};
// 收藏/取消后重拉各已渲染页 chars 的 w（原地更新，不重建 char-layer）→ 单击立即按新词组边界选
async function refreshCharsWForAllPages() {
  const wraps = document.querySelectorAll('[data-loaded="1"][data-page-num]');
  for (const pw of wraps) {
    const num = parseInt(pw.dataset.pageNum || '0', 10);
    if (!num || !pw.__charBoxes) continue;
    try {
      const d = await (await fetch('/pdf/api/page-chars?file=' + encodeURIComponent(FILE_REL) + '&page=' + num + '&v=' + CHARS_VER)).json();
      if (!d.ok || !d.chars) continue;
      const newW = d.chars.map(c => (c.w == null ? -1 : c.w));
      for (const cb of pw.__charBoxes) { if (cb._oi != null && cb._oi < newW.length) cb.w = newW[cb._oi]; }
      pw.__vocabMarks = d.vocab_marks || pw.__vocabMarks;
      try { renderVocabUnderlines(pw, pw.__vocabMarks); } catch (_) {}
    } catch (_) {}
  }
}

// ── 单词小框：单击单词/点查词 → 先 ecdict 核心(秒回)，可展开完整大框，可主动制卡 ──
let _wordPopState = null;
let _wordPopSeq = 0;   // 查词请求序号:防竞态(快速点不同词,旧词响应晚到覆盖新词)
function _positionWordPop(pop) {
  // 挂进 #main（不会被单页重渲染的 innerHTML='' 删掉）；用 page-wrap 布局坐标定位，
  // absolute 相对 #main → 随内容滚动。（之前挂 page-wrap，侧栏展开触发重渲染会把小框删掉 → 闪没）
  const pw = _charSel && _charSel.pw;
  const ch = pw && pw.__charBoxes && pw.__charBoxes[_charSel.startIdx];
  const main = document.getElementById('main');
  if (pw && ch && ch.left != null && main) {
    if (pop.parentElement !== main) main.appendChild(pop);
    pop.style.position = 'absolute';
    const W = 340;
    let left = pw.offsetLeft + ch.left;   // pw.offsetParent === #main（page-container static）
    const maxLeft = Math.max(4, main.scrollWidth - W - 4);
    if (left > maxLeft) left = maxLeft;
    if (left < 4) left = 4;
    pop.style.left = left + 'px';
    pop.style.top = (pw.offsetTop + ch.top + ch.height + 6) + 'px';
  } else {
    if (main && pop.parentElement !== main) main.appendChild(pop);
    pop.style.position = 'fixed';
    const m = (main || document.body).getBoundingClientRect();
    pop.style.left = (m.left + m.width / 2 - 170) + 'px';
    pop.style.top = (m.top + 70) + 'px';
  }
}
// 框外点击 → 自动关小框（用 pointerdown：原生指针不合成，避免 iOS 合成 mousedown 把刚弹出的框误关）
document.addEventListener('pointerdown', (e) => {
  const p = document.getElementById('word-pop');
  if (!p || p.style.display !== 'block') return;
  if (Date.now() - (window._wordPopOpenAt || 0) < 400) return;   // 刚弹出 400ms 内不关（挡打开那次 tap 的余波）
  if (!p.contains(e.target)) {
    p.style.display = 'none';
    // 词组模式：点别处只藏面板，**保留选中高亮**（用户要求「点击其他内容时这个词的高亮不消失」）。
    // 高亮只在「重新选词 / 点空白」时由 onStart 自然替换或清除；查词加载完会转常亮（独立于面板可见性）。
  }
}, true);
// 画日语声调(ピッチアクセント):读音拆拍 → 高/低 + 下降标记。
// accent: 0=平板(LHHH…不降),1=頭高(HLLL…),N=第N拍后下降(LH…H↓L)
function _renderPitch(reading, accent) {
  const small = 'ぁぃぅぇぉゃゅょゎァィゥェォャュョヮ';
  // 拆拍:小书写假名并入前一拍
  const mora = [];
  for (const ch of reading) {
    if (small.includes(ch) && mora.length) mora[mora.length - 1] += ch;
    else mora.push(ch);
  }
  const n = mora.length;
  if (!n) return '';
  // 每拍高低
  const hi = i => {            // i: 0-based 拍
    if (accent === 0) return i >= 1;          // 平板:第1拍低,其余高
    if (accent === 1) return i === 0;         // 頭高:第1拍高,其余低
    return i >= 1 && i < accent;              // 中高/尾高:2..accent 高
  };
  let html = '<span class="wp-pitch" title="声调型 ' +
    (accent === 0 ? '平板' : accent === 1 ? '頭高' : '第' + accent + '拍后降') + '">';
  for (let i = 0; i < n; i++) {
    const h = hi(i);
    const drop = (accent >= 1 && i + 1 === accent);   // 此拍后下降
    html += '<span class="pm' + (h ? ' hi' : '') + (drop ? ' drop' : '') + '">' + mora[i] + '</span>';
  }
  const tlabel = accent === 0 ? '平板' : accent === 1 ? '頭高' : '['+accent+']';
  html += '<span class="pm-type">' + tlabel + '</span></span>';
  return html;
}

// 日语变形分析 → HTML 行（原形 + 中文语法标签）。word-pop 和完整字典共用。
function _jpInflectHtml(inf, word) {
  if (!inf) return '';
  const esc = s => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
  const showBase = inf.base && inf.base !== (word || '');
  const b = showBase ? '原形 <b>' + esc(inf.base) + '</b>' : '';
  const m = (inf.marks || []).length ? '<span class="jp-inflect-mark">' + inf.marks.map(esc).join('・') + '</span>' : '';
  if (!b && !m) return '';
  return '<div class="jp-inflect">🔀 ' + [b, m].filter(Boolean).join('　') + '</div>';
}
// 英语原型 + 变形 → HTML 行（跟日语变形行同款样式）。clicked=用户点的词；lemma=ECDICT 还原的原型；
// forms=该词的各种屈折(复数/过去式/比较级…)。点的是变形词时显「原型 run」，并列出其余变形 chip。
function _enFormsHtml(lemma, forms, clicked) {
  const esc = s => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
  lemma = (lemma || '').toLowerCase();
  const c = (clicked || '').toLowerCase();
  const fs = [...new Set((forms || []).map(f => String(f || '').toLowerCase()).filter(Boolean))]
    .filter(f => f !== lemma).slice(0, 8);
  const b = (lemma && c && c !== lemma) ? '原型 <b>' + esc(lemma) + '</b>' : '';
  const m = fs.length ? '变形 <span class="jp-inflect-mark">' + fs.map(esc).join('・') + '</span>' : '';
  if (!b && !m) return '';
  return '<div class="jp-inflect">🔀 ' + [b, m].filter(Boolean).join('　') + '</div>';
}
window.showWordPopover = async (word, ctx) => {
  word = (word || '').trim().toLowerCase();
  if (!word) return;
  const myseq = ++_wordPopSeq;   // 本次查词序号;await 回来若已被新查词覆盖则放弃渲染
  toolbar.classList.remove('open');
  _wordPopState = {word, ctx: ctx || '', lemma: word};
  const pop = document.getElementById('word-pop');
  pop.style.display = 'block';
  window._wordPopOpenAt = Date.now();   // 框外关闭监听据此忽略刚弹出时的余波事件
  pop.innerHTML = '<div style="padding:14px;color:#8a9bb4">⏳ 查词中…</div>';
  _positionWordPop(pop);
  try {
    const r = await fetch('/pdf/api/dict-quick?word=' + encodeURIComponent(word) +
      '&file=' + encodeURIComponent(FILE_REL || '') + '&page=' + (currentPage || 0) +
      '&context=' + encodeURIComponent(ctx || '') +
      '&langs=' + encodeURIComponent((BOOK_LANGS || []).join(',')));
    const d = await r.json();
    if (myseq !== _wordPopSeq) return;   // 期间点了别的词 → 这是旧响应,丢弃(防覆盖新词)
    if (!d.ok) { pop.style.display = 'none'; _expandWordFull(word, ctx); return; }   // ecdict 没有 → 直接完整
    _wordPopState.lemma = d.lemma || word;
    _wordPopState.jp = !!d.jp;                // 掌握按钮按语言分流(jp/en 不同 store)
    _wordPopState.reading = (d.jp && d.reading) ? d.reading : '';   // 日语:发音念假名读音(保证读对)
    _wordPopState.mastered = !!d.mastered;   // 掌握开关初始态(日英都返回 mastered)
    const esc = s => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
    const defLines = (d.translation || d.definition || '(无释义)').split('\n').filter(Boolean).slice(0, 3).map(esc).join('<br>');
    // 词性单独做暗色小标签，跟含义用颜色/字号区分（日语：名詞・サ变 等）
    const posTag = (d.pos ? '<span class="wp-pos-tag">' + esc(d.pos) + '</span>' : '');
    // 变形分析：日语=原形+语法标签（过去た/否定ない/て形…）；英语=原型+各种屈折变形
    const inflectHtml = d.jp ? _jpInflectHtml(d.inflect, word) : _enFormsHtml(d.lemma || word, d.forms, word);
    // 日语:画声调曲线(读音+ピッチアクセント);否则普通音标
    const phonHtml = (d.jp && d.reading && d.accent != null)
      ? _renderPitch(d.reading, d.accent)
      : (d.phonetic ? '<span class="wp-phon">' + esc(d.phonetic) + '</span>' : '');
    // 日语母语例句(Tanaka):直接展示在小框;zh 未翻译则回退英文
    let exHtml = '';
    if (d.jp && Array.isArray(d.examples) && d.examples.length) {
      exHtml = '<div class="wp-ex">' + d.examples.slice(0, 2).map(e =>
        '<div class="wp-ex-ja">' + esc(e.ja) + '</div>' +
        '<div class="wp-ex-zh">' + esc(e.zh || e.en || '') + '</div>'
      ).join('') + '</div>';
    }
    pop.innerHTML =
      '<div class="wp-head"><span class="wp-word">' + esc(d.lemma || word) + '</span>' +
      phonHtml +
      '<button class="wp-speak" onclick="_speakCurWord()" title="发音">🔊</button>' +
      (d.freq_bnc ? '<span class="wp-freq">BNC#' + d.freq_bnc + '</span>' : '') + '</div>' +
      inflectHtml +
      '<div class="wp-def" onclick="_expandWordFull()" title="点开看完整释义/例句">' + posTag + defLines +
      exHtml +
      '<div class="wp-more">点这里展开完整字典 ▾</div></div>' +
      '<div class="wp-actions">' +
      // 掌握 toggle:日英统一同一个按钮(onclick 内部按语言分流 store);✓掌握=下划线消失
      '<button id="wp-master-btn" class="' + (d.mastered ? 'wp-anki' : '') + '" onclick="_wordPopMaster(this)" title="' + (d.mastered ? '点击取消掌握（恢复生词下划线）' : '标记掌握 100（下划线消失）') + '">' + (d.mastered ? '✓ 已掌握 100' : '☆ 标记掌握') + '</button>' +
      '<button onclick="_wordPopGrammar()" title="对该词所在整句做语法分析（分词/结构/跟踪知识点）">📊 语法</button>' +
      '</div>';
    // 查过即记入生词库 → 刷新本页下划线（橙=新/黄=见过/淡绿=熟）
    try { refreshVocabUnderlinesForAllPages(); } catch (_) {}
  } catch (e) {
    if (myseq !== _wordPopSeq) return;   // 旧请求出错也不覆盖当前词
    pop.innerHTML = '<div style="padding:14px;color:#c00">查词失败：' + e.message + '</div>';
  }
};
// 判定是否日语词:含假名→是;含汉字时按本书语言声明(声明含 ja 才算,未声明默认按日语,
// 跟后端 dict-quick want_ja 一致)。中文书(只声明 zh)的汉字词不当日语。
function _isJaWord(w) {
  if (/[぀-ヿ]/.test(w)) return true;
  if (!/[㐀-鿿]/.test(w)) return false;
  const declared = (BOOK_LANGS || []).length > 0;
  return declared ? BOOK_LANGS.includes('ja') : true;
}
window._expandWordFull = (w, c) => {
  const s = _wordPopState;
  const word = w || (s && s.word);
  const ctx = (c != null ? c : (s && s.ctx)) || '';
  const pop = document.getElementById('word-pop'); if (pop) pop.style.display = 'none';
  if (!word) return;
  if (_isJaWord(word)) dictStreamJP(word, ctx);   // 日语完整字典(离线富内容+按需AI)
  else dictStream(word, ctx);                     // 英语三源大框(ecdict+free+mw+例句)
};
// 小框喇叭：读当前词（避开 onclick 内联传参的引号冲突），同步播有道(手势栈内)
window._speakCurWord = () => {
  const s = _wordPopState;
  if (!s) return;
  if (s.reading) { _ttsWord(s.reading, 'ja-JP'); return; }   // 日语:直接念假名读音
  const w = s.lemma || s.word;
  if (w) _speakOnline(w);
};
// 小框「掌握」toggle（日英统一）：未掌握 ↔ 掌握 100 来回切，不关框。
// 掌握 → 该词不再标生词下划线；按语言走不同 store：日语 jp-vocab-mark(mastered/unknown)，
// 英语 vocab-mark(known/unknown，写 vocab 笔记 frontmatter.user_mark + 锁 mastery)。
window._wordPopMaster = (btn) => {
  const s = _wordPopState; if (!s) return;
  const w = s.lemma || s.word;
  const next = !s.mastered;
  const url = s.jp ? '/pdf/api/jp-vocab-mark' : '/pdf/api/vocab-mark';
  const mark = next ? 'known' : 'unknown';   // 日英统一口径(jp-vocab-mark 已接受 known/unknown)
  if (btn) btn.disabled = true;
  fetch(url, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({word: w, mark}),
  }).then(r => r.json()).then((d) => {
    if (d && d.ok === false) throw new Error(d.error || 'fail');
    s.mastered = next;
    if (btn) {
      btn.disabled = false;
      btn.textContent = s.mastered ? '✓ 已掌握 100' : '☆ 标记掌握';
      btn.title = s.mastered ? '点击取消掌握（恢复生词下划线）' : '标记掌握 100（下划线消失）';
      btn.classList.toggle('wp-anki', s.mastered);
    }
    try { refreshVocabUnderlinesForAllPages(); } catch (_) {}
    _toast?.(s.mastered ? '已掌握 100，下划线消失' : '已设为未掌握');
  }).catch(() => { if (btn) btn.disabled = false; _toast?.('标记失败'); });
};
// 小框「📊 语法」：对该词所在整句做语法分析（复用 onGrammarAnalyze：当前 _charSel=单词→自动扩成整句）
window._wordPopGrammar = () => {
  const p = document.getElementById('word-pop'); if (p) p.style.display = 'none';
  try { onGrammarAnalyze(); } catch (e) { window.dlog && window.dlog('grammar from word-pop fail: ' + (e && e.message)); }
};
// 工具栏「🔍 查词」：弹单词小框
window.onLookupWord = () => {
  if (!lastSelText) return;
  let ctx = '';
  if (_charSel && _charSel.pw && _charSel.pw.__charBoxes) {
    const chars = _charSel.pw.__charBoxes;
    const cr = _expandSentenceFromRange(chars, _charSel.startIdx, _charSel.endIdx);
    if (cr) ctx = _charsRangeToText(chars, cr.start, cr.end).slice(0, 400);
  }
  showWordPopover(lastSelText, ctx);
};

// 拿点击/触摸位置对应的 (node, offset)，可跨 span
function _caretAtPoint(x, y) {
  if (document.caretPositionFromPoint) {
    const p = document.caretPositionFromPoint(x, y);
    if (p && p.offsetNode) return {node: p.offsetNode, offset: p.offset};
  }
  if (document.caretRangeFromPoint) {
    const r = document.caretRangeFromPoint(x, y);
    if (r) return {node: r.startContainer, offset: r.startOffset};
  }
  return null;
}

// 把 textNode 的 offset 扩展到词边界（英文按 \w 扩展，CJK 按字粒度不变）
function _expandToWordBoundary(node, offset, direction) {
  if (!node || node.nodeType !== 3) return offset;
  const text = node.textContent || '';
  const isWord = (c) => /[A-Za-z0-9_]/.test(c);   // 仅扩展英文/数字（CJK 按字符）
  if (direction === 'start') {
    // 向左扩：如果当前位置在英文词内部，回退到词首
    let i = offset;
    if (i > 0 && i <= text.length && isWord(text[i - 1])) {
      while (i > 0 && isWord(text[i - 1])) i--;
    }
    return i;
  } else {
    // 向右扩：如果当前位置在英文词内部，前进到词尾
    let i = offset;
    if (i < text.length && isWord(text[i])) {
      while (i < text.length && isWord(text[i])) i++;
    }
    return i;
  }
}

// 用 (start, end) 两个 caret 设 selection、画 overlay、浮工具栏
function _applyRangeFromCarets(startC, endC, textLayerDiv) {
  if (!startC || !endC || !startC.node || !endC.node) return false;
  // 限定都在 textLayer 内
  if (!textLayerDiv.contains(startC.node) || !textLayerDiv.contains(endC.node)) return false;
  // 方向修正
  let s = startC, e = endC;
  if (s.node === e.node) {
    if (s.offset > e.offset) [s, e] = [e, s];
  } else {
    const pos = s.node.compareDocumentPosition(e.node);
    if (!(pos & Node.DOCUMENT_POSITION_FOLLOWING)) [s, e] = [e, s];
  }
  // 自动对齐词边界：起点向左、终点向右
  const startOffset = _expandToWordBoundary(s.node, s.offset, 'start');
  const endOffset   = _expandToWordBoundary(e.node, e.offset, 'end');
  const range = document.createRange();
  try {
    range.setStart(s.node, startOffset);
    range.setEnd(e.node, endOffset);
  } catch (_) { return false; }
  if (range.collapsed) return false;
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
  lastSelText = range.toString();
  _updateSelPreview(lastSelText);
  if (!lastSelText.trim()) { toolbar.classList.remove('open'); return false; }
  // overlay：用 PDF.js itemBoxes（PDF 原始坐标，跟 canvas 渲染同源，最准）
  document.querySelectorAll('.sel-overlay').forEach(ov => ov.innerHTML = '');
  let pw = (s.node.nodeType === 3 ? s.node.parentElement : s.node);
  while (pw && !pw.classList?.contains('page-wrap')) pw = pw.parentElement;
  if (pw) _paintWithItemBoxes(pw, range);
  const rect = range.getBoundingClientRect();
  const mainEl = document.getElementById('main');
  const mainRect = mainEl.getBoundingClientRect();
  toolbar.style.left = Math.max(8, rect.left - mainRect.left + mainEl.scrollLeft) + 'px';
  toolbar.style.top  = (rect.bottom - mainRect.top + mainEl.scrollTop + 6) + 'px';
  toolbar.classList.add('open');
  return true;
}

// 旧 textLayer 拖选状态（已被 char-layer 取代，保留声明避免引用报错）
let _dragStart = null;
let _legacyDragMoved = false;
let _dragMoveRaf = null;

function bindTextLayerClick(textLayerDiv) {
  // 单击/双击/三击逻辑（移动距离 < 8px 走这里）
  const handleClick = (span, clientX, clientY) => {
    if (!span || !textLayerDiv.contains(span) || !span.firstChild) return;
    const now = Date.now();
    if (_lastClickSpan === span && now - _lastClickTime < 380) {
      _clickCount = (_clickCount % 3) + 1;
    } else {
      _clickCount = 1;
      _lastClickSpan = span;
    }
    _lastClickTime = now;
    if (_clickCount === 1) {
      const offset = _spanOffsetFromPoint(span, clientX, clientY);
      const text = span.textContent || '';
      if (offset !== null) {
        const wb = _wordBoundsAt(text, offset);
        if (wb) { _selectSpanRange(span, wb.start, wb.end); return; }
      }
      _selectSpans([span]);
    } else if (_clickCount === 2) {
      _selectSpans(_spansInSameLine(span, textLayerDiv));
    } else {
      _selectSpans(_spansInParagraph(span, textLayerDiv));
    }
  };

  // 起始：mousedown / touchstart
  const onStart = (x, y, target) => {
    const span = target?.closest && target.closest('span');
    if (!span || !textLayerDiv.contains(span)) { _dragStart = null; window.dlog?.('START:miss span'); return; }
    const caret = _caretAtPoint(x, y);
    _dragStart = { x, y, caret, span, time: Date.now() };
    _legacyDragMoved = false;
    window.dlog?.('START x=' + Math.round(x) + ' y=' + Math.round(y) + ' caret=' + (caret ? (caret.node?.nodeName + '@' + caret.offset) : 'null'));
  };

  // 移动：mousemove / touchmove —— 实时更新选区
  const onMove = (x, y, ev) => {
    if (!_dragStart) return;
    const dx = Math.abs(x - _dragStart.x);
    const dy = Math.abs(y - _dragStart.y);
    if (dx + dy < 8) return;
    if (!_legacyDragMoved) {
      _legacyDragMoved = true;
      window.dlog?.('MOVE started, dx+dy=' + (dx+dy));
    }
    // touchmove 时 preventDefault 阻止页面滚动（让拖选生效）
    if (ev && ev.cancelable) ev.preventDefault();
    // RAF 节流
    if (_dragMoveRaf) return;
    _dragMoveRaf = requestAnimationFrame(() => {
      _dragMoveRaf = null;
      if (!_dragStart) return;
      const endC = _caretAtPoint(x, y);
      const ok = endC && _applyRangeFromCarets(_dragStart.caret, endC, textLayerDiv);
      if (!ok) window.dlog?.('MOVE caret/apply fail @ ' + Math.round(x)+','+Math.round(y) + ' endNode=' + (endC?.node?.nodeName || 'null'));
    });
  };

  // 结束：mouseup / touchend
  const onEnd = (x, y, target) => {
    if (!_dragStart) return;
    const start = _dragStart;
    _dragStart = null;
    if (_legacyDragMoved) {
      // 拖选：用结束位置 caret + 起始 caret 画范围
      const endC = _caretAtPoint(x, y);
      if (endC) _applyRangeFromCarets(start.caret, endC, textLayerDiv);
    } else {
      // 单击：走单/双/三击逻辑
      handleClick(start.span, x, y);
    }
  };

  // 鼠标
  textLayerDiv.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    onStart(e.clientX, e.clientY, e.target);
  });
  document.addEventListener('mousemove', (e) => onMove(e.clientX, e.clientY, null));
  document.addEventListener('mouseup', (e) => {
    if (!_dragStart) return;
    e.preventDefault();
    onEnd(e.clientX, e.clientY, e.target);
  });

  // 触屏
  textLayerDiv.addEventListener('touchstart', (e) => {
    if (e.touches.length !== 1) { _dragStart = null; return; }
    const t = e.touches[0];
    onStart(t.clientX, t.clientY, e.target);
  }, {passive: true});
  textLayerDiv.addEventListener('touchmove', (e) => {
    if (e.touches.length !== 1) return;
    const t = e.touches[0];
    onMove(t.clientX, t.clientY, e);
  }, {passive: false});   // 必须 passive:false 才能 preventDefault 阻滚动
  textLayerDiv.addEventListener('touchend', (e) => {
    if (!_dragStart) return;
    const t = e.changedTouches[0];
    if (!t) { _dragStart = null; return; }
    e.preventDefault();
    onEnd(t.clientX, t.clientY, e.target);
  });
  textLayerDiv.addEventListener('touchcancel', () => { _dragStart = null; });

  // 屏蔽 textLayer 内的 click 默认行为（避免双击/三击逻辑被 mouseup 已处理后又跑一遍）
  textLayerDiv.addEventListener('click', (e) => { e.preventDefault(); e.stopPropagation(); });
}

function paintSelectionOverlay() {
  // 清空所有 page-wrap 内的 sel-overlay 高亮
  document.querySelectorAll('.sel-overlay').forEach(ov => ov.innerHTML = '');
  const sel = window.getSelection();
  if (!sel.rangeCount) return;
  const rng = sel.getRangeAt(0);
  if (rng.collapsed) return;
  // 拿到选区在 textLayer 内的所有矩形（多行选中会有多个）
  const rects = rng.getClientRects();
  if (!rects.length) return;
  // 找到选区起点所在的 page-wrap（多页选中暂只画当前页）
  let pw = rng.startContainer.parentElement;
  while (pw && !pw.classList?.contains('page-wrap')) pw = pw.parentElement;
  if (!pw) return;
  const ov = pw.querySelector('.sel-overlay');
  if (!ov) return;
  const pwRect = pw.getBoundingClientRect();
  for (const r of rects) {
    if (!r.width || !r.height) continue;
    const div = document.createElement('div');
    div.className = 'hl';
    div.style.left   = (r.left - pwRect.left) + 'px';
    div.style.top    = (r.top  - pwRect.top)  + 'px';
    div.style.width  = r.width  + 'px';
    div.style.height = r.height + 'px';
    ov.appendChild(div);
  }
}

function checkSelection() {
  const sel = window.getSelection();
  const txt = (sel.toString() || '').trim();
  if (!txt || txt.length < 2) {
    // 无原生 selection：char-layer 自定义选中(画在 sel-overlay、不走原生 selection)还在的话别清掉它
    if (!(_charSel && lastSelText)) paintSelectionOverlay();
    return;
  }
  paintSelectionOverlay();
  lastSelText = txt;
    _updateSelPreview(lastSelText);
  try {
    const rng = sel.getRangeAt(0);
    const rect = rng.getBoundingClientRect();
    if (!rect.width && !rect.height) return;
    const mainEl = document.getElementById('main');
    const mainRect = mainEl.getBoundingClientRect();
    toolbar.style.left = Math.max(8, (rect.left - mainRect.left + mainEl.scrollLeft)) + 'px';
    toolbar.style.top  = (rect.bottom - mainRect.top + mainEl.scrollTop + 6) + 'px';
    toolbar.classList.add('open');
  } catch (_) {}
}
function scheduleCheck(delay) {
  clearTimeout(_selTimer);
  _selTimer = setTimeout(checkSelection, delay || 50);
}
document.addEventListener('mouseup', () => scheduleCheck(20));
document.addEventListener('touchend', () => scheduleCheck(250));   // iPad 长按完释放
document.addEventListener('selectionchange', () => scheduleCheck(200));
// 点 toolbar 外 + 既无 native selection 也无 char-layer 选中 + 不在 char-layer 上 → 关 toolbar
function _shouldCloseToolbar(target) {
  if (toolbar.contains(target)) return false;            // 点在 toolbar 内
  if (target?.closest?.('.char-layer')) return false;    // 点在 char-layer 上（char-layer 自己处理）
  const t = (window.getSelection().toString() || '').trim();
  if (t) return false;
  if (lastSelText && lastSelText.trim()) return false;   // char-layer 选中状态
  return true;
}
document.addEventListener('mousedown', (e) => {
  if (_shouldCloseToolbar(e.target)) {
    toolbar.classList.remove('open');
    document.querySelectorAll('.sel-overlay').forEach(ov => ov.innerHTML = '');
    lastSelText = '';
    _updateSelPreview('');
  }
});
document.addEventListener('touchstart', (e) => {
  if (_shouldCloseToolbar(e.target)) {
    toolbar.classList.remove('open');
    document.querySelectorAll('.sel-overlay').forEach(ov => ov.innerHTML = '');
    lastSelText = '';
    _updateSelPreview('');
  }
}, {passive: true});

// ─────────── PDF 高亮：sidecar JSON 持久化 + 渲染 + popover ───────────
const DEFAULT_HL_COLORS = ['#fff59d','#a7f3d0','#a3d4ff','#fda4af'];
function getHlColors() {
  try {
    const arr = JSON.parse(localStorage.getItem('pdf-hl-colors') || 'null');
    return (Array.isArray(arr) && arr.length) ? arr : DEFAULT_HL_COLORS;
  } catch { return DEFAULT_HL_COLORS; }
}
function saveHlColors(arr) {
  localStorage.setItem('pdf-hl-colors', JSON.stringify(arr));
}
let _lastHlColor = localStorage.getItem('pdf-hl-last-color') || DEFAULT_HL_COLORS[0];
let _allHighlights = [];
let _hlByPage = {};
let _resultContext = null;   // {charSel, text, sentence, kind} 由 onTranslate/onExplain 入口存
let _resultReqId = 0;        // 结果框请求序号：每开一次新框 +1，异步回调写入前比对，过期(被新任务覆盖)就丢弃

function escHtml(s) { return String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function _toast(msg) {
  let t = document.getElementById('hl-toast');
  if (!t) {
    t = document.createElement('div');
    t.id = 'hl-toast';
    t.style.cssText = 'position:fixed;left:50%;bottom:30px;transform:translateX(-50%);background:#10162a;border:1px solid #3b6db5;color:#cfe6ff;padding:9px 18px;border-radius:8px;font-size:13px;z-index:500;box-shadow:0 6px 16px rgba(0,0,0,.6);pointer-events:none';
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.style.display = 'block';
  clearTimeout(t._timer);
  t._timer = setTimeout(() => { t.style.display = 'none'; }, 1800);
}

async function loadAllHighlights() {
  try {
    const r = await fetch('/pdf/api/highlights?file=' + encodeURIComponent(FILE_REL));
    const d = await r.json();
    if (!d.ok) return;
    _allHighlights = d.highlights || [];
    _hlByPage = {};
    for (const h of _allHighlights) (_hlByPage[h.page] ||= []).push(h);
    document.querySelectorAll('.page-wrap[data-loaded="1"]').forEach(pw => {
      const n = parseInt(pw.dataset.pageNum || '0');
      if (n) renderHighlightsOnPage(pw, n);
    });
    window.dlog?.('高亮已加载：' + _allHighlights.length + ' 条');
  } catch (e) { window.dlog?.('hl load fail: ' + e.message, '#ff6b6b'); }
}

function renderHighlightsOnPage(pw, pageNum) {
  if (!pw) return;
  let layer = pw.querySelector('.hl-layer');
  if (!layer) {
    layer = document.createElement('div');
    layer.className = 'hl-layer';
  }
  // 永远把 hl-layer append 到最后，让它在 DOM 顺序上晚于 char-layer
  // （配合 z-index:5 双保险）→ hl-saved 在最上层接收点击
  pw.appendChild(layer);
  layer.innerHTML = '';
  const list = _hlByPage[pageNum] || [];
  const canvas = pw.querySelector('canvas');
  const cssW = canvas?.clientWidth || pw.clientWidth;
  const cssH = canvas?.clientHeight || pw.clientHeight;
  if (!cssW || !cssH) return;
  for (const h of list) {
    const pw_pt = h.page_w || pw.__pageWPt || cssW;
    const ph_pt = h.page_h || pw.__pageHPt || cssH;
    const sx = cssW / pw_pt;
    const sy = cssH / ph_pt;
    for (const r of (h.rects || [])) {
      const [x0, y0, x1, y1] = r;
      const div = document.createElement('div');
      const hasColor = !!(h.color && h.color.trim());
      const hasNote  = !!((h.note||'').trim() || (h.body||'').trim() || (h.sentence||'').trim());
      div.className = 'hl-saved'
        + (hasNote ? ' has-note' : '')
        + (hasColor ? '' : ' no-color');
      div.dataset.id = h.id;
      div.style.left = (x0 * sx) + 'px';
      div.style.top = (y0 * sy) + 'px';
      div.style.width = ((x1 - x0) * sx) + 'px';
      div.style.height = ((y1 - y0) * sy) + 'px';
      if (hasColor) div.style.background = h.color;
      div.title = (h.note || h.body || h.sentence || h.text || '').slice(0, 200);
      // 用 capture phase 拦事件，确保不被 char-layer 拖选逻辑捕获
      const stop = (e) => { e.stopPropagation(); };
      div.addEventListener('mousedown',  stop, true);
      div.addEventListener('mouseup',    stop, true);
      div.addEventListener('touchstart', stop, {passive:true, capture:true});
      div.addEventListener('touchend',   stop, {passive:true, capture:true});
      div.addEventListener('click', (e) => {
        e.stopPropagation();
        window.dlog?.('hl-saved click → openHlPopover id=' + h.id);
        openHlPopover(h, div, pw);
      }, true);
      layer.appendChild(div);
    }
  }
}

// 把 chars[s..e] 合并成连续 PDF pt rects（同行合并）
function _charsRangeToRects(chars, sIdx, eIdx) {
  if (sIdx > eIdx) [sIdx, eIdx] = [eIdx, sIdx];
  const rects = [];
  let cur = null;
  for (let i = sIdx; i <= eIdx; i++) {
    const c = chars[i];
    if (c.sp && (!c.width || c.width < 0.5)) {
      if (cur && c._x1 > cur.x1) cur.x1 = c._x1;
      continue;
    }
    const x0 = c._x0, y0 = c._y0, x1 = c._x1, y1 = c._y1;
    const lineH = y1 - y0;
    if (cur &&
        Math.abs(y0 - cur.y0) <= Math.max(2, (cur.y1 - cur.y0) * 0.5) &&
        x0 <= cur.x1 + Math.max(2, lineH * 0.6)) {
      cur.x1 = Math.max(cur.x1, x1);
      cur.y0 = Math.min(cur.y0, y0);
      cur.y1 = Math.max(cur.y1, y1);
    } else {
      if (cur) rects.push([cur.x0, cur.y0, cur.x1, cur.y1]);
      cur = {x0, y0, x1, y1};
    }
  }
  if (cur) rects.push([cur.x0, cur.y0, cur.x1, cur.y1]);
  return rects;
}

async function saveHighlight({pw, sIdx, eIdx, color, kind='note', sentence='', body='', note=''}) {
  if (!pw || !pw.__charBoxes) { alert('请先在 PDF 上选中再标记'); return null; }
  const chars = pw.__charBoxes;
  if (sIdx < 0 || eIdx >= chars.length) return null;
  const rects = _charsRangeToRects(chars, sIdx, eIdx);
  if (!rects.length) return null;
  const pageNum = parseInt(pw.dataset.pageNum || '0');
  const text = _charsRangeToText(chars, Math.min(sIdx,eIdx), Math.max(sIdx,eIdx));
  const payload = {
    file: FILE_REL, page: pageNum, rects, color, text,
    kind, sentence, body, note,
    page_w: pw.__pageWPt, page_h: pw.__pageHPt,
  };
  try {
    const r = await fetch('/pdf/api/highlights', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify(payload),
    });
    const d = await r.json();
    if (!d.ok) { alert('保存高亮失败：' + (d.error || '?')); return null; }
    _allHighlights.push(d.highlight);
    (_hlByPage[pageNum] ||= []).push(d.highlight);
    renderHighlightsOnPage(pw, pageNum);
    _lastHlColor = color;
    localStorage.setItem('pdf-hl-last-color', color);
    return d.highlight;
  } catch (e) {
    alert('保存高亮异常：' + e.message);
    return null;
  }
}

// 当前激活颜色（互斥单选；持久化）。空 = 没激活 → 「🖌 标记」按钮禁用
let _activeHlColor = localStorage.getItem('pdf-hl-active') || '';

// 工具栏色板：只有 [○ ○ ○ ○]；点色 = 立刻标记 + 互斥激活外框（记最近用过的色）
function renderHlPicker() {
  const c = document.getElementById('hl-color-picker');
  if (!c) return;
  c.innerHTML = '';
  for (const col of getHlColors()) {
    const sw = document.createElement('div');
    sw.className = 'swatch' + (col === _activeHlColor ? ' active' : '');
    sw.style.background = col;
    sw.title = '用此色标记';
    sw.onclick = (e) => { e.stopPropagation(); onPickColor(col); };
    c.appendChild(sw);
  }
}
async function onPickColor(col) {
  // 互斥激活：再点同色 = 取消激活；点其他色 = 切到该色（同时立刻标记）
  if (col === _activeHlColor) {
    _activeHlColor = '';
    try { localStorage.setItem('pdf-hl-active', ''); } catch(_) {}
    renderHlPicker();
    return;
  }
  _activeHlColor = col;
  try { localStorage.setItem('pdf-hl-active', col); } catch(_) {}
  renderHlPicker();
  if (!_charSel) { _toast('已选定颜色（先选中文字）'); return; }
  await saveHighlight({
    pw: _charSel.pw, sIdx: _charSel.startIdx, eIdx: _charSel.endIdx,
    color: col, kind: 'note',
  });
  _charSel.pw.querySelector('.sel-overlay')?.replaceChildren();
  _charSel = null;
  lastSelText = '';
  _updateSelPreview('');
  toolbar.classList.remove('open');
  _toast('已标记 🖌');
}

// 从 result-modal「🖌 标记到 PDF」入口：基于 _resultContext 保存
async function markFromResult() {
  if (!_resultContext || !_resultContext.charSel) {
    alert('找不到原始 PDF 选中位置（先在 PDF 上选中再翻译/解释，再点这里）');
    return;
  }
  const md = document.getElementById('result-content');
  const clone = md.cloneNode(true);
  clone.querySelectorAll('.head-pick,.reply-pick-all-result,.pick-btn').forEach(b => b.remove());
  const body = (clone.dataset.raw || clone.textContent || '').trim();
  const cs = _resultContext.charSel;
  const useColor = _activeHlColor || _lastHlColor || (getHlColors()[0] || '#fff59d');
  const h = await saveHighlight({
    pw: cs.pw, sIdx: cs.startIdx, eIdx: cs.endIdx,
    color: useColor,
    kind: _resultContext.kind || 'note',
    sentence: _resultContext.sentence || '',
    body,
  });
  if (h) { closeResult(); _toast('已加标记 🖌'); }
}
window.markFromResult = markFromResult;

// 从 result-modal「🎴 制 Anki」：把「选中原文 + 上下文 + 这条解释」做成 Anki 卡（后台，复用进度条）
async function ankiFromResult() {
  const sel = (_resultContext && _resultContext.text) || lastSelText || '';
  const md = document.getElementById('result-content');
  const clone = md.cloneNode(true);
  clone.querySelectorAll('.head-pick,.reply-pick-all-result,.pick-btn').forEach(b => b.remove());
  const body = (clone.dataset.raw || clone.textContent || '').trim();
  if (!sel && !body) { _toast('没有可制卡的内容'); return; }
  const sentence = (_resultContext && _resultContext.sentence) || '';
  // 原句导航链接：卡片背面可点回到 PDF 原文页
  const srcUrl = FILE_REL ? (location.origin + '/pdf/view?file=' + encodeURIComponent(FILE_REL) + '&page=' + (currentPage || 1)) : '';
  const text = `【原文】${sel}` + (sentence && sentence !== sel ? `\n【上下文】${sentence}` : '') + `\n【解释】${body}` +
               (srcUrl ? `\n【原文出处链接（务必原样放进卡片背面，做成可点链接）】${srcUrl}` : '');
  closeResult();
  const jobUi = _startBgJob('制 Anki 中…');
  try {
    const ov = _getAiOverrides();
    const r = await fetch('/pdf/api/snippets-to-async', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        snippets: [{text, source: sel || sentence}],
        make_note: false, make_anki: true, note_name: '',
        model: ov.model || '', effort: ov.effort || '',
      }),
    });
    const d = await r.json();
    if (!d.ok || !d.job_id) { _failBgJob(jobUi, d.error || '提交失败', null); return; }
    _pollJob(d.job_id, jobUi, null);
  } catch (e) { _failBgJob(jobUi, e.message, null); }
}
window.ankiFromResult = ankiFromResult;

// 解释结果框底部「追问」：基于已有内容继续问 AI，流式追加到同一框（多轮对话）
window._followupAsk = async () => {
  const inp = document.getElementById('result-followup-input');
  const q = (inp.value || '').trim();
  if (!q) return;
  inp.value = '';
  const contentEl = document.getElementById('result-content');
  const history = (contentEl.textContent || '').slice(0, 4000);
  const qDiv = document.createElement('div');
  qDiv.style.cssText = 'margin-top:12px;padding-top:10px;border-top:1px solid #2a3550;color:#a8cdff;font-size:12px;font-weight:600';
  qDiv.textContent = '问：' + q;
  contentEl.appendChild(qDiv);
  const aDiv = document.createElement('div');
  aDiv.style.cssText = 'margin-top:6px;color:#e6e6f0';
  aDiv.innerHTML = '<span class="loading">⏳</span>';
  contentEl.appendChild(aDiv);
  contentEl.scrollTop = contentEl.scrollHeight;
  const myReq = _resultReqId;
  try {
    const ov = _getAiOverrides();
    const render = (text) => {
      if (myReq !== _resultReqId) return;   // 结果框已被新内容作废
      aDiv.innerHTML = md(text || ' ');
      if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([aDiv]).catch(() => {});
      contentEl.scrollTop = contentEl.scrollHeight;
    };
    const res = await _aiStream('/pdf/api/explain', {
      method: 'POST', onText: render,
      body: {text: q, context: '基于之前的解释/对话继续回答：\n' + history, model: ov.model || '', effort: ov.effort || ''},
    });
    if (myReq !== _resultReqId) return;
    if (res.ok && res.text) render(res.text);
    else if (!res.ok) aDiv.innerHTML = '<span style="color:#c00">✗ ' + (res.error || '失败') + '</span>';
    else aDiv.innerHTML = '(无回答)';
  } catch (e) { aDiv.innerHTML = '<span style="color:#c00">✗ ' + e.message + '</span>'; }
  contentEl.scrollTop = contentEl.scrollHeight;
  try { addResultPickers(); } catch (_) {}   // 追问回答也加「+ 选段」，制 Anki(ankiFromResult)含全框选中
};

// ─── 语法分析（per-PDF 启用语法 KG，节点跟踪在技能树页面切换）────────────
let _grammarEnabledBooks = [];   // 本 PDF 启用的 grammar KG list（books）
let _grammarHasTracked = false;  // 是否至少有一个启用书中含 tracked 节点

async function loadGrammarTracked() {
  try {
    const r = await fetch('/pdf/api/grammar-tracked?file=' + encodeURIComponent(FILE_REL));
    const d = await r.json();
    _grammarEnabledBooks = d.enabled_books || [];
  } catch (e) { _grammarEnabledBooks = []; }
  await _refreshGrammarHasTracked();
  _updateGrammarBtnVisibility();
  return _grammarEnabledBooks;
}
async function _refreshGrammarHasTracked() {
  if (!_grammarEnabledBooks.length) { _grammarHasTracked = false; return; }
  try {
    const r = await fetch('/pdf/api/grammar-books');
    const d = await r.json();
    const enabledSet = new Set(_grammarEnabledBooks);
    _grammarHasTracked = (d.books || []).some(b => enabledSet.has(b.book) && (b.tracked_count || 0) > 0);
  } catch (e) { _grammarHasTracked = false; }
}
async function saveGrammarEnabledBooks(books) {
  _grammarEnabledBooks = books;
  try {
    await fetch('/pdf/api/grammar-tracked', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({file: FILE_REL, enabled_books: books}),
    });
  } catch (e) {}
  await _refreshGrammarHasTracked();
  _updateGrammarBtnVisibility();
}
function _updateGrammarBtnVisibility() {
  const row = document.getElementById('grammar-btn-row');
  if (!row) return;
  row.style.display = (_grammarHasTracked && lastSelText) ? '' : 'none';
}
async function renderGrammarTrackList() {
  const wrap = document.getElementById('set-grammar-list');
  if (!wrap) return;
  let books = [];
  try {
    const r = await fetch('/pdf/api/grammar-books');
    const d = await r.json();
    books = d.books || [];
  } catch (e) { books = []; }
  await loadGrammarTracked();
  const enabledSet = new Set(_grammarEnabledBooks);
  if (!books.length) {
    wrap.innerHTML = '<div style="color:#7a8497">还没有语法 KG。新建一本 kind=grammar 的书后会出现。</div>';
    return;
  }
  let html = '';
  for (const b of books) {
    const checked = enabledSet.has(b.book) ? 'checked' : '';
    const hot = (b.tracked_count > 0) ? `<span style="color:#34d399;margin-left:4px">${b.tracked_count} 已跟踪</span>` : `<span style="color:#7a8497;margin-left:4px">无跟踪（去技能树点节点开）</span>`;
    html += `<label style="display:flex;align-items:center;gap:8px;padding:6px 4px;cursor:pointer;color:#cfe6ff;border-radius:3px;border-bottom:1px solid #1f2740" title="共 ${b.total_l2} 个 level-2 语法点">
      <input type="checkbox" value="${b.book}" ${checked} onchange="_onGrammarBookToggle()" style="margin:0">
      <div style="flex:1;min-width:0">
        <div style="font-size:12px">${b.title.replace(/</g,'&lt;')}</div>
        <div style="font-size:10px;color:#7a8497">${b.total_l2} 个语法点 · ${hot}</div>
      </div>
      <a href="/skilltree/${encodeURIComponent(b.book)}/" target="_blank" onclick="event.stopPropagation()" style="color:#60a5fa;font-size:11px;text-decoration:none">技能树 →</a>
    </label>`;
  }
  wrap.innerHTML = html;
}
window._onGrammarBookToggle = function() {
  const books = Array.from(document.querySelectorAll('#set-grammar-list input[type=checkbox]:checked'))
    .map(cb => cb.value);
  window.dlog?.('grammar book toggle → ' + books.join(','));
  saveGrammarEnabledBooks(books);
};

// 选中工具栏的「📊 分析」按钮 → 分析所在完整句子，结果放右侧抽屉
window.onGrammarAnalyze = async () => {
  if (!lastSelText) return;
  if (!_grammarEnabledBooks.length) { _toast?.('请在 PDF 设置中启用至少一个语法 KG'); return; }
  if (!_grammarHasTracked) { _toast?.('已启用的 KG 中没有节点被跟踪，去技能树详情面板点「👁 跟踪」'); return; }
  if (!_charSel || !_charSel.pw || !_charSel.pw.__charBoxes) { _toast?.('找不到选中位置'); return; }
  const pw = _charSel.pw;
  const chars = pw.__charBoxes;
  const ctxRange = _expandSentenceFromRange(chars, _charSel.startIdx, _charSel.endIdx);
  if (!ctxRange) { _toast?.('无法识别完整句子'); return; }
  const sentence = _charsRangeToText(chars, ctxRange.start, ctxRange.end);
  if (sentence.length < 6) { _toast?.('句子太短'); return; }
  const text = lastSelText.trim();   // 用户选中的子串（焦点）
  toolbar.classList.remove('open');
  openGrammarPanel();
  switchSideTab('grammar');   // 分析时切到语法 tab
  const blockId = 'gb_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6);
  const block = _addLoadingBlock(blockId, sentence, text);
  try {
    const r = await fetch('/pdf/api/grammar-analyze', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text, sentence, file: FILE_REL, enabled_books: _grammarEnabledBooks}),
    });
    if (!r.ok) {
      let msg = `HTTP ${r.status}`;
      try { const err = await r.json(); if (err?.error) msg = err.error; } catch {}
      _fillBlockError(block, msg);
      return;
    }
    const d = await r.json();
    if (!d.ok) { _fillBlockError(block, d.error || '?'); return; }
    _fillGrammarBlock(block, d, sentence);          // spaCy 依存图秒出（翻译/语法点先占位）
    if (d.engine === 'spacy') _streamGrammar(block, sentence, text);  // AI 流式补翻译(先)+语法点
  } catch (e) {
    _fillBlockError(block, e.message);
  }
};

// AI 流式：先收翻译（[[TRANS]]..[[/TRANS]] 标志先出 → 立刻显示），再收语法点（[[POINTS]] JSON [[/POINTS]]）
async function _streamGrammar(block, sentence, text) {
  if (!block) return;
  let acc = '', transDone = false, pointsDone = false;
  const tryParse = () => {
    if (!transDone) {
      const tm = acc.match(/\[\[TRANS\]\]([\s\S]*?)\[\[\/TRANS\]\]/);
      if (tm) { _setBlockTrans(block, tm[1].trim()); transDone = true; }
    }
    if (!pointsDone) {
      const pm = acc.match(/\[\[POINTS\]\]([\s\S]*?)\[\[\/POINTS\]\]/);
      if (pm) { _setBlockPoints(block, pm[1].trim()); pointsDone = true; }
    }
  };
  // 抗断连：SSE 主路 + 切后台/网抖回退轮询（后台线程跑完，标志解析照常）
  const res = await _aiStream('/pdf/api/grammar-stream', {
    method: 'POST',
    body: {sentence, text, file: FILE_REL, enabled_books: _grammarEnabledBooks},
    onText: (t) => { acc = t; tryParse(); },
  });
  if (!res.ok && !acc) { _setBlockTransFail(block); return; }
  acc = res.text || acc; tryParse();
  // 兜底：流结束仍未解析到标志，尽量从残文里抠
  if (!transDone) {
    const tm = acc.match(/\[\[TRANS\]\]([\s\S]*?)(\[\[\/TRANS\]\]|\[\[POINTS\]\]|$)/);
    if (tm && tm[1].trim()) _setBlockTrans(block, tm[1].trim());
    else _setBlockTransFail(block);
  }
  if (!pointsDone) {
    const pm = acc.match(/\[\[POINTS\]\]([\s\S]*?)(\[\[\/POINTS\]\]|$)/);
    _setBlockPoints(block, pm ? pm[1].trim() : '[]');
  }
  // 分析完成 → 保存到该 PDF 的历史（按书本持久化）
  try {
    const sp = block.__spacy || {};
    fetch('/pdf/api/grammar-history-save', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({file: FILE_REL, item: {
        sentence, text,
        sentence_zh: block.__zh || '',
        tokens: sp.tokens || [], deps: sp.deps || [], clauses: sp.clauses || [],
        components: sp.components || [], clause_tree: sp.clause_tree || null,
        analyses: block.__points || [],
      }}),
    }).catch(() => {});
  } catch (e) {}
}
function _setBlockTrans(block, zh) {
  // 只填常驻翻译行；标题保持原文不动
  block.__zh = zh;
  const el = block.querySelector('.gb-trans');
  if (el) { el.classList.remove('gb-pending'); el.textContent = '🌐 ' + zh; }
}
function _setBlockTransFail(block) {
  const el = block.querySelector('.gb-trans');
  if (el) { el.classList.remove('gb-pending'); el.textContent = '🌐 （翻译失败，可重试）'; }
}
function _setBlockPoints(block, jsonStr) {
  const wrap = block.querySelector('.gb-analyses');
  if (!wrap) return;
  let arr = [];
  try { arr = JSON.parse(jsonStr) || []; } catch { arr = []; }
  block.__points = arr;   // 存下来供历史保存
  if (!arr.length) { wrap.remove(); return; }   // 没语法点就移除占位
  wrap.classList.remove('gb-pending');
  wrap.innerHTML = arr.map(a => `
    <div class="gb-ana">
      <div class="a-head">📊 ${_esc(a.point || a.node_name || '')}</div>
      ${a.phrase ? `<div class="a-phrase">📍 ${_esc(a.phrase)}</div>` : ''}
      <div class="a-body">${_esc(a.explanation || '')}${(a.examples||[]).length ? `<ul class="a-ex">${(a.examples||[]).map(e=>'<li>'+_esc(e)+'</li>').join('')}</ul>` : ''}</div>
    </div>`).join('');
  wrap.querySelectorAll('.gb-ana').forEach(el => el.addEventListener('click', () => el.classList.toggle('open')));
  // 标题栏角标
  const headEl = block.querySelector('.gb-header');
  if (headEl && !headEl.querySelector('.gb-badge')) {
    const badge = document.createElement('span');
    badge.className = 'gb-badge';
    badge.textContent = '语法点 ' + arr.length;
    headEl.insertBefore(badge, headEl.querySelector('.gb-del'));
  }
}

// ── 抽屉开关（流式堆叠，无左右对齐/滚动同步）──
function openGrammarPanel() {
  const p = document.getElementById('grammar-panel');
  if (!p) return;
  if (!p.classList.contains('open')) {
    p.classList.add('open');
    document.body.classList.add('grammar-open');
    // 打开侧栏让 #main 变窄 → 主动重算 scale 缩放 PDF（iPad Safari ResizeObserver 可能不及时）
    requestAnimationFrame(() => { _refitToWidth(true); });
  }
  // 展开时若停在语法 tab 且历史未载 → 主动载（刷新后默认语法 tab，不点切换也能显示记录）
  const _onGr = document.querySelector('#side-tabs .side-tab[data-pane="grammar"]')?.classList.contains('active');
  if (_onGr && !_grammarHistLoaded && typeof loadGrammarHistory === 'function') loadGrammarHistory();
}
window.closeGrammarPanel = () => {
  document.getElementById('grammar-panel')?.classList.remove('open');
  document.body.classList.remove('grammar-open');
  _hideDepTip();
  requestAnimationFrame(() => { _refitToWidth(true); });
};
// 顶栏「📊 语法」按钮：打开统一面板并切到语法 tab（再点同 tab 则关闭）
window.toggleGrammarPanel = () => {
  const p = document.getElementById('grammar-panel');
  if (!p) return;
  const onGr = document.querySelector('#side-tabs .side-tab[data-pane="grammar"]')?.classList.contains('active');
  if (p.classList.contains('open') && onGr) { closeGrammarPanel(); return; }
  openGrammarPanel();
  switchSideTab('grammar');
};
// 清空侧栏内全部分析卡
window.clearGrammarBlocks = () => {
  document.querySelectorAll('#grammar-panel-body .grammar-block').forEach(b => b.remove());
};

// ── POS 配色 + 中文短标签（displaCy 风格）──
const POS_COLORS = {
  noun:'#3b82f6', verb:'#ef4444', adj:'#22c55e', adv:'#a855f7',
  pron:'#ec4899', prep:'#06b6d4', det:'#64748b', conj:'#eab308',
  aux:'#f97316', num:'#14b8a6', part:'#8b5cf6', intj:'#f43f5e', punct:'#475569',
};
const POS_LABEL = {
  noun:'名', verb:'动', adj:'形', adv:'副', pron:'代', prep:'介',
  det:'限', conj:'连', aux:'助', num:'数', part:'小品', intj:'叹', punct:'标',
};
function _posColor(p){ return POS_COLORS[(p||'').toLowerCase()] || '#64748b'; }
function _esc(s){ return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

// ── 分析块：标题栏（可点击折叠/展开）+ 内容区；流式堆叠，最新插到最上 ──
function _addLoadingBlock(id, sentence, text) {
  const body = document.getElementById('grammar-panel-body');
  const block = document.createElement('div');
  block.className = 'grammar-block focus open';   // loading 时默认展开看进度
  block.id = id;
  const summary = sentence.slice(0, 60) + (sentence.length > 60 ? '…' : '');
  block.dataset.src = sentence;          // 原文，标题永远显示它
  block.dataset.text = text || '';       // 用户选中的焦点子串（删卡清缓存要用）
  block.innerHTML =
    `<div class="gb-header">
       <span class="gb-title" title="${_esc(sentence)}">${_esc(summary)}</span>
       <span class="gb-del" title="删除这条（同句下次重新分析）">🗑</span>
       <span class="gb-caret">▶</span>
     </div>
     <div class="gb-trans gb-pending">🌐 翻译中…</div>
     <div class="gb-content"><div class="gb-loading">⏳ 结构 / 语法分析中…</div></div>
     <div class="gb-fu-answers"></div>
     <div class="gb-followup">
       <input class="gb-fu-input" placeholder="继续追问这句的语法…" onkeydown="if(event.key==='Enter'){event.preventDefault();_grammarFollowup('${id}');}">
       <button onclick="_grammarFollowup('${id}')">追问</button>
       <button class="gb-anki-btn" onclick="_grammarAnki('${id}')" title="整句+译文+分析做成 Anki 卡">🎴</button>
     </div>`;
  block.querySelector('.gb-header').addEventListener('click', () => block.classList.toggle('open'));
  block.querySelector('.gb-del').addEventListener('click', (e) => {
    e.stopPropagation();
    // 真删除：清后端语法分析缓存 + 历史，下次同句从头生成
    fetch('/pdf/api/grammar-forget', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        sentence: block.dataset.src || '', text: block.dataset.text || '',
        file: FILE_REL, enabled_books: _grammarEnabledBooks,
      }),
    }).catch(() => {});
    block.remove();
  });
  // 移除同句旧卡（历史已渲染过的同句，避免重复）
  body.querySelectorAll('.grammar-block').forEach(b => { if (b !== block && b.dataset.src === sentence) b.remove(); });
  body.insertBefore(block, body.firstChild);   // 最新在最上
  body.scrollTop = 0;
  return block;
}
function _fillBlockError(block, msg) {
  if (!block) return;
  const content = block.querySelector('.gb-content') || block;
  content.innerHTML = `<div class="gb-loading" style="color:#ef4444">分析失败：${_esc(msg)}</div>`;
}
// 语法卡片「🎴」：整句 + 译文 + 分析 + 追问 → 一张 Anki 卡（复用后台制卡，带原文出处链接）
window._grammarAnki = async (blockId) => {
  const block = document.getElementById(blockId); if (!block) return;
  const sentence = block.dataset.src || '';
  const zh = (block.querySelector('.gb-trans')?.textContent || '').replace(/^🌐\s*/, '').trim();
  const analysis = (block.querySelector('.gb-content')?.textContent || '').trim();
  const fu = (block.querySelector('.gb-fu-answers')?.textContent || '').trim();
  if (!sentence && !analysis) { _toast && _toast('没有可制卡的内容'); return; }
  const srcUrl = FILE_REL ? (location.origin + '/pdf/view?file=' + encodeURIComponent(FILE_REL) + '&page=' + (currentPage || 1)) : '';
  const text = `【句子】${sentence}` + (zh ? `\n【译文】${zh}` : '') + (analysis ? `\n【语法分析】${analysis}` : '')
    + (fu ? `\n【追问】${fu}` : '') + (srcUrl ? `\n【原文出处链接（务必原样放进卡片背面，做成可点链接）】${srcUrl}` : '');
  const jobUi = _startBgJob('制 Anki 中…');
  try {
    const ov = _getAiOverrides();
    const r = await fetch('/pdf/api/snippets-to-async', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ snippets: [{text, source: sentence}], make_note: false, make_anki: true, note_name: '', model: ov.model || '', effort: ov.effort || '' }),
    });
    const d = await r.json();
    if (!d.ok || !d.job_id) { _failBgJob(jobUi, d.error || '提交失败', null); return; }
    _pollJob(d.job_id, jobUi, null);
  } catch (e) { _failBgJob(jobUi, e.message, null); }
};
// 语法卡片底部「继续追问」：带原句 + 译文 + 已有分析作上下文，流式回答追加到卡片内
window._grammarFollowup = async (blockId) => {
  const block = document.getElementById(blockId); if (!block) return;
  const inp = block.querySelector('.gb-fu-input');
  const q = (inp && inp.value || '').trim(); if (!q) return;
  inp.value = '';
  const sentence = block.dataset.src || '';
  const trans = (block.querySelector('.gb-trans')?.textContent || '').replace(/^🌐\s*/, '').trim();
  const analysis = (block.querySelector('.gb-content')?.textContent || '').slice(0, 3000);
  const prev = (block.querySelector('.gb-fu-answers')?.textContent || '').slice(-1500);
  const context = '【句子】' + sentence + (trans ? '\n【译文】' + trans : '')
    + '\n【已有语法分析】\n' + analysis + (prev ? '\n【之前的追问】\n' + prev : '');
  const ans = block.querySelector('.gb-fu-answers');
  const qDiv = document.createElement('div'); qDiv.className = 'gb-fu-q'; qDiv.textContent = '问：' + q; ans.appendChild(qDiv);
  const aDiv = document.createElement('div'); aDiv.className = 'gb-fu-a'; aDiv.innerHTML = '<span class="gb-loading">⏳</span>'; ans.appendChild(aDiv);
  try {
    const ov = _getAiOverrides();
    const r = await fetch('/pdf/api/explain', {
      method: 'POST', headers: {'Content-Type': 'application/json', 'Accept': 'text/event-stream'},
      body: JSON.stringify({text: q, context: '基于这句的语法分析继续回答：\n' + context, model: ov.model || '', effort: ov.effort || ''}),
    });
    const ct = r.headers.get('content-type') || '';
    if (ct.includes('event-stream')) {
      const reader = r.body.getReader(); const dec = new TextDecoder();
      let buf = '', acc = '';
      while (true) {
        const {value, done} = await reader.read(); if (done) break;
        buf += dec.decode(value, {stream: true}); let i;
        while ((i = buf.indexOf('\n\n')) !== -1) {
          const blk = buf.slice(0, i); buf = buf.slice(i + 2); let dl = '';
          for (const ln of blk.split('\n')) { if (ln.startsWith('data:')) dl = ln.slice(5).trim(); }
          if (!dl) continue;
          try { const o = JSON.parse(dl); if (o.text) { acc += o.text; aDiv.innerHTML = md(acc); if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([aDiv]).catch(()=>{}); } } catch (_) {}
        }
      }
      if (!acc) aDiv.innerHTML = '(无回答)';
    } else {
      const d = await r.json(); aDiv.innerHTML = md(d.explanation || d.text || '(无回答)');
    }
  } catch (e) { aDiv.innerHTML = '追问失败：' + _esc(e.message); }
};
function _fillGrammarBlock(block, d, sentence) {
  if (!block) return;
  const tokens = d.tokens || [];
  const deps = d.deps || [];
  const sentence_zh = d.sentence_zh || '';
  const analyses = d.analyses || [];
  const components = d.components || [];
  if (!tokens.length && !sentence_zh && !analyses.length && !components.length) {
    _fillBlockError(block, d.raw ? 'AI 未返回结构化结果' : '无结果');
    return;
  }
  block.__spacy = {tokens, deps, clauses: d.clauses || [], components, clause_tree: d.clause_tree};   // 存供历史保存
  const isSpacy = d.engine === 'spacy';   // spaCy 路径：翻译/语法点稍后由 SSE 流式填
  // 标题永远是原文（不被翻译覆盖）；翻译填到 header 外的常驻 gb-trans 行（折叠也可见）
  const transEl = block.querySelector('.gb-trans');
  if (transEl) {
    if (sentence_zh) { transEl.classList.remove('gb-pending'); transEl.textContent = '🌐 ' + sentence_zh; }
    else if (!isSpacy) { transEl.remove(); }   // AI 兜底路径但无翻译 → 移除占位；spaCy 保持「翻译中」等 SSE
  }
  const headEl = block.querySelector('.gb-header');
  if (headEl && analyses.length && !headEl.querySelector('.gb-badge')) {
    const badge = document.createElement('span');
    badge.className = 'gb-badge';
    badge.textContent = '语法点 ' + analyses.length;
    headEl.insertBefore(badge, headEl.querySelector('.gb-del'));
  }
  // 句子成分分块（主谓宾定状从句彩色块）；无 components 时回退依存图
  const hasStruct = components.length || tokens.length;
  const gvHtml = GV_MODES.map(([m, l]) => `<button type="button" data-gv="${m}" class="${m === _grammarViewMode ? 'active' : ''}">${l}</button>`).join('');
  const diagramHtml = hasStruct
    ? `<div class="gb-diagram-wrap"><div class="gb-diagram-toggle">📐 句子结构<span class="gv-switch">${gvHtml}</span><span class="dg-caret">▶</span></div><div class="gb-diagram"></div></div>`
    : '';
  // 语法点区：AI 路径直接渲染；spaCy 路径先占位等 SSE
  const anaHtml = analyses.length
    ? `<div class="gb-analyses">${analyses.map((a,i)=>`
        <div class="gb-ana" data-i="${i}">
          <div class="a-head">📊 ${_esc(a.node_name||a.node_id||'')}</div>
          ${a.phrase?`<div class="a-phrase">📍 ${_esc(a.phrase)}</div>`:''}
          <div class="a-body">${_esc(a.explanation||'')}${(a.examples||[]).length?`<ul class="a-ex">${(a.examples||[]).map(e=>'<li>'+_esc(e)+'</li>').join('')}</ul>`:''}</div>
        </div>`).join('')}</div>`
    : (isSpacy ? `<div class="gb-analyses gb-pending"><div class="gb-ana"><div class="a-head" style="color:#7a8497;font-weight:400">⏳ 语法点分析中…</div></div></div>` : '');
  const content = block.querySelector('.gb-content');
  content.innerHTML = diagramHtml + anaHtml;
  if (hasStruct) {
    const wrap = content.querySelector('.gb-diagram-wrap');
    const diag = content.querySelector('.gb-diagram');
    _renderStructInto(diag, wrap, {tokens, deps, clauses: d.clauses || [], components});
    content.querySelector('.gb-diagram-toggle').addEventListener('click', () => wrap.classList.toggle('dgram-open'));
    // 标题栏内的模式切换按钮（树/块/干/弧），点击切全局模式、不触发折叠
    content.querySelectorAll('.gv-switch button').forEach(btn => btn.addEventListener('click', (e) => {
      e.stopPropagation();
      setGrammarView(btn.dataset.gv);
    }));
  }
  content.querySelectorAll('.gb-ana').forEach(el => el.addEventListener('click', () => el.classList.toggle('open')));
  // 刚分析的块保持展开 + 高亮 5s，之后取消高亮（仍保持用户的展开/折叠状态）
  setTimeout(() => block.classList.remove('focus'), 5000);
}

// 长句结构显示模式：'components' 成分分块 / 'deps' 依存弧线图
const GV_MODES = [['tree', '树'], ['components', '块'], ['skeleton', '主干'], ['deps', '弧线']];
let _grammarViewMode = localStorage.getItem('pdf-grammar-view') || 'components';
window.setGrammarView = (mode) => {
  _grammarViewMode = ['deps', 'skeleton', 'components', 'tree'].includes(mode) ? mode : 'components';
  try { localStorage.setItem('pdf-grammar-view', _grammarViewMode); } catch (_) {}
  // 立即用新模式重渲染已显示的所有语法卡的结构区
  document.querySelectorAll('#grammar-panel-body .grammar-block').forEach(b => {
    if (!b.__spacy) return;
    const diag = b.querySelector('.gb-diagram'), wrap = b.querySelector('.gb-diagram-wrap');
    if (diag) _renderStructInto(diag, wrap, b.__spacy);
  });
  // 同步所有卡标题栏的模式按钮高亮 + 设置面板下拉
  document.querySelectorAll('.gv-switch button').forEach(b => b.classList.toggle('active', b.dataset.gv === _grammarViewMode));
  const sel = document.getElementById('set-grammar-view');
  if (sel) sel.value = _grammarViewMode;
};
// 统一结构渲染：按 _grammarViewMode 选 成分分块 / 主干折叠 / 依存图
function _renderStructInto(diag, wrap, sp) {
  diag.innerHTML = '';
  const comps = sp.components || [], toks = sp.tokens || [], deps = sp.deps || [], clauses = sp.clauses || [];
  if (_grammarViewMode === 'tree' && comps.length) {
    diag.appendChild(_renderTree(comps)); wrap?.classList.add('dgram-open');
  } else if (_grammarViewMode === 'skeleton' && comps.length) {
    diag.appendChild(_renderSkeleton(comps)); wrap?.classList.add('dgram-open');
  } else if (_grammarViewMode === 'components' && comps.length) {
    diag.appendChild(_renderComponents(comps)); wrap?.classList.add('dgram-open');
  } else if (sp.clause_tree) {
    // 可逐级展开的弧线：主句弧线 + 占位节点点开看从句弧线
    diag.appendChild(_renderDepTree(sp.clause_tree));
  } else if (toks.length) {
    // 旧数据(无 clause_tree)回退：整句单图 / 从句平铺分段
    if (clauses.length > 1) {
      for (const c of clauses) {
        if (!(c.tokens || []).length) continue;
        const seg = document.createElement('div');
        seg.className = 'dep-clause';
        const lbl = document.createElement('div');
        lbl.className = 'dep-clause-label';
        lbl.textContent = c.label || '从句';
        seg.appendChild(lbl);
        seg.appendChild(_renderDepSvg(c.tokens, c.deps || []));
        diag.appendChild(seg);
      }
    } else {
      diag.appendChild(_renderDepSvg(toks, deps));
    }
    diag.addEventListener('wheel', (e) => {
      if (diag.scrollWidth <= diag.clientWidth) return;
      const dy = Math.abs(e.deltaY) > Math.abs(e.deltaX) ? e.deltaY : e.deltaX;
      diag.scrollLeft += dy; e.preventDefault();
    }, {passive: false});
  }
}
// 主干+修饰折叠：核心成分(主谓宾表)行内显示，修饰/从句折叠成可点 chip
function _renderSkeleton(comps) {
  const CORE = new Set(['主语', '谓语', '宾语', '间接宾语', '表语', '宾语补足语', '形式主语']);
  const wrap = document.createElement('div');
  wrap.className = 'sk-blocks';
  for (const c of comps) {
    const label = c.label || '';
    if (CORE.has(label)) {
      const s = document.createElement('span');
      s.className = 'sk-core';
      s.textContent = c.text || '';
      wrap.appendChild(s);
    } else {
      const chip = document.createElement('span');
      chip.className = 'sk-mod';
      chip.style.color = _compColor(label);
      chip.dataset.text = c.text || '';
      chip.dataset.label = label;
      chip.textContent = '[' + label + ' …]';
      chip.addEventListener('click', () => {
        const open = chip.classList.toggle('open');
        chip.textContent = open ? ('[' + label + '：' + chip.dataset.text + ']') : ('[' + label + ' …]');
      });
      wrap.appendChild(chip);
    }
    wrap.appendChild(document.createTextNode(' '));
  }
  return wrap;
}

// ── 成分树（融合）：折叠看整句、点开逐级展开成分细节 ──
//   折叠态：显示该成分的「整体文本」(自己+所有后代按位置拼) + 成分标签
//   展开态：显示「自有核心」+ 缩进的下级成分(各自又可再展开)
function _renderTree(comps) {
  const children = {};
  comps.forEach((c, i) => {
    const p = (c.parent == null ? -1 : c.parent);
    (children[p] = children[p] || []).push(i);
  });
  const fullText = (idx) => {   // idx 及其所有后代的自有文本，按出现位置拼成整句片段
    const acc = [];
    const collect = (i) => { acc.push(comps[i]); (children[i] || []).forEach(collect); };
    collect(idx);
    acc.sort((a, b) => (a.start || 0) - (b.start || 0));
    return acc.map(c => c.text || '').join(' ');
  };
  const build = (idx) => {
    const c = comps[idx], col = _compColor(c.label || '');
    const kids = children[idx] || [];
    const node = document.createElement('div');
    node.className = 'ctree-node';
    const row = document.createElement('div');
    row.className = 'ct-row';
    const txt = document.createElement('span');
    txt.className = 'ct-text';
    txt.textContent = kids.length ? fullText(idx) : (c.text || '');   // 有子→折叠显示整体
    row.innerHTML = `<span class="ct-label" style="background:${col}">${_esc(c.label || '')}</span>`;
    row.appendChild(txt);
    node.appendChild(row);
    const isClause = (c.label || '').includes('从句');
    if (kids.length) {
      const car = document.createElement('span');
      car.className = 'ct-caret'; car.textContent = '▸';   // 默认折叠
      row.appendChild(car);
      const box = document.createElement('div');
      box.className = 'ct-children';
      box.style.display = 'none';
      // 从句类节点：展开后把从句谓语(自有核心)作为「谓语」子项，与主/宾/状平级
      if (isClause && (c.text || '').trim()) {
        const pred = document.createElement('div');
        pred.className = 'ctree-node';
        pred.innerHTML = `<div class="ct-row"><span class="ct-label" style="background:${_compColor('谓语')}">谓语</span><span class="ct-text">${_esc(c.text)}</span></div>`;
        box.appendChild(pred);
      }
      kids.forEach(k => box.appendChild(build(k)));
      node.appendChild(box);
      const toggle = (e) => {
        e.stopPropagation();
        const open = box.style.display === 'none';
        box.style.display = open ? '' : 'none';
        car.textContent = open ? '▾' : '▸';
        // 从句类展开后自身只留标签(谓语已挪进子)；短语类展开看核心
        txt.textContent = open ? (isClause ? '' : (c.text || '')) : fullText(idx);
        txt.classList.toggle('ct-core', open && !isClause);
      };
      car.addEventListener('click', toggle);
      txt.style.cursor = 'pointer';
      txt.addEventListener('click', toggle);
    }
    return node;
  };
  const wrap = document.createElement('div');
  wrap.className = 'ctree';
  (children[-1] || []).forEach(i => wrap.appendChild(build(i)));
  return wrap;
}

// ── 句子成分分块：主谓宾定状从句彩色块（无弧线、可换行，长句清晰）──
function _compColor(label) {
  if (label.includes('谓语')) return '#ef4444';
  if (label.includes('主语')) return '#3b82f6';
  if (label.includes('宾语')) return '#22c55e';
  if (label.includes('定语')) return '#06b6d4';
  if (label.includes('状语')) return '#a855f7';
  if (label.includes('表语') || label.includes('补语')) return '#eab308';
  if (label.includes('并列')) return '#94a3b8';
  return '#8a9bb4';
}
function _renderComponents(comps) {
  const wrap = document.createElement('div');
  wrap.className = 'comp-blocks';
  for (const c of comps) {
    const col = _compColor(c.label || '');
    const b = document.createElement('div');
    b.className = 'comp-block';
    b.style.borderLeftColor = col;
    b.innerHTML = `<span class="comp-label" style="background:${col}">${_esc(c.label || '')}</span><span class="comp-text">${_esc(c.text || '')}</span>`;
    wrap.appendChild(b);
  }
  return wrap;
}

// ── displaCy 依存图：弧线在词上方、词性着色、词性中文标在词下（无 components 时回退）──
function _renderDepSvg(tokens, deps, onNodeClick) {
  const NS = 'http://www.w3.org/2000/svg';
  const PAD_X = 18, GAP = 40, FONT = 16, POS_GAP = 18;   // 词距/字号加大，更舒展不挤
  const ARC_BASE = 24, ARC_STEP = 19;
  // 量词宽
  const meas = document.createElement('canvas').getContext('2d');
  meas.font = `600 ${FONT}px -apple-system,system-ui,sans-serif`;
  const widths = tokens.map(t => Math.max(meas.measureText(t.text||'').width, 12));
  // 词中心 x
  const cx = []; let x = PAD_X;
  for (let i=0;i<tokens.length;i++){ cx.push(x + widths[i]/2); x += widths[i] + GAP; }
  const totalW = Math.max(x - GAP + PAD_X, 60);
  // 弧线按跨度排序（短弧靠下）
  const arcs = deps.map(dp => ({...dp, span: Math.abs(dp.head - dp.child)})).sort((a,b)=>a.span-b.span);
  const maxSpan = arcs.length ? Math.max(...arcs.map(a=>a.span)) : 1;
  const wordBaseY = ARC_BASE + maxSpan*ARC_STEP + 26;
  const svgH = wordBaseY + POS_GAP + 8;
  const svg = document.createElementNS(NS,'svg');
  svg.setAttribute('width', totalW);
  svg.setAttribute('height', svgH);
  svg.setAttribute('viewBox', `0 0 ${totalW} ${svgH}`);
  const arcBottomY = wordBaseY - FONT - 4;   // 弧线落脚（词上边）
  for (const a of arcs) {
    const x1 = cx[a.head], x2 = cx[a.child];
    const dir = x2 > x1 ? 1 : -1;
    const top = arcBottomY - (ARC_BASE + a.span*ARC_STEP);
    const sx = x1 + dir*3, ex = x2 - dir*3;
    const path = document.createElementNS(NS,'path');
    path.setAttribute('d', `M ${sx} ${arcBottomY} C ${sx} ${top}, ${ex} ${top}, ${ex} ${arcBottomY}`);
    path.setAttribute('class','dep-arc');
    svg.appendChild(path);
    // 箭头指向 child（ex 端）
    const arrow = document.createElementNS(NS,'path');
    arrow.setAttribute('d', `M ${ex-3} ${arcBottomY-5} L ${ex+3} ${arcBottomY-5} L ${ex} ${arcBottomY} Z`);
    arrow.setAttribute('class','dep-arrow');
    svg.appendChild(arrow);
    // 关系标签
    if (a.label) {
      const lbl = document.createElementNS(NS,'text');
      lbl.setAttribute('x',(sx+ex)/2); lbl.setAttribute('y', top-2);
      lbl.setAttribute('text-anchor','middle'); lbl.setAttribute('class','dep-label');
      lbl.textContent = a.label;
      svg.appendChild(lbl);
    }
  }
  for (let i=0;i<tokens.length;i++){
    const t = tokens[i];
    const isClause = (t.pos === 'clause') || (t.ref != null);   // 子从句占位节点
    const col = isClause ? '#7dd3fc' : _posColor(t.pos);
    const g = document.createElementNS(NS,'g');
    g.setAttribute('class', 'dep-token' + (isClause ? ' dep-ph' : ''));
    const bw = widths[i] + 8;
    const bg = document.createElementNS(NS,'rect');
    bg.setAttribute('x', cx[i]-bw/2); bg.setAttribute('y', wordBaseY-FONT);
    bg.setAttribute('width', bw); bg.setAttribute('height', FONT+5);
    bg.setAttribute('rx',4); bg.setAttribute('fill', col); bg.setAttribute('opacity', isClause ? '0.22' : '0.16');
    if (isClause) { bg.setAttribute('stroke', col); bg.setAttribute('stroke-dasharray','3 2'); bg.setAttribute('stroke-width','1'); }
    g.appendChild(bg);
    const w = document.createElementNS(NS,'text');
    w.setAttribute('x', cx[i]); w.setAttribute('y', wordBaseY-2);
    w.setAttribute('text-anchor','middle'); w.setAttribute('class','dep-word');
    w.setAttribute('fill', col);
    w.textContent = t.text || '';
    g.appendChild(w);
    const p = document.createElementNS(NS,'text');
    p.setAttribute('x', cx[i]); p.setAttribute('y', wordBaseY+POS_GAP-2);
    p.setAttribute('text-anchor','middle'); p.setAttribute('class','dep-pos');
    p.textContent = isClause ? '点开▾' : (POS_LABEL[(t.pos||'').toLowerCase()] || t.pos || '');
    g.appendChild(p);
    if (isClause && onNodeClick) {
      g.style.cursor = 'pointer';
      g.addEventListener('click', ev => { ev.stopPropagation(); onNodeClick(i, g); });
    } else if (t.zh) {
      const tip = `${t.text}　${t.zh}`;
      g.addEventListener('mouseenter', ev => _showDepTip(ev, tip));
      g.addEventListener('mousemove', _moveDepTip);
      g.addEventListener('mouseleave', _hideDepTip);
      g.addEventListener('click', ev => { _showDepTip(ev, tip); setTimeout(_hideDepTip, 2600); });
    }
    svg.appendChild(g);
  }
  return svg;
}

// 可逐级展开的弧线树：主句弧线图 + 占位节点点开展开子从句弧线（递归）
function _renderDepTree(tree) {
  const wrap = document.createElement('div');
  wrap.className = 'dep-tree';
  const renderClause = (clause, container) => {
    const seg = document.createElement('div');
    seg.className = 'dep-tlevel';
    const childBox = document.createElement('div');
    childBox.className = 'dep-tchildren';
    const svg = _renderDepSvg(clause.nodes || [], clause.deps || [], (i) => {
      const node = (clause.nodes || [])[i];
      if (!node || node.ref == null) return;
      const exist = childBox.querySelector(`:scope > [data-ref="${node.ref}"]`);
      if (exist) { exist.remove(); return; }   // 再点收起
      const sub = document.createElement('div');
      sub.dataset.ref = node.ref;
      sub.className = 'dep-tsub';
      const lbl = document.createElement('div');
      lbl.className = 'dep-clause-label';
      lbl.textContent = (clause.children[node.ref] || {}).label || '从句';
      sub.appendChild(lbl);
      renderClause(clause.children[node.ref], sub);
      childBox.appendChild(sub);
    });
    // 图比容器宽时横向滚
    const sc = document.createElement('div');
    sc.className = 'dep-tscroll';
    sc.appendChild(svg);
    sc.addEventListener('wheel', (e) => {
      if (sc.scrollWidth <= sc.clientWidth) return;
      sc.scrollLeft += (Math.abs(e.deltaY) > Math.abs(e.deltaX) ? e.deltaY : e.deltaX);
      e.preventDefault();
    }, {passive: false});
    seg.appendChild(sc);
    seg.appendChild(childBox);
    container.appendChild(seg);
  };
  renderClause(tree, wrap);
  return wrap;
}

let _depTipEl = null;
function _showDepTip(ev, text){
  _depTipEl = _depTipEl || document.getElementById('dep-tip');
  if (!_depTipEl) return;
  _depTipEl.textContent = text;
  _depTipEl.style.display = 'block';
  _moveDepTip(ev);
}
function _moveDepTip(ev){
  if (!_depTipEl) return;
  _depTipEl.style.left = ((ev.clientX||0)+12) + 'px';
  _depTipEl.style.top  = ((ev.clientY||0)+14) + 'px';
}
function _hideDepTip(){ if (_depTipEl) _depTipEl.style.display='none'; }

// ─── 字典 SSE 流式渲染：ECDICT 立刻显示，free/mw/translate 后续追加 ───
async function dictStream(word, ctx) {
  const params = new URLSearchParams({
    word, file: FILE_REL || '', page: String(currentPage || 0), context: ctx || '',
  });
  window.dlog?.(`dictStream word="${word}" file=${FILE_REL?'Y':'N'} page=${currentPage} ctxLen=${ctx?.length||0}`);
  // 立刻 openResult 占位，避免空等
  openResult('📖 ' + word, word, '<div class="loading">⏳ 查词中…</div>');
  const _optPw = _charSel?.pw;   // 乐观下划线目标页（查词时所在页）
  const myReq = _resultReqId;    // 本次查词的请求序号；被新结果框作废后，后到的 SSE 渲染一律丢弃
  // 无论 SSE / JSON / 失败：1.8s 后无条件触发一次下划线刷新（vocab note 写盘耗时）
  setTimeout(() => {
    window.dlog?.('refreshVocabUnderlines (timer) for ' + word);
    refreshVocabUnderlinesForAllPages();
  }, 1800);
  // 3.5s 再刷一次（等 paragraph_exposure 完成）
  setTimeout(() => { refreshVocabUnderlinesForAllPages(); }, 3500);
  const contentEl = document.getElementById('result-content');
  const state = {
    word, lemma: word, forms: [],
    phon_us: '', phon_uk: '', audio_us: '', audio_uk: '',
    freq_bnc: 0, translation: '', definition: '',
    fd_defs: [], mw_defs: [], examples: new Set(), examples_zh: {},
    synonyms: [], antonyms: [],
    sources_hit: [], vocab_note: '',
  };
  const esc = s => String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;');
  const renderState = () => {
    const s = state;
    let html = '';
    const head = [];
    if (s.phon_us) head.push(`<span style="font-style:italic">US ${esc(s.phon_us)}</span>`);
    if (s.phon_uk) head.push(`<span style="font-style:italic">UK ${esc(s.phon_uk)}</span>`);
    if (s.freq_bnc) head.push(`<span style="color:#5a6680;font-size:11px">BNC #${s.freq_bnc}</span>`);
    if (s.audio_us) head.push(`<button onclick="new Audio('${esc(s.audio_us)}').play()" style="background:transparent;border:1px solid #3b6db5;color:#a8cdff;border-radius:50%;width:24px;height:24px;cursor:pointer;font-size:11px;padding:0">🔊</button>`);
    html += `<div style="display:flex;gap:8px;align-items:center;color:#a8cdff;font-size:13px">${head.join(' · ')}</div>`;
    if (s.lemma && s.lemma !== word) {
      html += `<div style="margin-top:4px;color:#7a8497;font-size:11px">原型：<code>${esc(s.lemma)}</code>${s.forms?.length?'（'+s.forms.map(esc).join('/')+'）':''}</div>`;
    }
    if (s.translation) html += `<div style="margin-top:10px;color:#cfe6ff;white-space:pre-wrap;line-height:1.6">${esc(s.translation)}</div>`;
    // MW + Free Dict 例句（合并）
    const allDefs = [];
    if (s.mw_defs.length) allDefs.push({label: '📚 MW', defs: s.mw_defs});
    if (s.fd_defs.length) allDefs.push({label: '🌐 Wiktionary', defs: s.fd_defs});
    for (const grp of allDefs) {
      html += `<div style="margin-top:12px;padding-top:8px;border-top:1px solid #2a3550;color:#8a9bb4;font-size:12px"><b style="color:#7a8497">${esc(grp.label)}</b>`;
      html += `<ul style="margin:6px 0 0 18px;padding:0;line-height:1.6">`;
      for (const d of grp.defs.slice(0, 6)) {
        html += `<li>${d.pos ? '<b>'+esc(d.pos)+'</b> ' : ''}${esc(d.en)}`;
        for (const ex of (d.examples||[]).slice(0, 2)) {
          const zh = state.examples_zh[ex];
          html += `<br><span style="color:#7a8497;font-size:11px">▸ ${esc(ex)}${zh ? '<br>　🇨🇳 ' + esc(zh) : ''}</span>`;
        }
        html += `</li>`;
      }
      html += `</ul></div>`;
    }
    // 同义反义
    if (s.synonyms.length || s.antonyms.length) {
      const meta = [];
      if (s.synonyms.length) meta.push('同 ' + s.synonyms.slice(0,5).map(esc).join(', '));
      if (s.antonyms.length) meta.push('反 ' + s.antonyms.slice(0,5).map(esc).join(', '));
      html += `<div style="margin-top:8px;color:#7a8497;font-size:11px">${meta.join(' · ')}</div>`;
    }
    contentEl.innerHTML = html;
    // 底部 actions：搬到 #vocab-actions（脱离内容滚动区，始终可见）
    const va = document.getElementById('vocab-actions');
    if (va) {
      va.className = 'show';
      va.innerHTML =
        `<button onclick="addVocabAnki('${esc(s.lemma||word)}')" style="background:#244470;border:1px solid #3b6db5;color:#fff;border-radius:6px;padding:6px 14px;cursor:pointer;font-size:12px">🎴 加入 Anki</button>` +
        `<button onclick="markVocabKnown('${esc(s.lemma||word)}', this)" style="background:#1d3a28;border:1px solid #2e7d4f;color:#9fe0b8;border-radius:6px;padding:6px 14px;cursor:pointer;font-size:12px" title="掌握度直接设为 100%，此后不再算作生词">✓ 已掌握</button>` +
        (s.sources_hit.length
          ? `<span style="color:#5a6680;font-size:10px;margin-left:auto">源：${s.sources_hit.join(' + ')}${s.vocab_note ? ' · <a href="obsidian://open?vault=obsidian&file='+encodeURIComponent(s.vocab_note)+'" style="color:#60a5fa">在 Obsidian 打开词条 →</a>' : ''}</span>`
          : `<span style="color:#5a6680;font-size:10px;margin-left:auto">⏳ 加载更多源…</span>`);
    }
  };

  let r;
  try {
    r = await fetch('/pdf/api/dict?' + params.toString(), {
      headers: {'Accept': 'text/event-stream'},
    });
  } catch (e) { return false; }
  if (!r.ok) return false;
  const ct = r.headers.get('content-type') || '';
  if (!ct.includes('event-stream')) {
    // 后端非 SSE 模式：fall back 一次性渲染（兼容旧路径，理论不会走这里）
    const d = await r.json().catch(() => ({}));
    if (!d.ok) return false;
    Object.assign(state, {
      lemma: d.lemma, forms: d.forms||[],
      phon_us: d.phonetic_us, phon_uk: d.phonetic_uk,
      audio_us: d.audio_us, audio_uk: d.audio_uk,
      freq_bnc: d.freq_bnc, translation: d.translation,
      synonyms: d.synonyms||[], antonyms: d.antonyms||[],
      sources_hit: d.sources_hit||[], vocab_note: d.vocab_note||'',
    });
    renderState();
    return true;
  }
  // SSE 模式：边读边渲染
  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buf = '', gotEcdict = false;
  let renderQueued = false, lastRender = 0;
  const scheduleRender = () => {
    if (myReq !== _resultReqId) return;   // 已被新结果框(解释/翻译)作废 → 不再写回，防延迟结果覆盖
    const now = Date.now();
    if (now - lastRender >= 80) { renderState(); lastRender = now; return; }
    if (renderQueued) return;
    renderQueued = true;
    setTimeout(() => { renderQueued = false; renderState(); lastRender = Date.now(); }, 80 - (now - lastRender));
  };
  while (true) {
    const {value, done} = await reader.read();
    if (done) break;
    buf += decoder.decode(value, {stream: true});
    let idx;
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const block = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      let evt = 'message', data = '';
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) evt = line.slice(6).trim();
        else if (line.startsWith('data:')) data += line.slice(5).trim();
      }
      let payload = {};
      try { payload = JSON.parse(data || '{}'); } catch(_) {}
      if (evt === 'ecdict') {
        gotEcdict = true;
        Object.assign(state, {
          lemma: payload.lemma, forms: payload.forms || [],
          phon_us: payload.phonetic || '',
          freq_bnc: payload.freq_bnc || 0,
          translation: payload.translation || '',
          definition: payload.definition || '',
          sources_hit: ['ecdict'],
        });
        scheduleRender();
        _markVocabOptimistic(_optPw, payload.lemma, payload.forms || []);   // 立即标下划线，不等笔记
      } else if (evt === 'free') {
        if (payload.phon_us) state.phon_us = payload.phon_us;
        if (payload.phon_uk) state.phon_uk = payload.phon_uk;
        if (payload.audio_us && !state.audio_us) state.audio_us = payload.audio_us;
        if (payload.audio_uk && !state.audio_uk) state.audio_uk = payload.audio_uk;
        state.fd_defs = payload.definitions_en || [];
        state.synonyms = payload.synonyms || [];
        state.antonyms = payload.antonyms || [];
        if (!state.sources_hit.includes('free_dict')) state.sources_hit.push('free_dict');
        scheduleRender();
      } else if (evt === 'mw') {
        if (payload.phon_us) state.phon_us = payload.phon_us;
        if (payload.audio_us) state.audio_us = payload.audio_us;
        state.mw_defs = payload.definitions_en || [];
        if (!state.sources_hit.includes('mw')) state.sources_hit.push('mw');
        scheduleRender();
      } else if (evt === 'translate') {
        if (payload.en && payload.zh) {
          state.examples_zh[payload.en] = payload.zh;
          scheduleRender();
        }
      } else if (evt === 'done') {
        state.vocab_note = payload.vocab_note || '';
        renderState();
        // 1.5s 后刷新下划线：等后台 vocab note 写完 + paragraph_exposure 跑完
        setTimeout(() => { refreshVocabUnderlinesForAllPages(); }, 1500);
      } else if (evt === 'error') {
        if (!gotEcdict) return false;   // ECDICT 都没拿到 → 让 AI 回落
      }
    }
  }
  return gotEcdict;
}
window.dictStream = dictStream;

// 完整字典框「✓ 已掌握」按钮：mastery 直接锁 100% → POST /pdf/api/vocab-mark
window.markVocabKnown = async (lemma, btn) => {
  if (!lemma) return;
  const old = btn.textContent;
  btn.disabled = true; btn.style.opacity = '0.6'; btn.textContent = '⏳ …';
  try {
    const r = await fetch('/pdf/api/vocab-mark', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({word: lemma, mark: 'known'}),
    });
    const d = await r.json().catch(() => ({}));
    if (d.ok) {
      btn.textContent = '✓ 已掌握 100%';
      btn.style.background = '#1f5132'; btn.style.borderColor = '#3ba566'; btn.style.color = '#cdf5d9';
      btn.style.opacity = '1';
      refreshVocabUnderlinesForAllPages?.();   // 掌握后该词不再标生词下划线
      window.dlog?.('vocab-mark known ok: ' + lemma + ' → mastery 1.0');
    } else {
      btn.disabled = false; btn.style.opacity = '1'; btn.textContent = old;
      window.dlog?.('vocab-mark failed: ' + (d.error || 'unknown'));
    }
  } catch (e) {
    btn.disabled = false; btn.style.opacity = '1'; btn.textContent = old;
  }
};

// 字典 modal「🎴 加入 Anki」按钮：POST /pdf/api/vocab-anki
window.addVocabAnki = async (lemma) => {
  if (!lemma) return;
  _toast('🎴 正在加 Anki…');
  try {
    const r = await fetch('/pdf/api/vocab-anki', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({word: lemma}),
    });
    const d = await r.json();
    if (d.ok) _toast(d.action === 'created' ? '✅ Anki 卡已创建' : '✅ Anki 卡已更新');
    else _toast('❌ ' + (d.error || '失败'));
  } catch (e) {
    _toast('❌ 网络错误：' + e.message);
  }
};

// 点击已存在的高亮 → popover（预览块 + 颜色 + 备注 + 删除）
let _popoverHL = null;
function closeHlPopover() {
  _popoverHL = null;
  document.getElementById('hl-popover')?.classList.remove('open');
}
window.closeHlPopover = closeHlPopover;
function openHlPopover(h, anchorDiv, pw) {
  _popoverHL = h;
  const pop = document.getElementById('hl-popover');
  window.dlog?.('openHlPopover: pop=' + (pop ? 'OK' : 'MISSING'));
  if (!pop) return;
  const colorsHtml = getHlColors().map(c =>
    `<span class="swatch${c===h.color?' cur':''}" data-c="${escHtml(c)}" style="background:${escHtml(c)}"></span>`
  ).join('');
  const kindLbl = h.kind === 'translate' ? '🌐 翻译'
                : h.kind === 'explain'   ? '💡 解释'
                : '📝 备注';
  pop.innerHTML = `
    <div class="hl-snip-wrap" data-id="${escHtml(h.id)}">
      <div class="hl-snip">
        <div class="hl-snip-content">
          ${h.text ? `<div class="hl-snip-row text"><span class="row-lbl">📌 选中</span>${escHtml(h.text)}</div>` : ''}
          ${h.sentence ? `<div class="hl-snip-row sentence"><span class="row-lbl">📖 所在句</span>${escHtml(h.sentence)}</div>` : ''}
          ${h.body ? `<div class="hl-snip-row body"><span class="row-lbl">${kindLbl}</span>${escHtml(h.body)}</div>` : ''}
          ${(!h.text && !h.sentence && !h.body) ? `<div class="hl-snip-row text" style="color:#7a8497">（无文字内容）</div>` : ''}
        </div>
        <div class="hl-snip-circle" title="按住左滑显示删除"></div>
      </div>
      <button class="hl-snip-del-row" type="button" title="删除高亮">🗑</button>
    </div>
    <div class="row"><span class="row-lbl">🎨 颜色</span>${colorsHtml}</div>
    <textarea id="hl-note" placeholder="自定义备注（可空）">${escHtml(h.note || '')}</textarea>
    <div class="actions">
      <button data-act="save" class="primary">保存</button>
    </div>
  `;
  // 预览块：点击展开 / 收起；右侧圆圈左滑（或整体触屏左滑）→ 下方滑出删除栏
  const wrapEl = pop.querySelector('.hl-snip-wrap');
  if (wrapEl) _attachSnipBehavior(wrapEl, () => _hlDelete(h, pw));
  // 颜色 swatch：点 = 立即切换颜色（PATCH 后端 + 重渲染）
  //   - 点已是 .cur 的色 → 取消高亮颜色
  //       · 没备注（note/body/sentence 都空）→ 直接删除该高亮
  //       · 有备注 → 保留高亮（color 设空）+ 视觉变"仅备注"虚框样式
  //   - 点别的色 → 立即切到该色
  pop.querySelectorAll('.row .swatch').forEach(sw => {
    sw.onclick = async (e) => {
      e.stopPropagation();
      if (sw.classList.contains('cur')) {
        const hasNote = (h.note || '').trim() || (h.body || '').trim() || (h.sentence || '').trim();
        if (!hasNote) {
          await _hlDelete(h, pw);
        } else {
          await _hlUpdate(h, pw, {color: ''});
          closeHlPopover();
          _toast('已取消颜色（备注保留）');
        }
        return;
      }
      // 切换到新色：立即 PATCH，不用等 [保存]
      const newColor = sw.dataset.c;
      pop.querySelectorAll('.row .swatch').forEach(s => s.classList.remove('cur'));
      sw.classList.add('cur');
      await _hlUpdate(h, pw, {color: newColor});
    };
  });
  // [保存] 只更新 note 文字（颜色由色板点击立即生效）
  pop.querySelector('[data-act=save]').onclick = async (e) => {
    e.stopPropagation();
    await _hlUpdate(h, pw, { note: document.getElementById('hl-note').value });
    closeHlPopover();
  };
  // 删除入口现在统一在 .hl-snip-del-row（左滑揭示）；底部不再有 [data-act=del] 按钮
  // 定位：高亮元素下方（贴齐左边，跟 main 滚动）
  const r = anchorDiv.getBoundingClientRect();
  pop.style.left = (r.left + window.scrollX) + 'px';
  pop.style.top  = (r.bottom + window.scrollY + 6) + 'px';
  pop.classList.add('open');
  // 防止 popover 跑出视口右侧
  requestAnimationFrame(() => {
    const pr = pop.getBoundingClientRect();
    if (pr.right > _visRight() - 8) {
      pop.style.left = Math.max(8, _visRight() - pr.width - 8) + 'px';
    }
  });
}

async function _hlUpdate(h, pw, patch) {
  try {
    const r = await fetch('/pdf/api/highlights', {
      method: 'PATCH', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({file: FILE_REL, id: h.id, ...patch}),
    });
    const d = await r.json();
    if (!d.ok) { alert('保存失败：' + (d.error || '?')); return; }
    Object.assign(h, d.highlight);
    renderHighlightsOnPage(pw, h.page);
    _toast('已保存');
  } catch (e) { alert('保存异常：' + e.message); }
}
async function _hlDelete(h, pw) {
  if (!confirm('删除这条高亮？')) return;
  try {
    const r = await fetch('/pdf/api/highlights', {
      method: 'DELETE', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({file: FILE_REL, id: h.id}),
    });
    const d = await r.json();
    if (!d.ok) { alert('删除失败：' + (d.error || '?')); return; }
    _allHighlights = _allHighlights.filter(x => x.id !== h.id);
    _hlByPage[h.page] = (_hlByPage[h.page] || []).filter(x => x.id !== h.id);
    renderHighlightsOnPage(pw, h.page);
    closeHlPopover();
    _toast('已删除');
  } catch (e) { alert('删除异常：' + e.message); }
}

// 预览块的交互：
//   - 单击文字内容 → 展开/收起全文
//   - 右侧圆圈左滑（或整体触屏左滑） → 下方滑出删除栏（.swiped）
//   - 再点圆圈 / 任意位置（除按钮）→ 复位
function _attachSnipBehavior(wrap, onDel) {
  const snip = wrap.querySelector('.hl-snip');
  const content = wrap.querySelector('.hl-snip-content');
  const circle = wrap.querySelector('.hl-snip-circle');
  const del = wrap.querySelector('.hl-snip-del-row');
  if (!snip || !del) return;
  del.onclick = (e) => { e.stopPropagation(); onDel(); };

  const reveal = () => { wrap.classList.add('swiped'); snip.style.transform = ''; };
  const reset  = () => { wrap.classList.remove('swiped'); snip.style.transform = ''; };

  // 触屏 swipe（整体 snip）
  let sx = 0, sy = 0, dx = 0, dy = 0, swiping = false, axis = '';
  const onTouchStart = (e) => {
    const t = e.touches[0]; sx = t.clientX; sy = t.clientY; dx = dy = 0; swiping = true; axis = '';
  };
  const onTouchMove = (e) => {
    if (!swiping) return;
    const t = e.touches[0];
    dx = t.clientX - sx; dy = t.clientY - sy;
    if (!axis) {
      if (Math.abs(dx) > 6 || Math.abs(dy) > 6) axis = Math.abs(dx) > Math.abs(dy) ? 'x' : 'y';
    }
    if (axis === 'x') {
      if (dx < 0) snip.style.transform = `translateX(${Math.max(dx,-80)}px)`;
      else if (wrap.classList.contains('swiped')) snip.style.transform = `translateX(${Math.min(dx,30)}px)`;
    }
  };
  const onTouchEnd = () => {
    swiping = false;
    if (axis === 'x') {
      if (dx < -40) reveal();
      else if (dx > 30 || !wrap.classList.contains('swiped')) reset();
      else reveal();   // 已 swipe 态、小位移 → 维持
    }
    dx = dy = 0; axis = '';
  };
  snip.addEventListener('touchstart', onTouchStart, {passive:true});
  snip.addEventListener('touchmove',  onTouchMove,  {passive:true});
  snip.addEventListener('touchend',   onTouchEnd);

  // 鼠标 swipe（按住圆圈拖动；其他位置点击 = 展开/收起）
  if (circle) {
    let md = false, mx = 0, mdx = 0;
    circle.addEventListener('mousedown', (e) => {
      md = true; mx = e.clientX; mdx = 0; e.preventDefault();
    });
    document.addEventListener('mousemove', (e) => {
      if (!md) return;
      mdx = e.clientX - mx;
      if (mdx < 0) snip.style.transform = `translateX(${Math.max(mdx,-80)}px)`;
      else if (wrap.classList.contains('swiped')) snip.style.transform = `translateX(${Math.min(mdx,30)}px)`;
    });
    document.addEventListener('mouseup', () => {
      if (!md) return;
      md = false;
      if (mdx < -40) reveal();
      else if (mdx > 30 || !wrap.classList.contains('swiped')) reset();
      mdx = 0;
    });
    // 圆圈单击（无 drag）= 切换 swiped
    circle.addEventListener('click', (e) => {
      e.stopPropagation();
      if (wrap.classList.contains('swiped')) reset(); else reveal();
    });
  }

  // 单击 content → 展开/收起全文
  if (content) {
    content.addEventListener('click', (e) => {
      if (e.target.closest('button')) return;
      // swipe 态时，点击不展开，先复位
      if (wrap.classList.contains('swiped')) { reset(); return; }
      content.classList.toggle('expanded');
    });
  }
}

// 设置 modal 里的颜色管理
function renderHlColorSetting() {
  const c = document.getElementById('set-hl-colors');
  if (!c) return;
  c.innerHTML = '';
  for (const col of getHlColors()) {
    const w = document.createElement('div');
    w.className = 'swatch-w';
    const sw = document.createElement('span');
    sw.className = 'swatch';
    sw.style.background = col;
    w.appendChild(sw);
    const del = document.createElement('button');
    del.className = 'del';
    del.textContent = '×';
    del.title = '删除';
    del.onclick = () => {
      const cur = getHlColors().filter(x => x !== col);
      saveHlColors(cur.length ? cur : DEFAULT_HL_COLORS);
      renderHlPicker();
      renderHlColorSetting();
    };
    w.appendChild(del);
    c.appendChild(w);
  }
}
window.addHlColor = () => {
  const v = (document.getElementById('set-hl-new')?.value || '').trim();
  if (!/^#[0-9a-fA-F]{3,8}$/.test(v)) { alert('颜色格式应为 #rrggbb'); return; }
  const arr = getHlColors();
  if (arr.includes(v)) return;
  arr.push(v);
  saveHlColors(arr);
  renderHlPicker();
  renderHlColorSetting();
};
window.resetHlColors = () => {
  saveHlColors(DEFAULT_HL_COLORS);
  renderHlPicker();
  renderHlColorSetting();
};

// 全局：点 popover 外 / hl-saved 外 → 关 popover
document.addEventListener('click', (e) => {
  if (!_popoverHL) return;
  if (e.target.closest?.('#hl-popover')) return;
  if (e.target.closest?.('.hl-saved')) return;
  closeHlPopover();
}, true);

// 缩放/重渲染后重新贴高亮（zoom 变了 css px 也变了）
const _origRenderPageInto = null;  // 仅占位
// 在 _renderPageInto 里 loadCharsAndBindLayer 内部已经调用了 renderHighlightsOnPage

// ── 日语词「完整字典」大页面(离线富内容 + 按需 AI 深入)──────────────────
let _jpKanjiData = [];   // 当前词的汉字拆解,供 chip 点击展开
let _jpPollTimer = null;   // 例句/汉字字义中译后台轮询替换的计时器
// 轮询 /api/dict-jp-zh,拿后台翻好的例句/汉字字义中文，原地替换英文（跟英文单词一致，不增加等待）
function _jpPollZh(word) {
  clearInterval(_jpPollTimer);
  let tries = 0;
  _jpPollTimer = setInterval(async () => {
    tries++;
    let d = null;
    try { d = await (await fetch('/pdf/api/dict-jp-zh?word=' + encodeURIComponent(word))).json(); }
    catch (_) { d = null; }
    if (!d || !d.ok) { if (tries >= 10) clearInterval(_jpPollTimer); return; }
    let pending = false;
    (d.examples || []).forEach((e, i) => {
      if (e.zh) {
        const el = document.querySelector('.jp-ex-zh[data-exi="' + i + '"]:not([data-zhdone])');
        if (el) { el.textContent = e.zh; el.dataset.zhdone = '1'; }
      } else pending = true;
    });
    (d.kanji || []).forEach((k, i) => {
      if (k.meanings_zh) {
        if (_jpKanjiData[i] && !_jpKanjiData[i].meanings_zh) {
          _jpKanjiData[i].meanings_zh = k.meanings_zh;
          const chip = document.querySelectorAll('.jp-kanji-chip')[i];
          if (chip && chip.classList.contains('active')) _jpKanjiTap(i);   // 详情正打开 → 刷新
        }
      } else pending = true;
    });
    if (!pending || tries >= 10) clearInterval(_jpPollTimer);
  }, 1500);
}
async function dictStreamJP(word, ctx) {
  clearInterval(_jpPollTimer);   // 取消上一个词的中译轮询，避免串到当前词
  openResult('📖 ' + word, word, '<div class="loading">⏳ 查词中…</div>');
  const myReq = _resultReqId;
  const contentEl = document.getElementById('result-content');
  let d;
  try {
    const r = await fetch('/pdf/api/dict-jp?word=' + encodeURIComponent(word) +
      '&context=' + encodeURIComponent(ctx || ''));
    d = await r.json();
  } catch (e) {
    if (myReq === _resultReqId) contentEl.innerHTML = '<div style="color:#c00;padding:14px">查词失败：' + e.message + '</div>';
    return false;
  }
  if (myReq !== _resultReqId) return false;
  if (!d.ok) return dictStream(word, ctx);   // 也许其实是英文词 → 回退三源框
  const esc = s => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
  const wq = esc(word).replace(/'/g, "\\'");
  const rq = esc(d.reading || word).replace(/'/g, "\\'");   // 发音念假名读音
  const phon = (d.reading && d.accent != null) ? _renderPitch(d.reading, d.accent)
    : (d.reading ? '<span class="wp-phon">' + esc(d.reading) + '</span>' : '');
  let html = '<div class="jp-head">' + phon +
    (d.romaji ? '<span class="jp-romaji">' + esc(d.romaji) + '</span>' : '') +
    (d.pos ? '<span class="jp-pos">' + esc(d.pos) + '</span>' : '') + '</div>';
  if (d.zh) html += '<div class="jp-zh">' + esc(d.zh) + '</div>';
  html += _jpInflectHtml(d.inflect, word);   // 变形分析:原形 + 语法标签
  _jpKanjiData = d.kanji || [];
  if (_jpKanjiData.length) {
    html += '<div class="jp-sec-label">汉字（点字看音读/训读）</div><div class="jp-kanji-row">' +
      _jpKanjiData.map((k, i) => '<button class="jp-kanji-chip" onclick="_jpKanjiTap(' + i + ')">' + esc(k.kanji) + '</button>').join('') +
      '</div><div id="jp-kanji-detail" class="jp-kanji-detail"></div>';
  }
  if ((d.examples || []).length) {
    html += '<div class="jp-sec-label">母语例句</div><div class="jp-ex">';
    d.examples.forEach((e, ei) => {
      html += '<div class="jp-ex-ja">' + esc(e.ja) + '</div>' +
              '<div class="jp-ex-zh" data-exi="' + ei + '"' + (e.zh ? ' data-zhdone="1"' : '') + '>' +
              esc(e.zh || e.en || '') + '</div>';   // 没中译先回退英文，后台翻好由 _jpPollZh 替换
    });
    html += '</div>';
  }
  html += '<button id="jp-ai-btn" class="jp-ai-btn" onclick="_jpAiDeep(\'' + wq + '\')">✨ AI 深入讲解（用法 / 语感 / 近义辨析）</button>' +
          '<div id="jp-ai-out" class="jp-ai-out"></div>';
  contentEl.innerHTML = html;
  contentEl.scrollTop = 0;
  if (_jpKanjiData.length) _jpKanjiTap(0);   // 默认展开第一个汉字
  // 有未翻的例句/汉字字义 → 后台翻 + 轮询替换英文（不增加等待）
  if ((d.examples || []).some(e => !e.zh) || _jpKanjiData.some(k => !k.meanings_zh)) _jpPollZh(word);
  const va = document.getElementById('vocab-actions');
  if (va) {
    va.className = 'show';
    const bs = 'border-radius:6px;padding:6px 14px;cursor:pointer;font-size:12px';
    va.innerHTML =
      '<button onclick="_ttsWord(\'' + rq + '\', \'ja-JP\')" style="background:transparent;border:1px solid #3b6db5;color:#a8cdff;' + bs + '">🔊 朗读</button>' +
      '<button onclick="addVocabAnki(\'' + wq + '\')" style="background:#244470;border:1px solid #3b6db5;color:#fff;' + bs + '">🎴 加入 Anki</button>' +
      '<button onclick="markVocabKnown(\'' + wq + '\', this)" style="background:#1d3a28;border:1px solid #2e7d4f;color:#9fe0b8;' + bs + '" title="掌握度设为100%">✓ 已掌握</button>';
  }
  return true;
}
window._jpKanjiTap = (i) => {
  const k = _jpKanjiData[i]; if (!k) return;
  document.querySelectorAll('.jp-kanji-chip').forEach((c, j) => c.classList.toggle('active', j === i));
  const det = document.getElementById('jp-kanji-detail'); if (!det) return;
  const esc = s => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
  let h = '<div class="jk-lit">' + esc(k.kanji) + '</div><div class="jk-body">';
  if ((k.on || []).length) h += '<div><span class="jk-tag jk-on">音</span>' + k.on.map(esc).join('、') + '</div>';
  if ((k.kun || []).length) h += '<div><span class="jk-tag jk-kun">訓</span>' + k.kun.map(esc).join('、') + '</div>';
  // 字义优先显示中文(meanings_zh,后端 Google 翻译)，缺失才回退英文
  const _meanEn = (k.meanings || []).map(esc).join('; ');
  if (k.meanings_zh || _meanEn) h += '<div class="jk-mean">' + (k.meanings_zh ? esc(k.meanings_zh) : _meanEn) + '</div>';
  h += '</div>';
  det.innerHTML = h;
};
window._jpAiDeep = async (word) => {
  const btn = document.getElementById('jp-ai-btn');
  const out = document.getElementById('jp-ai-out');
  if (!out) return;
  const myReq = _resultReqId;
  if (btn) { btn.disabled = true; btn.textContent = '✨ 生成中…'; }
  const ctx = (_wordPopState && _wordPopState.ctx) || '';
  try {
    const render = (text) => {
      if (myReq !== _resultReqId) return;   // 结果框已被新内容作废
      out.innerHTML = md(text || ' ');
      if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([out]).catch(() => {});
      out.scrollIntoView && out.scrollIntoView({block: 'nearest'});
    };
    const res = await _aiStream('/pdf/api/dict-jp-ai?word=' + encodeURIComponent(word) +
      '&context=' + encodeURIComponent(ctx), { method: 'GET', onText: render });
    if (myReq !== _resultReqId) return;
    if (res.ok) render(res.text);
    else out.innerHTML = '<span style="color:#c00">AI 失败：' + (res.error || '') + '</span>';
  } catch (e) {
    out.innerHTML = '<span style="color:#c00">AI 失败：' + e.message + '</span>';
  }
  if (btn) btn.style.display = 'none';
};

// 结果 modal
function openResult(title, src, contentHtml) {
  try { _pushQueryHistory(); } catch (_) {}   // 开新结果前，把上一个结果快照进「📜 历史」
  _resultReqId++;   // 新结果框 → 作废所有进行中的旧异步任务（查词/翻译/解释），它们的延迟回调将被丢弃
  document.getElementById('result-title').textContent = title;
  document.getElementById('result-src').textContent = src;
  document.getElementById('result-content').innerHTML = contentHtml;
  document.getElementById('result-content').scrollTop = 0;   // 新结果回到顶部
  document.getElementById('result-content').dataset.title = title;
  document.getElementById('result-content').dataset.src = src;
  // 清掉上次 vocab-actions（翻译/解释/AI 调用不需要它）
  const va = document.getElementById('vocab-actions');
  if (va) { va.className = ''; va.innerHTML = ''; }
  document.getElementById('result-mask').classList.add('open');
  if (window.MathJax && MathJax.typesetPromise) {
    MathJax.typesetPromise([document.getElementById('result-content')]).catch(()=>{});
  }
}
window.closeResult = () => { try { _pushQueryHistory(); } catch (_) {} document.getElementById('result-mask').classList.remove('open'); };

// ──────── AI 回答里的加号选中（同 QA browser 风格） ────────
function _headLevel(h) { return parseInt(h.tagName.slice(1), 10); }
function _isFakeHead(h) { return h.classList && h.classList.contains('fake-head'); }
function addResultPickers() {
  const md = document.getElementById('result-content');
  if (!md) return;
  // 先清掉旧的（流式过程中会被多次 marked.parse 覆盖）
  md.querySelectorAll('.head-pick').forEach(b => b.remove());
  md.querySelectorAll('.has-pick').forEach(el => el.classList.remove('has-pick'));
  const existingAll = document.querySelector('.reply-pick-all-result');
  if (existingAll) existingAll.remove();

  const realHeads = Array.from(md.querySelectorAll('h1,h2,h3,h4,h5,h6'));
  // 粗体段落假标题（AI 常用 **标题** 而非 ## 标题）
  const fakeHeads = [];
  md.querySelectorAll('p, li').forEach(el => {
    if (el.closest('h1,h2,h3,h4,h5,h6')) return;
    const strongs = el.querySelectorAll('strong');
    if (strongs.length !== 1) return;
    const t = (el.textContent || '').trim();
    const st = (strongs[0].textContent || '').trim();
    if (t.length >= 3 && st.length >= 2 && st.length / t.length >= 0.85) {
      el.classList.add('fake-head');
      fakeHeads.push(el);
    }
  });
  const heads = [...realHeads, ...fakeHeads];

  // 右上角小加号（选用整条回答），定位在 result-modal 标题旁
  const modal = document.getElementById('result-modal');
  let all = modal.querySelector('.reply-pick-all-result');
  if (!all) {
    all = document.createElement('button');
    all.className = 'pick-btn reply-pick-all-result';
    all.title = '选用整条回答（加入草稿）';
    all.style.position = 'absolute';
    all.style.right = '22px';
    all.style.top = '20px';
    modal.style.position = 'relative';
    modal.appendChild(all);
  }
  all.textContent = '+';
  all.classList.remove('on');
  all.onclick = (e) => {
    e.stopPropagation();
    const on = all.classList.toggle('on');
    all.textContent = on ? '✓' : '+';
    md.classList.toggle('picked-all', on);
    const fullText = (md.dataset.raw || md.textContent || '').trim();
    if (on) {
      if (fullText && !_drafts.some(d => d.text === fullText)) {
        _drafts.push({
          id: 'd_' + Date.now() + '_' + Math.random().toString(36).slice(2,6),
          text: fullText,
          source: md.dataset.title || '',
          src: md.dataset.src || '',
          time: Date.now(),
          selected: true,
        });
        _persistDrafts();
        _updateDraftBadge();
      }
    } else {
      _drafts = _drafts.filter(d => d.text !== fullText);
      _persistDrafts();
      _updateDraftBadge();
    }
  };
  md.dataset.raw = md.textContent || '';

  if (!heads.length) return;
  let singleTopCovers = false, singleTop = null;
  if (realHeads.length) {
    const minLvl = Math.min(...realHeads.map(_headLevel));
    const tops = realHeads.filter(h => _headLevel(h) === minLvl);
    if (tops.length === 1 && md.firstElementChild === tops[0]) {
      singleTopCovers = true; singleTop = tops[0];
    }
  }
  heads.forEach(h => {
    if (singleTopCovers && h === singleTop) return;
    if (h.querySelector('.head-pick')) return;
    h.classList.add('has-pick');
    const btn = document.createElement('button');
    btn.className = 'pick-btn head-pick';
    btn.textContent = '+';
    btn.onclick = (e) => { e.stopPropagation(); _toggleResultPick(h, md); };
    h.appendChild(btn);
  });
}
function _toggleResultPick(h, md) {
  const newOn = !h.querySelector('.head-pick').classList.contains('on');
  if (!_isFakeHead(h) && /^H[1-6]$/.test(h.tagName)) {
    const heads = Array.from(md.querySelectorAll('h1,h2,h3,h4,h5,h6'));
    const i = heads.indexOf(h);
    const lvl = _headLevel(h);
    _setResultPick(h, newOn);
    for (let j = i + 1; j < heads.length; j++) {
      if (_headLevel(heads[j]) <= lvl) break;
      _setResultPick(heads[j], newOn);
    }
  } else {
    _setResultPick(h, newOn);
  }
  _collectAndPersistResultSelection();
}
function _setResultPick(h, on) {
  const btn = h.querySelector('.head-pick');
  if (btn) btn.classList.toggle('on', on);
  h.classList.toggle('hsec-picked', on);
  let sib = h.nextElementSibling;
  while (sib && !/^H[1-6]$/.test(sib.tagName) && !(sib.classList && sib.classList.contains('fake-head'))) {
    sib.classList.toggle('hsec-picked-body', on);
    sib = sib.nextElementSibling;
  }
}
function _collectSelectedSectionTexts() {
  const md = document.getElementById('result-content');
  if (!md) return [];
  const parts = [];
  md.querySelectorAll('h1,h2,h3,h4,h5,h6,p.fake-head,li.fake-head').forEach(h => {
    const btn = h.querySelector('.head-pick');
    if (!btn || !btn.classList.contains('on')) return;
    let txt;
    if (/^H[1-6]$/.test(h.tagName)) {
      txt = '#'.repeat(_headLevel(h)) + ' ' + (h.textContent || '').replace(/\+\s*$/, '').trim();
    } else {
      txt = (h.textContent || '').replace(/\+\s*$/, '').trim();
    }
    let sib = h.nextElementSibling;
    while (sib && !/^H[1-6]$/.test(sib.tagName) && !(sib.classList && sib.classList.contains('fake-head'))) {
      const t = (sib.textContent || '').trim();
      if (t) txt += '\n' + t;
      sib = sib.nextElementSibling;
    }
    parts.push(txt.trim());
  });
  return parts;
}
function _collectAndPersistResultSelection() {
  // 收集当前 modal 内已选段，去重 push 到 _drafts
  const texts = _collectSelectedSectionTexts();
  const title = document.getElementById('result-content').dataset.title || '';
  const src = document.getElementById('result-content').dataset.src || '';
  for (const t of texts) {
    if (!t) continue;
    if (!_drafts.some(d => d.text === t)) {
      _drafts.push({id: 'd_' + Date.now() + '_' + Math.random().toString(36).slice(2,6), text: t, source: title, src: src, time: Date.now(), selected: true});
    }
  }
  _persistDrafts();
  _updateDraftBadge();
}

// ──────── 草稿列表（多个 AI 回答的勾选段累积） ────────
let _drafts = [];
try { _drafts = JSON.parse(localStorage.getItem('pdf-drafts') || '[]'); } catch (_) {}
function _persistDrafts() {
  try { localStorage.setItem('pdf-drafts', JSON.stringify(_drafts)); } catch (_) {}
}
function _updateDraftBadge() {
  const b = document.getElementById('draft-badge');
  document.getElementById('draft-count').textContent = _drafts.length;
  b.classList.toggle('show', _drafts.length > 0);
}
window.openDraftModal = () => {
  const list = document.getElementById('draft-list');
  const cnt = document.getElementById('draft-modal-count');
  cnt.textContent = `（共 ${_drafts.length} 段，已选 ${_drafts.filter(d => d.selected).length}）`;
  if (!_drafts.length) {
    list.innerHTML = '<div class="draft-empty">还没有勾选任何段落。<br>在 AI 回答里点 + 按钮收集段落。</div>';
  } else {
    list.innerHTML = _drafts.map((d) => `
      <div class="draft-item-wrap" data-id="${d.id}">
        <div class="draft-item ${d.selected ? 'selected' : ''}">
          <div class="body">
            <div class="src">${(d.source||'').replace(/&/g,'&amp;').replace(/</g,'&lt;')} · ${new Date(d.time).toLocaleString().slice(5,16)}</div>
            <div class="text" id="dt-${d.id}">${d.text.slice(0, 200).replace(/&/g,'&amp;').replace(/</g,'&lt;')}${d.text.length>200?'…':''}</div>
          </div>
          <div class="sel-circle" title="${d.selected?'已选（点击取消）':'未选（点击勾选）'}"></div>
        </div>
        <button class="draft-item-del-row" type="button" title="删除">🗑</button>
      </div>
    `).join('');
    // 绑定每条 draft 的交互
    list.querySelectorAll('.draft-item-wrap').forEach(w => _bindDraftItem(w));
  }
  document.getElementById('draft-mask').classList.add('open');
};
window.closeDraftModal = () => document.getElementById('draft-mask').classList.remove('open');

// 草稿项交互：圆圈右侧（单击=切换 selected）；body 单击=展开；触屏左滑 / 鼠标 body 拖 = 下方滑出删除栏
function _bindDraftItem(wrap) {
  const id = wrap.dataset.id;
  const item = wrap.querySelector('.draft-item');
  const body = wrap.querySelector('.body');
  const circle = wrap.querySelector('.sel-circle');
  const delBtn = wrap.querySelector('.draft-item-del-row');
  if (!item || !circle) return;
  const reveal = () => { wrap.classList.add('swiped'); item.style.transform = ''; };
  const reset  = () => { wrap.classList.remove('swiped'); item.style.transform = ''; };

  delBtn.onclick = (e) => { e.stopPropagation(); deleteDraft(id); };

  // 圆圈单击 = 切换 selected（如果当前 swiped 态先复位）
  circle.addEventListener('click', (e) => {
    e.stopPropagation();
    if (wrap.classList.contains('swiped')) { reset(); return; }
    toggleDraftSel(id);
  });

  // 触屏：item 整体左滑揭示删除
  let sx = 0, sy = 0, dx = 0, dy = 0, swiping = false, axis = '';
  item.addEventListener('touchstart', (e) => {
    const t = e.touches[0]; sx = t.clientX; sy = t.clientY; dx = dy = 0; swiping = true; axis = '';
  }, {passive: true});
  item.addEventListener('touchmove', (e) => {
    if (!swiping) return;
    const t = e.touches[0];
    dx = t.clientX - sx; dy = t.clientY - sy;
    if (!axis && (Math.abs(dx) > 6 || Math.abs(dy) > 6)) {
      axis = Math.abs(dx) > Math.abs(dy) ? 'x' : 'y';
    }
    if (axis === 'x') {
      if (dx < 0) item.style.transform = `translateX(${Math.max(dx,-80)}px)`;
      else if (wrap.classList.contains('swiped')) item.style.transform = `translateX(${Math.min(dx,30)}px)`;
    }
  }, {passive: true});
  item.addEventListener('touchend', () => {
    swiping = false;
    if (axis === 'x') {
      if (dx < -40) reveal();
      else if (dx > 30 || !wrap.classList.contains('swiped')) reset();
    }
    dx = dy = 0; axis = '';
  });

  // 鼠标：在 body 区域水平拖 → swipe；纯点击 = 展开
  let md = false, mx = 0, mdx = 0, dragged = false;
  body.addEventListener('mousedown', (e) => {
    md = true; mx = e.clientX; mdx = 0; dragged = false;
  });
  const onMove = (e) => {
    if (!md) return;
    mdx = e.clientX - mx;
    if (Math.abs(mdx) > 4) dragged = true;
    if (mdx < 0) item.style.transform = `translateX(${Math.max(mdx,-80)}px)`;
    else if (wrap.classList.contains('swiped')) item.style.transform = `translateX(${Math.min(mdx,30)}px)`;
  };
  const onUp = () => {
    if (!md) return;
    md = false;
    if (dragged) {
      if (mdx < -40) reveal();
      else if (mdx > 30 || !wrap.classList.contains('swiped')) reset();
    }
    mdx = 0;
  };
  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);

  // body 单击 = 展开/收起；处于 swiped 态则先复位
  body.addEventListener('click', (e) => {
    if (dragged) { dragged = false; e.stopPropagation(); return; }
    if (wrap.classList.contains('swiped')) { reset(); return; }
    expandDraft(id);
  });

  // 清理 listener：modal 关闭时调用（openDraftModal 重新渲染会重复绑定，需要清旧）
  // 这里粗暴依赖每次 openDraftModal 重写 innerHTML（旧 listener 随 DOM 销毁自动 GC，
  // document.mousemove/mouseup 还在但闭包引用的 wrap/md 状态已脱离 DOM，无副作用）
}
window.toggleDraftSel = (id) => {
  const d = _drafts.find(x => x.id === id);
  if (!d) return;
  d.selected = !d.selected;
  _persistDrafts();
  openDraftModal();
};
window.expandDraft = (id) => {
  const el = document.getElementById('dt-' + id);
  const d = _drafts.find(x => x.id === id);
  if (!el || !d) return;
  // 切换全文/截断显示
  if (el.dataset.full === '1') {
    el.textContent = d.text.slice(0, 200) + (d.text.length > 200 ? '…' : '');
    el.dataset.full = '0';
    el.style.maxHeight = '80px';
  } else {
    el.textContent = d.text;
    el.dataset.full = '1';
    el.style.maxHeight = 'none';
  }
};
window.deleteDraft = (id) => {
  _drafts = _drafts.filter(d => d.id !== id);
  _persistDrafts();
  _updateDraftBadge();
  openDraftModal();
};
window.clearAllDrafts = () => {
  if (!confirm('清空所有 ' + _drafts.length + ' 段已选内容？')) return;
  _drafts = [];
  _persistDrafts();
  _updateDraftBadge();
  closeDraftModal();
};

// ── 后台任务进度条（右下角堆叠）：AI 创建笔记/Anki 不阻塞阅读器 ──
let _bgJobSeq = 0;
function _ensureBgJobsEl() {
  let c = document.getElementById('bg-jobs');
  if (!c) {
    c = document.createElement('div'); c.id = 'bg-jobs';
    c.style.cssText = 'position:fixed;right:18px;bottom:80px;display:flex;flex-direction:column;gap:6px;z-index:520;align-items:flex-end';
    document.body.appendChild(c);
  }
  return c;
}
function _startBgJob(text) {
  const id = 'bgj' + (++_bgJobSeq);
  const el = document.createElement('div'); el.id = id;
  el.style.cssText = 'background:#10162a;border:1px solid #3b6db5;color:#cfe6ff;padding:7px 12px;border-radius:8px;font-size:12px;box-shadow:0 4px 12px rgba(0,0,0,.5);max-width:280px';
  el.textContent = '⏳ ' + text;
  _ensureBgJobsEl().appendChild(el);
  return id;
}
function _finishBgJob(id, text, openUrl) {
  const el = document.getElementById(id); if (!el) return;
  el.style.borderColor = '#34d399'; el.style.color = '#34d399';
  el.textContent = '✓ ' + text + (openUrl ? ' · 点击打开' : '');
  if (openUrl) { el.style.cursor = 'pointer'; el.onclick = () => { location.href = openUrl; }; }
  setTimeout(() => { el.style.transition = 'opacity .4s'; el.style.opacity = '0'; setTimeout(() => el.remove(), 400); }, openUrl ? 8000 : 4500);
}
function _failBgJob(id, text, restore) {
  const el = document.getElementById(id); if (!el) return;
  el.style.borderColor = '#f87171'; el.style.color = '#f87171'; el.style.cursor = 'pointer';
  el.textContent = '✗ ' + text + ' · 点关闭';
  el.onclick = () => el.remove();
  // 失败 → 把段落放回草稿，方便重试
  if (restore && restore.length) {
    for (const d of restore) { if (!_drafts.some(x => x.text === d.text)) { d.selected = true; _drafts.push(d); } }
    _persistDrafts(); _updateDraftBadge();
  }
}

// 轮询后台 job：网页切后台时轮询暂停、回来继续；job 在服务器跑完结果存着，不丢
function _pollJob(jobId, jobUi, restoreOnFail) {
  let tries = 0, unknownTries = 0;
  const iv = setInterval(async () => {
    tries++;
    try {
      const r = await fetch('/pdf/api/job-status?id=' + encodeURIComponent(jobId));
      const d = await r.json();
      if (d.status === 'done') {
        clearInterval(iv);
        const out = d.result || {};
        if (out.ok) {
          const parts = [];
          if (out.note_path) parts.push('笔记已建');
          if (out.anki_added) parts.push('Anki ' + out.anki_added + ' 张');
          _finishBgJob(jobUi, parts.join(' · ') || '完成', out.obsidian_url || '');
        } else { _failBgJob(jobUi, out.error || '失败', restoreOnFail); }
      } else if (d.status === 'error') {
        clearInterval(iv); _failBgJob(jobUi, d.error || '失败', restoreOnFail);
      } else if (d.status === 'unknown') {
        if (++unknownTries >= 3) { clearInterval(iv); _failBgJob(jobUi, '任务丢失(服务重启?)', restoreOnFail); }
      }
      // running → 继续轮询
    } catch (e) {
      // 网络瞬断/网页在后台 → 不立即失败，继续轮询；超 6 分钟才放弃
      if (tries > 180) { clearInterval(iv); _failBgJob(jobUi, '轮询超时', restoreOnFail); }
    }
  }, 2000);
}

async function _doCreate(makeNote, makeAnki) {
  const picked = _drafts.filter(d => d.selected);
  if (!picked.length) { alert('请先勾选 (圆圈) 要使用的段落'); return; }
  let noteName = '';
  if (makeNote) {
    noteName = prompt('请输入笔记名（不含 .md）：', '');
    if (noteName === null) return;
    noteName = (noteName || '').trim();
    if (!noteName) { alert('未输入笔记名'); return; }
  }
  // 立即关 modal + 乐观移除已选段（失败再放回），任务丢后台跑，不挡着阅读器
  const used = picked.slice();
  _drafts = _drafts.filter(x => !x.selected);
  _persistDrafts(); _updateDraftBadge(); closeDraftModal();
  const label = (makeNote && makeAnki) ? '笔记+Anki' : (makeNote ? '笔记' : 'Anki');
  const jobUi = _startBgJob('创建' + label + '中…（' + used.length + ' 段）');
  try {
    const ov = _getAiOverrides();
    // 提交到服务器后台 job（短请求，立即返回 job_id），再轮询；任务在服务器跑，网页切后台也不中断
    const r = await fetch('/pdf/api/snippets-to-async', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        snippets: used.map(d => ({text: d.text, source: d.source})),
        make_note: makeNote, make_anki: makeAnki,
        note_name: noteName,
        model: ov.model || '', effort: ov.effort || '',
      }),
    });
    const d = await r.json();
    if (!d.ok || !d.job_id) { _failBgJob(jobUi, d.error || '提交失败', used); return; }
    _pollJob(d.job_id, jobUi, used);
  } catch (e) {
    _failBgJob(jobUi, e.message, used);
  }
}
window.createNoteFromDrafts = () => _doCreate(true, false);
window.createAnkiFromDrafts = () => _doCreate(false, true);
window.createBothFromDrafts = () => _doCreate(true, true);

// 启动时刷新 badge
setTimeout(_updateDraftBadge, 200);

function md(s) {
  if (window.marked && marked.parse) {
    try {
      // CJK 与 markdown 强调标记紧贴时 marked 不识别(如 接**-ing**) → 之间插零宽空格，
      // 让 ** / * / ` 有空白边界。不动 $（留给 MathJax 渲染行内/块公式）
      const t = String(s == null ? '' : s)
        .replace(/([一-鿿　-〿＀-￯])([*`])/g, '$1\u200b$2')
        .replace(/([*`])([一-鿿　-〿＀-￯])/g, '$1\u200b$2');
      return marked.parse(t);
    } catch(_) {}
  }
  return String(s == null ? '' : s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\n/g,'<br>');
}

// ── 查询结果历史（per-book localStorage；openResult/closeResult 快照，📜 历史 tab 回看）──
let _restoringHistory = false;
function _qhistKey() { return 'pdf-qhist-' + (FILE_REL || '_'); }
function _loadQueryHistory() { try { return JSON.parse(localStorage.getItem(_qhistKey()) || '[]'); } catch (_) { return []; } }
function _saveQueryHistory(h) { try { localStorage.setItem(_qhistKey(), JSON.stringify(h)); } catch (_) {} }
function _pushQueryHistory() {
  if (_restoringHistory) return;
  const cont = document.getElementById('result-content'); if (!cont) return;
  const html = cont.innerHTML || '';
  const title = cont.dataset.title || '';
  const src = cont.dataset.src || '';
  if (!title && !src) return;
  if (html.length < 40 || (/⏳|class="loading"/.test(html) && html.length < 120)) return;   // 跳过空/纯 loading
  let h = _loadQueryHistory();
  if (h.length && h[0].src === src && h[0].title === title) { h[0].html = html.slice(0, 60000); h[0].time = Date.now(); }
  else h.unshift({ id: 'qh_' + Date.now(), title, src, html: html.slice(0, 60000), time: Date.now() });
  _saveQueryHistory(h.slice(0, 40));
  if (document.querySelector('#side-tabs .side-tab[data-pane="hist"].active')) renderQueryHistory();
}
function _qhFmtTime(t) {
  const d = new Date(t), n = new Date();
  const hm = String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
  return (d.toDateString() === n.toDateString() ? '今天 ' : ((d.getMonth() + 1) + '/' + d.getDate() + ' ')) + hm;
}
window.renderQueryHistory = () => {
  const box = document.getElementById('qhist-list'); if (!box) return;
  const h = _loadQueryHistory();
  if (!h.length) { box.innerHTML = '<div style="color:#5a6680;font-size:12px;padding:10px">还没有查询记录</div>'; return; }
  box.innerHTML = h.map(it =>
    '<div class="qhist-item" data-id="' + it.id + '"><div class="qhist-title">' + _esc(it.title) + '</div>'
    + '<div class="qhist-src">' + _esc((it.src || '').slice(0, 80)) + '</div>'
    + '<div class="qhist-time">' + _qhFmtTime(it.time) + '</div></div>').join('');
  box.querySelectorAll('.qhist-item').forEach(el => el.addEventListener('click', () => {
    const it = _loadQueryHistory().find(x => x.id === el.dataset.id); if (!it) return;
    _restoringHistory = true;
    try { openResult(it.title, it.src, it.html); } finally { _restoringHistory = false; }
    if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([document.getElementById('result-content')]).catch(() => {});
  }));
};

// AI 设置（per-browser localStorage，不动 server-config）
function _getAiOverrides() {
  try { return JSON.parse(localStorage.getItem('pdf-ai-overrides') || '{}'); }
  catch (_) { return {}; }
}
function _toggleAiModelRow() {
  const v = document.getElementById('set-sent-backend').value;
  document.getElementById('set-sent-ai-row').style.display = (v === 'ai') ? '' : 'none';
}
window._toggleAiModelRow = _toggleAiModelRow;

window.openSettings = () => {
  const ov = _getAiOverrides();
  document.getElementById('set-model').value = ov.model || '';
  document.getElementById('set-effort').value = ov.effort || '';
  document.getElementById('set-debug').checked = (localStorage.getItem('pdf-debug') === '1');
  const gv = document.getElementById('set-grammar-view');
  if (gv) gv.value = _grammarViewMode;   // 长句结构显示模式
  renderGrammarTrackList();   // 拉语法跟踪节点列表
  // 拉句子翻译配置
  fetch('/pdf/api/translate-config').then(r => r.json()).then(d => {
    if (!d.ok) return;
    document.getElementById('set-sent-backend').value = d.backend || 'auto';
    document.getElementById('set-sent-model').value = d.model || 'haiku';
    document.getElementById('set-sent-effort').value = d.effort || 'low';
    _toggleAiModelRow();
  }).catch(()=>{});
  const vu = localStorage.getItem('pdf-vocab-underline');
  document.getElementById('set-vocab-underline').checked = (vu === null) ? true : (vu === '1');
  const ct = localStorage.getItem('pdf-click-translate-unmastered');
  document.getElementById('set-click-translate').checked = (ct === null) ? true : (ct === '1');
  renderHlColorSetting();
  document.getElementById('settings-mask').style.display = 'flex';
};
window.closeSettings = () => { document.getElementById('settings-mask').style.display = 'none'; };
window.saveSettings = async () => {
  window.dlog?.('saveSettings 开始');
  try {
    const ov = {
      model:  document.getElementById('set-model')?.value || '',
      effort: document.getElementById('set-effort')?.value || '',
    };
    try { localStorage.setItem('pdf-ai-overrides', JSON.stringify(ov)); } catch (_) {}
    const dbg = document.getElementById('set-debug')?.checked || false;
    try { localStorage.setItem('pdf-debug', dbg ? '1' : '0'); } catch (_) {}
    const vu = document.getElementById('set-vocab-underline')?.checked;
    if (vu !== undefined) {
      try { localStorage.setItem('pdf-vocab-underline', vu ? '1' : '0'); } catch (_) {}
    }
    const ct = document.getElementById('set-click-translate')?.checked;
    if (ct !== undefined) {
      try { localStorage.setItem('pdf-click-translate-unmastered', ct ? '1' : '0'); } catch (_) {}
    }
    // 句子翻译配置 POST 到服务端
    const sb = document.getElementById('set-sent-backend');
    const sm = document.getElementById('set-sent-model');
    const se = document.getElementById('set-sent-effort');
    if (sb && sm && se) {
      try {
        const r = await fetch('/pdf/api/translate-config', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({backend: sb.value, model: sm.value, effort: se.value}),
        });
        const d = await r.json();
        window.dlog?.('translate-config POST: ' + (d.ok ? 'OK' : 'FAIL ' + (d.error||'?')));
        if (!d.ok) _toast?.('句子翻译设置保存失败：' + (d.error||'?'));
      } catch (e) {
        window.dlog?.('translate-config POST exception: ' + e.message, '#ff6b6b');
        _toast?.('句子翻译设置保存失败：' + e.message);
      }
    }
    _applyDebugVisibility();
    closeSettings();
    if (pdfDoc) renderPage(currentPage);
    window.dlog?.('saveSettings 完成');
  } catch (ex) {
    window.dlog?.('saveSettings ERROR: ' + ex.message, '#ff6b6b');
    _toast?.('设置保存出错：' + ex.message);
  }
};
function _applyDebugVisibility() {
  const el = document.getElementById('debug-log');
  if (!el) return;
  el.style.display = (localStorage.getItem('pdf-debug') === '1') ? '' : 'none';
}
// 启动时按 localStorage 设 debug 显示（默认隐藏）
if (localStorage.getItem('pdf-debug') !== '1') {
  if (document.readyState !== 'loading') _applyDebugVisibility();
  else document.addEventListener('DOMContentLoaded', _applyDebugVisibility);
  setTimeout(_applyDebugVisibility, 0);
}

// 抗断连流式 AI:SSE 主路(桌面端流式打字),iPad 切后台/网抖断了 → 回退轮询 /api/ai-stream-result
// 拿后台线程已生成的完整文本(断点不丢)。onText(累计全文) 实时回调(内部~80ms 节流)。返回 {ok,text,error}。
// 后端 rid 路由:translate/explain/grammar POST body.rid;dict-jp-ai GET ?rid=。
async function _aiStream(url, opts) {
  opts = opts || {};
  const method = (opts.method || 'GET').toUpperCase();
  const rid = 'r' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
  let furl = url, body = null;
  const headers = { 'Accept': 'text/event-stream' };
  if (method === 'GET') {
    furl += (url.indexOf('?') >= 0 ? '&' : '?') + 'rid=' + encodeURIComponent(rid);
  } else {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(Object.assign({}, opts.body || {}, { rid }));
  }
  const onTextRaw = opts.onText || (() => {});
  let acc = '', finished = false, usingPoll = false, lastCb = 0, cbTimer = null;
  const ctrl = new AbortController();
  return new Promise((resolve) => {
    const emit = (force) => {                  // onText 节流(~80ms),结束时强制
      if (finished && !force) return;
      const now = Date.now();
      if (force || now - lastCb >= 80) { lastCb = now; if (cbTimer) { clearTimeout(cbTimer); cbTimer = null; } try { onTextRaw(acc); } catch (_) {} }
      else if (!cbTimer) cbTimer = setTimeout(() => { cbTimer = null; lastCb = Date.now(); try { onTextRaw(acc); } catch (_) {} }, 80 - (now - lastCb));
    };
    const cleanup = () => { document.removeEventListener('visibilitychange', onVis); if (cbTimer) clearTimeout(cbTimer); try { ctrl.abort(); } catch (_) {} };
    const finish = (ok, error) => { if (finished) return; finished = true; try { onTextRaw(acc); } catch (_) {} cleanup(); resolve({ ok, text: acc, error: error || '' }); };
    // 回退轮询:后台线程仍在跑,拉它已生成的完整文本
    let pollN = 0;
    const poll = async () => {
      if (finished) return;
      pollN++;
      try {
        const r = await fetch('/pdf/api/ai-stream-result?id=' + encodeURIComponent(rid));
        const d = await r.json();
        if (typeof d.full === 'string' && d.full.length > acc.length) { acc = d.full; emit(); }
        if (d.status === 'done') return finish(true);
        if (d.status === 'error') return finish(false, d.error || 'AI 失败');
        if (d.status === 'unknown' && pollN > 3) return finish(!!acc, acc ? '' : '任务丢失(服务重启?)');
      } catch (_) { /* 网瞬断:继续 */ }
      if (pollN > 240) return finish(!!acc, acc ? '' : '轮询超时');   // ~5min 兜底
      setTimeout(poll, 1200);
    };
    const startPoll = () => { if (usingPoll || finished) return; usingPoll = true; try { ctrl.abort(); } catch (_) {} poll(); };
    // 切后台→回前台:iOS 挂起 JS 会让 SSE reader 卡死,主动转轮询
    const onVis = () => { if (document.visibilityState === 'visible' && !finished) startPoll(); };
    document.addEventListener('visibilitychange', onVis);
    // SSE 主路
    (async () => {
      try {
        const r = await fetch(furl, { method, headers, body, signal: ctrl.signal });
        const ct = r.headers.get('content-type') || '';
        if (!r.ok || !ct.includes('event-stream')) {   // 服务端没流式(多半错误 JSON) → 当普通 JSON
          let d = {}; try { d = await r.json(); } catch (_) {}
          if (d && d.ok && (d.translation || d.explanation)) { acc = d.translation || d.explanation; return finish(true); }
          return finish(false, (d && d.error) || ('HTTP ' + r.status));
        }
        const reader = r.body.getReader(), dec = new TextDecoder();
        let buf = '';
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          if (usingPoll || finished) return;
          buf += dec.decode(value, { stream: true });
          let idx;
          while ((idx = buf.indexOf('\n\n')) >= 0) {
            if (usingPoll || finished) return;
            const ev = buf.slice(0, idx); buf = buf.slice(idx + 2);
            if (/^event:\s*error/m.test(ev)) { let er = ''; const m = ev.match(/^data:\s*(.*)/m); try { er = JSON.parse(m[1]).error; } catch (_) {} return finish(false, er || 'AI 失败'); }
            if (/^event:\s*done/m.test(ev)) return finish(true);
            const m = ev.match(/^data:\s*(.*)/m);
            if (m) { try { const j = JSON.parse(m[1]); if (j.text) { acc += j.text; emit(); } } catch (_) {} }
          }
        }
        if (!finished && !usingPoll) startPoll();   // 流断了没收到 done → 转轮询补全
      } catch (e) {
        if (!finished && !usingPoll) startPoll();    // SSE 出错/abort → 后台线程仍在跑,转轮询
      }
    })();
  });
}

async function aiCall(path, body, label) {
  const ov = _getAiOverrides();
  if (ov.model)  body.model  = ov.model;
  if (ov.effort) body.effort = ov.effort;
  // 预览框显示「实际要处理的文本」(body.text，短选区已扩成整句) → 预览=输入，所见即所处理
  openResult(label, body.text || body.sentence || lastSelText, '<div class="loading">⏳ AI 处理中…</div>');
  const myReq = _resultReqId;   // 本次 AI 调用的请求序号；被新结果框作废后渲染一律丢弃
  const contentEl = document.getElementById('result-content');
  const render = (text) => {              // 渲染累计全文（marked + MathJax）
    if (myReq !== _resultReqId) return;   // 已被新结果框作废 → 不写回（防延迟流式覆盖新结果）
    contentEl.innerHTML = md(text || ' ');
    if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([contentEl]).catch(() => {});
  };
  try {
    // 抗断连：SSE 主路 + 切后台/网抖回退轮询（后台线程跑完，结果不丢）
    const res = await _aiStream(path, { method: 'POST', body, onText: render });
    if (myReq !== _resultReqId) return;
    render(res.text);
    if (!res.ok) contentEl.innerHTML += '<div style="color:#c00;margin-top:8px">✗ ' + (res.error || '失败') + '</div>';
    addResultPickers();   // 完成后给标题加 +
  } catch (e) {
    if (myReq === _resultReqId) contentEl.innerHTML = '<div style="color:#c00">✗ ' + e.message + '</div>';
  }
}

// 复制选中文本（char-layer 自定义选中，浏览器原生 selection 是空的）
window.onCopySel = async () => {
  const t = (lastSelText || '').trim();
  if (!t) { _toast?.('没有选中内容'); return; }
  try {
    await navigator.clipboard.writeText(t);
    _toast?.('已复制');
  } catch (e) {
    // 回退：临时 textarea + execCommand（http / 无权限场景）
    try {
      const ta = document.createElement('textarea');
      ta.value = t; ta.style.position = 'fixed'; ta.style.left = '-9999px';
      document.body.appendChild(ta); ta.select();
      document.execCommand('copy'); ta.remove();
      _toast?.('已复制');
    } catch (e2) { _toast?.('复制失败'); }
  }
};
// Ctrl/Cmd+C：char-layer 有选中时，把选中文本写进剪贴板（绕过空的原生 selection）
document.addEventListener('copy', (e) => {
  const t = (lastSelText || '').trim();
  if (!t || !(window.getSelection && String(window.getSelection()).length === 0)) return;
  // 仅当原生 selection 为空(说明是 char-layer 选中)时接管
  e.clipboardData.setData('text/plain', t);
  e.preventDefault();
});

// 从选中 char 范围构造一个"句子标记"对象(几何同 vocab 句子，PDF pt)，供手动翻译标记用
function _buildSentenceFromSel(pw, sIdx, eIdx) {
  const chars = pw.__charBoxes;
  if (!chars || sIdx < 0 || eIdx >= chars.length || sIdx > eIdx) return null;
  const rects = []; let cur = null, firstC = null, lastC = null, text = '';
  for (let i = sIdx; i <= eIdx; i++) {
    const c = chars[i];
    if (c.sp) { text += ' '; continue; }
    const x0 = c._x0, y0 = c._y0, x1 = c._x1, y1 = c._y1;
    if (x0 == null) continue;
    if (!firstC) firstC = [x0, y0, x1, y1];
    lastC = [x0, y0, x1, y1];
    text += c.c;
    if (cur && Math.abs(y0 - cur[1]) <= (y1 - y0) * 0.6) {
      cur[2] = Math.max(cur[2], x1); cur[1] = Math.min(cur[1], y0); cur[3] = Math.max(cur[3], y1);
    } else {
      if (cur) rects.push(cur.map(v => +v.toFixed(2)));
      cur = [x0, y0, x1, y1];
    }
  }
  if (cur) rects.push(cur.map(v => +v.toFixed(2)));
  text = text.replace(/\s+/g, ' ').trim();
  if (!rects.length || !text || !firstC) return null;
  return {text, rects, first_char: firstC.map(v => +v.toFixed(2)), last_char: lastC.map(v => +v.toFixed(2)),
          count: 0, lemmas: [], total_words: 0, manual: true};
}

window.onTranslate = async () => {
  if (!lastSelText) return;
  toolbar.classList.remove('open');
  const pw = _charSel && _charSel.pw;
  // 无 char 信息(罕见) → 退回大框 AI 翻译
  if (!pw || !pw.__charBoxes) {
    aiCall('/pdf/api/translate', {text: lastSelText, target_lang: '中文'}, '🌐 翻译');
    return;
  }
  // 选中句 → 生成句子标记(L框/box)，box 呼吸表示翻译中；译完存 sidecar + 自动弹译文浮层
  const sent = _buildSentenceFromSel(pw, _charSel.startIdx, _charSel.endIdx);
  if (!sent) { aiCall('/pdf/api/translate', {text: lastSelText, target_lang: '中文'}, '🌐 翻译'); return; }
  sent.__translating = true;
  pw.__vocabSentences = (pw.__vocabSentences || []).filter(s => s.text !== sent.text);
  pw.__vocabSentences.push(sent);
  renderVocabSentences(pw, pw.__vocabSentences);   // 呼吸 box
  try {
    const ov = _getAiOverrides();
    const r = await fetch('/pdf/api/translate-sentence', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        text: sent.text, model: ov.model || '', effort: ov.effort || '',
        file: FILE_REL, sentence: {rects: sent.rects, first_char: sent.first_char, last_char: sent.last_char, page: currentPage},
      }),
    });
    const d = await r.json();
    sent.__translating = false;
    if (d.ok && d.zh) {
      sent.zh = d.zh;
      renderVocabSentences(pw, pw.__vocabSentences);   // 停呼吸
      setTimeout(() => {   // 翻完自动画就地覆盖(直接画,不依赖找按钮 → 更可靠)
        const layer = pw.querySelector('.vocab-layer');
        if (!layer) return;
        const canvas = pw.querySelector('canvas');
        const cssW = canvas?.clientWidth || pw.clientWidth;
        const cssH = canvas?.clientHeight || pw.clientHeight;
        const sx = cssW / (pw.__pageWPt || cssW), sy = cssH / (pw.__pageHPt || cssH);
        const si = pw.__vocabSentences.indexOf(sent);
        const btn = pw.querySelector('.vocab-sentence-btn-l-start[data-sid="' + si + '"]')
          || {classList: {add() {}, remove() {}}};
        _drawSentenceOverlay(layer, sent, btn, sx, sy);
      }, 60);
    } else {
      renderVocabSentences(pw, pw.__vocabSentences);
      _toast('翻译失败：' + (d.error || '?'));
    }
  } catch (e) {
    sent.__translating = false;
    renderVocabSentences(pw, pw.__vocabSentences);
    _toast('翻译错误：' + e.message);
  }
};
window.onExplain = () => {
  if (!lastSelText) return;
  toolbar.classList.remove('open');
  let context = '';
  let explainText = lastSelText;   // 给 AI 解释的主体；短选区时换成所在整句
  if (_charSel && _charSel.pw && _charSel.pw.__charBoxes) {
    const chars = _charSel.pw.__charBoxes;
    const sLen = lastSelText.length;
    if (sLen < 50) {
      // 短选区(词/碎片，如跨行的 "at"+"or") → 以所在整句为解释主体，段落作上下文，
      // 避免 AI 纠结孤立碎词"内容不完整"
      const sR = _expandSentenceFromRange(chars, _charSel.startIdx, _charSel.endIdx);
      if (sR) {
        const sentence = _charsRangeToText(chars, sR.start, sR.end);
        if (sentence && sentence.length > sLen) explainText = sentence;
        const p1 = _paragraphExpandFromChar(chars, sR.start);
        const p2 = _paragraphExpandFromChar(chars, sR.end);
        if (p1 && p2) {
          const para = _charsRangeToText(chars, Math.min(p1.start, p2.start), Math.max(p1.end, p2.end));
          if (para.length > explainText.length) context = para;
        }
      }
    } else if (sLen < 300) {
      // 中选区 → 段落作上下文
      const p1 = _paragraphExpandFromChar(chars, _charSel.startIdx);
      const p2 = _paragraphExpandFromChar(chars, _charSel.endIdx);
      if (p1 && p2) {
        const cr = {start: Math.min(p1.start, p2.start), end: Math.max(p1.end, p2.end)};
        if (cr.start < _charSel.startIdx || cr.end > _charSel.endIdx)
          context = _charsRangeToText(chars, cr.start, cr.end);
      }
    }
  }
  _resultContext = _charSel ? {
    charSel: {pw: _charSel.pw, startIdx: _charSel.startIdx, endIdx: _charSel.endIdx},
    text: lastSelText, sentence: explainText, kind: 'explain',
  } : null;
  aiCall('/pdf/api/explain', {text: explainText, context}, '💡 AI 解释');
};
// 多词选中「💬 对话」：开对话框，预填原文 + 句子/段落上下文(也作 AI 上下文)，底部追问框多轮问
window.onChat = () => {
  if (!lastSelText) return;
  toolbar.classList.remove('open');
  let context = '';
  if (_charSel && _charSel.pw && _charSel.pw.__charBoxes) {
    const chars = _charSel.pw.__charBoxes;
    const cr = _expandSentenceFromRange(chars, _charSel.startIdx, _charSel.endIdx);
    if (cr) {
      const p1 = _paragraphExpandFromChar(chars, cr.start);
      const p2 = _paragraphExpandFromChar(chars, cr.end);
      if (p1 && p2) context = _charsRangeToText(chars, Math.min(p1.start, p2.start), Math.max(p1.end, p2.end));
      else context = _charsRangeToText(chars, cr.start, cr.end);
    }
  }
  _resultContext = _charSel ? {
    charSel: {pw: _charSel.pw, startIdx: _charSel.startIdx, endIdx: _charSel.endIdx},
    text: lastSelText, sentence: context || lastSelText, kind: 'chat',
  } : null;
  let html = '<div style="font-size:12.5px;line-height:1.65">'
    + '<div style="color:#a8cdff;font-weight:600;margin-bottom:3px">📌 原文</div>'
    + '<div style="color:#cfe6ff;white-space:pre-wrap">' + _esc(lastSelText) + '</div>';
  if (context && context.trim() !== lastSelText.trim()) {
    html += '<div style="color:#a8cdff;font-weight:600;margin:10px 0 3px">📖 上下文</div>'
      + '<div style="color:#8a9bb4;white-space:pre-wrap">' + _esc(context) + '</div>';
  }
  html += '<div style="margin-top:12px;color:#5a6680">↓ 在下方输入问题，AI 会结合原文和上下文回答</div></div>';
  openResult('💬 AI 对话', lastSelText, html);
  setTimeout(() => {
    const i = document.getElementById('result-followup-input');
    if (i) { i.placeholder = '问 AI（已带原文 + 上下文）…'; i.focus(); }
  }, 120);
};
window.onToQA = () => {
  if (!lastSelText) return;
  toolbar.classList.remove('open');
  const question = prompt('针对选中内容你想问 AI 什么？（如：解释里面的术语 / 这步推导为何成立 / 给一个例子）');
  if (!question || !question.trim()) return;
  // 复用 aiCall → SSE 流式 + Markdown/MathJax 渲染
  const oldSel = lastSelText;
  lastSelText = '问：' + question + '\n\n（基于选中：' + oldSel.slice(0, 100) +
                (oldSel.length > 100 ? '…' : '') + '）';
  aiCall('/pdf/api/explain', {
    text: question,
    context: '用户选中的教材片段：\n' + oldSel.slice(0, 3000),
  }, '💬 问 AI');
  setTimeout(() => { lastSelText = oldSel; }, 200);
};
window.onToNote = async () => {
  if (!lastSelText) return;
  toolbar.classList.remove('open');
  const name = prompt('请输入笔记名（不含 .md）：', '');
  if (name === null) return;
  const trimmed = (name || '').trim();
  if (!trimmed) return;
  openResult('📝 创建笔记', lastSelText, '<div class="loading">⏳ 正在写入…</div>');
  try {
    const r = await fetch('/pdf/api/to-note', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({text: lastSelText, name: trimmed, file: FILE_REL, page: currentPage}),
    });
    const d = await r.json();
    if (d.ok) {
      const safeUrl = (d.obsidian_url || '').replace(/'/g,"\\'");
      document.getElementById('result-content').innerHTML =
        '<div>✓ 笔记已创建：<code>' + d.note_path + '</code>（含 PDF 来源引用）</div>' +
        '<div style="margin-top:10px"><button onclick="location.href=\'' + safeUrl + '\'" style="background:#1a2540;border:1px solid #3b6db5;color:#cfe6ff;border-radius:6px;padding:6px 14px;cursor:pointer">📂 在 Obsidian 中打开</button></div>';
    } else {
      document.getElementById('result-content').innerHTML = '<div style="color:#c00">✗ ' + (d.error || '失败') + '</div>';
    }
  } catch (e) {
    document.getElementById('result-content').innerHTML = '<div style="color:#c00">✗ ' + e.message + '</div>';
  }
};

// 键盘快捷键
document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT') return;
  if (e.key === 'ArrowRight' || e.key === 'PageDown' || e.key === ' ') { e.preventDefault(); changePage(1); }
  else if (e.key === 'ArrowLeft' || e.key === 'PageUp') { e.preventDefault(); changePage(-1); }
  else if (e.key === 'Escape') { closeResult(); toolbar.classList.remove('open'); }
});

loadPdf();
