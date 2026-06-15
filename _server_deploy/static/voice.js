/* 全站语音助手浮窗 v2(阶段1:采集层 + 生命周期)。被 app.py inject_nav 注入到每个登录页面。
   交互:点麦克风 → 进入持续聆听(Web Audio VAD 自动起停、免手);再点关闭。长按切识别语种。
   采集(按工程范式,避开 Safari MediaRecorder 的坑):
     - 单一麦克风消费者:只用 getUserMedia + Web Audio,不并用 webkitSpeechRecognition。
     - getUserMedia 在点击同步栈里发起(本函数由 click 直接同步调用,首个 await 前),否则 iOS 拒授权。
     - 音频图 source→ScriptProcessor→zeroGain→destination:必须接 destination,否则 iOS 读到静音、VAD 失灵。
     - ScriptProcessor 抓 PCM → 浏览器内 OfflineAudioContext 风格降采样到 16k mono → 编 LINEAR16 WAV → 上传。
     - VAD:RMS 阈值起录 + 静音超时收句 + 硬时长上限兜底(VAD 万一失灵也保证会收)。
   生命周期纪律:每次会话一个自增 epoch;迟到的异步回调按 epoch 失配一律丢弃;timer 存 id 停时清;
     停止后 active 门闸不再发;忙时 FIFO 队列缓存语音、按序补发;每条退出路径彻底释放(停轨+关 AudioContext)。
   云端 STT(/api/voice/transcribe,command_and_search + speechContexts 把屏幕实体当发音偏置)→ 文字。
   文字 → /api/voice/agent → 命令直接执行 client_actions / 提问 speechSynthesis 念回。念回期间静音 VAD 防自反馈。
   关键步骤打 beacon 日志回 /api/voice/log(没法看用户设备控制台时盲调有据)。需 HTTPS(本站 Tailscale 真证书)。 */
(function () {
  if (window.__voiceLoaded) return;
  window.__voiceLoaded = true;
  // PDF 阅读器改用侧边栏 Copilot(reader.src/25-assistant.js),这里的悬浮麦克风不在 PDF 页出现
  if (location.pathname.indexOf('/pdf') === 0) return;

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
    '-webkit-tap-highlight-color:transparent;-webkit-user-select:none;user-select:none;-webkit-touch-callout:none;touch-action:manipulation}' +
    '#voice-fab:active{transform:scale(.92)}' +
    '#voice-fab.listen{background:#059669;animation:voiceBreath 2.4s ease-in-out infinite}' +
    '#voice-fab.hear{background:#059669;animation:voicePulse 1s infinite}' +
    '#voice-fab.busy{background:#6b7280}' +
    '#voice-fab.speak{background:#7c3aed}' +
    '#voice-ico{pointer-events:none}' +
    '#voice-lang{position:absolute;right:-2px;bottom:-2px;min-width:16px;height:16px;padding:0 3px;border-radius:9px;' +
    'background:#0b1220;color:#cfe0ff;font-size:10px;line-height:16px;font-weight:700;box-shadow:0 1px 4px rgba(0,0,0,.5);pointer-events:none}' +
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

  // ── beacon 日志(盲调有据)──
  function beacon(step, obj) {
    try {
      var body = JSON.stringify({ step: step, page: location.pathname, ep: epoch, t: Date.now(), d: obj || {} });
      if (navigator.sendBeacon) navigator.sendBeacon('/api/voice/log', body);
      else fetch('/api/voice/log', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: body, keepalive: true });
    } catch (_) {}
  }
  // 聆听开始→预热待命大脑(省冷启动);结束→回收。fire-and-forget。
  function prewarmBrain(off) {
    try { fetch('/api/voice/prewarm', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(off ? { off: 1 } : {}), keepalive: true }); } catch (_) {}
  }
  // PWA 系统通知:在点击手势里申请权限(iOS 要求);任务完成时弹(锁屏/切走也能收到)。走 service worker showNotification(iOS PWA 正路),退回 new Notification。
  function ensureNotifyPerm() {
    try { if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission().catch(function () {}); } catch (_) {}
  }
  function notify(title, body) {
    try {
      if (!('Notification' in window) || Notification.permission !== 'granted') return;
      var opt = { body: body, tag: 'voice-task', icon: '/static/icons/icon-192.png', badge: '/static/icons/icon-192.png' };
      if (navigator.serviceWorker && navigator.serviceWorker.ready) {
        navigator.serviceWorker.ready.then(function (reg) { reg.showNotification(title, opt); })
          .catch(function () { try { new Notification(title, opt); } catch (_) {} });
      } else { try { new Notification(title, opt); } catch (_) {} }
    } catch (_) {}
  }

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
  function curHints() {   // 屏幕可见实体名 → speechContexts 发音偏置(谐音纠错的音频层)
    try {
      var c = pageContext(), out = [];
      function add(arr, key, strip) {
        if (!arr) return;
        for (var i = 0; i < arr.length; i++) {
          var n = key ? (arr[i] && arr[i][key]) : arr[i];
          if (!n) continue; n = String(n); if (strip) n = n.replace(/\.[a-z0-9]+$/i, '');
          if (n) out.push(n);
        }
      }
      add(c.books, 'name', true); add(c.visible_kg_nodes, 'name', false);
      add(c.visible_vocab, null, false); add(c.visible_entities, 'name', false);
      return out.slice(0, 250);
    } catch (_) { return []; }
  }
  window.__voiceGo = function (path) { try { if (path && typeof path === 'string' && path.charAt(0) === '/') location.href = path; } catch (_) {} };
  window.__voiceOpenBook = function (rel) { try { if (rel && typeof rel === 'string') location.href = '/pdf/view?file=' + encodeURIComponent(rel); } catch (_) {} };
  function runClientActions(actions) {
    if (!actions || !actions.length) return;
    actions.forEach(function (a) {
      try { if (a && a.fn && typeof window[a.fn] === 'function') window[a.fn].apply(null, a.args || []); } catch (_) {}
    });
  }

  // ── 状态机 + 语种 ──
  var STATE = 'off';
  function setState(s) {
    STATE = s;
    fab.classList.remove('listen', 'hear', 'busy', 'speak');
    if (s === 'off') ico.textContent = '🎤';
    else if (s === 'listen') { fab.classList.add('listen'); ico.textContent = '👂'; }
    else if (s === 'hear') { fab.classList.add('hear'); ico.textContent = '🔴'; }
    else if (s === 'busy') { fab.classList.add('busy'); ico.textContent = '…'; }
    else if (s === 'speak') { fab.classList.add('speak'); ico.textContent = '🔊'; }
    refreshBadge();
  }
  var LANGS = [{ code: 'cmn-Hans-CN', tag: '中' }, { code: 'ja-JP', tag: '日' }, { code: 'en-US', tag: 'EN' }];
  var langIdx = 0;
  try { var sv = localStorage.getItem('voice-lang'); if (sv) { for (var li = 0; li < LANGS.length; li++) if (LANGS[li].code === sv) langIdx = li; } } catch (_) {}
  function curLang() { return LANGS[langIdx].code; }
  function refreshBadge() { badge.textContent = LANGS[langIdx].tag; badge.style.display = (STATE !== 'off') ? 'block' : 'none'; }
  function cycleLang() {
    langIdx = (langIdx + 1) % LANGS.length;
    try { localStorage.setItem('voice-lang', curLang()); } catch (_) {}
    refreshBadge(); status('🌐 识别语种:' + LANGS[langIdx].tag + '(' + curLang() + ')'); beacon('lang', { lang: curLang() });
  }

  // ════════════════ Web Audio 采集 + VAD ════════════════
  var epoch = 0;                  // 会话身份号:迟到回调按它失配丢弃
  var listening = false, stream = null, ac = null, sp = null, srcNode = null;
  var srcRate = 48000, seg = false, segChunks = [], preRoll = [], voiceFrames = 0, segStart = 0, lastVoice = 0;
  var queue = [], busy = false, ttsHold = 0, ttsTimer = 0;   // ttsHold:多播报源计数,>0 即静音 VAD(防串台)
  var START_RMS = 0.020, KEEP_RMS = 0.012, START_FRAMES = 2, SILENCE_MS = 800, MAX_MS = 9000, MIN_MS = 320, PREROLL = 4;

  function rms(buf) { var s = 0; for (var i = 0; i < buf.length; i++) s += buf[i] * buf[i]; return Math.sqrt(s / buf.length); }

  function onAudio(e) {
    if (!listening) return;
    var inp = e.inputBuffer.getChannelData(0);
    var r = rms(inp), now = Date.now();
    if (!seg) {
      preRoll.push(new Float32Array(inp)); if (preRoll.length > PREROLL) preRoll.shift();
      if (ttsHold > 0) { voiceFrames = 0; return; }          // 念回答/任务播报时不起新段(防自反馈)
      if (r > START_RMS) { voiceFrames++; if (voiceFrames >= START_FRAMES) { seg = true; segStart = now; lastVoice = now; segChunks = preRoll.slice(); preRoll = []; setState('hear'); } }
      else voiceFrames = 0;
    } else {
      segChunks.push(new Float32Array(inp));
      if (r > KEEP_RMS) lastVoice = now;
      if (now - lastVoice > SILENCE_MS || now - segStart > MAX_MS) endSeg(now);   // 静音收句 / 硬上限兜底
    }
  }

  function endSeg(now) {
    seg = false; voiceFrames = 0;
    var dur = now - segStart, chunks = segChunks; segChunks = [];
    if (listening && !busy && STATE !== 'off') setState('listen');
    if (dur < MIN_MS) return;                                // 太短=噪声,丢弃
    var wav = buildWav(chunks, srcRate);
    beacon('seg', { ms: dur, bytes: wav.size });
    queue.push({ wav: wav, ep: epoch }); pump();              // 带 epoch:跨会话残段可自鉴别丢弃
  }

  // PCM 合并 + 降采样到 16k + 编 LINEAR16 WAV
  function buildWav(chunks, sr) {
    var total = 0, i, j; for (i = 0; i < chunks.length; i++) total += chunks[i].length;
    var flat = new Float32Array(total), off = 0;
    for (i = 0; i < chunks.length; i++) { flat.set(chunks[i], off); off += chunks[i].length; }
    var ratio = sr / 16000, outLen = Math.floor(flat.length / ratio);
    var out = new Int16Array(outLen);
    for (i = 0; i < outLen; i++) {
      var start = Math.floor(i * ratio), end = Math.floor((i + 1) * ratio); if (end > flat.length) end = flat.length;
      var acc = 0, cnt = 0; for (j = start; j < end; j++) { acc += flat[j]; cnt++; }
      var v = cnt ? acc / cnt : 0; v = v < -1 ? -1 : v > 1 ? 1 : v;
      out[i] = v < 0 ? v * 0x8000 : v * 0x7FFF;
    }
    var buf = new ArrayBuffer(44 + out.length * 2), dv = new DataView(buf);
    function ws(o, s) { for (var k = 0; k < s.length; k++) dv.setUint8(o + k, s.charCodeAt(k)); }
    ws(0, 'RIFF'); dv.setUint32(4, 36 + out.length * 2, true); ws(8, 'WAVE');
    ws(12, 'fmt '); dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
    dv.setUint32(24, 16000, true); dv.setUint32(28, 16000 * 2, true); dv.setUint16(32, 2, true); dv.setUint16(34, 16, true);
    ws(36, 'data'); dv.setUint32(40, out.length * 2, true);
    var o2 = 44; for (i = 0; i < out.length; i++) { dv.setInt16(o2, out[i], true); o2 += 2; }
    return new Blob([buf], { type: 'audio/wav' });
  }

  // ── FIFO 队列:一次处理一段,忙时排队、停止后丢弃 ──
  async function pump() {
    if (busy || !queue.length || !listening) return;
    var item = queue.shift();
    if (item.ep !== epoch) { pump(); return; }                // 跨会话残段:丢弃,看下一个
    busy = true;
    var ep = epoch;
    try { await processSegment(item.wav, ep); } catch (_) {}
    if (ep !== epoch) return;                                 // stale:绝不归还非自己会话的 busy(否则击穿 FIFO)
    busy = false;
    if (listening) { if (STATE !== 'off' && STATE !== 'hear') setState('listen'); pump(); }
  }

  function tctl(ms) { var c = new AbortController(); var id = setTimeout(function () { try { c.abort(); } catch (_) {} }, ms); return { s: c.signal, clear: function () { clearTimeout(id); } }; }

  async function processSegment(wav, ep) {
    setState('busy'); status('✍️ 识别中…');
    var text = '';
    var t = tctl(30000), fd = new FormData();
    fd.append('audio', wav, 'rec.wav'); fd.append('lang', curLang());
    try { fd.append('hints', JSON.stringify(curHints())); } catch (_) {}
    try {
      var r = await fetch('/api/voice/transcribe', { method: 'POST', body: fd, signal: t.s });
      if (ep !== epoch) return;
      var d = await r.json();
      text = d.ok ? (d.text || '').trim() : '';
      if (!d.ok) { beacon('stt-fail', { e: (d.error || '').slice(0, 80) }); status('识别失败:' + (d.error || '')); }
    } catch (e) { beacon('stt-err', { e: String(e && e.message || e).slice(0, 80) }); }
    t.clear();
    if (ep !== epoch) return;
    if (!text) return;                                        // 没听清:不打扰,继续听
    beacon('stt-ok', { text: text });
    await handleCommand(text, ep);
  }

  async function handleCommand(text, ep) {
    setState('busy');
    say('<div class="vq">🗣 ' + escapeHtml(text) + '</div><div class="vs">🤔 思考中…</div>');
    var reply = '', actions = null, task = null, t = tctl(30000);
    try {
      var r = await fetch('/api/voice/agent', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ transcript: text, context: pageContext() }), signal: t.s,
      });
      if (ep !== epoch) { t.clear(); return; }
      var d = await r.json();
      if (d.ok) { reply = d.speak || ''; actions = d.client_actions; task = d.task || null; }
      else reply = '出错了:' + (d.error || '');
    } catch (e) { reply = (e && e.name === 'AbortError') ? '响应超时了' : ('出错了:' + (e && e.message || '')); }
    t.clear();
    if (ep !== epoch) return;
    runClientActions(actions);
    beacon('agent', { n: (actions && actions.length) || 0, task: task && task.kind });
    say('<div class="vq">🗣 ' + escapeHtml(text) + '</div>' + escapeHtml(reply));
    if (task) { startTask(task); return; }   // 综合任务:后台 worker 跑 + 独立轮询,结果只显示在浮窗(不念)
  }

  // ── 综合任务:派发 + 独立轮询播报(不绑 listening,锁屏/切后台恢复后仍能续播)──
  var _taskTimers = {};
  function startTask(task) {
    beacon('task-start', { kind: task.kind });
    fetch('/api/voice/task', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind: task.kind, params: task.params || {}, context: pageContext() }) })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (d && d.ok && d.task_id) pollTask(d.task_id, 0); else announce('没能开始这个任务'); })
      .catch(function () { announce('任务提交失败了'); });
  }
  function pollTask(id, n) {
    if (n > 100) { announce('这个任务等太久了,先这样'); delete _taskTimers[id]; return; }   // ~3.5min 上限
    fetch('/api/voice/task-status?id=' + encodeURIComponent(id))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.ok) { delete _taskTimers[id]; return; }
        if (d.status === 'running') {
          if (d.step) status('⏳ ' + d.step + '…');
          _taskTimers[id] = setTimeout(function () { pollTask(id, n + 1); }, 2000);
        } else {
          delete _taskTimers[id];
          beacon('task-done', { st: d.status });
          if (d.status === 'done') { announce(d.speak || '办好了'); runClientActions(d.client_actions); notify('语音助手 ✓', d.speak || '任务完成'); }
          else { announce('没办成:' + (d.error || '')); notify('语音助手', '任务没办成:' + (d.error || '')); }
        }
      })
      .catch(function () { _taskTimers[id] = setTimeout(function () { pollTask(id, n + 1); }, 3000); });
  }
  function announce(text) {   // 任务完成:只更新浮窗,不念(用户选了不要语音播报;iOS 异步/锁屏 TTS 也不可靠)
    say('<div>' + escapeHtml(text) + '</div>');
  }

  function speak(text, ep) {
    return new Promise(function (resolve) {
      if (!text || !window.speechSynthesis || ep !== epoch) { resolve(); return; }
      ttsHold++; setState('speak');
      var done = false;
      function fin() {
        if (done) return; done = true; clearTimeout(ttsTimer); ttsHold = Math.max(0, ttsHold - 1);
        if (ep === epoch) { try { window.speechSynthesis.cancel(); } catch (_) {} }  // 仅当仍是本会话才打断 TTS
        resolve();
      }
      try {
        window.speechSynthesis.cancel();
        var u = new SpeechSynthesisUtterance(text); u.lang = 'zh-CN'; u.rate = 1.05;
        u.onend = u.onerror = fin; window.speechSynthesis.speak(u);
      } catch (_) { fin(); return; }
      ttsTimer = setTimeout(fin, Math.min(15000, 1500 + text.length * 90));   // iOS onend 常不回调 → 超时兜底(存 id,停时清)
    });
  }

  // ── 开 / 关(getUserMedia 必须在点击同步栈里发起)──
  function startListening() {
    if (listening) return;
    ensureNotifyPerm();   // 在点击手势里申请通知权限(iOS 要求手势触发)
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) { status('此设备/浏览器不支持录音'); return; }
    var myEp = ++epoch;
    listening = true; queue = []; busy = false; seg = false; ttsHold = 0; preRoll = []; segChunks = []; voiceFrames = 0;
    setState('listen'); status('🎙 在听…(点一下停止,长按切语种)'); beacon('start', {}); prewarmBrain(false);
    navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } })
      .then(function (s) {
        if (myEp !== epoch) { try { s.getTracks().forEach(function (t) { t.stop(); }); } catch (_) {} return; }  // 已被停:释放
        stream = s;
        try {
          ac = new (window.AudioContext || window.webkitAudioContext)();
          if (ac.state === 'suspended') {   // resume 必须 await/catch:否则 reject 冒 unhandledrejection、或停在 suspended 成「静默死会话」空挂麦
            ac.resume().then(function () { beacon('ac-state', { s: ac && ac.state }); })
                       .catch(function (e) { beacon('ac-resume-fail', { e: String(e && e.message || e).slice(0, 60) }); });
          }
          srcRate = ac.sampleRate || 48000;
          srcNode = ac.createMediaStreamSource(s);
          sp = ac.createScriptProcessor(4096, 1, 1);
          var zero = ac.createGain(); zero.gain.value = 0;
          srcNode.connect(sp); sp.connect(zero); zero.connect(ac.destination);   // iOS:必须接 destination 否则读到静音
          sp.onaudioprocess = onAudio;
          beacon('mic-ok', { sr: srcRate });
        } catch (e) { beacon('mic-graph-err', { e: String(e && e.message || e).slice(0, 80) }); status('音频初始化失败'); stopListening(); }
      })
      .catch(function (e) {
        if (myEp !== epoch) return;
        beacon('mic-deny', { e: String(e && e.name || e) });
        status('麦克风权限被拒:' + (e && e.message || ''));
        listening = false; setState('off');
      });
  }

  function stopListening() {   // 每条退出路径都彻底释放,否则污染下次启动
    epoch++;                   // 作废所有在途回调
    listening = false; seg = false; busy = false; ttsHold = 0; queue = []; segChunks = []; preRoll = [];
    clearTimeout(ttsTimer);
    try { if (sp) { sp.onaudioprocess = null; sp.disconnect(); } } catch (_) {}
    try { if (srcNode) srcNode.disconnect(); } catch (_) {}
    try { if (stream) stream.getTracks().forEach(function (t) { t.stop(); }); } catch (_) {}
    try { if (ac) { var pr = ac.close(); if (pr && pr.catch) pr.catch(function () {}); } } catch (_) {}
    try { window.speechSynthesis && window.speechSynthesis.cancel(); } catch (_) {}
    sp = null; srcNode = null; stream = null; ac = null;
    setState('off'); beacon('stop', {}); prewarmBrain(true);
  }

  function toggle() { if (listening) stopListening(); else startListening(); }

  // 长按(480ms)切语种,短按 toggle
  var lpTimer = 0, lpFired = false;
  fab.addEventListener('pointerdown', function () { lpFired = false; lpTimer = setTimeout(function () { lpFired = true; cycleLang(); }, 480); });
  function clearLp() { clearTimeout(lpTimer); }
  fab.addEventListener('pointerup', clearLp);
  fab.addEventListener('pointercancel', clearLp);
  fab.addEventListener('pointerleave', clearLp);
  fab.addEventListener('click', function (e) { if (lpFired) { e.preventDefault(); return; } toggle(); });

  document.addEventListener('visibilitychange', function () { if (document.hidden && listening) stopListening(); });

  setState('off');
})();
