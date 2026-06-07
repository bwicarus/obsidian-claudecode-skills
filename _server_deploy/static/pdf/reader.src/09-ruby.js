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

