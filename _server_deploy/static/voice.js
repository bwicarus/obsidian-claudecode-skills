/* 全站语音助手浮窗(阶段1·连续聆听)。被 app.py inject_nav 注入到每个登录页面。
   交互:点麦克风 → 进入「聆听模式」(持续听、自动断句、免手);再点一下 → 关闭。长按切识别语种(中/日/英)。
   主路 = iOS/桌面原生 Web Speech API(webkitSpeechRecognition,Apple Siri 同源引擎,中日英一线质量),
   不支持时(非 Safari 壳 / 老浏览器)降级到现有 Cloud STT(MediaRecorder 单段录音 → /api/voice/transcribe)。
   识别出的整句 → /api/voice/agent → 命令直接执行 client_actions(翻页/缩放/查词…);提问则 speechSynthesis 念回答。
   iOS 加固(均来自实测坑):continuous 不可靠→onend 自动重启;单例不重建;麦克风预热防吞首句;节流重启防
   InvalidStateError;final 后主动 abort 防自反馈;念回答时停识别、念完再续;speak 超时兜底(iOS onend 常不回调,
   否则永久卡死);看门狗续命;fetch 超时;no-speech/aborted 当正常;切后台收手。需 HTTPS(本站 Tailscale 真证书)。
   页面可定义 window.__voiceContext() 上报上下文。 */
(function () {
  if (window.__voiceLoaded) return;
  window.__voiceLoaded = true;

  // ── DOM + 样式 ──
  var fab = document.createElement('button');
  fab.id = 'voice-fab'; fab.setAttribute('aria-label', '语音助手');
  var ico = document.createElement('span'); ico.id = 'voice-ico'; ico.textContent = '🎤';
  var badge = document.createElement('span'); badge.id = 'voice-lang'; badge.style.display = 'none';
  fab.appendChild(ico); fab.appendChild(badge);
  var bubble = document.createElement('div'); bubble.id = 'voice-bubble'; bubble.style.display = 'none';
  var css = document.createElement('style');
  css.textContent =
    '#voice-fab{position:fixed;right:16px;bottom:84px;z-index:2147483000;width:54px;height:54px;border-radius:50%;' +
    'border:none;background:#2563eb;color:#fff;font-size:24px;box-shadow:0 6px 18px rgba(0,0,0,.4);cursor:pointer;' +
    'display:flex;align-items:center;justify-content:center;transition:transform .12s,background .2s;' +
    '-webkit-tap-highlight-color:transparent;-webkit-user-select:none;user-select:none;' +
    '-webkit-touch-callout:none;touch-action:manipulation}' +   /* callout:none = 防 iOS 长按弹系统菜单抢手势 */
    '#voice-fab:active{transform:scale(.92)}' +
    '#voice-fab.listen{background:#059669;animation:voiceBreath 2.4s ease-in-out infinite}' +   /* 聆听中(绿·呼吸) */
    '#voice-fab.hear{background:#059669;animation:voicePulse 1s infinite}' +                    /* 正听到声音(绿·脉冲) */
    '#voice-fab.busy{background:#6b7280}' +                                                     /* 思考/处理 */
    '#voice-fab.speak{background:#7c3aed}' +                                                    /* 念回答 */
    '#voice-ico{pointer-events:none}' +
    '#voice-lang{position:absolute;right:-2px;bottom:-2px;min-width:16px;height:16px;padding:0 3px;border-radius:9px;' +
    'background:#0b1220;color:#cfe0ff;font-size:10px;line-height:16px;font-weight:700;box-shadow:0 1px 4px rgba(0,0,0,.5);' +
    'pointer-events:none}' +
    '@keyframes voicePulse{0%,100%{box-shadow:0 0 0 0 rgba(5,150,105,.5)}50%{box-shadow:0 0 0 12px rgba(5,150,105,0)}}' +
    '@keyframes voiceBreath{0%,100%{box-shadow:0 6px 18px rgba(0,0,0,.4)}50%{box-shadow:0 0 0 8px rgba(5,150,105,.18)}}' +
    '#voice-bubble{position:fixed;right:16px;bottom:148px;z-index:2147483000;max-width:min(78vw,360px);' +
    'background:#10162a;border:1px solid #2b3b63;color:#dce6ff;padding:10px 13px;border-radius:12px;font-size:14px;' +
    'line-height:1.5;box-shadow:0 8px 22px rgba(0,0,0,.5);word-break:break-word}' +
    '#voice-bubble .vq{color:#8ea4cf;font-size:12px;margin-bottom:3px}' +
    '#voice-bubble .vi{color:#6f86b8;font-size:13px;font-style:italic}' +
    '#voice-bubble .vs{color:#9aa0aa;font-size:12px}';
  document.head.appendChild(css);
  document.body.appendChild(bubble); document.body.appendChild(fab);

  function say(html) { bubble.innerHTML = html; bubble.style.display = 'block'; }
  function status(t) { say('<div class="vs">' + t + '</div>'); }
  function escapeHtml(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }

  // ── 上下文 + 派发 ──
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
  function runClientActions(actions) {
    if (!actions || !actions.length) return;
    actions.forEach(function (a) {
      try { if (a && a.fn && typeof window[a.fn] === 'function') window[a.fn].apply(null, a.args || []); } catch (_) {}
    });
  }

  // ── 状态机:off | listen | hear | busy | speak ──
  var STATE = 'off';
  function setState(s) {
    STATE = s;
    fab.classList.remove('listen', 'hear', 'busy', 'speak');
    if (s === 'off') ico.textContent = '🎤';
    else if (s === 'listen') { fab.classList.add('listen'); ico.textContent = '👂'; }
    else if (s === 'hear') { fab.classList.add('hear'); ico.textContent = '👂'; }
    else if (s === 'busy') { fab.classList.add('busy'); ico.textContent = '…'; }
    else if (s === 'speak') { fab.classList.add('speak'); ico.textContent = '🔊'; }
    refreshBadge();
  }

  // ── 语种(中文为主,长按切换;Web Speech 一次只认一种 BCP-47)──
  var SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  var LANGS = [{ code: 'zh-CN', tag: '中' }, { code: 'ja-JP', tag: '日' }, { code: 'en-US', tag: 'EN' }];
  var langIdx = 0;
  try { var saved = localStorage.getItem('voice-lang'); if (saved) { for (var i = 0; i < LANGS.length; i++) if (LANGS[i].code === saved) langIdx = i; } } catch (_) {}
  function curLang() { return LANGS[langIdx].code; }
  function refreshBadge() { badge.textContent = LANGS[langIdx].tag; badge.style.display = (SR && STATE !== 'off') ? 'block' : 'none'; }
  function cycleLang() {
    if (!SR) { status('🎙 录音模式:语种由服务器自动识别(中/英/日)'); return; }  // fallback 下切语种无意义
    langIdx = (langIdx + 1) % LANGS.length;
    try { localStorage.setItem('voice-lang', curLang()); } catch (_) {}
    refreshBadge();
    status('🌐 识别语种:' + LANGS[langIdx].tag + '(' + curLang() + ')');
    if (rec && wantListening) { try { rec.abort(); } catch (_) {} rec.lang = curLang(); }  // onend 会按新语种重启
  }

  // ── 共享:识别整句 → 后端 → 执行命令 / 念回答。cmdGen 让「用户中途按停」能取消在途结果 ──
  var busy = false, speaking = false, cmdGen = 0;
  async function handleCommand(text) {
    text = (text || '').trim();
    if (!text) { resume(); return; }
    var g = cmdGen;
    busy = true; setState('busy');
    say('<div class="vq">🗣 ' + escapeHtml(text) + '</div><div class="vs">🤔 思考中…</div>');
    var reply = '', actions = null;
    var ctl = new AbortController();
    var to = setTimeout(function () { try { ctl.abort(); } catch (_) {} }, 30000);  // 防 fetch 永久挂死 busy
    try {
      var r = await fetch('/api/voice/agent', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ transcript: text, context: pageContext() }), signal: ctl.signal,
      });
      var d = await r.json();
      if (d.ok) { reply = d.speak || ''; actions = d.client_actions; }
      else reply = '出错了:' + (d.error || '');
    } catch (e) { reply = (e && e.name === 'AbortError') ? '响应超时了' : ('出错了:' + (e && e.message || '')); }
    clearTimeout(to);
    busy = false;
    if (g !== cmdGen) { resume(); return; }   // 处理期间用户按了停止/切后台 → 丢弃这次结果,不执行不朗读
    runClientActions(actions);
    say('<div class="vq">🗣 ' + escapeHtml(text) + '</div>' + escapeHtml(reply));
    // 纯命令(带 client_action)只显示不朗读 → 连续聆听不被打断、零自反馈;提问类(无 action)才念。
    if (reply && !(actions && actions.length)) speak(reply);
    else resume();
  }

  function speak(text) {
    if (!text || !window.speechSynthesis) { resume(); return; }
    if (rec) { try { rec.abort(); } catch (_) {} }   // 念之前停识别(onend 因 speaking 守卫不重启)
    speaking = true; setState('speak');
    var done = false;
    function fin() { if (done) return; done = true; try { window.speechSynthesis.cancel(); } catch (_) {} speaking = false; resume(); }
    try {
      window.speechSynthesis.cancel();
      var u = new SpeechSynthesisUtterance(text);
      u.lang = 'zh-CN'; u.rate = 1.05;   // 回答恒为中文口语(后端 prompt 约束),故 TTS 用 zh-CN
      u.onend = u.onerror = fin;
      window.speechSynthesis.speak(u);
    } catch (_) { fin(); return; }
    // iOS speechSynthesis 的 onend/onerror 常常不回调 → 不兜底就永久卡 speaking 态、麦再也不开。按字数估时长 + 上限。
    setTimeout(fin, Math.min(15000, 1500 + text.length * 90));
  }

  // ════════════════ 主路:原生 Web Speech API ════════════════
  var rec = null, wantListening = false, isRunning = false, lastStart = 0, lastActivity = 0, silenceTimer = 0, wdTimer = 0;

  function buildRec() {
    var r;
    try { r = new SR(); } catch (_) { return null; }
    r.lang = curLang();
    r.interimResults = true;    // 实时反馈 + 静音断句
    r.continuous = false;       // iOS continuous 不可靠,靠 onend 重启实现连续
    r.maxAlternatives = 1;
    r.onstart = function () { isRunning = true; lastActivity = Date.now(); if (!busy && !speaking) setState('listen'); };
    r.onaudiostart = function () { lastActivity = Date.now(); if (wantListening && !busy && !speaking) setState('listen'); };
    r.onresult = function (e) {
      lastActivity = Date.now();
      var interim = '', finalText = '';
      for (var i = 0; i < e.results.length; i++) {
        var t = e.results[i][0].transcript;
        if (e.results[i].isFinal) finalText += t; else interim += t;
      }
      if (finalText) {
        clearTimeout(silenceTimer);
        try { r.abort(); } catch (_) {}   // 主动停:iOS final 后可能仍开麦 → 防自反馈 + 防 onresult 重入
        handleCommand(finalText);
        return;
      }
      if (interim) {
        if (!busy && !speaking) setState('hear');
        say('<div class="vi">🎙 ' + escapeHtml(interim) + '…</div>');
        clearTimeout(silenceTimer);
        silenceTimer = setTimeout(function () { try { r.stop(); } catch (_) {} }, 750);  // 750ms 无新词 = 说完
      }
    };
    r.onerror = function (ev) {
      var err = ev && ev.error;
      if (err === 'not-allowed' || err === 'service-not-allowed') {
        wantListening = false; setState('off');
        status('麦克风权限被拒,去设置里允许后重试');
      }
      // no-speech / aborted / no-match / network 等交给 onend 重启续听
    };
    r.onend = function () {
      isRunning = false; clearTimeout(silenceTimer);
      if (wantListening && !busy && !speaking) restart();
    };
    return r;
  }

  function restart() {   // 节流重启,防 InvalidStateError / 抽风
    var wait = Math.max(0, 320 - (Date.now() - lastStart));
    setTimeout(function () {
      if (!wantListening || busy || speaking || isRunning || !rec) return;
      try { lastStart = Date.now(); isRunning = true; rec.start(); }
      catch (err) {
        if (err && err.name === 'InvalidStateError') { isRunning = true; }  // 其实还在跑:保持,交给已存在会话的 onend
        else { isRunning = false; setTimeout(restart, 600); }
      }
    }, wait);
  }

  function watchdog() {   // 看门狗:任何「想听却停了/卡住」都在数秒内续上(覆盖 onend 不来、start 抛错残留等)
    if (!wantListening || busy || speaking) return;
    if (!isRunning) { restart(); return; }
    if (Date.now() - lastActivity > 8000) { try { rec.abort(); } catch (_) {} }  // 长时间无任何事件 = 卡死 → 强制重启
  }

  function resume() {   // 处理/朗读结束后回到聆听
    if (!wantListening) { if (STATE !== 'off') setState('off'); return; }
    if (SR) { setState('listen'); restart(); }
    else setState('listen');   // fallback 不会走到这(fbStop 已置 wantListening=false)
  }

  async function warmupMic() {   // 防吞首句:首个手势里先预热麦克风再立刻放掉
    try { var s = await navigator.mediaDevices.getUserMedia({ audio: true }); s.getTracks().forEach(function (t) { t.stop(); }); } catch (_) {}
  }

  async function startSR() {
    if (!rec) rec = buildRec();
    if (!rec) { status('此浏览器不支持语音识别'); return; }
    rec.lang = curLang();
    await warmupMic();
    wantListening = true; lastActivity = Date.now(); setState('listen');
    status('🎙 在听…(点一下停止,长按切换语种)');
    restart();
    clearInterval(wdTimer); wdTimer = setInterval(watchdog, 3000);
  }
  function stopSR() {
    cmdGen++;   // 作废在途命令
    wantListening = false; clearTimeout(silenceTimer); clearInterval(wdTimer);
    try { if (rec) rec.abort(); } catch (_) {}
    try { window.speechSynthesis && window.speechSynthesis.cancel(); } catch (_) {}
    speaking = false; busy = false; setState('off');
  }

  // ════════════════ Fallback:MediaRecorder → Cloud STT(切换式单段录音)════════════════
  var fbStream = null, fbRec = null, fbChunks = [], fbRecording = false;
  function pickMime() {
    var c = ['audio/mp4', 'audio/webm;codecs=opus', 'audio/webm', 'audio/aac'];
    for (var i = 0; i < c.length; i++) { try { if (window.MediaRecorder && MediaRecorder.isTypeSupported(c[i])) return c[i]; } catch (_) {} }
    return '';
  }
  async function fbStart() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) { status('此设备不支持录音'); return; }
    try { fbStream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } }); }
    catch (e) { status('麦克风权限被拒:' + (e && e.message || '')); return; }
    fbChunks = [];
    var mime = pickMime();
    try { fbRec = mime ? new MediaRecorder(fbStream, { mimeType: mime }) : new MediaRecorder(fbStream); }
    catch (_) { try { fbRec = new MediaRecorder(fbStream); } catch (e2) { status('录音初始化失败'); return; } }
    fbRec.ondataavailable = function (ev) { if (ev.data && ev.data.size) fbChunks.push(ev.data); };
    fbRec.onstop = function () {
      try { fbStream.getTracks().forEach(function (t) { t.stop(); }); } catch (_) {}
      fbProcess(new Blob(fbChunks, { type: (fbRec && fbRec.mimeType) || 'audio/mp4' }));
    };
    try { fbRec.start(); } catch (_) { status('无法开始录音'); return; }
    fbRecording = true; setState('hear');
    status('🔴 录音中…(点一下结束并识别)');
  }
  function fbStop() {
    fbRecording = false;
    try { if (fbRec && fbRec.state === 'recording') fbRec.stop(); } catch (_) {}
    setState('busy');
  }
  async function fbProcess(blob) {
    busy = true; setState('busy'); status('✍️ 识别中…');
    var ext = (blob.type.indexOf('webm') >= 0) ? 'webm' : (blob.type.indexOf('mp4') >= 0 ? 'mp4' : 'm4a');
    var fd = new FormData(); fd.append('audio', blob, 'rec.' + ext);
    var text = '';
    var ctl = new AbortController(); var to = setTimeout(function () { try { ctl.abort(); } catch (_) {} }, 30000);
    try {
      var r = await fetch('/api/voice/transcribe', { method: 'POST', body: fd, signal: ctl.signal });
      var d = await r.json();
      text = d.ok ? (d.text || '').trim() : '';
      if (!d.ok && d.error) status('识别失败:' + d.error);
    } catch (e) { status((e && e.name === 'AbortError') ? '识别超时' : ('识别出错:' + (e && e.message || ''))); }
    clearTimeout(to); busy = false;
    if (text) handleCommand(text); else setState('off');   // fallback:handleCommand 末尾 resume 因 wantListening=false 回 off,等再点
  }

  // ════════════════ 统一开关 ════════════════
  function toggle() {
    if (SR) { if (wantListening) stopSR(); else startSR(); }       // 主路:点开持续聆听 / 再点关闭
    else { if (fbRecording) fbStop(); else fbStart(); }            // fallback:点开始录 / 再点结束识别(单段)
  }

  // 长按(480ms)切语种,短按 toggle
  var lpTimer = 0, lpFired = false;
  fab.addEventListener('pointerdown', function () { lpFired = false; lpTimer = setTimeout(function () { lpFired = true; cycleLang(); }, 480); });
  function clearLp() { clearTimeout(lpTimer); }
  fab.addEventListener('pointerup', clearLp);
  fab.addEventListener('pointercancel', clearLp);
  fab.addEventListener('pointerleave', clearLp);
  fab.addEventListener('click', function (e) { if (lpFired) { e.preventDefault(); return; } toggle(); });

  // 切后台自动收手(iOS 媒体会话冲突)
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) { cmdGen++; if (SR && wantListening) stopSR(); else if (fbRecording) fbStop(); }
  });

  setState('off');
})();
