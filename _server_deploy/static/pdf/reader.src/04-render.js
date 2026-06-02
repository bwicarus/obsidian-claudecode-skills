async function renderPage(num) {
  if (!pdfDoc) return;
  num = Math.max(1, Math.min(pdfDoc.numPages, parseInt(num) || 1));
  currentPage = num;
  { const _pc = document.getElementById('page-cur'); if (_pc) _pc.textContent = num; }
  if (readMode !== 'single') {   // 连续 / 双页:滚到对应页占位 + 立即渲染
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

// 去边模式：把 page-wrap 裁成可见区(width/height=可见尺寸 + overflow:hidden),并通过 CSS
// 给所有子层(canvas/textLayer/char层/ruby/句子层…)加**同一个 translate** 位移到裁切原点。
// 纯位移→各层相对位置不变、选中坐标(ptToLocal 用 getBoundingClientRect)自动跟随,不会错位。
// canvas/层 CSS 尺寸仍是整页(cw×ch);裁切只靠 wrap 窗口 + 子层位移。
function _applyCropToWrap(wrap, cw, ch) {
  if (!_cropActive()) {
    wrap.classList.remove('crop-on');
    wrap.style.removeProperty('--crop-l');
    wrap.style.removeProperty('--crop-t');
    return;
  }
  const fl = _crop.l / 100, fr = _crop.r / 100, ft = _crop.t / 100, fb = _crop.b / 100;
  wrap.classList.add('crop-on');
  wrap.style.width = Math.max(1, Math.floor(cw * (1 - fl - fr))) + 'px';
  wrap.style.height = Math.max(1, Math.floor(ch * (1 - ft - fb))) + 'px';
  wrap.style.setProperty('--crop-l', (cw * fl).toFixed(1) + 'px');
  wrap.style.setProperty('--crop-t', (ch * ft).toFixed(1) + 'px');
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
  // 但 CSS 显示尺寸 = viewport（跟 textLayer 一致 → spans 跟文字对齐）。
  // backing 任一维超 iOS ~4096 会渲染空白 → 高倍放大时**动态降 outputScale**(保 backing≤4096,
  // CSS 尺寸照常放大)→ 缩放上限不再被页高卡死(否则高页 _scaleMax 只有 ~0.7),只是极端放大略软。
  const outputScale = Math.max(0.6, Math.min(window.devicePixelRatio || 1,
    4096 / Math.max(1, Math.max(viewport.width, viewport.height))));
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
  _applyCropToWrap(wrap, cw, ch);   // 去边模式:裁切窗口 + 子层统一位移
  wrap.dataset.loaded = '1';

  // 同步 URL + 拉 KG 节点：只在单页模式做
  if (readMode === 'single') {
    const u = new URL(location.href);
    u.searchParams.set('page', num);
    history.replaceState(null, '', u);
    loadPageNodes(num);
  }
}

