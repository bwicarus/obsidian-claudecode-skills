// facade.js — Shadow DOM 宿主 + document 门面(必须在 vendor/rc-*.js 之前加载)。
//
// 设计:rc-*.js 共享层(阅读器原文件)**逐字零改动**搬进扩展;build.py 把每份源码包一层
//   ;(function(document){ <原文> })(window.__bwReaderDoc);
// 用参数遮蔽全局 document,把它们的 getElementById/head/body/documentElement/事件监听
// 全部重定向进扩展自己的 Shadow DOM——源文件不 fork、不 drift,随时从阅读器重新拉最新版。
//
// 对应测绘结论(webext-reader-chrome-portspec):
//   · rc-* 的 DOM 根引用是 verbatim 复用的最大障碍 → 用门面一次解决,不逐文件 sed;
//   · 事件监听的 e.target 在 shadow 边界会 retarget 到 host → 包一层 composedPath()[0] 代理,
//     否则「点外关闭」判定把 shadow 内点击误判为框外(rc-sidedrawer 设置弹层就踩这个)。
(() => {
  "use strict";
  if (window.__bwReaderDoc) return;   // 幂等

  // ── Shadow 宿主:挂 documentElement 下,0×0 不占布局;fixed 子元素自己逃逸到视口 ──
  const host = document.createElement("div");
  host.id = "bw-reader-host";
  host.style.cssText = "position:absolute;top:0;left:0;width:0;height:0;z-index:2147483647;";
  document.documentElement.appendChild(host);
  const shadow = host.attachShadow({ mode: "open" });

  // 样式落点(充当 document.head 角色)。样式放 shadow 内任意位置都作用于整棵 shadow 树。
  const headEl = document.createElement("div");
  headEl.id = "bw-head";
  shadow.appendChild(headEl);

  // 根容器(充当 document.body / documentElement 双角色):
  //   · rc-sidedrawer 往 body 挂抽屉/把手、往 body toggle .ep-side-open 类 → 落这里;
  //   · rc-sidedrawer 往 documentElement 设 --gp-blur CSS 变量 → 落这里(变量继承到抽屉)。
  const root = document.createElement("div");
  root.id = "bw-root";
  shadow.appendChild(root);

  // ── 事件包装:e.target 换成 composedPath()[0](shadow 内真实目标),其余透传 ──
  const wrapHandler = (fn) => {
    const wrapped = (e) => {
      const proxied = new Proxy(e, {
        get(t, p) {
          if (p === "target") {
            try { const cp = e.composedPath(); if (cp && cp.length) return cp[0]; } catch (_) {}
            return e.target;
          }
          const v = t[p];
          return typeof v === "function" ? v.bind(t) : v;
        }
      });
      return fn.call(document, proxied);
    };
    return wrapped;
  };
  const handlerMap = new WeakMap();   // 原始 fn → wrapped(供 removeEventListener)

  // ── document 门面:重定向 DOM 根,其余(createElement/createTextNode/…)透传真 document ──
  window.__bwReaderDoc = new Proxy(document, {
    get(t, p) {
      if (p === "getElementById") return (id) => shadow.getElementById(id);
      if (p === "querySelector") return (s) => shadow.querySelector(s);
      if (p === "querySelectorAll") return (s) => shadow.querySelectorAll(s);
      if (p === "head") return headEl;
      if (p === "body") return root;
      if (p === "documentElement") return root;
      if (p === "addEventListener") {
        return (type, fn, opts) => {
          const w = wrapHandler(fn);
          handlerMap.set(fn, w);
          document.addEventListener(type, w, opts);
        };
      }
      if (p === "removeEventListener") {
        return (type, fn, opts) => document.removeEventListener(type, handlerMap.get(fn) || fn, opts);
      }
      const v = t[p];
      return typeof v === "function" ? v.bind(t) : v;
    },
    set(t, p, v) { t[p] = v; return true; }
  });

  // ── fetch 门面:vendor 包装把 rc-* 的 fetch 一并遮蔽成这个 ──
  // rc-* 全部用相对路径(/pdf/api/*、/api/assistant/*…)→ 重写到 Pi ORIGIN;
  // 跨源 + Bearer + SSE 统一走 background 长连 port(content script 的 fetch 受宿主页 CORS 限制,
  // background 有 host_permissions 才能带 Bearer 直连)。流式响应用 ReadableStream 原样重建,
  // rc-assistant 的 getReader() 打字机 / rid 续传语义不变。非本服务的绝对 URL(如词典音频)走原生 fetch。
  const ORIGIN = "https://bwicarus.taile44d0c.ts.net";
  let _port = null, _seq = 0;
  const _pending = new Map();
  function ensurePort() {
    if (_port) return _port;
    _port = chrome.runtime.connect({ name: "bw-fetch" });
    _port.onMessage.addListener((m) => {
      const p = _pending.get(m.id);
      if (!p) return;
      if (m.type === "head") p.head(m);
      else if (m.type === "chunk") p.chunk(m.b64);
      else if (m.type === "done") { p.done(); _pending.delete(m.id); }
      else if (m.type === "error") { p.error(m.error); _pending.delete(m.id); }
    });
    _port.onDisconnect.addListener(() => {
      _port = null;
      for (const p of _pending.values()) p.error("bridge disconnected");
      _pending.clear();
    });
    return _port;
  }
  window.__bwReaderFetch = function (url, init) {
    init = init || {};
    let u = String(url);
    if (u.startsWith("/")) u = ORIGIN + u;
    if (!u.startsWith(ORIGIN + "/")) return fetch(url, init);   // 外站资源走原生
    const id = ++_seq;
    return new Promise((resolve, reject) => {
      let ctrl = null;
      const stream = new ReadableStream({ start(c) { ctrl = c; } });
      let settled = false;
      _pending.set(id, {
        head(m) {
          settled = true;
          resolve(new Response(stream, { status: m.status, statusText: m.statusText || "", headers: m.headers || {} }));
        },
        chunk(b64) {
          try {
            const bin = atob(b64), arr = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
            ctrl.enqueue(arr);
          } catch (_) {}
        },
        done() { try { ctrl.close(); } catch (_) {} },
        error(e) {
          try { ctrl.error(new Error(e)); } catch (_) {}
          if (!settled) reject(new TypeError(e));
        }
      });
      const headers = {};
      try { new Headers(init.headers || {}).forEach((v, k) => { headers[k] = v; }); } catch (_) {}
      ensurePort().postMessage({
        id, url: u,
        init: { method: init.method || "GET", headers, body: (typeof init.body === "string") ? init.body : undefined }
      });
      if (init.signal) {
        init.signal.addEventListener("abort", () => {
          try { ensurePort().postMessage({ abort: id }); } catch (_) {}
          const p = _pending.get(id);
          if (p) { p.error("aborted"); _pending.delete(id); }
        });
      }
    });
  };

  // ── MathJax 配置(必须在 vendor/mathjax-full.js 之前定义;tex 配置逐字来自 pdf_reader.html:15)──
  // 差异仅一处:startup.typeset:false —— 扩展跑在任意网页上,绝不能自动排版宿主页正文
  // (宿主页里的 "$5" 之类会被误当公式);我们只经 RC.typeset(el) 对自己 shadow 里的节点手动排版。
  window.MathJax = {
    tex: { inlineMath: [["$", "$"], ["\\(", "\\)"]], displayMath: [["$$", "$$"], ["\\[", "\\]"]] },
    options: { skipHtmlTags: ["script", "noscript", "style", "textarea", "pre", "code"] },
    startup: { typeset: false }
  };

  // shell.js / 调试用句柄
  window.__bwShadow = shadow;
  window.__bwRoot = root;
  window.__bwHead = headEl;
})();
