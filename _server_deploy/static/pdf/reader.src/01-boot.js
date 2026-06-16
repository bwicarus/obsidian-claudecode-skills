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
let _pendingScrollY = 0;   // 上次位置恢复用
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
