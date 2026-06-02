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
    (typeof _toast === 'function') && _toast('已保存本书语言：' + (BOOK_LANGS.join(' / ') || '无'));
  } catch (e) { (typeof _toast === 'function') && _toast('保存失败：' + e.message); }
};
window.dlog('PDF_URL = ' + PDF_URL);
let pdfDoc = null;
let currentPage = window.__PDF_CFG.page;
let scale = 1.4;
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

