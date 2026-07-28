/* web-adapter.js — PWA 内实况网页的独立 DocumentHost adapter。
 *
 * 网页继续复用 PDF 阅读器模板里的顶栏、侧栏和共享 rc-* UI，但内容能力不继承 PdfAdapter：
 *   ① 顶栏标题位插一个地址栏(输网址/搜索、后退、📄阅读模式)
 *   ② 与 opaque sandbox iframe 桥接选区/正文/沉浸翻译
 *   ③ 以独立 window.WebAdapter 经 RC.use() 登记 kind=web
 *
 * WebAdapter 只组合共享 UI/assistant chrome；PDF 字符层、页几何、页锚、区域渲染和 PDF
 * sidecar 从不转抄为网页能力。当前只支持 URL navigation；DOM/quote anchor 仍明确 pending。
 */
(function () {
  var CFG = window.__PDF_CFG || {};
  if (!CFG.web_url) return;                      // 普通 PDF:本文件完全不生效
  document.body.classList.add('web-mode');

  var CUR = CFG.web_url;
  var _hist = [];
  var _sel = { text: '', ctx: '', rect: null, clientRect: null };
  var _pageText = '', _title = '';
  var RBI = !!CFG.web_rbi;   // RBI 模式:iframe 内容来自 Pi 真 Chrome 渲染(过验证/带登录态)
  var BRIDGE = String(CFG.web_bridge_nonce || '');
  var NAV_TICKET = String(CFG.web_navigation_ticket || '');
  function frameSrc(u) {
    var out = '/pdf/web/' + (RBI ? 'rbi' : 'frame') + '?url=' + encodeURIComponent(u);
    if (NAV_TICKET) out += '&__bwnav=' + encodeURIComponent(NAV_TICKET);
    return out;
  }
  var TR_NAME = { para: '独立段落', small: '下方小字', replace: '替换原文' };
  var _trOn = false;
  var _trStyle = (function () { try { return localStorage.getItem('rcWebTrStyle') || 'para'; } catch (e) { return 'para'; } })();
  function newBridgeNonce() {
    try {
      var bytes = new Uint8Array(24);
      crypto.getRandomValues(bytes);
      return Array.prototype.map.call(bytes, function (n) {
        return n.toString(16).padStart(2, '0');
      }).join('');
    } catch (e) {
      return '';
    }
  }
  function post(m) { try { frame().contentWindow.postMessage(m, '*'); } catch (e) {} }

  function frame() { return document.getElementById('wl-frame'); }
  function toast(m) { try { window.RC && RC.toast ? RC.toast(m) : 0; } catch (e) {} }

  // ── ① 顶栏地址栏(替换书名位;其余按钮=PDF 原样)──
  function mountBar() {
    // ⚠ 审计 #10 实锤:模板顶栏是 `#header > h1`(不是 #pdf-top/#pdf-title)——首版三个选择器
    //   全 0 命中、每次 if(!top) return 早退,地址栏/后退/📄 从未存在过,h1 还一直显示整条 URL。
    var top = document.getElementById('header');
    if (!top) return;
    var title = top.querySelector('h1');
    var box = document.createElement('input');
    box.id = 'wl-url'; box.value = CUR; box.spellcheck = false; box.autocomplete = 'off';
    box.title = '地址栏:输网址直接打开,输词走搜索';
    box.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter') return;
      var v = (this.value || '').trim(); if (!v) return;
      var isUrl = v.indexOf('http') === 0 || /^[\w-]+(\.[\w-]+)+([/]|$)/.test(v);
      if (isUrl) go(v); else location.href = '/pdf/web?q=' + encodeURIComponent(v);
    });
    var back = document.createElement('button');
    back.textContent = '←'; back.title = '后退';
    back.onclick = function () { var p = _hist.pop(); if (p) go(p, false); else location.href = '/pdf/web?home=1'; };
    var rd = document.createElement('button');
    rd.textContent = '📄'; rd.title = '阅读模式:抽正文进阅读器(可高亮/存 vault/进搜索与概念网)';
    rd.onclick = readerMode;
    // 「译」= 沉浸式双语对照。沿用 PDF 阅读器**译页**的心智:一个开关,开着就译当前看得见的,
    // 滚到哪译到哪(引擎在代理页内,见 web-immersive.js)。旁边的小按钮循环切三种样式。
    var tr = document.createElement('button');
    tr.id = 'wl-tr'; tr.textContent = '译'; tr.title = '沉浸式翻译:双语对照(滚到哪译到哪)';
    tr.onclick = function () {
      _trOn = !_trOn;
      tr.style.color = _trOn ? '#7fb2ff' : '';
      post({ __rcweb: 'translate', on: _trOn, style: _trStyle });
      toast(_trOn ? '沉浸式翻译:开(' + TR_NAME[_trStyle] + ')' : '沉浸式翻译:关');
    };
    // 「↗」在系统浏览器打开当前页。需要登录/带反机器人墙的站(claude.ai 这类)的唯一出路:
    //   我们的代理是 Pi 上**另一个 HTTP 客户端**,你在别的标签页登录的 cookie 属于你的设备,
    //   永远送不到它手里 —— 所以"新标签登录完回来继续用"这条路在架构上不成立。
    //   (反过来,**在本窗口内直接登录**是可以的:服务端按用户维护 cookie jar,实测能保持。)
    var ext = document.createElement('button');
    ext.id = 'wl-ext'; ext.textContent = '↗';
    ext.title = '在系统浏览器打开这一页(需要登录、或被反机器人验证拦住的站走这里)';
    ext.onclick = function () { try { window.open(CUR, '_blank', 'noopener'); } catch (e) {} };
    // 🔑 为当前站导入登录 cookie:解决"代理没有你的登录态"(B站图片防盗链/登录框都靠它)
    // 🖥 一键切到 RBI 真浏览器版(Pi 跑真 Chrome:过 Cloudflare / 图片全 / 可登录态)
    var rbi = document.createElement('button');
    rbi.id = 'wl-rbi'; rbi.textContent = '🖥';
    rbi.title = '真浏览器版(Pi 跑真 Chrome 渲染:过验证、图片全、DOM 完整;查词接入中)';
    rbi.onclick = function () { location.href = '/pdf/web/rbi-live?url=' + encodeURIComponent(CUR); };
    var key = document.createElement('button');
    key.id = 'wl-key'; key.textContent = '🔑';
    key.title = '导入登录 cookie：仅用于当前精确主机的 HTTPS 请求';
    key.onclick = function () {
      var host = ''; try { host = new URL(CUR).hostname; } catch (e) {}
      var dom = prompt('为哪个精确主机导入登录 cookie？不会发送给父域、兄弟子域或 HTTP。', host);
      if (!dom) return;
      var ck = prompt('粘贴该站的 cookie 字符串(形如 SESSDATA=xxx; bili_jct=yyy)。\n\n' +
                      '获取:电脑浏览器登录该站 → F12 开发者工具 → Application/存储 → Cookies → 复制。\n\n' +
                      '⚠ 这是你的登录凭证，会以 Secure + 精确主机方式保存在服务器上。服务器若被入侵，该账号仍可能被盗。仅对你信任的站这么做。');
      if (!ck) return;
      fetch('/pdf/api/web-cookie', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ domain: dom, cookie: ck }) })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          toast(d.ok ? ('已导入 ' + d.count + ' 个 cookie,重新加载…') : ('✗ ' + (d.error || '失败')));
          if (d.ok) setTimeout(function () { var f = frame(); if (f) f.src = f.src; }, 700);
        }).catch(function () { toast('网络错误'); });
    };
    var ts = document.createElement('button');
    ts.id = 'wl-trs'; ts.textContent = '⋮'; ts.title = '切换译文样式';
    ts.onclick = function () {
      var ks = ['para', 'small', 'replace'];
      _trStyle = ks[(ks.indexOf(_trStyle) + 1) % ks.length];
      try { localStorage.setItem('rcWebTrStyle', _trStyle); } catch (e) {}
      post({ __rcweb: 'translate', style: _trStyle });
      toast('译文样式:' + TR_NAME[_trStyle]);
    };
    if (title) {
      title.style.display = 'none';
      title.parentNode.insertBefore(back, title);
      title.parentNode.insertBefore(box, title);
      title.parentNode.insertBefore(rd, title.nextSibling);
      title.parentNode.insertBefore(tr, rd.nextSibling);
      title.parentNode.insertBefore(ts, tr.nextSibling);
      title.parentNode.insertBefore(ext, ts.nextSibling);
      title.parentNode.insertBefore(key, ext.nextSibling);
      title.parentNode.insertBefore(rbi, key.nextSibling);
    } else {
      [back, box, rd, tr, ts, ext, key, rbi].reverse().forEach(function (el) { top.insertBefore(el, top.firstChild); });
    }
  }

  function go(u, push) {
    if (!u) return;
    if (!/^https?:\/\//.test(u)) u = 'https://' + u;
    if (push !== false && CUR) _hist.push(CUR);
    CUR = u; CFG.web_url = u; _pageText = '';
    var b = document.getElementById('wl-url'); if (b) b.value = u;
    var targetFrame = frame();
    var nextNonce = newBridgeNonce();
    if (nextNonce) BRIDGE = nextNonce;
    targetFrame.name = 'bw-web-bridge:' + BRIDGE;
    targetFrame.src = frameSrc(u);   // 地址栏输入/后退;RBI 模式走真浏览器渲染
    try { history.replaceState(null, '', '/pdf/web/live?url=' + encodeURIComponent(u)); } catch (e) {}
    setTimeout(askText, 1200);
    if (_trOn) setTimeout(function () { post({ __rcweb: 'translate', on: true, style: _trStyle }); }, 1400);
  }
  window.wlGo = go;

  function readerMode() {
    toast('抽取正文中…');
    fetch('/pdf/api/web-fetch', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: CUR }) }).then(function (r) { return r.json(); }).then(function (d) {
        if (d.ok && d.file) location.href = '/pdf/html/view?file=' + encodeURIComponent(d.file);
        else toast('✗ ' + (d.error || '抽取失败'));
      }).catch(function () { toast('网络错误'); });
  }

  // ── ② 与代理 iframe 桥接 ──
  function askText() { try { frame().contentWindow.postMessage({ __rcweb: 'getText' }, '*'); } catch (e) {} }
  // iframe 自己导航后,只被动同步地址栏/历史(**不**回设 frame.src —— iframe 已经在新页了,
  // 再 set 会二次加载)。这是 iPad 导航可靠性的关键:导航在 iframe 内完成,外壳不参与跳转。
  function onLocated(u) {
    if (!u || u === CUR) return;
    if (CUR) _hist.push(CUR);
    CUR = u; CFG.web_url = u; _pageText = '';
    var b = document.getElementById('wl-url'); if (b) b.value = u;
    try { history.replaceState(null, '', '/pdf/web/live?url=' + encodeURIComponent(u)); } catch (e) {}
    setTimeout(askText, 1200);
    if (_trOn) setTimeout(function () { post({ __rcweb: 'translate', on: true, style: _trStyle }); }, 1400);
  }
  var _apiWindowAt = Date.now(), _apiCount = 0, _apiInflight = 0;
  function safeExternalUrl(value) {
    value = String(value || '').trim();
    if (value.length > 8192) return '';
    try {
      var parsed = new URL(value);
      return (parsed.protocol === 'http:' || parsed.protocol === 'https:') ? parsed.href : '';
    } catch (e) { return ''; }
  }
  function sameExternalUrl(left, right) {
    var a = safeExternalUrl(left), b = safeExternalUrl(right);
    return !!a && !!b && a === b;
  }
  function safeRect(value) {
    value = value || {};
    var out = {};
    ['left', 'top', 'right', 'bottom'].forEach(function (key) {
      var number = Number(value[key]);
      out[key] = Number.isFinite(number) ? Math.max(-100000, Math.min(100000, number)) : 0;
    });
    out.width = Math.max(0, out.right - out.left);
    out.height = Math.max(0, out.bottom - out.top);
    return out;
  }
  function shellRect(value) {
    var inner = safeRect(value);
    var fr = frame();
    var outer = fr && fr.getBoundingClientRect ? fr.getBoundingClientRect() : { left: 0, top: 0 };
    return {
      left: outer.left + inner.left,
      top: outer.top + inner.top,
      right: outer.left + inner.right,
      bottom: outer.top + inner.bottom,
      width: inner.width,
      height: inner.height
    };
  }
  function selectionController() {
    return window.__bwSelectionController || null;
  }
  function publishSelection() {
    var controller = selectionController();
    if (!controller || typeof controller.acceptExternal !== 'function') return false;
    controller.acceptExternal({
      source: 'web',
      text: _sel.text,
      context: _sel.ctx,
      rect: _sel.clientRect,
      data: { url: CUR }
    });
    return true;
  }
  function sharedAssistantHost() {
    try {
      return window.PdfAdapter && PdfAdapter._host && PdfAdapter._host.asst
        ? PdfAdapter._host.asst : null;
    } catch (e) { return null; }
  }
  function callSharedHost(name, args, fallback) {
    var host = sharedAssistantHost();
    if (host && typeof host[name] === 'function') {
      try { return host[name].apply(host, args || []); } catch (e) {}
    }
    return typeof fallback === 'function' ? fallback() : fallback;
  }
  function webFile() { return 'web:' + CUR; }
  function ensureResultConfig() {
    if (!(window.RC && RC.result && RC.result.config) || WebAdapter._resultCfgDone) return;
    RC.result.config({
      draftKey: 'web-drafts',
      aiParams: function () { return {}; },
      beforeOpen: function () {
        try { if (window._pushQueryHistory) window._pushQueryHistory(); } catch (e) {}
      }
    });
    WebAdapter._resultCfgDone = true;
  }

  // 只组合与内容几何无关的共享 assistant chrome。这里没有 noteMount/anchorFromPoint、
  // char range、page highlight 等 PDF host 方法，因此不会把“模板复用”误报成“网页支持 PDF 锚”。
  var WebAssistantHost = {
    md: function (text) {
      return callSharedHost('md', [text], function () {
        return window.RC && RC.md ? RC.md(text) : String(text == null ? '' : text);
      });
    },
    toast: function (message) { return callSharedHost('toast', [message], function () { toast(message); }); },
    fmtTime: function (ms) { return callSharedHost('fmtTime', [ms], ''); },
    fileRel: webFile,
    pdfNumPages: function () { return 1; },       // 共享侧栏历史 DTO 仍要求一个有限位置总数；不是 PDF 能力声明
    locCount: function () { return 1; },
    dispPage: function () { return 1; },
    pdfFromDisp: function () { return 1; },
    goTo: function () { return false; },          // URL 跳转只允许走 WebAdapter.navigate
    changePage: function () { return false; },
    fitWidth: function () { return false; },
    zoomBy: function () { return false; },
    toggleTranslate: function () {
      var button = document.getElementById('wl-tr');
      if (button) { button.click(); return true; }
      return false;
    },
    openDrawer: function () { return callSharedHost('openDrawer', [], false); },
    switchTab: function (name) { return callSharedHost('switchTab', [name], false); },
    mountPanel: function () {
      return document.getElementById('ep-side') || document.getElementById('grammar-panel');
    },
    mountTabs: function () {
      return document.getElementById('ep-side-tabs') || document.getElementById('side-tabs');
    },
    asstOpen: function () { return callSharedHost('asstOpen', [], false); },
    voiceContext: function () { return WebAdapter.getContext(); },
    setFocusSel: function (text, kind) { return callSharedHost('setFocusSel', [text, kind], false); },
    focusSel: function () { return callSharedHost('focusSel', [], null); },
    clearFigFocus: function () { return callSharedHost('clearFigFocus', [], false); },
    clearNoteAttached: function () { return callSharedHost('clearNoteAttached', [], false); },
    renderNoteChips: function () { return callSharedHost('renderNoteChips', [], false); },
    hlUrl: function () { return '/pdf/api/html-highlights'; },
    notesUrl: function () { return '/pdf/api/notes'; },
    noteCompositeUrl: function () { return '/pdf/api/note-composite'; },
    chatUrl: function () { return '/api/assistant/chat'; },
    historyUrl: function () { return '/api/assistant/history'; },
    clearUrl: function () { return '/api/assistant/clear'; }
  };

  var WebAdapter = window.WebAdapter = {
    kind: 'web',
    _host: { asst: WebAssistantHost },
    _resultCfgDone: false,
    config: (window.RC && RC.contract && RC.contract.adapterConfig)
      ? RC.contract.adapterConfig('web', {
          isPDF: false,
          reflow: true,
          hasFigures: false,
          hasImages: true,
          renderRegion: false,
          anchorKind: 'web-quote',
          popupMode: 'fixed',
          supportsVoice: true
        })
      : {
          isPDF: false, reflow: true, hasFigures: false, hasImages: true,
          renderRegion: false, anchorKind: 'web-quote', popupMode: 'fixed',
          supportsVoice: true
        },
    fileInfo: function () { return { file: webFile(), url: CUR, title: _title || CUR }; },
    getContext: function () {
      var context = {
        page_type: 'web',
        file: webFile(),
        book: _title || CUR,
        book_name: _title || CUR,
        url: CUR,
        total: 1,
        total_pages: 1,
        page: 1,
        pages: [1],
        visible_text: _pageText.slice(0, 4000),
        langs: langs()
      };
      if (_sel.text) {
        context.selection = _sel.text;
        context.selection_sentence = _sel.ctx;
      }
      return context;
    },
    captureSelection: function () {
      if (!_sel.text) return null;
      var selection = {
        text: _sel.text,
        context: _sel.ctx,
        ctx: _sel.ctx,
        sentence: _sel.ctx,
        rect: _sel.clientRect,
        anchor: null,
        data: { url: CUR, source: 'web' }
      };
      return window.RC && RC.contract && RC.contract.selection
        ? RC.contract.selection(selection) : selection;
    },
    clearSelection: function () {
      _sel = { text: '', ctx: '', rect: null, clientRect: null };
      var controller = selectionController();
      if (controller && typeof controller.clearExternal === 'function') controller.clearExternal('web');
    },
    currentChapterText: function () { return _pageText.slice(0, 8000); },
    currentLocation: function () {
      return { unit: 'url', index: 0, total: 1, data: { url: CUR } };
    },
    navigate: function (target) {
      var data = target && target.data ? target.data : (target || {});
      if (!data.url) return false;
      go(data.url);
      return true;
    },
    getEndpoints: function () {
      return {
        dict: '/pdf/api/dict',
        translate: '/pdf/api/translate',
        explain: '/pdf/api/explain',
        highlights: '/pdf/api/html-highlights'
      };
    },
    lookupWord: function (opts) {
      opts = opts || {};
      var word = String(opts.word || '').trim();
      if (!word) return;
      if (!(window.RC && RC.wordpop && RC.wordpop.show)) {
        if (opts.fallback) opts.fallback(word, opts.context || '');
        return;
      }
      RC.wordpop.show({
        word: word,
        rect: opts.anchorRect || (_sel && _sel.clientRect) || null,
        ctx: opts.context || '',
        file: webFile(),
        page: 0,
        langs: opts.langs || langs(),
        ignoreSelector: '#sel-toolbar',
        showAnki: false,
        onFallback: function (value) {
          if (opts.fallback) opts.fallback(value, opts.context || '');
        }
      });
    },
    lookupPhrase: function (opts) {
      opts = opts || {};
      var text = String(opts.text || '').trim();
      if (!text) return;
      if (!(window.RC && RC.phrasepop && RC.phrasepop.show)) {
        if (opts.fallback) opts.fallback();
        return;
      }
      RC.phrasepop.show({
        text: text,
        rect: opts.anchorRect || (_sel && _sel.clientRect) || null,
        result: opts.result || null,
        file: webFile(),
        langs: opts.langs || langs(),
        ignoreSelector: '#sel-toolbar',
        // Web 尚无 DOM/quote 持久锚：只复用词组浮层与账户级收藏/掌握，不伪造 PDF 呼吸层。
        onFav: function () {},
        onMastered: function () {},
        onExplain: function () {
          WebAdapter.explain({ text: text, context: _sel.ctx || '' });
        }
      });
    },
    translate: function (opts) {
      opts = opts || {};
      ensureResultConfig();
      if (!(window.RC && RC.result && RC.result.aiCall)) {
        if (opts.fallback) opts.fallback();
        return;
      }
      RC.result.aiCall('/pdf/api/translate', {
        text: opts.text || '', target_lang: '中文'
      }, '🌐 翻译', {});
    },
    explain: function (opts) {
      opts = opts || {};
      ensureResultConfig();
      if (!(window.RC && RC.result && RC.result.aiCall)) {
        if (opts.fallback) opts.fallback();
        return;
      }
      RC.result.aiCall('/pdf/api/explain', {
        text: opts.text || '', context: opts.context || ''
      }, '💡 AI 解释', { kind: 'explain' });
    },
    chat: function (opts) {
      opts = opts || {};
      if (window.RC && RC.ui && RC.ui.openSelectionChat
          && RC.ui.openSelectionChat(opts.text || '', opts.context || '')) return;
      ensureResultConfig();
      if (!(window.RC && RC.result && RC.result.openChat)) {
        if (opts.fallback) opts.fallback();
        return;
      }
      RC.result.openChat(opts.text || '', opts.context || '', { kind: 'chat' });
    },
    openModelSettings: function (opts) {
      opts = opts || {};
      if (window.RC && RC.assistant && RC.assistant.openModelSettings) {
        RC.assistant.openModelSettings(opts.focusAction);
        return;
      }
      if (opts.fallback) opts.fallback();
    },
    splitFollowups: function (text, fallback) {
      if (window.RC && RC.assistant && RC.assistant.splitFollowups) {
        return RC.assistant.splitFollowups(text);
      }
      return fallback ? fallback(text) : null;
    }
  };

  // web-adapter 是 parser-blocking classic script，而 reader.js 是 deferred module：先登记独立 web
  // host，PWA runtime 在 DOMContentLoaded 启动时就不会短暂绑定 PdfAdapter。
  try { if (window.RC && RC.use) RC.use(WebAdapter); } catch (e) {}

  function apiReply(id, payload) {
    post({ __rcweb: 'api-result', id: id, payload: payload || {} });
  }
  function validateSandboxApi(path, method, bodyText) {
    var url;
    try { url = new URL(path, location.origin); } catch (e) { throw new Error('invalid-api-url'); }
    if (url.origin !== location.origin) throw new Error('cross-origin-api');
    method = String(method || 'GET').toUpperCase();
    var body = null;
    if (bodyText) {
      if (String(bodyText).length > 512 * 1024) throw new Error('api-body-too-large');
      try { body = JSON.parse(String(bodyText)); } catch (e) { throw new Error('invalid-api-json'); }
    }
    function texts(limit) {
      var list = body && body.texts;
      if (!Array.isArray(list) || list.length > limit) throw new Error('invalid-api-texts');
      list.forEach(function (text) {
        if (typeof text !== 'string' || text.length > 3000) throw new Error('invalid-api-text');
      });
    }
    if (url.pathname === '/pdf/api/web-translate' && method === 'POST') {
      texts(200);
      if (body && Object.prototype.hasOwnProperty.call(body, 'url')) throw new Error('api-url-forbidden');
    } else if (url.pathname === '/pdf/api/web-vocab' && method === 'POST') {
      texts(500);
    } else {
      throw new Error('api-not-allowed');
    }
    return {
      path: url.pathname + url.search,
      method: method,
      body: bodyText ? String(bodyText) : null
    };
  }
  function handleSandboxApi(message) {
    var id = String(message.id || '');
    if (!/^[A-Za-z0-9_-]{1,80}$/.test(id)) return;
    var now = Date.now();
    if (now - _apiWindowAt >= 60000) {
      _apiWindowAt = now;
      _apiCount = 0;
    }
    if (_apiCount >= 90 || _apiInflight >= 6) {
      apiReply(id, { ok: false, status: 429, error: 'sandbox-api-rate-limit' });
      return;
    }
    var request;
    try {
      request = validateSandboxApi(message.path, message.method, message.body);
    } catch (error) {
      apiReply(id, { ok: false, status: 400, error: String(error && error.message || error) });
      return;
    }
    _apiCount += 1;
    _apiInflight += 1;
    var controller = typeof AbortController === 'function' ? new AbortController() : null;
    var timer = setTimeout(function () { try { if (controller) controller.abort(); } catch (e) {} }, 30000);
    fetch(request.path, {
      method: request.method,
      headers: request.body ? { 'Content-Type': 'application/json' } : {},
      body: request.body,
      credentials: 'same-origin',
      cache: 'no-store',
      signal: controller ? controller.signal : undefined
    }).then(function (response) {
      return response.text().then(function (text) {
        if (text.length > 2 * 1024 * 1024) throw new Error('sandbox-api-response-too-large');
        apiReply(id, {
          ok: response.ok,
          status: response.status,
          contentType: response.headers.get('Content-Type') || '',
          body: text
        });
      });
    }).catch(function (error) {
      apiReply(id, {
        ok: false,
        status: 502,
        error: String(error && error.message || error)
      });
    }).then(function () {
      clearTimeout(timer);
      _apiInflight = Math.max(0, _apiInflight - 1);
    });
  }
  window.addEventListener('message', function (e) {
    var d = e.data || {};
    var currentFrame = frame();
    if (!currentFrame || e.source !== currentFrame.contentWindow || e.origin !== 'null') return;
    if (!BRIDGE || d.__rcwebNonce !== BRIDGE) return;
    if (d.__rcweb === 'api') { handleSandboxApi(d); return; }
    if (d.__rcweb === 'located') {
      var located = safeExternalUrl(d.url);
      if (located) onLocated(located);
      return;
    }
    if (d.__rcweb === 'nav') {
      var navigation = safeExternalUrl(d.url);
      if (navigation) go(navigation);
      return;
    }   // 兼容旧路径(地址栏输入/后退仍走 go）
    if (d.__rcweb === 'ready') { _title = String(d.title || '').slice(0, 1000); askText(); return; }
    if (d.__rcweb === 'text') {
      _pageText = String(d.text || '').slice(0, 120000);
      _title = String(d.title || _title).slice(0, 1000);
      return;
    }
    if (d.__rcweb === 'sel') {
      var innerRect = safeRect(d.rect);
      _sel = {
        text: String(d.text || '').slice(0, 20000),
        ctx: String(d.ctx || '').slice(0, 50000),
        rect: innerRect,
        clientRect: shellRect(innerRect)
      };
      // 唯一入口在 reader.src 的 SelectionController bridge 内：只有它能更新模块词法
      // lastSelText / context / preview / toolbar。这里不再伪造 window 上的同名属性。
      publishSelection();
    }
  });

  // ── ③ reader.js 就绪后补发早到的选区；WebAdapter 本身早已独立登记 ──
  document.addEventListener('DOMContentLoaded', function () {
    mountBar();
    var fr = frame();
    if (fr) fr.addEventListener('load', askText);
    setTimeout(askText, 1500);
    publishSelection();
  });

  var _lang = null;
  function langs() {
    if (_lang) return _lang;
    var t = _pageText.slice(0, 4000), out = [];
    if ((t.match(/[ぁ-んァ-ヶ]/g) || []).length > 20) out.push('ja');
    var lat = (t.match(/[A-Za-z]/g) || []).length, han = (t.match(/[一-鿿]/g) || []).length;
    if (lat > Math.max(han, 1) * 2 && lat > 200) out.push('en');
    if (t.length > 200) _lang = out;
    return out;
  }
})();
