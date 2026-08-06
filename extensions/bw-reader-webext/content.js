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
// Report this page to an in-progress call, when it is the page being looked at.
//
// The call runs in an extension page of its own and cannot read other tabs --
// activeTab covers only the tab it was opened from. Rather than widening the
// extension to all sites (package_safari.py deliberately keeps host access to
// the Pi alone), the page reports itself: content scripts already run here, so
// no new permission is involved.
//
// Everything is guarded. A previous attempt to extend this file broke the popup
// on ordinary sites and the cause was never established, so nothing here may
// throw into the page: no unhandled rejection, no error escaping a listener.
(function () {
  "use strict";
  var runtime = (typeof chrome !== "undefined" && chrome.runtime) || null;
  if (!runtime || typeof runtime.sendMessage !== "function") return;

  var MAX_TEXT = 12000;
  var MAX_VIEWPORT_SIDE_TEXT = 2400;
  var THROTTLE_MS = 1500;
  var ACTIVE_CONTEXT_HEARTBEAT_MS = 60000;
  var PREFERENCE_KEY = "bwReaderExtensionPreferencesV2";
  var CONTEXT_SYNC_KEY = "eph-ctx-sync";
  var ACTIVE_CONTEXT_KEY = "bwActivePageContextV1";

  function createSourceInstanceId() {
    var bytes = new Uint8Array(16);
    try {
      if (window.crypto && typeof window.crypto.getRandomValues === "function") {
        window.crypto.getRandomValues(bytes);
      } else {
        for (var i = 0; i < bytes.length; i += 1) bytes[i] = (Math.random() * 256) | 0;
      }
      var binary = "";
      for (var j = 0; j < bytes.length; j += 1) binary += String.fromCharCode(bytes[j]);
      return window.btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
    } catch (_) {
      // Identity is routing metadata, never an authentication secret. Keep the
      // contract valid even on an unusual page realm without Web Crypto/btoa.
      var fallback = "";
      var alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
      for (var k = 0; k < 21; k += 1) fallback += alphabet[(Math.random() * 64) | 0];
      return fallback + "AQgw".charAt((Math.random() * 4) | 0);
    }
  }

  // One identity per top-level document instance. A reload gets a new one;
  // scroll/focus/heartbeat reports from the same document keep it.
  var sourceInstanceId = createSourceInstanceId();

  // Shows a line on the page itself.
  //
  // Every diagnostic so far travelled through the frame to the host, which
  // means a broken frame silenced the very report meant to reveal it -- an
  // instrument wired through the thing it was measuring. This writes straight
  // into the page, owes nothing to the frame, and is the only way to see the
  // steps that happen before the frame is involved at all.
  //
  // Temporary. It exists to answer one question and should come out once the
  // answer is in.
  // Routed through the shared channel now.
  //
  // This used to own its own box, while the frame owned another way of
  // speaking and the bridge owned a third. Three unconnected instruments meant
  // "delivered" and "nothing arrived" could both be true-looking with no way to
  // reconcile them. One channel, and every line says who spoke it.
  function probeLine(text) {
    var P = window.__bwProbe;
    if (P) P.probe("page", text);
  }
  probeLine("脚本已加载: " + String(location.href || "").slice(0, 60));

  // Frame reports, shown on the page's own probe.
  //
  // Without this the frame's half of the journey stays invisible: the page can
  // say it handed the snapshot over, and the bridge can say nothing arrived,
  // with no way to see which of the two is mistaken.
  try {
    // No enabled flag: the channel reads its own setting, so reporting is off
    // until ?bwdebug=1 turns it on. Shipping it on by default would put a
    // diagnostic overlay on every page the user visits.
    if (window.__bwProbe) window.__bwProbe.startProbeHost();
  } catch (_) {}

  // Delivers a snapshot to the bridge frame embedded in this page.
  //
  // The frame lives in the extension's shadow tree, which this script can reach
  // because it set it up; window.__bwShadow is visible from the isolated world.
  // Speaks only to a frame whose src is our own call.html, and reports when it
  // cannot find one -- a delivery that goes nowhere must not look like success.
  function deliverToFrame(snap) {
    try {
      var scope = window.__bwShadow || document;
      var frame = scope.querySelector('iframe[src*="call.html"]');
      probeLine(
        "找框: shadow=" + (window.__bwShadow ? "有" : "无") +
        " 框=" + (frame ? "有" : "无")
      );
      // Registered by identity, so only frames we embedded may report.
      try {
        if (frame && window.__bwProbe) window.__bwProbe.trustFrame(frame);
      } catch (_) {}
      if (!frame || !frame.contentWindow) {
        // Through the shared channel, not console: on iOS there is no Web
        // Inspector, so console.warn is indistinguishable from writing nothing.
        probeLine("投递: 页面内没有桥接框，改走后台通道");
        return false;
      }
      try {
        if (
          window.__bwBrowserControl &&
          typeof window.__bwBrowserControl.install === "function"
        ) {
          window.__bwBrowserControl.install({
            frame: frame,
            sourceInstanceId: sourceInstanceId,
          });
        } else {
          probeLine("浏览控制: 执行器尚未加载");
        }
      } catch (installError) {
        probeLine(
          "浏览控制安装失败: " +
          ((installError && installError.message) || "未知")
        );
      }
      frame.contentWindow.postMessage(
        { contract: "bw-page-context/1", type: "page", page: snap },
        "*"
      );
      return true;
    } catch (err) {
      probeLine("投递失败: " + ((err && err.message) || "未知"));
      return false;
    }
  }

  var LOCAL_VISUAL_CONTRACT = "bw-reader-visual-local/1";

  function exactVisualKeys(value, keys) {
    if (!value || typeof value !== "object" || Array.isArray(value)) return false;
    var actual = Object.keys(value).sort();
    var expected = keys.slice().sort();
    if (actual.length !== expected.length) return false;
    for (var i = 0; i < actual.length; i += 1) {
      if (actual[i] !== expected[i]) return false;
    }
    return true;
  }

  function ownCallFrameForSource(source) {
    if (source !== sourceInstanceId) return null;
    var scope = window.__bwShadow || document;
    var frame = scope.querySelector('iframe[src*="call.html"]');
    if (!frame || !frame.contentWindow) return null;
    try {
      var ownCallUrl = runtime.getURL("call.html");
      if (typeof frame.src !== "string" || !frame.src.startsWith(ownCallUrl)) return null;
    } catch (_) {
      return null;
    }
    return frame;
  }

  function normalizeLocalVisualRequest(message) {
    if (!exactVisualKeys(message, ["contract", "type", "request"])) return null;
    if (message.contract !== LOCAL_VISUAL_CONTRACT || message.type !== "capture-request") return null;
    var request = message.request;
    if (!exactVisualKeys(request, [
      "contract", "commandKind", "correlation", "sourceInstanceId",
      "snapshotRevision", "file", "page", "drawingRevision", "scope",
      "selectionId", "maxBytes", "chunkCharacters",
    ])) return null;
    if (
      request.contract !== "reader-visual-delivery/2" ||
      request.commandKind !== "capture-composite" ||
      !/^[A-Za-z0-9._:-]{1,160}$/.test(String(request.correlation || "")) ||
      request.sourceInstanceId !== sourceInstanceId ||
      !Number.isSafeInteger(request.snapshotRevision) || request.snapshotRevision < 0 ||
      request.file !== String(location.href || "") ||
      !["viewport-context", "drawing-nearby", "selection-near"].includes(request.scope) ||
      (request.scope === "selection-near") !== (typeof request.selectionId === "string") ||
      request.maxBytes !== 786432 || request.chunkCharacters !== 48000
    ) return null;
    return request;
  }

  function sendLocalVisualResponse(target, request, capture) {
    var ready = !!(
      capture &&
      capture.media_type === "image/jpeg" &&
      typeof capture.b64 === "string" &&
      capture.b64.length > 0
    );
    try {
      target.postMessage({
        contract: LOCAL_VISUAL_CONTRACT,
        type: "capture-response",
        correlation: request.correlation,
        sourceInstanceId: request.sourceInstanceId,
        status: ready ? "ready" : "unavailable",
        mimeType: ready ? "image/jpeg" : "",
        b64: ready ? capture.b64 : "",
      }, "*");
    } catch (_) {}
  }

  async function captureVisualForFrame(request) {
    if (document.visibilityState !== "visible") return null;
    try {
      if (!document.hasFocus()) return null;
    } catch (_) {}
    var RC = window.RC;
    if (!RC) return null;
    var target = {
      scope: request.scope,
      page: request.page,
      selectionId: request.selectionId,
      sourceInstanceId: request.sourceInstanceId,
      snapshotRevision: request.snapshotRevision,
      drawingRevision: request.drawingRevision,
    };
    if (
      request.scope === "viewport-context" &&
      typeof RC.capturePageComposite === "function"
    ) return await RC.capturePageComposite(target);
    if (
      (request.scope === "drawing-nearby" || request.scope === "selection-near") &&
      typeof RC.captureInkRegion === "function"
    ) return await RC.captureInkRegion(target);
    return null;
  }

  window.addEventListener("message", function (event) {
    var request = normalizeLocalVisualRequest(event.data);
    if (!request) return;
    var frame = ownCallFrameForSource(request.sourceInstanceId);
    if (!frame || event.source !== frame.contentWindow) return;
    Promise.resolve(captureVisualForFrame(request)).then(
      function (capture) { sendLocalVisualResponse(event.source, request, capture); },
      function () { sendLocalVisualResponse(event.source, request, null); }
    );
  });
  var lastSignature = "";
  var pendingSignature = "";
  var contextRevision = 0;
  var viewportRevision = 0;
  var lastBrowserControlCorrelation = "";
  var timer = null;
  var preferenceKnown = false;
  var contextSyncEnabled = false;
  var extensionStore = window.__bwExtensionStore || null;

  function enabledFromRecord(record) {
    if (
      !record ||
      record.schema !== 2 ||
      !record.values ||
      !Object.prototype.hasOwnProperty.call(record.values, CONTEXT_SYNC_KEY)
    ) return null;
    var raw = record.values[CONTEXT_SYNC_KEY];
    if (raw !== "1" && raw !== "0") return null;
    return raw === "1";
  }

  function applyPreference(record) {
    var next = enabledFromRecord(record);
    probeLine(
      "偏好: raw=" +
      (record && record.values ? String(record.values[CONTEXT_SYNC_KEY]) : "undefined") +
      " enabled=" + (next === null ? "未知" : (next ? "是" : "否"))
    );
    if (next === null) return false;
    var changed = !preferenceKnown || next !== contextSyncEnabled;
    preferenceKnown = true;
    contextSyncEnabled = next;
    if (!changed) return true;
    lastSignature = "";
    pendingSignature = "";
    if (contextSyncEnabled) schedule(true);
    return true;
  }

  function contentDigest(value) {
    var text = String(value || "");
    var hash = 2166136261;
    for (var i = 0; i < text.length; i += 1) {
      hash ^= text.charCodeAt(i);
      hash = Math.imul(hash, 16777619);
    }
    return (hash >>> 0).toString(16);
  }

  function normalizeReadableText(value) {
    return String(value || "")
      .replace(new RegExp(String.fromCharCode(13) + String.fromCharCode(10), "g"), String.fromCharCode(10))
      .replace(new RegExp(String.fromCharCode(13), "g"), String.fromCharCode(10))
      .replace(new RegExp(String.fromCharCode(160), "g"), " ")
      .replace(new RegExp("[ " + String.fromCharCode(9) + "]+", "g"), " ")
      .replace(new RegExp(" *" + String.fromCharCode(10) + " *", "g"), String.fromCharCode(10))
      .replace(new RegExp(String.fromCharCode(10) + "{3,}", "g"), String.fromCharCode(10) + String.fromCharCode(10))
      .trim();
  }

  // Stable identity for a document, independent of in-page anchors. A valid
  // rel=canonical is authoritative; otherwise the current HTTP(S) URL without
  // its fragment is used. URL performs hostname/default-port normalization for
  // us, while query parameters remain because they can select different text.
  function canonicalDocumentKey() {
    var candidate = String(location.href || "");
    try {
      var declared = document.querySelector('link[rel~="canonical"][href]');
      if (declared) candidate = String(declared.href || candidate);
    } catch (_) {}
    try {
      var parsed = new URL(candidate, String(location.href || ""));
      if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
        parsed = new URL(String(location.href || ""));
      }
      parsed.hash = "";
      parsed.username = "";
      parsed.password = "";
      return parsed.toString();
    } catch (_) {
      return String(location.href || "").split("#")[0];
    }
  }

  // Structural chrome: on nearly every page, part of none of them.
  var ARTICLE_CHROME =
    '[role=navigation],[role=banner],[role=contentinfo],[role=search],' +
    '[role=menu],[role=menubar],[role=toolbar],[role=tablist],' +
    '[aria-hidden="true"],nav,header,footer,aside';
  var ARTICLE_DROP = ARTICLE_CHROME + ',script,style,noscript,form,button';

  // Scores a subtree by how much of it reads as prose.
  //
  // Navigation is many short strings spread across many links; an article is
  // long runs of text in few blocks. Dividing text length by link density
  // separates the two without knowing anything about the site. Deliberately
  // simple -- the aim is to drop menus and sidebars, not to win every layout.
  function proseScore(el) {
    var text = "";
    try { text = String(el.innerText || ""); } catch (_) { return 0; }
    var len = text.replace(/\s+/g, " ").trim().length;
    if (len < 140) return 0;
    var linkLen = 0;
    try {
      var links = el.querySelectorAll("a");
      for (var i = 0; i < links.length; i += 1) {
        linkLen += String(links[i].innerText || "").length;
      }
    } catch (_) {}
    var linkRatio = len > 0 ? linkLen / len : 1;
    // Mostly-links subtrees are menus however long they run.
    if (linkRatio > 0.55) return 0;
    var blocks = 0;
    try { blocks = el.querySelectorAll("p,h1,h2,h3,li,blockquote").length; } catch (_) {}
    return len * (1 - linkRatio) * (1 + Math.min(blocks, 40) / 20);
  }

  // Picks the subtree that reads most like an article, else the body.
  //
  // Declared landmarks come first: a page that says <article> or role=main has
  // already answered the question. Scoring is the fallback for the many pages
  // that declare nothing.
  function articleRoot() {
    var body = document.body;
    if (!body) return null;
    var declared = null;
    try { declared = body.querySelector("article,[role=main],main"); } catch (_) {}
    if (declared && proseScore(declared) > 0) return declared;

    var best = null, bestScore = 0;
    try {
      var candidates = body.querySelectorAll("article,main,section,div");
      // Bounded: a deep page holds thousands of divs, and scanning all of them
      // on every navigation would cost more than the extraction is worth.
      var limit = Math.min(candidates.length, 400);
      for (var i = 0; i < limit; i += 1) {
        var el = candidates[i];
        try { if (el.closest(ARTICLE_CHROME)) continue; } catch (_) {}
        var score = proseScore(el);
        if (score > bestScore) { bestScore = score; best = el; }
      }
    } catch (_) {}
    return best || body;
  }

  var ARTICLE_BLOCK =
    "p,h1,h2,h3,h4,h5,h6,li,dd,dt,blockquote,figcaption,td,th,pre," +
    "article,main,section,div";
  var articleTextTraversalTruncated = false;

  // Reads each rendered text node exactly once. Element-level innerText cannot
  // be used here: accepting both a parent block and one of its child blocks
  // duplicates the child's text, while detached clones include hidden text.
  function articleText(root) {
    articleTextTraversalTruncated = false;
    var parts = [];
    var block = null;
    var blockText = "";
    var visibilityCache = typeof WeakMap === "function" ? new WeakMap() : null;

    function rendered(el) {
      if (!el || el.nodeType !== 1) return true;
      if (visibilityCache && visibilityCache.has(el)) return visibilityCache.get(el);
      var ok = true;
      try {
        if (
          el.hidden ||
          el.getAttribute("aria-hidden") === "true" ||
          el.matches(ARTICLE_DROP)
        ) ok = false;
      } catch (_) {}
      if (ok) {
        try {
          var style = window.getComputedStyle(el);
          ok = !!style &&
            style.display !== "none" &&
            style.visibility !== "hidden" &&
            style.visibility !== "collapse" &&
            style.contentVisibility !== "hidden" &&
            parseFloat(style.opacity || "1") !== 0;
        } catch (_) {}
      }
      if (ok && el.parentElement) ok = rendered(el.parentElement);
      if (visibilityCache) visibilityCache.set(el, ok);
      return ok;
    }

    function textBlock(el) {
      try {
        var found = el.closest(ARTICLE_BLOCK);
        if (found && (found === root || root.contains(found))) return found;
      } catch (_) {}
      return root;
    }

    function flush() {
      var value = blockText.replace(/\s+/g, " ").trim();
      if (value) parts.push(value);
      blockText = "";
    }

    try {
      var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      var node = walker.nextNode();
      var seen = 0;
      while (node && seen < 8000) {
        seen += 1;
        var parent = node.parentElement;
        var value = String(node.nodeValue || "");
        if (value.trim() && parent && rendered(parent)) {
          var nextBlock = textBlock(parent);
          if (block && nextBlock !== block) flush();
          block = nextBlock;
          blockText += value;
        }
        node = walker.nextNode();
      }
      if (node) articleTextTraversalTruncated = true;
    } catch (_) {
      articleTextTraversalTruncated = true;
    }
    flush();
    if (parts.length) return parts.join(String.fromCharCode(10) + String.fromCharCode(10));
    // Graceful fallback for unusual DOM implementations without TreeWalker.
    try { return String(root.innerText || ""); } catch (_) { return ""; }
  }

  // Reads only the prose intersecting the current visual viewport.
  //
  // The previous snapshot used articleText(root), so scrolling produced the
  // same whole-article body and was discarded by content de-duplication. Text
  // ranges give line boxes in viewport coordinates without cloning or changing
  // the host page. A missing Range/TreeWalker implementation is reported by a
  // null return so snapshot() can retain the existing whole-article fallback.
  function viewportArticleText(root) {
    if (
      !root ||
      typeof document.createRange !== "function" ||
      typeof document.createTreeWalker !== "function" ||
      typeof NodeFilter === "undefined"
    ) return null;

    var visual = window.visualViewport || null;
    var width = Number(visual && visual.width) ||
      Number(window.innerWidth) ||
      Number(document.documentElement && document.documentElement.clientWidth) || 0;
    var height = Number(visual && visual.height) ||
      Number(window.innerHeight) ||
      Number(document.documentElement && document.documentElement.clientHeight) || 0;
    if (!(width > 0) || !(height > 0)) return null;

    var left = Number(visual && visual.offsetLeft) || 0;
    var top = Number(visual && visual.offsetTop) || 0;
    var right = left + width;
    var bottom = top + height;
    var scrolling = document.scrollingElement || document.documentElement || document.body;
    var scrollLeft = Number(window.scrollX);
    var scrollTop = Number(window.scrollY);
    if (!isFinite(scrollLeft)) scrollLeft = Number(scrolling && scrolling.scrollLeft) || 0;
    if (!isFinite(scrollTop)) scrollTop = Number(scrolling && scrolling.scrollTop) || 0;

    var parts = [];
    var beforeText = "";
    var afterText = "";
    var sawVisible = false;
    var block = null;
    var blockText = "";
    var range = null;
    var visibilityCache = typeof WeakMap === "function" ? new WeakMap() : null;
    var clipCache = typeof WeakMap === "function" ? new WeakMap() : null;

    function rendered(el) {
      if (!el || el.nodeType !== 1) return true;
      if (visibilityCache && visibilityCache.has(el)) return visibilityCache.get(el);
      var ok = true;
      try {
        if (
          el.hidden ||
          el.getAttribute("aria-hidden") === "true" ||
          el.matches(ARTICLE_DROP)
        ) ok = false;
      } catch (_) {}
      if (ok) {
        try {
          var style = window.getComputedStyle(el);
          ok = !!style &&
            style.display !== "none" &&
            style.visibility !== "hidden" &&
            style.visibility !== "collapse" &&
            style.contentVisibility !== "hidden" &&
            parseFloat(style.opacity || "1") !== 0;
        } catch (_) {}
      }
      if (ok && el.parentElement) ok = rendered(el.parentElement);
      if (visibilityCache) visibilityCache.set(el, ok);
      return ok;
    }

    // Intersect the visual viewport with every overflow-clipping ancestor.
    // A text line can be inside the browser viewport but outside an inner
    // reading pane; getClientRects alone cannot distinguish those two cases.
    function clippingRect(el) {
      if (!el || el.nodeType !== 1) {
        return { left: left, top: top, right: right, bottom: bottom };
      }
      if (clipCache && clipCache.has(el)) return clipCache.get(el);
      var inherited = clippingRect(el.parentElement);
      if (!inherited) {
        if (clipCache) clipCache.set(el, null);
        return null;
      }
      var clip = {
        left: inherited.left,
        top: inherited.top,
        right: inherited.right,
        bottom: inherited.bottom,
      };
      try {
        var style = window.getComputedStyle(el);
        var rect = el.getBoundingClientRect();
        var clipsX = /^(auto|scroll|hidden|clip|overlay)$/.test(
          String(style && style.overflowX || "")
        );
        var clipsY = /^(auto|scroll|hidden|clip|overlay)$/.test(
          String(style && style.overflowY || "")
        );
        if (clipsX) {
          clip.left = Math.max(clip.left, rect.left);
          clip.right = Math.min(clip.right, rect.right);
        }
        if (clipsY) {
          clip.top = Math.max(clip.top, rect.top);
          clip.bottom = Math.min(clip.bottom, rect.bottom);
        }
      } catch (_) {}
      if (clip.left >= clip.right || clip.top >= clip.bottom) clip = null;
      if (clipCache) clipCache.set(el, clip);
      return clip;
    }

    function intersects(rect, clip) {
      return !!(
        rect && clip && rect.width > 0 && rect.height > 0 &&
        rect.right > clip.left && rect.left < clip.right &&
        rect.bottom > clip.top && rect.top < clip.bottom
      );
    }

    // A single text node can wrap over many screens. Browser line boxes do not
    // expose character offsets, so map their ordered range back to a bounded
    // character slice. Before/current/after use the same approximation, which
    // keeps the three fields disjoint instead of smuggling context into the
    // supposedly-visible field.
    function textSliceForLines(value, rects, firstLine, endLine) {
      if (!rects.length || firstLine < 0 || endLine <= firstLine) return "";
      if (firstLine === 0 && endLine === rects.length) return value;
      var startRatio = Math.max(0, firstLine) / rects.length;
      var endRatio = Math.min(rects.length, endLine) / rects.length;
      var start = Math.floor(value.length * startRatio);
      var end = Math.ceil(value.length * endRatio);
      var boundary = /[\s,.;:!?，。；：！？、]/;
      var floor = Math.max(0, start - 80);
      var ceiling = Math.min(value.length, end + 80);
      while (start > floor && !boundary.test(value.charAt(start - 1))) start -= 1;
      while (end < ceiling && !boundary.test(value.charAt(end))) end += 1;
      return value.slice(start, end);
    }

    function addBefore(value) {
      value = normalizeReadableText(value);
      if (!value) return;
      beforeText = normalizeReadableText(beforeText + "\n\n" + value);
      if (beforeText.length > MAX_VIEWPORT_SIDE_TEXT) {
        beforeText = beforeText.slice(beforeText.length - MAX_VIEWPORT_SIDE_TEXT);
      }
    }

    function addAfter(value) {
      if (afterText.length >= MAX_VIEWPORT_SIDE_TEXT) return;
      value = normalizeReadableText(value);
      if (!value) return;
      afterText = normalizeReadableText(afterText + "\n\n" + value)
        .slice(0, MAX_VIEWPORT_SIDE_TEXT);
    }

    function textBlock(el) {
      try {
        var found = el.closest(ARTICLE_BLOCK);
        if (found && (found === root || root.contains(found))) return found;
      } catch (_) {}
      return root;
    }

    function flush() {
      var value = blockText.replace(/\s+/g, " ").trim();
      if (value) parts.push(value);
      blockText = "";
    }

    try {
      range = document.createRange();
      if (!range || typeof range.getClientRects !== "function") return null;
      var walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      var node = walker.nextNode();
      var seen = 0;
      while (node && seen < 8000) {
        seen += 1;
        var parent = node.parentElement;
        var value = String(node.nodeValue || "");
        if (value.trim() && parent && rendered(parent)) {
          range.selectNodeContents(node);
          var rects = Array.prototype.slice.call(range.getClientRects() || []);
          var clip = clippingRect(parent);
          var firstVisible = -1;
          var lastVisible = -1;
          for (var i = 0; i < rects.length; i += 1) {
            if (intersects(rects[i], clip)) {
              if (firstVisible < 0) firstVisible = i;
              lastVisible = i;
            }
          }
          if (firstVisible >= 0) {
            if (firstVisible > 0) {
              addBefore(textSliceForLines(value, rects, 0, firstVisible));
            }
            var nextBlock = textBlock(parent);
            if (block && nextBlock !== block) flush();
            block = nextBlock;
            blockText += textSliceForLines(value, rects, firstVisible, lastVisible + 1);
            sawVisible = true;
            if (lastVisible + 1 < rects.length) {
              addAfter(textSliceForLines(value, rects, lastVisible + 1, rects.length));
            }
          } else if (rects.length) {
            var allBefore = true;
            var allAfter = true;
            for (var r = 0; r < rects.length; r += 1) {
              if (rects[r].bottom > (clip ? clip.top : top)) allBefore = false;
              if (rects[r].top < (clip ? clip.bottom : bottom)) allAfter = false;
            }
            if (!sawVisible && allBefore) addBefore(value);
            else if (sawVisible && allAfter) addAfter(value);
          }
        }
        node = walker.nextNode();
      }
    } catch (_) {
      return null;
    } finally {
      try { if (range && typeof range.detach === "function") range.detach(); } catch (_) {}
    }
    flush();
    var visibleText = parts.join(String.fromCharCode(10) + String.fromCharCode(10));
    if (!visibleText.trim()) return null;
    return {
      beforeText: normalizeReadableText(beforeText),
      visibleText: normalizeReadableText(visibleText),
      afterText: normalizeReadableText(afterText),
      // Internal only: it makes a real view movement distinct even when two
      // adjacent regions contain identical text. call.js consumes the key for
      // de-duplication but never places it in the strict Windows payload.
      viewKey: [
        viewportRevision,
        Math.round(scrollLeft),
        Math.round(scrollTop),
        Math.round(width),
        Math.round(height),
      ].join(":"),
    };
  }

  function snapshot() {
    var body = document.body;
    if (!body) return null;
    var fullText = "";
    var visibleText = "";
    var beforeText = "";
    var afterText = "";
    var fullTextTruncated = false;
    var viewKey = "fallback:" + viewportRevision;
    try {
      var root = articleRoot() || body;
      var whole = String(body.innerText || "");
      // The corpus is the complete readable page, not merely the article root.
      // body.innerText follows the live rendered tree, so hidden script/style
      // content stays out while navigation, sidebars and secondary reading
      // regions remain available for the conversation's first full read.
      fullText = whole;
      if (!fullText.trim()) {
        fullText = articleText(body);
        fullTextTruncated = articleTextTraversalTruncated;
      }
      if (root !== body) {
        probeLine(
          "全文提取: 整页 " + fullText.length + " 字; 视口主栏 " +
          String(root.tagName || "").toLowerCase()
        );
      }
      fullText = normalizeReadableText(fullText);

      var viewport = viewportArticleText(root);
      if (viewport) {
        visibleText = viewport.visibleText;
        beforeText = viewport.beforeText;
        afterText = viewport.afterText;
        viewKey = viewport.viewKey;
        probeLine(
          "视口正文: 前 " + beforeText.length +
          " / 当前 " + visibleText.length +
          " / 后 " + afterText.length + " 字"
        );
      } else {
        // Range geometry is unavailable: keep the article-like reading region
        // as the marked current view. The complete body remains separately in
        // document.text and never masquerades as the viewport.
        visibleText = root === body ? fullText : articleText(root);
      }
      visibleText = normalizeReadableText(visibleText).slice(0, MAX_TEXT);
      beforeText = normalizeReadableText(beforeText).slice(-MAX_VIEWPORT_SIDE_TEXT);
      afterText = normalizeReadableText(afterText).slice(0, MAX_VIEWPORT_SIDE_TEXT);
    } catch (err) {
      probeLine("正文提取失败: " + ((err && err.message) || "未知"));
      return null;
    }
    var selection = "";
    try {
      selection = String(window.getSelection() || "").trim().slice(0, 400);
    } catch (_) {}
    var selectionRegions = {
      contract: "reader-selection-regions/1",
      total: 0,
      truncated: false,
      items: [],
    };
    try {
      if (typeof window.RC?.selectionRegionsForPage === "function") {
        selectionRegions = window.RC.selectionRegionsForPage({ page: 0 });
      }
    } catch (_) {}
    var viewportPayload = {
      beforeText: beforeText,
      visibleText: visibleText,
      afterText: afterText,
      selectionState: selection ? "active" : "cleared",
      selection: selection,
      viewKey: viewKey,
    };
    if (lastBrowserControlCorrelation) {
      viewportPayload.controlCorrelation = lastBrowserControlCorrelation;
    }
    return {
      url: String(location.href || ""),
      title: String(document.title || ""),
      // Compatibility aliases for the existing snapshot path. They are the
      // current viewport only; the full document never enters page.text.
      text: visibleText,
      selection: selection,
      selectionRegions: selectionRegions,
      viewKey: viewKey,
      viewport: viewportPayload,
      document: {
        sourceInstanceId: sourceInstanceId,
        documentKey: canonicalDocumentKey(),
        // call.js computes SHA-256 and enforces the byte bound in its trusted
        // extension origin before constructing the POST field.
        text: fullText,
        truncated: fullTextTruncated,
      },
    };
  }

  function report(force) {
    probeLine(
      "上报入口: known=" + (preferenceKnown ? "是" : "否") +
      " enabled=" + (contextSyncEnabled ? "是" : "否") +
      " visible=" + String(document.visibilityState)
    );
    // Only a preference actually read as false stops the report.
    //
    // "Not yet known" was being treated as "the user turned it off", and the
    // page then went unreported forever. Tonight that state proved reachable in
    // a way none of the failure paths explain -- both of them set the flag, yet
    // ten seconds after load it was still unset, so refreshPreference had not
    // run at all. Whatever the cause, silence-by-default is the wrong answer to
    // an unread setting: the page in front of the user is the same page whether
    // or not a preference finished loading.
    //
    // An explicit false is still honoured. Turning sync off keeps working; not
    // knowing yet no longer means off.
    if (preferenceKnown && !contextSyncEnabled) return;
    // Only the page in front of the user. Background tabs stay silent, so a
    // dozen open tabs cannot fight over what the assistant is looking at.
    if (document.visibilityState !== "visible") {
      probeLine("跳过: 页面不可见");
      return;
    }
    // Focus as well as visibility.
    //
    // visibilityState alone calls every un-minimised tab "visible", so a tab
    // the user is not looking at can still report -- and under last-write-wins
    // it overwrites the page they actually have open. That happened: a login
    // page overwrote the article being read. Focus is what distinguishes the
    // one page in front of the user from the several merely on screen.
    var focused = true;
    try { focused = document.hasFocus(); } catch (_) {}
    // `force` only bypasses content de-duplication. It must never let an
    // unfocused tab overwrite the page that is actually in front of the user.
    if (!focused) {
      probeLine("跳过: 本页未获焦点");
      return;
    }
    var snap = snapshot();
    if (!snap) return;
    var signature = snap.url + "|" + snap.title + "|" + snap.viewKey + "|" +
      contentDigest(snap.text) + "|" + contentDigest(snap.selection) + "|" +
      contentDigest(JSON.stringify(snap.selectionRegions || null)) + "|" +
      contentDigest(snap.document && snap.document.text);
    if (!force && signature === lastSignature) {
      probeLine("跳过: 内容签名未变");
      return;
    }
    if (!force && signature === pendingSignature) {
      probeLine("跳过: 同签名投递中");
      return;
    }
    pendingSignature = signature;

    // Delivered before storage is touched, not after it succeeds.
    //
    // This call used to sit inside finishStorage -- the success callback of the
    // storage write -- which quietly made the direct path depend on the very
    // relay it was meant to replace. When the write stalled or failed the
    // callback never ran, and the delivery, along with every line reporting it,
    // went silent. Tonight's log showed exactly that: the gates all opened and
    // then nothing followed.
    //
    // The frame is in this page and the snapshot is already in hand. Nothing
    // about handing it over requires a storage write to have completed first.
    probeLine("采集: " + String(snap.url || "").slice(0, 60));
    var delivered = deliverToFrame(snap);
    probeLine("投递到框: " + (delivered ? "成功" : "失败(没找到框)"));

    // The direct frame bounds and hashes document.text before network I/O.
    // Do not also copy the unbounded corpus candidate into extension storage
    // or the legacy runtime fallback: those paths exist only to recover the
    // live viewport when a frame starts late, and large pages can exceed their
    // quotas before the intended POST gets a chance to apply its byte cap.
    var legacyPage = {
      url: snap.url,
      title: snap.title,
      text: snap.text,
      selection: snap.selection,
      viewKey: snap.viewKey,
      viewport: snap.viewport,
      selectionRegions: snap.selectionRegions,
    };

    contextRevision += 1;
    var envelope = {
      schema: 1,
      revision: Date.now() + "-" + contextRevision,
      capturedAt: Date.now(),
      page: legacyPage,
    };

    function finishStorage(success) {
      if (pendingSignature === signature) pendingSignature = "";
      if (!success) {
        schedule(true);
        return;
      }
      lastSignature = signature;
      // Retain the runtime message as a fast path. The storage record is the
      // reliable handoff: a late-starting inline call frame reads it back, so
      // losing this message can no longer leave its lastPage empty forever.
      // Handed straight to the frame in this very page.
      //
      // It used to go out through runtime.sendMessage -- across a process
      // boundary, to a background worker iOS reclaims at will, and back down
      // again -- with a storage relay bolted on to cover the messages that got
      // lost on the way. Two paths patching each other, three places to fail
      // in silence, and no way to tell from the outside which one had.
      //
      // The frame is a child of this document. Nothing needs to leave the page.
      // Kept only as a fallback for surfaces with no frame of their own.
      try {
        var result = runtime.sendMessage({ type: "BW_PAGE_ACTIVE", page: legacyPage });
        if (result && typeof result.catch === "function") result.catch(function () {});
      } catch (_) {}
    }

    if (!extensionStore || typeof extensionStore.set !== "function") {
      finishStorage(false);
      return;
    }
    Promise.resolve(extensionStore.set(ACTIVE_CONTEXT_KEY, envelope)).then(
      function () { finishStorage(true); },
      function () { finishStorage(false); }
    );
  }

  function schedule(force) {
    if (force) lastSignature = "";
    if (timer) return;
    timer = setTimeout(function () {
      timer = null;
      report(!!force);
    }, THROTTLE_MS);
  }

  try {
    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible") schedule(true);
    }, { passive: true });
    document.addEventListener("selectionchange", function () {
      schedule(false);
    }, { passive: true });
    window.addEventListener("rc:inkchange", function () {
      viewportRevision += 1;
      schedule(true);
    }, { passive: true });
    // Browser-control replies only mean that the scroll operation ran. Force
    // the resulting viewport through the snapshot path before the MCP tool is
    // allowed to report success, so an immediate follow-up read cannot see the
    // pre-scroll viewport.
    window.addEventListener("bw:browser-control-refresh", function (event) {
      var detail = event && event.detail;
      var requestId = String(detail && detail.requestId || "");
      var source = String(detail && detail.sourceInstanceId || "");
      if (
        source !== sourceInstanceId ||
        !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(requestId)
      ) return;
      lastBrowserControlCorrelation = requestId;
      viewportRevision += 1;
      // This acknowledgement is part of the control operation itself. Bypass
      // the ordinary 1.5 s scroll throttle so Windows can prove it received
      // the viewport produced by this exact request rather than an unrelated
      // heartbeat revision.
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
      lastSignature = "";
      report(true);
    }, { passive: true });
    function noteViewportScroll() {
      viewportRevision += 1;
      schedule(false);
    }
    window.addEventListener("scroll", noteViewportScroll, { passive: true });
    // Element scroll events do not bubble. Capture them at document level so a
    // site whose reading pane is an inner scroller updates just like the root
    // page. The document target is already covered by window above.
    document.addEventListener("scroll", function (event) {
      if (event && event.target === document) return;
      noteViewportScroll();
    }, { capture: true, passive: true });
    ["pageshow", "focus", "online"].forEach(function (type) {
      window.addEventListener(type, function () {
        refreshPreference(true);
      }, { passive: true });
    });
    // Keep asserting which foreground page is current even when its body and
    // selection do not change. Background pages still fail the strict focus
    // gate in report(), so they cannot win merely by having a live timer.
    window.setInterval(function () {
      if (contextSyncEnabled) schedule(true);
    }, ACTIVE_CONTEXT_HEARTBEAT_MS);

    // Asks the switch itself, rather than a copy of it.
    //
    // The setting is owned by RC.ctxSync and persisted to localStorage; this
    // file was reading chrome.storage.local under a different key entirely. Two
    // different stores, two different names -- so the value came back undefined
    // no matter how many times the user toggled it, and undefined folded to
    // false. Every "I turned it on and nothing happened" tonight was this.
    //
    // RC lives in this same page, injected alongside this script, so the switch
    // can simply be asked. The storage read stays as a fallback for surfaces
    // where RC is absent.
    // Mirrors the switch into extension storage, which every site can see.
    //
    // RC.ctxSync keeps the setting in localStorage, and a content script's
    // localStorage belongs to the host site -- one copy per domain, none of
    // them shared. So the switch was only ever readable on whichever site it
    // happened to be flipped on. That is why the same build reported enabled=是
    // on one page and enabled=否 on the next: not a race, a different store.
    //
    // Extension storage is per-extension rather than per-site, so a value seen
    // once is written there and every other page reads it back. The mirror is
    // deliberately its own key: the existing preferences record has a shape
    // this file does not own, and writing a single boolean into it would mean
    // guessing at that shape.
    var MIRROR_KEY = "bwCtxSyncMirrorV1";

    function mirrorPreference(value) {
      try {
        if (!extensionStore || typeof extensionStore.set !== "function") {
          probeLine("镜像写入: 无存储通道");
          return;
        }
        Promise.resolve(
          extensionStore.set(MIRROR_KEY, { schema: 1, enabled: !!value })
        ).then(
          function () { probeLine("镜像写入: " + (value ? "开" : "关")); },
          function (err) {
            probeLine("镜像写入失败: " + (err && err.message || "未知"));
          }
        );
      } catch (err) {
        probeLine("镜像写入异常: " + (err && err.message || "未知"));
      }
    }

    function preferenceFromRuntime() {
      try {
        var RC = window.RC;
        if (
          RC && RC.ctxSync &&
          typeof RC.ctxSync.enabled === "function"
        ) {
          // rc-core is injected on every website. Its enabled() helper returns
          // false both for an explicit "0" and for a missing site-local key.
          // Only the former is user intent; treating the latter as intent
          // would let the next ordinary website overwrite a true cross-site
          // mirror with its local default.
          var key = String(RC.ctxSync.LS_KEY || "");
          var raw = key ? localStorage.getItem(key) : null;
          if (raw !== "1" && raw !== "0") {
            probeLine("RC.ctxSync: 本页无显式值");
            return null;
          }
          var live = !!RC.ctxSync.enabled();
          mirrorPreference(live);
          return live;
        }
      } catch (_) {}
      probeLine("RC.ctxSync: 不可用(本页读不到开关)");
      return null;
    }

    // Reads the mirror. Only used where the switch itself is out of reach --
    // that is, on every site other than the one it was flipped on.
    function preferenceFromMirror() {
      if (!extensionStore || typeof extensionStore.get !== "function") {
        return Promise.resolve(null);
      }
      return Promise.resolve(extensionStore.get(MIRROR_KEY))
        .then(function (record) {
          if (!record || typeof record !== "object") return null;
          return !!record.enabled;
        })
        .catch(function () { return null; });
    }

    function refreshPreference(forceReport) {
      var live = preferenceFromRuntime();
      if (live !== null) {
        preferenceKnown = true;
        var changedLive = contextSyncEnabled !== live;
        contextSyncEnabled = live;
        if (forceReport && contextSyncEnabled) schedule(true);
        else if (changedLive && contextSyncEnabled) schedule(true);
        return;
      }
      if (!extensionStore || typeof extensionStore.get !== "function") {
        // Left unknown on purpose: no store means the answer is unavailable,
        // not that the answer is no.
        return;
      }
      preferenceFromMirror().then(function (mirrored) {
        if (mirrored === null) {
          // Said out loud. An empty mirror is the most likely state on a fresh
          // install and it looks exactly like a working one that reads false --
          // staying quiet here would hide the single fact worth knowing.
          probeLine("镜像偏好: 空(尚未写入)");
          return Promise.resolve(extensionStore.get(PREFERENCE_KEY)).then(function (record) {
            var fallback = enabledFromRecord(record);
            if (!applyPreference(record)) return;
            mirrorPreference(fallback);
            if (forceReport && contextSyncEnabled) schedule(true);
          }).catch(function () {
            // A failed legacy read still says nothing about user intent.
          });
        }
        preferenceKnown = true;
        var changedMirror = contextSyncEnabled !== mirrored;
        contextSyncEnabled = mirrored;
        probeLine("镜像偏好: " + (mirrored ? "开" : "关"));
        if (contextSyncEnabled && (forceReport || changedMirror)) schedule(true);
      });
    }

    refreshPreference(true);
    if (chrome.storage && chrome.storage.local) {
      if (chrome.storage.onChanged) {
        chrome.storage.onChanged.addListener(function (changes, areaName) {
          if (areaName !== "local" || !changes || !changes[PREFERENCE_KEY]) return;
          var record = changes[PREFERENCE_KEY].newValue;
          var changedValue = enabledFromRecord(record);
          if (!applyPreference(record)) return;
          mirrorPreference(changedValue);
        });
      }
    }
  } catch (_) {}
})();
