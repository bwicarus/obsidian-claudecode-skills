// ── 00-resilient-fetch.js:全站网络韧性(最先加载,最底层兜住「切后台 Load failed」)──
// iOS 切后台/锁屏会掐死进行中的请求,回前台后该请求 reject「TypeError: Load failed」。这里在 fetch 层统一兜:
//   ① 包装 window.fetch:GET/HEAD(幂等读)瞬断时**先等回到前台、再退避重试**(写请求不自动重试,防重复提交);
//   ② 暴露 window.__safeFetch:给「幂等但用 POST 的计算类请求」(如 AI 分析)显式开启重试。
// 重要边界:fetch() 只在「还没收到响应(连接失败/被掐)」时 reject;一旦返回 Response 就交还调用方,
//   所以**流式响应 body 读到一半断了不归这里管**——那是各功能各自的恢复(助手=拉历史补全、_aiStream=rid 轮询)。
(function () {
  if (window.__resilientFetch || !window.fetch) return;
  window.__resilientFetch = true;
  var _orig = window.fetch.bind(window);

  function whenVisible() {   // 当前在后台 → 等回到前台再继续(后台发请求多半也被掐)
    return new Promise(function (res) {
      if (document.visibilityState !== 'hidden') return res();
      var h = function () { if (document.visibilityState !== 'hidden') { document.removeEventListener('visibilitychange', h); res(); } };
      document.addEventListener('visibilitychange', h);
    });
  }
  function methodOf(input, init) {
    if (init && init.method) return String(init.method).toUpperCase();
    if (input && typeof input === 'object' && input.method) return String(input.method).toUpperCase();
    return 'GET';
  }
  async function run(input, init, maxRetry) {
    var i = 0;
    while (true) {
      try { return await _orig(input, init); }
      catch (e) {
        if (e && e.name === 'AbortError') throw e;     // 主动取消(用户停止 / 代码 abort)→ 不重试
        if (i++ >= maxRetry) throw e;                  // 重试用尽 → 抛回调用方(各自的 catch 兜底)
        await whenVisible();                           // 先回前台
        await new Promise(function (r) { setTimeout(r, Math.min(300 * i, 1200)); });   // 退避
      }
    }
  }
  window.fetch = function (input, init) {
    var m = methodOf(input, init);
    return run(input, init, (m === 'GET' || m === 'HEAD') ? 3 : 0);   // 写请求 maxRetry=0:只跑一次
  };
  // 幂等计算类(POST 但无持久副作用,如 grammar-analyze/translate)可显式要重试
  window.__safeFetch = function (input, init, conf) {
    conf = conf || {};
    return run(input, init, conf.retries == null ? 3 : conf.retries);
  };
})();
// PDF 阅读器主模块(从 pdf_reader.html 内联 <script type="module"> 抽出,2026-06)。
// 配置经 window.__PDF_CFG(模板内联 script 注入:pdf_url/file_rel/page)。架构/全局未变,纯物理拆分。
window.dlog('module script 开始执行（ES module 加载 OK）');
// ── 跨端同步(Kindle/Google Books 做法):iOS 把"装到主屏的 PWA"和 Safari 当成独立存储沙箱 →
//    localStorage 不互通(设置/阅读进度/旋转排版不同步)。进页面**先**把服务端存的本用户 pdf-* 偏好
//    灌进 localStorage(用原始 setItem,不回传),再往下读 localStorage 就是同步后的值。之后任何 pdf-*
//    写入(除大块 qhist)防抖回传服务器 → 设置/进度/排版自动跨设备/跨上下文同步。3s 超时不阻塞阅读。
const _origSetItem = localStorage.setItem.bind(localStorage);
const _prefTouched = new Set();   // 本次会话本地改过的 pdf-* 键:后台刷新不回写它们(防覆盖刚改的值)
async function _seedPrefs() {   // 拉服务端偏好灌进 localStorage(原始 setItem,不回传)
  const _r = await fetch('/pdf/api/prefs', { headers: { Accept: 'application/json' } });
  if (!_r || !_r.ok) return 0;
  const _d = await _r.json();
  let _n = 0;
  if (_d && _d.prefs) { for (const _k in _d.prefs) { if (_prefTouched.has(_k)) continue; try { _origSetItem(_k, _d.prefs[_k]); _n++; } catch (_) {} } }
  return _n;
}
// stale-while-revalidate(SWR,大站通用):本沙箱**首次**才阻塞拉取(否则旋转/设置读到的是空)→ 1.5s 超时不卡死;
// 之后每次用本地已同步值秒开,后台静默刷新供下次打开用(跨设备改动延迟一次打开生效)。__SYNCED 标记区分。
const _SYNC_FLAG = 'pdf-prefs-synced';
try {
  if (localStorage.getItem(_SYNC_FLAG)) {
    _seedPrefs().catch(() => {});   // 已同步过:后台刷新,不阻塞
  } else {
    const _n = await Promise.race([_seedPrefs(), new Promise((res) => setTimeout(() => res(0), 1500))]);
    // 迁移:服务端为空(老用户首次)→ 把本沙箱现有 pdf-* 一次性 push 上去(这些键是在回传 override 之前
    // 写的、不会自动上传)。让"已用 Safari 很久"的设置/进度/排版能被随后打开的 PWA 同步到。
    if (_n === 0) {
      const _boot = {};
      try {
        for (let _i = 0; _i < localStorage.length; _i++) {
          const _k = localStorage.key(_i);
          if (_k && _k.indexOf('pdf-') === 0 && _k.indexOf('pdf-qhist-') !== 0 && _k !== _SYNC_FLAG) _boot[_k] = localStorage.getItem(_k);
        }
        if (Object.keys(_boot).length) fetch('/pdf/api/prefs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ patch: _boot }) });
      } catch (_) {}
    }
    _origSetItem(_SYNC_FLAG, '1');
    window.dlog('✓ 首次从服务器同步 ' + _n + ' 项 PDF 偏好');
  }
} catch (_) { window.dlog('prefs 同步跳过(离线?)'); }
let _prefQueue = {}, _prefTimer = 0;
localStorage.setItem = function (k, v) {
  _origSetItem(k, v);
  if (typeof k === 'string' && k.indexOf('pdf-') === 0 && k.indexOf('pdf-qhist-') !== 0 && k !== _SYNC_FLAG) {
    _prefTouched.add(k);
    _prefQueue[k] = v; clearTimeout(_prefTimer);
    _prefTimer = setTimeout(() => {
      const patch = _prefQueue; _prefQueue = {};
      try { fetch('/pdf/api/prefs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ patch }) }); } catch (_) {}
    }, 1500);
  }
};
// cache buster：避开浏览器/代理的 mime 缓存（之前 nginx 错把 .mjs 当 octet-stream，已修但缓存还在）
const PDFJS_V = '20260526a';
// 图片模式(成熟方案:服务端按页出图,只取看到的页,不下载整本 PDF、且不加载 PDF.js 库)。默认开;localStorage 关作安全阀。
let _imgMode = (() => { try { return localStorage.getItem('pdf-img-mode') !== '0'; } catch (_) { return true; } })();
let pdfjsLib;
if (!_imgMode) {   // 仅经典(PDF.js canvas)模式才下载 2.8MB 库;图片模式跳过 → 省库下载 + 那 5 秒 import 等待
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
} else {
  window.dlog('图片模式:跳过 PDF.js 库(省 2.8MB 下载 + import 等待)');
}
// Service Worker:缓存页图(抗 iOS 定期清缓存 + 离线可读看过的页;PWA 标准做法)。只接管页图,其余放行。
// 作用域 /pdf/ 需 SW 从 /pdf/sw.js 提供。+ 申请持久存储(persist)让缓存更不易被系统清。
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/pdf/sw.js', { scope: '/pdf/' })
    .then(() => window.dlog && window.dlog('✓ Service Worker 已注册(页图持久缓存)'))
    .catch((e) => window.dlog && window.dlog('SW 注册失败(不影响阅读):' + (e && e.message)));
}
try { if (navigator.storage && navigator.storage.persist) navigator.storage.persist(); } catch (_) {}

const PDF_URL = window.__PDF_CFG.pdf_url;
const FILE_REL = window.__PDF_CFG.file_rel;
const PDF_SIZE = (window.__PDF_CFG && +window.__PDF_CFG.size) || 0;   // 文件字节数(决定小文件整本取/大文件 range)
const PDF_COMPRESSED = (window.__PDF_CFG && +window.__PDF_CFG.compressed) || 0;   // 当前是否在用压缩版
const PDF_COMP_AVAIL = (window.__PDF_CFG && +window.__PDF_CFG.comp_avail) || 0;   // 是否存在压缩版(供"加载慢→切压缩版")
// 切换到压缩版打开:记住偏好 + 重载带 &compressed=1
window._switchToCompressed = () => {
  try { localStorage.setItem('pdf-use-compressed:' + FILE_REL, '1'); } catch (_) {}
  const u = new URL(location.href); u.searchParams.set('compressed', '1'); location.href = u.toString();
};
// page-chars 缓存版本 = PDF mtime + 后端分词版本 _CHAR_CACHE_VER。前者在 PDF 变更时刷新,
// 后者在**分词逻辑变更**时刷新(同一 mtime 下也能让 iOS Safari 已缓存的旧分词数据失效 →
// 修了"只能选单字"类 bug 后客户端立刻重取,不必等 PDF mtime 变。配合后端 no-store。
const CHARS_VER = ((String(PDF_URL).match(/[?&]v=(\d+)/) || [])[1] || '2')
  + '.' + ((window.__PDF_CFG && window.__PDF_CFG.chars_ver) || '0');
let BOOK_LANGS = [];   // 本书声明的语言,如 ['en','ja'];影响点词查哪本词典
async function loadBookLangs() {
  try {
    const r = await fetch('/pdf/api/book-langs?file=' + encodeURIComponent(FILE_REL || ''));
    const d = await r.json();
    if (d.ok) BOOK_LANGS = d.langs || [];
  } catch (e) {}
}
// 本书是否开启「插图 AI 描述 + 徽标」(默认关,每本书独立,server 端 by FILE_REL)。off → 不拉 page-figures、不画徽标、不烧 AI
window.__figBookOn = false;
function _rerenderVisibleFigs() {   // 重渲所有已渲染页的徽标(开关切换/进书已开 时即时生效)
  try {
    document.querySelectorAll('.page-wrap[data-loaded="1"]').forEach(function (pw) {
      var pn = +pw.dataset.pageNum;   // ← 属性是 data-page-num(之前误用 dataset.page=NaN → 开了不显示)
      if (pn && window.renderFiguresOnPage) window.renderFiguresOnPage(pw, pn);
    });
  } catch (_) {}
}
window._rerenderVisibleFigs = _rerenderVisibleFigs;
async function loadBookFig() {
  try {
    const r = await fetch('/pdf/api/book-figures?file=' + encodeURIComponent(FILE_REL || ''));
    const d = await r.json();
    window.__figBookOn = !!(d && d.ok && d.enabled);
    if (window.__figBookOn) _rerenderVisibleFigs();   // 进书时本书已开 → 立刻把已渲染页的徽标画上(防 race)
  } catch (e) { window.__figBookOn = false; }
}
window.saveFigToggle = async function(on) {   // 设置面板「本书插图描述」开关,即时 POST
  try {
    const r = await fetch('/pdf/api/book-figures', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ file: FILE_REL, enabled: !!on }),
    });
    const d = await r.json();
    window.__figBookOn = !!(d && d.ok && d.enabled);
    (typeof _toast === 'function') && _toast(window.__figBookOn ? '已开启本书插图描述（翻页后逐页生成，首次需点 AI 几秒）' : '已关闭本书插图描述');
    // 即时反映:开→重渲已渲染页的徽标;关→清掉已画的徽标
    if (window.__figBookOn) _rerenderVisibleFigs();
    else { document.querySelectorAll('.fig-layer').forEach(l => l.innerHTML = ''); }
  } catch (e) { (typeof _toast === 'function') && _toast('保存失败：' + e.message); }
};
window.openLangPicker = function() { window.openSettings?.(); };   // 语言已并入设置面板,旧入口转开设置
window.saveLangPicker = async function() {   // 设置面板「保存本书语言」按钮(每本书独立,POST book-langs by FILE_REL)
  const langs = Array.from(document.querySelectorAll('#lang-checks input:checked')).map(c => c.value);
  try {
    const r = await fetch('/pdf/api/book-langs', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ file: FILE_REL, langs }),
    });
    const d = await r.json();
    if (d.ok) BOOK_LANGS = d.langs || langs;
    (typeof _toast === 'function') && _toast('已保存需要翻译的语言：' + (BOOK_LANGS.join(' / ') || '无(全部免于翻译)'));
  } catch (e) { (typeof _toast === 'function') && _toast('保存失败：' + e.message); }
};
window.dlog('PDF_URL = ' + PDF_URL);
let pdfDoc = null;
let currentPage = window.__PDF_CFG.page;
let scale = 1.4;
let _fitPageW = 0, _fitPageH = 0;   // 首页 @scale1 尺寸缓存(旋转记忆判"当前是否=宽度适应"用)
let _scaleMax = 3.0;   // scale 上限：loadPdf 按页高×dpr 动态算（防 canvas backing 高超 iOS ~4096）
const _ZOOM_MIN = 0.18;   // 用户缩放下限(双指/zoomChange)。放宽到 0.18 → 可缩到比 fit-width 更小(这些书 fit≈0.5)
// 去边阅读模式：每本书可配左/右/上/下各隐藏 %。开启时把可见区填满宽度(fit-width 除以可见宽占比),
// 再给 page-wrap 子层加同一 translate 位移 + overflow:hidden 裁切。纯位移不破坏选中坐标。
let _crop = {l: 0, r: 0, t: 0, b: 0};   // 百分比(后端 /api/book-crop 加载)
let _cropOn = false;                    // 开关(per-book localStorage)
function _cropKey() { return 'pdf-crop-on:' + FILE_REL; }
function _cropActive() { return _cropOn && (_crop.l || _crop.r || _crop.t || _crop.b); }
function _cropVisWFrac() { return _cropActive() ? Math.max(0.1, 1 - (_crop.l + _crop.r) / 100) : 1; }
function _cropVisHFrac() { return _cropActive() ? Math.max(0.1, 1 - (_crop.t + _crop.b) / 100) : 1; }
// 双页(spread)模式：连续滚动、每行 2 页并排。_spreadOffset 0/1 错开facing(0:1|2,3|4… 1:1单+2|3,4|5…)
let _spreadOffset = (() => { try { return localStorage.getItem('pdf-spread-offset:' + FILE_REL) === '1' ? 1 : 0; } catch (_) { return 0; } })();
let _spreadBeforePanel = null;   // 侧栏打开时把双页临时切单列,存被切走的 offset(关栏还原);null=没临时切。不持久。
function _spreadKey() { return 'pdf-spread-offset:' + FILE_REL; }
function _pagesPerRow() { return readMode === 'spread' ? 2 : 1; }
function _spreadRows(total, offset) {   // → [[1,2],[3,4]…] 或 offset=1 [[1],[2,3],[4,5]…]
  const rows = []; let p = 1;
  if (offset === 1 && total >= 1) { rows.push([1]); p = 2; }
  while (p <= total) { const row = [p]; if (p + 1 <= total) row.push(p + 1); rows.push(row); p += 2; }
  return rows;
}
let readMode = (() => {
  const m = new URLSearchParams(location.search).get('mode');   // 技能树书本图标可带 ?mode=continuous
  let v = (m === 'continuous' || m === 'spread') ? m : (localStorage.getItem('pdf-read-mode') || 'continuous');
  if (v === 'single') v = 'continuous';   // **已取消单页模式**:旧 localStorage/URL 的 single 一律当连续
  return v;
})();   // 'continuous' | 'spread'（不再有 'single'）
let _contIO = null;   // IntersectionObserver for 连续模式
let _pendingScrollY = 0;   // 上次位置恢复用(绝对像素,旧记录兜底)
let _pendingFrac = 0;      // 上次位置恢复用(页内比例 0-1,布局无关,优先于 scrollY)
let _pendingPage = 0;      // 恢复目标页(闭包捕获;三连 apply 不读活的 currentPage——它会被滚动处理器反馈式改掉,审计 BUG#3)
let _scrollSaveTimer = null;



// ── 页面叠层工厂(2026-06-10):wrap 直挂的全页叠层一律经这里建 ──
// 统一打 .page-layer 标记:CSS 的 .crop-on>.page-layer 会把层撑满整张位图(--full-w/h)。
// 经此创建的新叠层不可能再踩「去边模式右/下条带没有层 → 点不中」(pdf-reader.md §36⑤)。
function ensurePageLayer(pw, cls) {
  let l = pw.querySelector(':scope > .' + cls);
  if (!l) {
    l = document.createElement('div');
    l.className = cls + ' page-layer';
    pw.appendChild(l);
  } else if (!l.classList.contains('page-layer')) {
    l.classList.add('page-layer');
  }
  return l;
}

// 叠层几何自检(debug 用,console/_auditLayers() 调):层尺寸≠位图 或 charBoxes 烘焙比例≠实时比例 → 列出来。
// 这两类不一致正是「右缘点不中/越往下错位」级 bug 的前兆,让它自己喊出来,别等用户撞上。
window._auditLayers = function () {
  try {
    const out = [];
    for (const w of document.querySelectorAll('.page-wrap[data-loaded="1"]')) {
      const img = w.querySelector('.page-img, canvas');
      if (!img) continue;
      const ew = img.clientWidth, eh = img.clientHeight;
      for (const ch of w.children) {
        if (ch === img || !ch.className || !/layer|overlay/.test(String(ch.className))) continue;
        if (Math.abs(ch.clientWidth - ew) > 2 || Math.abs(ch.clientHeight - eh) > 2)
          out.push('p' + w.dataset.pageNum + ' ' + String(ch.className).split(' ')[0] + ' ' +
                   ch.clientWidth + 'x' + ch.clientHeight + ' ≠ 位图 ' + ew + 'x' + eh);
      }
      const cb = w.__charBoxes;
      if (cb && cb.length && w.__pageWPt && cb[0]._x0 != null && cb[0]._x1 > cb[0]._x0) {
        const live = ew / w.__pageWPt;
        const baked = cb[0].width / (cb[0]._x1 - cb[0]._x0);
        if (Math.abs(live - baked) > 0.01)
          out.push('p' + w.dataset.pageNum + ' charBoxes 比例 ' + baked.toFixed(3) + ' ≠ 实时 ' + live.toFixed(3) + '(交互时 _syncCharBoxScale 会自愈)');
      }
    }
    (out.length ? out : ['✓ 全部叠层尺寸/比例一致']).forEach(x => (window.dlog || console.log)(x));
    return out;
  } catch (e) { console.warn('auditLayers', e); return []; }
};
// ─── per-PDF 位置记忆（localStorage pdf-last-positions） ───
const LAST_POS_KEY = 'pdf-last-positions';
function _loadLastPositions() {
  try { return JSON.parse(localStorage.getItem(LAST_POS_KEY) || '{}'); }
  catch { return {}; }
}
let _ogLastPage = null;
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
  // 双向上下文同步:借用这个已有的翻页漏斗上报「当前活动文档」,不新增任何监听器。
  // 开关关着时 RC.ctxSync.report 立即返回 false(零网络);开着时由共享层合并 + 1s trailing。
  // 切页 → 丢弃绘图焦点(上一页的绘图区不再是"当前")。只在当前焦点确实是绘图时才发。
  try { if ((patch && patch.page) && patch.page !== _ogLastPage) { _ogLastPage = patch.page;
        window.RC?.outgoing?.dropDrawingFocus(); } } catch (_) {}
  try {
    window.RC?.ctxSync?.report({
      kind: 'pdf', file: FILE_REL,
      pos: (patch && patch.page) || currentPage,
      total: (window.__GRP ? window.__GRP.total : (window.pdfDoc && pdfDoc.numPages)) || undefined,
      title: document.title.replace(/ ·.*$/, '')
    });
  } catch (_) {}
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
  // 排版模式是设备偏好:无论哪边胜都沿用本机上次的 mode(不影响页码仲裁)
  if (pos && pos.mode && pos.mode !== readMode) {
    localStorage.setItem('pdf-read-mode', pos.mode);
    readMode = pos.mode;
  }
  // ── 单真相源仲裁(2026-07-06,大厂 Kindle/Books 模型;修「每次开在固定位置」):服务端续读(CFG.page/page_ts)
  //   与本地 localStorage 记录**按时间戳取新者**。此前两套系统无仲裁、LS 总是无条件覆盖 → 冻结的旧 LS 每次都赢。
  const srvTs = ((window.__PDF_CFG && +__PDF_CFG.page_ts) || 0) * 1000;   // 服务端 epoch秒 → ms
  if (!pos || !pos.page || (srvTs && srvTs >= (pos.ts || 0))) {
    // 服务端更新鲜(或无本地记录)→ 页码信服务端(currentPage 已=CFG.page)。
    // 但**同页时仍用本地的页内偏移**(审计 BUG#4:服务端只存页码不存 frac;5s 上报常晚于 600ms LS 存,
    // server 胜是常态——若因此把 frac 丢掉,每次都开在页顶,反而比仲裁前更差):页码用仲裁、页内偏移用本地。
    if (pos && pos.page === currentPage) {
      _pendingPage = currentPage;
      _pendingFrac = (pos.frac > 0 && pos.frac <= 1) ? pos.frac : 0;
      _pendingScrollY = pos.scrollY || 0;
    } else if (srvTs) {
      try { _saveLastPosition({ page: currentPage, scrollY: 0, frac: 0, mode: readMode }); } catch (_) {}   // 换页了才对齐本地(防旧 frac 錯页套用)
    }
    window.dlog?.('续读仲裁:server 胜 p.' + currentPage + (_pendingFrac ? ' (+本地frac)' : ''));
    return;
  }
  // 本地更新鲜(离线读过/上报失败过)→ 用本地,并把服务端记录「治愈」到本地页(别的设备才拿得到新进度)
  currentPage = pos.page;
  _pendingPage = pos.page;   // 闭包捕获目标页:三连 apply 不读活的 currentPage(会被滚动处理器反馈式改掉 → 恢复位置逐次后爬,审计 BUG#3)
  _pendingFrac = (pos.frac > 0 && pos.frac <= 1) ? pos.frac : 0;
  _pendingScrollY = pos.scrollY || 0;
  try {
    fetch('/pdf/api/reading-pos', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file: FILE_REL, kind: 'pdf', pos: pos.page }), keepalive: true }).catch(() => {});
  } catch (_) {}
  window.dlog?.('续读仲裁:local 胜 p.' + currentPage + (pos.frac ? ' frac=' + pos.frac.toFixed(3) : (pos.scrollY ? ' scrollY=' + pos.scrollY : '')));
  _toast?.('⏯ 已恢复到上次位置 p.' + currentPage);
}
function _restoreScrollAfterRender() {
  if (!_pendingScrollY && !_pendingFrac) return;
  const main = document.getElementById('main');
  if (!main) return;
  // 页内比例优先(布局无关:scale/旋转/占位高度变化都不跑偏);老记录无 frac 才退回绝对 scrollY
  const tgtPage = _pendingPage || currentPage;   // 捕获目标页(BUG#3:活的 currentPage 会被滚动处理器按视口中线改掉,三连 apply 逐次后爬)
  const apply = () => {
    if (_pendingFrac > 0) {
      const pw = document.querySelector(`.page-wrap[data-page-num="${tgtPage}"]`);
      if (pw && pw.offsetHeight) { main.scrollTop = pw.offsetTop + _pendingFrac * pw.offsetHeight; return; }
    }
    if (_pendingScrollY) main.scrollTop = _pendingScrollY;
  };
  // 多次 apply 直到稳定（连续模式占位高度逐步算准 / 真渲染替换占位时高度可能变）
  setTimeout(apply, 100);
  setTimeout(apply, 400);
  setTimeout(apply, 1000);
  setTimeout(() => { _pendingScrollY = 0; _pendingFrac = 0; _pendingPage = 0; }, 1100);   // 最后一次后清空（避免重复 apply 干扰用户主动滚动）
}
function _attachScrollSaver() {
  const main = document.getElementById('main');
  if (!main || main.__savedAttached) return;
  main.__savedAttached = true;
  main.addEventListener('scroll', () => {
    if (_scrollSaveTimer) clearTimeout(_scrollSaveTimer);
    _scrollSaveTimer = setTimeout(() => {
      let frac = 0;   // 页内比例(布局无关);算不出(页未渲)存 0,恢复时退回 scrollY
      try {
        const pw = document.querySelector(`.page-wrap[data-page-num="${currentPage}"]`);
        if (pw && pw.offsetHeight) frac = Math.max(0, Math.min(1, (main.scrollTop - pw.offsetTop) / pw.offsetHeight));
      } catch (_) {}
      _saveLastPosition({page: currentPage, scrollY: main.scrollTop, frac, mode: readMode, scale});
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
// 返回书架:直接进**完整书架页 /pdf/**(用户要的正经书库:压缩/预处理/预热/公式识别都在那)。
// (原来弹的"浮层临时书单"已弃用——用户嫌临时;换书统一回完整书架。)module 作用域,挂 window 供内联 onclick 调用。
window.goPdfList = function () { location.href = '/pdf/'; };

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
  // ★实况网页模式(用户拍板 2026-07-19:"就只是把书页的展示窗口换成网页"):
  //   同一张 PDF 阅读器页面,#page-container 换成同源代理 iframe(web-adapter.js 接管),
  //   这里直接返回——不下载 PDF、不建 pdfDoc,顶栏/侧栏/rc-* 全部照常初始化。
  if (window.__PDF_CFG && __PDF_CFG.web_url) { _pdfInitDone = true; return; }
  pdfLoadShow('📄 打开 PDF…', '大文件首次加载需几秒,正在流式下载结构');
  pdfLoadBar(null);
  // 加载 13s 还没出首页 + 有压缩版 + 当前没在用压缩版 → 在加载层显示「切换压缩版」按钮(慢网救急)
  setTimeout(() => {
    if (!_pdfInitDone && PDF_COMP_AVAIL && !PDF_COMPRESSED) {
      const b = document.getElementById('pdf-loading-switch'); if (b) b.style.display = '';
    }
  }, 13000);
  try {
    if (_imgMode || (window.__PDF_CFG && String(__PDF_CFG.file_rel||'').indexOf('vbook:')===0)) {   // 合并书强制图片模式(v2规格:classic 多文档门面后置)
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
    document.getElementById('page-total').textContent = '/ ' + (window.__GRP ? window.__GRP.total : pdfDoc.numPages);
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

// 单页统一结构(PDF.js 思路):单页也渲进**唯一一个 .page-wrap**(page-container 的子元素),不再直接
// 渲进 page-container。→ 缩放/适应/字符层/选中全走跟连续一模一样的 wrap 路径,根除「两套 DOM 结构来回
// 拆建」整类 bug(单页缩放变多页 / 适应回弹)。返回那个唯一 wrap;若残留多页/双页结构则清空重建。
function _singleWrap() {
  const pc = document.getElementById('page-container');
  let w = pc.querySelector('.page-wrap');
  if (!w || pc.querySelectorAll('.page-wrap').length > 1 || pc.querySelector('.spread-row')) {
    if (_contIO) { _contIO.disconnect(); _contIO = null; }
    pc.innerHTML = '';
    pc.removeAttribute('data-loaded');
    pc.style.zoom = ''; pc.style.transform = ''; pc.style.transformOrigin = '';   // 清掉直渲时代可能残留的缩放
    w = document.createElement('div');
    w.className = 'page-wrap';
    w.dataset.loaded = '0';
    w.style.margin = '0 auto';
    pc.appendChild(w);
  }
  return w;
}
async function renderPage(num) {
  if (!pdfDoc) return;
  num = Math.max(1, Math.min(pdfDoc.numPages, parseInt(num) || 1));
  currentPage = num;
  { const _pc = document.getElementById('page-cur'); if (_pc) _pc.textContent = (window._dispPage ? window._dispPage(num) : num); }
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
  // 单页:渲进唯一 wrap(跟连续同一套结构),翻页只是换 wrap 的 data-page-num 重渲
  const wrap = _singleWrap();
  wrap.dataset.pageNum = num;
  wrap.dataset.loaded = '0';
  await _renderPageInto(num, wrap);
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
// 宽度两档制(2026-07-06,参照 CDN/Next.js Image 的 srcset 桶化):需求 ≤原生宽 → natW 档;
// 超过(高 DPR 屏/手动放大)→ 2400 一步到顶档。此前放大分支直接用任意 cw×dpr
// (iPad dpr2 竖屏 2048/横屏 2400/捏合缩放每级一个新值)→ 单书实测 87 个宽度档,
// natW 预热/Service Worker/HTTP 缓存全 miss、Pi 对同一页反复冷渲染。两档后 URL 稳定、
// 缓存必中,每页每档最多冷渲染一次;显示尺寸仍由 CSS 缩放决定(浏览器降采样,清晰不糊)。
function _bucketReqW(cw) {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const natW = Math.max(400, Math.min(2400, Math.round((window.__imgMeta && window.__imgMeta.page_w) || cw)));
  return (Math.round(cw * dpr) <= natW) ? natW : 2400;
}
const _prefetched = new Set();
function _prefetchAround(num, radius) {
  if (!_imgMode) return;
  const meta = window.__imgMeta; if (!meta) return;
  const cw = Math.floor(meta.page_w * scale);
  const baseW = _bucketReqW(cw);   // 跟 _renderPageImg 同一公式(此前预取用 cw×dpr、渲染用 max(natW,…),首次预取会取错档)
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
  //   超过原生(高 DPR/手动放大)→ 2400 一步到顶(两档制,见 _bucketReqW)。reqW 只增不减(缩小复用大图→不闪)。
  const reqW = _ratchetReqW(num, _bucketReqW(cw));
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
  // 绘图区焦点(A5):手指长按 = 设为当前焦点(再长按取消)。
  // 只监听非笔指针且全程 passive → 画笔/擦除/滚动手势零影响。
  try {
    window.RC?.outgoing?.bindDrawingFocus(inkCanvas, () => ({
      file: FILE_REL, page: num,
      hasInk: !!(wrap.__inkStrokes && wrap.__inkStrokes.length)
    }));
  } catch (_) {}

  if (!wrap.__inkBound) {
    wrap.addEventListener('pointerdown', (e) => { if (window._inkPointerDown) window._inkPointerDown(e); }, true);
    const _blk = (e) => { for (const t of e.touches) { if (t.touchType === 'stylus') { e.preventDefault(); break; } } };
    wrap.addEventListener('touchstart', _blk, { passive: false });
    wrap.addEventListener('touchmove', _blk, { passive: false });
    wrap.__inkBound = true;
  }
  // char 层(PyMuPDF chars:选词/高亮/振假名/搜索)。它只用 viewport.scale + 自己做坐标转换 → shim viewport 够。
  loadCharsAndBindLayer(num, wrap, viewport).catch(e => window.dlog?.('chars load fail: ' + (e && e.message)));
  wrap.__inkStrokes = (window._ink && window._ink.byPage[num] && !(window._upClaimed && window._upClaimed[num])) ? JSON.parse(JSON.stringify(window._ink.byPage[num])) : [];   // #4:插入页占用的页号,陈旧真页不贴其墨迹
  if (window._inkRedraw) window._inkRedraw(wrap);
  _applyCropToWrap(wrap, cw, ch);
  wrap.__renderScale = scale;   // 记录渲染时的 scale → 缩放重排时按比例 zoom 现有位图(过渡期补偿)
  wrap.style.zoom = '';   // 本页位图已是当前 scale 原生像素 → 撤掉缩放过渡期的补偿 zoom(回到 1)
  wrap.dataset.loaded = '1';
  try { if (window.__uiShared && window.RC && RC.stickynote) RC.stickynote.mountPending(); } catch (_) {}   // 本页便签补挂(幂等;重渲 innerHTML='' 会清掉便签,这里自愈)
  try { if (window.__uiShared && window._userpagesMount) window._userpagesMount(); } catch (_) {}   // 用户页(插入页)补挂(幂等;hook 实现在模板 ui_shared 块,legacy 无感)
  // 服务端回的图比请求宽小 = 宽度容差「放大近似图」(模糊),它已在后台补渲精确宽 → 稍后换成清晰图
  if (img.naturalWidth < reqW - 2) _scheduleSharpen(num, wrap, reqW, mt, _gen, 0);
  if (window._updateMainOverflowX) requestAnimationFrame(window._updateMainOverflowX);
  setTimeout(() => _prefetchAround(num), 400);   // 渲染完当前页 → 后台预取前后页(延后,先让当前页图到位)
  if (window._maybeAutoPrewarm) window._maybeAutoPrewarm();   // 首页渲完(scale/宽度已定)→ 自动后台预热整本(只触发一次)
  if (readMode === 'single') {
    const u = new URL(location.href); u.searchParams.set('page', num); history.replaceState(null, '', u);
    loadPageNodes(num);
  }
}
// 拿到「模糊近似图」后,等服务端后台补渲精确宽完成,再把这一页的 <img> 原地换成清晰图(只换图、不重渲整页)。
// cache-bust 绕开浏览器缓存(近似图返回 no-store,本就不缓存;busted url 取磁盘上已渲好的精确图)。最多重试 3 次。
async function _scheduleSharpen(num, wrap, reqW, mt, gen, tries) {
  tries = tries || 0;
  if (tries > 3) return;
  setTimeout(async () => {
    if (!wrap.isConnected || wrap.__imgGen !== gen) return;   // 页已释放 / 已有更新渲染 → 放弃
    const im = document.createElement('img'); im.decoding = 'async';
    im.src = '/pdf/api/page-image?file=' + encodeURIComponent(FILE_REL) + '&page=' + num + '&w=' + reqW + '&v=' + mt + '&sharp=' + Date.now();
    try { await im.decode(); } catch (_) { return _scheduleSharpen(num, wrap, reqW, mt, gen, tries + 1); }
    if (!wrap.isConnected || wrap.__imgGen !== gen) return;
    if (im.naturalWidth < reqW - 2) return _scheduleSharpen(num, wrap, reqW, mt, gen, tries + 1);   // 后台还没渲好 → 再等
    const cur = wrap.querySelector('img.page-img');
    if (cur) { im.className = 'page-img'; im.style.width = cur.style.width; im.style.height = cur.style.height; im.style.display = 'block'; cur.replaceWith(im); }
  }, 1600 + tries * 1600);
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
  // 绘图区焦点(A5):手指长按 = 设为当前焦点(再长按取消)。
  // 只监听非笔指针且全程 passive → 画笔/擦除/滚动手势零影响。
  try {
    window.RC?.outgoing?.bindDrawingFocus(inkCanvas, () => ({
      file: FILE_REL, page: num,
      hasInk: !!(wrap.__inkStrokes && wrap.__inkStrokes.length)
    }));
  } catch (_) {}

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
  wrap.__inkStrokes = (window._ink && window._ink.byPage[num] && !(window._upClaimed && window._upClaimed[num])) ? JSON.parse(JSON.stringify(window._ink.byPage[num])) : [];   // #4:插入页占用的页号,陈旧真页不贴其墨迹
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
  try { if (window.__uiShared && window.RC && RC.stickynote) RC.stickynote.mountPending(); } catch (_) {}   // 本页便签补挂(幂等;重渲 innerHTML='' 会清掉便签,这里自愈)
  try { if (window.__uiShared && window._userpagesMount) window._userpagesMount(); } catch (_) {}   // 用户页(插入页)补挂(幂等;hook 实现在模板 ui_shared 块,legacy 无感)
  if (window._updateMainOverflowX) requestAnimationFrame(window._updateMainOverflowX);   // 渲染后据实测内容宽锁/放横向滚动

  // 同步 URL + 拉 KG 节点：只在单页模式做
  if (readMode === 'single') {
    const u = new URL(location.href);
    u.searchParams.set('page', num);
    history.replaceState(null, '', u);
    loadPageNodes(num);
  }
}

function loadPageNodes(num) {
  // 共享模式(__uiShared)→ PdfAdapter.renderPageNodes → rc-knowledge.renderInto(知识点卡统一)。
  //   取数(page-nodes,页作用域)+ __lastPageNodes(语音上下文)+ 容器 #kg-nodes 都由 adapter 处理;
  //   点开 skilltree / ☆跟踪 用 rc-knowledge 默认行为(与 PDF toggleNodeTrack 逐字一致);抽屉本体本阶段不迁,留 PDF 原版。
  //   RC 不可用 / 容器缺 → fallback 回 _loadPageNodesNative(原逻辑逐字不变)。
  if (window.__uiShared && window.PdfAdapter && PdfAdapter.renderPageNodes) {
    return PdfAdapter.renderPageNodes({
      file: FILE_REL, page: num, container: 'kg-nodes',
      onAfter: () => { try { window._refreshVocabIfPage?.(); } catch (_) {} },   // 等价原尾部
      fallback: () => _loadPageNodesNative(num),
    });
  }
  return _loadPageNodesNative(num);
}
async function _loadPageNodesNative(num) {
  try {
    const r = await fetch(`/pdf/api/page-nodes?file=${encodeURIComponent(FILE_REL)}&page=${num}`);
    const d = await r.json();
    const list = d.nodes || [];
    window.__lastPageNodes = list;   // 给语音助手 __voiceContext 做谐音纠错/上下文
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
  if (window.__GRP) {   // 分卷:越界自动加载相邻卷(单页/横滑模式)
    if (delta > 0 && currentPage >= __GRP.self.pages && __GRP.next) return window._grpNavNext();
    if (delta < 0 && currentPage <= 1 && __GRP.prev) return window._grpNavPrev();
  }
  await renderPage(currentPage + delta);
  _saveLastPosition({page: currentPage, mode: readMode, scale});
};
window.goToPage = async (n) => {
  await renderPage(n);
  _saveLastPosition({page: currentPage, mode: readMode, scale});
};

// 页码对齐:每本书一个偏移(PDF 页 - 书上印的页),存 localStorage(pdf-* 前缀 → 自动跨设备同步)。
// 显示处一律 _dispPage(pdf)=书上页码;跳页输入按书上页码 → _pdfFromDisp 转回 PDF 页。
window._pageOffset = function () {
  try { return parseInt(localStorage.getItem('pdf-page-offset:' + (typeof FILE_REL !== 'undefined' ? FILE_REL : '')) || '0', 10) || 0; } catch (_) { return 0; }
};
// 虚拟合并书:同名 partN 分卷 → 连续页码(组偏移正交叠加在印刷页对齐之上)+ 跨卷跳页。
// ⏸ 止血闸(2026-07-19):bolt-on 的组行为在单页/双页等模式暴露大量问题——正确做法是**转换层**
// (在唯一咽喉把多卷翻译成一本书,其余代码无感知),设计落地前默认关;&vmerge=1 保留实验入口。
window.__GRP = ((window.__PDF_CFG && __PDF_CFG.group) && /[?&]vmerge=1/.test(location.search)) ? __PDF_CFG.group : null;
window._grpOff = function () { return window.__GRP ? __GRP.self.offset : 0; };
window._dispPage = function (pdf) {
  pdf = parseInt(pdf, 10) || 0;
  var off = window._pageOffset(), pr = pdf - off;
  return ((off && pr >= 1) ? pr : pdf) + window._grpOff();   // 前言区(印页<1)回退显示 PDF 页;分卷再加组偏移
};
window._pdfFromDisp = function (disp) { return (parseInt(disp, 10) || 0) - window._grpOff() + window._pageOffset(); };
// 全局显示页 g 落在别的卷 → 跳转那一卷(true=已导航);本卷/非分卷 → false
window._grpNavToGlobal = function (g) {
  if (!window.__GRP) return false;
  g = parseInt(g, 10) || 0;
  var me = (window.__PDF_CFG && __PDF_CFG.file_rel) || '';
  var m = __GRP.members.find(function (x) { return g > x.offset && g <= x.offset + x.pages; });
  if (!m || m.rel === me) return false;
  location.href = '/pdf/view?file=' + encodeURIComponent(m.rel) + '&page=' + (g - m.offset);
  return true;
};
// 边界自动翻卷(用户定:不要按钮——滚动越界直接加载相邻卷)。
// 连续模式:到底后继续滚 / 到顶后继续拉,累积超阈值 → 自动跳相邻卷;单页模式在 changePage 越界时同样自动。
window._grpNavNext = function () {
  if (!(window.__GRP && __GRP.next) || window.__grpNaving) return;
  window.__grpNaving = 1; _grpToast('下一卷 · 第 ' + (__GRP.self.offset + __GRP.self.pages + 1) + ' 页 …');
  location.href = '/pdf/view?file=' + encodeURIComponent(__GRP.next) + '&page=1';
};
window._grpNavPrev = function () {
  if (!(window.__GRP && __GRP.prev) || window.__grpNaving) return;
  window.__grpNaving = 1; var pm = __GRP.members[__GRP.self.index - 1];
  _grpToast('上一卷 · 第 ' + __GRP.self.offset + ' 页 …');
  location.href = '/pdf/view?file=' + encodeURIComponent(__GRP.prev) + '&page=' + pm.pages;
};
function _grpToast(txt) {
  try {
    var t = document.createElement('div'); t.textContent = txt;
    t.style.cssText = 'position:fixed;left:50%;bottom:34px;transform:translateX(-50%);z-index:99;background:#1a2540ee;color:#cfe6ff;border:1px solid #3b6db5;padding:6px 16px;border-radius:16px;font-size:13px;box-shadow:0 4px 14px #0007';
    document.body.appendChild(t);
  } catch (_) {}
}
function _grpBoundarySetup() {
  if (!window.__GRP) return;
  var el = document.getElementById('main'); if (!el) return;
  // ⚡性能:绝不在 touchmove/wheel 里读 scrollHeight(连续模式频繁增删节点,每读一次强制重排=卡)。
  // 边缘状态由 scroll 事件 rAF 节流计算一次/帧;手势处理器只读标志、零布局访问。
  var edge = 0;   // 0=中间 1=顶 2=底
  var edgeSince = 0, nextArmed = false, prevArmed = false;   // 武装:落地即在边缘时不触发,须先离开过边缘(防跳卷后被回弹立刻弹回=乒乓)
  var rafPending = false;
  function _calcEdge() {
    rafPending = false;
    var stp = el.scrollTop, ch = el.clientHeight, sh = el.scrollHeight;
    var ne = (stp <= 2) ? 1 : (stp + ch >= sh - 4) ? 2 : 0;
    if (ne !== edge) {
      edge = ne; edgeSince = performance.now();
      if (edge !== 2) nextArmed = true;   // 离开过底部 → 允许下次到底触发
      if (edge !== 1) prevArmed = true;
    }
  }
  function _queueEdge() { if (!rafPending) { rafPending = true; requestAnimationFrame(_calcEdge); } }
  el.addEventListener('scroll', _queueEdge, { passive: true });
  _queueEdge();
  var accDown = 0, accUp = 0, touchY = null;
  var _dwell = function () { return performance.now() - edgeSince > 250; };   // 到边后驻留 250ms 才开始算(滤回弹)
  el.addEventListener('wheel', function (e) {
    if (!edge) { accDown = 0; accUp = 0; return; }
    if (e.deltaY > 0 && edge === 2 && nextArmed && _dwell() && !window.__grpCorridor) { accUp = 0; accDown += e.deltaY; if (accDown > 320) window._grpNavNext(); }
    else if (e.deltaY < 0 && edge === 1 && prevArmed && _dwell()) { accDown = 0; accUp += -e.deltaY; if (accUp > 320) window._grpNavPrev(); }
    else { accDown = 0; accUp = 0; }
  }, { passive: true });
  el.addEventListener('touchstart', function (e) { touchY = e.touches[0].clientY; accDown = 0; accUp = 0; }, { passive: true });
  el.addEventListener('touchmove', function (e) {
    if (touchY == null || !edge) { if (!edge) { accDown = 0; accUp = 0; } if (e.touches.length) touchY = e.touches[0].clientY; return; }
    var dy = touchY - e.touches[0].clientY;   // >0 = 手指上滑(向下滚)
    touchY = e.touches[0].clientY;
    if (dy > 0 && edge === 2 && nextArmed && _dwell() && !window.__grpCorridor) { accDown += dy; if (accDown > 150) window._grpNavNext(); }
    else if (dy < 0 && edge === 1 && prevArmed && _dwell()) { accUp += -dy; if (accUp > 150) window._grpNavPrev(); }
    else { accDown = 0; accUp = 0; }
  }, { passive: true });
  el.addEventListener('touchend', function () { touchY = null; accDown = 0; accUp = 0; }, { passive: true });
}
if (document.readyState !== 'loading') _grpBoundarySetup();
else window.addEventListener('DOMContentLoaded', _grpBoundarySetup);
window._refreshPageCur = function () { var pc = document.getElementById('page-cur'); if (pc && typeof currentPage !== 'undefined') pc.textContent = window._dispPage(currentPage); };
// 设置面板「页码对齐」:把当前页设为书上第 N 页 → 偏移=当前PDF页-N;applyPageOffset(0)=重置
window.applyPageOffset = function (forceZero) {
  var off;
  if (forceZero === 0) off = 0;
  else {
    var pr = parseInt((document.getElementById('set-pg-printed') || {}).value, 10);
    if (!pr) { if (typeof _toast === 'function') _toast('请先填书上的页码'); return; }
    off = (currentPage || 1) - pr;
  }
  try { localStorage.setItem('pdf-page-offset:' + FILE_REL, String(off)); } catch (_) {}
  // 镜像到服务端:后台 describe/provenance 要靠它把目录的印刷页对齐(前端 localStorage 它读不到)
  try { fetch('/pdf/api/page-offset', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({file: FILE_REL, offset: off})}).catch(function(){}); } catch (_) {}
  window._refreshPageCur();
  window._populatePageOffsetUI();
  if (typeof _toast === 'function') _toast(off ? ('已对齐(PDF 比书页多 ' + off + ' 页)') : '已重置页码对齐');
};
window._populatePageOffsetUI = function () {
  var cp = (typeof currentPage !== 'undefined' && currentPage) ? currentPage : 1;
  var a = document.getElementById('set-pg-pdf'); if (a) a.textContent = cp;
  var b = document.getElementById('set-pg-printed'); if (b) b.value = window._dispPage(cp) - window._grpOff();   // 印刷页对齐是卷内概念,预填剔除组偏移
  var c = document.getElementById('set-pg-cur-off'); if (c) c.textContent = '当前偏移：' + window._pageOffset();
};

// ── 助手聊天里点页码 → 跳页 + PDF 底部「↩ 回到第 X 页」(多次跳转仍回最早那页)──
window.__pageBackAnchor = null;
window.jumpWithBack = function (target) {
  target = parseInt(target, 10);
  if (!target || target < 1) return;
  const cur = (typeof currentPage !== 'undefined') ? currentPage : 1;
  if (window.__pageBackAnchor == null && target !== cur) window.__pageBackAnchor = cur;  // 第一次跳:记最早的来处
  goToPage(target);
  if (window.__pageBackAnchor != null && window.__pageBackAnchor !== target) _showPageBackBar(window.__pageBackAnchor);
  else _hidePageBackBar();
};
window.pageGoBack = function () {
  const b = window.__pageBackAnchor;
  window.__pageBackAnchor = null;
  _hidePageBackBar();
  if (b != null) goToPage(b);
};
function _showPageBackBar(p) {
  let bar = document.getElementById('page-back-bar');
  if (!bar) {
    bar = document.createElement('div'); bar.id = 'page-back-bar';
    bar.addEventListener('click', function () { window.pageGoBack(); });
    document.body.appendChild(bar);
    if (!document.getElementById('page-back-bar-css')) {
      const st = document.createElement('style'); st.id = 'page-back-bar-css';
      st.textContent =
        '#page-back-bar{position:fixed;left:50%;bottom:20px;transform:translateX(-50%);z-index:140;display:none;' +
        'background:#1a2540;border:1px solid #3b6db5;color:#cfe6ff;padding:9px 18px;border-radius:20px;font-size:14px;' +
        'cursor:pointer;box-shadow:0 6px 18px rgba(0,0,0,.5);-webkit-tap-highlight-color:transparent;white-space:nowrap}' +
        '#page-back-bar:active{transform:translateX(-50%) scale(.95)}' +
        '.asst-pagelink{color:#7dd3fc;cursor:pointer;border-bottom:1px dashed rgba(125,211,252,.6);padding:0 1px}' +
        '.asst-pagelink:active{color:#bae6fd}';
      document.head.appendChild(st);
    }
  }
  bar.textContent = '↩ 回到第 ' + window._dispPage(p) + ' 页';
  bar.style.display = 'block';
}
function _hidePageBackBar() { const bar = document.getElementById('page-back-bar'); if (bar) bar.style.display = 'none'; }
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
    numEl().textContent = window._dispPage(tgt) + ' / ' + window._dispPage(st.total);
    fillEl().style.width = (tgt / st.total * 100) + '%';
    const pc = document.getElementById('page-cur'); if (pc) pc.textContent = window._dispPage(tgt);
    e.preventDefault();
  });
  const end = () => {
    if (!st) return;
    const s = st; st = null;
    pop.style.display = 'none';
    if (s.moved) { goToPage(s.tgt); }
    else {
      const grandTotal = window.__GRP ? window.__GRP.total : window._dispPage(s.total);
      const n = prompt('跳到第几页(按书上印的页码,共 ' + grandTotal + ')', window._dispPage(currentPage));
      const v = parseInt(n);
      if (v && window._grpNavToGlobal && window._grpNavToGlobal(v)) return;   // 分卷:目标页在别的卷 → 跳那卷
      if (v) goToPage(Math.max(1, Math.min(s.total, window._pdfFromDisp(v))));   // 输入是书上页码 → 转回 PDF 页
      else window._refreshPageCur();
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
  scale = Math.max(_ZOOM_MIN, Math.min(_scaleMax, scale + delta));
  // +/- 缩放跟双指缩放(_applyZoom)一致:三模式都原地重排(单页=唯一 wrap),原地失败才按模式重建
  if (!(await _rescaleContinuousInPlace())) { if (readMode === 'single') await renderPage(currentPage); else await setupContinuousMode(); }
};
// 宽适应：按 #main 可用宽度重算 scale（取消 ＋/－ 或双指缩放，回到一页刚好铺满宽度）
window.fitWidth = async () => { window._atFitWidth = true; await _refitToWidth(true); window._rememberOrientLayout?.(); };   // 点「适应」= 回到宽度适应态(旋转保持适应);也记进当前方向
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
// ── 阶段6 门控:ui=shared → RC.grammar.loadHistory 渲进同一个 #grammar-panel-body(跟 EPUB 共用);
//   else 走原生逐字(_addHistoryBlock)。──
window.loadGrammarHistory = async () => {
  _grammarHistLoaded = true;
  if (window.__uiShared && window.RC && RC.grammar) {
    try {
      await RC.grammar.loadHistory('grammar-panel-body', FILE_REL, {
        aiParams: (typeof _getAiOverrides === 'function') ? _getAiOverrides : undefined,
        sourceUrl: () => FILE_REL ? (location.origin + '/pdf/view?file=' + encodeURIComponent(FILE_REL) + '&page=' + (currentPage || 1)) : '',
        viewModeKey: 'pdf-grammar-view',
      });
    } catch (e) { window.dlog?.('grammar history load fail: ' + (e && e.message)); }
    return;
  }
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
    window.__lastVocab = items;   // 给语音助手 __voiceContext 做谐音纠错/上下文
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

// 语音助手上下文:把「当前页能看到的实体」(书目/知识点/生词 + 当前页/书)报给 voice.js,
// 供后端做 speechContexts 发音偏置 + 大脑谐音纠错(按读音映射到真实项)。模块作用域可见 FILE_REL/currentPage/pdfDoc/readMode/BOOK_LANGS。
window.__voiceContext = function () {
  try {
    let sel = '', selSentence = '';
    // 优先 char-layer 选中(lastSelText:阅读器自绘选中,如漫画/PDF 的 OCR 文字层,原生 getSelection 常为空)
    // → 助手才拿得到"用户选中的内容"(修:开助手/点输入框后原生选区被清,但 lastSelText 仍在)。
    // 失效校验:char-layer 选中只认「当前页 + 10 分钟内」的,否则翻到别页后旧选中会被误当成现在在问的内容。
    // 原生选区(getSelection)是实时的,无陈旧问题,作回退。
    try {
      const ls = (typeof lastSelText === 'string' ? lastSelText : '').trim();
      const meta = window.__lastSelMeta;
      const curP = (typeof currentPage !== 'undefined' ? currentPage : -1);
      const fresh = ls && meta && meta.page === curP && (Date.now() - (meta.t || 0) < 600000);
      if (fresh) {
        sel = ls.slice(0, 400);
        selSentence = (typeof window.__lastSelSentence === 'string' ? window.__lastSelSentence : '').trim().slice(0, 600);
      } else {
        const nat = (window.getSelection ? getSelection().toString() : '').trim();
        if (nat) sel = nat.slice(0, 400);
      }
      // ★钉住的焦点(顶部 ¶ chip)**最优先**——用户实锤 2026-07-19:钉了一段问「把这里做成
      //   Anki 卡」,AI 却答「先把这一页抓取一下」→ 读整页、又慢又不是他要的。根因就是这里
      //   只看 char-layer/原生选区,而钉住时两者常已被清空(开助手/点输入框就清)。
      //   EPUB 侧 getContext 早有此分支(epub-html.js:4043),PDF 侧一直漏。
      try {
        const fs = window.__focusSel;
        if (fs && (fs.text || '').trim()) {
          sel = String(fs.text).trim().slice(0, 400);
          if (!selSentence && fs.sent) selSentence = String(fs.sent).trim().slice(0, 600);
        }
      } catch (_) {}
    } catch (_) {}
    let books = [];
    try {
      const c = JSON.parse(localStorage.getItem('pdf-bookshelf-cache') || '[]');
      if (Array.isArray(c)) books = c.map(p => ({ name: p.name, rel: p.rel })).slice(0, 80);
    } catch (_) {}
    const nodes = (window.__lastPageNodes || []).map(n => ({ id: n.id, name: n.name })).slice(0, 40);
    const vocab = (window.__lastVocab || []).map(v => v.lemma).filter(Boolean).slice(0, 50);
    // 带入的图(点/拖进来的 YOLO 图,可多张)。显式带入 → 保留到点 ✕,不做跨页过期。
    // 笔迹**发消息时实时收集**(画在 attach 之后也算),随图带给助手做合成,不依赖服务端墨迹保存时机
    let figures = [];
    try {
      figures = (window.__figAttached || []).filter(a => a && a.box).slice(0, 6).map(a => {
        const ink = (typeof window.__figInk === 'function') ? window.__figInk(a.page, a.box) : [];
        return {
          page: a.page, box: a.box, caption: (a.caption || '').slice(0, 80),
          desc: (a.desc || '').slice(0, 500), group: !!a.group,
          file_rel: a.file_rel || (typeof FILE_REL !== 'undefined' ? FILE_REL : ''),
          has_ink: ink.length > 0, ink: ink
        };
      });
    } catch (_) {}
    return {
      page_type: 'pdf',
      file_rel: (typeof FILE_REL !== 'undefined' ? FILE_REL : ''),
      book_name: (typeof FILE_REL !== 'undefined' && FILE_REL) ? FILE_REL.split('/').pop() : '',
      page: (typeof currentPage !== 'undefined' ? currentPage : 0),
      pages: (function () {   // 双页模式报当前可见的两页(offset0:1|2,3|4…; offset1:1,2|3,4|5…),否则单页
        try {
          var cp = currentPage;
          if (typeof readMode === 'undefined' || readMode !== 'spread') return [cp];
          var off = (typeof _spreadOffset !== 'undefined') ? _spreadOffset : 0, a;
          if (off === 0) a = (cp % 2) ? cp : cp - 1;
          else a = (cp <= 1) ? 1 : ((cp % 2) ? cp - 1 : cp);
          if (off === 1 && a === 1) return [1];
          return [a, a + 1];
        } catch (_) { return [currentPage]; }
      })(),
      total: (typeof pdfDoc !== 'undefined' && pdfDoc) ? pdfDoc.numPages : 0,
      page_offset: (typeof window._pageOffset === 'function' ? window._pageOffset() : 0),   // PDF页-印刷页:助手据此把页码转成书上印刷页(跟用户一致)
      read_mode: (typeof readMode !== 'undefined' ? readMode : ''),
      langs: (typeof BOOK_LANGS !== 'undefined' ? BOOK_LANGS : []),
      selection: sel,
      selection_sentence: selSentence,
      figures: figures,
      // 当前页手写墨迹(内存里实时,不依赖服务端保存时机)→ 服务端据此算"用笔圈/划下的文字"当焦点。
      ink: (function () { try { return (window._ink && window._ink.byPage && window._ink.byPage[currentPage]) ? window._ink.byPage[currentPage].slice(0, 60) : []; } catch (_) { return []; } })(),
      // 用户钉住的焦点(公式/段落 chip):持久(选中过期也保留),助手据此知道"在专门问这个"
      focus_sel: (window.__focusSel && window.__focusSel.text)
        ? { text: String(window.__focusSel.text).slice(0, 400), kind: window.__focusSel.kind || 'text' } : null,
      visible_kg_nodes: nodes,
      visible_vocab: vocab,
      books: books,
    };
  } catch (e) { return { page_type: 'pdf', url: location.pathname }; }
};
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
  window._gpApplyAppearance && _gpApplyAppearance();   // 切排版 → 套用本排版各自记的侧栏外观(悬浮/模糊),在 refit 前置好 grammar-floating
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
// 横向滚动锁:只看**页面本身**(连续/单页=.page-wrap,双页=.spread-row)有没有超过视口宽。
// 不用 main.scrollWidth —— 去边时页图/叠层按整宽(--full-w)渲染再 translate 移位,落在裁掉边距里的
// absolute 子元素(如生词下划线)会逃逸 overflow:hidden、把 scrollWidth 撑大 → 「适应后页面铺满却还能
// 横拖出一片空白」(用户报)。改成量真正的页框宽度:页框 ≤ 视口 → 锁 hidden(无横滑);只有真放大超宽才放 auto。
function _updateMainOverflowX() {
  const main = document.getElementById('main');
  if (!main) return;
  const rowSel = (typeof readMode !== 'undefined' && readMode === 'spread') ? '.spread-row' : '.page-wrap';
  let pageW = 0;
  document.querySelectorAll(rowSel).forEach(w => { const bw = w.getBoundingClientRect().width; if (bw > pageW) pageW = bw; });
  if (pageW > main.clientWidth + 2) { main.style.overflowX = 'auto'; }
  else { main.style.overflowX = 'hidden'; main.scrollLeft = 0; }   // 锁 hidden 时归零横向偏移(防从放大态切回适应残留偏移)
}
window._updateMainOverflowX = _updateMainOverflowX;
function _scheduleRefit(force) {
  if (_refitDebounce) clearTimeout(_refitDebounce);
  _refitDebounce = setTimeout(() => _refitToWidth(force), 180);
}
// (已删除「适应溢出兜底 _runFitOverflowGuard」)它测 main.scrollWidth 来「兜底再缩」,但会把**非页面元素**
// 的假溢出(如错位的 vocab 下划线、知识点抽屉)当真,于是把页面缩到 fit 以下 → 用户报的「适应后瞬间填满
// 又回弹」就是它干的。_computeFitScale 本身精确(去边时也按可见宽算到正好填满),不需要这个投机性兜底。
// 宽度适应态(粘性用户意图):开书默认=适应;只有**用户手动缩放**(_applyZoom:双指/单页缩放)才离开;
// 点「适应」(fitWidth)回到适应。旋转时若处于适应态 → 新方向强制重算适应,不还原该方向旧的手动缩放
// (用户拍板:自适应开着就无论怎么转都自动适应)。自动 refit(ResizeObserver)不改此意图标志。
window._atFitWidth = true;
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
    const keepFit = window._atFitWidth !== false;   // 旋转前正处宽度适应 → 新方向保持适应(用户拍板),忽略该方向旧的手动缩放存档
    if (_applyOrientLayoutVars(lay)) {
      if (keepFit) _orientPendingScale = 0;
      window.dlog && window.dlog('orient: 套用 ' + now + ' = ' + JSON.stringify(lay) + (keepFit ? ' (保持适应)' : ''));
      _updateModeButtons(); _updateCropBtn();
      await _applyModeChange(currentPage);
      await _applyPendingOrientScale();  // 还原该方向上次的缩放(宽度适应/手动放大)
      if (keepFit) { await _refitToWidth(true); window._atFitWidth = true; }   // 强制按新方向宽度重算适应
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
// 焦点锚:焦点屏幕点落在哪一页 + 页内比例(布局无关)。缩放后按此比例点移回原屏幕位置 → 焦点保持、缩放不跳。
// 用 getBoundingClientRect(屏幕坐标)天然绕开 offsetParent 链(单页/连续/双页 spread-row 通用)。
function _focalAnchor(sx, sy) {
  const wraps = document.querySelectorAll('#page-container .page-wrap');
  for (const w of wraps) {
    const r = w.getBoundingClientRect();
    if (r.height && sy >= r.top && sy < r.bottom) {
      return { pn: w.dataset.pageNum, fracY: (sy - r.top) / r.height, fracX: r.width ? (sx - r.left) / r.width : 0 };
    }
  }
  return null;
}
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
    window._atFitWidth = false;            // 手动缩放 → 离开宽度适应态(旋转不再强制适应,还原各方向记忆)
    // 统一:三模式都走 wrap 原地重标尺(单页=唯一 wrap,跟连续同一套 CSS-zoom 瞬时缩放 + 后台重栅格化);失败按模式重建
    if (!(await _rescaleContinuousInPlace())) { if (readMode === 'single') await renderPage(currentPage); else await setupContinuousMode(); }
    requestAnimationFrame(() => {
      if (focal && focal.s0 > 0) {
        const mr = main.getBoundingClientRect();
        let handled = false;
        // 焦点保持(大厂 pinch-zoom 标准):锚定「焦点落在哪一页 + 页内比例」而非像素线性外推。
        // 旧 fx*k 假设整个内容严格等比,但连续模式页间距(margin)不随 CSS-zoom 缩放 → 焦点在文档靠后处时
        // 偏差=Σ页间距×(k-1),放大 2× 可达几百 px(用户报「缩放后要移回阅读位置」的根因)。
        // 页锚用 getBoundingClientRect 差量把锚点移回起始焦点屏幕位置,布局无关、免受页间距/占位高度变化影响。
        if (focal.anchor && focal.anchor.pn != null) {
          const pw = document.querySelector('.page-wrap[data-page-num="' + focal.anchor.pn + '"]');
          if (pw) {
            const r = pw.getBoundingClientRect();
            if (r.height) {
              main.scrollTop  = Math.max(0, main.scrollTop  + (r.top  + focal.anchor.fracY * r.height) - focal.cy);
              main.scrollLeft = Math.max(0, main.scrollLeft + (r.left + focal.anchor.fracX * r.width)  - focal.cx);
              handled = true;
            }
          }
        }
        if (!handled) {   // 兜底:焦点落在页间距/该页已卸载 → 旧线性外推(近似)
          const k = newScale / focal.s0;
          main.scrollLeft = Math.max(0, focal.fx * k - (focal.cx - mr.left));
          main.scrollTop  = Math.max(0, focal.fy * k - (focal.cy - mr.top));
        }
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
        anchor: _focalAnchor(cx, cy),   // 焦点页锚(布局无关);缩放不跳
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
      _applyZoom(p.target, { fx: p.fx, fy: p.fy, cx: p.cx, cy: p.cy, s0: p.s0, anchor: p.anchor });
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
    _applyZoom(target, { fx: main.scrollLeft + (e.clientX - mr.left), fy: main.scrollTop + (e.clientY - mr.top), cx: e.clientX, cy: e.clientY, s0: scale, anchor: _focalAnchor(e.clientX, e.clientY) });
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
    ph.textContent = '… 第 ' + (window._dispPage ? window._dispPage(num) : num) + ' 页';
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
  _setupGrpBoxes(container, mainEl).catch(() => {});   // 虚拟合并书盒子模型:外卷页盒子接在末尾,滚动零等待
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
const READER_BUILD = 'reader-figpop-98';
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
  w.textContent = '… 第 ' + (window._dispPage ? window._dispPage(num) : num) + ' 页';
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
    // 55:插入页死区修——中线落在乐观插入页(.pdf-upage,无 data-page-num)上时 target=null,
    // currentPage 冻在上一页 → AI 按页码永远看不见插入页。后台插页 job 完成后 overlay 已绑真页号
    // (__upRec.page>0,服务端 PDF 已物理含该页)→ 用它更新 currentPage,页码链路即通(read_page/see_ink 都对)。
    if (!target) {
      const ups = document.querySelectorAll('#page-container .pdf-upage');
      for (let i = 0; i < ups.length; i++) {
        const r = ups[i].getBoundingClientRect();
        if (r.top <= center && r.bottom >= center) {
          const rec = ups[i].__upRec;
          if (rec && typeof rec.page === 'number' && rec.page > 0) {
            if (rec.page !== currentPage) {
              currentPage = rec.page;
              if (window._refreshPageCur) window._refreshPageCur(); else { const _pc = document.getElementById('page-cur'); if (_pc) _pc.textContent = rec.page; }   // 统一走一条路(含边界翻卷浮标)
            }
          }
          break;
        }
      }
    }
    if (target) {
      const num = parseInt(target.dataset.pageNum);
      if (num !== currentPage) {
        currentPage = num;
        if (window._refreshPageCur) window._refreshPageCur(); else { const _pc = document.getElementById('page-cur'); if (_pc) _pc.textContent = num; }   // 统一走一条路(含边界翻卷浮标)
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

// ── 虚拟合并书·盒子模型(用户设计:每个盒子=「(书,页)」,A书x页/B书y页都只是往盒子里填内容)──
// 后续卷的**全部页**建成 .grp-fbox 盒子接在本卷占位之后(同款分批让出事件循环);独立 IO 懒填
// 服务端页图(/api/page-image 按 (rel,page) 寻址,SW cache-first);滚动永不等待。
// 交互(选词/高亮)需本书会话 → 在外卷区**停稳 800ms** 后台无感换会话(深链已必胜仲裁,页图已缓存
// → 落地即显)。外卷区页码指示直接显全局页。上一卷方向不建盒子(顶部插入会触发滚动锚定跳动),保留上拉加载。
async function _setupGrpBoxes(container, mainEl) {
  window.__grpCorridor = false;
  const g = window.__GRP;
  if (!g || !g.next) return;
  const members = g.members.slice(g.self.index + 1);
  const ref = container.querySelector('.page-wrap');
  const refW = ref ? ref.style.width : '';
  const refH = (ref && ref.style.height) ? ref.style.height : '900px';
  const fio = new IntersectionObserver((es) => es.forEach((en) => {
    if (!en.isIntersecting) return;
    const ph = en.target;
    if (ph.dataset.fLoaded === '1') return;
    ph.dataset.fLoaded = '1';
    const cw = Math.floor(ph.clientWidth || 800);
    const reqW = (typeof _bucketReqW === 'function') ? _bucketReqW(cw) : Math.min(2400, cw * 2);
    const img = document.createElement('img');
    img.decoding = 'async';
    img.style.cssText = 'width:100%;height:auto;display:block';
    img.src = '/pdf/api/page-image?file=' + encodeURIComponent(ph.dataset.fRel) + '&page=' + ph.dataset.fLp + '&w=' + reqW + '&v=' + (ph.dataset.fMt || 0);
    img.onload = () => { ph.textContent = ''; ph.style.height = 'auto'; ph.appendChild(img); };
    img.onerror = () => { ph.dataset.fLoaded = '0'; ph.textContent = '… 第 ' + ph.dataset.fGlobal + ' 页(滚动到此自动切卷)'; };
  }), { rootMargin: '2400px', root: mainEl });
  const CH = 80;
  for (const m of members) {
    for (let start = 1; start <= m.pages; start += CH) {
      const frag = document.createDocumentFragment(), phs = [];
      const end = Math.min(start + CH - 1, m.pages);
      for (let k = start; k <= end; k++) {
        const ph = document.createElement('div');
        ph.className = 'grp-fbox';
        ph.dataset.fRel = m.rel; ph.dataset.fLp = k; ph.dataset.fGlobal = m.offset + k;
        ph.dataset.fMt = m.mtime || 0; ph.dataset.fLoaded = '0';
        ph.style.cssText = 'position:relative;background:#fff;color:#888;display:flex;align-items:center;justify-content:center;margin:0 auto 12px;';
        if (refW) ph.style.width = refW;
        ph.style.height = refH;
        ph.textContent = '… 第 ' + (m.offset + k) + ' 页';
        phs.push(ph); frag.appendChild(ph);
      }
      container.appendChild(frag);
      phs.forEach((ph) => fio.observe(ph));
      await new Promise((r) => setTimeout(r, 0));   // 同款分批,不冻主线程
    }
  }
  window.__grpCorridor = true;
  // 外卷区:页码指示(200ms 节流,只改显示不动 currentPage)+ 停稳 800ms 换会话
  let indT = null, idleT = null;
  const centerBox = () => {
    const r = mainEl.getBoundingClientRect();
    const el = document.elementFromPoint(r.left + mainEl.clientWidth / 2, r.top + mainEl.clientHeight / 2);
    return el && el.closest ? el.closest('.grp-fbox') : null;
  };
  mainEl.addEventListener('scroll', () => {
    if (!indT) indT = setTimeout(() => {
      indT = null;
      const fb = centerBox();
      if (fb) { const pc = document.getElementById('page-cur'); if (pc) pc.textContent = fb.dataset.fGlobal; }
    }, 200);
    if (idleT) clearTimeout(idleT);
    idleT = setTimeout(() => {
      const fb = centerBox();
      if (fb) location.href = '/pdf/view?file=' + encodeURIComponent(fb.dataset.fRel) + '&page=' + fb.dataset.fLp;
    }, 800);
  }, { passive: true });
}
// ──────── char-layer：PyMuPDF char-level bbox 驱动的精确选中 ────────

let _charSel = null;   // {pw, startIdx, endIdx, dragging}

// chars → charBoxes(坐标映射 + reading-order 排序)。初次建层 + cv 校正重取 都用它。
// PyMuPDF rawdict bbox 已是 image coordinate(y 向下,原点左上),不做 y 翻转(翻了会上下颠倒)。
function _mapCharBoxes(chars, scale) {
  const cb = chars.map((ch, _oi) => ({
    c: ch.c, _oi,
    w: (ch.w == null ? -1 : ch.w),
    bk: (ch.bk == null ? -1 : ch.bk),
    left: ch.x0 * scale, top: ch.y0 * scale,
    width: (ch.x1 - ch.x0) * scale, height: (ch.y1 - ch.y0) * scale,
    sp: !!ch.sp,
    fml: !!ch.fml, flx: ch.flx || '',   // 公式注入字符:fml 标记 + 首字符带原始 latex(flx)
    _x0: ch.x0, _y0: ch.y0, _x1: ch.x1, _y1: ch.y1,
  }));
  cb.sort((a, b) => {
    const aBase = a.top + a.height, bBase = b.top + b.height;
    const ref = Math.max(a.height, b.height) || 1;
    if (Math.abs(aBase - bBase) > ref * 0.8) return aBase - bBase;
    if (a.w !== -1 && a.w === b.w) return a._oi - b._oi;   // 同词内严格 reading order(连字/紧排根治)
    if (Math.abs(a.left - b.left) < ref * 0.3) return a._oi - b._oi;
    return a.left - b.left;
  });
  return cb;
}

async function loadCharsAndBindLayer(num, wrap, viewport, _retry) {
  _retry = _retry || 0;
  if (!wrap.isConnected) return;   // 页已被连续模式释放 → 放弃
  const scale = viewport.scale;
  const cvKey = 'pdf-cv:' + FILE_REL + ':' + num;
  let cvGuess; try { cvGuess = localStorage.getItem(cvKey) || ('v' + CHARS_VER); } catch (_) { cvGuess = 'v' + CHARS_VER; }
  const charsUrl = (cv) => `/pdf/api/page-chars?file=${encodeURIComponent(FILE_REL)}&page=${num}&v=${CHARS_VER}&cv=${encodeURIComponent(cv)}`;
  // overlay(生词/句子/真 cv)**并行**拉,不阻塞选词层;chars 用上次 cv 猜测 → 命中 SW 缓存秒回 → 选词立即可用
  const ovP = fetch(`/pdf/api/page-overlay?file=${encodeURIComponent(FILE_REL)}&page=${num}`).then(r => r.json()).catch(() => null);
  let d = null;
  try { d = await (await fetch(charsUrl(cvGuess))).json(); } catch (e) { d = null; }
  if (!d || !d.ok) {
    if (_retry < 2 && wrap.isConnected) {
      await new Promise(res => setTimeout(res, 500 + _retry * 600));
      return loadCharsAndBindLayer(num, wrap, viewport, _retry + 1);
    }
    window.dlog?.('chars api fail: ' + ((d && d.error) || 'fetch') + ' (retry ' + _retry + ')', '#ff6b6b');
    return;
  }
  const charBoxes = _mapCharBoxes(d.chars, scale);
  wrap.__pageWPt = d.page_w;
  wrap.__pageHPt = d.page_h;
  wrap.__viewportScale = scale;
  // 可视化文字框(URL ?dbg=1 或 设置开关)
  let _cbOn = new URLSearchParams(location.search).get('dbg') === '1';
  try { _cbOn = _cbOn || localStorage.getItem('pdf-charbox') === '1'; } catch (_) {}
  if (_cbOn) {
    const dbgLayer = ensurePageLayer(wrap, 'char-dbg-layer');
    dbgLayer.innerHTML = '';
    dbgLayer.style.cssText = 'position:absolute;inset:0;pointer-events:none;z-index:5';
    charBoxes.forEach((c) => {
      const e = document.createElement('div');
      e.style.cssText = `position:absolute;left:${c.left}px;top:${c.top}px;width:${c.width}px;height:${c.height}px;border:1px solid rgba(255,0,0,.4);font-size:9px;color:rgba(255,0,0,.8);line-height:${c.height}px;text-align:center;font-family:monospace`;
      e.textContent = c.sp ? '·' : c.c;
      dbgLayer.appendChild(e);
    });
    wrap.appendChild(dbgLayer);
  }
  wrap.__charBoxes = charBoxes;
  wrap.__charsBaseW = wrap.classList.contains('crop-on') ? (parseFloat(wrap.style.getPropertyValue('--full-w')) || wrap.clientWidth || 0) : (wrap.clientWidth || 0);   // #51:建层整页布局宽基准(去边=整页 --full-w,charBox 是整页坐标;非去边=clientWidth);重渲后按 char-layer 实时 BCR/baseW 换算
  try { window.__applyPhraseMergesLocal && window.__applyPhraseMergesLocal(wrap); } catch (_) {}   // 本地词组合并(收藏集驱动,教义:本地算)
  window.dlog?.('chars: ' + charBoxes.length + ' on page ' + num);
  // 创建 char-layer（透明覆盖整个 page-wrap）→ 绑定后**选词此刻即可用**(不等 overlay)
  const cl = ensurePageLayer(wrap, 'char-layer');
  wrap.__charLayer = cl;
  _bindCharLayer(cl, wrap);
  try { renderHighlightsOnPage(wrap, num); } catch(e) { window.dlog?.('hl render fail: '+e.message,'#ff6b6b'); }
  // 振假名/音标（chars 那条带的 furigana）
  wrap.__furigana = d.furigana || [];
  wrap.__furiVerified = false;
  try { renderRubyLayer(wrap); } catch(e) { window.dlog?.('ruby fail: '+e.message,'#ff6b6b'); }
  if (_rubyEnabled()) _verifyFurigana(wrap);
  try { renderPhraseHl(wrap); } catch(_) {}
  try { renderExplainHl(wrap); } catch(_) {}
  try { renderWordHl(wrap); } catch(_) {}
  try { window.renderFiguresOnPage && renderFiguresOnPage(wrap, num); } catch(_) {}   // 页级图注 Apple 徽标
  if (_pageTrOn) { wrap.__pageTrSeq = null; _pageTranslatePage(wrap); }
  if (window._pendingSearchHighlight && window._pendingSearchHighlight.page === num
      && wrap.__charBoxes && wrap.__charBoxes.length) {
    try { _highlightSearchResultsOnPage(wrap, window._pendingSearchHighlight.query); } catch (_) {}
    window._pendingSearchHighlight = null;
  }
  // —— overlay 到了(慢:服务端跑分词/生词,不阻塞上面的选词)：渲染生词/句子 + 校正 cv ——
  ovP.then(async (ov) => {
    if (!wrap.isConnected) return;
    if (ov && ov.ok && ov.cv) {
      try { localStorage.setItem(cvKey, ov.cv); } catch (_) {}   // 记真 cv,下次秒命中缓存
      if (ov.cv !== cvGuess) {
        // cvGuess 猜错(内容自上次起变过)→ 刚 chars 可能来自旧 SW 缓存 → 用真 cv 重取并刷新选词数据
        try {
          const d2 = await (await fetch(charsUrl(ov.cv))).json();
          if (d2 && d2.ok && wrap.isConnected) {
            wrap.__charBoxes = _mapCharBoxes(d2.chars, viewport.scale);
            wrap.__charsBaseW = wrap.classList.contains('crop-on') ? (parseFloat(wrap.style.getPropertyValue('--full-w')) || wrap.clientWidth || 0) : (wrap.clientWidth || 0);   // #51:cv 校正重建同步基准宽(整页布局宽)
            wrap.__pageWPt = d2.page_w; wrap.__pageHPt = d2.page_h;
            wrap.__furigana = d2.furigana || [];
            try { renderRubyLayer(wrap); } catch (_) {}
          }
        } catch (_) {}
      }
    }
    wrap.__vocabMarks = (ov && ov.vocab_marks) || [];
    wrap.__vocabSentences = (ov && ov.vocab_sentences) || [];
    wrap.__masteredFuri = new Set((ov && ov.mastered_furi) || []);   // 已掌握词面 → ruby 跳过其注音
    try { renderVocabUnderlines(wrap, wrap.__vocabMarks); } catch(e) { window.dlog?.('vocab underline fail: '+e.message,'#ff6b6b'); }
    try { renderRubyLayer(wrap); } catch (_) {}   // overlay 到了(含已掌握词集)→ 重画 ruby(首渲时还没这个集,此刻把已掌握词的注音去掉)
    try { renderVocabSentences(wrap, wrap.__vocabSentences); } catch(e) { window.dlog?.('vocab sentence fail: '+e.message,'#ff6b6b'); }
  });
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

// 共享 vocabulary-state 只参与可见投影；业务写入仍由 rc-wordpop/phrasepop 负责。
// 仓库缺失、尚未 hydrate 或没有该词记录时返回 false，后面的本地镜像/服务端 label
// 继续作为兼容 fallback。
function _vocabularyStateRepo() {
  try {
    const repo = window.BWReaderRuntime && window.BWReaderRuntime.vocabularyState;
    return repo &&
      repo.CONTRACT === 'vocabulary-state/1' &&
      typeof repo.isMastered === 'function'
      ? repo
      : null;
  } catch (_) { return null; }
}
function _vocabularyStateMarkMastered(mark) {
  const repo = _vocabularyStateRepo();
  if (!repo || !mark) return false;
  const word = String(mark.word || mark.surface || '').trim();
  const lemma = String(mark.lemma || word).trim();
  if (!lemma && !word) return false;
  try {
    return repo.isMastered({
      kind: 'word',
      language: mark.language || mark.lang || (mark.jp ? 'ja' : 'en'),
      lemma: lemma || word,
      word: word || lemma,
      surface: word || lemma,
      forms: Array.isArray(mark.forms) ? mark.forms : []
    });
  } catch (_) { return false; }
}

function renderVocabUnderlines(pw, marks) {
  if (!_vocabUnderlineEnabled()) return;
  // §18.5 local-first:服务端回**全候选**(含已掌握,label_slug='mastered'),渲染时本地过滤。
  // 共享仓库优先补充已掌握事实；旧 __masteredLocal/__vocabOverride 与服务端 label
  // 继续兜底，且 dirty 标记只能在真正收到服务器 mastery snapshot 时收敛。
  try {
    const _ovr = window.__vocabOverride;
    marks = (marks || []).filter((m) => {
      const k = String(m.lemma || m.word || '').toLowerCase();
      if (_vocabularyStateMarkMastered(m)) return false;
      if (_ovr && _ovr.has(k)) return !_ovr.get(k);
      if (window.__masteredLocal) return !window.__masteredLocal.has(k);
      return m.label_slug !== 'mastered';
    });
  } catch (_) {}
  // 确保有 layer（即使 marks 空也要清旧残留）
  let layer = pw.querySelector('.vocab-layer');
  if (!layer && marks && marks.length) {
    layer = ensurePageLayer(pw, 'vocab-layer');
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
  // 翻到本页 → 后台空闲预热这些生词的释义进查词缓存(点开秒显;prewarm=1 纯读,不 bump 暴露/不建笔记,不污染掌握度)。
  //   切书=整页重载自动清缓存;600 上限自然淘汰旧页的词。
  try { if (window.RC && RC.wordpop && RC.wordpop.prewarm) RC.wordpop.prewarm(marks.map(m => m.word).filter(Boolean)); } catch (_) {}
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
  if (!layer) layer = ensurePageLayer(pw, 'ruby-layer');
  layer.innerHTML = '';
  if (!items.length) return;
  const canvas = pw.querySelector('canvas');
  const cssW = canvas?.clientWidth || pw.clientWidth;
  const cssH = canvas?.clientHeight || pw.clientHeight;
  const pageWPt = pw.__pageWPt || cssW;
  const pageHPt = pw.__pageHPt || cssH;
  if (!cssW || !cssH || !pageWPt || !pageHPt) return;
  const sx = cssW / pageWPt, sy = cssH / pageHPt;
  const mastered = pw.__masteredFuri;   // 已掌握词面集(page-overlay 给);已掌握=不注音
  for (const it of items) {
    if (mastered && it.wd && mastered.has(it.wd)) continue;   // 已掌握的词跳过假名/音标
    const sp = _makeRubySpan(it, sx, sy);
    if (sp) layer.appendChild(sp);
  }
}
// 单个振假名/音标条目 → .rt span。renderRubyLayer 和「查词等待注音」共用,保证显示一致。
// 字号≈词高 36% 且受词宽/读音字数约束(横向不超词宽);略偏下贴本行。须放进 .ruby-layer 容器(CSS 作用域)。
function _makeRubySpan(it, sx, sy) {
  const rt = it.rt || ''; if (!rt) return null;
  const x0 = it.x0 * sx, y0 = it.y0 * sy, x1 = it.x1 * sx, y1 = it.y1 * sy;
  const w = Math.max(6, x1 - x0), h = Math.max(6, y1 - y0);
  const fs = Math.max(7, Math.min(h * 0.36, w / Math.max(1, rt.length) * 1.0));
  const sp = document.createElement('span');
  sp.className = 'rt';
  sp.textContent = rt;
  sp.style.left = x0 + 'px';
  sp.style.width = w + 'px';
  sp.style.fontSize = fs.toFixed(1) + 'px';
  sp.style.top = Math.max(0, y0 - fs * 0.34) + 'px';
  return sp;
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
        '<span class="sr-pg">P' + (window._dispPage ? window._dispPage(m.page) : m.page) + (m.count > 1 ? '·' + m.count : '') + '</span>' +
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
  if (!layer) layer = ensurePageLayer(pw, 'vocab-layer');
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
// trailing-coalesce:运行中再触发只记 rerun、跑完补一轮——1.8s/3.5s/1.5s 多轮是故意错峰等服务端
// vocab note 写盘的,直接 skip 会停在旧态;可视页优先 + 并发 3 小池(无界 Promise.all 会打满
// 单 worker 8 gthread,饿死滚动渲染请求)。
let _vocabRefreshing = false, _vocabRerun = false;
async function refreshVocabUnderlinesForAllPages() {
  if (!_vocabUnderlineEnabled() || !pdfDoc) return;
  if (_vocabRefreshing) { _vocabRerun = true; return; }
  _vocabRefreshing = true;
  try {
    do {
      _vocabRerun = false;
      // 单页模式当前页是 #page-container 自身(无 .page-wrap class)，故按 data-loaded+page-num 选，覆盖两种模式
      const wraps = [...document.querySelectorAll('[data-loaded="1"][data-page-num]')];
      // 可视页优先:用户正看的页下划线先变
      const vh = window.innerHeight || 0;
      const inView = (pw) => { const r = pw.getBoundingClientRect(); return r.bottom > 0 && r.top < vh; };
      wraps.sort((a, b) => (inView(b) ? 1 : 0) - (inView(a) ? 1 : 0));
      let next = 0;
      const worker = async () => {
        while (next < wraps.length) {
          const pw = wraps[next++];
          const pn = parseInt(pw.dataset.pageNum || '0', 10);
          if (!pn) continue;
          try {
            const r = await fetch('/pdf/api/page-vocab-marks?file=' + encodeURIComponent(FILE_REL) + '&page=' + pn);
            const d = await r.json();
            if (!d.ok) continue;
            pw.__vocabMarks = d.vocab_marks || [];
            pw.__vocabSentences = d.vocab_sentences || [];
            pw.__masteredFuri = new Set(d.mastered_furi || []);   // 刚标掌握 → 更新已掌握词集
            renderVocabUnderlines(pw, pw.__vocabMarks);
            renderVocabSentences(pw, pw.__vocabSentences);
            try { renderRubyLayer(pw); } catch (_) {}   // 重画 ruby:标掌握的词当下就不再显示假名注音
          } catch (e) { window.dlog?.('vocab refresh p.' + pn + ' fail: ' + e.message, '#ff6b6b'); }
        }
      };
      await Promise.all([worker(), worker(), worker()]);
    } while (_vocabRerun);
  } finally { _vocabRefreshing = false; }
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

// 找字符 ch 所在的「行 rect」(y 中心落其中、且比 ch 明显宽 → 排除退化小 rect)。找不到回退 ch 本身。
// 用途:句末字常是小标点「。」(高度仅 ~15pt 且低在行底),直接拿它做 L 竖线会太短/难点 → 改用整行高。
function _lineRectOf(ch, rects) {
  const cy = (ch[1] + ch[3]) / 2, cw = ch[2] - ch[0];
  let best = null;
  for (const r of (rects || [])) {
    if (r[1] - 1 <= cy && cy <= r[3] + 1 && (r[2] - r[0]) > cw * 1.5) {
      if (!best || (r[3] - r[1]) > (best[3] - best[1])) best = r;   // 取最高的(最像真行)
    }
  }
  return best || ch;
}
function renderVocabSentences(pw, sentences) {
  if (!_vocabUnderlineEnabled()) return;
  let layer = pw.querySelector('.vocab-layer');
  if (!layer && sentences && sentences.length) layer = ensurePageLayer(pw, 'vocab-layer');
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
  // 可见窗口(样式坐标)：去边时 .crop-on>* 给本层加了 translate(-crop-l,-crop-t)，wrap overflow:hidden 裁切。
  // 所以 L 按钮的 left/top 落在 [cropL, cropL+裁后宽] × [cropT, cropT+裁后高] 内才不被裁。非去边 = [0,全宽]。
  const _cropL = parseFloat(pw.style.getPropertyValue('--crop-l')) || 0;
  const _cropT = parseFloat(pw.style.getPropertyValue('--crop-t')) || 0;
  const _visW = layer.clientWidth || cssW, _visH = layer.clientHeight || cssH;
  const visL = _cropL, visR = _cropL + _visW, visT = _cropT, visB = _cropT + _visH;
  const GAP = 3;   // L 边框落在字外侧间隙(看得见,不压字形);贴可见边界时夹回(防被裁)
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
      // 「⌐」左/上边框落在首字外侧间隙(看得见、不压字形)；贴可见左/上边界时夹回(否则竖/横线被裁)
      let left0 = Math.max(visL, fc[0] * sx - GAP);
      let top0  = Math.max(visT, fc[1] * sy - GAP);
      // clamp 到句首所在行的文本右边界(rects[0][2])，不伸进纸张右 margin → 不画出延伸到页边的线
      const lineRight = (rects[0] ? rects[0][2] * sx : visR);
      const maxW0 = Math.max(charW0, lineRight - left0);
      // 角标臂长固定 ~44px(不再按字宽×6 → CJK 首字 34pt×6=204pt 像上划线;首尾一致干净小角标)
      const wantW0 = Math.min(Math.max(charW0, 44), maxW0);
      btn0.style.left = left0 + 'px';
      btn0.style.top = top0 + 'px';
      btn0.style.width = wantW0 + 'px';
      btn0.style.height = (fc[3] * sy - top0) + 'px';   // 到首字底
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
      // 加宽：L 形左缘向左延伸（更易点击）。border-box：右/下外缘**贴齐末字右/下边缘**(不再 +6 外扩到页边
      // margin)→ 去边模式下「⌟」的右/下边不会被 overflow:hidden 裁成半截(末字本身在可见区内,边框必可见)。
      const charW = (lc[2] - lc[0]) * sx;
      const wantW = Math.max(charW, 44);   // 角标臂长固定 ~44px(跟句首一致;不再按字宽×6)
      // 竖线高度用末字**所在行**的高度,不用小标点「。」(15pt)本身 → 否则竖线太短像只剩横线、点击区也太小点不中。
      const lcLine = _lineRectOf(lc, rects);
      // 「⌟」右/下边框落在末字外侧间隙(看得见、不压字形)；贴可见右/下边界时夹回(否则竖/横线被裁)
      const rightE = Math.min(visR, lc[2] * sx + GAP);
      const botE   = Math.min(visB, lcLine[3] * sy + GAP);   // 行底
      const top    = Math.max(visT, lcLine[1] * sy);          // 行顶
      let leftE = Math.max(visL, rightE - wantW);   // 向左延伸;行首时夹到可见左界
      btn.style.left = leftE + 'px';
      btn.style.top = top + 'px';
      btn.style.width = Math.max(charW, rightE - leftE) + 'px';
      btn.style.height = Math.max(1, botE - top) + 'px';
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
      _bindSentBtnLongPress(btn, s, pw);   // 所有句(自动+手动)L 按钮:长按弹菜单(重新翻译[+删除,仅手动])
      layer.appendChild(btn);
    }
    if (s.first_char) {
      const b0 = layer.querySelector(`.vocab-sentence-btn-l-start[data-sid="${sid}"]`);
      if (b0) _bindSentBtnLongPress(b0, s, pw);
    }
  }
}

// L 框长按 → 弹菜单(🔄 重新翻译 [+ 🗑 删除,仅手动框])。短按仍是显示/隐藏译文(不重译,防误触重译干扰)。
// 与短按/拖选共存：移动或短按则取消；触发后吃掉随后的 click。
function _bindSentBtnLongPress(btn, s, pw) {
  let timer = null, x0 = 0, y0 = 0, fired = false;
  btn.addEventListener('pointerdown', (e) => {
    x0 = e.clientX; y0 = e.clientY; fired = false;
    clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null; fired = true;
      if (navigator.vibrate) { try { navigator.vibrate(30); } catch (_) {} }
      _showSentMenu(btn, s, pw);
    }, 550);
  });
  const cancel = (e) => {
    if (timer && e && e.type === 'pointermove' && Math.hypot(e.clientX - x0, e.clientY - y0) < 12) return;
    clearTimeout(timer); timer = null;
  };
  btn.addEventListener('pointermove', cancel);
  btn.addEventListener('pointerup', cancel);
  btn.addEventListener('pointercancel', cancel);
  // 长按已弹菜单 → 吃掉随后的 click，避免又触发整句翻译/显示
  btn.addEventListener('click', (e) => { if (fired) { fired = false; e.stopPropagation(); e.preventDefault(); } }, true);
}
// 句子 L 按钮长按菜单
function _showSentMenu(btn, s, pw) {
  document.querySelectorAll('.sent-menu').forEach(m => m.remove());
  const menu = document.createElement('div');
  menu.className = 'sent-menu';
  let html = '<button type="button" data-act="re">🔄 重新翻译</button>';
  if (s.manual) html += '<button type="button" data-act="del">🗑 删除标记</button>';
  menu.innerHTML = html;
  document.body.appendChild(menu);
  const r = btn.getBoundingClientRect();
  menu.style.left = Math.max(6, Math.min(r.left, window.innerWidth - menu.offsetWidth - 6)) + 'px';
  menu.style.top = Math.min(r.bottom + 4, window.innerHeight - menu.offsetHeight - 6) + 'px';
  menu.addEventListener('click', (e) => {
    const b = e.target.closest('button'); if (!b) return;
    e.stopPropagation(); e.preventDefault();
    const act = b.dataset.act; menu.remove();
    if (act === 're') _sentRetranslate(s, pw);
    else if (act === 'del') _sentDismiss(s, pw);   // 长按菜单已是刻意操作,直接删,不再二次确认
  });
  setTimeout(() => {
    const close = (ev) => { if (!menu.contains(ev.target)) { menu.remove(); document.removeEventListener('pointerdown', close, true); } };
    document.addEventListener('pointerdown', close, true);
  }, 0);
}
// 强制重新翻译该句(单句翻译后端不缓存 → 必出新结果),重画译文浮层
function _sentRetranslate(s, pw) {
  if (!s || !pw) return;
  const lay0 = pw.querySelector('.vocab-layer');
  if (lay0) lay0.querySelectorAll('.vocab-sentence-overlay').forEach(el => el.remove());   // 关旧译文
  s.zh = ''; s.__translating = true;
  try { renderVocabSentences(pw, pw.__vocabSentences); } catch (_) {}   // 呼吸表示翻译中
  fetch('/pdf/api/translate-sentence', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text: s.text, fresh: 1 }),   // fresh:绕后端缓存,必出新结果(覆盖旧/坏译文)
  }).then(r => r.json()).then(d => {
    s.__translating = false;
    if (d.ok && d.zh) { s.zh = d.zh; try { renderVocabSentences(pw, pw.__vocabSentences); } catch (_) {} _reopenSentOverlay(pw, s); _toast?.('已重新翻译'); }
    else { try { renderVocabSentences(pw, pw.__vocabSentences); } catch (_) {} _toast?.('翻译失败：' + (d.error || '?')); }
  }).catch(e => { s.__translating = false; try { renderVocabSentences(pw, pw.__vocabSentences); } catch (_) {} _toast?.('网络错误：' + e.message); });
}
// 重画某句的就地译文浮层(renderVocabSentences 重建按钮后,按 sid 找回 L 按钮 + sx/sy)
function _reopenSentOverlay(pw, s) {
  const layer = pw.querySelector('.vocab-layer'); if (!layer || !s.zh) return;
  const canvas = pw.querySelector('canvas');
  const cssW = canvas?.clientWidth || pw.clientWidth, cssH = canvas?.clientHeight || pw.clientHeight;
  const pageWPt = pw.__pageWPt || cssW, pageHPt = pw.__pageHPt || cssH;
  if (!cssW || !cssH || !pageWPt || !pageHPt) return;
  const sx = cssW / pageWPt, sy = cssH / pageHPt;
  const si = (pw.__vocabSentences || []).indexOf(s);
  const btn = layer.querySelector(`.vocab-sentence-btn-l[data-sid="${si}"]`)
           || layer.querySelector(`.vocab-sentence-btn-l-start[data-sid="${si}"]`);
  if (btn) _drawSentenceOverlay(layer, s, btn, sx, sy);
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
  const panel = document.getElementById('grammar-panel') || document.getElementById('ep-side');   // 唯一抽屉模式下根 id=ep-side(body 类仍镜像 grammar-open)
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
// §18.5:掌握 toggle 的本地即时应用(0ms)——记覆盖 → 已渲染页用手头 __vocabMarks 本地重画,零网络。
// §18.6(用户指出"应先存本地"):覆盖表**持久化** localStorage(离线标记后刷新不丢),48h TTL
// (防多设备冲突:别处改了掌握态,本机陈旧覆盖最多赢 48h);服务端数据追上后由渲染层自动收敛清理。
const _VOVR_KEY = 'vocab-override-v1';
window.__vocabOverrideTs = new Map();
window.__vocabOverride = (() => {
  const m = new Map();
  try {
    const raw = JSON.parse(localStorage.getItem(_VOVR_KEY) || '{}');
    const now = Date.now();
    for (const k in raw) {
      if (raw[k] && (now - (raw[k].ts || 0)) < 48 * 3600 * 1000) {
        m.set(k, !!raw[k].v);
        window.__vocabOverrideTs.set(k, Number(raw[k].ts) || now);
      }
    }
  } catch (_) {}
  return m;
})();
window.__vocabOverridePersist = function () {
  try {
    const out = {};
    window.__vocabOverride.forEach((v, k) => { out[k] = { v: v, ts: (window.__vocabOverrideTs && __vocabOverrideTs.get(k)) || Date.now() }; });
    localStorage.setItem(_VOVR_KEY, JSON.stringify(out));
  } catch (_) {}
};
// §18.7 本地掌握库:开书拉全量 mastered 清单落 localStorage(SW netFallback → 离线用缓存快照);
// toggle 本地增删 → 掌握判定的**事实源在本地**(下划线过滤/wordpop 初始态都优先用它,脱离服务器)。
window.__masteredLocal = (() => { try { const r0 = JSON.parse(localStorage.getItem('vocab-mastered-v1') || 'null'); return new Set(r0 && Array.isArray(r0.set) ? r0.set : []); } catch (_) { return new Set(); } })();
window.__masteredLocalSave = function () { try { localStorage.setItem('vocab-mastered-v1', JSON.stringify({ ts: Date.now(), set: [...(window.__masteredLocal || [])] })); } catch (_) {} };
(async () => {
  try {
    const d = await (await fetch('/pdf/api/vocab-mastery-map?all=1&file=' + encodeURIComponent(typeof FILE_REL !== 'undefined' ? FILE_REL : ''))).json();
    if (d && d.ok && Array.isArray(d.mastered)) {
      const server = new Set(d.mastered.map((x) => String(x).toLowerCase()));
      let dirty = false;
      // 服务端 snapshot 只确认已经追上的 mutation；仍未追上的本地变更继续盖在 snapshot 上，
      // 避免启动时较慢的 GET 把用户刚点下去的状态覆盖回去。
      window.__vocabOverride.forEach((value, key) => {
        if (server.has(key) === value) {
          window.__vocabOverride.delete(key);
          window.__vocabOverrideTs.delete(key);
          dirty = true;
        } else if (value) server.add(key);
        else server.delete(key);
      });
      if (dirty) window.__vocabOverridePersist();
      window.__masteredLocal = server;
      window.__masteredLocalSave();
      document.querySelectorAll('[data-loaded="1"][data-page-num]').forEach((pw) => { if (pw.__vocabMarks) { try { renderVocabUnderlines(pw, pw.__vocabMarks); } catch (_) {} } });
    }
  } catch (_) {}
})();
window.applyVocabLocalOverride = function (lemma, mastered, meta) {
  try {
    const k = String(lemma || '').toLowerCase();
    if (!k) return function () {};
    const hadMastered = window.__masteredLocal.has(k);
    const hadOverride = window.__vocabOverride.has(k);
    const oldOverride = window.__vocabOverride.get(k);
    const oldTs = window.__vocabOverrideTs.get(k);
    if (mastered) __masteredLocal.add(k); else __masteredLocal.delete(k);
    window.__masteredLocalSave();
    __vocabOverrideTs.set(k, Date.now());
    window.__vocabOverride.set(k, !!mastered);
    window.__vocabOverridePersist();
    const keys = new Set([k, meta && meta.word, ...((meta && meta.forms) || [])]
      .filter(Boolean).map((value) => String(value).toLowerCase()));
    const paint = () => document.querySelectorAll('[data-loaded="1"][data-page-num]').forEach((pw) => {
      if (!pw.__vocabMarks) return;
      const hit = pw.__vocabMarks.some((mark) =>
        keys.has(String(mark.lemma || '').toLowerCase()) ||
        keys.has(String(mark.word || '').toLowerCase()));
      if (hit) { try { renderVocabUnderlines(pw, pw.__vocabMarks); } catch (_) {} }
    });
    paint();
    return function restoreVocabLocalOverride() {
      if (hadMastered) window.__masteredLocal.add(k); else window.__masteredLocal.delete(k);
      if (hadOverride) {
        window.__vocabOverride.set(k, oldOverride);
        window.__vocabOverrideTs.set(k, oldTs);
      } else {
        window.__vocabOverride.delete(k);
        window.__vocabOverrideTs.delete(k);
      }
      window.__masteredLocalSave();
      window.__vocabOverridePersist();
      paint();
    };
  } catch (_) { return function () {}; }
};

// vocabulary-state hydrate/provider 更新后只用现有 __vocabMarks 重画，不触发 mastery GET。
// 这让刷新页面后从 IndexedDB/Vault 晚到的掌握状态也能在当前帧附近消掉下划线。
(function _bindVocabularyStateUnderlineProjection() {
  const repo = typeof _vocabularyStateRepo === 'function'
    ? _vocabularyStateRepo()
    : null;
  if (!repo) return;
  let queued = false;
  const paint = () => {
    queued = false;
    document.querySelectorAll('[data-loaded="1"][data-page-num]').forEach((pw) => {
      if (!pw.__vocabMarks) return;
      try { renderVocabUnderlines(pw, pw.__vocabMarks); } catch (_) {}
    });
  };
  const schedule = () => {
    if (queued) return;
    queued = true;
    if (window.requestAnimationFrame) window.requestAnimationFrame(paint);
    else setTimeout(paint, 0);
  };
  try {
    if (typeof repo.subscribe === 'function') {
      repo.subscribe((event) => {
        const record = event && event.record;
        if (!record || record.property === 'mastered') schedule();
      });
    }
  } catch (_) {}
  try {
    if (typeof repo.ready === 'function') Promise.resolve(repo.ready()).then(schedule, () => {});
  } catch (_) {}
  try { document.addEventListener('bw:vocabulary-state-ready', schedule); } catch (_) {}
})();

// 找点击位置最近的非空格 char index
// 拖选期间要临时禁点的呼吸高亮 .hl（查词/词组/解释）——防拖选经过它们被截获(丢 move/up + 误弹)
const _OVL_HL_SEL = '.word-hl-layer .hl, .phrase-hl-layer .hl, .explain-hl-layer .hl';
// 根治点击归属:char-layer 收到每次点击后,用**几何**命中(getBoundingClientRect,与 .hl 的 pointer-events 状态无关)
// 判断是否落在查询高亮(词组/查词/解释)上。命中 → 交给该高亮动作(不选字/不查词);否则正常选字。
// 这是"状态无关的裁决",不依赖 pointer-events 抢占 → 根除"穿透到 char-layer 误查手指下的字"整类 bug。
function _overlayHlHitAtClient(pw, cx, cy) {
  const hls = document.querySelectorAll(_OVL_HL_SEL);   // 全文档搜(不限本页 pw):双页模式下词组高亮可能在别的 pw;getBoundingClientRect 包含判断只会命中点击点上的那个,不误命中别页
  for (let i = hls.length - 1; i >= 0; i--) {   // 逆序:后插入(DOM 靠后)= z:6 平级里视觉在上,先命中它
    const r = hls[i].getBoundingClientRect();
    if (r.width && r.height && cx >= r.left && cx <= r.right && cy >= r.top && cy <= r.bottom) {
      const layer = hls[i].closest('.phrase-hl-layer, .word-hl-layer, .explain-hl-layer');
      const kind = !layer ? '' : (layer.classList.contains('phrase-hl-layer') ? 'phrase'
        : layer.classList.contains('explain-hl-layer') ? 'explain' : 'word');
      return { kind, layer, el: hls[i] };
    }
  }
  return null;
}

// ⚡ charBoxes 的像素坐标是 loadCharsAndBindLayer 当时的 scale 烘焙的;refit/双页切换等竞态后
// 可能与当前显示尺寸脱节(实测 応用情報 p37 偏 19% → 页面右缘/底部整片点不中,而振假名/下划线
// 反而是准的——它们渲染时实时用 clientWidth/pageWPt 算比例)。交互入口处按**实时尺寸**重标定:
// boxes 自带 pt 坐标(_x0/_y0/_x1/_y1),O(N) 重算 left/top/width/height,比例没变就跳过。
function _syncCharBoxScale(pw) {
  const cb = pw && pw.__charBoxes;
  if (!cb || !cb.length || !pw.__pageWPt || !pw.__pageHPt) return;
  const ref = pw.__charLayer || pw.querySelector('.char-layer') || pw;
  const w = ref.clientWidth, h = ref.clientHeight;
  if (!w || !h) return;
  const sx = w / pw.__pageWPt, sy = h / pw.__pageHPt;
  if (Math.abs((pw.__cbSX || 0) - sx) < 0.001 && Math.abs((pw.__cbSY || 0) - sy) < 0.001) return;
  if (cb[0]._x0 == null) return;   // 旧结构无 pt 字段 → 放弃(下次重载自然修复)
  for (const c of cb) {
    if (c._x0 == null) continue;
    c.left = c._x0 * sx; c.width  = (c._x1 - c._x0) * sx;
    c.top  = c._y0 * sy; c.height = (c._y1 - c._y0) * sy;
  }
  pw.__cbSX = sx; pw.__cbSY = sy;
}
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
        if (!cjkPair && gap > ref * ((/[A-Za-z]/.test(c.c) && /[A-Za-z]/.test(lastChar.c)) ? 1.3 : 0.6) && !lastChar.sp && !c.sp) text += ' ';   // 0.6 防 justified 词内字距拉伸误拆(如 between→be tween)
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
        if (!cjkPair && gap > ref * ((/[A-Za-z]/.test(c.c) && /[A-Za-z]/.test(lastChar.c)) ? 1.3 : 0.6) && !lastChar.sp && !c.sp) text += ' ';   // 0.6 防 justified 词内字距拉伸误拆(如 between→be tween)
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
  // 给侧栏助手记下选中「所在句」(左右扩到句末标点/段落边界):助手不必每次都 read_page 才有上下文,
  // 直接拿这句判读音/义项(日语同字多音、含义随语境)。整句已被选中就不另存(避免与 selection 重复)。
  try {
    const _sr = _expandSentenceFromRange(chars, sIdx, eIdx);
    if (_sr) {
      const _sent = _charsRangeToText(chars, _sr.start, _sr.end).slice(0, 600);
      const _norm = s => (s || '').replace(/\s+/g, '');
      window.__lastSelSentence = (_sent && _norm(_sent) !== _norm(lastSelText)) ? _sent : '';
    }
  } catch (_) {}
  _charSel = {pw, startIdx: sIdx, endIdx: eIdx, dragging: _charSel?.dragging || false};
  try { window.__lastSelMeta = { page: (typeof _selPageNum === 'function' ? _selPageNum() : (typeof currentPage !== 'undefined' ? currentPage : 0)), t: Date.now() }; } catch (_) {}   // char 层选中也记 meta(页+时间);否则 __voiceContext 新鲜度校验(meta.page===curP && <10min)失败 → 助手拿到空选中
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
  // 防溢出屏：选区靠右/靠下时工具栏(max-width 480)会跑出可见区被裁 → 夹回 #main 可见区
  _clampToolbarIntoView(mainEl, pwRect.top - mainRect.top + mainEl.scrollTop + chars[sIdx].top);
}
// 把选择工具栏夹进 #main 可见区(absolute 定位在可滚动的 #main 内;可见区=滚动偏移+clientW/H)。
// selTopY=选区顶部内容 Y;底部放不下就翻到选区上方。
function _clampToolbarIntoView(mainEl, selTopY) {
  const tb = toolbar;
  const tbW = tb.offsetWidth, tbH = tb.offsetHeight;   // 读 offset 触发 reflow，open 后尺寸已确定
  if (!tbW || !tbH) return;
  const visL = mainEl.scrollLeft, visT = mainEl.scrollTop;
  const visR = visL + mainEl.clientWidth, visB = visT + mainEl.clientHeight;
  let left = parseFloat(tb.style.left) || 0;
  let top  = parseFloat(tb.style.top) || 0;
  if (left + tbW > visR - 8) left = visR - 8 - tbW;   // 右溢 → 左移
  if (left < visL + 8) left = visL + 8;               // 仍左溢 → 贴左
  if (top + tbH > visB - 8) {                         // 底溢 → 翻到选区上方
    const above = (selTopY != null ? selTopY : top) - tbH - 6;
    top = (above >= visT + 8) ? above : Math.max(visT + 8, visB - 8 - tbH);
  }
  tb.style.left = left + 'px';
  tb.style.top  = top + 'px';
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

// 公式注入字符:整条选中(同一 w,不被 / | 等截断)。公式字符是连续追加的,按 w 左右扩到底。
function _formulaBounds(chars, idx) {
  const wid = chars[idx].w;
  let s = idx, e = idx;
  while (s > 0 && chars[s - 1].w === wid && chars[s - 1].fml) s--;
  while (e < chars.length - 1 && chars[e + 1].w === wid && chars[e + 1].fml) e++;
  return { start: s, end: e };
}
// 公式渲染浮层:MathJax 渲染该公式 + 复制LaTeX / 问AI / 制卡。点公式区即弹,点别处消失。
function _formulaRawLatex(chars, b) {
  for (let i = b.start; i <= b.end; i++) if (chars[i].flx) return chars[i].flx;
  // 兜底:从字符 c 拼回并去掉首尾 $ / $$
  let s = chars.slice(b.start, b.end + 1).map(c => c.c).join('');
  s = s.replace(/^\$\$?/, '').replace(/\$\$?$/, '');
  return s;
}
function _ensureFmlPopCss() {
  if (document.getElementById('fml-pop-css')) return;
  const st = document.createElement('style'); st.id = 'fml-pop-css';
  st.textContent =
    '#fml-pop{position:absolute;z-index:150;background:#0f1830;border:1px solid #2f4a7d;border-radius:12px;' +
    'box-shadow:0 10px 30px rgba(0,0,0,.55);padding:10px 12px;max-width:min(92vw,560px);color:#e6eeff}' +
    '#fml-pop .fp-render{overflow-x:auto;overflow-y:hidden;text-align:center;padding:4px 2px 8px;color:#eaf2ff;font-size:18px}' +
    '#fml-pop .fp-tex{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;color:#9fb4e0;background:#0a1120;' +
    'border:1px solid #22325a;border-radius:7px;padding:5px 7px;white-space:pre-wrap;word-break:break-all;max-height:78px;overflow:auto;margin-bottom:8px}' +
    '#fml-pop .fp-btns{display:flex;gap:7px;flex-wrap:wrap}' +
    '#fml-pop .fp-btns button{flex:1 1 auto;min-width:66px;background:#16213e;border:1px solid #2f4a7d;color:#cfe0ff;' +
    'border-radius:8px;padding:7px 6px;font-size:13px;cursor:pointer;-webkit-tap-highlight-color:transparent}' +
    '#fml-pop .fp-btns button:active{background:#22325a}';
  document.head.appendChild(st);
}
function _hideFmlPop() { const p = document.getElementById('fml-pop'); if (p) p.remove(); }
window._hideFmlPop = _hideFmlPop;
function showFormulaPopover(pw, b) {
  _ensureFmlPopCss(); _hideFmlPop();
  const chars = pw.__charBoxes;
  const latex = _formulaRawLatex(chars, b);
  const isBlock = /\\begin\{|\\\\/.test(latex);
  const wrapped = isBlock ? ('$$' + latex + '$$') : ('$' + latex + '$');
  const copyStr = isBlock ? ('$$' + latex + '$$') : ('$' + latex + '$');
  const pop = document.createElement('div'); pop.id = 'fml-pop';
  const render = document.createElement('div'); render.className = 'fp-render';
  render.textContent = isBlock ? ('$$' + latex + '$$') : ('\\(' + latex + '\\)');
  const tex = document.createElement('div'); tex.className = 'fp-tex'; tex.textContent = copyStr;
  const btns = document.createElement('div'); btns.className = 'fp-btns';
  const mk = (label, fn) => { const x = document.createElement('button'); x.textContent = label; x.addEventListener('click', (ev) => { ev.stopPropagation(); fn(); }); return x; };
  btns.appendChild(mk('📋 复制', () => {
    (navigator.clipboard ? navigator.clipboard.writeText(copyStr) : Promise.reject()).then(() => { if (window._toast) window._toast('已复制 LaTeX'); }).catch(() => {
      try { const ta = document.createElement('textarea'); ta.value = copyStr; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); if (window._toast) window._toast('已复制 LaTeX'); } catch (_) {}
    });
  }));
  btns.appendChild(mk('💡 问 AI', () => { _hideFmlPop(); try { window.onExplain && window.onExplain(); } catch (_) {} }));
  btns.appendChild(mk('🃏 制卡', () => {
    _hideFmlPop();
    try { window.addDraftText && window.addDraftText(copyStr, '公式', FILE_REL + (typeof currentPage !== 'undefined' ? ('#p' + currentPage) : '')); } catch (_) {}
    try { window.openDraftModal && window.openDraftModal(); } catch (_) {}
  }));
  pop.appendChild(render); pop.appendChild(tex); pop.appendChild(btns);
  const mainEl = document.getElementById('viewer') || document.querySelector('.viewer') || document.body;
  mainEl.appendChild(pop);
  // 定位:公式上方(放不下则下方)。用选区起止字符的页内坐标。
  try {
    const pwRect = pw.getBoundingClientRect(), mainRect = mainEl.getBoundingClientRect();
    const c0 = chars[b.start], cN = chars[b.end];
    const leftPx = pwRect.left - mainRect.left + mainEl.scrollLeft + Math.min(c0.left, cN.left);
    const topAbove = pwRect.top - mainRect.top + mainEl.scrollTop + c0.top - pop.offsetHeight - 8;
    const topBelow = pwRect.top - mainRect.top + mainEl.scrollTop + Math.max(c0.top + c0.height, cN.top + cN.height) + 8;
    pop.style.left = Math.max(8, Math.min(leftPx, mainEl.scrollWidth - pop.offsetWidth - 8)) + 'px';
    pop.style.top = (topAbove > mainEl.scrollTop + 4 ? topAbove : topBelow) + 'px';
  } catch (_) {}
  if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([render]).catch(() => {});
}
window.showFormulaPopover = showFormulaPopover;
// 公式浮层外部点击关闭(char-layer 点击已 stopPropagation 不冒泡到这里 → 只处理页边/UI 等外部点)
document.addEventListener('pointerdown', (e) => {
  const p = document.getElementById('fml-pop');
  if (p && !(e.target && e.target.closest && e.target.closest('#fml-pop'))) _hideFmlPop();
});

let _dragStartCharIdx = null, _dragMoved = false, _dragStartXY = null, _fromLBtn = false;
let _hlTapPending = null;   // {pw,hit,x,y}:本次按下落在查询高亮上 → 松手(未拖动)时派发该高亮动作,不查词
let _dragDir = null;   // 触摸拖动首次动够时锁定:'scroll'(竖直为主→翻页) / 'select'(水平为主→选字)
let _swipeStart = null;   // 单页模式：起点在空白处的横滑 → 翻页（起点在字上仍走拖选）
let _lastClickCharIdx = -1, _lastClickTime = 0, _clickCount = 0;

// document 级 mousemove/mouseup 只在模块顶层注册一次:原先在 _bindCharLayer 内注册且从不移除,
// 每次页面重渲/缩放重绑都泄漏 +2 个监听,且旧闭包捕获过期 cl(缩放后 rect 失真致选区错位)。
// 经 __charDrag 分发:每次重绑都覆盖为指向最新 connected cl(见 _bindCharLayer 尾),天然路由到最新绑定。
document.addEventListener('mousemove', (e) => {
  if (_dragStartCharIdx == null || !_charSel) return;   // _dragStartCharIdx 为主守卫(touchcancel 只清它)
  const d = _charSel.pw && _charSel.pw.__charDrag; if (!d) return;
  const p = d.ptToLocal(e.clientX, e.clientY);
  d.onMove(p.x, p.y, null);
});
document.addEventListener('mouseup', (e) => {
  if (_dragStartCharIdx == null || !_charSel) return;
  const d = _charSel.pw && _charSel.pw.__charDrag; if (!d) return;
  const p = d.ptToLocal(e.clientX, e.clientY);
  d.onEnd(p.x, p.y);
});
// 覆盖层高亮点击派发:onStart 几何命中后记 _hlTapPending;这里在**松手**(pointerup,触摸+鼠标通用)派发该高亮动作,
//   移动超阈值(pointermove)则取消(视作拖动,不派发也不选字)。独立于 onEnd 的跨页守卫,对两端都可靠。
document.addEventListener('pointermove', (e) => {
  if (_hlTapPending && Math.abs(e.clientX - _hlTapPending.cx) + Math.abs(e.clientY - _hlTapPending.cy) >= 10) _hlTapPending = null;
}, true);
document.addEventListener('pointerup', () => {
  if (!_hlTapPending) return;
  const hit = _hlTapPending.hit, pw = _hlTapPending.pw; _hlTapPending = null;
  try { window.__readerHlTap && window.__readerHlTap(pw, hit); } catch (_) {}
}, true);
document.addEventListener('pointercancel', () => { _hlTapPending = null; }, true);
// 安全网:任何指针松开/取消 → 全局恢复呼吸高亮 .hl 可点。onStart 拖选时给它们置了 inline pointer-events:none,
// 但 onEnd 只恢复"起点页"那份(_charSel.pw!==pw 提前 return);跨页松手/中断手势会让别页 solid 词组高亮残留 none →
// 点它穿透到 char-layer =「点了不弹」。这里兜底清掉所有页的残留 none(只在手势结束时跑,不影响进行中的拖选)。
['pointerup', 'pointercancel', 'touchend', 'touchcancel'].forEach(ev => document.addEventListener(ev, () => {
  document.querySelectorAll(_OVL_HL_SEL).forEach(el => { if (el.style.pointerEvents === 'none') el.style.pointerEvents = ''; });
}, true));
// document 级 touchstart 无条件记 _clLastTouchAt:char-layer 的「touch 后 700ms 忽略 iOS 合成 mousedown」守卫
// 原本只在 touch 落在 char-layer 自身时才更新时间戳;touch 落在**覆盖层**(词组/查词高亮 .hl 等,char-layer 的兄弟,
// 冒泡不到 char-layer)时守卫失效 → 点词组高亮后 handler 删掉 .hl,~300ms 后合成 mousedown 穿到 char-layer →
// 误查手指下的词(A/B 缓存秒弹)覆盖词组框。这里任何 touch 都记时间戳,守卫就覆盖到覆盖层上的点击。
document.addEventListener('touchstart', () => { try { window._clLastTouchAt = Date.now(); } catch (_) {} }, true);

function _bindCharLayer(cl, pw) {
  const ptToLocal = (clientX, clientY) => {
    const r = cl.getBoundingClientRect();
    // 视觉坐标 → charBoxes 布局坐标:用 BCR 与 layout 尺寸的**比值**补偿全链路缩放——
    // wrap 自身的过渡 zoom、page-container/祖先的 pinch zoom、transform scale 一并覆盖。
    // 旧实现只除 pw.style.zoom:祖先有 zoom 时(实测双页态 ≈0.84)整页点击横向偏 ~16%,
    // 页中部胖字能蒙对、右缘/页底整片点不中(点視覺上的 議 → 命中右页的 内部)。
    const kx = r.width  ? cl.clientWidth  / r.width  : 1;
    const ky = r.height ? cl.clientHeight / r.height : 1;
    return {x: (clientX - r.left) * kx, y: (clientY - r.top) * ky};
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
    // 第三段:振假名带/行间缝/行尾余白容差。ruby 画在字行**上方 ~0.5 行高**(pointer-events:none),
    // 点在假名或行缝上时 y 不落在任何 char bbox 行内 → 此前直接 MISS 被当「点空白」清选区
    // (实测 応用情報 p37「議事」上方 furigana 区即死区)。给竖直偏差 ≤0.7×行高、水平贴近的字兜底;
    // dy 权重 ×2 → 行缝处优先归属更近的那一行。
    let best3 = -1, bd3 = Infinity;
    for (let i = 0; i < pw.__charBoxes.length; i++) {
      const c = pw.__charBoxes[i];
      if (c.sp) continue;
      const h = c.height || 30;
      const dy = y < c.top ? (c.top - y) : (y > c.top + c.height ? y - c.top - c.height : 0);
      if (dy > h * 0.7) continue;
      const cx = c.left + c.width / 2;
      const dx = Math.abs(x - cx);
      if (dx > h * 1.2) continue;
      const d = dx + dy * 2;
      if (d < bd3) { bd3 = d; best3 = i; }
    }
    if (best3 >= 0) return best3;
    return -1;
  };
  const onStart = (x, y, cx, cy) => {
    if (window._ink && (_ink.mode || _ink.drawing)) return false;   // 手写模式/正在画 → 不选字(防御:各入口都兜住)
    // 根治:先几何判断本次按下是否落在查询高亮上。是 → 记 pending,松手派发该高亮动作,**不选字/不查词**。
    //   与高亮 pointer-events 状态完全无关 → 无论 .hl 是否残留 none、事件是否穿透,都不会误查手指下的字。
    if (cx != null) {
      const _hit = _overlayHlHitAtClient(pw, cx, cy);
      if (_hit && _hit.kind) { _hlTapPending = { pw, hit: _hit, cx, cy }; _dragStartCharIdx = null; return false; }
    }
    _syncCharBoxScale(pw);   // 命中前先把 charBoxes 对齐到当前显示尺寸(烘焙 scale 可能已过期)
    _hideFmlPop();           // 任何新按下先关掉旧公式浮层(若新点中公式,onEnd 会重新弹)
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
    // 同理禁用 查词/词组/解释 呼吸高亮的 .hl 点击：否则拖选经过它们会被截获 → 丢 move/up(选区乱涨成多词
    // → 误弹词组按钮)+ 误触发其 click(弹出别词结果)。松手(onEnd)/转滚动(scroll)时恢复。
    pw.querySelectorAll(_OVL_HL_SEL).forEach(el => el.style.pointerEvents = 'none');
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
        pw.querySelectorAll(_OVL_HL_SEL).forEach(el => el.style.pointerEvents = '');
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
    pw.querySelectorAll(_OVL_HL_SEL).forEach(el => el.style.pointerEvents = '');       // 恢复呼吸高亮可点
    const startIdx = _dragStartCharIdx;
    _dragStartCharIdx = null;
    if (_dragMoved) {
      const idx = _findCharAt(pw.__charBoxes, x, y);
      if (idx >= 0) _selByCharRange(pw, startIdx, idx);
      try { window.__setFocusSel && window.__setFocusSel((lastSelText || '').trim(), 'text'); } catch (_) {}   // 拖选段落 → 右侧焦点显示
      // 选中=普通选中(点别处照常消失)。持久呼吸高亮只在点「词组」按钮查询期间出现(showPhrasePopover)
    } else if (_fromLBtn) {
      // 从 L 按钮起点且没拖动 = 单击 L 按钮 → 交给 L 按钮 click 处理整句翻译，这里不查词
      _fromLBtn = false;
    } else {
      // 公式注入字符:点公式区 → 整条公式选中 + MathJax 渲染浮层(不走单/双/三击词典)
      const _h0 = pw.__charBoxes[startIdx];
      if (_h0 && _h0.fml) {
        const fb = _formulaBounds(pw.__charBoxes, startIdx);
        _selByCharRange(pw, fb.start, fb.end);
        toolbar.classList.remove('open');
        try { showFormulaPopover(pw, fb); } catch (_) {}
        try { window.__setFocusSel && window.__setFocusSel(_formulaRawLatex(pw.__charBoxes, fb), 'formula'); } catch (_) {}
        _lastClickCharIdx = -1; _clickCount = 0;
        return;
      }
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
          // 母语(不需要翻译的语言)单击选词 = 毫无意义 → 单击中文汉字词(纯汉字无假名、本书非日语)不弹任何东西、清掉选中。
          // 拖选/双击行/三击段仍照常弹(走别的分支)。
          const isNativeHan = hasKanji(_t) && !hasKana(_t) && !(declared && BOOK_LANGS.includes('ja'));
          if (_t && _t.length <= 30 && (isJa || engOk)) {
            // 同步关掉刚被 _selByCharRange 打开的工具栏:同一事件 tick 内移除 → 浏览器根本不画它。
            // 此前靠 30ms 后的 showWordPopover 去关 → 工具栏闪一帧再消失(慢词时=「弹框闪烁后消失」)。
            toolbar.classList.remove('open');
            // 点的词是不是已知生词(有下划线=以前查过、服务器有缓存)→ 跳过呼吸,直接弹占位框秒填结果(用户反馈:已查过的词不该再呼吸等待)
            let _isKnown = false;
            try {
              const _vm = (_charSel && _charSel.pw && _charSel.pw.__vocabMarks) || [];
              const _tl = _t.toLowerCase();
              _isKnown = _vm.some(m => (m.word && String(m.word).toLowerCase() === _tl) || (m.lemma && String(m.lemma).toLowerCase() === _tl));
            } catch (_) {}
            if (window.__uiShared && window.PdfAdapter) {
              PdfAdapter.lookupWord({ word: _t, context: _ctx, page: _selPageNum(), file: FILE_REL, langs: BOOK_LANGS, anchorRect: _charSel, noBreathe: _isKnown, fallback: (w, c) => showWordPopover(w, c) });
            } else {
              setTimeout(() => { try { showWordPopover(_t, _ctx); } catch(_){} }, 30);
            }
          } else if (isNativeHan) {
            toolbar.classList.remove('open');
            lastSelText = '';
            _updateSelPreview('');
            pw.querySelectorAll('.sel-overlay').forEach(ov => ov.innerHTML = '');   // 清掉单击中文词的高亮 → 单击=无操作
          }
        } else {
          // 双击选行 / 三击选段 → 右侧焦点显示这段
          try { window.__setFocusSel && window.__setFocusSel((lastSelText || '').trim(), 'text'); } catch (_) {}
        }
      }
    }
  };

  cl.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    if ((window._ink && (_ink.mode || _ink.drawing)) || (Date.now() < (window.__inkGuardUntil || 0))) return;   // 手写模式/正在画/刚写完 1s 内 → 不选字查词(palm rejection)
    if (Date.now() - (window._clLastTouchAt || 0) < 700) return;   // 忽略 touch 后 iOS 合成的 mousedown（否则 onStart 双触发→假双击→刚弹的小框被冲掉）
    e.preventDefault(); e.stopPropagation();   // 阻止旧 document.mousedown 清 toolbar
    const p = ptToLocal(e.clientX, e.clientY);
    onStart(p.x, p.y, e.clientX, e.clientY);
  });
  // document 级 mousemove/mouseup 移到模块顶层单 dispatcher(经 pw.__charDrag 分发),不再每次绑定泄漏
  cl.addEventListener('touchstart', (e) => {
    if (e.touches.length !== 1) { _dragStartCharIdx = null; _swipeStart = null; return; }
    // Apple Pencil(touchType='stylus')或手写模式/正在画 → 让墨迹层处理,**不选字**
    // (墨迹绘制在 wrap 的 pointerdown,跟这条 touchstart 是不同类事件,pointerdown 的 stopPropagation 挡不住它)
    if ((e.touches[0] && e.touches[0].touchType === 'stylus') || (window._ink && (_ink.mode || _ink.drawing)) || (Date.now() < (window.__inkGuardUntil || 0))) {   // 手写中/刚写完 1s 内:手掌触摸不选字查词(palm rejection)
      _dragStartCharIdx = null; _swipeStart = null; return;
    }
    window._clLastTouchAt = Date.now();   // 标记触摸：后续 iOS 合成 mousedown 忽略
    e.stopPropagation();   // 阻止旧 document.touchstart 清 toolbar
    const t = e.touches[0];
    const p = ptToLocal(t.clientX, t.clientY);
    onStart(p.x, p.y, t.clientX, t.clientY);
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
        pw.querySelectorAll(_OVL_HL_SEL).forEach(el => el.style.pointerEvents = '');
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
  cl.addEventListener('touchcancel', () => { _dragStartCharIdx = null; _swipeStart = null;
    const vl = pw.querySelector('.vocab-layer'); if (vl) vl.style.pointerEvents = '';
    pw.querySelectorAll(_OVL_HL_SEL).forEach(el => el.style.pointerEvents = ''); });
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
  text = (text || '').trim();
  // 出向选区/焦点同步(2026-07-28 修):PDF 的真实选中走 char-layer(画在 sel-overlay,
  // **不产生原生 selection**),而 `checkSelection` 只挂在 mouseup/touchend/selectionchange 上
  // → char-layer 选中完成后没有任何事件通知出向漏斗,结果 UI 有选中、journal 无 focus,
  //   甚至因 `_charSel` 被滚动/翻页判定清空而误发 cancel(用户实测:p23 选中后 selection='')。
  // 这里是**选中变更的唯一全覆盖通知点**(提交与清空两条路径都经过它),所以挂在此处一处即可,
  // 不新建第二套选择机制。_ctxSelReport 内部已做:空串=显式取消、非空=focus('text')。
  try { if (typeof _ctxSelReport === 'function') _ctxSelReport(text); } catch (_) {}
  // 选中元数据(所在页 + 时戳),给语音/侧栏助手 __voiceContext 做「跨页陈旧选中」校验:
  // 翻到别页后旧选中不再当成"现在在问的内容"。每次选中变化都先清空所在句(char-layer 路径随后会补)。
  try {
    window.__lastSelSentence = '';
    window.__lastSelMeta = text
      ? { page: (typeof currentPage !== 'undefined' ? currentPage : 0), t: Date.now() }
      : null;
  } catch (_) {}
  if (!el) return;
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


window._updateToolbarMode = _updateToolbarMode;   // 实况网页(web-adapter)复用同一套按钮组开关(审计 #12)
// ──────── F6 词组：收藏（作分词依据）+ 词组详情面板 ────────
let _phraseFavSet = new Set();
let _phraseMarkSet = new Set();   // 已掌握词组(归一化键):标掌握后不再画生词下划线
const _phraseNorm = s => String(s || '').trim().replace(/\s+/g, ' ').toLowerCase();
function _applyVocabularyPhraseProjection(records) {
  for (const record of (Array.isArray(records) ? records : [])) {
    if (!record || record.kind !== 'phrase') continue;
    if (record.property === 'favorite') {
      // PDF 字符盒匹配历史上忽略排版空白；仓库仍保留规范词组，几何投影只转成自身需要的 key。
      const key = String(record.key || '').replace(/[\s\u3000]+/g, '');
      if (!key) continue;
      if (record.enabled) _phraseFavSet.add(key); else _phraseFavSet.delete(key);
    } else if (record.property === 'mastered') {
      const key = _phraseNorm(record.key);
      if (!key) continue;
      if (record.enabled) _phraseMarkSet.add(key); else _phraseMarkSet.delete(key);
    }
  }
  try { _applyPhraseMergesAll(); } catch (_) {}
  try {
    document.querySelectorAll('[data-loaded="1"][data-page-num]').forEach((pw) => {
      try { renderVocabUnderlines(pw, pw.__vocabMarks || []); } catch (_) {}
    });
  } catch (_) {}
}
try { window.__syncPhraseStateProjection = _applyVocabularyPhraseProjection; } catch (_) {}
async function _loadPhraseFavs() {
  // 共享模式由 rc-phrasepop 负责服务器兼容导入；PDF 字符层只消费统一仓库投影，
  // 避免同一页面启动时再重复拉两份相同数据。
  try {
    const repo = window.__uiShared && window.BWReaderRuntime && window.BWReaderRuntime.vocabularyState;
    if (repo && repo.CONTRACT === 'vocabulary-state/1' && typeof repo.snapshot === 'function') {
      _applyVocabularyPhraseProjection(repo.snapshot());
      return;
    }
  } catch (_) {}
  try { const d = await (await fetch('/pdf/api/phrases')).json(); if (d.ok) _phraseFavSet = new Set(d.phrases || []); } catch (_) {}
  try { const d = await (await fetch('/pdf/api/phrase-mark')).json(); if (d.ok) _phraseMarkSet = new Set(d.mastered || []); } catch (_) {}
}
window.onPhrase = () => {
  const controller = window.__bwSelectionController;
  const selected = (controller && controller.current && controller.current()) || {
    text: lastSelText || '', rect: null
  };
  const t = String(selected && selected.text || '').trim();
  if (!t) return;
  // 词组查询前清掉残留的查词呼吸/常亮高亮:它们(.rc-wp-breathe z:190)会盖住词组高亮(z:6)截获点击 →
  //   「点词组高亮没反应」(尤其"某词先查过、其查词高亮被打断残留"时)。切到词组模式旧查词高亮已陈旧。
  try { if (window.RC && RC.wordpop && RC.wordpop.clearHls) RC.wordpop.clearHls(); } catch (_) {}
  showPhrasePopover(t, { rect: selected && selected.rect || null });
};
// 词组查询期间的呼吸高亮：**只在点「词组」按钮、查询进行中**出现（showPhrasePopover 开始时建、
// 结果出来即移除）。状态驱动(存 pt 坐标到 _activePhraseHl)→ 查询那 1-2s 内即便发生重渲染也不丢、
// 持续呼吸；不受点别处影响(独立层)。平时选中=普通选中(点别处照常消失)，这里不参与。
let _phraseHlTimer = null;
let _phraseHls = [];          // 多个词组高亮并存 [{id,page,text,rects:[[x0,y0,x1,y1]pt...],solid}](原单例 _activePhraseHl → 数组)
let _phraseHlSeq = 0;
function _charRangeToPtRects(chars, s, e) {
  if (s > e) { const t = s; s = e; e = t; }
  // 块过滤:跟选中预览(_selByCharRange)/句子构造(_buildSentenceFromSel)严格一致——排序后选区首尾之间
  // 会交错进别气泡/别栏的字,不过滤就把别行的字也框进来(表现:选第2行却高亮第1、3行)。只取起止块区间内的字。
  const _blk = (c) => (c.bk != null && c.bk >= 0) ? c.bk : ((c.w == null || c.w < 0) ? -1 : Math.floor(c.w / 1000000));
  const sb = _blk(chars[s]), eb = _blk(chars[e]);
  const bLo = Math.min(sb, eb), bHi = Math.max(sb, eb);
  const inBlk = (c) => { if (sb < 0 || eb < 0) return true; const b = _blk(c); return b < 0 || (b >= bLo && b <= bHi); };
  const rects = []; let cur = null;
  for (let i = s; i <= e && i < chars.length; i++) {
    const c = chars[i];
    if (c.sp || c._x0 == null) continue;
    if (!inBlk(c)) continue;   // 别块字符不框,跟预览一致
    const lh = (c._y1 - c._y0) || 1;
    if (cur && Math.abs(c._y0 - cur[1]) <= lh * 0.6) {
      cur[2] = Math.max(cur[2], c._x1); cur[1] = Math.min(cur[1], c._y0); cur[3] = Math.max(cur[3], c._y1);
    } else { if (cur) rects.push(cur); cur = [c._x0, c._y0, c._x1, c._y1]; }
  }
  if (cur) rects.push(cur);
  return rects;
}
function renderPhraseHl(pw) {
  pw.querySelectorAll('.phrase-hl-layer').forEach(l => l.remove());   // 清本页所有旧层,按 state 重建(多高亮)
  const pn = parseInt(pw.dataset.pageNum || '0', 10);
  const mine = _phraseHls.filter(a => a.page === pn && a.rects && a.rects.length);
  if (!mine.length) return;
  const canvas = pw.querySelector('canvas');
  const cssW = canvas?.clientWidth || pw.clientWidth, cssH = canvas?.clientHeight || pw.clientHeight;
  const pageWPt = pw.__pageWPt || cssW, pageHPt = pw.__pageHPt || cssH;
  if (!cssW || !cssH || !pageWPt || !pageHPt) return;
  const sx = cssW / pageWPt, sy = cssH / pageHPt;
  for (const a of mine) {
    const layer = document.createElement('div');
    layer.className = 'phrase-hl-layer' + (a.solid ? '' : ' breathe');   // 查询中呼吸；出结果转常亮(a.solid)保持
    layer.dataset.phid = a.id;   // 用 id 定位本高亮的层(删除/转常亮只动自己那份)
    for (const r of a.rects) {
      const d = document.createElement('div'); d.className = 'hl';
      d.style.left = (r[0] * sx) + 'px'; d.style.top = (r[1] * sy) + 'px';
      d.style.width = ((r[2] - r[0]) * sx) + 'px'; d.style.height = ((r[3] - r[1]) * sy) + 'px';
      d.style.pointerEvents = 'auto';   // 显式抵消拖选残留的 inline pointer-events:none(否则 solid 高亮"点了不弹")
      layer.appendChild(d);
    }
    // 点高亮 → 弹词组框(.hl 命中时走这里;若因 pointer-events 残留穿透到 char-layer,由 char-layer 几何命中 → __readerHlTap 派发,同一函数)
    layer.addEventListener('click', (e) => { e.stopPropagation(); _openPhraseFromHl(a, layer); });
    pw.appendChild(layer);
  }
}
// 打开某词组高亮的结果框:锚点用高亮自身屏幕矩形(先算再删高亮,防原选区已清导致定位屏外),已存结果秒开。
function _openPhraseFromHl(a, layer) {
  if (!a) return;
  const scope = layer || document.querySelector('.phrase-hl-layer[data-phid="' + a.id + '"]');
  let anchorRect = null;
  try {
    let L = Infinity, T = Infinity, R = -Infinity, B = -Infinity;
    if (scope) scope.querySelectorAll('.hl').forEach(h => { const r = h.getBoundingClientRect(); if (r.width || r.height) { L = Math.min(L, r.left); T = Math.min(T, r.top); R = Math.max(R, r.right); B = Math.max(B, r.bottom); } });
    if (L < Infinity) anchorRect = { left: L, top: T, right: R, bottom: B, width: R - L, height: B - T };
  } catch (_) {}
  const res = a.result || null;   // 查询期已把结果存到高亮上 → 点击秒开,不重新 fetch
  _removePhraseHighlight(a);
  showPhrasePopover(a.text, { noHighlight: true, rect: anchorRect, result: res });
}
// char-layer 几何命中覆盖层高亮后派发(根治:点击归属由 char-layer 统一裁决,不靠 pointer-events 抢占)。
// hit={kind:'phrase'|'word'|'explain', layer, el}。phrase 完整派发;word/explain 暂只"不查词"(避免弹错词框),后续接各自重开。
window.__readerHlTap = function (pw, hit) {
  try {
    if (!hit || !hit.kind) return;
    if (hit.kind === 'phrase') {
      const phid = hit.layer && hit.layer.dataset ? hit.layer.dataset.phid : null;
      const a = _phraseHls.find(h => String(h.id) === String(phid));
      if (a) _openPhraseFromHl(a, hit.layer);
    }
    // word/explain:命中即"让路"(char-layer 不再误查手指下的字);其重开由各自 .hl 的原生 handler 兜(正常态 pointer-events:auto 时)
  } catch (_) {}
};
// arg=高亮对象→只删它;arg=字符串→删所有同文本(收藏该词组后);arg 空→全清(换书/大跳转)
function _removePhraseHighlight(arg) {
  if (arg && typeof arg === 'object') {
    _phraseHls = _phraseHls.filter(a => a !== arg);
    document.querySelectorAll('.phrase-hl-layer[data-phid="' + arg.id + '"]').forEach(l => l.remove());
    return;
  }
  if (typeof arg === 'string' && arg) {
    const gone = _phraseHls.filter(a => a.text === arg);
    _phraseHls = _phraseHls.filter(a => a.text !== arg);
    gone.forEach(a => document.querySelectorAll('.phrase-hl-layer[data-phid="' + a.id + '"]').forEach(l => l.remove()));
    return;
  }
  _phraseHls = [];
  clearTimeout(_phraseHlTimer);
  document.querySelectorAll('.phrase-hl-layer').forEach(l => l.remove());
}
function _showPhraseHighlight(pw) {
  if (!pw || !_charSel || !pw.__charBoxes) return null;
  const text = (lastSelText || '').trim();
  if (!text) return null;
  const rects = _charRangeToPtRects(pw.__charBoxes, _charSel.startIdx, _charSel.endIdx);
  if (!rects.length) return null;
  const hl = {id: ++_phraseHlSeq, page: parseInt(pw.dataset.pageNum || '0', 10) || currentPage, text, rects, solid: false};
  _phraseHls.push(hl);   // 追加,不清旧的 → 多个词组高亮并存
  const sel = pw.querySelector('.sel-overlay'); if (sel) sel.innerHTML = '';   // 移交持久层，避免双重高亮
  renderPhraseHl(pw);
  return hl;   // 返回本高亮,供 phraseSolid 精确标常亮(并发多查询各标各的)
}
// ──────── 解释高亮（与词组高亮平行：独立琥珀色，可与词组高亮共存）────────
// 设计:点「解释」**不开面板**,只在选区建一个**一直闪烁**的高亮,AI 在后台跑;
// 点高亮 → 打开解释页面(就绪=显缓存,未就绪=显加载中由后台填充) + **一次点击即移除高亮**。
// 单 active:再开新解释把旧 job 标 canceled 并替换高亮。
let _activeExplainHl = null;   // {page,text,rects,html,ready,canceled,panelReqId,title,src,resultContext}
function renderExplainHl(pw) {
  pw.querySelector('.explain-hl-layer')?.remove();
  const a = _activeExplainHl;
  if (!a || !a.rects || !a.rects.length) return;
  if (parseInt(pw.dataset.pageNum || '0', 10) !== a.page) return;
  const canvas = pw.querySelector('canvas');
  const cssW = canvas?.clientWidth || pw.clientWidth, cssH = canvas?.clientHeight || pw.clientHeight;
  const pageWPt = pw.__pageWPt || cssW, pageHPt = pw.__pageHPt || cssH;
  if (!cssW || !cssH || !pageWPt || !pageHPt) return;
  const sx = cssW / pageWPt, sy = cssH / pageHPt;
  const layer = document.createElement('div');
  layer.className = 'explain-hl-layer breathe';   // 一直闪烁,提示"点我看解释"
  for (const r of a.rects) {
    const d = document.createElement('div'); d.className = 'hl';
    d.style.left = (r[0] * sx) + 'px'; d.style.top = (r[1] * sy) + 'px';
    d.style.width = ((r[2] - r[0]) * sx) + 'px'; d.style.height = ((r[3] - r[1]) * sy) + 'px';
    layer.appendChild(d);
  }
  layer.addEventListener('click', (e) => { e.stopPropagation(); _reopenExplain(); });
  pw.appendChild(layer);
}
function _removeExplainHighlight() {
  _activeExplainHl = null;
  document.querySelectorAll('.explain-hl-layer').forEach(l => l.remove());
}
function _showExplainHighlight(pw, text) {
  if (!pw || !_charSel || !pw.__charBoxes) return null;
  const t = (text || lastSelText || '').trim();
  if (!t) return null;
  const rects = _charRangeToPtRects(pw.__charBoxes, _charSel.startIdx, _charSel.endIdx);
  if (!rects.length) return null;
  if (_activeExplainHl) _activeExplainHl.canceled = true;   // 旧 job 作废(结果丢弃,不再填面板)
  document.querySelectorAll('.explain-hl-layer').forEach(l => l.remove());
  _activeExplainHl = {
    page: parseInt(pw.dataset.pageNum || '0', 10) || currentPage,
    text: t, rects, html: null, ready: false, canceled: false, panelReqId: null,
    title: '💡 AI 解释', src: t, resultContext: null,
  };
  const sel = pw.querySelector('.sel-overlay'); if (sel) sel.innerHTML = '';   // 移交持久层,避免双重高亮
  renderExplainHl(pw);
  return _activeExplainHl;
}
function _reopenExplain() {
  const a = _activeExplainHl;
  if (!a) return;
  // 共享模式(__uiShared):结果框改走 rc-result,须用它暴露的 openResult/addResultPickers/_resultReqId
  // (window.openResult 已是 rc-result 版本;window.addResultPickers 需 rc-result 显式导出),否则本文件
  // 模块作用域内的裸 openResult/addResultPickers/_resultReqId 是 reader.js 自己另一份计数器/实现,
  // 两边互不作废对方的在途请求,会有极小概率的"旧结果覆盖新结果"竞态。非共享模式逐字不变。
  const shared = window.__uiShared && window.PdfAdapter;
  const _open = shared ? window.openResult : openResult;
  const _pickers = shared ? window.addResultPickers : addResultPickers;
  if (a.html) {
    // 已就绪 → 直接显缓存
    _open(a.title || '💡 AI 解释', a.src || a.text, a.html);
    try { if (a.resultContext) _resultContext = a.resultContext; } catch (_) {}
    try { _pickers && _pickers(); } catch (_) {}
  } else {
    // 还在后台跑 → 开加载面板,登记 reqId,完成时由 _runExplainBg 填充(并补 pickers)
    _open(a.title || '💡 AI 解释', a.src || a.text, '<div class="loading">⏳ AI 处理中…</div>');
    a.panelReqId = shared ? window._resultReqId : _resultReqId;   // openResult 已自增对应计数器,这里取新值
  }
  // 点高亮=打开页面 → 一次点击即移除高亮(后台 job 仍持自身引用,未 canceled 时继续填面板)
  document.querySelectorAll('.explain-hl-layer').forEach(l => l.remove());
  _activeExplainHl = null;
}
window.showPhrasePopover = async (text, opts) => {
  const adapter = (window.RC && RC.adapter) ? RC.adapter() : null;
  if (window.__uiShared && adapter && typeof adapter.lookupPhrase === 'function') {   // 共享模式按当前宿主分流；PDF 字符层路径不变
    toolbar.classList.remove('open');
    adapter.lookupPhrase({
      text,
      noHighlight: opts && opts.noHighlight,
      anchorRect: opts && opts.rect,
      result: opts && opts.result,
      fallback: () => _showPhrasePopoverNative(text, opts)
    });
    return;
  }
  return _showPhrasePopoverNative(text, opts);
};
async function _showPhrasePopoverNative(text, opts) {
  const pop = document.getElementById('word-pop');
  toolbar.classList.remove('open');
  _wordPopState = {word: text, ctx: '', lemma: text, phrase: true, reading: '', jp: false,
                   mastered: _phraseMarkSet.has(_phraseNorm(text))};
  pop.style.display = 'block';
  window._wordPopOpenAt = Date.now();
  pop.innerHTML = '<div style="padding:14px;color:#8a9bb4">⏳ 处理词组…</div>';
  _positionWordPop(pop);
  // **点了词组按钮**才把当前选区变持久呼吸高亮（查询中呼吸→出结果常亮保持，点高亮才消失）。
  // 点高亮重新弹框时 opts.noHighlight=true → 只弹框、不再建新高亮。
  let _nativePhl = null;
  if (_wordPopState.phrase && !(opts && opts.noHighlight)) _nativePhl = _showPhraseHighlight(_charSel && _charSel.pw);
  const esc = s => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
  const isJa = _isJaWord(text);
  _wordPopState.jp = isJa;   // 掌握按钮按语言分流 store(jp-vocab-mark / vocab-mark)
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
  if (_nativePhl) {
    _nativePhl.solid = true;
    const _l = document.querySelector('.phrase-hl-layer[data-phid="' + _nativePhl.id + '"]');
    if (_l) { _l.classList.remove('breathe'); _l.querySelectorAll('.hl').forEach(el => el.style.pointerEvents = 'auto'); }
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
    '<button id="wp-master-btn" class="' + (_wordPopState.mastered ? 'wp-anki' : '') + '" onclick="_wordPopMaster(this)" title="' + (_wordPopState.mastered ? '点击取消掌握（恢复生词下划线）' : '标记掌握 100（该词组不再标生词下划线）') + '">' +
    (_wordPopState.mastered ? '✓ 已掌握 100' : '☆ 标记掌握') + '</button>' +
    '<button onclick="onExplain()" title="详细解释这个词组">💡 解释</button>' +
    '</div>';
}
// ── 收藏词组乐观长下划线(2026-07-21 用户实锤"生成太慢"):真分词=整本 chars 服务端重算(慢),
//    这里用手头 charBoxes 客户端搜词组出现位置**即时**画线(几 ms);真渲染到位后 vocab-layer
//    整层重画,临时件自然被清,无缝接管。取消收藏即时摘除同款临时件。──
function _pfavPaint(t) {
  const needle = String(t || '').replace(/[\s\u3000]/g, ''); if (!needle) return 0;   // #55:去空白与页面无空白 str 对齐
  let n = 0;
  document.querySelectorAll('.page-wrap[data-page-num]').forEach((pw) => {
    const chars = pw.__charBoxes; if (!chars || !chars.length) return;
    let str = ''; const pos = [];
    for (let i = 0; i < chars.length; i++) {
      if (chars[i].sp) continue;   // #55:跳空格 char → str 无空白,与归一化词组对齐
      const cc = chars[i].c != null ? String(chars[i].c) : '';
      for (let j = 0; j < cc.length; j++) { str += cc[j]; pos.push(i); }
    }
    let from = 0, idx;
    const layer = ensurePageLayer(pw, 'vocab-layer'); if (!layer) return;
    while ((idx = str.indexOf(needle, from)) >= 0) {
      from = idx + needle.length;
      const sIdx = pos[idx], eIdx = pos[idx + needle.length - 1];
      if (sIdx == null || eIdx == null) continue;
      const rects = _charsRangeToRects(chars, sIdx, eIdx) || [];
      for (const r of rects) {
        const d = document.createElement('div');
        d.className = 'vocab-underline m-new';
        d.dataset.pfav = encodeURIComponent(needle);
        d.style.left = r.x0 + 'px'; d.style.top = (r.y1 + 1) + 'px'; d.style.width = (r.x1 - r.x0) + 'px';
        layer.appendChild(d); n++;
      }
    }
  });
  return n;
}
function _pfavUnpaint(t) {
  const k = encodeURIComponent(String(t || ''));
  document.querySelectorAll('.vocab-underline[data-pfav="' + k + '"]').forEach((el) => el.remove());
}
try { window.__pfavPaint = _pfavPaint; window.__pfavUnpaint = _pfavUnpaint; } catch (_) {}

// ── 本地真分词(2026-07-21 用户教义:服务器只做必要任务/备份/中继——词组合并的输入(收藏集+字符盒)
//    全在本地,分组计算就该在本地):对页内每个收藏词组出现位置,把这些字的 w 并成同组(借占位字既有
//    w,保 block 编码);_w0 存原生值 → 幂等重套(取消收藏=还原后按剩余收藏集重算)。
//    服务端不再为收藏触发整本 chars 重算;它的分词结果下次自然加载时到达,本函数重套幂等无冲突。──
function _applyPhraseMergesLocal(pw) {
  const chars = pw && pw.__charBoxes; if (!chars || !chars.length) return;
  for (const cb of chars) if (cb._w0 !== undefined) cb.w = cb._w0;   // 先整体还原
  const favs = (typeof _phraseFavSet !== 'undefined' && _phraseFavSet) ? [..._phraseFavSet] : [];
  if (!favs.length) return;
  let str = ''; const pos = [];
  for (let i = 0; i < chars.length; i++) {
    if (chars[i].sp) continue;   // #55:跳空格 char → str 无空白,与归一化词组对齐
    const cc = chars[i].c != null ? String(chars[i].c) : '';
    for (let j = 0; j < cc.length; j++) { str += cc[j]; pos.push(i); }
  }
  for (const t0 of favs) {
    const t = String(t0 || '').replace(/[\s\u3000]/g, '');   // #55:去空白与页面无空白 str 对齐(跨行/夹空格词组本地也合并)
    if (!t) continue;
    let from = 0, idx;
    while ((idx = str.indexOf(t, from)) >= 0) {
      from = idx + t.length;
      const sIdx = pos[idx], eIdx = pos[idx + t.length - 1];
      if (sIdx == null || eIdx == null) continue;
      let wUse = -1;
      for (let k = sIdx; k <= eIdx; k++) if (chars[k].w != null && chars[k].w >= 0) { wUse = chars[k].w; break; }
      if (wUse < 0) continue;   // 无既有词 id 可借(w 编码含块 id)→ 保守跳过
      for (let k = sIdx; k <= eIdx; k++) { const cb = chars[k]; if (cb._w0 === undefined) cb._w0 = cb.w; cb.w = wUse; }
    }
  }
}
function _applyPhraseMergesAll() { document.querySelectorAll('.page-wrap[data-page-num]').forEach((pw) => { try { _applyPhraseMergesLocal(pw); } catch (_) {} }); }
try { window.__applyPhraseMergesLocal = _applyPhraseMergesLocal; } catch (_) {}

window._phraseFav = (btn) => {
  const s = _wordPopState; if (!s || !s.word) return;
  const t = String(s.word).replace(/[\s\u3000]+/g, '');   // #55:收藏归一化去空白(跨行选中带 \n)→存干净词组;本地匹配也归一化,已存脏词组一并救回
  const has = _phraseFavSet.has(t);
  const nowFav = !has;
  // ① local-first(2026-07-20 用户实锤"点收藏要等 Pi"):本地先翻集合+画面,零等待
  if (nowFav) _phraseFavSet.add(t); else _phraseFavSet.delete(t);
  if (btn) { btn.disabled = false; btn.textContent = nowFav ? '★ 已收藏' : '☆ 收藏为词组'; btn.classList.toggle('wp-anki', nowFav); }
  _toast?.(nowFav ? '已收藏，之后会作为一个词分词' : '已取消收藏');
  if (nowFav) _removePhraseHighlight(t);   // 收藏后该词组变成划线(分词单元),只消除同文本的查询高亮(不动别的并存高亮)
  _applyPhraseMergesAll();   // 本地真分词(教义:服务器只做备份/中继;整本重算已撤,服务端结果下次自然加载幂等重套)
  if (nowFav) { try { _pfavPaint(t); } catch (_) {} } else { try { _pfavUnpaint(t); } catch (_) {} }   // 长下划线即时(先画后传)
  // ② 后台同步:成功用服务端全量校正;网络错 → outbox 入队(POST/DELETE 均幂等)
  fetch('/pdf/api/phrases', {
    method: has ? 'DELETE' : 'POST',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify({text: t}),
  }).then(r => r.json()).then(d => {
    if (d && d.ok) _phraseFavSet = new Set(d.phrases || []);
  }).catch((e) => {
    if (window.RC && RC.outbox && e && e.name === 'TypeError')
      RC.outbox.send('phrasefav', t, '/pdf/api/phrases', { text: t }, has ? 'DELETE' : 'POST');
  });
};
// 收藏/取消后重拉各已渲染页 chars 的 w（原地更新，不重建 char-layer）→ 单击立即按新词组边界选
// generation 守卫:快速收藏→立即取消两次调用交错时,旧轮迟到的响应不准写回(只让最新一轮落地);
// 距视口中心近的页先刷 + 并发 3 小池(页内 overlay→chars 的 cv 依赖仍串行;无界并发会打满服务端 gthread)。
let _charsWGen = 0;
async function _refreshOnePageCharsW(pw, num, gen) {
  // 先 overlay 拿新 cv(收藏词组改了→cv 变,避开 SW 缓存里旧 w)+ 新 vocab_marks
  let ov = null;
  try { ov = await (await fetch('/pdf/api/page-overlay?file=' + encodeURIComponent(FILE_REL) + '&page=' + num)).json(); } catch (_) {}
  if (gen !== _charsWGen) return;   // 已有更新一轮 → 本轮结果作废(连 localStorage cv 也不写,防旧值覆盖新)
  const cv = (ov && ov.ok && ov.cv) ? ov.cv : CHARS_VER;
  if (ov && ov.ok && ov.cv) { try { localStorage.setItem('pdf-cv:' + FILE_REL + ':' + num, ov.cv); } catch (_) {} }  // 词组变后更新各页 cv
  const d = await (await fetch('/pdf/api/page-chars?file=' + encodeURIComponent(FILE_REL) + '&page=' + num + '&v=' + CHARS_VER + '&cv=' + encodeURIComponent(cv))).json();
  if (gen !== _charsWGen) return;
  if (!d.ok || !d.chars) return;
  const newW = d.chars.map(c => (c.w == null ? -1 : c.w));
  for (const cb of pw.__charBoxes) { if (cb._oi != null && cb._oi < newW.length) { cb.w = newW[cb._oi]; delete cb._w0; } }
  try { _applyPhraseMergesLocal(pw); } catch (_) {}   // 服务端 w 覆盖后重套本地收藏合并(幂等)
  if (d.furigana) {   // 收藏词组后:振假名已按整体读音合并(当試験→とうしけん 一条),刷新并重画 ruby
    pw.__furigana = d.furigana;
    try { if (_rubyEnabled()) renderRubyLayer(pw); } catch (_) {}
  }
  pw.__vocabMarks = (ov && ov.vocab_marks) || [];   // overlay 失败 → 清空(跟初加载降级一致,不留陈旧下划线)
  try { renderVocabUnderlines(pw, pw.__vocabMarks); } catch (_) {}
}
async function refreshCharsWForAllPages() {
  const gen = ++_charsWGen;   // 入口 bump:作废所有在途旧轮
  const wraps = [...document.querySelectorAll('[data-loaded="1"][data-page-num]')];
  // 距视口中心近的页优先(用户正看的页分词最先生效)
  const cy = (window.innerHeight || 0) / 2;
  const dist = (pw) => { const r = pw.getBoundingClientRect(); return Math.abs((r.top + r.bottom) / 2 - cy); };
  wraps.sort((a, b) => dist(a) - dist(b));
  let next = 0;
  const worker = async () => {
    while (next < wraps.length && gen === _charsWGen) {
      const pw = wraps[next++];
      const num = parseInt(pw.dataset.pageNum || '0', 10);
      if (!num || !pw.__charBoxes) continue;
      try { await _refreshOnePageCharsW(pw, num, gen); } catch (_) {}
    }
  };
  await Promise.all([worker(), worker(), worker()]);
}

// ── 单词小框：单击单词/点查词 → 先 ecdict 核心(秒回)，可展开完整大框，可主动制卡 ──
let _wordPopState = null;
let _wordPopSeq = 0;   // 查词请求序号:防竞态(快速点不同词,旧词响应晚到覆盖新词)
function _positionWordPop(pop, cs) {
  // 挂进 #main（不会被单页重渲染的 innerHTML='' 删掉）；用 page-wrap 布局坐标定位，
  // absolute 相对 #main → 随内容滚动。（之前挂 page-wrap，侧栏展开触发重渲染会把小框删掉 → 闪没）
  // cs：可传入查词时捕获的 charSel（慢词点高亮后定位用，防此时 _charSel 已变）。默认用当前 _charSel。
  cs = cs || _charSel;
  const pw = cs && cs.pw;
  const ch = pw && pw.__charBoxes && pw.__charBoxes[cs.startIdx];
  const main = document.getElementById('main');
  if (pw && ch && ch.left != null && main) {
    if (pop.parentElement !== main) main.appendChild(pop);
    pop.style.position = 'absolute';
    const left = pw.offsetLeft + ch.left;   // pw.offsetParent === #main（page-container static）
    pop.style.left = Math.max(4, left) + 'px';
    pop.style.top = (pw.offsetTop + ch.top + ch.height + 6) + 'px';
    // 渲染后按「可视视口」夹取(不是 main.scrollWidth)：#main 可横向滚动 / 缩放，
    // 固定宽 + scrollWidth 夹会把框推到视口外被裁(日语词框右侧 語法/例句/标记掌握 被切)。
    // pop 是 absolute-in-#main、无额外 CSS 缩放 → 视口空间位移量 == 内容坐标位移量。
    requestAnimationFrame(() => {
      if (pop.style.display === 'none') return;
      const pr = pop.getBoundingClientRect();
      if (!pr.width) return;
      const mainRect = main.getBoundingClientRect();
      const vr = (typeof _visRight === 'function') ? _visRight() : window.innerWidth;
      let dL = parseFloat(pop.style.left) || 0;
      // 左右互斥(框不会同时两边溢出，除非比可视区还宽 → 那时优先保右缘[动作按钮]在视野)
      if (pr.right > vr - 8) dL -= (pr.right - (vr - 8));                        // 右溢出 → 左移
      else if (pr.left < mainRect.left + 8) dL += (mainRect.left + 8 - pr.left); // 左溢出 → 右移
      pop.style.left = Math.max(4, dL) + 'px';
      // 竖直：max-height(CSS 80vh)已封顶高度 → 在视口空间把框夹进 [8, innerH-8]
      // (pop 是 absolute-in-#main、无 CSS 缩放 → 视口位移量 == top 内容坐标位移量)
      const pr2 = pop.getBoundingClientRect();
      let dT = 0;
      if (pr2.bottom > window.innerHeight - 8) dT -= (pr2.bottom - (window.innerHeight - 8)); // 底溢 → 上移
      if (pr2.top + dT < 8) dT += (8 - (pr2.top + dT));                                       // 顶溢 → 下移
      if (dT) pop.style.top = ((parseFloat(pop.style.top) || 0) + dT) + 'px';
    });
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
// ── 单击查词的"等待"表现（照「解释」那套，多个可并存）──────────────────────────
// 快词(≤300ms 回，英语 ecdict / 已缓存日语)直接弹小框；慢词(日语 AI 等)不弹挡视线的"查词中"框，
// 而是给词建呼吸高亮当等待指示，就绪转常亮，**点高亮才出结果 + 高亮消失**。
// 多个查词可同时进行 → 多个呼吸高亮并存（`_wordHls` 数组），各自独立查、各自点开。
let _wordHlSeq = 0;          // 高亮 id 发号
let _wordHls = [];           // 并存的查词高亮 [{id,page,pw,rects,word,ctx,charSel,shown,ready,data,error,boxOpen}]
let _wordPopOwnerId = null;  // 当前 word-pop 小框归属的高亮 id（防并发查词回来填错框）
// 本会话查词结果缓存（word→dict-quick d）：已查过的词再点**直接秒显小框**(不发请求/不建高亮),
// 后台再打一次刷新暴露计数。用户诉求:"已有现成数据的词单击应直接出结果,不要先高亮再点"。
const _dictCache = new Map();
// 按词的 rects 从本页 furigana(日读音/英音标,page-chars 一直返回)取命中条目 → 查词等待时先标出来
function _furiHitsForRects(pw, rects) {
  const fg = pw && pw.__furigana;
  if (!fg || !fg.length || !rects || !rects.length) return [];
  let X0 = Infinity, Y0 = Infinity, X1 = -Infinity, Y1 = -Infinity;
  for (const r of rects) { X0 = Math.min(X0, r[0]); Y0 = Math.min(Y0, r[1]); X1 = Math.max(X1, r[2]); Y1 = Math.max(Y1, r[3]); }
  const hit = fg.filter(it => {
    if (!it.rt) return false;
    const cx = (it.x0 + it.x1) / 2, cy = (it.y0 + it.y1) / 2;
    return cx >= X0 - 1 && cx <= X1 + 1 && cy >= Y0 - 2 && cy <= Y1 + 2;
  });
  hit.sort((a, b) => (Math.abs(a.y0 - b.y0) > 4 ? a.y0 - b.y0 : a.x0 - b.x0));
  return hit;
}
function renderWordHl(pw) {   // 渲染该页所有查词高亮（多个并存；boxOpen 的不画）
  pw.querySelectorAll('.word-hl-layer').forEach(l => l.remove());
  const page = parseInt(pw.dataset.pageNum || '0', 10);
  const mine = _wordHls.filter(h => h.page === page && h.rects && h.rects.length && !h.boxOpen);
  if (!mine.length) return;
  const canvas = pw.querySelector('canvas');
  const cssW = canvas?.clientWidth || pw.clientWidth, cssH = canvas?.clientHeight || pw.clientHeight;
  const pageWPt = pw.__pageWPt || cssW, pageHPt = pw.__pageHPt || cssH;
  if (!cssW || !cssH || !pageWPt || !pageHPt) return;
  const sx = cssW / pageWPt, sy = cssH / pageHPt;
  for (const h of mine) {
    const layer = document.createElement('div');
    layer.className = 'word-hl-layer' + (h.ready ? '' : ' breathe');   // 查词中呼吸；就绪转常亮(点我看词义)
    for (const r of h.rects) {
      const d = document.createElement('div'); d.className = 'hl';
      d.style.left = (r[0] * sx) + 'px'; d.style.top = (r[1] * sy) + 'px';
      d.style.width = ((r[2] - r[0]) * sx) + 'px'; d.style.height = ((r[3] - r[1]) * sy) + 'px';
      layer.appendChild(d);
    }
    // 查词等待时:先把这处注音(日读音/英音标,本页 furigana)标在词上方——复用原振假名 .ruby-layer .rt 样式(大小/位置一致)
    const _hits = _furiHitsForRects(pw, h.rects);
    if (_hits.length) {
      const rl = document.createElement('div');
      rl.className = 'ruby-layer';   // pointer-events:none → 不挡高亮点击;.rt 样式靠这个作用域
      for (const it of _hits) { const sp = _makeRubySpan(it, sx, sy); if (sp) rl.appendChild(sp); }
      layer.appendChild(rl);
    }
    layer.addEventListener('click', (e) => { e.stopPropagation(); _wordHlClick(h); });
    pw.appendChild(layer);
  }
}
function _renderWordHlsFor(pw) { if (pw) { try { renderWordHl(pw); } catch (_) {} } }
function _removeWordHl(hl) {   // 移除单个高亮(点开/出错/查完后)
  _wordHls = _wordHls.filter(o => o !== hl);
  _renderWordHlsFor(hl.pw);
}
function _removeWordHighlight() {   // 清掉全部查词高亮(换书/大跳转时备用)
  _wordHls = [];
  document.querySelectorAll('.word-hl-layer').forEach(l => l.remove());
}
// 把一个慢词查词 hl 物化成呼吸高亮(加入 _wordHls 并渲染)。同范围去重防重复点同词叠两层。
function _materializeWordHl(hl) {
  const pw = hl.pw;
  if (!pw || !hl.charSel || !pw.__charBoxes) return;
  const rects = _charRangeToPtRects(pw.__charBoxes, hl.charSel.startIdx, hl.charSel.endIdx);
  if (!rects.length) return;
  hl.rects = rects; hl.shown = true;
  _wordHls = _wordHls.filter(o => !(o.page === hl.page && o.charSel && o.charSel.startIdx === hl.charSel.startIdx));
  _wordHls.push(hl);
  const sel = pw.querySelector('.sel-overlay'); if (sel) sel.innerHTML = '';   // 移交持久层(蓝选区→呼吸高亮)
  _renderWordHlsFor(pw);
}
// 点呼吸高亮：就绪→直接弹小框并移除该高亮；未就绪→用户主动开"查词中"小框(等 fetch 回来自动填)
function _wordHlClick(hl) {
  if (!hl) return;
  if (hl.ready) {
    if (hl.error) { _toast?.('查词失败'); _removeWordHl(hl); return; }
    _wordPopOwnerId = hl.id;
    _renderWordPop(hl.word, hl.ctx, hl.data, hl.charSel);
    _removeWordHl(hl);
  } else {
    hl.boxOpen = true; _wordPopOwnerId = hl.id;
    _wordPopState = {word: hl.word, ctx: hl.ctx, lemma: hl.word};
    const pop = document.getElementById('word-pop');
    pop.style.display = 'block'; window._wordPopOpenAt = Date.now();
    pop.innerHTML = '<div style="padding:14px;color:#8a9bb4">⏳ 查词中…</div>';
    _positionWordPop(pop, hl.charSel);
    _renderWordHlsFor(hl.pw);   // boxOpen=true → renderWordHl 过滤掉它,不再画呼吸高亮
  }
}
async function _lookupWordFetch(word, ctx) {
  const r = await fetch('/pdf/api/dict-quick?word=' + encodeURIComponent(word) +
    '&file=' + encodeURIComponent(FILE_REL || '') + '&page=' + ((typeof _selPageNum === 'function' ? _selPageNum() : currentPage) || 0) +
    '&context=' + encodeURIComponent(ctx || '') +
    '&langs=' + encodeURIComponent((BOOK_LANGS || []).join(',')));
  return await r.json();
}
// 渲染单词小框(已拿到 dict-quick 结果 d)。cs=查词时捕获的 charSel,用于定位(防之后选区变了定位飘)。
function _renderWordPop(word, ctx, d, cs) {
  const pop = document.getElementById('word-pop');
  _wordPopState = {word, ctx: ctx || '', lemma: word};
  if (!d || !d.ok) { pop.style.display = 'none'; _expandWordFull(word, ctx); return; }   // ecdict 没有 → 直接完整
  try { _dictCache.set(word, d); if (_dictCache.size > 600) _dictCache.delete(_dictCache.keys().next().value); } catch (_) {}
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
  pop.style.display = 'block';
  window._wordPopOpenAt = Date.now();   // 框外关闭监听据此忽略刚弹出时的余波事件
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
  _positionWordPop(pop, cs);
  // 查过即记入生词库 → 刷新本页下划线（橙=新/黄=见过/淡绿=熟）
  try { refreshVocabUnderlinesForAllPages(); } catch (_) {}
}
// 「结果到了自动弹出」的取消信号:点词后只要页面被滚动/滚轮、或又点了别的词,
// 慢词结果回来就**不再**自动弹(退回旧行为:常亮高亮等用户点,位置才不会错)。
let _wordPopCancelSeq = 0;
(() => {
  const m = document.getElementById('main');
  if (m) {
    m.addEventListener('scroll', () => { _wordPopCancelSeq++; }, { passive: true });
    m.addEventListener('wheel',  () => { _wordPopCancelSeq++; }, { passive: true });
  }
})();

window.showWordPopover = async (word, ctx) => {
  word = (word || '').trim().toLowerCase();
  if (!word) return;
  const _cseq = ++_wordPopCancelSeq;   // 本次点词占位;同时取消上一个还没回来的词的自动弹出
  toolbar.classList.remove('open');
  const pw = _charSel && _charSel.pw;
  const cap = _charSel ? {pw, startIdx: _charSel.startIdx, endIdx: _charSel.endIdx} : null;
  // 已有现成数据(本会话查过)→ **直接秒显小框**,不发请求/不建高亮;后台再打一次刷新暴露计数+缓存
  const cached = _dictCache.get(word);
  if (cached) {
    _wordPopOwnerId = ++_wordHlSeq;   // 占新 owner id(框归属);没有 hl 会匹配它 → 别的并发慢词回来不会覆盖本框
    _renderWordPop(word, ctx, cached, cap);
    _lookupWordFetch(word, ctx).then(d => { if (d && d.ok) _dictCache.set(word, d); }).catch(() => {});
    return;
  }
  // 每次查词一个独立 hl;多个可并存(各自呼吸/各自点开)，**不清掉别的查词高亮**。
  // 慢词(>400ms 未回)才物化成呼吸高亮当等待指示;快词(含服务端已缓存)直接弹小框(全程不显示"查词中"框)。
  const hl = {
    id: ++_wordHlSeq, page: pw ? (parseInt(pw.dataset.pageNum || '0', 10) || currentPage) : currentPage,
    pw, word, ctx, charSel: cap, rects: null, shown: false, ready: false, data: null, error: null, boxOpen: false,
  };
  const hlTimer = setTimeout(() => { if (!hl.ready && cap) _materializeWordHl(hl); }, 400);
  let d = null, err = null;
  try { d = await _lookupWordFetch(word, ctx); } catch (e) { err = e; }
  clearTimeout(hlTimer);
  hl.ready = true; hl.data = d; hl.error = err;
  if (!hl.shown) {
    // 快路径:300ms 内回来,没物化高亮 → 直接弹小框(归属本 hl)
    if (err) { _toast?.('查词失败：' + err.message); return; }
    _wordPopOwnerId = hl.id;
    _renderWordPop(word, ctx, d, cap);
  } else {
    // 慢路径:呼吸高亮已在 → 重画(本 hl 转常亮)
    _renderWordHlsFor(hl.pw);
    if (hl.boxOpen) {   // 用户已点高亮开了"查词中"小框 → 查完就清掉该高亮;框仍归属本 hl 才填(否则被别的查词接管了)
      if (_wordPopOwnerId === hl.id) {
        if (err) { const p = document.getElementById('word-pop'); if (p) p.innerHTML = '<div style="padding:14px;color:#c00">查词失败：' + err.message + '</div>'; }
        else _renderWordPop(word, ctx, d, hl.charSel);
      }
      _removeWordHl(hl);
    } else if (!err && _wordPopCancelSeq === _cseq) {
      // 结果到了且期间**没滚动页面、也没点别的词** → 自动弹出,不用再点高亮
      // (位置安全:没滚动过 → charSel 矩形仍是点击时的屏幕位置)。滚动/再点词后保持旧行为。
      _wordPopOwnerId = hl.id;
      _renderWordPop(word, ctx, d, hl.charSel);
      _removeWordHl(hl);
    }
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
// 共享模式(__uiShared)下点词已直连 RC.wordpop.show(rc-wordpop.js 自己的 _expandWordFull 读它自己的 _wordPopState,
//   已正确接线);本文件(reader.js 拼接产物,最后加载)若无条件也赋值 window._expandWordFull,会覆盖掉 rc-wordpop.js
//   的版本——而这份读的是 reader.src 自己的 _wordPopState(共享模式下从未被填,PDF 早已不走原生 showWordPopover 那条路),
//   于是 word 恒为 undefined,`if (!word) return;` 直接短路退出,「展开完整词典」按钮点了没反应(比 _wordPopMaster/
//   _wordPopGrammar/_speakCurWord 那三个更隐蔽——内部本有 __uiShared 分支,但 word 校验在分支之前就已经 return 了)。
//   门控:共享模式下别赋值;legacy 模式(!__uiShared)保留原生行为不变。
if (!window.__uiShared) {
window._expandWordFull = (w, c) => {
  const s = _wordPopState;
  const word = w || (s && s.word);
  const ctx = (c != null ? c : (s && s.ctx)) || '';
  const pop = document.getElementById('word-pop'); if (pop) pop.style.display = 'none';
  if (!word) return;
  if (window.__uiShared && window.PdfAdapter) {   // 阶段2:全词典大框 → rc-wordpop(共享模式;else 为原 dictStreamJP/dictStream,逐字不变)
    PdfAdapter.openFullDict({ word, context: ctx, jp: _isJaWord(word), file: FILE_REL,
      page: (typeof _selPageNum === 'function' ? _selPageNum() : currentPage), langs: BOOK_LANGS,
      fallback: () => { if (_isJaWord(word)) dictStreamJP(word, ctx); else dictStream(word, ctx); } });
    return;
  }
  if (_isJaWord(word)) dictStreamJP(word, ctx);   // 日语完整字典(离线富内容+按需AI)
  else dictStream(word, ctx);                     // 英语三源大框(ecdict+free+mw+例句)
};
}
// 小框喇叭：读当前词（避开 onclick 内联传参的引号冲突），同步播有道(手势栈内)
// 共享模式(__uiShared)下 rc-wordpop.js 核心框的按钮 onclick 写死调这几个全局名,但本文件(reader.js 拼接产物,
//   最后加载)若无条件也赋值,会覆盖 rc-wordpop.js 刚设好的版本(读的是不同的 _wordPopState,导致按钮静默失效)。
//   门控:共享模式下别赋值,让 rc-wordpop.js 自己的版本生效;legacy 模式(!__uiShared)保留原生行为不变。
if (!window.__uiShared) {
window._speakCurWord = () => {
  const s = _wordPopState;
  if (!s) return;
  if (s.reading) { _ttsWord(s.reading, 'ja-JP'); return; }   // 日语:直接念假名读音
  const w = s.lemma || s.word;
  if (w) _speakOnline(w);
};
}
// 小框「掌握」toggle（日英统一）：未掌握 ↔ 掌握 100 来回切，不关框。
// 掌握 → 该词不再标生词下划线；按语言走不同 store：日语 jp-vocab-mark(mastered/unknown)，
// 英语 vocab-mark(known/unknown，写 vocab 笔记 frontmatter.user_mark + 锁 mastery)。
// 乐观更新:标掌握瞬间先把该词下划线从所有已加载页移掉(不等服务器写库+重算),服务器响应后 refresh 再校正。
// 大厂标配(optimistic UI):本地实例下尤其明显——画面立刻响应,不用等任何往返。
function _dropVocabUnderlineOptimistic(s) {
  // __vocabMarks 是“全候选目录”，取消掌握时要靠它立刻恢复 was→be 等变形。
  // 旧实现把候选数组过滤改写，掌握虽快，却永久丢掉了本地恢复所需的数据。
  if (typeof window.applyVocabLocalOverride === 'function') {
    return window.applyVocabLocalOverride(
      (s && (s.lemma || s.word)) || '',
      true,
      s || null
    );
  }
  return function () {};
}
// 共享版 rc-wordpop 的「☆ 标记掌握」乐观去下划线经此调用(PDF 字符层专属;EPUB 走 __epubDeco.optimisticMaster)。
try { window.__pdfDropVocabUnderline = _dropVocabUnderlineOptimistic; } catch (_) {}

if (!window.__uiShared) {
window._wordPopMaster = (btn) => {
  const s = _wordPopState; if (!s) return;
  const next = !s.mastered;
  // 词组(多词/含空格):走 phrase-mark store，不建 ghost vocab 笔记；标掌握后该词组不再画生词下划线
  if (s.phrase) {
    const t = s.word;
    if (btn) btn.disabled = true;
    // 乐观:标掌握瞬间先去掉该词组下划线(对齐下方非 phrase 分支);失败按快照回滚
    let _undoDrop = null;
    if (next) {
      const snaps = [];
      document.querySelectorAll('[data-loaded="1"][data-page-num]').forEach(pw => {
        if (pw.__vocabMarks && pw.__vocabMarks.length) snaps.push({pw, marks: pw.__vocabMarks});
      });
      _dropVocabUnderlineOptimistic(s);
      const changed = snaps.filter(sn => sn.pw.__vocabMarks !== sn.marks).map(sn => ({...sn, after: sn.pw.__vocabMarks}));
      // 只回滚仍是本次乐观结果的页(防覆盖期间其他刷新写入的新 marks)
      _undoDrop = () => changed.forEach(({pw, marks, after}) => {
        if (pw.__vocabMarks === after) { pw.__vocabMarks = marks; try { renderVocabUnderlines(pw, marks); } catch (_) {} }
      });
    }
    fetch('/pdf/api/phrase-mark', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text: t, mark: next ? 'mastered' : ''}),
    }).then(r => r.json()).then((d) => {
      if (d && d.ok === false) throw new Error(d.error || 'fail');
      s.mastered = next;
      _phraseMarkSet = new Set(d.mastered || []);
      if (btn) {
        btn.disabled = false;
        btn.textContent = s.mastered ? '✓ 已掌握 100' : '☆ 标记掌握';
        btn.title = s.mastered ? '点击取消掌握（恢复词组下划线）' : '标记掌握 100（该词组不再标生词下划线）';
        btn.classList.toggle('wp-anki', s.mastered);
      }
      refreshCharsWForAllPages();   // 重拉 w + 重画下划线(掌握→该词组下划线消失)
      _toast?.(s.mastered ? '已掌握，下划线消失' : '已取消掌握');
    }).catch(() => { if (_undoDrop) _undoDrop(); if (btn) btn.disabled = false; _toast?.('标记失败'); });
    return;
  }
  const w = s.lemma || s.word;
  const url = s.jp ? '/pdf/api/jp-vocab-mark' : '/pdf/api/vocab-mark';
  const mark = next ? 'known' : 'unknown';   // 日英统一口径(jp-vocab-mark 已接受 known/unknown)
  if (next) _dropVocabUnderlineOptimistic(s);   // 乐观:立刻去下划线,不等服务器
  if (btn) btn.disabled = true;
  fetch(url, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({word: w, mark}),
  }).then(r => r.json()).then((d) => {
    if (d && d.ok === false) throw new Error(d.error || 'fail');
    s.mastered = next;
    try { const c = _dictCache.get(s.word); if (c) c.mastered = next; } catch (_) {}   // 同步缓存,再点不显旧掌握态
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
}
// 小框「📊 语法」：对该词所在整句做语法分析（复用 onGrammarAnalyze：当前 _charSel=单词→自动扩成整句）
if (!window.__uiShared) {
window._wordPopGrammar = () => {
  const p = document.getElementById('word-pop'); if (p) p.style.display = 'none';
  try { onGrammarAnalyze(); } catch (e) { window.dlog && window.dlog('grammar from word-pop fail: ' + (e && e.message)); }
};
}
// 工具栏「🔍 查词」：弹单词小框
window.onLookupWord = () => {
  const controller = window.__bwSelectionController;
  const selected = (controller && controller.current && controller.current()) || {
    text: lastSelText || '', context: '', rect: null
  };
  const text = String(selected && selected.text || '').trim();
  if (!text) return;
  let ctx = String(selected.context || selected.ctx || selected.sentence || '');
  const adapter = (window.RC && RC.adapter) ? RC.adapter() : null;
  if (!ctx && _charSel && _charSel.pw && _charSel.pw.__charBoxes) {
    const chars = _charSel.pw.__charBoxes;
    const cr = _expandSentenceFromRange(chars, _charSel.startIdx, _charSel.endIdx);
    if (cr) ctx = _charsRangeToText(chars, cr.start, cr.end).slice(0, 400);
  }
  if (window.__uiShared && adapter && typeof adapter.lookupWord === 'function') {
    let file = FILE_REL, langs = BOOK_LANGS, page = (typeof _selPageNum === 'function' ? _selPageNum() : currentPage);
    try {
      const info = adapter.fileInfo && adapter.fileInfo();
      if (info && info.file) file = info.file;
      const hostContext = adapter.getContext && adapter.getContext();
      if (hostContext && Array.isArray(hostContext.langs)) langs = hostContext.langs;
      if (selected.anchor && selected.anchor.page != null) page = selected.anchor.page;
    } catch (_) {}
    adapter.lookupWord({
      word: text, context: ctx, page, file, langs,
      anchorRect: adapter.kind === 'pdf' ? _charSel : selected.rect,
      fallback: (w, c) => showWordPopover(w, c)
    });
  } else {
    showWordPopover(text, ctx);
  }
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

function _ctxSelReport(txt) {
  // 选区即时同步(用户拍板 2026-07-27):建立/改动/**清空**都立刻推,不走导航防抖。
  // 传空串而不是省略字段 —— 省略会让快照留着上一次的旧选区(静默退化)。
  try {
    window.RC?.ctxSync?.report(
      { kind: 'pdf', file: FILE_REL, selection: txt || '', sel_page: currentPage },
      { immediate: true });
    // 焦点通道(与选区并行,语义不同:选区是"选了什么文字",焦点是"当前对象是谁")。
    // 取消必须显式发,否则上游会拿着已取消的对象当现状。
    if (txt) window.RC?.outgoing?.focus('text', { file: FILE_REL, page: currentPage, text: txt.slice(0, 200) });
    else window.RC?.outgoing?.cancel();
  } catch (_) {}
}
function checkSelection() {
  const sel = window.getSelection();
  const txt = (sel.toString() || '').trim();
  if (!txt || txt.length < 2) {
    // 无原生 selection：char-layer 自定义选中(画在 sel-overlay、不走原生 selection)还在的话别清掉它
    if (!(_charSel && lastSelText)) { paintSelectionOverlay(); _ctxSelReport(''); }
    // char-layer 自定义选中仍在 → 屏幕上**确实还有选区**,要上报它而不是空;
    // 原来这里整个跳过上报,导致"取消原生选中"后快照永远停在旧值(真机实测清空 0 次上报)。
    else _ctxSelReport(lastSelText || '');
    return;
  }
  paintSelectionOverlay();
  lastSelText = txt;
  _ctxSelReport(txt);
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

// 手写起笔时调(pdf-tail._inkBeginGuard):把任何**已有选中/查词框**一把清掉 + 关选中工具栏。
//   用户点子:画笔工作时选中内容全部取消选中(治 palm 抢在笔前落下、或第一笔误触已经选中的字)。
window.__clearContentSelection = function () {
  try { if (_charSel && _charSel.pw) _charSel.pw.querySelector('.sel-overlay')?.replaceChildren(); _charSel = null; } catch (_) {}
  try { document.querySelectorAll('.sel-overlay').forEach(ov => ov.innerHTML = ''); } catch (_) {}
  try { window.getSelection().removeAllRanges(); } catch (_) {}
  try { lastSelText = ''; } catch (_) {}
  try { toolbar.classList.remove('open'); } catch (_) {}
  try { _updateSelPreview(''); } catch (_) {}
  try { var _wp = document.getElementById('word-pop'); if (_wp) _wp.style.display = 'none'; } catch (_) {}   // 关查词/词组小框(共用 #word-pop)
  try { if (window.RC && RC.wordpop && RC.wordpop.clearHls) RC.wordpop.clearHls(); } catch (_) {}            // 清查词高亮
};

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
// ── 高亮触摸手势:全阅读器**唯一实例**(照 EPUB epub-html.js 的 document 委托做法)。
//    key = 高亮 id,故同一条高亮的多个重叠 rect 上的两次点会正确配对成双击。
let _hlOpenedAt = 0;
function _openHlEditorById(id) {
  const now = Date.now(); if (now - _hlOpenedAt < 520) return;   // 去重:iOS 的 pointerup 与原生 dblclick 可能双触发
  const h = _allHighlights.find((x) => x.id === id); if (!h) return;
  const div = document.querySelector('.hl-saved[data-id="' + (window.CSS && CSS.escape ? CSS.escape(id) : id) + '"]');
  const pw = div && div.closest ? div.closest('.page-wrap') : null;
  if (!div || !pw) return;
  _hlOpenedAt = now; openHlPopover(h, div, pw);
}
let _hlGestureSingleton = null;
function _hlGesture() {
  if (_hlGestureSingleton) return _hlGestureSingleton;
  if (!(window.RC && RC.highlight && RC.highlight.gesture)) return null;
  _hlGestureSingleton = RC.highlight.gesture({
    onLongPress: (id) => {
      const h = _allHighlights.find((x) => x.id === id);
      if (h && window.__asstOpen && window.__asstOpen()) _pdfHlToAsst(h);
    },
    doubleTapMs: 420, moveTol: 12,
    onDoubleTap: (id) => _openHlEditorById(id)
  });
  // 原生 dblclick 兜底:两次点落在不同 rect 时 dblclick 派发到公共祖先,故委托在 document 上按落点反查。
  document.addEventListener('dblclick', (e) => {
    const el = document.elementFromPoint(e.clientX, e.clientY);
    const hit = el && el.closest ? el.closest('.hl-saved') : null;
    if (!hit || !hit.dataset.id) return;
    e.preventDefault(); e.stopPropagation();
    _openHlEditorById(hit.dataset.id);
  }, true);
  return _hlGestureSingleton;
}
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
    const r = await fetch('/pdf/api/highlights?file=' + encodeURIComponent(FILE_REL), { cache: 'no-store' });   // no-store:撤销/改高亮后必须拿最新,否则命中浏览器缓存看不到变化
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

// 高亮底色用**半透明** rgba(不是实色):字一定透得出来(不被实色块盖死);配合 .hl-saved 的 mix-blend-mode:multiply,
// 能 multiply 到页图上时字更锐(黄×黑字=黑),万一某些页 multiply 被隔离也只是「半透明色」不会糊死/盖死正文。
function _hlRgba(hex, a) {
  const m = /^#?([0-9a-fA-F]{6})$/.exec((hex || '').trim());
  if (!m) return hex;   // 非 #rrggbb(已是 rgba/命名色)→ 原样
  const n = parseInt(m[1], 16);
  return 'rgba(' + (n >> 16 & 255) + ',' + (n >> 8 & 255) + ',' + (n & 255) + ',' + a + ')';
}
function renderHighlightsOnPage(pw, pageNum) {
  if (!pw) return;
  const layer = ensurePageLayer(pw, 'hl-layer');
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
      if (hasColor) div.style.background = _hlRgba(h.color, 0.4);   // 半透明底色:字透出来,不被实色盖死
      div.title = (h.note || h.body || h.sentence || h.text || '').slice(0, 200);
      // 用 capture phase 拦事件，确保不被 char-layer 拖选逻辑捕获
      const stop = (e) => { e.stopPropagation(); };
      div.addEventListener('mousedown',  stop, true);
      div.addEventListener('mouseup',    stop, true);
      div.addEventListener('touchstart', stop, {passive:true, capture:true});
      div.addEventListener('touchend',   stop, {passive:true, capture:true});
      // 交互(2026-07-05;2026-07-21 用户要求长按↔双击对调):长按 → 高亮内容加入对话上下文(助手开着时);双击 → 高亮弹框。单击不再开框。
      // 走共享 RC.highlight.gesture 的**唯一实例**(与 EPUB 的 document 委托同构)。
      // ⚠ 手势状态必须按 h.id 跨 rect 共享:一条高亮有多个**互相重叠**的 rect div,若每个 div 各持
      //   一个 gesture 实例,两次点落在不同 rect 上就各自算「第一击」,原生 dblclick 又因 target
      //   不同而派发到公共祖先 → iPad 上必须点三次才弹框(用户实测)。
      const _hg = _hlGesture();
      if (_hg) {
        div.addEventListener('pointerdown',   (e) => { e.stopPropagation(); _hg.down(h.id, e.clientX, e.clientY); }, true);
        div.addEventListener('pointermove',   (e) => { _hg.move(e.clientX, e.clientY); }, true);
        div.addEventListener('pointerup',     (e) => { e.stopPropagation(); _hg.up(h.id); }, true);
        div.addEventListener('pointercancel', () => _hg.cancel(), true);
      } else {
        div.addEventListener('click', (e) => { e.stopPropagation(); openHlPopover(h, div, pw); }, true);
      }
      layer.appendChild(div);
    }
  }
}

// 双击高亮(助手开着)→ 复用助手输入框上方的「焦点选中」chip(window.__setFocusSel:带 ✕、可随时取消、一眼看到已选中),
// 不弹 toast。__focusSel 经 __voiceContext.focus_sel 带给助手。跟公式/段落焦点选区同一套 UI。
function _pdfHlToAsst(h) {
  try {
    const txt = ((h.text || h.body || h.sentence || '') + '').trim(); if (!txt) return;
    // 复用助手输入框上方的「焦点选中」chip(带 ✕,可随时取消 + 一眼看到已选中),不弹 toast。__focusSel 进 __voiceContext.focus_sel。
    if (window.__setFocusSel) window.__setFocusSel(txt.slice(0, 400), 'text');
  } catch (e) {}
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
    const _sameBk = cur && c.bk != null && cur.bk != null && c.bk >= 0 && c.bk === cur.bk;   // #56:同排版块=同视觉行(OCR justified),水平相邻即合并,不受块内字符 top 抖动分段(治句子/下划线在括号处断)
    if (cur &&
        (_sameBk || Math.abs(y0 - cur.y0) <= Math.max(2, Math.max(cur.y1 - cur.y0, y1 - y0) * 0.6)) &&   // 跨块才按 y0 判行(跨行 y0 差>字高分段)
        x0 <= cur.x1 + Math.max(2, lineH * 0.6)) {
      cur.x1 = Math.max(cur.x1, x1);
      cur.y0 = Math.min(cur.y0, y0);
      cur.y1 = Math.max(cur.y1, y1);
    } else {
      if (cur) rects.push([cur.x0, cur.y0, cur.x1, cur.y1]);
      cur = {x0, y0, x1, y1, bk: c.bk};
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
    // 网络不通 + outbox → local-first:客户端生成 id(c_ 前缀),本地即时渲染,
    // 入队恢复后补投(服务端 POST 幂等 upsert 同 id,重放安全)。2026-07-20 outbox 第二批。
    if (window.RC && RC.outbox && e && e.name === 'TypeError') {
      const cid = 'c_' + Array.from(crypto.getRandomValues(new Uint8Array(8))).map(b => b.toString(16).padStart(2, '0')).join('');
      const h = Object.assign({ id: cid, time: Math.floor(Date.now() / 1000) }, payload);
      _allHighlights.push(h);
      (_hlByPage[pageNum] ||= []).push(h);
      renderHighlightsOnPage(pw, pageNum);
      _lastHlColor = color;
      try { localStorage.setItem('pdf-hl-last-color', color); } catch (_) {}
      RC.outbox.send('hl', cid, '/pdf/api/highlights', Object.assign({ id: cid }, payload));
      if (RC.toast) RC.toast('已高亮(离线,恢复后自动同步)');
      return h;
    }
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
// ── 阶段6 门控:ui=shared → RC.grammar.renderTrackList 渲进同一个 #set-grammar-list 容器(跟 EPUB 共用);
//   else 走原生逐字(_renderGrammarTrackListNative)。──
async function renderGrammarTrackList() {
  if (window.__uiShared && window.RC && RC.grammar) {
    return RC.grammar.renderTrackList('set-grammar-list', {
      file: FILE_REL,
      onAfterChange: () => { loadGrammarTracked(); },   // 保持工具栏「📊 语法」按钮可见性用的原生缓存同步
    });
  }
  return _renderGrammarTrackListNative();
}
async function _renderGrammarTrackListNative() {
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
// ── 阶段6 门控:ui=shared → 用 PDF 字符层抽出 sentence/text(char-layer host-bind,不迁进共享层)后交给
//   RC.grammar.analyze 统一渲染(块/结构图/流式/制卡/追问全走共享核心,跟 EPUB 同一套代码);
//   else 走原生逐字(_onGrammarAnalyzeNative)。RC 不可用 → 落回原生,绝不吞功能。──
window.onGrammarAnalyze = async () => {
  if (window.__uiShared && window.RC && RC.grammar) return _onGrammarAnalyzeShared();
  return _onGrammarAnalyzeNative();
};
async function _onGrammarAnalyzeShared() {
  if (!lastSelText) return;
  if (!_charSel || !_charSel.pw || !_charSel.pw.__charBoxes) { _toast?.('找不到选中位置'); return; }
  const pw = _charSel.pw;
  const chars = pw.__charBoxes;
  const ctxRange = _expandSentenceFromRange(chars, _charSel.startIdx, _charSel.endIdx);
  if (!ctxRange) { _toast?.('无法识别完整句子'); return; }
  const sentence = _charsRangeToText(chars, ctxRange.start, ctxRange.end);
  const text = lastSelText.trim();   // 用户选中的子串（焦点）
  toolbar.classList.remove('open');
  RC.grammar.analyze({
    file: FILE_REL, sentence, text, container: 'grammar-panel-body',
    aiParams: _getAiOverrides, viewModeKey: 'pdf-grammar-view',
    sourceUrl: () => FILE_REL ? (location.origin + '/pdf/view?file=' + encodeURIComponent(FILE_REL) + '&page=' + (currentPage || 1)) : '',
    onOpenPanel: () => { openGrammarPanel(); switchSideTab('grammar'); },
    onToast: (m) => _toast?.(m),
  });
}
async function _onGrammarAnalyzeNative() {
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
    const r = await __safeFetch('/pdf/api/grammar-analyze', {   // 幂等计算:切后台被掐→回前台自动重试(不重复副作用)
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text, sentence, file: FILE_REL, enabled_books: _grammarEnabledBooks}),
    }, {retries: 2});
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
    // 双页模式下侧栏挤窄 → 两页并排太小 → **临时**切单列(不写 localStorage/orient),关栏自动还原双页。
    // 但悬浮显示模式抽屉盖在正文上、不挤压 → 页面不变窄 → 保持双页不切。
    if (readMode === 'spread' && !document.body.classList.contains('grammar-floating')) {
      _spreadBeforePanel = _spreadOffset;
      readMode = 'continuous';
      _updateModeButtons();
    } else {
      _spreadBeforePanel = null;   // 单列下开栏:清残留标记(修"单页开关侧栏被莫名切到双页")
    }
    // 打开侧栏让 #main 变窄 → 重算 scale(debounce,挪出动画帧 + 与 ResizeObserver 去重)。
    // 悬浮模式 #main 宽度不变 → 不重排(重排会让背后 PDF 重渲染→闪烁);仅挤压模式重排。
    if (!document.body.classList.contains('grammar-floating')) _scheduleRefit(true);
  }
  // 展开时若停在语法 tab 且历史未载 → 主动载（刷新后默认语法 tab，不点切换也能显示记录）
  const _onGr = document.querySelector('#side-tabs .side-tab[data-pane="grammar"]')?.classList.contains('active');
  if (_onGr && !_grammarHistLoaded && typeof loadGrammarHistory === 'function') loadGrammarHistory();
}
window.closeGrammarPanel = () => {
  document.getElementById('grammar-panel')?.classList.remove('open');
  document.body.classList.remove('grammar-open');
  _hideDepTip();
  // 还原侧栏打开时临时切走的双页——仅当当前仍是"被临时切出来的单列"(开栏期间手动改过模式就不还原)
  if (_spreadBeforePanel != null && readMode === 'continuous') {
    readMode = 'spread';
    _spreadOffset = _spreadBeforePanel;
    _updateModeButtons();
  }
  _spreadBeforePanel = null;
  if (!document.body.classList.contains('grammar-floating')) _scheduleRefit(true);   // 悬浮模式宽度不变→不重排(免闪);挤压才重排
};

// ── 右栏外观设置：悬浮显示 + 背景模糊度。**按排版(continuous/spread)分别记忆**(用户要两种排版各存一套)──
// 键：pdf-gp-{floating,blur}-{continuous|spread}；缺则回退老的全局键(老用户迁移),再回退默认。切排版/转屏后重应用。
function _gpMode() { return (typeof readMode !== 'undefined' && readMode === 'spread') ? 'spread' : 'continuous'; }
function _gpGet(name, def) {
  let v = localStorage.getItem('pdf-gp-' + name + '-' + _gpMode());
  if (v === null) v = localStorage.getItem('pdf-gp-' + name);   // 迁移:老全局键
  return v === null ? def : v;
}
function _gpSyncUI() {
  const f = document.getElementById('gp-floating'); if (f) f.checked = _gpGet('floating', '0') === '1';
  const b = parseInt(_gpGet('blur', '20'), 10);
  const bi = document.getElementById('gp-blur'), bv = document.getElementById('gp-blur-val');
  if (bi) bi.value = b; if (bv) bv.textContent = b;
}
function _gpApplyAppearance() {
  document.body.classList.toggle('grammar-floating', _gpGet('floating', '0') === '1');
  document.documentElement.style.setProperty('--gp-blur', parseInt(_gpGet('blur', '20'), 10) + 'px');
  _gpSyncUI();
}
window._gpApplyAppearance = _gpApplyAppearance;   // 切排版(toggleReadMode/toggleSpread)/旋转后调,重应用本排版的侧栏外观
window._gpSetFloating = (on) => {
  localStorage.setItem('pdf-gp-floating-' + _gpMode(), on ? '1' : '0');
  document.body.classList.toggle('grammar-floating', !!on);
  if (typeof _scheduleRefit === 'function') _scheduleRefit(true);   // 悬浮↔挤压 → #main 宽度变 → 重排
};
window._gpSetBlur = (v) => {
  localStorage.setItem('pdf-gp-blur-' + _gpMode(), String(v));
  document.documentElement.style.setProperty('--gp-blur', v + 'px');
  const el = document.getElementById('gp-blur-val'); if (el) el.textContent = v;
};
window.toggleSideSettings = (ev) => {
  if (ev) ev.stopPropagation();
  const m = document.getElementById('side-settings'); if (!m) return;
  if (m.style.display === 'block') { m.style.display = 'none'; return; }
  _gpSyncUI();
  m.style.display = 'block';
};
document.addEventListener('pointerdown', (e) => {   // 点弹层外部 → 关
  const m = document.getElementById('side-settings');
  if (m && m.style.display === 'block' && !m.contains(e.target) && !e.target.closest('#side-settings-btn')) {
    m.style.display = 'none';
  }
}, true);
_gpApplyAppearance();   // 载入即应用持久化设置
// 顶栏「📊 语法」按钮：打开统一面板并切到语法 tab（再点同 tab 则关闭）
window.toggleGrammarPanel = () => {
  const p = document.getElementById('grammar-panel');
  if (!p) return;
  // 默认开「助手」tab(用户要求):开着且已在助手 → 关;否则开 + 切到助手
  const onAsst = document.querySelector('#side-tabs .side-tab[data-pane="asst"]')?.classList.contains('active');
  if (p.classList.contains('open') && onAsst) { closeGrammarPanel(); return; }
  openGrammarPanel();
  switchSideTab('asst');
  try { window.__renderFigChips && window.__renderFigChips(); } catch (_) {}   // 补渲已带入的图附件条
  try { window.__renderNoteChips && window.__renderNoteChips(); } catch (_) {}  // 补渲便签 chip(双击便签带进来的)
  try { window.__renderFocusSel && window.__renderFocusSel(); } catch (_) {}   // 补渲焦点选区(公式/段落)chip
  try { window.__asstPrewarm && window.__asstPrewarm(); } catch (_) {}         // 预热 claude 进程(减冷启动)
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
    // 为什么:原手写 SSE 循环每个 chunk 都全文重渲+MathJax 排版(零节流卡主线程,且断连丢结果);
    // 改用 _aiStream(同 17-highlight _followupAsk):自带 80ms 节流 + 非 SSE JSON 兜底 + rid 断连轮询。
    const render = (t) => {
      aDiv.innerHTML = md(t || ' ');
      if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([aDiv]).catch(() => {});
    };
    const res = await _aiStream('/pdf/api/explain', {
      method: 'POST', onText: render,
      body: {text: q, context: '基于这句的语法分析继续回答：\n' + context, model: ov.model || '', effort: ov.effort || ''},
    });
    if (res.ok && res.text) render(res.text);
    else if (!res.ok) aDiv.innerHTML = '追问失败：' + _esc(res.error || '失败');
    else aDiv.innerHTML = '(无回答)';
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
// ── 阶段6 门控:ui=shared → 分析块由 RC.grammar 渲染,gv-switch 按钮已直接闭包调 RC.grammar.setViewMode,
//   这里只需处理"设置面板下拉改值"这一条剩余入口,转调同一份共享逻辑;else 走原生逐字。──
window.setGrammarView = (mode) => {
  if (window.__uiShared && window.RC && RC.grammar) {
    RC.grammar.setViewMode('grammar-panel-body', mode, 'pdf-grammar-view');
    const sel = document.getElementById('set-grammar-view');
    if (sel) sel.value = RC.grammar.getViewMode('pdf-grammar-view');
    return;
  }
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
    word, file: FILE_REL || '', page: String((typeof _selPageNum === 'function' ? _selPageNum() : currentPage) || 0), context: ctx || '',
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
  // 共享模式(__uiShared)→ 走 PdfAdapter.openHlEditor → RC.highlight.openEditor(编辑浮层统一)。
  //   改色/备注/删除回调复用底座 _hlUpdate/_hlDelete(取消颜色语义照搬下方 268-277);RC 不可用 → fallback 回 _openHlPopoverNative(原逻辑逐字不变)。
  if (window.__uiShared && window.PdfAdapter) {
    document.getElementById('hl-popover')?.classList.remove('open');   // 防原生小框残留
    PdfAdapter.openHlEditor({
      colors: getHlColors(), current: h.color, note: h.note || '',
      preview: h.text || '', sentence: h.sentence || '', body: h.body || '', kind: h.kind,
      anchorEl: anchorDiv, anchorSelector: '.hl-saved', placeBelow: true,
      silent: true,   // _hlUpdate 会弹「已保存」→ 抑制 rc-highlight 的重复 toast(M5)
      onColor: (c) => {
        if (!c) {                                       // 取消颜色:照搬下方 268-277 语义
          const hasNote = (h.note || '').trim() || (h.body || '').trim() || (h.sentence || '').trim();
          if (!hasNote) _hlDelete(h, pw);
          else { _hlUpdate(h, pw, { color: '' }); _toast('已取消颜色（备注保留）'); }
        } else _hlUpdate(h, pw, { color: c });
      },
      onNote: (t) => _hlUpdate(h, pw, { note: t }),
      onDelete: () => _hlDelete(h, pw),
      fallback: () => _openHlPopoverNative(h, anchorDiv, pw),
    });
    return;
  }
  return _openHlPopoverNative(h, anchorDiv, pw);
}
function _openHlPopoverNative(h, anchorDiv, pw) {
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
  if (!confirm('删除这条高亮？')) return false;   // 取消 → 返回 false，让 rc-highlight 编辑浮层保持打开(M6)
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
// 共享模式(__uiShared)下 rc-wordpop.js 自己的日语汉字拆解 chip 已改用 addEventListener+本模块闭包(H1修复,不依赖全局);
//   本文件无条件赋值会覆盖 rc-wordpop.js 留的兼容导出,且读的是本文件自己的 _jpKanjiData(共享模式下从未被填,恒空数组)。
//   门控:共享模式下别赋值;legacy 模式(!__uiShared)保留原生行为不变。
if (!window.__uiShared) {
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
}
if (!window.__uiShared) {
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
}

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
function _syncDraftsLS() {   // 共享模式(?ui=shared):reader.js 与 rc-result 各有 _drafts 内存副本,读前从 localStorage 重载,rc-result 的「+选段」才对 reader.js 的草稿框/制卡可见;默认路径(__uiShared 未定义)early-return 零改动
  if (!window.__uiShared) return;
  try { _drafts = JSON.parse(localStorage.getItem('pdf-drafts') || '[]'); } catch (_) {}
}
// 直接把一段文本(如公式 LaTeX)加进草稿,供「制卡/笔记」用(公式浮层等外部调用)
window.addDraftText = (text, source, src) => {
  _syncDraftsLS();   // 共享模式:先从 localStorage 重载,别用陈旧 _drafts 覆盖掉 rc-result「+选段」刚写入的草稿
  const t = (text || '').trim();
  if (!t) return false;
  if (_drafts.some(d => d.text === t)) { _updateDraftBadge(); return true; }
  _drafts.push({ id: 'd_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6),
                 text: t, source: source || '公式', src: src || '', time: Date.now(), selected: true });
  _persistDrafts(); _updateDraftBadge();
  return true;
};
function _updateDraftBadge() {
  _syncDraftsLS();
  const b = document.getElementById('draft-badge');
  document.getElementById('draft-count').textContent = _drafts.length;
  b.classList.toggle('show', _drafts.length > 0);
}
window.openDraftModal = () => {
  _syncDraftsLS();
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
  _syncDraftsLS();
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
  s = String(s == null ? '' : s);
  if (window.marked && marked.parse) {
    try {
      // ① 先把数学公式整段抠出来换占位符再跑 marked,否则 marked 会把 $P(A_1)P(A_2)$ 里的
      //    _ 当斜体、* 当强调、\ 当转义拆坏 → 即便模型写了 $...$ 也渲染失败(本 bug 根因之一)。
      //    占位符 @@MJX{n}@@ 纯字母数字,marked 原样保留;渲染后再换回原公式交给 MathJax。
      const math = [];
      const hold = (m) => '@@MJX' + (math.push(m) - 1) + '@@';
      // ② CJK 与 markdown 强调标记紧贴时 marked 不识别(如 接**-ing**) → 插零宽空格给 ** / * / ` 留边界
      const t = s
        .replace(/\$\$[\s\S]+?\$\$/g, hold)               // 块级 $$..$$
        .replace(/\\\[[\s\S]+?\\\]/g, hold)               // 块级 \[..\]
        .replace(/\$(?!\s)(?:\\\$|[^$\n])+?\$/g, hold)     // 行内 $..$(去掉 $$ 后剩的;$ 后须非空白,避开 "$ 5")
        .replace(/\\\([\s\S]+?\\\)/g, hold)                // 行内 \(..\)
        .replace(/([一-鿿　-〿＀-￯])([*`])/g, '$1\u200b$2')
        .replace(/([*`])([一-鿿　-〿＀-￯])/g, '$1\u200b$2');
      let html = marked.parse(t);
      return html.replace(/@@MJX(\d+)@@/g, (_, i) => (math[+i] != null ? math[+i] : ''));   // ③ 还原公式
    } catch(_) {}
  }
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/\n/g,'<br>');
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
// 暴露给 rc-result 的 beforeOpen 钩子(共享模式:rc-result.openResult 每次开框前调 → 快照上一条进「📜 历史」)
try { window._pushQueryHistory = _pushQueryHistory; } catch (_) {}
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

// AI 设置:旧 per-request model/effort 覆盖(localStorage 'pdf-ai-overrides')已废弃(2026-07 收口)——
// 模型选择唯一真源 = 服务端按功能 action 预设(⚙ AI 模型设置,/api/assistant/action-pref[s])。
// 保留函数给全部下游消费点(aiCall / onOcrSel / onTranslate / _runExplainBg / 17-highlight /
// 18-grammar / 20-result-draft / 05-nav),恒返 {} → 请求体不再带 model/effort(后端各端点也已不读)。
// ?ui=legacy 原生模板的 set-model/set-effort 下拉仍显示但已无效果(模板按约定不动)。
function _getAiOverrides() { return {}; }
function _toggleAiModelRow() {
  const v = document.getElementById('set-sent-backend').value;
  document.getElementById('set-sent-ai-row').style.display = (v === 'ai') ? '' : 'none';
}
window._toggleAiModelRow = _toggleAiModelRow;

// 设置面板页内 tab 切换(AI·翻译 / 阅读 / 语法 / 高亮),记住上次所在 tab
window._setSettingsTab = (name) => {
  document.querySelectorAll('#settings-mask .set-tab').forEach(t => t.classList.toggle('active', t.dataset.pane === name));
  document.querySelectorAll('#settings-mask .set-pane').forEach(p => { p.style.display = (p.dataset.pane === name) ? '' : 'none'; });
  try { localStorage.setItem('pdf-set-tab', name); } catch (_) {}
};
// 设置面板回填(原 openSettings 函数体,除最后"显示 mask"一行,逐字不动)。ui=shared 时 rc-settings 建的
// 统一面板用同一套原生 id(mask 也叫 settings-mask,原模板 mask 已被 pdf-adapter 移除),所以这里的回填 +
// renderHlColorSetting / loadTocStatus / renderGrammarTrackList / _populatePageOffsetUI / _initCharOfsPanel
// 原样复用零重写(saveSettings / closeSettings / _setSettingsTab 同理,都按 id 找元素)。
function _fillSettings() {
  try { _setSettingsTab(localStorage.getItem('pdf-set-tab') || 'ai'); } catch (_) {}
  // set-model/set-effort:仅 ?ui=legacy 原生模板还有(共享面板 rc-settings 已删该下拉)→ 空值保护
  { const _sm = document.getElementById('set-model'); if (_sm) _sm.value = ''; }
  { const _se = document.getElementById('set-effort'); if (_se) _se.value = ''; }
  document.getElementById('set-debug').checked = (localStorage.getItem('pdf-debug') === '1');
  const gv = document.getElementById('set-grammar-view');
  // ui=shared:显示模式状态存在 RC.grammar 里(块渲染已转交它),读它的而不是原生 _grammarViewMode(会跟共享模式下的实际改动脱节)
  if (gv) gv.value = (window.__uiShared && window.RC && RC.grammar) ? RC.grammar.getViewMode('pdf-grammar-view') : _grammarViewMode;
  try { window._populatePageOffsetUI && window._populatePageOffsetUI(); } catch (_) {}   // 页码对齐:填当前页/偏移
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
  { const e = document.getElementById('set-auto-orient'); if (e) e.checked = (localStorage.getItem('pdf-auto-orient') === '1'); }
  // 去边百分比(本书,从已加载的 _crop 回填)
  { const g = (id, v) => { const e = document.getElementById(id); if (e) e.value = v || 0; };
    g('set-crop-l', _crop.l); g('set-crop-r', _crop.r); g('set-crop-t', _crop.t); g('set-crop-b', _crop.b); }
  // 本书文本语言勾选(每本书独立,从 BOOK_LANGS 回填)
  document.querySelectorAll('#lang-checks input').forEach(c => { c.checked = (BOOK_LANGS || []).includes(c.value); });
  { const e = document.getElementById('set-figures'); if (e) e.checked = !!window.__figBookOn; }   // 本书插图描述开关(每本书独立)
  { const e = document.getElementById('set-conceptnet');                                            // 概念网按书开火(共享面板默认隐藏,PDF 揭示+回填)
    if (e) { e.checked = !!window.__conceptNetBookOn;
             const sec = e.closest('[data-sec="pdf-conceptnet"]'); if (sec) sec.style.display = ''; } }
  try { loadTocStatus(); } catch (_) {}   // 书籍目录:已有→显示「已存在」,无→显示建立目录输入
  renderHlColorSetting();
  if (window._initCharOfsPanel) window._initCharOfsPanel();   // 文字层校准块状态
}
function _openSettingsNative() {
  _fillSettings();
  document.getElementById('settings-mask').style.display = 'flex';
}
// ── 门控:ui=shared → PdfAdapter.openSettings → RC.settings.open(rc-settings 统一面板,内容/行为跟 EPUB
//   一致;onFill/onSave/onCancel 直传原生 _fillSettings/saveSettings/closeSettings,机制零重写);
//   RC 不可用 / ?ui=legacy → 原生模板面板逐字不变。──
window.openSettings = () => {
  if (window.__uiShared && window.PdfAdapter && PdfAdapter.openSettings) {
    return PdfAdapter.openSettings({ fill: _fillSettings, fallback: _openSettingsNative });
  }
  return _openSettingsNative();
};
window._applyCropSettings = () => {
  const num = (id) => Math.max(0, Math.min(45, parseFloat(document.getElementById(id)?.value) || 0));
  const crop = {l: num('set-crop-l'), r: num('set-crop-r'), t: num('set-crop-t'), b: num('set-crop-b')};
  saveCropSettings(crop, true);   // 存后端 + 自动开启去边 + 重渲染
  closeSettings();
  _toast?.('去边已应用');
};
// ── 书籍目录(provenance)：已有→显示「已存在」，无→给建立目录的页范围输入 ──
window.loadTocStatus = async () => {
  const st = document.getElementById('set-toc-status'), box = document.getElementById('set-toc-build');
  if (!st) return;
  st.textContent = '检查中…'; if (box) box.style.display = 'none';
  try {
    const d = await (await fetch('/pdf/api/toc?file=' + encodeURIComponent(FILE_REL))).json();
    if (d.ok && d.exists) {
      const src = d.source === 'native' ? '书籍自带' : 'AI 建立';
      st.innerHTML = '✓ 已存在目录（' + src + '，' + d.count + ' 条）<a href="javascript:void 0" onclick="showTocBuild()" style="color:#7dd3fc;margin-left:8px">重建/覆盖</a>';
      if (box) box.style.display = 'none';
    } else {
      st.textContent = '本书暂无目录。可指定目录页范围让 AI 抽取：';
      showTocBuild();
    }
  } catch (_) { st.textContent = '（目录状态获取失败）'; }
};
window.showTocBuild = () => {
  const box = document.getElementById('set-toc-build'); if (box) box.style.display = 'block';
  // 默认把当前页填进起始，方便用户翻到目录页时直接建
  try { const s = document.getElementById('set-toc-start'); if (s && !s.value) s.value = currentPage; } catch (_) {}
};
window.buildToc = async () => {
  const s = parseInt(document.getElementById('set-toc-start')?.value),
        e = parseInt(document.getElementById('set-toc-end')?.value);
  const btn = document.getElementById('set-toc-btn'), st = document.getElementById('set-toc-status');
  if (!(s >= 1) || !(e >= s)) { _toast?.('请填合法的起止 PDF 页（end ≥ start）'); return; }
  if (e - s > 30) { _toast?.('目录范围最多 30 页'); return; }
  if (btn) { btn.disabled = true; btn.textContent = '识别中…（约几十秒）'; }
  try {
    const r = await (await fetch('/pdf/api/build-toc', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({file: FILE_REL, start: s, end: e})
    })).json();
    if (!r.ok) { _toast?.('建立失败：' + (r.error || '')); if (btn) { btn.disabled = false; btn.textContent = '建立目录'; } return; }
    // 轮询 job 状态
    let tries = 0;
    const poll = async () => {
      tries++;
      try {
        const d = await (await fetch('/pdf/api/build-toc-status?jid=' + r.jid)).json();
        if (d.status === 'done') {
          if (btn) { btn.disabled = false; btn.textContent = '建立目录'; }
          _toast?.('目录已建立（' + d.count + ' 条）'); loadTocStatus(); return;
        }
        if (d.status === 'error') {
          if (btn) { btn.disabled = false; btn.textContent = '建立目录'; }
          if (st) st.textContent = '建立失败：' + (d.error || ''); return;
        }
        if (st) st.textContent = (d.step || '处理中…') + '（' + tries + '）';
      } catch (_) {}
      if (tries < 90) setTimeout(poll, 2000);
      else { if (btn) { btn.disabled = false; btn.textContent = '建立目录'; } if (st) st.textContent = '超时，请重试'; }
    };
    setTimeout(poll, 2000);
  } catch (ex) { _toast?.('建立失败：' + ex.message); if (btn) { btn.disabled = false; btn.textContent = '建立目录'; } }
};
window.closeSettings = () => { document.getElementById('settings-mask').style.display = 'none'; };
window.saveSettings = async () => {
  window.dlog?.('saveSettings 开始');
  try {
    // (2026-07 收口)不再写 'pdf-ai-overrides':模型选择统一走服务端 action 预设;顺手清掉存量旧键
    try { localStorage.removeItem('pdf-ai-overrides'); } catch (_) {}
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
    const ao = document.getElementById('set-auto-orient')?.checked;
    if (ao !== undefined) {
      try { localStorage.setItem('pdf-auto-orient', ao ? '1' : '0'); } catch (_) {}
      if (ao) window._rememberOrientLayout?.();   // 刚开启 → 把当前布局记进当前方向作基线
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
// 通用网页搜索：选中内容开新标签页搜(不占 AI 额度、不需要后端参与)
window.onSearchSel = () => {
  const t = (lastSelText || '').trim();
  if (!t) { _toast?.('没有选中内容'); return; }
  window.open('https://www.bing.com/search?q=' + encodeURIComponent(t), '_blank');
};
// 选区重新识别：文字层坏掉(乱码/上标错/缺符号)时，把选区裁图发 Claude 视觉，拿回正确文字，
// 回填到 lastSelText + 预览 → 之后 复制/翻译/解释/对话 全用校正后的正确文字。
// 选中所在页(选区 char 层属于哪个 page-wrap)。连续滚动下视口居中页 currentPage ≠ 选中页,
// 凡「拿选中位置做事」(翻译浮层贴页/OCR 校正存页/笔记深链/查词日志页)都该用它,否则跨页选时定位到错页。
function _selPageNum() {
  const pw = _charSel && _charSel.pw;
  const n = pw && pw.dataset && parseInt(pw.dataset.pageNum, 10);
  return (n && n > 0) ? n : currentPage;
}
window._selPageNum = _selPageNum;

window.onOcrSel = async () => {
  const pw = _charSel && _charSel.pw;
  if (!pw || !pw.__charBoxes || !lastSelText) { (typeof _toast === 'function') && _toast('先选中文字'); return; }
  const chars = pw.__charBoxes;
  let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9, n = 0;   // 选区并集 bbox(PDF pt)
  for (let i = _charSel.startIdx; i <= _charSel.endIdx; i++) {
    const c = chars[i];
    if (!c || c.sp || c._x0 == null) continue;
    x0 = Math.min(x0, c._x0); y0 = Math.min(y0, c._y0);
    x1 = Math.max(x1, c._x1); y1 = Math.max(y1, c._y1); n++;
  }
  if (!n) { (typeof _toast === 'function') && _toast('选区无效'); return; }
  const selPage = _selPageNum();   // 选中所在页(不能用 currentPage,见 _selPageNum 注释)
  const prev = document.getElementById('sel-preview');
  const old = prev ? prev.innerHTML : '';
  if (prev) prev.innerHTML = '🔎 OCR 识别中…';
  try {
    const ov = _getAiOverrides();
    const r = await __safeFetch('/pdf/api/ocr-selection', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({file: FILE_REL, page: selPage, bbox: [x0, y0, x1, y1],
                            model: ov.model || '', effort: ov.effort || ''}),
    });
    const j = await r.json();
    if (j && j.ok && j.text) {
      lastSelText = j.text;                     // 校正后的文字回填,下游(复制/翻译/解释/对话)全用它
      if (prev) prev.innerHTML = _esc(j.text) + ' <span class="len">OCR ✓ 已写入</span>';
      // 持久化已落库:更新本页 cv(让重渲第一拉就命中新版)+ 立即重渲本页 → 校正注入字符层、永久生效
      if (j.cv) { try { localStorage.setItem('pdf-cv:' + FILE_REL + ':' + selPage, j.cv); } catch (_) {} }
      if (typeof _rerenderLoadedPages === 'function') { try { _rerenderLoadedPages(); } catch (_) {} }
      (typeof _toast === 'function') && _toast('已 OCR 校正并永久写入页面');
    } else {
      if (prev) prev.innerHTML = old;
      (typeof _toast === 'function') && _toast((j && j.error) || 'OCR 失败');
    }
  } catch (e) {
    if (prev) prev.innerHTML = old;
    (typeof _toast === 'function') && _toast('OCR 失败:' + (e && e.message || e));
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
  // 跟选中预览 _selByCharRange 用**同款块过滤**：排序后选区首尾之间会交错进别气泡/别栏的字，
  // 翻译/解释必须只取起止块区间内的字（= 预览所见），否则译文混进左右气泡内容。
  const _blk = (c) => (c.bk != null && c.bk >= 0) ? c.bk : ((c.w == null || c.w < 0) ? -1 : Math.floor(c.w / 1000000));
  const _sb = _blk(chars[sIdx]), _eb = _blk(chars[eIdx]);
  const _bLo = Math.min(_sb, _eb), _bHi = Math.max(_sb, _eb);
  const _inBlk = (c) => { if (_sb < 0 || _eb < 0) return true; const b = _blk(c); return b < 0 || (b >= _bLo && b <= _bHi); };
  const rects = []; let cur = null, firstC = null, lastC = null, text = '';
  for (let i = sIdx; i <= eIdx; i++) {
    const c = chars[i];
    if (!_inBlk(c)) continue;   // 别块(别气泡/别栏)字符不计入，跟预览严格一致
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

// 工具条动作统一从当前 RC adapter/SelectionController 读取。PDF adapter 仍返回原来的
// char-layer snapshot；WebAdapter 返回外部 iframe 通过唯一 bridge 写入的真实模块选区。
function _selectionForToolbarAction() {
  try {
    const controller = window.__bwSelectionController;
    const selection = controller && controller.current && controller.current();
    if (selection && selection.text) return selection;
  } catch (_) {}
  return { text: lastSelText || '', context: '', rect: null, anchor: null };
}
function _adapterForToolbarAction() {
  try { return window.RC && RC.adapter ? RC.adapter() : null; }
  catch (_) { return null; }
}

window.onTranslate = async () => {
  const selection = _selectionForToolbarAction();
  const selectedText = String(selection.text || '').trim();
  const adapter = _adapterForToolbarAction();
  if (!selectedText) return;
  if (window.__uiShared && adapter && typeof adapter.translate === 'function') {   // 共享模式按当前宿主分流；PDF 行为不变
    toolbar.classList.remove('open');
    _resultContext = adapter.kind === 'pdf' && _charSel
      ? { charSel: {pw: _charSel.pw, startIdx: _charSel.startIdx, endIdx: _charSel.endIdx}, text: selectedText, sentence: selectedText, kind: 'translate' }
      : null;
    adapter.translate({
      text: selectedText,
      context: String(selection.context || ''),
      fallback: () => aiCall('/pdf/api/translate', {text: selectedText, target_lang: '中文'}, '🌐 翻译')
    });
    return;
  }
  toolbar.classList.remove('open');
  const pw = _charSel && _charSel.pw;
  // 无 char 信息(罕见) → 退回大框 AI 翻译
  if (!pw || !pw.__charBoxes) {
    aiCall('/pdf/api/translate', {text: selectedText, target_lang: '中文'}, '🌐 翻译');
    return;
  }
  // 选中句 → 生成句子标记(L框/box)，box 呼吸表示翻译中；译完存 sidecar + 自动弹译文浮层
  const sent = _buildSentenceFromSel(pw, _charSel.startIdx, _charSel.endIdx);
  if (!sent) { aiCall('/pdf/api/translate', {text: selectedText, target_lang: '中文'}, '🌐 翻译'); return; }
  // **所译严格 = 预览(所选)文本**：sent.rects 只用作译文覆盖位置(geometry),要翻译的文字一律
  // 用工具栏预览那串 lastSelText，杜绝「翻译内容跟预览不一致」(_buildSentenceFromSel 重拼可能分歧)。
  { const _pv = selectedText.replace(/\s+/g, ' ').trim(); if (_pv) sent.text = _pv; }
  sent.__translating = true;
  pw.__vocabSentences = (pw.__vocabSentences || []).filter(s => s.text !== sent.text);
  pw.__vocabSentences.push(sent);
  renderVocabSentences(pw, pw.__vocabSentences);   // 呼吸 box
  try {
    const ov = _getAiOverrides();
    const r = await __safeFetch('/pdf/api/translate-sentence', {   // 幂等翻译:切后台被掐→回前台自动重试
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        text: sent.text, model: ov.model || '', effort: ov.effort || '',
        file: FILE_REL, sentence: {rects: sent.rects, first_char: sent.first_char, last_char: sent.last_char, page: _selPageNum()},
      }),
    }, {retries: 2});
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
  const selection = _selectionForToolbarAction();
  const selectedText = String(selection.text || '').trim();
  const adapter = _adapterForToolbarAction();
  if (!selectedText) return;
  toolbar.classList.remove('open');
  let context = adapter && adapter.kind !== 'pdf' ? String(selection.context || '') : '';
  let explainText = selectedText;   // 给 AI 解释的主体；短选区时换成所在整句
  if (_charSel && _charSel.pw && _charSel.pw.__charBoxes) {
    const chars = _charSel.pw.__charBoxes;
    const sLen = selectedText.length;
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
    text: selectedText, sentence: explainText, kind: 'explain',
  } : null;
  if (window.__uiShared && adapter && typeof adapter.explain === 'function') {   // 共享模式按当前宿主分流；PDF 行为不变
    adapter.explain({ text: explainText, context, fallback: () => aiCall('/pdf/api/explain', {text: explainText, context}, '💡 AI 解释') });
    return;
  }
  // 解释**不开面板**:选区建一个一直闪烁的琥珀高亮,AI 后台跑;点高亮才开解释页 + 移除高亮(一次点击)。
  const _ehl = (typeof _showExplainHighlight === 'function') ? _showExplainHighlight(_charSel && _charSel.pw, selectedText) : null;
  if (!_ehl) { aiCall('/pdf/api/explain', {text: explainText, context}, '💡 AI 解释'); return; }   // 建不了高亮(罕见)→退回旧式直接开面板
  _ehl.title = '💡 AI 解释'; _ehl.src = explainText; _ehl.resultContext = _resultContext;
  _runExplainBg(_ehl, explainText, context);
};
// 后台跑解释:不开面板,把渲染好的 HTML 缓存进高亮;若用户中途点了高亮开了加载面板(panelReqId 匹配),实时/收尾填充
async function _runExplainBg(hl, text, context) {
  const ov = _getAiOverrides();
  const body = {text, context};
  if (ov.model)  body.model  = ov.model;
  if (ov.effort) body.effort = ov.effort;
  // 共享模式(__uiShared):结果框由 rc-result 管,须比对它自己的计数器(window._resultReqId)+ 调它导出的
  // addResultPickers,否则本文件裸的 _resultReqId/addResultPickers 是另一份独立状态,两边互不作废
  // 对方在途请求(见 _reopenExplain 同一处注释)。非共享模式逐字不变。
  const _shared = window.__uiShared && window.PdfAdapter;
  const _curReqId = () => (_shared ? window._resultReqId : _resultReqId);
  const _fillPanel = (innerHtml, pickers) => {
    if (hl.panelReqId == null || hl.panelReqId !== _curReqId()) return;   // 用户没点开等 / 已开别的结果 → 不填
    const el = document.getElementById('result-content'); if (!el) return;
    el.innerHTML = innerHtml;
    if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([el]).catch(() => {});
    if (pickers) { try { _resultContext = hl.resultContext; } catch (_) {} try { (_shared ? window.addResultPickers : addResultPickers)?.(); } catch (_) {} }
  };
  let acc = '';
  try {
    const res = await _aiStream('/pdf/api/explain', { method: 'POST', body, onText: (t) => { acc = t; _fillPanel(md(t || ' '), false); } });
    if (hl.canceled) return;   // 被新解释替换 → 丢弃
    const full = res.text || acc;
    hl.html = md(full || ' ') + (res.ok ? '' : '<div style="color:#c00;margin-top:8px">✗ ' + (res.error || '失败') + '</div>');
    hl.ready = true;
    _fillPanel(hl.html, true);
  } catch (e) {
    if (hl.canceled) return;
    hl.html = '<div style="color:#c00">✗ ' + (e.message || '失败') + '</div>'; hl.ready = true;
    _fillPanel(hl.html, false);
  }
}
// 多词选中「💬 对话」：开对话框，预填原文 + 句子/段落上下文(也作 AI 上下文)，底部追问框多轮问
window.onChat = () => {
  const selection = _selectionForToolbarAction();
  const selectedText = String(selection.text || '').trim();
  const adapter = _adapterForToolbarAction();
  if (!selectedText) return;
  toolbar.classList.remove('open');
  let context = adapter && adapter.kind !== 'pdf' ? String(selection.context || '') : '';
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
    text: selectedText, sentence: context || selectedText, kind: 'chat',
  } : null;
  if (window.__uiShared && adapter && typeof adapter.chat === 'function') {   // 共享模式按当前宿主分流；PDF 行为不变
    adapter.chat({ text: selectedText, context, fallback: () => _openChat(selectedText, context) });
    return;
  }
  _openChat(selectedText, context);
};
// 对话面板打开(原 onChat 尾段逐字搬入;参数名沿用 lastSelText/context 保持函数体不变)
function _openChat(lastSelText, context) {
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
}
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
      body: JSON.stringify({text: lastSelText, name: trimmed, file: FILE_REL, page: _selPageNum()}),
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

// ──────── 文字层校准(扫描/OCR 书:文字层没对齐时手动微调 + 可视化文字框) ────────
window._charOfsByPage = window._charOfsByPage || {};

// 重渲已加载页(图走 decode-first 缓存→快;会重新拉 page-chars 应用新偏移 + 重画/撤红框)
function _rerenderLoadedPages() {
  const pc = document.getElementById('page-container');
  if (!pc) return;
  let wraps = [...pc.querySelectorAll('.page-wrap[data-loaded="1"]')];
  if (!wraps.length && pc.dataset.loaded === '1') wraps = [pc];   // 单页模式:容器即 wrap
  wraps.forEach(w => {
    w.dataset.loaded = '0';
    const num = parseInt(w.dataset.pageNum || currentPage, 10);
    if (typeof _renderPageInto === 'function') _renderPageInto(num, w).catch(() => {});
  });
}

window._charboxToggle = (cb) => {
  try { localStorage.setItem('pdf-charbox', (cb && cb.checked) ? '1' : '0'); } catch (_) {}
  _rerenderLoadedPages();   // 红框立即出现/消失
};

async function _fetchCharOfs(page) {
  try {
    const r = await fetch(`/pdf/api/char-offset?file=${encodeURIComponent(FILE_REL)}&page=${page}`);
    const d = await r.json();
    if (d.ok) return d.offset;
  } catch (_) {}
  return { dx: 0, dy: 0, scale: 1 };
}
async function _saveCharOfs(page, ofs) {
  try {
    const r = await fetch('/pdf/api/char-offset', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file: FILE_REL, page, dx: ofs.dx, dy: ofs.dy, scale: ofs.scale }),
    });
    const d = await r.json();
    if (d && d.cv) { try { localStorage.setItem('pdf-cv:' + FILE_REL + ':' + page, d.cv); } catch (_) {} }  // 偏移变→更新 cv→重渲即取新(不靠 backstop)
    return d;
  } catch (_) { return null; }
}
function _charOfsStep() {
  const v = parseFloat(document.getElementById('charofs-step')?.value);
  return (isFinite(v) && v > 0) ? v : 2;
}
function _updateCharOfsLabel(page, ofs) {
  const el = document.getElementById('charofs-cur');
  if (el) el.textContent = `第 ${page} 页　dx ${(+ofs.dx).toFixed(1)} · dy ${(+ofs.dy).toFixed(1)}` +
    ((+ofs.scale !== 1) ? ` · ×${(+ofs.scale).toFixed(2)}` : '');
}
// dirx/diry: -1/0/1。注意 PDF y 向下为正 → "下移文字"= dy 增大
window._nudgeChars = async (dirx, diry) => {
  const page = currentPage;
  let ofs = window._charOfsByPage[page] || await _fetchCharOfs(page);
  const step = _charOfsStep();
  ofs = { dx: (ofs.dx || 0) + dirx * step, dy: (ofs.dy || 0) + diry * step, scale: ofs.scale || 1 };
  window._charOfsByPage[page] = ofs;
  _updateCharOfsLabel(page, ofs);
  await _saveCharOfs(page, ofs);
  _rerenderLoadedPages();
};
window._resetCharOffset = async () => {
  const page = currentPage;
  const ofs = { dx: 0, dy: 0, scale: 1 };
  window._charOfsByPage[page] = ofs;
  _updateCharOfsLabel(page, ofs);
  await _saveCharOfs(page, ofs);
  _rerenderLoadedPages();
};
window._initCharOfsPanel = async () => {
  const cb = document.getElementById('set-charbox');
  if (cb) { try { cb.checked = localStorage.getItem('pdf-charbox') === '1'; } catch (_) {} }
  const st = document.getElementById('reocr-status'); if (st) st.textContent = '';
  const ofs = await _fetchCharOfs(currentPage);
  window._charOfsByPage[currentPage] = ofs;
  _updateCharOfsLabel(currentPage, ofs);
};

// 单页重扫:对当前页用 Google Vision 重新 OCR → 覆盖文字层(修识别错/漏/整页歪)
window._reocrPage = async () => {
  const btn = document.getElementById('reocr-btn');
  const st = document.getElementById('reocr-status');
  if (btn) btn.disabled = true;
  if (st) st.textContent = '⏳ 重扫第 ' + window._dispPage(currentPage) + ' 页…(Google Vision,几秒)';
  try {
    const r = await fetch('/pdf/api/reocr-page', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file: FILE_REL, page: currentPage }),
    });
    const d = await r.json();
    if (d.cv) { try { localStorage.setItem('pdf-cv:' + FILE_REL + ':' + currentPage, d.cv); } catch (_) {} }  // 重扫后 cv 更新→重渲直接取新覆盖
    if (d.ok && d.chars > 0) {
      if (st) st.textContent = '✓ 第 ' + window._dispPage(currentPage) + ' 页重扫完成(' + d.chars + ' 字)';
      _rerenderLoadedPages();   // cv 变 → 重渲拿新文字层
    } else if (d.ok) {
      if (st) st.textContent = '⚠ 未识别到文字(空白页或扫描质量差)';
    } else if (st) { st.textContent = '✗ ' + (d.error || '失败'); }
  } catch (e) { if (st) st.textContent = '✗ 网络失败'; }
  finally { if (btn) btn.disabled = false; }
};
window._clearReocr = async () => {
  const st = document.getElementById('reocr-status');
  if (st) st.textContent = '撤销中…';
  try {
    const r = await fetch('/pdf/api/reocr-page/clear', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file: FILE_REL, page: currentPage }),
    });
    const d = await r.json();
    if (d.cv) { try { localStorage.setItem('pdf-cv:' + FILE_REL + ':' + currentPage, d.cv); } catch (_) {} }  // 撤销后 cv 更新→重渲取回原文字层
    if (st) st.textContent = d.cleared ? ('✓ 已撤销第 ' + window._dispPage(currentPage) + ' 页重扫') : '该页无重扫记录';
    _rerenderLoadedPages();
  } catch (e) { if (st) st.textContent = '✗ 网络失败'; }
};

// 键盘快捷键
document.addEventListener('keydown', (e) => {
  // 焦点在任何可输入控件(含侧栏 AI 的 textarea / 可编辑区)时,左右键给光标用,别翻页
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.isContentEditable) return;
  if (e.key === 'ArrowRight' || e.key === 'PageDown' || e.key === ' ') { e.preventDefault(); changePage(1); }
  else if (e.key === 'ArrowLeft' || e.key === 'PageUp') { e.preventDefault(); changePage(-1); }
  else if (e.key === 'Escape') { closeResult(); toolbar.classList.remove('open'); }
});

// 顶栏 🎙 语音通话入口:打开豆包实时语音通话页,带上正在读的书+当前页(relay 会把本页内容注入通话上下文)。
// 内联 onclick 拿不到模块作用域的 FILE_REL/currentPage → 挂 window(项目惯例,同 _noteCreateAtCenter)。
window._voiceCall = () => {
  if (window.RC && RC.voicecall) {   // 语音输入模式(agent:说话=问侧栏助手,转写进输入框,回答文字为主)
    RC.voicecall.toggle({ file: FILE_REL || '', page: currentPage || 1 });
    return;
  }
  // 兜底:模块没加载 → 独立通话页(纯聊天,无页面控制)
  window.open('/static/pdf/voice-call.html?file=' + encodeURIComponent(FILE_REL || '') + '&page=' + (currentPage || 1), '_blank');
};
// S2S 伴读通话(长按侧栏 📞 触发):端到端语音对话,带本页上下文+翻页热同步;说"找视频/翻到第N页"
// 由 relay 解析模型回复的协议句式真执行。适合不看屏幕的场景;屏前日常用短按的 agent 模式。
window._voiceCallS2S = () => {
  if (window.RC && RC.voicecall) RC.voicecall.toggle({ mode: 's2s', file: FILE_REL || '', page: currentPage || 1 });
};
// 通话中翻页 → 同步给 relay 热更新豆包的页面上下文(否则它一直停在开通话那页;currentPage 是模块变量,
// 浮层拿不到 → 由本文件定时读。仅通话开着时发,setPage 内部去重)。
// 通话上下文采集(页码/墨迹/选中chip):2s 轮询兜底 + **用户开口瞬间立即同步一次**(rc-voicecall 在 450 事件调,
// 赶在模型处理这句话之前把最新状态推上去——治"画完立刻问,模型拿着旧状态答『没变化』"的竞态)。
window.__vcSyncNow = () => {
  try {
    if (!(window.RC && RC.voicecall && RC.voicecall.isOpen())) return;
    if (RC.voicecall.setPage) RC.voicecall.setPage(currentPage || 0);
    if (RC.voicecall.syncInk) {
      var _st = (window._ink && _ink.byPage && _ink.byPage[currentPage]) || [];
      RC.voicecall.syncInk(currentPage || 0, _st);
    }
    if (RC.voicecall.syncState && typeof window.__voiceContext === 'function') {
      var _vc = window.__voiceContext();
      RC.voicecall.syncState({
        sel: String(_vc.selection || '').slice(0, 300),
        focus: (_vc.focus_sel && _vc.focus_sel.text) ? String(_vc.focus_sel.text).slice(0, 200) : '',
        figs: (_vc.figures || []).length,
      });
    }
  } catch (_) {}
};
setInterval(() => { window.__vcSyncNow(); }, 2000);

loadPdf();
// ── 整本预热:按当前显示宽度把全书(背景图 + 字符/振假名)渲好缓存 → 翻页秒开。──
// 手动「📥 预热」按钮 + 开书自动后台预热(客户端自己解决,不用手动管)。后端 /api/prewarm-async 跑
// scripts/prewarm_pdf.py(detached,关网页不中断);进度按已缓存页图张数算,显示在按钮上。
// 宽度复刻 _renderPageImg 的 reqW(clamp(page_w×scale×dpr,400,2400))→ 预热的宽度=阅读器实际请求的宽度,不会错配。

function _prewarmWidth() {
  // 与 _renderPageImg 解耦后的基准一致:页**原生点宽**(与显示无关)。预热这个宽度=覆盖任意窗口的适应阅读。
  const pw = Math.round((window.__imgMeta && window.__imgMeta.page_w) || 0);
  return Math.max(400, Math.min(2400, pw || 1260));
}

let _prewarmPoll = null;
function _prewarmTrack(w) {
  const btn = document.getElementById('prewarm-btn');
  if (_prewarmPoll) clearInterval(_prewarmPoll);
  _prewarmPoll = setInterval(async () => {
    let st;
    try { st = await (await fetch('/pdf/api/prewarm-status?file=' + encodeURIComponent(FILE_REL) + '&width=' + w)).json(); }
    catch (_) { return; }
    if (!st || !st.ok) return;
    if (btn) btn.textContent = (st.percent >= 100) ? '📥 预热' : ('📥 ' + Math.floor(st.percent) + '%');
    if (st.percent >= 100 || (!st.running && st.percent > 0)) {
      clearInterval(_prewarmPoll); _prewarmPoll = null;
      if (btn) btn.textContent = '📥 预热';
      if (st.percent >= 100) _toast?.('整本预热完成 ✓ 翻页秒开');
    }
  }, 2500);
}

window._prewarmBook = async function (manual) {
  if (!window._imgMode || !window.__imgMeta || !scale) return;   // 仅图片模式
  const w = _prewarmWidth();
  let st;
  try { st = await (await fetch('/pdf/api/prewarm-status?file=' + encodeURIComponent(FILE_REL) + '&width=' + w)).json(); }
  catch (_) { return; }
  if (!st || !st.ok) return;
  if (!manual && st.percent >= 95) return;          // 自动:已基本预热好 → 不重复启
  if (st.running) { _prewarmTrack(w); return; }      // 已在跑 → 只跟进度
  try {
    await fetch('/pdf/api/prewarm-async', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file: FILE_REL, width: w }),
    });
  } catch (_) { return; }
  if (manual) _toast?.('开始预热整本（后台渲染，完成后翻页秒开）');
  _prewarmTrack(w);
};

// 开书后自动后台预热(默认开;localStorage pdf-auto-prewarm='0' 可关)。等首页渲完(scale/宽度已定)触发一次。
let _autoPrewarmDone = false;
window._maybeAutoPrewarm = function () {
  if (_autoPrewarmDone) return;
  if (localStorage.getItem('pdf-auto-prewarm') === '0') { _autoPrewarmDone = true; return; }
  if (!window._imgMode || !window.__imgMeta || !scale) return;
  _autoPrewarmDone = true;
  setTimeout(() => { try { window._prewarmBook(false); } catch (_) {} }, 1500);
};
// ── 23-bookshelf.js:已退役(2026-06-18)──
// 原来这里是「阅读器内浮层书单」(返回书架=零跳转秒开的临时选择层)。用户嫌它临时,改成
// 返回书架直接进**完整书架页 /pdf/**(正经书库:压缩/预处理/预热/🧮公式识别 都在那)。
// goPdfList 已改为 location.href='/pdf/'(见 03-loader.js)→ 本浮层不再被调用,整体移除。
// 公式 OCR 按钮迁到 templates/pdf_index.html(formulaOCR,走 /pdf/api/formula-ocr,Claude 视觉无需 PC)。
// ── 连接质量指示器(2026-06-10):工具栏小圆点 🟢直连 / 🟡慢(中继/弱网) / 🔴断 ──
// 背景:iPad Tailscale 掉中继(relay 绕东京)时整站变慢数秒,但应用零线索,用户只能怀疑代码。
// 每 30s(+页面回前台时)对 /api/ping 量一次 RTT;点圆点弹 toast 显示毫秒数与判级。
// 纯归因用,不做任何降级逻辑。
let _connDot = null, _connMs = -1;

function _connClass(ms) {
  if (ms < 0) return 'r';
  if (ms < 120) return 'g';     // 直连典型 <80ms(Tailscale 私网)
  if (ms < 450) return 'y';     // 中继/弱网
  return 'r';
}

async function _connProbe() {
  let ms = -1;
  try {
    const t0 = performance.now();
    const r = await fetch('/pdf/api/ping?_=' + Date.now(), { cache: 'no-store' });
    if (r.ok) ms = Math.round(performance.now() - t0);
  } catch (_) {}
  _connMs = ms;
  if (_connDot) {
    _connDot.className = 'conn-dot ' + _connClass(ms);
    _connDot.title = ms < 0 ? '服务器不可达' :
      `网络 ${ms}ms · ${ms < 120 ? '直连,正常' : ms < 450 ? '偏慢(可能 Tailscale 走中继/弱网)' : '很慢(中继/网络差)'}`;
  }
}

(() => {
  const tb = document.getElementById('header');
  if (!tb) return;
  _connDot = document.createElement('span');
  _connDot.className = 'conn-dot g';
  _connDot.onclick = () => {
    const ver = (typeof READER_BUILD !== 'undefined' ? READER_BUILD : '?');
    // 实时状态诊断:模式/缩放/页元素数(出 bug 时点圆点把这串报给开发,直接看到 iPad 上的真实状态)
    let diag = '';
    try {
      const rm = (typeof readMode !== 'undefined') ? readMode : '?';
      const sc = (typeof scale !== 'undefined') ? Math.round(scale * 100) / 100 : '?';
      const imgs = document.querySelectorAll('.page-img').length;
      const wraps = document.querySelectorAll('.page-wrap').length;
      const pc = document.getElementById('page-container');
      const z = pc ? (getComputedStyle(pc).zoom || '1') : '?';
      const t = pc && pc.style.transform ? 'T' : '-';
      diag = `\n${rm} ×${sc} img${imgs} wrap${wraps} z${z}${t}`;
    } catch (_) {}
    _toast?.((_connMs < 0 ? '🔴 不可达' :
      `${_connClass(_connMs) === 'g' ? '🟢' : _connClass(_connMs) === 'y' ? '🟡' : '🔴'} ${_connMs}ms`) +
      '  ' + ver + diag);
    _connProbe();
  };
  tb.appendChild(_connDot);
  _connProbe();
  setInterval(_connProbe, 30000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) _connProbe(); });
})();
// ── 25-assistant.js:PDF 阅读器侧边栏 Copilot(放进现有右侧抽屉 #grammar-panel 的一个 tab)──
// 输入框用系统键盘(iOS 自带听写麦克风=语音输入,零自造 STT)。走 /api/assistant/chat(SSE):
// agent 自己调工具(读页/搜索/翻译/制卡/跳页…)解复合请求。复用 reader 的 md()+MathJax 渲染答案。
// 🤖 fab 一键开抽屉到「助手」tab;快捷按钮(翻页/缩放)直调 window 函数 0 延迟。
(function () {
  if (window.__asstLoaded) return;
  if (window.__uiShared) return;   // ②b 收尾:shared 模式(默认)侧栏由 rc-assistant.js 的 mountPdfSidebar 接管;这份老实现只在 legacy(?ui=legacy,无 __uiShared)兜底
  var panelEl = document.getElementById('grammar-panel');
  var tabsEl = document.getElementById('side-tabs');
  if (!panelEl || !tabsEl) return;   // 抽屉不在(非阅读器页)就不挂
  window.__asstLoaded = true;

  var streaming = false;   // 对话历史由服务端保存,前端不再持本地数组

  // ── tab 注入(放第一个,最显眼)──
  var tabBtn = document.createElement('button');
  tabBtn.className = 'side-tab'; tabBtn.dataset.pane = 'asst';
  // Apple/SF「sparkles」图标(替代 🤖 emoji),复用模板 .si 样式
  tabBtn.innerHTML = '<svg class="si" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4l1.4 4.2L18 9.6l-4.6 1.4L12 16l-1.4-4.6L6 9.6l4.6-1.4L12 4z"/><path d="M18.6 14.5l.6 1.7 1.7.6-1.7.6-.6 1.7-.6-1.7-1.7-.6 1.7-.6.6-1.7z"/></svg>助手';
  tabBtn.onclick = function () { window.switchSideTab && window.switchSideTab('asst'); setTimeout(function () { ta && ta.focus(); }, 200); };
  tabsEl.insertBefore(tabBtn, tabsEl.firstChild);

  // ── pane 注入 ──
  var pane = document.createElement('div');
  pane.className = 'side-pane'; pane.dataset.pane = 'asst'; pane.id = 'side-pane-asst';
  // 快捷栏:共享构建器 rcBuildQuickBar(在则空容器等它填,与 EPUB 同一份来源 → 按钮永不分叉;
  //   历史「总结本页/本页生词」不再纳入)。legacy 模式(rc-assistant 未加载)→ native 兜底同款三按钮。
  var _quickNative = window.rcBuildQuickBar ? '' :
      '<button data-q="clear">🗑 清空</button>' +
      '<button data-q="models">⚙ 模型</button>';
  pane.innerHTML =
    '<div id="asst-thread"></div>' +
    '<div id="asst-quick">' + _quickNative + '</div>' +
    '<div id="asst-input">' +
      '<button id="asst-mic" title="语音输入"><svg viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.93V22h2v-3.07A7 7 0 0 0 19 12h-2z"/></svg></button>' +
      '<textarea id="asst-ta" rows="1" placeholder="问这本书 / 让我帮你…"></textarea>' +
      '<button id="asst-send" title="发送">➤</button></div>';
  panelEl.appendChild(pane);
  try {
    var _qb = document.getElementById('asst-quick');
    if (window.rcBuildQuickBar) window.rcBuildQuickBar(_qb, {});
    else if (window.rcBuildMediaRow) window.rcBuildMediaRow(_qb);   // legacy:至少并上「配图/视频」媒体行
  } catch (e) {}

  var css = document.createElement('style');
  css.textContent =
    '#asst-fab{position:fixed;right:14px;bottom:90px;z-index:115;width:50px;height:50px;border-radius:50%;border:none;' +
    'background:#2563eb;color:#fff;font-size:24px;box-shadow:0 6px 18px rgba(0,0,0,.4);cursor:pointer;display:flex;align-items:center;justify-content:center;-webkit-tap-highlight-color:transparent}' +
    '#asst-fab:active{transform:scale(.92)}' +
    '#side-pane-asst.active{display:flex;flex-direction:column;overflow:hidden;height:100%}' +
    '#asst-thread{flex:1 1 auto;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:10px;-webkit-overflow-scrolling:touch;min-height:0;overscroll-behavior:contain;touch-action:pan-y}' +   // contain+pan-y:滚到头不把滚动链漏给底下 PDF(否则阅读器在浮层下偷偷滚→IO 渲页=卡)
    '.asst-msg{max-width:92%;padding:9px 12px;border-radius:13px;font-size:14px;line-height:1.55;word-break:break-word}' +
    '.asst-u{align-self:flex-end;background:#1d4ed8;color:#fff;border-bottom-right-radius:4px}' +
    '.asst-a{align-self:flex-start;background:#161d31;border:1px solid #243152;border-bottom-left-radius:4px}' +
    '.asst-a p{margin:.4em 0}.asst-a ul,.asst-a ol{margin:.3em 0;padding-left:1.3em}.asst-a code{background:#0b1220;padding:1px 4px;border-radius:4px}' +
    '.asst-a h1,.asst-a h2,.asst-a h3{font-size:1em;margin:.5em 0 .2em}' +
    /* 内容图给浅色画布 matte:助手气泡恒深底(#161d31),透明底 SVG/图的黑轴黑字看不清 → 白底救场(同 rc-result;MathJax=chtml 无 svg 不误伤) */
    '.asst-a img,.asst-a svg{max-width:100%;height:auto;border-radius:8px;display:block;margin:.4em auto;background:#fff;padding:10px;box-sizing:border-box}' +
    '.asst-a img{cursor:zoom-in}' +
    '.asst-tool{align-self:flex-start;color:#7c93c4;font-size:12px;padding:2px 6px;font-style:italic}' +
    '.asst-note{align-self:center;background:#2a2410;border:1px solid #5a4a18;color:#e7d28a;font-size:12px;padding:4px 10px;border-radius:9px;max-width:96%}' +
    '.asst-undo{background:#3a1d2a;border:1px solid #6b3550;color:#ffd0e0;border-radius:7px;padding:2px 8px;font-size:12px;cursor:pointer;margin-left:6px}' +
    '.asst-undo:active{background:#52283a}.asst-undo:disabled{opacity:.5}' +
    '.asst-jump{background:#16293a;border:1px solid #2a4a63;color:#bce0ff;border-radius:7px;padding:2px 8px;font-size:12px;cursor:pointer;margin-left:6px}' +
    '.asst-jump:active{background:#1d3a52}' +
    '.asst-hl-row{display:flex;align-items:center;gap:6px;padding:5px 6px;border-radius:8px;margin-top:5px;background:#161d33}' +
    '.asst-hl-sw{flex:0 0 auto;width:12px;height:12px;border-radius:3px;border:1px solid #ffffff33}' +
    '.asst-hl-tx{flex:1 1 auto;min-width:0;font-size:12.5px;color:#cdd8f5;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}' +
    '.asst-hl-del{flex:0 0 auto;background:#3a1d1d;border:1px solid #6b3535;color:#ffd0d0;border-radius:7px;padding:2px 8px;font-size:12px;cursor:pointer}' +
    '.asst-hl-del:active{background:#522828}.asst-hl-del:disabled{opacity:.5}' +
    '.asst-hl-redo{flex:0 0 auto;background:#1d3a2a;border:1px solid #2f6347;color:#bfead0;border-radius:7px;padding:2px 8px;font-size:12px;cursor:pointer}.asst-hl-redo:active{background:#244a35}.asst-hl-redo:disabled{opacity:.5}' +   // M9:删完转「↪ 重做」
    '.asst-edit-card{align-self:flex-start;max-width:92%;background:#13203a;border:1px solid #294060;border-radius:11px;padding:8px 11px;display:flex;flex-direction:column;gap:7px}' +
    '.asst-edit-h{font-size:12.5px;color:#bfe0c8}' +
    '.asst-edit-chips{display:flex;flex-wrap:wrap;gap:6px}' +
    '.asst-edit-undo{align-self:flex-start;background:#26344f;border:1px solid #3a5273;color:#dbe7ff;border-radius:8px;padding:3px 12px;font-size:12.5px;cursor:pointer}' +
    '.asst-edit-undo:active{background:#2f4061}.asst-edit-undo:disabled{opacity:.55}' +
    '#asst-quick{flex:0 0 auto;display:flex;flex-wrap:wrap;gap:6px;padding:8px 10px;border-top:1px solid #233156}' +
    '#asst-quick button{background:#16203a;border:1px solid #2a3a63;color:#bcd0ff;border-radius:8px;padding:6px 10px;font-size:13px;cursor:pointer}' +
    '#asst-quick button:active{background:#22305a}' +
    '#asst-quick button.asst-learn{background:#16293a;border-color:#2a4a63;color:#bce0ff}' +   // 学习类按钮:跟导航类区分
    '#asst-send.stop{background:#b23b3b}' +   // 流式中:发送→停止(红)
    // AI 答完的「追问建议」chip
    '.asst-followups{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}' +
    '.asst-fu{background:#13233f;border:1px solid #2a3a63;color:#bcd0ff;border-radius:13px;padding:5px 11px;font-size:13px;cursor:pointer;text-align:left}' +
    '.asst-fu:active{background:#1d3358}' +
    // 每条回答右下角的「!」反馈按钮 + 弹出:显示这条回答经过了哪些 AI 调用(各步模型),再给两个回报动作
    '.asst-fb-bar{position:relative;margin-top:7px;display:flex;justify-content:flex-end;align-items:center}' +
    '.asst-tok{margin-right:auto;font-size:11px;color:#6f7fa3;background:#121a2e;border:1px solid #233156;border-radius:8px;padding:1px 7px}' +
    '.asst-fb-btn{width:22px;height:22px;line-height:20px;text-align:center;border-radius:50%;border:1px solid #2a3a63;background:#0e1525;color:#7c93c4;font-size:13px;font-weight:700;cursor:pointer;padding:0;-webkit-tap-highlight-color:transparent}' +
    '.asst-fb-btn:active{background:#1a2540}' +
    '.asst-fb-pop{position:absolute;right:0;bottom:28px;z-index:20;width:320px;max-width:88vw;background:#0d1426;border:1px solid #2a3a63;border-radius:11px;padding:9px;box-shadow:0 8px 22px rgba(0,0,0,.5);display:flex;flex-direction:column;gap:5px}' +
    '.afp-l-btn{cursor:pointer;text-decoration:underline dotted;text-underline-offset:2px;-webkit-tap-highlight-color:transparent}' +
    '.afp-l-btn:active{opacity:.7}' +
    '.afp-detail{white-space:pre-wrap;word-break:break-word;max-height:260px;overflow:auto;background:#0a1020;border:1px solid #233156;border-radius:8px;padding:8px 10px;margin:2px 0 4px;font-size:11.5px;color:#bcd0ee;line-height:1.55;-webkit-overflow-scrolling:touch}' +
    '.afp-h{font-size:11px;color:#7c93c4;margin-bottom:2px}' +
    '.afp-step{display:flex;align-items:center;gap:7px;font-size:12px;line-height:1.5}' +
    '.afp-l{color:#cdd9f2;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1 1 auto;min-width:0}' +
    '.afp-m{color:#7c93c4;flex:none;font-variant-numeric:tabular-nums}' +
    '.afp-gear-btn{flex:none;background:none;border:none;color:#6b7da0;font-size:13px;cursor:pointer;padding:0 1px;-webkit-tap-highlight-color:transparent}' +
    '.afp-gear-btn:active{color:#bcd0ff}' +
    '.afp-gear{display:flex;flex-wrap:wrap;align-items:center;gap:5px;margin:1px 0 5px;padding:7px;background:#0a1322;border:1px solid #243152;border-radius:8px}' +
    '.afp-glab{font-size:11px;color:#7c93c4;width:100%}' +
    '.afp-sel{background:#0d1426;border:1px solid #2a3a63;color:#dbe7ff;border-radius:6px;padding:3px 5px;font-size:12px;flex:1 1 42%;min-width:0}' +
    '.afp-gset{background:#16293a;border:1px solid #2a4a63;color:#bce0ff;border-radius:6px;padding:4px 9px;font-size:12px;cursor:pointer;flex:1 1 auto}' +
    '.afp-gdef{background:#1a2233;border:1px solid #2a3a63;color:#9fb4e0;border-radius:6px;padding:4px 9px;font-size:12px;cursor:pointer;flex:none}' +
    '.afp-foot{font-size:11px;color:#6b7da0;margin-top:5px;text-align:right;font-variant-numeric:tabular-nums}' +
    '.afp-acts{display:flex;flex-direction:column;gap:5px;margin-top:4px;border-top:1px solid #1d2742;padding-top:7px}' +
    '.afp-act{text-align:left;border:1px solid #2a3a63;border-radius:8px;padding:6px 9px;font-size:12px;cursor:pointer;color:#dbe7ff}' +
    '.afp-q{background:#16293a;border-color:#2a4a63}.afp-q:active{background:#1d3a52}' +
    '.afp-s{background:#1a2233}.afp-s:active{background:#222d44}' +
    '#asst-input{flex:0 0 auto;display:flex;gap:8px;padding:10px;border-top:1px solid #233156;align-items:flex-end}' +
    '#asst-ta{flex:1;background:#0b1220;border:1px solid #2a3a63;color:#e6eeff;border-radius:12px;padding:9px 11px;font-size:15px;resize:none;max-height:120px;line-height:1.4;font-family:inherit}' +
    '#asst-send{background:#2563eb;border:none;color:#fff;width:42px;height:42px;border-radius:12px;font-size:18px;cursor:pointer;flex:none}' +
    '#asst-send:disabled{opacity:.5}' +
    // 苹果风格语音按钮:静默时素净,听写时 iOS 蓝 + 呼吸光环
    '#asst-mic{background:#16203a;border:1px solid #2a3a63;color:#9fb4e0;width:42px;height:42px;border-radius:12px;cursor:pointer;flex:none;display:flex;align-items:center;justify-content:center;transition:background .2s,color .2s,border-color .2s,transform .1s;-webkit-tap-highlight-color:transparent}' +
    '#asst-mic:active{transform:scale(.9)}' +
    '#asst-mic.on{background:#0a84ff;border-color:#0a84ff;color:#fff;animation:asstMicPulse 1.5s ease-in-out infinite}' +
    '@keyframes asstMicPulse{0%,100%{box-shadow:0 0 0 0 rgba(10,132,255,.5)}50%{box-shadow:0 0 0 9px rgba(10,132,255,0)}}' +
    // 用户气泡里的「上下文卡片」:用过的图缩略图 / 选中的字段 / 涉及的页码,均可点击跳转
    '.asst-ctx-card{margin-top:7px;display:flex;flex-direction:column;gap:5px}' +
    '.actx-thumbs{display:flex;flex-wrap:wrap;gap:5px}' +
    '.actx-thumb{width:42px;height:42px;object-fit:cover;border-radius:6px;border:1px solid rgba(255,255,255,.45);background:#fff;cursor:pointer;flex:none}' +
    '.actx-thumb:active{transform:scale(.94)}' +
    '.actx-sel{font-size:12px;color:#dbe7ff;background:rgba(255,255,255,.13);border-left:2px solid rgba(255,255,255,.5);border-radius:4px;padding:3px 7px;cursor:pointer;line-height:1.4}' +
    '.actx-sel:active{background:rgba(255,255,255,.22)}' +
    '.actx-sel.actx-fml{text-align:center;white-space:normal;overflow-x:auto;color:#eaf2ff}' +
    '.actx-page{align-self:flex-start;font-size:11px;color:#eaf2ff;background:rgba(255,255,255,.16);border-radius:9px;padding:2px 9px;cursor:pointer}' +
    '.actx-page:active{background:rgba(255,255,255,.28)}' +
    // ⚙ 模型设置面板(每任务 后端/型号/深度)
    '.ams-mask{position:fixed;inset:0;z-index:130;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;padding:16px}' +
    '.ams-box{background:#0d1426;border:1px solid #2a3a63;border-radius:14px;max-width:440px;width:100%;max-height:86vh;overflow-y:auto;padding:14px 14px 16px;box-shadow:0 12px 40px rgba(0,0,0,.6)}' +
    '.ams-h{font-size:15px;color:#dbe7ff;font-weight:600;display:flex;align-items:center;justify-content:space-between;margin-bottom:3px}' +
    '.ams-x{background:none;border:none;color:#7c93c4;font-size:20px;cursor:pointer;padding:0 4px;line-height:1}' +
    '.ams-sub{font-size:11px;color:#6b7da0;margin-bottom:10px;line-height:1.5}' +
    '.ams-task{background:#0a1322;border:1px solid #243152;border-radius:10px;padding:10px;margin-bottom:9px}' +
    '.ams-tname{font-size:13px;color:#cdd9f2;font-weight:600;margin-bottom:2px}' +
    '.ams-tdef{font-size:11px;color:#6b7da0;margin-bottom:7px}' +
    '.ams-row{display:flex;gap:6px;flex-wrap:wrap;align-items:center}' +
    '.ams-sel{background:#0d1426;border:1px solid #2a3a63;color:#dbe7ff;border-radius:7px;padding:5px 6px;font-size:12px;flex:1 1 28%;min-width:0}' +
    '.ams-sel:disabled{opacity:.45}' +
    '.ams-rst{background:#1a2233;border:1px solid #2a3a63;color:#9fb4e0;border-radius:7px;padding:5px 9px;font-size:12px;cursor:pointer;flex:none}' +
    '.ams-rst:active{background:#222d44}' +
    '.ams-cur{font-size:11px;color:#7c93c4;margin-top:6px}' +
    '.ams-note{font-size:11px;color:#bfae72;background:#221d10;border:1px solid #463a18;border-radius:7px;padding:6px 9px;margin-top:4px;line-height:1.5}';
  document.head.appendChild(css);

  // 🤖 fab:一键开抽屉到助手 tab
  var fab = document.createElement('button');
  fab.id = 'asst-fab'; fab.title = '阅读助手'; fab.textContent = '🤖';
  fab.addEventListener('click', function () {
    try { if (typeof openGrammarPanel === 'function') openGrammarPanel(); } catch (_) {}
    window.switchSideTab && window.switchSideTab('asst');
    prewarm(false);
    try { if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission().catch(function () {}); } catch (_) {}
    setTimeout(function () { ta && ta.focus(); }, 250);
  });
  document.body.appendChild(fab);

  var thread = pane.querySelector('#asst-thread');
  var ta = pane.querySelector('#asst-ta');
  var sendBtn = pane.querySelector('#asst-send');

  function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }
  // 把回答里的页码引用(「第40页」「40页」)变成可点链接 → 跳页 + 底部「回到」条
  function _linkifyPages(el) {
    try {
      var total = (typeof pdfDoc !== 'undefined' && pdfDoc) ? pdfDoc.numPages : 99999;
      var re = /第?\s*(\d{1,4})\s*页/g;
      var nodes = [], w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null), nd;
      while ((nd = w.nextNode())) {
        if (nd.nodeValue && /\d\s*页/.test(nd.nodeValue) && nd.parentNode &&
            !nd.parentNode.closest('a,button,.asst-pagelink,code,pre')) nodes.push(nd);
      }
      nodes.forEach(function (node) {
        var t = node.nodeValue, frag = document.createDocumentFragment(), last = 0, m; re.lastIndex = 0;
        while ((m = re.exec(t))) {
          var pn = parseInt(m[1], 10);
          if (!pn || pn < 1 || pn > total) continue;
          if (m.index > last) frag.appendChild(document.createTextNode(t.slice(last, m.index)));
          var a = document.createElement('span'); a.className = 'asst-pagelink'; a.textContent = m[0]; a.dataset.page = pn;
          frag.appendChild(a); last = m.index + m[0].length;
        }
        if (last) { if (last < t.length) frag.appendChild(document.createTextNode(t.slice(last))); node.parentNode.replaceChild(frag, node); }
      });
    } catch (_) {}
  }
  function renderMd(el, text, withMath) {
    try { el.innerHTML = (typeof md === 'function') ? md(text || ' ') : esc(text).replace(/\n/g, '<br>'); }
    catch (_) { el.innerHTML = esc(text).replace(/\n/g, '<br>'); }
    _linkifyPages(el);
    // withMath===false(流式期间)跳过 MathJax:原先每 100ms 对整段重 typeset,长答案末段二次方卡顿 → 收尾只跑一次
    if (withMath !== false) { try { if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([el]).catch(function () {}); } catch (_) {} }
  }
  // stream-fx(mfx):流式期间在回答末尾挂一个闪烁光标(renderMd 每 delta 重渲 innerHTML,故每次都补挂)
  function _appendCaret(el) { try { var c = document.createElement('span'); c.className = 'mfx-caret'; el.appendChild(c); } catch (_) {} }
  // 逐字浮现 —— 把 el 正文按 字/词 切片包进 .mfx-w(返回 {spans,total})。
  //   下标 < revN(揭示游标,已揭示)的字打 .mfx-shown → 即时显示,不重播(整段重渲下防闪);
  //   下标 ≥ revN 的字默认隐藏(CSS),由 _revealTick 揭示游标连续推进时逐个加 .mfx-reveal 淡入。
  //   这样"揭示节奏"由稳定速度的游标驱动,跟 SSE delta 的到达节奏解耦 → 真·连续逐字(不是段一段)。
  //   光标放在揭示 frontier(第 revN-1 个)后面。长答案(>5000 字)外层跳过逐字以保性能。
  function _streamWrap(el, revN) {
    var idx = 0, spans = [];
    function walk(node) {
      var kids = Array.prototype.slice.call(node.childNodes);
      for (var i = 0; i < kids.length; i++) {
        var n = kids[i];
        if (n.nodeType === 3) {                       // 文本节点 → 切 字/词 包 span
          var toks = (n.nodeValue || '').match(/[一-鿿　-〿＀-￯]|[A-Za-z0-9]+(?:['’][A-Za-z]+)?|[^\sA-Za-z0-9一-鿿　-〿＀-￯]|\s+/g) || [];
          if (!toks.length) continue;
          var frag = document.createDocumentFragment();
          toks.forEach(function (p) {
            if (/^\s+$/.test(p)) { frag.appendChild(document.createTextNode(p)); return; }
            var s = document.createElement('span'); s.className = 'mfx-w'; s.textContent = p;
            if (idx < revN) { s.classList.add('mfx-shown'); }   // 已揭示:即时
            frag.appendChild(s); spans.push(s); idx++;
          });
          node.replaceChild(frag, n);
        } else if (n.nodeType === 1 && n.className !== 'mfx-caret') {
          walk(n);
        }
      }
    }
    try { walk(el); } catch (_) { return { spans: [], total: idx }; }
    var f = spans[Math.min(revN, spans.length) - 1];
    var c = document.createElement('span'); c.className = 'mfx-caret';
    if (f && f.parentNode) { f.parentNode.insertBefore(c, f.nextSibling); } else { el.appendChild(c); }
    return { spans: spans, total: idx };
  }
  // 收尾:把追问 chip / 「!」反馈条做一次淡入(逐个错峰)
  function _fadeInAfter(el) {
    try {
      var xs = el.querySelectorAll('.asst-followups,.asst-fb-bar');
      Array.prototype.forEach.call(xs, function (x, k) {
        x.classList.add('mfx-after');
        setTimeout(function () { x.classList.add('on'); }, 80 + k * 120);
      });
    } catch (_) {}
  }
  // 从回答里剥离 [[FOLLOWUP]]q1|q2|q3[[/FOLLOWUP]] 追问建议(容忍流式中途未闭合)
  // ── 阶段5 门控:ui=shared → PdfAdapter.splitFollowups → rc-assistant.splitFollowups(纯解析,逐字等价);
  //   else 原逻辑逐字(_splitFollowupsNative)。只迁解析(纯函数无 DOM);渲染 _renderFollowups 留 native
  //   (PDF 的 _fadeInAfter 错峰淡入绑死 .asst-followups class,rc 版产出 .rc-fu-box → 迁渲染会丢淡入)。──
  function _splitFollowups(text) {
    if (window.__uiShared && window.PdfAdapter && PdfAdapter.splitFollowups)
      return PdfAdapter.splitFollowups(text, _splitFollowupsNative);
    return _splitFollowupsNative(text);
  }
  function _splitFollowupsNative(text) {
    var fu = [];
    var push = function (body) { body.split(/[|\n]+/).forEach(function (q) { q = q.trim().replace(/^[\-·•\d\.\s]+/, ''); if (q) fu.push(q); }); };
    var clean = (text || '').replace(/\[\[FOLLOWUP\]\]([\s\S]*?)\[\[\/FOLLOWUP\]\]/g, function (m, body) { push(body); return ''; });
    var open = clean.indexOf('[[FOLLOWUP]]');   // 模型常漏结束标记 → 未闭合:从 [[FOLLOWUP]] 到结尾都当追问
    if (open >= 0) { push(clean.slice(open + 12).replace(/\[\[\/?FOLLOWUP\]\]/g, '')); clean = clean.slice(0, open); }
    return { text: clean.trim(), followups: fu.slice(0, 4) };
  }
  function _renderFollowups(afterEl, fus) {
    if (!fus || !fus.length) return;
    var box = document.createElement('div'); box.className = 'asst-followups';
    fus.forEach(function (q) {
      var b = document.createElement('button'); b.className = 'asst-fu';
      b.textContent = q;   // 纯文本占位;下面让 MathJax 就地把 chip 里的 $..$ 渲成公式(点击仍发原始 q)
      b.addEventListener('click', function () { if (!streaming) send(q); });
      box.appendChild(b);
    });
    afterEl.appendChild(box);
    try { if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([box]).catch(function () {}); } catch (_) {}   // 追问 chip 里的公式渲染
    scrollDown();
  }

  // ── 每条回答的「!」反馈:点开看这条回答经过了哪些 AI 调用(任务名 + 模型 + 耗时),再给三种调控 ──
  //  · 🎯 答得不够好 → 把「回答」动作的预设升一档 + 立刻用该档重答本题
  //  · 🐢 太慢了    → 把「回答」动作的预设调到「同质量更快」的档(不重答,只影响以后)
  //  · 每步 ⚙      → 直接给这个动作选 模型 + 深度(haiku/sonnet/opus × low…max),存为该动作预设
  // 速度/质量谱(Pareto,实测:opus·low ≈ sonnet·high 质量但更快 → 取代 sonnet·high;haiku 在最快端)
  // Pareto 清洗后的自动谱:每档=该质量下最快的配置。快端粗(haiku/sonnet·快不需要细分 effort);
  // 深端(opus)给全 effort 范围 low→max(含 medium)。sonnet·medium/high 被 opus·low 支配,故不入谱(⚙ 里仍可手选)。
  var _SPEC = [
    { model: 'haiku',  effort: 'low',    label: 'haiku·快' },
    { model: 'sonnet', effort: 'low',    label: 'sonnet·快' },
    { model: 'opus',   effort: 'low',    label: 'opus·快' },      // ≈sonnet·深 质量但实测更快 → 占这一档
    { model: 'opus',   effort: 'medium', label: 'opus·中' },
    { model: 'opus',   effort: 'high',   label: 'opus·深' },
    { model: 'opus',   effort: 'xhigh',  label: 'opus·更深' },
    { model: 'opus',   effort: 'max',    label: 'opus·max' }
  ];
  function _specIdx(m, e) { for (var i = 0; i < _SPEC.length; i++) if (_SPEC[i].model === m && _SPEC[i].effort === e) return i; return -1; }
  function _tierLabel(m, e) { var i = _specIdx(m, e); return i >= 0 ? _SPEC[i].label : (m + '·' + e); }
  function _curTier(trace) {   // 从 trace[0].model(如 "sonnet·high")解析本次「回答」动作档位
    try {
      var mm = String((trace && trace[0] && trace[0].model) || '').match(/([a-z]+)[^a-z]+([a-z]+)/i);
      if (mm) return { model: mm[1].toLowerCase(), effort: mm[2].toLowerCase() };
    } catch (_) {}
    return null;
  }
  // cur 在谱上的下标。sonnet·深(默认深答,不在谱上)按"质量≈opus·快"映射到 opus·快 的位置,使升/降一致。
  function _ladderIdxOf(cur) {
    if (!cur) return -1;
    if (cur.model === 'sonnet' && cur.effort === 'high') return _specIdx('opus', 'low');
    return _specIdx(cur.model, cur.effort);
  }
  function _strongerTier(cur) {   // 质量↑一档;null=已 opus·max;未知→默认 opus·深
    var i = _ladderIdxOf(cur);
    if (i < 0) { var d = _specIdx('opus', 'high'); return d >= 0 ? _SPEC[d] : _SPEC[_SPEC.length - 1]; }
    return (i + 1 < _SPEC.length) ? _SPEC[i + 1] : null;
  }
  function _fasterTier(cur) {   // 速度↑、尽量保质量;null=已 haiku·快;未知→sonnet·快
    // sonnet·深:同质量的更快档 = 直接换 opus·快(横向 Pareto 改进,你的洞见),不降质量
    if (cur && cur.model === 'sonnet' && cur.effort === 'high') { var o = _specIdx('opus', 'low'); return o >= 0 ? _SPEC[o] : _SPEC[1]; }
    var i = _ladderIdxOf(cur);
    if (i < 0) return _SPEC[1];
    return (i > 0) ? _SPEC[i - 1] : null;
  }
  var _fbOpenPop = null;
  function _fbClosePop() { if (_fbOpenPop) { try { _fbOpenPop.remove(); } catch (_) {} _fbOpenPop = null; } }
  document.addEventListener('click', function (e) {   // 点弹窗外任意处 → 收起
    if (_fbOpenPop && e.target && e.target.closest && !e.target.closest('.asst-fb-bar')) _fbClosePop();
  });
  // 给某动作存 (后端/型号/深度) 预设;backend 传 '' 清除回默认。跟感叹号「更强重答」共用此预设。
  function _setActionPref(action, backend, variant, depth, okMsg) {
    return fetch('/api/assistant/action-pref', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: action, backend: backend || '', variant: variant || '', depth: depth || '' }) })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (d && d.ok && typeof _toast === 'function') _toast(okMsg || '已设置'); return d; })
      .catch(function () {});
  }
  var _ACT_NAME = { orchestrator: '回答', summarize: '章节总结', vision: '看图' };
  // ── ⚙ 模型设置面板:列出各 AI 任务,每个可设 后端/型号/深度 ──
  var _DEPTH_LABEL = { auto: '自动(按问题)', low: 'low(快)', medium: 'medium', high: 'high(深)', xhigh: 'xhigh', max: 'max(最强)', none: '不思考', think: '思考' };
  var _BACKEND_LABEL = { claude: 'Claude', gemini: 'Gemini' };
  function _msMkSel(opts, val, labels, disabledSet) {
    var s = document.createElement('select'); s.className = 'ams-sel';
    (opts || []).forEach(function (o) {
      var op = document.createElement('option'); op.value = o;
      op.textContent = (labels && labels[o]) || o;
      if (disabledSet && disabledSet.indexOf(o) >= 0) op.disabled = true;
      if (o === val) op.selected = true;
      s.appendChild(op);
    });
    return s;
  }
  function _buildMsTask(action, info, cat, names, locked) {
    var def = info.def, cur = info.pref || def;
    var card = document.createElement('div'); card.className = 'ams-task';
    var nm = document.createElement('div'); nm.className = 'ams-tname'; nm.textContent = names[action] || action;
    var df = document.createElement('div'); df.className = 'ams-tdef';
    df.textContent = '默认:' + (_BACKEND_LABEL[def.backend] || def.backend) + ' · ' + (cat.variant_short[def.variant] || def.variant) + ' · ' + (_DEPTH_LABEL[def.depth] || def.depth);
    var row = document.createElement('div'); row.className = 'ams-row';
    var gstat = cat.gemini_status || {};
    function _fmtRetry(s) { if (!s) return ''; if (s < 90) return s + '秒'; if (s < 5400) return Math.round(s / 60) + '分'; return Math.round(s / 3600) + '小时'; }
    var varLabels = {}; (cat.variants.gemini || []).forEach(function (v) {
      var st = gstat[v], tag = ' · 免费';
      if (st && st.paid_only) { tag = ' · 💰仅付费'; }   // ListModels 证实只在付费清单(如 3.1-pro):恒计费,非临时状态
      else if (st && st.free === false) { tag = ' · 付费(' + (st.reason || '免费不可用') + (st.retry ? ',还需' + _fmtRetry(st.retry) : '') + ')'; }
      varLabels[v] = (cat.variant_short[v] || v) + tag;
    });
    var lockB = locked[action] || [];
    // 用户存的 variant 带 '@paid'(直连付费)不在 ListModels 清单里 → 插到对应裸型号后并给可读 label(同 rc-assistant)
    function _vlist(backend, val) {
      var l = (cat.variants[backend] || []).slice();
      if (val && l.indexOf(val) < 0 && /@paid$/.test(String(val))) {
        var bare = String(val).replace(/@paid$/, '');
        var i = l.indexOf(bare);
        l.splice(i >= 0 ? i + 1 : l.length, 0, val);
        varLabels[val] = (cat.variant_short[bare] || bare) + ' · 💰直连付费';
      }
      return l;
    }
    var selB = _msMkSel(cat.backends, cur.backend, _BACKEND_LABEL, lockB);
    var selV = _msMkSel(_vlist(cur.backend, cur.variant), cur.variant, varLabels);
    var selD = _msMkSel(cat.depths[cur.backend] || [], cur.depth, _DEPTH_LABEL);
    function save() {
      _setActionPref(action, selB.value, selV.value, selD.value,
        '「' + (names[action] || action) + '」已设为 ' + (cat.variant_short[selV.value] || selV.value) + '·' + (_DEPTH_LABEL[selD.value] || selD.value));
    }
    function rebindVD(backend, keepVal, vv, dv) {
      var nv = _msMkSel(_vlist(backend, keepVal ? vv : null), keepVal ? vv : (cat.variants[backend] || [])[0], varLabels);
      var nd = _msMkSel(cat.depths[backend] || [], keepVal ? dv : (cat.depths[backend] || [])[0], _DEPTH_LABEL);
      row.replaceChild(nv, selV); row.replaceChild(nd, selD); selV = nv; selD = nd;
      selV.addEventListener('change', save); selD.addEventListener('change', save);
    }
    selB.addEventListener('change', function () { rebindVD(selB.value, false); save(); });
    selV.addEventListener('change', save); selD.addEventListener('change', save);
    var rst = document.createElement('button'); rst.className = 'ams-rst'; rst.textContent = '默认';
    rst.addEventListener('click', function () {
      _setActionPref(action, '', '', '', '「' + (names[action] || action) + '」恢复默认');
      selB.value = def.backend; rebindVD(def.backend, true, def.variant, def.depth);
    });
    row.appendChild(selB); row.appendChild(selV); row.appendChild(selD); row.appendChild(rst);
    card.appendChild(nm); card.appendChild(df); card.appendChild(row);
    if (lockB.indexOf('gemini') >= 0) {
      var lk = document.createElement('div'); lk.className = 'ams-cur'; lk.textContent = '(根 agent 切 Gemini 需二期工具循环,暂锁)';
      card.appendChild(lk);
    }
    return card;
  }
  // ── 阶段5 门控:ui=shared → PdfAdapter.openModelSettings → rc-assistant.openModelSettings
  //   (同组端点 /api/assistant/action-pref[s] + 同组 action 名,消 ~140 行逐字重复);
  //   else 原逻辑逐字(_openModelSettingsNative);RC 不可用 → fallback 回 native,绝不吞功能。──
  function openModelSettings(focusAction) {
    if (window.__uiShared && window.PdfAdapter && PdfAdapter.openModelSettings) {
      return PdfAdapter.openModelSettings({ focusAction: focusAction, fallback: function () { _openModelSettingsNative(focusAction); } });
    }
    return _openModelSettingsNative(focusAction);
  }
  function _openModelSettingsNative(focusAction) {
    fetch('/api/assistant/action-prefs').then(function (r) { return r.json(); }).then(function (d) {
      if (!d || !d.ok) { if (typeof _toast === 'function') _toast('拉取设置失败'); return; }
      var mask = document.createElement('div'); mask.className = 'ams-mask';
      mask.addEventListener('click', function (e) { if (e.target === mask) mask.remove(); });
      var box = document.createElement('div'); box.className = 'ams-box';
      var h = document.createElement('div'); h.className = 'ams-h';
      var ht = document.createElement('span'); ht.textContent = '⚙ AI 模型设置';
      var x = document.createElement('button'); x.className = 'ams-x'; x.textContent = '×';
      x.addEventListener('click', function () { mask.remove(); });
      h.appendChild(ht); h.appendChild(x); box.appendChild(h);
      var sub = document.createElement('div'); sub.className = 'ams-sub';
      sub.textContent = '每个任务可单独设 后端/型号/深度,改完即时生效。跟感叹号「更强重答」共用同一套预设。';
      box.appendChild(sub);
      var _focusCard = null;
      function _renderActs(list) {
        list.forEach(function (a) {
          var ai = d.actions[a]; if (!ai) return;
          var c = _buildMsTask(a, { pref: ai.pref, def: ai.default }, d.catalog, d.names, d.locked || {});
          if (a === focusAction) _focusCard = c;   // 从某步⚙进来 → 定位到对应任务卡
          box.appendChild(c);
        });
      }
      _renderActs(['orchestrator', 'summarize', 'vision']);
      // PDF 阅读器其它 AI 入口(解释/翻译/字典/语法),跟助手共用同一套脱壳 Claude + Gemini 双后端预设
      var _rh = document.createElement('div'); _rh.className = 'ams-sub';
      _rh.style.cssText = 'margin-top:12px;font-weight:600;color:#9fc0ff;';
      _rh.textContent = '— PDF 阅读器其它 AI —';
      box.appendChild(_rh);
      _renderActs(['explain', 'translate', 'dict', 'grammar']);
      var note = document.createElement('div'); note.className = 'ams-note';
      note.textContent = '标「免费」= 免费档支持该型号;但免费是**共享算力**,高峰常过载(503)或限流时会自动落付费保不中断——'
        + '此时这里会标「付费(过载/限流)」、感叹号里也显付费。「💰仅付费」= 该型号免费档没有(如 3.1-pro),'
        + '选它每次调用都按量计费。flash 高峰过载较多;想更稳的免费可试 flash-lite 系。';
      box.appendChild(note);
      mask.appendChild(box); document.body.appendChild(mask);
      if (_focusCard) { try { _focusCard.style.outline = '2px solid #6aa3ff'; _focusCard.style.borderRadius = '8px'; _focusCard.scrollIntoView({ block: 'center' }); } catch (_) {} }
    }).catch(function () { if (typeof _toast === 'function') _toast('拉取设置失败'); });
  }
  try { window.openModelSettings = openModelSettings; } catch (_) {}   // 供 PDF 总设置面板调起
  function _buildFbPop(question, trace, close, ts) {
    var pop = document.createElement('div'); pop.className = 'asst-fb-pop';
    var h = document.createElement('div'); h.className = 'afp-h';
    h.textContent = (trace && trace.length) ? '这条回答经过的 AI 调用' : '对这条回答不满意?';
    pop.appendChild(h);
    var _tot = 0;
    // 无 trace(早期回答没存调用轨迹)→ 兜底合成一个「回答」步,保证每条都至少有「回答」动作的 ⚙
    var steps = (trace && trace.length) ? trace : [{ label: '回答', model: '', action: 'orchestrator' }];
    steps.forEach(function (st) {
      var row = document.createElement('div'); row.className = 'afp-step';
      var l = document.createElement('span'); l.className = 'afp-l'; l.textContent = st.label || '步骤';   // 任务名
      if (st.detail) {   // 这步有完整内容 → 步骤名变可点按钮,点开/收起显示该步的完整 AI 产出
        l.classList.add('afp-l-btn'); l.title = '点开看这一步的完整内容';
        l.addEventListener('click', function (e) {
          e.stopPropagation();
          var ex = row.nextSibling;
          if (ex && ex.classList && ex.classList.contains('afp-detail')) { ex.remove(); return; }   // 再点收起
          var dt = document.createElement('div'); dt.className = 'afp-detail'; dt.textContent = st.detail;
          row.parentNode.insertBefore(dt, row.nextSibling);
        });
      }
      var m = document.createElement('span'); m.className = 'afp-m';
      if (typeof st.sec === 'number') _tot += st.sec;
      var mt = st.model || '';
      m.textContent = mt + (typeof st.sec === 'number' ? (mt ? ' · ' : '') + st.sec + 's' : '');   // 模型 · 耗时(老回答可能都没有)
      if (st.tier === 'free' || st.tier === 'paid') {   // Gemini 实际服务这条用了哪档 → 标「免费/付费」
        var tg = document.createElement('span'); tg.textContent = st.tier === 'paid' ? '付费' : '免费';
        tg.style.cssText = 'margin-left:6px;padding:0 6px;border-radius:6px;font-size:11px;vertical-align:middle;'
          + (st.tier === 'paid' ? 'background:#5a3a1a;color:#ffcf8f;' : 'background:#1f4a2e;color:#8fe3a8;');
        m.appendChild(tg);
      }
      row.appendChild(l); row.appendChild(m);
      if (st.action) {   // 这一步是会调模型的动作 → 给个 ⚙ 直接设它的预设
        var g = document.createElement('button'); g.className = 'afp-gear-btn'; g.textContent = '⚙'; g.title = '设这个动作的模型/深度';
        g.addEventListener('click', function (e) {
          e.stopPropagation();
          _fbClosePop();   // 收起感叹号弹窗
          // 打开统一三维设置面板(支持 Claude/Gemini + 免费/付费标),定位到本动作 —— 不再是只有 Claude 的简易档
          try { openModelSettings(st.action); } catch (_) {}
        });
        row.appendChild(g);
      }
      pop.appendChild(row);
    });
    if (ts || _tot) {   // 页脚:完成时刻 + 总耗时(时间只要有 ts 就显示,不依赖 trace)
      var ft = document.createElement('div'); ft.className = 'afp-foot';
      var bits = [];
      if (ts && typeof _qhFmtTime === 'function') { try { bits.push('🕐 ' + _qhFmtTime(ts * 1000)); } catch (_) {} }
      if (_tot) bits.push('共 ' + (Math.round(_tot * 10) / 10) + 's');
      if (bits.length) { ft.textContent = bits.join(' · '); pop.appendChild(ft); }
    }
    var cur = _curTier(trace);
    var up = _strongerTier(cur);     // null = 已最强
    var down = _fasterTier(cur);     // null = 已最快
    var acts = document.createElement('div'); acts.className = 'afp-acts';
    // 去掉了🎯升档/🐢调快的爬梯子;只留一个「模型设置」按钮 → 打开统一三维设置面板(后端/型号/深度)
    var bSet = document.createElement('button'); bSet.className = 'afp-act afp-q';
    bSet.textContent = '⚙ 模型设置';
    bSet.addEventListener('click', function () { close(); try { openModelSettings(); } catch (_) {} });
    acts.appendChild(bSet); pop.appendChild(acts);
    return pop;
  }
  function _attachFeedback(bubble, question, trace, ts) {
    if (!bubble) return;
    try { var old = bubble.querySelector('.asst-fb-bar'); if (old) old.remove(); } catch (_) {}   // 重渲时防重复挂
    var bar = document.createElement('div'); bar.className = 'asst-fb-bar';
    var _tok = trace && trace[0] && trace[0].tok;   // 本轮累计 token → 显示「3.6k tok」
    if (_tok) {
      var tk = document.createElement('span'); tk.className = 'asst-tok';
      tk.textContent = (_tok >= 1000 ? (_tok / 1000).toFixed(1).replace(/\.0$/, '') + 'k' : _tok) + ' tok';
      tk.title = '这条回答累计消耗 token：' + _tok;
      bar.appendChild(tk);
    }
    var btn = document.createElement('button'); btn.className = 'asst-fb-btn'; btn.textContent = '!'; btn.title = '对这条回答不满意?';
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      if (_fbOpenPop && _fbOpenPop._owner === btn) { _fbClosePop(); return; }
      _fbClosePop();
      var pop = _buildFbPop(question, trace, _fbClosePop, ts); pop._owner = btn;
      bar.appendChild(pop); _fbOpenPop = pop;   // 不再 scrollDown:历史回答在中间时,点开不该把视图拽到底
    });
    bar.appendChild(btn); bubble.appendChild(bar);
  }

  document.addEventListener('click', function (e) {   // 点回答里的页码链接 → 跳页 + 底部回到条
    var t = e.target;
    if (t && t.classList && t.classList.contains('asst-pagelink') && t.dataset.page && typeof window.jumpWithBack === 'function') {
      // AI 写的「第N页」是**书上印刷页码** → 跳转前转回 PDF 页索引(过本书页码对齐偏移)
      var _pg = (typeof window._pdfFromDisp === 'function') ? window._pdfFromDisp(t.dataset.page) : parseInt(t.dataset.page, 10);
      window.jumpWithBack(_pg);
    }
  });
  function scrollDown() { thread.scrollTop = thread.scrollHeight; }
  function addMsg(cls, html) { var d = document.createElement('div'); d.className = 'asst-msg ' + cls; d.innerHTML = html; thread.appendChild(d); scrollDown(); return d; }

  // 视口焦点:当前与 #main 视口相交的页的字符层文字(镜像 EPUB _visibleText)。让 AI 回答/找视频/配图/拟搜索词
  // 都紧扣"用户此刻在看的这段",而非泛泛的整页/整章主题(后端 _sys_prompt 的「紧扣可见段落」指引靠它才生效)。
  function _visibleText() {
    try {
      var main = document.getElementById('main'); if (!main) return '';
      var mr = main.getBoundingClientRect(), top = mr.top, bot = mr.bottom;
      var pws = document.querySelectorAll('.page-wrap[data-page-num]'), parts = [];
      for (var i = 0; i < pws.length; i++) {
        var pw = pws[i], r = pw.getBoundingClientRect();
        if (r.height && r.bottom > top + 8 && r.top < bot - 8 && pw.__charBoxes && pw.__charBoxes.length) {   // 与视口相交
          var t = ''; try { t = _charsRangeToText(pw.__charBoxes, 0, pw.__charBoxes.length - 1); } catch (e) {}
          t = (t || '').replace(/\s+/g, ' ').trim();
          if (t) parts.push(t);
        }
      }
      var txt = parts.join('\n');
      return txt.length > 1000 ? txt.slice(0, 1000) + '…' : txt;
    } catch (e) { return ''; }
  }

  function ctx() {
    var c = { page_type: 'pdf' };
    // 取阅读器当前上下文经统一中间层 RC.adapter().getContext()(PdfAdapter 只读包 __voiceContext);
    // 中间层不可用(legacy 无 adapter / RC 未加载)→ 回退直连 __voiceContext。便签合并见下方(消费侧,不变)。
    try {
      var g = (window.RC && RC.adapter && RC.adapter().getContext) ? RC.adapter().getContext() : null;
      c = g || ((typeof window.__voiceContext === 'function') ? (window.__voiceContext() || c) : c);
    } catch (_) {}
    try { if (c && !c.visible_text) c.visible_text = _visibleText(); } catch (_) {}   // 视口焦点(镜像 EPUB 2516):AI 找视频/配图/回答紧扣当前屏幕,不退回泛章节
    // 便签注入(双击便签 → __noteAttached,见下方注入块):无笔画=文字+锚点附近正文走 context.notes 文本通道;
    // 有笔画=kind:'note' 条目并入 figures 走视觉通道(服务端 see_figure 认 note_id → _note_composite_png 现场合成)
    try {
      var atts = window.__noteAttached || [];
      var txtNotes = [], inkNotes = [];
      atts.forEach(function (n) {
        if (n.has_ink) {
          inkNotes.push({ kind: 'note', note_id: n.id, page: n.page || 0, caption: '手写便签',
                          desc: String(n.text || '').slice(0, 300), near: String(n.near || '').slice(0, 600),
                          file_rel: (typeof FILE_REL !== 'undefined' ? FILE_REL : ''), has_ink: true });
        } else {
          txtNotes.push({ id: n.id, text: String(n.text || '').slice(0, 2000), near: String(n.near || '').slice(0, 1200), page: n.page || 0 });
        }
      });
      if (txtNotes.length) c.notes = txtNotes.slice(0, 4);
      if (inkNotes.length) c.figures = ((c.figures || []).concat(inkNotes.slice(0, 4))).slice(0, 6);
    } catch (_) {}
    return c;
  }

  // ── 便签注入(阶段3,设计见 references/sticky-notes-design.md 用户规格8):双击便签(rc-stickynote onDoubleTap,
  //   经 27-rc-adapter 接到这里)→ 加入 __noteAttached + 输入框上方 chip(挨图附件条,同款视觉,✕ 移除)。
  //   chip 生命周期同图附件条:发送时定格进 ctx、发完即清。──
  window.__noteAttached = [];   // [{id,text,near,page,has_ink,_thumb}](_thumb=合成图 data_url,仅前端显示不随请求发)
  function _renderNoteChips() {
    try {
      var paneEl = document.getElementById('side-pane-asst');
      var input = paneEl && paneEl.querySelector('#asst-input');
      var list = window.__noteAttached || [];
      var wrap = document.getElementById('asst-note-chips');
      if (!list.length) { if (wrap) wrap.remove(); return; }
      if (!input) return;   // 助手还没建 → 数据仍在,开了再渲
      if (!wrap) {
        wrap = document.createElement('div'); wrap.id = 'asst-note-chips';
        wrap.style.cssText = 'display:flex;flex-wrap:wrap;gap:6px;padding:6px 10px 0';
        input.parentNode.insertBefore(wrap, input);
      }
      wrap.innerHTML = '';
      list.forEach(function (n) {
        var chip = document.createElement('div'); chip.className = 'asst-fig-chip';
        if (n.has_ink) {
          var img = document.createElement('img'); img.className = 'afc-thumb'; img.alt = '';
          if (n._thumb) {
            img.src = n._thumb; img.style.cursor = 'zoom-in';
            img.addEventListener('click', function () {   // 点缩略图看大图(复用 26-figures 的 .fig-lightbox 样式)
              var mask = document.createElement('div'); mask.className = 'fig-lightbox';
              var big = document.createElement('img'); big.src = n._thumb; big.alt = '';
              mask.appendChild(big); document.body.appendChild(mask);
              mask.addEventListener('click', function () { mask.remove(); });
            });
          }
          chip.appendChild(img);
        } else {
          var ic = document.createElement('span'); ic.textContent = '🗒'; ic.style.cssText = 'flex:none;font-size:15px'; chip.appendChild(ic);
        }
        var t = String(n.text || '').replace(/\s+/g, ' ').trim();
        var cap = document.createElement('span'); cap.className = 'afc-cap';
        cap.textContent = n.has_ink ? ('手写便签' + (t ? ' · ' + t.slice(0, 14) : '')) : (t.slice(0, 20) || '便签');
        var x = document.createElement('button'); x.className = 'afc-x'; x.textContent = '✕';
        x.addEventListener('click', function () { window.__noteAttached = (window.__noteAttached || []).filter(function (z) { return z.id !== n.id; }); _renderNoteChips(); });
        chip.appendChild(cap); chip.appendChild(x); wrap.appendChild(chip);
      });
    } catch (_) {}
  }
  window.__renderNoteChips = _renderNoteChips;
  window.__clearNoteAttached = function () { window.__noteAttached = []; _renderNoteChips(); };
  function _noteFetchThumb(entry) {
    setTimeout(function () {   // 稍等 rc-stickynote 的文字/笔画 PATCH 先落库,合成图才含最新内容
      fetch('/pdf/api/note-composite', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file: (typeof FILE_REL !== 'undefined' ? FILE_REL : ''), id: entry.id }) })
        .then(function (r) { return r.json(); })
        .then(function (d) { if (d && d.ok && d.data_url) { entry._thumb = d.data_url; _renderNoteChips(); } })
        .catch(function () {});
    }, 350);
  }
  // 锚点附近正文(contextAt):便签所在页字符层里,离锚点 y 最近的字符前后各 ±600 字
  function _noteNearText(anchor) {
    try {
      if (!anchor || anchor.kind !== 'pdf') return '';
      var pw = document.querySelector('.page-wrap[data-page-num="' + anchor.page + '"]');
      var ch = pw && pw.__charBoxes;
      if (!ch || !ch.length) return '';
      var yPx = Math.max(0, Math.min(1, anchor.y || 0)) * (pw.clientHeight || 1);
      var best = 0, bestD = Infinity;
      for (var i = 0; i < ch.length; i++) {
        var d = Math.abs((ch[i].top + ch[i].height / 2) - yPx);
        if (d < bestD) { bestD = d; best = i; }
      }
      return _charsRangeToText(ch, Math.max(0, best - 600), Math.min(ch.length - 1, best + 600)).slice(0, 1300);
    } catch (_) { return ''; }
  }
  window.__noteInject = function (note) {
    try {
      if (!note || !window.__asstOpen || !window.__asstOpen()) return false;   // 助手没开 → 维持现状(不注入)
      var list = window.__noteAttached = window.__noteAttached || [];
      var hasInk = !!(note.strokes && note.strokes.length);
      var old = null;
      for (var i = 0; i < list.length; i++) if (list[i].id === note.id) old = list[i];
      if (old) {   // 已在附件条 → 只刷新内容(文字/笔画可能变了),不重复加
        old.text = note.text || '';
        if (hasInk && !old.has_ink) { old.has_ink = true; old._thumb = ''; }
        if (old.has_ink) _noteFetchThumb(old);
        _renderNoteChips();
        if (typeof _toast === 'function') _toast('已在对话上下文');
        return true;
      }
      var entry = { id: note.id, text: note.text || '', near: _noteNearText(note.anchor),
                    page: (note.anchor && note.anchor.page) || 0, has_ink: hasInk, _thumb: '' };
      list.push(entry);
      _renderNoteChips();
      if (entry.has_ink) _noteFetchThumb(entry);
      if (typeof _toast === 'function') _toast('🗒 便签已带进对话');
      return true;
    } catch (_) { return false; }
  };

  // 点击历史/上下文卡片 → 跳到那一页(同书走 jumpWithBack 带「回到」条;跨书则打开那本书定位到页)
  function _jumpToCtx(file_rel, page) {
    page = parseInt(page, 10); if (!page || page < 1) return;
    var cur = (typeof FILE_REL !== 'undefined') ? FILE_REL : '';
    if (file_rel && cur && file_rel !== cur) { location.href = '/pdf/view?file=' + encodeURIComponent(file_rel) + '&page=' + page; return; }   // 跨书:正确路由是 /pdf/view(/pdf/ 是书架)
    if (typeof window.jumpWithBack === 'function') window.jumpWithBack(page);
  }
  // ③ 上下文卡「选中」跳页后:目标页字符层里找到这段文字 → 临时呼吸高亮几秒后自动移除。
  // 机制**复用不重写**:高亮走 15-phrase 的 _activePhraseHl 状态 + renderPhraseHl 渲染管线
  // (绝对定位进 page-wrap 内容坐标系随页滚动,08-charlayer 页重渲还会自动补画,铁律2/3);
  // 等页字符层就绪的重试节奏照搬 11-search 的 _applyPendingSearchHighlight。找不到文字(公式
  // 选区/OCR 差异)就只跳页不闪。定时移除前校验高亮仍是本次的,不误删用户随后发起的词组查询高亮。
  var _ctxSelFlashT = null;
  function _flashSelOnPage(page, text, tries) {
    page = parseInt(page, 10); text = String(text || '').trim();
    if (!page || !text) return;
    tries = tries || 0;
    var wrap = document.querySelector('[data-page-num="' + page + '"]');
    if (!(wrap && wrap.dataset.loaded === '1' && wrap.__charBoxes && wrap.__charBoxes.length)) {
      if (tries < 30) setTimeout(function () { _flashSelOnPage(page, text, tries + 1); }, 160);   // 最多 ~4.8s(同搜索)
      return;
    }
    try {
      var chars = wrap.__charBoxes;
      var full = chars.map(function (c) { return c.c || ''; }).join('').toLowerCase();
      var i = full.indexOf(text.toLowerCase());
      if (i < 0) return;
      var rects = _charRangeToPtRects(chars, i, i + text.length - 1);   // 起止含端点(同 _showPhraseHighlight 的 startIdx..endIdx)
      if (!rects.length) return;
      var mine = { id: ++_phraseHlSeq, page: page, text: text, rects: rects, solid: false };
      _phraseHls.push(mine);   // 追加一个临时闪烁高亮(多高亮:不清别的)
      renderPhraseHl(wrap);
      var first = wrap.querySelector('.phrase-hl-layer[data-phid="' + mine.id + '"] .hl');
      if (first) { try { first.scrollIntoView({ block: 'center' }); } catch (_) {} }
      clearTimeout(_ctxSelFlashT);
      _ctxSelFlashT = setTimeout(function () { if (_phraseHls.indexOf(mine) >= 0) { try { _removePhraseHighlight(mine); } catch (_) {} } }, 5000);
    } catch (_) {}
  }
  // open_book 工具的 client_action 用:打开另一本书(可定位页)
  window.openBookAt = function (fr, pg) { try { _jumpToCtx(fr, parseInt(pg, 10) || 1); } catch (_) {} };
  // 一条用户消息的上下文卡片:用过的图缩略图 + 选中的字段 + 涉及的页码,点任意一处都能跳过去
  // meta:{figures:[{file_rel,page,box,caption,group,has_ink}], selection, page, file_rel}; live=刚发的那条(图有笔迹走实时合成)
  // 问题是否跟「本页内容」相关:有选中/图(由它们承载跳转),或问题文字含本页指代 → 才给页码按钮;
  // 跟本页无关的纯问题(如"什么是特征值")不带页码 chip
  function _pageRefersToPage(msg) {
    var m = msg || '';
    return /这一?页|本页|此页|当前页|这段|这里|这张?图|这幅图|如[下图]图?|上面这?|这个公式|这道?题|本章|这一?章|这一?节|本节|页面|图里|图中|这部分/.test(m)
        || /\bthis (page|figure|fig|section|paragraph|chapter|image|diagram|part)\b|\bhere\b/i.test(m);
  }
  function _ctxCard(meta, live, msg) {
    if (!meta) return null;
    var figs = (meta.figures || []).filter(function (f) { return f && f.box; });
    var sel = (meta.selection || '').trim();
    var page = parseInt(meta.page, 10) || 0;
    var bookRel = meta.file_rel || (typeof FILE_REL !== 'undefined' ? FILE_REL : '');
    // 页码 chip:有选中/图时由它们跳转(不重复给);否则仅当问题确实指向本页才给
    var showPage = page && !figs.length && !sel && _pageRefersToPage(msg);
    if (!figs.length && !sel && !showPage) return null;
    var card = document.createElement('div'); card.className = 'asst-ctx-card';
    if (figs.length) {
      var row = document.createElement('div'); row.className = 'actx-thumbs';
      figs.forEach(function (f) {
        var fr = f.file_rel || bookRel;
        var img = document.createElement('img'); img.className = 'actx-thumb'; img.alt = '';
        img.title = (f.group ? '图组 · ' : '') + (f.caption || '图') + ' · p' + (window._dispPage ? window._dispPage(f.page) : f.page) + ' · 点击跳转';
        if (typeof window.__figThumb === 'function') window.__figThumb({ file_rel: fr, page: f.page, box: f.box, has_ink: f.has_ink }, img, live);
        img.addEventListener('click', function () { _jumpToCtx(fr, f.page); });
        row.appendChild(img);
      });
      card.appendChild(row);
    }
    if (sel) {
      var s = document.createElement('div'); s.className = 'actx-sel';
      if (/^\$\$?[\s\S]+\$\$?$/.test(sel)) {   // 公式选区($..$/$$..$$)→ MathJax 渲染,不显示成裸 LaTeX
        s.classList.add('actx-fml');
        var _raw = sel.replace(/^\$\$?/, '').replace(/\$\$?$/, '');
        var _block = /^\$\$/.test(sel) || /\\begin\{|\\\\/.test(_raw);
        s.textContent = _block ? ('\\[' + _raw + '\\]') : ('\\(' + _raw + '\\)');   // 跟公式浮层一致,避免单 $ 行内未启用
        if (window.MathJax && MathJax.typesetPromise) setTimeout(function () { try { MathJax.typesetPromise([s]); } catch (_) {} }, 0);
      } else {
        s.textContent = '“' + (sel.length > 64 ? sel.slice(0, 64) + '…' : sel) + '”';
      }
      s.title = page ? ('跳到第 ' + ((typeof window._dispPage === 'function') ? window._dispPage(page) : page) + ' 页') : '';
      s.addEventListener('click', function () {   // ③ 跳页后把这段选中在页上临时呼吸高亮(跨书整页跳走,不闪)
        _jumpToCtx(bookRel, page);
        var curF = (typeof FILE_REL !== 'undefined') ? FILE_REL : '';
        if (page && (!bookRel || !curF || bookRel === curF)) _flashSelOnPage(page, sel);
      });
      card.appendChild(s);
    }
    if (showPage) {
      var pg = document.createElement('span'); pg.className = 'actx-page';
      pg.textContent = '📄 第 ' + ((typeof window._dispPage === 'function') ? window._dispPage(page) : page) + ' 页';   // 存的是 PDF 页 → 显印刷页
      pg.addEventListener('click', function () { _jumpToCtx(bookRel, page); });
      card.appendChild(pg);
    }
    return card;
  }

  function runActions(actions) {
    if (!actions || !actions.length) return;
    actions.forEach(function (a) { try { if (a && a.fn && typeof window[a.fn] === 'function') window[a.fn].apply(null, a.args || []); } catch (_) {} });
  }
  // 助手「列出可删高亮」工具产生 → 在对话里逐条渲染:色块 + 文字 + 「↗跳转」+「🗑删除」。用户点跳转看/点删除移除(不替他删)。
  window._showHlPicker = function (d) {
    try {
      if (!d || !Array.isArray(d.items) || !d.items.length) return;
      var fileRel = d.file_rel || '';
      var box = document.createElement('div'); box.className = 'asst-msg asst-a';
      var head = document.createElement('div'); head.style.cssText = 'margin-bottom:4px;opacity:.85;font-size:12.5px';
      head.textContent = '共 ' + d.items.length + ' 处高亮 —— 点「跳转」去看,点「删除」移除:';
      box.appendChild(head);
      d.items.forEach(function (it) {
        var row = document.createElement('div'); row.className = 'asst-hl-row';
        var sw = document.createElement('span'); sw.className = 'asst-hl-sw'; sw.style.background = it.color || '#fff59d';
        var tx = document.createElement('span'); tx.className = 'asst-hl-tx';
        tx.textContent = '第' + it.page + '页 · ' + (it.text || '(无文字)');   // it.page=印刷页(显示)
        tx.title = it.text || '';
        var _jp = (it.pdf_page != null) ? it.pdf_page : it.page;   // 跳转用 PDF 页(jumpWithBack 收 PDF 页)
        var jb = document.createElement('button'); jb.className = 'asst-jump'; jb.setAttribute('data-page', _jp); jb.textContent = '↗ 跳转';
        var db2 = document.createElement('button'); db2.className = 'asst-hl-del';
        db2.setAttribute('data-id', it.id); db2.setAttribute('data-file', fileRel);
        try { db2.setAttribute('data-hl', JSON.stringify({ page: _jp, rects: it.rects || [], color: it.color, text: it.text || '' })); } catch (_) {}   // M9:删完「↪重做」重建用
        db2.textContent = '🗑 删除';
        row.appendChild(sw); row.appendChild(tx); row.appendChild(jb); row.appendChild(db2);
        box.appendChild(row);
      });
      thread.appendChild(box); scrollDown();
    } catch (_) {}
  };
  // agent 画完高亮后:重新拉高亮 + 重渲所有可见页(复用 17-highlight 的模块函数,本模块同作用域可调)
  window._reloadHighlights = async function () {
    try {
      if (typeof loadAllHighlights === 'function') await loadAllHighlights();
      document.querySelectorAll('.page-wrap').forEach(function (pw) {
        var n = parseInt(pw.dataset.pageNum); if (n && typeof renderHighlightsOnPage === 'function') renderHighlightsOnPage(pw, n);
      });
    } catch (_) {}
  };
  // AI 建/改便签、撤销/重做后:重挂页面便签(rc-stickynote.loadAll 幂等全量;legacy 模式无 RC 则静默跳过)。
  // 后端 notes_create/notes_edit/undo_last 的 client_action {fn:'notesReload'} 也走这里(runActions → window[fn])。
  window.notesReload = function () {
    try { if (window.RC && RC.stickynote && RC.stickynote.loadAll) RC.stickynote.loadAll(); } catch (_) {}
  };
  // ── 改动发生时**自动**生成「跳转 + 撤销/重做」卡片(系统在高亮/便签写入时生成,非 AI 文本生成)──
  var _assistEdits = {}, _aeCtr = 0;
  window._assistEdit = function (d) {
    try {
      if (!d || !Array.isArray(d.items) || !d.items.length) return;
      if (d.type === 'note') return _assistNoteCard(d);   // 便签写操作(notes_create/notes_edit)→ 便签版卡
      if (d.type !== 'highlight') return;
      try { window._reloadHighlights && window._reloadHighlights(); } catch (_) {}   // 先把刚画的高亮渲出来
      var eid = 'ae' + (++_aeCtr);
      _assistEdits[eid] = { file: d.file || '', items: d.items.slice(),
                            ids: d.items.map(function (it) { return it.id; }).filter(Boolean), undone: false };
      var pages = [], seen = {};
      d.items.forEach(function (it) {
        var dp = (it.disp_page != null) ? it.disp_page : it.pdf_page;
        if (dp != null && !seen[dp]) { seen[dp] = 1; pages.push({ disp: dp, pdf: (it.pdf_page != null ? it.pdf_page : dp) }); }
      });
      var card = document.createElement('div'); card.className = 'asst-edit-card';
      var head = document.createElement('div'); head.className = 'asst-edit-h';
      head.textContent = '✏️ 已高亮 ' + d.items.length + ' 处' + (pages.length ? '（第 ' + pages.map(function (p) { return p.disp; }).join('、') + ' 页）' : '');
      card.appendChild(head);
      if (pages.length) {
        var chips = document.createElement('div'); chips.className = 'asst-edit-chips';
        pages.forEach(function (p) {
          var c = document.createElement('button'); c.className = 'asst-jump';
          c.setAttribute('data-page', p.pdf); c.textContent = '→ 第' + p.disp + '页'; chips.appendChild(c);
        });
        card.appendChild(chips);
      }
      var btn = document.createElement('button'); btn.className = 'asst-edit-undo';
      btn.setAttribute('data-eid', eid); btn.textContent = '↩ 撤销';
      card.appendChild(btn);
      thread.appendChild(card); scrollDown();
    } catch (_) {}
  };
  // 便签写操作的「跳转 + 撤销⇄重做」卡(同高亮卡形态;后端 notes_create/notes_edit 的 client_action 触发):
  //   create:撤销=DELETE 该便签,重做=POST 快照重建(拿新 id 接管撤销);edit:撤销=PATCH 旧 text/color,重做=PATCH 新值。
  //   任何一步都只碰 text/color/整条,绝不动 strokes/anchor/尺寸。
  function _assistNoteCard(d) {
    try {
      window.notesReload();   // 先把刚建/改的便签渲出来
      var eid = 'ae' + (++_aeCtr);
      _assistEdits[eid] = { ntype: 'note', op: d.op || 'create', file: d.file || '', items: d.items.slice(), undone: false };
      var it0 = d.items[0] || {};
      var card = document.createElement('div'); card.className = 'asst-edit-card';
      var head = document.createElement('div'); head.className = 'asst-edit-h';
      head.textContent = '🗒 ' + (d.op === 'edit' ? '已修改便签' : '已创建便签') + (it0.disp_page ? '（第 ' + it0.disp_page + ' 页）' : '');
      card.appendChild(head);
      if (it0.pdf_page) {
        var chips = document.createElement('div'); chips.className = 'asst-edit-chips';
        var c = document.createElement('button'); c.className = 'asst-jump';
        c.setAttribute('data-page', it0.pdf_page); c.textContent = '→ 第' + (it0.disp_page || it0.pdf_page) + '页';
        chips.appendChild(c); card.appendChild(chips);
      }
      var btn = document.createElement('button'); btn.className = 'asst-edit-undo';
      btn.setAttribute('data-eid', eid); btn.textContent = '↩ 撤销';
      card.appendChild(btn);
      thread.appendChild(card); scrollDown();
    } catch (_) {}
  }
  // 便签卡的撤销⇄重做执行(点 .asst-edit-undo 且 st.ntype==='note' 时走这;完成后重挂页面便签)
  function _noteEditToggle(st, eb) {
    var API = '/pdf/api/notes';
    function fin(undone) { st.undone = undone; eb.disabled = false; eb.textContent = undone ? '↪ 重做' : '↩ 撤销'; window.notesReload(); }
    function patchAll(vals, undone) {
      Promise.all((st.items || []).map(function (it) {
        var v = it[vals] || {};
        return fetch(API, { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ file: st.file, id: it.id, text: v.text, color: v.color }) }).catch(function () {});
      })).then(function () { fin(undone); });
    }
    if (!st.undone) {
      eb.textContent = '撤销中…';
      if (st.op === 'edit') { patchAll('old', true); return; }
      Promise.all((st.items || []).map(function (it) {
        return fetch(API + '?file=' + encodeURIComponent(st.file) + '&id=' + encodeURIComponent(it.id), { method: 'DELETE' }).catch(function () {});
      })).then(function () { fin(true); });
    } else {
      eb.textContent = '重做中…';
      if (st.op === 'edit') { patchAll('new', false); return; }
      Promise.all((st.items || []).map(function (it) {
        var n = it.note || {};
        return fetch(API, { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ file: st.file, anchor: n.anchor, text: n.text, color: n.color, w: n.w, h: n.h, collapsed: n.collapsed, strokes: n.strokes }) })
          .then(function (r) { return r.json(); })
          .then(function (dd) { if (dd && dd.ok) { it.id = dd.id; it.note = dd.note || n; } })
          .catch(function () {});
      })).then(function () { fin(false); });
    }
  }

  var _abort = null, _recovering = false, _lastProgressTs = 0, _ridCtr = 0;
  function _whenVisibleAsst() {   // 在后台 → 等回到前台再继续(重连前先回前台,后台重连也会被掐)
    return new Promise(function (res) {
      if (document.visibilityState !== 'hidden') return res();
      var h = function () { if (document.visibilityState !== 'hidden') { document.removeEventListener('visibilitychange', h); res(); } };
      document.addEventListener('visibilitychange', h);
    });
  }
  // 切后台→回前台:iOS 常把进行中的 SSE fetch 掐死/僵死 → 回来报 "Load failed" 或永远卡在「思考中」。
  // 回前台后给 3s 看有无新进度,没有就主动 abort 这条死流 → 走「从服务端历史恢复本轮回答」(服务端 finally 已落库)。
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState !== 'visible' || !streaming) return;
    setTimeout(function () {
      if (streaming && !_recovering && (Date.now() - _lastProgressTs > 3000)) {
        _recovering = true; try { _abort && _abort.abort(); } catch (_) {}
      }
    }, 3000);
  });
  // 流断/掐死后:服务端早把用户消息落了库、并在 finally 落了助手回答(可能完整也可能到断点)→ 拉回来补上
  async function _recoverFromHistory(tries) {
    tries = tries || 0;
    try {
      var r = await fetch('/api/assistant/history');
      var d = await r.json();
      if (d && d.ok && d.messages && d.messages.length) {
        var last = d.messages[d.messages.length - 1];
        if (last && last.role === 'assistant' && last.content) return last;
        if (tries < 2) { await new Promise(function (rs) { setTimeout(rs, 800); }); return _recoverFromHistory(tries + 1); }   // 服务端 finally 还没落库 → 等等再试
      }
    } catch (_) {}
    return null;
  }
  function _setSendMode(stop) {   // 流式中:发送键→停止键(红 ■);否则发送(➤)
    if (stop) { sendBtn.classList.add('stop'); sendBtn.textContent = '■'; sendBtn.title = '停止'; }
    else { sendBtn.classList.remove('stop'); sendBtn.textContent = '➤'; sendBtn.title = '发送'; }
    sendBtn.disabled = false;
  }

  async function send(text, opts) {
    if (streaming) return;
    text = (text || '').trim();
    var sentCtx = ctx();                                // 发送时定格上下文(图/选中/页),气泡卡片与后端保存的元数据一致
    // 隐式选中(无 chip 的持久兜底)也要"所见即所得":升格为可见焦点 chip(带 ✕)→ 之后每条都看得见、随时可取消
    // (用户反馈:选中悄悄跟着每条消息发,但上方没有那个带 x 的框,无法取消)
    try { if (!(window.__focusSel && window.__focusSel.text) && sentCtx.selection && sentCtx.selection.trim() && window.__setFocusSel) window.__setFocusSel(sentCtx.selection, 'text'); } catch (_) {}
    if (!text) {
      // 空输入但有焦点上下文(带入的图 / 钉住的公式或段落 / 当前选中)→ 等于"就问这个",用默认问法直接发
      var _hasFig = (sentCtx.figures && sentCtx.figures.length);
      var _hasNote = (sentCtx.notes && sentCtx.notes.length);
      var _fs = sentCtx.focus_sel;
      var _hasSel = (sentCtx.selection && sentCtx.selection.trim());
      if (_hasFig) text = (sentCtx.figures.every(function (f) { return f && f.kind === 'note'; })) ? '讲讲这个便签' : '讲讲这张图';
      else if (_hasNote) text = '讲讲这个便签';
      else if (_fs && _fs.text) text = (_fs.kind === 'formula') ? '讲讲这个公式' : '讲讲这段';
      else if (_hasSel) text = '讲讲这段';
      else return;   // 真·空(无任何上下文)→ 不发
    }
    streaming = true; _setSendMode(true);
    var uMsg = addMsg('asst-u', esc(text));
    try { var _cc = _ctxCard(sentCtx, true, text); if (_cc) uMsg.appendChild(_cc); } catch (_) {}
    try { window.__clearFigFocus && window.__clearFigFocus(); } catch (_) {}   // 图已"用掉"并进了这条历史 → 清空带入列表,下一条不再重复携带
    try { window.__clearNoteAttached && window.__clearNoteAttached(); } catch (_) {}   // 便签 chip 同图附件条:发完即清(已定格进 sentCtx)
    var aMsg = addMsg('asst-a', '<span class="mfx-typing"><i></i><i></i><i></i></span>');
    var answer = '', acts = [], aborted = false, traceData = null, _recTs = 0;
    // 逐字浮现的"揭示游标":跟 SSE delta 到达节奏解耦,由 rAF 稳定速度推进 → 连续逐字(不段一段)
    var _revN = 0, _spans = [], _tot = 0, _raf = null, _lastTs = 0, _acc = 0, _noChar = false;
    function _revealTick(ts) {
      _raf = null;
      if (!streaming) return;
      if (!_lastTs) _lastTs = ts;
      var dt = Math.min(ts - _lastTs, 120); _lastTs = ts;   // clamp:切后台回来 dt 巨大,别一次灌完
      var backlog = _tot - _revN;
      if (backlog > 0) {
        var rate = 0.05 * (1 + backlog / 40);               // 字/ms:落后越多揭示越快,追上自然放慢
        _acc += dt * rate;
        var n = Math.min(backlog, Math.floor(_acc), 6);     // 每帧上限 6,防一次性灌入又变"段"
        if (n > 0) {
          _acc -= n;
          for (var k = 0; k < n; k++) { var s = _spans[_revN]; if (s) s.classList.add('mfx-reveal'); _revN++; }
          var c = aMsg.querySelector('.mfx-caret'), f = _spans[_revN - 1];
          if (c && f && f.parentNode) f.parentNode.insertBefore(c, f.nextSibling);
          scrollDown();
        }
      }
      if (streaming) _raf = requestAnimationFrame(_revealTick);
    }
    function _stopReveal() { if (_raf) { try { cancelAnimationFrame(_raf); } catch (_) {} _raf = null; } }
    var rid = 'c' + Date.now() + '_' + (_ridCtr++);   // 本轮任务 id:断线用它重连续读(服务端 detached 跑,不绑请求)
    var evSeen = 0, done = false;                      // 已消费的缓冲事件数(重连用 from=evSeen 续传)
    function _handleEv(ev, parsed) {
      if (ev === 'meta') return;                       // rid 确认,不计数
      evSeen++;
      if (ev === 'done') { done = true; return; }
      if (ev === 'tool') { aMsg.innerHTML = '<span class="asst-tool">🔧 ' + esc(parsed) + '…</span>'; scrollDown(); }
      else if (ev === 'tool-done') { try { aMsg.innerHTML = '<span class="asst-tool">思考中…</span>'; scrollDown(); } catch (_) {} }   // L3:工具完→中性「思考中」直到下个 answer/tool(镜像 EPUB)
      else if (ev === 'answer') {   // 流式轻量渲(不 MathJax)+ 剥 FOLLOWUP + 提亮&逐字浮现(揭示游标)+光标(mfx)
        answer = parsed; var _at = _splitFollowups(answer).text;
        renderMd(aMsg, _at, false); aMsg.classList.add('mfx-streaming');
        if (!_noChar && _at.length > 5000) { _noChar = true; _stopReveal(); }   // 超长答案:停揭示,改普通(保性能)
        if (_noChar) { _appendCaret(aMsg); }
        else {
          var w = _streamWrap(aMsg, _revN); _spans = w.spans; _tot = w.total;   // 重渲后重包:已揭示打 mfx-shown,新字等游标
          if (_revN > _tot) _revN = _tot;
          if (!_raf) { _lastTs = 0; _raf = requestAnimationFrame(_revealTick); }   // 启动/续跑揭示循环
        }
        scrollDown();
      }
      else if (ev === 'notice') { addMsg('asst-note', esc(parsed)); scrollDown(); }
      else if (ev === 'gemini-paid') {   // ② 免费 Gemini 受限→本次已用付费:提示条 + 一键「以后直接用付费」(渲染器在 rc-assistant,legacy 模式退纯文字)
        try {
          var _pn = (window.RC && RC.assistant && RC.assistant.paidNotice) ? RC.assistant.paidNotice(parsed) : null;
          if (_pn) { thread.appendChild(_pn); scrollDown(); }
          else if (!window.__paidNoted) { window.__paidNoted = true; addMsg('asst-note', esc((parsed && parsed.text) || '免费 Gemini 额度受限,本次已使用付费档。')); scrollDown(); }
        } catch (_) {}
      }
      else if (ev === 'actions') { try { runActions(parsed); } catch (_) {} }   // 实时:工具一执行完就应用(高亮/跳页立即生效),不等 AI 输出完
      else if (ev === 'trace') { traceData = parsed; }   // 调用链 → 喂「!」反馈弹窗
      else if (ev === 'task') { trackTask(parsed.task_id, parsed.label); }
      else if (ev === 'undo' && parsed && parsed.undo_id) {
        var _ujp = parsed.page ? ' <button class="asst-jump" data-page="' + esc(parsed.page) + '">↗ 跳转</button>' : '';
        addMsg('asst-a', '✓ ' + esc(parsed.label || '完成') + _ujp + ' <button class="asst-undo" data-uid="' + esc(parsed.undo_id) + '">↩ 撤销</button>');
      }
      else if (ev === 'error') { answer = '⚠️ ' + parsed; aMsg.innerHTML = esc(answer); }
    }
    // 开一条 SSE 读到自然结束/断开。首连带 message+context;重连只带 rid+from(服务端按 rid 续发缓冲事件)。
    async function _stream(body) {
      _abort = (typeof AbortController !== 'undefined') ? new AbortController() : null;
      var r = await fetch('/api/assistant/chat', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body), signal: _abort ? _abort.signal : undefined,
      });
      if (r.status === 410) { done = 'gone'; return; }   // 任务已过期(>3min)→ 走历史恢复
      if (!r.ok || !r.body) throw new Error('http ' + r.status);
      var reader = r.body.getReader(), dec = new TextDecoder(), buf = '';
      while (true) {
        var rd = await reader.read(); if (rd.done) break;
        _lastProgressTs = Date.now();   // 有数据 = 流活着(回前台看门狗据此判断僵死)
        buf += dec.decode(rd.value, { stream: true });
        var idx;
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          var chunk = buf.slice(0, idx); buf = buf.slice(idx + 2);
          var ev = 'message', data = '';
          chunk.split('\n').forEach(function (ln) {
            if (ln.indexOf('event:') === 0) ev = ln.slice(6).trim();
            else if (ln.indexOf('data:') === 0) data += ln.slice(5).trim();
          });
          var parsed; try { parsed = JSON.parse(data); } catch (_) { parsed = data; }
          _handleEv(ev, parsed);
          if (done) return;
        }
      }
    }
    _recovering = false; _lastProgressTs = Date.now();
    var tries = 0;
    while (!done && !aborted) {
      try {
        try { if (sentCtx && window.rcNoBook && window.rcNoBook()) sentCtx.no_book = true; } catch (e) {}
        await _stream(tries === 0
          ? { message: text, context: sentCtx, rid: rid, media_prefer: (window.rcMediaPrefer ? window.rcMediaPrefer() : undefined), force_effort: (opts && opts.forceEffort) || undefined, force_model: (opts && opts.forceModel) || undefined }
          : { rid: rid, from: evSeen });
      } catch (e) {
        if (e && e.name === 'AbortError') {
          if (_recovering) { _recovering = false; }   // 看门狗掐死僵死流 → 当断线,重连续传
          else { aborted = true; break; }             // 用户点停止 → 保留已生成部分
        }
        // 其它(Load failed / 网络断)→ 落到下面重连
      }
      if (done || done === 'gone' || aborted) break;
      if (++tries > 40) break;                          // 兜底:worker 6min 内必完成;真连不上才放弃
      try { aMsg.innerHTML = '<span class="asst-tool">连接断开,正在续传…</span>'; scrollDown(); } catch (_) {}
      await _whenVisibleAsst();                         // 等回到前台再重连
      await new Promise(function (rs) { setTimeout(rs, Math.min(400 * tries, 2000)); });
    }
    // 任务过期 / 兜底没续上 → 从服务端历史恢复(worker 跑完已落库,绝不丢)
    if ((done === 'gone' || (!done && !aborted)) && !answer) {
      try { aMsg.innerHTML = '<span class="asst-tool">正在恢复…</span>'; } catch (_) {}
      var rec = await _recoverFromHistory();
      if (rec && rec.content) { answer = rec.content; traceData = rec.trace || traceData; _recTs = rec.ts || 0; }
    }
    // 收尾:剥 FOLLOWUP → 完整渲染(MathJax 这一次)→ 追问 chip
    _stopReveal();                            // stream-fx:停揭示循环(下面 renderMd 重渲成干净 markdown,无 span/光标)
    aMsg.classList.remove('mfx-streaming');   // 停止提亮
    var pf = _splitFollowups(answer);
    if (pf.text) renderMd(aMsg, pf.text, true);
    else if (aMsg.innerHTML.indexOf('asst-tool') >= 0 || aMsg.innerHTML.indexOf('mfx-typing') >= 0) aMsg.innerHTML = esc(aborted ? '(已停止)' : '(没拿到回答)');
    if (!aborted) { try { _renderFollowups(aMsg, pf.followups); } catch (_) {} }
    if (!aborted && pf.text) { try { _attachFeedback(aMsg, text, traceData, _recTs || Math.floor(Date.now() / 1000)); } catch (_) {} }   // 「!」反馈按钮(带本轮调用链 + 耗时/时刻 + 可重答)
    if (!aborted) { try { _fadeInAfter(aMsg); } catch (_) {} }   // stream-fx:追问/反馈条错峰淡入
    runActions(acts);
    streaming = false; _abort = null; _recovering = false; _setSendMode(false);
  }

  // 后台写任务(制卡/笔记/生词):轮询完成 → 在对话里给结果 + 「↩ 撤销」按钮 + PWA 通知
  function trackTask(id, label) {
    if (!id) return;
    var line = addMsg('asst-a', '<span class="asst-tool">⏳ ' + esc(label || '处理') + '中…</span>');
    var n = 0;
    (function poll() {
      if (n++ > 120) { line.innerHTML = '<span class="asst-tool">⌛ ' + esc(label) + ':等太久了</span>'; return; }
      fetch('/api/voice/task-status?id=' + encodeURIComponent(id)).then(function (r) { return r.json(); }).then(function (d) {
        if (!d || !d.ok) { return; }
        if (d.status === 'running') { if (d.step) line.innerHTML = '<span class="asst-tool">⏳ ' + esc(d.step) + '…</span>'; setTimeout(poll, 2000); return; }
        if (d.status === 'done') {
          var uid = d.result && d.result.undo_id;
          line.innerHTML = '✓ ' + esc(d.speak || '完成') + (uid ? ' <button class="asst-undo" data-uid="' + esc(uid) + '">↩ 撤销</button>' : '');
          notify('阅读助手 ✓', d.speak || '任务完成');
        } else { line.innerHTML = '✗ ' + esc(d.error || '没办成'); }
        scrollDown();
      }).catch(function () { setTimeout(poll, 3000); });
    })();
  }
  thread.addEventListener('click', function (e) {
    var jb = e.target && e.target.closest && e.target.closest('.asst-jump');
    if (jb) { var jp = parseInt(jb.getAttribute('data-page'), 10); if (jp && typeof window.jumpWithBack === 'function') window.jumpWithBack(jp); return; }
    var redo = e.target && e.target.closest && e.target.closest('.asst-hl-redo');   // M9:删完的「↪ 重做」→ 用存的锚重建高亮(拿新 id)
    if (redo) {
      var rf = redo.getAttribute('data-file'), rd = {};
      try { rd = JSON.parse(redo.getAttribute('data-hl') || '{}'); } catch (_) {}
      if (!rd.rects || !rd.rects.length) { if (typeof _toast === 'function') _toast('这条没有几何信息,无法重建'); return; }
      redo.disabled = true; redo.textContent = '重建中…';
      fetch('/pdf/api/highlights', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ file: rf, page: rd.page, rects: rd.rects, color: rd.color, text: rd.text }) })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d && d.ok) {
            try { window._reloadHighlights && window._reloadHighlights(); } catch (_) {}
            var db = document.createElement('button'); db.className = 'asst-hl-del';
            db.setAttribute('data-id', d.id || ''); db.setAttribute('data-file', rf); db.setAttribute('data-hl', redo.getAttribute('data-hl') || ''); db.textContent = '🗑 删除';
            var row2 = redo.closest('.asst-hl-row'); redo.replaceWith(db);
            if (row2) { row2.style.opacity = ''; var tx2 = row2.querySelector('.asst-hl-tx'); if (tx2) tx2.style.textDecoration = ''; }
          } else { redo.disabled = false; redo.textContent = '↪ 重做'; if (typeof _toast === 'function') _toast('重建失败'); }
        })
        .catch(function () { redo.disabled = false; redo.textContent = '↪ 重做'; });
      return;
    }
    var del = e.target && e.target.closest && e.target.closest('.asst-hl-del');   // 「列出可删高亮」里的删除按钮
    if (del) {
      var hid = del.getAttribute('data-id'), hfile = del.getAttribute('data-file');
      del.disabled = true; del.textContent = '删除中…';
      fetch('/pdf/api/highlights', { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ file: hfile, id: hid }) })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d && d.ok) {
            try {   // 立刻把这条高亮的 DOM 元素(.hl-saved[data-id])抹掉 → 不等重拉就即时反映在页面
              var esc2 = (window.CSS && CSS.escape) ? CSS.escape(hid) : hid;
              document.querySelectorAll('.hl-saved[data-id="' + esc2 + '"]').forEach(function (el) { el.remove(); });
            } catch (_) {}
            try { window._reloadHighlights && window._reloadHighlights(); } catch (_) {}   // 再重拉,同步 _hlByPage,翻页回来不复现
            var row = del.closest('.asst-hl-row');
            if (row) { row.style.opacity = '.45'; var tx = row.querySelector('.asst-hl-tx'); if (tx) tx.style.textDecoration = 'line-through'; }
            var _rb = document.createElement('button'); _rb.className = 'asst-hl-redo'; _rb.textContent = '↪ 重做';   // M9:删完转重做(用存的锚重建)
            _rb.setAttribute('data-file', hfile); _rb.setAttribute('data-hl', del.getAttribute('data-hl') || '');
            del.replaceWith(_rb);
          } else { del.disabled = false; del.textContent = '🗑 删除'; if (typeof _toast === 'function') _toast('删除失败:' + ((d && d.error) || '')); }
        })
        .catch(function () { del.disabled = false; del.textContent = '🗑 删除'; });
      return;
    }
    // 自动卡片的「撤销 ⇄ 重做」切换:撤销=删全部 id,重做=用存的字段重建(拿新 id),按钮文字来回切
    var eb = e.target && e.target.closest && e.target.closest('.asst-edit-undo');
    if (eb) {
      var eid2 = eb.getAttribute('data-eid'); var st = _assistEdits[eid2]; if (!st) return;
      eb.disabled = true;
      if (st.ntype === 'note') { _noteEditToggle(st, eb); return; }   // 便签卡:走便签版撤销⇄重做
      if (!st.undone) {
        eb.textContent = '撤销中…';
        Promise.all((st.ids || []).map(function (id) {
          return fetch('/pdf/api/highlights', { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ file: st.file, id: id }) }).then(function (r) { return r.json(); }).catch(function () { return { ok: false }; });
        })).then(function () {
          try { window._reloadHighlights && window._reloadHighlights(); } catch (_) {}
          st.undone = true; eb.disabled = false; eb.textContent = '↪ 重做';
        });
      } else {
        eb.textContent = '重做中…';
        Promise.all((st.items || []).map(function (it) {
          return fetch('/pdf/api/highlights', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ file: st.file, page: it.pdf_page, rects: it.rects, color: it.color, text: it.text }) }).then(function (r) { return r.json(); }).then(function (d) { return (d && d.ok) ? d.id : null; }).catch(function () { return null; });
        })).then(function (nids) {
          st.ids = nids.filter(Boolean);
          try { window._reloadHighlights && window._reloadHighlights(); } catch (_) {}
          st.undone = false; eb.disabled = false; eb.textContent = '↩ 撤销';
        });
      }
      return;
    }
    var btn = e.target && e.target.closest && e.target.closest('.asst-undo'); if (!btn) return;
    var uid = btn.getAttribute('data-uid'); btn.disabled = true; btn.textContent = '撤销中…';
    fetch('/api/assistant/undo', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: uid }) })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.ok && d.kind === 'highlight') { try { window._reloadHighlights && window._reloadHighlights(); } catch (_) {} }   // 撤销高亮要重渲页面才视觉清掉
        btn.outerHTML = d && d.ok ? '<span class="asst-tool">↩ 已撤销</span>' : ('<span class="asst-tool">撤销失败:' + esc((d && d.error) || '') + '</span>');
      })
      .catch(function () { btn.disabled = false; btn.textContent = '↩ 撤销'; });
  });
  function notify(title, body) {
    try {
      if (!('Notification' in window) || Notification.permission !== 'granted') return;
      var opt = { body: body, tag: 'asst-task', icon: '/static/icons/icon-192.png' };
      if (navigator.serviceWorker && navigator.serviceWorker.ready) navigator.serviceWorker.ready.then(function (reg) { reg.showNotification(title, opt); }).catch(function () { try { new Notification(title, opt); } catch (_) {} });
      else try { new Notification(title, opt); } catch (_) {}
    } catch (_) {}
  }

  // 快捷按钮
  pane.querySelector('#asst-quick').addEventListener('click', function (e) {
    var btn = e.target && e.target.closest ? e.target.closest('button') : e.target;
    var ds = btn && btn.getAttribute('data-send');
    if (ds) { if (!streaming) send(ds); return; }   // 学习类快捷按钮:直接发预设问题
    var q = btn && btn.getAttribute('data-q'); if (!q) return;
    try {
      if (q === 'prev') window.changePage(-1);
      else if (q === 'next') window.changePage(1);
      else if (q === 'fit') window.fitWidth();
      else if (q === 'zin') window.zoomChange(0.15);
      else if (q === 'zout') window.zoomChange(-0.15);
      else if (q === 'ptrans') window.togglePageTranslate();
      else if (q === 'clear') { if (streaming) { try { _abort && _abort.abort(); } catch (_) {} streaming = false; _setSendMode(false); } thread.innerHTML = ''; fetch('/api/assistant/clear', { method: 'POST' }).catch(function () {}); greet(); }   // L5:流式中清空先中止,防在已移除气泡上继续写 + streaming 卡死
      else if (q === 'models') { openModelSettings(); }
    } catch (_) {}
  });

  // 输入
  function autorow() { ta.style.height = 'auto'; ta.style.height = Math.min(120, ta.scrollHeight) + 'px'; }
  ta.addEventListener('input', autorow);
  ta.addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); if (streaming) return; micStop(); var v = ta.value; ta.value = ''; autorow(); send(v); } });
  sendBtn.addEventListener('click', function () {
    if (streaming) { try { _abort && _abort.abort(); } catch (_) {} return; }   // 流式中点 ■ → 中止本轮
    micStop(); var v = ta.value; ta.value = ''; autorow(); send(v);
  });

  // ── 苹果风格语音按钮:持续聆听,只手动停(再点麦克风 / 点发送即停)。设备原生 STT(iOS=Siri 级)。
  //    iOS 的 SpeechRecognition 静默时会自己结束,所以只要用户没手动停,onend 就重启 = 真·持续聆听。
  //    识别结果只填进输入框(用户审一眼再发),续写已有内容;无 SR 的浏览器→聚焦输入框,用系统键盘自带听写麦克风。
  //    micStop/micStart 为函数声明(在本 IIFE 内提升),上面的发送处理器即可调用 micStop 收口,避免迟到结果回填残留。
  var micBtn = pane.querySelector('#asst-mic');
  var _SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  var micRec = null, micOn = false, micCommitted = '', micSessFinal = '', micSessTok = null, micLastWrite = '';
  var micStartTs = 0, micLastStart = 0, micFails = 0, micSessProductive = false;   // 总时长软上限 + 空转(弱网/无语音)退避
  function micStop() {                 // 手动停:micOn=false + 作废会话 → 迟到 onresult 不回填、onend 不重启
    if (!micOn) return;
    micOn = false; micSessTok = null; micFails = 0;
    try { micRec && micRec.stop(); } catch (_) {}
    micBtn.classList.remove('on');
  }
  function micSpin() {                  // 起一段识别(每段:会话令牌 tok + 实例身份 thisRec 双重身份)
    if (!micOn) return;
    var tok = (micSessTok = {});
    var thisRec;
    try {
      thisRec = micRec = new _SR();
      micRec.lang = 'zh-CN'; micRec.interimResults = true; micRec.continuous = true; micRec.maxAlternatives = 1;
      micSessFinal = ''; micSessProductive = false; micLastStart = Date.now();
      micRec.onresult = function (e) {
        if (!micOn || micSessTok !== tok) return;   // 发送/停止/编辑作废的会话:不回填(防残留/旧词复活/孤立实例串扰)
        micSessProductive = true;
        var f = '', it = '';
        for (var i = 0; i < e.results.length; i++) {
          if (e.results[i].isFinal) f += e.results[i][0].transcript; else it += e.results[i][0].transcript;
        }
        micSessFinal = f;
        ta.value = micCommitted + f + it; micLastWrite = ta.value; autorow();
      };
      micRec.onerror = function (ev) {  // 权限/无麦:立即放弃;network/no-speech 等交给下面的空转计数收口,不单次就放弃
        if (ev && (ev.error === 'not-allowed' || ev.error === 'service-not-allowed' || ev.error === 'audio-capture')) micOn = false;
      };
      micRec.onend = function () {
        if (micRec !== thisRec) return;             // 已被更晚的 spin 取代的孤立实例:不提交不重启(根治 orphan + 竞态)
        if (micSessTok === tok && micSessFinal) { micCommitted = (micCommitted + micSessFinal).replace(/\s+$/, '') + ' '; }
        micSessFinal = '';
        micBtn.classList.remove('on');
        if (!micOn) { autorow(); return; }
        if (Date.now() - micStartTs > 120000) { micStop(); return; }   // 总时长软上限 2min:忘关也不会一直占麦
        // 这段没出任何结果且很快就结束 = 疑似弱网/引擎空转 → 累计 5 次即停;出过结果或在正常等静默则清零
        if (!micSessProductive && (Date.now() - micLastStart) < 1200) { if (++micFails >= 5) { micStop(); return; } }
        else micFails = 0;
        micBtn.classList.add('on');
        setTimeout(function () { if (micOn && micRec === thisRec) micSpin(); }, micFails ? 700 : 0);   // 异步重启(打断紧致 churn)+ 退避
      };
      micRec.start();
    } catch (_) { micOn = false; micSessTok = null; micBtn.classList.remove('on'); ta.focus(); }
  }
  function micStart() {
    if (!_SR) { ta.focus(); return; }   // 无原生 STT:聚焦输入框,用系统键盘的听写麦克风
    micOn = true; micFails = 0; micStartTs = Date.now(); micBtn.classList.add('on');
    micCommitted = ta.value ? (ta.value.replace(/\s+$/, '') + ' ') : '';   // 续写已有内容
    micSessFinal = ''; micLastWrite = ta.value;
    micSpin();
  }
  // 听写中用户手动改输入框(典型:逐字删除):以改后文本为新基线 + 作废当前会话重起一段(新实例 results 为空,
  // 旧词不会被下次 onresult 带回来)。我们自己填的值不算手动编辑(programmatic 赋值不触发 input,micLastWrite 再兜底)。
  ta.addEventListener('input', function () {
    if (!micOn || ta.value === micLastWrite) return;
    micSessTok = null;                  // 作废:在途/后续旧 onresult 不再回填
    micCommitted = ta.value; micLastWrite = ta.value; micSessFinal = '';
    try { micRec && micRec.stop(); } catch (_) {}   // onend(micOn 仍真,且是当前实例)→ micSpin 重起 fresh-results 新会话
  });
  document.addEventListener('visibilitychange', function () { if (document.hidden) micStop(); });   // 切走/锁屏即停,免后台占麦空转
  if (!_SR) micBtn.title = '点这里→用键盘的听写麦克风';
  micBtn.addEventListener('click', function () { micOn ? micStop() : micStart(); });

  function prewarm(off) { try { fetch('/api/assistant/prewarm', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(off ? { off: 1 } : {}), keepalive: true }); } catch (_) {} }
  window.__asstPrewarm = function () { try { prewarm(false); } catch (_) {} };   // 切到助手 tab 时也预热(减第二条起的冷启动)
  function greet() { addMsg('asst-a', '我是这本书的阅读助手。试试:<br>· 这页讲什么 / 总结这页<br>· 翻译这段(先选中)<br>· 找讲XX的页跳过去<br>· 把这段做成卡片 / 整理成笔记<br><span style="color:#7a8497">(写入/制卡都可「↩ 撤销」;对话云端保存、跨设备;🗑 清空)</span>'); }
  function loadHistory() {   // 开面板载入服务端保存的历史(跨设备续上)
    fetch('/api/assistant/history').then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.ok && d.messages && d.messages.length) {
        var _lastQ = '';   // 历史回答的「!」反馈要带上「重答」用的原问题 → 记住上一条用户消息
        d.messages.forEach(function (m) {
          if (m.role === 'user') {
            _lastQ = m.content || '';
            var uel = addMsg('asst-u', esc(m.content));
            try { var c = _ctxCard({ figures: m.figures, selection: m.selection, page: m.page, file_rel: m.file_rel }, false, m.content); if (c) uel.appendChild(c); } catch (_) {}
          }
          else {
            var el = addMsg('asst-a', ''); var _pf = _splitFollowups(m.content || ''); renderMd(el, _pf.text);
            try { _renderFollowups(el, _pf.followups); } catch (_) {}
            try { _attachFeedback(el, _lastQ, m.trace || null, m.ts || null); } catch (_) {}   // 历史也带 trace(步骤/模型/耗时)+ 时刻;质量回报用 _lastQ 重答
            if (Array.isArray(m.videos) && m.videos.length && window.renderVideos) { try { window.renderVideos(m.videos); } catch (_) {} }   // 视频卡刷新回放(镜像 EPUB 阶段C)
            if (Array.isArray(m.undo_cards)) m.undo_cards.forEach(function (u) {   // H2:高亮撤销卡刷新回放(undo_id 服务端持久,撤销/跳转 handler 已复用)
              if (!u || !u.undo_id) return;
              var _ujp = u.page ? ' <button class="asst-jump" data-page="' + esc(u.page) + '">↗ 跳转</button>' : '';
              addMsg('asst-a', '✓ ' + esc(u.label || '完成') + _ujp + ' <button class="asst-undo" data-uid="' + esc(u.undo_id) + '">↩ 撤销</button>');
            });
          }
        });
        // 进面板自动滚到最新(最下方):渲完滚一次,再隔 250ms 补一次(图/MathJax 异步撑高后位置会漂)
        requestAnimationFrame(scrollDown);
        setTimeout(scrollDown, 250);
      } else greet();
    }).catch(greet);
  }
  loadHistory();
})();

// ── 26-figures.js:页级图注(扫描书插图的 AI 描述)。Apple 风格图区徽标 → 点开描述浮层。──
// 后端 /pdf/api/page-figures 懒描述+预取(见 pdf_reader._fig_*)。徽标定位用 Claude 给的归一 bbox。
(function () {
  if (window.__figLoaded) return; window.__figLoaded = true;

  var _cache = {};       // page -> {figs:[...], pending:bool}
  var _poll = {};        // page -> 轮询次数

  var css = document.createElement('style');
  css.textContent =
    // 苹果风格:磨砂玻璃圆形徽标,轻投影,极简「照片」符号
    '.fig-badge{position:absolute;width:26px;height:26px;border-radius:50%;z-index:6;cursor:pointer;' +
    'display:flex;align-items:center;justify-content:center;color:#fff;opacity:.5;' +
    'background:rgba(10,132,255,.62);-webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px);' +
    'box-shadow:0 2px 7px rgba(0,0,0,.2),inset 0 0 0 .5px rgba(255,255,255,.3);' +
    '-webkit-tap-highlight-color:transparent;transition:transform .12s,opacity .12s;touch-action:none}' +
    '.fig-badge:active{transform:scale(.88)}.fig-badge:hover{opacity:.95}' +
    '.fig-badge svg{width:15px;height:15px;display:block}' +
    '.fig-pop{position:fixed;z-index:130;max-width:min(86vw,440px);background:#11192c;color:#e8eeff;' +
    'border:1px solid #2a3a63;border-radius:16px;box-shadow:0 12px 40px rgba(0,0,0,.5);' +
    'padding:14px 16px;font-size:14px;line-height:1.6;max-height:60vh;overflow-y:auto;-webkit-overflow-scrolling:touch}' +
    '.fig-pop h4{margin:0 0 6px;font-size:14px;color:#7fb0ff;font-weight:600}' +
    '.fig-pop .fig-x{position:absolute;top:8px;right:10px;color:#8aa;cursor:pointer;font-size:16px;line-height:1}' +
    '.fig-pop p{margin:.35em 0}.fig-pop code{background:#0b1220;padding:1px 4px;border-radius:4px}' +
    '.fig-pop-mask{position:fixed;inset:0;z-index:129;background:transparent}' +
    // 点徽标时高亮该图的 YOLO 框范围
    '.fig-hl{position:absolute;z-index:5;pointer-events:none;border:2.5px solid rgba(10,132,255,.92);' +
    'background:rgba(10,132,255,.10);border-radius:7px;box-shadow:0 0 0 2px rgba(10,132,255,.18);' +
    'animation:figHlIn .22s ease-out}' +
    '@keyframes figHlIn{from{opacity:0;transform:scale(1.03)}to{opacity:1;transform:scale(1)}}' +
    // 持续「已选中」高亮(跟 __figAttached 同步,与临时 .fig-hl 分开;绿色区分选中态)
    '.fig-hl-sel{position:absolute;z-index:5;pointer-events:none;border:2.5px solid rgba(48,209,88,.95);' +
    'background:rgba(48,209,88,.12);border-radius:7px;box-shadow:0 0 0 2px rgba(48,209,88,.22);animation:figHlIn .22s ease-out}' +
    // 图区命中层(透明可点;touch-action:auto 不挡阅读滚动,拖拽改用徽标当把手)
    '.fig-hit{position:absolute;z-index:5;cursor:pointer;-webkit-tap-highlight-color:transparent;touch-action:pan-y}' +   // pan-y:竖向照常滚;长按才发起拖动(拖动时 preventDefault 抑制滚)
    '.fig-hit.dragging{touch-action:none}' +
    '.fig-drag-ghost{position:fixed;z-index:240;width:118px;max-height:150px;object-fit:contain;opacity:.6;' +
    'border:2px solid rgba(10,132,255,.85);border-radius:9px;box-shadow:0 10px 28px rgba(0,0,0,.55);' +
    'transform:translate(-50%,-50%);pointer-events:none;background:#fff}' +
    '#grammar-panel.fig-drop-ready{outline:2px dashed rgba(10,132,255,.5);outline-offset:-4px}' +
    '#grammar-panel.fig-drop-over{outline:3px solid rgba(10,132,255,.95);background:rgba(10,132,255,.07)}' +
    '#fig-drop-plus{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);z-index:131;' +
    'font-size:64px;font-weight:300;color:rgba(10,132,255,.6);pointer-events:none;text-shadow:0 2px 8px rgba(0,0,0,.4)}' +
    // 助手对话里「已带入的图」附件条列表(可多张,横向 wrap;每张 缩略图 + 图注 + ✕)
    '#asst-fig-chips{display:flex;flex-wrap:wrap;gap:6px;padding:6px 10px 0}' +
    '.asst-fig-chip{display:flex;align-items:center;gap:6px;padding:4px 6px;background:#16203a;' +
    'border:1px solid #2a3a63;border-radius:9px;max-width:100%}' +
    '.asst-fig-chip .afc-thumb{width:38px;height:38px;object-fit:cover;border-radius:5px;border:1px solid #3b6db5;background:#fff;flex:none}' +
    '.asst-fig-chip .afc-cap{font-size:11px;color:#cfe6ff;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:130px}' +
    '.asst-fig-chip .afc-x{background:transparent;border:none;color:#9ab;font-size:13px;cursor:pointer;flex:none;padding:0 2px}' +
    // 焦点选区 chip(公式/段落)
    '#asst-sel-chip{padding:6px 10px 0}' +
    '.asst-sel-chip-in{display:flex;align-items:center;gap:7px;padding:5px 8px;background:#101a30;border:1px solid #2f4a7d;border-radius:9px;max-width:100%}' +
    '.asst-sel-chip-in.is-fml{border-color:#3b6db5}' +
    '.asst-sel-chip-in .asc-icon{flex:none;font-size:14px}' +
    '.asst-sel-chip-in .asc-body{flex:1 1 auto;min-width:0;font-size:12px;color:#dbe7ff;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
    '.asst-sel-chip-in.is-fml .asc-body{color:#eaf2ff;white-space:normal;max-height:46px;overflow:auto}' +
    '.asst-sel-chip-in .asc-x{flex:none;background:transparent;border:none;color:#9ab;font-size:13px;cursor:pointer;padding:0 2px}' +
    // 点缩略图看大图(合成图)
    '.fig-lightbox{position:fixed;inset:0;z-index:300;background:rgba(0,0,0,.85);display:flex;align-items:center;justify-content:center;padding:16px;cursor:zoom-out}' +
    '.fig-lightbox img{max-width:100%;max-height:100%;object-fit:contain;border-radius:8px;box-shadow:0 8px 40px rgba(0,0,0,.6);background:#fff}';
  document.head.appendChild(css);

  var PHOTO_SVG = '<svg viewBox="0 0 24 24" fill="none"><rect x="3" y="5" width="18" height="14" rx="3" stroke="currentColor" stroke-width="1.7"/>' +
    '<circle cx="8.5" cy="10" r="1.6" fill="currentColor"/>' +
    '<path d="M5 17l4.5-4.5a1.5 1.5 0 0 1 2 0L17 18" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';

  function md(t) {
    try { return (typeof window.md === 'function') ? window.md(t || '') : null; } catch (_) { return null; }
  }
  function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }

  var _popTimer = null, _popRepos = null, _popBadge = null, _hlEl = null, _popOutside = null;
  function clearHl() { if (_hlEl) { _hlEl.remove(); _hlEl = null; } }
  function showHl(elOrPw, fig) {           // 在页面上画出该图 YOLO 框(fbox)的范围
    clearHl();
    var bb = (fig.fbox && fig.fbox.length === 4) ? fig.fbox : fig.bbox;
    if (!bb || bb.length !== 4 || !elOrPw) return;
    var pw = (elOrPw.classList && elOrPw.classList.contains('page-wrap')) ? elOrPw
             : (elOrPw.closest && elOrPw.closest('.page-wrap'));
    if (!pw) return;
    var layer = pw.querySelector('.fig-layer'); if (!layer) return;
    var canvas = pw.querySelector('canvas');
    var cssW = (canvas && canvas.clientWidth) || pw.clientWidth, cssH = (canvas && canvas.clientHeight) || pw.clientHeight;
    if (!cssW || !cssH) return;
    var el = document.createElement('div'); el.className = 'fig-hl';
    el.style.left = (bb[0] * cssW) + 'px'; el.style.top = (bb[1] * cssH) + 'px';
    el.style.width = Math.max(2, (bb[2] - bb[0]) * cssW) + 'px';
    el.style.height = Math.max(2, (bb[3] - bb[1]) * cssH) + 'px';
    layer.appendChild(el); _hlEl = el;
  }
  function closePop() {
    var p = document.getElementById('fig-pop'); if (p) p.remove();
    if (_popTimer) { clearTimeout(_popTimer); _popTimer = null; }
    if (_popRepos) { window.removeEventListener('scroll', _popRepos, true); window.removeEventListener('resize', _popRepos); _popRepos = null; }
    if (_popOutside) { document.removeEventListener('pointerdown', _popOutside, true); _popOutside = null; }
    clearHl();
    _popBadge = null;
  }
  // 轻点徽标=描述浮层。共享模式(__uiShared)→ 走 PdfAdapter.figurePop → RC.figures.openPop(描述弹层 chrome 统一);
  //   YOLO 框高亮 showHl 是纯几何 → 两路都画,保留底座;RC 不可用 → fallback 回 _openFigPopNative(原逻辑逐字不变)。
  function openPop(badge, fig) {
    if (window.__uiShared && window.PdfAdapter) {
      showHl(badge, fig);                              // YOLO 框高亮 = 纯几何,保留底座
      var _bodyHtml = md(fig.desc);
      PdfAdapter.figurePop({
        badge: badge, caption: fig.caption || '图',
        body: _bodyHtml != null ? _bodyHtml : ('<p>' + esc(fig.desc).replace(/\n/g, '<br>') + '</p>'),
        ignoreSelector: '.fig-badge, .fig-hit',        // PDF 徽标/命中层放行,再点同徽标正常 toggle
        fallback: function () { _openFigPopNative(badge, fig); }
      });
      return;
    }
    return _openFigPopNative(badge, fig);
  }
  function _openFigPopNative(badge, fig) {
    if (_popBadge === badge) { closePop(); return; }   // 再点同一徽标 → 关
    closePop();
    _popBadge = badge;
    showHl(badge, fig);                                // 高亮该图范围
    var pop = document.createElement('div'); pop.id = 'fig-pop'; pop.className = 'fig-pop';
    var body = md(fig.desc);
    pop.innerHTML = '<span class="fig-x">✕</span>' +
      (fig.caption ? '<h4>' + esc(fig.caption) + '</h4>' : '<h4>图</h4>') +
      '<div class="fig-body">' + (body != null ? body : ('<p>' + esc(fig.desc).replace(/\n/g, '<br>') + '</p>')) + '</div>';
    document.body.appendChild(pop);
    pop.querySelector('.fig-x').addEventListener('click', closePop);
    // 点浮层 / 徽标 / 图区命中层 之外 → 关(点空白处自动消失)。capture 在 pointerdown 早于各页 handler。
    // openPop 由徽标 pointerup 触发,本监听挂上时开启的那次 pointerdown 已过,不会自关。
    _popOutside = function (ev) {
      var t = ev.target;
      if (t.closest && (t.closest('#fig-pop') || t.closest('.fig-badge') || t.closest('.fig-hit'))) return;
      closePop();
    };
    document.addEventListener('pointerdown', _popOutside, true);
    // 上下方定一次(防滚动时反复翻转抖动);左右跟徽标(夹进视口)
    var ph = pop.getBoundingClientRect().height;
    var placeBelow = (badge.getBoundingClientRect().bottom + 8 + ph) <= (window.innerHeight - 8);
    function reposition() {
      if (!badge.isConnected) { closePop(); return; }
      var br = badge.getBoundingClientRect(), pw = pop.getBoundingClientRect().width;
      pop.style.left = Math.min(Math.max(8, br.left), window.innerWidth - pw - 8) + 'px';
      pop.style.top = (placeBelow ? br.bottom + 8 : br.top - pop.getBoundingClientRect().height - 8) + 'px';
      // 徽标(图)滚出视口=显示不了 → 几秒后自动关;滚回可见 → 取消关闭。可见时一直跟着图,不关。
      if (br.bottom <= 0 || br.top >= window.innerHeight) { if (!_popTimer) _popTimer = setTimeout(closePop, 3000); }
      else if (_popTimer) { clearTimeout(_popTimer); _popTimer = null; }
    }
    reposition();
    var _raf = 0;
    _popRepos = function () { if (!_raf) _raf = requestAnimationFrame(function () { _raf = 0; reposition(); }); };
    window.addEventListener('scroll', _popRepos, true);   // capture:任何滚动容器(连续模式 #main/window)都跟
    window.addEventListener('resize', _popRepos);
    try { if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([pop]).catch(function () {}); } catch (_) {}
  }

  function draw(pw, num) {
    var rec = _cache[num]; if (!rec || !rec.figs || !rec.figs.length) return;
    var layer = (typeof ensurePageLayer === 'function') ? ensurePageLayer(pw, 'fig-layer') : null;
    if (!layer) return;
    layer.style.pointerEvents = 'none';   // 层穿透,只徽标可点
    layer.innerHTML = '';
    var canvas = pw.querySelector('canvas');
    var cssW = (canvas && canvas.clientWidth) || pw.clientWidth, cssH = (canvas && canvas.clientHeight) || pw.clientHeight;
    // 去边模式:fig-layer(带 page-layer 类)被撑成**整张位图** --full-w/h 并 translate(-cropL,-cropT);
    // 徽标归一坐标也是整图基准 → 必须用整图尺寸定位,别用裁后的 clientWidth(否则错位/落到裁掉区被 overflow 裁没)。
    if (pw.classList.contains('crop-on')) {
      var fw = parseFloat(pw.style.getPropertyValue('--full-w')), fh = parseFloat(pw.style.getPropertyValue('--full-h'));
      if (fw > 0 && fh > 0) { cssW = fw; cssH = fh; }
    }
    if (!cssW || !cssH) return;
    // 徽标贴在**图自己的某个角**(离图心远、在图上而非正文上),用 char boxes 选一个**不压字**的角。
    // 图区本身没有 OCR 文字 → 图内的角天然空;text 检查主要挡 bbox 略大溢到邻近正文的情况。
    var boxes = pw.__charBoxes || [];   // {left,top,width,height} CSS px(08-charlayer 设)
    var S = 26, M = 3;
    function hitsText(x, y) {
      for (var i = 0; i < boxes.length; i++) {
        var bx = boxes[i]; if (bx.sp) continue;
        if (bx.left < x + S && bx.left + bx.width > x && bx.top < y + S && bx.top + bx.height > y) return true;
      }
      return false;
    }
    function clampX(x) { return Math.max(1, Math.min(cssW - S - 1, x)); }
    function clampY(y) { return Math.max(1, Math.min(cssH - S - 1, y)); }
    rec.figs.forEach(function (f) {
      var pos = null;
      // 服务端预算好的锚点(归一,徽标中心)= 贴着图的空白角,跨加载位置一致 → 直接用
      if (f.badge && f.badge.length === 2) {
        pos = [clampX(f.badge[0] * cssW - S / 2), clampY(f.badge[1] * cssH - S / 2)];
      } else {
        // 回退:旧启发(图框四角避开正文)。badge 缺(后端还没算好)时临时用。优先用收紧后的真实图框 fbox
        var rawb = (f.fbox && f.fbox.length === 4) ? f.fbox : f.bbox;
        var bb = (rawb && rawb.length === 4 && rawb[2] > rawb[0] && rawb[3] > rawb[1]) ? rawb : [0.02, 0.03, 0.1, 0.1];
        var fx0 = bb[0] * cssW, fy0 = bb[1] * cssH, fx1 = bb[2] * cssW, fy1 = bb[3] * cssH;
        var cands = [[fx1 - S - M, fy0 + M], [fx0 + M, fy0 + M], [fx1 - S - M, fy1 - S - M], [fx0 + M, fy1 - S - M]];
        for (var i = 0; i < cands.length; i++) {
          var x = clampX(cands[i][0]), y = clampY(cands[i][1]);
          if (!hitsText(x, y)) { pos = [x, y]; break; }
        }
        if (!pos) { pos = [clampX((fx0 + fx1) / 2 - S / 2), clampY((fy0 + fy1) / 2 - S / 2)]; }
      }
      // 图区命中层:点/长按图(非徽标)→ 设为「焦点图」+ 高亮范围(不弹描述);供助手据图回答 + 拖进对话
      var hb = (f.fbox && f.fbox.length === 4) ? f.fbox : f.bbox;
      if (hb && hb.length === 4 && hb[2] > hb[0] && hb[3] > hb[1]) {
        var hit = document.createElement('div'); hit.className = 'fig-hit';
        hit.style.left = (hb[0] * cssW) + 'px'; hit.style.top = (hb[1] * cssH) + 'px';
        hit.style.width = Math.max(8, (hb[2] - hb[0]) * cssW) + 'px';
        hit.style.height = Math.max(8, (hb[3] - hb[1]) * cssH) + 'px';
        hit.style.pointerEvents = 'auto';
        _bindFigHit(hit, f, num);
        layer.appendChild(hit);
      }
      var b = document.createElement('div'); b.className = 'fig-badge'; b.innerHTML = PHOTO_SVG;
      b.style.left = pos[0] + 'px'; b.style.top = pos[1] + 'px'; b.style.pointerEvents = 'auto';
      b.title = f.caption || '图说明';
      _bindBadge(b, f, num);    // 轻点=描述浮层;长按=拖进助手对话(徽标当拖拽把手,小不挡滚动)
      layer.appendChild(b);
    });
    _paintSelHls();   // 翻页/重绘后重画本页(及所有已加载页)的持续选中高亮,否则翻回来选中框丢
  }

  // ── 焦点图:点/长按图区 → 设 window.__figFocus + 高亮;长按拖动 → ghost 拖进右侧助手栏入上下文 ──
  function _figHasInk(num, bb) {
    try {
      var sp = (window._ink && window._ink.byPage && window._ink.byPage[num]) || [];
      for (var i = 0; i < sp.length; i++) {
        var ps = sp[i].p || [];
        for (var j = 0; j < ps.length; j++) {
          if (ps[j][0] >= bb[0] && ps[j][0] <= bb[2] && ps[j][1] >= bb[1] && ps[j][1] <= bb[3]) return true;
        }
      }
    } catch (_) {}
    return false;
  }
  function _figInk(num, bb) {   // 收集落在图框内的笔迹(归一坐标),随图带给助手 → 服务端按它合成,不依赖墨迹保存时机
    var out = [];
    try {
      var sp = (window._ink && window._ink.byPage && window._ink.byPage[num]) || [];
      for (var i = 0; i < sp.length && out.length < 30; i++) {
        var s = sp[i], ps = s.p || [], inb = false;
        for (var j = 0; j < ps.length; j++) {
          if (ps[j][0] >= bb[0] && ps[j][0] <= bb[2] && ps[j][1] >= bb[1] && ps[j][1] <= bb[3]) { inb = true; break; }
        }
        if (inb) out.push({ t: s.t, c: s.c, w: s.w, p: ps.map(function (p) { return [+(+p[0]).toFixed(3), +(+p[1]).toFixed(3)]; }) });
      }
    } catch (_) {}
    return out;
  }
  window.__figInk = _figInk;   // 给 __voiceContext 在发消息时**实时**收集图内笔迹(画在 attach 之后也算)
  // 焦点/带入:点图=高亮 + 加入「带入列表」;拖图=加入列表。列表(window.__figAttached)是助手上下文,可多张
  function _figId(fig, num) {
    var bb = (fig.fbox && fig.fbox.length === 4) ? fig.fbox : fig.bbox;
    return num + ':' + bb.map(function (v) { return (+v).toFixed(3); }).join(',');
  }
  function _attachFig(fig, num) {
    var bb = (fig.fbox && fig.fbox.length === 4) ? fig.fbox : fig.bbox;
    if (!bb || bb.length !== 4) return;
    if (!window.__figAttached) window.__figAttached = [];
    var id = _figId(fig, num);
    if (!window.__figAttached.some(function (a) { return a.id === id; })) {
      window.__figAttached.push({
        id: id, file_rel: (typeof FILE_REL !== 'undefined' ? FILE_REL : ''), page: num, box: bb,
        caption: fig.caption || '', desc: fig.desc || '', group: !!fig.group, has_ink: _figHasInk(num, bb)
      });
    }
    _renderChips();
  }
  // 持续选中高亮(跟 __figAttached 同步,与临时 _hlEl 分开;**绝不被 clearHl 清**)。
  // 遍历已加载页,对每张已带入的图在其 .fig-layer 画一个持久 .fig-hl-sel(data-figid);先清旧的再重画,防重复。
  function _paintSelHls() {
    try {
      document.querySelectorAll('.fig-layer .fig-hl-sel').forEach(function (e) { e.remove(); });
      var list = window.__figAttached || [];
      if (!list.length) return;
      list.forEach(function (a) {
        var pw = document.querySelector('.page-wrap[data-page-num="' + a.page + '"]');
        if (!pw) return;
        var layer = pw.querySelector('.fig-layer'); if (!layer) return;
        if (layer.querySelector('.fig-hl-sel[data-figid="' + a.id + '"]')) return;
        var bb = a.box; if (!bb || bb.length !== 4) return;
        var canvas = pw.querySelector('canvas');
        var cssW = (canvas && canvas.clientWidth) || pw.clientWidth, cssH = (canvas && canvas.clientHeight) || pw.clientHeight;
        if (pw.classList.contains('crop-on')) {   // 去边:层被撑成整图 → 用整图尺寸换算(同 showHl/draw)
          var fw = parseFloat(pw.style.getPropertyValue('--full-w')), fh = parseFloat(pw.style.getPropertyValue('--full-h'));
          if (fw > 0 && fh > 0) { cssW = fw; cssH = fh; }
        }
        if (!cssW || !cssH) return;
        var el = document.createElement('div'); el.className = 'fig-hl-sel'; el.setAttribute('data-figid', a.id);
        el.style.left = (bb[0] * cssW) + 'px'; el.style.top = (bb[1] * cssH) + 'px';
        el.style.width = Math.max(2, (bb[2] - bb[0]) * cssW) + 'px';
        el.style.height = Math.max(2, (bb[3] - bb[1]) * cssH) + 'px';
        layer.appendChild(el);
      });
    } catch (_) {}
  }
  window.__paintFigSelHls = _paintSelHls;
  // 长按 toggle(**专给长按用**):已选中 → 移除(去高亮 + 去带入);未选中 → 加入带入 + 画持久高亮。
  function _toggleFig(fig, num) {
    var id = _figId(fig, num);
    var has = (window.__figAttached || []).some(function (a) { return a.id === id; });
    if (has) {
      window.__figAttached = (window.__figAttached || []).filter(function (a) { return a.id !== id; });
      _renderChips(); _paintSelHls();
      if (typeof _toast === 'function') _toast('已取消选中');
    } else {
      _attachFig(fig, num);   // 已 push + renderChips
      _paintSelHls();
      if (typeof _toast === 'function') _toast(fig.group ? '已带入这个图组' : '已带入这张图');
    }
  }
  function setFigFocus(fig, anchorEl, num) {
    closePop();
    var pw = (anchorEl && anchorEl.closest) ? anchorEl.closest('.page-wrap')
             : document.querySelector('.page-wrap[data-page-num="' + num + '"]');
    showHl(pw, fig);            // 高亮范围(纯视觉,跟 AI 无关,保留)
    if (!window.__asstOpen()) return;   // 助手没开 → 不带入对话(拖图进面板那条本就要面板开,不受影响)
    _attachFig(fig, num);       // 加入带入列表(多张)
    if (typeof _toast === 'function') _toast(fig.group ? '已带入这个图组' : '已带入这张图');
  }
  window.__setFigFocus = setFigFocus;

  function _cropUrlOf(a) {
    return '/pdf/api/figure-crop?file=' + encodeURIComponent(a.file_rel) + '&page=' + a.page +
           '&box=' + a.box.map(function (v) { return (+v).toFixed(4); }).join(',') + (a.has_ink ? '&ink=1' : '');
  }
  // 取该图的渲染图:有笔迹 → POST 当前笔迹让服务端合成(复用已对齐的管线),回 blob url;无笔迹 → 直接 GET url
  function _fetchComposite(a, cb) {
    var ink = (typeof _figInk === 'function') ? _figInk(a.page, a.box) : [];
    if (!ink.length) { cb(_cropUrlOf(a)); return; }
    fetch('/pdf/api/figure-crop', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file: a.file_rel, page: a.page, box: a.box, strokes: ink })
    }).then(function (r) { return r.ok ? r.blob() : null; })
      .then(function (b) { cb(b ? URL.createObjectURL(b) : _cropUrlOf(a)); })
      .catch(function () { cb(_cropUrlOf(a)); });
  }
  // 点缩略图 → 看大图(合成图,实时取当前笔迹)
  function _openFigLightbox(a) {
    var mask = document.createElement('div'); mask.className = 'fig-lightbox';
    var img = document.createElement('img'); img.alt = '';
    mask.appendChild(img); document.body.appendChild(mask);
    _fetchComposite(a, function (url) { img.src = url; });
    mask.addEventListener('click', function () { mask.remove(); });
  }
  // 助手对话上方「已带入的图」附件条列表:每张一个缩略图 + 图注 + ✕(可多张)
  function _renderChips() {
    try {
      var pane = document.getElementById('side-pane-asst');
      var input = pane && pane.querySelector('#asst-input');
      var list = window.__figAttached || [];
      var wrap = document.getElementById('asst-fig-chips');
      if (!list.length) { if (wrap) wrap.remove(); return; }
      if (!input) return;       // 助手还没建/没开 → 列表仍在上下文里,开了再渲
      if (!wrap) { wrap = document.createElement('div'); wrap.id = 'asst-fig-chips'; input.parentNode.insertBefore(wrap, input); }
      wrap.innerHTML = '';
      list.forEach(function (a) {
        var chip = document.createElement('div'); chip.className = 'asst-fig-chip';
        var img = document.createElement('img'); img.className = 'afc-thumb'; img.style.cursor = 'zoom-in';
        _fetchComposite(a, function (url) { img.src = url; });       // 有笔迹 → 缩略图显示合成图
        img.addEventListener('click', function () { _openFigLightbox(a); });   // 点缩略图 → 看大图
        var cap = document.createElement('span'); cap.className = 'afc-cap'; cap.textContent = (a.group ? '图组 · ' : '') + (a.caption || '图') + ' · p' + (window._dispPage ? window._dispPage(a.page) : a.page);
        var x = document.createElement('button'); x.className = 'afc-x'; x.textContent = '✕';
        x.addEventListener('click', function () { window.__figAttached = (window.__figAttached || []).filter(function (z) { return z.id !== a.id; }); _renderChips(); _paintSelHls(); });
        chip.appendChild(img); chip.appendChild(cap); chip.appendChild(x); wrap.appendChild(chip);
      });
    } catch (_) {}
  }
  window.__renderFigChips = _renderChips;       // 助手打开时可调一次,补渲(点图在开助手前发生的情况)
  window.__clearFigFocus = function () { window.__figAttached = []; _renderChips(); clearHl(); _paintSelHls(); };
  // 给「历史/上下文卡片」渲缩略图:a={file_rel,page,box,has_ink};live(刚发的那条) 有笔迹走 POST 实时合成,
  // 历史回看走 GET &ink=1(服务端读已保存的 sidecar 笔迹),都拿到带笔迹的合成图
  window.__figThumb = function (a, imgEl, live) {
    if (!a || !a.box || !imgEl) return;
    if (live && a.has_ink) { _fetchComposite(a, function (url) { imgEl.src = url; }); }
    else { imgEl.src = _cropUrlOf(a); }
  };
  window.__figLightbox = function (a) { try { _openFigLightbox(a); } catch (_) {} };

  // ── 焦点选区:把当前选中的公式/段落显示在右侧助手栏(表示「现在焦点在这部分」)──
  // 与图附件并列。公式 kind='formula'(MathJax 渲染),文字 kind='text'(片段)。
  window.__focusSel = null;   // {text, kind}
  function _renderFocusSel() {
    try {
      var pane = document.getElementById('side-pane-asst');
      var input = pane && pane.querySelector('#asst-input');
      var wrap = document.getElementById('asst-sel-chip');
      var fs = window.__focusSel;
      if (!fs || !fs.text) { if (wrap) wrap.remove(); return; }
      if (!input) return;       // 助手没开 → 数据仍在,开了再渲
      if (!wrap) { wrap = document.createElement('div'); wrap.id = 'asst-sel-chip'; input.parentNode.insertBefore(wrap, input); }
      wrap.innerHTML = '';
      var chip = document.createElement('div'); chip.className = 'asst-sel-chip-in ' + (fs.kind === 'formula' ? 'is-fml' : 'is-txt');
      var icon = document.createElement('span'); icon.className = 'asc-icon'; icon.textContent = fs.kind === 'formula' ? '🧮' : '¶';
      var body = document.createElement('span'); body.className = 'asc-body';
      if (fs.kind === 'formula') {
        var raw = fs.text.replace(/^\$+/, '').replace(/\$+$/, '');
        body.textContent = '\\(' + raw + '\\)';
      } else {
        body.textContent = fs.text.slice(0, 90) + (fs.text.length > 90 ? '…' : '');
      }
      var x = document.createElement('button'); x.className = 'asc-x'; x.textContent = '✕';
      x.addEventListener('click', function () { window.__clearFocusSel(); });
      chip.appendChild(icon); chip.appendChild(body); chip.appendChild(x); wrap.appendChild(chip);
      if (fs.kind === 'formula' && window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([body]).catch(function () {});
    } catch (_) {}
  }
  // AI 对话栏(右侧助手 tab)是否开着:开了才允许「点选/多选 → 加入对话上下文」
  // (用户要求:没开 AI 栏时点选/多选只做查词/翻译/高亮,别悄悄往对话塞焦点/带入图)
  window.__asstOpen = function () {
    try {
      // drawer=shared(已翻默认)把 #grammar-panel 改名 #ep-side;不认它 → p 恒 null → __setFocusSel 恒短路
      //   = PDF 选中的焦点 chip 永不显示(EPUB 那份本就查 #ep-side 所以正常)。两名都认,legacy 逃生舱不受影响。
      var p = document.getElementById('grammar-panel') || document.getElementById('ep-side');
      var a = document.getElementById('side-pane-asst');
      return !!(p && p.classList.contains('open') && a && a.classList.contains('active'));
    } catch (_) { return false; }
  };
  window.__setFocusSel = function (text, kind) {
    if (!window.__asstOpen()) return;   // 助手没开 → 不钉焦点入对话(选中本身仍能查词/翻译/高亮)
    text = (text || '').trim();
    if (!text) { window.__clearFocusSel(); return; }
    window.__focusSel = { text: text, kind: kind || 'text' };
    _renderFocusSel();
  };
  window.__clearFocusSel = function () {
    window.__focusSel = null; _renderFocusSel();
    // ✕ = "这个上下文别再带":连隐式选中兜底(lastSelText,10min 新鲜期)一起清,否则下条消息又悄悄带上(用户反馈)
    try { lastSelText = ''; window.__lastSelSentence = ''; window.__lastSelMeta = null; } catch (_) {}
  };
  window.__renderFocusSel = _renderFocusSel;

  // 点图/徽标/浮层/助手栏 之外 → 取消高亮框(__figFocus 上下文保留,由附件条 ✕ 才清)
  document.addEventListener('pointerdown', function (e) {
    if (!_hlEl) return;
    var t = e.target;
    if (t && t.closest && (t.closest('.fig-hit') || t.closest('.fig-badge') || t.closest('.fig-pop') ||
        t.closest('.rc-fig-pop') || t.closest('#rc-fig-pop') ||   // 共享模式:图描述浮层是 rc-figures 的 #rc-fig-pop/.rc-fig-pop,内部交互(选字/滚动)别清蓝框
        t.closest('#side-pane-asst') || t.closest('#grammar-panel'))) return;
    clearHl();
  }, true);

  // ── 长按拖动 → ghost 跟手 + 右侧助手栏冒「+」→ 拖进去入上下文 ──
  var _drag = null;
  function _figCropUrl(fig, num) {
    var bb = (fig.fbox && fig.fbox.length === 4) ? fig.fbox : fig.bbox;
    return '/pdf/api/figure-crop?file=' + encodeURIComponent(FILE_REL) + '&page=' + num +
           '&box=' + bb.map(function (v) { return (+v).toFixed(4); }).join(',') +
           (_figHasInk(num, bb) ? '&ink=1' : '');
  }
  function _asstPanel() { return document.getElementById('grammar-panel'); }
  function _overAsst(x, y) {
    var dp = _asstPanel(); if (!dp) return false;
    var r = dp.getBoundingClientRect();
    if (r.width < 10 || r.right < 10 || r.left > window.innerWidth - 4) return false;   // 抽屉没开
    return x >= r.left - 24 && x <= r.right + 4 && y >= r.top && y <= r.bottom;
  }
  function _dragStart(fig, num, e) {
    _dragCancel();
    var g = document.createElement('img'); g.className = 'fig-drag-ghost';
    g.src = _figCropUrl(fig, num); g.alt = '';
    document.body.appendChild(g);
    _drag = { fig: fig, num: num, ghost: g };
    var dp = _asstPanel();
    if (dp) {
      dp.classList.add('fig-drop-ready');
      var plus = document.createElement('div'); plus.id = 'fig-drop-plus'; plus.textContent = '＋';
      dp.appendChild(plus);
    }
    _positionGhost(e);
    if (navigator.vibrate) { try { navigator.vibrate(14); } catch (_) {} }
  }
  function _positionGhost(e) { if (_drag && _drag.ghost) { _drag.ghost.style.left = e.clientX + 'px'; _drag.ghost.style.top = e.clientY + 'px'; } }
  function _dragMove(e) {
    if (!_drag) return; _positionGhost(e);
    var dp = _asstPanel(); if (dp) dp.classList.toggle('fig-drop-over', _overAsst(e.clientX, e.clientY));
  }
  function _dragEnd(fig, num, e) {
    var over = _overAsst(e.clientX, e.clientY);
    _dragCancel();
    if (over) {
      _attachFig(fig, num);     // 拖到助手面板上松手 = 加入带入列表(支持多张)
      try { if (window.switchSideTab) window.switchSideTab('asst'); } catch (_) {}
      _renderChips();           // 切到助手 tab 后补渲一次(确保附件条出现)
      if (typeof _toast === 'function') _toast('📷 已带进助手对话');
    } else {   // 长按原地松手(位移未超阈值、没拖到助手)= toggle 选中(再长按同图 → 取消;只有拖到助手才是纯加入)
      try { _toggleFig(fig, num); } catch (_) {}
    }
  }
  function _dragCancel() {
    if (_drag && _drag.ghost) _drag.ghost.remove();
    _drag = null;
    var dp = _asstPanel();
    if (dp) { dp.classList.remove('fig-drop-ready'); dp.classList.remove('fig-drop-over'); var p = document.getElementById('fig-drop-plus'); if (p) p.remove(); }
  }

  // fig-hit(pointer-events:auto)盖在图上会挡住下层文字高亮 mark → 双击想进编辑被图吃掉。
  // 解法:fig-hit **只吃长按**(选中图 toggle);快速单击/双击 → 找出正下方的 .hl-saved,把这次 tap
  // 合成 PointerEvent down+up 转发给它,由它自己的 RC.highlight.gesture 累积成双击=进高亮编辑(见 17-highlight.js)。不动长按拖动路径。
  function _markBelow(x, y) {     // 暂时关掉所有 fig 层的命中,elementFromPoint 才看得到底下的高亮 mark
    var toggled = [];
    try {
      document.querySelectorAll('.fig-hit, .fig-badge').forEach(function (el) { toggled.push([el, el.style.pointerEvents]); el.style.pointerEvents = 'none'; });
      var el = document.elementFromPoint(x, y);
      return (el && el.closest) ? el.closest('.hl-saved') : null;
    } catch (_) { return null; }
    finally { toggled.forEach(function (p) { p[0].style.pointerEvents = p[1] || ''; }); }
  }
  function _forwardTap(mark, e) {   // 把这次轻点合成 down+up 转发给下层高亮 mark(驱动它自己的双击/编辑,不再被图挡)
    try {
      var opt = { bubbles: true, cancelable: true, composed: true, clientX: e.clientX, clientY: e.clientY,
                  pointerId: e.pointerId || 1, pointerType: e.pointerType || 'touch', isPrimary: true };
      mark.dispatchEvent(new PointerEvent('pointerdown', opt));
      mark.dispatchEvent(new PointerEvent('pointerup', opt));
    } catch (_) {}
  }
  // 图区命中层:只管轻点 → 设焦点(放开滚动,不拦拖拽)。拖拽统一走徽标(_bindBadge)
  // 整张图:轻点 → 设焦点;长按(380ms 不动)→ 拖进助手对话(整图当拖拽范围,不再只靠徽标)。
  // touch-action:pan-y 让竖滑照常滚;长按门控 = 没长按就移动当滚动放掉,长按后 setPointerCapture+切 .dragging(touch-action:none)+preventDefault 稳拖。
  function _bindFigHit(hit, fig, num) {
    var sx = 0, sy = 0, st = 0, lp = null, moved = false, dragging = false, pid = null;
    hit.addEventListener('pointerdown', function (e) {
      if (e.pointerType === 'mouse' && e.button !== 0) return;
      sx = e.clientX; sy = e.clientY; st = Date.now(); moved = false; dragging = false; pid = e.pointerId;
      lp = setTimeout(function () {
        if (!moved) { dragging = true; hit.classList.add('dragging'); _dragStart(fig, num, e); try { hit.setPointerCapture(pid); } catch (_) {} }
      }, 380);
    });
    hit.addEventListener('pointermove', function (e) {
      if (!moved && (Math.abs(e.clientX - sx) > 8 || Math.abs(e.clientY - sy) > 8)) moved = true;
      if (dragging) { _dragMove(e); e.preventDefault(); }
      else if (moved && lp) { clearTimeout(lp); lp = null; }   // 长按前就移动 = 滚动/划过 → 放弃拖动,让页面正常滚
    });
    hit.addEventListener('pointerup', function (e) {
      if (lp) { clearTimeout(lp); lp = null; }
      if (dragging) { dragging = false; hit.classList.remove('dragging'); try { hit.releasePointerCapture(pid); } catch (_) {} _dragEnd(fig, num, e); return; }
      if (!moved && Date.now() - st < 600) {   // 轻点:下面若叠着文字高亮 mark → 转发给它(累积成双击=进编辑,图不拦);否则**不做任何事**(用户 2026-07-22:单击不留蓝框视觉反馈)。带入统一走长按(见 _dragEnd)
        var _mk = null; try { _mk = _markBelow(e.clientX, e.clientY); } catch (_) {}
        if (_mk) { _forwardTap(_mk, e); }
      }
    });
    hit.addEventListener('pointercancel', function () { if (lp) { clearTimeout(lp); lp = null; } if (dragging) { dragging = false; hit.classList.remove('dragging'); _dragCancel(); } });
  }

  // 徽标:轻点 → 描述浮层;长按(380ms 不动)→ 拖拽进助手对话。徽标 touch-action:none + setPointerCapture → 拖拽稳,不会被滚动取消
  function _bindBadge(b, fig, num) {
    var sx = 0, sy = 0, st = 0, lp = null, moved = false, dragging = false, pid = null;
    b.addEventListener('pointerdown', function (e) {
      if (e.pointerType === 'mouse' && e.button !== 0) return;
      sx = e.clientX; sy = e.clientY; st = Date.now(); moved = false; dragging = false; pid = e.pointerId;
      lp = setTimeout(function () {
        if (!moved) { dragging = true; _dragStart(fig, num, e); try { b.setPointerCapture(pid); } catch (_) {} }
      }, 380);
    });
    b.addEventListener('pointermove', function (e) {
      if (!moved && (Math.abs(e.clientX - sx) > 7 || Math.abs(e.clientY - sy) > 7)) moved = true;
      if (dragging) { _dragMove(e); e.preventDefault(); }
      else if (moved && lp) { clearTimeout(lp); lp = null; }
    });
    b.addEventListener('pointerup', function (e) {
      if (lp) { clearTimeout(lp); lp = null; }
      if (dragging) { dragging = false; try { b.releasePointerCapture(pid); } catch (_) {} _dragEnd(fig, num, e); return; }
      if (!moved && Date.now() - st < 500) { e.stopPropagation(); openPop(b, fig); }   // 轻点 → 描述
    });
    b.addEventListener('pointercancel', function () { if (lp) { clearTimeout(lp); lp = null; } if (dragging) { dragging = false; _dragCancel(); } });
  }

  function _fetchFigs(pw, num) {
    fetch('/pdf/api/page-figures?file=' + encodeURIComponent(FILE_REL) + '&page=' + num)
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) return;
        _cache[num] = { figs: d.figures || [], pending: !!d.pending };
        draw(pw, num);
        if (d.pending) schedulePoll(pw, num);   // 还没描述完(或上次失败,后端会再触发)→ 轮询拿
      }).catch(function () {});
  }

  window.renderFiguresOnPage = function (pw, num) {
    if (!pw || !num || typeof FILE_REL === 'undefined' || !FILE_REL) return;
    if (!window.__figBookOn) return;   // 本书未开插图描述 → 不拉 page-figures、不画徽标、不烧 AI
    var rec = _cache[num];
    if (rec && !rec.pending) { draw(pw, num); return; }   // 已确定(有图/NONE)→ 直接画,不再打扰后端
    // 没拉过 或 还 pending(含上次描述失败的页)→ 重新拉,重置轮询给新机会(回看/重渲会重试)
    _poll[num] = 0;
    _fetchFigs(pw, num);
  };

  function schedulePoll(pw, num) {       // 后台描述 ~8-15s,轮询几次拿结果(只在该页仍在 DOM 时)
    if ((_poll[num] || 0) >= 8) return;
    _poll[num] = (_poll[num] || 0) + 1;
    setTimeout(function () { if (pw.isConnected) _fetchFigs(pw, num); }, 4500);
  }
})();
// 27-rc-adapter.js — 把模块作用域内部量喂给独立的 pdf-adapter.js。
// 仅 ui=shared 加载了 pdf-adapter.js → window.PdfAdapter 存在时执行;默认无 flag 时整段跳过。
// 阶段1 的 lookupWord 由门控点直接传 opts,不依赖此 bind;此段只为 captureSelection/clearSelection(阶段2)就位。
if (window.PdfAdapter && PdfAdapter.bind) {
  // vocabulary-state 可能在 reader.js 之前或之后完成 IndexedDB/扩展 Vault hydrate。
  // 两种顺序都只把 phrase 记录投影进现有 PDF 字符盒，不触发任何确认 GET。
  const _syncSharedPhraseState = (record) => {
    try {
      if (typeof window.__syncPhraseStateProjection !== 'function') return;
      if (record) {
        if (record.kind === 'phrase') window.__syncPhraseStateProjection([record]);
        return;
      }
      const repo = window.BWReaderRuntime && window.BWReaderRuntime.vocabularyState;
      if (repo && repo.CONTRACT === 'vocabulary-state/1' && typeof repo.snapshot === 'function') {
        window.__syncPhraseStateProjection(repo.snapshot());
      }
    } catch (_) {}
  };
  try {
    document.addEventListener('bw:vocabulary-state-change', (event) => {
      _syncSharedPhraseState(event && event.detail && event.detail.record);
    });
    document.addEventListener('bw:vocabulary-state-ready', () => _syncSharedPhraseState());
  } catch (_) {}
  _syncSharedPhraseState();
  // PDF 也接进统一中间层 RC._adapter(设计见 /reader-middlelayer-design.md);助手经 RC.adapter().getContext() 取上下文,
  // 与 EPUB 用完全一样的方式。实况网页已经由 parser 阶段的 WebAdapter 独立登记；这里仍 bind
  // PDF 私有 host 供共享 UI 组合，但绝不能用 PdfAdapter 把当前 kind=web 的 DocumentHost 覆盖掉。
  const _readerIsWebHost = !!(window.__PDF_CFG && window.__PDF_CFG.web_url);
  try { if (!_readerIsWebHost && window.RC && RC.use) RC.use(PdfAdapter); } catch (e) {}
  function _pdfNoteGeom(pw) {
    // #51 去边(crop)坐标统一:便签/反馈层都挂进 .crop-on 的 page-wrap→吃 CSS translate(-cropL,-cropT)。
    // 以 char-layer(撑满整页 --full-w、与便签同吃 translate)的实时屏幕 BCR 为基准 → crop/zoom/祖先缩放
    // 全自动含在 BCR 里,三条链(拖动反馈 noteWordRect/mount、松手 anchorFromPoint→noteMount)同一坐标系。
    // char-layer 未就绪时回退 pw BCR + crop 变量手算整页屏幕投影。
    var cl = pw.__charLayer;
    var isCrop = pw.classList && pw.classList.contains('crop-on');
    var cropL = isCrop ? (parseFloat(pw.style.getPropertyValue('--crop-l')) || 0) : 0;
    var cropT = isCrop ? (parseFloat(pw.style.getPropertyValue('--crop-t')) || 0) : 0;
    var fullW = isCrop ? (parseFloat(pw.style.getPropertyValue('--full-w')) || pw.clientWidth) : pw.clientWidth;
    var fullH = isCrop ? (parseFloat(pw.style.getPropertyValue('--full-h')) || pw.clientHeight) : pw.clientHeight;
    if (cl) {
      var clr = cl.getBoundingClientRect();
      if (clr.width && clr.height) {
        return { left: clr.left, top: clr.top, sw: clr.width, sh: clr.height,
                 layW: cl.clientWidth || fullW, layH: cl.clientHeight || fullH, fullW: fullW, fullH: fullH };
      }
    }
    var pr = pw.getBoundingClientRect();
    var S = pr.width / (pw.clientWidth || 1) || 1;
    return { left: pr.left - cropL * S, top: pr.top - cropT * S,
             sw: fullW * S, sh: fullH * S, layW: fullW, layH: fullH, fullW: fullW, fullH: fullH };
  }
  PdfAdapter.bind({
    charSel: () => _charSel,
    lastSelText: () => lastSelText,
    selPageNum: () => (typeof _selPageNum === 'function' ? _selPageNum() : currentPage),
    currentPage: () => (typeof currentPage !== 'undefined' ? currentPage : 0),
    pageCount: () => { try { return pdfDoc ? pdfDoc.numPages : 0; } catch (_) { return 0; } },
    fileRel: () => FILE_REL,
    bookLangs: () => BOOK_LANGS,
    sentence: (cs) => {
      try { const ch = cs.pw.__charBoxes; const r = _expandSentenceFromRange(ch, cs.startIdx, cs.endIdx); return r ? _charsRangeToText(ch, r.start, r.end).slice(0, 600) : ''; }
      catch (_) { return ''; }
    },
    clear: () => {
      try { _charSel = null; lastSelText = ''; _updateSelPreview(''); document.querySelectorAll('.sel-overlay').forEach(o => o.innerHTML = ''); toolbar.classList.remove('open'); }
      catch (_) {}
    },
    // 查词呼吸高亮(rc-wordpop opts.breathe):照抄原生 _materializeWordHl+renderWordHl 单个 hl 的渲染路径——
    // 页面坐标系 rects 渲进 pw 内的 .word-hl-layer(pw 随 #main 滚动,层天然跟滚零漂移,这正是原生不会错位的原因;
    // 共享层此前用 position:fixed 兜底,滚动手动平移必有窗口期漂移)。cap=查词时捕获的 {pw,startIdx,endIdx}
    // (对象字段不会被后续选中改写,_charSel 变化是换引用)。呼吸/常亮由共享层切 layer 的 .breathe class,
    // 正好对齐原生 CSS `.word-hl-layer.breathe .hl`(renderWordHl 的 `h.ready ? '' : ' breathe'` 同一套类名)。
    // furigana 注音(原生 renderWordHl 426-433:等待时把日读音/英音标标在词上方)一并照搬。不进原生 _wordHls
    // 数组(生命周期由共享层 _wordHls 管),原生数组在共享模式下保持空,互不冲突。
    wordHlWrap: (cap) => {
      try {
        const pw = cap && cap.pw;
        if (!pw || !pw.__charBoxes) return null;
        const rects = _charRangeToPtRects(pw.__charBoxes, cap.startIdx, cap.endIdx);
        if (!rects.length) return null;
        const canvas = pw.querySelector('canvas');
        const cssW = canvas?.clientWidth || pw.clientWidth, cssH = canvas?.clientHeight || pw.clientHeight;
        const pageWPt = pw.__pageWPt || cssW, pageHPt = pw.__pageHPt || cssH;
        if (!cssW || !cssH || !pageWPt || !pageHPt) return null;
        const sx = cssW / pageWPt, sy = cssH / pageHPt;
        const layer = document.createElement('div');
        layer.className = 'word-hl-layer';
        for (const r of rects) {
          const d = document.createElement('div'); d.className = 'hl';
          d.style.left = (r[0] * sx) + 'px'; d.style.top = (r[1] * sy) + 'px';
          d.style.width = ((r[2] - r[0]) * sx) + 'px'; d.style.height = ((r[3] - r[1]) * sy) + 'px';
          layer.appendChild(d);
        }
        const _hits = _furiHitsForRects(pw, rects);
        if (_hits.length) {
          const rl = document.createElement('div');
          rl.className = 'ruby-layer';
          for (const it of _hits) { const sp = _makeRubySpan(it, sx, sy); if (sp) rl.appendChild(sp); }
          layer.appendChild(rl);
        }
        const sel = pw.querySelector('.sel-overlay'); if (sel) sel.innerHTML = '';   // 移交持久层(蓝选区→呼吸高亮,原生 456)
        pw.appendChild(layer);
        return layer;
      } catch (_) { return null; }
    },
    // 查词小框定位:直接复用原生 _positionWordPop(absolute-in-#main,随内容滚动;fixed 视口定位不跟滚)。
    // cs = 查词时捕获的 charSel 快照(pdf-adapter 的 positionPop hook 闭包传入)。
    positionWordPop: (pop, cs) => { try { _positionWordPop(pop, cs); } catch (_) {} },
    // 便签(rc-stickynote,设计见 references/sticky-notes-design.md「规格 v4」):挂载/锚定 per-reader——
    // mount=对应 pw(页未渲染→null,由 04-render 渲染完成点 mountPending 补挂);锚点=page+归一化 x/y(PDF 不改)。
    // v4 契约:mount 返回容器内**像素** left/top(定位机制归 host,组件只应用)。PDF=x·clientWidth/y·clientHeight,
    // 行为等价旧组件自算 %;页面重渲/缩放后 04-render 两处 mountPending → ensureMounted 用新 clientWidth 重算。
    noteMount: (anchor) => {
      if (!anchor || anchor.kind !== 'pdf') return null;
      const pw = document.querySelector('.page-wrap[data-page-num="' + anchor.page + '"]');
      if (!pw || pw.dataset.loaded !== '1') return null;
      const x = Math.max(0, Math.min(1, anchor.x || 0)), y = Math.max(0, Math.min(1, anchor.y || 0));
      const g = _pdfNoteGeom(pw);   // #51 crop:anchor=整页比例;便签 append 进 .crop-on 吃 translate → left 用整页布局坐标即对齐
      return { el: pw, left: x * g.fullW, top: y * g.fullH };
    },
    noteWordRect: (x, y) => {
      // #51 粒度=单词(用户设计):最近字符→同 w 词聚合精确框;dist(屏幕像素)给调用方判"超范围→横线"
      const t = document.elementFromPoint(x, y);
      const pw = t && t.closest ? t.closest('.page-wrap') : null;
      const cbs = pw && pw.__charBoxes;
      if (!cbs || !cbs.length) return null;
      // #51 charBox=建层整页布局坐标(__charsBaseW=建层整页布局宽);去边/缩放全含在 g(char-layer 实时 BCR)。
      // 屏幕点→建层布局坐标(dispK=屏幕/建层布局)与 charBox 同系匹配;返回当前布局坐标(Klay)供 _afx 挂进吃 translate 的 pw。
      const g = _pdfNoteGeom(pw);
      const baseW = pw.__charsBaseW || g.layW || 1;
      const dispK = (g.sw / baseW) || 1;      // 建层布局 → 当前屏幕像素
      const Klay = (g.layW / baseW) || 1;     // 建层布局 → 当前布局
      const px = (x - g.left) / dispK, py = (y - g.top) / dispK;   // 屏幕 → 建层布局坐标
      let best = null, bd = 1e18, bestRow = null, brd = 1e18;
      for (const cb of cbs) {
        if (cb.sp || !cb.width) continue;
        const cx = cb.left + cb.width / 2, cy = cb.top + cb.height / 2;
        // 行优先(用户拍板 2026-07-21:锁**左上角左侧同行**文字,非斜上方——锚语义"插在这段文字之后"):
        // 字符行高带内(±0.75字高)且在探测点左侧 → 按水平距离取最近;同行没有才退全局欧氏最近
        const rowOk = Math.abs(cy - py) <= Math.max(cb.height, 14) * 0.75;
        if (rowOk && cx <= px) {
          const dh = px - cx;
          if (dh < brd) { brd = dh; bestRow = cb; }
        }
        const d = (cx - px) * (cx - px) + (cy - py) * (cy - py);
        if (d < bd) { bd = d; best = cb; }
      }
      if (bestRow) { best = bestRow; bd = brd * brd; }
      if (!best) return null;
      let L = best.left, T = best.top, R2 = best.left + best.width, B = best.top + best.height;
      if (best.w !== -1) for (const cb of cbs) {
        if (cb.w !== best.w || cb.sp) continue;
        L = Math.min(L, cb.left); T = Math.min(T, cb.top);
        R2 = Math.max(R2, cb.left + cb.width); B = Math.max(B, cb.top + cb.height);
      }
      return { el: pw, left: L * Klay, top: T * Klay, width: (R2 - L) * Klay, height: (B - T) * Klay, dist: Math.sqrt(bd) * dispK };
    },
    noteAnchorFromPoint: (x, y) => {
      const t = document.elementFromPoint(x, y);
      let pw = t && t.closest ? t.closest('.page-wrap') : null;
      if (!pw || pw.dataset.loaded !== '1') {
        // 任何位置都能钉(用户拍板 2026-07-20):点不在页上(页缝/灰区/被浮层元素挡)→ 找**最近的已渲染页**,
        // 坐标 clamp 进页——钉页缝=贴上一页底部、水平位置保持(="内容排到上方最后的内容之后")
        pw = null; let best = 1e18;
        document.querySelectorAll('.page-wrap[data-loaded="1"]').forEach((p2) => {
          const r2 = p2.getBoundingClientRect();
          if (!r2.width || !r2.height) return;
          const dx = x < r2.left ? r2.left - x : (x > r2.right ? x - r2.right : 0);
          const dy = y < r2.top ? r2.top - y : (y > r2.bottom ? y - r2.bottom : 0);
          const d2 = dx * dx + dy * dy;
          if (d2 < best) { best = d2; pw = p2; }
        });
        if (!pw) return null;
      }
      const g = _pdfNoteGeom(pw);   // #51 crop:整页比例(char-layer 屏幕投影,含 translate)
      if (!g.sw || !g.sh) return null;
      const _cl = !(t && t.closest && t.closest('.page-wrap') === pw);   // 走了最近页 fallback=clamped(反馈层画插入横线)
      const a0 = { kind: 'pdf', page: parseInt(pw.dataset.pageNum || '0', 10) || 0,
               x: Math.max(0, Math.min(1, (x - g.left) / g.sw)),
               y: Math.max(0, Math.min(1, (y - g.top) / g.sh)) };
      if (_cl) a0.clamped = 1;
      return a0;
    },
    // 阶段2 词组(rc-phrasepop):呼吸高亮层是 PDF 字符层几何 → 留底座,adapter 只接管查询+小框渲染。
    phraseHighlight: () => { try { return _showPhraseHighlight(_charSel && _charSel.pw); } catch (_) { return null; } },   // 返回本高亮 → onSolid 精确标它(并发多查询各标各的)
    phraseSolid: (hl) => { try { if (!hl) return; hl.solid = true; const _l = document.querySelector('.phrase-hl-layer[data-phid="' + hl.id + '"]'); if (_l) { _l.classList.remove('breathe'); _l.querySelectorAll('.hl').forEach(el => el.style.pointerEvents = 'auto'); } } catch (_) {} },   // 不回退标"最后一个"(并发查询会误标最新那个)
    // 共享词组 UI 已把 vocabulary-state 作为事实源；宿主只负责本页几何投影。
    // 收藏分词和掌握下划线都能用现有字符盒在本地重算，不能再为了“确认”拉整页 chars。
    phraseFavoriteUpdate: (text, enabled) => {
      try {
        const key = String(text || '').replace(/[\s\u3000]+/g, '');
        if (!key) return;
        if (enabled) _phraseFavSet.add(key); else _phraseFavSet.delete(key);
        _applyPhraseMergesAll();
        if (enabled) _pfavPaint(key); else _pfavUnpaint(key);
      } catch (_) {}
    },
    phraseMasteryUpdate: (text, enabled) => {
      try {
        const key = _phraseNorm(text);
        if (!key) return;
        if (enabled) _phraseMarkSet.add(key); else _phraseMarkSet.delete(key);
        document.querySelectorAll('[data-loaded="1"][data-page-num]').forEach((pw) => {
          try { renderVocabUnderlines(pw, pw.__vocabMarks || []); } catch (_) {}
        });
      } catch (_) {}
    },
    // 仅供旧 adapter 降级；新共享路径不得调用这个联网重扫。
    phraseRefresh: () => { try { refreshCharsWForAllPages(); } catch (_) {} },
    removePhraseHighlight: (arg) => { try { _removePhraseHighlight(arg); } catch (_) {} },
    // 词组框「💡 解释」→ 复用 PDF onExplain(它自身也被门控,共享模式下转 rc-result)。
    onExplain: () => { try { window.onExplain && window.onExplain(); } catch (_) {} },
    // 阶段2(补丁):「解释」的琥珀色呼吸高亮 + 后台跑 + 点高亮才开面板 —— 100% 原样复用 PDF 原生
    //   _showExplainHighlight/_runExplainBg(15-phrase-wordpop.js / 21-misc-ai.js,函数体一字未改;
    //   两者内部已按 __uiShared 分流到 rc-result 的 openResult/addResultPickers/_resultReqId)。
    //   这层只是把 PdfAdapter.explain 的调用点接过去,等价 native onExplain 共享分支之前本该执行的那两行。
    explainHighlight: (text) => {
      try {
        const ehl = _showExplainHighlight(_charSel && _charSel.pw, text);
        if (ehl) { ehl.title = '💡 AI 解释'; ehl.src = text; ehl.resultContext = _resultContext; }
        return ehl;
      } catch (_) { return null; }
    },
    runExplainBg: (hl, text, context) => { try { _runExplainBg(hl, text, context); } catch (_) {} },
    // 阶段3 高亮列表(PdfAdapter.renderHighlightList)就位:数据/跳转/删除经底座(高亮叠层渲染本身留 PDF 字符层几何,不迁;
    //   PDF 暂无「高亮抽屉」UI → 这几个钩子目前无 live 调用方,先就位)。编辑/图描述浮层走 per-call opts,不依赖此 bind。
    allHighlights: () => (typeof _allHighlights !== 'undefined' ? _allHighlights : []),
    jumpToHl: (hl) => { try { if (hl && hl.page && window.goToPage) window.goToPage(hl.page); } catch (_) {} },
    hlDelete: (hl) => { try { var pw = document.querySelector('.page-wrap[data-page-num="' + (hl && hl.page) + '"]'); if (typeof _hlDelete === 'function') _hlDelete(hl, pw); } catch (_) {} },
    // ── 助手侧栏共享化(②a):把 25-assistant.js 依赖的**全部宿主符号**收进 asst host 袋,供未来搬进
    //    rc-assistant.js 的共享侧栏经 RC.adapter()._host.asst 取用(EPUB 提供同名袋 → 复用整份侧栏)。
    //    全是**纯转发**到 PDF 现有 window.* / reader.js 作用域符号(本文件同在 reader.js IIFE)→ PDF 行为零变化;
    //    typeof 守卫保证任一符号缺失也不炸。live 值(fileRel/pdfNumPages/noteAttached/activePhraseHl/focusSel)用 getter。
    asst: {
      md: (t) => { try { return md(t); } catch (_) { return (t == null ? '' : String(t)); } },
      toast: (m) => { try { if (typeof _toast === 'function') _toast(m); } catch (_) {} },
      fmtTime: (ms) => { try { return _qhFmtTime(ms); } catch (_) { return ''; } },
      fileRel: () => (typeof FILE_REL !== 'undefined' ? FILE_REL : ''),
      pdfNumPages: () => { try { return pdfDoc ? pdfDoc.numPages : 0; } catch (_) { return 0; } },
      goTo: (p) => { try { window.jumpWithBack && window.jumpWithBack(p); } catch (_) {} },
      goToInBook: (fr, p) => { try { if (window.openBookAt) window.openBookAt(fr, p); else window.location.href = '/pdf/view?file=' + encodeURIComponent(fr) + '&page=' + p; } catch (_) {} },
      dispPage: (p) => { try { return window._dispPage ? window._dispPage(p) : p; } catch (_) { return p; } },
      pdfFromDisp: (d) => { try { return window._pdfFromDisp ? window._pdfFromDisp(d) : d; } catch (_) { return d; } },
      locCount: () => { try { return pdfDoc ? pdfDoc.numPages : 0; } catch (_) { return 0; } },
      changePage: (d) => { try { window.changePage && window.changePage(d); } catch (_) {} },
      fitWidth: () => { try { window.fitWidth && window.fitWidth(); } catch (_) {} },
      zoomBy: (d) => { try { window.zoomChange && window.zoomChange(d); } catch (_) {} },
      toggleTranslate: () => { try { window.togglePageTranslate && window.togglePageTranslate(); } catch (_) {} },
      openDrawer: () => { try { if (typeof openGrammarPanel === 'function') openGrammarPanel(); } catch (_) {} },
      switchTab: (n) => { try { window.switchSideTab && window.switchSideTab(n); } catch (_) {} },
      asstOpen: () => { try { return !!(window.__asstOpen && window.__asstOpen()); } catch (_) { return false; } },
      voiceContext: () => { try { return window.__voiceContext ? window.__voiceContext() : null; } catch (_) { return null; } },
      setFocusSel: (t, k) => { try { window.__setFocusSel && window.__setFocusSel(t, k); } catch (_) {} },
      focusSel: () => { try { return window.__focusSel || null; } catch (_) { return null; } },
      clearFigFocus: () => { try { window.__clearFigFocus && window.__clearFigFocus(); } catch (_) {} },
      figThumb: (d, img, live) => { try { window.__figThumb && window.__figThumb(d, img, live); } catch (_) {} },
      noteAttached: () => { try { return window.__noteAttached || []; } catch (_) { return []; } },
      clearNoteAttached: () => { try { window.__clearNoteAttached && window.__clearNoteAttached(); } catch (_) {} },
      renderNoteChips: () => { try { window.__renderNoteChips && window.__renderNoteChips(); } catch (_) {} },
      notesReload: () => { try { window.notesReload && window.notesReload(); } catch (_) {} },
      noteInject: (n) => { try { return !!(window.__noteInject && window.__noteInject(n)); } catch (_) { return false; } },
      reloadHighlights: () => { try { window._reloadHighlights && window._reloadHighlights(); } catch (_) {} },
      loadAllHighlights: () => { try { if (typeof loadAllHighlights === 'function') loadAllHighlights(); } catch (_) {} },
      renderHighlightsOnPage: (pw, n) => { try { if (typeof renderHighlightsOnPage === 'function') renderHighlightsOnPage(pw, n); } catch (_) {} },
      showHlPicker: (d) => { try { window._showHlPicker && window._showHlPicker(d); } catch (_) {} },
      assistEdit: (d) => { try { window._assistEdit && window._assistEdit(d); } catch (_) {} },
      renderPhraseHl: (w) => { try { if (typeof renderPhraseHl === 'function') renderPhraseHl(w); } catch (_) {} },
      removePhraseHighlight: (arg) => { try { if (typeof _removePhraseHighlight === 'function') _removePhraseHighlight(arg); } catch (_) {} },
      activePhraseHl: () => { try { return _phraseHls.length ? _phraseHls[_phraseHls.length - 1] : null; } catch (_) { return null; } },   // 多高亮:返回最近一个
      setActivePhraseHl: (v) => { try { if (v) { if (v.id == null) v.id = ++_phraseHlSeq; _phraseHls.push(v); } } catch (_) {} },   // ②b:侧栏 _flashSelOnPage 追加一个高亮(不再覆盖单例)
      charsRangeToText: (ch, a, b) => { try { return _charsRangeToText(ch, a, b); } catch (_) { return ''; } },
      charRangeToPtRects: (ch, a, b) => { try { return _charRangeToPtRects(ch, a, b); } catch (_) { return []; } },
      flashSelOnPage: (p, t) => { try { if (typeof _flashSelOnPage === 'function') _flashSelOnPage(p, t); } catch (_) {} },
      noteNearText: (a) => { try { return typeof _noteNearText === 'function' ? _noteNearText(a) : ''; } catch (_) { return ''; } },
      jumpToCtx: (m) => { try { if (typeof _jumpToCtx === 'function') _jumpToCtx(m); } catch (_) {} },
      prewarm: (off) => { try { window.__asstPrewarm && window.__asstPrewarm(off); } catch (_) {} },
      getPaidNoted: () => { try { return !!window.__paidNoted; } catch (_) { return false; } },
      setPaidNoted: (v) => { try { window.__paidNoted = v; } catch (_) {} },
      // ③-2:注解 CRUD 端点(reader 专属;EPUB 提供自己的)。body 语义(PDF rects vs EPUB cfi)差异由各 reader 后端/guard 处理;
      //   DELETE(id+file)/note-composite(file+id)两侧一致,POST create 的 rects 仅 PDF(EPUB items 无 rects→侧栏 guard 跳过)。
      hlUrl: () => '/pdf/api/highlights',
      notesUrl: () => '/pdf/api/notes',
      noteCompositeUrl: () => '/pdf/api/note-composite',
      // ③-4b:chat/history/clear 端点(PDF 默认=原字面量;EPUB host 覆盖成 epub-* 端点)
      chatUrl: () => '/api/assistant/chat',
      historyUrl: () => '/api/assistant/history',
      clearUrl: () => '/api/assistant/clear',
      // ③-3:挂载点容器(侧栏往里建 tab/pane/DOM)。PDF=右侧抽屉 #grammar-panel + tab 栏 #side-tabs;
      //   EPUB 提供 #ep-side + 其 tab 栏。抽屉可见性类(.side-pane/.side-tab)仍 PDF 专属,③-4 再抽 EPUB 集成。
      mountPanel: () => document.getElementById('ep-side') || document.getElementById('grammar-panel'),   // 唯一抽屉(drawer=shared,28-shared-drawer 把 #grammar-panel 改名 #ep-side)优先
      mountTabs: () => document.getElementById('ep-side-tabs') || document.getElementById('side-tabs')
    }
  });
  // 便签初始化(共享组件;opts 经上面 host-bind 的 noteMount/noteAnchorFromPoint;🗒 按钮在模板 ui_shared 块内)
  try {
    // Web 的 DOM/quote anchor 尚未完成，不能把 PDF noteMount/anchorFromPoint 偷渡进共享组件。
    // PDF 分支完全保持原初始化；网页便签/固定卡片继续列为独立 Web host pending。
    if (!_readerIsWebHost && window.RC && RC.stickynote) {
      RC.stickynote.init({
        file: FILE_REL,
        mount: (a) => PdfAdapter._host.noteMount(a),
        anchorFromPoint: (x, y) => PdfAdapter._host.noteAnchorFromPoint(x, y),
        noteWordRect: (x, y) => { try { return PdfAdapter._host.noteWordRect(x, y); } catch (_) { return null; } },   // #51 词粒度反馈
        // 阶段3 AI 注入:双击便签 → 25-assistant __noteInject(助手开着才处理:无笔画走文本通道,有笔画走合成图/视觉通道)
        onDoubleTap: (note) => { try { return !!(window.__noteInject && window.__noteInject(note)); } catch (_) { return false; } },
        toast: (m) => { try { _toast?.(m); } catch (_) {} }
      });
      window._noteCreateAtCenter = () => { try { RC.stickynote.createAtCenter(); } catch (_) {} };
    }
  } catch (_) {}
  // ②b 收尾:shared 模式(本块只在 ui=shared 下执行)bind 完(HOST 就绪)后**无条件**挂 rc-assistant 的共享侧栏;
  //     老 25-assistant 已在 __uiShared 下自退,不会双挂。legacy(?ui=legacy)本块不执行 → 走老 25-assistant 兜底。
  try {
    if (window.RC && RC.assistant && RC.assistant.mountPdfSidebar) RC.assistant.mountPdfSidebar();
  } catch (_) {}
}
// ═══════════ 28-shared-drawer.js — 唯一抽屉:rc-sidedrawer 接管 PDF 抽屉 chrome ═══════════
// 背景:此前 PDF/EPUB 各一套抽屉(PDF=模板静态 #grammar-panel+#side-tabs+18-grammar/05-nav 开合切换;
//   EPUB=rc-sidedrawer 动态 #ep-side),tab 集合也分叉(EPUB 有 高亮/目录,PDF 有 历史)。用户拍板「唯一存在」
//   → rc-sidedrawer 泛化为两 reader 唯一抽屉,本文件把 PDF 迁上去,并补齐 高亮/目录 两个 pane。
// 设计(映射见 references/…workflow wf_493a012e 地图):
//   · 镜像 body 类:rc-sidedrawer 开/悬浮时同步 body.grammar-open/.grammar-floating →
//     pdf-styles.css 既有挤压(#main/#header/#result-mask)/悬浮取消/窄屏规则原样生效,消费方 JS(12-vocab)零改。
//   · 静态 chrome(#side-handle/#side-tabs/#side-settings)摘除,由 rc-sidedrawer 自建(把手/tab 栏/⚙外观弹层);
//     抽屉根 #grammar-panel 改名 #ep-side(rc-sidedrawer 内部查询全按它;视觉底座换 rc 注入 CSS,同几何/同 z-index 120)。
//   · 4 个静态 pane(grammar/vocab/kg/hist)原地保留(加 .ep-side-pane 类,内部 id 全不动);
//     asst tab/pane 已由 rc-assistant 注入(27 先跑)→ tab 按钮搬进新 tab 栏,pane 就地换类。
//   · PDF 外观按排版分档(pdf-gp-*-{mode})经 opts.appearanceKeys 注入;重排经 opts.onReflow 走 _scheduleRefit;
//     双页临时切单列/还原走 opts.onLayoutChange;「🗑 清空分析」走 opts.tabButtons(仅 grammar tab 显示)。
//   · 旧入口(switchSideTab/openGrammarPanel/closeGrammarPanel/toggleGrammarPanel/toggleSidebar/toggleVocab/
//     _gpSet*/_gpApplyAppearance)全部改道 RC.sidedrawer,同名保留 → 26-figures/rc-assistant/模板兜底零改。
// 状态:浏览器验证通过(2026-07-07)→ 已翻默认;&drawer=legacy / #drawer=legacy = 旧 PDF 抽屉逃生舱
//   (旧抽屉 JS 的物理删除跟 ?ui=legacy 整体退役一个批次做,在那之前逃生舱免费)。
(() => {
  if (((location.search || '') + (location.hash || '')).indexOf('drawer=legacy') >= 0) return;
  if (!(window.RC && RC.sidedrawer && window.__uiShared)) return;
  const panel = document.getElementById('grammar-panel');
  if (!panel) return;
  window.__pdfSharedDrawer = true;

  // ── ① 接管前先保住 rc-assistant 注入的 asst tab(27 已跑,tab 在旧 #side-tabs 里)──
  const asstTabBtn = document.querySelector('#side-tabs .side-tab[data-pane="asst"]');
  if (asstTabBtn) asstTabBtn.remove();

  // ── ② 摘静态 chrome + 抽屉根改共享 id(清掉早期兜底可能留下的打开态)──
  ['side-handle', 'side-tabs', 'side-settings'].forEach(id => { const el = document.getElementById(id); if (el) el.remove(); });
  panel.classList.remove('open');
  document.body.classList.remove('grammar-open');
  panel.id = 'ep-side';
  panel.querySelectorAll('.side-pane').forEach(p => { p.classList.add('ep-side-pane'); p.classList.remove('active'); });

  // ── ③ 新 pane:高亮 / 目录(补齐与 EPUB 的 tab 对等)──
  const mkPane = (name, inner) => {
    const d = document.createElement('div');
    d.className = 'ep-side-pane'; d.dataset.pane = name; d.innerHTML = inner;
    panel.appendChild(d); return d;
  };
  mkPane('hl', '<div id="pdf-hl-list" style="padding:10px 12px"></div>');
  mkPane('toc', '<div id="pdf-toc-list" style="padding:10px 12px"></div>');

  // 高亮 pane:GET /pdf/api/highlights → rc-highlight.renderList(reader 无关,EPUB loadHlPane 同款)
  const _loadHlPane = () => {
    const box = document.getElementById('pdf-hl-list'); if (!box) return;
    box.innerHTML = '<div style="color:#5a6680;font-size:12px">加载…</div>';
    fetch('/pdf/api/highlights?file=' + encodeURIComponent(FILE_REL)).then(r => r.json()).then(d => {
      const hs = (d && d.highlights) || [];
      RC.highlight.renderList(box, hs, {
        reverse: true,
        emptyHtml: '还没有高亮。<br>选中文字 → 「🖍 高亮」',
        onJump: (h) => { try { jumpWithBack(h.page); RC.sidedrawer.afterJump(); } catch (_) {} },
        onDelete: (h) => {
          fetch('/pdf/api/highlights?file=' + encodeURIComponent(FILE_REL) + '&id=' + encodeURIComponent(h.id), { method: 'DELETE' })
            .then(() => { try { window._reloadHighlights && window._reloadHighlights(); } catch (_) {} })
            .catch(() => {});
        },
      });
    }).catch(() => { box.innerHTML = '<div style="color:#5a6680;font-size:12px">加载失败</div>'; });
  };

  // 目录 pane:GET /api/toc?entries=1(book_toc._effective_toc,page=印刷页)→ 简单列表(照 EPUB buildToc)
  let _tocLoadedOnce = false;
  const _loadTocPane = (force) => {
    const box = document.getElementById('pdf-toc-list'); if (!box) return;
    if (_tocLoadedOnce && !force) return;
    box.innerHTML = '<div style="color:#5a6680;font-size:12px">加载…</div>';
    fetch('/pdf/api/toc?file=' + encodeURIComponent(FILE_REL) + '&entries=1').then(r => r.json()).then(d => {
      const es = (d && d.entries) || [];
      _tocLoadedOnce = true;
      if (!es.length) { box.innerHTML = '<div style="color:#5a6680;font-size:12px;line-height:1.6">这本书还没有目录。<br>设置面板 →「书籍目录」可建立(原生书签或 AI 识别)。</div>'; return; }
      box.innerHTML = '';
      es.forEach(e => {
        const it = document.createElement('div');
        const lv = Math.max(0, (e.level || 1) - 1);
        it.textContent = e.title || '';
        it.title = '第 ' + (window._dispPage ? window._dispPage(e.page) : e.page) + ' 页';
        it.style.cssText = 'padding:6px 8px 6px ' + (8 + lv * 16) + 'px;font-size:' + (lv ? 12.5 : 13.5) + 'px;' +
          (lv ? 'color:#9aa7c4' : 'color:#dbe4f8;font-weight:600') + ';cursor:pointer;border-radius:6px;line-height:1.45';
        it.onmouseenter = () => { it.style.background = '#1a2540'; };
        it.onmouseleave = () => { it.style.background = ''; };
        it.onclick = () => {
          const pdfPage = (typeof _pdfFromDisp === 'function') ? _pdfFromDisp(e.page) : e.page;
          try { jumpWithBack(pdfPage); RC.sidedrawer.afterJump(); } catch (_) {}
        };
        box.appendChild(it);
      });
    }).catch(() => { box.innerHTML = '<div style="color:#5a6680;font-size:12px">加载失败</div>'; });
  };

  // ── ④ 唯一抽屉 init(tab 集合与 EPUB 同序:asst,vocab,kg,hl,toc,grammar + PDF 专属 hist 排尾)──
  const _si = (p) => '<svg class="si" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">' + p + '</svg>';
  RC.sidedrawer.init({
    tabs: [
      { name: 'vocab', label: '单词本', icon: _si('<path d="M6 4h11a1 1 0 0 1 1 1v15H8a2 2 0 0 1-2-2V4z"/><path d="M6 4a2 2 0 0 0-2 2v12a2 2 0 0 1 2-2h12"/>') },
      { name: 'kg', label: '知识点', icon: _si('<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1.2" fill="currentColor" stroke="none"/><circle cx="4.5" cy="12" r="1.2" fill="currentColor" stroke="none"/><circle cx="4.5" cy="18" r="1.2" fill="currentColor" stroke="none"/>') },
      { name: 'hl', label: '高亮', icon: _si('<path d="M4 20h6M14 4l6 6-8.5 8.5H7v-4.5L14 4z"/>') },
      { name: 'toc', label: '目录', icon: _si('<path d="M4 6h16M4 12h16M4 18h16"/>') },
      { name: 'grammar', label: '语法', icon: _si('<path d="M4 6h16M4 12h11M4 18h7"/>') },
      { name: 'hist', label: '历史', icon: _si('<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>') },
    ],
    handleLabel: '助手 · 知识点',
    defaultTab: 'asst',
    mirrorOpenClass: 'grammar-open',
    mirrorFloatingClass: 'grammar-floating',
    appearanceKeys: (name) => 'pdf-gp-' + name + '-' + _gpMode(),   // 按排版分档(18-grammar._gpMode,同模块作用域)
    onReflow: () => { try { if (!document.body.classList.contains('grammar-floating') && typeof _scheduleRefit === 'function') _scheduleRefit(true); } catch (_) {} },   // 悬浮不重排防闪(照搬 18-grammar)
    onWidthChange: () => { try { if (!document.body.classList.contains('grammar-floating') && typeof _scheduleRefit === 'function') _scheduleRefit(true); } catch (_) {} },
    tabButtons: [{
      id: 'side-clear', title: '清空全部分析', tabs: ['grammar'],
      icon: _si('<path d="M4 7h16M10 4h4a1 1 0 0 1 1 1v2H9V5a1 1 0 0 1 1-1zM6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13"/>'),
      onClick: () => { try { window.clearGrammarBlocks && window.clearGrammarBlocks(); } catch (_) {} },
    }],
    onLayoutChange: (willOpen) => {   // 双页 spread + 挤压模式:开抽屉临时切单列,关还原(逐字照搬 18-grammar)
      try {
        if (willOpen) {
          if (readMode === 'spread' && !document.body.classList.contains('grammar-floating')) {
            _spreadBeforePanel = _spreadOffset; readMode = 'continuous'; _updateModeButtons();
          } else {
            _spreadBeforePanel = null;   // 单列下开栏:清残留标记(修"单页开关侧栏被莫名切到双页")
          }
        } else {
          try { _hideDepTip(); } catch (_) {}
          if (_spreadBeforePanel != null && readMode === 'continuous') {   // 仅还原"确实被临时切走的"
            readMode = 'spread'; _spreadOffset = _spreadBeforePanel; _updateModeButtons();
          }
          _spreadBeforePanel = null;
        }
      } catch (_) {}
      return null;   // 重排走 onReflow(_scheduleRefit 自带防抖),不需要滚动锚点回调
    },
    onTab: (name) => {   // 懒加载分发(平移自 05-nav switchSideTab 四钩子 + 新 hl/toc + asst 镜像 EPUB)
      if (name === 'vocab') { if (!_vocabLoaded) loadVocabList(); }
      else if (name === 'grammar') { if (!_grammarHistLoaded) loadGrammarHistory(); }
      else if (name === 'kg') loadPageNodes(currentPage);
      else if (name === 'hist') renderQueryHistory();
      else if (name === 'hl') _loadHlPane();
      else if (name === 'toc') _loadTocPane();
      else if (name === 'asst') {
        try { window.__renderFigChips && window.__renderFigChips(); } catch (_) {}
        try { window.__renderNoteChips && window.__renderNoteChips(); } catch (_) {}
        try { window.__renderFocusSel && window.__renderFocusSel(); } catch (_) {}
        try { window.__asstPrewarm && window.__asstPrewarm(); } catch (_) {}
        setTimeout(() => { const ta = document.getElementById('asst-ta'); if (ta) ta.focus(); }, 120);
      }
    },
  });

  // ── ⑤ asst tab 归位:搬进新 tab 栏第一位(类名换共享;active 态与 init 同步过的 pane 对齐)──
  if (asstTabBtn) {
    asstTabBtn.classList.remove('side-tab'); asstTabBtn.classList.add('ep-side-tab');
    const bar = document.getElementById('ep-side-tabs');
    if (bar) bar.insertBefore(asstTabBtn, bar.firstChild);
    const act = panel.querySelector('.ep-side-pane.active');
    asstTabBtn.classList.toggle('active', !!(act && act.dataset.pane === 'asst'));
  }

  // ── ⑥ 旧入口全部改道(同名覆盖;调用方 26-figures/rc-assistant HOST/模板兜底零改)──
  window.switchSideTab = (p) => RC.sidedrawer.setTab(p);
  openGrammarPanel = () => RC.sidedrawer.open();          // 模块级绑定重指(18-grammar 内部 callers 一起改道)
  window.closeGrammarPanel = () => RC.sidedrawer.close();
  window.toggleGrammarPanel = () => {                     // 把手/顶栏按钮:开着且在助手 → 关;否则开到助手(原语义)
    const onAsst = document.querySelector('#ep-side-tabs .ep-side-tab[data-pane="asst"]')?.classList.contains('active');
    if (RC.sidedrawer.isOpen() && onAsst) { RC.sidedrawer.close(); return; }
    RC.sidedrawer.open('asst');
  };
  window.toggleSidebar = () => {                          // 「📋 知识点」按钮
    const on = document.querySelector('#ep-side-tabs .ep-side-tab[data-pane="kg"]')?.classList.contains('active');
    if (RC.sidedrawer.isOpen() && on) { RC.sidedrawer.close(); return; }
    RC.sidedrawer.open('kg');
  };
  window.toggleVocab = () => {                            // 「单词本」按钮
    const on = document.querySelector('#ep-side-tabs .ep-side-tab[data-pane="vocab"]')?.classList.contains('active');
    if (RC.sidedrawer.isOpen() && on) { RC.sidedrawer.close(); return; }
    RC.sidedrawer.open('vocab');
  };
  window._gpSetFloating = (on) => RC.sidedrawer.setFloating(on);
  window._gpSetBlur = (v) => RC.sidedrawer.setBlur(v);
  window._gpApplyAppearance = () => RC.sidedrawer.applyAppearance();   // 06-layout 切排版后调 → 按新档重应用
})();
// 29-optimistic-delete.js — 自建页乐观删除的**就地 reconcile**(不刷新页面)。
//   pdf-uishared.js 删除时乐观移除那页 DOM(窗口内不动别页页号→匹配旧文件);后台真删 job 全部完成后
//   调本函数按新文件对齐:重编号(DOM 顺序=正确新序)+ 更新页数/mtime + 软重取注解层。
//   **图片模式 + 连续排版**才做;spread / canvas / DOM 页数跟后端不一致 / 任何异常 → 返回 false 让调用方 reload 兜底。
//   放在最后一个源文件:整包同一个 module,运行时(用户删除后)所有模块级量已初始化,闭包按引用取。
window.__upReconcileDelete = function (newMeta) {
  try {
    if (!_imgMode || readMode !== 'continuous') { try{localStorage.setItem('_recon_dbg',((localStorage.getItem('_recon_dbg')||'')+'|gate:mode '+_imgMode+' '+readMode).slice(-1500));}catch(_){} return false; }   // 非图片/非连续:退回 reload(spread 行分组/canvas 字节都动不了)
    var M = newMeta && parseInt(newMeta.page_count, 10);
    if (!M || M < 1) { try{localStorage.setItem('_recon_dbg',((localStorage.getItem('_recon_dbg')||'')+'|gate:meta').slice(-1500));}catch(_){} return false; }
    var container = document.getElementById('page-container');
    if (!container) { try{localStorage.setItem('_recon_dbg',((localStorage.getItem('_recon_dbg')||'')+'|gate:container').slice(-1500));}catch(_){} return false; }
    // 页元素 = 真页 .page-wrap + 本会话虚拟插入页 .pdf-upage,按 **DOM 顺序**(= 视觉页序 = 正确新序)。
    var kids = Array.prototype.filter.call(container.children, function (el) {
      return el.classList && (el.classList.contains('page-wrap') || el.classList.contains('pdf-upage'));
    });
    if (kids.length > M && window._upJustDeleted) {
      // 自愈:实测(2026-07-17 heisenbug)删除后偶发多出一个页元素(异步挂载把刚删的页又放回来)→
      //   把 uid 在「刚删除」名单里的僵尸元素清掉再对账,而不是直接放弃 reload。
      kids = kids.filter(function (el) {
        var uid = el.dataset && el.dataset.uid;
        if (uid && window._upJustDeleted[uid]) { try { el.remove(); } catch (_) {} return false; }
        return true;
      });
    }
    if (kids.length !== M) {
      try {
        var pw2 = 0, up2 = 0, dup = {}, seen = {};
        kids.forEach(function (el) {
          if (el.classList.contains('pdf-upage')) up2++; else pw2++;
          var pn = el.dataset.pageNum || ('u:' + (el.dataset.uid || '?'));
          if (seen[pn]) dup[pn] = (dup[pn] || 1) + 1; seen[pn] = 1;
        });
        localStorage.setItem('_recon_dbg', ((localStorage.getItem('_recon_dbg') || '') + '|gate:count dom=' + kids.length + ' backend=' + M + ' pw=' + pw2 + ' up=' + up2 + ' dup=' + JSON.stringify(dup)).slice(-1500));
      } catch (_) {}
      return false;
    }   // DOM 页数跟后端不一致(有未完成插入 job / spread 残留等)→ 稳妥退回 reload
    // ① 元数据:页数 + mtime(页图请求带新版号 v → 命中新文件页、绕过 immutable/SW 旧缓存)
    if (typeof pdfDoc !== 'undefined' && pdfDoc) pdfDoc.numPages = M;
    if (window.__imgMeta) { window.__imgMeta.page_count = M; if (newMeta.mtime) window.__imgMeta.mtime = newMeta.mtime; }
    var pt = document.getElementById('page-total'); if (pt) pt.textContent = '/ ' + M;
    // ② 重编号(DOM 顺序 → 1..M):真页 dataset.pageNum;虚拟插入页同步 __upRec.page;未渲染占位文字改写。
    kids.forEach(function (el, i) {
      var nn = i + 1;
      if (parseInt(el.dataset.pageNum, 10) !== nn) {
        el.dataset.pageNum = String(nn);
        if (el.__upRec) el.__upRec.page = nn;
        if (el.dataset.loaded === '0' && /第\s*\d+\s*页/.test(el.textContent || '')) el.textContent = '… 第 ' + (window._dispPage ? window._dispPage(nn) : nn) + ' 页';
      }
    });
    if (typeof currentPage === 'number' && currentPage > M) currentPage = M;
    // ③ 清按页号的前端缓存(键已过期):页图档位 + 预取去重。已渲染页图内容不变(同一物理页),不必重取。
    try { for (var k in _imgRasterW) delete _imgRasterW[k]; } catch (e) {}
    try { _prefetched.clear(); } catch (e) {}
    // ④ 软重取注解层(后端已迁移 → 正确数据,各自按新页号重渲/重挂,幂等清旧)。都不刷新页面。
    try { if (window._inkLoadAll) window._inkLoadAll(); } catch (e) {}
    try { if (typeof loadAllHighlights === 'function') loadAllHighlights(); } catch (e) {}
    try { if (window.RC && RC.userpages && RC.userpages.load) RC.userpages.load(); } catch (e) {}
    try { if (window.RC && RC.stickynote && RC.stickynote.loadAll) RC.stickynote.loadAll(); } catch (e) {}
    return true;
  } catch (e) { try{localStorage.setItem('_recon_dbg',((localStorage.getItem('_recon_dbg')||'')+'|reconcile-throw:'+e.message).slice(-1500));}catch(_){} try { window.dlog && window.dlog('reconcile fail: ' + e.message, '#ff6b6b'); } catch (_) {} return false; }
};
// 30-dwell.js — 读页停留追踪(注意力画像的「读过这页」原始数据;设计 references/attention-kb-design.md)。
//   用户要求的严谨判定,三重排除全在采集端:
//   ① 卡加载排除:当前页的**页图真实渲染完成**(img.complete && naturalWidth>0)才计秒——加载不出来=看不见=不算读;
//   ② 快翻排除:按秒累计,单页 <3s 的碎片不上报(翻过≠读过);服务端再设 15s/日 阈值;
//   ③ 挂机排除:60s 无任何交互(滚动/触摸/按键)即停表;页面切后台立即停表。
//   只采原始秒数,「读过」的判定阈值在服务端聚合器(attention_profile.DWELL_MIN_S)——阈值可调可重放。
(() => {
  if (typeof FILE_REL === 'undefined' || !FILE_REL) return;
  if (FILE_REL.indexOf('/.sandbox/') >= 0) return;          // 沙盒测试不采
  const acc = {};                                            // page → secs
  let lastAct = Date.now();
  ['scroll', 'touchstart', 'pointerdown', 'keydown', 'wheel'].forEach((ev) =>
    window.addEventListener(ev, () => { lastAct = Date.now(); }, { passive: true, capture: true }));

  //   ④ 虚拟页码(用户设计):自建页记它的 **uid**(u_xxxx,插删页都不变)而不是页码 —— 永不漂移;
  //      真实页只能记页码(真插入 PDF 后物理页号必变),靠服务端 PAGE_ANCHOR_MIGRATIONS 迁移。
  function curLoadedKey() {                                  // 视口中线页,且内容真的渲染出来了
    let key = '', best = -1;
    const mid = window.innerHeight / 2;
    document.querySelectorAll('.page-wrap[data-page-num], .pdf-upage').forEach((pw) => {
      const r = pw.getBoundingClientRect();
      if (!r.height) return;
      let ov = Math.min(r.bottom, window.innerHeight) - Math.max(r.top, 0);
      if (ov <= 0) return;
      if (r.top <= mid && r.bottom >= mid) ov += 1e6;
      if (ov > best) {
        best = ov;
        const img = pw.querySelector('img');
        const ok = img ? (img.complete && img.naturalWidth > 0)
                       : !!pw.querySelector('canvas, .up2-blocks, .pdf-upage-overlay, .textLayer');
        if (!ok) { key = ''; return; }                        // 没渲染出来 = 看不见 = 这秒不计
        const uid = pw.dataset.uid || (pw.__upRec && pw.__upRec.id) || '';
        key = uid ? ('u:' + uid) : ('p:' + (parseInt(pw.dataset.pageNum, 10) || 0));
      }
    });
    return (key === 'p:0') ? '' : key;
  }

  setInterval(() => {
    if (document.visibilityState !== 'visible') return;
    if (Date.now() - lastAct > 60000) return;                // 挂机:停表
    const k = curLoadedKey();
    if (k) acc[k] = (acc[k] || 0) + 1;
  }, 1000);

  let lastFlush = Date.now();
  function flush(useBeacon) {
    const entries = Object.entries(acc).filter(([, s]) => s >= 3);   // <3s 碎片=翻过,不上报
    if (!entries.length) return;
    entries.forEach(([k]) => delete acc[k]);
    const body = JSON.stringify({ file: FILE_REL, dwell: entries.map(([k, s]) => (
      k.charAt(0) === 'u' ? { upage: k.slice(2), secs: s } : { page: +k.slice(2), secs: s })) });
    try {
      if (useBeacon && navigator.sendBeacon) {
        navigator.sendBeacon('/pdf/api/read-dwell', new Blob([body], { type: 'application/json' }));
      } else {
        fetch('/pdf/api/read-dwell', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                       body, keepalive: true }).catch(() => {});
      }
      lastFlush = Date.now();
    } catch (e) {}
  }
  setInterval(() => { if (Date.now() - lastFlush > 30000) flush(false); }, 5000);
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') flush(true); });
})();
// ── 31-localbook.js:整本下载到本机(客户端 Cache Storage 预灌)──
// 「📥 预热」是**服务器侧**缓存(Pi 先渲好,请求变快);本模块是**客户端**整本落盘:
// 逐页 fetch,由 SW 按服务器核验账户写入 pdf-private-v4-*;页面永远不知道/拼接 cache namespace。
// 读路径零改动——缓存键靠直接调用渲染路径的同一批模块级函数/常量(_bucketReqW/_ratchetReqW/
// FILE_REL/CHARS_VER/__imgMeta,拼装后同一 module 作用域)构造,逐字节一致,永不漂移。
// 另:navigator.storage.persist()(主屏 PWA 更易授予 → 豁免 LRU 逐出;见 references/ios-webext-capabilities.md)。
// ⚠ iOS 上 Safari 标签页与主屏 PWA 存储不互通:固定从主屏入口用,别两头下载。

let _lbAbort = false, _lbRunning = false;

// 开机即申请持久存储(幂等;拒绝也无害,只是可被逐出)
try { navigator.storage && navigator.storage.persist && navigator.storage.persist().catch(() => {}); } catch (_) {}

function _lbDoneKey() { return 'lb-done:' + FILE_REL; }
function _lbImgUrl(p, baseW) {
  const reqW = _ratchetReqW(p, baseW);
  const mt = (window.__imgMeta && window.__imgMeta.mtime) || 0;
  return '/pdf/api/page-image?file=' + encodeURIComponent(FILE_REL) + '&page=' + p + '&w=' + reqW + '&v=' + mt;
}
function _lbCharsUrl(p, cv) {
  return `/pdf/api/page-chars?file=${encodeURIComponent(FILE_REL)}&page=${p}&v=${CHARS_VER}&cv=${encodeURIComponent(cv)}`;
}

async function _lbFetchInto(url) {
  // 已缓存时 SW cache-first 会直接返回；未缓存时只有已由服务器核验的 client 才会落 v4。
  const r = await fetch(url);
  if (!r.ok) throw new Error('HTTP ' + r.status + ' ' + url.slice(0, 80));
  return r;
}

async function _lbDownload(btn) {
  const meta = window.__imgMeta;
  if (!meta || !meta.page_count) { window.RC && RC.toast('书还没加载好,稍候再试'); return; }
  if (!_imgMode) { window.RC && RC.toast('此书当前为矢量模式,本机化暂只支持页图模式'); return; }
  if (!('caches' in window)) { window.RC && RC.toast('此浏览器不支持本机存储'); return; }
  try { if (window.BWReaderPrivateCache) await BWReaderPrivateCache.rebind(); } catch (_) {}
  _lbRunning = true; _lbAbort = false;
  const total = meta.page_count;
  const baseW = _bucketReqW(Math.floor(meta.page_w * scale));   // 与 _prefetchAround 同公式
  let done = 0, errs = 0;
  const worker = async (pages) => {
    for (const p of pages) {
      if (_lbAbort) return;
      try {
        await _lbFetchInto(_lbImgUrl(p, baseW));
        // 字符层:先按本地猜测 cv 灌一份(读路径首拉这个键);再经 overlay 拿真 cv 灌正主 + 记 localStorage
        const cvKey = 'pdf-cv:' + FILE_REL + ':' + p;
        let cvGuess; try { cvGuess = localStorage.getItem(cvKey) || ('v' + CHARS_VER); } catch (_) { cvGuess = 'v' + CHARS_VER; }
        await _lbFetchInto(_lbCharsUrl(p, cvGuess));
        try {
          const ov = await (await fetch(`/pdf/api/page-overlay?file=${encodeURIComponent(FILE_REL)}&page=${p}`)).json();
          if (ov && ov.cv && ov.cv !== cvGuess) {
            await _lbFetchInto(_lbCharsUrl(p, ov.cv));
            try { localStorage.setItem(cvKey, ov.cv); } catch (_) {}
          }
        } catch (_) {}
      } catch (e) { errs++; }
      done++;
      if (btn && (done % 3 === 0 || done === total)) btn.textContent = '⏳ ' + Math.round(done * 100 / total) + '%';
    }
  };
  // 3 路并发,分片错开
  const lanes = [[], [], []];
  for (let p = 1; p <= total; p++) lanes[p % 3].push(p);
  await Promise.all(lanes.map(worker));
  if (!_lbAbort) await _lbPrimeShell();   // 只预热 /static/ 资产；含账户身份的 HTML 永不落 Cache Storage
  _lbRunning = false;
  if (_lbAbort) { _lbSyncBtn(btn); window.RC && RC.toast('已暂停(已存部分保留,重按继续)'); return; }
  if (errs === 0) {
    try { localStorage.setItem(_lbDoneKey(), JSON.stringify({ pages: total, w: baseW, mt: (meta.mtime || 0), ts: Date.now() })); } catch (_) {}
    let est = '';
    try { const e = await navigator.storage.estimate(); est = ',本机共占用 ' + Math.round((e.usage || 0) / 1048576) + 'MB'; } catch (_) {}
    window.RC && RC.toast('✓ 整本已存本机(' + total + ' 页' + est + '),弱网/离线可读');
  } else {
    window.RC && RC.toast('存完但有 ' + errs + ' 页失败,重按可补齐');
  }
  _lbSyncBtn(btn);
}

// 把「打开这本书所需的静态壳」存进本机：只预热本页已加载的 /static/ 资产。
// 开书 HTML 含 window.__USER__/provider ticket，禁止进入 Cache Storage；离线由当前已加载 PWA fallback 承接。
async function _lbPrimeShell() {
  try {
    const urls = new Set();
    try { performance.getEntriesByType('resource').forEach((en) => { try { const u = new URL(en.name); if (u.origin === location.origin && u.pathname.startsWith('/static/')) urls.add(u.pathname + u.search); } catch (_) {} }); } catch (_) {}
    document.querySelectorAll('script[src],link[href]').forEach((el) => {
      const u = el.getAttribute('src') || el.getAttribute('href') || '';
      if (u.startsWith('/static/')) urls.add(u);
    });
    for (const u of urls) { try { await fetch(u); } catch (_) {} }
  } catch (_) {}
}

async function _lbDelete(btn) {
  try {
    if (!window.BWReaderPrivateCache) throw new Error('私有缓存控制器未就绪');
    const ok = await BWReaderPrivateCache.deleteBook(FILE_REL);
    if (!ok) throw new Error('当前账户尚未核验');
    try { localStorage.removeItem(_lbDoneKey()); } catch (_) {}
    window.RC && RC.toast('已删除当前账户的本机副本');
  } catch (e) { window.RC && RC.toast('删除失败:' + e); }
  _lbSyncBtn(btn);
}

function _lbState() {
  try {
    const rec = JSON.parse(localStorage.getItem(_lbDoneKey()) || 'null');
    if (!rec) return 'none';
    return (rec.mt === ((window.__imgMeta && window.__imgMeta.mtime) || 0)) ? 'done' : 'stale';
  } catch (_) { return 'none'; }
}
function _lbSyncBtn(btn) {
  if (!btn) return;
  const st = _lbState();
  btn.textContent = st === 'done' ? '✓ 本机' : (st === 'stale' ? '⟳ 本机' : '⬇ 本机');
  btn.title = st === 'done' ? '整本已存本机(点击可删除本机副本)'
    : st === 'stale' ? '书已更新,点击重新下载到本机'
    : '整本下载到本机:页图+字符层落盘,弱网/离线也能读(再点=暂停)';
  btn.classList.toggle('active', st === 'done');
}
function _lbClick() {
  const btn = document.getElementById('lb-btn');
  if (_lbRunning) { _lbAbort = true; return; }
  const st = _lbState();
  if (st === 'done') { if (confirm('整本已在本机。删除本机副本?')) _lbDelete(btn); return; }
  _lbDownload(btn);
}
window._lbClick = _lbClick;

// 顶栏注入「⬇ 本机」按钮(放搜索🔍前;模板是静态 DOM,此时已存在)
(function () {
  try {
    const header = document.getElementById('header');
    if (!header || document.getElementById('lb-btn')) return;
    const b = document.createElement('button');
    b.id = 'lb-btn';
    b.addEventListener('click', _lbClick);
    const anchor = header.querySelector('#crop-toggle');
    header.insertBefore(b, anchor ? anchor.nextSibling : null);
    // meta 异步就绪后再刷状态(stale 判定要 mtime)
    setTimeout(() => _lbSyncBtn(b), 1500);
    _lbSyncBtn(b);
  } catch (_) {}
})();
// ═══════════ 32-extension-host.js — PDF 书籍宿主能力白名单 ═══════════
// 仍在 reader.js 模块作用域内，因此只做薄适配，复用现有 PDF 几何、sidecar、墨迹、
// 便签和导航函数。旧 PWA 网页壳明确不登记为书籍宿主；普通网页由扩展直接处理。
(() => {
  if (window.__PDF_CFG && window.__PDF_CFG.web_url) return;
  if (!window.BWReaderBookHost || window.__bwReaderLocalApi) return;

  function selection() {
    try {
      const adapter = window.PdfAdapter;
      const s = adapter && adapter.captureSelection ? adapter.captureSelection() : null;
      if (!s || !s.text) return null;
      const text = String(s.text || '').trim();
      const sentence = String(s.context || s.ctx || s.sentence || window.__lastSelSentence || '').trim();
      return {
        text,
        sentence,
        context: sentence || text,
        rect: s.rect && (s.rect.client || s.rect),
        anchor: s.anchor || null,
        file: typeof FILE_REL !== 'undefined' ? FILE_REL : '',
        book: document.title || '',
        langs: (typeof BOOK_LANGS !== 'undefined' && Array.isArray(BOOK_LANGS)) ? BOOK_LANGS.slice() : [],
        page: (s.anchor && Number(s.anchor.page))
          || (typeof _selPageNum === 'function' ? _selPageNum() : currentPage) || 0,
      };
    } catch (_) { return null; }
  }

  function context() {
    try {
      const adapter = window.PdfAdapter;
      const value = adapter && adapter.getContext ? adapter.getContext() : null;
      return value ? JSON.parse(JSON.stringify(value)) : null;
    } catch (_) { return null; }
  }

  function currentLocation() {
    let total = 0;
    try { total = Number(pdfDoc && pdfDoc.numPages) || 0; } catch (_) {}
    return { unit: 'page', index: Math.max(0, (Number(currentPage) || 1) - 1), total };
  }

  async function action(name, payload) {
    payload = payload || {};
    switch (name) {
      case 'ocr':
        if (window.onOcrSel) await window.onOcrSel();
        return { selection: selection() };
      case 'highlight': {
        if (!_charSel || !_charSel.pw) throw new Error('没有可标记的 PDF 选区');
        const colors = getHlColors();
        const color = String(payload.color || _activeHlColor || _lastHlColor || colors[0] || '#fff59d');
        const h = await saveHighlight({
          pw: _charSel.pw,
          sIdx: _charSel.startIdx,
          eIdx: _charSel.endIdx,
          color,
          kind: String(payload.kind || 'note'),
          sentence: String(payload.sentence || ''),
          body: String(payload.body || ''),
          note: String(payload.note || ''),
        });
        if (h) {
          _charSel.pw.querySelector('.sel-overlay')?.replaceChildren();
          _charSel = null;
          lastSelText = '';
          _updateSelPreview('');
          toolbar.classList.remove('open');
          _toast('已标记 🖌');
        }
        return {
          ok: !!h,
          highlight: h ? { id: h.id, page: h.page, color: h.color, text: h.text } : null,
        };
      }
      case 'open_search':
        if (window.openSearch) window.openSearch();
        return { ok: true };
      case 'toggle_ruby':
        if (window.toggleRuby) window.toggleRuby();
        return {
          ok: true,
          ruby: typeof _rubyEnabled === 'function' ? !!_rubyEnabled() : false,
          translate: typeof _pageTrOn !== 'undefined' ? !!_pageTrOn : false,
        };
      case 'toggle_page_translate':
        if (window.togglePageTranslate) window.togglePageTranslate();
        return {
          ok: true,
          ruby: typeof _rubyEnabled === 'function' ? !!_rubyEnabled() : false,
          translate: typeof _pageTrOn !== 'undefined' ? !!_pageTrOn : false,
        };
      case 'create_sticky':
        if (!window._noteCreateAtCenter) throw new Error('PDF 便签层尚未就绪');
        window._noteCreateAtCenter();
        return { ok: true };
      case 'toggle_ink':
        if (!window.inkToggle) throw new Error('PDF 绘图层尚未就绪');
        window.inkToggle();
        return { ok: true, active: document.body.classList.contains('ink-mode') };
      case 'anchor_fx':
        if (window.RC && RC.stickynote && RC.stickynote.anchorFx) {
          if (payload.show) RC.stickynote.anchorFx.show(Number(payload.x) || 0, Number(payload.y) || 0);
          else RC.stickynote.anchorFx.hide();
        }
        return { ok: true };
      case 'jump_page':
      case 'jump_location': {
        const locationPayload = payload.location || payload;
        const page = Math.max(1, Number(locationPayload.page)
          || (Number.isFinite(Number(locationPayload.index)) ? Number(locationPayload.index) + 1 : 1));
        if (window.jumpWithBack) window.jumpWithBack(page);
        else if (window.goToPage) window.goToPage(page);
        return { ok: true, page };
      }
      case 'change_page':
        if (window.changePage) window.changePage(Number(payload.delta) || 0);
        return { ok: true };
      case 'fit_width':
        if (window.fitWidth) window.fitWidth();
        return { ok: true };
      case 'zoom_by':
        if (window.zoomChange) window.zoomChange(Number(payload.delta) || 0);
        return { ok: true };
      case 'jump_context': {
        const target = payload.context || payload || {};
        const page = Math.max(1, Number(target.page || target.pdf_page || 1));
        const file = String(target.file || target.file_rel || '');
        if (file && typeof FILE_REL !== 'undefined' && file !== FILE_REL && window.openBookAt) {
          window.openBookAt(file, page);
        } else if (window.jumpWithBack) {
          window.jumpWithBack(page);
        }
        return { ok: true };
      }
      case 'flash_selection':
        if (typeof _flashSelOnPage === 'function') {
          _flashSelOnPage(Number(payload.page) || currentPage, String(payload.text || ''));
        }
        return { ok: true };
      case 'pin_card': {
        const cards = Array.isArray(payload.cards) ? payload.cards.slice(0, 50) : [];
        if (!cards.length) throw new Error('没有可钉住的卡片');
        if (!(window.RC && RC.stickynote && RC.stickynote.createCardAt)) {
          throw new Error('PDF 卡片便签尚未就绪');
        }
        const x = Number(payload.x) || (window.innerWidth || 1024) / 2;
        const y = Number(payload.y) || (window.innerHeight || 768) / 2;
        RC.stickynote.createCardAt(x, y, cards, String(payload.gid || ''));
        _toast('📌 已钉到书页');
        return { ok: true };
      }
      case 'pin_html': {
        const html = payload.html || {};
        const content = String(html.content || '');
        if (!content) throw new Error('没有可粘贴的工具卡内容');
        if (!(window.RC && RC.stickynote && RC.stickynote.createHtmlAt)) {
          throw new Error('PDF 工具卡便签尚未就绪');
        }
        const x = Number(payload.x) || (window.innerWidth || 1024) / 2;
        const y = Number(payload.y) || (window.innerHeight || 768) / 2;
        const ok = RC.stickynote.createHtmlAt(x, y, {
          content,
          isHtml: !!html.isHtml,
          label: String(html.label || '卡片'),
          type: String(html.type || ''),
          icon: String(html.icon || ''),
          form: String(html.form || 'full'),
          cid: String(html.cid || payload.cid || ''),
        });
        if (!ok) throw new Error('请把工具卡放到 PDF 正文上再松手');
        return { ok: true };
      }
      case 'toggle_fullscreen':
        if (window.toggleFullscreen) window.toggleFullscreen();
        return { ok: true };
      case 'open_settings':
        if (window.openSettings) window.openSettings();
        return { ok: true };
      case 'open_favorite':
        if (!window._favOpenPicker) throw new Error('收藏组件尚未就绪');
        window._favOpenPicker();
        return { ok: true };
      case 'create_user_page':
        if (!window._upCreate) throw new Error('插入页组件尚未就绪');
        window._upCreate();
        return { ok: true };
      case 'toggle_layout':
        if (window.toggleSpread) window.toggleSpread();
        return { ok: true };
      case 'toggle_crop':
        if (window.toggleCrop) window.toggleCrop();
        return { ok: true };
      case 'clear_selection':
        if (window.PdfAdapter && PdfAdapter.clearSelection) PdfAdapter.clearSelection();
        return { ok: true };
      default:
        throw new Error('不允许的 PDF 本地命令：' + name);
    }
  }

  const names = [
    'ocr', 'highlight', 'open_search', 'toggle_ruby', 'toggle_page_translate',
    'create_sticky', 'toggle_ink', 'anchor_fx', 'jump_page', 'jump_location',
    'change_page', 'fit_width', 'zoom_by', 'jump_context', 'flash_selection',
    'pin_card', 'pin_html', 'toggle_fullscreen', 'open_settings', 'open_favorite',
    'create_user_page', 'toggle_layout', 'toggle_crop', 'clear_selection',
  ];
  const actions = {};
  names.forEach((name) => { actions[name] = (payload) => action(name, payload); });

  const localApi = BWReaderBookHost.register({
    mode: 'pdf',
    file: typeof FILE_REL !== 'undefined' ? FILE_REL : '',
    title: document.title || '',
    langs: (typeof BOOK_LANGS !== 'undefined' && Array.isArray(BOOK_LANGS)) ? BOOK_LANGS.slice() : [],
    selection,
    context,
    currentLocation,
    actions,
    capabilities: {
      selection: true,
      context: true,
      highlight: true,
      pdfHighlight: true,
      selectionOcr: true,
      bookSearch: true,
      ruby: true,
      pageTranslate: true,
      stickyNote: true,
      ink: true,
      anchorFx: true,
      pinCard: true,
      pinHtmlCard: true,
      jumpPage: true,
      navigation: true,
      zoom: true,
      layout: true,
      crop: true,
      fullscreen: true,
      bookSettings: true,
      favorite: true,
      userPage: true,
    },
  });

  // 无扩展时 PWA 也经同一动作名调用真实宿主；扩展接管后同名动作由桥转发回来。
  try {
    if (window.RC && RC.actions && window.PdfAdapter && RC.adapter && RC.adapter() === PdfAdapter) {
      const meta = (storage) => ({ owner: 'pwa', runtime: 'native', storage });
      RC.actions.bind('highlight.save', (payload) => localApi.localAction('highlight', payload), meta('book-sidecar'));
      RC.actions.bind('ink.toggle', () => localApi.localAction('toggle_ink', {}), meta('book-sidecar'));
      RC.actions.bind('note.create', () => localApi.localAction('create_sticky', {}), meta('book-sidecar'));
      RC.actions.bind('reading.ruby.toggle', () => localApi.localAction('toggle_ruby', {}), meta('device-local'));
      RC.actions.bind('translation.page.toggle', () => localApi.localAction('toggle_page_translate', {}), meta('device-local'));
      RC.actions.bind('pin.card', (payload) => localApi.localAction('pin_card', payload), meta('book-sidecar'));
      RC.actions.bind('pin.html', (payload) => localApi.localAction('pin_html', payload), meta('book-sidecar'));
      RC.actions.bind('pin.anchorFx', (payload) => localApi.localAction('anchor_fx', payload), meta('none'));
    }
  } catch (_) {}
})();
// ═══════════ 33-selection-controller.js — 统一当前选区 + 外部内容宿主入口 ═══════════
//
// reader.js 是一个 ES module，lastSelText / _charSel / toolbar 都是模块词法状态。iframe 或
// 扩展脚本不能靠写 window 上的同名属性假装改变它们；所有外部选区必须进入这里，再由这里同步
// 词法状态、上下文、预览和工具条。PDF 原生选区仍由 13/14/16 的既有路径写入，本桥只提供统一
// 读取和外部宿主写入口，不改变 PDF 字符层几何。
(() => {
  let externalSelection = null;

  function _normalized(input) {
    if (!input) return null;
    const raw = {
      text: String(input.text == null ? '' : input.text).slice(0, 20000),
      context: String(input.context != null ? input.context
        : (input.ctx != null ? input.ctx : (input.sentence || ''))).slice(0, 50000),
      rect: input.rect || null,
      anchor: input.anchor || null,
      data: Object.assign({}, input.data || {}, {
        source: String(input.source || (input.data && input.data.source) || 'external')
      })
    };
    if (window.RC && RC.contract && RC.contract.selection) return RC.contract.selection(raw);
    if (!raw.text.trim()) return null;
    raw.ctx = raw.context;
    raw.sentence = raw.context;
    return raw;
  }

  function _adapter() {
    try { return window.RC && RC.adapter ? RC.adapter() : null; }
    catch (_) { return null; }
  }

  function current() {
    const adapter = _adapter();
    if (adapter && typeof adapter.captureSelection === 'function') {
      try {
        const captured = _normalized(adapter.captureSelection());
        if (captured) return captured;
      } catch (_) {}
    }
    // legacy / 非 shared PDF 仍可从真实模块词法值读取；不为它杜撰 anchor。
    return _normalized({
      text: lastSelText || '',
      context: (typeof window.__lastSelSentence === 'string' ? window.__lastSelSentence : ''),
      rect: externalSelection && externalSelection.rect,
      data: externalSelection && externalSelection.data
    });
  }

  function _positionToolbar(rect) {
    if (!toolbar || !rect) return;
    const left = Number(rect.left);
    const bottom = Number(rect.bottom);
    if (!Number.isFinite(left) || !Number.isFinite(bottom)) return;
    const width = toolbar.offsetWidth || 320;
    const height = toolbar.offsetHeight || 52;
    toolbar.style.position = 'fixed';
    toolbar.style.left = Math.max(
      8,
      Math.min((window.innerWidth || 1024) - width - 8, left)
    ) + 'px';
    toolbar.style.top = Math.max(
      8,
      Math.min((window.innerHeight || 768) - height - 8, bottom + 8)
    ) + 'px';
    toolbar.style.zIndex = '900';
  }

  function acceptExternal(input) {
    const selection = _normalized(input);
    const source = String(input && input.source || 'external');
    const adapter = _adapter();
    // 外部 web frame 不能在当前 PDF/EPUB adapter 上写入词法选区。
    if (adapter && adapter.kind && source !== 'external' && adapter.kind !== source) return null;

    if (!selection) {
      clearExternal(source);
      return null;
    }
    externalSelection = selection;
    _charSel = null;                         // 明确不伪造 PDF char geometry
    lastSelText = selection.text;            // 真正更新 reader.js 模块词法状态
    _updateSelPreview(lastSelText);
    try {
      window.__lastSelSentence = selection.context || '';
      window.__lastSelMeta = {
        kind: source,
        url: selection.data && selection.data.url || '',
        t: Date.now()
      };
    } catch (_) {}
    try { if (typeof _updateGrammarBtnVisibility === 'function') _updateGrammarBtnVisibility(); } catch (_) {}
    _positionToolbar(selection.rect);
    if (toolbar) toolbar.classList.add('open');
    try {
      document.dispatchEvent(new CustomEvent('bw:selection-changed', {
        detail: { source, text: selection.text, context: selection.context }
      }));
    } catch (_) {}
    return selection;
  }

  function clearExternal(source) {
    source = String(source || 'external');
    const activeSource = externalSelection && externalSelection.data
      ? String(externalSelection.data.source || 'external') : '';
    if (activeSource && source !== 'external' && source !== activeSource) return false;
    externalSelection = null;
    _charSel = null;
    lastSelText = '';
    _updateSelPreview('');
    try { window.__lastSelSentence = ''; window.__lastSelMeta = null; } catch (_) {}
    if (toolbar) toolbar.classList.remove('open');
    return true;
  }

  const controller = Object.freeze({
    contract: 'selection-controller/1',
    current,
    acceptExternal,
    clearExternal,
    adapter: _adapter
  });
  window.__bwSelectionController = controller;
  // 稳定的外部入口名：WebAdapter / 后续 EPUB iframe adapter 只依赖它，不接触模块词法变量。
  window.__setExternalSelection = (selection) => controller.acceptExternal(selection);
})();
