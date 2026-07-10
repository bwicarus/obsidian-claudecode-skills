/* rc-voicecall.js — 阅读器内「豆包实时语音通话」(RC.voicecall),整合进 AI 侧边栏。
 * 跟独立页 voice-call.html 同一条链路(wss /voice-rt → relay → 豆包 S2S),但活在阅读器页面里 →
 * relay 的意图旁路(「找视频」等)下发 client_action,直接操控当前页面(视频卡点开 RC.videoPlayer)。
 * 开启时带上 file/page → relay 把本页内容注入通话上下文(豆包知道你在读什么)。
 * UI 两形态:侧栏在(#asst-input 存在)→ 通话条**内嵌**在侧栏输入框上方 + 入口按钮 #asst-call 挤在语音输入旁;
 *   侧栏组件不在 → 右下角浮层兜底。意图执行的过程/结果写进侧栏对话流(asst-thread 气泡+视频卡),
 *   用户能看到"真的在操作",而不是只听豆包嘴上说。
 * 音频:上行 AudioWorklet 采麦克风→16k PCM 20ms 包;下行 PCM24k AudioBuffer 排队;event 450(用户开口)→清队打断。
 */
(function () {
  if (!window.RC) window.RC = {};
  if (RC.voicecall) return;

  var ws = null, ac = null, capNode = null, micStream = null, playT = 0, playing = [];
  var box = null, f32buf = new Float32Array(0), curAText = '';
  // mode:'agent'(默认,耳=豆包ASR/嘴=豆包TTS/大脑=侧栏助手完整管线) | 's2s'(豆包端到端,旧路保留)
  var mode = 'agent';
  var vt = { sent: 0, tail: '', sid: 0 };   // 语音 tap 状态:已消费的回答长度 / 未成句尾巴 / 句序号
  var pendingUtter = null;                   // 助手忙时到达的新话(覆盖式排队,回答完自动发)

  // ── ⑧ 通话生命周期独立于侧栏/网络波动 ──
  // 侧栏关闭(rc-sidedrawer.close)只是 CSS 滑出、DOM 不销毁,本就不碰通话;真正断话的是网络波动/
  // iOS 切后台把 ws 掐了 → 旧代码 onclose 一律当"已挂断"。改成:teardown 时摘掉 ws 回调(主动挂断
  // 不再触发 onclose;也根治 fresh 重连时旧 ws 的迟到 onclose 误杀新连接),还挂着回调的 onclose
  // 必然是**意外断线** → 指数退避自动重连(不带 fresh → relay 用存的 dialog_id 接续记忆,体验连续)。
  // 配合 Wake Lock 防通话中息屏 + visibilitychange 回前台恢复(音频会话 resume / 后台断的线立即重连)。
  var _userHung = true;                      // true=没在通话/用户主动挂断;false=通话中
  var _reconnT = null, _reconnPend = false, _reconnN = 0, _wakeLock = null;
  function _acquireWL() {
    try {
      if (!navigator.wakeLock || _wakeLock) return;
      navigator.wakeLock.request('screen').then(function (wl) {
        _wakeLock = wl; wl.addEventListener('release', function () { _wakeLock = null; });
      }).catch(function () {});
    } catch (e) {}
  }
  function _releaseWL() { try { if (_wakeLock) { _wakeLock.release(); _wakeLock = null; } } catch (e) {} }
  function _scheduleReconnect() {
    if (_reconnT || _reconnPend) return;
    teardown(false); _userHung = false;      // teardown 置回 true → 恢复"通话意外中断"态
    _reconnN++;
    if (_reconnN > 8) { _userHung = true; _reconnN = 0; setSt('连接断开(点 📞 重拨)'); taPlaceholder(null); return; }
    if (document.hidden) { _reconnPend = true; return; }   // 后台不空转烧退避次数,回前台立即连
    var wait = Math.min(8000, 600 * Math.pow(2, _reconnN - 1));
    setSt('连接断开,' + Math.ceil(wait / 1000) + 's 后自动重连…'); taPlaceholder('语音重连中…');
    _reconnT = setTimeout(function () {
      _reconnT = null; setSt('重连中…');
      start(toggle._opts || {}).then(function () { if (!ws) _scheduleReconnect(); });
    }, wait);
  }
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) return;
    try { if (ac && ac.state !== 'running') ac.resume(); } catch (e) {}   // iOS 回前台:音频会话可能被挂起
    if (ws) { _acquireWL(); return; }        // 通话还活着:wake lock 切后台被系统释放过 → 重新拿
    if (_reconnPend || (!_userHung && !_reconnT)) {   // 后台断的线 → 回来立即重连
      _reconnPend = false;
      setSt('重连中…'); start(toggle._opts || {}).then(function () { if (!ws) _scheduleReconnect(); });
    }
  });

  function injectCss() {
    if (document.getElementById('rc-vc-css')) return;
    var s = document.createElement('style'); s.id = 'rc-vc-css';
    s.textContent =
      '#rc-vc{position:fixed;right:14px;bottom:78px;z-index:2147482000;width:min(320px,88vw);background:rgba(13,19,34,.96);' +
      'border:1px solid #2a3a63;border-radius:14px;box-shadow:0 12px 40px rgba(0,0,0,.6);color:#dbe7ff;font-size:13px;overflow:hidden}' +
      '#rc-vc .vc-head{display:flex;align-items:center;gap:8px;padding:9px 12px;background:rgba(0,0,0,.3)}' +
      '#rc-vc .vc-st{flex:1;color:#8a9bb4;font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
      '#rc-vc .vc-x{background:none;border:none;color:#8a9bb4;font-size:15px;cursor:pointer;padding:2px 6px}' +
      '#rc-vc .vc-sub{padding:8px 12px;min-height:44px;max-height:120px;overflow-y:auto}' +
      '#rc-vc .vc-u{color:#9fcbff;margin-bottom:3px}' +
      '#rc-vc .vc-a{color:#dbe7ff}' +
      '#rc-vc .vc-vids{display:flex;gap:6px;overflow-x:auto;padding:0 12px 8px}' +
      '#rc-vc .vc-vid{flex:0 0 128px;cursor:pointer;background:#0d1322;border:1px solid #263255;border-radius:8px;overflow:hidden}' +
      '#rc-vc .vc-vid img{width:100%;height:72px;object-fit:cover;display:block}' +
      '#rc-vc .vc-vid div{font-size:11px;padding:4px 6px;line-height:1.3;max-height:33px;overflow:hidden}' +
      '#rc-vc .vc-mic{display:flex;justify-content:center;padding:10px 0 12px}' +
      '#rc-vc .vc-mic button{width:58px;height:58px;border-radius:50%;border:none;font-size:24px;cursor:pointer;background:#1a7f4b;color:#fff}' +
      '#rc-vc.on .vc-mic button{background:#b73a3a}' +
      // 内嵌形态:活在侧栏输入框上方(不再 fixed 右下角);挂断键缩小
      '#rc-vc.vc-inline{position:static;width:auto;margin:0 10px 6px;right:auto;bottom:auto;box-shadow:0 4px 16px rgba(0,0,0,.35)}' +
      '#rc-vc.vc-inline .vc-mic{padding:6px 0 8px}' +
      '#rc-vc.vc-inline .vc-mic button{width:44px;height:44px;font-size:19px}' +
      // 侧栏 composer 里的通话入口按钮(样式镜像 #asst-mic;通话中绿色呼吸)
      '#asst-call{background:#16203a;border:1px solid #2a3a63;color:#9fb4e0;width:42px;height:42px;border-radius:12px;cursor:pointer;flex:none;display:flex;align-items:center;justify-content:center;transition:background .2s,color .2s,border-color .2s,transform .1s;-webkit-tap-highlight-color:transparent}' +
      '#asst-call:active{transform:scale(.9)}' +
      '#asst-call.on{background:#1a7f4b;border-color:#1a7f4b;color:#fff;animation:vcCallPulse 1.6s ease-in-out infinite}' +
      // 播报中:蓝色快脉冲(盖过 .on 绿;user 开口打断后自动回绿)
      '#asst-call.speaking{background:#0a84ff;border-color:#0a84ff;color:#fff;animation:vcCallPulse 1s ease-in-out infinite}' +
      '@keyframes vcCallPulse{0%,100%{box-shadow:0 0 0 0 rgba(26,127,75,.5)}50%{box-shadow:0 0 0 7px rgba(26,127,75,0)}}' +
      // 工具调用状态按钮 + 详情弹层(v3-⑤)
      '#vc-tool-btn{background:#16203a;border:1px solid #2a3a63;color:#9fb4e0;width:42px;height:42px;border-radius:12px;cursor:pointer;flex:none;display:none;align-items:center;justify-content:center;font-size:18px;-webkit-tap-highlight-color:transparent}' +
      '#vc-tool-btn.ok{color:#34d399;border-color:#1f6b4a}' +
      '#vc-tool-btn.err{color:#f87171;border-color:#7f2a2a}' +
      '.vc-spin{width:15px;height:15px;border:2px solid #3a4a73;border-top-color:#9fcbff;border-radius:50%;display:inline-block;animation:vcSpin .8s linear infinite;vertical-align:-2px}' +
      '@keyframes vcSpin{to{transform:rotate(360deg)}}' +
      '#vc-tool-pop{position:fixed;right:14px;bottom:132px;z-index:2147482100;width:min(340px,90vw);max-height:50vh;overflow-y:auto;background:rgba(13,19,34,.97);border:1px solid #2a3a63;border-radius:12px;padding:6px;color:#dbe7ff;font-size:12px;box-shadow:0 12px 40px rgba(0,0,0,.6)}' +
      '#vc-tool-pop .vtp-item{padding:7px 8px;border-bottom:1px solid #1d2a4a}' +
      '#vc-tool-pop .vtp-item:last-child{border-bottom:none}' +
      '#vc-tool-pop .vtp-h{font-weight:600}' +
      '#vc-tool-pop .vtp-t{color:#8a9bb4;font-weight:400;font-size:11px}' +
      '#vc-tool-pop .vtp-b{color:#aab8d4;margin-top:3px;word-break:break-all;white-space:pre-wrap;max-height:120px;overflow-y:auto}';
    document.head.appendChild(s);
  }

  function esc(s) { var d = document.createElement('div'); d.textContent = String(s == null ? '' : s); return d.innerHTML; }
  function setSt(t) { if (box) box.querySelector('.vc-st').textContent = t; }
  function callBtnOn(on) { var b = document.getElementById('asst-call'); if (b) b.classList[on ? 'add' : 'remove']('on'); }
  function callBtnSpeaking(on) { var b = document.getElementById('asst-call'); if (b) b.classList[on ? 'add' : 'remove']('speaking'); }
  // agent 模式无浮层:ASR 转写直接进侧栏输入框,状态用 placeholder + 按钮特效表达
  var _origPh = null;
  function taEl() { return document.getElementById('asst-ta'); }
  function taSet(v) {
    var ta = taEl(); if (!ta) return;
    ta.value = v;
    try { ta.dispatchEvent(new Event('input', { bubbles: true })); } catch (e) {}   // 触发 autorow 自动调高
  }
  function taPlaceholder(v) {
    var ta = taEl(); if (!ta) return;
    if (v == null) { if (_origPh !== null) ta.placeholder = _origPh; return; }
    if (_origPh === null) _origPh = ta.placeholder || '';
    ta.placeholder = v;
  }
  // ── 意图操作写进侧栏对话流(让用户看到"真的在做",不是豆包嘴上说说)──
  function threadMsg(cls, text) {
    var th = document.getElementById('asst-thread'); if (!th) return null;
    var d = document.createElement('div'); d.className = 'asst-msg ' + cls; d.textContent = text;
    th.appendChild(d); th.scrollTop = th.scrollHeight; return d;
  }

  // ── 工具调用状态按钮(v3-⑤,用户设计):执行通知**不进侧栏对话流**,收敛到固定小按钮——
  //    调用中转圈、完成 ✓、出错 ⚠;点击弹出调用详情(工具/args/耗时/实际输出,仿「!」弹窗模式)。──
  var toolLog = [];   // 最近调用 {label,status,tool,args,took_s,result_brief,cached}
  function onToolStatus(p) {
    p = p || {};
    var b = document.getElementById('vc-tool-btn'); if (!b) return;
    if (p.status === 'running') {
      toolLog.unshift({ label: p.label || '工具', status: 'running' });
      if (toolLog.length > 8) toolLog.pop();
      b.style.display = 'flex'; b.className = 'running'; b.innerHTML = '<span class="vc-spin"></span>';
      b.title = '正在执行:' + (p.label || '工具');
    } else {
      var it = null;
      for (var i = 0; i < toolLog.length; i++) if (toolLog[i].status === 'running') { it = toolLog[i]; break; }
      if (!it) { it = {}; toolLog.unshift(it); if (toolLog.length > 8) toolLog.pop(); }
      it.status = p.status; it.tool = p.tool; it.label = p.label || it.label || p.tool || '工具';
      it.args = p.args; it.took_s = p.took_s; it.result_brief = p.result_brief; it.cached = p.cached;
      b.style.display = 'flex';
      b.className = (p.status === 'done') ? 'ok' : 'err';
      b.textContent = (p.status === 'done') ? '✓' : '⚠';
      b.title = it.label + (p.status === 'done' ? ' 完成' : ' 出错') + '(点击看详情)';
    }
    renderToolPop(false);
  }
  function renderToolPop(force) {
    var pop = document.getElementById('vc-tool-pop');
    if (!pop) return;
    if (pop.style.display === 'none' && !force) return;
    pop.innerHTML = toolLog.map(function (it) {
      var st = it.status === 'running' ? '<span class="vc-spin"></span>' : (it.status === 'done' ? '✓' : '⚠');
      var head = '<div class="vtp-h">' + st + ' ' + esc(it.label || it.tool || '') +
        (it.took_s != null ? ' <span class="vtp-t">' + esc(it.took_s) + 's</span>' : '') +
        (it.cached ? ' <span class="vtp-t">复用缓存</span>' : '') + '</div>';
      var body = '';
      try { if (it.args && Object.keys(it.args).length) body += '<div class="vtp-b">args: ' + esc(JSON.stringify(it.args)) + '</div>'; } catch (e) {}
      if (it.result_brief) body += '<div class="vtp-b">' + esc(it.result_brief) + '</div>';
      return '<div class="vtp-item">' + head + body + '</div>';
    }).join('') || '<div class="vtp-item">还没有工具调用</div>';
  }
  function setSub(who, text) {
    if (!box) return;
    var el = box.querySelector(who === 'u' ? '.vc-u' : '.vc-a');
    el.textContent = (who === 'u' ? '你: ' : '豆包: ') + text;
    var sub = box.querySelector('.vc-sub'); sub.scrollTop = sub.scrollHeight;
  }

  // ── 播放(PCM24k)+ 打断 ──
  function playPcm(buf) {
    if (!ac) return;
    var i16 = new Int16Array(buf), f32 = new Float32Array(i16.length);
    for (var i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;
    var ab = ac.createBuffer(1, f32.length, 24000);
    ab.copyToChannel(f32, 0);
    var src = ac.createBufferSource(); src.buffer = ab; src.connect(ac.destination);
    var t = Math.max(ac.currentTime + 0.02, playT);
    src.start(t); playT = t + ab.duration; playing.push(src);
    callBtnSpeaking(true);
    src.onended = function () { var k = playing.indexOf(src); if (k >= 0) playing.splice(k, 1); if (!playing.length) callBtnSpeaking(false); };
  }
  function stopPlayback() { playing.forEach(function (s) { try { s.stop(); } catch (e) {} }); playing = []; playT = 0; callBtnSpeaking(false); }

  // ── 采集(mic → 16k PCM 20ms/包)──
  var WORKLET = 'class C extends AudioWorkletProcessor{process(i){var c=i[0][0];if(c)this.port.postMessage(c.slice(0));return true}}registerProcessor("vccap",C);';
  function onCap(chunk, rate) {
    var m = new Float32Array(f32buf.length + chunk.length);
    m.set(f32buf); m.set(chunk, f32buf.length); f32buf = m;
    var need = Math.round(rate * 0.02);
    while (f32buf.length >= need) {
      var seg = f32buf.subarray(0, need); f32buf = f32buf.slice(need);
      var out = new Int16Array(320);
      for (var i = 0; i < 320; i++) {
        var v = seg[Math.min(need - 1, Math.floor(i * need / 320))];
        out[i] = Math.max(-32768, Math.min(32767, v * 32768));
      }
      if (ws && ws.readyState === 1) ws.send(out.buffer);
    }
  }

  // ── client_action 派发:relay 意图旁路的页面控制指令在**阅读器环境**执行 ──
  function dispatch(fn, args) {
    if (fn === 'renderVideos') { renderVids((args || [[]])[0] || []); return; }
    try { if (typeof window[fn] === 'function') window[fn].apply(null, args || []); } catch (e) {}
  }
  function renderVids(vids) {
    if (!vids || !vids.length) return;
    // 优先落侧栏对话流:状态气泡改文案 + rc-video 的 renderVideos 把可播放卡插在它后面(最后一个 asst-a)
    var th = document.getElementById('asst-thread');
    if (th) {   // 视频结果是**内容**(非通知),照旧进侧栏:先建承载气泡,rc-video 把可播放卡插它后面
      threadMsg('asst-a', '给你找到了 ' + vids.length + ' 个相关视频:');
      try { if (window.renderVideos) window.renderVideos(vids); } catch (e) {}
      if (box && box.classList.contains('vc-inline')) { setSt('通话中'); return; }   // 内嵌模式不再重复渲染小横条
    }
    if (!box) return;
    var host = box.querySelector('.vc-vids'); host.innerHTML = '';
    vids.forEach(function (v) {
      var d = document.createElement('div'); d.className = 'vc-vid';
      d.innerHTML = '<img loading="lazy" referrerpolicy="no-referrer" src="' + esc(v.thumb || '') + '"><div>' + esc(v.title || '') + '</div>';
      d.addEventListener('click', function () {
        var bili = (v.src === 'bili' || /^BV[0-9A-Za-z]{10}/.test(v.id || ''));
        if (window.RC && RC.videoPlayer) RC.videoPlayer.open({ id: v.id, src: (bili ? 'bili' : 'yt'), title: v.title });
        else window.open((bili ? 'https://www.bilibili.com/video/' : 'https://www.youtube.com/watch?v=') + encodeURIComponent(v.id), '_blank');
      });
      host.appendChild(d);
    });
  }

  // ── agent 模式:嘴(把助手的流式回答按句喂 TTS)──
  function cleanForSpeech(s) {   // markdown → 适合朗读的纯文本(粗清;prompt 已让 AI 口语化,这里兜底)
    return String(s || '')
      .replace(/```[\s\S]*?```/g, ' 代码略 ')
      .replace(/\$\$?([^$]{1,120})\$\$?/g, '$1')
      .replace(/!\[[^\]]*\]\([^)]*\)/g, '')
      .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
      .replace(/[#*_`>|~]+/g, ' ')
      .replace(/\s+/g, ' ').trim();
  }
  function speak(text) {
    var t = cleanForSpeech(text);
    if (!t || !ws || ws.readyState !== 1) return;
    try { ws.send(JSON.stringify({ type: 'speak', text: t, id: ++vt.sid })); } catch (e) {}
  }
  function bargeIn() {   // 打断:清本地播放队列 + 作废 relay 侧排队/在流的合成
    stopPlayback();
    try { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'cancel' })); } catch (e) {}
  }
  // 助手流式回答 tap(rc-assistant 在 answer 增量/收尾时调):按标点小片段即时喂 TTS
  //   (双向流式 session:relay 侧一轮回答一个 session,片段连续合成 → 韵律连贯,首句无需等全文)
  function speakOn() { try { return localStorage.getItem('rc-voice-speak') === '1'; } catch (e) { return false; } }
  window.__asstVoiceTap = function (full, done) {
    if (mode !== 'agent' || !ws) return;
    if (!speakOn()) return;   // 默认静默:读比听快(用户拍板)。「🔊 朗读」开关点亮才合成,关着零 TTS 成本
    full = String(full || '');
    if (full.length < vt.sent) { vt.sent = 0; vt.tail = ''; }   // 新一轮回答开始
    vt.tail += full.slice(vt.sent); vt.sent = full.length;
    var re = /[^。！？!?;；,，\n]+[。！？!?;；,，\n]+/g, m, consumed = 0;   // 逗号级边界:更早开始出声
    while ((m = re.exec(vt.tail))) { speak(m[0]); consumed = re.lastIndex; }
    vt.tail = vt.tail.slice(consumed);
    if (done) {
      if (vt.tail.trim()) speak(vt.tail);
      vt.sent = 0; vt.tail = '';
      try { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'speak_done' })); } catch (e) {}   // FinishSession:让尾巴合成完
      if (pendingUtter) { var p = pendingUtter; pendingUtter = null; sendToAssistant(p, true); }   // 排队的下一句(别掐掉刚念的尾巴)
    }
  };
  window.__asstVoiceOn = function () { return mode === 'agent' && !!ws; };   // rc-assistant 据此给后端带 voice 标志
  function sendToAssistant(text, keepAudio) {
    if (!keepAudio) bargeIn();        // 新问题:停掉还在念的旧回答(排队派发除外)
    vt.sent = 0; vt.tail = '';
    if (window.__asstBusy && window.__asstBusy()) { pendingUtter = text; taPlaceholder('⏳ 上一条还在答,已排队:' + text.slice(0, 14) + '…'); return; }
    taPlaceholder('🎙 说话即可,松口自动发送…');
    if (window.__asstSend) window.__asstSend(text);
    else threadMsg('asst-note', '⚠ 助手未加载,请刷新页面');
  }
  function handleAgentMsg(m) {
    var p = m.payload || {};
    if (m.event === 'agent_ready') { taSet(''); taPlaceholder('🎙 说话即可,松口自动发送…'); return; }
    if (m.event === 'asr') {   // 进行中转写 → 直接写输入框;用户开口 → 立即打断播报
      if (playing.length) bargeIn();
      taSet(p.text || ''); return;
    }
    if (m.event === 'utterance' && p.text) { taSet(''); sendToAssistant(p.text); return; }
    if (m.event === 'tts_end') return;
    if (m.event === -1 || p.error) threadMsg('asst-note', '⚠ 语音:' + (p.error || '').slice(0, 80));
  }

  async function start(opts) {
    opts = opts || {};
    // 新连接 = relay 端 book 状态全新 → 指纹清零,让 __vcSyncNow 下一轮把选中/墨迹/页码重推上去
    // (旧代码重连/🧹后指纹残留 → 状态永不重推,relay 不知道选中和圈画)
    _stateFp = null; _inkFp = '';
    try {
      ac = new (window.AudioContext || window.webkitAudioContext)();
      await ac.resume();   // iOS:须在手势内
      micStream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
      await ac.audioWorklet.addModule(URL.createObjectURL(new Blob([WORKLET], { type: 'text/javascript' })));
      var src = ac.createMediaStreamSource(micStream);
      capNode = new AudioWorkletNode(ac, 'vccap');
      capNode.port.onmessage = function (e) { onCap(e.data, ac.sampleRate); };
      src.connect(capNode);
      var qs = (mode === 'agent')
        ? '?mode=agent'
        : '?file=' + encodeURIComponent(opts.file || '') + '&page=' + (opts.page || 0) + (toggle._fresh ? '&fresh=1' : '');
      toggle._fresh = false;
      var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
      ws = new WebSocket(proto + location.host + '/voice-rt' + qs);
      ws.binaryType = 'arraybuffer';
      ws.onopen = function () {
        _userHung = false; _reconnN = 0; _reconnPend = false; _acquireWL();
        setSt('通话中 · 说话即可(已带上本页内容)'); if (box) box.classList.add('on'); callBtnOn(true);
        taPlaceholder(mode === 'agent' ? '通话中,说话即可…' : null);
      };
      // teardown 会摘掉本回调 → 走到这里的必然是"没人主动挂断"的意外断线(网络波动/iOS 切后台掐 ws)
      ws.onclose = function () { _scheduleReconnect(); };
      ws.onerror = function () { setSt('连接出错'); };
      ws.onmessage = function (ev) {
        if (ev.data instanceof ArrayBuffer) { playPcm(ev.data); return; }
        var m; try { m = JSON.parse(ev.data); } catch (e) { return; }
        if (mode === 'agent') { handleAgentMsg(m); return; }
        var p = m.payload || {};
        if (m.event === 'client_action') { dispatch(p.fn, p.args); return; }
        if (m.event === 'tool_status') { onToolStatus(p); return; }   // 执行通知 → 固定状态按钮(不进对话流,用户设计)
        if (m.event === -1 || m.event === 153 || m.event === 599 || m.code) { setSt('⚠ ' + (p.error || p.message || '').slice(0, 60)); return; }
        if (m.event === 150) setSt('通话中(会话已建立)');
        else if (m.event === 359 && p.status_code === '20000002') { setSt('👋 好,下次再聊'); setTimeout(function () { teardown(true); }, 2500); }   // 说"挂了吧/再见"→识别退出意图→播完告别语自动挂断
        else if (m.event === 450) {   // 用户开口:打断播报 + **立即同步一次上下文**(墨迹/选中,赶在模型答题前——治刚画完就问的竞态)
          stopPlayback(); curAText = '';
          try { window.__vcSyncNow && window.__vcSyncNow(); } catch (e) {}
        }
        else if (m.event === 451) { var r = (p.results || [])[0] || {}; if (r.text && r.is_interim === false) setSub('u', r.text); }
        else if (m.event === 550) { curAText += (p.content || ''); setSub('a', curAText); }
      };
    } catch (ex) { setSt('启动失败: ' + ex.message); teardown(false); }
  }

  function teardown(closeBox) {
    _userHung = true; _releaseWL();
    if (_reconnT) { clearTimeout(_reconnT); _reconnT = null; }
    _reconnPend = false;
    // 摘回调再关:主动挂断不触发 onclose(重连逻辑只认"意外断");也防 fresh 重连时旧 ws 迟到的 onclose 误杀新连接
    try { if (ws) { ws.onclose = ws.onerror = ws.onmessage = null; if (ws.readyState === 1) { ws.send(JSON.stringify({ type: 'finish' })); } ws.close(); } } catch (e) {}
    ws = null; stopPlayback();
    try { if (capNode) capNode.disconnect(); } catch (e) {}
    try { if (micStream) micStream.getTracks().forEach(function (t) { t.stop(); }); } catch (e) {}
    try { if (ac) ac.close(); } catch (e) {}
    ac = null; capNode = null; micStream = null; f32buf = new Float32Array(0); curAText = '';
    vt.sent = 0; vt.tail = ''; pendingUtter = null;
    callBtnOn(false); callBtnSpeaking(false); taPlaceholder(null);
    if (box) { box.classList.remove('on'); if (closeBox) { box.remove(); box = null; } }
  }

  function toggle(opts) {
    injectCss();
    mode = (opts && opts.mode) || 'agent';
    if (ws) { teardown(false); setSt('已挂断(再点 📞 重新通话)'); return; }
    if (mode === 'agent') {   // agent 模式无浮层:状态全靠按钮特效(绿=在听/蓝=在念)+ 输入框(转写/placeholder)
      toggle._opts = opts || {};
      taPlaceholder('连接语音…');
      start(toggle._opts);
      return;
    }
    if (!box) {
      box = document.createElement('div'); box.id = 'rc-vc';
      box.innerHTML = '<div class="vc-head"><span>🎙</span><span class="vc-st">连接中…</span><button class="vc-new" title="新话题:清空对话记忆重新开始(书页上下文保留,忘掉之前聊的全部内容)">🧹</button><button class="vc-x">✕</button></div>' +
        '<div class="vc-sub"><div class="vc-u"></div><div class="vc-a"></div></div>' +
        '<div class="vc-vids"></div>' +
        '<div class="vc-mic"><button title="挂断/重拨">📞</button></div>';
      // 侧栏在 → 通话条内嵌在输入框上方(跟对话流一体);侧栏组件不在 → 右下角浮层兜底
      var asstInput = document.getElementById('asst-input');
      if (asstInput && asstInput.parentNode) { box.classList.add('vc-inline'); asstInput.parentNode.insertBefore(box, asstInput); }
      else document.body.appendChild(box);
      box.querySelector('.vc-x').addEventListener('click', function () { teardown(true); });
      box.querySelector('.vc-new').addEventListener('click', function () {   // 🧹 新话题:挂断 → 带 fresh=1 重连(relay 清 dialog_id + 不带历史)
        teardown(false);
        toggle._fresh = true;
        setSt('已清空记忆,重新开始…');
        start(toggle._opts || {});
      });
      box.querySelector('.vc-mic button').addEventListener('click', function () {
        if (ws) { teardown(false); setSt('已挂断'); } else { setSt('连接中…'); start(toggle._opts || {}); }
      });
    }
    toggle._opts = opts || {};
    try { box.querySelector('.vc-new').style.display = (mode === 's2s') ? '' : 'none'; } catch (e) {}   // 🧹 是 S2S 记忆专用;agent 模式用助手自己的「清空」
    setSt('连接中…');
    start(toggle._opts);
  }

  // 翻页同步:仅 s2s 模式需要(agent 模式每次发送时侧栏 ctx() 自带当前页,天然跟页走)
  function setPage(page) {
    if (mode !== 's2s') return;
    if (!ws || ws.readyState !== 1 || !page) return;
    var o = toggle._opts || {};
    if (page === o.page) return;
    o.page = page;
    _inkFp = '';   // 换页后墨迹指纹作废(新页的墨迹要重新同步)
    try { ws.send(JSON.stringify({ type: 'page', page: page })); setSt('通话中 · 已同步到第 ' + page + ' 页'); } catch (e) {}
  }

  // 选中/chip 状态同步(与侧栏 __voiceContext 同源):选中文字/钉住焦点/带入图 → relay 热更 SP。
  // 指纹去重:变化(含变空=取消选中/点掉 chip)才发;relay 端再比对,真变了才 UpdateConfig。
  var _stateFp = null;
  function syncState(state) {
    if (mode !== 's2s' || !ws || ws.readyState !== 1) return;
    state = state || {};
    var fp; try { fp = JSON.stringify(state); } catch (e) { fp = String(state.sel || '') + '|' + (state.figs || 0); }
    if (fp === _stateFp) return;
    var first = (_stateFp === null);
    _stateFp = fp;
    if (first && !(state.sel || state.focus || state.figs)) return;   // 首轮全空:不发(SP 本来就没有)
    try { ws.send(JSON.stringify({ type: 'state', sel: state.sel || '', focus: state.focus || '', figs: state.figs || 0 })); } catch (e) {}
  }

  // 侧栏「🗑 清空」→ S2S 记忆同步清空(fresh 重连;共享/native 两版按钮都是 data-q="clear",捕获阶段旁听不拦截)
  document.addEventListener('click', function (e) {
    try {
      var b = e.target && e.target.closest ? e.target.closest('[data-q="clear"]') : null;
      if (!b || mode !== 's2s' || !ws) return;
      teardown(false);
      toggle._fresh = true;
      taPlaceholder('对话已清空,语音记忆重置中…');
      start(toggle._opts || {});
    } catch (_) {}
  }, true);

  // 通话中圈画同步:21-misc-ai 轮询把当前页**内存实时墨迹**推过来(不等 sidecar 防抖保存,对齐侧栏 ctx["ink"] 机制),
  // 变了才发(指纹去重)→ relay 提取圈下文字 + 热更新 SP → 豆包知道"本页有你的标注"。
  var _inkFp = '';
  function syncInk(page, strokes) {
    if (mode !== 's2s' || !ws || ws.readyState !== 1 || !page) return;
    strokes = strokes || [];
    var fp; try { fp = page + ':' + strokes.length + ':' + JSON.stringify(strokes).length; } catch (e) { fp = page + ':' + strokes.length; }
    if (fp === _inkFp) return;
    var first = (_inkFp === '');
    _inkFp = fp;
    if (first && !strokes.length) return;   // 首次空态只记指纹(没圈过东西不必更新 SP)
    try { ws.send(JSON.stringify({ type: 'ink', page: page, strokes: strokes.slice(0, 60) })); setSt('通话中 · 已同步你的圈画'); } catch (e) {}
  }

  // ── 入口按钮:注入侧栏 composer,挤在语音输入 #asst-mic 旁(SF 电话线条图标;通话中绿色呼吸)──
  function injectBtn() {
    var input = document.getElementById('asst-input');
    if (!input) return false;
    if (document.getElementById('asst-call')) return true;
    injectCss();
    var b = document.createElement('button');
    b.id = 'asst-call'; b.type = 'button';
    b.title = '语音对话(说话=直接问助手,回答会朗读出来;查词/找视频/制卡/高亮等工具全可用,过程显示在对话里)';
    b.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M5 5.3C5 14.8 9.2 19 18.7 19c.72 0 1.3-.58 1.3-1.3v-2.35c0-.56-.36-1.06-.9-1.23l-2.62-.87a1.3 1.3 0 0 0-1.33.32l-.95.95a11.6 11.6 0 0 1-4.72-4.72l.95-.95c.35-.35.47-.87.32-1.33L9.85 4.9A1.3 1.3 0 0 0 8.62 4H6.3C5.58 4 5 4.58 5 5.3z"/></svg>';
    var mic = document.getElementById('asst-mic');
    if (mic && mic.parentNode === input) input.insertBefore(b, mic.nextSibling);
    else input.insertBefore(b, input.firstChild);
    // 短按=语音输入(agent);长按 600ms=S2S 伴读通话(端到端语音对话:自然闲聊+说"找视频/翻到第N页"它真执行)
    var _lpT = null, _lpFired = false;
    b.addEventListener('pointerdown', function () {
      _lpFired = false;
      _lpT = setTimeout(function () {
        _lpFired = true;
        try { navigator.vibrate && navigator.vibrate(15); } catch (e) {}
        if (ws) { teardown(false); }   // 先挂掉现有(不管哪种模式)再开 s2s
        if (window._voiceCallS2S) window._voiceCallS2S(); else toggle({ mode: 's2s' });
      }, 600);
    });
    ['pointerup', 'pointercancel', 'pointerleave'].forEach(function (evn) {
      b.addEventListener(evn, function () { if (_lpT) { clearTimeout(_lpT); _lpT = null; } });
    });
    b.addEventListener('contextmenu', function (e) { e.preventDefault(); });   // 长按不弹菜单(iOS)
    b.addEventListener('click', function () {
      if (_lpFired) { _lpFired = false; return; }   // 长按已处理,吞掉随后的 click
      // 21-misc-ai 的 _voiceCall 带 FILE_REL/currentPage(模块作用域);没有就裸开(无书页上下文)
      if (window._voiceCall) window._voiceCall(); else toggle({});
    });
    // 工具调用状态按钮(v3-⑤):挤在 📞 右边;点击开合详情弹层
    var tb = document.createElement('button');
    tb.id = 'vc-tool-btn'; tb.type = 'button'; tb.title = '工具调用状态';
    input.insertBefore(tb, b.nextSibling);
    var pop = document.createElement('div');
    pop.id = 'vc-tool-pop'; pop.style.display = 'none';
    document.body.appendChild(pop);
    tb.addEventListener('click', function () {
      var show = pop.style.display === 'none';
      if (show) renderToolPop(true);
      pop.style.display = show ? 'block' : 'none';
    });
    return true;
  }
  // 「🔊 朗读」开关:挤进侧栏快捷栏(蹭 rc-media-tg 样式,与「书页/配图/视频」同排)。默认关=回答只出文字。
  function injectSpeakToggle() {
    var qb = document.getElementById('asst-quick');
    if (!qb) return false;
    if (qb.querySelector('.vc-speak-tg')) return true;
    var b = document.createElement('button'); b.type = 'button'; b.className = 'rc-media-tg vc-speak-tg';
    b.innerHTML = '<span>🔊 朗读</span>';
    b.title = '语音对话时是否朗读回答。默认关(读比听快);点亮后回答边生成边念,你一开口它就闭嘴';
    if (speakOn()) b.classList.add('on');
    b.addEventListener('click', function () {
      var on = b.classList.toggle('on');
      try { localStorage.setItem('rc-voice-speak', on ? '1' : '0'); } catch (e) {}
      if (!on) bargeIn();   // 关掉的瞬间静音
    });
    qb.appendChild(b);
    return true;
  }

  // 侧栏 pane 注入时机不定(rc-assistant 加载在前,但保守起见轮询到出现为止)
  if (!(injectBtn() && injectSpeakToggle())) {
    var _tries = 0, _t = setInterval(function () { if ((injectBtn() && injectSpeakToggle()) || ++_tries > 40) clearInterval(_t); }, 750);
  }

  RC.voicecall = { toggle: toggle, isOpen: function () { return !!ws; }, setPage: setPage, syncInk: syncInk, syncState: syncState };
})();
