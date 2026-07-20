/* rc-wordpop.js — 统一控制层:点词字典「核心小框 → 展开完整词条」(共享,PDF/EPUB 通用)。
 * 忠实移植自 PDF 阅读器 reader.src/15-phrase-wordpop.js(单词小框 showWordPopover/_renderWordPop/
 *   _expandWordFull/_wordPopMaster/_speakCurWord/_wordPopGrammar/_renderPitch/_jpInflectHtml/_enFormsHtml)
 *   + reader.src/19-dict.js(完整框 dictStream 三源 SSE / markVocabKnown / addVocabAnki /
 *   dictStreamJP / _jpKanjiTap / _jpAiDeep / _jpPollZh)。UI、类名(#word-pop / .wp-* / .jp-* / .jk-*)、
 *   声调算法、变形渲染、SSE 解析、竞态守卫、80ms 节流、框外 pointerdown 400ms 豁免 —— 全部照搬,与 PDF 视觉一致。
 *
 * 与 PDF 的差异(reflow 适配,均为底座无关的退化,不改 reader.src/pdf_reader.html):
 *   · 定位:PDF 用 charBox(_positionWordPop);这里用 opts.rect(选区 viewport 矩形)+ position(rect)
 *     (左右钳进视口、放不下翻上、顶≥54),#word-pop = position:fixed。
 *   · 查词等待:PDF 的呼吸高亮 / furigana-on-wait 是字符层专属 → 退化成内联「⏳ 查询中…」占位。
 *   · 掌握后乐观去下划线(遍历多页)是字符层专属 → 退化成「只发 POST + 回调 onMastered」,前端刷新留空。
 *   · 慢词自动弹出(_wordPopCancelSeq)随呼吸高亮一并去除(小框开局即弹,无需滚动取消语义)。
 *
 * 完整词条「展开」调 window.RC.result.openResult(title, src, bodyHtml)(rc-result.js,PDF 20-result-draft.js
 *   的忠实移植,沿用 openResult/closeResult)。本模块假设 rc-result 维持 PDF 的 #result-content / #vocab-actions
 *   元素 id,并暴露当前结果请求序号(RC.result.reqId() 或 RC.result._resultReqId,亦回退 window._resultReqId);
 *   读不到序号时竞态守卫退化为「永不作废」(仍可用,只是少了跨动作的延迟结果丢弃)。
 *
 * 底座耦合全部走 opts(epub-html.js 在 dict 分支传):
 *   opts.word            查询词
 *   opts.rect            选区矩形(viewport,getBoundingClientRect 那种),用于定位
 *   opts.ctx             上下文整句(传给 dict-quick / dict / dict-jp-ai)
 *   opts.file            FREL,所有后端查询带 file=
 *   opts.langs           本书语言数组(决定英/日分流);EPUB 可传 [] 让模块按字符自动判英/日
 *   opts.markHighlight(word)  可选;有则核心框出「🖌 标记」按钮 → EPUB saveHl(替代 PDF charSel 坐标 PATCH)
 *   opts.onMastered(word)     可选;掌握 POST 成功后回调(EPUB 用于刷新下划线;现可空)
 *   opts.onGrammar(word)      可选;「📊 语法」占位回调,无则 toast 提示未接入
 *   opts.onFallback(word)     可选;非英/日词(纯中文等)→ 底座转译(与 rc-dict 一致),不开词典框
 *   opts.ignoreSelector       可选;CSS 选择器,框外 pointerdown 命中该选择器内部也不关框(复用 rc-dict.js
 *                             同款判断)。PDF 用于「#sel-toolbar 与 #word-pop 同屏共存」场景(点工具栏其余
 *                             按钮不误关刚弹出的小框);EPUB 不传,默认 '' → 行为与迁移前完全一致
 */
(function () {
  if (!window.RC) window.RC = {};
  if (window.RC.wordpop) return;
  var RC = window.RC;

  var esc = function (s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;'); };

  // ─────────────────────────── CSS(逐字搬自 pdf_reader.html;#word-pop 改 fixed、.wp-actions 加 flex-wrap)───────────────────────────
  var _cssInjected = false;
  function injectCss() {
    if (_cssInjected) return; _cssInjected = true;
    var css = document.createElement('style'); css.id = 'rc-wordpop-css';
    css.textContent = [
      '#word-pop .wp-speak{background:transparent;border:1px solid #3b6db5;color:#a8cdff;border-radius:50%;width:26px;height:26px;cursor:pointer;font-size:12px;padding:0;flex-shrink:0}',
      '#word-pop .wp-speak:hover{background:#244470;color:#fff}',
      // 单词小框(PDF 用 absolute-in-#main;EPUB reflow 用 fixed-in-viewport,配 opts.rect 定位)
      '#word-pop{position:fixed;display:none;background:#10162a;border:1px solid #3b6db5;border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.6);z-index:200;width:min(340px,86vw);font-size:13px;color:#cfe6ff;max-height:80vh;overflow-y:auto;overflow-x:hidden}',
      '#word-pop .wp-head{display:flex;align-items:center;gap:8px;padding:11px 14px 7px;flex-wrap:wrap}',
      '#word-pop .wp-word{font-size:17px;font-weight:600;color:#fff}',
      '#word-pop .wp-phon{color:#a8cdff;font-style:italic;font-size:12px}',
      '#word-pop .wp-pitch{display:inline-flex;align-items:flex-end;gap:0;font-size:15px;color:#cfe6ff;padding-top:4px}',
      '#word-pop .wp-pitch .pm{position:relative;padding:3px 1px 0;line-height:1.1;border-top:2px solid transparent}',
      '#word-pop .wp-pitch .pm.hi{border-top:2px solid #6fd3ff;color:#dff1ff}',
      '#word-pop .wp-pitch .pm.drop::after{content:"";position:absolute;right:-1px;top:0;height:9px;border-right:2px solid #ff8a8a}',
      '#word-pop .wp-pitch .pm-type{margin-left:6px;font-size:10px;color:#5a6680;font-style:normal;align-self:center;border:1px solid #2a3450;border-radius:4px;padding:0 4px}',
      '#word-pop .wp-ex{margin-top:8px;padding-top:7px;border-top:1px dashed #243049}',
      '#word-pop .wp-ex .wp-ex-ja{color:#dfe9ff;font-size:13px;line-height:1.5;margin-top:5px}',
      '#word-pop .wp-ex .wp-ex-zh{color:#8fb0d8;font-size:12px;line-height:1.45;margin-bottom:3px}',
      // 日语完整字典大页面(渲进 RC.result.openResult 的 #result-content)
      '#result-content .jp-head{display:flex;align-items:flex-end;gap:10px;flex-wrap:wrap;padding-bottom:4px}',
      '#result-content .jp-romaji{color:#8fb0d8;font-style:italic;font-size:13px}',
      '#result-content .jp-pos{color:#5a6680;font-size:11px;border:1px solid #2a3450;border-radius:4px;padding:0 5px;align-self:center}',
      '#result-content .jp-zh{margin-top:8px;color:#dff1ff;font-size:15px;line-height:1.6}',
      '#result-content .jp-sec-label{margin-top:14px;color:#7a8497;font-size:11px;border-top:1px solid #2a3550;padding-top:8px}',
      '#result-content .jp-kanji-row{display:flex;gap:10px;flex-wrap:wrap;margin-top:8px}',
      '#result-content .jp-kanji-chip{font-size:26px;line-height:1;padding:8px 12px;background:#162045;border:1px solid #2e3f63;border-radius:10px;color:#dff1ff;cursor:pointer;transition:all .12s}',
      '#result-content .jp-kanji-chip.active{background:#244470;border-color:#6fd3ff;box-shadow:0 0 0 1px #6fd3ff}',
      '#result-content .jp-kanji-detail{margin-top:8px;display:flex;gap:12px;align-items:flex-start;background:#10162a;border:1px solid #243049;border-radius:8px;padding:10px}',
      '#result-content .jp-kanji-detail:empty{display:none}',
      '#result-content .jk-lit{font-size:40px;line-height:1;color:#fff;flex-shrink:0}',
      '#result-content .jk-body{font-size:13px;color:#cfe6ff;line-height:1.8}',
      '#result-content .jk-tag{display:inline-block;width:18px;text-align:center;border-radius:3px;font-size:11px;margin-right:6px;color:#0b1020;font-style:normal}',
      '#result-content .jk-tag.jk-on{background:#6fd3ff}',
      '#result-content .jk-tag.jk-kun{background:#9fe0b8}',
      '#result-content .jk-mean{color:#8fb0d8;font-size:12px;margin-top:3px}',
      '#result-content .jp-ex-ja{color:#dfe9ff;font-size:14px;line-height:1.6;margin-top:7px}',
      '#result-content .jp-ex-zh{color:#8fb0d8;font-size:12.5px;line-height:1.45}',
      '#result-content .jp-ai-btn{margin-top:14px;width:100%;background:#1a2748;border:1px solid #3b6db5;color:#a8cdff;border-radius:8px;padding:9px;cursor:pointer;font-size:13px}',
      '#result-content .jp-ai-btn:disabled{opacity:.6}',
      '#result-content .jp-ai-out{margin-top:10px;color:#cfe6ff;line-height:1.7}',
      '#word-pop .wp-freq{color:#5a6680;font-size:11px;margin-left:auto}',
      '#word-pop .wp-def{padding:7px 14px 11px;line-height:1.6;color:#cfe6ff;cursor:pointer;border-top:1px solid #1f2740}',
      '#word-pop .wp-pos-tag{display:inline-block;font-size:10.5px;color:#7a8497;background:#1a2540;border:1px solid #2a3550;border-radius:4px;padding:0 5px;margin-right:7px;vertical-align:1px;font-weight:500}',
      // 日语变形分析行(原形 + 语法标签);word-pop 小框 + 完整字典(result-content)共用
      '#word-pop .jp-inflect,#result-content .jp-inflect{font-size:12px;color:#9fb4cf;padding:6px 14px 0;line-height:1.5}',
      '#result-content .jp-inflect{padding:6px 0 0}',
      '.jp-inflect b{color:#dff1ff;font-weight:600}',
      '.jp-inflect .jp-inflect-mark{display:inline-block;background:#16352a;border:1px solid #2e7d4f;color:#9fe0b8;border-radius:4px;padding:0 6px;font-size:11px;margin-left:2px}',
      '#word-pop .wp-def:hover{background:#162045}',
      '#word-pop .wp-more{color:#60a5fa;font-size:11px;margin-top:7px}',
      // flex-wrap 为 EPUB 多一两个按钮(🎴Anki/🖌标记)留行,避免横向溢出;其余照搬
      '#word-pop .wp-actions{display:flex;gap:8px;padding:9px 14px;border-top:1px solid #1f2740;background:#0d1322;flex-wrap:wrap}',
      '#word-pop .wp-actions button{flex:1;background:#1a2540;border:1px solid #2a3550;color:#cfe6ff;border-radius:6px;padding:8px;cursor:pointer;font-size:12px}',
      '#word-pop .wp-actions button:hover{border-color:#3b6db5}',
      '#word-pop .wp-anki{background:#13351f !important;border-color:#34d399 !important;color:#7ee2b8 !important}',
      // 待查词呼吸高亮(逐字对照 PDF pdf_reader.html .word-hl-layer .hl + sel-breathe):
      //   背景 rgba(56,178,172,.30) 青/teal、虚框 rgba(120,231,210,.92)、border-radius 3px、呼吸 opacity .45↔.9。
      // pointer-events:auto(非 none)——照搬 PDF 原生 _wordHlClick:呼吸高亮本身可点,未就绪点了主动开"查询中"
      // 占位框(boxOpen),已就绪点了直接弹结果。之前误设 none 导致用户点不到它,结果只能"自己冒出来"。
      // 动画拆到 .breathe 子类(照原生 renderWordHl 的 `h.ready ? '' : ' breathe'`):查词中呼吸,就绪转常亮等点击。
      '.rc-wp-breathe{position:fixed;z-index:190;pointer-events:auto;cursor:pointer;border-radius:3px;background:rgba(56,178,172,.30);' +
        'box-shadow:0 0 0 1px rgba(120,231,210,.92)}',
      '.rc-wp-breathe.breathe{animation:rcWpBreathe 1.1s ease-in-out infinite}',
      '@keyframes rcWpBreathe{0%,100%{opacity:.45}50%{opacity:.9}}'
    ].join('\n');
    document.head.appendChild(css);
  }

  // ─────────────────────────── 模块状态 ───────────────────────────
  var _wordPopState = null;        // {word, ctx, lemma, jp, reading, mastered}
  var _wordPopCancelSeq = 0;       // 照搬 PDF _wordPopCancelSeq:滚动/新查词都会 ++;慢词结果到达时"没被打断过"才自动弹框
  // 多个查词可同时进行 → 多个呼吸高亮并存(照搬原生 15-phrase-wordpop.js 的 _wordHls 数组语义,
  // "各自独立查、各自点开,不清掉别的查词高亮")。之前误做成单例(新查词清旧高亮),被用户抓包。
  var _wordHlSeq = 0;              // 高亮 id 发号
  var _wordHls = [];               // 并存的查词高亮 [{id,word,ctx,rect,breathe,el,isHost,unwrap,shown,ready,data,error,boxOpen,origin,scrollBase,scrollEl}]
  var _wordPopOwnerId = null;      // 当前 word-pop 小框归属的高亮 id(防并发查词回来填错框)
  var _dictCache = new Map();      // 本会话查词结果缓存(word→dict-quick d):已查过的词再点直接秒显小框
  // ── 生词释义预热:翻到某页时后台空闲把该页生词释义预填 _dictCache(点开秒显),prewarm=1 纯读不 bump 暴露/不建笔记。
  //    dedup(_prewarmSeen)+ 低并发(2)+ requestIdleCallback 空闲跑;切书/失效经 clearCache 释放,600 上限自然淘汰旧的。──
  var _DICT_CACHE_MAX = 600;
  var _prewarmSeen = new Set(), _prewarmQ = [], _prewarmActive = 0, _PREWARM_CONC = 2;
  function _prewarmPump() {
    while (_prewarmActive < _PREWARM_CONC && _prewarmQ.length) {
      var w = _prewarmQ.shift();
      if (!w || _dictCache.has(w)) continue;
      _prewarmActive++;
      (function (ww) {
        fetch('/pdf/api/dict-quick?word=' + encodeURIComponent(ww) + '&prewarm=1')
          .then(function (r) { return r.json(); })
          .then(function (d) { if (d && d.ok) { _dictCache.set(ww, d); if (_dictCache.size > _DICT_CACHE_MAX) _dictCache.delete(_dictCache.keys().next().value); } })
          .catch(function () {})
          .then(function () { _prewarmActive--; if (_prewarmQ.length) _schedPump(); });
      })(w);
    }
  }
  function _schedPump() {
    if (window.requestIdleCallback) requestIdleCallback(function () { _prewarmPump(); }, { timeout: 2500 });
    else setTimeout(_prewarmPump, 250);
  }
  function prewarm(words) {
    if (!Array.isArray(words) || !words.length) return;
    for (var i = 0; i < words.length; i++) {
      var w = String(words[i] == null ? '' : words[i]).trim().toLowerCase();
      if (!w || _dictCache.has(w) || _prewarmSeen.has(w)) continue;
      _prewarmSeen.add(w); _prewarmQ.push(w);
    }
    if (_prewarmQ.length) _schedPump();
  }
  function clearDictCache() { try { _dictCache.clear(); _prewarmSeen.clear(); _prewarmQ.length = 0; } catch (_) {} }
  var _jpKanjiData = [];           // 当前日语词的汉字拆解,供 chip 点击展开
  var _jpPollTimer = null;         // 例句/汉字字义中译后台轮询替换的计时器
  // 底座耦合(每次 show 刷新)
  var _ctx = { file: '', page: 0, langs: [], ctx: '', rect: null, markHighlight: null, onMastered: null, onGrammar: null, onFallback: null, ignoreSelector: '' };

  // ─────────────────────────── 结果框桥接(rc-result.js)───────────────────────────
  // 竞态守卫:理想是读 rc-result 的全局结果序号(开新框就 +1,延迟回调比对后丢弃)。当前 rc-result.js
  // 未暴露 _resultReqId,故再叠一个本模块自己的展开序号 _expandSeq 作兜底——保证「展开词 A → 紧接展开词 B」
  // 时 A 的迟到 SSE 不会覆盖 B(本模块内最常见的竞态)。一旦 rc-result 暴露 reqId()/._resultReqId,自动升级为
  // 完整的跨动作守卫(连 translate/explain 新开框也能作废本模块的迟到流)。
  var _expandSeq = 0;
  function _openResult(title, src, html) {
    _expandSeq++;   // 每开一次完整框自增(兜底序号)
    if (RC.result && typeof RC.result.openResult === 'function') return RC.result.openResult(title, src, html);
    if (typeof window.openResult === 'function') return window.openResult(title, src, html);   // 兼容直接全局
  }
  function _resReqId() {
    var r = RC.result;
    if (r) {
      if (typeof r.reqId === 'function') { try { return 'g' + r.reqId(); } catch (_) {} }   // 优先用 rc-result 的全局序号
      if (r._resultReqId != null) return 'g' + r._resultReqId;
    }
    if (typeof window._resultReqId !== 'undefined') return 'g' + window._resultReqId;
    return 'e' + _expandSeq;   // 兜底:本模块展开序号(仍能挡住本模块内 A→B 重入覆盖)
  }
  // EPUB 暂无字符层下划线;有全局刷新函数才调,否则 no-op(epub 之后接上自动生效)
  function _refreshUnderlines(lemma, mastered) {
    // §18.5:能本地就本地(PDF 提供 applyVocabLocalOverride → 0ms,治"消失又出现");未接宿主回退整页重拉
    try { if (lemma != null && typeof window.applyVocabLocalOverride === 'function') { window.applyVocabLocalOverride(lemma, mastered); return; } } catch (_) {}
    try { if (typeof window.refreshVocabUnderlinesForAllPages === 'function') window.refreshVocabUnderlinesForAllPages(); } catch (_) {} }

  // ─────────────────────────── 发音(照搬 reader.src/05-nav.js)───────────────────────────
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
           || vs.find(function (x) { return norm(x.lang).startsWith(pref); });
      if (v) u.voice = v;
      speechSynthesis.speak(u);
    } catch (e) {}
  }
  function _speakOnline(w) {
    if (!w) return;
    if (_isJaWord(w)) { _ttsWord(w, 'ja-JP'); return; }
    try {
      var a = new Audio('https://dict.youdao.com/dictvoice?type=2&audio=' + encodeURIComponent(w));   // 英语真人 mp3,免 key
      a.play().catch(function () { _ttsWord(w, 'en-US'); });
    } catch (e) { _ttsWord(w, 'en-US'); }
  }
  window._ttsWord = _ttsWord;
  window._speakOnline = _speakOnline;
  // 小框喇叭:读当前词(避开 onclick 内联传参的引号冲突)
  window._speakCurWord = function () {
    var s = _wordPopState;
    if (!s) return;
    if (s.reading) { _ttsWord(s.reading, 'ja-JP'); return; }   // 日语:直接念假名读音
    var w = s.lemma || s.word;
    if (w) _speakOnline(w);
  };

  // ─────────────────────────── 日语判定 / 声调 / 变形 / 英语词形(照搬 15-phrase-wordpop.js)───────────────────────────
  // 含假名→是;含汉字时按本书语言声明(声明含 ja 才算,未声明默认按日语,跟后端 dict-quick want_ja 一致)。
  function _isJaWord(w) {
    if (/[぀-ヿ]/.test(w)) return true;
    if (!/[㐀-鿿]/.test(w)) return false;
    var declared = (_ctx.langs || []).length > 0;
    return declared ? _ctx.langs.indexOf('ja') >= 0 : true;
  }
  // 画日语声调(ピッチアクセント):读音拆拍 → 高/低 + 下降标记。
  function _renderPitch(reading, accent) {
    var small = 'ぁぃぅぇぉゃゅょゎァィゥェォャュョヮ';
    var mora = [];   // 拆拍:小书写假名并入前一拍
    for (var ci = 0; ci < reading.length; ci++) {
      var ch = reading[ci];
      if (small.indexOf(ch) >= 0 && mora.length) mora[mora.length - 1] += ch;
      else mora.push(ch);
    }
    var n = mora.length;
    if (!n) return '';
    var hi = function (i) {                       // i: 0-based 拍
      if (accent === 0) return i >= 1;            // 平板:第1拍低,其余高
      if (accent === 1) return i === 0;           // 頭高:第1拍高,其余低
      return i >= 1 && i < accent;                // 中高/尾高:2..accent 高
    };
    var html = '<span class="wp-pitch" title="声调型 ' +
      (accent === 0 ? '平板' : accent === 1 ? '頭高' : '第' + accent + '拍后降') + '">';
    for (var i = 0; i < n; i++) {
      var h = hi(i);
      var drop = (accent >= 1 && i + 1 === accent);   // 此拍后下降
      html += '<span class="pm' + (h ? ' hi' : '') + (drop ? ' drop' : '') + '">' + mora[i] + '</span>';
    }
    var tlabel = accent === 0 ? '平板' : accent === 1 ? '頭高' : '[' + accent + ']';
    html += '<span class="pm-type">' + tlabel + '</span></span>';
    return html;
  }
  // 日语变形分析 → HTML 行(原形 + 中文语法标签)。word-pop 和完整字典共用。
  function _jpInflectHtml(inf, word) {
    if (!inf) return '';
    var showBase = inf.base && inf.base !== (word || '');
    var b = showBase ? '原形 <b>' + esc(inf.base) + '</b>' : '';
    var m = (inf.marks || []).length ? '<span class="jp-inflect-mark">' + inf.marks.map(esc).join('・') + '</span>' : '';
    if (!b && !m) return '';
    return '<div class="jp-inflect">🔀 ' + [b, m].filter(Boolean).join('　') + '</div>';
  }
  // 英语原型 + 变形 → HTML 行(跟日语变形行同款样式)。
  function _enFormsHtml(lemma, forms, clicked) {
    lemma = (lemma || '').toLowerCase();
    var c = (clicked || '').toLowerCase();
    var fs = Array.from(new Set((forms || []).map(function (f) { return String(f || '').toLowerCase(); }).filter(Boolean)))
      .filter(function (f) { return f !== lemma; }).slice(0, 8);
    var b = (lemma && c && c !== lemma) ? '原型 <b>' + esc(lemma) + '</b>' : '';
    var m = fs.length ? '变形 <span class="jp-inflect-mark">' + fs.map(esc).join('・') + '</span>' : '';
    if (!b && !m) return '';
    return '<div class="jp-inflect">🔀 ' + [b, m].filter(Boolean).join('　') + '</div>';
  }

  // ─────────────────────────── 定位 ───────────────────────────
  // 定位也走"机制退还给旧代码"(设计铁律):host 传了 positionPop hook(PDF=直调原生 _positionWordPop,
  // absolute-in-#main 随内容滚动)就优先用;否则用下面 fixed 视口定位(EPUB 的既有行为:左右钳进视口、
  // 放不下翻上、顶≥54)。hook 随"框归属"(_wordPopOwnerId)切换——多实例并存时各查词的 hook 闭包
  // 捕获各自的定位锚(PDF=charSel 快照),弹谁的框就用谁的 hook。
  var _popPosHook = null;
  function _positionPop(pop, rect) {
    if (!pop) return;
    if (_popPosHook) { try { _popPosHook(pop, rect); return; } catch (_) {} }
    if (!rect) {   // 没锚点矩形 → 视口上方居中兜底
      pop.style.left = Math.max(8, (window.innerWidth / 2 - 170)) + 'px';
      pop.style.top = '70px';
      return;
    }
    var w = pop.offsetWidth || 320, h = pop.offsetHeight || 120;
    pop.style.left = Math.min(Math.max(8, rect.left), window.innerWidth - w - 8) + 'px';
    var top = rect.bottom + 8;
    if (top + h > window.innerHeight - 8) top = Math.max(54, rect.top - h - 8);
    pop.style.top = top + 'px';
  }
  function _ensurePop() {
    var p = document.getElementById('word-pop');
    if (!p) { p = document.createElement('div'); p.id = 'word-pop'; p.setAttribute('onclick', 'event.stopPropagation()'); document.body.appendChild(p); }
    return p;
  }

  // ── 待查词呼吸高亮(对照 PDF 字符层 .word-hl-layer.breathe):查询中盖在词上呼吸当等待指示,结果回来即移除。
  //    rect 为父视口坐标(epub2.js / rc-dict 传入)。延迟 400ms 才出现(对齐 19-dict.js 慢词物化阈值)→ 快词/缓存词不闪。
  // 多实例状态机,逐分支照搬原生 15-phrase-wordpop.js(_wordHls/renderWordHl/_removeWordHl/_materializeWordHl/
  // _wordHlClick + showWordPopover 的慢词三分支):每次查词一个独立 hl,**并存、各自呼吸、各自点开、互不清除**;
  // 查询中 .breathe 呼吸,就绪转常亮(去 .breathe)等点击;boxOpen(用户点高亮开了"查词中"框)后高亮视觉隐去。
  // 视觉呈现两条路:①host(EPUB epub-html.js)经 opts.breathe.wrap() 用真实 <mark> 包文字节点(同生词下划线/
  // 词组呼吸高亮技术,随文档流滚动天然零漂移——用户用"下划线怎么没漂移问题"点破了 position:fixed 方案的
  // 结构性缺陷);②host 没提供 hook 时退回 position:fixed + 滚动平移兜底(PDF 共享模式不走这条:原生
  // _materializeWordHl 锚在页面坐标系,天然跟滚)。
  function _hlRemove(hl) {   // 原生 _removeWordHl:移除单个高亮(点开/出错/查完后),不动别的
    _wordHls = _wordHls.filter(function (o) { return o !== hl; });
    _hlRemoveVisual(hl);
  }
  function _hlRemoveVisual(hl) {   // 只移除视觉元素,保留 hl 记录(boxOpen 用:原生 renderWordHl 过滤 boxOpen 不画)
    if (!hl.el) return;
    try { hl.isHost ? (hl.unwrap && hl.unwrap(hl.el)) : hl.el.remove(); } catch (_) {}
    hl.el = null;
  }
  function _hlRect(hl) {   // 弹框定位:元素还在就现取(滚动后依然准),不然用捕获时的 rect
    if (hl.el) { try { var r = hl.el.getBoundingClientRect(); if (r.width || r.height) return r; } catch (_) {} }
    return hl.rect;
  }
  function _removeAllWordHls() { _wordHls.slice().forEach(_hlRemove); }   // 原生 _removeWordHighlight(换书/大跳转备用)
  // 物化呼吸高亮(原生 _materializeWordHl)。同词去重防重复点同词叠两层(原生按 页+字符起点,EPUB 无字符层用 word 近似)。
  function _materializeWordHl(hl) {
    _wordHls.filter(function (o) { return o !== hl && o.word === hl.word; }).forEach(_hlRemove);
    var el = null;
    if (hl.breathe && hl.breathe.wrap) {
      try { el = hl.breathe.wrap(); } catch (_) {}
      if (el) { hl.isHost = true; hl.unwrap = hl.breathe.unwrap || null; }
    }
    if (!el && hl.rect) {
      var w = (hl.rect.right - hl.rect.left) || hl.rect.width || 0, h = (hl.rect.bottom - hl.rect.top) || hl.rect.height || 0;
      if (w >= 1 || h >= 1) {
        try {
          el = document.createElement('div'); el.className = 'rc-wp-breathe';
          el.style.left = hl.rect.left + 'px'; el.style.top = hl.rect.top + 'px';
          el.style.width = Math.max(6, w) + 'px'; el.style.height = Math.max(6, h) + 'px';
          document.body.appendChild(el);
          hl.isHost = false;
          hl.origin = { left: hl.rect.left, top: hl.rect.top };
          hl.scrollEl = document.getElementById('ep-content') || document.getElementById('main');
          hl.scrollBase = { top: hl.scrollEl ? hl.scrollEl.scrollTop : (window.pageYOffset || 0), left: hl.scrollEl ? hl.scrollEl.scrollLeft : (window.pageXOffset || 0) };   // 创建同一刻同步记基准,不留窗口期
        } catch (_) { el = null; }
      }
    }
    if (!el) return;
    el.classList.add('breathe');   // 查询中呼吸;就绪转常亮(原生 renderWordHl 的 h.ready ? '' : ' breathe')
    el.addEventListener('pointerdown', function (e) { e.stopPropagation(); _wordHlClick(hl); });
    hl.el = el; hl.shown = true;
    _wordHls.push(hl);
  }
  // 点呼吸高亮(原生 _wordHlClick):就绪→直接弹小框并移除该高亮;未就绪→用户主动开"查词中"小框(等 fetch 回来自动填)
  function _wordHlClick(hl) {
    if (!hl) return;
    if (hl.ready) {
      if (hl.error) { RC.toast('查词失败'); _hlRemove(hl); return; }
      _wordPopOwnerId = hl.id;
      _popPosHook = hl.posHook || null;
      var r = _hlRect(hl);
      _hlRemove(hl);
      _renderWordPop(hl.word, hl.ctx, hl.data, r);
    } else {
      hl.boxOpen = true; _wordPopOwnerId = hl.id;
      _popPosHook = hl.posHook || null;
      _wordPopState = { word: hl.word, ctx: hl.ctx, lemma: hl.word };
      var pop = _ensurePop();
      var r2 = _hlRect(hl);
      pop.style.display = 'block'; window._wordPopOpenAt = Date.now();
      pop.innerHTML = '<div style="padding:14px;color:#8a9bb4">⏳ 查词中…</div>';
      _positionPop(pop, r2);
      _hlRemoveVisual(hl);   // 原生:boxOpen=true → renderWordHl 过滤掉它,不再画呼吸高亮(hl 记录留着等结果)
    }
  }
  // 滚动/滚轮 → _wordPopCancelSeq++(原生语义:只取消"结果到了自动弹框",从不清除高亮本身)。
  // 顺带平移所有 position:fixed 兜底高亮(host 真实 DOM 包裹的不需要,自己随文档流走)。
  document.addEventListener('scroll', function () {
    _wordPopCancelSeq++;
    for (var i = 0; i < _wordHls.length; i++) {
      var h = _wordHls[i];
      if (h.isHost || !h.el || !h.origin || !h.scrollBase) continue;
      var el = h.scrollEl;
      var sTop = (el && el.scrollTop != null) ? el.scrollTop : (window.pageYOffset || 0);
      var sLeft = (el && el.scrollLeft != null) ? el.scrollLeft : (window.pageXOffset || 0);
      h.el.style.top = (h.origin.top - (sTop - h.scrollBase.top)) + 'px';
      h.el.style.left = (h.origin.left - (sLeft - h.scrollBase.left)) + 'px';
    }
  }, true);
  document.addEventListener('wheel', function () { _wordPopCancelSeq++; }, { passive: true, capture: true });   // 照搬原生 wheel 监听(滚动到边界时 wheel 触发但 scroll 不一定触发)
  // 框外点击 → 自动关小框(pointerdown capture:原生指针不合成,避免 iOS 合成 mousedown 误关;刚弹 400ms 内豁免)
  // 只关框、**不清呼吸高亮**(照原生 15-phrase-wordpop.js:326-327:"点别处只藏面板,保留高亮;
  // 高亮只在重新选词/点空白时自然替换或清除;查词加载完会转常亮,独立于面板可见性")。
  // ignoreSelector(可选,底座经 opts.ignoreSelector 传入 → 落进 _ctx):点该选择器命中的区域也不关框。
  document.addEventListener('pointerdown', function (e) {
    var p = document.getElementById('word-pop');
    if (!p || p.style.display !== 'block') return;
    if (Date.now() - (window._wordPopOpenAt || 0) < 400) return;
    if (p.contains(e.target)) return;
    if (_ctx.ignoreSelector && e.target && e.target.closest && e.target.closest(_ctx.ignoreSelector)) return;
    p.style.display = 'none';
  }, true);

  // ─────────────────────────── 核心小框 ───────────────────────────
  function _lookupFetch(word) {
    var url = '/pdf/api/dict-quick?word=' + encodeURIComponent(word) +
      '&file=' + encodeURIComponent(_ctx.file || '') +
      '&page=' + (_ctx.page || 0) +
      '&context=' + encodeURIComponent(_ctx.ctx || '') +
      '&langs=' + encodeURIComponent((_ctx.langs || []).join(','));
    return fetch(url).then(function (r) { return r.json(); });
  }

  // 渲染单词小框(已拿到 dict-quick 结果 d)。rect=查词时捕获的选区矩形,用于定位。
  function _renderWordPop(word, ctx, d, rect) {
    var pop = _ensurePop();
    _wordPopState = { word: word, ctx: ctx || '', lemma: word };
    if (!d || !d.ok) { pop.style.display = 'none'; window._expandWordFull(word, ctx); return; }   // ecdict 没有 → 直接完整
    try { _dictCache.set(word, d); if (_dictCache.size > 600) _dictCache.delete(_dictCache.keys().next().value); } catch (_) {}
    _wordPopState.lemma = d.lemma || word;
    _wordPopState.jp = !!d.jp;                                      // 掌握按钮按语言分流(jp/en 不同 store)
    _wordPopState.reading = (d.jp && d.reading) ? d.reading : '';   // 日语:发音念假名读音(保证读对)
    _wordPopState.mastered = (function () {
      // §18.7 本地掌握库优先:SW/跨站缓存里的 dict 响应 mastered 可能陈旧,本地库才是事实源
      try {
        var _k = String(d.lemma || d.word || '').toLowerCase();
        if (window.__vocabOverride && window.__vocabOverride.has(_k)) return window.__vocabOverride.get(_k);
        if (window.__masteredLocal) return window.__masteredLocal.has(_k);
      } catch (_) {}
      return !!d.mastered;
    })();
    var defLines = (d.translation || d.definition || '(无释义)').split('\n').filter(Boolean).slice(0, 3).map(esc).join('<br>');
    var posTag = (d.pos ? '<span class="wp-pos-tag">' + esc(d.pos) + '</span>' : '');
    var inflectHtml = d.jp ? _jpInflectHtml(d.inflect, word) : _enFormsHtml(d.lemma || word, d.forms, word);
    var phonHtml = (d.jp && d.reading && d.accent != null)
      ? _renderPitch(d.reading, d.accent)
      : (d.phonetic ? '<span class="wp-phon">' + esc(d.phonetic) + '</span>' : '');
    var exHtml = '';   // 日语母语例句(Tanaka):直接展示;zh 未翻译则回退英文
    if (d.jp && Array.isArray(d.examples) && d.examples.length) {
      exHtml = '<div class="wp-ex">' + d.examples.slice(0, 2).map(function (e) {
        return '<div class="wp-ex-ja">' + esc(e.ja) + '</div>' +
               '<div class="wp-ex-zh">' + esc(e.zh || e.en || '') + '</div>';
      }).join('') + '</div>';
    }
    pop.style.display = 'block';
    window._wordPopOpenAt = Date.now();   // 框外关闭监听据此忽略刚弹出时的余波事件
    pop.innerHTML =
      '<div class="wp-head"><span class="wp-word">' + esc(d.lemma || word) + '</span>' +
      phonHtml +
      '<button class="wp-speak" onclick="_speakCurWord()" title="发音">🔊</button>' +
      (d.freq_bnc ? '<span class="wp-freq">BNC#' + d.freq_bnc + '</span>' : '') + '</div>' +
      inflectHtml +
      '<div class="wp-def" onclick="_expandWordFull()" title="点开看完整释义/例句">' + posTag + defLines +
      exHtml +
      '<div class="wp-more">点这里展开完整字典 ▾</div></div>' +
      '<div class="wp-actions">' +
      // 掌握 toggle:日英统一同一个按钮(onclick 内部按语言分流 store);✓掌握=下划线消失
      '<button id="wp-master-btn" class="' + (d.mastered ? 'wp-anki' : '') + '" onclick="_wordPopMaster(this)" title="' + (d.mastered ? '点击取消掌握（恢复生词下划线）' : '标记掌握 100（下划线消失）') + '">' + (d.mastered ? '✓ 已掌握 100' : '☆ 标记掌握') + '</button>' +
      (_ctx.showAnki ? '<button onclick="_wordPopAnki()" title="把该词加入 Anki">🎴 Anki</button>' : '') +
      (_ctx.markHighlight ? '<button onclick="_wordPopMark()" title="把该词标为高亮">🖌 标记</button>' : '') +
      '<button onclick="_wordPopGrammar()" title="对该词所在整句做语法分析（分词/结构/跟踪知识点）">📊 语法</button>' +
      '</div>';
    _positionPop(pop, rect);
    _refreshUnderlines();   // 查过即记入生词库 → 刷新下划线(EPUB 暂 no-op)
  }

  // ─────────────────────────── 小框动作按钮 ───────────────────────────
  // 展开完整词条:日语走 dictStreamJP(离线富内容 + 按需 AI),英语走 dictStream(三源 SSE)。
  window._expandWordFull = function (w, c) {
    var s = _wordPopState;
    var word = w || (s && s.word);
    var ctx = (c != null ? c : (s && s.ctx)) || '';
    var pop = document.getElementById('word-pop'); if (pop) pop.style.display = 'none';
    if (!word) return;
    if (_isJaWord(word)) dictStreamJP(word, ctx);
    else dictStream(word, ctx);
  };
  function _paintMasterBtn(btn, on) {
    if (!btn) return;
    btn.textContent = on ? '✓ 已掌握 100' : '☆ 标记掌握';
    btn.title = on ? '点击取消掌握（恢复生词下划线）' : '标记掌握 100（下划线消失）';
    btn.classList.toggle('wp-anki', on);
  }
  // 「掌握」toggle(日英统一):未掌握 ↔ 掌握 100 来回切,不关框。掌握→该词不再标生词下划线。
  // 乐观 UI(大厂标配):点击瞬间就翻按钮态 + 去下划线,不等服务端往返;失败再回滚。
  //   之前共享版把按钮翻转/去下划线全塞进 POST 的 .then → PDF 下点了要干等服务端写库+重算,体感很慢(回归)。
  window._wordPopMaster = function (btn) {
    var s = _wordPopState; if (!s) return;
    if (btn && btn.__busy) return;   // 同一按钮请求未回前不叠加(状态已乐观翻转,再点会乱)
    var next = !s.mastered;
    var prev = s.mastered;
    var w = s.lemma || s.word;
    var url = s.jp ? '/pdf/api/jp-vocab-mark' : '/pdf/api/vocab-mark';
    var mark = next ? 'known' : 'unknown';   // 日英统一口径
    // ① 立刻乐观:关框(点击即消失,不等服务端)+ 翻按钮 + 同步缓存 + 去下划线(PDF 字符层 / EPUB deco)
    try { var _pp = document.getElementById('word-pop'); if (_pp) _pp.style.display = 'none'; } catch (_) {}
    s.mastered = next;
    try { var c = _dictCache.get(s.word); if (c) c.mastered = next; } catch (_) {}
    _paintMasterBtn(btn, next);
    var _restoreUnd = null;
    if (next) {
      try { if (window.__pdfDropVocabUnderline) _restoreUnd = window.__pdfDropVocabUnderline(s); } catch (_) {}          // PDF 字符层
      try { if (!_restoreUnd && window.__epubDeco && __epubDeco.optimisticMaster) _restoreUnd = __epubDeco.optimisticMaster(w); } catch (_) {}   // EPUB
    }
    // ② 后台落库,回来只做权威校正 / 失败回滚
    if (btn) btn.__busy = true;
    RC.reqJson('POST', url, { word: w, mark: mark }).then(function (d) {
      if (btn) btn.__busy = false;
      if (d && d.ok === false) throw new Error(d.error || 'fail');
      _refreshUnderlines(w, next);   // §18.5:本地覆盖 0ms 应用(服务端已确认,无需重拉)
      try { if (_ctx.onMastered) _ctx.onMastered(w); } catch (_) {}
      RC.toast(next ? '已掌握 100，下划线消失' : '已设为未掌握');
    }).catch(function (err) {
      if (btn) btn.__busy = false;
      // 网络不通(fetch TypeError)且有 outbox → local-first:保持乐观态,入队恢复后自动补投
      if (RC.outbox && err && err.name === 'TypeError') {
        RC.outbox.send('vocab', url + '|' + w, url, { word: w, mark: mark });
        _refreshUnderlines(w, next);   // 离线也本地生效
        RC.toast(next ? '已掌握(离线,恢复后自动同步)' : '已取消(离线,恢复后自动同步)');
        return;
      }
      s.mastered = prev;
      try { var c2 = _dictCache.get(s.word); if (c2) c2.mastered = prev; } catch (_) {}
      _paintMasterBtn(btn, prev);
      if (_restoreUnd) { try { _restoreUnd(); } catch (_) {} }
      RC.toast('标记失败');
    });
  };
  // 核心框「🎴 Anki」:读 _wordPopState 避开 onclick 引号转义,直接复用完整框的 addVocabAnki。
  window._wordPopAnki = function () { var s = _wordPopState; if (!s) return; window.addVocabAnki(s.lemma || s.word); };
  // 核心框「🖌 标记」(仅 opts.markHighlight 提供时出现)→ 底座 saveHl。
  window._wordPopMark = function () {
    var s = _wordPopState; if (!s) return;
    var p = document.getElementById('word-pop'); if (p) p.style.display = 'none';
    try { if (_ctx.markHighlight) _ctx.markHighlight(s.word); } catch (_) {}
  };
  // 「📊 语法」占位:有底座回调则调,否则提示未接入。
  window._wordPopGrammar = function () {
    var p = document.getElementById('word-pop'); if (p) p.style.display = 'none';
    if (_ctx.onGrammar) { try { _ctx.onGrammar(_wordPopState && _wordPopState.word); return; } catch (_) {} }
    RC.toast('语法分析暂未接入');
  };

  // ─────────────────────────── 入口:RC.wordpop.show(opts)───────────────────────────
  function show(opts) {
    opts = opts || {};
    injectCss();
    // 注意:这里**不清**已有的呼吸高亮——原生语义是多个查词并存,"不清掉别的查词高亮"(15-phrase-wordpop.js:558)。
    var word = String(opts.word == null ? '' : opts.word).trim().toLowerCase();
    if (!word) return;
    // 同词去重:重查同一词先清掉它上一次残留的呼吸/常亮高亮(不动别词,保留多查词并存语义)。
    //   否则缓存秒弹路径不建也不清高亮 → 旧高亮(.rc-wp-breathe z:190)一直残留,盖住词组高亮(z:6)截获点击。
    try { _wordHls.filter(function (o) { return o.word === word; }).forEach(_hlRemove); } catch (_) {}
    _ctx = {
      file: opts.file || '', page: opts.page || 0, langs: opts.langs || [], ctx: opts.ctx || '',
      rect: opts.rect || null, markHighlight: opts.markHighlight || null,
      onMastered: opts.onMastered || null, onGrammar: opts.onGrammar || null, onFallback: opts.onFallback || null,
      ignoreSelector: opts.ignoreSelector || '',
      showAnki: opts.showAnki !== false,   // 默认 true(EPUB 现状不变);PDF 传 false 恢复原生「掌握+语法」两按钮(PDF 原生小框从没有过🎴Anki按钮,已有选中工具栏🎴制卡)
      breathe: opts.breathe || null,   // {wrap(), unwrap(el)} 可选;host 提供真实 DOM 包裹实现呼吸高亮(零滚动漂移),不提供则退回 position:fixed 兜底
      positionPop: opts.positionPop || null,   // (pop, rect) 可选;host 提供框定位机制(PDF=原生 _positionWordPop,随内容滚动),不提供用 fixed 视口定位
      noBreathe: !!opts.noBreathe   // 已知词(有生词下划线=以前查过、服务器有缓存)→ 不呼吸,直接弹占位框秒填结果
    };
    // 非英日(纯中文等)→ 交给底座转译(与 rc-dict 一致),不开词典框。isJa 用 _isJaWord(尊重 langs:
    //   未声明 langs 时汉字默认按日语,跟 PDF dict-quick want_ja 一致;声明 langs 不含 ja 的汉字 → 转译)。
    var isJa = _isJaWord(word);
    var isEn = /^[A-Za-z][A-Za-z'’\-]*$/.test(word);
    if (!isJa && !isEn) { if (_ctx.onFallback) { try { _ctx.onFallback(word); } catch (_) {} } return; }
    var _cseq = ++_wordPopCancelSeq;   // 照搬原生:本次点词占位;同时取消上一个还没回来的词的**自动弹出**(不清它的高亮)
    var pop = _ensurePop();
    // 已有现成数据(本会话查过)→ 直接秒显小框,不发请求;后台再打一次刷新暴露计数 + 缓存。
    // owner 占新 id(照原生:没有 hl 会匹配它 → 别的并发慢词回来不会覆盖本框)。
    var cached = _dictCache.get(word);
    if (cached) {
      _wordPopOwnerId = ++_wordHlSeq;
      _popPosHook = _ctx.positionPop;
      _renderWordPop(word, _ctx.ctx, cached, _ctx.rect);
      _lookupFetch(word).then(function (d) { if (d && d.ok) _dictCache.set(word, d); }).catch(function () {});
      return;
    }
    // 已知词(有生词下划线,本 session 没查过 → _dictCache 空,但服务器有缓存查询很快):不呼吸,
    //   立刻弹占位框(display:block ⏳)+ fetch 秒填结果,而不是"呼吸高亮等 400ms"。owner 守卫防被别的词覆盖。
    if (_ctx.noBreathe) {
      var _oid = _wordPopOwnerId = ++_wordHlSeq;
      _popPosHook = _ctx.positionPop;
      pop.style.display = 'block'; window._wordPopOpenAt = Date.now();
      pop.innerHTML = '<div style="padding:14px;color:#8a9bb4">⏳ 查询中…</div>';
      _positionPop(pop, _ctx.rect);
      _lookupFetch(word).then(function (d) {
        if (d && d.ok) _dictCache.set(word, d);
        if (_wordPopOwnerId !== _oid) return;   // 期间点了别的词 → 不覆盖
        if (d && d.ok) { _popPosHook = _ctx.positionPop; _renderWordPop(word, _ctx.ctx, d, _ctx.rect); }
        else if (_ctx.onFallback) { try { pop.style.display = 'none'; _ctx.onFallback(word); } catch (_) {} }
        else pop.innerHTML = '<div style="padding:14px;color:#8a9bb4">未查到</div>';
      }).catch(function () { if (_wordPopOwnerId === _oid) pop.innerHTML = '<div style="padding:14px;color:#c88">查询失败</div>'; });
      return;
    }
    // 慢词生命周期,逐分支照搬原生 showWordPopover:**每次调用独立跑完,多个并存,互不作废**
    // (之前用"新一轮 seq 作废旧一轮"是错的:旧词的高亮会永远残留、结果回来也没人处理)。
    // 快词(400ms 内回)全程不显示任何东西,数据一到直接弹真结果;慢词 400ms 后物化呼吸高亮
    // (有 breathe hook 或 rect;都没有才退化成"⏳ 查询中…"占位框)。结果到达三分支:
    // ①没物化(快)→ 无条件弹;②boxOpen(用户点过高亮开了"查词中"框)→ 框还归属自己才填,高亮移除;
    // ③物化了没点 → 转常亮;没出错且期间没滚动/没点别的词(_cseq)才自动弹,否则高亮留着等用户点。
    var hl = {
      id: ++_wordHlSeq, word: word, ctx: _ctx.ctx, rect: _ctx.rect,
      breathe: _ctx.breathe || null, posHook: _ctx.positionPop || null, el: null, isHost: false, unwrap: null,
      shown: false, ready: false, data: null, error: null, boxOpen: false,
      origin: null, scrollBase: null, scrollEl: null
    };
    var _placeholderShown = false;
    var hlTimer = setTimeout(function () {
      hlTimer = null;
      if (hl.ready) return;   // 原生:`if (!hl.ready && cap) _materializeWordHl(hl)`
      _materializeWordHl(hl);
      if (!hl.shown) {   // 无 hook 也无有效 rect(极端兜底)→ 占位框
        _placeholderShown = true;
        _popPosHook = hl.posHook;
        pop.style.display = 'block'; window._wordPopOpenAt = Date.now();
        pop.innerHTML = '<div style="padding:14px;color:#8a9bb4">⏳ 查询中…</div>';
        _positionPop(pop, hl.rect);
      }
    }, 400);
    _lookupFetch(word).then(function (d) {
      if (hlTimer) { clearTimeout(hlTimer); hlTimer = null; }
      hl.ready = true; hl.data = d;
      if (!hl.shown) {
        // 快路径(或占位框):直接弹,框归属本 hl
        _wordPopOwnerId = hl.id;
        _popPosHook = hl.posHook;
        _renderWordPop(word, hl.ctx, d, hl.rect);
      } else if (hl.boxOpen) {
        // 用户已点高亮开了"查词中"框 → 框仍归属本 hl 才填(否则已被别的查词接管);高亮记录移除
        if (_wordPopOwnerId === hl.id) _renderWordPop(word, hl.ctx, d, _hlRect(hl));
        _hlRemove(hl);
      } else if (_wordPopCancelSeq === _cseq) {
        // 结果到了且期间没滚动、没点别的词 → 自动弹出,不用再点高亮
        _wordPopOwnerId = hl.id;
        _popPosHook = hl.posHook;
        var r = _hlRect(hl);
        _hlRemove(hl);
        _renderWordPop(word, hl.ctx, d, r);
      } else {
        // 被打断 → 高亮转常亮留在原地(原生 renderWordHl 重画时按 ready 去 breathe),等用户点
        if (hl.el) hl.el.classList.remove('breathe');
      }
    }).catch(function (e) {
      if (hlTimer) { clearTimeout(hlTimer); hlTimer = null; }
      hl.ready = true; hl.error = e;
      if (!hl.shown) {
        RC.toast('查词失败：' + (e && e.message || ''));
        if (_placeholderShown) pop.style.display = 'none';
        return;
      }
      if (hl.boxOpen) {
        if (_wordPopOwnerId === hl.id) pop.innerHTML = '<div style="padding:14px;color:#c00">查词失败：' + (e && e.message || '') + '</div>';
        _hlRemove(hl);
        return;
      }
      // 高亮转常亮留着,点了才 toast(原生 _wordHlClick 的 ready+error 分支)
      if (hl.el) hl.el.classList.remove('breathe');
    });
  }

  // ─────────────────────────── 英文完整框:三源 SSE(照搬 19-dict.js::dictStream)───────────────────────────
  function dictStream(word, ctx) {
    var params = new URLSearchParams({
      word: word, file: _ctx.file || '', page: String(_ctx.page || 0), context: ctx || ''
    });
    _openResult('📖 ' + word, word, '<div class="loading">⏳ 查词中…</div>');   // 立刻占位,避免空等
    var myReq = _resReqId();    // 本次查词的请求序号;被新结果框作废后,后到的 SSE 渲染一律丢弃
    // 无论 SSE / JSON / 失败:1.8s + 3.5s 后刷新下划线(vocab note 写盘耗时;EPUB 暂 no-op)
    setTimeout(function () { _refreshUnderlines(); }, 1800);
    setTimeout(function () { _refreshUnderlines(); }, 3500);
    var contentEl = document.getElementById('result-content');
    var state = {
      word: word, lemma: word, forms: [],
      phon_us: '', phon_uk: '', audio_us: '', audio_uk: '',
      freq_bnc: 0, translation: '', definition: '',
      fd_defs: [], mw_defs: [], examples: new Set(), examples_zh: {},
      synonyms: [], antonyms: [],
      sources_hit: [], vocab_note: ''
    };
    var renderState = function () {
      var s = state;
      var html = '';
      var head = [];
      if (s.phon_us) head.push('<span style="font-style:italic">US ' + esc(s.phon_us) + '</span>');
      if (s.phon_uk) head.push('<span style="font-style:italic">UK ' + esc(s.phon_uk) + '</span>');
      if (s.freq_bnc) head.push('<span style="color:#5a6680;font-size:11px">BNC #' + s.freq_bnc + '</span>');
      if (s.audio_us) head.push('<button onclick="new Audio(\'' + esc(s.audio_us) + '\').play()" style="background:transparent;border:1px solid #3b6db5;color:#a8cdff;border-radius:50%;width:24px;height:24px;cursor:pointer;font-size:11px;padding:0">🔊</button>');
      html += '<div style="display:flex;gap:8px;align-items:center;color:#a8cdff;font-size:13px">' + head.join(' · ') + '</div>';
      if (s.lemma && s.lemma !== word) {
        html += '<div style="margin-top:4px;color:#7a8497;font-size:11px">原型：<code>' + esc(s.lemma) + '</code>' + (s.forms && s.forms.length ? '（' + s.forms.map(esc).join('/') + '）' : '') + '</div>';
      }
      if (s.translation) html += '<div style="margin-top:10px;color:#cfe6ff;white-space:pre-wrap;line-height:1.6">' + esc(s.translation) + '</div>';
      // MW + Free Dict 例句(合并)
      var allDefs = [];
      if (s.mw_defs.length) allDefs.push({ label: '📚 MW', defs: s.mw_defs });
      if (s.fd_defs.length) allDefs.push({ label: '🌐 Wiktionary', defs: s.fd_defs });
      for (var gi = 0; gi < allDefs.length; gi++) {
        var grp = allDefs[gi];
        html += '<div style="margin-top:12px;padding-top:8px;border-top:1px solid #2a3550;color:#8a9bb4;font-size:12px"><b style="color:#7a8497">' + esc(grp.label) + '</b>';
        html += '<ul style="margin:6px 0 0 18px;padding:0;line-height:1.6">';
        var defs = grp.defs.slice(0, 6);
        for (var di = 0; di < defs.length; di++) {
          var d = defs[di];
          html += '<li>' + (d.pos ? '<b>' + esc(d.pos) + '</b> ' : '') + esc(d.en);
          var exs = (d.examples || []).slice(0, 2);
          for (var ei = 0; ei < exs.length; ei++) {
            var ex = exs[ei];
            var zh = state.examples_zh[ex];
            html += '<br><span style="color:#7a8497;font-size:11px">▸ ' + esc(ex) + (zh ? '<br>　🇨🇳 ' + esc(zh) : '') + '</span>';
          }
          html += '</li>';
        }
        html += '</ul></div>';
      }
      // 同义反义
      if (s.synonyms.length || s.antonyms.length) {
        var meta = [];
        if (s.synonyms.length) meta.push('同 ' + s.synonyms.slice(0, 5).map(esc).join(', '));
        if (s.antonyms.length) meta.push('反 ' + s.antonyms.slice(0, 5).map(esc).join(', '));
        html += '<div style="margin-top:8px;color:#7a8497;font-size:11px">' + meta.join(' · ') + '</div>';
      }
      contentEl.innerHTML = html;
      // 底部 actions:搬到 #vocab-actions(脱离内容滚动区,始终可见)
      var va = document.getElementById('vocab-actions');
      if (va) {
        va.className = 'show';
        va.innerHTML =
          '<button onclick="addVocabAnki(\'' + esc(s.lemma || word) + '\')" style="background:#244470;border:1px solid #3b6db5;color:#fff;border-radius:6px;padding:6px 14px;cursor:pointer;font-size:12px">🎴 加入 Anki</button>' +
          '<button onclick="markVocabKnown(\'' + esc(s.lemma || word) + '\', this)" style="background:#1d3a28;border:1px solid #2e7d4f;color:#9fe0b8;border-radius:6px;padding:6px 14px;cursor:pointer;font-size:12px" title="掌握度直接设为 100%，此后不再算作生词">✓ 已掌握</button>' +
          (s.sources_hit.length
            ? '<span style="color:#5a6680;font-size:10px;margin-left:auto">源：' + s.sources_hit.join(' + ') + (s.vocab_note ? ' · <a href="obsidian://open?vault=obsidian&file=' + encodeURIComponent(s.vocab_note) + '" style="color:#60a5fa">在 Obsidian 打开词条 →</a>' : '') + '</span>'
            : '<span style="color:#5a6680;font-size:10px;margin-left:auto">⏳ 加载更多源…</span>');
      }
    };

    return (async function () {
      var r;
      try {
        r = await fetch('/pdf/api/dict?' + params.toString(), { headers: { 'Accept': 'text/event-stream' } });
      } catch (e) { return false; }
      if (!r.ok) return false;
      var ct = r.headers.get('content-type') || '';
      if (!ct.includes('event-stream')) {
        // 后端非 SSE:fall back 一次性渲染(兼容旧路径)
        var dj = await r.json().catch(function () { return {}; });
        if (!dj.ok) return false;
        Object.assign(state, {
          lemma: dj.lemma, forms: dj.forms || [],
          phon_us: dj.phonetic_us, phon_uk: dj.phonetic_uk,
          audio_us: dj.audio_us, audio_uk: dj.audio_uk,
          freq_bnc: dj.freq_bnc, translation: dj.translation,
          synonyms: dj.synonyms || [], antonyms: dj.antonyms || [],
          sources_hit: dj.sources_hit || [], vocab_note: dj.vocab_note || ''
        });
        renderState();
        return true;
      }
      // SSE 模式:边读边渲染
      var reader = r.body.getReader();
      var decoder = new TextDecoder();
      var buf = '', gotEcdict = false;
      var renderQueued = false, lastRender = 0;
      var scheduleRender = function () {
        if (myReq !== _resReqId()) return;   // 已被新结果框作废 → 不再写回,防延迟结果覆盖
        var now = Date.now();
        if (now - lastRender >= 80) { renderState(); lastRender = now; return; }
        if (renderQueued) return;
        renderQueued = true;
        setTimeout(function () { renderQueued = false; renderState(); lastRender = Date.now(); }, 80 - (now - lastRender));
      };
      while (true) {
        var rd = await reader.read();
        if (rd.done) break;
        buf += decoder.decode(rd.value, { stream: true });
        var idx;
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          var block = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          var evt = 'message', data = '';
          var lines = block.split('\n');
          for (var li = 0; li < lines.length; li++) {
            var line = lines[li];
            if (line.startsWith('event:')) evt = line.slice(6).trim();
            else if (line.startsWith('data:')) data += line.slice(5).trim();
          }
          var payload = {};
          try { payload = JSON.parse(data || '{}'); } catch (_) {}
          if (evt === 'ecdict') {
            gotEcdict = true;
            Object.assign(state, {
              lemma: payload.lemma, forms: payload.forms || [],
              phon_us: payload.phonetic || '',
              freq_bnc: payload.freq_bnc || 0,
              translation: payload.translation || '',
              definition: payload.definition || '',
              sources_hit: ['ecdict']
            });
            scheduleRender();
          } else if (evt === 'free') {
            if (payload.phon_us) state.phon_us = payload.phon_us;
            if (payload.phon_uk) state.phon_uk = payload.phon_uk;
            if (payload.audio_us && !state.audio_us) state.audio_us = payload.audio_us;
            if (payload.audio_uk && !state.audio_uk) state.audio_uk = payload.audio_uk;
            state.fd_defs = payload.definitions_en || [];
            state.synonyms = payload.synonyms || [];
            state.antonyms = payload.antonyms || [];
            if (state.sources_hit.indexOf('free_dict') < 0) state.sources_hit.push('free_dict');
            scheduleRender();
          } else if (evt === 'mw') {
            if (payload.phon_us) state.phon_us = payload.phon_us;
            if (payload.audio_us) state.audio_us = payload.audio_us;
            state.mw_defs = payload.definitions_en || [];
            if (state.sources_hit.indexOf('mw') < 0) state.sources_hit.push('mw');
            scheduleRender();
          } else if (evt === 'translate') {
            if (payload.en && payload.zh) {
              state.examples_zh[payload.en] = payload.zh;
              scheduleRender();
            }
          } else if (evt === 'done') {
            state.vocab_note = payload.vocab_note || '';
            renderState();
            setTimeout(function () { _refreshUnderlines(); }, 1500);
          } else if (evt === 'error') {
            if (!gotEcdict) return false;   // ECDICT 都没拿到 → 让 AI 回落
          }
        }
      }
      return gotEcdict;
    })();
  }
  window.dictStream = dictStream;

  // 完整字典框「✓ 已掌握」按钮:mastery 直接锁 100% → POST /pdf/api/vocab-mark
  window.markVocabKnown = async function (lemma, btn) {
    if (!lemma) return;
    var old = btn.textContent;
    btn.disabled = true; btn.style.opacity = '0.6'; btn.textContent = '⏳ …';
    try {
      var d = await RC.reqJson('POST', '/pdf/api/vocab-mark', { word: lemma, mark: 'known' });
      if (d && d.ok) {
        btn.textContent = '✓ 已掌握 100%';
        btn.style.background = '#1f5132'; btn.style.borderColor = '#3ba566'; btn.style.color = '#cdf5d9';
        btn.style.opacity = '1';
        _refreshUnderlines();   // 掌握后该词不再标生词下划线
        try { if (_ctx.onMastered) _ctx.onMastered(lemma); } catch (_) {}
      } else {
        btn.disabled = false; btn.style.opacity = '1'; btn.textContent = old;
      }
    } catch (e) {
      btn.disabled = false; btn.style.opacity = '1'; btn.textContent = old;
    }
  };

  // 字典 modal「🎴 加入 Anki」按钮:POST /pdf/api/vocab-anki
  window.addVocabAnki = async function (lemma) {
    if (!lemma) return;
    RC.toast('🎴 正在加 Anki…');
    try {
      var d = await RC.reqJson('POST', '/pdf/api/vocab-anki', { word: lemma });
      if (d.ok) RC.toast(d.action === 'created' ? '✅ Anki 卡已创建' : '✅ Anki 卡已更新');
      else RC.toast('❌ ' + (d.error || '失败'));
    } catch (e) {
      RC.toast('❌ 网络错误：' + e.message);
    }
  };

  // ─────────────────────────── 日语完整框(照搬 19-dict.js)───────────────────────────
  // 轮询 /api/dict-jp-zh,拿后台翻好的例句/汉字字义中文,原地替换英文(跟英文单词一致,不增加等待)。
  function _jpPollZh(word) {
    clearInterval(_jpPollTimer);
    var tries = 0;
    _jpPollTimer = setInterval(async function () {
      tries++;
      var d = null;
      try { d = await RC.reqJson('GET', '/pdf/api/dict-jp-zh?word=' + encodeURIComponent(word)); }
      catch (_) { d = null; }
      if (!d || !d.ok) { if (tries >= 10) clearInterval(_jpPollTimer); return; }
      var pending = false;
      (d.examples || []).forEach(function (e, i) {
        if (e.zh) {
          var el = document.querySelector('.jp-ex-zh[data-exi="' + i + '"]:not([data-zhdone])');
          if (el) { el.textContent = e.zh; el.dataset.zhdone = '1'; }
        } else pending = true;
      });
      (d.kanji || []).forEach(function (k, i) {
        if (k.meanings_zh) {
          if (_jpKanjiData[i] && !_jpKanjiData[i].meanings_zh) {
            _jpKanjiData[i].meanings_zh = k.meanings_zh;
            var chip = document.querySelectorAll('.jp-kanji-chip')[i];
            if (chip && chip.classList.contains('active')) jpKanjiTap(i);   // 详情正打开 → 刷新(本模块局部,别用被 reader.js 夺走的裸全局)
          }
        } else pending = true;
      });
      if (!pending || tries >= 10) clearInterval(_jpPollTimer);
    }, 1500);
  }
  async function dictStreamJP(word, ctx) {
    clearInterval(_jpPollTimer);   // 取消上一个词的中译轮询,避免串到当前词
    _openResult('📖 ' + word, word, '<div class="loading">⏳ 查词中…</div>');
    var myReq = _resReqId();
    var contentEl = document.getElementById('result-content');
    var d;
    try {
      var r = await fetch('/pdf/api/dict-jp?word=' + encodeURIComponent(word) + '&context=' + encodeURIComponent(ctx || ''));
      d = await r.json();
    } catch (e) {
      if (myReq === _resReqId()) contentEl.innerHTML = '<div style="color:#c00;padding:14px">查词失败：' + e.message + '</div>';
      return false;
    }
    if (myReq !== _resReqId()) return false;
    if (!d.ok) {
      if (_isJaWord(word)) {
        // 日语词查不到(典型=人名/专有名词):给终态 + ✨AI 深入讲解直接顶上。
        // 此前落进英文三源框 → 日文进英文管道永远「查询中」空转(2026-07-20 用户实锤「伊部」)。
        contentEl.innerHTML = '<div style="padding:6px 2px 10px;color:#8a9bb4">「' + esc(word) + '」暂无词典释义（可能是人名/专有名词），已请 AI 讲解：</div>' +
          '<button id="jp-ai-btn" style="display:none"></button><div id="jp-ai-out" class="jp-ai-out"></div>';
        try { jpAiDeep(word); } catch (e) {}
        return false;
      }
      return dictStream(word, ctx);   // 也许其实是英文词 → 回退三源框
    }
    var wq = esc(word).replace(/'/g, "\\'");
    var rq = esc(d.reading || word).replace(/'/g, "\\'");   // 发音念假名读音
    var phon = (d.reading && d.accent != null) ? _renderPitch(d.reading, d.accent)
      : (d.reading ? '<span class="wp-phon">' + esc(d.reading) + '</span>' : '');
    var html = '<div class="jp-head">' + phon +
      (d.romaji ? '<span class="jp-romaji">' + esc(d.romaji) + '</span>' : '') +
      (d.pos ? '<span class="jp-pos">' + esc(d.pos) + '</span>' : '') + '</div>';
    if (d.zh) html += '<div class="jp-zh">' + esc(d.zh) + '</div>';
    html += _jpInflectHtml(d.inflect, word);   // 变形分析:原形 + 语法标签
    _jpKanjiData = d.kanji || [];
    if (_jpKanjiData.length) {
      // chip 不用裸 onclick(共享模式下 window._jpKanjiTap 被后加载的 reader.js 夺走、数据为空 → no-op);改 data-ki + 下方 addEventListener 绑本模块 jpKanjiTap
      html += '<div class="jp-sec-label">汉字（点字看音读/训读）</div><div class="jp-kanji-row">' +
        _jpKanjiData.map(function (k, i) { return '<button class="jp-kanji-chip" data-ki="' + i + '">' + esc(k.kanji) + '</button>'; }).join('') +
        '</div><div id="jp-kanji-detail" class="jp-kanji-detail"></div>';
    }
    if ((d.examples || []).length) {
      html += '<div class="jp-sec-label">母语例句</div><div class="jp-ex">';
      d.examples.forEach(function (e, ei) {
        html += '<div class="jp-ex-ja">' + esc(e.ja) + '</div>' +
                '<div class="jp-ex-zh" data-exi="' + ei + '"' + (e.zh ? ' data-zhdone="1"' : '') + '>' +
                esc(e.zh || e.en || '') + '</div>';   // 没中译先回退英文,后台翻好由 _jpPollZh 替换
      });
      html += '</div>';
    }
    html += '<button id="jp-ai-btn" class="jp-ai-btn">✨ AI 深入讲解（用法 / 语感 / 近义辨析）</button>' +
            '<div id="jp-ai-out" class="jp-ai-out"></div>';
    contentEl.innerHTML = html;
    contentEl.scrollTop = 0;
    // 汉字 chip + AI 按钮:addEventListener 绑本模块局部闭包(jpKanjiTap/jpAiDeep 读本模块 _jpKanjiData / _wordPopState),
    //   不依赖裸 window._jpKanjiTap/_jpAiDeep —— 共享模式(PDF)下它们被后加载的 reader.js 同名全局覆盖且数据为空 → 裸全局会静默 no-op。
    contentEl.querySelectorAll('.jp-kanji-chip').forEach(function (btn) {
      btn.addEventListener('click', function () { jpKanjiTap(parseInt(btn.dataset.ki, 10)); });
    });
    var _aiBtn = contentEl.querySelector('#jp-ai-btn');
    if (_aiBtn) _aiBtn.addEventListener('click', function () { jpAiDeep(word); });
    if (_jpKanjiData.length) jpKanjiTap(0);   // 默认展开第一个汉字(本模块局部)
    // 有未翻的例句/汉字字义 → 后台翻 + 轮询替换英文(不增加等待)
    if ((d.examples || []).some(function (e) { return !e.zh; }) || _jpKanjiData.some(function (k) { return !k.meanings_zh; })) _jpPollZh(word);
    var va = document.getElementById('vocab-actions');
    if (va) {
      va.className = 'show';
      var bs = 'border-radius:6px;padding:6px 14px;cursor:pointer;font-size:12px';
      va.innerHTML =
        '<button onclick="_ttsWord(\'' + rq + '\', \'ja-JP\')" style="background:transparent;border:1px solid #3b6db5;color:#a8cdff;' + bs + '">🔊 朗读</button>' +
        '<button onclick="addVocabAnki(\'' + wq + '\')" style="background:#244470;border:1px solid #3b6db5;color:#fff;' + bs + '">🎴 加入 Anki</button>' +
        '<button onclick="markVocabKnown(\'' + wq + '\', this)" style="background:#1d3a28;border:1px solid #2e7d4f;color:#9fe0b8;' + bs + '" title="掌握度设为100%">✓ 已掌握</button>';
    }
    return true;
  }
  window.dictStreamJP = dictStreamJP;

  // 本模块局部(chip 点击/默认展开/后台回填经 addEventListener 绑这个,不受 reader.js 同名全局覆盖影响)。
  function jpKanjiTap(i) {
    var k = _jpKanjiData[i]; if (!k) return;
    document.querySelectorAll('.jp-kanji-chip').forEach(function (c, j) { c.classList.toggle('active', j === i); });
    var det = document.getElementById('jp-kanji-detail'); if (!det) return;
    var h = '<div class="jk-lit">' + esc(k.kanji) + '</div><div class="jk-body">';
    if ((k.on || []).length) h += '<div><span class="jk-tag jk-on">音</span>' + k.on.map(esc).join('、') + '</div>';
    if ((k.kun || []).length) h += '<div><span class="jk-tag jk-kun">訓</span>' + k.kun.map(esc).join('、') + '</div>';
    // 字义优先显示中文(meanings_zh,后端 Google 翻译),缺失才回退英文
    var _meanEn = (k.meanings || []).map(esc).join('; ');
    if (k.meanings_zh || _meanEn) h += '<div class="jk-mean">' + (k.meanings_zh ? esc(k.meanings_zh) : _meanEn) + '</div>';
    h += '</div>';
    det.innerHTML = h;
  }
  window._jpKanjiTap = jpKanjiTap;   // 仅兼容导出(EPUB 无 reader.js 覆盖时可用;本模块内部一律用局部 jpKanjiTap)

  async function jpAiDeep(word) {
    var btn = document.getElementById('jp-ai-btn');
    var out = document.getElementById('jp-ai-out');
    if (!out) return;
    var myReq = _resReqId();
    if (btn) { btn.disabled = true; btn.textContent = '✨ 生成中…'; }
    var ctx = (_wordPopState && _wordPopState.ctx) || '';
    try {
      var render = function (text) {
        if (myReq !== _resReqId()) return;   // 结果框已被新内容作废
        out.innerHTML = RC.md(text || ' ');
        RC.typeset(out);
        if (out.scrollIntoView) out.scrollIntoView({ block: 'nearest' });
      };
      var res = await _aiStream('/pdf/api/dict-jp-ai?word=' + encodeURIComponent(word) + '&context=' + encodeURIComponent(ctx), { method: 'GET', onText: render });
      if (myReq !== _resReqId()) return;
      if (res.ok) render(res.text);
      else out.innerHTML = '<span style="color:#c00">AI 失败：' + (res.error || '') + '</span>';
    } catch (e) {
      out.innerHTML = '<span style="color:#c00">AI 失败：' + e.message + '</span>';
    }
    if (btn) btn.style.display = 'none';
  }
  window._jpAiDeep = jpAiDeep;   // 仅兼容导出;本模块的 AI 按钮经 addEventListener 直接绑局部 jpAiDeep

  // ─────────────────────────── SSE 抗断连流(照搬 21-misc-ai.js::_aiStream)───────────────────────────
  // SSE 主路 + 切后台/网抖回退轮询(/pdf/api/ai-stream-result?id=rid,后台线程跑完结果不丢)。
  async function _aiStream(url, opts) {
    opts = opts || {};
    var method = (opts.method || 'GET').toUpperCase();
    var rid = 'r' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
    var furl = url, body = null;
    var headers = { 'Accept': 'text/event-stream' };
    if (method === 'GET') {
      furl += (url.indexOf('?') >= 0 ? '&' : '?') + 'rid=' + encodeURIComponent(rid);
    } else {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(Object.assign({}, opts.body || {}, { rid: rid }));
    }
    var onTextRaw = opts.onText || function () {};
    var acc = '', finished = false, usingPoll = false, lastCb = 0, cbTimer = null;
    var ctrl = new AbortController();
    return new Promise(function (resolve) {
      var emit = function (force) {                  // onText 节流(~80ms),结束时强制
        if (finished && !force) return;
        var now = Date.now();
        if (force || now - lastCb >= 80) { lastCb = now; if (cbTimer) { clearTimeout(cbTimer); cbTimer = null; } try { onTextRaw(acc); } catch (_) {} }
        else if (!cbTimer) cbTimer = setTimeout(function () { cbTimer = null; lastCb = Date.now(); try { onTextRaw(acc); } catch (_) {} }, 80 - (now - lastCb));
      };
      var cleanup = function () { document.removeEventListener('visibilitychange', onVis); if (cbTimer) clearTimeout(cbTimer); try { ctrl.abort(); } catch (_) {} };
      var finish = function (ok, error) { if (finished) return; finished = true; try { onTextRaw(acc); } catch (_) {} cleanup(); resolve({ ok: ok, text: acc, error: error || '' }); };
      // 回退轮询:后台线程仍在跑,拉它已生成的完整文本
      var pollN = 0;
      var poll = async function () {
        if (finished) return;
        pollN++;
        try {
          var d = await RC.reqJson('GET', '/pdf/api/ai-stream-result?id=' + encodeURIComponent(rid));
          if (typeof d.full === 'string' && d.full.length > acc.length) { acc = d.full; emit(); }
          if (d.status === 'done') return finish(true);
          if (d.status === 'error') return finish(false, d.error || 'AI 失败');
          if (d.status === 'unknown' && pollN > 3) return finish(!!acc, acc ? '' : '任务丢失(服务重启?)');
        } catch (_) { /* 网瞬断:继续 */ }
        if (pollN > 240) return finish(!!acc, acc ? '' : '轮询超时');   // ~5min 兜底
        setTimeout(poll, 1200);
      };
      var startPoll = function () { if (usingPoll || finished) return; usingPoll = true; try { ctrl.abort(); } catch (_) {} poll(); };
      // 切后台→回前台:iOS 挂起 JS 会让 SSE reader 卡死,主动转轮询
      var onVis = function () { if (document.visibilityState === 'visible' && !finished) startPoll(); };
      document.addEventListener('visibilitychange', onVis);
      // SSE 主路
      (async function () {
        try {
          var r = await fetch(furl, { method: method, headers: headers, body: body, signal: ctrl.signal });
          var ct = r.headers.get('content-type') || '';
          if (!r.ok || !ct.includes('event-stream')) {   // 服务端没流式(多半错误 JSON) → 当普通 JSON
            var dj = {}; try { dj = await r.json(); } catch (_) {}
            if (dj && dj.ok && (dj.translation || dj.explanation)) { acc = dj.translation || dj.explanation; return finish(true); }
            return finish(false, (dj && dj.error) || ('HTTP ' + r.status));
          }
          var reader = r.body.getReader(), dec = new TextDecoder();
          var buf = '';
          while (true) {
            var rd = await reader.read();
            if (rd.done) break;
            if (usingPoll || finished) return;
            buf += dec.decode(rd.value, { stream: true });
            var idx;
            while ((idx = buf.indexOf('\n\n')) >= 0) {
              if (usingPoll || finished) return;
              var ev = buf.slice(0, idx); buf = buf.slice(idx + 2);
              if (/^event:\s*error/m.test(ev)) { var er = ''; var me = ev.match(/^data:\s*(.*)/m); try { er = JSON.parse(me[1]).error; } catch (_) {} return finish(false, er || 'AI 失败'); }
              if (/^event:\s*done/m.test(ev)) return finish(true);
              var m = ev.match(/^data:\s*(.*)/m);
              if (m) { try { var j = JSON.parse(m[1]); if (j.text) { acc += j.text; emit(); } } catch (_) {} }
            }
          }
          if (!finished && !usingPoll) startPoll();   // 流断了没收到 done → 转轮询补全
        } catch (e) {
          if (!finished && !usingPoll) startPoll();    // SSE 出错/abort → 后台线程仍在跑,转轮询
        }
      })();
    });
  }

  // ── PDF 阶段2 新增:门控点(reader.src _expandWordFull)直接开「完整词条大框」,跳过核心小框 ──
  // 设 _ctx(file/page/langs/ctx,供英文 dictStream 暴露计数)+ _wordPopState(供 _jpAiDeep 取 ctx),
  // 再按 jp 路由 dictStreamJP/dictStream。EPUB 不用此入口(其展开走 window._expandWordFull,_ctx 已由 show 设),
  // 故只是新增一个导出方法,show 与 EPUB 行为完全不变。
  function openFull(opts) {
    opts = opts || {};
    injectCss();
    var word = String(opts.word == null ? '' : opts.word).trim();
    if (!word) return;
    _ctx = {
      file: opts.file || '', page: opts.page || 0, langs: opts.langs || [], ctx: opts.ctx || '',
      rect: null, markHighlight: null, onMastered: null, onGrammar: null, onFallback: null, ignoreSelector: ''
    };
    _wordPopState = { word: word, ctx: opts.ctx || '', lemma: word };
    var jp = (opts.jp != null) ? !!opts.jp : _isJaWord(word);
    if (jp) dictStreamJP(word, opts.ctx || '');
    else dictStream(word, opts.ctx || '');
  }

  RC.wordpop = { show: show, openFull: openFull, clearHls: _removeAllWordHls, prewarm: prewarm, clearCache: clearDictCache };   // clearHls:清查词高亮;prewarm(words):翻页后台预热释义;clearCache:切书/失效释放
})();
