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
