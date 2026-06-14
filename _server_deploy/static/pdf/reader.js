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
    (typeof _toast === 'function') && _toast('已保存本书语言：' + (BOOK_LANGS.join(' / ') || '无'));
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
  scale = Math.max(_ZOOM_MIN, Math.min(_scaleMax, scale + delta));
  // +/- 缩放跟双指缩放(_applyZoom)一致:三模式都原地重排(单页=唯一 wrap),原地失败才按模式重建
  if (!(await _rescaleContinuousInPlace())) { if (readMode === 'single') await renderPage(currentPage); else await setupContinuousMode(); }
};
// 宽适应：按 #main 可用宽度重算 scale（取消 ＋/－ 或双指缩放，回到一页刚好铺满宽度）
window.fitWidth = async () => { await _refitToWidth(true); window._rememberOrientLayout?.(); };   // 适应也记进当前方向
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
    if (_applyOrientLayoutVars(lay)) {
      window.dlog && window.dlog('orient: 套用 ' + now + ' = ' + JSON.stringify(lay));
      _updateModeButtons(); _updateCropBtn();
      await _applyModeChange(currentPage);
      await _applyPendingOrientScale();  // 还原该方向上次的缩放(宽度适应/手动放大)
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
    // 统一:三模式都走 wrap 原地重标尺(单页=唯一 wrap,跟连续同一套 CSS-zoom 瞬时缩放 + 后台重栅格化);失败按模式重建
    if (!(await _rescaleContinuousInPlace())) { if (readMode === 'single') await renderPage(currentPage); else await setupContinuousMode(); }
    requestAnimationFrame(() => {
      if (focal && focal.s0 > 0) {
        const mr = main.getBoundingClientRect();
        const k = newScale / focal.s0;   // 内容坐标随 scale 等比放大
        main.scrollLeft = Math.max(0, focal.fx * k - (focal.cx - mr.left));
        main.scrollTop  = Math.max(0, focal.fy * k - (focal.cy - mr.top));
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
      _applyZoom(p.target, { fx: p.fx, fy: p.fy, cx: p.cx, cy: p.cy, s0: p.s0 });
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
    _applyZoom(target, { fx: main.scrollLeft + (e.clientX - mr.left), fy: main.scrollTop + (e.clientY - mr.top), cx: e.clientX, cy: e.clientY, s0: scale });
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
    ph.textContent = '… 第 ' + num + ' 页';
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
const READER_BUILD = 'reader-fix-29';
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
  w.textContent = '… 第 ' + num + ' 页';
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
    if (target) {
      const num = parseInt(target.dataset.pageNum);
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
    }
    _unloadFarPages(wraps);   // 顺带卸载远处页,封顶内存(复用已取好的 wraps)
  }, 200);
}

// 选中浮出工具栏 —— 跨桌面（mouseup）+ 触屏（touchend）+ 通用（selectionchange）
const toolbar = document.getElementById('sel-toolbar');
let lastSelText = '';
     _updateSelPreview('');
let _selTimer = null;
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
            wrap.__pageWPt = d2.page_w; wrap.__pageHPt = d2.page_h;
            wrap.__furigana = d2.furigana || [];
            try { renderRubyLayer(wrap); } catch (_) {}
          }
        } catch (_) {}
      }
    }
    wrap.__vocabMarks = (ov && ov.vocab_marks) || [];
    wrap.__vocabSentences = (ov && ov.vocab_sentences) || [];
    try { renderVocabUnderlines(wrap, wrap.__vocabMarks); } catch(e) { window.dlog?.('vocab underline fail: '+e.message,'#ff6b6b'); }
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

function renderVocabUnderlines(pw, marks) {
  if (!_vocabUnderlineEnabled()) return;
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
  for (const it of items) {
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
            renderVocabUnderlines(pw, pw.__vocabMarks);
            renderVocabSentences(pw, pw.__vocabSentences);
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
// 拖选期间要临时禁点的呼吸高亮 .hl（查词/词组/解释）——防拖选经过它们被截获(丢 move/up + 误弹)
const _OVL_HL_SEL = '.word-hl-layer .hl, .phrase-hl-layer .hl, .explain-hl-layer .hl';

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

let _dragStartCharIdx = null, _dragMoved = false, _dragStartXY = null, _fromLBtn = false;
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
  const onStart = (x, y) => {
    _syncCharBoxScale(pw);   // 命中前先把 charBoxes 对齐到当前显示尺寸(烘焙 scale 可能已过期)
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
            // 同步关掉刚被 _selByCharRange 打开的工具栏:同一事件 tick 内移除 → 浏览器根本不画它。
            // 此前靠 30ms 后的 showWordPopover 去关 → 工具栏闪一帧再消失(慢词时=「弹框闪烁后消失」)。
            toolbar.classList.remove('open');
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
  // document 级 mousemove/mouseup 移到模块顶层单 dispatcher(经 pw.__charDrag 分发),不再每次绑定泄漏
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
let _phraseMarkSet = new Set();   // 已掌握词组(归一化键):标掌握后不再画生词下划线
const _phraseNorm = s => String(s || '').trim().replace(/\s+/g, ' ').toLowerCase();
async function _loadPhraseFavs() {
  try { const d = await (await fetch('/pdf/api/phrases')).json(); if (d.ok) _phraseFavSet = new Set(d.phrases || []); } catch (_) {}
  try { const d = await (await fetch('/pdf/api/phrase-mark')).json(); if (d.ok) _phraseMarkSet = new Set(d.mastered || []); } catch (_) {}
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
  if (a.html) {
    // 已就绪 → 直接显缓存
    openResult(a.title || '💡 AI 解释', a.src || a.text, a.html);
    try { if (a.resultContext) _resultContext = a.resultContext; } catch (_) {}
    try { addResultPickers(); } catch (_) {}
  } else {
    // 还在后台跑 → 开加载面板,登记 reqId,完成时由 _runExplainBg 填充(并补 pickers)
    openResult(a.title || '💡 AI 解释', a.src || a.text, '<div class="loading">⏳ AI 处理中…</div>');
    a.panelReqId = _resultReqId;   // openResult 已自增 _resultReqId,这里取新值
  }
  // 点高亮=打开页面 → 一次点击即移除高亮(后台 job 仍持自身引用,未 canceled 时继续填面板)
  document.querySelectorAll('.explain-hl-layer').forEach(l => l.remove());
  _activeExplainHl = null;
}
window.showPhrasePopover = async (text, opts) => {
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
  if (_wordPopState.phrase && !(opts && opts.noHighlight)) _showPhraseHighlight(_charSel && _charSel.pw);
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
    '<button id="wp-master-btn" class="' + (_wordPopState.mastered ? 'wp-anki' : '') + '" onclick="_wordPopMaster(this)" title="' + (_wordPopState.mastered ? '点击取消掌握（恢复生词下划线）' : '标记掌握 100（该词组不再标生词下划线）') + '">' +
    (_wordPopState.mastered ? '✓ 已掌握 100' : '☆ 标记掌握') + '</button>' +
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
      if (nowFav) _removePhraseHighlight();   // 收藏后该词组变成划线(分词单元),呼吸查询高亮自动消除
      refreshCharsWForAllPages();   // 让分词合并立即生效
    } else if (btn) { btn.disabled = false; }
  }).catch(() => { if (btn) btn.disabled = false; });
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
  for (const cb of pw.__charBoxes) { if (cb._oi != null && cb._oi < newW.length) cb.w = newW[cb._oi]; }
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
    '&file=' + encodeURIComponent(FILE_REL || '') + '&page=' + (currentPage || 0) +
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
// 乐观更新:标掌握瞬间先把该词下划线从所有已加载页移掉(不等服务器写库+重算),服务器响应后 refresh 再校正。
// 大厂标配(optimistic UI):本地实例下尤其明显——画面立刻响应,不用等任何往返。
function _dropVocabUnderlineOptimistic(s) {
  const keys = new Set();
  for (const k of [s && s.lemma, s && s.word]) if (k) keys.add(String(k).toLowerCase());
  if (!keys.size) return;
  document.querySelectorAll('[data-loaded="1"][data-page-num]').forEach(pw => {
    if (!pw.__vocabMarks || !pw.__vocabMarks.length) return;
    const before = pw.__vocabMarks.length;
    pw.__vocabMarks = pw.__vocabMarks.filter(m =>
      !(keys.has(String(m.lemma || '').toLowerCase()) || keys.has(String(m.word || '').toLowerCase())));
    if (pw.__vocabMarks.length !== before) { try { renderVocabUnderlines(pw, pw.__vocabMarks); } catch (_) {} }
  });
}

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
    // 双页模式下侧栏挤窄 → 两页并排太小 → **临时**切单列(不写 localStorage/orient),关栏自动还原双页。
    // 但悬浮显示模式抽屉盖在正文上、不挤压 → 页面不变窄 → 保持双页不切。
    if (readMode === 'spread' && !document.body.classList.contains('grammar-floating')) {
      _spreadBeforePanel = _spreadOffset;
      readMode = 'continuous';
      _updateModeButtons();
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
  // 还原侧栏打开时临时切走的双页
  if (_spreadBeforePanel != null) {
    readMode = 'spread';
    _spreadOffset = _spreadBeforePanel;
    _spreadBeforePanel = null;
    _updateModeButtons();
  }
  if (!document.body.classList.contains('grammar-floating')) _scheduleRefit(true);   // 悬浮模式宽度不变→不重排(免闪);挤压才重排
};

// ── 右栏外观设置：悬浮显示 + 背景模糊度（localStorage 持久化，仿仪表盘抽屉设置）──
function _gpApplyAppearance() {
  document.body.classList.toggle('grammar-floating', localStorage.getItem('pdf-gp-floating') === '1');
  const blur = parseInt(localStorage.getItem('pdf-gp-blur') || '20', 10);
  document.documentElement.style.setProperty('--gp-blur', blur + 'px');
}
window._gpSetFloating = (on) => {
  localStorage.setItem('pdf-gp-floating', on ? '1' : '0');
  document.body.classList.toggle('grammar-floating', !!on);
  if (typeof _scheduleRefit === 'function') _scheduleRefit(true);   // 悬浮↔挤压 → #main 宽度变 → 重排
};
window._gpSetBlur = (v) => {
  localStorage.setItem('pdf-gp-blur', String(v));
  document.documentElement.style.setProperty('--gp-blur', v + 'px');
  const el = document.getElementById('gp-blur-val'); if (el) el.textContent = v;
};
window.toggleSideSettings = (ev) => {
  if (ev) ev.stopPropagation();
  const m = document.getElementById('side-settings'); if (!m) return;
  if (m.style.display === 'block') { m.style.display = 'none'; return; }
  const f = document.getElementById('gp-floating');
  if (f) f.checked = localStorage.getItem('pdf-gp-floating') === '1';
  const b = parseInt(localStorage.getItem('pdf-gp-blur') || '20', 10);
  const bi = document.getElementById('gp-blur'), bv = document.getElementById('gp-blur-val');
  if (bi) bi.value = b; if (bv) bv.textContent = b;
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

// 设置面板页内 tab 切换(AI·翻译 / 阅读 / 语法 / 高亮),记住上次所在 tab
window._setSettingsTab = (name) => {
  document.querySelectorAll('#settings-mask .set-tab').forEach(t => t.classList.toggle('active', t.dataset.pane === name));
  document.querySelectorAll('#settings-mask .set-pane').forEach(p => { p.style.display = (p.dataset.pane === name) ? '' : 'none'; });
  try { localStorage.setItem('pdf-set-tab', name); } catch (_) {}
};
window.openSettings = () => {
  try { _setSettingsTab(localStorage.getItem('pdf-set-tab') || 'ai'); } catch (_) {}
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
  { const e = document.getElementById('set-auto-orient'); if (e) e.checked = (localStorage.getItem('pdf-auto-orient') === '1'); }
  // 去边百分比(本书,从已加载的 _crop 回填)
  { const g = (id, v) => { const e = document.getElementById(id); if (e) e.value = v || 0; };
    g('set-crop-l', _crop.l); g('set-crop-r', _crop.r); g('set-crop-t', _crop.t); g('set-crop-b', _crop.b); }
  // 本书文本语言勾选(每本书独立,从 BOOK_LANGS 回填)
  document.querySelectorAll('#lang-checks input').forEach(c => { c.checked = (BOOK_LANGS || []).includes(c.value); });
  renderHlColorSetting();
  if (window._initCharOfsPanel) window._initCharOfsPanel();   // 文字层校准块状态
  document.getElementById('settings-mask').style.display = 'flex';
};
window._applyCropSettings = () => {
  const num = (id) => Math.max(0, Math.min(45, parseFloat(document.getElementById(id)?.value) || 0));
  const crop = {l: num('set-crop-l'), r: num('set-crop-r'), t: num('set-crop-t'), b: num('set-crop-b')};
  saveCropSettings(crop, true);   // 存后端 + 自动开启去边 + 重渲染
  closeSettings();
  _toast?.('去边已应用');
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
  // **所译严格 = 预览(所选)文本**：sent.rects 只用作译文覆盖位置(geometry),要翻译的文字一律
  // 用工具栏预览那串 lastSelText，杜绝「翻译内容跟预览不一致」(_buildSentenceFromSel 重拼可能分歧)。
  { const _pv = (lastSelText || '').replace(/\s+/g, ' ').trim(); if (_pv) sent.text = _pv; }
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
  // 解释**不开面板**:选区建一个一直闪烁的琥珀高亮,AI 后台跑;点高亮才开解释页 + 移除高亮(一次点击)。
  const _ehl = (typeof _showExplainHighlight === 'function') ? _showExplainHighlight(_charSel && _charSel.pw, lastSelText) : null;
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
  const _fillPanel = (innerHtml, pickers) => {
    if (hl.panelReqId == null || hl.panelReqId !== _resultReqId) return;   // 用户没点开等 / 已开别的结果 → 不填
    const el = document.getElementById('result-content'); if (!el) return;
    el.innerHTML = innerHtml;
    if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([el]).catch(() => {});
    if (pickers) { try { _resultContext = hl.resultContext; } catch (_) {} try { addResultPickers(); } catch (_) {} }
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
  if (st) st.textContent = '⏳ 重扫第 ' + currentPage + ' 页…(Google Vision,几秒)';
  try {
    const r = await fetch('/pdf/api/reocr-page', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file: FILE_REL, page: currentPage }),
    });
    const d = await r.json();
    if (d.cv) { try { localStorage.setItem('pdf-cv:' + FILE_REL + ':' + currentPage, d.cv); } catch (_) {} }  // 重扫后 cv 更新→重渲直接取新覆盖
    if (d.ok && d.chars > 0) {
      if (st) st.textContent = '✓ 第 ' + currentPage + ' 页重扫完成(' + d.chars + ' 字)';
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
    if (st) st.textContent = d.cleared ? ('✓ 已撤销第 ' + currentPage + ' 页重扫') : '该页无重扫记录';
    _rerenderLoadedPages();
  } catch (e) { if (st) st.textContent = '✗ 网络失败'; }
};

// 键盘快捷键
document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'INPUT') return;
  if (e.key === 'ArrowRight' || e.key === 'PageDown' || e.key === ' ') { e.preventDefault(); changePage(1); }
  else if (e.key === 'ArrowLeft' || e.key === 'PageUp') { e.preventDefault(); changePage(-1); }
  else if (e.key === 'Escape') { closeResult(); toolbar.classList.remove('open'); }
});

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
// ── 书架浮层:点「返回书架」**零跳转秒开**(SPA 思路,2026-06-10)──
// 旧行为=整页跳 /pdf/:重阅读器卸载(几千 DOM)+ 服务端渲染,慢到要靠"返回中…"过场动画硬撑。
// 现改:阅读器内弹浮层——书单 localStorage 缓存**即时渲染** + 后台 fetch 刷新;点书才真正导航
// (那是"打开新书",本来就要整页加载);「完整书架」链接保留去 /pdf/(压缩/预处理/预热等重操作)。
// 压缩版决策与选书页同款:per-book localStorage 'pdf-use-compressed:<rel>'==='1' 才带 &compressed=1。
function _openBookshelf() {
  let ov = document.getElementById('bookshelf-ov');
  if (!ov) {
    ov = document.createElement('div');
    ov.id = 'bookshelf-ov';
    ov.innerHTML =
      '<div class="bs-head"><b>📚 书架</b>' +
      '<a href="/pdf/" class="bs-full">完整书架(压缩 / 预处理 / 预热) ›</a>' +
      '<button class="bs-close" onclick="document.getElementById(\'bookshelf-ov\').style.display=\'none\'">✕ 继续阅读</button></div>' +
      '<div class="bs-list" id="bs-list">加载中…</div>';
    // 点浮层空白处(列表外)不关——明确动作:✕ 继续阅读 / 点书 / 完整书架
    document.body.appendChild(ov);
  }
  ov.style.display = 'flex';
  const render = (pdfs) => {
    const cur = (window.__PDF_CFG && window.__PDF_CFG.file_rel) || '';
    const el = document.getElementById('bs-list');
    if (!pdfs || !pdfs.length) { el.textContent = '(没有书)'; return; }
    el.innerHTML = pdfs.map(p => {
      const mb = (p.size_kb / 1024).toFixed(1);
      const here = p.rel === cur;
      let url = '/pdf/view?file=' + encodeURIComponent(p.rel);
      try {
        if (p.comp_exists && localStorage.getItem('pdf-use-compressed:' + p.rel) === '1') url += '&compressed=1';
      } catch (_) {}
      return '<a class="bs-item' + (here ? ' cur' : '') + '" href="' + url + '" onclick="return _bsOpen(this)">' +
        '<span class="bs-name">' + _esc(p.name) + (here ? '　←当前' : '') + '</span>' +
        '<span class="bs-meta">' + _esc(p.dir && p.dir !== '.' ? p.dir : '') + (p.dir && p.dir !== '.' ? ' · ' : '') + mb + ' MB' +
        (p.comp_exists ? ' · 🗜有压缩版' : '') + '</span></a>';
    }).join('');
  };
  try { const c = JSON.parse(localStorage.getItem('pdf-bookshelf-cache') || 'null'); if (c) render(c); } catch (_) {}
  fetch('/pdf/api/list-pdfs').then(r => r.json()).then(d => {
    if (d && d.ok) {
      render(d.pdfs);
      try { localStorage.setItem('pdf-bookshelf-cache', JSON.stringify(d.pdfs)); } catch (_) {}
    }
  }).catch(() => {});
}
window._openBookshelf = _openBookshelf;

// 点书:**立即给行内反馈**(⏳打开中,iPad 上整页导航首绘前老页面冻住,没反馈像死机),
// 画出反馈后再 ① 主动清空 page-container(几千 DOM 的卸载成本提前、可控,Safari 导航提交更快)
// ② 真正导航。href 保留 → 长按/新标签打开不受影响。
window._bsOpen = function (a) {
  try {
    if (a.classList.contains('opening')) return false;   // 防双击
    a.classList.add('opening');
    const n = a.querySelector('.bs-name');
    if (n) n.textContent = '⏳ 打开中…　' + n.textContent;
    requestAnimationFrame(() => requestAnimationFrame(() => {   // 两帧:确保反馈已 paint 再开拆
      try {
        if (typeof _contIO !== 'undefined' && _contIO) _contIO.disconnect();
        const pc = document.getElementById('page-container');
        if (pc) pc.replaceChildren();
      } catch (_) {}
      location.href = a.href;
    }));
    return false;
  } catch (_) {
    return true;   // 异常 → 走 <a> 默认导航
  }
};
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
