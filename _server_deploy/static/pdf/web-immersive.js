/* web-immersive.js — 网页沉浸式翻译引擎(双语对照)。
 *
 * 跑在**被代理的网页内部**(由 html_reader 的 _PROXY_INJECT 注入),不是外壳里。
 * 理由:段落识别要走真实 DOM,而代理页是同源文档;引擎住在页内,页内跳转也自动跟着走,
 * 外壳只管发指令(postMessage)——与选区/正文上报同一条桥。
 *
 * 段落粒度采用 FluentRead(开源沉浸式翻译)验证过的那套规则,核心是一条:
 *   **节点若含非空子元素 → 不翻它,下沉到子元素**。
 * 这条是"整页翻成一坨"和"逐词翻碎"之间的分界线,自己拍脑袋写必然踩坑。
 *
 * 三种样式(用户拍板:默认独立段落):
 *   para    独立段落 —— 译文另起一段,与原文同字号(默认)
 *   small   下方小字 —— 译文紧随其后,小一号、淡一点
 *   replace 原文直接代替 —— 只显示译文(点译文可临时看回原文)
 *
 * 触发时机沿用 PDF 阅读器「译页」的心智:一个开关,开着就把**当前可见**的段落译出来,
 * 滚到哪译到哪(IntersectionObserver),不预译整站——省钱也省等待。
 */
(function () {
  if (window.__rcImmersive) return;
  window.__rcImmersive = true;

  var ST = { on: false, style: 'para', busy: false, seq: 0 };
  var DONE = '__rcTrDone';          // 已处理标记(挂在节点上,避免重复翻)
  var MARK = 'rc-tr-block';

  // ── 段落识别(照搬 FluentRead 的三类集合)──
  var DIRECT = { H1:1, H2:1, H3:1, H4:1, H5:1, H6:1, P:1, LI:1, DD:1, DT:1,
                 BLOCKQUOTE:1, FIGCAPTION:1, CAPTION:1, TD:1, TH:1, SUMMARY:1 };
  var SKIP = { SCRIPT:1, STYLE:1, CODE:1, PRE:1, KBD:1, SAMP:1, VAR:1, TEXTAREA:1,
               INPUT:1, SELECT:1, OPTION:1, IFRAME:1, SVG:1, CANVAS:1, VIDEO:1,
               AUDIO:1, NOSCRIPT:1, HEAD:1, META:1, LINK:1, NAV:1, HEADER:1, FOOTER:1 };
  // 行内元素不算"非空子元素"——<p>文字 <a>链接</a> 文字</p> 必须整段翻,不能因为有 <a> 就下沉
  var INLINE = { A:1, SPAN:1, B:1, STRONG:1, I:1, EM:1, U:1, S:1, SMALL:1, SUB:1, SUP:1,
                 MARK:1, ABBR:1, CITE:1, Q:1, TIME:1, BR:1, WBR:1, FONT:1, LABEL:1 };

  function hasBlockChild(el) {
    for (var i = 0; i < el.children.length; i++) {
      var c = el.children[i];
      if (SKIP[c.tagName]) continue;
      if (INLINE[c.tagName]) continue;
      if ((c.textContent || '').trim()) return true;   // 非空的块级子元素 → 该下沉
    }
    return false;
  }

  // 界面骨架(导航/页眉页脚/目录/搜索框)不是正文 —— 实测维基把「主菜单/搜索/捐赠/登录」
  // 和整个目录树都翻了出来。光靠标签名不够(维基用 <div role="navigation">),得认 ARIA 角色。
  var CHROME = '[role=navigation],[role=banner],[role=contentinfo],[role=search],' +
               '[role=menu],[role=menubar],[role=toolbar],[role=tablist],[aria-hidden="true"]';

  function usable(el) {
    if (!el || el.nodeType !== 1) return false;
    if (SKIP[el.tagName] || el.closest('.' + MARK)) return false;
    if (el[DONE] || el.classList.contains('rc-tr-src')) return false;
    if (el.isContentEditable) return false;
    try { if (el.closest(CHROME)) return false; } catch (e) {}
    var t = (el.innerText || el.textContent || '').trim();
    if (t.length < 4) return false;                    // 太短(图标/序号)不值得翻
    if (!/[A-Za-z぀-ヿ一-鿿]/.test(t)) return false;   // 纯数字/符号跳过
    if (t.length > 3000) return false;
    // innerText 里出现换行 = 渲染上就是多块内容。DIRECT(<p>/<li>…)里可能只是 <br>,放行;
    // 非 DIRECT 的容器出现换行,基本都是"一堆并列链接/按钮"被当成一段(维基菜单就是这么混进来的)。
    if (!DIRECT[el.tagName] && /\n/.test(t)) return false;
    return true;
  }

  /** 收集可翻段落:DIRECT 直接收;其余容器只有"没有块级子元素"时才收(否则下沉)。 */
  function collect(root) {
    var out = [];
    var w = document.createTreeWalker(root || document.body, NodeFilter.SHOW_ELEMENT, {
      acceptNode: function (el) {
        if (SKIP[el.tagName]) return NodeFilter.FILTER_REJECT;
        if (el.classList && el.classList.contains(MARK)) return NodeFilter.FILTER_REJECT;
        try { if (el.matches(CHROME)) return NodeFilter.FILTER_REJECT; } catch (e) {}
        if (DIRECT[el.tagName] && !hasBlockChild(el)) return NodeFilter.FILTER_ACCEPT;
        if (!DIRECT[el.tagName] && !hasBlockChild(el) && el.children.length < 12)
          return NodeFilter.FILTER_ACCEPT;
        return NodeFilter.FILTER_SKIP;
      }
    });
    var n;
    while ((n = w.nextNode())) { if (usable(n)) out.push(n); }
    return out;
  }

  // ── 样式 ──
  function css() {
    if (document.getElementById('rc-tr-css')) return;
    var s = document.createElement('style');
    s.id = 'rc-tr-css';
    s.textContent =
      '.rc-tr-block{display:block;margin:.35em 0 .1em;color:inherit;' +
        'border-left:2px solid rgba(90,150,240,.42);padding-left:.6em;' +
        'font-family:inherit;text-align:left;white-space:normal}' +
      '.rc-tr-small{font-size:.86em;opacity:.78;border-left-color:rgba(90,150,240,.3);margin:.2em 0}' +
      '.rc-tr-load{opacity:.45;font-style:italic}' +
      '.rc-tr-src-hidden{display:none !important}' +
      '.rc-tr-peek{outline:1px dashed rgba(90,150,240,.6);cursor:pointer}' +
      // 未掌握词多的段落:呼吸框 + 单段「译」按钮(镜像 PDF 阅读器 12-vocab-sentences 的形态,
      // 那边是句级几何框,这边是段级 —— 段落就是网页的天然语义块)
      '.rc-vocab-hot{outline:1.5px solid rgba(255,176,80,.5);outline-offset:2px;border-radius:3px;' +
        'animation:rcVocabBreath 2.6s ease-in-out infinite}' +
      '@keyframes rcVocabBreath{0%,100%{outline-color:rgba(255,176,80,.22)}50%{outline-color:rgba(255,176,80,.62)}}' +
      '.rc-vocab-btn{display:inline-block;margin-left:.4em;padding:0 .45em;font-size:.75em;line-height:1.6;' +
        'border-radius:9px;background:rgba(255,176,80,.18);color:#c8801e;border:1px solid rgba(255,176,80,.45);' +
        'cursor:pointer;user-select:none;vertical-align:middle;font-style:normal;font-weight:600}';
    (document.head || document.documentElement).appendChild(s);
  }

  /** ⚠ 站点常给段落设 -webkit-line-clamp / text-overflow 省略号,译文塞进去会被截掉。
   *  FluentRead 的做法(smashTruncationStyle)是就地拆掉这几个属性,照抄。 */
  function unclamp(el) {
    try {
      var cs = getComputedStyle(el);
      if (cs.webkitLineClamp && cs.webkitLineClamp !== 'none') {
        el.style.webkitLineClamp = 'unset'; el.style.display = 'block';
      }
      if (cs.textOverflow === 'ellipsis') el.style.textOverflow = 'clip';
      if (cs.overflow === 'hidden') el.style.overflow = 'visible';
      if (cs.whiteSpace === 'nowrap') el.style.whiteSpace = 'normal';
      if (cs.maxHeight && cs.maxHeight !== 'none') el.style.maxHeight = 'none';
    } catch (e) {}
  }

  function attach(el, zh) {
    if (!zh) return;
    unclamp(el);
    try {   // 整段已译 → 未掌握词提示完成使命,撤掉(免得框和按钮跟译文并存)
      el.classList.remove('rc-vocab-hot');
      var vb = el.querySelector(':scope > .rc-vocab-btn'); if (vb) vb.remove();
    } catch (e) {}
    var box = document.createElement(el.tagName === 'LI' || el.tagName === 'TD' ? 'div' : 'span');
    box.className = MARK + (ST.style === 'small' ? ' rc-tr-small' : '');
    box.textContent = zh;
    box.setAttribute('data-rc-tr', '1');
    if (ST.style === 'replace') {
      // 原文收起(不删):点译文可临时看回去 —— 查词/高亮仍指向原文,不能真丢
      el.classList.add('rc-tr-peek');
      var kids = [];
      for (var i = 0; i < el.childNodes.length; i++) kids.push(el.childNodes[i]);
      var wrap = document.createElement('span');
      wrap.className = 'rc-tr-src rc-tr-src-hidden';
      kids.forEach(function (k) { wrap.appendChild(k); });
      el.appendChild(wrap);
      el.appendChild(box);
      el.addEventListener('click', function (ev) {
        if (ev.target.closest && ev.target.closest('a')) return;   // 别劫持链接
        wrap.classList.toggle('rc-tr-src-hidden');
        box.style.display = wrap.classList.contains('rc-tr-src-hidden') ? '' : 'none';
      });
    } else {
      el.appendChild(box);
    }
    el[DONE] = 1;
  }

  // ── 批量翻译(可见优先)──
  var PEND = [], PENDT = null;

  function enqueue(el, force) {
    if (el[DONE] || el.__rcQ) return;
    el.__rcQ = 1;
    if (force) el.__rcForce = 1;   // 单段按需翻译:与全局开关无关(生词按钮的意义就在这)
    PEND.push(el);
    if (PENDT) return;
    PENDT = setTimeout(flush, 220);      // 攒一批再发,别一段一个请求
  }

  function flush() {
    PENDT = null;
    // ⚠ 这里**不能**要求 ST.on:生词段落的「译 N」按钮就是给"没开全局翻译"时用的
    //   (实测漏了这条 → 点按钮框消失但译文不出来)。是否采纳结果在下面逐条判。
    if (!PEND.length) return;
    var batch = PEND.splice(0, 40);
    var texts = batch.map(function (e) { return (e.innerText || e.textContent || '').trim(); });
    var seq = ST.seq;
    batch.forEach(function (e) {
      var ph = document.createElement('span');
      ph.className = MARK + ' rc-tr-load' + (ST.style === 'small' ? ' rc-tr-small' : '');
      ph.textContent = '翻译中…';
      ph.setAttribute('data-rc-ph', '1');
      e.appendChild(ph);
    });
    // ⚠ 必须走 shim 留出的原始 fetch:页面里的 window.fetch 已被代理层 patch,
    //   它会把 /pdf/api/… 当成原站的相对路径翻走(实测 405)。
    var F = window.__rcRawFetch || window.fetch;
    F('/pdf/api/web-translate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ texts: texts })
    }).then(function (r) { return r.json(); }).then(function (d) {
      batch.forEach(function (e, i) {
        var ph = e.querySelector(':scope > [data-rc-ph]');
        if (ph) ph.remove();
        // 期间被关掉/换样式 → 丢弃;但**用户手点的单段**不受全局开关影响
        if (seq !== ST.seq || (!ST.on && !e.__rcForce)) { e.__rcQ = 0; return; }
        attach(e, (d && d.zh && d.zh[i]) || '');
        e.__rcQ = 0;
      });
      if (PEND.length && !PENDT) PENDT = setTimeout(flush, 120);
      report();
    }).catch(function () {
      batch.forEach(function (e) {
        var ph = e.querySelector(':scope > [data-rc-ph]');
        if (ph) ph.remove();
        e.__rcQ = 0;
      });
    });
  }

  // ── 未掌握词多的段落:自动标出来,一键单段翻译(不必开全局译)──
  function scanVocab(list) {
    var els = list.filter(function (e) { return !e.__rcVocab; });
    if (!els.length) return;
    els.forEach(function (e) { e.__rcVocab = 1; });
    var F = window.__rcRawFetch || window.fetch;
    F('/pdf/api/web-vocab', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ texts: els.map(function (e) { return (e.innerText || '').trim(); }) })
    }).then(function (r) { return r.json(); }).then(function (d) {
      (d.marks || []).forEach(function (m) {
        var el = els[m.i];
        if (!el || el[DONE] || el.querySelector('.rc-vocab-btn')) return;
        el.classList.add('rc-vocab-hot');
        var b = document.createElement('span');
        b.className = 'rc-vocab-btn';
        b.textContent = '译 ' + m.count;
        b.title = '这段有 ' + m.count + ' 个未掌握词:' + (m.words || []).join('、') + '\n点一下翻译这一段';
        b.onclick = function (ev) {
          ev.stopPropagation(); ev.preventDefault();
          b.remove(); el.classList.remove('rc-vocab-hot');
          el.__rcQ = 0; el[DONE] = 0;
          enqueue(el, true);
          clearTimeout(PENDT); PENDT = setTimeout(flush, 60);   // 手点要即时,别等攒批
        };
        el.appendChild(b);
      });
    }).catch(function () {});
  }

  function report() {
    try {
      var n = document.querySelectorAll('.' + MARK + ':not(.rc-tr-load)').length;
      parent.postMessage({ __rcweb: 'trstat', n: n, on: ST.on, style: ST.style }, '*');
    } catch (e) {}
  }

  // ── 只译看得见的(滚到哪译到哪)——与「译页」同样的按需心智,省钱省等待 ──
  var IO = null;
  function observe(list) {
    if (!IO) {
      IO = new IntersectionObserver(function (ents) {
        ents.forEach(function (en) {
          if (!en.isIntersecting) return;
          IO.unobserve(en.target);
          if (ST.on) enqueue(en.target);
        });
      }, { rootMargin: '80px 0px', threshold: 0.01 });
    }
    list.forEach(function (el) { try { IO.observe(el); } catch (e) {} });
  }

  /** 生词提示独立于翻译开关:进页面就扫(它不花翻译额度,只查本地词库)。 */
  function vocabPass() {
    css();
    var all = collect(document.body);
    var vis = all.filter(function (e) {
      var r = e.getBoundingClientRect();
      return r.top < innerHeight * 2.5 && r.bottom > -200;
    });
    scanVocab(vis.slice(0, 80));
  }
  window.__rcVocabPass = vocabPass;

  var MO = null;
  function watchDynamic() {
    if (MO) return;
    MO = new MutationObserver(function (ms) {
      if (!ST.on) return;
      var add = [];
      ms.forEach(function (m) {
        [].forEach.call(m.addedNodes || [], function (n) {
          if (!n || n.nodeType !== 1) return;
          if (n.classList && n.classList.contains(MARK)) return;   // 别把自己的译文当新内容
          add = add.concat(collect(n));
        });
      });
      if (add.length) observe(add);
    });
    MO.observe(document.body, { childList: true, subtree: true });
  }

  function start() {
    css();
    ST.on = true;
    observe(collect(document.body));
    watchDynamic();
    report();
  }

  function stop() {
    ST.on = false;
    ST.seq++;                                      // 让在途请求的结果作废
    PEND.length = 0;
    [].forEach.call(document.querySelectorAll('.' + MARK), function (b) { b.remove(); });
    [].forEach.call(document.querySelectorAll('.rc-tr-src'), function (w) {
      var p = w.parentNode; if (!p) return;
      while (w.firstChild) p.insertBefore(w.firstChild, w);
      w.remove();
      p.classList.remove('rc-tr-peek');
    });
    [].forEach.call(document.querySelectorAll('.rc-vocab-btn'), function (b) { b.remove(); });
    [].forEach.call(document.querySelectorAll('.rc-vocab-hot'), function (b) {
      b.classList.remove('rc-vocab-hot'); b.__rcVocab = 0;
    });
    [].forEach.call(document.querySelectorAll('*'), function (e) {
      if (e[DONE]) { e[DONE] = 0; e.__rcQ = 0; }
    });
    if (IO) { IO.disconnect(); IO = null; }
    report();
  }

  window.addEventListener('message', function (e) {
    var d = e.data || {};
    if (d.__rcweb !== 'translate') return;
    if (d.style && d.style !== ST.style) {
      var was = ST.on;
      if (was) stop();
      ST.style = d.style;
      if (was) start();
      return;
    }
    if (d.on === true && !ST.on) start();
    else if (d.on === false && ST.on) stop();
    else if (d.on === undefined) { ST.on ? stop() : start(); }
  });

  // 生词提示不等用户点「译」——页面稳定后自动跑一遍(纯本地词库查询,不烧翻译额度)。
  // 这与 PDF 阅读器一致:那边打开一页就把"未掌握词多的句子"框出来,不需要先开译页。
  function autoVocab() {
    try { vocabPass(); } catch (e) {}
  }
  // ⚠ 别挂在 load 上:代理下重站的子资源要排队(还有舱壁限流),load 可能很久不来甚至不来
  //   —— 实测维基页 8 秒内一次都没扫(手动调 vocabPass() 立刻出 4 段)。
  //   改成定时多跑几次:__rcVocab 标记天然幂等,重复调用零副作用。
  [1200, 3000, 6000].forEach(function (ms) { setTimeout(autoVocab, ms); });
  window.addEventListener('load', function () { setTimeout(autoVocab, 500); });
  addEventListener('scroll', (function () {   // 滚动到新内容时补扫(节流)
    var t = null;
    return function () { if (t) return; t = setTimeout(function () { t = null; autoVocab(); }, 700); };
  })(), { passive: true });

  window.__rcTr = { start: start, stop: stop, state: ST, vocab: vocabPass };
})();
