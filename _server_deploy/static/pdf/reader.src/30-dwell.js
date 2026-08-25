// 30-dwell.js — 读页停留追踪(注意力画像的「读过这页」原始数据;设计 references/attention-kb-design.md)。
//   用户要求的严谨判定,三重排除全在采集端:
//   ① 卡加载排除:当前页的**页图真实渲染完成**(img.complete && naturalWidth>0)才计秒——加载不出来=看不见=不算读;
//   ② 快翻排除:按秒累计,单页 <3s 的碎片不上报(翻过≠读过);服务端再设 15s/日 阈值;
//   ③ 挂机排除:60s 无任何交互(滚动/触摸/按键)即停表;页面切后台立即停表。
//   只采原始秒数,「读过」的判定阈值在服务端聚合器(attention_profile.DWELL_MIN_S)——阈值可调可重放。
(() => {
  if (typeof FILE_REL === 'undefined' || !FILE_REL) return;
  if (FILE_REL.indexOf('/.sandbox/') >= 0) return;          // 沙盒测试不采
  const acc = {};                                            // page → secs
  let lastAct = Date.now();
  ['scroll', 'touchstart', 'pointerdown', 'keydown', 'wheel'].forEach((ev) =>
    window.addEventListener(ev, () => { lastAct = Date.now(); }, { passive: true, capture: true }));

  //   ④ 虚拟页码(用户设计):自建页记它的 **uid**(u_xxxx,插删页都不变)而不是页码 —— 永不漂移;
  //      真实页只能记页码(真插入 PDF 后物理页号必变),靠服务端 PAGE_ANCHOR_MIGRATIONS 迁移。
  function curLoadedKey() {                                  // 视口中线页,且内容真的渲染出来了
    let key = '', best = -1;
    const mid = window.innerHeight / 2;
    document.querySelectorAll('.page-wrap[data-page-num], .pdf-upage').forEach((pw) => {
      const r = pw.getBoundingClientRect();
      if (!r.height) return;
      let ov = Math.min(r.bottom, window.innerHeight) - Math.max(r.top, 0);
      if (ov <= 0) return;
      if (r.top <= mid && r.bottom >= mid) ov += 1e6;
      if (ov > best) {
        best = ov;
        const img = pw.querySelector('img');
        const ok = img ? (img.complete && img.naturalWidth > 0)
                       : !!pw.querySelector('canvas, .up2-blocks, .pdf-upage-overlay, .textLayer');
        if (!ok) { key = ''; return; }                        // 没渲染出来 = 看不见 = 这秒不计
        const uid = pw.dataset.uid || (pw.__upRec && pw.__upRec.id) || '';
        key = uid ? ('u:' + uid) : ('p:' + (parseInt(pw.dataset.pageNum, 10) || 0));
      }
    });
    return (key === 'p:0') ? '' : key;
  }

  setInterval(() => {
    if (document.visibilityState !== 'visible') return;
    if (Date.now() - lastAct > 60000) return;                // 挂机:停表
    const k = curLoadedKey();
    if (k) acc[k] = (acc[k] || 0) + 1;
  }, 1000);

  let lastFlush = Date.now();
  function flush(useBeacon) {
    const entries = Object.entries(acc).filter(([, s]) => s >= 3);   // <3s 碎片=翻过,不上报
    if (!entries.length) return;
    entries.forEach(([k]) => delete acc[k]);
    // 活动账本 §3.3：带上「我是哪个端」。服务端补不出来 ——
    // 它从不读 UA/Origin，而认证形态只能二分（扩展 Bearer / App 与桌面都是
    // session cookie），三分只能由客户端自报。
    const client = (window.RC && RC.clientRole) ? RC.clientRole() : '';
    // 活动账本 §3.4「地点」：App 的 Swift 侧把最近定位推在这个全局变量里
    //（开关关着/没授权/非 App 环境就没有）。必须走全局而不是异步取 ——
    // pagehide 的 beacon flush 是同步的，等不了任何 Promise。
    // 只带新鲜的（≤30min）：一条 dwell 配一个两小时前的位置是错误数据。
    const payload = { file: FILE_REL, client: client, dwell: entries.map(([k, s]) => (
      k.charAt(0) === 'u' ? { upage: k.slice(2), secs: s } : { page: +k.slice(2), secs: s })) };
    const loc = window.__BW_DEVICE_LOCATION__;
    if (loc && typeof loc === 'object' &&
        Number.isFinite(loc.lat) && Number.isFinite(loc.lon) &&
        Number.isFinite(loc.at) && (Date.now() / 1000 - loc.at) < 1800) {
      payload.loc = { lat: loc.lat, lon: loc.lon,
                      acc: Number.isFinite(loc.acc) ? loc.acc : null,
                      at: Math.floor(loc.at) };
      if (typeof loc.name === 'string' && loc.name) {
        payload.loc.name = loc.name.slice(0, 80);
      }
    }
    const body = JSON.stringify(payload);
    try {
      if (useBeacon && navigator.sendBeacon) {
        // The native App owns this same-origin route through a synchronous
        // WK bridge. Keep the JSON string available synchronously so a
        // visibility/pagehide flush cannot be lost while Blob.text() is still
        // queued; the web/PWA path retains the old application/json Blob.
        const payload = window.__BW_NATIVE_LOCAL_READER__ === true
          ? body : new Blob([body], { type: 'application/json' });
        // @interaction learning.read-dwell.report
        navigator.sendBeacon('/pdf/api/read-dwell', payload);
      } else {
        fetch('/pdf/api/read-dwell', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                       body, keepalive: true }).catch(() => {});
      }
      lastFlush = Date.now();
    } catch (e) {}
  }
  setInterval(() => { if (Date.now() - lastFlush > 30000) flush(false); }, 5000);
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden') flush(true); });
})();
