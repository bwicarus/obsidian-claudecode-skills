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
      return '<div class="bs-row">' +
        '<a class="bs-item' + (here ? ' cur' : '') + '" href="' + url + '" onclick="return _bsOpen(this)">' +
        '<span class="bs-name">' + _esc(p.name) + (here ? '　←当前' : '') + '</span>' +
        '<span class="bs-meta">' + _esc(p.dir && p.dir !== '.' ? p.dir : '') + (p.dir && p.dir !== '.' ? ' · ' : '') + mb + ' MB' +
        (p.comp_exists ? ' · 🗜有压缩版' : '') + '</span></a>' +
        '<button class="bs-ocr" title="公式识别(Claude 视觉,后台跑,无需 PC)" data-rel="' + encodeURIComponent(p.rel) + '" onclick="_bsFormulaOCR(this)">🧮</button>' +
        '</div>';
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

// 书架每本书的「🧮 公式识别」:后台跑 Claude 视觉给缺 latex 的公式框补 LaTeX(无需 PC,中文混排也准)。
if (!document.getElementById('bs-ocr-css')) {
  const _st = document.createElement('style'); _st.id = 'bs-ocr-css';
  _st.textContent = '.bs-row{display:flex;align-items:stretch;border-bottom:1px solid #151d27}' +
    '.bs-row .bs-item{flex:1 1 auto;min-width:0;border-bottom:none;border-radius:8px 0 0 8px}' +
    '.bs-ocr{flex:none;width:46px;background:transparent;border:none;color:#7d8590;font-size:17px;cursor:pointer;border-radius:0 8px 8px 0;-webkit-tap-highlight-color:transparent}' +
    '.bs-ocr:hover,.bs-ocr:active{background:#16202c;color:#cfe0ff}.bs-ocr:disabled{opacity:.6}';
  document.head.appendChild(_st);
}
window._bsFormulaOCR = async function (btn) {
  const rel = decodeURIComponent(btn.dataset.rel || '');
  if (!rel || btn._busy) return;
  const tip = (m) => { if (window._toast) window._toast(m); };
  btn._busy = true; const old = btn.textContent; btn.disabled = true; btn.textContent = '⏳';
  const finish = (txt) => { if (btn._t) { clearInterval(btn._t); btn._t = null; } btn.textContent = txt; setTimeout(() => { btn.textContent = old; btn.disabled = false; btn._busy = false; }, 4000); };
  try {
    const d = await (await fetch('/pdf/api/formula-ocr', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ file: rel }) })).json();
    if (!d.ok) { finish('⚠'); tip(d.msg || '启动失败'); return; }
    if (d.done) { finish('✅'); tip('公式都已识别(' + d.have + '/' + d.total + ')'); return; }
    tip('公式识别已在后台开始(' + d.have + '/' + d.total + ' 已好),完成后即可点选公式');
    btn._t = setInterval(async () => {   // 轮询直到后台进程结束(几分钟~半小时,取决于公式数)
      try {
        const s = await (await fetch('/pdf/api/formula-ocr-status?file=' + encodeURIComponent(rel))).json();
        if (s && s.ok) { btn.textContent = '🧮' + Math.round(100 * s.have / Math.max(1, s.total)) + '%';
          if (!s.running) { finish('✅'); tip('公式识别完成 ' + s.have + '/' + s.total); } }
      } catch (_) {}
    }, 8000);
  } catch (e) { finish('⚠'); tip('请求失败:' + (e && e.message || e)); }
};

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
