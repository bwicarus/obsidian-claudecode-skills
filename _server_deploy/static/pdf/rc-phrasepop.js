/* rc-phrasepop.js — 统一控制层:F6「词组」浮层(共享,PDF/EPUB 通用)。
 * 忠实移植自 PDF 阅读器 reader.src/15-phrase-wordpop.js 的
 *   showPhrasePopover / _phraseFav(收藏) / _wordPopMaster 的 phrase 分支(标掌握) /
 *   _renderPitch / _isJaWord,以及 _loadPhraseFavs。端点、字段、按钮文案、声调算法 —— 全部照搬。
 *
 * 自包含:复用 #word-pop 元素 + .wp-* 视觉(与 rc-wordpop 同款,本模块也自注入一份 guard,
 *   即便从未开过单词框也能正确显示);发音复用 rc-wordpop 暴露的 window._ttsWord / _speakOnline;
 *   _renderPitch / _isJaWord 本模块内重实现(逐字搬自 PDF,不跨模块取私有函数)。
 *
 * 与 PDF 的差异(reflow 适配,底座无关的退化,均不改 reader.src):
 *   · 定位:PDF 用 charBox;这里用 opts.rect(选区 viewport 矩形)+ _position(rect),#word-pop=fixed。
 *   · 呼吸高亮 / 收藏后乐观去下划线是字符层专属 → 退化成「底座回调」:onSolid(出结果转常亮)/
 *     onFav(收藏后底座去 mark + 刷新)/ onMastered(标掌握后底座刷新)。底座(epub-html.js)用原生
 *     <mark class="ep-phrase-hl"> 实现呼吸高亮(只包不增删可见字符,G0 偏移不漂)。
 *
 * 底座耦合全部走 opts(epub-html.js 在 onPhrase 传):
 *   opts.text         词组文本
 *   opts.rect         选区矩形(viewport,getBoundingClientRect 那种),用于定位
 *   opts.file         FREL,日语词组查 dict-jp 时带(本模块当前 dict-jp 按 word 全局查,file 预留)
 *   opts.langs        本书语言数组(决定英/日分流);EPUB 传 bookLangsArr() 让模块按声明 + 字符判英/日
 *   opts.onSolid()    出结果回调(查询返回后);底座停呼吸转常亮保持
 *   opts.onFav(text, nowFav)        收藏 toggle 成功后回调(底座:收藏→去 mark + 刷新分词/下划线)
 *   opts.onMastered(text, mastered) 标掌握 toggle 成功后回调(底座:刷新生词下划线)
 *   opts.onExplain(text)            💡 解释回调(底座:走 RC.result 解释模态)
 */
(function () {
  if (!window.RC) window.RC = {};
  var RC = window.RC;
  if (RC.phrasepop) return;

  var esc = function (s) { return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;'); };

  // ── 收藏词组 + 已掌握词组 全局 store(跟 PDF _phraseFavSet / _phraseMarkSet 一致;非书本相关)──
  var _favSet = new Set();
  var _markSet = new Set();
  var _norm = function (s) { return String(s || '').trim().replace(/\s+/g, ' ').toLowerCase(); };
  function loadFavs() {
    fetch('/pdf/api/phrases').then(function (r) { return r.json(); }).then(function (d) { if (d && d.ok) _favSet = new Set(d.phrases || []); }).catch(function () {});
    fetch('/pdf/api/phrase-mark').then(function (r) { return r.json(); }).then(function (d) { if (d && d.ok) _markSet = new Set(d.mastered || []); }).catch(function () {});
  }
  function favList() { return Array.from(_favSet); }   // 给底座 _vocabTokens 做最长匹配分词

  // ── CSS(.wp-* 逐字搬自 pdf_reader.html / rc-wordpop;guard:从未开过单词框也能用)──
  var _cssInjected = false;
  function injectCss() {
    if (_cssInjected) return; _cssInjected = true;
    if (document.getElementById('rc-wordpop-css') || document.getElementById('rc-phrasepop-css')) { _cssInjected = true; if (document.getElementById('rc-phrasepop-css')) return; }
    var css = document.createElement('style'); css.id = 'rc-phrasepop-css';
    css.textContent = [
      '#word-pop{position:fixed;display:none;background:#10162a;border:1px solid #3b6db5;border-radius:10px;box-shadow:0 8px 24px rgba(0,0,0,.6);z-index:200;width:min(340px,86vw);font-size:13px;color:#cfe6ff;max-height:80vh;overflow-y:auto;overflow-x:hidden}',
      '#word-pop .wp-head{display:flex;align-items:center;gap:8px;padding:11px 14px 7px;flex-wrap:wrap}',
      '#word-pop .wp-word{font-size:17px;font-weight:600;color:#fff}',
      '#word-pop .wp-phon{color:#a8cdff;font-style:italic;font-size:12px}',
      '#word-pop .wp-pitch{display:inline-flex;align-items:flex-end;gap:0;font-size:15px;color:#cfe6ff;padding-top:4px}',
      '#word-pop .wp-pitch .pm{position:relative;padding:3px 1px 0;line-height:1.1;border-top:2px solid transparent}',
      '#word-pop .wp-pitch .pm.hi{border-top:2px solid #6fd3ff;color:#dff1ff}',
      '#word-pop .wp-pitch .pm.drop::after{content:"";position:absolute;right:-1px;top:0;height:9px;border-right:2px solid #ff8a8a}',
      '#word-pop .wp-pitch .pm-type{margin-left:6px;font-size:10px;color:#5a6680;font-style:normal;align-self:center;border:1px solid #2a3450;border-radius:4px;padding:0 4px}',
      '#word-pop .wp-speak{background:transparent;border:1px solid #3b6db5;color:#a8cdff;border-radius:50%;width:26px;height:26px;cursor:pointer;font-size:12px;padding:0;flex-shrink:0}',
      '#word-pop .wp-speak:hover{background:#244470;color:#fff}',
      '#word-pop .wp-def{padding:7px 14px 11px;line-height:1.6;color:#cfe6ff;border-top:1px solid #1f2740}',
      '#word-pop .wp-actions{display:flex;gap:8px;padding:9px 14px;border-top:1px solid #1f2740;background:#0d1322;flex-wrap:wrap}',
      '#word-pop .wp-actions button{flex:1;background:#1a2540;border:1px solid #2a3550;color:#cfe6ff;border-radius:6px;padding:8px;cursor:pointer;font-size:12px}',
      '#word-pop .wp-actions button:hover{border-color:#3b6db5}',
      '#word-pop .wp-anki{background:#13351f !important;border-color:#34d399 !important;color:#7ee2b8 !important}'
    ].join('\n');
    document.head.appendChild(css);
  }

  // ── 日语判定(照搬 15-phrase-wordpop.js::_isJaWord:含假名→是;含汉字按本书声明,未声明默认日语)──
  function _isJaWord(w) {
    if (/[぀-ヿ]/.test(w)) return true;
    if (!/[㐀-鿿]/.test(w)) return false;
    var declared = (_ctx.langs || []).length > 0;
    return declared ? _ctx.langs.indexOf('ja') >= 0 : true;
  }
  // ── 日语声调(ピッチアクセント),逐字搬自 15-phrase-wordpop.js::_renderPitch ──
  function _renderPitch(reading, accent) {
    var small = 'ぁぃぅぇぉゃゅょゎァィゥェォャュョヮ';
    var mora = [];
    for (var ci = 0; ci < reading.length; ci++) {
      var ch = reading[ci];
      if (small.indexOf(ch) >= 0 && mora.length) mora[mora.length - 1] += ch;
      else mora.push(ch);
    }
    var n = mora.length;
    if (!n) return '';
    var hi = function (i) {
      if (accent === 0) return i >= 1;
      if (accent === 1) return i === 0;
      return i >= 1 && i < accent;
    };
    var html = '<span class="wp-pitch" title="声调型 ' +
      (accent === 0 ? '平板' : accent === 1 ? '頭高' : '第' + accent + '拍后降') + '">';
    for (var i = 0; i < n; i++) {
      var h = hi(i);
      var drop = (accent >= 1 && i + 1 === accent);
      html += '<span class="pm' + (h ? ' hi' : '') + (drop ? ' drop' : '') + '">' + mora[i] + '</span>';
    }
    var tlabel = accent === 0 ? '平板' : accent === 1 ? '頭高' : '[' + accent + ']';
    html += '<span class="pm-type">' + tlabel + '</span></span>';
    return html;
  }

  // ── 定位(rc-wordpop _positionPop 套路:左右钳进视口、放不下翻上、顶≥54)──
  function _position(pop, rect) {
    if (!pop) return;
    if (!rect) { pop.style.left = Math.max(8, (window.innerWidth / 2 - 170)) + 'px'; pop.style.top = '70px'; return; }
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
  // 框外点击 → 关小框(pointerdown capture;刚弹 400ms 内豁免)。rc-wordpop 已注册同款;本模块也注册一份
  // 以防本模块单独使用(两份都只是把 #word-pop 藏掉,幂等无害)。
  document.addEventListener('pointerdown', function (e) {
    var p = document.getElementById('word-pop');
    if (!p || p.style.display !== 'block') return;
    if (Date.now() - (window._wordPopOpenAt || 0) < 400) return;
    if (!p.contains(e.target)) p.style.display = 'none';
  }, true);

  // ── 模块状态 ──
  var _state = null;   // {text, jp, reading, mastered}
  var _ctx = {};       // 当前 show 的 opts(底座回调 + langs)

  // 喇叭:读当前词组(日语念假名读音,否则有道/英语)。复用 rc-wordpop 暴露的 window._ttsWord / _speakOnline。
  window._epPhraseSpeak = function () {
    var s = _state; if (!s) return;
    if (s.reading && window._ttsWord) { window._ttsWord(s.reading, 'ja-JP'); return; }
    if (s.text && window._speakOnline) window._speakOnline(s.text);
  };
  // ☆ 收藏为词组(照搬 15-phrase-wordpop.js::_phraseFav):POST/DELETE /pdf/api/phrases。
  window._epPhraseFav = function (btn) {
    var s = _state; if (!s || !s.text) return;
    var t = s.text;
    var has = _favSet.has(t);
    if (btn) btn.disabled = true;
    fetch('/pdf/api/phrases', {
      method: has ? 'DELETE' : 'POST',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: t })
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.ok) {
        _favSet = new Set(d.phrases || []);
        var nowFav = _favSet.has(t);
        if (btn) { btn.disabled = false; btn.textContent = nowFav ? '★ 已收藏' : '☆ 收藏为词组'; btn.classList.toggle('wp-anki', nowFav); }
        RC.toast(nowFav ? '已收藏，之后会作为一个词分词' : '已取消收藏');
        try { if (_ctx.onFav) _ctx.onFav(t, nowFav); } catch (_) {}
      } else if (btn) { btn.disabled = false; }
    }).catch(function () { if (btn) btn.disabled = false; });
  };
  // ☆ 标记掌握(照搬 15-phrase-wordpop.js::_wordPopMaster 的 phrase 分支):POST /pdf/api/phrase-mark。
  // PDF 此处有「乐观去下划线 + 失败回滚」(字符层专属) → reflow 退化成「POST + onMastered 刷新」。
  window._epPhraseMaster = function (btn) {
    var s = _state; if (!s) return;
    var next = !s.mastered;
    var t = s.text;
    if (btn) btn.disabled = true;
    fetch('/pdf/api/phrase-mark', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: t, mark: next ? 'mastered' : '' })
    }).then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.ok === false) throw new Error(d.error || 'fail');
      s.mastered = next;
      _markSet = new Set(d.mastered || []);
      if (btn) {
        btn.disabled = false;
        btn.textContent = s.mastered ? '✓ 已掌握 100' : '☆ 标记掌握';
        btn.title = s.mastered ? '点击取消掌握（恢复词组下划线）' : '标记掌握 100（该词组不再标生词下划线）';
        btn.classList.toggle('wp-anki', s.mastered);
      }
      RC.toast(s.mastered ? '已掌握，下划线消失' : '已取消掌握');
      try { if (_ctx.onMastered) _ctx.onMastered(t, s.mastered); } catch (_) {}
    }).catch(function () { if (btn) btn.disabled = false; RC.toast('标记失败'); });
  };
  // 💡 解释:藏框 → 底座 onExplain(走 RC.result 解释模态)。
  window._epPhraseExplain = function () {
    var p = document.getElementById('word-pop'); if (p) p.style.display = 'none';
    if (_ctx.onExplain) { try { _ctx.onExplain(_state && _state.text); } catch (_) {} }
  };

  // ── 入口:RC.phrasepop.show(opts)。逐字照搬 15-phrase-wordpop.js::showPhrasePopover 的内容/字段/端点 ──
  function show(opts) {
    opts = opts || {};
    injectCss();
    var text = String(opts.text == null ? '' : opts.text).trim();
    if (!text) return;
    _ctx = opts;
    var pop = _ensurePop();
    _state = { text: text, jp: false, reading: '', mastered: _markSet.has(_norm(text)) };
    pop.style.display = 'block';
    window._wordPopOpenAt = Date.now();
    pop.innerHTML = '<div style="padding:14px;color:#8a9bb4">⏳ 处理词组…</div>';
    _position(pop, opts.rect);
    var isJa = _isJaWord(text);
    _state.jp = isJa;   // 掌握按钮按语言分流(PDF 同)。词组掌握统一走 phrase-mark,这里仅记录。
    (function () {
      var zh = '', reading = '', accent = null;
      var fin = function () {
        // 出结果 → 底座停呼吸转常亮保持(点高亮才消失)。照搬 PDF：solid=true。
        try { if (_ctx.onSolid) _ctx.onSolid(); } catch (_) {}
        _state.reading = reading;
        var phon = (isJa && reading && accent != null) ? _renderPitch(reading, accent)
          : (reading ? '<span class="wp-phon">' + esc(reading) + '</span>' : '');
        var fav = _favSet.has(text);
        pop.innerHTML =
          '<div class="wp-head"><span class="wp-word">' + esc(text) + '</span>' + phon +
          (reading ? '<button class="wp-speak" onclick="_epPhraseSpeak()" title="发音">🔊</button>' : '') + '</div>' +
          '<div class="wp-def">' + (zh ? esc(zh) : '<span style="color:#8a9bb4">（无翻译）</span>') + '</div>' +
          '<div class="wp-actions">' +
          '<button id="ep-phrase-fav-btn" class="' + (fav ? 'wp-anki' : '') + '" onclick="_epPhraseFav(this)">' +
          (fav ? '★ 已收藏' : '☆ 收藏为词组') + '</button>' +
          '<button id="ep-phrase-master-btn" class="' + (_state.mastered ? 'wp-anki' : '') + '" onclick="_epPhraseMaster(this)" title="' + (_state.mastered ? '点击取消掌握（恢复词组下划线）' : '标记掌握 100（该词组不再标生词下划线）') + '">' +
          (_state.mastered ? '✓ 已掌握 100' : '☆ 标记掌握') + '</button>' +
          '<button onclick="_epPhraseExplain()" title="详细解释这个词组">💡 解释</button>' +
          '</div>';
        _position(pop, opts.rect);   // 内容定型后再夹一次进视口
      };
      (async function () {
        try {
          if (isJa) {
            var dj = await (await fetch('/pdf/api/dict-jp?word=' + encodeURIComponent(text))).json();
            if (dj && dj.ok) { zh = dj.zh || ''; reading = dj.reading || ''; accent = (dj.accent != null ? dj.accent : null); }
          }
          if (!zh) {
            var dt = await (await fetch('/pdf/api/translate-sentence', {
              method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: text })
            })).json();
            if (dt && dt.ok) zh = dt.zh || '';
          }
        } catch (_) {}
        fin();
      })();
    })();
  }

  RC.phrasepop = { show: show, loadFavs: loadFavs, favList: favList };
  loadFavs();
})();
