/* epub2-deco.js — epub.js 版 EPUB 阅读器:章节 iframe 内容装饰层
 *   P4 生词下划线 / P5 振假名·音标 ruby / P6 词组 / P10 需要翻译的语言(BOOK_LANGS 门控)
 *
 * 跟手搓版 epub-html.js 的区别只有一个:底座从「主文档 #ep-col 里的 .ep-sec 段」换成
 * 「epub.js 每个章节 iframe 的 document」。逻辑(分词 / 门控 / 端点 / 字段 / 按钮文案 / 呼吸态)逐字照搬。
 *
 * ── 装饰怎么注进 iframe(关键) ──
 *   epub.js 的章节内容跑在各自的 <iframe> 里(不是父文档)。装饰 = 在那个 iframe 的 document 上
 *   TreeWalker 遍历文字节点 + wrap(下划线 <span> / ruby / 呼吸 <mark>)。每个 iframe 还要把装饰 CSS
 *   注进它自己的 <head>(父文档样式管不到 iframe)。
 *   触发点:rendition.on('rendered', (section, view) => decorate(view.contents.document))
 *           + R.hooks.content.register(contents => decorate(contents.document))
 *           + 已渲染的 R.getContents().forEach(c => decorate(c.document))
 *           + 开书初期反复 scan(修首屏竞态)。
 *   epub.js 卸载离屏章节会丢装饰(整个 iframe 销毁)→ 该章重渲时 rendered 再次触发,重新装饰。
 *   幂等:CSS 注入用 doc.__ep2Css 守卫;生词/ruby 各用 body.dataset.ep2Vocab / ep2Ruby 守卫
 *        (重复 rendered 不重复包);选区/词组 tap 监听用 doc.__ep2Sel / __ep2Tap 守卫。
 *
 * ── BOOK_LANGS(P10)门控 ──
 *   GET /pdf/api/book-langs 取本书「需要翻译的语言」数组(如 ['en','ja']);没勾的语言=母语,
 *   不画生词下划线。**中文汉字落在 ja 正则范围里,必须靠这层 gate 才不被当日语划线**(照搬 PDF)。
 *   设置面板「保存」→ window.saveLangPicker POST 回去 + 重画所有已渲染 iframe。
 *
 * ── 照搬来源 ──
 *   生词分词/门控:epub-html.js::_vocabTokens / _vocabApplySection / _favPhraseAt(= PDF 08-charlayer + 12-vocab-sentences)
 *   ruby:        epub-html.js::_rubyApplySection / _wrapTokens / _unwrap(= PDF 09-ruby)
 *   词组:        epub-html.js::_isShortPhrase / onPhrase / _openPhrasePopover(= PDF 14/15)+ 共享层 RC.phrasepop
 *   BOOK_LANGS:  epub-html.js::loadBookLangs / saveLangPicker
 *
 * ── 用到的端点(只调,不改) ──
 *   GET  /pdf/api/book-langs / POST /pdf/api/book-langs        本书语言
 *   GET  /pdf/api/vocab-mastery-map                            生词掌握度查找表 {map:{word:{label,mastery}}}
 *   POST /pdf/api/epub-furigana                                振假名/音标 {items:[{tokens:[{start,end,reading}]}]}
 *   /pdf/api/phrases /pdf/api/phrase-mark /pdf/api/dict-jp …   词组收藏/掌握/查(由 rc-phrasepop 调)
 *
 * 依赖:epub-reader.js(window.__epub) + rc-phrasepop.js(RC.phrasepop) + rc-wordpop/rc-result/rc-settings(已在页内)。
 */
(function () {
  'use strict';
  function ready(fn) { if (window.__epub && window.__epub.rendition) fn(); else setTimeout(function () { ready(fn); }, 120); }
  ready(init);

  function init() {
    var R = window.__epub.rendition, CFG = window.__epub.cfg || {};
    var FREL = CFG.fileRel || '';
    var $ = function (id) { return document.getElementById(id); };
    function toast(m) { if (window.RC && RC.toast) RC.toast(m); }
    function aiParams() { return (window.RC && RC.settings && RC.settings.aiParams) ? RC.settings.aiParams() : {}; }

    // ════════════════════════════════════════════════════════════════════════
    // 1) BOOK_LANGS(P10)—— 照搬 epub-html.js loadBookLangs / saveLangPicker。
    //    saveLangPicker 暴露到 window:设置面板「保存」按钮(rc-settings onSaveLangs)调它。
    // ════════════════════════════════════════════════════════════════════════
    var BOOK_LANGS = [];
    function bookLangsArr() { return BOOK_LANGS; }
    function loadBookLangs() {
      fetch('/pdf/api/book-langs?file=' + encodeURIComponent(FREL)).then(function (r) { return r.json(); }).then(function (d) {
        if (d && d.ok) { BOOK_LANGS = d.langs || []; vocabClear(); jaWordClear(); decorateAll(); if (window.__epubRewrapWords) window.__epubRewrapWords(); }
      }).catch(function () {});
    }
    window.saveLangPicker = function () {
      var langs = Array.prototype.map.call(document.querySelectorAll('#eph-lang-checks input:checked'), function (c) { return c.value; });
      fetch('/pdf/api/book-langs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ file: FREL, langs: langs }) })
        .then(function (r) { return r.json(); }).then(function (d) {
          if (d && d.ok) BOOK_LANGS = d.langs || langs;
          toast('已保存需要翻译的语言:' + (BOOK_LANGS.join(' / ') || '无(全部免于翻译)'));
          vocabClear(); jaWordClear(); decorateAll();   // 门控变了 → 清+重画生词下划线 + 日语分词浮层
          if (window.__epubRewrapWords) window.__epubRewrapWords();   // 'en' 变化 → 重包英文 .ep-w 单击 span
        }).catch(function (e) { toast('保存失败:' + (e.message || '网络错误')); });
    };

    // ── HTTP 小工具 ──
    function postJson(url, body, ok, err) {
      fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
        .then(function (r) { return r.json(); })
        .then(function (d) { if (d && d.ok) ok(d); else if (err) err((d && d.error) || '失败'); })
        .catch(function (e) { if (err) err(e.message || '网络错误'); });
    }

    // ════════════════════════════════════════════════════════════════════════
    // 2) iframe 装饰底座 —— CSS 注入 / 遍历 / wrap / unwrap(逐字照搬 epub-html.js,
    //    把「secEl / col」换成「iframe doc / doc.body」,创建元素一律用 ownerDocument)。
    // ════════════════════════════════════════════════════════════════════════
    // 装饰 CSS(去掉手搓版的 #ep-col 前缀;rt 加 pointer-events:none 让点击穿透到基字,
    // 避免点振假名读音被 epub2 当词选中。颜色不依赖父文档 --lnk,直接给跨主题可读的蓝)。
    var DECO_CSS = [
      'ruby[data-eph]{ruby-align:center}',
      'rt.ep-rt{font-size:.55em;line-height:1;color:#3b82f6;font-weight:600;user-select:none;pointer-events:none}',
      '.ep-vocab-und{border-bottom:2px solid transparent}',
      '.ep-vocab-und.m-new{border-bottom-color:#f59e0b}',
      '.ep-vocab-und.m-learning{border-bottom-color:#fb923c}',
      '.ep-vocab-und.m-seen{border-bottom-color:#facc15}',
      '.ep-vocab-und.m-known{border-bottom-color:#a3e635;opacity:.85}',
      // 乐观去下划线(点「已掌握」后立刻隐线,不等服务端;失败再撤销该 class。服务端刷新后整词不再 wrap → class 自然消失)
      '.ep-vocab-und.ep2-und-opt-off{border-bottom-color:transparent !important;opacity:1 !important}',
      'mark.ep-phrase-hl{background:rgba(59,109,181,.42);border:1px solid rgba(111,211,255,.85);border-radius:3px;color:inherit;cursor:pointer;padding:0;-webkit-box-decoration-break:clone;box-decoration-break:clone}',
      'mark.ep-phrase-hl.breathe{animation:ep2-phrase-hl-breathe 1.1s ease-in-out infinite}',
      '@keyframes ep2-phrase-hl-breathe{0%,100%{opacity:.45}50%{opacity:.9}}'
    ].join('\n');
    function injectCss(doc) {
      if (!doc || doc.__ep2Css) return; doc.__ep2Css = 1;
      try { var s = doc.createElement('style'); s.textContent = DECO_CSS; (doc.head || doc.documentElement).appendChild(s); } catch (e) {}
    }
    // 当前所有已渲染章节的 document
    function contentsDocs() {
      try { var ad = window.RC && RC.adapter && RC.adapter(); if (ad && ad.eachContentDoc) { var out = []; ad.eachContentDoc(function (doc) { if (doc) out.push(doc); }); return out; } } catch (e) {}
      try { return (R.getContents() || []).map(function (c) { return c && c.document; }).filter(Boolean); } catch (e) { return []; }   // 回退:adapter 未就绪
    }
    function findIframe(doc) {
      try { if (doc.defaultView && doc.defaultView.frameElement) return doc.defaultView.frameElement; } catch (e) {}
      var ifrs = document.querySelectorAll('#ep-viewer iframe');
      for (var i = 0; i < ifrs.length; i++) { try { if (ifrs[i].contentDocument === doc) return ifrs[i]; } catch (e) {} }
      return null;
    }
    // iframe 内坐标 → 父视口坐标(加 iframe 的 getBoundingClientRect 偏移;同 epub2.js captureSelection)
    function rectInParent(doc, r) {
      var ifr = findIframe(doc), ib = ifr ? ifr.getBoundingClientRect() : { left: 0, top: 0 };
      return { left: ib.left + r.left, top: ib.top + r.top, right: ib.left + r.right, bottom: ib.top + r.bottom };
    }
    // _countable:点在我们注入的 rt 读音上 → 不计入(照搬 epub-html._countable,只认 data-eph 标记)
    function countable(textNode) {
      var el = textNode.parentElement, body = textNode.ownerDocument && textNode.ownerDocument.body;
      while (el && el !== body) {
        if (el.tagName === 'RT' && el.getAttribute && el.getAttribute('data-eph') === '1') return false;
        el = el.parentElement;
      }
      return true;
    }
    // 候选文字节点(照搬 epub-html._decoCandidates):跳过 rt 读音 / mark / ruby;vocab 模式再跳 .ep-vocab-und
    function decoCandidates(root, mode) {
      var doc = root.ownerDocument || root;
      var w = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT, null), n, out = [];
      while ((n = w.nextNode())) {
        if (!countable(n)) continue;
        var v = n.nodeValue; if (!v || !v.trim()) continue;
        var p = n.parentElement; if (!p) continue;
        if (p.closest && p.closest('mark')) continue;
        if (p.closest && p.closest('ruby')) continue;
        if (mode === 'vocab' && p.closest && p.closest('.ep-vocab-und')) continue;
        if (mode === 'jaw' && p.closest && (p.closest('.ep-vocab-und') || p.closest('.ep-w'))) continue;
        out.push(n);
      }
      return out;
    }
    // 把一个文字节点按 token offset 切片 wrap(照搬 epub-html._wrapTokens;rt 用 ownerDocument 建)
    function wrapTokens(node, toks, makeWrap) {
      var od = node.ownerDocument;
      var sorted = toks.slice().filter(function (t) { return t.end > t.start; }).sort(function (a, b) { return a.start - b.start; });
      function appendExtra(wrap, t) { if (t.__rt != null) { var rt = od.createElement('rt'); rt.className = 'ep-rt'; rt.setAttribute('data-eph', '1'); rt.textContent = t.__rt; wrap.appendChild(rt); } }
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
    // 拆 wrap(照搬 epub-html._unwrap:剥掉 rt 读音子节点,保留可见文本,父 normalize)
    function unwrapEl(el) {
      var p = el.parentNode; if (!p) return;
      while (el.firstChild) {
        var c = el.firstChild;
        if (c.nodeType === 1 && c.tagName === 'RT') { el.removeChild(c); continue; }
        p.insertBefore(c, el);
      }
      p.removeChild(el); if (p.normalize) p.normalize();
    }

    // ════════════════════════════════════════════════════════════════════════
    // 3) 生词下划线(P4)+ 语言门控(P10)—— 逐字照搬 epub-html.js。
    //    _vocabMap 整本一次(/api/vocab-mastery-map),客户端缓存;各 iframe 只对本章文本出现的词查表着色。
    // ════════════════════════════════════════════════════════════════════════
    var _vocabMap = null;
    function vocabOn() { var v = localStorage.getItem('eph-vocab-underline'); return v === null ? true : v === '1'; }
    function loadVocabMap() {
      if (_vocabMap || !vocabOn()) return;
      fetch('/pdf/api/vocab-mastery-map?file=' + encodeURIComponent(FREL)).then(function (r) { return r.json(); }).then(function (d) {
        if (d && d.ok) { _vocabMap = d.map || {}; _rvMapKeys = Object.keys(_vocabMap).sort().join(','); decorateAll(); }
      }).catch(function () {});
    }
    // 已掌握词组(归一化键):标掌握后 vocabTokens 命中也不画下划线(照搬 PDF 15-phrase-wordpop _phraseMarkSet/_phraseNorm)
    var _phraseMarkSet = new Set();
    function phraseNorm(s) { return String(s || '').trim().replace(/\s+/g, ' ').toLowerCase(); }
    function loadPhraseMarks(cb) {
      fetch('/pdf/api/phrase-mark').then(function (r) { return r.json(); }).then(function (d) {
        if (d && d.ok) _phraseMarkSet = new Set(d.mastered || []);
      }).catch(function () {}).then(function () { if (cb) cb(); });
    }
    // 收藏词组里从 i 起找最长匹配(拉丁大小写不敏感)。照搬 epub-html._favPhraseAt(收藏词组=一个分词单元)
    function favPhraseAt(s, i, favs) {
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
    // 分词 + 门控(逐字照搬 epub-html._vocabTokens):wantEn/wantJa 由 BOOK_LANGS 决定;两者都没勾 → 不划线
    function vocabTokens(s) {
      var out = [], i = 0, L = s.length;
      var wantEn = BOOK_LANGS.indexOf('en') >= 0, wantJa = BOOK_LANGS.indexOf('ja') >= 0;
      if (!wantEn && !wantJa) return out;
      var en = function (c) { return /[A-Za-z]/.test(c); }, ja = function (c) { return /[぀-ヿ㐀-鿿一-鿿]/.test(c); };
      var favs = (window.RC && RC.phrasepop && RC.phrasepop.favList) ? RC.phrasepop.favList() : [];   // 英文收藏词组也参与合并(去掉 wantJa 门控,对齐 PDF)
      while (i < L) {
        if (favs.length) {
          var fh = favPhraseAt(s, i, favs);
          if (fh) {
            var key = s.slice(fh.start, fh.end);
            if (!_phraseMarkSet.has(phraseNorm(key))) {   // 已掌握词组命中也不画下划线(对齐 PDF)
              var info0 = _vocabMap[key] || _vocabMap[key.toLowerCase()];
              if (info0 && info0.label) out.push({ start: fh.start, end: fh.end, label: info0.label });
            }
            i = fh.end; continue;
          }
        }
        var c = s[i];
        if (wantEn && en(c)) { var j = i + 1; while (j < L && /[A-Za-z'’\-]/.test(s[j])) j++; var info = _vocabMap[s.slice(i, j).toLowerCase()]; if (info && info.label) out.push({ start: i, end: j, label: info.label }); i = j; continue; }
        if (wantJa && ja(c)) {
          var hit = null, maxL = Math.min(12, L - i);
          for (var len = maxL; len >= 1; len--) { var inf = _vocabMap[s.slice(i, i + len)]; if (inf && inf.label) { hit = { start: i, end: i + len, label: inf.label }; break; } }
          if (hit) { out.push(hit); i = hit.end; continue; }
          i++; continue;
        }
        i++;
      }
      return out;
    }
    function vocabApply(doc) {
      if (!vocabOn() || !_vocabMap) return;
      var body = doc.body; if (!body || body.dataset.ep2Vocab === '1') return;
      body.dataset.ep2Vocab = '1';
      decoCandidates(body, 'vocab').forEach(function (n) {
        var toks = vocabTokens(n.nodeValue);
        if (toks.length) wrapTokens(n, toks, function (t) { var sp = doc.createElement('span'); sp.className = 'ep-vocab-und m-' + t.label; return sp; });
      });
    }
    function vocabClear() {
      contentsDocs().forEach(function (doc) {
        try { doc.querySelectorAll('span.ep-vocab-und').forEach(unwrapEl); if (doc.body) doc.body.dataset.ep2Vocab = ''; } catch (e) {}
      });
    }
    function setVocabUnderline(on) {
      try { localStorage.setItem('eph-vocab-underline', on ? '1' : '0'); } catch (_) {}
      if (on) { if (!_vocabMap) loadVocabMap(); else decorateAll(); } else vocabClear();
    }
    // 门控/分词依据变了(切语言 / 收藏词组)→ 清+重画(map 不变,无需重取)
    function refreshVocab() { if (vocabOn()) { vocabClear(); decorateAll(); } }
    // 乐观去下划线:点「已掌握」后立刻把该词的生词下划线隐掉(不等服务端 vocab-mark + map 刷新),失败再撤销。
    //   返回 restore():撤销本次隐藏。服务端落库后 refreshVocabUnderlinesForAllPages 重画时该词已不在 map → span 自然不再 wrap。
    function optimisticMaster(word) {
      var lower = String(word || '').trim().toLowerCase();
      if (!lower) return function () {};
      var touched = [];
      contentsDocs().forEach(function (doc) {
        try {
          doc.querySelectorAll('span.ep-vocab-und').forEach(function (sp) {
            var t = (sp.textContent || '').trim();
            if ((t.toLowerCase() === lower || t === word) && !sp.classList.contains('ep2-und-opt-off')) {
              sp.classList.add('ep2-und-opt-off'); touched.push(sp);
            }
          });
        } catch (e) {}
      });
      return function restore() { touched.forEach(function (sp) { try { sp.classList.remove('ep2-und-opt-off'); } catch (e) {} }); };
    }
    // 查词/标掌握后实时刷新(照搬 PDF refreshVocabUnderlinesForAllPages):重取 map + 清 + 重画
    var _rvT = null, _rvMapKeys = '';
    window.refreshVocabUnderlinesForAllPages = function () {
      clearTimeout(_rvT);
      _rvT = setTimeout(function () {
        fetch('/pdf/api/vocab-mastery-map?file=' + encodeURIComponent(FREL)).then(function (r) { return r.json(); }).then(function (d) {
          if (!d || !d.ok) return;
          var nm = d.map || {}, keys = Object.keys(nm).sort().join(',');
          if (keys === _rvMapKeys) return;   // map 没变(查的是已知词/已掌握态没改)→ 不重画,避免整屏下划线闪烁
          _rvMapKeys = keys; _vocabMap = nm; vocabClear(); decorateAll();
        }).catch(function () {});
      }, 900);   // 防抖:rc-wordpop 每次查词调 3 次(0/1800/3500ms)→ 合并成 1 次
    };

    // ════════════════════════════════════════════════════════════════════════
    // 4) 振假名 / 音标 ruby(P5)—— 顶栏「あ」按钮(ep-ruby)开关。逐字照搬 epub-html._rubyApplySection。
    //    日语词:unidic 平假名读音;英文词:ECDICT 音标。各 iframe 独立请求 /api/epub-furigana。
    // ════════════════════════════════════════════════════════════════════════
    function rubyOn() { return localStorage.getItem('eph-ruby') === '1'; }   // 持久化(照搬 PDF _rubyEnabled 的 pdf-ruby 键,换 eph-ruby)
    function setRubyOn(on) { try { localStorage.setItem('eph-ruby', on ? '1' : '0'); } catch (_) {} }
    function rubyApply(doc) {
      if (!rubyOn()) return;
      var body = doc.body; if (!body || body.dataset.ep2Ruby === '1') return;
      body.dataset.ep2Ruby = '1';
      var nodes = decoCandidates(body, 'ruby').filter(function (n) { return /[㐀-鿿一-鿿A-Za-z]/.test(n.nodeValue); });
      if (!nodes.length) return;
      postJson('/pdf/api/epub-furigana', { file: FREL, texts: nodes.map(function (n) { return n.nodeValue; }) }, function (d) {
        if (!rubyOn()) { body.dataset.ep2Ruby = ''; return; }
        var items = d.items || [];
        nodes.forEach(function (n, i) {
          var toks = ((items[i] && items[i].tokens) || []).filter(function (t) { return t.reading; })
            .map(function (t) { return { start: t.start, end: t.end, __rt: t.reading }; });
          if (toks.length) wrapTokens(n, toks, function () { var r = doc.createElement('ruby'); r.setAttribute('data-eph', '1'); return r; });
        });
        verifyRuby(doc);   // 渲染后后台 AI 读音校正(照搬 PDF 09-ruby _verifyFurigana)
      }, function () { body.dataset.ep2Ruby = ''; });
    }
    // 振假名读音 AI 上下文校正(照搬 PDF 09-ruby _verifyFurigana):后台调 /pdf/api/epub-furigana-verify,
    // 拿纠正(计数器/熟字训/多音字)原地替换 rt。每个 section 只调一次(dataset 守卫防重复);不阻塞渲染。
    function verifyRuby(doc) {
      var body = doc.body; if (!body || body.dataset.ep2RubyVer === '1') return;
      var rtEls = [].slice.call(body.querySelectorAll('ruby[data-eph="1"] > rt.ep-rt'));
      if (!rtEls.length) return;
      body.dataset.ep2RubyVer = '1';
      var readings = rtEls.map(function (rt) {
        var base = '', rb = rt.parentElement;
        if (rb) { for (var c = rb.firstChild; c; c = c.nextSibling) { if (c.nodeType === 3) base += c.nodeValue; } }
        return { base: base, rt: rt.textContent || '' };
      });
      postJson('/pdf/api/epub-furigana-verify', { file: FREL, text: (body.textContent || '').slice(0, 4000), readings: readings }, function (d) {
        if (!rubyOn()) return;
        var fixed = d.readings || [];
        if (fixed.length === rtEls.length) {   // 整列纠正后读音(对齐契约 {text,readings}→readings)
          rtEls.forEach(function (rt, i) {
            var nr = fixed[i], s = (nr && typeof nr === 'object') ? (nr.rt != null ? nr.rt : nr.reading) : nr;
            if (s != null && s !== '' && s !== rt.textContent) rt.textContent = s;
          });
        } else if ((d.fixes || []).length) {   // 兼容 PDF 同款 sparse fixes:[{i,r}]
          d.fixes.forEach(function (f) { var rt = rtEls[f.i]; if (rt && f.r != null && rt.textContent !== f.r) rt.textContent = f.r; });
        }
      }, function () { body.dataset.ep2RubyVer = ''; });
    }
    function rubyClear() {
      contentsDocs().forEach(function (doc) {
        try { doc.querySelectorAll('ruby[data-eph="1"]').forEach(unwrapEl); if (doc.body) { doc.body.dataset.ep2Ruby = ''; doc.body.dataset.ep2RubyVer = ''; } } catch (e) {}
      });
    }
    // ── 日语分词浮层(抛弃轮询):'ja' 启用 → 每个日语词包 .ep-w[data-jaw] 可点 span(全分词 /api/epub-tokenize,含纯假名/助词)→
    //    父文档浮层按钮盖词上 → 精确单击查词。vocab 先跑(.ep-vocab-und 被跳过,本就被浮层覆盖);ja 包完再跑 ruby(嵌进 .ep-w 不冲突)。──
    function jaWordApply(doc, done) {
      done = done || function () {};
      if (BOOK_LANGS.indexOf('ja') < 0) { done(); return; }
      var body = doc.body; if (!body || body.dataset.ep2Jaw === '1') { done(); return; }
      body.dataset.ep2Jaw = '1';
      var nodes = decoCandidates(body, 'jaw').filter(function (nd) { return /[぀-ヿ㐀-鿿一-鿿]/.test(nd.nodeValue); });
      if (!nodes.length) { body.dataset.ep2Jaw = ''; done(); return; }
      postJson('/pdf/api/epub-tokenize', { file: FREL, texts: nodes.map(function (nd) { return nd.nodeValue; }) }, function (d) {
        try {
          if (BOOK_LANGS.indexOf('ja') < 0) { body.dataset.ep2Jaw = ''; done(); return; }
          var items = d.items || [];
          nodes.forEach(function (nd, i) {
            if (!nd.parentNode) return;
            var sent = items[i] && items[i].text;
            if (nd.nodeValue !== sent) return;   // 节点已被其它装饰改动 → 跳过(防 token 偏移错位)
            var toks = ((items[i] && items[i].tokens) || []).filter(function (t) { var seg = (nd.nodeValue || '').slice(t.start, t.end); return /[぀-ヿ㐀-鿿一-鿿]/.test(seg); });
            if (toks.length) wrapTokens(nd, toks, function () { var sp = doc.createElement('span'); sp.className = 'ep-w'; sp.setAttribute('data-jaw', '1'); return sp; });
          });
          if (window.__epubRebuildWordOv) window.__epubRebuildWordOv();
        } catch (e) {}
        done();
      }, function () { body.dataset.ep2Jaw = ''; done(); });
    }
    function jaWordClear() {
      contentsDocs().forEach(function (doc) {
        try { doc.querySelectorAll('span.ep-w[data-jaw]').forEach(unwrapEl); if (doc.body) doc.body.dataset.ep2Jaw = ''; } catch (e) {}
      });
    }
    function toggleRuby() {
      var on = !rubyOn(); setRubyOn(on);
      var b = $('ep-ruby'); if (b) b.classList.toggle('active', on);
      if (on) { toast('振假名 / 音标 已开'); decorateAll(); } else rubyClear();
    }
    var _rubyBtn = $('ep-ruby');
    if (_rubyBtn) { _rubyBtn.addEventListener('click', toggleRuby); _rubyBtn.classList.toggle('active', rubyOn()); }   // init 回填持久态(开则下方 decorateAll/scan 会自动上 ruby)

    // ════════════════════════════════════════════════════════════════════════
    // 5) 词组(P6)—— 选短多词 → 工具栏「📘词组」→ RC.phrasepop.show + 呼吸 <mark>(注进 iframe doc)。
    //    判定 _isShortPhrase / 浮层回调 / 收藏作分词 —— 逐字照搬 epub-html.js onPhrase / _openPhrasePopover。
    //    选区快照走自有 _sel(轮询 + 事件维护):移动端 tap 工具栏会先收起原生选区,必须用快照不能现取。
    // ════════════════════════════════════════════════════════════════════════
    function isWordSel(t) { t = t || ''; return t.length <= 30 && !/\s/.test(t) && (/^[A-Za-z][A-Za-z'’\-]*$/.test(t) || /[぀-ヿ]/.test(t)); }
    function isShortPhrase(text) {   // 逐字照搬 epub-html._isShortPhrase
      var t = (text || '').trim();
      if (!t) return false;
      if (/[。！？、，.!?]$/.test(t)) return false;
      if (/[぀-ヿ㐀-鿿]/.test(t)) return t.length >= 2 && t.length <= 8 && !/[。！？、，.!?]/.test(t);
      var words = t.split(/\s+/).filter(Boolean);
      return words.length >= 2 && words.length <= 5 && t.length <= 40;
    }
    // 取当前(任一 iframe 内)未折叠选区信息;无则 null
    function currentSelInfo() {
      var cs; try { cs = R.getContents() || []; } catch (e) { return null; }
      for (var i = 0; i < cs.length; i++) {
        try {
          var win = cs[i].window, doc = cs[i].document, s = win.getSelection();
          if (s && !s.isCollapsed) {
            var t = (s.toString() || '').trim();
            if (t) {
              var rng = s.getRangeAt(0);
              var node = rng.startContainer, blk = node.nodeType === 3 ? node.parentElement : node;
              blk = blk && blk.closest ? blk.closest('p,li,td,blockquote,div,section,h1,h2,h3,h4') : null;
              return { text: t, doc: doc, win: win, range: rng.cloneRange(), domRect: rng.getBoundingClientRect(),
                       ctx: (blk ? (blk.textContent || '') : '').trim().slice(0, 1200) };
            }
          }
        } catch (e) {}
      }
      return null;
    }
    var _sel = null;   // 选区快照(轮询/事件更新;onPhrase 用它,不现取——移动端 tap 会先收起选区)
    function updatePhraseBtn() {
      var info = currentSelInfo();
      var show = false;
      if (info && info.text) { _sel = info; show = !isWordSel(info.text) && isShortPhrase(info.text); }
      var b = $('ep2-phrase-btn');
      if (b) { b.style.display = show ? '' : 'none'; b.classList.toggle('breathe', show); }
    }

    // 父文档样式:📘词组按钮(配色/呼吸,照 #ep-phrase-btn)
    function injectParentCss() {
      if (document.getElementById('ep2-deco-css')) return;
      var s = document.createElement('style'); s.id = 'ep2-deco-css';
      s.textContent = [
        '#ep2-phrase-btn{background:#244470 !important;border-color:#6fd3ff !important;color:#cfe6ff !important}',
        '#ep2-phrase-btn.breathe{animation:ep2-phrase-btn-breathe 1.1s ease-in-out infinite}',
        '@keyframes ep2-phrase-btn-breathe{0%,100%{box-shadow:0 0 0 0 rgba(111,211,255,0)}50%{box-shadow:0 0 0 3px rgba(111,211,255,.45)}}'
      ].join('\n');
      document.head.appendChild(s);
    }
    // 注 📘词组 按钮进选区工具栏(无 data-grp → epub2.js showSel 不管它的显隐,由本模块按短词组判定控制)
    function ensurePhraseBtn() {
      var btns = document.querySelector('#ep-sel .ep-btns'); if (!btns) return;
      if (document.getElementById('ep2-phrase-btn')) return;
      injectParentCss();
      var b = document.createElement('button'); b.id = 'ep2-phrase-btn'; b.textContent = '📘 词组'; b.title = '当作词组：看释义/翻译 + 收藏作分词依据'; b.style.display = 'none';
      b.addEventListener('click', function (e) {
        e.stopPropagation();   // 别让 epub2.js 的 selBar 委托 handler 也跑(它会 hideSel + 走它自己的分支)
        onPhrase();
        var sb = $('ep-sel'); if (sb) sb.classList.remove('open');
      });
      btns.appendChild(b);
    }

    // 呼吸高亮:把选区 range 在 iframe doc 里切片 wrap 成 <mark class="ep-phrase-hl breathe">(只包不增删可见字符)。
    // 跨多文字节点:reverse 处理保偏移有效。reflow 退化 PDF 的「绝对叠层」为原生 mark。
    var _phraseDoc = null;
    function unwrapPhrase() {
      contentsDocs().forEach(function (doc) {
        try {
          var ms = doc.querySelectorAll ? doc.querySelectorAll('mark.ep-phrase-hl[data-ep2="1"]') : [];
          Array.prototype.forEach.call(ms, function (mk) { var p = mk.parentNode; if (!p) return; while (mk.firstChild) p.insertBefore(mk.firstChild, mk); p.removeChild(mk); if (p.normalize) p.normalize(); });
        } catch (e) {}
      });
      _phraseDoc = null;
    }
    function wrapRangeBreathe(doc, range) {
      unwrapPhrase();
      var marks = [];
      try {
        var root = range.commonAncestorContainer;
        if (root.nodeType === 3) root = root.parentNode;
        var w = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT, null), n, nodes = [];
        while ((n = w.nextNode())) { try { if (range.intersectsNode(n)) nodes.push(n); } catch (e) {} }
        for (var k = nodes.length - 1; k >= 0; k--) {
          var node = nodes[k];
          var s = (node === range.startContainer) ? range.startOffset : 0;
          var e = (node === range.endContainer) ? range.endOffset : node.nodeValue.length;
          if (e <= s) continue;
          var after = node.splitText(s); after.splitText(e - s);
          var mk = doc.createElement('mark'); mk.className = 'ep-phrase-hl breathe'; mk.setAttribute('data-ep2', '1');
          after.parentNode.insertBefore(mk, after); mk.appendChild(after);
          marks.push(mk);
        }
      } catch (e) {}
      _phraseDoc = doc;
      return marks;
    }
    function phraseSetSolid() {   // 出结果 → 停呼吸转常亮(照搬 PDF solid=true)
      contentsDocs().forEach(function (doc) {
        try { var ms = doc.querySelectorAll('mark.ep-phrase-hl[data-ep2="1"]'); Array.prototype.forEach.call(ms, function (mk) { mk.classList.remove('breathe'); }); } catch (e) {}
      });
    }
    // 词组浮层(走共享层 RC.phrasepop;回调照搬 epub-html._openPhrasePopover,去掉高亮锚——本阶段无 EPUB 高亮)
    function mkResultOpts(kind, sentence) {
      return { kind: kind, aiParams: aiParams, ankiSource: function () { return { file: FREL, sentence: sentence || '', sourceUrl: location.origin + '/pdf/epub/view?file=' + encodeURIComponent(FREL) }; } };
    }
    function openPhrasePopover(text, rect, ctx) {
      if (!(window.RC && RC.phrasepop)) { if (window.RC && RC.result) RC.result.aiCall('/pdf/api/translate', { text: text, target_lang: '中文' }, '🌐 翻译', mkResultOpts('note', ctx)); return; }
      RC.phrasepop.show({
        text: text, rect: rect, file: FREL, langs: bookLangsArr(),
        onSolid: function () { phraseSetSolid(); },
        onFav: function (t, nowFav) { if (nowFav) unwrapPhrase(); refreshVocab(); },                                // 收藏→变分词单元:去呼吸 + 无条件重画下划线(favList 变了)
        onMastered: function () { loadPhraseMarks(refreshVocab); },                                                  // 标掌握→重载已掌握集 + 无条件重画(map 不变,refreshVocabUnderlinesForAllPages 的 keys 守卫会早返回→必须 refreshVocab)
        onExplain: function (t) { RC.result.aiCall('/pdf/api/explain', { text: t, context: ctx || '' }, '💡 AI 解释', mkResultOpts('explain', ctx)); }
      });
    }
    // 工具栏「📘词组」(照搬 epub-html.onPhrase):选区变持久呼吸 mark + 清原生选区 + 开词组浮层
    function onPhrase() {
      var s = _sel; if (!s || !s.text) return;
      injectCss(s.doc);
      var marks = wrapRangeBreathe(s.doc, s.range);
      try { s.win.getSelection().removeAllRanges(); } catch (e) {}
      var b = $('ep2-phrase-btn'); if (b) { b.style.display = 'none'; b.classList.remove('breathe'); }
      var r = (marks && marks.length) ? marks[0].getBoundingClientRect() : s.domRect;   // wrap 后用 mark 实位更准
      openPhrasePopover(s.text, rectInParent(s.doc, r), s.ctx);
    }

    // ════════════════════════════════════════════════════════════════════════
    // 6) 每个 iframe doc 的装饰编排 + 事件挂载(幂等)。
    // ════════════════════════════════════════════════════════════════════════
    // 选区轮询触发(iOS iframe 内 selectionchange/touchend 靠不住,补事件 + 轮询;同 epub2.js 思路)
    function attachSelTrack(doc) {
      if (!doc || doc.__ep2Sel) return; doc.__ep2Sel = 1;
      var fire = function () { setTimeout(updatePhraseBtn, 12); };
      doc.addEventListener('mouseup', fire, true);
      doc.addEventListener('touchend', fire, true);
      doc.addEventListener('selectionchange', function () { clearTimeout(attachSelTrack._t); attachSelTrack._t = setTimeout(updatePhraseBtn, 200); });
    }
    // 点呼吸 mark → 去高亮 + 重弹词组框(不建新 mark)。pointerdown/up「无位移 tap」(避 iOS click 不稳),照搬 epub-html
    function attachPhraseTap(doc) {
      if (!doc || doc.__ep2Tap) return; doc.__ep2Tap = 1;
      var dn = null;
      doc.addEventListener('pointerdown', function (e) { var mk = e.target.closest && e.target.closest('mark.ep-phrase-hl'); dn = mk ? { mk: mk, x: e.clientX, y: e.clientY } : null; }, true);
      doc.addEventListener('pointerup', function (e) {
        if (!dn) return; var d = dn; dn = null;
        var mk = e.target.closest && e.target.closest('mark.ep-phrase-hl');
        if (!mk || mk !== d.mk) return;
        if (Math.abs(e.clientX - d.x) > 8 || Math.abs(e.clientY - d.y) > 8) return;
        var txt = (mk.textContent || '').trim();
        var rect = rectInParent(doc, mk.getBoundingClientRect());
        var pblk = mk.parentElement && mk.parentElement.closest ? mk.parentElement.closest('p,li,td,blockquote,h1,h2,h3,h4,div') : null;
        var ctx = pblk ? (pblk.textContent || '').trim().slice(0, 1200) : '';
        unwrapPhrase();   // 先去高亮再重弹(不建新 mark)
        openPhrasePopover(txt, rect, ctx);
      }, true);
    }
    function decorate(doc) {
      if (!doc) return;
      injectCss(doc);
      attachSelTrack(doc);
      attachPhraseTap(doc);
      // 装饰推迟到空闲帧(不卡章节首屏渲染/滚动)
      (window.requestIdleCallback || function (f) { return setTimeout(f, 0); })(function () {
        try {
          if (vocabOn() && _vocabMap) vocabApply(doc);
          var afterJa = function () { try { if (rubyOn()) rubyApply(doc); } catch (e) {} };
          if (BOOK_LANGS.indexOf('ja') >= 0) jaWordApply(doc, afterJa); else afterJa();
        } catch (e) {}
      });
    }
    function decorateAll() { contentsDocs().forEach(decorate); }

    // ── 触发链:hooks.content.register + rendered + 已渲染立刻 + 开书初期反复 scan(修首屏竞态)──
    // 渲染钩子:优先走 RC.adapter().onContentRendered(阅读器无关);adapter 未就绪 → 回退直接挂 rendition
    var _adRendered = false;
    try { var _ad = window.RC && RC.adapter && RC.adapter(); if (_ad && _ad.onContentRendered) { _ad.onContentRendered(function (doc) { try { if (doc) decorate(doc); } catch (e) {} }); _adRendered = true; } } catch (e) {}
    if (!_adRendered) {
      try { R.hooks.content.register(function (contents) { try { if (contents && contents.document) decorate(contents.document); } catch (e) {} }); } catch (e) {}
      try { R.on('rendered', function (section, view) { try { if (view && view.contents && view.contents.document) decorate(view.contents.document); } catch (e) {} }); } catch (e) {}
    }
    decorateAll();
    (function scan(n) { decorateAll(); if (n < 20) setTimeout(function () { scan(n + 1); }, 300); })(0);

    // 选区轮询(驱动 📘词组 按钮显隐 + 快照;事件挂在各 iframe doc 上补 snappy)
    ensurePhraseBtn();
    setInterval(updatePhraseBtn, 380);

    // ════════════════════════════════════════════════════════════════════════
    // 7) 对外接口(给 epub2.js 设置面板回调 / wordpop onMastered 用)。
    // ════════════════════════════════════════════════════════════════════════
    window.__epubDeco = {
      bookLangs: bookLangsArr,            // 设置面板 getBookLangs 回填用
      saveLangs: window.saveLangPicker,   // 设置面板 onSaveLangs 用(= window.saveLangPicker)
      setVocabUnderline: setVocabUnderline, // 设置面板 onVocabUnderline 用
      refreshAll: decorateAll,            // 切语言/外部需要时重画所有已渲染 iframe
      refreshVocab: refreshVocab,
      optimisticMaster: optimisticMaster, // rc-wordpop「已掌握」乐观去下划线用
      toggleRuby: toggleRuby
    };

    // 启动:取本书语言 + 生词 map + 已掌握词组集(都 async;到了各自重画)
    loadBookLangs();
    loadVocabMap();
    loadPhraseMarks(refreshVocab);   // markSet 到了 → 无条件重画(把已掌握词组的下划线去掉)
  }
})();
