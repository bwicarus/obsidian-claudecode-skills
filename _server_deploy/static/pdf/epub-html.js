/* epub-html.js — 统一 HTML 阅读器(第一步:EPUB)
 * 内容由服务端消毒成 HTML、渲进主文档(非 iframe)→ 控制层像 PDF 字符层一样原生直达。
 * 含:懒加载渲染 + 原生选中工具栏 + AI(查词/翻译/解释/对话/章总结) + 偏移锚高亮 + 搜索 + 设置/目录/续读/完整版。 */
(function () {
  'use strict';
  // ── 临时诊断横幅:任何未捕获 JS 错误显示在屏幕顶部红条(定位 init 报错挡住按钮/书本语言的真因)。查完即撤。──
  (function () {
    function _show(msg) {
      try {
        var b = document.getElementById('ep-err-banner');
        if (!b) {
          b = document.createElement('div'); b.id = 'ep-err-banner';
          b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;background:#c0392b;color:#fff;font:12px/1.45 monospace;padding:8px 34px 8px 10px;white-space:pre-wrap;max-height:45vh;overflow:auto;box-shadow:0 2px 10px rgba(0,0,0,.55)';
          var x = document.createElement('button'); x.textContent = '✕';
          x.style.cssText = 'position:absolute;top:3px;right:6px;background:none;border:none;color:#fff;font-size:17px;cursor:pointer';
          x.onclick = function () { b.remove(); };
          (document.body || document.documentElement).appendChild(b); b.appendChild(x);
        }
        var ln = document.createElement('div'); ln.textContent = '⚠ ' + msg; b.appendChild(ln);
      } catch (e) {}
    }
    window.addEventListener('error', function (e) { _show((e.message || 'error') + '  @' + ((e.filename || '').split('/').pop()) + ':' + e.lineno + ':' + e.colno); });
    window.addEventListener('unhandledrejection', function (e) { var r = e.reason; _show('Promise: ' + ((r && (r.message || r.stack)) || r)); });
  })();
  var CFG = window.EPUB_CFG || {};
  var $ = function (id) { return document.getElementById(id); };
  function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }

  var FREL = CFG.fileRel || '';
  // 书本语言(设置面板「书本语言」项;影响点词查典/单击选词的语种判断)。'' 自动 / 'zh' / 'en' / 'ja'
  var BOOK_LANGS = [];   // 本书「需要翻译的语言」(照搬 PDF:数组如 ['en','ja'],服务端 per-book by file;没勾的=母语免翻译)
  function bookLangsArr() { return BOOK_LANGS; }
  function loadBookLangs() {   // 照搬 PDF loadBookLangs:GET /pdf/api/book-langs(每本书独立)
    fetch('/pdf/api/book-langs?file=' + encodeURIComponent(FREL)).then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.ok) { BOOK_LANGS = d.langs || []; if (window.refreshVocabUnderlinesForAllPages) window.refreshVocabUnderlinesForAllPages(); }
    }).catch(function () {});
  }
  window.saveLangPicker = function () {   // 照搬 PDF saveLangPicker:设置面板「保存」→ POST book-langs
    var langs = Array.prototype.map.call(document.querySelectorAll('#eph-lang-checks input:checked'), function (c) { return c.value; });
    fetch('/pdf/api/book-langs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ file: FREL, langs: langs }) })
      .then(function (r) { return r.json(); }).then(function (d) {
        if (d && d.ok) BOOK_LANGS = d.langs || langs;
        toast('已保存需要翻译的语言:' + (BOOK_LANGS.join(' / ') || '无(全部免于翻译)'));
        if (window.refreshVocabUnderlinesForAllPages) window.refreshVocabUnderlinesForAllPages();
      }).catch(function (e) { toast('保存失败:' + (e.message || '网络错误')); });
  };
  var content = $('ep-content'), col = $('ep-col');
  var selBar = $('ep-sel'), dictBox = $('ep-dict');
  // 清理批次:内联助手已退役 → 助手消息容器=共享侧栏 #asst-thread(mountPdfSidebar 建);未挂载时游离 div 兜底(静默不炸)
  function _asstBody() { return document.getElementById('asst-thread') || (_asstBody._d = _asstBody._d || document.createElement('div')); }
  var COUNT = 0, TOC = [];
  var _curTopIdx = 0;
  var _jumping = false, _jumpSeq = 0;   // ⑤ 跳转进行中标志:重试循环期间路上全是矮占位,topIdx 会被算得飙高,此窗口不写续读位置
  // 初始定位守护期(2026-07-05):冷加载后浏览器(iOS Safari 尤甚)可能迟到把内层滚动容器 content 的 scrollTop 还原到
  // 上次停留处 = 首开跳末尾 927 根因(scrollRestoration='manual' 只管文档滚动,管不住内层容器)。期内无用户交互时的
  // 任何滚动都判为浏览器乱还原 → 钉回目标;用户一交互(触摸/滚轮/按键)即失效,之后正常阅读不再干预。
  // 开书「幕后定位」(2026-07-06,用户要求"直接转到记录位置"):定位到续读位置并等目标章真实加载完之前,
  // 正文 opacity:0 遮住(顶栏/抽屉照常可用)——不再看到「先落错误位置→再被拉回」的滚动舞;
  // jumpTo 重试/927 守护全在幕后进行,就位后正文直接呈现在记录位置。2.5s 硬兜底必揭幕(目标章拉不下来也不困人)。
  var _settled = false;
  function _settleReveal() { if (_settled) return; _settled = true; try { document.body.classList.remove('ep-settling'); } catch (e) {} }
  try {
    var _svl = document.createElement('style');
    _svl.textContent = '#ep-col{transition:opacity .18s}body.ep-settling #ep-col{opacity:0}';
    (document.head || document.documentElement).appendChild(_svl);
    document.body.classList.add('ep-settling');
    setTimeout(_settleReveal, 2500);
  } catch (e) { _settled = true; }
  var _initGuard = ((window.matchMedia && matchMedia('(pointer: coarse)').matches) || ('ontouchstart' in window));   // 927 守护只在触摸设备启用(iOS Safari 内层滚动还原=此 bug 源头);桌面鼠标拖滚动条不触发任何"用户交互"事件、会被 _reassertInitial 拉回(审查确认),故桌面不守护
  // 旋转(横↔竖)后残留的横向偏移归零:reflow 阅读器宽度本就随视口自适应(--colw em 上限),但旋转瞬间旧坐标的
  // 绝对定位元素/canvas 可能把滚动区短暂撑宽 → 归零 scrollLeft(配合 #ep-content overflow-x:hidden 根治左右晃动)
  try { window.addEventListener('resize', function () { setTimeout(function () { try { content.scrollLeft = 0; } catch (e) {} }, 120); }); } catch (e) {}
  ['touchstart', 'wheel', 'pointerdown', 'keydown'].forEach(function (ev) {
    try { document.addEventListener(ev, function () { _initGuard = false; }, { once: true, passive: true, capture: true }); } catch (e) {}
  });
  var loaded = {}, secEls = [];
  var cur = { text: '', ctx: '', rect: null, anchor: null };
  var _hls = {};

  // ── 插图说明徽标总开关(设置面板「📷 插图说明徽标」,默认开,localStorage,纯客户端 UI 隐藏,不拦截 AI 描述请求)
  // 供设置面板两处调用:rc-settings.js 主模态 + 本文件末尾 #ep-set 兜底面板(RC.settings 加载失败时),两边都直接调这个全局函数。
  var LS_FIG = 'eph-fig-badge';
  window.toggleFigBadge = function (on) {
    try { localStorage.setItem(LS_FIG, on ? '1' : '0'); } catch (e) {}
    document.body.classList.toggle('ep-fig-off', !on);
  };
  document.body.classList.toggle('ep-fig-off', localStorage.getItem(LS_FIG) === '0');   // 启动即按记忆应用(默认开,不加类)

  // ── 设置 ──
  // 927 跳末尾真根因第二弹(2026-07-03):浏览器自己的滚动位置恢复(Safari 重开页面会还原上次 scrollTop)——
  // 全书重开时章节全是矮占位,还原出的大 scrollTop 直接把人甩到书尾附近;跟我们存的 LS.pos 无关,所以
  // 上一轮 v2 前缀修复后仍复发。禁用浏览器恢复,位置恢复只走我们自己的 onBuilt 逻辑。
  try { if ('scrollRestoration' in history) history.scrollRestoration = 'manual'; } catch (e) {}
  var LS = { fs: 'eph-fs', th: 'eph-th', lh: 'eph-lh', mw: 'eph-mw', pos: 'eph-pos:' + FREL };
  var st = { fs: parseInt(localStorage.getItem(LS.fs) || '100', 10) || 100,
             th: localStorage.getItem(LS.th) || 'paper',
             lh: parseFloat(localStorage.getItem(LS.lh) || '1.7') || 1.7,
             mw: parseInt(localStorage.getItem(LS.mw) || '54', 10) || 54 };
  var THEMES = { paper: { pa: '#f6f3ea', ink: '#1b1b1b', lnk: '#2a5db0' }, sepia: { pa: '#e9ddc4', ink: '#5b4636', lnk: '#8a5a2b' }, night: { pa: '#15181d', ink: '#c7ccd1', lnk: '#6fa8ff' } };
  function applyStyle() {
    var t = THEMES[st.th] || THEMES.paper, r = document.documentElement.style;
    r.setProperty('--pa', t.pa); r.setProperty('--ink', t.ink); r.setProperty('--lnk', t.lnk);
    r.setProperty('--fs', (19 * st.fs / 100).toFixed(1) + 'px'); r.setProperty('--lh', st.lh); r.setProperty('--colw', st.mw + 'em');
    // 便签 v4 重定位时机②:字号/行距/栏宽变 → reflow → 内容锚字符位置变,重算便签像素位置(读 rect 会强制同步布局,便签少,廉价)
    try { if (window.RC && RC.stickynote && RC.stickynote.repositionAll) RC.stickynote.repositionAll(); } catch (e) {}
  }
  // reflow 阅读位置保持(Kindle/Apple Books 标准):改字号/行距会 reflow → 当前读的那段位移(用户报「缩放伴随章节移动」)。
  // applyStyle 只改 CSS 变量不重建 DOM → 视口内一个字符的 Range 全程有效:reflow 前记该字符视口 Y,reflow 后重测,差量补 scrollTop 把它移回原位。
  function _reflowKeepAnchor(apply) {
    var sc = content;
    if (!sc) { apply(); return; }
    var rect = sc.getBoundingClientRect();
    var px = rect.left + rect.width / 2, py = rect.top + Math.min(80, rect.height * 0.25);   // 视口靠上 1/4(避开边缘)找锚
    var rng = null, el = null, y0 = null;
    try {
      if (document.caretRangeFromPoint) rng = document.caretRangeFromPoint(px, py);
      else if (document.caretPositionFromPoint) { var p = document.caretPositionFromPoint(px, py); if (p) { rng = document.createRange(); rng.setStart(p.offsetNode, p.offset); rng.collapse(true); } }
      if (rng) { var rr = rng.getClientRects(); if (rr && rr.length) y0 = rr[0].top; }
    } catch (e) { rng = null; }
    if (y0 == null) { try { el = document.elementFromPoint(px, py); if (el) y0 = el.getBoundingClientRect().top; } catch (e) {} }   // 文字锚失败退元素锚
    apply();   // 改字号/行距 → reflow
    if (y0 == null) return;   // 没锚(空白/未挂载)→ 不动,安全
    try {
      var y1 = null;
      if (rng) { var r2 = rng.getClientRects(); if (r2 && r2.length) y1 = r2[0].top; }
      if (y1 == null && el) y1 = el.getBoundingClientRect().top;
      if (y1 != null) sc.scrollTop += (y1 - y0);   // 差量:把锚字符移回 reflow 前的视口位置
    } catch (e) {}
  }
  function refreshSet() { $('ep-fs-v').textContent = st.fs + '%'; $('ep-lh-v').textContent = st.lh.toFixed(1); if ($('ep-mw-v')) $('ep-mw-v').textContent = st.mw;
    [].forEach.call($('ep-theme').children, function (b) { b.classList.toggle('on', b.dataset.th === st.th); });
    var fb = $('ep-fig-badge-chk'); if (fb) fb.checked = (localStorage.getItem(LS_FIG) !== '0'); }

  // ── markdown + 数学 ──
  // markdown + 数学渲染:委托共享层 window.RC(rc-md.js)。统一控制层第一步——PDF/EPUB 共用同一份 md(),
  // 不再各写一套(漏 MathJax 配置/CJK 零宽空格那类 drift 从此根除)。RC 没加载到则退回内联兜底。
  function renderMd(s) {
    if (window.RC && RC.md) return RC.md(s);
    return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\n/g, '<br>');
  }
  function typeset(el) { if (window.RC && RC.typeset) { RC.typeset(el); return; } try { if (el && window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([el]); } catch (e) {} }
  function setMd(el, text) { el.innerHTML = renderMd(text); typeset(el); }

  // ── 渲染:manifest → 占位 → 懒加载 ──
  var observer;
  function initRender() {
    fetch('/pdf/api/epub-manifest?file=' + encodeURIComponent(FREL)).then(function (r) { return r.json(); }).then(function (d) {
      if (!d || !d.ok) { showErr('打开失败：' + ((d && d.error) || '')); return; }
      COUNT = d.count || 0; TOC = d.toc || [];
      // 每步独立 try/catch + 无论如何都 hideLoad:任何一步抛错都不能把加载遮罩(z-index90 盖满屏)留在原地挡住所有按钮
      try { buildToc(); } catch (e) { setTimeout(function () { throw e; }, 0); }
      try { observer = new IntersectionObserver(onIntersect, { root: content, rootMargin: '800px 0px' }); content.addEventListener('scroll', onScroll, { passive: true }); } catch (e) { setTimeout(function () { throw e; }, 0); }
      hideLoad();
      // 分批建占位(CHUNK + setTimeout 让出主线程,几百章不冻 UI);占位高度小(只增不减→文档高度永不塌缩,
      // 不会被浏览器把 scrollTop 钳到底部 = 修「自动滚到最后一页」)。照搬 PDF setupContinuousMode 思路。
      var i = 0, CHUNK = 100;
      (function build() {
        var frag = document.createDocumentFragment(), batch = [];
        for (var n = 0; n < CHUNK && i < COUNT; n++, i++) {
          var s = document.createElement('div'); s.className = 'ep-sec ph'; s.dataset.idx = i; s.textContent = '…';
          frag.appendChild(s); secEls.push(s); batch.push(s);
        }
        col.appendChild(frag);
        batch.forEach(function (s) { observer.observe(s); });
        if (i < COUNT) setTimeout(build, 0);
        else onBuilt();
      })();
    }).catch(function (e) { showErr('网络错误：' + (e && e.message)); });
  }
  var _ready = false;
  function onBuilt() {
    // 续读位置格式版本化(2026-07-03):写入端的脏值 bug(跳转占位期 topIdx 飙高被存)已修,但**修复前
    // 存下的旧脏值还在用户 localStorage 里**(实测:开书直接被丢到 927 章的空占位)。新写入带 'v2:' 前缀
    // (见 onScroll 保存处);读到旧格式纯数字 = 修复前存的 = 一律不可信回开头。真实位置滚动后即以新格式重存。
    try { content.scrollTop = 0; } catch (e) {}   // 掐掉浏览器/bfcache 可能残留的滚动恢复,位置只由下面自己的逻辑决定
    var raw = localStorage.getItem(LS.pos) || '';
    var pos = 0, lsTs = 0;
    if (raw.indexOf('v3:') === 0) { var _p3 = raw.slice(3).split(':'); pos = parseInt(_p3[0], 10) || 0; lsTs = parseInt(_p3[1], 10) || 0; }
    else if (raw.indexOf('v2:') === 0) pos = parseInt(raw.slice(3), 10) || 0;   // 旧格式无 ts → 当最旧(服务端有记录就让位)
    pos = Math.min(Math.max(0, pos), COUNT - 1);
    if (pos >= COUNT - 1) pos = 0;   // 正好末章仍当脏值,回开头(双保险)——只适用 LS 旧值,服务端值不走此守卫
    // 服务端续读记录优先于 LS(2026-07-03,跨设备真源:iPad 读到哪,PC 打开就在哪;由 /pdf/epub/view 注入
    // EPUB_CFG.serverPos,零异步等待)。写入端(onScroll 保存点)已过 _jumping+loaded 校验才上报 → 可信,
    // 不做「末章当脏值」回退;服务端没记录(null→NaN)→ 用上面的 LS v2 当离线兜底。
    var _sp = parseInt(CFG.serverPos, 10);
    var _spTs = (parseInt(CFG.serverPosTs, 10) || 0) * 1000;   // 服务端 epoch秒 → ms
    if (!isNaN(_sp) && _sp >= 0 && (_spTs >= lsTs || pos <= 0)) {
      pos = Math.min(_sp, COUNT - 1);   // 时间戳仲裁:服务端新(或本地无有效记录)→ 服务端胜(同 PDF 模型;修 BUG#5 下半:旧 serverPos 无条件压过更新的 LS)
    } else if (pos > 0 && !isNaN(_sp) && _sp >= 0) {
      // 本地更新鲜(上报失败过/离线读过)→ 用本地,并治愈服务端记录(别的设备才拿得到新进度)
      try { fetch('/pdf/api/reading-pos', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ file: FREL, kind: 'epub', pos: pos }), keepalive: true }).catch(function () {}); } catch (e) {}
    }
    // ?sec=N 深链(收藏夹查看页「打开原书↗」等):本次初始定位优先用它;只影响这次打开,LS.pos 记忆照常
    var _m = location.search.match(/[?&]sec=(\d+)/);
    if (_m) pos = Math.min(Math.max(0, parseInt(_m[1], 10) || 0), COUNT - 1);   // 深链不做"末章当脏值"回退(用户就是要去那)
    if (pos > 0) jumpTo(pos, false);
    // 初始定位守护(2026-07-05,修「首开跳末尾 927,刷新才对」):冷加载后浏览器可能迟到还原内层滚动到末尾,盖掉 jumpTo(pos)。
    // 在若干时刻重钉回目标 pos(含 pos=0 顶部);_reassertInitial 内 _initGuard 保证用户一交互即停,不误伤正常浏览。
    [150, 350, 650, 1000, 1500, 2200, 3200].forEach(function (ms) { setTimeout(function () { _reassertInitial(pos); }, ms); });
    setTimeout(function () { _initGuard = false; }, 3600);   // 守护期硬上限(即便用户一直不交互也放行)
    // 幕后定位完成(目标章真实加载)→ 最后钉一次 → 揭幕:正文直接呈现在记录位置(不透出中间的错误位置)
    (function _waitSettle(n) {
      if (_settled) return;
      if (loaded[pos] === true) {
        try { var el2 = secEls[pos]; if (el2) { var cr2 = content.getBoundingClientRect(), r2 = el2.getBoundingClientRect(); var dy2 = r2.top - cr2.top; if (Math.abs(dy2) > 4) content.scrollTop += dy2; } } catch (e) {}
        requestAnimationFrame(function () { requestAnimationFrame(_settleReveal); });
        // 揭幕后温和补钉(审计 BUG#7:上方预载章图片解码/idle 装饰迟到改高 → 桌面无 927 守护会再跳一下);用户一交互即停
        var pin = true;
        ['touchstart', 'wheel', 'pointerdown', 'keydown'].forEach(function (ev) { try { document.addEventListener(ev, function () { pin = false; }, { once: true, passive: true, capture: true }); } catch (e) {} });
        [300, 800, 1600].forEach(function (ms) { setTimeout(function () {
          if (!pin) return;
          try { var e3 = secEls[pos]; if (e3) { var c3 = content.getBoundingClientRect(), r3 = e3.getBoundingClientRect(), d3 = r3.top - c3.top; if (Math.abs(d3) > 4) content.scrollTop += d3; } } catch (e) {}
        }, ms); });
        return;
      }
      if (n >= 22) { _settleReveal(); return; }
      setTimeout(function () { _waitSettle(n + 1); }, 100);
    })(0);
    // 用户页(插入页):占位全建好(secEls 齐)后再加载插入——.ep-usec 是 .ep-sec 的兄弟节点,不进 secEls,
    // 原书章序/锚零影响;jumpTo 的重试循环会持续钉住目标章,插入引起的高度变化被自动吸收。
    try { if (window.RC && RC.userpages) RC.userpages.load(); } catch (e) {}
    setTimeout(function () { _ready = true; }, 1200);   // 初始抖动稳定后才允许保存位置(防写坏)
  }
  function onIntersect(entries) {
    entries.forEach(function (en) { if (en.isIntersecting) loadSection(parseInt(en.target.dataset.idx, 10)); });
  }
  var _secInflight = 0, _secWait = [], SEC_MAX = 4, _secGen = 0;   // 章节请求并发上限 + 章节代号(_secGen:收藏夹 reconcile 重排=换代,旧代在途响应作废;审计 BUG#1)
  function _secDone() { if (_secInflight > 0) _secInflight--; if (_secWait.length && _secInflight < SEC_MAX) _fetchSection(_secWait.shift()); }
  function loadSection(idx) {
    if (loaded[idx]) return; loaded[idx] = 'loading';
    if (_secInflight >= SEC_MAX) { _secWait.push(idx); return; }
    _fetchSection(idx);
  }
  function _fetchSection(idx) {
    _secInflight++;
    var gen = _secGen;   // 捕获代号:重排后 idx→元素映射失效,旧响应写入会错位内容+误标 loaded 永久卡占位(BUG#1)
    var el = secEls[idx]; if (!el) { _secDone(); return; }
    fetch('/pdf/api/epub-section?file=' + encodeURIComponent(FREL) + '&idx=' + idx).then(function (r) { return r.json(); }).then(function (d) {
      _secDone();   // 响应到手、连接释放 → 立刻放行下一个章节请求(渲染在本帧继续,不占连接)
      if (gen !== _secGen) return;   // 已换代:丢弃(新代按新 idx 重拉;loaded 已被 reconcile 重建,不动)
      if (!d || !d.ok) { el.textContent = ''; el.classList.remove('ph'); loaded[idx] = true; return; }
      // 上方加载 → 记录高度差,加载后补 scrollTop 防跳动
      var aboveTop = el.getBoundingClientRect().bottom < 0;
      var h0 = el.offsetHeight;
      el.classList.remove('ph'); el.style.minHeight = ''; el.innerHTML = d.html;
      loaded[idx] = true;
      if (aboveTop) { var dh = el.offsetHeight - h0; if (dh) content.scrollTop += dh; }
      // 收藏夹物化 EPUB 的「我的页」条目含 $..$ 公式:普通书 section 阅读器不 typeset(书用 MathML/图),
      //   这里**仅对收藏夹书(FREL 前缀)且仅 .fav-item-userpage 元素** typeset(PDF 透明词层/EPUB 条目不碰 → 零普通书回归)。
      try {
        if (FREL.indexOf('资源/收藏夹/') === 0 && window.MathJax && MathJax.typesetPromise) {
          var _ups = el.querySelectorAll('.fav-item-userpage');
          if (_ups.length) MathJax.typesetPromise([].slice.call(_ups)).catch(function () {});
        }
      } catch (e) {}
      // 高亮/图徽标/装饰整体 try/catch:任一抛错都不能让本章退回未加载(内容已 innerHTML 上屏);错误异步抛给横幅诊断
      try {
        var imgs = el.querySelectorAll('img');
        if (imgs.length && idx < 60) setTimeout(function () { var ok = 0; imgs.forEach(function (im) { if (im.naturalWidth > 0) ok++; }); var src0 = imgs[0] ? (imgs[0].getAttribute('src') || '') : ''; dbg('sec' + idx + ' imgs=' + imgs.length + ' loaded=' + ok + ' src0=' + src0.slice(-46)); }, 1800);
        // 所有装饰(高亮/图徽标/生词/振假名)全推迟到空闲帧 → loadSection 同步部分只剩 innerHTML,不阻塞侧边栏点击/滚动
        // (根因:多章同时加载时,applyHl + decorateFigures 同步跑占满主线程,侧边栏按钮的 tap 排不进事件循环)
        (window.requestIdleCallback || function (f) { return setTimeout(f, 0); })(function () {
          try {
            Object.keys(_hls).forEach(function (id) { var h = _hls[id]; if (h.anchor && h.anchor.section === idx) applyHl(el, h); });
            decorateFigures(el);   // 给大图挂 🔎 徽标
            _tokenizeSection(el);   // 分词缓存(单击查词用),不受振假名/生词开关影响,始终跑
            try { _favBindSection(el); } catch (e) {}   // 收藏夹 PDF 条目:char-layer 式自定义选择(仅 fav 书生效)
            if (_vocabOn() || _deco.ruby || _deco.pagetr) _decorateSection(el);
            try { _inkOnSectionLoaded(el, idx); } catch (e) {}   // 本章有存档墨迹 / 正处手写模式 → 贴 canvas + 重绘
            try { if (window.RC && RC.stickynote) RC.stickynote.mountPending(); } catch (e) {}   // 本章便签补挂(幂等)
          } catch (e) {}
        }, { timeout: 1000 });
      } catch (e) { setTimeout(function () { throw e; }, 0); }
    }).catch(function () { if (gen === _secGen) loaded[idx] = false; _secDone(); });
  }

  // ── 进度 + 续读位置 ──
  var _saveT;
  // ③ 当前节 = 「覆盖画面主体中线(可见正文区竖向中点)的节」,不再是「顶部第一个 bottom>60 的节」。
  // 用户反馈:上一章只剩一条尾巴挂在顶部时,画面主体+传给 AI 的可见内容其实几乎都是下一章了,却还算上一章。
  // 改用参考线 ref=可见正文区中点:谁覆盖它谁就是当前节 → 页码/current_section_idx 跟画面主体一致。
  // 邻域双向搜(bottom 沿文档序单调不减)仍是 O(1);只把阈值从 60(顶部)挪到可见区中点。
  function _findTopIdx() {
    var n = secEls.length; if (!n) return 0;
    var ref = 60;
    try { var cr = content.getBoundingClientRect(); ref = (Math.max(cr.top, 0) + Math.min(cr.bottom, window.innerHeight || cr.bottom)) / 2; } catch (e) {}
    var i = Math.min(Math.max(_curTopIdx | 0, 0), n - 1);
    var below = function (k) { return secEls[k].getBoundingClientRect().bottom > ref; };
    if (below(i)) { while (i > 0 && below(i - 1)) i--; return i; }
    while (i < n - 1) { i++; if (below(i)) return i; }
    return n - 1;
  }
  function onScroll() {
    var topIdx = _findTopIdx();
    var pct = COUNT > 1 ? Math.round(topIdx / (COUNT - 1) * 100) : 0;
    _curTopIdx = topIdx;
    $('ep-page-cur').textContent = (topIdx + 1); $('ep-page-total').textContent = '/ ' + (COUNT || '–'); $('ep-bar').style.width = pct + '%';
    try { _inkDecoScheduleVisible(); } catch (e) {}   // 滚到带墨迹的章节 → 空闲帧补贴墨迹(不阻塞滚动)
    if (!_ready || _jumping || _initGuard) return;   // ⑤ 初始加载抖动期 / 跳转进行中 / 初始定位守护期:topIdx 不可信,不存位置(防把脏位置或浏览器还原的 927 写进 localStorage/服务端)
    clearTimeout(_saveT); _saveT = setTimeout(function () {
      // ⑤ 400ms 后再校一次:期间开始了新跳转、或顶部节还是未加载占位(占位高度≠真实高度,topIdx 不可信)→ 不存
      if (_jumping || loaded[topIdx] !== true) return;
      localStorage.setItem(LS.pos, 'v3:' + topIdx + ':' + Date.now());   // v3=idx:ts(ms)。ts 供 onBuilt 与服务端记录按新者胜仲裁(同 PDF 模型);v2 旧格式仍可读(ts 当 0)
      _reportPos(topIdx);   // 服务端同记一份(≥5s 节流;跨设备续读真源,LS 只是本机离线兜底)
    }, 400);
  }
  // ── 服务端续读位置上报(2026-07-03):POST /pdf/api/reading-pos → state/reader-positions.json。
  // 只从上面 onScroll 的已校验保存点进来(过了 _jumping+loaded 校验的 topIdx 才可信)→ ≥5s 节流 trailing;
  // 切后台/关页 sendBeacon 兜底立即送最后位置(卸载中 fetch 会被浏览器砍)。sent 初值=开局注入的 serverPos:
  // 打开后不动就不上报,不会用旧值盖掉别的设备刚写的新进度。
  var _srvPos = { val: -1, sent: (function () { var v = parseInt(CFG.serverPos, 10); return isNaN(v) ? -1 : v; })(), t: 0, timer: null };
  var _ogLastIdx = null;
  // 视口中心落在本节的相对位置(0~1)。服务端据此以视口为中心取 ±N 段,不再整章灌入
  // (任务书一)。用 ratio 而不是段序号:服务端的"段"是按文本行切的,前端是 DOM 块级
  // 元素,两边序号对不齐;比例不依赖切分方式,错一点也只是上下文窗口偏移一点。
  // 拿不到可信值就返回 null —— 服务端会退回整章并注明,好过用错位的视口截出错误段落。
  function _viewportRatio(idx) {
    try {
      if (loaded[idx] !== true) return null;   // 未加载的节是矮占位,高度不可信
      var el = secEls[idx];
      if (!el || !content) return null;
      var cr = content.getBoundingClientRect(), r = el.getBoundingClientRect();
      if (!(r.height > 0)) return null;
      var v = ((cr.top + cr.height / 2) - r.top) / r.height;
      if (!isFinite(v)) return null;
      return Math.max(0, Math.min(1, Math.round(v * 1000) / 1000));
    } catch (e) { return null; }
  }

  function _reportPos(idx) {
    _srvPos.val = idx;
    // 换节 → 丢弃绘图焦点(上一节的绘图区不再是当前)
    try { if (idx !== _ogLastIdx) { _ogLastIdx = idx;
          window.RC && RC.outgoing && RC.outgoing.dropDrawingFocus(); } } catch (e) {}
    // 双向上下文同步:复用这个已有漏斗,不新增监听(关时 report 立即返回,零网络)
    try {
      var _vr = _viewportRatio(idx);
      window.RC && RC.ctxSync && RC.ctxSync.report({
        kind: 'epub', file: FREL, pos: idx,
        title: document.title.replace(/ ·.*$/, ''),
        // 同一节内滚动时 pos 不变、只有 viewport 变:共享层会更新 pend 但不排定发送,
        // 等 dwell 那一发带上最新视口(见 rc-core._ctxViewportEq 分支)。
        viewport: (_vr === null ? null : { ratio: _vr })
      });
    } catch (e) {}
    if (_srvPos.timer) return;   // 已排队:trailing 自带最新值
    _srvPos.timer = setTimeout(_posFlush, Math.max(0, 5000 - (Date.now() - _srvPos.t)));
  }
  function _posFlush() {
    _srvPos.timer = null;
    if (_srvPos.val < 0 || _srvPos.val === _srvPos.sent) return;
    _srvPos.t = Date.now();
    var v = _srvPos.val;   // POST 成功才记 sent(审计 BUG#5:乐观置 sent 后一次瞬断 → 同位置永不重试,beacon 也被 val===sent 拦)
    try {
      fetch('/pdf/api/reading-pos', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file: FREL, kind: 'epub', pos: v }), keepalive: true })
        .then(function (r) { if (r && r.ok) _srvPos.sent = v; }).catch(function () {});
    } catch (e) {}
  }
  function _posBeacon() {
    if (_srvPos.timer) { clearTimeout(_srvPos.timer); _srvPos.timer = null; }
    if (_srvPos.val < 0 || _srvPos.val === _srvPos.sent) return;
    try {
      var b = new Blob([JSON.stringify({ file: FREL, kind: 'epub', pos: _srvPos.val })], { type: 'application/json' });
      if (navigator.sendBeacon && navigator.sendBeacon('/pdf/api/reading-pos', b)) { _srvPos.sent = _srvPos.val; return; }
    } catch (e) {}
    _posFlush();   // 无 sendBeacon(老浏览器)→ 退回 keepalive fetch
  }
  window.addEventListener('pagehide', _posBeacon);
  document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'hidden') _posBeacon(); });
  // ── 抽屉开/关的阅读位置保持(⑤):开抽屉用 padding-right 挤压 #ep-content → 正文 reflow,全文高度
  // 重排,同一 scrollTop 对应的内容位置变了 = 视觉「跳页」(关抽屉恢复原宽又跳回来)。挤压**前**记
  // 「顶部可见节 + 该节内已滚过的比例」,重排完成后滚回等效位置。挂在 rc-sidedrawer 的 onLayoutChange
  // 钩子上(类名切换前取锚、返回 restore 由它在下一帧 + 过渡结束后调;时序在共享层,锚点机制在这)。
  function _keepReadPos() {
    var top = _curTopIdx, el = secEls[top];
    if (!el) return null;
    var cr = content.getBoundingClientRect(), r = el.getBoundingClientRect();
    var frac = r.height > 0 ? (cr.top - r.top) / r.height : 0;   // 该节顶已滚过视口顶的比例(负=节顶还在视口内)
    return function restore() {
      var el2 = secEls[top]; if (!el2) return;
      var cr2 = content.getBoundingClientRect(), r2 = el2.getBoundingClientRect();
      // 目标:让该节顶重新位于视口顶上方 frac*新节高 处 → 还需滚 (节顶当前相对视口顶的距离) + frac*新节高
      content.scrollTop += (r2.top - cr2.top) + frac * r2.height;
    };
  }
  // ② 所有跳转统一瞬时:第二参数保留签名但不再走平滑——scrollIntoView({behavior:'auto'}) 在 iOS 上可能被
  // 当平滑处理,叠加 70ms 重试循环 = 漫长滚动动画,且平滑路过的每一章都被 IntersectionObserver 触发
  // fetch+innerHTML(掉帧根因③);目标章未加载时占位高度不准,滚过去后加载膨胀又漂 = 要点第二次。
  // 改手动算 scrollTop(等价 block:'start',强制瞬时无歧义),重试循环保留(钉住目标直到该章加载完)。
  function jumpTo(idx, _smoothIgnored) {
    var el = secEls[idx]; if (!el) return;
    loadSection(idx);
    var tok = ++_jumpSeq;   // ⑤ 新跳转作废旧重试循环;进行中不写续读位置(见 onScroll)
    _jumping = true;
    var tries = 0;
    (function go() {
      if (tok !== _jumpSeq) return;   // 已被更新的跳转接管
      try {
        var cr = content.getBoundingClientRect(), r = el.getBoundingClientRect();
        content.scrollTop += (r.top - cr.top);
      } catch (e) {}
      if (loaded[idx] !== true && tries++ < 25) { setTimeout(go, 70); return; }
      _jumping = false;
      try { onScroll(); } catch (e2) {}   // 跳完刷新进度条/topIdx 并(经 loaded 校验后)恢复位置保存
    })();
  }
  // 初始定位守护:见 _initGuard。dy 明显(>4px)才动 → 已在目标即 no-op;用户一交互即整体失效,不误伤正常浏览。
  function _reassertInitial(pos) {
    if (!_initGuard) return;
    var el = secEls[pos]; if (!el) return;
    try { var cr = content.getBoundingClientRect(), r = el.getBoundingClientRect(); var dy = r.top - cr.top; if (Math.abs(dy) > 4) content.scrollTop += dy; } catch (e) {}
  }

  // ── 目录 ──
  function buildToc() {
    var box = $('ep-toc-list'); box.innerHTML = '';
    TOC.forEach(function (t) {
      var a = document.createElement('div'); a.className = 'ep-toc-i'; a.textContent = t.label;
      // ⑥ 跳转不再自动收抽屉(宽屏正文被挤在左侧仍可见,可连续点目录);仅极窄屏(抽屉≥90vw 盖满)才收,
      // 判定统一走 RC.sidedrawer.afterJump(本文件所有跳转点共用 _drawerAfterJump)。
      a.onclick = function () { jumpTo(t.idx, false); _drawerAfterJump(); };
      box.appendChild(a);
    });
    if (!TOC.length) box.innerHTML = '<div class="ep-empty">无目录</div>';
  }
  // 目录已并入统一抽屉「目录」tab(由 buildToc 填 #ep-toc-list);开关走 RC.sidedrawer

  // ── 原生选区 → 工具栏(内容在主文档,直接 getSelection)──
  function _ctxSelReport(txt) {
    // 选区即时同步:建立/改动/清空都立刻推(空串=显式无选区,不是省略字段)
    try {
      window.RC && RC.ctxSync && RC.ctxSync.report(
        {
          kind: 'epub',
          file: FREL,
          selection: txt || '',
          sel_page: _curTopIdx
        },
        { immediate: true });
      if (window.RC && RC.outgoing) {
        if (txt) RC.outgoing.focus('text', { file: FREL, text: String(txt).slice(0, 200) });
        else RC.outgoing.cancel();
      }
    } catch (e) {}
  }
  function captureSel(opts) {
    try {
      // 精确 Range(词/行/段三级点击已经算好边界)传 {snapWords:false} 跳过对齐,只有原生自由拖选才需要
      var doSnap = !opts || opts.snapWords !== false;
      var s = window.getSelection();
      if (!s || !s.rangeCount || s.isCollapsed) { hideSel(); _ctxSelReport(''); return; }
      var txt = (s.toString() || '').trim();
      _ctxSelReport(txt);
      if (!txt) { hideSel(); return; }
      var rng = s.getRangeAt(0);
      if (!col.contains(rng.commonAncestorContainer)) { dbg('cap: 选区不在正文 col 内'); hideSel(); return; }
      var secInfo = secOf(rng.startContainer);
      if (!secInfo) {   // 插入页(.ep-usec,不进 secEls):偏移空间=用户页正文 .rc-up-body,section=u_* 字符串 id → 高亮/锚类功能全套可用
        var _e0 = rng.startContainer.nodeType === 3 ? rng.startContainer.parentElement : rng.startContainer;
        var _ub = _e0 && _e0.closest ? _e0.closest('.ep-usec .rc-up-body') : null;
        var _ue = _e0 && _e0.closest ? _e0.closest('.ep-usec') : null;
        if (_ub && _ue && _ue.dataset.uid) secInfo = { el: _ub, idx: _ue.dataset.uid };
      }
      var anchor = null;
      if (secInfo) {
        var start = offsetOf(secInfo.el, rng.startContainer, rng.startOffset);
        var end = offsetOf(secInfo.el, rng.endContainer, rng.endOffset);
        if (end < start) { var tmp = start; start = end; end = tmp; }
        var full = _countableText(secInfo.el);
        if (doSnap) {
          var snapped = _snapWordBoundary(full, start, end, secInfo.idx);
          if (snapped.start !== start || snapped.end !== end) {
            var newRng = _rangeFromOffsets(secInfo.el, snapped.start, snapped.end);
            if (newRng) { try { s.removeAllRanges(); s.addRange(newRng); rng = newRng; } catch (e) {} }
          }
          start = snapped.start; end = snapped.end;
        }
        anchor = { section: secInfo.idx, start: start, end: end };
        var clean = full.slice(start, end).trim();   // 剔除装饰开着时夹带的 rt 读音/译文
        if (clean) txt = clean;
      }
      var blk = rng.startContainer.nodeType === 3 ? rng.startContainer.parentElement : rng.startContainer;
      blk = blk && blk.closest ? blk.closest('p,li,td,blockquote,h1,h2,h3,h4,div') : null;
      cur = { text: txt, ctx: _countableText(blk).trim().slice(0, 1200), anchor: anchor, rect: rng.getBoundingClientRect(), _selT: Date.now() };
      dbg('cap text="' + txt.slice(0, 8) + '" anchor=' + JSON.stringify(anchor));
      if (_phraseSnap) _unwrapPhrase();   // 重新选词 → 清掉上一处词组呼吸高亮(照搬 PDF onStart 语义)
      // 扩展接管后仍保留 cur/anchor 供 book-host 读取，但不要再弹出 PWA 自己的选区工具栏。
      // 这样同一份书籍锚点逻辑只运行一次，界面和网络动作统一交给扩展。
      if (document.documentElement.dataset.bwReaderExtensionActive === '1') {
        hideSel();
        return;
      }
      showSel();
      try { window.__setFocusSel && window.__setFocusSel(txt, /\$/.test(txt) ? 'formula' : 'text'); } catch (e) {}   // 助手开着时钉焦点(照搬 PDF 13-selection.js:586/599/656)→ 输入框上方可视 chip,可 ✕ 取消
    } catch (e) { dbg('cap ERR: ' + (e && e.message)); }
  }
  function secOf(node) {
    var el = node.nodeType === 3 ? node.parentElement : node;
    while (el && el !== col) { if (el.classList && el.classList.contains('ep-sec')) return { el: el, idx: parseInt(el.dataset.idx, 10) }; el = el.parentElement; }
    return null;
  }
  // ── G0:装饰节点判定。只把「我们注入的额外可见文本」排除出偏移空间:振假名读音 <rt data-eph=1> 与译文块 .ep-tr-rt。
  // 关键:**不排除书本身原生 <rt>**——G0 前 offsetOf 把原生 rt 也计入,既有高亮偏移就含原生读音;若现在排除会让原生振假名页旧高亮漂移。故只认 data-eph 标记。
  // <mark>(高亮)/<ruby>基字/<span.ep-vocab-und>(生词)只包不改字 → 照常计入(可见字符数不变)。
  function _countable(textNode) {
    var el = textNode.parentElement;
    while (el && el !== col) {
      if (el.tagName === 'RT' && el.dataset && el.dataset.eph === '1') return false;
      if (el.classList && el.classList.contains('ep-tr-rt')) return false;
      // 便签(rc-stickynote)挂进 .ep-sec 内部,自带文字(textarea 值/工具按钮 emoji)——不是书的内容,
      // 排除出偏移空间(便签追加在节尾,不影响既有高亮偏移;v4 内容锚取锚时也绝不能锚到便签自己身上)
      if (el.classList && el.classList.contains('rc-note')) return false;
      el = el.parentElement;
    }
    return true;
  }
  function _countableText(el) {
    if (!el) return '';
    var w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null), n, out = '';
    while ((n = w.nextNode())) { if (_countable(n)) out += n.nodeValue; }
    return out;
  }
  function offsetOf(secEl, node, off) {
    var w = document.createTreeWalker(secEl, NodeFilter.SHOW_TEXT, null), n, total = 0;
    while ((n = w.nextNode())) {
      if (n === node) return total + (_countable(n) ? off : 0);
      if (!_countable(n)) continue;
      total += n.nodeValue.length;
    }
    return total;
  }
  // offsetOf 的反向:section 内「可数」字符偏移 → {node,offset}(供拖选边界扩张后重建 Range)
  function _domPosAtOffset(secEl, offset) {
    var w = document.createTreeWalker(secEl, NodeFilter.SHOW_TEXT, null), n, total = 0, last = null;
    while ((n = w.nextNode())) {
      if (!_countable(n)) continue;
      var len = n.nodeValue.length;
      last = n;
      if (offset <= total + len) return { node: n, offset: offset - total };
      total += len;
    }
    return last ? { node: last, offset: last.nodeValue.length } : null;
  }
  function _rangeFromOffsets(secEl, start, end) {
    var a = _domPosAtOffset(secEl, start), b = _domPosAtOffset(secEl, end);
    if (!a || !b) return null;
    var r = document.createRange();
    try { r.setStart(a.node, a.offset); r.setEnd(b.node, b.offset); } catch (e) { return null; }
    return r;
  }
  // ── G1:拖选自动对齐词边界。假名/汉字优先用 _tokSnap 查后端 fugashi 真分词缓存(见上 _tokenizeSection);
  // 缓存未就绪(刚翻到本章、请求还没回来)时退化成兜底:扩到脚本类别切换/标点/空白为止——连续汉字算一类、
  // 连续假名(平+片)算另一类。英文/数字按 \w 扩(同 wordAt 的拉丁字符集,不接分词,fugashi 只管日语)。
  // 韩文没有分词器接入,维持兜底逻辑。WORD_SNAP_CAP 兜底:防止畸形无标点文本导致单次扩张过大拖累交互。
  function _wordClass(ch) {
    if (ch == null) return null;
    if (/[A-Za-z0-9_'’\-]/.test(ch)) return 'latin';
    if (/[぀-ヿ]/.test(ch)) return 'kana';
    if (/[一-鿿㐀-䶿]/.test(ch)) return 'kanji';
    if (/[가-힯]/.test(ch)) return 'hangul';
    return null;
  }
  var WORD_SNAP_CAP = 400;
  function _snapWordBoundary(text, start, end, secIdx) {
    if (start > end) { var t0 = start; start = end; end = t0; }
    if (start >= end || start < 0 || end > text.length) return { start: start, end: end };
    // 分词缓存就绪时,假名/汉字边界优先用真分词(_tokSnap)扩;缓存未就绪或非日语字符 → 退回原字符类兜底
    // (secIdx 省略时行为与旧版逐字节相同,不影响未接缓存的调用方)
    var tok = (secIdx != null) ? _tokSnap(secIdx, start, end) : null;
    var sCls = _wordClass(text[start]);
    var didStart = false;
    if (tok && (sCls === 'kana' || sCls === 'kanji') && tok.start !== start) { start = tok.start; didStart = true; }
    if (!didStart && sCls && sCls !== 'kanji') { var lim = Math.max(0, start - WORD_SNAP_CAP); while (start > lim && _wordClass(text[start - 1]) === sCls) start--; }   // kanji(中/日汉字)只信真分词 tokSnap;无分词就不字符类扩张——否则会把连续汉字当一个词、跨块吞进上方标题(_countableText 块间无分隔符)
    var eCls = _wordClass(text[end - 1]);
    var didEnd = false;
    if (tok && (eCls === 'kana' || eCls === 'kanji') && tok.end !== end) { end = tok.end; didEnd = true; }
    if (!didEnd && eCls && eCls !== 'kanji') { var lim2 = Math.min(text.length, end + WORD_SNAP_CAP); while (end < lim2 && _wordClass(text[end]) === eCls) end++; }   // 同上:kanji 尾部不字符类扩张(防跨块/连续汉字过扩)
    return { start: start, end: end };
  }
  // 选中触发三重兜底:selectionchange + mouseup/touchend + 轮询(iOS 主文档 selectionchange 有时不稳)
  document.addEventListener('selectionchange', function () { clearTimeout(captureSel._t); captureSel._t = setTimeout(captureSel, 160); });
  content.addEventListener('mouseup', function () { setTimeout(captureSel, 10); }, true);
  content.addEventListener('touchend', function () { setTimeout(captureSel, 10); }, true);
  var _polLast = '';
  setInterval(function () { try { var s = window.getSelection(); var t = s && !s.isCollapsed ? (s.toString() || '').trim() : ''; if (t && t !== _polLast) { _polLast = t; captureSel(); } else if (!t) { _polLast = ''; } } catch (e) {} }, 450);
  function showSel() {
    // 按词/多词分流(照搬 PDF 14-textlayer-legacy):单个英/日词 → 查词组;多词/中文 → 翻译/解释/对话组;复制/高亮/笔记/制卡 两者都有
    var word = RC.ui.isDictionaryWord(cur.text);
    // F6 短词组(照搬 PDF):非单词 + 命中短词组阈值 → 露「📘词组」按钮(呼吸提示)
    var phrase = !word && _isShortPhrase(cur.text);
    selBar.querySelectorAll('[data-grp]').forEach(function (b) {
      var g = b.dataset.grp;
      var show = (g === 'both') || (word ? g === 'word' : g === 'multi') || (g === 'phrase' && phrase);
      // 「📊 语法」对齐 PDF 原生 _updateGrammarBtnVisibility(18-grammar.js):有跟踪中的语法 KG 节点才显示
      // (原生 = _grammarHasTracked && lastSelText;这里选中已成立,只差 hasTracked。开书时已 loadTracked 预载)
      if (show && b.dataset.act === 'grammar') show = !!(window.RC && RC.grammar && RC.grammar.hasTracked());
      b.style.display = show ? '' : 'none';
    });
    var pb = $('ep-phrase-btn'); if (pb) pb.classList.toggle('breathe', phrase);
    // preview:已选文本 + 字数(照搬 PDF .preview;>120 字按「前60…后40」截断)
    var pv = $('ep-preview');
    if (pv) { var t = cur.text || ''; var disp = t.length > 120 ? (t.slice(0, 60) + '…' + t.slice(-40)) : t; pv.innerHTML = esc(disp) + '<span class="len">' + t.length + ' 字</span>'; }
    // 色板激活态:标出上次用的色(照搬 PDF onPickColor 的 .active)
    var last = localStorage.getItem('eph-hl-color') || hlColors()[0];
    selBar.querySelectorAll('#ep-hl-pick .swatch').forEach(function (i) { i.classList.toggle('active', i.dataset.c === last); });
    selBar.classList.add('open');
    // 2A：与 PDF / HTML / 扩展共用紧贴选区的横向浮条；本宿主只提供 EPUB offset 算出的 client rect。
    if (window.RC && RC.ui && RC.ui.placeSelectionToolbar) RC.ui.placeSelectionToolbar(selBar, cur.rect, { gap: 8 });
  }
  function hideSel() { selBar.classList.remove('open'); try { if (typeof _cselClear === 'function') _cselClear(); } catch (e) {} }

  // ════════ F6 词组(逐字照搬 PDF reader.src/14-textlayer-legacy.js::_isShortPhrase + 15-phrase-wordpop.js::
  //   onPhrase/showPhrasePopover/_phraseFav。reflow 适配:绝对叠层 .phrase-hl-layer → 原生 <mark class="ep-phrase-hl">,
  //   词组浮层走共享层 RC.phrasepop)════════
  // 短词组判定:中日 2-8 字(无句末标点) / 拉丁 2-5 词且不太长(无句末标点)。逐字搬自 PDF _isShortPhrase。
  function _isShortPhrase(text) {
    var t = (text || '').trim();
    if (!t) return false;
    if (/[。！？、，.!?]$/.test(t)) return false;
    if (/[぀-ヿ㐀-鿿]/.test(t)) {
      return t.length >= 2 && t.length <= 8 && !/[。！？、，.!?]/.test(t);
    }
    var words = t.split(/\s+/).filter(Boolean);
    return words.length >= 2 && words.length <= 5 && t.length <= 40;
  }
  // ── 词组临时呼吸高亮(reflow:PDF 的 _showPhraseHighlight/renderPhraseHl/_removePhraseHighlight 用绝对叠层,
  //   这里用原生 <mark>。G0:只包不增删可见字符 → 偏移不漂。查询中 .breathe 呼吸,出结果转常亮,点它消失重弹)──
  var _phraseSnap = null;   // 当前词组高亮的选区快照 {section,start,end,text}
  function _wrapPhraseRange(section, start, end, breathe) {
    _unwrapPhrase();
    var secEl = secEls[section]; if (!secEl || start >= end) return false;
    var w = document.createTreeWalker(secEl, NodeFilter.SHOW_TEXT, null), n, pos = 0, hits = [];   // 偏移空间同 applyHl/offsetOf(只数 _countable)
    while ((n = w.nextNode())) {
      if (!_countable(n)) continue;
      var len = n.nodeValue.length;
      if (n.parentElement && n.parentElement.tagName === 'MARK') { pos += len; continue; }   // 已是高亮内文本 → 计偏移不重复包(同 applyHl)
      var ns = pos, ne = pos + len;
      if (ne > start && ns < end) hits.push({ node: n, s: Math.max(0, start - ns), e: Math.min(len, end - ns) });
      pos = ne; if (pos >= end) break;
    }
    if (!hits.length) return false;
    hits.reverse().forEach(function (r) {
      try {
        var rest = r.node.splitText(r.s); rest.splitText(r.e - r.s);
        var mk = document.createElement('mark'); mk.className = 'ep-phrase-hl' + (breathe ? ' breathe' : '');
        rest.parentNode.insertBefore(mk, rest); mk.appendChild(rest);
      } catch (e) {}
    });
    _phraseSnap = { section: section, start: start, end: end, text: (secEls[section] ? _countableText(secEls[section]).slice(start, end) : '') };
    return true;
  }
  function _unwrapPhrase() {
    col.querySelectorAll('mark.ep-phrase-hl').forEach(function (mk) { var p = mk.parentNode; while (mk.firstChild) p.insertBefore(mk.firstChild, mk); p.removeChild(mk); p.normalize(); });
    _phraseSnap = null;
  }
  function _phraseSetSolid() { col.querySelectorAll('mark.ep-phrase-hl').forEach(function (mk) { mk.classList.remove('breathe'); }); }   // 出结果转常亮保持
  // 单词查询中呼吸高亮(供 RC.wordpop 经 opts.breathe host-bind 调用)。跟词组呼吸高亮(上面 _wrapPhraseRange)
  // 同一套技术:真实 <mark> 包在文字节点上,靠正常文档流随内容滚动——不是 rc-wp-breathe 那种 position:fixed
  // 视口坐标叠层(那种要靠 JS 监听滚动手动平移,创建到第一次滚动事件之间必然有窗口期,永远会有"刚开始动一下就
  // 偏移"的问题)。生词下划线/高亮/词组呼吸这几个既有效果都天然没有滚动漂移,根源就是它们全都用这个技术,
  // 从来没用过 position:fixed + 滚动跟随——用户当场问"下划线明明就没有这种问题"点破的正是这个架构差异。
  function _wrapWordBreathe(node, start, end, text) {
    try {
      // 校验偏移仍对得上词文字:多个查词高亮并存时(共享层 _wordHls 数组语义),同一文本节点里先物化的 mark
      // 会 splitText 把节点切短,让后一次查词捕获的偏移失效 → 不匹配就放弃包裹(返回 null,共享层落兜底)
      if (text != null && (node.nodeValue || '').slice(start, end) !== text) return null;
      var rest = node.splitText(start); rest.splitText(end - start);
      var mk = document.createElement('mark'); mk.className = 'ep-word-breathe';
      rest.parentNode.insertBefore(mk, rest); mk.appendChild(rest);
      return mk;
    } catch (e) { return null; }
  }
  // 点词即时"选中指示":ECDICT 快词秒回、赶不上 rc-wordpop 的 400ms 呼吸高亮 → 点英语词时立刻画一个选中框,
  // 下次点触(非点在框上)清掉。⚠ 用**自绘 absolute 浮层**(挂在词所在 .ep-sec,它 position:relative,随内容滚动、零漂移),
  // **不用 splitText 包 mark**——否则拆到带生词下划线的文本节点会让下划线闪一下。纯 EPUB 本地,不碰共享 rc-wordpop、不影响 PDF。
  var _epClickSel = [];
  function _epClearClickMark() {
    for (var i = 0; i < _epClickSel.length; i++) { try { _epClickSel[i].remove(); } catch (e) {} }
    _epClickSel = [];
  }
  function _epSetClickMark(node, range) {
    _epClearClickMark();
    try {
      var s = secOf(node); var sec = s && s.el; if (!sec || !range) return;
      var sr = sec.getBoundingClientRect();
      var rects = range.getClientRects();
      for (var i = 0; i < rects.length; i++) {
        var rc = rects[i]; if (!(rc.width > 0 && rc.height > 0)) continue;
        var d = document.createElement('div'); d.className = 'ep-click-sel';
        d.style.cssText = 'position:absolute;pointer-events:none;z-index:1;box-sizing:border-box;left:' + (rc.left - sr.left) + 'px;top:' + (rc.top - sr.top) + 'px;width:' + rc.width + 'px;height:' + rc.height + 'px;background:rgba(56,178,172,.25);border:1px solid rgba(120,231,210,.85);border-radius:3px';
        sec.appendChild(d); _epClickSel.push(d);
      }
    } catch (e) {}
  }
  function _unwrapWordBreathe(mk) {
    if (!mk || !mk.parentNode) return;
    var p = mk.parentNode;
    while (mk.firstChild) p.insertBefore(mk.firstChild, mk);
    p.removeChild(mk); p.normalize();
  }
  // 词组浮层:走共享层 RC.phrasepop(自包含,照 rc-wordpop 风格);底座回调挂呼吸态 / 收藏去 mark / 标掌握刷新 / 💡解释走 RC.result。
  function _openPhrasePopover(text, rect, ctx, snap) {
    if (!(window.RC && RC.phrasepop)) { RC.result && RC.result.aiCall('/pdf/api/translate', { text: text, target_lang: '中文' }, '🌐 翻译', { aiParams: function () { return (window.RC && RC.settings) ? RC.settings.aiParams() : {}; } }); return; }
    RC.phrasepop.show({
      text: text, rect: rect, context: ctx || '', file: FREL, langs: bookLangsArr(),
      onSolid: function () { _phraseSetSolid(); },                                  // 查询返回 → 停呼吸转常亮
      onFav: function (t, nowFav) { if (nowFav) _unwrapPhrase(); refreshVocabUnderlinesForAllPages(); },   // 收藏→变分词单元:去呼吸高亮 + 重画下划线
      onMastered: function () { refreshVocabUnderlinesForAllPages(); },             // 标掌握→刷新生词下划线
      onExplain: function (t) {   // 💡 解释:照搬 selBar 'explain' 路径(RC.result.aiCall + markHighlight/ankiSource),用词组锚
        var selTxt = t, selAnchor = snap, selCtx = ctx || '';
        RC.result.aiCall('/pdf/api/explain', { text: selTxt, context: selCtx }, '💡 AI 解释', {
          kind: 'explain', aiParams: function () { return (window.RC && RC.settings) ? RC.settings.aiParams() : {}; },
          markHighlight: function (mtext, body, sentence, hkind) {
            if (!selAnchor) { toast('无法定位选区'); return false; }
            var color = localStorage.getItem('eph-hl-color') || hlColors()[0];
            reqJson('POST', '/pdf/api/epub-highlights', { file: FREL, anchor: selAnchor, text: selTxt, color: color }, function (d) { var h = d.highlight; _hls[h.id] = h; var el = secEls[selAnchor.section]; if (el) applyHl(el, h); if (body) patchHl(h, { note: body, sentence: sentence || selCtx || '', kind: hkind || 'explain' }); }, function (er) { toast('高亮失败:' + er); });
            return true;
          },
          ankiSource: function () { return { file: FREL, sentence: selCtx, sourceUrl: location.origin + '/pdf/epub/view?file=' + encodeURIComponent(FREL) }; }
        });
      }
    });
  }
  // 工具栏「📘词组」:照搬 PDF onPhrase → 把当前选区变持久呼吸 mark(移交持久层,清原生蓝选区)+ 开词组浮层。
  window.onPhrase = function () {
    var t = (cur.text || '').trim(); if (!t) return;
    var rect = cur.rect, ctx = cur.ctx;
    var snap = cur.anchor ? { section: cur.anchor.section, start: cur.anchor.start, end: cur.anchor.end, text: t, context: ctx || '' } : null;
    hideSel();
    if (snap && _wrapPhraseRange(snap.section, snap.start, snap.end, true)) {
      try { window.getSelection().removeAllRanges(); } catch (e) {}   // 移交持久 mark,避免双重高亮(照搬 PDF _showPhraseHighlight)
    }
    _openPhrasePopover(t, rect, ctx, snap);
  };
  // 点词组高亮 mark → 高亮消失 + 重弹词组框(不再建新高亮)。照搬 PDF renderPhraseHl 的 layer.click;
  // pointerdown+pointerup 无移动 tap 检测(同 mark.ep-hl 那套),避 iOS click 不稳。
  (function () {
    var dn = null;
    document.addEventListener('pointerdown', function (e) {
      var mk = e.target.closest && e.target.closest('mark.ep-phrase-hl');
      dn = mk ? { mk: mk, x: e.clientX, y: e.clientY } : null;
    }, true);
    document.addEventListener('pointerup', function (e) {
      if (!dn) return; var d = dn; dn = null;
      var mk = e.target.closest && e.target.closest('mark.ep-phrase-hl');
      if (!mk || mk !== d.mk) return;
      if (Math.abs(e.clientX - d.x) > 8 || Math.abs(e.clientY - d.y) > 8) return;
      var snap = _phraseSnap, rect = mk.getBoundingClientRect();
      var txt = (snap && snap.text) || (mk.textContent || '').trim();
      _unwrapPhrase();   // snap 已存 → 先去高亮再重弹(noHighlight:不再建新 mark)
      _openPhrasePopover(txt, rect, (snap && snap.context) || '', snap);
    }, true);
  })();

  function _execCopy(s) { try { var ta = document.createElement('textarea'); ta.value = s; ta.style.cssText = 'position:fixed;left:-9999px;top:0'; document.body.appendChild(ta); ta.select(); var ok = document.execCommand('copy'); document.body.removeChild(ta); return ok; } catch (e) { return false; } }
  // 点高亮 mark → 编辑浮层(RC.highlight)。iOS 上点可选文本里的 <mark> 时 click 不稳(被当选词/置光标),
  // 改用 pointerdown+pointerup 的「无移动 tap」检测;同时避免 click 与「再点关」double-trigger。
  // 点高亮 mark 改交互(2026-07-05;2026-07-21 长按↔双击对调):长按 → 高亮内容加入对话上下文(助手开着时);双击 → 编辑浮层。
  // 单击不再开框。走共享 RC.highlight.gesture(PDF/EPUB 一致);RC 未加载则兜底回原单击开框。
  (function () {
    var G = (window.RC && RC.highlight && RC.highlight.gesture) ? RC.highlight.gesture({
      onLongPress: function (id) { var h = _hls[id]; if (h && window.__asstOpen && window.__asstOpen()) _epubHlToAsst(h); },
      onDoubleTap: function (id) { var h = _hls[id]; if (!h) return; try { var s = window.getSelection(); if (s && s.removeAllRanges) s.removeAllRanges(); } catch (_) {} openHlEditor(h, col.querySelector('mark.ep-hl[data-id="' + id + '"]')); }
    }) : null;
    if (G) {
      document.addEventListener('pointerdown', function (e) { var mk = e.target.closest && e.target.closest('mark.ep-hl'); if (mk && mk.dataset.id) G.down(mk.dataset.id, e.clientX, e.clientY); else G.cancel(); }, true);
      document.addEventListener('pointermove', function (e) { G.move(e.clientX, e.clientY); }, true);
      document.addEventListener('pointerup', function (e) { var mk = e.target.closest && e.target.closest('mark.ep-hl'); G.up(mk && mk.dataset.id ? mk.dataset.id : ''); }, true);
      document.addEventListener('pointercancel', function () { G.cancel(); }, true);
    } else {
      var dn = null;
      document.addEventListener('pointerdown', function (e) { var mk = e.target.closest && e.target.closest('mark.ep-hl'); dn = mk ? { mk: mk, x: e.clientX, y: e.clientY } : null; }, true);
      document.addEventListener('pointerup', function (e) { if (!dn) return; var d = dn; dn = null; var mk = e.target.closest && e.target.closest('mark.ep-hl'); if (!mk || mk !== d.mk) return; if (Math.abs(e.clientX - d.x) > 8 || Math.abs(e.clientY - d.y) > 8) return; if (_hls[mk.dataset.id]) openHlEditor(_hls[mk.dataset.id], mk); }, true);
    }
  })();
  // 双击高亮(助手开着)→ 把高亮文本+所在句写进 cur(选中状态),curSelection() 持久兜底即带给助手
  // (高亮就在屏上、时间戳刷新 → 新鲜校验必过)。零后端改动,复用现有「选中→助手」路径。
  function _epubHlToAsst(h) {
    var txt = ((h.text || h.body || h.sentence || '') + '').trim(); if (!txt) return;
    // 复用助手输入框上方的「焦点选中」chip(带 ✕,可随时取消 + 一眼看到已选中),不弹 toast。__focusSel 被 runAssistant 消费(见 selInfo 覆盖处)。
    if (window.__setFocusSel) window.__setFocusSel(txt.slice(0, 1200), 'text');
  }
  // ── 日语分词缓存(真分词,照搬 PDF 的原生模式:PyMuPDF 提取字符层时用 fugashi 给每个字符标好 word id
  //   char.w,前端 _expandToWordStart/End 只是同步查这个已经算好的表——不是点击时现发请求。这里对应地在
  //   「章节渲染完」时机(_fetchSection 空闲回调,见上)后台调 /pdf/api/epub-tokenize 给整章分词一次,
  //   缓存在 section 内「可数字符」绝对偏移空间(跟 offsetOf/_countableText/高亮 anchor 同一套坐标系,
  //   故不受 ruby/生词等其它装饰事后 splitText 打散节点的影响)。wordAt() 点击时同步查表命中就用精确边界,
  //   缓存还没到/无日语字符 就退回原字符类兜底——不阻塞、不改变现有响应时机。
  //   根治:平假名/汉字被 wordAt 旧字符类归成一类,导致不断句日语正文单击选中一整句)
  var _tokCache = {};   // secIdx -> [{start,end}](已就绪) | 'pending'(请求中,退回兜底)
  function _tokenizeSection(secEl) {
    if (!secEl || secEl.dataset.epTok) return;
    var idx = parseInt(secEl.dataset.idx, 10);
    var full = _countableText(secEl);
    if (!/[぀-ヿ㐀-鿿]/.test(full)) { _tokCache[idx] = []; return; }   // 无日语字符,不必分词,也不用占 dataset 标记(允许日后内容变化重试)
    secEl.dataset.epTok = '1';
    _tokCache[idx] = 'pending';
    reqJson('POST', '/pdf/api/epub-tokenize', { file: FREL, texts: [full] }, function (d) {
      var items = d.items || [];
      _tokCache[idx] = (items[0] && items[0].tokens) || [];
    }, function () { _tokCache[idx] = []; secEl.dataset.epTok = ''; });
  }
  // 给定 section 绝对偏移区间,若命中的 token 把 start/end 卡在词中间就扩到词边界(供 wordAt 单击 + 拖选对齐共用)
  function _tokSnap(secIdx, start, end) {
    var toks = _tokCache[secIdx];
    if (!toks || toks === 'pending' || !toks.length) return null;
    var ns = start, ne = end, hit = false;
    for (var k = 0; k < toks.length; k++) {
      var t = toks[k];
      if (start >= t.start && start < t.end) { ns = t.start; hit = true; }
      if (end > t.start && end <= t.end) { ne = t.end; hit = true; }
    }
    return hit ? { start: ns, end: ne } : null;
  }
  // ── 单击选词(照搬 PDF 13-selection):点正文文字 → 选中该词;英/日词弹工具栏(→查词),纯中文词单击=不做事 ──
  function caretFromPoint(x, y) {
    if (document.caretRangeFromPoint) { var r = document.caretRangeFromPoint(x, y); return r ? { node: r.startContainer, offset: r.startOffset } : null; }
    if (document.caretPositionFromPoint) { var p = document.caretPositionFromPoint(x, y); return p ? { node: p.offsetNode, offset: p.offset } : null; }
    return null;
  }
  function wordAt(node, off) {
    if (!_countable(node)) return null;   // G0:点在我们注入的 rt 读音 / 译文块上 → 不选词
    var s = node.nodeValue || ''; if (!s) return null;
    var isW = function (c) { return /[A-Za-z0-9'’\-]/.test(c) || /[぀-ヿ㐀-鿿가-힯一-鿿]/.test(c); };
    var i = off; if (i >= s.length) i = s.length - 1; if (i < 0) return null;
    if (!isW(s[i])) { if (i > 0 && isW(s[i - 1])) i--; else return null; }
    // 命中日语字符 → 优先用真分词缓存精确扩边界(见 _tokenizeSection);缓存未就绪/跨节点(装饰把词从中间拆开)
    // 时退回下面的字符类扩张兜底,不强行跨节点选(wordAt 契约是单节点内 start/end)
    if (/[぀-ヿ㐀-鿿]/.test(s[i])) {
      var secInfo = secOf(node);
      if (secInfo) {
        var abs = offsetOf(secInfo.el, node, i);
        var snap = _tokSnap(secInfo.idx, abs, abs + 1);
        if (snap) {
          var a = _domPosAtOffset(secInfo.el, snap.start), b = _domPosAtOffset(secInfo.el, snap.end);
          if (a && b && a.node === b.node && b.offset > a.offset) return { node: a.node, start: a.offset, end: b.offset, text: a.node.nodeValue.slice(a.offset, b.offset) };
        }
      }
    }
    var lo = i, hi = i + 1;
    while (lo > 0 && isW(s[lo - 1])) lo--;
    while (hi < s.length && isW(s[hi])) hi++;
    return { node: node, start: lo, end: hi, text: s.slice(lo, hi) };
  }
  // ── 多级点击选择(1击=词/2击=视觉行/3击=段落,循环 1→2→3→1)。桌面鼠标 + 触屏统一走 pointerdown/pointerup
  // (不依赖浏览器 dblclick / e.detail——iOS 合成 click 的 detail 不像桌面那样递增到 2/3,这个语义在移动端不可靠),
  // 自建 380ms 时间窗 + 「同一个词」判据计数(比像素距离更抗字号/设备差异;沿用 PDF reader.src/13-selection.js
  // 的 380ms 窗口对齐手感)。1 击=完全沿用原有精确选词逻辑(未改动其行为);2/3 击是新增的范围扩大。
  var _tapDown = null, _tapWord = null, _tapTime = 0, _tapCount = 0;
  var TAP_WIN_MS = 380, TAP_MOVE_TOL = 10;
  content.addEventListener('pointerdown', function (e) {
    if (e.pointerType === 'mouse' && e.button !== 0) { _tapDown = null; return; }
    _tapDown = { x: e.clientX, y: e.clientY };
  });
  content.addEventListener('pointerup', function (e) {
    var down = _tapDown; _tapDown = null;
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    if (!down) return;   // 本次 pointerdown 没被记录到(例如手写双击切工具在 col 捕获阶段吞掉了它)→ 保守放弃,交给别的模块处理,不抢手写手势
    if (Math.hypot(e.clientX - down.x, e.clientY - down.y) > TAP_MOVE_TOL) return;   // 有明显位移(拖选/滑动)→ 交给原生拖选,不当 tap
    if (document.documentElement.dataset.bwReaderExtensionActive === '1') {
      _tapCount = 0; _tapWord = null;
      return;   // 扩展接管点词/多击语义；PWA 只继续提供书籍 DOM、选区和锚点。
    }
    if (e.target.closest('img, mark.ep-hl, mark.ep-phrase-hl, mark.ep-word-breathe, .rc-note, .rc-fig-badge, a, #ep-side, #ep-sel, #rc-dict-pop, #rc-fig-pop, .rc-up-bar, .rc-up-edit, .ep-up-editbtn')) { _tapCount = 0; _tapWord = null; return; }   // 词组/高亮/呼吸高亮/便签/图/用户页 Aa 按钮与编辑器 tap 由各自专属逻辑接管;顺手清计数,防止跟下次文字 tap 串成假的连击(用户页**正文 .rc-up-body**不排除→单击查词照常)
    _epClearClickMark();   // 新的一次点触(非点在已有 mark/UI 上)→ 先清掉上一个点词的"选中指示"
    try { if (typeof _cselClear === 'function') _cselClear(); } catch (e) {}   // 顺手清掉上一次拖选的自绘高亮残留(直翻开点词不走 _cselApply,靠这里清)
    var sel = window.getSelection();
    if (sel && !sel.isCollapsed && (sel.toString() || '').trim()) return;   // 已有拖选 → 不抢
    var pos = caretFromPoint(e.clientX, e.clientY);
    if (!pos || pos.node.nodeType !== 3 || !col.contains(pos.node)) { _tapCount = 0; _tapWord = null; _tapTime = 0; return; }
    var w = wordAt(pos.node, pos.offset);
    if (!w || !w.text) { _tapCount = 0; _tapWord = null; _tapTime = 0; return; }
    var hitRange;
    try { hitRange = document.createRange(); hitRange.setStart(w.node, w.start); hitRange.setEnd(w.node, w.end); } catch (er) { return; }
    if (!(RC.ui && RC.ui.rangeHitTest && RC.ui.rangeHitTest(hitRange, e.clientX, e.clientY, { pointerType: e.pointerType || 'mouse' }))) {
      _tapCount = 0; _tapWord = null; _tapTime = 0;
      return;   // caret API 只给最近插入点；点章/段落空白不能被记成最近词的一击
    }
    var now = Date.now();
    var sameSpot = _tapWord && _tapWord.node === w.node && _tapWord.start === w.start && _tapWord.end === w.end;
    _tapCount = (sameSpot && now - _tapTime < TAP_WIN_MS) ? (_tapCount % 3) + 1 : 1;
    _tapTime = now; _tapWord = { node: w.node, start: w.start, end: w.end };
    if (_tapCount === 2 && _selectVisualLine(e.clientX, e.clientY, w)) return;
    if (_tapCount === 3 && _selectParagraph(w)) return;
    if (_tapCount !== 1) _tapCount = 1;   // 2/3 击找不到可用的行/块级祖先(如裸文本没有 p/li 包裹)→ 退回单词逻辑,计数复位重来
    _tap1Word(w, sel);
  });
  // ── 语法分析:字典小框「📊 语法」+ 选中工具栏「📊 语法」共同入口,汇到 RC.grammar.analyze(共享核心,跟 PDF 同一套)。
  //   EPUB 没有 PDF 的字符层,"该分析哪句话"用 RC.grammar.extractSentence(ctx,focus) 从段落文本里按标点切句抠出来
  //   (ctx = 词/选区所在块级祖先的文本,跟其它 AI 功能取上下文的方式一致,不另造机制)。
  function _openGrammarSet() { var s = $('ep-grammar-set'); if (s) s.classList.add('open'); }   // 展开「⚙ 启用语法 KG」折叠区(门槛没达到时引导用户去勾)
  function _grammarAnalyzeFrom(ctx, focus) {
    if (!(window.RC && RC.grammar)) { toast('语法分析暂未接入'); return; }
    var sentence = RC.grammar.extractSentence(ctx, focus);
    RC.grammar.analyze({
      file: FREL, sentence: sentence, text: focus, container: 'ep-grammar-body',
      aiParams: function () { return (window.RC && RC.settings) ? RC.settings.aiParams() : {}; },
      sourceUrl: function () { return location.origin + '/pdf/epub/view?file=' + encodeURIComponent(FREL); },
      viewModeKey: 'eph-grammar-view',
      onOpenPanel: function () { if (window.RC && RC.sidedrawer) RC.sidedrawer.open('grammar'); },
      onNeedSetup: function () { if (window.RC && RC.sidedrawer) RC.sidedrawer.open('grammar'); _openGrammarSet(); },
      onToast: toast,
    });
  }
  // 1 击(照搬原逻辑,未改动):英/日词「直翻」开→弹字典小框;纯中文单击不做事;直翻关→选中+开工具栏(查词组)
  function _tap1Word(w, sel) {
    var t = w.text, isEn = /^[A-Za-z][A-Za-z'’\-]*$/.test(t);
    // 日文书:汉字词也当日语(查词);中文书/自动:纯汉字词不弹查词(母语)
    var isJa = /[぀-ヿ]/.test(t) || (BOOK_LANGS.indexOf('ja') >= 0 && /[一-鿿㐀-䶿]/.test(t));
    if (!isJa && !isEn) { if (sel) sel.removeAllRanges(); hideSel(); return; }   // 纯中文等 → 单击不做事 + 清掉
    var r; try { r = document.createRange(); r.setStart(w.node, w.start); r.setEnd(w.node, w.end); } catch (er) { return; }
    if (_clickTranslate() && window.RC && RC.wordpop) {
      var rect = r.getBoundingClientRect();
      var pblk = w.node.parentElement && w.node.parentElement.closest ? w.node.parentElement.closest('p,li,td,blockquote,h1,h2,h3,h4,div') : null;
      var pctx = _countableText(pblk).trim().slice(0, 1200);
      if (sel) sel.removeAllRanges();
      _epSetClickMark(w.node, r);   // 点词即时"选中指示"(自绘浮层,不拆文本 → 不扰生词下划线)
      hideSel();
      RC.wordpop.show({ word: t, rect: rect, ctx: pctx, file: FREL, langs: bookLangsArr(),
        markHighlight: function () {}, onMastered: function () {},
        onGrammar: function (w) { _grammarAnalyzeFrom(pctx, w || t); },
        onFallback: function (word) { RC.result.aiCall('/pdf/api/translate', { text: word, target_lang: '中文' }, '🌐 翻译', { aiParams: function () { return (window.RC && RC.settings) ? RC.settings.aiParams() : {}; } }); },
        // 呼吸高亮 host-bind:真实 <mark> 包文字节点(同生词下划线/词组呼吸高亮的技术),随内容自然滚动,
        // 不用 rc-wordpop 自带那套 position:fixed + 监听滚动手动平移的兜底方案(那个天生有创建到第一次滚动
        // 事件之间的窗口期,会漂移——用户当场用"下划线怎么没这问题"点破了这个架构差异)。
        breathe: { wrap: function () { return _wrapWordBreathe(w.node, w.start, w.end, t); }, unwrap: _unwrapWordBreathe } });
      return;
    }
    if (USE_CUSTOM_SEL) { _cselApply(r, { snapWords: false }); return; }   // 自绘选区(替原生 addRange,防 iOS 抖动)
    try { sel.removeAllRanges(); sel.addRange(r); } catch (er2) { return; }
    setTimeout(function () { captureSel({ snapWords: false }); }, 0);   // 直翻关 → 选中 + 弹工具栏(词已精确,跳过边界对齐)
  }
  // 2 击 = 视觉行:reflow 没有像 PDF char-layer 那样现成的逐行像素数组,只能现测——用 Range.getClientRects()
  // 找落点所在的「行带」(取块内最高的 rect 当行高基准),再逐个 countable 字符测 1 字符 Range 命中该行带的区间。
  // 只在双击那一刻跑一次(不是每帧),复用同一个 probe Range 减少对象分配;MAX_CHARS 防止误配到超大块级祖先拖垮交互。
  function _lineRangeInBlock(blk, y) {
    var full = document.createRange(); full.selectNodeContents(blk);
    var rects = full.getClientRects(); if (!rects.length) return null;
    var lineH = 0; for (var ri = 0; ri < rects.length; ri++) if (rects[ri].height > lineH) lineH = rects[ri].height;
    if (!lineH) lineH = 20;
    var band = null;
    for (var i = 0; i < rects.length; i++) { var rc = rects[i]; if (y >= rc.top - lineH * 0.3 && y <= rc.bottom + lineH * 0.3) { band = rc; break; } }
    if (!band) band = rects[0];
    var walker = document.createTreeWalker(blk, NodeFilter.SHOW_TEXT, null), n;
    var startNode = null, startOff = 0, endNode = null, endOff = 0, found = false, measured = 0, MAX_CHARS = 3000;
    var probe = document.createRange();
    while ((n = walker.nextNode()) && measured < MAX_CHARS) {
      if (!_countable(n)) continue;
      var s = n.nodeValue, len = s.length;
      for (var ci = 0; ci < len && measured < MAX_CHARS; ci++, measured++) {
        try { probe.setStart(n, ci); probe.setEnd(n, ci + 1); } catch (e) { continue; }
        var cr = probe.getClientRects()[0]; if (!cr) continue;
        if (cr.bottom > band.top + 1 && cr.top < band.bottom - 1) {
          if (!found) { startNode = n; startOff = ci; found = true; }
          endNode = n; endOff = ci + 1;
        } else if (found && cr.top >= band.bottom - 1) { ci = len; }   // 已经测过该行 → 跳出当前文本节点内层循环,省剩余字符
      }
    }
    if (!found) return null;
    var r = document.createRange();
    try { r.setStart(startNode, startOff); r.setEnd(endNode, endOff); } catch (e) { return null; }
    return r;
  }
  function _selectVisualLine(x, y, w) {
    var el = w.node.parentElement;
    var blk = el && el.closest ? el.closest('p,li,td,blockquote,h1,h2,h3,h4,div') : null;
    if (!blk || !col.contains(blk)) return false;
    var r = _lineRangeInBlock(blk, y);
    if (!r) return false;
    var sel = window.getSelection();
    if (USE_CUSTOM_SEL) return _cselApply(r, { snapWords: false });   // 自绘选区(替原生 addRange,防 iOS 抖动)
    try { sel.removeAllRanges(); sel.addRange(r); } catch (e) { return false; }
    setTimeout(function () { captureSel({ snapWords: false }); }, 0);   // 行边界已是精确视觉行,跳过词边界对齐(否则 CJK 换行断在词中间会被越界扩张吃掉相邻行)
    return true;
  }
  // blk 内第一个/最后一个可数文字节点的边界(不能用 r.selectNodeContents(blk) —— 那样端点落在元素节点本身,
  //   offsetOf() 的 TreeWalker 恒等比较永远匹配不上元素节点 → anchor 退化成 start===end,标记的高亮存进服务端却永远不渲染)。
  function _wholeBlockTextRange(blk) {
    var walker = document.createTreeWalker(blk, NodeFilter.SHOW_TEXT, null), n;
    var startNode = null, startOff = 0, endNode = null, endOff = 0;
    while ((n = walker.nextNode())) {
      if (!_countable(n)) continue;
      var len = n.nodeValue.length; if (!len) continue;
      if (!startNode) { startNode = n; startOff = 0; }
      endNode = n; endOff = len;
    }
    if (!startNode) return null;
    var r = document.createRange();
    try { r.setStart(startNode, startOff); r.setEnd(endNode, endOff); } catch (e) { return null; }
    return r;
  }
  // 3 击 = 整段:最近的块级祖先(p/li/td/blockquote/h1-4/div)全部内容
  function _selectParagraph(w) {
    var el = w.node.parentElement;
    var blk = el && el.closest ? el.closest('p,li,td,blockquote,h1,h2,h3,h4,div') : null;
    if (!blk || !col.contains(blk)) return false;
    var r = _wholeBlockTextRange(blk);
    if (!r) return false;
    var sel = window.getSelection();
    if (USE_CUSTOM_SEL) return _cselApply(r, { snapWords: false });   // 自绘选区(替原生 addRange,防 iOS 抖动)
    try { sel.removeAllRanges(); sel.addRange(r); } catch (e) { return false; }
    setTimeout(function () { captureSel({ snapWords: false }); }, 0);
    return true;
  }
  function _clickTranslate() { var v = localStorage.getItem('eph-click-translate'); return v === null ? true : v === '1'; }

  // ════════ 收藏夹 PDF 条目:char-layer 式自定义选择(替原生 DOM Selection,修 iOS 长按拖选乱跨行)════════
  //   背景:收藏夹物化成 EPUB,PDF 条目 = 页图 + 透明可选词层(.fav-pdf-txt/.fav-pdf-line/span)。原生 DOM
  //   Selection 在 iOS 对绝对/嵌套词层的长按拖选会乱跨上下多行(v6 行盒分组只缓解,非根治)。**逐字照搬 PDF
  //   阅读器主力工具的 char-layer 做法**(reader.src/13-selection.js::_bindCharLayer + _selByCharRange):
  //   词层 user-select:none 彻底关掉原生选区(iOS 长按拖选引擎压根不启动 → 零抖动),自建"拖选=按词 bbox 圈
  //   范围 → 自绘高亮浮层 .fav-pdf-sel → **手动组 cur + showSel**(不碰原生 selection,与 char-layer 手动开
  //   toolbar 同构)"。⚠ 关键坑:user-select:none 下程序化 addRange 后 getSelection().toString() 恒空 → 无法
  //   复用 captureSel 的原生选区读取;故走**手动 cur**(text/anchor 用既有 offsetOf/_countableText 算,与
  //   高亮/便签 offset 锚同口径)。既有 content 的 capture 相 mouseup/touchend→captureSel(空选区→hideSel)会
  //   清掉手动 toolbar → 在 **document capture** 相拦下:真拖选(moved)时 _favEnd 后 stopPropagation,content 那
  //   两个 capture 监听收不到 → 不误清(零改 captureSel)。单击查词仍走既有 content 单击处理(caretRangeFromPoint,
  //   user-select:none 下照常命中透明词)。**仅收藏夹书(FREL 前缀)生效**;EPUB 条目 / 正文 / 普通书选择一律不碰。
  var IS_FAV_BOOK = FREL.replace(/^\//, '').indexOf('资源/收藏夹/') === 0;
  var _favDrag = null;   // {page, words, a, b, x0, y0, moved, dir, isMouse}
  var FAV_MOVE_TOL = 8;
  // 收集 fav 页的词 span(reading order = DOM order),缓存各词 page-local bbox(选字拖选期禁滚 → rect 不动)
  function _favWords(page) {
    var spans = page.querySelectorAll('.fav-pdf-txt .fav-pdf-line span');
    var pr = page.getBoundingClientRect();
    var arr = [];
    for (var i = 0; i < spans.length; i++) {
      var sp = spans[i], tn = sp.firstChild;
      if (!tn || tn.nodeType !== 3 || !tn.nodeValue) continue;
      var r = sp.getBoundingClientRect();
      if (!r.width && !r.height) continue;
      arr.push({ node: tn, left: r.left - pr.left, top: r.top - pr.top, right: r.right - pr.left, bottom: r.bottom - pr.top });
    }
    return arr;
  }
  // page-local (x,y) 命中词:严格 bbox → 同行 x 最近 → Manhattan 兜底(照 13-selection::_findCharAt)
  function _favHit(words, x, y) {
    var i, c;
    for (i = 0; i < words.length; i++) { c = words[i]; if (x >= c.left && x <= c.right && y >= c.top && y <= c.bottom) return i; }
    var best = -1, bd = Infinity;
    for (i = 0; i < words.length; i++) { c = words[i]; if (y >= c.top - 2 && y <= c.bottom + 2) { var dx = x < c.left ? c.left - x : (x > c.right ? x - c.right : 0); if (dx < bd) { bd = dx; best = i; } } }
    if (best >= 0) return best;
    for (i = 0; i < words.length; i++) { c = words[i]; var cx = (c.left + c.right) / 2, cy = (c.top + c.bottom) / 2; var d = Math.abs(x - cx) + Math.abs(y - cy) * 3; if (d < bd) { bd = d; best = i; } }
    return best;
  }
  // 拖选起点必须真的碰到词框；_favHit 的同行/全页 nearest 只允许在已经从有效词起手后，
  // 用于手指拖出文字区域时继续延伸选区，绝不能拿来把页边空白变成最近词。
  function _favHitStrict(words, x, y, isMouse) {
    var sx = isMouse ? 1 : 3, sy = isMouse ? 2 : 5;
    for (var i = 0; i < words.length; i++) {
      var c = words[i];
      if (x >= c.left - sx && x <= c.right + sx && y >= c.top - sy && y <= c.bottom + sy) return i;
    }
    return -1;
  }
  // 自绘选中高亮(char-layer 的 .sel-overlay 等价):按选中词 [a..b] 的 page-local bbox 画 .hl(同行合并成连续矩形,视觉贴合)
  function _favPaintWords(page, words, a, b) {
    var ov = page.querySelector('.fav-pdf-sel'); if (!ov) return;
    ov.innerHTML = '';
    if (a > b) { var t = a; a = b; b = t; }
    var box = null, out = [];
    for (var i = a; i <= b && i < words.length; i++) {
      var c = words[i], h = c.bottom - c.top;
      if (box && Math.abs(c.top - box.top) <= h * 0.5 && c.left <= box.right + h * 0.6) {
        box.right = Math.max(box.right, c.right); box.bottom = Math.max(box.bottom, c.bottom);
      } else { if (box) out.push(box); box = { left: c.left, top: c.top, right: c.right, bottom: c.bottom }; }
    }
    if (box) out.push(box);
    out.forEach(function (r) { var d = document.createElement('div'); d.className = 'hl'; d.style.cssText = 'position:absolute;background:rgba(90,150,255,.32);border-radius:2px;left:' + r.left + 'px;top:' + r.top + 'px;width:' + (r.right - r.left) + 'px;height:' + (r.bottom - r.top) + 'px'; ov.appendChild(d); });
  }
  function _favClearOverlays() { document.querySelectorAll('.fav-pdf-sel').forEach(function (o) { o.innerHTML = ''; }); }

  function _favStart(page, clientX, clientY, isMouse) {
    if (_epInk && (_epInk.mode || _epInk.drawing)) return;   // 手写模式/正在画 → 不选字
    _favClearOverlays();                                     // 起新选:清掉上一处 fav 自绘高亮(照 PDF onStart 清 sel-overlay)
    var words = _favWords(page); if (!words.length) return;
    var pr = page.getBoundingClientRect();
    var a = _favHitStrict(words, clientX - pr.left, clientY - pr.top, isMouse);
    if (a < 0) return;
    _favDrag = { page: page, words: words, a: a, b: a, x0: clientX, y0: clientY, moved: false, dir: isMouse ? 'select' : null, isMouse: isMouse };
  }
  function _favMove(clientX, clientY, ev) {
    var g = _favDrag; if (!g) return;
    var dx = Math.abs(clientX - g.x0), dy = Math.abs(clientY - g.y0);
    if (!g.moved && dx + dy < FAV_MOVE_TOL) return;
    if (!g.isMouse && g.dir === null) {   // 触摸首次动够时锁方向:竖直为主=滚动(放弃选字),否则=选字(照 13-selection 触摸方向锁)
      g.dir = (dy > dx) ? 'scroll' : 'select';
      if (g.dir === 'scroll') { _favDrag = null; _favClearOverlays(); return; }
    }
    g.moved = true;
    if (ev && ev.cancelable) ev.preventDefault();   // 选字模式:拦下页面滚动
    var pr = g.page.getBoundingClientRect();
    var idx = _favHit(g.words, clientX - pr.left, clientY - pr.top);
    if (idx < 0) return;
    g.b = idx;
    _favPaintWords(g.page, g.words, g.a, g.b);
  }
  // 拖选定案:词已整词 → 用既有 offsetOf/_countableText 算 section 内 offset 锚 + 选中文本(与高亮/便签同口径),
  // 手动组 cur + showSel(不碰原生 selection,照 PDF char-layer 手动开 toolbar)。返回 true=真选中。
  function _favEnd() {
    var g = _favDrag; _favDrag = null;
    if (!g || !g.moved) return false;   // 纯 tap → 交给既有 content 单击查词逻辑(caretRangeFromPoint)
    var a = Math.min(g.a, g.b), b = Math.max(g.a, g.b);
    var wa = g.words[a], wb = g.words[b];
    if (!wa || !wb) return false;
    var sec = secOf(g.page); if (!sec) return false;
    try {
      var full = _countableText(sec.el);
      var start = offsetOf(sec.el, wa.node, 0);
      var end = offsetOf(sec.el, wb.node, wb.node.nodeValue.length);
      if (end < start) { var tmp = start; start = end; end = tmp; }
      var text = full.slice(start, end).trim();
      if (!text) return false;
      try { if (_phraseSnap) _unwrapPhrase(); } catch (e) {}   // 重新选 → 清上一处词组呼吸高亮(照 captureSel)
      cur = { text: text, ctx: full.trim().slice(0, 1200), anchor: { section: sec.idx, start: start, end: end },
              rect: (wa.node.parentElement || g.page).getBoundingClientRect(), _selT: Date.now() };
      showSel();
      _favPaintWords(g.page, g.words, a, b);   // 定案高亮
      try { window.__setFocusSel && window.__setFocusSel(text, /\$/.test(text) ? 'formula' : 'text'); } catch (e) {}
      return true;
    } catch (e) { dbg('favEnd ERR: ' + (e && e.message)); return false; }
  }
  function _favBindPage(page) {
    if (page.__favBound) return; page.__favBound = true;
    var txt = page.querySelector('.fav-pdf-txt'); if (!txt) return;
    txt.style.userSelect = 'none'; txt.style.webkitUserSelect = 'none';   // belt:即便存量夹旧 CSS 还是 user-select:text
    if (!page.querySelector('.fav-pdf-sel')) { var ov = document.createElement('div'); ov.className = 'fav-pdf-sel'; ov.style.cssText = 'position:absolute;left:0;top:0;right:0;bottom:0;pointer-events:none;z-index:4'; page.appendChild(ov); }
    page.addEventListener('mousedown', function (e) { if (e.button !== 0) return; _favStart(page, e.clientX, e.clientY, true); });
    page.addEventListener('touchstart', function (e) { if (e.touches.length !== 1) { _favDrag = null; return; } var t = e.touches[0]; _favStart(page, t.clientX, t.clientY, false); }, { passive: true });
    page.addEventListener('touchmove', function (e) { if (!_favDrag || e.touches.length !== 1) return; var t = e.touches[0]; _favMove(t.clientX, t.clientY, e); }, { passive: false });
  }
  function _favBindSection(secEl) { if (!IS_FAV_BOOK || !secEl) return; try { secEl.querySelectorAll('.fav-pdf-page').forEach(_favBindPage); } catch (e) {} }
  // 拖选 move/end 挂 **document capture**(单次,防每页绑泄漏;end 用 capture 才能抢在 content 那两个 capture 相
  // mouseup/touchend→captureSel 之前 stopPropagation → 不误清手动 toolbar)。仅 _favDrag 有值时动作。
  if (IS_FAV_BOOK) {
    document.addEventListener('mousemove', function (e) { if (_favDrag && _favDrag.isMouse) _favMove(e.clientX, e.clientY, null); });
    document.addEventListener('mouseup', function (e) {
      if (!(_favDrag && _favDrag.isMouse)) return;
      var moved = _favDrag.moved; _favEnd(); if (moved) e.stopPropagation();
    }, true);
    document.addEventListener('touchend', function (e) {
      if (!_favDrag) return;
      var moved = _favDrag.moved; _favEnd();
      if (moved) { if (e.cancelable) e.preventDefault(); e.stopPropagation(); }   // 真拖选 → 拦下 content capture captureSel(防清 toolbar)+ 合成 click
    }, true);
    document.addEventListener('touchcancel', function () { _favDrag = null; }, true);
    // 点 fav 页 / 工具栏之外 → 清掉 fav 自绘高亮(toolbar 由既有 captureSel 空选区兜底 hideSel;这里只补清自绘层)
    document.addEventListener('pointerdown', function (e) {
      if (_favDrag) return;
      if (e.target && e.target.closest && e.target.closest('.fav-pdf-page')) return;   // 点 fav 页 → 交 _favStart 处理
      _favClearOverlays();
    });
  }

  // ════════ 普通 EPUB 正文:自绘选区(替原生 Selection,根治 iOS 长按拖选抖动)════════
  //   策略照搬 fav(user-select:none 关原生 → iOS 长按拖选引擎不启动=零抖动 + 自建拖选/画高亮/手动组 cur),
  //   但 reflow 无现成词 span → 用 caretFromPoint + Range 现算;高亮 range.getClientRects() 按 .ep-sec 锚定(随内容滚动零漂移)。
  //   仅普通 EPUB(非收藏夹)生效;单击查词/直翻仍走既有 content 单击。插入页(.ep-usec)不加 user-select:none → 保留原生选区+450ms 轮询。
  //   开关 USE_CUSTOM_SEL 一键回退整套(=false 时全走原生老路)。设计见 /reader-middlelayer-design.md 之外的 A(选中抖动)。
  var USE_CUSTOM_SEL = !IS_FAV_BOOK;
  var _cselDraw = [], _cdrag = null, CSEL_TOL = 8;
  function _cselClear() { for (var i = 0; i < _cselDraw.length; i++) { try { _cselDraw[i].remove(); } catch (e) {} } _cselDraw = []; }
  function _cselSecAt(x, y) { var el = document.elementFromPoint(x, y); return (el && el.closest) ? el.closest('.ep-sec') : null; }
  function _cselPaint(range) {
    _cselClear();
    var rects = range.getClientRects();
    for (var i = 0; i < rects.length; i++) {
      var rc = rects[i]; if (!(rc.width > 0 && rc.height > 0)) continue;
      var sec = _cselSecAt((rc.left + rc.right) / 2, (rc.top + rc.bottom) / 2); if (!sec) continue;
      var sr = sec.getBoundingClientRect();
      var d = document.createElement('div'); d.className = 'ep-csel';
      d.style.cssText = 'position:absolute;pointer-events:none;z-index:1;background:rgba(90,150,255,.32);border-radius:2px;left:' + (rc.left - sr.left) + 'px;top:' + (rc.top - sr.top) + 'px;width:' + rc.width + 'px;height:' + rc.height + 'px';
      sec.appendChild(d); _cselDraw.push(d);
    }
  }
  // 把一个 Range 落成 cur + showSel + 自绘高亮(镜像 captureSel 的锚/snap,但从传入 range 取、不碰原生选区)。
  function _cselApply(range, opts) {
    try {
      if (!range || !col.contains(range.commonAncestorContainer)) return false;
      var si = secOf(range.startContainer); if (!si) return false;   // v1:仅正文 .ep-sec(插入页走原生轮询)
      var start = offsetOf(si.el, range.startContainer, range.startOffset);
      var se = secOf(range.endContainer);
      var end = (se && se.el === si.el) ? offsetOf(si.el, range.endContainer, range.endOffset) : _countableText(si.el).length;   // 跨章 → 截到本章末(v1)
      if (end < start) { var t = start; start = end; end = t; }
      var full = _countableText(si.el);
      if (!opts || opts.snapWords !== false) { var sn = _snapWordBoundary(full, start, end, si.idx); start = sn.start; end = sn.end; }
      var text = full.slice(start, end).trim(); if (!text) return false;
      var pr = _rangeFromOffsets(si.el, start, end) || range;
      try { if (_phraseSnap) _unwrapPhrase(); } catch (e) {}
      cur = { text: text, ctx: full.trim().slice(0, 1200), anchor: { section: si.idx, start: start, end: end }, rect: pr.getBoundingClientRect(), _selT: Date.now() };
      _cselPaint(pr); showSel();
      try { window.__setFocusSel && window.__setFocusSel(text, /\$/.test(text) ? 'formula' : 'text'); } catch (e) {}
      return true;
    } catch (e) { return false; }
  }
  var _CSEL_LEAF = 'p,li,td,blockquote,h1,h2,h3,h4,h5,h6,dd,dt,figcaption';   // 叶子文本块(**不含**容器 div → 否则夹到的是整章首个文字=标题,反而把标题拽进选区)
  function _blockAtY(secEl, y) {
    var blks = secEl.querySelectorAll(_CSEL_LEAF), best = null, bd = Infinity;
    for (var i = 0; i < blks.length; i++) {
      var b = blks[i]; if (!(b.textContent || '').trim()) continue;
      var r = b.getBoundingClientRect(); if (!r.height) continue;
      if (y >= r.top && y <= r.bottom) return b;   // y 命中该块
      var d = y < r.top ? r.top - y : y - r.bottom; if (d < bd) { bd = d; best = b; }
    }
    return best;
  }
  function _firstTextIn(el) { var w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null), n; while ((n = w.nextNode())) { if (_countable(n) && (n.nodeValue || '').trim()) return n; } return null; }
  function _lastTextIn(el) { var w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null), n, last = null; while ((n = w.nextNode())) { if (_countable(n) && (n.nodeValue || '').trim()) last = n; } return last; }
  // caret 约束:iOS 在行边界会把 caretFromPoint 解析到相邻块(典型:选第一段第一行时解析到正上方标题)→
  // 用触点实际所在块(elementFromPoint→closest 块级)夹住:caret 若落到别的块,改用触点块内靠 y 的一端。
  // caret 的竖直 rect(用 1 字符 Range 现测):判断 caretFromPoint 是不是解析到了别的行
  function _caretRect(pos) {
    if (!pos || !pos.node || pos.node.nodeType !== 3) return null;
    try {
      var len = (pos.node.nodeValue || '').length, r = document.createRange();
      r.setStart(pos.node, Math.min(pos.offset, len)); r.setEnd(pos.node, Math.min(pos.offset + 1, len));
      return r.getClientRects()[0] || r.getBoundingClientRect();
    } catch (e) { return null; }
  }
  // iOS 在行首/行边界会把 caretFromPoint 解析到相邻行(典型:选第一段第一行→caret 落到上方标题)。
  // 治本(不依赖块结构,p/div 都行):caret 竖直 rect 跟触点 y 对不上 → 朝触点方向微移 y 重取,
  // 直到 caret 落在真正含 y 的那一行;x 不变=保住水平精度。
  function _caretIn(x, y) {
    var pos = caretFromPoint(x, y);
    var cr = _caretRect(pos);
    if (cr && (y < cr.top - 4 || y > cr.bottom + 4)) {
      var dir = (y > cr.bottom) ? 1 : -1;   // caret 在触点上方→往下找;在下方→往上找
      for (var k = 1; k <= 12; k++) {
        var p2 = caretFromPoint(x, y + dir * k * 4);
        var r2 = _caretRect(p2);
        if (p2 && p2.node && p2.node.nodeType === 3 && r2 && y >= r2.top - 4 && y <= r2.bottom + 4) return p2;
      }
    }
    return pos;
  }
  function _cselStart(clientX, clientY, isMouse) {
    if (_epInk && (_epInk.mode || _epInk.drawing)) return;
    var pos = _caretIn(clientX, clientY);
    if (!pos || !pos.node || pos.node.nodeType !== 3 || !col.contains(pos.node) || !secOf(pos.node)) return;
    _cdrag = { sNode: pos.node, sOff: pos.offset, x0: clientX, y0: clientY, moved: false, dir: isMouse ? 'select' : null, isMouse: isMouse, range: null };
  }
  function _cselMove(clientX, clientY, ev) {
    var g = _cdrag; if (!g) return;
    var dx = Math.abs(clientX - g.x0), dy = Math.abs(clientY - g.y0);
    if (!g.moved && dx + dy < CSEL_TOL) return;
    if (!g.isMouse && g.dir === null) { g.dir = (dy > dx) ? 'scroll' : 'select'; if (g.dir === 'scroll') { _cdrag = null; _cselClear(); return; } }   // 触摸方向锁:竖=滚动放弃选字
    g.moved = true;
    if (ev && ev.cancelable) ev.preventDefault();   // 选字模式:拦下页面滚动
    var pos = _caretIn(clientX, clientY);
    if (!pos || !pos.node || pos.node.nodeType !== 3) return;
    var r = document.createRange();
    var sN = g.sNode, sO = g.sOff, eN = pos.node, eO = pos.offset;
    var cmp = g.sNode.compareDocumentPosition(pos.node);
    if ((cmp & Node.DOCUMENT_POSITION_PRECEDING) || (pos.node === g.sNode && pos.offset < g.sOff)) { sN = pos.node; sO = pos.offset; eN = g.sNode; eO = g.sOff; }
    try { r.setStart(sN, sO); r.setEnd(eN, eO); } catch (e) { return; }
    g.range = r; _cselPaint(r);
  }
  function _cselEnd() { var g = _cdrag; _cdrag = null; if (!g || !g.moved || !g.range) return false; return _cselApply(g.range, { snapWords: true }); }
  if (USE_CUSTOM_SEL) {
    var _csStyle = document.createElement('style'); _csStyle.textContent = '#ep-col .ep-sec{-webkit-user-select:none;user-select:none}'; (document.head || document.documentElement).appendChild(_csStyle);
    var _csExcl = 'img, mark.ep-hl, mark.ep-phrase-hl, mark.ep-word-breathe, .rc-note, .rc-fig-badge, .fav-fig-badge, a, #ep-side, #ep-sel, #rc-dict-pop, #rc-fig-pop, .rc-up-bar, .rc-up-edit, .ep-up-editbtn, .ep-usec, button, input, textarea, canvas';
    var _csExcluded = function (t) { return !!(t && t.closest && t.closest(_csExcl)); };
    content.addEventListener('touchstart', function (e) { if (e.touches.length !== 1) { _cdrag = null; return; } if (_csExcluded(e.target)) return; var t = e.touches[0]; _cselStart(t.clientX, t.clientY, false); }, { passive: true });
    content.addEventListener('touchmove', function (e) { if (!_cdrag || e.touches.length !== 1) return; var t = e.touches[0]; _cselMove(t.clientX, t.clientY, e); }, { passive: false });
    document.addEventListener('touchend', function (e) { if (!_cdrag) return; var moved = _cdrag.moved; _cselEnd(); if (moved) { if (e.cancelable) e.preventDefault(); e.stopPropagation(); } }, true);
    document.addEventListener('touchcancel', function () { _cdrag = null; }, true);
    content.addEventListener('mousedown', function (e) { if (e.button !== 0 || _csExcluded(e.target)) return; _cselStart(e.clientX, e.clientY, true); });
    document.addEventListener('mousemove', function (e) { if (_cdrag && _cdrag.isMouse) _cselMove(e.clientX, e.clientY, null); });
    document.addEventListener('mouseup', function (e) { if (!(_cdrag && _cdrag.isMouse)) return; var moved = _cdrag.moved; _cselEnd(); if (moved) e.stopPropagation(); }, true);
  }

  // ── 工具栏动作 ──
  selBar.addEventListener('click', function (e) {
    var txt = cur.text; if (!txt) return;
    var sw = e.target.closest('#ep-hl-pick i');   // 点工具栏色板 → 直接用该色高亮(照搬 PDF)
    if (sw) { hideSel(); saveHl(txt, cur.anchor, sw.dataset.c); return; }
    var b = e.target.closest('button'); if (!b) return;
    var act = b.dataset.act;
    hideSel();
    // 选区快照:结果模态的 markHighlight/ankiSource 闭包稍后才执行,cur 会被下次选中覆盖(照搬 PDF 存上下文)
    var selTxt = cur.text, selAnchor = cur.anchor, selCtx = cur.ctx;
    function resultOpts(kind) {
      return {
        kind: kind, aiParams: function () { return (window.RC && RC.settings) ? RC.settings.aiParams() : {}; },
        markHighlight: function (text, body, sentence, hkind) {
          if (!selAnchor) { toast('无法定位选区'); return false; }
          var color = localStorage.getItem('eph-hl-color') || hlColors()[0];
          reqJson('POST', '/pdf/api/epub-highlights', { file: FREL, anchor: selAnchor, text: selTxt, color: color }, function (d) { var h = d.highlight; _hls[h.id] = h; var el = _secElOf(selAnchor.section); if (el) applyHl(el, h); if (body) patchHl(h, { note: body, sentence: sentence || selCtx || '', kind: hkind || kind || 'note' }); }, function (er) { toast('高亮失败:' + er); });
          return true;
        },
        ankiSource: function () { return { file: FREL, sentence: selCtx, sourceUrl: location.origin + '/pdf/epub/view?file=' + encodeURIComponent(FREL) }; }
      };
    }
    if (act === 'copy') {
      var doT = function (ok) { toast(ok ? '已复制' : '复制失败'); };
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(txt).then(function () { doT(true); }, function () { doT(_execCopy(txt)); });
      else doT(_execCopy(txt));
    }
    else if (act === 'search') {   // 通用网页搜索:选中内容开新标签页搜(不占 AI 额度,不需要后端)
      window.open('https://www.bing.com/search?q=' + encodeURIComponent(txt), '_blank');
    }
    else if (act === 'phrase') onPhrase();   // F6 词组(照搬 PDF onPhrase):呼吸 mark + 词组浮层
    else if (act === 'dict') RC.wordpop.show({
      word: txt, rect: cur.rect, ctx: selCtx, file: FREL, langs: bookLangsArr(),
      markHighlight: function () { saveHl(txt, selAnchor, localStorage.getItem('eph-hl-color') || hlColors()[0]); },
      onMastered: function () {},
      onGrammar: function (w) { _grammarAnalyzeFrom(selCtx, w || txt); },
      // 非英日(纯中文等)→ 保留转译走结果模态
      onFallback: function (word) { RC.result.aiCall('/pdf/api/translate', { text: word, target_lang: '中文' }, '🌐 翻译', resultOpts('note')); }
    });
    else if (act === 'translate') RC.result.aiCall('/pdf/api/translate', { text: txt, target_lang: '中文' }, '🌐 翻译', resultOpts('note'));
    else if (act === 'explain') RC.result.aiCall('/pdf/api/explain', { text: txt, context: selCtx }, '💡 AI 解释', resultOpts('explain'));
    else if (act === 'grammar') _grammarAnalyzeFrom(selCtx, selTxt);   // 选区多词/句 →「📊 语法」直接分析整句(RC.grammar.extractSentence 从 selCtx 里抠出含 selTxt 的那句)
    else if (act === 'chat') { if (!(RC.ui && RC.ui.openSelectionChat && RC.ui.openSelectionChat(txt, selCtx))) RC.result.openChat(txt, selCtx, resultOpts('note')); }
    else if (act === 'note') RC.snippets.toNote(snipOpts(txt));
    else if (act === 'anki') RC.snippets.toAnki(snipOpts(txt));
  });
  // 选段→笔记/Anki:用共享层 RC.snippets(统一控制层步1);EPUB 适配器提供 file/笔记名 prompt/结果卡容器
  function snipOpts(txt) {
    return {
      text: txt, file: FREL,
      getNoteName: function () { return prompt('新笔记名(可不带 .md):', (txt || '').slice(0, 18).replace(/\s+/g, ' ')); },
      showCard: function (head, sub) { openAi(head.replace(/^[^\s]+\s+/, '')); return addCard(head, sub); }
    };
  }
  // ════════ 图进助手(主文档版,照搬 epub2-extra.js 的 attachFig/renderChips/拖拽门控,坐标直用 e.client*)════════
  //   双击图 → 带进右侧助手对话(图进助手);长按保留 iOS 原生菜单(复制/粘贴图像)。
  //   __figAttached 项 = {id,src,file_rel,caption,desc}(跨文件契约,runAssistant 读作 context.figures 随请求发后端视觉)。
  function _epAsstOpen() {
    try {
      var s = document.getElementById('ep-side'); if (!(s && s.classList.contains('open'))) return false;
      var pane = s.querySelector('.ep-side-pane[data-pane="asst"]'); return !!(pane && pane.classList.contains('active'));
    } catch (e) { return false; }
  }
  function _figSrc(im) { return (im && (im.src || im.getAttribute('src'))) || ''; }
  function attachFig(im) {
    var src = _figSrc(im); if (!src) return;
    if (!window.__figAttached) window.__figAttached = [];
    if (!window.__figAttached.some(function (a) { return a.id === src; })) {
      window.__figAttached.push({ id: src, src: src, file_rel: FREL, caption: (im.getAttribute('alt') || '').trim(), desc: (im.dataset && im.dataset.figdesc) || '' });
    }
    renderFigChips();
  }
  function renderFigChips() {
    try {
      var input = $('ep-ai-input') || $('asst-input'), list = window.__figAttached || [], wrap = $('ep-asst-fig-chips');   // ③-4b:共享侧栏输入行 id=asst-input
      if (!list.length) { if (wrap) wrap.remove(); return; }
      if (!input) return;       // 助手输入行还没建 → 列表仍在上下文里,开了再渲
      if (!wrap) { wrap = document.createElement('div'); wrap.id = 'ep-asst-fig-chips'; input.parentNode.insertBefore(wrap, input); }
      wrap.innerHTML = '';
      list.forEach(function (a) {
        var chip = document.createElement('div'); chip.className = 'ep-asst-fig-chip';
        var img = document.createElement('img'); img.src = a.src; img.alt = '';
        var cap = document.createElement('span'); cap.className = 'afc-cap'; cap.textContent = a.caption || '图';
        var x = document.createElement('button'); x.className = 'afc-x'; x.textContent = '✕';
        x.addEventListener('click', function () { window.__figAttached = (window.__figAttached || []).filter(function (z) { return z.id !== a.id; }); renderFigChips(); });
        chip.appendChild(img); chip.appendChild(cap); chip.appendChild(x); wrap.appendChild(chip);
      });
    } catch (e) {}
  }
  window.__renderFigChips = renderFigChips;   // 助手打开时补渲一次(图在开助手前点的情况)
  window.__clearFigAttached = function () { window.__figAttached = []; renderFigChips(); };

  // ── 图上手写墨迹 → 助手看合成图(照搬 PDF _figure_crop_png with_ink / 26-figures.js __figInk):
  //    墨迹按 .ep-sec 章归一化存;一张 <img> 在章内占 imgbox(归一化矩形);落在 imgbox 内的笔画随图带给后端,
  //    后端 _epub_figure_ink_png 把墨迹叠到图上。发消息时现测(画在 attach 之后也算),坐标与墨迹同为「相对 .ep-sec 归一化」。──
  function _epFindImgBySrc(src) {
    try {
      var ims = document.querySelectorAll('.ep-sec img');
      for (var i = 0; i < ims.length; i++) { if (_figSrc(ims[i]) === src) return ims[i]; }
    } catch (e) {}
    return null;
  }
  function _epImgInkMeta(im) {
    try {
      var s = secOf(im); if (!s || s.idx == null || isNaN(s.idx)) return null;
      var sr = s.el.getBoundingClientRect(), ir = im.getBoundingClientRect();
      if (!sr.width || !sr.height || !ir.width) return null;
      var box = [(ir.left - sr.left) / sr.width, (ir.top - sr.top) / sr.height,
                 (ir.right - sr.left) / sr.width, (ir.bottom - sr.top) / sr.height];   // img 在章内归一化矩形
      var strokes = (s.el.__inkStrokes) || (_epInk && _epInk.data && _epInk.data[s.idx]) || [];   // 该章墨迹(章归一化)
      var ink = [];
      for (var i = 0; i < strokes.length && ink.length < 40; i++) {
        var ps = strokes[i].p || [], inb = false;
        for (var j = 0; j < ps.length; j++) {
          if (ps[j][0] >= box[0] && ps[j][0] <= box[2] && ps[j][1] >= box[1] && ps[j][1] <= box[3]) { inb = true; break; }
        }
        if (inb) ink.push(strokes[i]);   // 坐标原样(章归一化),后端按 imgbox 换算到图内像素
      }
      return { section: s.idx, imgbox: box, ink: ink, has_ink: ink.length > 0, imgsw: Math.round(ir.width) };
    } catch (e) { return null; }
  }
  function _epCollectFigures() {
    var out = [], seen = {};
    // 1) 用户手动带入的图:发消息时现测 imgbox + 图内墨迹(画在 attach 之后也算)
    (window.__figAttached || []).forEach(function (a) {
      var f = { id: a.id, src: a.src, file_rel: a.file_rel || FREL, caption: a.caption, desc: a.desc };
      var im = _epFindImgBySrc(a.src);
      if (im) { var m = _epImgInkMeta(im); if (m) { f.section = m.section; f.imgbox = m.imgbox; f.ink = m.ink; f.has_ink = m.has_ink; f.imgsw = m.imgsw; } }
      out.push(f); seen[a.src] = 1;
    });
    // 2) 自动带入:当前视口里「图上有手写圈点」的图(用户画了圈直接问、没手动点图)——照搬 PDF「自动带当前页墨迹」的语义
    try {
      var vh = window.innerHeight || 800, ims = document.querySelectorAll('.ep-sec img');
      for (var i = 0; i < ims.length && out.length < 6; i++) {
        var im2 = ims[i], src2 = _figSrc(im2);
        if (!src2 || seen[src2]) continue;
        var r = im2.getBoundingClientRect();
        if (!r.width || r.bottom < -60 || r.top > vh + 60) continue;   // 只看当前视口附近的图
        var m2 = _epImgInkMeta(im2);
        if (m2 && m2.has_ink) {
          out.push({ id: src2, src: src2, file_rel: FREL, caption: (im2.getAttribute('alt') || '').trim(),
                     desc: (im2.dataset && im2.dataset.figdesc) || '',
                     section: m2.section, imgbox: m2.imgbox, ink: m2.ink, has_ink: true, imgsw: m2.imgsw });
          seen[src2] = 1;
        }
      }
    } catch (e) {}
    // 3) 有笔画便签(原逻辑不变:kind='note',后端 see_figure 认 note_id 现场合成)
    (window.__noteAttached || []).filter(function (n) { return n.has_ink; }).slice(0, 4).forEach(function (n) {
      if (out.length < 6) out.push({ kind: 'note', note_id: n.id, section: n.section, caption: '手写便签',
        desc: String(n.text || '').slice(0, 300), near: String(n.near || '').slice(0, 600), file_rel: FREL, has_ink: true });
    });
    // 给每张图补统一 opaque ref(设计 §8):助手/后端只透传 ref、不看 src/imgbox。note 类不加。旧字段保留兜底。
    return out.slice(0, 6).map(function (f) {
      if (f && !f.ref && f.imgbox && f.kind !== 'note') f.ref = { kind: 'epub', src: f.src, imgbox: f.imgbox, imgsw: f.imgsw };
      return f;
    });
  }
  window.__epCollectFigures = _epCollectFigures;

  // ── 焦点选区(照搬 PDF 26-figures.js __focusSel 一整套):把当前选中的公式/段落显示在输入框上方,可视 + 可 ✕ 取消。
  //   与图附件并列、同一套「发送前上下文预览」语义。公式 kind='formula'(MathJax 渲染),文字 kind='text'(片段)。──
  window.__focusSel = null;   // {text, kind}
  function _renderFocusSel() {
    try {
      var input = $('ep-ai-input') || $('asst-input');   // ③-4b:共享侧栏输入行 id=asst-input
      var wrap = $('ep-asst-sel-chip');
      var fs = window.__focusSel;
      if (!fs || !fs.text) { if (wrap) wrap.remove(); return; }
      if (!input) return;       // 助手没开 → 数据仍在,开了再渲
      if (!wrap) { wrap = document.createElement('div'); wrap.id = 'ep-asst-sel-chip'; input.parentNode.insertBefore(wrap, $('ep-asst-fig-chips') || input); }
      wrap.innerHTML = '';
      var chip = document.createElement('div'); chip.className = 'ep-asst-sel-chip-in ' + (fs.kind === 'formula' ? 'is-fml' : 'is-txt');
      var icon = document.createElement('span'); icon.className = 'asc-icon'; icon.textContent = fs.kind === 'formula' ? '🧮' : '¶';
      var body = document.createElement('span'); body.className = 'asc-body';
      if (fs.kind === 'formula') {
        var raw = fs.text.replace(/^\$+/, '').replace(/\$+$/, '');
        body.textContent = '\\(' + raw + '\\)';
      } else {
        body.textContent = fs.text.slice(0, 90) + (fs.text.length > 90 ? '…' : '');
      }
      var x = document.createElement('button'); x.className = 'asc-x'; x.textContent = '✕';
      x.addEventListener('click', function () { window.__clearFocusSel(); });
      chip.appendChild(icon); chip.appendChild(body); chip.appendChild(x); wrap.appendChild(chip);
      if (fs.kind === 'formula' && window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([body]).catch(function () {});
    } catch (e) {}
  }
  // 助手没开 → 不钉焦点入对话(选中本身仍能查词/翻译/高亮/带入 AI 结果框);开了才把选中当「现在问这个」的显式上下文
  window.__asstOpen = function () {
    try {
      var s = document.getElementById('ep-side');
      var a = document.getElementById('ep-side-asst') || document.getElementById('side-pane-asst');   // ③-4b:共享侧栏 pane(内联 pane 已摘)也认,否则 __setFocusSel 恒短路=选中 chip 永不钉
      return !!(s && s.classList.contains('open') && a && a.classList.contains('active'));
    } catch (e) { return false; }
  };
  window.__setFocusSel = function (text, kind) {
    if (!window.__asstOpen()) return;
    text = (text || '').trim();
    if (!text) { window.__clearFocusSel(); return; }
    window.__focusSel = { text: text, kind: kind || 'text' };
    _renderFocusSel();
  };
  window.__clearFocusSel = function () {
    window.__focusSel = null; _renderFocusSel();
    // ✕ = "这个上下文别再带":连隐式选中兜底(cur.text,10min 新鲜期)一起清,否则下条消息又悄悄带上(用户反馈)
    try { cur = { text: '', ctx: '', rect: null, anchor: null }; } catch (e) {}
  };
  window.__renderFocusSel = _renderFocusSel;

  // ── 便签注入(阶段3,设计见 references/sticky-notes-design.md 用户规格8):双击便签(rc-stickynote onDoubleTap,
  //   启动段接 noteInject)→ 加入 __noteAttached + 输入框上方 chip(挨图附件条,同款视觉,✕ 移除)。
  //   发送时:无笔画=文字+锚点附近正文走 context.notes 文本通道;有笔画=kind:'note' 条目并入 figures 走视觉通道
  //   (服务端 see_figure 认 note_id → pdf_reader._note_composite_png 现场合成)。chip 生命周期同图附件条:发完即清。──
  window.__noteAttached = [];   // [{id,text,near,section,has_ink,_thumb}](_thumb=合成图 data_url,仅前端显示不随请求发)
  function renderNoteChips() {
    try {
      var input = $('ep-ai-input') || $('asst-input'), list = window.__noteAttached || [], wrap = $('ep-asst-note-chips');   // ③-4b:共享侧栏输入行 id=asst-input
      if (!list.length) { if (wrap) wrap.remove(); return; }
      if (!input) return;   // 助手输入行还没建 → 数据仍在,开了再渲
      if (!wrap) {
        wrap = document.createElement('div'); wrap.id = 'ep-asst-note-chips';
        wrap.style.cssText = 'display:flex;flex-wrap:wrap;gap:6px;padding:6px 10px 0';
        input.parentNode.insertBefore(wrap, input);
      }
      wrap.innerHTML = '';
      list.forEach(function (n) {
        var chip = document.createElement('div'); chip.className = 'ep-asst-fig-chip';
        if (n.has_ink) {
          var img = document.createElement('img'); img.alt = '';
          if (n._thumb) img.src = n._thumb;
          chip.appendChild(img);
        } else {
          var ic = document.createElement('span'); ic.textContent = '🗒'; ic.style.cssText = 'flex:none;font-size:15px'; chip.appendChild(ic);
        }
        var t = String(n.text || '').replace(/\s+/g, ' ').trim();
        var cap = document.createElement('span'); cap.className = 'afc-cap';
        cap.textContent = n.has_ink ? ('手写便签' + (t ? ' · ' + t.slice(0, 14) : '')) : (t.slice(0, 20) || '便签');
        var x = document.createElement('button'); x.className = 'afc-x'; x.textContent = '✕';
        x.addEventListener('click', function () { window.__noteAttached = (window.__noteAttached || []).filter(function (z) { return z.id !== n.id; }); renderNoteChips(); });
        chip.appendChild(cap); chip.appendChild(x); wrap.appendChild(chip);
      });
    } catch (e) {}
  }
  window.__renderNoteChips = renderNoteChips;
  window.__clearNoteAttached = function () { window.__noteAttached = []; renderNoteChips(); };
  function _noteFetchThumb(entry) {
    setTimeout(function () {   // 稍等 rc-stickynote 的文字/笔画 PATCH 先落库,合成图才含最新内容
      fetch('/pdf/api/note-composite', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ file: FREL, id: entry.id }) })
        .then(function (r) { return r.json(); })
        .then(function (d) { if (d && d.ok && d.data_url) { entry._thumb = d.data_url; renderNoteChips(); } })
        .catch(function () {});
    }, 350);
  }
  // 锚点附近正文(contextAt):内容锚(v4)直接用字符偏移 off 定位;旧比例锚按 y 比例估。±600 字(章未加载 → 空)
  function _noteNearText(anchor) {
    try {
      if (!anchor || anchor.kind !== 'epub') return '';
      var sec = anchor.section;
      if (loaded[sec] !== true || !secEls[sec]) return '';
      var full = _countableText(secEls[sec]) || '';
      if (!full) return '';
      var idx = (anchor.off != null)
        ? Math.max(0, Math.min(full.length, anchor.off | 0))
        : Math.floor(full.length * Math.max(0, Math.min(1, anchor.y || 0)));
      return full.slice(Math.max(0, idx - 600), idx + 600);
    } catch (e) { return ''; }
  }
  function noteInject(note) {
    try {
      if (!note || !window.__asstOpen()) return false;   // 助手没开 → 维持现状(不注入)
      var list = window.__noteAttached = window.__noteAttached || [];
      var hasInk = !!(note.strokes && note.strokes.length);
      var old = null;
      for (var i = 0; i < list.length; i++) if (list[i].id === note.id) old = list[i];
      if (old) {   // 已在附件条 → 只刷新内容(文字/笔画可能变了),不重复加
        old.text = note.text || '';
        if (hasInk && !old.has_ink) { old.has_ink = true; old._thumb = ''; }
        if (old.has_ink) _noteFetchThumb(old);
        renderNoteChips();
        toast('已在对话上下文');
        return true;
      }
      var entry = { id: note.id, text: note.text || '', near: _noteNearText(note.anchor),
                    section: (note.anchor && note.anchor.section != null) ? note.anchor.section : -1,
                    has_ink: hasInk, _thumb: '' };
      list.push(entry);
      renderNoteChips();
      if (entry.has_ink) _noteFetchThumb(entry);
      toast('🗒 便签已带进对话');
      return true;
    } catch (e) { return false; }
  }

  var _figDrag = null, _figDragDocCleanup = null;
  function _epSideEl() { return document.getElementById('ep-side'); }
  function _overSide(x, y) {
    var s = _epSideEl(); if (!s || !s.classList.contains('open')) return false;
    var r = s.getBoundingClientRect(); if (r.width < 10) return false;
    return x >= r.left - 24 && x <= r.right + 4 && y >= r.top && y <= r.bottom;
  }
  function _figDragStart(im, e) {
    _figDragCancel();
    var g = document.createElement('img'); g.className = 'ep-fig-drag-ghost'; g.src = _figSrc(im); g.alt = '';
    g.style.left = e.clientX + 'px'; g.style.top = e.clientY + 'px';
    document.body.appendChild(g);
    _figDrag = { im: im, ghost: g, pid: (e && e.pointerId) };
    try { if (e && im.setPointerCapture) im.setPointerCapture(e.pointerId); } catch (_) {}   // 捕获指针 → 手指移出图也收得到 move/up
    // 文档级兜底:iOS 长按抢触摸时图上 pointerup/cancel 可能不触发 → 这里保证 ghost 必被清
    _figDragDocCleanup = function (ev) { if (!_figDrag) return; if (ev.type === 'pointerup') _figDragEnd(_figDrag.im, ev); else _figDragCancel(); };
    try { document.addEventListener('pointerup', _figDragDocCleanup, true); document.addEventListener('pointercancel', _figDragDocCleanup, true); } catch (_) {}
    var s = _epSideEl();
    if (s && s.classList.contains('open')) { s.classList.add('ep-fig-drop-ready'); var plus = document.createElement('div'); plus.id = 'ep-fig-drop-plus'; plus.textContent = '＋'; s.appendChild(plus); }
    if (navigator.vibrate) { try { navigator.vibrate(14); } catch (_) {} }
  }
  function _figDragMove(e) {
    if (!_figDrag) return;
    _figDrag.ghost.style.left = e.clientX + 'px'; _figDrag.ghost.style.top = e.clientY + 'px';
    var s = _epSideEl(); if (s) s.classList.toggle('ep-fig-drop-over', _overSide(e.clientX, e.clientY));
  }
  function _figDragEnd(im, e) {
    if (!_figDrag) return;   // 已结束(防图上 handler + 文档兜底双触发 → 防重复 attach)
    var over = _overSide(e.clientX, e.clientY); _figDragCancel();
    if (over) { attachFig(im); try { if (window.RC && RC.sidedrawer) RC.sidedrawer.open('asst'); } catch (_) {} renderFigChips(); toast('📷 已带进助手对话'); }
  }
  function _figDragCancel() {
    if (_figDrag) {
      try { if (_figDrag.ghost) _figDrag.ghost.remove(); } catch (_) {}
      try { if (_figDrag.pid != null && _figDrag.im && _figDrag.im.releasePointerCapture) _figDrag.im.releasePointerCapture(_figDrag.pid); } catch (_) {}
    }
    _figDrag = null;
    if (_figDragDocCleanup) { try { document.removeEventListener('pointerup', _figDragDocCleanup, true); document.removeEventListener('pointercancel', _figDragDocCleanup, true); } catch (_) {} _figDragDocCleanup = null; }
    var s = _epSideEl(); if (s) { s.classList.remove('ep-fig-drop-ready'); s.classList.remove('ep-fig-drop-over'); var pl = document.getElementById('ep-fig-drop-plus'); if (pl) pl.remove(); }
  }
  // 给「图」(宽≥半栏)绑长按拖进助手 / 轻点带入(门控照搬 epub2-extra:380ms 长按 / 8px 移动阈值 / 600ms 轻点窗口)。幂等。
  function _bindFigToAssistant(im, figMin) {
    if (!im) return;
    var fix = function () {
      if (im.dataset.epFigBound === '1') return;
      if ((im.getBoundingClientRect().width || im.naturalWidth || 0) < figMin) return;   // 行内公式/小图不绑
      im.dataset.epFigBound = '1';
      // 用户方案 2026-07-21:图长按原地=带进助手(选中);移动超阈值=当滚动放弃(松手位移分辨)。抑制 iOS 原生长按菜单。
      var _lp = null, _sx = 0, _sy = 0, _moved = false;
      try { im.style.webkitTouchCallout = 'none'; } catch (_) {}
      im.addEventListener('pointerdown', function (e) {
        if (e.pointerType === 'mouse' && e.button !== 0) return;
        _sx = e.clientX; _sy = e.clientY; _moved = false;
        _lp = setTimeout(function () {
          if (_moved) return;
          _lp = null;
          attachFig(im);
          try { if (window.RC && RC.sidedrawer) RC.sidedrawer.open('asst'); } catch (_) {}
          renderFigChips();
          toast('📷 已带进助手对话');
          if (navigator.vibrate) { try { navigator.vibrate(14); } catch (_) {} }
        }, 380);
      });
      im.addEventListener('pointermove', function (e) {
        if (!_moved && (Math.abs(e.clientX - _sx) > 10 || Math.abs(e.clientY - _sy) > 10)) { _moved = true; if (_lp) { clearTimeout(_lp); _lp = null; } }
      });
      im.addEventListener('pointerup', function () { if (_lp) { clearTimeout(_lp); _lp = null; } });
      im.addEventListener('pointercancel', function () { if (_lp) { clearTimeout(_lp); _lp = null; } });
    };
    if (im.complete) fix(); else im.addEventListener('load', fix);
    setTimeout(fix, 1300);
  }

  // ── 插图徽标:用共享层 RC.figures(统一控制层步1);EPUB 适配器只提供「找图注上下文 + 取描述」这点底座逻辑 ──
  function decorateFigures(secEl) {
    // 纵横比:只给「图」(宽度超过半个栏宽)设 height:auto 按比例缩放(行内公式宽<半栏,不动,保留书写死尺寸不被压扁)
    var figMin = Math.max(120, (col.clientWidth || 600) * 0.5);
    secEl.querySelectorAll('img').forEach(function (im) {
      var fix = function () { if ((im.getBoundingClientRect().width || im.naturalWidth || 0) >= figMin) im.style.height = 'auto'; };
      if (im.complete) fix(); else im.addEventListener('load', fix);
      setTimeout(fix, 1200);
      _bindFigToAssistant(im, figMin);   // 图进助手:长按拖进助手 / 轻点(助手开着)带入
    });
    // 收藏夹 PDF 整页图(.fav-pdf-img)不当作一张图盖徽标——它的图徽标走 per-figure(见 _favPdfBadges)。其余真图照常 decorate。
    var _figOpts = {
      minWidth: figMin,
      getContext: figContext,
      getCached: function (im) { return im.dataset.figdesc != null ? im.dataset.figdesc : null; },
      setCached: function (im, desc) { im.dataset.figdesc = desc || ''; },
      describe: function (im, ctx) {
        var m = (im.getAttribute('src') || '').match(/\/pdf\/epub\/file\/([a-z0-9]+)\/(.+)$/);
        if (!m) return Promise.reject('无效图');
        return RC.reqJson('POST', '/pdf/api/epub-img-describe', { sha: m[1], path: decodeURIComponent(m[2]), caption: ctx.caption, context: ctx.context })
          .then(function (d) { if (!d || !d.ok) throw ((d && d.error) || '失败'); return d.desc || ''; });
      }
    };
    if (window.RC && RC.figures && RC.figures.attach) {
      secEl.querySelectorAll('img').forEach(function (im) { if (!im.classList.contains('fav-pdf-img')) RC.figures.attach(im, _figOpts); });
    }
    secEl.querySelectorAll('.fav-pdf-page[data-favpdf-file]').forEach(_favPdfBadges);
  }
  // 收藏夹 PDF 页:按原书 page-figures 的图数据(bbox/desc/badge)在真图上渲染徽标——复用原本判定+内容(替代整页盖一个右上角徽标)。
  function _favPdfBadges(page) {
    if (page.__favFigDone) return; page.__favFigDone = 1;
    var file = page.getAttribute('data-favpdf-file'), pno = page.getAttribute('data-favpdf-page');
    if (!file || !pno || !(window.RC && RC.reqJson)) return;
    RC.reqJson('GET', '/pdf/api/page-figures?file=' + encodeURIComponent(file) + '&page=' + encodeURIComponent(pno)).then(function (d) {
      if (!d || !d.ok || !d.figures || !d.figures.length) return;
      d.figures.forEach(function (f) {
        var b = f.badge; if (!b || b.length < 2) return;
        var el = document.createElement('div'); el.className = 'fav-fig-badge';
        el.innerHTML = (RC.figures && RC.figures.PHOTO_SVG) ? RC.figures.PHOTO_SVG : '📷';
        el.style.left = (b[0] * 100).toFixed(2) + '%'; el.style.top = (b[1] * 100).toFixed(2) + '%';
        el.title = f.caption || '图说明';
        el.addEventListener('click', function (e) {
          e.stopPropagation();
          var body = RC.md ? RC.md(f.desc || '') : (f.desc || '');
          if (RC.figures && RC.figures.openPop) RC.figures.openPop(el, f.caption || '图', body, { ignoreSelector: '.fav-fig-badge' });   // 传 ignoreSelector:再点同徽标=切换关(否则点外关先关、click 又重开,永远关不掉)
        });
        page.appendChild(el);
      });
    }).catch(function () {});
  }
  function figContext(im) {
    var blk = im.closest('p,div,figure,li') || im.parentElement;
    var caption = (im.getAttribute('alt') || '').trim();
    var nb = blk && blk.nextElementSibling;
    if (nb) { var t = (nb.textContent || '').trim(); if (t && (t.length < 60 || /^(图|圖|fig|figure)/i.test(t))) caption = caption || t; }
    var parts = [];
    var pv = blk && blk.previousElementSibling; if (pv) parts.push((pv.textContent || '').trim());
    var nx = (nb && nb.nextElementSibling) || nb; if (nx) parts.push((nx.textContent || '').trim());
    return { caption: caption.slice(0, 200), context: parts.filter(Boolean).join('\n').slice(0, 1500) };
  }

  // 字典:走共享层 RC.wordpop(工具栏 dict 分支 / 单击选词都调 RC.wordpop.show;旧 RC.dict 轻框已退役删除)

  // ── AI 侧栏 ──
  function openAi(t) { if (window.RC && RC.sidedrawer) RC.sidedrawer.open('asst'); }
  function closeAi() { if (window.RC && RC.sidedrawer) RC.sidedrawer.close(); }
  function addCard(head, sub) {
    // 5V：工具输出统一走语音对话已有的三态工具卡；普通 AI 文本仍由共享助手渲染成气泡。
    var label = String(head || '工具结果').replace(/<[^>]+>/g, '');
    if (sub) label += ' · ' + String(sub).slice(0, 40) + (String(sub).length > 40 ? '…' : '');
    var body = (window.RC && RC.ui && RC.ui.appendToolCard) ? RC.ui.appendToolCard(_asstBody(), { label: label, type: '#b9a8ff', form: 'full' }) : null;
    if (!body) { var card = document.createElement('div'); card.className = 'ep-card'; card.innerHTML = '<div class="h">' + head + (sub ? '<span class="ep-sel-chip">' + esc(sub.slice(0, 40)) + (sub.length > 40 ? '…' : '') + '</span>' : '') + '</div><div class="c"><span class="ep-spin"></span></div>'; _asstBody().appendChild(card); body = card.querySelector('.c'); }
    _asstBody().scrollTop = _asstBody().scrollHeight; return body;
  }
  function curChapText() { var topIdx = 0; for (var i = 0; i < secEls.length; i++) { if (secEls[i].getBoundingClientRect().bottom > 60) { topIdx = i; break; } } var el = secEls[topIdx]; return el ? (el.innerText || '').slice(0, 4000) : ''; }
  function chapLabelOf(idx) { var lab = ''; for (var i = 0; i < TOC.length; i++) { if (TOC[i].idx <= idx) lab = TOC[i].label; else break; } return lab; }
  // ============================================================================
  // Phase H:EPUB 助手 = agentic Copilot(会调工具 + 有记忆)。
  // 接 /pdf/api/epub-assistant(SSE,detached worker + rid 重连)+ /pdf/api/epub-convo(历史)。
  // 保留:上下文卡 / 追问 chips / 停止按钮 / mic / 底部快捷。新增:工具循环 / 高亮动作 / 跳章 / 撤销 / trace。
  // ============================================================================

  // ── 自包含 CSS(tool spinner 行 / notice 横幅 / undo·task 提示条 / trace 折叠 / 章节链接)──
  (function injectAsstCss() {
    if (document.getElementById('ep-asst-extra-css')) return;
    var s = document.createElement('style'); s.id = 'ep-asst-extra-css';
    s.textContent =
      '.ep-asst-tool{color:#7c93c4;font-size:12.5px;font-style:italic;display:inline-flex;align-items:center;gap:6px}' +
      '.ep-asst-note{align-self:center;background:#2a2410;border:1px solid #5a4a18;color:#e7d28a;font-size:12px;padding:4px 10px;border-radius:9px;max-width:96%;line-height:1.5}' +
      '.ep-asst-undo{background:#3a1d2a;border:1px solid #6b3550;color:#ffd0e0;border-radius:7px;padding:2px 8px;font-size:12px;cursor:pointer;margin-left:6px}' +
      '.ep-asst-undo:active{background:#52283a}.ep-asst-undo:disabled{opacity:.5}' +
      '.ep-asst-jump{background:#16293a;border:1px solid #2a4a63;color:#bce0ff;border-radius:7px;padding:2px 8px;font-size:12px;cursor:pointer;margin-left:6px}' +
      '.ep-asst-jump:active{background:#1d3a52}' +
      '.ep-edit-card{background:#11233a;border:1px solid #2a4a63}' +
      '.ep-edit-h{font-size:13px;color:#cfe6ff;margin-bottom:6px}' +
      '.ep-edit-row{display:flex;gap:6px;flex-wrap:wrap}' +
      '.ep-edit-undo{background:#26344f;border:1px solid #3a5273;color:#dbe7ff;border-radius:8px;padding:3px 12px;font-size:12.5px;cursor:pointer}' +
      '.ep-edit-undo:active{background:#2f4061}.ep-edit-undo:disabled{opacity:.55}' +
      '.ep-chaplink{color:#7dd3fc;cursor:pointer;text-decoration:underline dotted;text-underline-offset:2px}' +
      '.ep-chaplink:active{opacity:.7}' +
      // ③ 上下文卡「选中」跳转后的临时呼吸高亮(splitText+mark 同 ep-phrase-hl 机制,独立 class 几秒后移除)
      'mark.ep-ctx-flash{background:rgba(59,109,181,.42);border:1px solid rgba(111,211,255,.85);border-radius:3px;color:inherit;padding:0;' +
        '-webkit-box-decoration-break:clone;box-decoration-break:clone;animation:epCtxFlash 1.1s ease-in-out infinite}' +
      '@keyframes epCtxFlash{0%,100%{opacity:.55}50%{opacity:.95}}' +
      // 「!」反馈条 + 弹窗:逐字照搬 PDF reader.src/25-assistant.js 的 .asst-fb-*/.afp-*
      //   (前缀改 ep- 防撞,数值/视觉与 PDF 完全一致)
      '.ep-fb-bar{position:relative;margin-top:7px;display:flex;justify-content:flex-end;align-items:center}' +
      '.ep-fb-tok{margin-right:auto;font-size:11px;color:#6f7fa3;background:#121a2e;border:1px solid #233156;border-radius:8px;padding:1px 7px}' +
      '.ep-fb-btn{width:22px;height:22px;line-height:20px;text-align:center;border-radius:50%;border:1px solid #2a3a63;background:#0e1525;color:#7c93c4;font-size:13px;font-weight:700;cursor:pointer;padding:0;-webkit-tap-highlight-color:transparent}' +
      '.ep-fb-btn:active{background:#1a2540}' +
      '.ep-fb-pop{position:absolute;right:0;bottom:28px;z-index:20;width:320px;max-width:88vw;background:#0d1426;border:1px solid #2a3a63;border-radius:11px;padding:9px;box-shadow:0 8px 22px rgba(0,0,0,.5);display:flex;flex-direction:column;gap:5px}' +
      '.ep-afp-l-btn{cursor:pointer;text-decoration:underline dotted;text-underline-offset:2px;-webkit-tap-highlight-color:transparent}' +
      '.ep-afp-l-btn:active{opacity:.7}' +
      '.ep-afp-detail{white-space:pre-wrap;word-break:break-word;max-height:260px;overflow:auto;background:#0a1020;border:1px solid #233156;border-radius:8px;padding:8px 10px;margin:2px 0 4px;font-size:11.5px;color:#bcd0ee;line-height:1.55;-webkit-overflow-scrolling:touch}' +
      '.ep-afp-h{font-size:11px;color:#7c93c4;margin-bottom:2px}' +
      '.ep-afp-step{display:flex;align-items:center;gap:7px;font-size:12px;line-height:1.5}' +
      '.ep-afp-l{color:#cdd9f2;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1 1 auto;min-width:0}' +
      '.ep-afp-m{color:#7c93c4;flex:none;font-variant-numeric:tabular-nums}' +
      '.ep-afp-gear-btn{flex:none;background:none;border:none;color:#6b7da0;font-size:13px;cursor:pointer;padding:0 1px;-webkit-tap-highlight-color:transparent}' +
      '.ep-afp-gear-btn:active{color:#bcd0ff}' +
      '.ep-afp-gear{display:flex;flex-wrap:wrap;align-items:center;gap:5px;margin:1px 0 5px;padding:7px;background:#0a1322;border:1px solid #243152;border-radius:8px}' +
      '.ep-afp-glab{font-size:11px;color:#7c93c4;width:100%}' +
      '.ep-afp-sel{background:#0d1426;border:1px solid #2a3a63;color:#dbe7ff;border-radius:6px;padding:3px 5px;font-size:12px;flex:1 1 42%;min-width:0}' +
      '.ep-afp-gset{background:#16293a;border:1px solid #2a4a63;color:#bce0ff;border-radius:6px;padding:4px 9px;font-size:12px;cursor:pointer;flex:1 1 auto}' +
      '.ep-afp-gdef{background:#1a2233;border:1px solid #2a3a63;color:#9fb4e0;border-radius:6px;padding:4px 9px;font-size:12px;cursor:pointer;flex:none}' +
      '.ep-afp-foot{font-size:11px;color:#6b7da0;margin-top:5px;text-align:right;font-variant-numeric:tabular-nums}' +
      '.ep-afp-acts{display:flex;flex-direction:column;gap:5px;margin-top:4px;border-top:1px solid #1d2742;padding-top:7px}' +
      '.ep-afp-act{text-align:left;border:1px solid #2a3a63;border-radius:8px;padding:6px 9px;font-size:12px;cursor:pointer;color:#dbe7ff}' +
      '.ep-afp-q{background:#16293a;border-color:#2a4a63}.ep-afp-q:active{background:#1d3a52}' +
      '.ep-afp-s{background:#1a2233}.ep-afp-s:active{background:#222d44}' +
      // ── 写操作「撤销/重做」持久卡(系统自动生成,随对话持久化)── 逐字照搬 epub2-assist.js 的 .ep-act-*
      '.ep-act-card{background:#0f1f17;border:1px solid #2a5a3e}' +
      '.ep-act-card.ep-act-undone{background:#231b22;border-color:#5a3550}' +
      '.ep-act-h{font-size:13px;color:#bfead0;margin-bottom:6px;display:flex;align-items:center;gap:6px}' +
      '.ep-act-card.ep-act-undone .ep-act-h{color:#e7c6dd;text-decoration:line-through;opacity:.85}' +
      '.ep-act-row{display:flex;gap:6px;flex-wrap:wrap}' +
      '.ep-act-btn{border-radius:8px;padding:3px 12px;font-size:12.5px;cursor:pointer;font-family:inherit}' +
      '.ep-act-detail-btn{background:#13233f;border:1px solid #2a3a63;color:#bcd0ff}.ep-act-detail-btn:active{background:#1d3358}' +
      '.ep-act-undo{background:#26344f;border:1px solid #3a5273;color:#dbe7ff}.ep-act-undo:active{background:#2f4061}' +
      '.ep-act-redo{background:#1d3a2a;border:1px solid #2f6347;color:#bfead0}.ep-act-redo:active{background:#244a35}' +
      '.ep-act-btn:disabled{opacity:.42;cursor:default}' +
      '.ep-act-detail-box{margin-top:7px;white-space:pre-wrap;word-break:break-word;max-height:240px;overflow:auto;background:#0a1020;border:1px solid #233156;border-radius:8px;padding:7px 9px;font-size:11.5px;color:#bcd0ee;line-height:1.55;-webkit-overflow-scrolling:touch}' +
      // ── 逐条高亮列表(auto_highlight 标完 / find_highlights 列出)= showHlPicker 的卡片 ── 照搬 epub2-assist .ep-hl-*
      '.ep-hl-pick-h{font-size:12.5px;color:#cfe6ff;opacity:.9;margin-bottom:5px}' +
      '.ep-hl-row{display:flex;align-items:center;gap:7px;padding:4px 0;border-top:1px solid #1d2742}' +
      '.ep-hl-row:first-of-type{border-top:none}' +
      '.ep-hl-sw{flex:none;width:13px;height:13px;border-radius:3px;border:1px solid rgba(255,255,255,.3)}' +
      '.ep-hl-tx{flex:1 1 auto;min-width:0;font-size:12px;color:#dbe7ff;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
      '.ep-hl-del{flex:none;background:#3a1d2a;border:1px solid #6b3550;color:#ffd0e0;border-radius:7px;padding:2px 8px;font-size:12px;cursor:pointer}' +
      '.ep-hl-del:active{background:#52283a}.ep-hl-del:disabled{opacity:.5}' +
      // 删完转「↪ 重做」(用存下的偏移锚 + 色 + 原文重建);删除⇄重做单按钮互斥,对齐别的写操作卡
      '.ep-hl-redo{flex:none;background:#1d3a2a;border:1px solid #2f6347;color:#bfead0;border-radius:7px;padding:2px 8px;font-size:12px;cursor:pointer}' +
      '.ep-hl-redo:active{background:#244a35}.ep-hl-redo:disabled{opacity:.5}' +
      // ── 图进助手:附件条 + 长按拖拽 ghost(主文档版,照搬 epub2-extra.js 的 #ep-asst-fig-chips / .ep-fig-drag-ghost)──
      '#ep-asst-fig-chips{display:flex;flex-wrap:wrap;gap:6px;padding:6px 10px 0}' +
      '.ep-asst-fig-chip{display:flex;align-items:center;gap:6px;padding:4px 6px;background:#16203a;border:1px solid #2a3a63;border-radius:9px;max-width:100%}' +
      '.ep-asst-fig-chip img{width:38px;height:38px;object-fit:cover;border-radius:5px;border:1px solid #3b6db5;background:#fff;flex:none}' +
      '.ep-asst-fig-chip .afc-cap{font-size:11px;color:#cfe6ff;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:130px}' +
      '.ep-asst-fig-chip .afc-x{background:transparent;border:none;color:#9ab;font-size:13px;cursor:pointer;flex:none;padding:0 2px}' +
      // 焦点选区 chip(公式/段落,照搬 PDF #asst-sel-chip)
      '#ep-asst-sel-chip{padding:4px 10px 0}' +
      '.ep-asst-sel-chip-in{display:flex;align-items:center;gap:7px;padding:5px 8px;background:#101a30;border:1px solid #2f4a7d;border-radius:9px;max-width:100%}' +
      '.ep-asst-sel-chip-in.is-fml{border-color:#3b6db5}' +
      '.ep-asst-sel-chip-in .asc-icon{flex:none;font-size:14px}' +
      '.ep-asst-sel-chip-in .asc-body{flex:1 1 auto;min-width:0;font-size:12px;color:#dbe7ff;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
      '.ep-asst-sel-chip-in.is-fml .asc-body{color:#eaf2ff;white-space:normal;max-height:46px;overflow:auto}' +
      '.ep-asst-sel-chip-in .asc-x{flex:none;background:transparent;border:none;color:#9ab;font-size:13px;cursor:pointer;padding:0 2px}' +
      '.ep-fig-drag-ghost{position:fixed;z-index:240;width:118px;max-height:150px;object-fit:contain;opacity:.6;border:2px solid rgba(10,132,255,.85);border-radius:9px;box-shadow:0 10px 28px rgba(0,0,0,.55);transform:translate(-50%,-50%);pointer-events:none;background:#fff}' +
      '#ep-side.ep-fig-drop-ready{outline:2px dashed rgba(10,132,255,.5);outline-offset:-4px}' +
      '#ep-side.ep-fig-drop-over{outline:3px solid rgba(10,132,255,.95);background:rgba(10,132,255,.07)}' +
      '#ep-fig-drop-plus{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);z-index:131;font-size:64px;font-weight:300;color:rgba(10,132,255,.6);pointer-events:none;text-shadow:0 2px 8px rgba(0,0,0,.4)}';
    document.head.appendChild(s);
  })();

  // ── runActions:后端 actions 事件 → 调 window 上的动作函数(未知 fn 安全忽略,不抛)──
  function runActions(actions) {
    if (!actions || !actions.length) return;
    actions.forEach(function (a) {
      try { if (a && a.fn && typeof window[a.fn] === 'function') window[a.fn].apply(null, a.args || []); } catch (e) {}
    });
  }
  window.jumpTo = jumpTo;   // {fn:"jumpTo",args:[idx]}
  window.notesReload = function () {   // {fn:"notesReload"}:AI 建/改便签、撤销/重做后重挂页面便签(loadAll 幂等全量)
    try { if (window.RC && RC.stickynote && RC.stickynote.loadAll) RC.stickynote.loadAll(); } catch (e) {}
  };
  window.openBookAt = function (fileRel, page) {   // {fn:"openBookAt",args:[file_rel,page]} 跨书跳转
    try {
      if (!fileRel) return;
      var url = /\.epub$/i.test(fileRel)
        ? ('/pdf/epub/view?file=' + encodeURIComponent(fileRel) + (page != null ? '&page=' + encodeURIComponent(page) : ''))
        : ('/pdf/view?file=' + encodeURIComponent(fileRel) + (page ? '&page=' + page : ''));
      location.href = url;
    } catch (e) {}
  };

  // ── window.epubHighlight:{section,texts:[...],color} → 在该章 DOM 按文本定位 → 算偏移锚 → 调 saveHl ──
  function _sectionRawText(secEl) {   // 拼该章所有 text 节点(偏移空间跟 offsetOf/applyHl 一致)
    var w = document.createTreeWalker(secEl, NodeFilter.SHOW_TEXT, null), n, raw = '';
    while ((n = w.nextNode())) raw += n.nodeValue;
    return raw;
  }
  function _findOffset(raw, query) {   // 先精确找,再空白归一化容错(提取文本 vs DOM 原始空白常不同)
    query = String(query || '');
    var i = raw.indexOf(query);
    if (i >= 0) return { start: i, end: i + query.length };
    var q = query.replace(/\s+/g, ' ').trim();
    if (!q) return null;
    var comp = '', map = [], prevSp = false;   // 折叠连续空白 + 记 comp→raw 偏移映射
    for (var k = 0; k < raw.length; k++) {
      var c = raw[k];
      if (/\s/.test(c)) { if (prevSp) continue; comp += ' '; map.push(k); prevSp = true; }
      else { comp += c; map.push(k); prevSp = false; }
    }
    var j = comp.indexOf(q);
    if (j < 0) return null;
    var s = map[j], lastM = j + q.length - 1;
    return { start: s, end: lastM < map.length ? map[lastM] + 1 : raw.length };
  }
  function _findUniqueOffset(raw, query) {
    query = String(query || '');
    if (!query.trim()) throw new Error('BW_READER_SOURCE_TEXT_EMPTY');
    var first = raw.indexOf(query);
    if (first >= 0) {
      if (raw.indexOf(query, first + 1) >= 0) throw new Error('BW_READER_SOURCE_TEXT_AMBIGUOUS');
      return { start: first, end: first + query.length };
    }
    var q = query.replace(/\s+/g, ' ').trim();
    var comp = '', map = [], prevSp = false;
    for (var k = 0; k < raw.length; k++) {
      var c = raw[k];
      if (/\s/.test(c)) {
        if (prevSp) continue;
        comp += ' '; map.push(k); prevSp = true;
      } else {
        comp += c; map.push(k); prevSp = false;
      }
    }
    var lead = comp.length - comp.replace(/^\s+/, '').length;
    comp = comp.trim(); map = map.slice(lead, lead + comp.length);
    var pos = comp.indexOf(q);
    if (pos < 0) throw new Error('BW_READER_SOURCE_TEXT_NOT_FOUND');
    if (comp.indexOf(q, pos + 1) >= 0) throw new Error('BW_READER_SOURCE_TEXT_AMBIGUOUS');
    var rawStart = map[pos], rawEndAt = map[pos + q.length - 1];
    if (!Number.isInteger(rawStart) || !Number.isInteger(rawEndAt)) throw new Error('BW_READER_SOURCE_TEXT_RANGE_INVALID');
    return { start: rawStart, end: rawEndAt + 1 };
  }
  // 确保某 section 已渲染(没渲染就 loadSection 再轮询)→ resolve(loaded?)。
  function _ensureLoaded(section) {
    return new Promise(function (resolve) {
      if (loaded[section] === true) { resolve(true); return; }
      try { loadSection(section); } catch (e) {}
      var tries = 0;
      (function wait() {
        if (loaded[section] === true) { resolve(true); return; }
        if (tries++ > 30) { resolve(loaded[section] === true); return; }
        setTimeout(wait, 120);
      })();
    });
  }
  // 在某 section 里按文本定位 → POST 偏移锚高亮 → resolve(created[{id,section,text,anchor,color}])
  function _markGroup(section, texts, color) {
    return _ensureLoaded(section).then(function (ok) {
      var el = secEls[section];
      if (!ok || !el) { dbg('epubHighlight: 章节 ' + section + ' 未加载'); return []; }
      var raw = _sectionRawText(el), anchors = [];
      texts.forEach(function (t) {
        t = String(t || '').trim(); if (!t) return;
        var off = _findOffset(raw, t);
        if (!off || off.end <= off.start) { dbg('epubHighlight 定位失败:' + t.slice(0, 14)); return; }
        anchors.push({ text: t, anchor: { section: section, start: off.start, end: off.end } });
      });
      if (!anchors.length) return [];
      return Promise.all(anchors.map(function (a) {
        return new Promise(function (res) {
          reqJson('POST', '/pdf/api/epub-highlights', { file: FREL, anchor: a.anchor, text: a.text, color: color },
            function (d) { var h = d.highlight; _hls[h.id] = h; var el2 = secEls[section]; if (el2) applyHl(el2, h); res({ id: h.id, section: section, text: a.text, anchor: a.anchor, color: color }); },
            function () { res(null); });
        });
      })).then(function (rs) { return rs.filter(Boolean); });
    });
  }
  // 统一入口:单 section(手动 epub_highlight,{section,texts})/ 多 section(整章 auto_highlight,{sections:[{section,texts}],picker:true})。
  //   多 section **串行**逐章定位画高亮,完成后:picker → showHlPicker 逐条跳转/删除列表;否则 → _epAssistEdit 会话撤销⇄重做卡。
  function _epubHighlightTransaction(arg) {
    return new Promise(function (resolve, reject) {
      try {
        if (!arg) throw new Error('缺少高亮参数');
        var color = arg.color || localStorage.getItem('eph-hl-color') || hlColors()[0];
        var groups;
        if (Array.isArray(arg.sections)) {
          groups = arg.sections.map(function (g) {
            var ts = (g.texts || []).map(function (t) { return String(t || '').trim(); }).filter(Boolean);
            var s = parseInt(g.section, 10); if (isNaN(s)) s = _curTopIdx;
            return { section: s, texts: ts };
          }).filter(function (g) { return g.texts.length; });
        } else {
          var ts0 = (arg.texts || (arg.text ? [arg.text] : [])).map(function (t) { return String(t || '').trim(); }).filter(Boolean);
          if (!ts0.length) throw new Error('没有可定位的高亮原文');
          var s0 = parseInt(arg.section, 10); if (isNaN(s0)) s0 = _curTopIdx;
          groups = [{ section: s0, texts: ts0 }];
        }
        if (!groups.length) throw new Error('没有可执行的高亮分组');
        var wantPicker = !!arg.picker, firstSection = groups[0].section, allCreated = [];
        (function runGroup(i) {
          if (i >= groups.length) {
            if (groups.length > 1) { try { jumpTo(firstSection, false); } catch (e) {} }   // 收尾回到整章起点
            if (!allCreated.length) { reject(new Error('没有在正文中定位到高亮原文')); return; }
            if (window.__BW_NATIVE_LOCAL_READER__) {
              var action = {
                id: 'act_nehl_' + Date.now() + '_' + Math.random().toString(16).slice(2, 8),
                kind: wantPicker ? 'auto_highlight' : 'epub_highlight',
                title: (wantPicker ? '自动标重点:' : '高亮:') + allCreated.length + '处',
                detail: allCreated.map(function (item) { return '· ' + String(item.text || '').slice(0, 120); }).join('\n'),
                undo: { op: 'hl_delete', file: FREL, ids: allCreated.map(function (item) { return item.id; }) },
                redo: { op: 'hl_create', file: FREL, items: allCreated },
                state: 'done', ts: Math.floor(Date.now() / 1000)
              };
              _epAttachActions([action]).then(function () {
                _epShowAction(action);
                if (wantPicker) { try { window.showHlPicker({ items: allCreated }); } catch (e) {} }
                resolve({ action: action, items: allCreated });
              }).catch(function (error) {
                allCreated.forEach(function (item) {
                  try { unapplyHl(item); delete _hls[item.id]; } catch (_) {}
                });
                toast('高亮未保存:' + String(error && error.message || error));
                reject(error);
              });
              return;
            }
            if (wantPicker) { try { window.showHlPicker({ items: allCreated }); } catch (e) {} }   // 逐条跳转/删除
            else { try { _epAssistEdit({ section: firstSection, items: allCreated }); } catch (e) {} }   // 会话撤销⇄重做卡
            resolve({ items: allCreated });
            return;
          }
          var g = groups[i];
          _markGroup(g.section, g.texts, color).then(function (created) {
            allCreated = allCreated.concat(created || []); runGroup(i + 1);
          }).catch(reject);
        })(0);
      } catch (e) {
        dbg('epubHighlight err:' + (e && e.message));
        reject(e);
      }
    });
  }
  // Keep the established fire-and-forget UI hook while exposing a promise
  // specifically for native chat/voice write-before-success interception.
  window.nativeLocalEPUBHighlight = _epubHighlightTransaction;
  window.epubHighlight = function (arg) {
    var task = _epubHighlightTransaction(arg);
    task.catch(function () {});   // old action dispatchers intentionally ignore returns
    return task;
  };

  function _epubExactSource(request) {
    request = request || {};
    if (request.file !== FREL) return Promise.reject(new Error('BW_READER_SOURCE_WRONG_BOOK'));
    if (!request.target || request.target.kind !== 'epub') return Promise.reject(new Error('BW_READER_SOURCE_TARGET_KIND'));
    var section = Number(request.target.section);
    if (!Number.isInteger(section) || section < 0 || section >= COUNT) return Promise.reject(new Error('BW_READER_SOURCE_SECTION_INVALID'));
    return _ensureLoaded(section).then(function (ok) {
      var el = secEls[section];
      if (!ok || !el) throw new Error('BW_READER_SOURCE_SECTION_UNAVAILABLE');
      var offset = _findUniqueOffset(_sectionRawText(el), request.sourceText != null ? request.sourceText : request.text);
      return { section: section, offset: offset, element: el };
    });
  }

  window.__bwReaderValidateExactSource = function (request) {
    return _epubExactSource(request).then(function (found) {
      return { ok: true, section: found.section, start: found.offset.start, end: found.offset.end };
    });
  };

  window.__bwReaderHighlightExactText = function (request) {
    request = request || {};
    var colors = { yellow:'#fff59d', green:'#a7f3d0', blue:'#a3d4ff', pink:'#fda4af' };
    if (!colors[request.color]) return Promise.reject(new Error('BW_READER_HIGHLIGHT_COLOR_INVALID'));
    if (!/^c_[a-f0-9]{8,32}$/.test(request.mutationId || '')) return Promise.reject(new Error('BW_READER_HIGHLIGHT_MUTATION_ID'));
    return _epubExactSource(request).then(function (found) {
      var body = {
        file: FREL,
        id: request.mutationId,
        anchor: { section: found.section, start: found.offset.start, end: found.offset.end },
        text: String(request.text || '').trim(),
        color: colors[request.color],
        note: request.note || '',
        kind: 'note'
      };
      return new Promise(function (resolve, reject) {
        reqJson('POST', '/pdf/api/epub-highlights', body, function (data) {
          var h = data.highlight;
          if (!h || !h.id) { reject(new Error('BW_READER_HIGHLIGHT_SAVE_INVALID')); return; }
          _hls[h.id] = h;
          if (found.element) applyHl(found.element, h);
          resolve({ ok: true, status: 'highlight_saved', id: h.id, section: found.section, text: h.text });
        }, function (error) { reject(new Error('BW_READER_HIGHLIGHT_SAVE_REJECTED:' + error)); });
      });
    });
  };

  // ── 「第N章」→ section idx 换算(④ off-by-one 修复)──
  // 根因:以前把 N **直接当 0 基 section idx** 跳(dataset.idx=N → jumpTo(N)),但用户读到的「第N章」
  // 是**人类章号**——EPUB spine 里封面/目次等前付也各占 section,章号≠idx(如 第1章 在 idx 2 时,
  // 点「第3章」跳 idx 3 = 第2章正文,正是「跳到 X-1」);就算 1:1 对应也还差 0 基那个 1。
  // 现在换算规则:① 优先按 TOC label 匹配 —— 目录里找标题含「第N章」(全角/汉数字也认)的项,取它的
  // idx(这才是用户语义下的真实目标);② TOC 没有能匹配的 label(目录用外文标题等)才退回
  // 旧约定:N 当 0 基 section idx(系统提示让 AI 按工具返回的 idx 写「(第N章)」,此时这是唯一语义)。
  // 返回 {idx, viaLabel}:viaLabel=true 走①(AI 写的就是人类章名,显示原样);false 走②(显示的
  // 「第N章」是 spine idx 语义,人类看是错的 → linkify 时显示层替换成该 idx 的目录章名,跳转不变)。
  function _kanjiToNum(s) {   // 汉数字 → int(支持 十/百/千 组合 与 一〇三 位记两种写法;非法 → NaN)
    if (!s) return NaN;
    var digs = { '〇': 0, '零': 0, '一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9 };
    var units = { '十': 10, '百': 100, '千': 1000 };
    var total = 0, cur = 0, hasUnit = false, i, ch;
    for (i = 0; i < s.length; i++) { if (units[s.charAt(i)] != null) { hasUnit = true; break; } }
    if (!hasUnit) {
      for (i = 0; i < s.length; i++) { ch = digs[s.charAt(i)]; if (ch == null) return NaN; total = total * 10 + ch; }
      return total;
    }
    for (i = 0; i < s.length; i++) {
      ch = s.charAt(i);
      if (digs[ch] != null) cur = digs[ch];
      else if (units[ch] != null) { total += (cur || 1) * units[ch]; cur = 0; }
      else return NaN;
    }
    return total + cur;
  }
  function _numToKanji(n) {   // int → 汉数字(十/百/千 组合,12→十二;超范围 → '' 不参与匹配)
    if (!(n > 0) || n > 3999) return '';
    var digs = '〇一二三四五六七八九', out = '', us = [[1000, '千'], [100, '百'], [10, '十']];
    for (var i = 0; i < us.length; i++) {
      var d = Math.floor(n / us[i][0]) % 10;
      if (d) out += ((d === 1 && us[i][0] === 10 && !out) ? '' : digs.charAt(d)) + us[i][1];
    }
    if (n % 10) out += digs.charAt(n % 10);
    return out;
  }
  function _chapTarget(n) {
    if (isNaN(n) || n < 0) return null;
    var zen = String(n).replace(/[0-9]/g, function (d) { return '０１２３４５６７８９'.charAt(+d); });
    var kan = _numToKanji(n);
    var re2 = new RegExp('第\\s*(?:' + n + '|' + zen + (kan ? '|' + kan : '') + ')\\s*[章節节]');
    for (var i = 0; i < TOC.length; i++) {
      if (TOC[i] && TOC[i].idx != null && re2.test(String(TOC[i].label || ''))) return { idx: TOC[i].idx, viaLabel: true };
    }
    return (n < COUNT) ? { idx: n, viaLabel: false } : null;   // 兜底:当 section idx(超范围则不做链接)
  }
  // ── (第N章[ 章名])→ 可点链接(目标 idx 在 linkify 时经 _chapTarget 换算好,存 dataset.idx)──
  // ① 支持三种 AI 写法:(第3章) 阿拉伯 /(第３章)全角 /(第一章 原子の運動)汉数字+目录章名原文
  //   (服务端 prompt 现在教 AI 优先照抄目录章名);兜底路径(N=spine idx)显示替换为目录章名,跳转照旧。
  function linkifyChapters(el) {
    try {
      var NUM = '\\d{1,4}|[０-９]{1,4}|[〇零一二三四五六七八九十百千]{1,6}';
      var re = new RegExp('[（(]\\s*第\\s*(' + NUM + ')\\s*[章節节]([^()（）]{0,40}?)\\s*[)）]', 'g');
      var pre = new RegExp('第\\s*(?:' + NUM + ')\\s*[章節节]');
      var nodes = [], w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null), nd;
      while ((nd = w.nextNode())) {
        if (nd.nodeValue && pre.test(nd.nodeValue) && nd.parentNode &&
            !(nd.parentNode.closest && nd.parentNode.closest('a,button,.ep-chaplink,code,pre'))) nodes.push(nd);
      }
      nodes.forEach(function (node) {
        var t = node.nodeValue, frag = document.createDocumentFragment(), last = 0, m; re.lastIndex = 0;
        while ((m = re.exec(t))) {
          var n = /^[0-9０-９]/.test(m[1])
            ? parseInt(m[1].replace(/[０-９]/g, function (d) { return String('０１２３４５６７８９'.indexOf(d)); }), 10)
            : _kanjiToNum(m[1]);
          var tgt = _chapTarget(n);
          if (!tgt) continue;
          if (m.index > last) frag.appendChild(document.createTextNode(t.slice(last, m.index)));
          var a = document.createElement('span'); a.className = 'ep-chaplink'; a.dataset.idx = tgt.idx;
          var disp = m[0];
          if (!tgt.viaLabel) {   // idx 兜底语义:显示的「第N章」对人是错的 → 替换为覆盖该 idx 的目录章名
            var lab = chapLabelOf(tgt.idx);
            if (lab) disp = '（' + lab + '）';
          }
          a.textContent = disp;
          frag.appendChild(a); last = m.index + m[0].length;
        }
        if (last) { if (last < t.length) frag.appendChild(document.createTextNode(t.slice(last))); node.parentNode.replaceChild(frag, node); }
      });
    } catch (e) {}
  }

  // 照搬 PDF _assistEdit:AI 画完高亮 → 自动生成「跳转 + 撤销⇄重做」卡片(再点切换:撤销=删,重做=用存的锚重建拿新 id)
  var _assistEdits = {}, _aeCtr = 0;
  function _epAssistEdit(d) {
    if (!d || !d.items || !d.items.length) return;
    var eid = 'ae' + (++_aeCtr);
    _assistEdits[eid] = { items: d.items.slice(), undone: false };
    var card = document.createElement('div'); card.className = 'ep-msg a ep-edit-card';
    card.innerHTML = '<div class="ep-edit-h">✏️ AI 已高亮 ' + d.items.length + ' 处</div>' +
      '<div class="ep-edit-row"><button class="ep-asst-jump" data-idx="' + d.section + '">→ 跳到此处</button>' +
      '<button class="ep-edit-undo" data-eid="' + eid + '">↩ 撤销</button></div>';
    _asstBody().appendChild(card); _asstBody().scrollTop = _asstBody().scrollHeight;
  }

  // 章名(给高亮列表 / 任务卡用)
  function _sectName(idx) { return chapLabelOf(idx) || ('第 ' + ((idx | 0) + 1) + ' 节'); }

  // PWA 通知(后台任务完成时弹)── 照搬 epub2-assist.js notify
  function notify(title, body) {
    try {
      if (!('Notification' in window) || Notification.permission !== 'granted') return;
      var opt = { body: body, tag: 'ep-asst-task', icon: '/static/icons/icon-192.png' };
      if (navigator.serviceWorker && navigator.serviceWorker.ready) navigator.serviceWorker.ready.then(function (reg) { reg.showNotification(title, opt); }).catch(function () { try { new Notification(title, opt); } catch (_) {} });
      else try { new Notification(title, opt); } catch (_) {}
    } catch (_) {}
  }

  // ════════════════════════════════════════════════════════════════════════
  // 写操作「撤销/重做」持久卡(系统自动生成,不靠 AI):制卡/笔记/生词完成 → 一张
  //   <title> [查看详情][↩撤销][↪重做] 卡;持久化进对话(刷新后还在、undo/redo 还能用)。
  //   逐字照搬 epub2-assist.js,底座差异:高亮类副作用走手搓版偏移锚 applyHl/unapplyHl(默认版无 CFI 注解)。
  //   制卡/笔记/生词全 reader-agnostic,跟 epub.js 版命中同一组 /pdf/api/epub-action 端点。
  // ════════════════════════════════════════════════════════════════════════
  var ICON_OF = { make_anki: '🎴', add_vocab: '📒', make_note: '🗒️', epub_highlight: '🖍️', auto_highlight: '🖍️',
                  notes_create: '🗒', notes_edit: '🗒' };
  function _epActSync(card) {                  // 据 state 切按钮可用态 + 卡片视觉
    var rec = card.__act || {}, undone = rec.state === 'undone';
    var bu = card.querySelector('.ep-act-undo'), br = card.querySelector('.ep-act-redo');
    if (bu) bu.disabled = undone;
    if (br) br.disabled = !undone;
    card.classList.toggle('ep-act-undone', undone);
  }
  // 高亮类 action 的客户端副作用(默认版偏移锚):undo→抹 mark;redo→照锚重画 mark。
  //   默认版不主动建高亮 action 卡(高亮走 _epAssistEdit 会话卡 + showHlPicker 逐条),此分支主要为 SSE/历史回放兜底,带 anchor 才动作。
  function _epActClientFx(rec, toState) {
    if (!rec) return;
    if (rec.kind === 'notes_create' || rec.kind === 'notes_edit') {   // 便签写操作:撤销/重做后重挂页面便签
      try { window.notesReload(); } catch (e) {}
      return;
    }
    if (rec.kind !== 'epub_highlight' && rec.kind !== 'auto_highlight') return;
    var items = (rec.redo && rec.redo.items) || [];
    items.forEach(function (it) {
      try {
        if (toState === 'undone') { if (it && it.id) unapplyHl({ id: it.id }); }
        else if (it && it.anchor) { var el = secEls[it.anchor.section]; if (el) applyHl(el, { id: it.id, anchor: it.anchor, color: it.color || '#ffd54a' }); }
      } catch (e) {}
    });
  }
  function _epActDo(card, op) {
    var rec = card.__act; if (!rec) return;
    var btn = card.querySelector(op === 'undo' ? '.ep-act-undo' : '.ep-act-redo');
    if (!btn || btn.disabled) return;
    var old = btn.textContent; btn.disabled = true; btn.textContent = (op === 'undo' ? '撤销中…' : '重做中…');
    reqJson('POST', '/pdf/api/epub-action', { op: op, file: FREL, action: rec },
      function (d) {
        rec = card.__act = d.action || rec;
        rec.state = d.state || (op === 'undo' ? 'undone' : 'done');
        _epActClientFx(rec, rec.state);
        var box = card.querySelector('.ep-act-detail-box'); if (box && rec.detail) box.textContent = rec.detail;
        btn.textContent = old; _epActSync(card);
      },
      function (er) { btn.disabled = false; btn.textContent = old; toast((op === 'undo' ? '撤销' : '重做') + '失败:' + er); });
  }
  function _epActionCard(rec) {
    if (!rec || !rec.id) return null;
    var card = document.createElement('div'); card.className = 'ep-msg a ep-act-card';
    card.dataset.aid = rec.id; card.__act = rec;
    var h = document.createElement('div'); h.className = 'ep-act-h';
    h.textContent = (ICON_OF[rec.kind] || '✏️') + ' ' + (rec.title || '写操作');
    var row = document.createElement('div'); row.className = 'ep-act-row';
    var bd = document.createElement('button'); bd.className = 'ep-act-btn ep-act-detail-btn'; bd.textContent = '查看详情';
    var bu = document.createElement('button'); bu.className = 'ep-act-btn ep-act-undo'; bu.textContent = '↩ 撤销';
    var br = document.createElement('button'); br.className = 'ep-act-btn ep-act-redo'; br.textContent = '↪ 重做';
    var box = document.createElement('div'); box.className = 'ep-act-detail-box'; box.style.display = 'none';
    box.textContent = rec.detail || '(无详情)';
    bd.addEventListener('click', function () { box.style.display = (box.style.display === 'none') ? 'block' : 'none'; _asstBody().scrollTop = _asstBody().scrollHeight; });
    bu.addEventListener('click', function () { _epActDo(card, 'undo'); });
    br.addEventListener('click', function () { _epActDo(card, 'redo'); });
    row.appendChild(bd); row.appendChild(bu); row.appendChild(br);
    card.appendChild(h); card.appendChild(row); card.appendChild(box);
    _epActSync(card);
    return card;
  }
  function _epShowAction(rec) { var c = _epActionCard(rec); if (c) { _asstBody().appendChild(c); _asstBody().scrollTop = _asstBody().scrollHeight; } return c; }
  function _epAttachActions(batch) {
    return new Promise(function (resolve, reject) {
      function send() {
        if (window.__asstStreaming) { setTimeout(send, 300); return; }
        fetch('/pdf/api/epub-action', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ op: 'attach', file: FREL, actions: batch }) })
          .then(function (response) {
            return response.json().then(function (payload) {
              if (!response.ok || !payload || payload.ok !== true || payload.stored !== true) {
                throw new Error((payload && payload.error) || ('HTTP ' + response.status));
              }
              resolve(payload);
            });
          }).catch(reject);
      }
      send();
    });
  }
  // 前端建的 action → 落库(upsert by id,幂等)。流式中先等本轮 assistant 消息落库再 attach(防 attach 落到 user 消息上)。
  var _epPending = [];
  function _epFlushActions() {
    if (!_epPending.length) return;
    if (window.__asstStreaming) { setTimeout(_epFlushActions, 300); return; }   // 共享侧栏流式中缓一缓:等本轮 assistant 消息落库再 attach(rc-assistant._setSendMode 维护标记)
    var batch = _epPending.slice(); _epPending = [];
    _epAttachActions(batch).catch(function (error) {
      toast('动作未保存:' + String(error && error.message || error));
    });
  }
  function _epQueueAction(rec) { if (rec) { _epPending.push(rec); _epFlushActions(); } }
  // 后台任务(制卡/笔记/生词)完成 → 拿 undo_id 让后端建完整 action(含快照)+ 落库,再渲卡
  function _epTaskAction(undoId) {
    if (!undoId) return;
    fetch('/pdf/api/epub-action', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ op: 'from_task', file: FREL, undo_id: undoId }) })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (d && d.ok && d.action) { _epShowAction(d.action); } })
      .catch(function () {});
  }

  // Native note writes are planned by the Pi assistant but committed only by
  // this client action. `/epub-action` performs the App-owned IndexedDB write
  // and Pi conversation-metadata commit as one compensated transaction; the
  // success card is therefore never shown before both halves acknowledge.
  function _nativeLocalEPUBMutationTransaction(request) {
    if (!request || request.contract !== 'reader-native-epub-action/1' ||
        !request.action || request.file !== FREL) {
      toast('本机便签操作无效');
      return Promise.reject(new Error('本机便签操作无效'));
    }
    // @interaction document.epub-action.commit
    return fetch('/pdf/api/epub-action', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ op: 'native_apply', contract: request.contract,
        file: request.file, action: request.action }) })
      .then(function (response) {
        return response.json().then(function (payload) {
          if (!response.ok || !payload || payload.ok !== true || !payload.action) {
            throw new Error((payload && payload.error) || ('HTTP ' + response.status));
          }
          try { window.notesReload(); } catch (_) {}
          _epShowAction(payload.action);
          toast('便签已保存');
          return payload;
        });
      }).catch(function (error) {
        toast('便签未保存:' + String(error && error.message || error));
        throw error;
      });
  }
  window.nativeLocalEPUBMutationTransaction = _nativeLocalEPUBMutationTransaction;
  window.nativeLocalEPUBMutation = function (request) {
    var task = _nativeLocalEPUBMutationTransaction(request);
    task.catch(function () {});   // established client_action dispatch is fire-and-forget
    return task;
  };

  // ── window.showHlPicker:逐条渲染一组高亮(色块 + 原文 + ↗跳转 + 🗑删除)= epub2-assist 的偏移锚版。
  //    auto_highlight 标完(picker:true)/ find_highlights(后端 client_action {fn:showHlPicker})都用它。
  window.showHlPicker = function (d) {
    try {
      var items = ((d && d.items) || []).filter(function (it) { return it && it.id; });   // 无 id 删不了 → 不渲
      var card = document.createElement('div'); card.className = 'ep-msg a';
      if (!items.length) {
        card.innerHTML = '<span class="ep-asst-tool">没有可操作的高亮</span>';
        _asstBody().appendChild(card); _asstBody().scrollTop = _asstBody().scrollHeight; return;
      }
      var h = document.createElement('div'); h.className = 'ep-hl-pick-h';
      h.textContent = '共 ' + items.length + ' 处高亮 —— 点「跳转」去看,点「删除」移除:';
      card.appendChild(h);
      items.forEach(function (it) {
        var row = document.createElement('div'); row.className = 'ep-hl-row';
        var sw = document.createElement('span'); sw.className = 'ep-hl-sw'; sw.style.background = it.color || '#ffd54a';
        var tx = document.createElement('span'); tx.className = 'ep-hl-tx';
        var sec = (it.section != null) ? it.section : (it.anchor && it.anchor.section);
        tx.textContent = ((sec != null) ? (_sectName(sec) + ' · ') : '') + (it.text || '(无文字)');
        tx.title = it.text || '';
        // 存下重建所需的偏移锚/原文/色 → 删完能「↪重做」恢复。auto_highlight 的 item 自带 anchor;
        // find_highlights 的 item 不带 anchor(后端只给 id/section/text/color),从已加载的 _hls[id] 取完整锚兜底。
        var full = _hls[it.id] || {};
        row.__hl = { id: it.id, section: sec, text: it.text || full.text || '',
                     anchor: it.anchor || full.anchor || null,
                     color: it.color || full.color || (localStorage.getItem('eph-hl-color') || hlColors()[0]) };
        var jb = document.createElement('button'); jb.className = 'ep-asst-jump';
        if (sec != null) jb.dataset.idx = sec;
        jb.textContent = '↗ 跳转';
        var del = document.createElement('button'); del.className = 'ep-hl-del';
        del.dataset.id = it.id; del.textContent = '🗑 删除';
        row.appendChild(sw); row.appendChild(tx); row.appendChild(jb); row.appendChild(del);
        card.appendChild(row);
      });
      _asstBody().appendChild(card); _asstBody().scrollTop = _asstBody().scrollHeight;
    } catch (e) { dbg('showHlPicker err:' + (e && e.message)); }
  };

  // 气泡区委托:章节链接跳转 / ↗跳转 chip / ↩撤销
  _asstBody().addEventListener('click', function (e) {
    var t = e.target;
    var cl = t.closest && t.closest('.ep-chaplink');   // ⑥ 跳转类操作不再自动收抽屉(仅极窄屏收,见 _drawerAfterJump)
    if (cl) { var ci = parseInt(cl.dataset.idx, 10); if (!isNaN(ci)) { jumpTo(ci, false); _drawerAfterJump(); } return; }
    var jp = t.closest && t.closest('.ep-asst-jump');
    if (jp) { var ji = parseInt(jp.dataset.idx, 10); if (!isNaN(ji)) { jumpTo(ji, false); _drawerAfterJump(); } return; }
    var hd = t.closest && t.closest('.ep-hl-del');   // showHlPicker 里的「🗑删除」:删该条高亮 → 删完转「↪重做」(可恢复)
    if (hd) {
      var hrow = hd.closest('.ep-hl-row'), hrec = hrow && hrow.__hl;
      var hid = (hrec && hrec.id) || hd.dataset.id; if (!hid) return;
      hd.disabled = true; hd.textContent = '删除中…';
      fetch('/pdf/api/epub-highlights?file=' + encodeURIComponent(FREL) + '&id=' + encodeURIComponent(hid), { method: 'DELETE' })
        .then(function (r) { return r.json(); })
        .then(function (dd) {
          if (dd && dd.ok) {
            try { unapplyHl({ id: hid }); } catch (e) {}   // 立刻抹掉正文 mark(不等刷新)
            delete _hls[hid];
            if (hrec) hrec.id = null;
            if (hrow) { hrow.style.opacity = '.55'; var tx = hrow.querySelector('.ep-hl-tx'); if (tx) tx.style.textDecoration = 'line-through'; }
            // 有偏移锚 → 转「↪ 重做」(同一按钮翻成 redo,可恢复);无锚(老数据)→ 退回「已删」静态
            if (hrec && hrec.anchor) { hd.className = 'ep-hl-redo'; hd.disabled = false; hd.textContent = '↪ 重做'; }
            else hd.outerHTML = '<span class="ep-asst-tool">🗑 已删</span>';
          } else { hd.disabled = false; hd.textContent = '🗑 删除'; toast('删除失败:' + ((dd && dd.error) || '')); }
        })
        .catch(function () { hd.disabled = false; hd.textContent = '🗑 删除'; });
      return;
    }
    var hr = t.closest && t.closest('.ep-hl-redo');   // showHlPicker 里的「↪重做」:用存下的偏移锚 + 色 + 原文重建高亮(拿新 id),转回「🗑删除」
    if (hr) {
      var rrow = hr.closest('.ep-hl-row'), rrec = rrow && rrow.__hl;
      if (!rrec || !rrec.anchor) { toast('无法恢复(缺少定位)'); return; }
      hr.disabled = true; hr.textContent = '重做中…';
      reqJson('POST', '/pdf/api/epub-highlights', { file: FREL, anchor: rrec.anchor, text: rrec.text, color: rrec.color },
        function (d) {
          var h = d.highlight; _hls[h.id] = h; rrec.id = h.id;
          var el = secEls[rrec.anchor.section]; if (el) applyHl(el, h);
          if (rrow) { rrow.style.opacity = ''; var tx = rrow.querySelector('.ep-hl-tx'); if (tx) tx.style.textDecoration = ''; }
          hr.className = 'ep-hl-del'; hr.dataset.id = h.id; hr.disabled = false; hr.textContent = '🗑 删除';
        },
        function (er) { hr.disabled = false; hr.textContent = '↪ 重做'; toast('重做失败:' + er); });
      return;
    }
    var ub = t.closest && t.closest('.ep-asst-undo');
    if (ub) {
      var uid = ub.getAttribute('data-uid'); if (!uid) return;
      ub.disabled = true; ub.textContent = '撤销中…';
      reqJson('POST', '/api/assistant/undo', { id: uid },
        function () { ub.outerHTML = '<span class="ep-asst-tool">↩ 已撤销</span>'; },
        function (er) { ub.disabled = false; ub.textContent = '↩ 撤销'; toast('撤销失败:' + er); });
      return;
    }
    // 高亮卡的「撤销 ⇄ 重做」切换(照搬 PDF asst-edit-undo)
    var eb = t.closest && t.closest('.ep-edit-undo');
    if (eb) {
      var eid = eb.getAttribute('data-eid'); var stt = _assistEdits[eid]; if (!stt) return;
      if (!stt.undone) {
        eb.disabled = true; eb.textContent = '撤销中…';
        var n = stt.items.length, done0 = 0;
        stt.items.forEach(function (it) {
          reqJson('DELETE', '/pdf/api/epub-highlights?file=' + encodeURIComponent(FREL) + '&id=' + encodeURIComponent(it.id), null,
            function () { unapplyHl({ id: it.id }); delete _hls[it.id]; if (++done0 === n) { stt.undone = true; eb.disabled = false; eb.textContent = '↪ 重做'; } },
            function () { if (++done0 === n) { stt.undone = true; eb.disabled = false; eb.textContent = '↪ 重做'; } });
        });
      } else {
        eb.disabled = true; eb.textContent = '重做中…';
        var n2 = stt.items.length, done2 = 0, fresh = [];
        stt.items.forEach(function (it) {
          reqJson('POST', '/pdf/api/epub-highlights', { file: FREL, anchor: it.anchor, text: it.text, color: it.color },
            function (d) { var h = d.highlight; _hls[h.id] = h; var el = secEls[it.anchor.section]; if (el) applyHl(el, h); fresh.push({ id: h.id, text: it.text, anchor: it.anchor, color: it.color }); if (++done2 === n2) { stt.items = fresh; stt.undone = false; eb.disabled = false; eb.textContent = '↩ 撤销'; } },
            function () { if (++done2 === n2) { stt.undone = false; eb.disabled = false; eb.textContent = '↩ 撤销'; } });
        });
      }
      return;
    }
  });

  // ── 选区:cur._pending 优先(供别处带入),否则取正文活动选区;selection_sentence 给 cur.ctx ──
  // 当前视口正在看的正文(注意力焦点):取与滚动容器视口相交的正文块文字,限长防 prompt 膨胀。
  // EPUB 一节=整章内容太长,只给 AI 章节号会答偏(把「酶」问成整章「生物物理」);给可见部分 → 回答/找视频紧扣当前。
  function _visibleText() {
    try {
      var sc = content; if (!sc || !col) return '';
      var r = sc.getBoundingClientRect(), top = r.top, bot = r.bottom;
      var blocks = col.querySelectorAll('p,h1,h2,h3,h4,h5,h6,li,blockquote,figcaption,td');
      var parts = [];
      for (var i = 0; i < blocks.length; i++) {
        var b = blocks[i], br = b.getBoundingClientRect();
        if (br.height && br.bottom > top + 8 && br.top < bot - 8) {   // 与视口相交(留 8px 容差,避开只露一线的块)
          var t = (b.textContent || '').replace(/\s+/g, ' ').trim();
          if (t) parts.push(t);
        }
      }
      if (!parts.length) {   // 兜底:正文块非 p/h(有些书结构不同)→ 取视口内的整个 .ep-sec 文字
        var secs = col.querySelectorAll('.ep-sec');
        for (var j = 0; j < secs.length; j++) {
          var sr = secs[j].getBoundingClientRect();
          if (sr.height && sr.bottom > top + 8 && sr.top < bot - 8) { var st = (secs[j].textContent || '').replace(/\s+/g, ' ').trim(); if (st) parts.push(st); }
        }
      }
      var txt = parts.join('\n');
      return txt.length > 1000 ? txt.slice(0, 1000) + '…' : txt;
    } catch (e) { return ''; }
  }
  function curSelection() {
    var sel = cur._pending || '', sent = '';
    if (sel) { sent = (cur.text === sel ? (cur.ctx || '') : ''); return { sel: sel, sent: sent }; }
    // 持久兜底(照搬 PDF 05-nav __voiceContext lastSelText):开助手/点输入框后原生选区被清,但 cur.text 仍在 →
    // 助手才拿得到「用户刚选的内容」。新鲜度防陈旧:选中所在节仍在视野(≤1 节)且 10 分钟内才用,否则回退 live 原生(实时,无陈旧)。
    var an = cur.anchor, secVisible = false;
    if (an) { try { var _se = document.querySelector('.ep-sec[data-idx="' + an.section + '"]'); if (_se) { var _r = _se.getBoundingClientRect(); secVisible = _r.bottom > 0 && _r.top < (window.innerHeight || 800); } } catch (_) {} }
    if (cur.text && an && (Date.now() - (cur._selT || 0) < 600000) && secVisible) {   // 新鲜度:选中所在节仍在视野就算(比 ±1 节稳——章节大小不一/开抽屉重排 _curTopIdx 会偏,±1 会误判成陈旧)
      return { sel: cur.text, sent: cur.ctx || '' };
    }
    try {
      var s = window.getSelection();
      if (s && s.rangeCount && !s.isCollapsed) {
        var tx = (s.toString() || '').trim();
        if (tx && col.contains(s.getRangeAt(0).commonAncestorContainer)) { sel = tx; sent = (cur.text === tx ? (cur.ctx || '') : ''); }
      }
    } catch (e) {}
    return { sel: sel, sent: sent };
  }

  function streamInto(card, url, body) { body.rid = 'e' + Date.now(); var acc = ''; sse(url, body, function (t) { acc += t; setMd(card, acc); _asstBody().scrollTop = _asstBody().scrollHeight; }, function () { if (!acc) card.textContent = '(空)'; }, function (er) { card.textContent = '✗ ' + er; }); }

  // ── 高亮(偏移锚:{section,start,end})──
  function hlColors() { return (window.RC && RC.settings && RC.settings.hlColors) ? RC.settings.hlColors() : ['#fff59d', '#a7f3d0', '#a3d4ff', '#fda4af']; }
  // 动态渲染选中工具栏的色板(读 RC.settings.hlColors;设置面板改色后调本函数重渲)
  function renderHlPicker() {
    var box = $('ep-hl-pick'); if (!box) return;
    var html = '<span class="lbl">\ud83d\udd8c</span>';
    hlColors().forEach(function (c) { html += '<i class="swatch" data-c="' + c + '" style="background:' + c + '"></i>'; });
    box.innerHTML = html;
  }
  // 锚元素解析:正文章节 = secEls[int idx];插入页(.ep-usec,string u_* 锚)= 用户页正文 .rc-up-body(offset 空间/高亮容器)。
  //   插入页元素由 RC.userpages 管理(mount/place),未挂载时返 null;高亮由 onRender 在挂载/重渲后按 offset 复原。
  function _secElOf(section) {
    if (typeof section === 'string') { var ue = (window.RC && RC.userpages && RC.userpages.elOf) ? RC.userpages.elOf(section) : null; return ue ? ue.querySelector('.rc-up-body') : null; }
    return secEls[section];
  }
  function applyHl(secEl, h) {
    var a = h.anchor; if (!a || a.start >= a.end) return;
    if (secEl.querySelector('mark.ep-hl[data-id="' + h.id + '"]')) return;   // 已应用
    var w = document.createTreeWalker(secEl, NodeFilter.SHOW_TEXT, null), n, pos = 0, hits = [];
    while ((n = w.nextNode())) {
      if (!_countable(n)) continue;                                              // G0:我们的 rt/译文不进偏移空间
      var len = n.nodeValue.length;
      if (n.parentElement && n.parentElement.tagName === 'MARK') { pos += len; continue; }
      var ns = pos, ne = pos + len;
      if (ne > a.start && ns < a.end) hits.push({ node: n, s: Math.max(0, a.start - ns), e: Math.min(len, a.end - ns) });
      pos = ne; if (pos >= a.end) break;
    }
    dbg('applyHl id=' + h.id + ' hits=' + hits.length + ' anchor=' + JSON.stringify(a));
    var hasColor = !!(h.color && h.color.trim());
    var hasNote = !!((h.note || '').trim());
    hits.reverse().forEach(function (r) {
      try {
        var rest = r.node.splitText(r.s); rest.splitText(r.e - r.s);
        var mk = document.createElement('mark'); mk.className = 'ep-hl' + (hasColor ? '' : ' nocolor') + (hasNote ? ' has-note' : ''); mk.dataset.id = h.id;
        // 照搬 PDF:半透明 rgba 底色 → 字透出来不被实色盖死;无色 → 虚框模式(只留备注)
        if (hasColor) mk.style.background = hlRgba(h.color, 0.42);
        rest.parentNode.insertBefore(mk, rest); mk.appendChild(rest);
      } catch (e) { dbg('applyHl wrap err: ' + (e && e.message)); }
    });
  }
  function hlRgba(hex, a) { var m = /^#?([0-9a-fA-F]{6})$/.exec((hex || '').trim()); if (!m) return hex || 'transparent'; var n = parseInt(m[1], 16); return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + a + ')'; }
  function unapplyHl(h) { col.querySelectorAll('mark.ep-hl[data-id="' + h.id + '"]').forEach(function (mk) { var p = mk.parentNode; while (mk.firstChild) p.insertBefore(mk.firstChild, mk); p.removeChild(mk); p.normalize(); }); }
  function saveHl(text, anchor, color) {
    if (!anchor) { toast('无法定位选区'); return Promise.resolve(null); }
    color = color || localStorage.getItem('eph-hl-color') || hlColors()[0];   // 记住上次用的色(照搬 PDF)
    try { localStorage.setItem('eph-hl-color', color); } catch (e) {}
    return new Promise(function (resolve) {
      reqJson('POST', '/pdf/api/epub-highlights', { file: FREL, anchor: anchor, text: text, color: color }, function (d) {
        _hls[d.highlight.id] = d.highlight; var el = _secElOf(anchor.section);
        if (el) applyHl(el, d.highlight); toast('已高亮'); window.getSelection().removeAllRanges(); hideSel();
        resolve(d.highlight);
      }, function (er) {
        toast('高亮失败:' + er);
        resolve(null);
      });
    });
  }
  function delHl(h) { reqJson('DELETE', '/pdf/api/epub-highlights?file=' + encodeURIComponent(FREL) + '&id=' + encodeURIComponent(h.id), null, function () { unapplyHl(h); delete _hls[h.id]; toast('已删除'); }, function () {}); }
  function patchHl(h, f) { reqJson('PATCH', '/pdf/api/epub-highlights', Object.assign({ file: FREL, id: h.id }, f), function (d) { if (f.color && f.color !== h.color) { unapplyHl(h); h.color = d.highlight.color; var el = _secElOf(h.anchor.section); if (el) applyHl(el, h); } h.note = d.highlight.note; if ('sentence' in f) h.sentence = d.highlight.sentence; if ('kind' in f) h.kind = d.highlight.kind; if ('body' in f) h.body = d.highlight.body; }, function () {}); }
  // 高亮编辑浮层:用共享层 RC.highlight.openEditor;EPUB 适配器提供改色/备注/删除回调 + 锚元素
  function openHlEditor(h, anchorEl) {
    RC.highlight.openEditor({
      colors: hlColors(), current: h.color, note: h.note || '',
      preview: h.text || '', sentence: h.sentence || '',   // 只读预览:看到高亮的原文 / 所在句(H8)
      anchorEl: anchorEl, anchorSelector: 'mark.ep-hl',
      onColor: function (c) { patchHl(h, { color: c }); },
      onNote: function (t) { patchHl(h, { note: t }); },
      onDelete: function () { delHl(h); }
    });
  }
  function loadHls() { fetch('/pdf/api/epub-highlights?file=' + encodeURIComponent(FREL)).then(function (r) { return r.json(); }).then(function (d) { (d.highlights || []).forEach(function (h) { if (!h.anchor) return; _hls[h.id] = h; if (typeof h.anchor.section === 'string') { var ub = _secElOf(h.anchor.section); if (ub && ub.isConnected) applyHl(ub, h); } else { var el = secEls[h.anchor.section]; if (el && loaded[h.anchor.section] === true) applyHl(el, h); } }); _decorateVisible(); }).catch(function () {}); }
  // 高亮列表:渲进抽屉「高亮」pane(由 RC.sidedrawer 的 onTab('hl') 触发)
  // ── 查询结果历史(镜像 PDF 21-misc-ai;统一抽屉「历史」tab 对等)──
  //   rc-result 的 beforeOpen 钩子(共享层早就位,此前 EPUB 没传)每次开结果框前把上一条快照进
  //   localStorage 'pdf-qhist-<FREL>'(per-book,同 PDF 键前缀;设备本地,PDF 侧同款设计);点条目 openResult 回放。
  var _qhRestoring = false;
  function _qhKey() { return 'pdf-qhist-' + (FREL || '_'); }
  function _qhLoad() { try { return JSON.parse(localStorage.getItem(_qhKey()) || '[]'); } catch (e) { return []; } }
  function _qhPush() {
    if (_qhRestoring) return;
    var cont = document.getElementById('result-content'); if (!cont) return;
    var html = cont.innerHTML || '', title = cont.dataset.title || '', src = cont.dataset.src || '';
    if (!title && !src) return;
    if (html.length < 40 || (/⏳|class="loading"/.test(html) && html.length < 120)) return;   // 跳过空/纯 loading
    var h = _qhLoad();
    if (h.length && h[0].src === src && h[0].title === title) { h[0].html = html.slice(0, 60000); h[0].time = Date.now(); }
    else h.unshift({ id: 'qh_' + Date.now(), title: title, src: src, html: html.slice(0, 60000), time: Date.now() });
    try { localStorage.setItem(_qhKey(), JSON.stringify(h.slice(0, 40))); } catch (e) {}
    var pane = document.getElementById('ep-side-hist');
    if (pane && pane.classList.contains('active')) _renderQhist();
  }
  try { if (window.RC && RC.result && RC.result.config) RC.result.config({ beforeOpen: _qhPush }); } catch (e) {}
  function _qhFmt(t) {
    var d = new Date(t), n = new Date();
    var hm = String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
    return (d.toDateString() === n.toDateString() ? '今天 ' : ((d.getMonth() + 1) + '/' + d.getDate() + ' ')) + hm;
  }
  function _renderQhist() {
    var box = document.getElementById('ep-qhist-list'); if (!box) return;
    if (!document.getElementById('ep-qhist-css')) {   // 样式照搬 pdf-styles.css 的 .qhist-*(那份只在 PDF 页)
      var st = document.createElement('style'); st.id = 'ep-qhist-css';
      st.textContent = '#ep-qhist-list .qhist-item{padding:8px 12px;border-bottom:1px solid #1f2740;cursor:pointer}' +
        '#ep-qhist-list .qhist-item:hover{background:#162045}' +
        '#ep-qhist-list .qhist-title{color:#cfe6ff;font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}' +
        '#ep-qhist-list .qhist-src{color:#8a9bb4;font-size:11px;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}' +
        '#ep-qhist-list .qhist-time{color:#5a6680;font-size:10px;margin-top:2px}';
      document.head.appendChild(st);
    }
    var h = _qhLoad();
    if (!h.length) { box.innerHTML = '<div style="color:#5a6680;font-size:12px;padding:10px">还没有查询记录</div>'; return; }
    box.innerHTML = h.map(function (it) {
      return '<div class="qhist-item" data-id="' + it.id + '"><div class="qhist-title">' + esc(it.title) + '</div>' +
        '<div class="qhist-src">' + esc((it.src || '').slice(0, 80)) + '</div>' +
        '<div class="qhist-time">' + _qhFmt(it.time) + '</div></div>';
    }).join('');
    box.querySelectorAll('.qhist-item').forEach(function (el) {
      el.addEventListener('click', function () {
        var it = _qhLoad().find(function (x) { return x.id === el.dataset.id; }); if (!it) return;
        _qhRestoring = true;
        try { window.openResult(it.title, it.src, it.html); } finally { _qhRestoring = false; }
        try { if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([document.getElementById('result-content')]).catch(function () {}); } catch (e) {}
      });
    });
  }

  function loadHlPane() {
    var box = $('ep-side-hl'); if (!box) return;
    box.innerHTML = '<div class="ep-empty"><span class="ep-spin"></span> 加载…</div>';
    fetch('/pdf/api/epub-highlights?file=' + encodeURIComponent(FREL)).then(function (r) { return r.json(); }).then(function (d) {
      var hs = (d && d.highlights || []).filter(function (h) { return h.anchor; });
      hs.forEach(function (h) { _hls[h.id] = h; });
      RC.highlight.renderList(box, hs, {
        reverse: true,
        emptyHtml: '还没有高亮。<br>选中文字 → 底部「🖍 高亮」',
        onJump: function (h) { jumpTo(h.anchor.section, false); _drawerAfterJump(); },   // ⑥ 宽屏保持抽屉开,可连续点下一条
        onDelete: function (h) { delHl(h); }
      });
    }).catch(function () { box.innerHTML = '<div class="ep-empty">加载失败</div>'; });
  }

  // ── 搜索(照搬 PDF:居中浮层 + 防抖 oninput + 计数 + Esc 关)──
  var sp = $('ep-search');
  $('ep-search-btn').addEventListener('click', function () { sp.classList.add('open'); setTimeout(function () { $('ep-search-in').focus(); }, 100); });
  $('ep-search-x').addEventListener('click', function () { sp.classList.remove('open'); });
  function doSearch() {
    var q = ($('ep-search-in').value || '').trim(), res = $('ep-search-res'), stat = $('ep-search-stat');
    if (!q) { res.innerHTML = ''; if (stat) stat.textContent = ''; return; }
    if (stat) stat.textContent = '…'; res.innerHTML = '<div class="ep-sr-empty"><span class="ep-spin"></span> 搜索中…</div>';
    fetch('/pdf/api/epub-search?file=' + encodeURIComponent(FREL) + '&q=' + encodeURIComponent(q)).then(function (r) { return r.json(); }).then(function (d) {
      var rs = (d && d.results) || []; if (!rs.length) { res.innerHTML = '<div class="ep-sr-empty">没找到「' + esc(q) + '」</div>'; if (stat) stat.textContent = '0'; return; }
      if (stat) stat.textContent = rs.length + ' 处';
      res.innerHTML = ''; var rx = new RegExp('(' + q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi');
      rs.forEach(function (m) { var el = document.createElement('div'); el.className = 'ep-sr'; el.innerHTML = '<div class="loc">' + esc(m.loc || '') + '</div><div class="ex">' + esc(m.excerpt).replace(rx, '<b>$1</b>') + '</div>';
        el.onclick = function () { sp.classList.remove('open'); if (typeof m.idx === 'number') { jumpTo(m.idx, false); _searchHilite(m.idx, q); } }; res.appendChild(el); });
    }).catch(function () { res.innerHTML = '<div class="ep-sr-empty">搜索失败</div>'; if (stat) stat.textContent = ''; });
  }
  var _searchDeb = (window.RC && RC.debounce) ? RC.debounce(doSearch, 320) : doSearch;
  $('ep-search-in').addEventListener('input', _searchDeb);
  $('ep-search-in').addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); doSearch(); } else if (e.key === 'Escape') { sp.classList.remove('open'); } });
  // 搜索命中:跳到章后在正文画黄高亮 + 滚到命中处居中 + 6s 淡出(照搬 PDF .search-hl;用 span 不用 mark 不干扰高亮系统)
  function _searchHilite(idx, q) {
    if (!q) return;
    var tries = 0;
    (function go() {
      var el = secEls[idx];
      if (!el || loaded[idx] !== true) { if (tries++ < 30) setTimeout(go, 120); return; }
      try {
        var w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null), n, hit = null;
        var ql = q.toLowerCase();
        while ((n = w.nextNode())) {
          if (!_countable(n)) continue;
          if (n.parentElement && (n.parentElement.closest('mark,.ep-search-hl,rt'))) continue;
          var pos = (n.nodeValue || '').toLowerCase().indexOf(ql);
          if (pos >= 0) { hit = { node: n, s: pos, e: pos + q.length }; break; }
        }
        if (!hit) return;
        var rest = hit.node.splitText(hit.s); rest.splitText(hit.e - hit.s);
        var sp2 = document.createElement('span'); sp2.className = 'ep-search-hl';
        rest.parentNode.insertBefore(sp2, rest); sp2.appendChild(rest);
        _centerInstant(sp2);   // ② 跳转类统一瞬时(原 smooth scrollIntoView)
        setTimeout(function () { try { var p = sp2.parentNode; while (sp2.firstChild) p.insertBefore(sp2.firstChild, sp2); p.removeChild(sp2); p.normalize(); } catch (e) {} }, 6000);
      } catch (e) {}
    })();
  }

  // ── 顶栏 / 设置 / 完整版 ──(目录/助手/高亮/知识点 入口都并入右侧统一抽屉,见 RC.sidedrawer.init)
  function openSettings(tab) {
    if (!(window.RC && RC.settings)) { $('ep-set').classList.toggle('open'); return; }   // 兜底
    RC.settings.open({
      tab: tab,
      getReadState: function () { return { fs: st.fs, th: st.th, lh: st.lh }; },
      onFontSize: function (d) { st.fs = Math.min(220, Math.max(70, st.fs + d)); localStorage.setItem(LS.fs, st.fs); _reflowKeepAnchor(applyStyle); refreshSet(); },
      onLineHeight: function (d) { st.lh = Math.min(2.4, Math.max(1.0, +(st.lh + d).toFixed(1))); localStorage.setItem(LS.lh, st.lh); _reflowKeepAnchor(applyStyle); refreshSet(); },
      onTheme: function (th) { st.th = th; localStorage.setItem(LS.th, st.th); applyStyle(); refreshSet(); },
      onConvertFull: function (btn) { convertToFull(btn); },
      getBookLangs: function () { return BOOK_LANGS; },
      onSaveLangs: function () { if (window.saveLangPicker) window.saveLangPicker(); },
      onVocabUnderline: function (on) { if (window.__epSetVocabUnderline) window.__epSetVocabUnderline(on); },
      onClickTranslate: function (on) { try { localStorage.setItem('eph-click-translate', on ? '1' : '0'); } catch (e) {} },
      onHlColors: function () { renderHlPicker(); },   // 设置面板增删/恢复高亮色 → 立即重渲工具栏色板
      grammarFile: FREL,   // 语法 pane:KG 启用列表(RC.grammar.renderTrackList 渲进 #set-grammar-list,跟 PDF 同一份)
      onGrammarView: function (v) {   // 长句结构显示切换:即时重渲已开的分析块(等价 PDF setGrammarView 共享分支)
        if (window.RC && RC.grammar && RC.grammar.setViewMode) { RC.grammar.setViewMode('ep-grammar-body', v, 'eph-grammar-view'); }
        else { try { localStorage.setItem('eph-grammar-view', v); } catch (e) {} }
      }
    });
  }
  $('ep-set-btn').addEventListener('click', function () { openSettings(); });
  function convertToFull(btn) {
    btn.disabled = true; btn.textContent = '⏳ 处理中…';
    reqJson('POST', '/pdf/api/epub-to-full', { file: FREL }, function (d) {
      if (d.ready) { location.href = d.view_url; return; }
      var iv = setInterval(function () { fetch('/pdf/api/ebook-convert-status?job=' + encodeURIComponent(d.job)).then(function (r) { return r.json(); }).then(function (s) {
        if (s.status === 'done') { clearInterval(iv); location.href = s.view_url || d.view_url; }
        else if (s.status === 'error') { clearInterval(iv); btn.textContent = '✗ 转换失败'; btn.disabled = false; }
        else btn.textContent = '⏳ 转换中(可关页面)…';
      }).catch(function () {}); }, 5000);
    }, function (er) { btn.textContent = '✗ ' + er; btn.disabled = false; });
  }
  // ── 章节 scrubber(照搬 PDF #page-scrub:横拖跳章 + 浮层;reflow 按 section idx)──
  (function setupScrub() {
    var sc = $('ep-scrub'), pop = $('ep-scrub-pop'); if (!sc || !pop) return;
    var drag = null;
    function chapLabel(idx) { var lab = ''; for (var i = 0; i < TOC.length; i++) { if (TOC[i].idx <= idx) lab = TOC[i].label; else break; } return lab; }
    function showPop(idx) { pop.classList.add('on'); var lab = chapLabel(idx); pop.querySelector('.psp-num').textContent = (lab ? lab + '  ' : '') + '(' + (idx + 1) + '/' + COUNT + ')'; pop.querySelector('.psp-fill').style.width = (COUNT > 1 ? idx / (COUNT - 1) * 100 : 0) + '%'; }
    sc.addEventListener('pointerdown', function (e) { if (!COUNT) return; drag = { x: e.clientX, start: _curTopIdx, moved: false, tgt: _curTopIdx }; try { sc.setPointerCapture(e.pointerId); } catch (er) {} e.preventDefault(); });
    sc.addEventListener('pointermove', function (e) { if (!drag) return; var w = Math.max(220, (content.clientWidth || window.innerWidth) * 0.7); var dx = e.clientX - drag.x; if (Math.abs(dx) > 4) drag.moved = true; var tgt = Math.max(0, Math.min(COUNT - 1, Math.round(drag.start + dx / w * COUNT))); drag.tgt = tgt; $('ep-page-cur').textContent = (tgt + 1); showPop(tgt); });
    sc.addEventListener('pointerup', function (e) {
      if (!drag) return; var d = drag; drag = null; pop.classList.remove('on'); try { sc.releasePointerCapture(e.pointerId); } catch (er) {}
      if (d.moved) { jumpTo(d.tgt, false); }
      else { var cur0 = COUNT > 1 ? Math.round(_curTopIdx / (COUNT - 1) * 100) : 0; var v = prompt('跳到百分比 (0-100):', String(cur0)); if (v != null) { var p = parseInt(v, 10); if (!isNaN(p)) jumpTo(Math.max(0, Math.min(COUNT - 1, Math.round(p / 100 * (COUNT - 1)))), false); } }
    });
    sc.addEventListener('pointercancel', function () { drag = null; pop.classList.remove('on'); });
  })();
  // ── 全屏(照搬 PDF/epub.js 版 setupFs:body.fs-mode 隐顶栏 + Fullscreen API + 记忆 + toast 提示
  //    + fullscreenchange 监听(浏览器/Esc/F11 退出后同步 set(false),防顶栏卡死)+ webkit 前缀回退)──
  (function setupFs() {
    var btn = $('fs-toggle'), rest = $('fs-restore'); if (!btn) return;
    function _fsActive() { return !!(document.fullscreenElement || document.webkitFullscreenElement); }
    function _reqFs() { var el = document.documentElement, fn = el.requestFullscreen || el.webkitRequestFullscreen; if (fn && !_fsActive()) { try { Promise.resolve(fn.call(el)).catch(function () {}); } catch (e) {} } }
    function _exitFs() { var fn = document.exitFullscreen || document.webkitExitFullscreen; if (fn && _fsActive()) { try { Promise.resolve(fn.call(document)).catch(function () {}); } catch (e) {} } }
    function set(on) {
      document.body.classList.toggle('fs-mode', on); btn.classList.toggle('active', on);
      try { localStorage.setItem('eph-fs-mode', on ? '1' : '0'); } catch (e) {}
      if (on) _reqFs(); else _exitFs();
      toast(on ? '全屏阅读:点右上角 ⤢ 恢复' : '已退出全屏');
    }
    btn.addEventListener('click', function () { set(!document.body.classList.contains('fs-mode')); });
    if (rest) rest.addEventListener('click', function () { set(false); });
    // 用户用 Esc/F11 退出浏览器全屏 → 同步退出页内全屏(顶栏恢复),防顶栏卡死(对齐 PDF/epub.js fullscreenchange)
    ['fullscreenchange', 'webkitfullscreenchange'].forEach(function (ev) {
      document.addEventListener(ev, function () { if (!_fsActive() && document.body.classList.contains('fs-mode')) set(false); });
    });
    var restore = localStorage.getItem('eph-fs-mode') === '1' && _fsActive();
    document.body.classList.toggle('fs-mode', restore);
    btn.classList.toggle('active', restore);
    if (!restore) { try { localStorage.setItem('eph-fs-mode', '0'); } catch (e) {} }
  })();
  // ── 双指捏合缩放(对照 PDF/epub.js setupEpubPinch:reflow 无栅格 → 捏合映射到字号步进 onFontSize)──
  //   底座换默认手搓版:主文档容器 #ep-content 双指 touchstart 记 Math.hypot 间距 → touchend 按比例换算字号步数
  //   (STEP=10,对齐设置面板 A±);document 挂 gesturestart/gesturechange preventDefault 挡 iOS 原生位图糊缩放;
  //   desktop Ctrl+滚轮 同映射。默认版正文在主文档(无 iframe),只挂 #ep-content + document,不需逐章 doc。
  (function setupEpubPinch() {
    var STEP = 10;
    function _fontStep(delta) { st.fs = Math.min(220, Math.max(70, st.fs + delta)); try { localStorage.setItem(LS.fs, st.fs); } catch (e) {} _reflowKeepAnchor(applyStyle); refreshSet(); }
    function _noGesture(e) { try { e.preventDefault(); } catch (_) {} }
    try { document.addEventListener('gesturestart', _noGesture, { passive: false }); document.addEventListener('gesturechange', _noGesture, { passive: false }); } catch (e) {}
    function applyPinch(d0, d1) {
      if (!d0 || !d1) return;
      var ratio = d1 / d0, steps = 0;
      if (ratio > 1.05) steps = Math.round(Math.log(ratio) / Math.log(1.15));
      else if (ratio < 0.95) steps = -Math.round(Math.log(1 / ratio) / Math.log(1.15));
      if (steps) _fontStep(steps * STEP);   // _fontStep 内部已 clamp 70-220
    }
    if (!content) return;
    var d0 = 0, dLast = 0;
    content.addEventListener('touchstart', function (e) { if (e.touches && e.touches.length === 2) { var a = e.touches[0], b = e.touches[1]; d0 = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) || 0; dLast = d0; } }, { passive: true });
    content.addEventListener('touchmove', function (e) { if (d0 && e.touches && e.touches.length === 2) { var a = e.touches[0], b = e.touches[1]; dLast = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY) || dLast; if (e.cancelable) { try { e.preventDefault(); } catch (_) {} } } }, { passive: false });
    var endf = function () { if (d0 && dLast) applyPinch(d0, dLast); d0 = 0; dLast = 0; };
    content.addEventListener('touchend', function (e) { if (d0 && (!e.touches || e.touches.length < 2)) endf(); }, { passive: true });
    content.addEventListener('touchcancel', endf, { passive: true });
    content.addEventListener('wheel', function (e) { if (!e.ctrlKey) return; if (e.cancelable) { try { e.preventDefault(); } catch (_) {} } _fontStep(e.deltaY < 0 ? STEP : -STEP); }, { passive: false });
  })();
  // ── NotebookLM 式收藏集一键入口:**仅收藏夹(IS_FAV_BOOK)** 在助手快捷区置顶注入 3 个「整集级」动作 ──
  //   普通 EPUB 书 / PDF 阅读器都不注入 → 零影响。按钮走既有 data-send → 上面那个 #ep-asst-quick 委托 handler
  //   自动 sendChat 发预设 prompt(无需另接);它们住在 asst pane 的快捷区 = 侧栏助手打开时才可见/可点。
  //   预设 prompt 明确引导 AI 用后端注入的【收藏集目录】通览全集 + 按需 read_source_page 翻原书查证
  //   (收藏集识别/目录/工具全在 epub_assistant.py 的收藏集分支,后端零改)。
  function _favNotebookEntries() {   // ③-4c:IIFE→具名函数,共享侧栏挂载后(文件末 flag 块)重注入 #asst-quick(内联 quick 区随 pane 被摘)
    if (!IS_FAV_BOOK) return;
    var quick = document.getElementById('ep-asst-quick') || document.getElementById('asst-quick');   // 共享侧栏快捷区 id=asst-quick
    if (!quick || quick.querySelector('.asst-fav-nb')) return;   // 幂等
    if (!document.getElementById('ep-fav-nb-css')) {
      var st = document.createElement('style'); st.id = 'ep-fav-nb-css';
      st.textContent = '#ep-asst-quick button.asst-fav-nb,#asst-quick button.asst-fav-nb{background:#241a3a;border-color:#4a3a7d;color:#dcc9ff}' +
        '#ep-asst-quick button.asst-fav-nb:active,#asst-quick button.asst-fav-nb:active{background:#33285a}';
      document.head.appendChild(st);
    }
    var defs = [
      ['📋 总结整本', '基于这个收藏集里的所有条目,给我一份整体总结:先用一两句话概括整个收藏集围绕的主题,再逐条说明每条讲了什么。请先看【收藏集目录】通览全貌;某条需要更多细节时用 read_source_page 翻原书。'],
      ['🔗 串联要点', '把这个收藏集各条目的要点串成一条逻辑线:说明它们之间怎么关联、如何递进或呼应,最后用一句话点出主线。请参照【收藏集目录】通览各条;关键处可用 read_source_page 回原书核对。'],
      ['💡 找共同点', '找出这个收藏集各条目之间的共同主题、概念或联系,逐个列出,并指明每个共同点分别体现在哪几条(引用具体条目)。请基于【收藏集目录】通览全集;有疑问用 read_source_page 翻原书查证。']
    ];
    var anchor = quick.firstChild;   // 置顶:整集级动作排在「本章」级快捷按钮之前
    defs.forEach(function (d) {
      var b = document.createElement('button');
      b.className = 'asst-fav-nb'; b.textContent = d[0]; b.setAttribute('data-send', d[1]);
      quick.insertBefore(b, anchor);
    });
  }
  _favNotebookEntries();
  $('ep-fs-up').addEventListener('click', function () { st.fs = Math.min(220, st.fs + 10); localStorage.setItem(LS.fs, st.fs); applyStyle(); refreshSet(); });
  $('ep-fs-dn').addEventListener('click', function () { st.fs = Math.max(70, st.fs - 10); localStorage.setItem(LS.fs, st.fs); applyStyle(); refreshSet(); });
  $('ep-lh-up').addEventListener('click', function () { st.lh = Math.min(2.4, +(st.lh + 0.1).toFixed(1)); localStorage.setItem(LS.lh, st.lh); applyStyle(); refreshSet(); });
  $('ep-lh-dn').addEventListener('click', function () { st.lh = Math.max(1.0, +(st.lh - 0.1).toFixed(1)); localStorage.setItem(LS.lh, st.lh); applyStyle(); refreshSet(); });
  $('ep-mw-up').addEventListener('click', function () { st.mw = Math.min(100, st.mw + 6); localStorage.setItem(LS.mw, st.mw); applyStyle(); refreshSet(); });   // 栏宽 + = 文本更宽、两边页边距更小
  $('ep-mw-dn').addEventListener('click', function () { st.mw = Math.max(40, st.mw - 6); localStorage.setItem(LS.mw, st.mw); applyStyle(); refreshSet(); });
  [].forEach.call($('ep-theme').children, function (b) { b.addEventListener('click', function () { st.th = b.dataset.th; localStorage.setItem(LS.th, st.th); applyStyle(); refreshSet(); }); });
  var fullBtn = $('ep-full-btn');
  fullBtn.addEventListener('click', function () { fullBtn.disabled = true; fullBtn.textContent = '⏳ 处理中…'; reqJson('POST', '/pdf/api/epub-to-full', { file: FREL }, function (d) { if (d.ready) { location.href = d.view_url; return; } pollFull(d.job, d.view_url); }, function (er) { fullBtn.textContent = '✗ ' + er; fullBtn.disabled = false; }); });
  function pollFull(job, url) { var iv = setInterval(function () { fetch('/pdf/api/ebook-convert-status?job=' + encodeURIComponent(job)).then(function (r) { return r.json(); }).then(function (s) { if (s.status === 'done') { clearInterval(iv); location.href = s.view_url || url; } else if (s.status === 'error') { clearInterval(iv); fullBtn.textContent = '✗ 转换失败'; fullBtn.disabled = false; } else fullBtn.textContent = '⏳ 转换中(可关页面)…'; }).catch(function () {}); }, 5000); }

  // ── 网络 ──
  function reqJson(method, url, body, ok, err) { var o = { method: method, headers: {} }; if (body) { o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(body); } fetch(url, o).then(function (r) { return r.json(); }).then(function (d) { if (d && d.ok) ok(d); else err((d && d.error) || '失败'); }).catch(function (e) { err(e.message || '网络错误'); }); }
  function postJson(url, body, ok, err) { reqJson('POST', url, body, ok, err); }
  async function sse(url, body, onDelta, onDone, onErr) {
    try { var res = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' }, body: JSON.stringify(body) });
      if (!res.ok || !res.body) { onErr('HTTP ' + res.status); return; }
      var reader = res.body.getReader(), dec = new TextDecoder(), buf = '';
      while (true) { var rd = await reader.read(); if (rd.done) break; buf += dec.decode(rd.value, { stream: true }); var idx;
        while ((idx = buf.indexOf('\n\n')) >= 0) { var chunk = buf.slice(0, idx); buf = buf.slice(idx + 2); var ev = 'message', data = '';
          chunk.split('\n').forEach(function (line) { if (line.indexOf('event:') === 0) ev = line.slice(6).trim(); else if (line.indexOf('data:') === 0) data += line.slice(5).trim(); });
          if (ev === 'error') { try { onErr(JSON.parse(data).error); } catch (e) { onErr('AI 失败'); } return; }
          if (ev === 'done') { onDone(); return; }
          if (data) { try { var d = JSON.parse(data); if (d.text) onDelta(d.text); } catch (e) {} } } }
      onDone();
    } catch (e) { onErr(e.message || '连接失败'); }
  }
  var _tT;
  function toast(msg) { var el = $('ep-toast'); if (!el) { el = document.createElement('div'); el.id = 'ep-toast'; el.style.cssText = 'position:fixed;left:50%;bottom:44px;transform:translateX(-50%);background:#10162a;border:1px solid #3b6db5;color:#cfe6ff;padding:8px 16px;border-radius:8px;font-size:13px;z-index:95;box-shadow:0 6px 16px rgba(0,0,0,.6);transition:opacity .2s'; document.body.appendChild(el); } el.textContent = msg; el.style.opacity = '1'; clearTimeout(_tT); _tT = setTimeout(function () { el.style.opacity = '0'; }, 1400); }
  function showErr(m) { try { _settleReveal(); } catch (e) {} var el = $('ep-load'); if (el) el.innerHTML = '<div style="color:#ff9a9a">✗ ' + m + '</div><a href="/pdf/">← 返回书架</a>'; }
  function hideLoad() { var el = $('ep-load'); if (el) el.style.display = 'none'; }

  var DBG = location.search.indexOf('dbg=1') >= 0 || localStorage.getItem('eph-debug') === '1';   // ?dbg=1 或设置面板 debug 开关
  function dbg(m) { if (!DBG) return; try { fetch('/pdf/api/epub-dbg', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ msg: '[html] ' + m }), keepalive: true }).catch(function () {}); } catch (e) {} }
  window.__epdbg = dbg;

  // ════════ Phase G:振假名(あ) / 译页 / 生词下划线 ════════(只处理可视已加载 section、幂等、off 干净还原、不改可见字符数)
  var _deco = { ruby: false, pagetr: false };
  function _decoCandidates(secEl, mode) {
    var w = document.createTreeWalker(secEl, NodeFilter.SHOW_TEXT, null), n, out = [];
    while ((n = w.nextNode())) {
      if (!_countable(n)) continue;
      var v = n.nodeValue; if (!v || !v.trim()) continue;
      var p = n.parentElement; if (!p) continue;
      if (p.closest('mark')) continue;
      if (p.closest('ruby')) continue;
      if (mode === 'vocab' && p.closest('.ep-vocab-und')) continue;
      out.push(n);
    }
    return out;
  }
  function _visibleLoadedSecs() {
    var vh = window.innerHeight || 800, M = 700, out = [];
    var n = secEls.length;
    if (!n) return out;
    // _curTopIdx is maintained by the O(1) neighbour walk in onScroll. Start
    // there and stop as soon as each side leaves the viewport margin. A long
    // reading session can leave hundreds of earlier sections loaded; scanning
    // all of them here after every scroll forced a full layout walk and made
    // EPUB progressively slower.
    var anchor = Math.min(Math.max(_curTopIdx | 0, 0), n - 1);
    for (var i = anchor; i < n; i++) {
      var forward = secEls[i];
      if (!forward) continue;
      var fr = forward.getBoundingClientRect();
      if (fr.top >= vh + M) break;
      if (loaded[i] === true && fr.bottom > -M) out.push(forward);
    }
    for (var j = anchor - 1; j >= 0; j--) {
      var backward = secEls[j];
      if (!backward) continue;
      var br = backward.getBoundingClientRect();
      if (br.bottom <= -M) break;
      if (loaded[j] === true && br.top < vh + M) out.unshift(backward);
    }
    return out;
  }
  function _decorateSection(el) {
    if (_vocabOn() && _vocabMap) _vocabApplySection(el);
    if (_deco.ruby) _rubyApplySection(el);
    else if (_deco.pagetr) _pagetrApplySection(el);
  }
  function _decorateVisible() { _visibleLoadedSecs().forEach(_decorateSection); }
  var _decoT;
  function _decoSchedule() {
    // The listener is always installed. Before the vocabulary map exists (or
    // when underlines are disabled), no decoration mode means there is no
    // useful layout work to schedule.
    if ((!_vocabOn() || !_vocabMap) && !_deco.ruby && !_deco.pagetr) return;
    clearTimeout(_decoT); _decoT = setTimeout(_decorateVisible, 250);
  }
  function _wrapTokens(node, toks, makeWrap) {
    var sorted = toks.slice().filter(function (t) { return t.end > t.start; }).sort(function (a, b) { return a.start - b.start; });
    function appendExtra(wrap, t) { if (t.__rt != null) { var rt = document.createElement('rt'); rt.className = 'ep-rt'; rt.dataset.eph = '1'; rt.textContent = t.__rt; wrap.appendChild(rt); } }
    for (var k = sorted.length - 1; k >= 0; k--) {
      var t = sorted[k];
      try {
        var s = Math.max(0, t.start), e = Math.min(node.nodeValue.length, t.end);
        if (e <= s) continue;
        var after = node.splitText(s); after.splitText(e - s);
        var wrap = makeWrap(t, after);
        if (wrap) { after.parentNode.insertBefore(wrap, after); wrap.appendChild(after); appendExtra(wrap, t); }
      } catch (_) {}
    }
  }
  function _unwrap(parentNormalize) {
    return function (wrapEl) {
      var p = wrapEl.parentNode; if (!p) return;
      while (wrapEl.firstChild) {
        var c = wrapEl.firstChild;
        if (c.nodeType === 1 && c.tagName === 'RT') { wrapEl.removeChild(c); continue; }
        p.insertBefore(c, wrapEl);
      }
      p.removeChild(wrapEl); if (parentNormalize) p.normalize();
    };
  }
  function _rubyApplySection(secEl) {
    if (!_deco.ruby || secEl.dataset.epRuby === '1') return;
    secEl.dataset.epRuby = '1';
    var nodes = _decoCandidates(secEl, 'ruby').filter(function (n) { return /[㐀-鿿一-鿿A-Za-z]/.test(n.nodeValue); });
    if (!nodes.length) return;
    reqJson('POST', '/pdf/api/epub-furigana', { texts: nodes.map(function (n) { return n.nodeValue; }) }, function (d) {
      if (!_deco.ruby) { secEl.dataset.epRuby = ''; return; }
      var items = d.items || [];
      nodes.forEach(function (n, i) {
        var toks = ((items[i] && items[i].tokens) || []).filter(function (t) { return t.reading; })
          .map(function (t) { return { start: t.start, end: t.end, __rt: t.reading }; });
        if (toks.length) _wrapTokens(n, toks, function () { var r = document.createElement('ruby'); r.dataset.eph = '1'; return r; });
      });
      _rubyVerifySection(secEl);   // 渲染完成后台 AI 按上下文校正读音(量词/熟字訓/多音字),不阻塞、失败静默
    }, function () { secEl.dataset.epRuby = ''; });
  }
  // 振假名 AI 校正:整段上下文 + 已渲染 ruby 列表送 /api/epub-furigana-verify,拿 fixes 原地替换 <rt> 文本。
  // 照搬 PDF 09-ruby _verifyFurigana 的"不阻塞渲染、每段只调一次、失败静默"原则;字段用 wd(与后端 _word_of 对齐,
  // 不是 epub2-deco.js 那份用的 'base' —— 那个键后端认不出,校正永远是空操作,这里不重蹈)。
  function _rubyVerifySection(secEl) {
    if (secEl.dataset.epRubyVer === '1') return;
    var rtEls = Array.prototype.slice.call(secEl.querySelectorAll('ruby[data-eph="1"] > rt.ep-rt'));
    if (!rtEls.length) return;
    secEl.dataset.epRubyVer = '1';
    var readings = rtEls.map(function (rt) {
      var wd = '', rb = rt.parentElement;
      if (rb) { for (var c = rb.firstChild; c; c = c.nextSibling) { if (c.nodeType === 3) wd += c.nodeValue; } }
      return { wd: wd, rt: rt.textContent || '' };
    });
    reqJson('POST', '/pdf/api/epub-furigana-verify', { file: FREL, text: (secEl.textContent || '').slice(0, 4000), readings: readings }, function (d) {
      if (!_deco.ruby) return;
      var fixed = d.readings || [];
      if (fixed.length === rtEls.length) {
        rtEls.forEach(function (rt, i) {
          var nr = fixed[i], s = (nr && typeof nr === 'object') ? (nr.rt != null ? nr.rt : nr.reading) : nr;
          if (s != null && s !== '' && s !== rt.textContent) rt.textContent = s;
        });
      } else if ((d.fixes || []).length) {
        d.fixes.forEach(function (f) { var rt = rtEls[f.i]; if (rt && f.r != null && rt.textContent !== f.r) rt.textContent = f.r; });
      }
    }, function () { secEl.dataset.epRubyVer = ''; });
  }
  function _rubyClearAll() {
    col.querySelectorAll('ruby[data-eph="1"]').forEach(_unwrap(true));
    secEls.forEach(function (el) { el.dataset.epRuby = ''; el.dataset.epRubyVer = ''; });
  }
  function _pagetrApplySection(secEl) {
    if (!_deco.pagetr || secEl.dataset.epTr === '1') return;
    secEl.dataset.epTr = '1';
    var blocks = [].slice.call(secEl.querySelectorAll('p,li,blockquote')).filter(function (b) {
      return !b.dataset.epTr && !b.querySelector('p,li,blockquote') && _countableText(b).trim();
    });
    if (!blocks.length) return;
    reqJson('POST', '/pdf/api/epub-translate-section', { file: FREL, texts: blocks.map(function (b) { return _countableText(b).trim(); }) }, function (d) {
      if (!_deco.pagetr) { secEl.dataset.epTr = ''; return; }
      var tr = d.translations || [];
      blocks.forEach(function (b, i) {
        if (b.dataset.epTr === '1') return;
        var zh = (tr[i] || '').trim(); if (!zh) return;
        b.dataset.epTr = '1';
        var div = document.createElement('div'); div.className = 'ep-tr-rt'; div.textContent = zh;
        b.parentNode.insertBefore(div, b.nextSibling);
      });
    }, function () { secEl.dataset.epTr = ''; });
  }
  function _pagetrClearAll() {
    col.querySelectorAll('.ep-tr-rt').forEach(function (el) { el.remove(); });
    col.querySelectorAll('[data-ep-tr="1"]').forEach(function (b) { b.removeAttribute('data-ep-tr'); });
    secEls.forEach(function (el) { el.dataset.epTr = ''; });
  }
  var _vocabMap = null;
  var _vocabLocalCandidates = Object.create(null);
  function _epVocabularyStateRepo() {
    try {
      var repo = window.BWReaderRuntime && window.BWReaderRuntime.vocabularyState;
      return repo &&
        repo.CONTRACT === 'vocabulary-state/1' &&
        typeof repo.isMastered === 'function'
        ? repo
        : null;
    } catch (e) { return null; }
  }
  function _epVocabularyStateMastered(kind, language, surface, lemma, forms) {
    var repo = _epVocabularyStateRepo();
    if (!repo) return false;
    surface = String(surface || '').trim();
    lemma = String(lemma || surface).trim();
    if (!surface && !lemma) return false;
    try {
      return repo.isMastered({
        kind: kind || 'word',
        language: language || 'und',
        lemma: lemma || surface,
        word: surface || lemma,
        surface: surface || lemma,
        text: surface || lemma,
        forms: Array.isArray(forms) ? forms : []
      });
    } catch (e) { return false; }
  }
  var _vocabMasteredLocal = (function () {
    try {
      var raw = JSON.parse(localStorage.getItem('vocab-mastered-v1') || 'null');
      return new Set(raw && Array.isArray(raw.set) ? raw.set.map(function (v) { return String(v).toLowerCase(); }) : []);
    } catch (e) { return new Set(); }
  })();
  function _saveVocabMasteredLocal() {
    try {
      localStorage.setItem('vocab-mastered-v1', JSON.stringify({
        ts: Date.now(), set: Array.from(_vocabMasteredLocal)
      }));
    } catch (e) {}
  }
  function _vocabOn() { var v = localStorage.getItem('eph-vocab-underline'); return v === null ? true : v === '1'; }
  function _loadVocabMap() {
    if (_vocabMap || !_vocabOn()) return;
    fetch('/pdf/api/vocab-mastery-map?file=' + encodeURIComponent(FREL)).then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.ok) {
        _vocabMap = Object.assign({}, d.map || {}, _vocabLocalCandidates);
        _decorateVisible();
      }
    }).catch(function () {});
  }
  // 从位置 i 起在收藏词组里找最长匹配(拉丁大小写不敏感)。reflow 适配 PDF 后端 _merge_favorite_phrases:
  // 收藏词组当一个分词单元(子词不再各自下划线 / 整段当一个生词单元)。
  function _favPhraseAt(s, i, favs) {
    var best = null;
    for (var k = 0; k < favs.length; k++) {
      var ph = favs[k]; if (!ph) continue;
      var len = ph.length;
      if (len < 1 || i + len > s.length) continue;
      var seg = s.substr(i, len);
      if (seg === ph || seg.toLowerCase() === ph.toLowerCase()) { if (!best || len > best.end - best.start) best = { start: i, end: i + len }; }
    }
    return best;
  }
  function _vocabTokens(s) {
    var out = [], i = 0, L = s.length;
    // 按「需要翻译的语言」门控:没勾的语言=母语,不标生词下划线(中文汉字落在 ja 正则范围,必须靠这层 gate 不被划线)
    var wantEn = BOOK_LANGS.indexOf('en') >= 0, wantJa = BOOK_LANGS.indexOf('ja') >= 0;
    if (!wantEn && !wantJa) return out;
    var en = function (c) { return /[A-Za-z]/.test(c); }, ja = function (c) { return /[぀-ヿ㐀-鿿一-鿿]/.test(c); };
    var favs = (wantJa && window.RC && RC.phrasepop && RC.phrasepop.favList) ? RC.phrasepop.favList() : [];   // 收藏词组优先最长匹配(日语分词)
    while (i < L) {
      if (favs.length) {
        var fh = _favPhraseAt(s, i, favs);
        if (fh) {
          var key = s.slice(fh.start, fh.end);
          var info0 = _vocabMap[key] || _vocabMap[key.toLowerCase()];
          var k0 = key.toLowerCase(), l0 = String((info0 && info0.lemma) || k0).toLowerCase();
          var lang0 = /[぀-ヿ㐀-鿿一-鿿]/.test(key) ? 'ja' : 'en';
          if (info0 && info0.label &&
              !_epVocabularyStateMastered('phrase', lang0, key, l0) &&
              !_vocabMasteredLocal.has(k0) && !_vocabMasteredLocal.has(l0)) {
            out.push({ start: fh.start, end: fh.end, label: info0.label });
          }
          i = fh.end; continue;
        }   // 整段消费(收藏词组=一个分词单元)
      }
      var c = s[i];
      if (wantEn && en(c)) {
        var j = i + 1; while (j < L && /[A-Za-z'’\-]/.test(s[j])) j++;
        var surface = s.slice(i, j).toLowerCase(), info = _vocabMap[surface];
        var infoLemma = String((info && info.lemma) || surface).toLowerCase();
        if (info && info.label &&
            !_epVocabularyStateMastered('word', 'en', surface, infoLemma) &&
            !_vocabMasteredLocal.has(surface) && !_vocabMasteredLocal.has(infoLemma)) {
          out.push({ start: i, end: j, label: info.label });
        }
        i = j; continue;
      }
      if (wantJa && ja(c)) {
        var hit = null, maxL = Math.min(12, L - i);
        for (var len = maxL; len >= 1; len--) {
          var surfaceJa = s.slice(i, i + len), inf = _vocabMap[surfaceJa];
          var lemmaJa = String((inf && inf.lemma) || surfaceJa).toLowerCase();
          if (inf && inf.label &&
              !_epVocabularyStateMastered('word', 'ja', surfaceJa, lemmaJa) &&
              !_vocabMasteredLocal.has(surfaceJa.toLowerCase()) && !_vocabMasteredLocal.has(lemmaJa)) {
            hit = { start: i, end: i + len, label: inf.label }; break;
          }
        }
        if (hit) { out.push(hit); i = hit.end; continue; }
        i++; continue;
      }
      i++;
    }
    return out;
  }
  function _vocabApplySection(secEl) {
    if (!_vocabOn() || !_vocabMap || secEl.dataset.epVocab === '1') return;
    secEl.dataset.epVocab = '1';
    _decoCandidates(secEl, 'vocab').forEach(function (n) {
      var toks = _vocabTokens(n.nodeValue);
      if (toks.length) _wrapTokens(n, toks, function (t) { var sp = document.createElement('span'); sp.className = 'ep-vocab-und m-' + t.label; return sp; });
    });
  }
  function _vocabClearAll() {
    col.querySelectorAll('span.ep-vocab-und').forEach(_unwrap(true));
    secEls.forEach(function (el) { el.dataset.epVocab = ''; });
  }
  $('ep-ruby').addEventListener('click', function () {
    _deco.ruby = !_deco.ruby; this.classList.toggle('active', _deco.ruby);
    if (_deco.ruby && _deco.pagetr) { _deco.pagetr = false; $('ep-pagetr').classList.remove('active'); _pagetrClearAll(); }
    if (_deco.ruby) { toast('振假名 / 音标 已开'); _decorateVisible(); } else _rubyClearAll();
  });
  $('ep-pagetr').addEventListener('click', function () {
    _deco.pagetr = !_deco.pagetr; this.classList.toggle('active', _deco.pagetr);
    if (_deco.pagetr && _deco.ruby) { _deco.ruby = false; $('ep-ruby').classList.remove('active'); _rubyClearAll(); }
    if (_deco.pagetr) { toast('整页翻译 已开，翻译中…'); _decorateVisible(); } else _pagetrClearAll();
  });
  function _setVocabUnderline(on) {
    try { localStorage.setItem('eph-vocab-underline', on ? '1' : '0'); } catch (_) {}
    if (on) { if (!_vocabMap) _loadVocabMap(); else _decorateVisible(); } else _vocabClearAll();
  }
  window.__epSetVocabUnderline = _setVocabUnderline;
  // 查词/标掌握后实时刷新生词下划线(照搬 PDF refreshVocabUnderlinesForAllPages):重取 map + 重画可视章。
  // 顺带重刷振假名:清 epRuby → 对可视章重新 _rubyApplySection(后端 /api/epub-furigana 已 live 过滤已掌握词)
  //   → **已掌握的词假名当下就消失**,跟 PDF 一致(mastery 判定统一在后端 _word_mastered)。
  window.refreshVocabUnderlinesForAllPages = function () {
    _vocabMap = null; _vocabClearAll();
    if (_deco.ruby) _rubyClearAll();
    _loadVocabMap();
    if (_deco.ruby) _decorateVisible();   // vocab 因 map=null 暂跳过(等 _loadVocabMap 回调);ruby 因 epRuby 已清会重拉
  };
  function _rerenderVisibleVocab() {
    _visibleLoadedSecs().forEach(function (secEl) {
      secEl.querySelectorAll('span.ep-vocab-und').forEach(_unwrap(true));
      secEl.dataset.epVocab = '';
      _vocabApplySection(secEl);
    });
  }
  // IndexedDB/Vault hydrate 可能晚于章节首绘；仓库变化只重用手头 mastery map 重画，
  // 不清 map、不访问服务器。仓库缺失时旧 localStorage/server 路径保持原样。
  (function _bindEpVocabularyStateProjection() {
    var repo = _epVocabularyStateRepo();
    if (!repo) return;
    var queued = false;
    function paint() {
      queued = false;
      if (_vocabMap && _vocabOn()) _rerenderVisibleVocab();
    }
    function schedule() {
      if (queued) return;
      queued = true;
      if (window.requestAnimationFrame) window.requestAnimationFrame(paint);
      else setTimeout(paint, 0);
    }
    try {
      if (typeof repo.subscribe === 'function') {
        repo.subscribe(function (event) {
          var record = event && event.record;
          if (!record || record.property === 'mastered') schedule();
        });
      }
    } catch (e) {}
    try {
      if (typeof repo.ready === 'function') Promise.resolve(repo.ready()).then(schedule, function () {});
    } catch (e) {}
    try { document.addEventListener('bw:vocabulary-state-ready', schedule); } catch (e) {}
  })();
  // rc-wordpop 的统一 mastery 本地投影。已有 span 与新补候选都在当前帧重画，
  // 不清空 _vocabMap、不 GET mastery-map；服务端响应只负责后台同步。
  window.applyVocabLocalOverride = function (lemma, mastered, meta) {
    var values = [lemma, meta && meta.word].concat((meta && meta.forms) || [])
      .filter(Boolean).map(function (value) { return String(value).trim().toLowerCase(); });
    var keys = Array.from(new Set(values));
    if (!keys.length) return function () {};
    var oldMastered = keys.map(function (key) { return [key, _vocabMasteredLocal.has(key)]; });
    var oldMap = keys.map(function (key) {
      return [key, Object.prototype.hasOwnProperty.call(_vocabMap || {}, key), _vocabMap && _vocabMap[key]];
    });
    var oldCandidates = keys.map(function (key) {
      return [key, Object.prototype.hasOwnProperty.call(_vocabLocalCandidates, key), _vocabLocalCandidates[key]];
    });
    keys.forEach(function (key) {
      if (mastered) _vocabMasteredLocal.add(key);
      else {
        _vocabMasteredLocal.delete(key);
        _vocabMap = _vocabMap || {};
        if (!_vocabMap[key]) {
          _vocabLocalCandidates[key] = { label: 'known', mastery: 0.8, lemma: String(lemma || key).toLowerCase() };
          _vocabMap[key] = _vocabLocalCandidates[key];
        }
      }
    });
    _saveVocabMasteredLocal();
    _rerenderVisibleVocab();
    return function restoreEpubVocabOverride() {
      oldMastered.forEach(function (entry) {
        if (entry[1]) _vocabMasteredLocal.add(entry[0]); else _vocabMasteredLocal.delete(entry[0]);
      });
      oldMap.forEach(function (entry) {
        if (entry[1]) _vocabMap[entry[0]] = entry[2]; else if (_vocabMap) delete _vocabMap[entry[0]];
      });
      oldCandidates.forEach(function (entry) {
        if (entry[1]) _vocabLocalCandidates[entry[0]] = entry[2];
        else delete _vocabLocalCandidates[entry[0]];
      });
      _saveVocabMasteredLocal();
      _rerenderVisibleVocab();
    };
  };
  // 乐观去下划线(rc-wordpop.js「☆ 标记掌握」调 __epubDeco.optimisticMaster):点掌握后立刻把该词的
  // 生词下划线隐掉(加 class,不动 DOM 结构),不等服务端 vocab-mark + map 刷新;失败调返回的 restore() 撤销。
  // 逐字照搬 epubjs 引擎的已验证实现(epub2-deco.js optimisticMaster,含 ep2-und-opt-off 同名 class,
  // CSS 在 epub_html_reader.html);服务端落库后 refreshVocabUnderlinesForAllPages 重画时该词已不在 map → 自然兜底。
  // 语义对齐 PDF 原生 15-phrase-wordpop.js _dropVocabUnderlineOptimistic(乐观即时消失 + 失败回滚 + 刷新校正)。
  window.__epubDeco = {
    optimisticMaster: function (word) {
      var lower = String(word || '').trim().toLowerCase();
      if (!lower) return function () {};
      var touched = [];
      try {
        col.querySelectorAll('span.ep-vocab-und').forEach(function (sp) {
          var t = (sp.textContent || '').trim();
          if ((t.toLowerCase() === lower || t === word) && !sp.classList.contains('ep2-und-opt-off')) {
            sp.classList.add('ep2-und-opt-off'); touched.push(sp);
          }
        });
      } catch (e) {}
      return function restore() { touched.forEach(function (sp) { try { sp.classList.remove('ep2-und-opt-off'); } catch (e) {} }); };
    }
  };
  content.addEventListener('scroll', _decoSchedule, { passive: true });
  _loadVocabMap();

  // ── 单词本 pane:逐字照搬 PDF reader.src/05-nav.js(loadVocabList + 发音 + 加 Anki + 点开释义 + mastery 排序)。
  //    只改底座:scope「本页」→「本章」(reflow 无页 → 拉本书词,再用当前 section 文本里出现的词过滤);
  //    点词开释义走 EPUB 底座 RC.wordpop.show(EN 核心框可展开 / JA 完整框);加卡走 /pdf/api/vocab-anki(同 PDF)。──
  var _vocabScope = 'book', _vocabLoaded = false;
  function loadVocabPane(scope) {
    if (scope) _vocabScope = scope;
    _vocabLoaded = true;
    var listEl = $('ep-vocab-list'), cntEl = $('ep-vocab-count');
    if (!listEl) return;
    document.querySelectorAll('#ep-vocab-scope-row button').forEach(function (b) { b.classList.toggle('active', b.dataset.scope === _vocabScope); });
    listEl.innerHTML = '<div style="color:#5a6680;font-size:12px;padding:10px">加载中…</div>';
    if (cntEl) cntEl.textContent = '';
    // 后端只有 book/all(reflow 无 page);本章 = 拉本书词 + 当前 section 文本过滤
    var backendScope = (_vocabScope === 'all') ? 'all' : 'book';
    fetch('/pdf/api/vocab-list?file=' + encodeURIComponent(FREL || '') + '&scope=' + backendScope)
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var items = (d && d.items) || [];
        if (_vocabScope === 'chapter') items = _vocabFilterChapter(items);
        window.__lastVocab = items;   // 给语音助手 __voiceContext 做谐音纠错/上下文(照搬 PDF)
        if (cntEl) cntEl.textContent = items.length ? (items.length + ' 词') : '';
        if (!items.length) {
          var empty = { chapter: '本章没出现已查过的单词', book: '这本书还没查过单词', all: '单词库为空' }[_vocabScope] || '没有单词';
          listEl.innerHTML = '<div style="color:#5a6680;font-size:12px;padding:10px">' + empty + '</div>';
          return;
        }
        listEl.innerHTML = '';
        items.forEach(function (it) { listEl.appendChild(_renderVocabItem(it)); });
      })
      .catch(function (e) {
        listEl.innerHTML = '<div style="color:#ef4444;font-size:12px;padding:10px">加载失败:' + esc(e.message || '网络错误') + '</div>';
      });
  }
  window.loadVocabPane = loadVocabPane;   // HTML 内联 onclick 在全局作用域执行

  // 本章作用域:reflow 无页码,定义为「当前(顶部可见)section 文本里出现的词」(对照 PDF「本页」⊂「本书」)
  function _curSectionText() {
    var topIdx = 0;
    for (var i = 0; i < secEls.length; i++) { if (secEls[i].getBoundingClientRect().bottom > 60) { topIdx = i; break; } }
    var el = secEls[topIdx];
    return el ? (el.innerText || '') : '';
  }
  function _vocabFilterChapter(items) {
    var txt = _curSectionText(); if (!txt) return [];
    var lc = txt.toLowerCase();
    return items.filter(function (it) {
      var lem = (it.lemma || '').trim(); if (!lem) return false;
      if (/^[a-z][a-z'’\-]*$/i.test(lem)) {
        var re = lem.toLowerCase().replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        return new RegExp('\\b' + re + '\\b', 'i').test(lc);
      }
      return txt.indexOf(lem) >= 0;   // 日语/其它:子串匹配
    });
  }
  // 本章 scope:滚动切章时实时刷新(照搬 PDF goToPage→_refreshVocabIfPage 意图,reflow 改 scroll 触发,防抖)
  var _vocabRefreshT;
  function _refreshVocabIfChapter() {
    var pane = $('ep-side-vocab');
    if (!(pane && pane.classList.contains('active') && _vocabScope === 'chapter')) return;
    clearTimeout(_vocabRefreshT);
    _vocabRefreshT = setTimeout(function () { loadVocabPane(); }, 350);
  }
  content.addEventListener('scroll', _refreshVocabIfChapter, { passive: true });

  function _masteryColor(m) {   // 照搬 PDF
    if (m >= 0.8) return '#22c55e';
    if (m >= 0.5) return '#eab308';
    if (m >= 0.2) return '#f97316';
    return '#ef4444';
  }
  // 发音:日语词走浏览器原生 ja-JP TTS;英语词走有道真人 mp3,失败退化 en-US TTS(照搬 PDF _speakWord/_speakOnline/_ttsWord)
  function _isJaWordEp(w) { return /[぀-ヿ㐀-鿿一-鿿]/.test(w || ''); }
  function _speakWord(lemma, audio) {
    if (audio) {
      try {
        var a = new Audio('/pdf/api/vocab-audio?path=' + encodeURIComponent(audio));
        a.play().catch(function () { _speakOnline(lemma); });
        return;
      } catch (e) {}
    }
    _speakOnline(lemma);
  }
  function _speakOnline(w) {
    if (!w) return;
    if (_isJaWordEp(w)) { _ttsWord(w, 'ja-JP'); return; }
    try {
      var a = new Audio('https://dict.youdao.com/dictvoice?type=2&audio=' + encodeURIComponent(w));
      a.play().catch(function () { _ttsWord(w, 'en-US'); });
    } catch (e) { _ttsWord(w, 'en-US'); }
  }
  function _ttsWord(w, lang) {
    lang = lang || 'en-US';
    try {
      speechSynthesis.cancel();
      var u = new SpeechSynthesisUtterance(w);
      u.lang = lang;
      var pref = lang.slice(0, 2).toLowerCase();
      var norm = function (s) { return (s || '').toLowerCase().replace('_', '-'); };
      var vs = speechSynthesis.getVoices() || [];   // iOS 首次可能为空,getVoices 触发加载
      var v = vs.find(function (x) { return norm(x.lang) === lang.toLowerCase(); })
           || vs.find(function (x) { return norm(x.lang).indexOf(pref) === 0; });
      if (v) u.voice = v;
      speechSynthesis.speak(u);
    } catch (e) {}
  }
  function _vocabAddAnki(lemma, btn) {   // 照搬 PDF:走 /pdf/api/vocab-anki
    var old = btn.textContent;
    btn.textContent = '…'; btn.disabled = true;
    fetch('/pdf/api/vocab-anki', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ word: lemma }) })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.ok) { btn.textContent = '✓ 已加'; btn.classList.add('done'); }
        else { btn.textContent = old; btn.disabled = false; toast('加卡失败:' + ((d && d.error) || '?')); }
      })
      .catch(function (e) { btn.textContent = old; btn.disabled = false; toast('加卡失败:' + (e.message || '网络错误')); });
  }
  function _renderVocabItem(it) {   // 照搬 PDF _renderVocabItem:单词/音标/🔊朗读/掌握 badge+bar/释义/🎴加卡
    var div = document.createElement('div');
    div.className = 'vocab-item';
    var pct = Math.round((it.mastery || 0) * 100);
    var col = _masteryColor(it.mastery || 0);
    div.innerHTML =
      '<div class="vi-head">' +
        '<span class="vi-word">' + esc(it.lemma) + '</span>' +
        (it.phonetic ? '<span class="vi-phon">' + esc(it.phonetic) + '</span>' : '') +
        '<button class="vi-audio" title="发音">🔊</button>' +
        '<span class="vi-mastery-badge" style="background:' + col + '22;color:' + col + '">' + esc(it.mastery_label || (pct + '%')) + '</span>' +
      '</div>' +
      '<div class="vi-bar"><div style="width:' + pct + '%;background:' + col + '"></div></div>' +
      (it.zh ? '<div class="vi-zh">' + esc(it.zh) + '</div>' : '') +
      '<div class="vi-foot">' +
        '<span class="vi-pages"></span>' +   // reflow 无 PDF 页码 → 空占位撑开,Anki 右对齐(PDF 的 p{N} 跳转在 EPUB 无意义)
        '<button class="vi-anki">' + (it.has_card ? '✓ 已加' : '📇 加卡') + '</button>' +
      '</div>';
    if (it.has_card) div.querySelector('.vi-anki').classList.add('done');
    div.querySelector('.vi-word').addEventListener('click', function () {
      var rect = this.getBoundingClientRect();   // 底座:PDF dictStream → EPUB RC.wordpop.show(EN 核心框/JA 完整框,带 file=FREL 记暴露)
      if (window.RC && RC.wordpop) RC.wordpop.show({ word: it.lemma, rect: rect, file: FREL, langs: bookLangsArr() });
    });
    div.querySelector('.vi-audio').addEventListener('click', function (e) { e.stopPropagation(); _speakWord(it.lemma, it.audio); });
    div.querySelector('.vi-anki').addEventListener('click', function (e) {
      e.stopPropagation();
      var b = e.currentTarget;
      if (!b.classList.contains('done')) _vocabAddAnki(it.lemma, b);
    });
    return div;
  }

  // ════════════════ 手写墨迹层 ════════════════
  // 照搬 PDF 阅读器(static/pdf/pdf-tail.js 的 _ink*;2026-07-06 起从模板内联抽出到该文件)
  // 每章一块 canvas(absolute 填满本章),坐标归一化 0-1(相对本章内容盒)。改字号/行距 → 本章盒尺寸变 →
  // ResizeObserver 触发按比例重绘(墨迹随盒等比缩放;font-size 变了对齐有偏移是 reflow 固有限制,可接受)。
  // canvas 永远 pointer-events:none(纯显示);绘制靠 #ep-col 上的 capture pointerdown 委托拦截。
  var _epInk = {
    tool: 'pen', color: '#e74c3c', width: 2.5,
    mode: false,        // 桌面/触屏手写模式(鼠标/手指接管);Apple Pencil 始终可画
    visible: true,
    data: {},           // {idx: [stroke,...]} 服务端加载
    drawing: null,      // {el, idx, stroke|eraser, snap, raf}
    lastEl: null,       // 最近绘制的章(撤销/清空作用对象)
    saveTimers: {},
    _lastTap: null, quickErase: false, _revertT: null, _prevTool: 'pen',
    ro: null, liveCv: null,   // liveCv:正在画的笔画专用「视口固定」叠加层(避免每帧在整章主 canvas 上 putImageData → 卡顿根因)
    paletteAnchor: null,      // 最近 Pencil hover/落笔的视口坐标；工具栏打开时跟随到其上方
    _paletteRaf: null,
  };
  window._epInk = _epInk;
  _epInk.fileOf = {};   // idx(uid) → 原书文件(收藏夹自建页跨书写原书墨迹);缺省 = FREL

  // 收藏夹自建页:墨迹画在 .fav-item-userpage 页体(与原书 .ep-usec 同 86vh 几何 → 坐标对齐 + 跨书写原书),不画在整个 fav 章
  function _favUpElIn(secEl) { return (secEl && secEl.querySelector) ? secEl.querySelector('.ep-usec[data-uid], .fav-pdf-page[data-favpdf-file]') : null; }   // 收藏夹自建页(.ep-usec)/PDF 页(.fav-pdf-page):ink canvas 在它上 → Clear/Undo/_inkActiveEl 拿它,不是外层容器(审查:否则清空会写空覆盖原书墨迹)
  function _inkActiveEl() {
    if (_epInk.lastEl && document.body.contains(_epInk.lastEl)) return _epInk.lastEl;
    var vh = window.innerHeight || 800, mid = vh / 2;
    for (var i = 0; i < secEls.length; i++) {
      if (loaded[i] !== true) continue;
      var r = secEls[i].getBoundingClientRect();
      if (r.top <= mid && r.bottom >= mid) return _favUpElIn(secEls[i]) || secEls[i];
    }
    var last = secEls[_curTopIdx];
    return (last && _favUpElIn(last)) || last || null;
  }
  function _inkStrokesOf(el) { if (!el.__inkStrokes) el.__inkStrokes = []; return el.__inkStrokes; }
  // 墨迹 idx:正文章节 = dataset.idx(非负整数);插入页(.ep-usec)/收藏夹自建页(.fav-item-userpage)= dataset.uid(u_* 字符串,独立编号空间)。
  //   _epInk.data/saveTimers/dirty 都以此为对象键(字符串键天然共存),/api/epub-ink 后端已放行 u_* 字符串 idx。
  function _inkFileOf(idx) { return (_epInk.fileOf && _epInk.fileOf[idx]) || FREL; }   // 收藏夹自建页:该 uid 绑原书文件 → 墨迹读写落原书(双向);其余走 FREL
  function _inkIdxOf(el) {
    if (el && el.dataset && el.dataset.favpdfFile) return 'pdf|' + el.dataset.favpdfFile + '|' + el.dataset.favpdfPage;   // 收藏夹 PDF 页:键=pdf|原书|页号 → 存取路由原书 /api/ink(同一张纸)
    return (el && el.dataset && el.dataset.uid) ? el.dataset.uid : parseInt(el.dataset.idx, 10);
  }
  function _voicePageOfInkEl(el, idx) {
    try {
      var section = el && el.matches && el.matches('.ep-sec[data-idx]')
        ? el : (el && el.closest ? el.closest('.ep-sec[data-idx]') : null);
      if (section) {
        var sectionIdx = parseInt(section.dataset.idx, 10);
        if (Number.isFinite(sectionIdx)) return sectionIdx + 1;
      }
    } catch (e) {}
    if (typeof idx === 'number' && Number.isFinite(idx)) return idx + 1;
    return (_curTopIdx | 0) + 1;
  }
  // 插入页存档墨迹复原(onRender 时调):建 canvas + 重绘(_inkLoadAll 若还没回来,_inkApplyVisibleSaved 兜底)
  function _epUpApplyInk(usecEl, uid) {
    var d = _epInk.data[uid];
    if (usecEl.__inkCv) { _inkRedraw(usecEl); return; }   // 已建 → 内容重排后重绘
    if ((d && d.length) || _epInk.mode) {
      if (d && d.length) usecEl.__inkStrokes = JSON.parse(JSON.stringify(d));
      _inkEnsure(usecEl, uid); _inkRedraw(usecEl);
    }
  }
  function _epUpEls() {   // 已挂载的插入页元素 [{el,id}]
    var out = [];
    try { (window.RC && RC.userpages ? (RC.userpages.pages() || []) : []).forEach(function (p) { var el = RC.userpages.elOf ? RC.userpages.elOf(p.id) : null; if (el && el.isConnected) out.push({ el: el, id: p.id }); }); } catch (e) {}
    return out;
  }

  function _inkSizeCanvas(el) {
    var cv = el.__inkCv; if (!cv) return;
    var cw = el.clientWidth, ch = el.clientHeight;
    if (cw < 1 || ch < 1) return;
    var dpr = window.devicePixelRatio || 1;
    var w = Math.max(1, Math.floor(cw * dpr)), h = Math.max(1, Math.floor(ch * dpr));
    if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
    cv.style.width = cw + 'px'; cv.style.height = ch + 'px';
  }
  // 给某章建/取墨迹 canvas(惰性:只在有墨迹 / 手写模式 / 真要画时建,几百章不全建 → 省内存)
  function _inkEnsure(el, idx) {
    if (el.__inkCv) return el.__inkCv;
    var cv = document.createElement('canvas');
    cv.className = 'ep-ink-canvas';
    el.appendChild(cv);
    // 绘图区焦点(A5):手指长按 = 设为当前焦点(再长按取消)。笔/橡皮不经过这条路径。
    try {
      window.RC && RC.outgoing && RC.outgoing.bindDrawingFocus(cv, function () {
        return { file: FREL, page: idx,
                 hasInk: !!(el.__inkStrokes && el.__inkStrokes.length) };
      });
    } catch (e) {}
    el.__inkCv = cv; el.__inkIdx = idx; (_epInk.elOf = _epInk.elOf || {})[idx] = el;   // key→el:beacon 读 live el.__inkStrokes(防 _epInk.data 被跨书/同步 clobber 后送陈旧笔画)
    if (el.__inkStrokes == null) el.__inkStrokes = (_epInk.data[idx] ? JSON.parse(JSON.stringify(_epInk.data[idx])) : []);
    _inkSizeCanvas(el);
    if (!_epInk.ro && window.ResizeObserver) {
      _epInk.ro = new ResizeObserver(function (ents) {
        ents.forEach(function (en) { var t = en.target; if (t.__inkCv) { _inkSizeCanvas(t); _inkRedraw(t); } });
      });
    }
    if (_epInk.ro) { try { _epInk.ro.observe(el); } catch (e) {} }   // reflow / 图片迟到改高 → 重排 canvas + 重绘
    _inkRedraw(el);
    return cv;
  }
  // 几何/渲染/命中/撤销栈 = 共享核心 rc-ink.js(RCInk,三阅读器唯一实现);这里只留绑定本阅读器
  // canvas 属性(__inkCv)与状态(_epInk.visible)的薄 wrapper,函数名/签名不变。
  function _inkNorm(el, cx, cy) { return RCInk.norm(el.__inkCv, cx, cy); }
  function _inkRedraw(el) {
    if (!el || !el.__inkCv) return;
    RCInk.redraw(el.__inkCv, el.__inkStrokes, _epInk.visible);
  }
  function _inkDrawStroke(ctx, s, W, H, dpr) { RCInk.drawStroke(ctx, s, W, H, dpr); }
  // ── 橡皮命中检测(归一化坐标)──
  function _inkPtSeg(p, a, b) { return RCInk.ptSeg(p, a, b); }
  function _inkHit(s, pt, thr) { return RCInk.hit(s, pt, thr); }
  function _inkEraseAt(el, pt) {
    var removed = RCInk.eraseAt(_inkStrokesOf(el), pt, 0.014);
    if (removed) _inkRedraw(el);
    return removed;
  }
  // ── undo / redo(每章)──
  function _inkPushUndo(el) { RCInk.pushUndo(el); }
  // ── 高性能 live stroke ──────────────────────────────────────────────────────
  // 已提交笔画留在各章主 canvas 不动;正在画的这一条只画在「视口固定」叠加 canvas 上(永远只视口大小→clearRect 极廉价),
  // 松手才 _inkRedraw 一次性把含新笔画的全部平滑落到主 canvas。
  // 取代原「每帧 putImageData 整张主 canvas」——EPUB 整章主 canvas 可上万 px 高,逐帧 getImageData/putImageData 是卡顿根因。
  function _inkLiveCanvas() {
    var cv = _epInk.liveCv;
    if (!cv) {
      cv = document.createElement('canvas');
      cv.className = 'ep-ink-live';
      cv.style.cssText = 'position:fixed;left:0;top:0;pointer-events:none;z-index:49;display:none';   // 视口固定;pointer-events:none 不挡操作;z-index 49<顶栏 50
      document.body.appendChild(cv);                 // 挂 body(无 transform 祖先)→ fixed 真贴视口,client 坐标直接对齐
      _epInk.liveCv = cv;
    }
    var dpr = window.devicePixelRatio || 1, iw = window.innerWidth || 1, ih = window.innerHeight || 1;
    var w = Math.max(1, Math.floor(iw * dpr)), h = Math.max(1, Math.floor(ih * dpr));
    if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }   // 随屏/转向变化重置(下次起笔时)
    cv.style.width = iw + 'px'; cv.style.height = ih + 'px';
    return cv;
  }
  function _inkLiveShow() { var cv = _inkLiveCanvas(); cv.style.display = 'block'; return cv; }
  function _inkLiveHide() {
    var cv = _epInk.liveCv; if (!cv) return;
    var ctx = cv.getContext('2d'); ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.clearRect(0, 0, cv.width, cv.height);
    cv.style.display = 'none';
  }
  // 在视口叠加层上重绘当前这一条 live stroke(d.live = 视口归一化点;复用 _inkDrawStroke 保持平滑 quadratic)
  function _inkDrawLive(d) {
    var cv = _epInk.liveCv; if (!cv) return;
    var ctx = cv.getContext('2d');
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, cv.width, cv.height);   // 只清视口大小 → 廉价,完全不碰整章主 canvas
    _inkDrawStroke(ctx, {
      t: d.stroke.t, id: d.stroke.id, createdAtEpochMs: d.stroke.createdAtEpochMs,
      c: d.stroke.c, w: d.stroke.w, p: d.live
    }, cv.width, cv.height, window.devicePixelRatio || 1);
  }
  // 手写顶栏按钮图标(SF 线条 SVG,跟随 currentColor:active/erasing 态色自动跟):笔态 / 橡皮态
  var _RC_INK_PEN = '<svg class="rc-tbi" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M19.3 7.3L16.7 4.7L4.7 16.7L4 20L7.3 19.3Z"/><path d="M7.3 19.3L4.7 16.7"/></svg>';
  var _RC_INK_ERASER = '<svg class="rc-tbi" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M19 20h-10.5l-4.21-4.3a1 1 0 0 1 0-1.41l10-10a1 1 0 0 1 1.41 0l5 5a1 1 0 0 1 0 1.41l-9.2 9.3"/><path d="M18 13.3l-6.3-6.3"/></svg>';
  var _RC_INK_REGION = '<svg class="rc-tbi" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5 6.5L11 3.5L19 7L20 15L14 20L6 18L3.5 11Z"/><path d="M5 6.5L6 18" stroke-dasharray="2 2"/></svg>';
  // 工具变更 → 工具栏按钮高亮 + 顶栏 笔/橡皮 图标 + 临时橡皮态(照搬 PDF _inkUpdateToolUI)
  function _inkUpdateToolUI() {
    var t = _epInk.tool, tb = $('ep-ink-toolbar');
    if (tb) [].forEach.call(tb.querySelectorAll('button[data-itool]'), function (b) { b.classList.toggle('on', b.dataset.itool === t); });
    var fb = $('ep-ink-btn');
    if (fb) { fb.innerHTML = (t === 'eraser') ? _RC_INK_ERASER : (t === 'region' ? _RC_INK_REGION : _RC_INK_PEN); fb.classList.toggle('ep-ink-erasing', t === 'eraser' && _epInk.quickErase); }
    try {
      if (window.__BW_NATIVE_PENCILKIT_INK__ === true) {
        var handler = window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.bwNativePencilInk;
        if (handler && handler.postMessage) handler.postMessage({ type: 'tool', tool: t === 'region' ? 'selection' : t });
      }
    } catch (_) {}
  }
  function _inkArmRevert(ms) {
    clearTimeout(_epInk._revertT);
    _epInk._revertT = setTimeout(function () {
      if (_epInk.drawing && _epInk.drawing.eraser) { _inkArmRevert(400); return; }
      _inkExitQuickErase(true, true);
    }, ms);
  }
  function _inkExitQuickErase(toPen, notify) {
    clearTimeout(_epInk._revertT); _epInk._revertT = null;
    var was = _epInk.quickErase; _epInk.quickErase = false;
    if (toPen) { _epInk.tool = _epInk._prevTool || 'pen'; _inkUpdateToolUI(); if (was && notify) { try { toast('✏️ 已回到笔'); } catch (e) {} } }
    else { _inkUpdateToolUI(); }
  }
  function _inkDoubleTapAction() {
    var value = 'eraser';
    try { value = String(localStorage.getItem('rc-ink-double-tap-action') || 'eraser'); } catch (_) {}
    return value === 'selection' || value === 'none' ? value : 'eraser';
  }
  // 手指快速双击按设置切换临时橡皮 / 共享选区笔 / 不接管。
  function _inkDoubleTapSwitch(el, requestedAction) {
    var action = requestedAction || _inkDoubleTapAction();
    if (action === 'none') return false;
    if (action === 'selection') {
      clearTimeout(_epInk._revertT); _epInk._revertT = null; _epInk.quickErase = false;
      _epInk.tool = _epInk.tool === 'region' ? 'pen' : 'region';
      _epInk._prevTool = 'pen';
      _inkUpdateToolUI();
      try { toast(_epInk.tool === 'region' ? '⬡ 已切到共享选区笔' : '✏️ 已退出共享选区笔'); } catch (_) {}
      return true;
    }
    // 手指永不画 → 双击无误点小点要撤(照搬 PDF:finger 双击 skipUndo=true,不动已有笔画)
    if (_epInk.tool === 'eraser') { _inkExitQuickErase(true, false); }   // 再次双击 → 立刻回笔(还原上一支工具)
    else { _epInk._prevTool = _epInk.tool; _epInk.tool = 'eraser'; _epInk.quickErase = true; _inkUpdateToolUI(); _inkArmRevert(2500); }   // 进临时橡皮 + 武装空闲自动回笔
    return true;
  }
  function _inkRegionId() { return 'r-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 10); }
  function _inkPlaceToolbar() { RCInk.positionToolbarAbove($('ep-ink-toolbar'), _epInk.paletteAnchor); }
  function _inkRememberPaletteAnchor(clientX, clientY) {
    if (!Number.isFinite(clientX) || !Number.isFinite(clientY)) return;
    _epInk.paletteAnchor = { x: clientX, y: clientY };
    var tb = $('ep-ink-toolbar');
    if (tb && tb.classList.contains('show') && !_epInk._paletteRaf) {
      _epInk._paletteRaf = requestAnimationFrame(function () { _epInk._paletteRaf = null; _inkPlaceToolbar(); });
    }
  }

  // ── 便签跨界路由辅助(rc-stickynote:笔尖实时位置决定写便签还是写页面,一条笔画在边界切段)──
  // 页面段收尾(切进便签前):擦边单点毛段丢弃,已画部分落主 canvas + debounce 保存。
  function _inkEndPageSeg(d) {
    if (d.raf) { cancelAnimationFrame(d.raf); d.raf = null; }
    if (!d.eraser && d.stroke) {
      if (d.stroke.t === 'pen' && d.stroke.p.length < 2) { var arr = _inkStrokesOf(d.el), i = arr.indexOf(d.stroke); if (i >= 0) arr.splice(i, 1); }
      d.stroke = null; d.live = null;
    }
    if (d.el) { _inkRedraw(d.el); _inkScheduleSave(d.el, d.idx); }
    _inkLiveHide();
  }
  // 页面开新段(出便签后):按当前点重新找章锚(可能已跨章);不在任何已加载章上 → 悬空(dangling),后续 move 再试。
  function _inkBeginPageSegAt(d, e) {
    var t = document.elementFromPoint(e.clientX, e.clientY);
    var el = t && t.closest ? t.closest('.fav-item-userpage[data-uid], .fav-pdf-page[data-favpdf-file], .ep-usec, .ep-sec') : null;   // 收藏夹自建页/PDF 页:优先命中页体子元素(键 → 墨迹写回原书)
    if (!el || el.classList.contains('ph')) { d.dangling = true; return false; }
    if (el.dataset && el.dataset.favpdfFile && !(_epInk.favPdfLoaded && _epInk.favPdfLoaded[el.dataset.favpdfFile])) { try { _favPdfInkLoad(); } catch (x) {} d.dangling = true; return false; }   // 原书墨迹没落地前禁画(整页替换会盖掉旧墨迹,审查 high)
    if (el.dataset.uid && el.dataset.inkFile) { _epInk.fileOf[el.dataset.uid] = el.dataset.inkFile; if (el.classList.contains('fav-up-editing')) { d.dangling = true; return false; } }   // 确保墨迹写回原书;正文编辑态不画
    var idx = _inkIdxOf(el);
    _inkEnsure(el, idx);
    var pt = _inkNorm(el, e.clientX, e.clientY); if (!pt) { d.dangling = true; return false; }
    d.dangling = false;
    d.el = el; d.idx = idx; d.pageTouched = true; _epInk.lastEl = el;
    _inkPushUndo(el);
    if (d.eraser) { _inkEraseAt(el, pt); return true; }
    var stroke = { t: 'pen', c: _epInk.color, w: parseFloat(_epInk.width) || 2.5, p: [pt] };
    _inkStrokesOf(el).push(stroke);
    var iw = window.innerWidth || 1, ih = window.innerHeight || 1;
    _inkLiveShow();
    d.stroke = stroke; d.live = [[e.clientX / iw, e.clientY / ih]];
    _inkDrawLive(d);
    return true;
  }

  // ── 指针绘制(委托在 #ep-col 上,capture 拦截 → 找到 .ep-sec 当锚)──
  function _inkPointerDown(e) {
    var el = e.target && e.target.closest ? e.target.closest('.fav-item-userpage[data-uid], .fav-pdf-page[data-favpdf-file], .ep-usec, .ep-sec') : null;   // 收藏夹自建页/PDF 页:优先命中页体子元素(键 → 墨迹写回原书)
    if (!el || el.classList.contains('ph')) return;
    if (el.dataset.uid && el.dataset.inkFile) {
      _epInk.fileOf[el.dataset.uid] = el.dataset.inkFile;   // 确保墨迹写回原书文件(即便 _favUpInkLoad 还没跑)
      if (el.classList.contains('fav-up-editing') || (e.target.closest && e.target.closest('.fav-up-editbtn, .fav-up-edit'))) return;   // 正文编辑态 / 点编辑按钮 → 不画
    }
    if (el.dataset && el.dataset.favpdfFile && !(_epInk.favPdfLoaded && _epInk.favPdfLoaded[el.dataset.favpdfFile])) {   // 原书墨迹没落地前禁画(整页替换会盖掉旧墨迹,审查 high)
      var _fpf = el.dataset.favpdfFile;
      if (_epInk.favPdfDead && _epInk.favPdfDead[_fpf]) {   // 终态:原书不可用 → 一次性提示,不再拉不再刷屏(BUG#6)
        if (!_epInk._deadToastT || Date.now() - _epInk._deadToastT > 5000) { _epInk._deadToastT = Date.now(); try { toast('这页的原书不可用(可能已移动/删除),暂不能画'); } catch (x) {} }
        return;
      }
      try { _favPdfInkLoad(); } catch (x) {}
      if (!_epInk._gateToastT || Date.now() - _epInk._gateToastT > 3000) { _epInk._gateToastT = Date.now(); try { toast('原书墨迹加载中,请稍候再画'); } catch (x) {} }   // 3s 节流,慢网连续落笔不刷屏
      return;
    }
    if (el.classList.contains('ep-usec')) {   // 插入页:编辑态禁手写;点在 Aa/编辑区 → 让按钮处理不画
      if (el.classList.contains('editing') || document.body.classList.contains('ep-up-editing')) return;
      if (e.target.closest('.ep-up-editbtn, .rc-up-edit, .rc-up-bar')) return;
    }
    var idx = _inkIdxOf(el);
    // 便签 gate:手指落在便签上(聚焦文字/双击橡皮/长按样式)全归便签自己,页面 ink 不掺和
    var noteEl = e.target && e.target.closest ? e.target.closest('.rc-note') : null;
    if (noteEl && e.pointerType === 'touch') return;
    // 原生 PencilKit 画布位于 WebView 上方；它真正命中时本处理器收不到同一
    // Pencil 事件。若原生布局或 hit-test 尚未就绪，事件会落回这里继续绘制，
    // 避免仅凭“支持 PencilKit”的能力标志制造输入黑洞。
    // 手指快速双击切 笔↔临时橡皮(照搬 PDF gate:手写模式开 或 已画过墨迹 lastEl 设了→均拦截;两者皆否时双击留给系统选词)。
    // 手指本身永不画(只滚动/双击切工具),所以双击检测干净,不和「画点」抢。这是 Apple Pencil 笔身双击在浏览器拿不到时的替代。
    if (e.pointerType === 'touch' && !_epInk.drawing && (_epInk.mode || _epInk.lastEl)) {
      var now = Date.now(), lt = _epInk._lastTap;
      if (lt && now - lt.t < 350 && Math.hypot(e.clientX - lt.x, e.clientY - lt.y) < 32) {
        _epInk._lastTap = null;
        var action = _inkDoubleTapAction();
        if (action !== 'none') { e.preventDefault(); e.stopPropagation(); _inkDoubleTapSwitch(el, action); return; }
      }
      _epInk._lastTap = { t: now, x: e.clientX, y: e.clientY };
    }
    // 绘制门槛(照搬 PDF):Apple Pencil 始终;鼠标仅桌面手写模式;手指永不画(只滚动/双击切工具)
    if (!(e.pointerType === 'pen' || (e.pointerType === 'mouse' && _epInk.mode))) return;
    if (e.pointerType === 'pen') _inkRememberPaletteAnchor(e.clientX, e.clientY);
    if (noteEl && _epInk.tool === 'region') return;   // 共享选区属于书页，不写进便签私有墨迹层
    // 便签路由:笔落在展开便签 body 上 → 整条手势仍由本模块主持,但这一段经 rc-stickynote 的
    // penBegin/penMove/penEnd 写进便签(跨界切割在 _inkPointerMove 处理)。落在 handle/工具等部位 →
    // 直接放行(不 stopPropagation),让便签自身手势(单击折叠/长按移动)接管;两种情况都不画页面
    // ——这就是 v1「pen 穿透便签写到书页」的根治点(本 capture 监听在祖先层,先于便签自己的监听)。
    if (noteEl) {
      var RS0 = window.RC && RC.stickynote;
      var overBody = RS0 && RS0.penRoute && RS0.penRoute(e.clientX, e.clientY) &&
        !(e.target.closest('.rc-note-handle') || e.target.closest('.rc-note-tools') || e.target.closest('.rc-note-rs') || e.target.closest('.rc-note-del'));
      if (!overBody) return;
      e.preventDefault(); e.stopPropagation();
      try { el.setPointerCapture(e.pointerId); } catch (_) {}
      hideSel();
      RS0.penBegin(e, { eraser: _epInk.tool === 'eraser' });
      _epInk.drawing = { el: el, captureEl: el, pid: e.pointerId, idx: idx, noteRoute: true, eraser: _epInk.tool === 'eraser' };
      document.addEventListener('pointermove', _inkPointerMove, true);
      document.addEventListener('pointerup', _inkPointerUp, true);
      document.addEventListener('pointercancel', _inkPointerUp, true);
      return;
    }
    e.preventDefault(); e.stopPropagation();   // 挡掉原生选区 / 链接
    _inkEnsure(el, idx);
    try { el.setPointerCapture(e.pointerId); } catch (_) {}
    _epInk.lastEl = el; hideSel();
    var pt = _inkNorm(el, e.clientX, e.clientY); if (!pt) return;
    if (_epInk.tool === 'eraser') {
      if (_epInk.quickErase) clearTimeout(_epInk._revertT);   // 正在擦 → 暂停自动回笔计时(抬笔再重启)
      _inkPushUndo(el); _inkEraseAt(el, pt);
      _epInk.drawing = { el: el, captureEl: el, pid: e.pointerId, idx: idx, eraser: true, pageTouched: true };
    } else {
      _inkPushUndo(el);
      var isRegion = _epInk.tool === 'region';
      var stroke = { t: _epInk.tool, c: isRegion ? '#0a84ff' : _epInk.color, w: isRegion ? 2 : (parseFloat(_epInk.width) || 2.5), p: [pt] };
      if (_epInk.tool === 'region') { stroke.id = _inkRegionId(); stroke.createdAtEpochMs = Date.now(); }
      _inkStrokesOf(el).push(stroke);                          // 先入册;绘制期间只画视口叠加层,松手 _inkRedraw 一次性落到主 canvas
      var iw0 = window.innerWidth || 1, ih0 = window.innerHeight || 1;
      _inkLiveShow();
      _epInk.drawing = { el: el, captureEl: el, pid: e.pointerId, idx: idx, stroke: stroke, live: [[e.clientX / iw0, e.clientY / ih0]], raf: null, pageTouched: true };
      _inkDrawLive(_epInk.drawing);                            // 画当前起点(视口叠加层)
    }
    document.addEventListener('pointermove', _inkPointerMove, true);
    document.addEventListener('pointerup', _inkPointerUp, true);
    document.addEventListener('pointercancel', _inkPointerUp, true);
  }
  function _inkPointerMove(e) {
    var d = _epInk.drawing; if (!d) return;
    // 手写笔、手指可能同时存在；只有发起当前笔画的 pointerId 才能阻止
    // 默认动作，手指不能因残留的 pen 状态而丢掉第一轮原生滚动。
    if (e.pointerId !== d.pid) return;
    if (e.pointerType === 'pen') _inkRememberPaletteAnchor(e.clientX, e.clientY);
    e.preventDefault();
    // ── 便签跨界路由(规格:笔尖实时位置决定写哪层;pen/橡皮参与切割,line/arrow/rect 形状不切)──
    var RS = window.RC && RC.stickynote;
    if (RS && RS.penRoute && (d.noteRoute || d.dangling || d.eraser || (d.stroke && d.stroke.t === 'pen'))) {
      var over = RS.penRoute(e.clientX, e.clientY);
      if (d.noteRoute) {
        if (over) { RS.penMove(e); return; }
        RS.penEnd({ boundary: true });        // 出便签:便签段收尾(擦边单点毛段丢弃)
        d.noteRoute = false;
        _inkBeginPageSegAt(d, e);             // 页面开新段(不在任何已加载章上 → 悬空,后续 move 再试)
        return;
      }
      if (over) {                              // 进便签:页面段收尾 → 便签段开始
        _inkEndPageSeg(d);
        RS.penBegin(e, { eraser: !!d.eraser });
        d.noteRoute = true;
        return;
      }
      if (d.dangling) { _inkBeginPageSegAt(d, e); return; }
    }
    if (d.eraser) { var ptE = _inkNorm(d.el, e.clientX, e.clientY); if (ptE) _inkEraseAt(d.el, ptE); return; }
    if (!d.stroke) return;   // 防御:路由暂态(悬空段等)下无页面笔画
    var s = d.stroke, iw = window.innerWidth || 1, ih = window.innerHeight || 1;
    if (s.t === 'pen' || s.t === 'region') {
      var evs = e.getCoalescedEvents ? e.getCoalescedEvents() : [e];   // 合并事件采样(高频笔)→ 逐点入库
      for (var i = 0; i < evs.length; i++) {
        if (s.t === 'region' && s.p.length >= (RCInk.REGION_MAX_POINTS || 512)) break;
        var p = _inkNorm(d.el, evs[i].clientX, evs[i].clientY); if (!p) continue;
        var lastp = s.p[s.p.length - 1];
        if (lastp) { var dx = p[0] - lastp[0], dy = p[1] - lastp[1]; if (dx * dx + dy * dy < 6e-6) continue; }   // 抽稀近点(防曲线发毛)
        s.p.push(p);                                              // 章归一化(存盘/橡皮命中检测)
        d.live.push([evs[i].clientX / iw, evs[i].clientY / ih]);   // 视口归一化(live 叠加层绘制),与 s.p 逐点对齐
      }
    } else {
      var pt2 = _inkNorm(d.el, e.clientX, e.clientY); if (pt2) { s.p[1] = pt2; d.live[1] = [e.clientX / iw, e.clientY / ih]; }
    }
    // rAF 节流:每帧最多重绘一次(只清+画视口叠加层一条)→ 跟手且廉价
    if (!d.raf) d.raf = requestAnimationFrame(function () { d.raf = null; if (_epInk.drawing === d) _inkDrawLive(d); });
  }
  function _inkPointerUp(e) {
    var d = _epInk.drawing; if (!d) return;
    if (e.pointerId !== d.pid) return;
    if (d.raf) { cancelAnimationFrame(d.raf); d.raf = null; }
    document.removeEventListener('pointermove', _inkPointerMove, true);
    document.removeEventListener('pointerup', _inkPointerUp, true);
    document.removeEventListener('pointercancel', _inkPointerUp, true);
    try {
      if (d.captureEl && d.captureEl.hasPointerCapture && d.captureEl.hasPointerCapture(d.pid)) {
        d.captureEl.releasePointerCapture(d.pid);
      }
    } catch (_) {}
    var RS = window.RC && RC.stickynote;
    if (d.noteRoute) { if (RS && RS.penEnd) RS.penEnd(); }   // 便签内抬笔:便签段收尾(单点=有意的点,保留)
    else if (!d.eraser && d.stroke) {
      var s = d.stroke;
      if ((s.t === 'region' && s.p.length < 3) || (s.t !== 'pen' && s.t !== 'region' && s.p.length < 2)) { var arr = _inkStrokesOf(d.el), i = arr.indexOf(s); if (i >= 0) arr.splice(i, 1); }
    }
    var wasEraser = d.eraser;
    _epInk.drawing = null;
    if (d.pageTouched && d.el) {
      _inkRedraw(d.el);   // 最终平滑全量重绘到本章主 canvas(pen/shape:含刚提交的新笔画;eraser:擦除后最终态)
      _inkScheduleSave(d.el, d.idx);
      try {
        var voicePage = _voicePageOfInkEl(d.el, d.idx);
        window.dispatchEvent(new CustomEvent('rc:inkchange', {
          detail: {
            source: 'web-ink',
            page: voicePage,
            pages: [voicePage],
            changes: [{ page: voicePage, strokes: _inkStrokesOf(d.el) }]
          }
        }));
      } catch (_) {}
    }
    _inkLiveHide();       // 再清隐视口叠加层(顺序:先主 canvas 后叠加层 → 无 1 帧空档;eraser 未用到叠加层也无妨)
    if (wasEraser && _epInk.quickErase) _inkArmRevert(900);   // 临时橡皮:擦完抬笔,停 0.9s 没再擦 → 自动回笔
  }

  // App-native PencilKit adapter. EPUB/reflow geometry belongs to this host,
  // so Swift only supplies viewport-normalized points and this code performs
  // section binding, canonical normalization, persistence and composite-shot
  // refresh through the same path as the existing PWA pen.
  (function installNativeInkHost() {
    var surfaceMap = Object.create(null), reportTimer = 0;
    var documentToken = (window.crypto && typeof window.crypto.randomUUID === 'function')
      ? window.crypto.randomUUID()
      : (Date.now().toString(36) + '-' + Math.random().toString(36).slice(2));
    var appliedOps = Object.create(null), appliedOrder = [];
    var surfaceSelector = '.fav-item-userpage[data-uid], .fav-pdf-page[data-favpdf-file], .ep-usec, .ep-sec';
    var interactiveSelector = [
      'button', 'a', '[role="button"]', 'input', 'textarea', 'select',
      '.rc-note', '.rc-up-bar', '.rc-up-edit', '.ep-up-editbtn',
      '.rc-up-edit', '.rc-up-bar', '.fav-up-editbtn', '.fav-up-edit'
    ].join(',');

    function eligible(el) {
      if (!el || el.classList.contains('ph')) return false;
      if (el.classList.contains('fav-item-userpage')) {
        if (el.classList.contains('fav-up-editing')) return false;
        if (el.dataset.uid && el.dataset.inkFile) {
          _epInk.fileOf[el.dataset.uid] = el.dataset.inkFile;
        }
      }
      if (el.classList.contains('fav-pdf-page')) {
        var file = el.dataset && el.dataset.favpdfFile;
        if (!file || !(_epInk.favPdfLoaded && _epInk.favPdfLoaded[file])) return false;
      }
      if (el.classList.contains('ep-usec') &&
          (el.classList.contains('editing') || document.body.classList.contains('ep-up-editing'))) {
        return false;
      }
      return true;
    }
    function surfaceId(el) {
      var idx = _inkIdxOf(el);
      return idx == null || idx === '' ? null : 'section:' + String(idx);
    }
    function resolveSurface(id) {
      var cached = surfaceMap[id];
      if (cached && cached.isConnected && eligible(cached)) return cached;
      var all = document.querySelectorAll(surfaceSelector);
      for (var i = 0; i < all.length; i++) {
        if (eligible(all[i]) && surfaceId(all[i]) === id) {
          _inkEnsure(all[i], _inkIdxOf(all[i]));
          surfaceMap[id] = all[i];
          return all[i];
        }
      }
      return null;
    }
    function normalizedRect(rect) {
      var vw = window.innerWidth || 1, vh = window.innerHeight || 1;
      if (rect.right <= 0 || rect.bottom <= 0 || rect.left >= vw || rect.top >= vh ||
          !(rect.width > 0 && rect.height > 0)) return null;
      return {
        x: rect.left / vw, y: rect.top / vh,
        width: rect.width / vw, height: rect.height / vh
      };
    }
    function exclusionsOf(el) {
      var out = [];
      el.querySelectorAll(interactiveSelector).forEach(function (child) {
        if (out.length >= 128) return;
        var rect = normalizedRect(child.getBoundingClientRect());
        if (rect) out.push(rect);
      });
      return out;
    }
    function describe() {
      surfaceMap = Object.create(null);
      var surfaces = [];
      document.querySelectorAll(surfaceSelector).forEach(function (el) {
        if (!eligible(el)) return;
        // Preserve the existing lazy-canvas behavior: offscreen EPUB sections
        // do not get an ink canvas merely because Swift requested a layout.
        if (!normalizedRect(el.getBoundingClientRect())) return;
        var idx = _inkIdxOf(el);
        _inkEnsure(el, idx);
        var id = surfaceId(el), rect = el.__inkCv && normalizedRect(el.__inkCv.getBoundingClientRect());
        if (!id || !rect) return;
        surfaceMap[id] = el;
        surfaces.push({ id: id, rect: rect, exclusions: exclusionsOf(el) });
      });
      return { type: 'layout', documentToken: documentToken, surfaces: surfaces };
    }
    function report() {
      if (window.__BW_NATIVE_PENCILKIT_INK__ !== true) return;
      try {
        var handler = window.webkit && window.webkit.messageHandlers &&
          window.webkit.messageHandlers.bwNativePencilInk;
        if (handler && handler.postMessage) handler.postMessage(describe());
      } catch (_) {}
    }
    function scheduleReport() {
      if (reportTimer) return;
      reportTimer = setTimeout(function () { reportTimer = 0; report(); }, 50);
    }
    function parsedSegments(input, minimumPoints, maximumPoints) {
      var raw = input && Array.isArray(input.segments) ? input.segments.slice(0, 64) : [];
      var out = [];
      for (var i = 0; i < raw.length; i++) {
        var segment = raw[i] || {}, id = String(segment.surfaceId || '');
        var el = resolveSurface(id), points = Array.isArray(segment.points) ? segment.points.slice(0, maximumPoints || 4096) : [];
        if (!el || points.length < (minimumPoints || 1)) return null;
        points = points.map(function (point) {
          if (!Array.isArray(point) || point.length < 2) return null;
          var x = Number(point[0]), y = Number(point[1]);
          if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
          return [Math.max(0, Math.min(1, x)), Math.max(0, Math.min(1, y))];
        });
        if (points.some(function (point) { return !point; })) return null;
        out.push({ raw: segment, id: id, el: el, idx: _inkIdxOf(el), points: points });
      }
      return out.length ? out : null;
    }
    function operationId(input) {
      if (!input || input.documentToken !== documentToken) return null;
      var id = String(input.opId || '');
      return /^[A-Za-z0-9_-]{1,96}$/.test(id) ? id : null;
    }
    function rememberOperation(id, operation) {
      if (appliedOps[id]) return appliedOps[id];
      appliedOps[id] = operation;
      appliedOrder.push(id);
      if (appliedOrder.length > 512) delete appliedOps[appliedOrder.shift()];
      return operation;
    }
    function finishOperation(operation) {
      var voiceChangesByPage = Object.create(null);
      Object.keys(operation.touched).forEach(function (key) {
        var segment = operation.touched[key];
        var voicePage = _voicePageOfInkEl(segment.el, segment.idx);
        _epInk.lastEl = segment.el;
        _inkRedraw(segment.el);
        _inkScheduleSave(segment.el, segment.idx);
        voiceChangesByPage[String(voicePage)] = {
          page: voicePage,
          strokes: _inkStrokesOf(segment.el)
        };
      });
      operation.state = 'applied';
      scheduleReport();
      // Match PDF: make native PencilKit changes visible to Realtime at pen-up,
      // without waiting for the periodic adapter poll.
      try {
        var voiceChanges = Object.keys(voiceChangesByPage).map(function (key) {
          return voiceChangesByPage[key];
        });
        window.dispatchEvent(new CustomEvent('rc:inkchange', {
          detail: {
            source: 'native-pencil',
            opId: operation.opId,
            pages: voiceChanges.map(function (change) { return change.page; }),
            changes: voiceChanges
          }
        }));
      } catch (e) {}
      return operation.kind === 'commit' || operation.kind === 'createRegion'
        ? { ok: true, written: operation.written, sections: Object.keys(operation.touched) }
        : { ok: true, removed: operation.removed, sections: Object.keys(operation.touched) };
    }
    function resumeCommit(operation) {
      while (operation.nextSegment < operation.segments.length) {
        var segment = operation.segments[operation.nextSegment];
        var color = /^#[0-9a-f]{6}$/i.test(String(segment.raw.color || '')) ? String(segment.raw.color) : '#ff3b30';
        var width = Math.max(1, Math.min(20, Number(segment.raw.width) || 4));
        if (!operation.touched[segment.id]) {
          _inkPushUndo(segment.el);
          operation.touched[segment.id] = segment;
        }
        _inkStrokesOf(segment.el).push({ t: 'pen', c: color, w: width, p: segment.points });
        operation.nextSegment += 1;
        operation.written += 1;
      }
      operation.state = 'mutated';
      return finishOperation(operation);
    }
    function resumeCreateRegion(operation) {
      while (operation.nextSegment < operation.segments.length) {
        var segment = operation.segments[operation.nextSegment], raw = segment.raw;
        var color = /^#[0-9a-f]{6}$/i.test(String(raw.color || '')) ? String(raw.color) : '#ff3b30';
        var width = Math.max(1, Math.min(20, Number(raw.width) || 3));
        var regionId = operation.regionId;
        if (operation.segments.length > 1) regionId = regionId.slice(0, 86) + '-' + operation.nextSegment;
        var createdAt = Number(raw.createdAtEpochMs || operation.createdAtEpochMs);
        if (!Number.isFinite(createdAt) || createdAt <= 0) createdAt = operation.createdAtEpochMs;
        if (!operation.touched[segment.id]) {
          _inkPushUndo(segment.el);
          operation.touched[segment.id] = segment;
        }
        var regionStrokes = _inkStrokesOf(segment.el);
        regionStrokes.push({
          t: 'region', id: regionId, createdAtEpochMs: createdAt,
          c: color, w: width, p: segment.points.slice(0, RCInk.REGION_MAX_POINTS || 512)
        });
        operation.nextSegment += 1;
        operation.written += 1;
      }
      operation.state = 'mutated';
      return finishOperation(operation);
    }
    window.__bwNativeInkHost = {
      describe: describe,
      refresh: report,
      ownsPoint: function (clientX, clientY) {
        var hit = document.elementFromPoint(clientX, clientY);
        if (!hit || (hit.closest && hit.closest(interactiveSelector))) return false;
        var el = hit.closest && hit.closest(surfaceSelector);
        return !!(el && eligible(el));
      },
      commit: function (input) {
        if (window.__BW_NATIVE_PENCILKIT_INK__ !== true) return { ok: false, error: 'native_disabled' };
        var opId = operationId(input);
        if (!opId) return { ok: false, error: 'native_document_stale' };
        if (appliedOps[opId]) {
          if (appliedOps[opId].state === 'applied') return { ok: true, duplicate: true };
          return appliedOps[opId].kind === 'commit' && appliedOps[opId].state === 'mutating'
            ? resumeCommit(appliedOps[opId])
            : finishOperation(appliedOps[opId]);
        }
        var segments = parsedSegments(input, 2, 4096);
        if (!segments) return { ok: false, error: 'native_surface_stale' };
        var operation = rememberOperation(opId, {
          kind: 'commit', state: 'mutating', touched: Object.create(null), opId: opId,
          segments: segments, nextSegment: 0, written: 0
        });
        return resumeCommit(operation);
      },
      createRegion: function (input) {
        if (window.__BW_NATIVE_PENCILKIT_INK__ !== true) return { ok: false, error: 'native_disabled' };
        var opId = operationId(input);
        if (!opId) return { ok: false, error: 'native_document_stale' };
        if (appliedOps[opId]) {
          if (appliedOps[opId].state === 'applied') return { ok: true, duplicate: true };
          return appliedOps[opId].kind === 'createRegion' && appliedOps[opId].state === 'mutating'
            ? resumeCreateRegion(appliedOps[opId]) : finishOperation(appliedOps[opId]);
        }
        var segments = parsedSegments(input, 3, RCInk.REGION_MAX_POINTS || 512);
        if (!segments) return { ok: false, error: 'native_surface_stale' };
        var regionId = String(input.regionId || '');
        if (!/^[A-Za-z0-9_-]{1,96}$/.test(regionId)) return { ok: false, error: 'native_region_invalid' };
        var createdAt = Number(input.createdAtEpochMs);
        if (!Number.isFinite(createdAt) || createdAt <= 0) createdAt = Date.now();
        var operation = rememberOperation(opId, {
          kind: 'createRegion', state: 'mutating', touched: Object.create(null), opId: opId, regionId: regionId,
          createdAtEpochMs: createdAt, segments: segments, nextSegment: 0, written: 0
        });
        return resumeCreateRegion(operation);
      },
      erase: function (input) {
        if (window.__BW_NATIVE_PENCILKIT_INK__ !== true) return { ok: false, error: 'native_disabled' };
        var opId = operationId(input);
        if (!opId) return { ok: false, error: 'native_document_stale' };
        if (appliedOps[opId]) {
          if (appliedOps[opId].state === 'applied') return { ok: true, duplicate: true };
          return finishOperation(appliedOps[opId]);
        }
        var segments = parsedSegments(input, 1, 4096);
        if (!segments) return { ok: false, error: 'native_surface_stale' };
        var touched = Object.create(null), removed = 0;
        segments.forEach(function (segment) {
          if (!touched[segment.id]) { _inkPushUndo(segment.el); touched[segment.id] = segment; }
          segment.points.forEach(function (point) {
            if (RCInk.eraseAt(_inkStrokesOf(segment.el), point, 0.018)) removed += 1;
          });
        });
        var operation = rememberOperation(opId, {
          kind: 'erase', state: 'mutated', touched: touched, removed: removed, opId: opId
        });
        return finishOperation(operation);
      }
    };
    window.addEventListener('scroll', scheduleReport, true);
    window.addEventListener('resize', scheduleReport, true);
    window.addEventListener('pageshow', scheduleReport, true);
    document.addEventListener('rc-upage-resize', scheduleReport, true);
    try {
      new MutationObserver(scheduleReport).observe(document.documentElement, {
        childList: true, subtree: true, attributes: true,
        attributeFilter: ['class', 'style', 'data-uid', 'data-favpdf-file']
      });
    } catch (_) {}
    setTimeout(report, 0);
    setTimeout(report, 350);
  })();
  // Apple Pencil 触摸(touchType=stylus)阻止默认滚动 → 笔不滚页;手指放行照常滚
  function _inkBlockStylusScroll(e) { for (var i = 0; i < e.touches.length; i++) { if (e.touches[i].touchType === 'stylus') { e.preventDefault(); break; } } }

  // ── 保存 / 加载(按 section idx,debounce POST)──
  // ⚠ 900ms 防抖窗口内关页/切后台会丢最后一批笔画 → dirty 集合 + pagehide/切后台 sendBeacon 立即补发。
  function _inkScheduleSave(el, idx) {
    // ⚠ 捕获数组引用快照,不在 setTimeout 里延迟读 el.__inkStrokes:section 重渲染/回收会重置它 →
    //   延迟读会把空值存进服务器覆盖真笔画(同 PDF 侧根因)。快照指向原数组(有笔画),继续画是同数组 push。
    var strokes = el.__inkStrokes;
    _epInk.data[idx] = strokes;
    (_epInk.dirty = _epInk.dirty || {})[idx] = true;
    (_epInk.pend = _epInk.pend || {})[idx] = 1;   // 有新一批待存(save 成功清 dirty 前查它:期间又画了 → 保持 dirty)
    clearTimeout(_epInk.saveTimers[idx]);
    _epInk.saveTimers[idx] = setTimeout(function () { if (_epInk.pend) delete _epInk.pend[idx]; _inkSave(idx, strokes); }, 900);
  }
  function _inkSave(idx, strokes) {
    // 绘图有改动 → 交给共享层合并(停手约 1s 才去取版本)。这里每笔都调是安全的。
    try { window.RC && RC.outgoing && RC.outgoing.drawingTouched(FREL, idx); } catch (e) {}
    if (!FREL) return;
    // POST **落地后**才清 dirty(审查:在途窗口清了会被对侧同步用 pre-POST 旧值覆盖本地);期间又画了(pend)→ 保持
    var done = function () { if (_epInk.dirty && !(_epInk.pend && _epInk.pend[idx])) delete _epInk.dirty[idx]; };
    var fail = function () { (_epInk.dirty = _epInk.dirty || {})[idx] = true; };   // 失败重标脏,flush 兜底还有机会补
    var k = String(idx);
    if (k.indexOf('pdf|') === 0) {   // 收藏夹 PDF 页:写回原书 PDF 墨迹(/api/ink 按页号;同一张纸)
      var sg = k.split('|');
      reqJson('POST', '/pdf/api/ink', { file: sg[1], page: parseInt(sg[2], 10), strokes: strokes || [] }, done, fail);
      return;
    }
    reqJson('POST', '/pdf/api/epub-ink', { file: _inkFileOf(idx), idx: idx, strokes: strokes || [] }, done, fail);
    _inkShotSync(_inkFileOf(idx), (strokes || []).length > 0);   // EPUB 笔迹合成图:存笔迹时顺带拍视口截图存服务端,see_ink 全链路回退用(用户诉求:中间层按需产合成图)
  }
  // 存笔迹时把「正文+笔迹」合成图(视口截图)推服务端 /api/epub-ink-shot,供 see_ink 各链路(文字/语音/WS/WebRTC)
  //   拿不到请求时截图时回退读。有笔迹→拍图存;无笔迹→删图。节流:同一本书 1.2s 内合并一次,避免连续落笔狂拍。
  var _inkShotT = null, _inkShotLast = 0;
  function _inkShotSync(fileRel, hasInk) {
    if (!fileRel) return;
    clearTimeout(_inkShotT);
    if (!hasInk) {   // 清空:立即删服务端图(别留陈旧)
      try { reqJson('POST', '/pdf/api/epub-ink-shot', { file: fileRel, b64: '' }, function () {}, function () {}); } catch (e) {}
      return;
    }
    _inkShotT = setTimeout(function () {
      if (!(window.RC && RC.captureView)) return;
      RC.captureView().then(function (shot) {
        if (shot && shot.b64) {
          try { reqJson('POST', '/pdf/api/epub-ink-shot', { file: fileRel, b64: shot.b64, media_type: shot.media_type }, function () {}, function () {}); } catch (e) {}
        }
      }).catch(function () {});
    }, 1200);
  }
  // 自建页拖拽改高 → 墨迹按 startH/新H 重归一化 y,保持像素位置不变(空间加到下方,不整体拉伸)。
  //   墨迹 y 归一到 section 高度(见 _inkNorm/_inkDrawStroke),高度一变 y*H 就竖向拉伸——resize 时反向补偿即抵消。
  //   rc-userpages 在 resize 各阶段发 'rc-upage-resize'{phase,startH,curH}:start 存原始快照,move/end 按比例回算,end 落库。
  //   ResizeObserver 会用(已回算的)__inkStrokes 在新尺寸重绘 → move 无需自绘;end 再 redraw+save。
  document.addEventListener('rc-upage-resize', function (e) {
    var el = e.target, d = e.detail || {};
    if (!el || !el.classList || !el.classList.contains('ep-usec')) return;
    if (d.phase === 'start') {
      var s0 = el.__inkStrokes;
      el.__inkRzOrig = (s0 && s0.length) ? JSON.parse(JSON.stringify(s0)) : [];
      el.__inkRzH = d.startH || el.getBoundingClientRect().height || 1;
      return;
    }
    var orig = el.__inkRzOrig; if (!orig || !orig.length) return;
    var sc = (el.__inkRzH || d.startH || 1) / (d.curH || el.__inkRzH || 1);
    el.__inkStrokes = orig.map(function (s) { var c = {}; for (var k in s) c[k] = s[k]; c.p = (s.p || []).map(function (pt) { return [pt[0], pt[1] * sc]; }); return c; });
    if (d.phase === 'end') {
      try { _inkRedraw(el); } catch (_) {}
      var idx = _inkIdxOf(el);
      if (_epInk && _epInk.data) _epInk.data[idx] = JSON.parse(JSON.stringify(el.__inkStrokes));
      try { _inkSave(idx, el.__inkStrokes); } catch (_) {}
      el.__inkRzOrig = null; el.__inkRzH = 0;
    }
  });
  function _inkFlushBeacon() {
    if (!FREL || !_epInk.dirty) return;
    for (var k in _epInk.dirty) {
      clearTimeout(_epInk.saveTimers[k]);   // 用字符串键 k(原 parseInt(k) 对 u_* 插入页键=NaN,beacon 送错;顺手修)
      var sent = false;
      try {
        var st = ((_epInk.elOf && _epInk.elOf[k] && _epInk.elOf[k].__inkStrokes) || _epInk.data[k] || []);   // 读 live el.__inkStrokes 为准(_epInk.data 可能被跨书拉取/实时同步 clobber 成陈旧)
        if (String(k).indexOf('pdf|') === 0) {   // 收藏夹 PDF 页 → /api/ink(按页号)
          var sg2 = String(k).split('|');
          sent = !!(navigator.sendBeacon && navigator.sendBeacon('/pdf/api/ink',
            new Blob([JSON.stringify({ file: sg2[1], page: parseInt(sg2[2], 10), strokes: st })], { type: 'application/json' })));
        } else {
          sent = !!(navigator.sendBeacon && navigator.sendBeacon('/pdf/api/epub-ink',
            new Blob([JSON.stringify({ file: _inkFileOf(k), idx: k, strokes: st })], { type: 'application/json' })));
        }
      } catch (e) {}
      if (sent) delete _epInk.dirty[k];   // 送出去了才清(sendBeacon false=没发出,保 dirty 兜底,防被同步旧值覆盖)
    }
  }
  window.addEventListener('pagehide', _inkFlushBeacon);
  document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'hidden') _inkFlushBeacon(); });
  function _inkHasAny() { for (var k in _epInk.data) { if (_epInk.data[k] && _epInk.data[k].length) return true; } return false; }
  function _inkApplyVisibleSaved() {
    _visibleLoadedSecs().forEach(function (el) {
      var idx = parseInt(el.dataset.idx, 10);
      if (!el.__inkCv && _epInk.data[idx] && _epInk.data[idx].length) { el.__inkStrokes = JSON.parse(JSON.stringify(_epInk.data[idx])); _inkEnsure(el, idx); _inkRedraw(el); }
    });
    _epUpEls().forEach(function (u) { try { _epUpApplyInk(u.el, u.id); } catch (e) {} });   // 插入页(.ep-usec,u_* 键)存档墨迹复原
  }
  var _inkDecoT;
  function _inkDecoScheduleVisible() {
    if (!_inkHasAny()) return;
    clearTimeout(_inkDecoT);
    _inkDecoT = setTimeout(function () { try { _inkApplyVisibleSaved(); } catch (e) {} }, 200);
  }
  // 收藏夹自建页(EPUB)墨迹实时绑定原书:.fav-item-userpage[data-uid][data-ink-file] → 记 fileOf(uid→原书文件),
  //   按原书文件拉墨迹(每文件只拉一次,只取 u_* 自建页键避与 fav 整数章键撞)填 _epInk.data[uid],挂 canvas 到页体。
  //   幂等(已挂/数据已在则跳)。原书画了→fav 打开同步;fav 里画→_inkSave 经 _inkFileOf 写回原书。
  function _favUpInkLoad() {
    var ups = document.querySelectorAll('.fav-item-userpage[data-uid][data-ink-file]');
    if (!ups.length) return;
    _epInk.favFetched = _epInk.favFetched || {};
    var need = {};
    for (var i = 0; i < ups.length; i++) {
      var uid = ups[i].dataset.uid, f = ups[i].dataset.inkFile;
      if (!uid || !f) continue;
      _epInk.fileOf[uid] = f;   // 墨迹绘制/保存路由回原书
      if (_epInk.data[uid] == null && !_epInk.favFetched[f]) (need[f] = need[f] || []).push(f);
    }
    if (!Object.keys(need).length) { try { _inkApplyVisibleSaved(); } catch (e) {} return; }   // 数据已在 → 让 _epUpApplyInk 把墨迹复原到 mountOne 出来的 .ep-usec
    Object.keys(need).forEach(function (f) {
      _epInk.favFetched[f] = true;
      fetch('/pdf/api/epub-ink?file=' + encodeURIComponent(f)).then(function (r) { return r.json(); }).then(function (d) {
        var secs = (d && d.sections) || {};
        Object.keys(secs).forEach(function (k) { if (k.indexOf('u_') === 0 && !(_epInk.dirty && _epInk.dirty[k])) _epInk.data[k] = JSON.parse(JSON.stringify(secs[k])); });   // 只取原书 u_* 自建页墨迹;dirty 不覆盖 → 防丢新笔
        try { _inkApplyVisibleSaved(); } catch (e) {}   // 数据到位 → _epUpApplyInk 把原书墨迹画到挂载的 .ep-usec(与原书插入页同一复原路径)
      }).catch(function () { delete _epInk.favFetched[f]; });
    });
  }
  // 收藏夹自建页 = 原书那页的实时编辑器:用 RC.userpages.mountOne 把它挂成真 .ep-usec(与原书**同一份代码**:
  //   Aa 即时编辑 / 下边缘改高 / keepRatio / 自动保存,全经 buildInstant + _fileOf 存回原书 file+uid)。挂好后隐藏 baked 层、接墨迹路由。
  function _favUpMount() {
    if (!(window.RC && RC.userpages && RC.userpages.mountOne)) return;
    if (!_favUpMount._css) {   // 一次性注入(免重建 fav):挂载后隐藏 baked 显示层 + 容器不再撑 86vh(改由内部 .ep-usec 驱动)
      _favUpMount._css = 1;
      try { var s = document.createElement('style'); s.textContent = '.fav-item-userpage.fav-up-mounted{min-height:0;position:static}.fav-item-userpage.fav-up-mounted>.fav-up-disp{display:none}'; (document.head || document.documentElement).appendChild(s); } catch (e) {}
    }
    document.querySelectorAll('.fav-item-userpage[data-uid][data-ink-file]').forEach(function (cont) {
      if (cont.__favMounted) return; cont.__favMounted = 1;
      var uid = cont.dataset.uid, f = cont.dataset.inkFile;
      if (!uid || !f) { cont.__favMounted = 0; return; }
      _epInk.fileOf[uid] = f;   // 先登记:墨迹绘制/保存路由回原书
      RC.userpages.mountOne(cont, { file: f, id: uid }).then(function (el) {
        if (!el) { cont.__favMounted = 0; return; }
        el.dataset.inkFile = f;                 // .ep-usec 也带 data-ink-file → 墨迹/正文同步都定址原书
        cont.classList.add('fav-up-mounted');   // 隐藏 baked .fav-up-disp(CSS)
        try { _favUpInkLoad(); } catch (e) {}    // 复原原书墨迹到这页
      });
    });
  }
  // 自建页双向**增量实时同步**(2026-07-05):页面可见时(visibilitychange)+ 每 7s 轮询,重新拉墨迹/正文,变了就就地重渲。
  //   收藏夹页按 data-ink-file(原书)拉、原书 .ep-usec 按 FREL 拉 → 在一处编辑,另一处(同/异设备)≤7s 自动更新,无需刷新。
  //   正在画/编辑的那页跳过(不打断);首次只记基线不重渲(免闪);跨设备靠服务端同一份边车收敛。
  function _userpageLiveSync() {
    if (document.visibilityState !== 'visible') return;
    // 收藏夹 PDF 页墨迹(同一张纸:原书 /api/ink):按原书文件分组拉,变了就重绘(先跑,不依赖下面 userpage targets)
    var pdfByFile = {};
    document.querySelectorAll('.fav-pdf-page[data-favpdf-file]').forEach(function (el) {
      if (el.isConnected && el.dataset.favpdfFile) (pdfByFile[el.dataset.favpdfFile] = pdfByFile[el.dataset.favpdfFile] || []).push(el);
    });
    Object.keys(pdfByFile).forEach(function (f) {
      fetch('/pdf/api/ink?file=' + encodeURIComponent(f)).then(function (r) { return r.json(); }).then(function (d) {
        if (!(d && d.ok)) return;
        var pgs = d.pages || {};
        pdfByFile[f].forEach(function (el) {
          if (_epInk.drawing && _epInk.drawing.el === el) return;   // 正在这页画 → 不打断
          var k = 'pdf|' + f + '|' + el.dataset.favpdfPage;
          if (_epInk.dirty && _epInk.dirty[k]) return;              // 本地有待存 → 别覆盖
          var fresh = pgs[String(el.dataset.favpdfPage)] || [];
          if (JSON.stringify(fresh) !== JSON.stringify(_epInk.data[k] || [])) {
            _epInk.data[k] = JSON.parse(JSON.stringify(fresh));
            el.__inkStrokes = JSON.parse(JSON.stringify(fresh));
            if (fresh.length) _inkEnsure(el, k);
            if (el.__inkCv) _inkRedraw(el);
          }
        });
      }).catch(function () {});
    });
    var targets = [];
    var upPages = (window.RC && RC.userpages && RC.userpages.pages) ? RC.userpages.pages() : [];
    // 原书插入页(file=FREL)+ 收藏夹挂载页(mountOne 出来的 .ep-usec,file=data-ink-file 原书)统一走同一套(.ep-usec)
    document.querySelectorAll('.ep-usec[data-uid]').forEach(function (el) {
      if (!el.isConnected) return;
      var uid = el.dataset.uid;
      targets.push({ el: el, uid: uid, file: (el.dataset.inkFile || FREL), disp: el.querySelector('.rc-up-body'), page: upPages.filter(function (p) { return p.id === uid; })[0] || null, editing: el.classList.contains('editing') || document.body.classList.contains('ep-up-editing') });
    });
    if (!targets.length) return;
    var byFile = {};
    targets.forEach(function (t) { if (t.file) (byFile[t.file] = byFile[t.file] || []).push(t); });
    Object.keys(byFile).forEach(function (f) {
      fetch('/pdf/api/epub-ink?file=' + encodeURIComponent(f)).then(function (r) { return r.json(); }).then(function (d) {
        var secs = (d && d.sections) || {};
        byFile[f].forEach(function (t) {
          if (t.editing) return;
          if (_epInk.drawing && _epInk.drawing.el === t.el) return;   // 正在这页画 → 不打断
          if (_epInk.dirty && _epInk.dirty[t.uid]) return;           // 本地有待存笔画 → 别用服务端旧值覆盖(等 debounce 存完下轮再同步)
          var fresh = secs[t.uid] || [];
          if (JSON.stringify(fresh) !== JSON.stringify(_epInk.data[t.uid] || [])) {
            _epInk.data[t.uid] = JSON.parse(JSON.stringify(fresh));
            t.el.__inkStrokes = JSON.parse(JSON.stringify(fresh));
            if (fresh.length) _inkEnsure(t.el, t.uid);
            if (t.el.__inkCv) _inkRedraw(t.el);
          }
        });
      }).catch(function () {});
      fetch('/pdf/api/userpages?file=' + encodeURIComponent(f)).then(function (r) { return r.json(); }).then(function (d) {
        if (!(d && d.ok)) return;   // 出错别当"页没了"误删
        var pages = d.pages || [];
        byFile[f].forEach(function (t) {
          if (t.editing || !t.disp) return;
          var rec = pages.filter(function (p) { return p.id === t.uid; })[0];
          if (!rec) { _upageRemoveLive(f, t.uid); return; }   // 页在别处被删(切后台错过事件)→ 移除本视图元素
          var newMd = rec.md || '';
          if (t.disp.__syncMd == null) { t.disp.__syncMd = newMd; if (t.page) t.page.md = newMd; return; }   // 首次:记基线,不重渲(免闪)
          if (newMd !== t.disp.__syncMd) {
            t.disp.__syncMd = newMd;
            if (t.page) t.page.md = newMd;   // 同步 rc-userpages 缓存,下次编辑从新 md 起
            if (window.RC && RC.md) { try { t.disp.innerHTML = RC.md(newMd); if (RC.typeset) RC.typeset(t.disp); } catch (e) {} }
          }
        });
      }).catch(function () {});
    });
  }
  // ── 收藏夹结构真·增量(2026-07-05,用户拍板):新收藏 → 已打开的收藏夹几秒内长出新节;取消收藏/删页 → 当场消失,不刷新。
  //   机制:fav CRUD → 后台重建 → 'fav-built' 事件 → 拉 /api/fav-meta 按条目 key diff 新旧 items:
  //   保留的节(每条目内容确定性生成,没变)复用 DOM 只改 idx;移除的节删掉;新增的节建占位交给懒加载(从新 EPUB 拉对应 idx)。
  var _FAV_FID = (typeof IS_FAV_BOOK !== 'undefined' && IS_FAV_BOOK) ? ((FREL.replace(/^\//, '').split('/').pop() || '').replace(/\.epub$/, '')) : '';
  var _favMetaItems = null;   // 上次已知 items(下标=section idx);首拉当基线
  function _favItemKey(it) { return (it.kind || '') + '|' + (it.src_file || '') + '|' + (it.kind === 'pdf' ? it.src_page : (it.kind === 'epub' ? it.src_section : it.id)); }
  function _favMetaFetch(cb) {
    if (!_FAV_FID) return;
    fetch('/pdf/api/fav-meta?id=' + encodeURIComponent(_FAV_FID)).then(function (r) { return r.json(); }).then(function (d) {
      var items = (d && d.meta && d.meta.items) || null;
      if (items) cb(items);
    }).catch(function () {});
  }
  if (_FAV_FID) _favMetaFetch(function (items) { if (!_favMetaItems) _favMetaItems = items; });   // 开书记基线
  // 来源条「☆ 取消收藏」:PATCH remove_item(只动收藏夹条目,不碰原书)→ 本节乐观隐藏;后台重建 fav-built → reconcile 收尾
  document.addEventListener('click', function (e) {
    var b = e.target && e.target.closest ? e.target.closest('.fav-unfav') : null;
    if (!b || !_FAV_FID) return;
    e.preventDefault(); e.stopPropagation();
    var it; try { it = JSON.parse(b.dataset.fitem || ''); } catch (x) { return; }
    if (!confirm('取消收藏这一页?(不影响原书内容)')) return;
    reqJson('PATCH', '/pdf/api/favorites', { folder: _FAV_FID, remove_item: it }, function (d) {
      if (!(d && d.ok)) { toast('取消失败:' + ((d && d.error) || '?')); return; }
      var s = b.closest('.ep-sec'); if (s) { s.style.display = 'none'; s.dataset.unfav = '1'; }   // 标记乐观隐藏:reconcile 发现条目仍在(重新收藏)会复原(BUG#2)
      toast('已取消收藏');
    }, function () { toast('网络错误,没取消上'); });
  }, true);
  // 自建页被删(任一视图的 🗑)→ 所有打开着的视图当场移除该页元素(原书=.ep-usec 本体;收藏夹=整节隐藏,fav-built 后 reconcile 收尾)
  function _upageRemoveLive(file, uid) {
    if (!uid) return;
    try {
      var el = document.querySelector('.ep-usec[data-uid="' + uid + '"]');
      if (el && (file === FREL || el.dataset.inkFile === file)) el.remove();
      var fc = document.querySelector('.fav-item-userpage[data-uid="' + uid + '"]');
      if (fc && fc.dataset.inkFile === file) { var fs = fc.closest('.ep-sec'); ((fs || fc)).style.display = 'none'; }
    } catch (e) {}
  }
  var _favReconT = null;
  function _favReconcile() {
    if (!_FAV_FID) return;
    if (document.body.classList.contains('ep-up-editing') || (_epInk && _epInk.drawing)) { clearTimeout(_favReconT); _favReconT = setTimeout(_favReconcile, 2000); return; }   // 正在编辑/画 → 稍后再排
    _favMetaFetch(function (items) {
      if (!_favMetaItems) { _favMetaItems = items; return; }
      var oldByKey = {};
      for (var i = 0; i < _favMetaItems.length; i++) oldByKey[_favItemKey(_favMetaItems[i])] = i;
      var same = items.length === _favMetaItems.length && items.every(function (it, j) { return oldByKey[_favItemKey(it)] === j; });
      if (same) {
        _favMetaItems = items;
        // 曾被「☆取消收藏」乐观隐藏、但条目仍在(期间又被重新收藏)→ 复原可见(审计 BUG#2:否则复活成不可见)
        try { secEls.forEach(function (se) { if (se && se.dataset && se.dataset.unfav) { delete se.dataset.unfav; se.style.display = ''; } }); } catch (e) {}
        return;   // 结构没变(纯内容重建)→ 不动 DOM
      }
      var newEls = [], newLoaded = {}, usedOld = {};
      for (var j = 0; j < items.length; j++) {
        var oi = oldByKey[_favItemKey(items[j])];
        if (oi != null && !usedOld[oi] && secEls[oi]) {
          usedOld[oi] = 1;
          var el = secEls[oi]; el.dataset.idx = j;
          if (el.dataset.unfav) { delete el.dataset.unfav; el.style.display = ''; }   // 乐观隐藏过但条目仍在 → 复原可见(BUG#2)
          newEls.push(el); newLoaded[j] = (loaded[oi] === true);   // 'loading' 不搬:旧代在途响应已作废,置 false 让新代重拉(BUG#1)
        } else {
          var ph = document.createElement('div'); ph.className = 'ep-sec ph'; ph.dataset.idx = j; ph.textContent = '…';
          newEls.push(ph); newLoaded[j] = false;
        }
      }
      for (var r = 0; r < secEls.length; r++) { if (!usedOld[r] && secEls[r]) { try { secEls[r].remove(); } catch (e) {} } }   // 被移除的节当场消失
      for (var m = 0; m < newEls.length; m++) { try { col.appendChild(newEls[m]); } catch (e) {} }   // 按新顺序落位(同序同内容 → 布局/滚动不跳)
      _secGen++; _secWait.length = 0;   // 换代:作废在途旧 idx 章节请求 + 清排队(BUG#1)
      secEls = newEls; loaded = newLoaded; COUNT = items.length; _favMetaItems = items;
      try { newEls.forEach(function (el) { if (el.classList.contains('ph')) observer.observe(el); }); } catch (e) {}   // 新节交给懒加载(从重建后的新 EPUB 拉)
      try { $('ep-page-total').textContent = '/ ' + (COUNT || '–'); onScroll(); } catch (e) {}
      try { toast('收藏夹已同步更新'); } catch (e) {}
    });
  }
  // 实时同步:SSE 事件推送为主(墨迹/正文一存,另一侧 ~1s 收到即重拉重渲);visibilitychange + 慢轮询兜底(断线/漏事件)。
  var _readerES = null, _liveSyncT = null;
  function _liveSyncSoon() { clearTimeout(_liveSyncT); _liveSyncT = setTimeout(function () { try { _userpageLiveSync(); } catch (e) {} }, 120); }
  function _favUpFiles() {
    var s = {};
    document.querySelectorAll('.fav-item-userpage[data-ink-file]').forEach(function (el) { if (el.dataset.inkFile) s[el.dataset.inkFile] = 1; });
    document.querySelectorAll('.fav-pdf-page[data-favpdf-file]').forEach(function (el) { if (el.dataset.favpdfFile) s[el.dataset.favpdfFile] = 1; });   // 收藏夹 PDF 页:原书 ink 事件也触发同步
    if (document.querySelector('.ep-usec[data-uid]')) s[FREL] = 1;
    return Object.keys(s);
  }
  var _resRetry = 0;
  // 133:退避+抖动(同 pdf-tail.js)。SSE 舱壁满时返 503,EventSource 按规范对非 200 不自己重连 →
  // 全靠下面的定时器;恒定 3s 会变成持续硬刷。
  function _resBackoff() { return Math.min(30000, 3000 * Math.pow(2, Math.min(_resRetry, 4))) * (0.7 + Math.random() * 0.6); }
  function _readerEventsConnect() {
    if (_readerES || typeof EventSource === 'undefined') return;
    try {
      _readerES = new EventSource('/pdf/api/reader-events');
      _readerES.addEventListener('open', function () { _resRetry = 0; });
      _readerES.addEventListener('change', function (e) {
        var _ev0; try { _ev0 = JSON.parse(e.data); } catch (_) { _ev0 = null; }
        if (_ev0 && _ev0.kind === 'assistant-history') {   // 外部写入 → 侧栏当场追加(同 PDF 宿主)
          try { if (window.RC && RC.assistant && RC.assistant.onHistoryEvent) RC.assistant.onHistoryEvent(_ev0); } catch (_) {}
          return;
        }
        if (document.visibilityState !== 'visible') return;   // 不活跃 → 忽略(后端已更新,回来 visibility 同步)
        var ev; try { ev = JSON.parse(e.data); } catch (x) { return; }
        if (ev && ev.kind === 'client-action' && ev.action && (!ev.file || ev.file === FREL)) {   // MCP 遥控:统一走 RC.execRemote(EPUB 的 jumpWithBack→HOST.goTo 章跳)
          try { if (window.RC && RC.execRemote) RC.execRemote(ev.action); } catch (x3) {}
          return;
        }
        if (ev && ev.kind === 'fav-built' && _FAV_FID && ev.file === ('fav:' + _FAV_FID)) { _favReconcile(); return; }   // 本收藏夹重建完 → 结构增量重排
        if (ev && ev.kind === 'userpage-del') { _upageRemoveLive(ev.file, ev.uid); return; }   // 别处删了这张纸 → 本视图当场移除
        if (ev && ev.file && _favUpFiles().indexOf(ev.file) >= 0) _liveSyncSoon();   // 事件跟本阅读器某自建页来源匹配才同步
      });
      _readerES.onerror = function () { if (_readerES && _readerES.readyState === 2) { _readerES = null; _resRetry++; setTimeout(_readerEventsConnect, _resBackoff()); } };   // 永久关 → 退避重连
    } catch (e) { _readerES = null; }
  }
  try {
    _readerEventsConnect();
    // Service Worker(同 PDF 01-boot):EPUB 只从这进也能激活 /pdf/ 域 SW → manifest/section SWR 秒开
    if ('serviceWorker' in navigator) { try { navigator.serviceWorker.register('/pdf/sw.js', { scope: '/pdf/' }).catch(function () {}); } catch (e) {} }
    try { if (navigator.storage && navigator.storage.persist) navigator.storage.persist(); } catch (e) {}
    document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'visible') { try { _userpageLiveSync(); } catch (e) {} try { _favReconcile(); } catch (e) {} _readerEventsConnect(); } });
    var _lsTick = 0;
    setInterval(function () { try {
      _lsTick++;
      if (_readerES && _readerES.readyState === 1 && (_lsTick % 3)) return;   // SSE 健康 → 兜底降频到 60s;断线/未连 → 保持 20s
      _userpageLiveSync();
    } catch (e) {} }, 20000);   // 慢轮询兜底(SSE 为主;可见时 gate + 无自建页早退,普通书近零开销)
  } catch (e) {}
  // 收藏夹 PDF 页墨迹:按原书文件拉 /api/ink(每文件一次),填 _epInk.data['pdf|文件|页'],挂 canvas 到 .fav-pdf-page(同一张纸)
  function _favPdfInkLoad() {
    var els = document.querySelectorAll('.fav-pdf-page[data-favpdf-file]');
    if (!els.length) return;
    _epInk.favPdfFetched = _epInk.favPdfFetched || {};
    _epInk.favPdfLoaded = _epInk.favPdfLoaded || {};   // fetch 成功落地的文件。起笔门槛用它:没 loaded 前禁画(整页替换语义下,空底起笔会把原书旧墨迹整页盖掉——审查 high)
    var apply = function () {
      // 现查而非快照(审查:fetch 在途期间懒加载出的页也要覆盖);canvas 已预建(手写模式)也补数据,不跳
      document.querySelectorAll('.fav-pdf-page[data-favpdf-file]').forEach(function (el) {
        if (!el.isConnected) return;
        var k = 'pdf|' + el.dataset.favpdfFile + '|' + el.dataset.favpdfPage;
        if (_epInk.drawing && _epInk.drawing.el === el) return;
        if (_epInk.dirty && _epInk.dirty[k]) return;
        var d = _epInk.data[k];
        if (d && d.length) {
          if (!el.__inkStrokes || !el.__inkStrokes.length) el.__inkStrokes = JSON.parse(JSON.stringify(d));
          _inkEnsure(el, k); _inkRedraw(el);
        } else if (_epInk.mode && !el.__inkCv) { _inkEnsure(el, k); }
      });
    };
    var need = {};
    els.forEach(function (el) { var f = el.dataset.favpdfFile; if (f && !_epInk.favPdfFetched[f]) need[f] = 1; });
    var ks = Object.keys(need);
    if (!ks.length) { apply(); return; }
    ks.forEach(function (f) {
      _epInk.favPdfFetched[f] = true;
      fetch('/pdf/api/ink?file=' + encodeURIComponent(f)).then(function (r) { return r.json(); }).then(function (d) {
        if (!(d && d.ok)) { (_epInk.favPdfDead = _epInk.favPdfDead || {})[f] = 1; return; }   // 服务端明确拒绝(原书没了/改名)→ 终态,不再重试也不放行(网络错走 catch 保留重试;审计 BUG#6)
        var pgs = d.pages || {};
        Object.keys(pgs).forEach(function (pg) { var k2 = 'pdf|' + f + '|' + pg; if (!(_epInk.dirty && _epInk.dirty[k2])) _epInk.data[k2] = JSON.parse(JSON.stringify(pgs[pg])); });   // dirty 不覆盖防丢新笔
        _epInk.favPdfLoaded[f] = true;   // 落地 → 放行起笔
        apply();
      }).catch(function () { delete _epInk.favPdfFetched[f]; });
    });
  }
  function _inkOnSectionLoaded(el, idx) {
    var d = _epInk.data[idx];
    if ((d && d.length) || _epInk.mode) { if (d && d.length) el.__inkStrokes = JSON.parse(JSON.stringify(d)); _inkEnsure(el, idx); }
    try { _favUpMount(); } catch (e) {}     // 收藏夹自建页:挂成真 .ep-usec(复用原书编辑器/改高;幂等)
    try { _favUpInkLoad(); } catch (e) {}   // 收藏夹自建页:复原原书墨迹到挂载的 .ep-usec(幂等)
    try { _favPdfInkLoad(); } catch (e) {}  // 收藏夹 PDF 页:复原原书 PDF 墨迹(幂等)
  }
  function _inkLoadAll() {
    if (!FREL) return;
    fetch('/pdf/api/epub-ink?file=' + encodeURIComponent(FREL)).then(function (r) { return r.json(); }).then(function (d) {
      if (!d || !d.ok || !d.sections) return;
      _epInk.data = {};
      _epInk.favFetched = {}; _epInk.favPdfFetched = {};   // 本书墨迹重载 → 收藏夹跨书拉取缓存一并重置(_epInk.data 已清,须重拉原书墨迹)
      Object.keys(d.sections).forEach(function (k) { _epInk.data[/^\d+$/.test(k) ? parseInt(k, 10) : k] = d.sections[k]; });   // 正文=整数键;插入页=u_* 字符串键(保留原样)
      (window.requestIdleCallback || function (f) { return setTimeout(f, 0); })(function () { try { _inkApplyVisibleSaved(); _favUpInkLoad(); _favPdfInkLoad(); } catch (e) {} }, { timeout: 1500 });
    }).catch(function () {});
  }

  // ── 工具栏 ──
  function _inkToggleMode() {
    _epInk.mode = !_epInk.mode;
    document.body.classList.toggle('ep-ink-mode', _epInk.mode);
    var b = $('ep-ink-btn'); if (b) b.classList.toggle('active', _epInk.mode);
    var tb = $('ep-ink-toolbar'); if (tb) tb.classList.toggle('show', _epInk.mode);
    if (_epInk.mode) { _visibleLoadedSecs().forEach(function (el) { _inkEnsure(el, parseInt(el.dataset.idx, 10)); }); hideSel(); }
    if (_epInk.mode) requestAnimationFrame(_inkPlaceToolbar);
  }
  function _inkSetTool(t) { clearTimeout(_epInk._revertT); _epInk._revertT = null; _epInk.quickErase = false; _epInk.tool = t; _inkUpdateToolUI(); }
  function _inkSetColor(c, btn) {
    _epInk.color = c; try { localStorage.setItem('eph-ink-color', c); } catch (e) {}
    var tb = $('ep-ink-toolbar'); if (tb) [].forEach.call(tb.querySelectorAll('.ep-ink-color'), function (b) { b.classList.toggle('on', b === btn); });
  }
  function _inkUndo() {
    var el = _inkActiveEl(); if (!el) return; _inkEnsure(el, _inkIdxOf(el));
    if (!el.__inkUndo || !el.__inkUndo.length) return;
    if (!el.__inkRedo) el.__inkRedo = [];
    el.__inkRedo.push(JSON.stringify(el.__inkStrokes || []));
    el.__inkStrokes = JSON.parse(el.__inkUndo.pop());
    _epInk.lastEl = el; _inkRedraw(el); _inkScheduleSave(el, _inkIdxOf(el));
  }
  function _inkRedo() {
    var el = _inkActiveEl(); if (!el) return; _inkEnsure(el, _inkIdxOf(el));
    if (!el.__inkRedo || !el.__inkRedo.length) return;
    if (!el.__inkUndo) el.__inkUndo = [];
    el.__inkUndo.push(JSON.stringify(el.__inkStrokes || []));
    el.__inkStrokes = JSON.parse(el.__inkRedo.pop());
    _epInk.lastEl = el; _inkRedraw(el); _inkScheduleSave(el, _inkIdxOf(el));
  }
  function _inkClearActive() {
    var el = _inkActiveEl(); if (!el) return; _inkEnsure(el, _inkIdxOf(el));
    if (!(el.__inkStrokes && el.__inkStrokes.length)) return;
    _inkPushUndo(el); el.__inkStrokes = []; _epInk.lastEl = el; _inkRedraw(el); _inkScheduleSave(el, _inkIdxOf(el));
  }
  function _inkToggleVisible() {
    _epInk.visible = !_epInk.visible;
    var eye = $('ep-ink-eye'); if (eye) eye.style.opacity = _epInk.visible ? '1' : '.4';
    secEls.forEach(function (el) { if (el.__inkCv) _inkRedraw(el); });
  }
  function _inkWireToolbar() {
    var btn = $('ep-ink-btn'); if (btn) btn.addEventListener('click', _inkToggleMode);
    var tb = $('ep-ink-toolbar'); if (!tb) return;
    tb.addEventListener('click', function (e) {
      var b = e.target.closest ? e.target.closest('button') : null; if (!b) return;
      if (b.dataset.itool) { _inkSetTool(b.dataset.itool); }
      else if (b.dataset.c) { _inkSetColor(b.dataset.c, b); }
      else {
        var a = b.dataset.act;
        if (a === 'undo') _inkUndo(); else if (a === 'redo') _inkRedo();
        else if (a === 'eye') _inkToggleVisible(); else if (a === 'clear') _inkClearActive();
      }
    });
    var w = $('ep-ink-width'); if (w) w.addEventListener('input', function () { _epInk.width = parseFloat(this.value) || 2.5; });
  }
  function _inkInit() {
    try { _epInk.color = localStorage.getItem('eph-ink-color') || _epInk.color; } catch (e) {}
    _inkWireToolbar();
    var tb = $('ep-ink-toolbar');
    if (tb) [].forEach.call(tb.querySelectorAll('.ep-ink-color'), function (b) { b.classList.toggle('on', b.dataset.c === _epInk.color); });
    // 委托监听:#ep-col capture pointerdown(找 .ep-sec)+ 触屏 stylus 阻止滚动(笔不滚页,手指照常)
    col.addEventListener('pointerdown', _inkPointerDown, true);
    document.addEventListener('pointermove', function (e) {
      if (e.pointerType === 'pen' && !_epInk.drawing && !(e.target && e.target.closest && e.target.closest('#ep-ink-toolbar'))) _inkRememberPaletteAnchor(e.clientX, e.clientY);
    }, true);
    col.addEventListener('touchstart', _inkBlockStylusScroll, { passive: false });
    col.addEventListener('touchmove', _inkBlockStylusScroll, { passive: false });
    setTimeout(_inkLoadAll, 900);   // 延迟拉 sidecar,不和首屏章节抢连接
  }

  // ── 语法分析 pane:懒填充(共享核心 RC.grammar 接管块渲染,本模块只负责"进 pane 时拉什么")。
  //   KG 启用列表每次进 pane 都刷新(跟踪状态可能在技能树页面被改过,照搬 epub2-grammar.js 的 onGrammarPaneActive);
  //   历史只load 一次(_grammarHistLoaded 门槛,跟助手/单词本同一套习惯)。
  var _grammarHistLoaded = false, _grammarSetWired = false;
  function loadGrammarPane() {
    if (!_grammarSetWired) {
      _grammarSetWired = true;
      var head = $('ep-grammar-set-head');
      if (head) head.addEventListener('click', function () { var s = $('ep-grammar-set'); if (s) s.classList.toggle('open'); });
    }
    if (!(window.RC && RC.grammar)) return;
    RC.grammar.renderTrackList('ep-grammar-kglist', { file: FREL });
    if (!_grammarHistLoaded) {
      _grammarHistLoaded = true;
      RC.grammar.loadHistory('ep-grammar-body', FREL, {
        aiParams: function () { return (window.RC && RC.settings) ? RC.settings.aiParams() : {}; },
        sourceUrl: function () { return location.origin + '/pdf/epub/view?file=' + encodeURIComponent(FREL); },
        viewModeKey: 'eph-grammar-view',
      });
    }
  }
  // ── 启动 ──
  applyStyle(); refreshSet(); renderHlPicker(); loadBookLangs();
  // 语法 KG 跟踪状态预载(对齐 PDF 原生启动时 loadGrammarTracked):showSel 靠 RC.grammar.hasTracked()
  // 决定「📊 语法」按钮显隐,不预载则首次选中时恒 false 按钮不出现。失败静默(按钮不显示,语法 tab 内仍可配置)。
  try { if (window.RC && RC.grammar && RC.grammar.loadTracked) RC.grammar.loadTracked(FREL); } catch (e) {}
  // 便签(共享组件 rc-stickynote,设计见 references/sticky-notes-design.md「规格 v4」):挂载/锚定 per-reader——
  // mount=对应 .ep-sec(章未加载→null,由 _fetchSection 空闲回调 mountPending 补挂),返回 {el,left,top} 像素
  // (铁律修正:定位机制在 host,组件只应用);锚点 v4 = 内容锚 {kind:'epub',section,off,dx,dy}(off=最近可数
  // 文字偏移,同高亮 offsetOf 坐标系;dx/dy=便签相对该字符 rect 的像素偏移;x/y 仍带上,作纯图/无文字兜底 +
  // _noteNearText 用)。重排(侧栏开关/字号/栏宽/resize)后字符位置变,repositionAll 重算 → 便签跟着字走,零漂移。
  // ── v4 内容锚辅助 ──
  function _noteCharRect(secEl, off) {   // 可数字符偏移 → 该字符 viewport rect(取不到 → null)
    var p = _domPosAtOffset(secEl, Math.max(0, off | 0));
    if (!p || !p.node) return null;
    var len = p.node.nodeValue.length;
    var s = Math.min(p.offset, len), e = Math.min(len, s + 1);
    var r = document.createRange();
    try { r.setStart(p.node, s); r.setEnd(p.node, e); } catch (er) { return null; }
    var rect = r.getBoundingClientRect();
    if (!rect || (!rect.width && !rect.height && !rect.top && !rect.left && !rect.bottom)) {
      var rs = r.getClientRects();
      rect = rs && rs.length ? rs[0] : null;
    }
    return rect;
  }
  function _noteOffPos(secEl, anchor) {   // 内容锚 → section 内像素 left/top(字符 rect + dx/dy)
    var cr = _noteCharRect(secEl, anchor.off);
    if (!cr) return null;
    var sr = secEl.getBoundingClientRect();
    return { left: cr.left - sr.left + (anchor.dx || 0), top: cr.top - sr.top + (anchor.dy || 0) };
  }
  // 视口点 → 该 section 内最近可数字符的绝对偏移(懒迁移用,section 可离屏:纯几何搜,不依赖 elementFromPoint)。
  // 先按文本节点整体 rects 找最近节点,再节点内粗采样(≤32 步)+ 邻域细化,控制 getBoundingClientRect 次数。
  function _noteNearestOff(secEl, px, py) {
    var w = document.createTreeWalker(secEl, NodeFilter.SHOW_TEXT, null), n, best = null;
    while ((n = w.nextNode())) {
      if (!_countable(n) || !(n.nodeValue || '').trim()) continue;
      var rng = document.createRange(); rng.selectNodeContents(n);
      var rects = rng.getClientRects();
      for (var i = 0; i < rects.length; i++) {
        var rc = rects[i]; if (!rc.width && !rc.height) continue;
        var ddx = px < rc.left ? rc.left - px : (px > rc.right ? px - rc.right : 0);
        var ddy = py < rc.top ? rc.top - py : (py > rc.bottom ? py - rc.bottom : 0);
        var d = ddx * ddx + ddy * ddy;
        if (!best || d < best.d) best = { node: n, d: d };
        if (d === 0) break;
      }
      if (best && best.d === 0) break;
    }
    if (!best) return null;
    var node = best.node, len = node.nodeValue.length;
    var stride = Math.max(1, Math.ceil(len / 32)), bo = 0, bd = Infinity;
    var charD = function (i) {
      var r2 = document.createRange();
      try { r2.setStart(node, i); r2.setEnd(node, Math.min(len, i + 1)); } catch (e) { return Infinity; }
      var rc2 = r2.getBoundingClientRect();
      if (!rc2 || (!rc2.width && !rc2.height)) return Infinity;
      var cx = (rc2.left + rc2.right) / 2, cy = (rc2.top + rc2.bottom) / 2;
      return (cx - px) * (cx - px) + (cy - py) * (cy - py);
    };
    for (var k = 0; k < len; k += stride) { var d1 = charD(k); if (d1 < bd) { bd = d1; bo = k; } }
    for (var j = Math.max(0, bo - stride); j < Math.min(len, bo + stride + 1); j++) { var d2 = charD(j); if (d2 < bd) { bd = d2; bo = j; } }
    if (bd === Infinity) return null;
    return offsetOf(secEl, node, bo);
  }
  function _noteUpgradeAnchor(secEl, anchor) {   // 旧比例锚 → 内容锚(纯图/无文字 → null,保留 x/y 兜底)
    try {
      var sr = secEl.getBoundingClientRect();
      if (!sr.width || !sr.height) return null;
      var px = sr.left + Math.max(0, Math.min(1, anchor.x || 0)) * sr.width;
      var py = sr.top + Math.max(0, Math.min(1, anchor.y || 0)) * sr.height;
      var off = _noteNearestOff(secEl, px, py);
      if (off == null) return null;
      var cr = _noteCharRect(secEl, off);
      if (!cr) return null;
      return { kind: 'epub', section: anchor.section, x: anchor.x || 0, y: anchor.y || 0,
               off: off, dx: px - cr.left, dy: py - cr.top };
    } catch (e) { return null; }
  }
  try {
    if (window.RC && RC.stickynote) {
      RC.stickynote.init({
        file: FREL,
        mount: function (anchor) {
          if (!anchor || anchor.kind !== 'epub') return null;
          var el;
          if (typeof anchor.section === 'string') {   // 用户页(u_*):容器=RC.userpages 的 .ep-usec(未插入 → null,待 mountPending 重试)
            el = (window.RC && RC.userpages && RC.userpages.elOf) ? RC.userpages.elOf(anchor.section) : null;
            if (!el || !el.isConnected) return null;
          } else {
            el = secEls[anchor.section];
            if (!el || loaded[anchor.section] !== true) return null;
          }
          var r = el.getBoundingClientRect();
          if (!r.width || !r.height) return null;
          var out = { el: el };
          if (anchor.off == null) {   // 懒迁移:旧 x/y 比例锚 → 内容锚(组件收到 out.anchor 会 PATCH 落库)
            var up = _noteUpgradeAnchor(el, anchor);
            if (up) { anchor = up; out.anchor = up; }
          }
          var pos = (anchor.off != null) ? _noteOffPos(el, anchor) : null;
          if (pos) { out.left = pos.left; out.top = pos.top; }
          else {   // 纯图/无文字 section:保留 x/y 比例兜底(渲染路径兼容两种 anchor)
            out.left = Math.max(0, Math.min(1, anchor.x || 0)) * r.width;
            out.top = Math.max(0, Math.min(1, anchor.y || 0)) * r.height;
          }
          return out;
        },
        // 创建/拖动松手取锚:caretFromPoint 在落点小范围候选点里找最近可数文字 → 内容锚;
        // 落不到文字(纯图/页缝)→ 退回 section 相对 x/y 比例锚(旧语义)。
        noteWordRect: function (x, y) {
          // #51 粒度=单词:caret 命中字符→两侧扩至空白/标点(cap 11)→Range 精确框(容器内坐标);落空白/标点=null(→横线)
          // 行优先(用户拍板):原点没中字时向**左**试探(锁左上角左侧同行文字,非斜上方)
          var pos = null, SEP = /[\s、。,.!?！？;；:：「」『』()（）【】\u3000]/;
          var _lx = [0, -14, -28, -44];
          for (var _li = 0; _li < _lx.length; _li++) {
            var p0 = caretFromPoint(x + _lx[_li], y);
            if (p0 && p0.node && p0.node.nodeType === 3 && col.contains(p0.node)) {
              var _ss = p0.node.nodeValue || '', _ii = Math.min(p0.offset, _ss.length - 1);
              if (_ii >= 0 && !SEP.test(_ss[_ii] || '')) { pos = p0; break; }
            }
          }
          if (!pos) return null;
          var s0 = pos.node.nodeValue || '', i0 = Math.min(pos.offset, s0.length - 1);
          var a = i0, b = i0;
          while (a > 0 && !SEP.test(s0[a - 1]) && i0 - a < 11) a--;
          while (b < s0.length - 1 && !SEP.test(s0[b + 1]) && b - i0 < 11) b++;
          var rg = document.createRange();
          rg.setStart(pos.node, a); rg.setEnd(pos.node, b + 1);
          var rr = rg.getBoundingClientRect();
          if (!rr.width) return null;
          var host = pos.node.parentElement && pos.node.parentElement.closest ? pos.node.parentElement.closest('.ep-sec, .ep-usec') : null;
          if (!host) return null;
          var hr = host.getBoundingClientRect();
          return { el: host, left: rr.left - hr.left, top: rr.top - hr.top, width: rr.width, height: rr.height, dist: 0 };
        },
        anchorFromPoint: function (x, y) {
          var cands = [[0, 0], [10, 8], [-10, 8], [24, 10], [0, 22], [-24, 10], [40, 12], [0, -14]];
          for (var i = 0; i < cands.length; i++) {
            var pos = caretFromPoint(x + cands[i][0], y + cands[i][1]);
            if (!pos || !pos.node || pos.node.nodeType !== 3 || !col.contains(pos.node) || !_countable(pos.node)) continue;
            var si = secOf(pos.node);
            if (!si) {   // 用户页(.ep-usec,不进 secEls):锚 section=u_* 字符串 id,偏移空间=用户页自身可数文本
              var _ue = pos.node.parentElement && pos.node.parentElement.closest ? pos.node.parentElement.closest('.ep-usec') : null;
              if (_ue && _ue.dataset.uid) si = { el: _ue, idx: _ue.dataset.uid };
            }
            if (!si) continue;
            if (typeof si.idx !== 'string' && (si.el.classList.contains('ph') || loaded[si.idx] !== true)) continue;
            var off = offsetOf(si.el, pos.node, pos.offset);
            var cr = _noteCharRect(si.el, off);
            var sr = si.el.getBoundingClientRect();
            if (!cr || !sr.width || !sr.height) continue;
            return { kind: 'epub', section: si.idx, x: (x - sr.left) / sr.width, y: (y - sr.top) / sr.height,
                     off: off, dx: x - cr.left, dy: y - cr.top };
          }
          var t = document.elementFromPoint(x, y);
          var _us = t && t.closest ? t.closest('.ep-usec') : null;   // 用户页兜底:比例锚,section=u_* 字符串
          if (_us && _us.dataset.uid) {
            var _ur = _us.getBoundingClientRect();
            if (_ur.width && _ur.height) return { kind: 'epub', section: _us.dataset.uid, x: (x - _ur.left) / _ur.width, y: (y - _ur.top) / _ur.height };
          }
          var sec = t && t.closest ? t.closest('.ep-sec') : null;
          if (!sec || sec.classList.contains('ph')) {
            // 任何位置都能钉(用户拍板 2026-07-21):点不在章上(章缝/页边灰区/被浮层元素挡)→ 找**最近的已加载章**,
            // 比例 clamp 进章——钉章缝=贴上一章底部、水平位置保持(PDF 路同款 fallback,27-rc-adapter)
            sec = null;
            var best = 1e18;
            col.querySelectorAll('.ep-sec:not(.ph)').forEach(function (s2) {
              var r2 = s2.getBoundingClientRect();
              if (!r2.width || !r2.height) return;
              var dx2 = x < r2.left ? r2.left - x : (x > r2.right ? x - r2.right : 0);
              var dy2 = y < r2.top ? r2.top - y : (y > r2.bottom ? y - r2.bottom : 0);
              var d2 = dx2 * dx2 + dy2 * dy2;
              if (d2 < best) { best = d2; sec = s2; }
            });
            if (!sec) return null;
          }
          var r = sec.getBoundingClientRect();
          if (!r.width || !r.height) return null;
          var _a0 = { kind: 'epub', section: parseInt(sec.dataset.idx, 10),
                   x: Math.max(0, Math.min(1, (x - r.left) / r.width)),
                   y: Math.max(0, Math.min(1, (y - r.top) / r.height)) };
          var _t2 = document.elementFromPoint(x, y);
          if (!(_t2 && _t2.closest && _t2.closest('.ep-sec') === sec)) _a0.clamped = 1;   // 最近章 fallback=clamped(插入横线)
          return _a0;
        },
        // 阶段3 AI 注入:双击便签 → noteInject(助手开着才处理:无笔画走文本通道,有笔画走合成图/视觉通道)
        onDoubleTap: function (note) { try { return noteInject(note); } catch (e) { return false; } },
        toast: toast
      });
      var nb = $('ep-note-btn');
      if (nb) nb.addEventListener('click', function () { RC.stickynote.createAtCenter(); });
      // v4 重定位时机③:window resize(旋转/分屏;rc-sidedrawer 开关也会派发合成 resize)→ 防抖重定位
      var _noteRszT;
      window.addEventListener('resize', function () {
        clearTimeout(_noteRszT);
        _noteRszT = setTimeout(function () { try { RC.stickynote.repositionAll(); } catch (e) {} }, 250);
      });
    }
  } catch (e) {}
  // ⭐ 收藏当前章(共享组件 rc-favorites;当前章 = _curTopIdx,同 scrubber/位置记忆口径)
  try {
    var _favBtn = $('ep-fav-btn');
    if (_favBtn) _favBtn.addEventListener('click', function () {
      if (window.RC && RC.favorites && RC.favorites.openPicker) RC.favorites.openPicker({ file: FREL, kind: 'epub', section: _curTopIdx | 0 });
      else toast('收藏组件未就绪,刷新重试');
    });
    // ⭐ 亮暗态:当前章已在任一收藏夹 → 亮(判定/节流/缓存都在 rc-favorites.bindStar 内)
    if (_favBtn && window.RC && RC.favorites && RC.favorites.bindStar)
      RC.favorites.bindStar(_favBtn, function () { return { file: FREL, kind: 'epub', section: _curTopIdx | 0 }; });
  } catch (e) {}
  // ➕ 插入页(用户页,共享组件 rc-userpages;设计 references/reader-userpages-favorites.md「一」)。
  // 锚定铁律自查:.ep-usec 不带 .ep-sec class、不进 secEls → secOf/_findTopIdx/onScroll/jumpTo/scrubber/
  // applyHl/装饰循环/墨迹 全按 secEls 或 '.ep-sec' 选择器走,插入元素天然不被命中;after=章序(1-based,
  // =idx+1;0=书首),原书 section 编号零挤占,已有高亮/便签/墨迹锚不动。
  function _upPlace(el, after, refEl) {
    if (!secEls.length) return false;   // 占位还没建好(onBuilt 前)→ 待下次 mountAll
    var prev = refEl || (after > 0 ? secEls[Math.min(after, secEls.length) - 1] : null);
    if (prev) {
      if (el.previousElementSibling === prev && el.parentNode === col) return true;   // 已在位,幂等跳过
      col.insertBefore(el, prev.nextSibling);
    } else {   // after=0 书首:插在第一章之前
      if (col.firstElementChild === el) return true;
      col.insertBefore(el, col.firstChild);
    }
    return true;
  }
  try {
    if (window.RC && RC.userpages) {
      RC.userpages.init({
        file: FREL, cls: 'ep-usec',
        instant: true,   // EPUB 插入页 = 整页空白 + Aa 即时编辑覆盖层(照 PDF overlay);PDF v1 legacy 不传 → 卡片模式
        place: _upPlace,
        afterCurrent: function () { return (_curTopIdx | 0) + 1; },   // 「在当前章之后」(1-based 章序)
        posLabel: function (a) { return a > 0 ? ('第 ' + a + ' 章(节)') : '书首'; },
        scrollTo: function (el) {
          try { var cr = content.getBoundingClientRect(), r = el.getBoundingClientRect(); content.scrollTop += (r.top - cr.top); } catch (e) {}
        },
        // 显示态渲染后:MathJax 排版 → 按 offset 复原插入页高亮(口径一致,照 PDF _ovTypesetThenHl);再复原本页存档墨迹
        onRender: function (el, p) {
          var body = el.querySelector('.rc-up-body');
          var applyAll = function () {
            if (!body || !body.isConnected) return;
            Object.keys(_hls).forEach(function (id) { var h = _hls[id]; if (h && h.anchor && h.anchor.section === p.id) { try { applyHl(body, h); } catch (e) {} } });
          };
          try { if (body && window.MathJax && MathJax.typesetPromise) { MathJax.typesetPromise([body]).then(applyAll).catch(applyAll); } else applyAll(); } catch (e) { applyAll(); }
          try { _epUpApplyInk(el, p.id); } catch (e) {}
        },
        // 删除插入页:顺手清该页高亮(服务端+缓存)与墨迹(避免孤儿;插入页不复用别处编号,清了不影响正文)
        onRemoved: function (p) {
          Object.keys(_hls).forEach(function (id) { var h = _hls[id]; if (h && h.anchor && h.anchor.section === p.id) { reqJson('DELETE', '/pdf/api/epub-highlights?file=' + encodeURIComponent(FREL) + '&id=' + encodeURIComponent(id), null, function () {}, function () {}); delete _hls[id]; } });
          if (_epInk.data[p.id]) { var _kf = (_epInk.fileOf && _epInk.fileOf[p.id]) || FREL; delete _epInk.data[p.id]; reqJson('POST', '/pdf/api/epub-ink', { file: _kf, idx: p.id, strokes: [] }, function () {}, function () {}); }   // 收藏夹挂载页墨迹在原书 → 清对文件(审查:否则原书墨迹成孤儿)
          // 收藏夹视图:同步当场移除该页所在的整节(分隔条+容器;真·增量,不刷新)。后端级联已把收藏夹条目删掉+后台重建
          try { var _fc = document.querySelector('.fav-item-userpage[data-uid="' + p.id + '"]'); if (_fc) { var _fs = _fc.closest('.ep-sec'); (_fs || _fc).style.display = 'none'; } } catch (e) {}
        }
      });
      var _upBtn = $('ep-upage-btn');
      if (_upBtn) _upBtn.addEventListener('click', function () { RC.userpages.create(); });
    }
  } catch (e) {}
  fetch('/pdf/api/epub-css?file=' + encodeURIComponent(FREL)).then(function (r) { return r.text(); }).then(function (css) { $('ep-book-css').textContent = css; dbg('book css injected ' + css.length + 'B'); }).catch(function () {});
  initRender();
  setTimeout(loadHls, 800);
  _inkInit();   // 手写墨迹层:委托监听 + 工具栏 + 延迟加载 sidecar(照搬 PDF,按章 .ep-sec 锚定)
  // 右侧统一抽屉(照搬 PDF #grammar-panel):把手 + 6 tab(助手/单词本/知识点/高亮/目录/语法),懒填充
  if (window.RC && RC.sidedrawer) {
    RC.sidedrawer.init({
      handleLabel: '助手 · 知识点',
      defaultTab: 'asst',
      // ⑤ 开/关抽屉挤压正文导致的「跳页」:类名切换前取阅读锚(顶部可见节+节内比例),重排后滚回等效位置。
      // 便签 v4 重定位时机①:重排后字符位置变 → 恢复阅读位置的同一时机重算便签像素位置(repositionAll)。
      onLayoutChange: function () {
        var restore = null;
        try { restore = _keepReadPos(); } catch (e) {}
        return function () {
          if (restore) { try { restore(); } catch (e) {} }
          try { if (window.RC && RC.stickynote && RC.stickynote.repositionAll) RC.stickynote.repositionAll(); } catch (e2) {}
        };
      },
      // 显式给全部 tab(含新增「语法」);沿用 rc-sidedrawer DEFAULT_TABS 里前 5 个的图标,语法图标照搬
      // epub2-grammar.js 已验证过的 GRAMMAR_TAB_ICON(跟 epub.js 版视觉一致)。
      tabs: [
        { name: 'asst', label: '助手', icon: '<svg class="si" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4l1.4 4.2L18 9.6l-4.6 1.4L12 16l-1.4-4.6L6 9.6l4.6-1.4L12 4z"/><path d="M18.6 14.5l.6 1.7 1.7.6-1.7.6-.6 1.7-.6-1.7-1.7-.6 1.7-.6.6-1.7z"/></svg>' },
        { name: 'vocab', label: '单词本', icon: '<svg class="si" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M6 4h11a1 1 0 0 1 1 1v15H8a2 2 0 0 1-2-2V4z"/><path d="M6 4a2 2 0 0 0-2 2v12a2 2 0 0 1 2-2h12"/></svg>' },
        { name: 'kg', label: '知识点', icon: '<svg class="si" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1.2" fill="currentColor" stroke="none"/><circle cx="4.5" cy="12" r="1.2" fill="currentColor" stroke="none"/><circle cx="4.5" cy="18" r="1.2" fill="currentColor" stroke="none"/></svg>' },
        { name: 'hl', label: '高亮', icon: '<svg class="si" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 20h6M14 4l6 6-8.5 8.5H7v-4.5L14 4z"/></svg>' },
        { name: 'toc', label: '目录', icon: '<svg class="si" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M4 6h16M4 12h16M4 18h16"/></svg>' },
        { name: 'grammar', label: '语法', icon: '<svg class="si" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M5 4v6a3 3 0 0 0 3 3h8M19 4v6a3 3 0 0 1-3 3"/><circle cx="5" cy="3.5" r="1.4" fill="currentColor" stroke="none"/><circle cx="19" cy="3.5" r="1.4" fill="currentColor" stroke="none"/><circle cx="12" cy="20" r="1.4" fill="currentColor" stroke="none"/><path d="M12 13v5"/></svg>' },
        { name: 'hist', label: '历史', icon: '<svg class="si" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>' }   // 查询结果历史(镜像 PDF,统一抽屉 tab 对等)
      ],
      onTab: function (name) {
        if (name === 'asst') {
          try { fetch('/api/assistant/prewarm', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}', keepalive: true }); } catch (e) {}   // 切到助手 tab 就预热待命 Claude CLI 进程(照 PDF __asstPrewarm,通用端点跟具体书无关)
          try { if (window.__renderFigChips) window.__renderFigChips(); } catch (e) {}   // 开助手前点了图 → 进来补渲附件条
          try { if (window.__renderNoteChips) window.__renderNoteChips(); } catch (e) {}  // 补渲便签 chip(双击便签带进来的)
          try { if (window.__renderFocusSel) window.__renderFocusSel(); } catch (e) {}   // 防御性重渲(DOM 可能被章节重建)
          try { if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission().catch(function () {}); } catch (e) {}
          setTimeout(function () { var ta = $('ep-ai-ta') || $('asst-ta'); if (ta) ta.focus(); }, 120);   // 共享侧栏输入框 id=asst-ta
        }
        else if (name === 'kg') { if (window.RC && RC.knowledge) RC.knowledge.load(); }
        else if (name === 'hl') { loadHlPane(); }
        else if (name === 'hist') { _renderQhist(); }
        else if (name === 'vocab') { if (!_vocabLoaded) loadVocabPane(); }   // 首次进单词本自动载(照搬 PDF)
        else if (name === 'grammar') { loadGrammarPane(); }
        // toc 已由 buildToc 在 manifest 加载时填好 #ep-toc-list,无需懒填
      }
    });
  }
  // 知识点:embedded 模式 → rc-knowledge 不自建抽屉/把手,只把节点卡渲进抽屉的 #ep-kg-nodes
  if (window.RC && RC.knowledge) {
    RC.knowledge.init({
      embedded: true,
      fetchNodes: function () {
        return fetch('/pdf/api/epub-nodes?file=' + encodeURIComponent(FREL)).then(function (r) { return r.json(); }).then(function (d) { return d.nodes || []; });
      }
    });
  }

  function _viewportHasInk() {   // 当前视口内任一章有手写笔迹?(getContext 的 want_viewshot=据此让共享 send 预拍视口截图给 AI 看笔迹)
    try {
      var secs = document.querySelectorAll('.ep-sec, .ep-usec');
      var vh = window.innerHeight || document.documentElement.clientHeight;
      for (var i = 0; i < secs.length; i++) {
        var r = secs[i].getBoundingClientRect();
        if (r.bottom < 0 || r.top > vh) continue;   // 不在视口 → 跳过
        var st = secs[i].__inkStrokes;
        if (st && st.length) return true;
      }
    } catch (e) {}
    return false;
  }

  // ════════ EpubHtmlAdapter:把 epub-html.js(HTML 直渲 EPUB)收敛进统一中间层 RC.adapter 契约 ════════
  // 设计见 /reader-middlelayer-design.md。此前 epub-html.js 是 direct driver、无 adapter 对象 →
  // 现注册进 RC._adapter,让上层(助手)经 RC.adapter() 统一取上下文/图,不再直连本地函数(_epCollectFigures)。
  // 名字避开 epub2.js 的 EpubAdapter(那是 epub.js iframe 版)。所有 config 消费者已核实:RC.config 零消费、
  // RC.endpoints 返回{}不变、toast 回退默认 → 注册对现有 EPUB 页零影响。
  var EpubHtmlAdapter = {
    kind: 'epub-html',
    config: { isPDF: false, reflow: true, hasFigures: true, hasFormula: true, hasImages: true,
              renderRegion: false, dictMode: 'sse', popupMode: 'fixed', clickWordDetect: true,
              anchorKind: 'epub-offset', supportsVoice: true },   // ㉟:语音全链路已对齐(rc-voicecall 上页 + page=section idx+1 分流)
    getEndpoints: function () { return {}; },
    fileInfo: function () { return { file: FREL, langs: (CFG && CFG.langs) || [] }; },
    // 助手上下文(章 idx/toc/选区/图/便签)——收口自 runAssistant 内联组装,产出该阅读器后端(epub_assistant.py)所需形状
    getContext: function (opts) {
      opts = opts || {}; var sel = opts.selection || {};
      // ③-4b:共享侧栏 ctx() 无参调用 → 这里自采选中(镜像 sendChat:钉住焦点 chip 优先于隐式选中,
      //   选区来自 cur 时定格偏移锚)。内联 runAssistant 传 opts.selection 时不进此分支(零回归)。
      if (!sel.sel) {
        try {
          var _si = curSelection() || {};
          if (window.__focusSel && window.__focusSel.text) _si = { sel: window.__focusSel.text, sent: _si.sent || '' };
          if (_si.sel && cur.anchor && (cur.text || '').trim() === _si.sel.trim())
            _si.anchor = { section: cur.anchor.section, start: cur.anchor.start, end: cur.anchor.end };
          if (_si.sel) sel = _si;
        } catch (e) {}
      }
      return {
        file: FREL, book: (CFG && CFG.fileName) || '',
        langs: bookLangsArr(),   // M1:书语言(en/ja…)→ 后端 meta「书语言」,AI 不必猜语言(镜像 PDF)
        visible_text: (function () { try { return _visibleText(); } catch (e) { return ''; } })(),   // ③-4b:视口焦点收进 adapter——共享侧栏 ctx() 的本地 _visibleText 是 PDF page-wrap 版,EPUB 上为空(726df1 视口焦点修复会丢);内联路径有 !visible_text 守卫不双填
        visible_vocab: (window.__lastVocab || []).map(function (v) { return v.lemma; }).filter(Boolean).slice(0, 50),   // M2:本页下划线生词(镜像 PDF 05-nav:446)
        current_section_idx: _curTopIdx, total_sections: COUNT, toc: TOC,
        selection: sel.sel || '', selection_sentence: sel.sent || '', selection_anchor: sel.anchor || undefined,
        figures: _epCollectFigures(),
        want_viewshot: _viewportHasInk(),   // EPUB 笔迹画在 HTML 上、服务端渲不了 → 视口有笔迹时让共享 send 预拍一张视口截图塞 view_image(PdfAdapter 不返回此字段=PDF 走服务端裁图不受影响)
        notes: (window.__noteAttached || []).filter(function (n) { return !n.has_ink; }).slice(0, 4).map(function (n) {
          return { id: n.id, text: String(n.text || '').slice(0, 2000), near: String(n.near || '').slice(0, 1200), section: n.section };
        })
      };
    },
    // 语音专用的当前页墨迹接口。完整 strokes 不混入通用 getContext，避免每次
    // 位置/选区同步都复制大数组；rc-voicecall 只在自己的 2s/事件同步中读取。
    getVoiceInk: function (pageHint) {
      var requestedPage = parseInt(pageHint, 10);
      var exactPage = Number.isFinite(requestedPage) && requestedPage > 0;
      var el = null;
      if (exactPage) {
        var requestedSection = secEls[requestedPage - 1];
        el = (requestedSection && _favUpElIn(requestedSection)) || requestedSection || null;
      } else {
        el = _epInk.lastEl;
      }
      try {
        if (el && document.body.contains(el)) {
          var rect = el.getBoundingClientRect();
          if (!exactPage && (rect.bottom <= 0 || rect.top >= (window.innerHeight || 0))) el = null;
        } else el = null;
      } catch (e) { el = null; }
      if (!el) {
        var section = secEls[_curTopIdx];
        el = (section && _favUpElIn(section)) || section || null;
      }
      var strokes = [];
      if (el) {
        if (Array.isArray(el.__inkStrokes)) strokes = el.__inkStrokes;
        else {
          var idx = _inkIdxOf(el);
          if (Array.isArray(_epInk.data[idx])) strokes = _epInk.data[idx];
        }
      }
      return { page: _voicePageOfInkEl(el), strokes: strokes || [] };
    },
    // 图 + 用户手写圈点采集(reader-agnostic 目标;当前返回 epub-html 现有形状,后续增量再归一化成统一 Figure DTO)
    collectFigures: function () { return _epCollectFigures(); },
    // DocumentHost 迁移入口：复用现有 cur/offset 锚，不另建第二套选区。
    captureSelection: function () {
      var s = curSelection();
      if (!s || !s.sel) return null;
      var same = cur.text && String(cur.text).trim() === String(s.sel).trim();
      var r = same && cur.rect ? cur.rect : null;
      return RC.contract.selection({
        text: s.sel,
        context: s.sent || '',
        anchor: same && cur.anchor ? {
          kind: 'epub-offset',
          section: cur.anchor.section,
          start: cur.anchor.start,
          end: cur.anchor.end
        } : null,
        rect: r ? { left: r.left, top: r.top, right: r.right, bottom: r.bottom, width: r.width, height: r.height } : null
      });
    },
    clearSelection: function () {
      try { var s = window.getSelection(); if (s && s.removeAllRanges) s.removeAllRanges(); } catch (_) {}
      hideSel();
    },
    currentLocation: function () { return { unit: 'section', index: _curTopIdx, total: COUNT }; },
    navigate: function (target) {
      var a = target && target.data ? target.data : (target || {});
      var idx = a.section != null ? a.section : (a.index != null ? a.index : (a.page != null ? a.page : target));
      idx = parseInt(idx, 10);
      if (isNaN(idx)) return false;
      jumpTo(Math.max(0, Math.min(COUNT - 1, idx)), false);
      return true;
    },
    // ③-4a:EPUB 的共享侧栏 host(asst 契约,对齐 PdfAdapter._host.asst)。RC.adapter()._host.asst 取到。
    //   纯新增,不动现有内联助手;③-4b EPUB 挂 RC.assistant.mountPdfSidebar() 时才生效(flag 门控 + 浏览器验证)。
    //   页↔章语义:章 idx=显示(dispPage/pdfFromDisp 恒等);PDF 字符层/词组高亮专属方法→no-op(EPUB 用 _jumpFlashSel 替 flashSelOnPage)。
    _host: {
      asst: {
        md: function (t) { return renderMd(t); },
        toast: function (m) { try { toast(m); } catch (_) {} },
        fmtTime: function (ms) { try { var s = Math.round((Date.now() - (ms || 0)) / 1000); return s < 60 ? (s + '秒前') : (s < 3600 ? (Math.round(s / 60) + '分钟前') : (Math.round(s / 3600) + '小时前')); } catch (_) { return ''; } },
        fileRel: function () { return FREL; },
        pdfNumPages: function () { return COUNT; },
        locCount: function () { return COUNT; },
        goTo: function (loc) { try { jumpTo(parseInt(loc, 10) || 0, false); if (typeof _drawerAfterJump === 'function') _drawerAfterJump(); } catch (_) {} },
        goToInBook: function (fr, loc) { try { location.href = '/pdf/epub/view?file=' + encodeURIComponent(fr) + '#sec=' + loc; } catch (_) {} },
        dispPage: function (p) { return p; },
        pdfFromDisp: function (d) { return d; },
        changePage: function (dd) { try { jumpTo((parseInt(_curTopIdx, 10) || 0) + (dd || 0), false); } catch (_) {} },
        fitWidth: function () {},
        zoomBy: function () {},
        toggleTranslate: function () {},
        openDrawer: function () { try { if (window.RC && RC.sidedrawer) RC.sidedrawer.open('asst'); } catch (_) {} },
        switchTab: function (n) { try { if (window.RC && RC.sidedrawer) RC.sidedrawer.open(n); } catch (_) {} },
        asstOpen: function () { try { var s = document.getElementById('ep-side'); var pane = s && s.querySelector('.ep-side-pane[data-pane="asst"]'); return !!(pane && pane.classList.contains('active')); } catch (_) { return false; } },
        voiceContext: function () { return null; },
        setFocusSel: function (t, k) { try { window.__setFocusSel && window.__setFocusSel(t, k); } catch (_) {} },
        focusSel: function () { return window.__focusSel || null; },
        clearFigFocus: function () { try { window.__clearFigAttached && window.__clearFigAttached(); } catch (_) {} },
        figThumb: function () {},
        locLabel: function (idx) { try { return chapLabelOf(parseInt(idx, 10) || 0) || ''; } catch (_) { return ''; } },
        locNoun: function () { return '章'; },   // greet/提示语位置量词(PDF 默认「页」)
        // 清理批次:EPUB 动作卡基建(持久撤销/重做卡)接给共享侧栏——SSE 'action' 事件 + 历史 m.actions 回放 + 后台任务完成持久卡
        showAction: function (rec) { try { return _epShowAction(rec); } catch (_) { return null; } },
        queueAction: function (rec) { try { _epQueueAction(rec); } catch (_) {} },
        taskAction: function (uid) { try { _epTaskAction(uid); } catch (_) {} },
        noteAttached: function () { return window.__noteAttached || []; },
        clearNoteAttached: function () { try { window.__clearNoteAttached && window.__clearNoteAttached(); } catch (_) {} },
        renderNoteChips: function () { try { if (typeof renderNoteChips === 'function') renderNoteChips(); } catch (_) {} },
        notesReload: function () { try { window.notesReload && window.notesReload(); } catch (_) {} },
        noteInject: function () { return false; },
        reloadHighlights: function () { try { if (typeof loadHls === 'function') loadHls(); } catch (_) {} },
        loadAllHighlights: function () { try { if (typeof loadHls === 'function') loadHls(); } catch (_) {} },
        renderHighlightsOnPage: function () {},
        showHlPicker: function (d) { try { window._showHlPicker && window._showHlPicker(d); } catch (_) {} },
        assistEdit: function (d) { try { if (typeof _epAssistEdit === 'function') _epAssistEdit(d); } catch (_) {} },
        renderPhraseHl: function () {},
        removePhraseHighlight: function () {},
        activePhraseHl: function () { return null; },
        setActivePhraseHl: function () {},
        charsRangeToText: function () { return ''; },
        charRangeToPtRects: function () { return []; },
        flashSelOnPage: function (loc, text) { try { if (typeof _jumpFlashSel === 'function') _jumpFlashSel(parseInt(loc, 10) || 0, null, text); } catch (_) {} },
        noteNearText: function (a) { try { return (typeof _noteNearText === 'function') ? _noteNearText(a) : ''; } catch (_) { return ''; } },
        jumpToCtx: function (m) { try { var sec = (m && (m.section != null ? m.section : m.page)) || 0; jumpTo(parseInt(sec, 10) || 0, false); if (typeof _drawerAfterJump === 'function') _drawerAfterJump(); } catch (_) {} },
        prewarm: function (off) { try { fetch('/api/assistant/prewarm', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(off ? { off: 1 } : {}), keepalive: true }); } catch (_) {} },
        getPaidNoted: function () { try { return !!window.__paidNoted; } catch (_) { return false; } },
        setPaidNoted: function (v) { try { window.__paidNoted = v; } catch (_) {} },
        hlUrl: function () { return '/pdf/api/epub-highlights'; },
        notesUrl: function () { return '/pdf/api/notes'; },
        noteCompositeUrl: function () { return '/pdf/api/note-composite'; },
        // ③-4b:chat/history/clear → EPUB 后端(epub_assistant.py);后端按原形取 file,故 history/clear 带 ?file=
        chatUrl: function () { return '/pdf/api/epub-assistant'; },
        historyUrl: function () { return '/pdf/api/epub-convo?file=' + encodeURIComponent(FREL); },
        clearUrl: function () { return '/pdf/api/epub-convo/clear?file=' + encodeURIComponent(FREL); },
        // ㉟ 语音通话轮次落库:进本书 epub-convo(与侧栏历史/清空同源同清;复用现成 append 端点,一轮两条)
        voiceLog: function (q, a, page) {
          [['user', q], ['assistant', a]].forEach(function (p) {
            if (!p[1]) return;
            try {
              fetch('/pdf/api/epub-convo/append', { method: 'POST', headers: { 'Content-Type': 'application/json' }, keepalive: true,
                body: JSON.stringify({ file: FREL, role: p[0], content: String(p[1]).slice(0, 4000), section: (page || 1) - 1 }) }).catch(function () {});
            } catch (e) {}
          });
        },
        mountPanel: function () { return document.getElementById('ep-side'); },
        mountTabs: function () { return document.getElementById('ep-side-tabs') || document.getElementById('ep-side'); }
      }
    }
  };
  try { if (window.RC && RC.use) { RC.use(EpubHtmlAdapter); window.__epubHtmlAdapter = EpubHtmlAdapter; } } catch (e) {}

  // ════════ PWA 书籍宿主白名单：扩展只调动作，章节 DOM/锚/sidecar 仍由本页拥有 ════════
  try {
    if (window.BWReaderBookHost && !window.__bwReaderLocalApi) {
      var _bookRoute = '';
      try { _bookRoute = String(document.querySelector('meta[name="bw-reader-route"]').getAttribute('content') || ''); } catch (_) {}
      var _bookMode = _bookRoute === 'favorite' ? 'favorite' : 'epub';
      function _bookSelection() {
        var value = EpubHtmlAdapter.captureSelection();
        if (!value) return null;
        value.file = FREL;
        value.book = (CFG && CFG.fileName) || document.title || '';
        value.langs = bookLangsArr();
        value.page = (_curTopIdx | 0) + 1;
        value.location = { unit: 'section', index: _curTopIdx | 0, total: COUNT };
        return value;
      }
      function _bookAction(name, payload) {
        payload = payload || {};
        if (name === 'clear_selection') {
          EpubHtmlAdapter.clearSelection();
          return { ok: true };
        }
        if (name === 'highlight') {
          var selected = _bookSelection();
          if (!selected || !selected.anchor) return Promise.reject(new Error('没有可标记的 EPUB 选区'));
          return saveHl(selected.text, selected.anchor, String(payload.color || '')).then(function (record) {
            if (!record) throw new Error('EPUB 高亮保存失败');
            return { ok: true, highlight: record };
          });
        }
        if (name === 'open_search') {
          sp.classList.add('open');
          var input = $('ep-search-in'); if (input) input.focus();
          return { ok: true };
        }
        if (name === 'toggle_ruby') {
          $('ep-ruby').click();
          return { ok: true, ruby: !!_deco.ruby, translate: !!_deco.pagetr };
        }
        if (name === 'toggle_page_translate') {
          $('ep-pagetr').click();
          return { ok: true, ruby: !!_deco.ruby, translate: !!_deco.pagetr };
        }
        if (name === 'create_sticky') {
          if (!(window.RC && RC.stickynote && RC.stickynote.createAtCenter)) throw new Error('EPUB 便签层尚未就绪');
          RC.stickynote.createAtCenter();
          return { ok: true };
        }
        if (name === 'toggle_ink') {
          _inkToggleMode();
          return { ok: true, active: !!_epInk.mode };
        }
        if (name === 'anchor_fx') {
          if (window.RC && RC.stickynote && RC.stickynote.anchorFx) {
            if (payload.show) RC.stickynote.anchorFx.show(Number(payload.x) || 0, Number(payload.y) || 0);
            else RC.stickynote.anchorFx.hide();
          }
          return { ok: true };
        }
        if (name === 'jump_page' || name === 'jump_location') {
          var loc = payload.location || payload;
          var idx = loc.section != null ? loc.section
            : (loc.index != null ? loc.index : Math.max(0, (Number(loc.page) || 1) - 1));
          idx = Math.max(0, Math.min(COUNT - 1, parseInt(idx, 10) || 0));
          jumpTo(idx, false);
          return { ok: true, section: idx };
        }
        if (name === 'change_page') {
          jumpTo(Math.max(0, Math.min(COUNT - 1, (_curTopIdx | 0) + (Number(payload.delta) || 0))), false);
          return { ok: true };
        }
        if (name === 'jump_context') {
          var target = payload.context || payload || {};
          var file = String(target.file || target.file_rel || '');
          var section = target.section != null ? target.section : Math.max(0, (Number(target.page) || 1) - 1);
          if (file && file !== FREL && window.openBookAt) window.openBookAt(file, Number(section) + 1);
          else jumpTo(Math.max(0, Math.min(COUNT - 1, parseInt(section, 10) || 0)), false);
          return { ok: true };
        }
        if (name === 'flash_selection') {
          if (typeof _jumpFlashSel === 'function') {
            _jumpFlashSel(Math.max(0, (Number(payload.page) || 1) - 1), null, String(payload.text || ''));
          }
          return { ok: true };
        }
        if (name === 'pin_card') {
          var cards = Array.isArray(payload.cards) ? payload.cards.slice(0, 50) : [];
          if (!cards.length) throw new Error('没有可钉住的卡片');
          if (!(window.RC && RC.stickynote && RC.stickynote.createCardAt)) throw new Error('EPUB 卡片便签尚未就绪');
          RC.stickynote.createCardAt(
            Number(payload.x) || (window.innerWidth || 1024) / 2,
            Number(payload.y) || (window.innerHeight || 768) / 2,
            cards,
            String(payload.gid || '')
          );
          return { ok: true };
        }
        if (name === 'pin_html') {
          var html = payload.html || {};
          if (!html.content) throw new Error('没有可粘贴的工具卡内容');
          if (!(window.RC && RC.stickynote && RC.stickynote.createHtmlAt)) throw new Error('EPUB 工具卡便签尚未就绪');
          var ok = RC.stickynote.createHtmlAt(
            Number(payload.x) || (window.innerWidth || 1024) / 2,
            Number(payload.y) || (window.innerHeight || 768) / 2,
            {
              content: String(html.content || ''),
              isHtml: !!html.isHtml,
              label: String(html.label || '卡片'),
              type: String(html.type || ''),
              icon: String(html.icon || ''),
              form: String(html.form || 'full'),
              cid: String(html.cid || payload.cid || '')
            }
          );
          if (!ok) throw new Error('请把工具卡放到书页正文上再松手');
          return { ok: true };
        }
        if (name === 'toggle_fullscreen') {
          $('fs-toggle').click();
          return { ok: true };
        }
        if (name === 'open_settings') {
          openSettings();
          return { ok: true };
        }
        if (name === 'open_favorite') {
          var fav = $('ep-fav-btn'); if (!fav) throw new Error('收藏组件尚未就绪');
          fav.click();
          return { ok: true };
        }
        if (name === 'create_user_page') {
          if (!(window.RC && RC.userpages && RC.userpages.create)) throw new Error('插入页组件尚未就绪');
          RC.userpages.create();
          return { ok: true };
        }
        throw new Error('不允许的 EPUB 本地命令：' + name);
      }
      var _bookActionNames = [
        'clear_selection', 'highlight', 'open_search', 'toggle_ruby', 'toggle_page_translate',
        'create_sticky', 'toggle_ink', 'anchor_fx', 'jump_page', 'jump_location',
        'change_page', 'jump_context', 'flash_selection', 'pin_card', 'pin_html',
        'toggle_fullscreen', 'open_settings', 'open_favorite', 'create_user_page'
      ];
      var _bookActions = {};
      _bookActionNames.forEach(function (name) {
        _bookActions[name] = function (payload) { return _bookAction(name, payload); };
      });
      var _bookLocalApi = BWReaderBookHost.register({
        mode: _bookMode,
        file: FREL,
        title: (CFG && CFG.fileName) || document.title || '',
        langs: bookLangsArr(),
        selection: _bookSelection,
        context: function () { return EpubHtmlAdapter.getContext(); },
        currentLocation: function () { return EpubHtmlAdapter.currentLocation(); },
        actions: _bookActions,
        capabilities: {
          selection: true, context: true, highlight: true,
          bookSearch: true, ruby: true, pageTranslate: true,
          stickyNote: true, ink: true, anchorFx: true,
          pinCard: true, pinHtmlCard: true, jumpPage: true, navigation: true,
          fullscreen: true, bookSettings: true, favorite: true, userPage: true
        }
      });
      if (window.RC && RC.actions) {
        var _bookMeta = function (storage) { return { owner: 'pwa', runtime: 'native', storage: storage }; };
        RC.actions.bind('highlight.save', function (p) { return _bookLocalApi.localAction('highlight', p); }, _bookMeta('book-sidecar'));
        RC.actions.bind('ink.toggle', function () { return _bookLocalApi.localAction('toggle_ink', {}); }, _bookMeta('book-sidecar'));
        RC.actions.bind('note.create', function () { return _bookLocalApi.localAction('create_sticky', {}); }, _bookMeta('book-sidecar'));
        RC.actions.bind('reading.ruby.toggle', function () { return _bookLocalApi.localAction('toggle_ruby', {}); }, _bookMeta('device-local'));
        RC.actions.bind('translation.page.toggle', function () { return _bookLocalApi.localAction('toggle_page_translate', {}); }, _bookMeta('device-local'));
        RC.actions.bind('pin.card', function (p) { return _bookLocalApi.localAction('pin_card', p); }, _bookMeta('book-sidecar'));
        RC.actions.bind('pin.html', function (p) { return _bookLocalApi.localAction('pin_html', p); }, _bookMeta('book-sidecar'));
        RC.actions.bind('pin.anchorFx', function (p) { return _bookLocalApi.localAction('anchor_fx', p); }, _bookMeta('none'));
      }
    }
  } catch (e) {
    try { console.warn('[BW] EPUB 书籍宿主登记失败', e); } catch (_) {}
  }

  // ════════ ③-4b:?asst=shared → EPUB 退役内联助手,改挂共享侧栏(rc-assistant.js mountPdfSidebar)════════
  //   默认(无 flag)完全走内联助手,零影响 → 浏览器验证 ?asst=shared 通过后再翻默认。
  //   共享侧栏经 EpubHtmlAdapter._host.asst 的 HOST 取所有 reader 触点:chat/history/clear 端点→epub-*、
  //   context→getContext()、导航→jumpTo、注解→epub-highlights;SSE 事件协议两后端本就同款。
  //   步骤:① 摘掉模板内联 asst pane + RC.sidedrawer 建的内联 asst tab(避免 data-pane="asst" 撞两份)
  //         ② mountPdfSidebar() 自建共享 tab+pane 进 #ep-side-tabs/#ep-side
  //         ③ 把共享 tab/pane 的 PDF class(side-tab/side-pane)补成 EPUB 抽屉 class → setTab() 认得
  //   首次开抽屉时 open()→setTab(_lastTab) 会正确激活共享 pane(class/data-pane 已就位),无需手动同步初始态。
  try {
    if (window.RC && RC.assistant && RC.assistant.mountPdfSidebar) {   // 清理批次:内联助手已物理删除 → 共享侧栏无条件挂载(逃生舱随之退役)
      var _op = document.getElementById('ep-side-asst');                                   // 内联 asst pane(模板)
      if (_op && _op.parentNode) _op.parentNode.removeChild(_op);
      var _ot = document.querySelector('#ep-side-tabs .ep-side-tab[data-pane="asst"]');     // 内联 asst tab(RC.sidedrawer 建)
      if (_ot && _ot.parentNode) _ot.parentNode.removeChild(_ot);
      RC.assistant.mountPdfSidebar();
      var _nt = document.querySelector('#ep-side-tabs .side-tab[data-pane="asst"]');         // 共享 tab:PDF class → EPUB class
      if (_nt) { _nt.classList.remove('side-tab'); _nt.classList.add('ep-side-tab'); }
      var _np = document.getElementById('side-pane-asst');                                   // 共享 pane:补 EPUB class(setTab 靠它 + data-pane 找)
      if (_np) _np.classList.add('ep-side-pane');
      try { _favNotebookEntries(); } catch (e) {}   // 收藏夹 NotebookLM 三入口:内联 quick 区已随 pane 被摘 → 重注入共享 #asst-quick
    }
  } catch (e) {}
})();
