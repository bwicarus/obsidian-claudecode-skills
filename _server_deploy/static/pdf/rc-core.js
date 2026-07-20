/* rc-core.js — 统一控制层(Reader Control / window.RC)的地基。
 * 目标:PDF 阅读器(reader.src/*.js)和 EPUB 阅读器(epub-html.js)**共用一份控制层代码**,
 * 各自只提供一个「适配器」(选区/坐标/锚点/跳转/上下文/端点/能力开关)——底座耦合的唯一落点。
 * 本文件零底座耦合:只做命名空间引导 + 适配器注册 + 通用工具。是 rc-md / rc-* 的底座。
 * 迁移策略:先抽共享模块 + 只接 EPUB 验证;再逐个把 PDF 部件 behind window.RC_USE.<mod> flag 切过来(旧实现保留)。
 */
(function () {
  if (window.RC && window.RC.use) return;   // 按能力守卫:别的模块先建了空 RC 壳(如曾被 rc-outbox 抢先)也不跳过初始化;已有成员由下方赋值保留语义=覆盖为正版
  // 【iOS 根治】button 的原生外观(push-button)会画一层浅色圆角块盖住自定义 background → 用户看到的「白色方块」。
  // 桌面 Chromium 不画 ⇒ headless 测不出。这里在共享层地基上兜底一次(最低优先级,零回归),覆盖没有引 pdf/epub-styles 的页面。
  try {
    if (!document.getElementById('rc-btn-reset')) {
      var _bs = document.createElement('style'); _bs.id = 'rc-btn-reset';
      _bs.textContent = 'button{-webkit-appearance:none;appearance:none}';
      (document.head || document.documentElement).insertBefore(_bs, (document.head || document.documentElement).firstChild);
    }
  } catch (e) {}
  var _pre = window.RC || {};   // 先到的成员(如 rc-outbox.outbox)保留
  var RC = window.RC = {
    _adapter: null,
    // 各 reader 在自己脚本末尾 RC.use(adapter) 注册整套适配器方法
    use: function (adapter) { RC._adapter = adapter || {}; return RC; },
    adapter: function () { return RC._adapter || {}; },
    // 能力开关位:门控 reader 专属逻辑(公式/单击词/语音/SSE三源…),防另一端报错或误触
    config: function () { var a = RC._adapter; return (a && a.config) || {}; },
    // 所有后端 URL 参数化(PDF=/api/assistant/chat,EPUB=/pdf/api/epub-chat 等)
    endpoints: function () { var a = RC._adapter; return (a && a.getEndpoints && a.getEndpoints()) || {}; },
    // ── 通用工具(纯,无底座耦合)──
    esc: function (s) { var d = document.createElement('div'); d.textContent = (s == null ? '' : String(s)); return d.innerHTML; },
    debounce: function (fn, ms) { var t; return function () { var a = arguments, c = this; clearTimeout(t); t = setTimeout(function () { fn.apply(c, a); }, ms); }; },
    // 容错 fetch JSON
    reqJson: function (method, url, body) {
      var o = { method: method, headers: {} };
      if (body) { o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(body); }
      return fetch(url, o).then(function (r) { return r.json(); });
    },
    // 共享 toast(适配器可设 RC._adapter.toast 覆盖样式;默认底部居中)
    toast: function (msg) {
      var a = RC._adapter; if (a && a.toast) { try { a.toast(msg); return; } catch (e) {} }
      var el = document.getElementById('rc-toast');
      if (!el) {
        el = document.createElement('div'); el.id = 'rc-toast';
        el.style.cssText = 'position:fixed;left:50%;bottom:44px;transform:translateX(-50%);background:#10162a;border:1px solid #3b6db5;color:#cfe6ff;padding:8px 16px;border-radius:8px;font-size:13px;z-index:9000;box-shadow:0 6px 16px rgba(0,0,0,.6);transition:opacity .2s;pointer-events:none';
        document.body.appendChild(el);
      }
      el.textContent = msg; el.style.opacity = '1';
      clearTimeout(RC._toastT); RC._toastT = setTimeout(function () { el.style.opacity = '0'; }, 1400);
    }
  };
  for (var k in _pre) { if (!(k in RC)) RC[k] = _pre[k]; }   // 合并先到成员
})();
