// ── 25-assistant.js:PDF 阅读器侧边栏 Copilot(放进现有右侧抽屉 #grammar-panel 的一个 tab)──
// 输入框用系统键盘(iOS 自带听写麦克风=语音输入,零自造 STT)。走 /api/assistant/chat(SSE):
// agent 自己调工具(读页/搜索/翻译/制卡/跳页…)解复合请求。复用 reader 的 md()+MathJax 渲染答案。
// 🤖 fab 一键开抽屉到「助手」tab;快捷按钮(翻页/缩放)直调 window 函数 0 延迟。
(function () {
  if (window.__asstLoaded) return;
  var panelEl = document.getElementById('grammar-panel');
  var tabsEl = document.getElementById('side-tabs');
  if (!panelEl || !tabsEl) return;   // 抽屉不在(非阅读器页)就不挂
  window.__asstLoaded = true;

  var streaming = false;   // 对话历史由服务端保存,前端不再持本地数组

  // ── tab 注入(放第一个,最显眼)──
  var tabBtn = document.createElement('button');
  tabBtn.className = 'side-tab'; tabBtn.dataset.pane = 'asst';
  // Apple/SF「sparkles」图标(替代 🤖 emoji),复用模板 .si 样式
  tabBtn.innerHTML = '<svg class="si" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4l1.4 4.2L18 9.6l-4.6 1.4L12 16l-1.4-4.6L6 9.6l4.6-1.4L12 4z"/><path d="M18.6 14.5l.6 1.7 1.7.6-1.7.6-.6 1.7-.6-1.7-1.7-.6 1.7-.6.6-1.7z"/></svg>助手';
  tabBtn.onclick = function () { window.switchSideTab && window.switchSideTab('asst'); setTimeout(function () { ta && ta.focus(); }, 200); };
  tabsEl.insertBefore(tabBtn, tabsEl.firstChild);

  // ── pane 注入 ──
  var pane = document.createElement('div');
  pane.className = 'side-pane'; pane.dataset.pane = 'asst'; pane.id = 'side-pane-asst';
  pane.innerHTML =
    '<div id="asst-thread"></div>' +
    '<div id="asst-quick">' +
      '<button class="asst-learn" data-send="总结这页">📝 总结本页</button>' +
      '<button class="asst-learn" data-send="这页我还没掌握哪些词?逐个讲讲">📚 本页生词</button>' +
      '<button class="asst-learn" data-send="这页涉及哪些知识点?简要讲讲">🧩 这页知识点</button>' +
      '<button data-q="clear">🗑 清空</button>' +
    '</div>' +
    '<div id="asst-input">' +
      '<button id="asst-mic" title="语音输入"><svg viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.93V22h2v-3.07A7 7 0 0 0 19 12h-2z"/></svg></button>' +
      '<textarea id="asst-ta" rows="1" placeholder="问这本书 / 让我帮你…"></textarea>' +
      '<button id="asst-send" title="发送">➤</button></div>';
  panelEl.appendChild(pane);

  var css = document.createElement('style');
  css.textContent =
    '#asst-fab{position:fixed;right:14px;bottom:90px;z-index:115;width:50px;height:50px;border-radius:50%;border:none;' +
    'background:#2563eb;color:#fff;font-size:24px;box-shadow:0 6px 18px rgba(0,0,0,.4);cursor:pointer;display:flex;align-items:center;justify-content:center;-webkit-tap-highlight-color:transparent}' +
    '#asst-fab:active{transform:scale(.92)}' +
    '#side-pane-asst.active{display:flex;flex-direction:column;overflow:hidden;height:100%}' +
    '#asst-thread{flex:1 1 auto;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:10px;-webkit-overflow-scrolling:touch;min-height:0;overscroll-behavior:contain;touch-action:pan-y}' +   // contain+pan-y:滚到头不把滚动链漏给底下 PDF(否则阅读器在浮层下偷偷滚→IO 渲页=卡)
    '.asst-msg{max-width:92%;padding:9px 12px;border-radius:13px;font-size:14px;line-height:1.55;word-break:break-word}' +
    '.asst-u{align-self:flex-end;background:#1d4ed8;color:#fff;border-bottom-right-radius:4px}' +
    '.asst-a{align-self:flex-start;background:#161d31;border:1px solid #243152;border-bottom-left-radius:4px}' +
    '.asst-a p{margin:.4em 0}.asst-a ul,.asst-a ol{margin:.3em 0;padding-left:1.3em}.asst-a code{background:#0b1220;padding:1px 4px;border-radius:4px}' +
    '.asst-a h1,.asst-a h2,.asst-a h3{font-size:1em;margin:.5em 0 .2em}' +
    '.asst-tool{align-self:flex-start;color:#7c93c4;font-size:12px;padding:2px 6px;font-style:italic}' +
    '.asst-note{align-self:center;background:#2a2410;border:1px solid #5a4a18;color:#e7d28a;font-size:12px;padding:4px 10px;border-radius:9px;max-width:96%}' +
    '.asst-undo{background:#3a1d2a;border:1px solid #6b3550;color:#ffd0e0;border-radius:7px;padding:2px 8px;font-size:12px;cursor:pointer;margin-left:6px}' +
    '.asst-undo:active{background:#52283a}.asst-undo:disabled{opacity:.5}' +
    '#asst-quick{flex:0 0 auto;display:flex;flex-wrap:wrap;gap:6px;padding:8px 10px;border-top:1px solid #233156}' +
    '#asst-quick button{background:#16203a;border:1px solid #2a3a63;color:#bcd0ff;border-radius:8px;padding:6px 10px;font-size:13px;cursor:pointer}' +
    '#asst-quick button:active{background:#22305a}' +
    '#asst-quick button.asst-learn{background:#16293a;border-color:#2a4a63;color:#bce0ff}' +   // 学习类按钮:跟导航类区分
    '#asst-send.stop{background:#b23b3b}' +   // 流式中:发送→停止(红)
    // AI 答完的「追问建议」chip
    '.asst-followups{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}' +
    '.asst-fu{background:#13233f;border:1px solid #2a3a63;color:#bcd0ff;border-radius:13px;padding:5px 11px;font-size:13px;cursor:pointer;text-align:left}' +
    '.asst-fu:active{background:#1d3358}' +
    // 每条回答右下角的「!」反馈按钮 + 弹出:显示这条回答经过了哪些 AI 调用(各步模型),再给两个回报动作
    '.asst-fb-bar{position:relative;margin-top:7px;display:flex;justify-content:flex-end}' +
    '.asst-fb-btn{width:22px;height:22px;line-height:20px;text-align:center;border-radius:50%;border:1px solid #2a3a63;background:#0e1525;color:#7c93c4;font-size:13px;font-weight:700;cursor:pointer;padding:0;-webkit-tap-highlight-color:transparent}' +
    '.asst-fb-btn:active{background:#1a2540}' +
    '.asst-fb-pop{position:absolute;right:0;bottom:28px;z-index:20;width:236px;background:#0d1426;border:1px solid #2a3a63;border-radius:11px;padding:9px;box-shadow:0 8px 22px rgba(0,0,0,.5);display:flex;flex-direction:column;gap:5px}' +
    '.afp-h{font-size:11px;color:#7c93c4;margin-bottom:2px}' +
    '.afp-step{display:flex;justify-content:space-between;gap:8px;font-size:12px;line-height:1.5}' +
    '.afp-l{color:#cdd9f2;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
    '.afp-m{color:#7c93c4;flex:none;font-variant-numeric:tabular-nums}' +
    '.afp-acts{display:flex;flex-direction:column;gap:5px;margin-top:4px;border-top:1px solid #1d2742;padding-top:7px}' +
    '.afp-act{text-align:left;border:1px solid #2a3a63;border-radius:8px;padding:6px 9px;font-size:12px;cursor:pointer;color:#dbe7ff}' +
    '.afp-q{background:#16293a;border-color:#2a4a63}.afp-q:active{background:#1d3a52}' +
    '.afp-s{background:#1a2233}.afp-s:active{background:#222d44}' +
    '#asst-input{flex:0 0 auto;display:flex;gap:8px;padding:10px;border-top:1px solid #233156;align-items:flex-end}' +
    '#asst-ta{flex:1;background:#0b1220;border:1px solid #2a3a63;color:#e6eeff;border-radius:12px;padding:9px 11px;font-size:15px;resize:none;max-height:120px;line-height:1.4;font-family:inherit}' +
    '#asst-send{background:#2563eb;border:none;color:#fff;width:42px;height:42px;border-radius:12px;font-size:18px;cursor:pointer;flex:none}' +
    '#asst-send:disabled{opacity:.5}' +
    // 苹果风格语音按钮:静默时素净,听写时 iOS 蓝 + 呼吸光环
    '#asst-mic{background:#16203a;border:1px solid #2a3a63;color:#9fb4e0;width:42px;height:42px;border-radius:12px;cursor:pointer;flex:none;display:flex;align-items:center;justify-content:center;transition:background .2s,color .2s,border-color .2s,transform .1s;-webkit-tap-highlight-color:transparent}' +
    '#asst-mic:active{transform:scale(.9)}' +
    '#asst-mic.on{background:#0a84ff;border-color:#0a84ff;color:#fff;animation:asstMicPulse 1.5s ease-in-out infinite}' +
    '@keyframes asstMicPulse{0%,100%{box-shadow:0 0 0 0 rgba(10,132,255,.5)}50%{box-shadow:0 0 0 9px rgba(10,132,255,0)}}' +
    // 用户气泡里的「上下文卡片」:用过的图缩略图 / 选中的字段 / 涉及的页码,均可点击跳转
    '.asst-ctx-card{margin-top:7px;display:flex;flex-direction:column;gap:5px}' +
    '.actx-thumbs{display:flex;flex-wrap:wrap;gap:5px}' +
    '.actx-thumb{width:42px;height:42px;object-fit:cover;border-radius:6px;border:1px solid rgba(255,255,255,.45);background:#fff;cursor:pointer;flex:none}' +
    '.actx-thumb:active{transform:scale(.94)}' +
    '.actx-sel{font-size:12px;color:#dbe7ff;background:rgba(255,255,255,.13);border-left:2px solid rgba(255,255,255,.5);border-radius:4px;padding:3px 7px;cursor:pointer;line-height:1.4}' +
    '.actx-sel:active{background:rgba(255,255,255,.22)}' +
    '.actx-sel.actx-fml{text-align:center;white-space:normal;overflow-x:auto;color:#eaf2ff}' +
    '.actx-page{align-self:flex-start;font-size:11px;color:#eaf2ff;background:rgba(255,255,255,.16);border-radius:9px;padding:2px 9px;cursor:pointer}' +
    '.actx-page:active{background:rgba(255,255,255,.28)}';
  document.head.appendChild(css);

  // 🤖 fab:一键开抽屉到助手 tab
  var fab = document.createElement('button');
  fab.id = 'asst-fab'; fab.title = '阅读助手'; fab.textContent = '🤖';
  fab.addEventListener('click', function () {
    try { if (typeof openGrammarPanel === 'function') openGrammarPanel(); } catch (_) {}
    window.switchSideTab && window.switchSideTab('asst');
    prewarm(false);
    try { if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission().catch(function () {}); } catch (_) {}
    setTimeout(function () { ta && ta.focus(); }, 250);
  });
  document.body.appendChild(fab);

  var thread = pane.querySelector('#asst-thread');
  var ta = pane.querySelector('#asst-ta');
  var sendBtn = pane.querySelector('#asst-send');

  function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }
  // 把回答里的页码引用(「第40页」「40页」)变成可点链接 → 跳页 + 底部「回到」条
  function _linkifyPages(el) {
    try {
      var total = (typeof pdfDoc !== 'undefined' && pdfDoc) ? pdfDoc.numPages : 99999;
      var re = /第?\s*(\d{1,4})\s*页/g;
      var nodes = [], w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null), nd;
      while ((nd = w.nextNode())) {
        if (nd.nodeValue && /\d\s*页/.test(nd.nodeValue) && nd.parentNode &&
            !nd.parentNode.closest('a,button,.asst-pagelink,code,pre')) nodes.push(nd);
      }
      nodes.forEach(function (node) {
        var t = node.nodeValue, frag = document.createDocumentFragment(), last = 0, m; re.lastIndex = 0;
        while ((m = re.exec(t))) {
          var pn = parseInt(m[1], 10);
          if (!pn || pn < 1 || pn > total) continue;
          if (m.index > last) frag.appendChild(document.createTextNode(t.slice(last, m.index)));
          var a = document.createElement('span'); a.className = 'asst-pagelink'; a.textContent = m[0]; a.dataset.page = pn;
          frag.appendChild(a); last = m.index + m[0].length;
        }
        if (last) { if (last < t.length) frag.appendChild(document.createTextNode(t.slice(last))); node.parentNode.replaceChild(frag, node); }
      });
    } catch (_) {}
  }
  function renderMd(el, text, withMath) {
    try { el.innerHTML = (typeof md === 'function') ? md(text || ' ') : esc(text).replace(/\n/g, '<br>'); }
    catch (_) { el.innerHTML = esc(text).replace(/\n/g, '<br>'); }
    _linkifyPages(el);
    // withMath===false(流式期间)跳过 MathJax:原先每 100ms 对整段重 typeset,长答案末段二次方卡顿 → 收尾只跑一次
    if (withMath !== false) { try { if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([el]).catch(function () {}); } catch (_) {} }
  }
  // 从回答里剥离 [[FOLLOWUP]]q1|q2|q3[[/FOLLOWUP]] 追问建议(容忍流式中途未闭合)
  function _splitFollowups(text) {
    var fu = [];
    var push = function (body) { body.split(/[|\n]+/).forEach(function (q) { q = q.trim().replace(/^[\-·•\d\.\s]+/, ''); if (q) fu.push(q); }); };
    var clean = (text || '').replace(/\[\[FOLLOWUP\]\]([\s\S]*?)\[\[\/FOLLOWUP\]\]/g, function (m, body) { push(body); return ''; });
    var open = clean.indexOf('[[FOLLOWUP]]');   // 模型常漏结束标记 → 未闭合:从 [[FOLLOWUP]] 到结尾都当追问
    if (open >= 0) { push(clean.slice(open + 12).replace(/\[\[\/?FOLLOWUP\]\]/g, '')); clean = clean.slice(0, open); }
    return { text: clean.trim(), followups: fu.slice(0, 4) };
  }
  function _renderFollowups(afterEl, fus) {
    if (!fus || !fus.length) return;
    var box = document.createElement('div'); box.className = 'asst-followups';
    fus.forEach(function (q) {
      var b = document.createElement('button'); b.className = 'asst-fu'; b.textContent = q;
      b.addEventListener('click', function () { if (!streaming) send(q); });
      box.appendChild(b);
    });
    afterEl.appendChild(box); scrollDown();
  }

  // ── 每条回答的「!」反馈:点开看这条回答经过了哪些 AI 调用(各步模型),再给两个回报动作 ──
  //  · 🎯 答得不够好 → 沿「模型梯子」升一级重答本题(同时记一次「偏质量」,长期路由更强)
  //  · 🐢 太慢了    → 记一次「偏速度」(长期更倾向快模型)
  // 模型梯子(能力**严格递增**):每按一次「不够好」往上爬一级,顶端 opus·max。
  //  (claude effort 枚举 = low/medium/high/xhigh/max,**没有 ultra**;只调 effort 不换模型 = 早先的 bug:sonnet·high 按了纹丝不动)
  //  刻意不收的两类:① opus·low —— 实测质量 ≈ sonnet·深(横向不是向上),放进来会让"更强"按了反而更慢不更好;
  //                  ② haiku —— 比 sonnet 弱,永远不是"更强";它只属于反方向(🐢 太慢→未来路由偏快),不进这个向上梯子。
  var _ASST_LADDER = [
    { model: 'sonnet', effort: 'low',   label: 'sonnet·快' },
    { model: 'sonnet', effort: 'high',  label: 'sonnet·深' },
    { model: 'opus',   effort: 'high',  label: 'opus·深' },
    { model: 'opus',   effort: 'xhigh', label: 'opus·更深' },
    { model: 'opus',   effort: 'max',   label: 'opus·max' }
  ];
  function _curTier(trace) {   // 从 trace[0].model(如 "sonnet·high")解析本次编排器档位
    try {
      var mm = String((trace && trace[0] && trace[0].model) || '').match(/([a-z]+)[^a-z]+([a-z]+)/i);
      if (mm) return { model: mm[1].toLowerCase(), effort: mm[2].toLowerCase() };
    } catch (_) {}
    return null;
  }
  function _nextTier(cur) {   // 比 cur 高一级的档;null=已在顶;cur 不在梯子(历史回答无 trace)→ 默认直升 opus·深
    var i = -1;
    for (var k = 0; cur && k < _ASST_LADDER.length; k++)
      if (_ASST_LADDER[k].model === cur.model && _ASST_LADDER[k].effort === cur.effort) { i = k; break; }
    if (i < 0) return { model: 'opus', effort: 'high', label: 'opus·深' };
    return (i + 1 < _ASST_LADDER.length) ? _ASST_LADDER[i + 1] : null;
  }
  var _fbOpenPop = null;
  function _fbClosePop() { if (_fbOpenPop) { try { _fbOpenPop.remove(); } catch (_) {} _fbOpenPop = null; } }
  document.addEventListener('click', function (e) {   // 点弹窗外任意处 → 收起
    if (_fbOpenPop && e.target && e.target.closest && !e.target.closest('.asst-fb-bar')) _fbClosePop();
  });
  function _fb(kind) {   // 回报偏好;后端 _pref_bump 调整该用户的 质量↔速度 档位
    fetch('/api/assistant/feedback', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ kind: kind }) })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (d && d.ok && typeof _toast === 'function') _toast('已记录 · ' + (d.pref || '偏好已更新')); })
      .catch(function () {});
  }
  function _buildFbPop(question, trace, close) {
    var pop = document.createElement('div'); pop.className = 'asst-fb-pop';
    var h = document.createElement('div'); h.className = 'afp-h';
    h.textContent = (trace && trace.length) ? '这条回答经过的 AI 调用' : '对这条回答不满意?';
    pop.appendChild(h);
    (trace || []).forEach(function (st) {
      var row = document.createElement('div'); row.className = 'afp-step';
      var l = document.createElement('span'); l.className = 'afp-l'; l.textContent = st.label || '步骤';
      var m = document.createElement('span'); m.className = 'afp-m'; m.textContent = st.model || '—';
      row.appendChild(l); row.appendChild(m); pop.appendChild(row);
    });
    var cur = _curTier(trace);
    var nxt = _nextTier(cur);   // null = 已在梯子顶(opus·max)
    var acts = document.createElement('div'); acts.className = 'afp-acts';
    var bQ = document.createElement('button'); bQ.className = 'afp-act afp-q';
    if (!question) bQ.textContent = '🎯 以后这类问题用更强的';
    else if (nxt) bQ.textContent = '🎯 不够好 · 升到「' + nxt.label + '」重答';
    else bQ.textContent = '🎯 已是最强 · 用 opus·max 再答一次';
    bQ.addEventListener('click', function () {
      close(); _fb('quality');
      if (question && !streaming) {
        var tgt = nxt || { model: 'opus', effort: 'max' };
        send(question, { forceModel: tgt.model, forceEffort: tgt.effort });
      }
    });
    var bS = document.createElement('button'); bS.className = 'afp-act afp-s'; bS.textContent = '🐢 太慢了 · 以后更快';
    bS.addEventListener('click', function () { close(); _fb('slow'); });
    acts.appendChild(bQ); acts.appendChild(bS); pop.appendChild(acts);
    return pop;
  }
  function _attachFeedback(bubble, question, trace) {
    if (!bubble) return;
    try { var old = bubble.querySelector('.asst-fb-bar'); if (old) old.remove(); } catch (_) {}   // 重渲时防重复挂
    var bar = document.createElement('div'); bar.className = 'asst-fb-bar';
    var btn = document.createElement('button'); btn.className = 'asst-fb-btn'; btn.textContent = '!'; btn.title = '对这条回答不满意?';
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      if (_fbOpenPop && _fbOpenPop._owner === btn) { _fbClosePop(); return; }
      _fbClosePop();
      var pop = _buildFbPop(question, trace, _fbClosePop); pop._owner = btn;
      bar.appendChild(pop); _fbOpenPop = pop; scrollDown();
    });
    bar.appendChild(btn); bubble.appendChild(bar);
  }

  document.addEventListener('click', function (e) {   // 点回答里的页码链接 → 跳页 + 底部回到条
    var t = e.target;
    if (t && t.classList && t.classList.contains('asst-pagelink') && t.dataset.page && typeof window.jumpWithBack === 'function') {
      window.jumpWithBack(t.dataset.page);
    }
  });
  function scrollDown() { thread.scrollTop = thread.scrollHeight; }
  function addMsg(cls, html) { var d = document.createElement('div'); d.className = 'asst-msg ' + cls; d.innerHTML = html; thread.appendChild(d); scrollDown(); return d; }

  function ctx() { try { if (typeof window.__voiceContext === 'function') return window.__voiceContext(); } catch (_) {} return { page_type: 'pdf' }; }

  // 点击历史/上下文卡片 → 跳到那一页(同书走 jumpWithBack 带「回到」条;跨书则打开那本书定位到页)
  function _jumpToCtx(file_rel, page) {
    page = parseInt(page, 10); if (!page || page < 1) return;
    var cur = (typeof FILE_REL !== 'undefined') ? FILE_REL : '';
    if (file_rel && cur && file_rel !== cur) { location.href = '/pdf/view?file=' + encodeURIComponent(file_rel) + '&page=' + page; return; }   // 跨书:正确路由是 /pdf/view(/pdf/ 是书架)
    if (typeof window.jumpWithBack === 'function') window.jumpWithBack(page);
  }
  // open_book 工具的 client_action 用:打开另一本书(可定位页)
  window.openBookAt = function (fr, pg) { try { _jumpToCtx(fr, parseInt(pg, 10) || 1); } catch (_) {} };
  // 一条用户消息的上下文卡片:用过的图缩略图 + 选中的字段 + 涉及的页码,点任意一处都能跳过去
  // meta:{figures:[{file_rel,page,box,caption,group,has_ink}], selection, page, file_rel}; live=刚发的那条(图有笔迹走实时合成)
  // 问题是否跟「本页内容」相关:有选中/图(由它们承载跳转),或问题文字含本页指代 → 才给页码按钮;
  // 跟本页无关的纯问题(如"什么是特征值")不带页码 chip
  function _pageRefersToPage(msg) {
    var m = msg || '';
    return /这一?页|本页|此页|当前页|这段|这里|这张?图|这幅图|如[下图]图?|上面这?|这个公式|这道?题|本章|这一?章|这一?节|本节|页面|图里|图中|这部分/.test(m)
        || /\bthis (page|figure|fig|section|paragraph|chapter|image|diagram|part)\b|\bhere\b/i.test(m);
  }
  function _ctxCard(meta, live, msg) {
    if (!meta) return null;
    var figs = (meta.figures || []).filter(function (f) { return f && f.box; });
    var sel = (meta.selection || '').trim();
    var page = parseInt(meta.page, 10) || 0;
    var bookRel = meta.file_rel || (typeof FILE_REL !== 'undefined' ? FILE_REL : '');
    // 页码 chip:有选中/图时由它们跳转(不重复给);否则仅当问题确实指向本页才给
    var showPage = page && !figs.length && !sel && _pageRefersToPage(msg);
    if (!figs.length && !sel && !showPage) return null;
    var card = document.createElement('div'); card.className = 'asst-ctx-card';
    if (figs.length) {
      var row = document.createElement('div'); row.className = 'actx-thumbs';
      figs.forEach(function (f) {
        var fr = f.file_rel || bookRel;
        var img = document.createElement('img'); img.className = 'actx-thumb'; img.alt = '';
        img.title = (f.group ? '图组 · ' : '') + (f.caption || '图') + ' · p' + f.page + ' · 点击跳转';
        if (typeof window.__figThumb === 'function') window.__figThumb({ file_rel: fr, page: f.page, box: f.box, has_ink: f.has_ink }, img, live);
        img.addEventListener('click', function () { _jumpToCtx(fr, f.page); });
        row.appendChild(img);
      });
      card.appendChild(row);
    }
    if (sel) {
      var s = document.createElement('div'); s.className = 'actx-sel';
      if (/^\$\$?[\s\S]+\$\$?$/.test(sel)) {   // 公式选区($..$/$$..$$)→ MathJax 渲染,不显示成裸 LaTeX
        s.classList.add('actx-fml');
        var _raw = sel.replace(/^\$\$?/, '').replace(/\$\$?$/, '');
        var _block = /^\$\$/.test(sel) || /\\begin\{|\\\\/.test(_raw);
        s.textContent = _block ? ('\\[' + _raw + '\\]') : ('\\(' + _raw + '\\)');   // 跟公式浮层一致,避免单 $ 行内未启用
        if (window.MathJax && MathJax.typesetPromise) setTimeout(function () { try { MathJax.typesetPromise([s]); } catch (_) {} }, 0);
      } else {
        s.textContent = '“' + (sel.length > 64 ? sel.slice(0, 64) + '…' : sel) + '”';
      }
      s.title = page ? ('跳到第 ' + page + ' 页') : '';
      s.addEventListener('click', function () { _jumpToCtx(bookRel, page); });
      card.appendChild(s);
    }
    if (showPage) {
      var pg = document.createElement('span'); pg.className = 'actx-page';
      pg.textContent = '📄 第 ' + page + ' 页';
      pg.addEventListener('click', function () { _jumpToCtx(bookRel, page); });
      card.appendChild(pg);
    }
    return card;
  }

  function runActions(actions) {
    if (!actions || !actions.length) return;
    actions.forEach(function (a) { try { if (a && a.fn && typeof window[a.fn] === 'function') window[a.fn].apply(null, a.args || []); } catch (_) {} });
  }
  // agent 画完高亮后:重新拉高亮 + 重渲所有可见页(复用 17-highlight 的模块函数,本模块同作用域可调)
  window._reloadHighlights = async function () {
    try {
      if (typeof loadAllHighlights === 'function') await loadAllHighlights();
      document.querySelectorAll('.page-wrap').forEach(function (pw) {
        var n = parseInt(pw.dataset.pageNum); if (n && typeof renderHighlightsOnPage === 'function') renderHighlightsOnPage(pw, n);
      });
    } catch (_) {}
  };

  var _abort = null;
  function _setSendMode(stop) {   // 流式中:发送键→停止键(红 ■);否则发送(➤)
    if (stop) { sendBtn.classList.add('stop'); sendBtn.textContent = '■'; sendBtn.title = '停止'; }
    else { sendBtn.classList.remove('stop'); sendBtn.textContent = '➤'; sendBtn.title = '发送'; }
    sendBtn.disabled = false;
  }

  async function send(text, opts) {
    if (streaming) return;
    text = (text || '').trim();
    var sentCtx = ctx();                                // 发送时定格上下文(图/选中/页),气泡卡片与后端保存的元数据一致
    if (!text) {
      // 空输入但有焦点上下文(带入的图 / 钉住的公式或段落 / 当前选中)→ 等于"就问这个",用默认问法直接发
      var _hasFig = (sentCtx.figures && sentCtx.figures.length);
      var _fs = sentCtx.focus_sel;
      var _hasSel = (sentCtx.selection && sentCtx.selection.trim());
      if (_hasFig) text = '讲讲这张图';
      else if (_fs && _fs.text) text = (_fs.kind === 'formula') ? '讲讲这个公式' : '讲讲这段';
      else if (_hasSel) text = '讲讲这段';
      else return;   // 真·空(无任何上下文)→ 不发
    }
    streaming = true; _setSendMode(true);
    var uMsg = addMsg('asst-u', esc(text));
    try { var _cc = _ctxCard(sentCtx, true, text); if (_cc) uMsg.appendChild(_cc); } catch (_) {}
    try { window.__clearFigFocus && window.__clearFigFocus(); } catch (_) {}   // 图已"用掉"并进了这条历史 → 清空带入列表,下一条不再重复携带
    var aMsg = addMsg('asst-a', '<span class="asst-tool">思考中…</span>');
    var answer = '', acts = [], aborted = false, traceData = null;
    _abort = (typeof AbortController !== 'undefined') ? new AbortController() : null;
    try {
      var r = await fetch('/api/assistant/chat', {     // 历史由服务端保存(跨设备),前端不再传
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text, context: sentCtx, force_effort: (opts && opts.forceEffort) || undefined, force_model: (opts && opts.forceModel) || undefined }),
        signal: _abort ? _abort.signal : undefined,
      });
      var reader = r.body.getReader(), dec = new TextDecoder(), buf = '';
      while (true) {
        var rd = await reader.read(); if (rd.done) break;
        buf += dec.decode(rd.value, { stream: true });
        var idx;
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          var chunk = buf.slice(0, idx); buf = buf.slice(idx + 2);
          var ev = 'message', data = '';
          chunk.split('\n').forEach(function (ln) {
            if (ln.indexOf('event:') === 0) ev = ln.slice(6).trim();
            else if (ln.indexOf('data:') === 0) data += ln.slice(5).trim();
          });
          var parsed; try { parsed = JSON.parse(data); } catch (_) { parsed = data; }
          if (ev === 'tool') { aMsg.innerHTML = '<span class="asst-tool">🔧 ' + esc(parsed) + '…</span>'; scrollDown(); }
          else if (ev === 'answer') { answer = parsed; renderMd(aMsg, _splitFollowups(answer).text, false); scrollDown(); }   // 流式只轻量渲(不 MathJax) + 剥 FOLLOWUP 标记
          else if (ev === 'notice') { addMsg('asst-note', esc(parsed)); scrollDown(); }
          else if (ev === 'actions') { acts = parsed; }
          else if (ev === 'trace') { traceData = parsed; }   // 这条回答经过的 AI 调用链(各步模型)→ 喂给「!」反馈弹窗
          else if (ev === 'task') { trackTask(parsed.task_id, parsed.label); }
          else if (ev === 'undo' && parsed.undo_id) { addMsg('asst-a', '✓ ' + esc(parsed.label || '完成') + ' <button class="asst-undo" data-uid="' + esc(parsed.undo_id) + '">↩ 撤销</button>'); }
          else if (ev === 'error') { answer = '⚠️ ' + parsed; aMsg.innerHTML = esc(answer); }
        }
      }
    } catch (e) {
      if (e && e.name === 'AbortError') aborted = true;             // 用户点停止 → 不报错,保留已生成的部分
      else answer = answer || ('⚠️ ' + (e && e.message || '出错了'));
    }
    // 收尾:剥 FOLLOWUP → 完整渲染(MathJax 这一次)→ 追问 chip
    var pf = _splitFollowups(answer);
    if (pf.text) renderMd(aMsg, pf.text, true);
    else if (aMsg.innerHTML.indexOf('asst-tool') >= 0) aMsg.innerHTML = esc(aborted ? '(已停止)' : '(没拿到回答)');
    if (!aborted) { try { _renderFollowups(aMsg, pf.followups); } catch (_) {} }
    if (!aborted && pf.text) { try { _attachFeedback(aMsg, text, traceData); } catch (_) {} }   // 「!」反馈按钮(带本轮调用链 + 可重答)
    runActions(acts);
    streaming = false; _abort = null; _setSendMode(false);
  }

  // 后台写任务(制卡/笔记/生词):轮询完成 → 在对话里给结果 + 「↩ 撤销」按钮 + PWA 通知
  function trackTask(id, label) {
    if (!id) return;
    var line = addMsg('asst-a', '<span class="asst-tool">⏳ ' + esc(label || '处理') + '中…</span>');
    var n = 0;
    (function poll() {
      if (n++ > 120) { line.innerHTML = '<span class="asst-tool">⌛ ' + esc(label) + ':等太久了</span>'; return; }
      fetch('/api/voice/task-status?id=' + encodeURIComponent(id)).then(function (r) { return r.json(); }).then(function (d) {
        if (!d || !d.ok) { return; }
        if (d.status === 'running') { if (d.step) line.innerHTML = '<span class="asst-tool">⏳ ' + esc(d.step) + '…</span>'; setTimeout(poll, 2000); return; }
        if (d.status === 'done') {
          var uid = d.result && d.result.undo_id;
          line.innerHTML = '✓ ' + esc(d.speak || '完成') + (uid ? ' <button class="asst-undo" data-uid="' + esc(uid) + '">↩ 撤销</button>' : '');
          notify('阅读助手 ✓', d.speak || '任务完成');
        } else { line.innerHTML = '✗ ' + esc(d.error || '没办成'); }
        scrollDown();
      }).catch(function () { setTimeout(poll, 3000); });
    })();
  }
  thread.addEventListener('click', function (e) {
    var btn = e.target && e.target.closest && e.target.closest('.asst-undo'); if (!btn) return;
    var uid = btn.getAttribute('data-uid'); btn.disabled = true; btn.textContent = '撤销中…';
    fetch('/api/assistant/undo', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: uid }) })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.ok && d.kind === 'highlight') { try { window._reloadHighlights && window._reloadHighlights(); } catch (_) {} }   // 撤销高亮要重渲页面才视觉清掉
        btn.outerHTML = d && d.ok ? '<span class="asst-tool">↩ 已撤销</span>' : ('<span class="asst-tool">撤销失败:' + esc((d && d.error) || '') + '</span>');
      })
      .catch(function () { btn.disabled = false; btn.textContent = '↩ 撤销'; });
  });
  function notify(title, body) {
    try {
      if (!('Notification' in window) || Notification.permission !== 'granted') return;
      var opt = { body: body, tag: 'asst-task', icon: '/static/icons/icon-192.png' };
      if (navigator.serviceWorker && navigator.serviceWorker.ready) navigator.serviceWorker.ready.then(function (reg) { reg.showNotification(title, opt); }).catch(function () { try { new Notification(title, opt); } catch (_) {} });
      else try { new Notification(title, opt); } catch (_) {}
    } catch (_) {}
  }

  // 快捷按钮
  pane.querySelector('#asst-quick').addEventListener('click', function (e) {
    var btn = e.target && e.target.closest ? e.target.closest('button') : e.target;
    var ds = btn && btn.getAttribute('data-send');
    if (ds) { if (!streaming) send(ds); return; }   // 学习类快捷按钮:直接发预设问题
    var q = btn && btn.getAttribute('data-q'); if (!q) return;
    try {
      if (q === 'prev') window.changePage(-1);
      else if (q === 'next') window.changePage(1);
      else if (q === 'fit') window.fitWidth();
      else if (q === 'zin') window.zoomChange(0.15);
      else if (q === 'zout') window.zoomChange(-0.15);
      else if (q === 'ptrans') window.togglePageTranslate();
      else if (q === 'clear') { thread.innerHTML = ''; fetch('/api/assistant/clear', { method: 'POST' }).catch(function () {}); greet(); }
    } catch (_) {}
  });

  // 输入
  function autorow() { ta.style.height = 'auto'; ta.style.height = Math.min(120, ta.scrollHeight) + 'px'; }
  ta.addEventListener('input', autorow);
  ta.addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); if (streaming) return; micStop(); var v = ta.value; ta.value = ''; autorow(); send(v); } });
  sendBtn.addEventListener('click', function () {
    if (streaming) { try { _abort && _abort.abort(); } catch (_) {} return; }   // 流式中点 ■ → 中止本轮
    micStop(); var v = ta.value; ta.value = ''; autorow(); send(v);
  });

  // ── 苹果风格语音按钮:持续聆听,只手动停(再点麦克风 / 点发送即停)。设备原生 STT(iOS=Siri 级)。
  //    iOS 的 SpeechRecognition 静默时会自己结束,所以只要用户没手动停,onend 就重启 = 真·持续聆听。
  //    识别结果只填进输入框(用户审一眼再发),续写已有内容;无 SR 的浏览器→聚焦输入框,用系统键盘自带听写麦克风。
  //    micStop/micStart 为函数声明(在本 IIFE 内提升),上面的发送处理器即可调用 micStop 收口,避免迟到结果回填残留。
  var micBtn = pane.querySelector('#asst-mic');
  var _SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  var micRec = null, micOn = false, micCommitted = '', micSessFinal = '', micSessTok = null, micLastWrite = '';
  var micStartTs = 0, micLastStart = 0, micFails = 0, micSessProductive = false;   // 总时长软上限 + 空转(弱网/无语音)退避
  function micStop() {                 // 手动停:micOn=false + 作废会话 → 迟到 onresult 不回填、onend 不重启
    if (!micOn) return;
    micOn = false; micSessTok = null; micFails = 0;
    try { micRec && micRec.stop(); } catch (_) {}
    micBtn.classList.remove('on');
  }
  function micSpin() {                  // 起一段识别(每段:会话令牌 tok + 实例身份 thisRec 双重身份)
    if (!micOn) return;
    var tok = (micSessTok = {});
    var thisRec;
    try {
      thisRec = micRec = new _SR();
      micRec.lang = 'zh-CN'; micRec.interimResults = true; micRec.continuous = true; micRec.maxAlternatives = 1;
      micSessFinal = ''; micSessProductive = false; micLastStart = Date.now();
      micRec.onresult = function (e) {
        if (!micOn || micSessTok !== tok) return;   // 发送/停止/编辑作废的会话:不回填(防残留/旧词复活/孤立实例串扰)
        micSessProductive = true;
        var f = '', it = '';
        for (var i = 0; i < e.results.length; i++) {
          if (e.results[i].isFinal) f += e.results[i][0].transcript; else it += e.results[i][0].transcript;
        }
        micSessFinal = f;
        ta.value = micCommitted + f + it; micLastWrite = ta.value; autorow();
      };
      micRec.onerror = function (ev) {  // 权限/无麦:立即放弃;network/no-speech 等交给下面的空转计数收口,不单次就放弃
        if (ev && (ev.error === 'not-allowed' || ev.error === 'service-not-allowed' || ev.error === 'audio-capture')) micOn = false;
      };
      micRec.onend = function () {
        if (micRec !== thisRec) return;             // 已被更晚的 spin 取代的孤立实例:不提交不重启(根治 orphan + 竞态)
        if (micSessTok === tok && micSessFinal) { micCommitted = (micCommitted + micSessFinal).replace(/\s+$/, '') + ' '; }
        micSessFinal = '';
        micBtn.classList.remove('on');
        if (!micOn) { autorow(); return; }
        if (Date.now() - micStartTs > 120000) { micStop(); return; }   // 总时长软上限 2min:忘关也不会一直占麦
        // 这段没出任何结果且很快就结束 = 疑似弱网/引擎空转 → 累计 5 次即停;出过结果或在正常等静默则清零
        if (!micSessProductive && (Date.now() - micLastStart) < 1200) { if (++micFails >= 5) { micStop(); return; } }
        else micFails = 0;
        micBtn.classList.add('on');
        setTimeout(function () { if (micOn && micRec === thisRec) micSpin(); }, micFails ? 700 : 0);   // 异步重启(打断紧致 churn)+ 退避
      };
      micRec.start();
    } catch (_) { micOn = false; micSessTok = null; micBtn.classList.remove('on'); ta.focus(); }
  }
  function micStart() {
    if (!_SR) { ta.focus(); return; }   // 无原生 STT:聚焦输入框,用系统键盘的听写麦克风
    micOn = true; micFails = 0; micStartTs = Date.now(); micBtn.classList.add('on');
    micCommitted = ta.value ? (ta.value.replace(/\s+$/, '') + ' ') : '';   // 续写已有内容
    micSessFinal = ''; micLastWrite = ta.value;
    micSpin();
  }
  // 听写中用户手动改输入框(典型:逐字删除):以改后文本为新基线 + 作废当前会话重起一段(新实例 results 为空,
  // 旧词不会被下次 onresult 带回来)。我们自己填的值不算手动编辑(programmatic 赋值不触发 input,micLastWrite 再兜底)。
  ta.addEventListener('input', function () {
    if (!micOn || ta.value === micLastWrite) return;
    micSessTok = null;                  // 作废:在途/后续旧 onresult 不再回填
    micCommitted = ta.value; micLastWrite = ta.value; micSessFinal = '';
    try { micRec && micRec.stop(); } catch (_) {}   // onend(micOn 仍真,且是当前实例)→ micSpin 重起 fresh-results 新会话
  });
  document.addEventListener('visibilitychange', function () { if (document.hidden) micStop(); });   // 切走/锁屏即停,免后台占麦空转
  if (!_SR) micBtn.title = '点这里→用键盘的听写麦克风';
  micBtn.addEventListener('click', function () { micOn ? micStop() : micStart(); });

  function prewarm(off) { try { fetch('/api/assistant/prewarm', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(off ? { off: 1 } : {}), keepalive: true }); } catch (_) {} }
  window.__asstPrewarm = function () { try { prewarm(false); } catch (_) {} };   // 切到助手 tab 时也预热(减第二条起的冷启动)
  function greet() { addMsg('asst-a', '我是这本书的阅读助手。试试:<br>· 这页讲什么 / 总结这页<br>· 翻译这段(先选中)<br>· 找讲XX的页跳过去<br>· 把这段做成卡片 / 整理成笔记<br><span style="color:#7a8497">(写入/制卡都可「↩ 撤销」;对话云端保存、跨设备;🗑 清空)</span>'); }
  function loadHistory() {   // 开面板载入服务端保存的历史(跨设备续上)
    fetch('/api/assistant/history').then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.ok && d.messages && d.messages.length) {
        var _lastQ = '';   // 历史回答的「!」反馈要带上「重答」用的原问题 → 记住上一条用户消息
        d.messages.forEach(function (m) {
          if (m.role === 'user') {
            _lastQ = m.content || '';
            var uel = addMsg('asst-u', esc(m.content));
            try { var c = _ctxCard({ figures: m.figures, selection: m.selection, page: m.page, file_rel: m.file_rel }, false, m.content); if (c) uel.appendChild(c); } catch (_) {}
          }
          else {
            var el = addMsg('asst-a', ''); var _pf = _splitFollowups(m.content || ''); renderMd(el, _pf.text);
            try { _renderFollowups(el, _pf.followups); } catch (_) {}
            try { _attachFeedback(el, _lastQ, null); } catch (_) {}   // 历史无逐步 trace → 仅给回报动作(质量回报会用 _lastQ 重答)
          }
        });
        // 进面板自动滚到最新(最下方):渲完滚一次,再隔 250ms 补一次(图/MathJax 异步撑高后位置会漂)
        requestAnimationFrame(scrollDown);
        setTimeout(scrollDown, 250);
      } else greet();
    }).catch(greet);
  }
  loadHistory();
})();

