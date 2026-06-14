/* 全站语音助手浮窗(阶段 0)。被 app.py inject_nav 注入到每个登录页面(同 nav.js)。
   点麦克风 → 录音(MediaRecorder)→ /api/voice/transcribe(Cloud STT)→ /api/voice/agent(Claude)
   → speechSynthesis 念回答 + 执行 client_actions。需 HTTPS(getUserMedia),本站走 Tailscale 真证书。
   页面可定义 window.__voiceContext() 返回 {page_type, ...} 让 agent 知道你在看什么;默认从 URL 推断。 */
(function () {
  if (window.__voiceLoaded) return;
  window.__voiceLoaded = true;

  // ── 浮窗 UI ──
  var fab = document.createElement('button');
  fab.id = 'voice-fab';
  fab.setAttribute('aria-label', '语音助手');
  fab.innerHTML = '🎤';
  var bubble = document.createElement('div');
  bubble.id = 'voice-bubble';
  bubble.style.display = 'none';
  var css = document.createElement('style');
  css.textContent =
    '#voice-fab{position:fixed;right:16px;bottom:84px;z-index:2147483000;width:54px;height:54px;border-radius:50%;' +
    'border:none;background:#2563eb;color:#fff;font-size:24px;box-shadow:0 6px 18px rgba(0,0,0,.4);cursor:pointer;' +
    'display:flex;align-items:center;justify-content:center;transition:transform .12s,background .2s;-webkit-tap-highlight-color:transparent}' +
    '#voice-fab:active{transform:scale(.92)}' +
    '#voice-fab.rec{background:#dc2626;animation:voicePulse 1s infinite}' +
    '#voice-fab.busy{background:#6b7280}' +
    '@keyframes voicePulse{0%,100%{box-shadow:0 0 0 0 rgba(220,38,38,.5)}50%{box-shadow:0 0 0 12px rgba(220,38,38,0)}}' +
    '#voice-bubble{position:fixed;right:16px;bottom:148px;z-index:2147483000;max-width:min(78vw,360px);' +
    'background:#10162a;border:1px solid #2b3b63;color:#dce6ff;padding:10px 13px;border-radius:12px;font-size:14px;' +
    'line-height:1.5;box-shadow:0 8px 22px rgba(0,0,0,.5);word-break:break-word}' +
    '#voice-bubble .vq{color:#8ea4cf;font-size:12px;margin-bottom:3px}' +
    '#voice-bubble .vs{color:#9aa0aa;font-size:12px}';
  document.head.appendChild(css);
  document.body.appendChild(bubble);
  document.body.appendChild(fab);

  function say(html) { bubble.innerHTML = html; bubble.style.display = 'block'; }
  function status(t) { say('<div class="vs">' + t + '</div>'); }

  // ── 页面上下文(给 agent 知道你在看什么)──
  function pageContext() {
    try { if (typeof window.__voiceContext === 'function') return window.__voiceContext() || {}; } catch (_) {}
    var p = location.pathname;
    var type = p.indexOf('/pdf') === 0 ? 'pdf' : p.indexOf('/skilltree') === 0 ? 'skilltree' :
               p.indexOf('/insights') === 0 ? 'insights' : p.indexOf('/private/fitness') === 0 ? 'fitness' :
               p.indexOf('/dashboard') === 0 ? 'dashboard' : 'other';
    var ctx = { page_type: type, url: p };
    try { var sel = (window.getSelection && window.getSelection().toString() || '').trim(); if (sel) ctx.selection = sel.slice(0, 400); } catch (_) {}
    return ctx;
  }

  // ── TTS ──
  function speak(text) {
    try {
      if (!text || !window.speechSynthesis) return;
      window.speechSynthesis.cancel();
      var u = new SpeechSynthesisUtterance(text);
      u.lang = 'zh-CN'; u.rate = 1.05;
      window.speechSynthesis.speak(u);
    } catch (_) {}
  }

  // ── 录音 ──
  var mediaRec = null, chunks = [], recording = false, busy = false;

  function pickMime() {
    var cands = ['audio/mp4', 'audio/webm;codecs=opus', 'audio/webm', 'audio/aac'];
    for (var i = 0; i < cands.length; i++) {
      try { if (window.MediaRecorder && MediaRecorder.isTypeSupported(cands[i])) return cands[i]; } catch (_) {}
    }
    return '';
  }

  async function startRec() {
    if (busy || recording) return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) { status('此设备/浏览器不支持录音'); return; }
    var stream;
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: true }); }
    catch (e) { status('麦克风权限被拒:' + (e && e.message || '')); return; }
    chunks = [];
    var mime = pickMime();
    try { mediaRec = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream); }
    catch (e) { mediaRec = new MediaRecorder(stream); }
    mediaRec.ondataavailable = function (ev) { if (ev.data && ev.data.size) chunks.push(ev.data); };
    mediaRec.onstop = function () {
      try { stream.getTracks().forEach(function (t) { t.stop(); }); } catch (_) {}
      var blob = new Blob(chunks, { type: (mediaRec && mediaRec.mimeType) || 'audio/mp4' });
      handleAudio(blob);
    };
    mediaRec.start();
    recording = true; fab.classList.add('rec'); fab.innerHTML = '■';
    status('🔴 听着呢…(再点一下结束)');
  }

  function stopRec() {
    if (!recording || !mediaRec) return;
    recording = false; fab.classList.remove('rec');
    try { mediaRec.stop(); } catch (_) {}
  }

  async function handleAudio(blob) {
    busy = true; fab.classList.add('busy'); fab.innerHTML = '…'; status('✍️ 转写中…');
    var ext = (blob.type.indexOf('webm') >= 0) ? 'webm' : (blob.type.indexOf('mp4') >= 0 ? 'mp4' : 'm4a');
    var fd = new FormData(); fd.append('audio', blob, 'rec.' + ext);
    var transcript = '';
    try {
      var r = await fetch('/api/voice/transcribe', { method: 'POST', body: fd });
      var d = await r.json();
      if (!d.ok) { status('转写失败:' + (d.error || '')); reset(); return; }
      transcript = (d.text || '').trim();
    } catch (e) { status('转写出错:' + (e && e.message || '')); reset(); return; }
    if (!transcript) { status('没听清,再说一遍?'); reset(); return; }
    say('<div class="vq">🗣 ' + escapeHtml(transcript) + '</div><div class="vs">🤔 思考中…</div>');
    try {
      var r2 = await fetch('/api/voice/agent', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ transcript: transcript, context: pageContext() }),
      });
      var d2 = await r2.json();
      if (!d2.ok) { say('<div class="vq">🗣 ' + escapeHtml(transcript) + '</div><div class="vs">出错:' + (d2.error || '') + '</div>'); reset(); return; }
      var reply = d2.speak || '';
      say('<div class="vq">🗣 ' + escapeHtml(transcript) + '</div>' + escapeHtml(reply));
      runClientActions(d2.client_actions);
      speak(reply);
    } catch (e) { say('<div class="vq">🗣 ' + escapeHtml(transcript) + '</div><div class="vs">出错:' + (e && e.message || '') + '</div>'); }
    reset();
  }

  function runClientActions(actions) {
    if (!actions || !actions.length) return;
    actions.forEach(function (a) {
      try { if (a && a.fn && typeof window[a.fn] === 'function') window[a.fn].apply(null, a.args || []); } catch (_) {}
    });
  }

  function reset() { busy = false; fab.classList.remove('busy', 'rec'); fab.innerHTML = '🎤'; }
  function escapeHtml(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }

  fab.addEventListener('click', function () { if (recording) stopRec(); else if (!busy) startRec(); });
})();
