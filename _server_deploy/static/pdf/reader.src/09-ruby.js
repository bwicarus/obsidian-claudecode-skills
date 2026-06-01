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
    const rt = it.rt || ''; if (!rt) continue;
    const x0 = it.x0 * sx, y0 = it.y0 * sy, x1 = it.x1 * sx, y1 = it.y1 * sy;
    const w = Math.max(6, x1 - x0), h = Math.max(6, y1 - y0);
    // 字号：约词高 36%；并受「词宽/读音字数」约束使读音横向不超过词宽（系数 1.0，
    // 之前 1.4 → 读音比词宽 40% + overflow:hidden 把末尾假名切掉，故收到 1.0 + 配 overflow:visible）
    const fs = Math.max(7, Math.min(h * 0.36, w / Math.max(1, rt.length) * 1.0));
    const sp = document.createElement('span');
    sp.className = 'rt';
    sp.textContent = rt;
    sp.style.left = x0 + 'px';
    sp.style.width = w + 'px';
    sp.style.fontSize = fs.toFixed(1) + 'px';
    // ruby 略偏下贴近本行汉字：只 1/3 露在框顶之上，2/3 落进本行字框顶部 padding(=视觉空隙)。
    // （OCR 字框顶有 padding，框比实际字高；偏下放更贴自己这行、更不碰上一行。用户要求再向下些。）
    sp.style.top = Math.max(0, y0 - fs * 0.34) + 'px';
    layer.appendChild(sp);
  }
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

