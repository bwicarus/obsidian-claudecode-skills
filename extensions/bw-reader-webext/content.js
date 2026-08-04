// 统一选区工具条：普通网页与 PWA 共用同一套 chrome/结果卡；PDF 几何与持久化走桥接白名单。
(() => {
  'use strict';
  if (window.__bwPwaProviderOnly) return;
  if (window.top !== window) return;
  const RC = window.RC, root = window.__bwRoot, shadow = window.__bwShadow;
  if (!RC || !root || !shadow) return;
  const pwa = window.__bwPwaBridge || null;
  function pwaCapability(name) {
    return !!(pwa && pwa.state && pwa.state.capabilities && pwa.state.capabilities[name]);
  }
  let snapshot = null, locked = false, timer = null, _suppressBar = 0;   // _suppressBar:lookupWord(单击词直查)后短暂抑制 SELECTION 弹工具条(用户 2026-07-22:接管模式单击词只查词、不再多弹工具条)
  const COLORS = ['#fff59d', '#a7f3d0', '#a3d4ff', '#fda4af'];   // 兜底默认(RC.settings 不可用/未配时)
  // 高亮色板复用统一设置:RC.settings.hlColors()(读 eph-hl-colors,设置面板「高亮」tab 增删,经 settings-sync 桥全站一致)
  function _hlColors() { try { return (RC.settings && RC.settings.hlColors && RC.settings.hlColors()) || COLORS; } catch (e) { return COLORS; } }
  function _hlSwatchHtml() { return _hlColors().map(c => `<span class="bw-hl-swatch" data-color="${c}" style="background:${c}" title="用此色标记"></span>`).join(''); }

  const style = document.createElement('style');
  style.id = 'bw-selection-css';
  style.textContent = `
#bw-sel-toolbar{position:fixed;display:none;background:var(--rc-bg-raised,#1a2540);border:1px solid var(--rc-border-accent,#3b6db5);border-radius:var(--rc-radius-md,8px);padding:6px;gap:8px;z-index:1000;box-shadow:var(--rc-shadow-float,0 6px 16px rgba(0,0,0,.6));flex-direction:row;align-items:flex-start;width:max-content;max-width:min(480px,calc(100vw - 16px));font:12px/1.4 var(--rc-font-ui);color:var(--rc-text-strong,#cfe6ff)}
#bw-sel-toolbar.open{display:flex}#bw-sel-main{display:flex;flex-direction:column;gap:6px;flex:1;min-width:0}
#bw-sel-preview{color:#a8cdff;font-size:11px;line-height:1.5;background:rgba(0,0,0,.3);border-radius:4px;padding:5px 9px;max-height:54px;overflow:hidden;word-break:break-word;border-left:2px solid #60a5fa}
#bw-sel-preview .len{color:var(--rc-text-dim,#7a8497);font-size:10px;margin-left:6px}.bw-sel-btns{display:flex;gap:4px;flex-wrap:wrap}
#bw-sel-toolbar button{background:transparent;border:none;color:var(--rc-text-strong,#cfe6ff);cursor:pointer;padding:6px 10px;border-radius:var(--rc-radius-xs,4px);font:inherit;white-space:nowrap;-webkit-tap-highlight-color:transparent}
#bw-sel-toolbar button:hover{background:var(--rc-bg-hover,#2c3e6a);color:#fff}.bw-sel-btns.multi button{padding:8px 13px;font-size:13px}
#bw-hl-colors{display:flex;flex-direction:column;gap:6px;align-items:center;padding:2px 8px 2px 2px;flex-shrink:0;border-right:1px solid var(--rc-border,#2a3550)}
#bw-hl-colors .lbl{font-size:12px}.bw-hl-swatch{width:17px;height:17px;border-radius:50%;border:2px solid #5b6a85;cursor:pointer;transition:.12s}
.bw-hl-swatch:hover{border-color:#fff;transform:scale(1.08)}#bw-sel-x{position:absolute;right:-7px;top:-9px!important;padding:0!important;width:20px;height:20px;border-radius:50%!important;background:#293753!important;border:1px solid #52668d!important;color:#cfe6ff!important;line-height:18px!important}
@media(max-width:600px){#bw-sel-toolbar{max-width:calc(100vw - 12px)}.bw-sel-btns.multi button{padding:7px 9px;font-size:12px}}
`;
  window.__bwHead.appendChild(style);

  const bar = document.createElement('div');
  bar.id = 'bw-sel-toolbar';
  bar.innerHTML = `
    <div id="bw-hl-colors"><span class="lbl">🖌</span>${_hlSwatchHtml()}</div>
    <div id="bw-sel-main"><div id="bw-sel-preview">—</div><div class="bw-sel-btns" id="bw-sel-actions"></div></div>
    <button id="bw-sel-x" title="关闭">×</button>`;
  root.appendChild(bar);
  const preview = bar.querySelector('#bw-sel-preview'), actions = bar.querySelector('#bw-sel-actions');

  function adapter() { try { return RC.adapter && RC.adapter(); } catch (_) { return null; } }
  function hostAction(name, payload, fallback) { return RC.actions.run(name, payload || {}, fallback); }
  function langHint() {
    const l = String(document.documentElement.lang || '').toLowerCase();
    if (l.startsWith('ja')) return ['ja']; if (l.startsWith('en')) return ['en']; return [];
  }
  // 目标语言集合(= web-immersive 同键 bw-set-target-langs,经 settings-sync 桥全站一致;默认 en+ja)
  function _targets() { try { const v = JSON.parse(localStorage.getItem('bw-set-target-langs') || '["en","ja"]'); return (v && v.length) ? v : ['en', 'ja']; } catch (e) { return ['en', 'ja']; } }
  // 母语抑制(照 PDF reader.js isNativeHan):纯汉字、无假名无拉丁的词,若既不学日语也不要中文 → 视为母语,单击不弹查词框
  function _isNativeHanWord(w) {
    if (!/[一-鿿]/.test(w) || /[぀-ヿ]/.test(w) || /[A-Za-z]/.test(w)) return false;
    const tg = _targets(); return tg.indexOf('ja') < 0 && tg.indexOf('zh') < 0;
  }
  // 选区 kind 判定:拉丁靠空格,CJK/假名无空格靠 Intl.Segmenter 数词段(整段恰好一个词=word,
  //   否则 multi)。旧逻辑只看 /\s/ → 日语拖选多字无空格恒判 word,词组按钮不出、还当单词查(bug ②③根因)。
  function _selKind(text) {
    const t = String(text == null ? '' : text).trim();
    if (!t) return 'word';
    if (/\s/.test(t)) return 'multi';   // 拉丁多词有空格
    try {
      if (window.Intl && Intl.Segmenter) {
        const sg = new Intl.Segmenter(undefined, { granularity: 'word' });
        let count = 0, whole = false;
        for (const seg of sg.segment(t)) {
          count++;
          if (seg.index === 0 && seg.segment.length === t.length) whole = true;
          if (count > 1) break;
        }
        if (whole) return 'word';         // 整段就是一个词段(单个日语词/单个英文词)
        if (count > 1) return 'multi';    // 跨了词边界=词组
      }
    } catch (_) {}
    return (t.length > 1 && /[぀-ヿ㐀-鿿一-鿿]/.test(t)) ? 'multi' : 'word';   // 无 Segmenter 兜底
  }
  function normalize(s) {
    if (!s || !s.text) return null;
    const rect = s.rect?.client || s.rect || null;
    return {
      text: String(s.text).trim().slice(0, 5000), context: String(s.context || s.sentence || '').slice(0, 2000),
      sentence: String(s.sentence || s.context || ''), rect, anchor: s.anchor || null,
      page: Number(s.page || s.anchor?.page || 0), file: s.file || adapter()?.fileInfo?.().file || ('web:' + location.href.split('#')[0]),
      langs: s.langs || adapter()?.fileInfo?.().langs || langHint(), kind: s.kind || _selKind(s.text),
      shortPhrase: s.shortPhrase != null ? !!s.shortPhrase : String(s.text).trim().length <= 80
    };
  }
  function captureWeb() { return normalize(adapter()?.captureSelection?.()); }
  function position(rect) {
    bar.classList.add('open');
    if (RC.ui?.placeSelectionToolbar) { RC.ui.placeSelectionToolbar(bar, rect, {gap:8}); return; }
    const br = bar.getBoundingClientRect(), gap = 8, vw = innerWidth, vh = innerHeight;
    let left = rect ? rect.left : (vw - br.width) / 2, top = rect ? rect.bottom + gap : vh - br.height - 18;
    left = Math.max(6, Math.min(left, vw - br.width - 6)); if (top + br.height > vh - 6 && rect) top = rect.top - br.height - gap;
    bar.style.left = left + 'px'; bar.style.top = Math.max(6, Math.min(top, vh - br.height - 6)) + 'px';
  }
  function button(label, action, title) {
    const b = document.createElement('button'); b.textContent = label; b.dataset.action = action; if (title) b.title = title; actions.appendChild(b);
  }
  function renderActions() {
    actions.innerHTML = '';
    const word = snapshot.kind === 'word';
    const colorRail = bar.querySelector('#bw-hl-colors');
    if (colorRail) colorRail.style.display =
      pwa && !(pwaCapability('highlight') || pwaCapability('pdfHighlight'))
        ? 'none' : '';
    actions.classList.toggle('multi', !word);
    if (!word && snapshot.shortPhrase) button('📘 词组', 'phrase');
    button('📋 复制', 'copy');
    if (word) button('🔍 查词', 'lookup');
    if (pwaCapability('selectionOcr')) button('🔎 OCR', 'ocr', 'PDF 文字层有误时重新识别选区');
    if (!word) { button('🌐 翻译', 'translate'); button('💡 解释', 'explain'); button('💬 对话', 'chat'); }
    button('🎴 制卡', 'anki'); button('📝 笔记', 'note');
    if (!word) button('📊 语法', 'grammar');
    button('🔍 搜索', 'search');
  }
  function show(s) {
    s = normalize(s); if (!s || !s.text) { if (!locked) hide(false); return; }
    snapshot = s; locked = false;
    preview.innerHTML = RC.esc(snapshot.text.slice(0, 260)) + (snapshot.text.length > 260 ? '…' : '') + `<span class="len">${snapshot.text.length} 字</span>`;
    renderActions(); position(snapshot.rect);
  }
  function hide(clear) {
    bar.classList.remove('open'); locked = false;
    if (clear) { snapshot = null; adapter()?.clearSelection?.(); }
  }
  function resultOpts(kind) {
    const snap = snapshot;
    return {
      kind: kind || 'note', draftKey: 'bw-extension-drafts', aiParams: () => ({}),
      markHighlight: (_text, body, sentence, k) => {
        return hostAction('highlight.save', { color: COLORS[0], body, sentence, kind: k || kind || 'note' }, () => {
          RC.toast('当前宿主尚未提供持久高亮'); return false;
        });
      },
      ankiSource: () => ({ file: snap?.file || '', sentence: snap?.sentence || '', sourceUrl: location.href })
    };
  }
  // ── 查词当前词高亮(网页版补;PDF 是字符层呼吸高亮,rc-wordpop.js 明写网页退化成内联占位)──
  //   用 CSS Highlight API 圈住被查的词:不改宿主 DOM、随文档流滚动天然零漂移(与 web-immersive
  //   生词下划线 ::highlight(rc-vocab-*) 同机制)。落点选扩展 content.js(网页专属、副作用最小):
  //   PDF/EPUB 各有自己的当前词高亮宿主,共享层不掺;CSS 注进**真实页 head**(高亮的文字节点在真实页)。
  const _WP_HL = !pwa && !!(window.Highlight && window.CSS && CSS.highlights);
  function _injectWpHlCss() {
    if (!_WP_HL || document.getElementById('rc-wordpop-current-css')) return;
    const st = document.createElement('style'); st.id = 'rc-wordpop-current-css';
    // 颜色对齐 PDF 查词呼吸高亮(rc-wordpop.js .rc-wp-breathe 青/teal rgba(56,178,172,*))。
    // ::highlight 伪元素跨浏览器不支持 animation → 取 PDF「呼吸 1.6s 后转常亮」的常亮态(纯色保持)。
    st.textContent = '::highlight(rc-wordpop-current){background-color:rgba(56,178,172,.34);color:inherit}';
    (document.head || document.documentElement).appendChild(st);
  }
  function clearCurWord() {
    if (!window.CSS || !CSS.highlights) return;
    try { CSS.highlights.delete('rc-wordpop-current'); } catch (_) {}
  }
  function setCurWord(range) {
    if (!_WP_HL || !range) { clearCurWord(); return; }
    clearCurWord(); _injectWpHlCss();
    // 清除时机全交给下方 pointerdown-outside(与 word-pop「框外关闭」同一手势、同一 400ms 豁免),
    // 高亮生命周期跟着小框走。**不**观察 #word-pop 的 display:小框慢词路径开局即 none、异步才弹,
    // 观察 display!==block 会在框还没弹出时就误清高亮(实测 set 后立刻被 del)。
    try { CSS.highlights.set('rc-wordpop-current', new Highlight(range)); } catch (_) {}
  }

  function lookup(s, range) {
    s = normalize(s || snapshot); if (!s) return;
    _suppressBar = Date.now() + 500;   // 单击词直查 → 抑制紧随而来的 SELECTION,否则工具条被弹回来=双弹(用户实测)
    RC.wordpop.show({ word: s.text, rect: s.rect, ctx: s.context, file: s.file, page: s.page, langs: s.langs,
      showAnki: true, onGrammar: () => run('grammar'), onFallback: () => translate(s) });
    setCurWord(range);   // 网页版:高亮被查的当前词(CSS Highlight);PWA/无 range 时 setCurWord 只清不设
    hide(false);
  }
  function phrase(s) {
    s = normalize(s || snapshot); if (!s) return;
    RC.phrasepop.show({ text: s.text, rect: s.rect, file: s.file, langs: s.langs,
      onExplain: () => explain(s), onSolid: () => {}, onFav: () => {}, onMastered: () => {} });
    hide(false);
  }
  function translate(s) { s = normalize(s || snapshot); if (s) { hide(false); RC.result.aiCall('/pdf/api/translate', { text:s.text, target_lang:'中文' }, '🌐 翻译', resultOpts('translate')); } }
  function explain(s) { s = normalize(s || snapshot); if (s) { hide(false); RC.result.aiCall('/pdf/api/explain', { text:s.text, context:s.context }, '💡 AI 解释', resultOpts('explain')); } }
  function chat(s) { s = normalize(s || snapshot); if (s) { hide(false); if (!(RC.ui && RC.ui.openSelectionChat && RC.ui.openSelectionChat(s.text, s.context))) RC.result.openChat(s.text, s.context, resultOpts('chat')); } }
  function showCard(head, sub) {
    RC.result.openResult(head, sub || '', '<div id="bw-snip-card"></div>');
    return shadow.getElementById('bw-snip-card');
  }
  function note(s) {
    s = normalize(s || snapshot); if (!s) return;
    RC.snippets.toNote({ text:s.text, file:s.file, page:s.page, getNoteName:() => prompt('请输入笔记名（不含 .md）：', ''), showCard }); hide(false);
  }
  function anki(s) {
    s = normalize(s || snapshot); if (!s) return;
    RC.snippets.toAnki({ text:s.text, file:s.file, source:s.sentence || s.text, showCard }); hide(false);
  }
  function grammar(s) {
    s = normalize(s || snapshot); if (!s || !RC.grammar) return;
    let pane = shadow.getElementById('bw-grammar-list');
    if (!pane) { RC.toast('语法面板尚未就绪'); return; }
    // file 用全局语法身份(不 per-URL)：KG 启用="我在学哪些语法点"的跨站集合，须与设置面板 renderTrackList 同 file；
    // viewModeKey 统一 eph-grammar-view(= 设置面板 select 键，且经 settings-sync 桥跨站同步；旧 bw-ext-* 键不同步、设置改了不生效)。
    RC.grammar.analyze({ text:s.text, sentence:s.sentence || s.context || s.text, file:'web:__grammar__', container:pane,
      onOpenPanel:() => RC.sidedrawer.open('grammar'), sourceUrl:() => location.href, viewModeKey:'eph-grammar-view' });
    hide(false);
  }
  async function run(name, s) {
    s = normalize(s || snapshot);
    if (!s && name !== 'close') return;
    if (name === 'copy') { try { await navigator.clipboard.writeText(s.text); RC.toast('已复制'); } catch (_) { RC.toast('复制失败'); } }
    else if (name === 'lookup') lookup(s); else if (name === 'phrase') phrase(s);
    else if (name === 'translate') translate(s); else if (name === 'explain') explain(s); else if (name === 'chat') chat(s);
    else if (name === 'anki') anki(s); else if (name === 'note') note(s); else if (name === 'grammar') grammar(s);
    else if (name === 'search') window.open('https://www.bing.com/search?q=' + encodeURIComponent(s.text), '_blank');
    else if (name === 'ocr' && pwaCapability('selectionOcr')) { try { const r = await pwa.local('ocr'); show(r?.selection); } catch (e) { RC.toast(e.message); } }
  }

  actions.addEventListener('click', (e) => { const b = e.target.closest('button[data-action]'); if (b) run(b.dataset.action); });
  bar.querySelector('#bw-sel-x').addEventListener('click', () => hide(true));
  bar.addEventListener('pointerdown', () => { locked = true; });
  // 事件委托到容器(而非逐个 swatch):设置里增删颜色后重渲 innerHTML 仍生效,无需重绑
  bar.querySelector('#bw-hl-colors').addEventListener('click', async (e) => {
    const sw = e.target.closest('.bw-hl-swatch'); if (!sw || !snapshot) return; const color = sw.dataset.color;
    try {
      await hostAction('highlight.save', { color }, () => { throw new Error('这个页面暂时无法建立稳定高亮锚点'); });
      hide(false);
    } catch (e) { RC.toast(e.message || '标记失败'); }
  });
  // 设置面板「高亮」tab 增删颜色 → onHlColors 回调经 shell 调这里重渲选区工具条色板
  function _renderHlSwatches() { const box = bar.querySelector('#bw-hl-colors'); if (box) box.innerHTML = `<span class="lbl">🖌</span>${_hlSwatchHtml()}`; }
  window.__bwRenderHlSwatches = _renderHlSwatches;

  if (pwa) {
    pwa.on('SELECTION', (s) => { clearTimeout(timer); timer = setTimeout(() => { if (Date.now() < _suppressBar) return; show(s); }, 120); });   // 小防抖+抑制门:单击词的 lookupWord 会抢先设 _suppressBar → 这次 SELECTION 就不弹工具条;拖选多字无 lookupWord → 照常弹
    pwa.on('ACTION', (m) => {
      if (!m || !m.action) return;
      root.dataset.pwaAction = m.action;
      const s = normalize(m.selection || m.options); if (s) snapshot = s;
      if (m.action === 'lookupWord') lookup(s);
      else if (m.action === 'lookupPhrase') phrase(s);
      else if (m.action === 'translate') translate(s);
      else if (m.action === 'explain') explain(s);
      else if (m.action === 'chat') chat(s);
      else if (m.action === 'openFullDict') RC.wordpop.openFull({ word:m.options?.word || s?.text, ctx:m.options?.context || s?.context, jp:!!m.options?.jp, file:s?.file, page:s?.page, langs:s?.langs });
      else if (m.action === 'openModelSettings') RC.assistant?.openModelSettings?.(m.options?.focusAction);
    });
    if (pwa.selection) show(pwa.selection);
  } else {
    document.addEventListener('selectionchange', () => { clearTimeout(timer); timer = setTimeout(() => {
      if (locked) return;
      // _suppressBar 只该拦"单击词直查后紧跟的自动 SELECTION"(那是塌缩/空选区);用户真的拖选
      // (非塌缩、有选区文本)必须照弹,否则刚查过一个词就拖选 → 500ms 内被误抑制 = 工具条不出(bug ②)。
      const sel = getSelection();
      const real = !!(sel && !sel.isCollapsed && String(sel).trim());
      if (real || Date.now() >= _suppressBar) show(captureWeb());
    }, 180); }, {passive:true});
  }

  // 普通网页点词直查；PWA 的字符层点击由 PdfAdapter→ACTION 交接，不能重复探测。
  if (!pwa) {
    const caret = (x,y) => document.caretRangeFromPoint ? document.caretRangeFromPoint(x,y) : null;
    let _lastPointerKind = 'mouse', _lastPointerAt = 0;
    const _rangeHit = (range, e) => {
      const kind = e.pointerType || (Date.now() - _lastPointerAt < 1000 ? _lastPointerKind : 'mouse');
      return !!(RC.ui?.rangeHitTest && RC.ui.rangeHitTest(range, e.clientX, e.clientY, { pointerType: kind }));
    };
    // 拉丁词内字符(原逻辑,别回归英文查词)
    const isW = c => /[A-Za-z0-9'’\-]/.test(c);
    // CJK/假名:汉字+平/片假名+全/半角+CJK标点(＀-￯ 已含半角片假名 ｦ-ﾟ、片假名块含长音ー中点・、
    //   平假名块含浊音记号゛゜)。逐字对齐 PDF reader.src/13-selection.js:132 的词边界判定,别漏词内字符。
    const isCJK = c => /[　-〿぀-ゟ゠-ヿ㐀-䶿一-鿿＀-￯]/.test(c);
    // 日语无空格分词:Intl.Segmenter 找**包含点击 offset 且 isWordLike 的词段**(跟 web-immersive 句级
    //   切分同机制,行 109-111)。无 Segmenter 兜底取连续 CJK 串。
    const segCJK = (text, off) => {
      try {
        if (window.Intl && Intl.Segmenter) {
          const sg = new Intl.Segmenter(undefined, { granularity: 'word' });
          for (const s of sg.segment(text)) {
            if (off >= s.index && off < s.index + s.segment.length)
              return s.isWordLike ? { a: s.index, b: s.index + s.segment.length } : null;
          }
          return null;
        }
      } catch (_) {}
      let a = off, b = off + 1;
      while (a > 0 && isCJK(text[a-1])) a--; while (b < text.length && isCJK(text[b])) b++;
      return b > a ? { a, b } : null;
    };
    // ① 落点是否命中 web-immersive 的生词下划线(CSS Highlight rc-vocab-new/seen/known,与 content.js
    //   同一 isolated world 共享 CSS.highlights)。命中就直接查那条 range 的词——与用户看到的下划线
    //   完全同口径,避免"独立再分词≠下划线分词"导致查错词、甚至点标点段落分不出词打不开框(bug ①)。
    const _vocabRangeAt = (node, offset) => {
      if (!window.CSS || !CSS.highlights) return null;
      for (const name of ['rc-vocab-new', 'rc-vocab-seen', 'rc-vocab-known']) {
        let hl; try { hl = CSS.highlights.get(name); } catch (_) { hl = null; }
        if (!hl) continue;
        for (const rng of hl) { try { if (rng.isPointInRange(node, offset)) return rng; } catch (_) {} }
      }
      return null;
    };
    document.addEventListener('click', (e) => {
      if (e.composedPath().includes(window.__bwReaderHost) || e.target.closest?.('a,button,input,textarea,select,[contenteditable="true"],[role="button"],.rc-vocab-btn,.rc-tr-block,video,audio')) return;
      const ns = getSelection(); if (ns && !ns.isCollapsed) return;
      const r = caret(e.clientX,e.clientY); if (!r || r.startContainer.nodeType !== 3) return;
      const node = r.startContainer, t = node.nodeValue || ''; if (!t) return;
      // 优先复用下划线词 range(命中即查那个词,与下划线一致);没命中再走下方独立分词。
      const vr = _vocabRangeAt(node, r.startOffset);
      if (vr && _rangeHit(vr, e)) {
        const vbox = vr.getBoundingClientRect();
        const vel = vr.startContainer.nodeType === 3 ? vr.startContainer.parentElement : vr.startContainer;
        const vblk = vel?.closest?.('p,li,td,blockquote,div');
        lookup({ text: vr.toString(), context: (vblk?.textContent || '').slice(0, 1200), rect: vbox,
          file: 'web:' + location.href.split('#')[0], langs: langHint(), kind: 'word' }, vr);
        return;
      }
      let i = Math.min(r.startOffset, t.length - 1); if (i < 0) return;
      let a, b;
      // 点击位置字符是 CJK/假名 → Intl.Segmenter 分词(日语汉字/假名不匹配 isW,原逻辑会直接 return 不查=根因);
      //   否则走原拉丁 isW 词边界逻辑,完全不变。
      if (isCJK(t[i]) || (!isW(t[i]) && i && isCJK(t[i-1]))) {
        if (!isCJK(t[i]) && i) i--;   // 点在词右缘、caret 落到下一(非CJK)字符 → 回退一格到词内
        const seg = segCJK(t, i); if (!seg) return;   // 点到标点/无词段 → 不查
        a = seg.a; b = seg.b;
      } else {
        if (!isW(t[i]) && !(i && isW(t[i-1]))) return; if (!isW(t[i])) i--;
        a = i; b = i + 1; while (a && isW(t[a-1])) a--; while (b < t.length && isW(t[b])) b++;
      }
      if (_isNativeHanWord(t.slice(a,b))) return;   // 纯中文母语词(未学日/中)→ 不弹查词框(照 PDF isNativeHan);下划线词路径不抑制
      const rr=document.createRange(); rr.setStart(node,a); rr.setEnd(node,b);
      if (!_rangeHit(rr, e)) return;   // caret 只是“最近插入点”；物理落点没碰到文字就不吸附查最近词
      const box=rr.getBoundingClientRect();
      const blk=node.parentElement?.closest?.('p,li,td,blockquote,div');
      lookup({text:t.slice(a,b),context:(blk?.textContent||'').slice(0,1200),rect:box,file:'web:'+location.href.split('#')[0],langs:langHint(),kind:'word'}, rr);
    }, true);
    // 点真实页别处 → 与 word-pop「框外关闭」同义 → 清当前词高亮(点扩展 UI/小框内不清,便于按🔊等)。
    //   逐条对齐 rc-wordpop 框外关闭:同一 pointerdown 手势 + 同一 400ms 开框豁免(window._wordPopOpenAt
    //   由 rc-wordpop 在同一 isolated world 设),高亮与小框严格同生共死;开新词的 click 紧随会重设,安全。
    document.addEventListener('pointerdown', (e) => {
      _lastPointerKind = e.pointerType || 'mouse'; _lastPointerAt = Date.now();
      if (e.composedPath().includes(window.__bwReaderHost)) return;
      if (Date.now() - (window._wordPopOpenAt || 0) < 400) return;
      clearCurWord();
    }, true);
  }
})();

// Tell Windows what this page is, from the page itself.
//
// The context endpoint accepts many concurrent connections, plays no part in
// audio ownership, and evicts nobody -- so every page can simply describe
// itself while the App holds the call from end to end. Snapshot writes are
// serialised there and the last one wins, which means switching pages needs no
// coordination at all: whichever page is in front writes, and that is the
// context. Closing a page just closes its link; the snapshot survives.
//
// Only the visible page connects. Background tabs stay silent, so a dozen open
// tabs cannot argue over what the assistant is looking at, and no connection is
// held for a page nobody is reading.
//
// Everything is guarded. An earlier attempt to extend this file broke the popup
// on ordinary sites and the cause was never established, so nothing here may
// throw into the page.
(function () {
  "use strict";
  if (typeof WebSocket !== "function") return;

  var ENDPOINT = "wss://bwicarus-2.taile44d0c.ts.net/reader-context/v1";
  var CONTRACT = "reader-computer-voice-direct/1";
  var OUTGOING = "reader-outgoing-context/1";
  var MAX_TEXT = 12000;
  var THROTTLE_MS = 1500;

  var socket = null;
  var ready = false;
  var sessionId = null;
  var seq = 0;
  var reqId = 0;
  var pending = {};
  var lastSignature = "";
  var timer = null;
  var retryMs = 2000;
  var retryTimer = null;

  function hex(n) {
    var s = "", d = "0123456789abcdef";
    for (var i = 0; i < n; i += 1) s += d[Math.floor(Math.random() * 16)];
    return s;
  }

  // 22 base64url characters means the encoding of 16 bytes, not 22 characters
  // drawn from the alphabet: the last one carries only 2 bits and is limited to
  // A/Q/g/w, so random picking is rejected as "base64url 字段无效".
  function newSessionId() {
    var bytes = new Uint8Array(16);
    crypto.getRandomValues(bytes);
    var binary = "";
    for (var i = 0; i < bytes.length; i += 1) binary += String.fromCharCode(bytes[i]);
    return "session-" + btoa(binary)
      .replace(/\+/g, "-")
      .replace(/\//g, "_")
      .replace(/=+$/, "");
  }

  function request(type, fields) {
    return new Promise(function (resolve, reject) {
      if (!socket || socket.readyState !== 1) { reject(new Error("not open")); return; }
      reqId += 1;
      var id = "cs-" + reqId;
      var message = { contract: CONTRACT, type: type, requestId: id };
      for (var k in fields) if (Object.prototype.hasOwnProperty.call(fields, k)) message[k] = fields[k];
      var timeout = setTimeout(function () { delete pending[id]; reject(new Error("timeout")); }, 10000);
      pending[id] = {
        resolve: function (v) { clearTimeout(timeout); resolve(v); },
        reject: function (e) { clearTimeout(timeout); reject(e); },
      };
      try { socket.send(JSON.stringify(message)); }
      catch (err) { clearTimeout(timeout); delete pending[id]; reject(err); }
    });
  }

  function snapshot() {
    var body = document.body;
    if (!body) return null;
    var text = "";
    try {
      text = String(body.innerText || "")
        .replace(/[ \t]+/g, " ").replace(/\n{3,}/g, "\n\n").trim().slice(0, MAX_TEXT);
    } catch (_) { return null; }
    var selection = "";
    try { selection = String(window.getSelection() || "").trim().slice(0, 400); } catch (_) {}
    return {
      url: String(location.href || ""),
      title: String(document.title || ""),
      text: text,
      selection: selection,
    };
  }

  function push() {
    if (!ready) return;
    var snap = snapshot();
    if (!snap) return;
    var signature = snap.url + "|" + snap.text.length + "|" + snap.selection;
    if (signature === lastSignature) return;
    lastSignature = signature;

    seq += 1;
    request("context", {
      sessionId: sessionId,
      contextContract: OUTGOING,
      event: {
        v: 1, seq: seq, type: "page.context", event: "page.context",
        ts: Math.floor(Date.now() / 1000), id: hex(16), stable: true,
        book_id: snap.url, file: snap.url, page: 0,
        title: snap.title, kind: "web", text_available: !!snap.text,
        page_context: {
          text: snap.text,
          text_available: !!snap.text,
          text_source: "extension-page",
          fallback_reason: snap.text ? null : "扩展未取得正文",
          truncated: snap.text.length >= MAX_TEXT,
          reason: "active",
          visual: null,
          embeds: { highlights: 0, blocks: 0, unanchored: [] },
        },
      },
    }).then(function () {
      // Position and selection travel separately; Windows pairs them with the
      // event by (file, page), so page 0 must match on both sides.
      return request("active-reading", {
        sessionId: sessionId,
        kind: "web",
        file: snap.url,
        title: snap.title,
        page: 0,
        selectionState: snap.selection ? "active" : "cleared",
        selection: snap.selection || null,
      });
    }).catch(function () {
      // A rejected push is not worth disturbing the page over; the next change
      // will try again, and a dead link is handled by onclose.
      lastSignature = "";
    });
  }

  function schedule() {
    if (timer) return;
    timer = setTimeout(function () { timer = null; push(); }, THROTTLE_MS);
  }

  function disconnect() {
    ready = false;
    if (retryTimer) { clearTimeout(retryTimer); retryTimer = null; }
    try { if (socket) socket.close(); } catch (_) {}
    socket = null;
    sessionId = null;
    lastSignature = "";
  }

  function connect() {
    if (socket || document.visibilityState !== "visible") return;
    try { socket = new WebSocket(ENDPOINT); }
    catch (_) { socket = null; return; }

    socket.onopen = function () {
      request("hello", { protocolVersion: 3 })
        .then(function () {
          sessionId = newSessionId();
          return request("context-open", { sessionId: sessionId });
        })
        .then(function () { ready = true; retryMs = 2000; push(); })
        .catch(function () { try { socket.close(); } catch (_) {} });
    };

    socket.onmessage = function (event) {
      var message;
      try { message = JSON.parse(String(event.data || "")); } catch (_) { return; }
      var entry = message && pending[message.requestId];
      if (!entry) return;
      delete pending[message.requestId];
      if (message.ok === true) entry.resolve(message.payload || message);
      else entry.reject(new Error((message.error && message.error.code) || "rejected"));
    };

    socket.onclose = function () {
      ready = false;
      socket = null;
      sessionId = null;
      for (var k in pending) { try { pending[k].reject(new Error("closed")); } catch (_) {} }
      pending = {};
      // Retry only while this page is the one being looked at; a hidden page
      // has no reason to hold a link.
      if (document.visibilityState === "visible" && !retryTimer) {
        retryTimer = setTimeout(function () { retryTimer = null; connect(); }, retryMs);
        retryMs = Math.min(retryMs * 2, 30000);
      }
    };

    socket.onerror = function () {};
  }

  function onVisibility() {
    if (document.visibilityState === "visible") connect();
    else disconnect();
  }

  // Answers the popup's status query and nothing else. Without it a page whose
  // own CSP blocks the connection looks exactly like one that is working.
  try {
    var rt = (typeof chrome !== "undefined" && chrome.runtime) || null;
    if (rt && rt.onMessage && typeof rt.onMessage.addListener === "function") {
      rt.onMessage.addListener(function (message, _sender, respond) {
        if (!message || message.type !== "BW_CTX_STATUS") return undefined;
        try { respond({ ready: ready, url: String(location.href || "") }); } catch (_) {}
        return undefined;
      });
    }
  } catch (_) {}

  try {
    document.addEventListener("visibilitychange", onVisibility, { passive: true });
    document.addEventListener("selectionchange", schedule, { passive: true });
    window.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("pagehide", disconnect, { passive: true });
    // Late enough that a client-rendered page has content to describe.
    setTimeout(function () { if (document.visibilityState === "visible") connect(); }, 1500);
  } catch (_) {}
})();
