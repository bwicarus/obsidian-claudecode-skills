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
let _refPageW = 0;     // 参考页(第1页)在 scale=1 时的宽。统一页宽：每页按自身原生宽缩放到 _refPageW × scale
let readMode = (() => {
  const m = new URLSearchParams(location.search).get('mode');   // 技能树书本图标可带 ?mode=continuous
  return (m === 'continuous' || m === 'single') ? m : (localStorage.getItem('pdf-read-mode') || 'single');
})();   // 'single' | 'continuous'
let _contIO = null;   // IntersectionObserver for 连续模式
let _pendingScrollY = 0;   // 上次位置恢复用
let _scrollSaveTimer = null;

