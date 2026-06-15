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
  tabBtn.textContent = '🤖 助手';
  tabBtn.onclick = function () { window.switchSideTab && window.switchSideTab('asst'); setTimeout(function () { ta && ta.focus(); }, 200); };
  tabsEl.insertBefore(tabBtn, tabsEl.firstChild);

  // ── pane 注入 ──
  var pane = document.createElement('div');
  pane.className = 'side-pane'; pane.dataset.pane = 'asst'; pane.id = 'side-pane-asst';
  pane.innerHTML =
    '<div id="asst-thread"></div>' +
    '<div id="asst-quick">' +
      '<button data-q="prev">◀ 上页</button><button data-q="next">下页 ▶</button>' +
      '<button data-q="fit">适应</button><button data-q="zin">A+</button><button data-q="zout">A-</button>' +
      '<button data-q="ptrans">译页</button><button data-q="clear">🗑 清空</button>' +
    '</div>' +
    '<div id="asst-input"><textarea id="asst-ta" rows="1" placeholder="问这本书 / 让我帮你…(点键盘麦克风可说)"></textarea>' +
      '<button id="asst-send" title="发送">➤</button></div>';
  panelEl.appendChild(pane);

  var css = document.createElement('style');
  css.textContent =
    '#asst-fab{position:fixed;right:14px;bottom:90px;z-index:115;width:50px;height:50px;border-radius:50%;border:none;' +
    'background:#2563eb;color:#fff;font-size:24px;box-shadow:0 6px 18px rgba(0,0,0,.4);cursor:pointer;display:flex;align-items:center;justify-content:center;-webkit-tap-highlight-color:transparent}' +
    '#asst-fab:active{transform:scale(.92)}' +
    '#side-pane-asst.active{display:flex;flex-direction:column;overflow:hidden;height:100%}' +
    '#asst-thread{flex:1 1 auto;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:10px;-webkit-overflow-scrolling:touch;min-height:0}' +
    '.asst-msg{max-width:92%;padding:9px 12px;border-radius:13px;font-size:14px;line-height:1.55;word-break:break-word}' +
    '.asst-u{align-self:flex-end;background:#1d4ed8;color:#fff;border-bottom-right-radius:4px}' +
    '.asst-a{align-self:flex-start;background:#161d31;border:1px solid #243152;border-bottom-left-radius:4px}' +
    '.asst-a p{margin:.4em 0}.asst-a ul,.asst-a ol{margin:.3em 0;padding-left:1.3em}.asst-a code{background:#0b1220;padding:1px 4px;border-radius:4px}' +
    '.asst-a h1,.asst-a h2,.asst-a h3{font-size:1em;margin:.5em 0 .2em}' +
    '.asst-tool{align-self:flex-start;color:#7c93c4;font-size:12px;padding:2px 6px;font-style:italic}' +
    '.asst-undo{background:#3a1d2a;border:1px solid #6b3550;color:#ffd0e0;border-radius:7px;padding:2px 8px;font-size:12px;cursor:pointer;margin-left:6px}' +
    '.asst-undo:active{background:#52283a}.asst-undo:disabled{opacity:.5}' +
    '#asst-quick{flex:0 0 auto;display:flex;flex-wrap:wrap;gap:6px;padding:8px 10px;border-top:1px solid #233156}' +
    '#asst-quick button{background:#16203a;border:1px solid #2a3a63;color:#bcd0ff;border-radius:8px;padding:6px 10px;font-size:13px;cursor:pointer}' +
    '#asst-quick button:active{background:#22305a}' +
    '#asst-input{flex:0 0 auto;display:flex;gap:8px;padding:10px;border-top:1px solid #233156;align-items:flex-end}' +
    '#asst-ta{flex:1;background:#0b1220;border:1px solid #2a3a63;color:#e6eeff;border-radius:12px;padding:9px 11px;font-size:15px;resize:none;max-height:120px;line-height:1.4;font-family:inherit}' +
    '#asst-send{background:#2563eb;border:none;color:#fff;width:42px;height:42px;border-radius:12px;font-size:18px;cursor:pointer;flex:none}' +
    '#asst-send:disabled{opacity:.5}';
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
  function renderMd(el, text) {
    try { el.innerHTML = (typeof md === 'function') ? md(text || ' ') : esc(text).replace(/\n/g, '<br>'); }
    catch (_) { el.innerHTML = esc(text).replace(/\n/g, '<br>'); }
    try { if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([el]).catch(function () {}); } catch (_) {}
  }
  function scrollDown() { thread.scrollTop = thread.scrollHeight; }
  function addMsg(cls, html) { var d = document.createElement('div'); d.className = 'asst-msg ' + cls; d.innerHTML = html; thread.appendChild(d); scrollDown(); return d; }

  function ctx() { try { if (typeof window.__voiceContext === 'function') return window.__voiceContext(); } catch (_) {} return { page_type: 'pdf' }; }
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
    var answer = '', acts = [];
    try {
      var r = await fetch('/api/assistant/chat', {     // 历史由服务端保存(跨设备),前端不再传
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: text, context: ctx() }),
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
          else if (ev === 'task') { trackTask(parsed.task_id, parsed.label); }
          else if (ev === 'error') { answer = '⚠️ ' + parsed; aMsg.innerHTML = esc(answer); }
        }
      }
    } catch (e) { answer = '⚠️ ' + (e && e.message || '出错了'); aMsg.innerHTML = esc(answer); }
    if (!answer && aMsg.innerHTML.indexOf('asst-tool') >= 0) { aMsg.innerHTML = esc('(没拿到回答)'); }
    runActions(acts);
    streaming = false; sendBtn.disabled = false;
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
      .then(function (d) { btn.outerHTML = d && d.ok ? '<span class="asst-tool">↩ 已撤销</span>' : ('<span class="asst-tool">撤销失败:' + esc((d && d.error) || '') + '</span>'); })
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
    var q = e.target && e.target.getAttribute('data-q'); if (!q) return;
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
  ta.addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); var v = ta.value; ta.value = ''; autorow(); send(v); } });
  sendBtn.addEventListener('click', function () { var v = ta.value; ta.value = ''; autorow(); send(v); });

  function prewarm(off) { try { fetch('/api/assistant/prewarm', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(off ? { off: 1 } : {}), keepalive: true }); } catch (_) {} }
  function greet() { addMsg('asst-a', '我是这本书的阅读助手。试试:<br>· 这页讲什么 / 总结这页<br>· 翻译这段(先选中)<br>· 找讲XX的页跳过去<br>· 把这段做成卡片 / 整理成笔记<br><span style="color:#7a8497">(写入/制卡都可「↩ 撤销」;对话云端保存、跨设备;🗑 清空)</span>'); }
  function loadHistory() {   // 开面板载入服务端保存的历史(跨设备续上)
    fetch('/api/assistant/history').then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.ok && d.messages && d.messages.length) {
        d.messages.forEach(function (m) {
          if (m.role === 'user') addMsg('asst-u', esc(m.content));
          else { var el = addMsg('asst-a', ''); renderMd(el, m.content || ''); }
        });
      } else greet();
    }).catch(greet);
  }
  loadHistory();
})();

