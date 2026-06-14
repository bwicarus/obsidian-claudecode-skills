/* 全站语音助手浮窗(阶段1·连续聆听 VAD)。被 app.py inject_nav 注入到每个登录页面。
   点麦克风 → 进入"聆听模式":Web Audio 能量检测(VAD)自动起停 —— 你开始说就自动录、停下约 1.1s 自动结束,
   分段送 /api/voice/transcribe(Cloud STT)→ /api/voice/agent → speechSynthesis 念回答 + 执行 client_actions,
   念完自动继续听下一句。再点一下退出聆听。处理/朗读期间暂停录音,避免它把自己念的话当输入。
   需 HTTPS(getUserMedia),本站走 Tailscale 真证书。页面可定义 window.__voiceContext() 上报上下文。 */
(function () {
  if (window.__voiceLoaded) return;
  window.__voiceLoaded = true;

  var fab = document.createElement('button');
  fab.id = 'voice-fab'; fab.setAttribute('aria-label', '语音助手'); fab.innerHTML = '🎤';
  var bubble = document.createElement('div'); bubble.id = 'voice-bubble'; bubble.style.display = 'none';
  var css = document.createElement('style');
  css.textContent =
    '#voice-fab{position:fixed;right:16px;bottom:84px;z-index:2147483000;width:54px;height:54px;border-radius:50%;' +
    'border:none;background:#2563eb;color:#fff;font-size:24px;box-shadow:0 6px 18px rgba(0,0,0,.4);cursor:pointer;' +
    'display:flex;align-items:center;justify-content:center;transition:transform .12s,background .2s;-webkit-tap-highlight-color:transparent}' +
    '#voice-fab:active{transform:scale(.92)}' +
    '#voice-fab.listen{background:#059669}' +            /* 聆听中(绿) */
    '#voice-fab.rec{background:#dc2626;animation:voicePulse 1s infinite}' +   /* 正在收音(红脉冲) */
    '#voice-fab.busy{background:#6b7280}' +              /* 转写/思考 */
    '@keyframes voicePulse{0%,100%{box-shadow:0 0 0 0 rgba(220,38,38,.5)}50%{box-shadow:0 0 0 12px rgba(220,38,38,0)}}' +
    '#voice-bubble{position:fixed;right:16px;bottom:148px;z-index:2147483000;max-width:min(78vw,360px);' +
    'background:#10162a;border:1px solid #2b3b63;color:#dce6ff;padding:10px 13px;border-radius:12px;font-size:14px;' +
    'line-height:1.5;box-shadow:0 8px 22px rgba(0,0,0,.5);word-break:break-word}' +
    '#voice-bubble .vq{color:#8ea4cf;font-size:12px;margin-bottom:3px}' +
    '#voice-bubble .vs{color:#9aa0aa;font-size:12px}';
  document.head.appendChild(css);
  document.body.appendChild(bubble); document.body.appendChild(fab);

  function say(html) { bubble.innerHTML = html; bubble.style.display = 'block'; }
  function status(t) { say('<div class="vs">' + t + '</div>'); }
  function escapeHtml(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }

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

  function speak(text, onend) {
    try {
      if (!text || !window.speechSynthesis) { onend && onend(); return; }
      window.speechSynthesis.cancel();
      var u = new SpeechSynthesisUtterance(text);
      u.lang = 'zh-CN'; u.rate = 1.05;
      u.onend = function () { onend && onend(); };
      u.onerror = function () { onend && onend(); };
      window.speechSynthesis.speak(u);
    } catch (_) { onend && onend(); }
  }

  // ── 连续聆听 + VAD ──
  var listening = false;        // 聆听模式总开关
  var stream = null, ac = null, analyser = null, buf = null, rafId = 0;
  var mediaRec = null, chunks = [], state = 'idle';   // idle | speaking | busy
  var voiceFrames = 0, speechStart = 0, lastVoice = 0;
  var START_RMS = 0.025, STOP_RMS = 0.018, START_FRAMES = 3, SILENCE_MS = 1100, MIN_MS = 280;

  function pickMime() {
    var c = ['audio/mp4', 'audio/webm;codecs=opus', 'audio/webm', 'audio/aac'];
    for (var i = 0; i < c.length; i++) { try { if (window.MediaRecorder && MediaRecorder.isTypeSupported(c[i])) return c[i]; } catch (_) {} }
    return '';
  }

  async function startListening() {
    if (listening) return;
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) { status('此设备/浏览器不支持录音'); return; }
    try { stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } }); }
    catch (e) { status('麦克风权限被拒:' + (e && e.message || '')); return; }
    try {
      ac = new (window.AudioContext || window.webkitAudioContext)();
      if (ac.state === 'suspended') await ac.resume();
      var src = ac.createMediaStreamSource(stream);
      analyser = ac.createAnalyser(); analyser.fftSize = 1024;
      buf = new Float32Array(analyser.fftSize);
      src.connect(analyser);
    } catch (e) { status('音频初始化失败:' + (e && e.message || '')); stopListening(); return; }
    listening = true; state = 'idle'; fab.classList.add('listen'); fab.innerHTML = '👂';
    status('🎙 在听…(点麦克风停止)');
    loop();
  }

  function stopListening() {
    listening = false; state = 'idle';
    if (rafId) { cancelAnimationFrame(rafId); rafId = 0; }
    try { if (mediaRec && mediaRec.state === 'recording') mediaRec.stop(); } catch (_) {}
    try { if (stream) stream.getTracks().forEach(function (t) { t.stop(); }); } catch (_) {}
    try { if (ac) ac.close(); } catch (_) {}
    stream = null; ac = null; analyser = null; mediaRec = null;
    fab.classList.remove('listen', 'rec', 'busy'); fab.innerHTML = '🎤';
    try { window.speechSynthesis && window.speechSynthesis.cancel(); } catch (_) {}
  }

  function rms() {
    if (!analyser) return 0;
    analyser.getFloatTimeDomainData(buf);
    var s = 0; for (var i = 0; i < buf.length; i++) s += buf[i] * buf[i];
    return Math.sqrt(s / buf.length);
  }

  function beginRec() {
    chunks = [];
    var mime = pickMime();
    try { mediaRec = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream); }
    catch (e) { try { mediaRec = new MediaRecorder(stream); } catch (_) { return; } }
    mediaRec.ondataavailable = function (ev) { if (ev.data && ev.data.size) chunks.push(ev.data); };
    mediaRec.onstop = function () {
      var blob = new Blob(chunks, { type: (mediaRec && mediaRec.mimeType) || 'audio/mp4' });
      if (Date.now() - speechStart < MIN_MS) { state = 'idle'; fab.classList.remove('rec'); return; }  // 太短=噪声,丢弃
      processSegment(blob);
    };
    try { mediaRec.start(); } catch (_) { return; }
    state = 'speaking'; fab.classList.add('rec'); fab.innerHTML = '🔴'; status('🔴 说话中…');
  }

  function endRec() {
    fab.classList.remove('rec');
    try { if (mediaRec && mediaRec.state === 'recording') mediaRec.stop(); } catch (_) {}
    state = 'busy'; fab.classList.add('busy'); fab.innerHTML = '…';
  }

  function loop() {
    if (!listening) return;
    rafId = requestAnimationFrame(loop);
    if (state === 'busy') return;                 // 处理/朗读期间不检测(避免听到自己)
    var e = rms(), now = Date.now();
    if (state === 'idle') {
      if (e > START_RMS) { voiceFrames++; if (voiceFrames >= START_FRAMES) { speechStart = now; lastVoice = now; beginRec(); } }
      else voiceFrames = 0;
    } else if (state === 'speaking') {
      if (e > STOP_RMS) lastVoice = now;
      if (now - lastVoice > SILENCE_MS) endRec();   // 静音够久 → 自动结束这段
    }
  }

  async function processSegment(blob) {
    state = 'busy'; status('✍️ 转写中…');
    var ext = (blob.type.indexOf('webm') >= 0) ? 'webm' : (blob.type.indexOf('mp4') >= 0 ? 'mp4' : 'm4a');
    var fd = new FormData(); fd.append('audio', blob, 'rec.' + ext);
    var transcript = '';
    try {
      var r = await fetch('/api/voice/transcribe', { method: 'POST', body: fd });
      var d = await r.json();
      transcript = d.ok ? (d.text || '').trim() : '';
      if (!d.ok && d.error) { status('转写失败:' + d.error); }
    } catch (e) { status('转写出错:' + (e && e.message || '')); }
    if (!transcript) { resumeIdle(); return; }     // 没词:不打扰,继续听
    say('<div class="vq">🗣 ' + escapeHtml(transcript) + '</div><div class="vs">🤔 思考中…</div>');
    var reply = '';
    try {
      var r2 = await fetch('/api/voice/agent', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ transcript: transcript, context: pageContext() }),
      });
      var d2 = await r2.json();
      if (d2.ok) { reply = d2.speak || ''; runClientActions(d2.client_actions); }
      else reply = '出错了:' + (d2.error || '');
    } catch (e) { reply = '出错了:' + (e && e.message || ''); }
    say('<div class="vq">🗣 ' + escapeHtml(transcript) + '</div>' + escapeHtml(reply));
    speak(reply, resumeIdle);                       // 念完再继续听(避免听到自己)
  }

  function resumeIdle() {
    if (!listening) return;
    state = 'idle'; voiceFrames = 0;
    fab.classList.remove('busy', 'rec'); fab.classList.add('listen'); fab.innerHTML = '👂';
  }

  function runClientActions(actions) {
    if (!actions || !actions.length) return;
    actions.forEach(function (a) {
      try { if (a && a.fn && typeof window[a.fn] === 'function') window[a.fn].apply(null, a.args || []); } catch (_) {}
    });
  }

  fab.addEventListener('click', function () { if (listening) stopListening(); else startListening(); });
})();
