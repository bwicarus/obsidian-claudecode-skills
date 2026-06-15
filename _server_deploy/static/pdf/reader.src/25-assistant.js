// ── 25-assistant.js:PDF 阅读器侧边栏 Copilot ──
// 右侧可收起对话面板。输入框用系统键盘(iOS 自带听写麦克风=语音输入,零自造 STT)。
// 走 /api/assistant/chat(SSE):agent 自己调工具(读页/搜索/翻译/制卡/跳页…)解复合请求。
// 复用 reader 的 md()(marked)+ MathJax 渲染答案(教材公式);复用 __voiceContext 报当前页。
// 快捷按钮(翻页/缩放)直接调 window 函数,0 延迟,不走 agent。
(function () {
  if (window.__asstLoaded) return;
  window.__asstLoaded = true;

  var history = [];          // [{role, content}] ≤6 轮
  var streaming = false;

  // ── DOM ──
  var fab = document.createElement('button');
  fab.id = 'asst-fab'; fab.title = '助手'; fab.textContent = '🤖';
  var panel = document.createElement('div'); panel.id = 'asst-panel';
  panel.innerHTML =
    '<div id="asst-head"><span>📚 阅读助手</span><button id="asst-close" title="收起">✕</button></div>' +
    '<div id="asst-thread"></div>' +
    '<div id="asst-quick">' +
      '<button data-q="prev">◀ 上页</button><button data-q="next">下页 ▶</button>' +
      '<button data-q="fit">适应</button><button data-q="zin">A+</button><button data-q="zout">A-</button>' +
      '<button data-q="ptrans">译页</button>' +
    '</div>' +
    '<div id="asst-input"><textarea id="asst-ta" rows="1" placeholder="问这本书 / 让我帮你…(点键盘麦克风可说)"></textarea>' +
      '<button id="asst-send" title="发送">➤</button></div>';
  var css = document.createElement('style');
  css.textContent =
    '#asst-fab{position:fixed;right:14px;bottom:90px;z-index:2147482000;width:50px;height:50px;border-radius:50%;border:none;' +
    'background:#2563eb;color:#fff;font-size:24px;box-shadow:0 6px 18px rgba(0,0,0,.4);cursor:pointer;display:flex;align-items:center;justify-content:center;-webkit-tap-highlight-color:transparent}' +
    '#asst-fab:active{transform:scale(.92)}' +
    '#asst-panel{position:fixed;top:0;right:0;height:100%;width:min(94vw,400px);z-index:2147482001;background:#0d1322;' +
    'border-left:1px solid #233156;box-shadow:-8px 0 24px rgba(0,0,0,.5);display:flex;flex-direction:column;transform:translateX(105%);transition:transform .22s ease;color:#dce6ff}' +
    '#asst-panel.open{transform:translateX(0)}' +
    '#asst-head{display:flex;align-items:center;justify-content:space-between;padding:12px 14px;border-bottom:1px solid #233156;font-weight:600;font-size:15px}' +
    '#asst-close{background:none;border:none;color:#8ea4cf;font-size:18px;cursor:pointer;padding:4px 8px}' +
    '#asst-thread{flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:10px;-webkit-overflow-scrolling:touch}' +
    '.asst-msg{max-width:92%;padding:9px 12px;border-radius:13px;font-size:14px;line-height:1.55;word-break:break-word}' +
    '.asst-u{align-self:flex-end;background:#1d4ed8;color:#fff;border-bottom-right-radius:4px}' +
    '.asst-a{align-self:flex-start;background:#161d31;border:1px solid #243152;border-bottom-left-radius:4px}' +
    '.asst-a p{margin:.4em 0}.asst-a ul,.asst-a ol{margin:.3em 0;padding-left:1.3em}.asst-a code{background:#0b1220;padding:1px 4px;border-radius:4px}' +
    '.asst-a h1,.asst-a h2,.asst-a h3{font-size:1em;margin:.5em 0 .2em}' +
    '.asst-tool{align-self:flex-start;color:#7c93c4;font-size:12px;padding:2px 6px;font-style:italic}' +
    '#asst-quick{display:flex;flex-wrap:wrap;gap:6px;padding:8px 10px;border-top:1px solid #233156}' +
    '#asst-quick button{background:#16203a;border:1px solid #2a3a63;color:#bcd0ff;border-radius:8px;padding:6px 10px;font-size:13px;cursor:pointer}' +
    '#asst-quick button:active{background:#22305a}' +
    '#asst-input{display:flex;gap:8px;padding:10px;border-top:1px solid #233156;align-items:flex-end}' +
    '#asst-ta{flex:1;background:#0b1220;border:1px solid #2a3a63;color:#e6eeff;border-radius:12px;padding:9px 11px;font-size:15px;resize:none;max-height:120px;line-height:1.4;font-family:inherit}' +
    '#asst-send{background:#2563eb;border:none;color:#fff;width:42px;height:42px;border-radius:12px;font-size:18px;cursor:pointer;flex:none}' +
    '#asst-send:disabled{opacity:.5}';
  document.head.appendChild(css);
  document.body.appendChild(panel); document.body.appendChild(fab);

  var thread = panel.querySelector('#asst-thread');
  var ta = panel.querySelector('#asst-ta');
  var sendBtn = panel.querySelector('#asst-send');

  function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }
  function renderMd(el, text) {
    try { el.innerHTML = (typeof md === 'function') ? md(text || ' ') : esc(text).replace(/\n/g, '<br>'); }
    catch (_) { el.innerHTML = esc(text).replace(/\n/g, '<br>'); }
    try { if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([el]).catch(function () {}); } catch (_) {}
  }
  function scrollDown() { thread.scrollTop = thread.scrollHeight; }
  function addMsg(cls, html) { var d = document.createElement('div'); d.className = 'asst-msg ' + cls; d.innerHTML = html; thread.appendChild(d); scrollDown(); return d; }

  function ctx() {
    try { if (typeof window.__voiceContext === 'function') return window.__voiceContext(); } catch (_) {}
    return { page_type: 'pdf' };
  }
  function runActions(actions) {
    if (!actions || !actions.length) return;
    actions.forEach(function (a) { try { if (a && a.fn && typeof window[a.fn] === 'function') window[a.fn].apply(null, a.args || []); } catch (_) {} });
  }

  async function send(text) {
    text = (text || '').trim();
    if (!text || streaming) return;
    streaming = true; sendBtn.disabled = true;
    addMsg('asst-u', esc(text));
    var aMsg = addMsg('asst-a', '<span class="asst-tool">思考中…</span>');
    history.push({ role: 'user', content: text });
    var answer = '', acts = [];
    try {
      var r = await fetch('/api/assistant/chat', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text, context: ctx(), history: history.slice(-6) }),
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
          else if (ev === 'answer') { answer = parsed; renderMd(aMsg, answer); scrollDown(); }
          else if (ev === 'actions') { acts = parsed; }
          else if (ev === 'error') { answer = '⚠️ ' + parsed; aMsg.innerHTML = esc(answer); }
        }
      }
    } catch (e) { answer = '⚠️ ' + (e && e.message || '出错了'); aMsg.innerHTML = esc(answer); }
    if (!answer) { answer = '(没拿到回答)'; aMsg.innerHTML = esc(answer); }
    runActions(acts);
    history.push({ role: 'assistant', content: answer.slice(0, 1200) });
    if (history.length > 12) history = history.slice(-12);
    streaming = false; sendBtn.disabled = false;
  }

  // ── 快捷按钮(直调 window 函数,0 延迟)──
  panel.querySelector('#asst-quick').addEventListener('click', function (e) {
    var q = e.target && e.target.getAttribute('data-q'); if (!q) return;
    try {
      if (q === 'prev') window.changePage(-1);
      else if (q === 'next') window.changePage(1);
      else if (q === 'fit') window.fitWidth();
      else if (q === 'zin') window.zoomChange(0.15);
      else if (q === 'zout') window.zoomChange(-0.15);
      else if (q === 'ptrans') window.togglePageTranslate();
    } catch (_) {}
  });

  // ── 输入 ──
  function autorow() { ta.style.height = 'auto'; ta.style.height = Math.min(120, ta.scrollHeight) + 'px'; }
  ta.addEventListener('input', autorow);
  ta.addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); var v = ta.value; ta.value = ''; autorow(); send(v); } });
  sendBtn.addEventListener('click', function () { var v = ta.value; ta.value = ''; autorow(); send(v); });

  // ── 开关 + 预热 ──
  function prewarm(off) { try { fetch('/api/assistant/prewarm', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(off ? { off: 1 } : {}), keepalive: true }); } catch (_) {} }
  function open() {
    panel.classList.add('open'); fab.style.display = 'none'; prewarm(false);
    if (!thread.children.length) addMsg('asst-a', '我是这本书的阅读助手。试试:<br>· 这页讲什么 / 总结这页<br>· 翻译这段(先选中)<br>· 找讲XX的页跳过去<br>· 把这段做成卡片 / 整理成笔记');
    setTimeout(function () { ta.focus(); }, 250);
  }
  function close() { panel.classList.remove('open'); fab.style.display = 'flex'; prewarm(true); }
  fab.addEventListener('click', open);
  panel.querySelector('#asst-close').addEventListener('click', close);
})();
