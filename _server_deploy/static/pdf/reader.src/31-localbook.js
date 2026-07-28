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
