/* epub2-sentences.js — EPUB(epub.js 版)「生词句子系统」
 *   移植自 PDF 阅读器 reader.src/12-vocab-sentences.js（句子框 → L 按钮极简化的最终形态,task#179/180/181/190）。
 *
 * 功能(对照 PDF):
 *   · 未掌握词数 ≥ THRESH(3)的句子,句首 ⌐ + 句末 ⌟ 各一个 L 形小按钮。
 *   · 点 L → 该句整句中文翻译(/pdf/api/translate-sentence),就地覆盖在句子位置上显示译文;
 *     翻译中句子 hatch 框呼吸动画(hatchStrong),出结果停。再次点 L → 关闭(译文存内存+sidecar,再点即回放)。
 *   · L 长按 → 菜单(🔄 重新翻译 / 🗑 删除标记)。删除标记按本书 localStorage 持久(EPUB 无 sidecar)。
 *
 * ── 跟 PDF 的底座差异(为什么不能照搬绝对坐标) ──
 *   PDF 有固定页 + char bbox,L 按钮按 char 像素坐标绝对定位在页层里。
 *   EPUB 内容跑在各章节 <iframe> 里、reflow(本书 flow:'scrolled' 连续竖向滚动),没有固定页/char 层。
 *   → 照搬本仓库**已验证的 reflow-robust 范式**:epub2.js 的父文档分词浮层 `#ep-word-ov` / `_rebuildWordOv`
 *     —— 可点元素放在**父文档**的覆盖层里,用 findIframe 偏移把 iframe 内坐标换算到父视口,
 *        在 rendered / relocate / scroll / resize 时重建位置(跟着 reflow 走)。
 *   本模块对 iframe DOM **零改动**(只存活动 Range,读 getClientRects),因此跟 epub2-deco 的
 *   生词下划线 / ruby / 分词 .ep-w span(它在 iframe 内 wrap DOM)**互不干扰**:它管词级 span,我管句级覆盖层。
 *
 * ── 未掌握句判定(跟 epub2-deco 生词下划线同源口径) ──
 *   复用 /pdf/api/vocab-mastery-map(整本一次,返回 {word_lower:{label,mastery}},已 mastered 的不在表里)。
 *   分词 + 门控逐字照搬 epub2-deco.vocabTokens(BOOK_LANGS 决定 wantEn/wantJa;收藏词组算一个分词单元),
 *   一句里命中表的 token(=会被下划线的词)的不重复个数 ≥ THRESH 且句子够长 → 标 L。
 *
 * 只调现有端点:GET /pdf/api/vocab-mastery-map  +  POST /pdf/api/translate-sentence。不加后端端点。
 * 依赖:epub-reader.js(window.__epub.rendition) + epub2-deco.js(window.__epubDeco.bookLangs / refreshVocabUnderlinesForAllPages)。
 */
(function () {
  'use strict';
  function ready(fn) { if (window.__epub && window.__epub.rendition) fn(); else setTimeout(function () { ready(fn); }, 150); }
  ready(init);

  function init() {
    var R = window.__epub.rendition, CFG = window.__epub.cfg || {};
    var FREL = CFG.fileRel || '';

    // ── 可调参数 ──
    var THRESH = 3;      // 未掌握词数阈值(同 PDF _build_unmastered_sentences threshold)
    var MIN_WORDS = 10;  // 句子最少词数(逐字对齐 PDF min_words=10:按词不按字符;EN 词/JP token+字)
    var MAX_LEN = 600;   // 太长的不当一句(脏数据/无标点整段)

    // 句子颜色 palette(照搬 PDF SENT_COLORS):[stroke, fill],按句序轮替
    var SENT_COLORS = [
      ['#d97706', 'rgba(245,158,11,.18)'], ['#059669', 'rgba(16,185,129,.18)'],
      ['#2563eb', 'rgba(59,130,246,.18)'], ['#9333ea', 'rgba(168,85,247,.18)'],
      ['#db2777', 'rgba(236,72,153,.18)'], ['#0891b2', 'rgba(20,184,166,.18)']
    ];

    // ── 状态 ──
    var _vocabMap = null, _mapKeys = '';
    var _items = [];                 // [{doc, range(live), text, id, ci}]
    var _zh = {};                    // id(=norm text) → 译文,内存缓存(再点回放)
    var _translating = {};           // id → 正在翻译(呼吸)
    var _openId = null;              // 当前展开译文的句 id
    var _ov = null, _tov = null;   // 父文档覆盖层 / 译文覆盖盒
    var _hoverId = null;           // 当前悬停高亮的句 id(rebuild 时给该句 hatch 框续上 .highlight)
    var _dismissed = loadDismissed(); // {id:1} 本书已删标记的句

    function $(id) { return document.getElementById(id); }
    function toast(m) { try { if (window.RC && RC.toast) RC.toast(m); } catch (e) {} }
    function norm(t) { return String(t || '').replace(/\s+/g, ' ').trim().slice(0, 400); }
    function bookLangs() { try { return (window.__epubDeco && __epubDeco.bookLangs()) || []; } catch (e) { return []; } }
    function favList() { try { return (window.RC && RC.phrasepop && RC.phrasepop.favList) ? RC.phrasepop.favList() : []; } catch (e) { return []; } }

    function loadDismissed() {
      try { return JSON.parse(localStorage.getItem('ep2-sent-dismissed:' + FREL) || '{}') || {}; } catch (e) { return {}; }
    }
    function saveDismissed() { try { localStorage.setItem('ep2-sent-dismissed:' + FREL, JSON.stringify(_dismissed)); } catch (e) {} }

    // ════════════════════════════════════════════════════════════════════════
    // 1) 父文档样式注入(L 按钮 / 译文覆盖盒 / 长按菜单 都在父文档覆盖层里)
    // ════════════════════════════════════════════════════════════════════════
    (function injectCss() {
      if ($('ep2-sent-css')) return;
      var s = document.createElement('style'); s.id = 'ep2-sent-css';
      s.textContent = [
        '#ep2-sent-ov{position:fixed;left:0;top:0;right:0;bottom:0;pointer-events:none;z-index:44}',
        // L 形小角标(⌐ / ⌟):只画两条边,贴句首/句末;pointer-events:auto 才可点。border 4px(逐字对齐 PDF .vocab-sentence-btn-l)
        '.ep2-sl,.ep2-el{position:absolute;pointer-events:auto;cursor:pointer;box-sizing:border-box;background:transparent;' +
          '-webkit-tap-highlight-color:transparent;touch-action:manipulation;border-style:solid;border-width:0;border-color:currentColor;transition:background .12s,box-shadow .12s}',
        '.ep2-sl{border-left-width:4px;border-top-width:4px;border-top-left-radius:3px}',
        '.ep2-el{border-right-width:4px;border-bottom-width:4px;border-bottom-right-radius:3px}',
        '.ep2-sl:hover,.ep2-el:hover{background:var(--sent-fill,rgba(245,158,11,.10));box-shadow:0 0 0 1px var(--sent-fill,rgba(245,158,11,.3))}',
        '.ep2-sl.active,.ep2-el.active{background:var(--sent-fill,rgba(245,158,11,.10))}',   // active(对齐 PDF:fill 底 + inset box-shadow)
        '.ep2-sl.active{box-shadow:inset 4px 4px 0 0 currentColor}',
        '.ep2-el.active{box-shadow:inset -4px -4px 0 0 currentColor}',
        // 持久 hatch 句子框(逐字对齐 PDF .vocab-sentence-box):每行 rect 常驻淡 135° 斜纹;翻译中加深(hatchStrong)+呼吸
        '.ep2-sent-box{position:absolute;pointer-events:none;box-sizing:border-box;transition:all .12s;background-image:var(--sent-hatch);background-color:transparent;mix-blend-mode:multiply}',
        '.ep2-sent-box.translating{background-image:var(--sent-hatch-strong,var(--sent-hatch));animation:ep2-sent-breathe 1.1s ease-in-out infinite}',
        '.ep2-sent-box.highlight{background-color:var(--sent-fill,rgba(245,158,11,.08))}',
        '@keyframes ep2-sent-breathe{0%,100%{opacity:.3}50%{opacity:1}}',
        // 译文「就地覆盖」盒:盖在句子位置上,半透明纸色背景,中文文本自动换行
        '.ep2-sent-zh{position:absolute;pointer-events:auto;cursor:pointer;box-sizing:border-box;' +
          'background:rgba(246,243,234,.97);color:#1b1b1b;border-left:3px solid currentColor;border-radius:5px;' +
          'padding:5px 8px;font-size:14px;line-height:1.55;word-break:break-word;overflow:auto;' +
          'box-shadow:0 4px 18px rgba(0,0,0,.28);max-height:60vh}',
        '.ep2-sent-zh .ep2-zh-loading{color:#7a6a4a;font-style:italic}',
        // 长按菜单(照搬 PDF .sent-menu)
        '.ep2-sent-menu{position:fixed;z-index:260;background:#10162a;border:1px solid #3b6db5;border-radius:9px;' +
          'box-shadow:0 8px 26px rgba(0,0,0,.6);padding:5px;display:flex;flex-direction:column;gap:3px;min-width:130px}',
        '.ep2-sent-menu button{background:#1a2540;border:1px solid #2a3550;color:#cfe6ff;border-radius:6px;' +
          'padding:8px 12px;font-size:13px;text-align:left;cursor:pointer;white-space:nowrap}',
        '.ep2-sent-menu button:hover{background:#2c3e6a;border-color:#3b6db5}'
      ].join('\n');
      document.head.appendChild(s);
    })();

    function ensureOv() {
      if (_ov && _ov.parentNode) return _ov;
      _ov = document.createElement('div'); _ov.id = 'ep2-sent-ov';
      document.body.appendChild(_ov);
      return _ov;
    }

    // ════════════════════════════════════════════════════════════════════════
    // 2) 生词掌握度 map（复用 /pdf/api/vocab-mastery-map，客户端缓存）
    //    跟 epub2-deco 各自取一份(都被服务端/浏览器缓存,无害);拿到后重新切句。
    // ════════════════════════════════════════════════════════════════════════
    function loadVocabMap(cb) {
      fetch('/pdf/api/vocab-mastery-map?file=' + encodeURIComponent(FREL))
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d && d.ok) { _vocabMap = d.map || {}; _mapKeys = Object.keys(_vocabMap).sort().join(','); if (cb) cb(); }
        }).catch(function () {});
    }

    // 收藏词组里从 i 起找最长匹配(照搬 epub2-deco.favPhraseAt)
    function favPhraseAt(s, i, favs) {
      var best = null;
      for (var k = 0; k < favs.length; k++) {
        var ph = favs[k]; if (!ph) continue;
        var len = ph.length; if (len < 1 || i + len > s.length) continue;
        var seg = s.substr(i, len);
        if (seg === ph || seg.toLowerCase() === ph.toLowerCase()) { if (!best || len > best.end - best.start) best = { start: i, end: i + len }; }
      }
      return best;
    }
    // 一句里「未掌握词」不重复个数 + 总词数(分词/门控逐字照搬 epub2-deco.vocabTokens / PDF _flush_word:
    // push token 改「记到 set」计未掌握,同时按 PDF 口径累计 cur_total_words → 句长按词门控,非字符)。
    function scanSentence(s) {
      var map = _vocabMap; if (!map) return { unmastered: 0, words: 0 };
      var langs = bookLangs();
      var wantEn = langs.indexOf('en') >= 0, wantJa = langs.indexOf('ja') >= 0;
      if (!wantEn && !wantJa) return { unmastered: 0, words: 0 };
      var en = function (c) { return /[A-Za-z]/.test(c); }, ja = function (c) { return /[぀-ヿ㐀-鿿一-鿿]/.test(c); };
      var favs = wantJa ? favList() : [];
      var i = 0, L = s.length, seen = {}, words = 0;
      while (i < L) {
        if (favs.length) {
          var fh = favPhraseAt(s, i, favs);
          if (fh) { var key = s.slice(fh.start, fh.end); var info0 = map[key] || map[key.toLowerCase()]; if (info0 && info0.label) seen[key.toLowerCase()] = 1; words++; i = fh.end; continue; }
        }
        var c = s[i];
        if (wantEn && en(c)) {
          var j = i + 1; while (j < L && /[A-Za-z'’\-]/.test(s[j])) j++;
          var w = s.slice(i, j).toLowerCase();
          if (w.length > 2) { var info = map[w]; if (info && info.label) seen[w] = 1; }   // ≤2 字母功能词默认掌握,不计(同 PDF)
          if (w.length >= 2 || /^[a-z]+$/.test(w)) words++;   // 同 PDF _flush_word:len>=2 或 isalpha 计 1 词
          i = j; continue;
        }
        if (wantJa && ja(c)) {
          var hit = null, maxL = Math.min(12, L - i);
          for (var len = maxL; len >= 1; len--) { var inf = map[s.slice(i, i + len)]; if (inf && inf.label) { hit = { k: s.slice(i, i + len), end: i + len }; break; } }
          if (hit) { seen[hit.k.toLowerCase()] = 1; words++; i = hit.end; continue; }
          words++; i++; continue;   // JP 无客户端分词器:未命中按字计 1(token 近似,≥10 词门控)
        }
        i++;
      }
      var n = 0; for (var kk in seen) if (seen.hasOwnProperty(kk)) n++;
      return { unmastered: n, words: words };
    }

    // ════════════════════════════════════════════════════════════════════════
    // 3) 切句 + 判定(在某个 iframe doc 里):产出活动 Range（零 DOM 改动）
    //    句子边界 = . ! ? 。！？ + block 元素切换(不跨 <p>);跨多文字节点用同一个 Range 串起。
    // ════════════════════════════════════════════════════════════════════════
    function sentCandidates(root) {
      var doc = root.ownerDocument || root;
      var w = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT, null), n, out = [];
      while ((n = w.nextNode())) {
        var v = n.nodeValue; if (!v || !v.trim()) continue;
        var p = n.parentElement; if (!p) continue;
        // 跳过脚本/样式/代码/我们注入的 rt 读音(epub2-deco 的振假名)；标题(h1-h4)不当学习句
        if (p.closest && p.closest('script,style,code,pre,rt,h1,h2,h3,h4')) continue;
        out.push(n);
      }
      return out;
    }
    function blockOf(n) { var p = n.parentElement; return (p && p.closest) ? p.closest('p,li,td,blockquote,div,section,h1,h2,h3,h4,h5,h6') : null; }
    var TERM = /[.!?。！？]/;

    function removeItemsForDoc(doc) { _items = _items.filter(function (it) { return it.doc !== doc; }); }

    function detect(doc) {
      if (!doc || !doc.body || !_vocabMap) return;
      var langs = bookLangs();
      removeItemsForDoc(doc);
      if (langs.indexOf('en') < 0 && langs.indexOf('ja') < 0) return;   // 母语书 → 不标(同生词下划线门控)
      var nodes = sentCandidates(doc.body);
      if (!nodes.length) return;
      var buf = [];          // [{node, off, ch}]
      var curBlk = undefined;

      function flush() {
        if (buf.length) {
          // 去首尾空白,定位句首/句末非空字符
          var fi = 0, li = buf.length - 1;
          while (fi <= li && /\s/.test(buf[fi].ch)) fi++;
          while (li >= fi && /\s/.test(buf[li].ch)) li--;
          if (li >= fi) {
            var text = norm(buf.map(function (x) { return x.ch; }).join(''));
            var id = text;
            var sc = (text.length <= MAX_LEN && !_dismissed[id]) ? scanSentence(text) : { unmastered: 0, words: 0 };
            if (text.length <= MAX_LEN && !_dismissed[id] && sc.unmastered >= THRESH && sc.words >= MIN_WORDS) {
              try {
                var rng = doc.createRange();
                rng.setStart(buf[fi].node, buf[fi].off);
                rng.setEnd(buf[li].node, buf[li].off + 1);
                _items.push({ doc: doc, range: rng, text: text, id: id, ci: _items.length, count: sc.unmastered });
              } catch (e) {}
            }
          }
        }
        buf = [];
      }

      for (var k = 0; k < nodes.length; k++) {
        var node = nodes[k];
        var blk = blockOf(node);
        if (curBlk !== undefined && blk !== curBlk) flush();   // 跨 block → 断句
        curBlk = blk;
        var s = node.nodeValue;
        for (var i = 0; i < s.length; i++) {
          buf.push({ node: node, off: i, ch: s[i] });
          if (TERM.test(s[i])) {
            // 句末标点后吃掉紧跟的右引号/右括号(连进本句),遇空白/实义字符停
            var j = i + 1;
            while (j < s.length && /[”’"」』）)\]]/.test(s[j])) { buf.push({ node: node, off: j, ch: s[j] }); j++; }
            i = j - 1;
            flush();
          }
        }
      }
      flush();
    }

    function detectAll() { try { (R.getContents() || []).forEach(function (c) { if (c && c.document) detect(c.document); }); } catch (e) {} }

    // ════════════════════════════════════════════════════════════════════════
    // 4) 几何:把句子 Range 的 client rects 换算到父视口坐标(findIframe 偏移)
    // ════════════════════════════════════════════════════════════════════════
    function findIframe(doc) {
      try { if (doc.defaultView && doc.defaultView.frameElement) return doc.defaultView.frameElement; } catch (e) {}
      var ifrs = document.querySelectorAll('#ep-viewer iframe');
      for (var i = 0; i < ifrs.length; i++) { try { if (ifrs[i].contentDocument === doc) return ifrs[i]; } catch (e) {} }
      return null;
    }
    // 返回 {first, last, box} 父视口坐标;不可见/失败 → null
    function geomOf(it) {
      var ifr = findIframe(it.doc); if (!ifr) return null;
      var ib = ifr.getBoundingClientRect();
      var rects; try { rects = it.range.getClientRects(); } catch (e) { return null; }
      if (!rects || !rects.length) return null;
      var arr = [];
      for (var i = 0; i < rects.length; i++) { var r = rects[i]; if (r.width < 0.5 && r.height < 0.5) continue; arr.push(r); }
      if (!arr.length) return null;
      var f = arr[0], l = arr[arr.length - 1];
      var first = { left: ib.left + f.left, top: ib.top + f.top, right: ib.left + f.right, bottom: ib.top + f.bottom, h: f.height };
      var last = { left: ib.left + l.left, top: ib.top + l.top, right: ib.left + l.right, bottom: ib.top + l.bottom, h: l.height };
      var L = 1e9, T = 1e9, Rr = -1e9, Bb = -1e9, lines = [];
      for (var j = 0; j < arr.length; j++) {
        var r2 = arr[j];
        var ll = ib.left + r2.left, tt = ib.top + r2.top, rr = ib.left + r2.right, bb = ib.top + r2.bottom;
        L = Math.min(L, ll); T = Math.min(T, tt); Rr = Math.max(Rr, rr); Bb = Math.max(Bb, bb);
        lines.push({ left: ll, top: tt, right: rr, bottom: bb });   // 每行 rect(父视口坐标)→ 持久 hatch 框
      }
      return { first: first, last: last, box: { left: L, top: T, right: Rr, bottom: Bb }, lines: lines };
    }
    function inView(top, bottom) { var vh = window.innerHeight || 0; return bottom > -60 && top < vh + 60; }

    // ════════════════════════════════════════════════════════════════════════
    // 5) 重建 L 按钮(父覆盖层)—— 跟着 reflow / 滚动 / 翻页 重定位(镜像 epub2.js _rebuildWordOv)
    //    句级数量少(每屏几条),每次清空重建,简单且足够快。
    // ════════════════════════════════════════════════════════════════════════
    var _rbT = null;
    function scheduleRebuild() { clearTimeout(_rbT); _rbT = setTimeout(rebuild, 80); }

    function makeBracket(cls, it) {
      var b = document.createElement('div');
      b.className = cls;
      b.dataset.id = it.id;
      b.title = '翻译整句（含 ' + (it.count || 0) + ' 个未掌握词）：' + (it.text || '').slice(0, 80) + '…';   // 逐字对齐 PDF L title
      var col = SENT_COLORS[it.ci % SENT_COLORS.length];
      b.style.color = col[0];
      b.style.setProperty('--sent-fill', col[1]);   // active/hover 底色用 fill(对齐 PDF .vocab-sentence-btn-l)
      if (_openId === it.id) b.classList.add('active');
      b.__it = it;
      bindBracket(b, it);
      return b;
    }
    function rebuild() {
      var ov = ensureOv();
      // 清掉旧 L 按钮 + 旧 hatch 框(保留译文盒 _tov)
      var olds = ov.querySelectorAll('.ep2-sl, .ep2-el, .ep2-sent-box');
      for (var i = 0; i < olds.length; i++) olds[i].remove();
      // 剔除已卸载章节的 item
      var alive = [];
      try { alive = (R.getContents() || []).map(function (c) { return c.document; }); } catch (e) {}
      _items = _items.filter(function (it) { return alive.indexOf(it.doc) >= 0; });

      var ARM = 44;   // L 角标臂长(px,逐字对齐 PDF wantW=Math.max(charW,44))
      _items.forEach(function (it) {
        var g = geomOf(it); if (!g) return;
        // 持久 hatch 句子框:每行 rect 画一个常驻淡框(可见行才画;翻译中加深+呼吸,悬停 highlight)
        var col0 = SENT_COLORS[it.ci % SENT_COLORS.length], stroke = col0[0], fill = col0[1];
        var hatch = 'repeating-linear-gradient(135deg, ' + stroke + '55 0 1px, transparent 1px 4px)';
        var hatchStrong = 'repeating-linear-gradient(135deg, ' + stroke + '88 0 1.2px, transparent 1.2px 4px)';
        var translating = !!_translating[it.id], hovered = (_hoverId === it.id);
        (g.lines || []).forEach(function (ln) {
          if (!inView(ln.top, ln.bottom)) return;
          var box = document.createElement('div');
          box.className = 'ep2-sent-box' + (translating ? ' translating' : '') + (hovered ? ' highlight' : '');
          box.dataset.id = it.id;
          box.style.color = stroke;
          box.style.setProperty('--sent-fill', fill);
          box.style.setProperty('--sent-hatch', hatch);
          box.style.setProperty('--sent-hatch-strong', hatchStrong);
          box.style.left = ln.left + 'px';
          box.style.top = ln.top + 'px';
          box.style.width = Math.max(2, ln.right - ln.left) + 'px';
          box.style.height = Math.max(2, ln.bottom - ln.top) + 'px';
          ov.appendChild(box);
        });
        // 句首 ⌐:落在句首字外侧间隙,可见才画(翻页/滚动出屏的不画)
        if (inView(g.first.top, g.first.bottom)) {
          var b0 = makeBracket('ep2-sl', it);
          var fh = Math.max(12, Math.min(g.first.h, 30));
          b0.style.left = (g.first.left - 3) + 'px';
          b0.style.top = (g.first.top - 3) + 'px';
          b0.style.width = ARM + 'px';
          b0.style.height = fh + 'px';
          ov.appendChild(b0);
        }
        // 句末 ⌟:落在句末字外侧间隙
        if (inView(g.last.top, g.last.bottom)) {
          var b1 = makeBracket('ep2-el', it);
          var lh = Math.max(12, Math.min(g.last.h, 30));
          b1.style.left = (g.last.right + 3 - ARM) + 'px';
          b1.style.top = (g.last.bottom + 3 - lh) + 'px';
          b1.style.width = ARM + 'px';
          b1.style.height = lh + 'px';
          ov.appendChild(b1);
        }
      });
      // 译文盒跟随重定位(滚动 / reflow 后贴回句子位置);句子出屏 → 收起(缓存留着,再点回放)
      if (_openId) repositionOverlay();
    }

    // ════════════════════════════════════════════════════════════════════════
    // 6) L 按钮交互:短按 = 翻译/回放/关闭;长按 = 菜单;悬停 = 高亮该句
    // ════════════════════════════════════════════════════════════════════════
    function bindBracket(b, it) {
      // 悬停高亮
      b.addEventListener('mouseenter', function () { showHl(it); });
      b.addEventListener('mouseleave', function () { hideHl(); });
      // 长按检测(照搬 PDF _bindSentBtnLongPress):无位移 550ms → 菜单,触发后吃掉随后的 click
      var timer = null, x0 = 0, y0 = 0, fired = false;
      b.addEventListener('pointerdown', function (e) {
        x0 = e.clientX; y0 = e.clientY; fired = false;
        clearTimeout(timer);
        timer = setTimeout(function () {
          timer = null; fired = true;
          if (navigator.vibrate) { try { navigator.vibrate(30); } catch (_) {} }
          showMenu(b, it);
        }, 550);
      });
      var cancel = function (e) {
        if (timer && e && e.type === 'pointermove' && Math.hypot(e.clientX - x0, e.clientY - y0) < 12) return;
        clearTimeout(timer); timer = null;
      };
      b.addEventListener('pointermove', cancel);
      b.addEventListener('pointerup', cancel);
      b.addEventListener('pointercancel', cancel);
      b.addEventListener('click', function (e) {
        e.stopPropagation(); e.preventDefault();
        if (fired) { fired = false; return; }   // 长按已弹菜单 → 不再翻译
        toggleSentence(it);
      });
    }

    // 悬停高亮:给该句所有 hatch 框加 .highlight(逐字对齐 PDF mouseenter → .vocab-sentence-box.highlight)
    function showHl(it) {
      _hoverId = it.id;
      var bx = ensureOv().querySelectorAll('.ep2-sent-box');
      for (var i = 0; i < bx.length; i++) { if (bx[i].dataset.id === it.id) bx[i].classList.add('highlight'); }
    }
    function hideHl() {
      _hoverId = null;
      var bx = ensureOv().querySelectorAll('.ep2-sent-box.highlight');
      for (var i = 0; i < bx.length; i++) bx[i].classList.remove('highlight');
    }

    // 短按:已开 → 关;有缓存 → 直接画;否则现场翻
    function toggleSentence(it) {
      if (_openId === it.id) { closeOverlay(); return; }
      closeOverlay();
      if (_zh[it.id]) { _openId = it.id; drawOverlay(it); rebuild(); return; }
      translateSentence(it, false);
    }

    // 当前活动 Range 的 client rects(传后端 _tr_save_one:满足 sent.rects 门槛 → 持久句子标记 + 译文 sidecar)
    function rectsOf(it) {
      try {
        var rs = it.range.getClientRects(), out = [];
        for (var i = 0; i < rs.length; i++) { var r = rs[i]; if (r.width < 0.5 && r.height < 0.5) continue; out.push([Math.round(r.left), Math.round(r.top), Math.round(r.right), Math.round(r.bottom)]); }
        return out;
      } catch (e) { return []; }
    }
    function translateSentence(it, fresh) {
      _translating[it.id] = 1;
      rebuild();                                   // 句子框开始呼吸(hatchStrong)
      var body = { text: it.text };
      if (fresh) body.fresh = 1;
      // 带 file + sentence.rects → 后端 _tr_save_one 存 manual:True(译文/句子标记落 sidecar,刷新不丢)
      if (FREL) { body.file = FREL; body.sentence = { text: it.text, rects: rectsOf(it), count: it.count || 0 }; }
      fetch('/pdf/api/translate-sentence', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      }).then(function (r) { return r.json(); }).then(function (d) {
        delete _translating[it.id];
        if (d && d.ok && d.zh) {
          _zh[it.id] = d.zh; _openId = it.id; drawOverlay(it); rebuild();
          if (fresh) toast('已重新翻译');
        } else { rebuild(); toast('翻译失败：' + ((d && d.error) || '?')); }
      }).catch(function (e) { delete _translating[it.id]; rebuild(); toast('网络错误：' + e.message); });
    }

    // 译文「就地覆盖」盒:盖在句子 bounding box 上,显示中文
    function drawOverlay(it) {
      removeTov();
      var zh = _zh[it.id]; if (!zh) return;
      var g = geomOf(it); if (!g) return;
      var d = document.createElement('div'); d.className = 'ep2-sent-zh';
      d.dataset.id = it.id;
      var col = SENT_COLORS[it.ci % SENT_COLORS.length];
      d.style.color = col[0];
      d.style.left = g.box.left + 'px';
      d.style.top = g.box.top + 'px';
      d.style.width = Math.max(80, g.box.right - g.box.left) + 'px';
      d.style.minHeight = Math.max(g.first.h, g.box.bottom - g.box.top) + 'px';
      // 字号贴近原文行高(夹在 12.5–17 之间)
      var fs = Math.max(12.5, Math.min(17, g.first.h * 0.62));
      d.style.fontSize = fs.toFixed(1) + 'px';
      d.textContent = zh;   // 就地译文无 🇨🇳 前缀(逐字对齐 PDF _drawSentenceOverlay)
      d.addEventListener('click', function (e) { e.stopPropagation(); closeOverlay(); });
      ensureOv().appendChild(d); _tov = d;
    }
    function repositionOverlay() {
      var it = _items.filter(function (x) { return x.id === _openId; })[0];
      if (!it) { removeTov(); return; }   // 该句章节已卸载
      var g = geomOf(it);
      if (!g || !inView(g.box.top, g.box.bottom)) { removeTov(); return; }   // 滚出屏 → 收起(缓存留着)
      if (!_tov) { drawOverlay(it); return; }
      _tov.style.left = g.box.left + 'px';
      _tov.style.top = g.box.top + 'px';
      _tov.style.width = Math.max(80, g.box.right - g.box.left) + 'px';
    }
    function removeTov() { if (_tov && _tov.parentNode) _tov.parentNode.removeChild(_tov); _tov = null; }
    function closeOverlay() { removeTov(); _openId = null; var a = ensureOv().querySelectorAll('.active'); for (var i = 0; i < a.length; i++) a[i].classList.remove('active'); }

    // ── 长按菜单 ──
    function showMenu(b, it) {
      document.querySelectorAll('.ep2-sent-menu').forEach(function (m) { m.remove(); });
      var menu = document.createElement('div'); menu.className = 'ep2-sent-menu';
      menu.innerHTML = '<button type="button" data-act="re">🔄 重新翻译</button><button type="button" data-act="del">🗑 删除标记</button>';
      document.body.appendChild(menu);
      var r = b.getBoundingClientRect();
      menu.style.left = Math.max(6, Math.min(r.left, window.innerWidth - menu.offsetWidth - 6)) + 'px';
      menu.style.top = Math.min(r.bottom + 4, window.innerHeight - menu.offsetHeight - 6) + 'px';
      menu.addEventListener('click', function (e) {
        var btn = e.target.closest('button'); if (!btn) return;
        e.stopPropagation(); e.preventDefault();
        var act = btn.dataset.act; menu.remove();
        if (act === 're') { closeOverlay(); delete _zh[it.id]; translateSentence(it, true); }
        else if (act === 'del') dismiss(it);
      });
      setTimeout(function () {
        var close = function (ev) { if (!menu.contains(ev.target)) { menu.remove(); document.removeEventListener('pointerdown', close, true); } };
        document.addEventListener('pointerdown', close, true);
      }, 0);
    }
    function dismiss(it) {
      _dismissed[it.id] = 1; saveDismissed();   // 本书 localStorage(detect 时过滤)
      if (_openId === it.id) closeOverlay();
      delete _zh[it.id];
      _items = _items.filter(function (x) { return x.id !== it.id; });
      rebuild();
      // 服务端持久(对齐 PDF _sentDismiss):记 dismissed + 从译文 sidecar 删该句
      if (FREL && it.text) {
        fetch('/pdf/api/sentence-dismiss', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ file: FREL, text: it.text })
        }).catch(function () {});
      }
    }

    // ════════════════════════════════════════════════════════════════════════
    // 7) 触发链:渲染/翻页/滚动/缩放 重定位;rendered/hook 重切句;掌握度变了重切。
    // ════════════════════════════════════════════════════════════════════════
    var _detT = {};
    function scheduleDetect(doc) {
      if (!doc) return;
      // 推迟到空闲帧,让 epub2-deco 的 wrap 先跑完(切句只读文本不依赖它,但晚点切 Range 更稳)
      var key = (doc.body && doc.body.__ep2sKey) || (doc.body && (doc.body.__ep2sKey = 'd' + Math.random().toString(36).slice(2)));
      clearTimeout(_detT[key]);
      _detT[key] = setTimeout(function () { (window.requestIdleCallback || function (f) { return setTimeout(f, 0); })(function () { detect(doc); rebuild(); }); }, 200);
    }

    try { R.hooks.content.register(function (c) { try { if (c && c.document) scheduleDetect(c.document); } catch (e) {} }); } catch (e) {}
    try { R.on('rendered', function (sec, view) { try { if (view && view.contents && view.contents.document) scheduleDetect(view.contents.document); } catch (e) {} scheduleRebuild(); }); } catch (e) {}
    try { R.on('relocate', function () { repositionOverlay(); scheduleRebuild(); }); } catch (e) {}
    try { window.addEventListener('resize', scheduleRebuild); } catch (e) {}
    try { var vp = $('ep-viewer'); if (vp) vp.addEventListener('scroll', function () { hideHl(); scheduleRebuild(); }, true); } catch (e) {}

    // 掌握度变了(查词/标掌握后,rc-wordpop 调 window.refreshVocabUnderlinesForAllPages)→ 重取 map + 重切。
    // 包住 epub2-deco 已挂的实现:先跑它(刷生词下划线),再防抖刷我自己的句子。
    var _prevRefresh = window.refreshVocabUnderlinesForAllPages, _mapRefT = null;
    window.refreshVocabUnderlinesForAllPages = function () {
      try { if (typeof _prevRefresh === 'function') _prevRefresh.apply(this, arguments); } catch (e) {}
      clearTimeout(_mapRefT);
      _mapRefT = setTimeout(function () {
        loadVocabMap(function () {
          // map keys 没变(查的是已知词/掌握态没改)→ 不重切,避免闪烁
          if (Object.keys(_vocabMap).sort().join(',') === _lastDetKeys) return;
          _lastDetKeys = _mapKeys; detectAll(); rebuild();
        });
      }, 1000);
    };
    var _lastDetKeys = '';

    // 启动:取 map → 切句 → 重建;开书初期反复扫描(修首屏/异步装饰竞态,同 epub2-deco scan)
    loadVocabMap(function () { _lastDetKeys = _mapKeys; detectAll(); rebuild(); });
    (function scan(n) { if (_vocabMap) { detectAll(); rebuild(); } if (n < 14) setTimeout(function () { scan(n + 1); }, 420); })(0);

    // 对外（调试/将来设置开关用）
    window.__epubSentences = { detectAll: function () { detectAll(); rebuild(); }, rebuild: rebuild, items: function () { return _items; } };
  }
})();
