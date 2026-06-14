async function renderPage(num) {
  if (!pdfDoc) return;
  num = Math.max(1, Math.min(pdfDoc.numPages, parseInt(num) || 1));
  currentPage = num;
  { const _pc = document.getElementById('page-cur'); if (_pc) _pc.textContent = num; }
  window._refreshVocabIfPage?.();   // 离散翻页(◀▶/滑块/跳页)也刷新「本页」单词本(连续模式下 loadPageNodes 只靠滚动触发,会漏)
  if (readMode !== 'single') {   // 连续 / 双页:滚到对应页占位 + 立即渲染
    // 连续模式：滚到对应页占位 + 立即渲染目标页(别等 IO,跳页/翻页不卡)
    const ph = document.querySelector(`[data-page-num="${num}"]`);
    if (ph) {
      ph.scrollIntoView({block: 'start', behavior: 'auto'});
      if (ph.dataset.loaded === '0') _renderPageInto(num, ph).catch(() => {});
    }
    return;
  }
  // 强力兜底:单页 page-container 里若残留多页/双页结构(.page-wrap/.spread-row),先无条件清空 + 复位
  // loaded。否则 _renderPageImg 在 decode 竞态/scale 漂移时会提前 return、不执行 innerHTML='' → 残留的多页
  // 结构留着 = 「单页缩放却显示多页」(iPad 真机捏合复现,headless 不必现)。清了再渲,保证单页只剩一页。
  const _pc = document.getElementById('page-container');
  if (_pc.querySelector('.page-wrap, .spread-row')) { _pc.innerHTML = ''; _pc.dataset.loaded = '0'; }
  await _renderPageInto(num, _pc, true);
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
  // ⚡ 叠层必须撑满**整张位图**(2026-06-10 修):inset:0 的层(char-layer/vocab/ruby/hl 等)
  // 只有裁后宽高,translate(-cropL,-cropT) 后可见页面的**右侧 cropL、底部 cropT 条带没有层覆盖**
  // → 点击落到 <img> 上,选词全失效(振假名 overflow 照画 → "有浮层但点不中"的根因)。
  // CSS 里 .crop-on 的叠层改用这两个变量定宽高(sel-overlay/ink 本就内联整宽,不受影响)。
  wrap.style.setProperty('--full-w', cw + 'px');
  wrap.style.setProperty('--full-h', ch + 'px');
}

// 后台预取当前页前后若干页的图(read-ahead,大型文档网站标配):低优先级 fetch → 落进浏览器缓存
// → 翻到时瞬开。已加载/已预取的页本就被 HTTP immutable 缓存,不重复取(_prefetched 去重)。
// 页图栅格宽**只增不减**（按页记最大用过的 w）。缩小(zoom out)时不降栅格 → 复用已缓存的更大图、
// 浏览器降采样显示(清晰),不换 src 重新 fetch → 消除"缩小一下整页白屏重新加载"。放大超过当前栅格才取更高清。
const _imgRasterW = {};
function _ratchetReqW(page, w) {
  const rw = Math.max(w, _imgRasterW[page] || 0);
  _imgRasterW[page] = rw;
  return rw;
}
const _prefetched = new Set();
function _prefetchAround(num, radius) {
  if (!_imgMode) return;
  const meta = window.__imgMeta; if (!meta) return;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const cw = Math.floor(meta.page_w * scale);
  const baseW = Math.max(400, Math.min(2400, Math.round(cw * dpr)));
  const R = radius || 3;
  const want = [];
  for (let d = 1; d <= R; d++) { want.push(num + d); if (num - d >= 1) want.push(num - d); }   // 偏向后页(顺序阅读)
  for (const p of want) {
    if (p < 1 || p > meta.page_count) continue;
    const reqW = _ratchetReqW(p, baseW);   // 跟渲染用同一 ratchet → 缓存键一致
    const key = p + ':' + reqW;
    if (_prefetched.has(key)) continue;
    _prefetched.add(key);
    const url = '/pdf/api/page-image?file=' + encodeURIComponent(FILE_REL) + '&page=' + p + '&w=' + reqW + '&v=' + (meta.mtime || 0);
    try {
      fetch(url, { priority: 'low' }).then(r => r.blob()).catch(() => _prefetched.delete(key));   // 消费响应 → 进缓存;不抢交互带宽
    } catch (_) { _prefetched.delete(key); }
  }
}
window._prefetchAround = _prefetchAround;
// 图片模式渲染:用服务端渲染好的页图(<img>)代替 PDF.js canvas。叠层(选词 char 层/高亮/振假名/墨迹)
// 全是按坐标定位,跟 canvas 路径一样工作。只取这一页的图(几百 KB),不下载整本 PDF。
async function _renderPageImg(num, wrap, viewport) {
  const _gen = (wrap.__imgGen = (wrap.__imgGen || 0) + 1);   // 重入守卫:并发/IO 重渲时,旧渲染 decode 完别覆盖新渲染(最后发起的赢)
  const cw = Math.floor(viewport.width);
  let ch = Math.floor(viewport.height);   // 初值用 meta(page1)高,decode 后改用本页图的真实宽高比(见下)
  // **渲染分辨率与显示宽度解耦**:基准=页**原生点宽**(扫描书=原生像素宽),显示多大由客户端 CSS 缩放决定。
  // → 适应阅读时 reqW=原生宽(与窗口/缩放级别无关) → 预热一次即覆盖任意窗口大小,不会"换窗口就没命中"。
  //   只有手动放大到超过原生(cw×dpr>原生)才按 dpr 提分辨率防糊。reqW 只增不减(缩小复用大图→不闪)。封顶 2400。
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const _natW = Math.round((window.__imgMeta && window.__imgMeta.page_w) || cw);
  const reqW = _ratchetReqW(num, Math.max(400, Math.min(2400, Math.max(_natW, Math.round(cw * dpr)))));
  const mt = (window.__imgMeta && window.__imgMeta.mtime) || 0;
  const img = document.createElement('img');
  img.className = 'page-img'; img.decoding = 'async';
  img.src = '/pdf/api/page-image?file=' + encodeURIComponent(FILE_REL) + '&page=' + num + '&w=' + reqW + '&v=' + mt;
  // **先把新页图 decode 好再换**:旧内容/旧图一直可见到此刻 → 去边/缩放/侧栏等重渲染无空白闪烁(cache 命中=秒回)
  try { await img.decode(); } catch (_) {}
  if (!wrap.isConnected || wrap.__imgGen !== _gen) return;   // 解码期间该页已被释放 / 已有更新的渲染 → 放弃
  if (img.naturalWidth === 0) return;   // decode 失败(catch 吞掉)→ 别换入空/坏图,留旧内容;loaded 仍 0,IO 滚到时重试
  // 自愈:decode 这段异步窗口里全局 scale 变了(缩放/切模式与渲染赛跑)→ 别用旧 scale 的图换入,否则本页
  // 定格在旧 scale(其它页已新 scale → 行间大小不一)。按当前 scale 重渲;__imgGen 守卫防叠加,scale 稳定后
  // viewport.scale==scale → 不再触发,自然收敛。(__imgGen 只防同页两渲染互覆盖,挡不住"用旧全局 scale 发起的渲染")
  if (Math.abs(scale - (viewport.scale || 1)) > 0.005) {
    const _p2 = await pdfDoc.getPage(num);
    return _renderPageImg(num, wrap, _p2.getViewport({ scale }));
  }
  // ⚡ 根治「越往下错位越严重」:扫描书**每页高度不同**,但图片模式 shim 对所有页都用 page1 的 meta 高
  // (ch=floor(meta.page_h×scale))→ 本页图被压/拉到 page1 高度显示,而 char 层按本页**真实**高度铺坐标
  // → 纵向比例不一致、误差随 y 累积(顶部不偏、底部最偏)。改用**图自身真实宽高比**算显示高(宽统一→对齐准)。
  ch = Math.max(1, Math.round(cw * (img.naturalHeight / img.naturalWidth)));
  img.style.width = cw + 'px'; img.style.height = ch + 'px'; img.style.display = 'block';
  wrap.innerHTML = '';
  wrap.__charLayer = null; wrap.__charBoxes = null; wrap.__inkStrokes = null;
  wrap.style.width = ''; wrap.style.height = '';
  wrap.style.background = ''; wrap.style.color = '';
  wrap.style.display = ''; wrap.style.alignItems = ''; wrap.style.justifyContent = '';
  wrap.dataset.pageNum = num;
  _applyCropToWrap(wrap, cw, ch);   // 去边:渲染前先收成裁切窄宽(子层 translate)
  wrap.appendChild(img);
  const selOverlay = document.createElement('div');   // 选中高亮叠层(同 canvas 路径)
  selOverlay.className = 'sel-overlay';
  selOverlay.style.width = cw + 'px'; selOverlay.style.height = ch + 'px';
  wrap.appendChild(selOverlay);
  const inkCanvas = document.createElement('canvas');   // 手写墨迹层
  inkCanvas.className = 'ink-layer';
  inkCanvas.style.width = cw + 'px'; inkCanvas.style.height = ch + 'px';
  const _inkDpr = window.devicePixelRatio || 1;
  inkCanvas.width = Math.floor(cw * _inkDpr); inkCanvas.height = Math.floor(ch * _inkDpr);
  wrap.appendChild(inkCanvas); wrap.__inkCanvas = inkCanvas;
  if (!wrap.__inkBound) {
    wrap.addEventListener('pointerdown', (e) => { if (window._inkPointerDown) window._inkPointerDown(e); }, true);
    const _blk = (e) => { for (const t of e.touches) { if (t.touchType === 'stylus') { e.preventDefault(); break; } } };
    wrap.addEventListener('touchstart', _blk, { passive: false });
    wrap.addEventListener('touchmove', _blk, { passive: false });
    wrap.__inkBound = true;
  }
  // char 层(PyMuPDF chars:选词/高亮/振假名/搜索)。它只用 viewport.scale + 自己做坐标转换 → shim viewport 够。
  loadCharsAndBindLayer(num, wrap, viewport).catch(e => window.dlog?.('chars load fail: ' + (e && e.message)));
  wrap.__inkStrokes = (window._ink && window._ink.byPage[num]) ? JSON.parse(JSON.stringify(window._ink.byPage[num])) : [];
  if (window._inkRedraw) window._inkRedraw(wrap);
  _applyCropToWrap(wrap, cw, ch);
  wrap.__renderScale = scale;   // 记录渲染时的 scale → 缩放重排时按比例 zoom 现有位图(过渡期补偿)
  wrap.style.zoom = '';   // 本页位图已是当前 scale 原生像素 → 撤掉缩放过渡期的补偿 zoom(回到 1)
  wrap.dataset.loaded = '1';
  if (window._updateMainOverflowX) requestAnimationFrame(window._updateMainOverflowX);
  setTimeout(() => _prefetchAround(num), 400);   // 渲染完当前页 → 后台预取前后页(延后,先让当前页图到位)
  if (window._maybeAutoPrewarm) window._maybeAutoPrewarm();   // 首页渲完(scale/宽度已定)→ 自动后台预热整本(只触发一次)
  if (readMode === 'single') {
    const u = new URL(location.href); u.searchParams.set('page', num); history.replaceState(null, '', u);
    loadPageNodes(num);
  }
}
async function _renderPageInto(num, wrap) {
  if (!pdfDoc) return;
  if (wrap.dataset.loaded === '1') return;
  const page = await pdfDoc.getPage(num);
  const viewport = page.getViewport({scale});
  if (_imgMode) { await _renderPageImg(num, wrap, viewport); return; }   // 图片模式:渲染服务端页图,不用 canvas/PDF.js
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
  // 去边:**渲染前**就把 wrap 收成裁切窄宽 + 加 .crop-on(overflow:hidden + 子层 translate)。
  // 否则 page.render()/getTextContent() 那几百 ms 异步窗口里 wrap 是全宽 canvas、还没 crop,
  // 该页会短暂显示成全宽未裁切(双页模式下表现为"有些页比别的宽")。子层 CSS 规则 .crop-on>*
  // 会自动 translate 之后 append 进来的每个层,故此处先设无妨。
  _applyCropToWrap(wrap, cw, ch);
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
  // wrap 级监听只绑一次(卸载→重渲染同一 wrap 时不重复绑定,否则累积成内存泄漏 + 多次触发)
  if (!wrap.__inkBound) {
    wrap.addEventListener('pointerdown', (e) => { if (window._inkPointerDown) window._inkPointerDown(e); }, true);
    // iOS：Apple Pencil 触摸(touchType=stylus)阻止其默认滚动，手指(direct)放行 → 笔不滚页、手指照常滚
    const _inkBlockStylusScroll = (e) => {
      for (const t of e.touches) { if (t.touchType === 'stylus') { e.preventDefault(); break; } }
    };
    wrap.addEventListener('touchstart', _inkBlockStylusScroll, { passive: false });
    wrap.addEventListener('touchmove', _inkBlockStylusScroll, { passive: false });
    wrap.__inkBound = true;
  }

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
  // 自愈:渲染期间(render/getTextContent/tl.render 几段 await)全局 scale 变了 → 本页定格旧 scale
  // (与缩放/切模式赛跑,行间大小不一)。按当前 scale 重渲;__imgGen 防叠加,scale 稳定后自然收敛。
  if (wrap.isConnected && Math.abs(scale - (viewport.scale || 1)) > 0.005) {
    wrap.dataset.loaded = '0';
    return _renderPageInto(num, wrap);
  }
  wrap.__renderScale = scale;   // 记录渲染时 scale → 缩放重排按比例 zoom 现有位图(过渡期补偿)
  wrap.style.zoom = '';   // 本页已是当前 scale 原生像素 → 撤掉补偿 zoom(回到 1),canvas 模式同样
  wrap.dataset.loaded = '1';
  if (window._updateMainOverflowX) requestAnimationFrame(window._updateMainOverflowX);   // 渲染后据实测内容宽锁/放横向滚动

  // 同步 URL + 拉 KG 节点：只在单页模式做
  if (readMode === 'single') {
    const u = new URL(location.href);
    u.searchParams.set('page', num);
    history.replaceState(null, '', u);
    loadPageNodes(num);
  }
}

