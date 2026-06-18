// ── 00-resilient-fetch.js:全站网络韧性(最先加载,最底层兜住「切后台 Load failed」)──
// iOS 切后台/锁屏会掐死进行中的请求,回前台后该请求 reject「TypeError: Load failed」。这里在 fetch 层统一兜:
//   ① 包装 window.fetch:GET/HEAD(幂等读)瞬断时**先等回到前台、再退避重试**(写请求不自动重试,防重复提交);
//   ② 暴露 window.__safeFetch:给「幂等但用 POST 的计算类请求」(如 AI 分析)显式开启重试。
// 重要边界:fetch() 只在「还没收到响应(连接失败/被掐)」时 reject;一旦返回 Response 就交还调用方,
//   所以**流式响应 body 读到一半断了不归这里管**——那是各功能各自的恢复(助手=拉历史补全、_aiStream=rid 轮询)。
(function () {
  if (window.__resilientFetch || !window.fetch) return;
  window.__resilientFetch = true;
  var _orig = window.fetch.bind(window);

  function whenVisible() {   // 当前在后台 → 等回到前台再继续(后台发请求多半也被掐)
    return new Promise(function (res) {
      if (document.visibilityState !== 'hidden') return res();
      var h = function () { if (document.visibilityState !== 'hidden') { document.removeEventListener('visibilitychange', h); res(); } };
      document.addEventListener('visibilitychange', h);
    });
  }
  function methodOf(input, init) {
    if (init && init.method) return String(init.method).toUpperCase();
    if (input && typeof input === 'object' && input.method) return String(input.method).toUpperCase();
    return 'GET';
  }
  async function run(input, init, maxRetry) {
    var i = 0;
    while (true) {
      try { return await _orig(input, init); }
      catch (e) {
        if (e && e.name === 'AbortError') throw e;     // 主动取消(用户停止 / 代码 abort)→ 不重试
        if (i++ >= maxRetry) throw e;                  // 重试用尽 → 抛回调用方(各自的 catch 兜底)
        await whenVisible();                           // 先回前台
        await new Promise(function (r) { setTimeout(r, Math.min(300 * i, 1200)); });   // 退避
      }
    }
  }
  window.fetch = function (input, init) {
    var m = methodOf(input, init);
    return run(input, init, (m === 'GET' || m === 'HEAD') ? 3 : 0);   // 写请求 maxRetry=0:只跑一次
  };
  // 幂等计算类(POST 但无持久副作用,如 grammar-analyze/translate)可显式要重试
  window.__safeFetch = function (input, init, conf) {
    conf = conf || {};
    return run(input, init, conf.retries == null ? 3 : conf.retries);
  };
})();
