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
    const ver = (typeof READER_BUILD !== 'undefined' ? READER_BUILD : '?') + (window.__READER_GIT ? ' · ' + window.__READER_GIT : '');
    _toast?.((_connMs < 0 ? '🔴 服务器不可达' :
      `${_connClass(_connMs) === 'g' ? '🟢' : _connClass(_connMs) === 'y' ? '🟡' : '🔴'} 网络往返 ${_connMs}ms` +
      (_connMs >= 120 ? '(偏慢:多半 Tailscale 掉中继/弱网)' : '')) +
      '\n版本 ' + ver);   // 一眼查阅读器版本:出 bug 时点圆点报这个版本号,确认是否最新
    _connProbe();
  };
  tb.appendChild(_connDot);
  _connProbe();
  setInterval(_connProbe, 30000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) _connProbe(); });
})();
