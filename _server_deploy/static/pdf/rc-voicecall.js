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

  var PAGE_CARD_CONTENT_LIMIT = 100000;
  var ws = null, ac = null, capNode = null, micStream = null, playT = 0, playing = [];
  var box = null, f32buf = new Float32Array(0), curAText = '';
  var _computerVoiceUnsub = null;
  // 宿主只负责解析服务地址；语音状态机/侧栏/工具循环仍只有本文件一份。
  // PWA 是同源所以走 fallback，扩展在任意站点由 WebAdapter 宿主把 /voice-rt 指回阅读器服务。
  function _wsUrl(path) {
    try {
      if (typeof window.__bwReaderWsUrl === 'function') return window.__bwReaderWsUrl(path);
    } catch (e) {}
    return (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + path;
  }
  // 状态机只依赖浏览器 WebSocket 接口，不关心连接由谁建立：
  // · PWA 没有 hook，继续原生同源 WebSocket；
  // · 扩展普通网页受宿主页 CSP 限制，由 facade 提供同接口、后台代建连接。
  // 这样语音/侧栏仍只有本文件一份，扩展没有第二套通话实现。
  function _openWs(path) {
    // hook 存在就让错误显式冒泡；不能静默降级成宿主页直连，否则既重新撞 CSP，
    // 也会绕过扩展后台的账户/路由围栏。
    if (typeof window.__bwReaderOpenWebSocket === 'function') {
      return window.__bwReaderOpenWebSocket(path);
    }
    return new WebSocket(_wsUrl(path));
  }
  // mode:'agent'(默认,耳=豆包ASR/嘴=豆包TTS/大脑=侧栏助手完整管线) | 's2s'(豆包端到端,旧路保留)
  var mode = 'agent';
  // BWReader App 中 agent 模式只把媒体层交给原生代码；识别终稿仍回到
  // handleAgentMsg → __asstSend，模型选择、工具、历史和对话 UI 继续复用网页助手。
  // Safari/PWA/普通浏览器没有此 capability，原 WebAudio + /voice-rt 路径逐字保留。
  var _nativeAgent = { phase: 'idle', active: false, busy: false, speaking: false };
  var _nativeAgentWatchdog = null;
  var _nativeAgentWatchdogKind = '';
  function _extensionNativeAgentBridge() {
    try {
      var bridge = window.__bwNativeAgentVoiceExtensionBridge;
      return bridge && typeof bridge.available === 'function' &&
        bridge.available() === true && typeof bridge.post === 'function'
          ? bridge : null;
    } catch (e) { return null; }
  }
  function _nativeAgentAvailable() {
    try {
      var appWebView = window.__BW_NATIVE_AGENT_VOICE__ === true &&
        !!(window.webkit && window.webkit.messageHandlers &&
           window.webkit.messageHandlers.bwNativeAgentVoice);
      return appWebView || !!_extensionNativeAgentBridge();
    } catch (e) { return false; }
  }
  function _nativeAgentEngaged() {
    return _nativeAgent.active === true || _nativeAgent.busy === true;
  }
  function _nativeAgentResetLocal(message) {
    if (_nativeAgentWatchdog) clearTimeout(_nativeAgentWatchdog);
    _nativeAgentWatchdog = null;
    _nativeAgentWatchdogKind = '';
    _nativeAgent = { phase: 'idle', active: false, busy: false, speaking: false };
    pendingUtter = null; activeUtter = '';
    callBtnOn(false); callBtnConnecting(false); callBtnSpeaking(false);
    taPlaceholder(null);
    if (message) {
      setSt(message);
      try { RC.toast(message); } catch (e) {}
    }
  }
  function _nativeAgentArmWatchdog(kind) {
    if (_nativeAgentWatchdog) clearTimeout(_nativeAgentWatchdog);
    _nativeAgentWatchdogKind = kind === 'stop' ? 'stop' : 'start';
    _nativeAgentWatchdog = setTimeout(function () {
      var timedOutKind = _nativeAgentWatchdogKind;
      _nativeAgentWatchdog = null;
      _nativeAgentWatchdogKind = '';
      if (timedOutKind === 'stop' && _nativeAgent.active) {
        _nativeAgent.busy = false;
        callBtnConnecting(false);
        setSt('原生语音挂断状态未知，请再点一次');
        try { RC.toast('原生语音挂断状态未知，请再点一次'); } catch (e) {}
      } else {
        _nativeAgentResetLocal('BWReader App 原生语音未返回状态，请重试');
      }
    }, 45000);
  }
  function _nativeAgentPost(body) {
    if (!_nativeAgentAvailable()) return false;
    try {
      if (window.webkit && window.webkit.messageHandlers &&
          window.webkit.messageHandlers.bwNativeAgentVoice) {
        window.webkit.messageHandlers.bwNativeAgentVoice.postMessage(body);
        return true;
      }
      var bridge = _extensionNativeAgentBridge();
      if (!bridge) return false;
      bridge.post(body).catch(function (error) {
        _nativeAgentResetLocal(
          (error && error.message) || 'BWReader App 原生语音请求失败'
        );
      });
      return true;
    } catch (e) { return false; }
  }
  function _nativeAgentStart(opts) {
    opts = opts || {};
    _nativeAgent.busy = true;
    callBtnConnecting(true);
    taPlaceholder('连接原生语音…');
    var posted = _nativeAgentPost({
      action: 'start',
      file: String(opts.file || ''),
      page: Number(opts.page || 0)
    });
    if (!posted) {
      _nativeAgentResetLocal(null);
      return false;
    }
    _nativeAgentArmWatchdog('start');
    return true;
  }
  function _nativeAgentStop() {
    _nativeAgent.busy = true;
    var posted = _nativeAgentPost({ action: 'stop' });
    if (!posted) {
      _nativeAgent.busy = false;
      callBtnConnecting(false);
      setSt('无法联系 BWReader App 原生语音');
      try { RC.toast('无法联系 BWReader App 原生语音'); } catch (e) {}
      return false;
    }
    _nativeAgentArmWatchdog('stop');
    return true;
  }
  window.addEventListener('bw-native-agent-voice-event', function (event) {
    var message = (event && event.detail) || {};
    var payload = message.payload || {};
    if (message.event === 'state') {
      if (_nativeAgentWatchdog) clearTimeout(_nativeAgentWatchdog);
      _nativeAgentWatchdog = null;
      _nativeAgentWatchdogKind = '';
      _nativeAgent.phase = String(payload.phase || 'idle');
      _nativeAgent.active = payload.active === true;
      _nativeAgent.busy = payload.busy === true;
      _nativeAgent.speaking = payload.speaking === true;
      callBtnOn(_nativeAgent.active);
      callBtnConnecting(_nativeAgent.busy && !_nativeAgent.active);
      callBtnSpeaking(_nativeAgent.speaking);
      if (_nativeAgent.busy) {
        _nativeAgentArmWatchdog(
          _nativeAgent.phase === 'stopping' ? 'stop' : 'start'
        );
      }
      if (_nativeAgent.phase === 'failed') {
        setSt('启动失败: ' + String(payload.detail || '原生语音连接失败'));
        taPlaceholder(null);
        try { RC.toast(String(payload.detail || '原生语音连接失败')); } catch (e) {}
      } else if (_nativeAgent.phase === 'idle') {
        taPlaceholder(null);
      } else if (_nativeAgent.phase === 'listening') {
        setSt('通话中 · 原生后台音频');
        taPlaceholder('🎙 连续听中,说话即可…');
      } else if (payload.detail) {
        setSt(String(payload.detail));
      }
      return;
    }
    // Keep one agent event consumer. Native and browser transports feed the
    // exact same ASR/utterance/TTS envelope into the existing assistant flow.
    // Native reports tts_seg when AVAudioEngine receives that segment's first
    // PCM block, so its subtitle can be shown immediately instead of waiting
    // for the browser AudioContext chunk binder that this path intentionally
    // does not use.
    if (message.event === 'tts_seg') {
      if (payload.text) capShow(payload.text);
      return;
    }
    handleAgentMsg(message);
  });
  var vt = { sent: 0, tail: '', sid: 0, pref: '' };   // 语音 tap 状态:已消费长度 / 未成句尾巴 / 句序号 / 上次 full(前缀判定轮次替换)
  var pendingUtter = null;                   // 助手忙时到达的新话(覆盖式排队,回答完自动发)
  var activeUtter = '';                      // 当前正在回答的终稿指纹；拦同一 ASR 终稿的重复投递
  function _utterKey(text) { return String(text || '').trim().replace(/\s+/g, ' '); }
  var _reviewVoiceHint = '复习模式暂不启动实时语音通话；普通听写和朗读仍可使用';
  function _assistantInReview() {
    try {
      return !!(window.RC && RC.assistant && RC.assistant.getMode &&
        RC.assistant.getMode() === 'review');
    } catch (e) { return false; }
  }
  function _voiceReviewNotice() {
    try {
      if (window.RC && RC.toast) RC.toast(_reviewVoiceHint);
    } catch (e) {}
  }
  function _rememberVoiceTitle(el) {
    if (!el || !el.dataset || el.dataset.vcNormalTitle != null) return;
    el.dataset.vcNormalTitle = el.title || '';
    el.dataset.vcNormalAriaLabel = el.getAttribute('aria-label') || '';
  }
  // 电话按钮是软禁用：保留键盘焦点和点击反馈，但绝不进入拨号逻辑。
  // 麦克风仍可单击走系统听写，只把长按连续 ASR 标成不可用。
  function _syncReviewVoiceUi() {
    var blocked = _assistantInReview();
    ['asst-call', 'vc-top-call', 'asst-computer', 'vc-top-computer'].forEach(function (id) {
      var el = document.getElementById(id);
      if (!el) return;
      _rememberVoiceTitle(el);
      el.classList.toggle('vc-review-disabled', blocked);
      el.setAttribute('aria-disabled', blocked ? 'true' : 'false');
      el.title = blocked ? _reviewVoiceHint : (el.dataset.vcNormalTitle || '');
      var normalLabel = el.dataset.vcNormalAriaLabel || el.dataset.vcNormalTitle || '实时语音通话';
      el.setAttribute('aria-label', blocked ? _reviewVoiceHint : normalLabel);
    });
    ['asst-mic', 'vc-top-mic'].forEach(function (id) {
      var el = document.getElementById(id);
      if (!el) return;
      _rememberVoiceTitle(el);
      el.dataset.vcLongpress = blocked ? 'disabled-in-review' : 'enabled';
      el.title = blocked
        ? '语音输入：单击仍为系统听写；复习模式不启用长按连续语音'
        : (el.dataset.vcNormalTitle || '');
      el.setAttribute(
        'aria-description',
        blocked ? '单击系统听写可用；长按连续语音在复习模式不可用' : ''
      );
    });
    return blocked;
  }
  function _reviewVoiceGate(notify) {
    if (!_assistantInReview()) return false;
    _syncReviewVoiceUi();
    if (notify !== false) _voiceReviewNotice();
    return true;
  }

  // ── ⑧ 通话生命周期独立于侧栏/网络波动 ──
  // 侧栏关闭(rc-sidedrawer.close)只是 CSS 滑出、DOM 不销毁,本就不碰通话;真正断话的是网络波动/
  // iOS 切后台把 ws 掐了 → 旧代码 onclose 一律当"已挂断"。改成:teardown 时摘掉 ws 回调(主动挂断
  // 不再触发 onclose;也根治 fresh 重连时旧 ws 的迟到 onclose 误杀新连接),还挂着回调的 onclose
  // 必然是**意外断线** → 指数退避自动重连(不带 fresh → relay 用存的 dialog_id 接续记忆,体验连续)。
  // 配合 Wake Lock 防通话中息屏 + visibilitychange 回前台恢复(音频会话 resume / 后台断的线立即重连)。
  var _userHung = true;                      // true=没在通话/用户主动挂断;false=通话中
  var _reconnT = null, _reconnPend = false, _reconnN = 0, _wakeLock = null;
  var _gen = 0;                              // 连接世代(teardown/start 推进;在飞的旧 start 过期自毁)
  function _acquireWL() {
    try {
      if (!navigator.wakeLock || _wakeLock) return;
      navigator.wakeLock.request('screen').then(function (wl) {
        _wakeLock = wl; wl.addEventListener('release', function () { _wakeLock = null; });
      }).catch(function () {});
    } catch (e) {}
  }
  function _releaseWL() { try { if (_wakeLock) { _wakeLock.release(); _wakeLock = null; } } catch (e) {} }
  function _tryStart() {
    if (_reviewVoiceGate(false)) {
      _userHung = true; _reconnPend = false;
      return;
    }
    // 一次重连尝试 = start + watchdog:start 可能整个卡死(iOS 非手势 resume 永远 pending 等)
    // → .then 永不触发,没有 watchdog 就永远停在"重连中…"。12s 没建成 ws 视为卡死,推进下一轮
    // (_scheduleReconnect→teardown 会 ++_gen,把挂着的旧 start 判死,不会复活)。
    setSt('重连中…'); taPlaceholder('语音重连中…');
    var g0 = _gen + 1;   // start 开头 ++_gen 后的世代号
    start(toggle._opts || {}).then(function () {
      if (!ws && g0 === _gen && !_userHung) _scheduleReconnect();   // 本世代失败(未被替代)→ 退避重排
    });
    setTimeout(function () {
      if (!ws && g0 === _gen && !_userHung) _scheduleReconnect();   // watchdog:本世代 12s 没建成 → 强制推进
    }, 12000);
  }
  function _scheduleReconnect() {
    if (_reviewVoiceGate(false)) {
      _userHung = true; _reconnPend = false;
      return;
    }
    if (_reconnT || _reconnPend) return;
    teardown(false); _userHung = false;      // teardown 置回 true → 恢复"通话意外中断"态
    _reconnN++;
    if (_reconnN > 8) { _userHung = true; _reconnN = 0; setSt('连接断开(点通话按钮重拨)'); taPlaceholder(null); return; }
    if (document.hidden) { _reconnPend = true; return; }   // 后台不空转烧退避次数,回前台立即连
    var wait = Math.min(8000, 600 * Math.pow(2, _reconnN - 1));
    setSt('连接断开,' + Math.ceil(wait / 1000) + 's 后自动重连…'); taPlaceholder('语音重连中…');
    _reconnT = setTimeout(function () { _reconnT = null; _tryStart(); }, wait);
  }
  document.addEventListener('visibilitychange', function () {
    if (document.hidden) return;
    try { if (ac && ac.state !== 'running') ac.resume(); } catch (e) {}   // iOS 回前台:音频会话可能被挂起
    if (ws) {
      _acquireWL();                          // 通话还活着:wake lock 切后台被系统释放过 → 重新拿
      // ㉞ rtc 假活检查:shim 的 readyState 恒为 1,回前台必须看真实 pc 状态(iOS 后台常把 WebRTC 掐死)
      if (_rtc.on && _rtc.pc && ['failed', 'closed', 'disconnected'].indexOf(_rtc.pc.connectionState) >= 0) _rtcDead('后台挂起');
      return;
    }
    if (_reconnPend || (!_userHung && !_reconnT)) {   // 后台断的线 → 回来立即重连
      _reconnPend = false;
      _tryStart();
    }
  });
  // iOS 音频恢复:suspended 的 AudioContext 只有用户手势才能真正 resume——常驻捕获监听,
  // 通话中用户任意触屏即恢复声音(没通话时 ac=null,零开销)。朗读专用 _tts.ac 同样救
  // (通话失败后它可能在非手势回调里重建=永久 suspended,只有这里能救回)。
  document.addEventListener('pointerdown', function () {
    try { if (ac && ac.state !== 'running') ac.resume(); } catch (e) {}
    try { if (_tts.ac && _tts.ac.state !== 'running') _tts.ac.resume(); } catch (e) {}
    // 字幕"正在听"兜底:通话在、字幕该显示却全空(如 agent_ready 时侧栏还开着被 gate 吞)→ 触屏后补亮
    setTimeout(function () {
      try {
        if (ws && _capVisible() && (!_cap.el ||
            (_cap.cur.style.display === 'none' && _cap.wait.style.display === 'none' && _cap.st.style.display === 'none'))) capWait(true);
      } catch (e) {}
    }, 350);
  }, true);

  function injectCss() {
    if (document.getElementById('rc-vc-css')) return;
    var s = document.createElement('style'); s.id = 'rc-vc-css';
    s.textContent =
      // 朗读字幕(v3-⑳ → 133 重做外观):**一整块**毛玻璃(仿 iOS 实况字幕),不再是两个分离的黑胶囊。
      //   上一句淡、当前句亮,同一块玻璃里;你说的话左侧一条蓝细条、AI 无条 —— 不再整块变蓝(太重、太碎)。
      '#vc-cap{position:fixed;left:50%;transform:translateX(-50%) translateY(12px) scale(.97);' +
        'bottom:calc(76px + env(safe-area-inset-bottom,0px));z-index:2147481500;' +
        'display:flex;flex-direction:column;align-items:stretch;gap:1px;pointer-events:none;' +
        'width:max-content;max-width:min(88vw,640px);padding:11px 17px;border-radius:22px;' +
        'background:linear-gradient(180deg,rgba(26,26,32,.78),rgba(16,16,20,.84));' +   // 白页上也要够暗:文字才立得住
        '-webkit-backdrop-filter:blur(30px) saturate(1.8);backdrop-filter:blur(30px) saturate(1.8);' +
        'border:0.5px solid rgba(255,255,255,.14);' +
        'box-shadow:0 14px 48px -12px rgba(0,0,0,.5),inset 0 0.5px 0 rgba(255,255,255,.09);' +
        'opacity:0;transition:opacity .34s cubic-bezier(.32,.72,.36,1),transform .34s cubic-bezier(.32,.72,.36,1);' +
        'font-family:-apple-system,system-ui,sans-serif}' +
      '#vc-cap.on{opacity:1;transform:translateX(-50%) translateY(0) scale(1)}' +
      // 每一行 = 纯文字(玻璃在外层容器上,行内不再各自套底色)
      '#vc-cap .vc-cap-line{background:none;box-shadow:none;border-radius:0;padding:3px 0 3px 11px;' +
        'color:rgba(255,255,255,.97);font-size:16px;font-weight:450;line-height:1.55;letter-spacing:.012em;' +
        'text-align:left;max-width:100%;word-break:break-word;position:relative;' +
        'animation:vcCapIn .34s cubic-bezier(.2,.85,.3,1);transition:opacity .3s}' +
      '@keyframes vcCapIn{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:none}}' +
      '#vc-cap .vc-cap-prev{opacity:.42;font-size:13.5px;font-weight:400;padding-bottom:1px}' +   // 上一句:淡一档、小一档
      // 你说的话:左侧一条蓝细条(不整块变蓝);AI 无条
      '#vc-cap .vc-cap-u::before{content:"";position:absolute;left:0;top:7px;bottom:7px;width:2.5px;border-radius:2px;background:#0a84ff}' +
      '#vc-cap .vc-cap-u{color:rgba(255,255,255,.92)}' +
      // 状态行 / "正在听":做成小 chip,不跟字幕同宽
      '#vc-cap .vc-cap-st,#vc-cap .vc-cap-wait{align-self:flex-start;display:flex;align-items:center;gap:7px;' +
        'font-size:12.5px;font-weight:450;color:rgba(255,255,255,.8);background:rgba(255,255,255,.09);' +
        'border-radius:10px;padding:4px 10px 4px 9px;margin-top:3px;box-shadow:none;letter-spacing:0}' +
      '#vc-cap .vc-cap-st svg,#vc-cap .vc-cap-wait svg{width:13px;height:13px;flex:none;opacity:.85}' +
      '#vc-cap .vc-cap-st.vc-st-ok{background:rgba(48,209,88,.16);color:#a8ebbb}' +
      '#vc-cap .vc-cap-st.vc-st-err{background:rgba(255,105,97,.16);color:#ffc4bf}' +
      '#vc-cap .vc-cap-st .vc-tks.ok{color:#30d158;font-weight:600}' +
      '#vc-cap .vc-cap-st .vc-tks.err{color:#ff6961}' +
      '.vc-spin-s{width:11px;height:11px;border-width:1.6px;flex:none}' +
      '#vc-cap .vc-cap-wait i{width:4px;height:4px;border-radius:50%;background:#fff;opacity:.3;animation:vcCapDot 1.4s ease-in-out infinite}' +
      '#vc-cap .vc-cap-wait i:nth-of-type(2){animation-delay:.22s}' +
      '#vc-cap .vc-cap-wait i:nth-of-type(3){animation-delay:.44s}' +
      '@keyframes vcCapDot{0%,100%{opacity:.25;transform:translateY(0)}50%{opacity:.95;transform:translateY(-2.5px)}}' +
      // 65 文字卡片:iOS 通知风磨砂堆叠(右下锚,新卡在前,旧卡左上交错缩小)
      '#vc-cards{position:fixed;right:14px;bottom:calc(150px + env(safe-area-inset-bottom,0px));z-index:2147481400;width:min(80vw,340px);pointer-events:none}' +
      // ⚠ 毛玻璃**不能**画在这个元素自己身上:它同时在 transition width/height/border-radius,
      //   而 WebKit(iOS)下 backdrop-filter 的采样与裁剪跟不上尺寸变化 —— 卡片右侧和底部
      //   会露出一圈没被裁掉的模糊层,看起来像"上面盖了个更大且错位的透明浮层"(实测)。
      //   放到 ::before 上:它由布局逐帧决定 inset:0,自己不参与任何过渡,裁剪永远是当前尺寸。
      // ⚠ --vc-cardblur 默认 none:backdrop-filter 在 iOS 上会**整层与元素本体错位**
      //   (模糊层留在左上、本体偏右下,超出的被裁 → 右边看着变成直角)。移到 ::before 只是
      //   把裁剪滞后治好了,错位是 backdrop-filter 自己的合成问题,换个宿主一样犯。
      //   球那边去掉它之后问题直接消失,同一个原因 —— 所以这里也去掉,底色相应加实补偿。
      //   想找回磨砂:把 --vc-cardblur 改回 blur(24px) saturate(1.6) 即可,一处开关。
      '.vc-card{position:absolute;right:0;bottom:0;width:100%;background:transparent;isolation:isolate;' +
      '--vc-cardbg:rgba(30,30,34,.9);--vc-cardblur:none;' +
      '-webkit-user-select:none;user-select:none;-webkit-touch-callout:none;' +
      'border:0.5px solid rgba(255,255,255,.14);border-radius:16px;box-shadow:0 12px 40px rgba(0,0,0,.4);color:#f2f2f7;font-size:14px;line-height:1.55;' +
      'padding:10px 13px 12px;pointer-events:auto;display:flex;flex-direction:column;max-height:36vh;' +
      'transition:transform .38s cubic-bezier(.32,.72,.36,1),opacity .32s ease;font-family:-apple-system,system-ui,sans-serif}' +
      // 底面(磨砂 + 底色)统一由这一层画。各状态只改 --vc-cardbg/--vc-cardblur 两个变量,
      // 不再各自写 background —— 否则一旦有人漏改,那个状态就退回到"卡片自己画背景",
      // 尺寸过渡时的裁剪滞后立刻回来。
      // 裁剪兜底:非圆点态一律按自身圆角裁。上面把毛玻璃移到 ::before 是治本,
      // 这一条是**不问来源**的兜底 —— 任何比卡片大或错位的层(含 WebKit 自己合成出来的)
      // 都到边为止,不会再从右边和底下露出来。圆点态要露出标记,保持 visible。
      '.vc-card:not(.vc-dot){overflow:hidden}' +
      '.vc-card::before{content:"";position:absolute;inset:0;border-radius:inherit;z-index:-1;pointer-events:none;' +
        'background:var(--vc-cardbg);-webkit-backdrop-filter:var(--vc-cardblur);backdrop-filter:var(--vc-cardblur);' +
        'transition:background .3s ease}' +
      // 141 轮次容器(rc-turncard.js;设计见 references/adr-turn-container.md):
      //   容器本体复用既有气泡/结果卡外观(.asst-a / .vc-if / .vc-if-hd),流程按钮复用 .vc-flowb,
      //   看大图复用 .fig-lightbox —— 一律不另造(用户拍板:复用之前设计的,别自己新设计一套)。
      '.rc-turn .rc-turn-bd>.rc-part+.rc-part{margin-top:8px}' +
      '.rc-turn.vc-if .rc-turn-bd{padding-top:2px}' +
      '.rc-part-cardin{padding:0!important;background:transparent!important;border:none!important;box-shadow:none!important}' +
      '.rc-turn-flow{margin-top:8px;padding-top:8px;border-top:0.5px solid rgba(255,255,255,.12);font-size:12px;color:#9fb0cf}' +
      '.rc-flow-meta{color:#7f92b8;margin-bottom:6px}' +
      '.rc-flow-node{padding:6px 0;border-bottom:0.5px solid rgba(255,255,255,.07)}' +
      '.rc-flow-node:last-child{border-bottom:none}' +
      '.rc-flow-h{color:#b9a8ff;font-weight:600;margin-bottom:4px}' +
      '.rc-flow-args{color:#7f92b8;word-break:break-all;font-size:11px}' +
      '.rc-flow-step{color:#8fa0c0;font-size:11px}' +
      '.rc-flow-r{margin-top:4px}' +
      '.rc-flow-img{max-width:min(100%,220px);max-height:180px;border-radius:8px;cursor:zoom-in;' +
        'border:1px solid rgba(255,255,255,.12);background:#0e1422;display:block;margin-bottom:2px}' +
      '.rc-flow-cap{font-size:11px;color:#7f92b8;margin-bottom:4px}' +
      '.vc-card-hd{display:flex;align-items:center;gap:6px;font-size:12px;color:#b9a8ff;margin-bottom:6px;flex:none}' +
      '.vc-card-x{margin-left:auto;width:22px;height:22px;border-radius:50%;background:rgba(255,255,255,.14);border:none;color:#e8e8ee;display:flex;align-items:center;justify-content:center;cursor:pointer;padding:0;flex:none}' +
      '.vc-map-live{position:absolute;inset:0;z-index:1;overflow:hidden;touch-action:none;cursor:grab;background:#0f1420;opacity:0;transition:opacity .18s}' +
      '.vc-ig-cell.vc-map-ready .vc-map-live{opacity:1}' +
      '.vc-ig-cell[data-map-url] .vc-ig-img{pointer-events:none}' +
      '.vc-ig-cell[data-map-url]{min-height:150px}' +
      '.vc-ig-map{position:absolute;left:6px;top:6px;z-index:3;width:26px;height:26px;border-radius:13px;border:none;background:rgba(16,23,38,.78);color:#fff;font-size:14px;cursor:pointer;padding:0}' +
      '.vc-mapov{position:fixed;inset:0;z-index:2147483200;background:#101726;display:flex;flex-direction:column}' +
      '.vc-map-hd{display:flex;align-items:center;gap:8px;padding:10px 14px;color:#dbe4f8;font-size:14px;background:#0b111f}' +
      '.vc-map-hd span:first-child{font-weight:600;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
      '.vc-map-attr{color:#5a6680;font-size:11px;flex:none}' +
      '.vc-map-z,.vc-map-x{width:34px;height:34px;border-radius:8px;border:1px solid #2a3550;background:#1a2540;color:#dbe4f8;font-size:16px;cursor:pointer;flex:none;padding:0}' +
      '.vc-map-vp{flex:1;position:relative;overflow:hidden;touch-action:none;cursor:grab}' +
      '.vc-map-world{position:absolute;inset:0}' +
      '.vc-map-tile{position:absolute;width:256px;height:256px;pointer-events:none;user-select:none}' +
      '.vc-map-pin{position:absolute;width:14px;height:14px;margin:-7px 0 0 -7px;border-radius:50%;background:#e5484d;border:2.5px solid #fff;box-shadow:0 1px 6px rgba(0,0,0,.5);pointer-events:none}' +
      '.vc-card-pin{margin-left:auto;width:22px;height:22px;border-radius:50%;background:rgba(255,255,255,.14);border:none;color:#e8e8ee;display:flex;align-items:center;justify-content:center;cursor:pointer;padding:0;flex:none}' +
      '.vc-card-pin svg{width:11px;height:11px}' +
      // 钉入书页态(便签壳内的真 vc-card):脱离浮层定位,静态占满便签宽
      '.vc-card.vc-pinned{position:relative;right:auto;bottom:auto;left:auto;top:auto;width:100%;box-shadow:0 6px 22px rgba(0,0,0,.35)}' +
      '.vc-card.vc-pinned.vc-hasdot:not(.vc-min):not(.vc-dot){width:100%}' +   // 方块态宽度跟便签 w 自适应(压过 .vc-hasdot 固定 326px)
      '.vc-card-pin + .vc-card-p{margin-left:6px}' +
      '.vc-card-x svg{width:10px;height:10px}' +
      '.vc-card-p{margin-left:auto;width:22px;height:22px;border-radius:50%;background:rgba(123,108,255,.16);border:none;color:#9d8cff;' +
      'font-size:9px;display:flex;align-items:center;justify-content:center;cursor:pointer;padding:0 0 0 1px;flex:none;transition:transform .12s}' +
      '.vc-card-p:active{transform:scale(.82)}' +
      '.vc-card-p.playing{background:#7b6cff;color:#fff;animation:vcClipBreath2 1.8s ease-in-out infinite}' +
      '@keyframes vcClipBreath2{0%,100%{box-shadow:0 0 0 0 rgba(123,108,255,.45)}50%{box-shadow:0 0 0 6px rgba(123,108,255,0)}}' +
      '.vc-card-hd .vc-card-x{margin-left:6px}' +
      '.vc-card-bd{overflow-y:auto;white-space:pre-wrap;word-break:break-word;-webkit-overflow-scrolling:touch;min-height:0}' +
      // Markdown 元素样式必须挂在 .vc-card-bd 作用域下:侧栏气泡那套(.asst-a)只有内联态
      // 蹭得到,浮层卡和钉页卡够不着 —— 不补的话同一张卡在三个宿主下排版不一样。
      // 卡片空间小,间距一律收紧。
      '.vc-card-bd p{margin:0 0 .55em}.vc-card-bd p:last-child{margin-bottom:0}' +
      '.vc-card-bd ul,.vc-card-bd ol{margin:.3em 0 .55em;padding-left:1.35em}' +
      '.vc-card-bd li{margin:.14em 0}' +
      '.vc-card-bd h1,.vc-card-bd h2,.vc-card-bd h3,.vc-card-bd h4{font-size:1.04em;font-weight:650;margin:.55em 0 .28em}' +
      '.vc-card-bd h1:first-child,.vc-card-bd h2:first-child,.vc-card-bd h3:first-child{margin-top:0}' +
      '.vc-card-bd code{background:rgba(255,255,255,.1);border-radius:4px;padding:.08em .34em;font-size:.92em}' +
      '.vc-card-bd pre{background:rgba(0,0,0,.28);border-radius:8px;padding:8px 10px;overflow-x:auto;margin:.42em 0}' +
      '.vc-card-bd pre code{background:none;padding:0}' +
      '.vc-card-bd blockquote{margin:.42em 0;padding-left:.7em;border-left:2px solid rgba(255,255,255,.22);color:#c6cddf}' +
      '.vc-card-bd a{color:#8ab4ff}' +
      '.vc-card-bd hr{border:none;border-top:1px solid rgba(255,255,255,.14);margin:.6em 0}' +
      '.vc-card-bd table{border-collapse:collapse;font-size:.92em;margin:.42em 0;display:block;overflow-x:auto}' +
      '.vc-card-bd th,.vc-card-bd td{border:1px solid rgba(255,255,255,.16);padding:2px 6px;text-align:left}' +
      '.vc-card-bd img{max-width:100%;border-radius:6px}' +
      // 双击/触屏双点进入页面 placement 尺寸调整：只改变 full 方块态；
      // dot/min 保留固定语义尺寸。manipulation 保留滚动/缩放，但避免浏览器
      // 把触屏双点优先解释为页面缩放。
      // 尺寸按 cid 保存，但只投影到页面上的副本；侧栏/收藏/复习永远不跟随。
      '.vc-card.vc-user-sized:not(.vc-dot):not(.vc-min){box-sizing:border-box;width:var(--vc-user-w)!important;height:var(--vc-user-h)!important;max-height:min(calc(100vh - 24px),var(--vc-user-h))!important;min-width:180px;min-height:100px}' +
      '.vc-card.vc-user-sized:not(.vc-dot):not(.vc-min)>.vc-card-bd{flex:1 1 auto;min-height:0}' +
      '.vc-card.vc-page-placement:not(.vc-dot):not(.vc-min)>.vc-card-bd{touch-action:manipulation}' +
      // 横向 flex track 的 cross-size 未确定时，子项 height:100% 会退回内容高度，评分栏
      // 因而落到页面卡裁剪区外。页面 placement 用可收缩 grid row 明确分配卡面和圆点高度，
      // slide 再沿交叉轴伸展；只有 fc-review-scroll 承担正文纵向滚动。
      '.vc-card.vc-page-placement:not(.vc-dot):not(.vc-min)>.vc-card-bd.fc-bare{display:flex;flex-direction:column;overflow:hidden}' +
      '.vc-card.vc-page-placement:not(.vc-dot):not(.vc-min)>.vc-card-bd.fc-bare>.fc-wrap{display:grid;grid-template-rows:minmax(0,1fr) auto;flex:1 1 auto;min-height:0}' +
      '.vc-card.vc-page-placement:not(.vc-dot):not(.vc-min)>.vc-card-bd.fc-bare>.fc-wrap>.fc-track{grid-row:1;min-height:0;height:auto;align-items:stretch;overflow-y:hidden}' +
      '.vc-card.vc-page-placement:not(.vc-dot):not(.vc-min)>.vc-card-bd.fc-bare>.fc-wrap>.fc-track>.fc-slide{height:auto;min-height:0;max-height:100%;align-self:stretch;overflow:hidden}' +
      '.vc-card.vc-page-placement:not(.vc-dot):not(.vc-min)>.vc-card-bd.fc-bare>.fc-wrap>.fc-track>.fc-slide>.fc-card{box-sizing:border-box;height:100%;max-height:100%;overflow:hidden}' +
      '.vc-card.vc-page-placement:not(.vc-dot):not(.vc-min)>.vc-card-bd.fc-bare>.fc-wrap>.fc-dots{grid-row:2}' +
      '.vc-card-rs{position:absolute;right:5px;bottom:5px;width:25px;height:25px;display:none;z-index:24;cursor:nwse-resize;touch-action:none;padding:0;border-radius:9px;border:1px solid rgba(157,140,255,.66);background:rgba(24,28,42,.92);box-shadow:0 5px 16px rgba(0,0,0,.34);color:#d8d1ff;-webkit-tap-highlight-color:transparent}' +
      '.vc-card-rs::after{content:"";position:absolute;right:6px;bottom:6px;width:8px;height:8px;border-right:2px solid currentColor;border-bottom:2px solid currentColor;border-radius:1px}' +
      '.vc-card.vc-resize-armed:not(.vc-dot):not(.vc-min)>.vc-card-rs{display:block;animation:vcRsIn .18s cubic-bezier(.2,.85,.3,1)}' +
      '.vc-card.vc-resizing{transition:none!important;will-change:width,height;box-shadow:0 18px 46px rgba(0,0,0,.52),0 0 0 1px rgba(157,140,255,.68)!important}' +
      '@keyframes vcRsIn{from{opacity:0;transform:scale(.65)}to{opacity:1;transform:none}}' +
      '.vc-card.vc-lift{box-shadow:0 22px 60px rgba(0,0,0,.55),0 0 0 0.5px rgba(255,255,255,.2);cursor:grabbing}' +
      '.vc-card-sum{display:none;font-size:12.5px;color:#aab8d4;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}' +
      '.vc-card.vc-min .vc-card-bd{display:none}.vc-card.vc-min .vc-card-sum{display:block}' +
      '.vc-card.vc-min{padding:9px 13px}' +
      // ── 工具指示器 v2(用户设计):在这张卡上加**第三态=圆形标记**,并让标记本身当形态控制按钮 ──
      //    圆(vc-dot,创建/收起:透明玻璃无边缘) → 长条(vc-min) → 方块(展开)。标记坐落在方块左上角。
      // 标记 = 圆角方形(用户改:套长条的外观,别用正圆),坐落在卡片**左上角**,永远是形态控制按钮
      '.vc-card-dot{position:absolute;left:0;top:0;width:40px;height:40px;border-radius:13px;padding:0;border:0.5px solid var(--vc-tl,rgba(255,255,255,.16));' +
        'background:var(--vc-tf,rgba(28,28,30,.72));-webkit-backdrop-filter:blur(18px);backdrop-filter:blur(18px);' +
        'color:var(--vc-tc,#b9a8ff);display:flex;align-items:center;justify-content:center;cursor:pointer;z-index:2;' +
        'transition:transform .16s cubic-bezier(.34,1.5,.64,1),background .3s,border-color .3s,box-shadow .3s}' +
      '.vc-card-dot svg{width:17px;height:17px}' +
      '.vc-card.vc-hasdot .vc-card-hd{padding-left:48px;min-height:40px}' +   // 标记坐在左上角 → 标题让位
      '.vc-card-dot:active{transform:scale(.9)}' +
      '.vc-card-dot.busy{animation:vcDotBr 1.5s ease-in-out infinite}' +
      '@keyframes vcDotBr{0%,100%{opacity:.45}50%{opacity:1}}' +
      // 进行中/创建:标记是**透明玻璃**(无色);出结果:有色磨砂(两个状态一眼可辨,用户要求)
      '.vc-card.vc-typed .vc-card-dot{--vc-tf:color-mix(in srgb,var(--vc-tc) 22%,rgba(22,26,38,.8));' +
        '--vc-tl:color-mix(in srgb,var(--vc-tc) 48%,transparent);box-shadow:0 6px 18px -8px rgba(0,0,0,.5)}' +
      // ⚠ 进行中的标记必须**在任何底色上都看得见**:原来是 6% 白玻璃,压在 PDF 白页上等于隐形(用户实测"不显示方块了")
      '.vc-card.vc-busy .vc-card-dot{--vc-tf:rgba(22,26,38,.62);--vc-tl:rgba(255,255,255,.22);' +
        'box-shadow:0 4px 14px -4px rgba(0,0,0,.45)}' +
      // 收起态:整张卡就是那枚圆角方形标记
      '.vc-card.vc-dot{width:40px;height:40px;min-height:0;padding:0;border-radius:13px;border-color:transparent;' +
        '--vc-cardbg:transparent;--vc-cardblur:none;box-shadow:none;overflow:visible}' +
      '.vc-card.vc-dot .vc-card-hd,.vc-card.vc-dot .vc-card-sum,.vc-card.vc-dot .vc-card-bd{display:none}' +
      // 收起成球时让后面的书页透出来。⚠ 但不能只是把填充调淡 —— 上面那条注释记着教训:
      // 6% 白玻璃压在 PDF 白页上直接隐形。所以**可见性改由轮廓承担**:填充与模糊都压低,
      // 边框反而加实、再加一圈极淡的外描边,球在白页和深色插图上都还认得出。
      // 展开后不受影响(下面的 :not(.vc-dot) 把值还原)。
      // ⚠ 收起态**去掉** backdrop-filter,不是调低它:
      //   ① iOS 上这一层的合成结果与元素本体对不齐,模糊层留在左上、本体偏右下,
      //      超出的部分被裁 → 右边看着变成直角(用户诊断:"偏右下就导致右边被截掉",正解);
      //   ② 球要的是"透出后面的字",而 blur 本身就是遮挡 —— 两者方向相反。
      //   去掉之后这个元素只剩一层背景,没有第二个层可错位,问题从根上消失。
      //   可见性仍由轮廓承担:内描边加实 + 一圈极淡外描边,亮底暗底都认得出。
      '.vc-card.vc-dot .vc-card-dot{--vc-tf:color-mix(in srgb,var(--vc-tc) 14%,rgba(22,26,38,.38));' +
        '--vc-tl:color-mix(in srgb,var(--vc-tc) 66%,rgba(255,255,255,.38));' +
        '-webkit-backdrop-filter:none;backdrop-filter:none;' +
        'box-shadow:0 2px 9px -3px rgba(0,0,0,.34),0 0 0 0.5px rgba(0,0,0,.18)}' +
      // 手指按住时补回不透明度:正在操作的东西该是实的,不然点下去像没点到。
      '.vc-card.vc-dot .vc-card-dot:active{--vc-tf:color-mix(in srgb,var(--vc-tc) 26%,rgba(22,26,38,.72))}' +
      // 三态生长:**以左上角(标记位置)为原点**拉长/展开——标记不动,卡片从它身上长出来
      // 三态生长的曲线统一成 iOS 那条 smooth-spring 近似(.32,.72,.36,1):起步快、尾巴长,
      // 停下来时没有回弹的"抖"。原来尺寸和 transform 用了两条不同曲线(其中一条 1.35 会过冲),
      // 两条曲线同时跑,边框和内容看着像各走各的。
      '.vc-card.vc-hasdot{right:auto;bottom:auto;transform-origin:0 0;' +
        'transition:width .42s cubic-bezier(.32,.72,.36,1),height .42s cubic-bezier(.32,.72,.36,1),' +
        'border-radius .34s cubic-bezier(.32,.72,.36,1),box-shadow .34s ease,border-color .25s,' +
        'transform .34s cubic-bezier(.32,.72,.36,1),opacity .26s ease}' +
      // 内容**错峰**:容器先长开,正文晚 .14s 再淡入;收起时正文先走(.14s),容器随后收。
      // 这是这类展开动画看着"稳"的关键 —— 否则正文会跟着容器一起被拉伸/压扁。
      '.vc-card.vc-hasdot.vc-min .vc-card-bd{display:block;opacity:0;pointer-events:none;transition:opacity .14s ease}' +
      '.vc-card.vc-hasdot:not(.vc-min):not(.vc-dot) .vc-card-bd{opacity:1;transition:opacity .26s ease .14s}' +
      // 长条 = **一行**(用户改):标题+状态+▶+✕ 全挤在头部一行,与标记同高 40px
      //   → 标记→长条 = 上下边不动、纯向右拉长;长条→方块 = 纯向下伸长(动画方向干净)
      '.vc-card.vc-hasdot.vc-min{width:300px;height:40px;min-height:40px;padding:0 10px 0 0;overflow:hidden}' +
      '.vc-card.vc-hasdot.vc-min .vc-card-hd{margin-bottom:0;height:40px}' +
      // ⚠ 正文不再 display:none —— 它改由上面的错峰规则做 opacity 淡出(display 不能过渡),
      //   靠 vc-min 的 overflow:hidden + 固定 40px 高裁掉,布局不受影响。
      '.vc-card.vc-hasdot.vc-min .vc-card-sum{display:none}' +
      '.vc-card.vc-hasdot:not(.vc-min):not(.vc-dot){width:326px;padding:0 13px 12px 0}' +
      '.vc-card.vc-hasdot:not(.vc-min):not(.vc-dot) .vc-card-hd{padding-right:10px}' +
      '.vc-card.vc-hasdot:not(.vc-min):not(.vc-dot) .vc-card-sum{display:none}' +
      '.vc-card.vc-hasdot:not(.vc-min):not(.vc-dot) .vc-card-bd{padding-left:13px}' +
      // 完成态:有色磨砂 + 边缘阴影(跟"创建时的透明玻璃圆"区分开)
      '.vc-card.vc-typed{border-color:color-mix(in srgb,var(--vc-tc) 42%,transparent);' +
        '--vc-cardbg:color-mix(in srgb,var(--vc-tc) 15%,rgba(28,30,34,.9));' +   /* 去 blur 后加实 */
        'box-shadow:0 16px 42px rgba(0,0,0,.5),0 0 22px -8px color-mix(in srgb,var(--vc-tc) 55%,transparent)}' +
      '.vc-card.vc-typed .vc-card-hd{color:var(--vc-tc)}' +
      '.vc-card.vc-typed.vc-dot{--vc-cardbg:rgba(255,255,255,.05);--vc-cardblur:none;box-shadow:none;border-color:transparent}' +
      '.vc-card.vc-err{--vc-tc:#ff6961}' +
      // 系统开了"减弱动态效果"就只留淡入淡出 —— iOS 上这是无障碍设置,不是可选装饰。
      '@media (prefers-reduced-motion:reduce){' +
        '.vc-card,.vc-card.vc-hasdot,.vc-card::before,.vc-card .vc-card-bd{transition-duration:.01s!important;transition-delay:0s!important}' +
        '.vc-card-dot{transition:none}}' +
      // 展开视图 = **数据流图**(用户设计):AI / 工具 / 结果 各一个可点开的小方块,用线连起来表示数据传递
      '.vc-flow{margin-top:2px}' +
      '.vc-fn{display:flex;align-items:center;gap:8px;padding:7px 9px;border-radius:10px;cursor:pointer;' +
        'background:rgba(255,255,255,.05);border:0.5px solid rgba(255,255,255,.1);-webkit-tap-highlight-color:transparent;' +
        'transition:background .18s,border-color .18s}' +
      '.vc-fn:active{background:rgba(255,255,255,.1)}' +
      '.vc-fn.on{border-color:var(--vc-tc);background:color-mix(in srgb,var(--vc-tc) 14%,rgba(255,255,255,.04))}' +
      '.vc-fn-i{flex:none;width:22px;height:22px;border-radius:7px;display:flex;align-items:center;justify-content:center;' +
        'background:color-mix(in srgb,var(--vc-tc) 24%,rgba(0,0,0,.32));color:#dfe7f6}' +
      '.vc-fn-i svg{width:13px;height:13px}' +
      // 展开(长条/方块)后:左上角那枚标记按钮**不再显示**(用户要求);形态切换改点头部
      '.vc-card.vc-hasdot:not(.vc-dot) .vc-card-dot{display:none}' +
      '.vc-card.vc-hasdot:not(.vc-dot) .vc-card-hd{padding-left:13px}' +
      '.vc-card.vc-hasdot:not(.vc-min):not(.vc-dot) .vc-card-bd{padding-left:13px}' +
      // 结果卡标题栏上的「流程」按钮(天气/搜索等自带结果卡的工具:唯一显示的是结果卡,流程收在这个按钮里)
      '.vc-flowb{flex:none;width:22px;height:22px;border-radius:50%;background:rgba(255,255,255,.12);border:none;color:#cbd6ea;' +
        'display:flex;align-items:center;justify-content:center;cursor:pointer;padding:0;margin-left:4px;line-height:0;' +
        '-webkit-appearance:none;appearance:none;font-size:0}' +
      '.vc-flowb svg{width:13px;height:13px;display:block;stroke:currentColor;fill:none}' +
      // ★ 141(白块真凶):rc-assistant 有一条 `.asst-a img,.asst-a svg{background:#fff;padding:10px;border-radius:8px}`
      //   —— 那是给**助手回答里的内容图片/公式 SVG** 加白底的(深色图看得清)。
      //   而轮次容器复用了 .asst-a → 它里面**所有 SVG 都被涂成白底**,包括【流程】按钮的图标
      //   = 20px 白圆角块塞满 22px 的圆 = 用户看到的"白方块"。
      //   (toolchip 自己的卡是 .vc-card、不带 .asst-a,所以它的按钮一直正常 —— 用户"之前渲染正常"的观察是对的。)
      //   ⚠ 跟 Safari 无关:Chromium 里同样是白的,我先前只验了按钮本身的 computed style、没验它里面的 svg。
      //   修:UI 图标不是"内容图片"。用更高优先级(0,2,1 > 0,1,1)把它们从那条白底规则里摘出来。
      '.asst-a .vc-flowb svg{width:13px;height:13px;background:none;padding:0;margin:0;border-radius:0;max-width:none;display:block}' +
      '.asst-a .vc-if-hd svg,.asst-a [role="button"] svg,.asst-a button svg{background:none;padding:0;margin:0;border-radius:0;max-width:none;height:auto}' +
      // ★ 流程里的工具长条图标(.vc-fn-i / .vc-flow / 详情窗)也是 UI 图标,同样从白底规则摘出去(白块复发根因:上一条没覆盖 .vc-fn-i)。
      '.asst-a .vc-flow svg,.asst-a .vc-fn svg,.asst-a .vc-fn-i svg,.asst-a .vc-dtl svg,.asst-a .rc-flow-node svg{background:none;padding:0;margin:0;border-radius:0;max-width:none;display:block;height:auto}' +
      '.asst-a .vc-fn-i svg{width:13px;height:13px}' +
      '.vc-flowb:active{transform:scale(.86)}' +
      '.vc-flowb.on{background:#7b6cff;color:#fff}' +
      '.vc-flowbox{margin-top:8px;padding-top:8px;border-top:0.5px solid rgba(255,255,255,.12)}' +
      // 139(用户):工具调用**详情窗**(长按流程里的小长条打开)——复用旧「!」面板的格式:
      //   每条流程可点名字看细节,后面跟模型 / 耗时。
      '#vc-dtl{position:fixed;inset:0;z-index:2147481700;display:flex;align-items:center;justify-content:center;' +
        'background:rgba(0,0,0,.42);-webkit-backdrop-filter:blur(6px);backdrop-filter:blur(6px);opacity:0;transition:opacity .22s}' +
      '#vc-dtl.on{opacity:1}' +
      '.vc-dtl-w{width:min(92vw,520px);max-height:78vh;display:flex;flex-direction:column;border-radius:20px;' +
        'background:linear-gradient(180deg,rgba(30,30,36,.96),rgba(20,20,26,.98));border:0.5px solid rgba(255,255,255,.14);' +
        'box-shadow:0 28px 70px -18px rgba(0,0,0,.75);color:#e9eefb;font-family:-apple-system,system-ui,sans-serif;' +
        'transform:scale(.94);transition:transform .26s cubic-bezier(.34,1.4,.64,1)}' +
      '#vc-dtl.on .vc-dtl-w{transform:scale(1)}' +
      '.vc-dtl-h{display:flex;align-items:center;gap:8px;padding:13px 15px;border-bottom:0.5px solid rgba(255,255,255,.1);flex:none}' +
      '.vc-dtl-h b{flex:1;font-size:14.5px;font-weight:650}' +
      '.vc-dtl-h .vc-dtl-x{width:26px;height:26px;border-radius:50%;border:none;background:rgba(255,255,255,.12);color:#dfe6f5;' +
        'display:flex;align-items:center;justify-content:center;cursor:pointer;padding:0;font-size:14px;line-height:1}' +
      '.vc-dtl-b{overflow-y:auto;padding:10px 13px 14px;-webkit-overflow-scrolling:touch}' +
      '.vc-dtl-r{border-radius:11px;background:rgba(255,255,255,.05);border:0.5px solid rgba(255,255,255,.08);margin-top:7px;overflow:hidden}' +
      '.vc-dtl-r>.h{display:flex;align-items:center;gap:8px;padding:9px 11px;cursor:pointer;-webkit-tap-highlight-color:transparent}' +
      '.vc-dtl-r>.h:active{background:rgba(255,255,255,.07)}' +
      '.vc-dtl-r .nm{flex:1;font-size:13px;color:#e6ecf8;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
      '.vc-dtl-r .md{flex:none;font-size:10.5px;padding:2px 7px;border-radius:6px;background:rgba(123,108,255,.2);color:#c3b7ff;font-weight:600}' +
      '.vc-dtl-r .sc{flex:none;font-size:11px;color:#8195b8;font-variant-numeric:tabular-nums}' +
      '.vc-dtl-r .ar{flex:none;font-size:10px;color:#8195b8;transition:transform .2s}' +
      '.vc-dtl-r.on .ar{transform:rotate(90deg)}' +
      '.vc-dtl-r .bd{padding:2px 11px 11px;font-size:12.5px;line-height:1.6;color:#c9d4e8;word-break:break-word;max-height:230px;overflow:auto}' +
      '.vc-dtl-r .bd pre{white-space:pre-wrap;word-break:break-all;font-family:ui-monospace,Menlo,monospace;font-size:11px}' +
      // 140(用户):工具的说明 / 内部 prompt —— 凡是会进 AI 并实际产生影响的,都能在这里直接改
      // 143:调用前垫话策略(详情窗里的分段控件)
      // 148:工具卡面板的「模型」段(跟 .vc-fl 同一套配色/间距,视觉上是同族)
      '.vc-md{margin-bottom:12px;padding-bottom:11px;border-bottom:0.5px solid rgba(255,255,255,.1)}' +
      '.vc-md-t{font-size:11.5px;color:#9db0d4;font-weight:600;margin-bottom:7px}' +
      '.vc-md-t em{font-style:normal;color:#7f92b8;font-weight:400}' +
      '.vc-md-r{display:flex;gap:6px}' +
      '.vc-md-r select{flex:1;min-width:0;padding:7px 8px;font-size:12px;font-weight:600;cursor:pointer;\n        border-radius:10px;border:0.5px solid rgba(255,255,255,.14);background:rgba(255,255,255,.05);\n        color:#c6d2e8;-webkit-appearance:none;appearance:none;text-overflow:ellipsis}' +
      '.vc-md-fast{flex:none;border-radius:10px;border:.5px solid rgba(255,255,255,.14);background:rgba(255,255,255,.05);color:#8fa1c4;padding:7px 9px;font-size:11px;font-weight:650;cursor:pointer}' +
      '.vc-md-fast.on{background:rgba(39,165,104,.20);border-color:rgba(70,220,143,.55);color:#9af0c7}.vc-md-fast:disabled{opacity:.38;cursor:not-allowed}' +
      '.vc-md-st{font-size:10.5px;color:#7f92b8;margin-top:5px}' +
      '.vc-fl{margin-bottom:12px;padding-bottom:11px;border-bottom:0.5px solid rgba(255,255,255,.1)}' +
      '.vc-fl-t{font-size:11.5px;color:#9db0d4;font-weight:600;margin-bottom:7px}' +
      '.vc-fl-t em{font-style:normal;color:#7f92b8;font-weight:400}' +
      '.vc-fl-seg{display:flex;gap:0;border-radius:10px;overflow:hidden;border:0.5px solid rgba(255,255,255,.14)}' +
      '.vc-fl-seg button{flex:1;padding:7px 4px;font-size:12px;font-weight:600;cursor:pointer;border:none;\n        background:rgba(255,255,255,.05);color:#9db0d4;-webkit-appearance:none;appearance:none;\n        border-right:0.5px solid rgba(255,255,255,.12)}' +
      '.vc-fl-seg button:last-child{border-right:none}' +
      '.vc-fl-seg button.on{background:#7b6cff;color:#fff}' +
      '.vc-fl-st{font-size:10.5px;color:#7f92b8;margin-top:5px}' +
      '.vc-tp{margin-top:12px;padding-top:10px;border-top:0.5px solid rgba(255,255,255,.1)}' +
      '.vc-tp-t{font-size:11.5px;color:#9db0d4;font-weight:600;margin-bottom:2px}' +
      '.vc-tp-t em{font-style:normal;color:#7f92b8;font-weight:400}' +
      '.vc-tp-f{margin-top:9px}' +
      '.vc-tp-f label{display:block;font-size:11.5px;color:#c6d2e8;margin-bottom:4px}' +
      '.vc-tp-f textarea{width:100%;box-sizing:border-box;min-height:74px;resize:vertical;border-radius:10px;padding:8px 10px;' +
        'background:rgba(0,0,0,.28);border:0.5px solid rgba(255,255,255,.14);color:#e6ecf8;' +
        'font-size:12.5px;line-height:1.55;font-family:-apple-system,system-ui,sans-serif;-webkit-appearance:none}' +
      '.vc-tp-f textarea:focus{outline:none;border-color:#7b6cff;box-shadow:0 0 0 2px rgba(123,108,255,.25)}' +
      '.vc-tp-f .st{font-size:10.5px;color:#7f92b8;margin-top:3px}' +
      '.vc-tp-btns{display:flex;flex-wrap:wrap;gap:7px;margin-top:11px}' +
      '.vc-tp-btns button{flex:1;min-width:78px;border-radius:10px;padding:8px 6px;font-size:12px;font-weight:600;cursor:pointer;' +
        'border:0.5px solid rgba(255,255,255,.16);background:rgba(255,255,255,.08);color:#dbe4f5;-webkit-appearance:none}' +
      '.vc-tp-btns button.pri{background:#7b6cff;border-color:#7b6cff;color:#fff}' +
      '.vc-tp-btns button:active{transform:scale(.96)}' +
      // 侧栏结果卡折叠成一行长条(点头部切换;侧栏没有标记)
      '.vc-if.vc-if-min > *:not(.vc-if-hd){display:none}' +
      '.vc-if.vc-if-min .vc-if-hd{margin-bottom:-4px}' +
      // 唯一保留 ▶ 的地方(用户设计):纯文字结果那块内容的角落 —— 点它用 TTS 念
      '.vc-fp-tts{position:absolute;right:4px;bottom:4px;width:22px;height:22px;border-radius:50%;padding:0;border:none;' +
        'background:rgba(123,108,255,.18);color:#9d8cff;display:flex;align-items:center;justify-content:center;cursor:pointer;' +
        'transition:transform .12s}' +
      '.vc-fp-tts:active{transform:scale(.85)}' +
      '.vc-fp-tts svg{width:11px;height:11px}' +
      '.vc-fn-t{flex:1;font-size:12.5px;color:#e2e9f7;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
      '.vc-fn-m{flex:none;font-size:10.5px;color:#7f92b8;font-variant-numeric:tabular-nums}' +
      '.vc-fn-x{flex:none;font-size:10px;color:#7f92b8;transition:transform .2s}' +
      '.vc-fn.on .vc-fn-x{transform:rotate(90deg)}' +
      // 连接线:表示数据从上一个方块流到下一个
      '.vc-fw{height:16px;margin-left:19px;border-left:1.5px solid color-mix(in srgb,var(--vc-tc) 45%,transparent);position:relative}' +
      '.vc-fw::after{content:"";position:absolute;left:-3.5px;bottom:0;width:6px;height:6px;border-right:1.5px solid color-mix(in srgb,var(--vc-tc) 60%,transparent);' +
        'border-bottom:1.5px solid color-mix(in srgb,var(--vc-tc) 60%,transparent);transform:rotate(45deg)}' +
      // 方块展开出来的载荷(markdown / 公式 / 图 / JSON 都在这里正常渲染)
      '.vc-fp{margin:6px 0 0 19px;padding:8px 10px;border-left:1.5px solid rgba(255,255,255,.12);' +
        'font-size:12.5px;line-height:1.6;color:#dbe4f5;max-height:230px;overflow:auto;word-break:break-word}' +
      '.vc-fp pre,.vc-fp code{font-family:ui-monospace,Menlo,monospace;font-size:11px;white-space:pre-wrap;word-break:break-all;color:#b9c6e0}' +
      '.vc-fp img{max-width:100%;border-radius:6px;display:block;margin-top:5px}' +
      '.vc-fp p{margin:.35em 0}.vc-fp ul,.vc-fp ol{margin:.35em 0;padding-left:1.2em}' +
      // 头部:标题 + 状态(一行长条里状态就显示在这)
      '.vc-hd-l{flex:none;font-weight:600}' +
      '.vc-hd-s{flex:1;font-size:11.5px;color:#93a4c6;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
      // Anki 完整卡片预览(正/反面翻页 + 挖空 + 公式/图由 MathJax/img 渲染)
      '.vc-fc{margin-top:7px;background:rgba(0,0,0,.26);border:0.5px solid rgba(255,255,255,.1);border-radius:9px;padding:7px 9px;color:#e6ecf8;font-size:13px}' +
      '.vc-fc img{max-width:100%;border-radius:6px;margin-top:5px;display:block}' +
      '.vc-fc-t{font-size:9.5px;letter-spacing:.1em;color:#7c8bab;font-weight:700;margin-bottom:3px}' +
      // 删掉「正面/背面」标题后，靠底色深一档区分背面（.vc-fc 本身已有边框和间距）。
      '.vc-fc-back{background:rgba(0,0,0,.36)}' +
      '.vc-cz{background:rgba(123,108,255,.22);border-bottom:1.5px solid #7b6cff;border-radius:3px;padding:0 5px;color:#cdc6ff;font-weight:600}' +
      '.vc-fc-n{display:flex;align-items:center;gap:7px;margin-top:7px}' +
      '.vc-fc-n button{background:transparent;border:0.5px solid rgba(255,255,255,.16);border-radius:7px;color:#93a4c6;width:26px;height:24px;cursor:pointer;font-size:13px;padding:0}' +
      '.vc-fc-n button:disabled{opacity:.3}' +
      '.vc-fc-d{width:6px;height:6px;border-radius:50%;background:rgba(255,255,255,.22)}' +
      '.vc-fc-d.on{background:#fff;width:14px;border-radius:3px}' +
      // 侧栏内联(同一张卡进对话流:静态排布,不绝对定位)。它没有左上角标记 → 头部不用让位
      '.vc-card.vc-inflow{position:relative;right:auto;bottom:auto;width:100%!important;margin:8px 0;transform:none!important;max-height:none}' +
      '.vc-card.vc-inflow .vc-card-hd{padding-left:12px}' +
      '.vc-card.vc-inflow.vc-min{padding:0 10px 0 0}' +
      '.vc-card.vc-inflow:not(.vc-min) .vc-card-bd{padding-left:12px}' +
      '.vc-card.vc-inflow.vc-dot{width:40px!important;height:40px;padding:0;margin:8px 0}' +   // 侧栏圆点态:40×40 小圆(压过 vc-inflow 的 width:100%)
      '@keyframes vcPinPop{0%{filter:brightness(1)}40%{filter:brightness(1.4)}100%{filter:brightness(1)}}' +   // ⚠不用 transform:会覆盖拖动后的内联 translate 导致瞬移
      '.vc-pin-pop{animation:vcPinPop .4s ease}' +
      '@keyframes vcDragCharge{0%{outline-color:rgba(125,211,252,0);outline-offset:6px}100%{outline-color:rgba(125,211,252,.82);outline-offset:2px}}' +
      '.vc-drag-charging{outline:1.5px solid transparent;animation:vcDragCharge .42s linear both}' +
      '.vc-drag-ready{outline:2px solid rgba(125,211,252,.92);outline-offset:2px}' +
      '.vc-drag-charging .vc-card-hd,.vc-drag-charging.vc-card-hd,.vc-drag-charging .vc-card-dot,.vc-drag-charging.vc-card-dot{cursor:wait!important}' +
      '.vc-drag-ready .vc-card-hd,.vc-drag-ready.vc-card-hd,.vc-drag-ready .vc-card-dot,.vc-drag-ready.vc-card-dot{cursor:grabbing!important}' +
      '@media (prefers-reduced-motion:reduce){.vc-drag-charging{animation:none;outline-color:rgba(125,211,252,.62);outline-offset:3px}}' +
      '#vc-dock-btn{position:fixed;right:14px;bottom:calc(96px + env(safe-area-inset-bottom,0px));z-index:2147481420;width:40px;height:40px;border-radius:50%;' +
      'border:0.5px solid rgba(255,255,255,.16);background:rgba(40,36,64,.72);-webkit-backdrop-filter:blur(20px);backdrop-filter:blur(20px);' +
      'color:#b9a8ff;display:none;align-items:center;justify-content:center;cursor:pointer;box-shadow:0 8px 26px rgba(0,0,0,.4);padding:0}' +
      '#vc-dock-btn .vc-dk-n{position:absolute;top:-4px;right:-4px;min-width:16px;height:16px;border-radius:8px;background:#7b6cff;color:#fff;font-size:10px;display:flex;align-items:center;justify-content:center;padding:0 4px}' +
      '#vc-dock-hint{position:fixed;left:0;right:0;bottom:0;height:150px;pointer-events:none;z-index:2147481410;opacity:0;transition:opacity .25s;' +
      'background:linear-gradient(to top,rgba(123,108,255,.38),rgba(123,108,255,.1) 55%,transparent)}' +
      '#vc-dock-hint.on{opacity:1}' +
      // 134(用户设计):往下拖=收藏 → 往上拖=**删除**。左上角一个窄的红色投放区,**只在拖动时出现**,平时不挡任何东西。
      '#vc-trash{position:fixed;left:0;top:0;width:126px;height:92px;z-index:2147481600;pointer-events:none;' +
        'display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;' +
        'border-radius:0 0 24px 0;color:#fff;font-size:12px;font-weight:600;letter-spacing:.02em;' +
        'background:linear-gradient(135deg,rgba(255,69,58,.92),rgba(255,69,58,.45));' +
        '-webkit-backdrop-filter:blur(16px);backdrop-filter:blur(16px);' +
        'box-shadow:0 10px 34px -10px rgba(255,69,58,.6);' +
        'opacity:0;transform:translate(-14px,-14px) scale(.9);' +
        'transition:opacity .22s ease,transform .28s cubic-bezier(.34,1.4,.64,1),box-shadow .2s}' +
      '#vc-trash.on{opacity:.97;transform:translate(0,0) scale(1)}' +
      '#vc-trash.hot{background:linear-gradient(135deg,#ff453a,rgba(255,69,58,.8));' +
        'box-shadow:0 14px 44px -8px rgba(255,69,58,.85),0 0 0 2px rgba(255,255,255,.35) inset;transform:scale(1.06)}' +
      '#vc-trash svg{width:22px;height:22px;transition:transform .2s}' +
      '#vc-trash.hot svg{transform:scale(1.16) rotate(-8deg)}' +
      '#vc-dock-panel{position:fixed;left:0;right:0;bottom:0;z-index:2147481430;max-height:46vh;display:flex;flex-direction:column;' +
      'background:rgba(24,24,30,.82);-webkit-backdrop-filter:blur(26px) saturate(1.5);backdrop-filter:blur(26px) saturate(1.5);' +
      'border-top:0.5px solid rgba(255,255,255,.14);box-shadow:0 -14px 44px rgba(0,0,0,.45);padding-bottom:env(safe-area-inset-bottom,0px)}' +
      '.vc-dkp-hd{display:flex;align-items:center;gap:8px;padding:9px 14px 4px;flex:none}' +
      '.vc-dkp-t{font-size:12px;color:#9fb0cf;flex:1}' +
      '.vc-dkp-b{border:1px solid #35446b;background:rgba(255,255,255,.05);color:#9fb4e0;border-radius:8px;padding:4px 10px;font-size:12px;cursor:pointer}' +
      '.vc-dkp-b.on{background:#2b3a5f;color:#cfe0ff}.vc-dkp-b.danger{border-color:#7f2a2a;color:#ff8a80}' +
      // 时间线:天节点=轴上单线条+日期;每张卡上方竖线+具体时刻(用户设计元素,布局取横向时间轴成熟形态)
      '.vc-dkp-sc{flex:1;display:flex;align-items:flex-start;gap:14px;overflow-x:auto;overflow-y:hidden;padding:14px 16px 10px;-webkit-overflow-scrolling:touch;' +
      'scroll-snap-type:x proximity;scroll-padding:0 50%}' +
      '.vc-dkp-cell{scroll-snap-align:center;transition:width .28s ease}' +
      '.vc-dkp-cell[data-lvl="0"]{width:min(76vw,330px)}' +
      '.vc-dkp-cell[data-lvl="0"] .vc-dkp-txt{-webkit-line-clamp:9;font-size:12.5px}' +
      '.vc-dkp-cell[data-lvl="1"]{width:180px}' +
      '.vc-dkp-cell[data-lvl="1"] .vc-dkp-txt{-webkit-line-clamp:3}' +
      '.vc-dkp-cell[data-lvl="2"]{width:112px}' +
      '.vc-dkp-cell[data-lvl="2"] .vc-dkp-txt{-webkit-line-clamp:1}' +
      '.vc-dkp-cell[data-lvl="2"] .vc-dk-m{display:none}' +
      '.vc-dkp-day{flex:none;display:flex;align-items:center;height:18px;margin-top:0;padding:0 10px 0 2px;position:relative}' +
      '.vc-dkp-day span{font-size:11px;color:#b9a8ff;font-weight:600;white-space:nowrap;padding:0 8px;border-bottom:1px solid rgba(123,108,255,.5);line-height:17px}' +
      '.vc-dkp-cell{flex:none;display:flex;flex-direction:column;align-items:center;width:158px}' +
      '.vc-dkp-tick{font-size:10px;color:#7f8aa6;line-height:1;padding-bottom:2px;position:relative}' +
      '.vc-dkp-tick::after{content:"";display:block;width:1px;height:8px;background:rgba(123,108,255,.45);margin:3px auto 4px}' +
      '.vc-dk-card{width:100%;background:rgba(255,255,255,.06);border:0.5px solid rgba(255,255,255,.1);border-radius:12px;padding:8px 10px;' +
      'color:#e4e9f5;font-size:12px;-webkit-user-select:none;user-select:none;-webkit-touch-callout:none;transition:box-shadow .2s}' +
      '.vc-dk-card.vc-picked{box-shadow:0 0 0 1.5px rgba(123,108,255,.85)}' +
      '.vc-dk-card.del-mark{box-shadow:0 0 0 1.5px rgba(255,90,80,.9);position:relative}' +
      '.vc-dk-card.del-mark::after{content:"✕";position:absolute;top:-6px;right:-6px;width:16px;height:16px;border-radius:50%;background:#e0463c;color:#fff;font-size:10px;display:flex;align-items:center;justify-content:center}' +
      '.vc-dkp-txt{color:#aab6cf;font-size:11.5px;line-height:1.45;margin-top:2px;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}' +
      '.vc-dk-empty{color:#7f8aa6;font-size:12px;text-align:center;padding:12px 4px}' +
      '.vc-drag-ghost{position:fixed;left:0;top:0;z-index:2147481460;pointer-events:none;opacity:.92;border-radius:14px;overflow:hidden;color:#dde6f5;font-size:13px;line-height:1.5;' +
      'background:rgba(30,32,42,.94);border:1px solid rgba(126,171,255,.48);box-shadow:0 18px 50px rgba(0,0,0,.55),0 0 0 2px rgba(126,171,255,.15);' +
      'padding:10px 12px;max-height:min(280px,60vh);contain:layout paint;will-change:transform;transition:none!important}' +
      '.vc-drag-ghost *{animation:none!important;transition:none!important}' +
      '.vc-dk-m{font-size:10.5px;color:#6f7d9e;margin-top:3px}' +
      '.vc-fav-b{position:absolute;right:28px;bottom:5px;width:18px;height:18px;border-radius:50%;border:none;cursor:pointer;' +
      'background:rgba(255,255,255,.1);color:#8fa0c2;display:flex;align-items:center;justify-content:center;padding:0}' +
      '.vc-fav-b.on{color:#ffd54f;background:rgba(255,213,79,.15)}' +
      '.vc-pin-chip{display:flex;align-items:center;gap:7px;padding:6px 9px;border-radius:10px;' +
      'background:rgba(123,108,255,.12);border:1px solid rgba(123,108,255,.4);max-width:100%}' +
      '.vc-pc-l{font-size:12px;color:#c9bcff;font-weight:600;flex:none}' +
      '.vc-pc-s{font-size:11.5px;color:#8d97b4;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0}' +
      '.vc-pc-x{margin-left:auto;flex:none;width:18px;height:18px;border-radius:50%;border:none;background:rgba(255,255,255,.12);color:#cfd6ea;font-size:10px;cursor:pointer;display:flex;align-items:center;justify-content:center;padding:0}' +
      // 70 结构化信息卡(天气/新闻/事实)+双击选中态(带入 2.1 上下文)
      // 结果卡头部 = 直接套用我们方块的头部(小字主题色 + 整条当把手);⠿ 那个多余的拖动按钮已删
      '.vc-if-hd{font-size:12px;color:#b9a8ff;font-weight:600;margin:-4px -6px 6px;padding:5px 8px;display:flex;align-items:center;gap:6px;cursor:grab;' +
      'background:rgba(255,255,255,.06);border-radius:9px;font-weight:600}' +
      '.vc-if-hd span:first-child{flex:1}' +
      // 进度状态行(标题的下面一行,用户设计 #49/#52):进行中状态显示在标题区内、不在 body 上方。
      '.rc-turn-status{font-size:11px;color:#8a97b5;font-weight:500;margin:-2px 2px 6px;padding:0 2px;display:flex;align-items:center;gap:5px;line-height:1.5;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}' +
      '.rc-turn-status[hidden]{display:none}' +
      '.rc-turn-status.done{color:#79b891}' +
      '.rc-turn-status .vc-spin-s{width:10px;height:10px;flex:none}' +
      // #44 框选保存:工具长条前的选中圆点(实心=选中/空心=排除)+ 排除态整条变淡
      '.rc-sel-dot{flex:none;width:13px;height:13px;border-radius:50%;border:1.5px solid #7b8cae;margin-right:7px;cursor:pointer;box-sizing:border-box}' +
      '.rc-sel-dot.on{background:#7b6cff;border-color:#7b6cff}' +
      '.vc-fn.rc-fn-off{opacity:.4}' +
      '.vc-fn.rc-fn-off .vc-fn-t{text-decoration:line-through}' +
      '.rc-flow-selhint{font-size:11px;color:#8a97b5;margin:4px 2px 2px}' +
      '.vc-grip{flex:none;color:#6f7d9e;font-size:13px;letter-spacing:1px;padding:1px 5px;border-radius:6px;background:rgba(255,255,255,.06)}' +
      '.vc-bub-hd{display:flex;align-items:center;justify-content:flex-end;margin:-3px -4px 4px 0;cursor:grab;touch-action:none}' +
      '.vc-if-wt{font-size:26px;font-weight:600;letter-spacing:-.5px}' +
      '.vc-if-wc{font-size:14px;color:#cdd9f2;margin-top:1px}' +
      '.vc-if-ws{font-size:12px;color:#8a9bb4;margin-top:2px}' +
      '.vc-if-tip{font-size:12px;color:#b8c6e2;margin-top:6px;padding-top:6px;border-top:0.5px solid rgba(255,255,255,.1)}' +
      '.vc-if-ni{padding:5px 0;border-bottom:0.5px solid rgba(255,255,255,.08)}.vc-if-ni:last-child{border-bottom:none}' +
      '.vc-if-nt{font-size:13px;font-weight:600;color:#e8eefb}' +
      '.vc-if-ns{font-size:12px;color:#9fb0cf;margin-top:1px}' +
      '.vc-if-src{opacity:.65}' +
      '.vc-ig{display:flex;flex-wrap:wrap;gap:8px}' +
      '.vc-ig-cell{position:relative;width:calc(50% - 4px);border-radius:10px;overflow:hidden;background:rgba(255,255,255,.04)}' +
      // 单张图占满整宽:两列是给多图用的,一张图也缩在左半边纯属浪费(用户实测"图片无法最大化")。
      // :only-child 而不是 :first-child —— ✕ 删掉的图渲染成空串,剩最后一张时正好命中。
      '.vc-ig-cell:only-child{width:100%}' +
      // 手动调过大小的卡:让单图**吃满可用高度**。图片本来只按宽度撑、高度由宽高比定,
      // 所以你把卡片拉高之后下面全是空的。contain 保证不裁不变形,底色补住letterbox 区域。
      '.vc-card.vc-user-sized:not(.vc-dot):not(.vc-min) .vc-ig{height:100%}' +
      '.vc-card.vc-user-sized:not(.vc-dot):not(.vc-min) .vc-ig-cell:only-child{height:100%;display:flex;flex-direction:column}' +
      '.vc-card.vc-user-sized:not(.vc-dot):not(.vc-min) .vc-ig-cell:only-child .vc-ig-img{flex:1 1 auto;min-height:0;height:auto;object-fit:contain;background:rgba(0,0,0,.18)}' +
      '.vc-card.vc-drop-hot,.vc-if.vc-drop-hot{box-shadow:0 0 0 2.5px #0a84ff,0 12px 40px rgba(0,0,0,.4)!important;transition:box-shadow .12s}' +
      '.vc-imgdrop{margin-top:8px}' +
      '.vc-imgdrop img{max-width:100%;border-radius:8px;display:block}' +
      '.vc-imgdrop-t{font-size:11px;color:#9aa4b8;margin-top:3px}' +
      '.vc-ig-cell.vc-picked{box-shadow:0 0 0 2px rgba(123,108,255,.9)}' +
      '.vc-ig-img{width:100%;display:block;border-radius:10px 10px 0 0;cursor:pointer}' +
      '.vc-ig-t{font-size:10.5px;color:#9fb0cf;padding:3px 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}' +
      '.vc-vg-wrap{position:relative}' +
      '.vc-vg-empty{display:flex;align-items:center;justify-content:center;min-height:84px;color:#7d8db0;font-size:11px;background:#10182b}' +
      '.vc-vg-play{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:34px;height:34px;border-radius:50%;border:none;' +
      'background:rgba(0,0,0,.6);color:#fff;font-size:13px;display:flex;align-items:center;justify-content:center;cursor:pointer;z-index:2}' +
      '.vc-vg-tag{position:absolute;top:4px;left:4px;z-index:2;font-size:9px;padding:1px 5px;border-radius:5px;background:#c00;color:#fff}' +
      '.vc-vg-tag.bili{background:#fb7299}' +
      '.vc-vg-ch{color:#7d8db0;font-size:9.5px}' +
      '.vc-ig-x{position:absolute;top:4px;right:4px;width:18px;height:18px;border-radius:50%;border:none;background:rgba(0,0,0,.55);color:#fff;' +
      'font-size:10px;display:flex;align-items:center;justify-content:center;cursor:pointer;padding:0;z-index:2}' +
      '.vc-if-fa{font-size:15px;font-weight:600}' +
      '.vc-if-fd{font-size:12.5px;color:#b8c6e2;margin-top:3px}' +
      '.vc-if-g{font-size:13.5px;line-height:1.55}' +
      '.vc-if-srcs{margin-top:7px;font-size:11px}.vc-if-srcs a{color:#7ea2e6;text-decoration:none}' +
      '.vc-pinnable{-webkit-user-select:none;user-select:none;-webkit-touch-callout:none}' +
      '.vc-picked{box-shadow:0 0 0 2px rgba(123,108,255,.85),0 12px 40px rgba(0,0,0,.4) !important;border-radius:16px}' +
      '.asst-msg.vc-if{max-width:96%;width:min(100%,340px)}' +
      // Apple 简约风:毛玻璃 + iOS 系统色(绿 #30d158/蓝 #0a84ff/橙 #ff9f0a)+ 细边 + SF 线条图标 + sheet 抓手
      '#rc-vc{position:fixed;right:14px;bottom:78px;z-index:2147482000;width:min(320px,88vw);background:rgba(24,30,46,.78);' +
      '-webkit-backdrop-filter:blur(24px) saturate(1.5);backdrop-filter:blur(24px) saturate(1.5);' +
      'border:1px solid rgba(255,255,255,.09);border-radius:16px;box-shadow:0 12px 40px rgba(0,0,0,.45);color:#eaf0fa;' +
      'font-size:13px;overflow:hidden;font-family:-apple-system,system-ui,sans-serif}' +
      // sheet 抓手:Apple 底部面板同款小横条,整条可拖 → 调下方对话区高度
      '#rc-vc .vc-grab{padding:7px 0 3px;display:flex;justify-content:center;cursor:ns-resize;touch-action:none}' +
      '#rc-vc .vc-grab::before{content:"";width:36px;height:5px;border-radius:3px;background:rgba(255,255,255,.28)}' +
      '#rc-vc .vc-head{display:flex;align-items:center;gap:8px;padding:2px 12px 8px}' +
      '#rc-vc .vc-dot{width:8px;height:8px;border-radius:50%;background:#ff9f0a;flex:none;animation:vcDot 1.1s ease-in-out infinite}' +
      '#rc-vc.on .vc-dot{background:#30d158;animation:vcDot 2.2s ease-in-out infinite}' +
      '@keyframes vcDot{0%,100%{opacity:1}50%{opacity:.35}}' +
      '#rc-vc .vc-st{flex:1;color:rgba(235,240,250,.55);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
      '#rc-vc .vc-new,#rc-vc .vc-x{background:rgba(255,255,255,.08);border:none;color:rgba(235,240,250,.75);width:28px;height:28px;' +
      'border-radius:50%;cursor:pointer;flex:none;display:flex;align-items:center;justify-content:center;padding:0;-webkit-tap-highlight-color:transparent}' +
      '#rc-vc .vc-new:active,#rc-vc .vc-x:active{background:rgba(255,255,255,.18)}' +
      '#rc-vc .vc-x{color:#ff6961}' +
      // 对话区:累积消息流(iMessage 风),高度由抓手拖出来、持久化
      '#rc-vc .vc-sub{padding:2px 12px 8px;height:132px;overflow-y:auto;display:flex;flex-direction:column;gap:6px;overscroll-behavior:contain}' +
      '#rc-vc .vc-m{max-width:86%;padding:6px 11px;border-radius:16px;line-height:1.45;word-break:break-word;white-space:pre-wrap}' +
      '#rc-vc .vc-mu{align-self:flex-end;background:#0a84ff;color:#fff;border-bottom-right-radius:5px}' +
      '#rc-vc .vc-ma{align-self:flex-start;background:rgba(255,255,255,.12);color:#eaf0fa;border-bottom-left-radius:5px}' +
      '#rc-vc .vc-vids{display:flex;gap:6px;overflow-x:auto;padding:0 12px 10px}' +
      '#rc-vc .vc-vid{flex:0 0 128px;cursor:pointer;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.09);border-radius:10px;overflow:hidden}' +
      '#rc-vc .vc-vid img{width:100%;height:72px;object-fit:cover;display:block}' +
      '#rc-vc .vc-vid div{font-size:11px;padding:4px 6px;line-height:1.3;max-height:33px;overflow:hidden}' +
      // 内嵌形态:活在侧栏输入框上方(不再 fixed 右下角)
      '#rc-vc.vc-inline{display:none !important}' +   // 66 用户裁定:输入框上方的通话条残留版面撤除(状态看按钮/字幕;对话在侧栏流)
      '#asst-input.vc-live{box-shadow:0 0 0 1.5px rgba(94,92,230,.6),0 0 16px rgba(94,92,230,.22);border-radius:14px;transition:box-shadow .3s}' +
      // 侧栏 composer 里的通话入口按钮(样式镜像 #asst-mic;通话中绿色呼吸)
       '#asst-call,#asst-computer{background:#16203a;border:1px solid #2a3a63;color:#9fb4e0;width:42px;height:42px;border-radius:12px;cursor:pointer;flex:none;display:flex;align-items:center;justify-content:center;transition:background .2s,color .2s,border-color .2s,transform .1s;-webkit-tap-highlight-color:transparent}' +
       '#asst-call:active,#asst-computer:active{transform:scale(.9)}' +
       '#asst-call.on{background:#1a7f4b;border-color:#1a7f4b;color:#fff;animation:vcCallPulse 1.6s ease-in-out infinite}' +
      // 播报中:蓝色快脉冲(盖过 .on 绿;user 开口打断后自动回绿)
       '#asst-call.speaking{background:#0a84ff;border-color:#0a84ff;color:#fff;animation:vcCallPulse 1s ease-in-out infinite}' +
       '#asst-call.connecting{background:#8a5a00;border-color:#ff9f0a;color:#ffd60a;animation:vcCallPulse .7s ease-in-out infinite}' +
       '#asst-computer.on{background:#1a7f4b;border-color:#1a7f4b;color:#fff;animation:vcCallPulse 1.6s ease-in-out infinite}' +
       '#asst-computer.speaking{background:#0a84ff;border-color:#0a84ff;color:#fff;animation:vcCallPulse 1s ease-in-out infinite}' +
       '#asst-computer.connecting{background:#8a5a00;border-color:#ff9f0a;color:#ffd60a;animation:vcCallPulse .7s ease-in-out infinite}' +
       '#asst-computer.native-app-required,#vc-top-computer.native-app-required{opacity:.38;cursor:not-allowed;animation:none!important}' +
      // 桥接模式(ReaderPC 仅桥接,语音未接管):蓝色描边、不脉冲——一眼区别于绿(通话)与灰(不可用)
       '#asst-computer.bridge-only,#vc-top-computer.bridge-only{background:#12233d;border-color:#4da3ff;color:#4da3ff;animation:none!important}' +
       '#asst-call.vc-review-disabled,#vc-top-call.vc-review-disabled,#asst-computer.vc-review-disabled,#vc-top-computer.vc-review-disabled{opacity:.48;cursor:not-allowed;animation:none!important}' +
      // ASR 连续听(mic 长按开):紫色呼吸,与系统听写的蓝 .on 区分
      '#asst-mic.asr{background:#bf5af2 !important;border-color:#bf5af2 !important;color:#fff !important;animation:vcCallPulse 1.6s ease-in-out infinite}' +
      // 朗读开关播报中:淡蓝呼吸
      '.vc-speak-tg.speaking{animation:vcCallPulse 1.2s ease-in-out infinite}' +
      // 记忆起点选择行(v3-⑰c)
      '.vc-recall-pane{display:flex;gap:6px;align-items:center;margin:6px 10px;padding:6px 8px;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.09);border-radius:10px;flex-wrap:wrap}' +
      '.vc-recall-pane input{background:#0d1426;border:1px solid #2a3a63;color:#dbe7ff;border-radius:7px;padding:4px 6px;font-size:12px;flex:1 1 150px;min-width:0}' +
      '.vc-recall-pane button{background:#1a2233;border:1px solid #2a3a63;color:#9fb4e0;border-radius:7px;padding:4px 10px;font-size:12px;cursor:pointer;flex:none}' +
      '@keyframes vcCallPulse{0%,100%{box-shadow:0 0 0 0 rgba(26,127,75,.5)}50%{box-shadow:0 0 0 7px rgba(26,127,75,0)}}' +
      // 长按到点确认(㉒):弹一下+变紫,到点瞬间就知道"够了可以松手"
      '@keyframes vcLpPop{0%{transform:scale(1)}45%{transform:scale(1.28)}70%{transform:scale(.92)}100%{transform:scale(1)}}' +
      '.vc-lp-pop{animation:vcLpPop .4s ease !important;color:#bf5af2 !important;border-color:#bf5af2 !important}' +
      // 顶栏语音按钮(侧栏收起时显示;样式蹭顶栏原生 button,状态只动颜色+呼吸)
      '#vc-top-mic.on{color:#0a84ff !important;border-color:#0a84ff !important}' +
      '#vc-top-mic.asr{color:#bf5af2 !important;border-color:#bf5af2 !important;animation:vcCallPulse 1.6s ease-in-out infinite}' +
       '#vc-top-call.on{color:#30d158 !important;border-color:#30d158 !important;animation:vcCallPulse 2.2s ease-in-out infinite}' +
       '#vc-top-call.speaking{color:#0a84ff !important;border-color:#0a84ff !important;animation:vcCallPulse 1s ease-in-out infinite}' +
       '#vc-top-call.connecting{color:#ff9f0a !important;border-color:#ff9f0a !important;animation:vcCallPulse .7s ease-in-out infinite}' +
       '#vc-top-computer.on{color:#30d158 !important;border-color:#30d158 !important;animation:vcCallPulse 2.2s ease-in-out infinite}' +
       '#vc-top-computer.speaking{color:#0a84ff !important;border-color:#0a84ff !important;animation:vcCallPulse 1s ease-in-out infinite}' +
       '#vc-top-computer.connecting{color:#ff9f0a !important;border-color:#ff9f0a !important;animation:vcCallPulse .7s ease-in-out infinite}' +
       '#vc-top-mic,#vc-top-call,#vc-top-computer{-webkit-user-select:none;user-select:none;-webkit-touch-callout:none;touch-action:manipulation}' +
      // 长按/连点这些控件时禁掉 iOS 文本选中高亮与放大镜(长按手势专用控件,选中毫无意义)
       '#asst-call,#asst-computer,#asst-mic,#vc-tool-btn,.vc-speak-tg,#asst-input button,#asst-quick button,#rc-vc .vc-grab,#rc-vc .vc-head button{-webkit-user-select:none;user-select:none;-webkit-touch-callout:none;touch-action:manipulation}' +
      '#rc-vc .vc-grab{touch-action:none}' +   // 抓手保持 none(要拖拽调高)
      // 工具调用状态按钮 + 详情弹层(v3-⑤)
      '#vc-tool-btn{background:#16203a;border:1px solid #2a3a63;color:#9fb4e0;width:42px;height:42px;border-radius:12px;cursor:pointer;flex:none;display:none;align-items:center;justify-content:center;font-size:18px;-webkit-tap-highlight-color:transparent}' +
      '#vc-tool-btn.ok{color:#34d399;border-color:#1f6b4a}' +
      '#vc-tool-btn.err{color:#f87171;border-color:#7f2a2a}' +
      '.vc-spin{width:15px;height:15px;border:2px solid #3a4a73;border-top-color:#9fcbff;border-radius:50%;display:inline-block;animation:vcSpin .8s linear infinite;vertical-align:-2px}' +
      '@keyframes vcSpin{to{transform:rotate(360deg)}}' +
      // 侧栏对话流里的工具调用详情卡(v3-⑯,感叹号式)
      '.vc-tcard{margin:4px 0;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.09);border-radius:10px;font-size:12px;overflow:hidden}' +
      '.vc-tcard.err{border-color:rgba(255,105,97,.35)}' +
      '.vc-tc-h{display:flex;align-items:center;gap:7px;padding:6px 10px;cursor:pointer;-webkit-user-select:none;user-select:none;-webkit-tap-highlight-color:transparent}' +
      '.vc-tc-h:active{background:rgba(255,255,255,.06)}' +
      '.vc-tc-st{flex:none}.vc-tcard .vc-tc-st{color:#30d158}.vc-tcard.err .vc-tc-st{color:#ff6961}' +
      '.vc-tc-l{flex:1;color:#cdd9f2;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
      '.vc-tc-i{flex:none;display:inline-flex;color:#8fa4cc}.vc-tc-i svg{width:13px;height:13px}' +
      '.vc-tc-t{flex:none;color:#8a9bb4;font-size:11px}' +
      '.vc-tc-x{flex:none;color:#8a9bb4}' +
      '.vc-tc-b{padding:2px 10px 8px;border-top:1px solid rgba(255,255,255,.07)}' +
      '.vc-tc-k{color:#7c93c4;font-size:11px;margin:6px 0 2px;font-weight:600}' +
      '.vc-tc-v{color:#b9c6e0;font-family:ui-monospace,Menlo,monospace;font-size:11px;line-height:1.5;word-break:break-all;white-space:pre-wrap;max-height:180px;overflow-y:auto}';
    document.head.appendChild(s);
  }

  function esc(s) { var d = document.createElement('div'); d.textContent = String(s == null ? '' : s); return d.innerHTML; }
  function setSt(t) { if (box) box.querySelector('.vc-st').textContent = t; }
  function callBtnConnecting(on) {   // 96(用户设计):按下→接通之间的"正在等它开启"视觉态(琥珀快脉冲)
    var b = document.getElementById('asst-call');
    if (b) b.classList[on ? 'add' : 'remove']('connecting');
  }
  function callBtnOn(on) {
    var b = document.getElementById('asst-call'), m = document.getElementById('asst-mic');
    callBtnConnecting(false);   // 96:状态确定(接通/挂断/失败)即退出等待态
    if (!on) { if (b) b.classList.remove('on'); if (m) m.classList.remove('asr'); return; }
    if (mode === 'agent') { if (m) m.classList.add('asr'); }   // ASR 连续听:麦克风按钮紫色呼吸(区别系统听写的蓝)
    else if (b) b.classList.add('on');                          // S2S:电话按钮绿色呼吸
  }
  function callBtnSpeaking(on) {
    if (mode === 'agent') { var tg = document.querySelector('.vc-speak-tg'); if (tg) tg.classList[on ? 'add' : 'remove']('speaking'); return; }
    var b = document.getElementById('asst-call'); if (b) b.classList[on ? 'add' : 'remove']('speaking');
  }
  function computerBtnConnecting(on) {
    var b = document.getElementById('asst-computer');
    if (b) b.classList[on ? 'add' : 'remove']('connecting');
  }
  function computerBtnOn(on) {
    var b = document.getElementById('asst-computer');
    computerBtnConnecting(false);
    if (!b) return;
    b.classList[on ? 'add' : 'remove']('on');
    if (!on) b.classList.remove('speaking');
  }
  function computerBtnSpeaking(on) {
    var b = document.getElementById('asst-computer');
    if (b) b.classList[on ? 'add' : 'remove']('speaking');
  }
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
  // v3-⑯b(用户设计):转圈按钮=纯"进行中"指示——调用开始现身旋转,**点击即中止**,
  //   结束/中止后自动消失;查记录的职责全归对话流详情卡(threadToolCard)。
  // 69 工具图标(用户需求:不同工具不同符号):按类别映射 SF 线条 SVG(currentColor)
  var _TICONS = {
    read: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><path d="M3 2.8h7.5L13 5.3v8H3z"/><path d="M5.4 7h5.2M5.4 9.3h5.2M5.4 11.6h3.4"/></svg>',
    search: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><circle cx="7" cy="7" r="4.2"/><path d="M10.3 10.3L13.6 13.6"/></svg>',
    eye: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M1.8 8s2.3-4.2 6.2-4.2S14.2 8 14.2 8s-2.3 4.2-6.2 4.2S1.8 8 1.8 8z"/><circle cx="8" cy="8" r="1.9"/></svg>',
    write: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"><path d="M9.8 3.2l3 3L6 13H3v-3z"/><path d="M8.4 4.6l3 3"/></svg>',
    nav: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8h9M8.6 4.2L12.4 8l-3.8 3.8"/></svg>',
    route: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M2 8h3.2C8 8 8 4.5 10.8 4.5H13M5.2 8C8 8 8 11.5 10.8 11.5H13"/><path d="M11.4 2.8L13.2 4.5l-1.8 1.7M11.4 9.8l1.8 1.7-1.8 1.7"/></svg>',
    dict: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><path d="M3.5 2.5h9v11h-9a1.2 1.2 0 0 1 0-2.4h9"/><path d="M6.2 8.6L8 4.8l1.8 3.8M6.7 7.6h2.6"/></svg>',
    net: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><circle cx="8" cy="8" r="5.6"/><path d="M2.4 8h11.2M8 2.4c-3.4 3.4-3.4 7.8 0 11.2M8 2.4c3.4 3.4 3.4 7.8 0 11.2"/></svg>',
    gear: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="2.2"/><path d="M8 2.6v1.7M8 11.7v1.7M2.6 8h1.7M11.7 8h1.7M4.2 4.2l1.2 1.2M10.6 10.6l1.2 1.2M11.8 4.2l-1.2 1.2M5.4 10.6l-1.2 1.2"/></svg>'
  };
  function _toolIcon(name) {
    name = String(name || '');
    if (/^(route_to_text|deep_think)/.test(name)) return _TICONS.route;
    if (/^see_/.test(name)) return _TICONS.eye;
    if (/^(web_search)/.test(name)) return _TICONS.net;
    if (/^(search|find|recall)/.test(name)) return _TICONS.search;
    if (/^(read|summarize|toc|page_vocab)/.test(name)) return _TICONS.read;
    if (/^(make|add|highlight|note|mark)/.test(name)) return _TICONS.write;
    if (/^(goto|turn|go_to)/.test(name)) return _TICONS.nav;
    if (/^(translate|dict|lookup)/.test(name)) return _TICONS.dict;
    return _TICONS.gear;
  }
  // ── 工具指示器 v2(rc-toolchip):每次工具调用一个 chip(圆/长条/方块),状态由此驱动 ──
  //    key = 工具名+轮次(同轮同工具复用同一个 chip);后台任务(制卡等)拿 task_id 继续轮询步骤+结果。
  var _chipOf = {};
  // 136(用户实测"读页面这类工具不消失、还变成小方块"的根因之一):
  //   relay 的 running 事件曾**只带 label 不带 tool**,而 done 带 tool 且 label 可能多出"(已过期)"后缀
  //   → key 对不上:done 没找到那张 running 的卡,又新建一张收尾,**原来那张永远卡在"进行中"**,
  //     20s 后被自动收成小方块。所以 key 只能用**稳定标识**(call_id / 工具名),绝不能用会变的 label。
  function _chipKey(p) { return (p.call_id || p.id || '') + '|' + (p.tool || p.label || 'tool'); }
  var _chipSeq = [];   // 按开工顺序排(工具是串行的)——key 万一没配上,收尾时认领最近那个未收尾的
  function _chipStart(p) {
    if (!(window.RC && RC.toolChip)) return null;
    var k = _chipKey(p);
    if (_chipOf[k]) return _chipOf[k];
    var c = RC.toolChip.create({ tool: p.tool || '', label: p.label || p.tool || '工具' });
    RC.toolChip.progress(c, (p.label || '处理') + '…');
    _chipOf[k] = c; c._k = k; _chipSeq.push(c);
    // 141(轮次容器):工具运行中 → 容器里显示一条**临时**指示(不是 part、不落库)。
    //   ⚠ 必须同时 silence 掉 chip 自己的 DOM —— 我上一版只拆了 absorb,chip 照样在 thread 里画自己那张卡,
    //   于是变成「chip 卡 + 容器卡」两块(用户实测图1)。显示权归容器,chip 只留对象供后台任务追踪。
    try {
      if (RC.turnCard && window.__asstVoiceTid) {
        RC.turnCard.busy(window.__asstVoiceTid(), p.label || p.tool || '工具');
        RC.toolChip.silence(c);
      }
    } catch (e) {}
    return c;
  }
  window.__vcChipSeqClear = function () { _chipSeq = []; _chipOf = {}; };   // 清空对话 → 待收尾队列一起清
  function _chipTake(p) {   // 收尾时取那张卡:先按 key,不中就认领最近一张未收尾的(兼容 running 不带工具名的老 relay)
    var k = _chipKey(p), c = _chipOf[k];
    if (c) { delete _chipOf[k]; _chipSeq = _chipSeq.filter(function (x) { return x !== c; }); return c; }
    c = _chipSeq.pop();
    if (c) { delete _chipOf[c._k]; return c; }
    return null;
  }
  function _chipMeta(p) {   // 调用详情(=原「!」面板内容:指令/上下文/参数/喂回结果)
    var rows = [];
    if (p.cmd) rows.push(['指令(S2S 原话)', p.cmd]);
    if (p.ctx_brief) {
      var cb = p.ctx_brief, cp = ['第' + (cb.page || '?') + '页'];
      if (cb.ink) cp.push('墨迹' + cb.ink + '笔');
      if (cb.sel) cp.push('选中' + cb.sel + '字');
      rows.push(['携带上下文', cp.join(' · ')]);
    }
    try { if (p.args && Object.keys(p.args).length) rows.push(['参数', JSON.stringify(p.args)]); } catch (e) {}
    if (p.took_s != null) rows.push(['耗时', p.took_s + 's' + (p.cached ? ' · 复用缓存' : '')]);
    if (p.rag) rows.push(['喂回给它播报的结果', p.rag]);
    else if (p.result_brief) rows.push(['结果', p.result_brief]);
    return rows;
  }
  function _toolCardRepositorySource(gid, payload, tool) {
    payload = payload || {};
    // 普通 make_anki 没有上游 draftId，但浮层与流内宿主都会用
    // requireDraftIdForReplay 再登记同一 gid。用 gid 派生本地重放身份，既稳定
    // 非空，也不借用页面 placement 或外部 Anki/Pi 身份。
    var localDraftId = 'reader-card-tool:' + String(gid || '');
    var explicit = String(payload.source_ref || payload.src || '').slice(0, 4096);
    var quote = '';
    try {
      quote = String(payload.source_text || payload.text ||
        (payload.args && payload.args.text) || '').slice(0, 32768);
    } catch (_) {}
    var exact = payload.source_highlight &&
      typeof payload.source_highlight === 'object'
      ? payload.source_highlight : null;
    var source = {
      kind: 'reader-tool-card-draft',
      sourceId: explicit || localDraftId,
      draftId: localDraftId,
      tool: String(tool || 'make_anki').slice(0, 160),
      legacy: { piEntityRegistered: !!payload.id }
    };
    // 普通制卡的 text 是用户/助手生成内容，不是当前页引用。
    // 只有上游显式给出 source_ref 或精确高亮合同时，才把书页来源
    // 写入卡仓；绝不用“用户此刻刚好打开的页”伪造 provenance。
    if (exact) {
      var file = String(exact.file || '').slice(0, 4096);
      source.kind = 'reader-book-exact-card-draft';
      source.documentId = file;
      source.quote = String(exact.text || exact.sourceText || quote).slice(0, 32768);
      source.location = exact.target && typeof exact.target === 'object'
        ? exact.target : {};
      if (!explicit && file) source.sourceId = 'reader-book:' + file;
    } else if (explicit) {
      source.kind = 'reader-book-reference-card-draft';
      if (quote) source.quote = quote;
    } else if (quote) {
      source.context = quote;
    }
    return source;
  }
  function _applyCardSourceHighlight(payload, gid) {
    var request = payload && payload.source_highlight;
    if (!request) return Promise.resolve(false);
    if (typeof window.__bwReaderHighlightExactText !== 'function') {
      return Promise.reject(new Error('BW_READER_CARD_SOURCE_HIGHLIGHT_UNAVAILABLE'));
    }
    var hex = String(gid || '').replace(/^card_/, '').replace(/[^a-f0-9]/g, '');
    if (hex.length < 8) {
      return Promise.reject(new Error('BW_READER_CARD_SOURCE_HIGHLIGHT_ID'));
    }
    return Promise.resolve(window.__bwReaderHighlightExactText({
      file: String(request.file || ''),
      target: request.target,
      text: String(request.text || ''),
      color: request.color || 'green',
      note: String(request.note || '').slice(0, 1000),
      mutationId: 'c_' + hex.slice(0, 24)
    })).then(function () { return true; });
  }
  function _validateToolCardSource(payload) {
    var request = payload && payload.source_highlight;
    if (!request) return Promise.resolve({ generic: true });
    if (typeof window.__bwReaderValidateExactSource !== 'function') {
      return Promise.reject(new Error('BW_READER_CARD_SOURCE_VALIDATOR_UNAVAILABLE'));
    }
    return Promise.resolve(window.__bwReaderValidateExactSource({
      file: String(request.file || ''),
      target: request.target,
      sourceText: String(request.text || request.sourceText || '')
    }));
  }
  function _projectCardSourceHighlight(payload, gid) {
    return _applyCardSourceHighlight(payload, gid).catch(function (error) {
      try {
        if (window.dlog) window.dlog(
          '卡片已保存；来源高亮投影失败 ' +
          String(error && (error.code || error.message) || error).slice(0, 160),
          '#f0c674'
        );
      } catch (_) {}
      return false;
    });
  }
  function _chipEnd(p) {
    if (!(window.RC && RC.toolChip)) return;
    try { _rtcCreFetch._t = 0; _rtcCreFetch(); } catch (e) {}   // 工具完成=可能有新创造物 → 强制刷新清单缓存
    // P0(2026-07-21 用户实锤"制卡卡永远处理中"根因):工具一旦收尾=必须解除 turnCard 的 busy 转圈,
    //   覆盖**所有**提前 return 分支(error / task_id 后台任务),不再只在正常分支末尾 idle
    //   (旧:error/task_id 分支提前 return → turnCard.statusEl 的"处理中"旋转永不 hidden = 永远转圈)。
    try { if (RC.turnCard && window.__asstVoiceTid) RC.turnCard.idle(window.__asstVoiceTid()); } catch (e) {}
    var c = _chipTake(p);
    if (!c) c = RC.toolChip.create({ tool: p.tool || '', label: p.label || '工具' });   // 没见过 running(缓存命中/补发)→ 现造一个直接收尾
    // 阶段1(语音工具卡聚合):本轮有 turnCard 容器时,收尾拿到的 chip 也要 silence——_chipStart 已 silence 了
    //   running 期那张;但 _chipTake 未命中(缓存命中/补发/key 变了)时上面**新建**的这张是非 silence 的,
    //   若不 silence,下面 done/progress 会画出独立散卡(与容器里的 tool part 重复=用户实测三张散卡)。
    //   已 silence 的 chip(absorbed=[])跳过。工具**统一** addPart 进 turnCard(下方各分支已接线),对齐文字模式。
    try { if (RC.turnCard && window.__asstVoiceTid && !c.absorbed) RC.toolChip.silence(c); } catch (e) {}
    if (p.tool) { try { RC.toolChip.retype(c, p.tool); } catch (e) {} }   // 136:done 才拿到真实工具名 → 重判类型(执行类=完成即消失)
    if (p.sub_steps && p.sub_steps.length) { try { RC.toolChip.addSteps(c, p.sub_steps); } catch (e) {} }   // 137:工具内部子步骤 → 并进这张卡的步骤(不另起卡)
    if (p.vision && p.vision.length) { try { RC.toolChip.setVision(c, p.vision); } catch (e) {} }   // 141:真正喂给 AI 的图(see_ink 的笔迹合成图等)
    RC.toolChip.setMeta(c, _chipMeta(p));
    if (p.status === 'error') {
      RC.toolChip.fail(c, p.label || '失败');
      try {
        if (RC.turnCard && window.__asstVoiceTid) {
          var errorDetail = String(p.rag || p.result_brief || p.label || '失败').slice(0, 6000);
          RC.turnCard.addPart(window.__asstVoiceTid(), {
            kind: 'tool', tool: p.tool || '', label: (p.label || '工具') + '(失败)',
            args: p.args || {}, steps: p.sub_steps || [], result: errorDetail,
            vision: p.vision || [], took_s: p.took_s, model: p.model,
            error: errorDetail
          });
        }
      } catch (e) {}
      return;
    }
    // ④ 同步制卡(2026-07-21 用户拍板:工具等做完才返回,rag 直接带 cards)→ 不轮询,直接双宿主显示卡片
    var _sc = null, _sdrf = false;
    try { var _sr = (p.result && p.result.cards) ? p.result : ((typeof p.rag === 'string') ? JSON.parse(p.rag) : (p.rag || {}));
          if (_sr && _sr.cards && _sr.cards.length) { _sc = _sr.cards; _sdrf = (_sr.deferred !== false); } } catch (e) {}
    if (_sc) {
      RC.toolChip.done(c, { summary: '生成了 ' + _sc.length + ' 张卡片草稿' });
      var _gid = (_sr.id && /^card_[a-f0-9]{4,64}$/.test(_sr.id)) ? _sr.id : '';
      if (!_gid) {
        try {
          var _repoForGid = window.BWReaderRuntime && window.BWReaderRuntime.cardRepository;
          _gid = _repoForGid && typeof _repoForGid.newCardId === 'function'
            ? _repoForGid.newCardId() : '';
        } catch (_) { _gid = ''; }
      }
      if (!_gid) _gid = 'fcg_' + RC.voiceCard.mkCid();
      var _stid = window.__asstVoiceTid && window.__asstVoiceTid();
      if (_sdrf && RC.flashcard &&
          typeof RC.flashcard.presentDraft === 'function') {
        _validateToolCardSource(_sr).then(function () {
          return RC.flashcard.presentDraft(_sc, _gid, {
            entityRegistered: !!_sr.id,
            repositorySource: _toolCardRepositorySource(_gid, _sr, p.tool),
            localDraft: null
          });
        }).then(function (rendered) {
          if (!rendered) throw new Error('BW_CARD_REPOSITORY_DRAFT_RENDER_FAILED');
          // 本地仓库先落稳，再把同一 gid 暴露到侧栏。否则用户在慢存储上
          // 立即点“保存”会先于 registerDraft，造成一张看得到却无法确认的卡。
          if (_stid && RC.turnCard) {
            RC.turnCard.idle(_stid);
            // 带上与浮层完全相同的身份：同 gid/cards/source 的再次 registerDraft
            // 是幂等的，靠身份一致而不是靠跳过登记来避免第二个实体。
            // entityRegistered 仍是 Pi 兼容标志，沿用上游的值，不在这里翻成 true。
            RC.turnCard.addPart(_stid, {
              kind: 'cards', cards: _sc, draft: true, gid: _gid,
              entityRegistered: !!_sr.id,
              repositorySource: _toolCardRepositorySource(_gid, _sr, p.tool),
              localDraft: null
            });
          }
          _projectCardSourceHighlight(_sr, _gid);
        }).catch(function (error) {
          try { if (window.dlog) window.dlog(
            '卡片草稿本地登记失败 ' +
            String(error && (error.code || error.message) || error).slice(0, 160),
            '#ff6b6b'
          ); } catch (_) {}
          try {
            if (_stid && RC.turnCard) RC.turnCard.addPart(_stid, {
              kind: 'text',
              text: '✗ 卡片草稿未写入本地仓库，未显示可保存卡片。'
            });
          } catch (_) {}
        });
      } else if (_sdrf) {
        try {
          if (_stid && RC.turnCard) RC.turnCard.addPart(_stid, {
            kind: 'text',
            text: '✗ Reader 本地卡片仓库未加载，未显示可保存卡片。'
          });
        } catch (_) {}
      } else {
        if (_stid && RC.turnCard) {
          RC.turnCard.idle(_stid);
          RC.turnCard.addPart(_stid, {
            kind: 'cards', cards: _sc, draft: _sdrf, gid: _gid
          });
        }
        if (RC.flashcard && typeof RC.flashcard.renderEntity === 'function') {   // 非草稿预览仍走学习卡唯一组合入口
        RC.flashcard.renderEntity(null, {
          surface: 'float',
          mode: 'preview',
          cards: _sc,
          gid: _gid,
          label: '🎴 制卡',
          tool: 'make_anki',
          type: '#b9a8ff',
          icon: '🎴',
          form: 'full',
          selectionLabel: '卡片'
        });
        }
      }
      return;
    }
    // 后台任务(记笔记/生词等仍异步):工具只是"派发成功",真正的步骤与结果要继续轮询 task-status
    var tid = p.task_id || (p.result && p.result.task_id) || _pickTaskId(p.rag);
    // CLI 委托任务(make_paper/do_task):走**跟文字侧栏同一套** _trackCliTask —— 轮询 task-status 把 CLI
    //   内部工具填进本轮容器的【流程】+ 增量结果 + 建纸。否则语音路流程恒空「本轮没有工具调用」(用户实测)。
    if (tid && (p.tool === 'make_paper' || p.tool === 'do_task' || p.tool === 'read_check_report' || p.tool === 'run_saved_task')
        && window.RC && RC.assistant && RC.assistant.trackCliTask && RC.turnCard && window.__asstVoiceTid) {
      try { RC.assistant.trackCliTask(window.__asstVoiceTid(), tid, p.label || p.tool || '造纸'); } catch (e) {}
      return;
    }
    if (tid) {
      RC.toolChip.progress(c, '已派发,正在后台执行…');
      var _turnTid = (window.__asstVoiceTid && window.__asstVoiceTid()) || null;   // 捕获当前轮(回调时可能已换轮)
      // 后台任务完成 → 把 result.cards 渲进 turnCard 卡片预览(用户实锤"没看到卡片预览");失败落错误 part
      _chipTrackTask(c, tid, function (stt, d) {
        try {
          if (stt === 'done' && d && d.result && d.result.cards && d.result.cards.length) {
            var _cds = d.result.cards, _drf = !!d.result.deferred;
            var _gid2 = (d.result.id && /^card_[a-f0-9]{4,64}$/.test(d.result.id))
              ? d.result.id : '';
            if (!_gid2) {
              try {
                var _repoForGid2 = window.BWReaderRuntime && window.BWReaderRuntime.cardRepository;
                _gid2 = _repoForGid2 && typeof _repoForGid2.newCardId === 'function'
                  ? _repoForGid2.newCardId() : '';
              } catch (_) { _gid2 = ''; }
            }
            if (!_gid2) _gid2 = 'fcg_' + RC.voiceCard.mkCid();
            // ④ 字幕模式浮层镜像(天气卡双宿主:侧栏开→容器隐藏、关侧栏=字幕模式浮现)+ 长按独立选中
            if (_drf && RC.flashcard &&
                typeof RC.flashcard.presentDraft === 'function') {
              _validateToolCardSource(d.result).then(function () {
                return RC.flashcard.presentDraft(_cds, _gid2, {
                  entityRegistered: !!d.result.id,
                  repositorySource: _toolCardRepositorySource(
                    _gid2, d.result, 'make_anki'
                  ),
                  localDraft: null
                });
              }).then(function (rendered) {
                if (!rendered) throw new Error('BW_CARD_REPOSITORY_DRAFT_RENDER_FAILED');
                if (_turnTid && RC.turnCard) RC.turnCard.addPart(_turnTid, {
                  kind: 'cards', cards: _cds, draft: true, gid: _gid2
                });
                _projectCardSourceHighlight(d.result, _gid2);
              }).catch(function (error) {
                try { if (window.dlog) window.dlog(
                  '后台卡片草稿本地登记失败 ' +
                  String(error && (error.code || error.message) || error).slice(0, 160),
                  '#ff6b6b'
                ); } catch (_) {}
                try {
                  if (_turnTid && RC.turnCard) RC.turnCard.addPart(_turnTid, {
                    kind: 'text',
                    text: '✗ 卡片草稿未写入本地仓库，未显示可保存卡片。'
                  });
                } catch (_) {}
              });
            } else if (_drf) {
              try {
                if (_turnTid && RC.turnCard) RC.turnCard.addPart(_turnTid, {
                  kind: 'text',
                  text: '✗ Reader 本地卡片仓库未加载，未显示可保存卡片。'
                });
              } catch (_) {}
            } else {
              if (_turnTid && RC.turnCard) RC.turnCard.addPart(_turnTid, {
                kind: 'cards', cards: _cds, draft: _drf, gid: _gid2
              });
              if (RC.flashcard && typeof RC.flashcard.renderEntity === 'function') {
              RC.flashcard.renderEntity(null, {
                surface: 'float',
                mode: 'preview',
                cards: _cds,
                gid: _gid2,
                label: '🎴 制卡',
                tool: 'make_anki',
                type: '#b9a8ff',
                icon: '🎴',
                form: 'full',
                selectionLabel: '卡片'
              });
              }
            }
          } else if (stt === 'error') {
            if (_turnTid && RC.turnCard) RC.turnCard.addPart(_turnTid, { kind: 'text', text: '✗ 制卡没成:' + ((d && d.error) || '内容可能不适合制卡') });
          }
        } catch (e) {}
      });
      try { if (RC.turnCard && _turnTid) RC.turnCard.addPart(_turnTid,
        { kind: 'text', text: '⏳ 正在后台生成卡片,完成会在这里显示预览…' }); } catch (e) {}
      return;
    }
    RC.toolChip.done(c, { summary: p.label || '完成', detail: p.rag || p.result_brief || '' });
    // 141(轮次容器):工具完成 → 把它作为一个 **tool part 注入本轮容器**(参数/子步骤/喂给 AI 的图/结果
    //   全在 part 里)。容器据此长出卡头 +【流程】按钮;part 落库 → 刷新后**照样是卡**(旧实现刷完就没了)。
    try {
      if (RC.turnCard && window.__asstVoiceTid) {
        var _tid = window.__asstVoiceTid();
        RC.turnCard.idle(_tid);
        RC.turnCard.addPart(_tid, { kind: 'tool', tool: p.tool || '', label: p.label || p.tool || '工具',
          args: p.args || {}, steps: p.sub_steps || [], vision: p.vision || [],
          result: String(p.rag || p.result_brief || '').slice(0, 6000), took_s: p.took_s, model: p.model });   // model 补上(流程详情窗显示;relay 没带则显 —)
      }
    } catch (e) {}
  }
  function _pickTaskId(rag) {   // 工具返回体里带 task_id(voice-tool 的 rag 是 JSON 字符串)
    try { var o = typeof rag === 'string' ? JSON.parse(rag) : rag; return (o && o.task_id) || ''; } catch (e) { return ''; }
  }
  function _chipTrackTask(c, tid, onFinal) { RC.toolChip.track(c, tid, onFinal); }   // 轮询后台任务;onFinal 让调用方拿最终结果(卡片预览)

  function onToolStatus(p) {
    p = p || {};
    try { if (p.status === 'running') _chipStart(p); else if (p.status !== 'aborted') _chipEnd(p); } catch (e) {}
    // chip 系统在 = 工具状态由 chip 的长条负责(用户设计:这套是"进行中"指示的高级替代)
    //   → 字幕框只留说话内容,不再挤工具状态行(超出的那部分内容归到标记里去)。
    var _hasChip = !!(window.RC && RC.toolChip);
    var b = document.getElementById('vc-tool-btn'); if (!b) return;
    var _ic = _toolIcon(p.tool || p.label);
    if (p.status === 'running') {
      b.style.display = 'flex'; b.className = 'running'; b.innerHTML = '<span class="vc-spin"></span>';
      b.title = '正在执行:' + (p.label || '工具') + '(点击中止)';
      if (!_hasChip) capStatus({ html: _ic + '<span>' + esc(p.label || '正在处理') + '…</span><span class="vc-spin vc-spin-s"></span>', cls: 'run' });   // 69:图标+转圈(chip 在=交给 chip 长条)
      // 103(用户实测:relay 重启时进行中的工具死亡=永远转圈):running 超时兜底——150s 没等到 done/error 自动标超时
      if (onToolStatus._t0) clearTimeout(onToolStatus._t0);
      onToolStatus._t0 = setTimeout(function () {
        var b2 = document.getElementById('vc-tool-btn');
        if (b2 && b2.className === 'running') onToolStatus({ status: 'error', tool: p.tool, label: (p.label || '工具') + '·超时(服务可能重启过,重问一次即可)' });
      }, 150000);
    } else {
      if (onToolStatus._t0) { clearTimeout(onToolStatus._t0); onToolStatus._t0 = null; }
      b.style.display = 'none'; b.className = ''; b.textContent = '';   // 完成/出错/中止 → 自动消失
      // 69:完成/失败在字幕停留一下再走(旧行为=立即清,侧栏关着的用户什么都看不见)
      if (_hasChip) { capStatus(null); }   // 完成/失败都由 chip 表达(方块/红长条)
      else if (p.status === 'done') capStatus({ html: _ic + '<span>' + esc(p.label || '完成') + '</span><span class="vc-tks ok">✓</span>', cls: 'ok', hold: 2500 });
      else if (p.status === 'error') capStatus({ html: _ic + '<span>' + esc(p.label || '工具') + '</span><span class="vc-tks err">⚠</span>', cls: 'err', hold: 4000 });
      else capStatus(null);
      if (p.status === 'aborted') {
        var th = document.getElementById('asst-thread');
        if (th) { var a = document.createElement('div'); a.className = 'vc-tcard err'; a.innerHTML = '<div class="vc-tc-h"><span class="vc-tc-st">⊘</span><span class="vc-tc-l">已中止</span></div>'; th.appendChild(a); th.scrollTop = th.scrollHeight; }
      } else if (!(window.RC && RC.toolChip)) {
        threadToolCard(p);   // 回退:没有 chip 组件时仍用旧详情卡
      }
    }
  }
  // 侧栏对话流里的工具调用详情卡(用户设计:像回答旁的感叹号那样,点开看每一步——
  // S2S 发的指令 JSON / 携带的页面上下文 / 参数 / 喂回豆包播报的真实结果)。折叠一行,点头部展开。
  function threadToolCard(p) {
    var th = document.getElementById('asst-thread'); if (!th) return;
    var ok = p.status === 'done';
    var d = document.createElement('div'); d.className = 'vc-tcard' + (ok ? '' : ' err');
    var rows = [];
    if (p.cmd) rows.push(['指令(S2S 原话)', p.cmd]);
    if (p.ctx_brief) {
      var cb = p.ctx_brief, cparts = ['第' + (cb.page || '?') + '页'];
      if (cb.ink) cparts.push('墨迹' + cb.ink + '笔');
      if (cb.sel) cparts.push('选中' + cb.sel + '字');
      rows.push(['携带上下文', cparts.join(' · ')]);
    }
    try { if (p.args && Object.keys(p.args).length) rows.push(['参数', JSON.stringify(p.args)]); } catch (e) {}
    if (p.rag) rows.push(['喂回给它播报的结果', p.rag]);
    else if (p.result_brief) rows.push(['结果', p.result_brief]);
    d.innerHTML = '<div class="vc-tc-h"><span class="vc-tc-st">' + (ok ? '✓' : '⚠') + '</span>' +
      '<span class="vc-tc-i">' + _toolIcon(p.tool || p.label) + '</span>' +
      '<span class="vc-tc-l">' + esc(p.label || p.tool || '工具') + '</span>' +
      (p.took_s != null ? '<span class="vc-tc-t">' + esc(p.took_s) + 's</span>' : '') +
      (p.cached ? '<span class="vc-tc-t">复用缓存</span>' : '') +
      '<span class="vc-tc-x">▸</span></div>' +
      '<div class="vc-tc-b" style="display:none">' +
      rows.map(function (r) { return '<div class="vc-tc-k">' + esc(r[0]) + '</div><div class="vc-tc-v">' + esc(r[1]) + '</div>'; }).join('') +
      '</div>';
    d.querySelector('.vc-tc-h').addEventListener('click', function () {
      var body = d.querySelector('.vc-tc-b'), open = body.style.display === 'none';
      body.style.display = open ? 'block' : 'none';
      d.querySelector('.vc-tc-x').textContent = open ? '▾' : '▸';
    });
    th.appendChild(d); th.scrollTop = th.scrollHeight;
  }
  // 字幕改**累积对话流**(iMessage 风,右蓝=你/左灰=AI):旧版只有"最后一句"两行,用户反馈看不到对话内容。
  // AI 一轮 = 一个气泡(550 增量更新同一元素;450 用户开口 = 上一轮定稿,curAEl 置空)。
  var curAEl = null;
  var _lastU = '';   // ㉛:最近一句用户话(451/whisper 定稿存,轮完随 AI 句一起落库)
  function setSub(who, text, iid) {
    // ㉛(用户设计):侧栏助手在 → 通话对话直接进 #asst-thread(与文字对话同流同清);浮层迷你对话区只兜底
    if (window.__asstVoiceMsg && window.__asstVoiceMsg(who, text, iid ? { utterId: iid } : undefined)) return;
    if (!box) return;
    var sub = box.querySelector('.vc-sub'); if (!sub) return;
    if (who === 'u') {
      if (iid && setSub._u && setSub._u.iid === iid && setSub._u.el && setSub._u.el.parentNode === sub) {
        setSub._u.el.textContent = text;   // 112:同 iid=覆盖同一气泡(可修订全文,不追加)
        return;
      }
      var d = document.createElement('div'); d.className = 'vc-m vc-mu'; d.textContent = text;
      if (iid) setSub._u = { iid: iid, el: d };
      // GPT 的用户转写(whisper)异步迟到:AI 回复常已在流——用户句按时序插到进行中气泡**前面**,
      // 不断开 curAEl(断开会让后续 delta 带全量文本另起新气泡=同一回复显示两遍)
      if (curAEl && curAEl.parentNode === sub) sub.insertBefore(d, curAEl);
      else { sub.appendChild(d); curAEl = null; }
    } else {
      if (!curAEl || !curAEl.parentNode) { curAEl = document.createElement('div'); curAEl.className = 'vc-m vc-ma'; sub.appendChild(curAEl); }
      curAEl.textContent = text;
    }
    while (sub.children.length > 80) sub.removeChild(sub.firstChild);
    sub.scrollTop = sub.scrollHeight;
  }

  // ── AEC 环回(㉘,官方公认 workaround):浏览器回声消除的参考信号**只取 WebRTC/<audio> 元素路径**,
  //    纯 WebAudio 播的声音不被 AEC 消(Chromium issue 40252911)——通话音频外放时全量进麦=回声自激根源。
  //    把播放经本地 RTCPeerConnection 环回成"远端流"再用 <audio> 播 → AEC 当远端参与者音频自动消;
  //    顺带走"通话音频"路径,iOS 麦克风活跃时的系统 ducking(音量忽大忽小,WebKit #218012)通常也更平稳。──
  var _aec = { dest: null, el: null, pc1: null, pc2: null, ready: false, ctx: null };
  // 130(用户实测:文字模式 + TTS 代读时,①我开口它不停 ②它念的时候模型听不见我)——
  //   根因:AEC 环回**只在 WS 引擎路径建**,WebRTC(GPT)路径没建 → TTS 代念走裸 WebAudio,
  //   不在回声消除的参考信号里 → 只能靠 _ttsMicGuard **禁麦**防模型听见自己 →
  //   麦一禁,VAD 收不到 speech_started:你打断不了它,它也听不见你。死结。
  //   解法:**给 TTS 的 AudioContext 也建一条环回** → 代念的声音进 AEC 参考被消掉 → 不必再禁麦。
  var _taec = { dest: null, el: null, pc1: null, pc2: null, ready: false, ctx: null };
  async function _aecSetup(acx) { return _aecMake(_aec, acx); }
  async function _ttsAecSetup(acx) { return _aecMake(_taec, acx); }
  async function _aecMake(slot, acx) {
    if (!acx) return;
    if (slot.ready && slot.ctx === acx) return;
    _aecKill(slot);
    try {
      var dest = acx.createMediaStreamDestination();
      var pc1 = new RTCPeerConnection(), pc2 = new RTCPeerConnection();
      pc1.onicecandidate = function (e) { if (e.candidate) { try { pc2.addIceCandidate(e.candidate); } catch (_) {} } };
      pc2.onicecandidate = function (e) { if (e.candidate) { try { pc1.addIceCandidate(e.candidate); } catch (_) {} } };
      pc2.ontrack = function (e) {
        var el = document.createElement('audio');
        el.autoplay = true; el.setAttribute('playsinline', ''); el.style.display = 'none';
        el.srcObject = e.streams[0];
        document.body.appendChild(el);
        slot.el = el;
        try { el.play(); } catch (_) {}
      };
      dest.stream.getTracks().forEach(function (t) { pc1.addTrack(t, dest.stream); });
      var offer = await pc1.createOffer();
      await pc1.setLocalDescription(offer);
      await pc2.setRemoteDescription(offer);
      var ans = await pc2.createAnswer();
      await pc2.setLocalDescription(ans);
      await pc1.setRemoteDescription(ans);
      slot.dest = dest; slot.pc1 = pc1; slot.pc2 = pc2; slot.ctx = acx; slot.ready = true;
    } catch (e) { _aecKill(slot); }   // 失败回落直连(现状),不阻塞通话
  }
  function _aecKill(slot) {
    try { if (slot.pc1) slot.pc1.close(); } catch (e) {}
    try { if (slot.pc2) slot.pc2.close(); } catch (e) {}
    try { if (slot.el) slot.el.remove(); } catch (e) {}
    slot.dest = null; slot.el = null; slot.pc1 = null; slot.pc2 = null; slot.ready = false; slot.ctx = null;
  }
  function _aecTeardown() { _aecKill(_aec); }

  // ── 播放(PCM24k)+ 打断 ──
  var _playStat = { t0: 0, queued: 0 };   // 本轮回答播放统计:打断时回报**真实已播毫秒**给 relay 做 truncate
  function _reportPlayed() {   // (官方语义:audio_end_ms=用户实际听到的毫秒;旧版按已转发字节高估≈不截,上下文残留没听到的内容)
    try {
      if (ws && ws.readyState === 1 && _playStat.queued > 0 && ac) {
        var played = Math.max(0, Math.min(ac.currentTime - _playStat.t0, _playStat.queued));
        ws.send(JSON.stringify({ type: 'played_ms', ms: Math.round(played * 1000) }));
      }
    } catch (e) {}
    _playStat = { t0: 0, queued: 0 };
  }
  function playPcm(buf) {
    if (!ac) return;
    if (mode === 's2s' && !s2sSpeakOn()) return;   // S2S+朗读灭:丢音频,回复看对话窗字幕(550 增量驱动)
    var i16 = new Int16Array(buf), f32 = new Float32Array(i16.length);
    for (var i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;
    var ab = ac.createBuffer(1, f32.length, 24000);
    ab.copyToChannel(f32, 0);
    var src = ac.createBufferSource(); src.buffer = ab;
    src.connect((_aec.ready && _aec.ctx === ac) ? _aec.dest : ac.destination);   // 环回在 → AEC 生效路径;不在 → 直连兜底
    if (mode === 's2s') { var _tp = _wsRecTap(); if (_tp) { try { src.connect(_tp); } catch (e) {} } }   // 66b:同块喂录音 tap(按轮存原声)
    var t = Math.max(ac.currentTime + 0.02, playT);
    src.start(t); playT = t + ab.duration; playing.push(src);
    if (_playStat.queued === 0) _playStat.t0 = t;   // 本轮回答首块的开播时刻
    _playStat.queued += ab.duration;
    _capBindChunk(t, ac);   // agent 通话朗读:句首块 → 字幕在开播时刻亮(S2S 无 seg 帧,零影响)
    callBtnSpeaking(true);
    src.onended = function () { var k = playing.indexOf(src); if (k >= 0) playing.splice(k, 1); if (!playing.length) { callBtnSpeaking(false); try { _lastPlayEnd = ac ? ac.currentTime : 0; } catch (e) {} } };
  }
  function stopPlayback() { playing.forEach(function (s) { try { s.stop(); } catch (e) {} }); playing = []; playT = 0; callBtnSpeaking(false); }

  // ── 66b WS 引擎按轮语音录制(用户设计:AI 说过的话点 ▶ 回放当时原声)——豆包/Grok/GPT-WS 的音频
  //    走 WebAudio 播放(没有 WebRTC remote 轨可录),录法=playPcm 的 source 多接一个
  //    MediaStreamDestination 分支(__vcTtsCapture 同模式);轮界:首块懒开录 / 359 播完收(等队列真放完:
  //    playT 快照,97 同款坑「数据推完≠播完」)/ 450 打断即收 ──
  var _wsRec = { mr: null, chunks: [], mime: '', tap: null, ctx: null, pend: null };
  function _wsRecTap() {   // playPcm 每块调:确保 tap+录音器活着(ac 重建后 tap 随之重建);返回 tap|null
    if (!window.MediaRecorder || !ac) return null;
    if (!_wsRec.tap || _wsRec.ctx !== ac) {
      try { _wsRec.tap = ac.createMediaStreamDestination(); _wsRec.ctx = ac; } catch (e) { return null; }
    }
    if (!_wsRec.mr) {
      var mime = _recMime(); if (!mime) return _wsRec.tap;
      try {
        var mr = new MediaRecorder(_wsRec.tap.stream, { mimeType: mime });
        _wsRec.mr = mr; _wsRec.chunks = []; _wsRec.mime = mime;
        mr.ondataavailable = function (e) { if (e.data && e.data.size) _wsRec.chunks.push(e.data); };
        mr.start(1000);
      } catch (e) { _wsRec.mr = null; }
    }
    return _wsRec.tap;
  }
  function _wsRecCutPend() { var f = _wsRec.pend; _wsRec.pend = null; if (f) f(); }   // 450:上一轮还在等队列放完 → 立即截住(别把新轮录进尾巴)
  function _wsRecAbort() { _wsRec.pend = null; try { if (_wsRec.mr && _wsRec.mr.state !== 'inactive') _wsRec.mr.stop(); } catch (e) {} _wsRec.mr = null; }   // 丢弃(不上传:onstop 未设 id 引用)
  function _wsRecFinish(immediate) {   // 返回 clipId(录着才有);blob 异步上传,落库先带 id(镜像 _recFinish)
    var mr = _wsRec.mr;
    if (!mr || mr.state === 'inactive') { _wsRec.mr = null; return ''; }
    var id = 'c' + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
    var mime = _wsRec.mime, chunks = _wsRec.chunks;
    mr.onstop = function () {
      try {
        var blob = new Blob(chunks, { type: mime });
        if (blob.size > 4000) fetch('/api/assistant/voice-clip?id=' + id, { method: 'POST', headers: { 'Content-Type': mime }, body: blob }).catch(function () {});
      } catch (e) {}
    };
    function stopNow() { _wsRec.pend = null; try { if (mr.state !== 'inactive') mr.stop(); } catch (e) {} }
    if (immediate) stopNow();   // 打断:音频已停,立即收(录到打断点)
    else {   // 359=服务端推完,队列常还有几秒没放:等本轮队列末端时刻(playT 快照)过去再停
      var tEnd = playT;
      _wsRec.pend = stopNow;
      var iv = setInterval(function () {
        if (_wsRec.pend !== stopNow) { clearInterval(iv); return; }   // 已被 450 截停
        if (!ac || ac.currentTime >= tEnd - 0.05) { clearInterval(iv); setTimeout(stopNow, 200); }
      }, 300);
      setTimeout(stopNow, 120000);   // 兜底:别让录音器永远吊着
    }
    _wsRec.mr = null;   // 状态机腾位:下一轮首块可开新录音器(本轮收尾由闭包完成)
    return id;
  }

  // ── 采集(mic → 16k PCM 20ms/包)──
  var WORKLET = 'class C extends AudioWorkletProcessor{process(i){var c=i[0][0];if(c)this.port.postMessage(c.slice(0));return true}}registerProcessor("vccap",C);';
  var _upRate = 16000;   // 上行采样率:豆包=16k;GPT Realtime 只吃 24k(relay 的 up_rate 事件动态切)
  var _halfDuplex = false, _lastPlayEnd = 0;   // 半双工(㉙,GPT 外放默认):AI 播放期整段静麦+播完 350ms 残响缓冲
  var _hpIn = false;   // 119:耳机在线(通话建立时检测+devicechange 实时更新)——耳机/桥场景免半双工=可随时打断
  try {
    navigator.mediaDevices.addEventListener('devicechange', function () {
      _headphonesIn().then(function (hp) { _hpIn = hp; });
    });
  } catch (e) {}
  // ㉘f:㉕b 的回声能量门已撤——PCM 流无时间戳,丢包≠插入静默而是**把话剪辑拼接**(句中插话根因)。
  // 半双工与门的本质区别:全有或全无=干净静默,不破坏 VAD/转写的输入完整性;这是外放场景的成熟可靠解
  // (AEC 环回在 Safari/iPad 不保证生效,实测回声仍被转写成用户输入触发自问自答)。
  // ── 99(用户设计):WebRTC 回声桥——外放时浏览器媒体走 WebRTC 到 Pi(aiortc),<audio> 播放
  //    进浏览器 AEC 参考=回声消除生效(同 #280 原理);戴耳机自动退出桥(直连 ws,省一跳)。
  //    纯音频面:事件/字幕/控制照旧走 ws。桥建立失败/中断=双侧自动回退 ws 音频(relay 侧同样回退)。
  var _abridge = { on: false, pc: null, el: null };
  function _headphonesIn() {   // 耳机检测:授权后 label 可见;iOS 不列 audiooutput,靠 input label 兜
    return navigator.mediaDevices.enumerateDevices().then(function (ds) {
      return ds.some(function (d) {
        return (d.kind === 'audioinput' || d.kind === 'audiooutput') &&
          /airpod|headphone|headset|earbud|earpiece|耳机|イヤホン|ヘッドホン/i.test(d.label || '');
      });
    }).catch(function () { return false; });
  }
  function _bridgePref() { try { return localStorage.getItem('rc-voice-bridge') || 'auto'; } catch (e) { return 'auto'; } }
  function _abridgeStop() {
    _abridge.on = false;
    try { if (_abridge.pc) _abridge.pc.close(); } catch (e) {}
    try { if (_abridge.el) _abridge.el.remove(); } catch (e) {}
    _abridge.pc = null; _abridge.el = null;
  }
  async function _abridgeStart(mic) {   // 桥建立:offer 经 ws 信令;connected 才接管麦上行
    try {
      var pc = new RTCPeerConnection();
      _abridge.pc = pc;
      pc.addTrack(mic.getAudioTracks()[0], mic);
      pc.ontrack = function (e) {
        var el = document.createElement('audio');
        el.autoplay = true; el.setAttribute('playsinline', ''); el.style.display = 'none';
        el.srcObject = e.streams[0];
        document.body.appendChild(el);
        _abridge.el = el;
        try { el.play(); } catch (e2) {}
      };
      pc.onconnectionstatechange = function () {
        var st = pc.connectionState;
        if (st === 'connected') { _abridge.on = true; setSt('通话中(回声桥·外放可用)'); }
        else if (st === 'failed' || st === 'closed' || st === 'disconnected') {
          if (_abridge.pc === pc && _abridge.on) { _abridgeStop(); setSt('通话中(桥断,已回退)'); }
        }
      };
      var offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      await new Promise(function (r) {   // non-trickle:等 ICE 收集完(Tailscale 内 host candidates,快)
        if (pc.iceGatheringState === 'complete') { r(); return; }
        var t0 = setTimeout(r, 2000);
        pc.onicegatheringstatechange = function () { if (pc.iceGatheringState === 'complete') { clearTimeout(t0); r(); } };
      });
      if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'bridge_offer', sdp: pc.localDescription.sdp }));
      setTimeout(function () {   // 6s 没 connected=放弃(relay 侧 pc 断态自回退)
        if (_abridge.pc === pc && !_abridge.on) { console.warn('[vc] 回声桥超时,回退 ws 音频'); _abridgeStop(); }
      }, 6000);
    } catch (e) { console.warn('[vc] 回声桥建立失败', e); _abridgeStop(); }
  }
  function onCap(chunk, rate) {
    if (_abridge.on) return;   // 99:桥接管麦上行(WebRTC 轨),ws 不再发音频
    if (_halfDuplex && !_hpIn && !_abridge.on && (playing.length || (ac && ac.currentTime - _lastPlayEnd < 0.35))) { f32buf = new Float32Array(0); return; }   // 119:耳机/桥=全双工可打断;仅外放无桥才静麦兜底
    var m = new Float32Array(f32buf.length + chunk.length);
    m.set(f32buf); m.set(chunk, f32buf.length); f32buf = m;
    var need = Math.round(rate * 0.02), outN = Math.round(_upRate * 0.02);
    while (f32buf.length >= need) {
      var seg = f32buf.subarray(0, need); f32buf = f32buf.slice(need);
      var out = new Int16Array(outN);
      for (var i = 0; i < outN; i++) {
        var v = seg[Math.min(need - 1, Math.floor(i * need / outN))];
        out[i] = Math.max(-32768, Math.min(32767, v * 32768));
      }
      if (ws && ws.readyState === 1) ws.send(out.buffer);
    }
  }

  // ── client_action 派发:relay 意图旁路的页面控制指令在**阅读器环境**执行 ──
  function dispatch(fn, args) {
    var result;
    if (fn === 'renderVideos') result = renderVids((args || [[]])[0] || [], (args || [])[1]);
    else if (fn === 'renderImages') result = renderImgs((args || [[]])[0] || []);
    else if (fn === 'renderInfoCard') result = renderInfo((args || [{}])[0] || {});
    else {
      try { if (typeof window[fn] === 'function') return window[fn].apply(null, args || []); } catch (e) {}
      return null;
    }
    // relay 路径没有 ACK 消费者，但异步落库仍必须有拒绝处理，不能制造
    // WebKit 的 unhandledrejection。Direct 路径会直接 await renderInfo 的原 Promise。
    if (result && typeof result.catch === 'function') result.catch(function () {});
    return result;
  }
  window.__vcDispatch = dispatch;   // 测试/共享层可直接派发结果卡(与 relay client_action 同一条路)

  // Windows Reader 输出与 Realtime 使用同一批渲染/动作入口，但不接受任意函数名。
  // 外部只交语义化 kind；这里是唯一白名单，避免把旧 dispatch 的 window[fn]
  // 兼容分支扩大成远程脚本入口。
  var _readerOutputSeen = Object.create(null);
  var _readerOutputPending = Object.create(null);
  // page-chars 卡只有真正写进 document-notes 才算完成。第一次写入失败时已经
  // 给用户显示过一次回退卡；后续 durable replay 只重试 placement，不能再往
  // 对话流/浮层各复制一张。
  var _readerOutputAwaitingBind = Object.create(null);
  var _readerOutputOrder = [];
  function _rememberReaderOutput(id, receipt) {
    _readerOutputSeen[id] = {
      bindOutcome: receipt && receipt.bindOutcome || null,
      bindReason: receipt && receipt.bindReason || null
    };
    _readerOutputOrder.push(id);
    while (_readerOutputOrder.length > 256) {
      delete _readerOutputSeen[_readerOutputOrder.shift()];
    }
  }
  function _readerOutputNeedsBound(delivery) {
    var card = delivery && delivery.kind === 'card' && delivery.payload &&
      delivery.payload.card;
    return !!(card && card.bind && card.bind.kind === 'page-chars');
  }
  function _readerOutputReject(error) {
    return {
      outcome: 'rejected',
      error: String((error && (error.code || error.message)) || error ||
        'BW_READER_REALTIME_OUTPUT_FAILED').slice(0, 500)
    };
  }
  function _readerDraftGid(draftId) {
    draftId = String(draftId || '');
    if (!/^draft-[a-f0-9]{32}$/.test(draftId)) {
      return Promise.reject(new Error('BW_READER_ANKI_DRAFT_ID_INVALID'));
    }
    if (!window.crypto || !window.crypto.subtle ||
        typeof window.crypto.subtle.digest !== 'function' ||
        typeof TextEncoder !== 'function') {
      return Promise.reject(new Error('BW_READER_ANKI_DRAFT_HASH_UNAVAILABLE'));
    }
    return window.crypto.subtle.digest(
      'SHA-256', new TextEncoder().encode(draftId)
    ).then(function (buffer) {
      var hex = Array.prototype.map.call(new Uint8Array(buffer), function (byte) {
        return byte.toString(16).padStart(2, '0');
      }).join('');
      return 'card_' + hex.slice(0, 12);
    });
  }
  function _readerDraftSource(delivery, payload, draftId) {
    payload = payload || {};
    var exact = _readerDraftSourceMode(payload) === 'exact';
    var file = String(payload.file || delivery && delivery.file || '');
    var source = {
      kind: exact ? 'readerpc-verified-draft' : 'readerpc-generated-draft',
      sourceId: exact ? ('reader-book:' + file) : ('reader-draft:' + draftId),
      tool: 'reader_anki_draft',
      draftId: draftId,
      sourceInstanceId: String(delivery && delivery.sourceInstanceId || '')
    };
    if (exact) {
      source.documentId = file;
      source.quote = String(payload.sourceText || '');
      source.location = payload.target;
    }
    return source;
  }
  function _readerDraftSourceMode(payload) {
    payload = payload || {};
    var present = [
      typeof payload.file === 'string' && !!payload.file,
      !!payload.target && typeof payload.target === 'object',
      typeof payload.sourceText === 'string' && !!payload.sourceText
    ];
    var count = present.filter(Boolean).length;
    if (count === 0) return 'generic';
    if (count === present.length) return 'exact';
    throw new Error('BW_READER_ANKI_DRAFT_SOURCE_PARTIAL');
  }
  function _readerOutputScroller() {
    return document.getElementById('main') ||
      document.getElementById('content') ||
      document.scrollingElement || document.documentElement;
  }
  function _readerOutputNavigate(delivery) {
    var p = delivery.payload || {}, action = p.action;
    var browser = window.__bwBrowserControl;
    if (browser && typeof browser.execute === 'function' &&
        action !== 'go-to-page' && action !== 'go-to-section') {
      var req = {
        contract: 'bw-browser-control/1',
        type: 'request',
        requestId: delivery.correlation,
        sourceInstanceId: delivery.sourceInstanceId,
        action: action
      };
      if (p.target !== null) req.target = p.target;
      if (p.selectionId !== null) req.selectionId = p.selectionId;
      var response = browser.execute(req);
      if (!response || response.ok !== true) {
        throw new Error(response && response.error &&
          (response.error.code || response.error.message) ||
          'BW_READER_NAVIGATION_FAILED');
      }
      return response.state || true;
    }
    if (action === 'next-viewport' || action === 'previous-viewport') {
      var scroller = _readerOutputScroller();
      var height = scroller.clientHeight || window.innerHeight || 600;
      var delta = Math.max(120, Math.round(height * 0.82)) *
        (action === 'next-viewport' ? 1 : -1);
      if (typeof scroller.scrollBy === 'function') {
        scroller.scrollBy({ top: delta, left: 0, behavior: 'smooth' });
      } else scroller.scrollTop += delta;
      return true;
    }
    if (action === 'go-to-page' || action === 'go-to-section') {
      var host = RC.documentHost && RC.documentHost.current &&
        RC.documentHost.current();
      if (!host || typeof host.navigate !== 'function') {
        throw new Error('BW_READER_NAVIGATION_UNAVAILABLE');
      }
      var data = action === 'go-to-page'
        ? { page: p.target, index: p.target }
        : { section: p.target, index: p.target };
      return host.navigate({ data: data }, { source: 'reader-realtime-output' });
    }
    throw new Error('BW_READER_NAVIGATION_UNAVAILABLE');
  }
  function _applyReaderRealtimeOutput(delivery) {
    try {
      var p = delivery.payload || {}, work;
      var _bindOnlyReplay = !!_readerOutputAwaitingBind[delivery.correlation];
      if (delivery.kind === 'assistant-turn') {
        if (typeof window.__asstVoiceMsg !== 'function' ||
            typeof window.__asstVoiceLog !== 'function') {
          throw new Error('BW_READER_CONVERSATION_RECEIVER_UNAVAILABLE');
        }
        var _resetRendered = window.__asstVoiceMsg('reset');
        var _userRendered = window.__asstVoiceMsg('u', p.user);
        var _assistantRendered = window.__asstVoiceMsg(
          'a', p.assistant, { md: true }
        );
        if (!_resetRendered || !_userRendered || !_assistantRendered) {
          throw new Error('BW_READER_CONVERSATION_RENDER_FAILED');
        }
        window.__asstVoiceLog(
          p.user,
          p.assistant,
          delivery.file,
          delivery.page,
          { external_thread_id: p.threadId || null, via: 'windows-reader-output' }
        );
        work = true;
      } else if (delivery.kind === 'tool-status') {
        onToolStatus({
          status: p.status,
          tool: p.tool,
          label: p.label,
          result_brief: p.detail || ''
        });
        work = true;
      } else if (delivery.kind === 'card') {
        // 带 bind 的卡：回执必须说清**钉上了没有**。
        // 必须等待本地 placement 真正提交；Promise 还在 pending 时不能抢跑回执。
        //
        // 只用两个字段：bindOutcome（枚举）+ bindReason（没钉上时的原因）。
        // 沿途要过 11 道闸/重建点，字段每多一个就多 11 处 —— 而 kind/detail
        // 对助手的下一步决策没有影响，它需要知道的是「钉上了没有、为什么」。
        work = Promise.resolve(renderInfo(p.card, {
          uid: String(delivery.correlation || ''),
          bindOnly: _bindOnlyReplay
        })).then(function (result) {
          if (!result || result.rendered !== true) {
            throw new Error('BW_READER_CARD_RENDER_FAILED');
          }
          return {
            bindOutcome: result.bindOutcome || 'none',
            bindReason: result.bindReason || null
          };
        });
      } else if (delivery.kind === 'navigate') {
        work = _readerOutputNavigate(delivery);
      } else if (delivery.kind === 'highlight') {
        if (!(RC.actions && RC.actions.has && RC.actions.has('highlight.save'))) {
          throw new Error('BW_READER_HIGHLIGHT_UNAVAILABLE');
        }
        work = RC.actions.run('highlight.save', {
          color: p.color,
          note: p.note || ''
        });
      } else if (delivery.kind === 'highlight-text') {
        if (typeof window.__bwReaderHighlightExactText !== 'function') {
          throw new Error('BW_READER_HIGHLIGHT_TEXT_UNAVAILABLE');
        }
        work = window.__bwReaderHighlightExactText(p);
      } else if (delivery.kind === 'highlight-range') {
        if (typeof window.__bwReaderHighlightRange !== 'function') {
          throw new Error('BW_READER_HIGHLIGHT_RANGE_UNAVAILABLE');
        }
        work = window.__bwReaderHighlightRange(p);
      } else if (delivery.kind === 'anki-draft') {
        if (!(RC.flashcard && typeof RC.flashcard.presentDraft === 'function')) {
          throw new Error('BW_READER_ANKI_DRAFT_UNAVAILABLE');
        }
        var _draftSourceMode = _readerDraftSourceMode(p);
        if (_draftSourceMode === 'exact' &&
            typeof window.__bwReaderValidateExactSource !== 'function') {
          throw new Error('BW_READER_ANKI_DRAFT_SOURCE_VALIDATOR_UNAVAILABLE');
        }
        work = (_draftSourceMode === 'exact'
          ? Promise.resolve(window.__bwReaderValidateExactSource(p))
          : Promise.resolve({ ok: true, generic: true }))
          .then(function () {
            return _readerDraftGid(p.draftId);
          })
          .then(function (gid) {
            var draftId = String(p.draftId || '');
            // 同一份身份构造一次、两个宿主共用：分别构造就可能悄悄产生差异，
            // 而差异在仓库里表现为两个实体。
            var draftSource = _readerDraftSource(delivery, p, draftId);
            var draftLocal = {
              draftId: draftId,
              sourceInstanceId: delivery.sourceInstanceId
            };
            return Promise.resolve(RC.flashcard.presentDraft(
              p.cards,
              gid,
              {
                entityRegistered: false,
                repositorySource: draftSource,
                localDraft: draftLocal
              }
            )).then(function (rendered) {
              if (!rendered) throw new Error('BW_READER_ANKI_DRAFT_RENDER_FAILED');
              // 本地仓已落稳，再把同一 gid 暴露到对话流。
              _mirrorDraftIntoTurnFlow(p.cards, gid, draftSource, draftLocal);
              return {
                status: 'draft_delivered',
                anki_written: false,
                gid: gid,
                repository: 'local'
              };
            });
          });
      } else if (delivery.kind === 'client-action') {
        // normalizer 可能被 facade 绕过，所以执行侧不能再动态 window[fn]。
        // 这里只调用 native-local-runtime 的单一语义入口；由它依据受信
        // nativeInterfaceSurface 分流 PDF/EPUB，并再次校验一次性编号。
        var _caFn = String(p.fn || '');
        var _caArgs = Array.isArray(p.args) ? p.args : [];
        // 显式映射，仍不动态分派：每个入口自带参数校验，且只认这张表里的名字。
        // 表按名字取函数，而不是按消息里的字符串去 window 上找 —— 后者会把
        // 一条跨进程消息升级成"调用页面任意函数"。
        var _caTarget = null;
        var _caCall = null;
        if (_caFn === '_nativeReaderUndoLast') {
          var _caId = _caArgs.length === 1 ? String(_caArgs[0] || '') : '';
          if (!/^rundo_[0-9a-f]{24}$/.test(_caId)) {
            throw new Error('BW_READER_CLIENT_ACTION_INVALID:' + _caFn);
          }
          _caTarget = window._nativeReaderUndoLast;
          _caCall = function (target) { return target.call(window, _caId); };
        } else if (_caFn === '_nativeReaderPageCardMutate') {
          var _caCardArg = _caArgs.length === 1 && _caArgs[0] &&
            typeof _caArgs[0] === 'object' && !Array.isArray(_caArgs[0])
            ? _caArgs[0] : null;
          var _caCardOp = _caCardArg ? String(_caCardArg.operation || '') : '';
          var _caCardOperationId = _caCardArg
            ? String(_caCardArg.operationId || '') : '';
          var _caCardExpectedId = _caCardArg
            ? String(_caCardArg.expectedId || '') : '';
          var _caCardReplacement = _caCardArg && _caCardArg.replacement;
          var _caCardHasNumber = !!(_caCardArg &&
            Object.prototype.hasOwnProperty.call(_caCardArg, 'number'));
          var _caAllowedKeys = _caCardOp === 'edit'
            ? ['operation', 'operationId', 'expectedId', 'expectedRevision', 'replacement']
            : ['operation', 'operationId', 'expectedId', 'expectedRevision'];
          if (_caCardHasNumber) _caAllowedKeys.push('number');
          if (!_caCardArg || (_caCardOp !== 'edit' && _caCardOp !== 'delete') ||
              Object.keys(_caCardArg).some(function (key) {
                return _caAllowedKeys.indexOf(key) < 0;
              }) || Object.keys(_caCardArg).length !== _caAllowedKeys.length ||
              !/^pcard_[0-9a-f]{24}$/.test(_caCardOperationId) ||
              (_caCardHasNumber &&
                (!Number.isSafeInteger(_caCardArg.number) || _caCardArg.number < 1)) ||
              !/^[A-Za-z0-9_-]{2,96}$/.test(_caCardExpectedId) ||
              !Number.isSafeInteger(_caCardArg.expectedRevision) ||
              _caCardArg.expectedRevision < 0) {
            throw new Error('BW_READER_CLIENT_ACTION_INVALID:' + _caFn);
          }
          if (_caCardOp === 'delete') {
            if (_caCardReplacement != null) {
              throw new Error('BW_READER_CLIENT_ACTION_INVALID:' + _caFn);
            }
          } else {
            var _caHasContent = _caCardReplacement &&
              Object.prototype.hasOwnProperty.call(_caCardReplacement, 'content');
            var _caHasCards = _caCardReplacement &&
              Object.prototype.hasOwnProperty.call(_caCardReplacement, 'cards');
            if (!_caCardReplacement || typeof _caCardReplacement !== 'object' ||
                Array.isArray(_caCardReplacement) || _caHasContent === _caHasCards ||
                Object.keys(_caCardReplacement).some(function (key) {
                  return key !== 'content' && key !== 'cards';
                })) {
              throw new Error('BW_READER_CLIENT_ACTION_INVALID:' + _caFn);
            }
            if (_caHasContent) {
              if (typeof _caCardReplacement.content !== 'string' ||
                  !_caCardReplacement.content.trim() ||
                  _caCardReplacement.content.length > PAGE_CARD_CONTENT_LIMIT) {
                throw new Error('BW_READER_CLIENT_ACTION_INVALID:' + _caFn);
              }
            } else {
              if (!Array.isArray(_caCardReplacement.cards) ||
                  !_caCardReplacement.cards.length ||
                  _caCardReplacement.cards.length > 12 ||
                  !_caCardReplacement.cards.every(function (card) {
                    if (!card || typeof card !== 'object' || Array.isArray(card)) {
                      return false;
                    }
                    var keys = Object.keys(card).sort().join(',');
                    if (card.type === 'basic' && keys === 'back,front,type') {
                      return typeof card.front === 'string' && !!card.front.trim() &&
                        card.front.length <= PAGE_CARD_CONTENT_LIMIT &&
                        typeof card.back === 'string' && !!card.back.trim() &&
                        card.back.length <= PAGE_CARD_CONTENT_LIMIT;
                    }
                    return card.type === 'cloze' && keys === 'cloze,type' &&
                      typeof card.cloze === 'string' &&
                      card.cloze.length <= PAGE_CARD_CONTENT_LIMIT &&
                      /\{\{c[1-9][0-9]*::[\s\S]+?\}\}/.test(card.cloze);
                  })) {
                throw new Error('BW_READER_CLIENT_ACTION_INVALID:' + _caFn);
              }
            }
          }
          _caTarget = window._nativeReaderPageCardMutate;
          _caCall = function (target) {
            var input = {
              operation: _caCardOp,
              operationId: _caCardOperationId,
              expectedId: _caCardExpectedId,
              expectedRevision: _caCardArg.expectedRevision,
              replacement: _caCardOp === 'edit' ? _caCardReplacement : undefined
            };
            if (_caCardOp !== 'edit') delete input.replacement;
            if (_caCardHasNumber) input.number = _caCardArg.number;
            return target.call(window, input);
          };
        } else if (_caFn === '_nativeReaderLearningCardMutate') {
          var _caLearning = _caArgs.length === 1 && _caArgs[0] &&
            typeof _caArgs[0] === 'object' && !Array.isArray(_caArgs[0])
            ? _caArgs[0] : null;
          var _caLearningOperation = _caLearning
            ? String(_caLearning.operation || '') : '';
          var _caLearningHasCard = _caLearningOperation === 'edit' &&
            Object.prototype.hasOwnProperty.call(_caLearning, 'card');
          var _caLearningHasSource = _caLearningOperation === 'edit' &&
            Object.prototype.hasOwnProperty.call(_caLearning, 'source');
          var _caLearningKeys = _caLearningOperation === 'edit'
            ? ['operation', 'mutationId', 'id', 'cardIndex',
              'expectedEntityRev', 'externalPolicy']
            : ['operation', 'mutationId', 'id', 'cardIndex',
              'expectedStateRev', 'externalPolicy'];
          if (_caLearningHasCard) _caLearningKeys.push('card');
          if (_caLearningHasSource) _caLearningKeys.push('source');
          function _caLearningBytes(input) {
            var serialized;
            try { serialized = typeof input === 'string'
              ? input : JSON.stringify(input); }
            catch (_) { return Infinity; }
            return typeof TextEncoder === 'function'
              ? new TextEncoder().encode(serialized).byteLength
              : unescape(encodeURIComponent(serialized)).length;
          }
          function _caLearningJsonValid(input, seen, depth) {
            if (input === null || typeof input === 'boolean') return true;
            if (typeof input === 'string') return input.indexOf('\u0000') < 0;
            if (typeof input === 'number') return Number.isFinite(input);
            if (typeof input !== 'object' || depth > 64 ||
                (!Array.isArray(input) &&
                  Object.prototype.toString.call(input) !== '[object Object]') ||
                seen.indexOf(input) >= 0) return false;
            seen.push(input);
            var valid = true;
            if (Array.isArray(input)) {
              for (var index = 0; index < input.length; index += 1) {
                if (!Object.prototype.hasOwnProperty.call(input, index) ||
                    !_caLearningJsonValid(input[index], seen, depth + 1)) {
                  valid = false;
                  break;
                }
              }
            } else {
              valid = Object.keys(input).every(function (key) {
                return key.indexOf('\u0000') < 0 &&
                  _caLearningJsonValid(input[key], seen, depth + 1);
              });
            }
            seen.pop();
            return valid;
          }
          function _caLearningSourceValid(source) {
            if (!source || typeof source !== 'object' || Array.isArray(source)) {
              return false;
            }
            var textLimits = {
              kind: 80, sourceId: 4096, documentId: 4096, bookId: 4096,
              url: 8192, title: 1024, quote: 32768, context: 65536,
              tool: 160, draftId: 512, sourceInstanceId: 512,
              requirement: 32768
            };
            var objectKeys = ['location', 'anchor', 'selection', 'legacy'];
            var allowed = Object.keys(textLimits).concat(objectKeys);
            if (Object.keys(source).some(function (key) {
              return allowed.indexOf(key) < 0;
            }) || typeof source.kind !== 'string' || !source.kind.trim()) {
              return false;
            }
            if (Object.keys(textLimits).some(function (key) {
              return Object.prototype.hasOwnProperty.call(source, key) &&
                (typeof source[key] !== 'string' ||
                  source[key].indexOf('\u0000') >= 0 ||
                  _caLearningBytes(source[key]) > textLimits[key]);
            })) return false;
            if (objectKeys.some(function (key) {
              var nested = source[key];
              return Object.prototype.hasOwnProperty.call(source, key) &&
                (!nested || typeof nested !== 'object' ||
                  Array.isArray(nested) ||
                  !_caLearningJsonValid(nested, [], 0));
            })) return false;
            if (!['sourceId', 'documentId', 'bookId', 'url', 'draftId',
              'sourceInstanceId'].some(function (key) {
                return typeof source[key] === 'string' && !!source[key].trim();
              })) return false;
            return _caLearningBytes(source) <= 128 * 1024;
          }
          if (!_caLearning ||
              (_caLearningOperation !== 'edit' &&
                _caLearningOperation !== 'delete') ||
              Object.keys(_caLearning).length !== _caLearningKeys.length ||
              Object.keys(_caLearning).some(function (key) {
                return _caLearningKeys.indexOf(key) < 0;
              }) ||
              !/^lcard_[0-9a-f]{24}$/.test(
                String(_caLearning.mutationId || '')
              ) ||
              !/^card_[0-9a-f]{4,64}$/.test(
                String(_caLearning.id || '')
              ) ||
              !Number.isSafeInteger(_caLearning.cardIndex) ||
              _caLearning.cardIndex < 0 || _caLearning.cardIndex > 255 ||
              ['reader-only', 'sync-if-projected']
                .indexOf(_caLearning.externalPolicy) < 0 ||
              (_caLearningOperation === 'edit'
                ? (!Number.isSafeInteger(_caLearning.expectedEntityRev) ||
                  _caLearning.expectedEntityRev < 0 ||
                  (!_caLearningHasCard && !_caLearningHasSource) ||
                  (_caLearningHasCard &&
                    (!_caLearning.card ||
                      typeof _caLearning.card !== 'object' ||
                      Array.isArray(_caLearning.card) ||
                      _caLearningBytes(_caLearning.card) > 200000)) ||
                  (_caLearningHasSource &&
                    !_caLearningSourceValid(_caLearning.source)))
                : (!Number.isSafeInteger(_caLearning.expectedStateRev) ||
                  _caLearning.expectedStateRev < 0))) {
            throw new Error('BW_READER_CLIENT_ACTION_INVALID:' + _caFn);
          }
          _caTarget = window._nativeReaderLearningCardMutate;
          _caCall = function (target) {
            return target.call(window, JSON.parse(JSON.stringify(_caLearning)));
          };
        } else if (_caFn === '_nativeReaderCreateNote') {
          var _caNote = _caArgs.length === 1 && _caArgs[0] &&
            typeof _caArgs[0] === 'object' && !Array.isArray(_caArgs[0])
            ? _caArgs[0] : null;
          var _caText = _caNote ? String(_caNote.text == null ? '' : _caNote.text) : '';
          if (!_caNote || !_caText.trim() || _caText.length > 4000) {
            throw new Error('BW_READER_CLIENT_ACTION_INVALID:' + _caFn);
          }
          _caTarget = window._nativeReaderCreateNote;
          _caCall = function (target) {
            return target.call(window, { text: _caText });
          };
        } else if (_caFn === '_nativeReaderEditNote') {
          var _caEditArg = Array.isArray(p.args) ? p.args[0] : null;
          var _caEditId = _caEditArg && typeof _caEditArg === 'object'
            ? String(_caEditArg.id == null ? '' : _caEditArg.id) : '';
          var _caEditText = _caEditArg && typeof _caEditArg === 'object'
            ? String(_caEditArg.text == null ? '' : _caEditArg.text) : '';
          if (!/^[A-Za-z0-9_-]{1,64}$/.test(_caEditId) ||
              !_caEditText.trim() || _caEditText.length > 4000) {
            throw new Error('BW_READER_CLIENT_ACTION_INVALID:' + _caFn);
          }
          _caTarget = window._nativeReaderEditNote;
          _caCall = function (target) {
            return target.call(window, { id: _caEditId, text: _caEditText });
          };
        } else if (_caFn === '_nativeReaderMakeNote') {
          var _caMakeArg = Array.isArray(p.args) ? p.args[0] : null;
          var _caMakeText = _caMakeArg && typeof _caMakeArg === 'object'
            ? String(_caMakeArg.text == null ? '' : _caMakeArg.text) : '';
          var _caMakeTitle = _caMakeArg && typeof _caMakeArg === 'object'
            ? String(_caMakeArg.title == null ? '' : _caMakeArg.title) : '';
          if (!_caMakeText.trim() || _caMakeText.length > 240000 ||
              _caMakeTitle.length > 240) {
            throw new Error('BW_READER_CLIENT_ACTION_INVALID:' + _caFn);
          }
          _caTarget = window._nativeReaderMakeNote;
          _caCall = function (target) {
            return target.call(window, {
              title: _caMakeTitle,
              text: _caMakeText
            });
          };
        } else if (_caFn === '_nativeReaderMarkVocabulary') {
          var _caVocabArg = Array.isArray(p.args) ? p.args[0] : null;
          var _caVocabWord = _caVocabArg && typeof _caVocabArg === 'object'
            ? String(_caVocabArg.word == null ? '' : _caVocabArg.word) : '';
          var _caVocabMark = _caVocabArg && typeof _caVocabArg === 'object'
            ? String(_caVocabArg.mark == null ? '' : _caVocabArg.mark) : '';
          if (!_caVocabWord.trim() || _caVocabWord.length > 128 ||
              (_caVocabMark !== 'known' && _caVocabMark !== 'unknown')) {
            throw new Error('BW_READER_CLIENT_ACTION_INVALID:' + _caFn);
          }
          _caTarget = window._nativeReaderMarkVocabulary;
          _caCall = function (target) {
            return target.call(window, {
              word: _caVocabWord,
              mark: _caVocabMark
            });
          };
        } else if (_caFn === '_bwWebHighlightByText') {
          var _caWebArg = Array.isArray(p.args) ? p.args[0] : null;
          var _caWebExact = _caWebArg && typeof _caWebArg === 'object'
            ? String(_caWebArg.exact == null ? '' : _caWebArg.exact) : '';
          if (!_caWebExact.trim() || _caWebExact.length > 2000) {
            throw new Error('BW_READER_CLIENT_ACTION_INVALID:' + _caFn);
          }
          var _caWebColor = _caWebArg && typeof _caWebArg.color === 'string'
            ? _caWebArg.color : '';
          var _caWebNote = _caWebArg && typeof _caWebArg.note === 'string'
            ? _caWebArg.note : '';
          if (_caWebColor.length > 32 || _caWebNote.length > 2000) {
            throw new Error('BW_READER_CLIENT_ACTION_INVALID:' + _caFn);
          }
          _caTarget = window._bwWebHighlightByText;
          _caCall = function (target) {
            return target.call(window, {
              exact: _caWebExact,
              prefix: _caWebArg && typeof _caWebArg.prefix === 'string'
                ? _caWebArg.prefix.slice(0, 200) : '',
              suffix: _caWebArg && typeof _caWebArg.suffix === 'string'
                ? _caWebArg.suffix.slice(0, 200) : '',
              color: _caWebColor,
              note: _caWebNote
            });
          };
        } else if (_caFn === '_bwWebNoteCreate') {
          var _caWnArg = Array.isArray(p.args) ? p.args[0] : null;
          var _caWnText = _caWnArg && typeof _caWnArg === 'object'
            ? String(_caWnArg.text == null ? '' : _caWnArg.text) : '';
          if (!_caWnText.trim() || _caWnText.length > 4000) {
            throw new Error('BW_READER_CLIENT_ACTION_INVALID:' + _caFn);
          }
          _caTarget = window._bwWebNoteCreate;
          _caCall = function (target) {
            return target.call(window, { text: _caWnText });
          };
        } else if (_caFn === '__upStartTask') {
          // 交互练习纸:normalizer 已做结构闸,执行侧再卡一次形状后调页面的
          // 造纸入口(pdf-uishared;本机书在入口内自动走 _lp 本地分支)。
          var _caPaper = _caArgs.length === 1 && _caArgs[0] &&
            typeof _caArgs[0] === 'object' && !Array.isArray(_caArgs[0])
            ? _caArgs[0] : null;
          var _caPaperBlocks = _caPaper && _caPaper.params &&
            Array.isArray(_caPaper.params.blocks) ? _caPaper.params.blocks : null;
          if (!_caPaper || _caPaper.kind !== 'free' || !_caPaperBlocks ||
              !_caPaperBlocks.length || _caPaperBlocks.length > 48) {
            throw new Error('BW_READER_CLIENT_ACTION_INVALID:' + _caFn);
          }
          _caTarget = window.__upStartTask;
          _caCall = function (target) { return target.call(window, _caPaper); };
        } else {
          throw new Error('BW_READER_CLIENT_ACTION_INVALID:' + _caFn);
        }
        if (typeof _caTarget !== 'function') {
          throw new Error(
            'BW_READER_CLIENT_ACTION_UNAVAILABLE:' + _caFn
          );
        }
        work = Promise.resolve(_caCall(_caTarget));
      } else {
        throw new Error('BW_READER_REALTIME_OUTPUT_KIND_UNSUPPORTED');
      }
      return Promise.resolve(work).then(function (value) {
        var receipt = { outcome: 'applied' };
        // ⚠ 回调原先**不接参数**，各分支辛苦拼的结构化结果在这里被整个丢掉，
        //   回执永远是那个固定字面量。2026-08-19 用户连问两轮「AI 说成功但
        //   什么都看不到」，根子就在这一行。
        //   ⚠ 只搬认识的键，别整个 assign —— 下游 exactObject 是全等白名单，
        //     多一个键会让**成功的投递被回成 rejected**，且链路上零报错。
        if (value && typeof value === 'object') {
          if (value.bindOutcome) receipt.bindOutcome = value.bindOutcome;
          if (value.bindReason) receipt.bindReason = value.bindReason;
        }
        if (_readerOutputNeedsBound(delivery) && receipt.bindOutcome !== 'bound') {
          _readerOutputAwaitingBind[delivery.correlation] = 1;
        } else {
          delete _readerOutputAwaitingBind[delivery.correlation];
          _rememberReaderOutput(delivery.correlation, receipt);
        }
        return receipt;
      }, function (error) {
        return _readerOutputReject(error);
      });
    } catch (error) {
      return Promise.resolve(_readerOutputReject(error));
    }
  }
  function _acceptReaderRealtimeOutput(delivery) {
    if (!delivery || !delivery.correlation || !delivery.kind) {
      return Promise.resolve(_readerOutputReject('BW_READER_REALTIME_OUTPUT_INVALID'));
    }
    var correlation = String(delivery.correlation);
    var seen = _readerOutputSeen[correlation];
    if (seen) {
      var replay = { outcome: 'replay' };
      // Durable page-chars 卡收到 bound 后，桥接端还可能在落本地 outbox 状态前
      // 崩溃。重放必须继续证明它已 bound，不能退化成无结果的 replay。
      if (seen.bindOutcome) replay.bindOutcome = seen.bindOutcome;
      if (seen.bindReason) replay.bindReason = seen.bindReason;
      return Promise.resolve(replay);
    }
    // 同一 correlation 在第一次落库尚未完成时可能被桥接层重送。共享同一 Promise，
    // 既不重复写 placement，也让两个调用者得到同一条真实回执。
    if (_readerOutputPending[correlation]) return _readerOutputPending[correlation];
    // Defer the real work by one microtask so the pending entry is installed
    // before any host/repository callback can synchronously re-enter us.
    var pending = Promise.resolve().then(function () {
      return _applyReaderRealtimeOutput(delivery);
    }).then(
      function (receipt) {
        delete _readerOutputPending[correlation];
        return receipt;
      },
      function (error) {
        delete _readerOutputPending[correlation];
        return _readerOutputReject(error);
      }
    );
    _readerOutputPending[correlation] = pending;
    return pending;
  }
  // 把已登记的草稿镜像到当前对话流，与浮层构成同一实体的两个视图。
  //
  // 侧栏打开时浮层容器被让位，草稿就此完全不可见，也不进对话历史 —— 用户只知道
  // "卡没了"。原设计是同 gid 双宿主：浮层与流内是同一张卡的两处显示。
  // 所以这里传的 gid / cards / repositorySource / localDraft 必须与上游登记时
  // 一模一样。
  //
  // entityRegistered 保持 false：它是旧 Pi entity registry 的兼容标志
  // （rc-flashcard 的 legacy.piEntityRegistered 与 _stateSync 都据此判断），
  // **不表示本地 card-repository 已 registerDraft**。相同 gid/cards/source 的
  // 第二次 registerDraft 本身幂等，不会产生第二个实体；把它写成 true 反而会
  // 让流内那张被当成"已在 Pi 注册过"，那是另一回事。
  function _mirrorDraftIntoTurnFlow(cards, gid, repositorySource, localDraft) {
    try {
      if (!(RC.turnCard && typeof RC.turnCard.addPart === 'function')) return false;
      // 始终用以 gid 命名的确定性轮次，既不读 current() 也不绑 __asstVoiceTid。
      //
      // 这条协议不带 threadId：__asstVoiceTid 可能是个陈旧的语音轮次，而
      // RC.turnCard.current() 会被 loadHistory 的 renderTurn 改写 —— 两者都可能
      // 把草稿挂到别人的轮次上。确定性 tid 没有这个歧义，重复投递也落在同一处。
      //
      // ⚠ 只承诺本轮侧栏可见，不承诺跨会话持久：rc-assistant 的 onChange 走
      // upsert_only，没有既存 turn 记录时不会凭空建一条。草稿的持久化权威是
      // 本地 card repository，不是这条对话流记录。
      var tid = 'reader-draft:' + String(gid);
      if (typeof RC.turnCard.idle === 'function') RC.turnCard.idle(tid);
      RC.turnCard.addPart(tid, {
        kind: 'cards',
        cards: cards,
        draft: true,
        gid: gid,
        entityRegistered: false,
        repositorySource: repositorySource,
        localDraft: localDraft || null
      });
      return true;
    } catch (e) { return false; }
  }

  RC.voicecall = RC.voicecall || {};
  RC.voicecall.acceptRealtimeOutput = _acceptReaderRealtimeOutput;
  // ── 70 结构化结果卡(用户设计):web_search 的 Gemini 综合直接给 kind/data/brief——
  //    系统按类型渲染(天气/新闻/事实/综合),2.1 只口头一句概况;双击卡=内容带入 2.1 上下文(再双击=移出) ──
  function _cardHttpsURL(value) {
    var raw = String(value == null ? '' : value);
    if (!raw || raw !== raw.trim()) return '';
    try {
      var parsed = new URL(raw);
      if (parsed.protocol !== 'https:' || !parsed.hostname || parsed.username || parsed.password || parsed.hash) return '';
      return parsed.href;
    } catch (e) { return ''; }
  }
  function _cardMediaURL(value) {
    var raw = String(value == null ? '' : value).trim();
    if (!raw) return '';
    // 这些地址已由 Reader 自己签发或持有；外部 card 合同只额外允许 HTTPS。
    if (/^(?:blob:|data:image\/)/i.test(raw) ||
        /^\/pdf\/api\/(?:page-image(?:\?|$)|asset\/|img-proxy(?:\?|$))/.test(raw)) return raw;
    var remote = _cardHttpsURL(raw);
    return remote ? '/pdf/api/img-proxy?url=' + encodeURIComponent(remote) : '';
  }
  function _cardAssetID(value) {
    var raw = String(value == null ? '' : value).trim();
    return /^[a-z]{2,4}_[a-f0-9]{4,12}$/.test(raw) ? raw : '';
  }
  function _cardAssetURL(value) {
    var aid = _cardAssetID(value);
    return aid ? '/pdf/api/asset/' + aid + '?proxy=1' : '';
  }
  function _cardImageURL(item) {
    item = item || {};
    return _cardAssetURL(item.aid) || _cardMediaURL(item.url);
  }
  function _videoCardRef(item) {
    item = item || {};
    var url = _cardHttpsURL(item.url);
    var id = '', source = '';
    var hint = String(item.src || '').toLowerCase();
    try {
      if (url) {
        var parsed = new URL(url);
        var host = parsed.hostname.toLowerCase();
        var parts = parsed.pathname.split('/').filter(Boolean);
        if (host === 'youtu.be') {
          source = 'yt'; id = parts[0] || '';
        } else if (host === 'youtube.com' || host === 'www.youtube.com' ||
                   host === 'm.youtube.com' || host === 'music.youtube.com' ||
                   host === 'youtube-nocookie.com' || host === 'www.youtube-nocookie.com') {
          source = 'yt';
          if (parsed.pathname === '/watch') id = parsed.searchParams.get('v') || '';
          else if (/^(?:embed|shorts|live)$/.test(parts[0] || '')) id = parts[1] || '';
        } else if (host === 'bilibili.com' || /\.bilibili\.com$/.test(host) || host === 'b23.tv') {
          source = 'bili';
          if ((parts[0] || '').toLowerCase() === 'video') id = parts[1] || '';
        }
      }
    } catch (e) {}
    if (!source && /bili|b站|哔哩/.test(hint)) source = 'bili';
    if (!source && /youtube|\byt\b/.test(hint)) source = 'yt';
    if (source === 'bili') {
      if (!/^(?:BV[0-9A-Za-z]{10}|av\d{1,16})$/.test(id)) id = '';
      if (!id && /^(?:BV[0-9A-Za-z]{10}|av\d{1,16})$/.test(String(item.id || ''))) id = String(item.id);
    } else {
      if (!/^[A-Za-z0-9_-]{11}$/.test(id)) id = '';
      if (!id && /^[A-Za-z0-9_-]{11}$/.test(String(item.id || ''))) id = String(item.id);
      if (id) source = 'yt';
    }
    if (!url && id) {
      url = source === 'bili'
        ? 'https://www.bilibili.com/video/' + encodeURIComponent(id)
        : 'https://www.youtube.com/watch?v=' + encodeURIComponent(id);
    }
    return { id: id, src: source, url: url };
  }
  function _videoCardThumb(item, ref) {
    var direct = _cardMediaURL(_videoCardThumbSource(item, ref));
    if (direct) return direct;
    return '';
  }
  function _videoCardThumbSource(item, ref) {
    var raw = String((item || {}).thumb || '').trim();
    if (_cardMediaURL(raw)) return raw;
    if (ref && ref.src === 'yt' && ref.id) {
      return 'https://i.ytimg.com/vi/' + encodeURIComponent(ref.id) + '/mqdefault.jpg';
    }
    return '';
  }
  function _videoButtonRef(button) {
    if (!button) return { id: '', src: '', url: '' };
    var direct = _videoCardRef({
      id: button.getAttribute('data-video-id') || '',
      src: button.getAttribute('data-video-src') || '',
      url: button.getAttribute('data-video-url') || ''
    });
    if (direct.id || direct.url) return direct;
    // 旧的已固定 YouTube 卡没有在按钮上保存播放身份，但缩略图仍保存
    // i.ytimg.com/vi/<id>/... 的稳定来源；从它恢复，不要求用户重新生成卡片。
    try {
      var cell = button.closest && button.closest('.vc-ig-cell');
      var image = cell && cell.querySelector('.vc-ig-img');
      var raw = String(image && (image.getAttribute('data-source-url') || image.getAttribute('src')) || '');
      var outer = new URL(raw, window.location.href);
      if (outer.pathname === '/pdf/api/img-proxy') raw = outer.searchParams.get('url') || '';
      var parsed = new URL(raw, window.location.href);
      var host = parsed.hostname.toLowerCase();
      var parts = parsed.pathname.split('/').filter(Boolean);
      if ((host === 'i.ytimg.com' || host === 'img.youtube.com') &&
          parts[0] === 'vi' && /^[A-Za-z0-9_-]{11}$/.test(parts[1] || '')) {
        return _videoCardRef({ id: parts[1], src: 'yt' });
      }
    } catch (e) {}
    return direct;
  }
  function _openVideoRef(ref, title) {
    ref = ref || {};
    try {
      if (ref.id && window.RC && window.RC.videoPlayer &&
          typeof window.RC.videoPlayer.open === 'function') {
        window.RC.videoPlayer.open({
          id: ref.id,
          src: ref.src === 'bili' ? 'bili' : 'yt',
          title: String(title || '')
        });
        return true;
      }
      if (ref.url) {
        window.open(ref.url, '_blank');
        return true;
      }
      if (typeof _toast === 'function') _toast('视频地址无效');
    } catch (e) {
      try { if (typeof _toast === 'function') _toast('视频无法打开'); } catch (_) {}
    }
    return false;
  }
  var _pinnedVideoClickBound = false;
  function _bindPinnedVideoClicks() {
    if (_pinnedVideoClickBound) return;
    _pinnedVideoClickBound = true;
    // HTML 便签会在重开/同步时重建 DOM。把播放行为委托到 document，固定后的
    // 视频卡无需保存闭包，也不会因为便签重挂而丢失点击能力。
    document.addEventListener('click', function (event) {
      var button = event.target && event.target.closest &&
        event.target.closest('.rc-note .vc-vg-play,.bw-page-pin .vc-vg-play');
      if (!button) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      _openVideoRef(
        _videoButtonRef(button),
        button.getAttribute('data-video-title') || ''
      );
    }, true);
  }
  _bindPinnedVideoClicks();
  var _cardImageFallbackBound = false;
  function _bindCardImageFallbacks() {
    if (_cardImageFallbackBound) return;
    _cardImageFallbackBound = true;
    // Asset ids are the durable primary source.  A raw HTTPS source is kept
    // only as a one-shot compatibility fallback for an account whose asset
    // registry has not reached this host yet.  The fallback still crosses the
    // existing bounded same-origin proxy; arbitrary network URLs never become
    // DOM image sources.
    // In the ordinary-page extension host `document` is a facade whose body is
    // the actual Reader root inside a ShadowRoot.  Image error events are not
    // composed, so a listener forwarded to the host document never sees them;
    // capture on the Reader root works in both the Shadow DOM host and PWA.
    var eventRoot = document.body && document.body.addEventListener
      ? document.body : document;
    eventRoot.addEventListener('error', function (event) {
      var image = event.target;
      if (!image || String(image.tagName || '').toLowerCase() !== 'img' ||
          image.getAttribute('data-asset-fallback-done') === '1') return;
      var aid = _cardAssetID(image.getAttribute('data-aid'));
      var primary = _cardAssetURL(aid);
      if (!aid || image.getAttribute('src') !== primary) return;
      var sourceURL = _cardHttpsURL(image.getAttribute('data-source-url'));
      var fallback = sourceURL
        ? '/pdf/api/img-proxy?url=' + encodeURIComponent(sourceURL) : '';
      if (!fallback || fallback === primary) return;
      image.setAttribute('data-asset-fallback-done', '1');
      image.setAttribute('src', fallback);
    }, true);
  }
  _bindCardImageFallbacks();
  function _infoHtml(card) {
    var k = card.kind, d = card.data || {}, h = '';
    function e0(x) { return esc(String(x == null ? '' : x)); }
    // 长文本字段走 Markdown。此前全字段一律 e0()=esc() 转义死,再走 _cardDom 的 isHtml
    // 支路(那里还把 white-space 从 pre-wrap 改成 normal)—— 于是 Markdown 不渲染、
    // **连换行都没了**,两个症状同一个来源。
    // RC.md 内部已经过 RC.safeHtml 净化;拿不到管线就退回转义,绝不把未净化的原文塞进 innerHTML。
    // 只用于成段的正文;weather/news 那些短字段(cond/loc/标题)仍走 e0 —— 它们里的
    // * _ 是字面量,过 marked 会被当强调符吃掉。
    function md(x) {
      var s = String(x == null ? '' : x);
      if (!s) return '';
      try { if (window.RC && RC.md) return RC.md(s); } catch (e) {}
      return e0(s);
    }
    if (k === 'weather') {
      h = '<div class="vc-if-w"><div class="vc-if-wt">' + e0(d.lo) + '–' + e0(d.hi) + '°C</div>' +
          '<div class="vc-if-wc">' + e0(d.cond) + (d.precip != null ? ' · 降水 ' + e0(d.precip) + '%' : '') + '</div>' +
          '<div class="vc-if-ws">' + e0(d.loc) + ' ' + e0(d.date) + '</div>' +
          (d.tip ? '<div class="vc-if-tip">' + e0(d.tip) + '</div>' : '') + '</div>';
    } else if (k === 'news') {
      h = '<div class="vc-if-n">' + (d.items || []).slice(0, 5).map(function (it) {
        return '<div class="vc-if-ni"><div class="vc-if-nt">' + e0(it.t) + '</div>' +
               '<div class="vc-if-ns">' + e0(it.s) + (it.src ? ' <span class="vc-if-src">— ' + e0(it.src) + '</span>' : '') + '</div></div>';
      }).join('') + '</div>';
    } else if (k === 'images') {
      h = '<div class="vc-ig">' + (d.items || []).map(function (it, i) {
        if (it._gone) return '';   // ✕删除的图不再渲染(拖整框/回放/三态重渲都不带回;data-i 保原索引供 ✕ 定位)
        var aid = _cardAssetID(it.aid), media = _cardImageURL(it);
        var mapUrl = _mapMetaFromUrl(it.url) ? it.url : '';
        return '<div class="vc-ig-cell" data-i="' + i + '"' +
          (mapUrl ? ' data-map-url="' + esc(mapUrl) + '"' : '') + '>' +
          '<button type="button" class="vc-ig-x" data-i="' + i + '" aria-label="移除">✕</button>' +
          (mapUrl ? '<button type="button" class="vc-ig-map" data-i="' + i + '" aria-label="全屏地图">⛶</button>' : '') +
          (media ? '<img class="vc-ig-img" data-i="' + i + '"' + (aid ? ' data-aid="' + esc(aid) + '"' : '') +
            ' data-source-url="' + esc(it.url || '') + '" src="' + esc(media) + '" alt="' + esc(it.title || '') + '">' :
            '<span class="rc-img-broken">🖼 图片地址无效</span>') +
          (it.title ? '<div class="vc-ig-t">' + esc(it.title) + '</div>' : '') + '</div>';
      }).join('') + '</div>';
    } else if (k === 'videos') {
      h = '<div class="vc-ig">' + (d.items || []).map(function (it, i) {
        if (it._gone) return '';   // ✕删除的视频不再渲染(同图卡)
        var ref = _videoCardRef(it), thumbSource = _videoCardThumbSource(it, ref), thumb = _videoCardThumb(it, ref);
        var isBili = ref.src === 'bili';
        return '<div class="vc-ig-cell" data-i="' + i + '">' +
          '<button type="button" class="vc-ig-x" data-i="' + i + '" aria-label="移除">✕</button>' +
          '<span class="vc-vg-tag' + (isBili ? ' bili' : '') + '">' + (isBili ? 'B站' : (ref.src === 'yt' ? 'YouTube' : esc(it.src || '视频'))) + '</span>' +
          '<div class="vc-vg-wrap">' + (thumb ? '<img class="vc-ig-img" data-i="' + i + '" loading="lazy" referrerpolicy="same-origin" data-source-url="' + esc(thumbSource) + '" src="' + esc(thumb) + '" alt="">' : '<div class="vc-vg-empty">无预览图</div>') +
          '<button type="button" class="vc-vg-play" data-i="' + i + '"' +
          ' data-video-id="' + esc(ref.id || '') + '"' +
          ' data-video-src="' + esc(ref.src || '') + '"' +
          ' data-video-url="' + esc(ref.url || '') + '"' +
          ' data-video-title="' + esc(it.title || '') + '" aria-label="播放">▶</button></div>' +
          '<div class="vc-ig-t">' + esc(it.title || '') + (it.channel ? '<br><span class="vc-vg-ch">' + esc(it.channel) + '</span>' : '') + '</div></div>';
      }).join('') + '</div>';
    } else if (k === 'fact') {
      h = '<div class="vc-if-f"><div class="vc-if-fa">' + md(d.answer) + '</div>' +
          (d.detail ? '<div class="vc-if-fd">' + md(d.detail) + '</div>' : '') + '</div>';
    } else {
      h = '<div class="vc-if-g">' + md(d.text || card.brief || '') + '</div>';
    }
    if (card.sources && card.sources.length) {
      h += '<div class="vc-if-srcs">' + card.sources.slice(0, 3).map(function (sc) {
        return '<a href="' + esc(sc.url || '#') + '" target="_blank" rel="noopener">' + e0((sc.title || '来源').split('.')[0]) + '</a>';
      }).join(' · ') + '</div>';
    }
    return h;
  }
  // ── 交互地图（2026-08-26 用户拍板：卡片内直接能拖能缩，全屏另给按钮）──
  //
  // 为什么不用 Apple 原生地图：卡片是网页（App 内是 WKWebView、Safari 里
  // 就是网页），MKMapView 是原生视图 —— 只能悬浮在网页**之上**，得靠坐标
  // 同步跟着卡片拖动/折叠/滚动，而且只在 App 里有。Apple 给网页的 MapKit JS
  // 要开发者密钥 + 自建 JWT 签名 + 外部脚本（撞 CSP）。这套手写瓦片引擎是
  // 纯本地的，三个表面（App / Safari / 桌面）同一份代码。
  //
  // 性能：一屏可见区域只有几张 256px 瓦片，平移是纯定位、缩放整层重建，
  // 比一张大静态图重不了多少。
  //
  // 坐标：坐标直接从静态图 URL 解析（Google staticmap / Yandex），AI 侧
  // 什么都不用改。
  function _mapMetaFromUrl(url) {
    var u = String(url || '');
    var m, lat, lon, zoom = 5, marks = [];
    if (/maps\.googleapis\.com\/maps\/api\/staticmap/.test(u)) {
      m = u.match(/[?&]center=(-?[0-9.]+)(?:,|%2C)(-?[0-9.]+)/);
      if (m) { lat = +m[1]; lon = +m[2]; }
      m = u.match(/[?&]zoom=([0-9]+)/); if (m) zoom = +m[1];
      var mm = u.match(/[?&]markers=([^&]+)/g) || [];
      mm.forEach(function (piece) {
        var mk = decodeURIComponent(piece).match(/(-?[0-9.]+),(-?[0-9.]+)\s*$/);
        if (mk) marks.push([+mk[1], +mk[2]]);
      });
      if (lat == null && marks.length) { lat = marks[0][0]; lon = marks[0][1]; }
    } else if (/static-maps\.yandex\.ru/.test(u)) {
      m = u.match(/[?&]ll=(-?[0-9.]+)(?:,|%2C)(-?[0-9.]+)/);
      if (m) { lon = +m[1]; lat = +m[2]; }   // Yandex 经度在前
      m = u.match(/[?&]z=([0-9]+)/); if (m) zoom = +m[1];
      var pt = u.match(/[?&]pt=([^&]+)/);
      if (pt) decodeURIComponent(pt[1]).split('~').forEach(function (piece) {
        var mk = piece.match(/^(-?[0-9.]+),(-?[0-9.]+)/);
        if (mk) marks.push([+mk[2], +mk[1]]);
      });
      if (lat == null && marks.length) { lat = marks[0][0]; lon = marks[0][1]; }
    }
    if (lat == null || lon == null || !isFinite(lat) || !isFinite(lon)) return null;
    if (!marks.length) marks.push([lat, lon]);
    return { lat: lat, lon: lon, zoom: Math.max(2, Math.min(19, zoom)), marks: marks };
  }

  /// 把一个容器变成活地图。inline 与全屏共用这一个引擎（唯一实现）。
  /// 返回 { destroy } —— 卡片被移除时要断掉监听，否则每张关掉的地图卡
  /// 都会在 window 上留一对 scroll/resize 监听（泄漏会随卡片数量累积）。
  function _mountMapView(viewport, meta) {
    var TS = 256;
    var useProxy = /^127\.0\.0\.1$|^localhost$/.test(location.hostname);
    function tileSrc(z, x, y) {
      var n = 1 << z;
      x = ((x % n) + n) % n;
      if (y < 0 || y >= n) return '';
      var remote = 'https://tile.openstreetmap.org/' + z + '/' + x + '/' + y + '.png';
      return useProxy ? '/pdf/api/img-proxy?url=' + encodeURIComponent(remote) : remote;
    }
    function w2ll(wx, wy, z) {
      var s = TS * (1 << z);
      var lon = wx / s * 360 - 180;
      var n = Math.PI - 2 * Math.PI * wy / s;
      return [180 / Math.PI * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n))), lon];
    }
    function ll2w(lat, lon, z) {
      var s = TS * (1 << z);
      var x = (lon + 180) / 360 * s;
      var r = lat * Math.PI / 180;
      var y = (1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2 * s;
      return [x, y];
    }
    var st = { lat: meta.lat, lon: meta.lon, z: meta.zoom };
    var world = document.createElement('div');
    world.className = 'vc-map-world';
    viewport.appendChild(world);
    var tiles = {};
    function render() {
      var vw = viewport.clientWidth, vh = viewport.clientHeight;
      if (!vw || !vh) return;   // 还没布局(折叠态/未挂载):这一拍跳过
      var c = ll2w(st.lat, st.lon, st.z);
      var left = c[0] - vw / 2, top = c[1] - vh / 2;
      var x0 = Math.floor(left / TS), y0 = Math.floor(top / TS);
      var x1 = Math.floor((left + vw) / TS), y1 = Math.floor((top + vh) / TS);
      var want = {};
      for (var ty = y0; ty <= y1; ty++) for (var tx = x0; tx <= x1; tx++) {
        var key = st.z + '/' + tx + '/' + ty;
        want[key] = 1;
        var img = tiles[key];
        if (!img) {
          var src = tileSrc(st.z, tx, ty);
          if (!src) continue;
          img = document.createElement('img');
          img.className = 'vc-map-tile';
          img.decoding = 'async';
          img.src = src;
          tiles[key] = img;
          world.appendChild(img);
        }
        img.style.left = (tx * TS - left) + 'px';
        img.style.top = (ty * TS - top) + 'px';
      }
      Object.keys(tiles).forEach(function (key) {
        if (!want[key]) { tiles[key].remove(); delete tiles[key]; }
      });
      world.querySelectorAll('.vc-map-pin').forEach(function (el) { el.remove(); });
      meta.marks.forEach(function (mk) {
        var w = ll2w(mk[0], mk[1], st.z);
        var pin = document.createElement('div');
        pin.className = 'vc-map-pin';
        pin.style.left = (w[0] - left) + 'px';
        pin.style.top = (w[1] - top) + 'px';
        world.appendChild(pin);
      });
    }
    function zoomTo(dz, px, py) {
      var nz = Math.max(2, Math.min(19, st.z + dz));
      if (nz === st.z) return;
      var vw = viewport.clientWidth, vh = viewport.clientHeight;
      var fx = px == null ? vw / 2 : px, fy = py == null ? vh / 2 : py;
      var c = ll2w(st.lat, st.lon, st.z);
      var focus = w2ll(c[0] - vw / 2 + fx, c[1] - vh / 2 + fy, st.z);
      var fw = ll2w(focus[0], focus[1], nz);
      var nc = w2ll(fw[0] + (vw / 2 - fx), fw[1] + (vh / 2 - fy), nz);
      st.lat = nc[0]; st.lon = nc[1]; st.z = nz;
      Object.keys(tiles).forEach(function (key) { tiles[key].remove(); delete tiles[key]; });
      render();
    }
    var drag = null, pinch = null;
    function onDown(ev) {
      if (drag && pinch == null && ev.pointerId !== drag.id) {
        pinch = { a: drag.id, b: ev.pointerId, ax: drag.lx, ay: drag.ly,
                  bx: ev.clientX, by: ev.clientY, base: null };
      } else {
        drag = { id: ev.pointerId, lx: ev.clientX, ly: ev.clientY };
      }
      try { viewport.setPointerCapture(ev.pointerId); } catch (e) {}
    }
    function onMove(ev) {
      if (pinch) {
        if (ev.pointerId === pinch.a) { pinch.ax = ev.clientX; pinch.ay = ev.clientY; }
        else if (ev.pointerId === pinch.b) { pinch.bx = ev.clientX; pinch.by = ev.clientY; }
        else return;
        var d = Math.hypot(pinch.ax - pinch.bx, pinch.ay - pinch.by);
        if (pinch.base == null) { pinch.base = d; return; }
        if (d > pinch.base * 1.35) { zoomTo(1, (pinch.ax + pinch.bx) / 2, (pinch.ay + pinch.by) / 2); pinch.base = d; }
        else if (d < pinch.base / 1.35) { zoomTo(-1, (pinch.ax + pinch.bx) / 2, (pinch.ay + pinch.by) / 2); pinch.base = d; }
        return;
      }
      if (!drag || ev.pointerId !== drag.id) return;
      var dx = ev.clientX - drag.lx, dy = ev.clientY - drag.ly;
      drag.lx = ev.clientX; drag.ly = ev.clientY;
      var c = ll2w(st.lat, st.lon, st.z);
      var nc = w2ll(c[0] - dx, c[1] - dy, st.z);
      st.lat = nc[0]; st.lon = nc[1];
      render();
    }
    function onUp(ev) {
      if (pinch && (ev.pointerId === pinch.a || ev.pointerId === pinch.b)) pinch = null;
      if (drag && ev.pointerId === drag.id) drag = null;
    }
    function onWheel(ev) {
      ev.preventDefault();
      var r = viewport.getBoundingClientRect();
      zoomTo(ev.deltaY < 0 ? 1 : -1, ev.clientX - r.left, ev.clientY - r.top);
    }
    function onDouble(ev) {
      ev.preventDefault();
      var r = viewport.getBoundingClientRect();
      zoomTo(1, ev.clientX - r.left, ev.clientY - r.top);
    }
    viewport.addEventListener('pointerdown', onDown);
    viewport.addEventListener('pointermove', onMove);
    viewport.addEventListener('pointerup', onUp);
    viewport.addEventListener('pointercancel', onUp);
    viewport.addEventListener('wheel', onWheel, { passive: false });
    viewport.addEventListener('dblclick', onDouble);
    // 卡片可以被拉伸/折叠/展开,尺寸变了要重排瓦片。ResizeObserver 只盯
    // 这一个元素,比在 window 上挂 resize 更准也更省。
    var ro = null;
    try {
      ro = new ResizeObserver(function () { render(); });
      ro.observe(viewport);
    } catch (e) {}
    render();
    return {
      zoom: zoomTo,
      state: st,
      destroy: function () {
        try { if (ro) ro.disconnect(); } catch (e) {}
        viewport.removeEventListener('pointerdown', onDown);
        viewport.removeEventListener('pointermove', onMove);
        viewport.removeEventListener('pointerup', onUp);
        viewport.removeEventListener('pointercancel', onUp);
        viewport.removeEventListener('wheel', onWheel);
        viewport.removeEventListener('dblclick', onDouble);
        world.remove();
      }
    };
  }

  function _openMapViewer(meta, title) {
    var ov = document.createElement('div');
    ov.className = 'vc-mapov';
    ov.innerHTML =
      '<div class="vc-map-hd"><span>' + esc(title || '地图') + '</span>' +
      '<span class="vc-map-attr">© OpenStreetMap</span>' +
      '<button type="button" class="vc-map-z" data-d="1">＋</button>' +
      '<button type="button" class="vc-map-z" data-d="-1">－</button>' +
      '<button type="button" class="vc-map-x">✕</button></div>' +
      '<div class="vc-map-vp"></div>';
    document.body.appendChild(ov);
    var view = _mountMapView(ov.querySelector('.vc-map-vp'), {
      lat: meta.lat, lon: meta.lon, zoom: meta.zoom, marks: meta.marks
    });
    ov.querySelectorAll('.vc-map-z').forEach(function (btn) {
      btn.addEventListener('click', function () { view.zoom(+btn.dataset.d); });
    });
    function close() { try { view.destroy(); } catch (e) {} ov.remove(); }
    ov.querySelector('.vc-map-x').addEventListener('click', close);
    return ov;
  }

  /// 把图卡里的静态地图升级成活地图（渐进增强）：静态 <img> 始终在，
  /// 这里只是叠一层可交互瓦片。任何一处没跑到这个函数（侧栏/钉页/回放
  /// 等实例），看到的仍是那张静态图，而不是空框。
  function _upgradeMapCells(root) {
    if (!root || !root.querySelectorAll) return;
    root.querySelectorAll('.vc-ig-cell[data-map-url]').forEach(function (cell) {
      if (cell.__bwMapView) return;
      var meta = _mapMetaFromUrl(cell.getAttribute('data-map-url'));
      if (!meta) return;
      var live = document.createElement('div');
      live.className = 'vc-map-live';
      cell.appendChild(live);
      try {
        cell.__bwMapView = _mountMapView(live, meta);
        cell.__bwMapMeta = meta;
        cell.classList.add('vc-map-ready');   // 就位后才盖住静态图
      } catch (e) {
        live.remove();
      }
    });
  }


  function _infoText(card) {   // 双击带入上下文用的纯文本化
    var d = card.data || {}, k = card.kind;
    if (k === 'weather') return (card.title || '天气') + ':' + [d.loc, d.date, d.cond, (d.lo != null ? d.lo + '-' + d.hi + '°C' : ''), (d.precip != null ? '降水' + d.precip + '%' : ''), d.tip].filter(Boolean).join(',');
    if (k === 'news') return (card.title || '新闻') + ':' + (d.items || []).map(function (it) { return (it.t || '') + '(' + (it.s || '') + ')'; }).join(';');
    if (k === 'fact') return (card.title || '') + ':' + (d.answer || '') + ' ' + (d.detail || '');
    if (k === 'images') return (card.title || '配图') + ':' + (d.items || []).filter(function (it) { return !it._gone; }).map(function (it) { return (it.title || '图') + (it.src ? '[源:' + it.src + ']' : ''); }).join(';') + '(图片本身在用户屏幕上;上下文只带元数据,不含图片/URL)';   // 用户拍板:带图卡入上下文=每张图的元数据,不是图本身;✕删除的不带
    if (k === 'videos') return (card.title || '视频') + ':' + (d.items || []).map(function (it) { return (it.title || '') + '(' + (it.channel || '') + ')' + (it.url || ''); }).join(';');
    return d.text || card.brief || card.title || '';
  }
  // 77 pin 状态中心:选中集合为唯一真相(卡片紫框只是视图)。注入改**覆盖式快照**(防抖 1.2s+指纹):
  // 反复选中/取消若最终状态没变=零注入;变了=一条"当前带入清单(以本条为准,旧声明作废)"——历史不膨胀、语义无歧义
  var _pins = { map: {}, fp: null, t: null, els: {}, cids: {}, ids: {} };   // 95:cids={卡片稳定编号:label}；ids={label:语义上下文编号}
  var _cidSeq = 0;
  function _mkCid() { return 'c' + Date.now().toString(36) + '-' + (++_cidSeq); }   // 95:卡片出生编号,跟随卡片所有形态流转
  function _ctxSelectionRegistry() {
    try { return window.BWReaderRuntime && window.BWReaderRuntime.contextSelections; } catch (e) { return null; }
  }
  function _pinContextId(el, cid, spec) {
    spec = spec || {};
    var id = String(spec.id || (el && el.dataset && el.dataset.vcContextId) || '');
    if (!id && el && el.dataset && el.dataset.turn) id = 'turn:' + el.dataset.turn;
    if (!id) id = cid ? ('card:' + cid) : ('context:' + _mkCid());
    try { if (el && el.dataset) el.dataset.vcContextId = id; } catch (e) {}
    return id;
  }
  function _outgoingKindOf(spec, el) {
    // 把既有的选中对象类型映射成 outgoing 的稳定 kind。未识别的一律 'card'
    // (它们都是卡片系统里的对象),不新增类型、不猜。
    var k = String((spec && spec.kind) || '');
    if (k === 'image-item') return 'image';
    if (k === 'video-item') return 'image';       // 视频封面按图片语义(服务端 kind 表无 video)
    if (k === 'ink' || k === 'drawing') return 'drawing';
    if (k === 'region') return 'region';
    try { if (el && el.closest && el.closest('.vc-ink, [data-ink-region]')) return 'drawing'; } catch (e) {}
    return 'card';
  }

  function _pinRemember(el, label, text, cid, spec) {
    spec = spec || {};
    var id = _pinContextId(el, cid, spec);
    // ── 出向焦点(A5):选中/替换当前对象。**统一挂在这里** —— 卡片、图片、视频封面、
    //    绘图区都走 _pinRemember,接一处即可,不必在每种交互里各写一套(也就不会漏)。
    //    只带稳定 kind + 最小语义 + 引用,不塞正文/图本身(payload 要小)。
    try {
      if (window.RC && RC.outgoing) {
        RC.outgoing.focus(_outgoingKindOf(spec, el), {
          id: String(id || cid || label || '').slice(0, 120),
          cid: String(cid || '').slice(0, 80),
          label: String(label || '').slice(0, 80),
          brief: String(text || '').slice(0, 160)
        });
      }
    } catch (e) {}
    _pins.map[label] = String(text || '').slice(0, 2500);
    _pins.els[label] = el;
    _pins.ids[label] = id;
    if (cid) _pins.cids[cid] = label;
    try {
      var registry = _ctxSelectionRegistry();
      if (registry) {
        var record = {
          id: id,
          kind: spec.kind || (cid ? 'card' : 'context'),
          label: label,
          text: _pins.map[label],
          source: spec.source || (cid ? { cid: cid } : {}),
          meta: spec.meta || {}
        };
        if (Object.prototype.hasOwnProperty.call(spec, 'parentId')) record.parentId = spec.parentId || '';
        if (Object.prototype.hasOwnProperty.call(spec, 'covers')) record.covers = spec.covers || [];
        registry.select(record);
      }
    } catch (e) {}
    return id;
  }
  function _pinForget(label, cid, el) {
    label = String(label || '');
    var id = _pins.ids[label] || (el && el.dataset && el.dataset.vcContextId) || '';
    try { var registry = _ctxSelectionRegistry(); if (registry && id) registry.deselect(id); } catch (e) {}
    delete _pins.map[label]; delete _pins.els[label]; delete _pins.ids[label];
    if (cid && _pins.cids[cid] === label) delete _pins.cids[cid];
    Object.keys(_pins.cids).forEach(function (key) {
      if (_pins.cids[key] === label) delete _pins.cids[key];
    });
    // 取消选中 → 焦点必须**显式取消**,否则上游会拿着已取消的对象当现状
    // (A5 硬规则)。但这里原来无条件调用:钉着 3 张卡片时忘掉其中 1 张,
    // 会把 focus 整个清空,另外 2 张跟着从快照里消失——A5 规则要求的是
    // "取消的那个不能再被当成现状",不是"还有东西钉着也一起清空"。
    // 只在忘掉的是最后一张时才真的取消。
    if (Object.keys(_pins.map).length === 0) {
      try { if (window.RC && RC.outgoing) RC.outgoing.cancel(); } catch (e) {}
    }
    return id;
  }
  function _effectivePins(options) {
    options = options || {};
    try {
      var registry = _ctxSelectionRegistry();
      if (registry) return registry.toLegacy({
        limit: options.limit == null ? 8 : options.limit,
        maxText: options.maxText == null ? 2500 : options.maxText
      });
    } catch (e) {}
    var labels = Object.keys(_pins.map).sort();
    if (options.limit != null) labels = labels.slice(0, options.limit);
    var map = {}, items = [];
    labels.forEach(function (label) {
      var text = String(_pins.map[label] || '').slice(0, options.maxText == null ? 2500 : options.maxText);
      map[label] = text;
      items.push({ id: _pins.ids[label] || ('legacy:' + label), kind: 'context', label: label, text: text, parentId: '', covers: [], source: {}, meta: {} });
    });
    return { labels: labels, map: map, items: items, serialized: JSON.stringify({ contract: 'context-selection/legacy', items: items }) };
  }
  function _pinSync() {
    if (_pins.t) clearTimeout(_pins.t);
    _pins.t = setTimeout(function () {
      _pins.t = null;
      // 统一端口迁移(references/voice-context-injection.md #1):fp/通道判定归 voiceCtx(投递成功才前移,
      //   根治"通话外改 pin 推进 fp → 下通电话漏快照");文案在模块尾 register('pins') 的 text()
      var effective = _effectivePins({ limit: 8, maxText: 2500 });
      try { RC.voiceCtx && RC.voiceCtx.state('pins', { labels: effective.labels, map: effective.map, serialized: effective.serialized }); } catch (e) {}
    }, 1200);
  }
  try {
    var _contextRegistry0 = _ctxSelectionRegistry();
    if (_contextRegistry0 && _contextRegistry0.subscribe) {
      _contextRegistry0.subscribe(function () {
        _pinSync();
        try { _chipRender(); } catch (e) {}
      });
    }
  } catch (e) {}
  // 用户强调:同一编号(cid)的卡无论出现在字幕浮层 / 侧栏 / 收藏夹,选中一处则**处处高亮**,
  //   取消一处则**处处取消**。每个渲染实例出生时来登记,选中态按 cid 广播到全部实例。
  _pins.byCid = {};
  // 卡片尺寸是页面 placement 的设备级 presentation，不是卡实体本身的属性。
  // 同 cid 的页面副本可以共享，但侧栏/收藏/复习不读取也不应用；不进入服务器或跨设备同步。
  var _CARD_PRESENTATION_ID = 'page-card-presentation-v1:';
  var _cardSizes = Object.create(null);
  var _pageSizeByCid = Object.create(null);
  var _cardSizeDirty = Object.create(null);
  var _cardSizeLoads = Object.create(null);
  var _cardSizeWrite = Promise.resolve();
  var _cardResizeArmed = null;
  var _cardResizeActive = null;
  var _cardResizeOutsideBound = false;
  var _cardSizeMutationSeq = 0;
  var _cardSizeRuntimeReadyBound = false;
  function _cardSizeCid(value) {
    var cid = String(value == null ? '' : value);
    if (!cid || cid.length > 200 ||
        cid === '__proto__' || cid === 'prototype' || cid === 'constructor' ||
        /[\u0000-\u001f\u007f]/.test(cid)) return '';
    return cid;
  }
  function _cardSizeRecord(value) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
    var w = Number(value.w), h = Number(value.h), updatedAt = Number(value.updatedAt);
    if (!Number.isFinite(w) || !Number.isFinite(h) || !Number.isFinite(updatedAt)) return null;
    w = Math.round(w); h = Math.round(h); updatedAt = Math.max(0, Math.round(updatedAt));
    if (w < 180 || w > 720 || h < 100 || h > 720) return null;
    return { w: w, h: h, updatedAt: updatedAt };
  }
  function _cardSizeExtensionStore() {
    var store = window.__bwPageCardPresentation;
    return store && typeof store.get === 'function' &&
      typeof store.set === 'function'
      ? store : null;
  }
  function _cardSizePwaStore() {
    try {
      var pwa = window.BWReaderRuntime && BWReaderRuntime.pwaRuntime;
      var stores = pwa && typeof pwa.localStores === 'function' && pwa.localStores();
      var store = stores && stores.device;
      return store && typeof store.get === 'function' && typeof store.put === 'function'
        ? store : null;
    } catch (_) { return null; }
  }
  function _cardSizeApplyOne(el, record) {
    if (!el) return;
    if (!record) {
      el.classList.remove('vc-user-sized');
      el.style.removeProperty('--vc-user-w');
      el.style.removeProperty('--vc-user-h');
    } else {
      el.style.setProperty('--vc-user-w', record.w + 'px');
      el.style.setProperty('--vc-user-h', record.h + 'px');
      el.classList.add('vc-user-sized');
    }
    try {
      if (typeof el.__bwCardSizeApply === 'function') {
        el.__bwCardSizeApply(record ? {
          cid: (el.dataset && el.dataset.vcCid) || '',
          w: record.w, h: record.h, updatedAt: record.updatedAt
        } : null);
      }
    } catch (_) {}
  }
  function _cardSizePaint(cid, record) {
    var live = (_pageSizeByCid[cid] || []).filter(function (el) {
      return el && el.isConnected && el.__bwCardPageSizing === true;
    });
    _pageSizeByCid[cid] = live;
    live.forEach(function (el) { _cardSizeApplyOne(el, record); });
  }
  function _cardSizeAccept(cid, value, fromStorage) {
    cid = _cardSizeCid(cid);
    var record = _cardSizeRecord(value);
    if (!cid || !record || (fromStorage && _cardSizeDirty[cid])) return null;
    var current = _cardSizes[cid];
    if (!current || record.updatedAt >= current.updatedAt) {
      _cardSizes[cid] = record;
      _cardSizePaint(cid, record);
    }
    return _cardSizes[cid] || null;
  }
  function _cardSizeLoadExtension(cid) {
    var store = _cardSizeExtensionStore();
    if (!store) return Promise.resolve(null);
    return Promise.resolve(store.get(cid)).then(function (value) {
      return _cardSizeAccept(cid, value, true);
    }).catch(function () { return null; });
  }
  function _cardSizeLoadPwa(cid) {
    var store = _cardSizePwaStore();
    if (!store) {
      if (!_cardSizeRuntimeReadyBound && !_cardSizeExtensionStore()) {
        _cardSizeRuntimeReadyBound = true;
        document.addEventListener('bw:reader-runtime-ready', function () {
          _cardSizeRuntimeReadyBound = false;
          Object.keys(_pageSizeByCid || {}).forEach(function (key) {
            delete _cardSizeLoads[key];
            _cardSizeLoad(key);
          });
        }, { once: true });
      }
      return Promise.resolve(null);
    }
    return Promise.resolve(
      store.get('ui-session', _CARD_PRESENTATION_ID + cid, { includeDeleted: true })
    ).then(function (record) {
      if (!record || record.deleted) return null;
      return _cardSizeAccept(cid, record, true);
    }).catch(function () { return null; });
  }
  function _cardSizeLoad(cid) {
    cid = _cardSizeCid(cid);
    if (!cid) return Promise.resolve(null);
    if (_cardSizes[cid]) return Promise.resolve(_cardSizes[cid]);
    if (_cardSizeLoads[cid]) return _cardSizeLoads[cid];
    _cardSizeLoads[cid] = (_cardSizeExtensionStore()
      ? _cardSizeLoadExtension(cid)
      : _cardSizeLoadPwa(cid)).then(function (record) {
        delete _cardSizeLoads[cid];
        return record;
      }, function () {
        delete _cardSizeLoads[cid];
        return null;
      });
    return _cardSizeLoads[cid];
  }
  function _cardSizePageReg(el, cid) {
    cid = _cardSizeCid(cid);
    if (!el || !cid) return;
    el.__bwCardPageSizing = true;
    el.classList.add('vc-page-placement');
    var live = (_pageSizeByCid[cid] || []).filter(function (item) {
      return item && item.isConnected && item.__bwCardPageSizing === true;
    });
    if (live.indexOf(el) < 0) live.push(el);
    _pageSizeByCid[cid] = live;
    if (_cardSizes[cid]) _cardSizeApplyOne(el, _cardSizes[cid]);
    _cardSizeLoad(cid).then(function (record) {
      if (record && el.isConnected && el.__bwCardPageSizing === true) {
        _cardSizeApplyOne(el, record);
      }
    });
  }
  function _cardSizePersist(cid, record) {
    cid = _cardSizeCid(cid);
    record = _cardSizeRecord(record);
    if (!cid || !record) return Promise.reject(new Error('卡片尺寸无效'));
    _cardSizeDirty[cid] = true;
    var operation = _cardSizeWrite.catch(function () {}).then(function () {
      var extensionStore = _cardSizeExtensionStore();
      if (extensionStore) {
        return extensionStore.set(cid, {
          w: record.w, h: record.h, updatedAt: record.updatedAt
        });
      }
      var pwaStore = _cardSizePwaStore();
      if (!pwaStore) throw new Error('卡片尺寸本地仓库尚未就绪');
      var id = _CARD_PRESENTATION_ID + cid;
      return Promise.resolve(pwaStore.get('ui-session', id, { includeDeleted: true })).then(function (old) {
        return pwaStore.put('ui-session', {
          id: id, schema: 1, cid: cid, w: record.w, h: record.h, updatedAt: record.updatedAt
        }, {
          id: id,
          ifRev: Number(old && old.rev) || 0,
          mutationId: ['card-presentation-v1', Date.now(), ++_cardSizeMutationSeq].join(':')
        });
      });
    });
    _cardSizeWrite = operation;
    return operation.then(function () {
      delete _cardSizeDirty[cid];
      return record;
    }, function (error) {
      delete _cardSizeDirty[cid];
      throw error;
    });
  }
  function _cardPressEligible(target) {
    if (!target || !target.closest) return false;
    return !target.closest(
      '.vc-card-rs,.vc-card-x,button,a,input,textarea,select,' +
      '[contenteditable="true"],[role="button"],.fc-dot'
    );
  }
  function _cardResizeEventInside(event, el) {
    if (!event || !el) return false;
    try {
      var path = typeof event.composedPath === 'function'
        ? event.composedPath() : null;
      if (path && path.indexOf(el) >= 0) return true;
    } catch (_) {}
    var node = event.target;
    while (node) {
      if (node === el) return true;
      node = node.parentNode || node.host || null;
    }
    return false;
  }
  function _cardResizeDismissOutside(event) {
    var el = _cardResizeArmed;
    if (!el || _cardResizeActive) return;
    if (el.isConnected && _cardResizeEventInside(event, el)) return;
    el.classList.remove('vc-resize-armed');
    if (_cardResizeArmed === el) _cardResizeArmed = null;
  }
  function _cardResizeBindOutsideDismiss() {
    if (_cardResizeOutsideBound) return;
    _cardResizeOutsideBound = true;
    // pointerdown 让鼠标/触屏在落到卡外的第一时间收起；click 覆盖键盘等
    // 非 pointer 激活。监听器只清视觉状态，不截断宿主页原有事件。
    document.addEventListener('pointerdown', _cardResizeDismissOutside, true);
    document.addEventListener('click', _cardResizeDismissOutside, true);
  }
  function _cardResizeArm(el, cid) {
    if (!el || !cid) return;
    if (_cardResizeArmed && _cardResizeArmed !== el) {
      _cardResizeArmed.classList.remove('vc-resize-armed');
    }
    var already = el.classList.contains('vc-resize-armed');
    if (already) {
      el.classList.remove('vc-resize-armed');
      _cardResizeArmed = null;
      return;
    }
    if (_cardForm(el) !== 'full') {
      _cardForm(el, 'full');
      try { if (typeof el.__bwCardFormApply === 'function') el.__bwCardFormApply('full'); } catch (_) {}
    }
    el.classList.add('vc-resize-armed');
    _cardResizeArmed = el;
  }
  function _cardResizeBind(el, cid, pressTarget) {
    cid = _cardSizeCid(cid);
    if (!el || !cid || !pressTarget ||
        el.__bwCardPageSizing !== true) return null;
    _cardResizeBindOutsideDismiss();
    var previousBinding = el.__bwCardResizeBinding;
    if (previousBinding && previousBinding.pressTarget === pressTarget &&
        previousBinding.cid === cid) return previousBinding;
    if (previousBinding) {
      try { previousBinding.destroy(); } catch (_) {}
    }
    var handle = el.querySelector && el.querySelector('.vc-card-rs');
    if (!handle) {
      handle = document.createElement('button');
      handle.type = 'button';
      handle.className = 'vc-card-rs';
      handle.title = '拖动调整卡片大小';
      handle.setAttribute('aria-label', '调整卡片大小');
      el.appendChild(handle);
    }
    var taps = {
      count: 0, at: 0, x: 0, y: 0, pointerType: '', down: null,
      ignoreClickUntil: 0, suppressClickUntil: 0
    };
    function resetTapSequence() {
      taps.count = 0; taps.at = 0; taps.pointerType = ''; taps.down = null;
    }
    function countTap(x, y, pointerType) {
      var now = Date.now();
      pointerType = String(pointerType || 'legacy-click');
      var near = Math.hypot(x - taps.x, y - taps.y) <= 28;
      var samePointerType = !taps.pointerType ||
        taps.pointerType === pointerType;
      taps.count = now - taps.at <= 430 && near && samePointerType
        ? taps.count + 1 : 1;
      taps.at = now; taps.x = x; taps.y = y;
      taps.pointerType = pointerType;
      if (taps.count < 2) return false;
      taps.count = 0;
      taps.pointerType = '';
      _cardResizeArm(el, cid);
      // Chromium 会在 touch pointerup 后合成 click；只吞掉完成双点的第二个
      // click，第一点仍保持卡面原有点击语义。
      taps.suppressClickUntil = now + 650;
      return true;
    }
    function pointerCanTap(event) {
      if (!event || event.isPrimary === false) return false;
      var pointerType = String(event.pointerType || 'mouse');
      return pointerType !== 'mouse' ||
        event.button === undefined || event.button === 0;
    }
    function onPointerDown(event) {
      if (!_cardPressEligible(event.target) ||
          !pointerCanTap(event)) {
        resetTapSequence(); return;
      }
      // 上一次完成双点后的兼容 click 若已不可能先于这次新按下到达，就不能
      // 再误吞新手势自己的单击。
      taps.suppressClickUntil = 0;
      taps.down = {
        id: event.pointerId, x: event.clientX, y: event.clientY,
        at: Date.now(), moved: false,
        pointerType: String(event.pointerType || 'mouse')
      };
    }
    function onPointerMove(event) {
      var down = taps.down;
      if (!down || (down.id != null && event.pointerId !== down.id)) return;
      if (Math.hypot(event.clientX - down.x, event.clientY - down.y) > 12) {
        down.moved = true;
      }
    }
    function onPointerUp(event) {
      var down = taps.down;
      taps.down = null;
      if (!down || down.moved ||
          (down.id != null && event.pointerId !== down.id) ||
          event.isPrimary === false ||
          Date.now() - down.at > 260 ||
          !_cardPressEligible(event.target)) return;
      // 覆盖触摸/鼠标兼容 click 的最长常见延迟，避免一击被 pointerup +
      // synthetic click 重复计数。
      taps.ignoreClickUntil = Date.now() + 650;
      if (countTap(event.clientX, event.clientY, down.pointerType)) {
        event.preventDefault();
        event.stopPropagation();
      }
    }
    function onPointerCancel() {
      resetTapSequence();
    }
    function onPointerLeave(event) {
      var down = taps.down;
      if (!down ||
          (down.id != null && event.pointerId !== down.id)) return;
      // 仅中止尚未完成的这一点。触摸指针在 pointerup 后会自然派发
      // pointerleave；此时 down 已清空，必须保留第一点，第二点才能成立。
      resetTapSequence();
    }
    function onClick(event) {
      var now = Date.now();
      if (now < taps.suppressClickUntil) {
        taps.suppressClickUntil = 0;
        event.preventDefault();
        event.stopImmediatePropagation();
        return;
      }
      if (now < taps.ignoreClickUntil || !_cardPressEligible(event.target)) {
        return;
      }
      if (countTap(
        Number(event.clientX) || 0,
        Number(event.clientY) || 0,
        'legacy-click'
      )) {
        event.preventDefault();
        event.stopImmediatePropagation();
      }
    }
    function onHandleDown(event) {
      if (_cardResizeActive ||
          !pointerCanTap(event)) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      try {
        if (typeof el.__bwCancelPinHold === 'function') {
          el.__bwCancelPinHold();
        }
      } catch (_) {}
      var rect = el.getBoundingClientRect();
      var before = _cardSizes[cid]
        ? Object.assign({}, _cardSizes[cid])
        : null;
      var session = {
        el: el, cid: cid, handle: handle, pointerId: event.pointerId,
        sx: event.clientX, sy: event.clientY,
        w0: Math.max(180, Math.min(720, Math.round(rect.width))),
        h0: Math.max(100, Math.min(720, Math.round(rect.height))),
        before: before, next: before, raf: 0
      };
      _cardResizeActive = session;
      el.classList.add('vc-resizing');
      try { handle.setPointerCapture(event.pointerId); } catch (_) {}
      function paint() {
        session.raf = 0;
        if (_cardResizeActive !== session || !session.next) return;
        _cardSizes[cid] = session.next;
        _cardSizePaint(cid, session.next);
      }
      function move(ev) {
        if (_cardResizeActive !== session ||
            (session.pointerId != null &&
             ev.pointerId !== session.pointerId)) return;
        ev.preventDefault();
        session.next = {
          w: Math.max(180, Math.min(
            720,
            Math.round(session.w0 + ev.clientX - session.sx)
          )),
          h: Math.max(100, Math.min(
            720,
            Math.round(session.h0 + ev.clientY - session.sy)
          )),
          updatedAt: Date.now()
        };
        if (!session.raf) session.raf = requestAnimationFrame(paint);
      }
      function finish(ev, cancelled) {
        if (_cardResizeActive !== session ||
            (session.pointerId != null &&
             ev.pointerId !== session.pointerId)) return;
        _cardResizeActive = null;
        if (session.raf) {
          cancelAnimationFrame(session.raf);
          session.raf = 0;
        }
        handle.removeEventListener('pointermove', move);
        handle.removeEventListener('pointerup', up);
        handle.removeEventListener('pointercancel', cancel);
        el.classList.remove('vc-resizing');
        try { handle.releasePointerCapture(session.pointerId); } catch (_) {}
        if (cancelled) {
          if (session.before) _cardSizes[cid] = session.before;
          else delete _cardSizes[cid];
          _cardSizePaint(cid, session.before);
          return;
        }
        var record = _cardSizeRecord(session.next || {
          w: session.w0, h: session.h0, updatedAt: Date.now()
        });
        if (!record) return;
        _cardSizes[cid] = record;
        _cardSizePaint(cid, record);
        _cardSizePersist(cid, record).catch(function () {
          try {
            RC.toast && RC.toast('卡片大小已调整，但本地保存失败');
          } catch (_) {}
        });
      }
      function up(ev) { finish(ev, false); }
      function cancel(ev) { finish(ev, true); }
      handle.addEventListener('pointermove', move);
      handle.addEventListener('pointerup', up);
      handle.addEventListener('pointercancel', cancel);
    }
    pressTarget.addEventListener('pointerdown', onPointerDown, true);
    pressTarget.addEventListener('pointermove', onPointerMove, true);
    pressTarget.addEventListener('pointerup', onPointerUp, true);
    pressTarget.addEventListener('pointercancel', onPointerCancel, true);
    pressTarget.addEventListener('pointerleave', onPointerLeave, true);
    pressTarget.addEventListener('click', onClick, true);
    handle.addEventListener('pointerdown', onHandleDown);
    var binding = {
      cid: cid,
      handle: handle,
      pressTarget: pressTarget,
      destroy: function () {
        resetTapSequence();
        if (_cardResizeArmed === el) {
          el.classList.remove('vc-resize-armed');
          _cardResizeArmed = null;
        }
        pressTarget.removeEventListener('pointerdown', onPointerDown, true);
        pressTarget.removeEventListener('pointermove', onPointerMove, true);
        pressTarget.removeEventListener('pointerup', onPointerUp, true);
        pressTarget.removeEventListener(
          'pointercancel',
          onPointerCancel,
          true
        );
        pressTarget.removeEventListener(
          'pointerleave',
          onPointerLeave,
          true
        );
        pressTarget.removeEventListener('click', onClick, true);
        handle.removeEventListener('pointerdown', onHandleDown);
        if (el.__bwCardResizeBinding === binding) {
          el.__bwCardResizeBinding = null;
        }
      }
    };
    el.__bwCardResizeBinding = binding;
    return binding;
  }
  function _pinReg(el, cid) {
    if (!el || !cid) return;
    el.dataset.vcCid = cid;
    var a = (_pins.byCid[cid] = (_pins.byCid[cid] || []).filter(function (x) { return x.isConnected; }));
    if (a.indexOf(el) < 0) a.push(el);
    if (_pins.cids[cid] && _pins.map[_pins.cids[cid]]) el.classList.add('vc-picked');   // 已选中 → 新实例出生即高亮
  }
  function _pinPaint(cid, on) {
    (_pins.byCid[cid] || []).filter(function (x) { return x.isConnected; }).forEach(function (x) {
      x.classList.toggle('vc-picked', !!on);
      x.classList.add('vc-pin-pop'); setTimeout(function () { x.classList.remove('vc-pin-pop'); }, 420);
    });
  }
  function _pinToggle(el, label, textFn, spec) {
    spec = spec || {};
    var on = !el.classList.contains('vc-picked');
    var cid = (el.dataset && el.dataset.vcCid) || '';
    if (on) {
      if (cid && _pins.cids[cid] && _pins.map[_pins.cids[cid]]) {   // 95:同编号的卡已在上下文=同一张卡的另一实例,不重复注入——只把这个实例也点亮
        el.dataset.pinLabel = _pins.cids[cid];
        _pinPaint(cid, true);
        return;
      }
      var lb = label, i = 2;
      while (_pins.map[lb]) lb = label + '·' + (i++);   // label 唯一化(**不同**编号的同名卡,如两次天气卡,不互相顶掉)
      el.dataset.pinLabel = lb;
      _pinRemember(el, lb, String(textFn()).slice(0, 2500), cid, spec);   // 79:长按=全文入脑；Registry 负责父子包含去重
    } else {
      var lb0 = (cid && _pins.cids[cid]) || el.dataset.pinLabel || label;
      _pinForget(lb0, cid, el);
    }
    el.classList.toggle('vc-picked', on);
    el.classList.add('vc-pin-pop'); setTimeout(function () { el.classList.remove('vc-pin-pop'); }, 420);
    if (cid) _pinPaint(cid, on);   // 同号卡:处处高亮 / 处处取消(用户强调)
    _pinSync(); _chipRender();
  }
  function _pinBind(el, label, textFn, spec, pressTarget) {   // 72:正文长按 600ms=选中；owner 仍是整卡，pressTarget 只负责手势
    if (!el) return null;
    pressTarget = pressTarget || el;
    el.classList.add('vc-pinnable');
    var _bindCid = (el.dataset && el.dataset.vcCid) || '';
    spec = spec || {};
    // turn 整体与内部工具/结果卡的 DOM 已有稳定 data-turn；在调用方未显式声明时自动补 parentId。
    // 这里只补结构元数据，不改变手势，也不把选择状态写入 system prompt / 工具定义。
    try {
      var _turnHost = el.closest && el.closest('[data-turn]');
      if (_turnHost && _turnHost !== el && !spec.parentId) {
        var _tid0 = _turnHost.getAttribute('data-turn');
        if (_tid0) {
          var _specCopy = {};
          Object.keys(spec).forEach(function (key) { _specCopy[key] = spec[key]; });
          _specCopy.parentId = 'turn:' + _tid0;
          spec = _specCopy;
        }
      }
    } catch (e) {}
    _pinContextId(el, _bindCid, spec || {});   // DOM 实例出生即绑定稳定语义 id；选择时不临时重编号
    // mountAll / 状态重绘会对同一个 owner+正文重复登记。只更新最新 payload，
    // 绝不能叠加 pointer/click 监听，否则一次长按会切换两次又回到原状态。
    var pinBindings = el.__bwPinHoldBindings;
    if (!pinBindings) {
      pinBindings = [];
      try { Object.defineProperty(el, '__bwPinHoldBindings', { value: pinBindings, configurable: true }); }
      catch (e) { el.__bwPinHoldBindings = pinBindings; }
    }
    for (var pbi = 0; pbi < pinBindings.length; pbi++) {
      if (pinBindings[pbi].pressTarget === pressTarget) {
        pinBindings[pbi].label = label;
        pinBindings[pbi].textFn = textFn;
        pinBindings[pbi].spec = spec;
        // 页面尺寸手势与长按必须使用同一块正文命中面；重绘复用 binding
        // 时也补齐，避免先 mount、后注册 placement 的调用顺序留下漏绑。
        if (el.__bwCardPageSizing === true) {
          _cardResizeBind(el, _bindCid, pressTarget);
        }
        return pinBindings[pbi];
      }
    }
    // 一个 owner 只能有一个实际长按面。调用方从旧的“整卡”迁到第 5 参数
    // “正文”时，若留下整卡监听，正文 pointerdown 会同时冒泡到两套 600ms
    // timer，最终选中后立刻又取消。先完整拆掉旧 target 再绑定新 target。
    pinBindings.slice().forEach(function (oldBinding) {
      try { oldBinding.destroy(); } catch (e) {}
    });
    var binding = { owner: el, pressTarget: pressTarget, label: label, textFn: textFn, spec: spec };
    pinBindings.push(binding);
    var lpT = null, lpX = 0, lpY = 0, lpId = null;
    var suppressClickUntil = 0;
    var destroyed = false;
    function _cancelOnePinHold() {
      if (lpT) { clearTimeout(lpT); lpT = null; }
      lpId = null;
    }
    function _cancelPinHold() {
      (el.__bwPinHoldBindings || []).forEach(function (x) {
        try { x.cancel(); } catch (e) {}
      });
    }
    binding.cancel = _cancelOnePinHold;
    // charged drag 在 420ms 进入 ready 时显式取消正文长按；公开 owner 级取消器，
    // 即使未来一个 owner 有多个独立 pressTarget，也会一次清完。
    el.__bwCancelPinHold = _cancelPinHold;
    function _fire() {
      lpT = null;
      suppressClickUntil = Date.now() + 650;
      // 97(用户设计):不再限通话中——文字模式同样可长按带入(chips 照常出输入框上方,send 时随 ctx.pinned 走文字管线)
      _pinToggle(el, binding.label, binding.textFn, binding.spec);   // owner 决定视觉/身份；重复 mount 只更新最新文本与 spec
    }
    function _pointerDown(ev) {
      // 按钮/链接/编辑控件拥有自己的长按与点击语义；不能因为用户按住
      // “显示答案/评分/改进”而把整张卡误加入上下文。
      // 双击/触屏双点调整大小复用同一个 predicate，所以两种手势的可用范围
      // 完全一致；Anki 正反面正文可用，评分键和分页圆点不可用。
      if (!_cardPressEligible(ev.target)) return;
      if ((ev.button !== undefined && ev.button !== 0) || lpId !== null) return;
      lpId = ev.pointerId;
      lpX = ev.clientX; lpY = ev.clientY;
      if (lpT) { clearTimeout(lpT); lpT = null; }
      lpT = setTimeout(_fire, 600);
    }
    function _pointerMove(ev) {
      if (lpId !== null && ev.pointerId !== lpId) return;
      if (lpT && (Math.abs(ev.clientX - lpX) + Math.abs(ev.clientY - lpY)) > 14) _cancelOnePinHold();   // 拖动=取消长按(75:8px 手指按住必抖过=长按永远触发不了的根因,放宽)
    }
    function _click(ev) {
      if (Date.now() >= suppressClickUntil) return;
      suppressClickUntil = 0;
      ev.preventDefault();
      ev.stopImmediatePropagation();
    }   // 成功长按后的浏览器合成 click 只吞一次；短点不受影响
    function _contextMenu(ev) { ev.preventDefault(); }   // 75:iOS 长按系统菜单会抢手势
    function _pointerEnd(ev) {
      if (lpId !== null && ev.pointerId != null && ev.pointerId !== lpId) return;
      _cancelOnePinHold();
    }
    binding.destroy = function () {
      if (destroyed) return;
      destroyed = true;
      _cancelOnePinHold();
      pressTarget.removeEventListener('pointerdown', _pointerDown);
      pressTarget.removeEventListener('pointermove', _pointerMove);
      pressTarget.removeEventListener('click', _click, true);
      pressTarget.removeEventListener('contextmenu', _contextMenu);
      ['pointerup', 'pointercancel', 'pointerleave'].forEach(function (evn) {
        pressTarget.removeEventListener(evn, _pointerEnd);
      });
      var at = pinBindings.indexOf(binding);
      if (at >= 0) pinBindings.splice(at, 1);
    };
    pressTarget.addEventListener('pointerdown', _pointerDown);
    pressTarget.addEventListener('pointermove', _pointerMove);
    pressTarget.addEventListener('click', _click, true);
    pressTarget.addEventListener('contextmenu', _contextMenu);
    ['pointerup', 'pointercancel', 'pointerleave'].forEach(function (evn) {
      pressTarget.addEventListener(evn, _pointerEnd);
    });
    if (el.__bwCardPageSizing === true) {
      _cardResizeBind(el, _bindCid, pressTarget);
    }
    return binding;
  }
  function _imgGoneNote(it) {   // ✕删除通告 → 统一端口 event(append-only 不改历史保缓存;800ms 合并;
    //   无通话=pending 环留底,通话建立/文字 send 补投——根治"dc 没开时删图信息永久丢失+死环零消费")
    if (!it) return;
    try { RC.voiceCtx && RC.voiceCtx.event('removed_imgs', { aid: it.aid || '', title: it.title || '' }, { mergeMs: 800 }); } catch (e) {}
  }
  function _igWire(root, card) {   // 88/98:图卡+视频卡交互——✕移除;点封面=只选中这一张(带入上下文,再点取消);视频▶=播放
    if (!card || (card.kind !== 'images' && card.kind !== 'videos')) return;
    // 地图项就地升级成可拖可缩的活地图(用户 2026-08-26:卡片内直接能动,
    // 全屏另给按钮)。静态图仍是基底,升级失败就退回看得见的图。
    try { _upgradeMapCells(root); } catch (e) {}
    if (card.kind === 'videos') root.addEventListener('error', function (ev) {
      var img = ev.target;
      if (!img || !img.classList || !img.classList.contains('vc-ig-img')) return;
      img.style.display = 'none';
      var wrap = img.closest && img.closest('.vc-vg-wrap');
      if (wrap && !wrap.querySelector('.vc-vg-empty')) {
        var empty = document.createElement('div'); empty.className = 'vc-vg-empty'; empty.textContent = '封面加载失败';
        wrap.insertBefore(empty, wrap.querySelector('.vc-vg-play'));
      }
    }, true);
    root.addEventListener('click', function (ev) {
      var pb = ev.target.closest && ev.target.closest('.vc-vg-play');
      if (pb) {   // 98:播放钮=原播放行为(浮动播放器/新窗),不参与选中
        ev.preventDefault();
        ev.stopPropagation();
        var ip = +pb.getAttribute('data-i');
        var vt = ((card.data || {}).items || [])[ip] || {};
        _openVideoRef(_videoButtonRef(pb), vt.title || '');
      }
    });
    root.addEventListener('click', function (ev) {
      var mb = ev.target.closest && ev.target.closest('.vc-ig-map');
      if (mb) {
        ev.preventDefault();
        ev.stopPropagation();
        var im = +mb.getAttribute('data-i');
        var mit = ((card.data || {}).items || [])[im] || {};
        var mcell = mb.closest && mb.closest('.vc-ig-cell');
        var mmeta = _mapMetaFromUrl(mit.url);
        // 全屏接着卡内**当前**的视角走：用户在卡里拖到了别处再点全屏，
        // 却跳回初始位置，是最容易让人以为"点错了"的那种不连贯。
        try {
          var liveState = mcell && mcell.__bwMapView && mcell.__bwMapView.state;
          if (mmeta && liveState) {
            mmeta = {
              lat: liveState.lat, lon: liveState.lon, zoom: liveState.z,
              marks: (mcell.__bwMapMeta || mmeta).marks
            };
          }
        } catch (e) {}
        if (mmeta) _openMapViewer(mmeta, mit.title || card.title || '地图');
        return;
      }
      var x = ev.target.closest('.vc-ig-x');
      if (x) {
        ev.stopPropagation();
        var i0 = +x.getAttribute('data-i');
        var cell = x.closest('.vc-ig-cell');
        if (cell) cell.remove();
        try { (card.data.items || [])[i0]._gone = 1; } catch (e) {}
        try { _imgGoneNote((card.data.items || [])[i0]); } catch (e) {}   // 删除通告(append-only 保缓存)
        return;
      }
      var img = ev.target.closest('.vc-ig-img');
      if (img && img.closest('.vc-ig-cell.vc-map-ready')) return;   // 活地图上的点击=操作地图,不是选中
      if (img) {
        ev.stopPropagation();
        var i1 = +img.getAttribute('data-i');
        var it = ((card.data || {}).items || [])[i1] || {};
        var cell1 = img.closest('.vc-ig-cell');
        var on = !cell1.classList.contains('vc-picked');
        root.querySelectorAll('.vc-ig-cell.vc-picked').forEach(function (c2) {   // 单选:先清其它
          c2.classList.remove('vc-picked');
          var lb2 = c2.dataset.pinLabel, gc2 = c2.dataset.vcCid;
          if (lb2) _pinForget(lb2, gc2, c2);
        });
        if (on) {
          var gcid = (card.cid || '') + '#' + i1;   // 95:图编号=卡号#序号(浮层/侧栏两实例互斥)
          if (_pins.cids[gcid] && _pins.map[_pins.cids[gcid]]) {
            try { if (typeof _toast === 'function') _toast('这张图已在上下文中'); } catch (e) {}
            return;
          }
          cell1.classList.add('vc-picked');
          var lb0 = (it.title || (card.kind === 'videos' ? '视频' : '配图')) + (card.kind === 'videos' ? '·视频' : '·图') + (i1 + 1);
          var lb = lb0, lbn = 2; while (_pins.map[lb]) lb = lb0 + '·' + (lbn++);
          cell1.dataset.pinLabel = lb; cell1.dataset.vcCid = gcid;
          _pinRemember(cell1, lb,
            ((it.title || '') + (it.channel ? '(' + it.channel + ')' : '') + ' ' + (it.url || '')).slice(0, 500),
            gcid, {
              id: 'card:' + (card.cid || '') + '/item:' + i1,
              kind: card.kind === 'videos' ? 'video-item' : 'image-item',
              parentId: 'card:' + (card.cid || ''),
              source: { cid: card.cid || '', item: i1 }
            });
        }
        _pinSync(); _chipRender();
      }
    });
  }
  function _infoCardEl(card) {   // 87:构一张侧栏信息卡(实时与历史回放共用——刷新后卡永远还是卡)
    injectCss();   // 131:历史回放/侧栏先出卡时通话 UI 可能还没建过 → 样式没注入,标题栏按钮就成了裸 <button>(白块)
    if (!card.cid) card.cid = _mkCid();   // 95:历史旧卡(落库时还没 cid 字段)补发——本次会话内该实例稳定
    var label = card.title || '搜索结果';
    // 统一壳(用户拍板 2026-07-21):侧栏结果卡也走 _renderInflow=同一 _cardDom → 与浮层/钉入卡同长相+三态圆点
    //   (原手建 .vc-if + vc-if-min 两态折叠退役)。_infoHtml 进 bd;主题色/图标按 kind 取(与浮层 1330 同源)。
    var _ck = { images: 'image', videos: 'video', weather: 'weather', news: 'news' }[card.kind] || 'text';
    var _cst = {}; try { _cst = (window.RC && RC.toolChip && RC.toolChip.styleOf) ? RC.toolChip.styleOf(_ck) : {}; } catch (e) {}
    var d = _renderInflow(null, { text: _infoHtml(card), label: label, isHtml: true, type: _cst.color, icon: _cst.icon, form: 'full', cid: card.cid }).el;
    _pinBind(d, label, function () { return _infoText(card); });
    try { _dragToDock(d, function () { return { label: label, kind: card.kind, raw: '<div class="vc-if-hd"><span>' + esc(label) + '</span></div>' + _infoHtml(card), isHtml: true, text: _infoText(card), cid: card.cid }; }); } catch (e) {}   // cid 跟随副本；#img:raw 调用时动态生成
    try { _igWire(d, card); } catch (e) {}   // 88:图卡交互(✕/单选)
    try { d.__vcCard = card; } catch (e) {}   // #img:挂 card 数据对象——拖图入卡时反查,插入落 data.items(持久,非 DOM-only)
    return d;
  }
  window.__vcInfoCardEl = function (card) { try { return (card && card.kind) ? _infoCardEl(card) : null; } catch (e) { return null; } };
  // 「任务完成预制语音」(设备级开关,用户设计):工具/任务做完念一句固定提示音(如"搜索完成")。
  //   ⚠ 与「工具完成后口头回报」互斥——那个开着时 AI 自己会拿结果说话,预制音再响就是**两个声音同时发**。
  //     所以只要口头回报是开的,这里**自动禁用**(不管开关怎么设)。
  function _cueOn() {
    try { if (localStorage.getItem('rc-voice-toolreply') === '1') return false; } catch (e) {}   // 口头回报开 → 预制音让路
    try { return localStorage.getItem('rc-voice-cue') !== '0'; } catch (e) { return true; }
  }
  function _cue(text) {
    if (!_cueOn()) return;
    if (!(_rtc.on && (_voiceMode() !== 'stt' || _ttsOn()))) return;
    try { _speakSafe(text); } catch (e) {}
  }
  window.__vcCue = _cue;   // 供任务完成播报复用
  // 书页字符锚补绑成功后要关掉浮层那份（否则同一内容两处并存）。
  window.__vcCardClose = function (c) { try { _cardClose(c); } catch (e) {} };
  // 绑不上的卡先挂这里，等那一页真的渲染出来再补绑。
  //   最常见的失败恰恰是"那页还没渲染"（用户在别处翻着，AI 把卡绑到第 12 页），
  //   而这种失败是**会自己好的** —— 只要那页出现。以前退回浮层就到此为止，
  //   球落在随机位置、既不记得想去哪也永不重试。
  var _bindPending = [];
  window.__upBindRetry = function (upageId) {
    if (!upageId || !_bindPending.length) return;
    var rest = [];
    _bindPending.forEach(function (item) {
      if (String(item.upage) !== String(upageId)) { rest.push(item); return; }
      var ok = false;
      try {
        ok = (typeof window.__upBindCard === 'function')
          && window.__upBindCard(item.upage, item.bid, item.payload);
      } catch (e) {}
      if (!ok) { rest.push(item); return; }
      // 绑上了就把浮层那份关掉 —— 否则同一内容两处并存，用户会以为出了两张卡。
      try { if (item.card) _cardClose(item.card); } catch (e) {}
    });
    _bindPending = rest;
  };

  function _renderInfoResult(rendered, outcome, reason) {
    return {
      rendered: rendered === true,
      bindOutcome: outcome || 'none',
      bindReason: reason || null
    };
  }

  /// 词锚卡沿用已经确认过的四类脚注色，而不是工具运行状态色：
  /// 文字/背景/辨析=紫，考点/出题=蓝，配图/视频=绿，数值=黄。
  /// `kind` 是结构卡协议字段；旧卡没有单独 category，所以标题只作为兼容判定。
  function _bindTone(kind, label) {
    kind = String(kind || '').toLowerCase();
    label = String(label || '');
    if (kind === 'images' || kind === 'image' || kind === 'videos' || kind === 'video' ||
        /配图|图片|图像|视频/.test(label)) return '#34d399';
    if (kind === 'fact' || kind === 'qa' ||
        /考点|出题|问答|问题|练习|测试|题目/.test(label)) return '#7dd3fc';
    if (kind === 'weather' || kind === 'numeric' || kind === 'number' ||
        /数值|数据|统计|温度|百分比/.test(label)) return '#fbbf24';
    return '#b9a8ff';
  }

  async function renderInfo(card, options) {
    if (!card || !card.kind) return _renderInfoResult(false);
    options = options || {};
    var _bindOutcome = null;
    var label = card.title || '搜索结果';
    var _pendBind = null;   // 绑不上时记下"它想去哪"，浮层卡建出来后一起入列
    var _pendPageBind = null;   // 同上，书页字符锚那条
    // ★ card.bind：把这张卡钉到自建页的某个格子块（协议见 reader_card_contract._norm_bind）。
    //   钉进去之后它就是那一页的一个 block —— 位置和"内容序列上的位置"是同一件事，
    //   AI 下次读这一页时它按顺序出现在被绑定的块之后，而不是浮在旁边的游离注解。
    //   ⚠ 绑不上（那页没渲染 / 目标块已删）就**退回浮层**，绝不把卡丢掉：
    //     位置信息没了还能补，内容没了就真没了。
    try {
      var _b = card.bind;
      if (_b && _b.kind === 'upage-block' && typeof window.__upBindCard === 'function') {
        var _bp = {
          isHtml: true,
          raw: '<div class="vc-if-hd"><span>' + esc(label) + '</span></div>' + _infoHtml(card),
          text: _infoText(card),
          label: label
        };
        var okBind = window.__upBindCard(_b.upage, _b.bid, _bp);
        if (okBind) return _renderInfoResult(true, 'bound');
        _bindOutcome = { outcome: 'floating', reason: 'upage-not-open' };
        // 记下"它想去哪"。那页出现时 __upBindRetry 会把它接回去。
        _pendBind = { upage: _b.upage, bid: _b.bid, payload: _bp, card: null };
        try { RC.toast && RC.toast('那一页还没打开，卡片先放浮层，等页面出现会自己归位'); } catch (e2) {}
      }
      // 书页正文的字符锚（C15 第二版）。跟 upage-block 同一套处置：钉上了就不再
      //   出浮层；钉不上退回浮层并记下想去哪，那页渲染出来时自己归位。
      if (_b && _b.kind === 'page-chars') {
        var _pp = {
          isHtml: true,
          raw: _infoHtml(card),
          text: _infoText(card),
          label: label,
          // 标记按**这张卡的身份**去重，不按它落在哪个词。用区间当身份的话，
          // 同一个词上的第二张卡会把第一张的标记抹掉 —— 而 AI 钉的卡没有
          // 宿主便签兜着，抹掉就是内容彻底失联。
          uid: card.cid || options.uid || '',
          category: card.kind || 'general',
          // 标记、浮标和展开卡共用脚注分类色；kind 是主判据，标题只兼容旧卡。
          tone: _bindTone(card.kind, label)
        };
        // `__pageBindPersist` 只有在 document-notes 权威仓提交并完成本地投影后
        // 才 resolve ok:true。这里绝不先画临时框再谎报 bound。
        var _pr = null;
        if (typeof window.__pageBindPersist === 'function') {
          try { _pr = await window.__pageBindPersist(_b, _pp); }
          catch (persistError) {
            _pr = {
              ok: false,
              why: String((persistError && (persistError.code || persistError.message)) ||
                'persist-failed').slice(0, 120)
            };
          }
        } else _pr = { ok: false, why: 'persistence-unavailable' };
        if (_pr && _pr.ok === true) return _renderInfoResult(true, 'bound');
        // durable replay 的第一次失败已经生成过回退卡并登记补绑。后续只重试
        // 权威 placement：再次走下面的 turnCard / 浮层分支会让同一 correlation
        // 每重放一次就多出一张卡。rendered:true 表示首轮回退视图仍是已应用结果。
        if (options.bindOnly === true) {
          return _renderInfoResult(
            true,
            'floating',
            (_pr && _pr.why) || 'unknown'
          );
        }
        _bindOutcome = {
          outcome: 'floating',
          reason: (_pr && _pr.why) || 'unknown'
        };
        _pendPageBind = { bind: _b, payload: _pp };
        try { RC.toast && RC.toast('那一页还没渲染，卡片先放浮层，翻到时会自己归位'); } catch (e2) {}
      }
    } catch (e) {
      if (card.bind) {
        _bindOutcome = {
          outcome: 'floating',
          reason: String((e && (e.code || e.message)) || 'bind-failed').slice(0, 120)
        };
      }
    }
    var th = document.getElementById('asst-thread');
    var _hosts = [];
    // 141(轮次容器):结果卡 = 本轮容器里的一个 **card part**。前置语是它前面的 text part,天然同框,
    //   不需要"搬";也不再需要 absorb 认领 —— 工具细节统一收在容器的【流程】按钮里。
    var _tcOk = false;
    try {
      if (RC.turnCard && window.__asstVoiceTid) {
        var _tcPart = RC.turnCard.addPart(
          window.__asstVoiceTid(),
          { kind: 'card', card: card }
        );
        _tcOk = !!(_tcPart && _tcPart.isConnected);
      }
    } catch (e) {}
    if (th && !_tcOk) { var d = _infoCardEl(card); th.appendChild(d); th.scrollTop = th.scrollHeight; if (d.isConnected) _hosts.push(d); }
    if (!_sideOpen()) {
      // ⚠ 浮层镜像**不要再套一层 vc-if-hd**:_cardPush 自己就有卡头(标题+按钮)——套了就是两条标题栏(用户实测)
      // 132(用户):结果卡(天气/图/视频/新闻)也要有**同一套三态** —— 标记 / 长条 / 方块,单击循环。
      //   以"方块"出生(它就是用户要看的内容),但标记/头部点击可收成长条、再收成标记。
      var _ck = { images: 'image', videos: 'video', weather: 'weather', news: 'news' }[card.kind] || 'text';
      var _cst = {};
      try { _cst = (window.RC && RC.toolChip && RC.toolChip.styleOf) ? RC.toolChip.styleOf(_ck) : {}; } catch (e) {}
      // 带 bind 的卡即使**绑不上**（那页没渲染 / 目标块已删）也不能到点就没：
      //   绑定这件事说明它是钉在某处的一次记录，退回浮层已经丢了位置，
      //   再让它自己消失就把内容也一起丢了。收成球留着，用户还能找回来。
      var c = _cardPush(_infoHtml(card), label, true, false, card.cid,
                        { dot: true, form: 'full', type: _cst.color, icon: _cst.icon,
                          keepAsDot: !!card.bind });
      if (c && _pendBind) { _pendBind.card = c; _bindPending.push(_pendBind); _pendBind = null; }
      if (c && _pendPageBind && window.__pageBindDefer) {
        try { window.__pageBindDefer(_pendPageBind.bind, _pendPageBind.payload, c); } catch (e2) {}
        _pendPageBind = null;
      }
      if (c) {
        c.el.classList.add('vc-typed');   // 有色磨砂(与工具卡同一套观感)
        try { c.el.__vcCard = card; } catch (e) {}   // #img:浮层实例同挂 card(与侧栏实例共享同一对象 → 拖图入卡 push 一次两处同现)
        // 结果卡不是"文字回复" → 标题栏只留【数据流】按钮(▶/✕ 去掉,与侧栏一致)
        ['.vc-card-p', '.vc-card-x'].forEach(function (q) { var b = c.el.querySelector(q); if (b) b.remove(); });
        _pinBind(c.el, label, function () { return _infoText(card); });
        try { _igWire(c.el, card); } catch (e) {}
        if (c.el.isConnected) _hosts.push(c.el);
      }
    }
    // 用户设计:这类工具**本来就有自己的结果卡** → 别再让工具指示器另造一张(字幕模式曾一次弹两张)。
    //   把工具卡「吸收」进结果卡:唯一显示的是结果卡,标题栏多一个按钮,点开就是那条线性流程图。
    // 141:容器在 → 上面已注入 card part,_hosts 为空,absorb 自然 no-op(它对空数组直接 return)。
    //   只有容器不可用(极端兜底)才会走到旧的"结果卡吸收工具卡"路径。
    try { if (_hosts.length && window.RC && RC.toolChip && RC.toolChip.absorb) RC.toolChip.absorb(_hosts); } catch (e) {}
    // 141(轮次容器):卡已经作为 card part 进了本轮容器,轮次结束时随 parts 一起落库。
    //   ⚠ **绝不能再单独落一条 card** —— 那样历史里同一张卡存两份(parts 一份 + m.card 一份),
    //   回放时容器渲一遍、旧路径又渲一遍 = **重复内容**(用户实测图3)。
    //   只有容器不可用(极端兜底)才回落旧的独立落库。
    if (!_tcOk) {
      try {
        fetch('/api/assistant/log', { method: 'POST', headers: { 'Content-Type': 'application/json' }, keepalive: true,
          body: JSON.stringify({ assistant: '', card: card, via: 'voice', file: _rtc.ctxFile || '', page: _rtc.ctxPage || 0 }) }).catch(function () {});
      } catch (e) {}
    }
    // ⚠ 补绑登记不能只在「浮层那条路」里做。上面 __pageBindDefer 的调用在
    //   `if (!_sideOpen())` 分支内部 —— 侧栏开着时卡片进侧栏、不建浮层，于是
    //   那句 toast 承诺的「翻到时会自己归位」根本没登记，永远不会归位。
    //   而失败提示是**无条件**弹的，所以用户听到的是一句空头支票。
    //   这里补上：没浮层就不传 card（__pageBindDefer 第三参只用于成功后关掉
    //   浮层镜像，为 null 时那步自然跳过）。
    if (_pendPageBind && window.__pageBindDefer) {
      try { window.__pageBindDefer(_pendPageBind.bind, _pendPageBind.payload, null); } catch (e2) {}
      _pendPageBind = null;
    }
    var _rendered = _tcOk || _hosts.length > 0;
    if (_rendered) {
      _cue('搜索完成');   // 75:仅在卡片实际出现后播放确认音；拒绝回执不能伪装成功
    }
    return _renderInfoResult(
      _rendered,
      _bindOutcome && _bindOutcome.outcome,
      _bindOutcome && _bindOutcome.reason
    );
  }
  function renderImgs(imgs) {   // 88:图片结果升格为结构化卡(kind:'images')走 renderInfo 全管线——对话流+浮层同款、落库、回放、✕/单选/溯源
    if (!imgs || !imgs.length) return;
    return renderInfo({
      kind: 'images', title: '配图 × ' + imgs.length,
      brief: imgs.map(function (im) { return im.title || im.concept || '图'; }).join('、').slice(0, 120),
      data: { items: imgs.filter(function (im) { return im && im.image_url; }).map(function (im) {
        return { url: im.image_url, aid: im.id || '', title: im.title || im.concept || '', page: im.page_url || '',
                 src: im.source || '', q: im.matched_query || '' };   // aid=资产编号(拖出贴页用内链→贴页触发本地化)
      }) }
    });
  }
  function renderVids(vids, meta) {   // 98(用户设计):视频升格结构卡(kind:'videos')走 renderInfo 全管线——对话流+浮层同款、✕/单选/播放、落库回放、溯源
    if (!vids || !vids.length) return;
    return renderInfo({
      kind: 'videos', title: '相关视频 × ' + vids.length,
      brief: vids.map(function (v) { return v.title || ''; }).filter(Boolean).slice(0, 4).join('、').slice(0, 120),
      data: { q: (meta && meta.q) || '', items: vids.map(function (v) {
        var bili = (v.src === 'bili' || /^BV[0-9A-Za-z]{10}/.test(v.id || ''));
        return { id: v.id || '', title: v.title || '', channel: v.channel || '', thumb: v.thumb || '',
                 src: bili ? 'bili' : 'yt',
                 url: (bili ? 'https://www.bilibili.com/video/' : 'https://www.youtube.com/watch?v=') + encodeURIComponent(v.id || '') };
      }) }
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
      .replace(/[(（]\s*(?:第\s*\d+\s*[-~至]?\s*\d*\s*页|p\.?\s*\d+)\s*[)）]/gi, '')   // 页码引用"(第10页)"念出来很吵:显示保留(可点击跳转),朗读剪掉
      .replace(/\s+/g, ' ').trim();
  }
  function speak(text) {
    var t = cleanForSpeech(text);
    if (!t) return;
    if (_nativeAgent.active) {
      _nativeAgentPost({ action: 'speak', text: t, mood: vt.mood || '' });
      return;
    }
    var w = (ws && mode === 'agent' && ws.readyState === 1) ? ws : _tts.ws;   // agent 通话在→走它;否则朗读专用通道
    if (w && w.readyState === 1) { try { w.send(JSON.stringify({ type: 'speak', text: t, id: ++vt.sid, mood: vt.mood || '' })); } catch (e) {} }
  }
  function bargeIn() {   // 打断:清本地播放队列 + 作废 relay 侧排队/在流的合成(两条通道都发)
    try { _sq.length = 0; if (_sqT) { clearInterval(_sqT); _sqT = null; } } catch (e) {}   // 67b:排队待念的也作废
    stopPlayback(); _ttsStopPlay(); capClear();
    if (_nativeAgentEngaged()) _nativeAgentPost({ action: 'cancel' });
    try { if (ws && mode === 'agent' && ws.readyState === 1) ws.send(JSON.stringify({ type: 'cancel' })); } catch (e) {}
    try { if (_tts.ws && _tts.ws.readyState === 1) _tts.ws.send(JSON.stringify({ type: 'cancel' })); } catch (e) {}
  }
  // ── 朗读字幕(v3-⑳,用户设计):侧栏关着时把正在念的句子显示在屏幕下方(上一句半透明小字、当前句清晰),
  //    日语/错字等"念不出所以然"的内容能看见;工具/agent 执行时兼作状态显示。
  //    同步机制:relay 在每句音频前发 tts_seg JSON 帧(uni 2.0 引擎串行合成,帧紧贴该句首块)→ 前端把句子
  //    绑到该块的播放调度时刻(playT),字幕跟声音精确同步;bidi/moon 音频不分句,退化为略超前。
  //    开关只在语音设置卡(localStorage rc-voice-sub,默认开);pointer-events:none 不挡任何触控。──
  function capOn() { try { return localStorage.getItem('rc-voice-sub') !== '0'; } catch (e) { return true; } }
  var _cap = { el: null, prev: null, cur: null, wait: null, st: null, curWho: null, pend: [], bind: false, timers: [], hideT: null };
  function _capEl() {
    if (_cap.el && _cap.el.parentNode) return _cap.el;
    injectCss();   // 纯朗读场景(通话浮层没建过)也要样式
    var d = document.createElement('div'); d.id = 'vc-cap';
    d.innerHTML = '<div class="vc-cap-line vc-cap-prev" style="display:none"></div>' +
                  '<div class="vc-cap-line vc-cap-cur" style="display:none"></div>' +
                  '<div class="vc-cap-line vc-cap-wait" style="display:none">' +
                    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10v1a7 7 0 0 0 14 0v-1"/><path d="M12 18v4"/></svg>' +
                    '<i></i><i></i><i></i></div>' +
                  '<div class="vc-cap-line vc-cap-st" style="display:none"></div>';
    document.body.appendChild(d);
    _cap.el = d; _cap.prev = d.children[0]; _cap.cur = d.children[1]; _cap.wait = d.children[2]; _cap.st = d.children[3];
    return d;
  }
  function _capVisible() {   // 显示条件:开关开 + 语音链路活跃(朗读亮/通话中/Apple 听写中) + 侧栏关着(开着有对话流,字幕多余)
    if (!capOn() || !(speakOn() || ws || _cap.dictating)) return false;
    try { if (window.RC && RC.sidedrawer && RC.sidedrawer.isOpen()) return false; } catch (e) {}
    return true;
  }
  function capShow(text, who) {   // who:'a'=AI(默认)/'u'=用户(ASR 转写,iMessage 蓝);当前句下移成"上一句"
    if (!_capVisible()) return;
    _capEl();
    if (_cap.hideT) { clearTimeout(_cap.hideT); _cap.hideT = null; }
    var old = _cap.cur.textContent;
    if (old) {
      _cap.prev.textContent = old; _cap.prev.style.display = '';
      _cap.prev.classList.toggle('vc-cap-u', _cap.curWho === 'u');
    }
    _cap.cur.textContent = text; _cap.cur.style.display = '';
    _cap.cur.classList.toggle('vc-cap-u', who === 'u');
    _cap.curWho = who || 'a';
    capWait(false); _capPlace();
    _cap.el.classList.add('on');
  }
  // ── ASR 假转写判据(与 voice_realtime_relay.py 的 _ASR_GHOST_ANCHORS/_ASR_PROMPT_MIRROR/
  //    _GHOST_LCS_MIN 逐字对应;改一处必须改另一处,有测试守)──
  var VC_ASR_ANCHORS = ['学习伴读通话'];
  var VC_ASR_MIRROR = '关键词:Anki、笔迹、振假名、生词、假名'
                    + '|学习伴读通话。常说:这一页/这页讲了什么/上一页/下一页/翻到第N页/读一下/'
                    + '做卡片/记笔记/生词/翻译/解释/公式/我画的/笔迹';
  var VC_GHOST_LCS_MIN = 10;   // 「翻到第N页」才5字、「下一页」3字 → 10 字才不会误杀真人
  var VC_GHOST_COV_MIN_LEN = 10;   // 复读变体判据:去标点后至少这么长才启用(短句不判,防误杀)
  var VC_GHOST_COV_RATIO = 0.8;    // 累计匹配块占比≥此=复读变体(同音错字/漏字打断连续子串→LCS 判不出;与 relay _GHOST_COV_* 同步)说的话
  function _stripPunct(x) { return (x || '').replace(/[\s,，。、:：;；/·!！?？…\-]+/g, ''); }
  function _lcsLen(a, b) {   // 最长公共**子串**(连续),与 Python difflib find_longest_match 同义
    if (!a || !b) return 0;
    var prev = new Array(b.length + 1).fill(0), best = 0;
    for (var i = 1; i <= a.length; i++) {
      var cur = new Array(b.length + 1).fill(0);
      for (var j = 1; j <= b.length; j++) {
        if (a[i - 1] === b[j - 1]) { cur[j] = prev[j - 1] + 1; if (cur[j] > best) best = cur[j]; }
      }
      prev = cur;
    }
    return best;
  }
  function _cumMatch(a, b) {   // 累计匹配块字符数,对应 Python difflib get_matching_blocks(Ratcliff-Obershelp 递归);判'复读变体'
    if (!a || !b) return 0;
    function lm(alo, ahi, blo, bhi) {
      var best = { a: alo, b: blo, size: 0 }, j2 = {};
      for (var i = alo; i < ahi; i++) {
        var nj = {};
        for (var j = blo; j < bhi; j++) {
          if (a[i] === b[j]) { var k = (j > blo ? (j2[j - 1] || 0) : 0) + 1; nj[j] = k; if (k > best.size) best = { a: i - k + 1, b: j - k + 1, size: k }; }
        }
        j2 = nj;
      }
      return best;
    }
    var total = 0, st = [[0, a.length, 0, b.length]];
    while (st.length) {
      var g = st.pop(), m = lm(g[0], g[1], g[2], g[3]);
      if (m.size > 0) {
        total += m.size;
        if (g[0] < m.a && g[2] < m.b) st.push([g[0], m.a, g[2], m.b]);
        if (m.a + m.size < g[1] && m.b + m.size < g[3]) st.push([m.a + m.size, g[1], m.b + m.size, g[3]]);
      }
    }
    return total;
  }
  function _isAsrGhost(tx) {
    var s = (tx || '').trim();
    if (!s) return true;
    for (var i = 0; i < VC_ASR_ANCHORS.length; i++) if (s.indexOf(VC_ASR_ANCHORS[i]) >= 0) return true;
    var a1 = _stripPunct(s), a2 = _stripPunct(VC_ASR_MIRROR);
    if (_lcsLen(a1, a2) >= VC_GHOST_LCS_MIN) return true;
    if (a1.length >= VC_GHOST_COV_MIN_LEN && _cumMatch(a1, a2) >= VC_GHOST_COV_RATIO * a1.length) return true;   // 复读变体:同音错字/漏字打断LCS但整体重合(实测'笔记'版 0.94)
    return false;
  }
  try { window.__vcIsAsrGhost = _isAsrGhost; } catch (e) {}   // 供测试/调试

  function capUser(text) {   // ASR interim 实时更新:当前句已是用户句→原地改字,否则新起一句
    if (!text || !_capVisible()) return;
    _capEl();
    if (_cap.curWho === 'u') {
      if (_cap.hideT) { clearTimeout(_cap.hideT); _cap.hideT = null; }
      _cap.cur.textContent = text; _cap.cur.style.display = '';
      capWait(false); _capPlace(); _cap.el.classList.add('on');
      return;
    }
    capShow(text, 'u');
  }
  function capStream(who, full) {   // S2S:整轮累积文本 → 尾句(可能残)进 cur、倒数第二句进 prev
    if (!full || !_capVisible()) return;   // (S2S 音频不分句,退化为文本驱动,略超前于声音)
    _capEl();
    if (_cap.hideT) { clearTimeout(_cap.hideT); _cap.hideT = null; }
    var re = /[^。！？!?;；\n]+[。！？!?;；\n]*/g, parts = [], m;
    while ((m = re.exec(full))) { if (m[0].trim()) parts.push(m[0].trim()); }
    if (!parts.length) return;
    if (parts.length > 1) {
      _cap.prev.textContent = parts[parts.length - 2];
      _cap.prev.style.display = ''; _cap.prev.classList.remove('vc-cap-u');
    }
    _cap.cur.textContent = parts[parts.length - 1]; _cap.cur.style.display = '';
    _cap.cur.classList.toggle('vc-cap-u', who === 'u'); _cap.curWho = who;
    capWait(false); _capPlace(); _cap.el.classList.add('on');
  }
  function _capPlace() {   // S2S 通话浮层在时:字幕抬到浮层上方(iPhone 上浮层近全宽,不避让会被盖住)
    if (!_cap.el) return;
    var b = 0;
    try {
      // 107(用户实测"收起侧栏开语音字幕消失"根因):通话条内嵌在侧栏里时(vc-inline),侧栏收起=rect 为 0/屏外,
      // 旧算式 b=屏高+10 把字幕抬出屏幕顶端。避让只在浮层**真实可见**时生效。
      if (box && box.classList.contains('on')) {
        var r0 = box.getBoundingClientRect();
        if (r0.height > 0 && r0.top > 40 && r0.top < window.innerHeight) b = window.innerHeight - r0.top + 10;
      }
    } catch (e) {}
    _cap.el.style.bottom = b > 0 ? (b + 'px') : '';
  }
  function capWait(on) {   // "正在听"等待指示(mic+三点跳动):ASR 通话空闲时亮
    if (!_cap.el && !on) return;
    if (on) {
      if (!_capVisible()) return;
      _capEl(); _cap.wait.style.display = 'flex'; _capPlace(); _cap.el.classList.add('on');
    } else if (_cap.wait) _cap.wait.style.display = 'none';
  }
  var _capStT = null;
  function capStatus(text) {   // 状态行(工具执行/完成):text=null 清除;字符串或 {html, cls, hold}(69)
    if (_capStT) { clearTimeout(_capStT); _capStT = null; }
    if (text == null) { if (_cap.st) { _cap.st.style.display = 'none'; _cap.st.className = 'vc-cap-line vc-cap-st'; } _capMaybeHide(1200); return; }
    if (typeof text === 'object') {
      if (!_capVisible()) return;   // 与字符串路径同 gate(侧栏开=对话流可见,状态行不重复)
      _capEl();
      if (_cap.hideT) { clearTimeout(_cap.hideT); _cap.hideT = null; }
      _cap.st.innerHTML = text.html || '';
      _cap.st.className = 'vc-cap-line vc-cap-st' + (text.cls ? ' vc-st-' + text.cls : '');
      _cap.st.style.display = 'flex';
      _cap.el.classList.add('on');
      if (text.hold) _capStT = setTimeout(function () { capStatus(null); }, text.hold);
      return;
    }
    if (!_capVisible()) return;
    _capEl();
    if (_cap.hideT) { clearTimeout(_cap.hideT); _cap.hideT = null; }
    _cap.st.textContent = text; _cap.st.className = 'vc-cap-line vc-cap-st'; _cap.st.style.display = '';
    _cap.el.classList.add('on');
  }
  function _capMaybeHide(delay) {   // 播完停留几秒再淡出(还在播/状态行亮着→再等);ASR 通话继续→句子清掉回"正在听"
    if (!_cap.el) return;
    if (_cap.hideT) clearTimeout(_cap.hideT);
    _cap.hideT = setTimeout(function () {
      _cap.hideT = null;
      if (_tts.playing.length || playing.length || (_cap.st && _cap.st.style.display !== 'none')) { _capMaybeHide(1500); return; }
      _cap.cur.textContent = ''; _cap.prev.textContent = ''; _cap.curWho = null;
      _cap.cur.style.display = 'none'; _cap.prev.style.display = 'none';
      _cap.cur.classList.remove('vc-cap-u'); _cap.prev.classList.remove('vc-cap-u');
      if (ws && _capVisible()) { capWait(true); return; }   // 通话还在(ASR/S2S 都在听):等待指示常驻
      _cap.el.classList.remove('on');
    }, delay || 4000);
  }
  function capClear() {   // 打断/挂断:清句队列+定时器,立即隐藏;通话还在 → 直接回"正在听"
    _cap.pend = []; _cap.bind = false; _cap.curWho = null;
    _cap.gen = (_cap.gen || 0) + 1;   // 世代:作废还没到点的 capShow 定时器(打断后 straggler 音频块不许把旧句闪回来)
    _cap.timers.forEach(function (t) { clearTimeout(t); }); _cap.timers = [];
    if (_cap.hideT) { clearTimeout(_cap.hideT); _cap.hideT = null; }
    if (_cap.el) _cap.el.classList.remove('on');
    if (_cap.cur) { _cap.cur.textContent = ''; _cap.cur.style.display = 'none'; _cap.cur.classList.remove('vc-cap-u'); }
    if (_cap.prev) { _cap.prev.textContent = ''; _cap.prev.style.display = 'none'; _cap.prev.classList.remove('vc-cap-u'); }
    if (_cap.wait) _cap.wait.style.display = 'none';
    if (_cap.st) _cap.st.style.display = 'none';
    if (ws) capWait(true);   // 打断(通话中)≠挂断:麦还开着,等待指示立即回位
  }
  function _capSeg(text) { if (text) { _cap.pend.push(text); _cap.bind = true; _cap.sawSeg = 1; } }   // 收到 tts_seg 帧:下一个音频块=该句开头(sawSeg:精确路活着)
  function _capBindChunk(startAt, acx) {   // 音频块调度好了:若它是句首块,到点亮字幕
    if (!_cap.bind || !_cap.pend.length) return;
    _cap.bind = false;
    var txt = _cap.pend.shift(), g0 = _cap.gen || 0;
    var ms = Math.max(0, (startAt - acx.currentTime) * 1000);
    _cap.timers.push(setTimeout(function () { if ((_cap.gen || 0) === g0) capShow(txt); }, ms));
  }
  window.__vcCapStatus = capStatus;   // rc-assistant 的 tool 事件也走字幕状态行(侧栏关着时能看到 agent 在干嘛)
  window.__vcCapUser = function (text) { _cap.dictating = true; capUser(text); };        // Apple 听写转写上字幕(侧栏关时)
  window.__vcCapDictEnd = function () { _cap.dictating = false; _capMaybeHide(4000); };  // 听写结束:几秒后淡出
  window.__vcTtsBusy = function () {   // 朗读是否还在响/还有句没播(听写暂停-恢复的依据)
    return !!(_tts.playing.length || _cap.pend.length);
  };
  window.__vcDictAudioOff = function () {   // 听写发送前:关掉朗读 AudioContext——它若建于听写(录音会话)期间,路由粘扬声器;
    try { if (_tts.ac) { _tts.ac.close(); _tts.ac = null; _ttsStopPlay(); } } catch (e) {}   // 麦释放后 tap 重建即拿干净 playback 会话(耳机)
    _audioSession('playback');
  };
  // 侧栏开闭 ↔ 字幕联动(㉔,用户设计):开侧栏字幕立即消失(对话流可见,不重复);
  // 关侧栏且语音功能活跃(通话/朗读/听写)→ 字幕/等待指示自动回来。
  (function _capSideHook(n) {
    var side = document.getElementById('ep-side');
    if (!side) { if (n < 30) setTimeout(function () { _capSideHook(n + 1); }, 900); return; }
    new MutationObserver(function () {
      var open = side.classList.contains('open');
      if (open) capClear();   // capClear 内的"回等待"经 capWait→_capVisible gate(侧栏开)自然不亮
      else if (ws || speakOn() || _cap.dictating) capWait(true);
      try { _cardsVisSync(); } catch (e) {}   // 77:浮层卡随侧栏开合隐/现,选中项变输入框上方 chip
    }).observe(side, { attributes: true, attributeFilter: ['class'] });
  })(0);

  // ── 朗读专用通道(?mode=tts,v3-⑬):没在语音通话时点亮「🔊 朗读」→ 回答经双向流式 TTS 播——
  //    不开麦、不连 ASR;独立 ws + 独立 AudioContext(与通话互不干扰)。点亮开关(手势内)预热。──
  var _tts = { ws: null, ac: null, playT: 0, playing: [] };
  // iOS 音频路由(用户实测:听写+朗读时戴耳机却走扬声器):页面用过麦克风后 WebKit 会话粘在
  // "play-and-record" 类别,该类别**默认强制扬声器**。Safari 17+ 的 Audio Session API 可声明意图:
  // 纯播放(朗读)= 'playback' → 正常走耳机;开麦通话= 'play-and-record'。挂断后切回 playback。
  function _audioSession(kind) {
    try { if (navigator.audioSession) navigator.audioSession.type = kind; } catch (e) {}
  }
  // ⚠ 不在页面加载时全局设 'playback':该类别会**静音麦克风采集**(getUserMedia 拿到无声流,
  // S2S/ASR 全聋=豆包连 450 都不发,用户实测"说什么都没反应"的根因)。playback 只在朗读通道
  // (_ttsEnsure,gate !ws)和挂断后(teardown)声明——耳机路由该管的场景都覆盖,不碰开麦链路。
  var _connecting = false;   // 通话建立中(start 已声明 play-and-record、ws 还没赋值):此窗口谁也不许改回 playback,否则开出来的麦是静音的
  function _ttsEnsure() {
    if (!ws && !_connecting) _audioSession('playback');   // 没在通话/没在建:确保纯播放类别(耳机路由)
    try {
      if (!_tts.ac) { try { _tts.ac = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 }); } catch (e) { _tts.ac = new (window.AudioContext || window.webkitAudioContext)(); } }   // 24k 定频:消逐 chunk 独立重采样的边界 click(官方 console 同款)
      if (_tts.ac.state !== 'running') _tts.ac.resume();
      if (_ttsIrqWant() && !_taec.ready) { try { _ttsAecSetup(_tts.ac); } catch (e) {} }   // 131:只在「可打断代念」条件成立时建
    } catch (e) {}
    if (_tts.ws && _tts.ws.readyState <= 1) return;
    try {
      var w = _openWs('/voice-rt?mode=tts');
      w.binaryType = 'arraybuffer';
      w.onmessage = function (ev) {
        if (ev.data instanceof ArrayBuffer) { _ttsPlay(ev.data); return; }
        try {
          var j = JSON.parse(ev.data);
          if (j.event === 'tts_seg') _capSeg(j.payload && j.payload.text);        // 字幕:句边界帧
          else if (j.event === 'tts_end') _capMaybeHide(2500);
        } catch (e) {}
      };
      w.onclose = function () { if (_tts.ws === w) _tts.ws = null; };
      _tts.ws = w;
    } catch (e) {}
  }
  function _ttsPlay(buf) {
    var a = _tts.ac; if (!a) return;
    var i16 = new Int16Array(buf), f32 = new Float32Array(i16.length);
    for (var i = 0; i < i16.length; i++) f32[i] = i16[i] / 32768;
    var ab = a.createBuffer(1, f32.length, 24000);
    ab.copyToChannel(f32, 0);
    var src = a.createBufferSource(); src.buffer = ab;
    src.connect((_taec.ready && _taec.ctx === a) ? _taec.dest : a.destination);   // 130:环回在 → 代念进 AEC 参考(不必禁麦)
    if (_tts.tap) { try { src.connect(_tts.tap); } catch (e) {} }   // 66:录制抽头(历史灰钮=念+存)
    var t = Math.max(a.currentTime + 0.02, _tts.playT);
    src.start(t); _tts.playT = t + ab.duration; _tts.playing.push(src);
    _capBindChunk(t, a);   // 句首块 → 字幕在该块真正开播的时刻亮
    var tg = document.querySelector('.vc-speak-tg'); if (tg) tg.classList.add('speaking');
    src.onended = function () {
      var k = _tts.playing.indexOf(src); if (k >= 0) _tts.playing.splice(k, 1);
      if (!_tts.playing.length) { var g = document.querySelector('.vc-speak-tg'); if (g) g.classList.remove('speaking'); }
    };
  }
  window.__vcTtsCapture = function (text) {   // 66:历史灰钮——朗读通道现场念这条,同时录 WebAudio 输出;resolve(blob|null)
    return new Promise(function (resolve) {
      try {
        _ttsEnsure();
        setTimeout(function () {
          var a = _tts.ac;
          if (!a || !window.MediaRecorder) { try { speak(text); } catch (e) {} resolve(null); return; }
          var mime = _recMime();
          if (!mime) { try { speak(text); } catch (e) {} resolve(null); return; }
          var dest = a.createMediaStreamDestination();
          _tts.tap = dest;
          var mr, chunks = [];
          try { mr = new MediaRecorder(dest.stream, { mimeType: mime }); } catch (e) { _tts.tap = null; try { speak(text); } catch (e2) {} resolve(null); return; }
          mr.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };
          mr.onstop = function () { _tts.tap = null; resolve(chunks.length ? new Blob(chunks, { type: mime }) : null); };
          mr.start(1000);
          try { _ttsMicGuard(); } catch (e) {}   // 通话中念历史:照旧禁麦防模型听见
          try { speak(text); } catch (e) {}
          var t0 = Date.now(), saw = false, quiet = 0;
          var iv = setInterval(function () {
            var playing = false; try { playing = _tts.playing.length > 0; } catch (e) {}
            if (playing) { saw = true; quiet = 0; } else if (saw) quiet++;
            if ((saw && quiet >= 4) || (!saw && Date.now() - t0 > 9000) || Date.now() - t0 > 180000) {
              clearInterval(iv);
              try { mr.stop(); } catch (e) { _tts.tap = null; resolve(null); }
            }
          }, 350);
        }, 350);
      } catch (e) { resolve(null); }
    });
  };
  function _ttsStopPlay() {
    _tts.playing.forEach(function (s) { try { s.stop(); } catch (e) {} });
    _tts.playing = []; _tts.playT = 0;
    var tg = document.querySelector('.vc-speak-tg'); if (tg) tg.classList.remove('speaking');
  }
  function _ttsShutdown() {
    try { if (_tts.ws) { if (_tts.ws.readyState === 1) _tts.ws.send(JSON.stringify({ type: 'cancel' })); _tts.ws.close(); } } catch (e) {}
    _tts.ws = null; _ttsStopPlay(); capClear();
    var p = null;
    try { if (_tts.ac) p = _tts.ac.close(); } catch (e) {}
    _tts.ac = null;
    return p;   // close 的 promise:开麦前要 await 它落地,否则旧 playback 会话还激活着,类别切不动
  }
  // 助手流式回答 tap(rc-assistant 在 answer 增量/收尾时调):按标点小片段即时喂 TTS
  //   (双向流式 session:relay 侧一轮回答一个 session,片段连续合成 → 韵律连贯,首句无需等全文)
  function speakOn() { try { return localStorage.getItem('rc-voice-speak') === '1'; } catch (e) { return false; } }
  // S2S 通话中的出声开关(独立键,默认亮):灭=丢音频只看对话窗字幕。⚠ S2S 双流恒计费(协议无输出模态
  // 开关,已全查证),灭省的是听觉干扰不是钱;真文本对话用 mic 长按的 ASR 模式(零豆包输出音频费)。
  // ㊿b 四态循环(用户设计,GPT rtc 下每档都是真后台差异):audio=全音频 / mixed=直接提问用音频·工具结果与
  // 深度思考用文字 / text=全文字(输出音频费归零) / tts=文字回复+豆包朗读通道代念(比 Realtime 音频便宜)。
  // 豆包 S2S 引擎映射:audio|mixed=播,text|tts=丢音频(它协议无模态开关,不省钱只静音)。
  // 61 四态(用户定稿):sts 纯语音 / stt 纯文字(2048 随便写,无路由) / half 混合(提问=语音·工具/深度=文字) /
  // route 智能路由(语音短答 + 长内容模型自调 route_to_text 转服务端文本模型写全文);
  // TTS 退出模式行列,改**独立通用开关**(_ttsOn):任何模式的文字输出都流式切句代念(尽快开口)
  var _VM_SEQ = ['sts', 'stt', 'half', 'route'];
  // 65 Apple 化:SF 风线条 SVG(currentColor 跟随亮灭态)+短中文标签;_VM_TXT=状态行用纯文字
  var _VMI = {
    sts: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M2 6.5v3M5 4v8M8 2.5v11M11 4v8M14 6.5v3"/></svg>',
    stt: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round" stroke-linecap="round"><path d="M2.5 3.5h11v7.2h-6L4.7 13v-2.3H2.5z"/><path d="M5 6.2h6M5 8.4h4"/></svg>',
    half: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"><path d="M2 6.5v3M4.7 4.5v7M7.4 6v4"/><path d="M10.5 5.6h3.5M10.5 8h3.5M10.5 10.4h2.4"/></svg>',
    route: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M2 8h3.2C8 8 8 4.5 10.8 4.5H13M5.2 8C8 8 8 11.5 10.8 11.5H13"/><path d="M11.4 2.8L13.2 4.5l-1.8 1.7M11.4 9.8l1.8 1.7-1.8 1.7"/></svg>',
    spk: '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6.5v3h2L8.5 12V4L5 6.5z"/><path d="M10.5 6a3 3 0 0 1 0 4M12.5 4.3a5.5 5.5 0 0 1 0 7.4"/></svg>'
  };
  var _VM_LABEL = { sts: _VMI.sts + '<span>语音</span>', stt: _VMI.stt + '<span>文字</span>',
                    half: _VMI.half + '<span>混合</span>', route: _VMI.route + '<span>路由</span>' };
  var _VM_TXT = { sts: '语音', stt: '文字', half: '混合', route: '路由' };
  var _VM_OLD = { audio: 'sts', mixed: 'half', text: 'stt', tts: 'stt' };   // 旧值映射(存量配置兼容)
  function _voiceMode() {
    var v = 'sts'; try { v = localStorage.getItem('rc-voice-mode-s2s') || 'sts'; } catch (e) {}
    return _VM_OLD[v] || (_VM_SEQ.indexOf(v) >= 0 ? v : 'sts');
  }
  function _ttsOn() { try { return localStorage.getItem('rc-voice-tts') === '1'; } catch (e) { return false; } }
  function s2sSpeakOn() { return _voiceMode() !== 'stt'; }   // 豆包引擎映射:stt=静音丢音频,其余=播
  // 句片里的语气标签流解析(v3-⑱c 转折):[语气:XX] 之后的文字都按新语气,直到下一个标签——
  // 标签前的残句先按旧 mood 念,然后切换。标签内约定无标点(prompt),所以不会被句边界撕裂。
  function _speakSeg(seg) {
    var re = /[\[【]语气[::]\s*([^\]】]{1,12})[\]】]/g, last = 0, mm;
    while ((mm = re.exec(seg))) {
      var before = seg.slice(last, mm.index);
      if (before.trim()) speak(before);
      vt.mood = mm[1].trim();               // 情绪转折点:此后句子换新语气
      last = re.lastIndex;
    }
    var rest = seg.slice(last);
    if (rest.trim()) speak(rest);
  }
  window.__asstVoiceTap = function (full, done) {
    // 回答结束与 TTS 开关无关：先释放本轮终稿指纹，再把真正不同的排队问题
    // 放到下一任务发送（此刻 rc-assistant 还没把 streaming 置回 false）。
    if (done) {
      activeUtter = '';
      if (pendingUtter) {
        var queuedUtter = pendingUtter; pendingUtter = null;
        setTimeout(function () { sendToAssistant(queuedUtter, true); }, 0);
      }
    }
    if (!speakOn()) return;                 // 「🔊 朗读」没点亮=零 TTS 成本(读比听快,用户拍板默认关)
    if (ws && mode === 's2s') return;       // S2S 通话:豆包自己出声,朗读开关不适用
    if (!(ws && mode === 'agent') && !_nativeAgent.active) _ttsEnsure();   // 没开 ASR 通话(纯打字/听写提问)→ lazy 朗读专用通道
    full = String(full || '');
    // 轮次替换判定(编排器每个工具轮 answer 从头重来):full 不再是已消费前缀的延伸 → 念完上轮残句,从新文本头接着念。
    // ⚠ 不 bargeIn:轮间过渡(开场白→工具→正式回答)要连贯念完;真正的打断只在新提问(sendToAssistant)。
    //   旧版按 full 变短判"新回答"并打断,工具轮一到就掐掉刚念的开场白从头重念 → "念几个字→停→从头再读"的结巴。
    if (!full.startsWith(vt.pref)) {
      if (vt.tail.trim()) _speakSeg(vt.tail.replace(/[\[【]语气?[::]?[^\]】]{0,12}$/, ''));   // 上轮残句(常无尾标点)念完
      vt.sent = 0; vt.tail = '';
    }
    vt.pref = full;
    vt.tail += full.slice(vt.sent); vt.sent = full.length;
    var re = /[^。！？!?;；,，\n]+[。！？!?;；,，\n]+/g, m, consumed = 0;   // 逗号级边界:更早开始出声
    while ((m = re.exec(vt.tail))) { _speakSeg(m[0]); consumed = re.lastIndex; }
    vt.tail = vt.tail.slice(consumed);
    if (done) {
      if (vt.tail.trim()) _speakSeg(vt.tail.replace(/[\[【]语气?[::]?[^\]】]{0,12}$/, ''));
      vt.sent = 0; vt.tail = ''; vt.pref = '';
      try {
        if (_nativeAgent.active) _nativeAgentPost({ action: 'speak_done' });
        else {
          var wd = (ws && mode === 'agent' && ws.readyState === 1) ? ws : _tts.ws;
          if (wd && wd.readyState === 1) wd.send(JSON.stringify({ type: 'speak_done' }));   // FinishSession:让尾巴合成完
        }
      } catch (e) {}
    }
  };
  window.__asstVoiceOn = function () { return speakOn() && !(ws && mode === 's2s'); };   // 朗读亮且非 S2S 通话 → 后端回答用『适合朗读』风格
  function sendToAssistant(text, keepAudio) {
    text = String(text || '').trim();
    if (!text) return;
    if (!keepAudio) bargeIn();        // 新问题:停掉还在念的旧回答(排队派发除外)
    vt.sent = 0; vt.tail = ''; vt.pref = ''; vt.mood = null;
    var utterKey = _utterKey(text);
    if (window.__asstBusy && window.__asstBusy()) {
      // iOS/relay 偶尔会把同一个 final utterance 再交付一次。它不是用户的新问题，
      // 不应在当前回答结束后被放大成第二个完整回合。
      if (utterKey && (utterKey === activeUtter || utterKey === _utterKey(pendingUtter))) return;
      pendingUtter = text; taPlaceholder('⏳ 上一条还在答,已排队:' + text.slice(0, 14) + '…'); return;
    }
    activeUtter = utterKey;
    taPlaceholder('🎙 说话即可,松口自动发送…');
    if (window.__asstSend) window.__asstSend(text);
    else threadMsg('asst-note', '⚠ 助手未加载,请刷新页面');
  }
  function handleAgentMsg(m) {
    var p = m.payload || {};
    if (m.event === 'agent_ready') { taSet(''); taPlaceholder('🎙 说话即可,松口自动发送…'); capWait(true); return; }
    if (m.event === 'asr') {   // 进行中转写 → 写输入框 + 字幕用户句实时上屏;用户开口 → 立即打断播报
      if (playing.length) bargeIn();
      taSet(p.text || '');
      if (p.text) capUser(p.text);
      return;
    }
    if (m.event === 'utterance' && p.text) {
      taSet(''); sendToAssistant(p.text); capUser(p.text);   // capUser 在 send 之后:send 内 bargeIn 会清字幕,定稿句要重建
      _capMaybeHide(10000);   // 兜底:朗读灭+纯文本回答时没有任何后续字幕事件,10s 后淡出回"正在听"(朗读亮时 capShow 会 clear 掉这个)
      return;
    }
    if (m.event === 'tts_seg') { _capSeg(p.text); return; }         // 字幕:句边界帧(agent 通话朗读)
    if (m.event === 'tts_end') { _capMaybeHide(2500); return; }
    if (m.event === -1 || p.error) threadMsg('asst-note', '⚠ 语音:' + (p.error || '').slice(0, 80));
  }

  // ── GPT Realtime WebRTC 直连(㉚,用户拍板):媒体直连 OpenAI——WebRTC 路径浏览器 AEC **真正生效**
  //    (外放无回声+全双工随时插话,不再需要半双工妥协)。密钥不下发(SDP 经 /rtc-call 后端代理);
  //    工具循环在本地(dc 收 function_call → fetch /voice-tool → dc 回填),client_action 直接执行;
  //    全局 ws 指向 shim:既有 {type:page/ink/state/text} 同步消息被翻译成 dc 事件 → 同步/UI 代码零改。──
  var _rtc = { pc: null, dc: null, el: null, mic: null, on: false, imgOn: false, callId: '', nativeDirect: false,
               sidebandKey: '', ctxFile: '', ctxPage: 0, ink: null, sel: '', _inkFp: '', inkDirty: false,
               hasInk: false, inkVer: 0, inkSeenVer: 0,
               inkPages: null, activeInkPage: null,
               inkResponseAcks: null, inkAckSeq: 0, turnEpoch: 0,
               visualTurnEpoch: -1,
               items: [], inTok: 0, compactTh: 0, lastCompact: 0 };   // ㊳:item 账本+每轮输入量+会话内压缩阈值
  function _rtcInkFingerprint(page, strokes) {
    strokes = strokes || [];
    var raw = '';
    try { raw = JSON.stringify(strokes); } catch (e) { raw = String(strokes.length); }
    var hash = 2166136261;
    for (var i = 0; i < raw.length; i++) {
      hash ^= raw.charCodeAt(i);
      hash = Math.imul(hash, 16777619);
    }
    return String(page || 0) + ':' + strokes.length + ':' + raw.length + ':' + (hash >>> 0).toString(16);
  }
  function _rtcInkPageState(page, create) {
    var key = String(Number(page) || 0);
    if (!_rtc.inkPages && create !== false) _rtc.inkPages = Object.create(null);
    var state = _rtc.inkPages && _rtc.inkPages[key];
    if (!state && create !== false) {
      state = {
        initialized: false, fp: '', strokes: [], hasInk: false,
        ver: 0, seenVer: 0, pending: false, pendingCount: 0,
        pendingOps: Object.create(null), waiters: [],
        signal: 0, signalSent: 0
      };
      _rtc.inkPages[key] = state;
    }
    return state || null;
  }
  function _rtcUseInkPage(page) {
    var state = _rtcInkPageState(page, true);
    _rtc.ink = state.strokes;
    _rtc._inkFp = state.fp;
    _rtc.hasInk = state.hasInk;
    _rtc.inkVer = state.ver;
    _rtc.inkSeenVer = state.seenVer;
    _rtc.inkDirty = !!(state.pending || (state.hasInk && state.ver > state.seenVer));
    return state;
  }
  function _rtcFreshInkPage() {
    var active = Number(_rtc.activeInkPage) || 0;
    if (active) {
      var activeState = _rtcInkPageState(active, false);
      if (activeState && (activeState.pending || (activeState.hasInk && activeState.ver > activeState.seenVer))) return active;
    }
    var current = Number(_rtc.ctxPage) || 0;
    if (current) {
      var currentState = _rtcInkPageState(current, false);
      if (currentState && (currentState.pending || (currentState.hasInk && currentState.ver > currentState.seenVer))) return current;
    }
    return 0;
  }
  function _rtcHasFreshInk(page) {
    if (page == null) return _rtcFreshInkPage() > 0;
    var state = _rtcInkPageState(page, false);
    return !!(state && (state.pending || (state.hasInk && state.ver > state.seenVer)));
  }
  function _rtcEffectiveTool(name, freshInk) {
    if (freshInk && /^(read_selection|read_page|see_page|see_figure)$/.test(String(name || ''))) return 'see_ink';
    return name;
  }
  function _rtcMarkInkSeen(name, ok, versionAtStart, pageAtStart) {
    if (!ok || name !== 'see_ink') return;
    var state = _rtcInkPageState(pageAtStart, false);
    if (!state) return;
    var version = Math.min(
      Math.max(0, state.ver || 0),
      Math.max(0, Number(versionAtStart) || 0)
    );
    if (version > (state.seenVer || 0)) state.seenVer = version;
    if (Number(_rtc.activeInkPage) === Number(pageAtStart) && !_rtcHasFreshInk(pageAtStart)) {
      _rtc.activeInkPage = null;
    }
    if (Number(pageAtStart) === Number(_rtc.ctxPage)) _rtcUseInkPage(pageAtStart);
  }
  function _rtcSetInkPending(page, opId) {
    var state = _rtcInkPageState(page, true);
    opId = String(opId || '');
    if (opId && state.pendingOps[opId]) return;
    if (opId) state.pendingOps[opId] = true;
    state.pendingCount = (state.pendingCount || 0) + 1;
    state.pending = true;
    if (Number(page) === Number(_rtc.ctxPage)) _rtcUseInkPage(page);
  }
  function _rtcDecreaseInkPending(page, opId) {
    var state = _rtcInkPageState(page, false);
    if (!state) return null;
    opId = String(opId || '');
    if (opId) {
      if (state.pendingOps[opId]) {
        delete state.pendingOps[opId];
        state.pendingCount = Math.max(0, (state.pendingCount || 0) - 1);
      }
    } else if ((state.pendingCount || 0) > 0) {
      state.pendingCount -= 1;
    }
    state.pending = (state.pendingCount || 0) > 0;
    if (Number(page) === Number(_rtc.ctxPage)) _rtcUseInkPage(page);
    return state;
  }
  function _rtcResolveInkPending(page) {
    var state = _rtcInkPageState(page, false);
    if (!state) return;
    if ((state.pendingCount || 0) > 0) {
      state.pending = true;
      if (Number(page) === Number(_rtc.ctxPage)) _rtcUseInkPage(page);
      return;
    }
    state.pending = false;
    var waiters = state.waiters.splice(0);
    waiters.forEach(function (resolve) { try { resolve(true); } catch (e) {} });
    if (Number(page) === Number(_rtc.ctxPage)) _rtcUseInkPage(page);
  }
  function _rtcCancelInkPending(page, opId) {
    var state = _rtcDecreaseInkPending(page, opId);
    if (!state) return;
    if (!state.pending) {
      var waiters = state.waiters.splice(0);
      waiters.forEach(function (resolve) { try { resolve(false); } catch (e) {} });
    }
  }
  function _rtcAwaitInkCommit(page, ms) {
    var state = _rtcInkPageState(page, false);
    if (!state || (!state.pending && !(state.pendingCount > 0))) return Promise.resolve(true);
    return new Promise(function (resolve) {
      var done = false;
      var finish = function (value) {
        if (done) return;
        done = true;
        clearTimeout(timer);
        var index = state.waiters.indexOf(finish);
        if (index >= 0) state.waiters.splice(index, 1);
        resolve(value);
      };
      var timer = setTimeout(function () { finish(false); }, ms);
      state.waiters.push(finish);
    });
  }
  function _rtcResetInkPages() {
    if (_rtc.inkPages) {
      Object.keys(_rtc.inkPages).forEach(function (key) {
        var state = _rtc.inkPages[key];
        var waiters = state && state.waiters ? state.waiters.splice(0) : [];
        waiters.forEach(function (resolve) { try { resolve(false); } catch (e) {} });
      });
    }
    _rtc.inkPages = Object.create(null);
  }
  function _rtcBeginUserTurn() {
    _rtc.turnEpoch = (_rtc.turnEpoch || 0) + 1;
    _rtc.visualTurnEpoch = -1;
    // App nativeDirect has no Pi control event to establish a user-turn
    // boundary. Without doing it here, the tool preamble and final answer are
    // split into different history turns.
    _rtc._newTurn = true;
    _rtc.pendingToolCalls = Object.create(null);
    _rtc.pendingToolResponse = null;
    // 旧轮尚未确认的回答即使迟到也不能消费笔迹；fresh 留给新轮重新判断。
    _rtc.inkResponseAcks = Object.create(null);
    return _rtc.turnEpoch;
  }
  // App Realtime 的普通会话完全本机运行。只有这些明确需要联网模型、搜索
  // 或现有 Anki/CLI 后端的工具，才允许按需调用用户自己的 Pi AI API；Pi
  // 离线不会影响通话建立、选区、页面、笔迹、视口图或本机笔记。
  var NATIVE_REALTIME_PI_AI_TOOLS = new Set([
    'make_anki', 'web_search', 'search_image', 'search_video',
    'deep_think', 'do_task', 'make_paper', 'route_to_text'
  ]);
  function _nativeRealtimePiAITool(name) {
    return NATIVE_REALTIME_PI_AI_TOOLS.has(String(name || ''));
  }
  function _dcSend(obj) {
    try {
      if (_rtc.dc && _rtc.dc.readyState === 'open') {
        _rtc.dc.send(JSON.stringify(obj));
        return true;
      }
    } catch (e) {}
    return false;
  }
  function _rtcSys(text) {
    _dcSend({ type: 'conversation.item.create', item: { type: 'message', role: 'system', content: [{ type: 'input_text', text: text }] } });
  }
  // ── 66 按轮语音录制(用户设计:历史对话可回放当时的语音):remote 轨 MediaRecorder,
  //    音频轮 created 开录 / done 收尾拿 clipId(上传异步),文字轮不录;打断的半截轮也收 ──
  // 128(用户实测:两段录音拼起来是**第一段的前半部分**)——根因:录音是按**文字时间线**切的。
  //   旧法:首个转写 delta 开录、response.done 时按"字数÷5.5"**估算**播放结束再停。
  //   但转写 delta 跑在音频播放**前面**很多(生成快、播放慢)→ 开录时上一轮的音频还在播、
  //   停录时本轮才播了个开头 → 录到的是「上轮尾巴 + 本轮开头」,而且长度全靠猜。
  //   正解:用 WebRTC 专属的官方事件 output_audio_buffer.started/stopped/cleared —— 它们标记的是
  //   音频**真正开始 / 结束播放**的时刻,录音起止跟它走,一秒不差。
  var _rec = { mr: null, chunks: [], mime: '', id: '', oab: false };
  function _recMime() {
    var c = ['audio/mp4', 'audio/webm;codecs=opus', 'audio/webm'];
    for (var i = 0; i < c.length; i++) { try { if (window.MediaRecorder && MediaRecorder.isTypeSupported(c[i])) return c[i]; } catch (e) {} }
    return '';
  }
  function _recStart() {   // 音频**开始播** → 开录(clipId 此刻就发,落库不用等录完)
    _recDrop();
    if (!_rtc.remoteStream || !window.MediaRecorder) return '';
    var mime = _recMime(); if (!mime) return '';
    try {
      var mr = new MediaRecorder(_rtc.remoteStream, { mimeType: mime });
      var id = 'c' + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
      var chunks = [];
      _rec = { mr: mr, chunks: chunks, mime: mime, id: id, oab: _rec.oab };
      mr.ondataavailable = function (e) { if (e.data && e.data.size) chunks.push(e.data); };
      mr.onstop = function () {   // 播完才停 → blob 是完整的一整段
        try {
          var blob = new Blob(chunks, { type: mime });
          if (blob.size > 4000) fetch('/api/assistant/voice-clip?id=' + id, { method: 'POST', headers: { 'Content-Type': mime }, body: blob }).catch(function () {});
        } catch (e) {}
      };
      mr.start(1000);
      return id;
    } catch (e) { _rec.mr = null; return ''; }
  }
  function _recStop() {    // 音频**播完 / 被打断** → 停录并上传(返回 clipId)
    var mr = _rec.mr, id = _rec.id;
    if (!mr) return '';
    try { if (mr.state !== 'inactive') mr.stop(); } catch (e) {}
    _rec.mr = null;
    return id;
  }
  function _recDrop() {    // 丢弃(不上传):挂断/重连时清场
    try { if (_rec.mr) { _rec.mr.onstop = null; if (_rec.mr.state !== 'inactive') _rec.mr.stop(); } } catch (e) {}
    _rec.mr = null;
  }
  function _recId() { return _rec.id || ''; }
  function _recAbort() { if (!_rec.oab) _recDrop(); }   // 官方事件在用时:不许中途掐(上一轮可能还在播)
  function _recFinishLegacy() {   // 回退路径(万一拿不到 output_audio_buffer.*):仍按估算收尾
    var mr = _rec.mr;
    if (!mr || mr.state === 'inactive') { _rec.mr = null; return ''; }
    var id = _rec.id;
    var _wait = 600;
    try { _wait = Math.max(0, (_rtc.aEnd || 0) - Date.now()) + 600; } catch (e) {}
    setTimeout(function () { try { if (mr.state !== 'inactive') mr.stop(); } catch (e) {} }, Math.min(_wait, 90000));
    _rec.mr = null;
    return id;
  }
  function _recFinish() { return _rec.oab ? _recId() : _recFinishLegacy(); }
  window.__vcSendText = function (text) {   // 66:侧栏输入框在 2.1(WebRTC)通话中打字直达实时模型(rc-assistant send 拦截调用)
    if (!(_rtc.on && ws && mode === 's2s')) return false;
    try {
      try { window.__asstVoiceMsg && window.__asstVoiceMsg('u', text); } catch (e) {}
      try { capUser(text); } catch (e) {}
      ws.send(JSON.stringify({ type: 'text', content: text }));   // shim → _rtcHandleUp 'text':flush ctx+item.create+RespCreate
      return true;
    } catch (e) { return false; }
  };
  function _rtcShimWs() {   // 顶替全局 ws:同步消息翻译成 dc,二进制(音频)忽略(媒体走 WebRTC 轨)
    return { readyState: 1, __shim: 1, close: function () {}, send: function (data) {
      if (typeof data !== 'string') return;
      // ㊺P2:上行状态(page/state/ink)镜像给控制 WS——relay 是工具执行者,ctx 要最新笔迹/选中/页码
      try { if (_rtc.ctlWs && _rtc.ctlWs.readyState === 1) _rtc.ctlWs.send(data); } catch (e) {}
      var j; try { j = JSON.parse(data); } catch (e) { return; }
      _rtcHandleUp(j);
    } };
  }
  function _rtcInterrupt() {
    // 程序发起的打断:光发 response.cancel 不够——已推进浏览器 WebRTC output buffer 的音频会照播完
    // (用户点了停,AI 还在自顾自说)。官方(client events / output_audio_buffer.clear,WebRTC/SIP Only)
    // 要求切断已缓冲音频必须发 output_audio_buffer.clear,且须紧跟在 response.cancel 之后。
    // (VAD 自动打断不走这里——WebRTC 下服务端自管 output 缓冲;我们已监听 output_audio_buffer.cleared,录音收尾自动对齐。)
    _dcSend({ type: 'response.cancel' });
    _dcSend({ type: 'output_audio_buffer.clear' });
  }
  function _rtcHandleUp(j) {
    var t = j.type;
    if (t === 'page') {
      // ㊵ 拉模式(用户拍板"只在需要时读取"):翻页/滚动**只更新本地状态,一个字都不发**——
      // 内容在用户开口/发文字的瞬间经 _rtcFlushCtx 注入(不问=零注入);模型要更多内容自己调 read_page
      var np = j.page;
      var pageChanged = !!(np && np !== _rtc.ctxPage);
      if (pageChanged) {
        _rtc.pageTs = Date.now();   // 用户注入策略:翻页时刻(停留 8s~12min 内开口才注入页面内容,省 token)
        clearTimeout(_rtc._ptPreT);   // 停留 8s(确认在读,不是快速翻过)→ **预拉**页文本填 cache:首问就有,不再'开口才拉赶不上'
        _rtc._ptPreT = setTimeout(function () {
          if (_rtc.ctxPage === np && !_rtc.pendText && _rtc.ctxFile) _rtcFetchPageText(_rtc.ctxFile + ':' + np);
        }, 8000);
      }
      if (np) {
        _rtc.ctxPage = np;
        _rtcUseInkPage(np);
        if (pageChanged) _rtc.activeInkPage = _rtcHasFreshInk(np) ? Number(np) : null;
      }
      if (j.total) _rtc.ctxTotal = j.total | 0;   // 131:总页数(用户实测 AI 上下文里没有,答不出"这本书多少页/还剩多少")
      if (j.text != null) _rtc.pendText = String(j.text || '');
      else if (pageChanged) _rtc.pendText = '';   // PDF 只推页码时不能沿用上一页文字；随后由当前页 char/provider 补齐
    } else if (t === 'ink') {
      var inkPage = Number(j.page) || Number(_rtc.ctxPage) || 0;
      var strokes = j.strokes || [];
      var state = _rtcInkPageState(inkPage, true);
      var fp = String(j.revision || _rtcInkFingerprint(inkPage, strokes));
      if (fp === state.fp) {
        // 无有效几何变化的抬笔（例如过短的选区线）不能留下永久 pending，
        // 也不能凭一次事件把旧批注升级成“新笔迹”。
        _rtcResolveInkPending(inkPage);
        return;
      }
      // pending 只表示原生操作仍在提交，不能把轮询到的旧几何误当成新版本；
      // 真正的首个变化由完成事件携带 changed=true。
      var explicitChange = j.changed === true;
      state.fp = fp;
      state.strokes = strokes;
      state.hasInk = !!strokes.length;
      // 133:笔迹/选中的**系统消息注入已收归 relay**(它在 speech_started 上注入,见 voice_realtime_relay.py)。
      // 这里曾用 _rtcSys 经 data channel 直发(127 为省一个 Pi 往返),但实测**从没落地过**:
      // 探针证实墨迹推到了 relay(⇡ink),却没有任何 role=system 的 item 进过对话——_dcSend 在 dc 未 open 时
      // 是静默丢弃的,失败无声无息,于是 bca7bb7 的措辞等于从没生效。别再往这条哑路上加东西。
      // 本地以版本而不是布尔值记录「模型尚未看过的新笔迹」。截图期间再落一笔时，
      // see_ink 只能消费调用开始前的版本，新版本会留到下一用户轮，不能被旧截图误清。
      // 每页首帧只建立旧批注基线；只有落笔事件或已建基线后的内容变化才是“新”。
      // 因而多页未读版本彼此独立，切页不会清掉其它页的待看状态。
      var baselineOnly = !state.initialized && !explicitChange;
      state.initialized = true;
      state.pending = (state.pendingCount || 0) > 0;
      if (!state.pending) {
        var waiters = state.waiters.splice(0);
        waiters.forEach(function (resolve) { try { resolve(true); } catch (e) {} });
      }
      if (baselineOnly) {
        if (inkPage === Number(_rtc.ctxPage)) _rtcUseInkPage(inkPage);
        return;
      }
      state.ver = (state.ver || 0) + 1;
      if (!strokes.length) {
        state.seenVer = state.ver;
        if (inkPage === Number(_rtc.ctxPage)) _rtcUseInkPage(inkPage);
        return;
      }
      if (inkPage === Number(_rtc.ctxPage)) _rtcUseInkPage(inkPage);
    } else if (t === 'state') {
      var sel = (j.sel || '').trim();
      if (sel === _rtc.sel) return;
      _rtc.sel = sel;   // 133:同上,注入归 relay(本地只记状态)
    } else if (t === 'text' && j.content) {
      _rtcBeginUserTurn();
      _rtcInterrupt();   // AI 正说话时打字发消息:不先打断会撞 conversation_already_has_active_response,这条 item 进了上下文却永远没 response(官方参考实现 handleSendTextMessage 第一行也是 interrupt)
      _rtcFlushCtx();   // ㊵ 拉模式:提问瞬间注入他正看着的内容
      _lastU = String(j.content).slice(0, 2000);   // ㉛:打字输入的问题也随轮次落库
      _rtc.turnToolAny = false;   // ㊸b:打字新用户轮开始 → 清工具标志
      _dcSend({ type: 'conversation.item.create', item: { type: 'message', role: 'user', content: [{ type: 'input_text', text: _lastU }] } });
      _rtcRespCreate('user');
    } else if (t === 'cancel' || t === 'tool_abort') {
      _rtcInterrupt();
    }
  }
  // ── 65 文字卡片(用户设计):route/stt 等文字回复在**侧栏关闭**时弹半透明磨砂卡——固定右下,
  //    按时间层叠(新卡在前,旧卡向左上交错缩小),每张可关;自动消失开关+秒数在语音设置卡(设备级)──
  var _cards = { list: [] };
  function _cardHideOn() { try { return localStorage.getItem('rc-voice-card-hide') !== '0'; } catch (e) { return true; } }
  function _cardSecs() { var v = 20; try { v = parseInt(localStorage.getItem('rc-voice-card-secs') || '20', 10) || 20; } catch (e) {} return Math.max(5, Math.min(60, v)); }
  function _sideOpen() { var sd = document.getElementById('ep-side'); return !!(sd && sd.classList.contains('open')); }
  var _dock = { list: [], open: false, loaded: false };
  var _FAV_CARDS_PAYLOAD_VERSION = 1;
  var _FAV_CARDS_MAX_COUNT = 64;
  var _FAV_CARDS_MAX_BYTES = 256 * 1024;
  var _FAV_CARDS_MAX_DEPTH = 16;
  var _FAV_CARDS_MAX_NODES = 8192;
  function _favUtf8Bytes(text) {
    try { return new TextEncoder().encode(text).length; } catch (e) {}
    try { return unescape(encodeURIComponent(text)).length; } catch (e2) { return Infinity; }
  }
  function _favJsonTreeOk(root) {
    var stack = [{ value: root, depth: 0 }], seen = [], nodes = 0;
    while (stack.length) {
      var entry = stack.pop(), value = entry.value;
      nodes += 1;
      if (nodes > _FAV_CARDS_MAX_NODES || entry.depth > _FAV_CARDS_MAX_DEPTH) return false;
      if (value == null || typeof value === 'string' || typeof value === 'boolean') continue;
      if (typeof value === 'number') { if (!Number.isFinite(value)) return false; continue; }
      if (typeof value !== 'object') return false;
      if (seen.indexOf(value) >= 0) return false;
      seen.push(value);
      if (Array.isArray(value)) {
        for (var ai = 0; ai < value.length; ai++) stack.push({ value: value[ai], depth: entry.depth + 1 });
      } else {
        var proto = Object.getPrototypeOf(value);
        if (proto !== Object.prototype && proto !== null) return false;
        var keys = Object.keys(value);
        for (var ki = 0; ki < keys.length; ki++) stack.push({ value: value[keys[ki]], depth: entry.depth + 1 });
      }
    }
    return true;
  }
  function _favPrepare(rec) {
    rec = rec || {};
    var isCards = rec.kind === 'cards' || !!rec.gid ||
      (rec.payload && rec.payload.kind === 'cards');
    if (!isCards) return { ok: true, record: rec };   // 普通 HTML/text 卡维持旧协议
    var payload = rec.payload, cards = null;
    try {
      if (payload == null) {
        cards = (typeof rec.raw === 'string') ? JSON.parse(rec.raw) : rec.raw;
        payload = { version: _FAV_CARDS_PAYLOAD_VERSION, kind: 'cards', cards: cards };
      }
      if (!payload || Object.getPrototypeOf(payload) !== Object.prototype ||
          Object.keys(payload).sort().join(',') !== 'cards,kind,version' ||
          payload.version !== _FAV_CARDS_PAYLOAD_VERSION || payload.kind !== 'cards') {
        throw new Error('payload-contract');
      }
      cards = payload.cards;
      if (!Array.isArray(cards) || !cards.length || cards.length > _FAV_CARDS_MAX_COUNT ||
          cards.some(function (card) {
            return !card || typeof card !== 'object' || Array.isArray(card);
          }) || !_favJsonTreeOk(payload)) {
        throw new Error('payload-shape');
      }
      var serialized = JSON.stringify(payload);
      if (!serialized || _favUtf8Bytes(serialized) > _FAV_CARDS_MAX_BYTES) {
        throw new Error('payload-size');
      }
      var out = {};
      Object.keys(rec).forEach(function (key) {
        if (key !== 'raw' && key !== 'payload') out[key] = rec[key];
      });
      out.kind = 'cards';
      out.payload = JSON.parse(serialized);
      out.cid = String(out.cid || out.gid || _mkCid());
      out.gid = String(out.gid || out.cid);
      if (!/^[A-Za-z0-9_-]{1,80}$/.test(out.cid) || !/^[A-Za-z0-9_-]{1,80}$/.test(out.gid)) {
        throw new Error('payload-identity');
      }
      out.id = String(out.id || out.gid);
      if (!/^[A-Za-z0-9_-]{1,80}$/.test(out.id)) throw new Error('payload-id');
      return { ok: true, record: out };
    } catch (e) {
      return {
        ok: false,
        error: e && e.message === 'payload-size'
          ? '学习卡过大，未加入收藏夹'
          : '学习卡数据不完整，未加入收藏夹'
      };
    }
  }
  function _dockLoad(cb) {   // 78:收藏夹服务端持久化(独立于会话,清空对话不清它)
    if (_dock.loaded) { cb && cb(); return; }
    fetch('/api/assistant/voice-cards').then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.ok) { _dock.list = d.cards || []; _dock.loaded = true; }
      _dockBtn(); cb && cb();
    }).catch(function () { cb && cb(); });
  }
  function _favSave(rec) {
    rec.cid = rec.cid || rec.gid || _mkCid();   // 学习卡 cid=gid；收藏只增加宿主，不另发编号
    rec.id = rec.id || ((rec.kind === 'cards' || rec.gid) ? (rec.gid || rec.cid) :
      ('v' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6)));
    var current = null;
    _dock.list.forEach(function (item) {
      if (item.id === rec.id) current = item;
    });
    if (rec.kind === 'cards' || rec.gid) {
      var revision = Math.max(
        Number(rec.revision) || 0,
        Number(current && current.revision) || 0
      ) + 1;
      rec.revision = revision;
    }
    var prepared = _favPrepare(rec);
    if (!prepared.ok) {
      try { if (typeof _toast === 'function') _toast(prepared.error); } catch (e0) {}
      return '';
    }
    rec = prepared.record;
    var i = -1, previous = null;
    _dock.list.forEach(function (x, k) {
      if (x.id === rec.id) { i = k; previous = x; }
    });
    if (i < 0) _dock.list.push(rec); else _dock.list[i] = rec;
    _dockBtn(); if (_dock.open) _dockPanel(true);
    fetch('/api/assistant/voice-cards', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ op: 'add', card: rec }) }).then(function (response) {
        if (!response.ok) {
          var error = new Error('favorite-save-' + response.status);
          error.rejectPayload = response.status === 400 || response.status === 413;
          error.staleRevision = response.status === 409;
          throw error;
        }
      }).catch(function (error) {
        // 客户端已经按同一合同完整校验；断网/服务暂不可用时保留本地会话里的
        // 完整卡，符合 local-first。只有服务端明确 400/413 拒绝 payload 才回滚，
        // 绝不能显示一张刷新后必坏的“已收藏”卡。
        if (error && error.staleRevision) return;   // 较新的同卡状态已先落库
        if (!error || !error.rejectPayload) {
          try { if (typeof _toast === 'function') _toast('已本地收藏，跨设备同步暂不可用'); } catch (e0) {}
          return;
        }
        var at = -1;
        _dock.list.forEach(function (x, k) { if (x.id === rec.id) at = k; });
        if (previous) {
          if (at < 0) _dock.list.push(previous); else _dock.list[at] = previous;
        } else if (at >= 0) {
          _dock.list.splice(at, 1);
        }
        _dockBtn(); if (_dock.open) _dockPanel(true);
        try { if (typeof _toast === 'function') _toast('收藏失败，卡片未保存'); } catch (e1) {}
      });
    return rec.id;
  }
  function _favMeta() {   // 元数据:书/页/触发这轮的问题——长回答离开会话也能自释
    return { file: (_rtc.ctxFile || '').split('/').pop() || '', page: String(_rtc.ctxPage || ''), q: (_lastU || '').slice(0, 120) };
  }
  window.__vcPinBind = function (el, label, textFn, spec, pressTarget) { try { return _pinBind(el, label, textFn, spec, pressTarget); } catch (e) { return null; } };   // 79:owner 保持整卡；pressTarget 可收窄到正文
  // 工具指示器 v2:把**这张卡**(.vc-card)整套能力暴露出去,rc-toolchip 只当状态机、不另造 DOM——
  //   用户拍板:「我很喜欢这个方块的样式,在这个基础上进行修改就好」。
  RC.voiceCard = {
    css: function () { try { injectCss(); } catch (e) {} },   // 131:任何时候建卡/挂按钮前先确保样式在
    renderInto: function (host, spec) { try { return _renderInto(host, spec); } catch (e) { return null; } },   // 钉入书页:与浮层卡同一 _cardDom 渲染
    renderInflow: function (host, spec) { try { return _renderInflow(host, spec); } catch (e) { return null; } },   // 侧栏对话流:同一 _cardDom(壳+三态);专属交互调用方自挂
    trash: { show: function (on) { try { _trashShow(on); } catch (e) {} }, hot: function (on) { try { _trashHot(on); } catch (e) {} }, inZone: function (x, y) { try { return _inTrashZone(x, y); } catch (e) { return false; } } },   // 钉入卡拖到左上角删除(浮层同一删除区)
    favorite: {
      hint: function (on) { try { _dockHint(on); } catch (e) {} },
      inZone: function (x, y) { try { return _inDockZone(x, y); } catch (e) { return false; } },
      save: function (rec) { try { rec = rec || {}; rec.meta = rec.meta || _favMeta(); _dockLoad(function () { _favSave(rec); }); if (typeof _toast === 'function') _toast('已收入收藏夹'); return true; } catch (e) { return false; } },
      prepare: function (rec) { try { return _favPrepare(rec || {}); } catch (e) { return { ok: false, error: '学习卡数据不完整，未加入收藏夹' }; } }   // 合同/宿主可预检；真正保存仍唯一走 save
    },
    push: function (text, label, isHtml, force, cid, opts) { try { return _cardPush(text, label, isHtml, force, cid, opts); } catch (e) { return null; } },
    close: function (c) { try { _cardClose(c); } catch (e) {} },
    form: function (el, f) { try { return _cardForm(el, f); } catch (e) { return 'full'; } },
    layout: function () { try { _cardLayout(); } catch (e) {} },
    mkCid: _mkCid,
    pinReg: function (el, cid) { try { _pinReg(el, cid); } catch (e) {} },       // 登记实例 → 选中按 cid 处处同步
    pinBind: function (el, label, fn, spec, pressTarget) { try { return _pinBind(el, label, fn, spec, pressTarget); } catch (e) { return null; } },   // 长按=选中/取消；第 5 参数只收窄手势面
    cardSize: {
      get: function (cid) {
        cid = _cardSizeCid(cid);
        return cid && _cardSizes[cid] ? Object.assign({}, _cardSizes[cid]) : null;
      },
      set: function (cid, value) {
        cid = _cardSizeCid(cid);
        var record = _cardSizeRecord(value);
        if (!cid || !record) return Promise.reject(new Error('卡片尺寸无效'));
        record.updatedAt = Math.max(record.updatedAt, Date.now());
        _cardSizes[cid] = record;
        _cardSizePaint(cid, record);
        return _cardSizePersist(cid, record);
      },
      load: function (cid) { return _cardSizeLoad(cid); }
    },
    bindChargedDrag: _bindChargedDrag,   // 卡头/dot 共用 420ms charged drag；正文不挂 touch-action:none
    dragToDock: function (el, fn) { try { _dragToDock(el, fn); } catch (e) {} },  // 侧栏卡长按拖出=生成副本/收藏
    sideOpen: _sideOpen
  };
  function _placeFx(x, y) {   // 92:放置特效——小卡从放手点飞向浮层堆叠位置缩小淡出("看不见但确实放过去了")
    try {
      var f = document.createElement('div');
      f.style.cssText = 'position:fixed;left:' + (x - 30) + 'px;top:' + (y - 20) + 'px;width:60px;height:40px;border-radius:10px;' +
        'background:rgba(123,108,255,.55);border:1px solid rgba(255,255,255,.35);z-index:2147481470;pointer-events:none;' +
        'transition:transform .55s cubic-bezier(.4,0,.6,1),opacity .55s';
      document.body.appendChild(f);
      requestAnimationFrame(function () {
        var tx = (window.innerWidth - 80) - x, ty = (window.innerHeight - 210) - y;
        f.style.transform = 'translate(' + tx + 'px,' + ty + 'px) scale(.18)';
        f.style.opacity = '0';
      });
      setTimeout(function () { try { f.remove(); } catch (e) {} }, 650);
    } catch (e) {}
  }
  function _dropOnUpage(e) {   // #50:落点下面是不是一张自建页(.pdf-upage);是则返回它,供粘贴
    try {
      var t = document.elementFromPoint(e.clientX, e.clientY);
      var pg = t && t.closest ? t.closest('.pdf-upage') : null;
      return (pg && pg.__upRec) ? pg : null;
    } catch (err) { return null; }
  }
  function _payloadCards(rec) {   // 学习卡必须还原为活状态机，不能降级成 HTML 快照
    if (!rec || (rec.kind !== 'cards' && !rec.gid)) return null;   // 兼容旧收藏：当时有 gid 但未写 kind
    try {
      var a = rec.payload && rec.payload.version === _FAV_CARDS_PAYLOAD_VERSION &&
        rec.payload.kind === 'cards' ? rec.payload.cards :
        ((typeof rec.raw === 'string') ? JSON.parse(rec.raw) : rec.raw);
      return (Array.isArray(a) && a.length && typeof a[0] === 'object') ? a : null;
    } catch (e) { return null; }
  }
  function _payloadPinAt(rec, x, y, srcEl) {
    if (!rec) return false;
    var cards = _payloadCards(rec);
    try {
      if (cards && window.RC && RC.stickynote && RC.stickynote.createCardAt)
        return RC.stickynote.createCardAt(x, y, cards, rec.gid || rec.cid || '');
      var _k = { images: 'image', videos: 'video', weather: 'weather', news: 'news' }[rec.kind] || 'text';
      var _st = {}; try { _st = (window.RC && RC.toolChip && RC.toolChip.styleOf) ? RC.toolChip.styleOf(_k) : {}; } catch (e2) {}
      if (window.RC && RC.stickynote && RC.stickynote.createHtmlAt)
        return RC.stickynote.createHtmlAt(x, y, { content: rec.isHtml ? rec.raw : String(rec.raw || rec.text || ''), isHtml: !!rec.isHtml, label: rec.label || '卡片', type: _st.color || '', icon: _st.icon || '', cid: rec.cid || (srcEl && srcEl.dataset && srcEl.dataset.vcCid) || '' });
    } catch (e) {}
    return false;
  }
  function _payloadFloat(rec, srcEl) {
    var cid = (rec && (rec.cid || rec.gid)) || (srcEl && srcEl.dataset && srcEl.dataset.vcCid) || _mkCid();
    var cards = _payloadCards(rec), c = null;
    if (cards && window.RC && RC.flashcard &&
        typeof RC.flashcard.renderEntity === 'function') {
      var gid = rec.gid || cid;
      var entity = RC.flashcard.renderEntity(null, {
        surface: 'float',
        mode: 'state',
        cards: cards,
        gid: gid,
        label: rec.label || '🎴 学习卡片',
        type: '#b9a8ff',
        icon: '🎴',
        form: 'full',
        selectionLabel: rec.label || '学习卡片',
        // 收藏夹不是 HTML 快照：活卡状态变化后把完整快照回写同一
        // 结构化记录。评分/翻面字段与 Anki/source/投影身份一起保存。
        onStateChange: function (nextCards) {
          if (!Array.isArray(nextCards) || !nextCards.length) return;
          var next = {};
          Object.keys(rec).forEach(function (key) {
            if (key !== 'raw' && key !== 'payload') next[key] = rec[key];
          });
          next.kind = 'cards';
          next.cid = rec.cid || gid;
          next.gid = gid;
          next.payload = {
            version: _FAV_CARDS_PAYLOAD_VERSION,
            kind: 'cards',
            cards: nextCards
          };
          _favSave(next);   // _favPrepare 再做同一 256KiB/结构/身份/修订号围栏
        }
      });
      c = entity && entity.voiceCard;
    } else if (rec) c = _cardPush(rec.isHtml ? rec.raw : (rec.raw || rec.text), rec.label, !!rec.isHtml, true, cid);
    return c;
  }
  // ── 卡片统一「蓄力 → ready → 拖动」状态机 ──
  // touch-action 必须在 pointerdown 前声明，故只挂专用卡头/dot；正文仍保留滚动、选择和 pinBind 长按。
  // pointercancel / capture 丢失 / 窗口失焦都是系统中断，只走 onCancel 回滚，永远不提交 drop。
  var _activeChargedDrag = null;
  function _chargedDragSessionStale(session, nextDownEvent) {
    if (!session) return false;
    // isConnected 只在浏览器真实 Node 上可靠；测试替身/旧宿主未提供时保持
    // fail-closed，不凭“未知”抢占仍可能活跃的手势。
    if (session.captureEl && session.captureEl.isConnected === false) return true;
    if (session.feedbackEl && session.feedbackEl.isConnected === false) return true;
    if (session.runtimeAttached === false) return true;
    if (!session.doc || !session.win || session.win.closed === true) return true;
    if (session.captureEl && session.captureEl.ownerDocument &&
        session.captureEl.ownerDocument !== session.doc) return true;
    if (session.doc.defaultView && session.doc.defaultView !== session.win) return true;
    // Pointer Events 保证同一个 pointerId 在一次 active-pointer 生命周期内
    // 不会再次产生新的 pointerdown。若下一次 pointerdown 已使用同一 id，
    // 且不是同一事件正在经过另一层 listener，旧 session 的 pointerup/cancel
    // 必然已被宿主漏掉；这是可验证的 stale 证明，可安全回收。
    if (nextDownEvent && session.pointerId != null &&
        nextDownEvent.pointerId === session.pointerId &&
        session.downEvent !== nextDownEvent) return true;
    return false;
  }
  function _reapStaleChargedDrag(nextDownEvent) {
    var stale = _activeChargedDrag;
    if (!stale || !_chargedDragSessionStale(stale, nextDownEvent)) return false;
    // 只让原 binding 用自己的 cleanup 回收 timer、pointer capture 和监听器；
    // 不能直接覆盖仍连接的全局 owner，也不能把失效手势误当 drop 提交。
    try {
      if (typeof stale.cancelActive === 'function') {
        stale.cancelActive('stale-active');
      }
    } catch (e) {}
    // 已有明确 stale 证明但旧 binding 本身也损坏时，至少释放全局闩锁；
    // 活跃且连接正常的 session 永远不会走到这里。
    if (_activeChargedDrag === stale) _activeChargedDrag = null;
    return true;
  }
  function _bindChargedDrag(handles, opts) {
    opts = opts || {};
    var list;
    if (Array.isArray(handles)) list = handles.slice();
    else if (handles && typeof handles.length === 'number' && !handles.addEventListener) list = Array.prototype.slice.call(handles);
    else list = [handles];
    list = list.filter(function (h) { return !!(h && h.addEventListener); });
    var holdMs = opts.holdMs == null ? 420 : Math.max(0, Number(opts.holdMs) || 0);
    var slop = opts.slop == null ? 8 : Math.max(0, Number(opts.slop) || 0);
    var dragSlop = opts.dragSlop == null ? 1 : Math.max(0, Number(opts.dragSlop) || 0);
    var touchAction = opts.touchAction == null ? 'none' : String(opts.touchAction);
    var state = null, suppressClickUntil = 0, destroyed = false;
    var oldTouchActions = [];

    function _call(name, args) {
      try { if (typeof opts[name] === 'function') return opts[name].apply(null, args || []); } catch (e) {}
    }
    function _feedback(s, charging, ready) {
      var f = (s && s.feedbackEl) || opts.feedbackEl;
      if (!f || !f.classList) return;
      f.classList.toggle('vc-drag-charging', !!charging);
      f.classList.toggle('vc-drag-ready', !!ready);
    }
    function _samePointer(e, s) {
      return !!(s && (!e || e.pointerId == null || s.pointerId == null || e.pointerId === s.pointerId));
    }
    function _removeRuntimeListeners(s) {
      if (!s) return;
      var doc = s.doc, win = s.win;
      if (doc && doc.removeEventListener) {
        doc.removeEventListener('pointermove', _move, true);
        doc.removeEventListener('pointerup', _up, true);
        doc.removeEventListener('pointercancel', _pointerCancel, true);
        doc.removeEventListener('visibilitychange', _visibility, true);
      }
      if (win && win.removeEventListener) win.removeEventListener('blur', _blur, true);
      if (s.captureEl) s.captureEl.removeEventListener('lostpointercapture', _lostCapture, true);
      s.runtimeAttached = false;
    }
    function _cleanup(s) {
      if (!s) return;
      if (s.timer) { clearTimeout(s.timer); s.timer = null; }
      _removeRuntimeListeners(s);
      _feedback(s, false, false);
      if (state === s) state = null;
      if (_activeChargedDrag === s) _activeChargedDrag = null;
      try {
        if (s.captureEl && s.pointerId != null && s.captureEl.hasPointerCapture &&
            s.captureEl.hasPointerCapture(s.pointerId)) s.captureEl.releasePointerCapture(s.pointerId);
      } catch (e) {}
    }
    function _cancel(reason, e) {
      var s = state;
      if (!s || !_samePointer(e, s)) return;
      s.lastEvent = e || s.lastEvent;
      _cleanup(s);
      _call('onCancel', [s, e || s.lastEvent, reason || 'cancel']);
    }
    function _ready() {
      var s = state;
      if (!s || s.ready) return;
      s.timer = null;
      s.ready = true;
      s.readyX = s.lastX;
      s.readyY = s.lastY;
      s.dx = 0; s.dy = 0;
      suppressClickUntil = Date.now() + 650;
      _feedback(s, false, true);
      try {
        var nav = s.win && s.win.navigator;
        if (nav && typeof nav.vibrate === 'function') nav.vibrate(8);
      } catch (e) {}
      _call('onReady', [s, s.lastEvent]);
    }
    function _move(e) {
      var s = state;
      if (!s || !_samePointer(e, s)) return;
      s.lastEvent = e;
      s.lastX = e.clientX; s.lastY = e.clientY;
      if (!s.ready) {
        var drift = Math.hypot(e.clientX - s.startX, e.clientY - s.startY);
        if (drift > slop) {
          if (s.timer) { clearTimeout(s.timer); s.timer = null; }
          // 这不是短点：用户已经试图移动，只是尚未完成蓄力。浏览器（尤其
          // mouse / touch-action:none）仍可能在 pointerup 后合成 click；
          // 只吞紧随其后的那一次，不能让卡片形态误切换。
          suppressClickUntil = Date.now() + 650;
          _cancel('slop', e);
        }
        return;
      }
      if (e.cancelable !== false && e.preventDefault) e.preventDefault();
      s.dx = e.clientX - s.readyX;
      s.dy = e.clientY - s.readyY;
      if (!s.moved && Math.hypot(s.dx, s.dy) > dragSlop) s.moved = true;
      _call('onMove', [s, e]);
    }
    function _up(e) {
      var s = state;
      if (!s || !_samePointer(e, s)) return;
      s.lastEvent = e;
      if (!s.ready) { _cleanup(s); return; }   // 真短点：不吞 click，也不进入 drag 回调
      suppressClickUntil = Date.now() + 650;
      _cleanup(s);
      _call('onEnd', [s, e]);
    }
    function _pointerCancel(e) { _cancel('pointercancel', e); }
    function _lostCapture(e) { _cancel('lostpointercapture', e); }
    function _blur(e) { _cancel('blur', e); }
    function _visibility(e) {
      var s = state;
      if (s && s.doc && s.doc.hidden) _cancel('hidden', e);
    }
    function _down(e) {
      if (destroyed) return;
      // 同一 binding 也可能在宿主漏掉 pointerup/cancel 后残留 local state；
      // 必须先用同一套 stale 证明回收，不能在 state 早退处把全局锁永久卡死。
      if (state && _activeChargedDrag === state) {
        _reapStaleChargedDrag(e);
      }
      if (state) return;
      if (_activeChargedDrag) {
        _reapStaleChargedDrag(e);
      }
      if (_activeChargedDrag) return;
      if (e.button !== undefined && e.button !== 0) return;
      if (_call('canStart', [e]) === false) return;
      var captureEl = e.currentTarget || this;
      var doc = (captureEl && captureEl.ownerDocument) || document;
      var win = (doc && doc.defaultView) || window;
      var s = {
        id: e.pointerId,
        pointerId: e.pointerId,
        pointerType: e.pointerType || '',
        startX: e.clientX, startY: e.clientY,
        readyX: e.clientX, readyY: e.clientY,
        lastX: e.clientX, lastY: e.clientY,
        dx: 0, dy: 0, moved: false, ready: false,
        captureEl: captureEl,
        feedbackEl: opts.feedbackEl || captureEl,
        doc: doc, win: win, downEvent: e, lastEvent: e, timer: null,
        runtimeAttached: false, cancelActive: null
      };
      s.cancelActive = function (reason) {
        if (state === s) _cancel(reason || 'stale-active', s.lastEvent);
      };
      state = s; _activeChargedDrag = s;
      _feedback(s, true, false);
      doc.addEventListener('pointermove', _move, true);
      doc.addEventListener('pointerup', _up, true);
      doc.addEventListener('pointercancel', _pointerCancel, true);
      doc.addEventListener('visibilitychange', _visibility, true);
      if (win && win.addEventListener) win.addEventListener('blur', _blur, true);
      captureEl.addEventListener('lostpointercapture', _lostCapture, true);
      s.runtimeAttached = true;
      try { if (captureEl.setPointerCapture && e.pointerId != null) captureEl.setPointerCapture(e.pointerId); } catch (err) {}
      s.timer = setTimeout(_ready, holdMs);
    }
    function _click(e) {
      if (Date.now() >= suppressClickUntil) return;
      suppressClickUntil = 0;
      e.preventDefault();
      e.stopImmediatePropagation();
      _call('onClickSuppressed', [e]);
    }
    function _contextmenu(e) {
      if (state || Date.now() < suppressClickUntil) e.preventDefault();
    }

    list.forEach(function (h) {
      oldTouchActions.push({ el: h, value: h.style.touchAction });
      h.style.touchAction = touchAction;
      h.addEventListener('pointerdown', _down);
      h.addEventListener('click', _click, true);
      h.addEventListener('contextmenu', _contextmenu);
    });
    var controller = {
      cancel: function (reason) { _cancel(reason || 'external', state && state.lastEvent); },
      destroy: function () {
        if (destroyed) return;
        destroyed = true;
        _cancel('destroy', state && state.lastEvent);
        list.forEach(function (h) {
          h.removeEventListener('pointerdown', _down);
          h.removeEventListener('click', _click, true);
          h.removeEventListener('contextmenu', _contextmenu);
        });
        oldTouchActions.forEach(function (x) { x.el.style.touchAction = x.value; });
      },
      isActive: function () { return !!state; }
    };
    return controller;
  }
  function _dragToDock(el, payloadFn) {   // 83(用户设计):卡头蓄力 420ms 后拖出副本；短按仍只切形态
    try { injectCss(); } catch (e) {}   // 92:ghost 样式保险——通话 UI 没初始化过时侧栏拖动 ghost 曾无样式(看不见"卡片")
    var hd = el.querySelector('.vc-card-hd') || el.querySelector('.vc-if-hd') || el.firstElementChild || el;   // 统一壳=vc-card-hd(旧 vc-if-hd 兼容兜底)
    hd.style.touchAction = 'none'; hd.style.cursor = 'grab';
    // 同一共享卡可能被不同装载路径再次注册。只保留一套监听器，并更新其 payload，
    // 否则一次 pointerup 会创建多个相同页面卡。
    var binding = hd.__bwDragToDockBinding;
    if (binding) { binding.payloadFn = payloadFn; return binding.drag; }
    binding = { payloadFn: payloadFn };
    try { Object.defineProperty(hd, '__bwDragToDockBinding', { value: binding, configurable: true }); }
    catch (e) { hd.__bwDragToDockBinding = binding; }
    var ghost = null, moved = false, grabX = 28, grabY = 18;
    function _pos(x, y) {
      if (!ghost) return;
      var l = x - Math.min(Math.max(18, grabX), Math.max(18, ghost.offsetWidth - 18));
      var t = y - Math.min(Math.max(12, grabY), 34);
      ghost.style.transform = 'translate3d(' + Math.round(l) + 'px,' + Math.round(t) + 'px,0)';
    }
    function _cleanDrag() {
      _dragFxEnd();   // #51:反馈层清场
      if (ghost) { ghost.remove(); ghost = null; }
      el.style.opacity = '';
      _dockHint(false); _trashShow(false);
    }
    binding.drag = _bindChargedDrag(hd, {
      holdMs: 420,
      slop: 8,
      dragSlop: 1,
      feedbackEl: el,
      canStart: function (ev) {
        if (ev.target && ev.target.closest && ev.target.closest('button,a')) return false;
        moved = false;
        el.dataset.downT = String(Date.now());
        try { var _sr0 = el.getBoundingClientRect(); grabX = ev.clientX - _sr0.left; grabY = ev.clientY - _sr0.top; } catch (e) {}
        return true;
      },
      onReady: function (session) {
        try { if (typeof el.__bwCancelPinHold === 'function') el.__bwCancelPinHold(); } catch (e0) {}
        ghost = el.cloneNode(true); ghost.className = 'vc-drag-ghost';
        var _gsd = document.getElementById('ep-side');   // 卡宽按左侧页面区自适应(用户拍板;与钉入宽同式)
        var _gsl = _gsd ? _gsd.getBoundingClientRect().left : window.innerWidth;
        ghost.style.width = Math.max(240, Math.min(480, Math.round(_gsl * 0.44))) + 'px';
        document.body.appendChild(ghost);
        el.style.opacity = '.22';
        _pos(session.lastX, session.lastY);
      },
      onMove: function (session, e2) {
        if (!moved && session.moved) {
          moved = true;
          el.dataset.touched = '1';
          try { if (typeof el.__bwCancelPinHold === 'function') el.__bwCancelPinHold(); } catch (e0) {}
        }
        if (moved) {
          _pos(e2.clientX, e2.clientY);
          _dockHint(_inDockZone(e2.clientX, e2.clientY));
          _trashShow(true); _trashHot(_inTrashZone(e2.clientX, e2.clientY));   // 134:侧栏卡也能往左上角拖删
          try { var _sd9 = document.getElementById('ep-side'); var _sl9 = _sd9 ? _sd9.getBoundingClientRect().left : window.innerWidth; if (e2.clientX < _sl9 - 30) { var _gr9 = ghost ? ghost.getBoundingClientRect() : null; _dragFx(_gr9 ? _gr9.left : e2.clientX, _gr9 ? _gr9.top : e2.clientY, el, null); } else _dragFxEnd(); } catch (e9) {}   // #51:探测点=ghost 左上角(=钉入点,用户拍板)
        }
      },
      onEnd: function (session, e3) {
        var wasMoved = moved && session.moved;
        moved = false;
        var gr = null; try { if (ghost) gr = ghost.getBoundingClientRect(); } catch (e) {}   // ghost 左上角=松手时卡的位置=钉入点(用户拍板)
        _cleanDrag();
        if (wasMoved && e3 && _inTrashZone(e3.clientX, e3.clientY)) {   // 侧栏拖出的始终是副本：删除区只丢弃副本，绝不删对话原卡
          try { if (typeof _toast === 'function') _toast('已丢弃拖出的副本'); } catch (e) {}
          return;
        }
        if (wasMoved && e3 && _inDockZone(e3.clientX, e3.clientY)) {
          var rec = binding.payloadFn();
          rec.meta = rec.meta || _favMeta();
          rec.cid = rec.cid || (el.dataset && el.dataset.vcCid) || _mkCid();   // 95:收藏保留卡片编号
          _dockLoad(function () { _favSave(rec); });
          try { if (typeof _toast === 'function') _toast('已收入收藏夹'); } catch (e) {}
        } else if (wasMoved && e3 && _dropOnUpage(e3) && window.__upPasteCard) {
          // #50(用户设计):把卡从侧栏/收藏拖到**自建页**上=粘贴。落点吸附到最近行列交叉点,
          //   持久化,内容作为**独立副本**(删除不影响收藏夹/侧栏原件)。
          var _pg = _dropOnUpage(e3);
          window.__upPasteCard(_pg, e3.clientX, e3.clientY, binding.payloadFn());
          _placeFx(e3.clientX, e3.clientY);
          try { if (typeof _toast === 'function') _toast('已贴到页面'); } catch (e) {}
        } else if (wasMoved && e3 && _sideOpen()) {
          // 92→改(用户 2026-07-20):侧栏拖出=**默认钉子模式**,松手点直接**钉入页面**(ghost 左上角=卡左上角=钉入点)。
          //   旧"放入字幕浮层"已按用户要求去掉(跟钉子模式矛盾:拖出后变普通浮动卡)。钉不上(松手不在正文)才回退浮层钉子卡。
          var sd2 = document.getElementById('ep-side');
          var sl = sd2 ? sd2.getBoundingClientRect().left : window.innerWidth;
          if (e3.clientX < sl - 30) {
            var rec2 = binding.payloadFn();
            var px = gr ? gr.left : e3.clientX, py = gr ? gr.top : e3.clientY;
            var pinned = false;
            try { pinned = _payloadPinAt(rec2, px, py, el); } catch (e) {}
            if (pinned) { _placeFx(e3.clientX, e3.clientY); }
            else {   // 松手不在正文(anchorFromPoint 落空)→ 回退:浮层钉子卡(不自动消失,可继续拖去钉)
              var c2 = _payloadFloat(rec2, el);
              if (c2) { c2.pinned = true; c2.pinMode = true; _placeFx(e3.clientX, e3.clientY); try { if (typeof _toast === 'function') _toast('没落在正文上——卡片已浮出,拖到正文可钉住'); } catch (e) {} }
            }
          }
        }
      },
      onCancel: function () {
        moved = false;
        _cleanDrag();   // 系统取消只回滚视觉；绝不删除/收藏/钉页
      },
      onClickSuppressed: function () {
        el.dataset.downT = '';
        el.dataset.dragged = '';
      }
    });
    return binding.drag;
  }
  // ── 拖动反馈统一 helper(#51):落在卡上=目标卡高亮环;否则=锚定反馈(光带/插入线);清=两者都清 ──
  var _dropHotEl = null;
  function _dragFx(cx, cy, srcEl, ignoreEl) {
    var tgt = null;
    try { var t0 = document.elementFromPoint(cx, cy); tgt = t0 && t0.closest && t0.closest('.vc-card, .vc-if, .vc-dk-card'); } catch (e) {}
    if (tgt && srcEl && (tgt === srcEl || tgt.contains(srcEl) || srcEl.contains(tgt))) tgt = null;   // 源卡自己不算目标
    if (tgt && !tgt.closest('.rc-note')) {
      if (_dropHotEl !== tgt) { _dragFxClearHot(); _dropHotEl = tgt; tgt.classList.add('vc-drop-hot'); }
      try { RC.stickynote && RC.stickynote.anchorFx && RC.stickynote.anchorFx.hide(); } catch (e) {}
    } else {
      _dragFxClearHot();
      try { RC.stickynote && RC.stickynote.anchorFx && RC.stickynote.anchorFx.show(cx, cy, ignoreEl); } catch (e) {}
    }
  }
  function _dragFxClearHot() { if (_dropHotEl) { try { _dropHotEl.classList.remove('vc-drop-hot'); } catch (e) {} _dropHotEl = null; } }
  function _dragFxEnd() { _dragFxClearHot(); try { RC.stickynote && RC.stickynote.anchorFx && RC.stickynote.anchorFx.hide(); } catch (e) {} }
  // ── 单张图片拖出=图片便签(用户拍板 2026-07-20:侧栏/收藏夹/工具卡里按住一张图拖到正文=便签装图粘贴)──
  //   长按 300ms+位移>10 才起拖(静止长按后 iOS 不再把手势判给滚动;短滑=图库横滑照旧);
  //   capture 只监听不拦截(不吞卡内按钮/单选 click);松手 anchorFromPoint 命中正文才建,落空=放弃。
  (function () {
    var g = null;
    document.addEventListener('pointerdown', function (ev) {
      var im = ev.target && ev.target.closest && ev.target.closest('.vc-card img, .vc-if img, .vc-dk-card img');
      if (!im || !im.src || im.closest('.rc-note')) return;   // 已钉便签里的图不再拖出
      g = { img: im, sx: ev.clientX, sy: ev.clientY, t0: Date.now(), moved: false, id: ev.pointerId };
    }, true);
    document.addEventListener('pointermove', function (ev) {
      if (!g || ev.pointerId !== g.id) return;
      var dist = Math.hypot(ev.clientX - g.sx, ev.clientY - g.sy);
      if (!g.moved) {
        var _adx = Math.abs(ev.clientX - g.sx), _ady = Math.abs(ev.clientY - g.sy);
        // 只有**明显横向**快滑才交还给图库横滑(横 > 纵);纵向拖(拖出正文/拖到另一张卡)立即起拖,不必先静止 300ms
        if (_adx > 10 && _adx > _ady * 1.3 && Date.now() - g.t0 < 300) { g = null; return; }
        if (dist > 10) {
          g.moved = true;
          g.ghost = document.createElement('img'); g.ghost.src = g.img.src;
          g.ghost.style.cssText = 'position:fixed;z-index:2147481470;width:120px;border-radius:10px;pointer-events:none;opacity:.85;box-shadow:0 10px 30px rgba(0,0,0,.4)';
          document.body.appendChild(g.ghost);
        }
      }
      if (g && g.moved && g.ghost) { ev.preventDefault(); g.ghost.style.left = (ev.clientX - 60) + 'px'; g.ghost.style.top = (ev.clientY - 40) + 'px'; _dragFx(ev.clientX, ev.clientY, g.img.closest('.vc-card, .vc-if, .vc-dk-card')); }
    }, true);
    document.addEventListener('pointerup', function (ev) {
      if (!g || ev.pointerId !== g.id) return;
      var was = g; g = null;
      if (was.ghost) { try { was.ghost.remove(); } catch (e) {} }
      _dragFxEnd();
      if (!was.moved) return;
      // 元数据(用户拍板:图的元数据跟着进卡):alt + 图网格标题(.vc-ig-t)
      var _cap = was.img.alt || '';
      try { var _cell = was.img.closest('.vc-ig-cell'); var _t = _cell && _cell.querySelector('.vc-ig-t'); if (_t && _t.textContent.trim()) _cap = _t.textContent.trim(); } catch (e) {}
      var _sourceUrl = (was.img.dataset && was.img.dataset.sourceUrl) || '';
      var _persistentSource = _cardHttpsURL(_sourceUrl) ||
        (/^(?:data:image\/|\/pdf\/api\/(?:asset\/|page-image(?:\?|$)|img-proxy(?:\?|$)))/i.test(_sourceUrl) ? _sourceUrl : '');
      var _currentAttr = String(was.img.getAttribute && was.img.getAttribute('src') || '');
      var _stableCurrent = /^(?:data:image\/|\/pdf\/api\/(?:asset\/|page-image(?:\?|$)|img-proxy(?:\?|$)))/i.test(_currentAttr)
        ? _currentAttr : '';
      var _dragAid = _cardAssetID(was.img.dataset && was.img.dataset.aid);
      var _asrcRaw = _cardAssetURL(_dragAid) ||
        _cardMediaURL(_persistentSource) || _stableCurrent;
      if (!_asrcRaw) {
        try { if (typeof _toast === 'function') _toast('图片来源无效，未粘贴'); } catch (e0) {}
        return;
      }
      var _asrc = esc(_asrcRaw);   // 持久化稳定 asset/proxy 地址，不能保存 App loopback 或扩展 blob 临时 URL
      var _sourceAttr = _persistentSource ? ' data-source-url="' + esc(_persistentSource) + '"' : '';
      var _aidAttr = _dragAid ? ' data-aid="' + esc(_dragAid) + '"' : '';
      var _ih = '<div class="vc-imgdrop"><img' + _aidAttr + _sourceAttr + ' src="' + _asrc + '">' + (_cap ? '<div class="vc-imgdrop-t">' + esc(_cap) + '</div>' : '') + '</div>';
      // ① 落在另一张卡上 → 图进那张卡的**数据层**(元数据跟进,非 DOM-only:三态重渲/回放/上下文/拖整框都带);同编号(cid)所有实例同步;收藏夹持久
      try {
        var _tgt = document.elementFromPoint(ev.clientX, ev.clientY);
        var _tc = _tgt && _tgt.closest && _tgt.closest('.vc-card, .vc-if, .vc-dk-card');
        if (_tc && !_tc.contains(was.img) && !_tc.closest('.rc-note')) {   // 便签内卡暂不作目标(重挂会丢,防半吊子)
          var _cid2 = (_tc.dataset && _tc.dataset.vcCid) || '';
          var _els = [];
          try { _els = (_cid2 && _pins.byCid[_cid2] || []).filter(function (x) { return x.isConnected; }); } catch (e) {}
          if (!_els.length) _els = [_tc];
          var _tcard = _tc.__vcCard || null;   // #img:目标卡数据对象(建卡时挂;同 cid 浮层/侧栏共享同一对象)
          var _dImg = { url: _persistentSource || _asrcRaw, aid: _dragAid, title: _cap, src: 'dragged', _added: 1 };   // 只保存稳定原始来源或 Reader 资产地址;绝不落 App loopback/blob 临时 URL
          if (_tcard && _tcard.kind === 'images' && _tcard.data) {   // 图卡 → push data.items(持久) + 各实例 append 标准 vc-ig-cell(即时;_igWire 委托自动接管 ✕/单选)
            _tcard.data.items = _tcard.data.items || [];
            _tcard.data.items.push(_dImg);
            var _ni = _tcard.data.items.length - 1;
            var _cellH = '<div class="vc-ig-cell" data-i="' + _ni + '">' +
              '<button type="button" class="vc-ig-x" data-i="' + _ni + '" aria-label="移除">✕</button>' +
              '<img class="vc-ig-img" data-i="' + _ni + '"' + (_dImg.aid ? ' data-aid="' + esc(_dImg.aid) + '"' : '') + _sourceAttr + ' src="' + _asrc + '" alt="' + esc(_cap) + '">' +
              (_cap ? '<div class="vc-ig-t">' + esc(_cap) + '</div>' : '') + '</div>';
            _els.forEach(function (el2) { var ig = el2.querySelector('.vc-ig'); if (ig) ig.insertAdjacentHTML('beforeend', _cellH); else { var bd0 = el2.querySelector('.vc-card-bd') || el2; bd0.insertAdjacentHTML('beforeend', _ih); } });
          } else {   // 非图卡/无 card 对象 → DOM 塞入兜底(视觉即时,不落 data)
            _els.forEach(function (el2) { var bd2 = el2.querySelector('.vc-card-bd') || el2; bd2.insertAdjacentHTML('beforeend', _ih); });
          }
          try {   // 收藏夹同 cid 条目:图卡重存 _infoHtml(含新图)、其它 append;服务端保存(重开不丢)
            (_dock.list || []).forEach(function (rec) { if (rec.cid && rec.cid === _cid2) {
              rec.raw = (_tcard && _tcard.kind === 'images') ? ('<div class="vc-if-hd"><span>' + esc(_tcard.title || '配图') + '</span></div>' + _infoHtml(_tcard)) : (String(rec.raw || rec.text || '') + _ih);
              rec.isHtml = true; _favSave(rec);
            } });
          } catch (e) {}
          try { if (typeof _toast === 'function') _toast('已放入卡片' + (_els.length > 1 ? '(同编号 ' + _els.length + ' 处已同步)' : '')); } catch (e) {}
          return;
        }
      } catch (e) {}
      // ② 落在正文 → 图片便签(现有)
      try {
        if (window.RC && RC.stickynote && RC.stickynote.createHtmlAt)
          RC.stickynote.createHtmlAt(ev.clientX, ev.clientY, {
            content: '<img' + _aidAttr + _sourceAttr + ' src="' + _asrc + '" style="max-width:100%;border-radius:8px;display:block">' + (_cap ? '<div style="font-size:11px;opacity:.7;margin-top:3px">' + esc(_cap) + '</div>' : ''),
            isHtml: true, label: _cap || '图片' });   // 松手不在正文=anchorFromPoint 落空,toast 提示,不误钉
      } catch (e) {}
    }, true);
    document.addEventListener('pointercancel', function () { if (g && g.ghost) { try { g.ghost.remove(); } catch (e) {} } g = null; _dragFxEnd(); }, true);
  })();
  window.__vcDragToDock = function (el, payloadFn) { try { _dragToDock(el, payloadFn); } catch (e) {} };
  window.__vcTtsWarm = function () { try { _ttsEnsure(); } catch (e) {} };   // 82:必须在点击手势**同步栈**内调(iOS AudioContext 手势激活)
  window.__vcSpeakText = function (text) {   // 83:TTS 念一段文字(卡片/气泡播放钮);返回 stop 函数
    try { _ttsEnsure(); } catch (e) {}
    try { _speakSafe(String(text || '').slice(0, 4000)); } catch (e) {}
    return function () { try { bargeIn(); } catch (e) {} };
  };
  window.__vcPins = function () {   // 97:文字助手 send 时取当前带入的卡片(非通话模式的上下文注入)
    try {
      // 动态选择快照只在本次请求尾部变化；稳定 id 排序保证同集合字节一致，不触碰 system prompt / 工具表。
      return _effectivePins({ limit: 8, maxText: 2500 }).items;
    } catch (e) { return []; }
  };
  window.__vcTtsStop = function () { try { bargeIn(); } catch (e) {} };   // 97:TTS 生成/播放随时可停(历史▶钮 busy 再点=停)
  window.__vcMicHold = function (on) {   // 82:历史 clip 经 <audio> 播放不在 WebRTC AEC 参考里——播放期禁麦防 AI 听到自己
    try {
      var mt = _rtc.mic && _rtc.mic.getAudioTracks && _rtc.mic.getAudioTracks()[0];
      if (mt) mt.enabled = !on;
    } catch (e) {}
  };
  window.__vcFavBtn = function (el, payloadFn) {   // 78:星形「收藏」钮(信息卡/长回答气泡通用,「!」左侧)
    try {
      if (!el || el.querySelector(':scope > .vc-fav-b')) return;
      el.style.position = 'relative';
      var b = document.createElement('button'); b.type = 'button'; b.className = 'vc-fav-b'; b.title = '加入卡片收藏夹(独立于对话,清空不丢)';
      b.innerHTML = '<svg width="11" height="11" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><path d="M8 2.2l1.8 3.7 4 .58-2.9 2.83.68 4L8 11.4l-3.6 1.9.68-4L2.2 6.48l4-.58z"/></svg>';
      b.addEventListener('click', function (ev) {
        ev.stopPropagation();
        if (b.classList.contains('on')) return;
        var rec = payloadFn();
        rec.cid = rec.cid || (el.dataset && el.dataset.vcCid) || '';
        rec.meta = rec.meta || _favMeta();
        _dockLoad(function () { _favSave(rec); });
        b.classList.add('on');
        if (typeof _toast === 'function') { try { _toast('已收藏'); } catch (e) {} }
      });
      el.appendChild(b);
    } catch (e) {}
  };
  setTimeout(function () { try { _dockLoad(); } catch (e) {} }, 2500);   // 106(用户实测):收藏夹按钮曾是懒加载——有存货但页面加载后不显示,直到做一次收藏;开页主动拉一次
  function _dockBtn() {
    var b = document.getElementById('vc-dock-btn');
    if (!b) {
      b = document.createElement('button'); b.id = 'vc-dock-btn'; b.type = 'button';
      b.innerHTML = '<svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><path d="M2.5 9.5V12a1.5 1.5 0 0 0 1.5 1.5h8A1.5 1.5 0 0 0 13.5 12V9.5M2.5 9.5h3l1 1.6h3l1-1.6h3M4.5 6.5L8 3l3.5 3.5M8 3v6"/></svg><span class="vc-dk-n"></span>';
      b.title = '卡片收藏夹(把浮层卡拖到右下角可收入)';
      b.addEventListener('click', function () { _dock.open = !_dock.open; _dockPanel(_dock.open); });
      document.body.appendChild(b);
    }
    b.querySelector('.vc-dk-n').textContent = String(_dock.list.length);
    b.style.display = _dock.list.length ? 'flex' : 'none';
  }
  function _dockHint(on) {
    var h = document.getElementById('vc-dock-hint');
    if (!h) { h = document.createElement('div'); h.id = 'vc-dock-hint'; document.body.appendChild(h); }
    h.classList.toggle('on', !!on);
  }
  function _inDockZone(x, y) { return (window.innerHeight - y) < 130; }   // 80:拖到屏幕最下端整条边=收入
  // 134:往上拖到左上角=删除(手动消失的通道;跟自动收起/自动消失互补)
  function _trashEl() {
    var t = document.getElementById('vc-trash');
    if (!t) {
      injectCss();
      t = document.createElement('div'); t.id = 'vc-trash';
      t.innerHTML = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">' +
        '<path d="M2.6 4.2h10.8"/><path d="M6.2 4.2V2.9a1 1 0 0 1 1-1h1.6a1 1 0 0 1 1 1v1.3"/>' +
        '<path d="M4 4.2l.6 8.2a1.2 1.2 0 0 0 1.2 1.1h4.4a1.2 1.2 0 0 0 1.2-1.1l.6-8.2"/>' +
        '<path d="M6.6 6.8v4M9.4 6.8v4"/></svg><span>删除</span>';
      document.body.appendChild(t);
    }
    return t;
  }
  function _trashShow(on) { var t = _trashEl(); t.classList.toggle('on', !!on); if (!on) t.classList.remove('hot'); }
  function _trashHot(on) { _trashEl().classList.toggle('hot', !!on); }
  function _inTrashZone(x, y) { return x < 126 && y < 92; }
  function _dockAdd(c) {   // 收入:持久化到服务端(78,清空对话不丢),浮层 DOM 撤
    try { clearTimeout(c.t); } catch (e) {}
    var body = c.bd || c.el.querySelector('.vc-card-bd');
    var cid = c.cid || (c.el.dataset && c.el.dataset.vcCid) || '';
    var rec = null;
    if ((c.kind === 'cards' || (body && body.__fc)) && RC.flashcard &&
        typeof RC.flashcard.snapshot === 'function') {
      var cards = RC.flashcard.snapshot(body);
      var gid = c.gid || (body && body.__fc && body.__fc.gid) || cid;
      if (cards.length && gid) {
        rec = {
          id: gid,
          label: c.label || '学习卡片',
          kind: 'cards',
          payload: {
            version: _FAV_CARDS_PAYLOAD_VERSION,
            kind: 'cards',
            cards: cards
          },
          isHtml: false,
          text: (RC.flashcard.cardsText && RC.flashcard.cardsText(cards)) ||
            ((body || {}).textContent || ''),
          meta: _favMeta(),
          cid: cid || gid,
          gid: gid
        };
      }
    }
    if (!rec) {
      rec = {
        label: c.label || '卡片',
        raw: c.raw || '',
        isHtml: !!c.isHtml,
        text: (body || {}).textContent || '',
        meta: _favMeta(),
        cid: cid
      };
    }
    _dockLoad(function () { _favSave(rec); });
    var i = _cards.list.indexOf(c); if (i >= 0) _cards.list.splice(i, 1);
    try { c.el.remove(); } catch (e) {}
    _cardLayout(); _dockHint(false);
  }
  function _dockPanel(show) {
    var p0 = document.getElementById('vc-dock-panel');
    if (!show) { if (p0) p0.remove(); _dock.open = false; _dock.delMode = false; _dock.trash = false; return; }
    if (!_dock.loaded) { _dockLoad(function () { if (_dock.open) _dockPanel(true); }); }
    if (!p0) { p0 = document.createElement('div'); p0.id = 'vc-dock-panel'; document.body.appendChild(p0); }
    if (!_dock._outside) {   // 84:点面板外=自动关闭(选择模式/回收站视图除外——那是明确的操作态,别误关)
      _dock._outside = function (ev) {
        if (!_dock.open || _dock.delMode || _dock.trash) return;
        var p1 = document.getElementById('vc-dock-panel'), b1 = document.getElementById('vc-dock-btn');
        if ((p1 && p1.contains(ev.target)) || (b1 && b1.contains(ev.target))) return;
        _dockPanel(false); _dockBtn();
      };
      document.addEventListener('pointerdown', _dock._outside, true);
    }
    p0.innerHTML = '';
    // 顶栏:标题 + 删除模式开关(删除模式下再露"回收站"和批量删除)
    var hd = document.createElement('div'); hd.className = 'vc-dkp-hd';
    hd.innerHTML = '<span class="vc-dkp-t">' + (_dock.trash ? '回收站(1 天内可恢复,点卡片=恢复)' : '卡片收藏夹(向上拖出=复制到屏幕)') + '</span>' +
      (_dock.trash ? '<button type="button" class="vc-dkp-b" data-a="back">← 返回</button>'
        : ('<button type="button" class="vc-dkp-b' + (_dock.delMode ? ' on' : '') + '" data-a="delmode">' + (_dock.delMode ? '完成' : '选择') + '</button>' +
           (_dock.delMode ? '<button type="button" class="vc-dkp-b" data-a="trash">回收站</button><button type="button" class="vc-dkp-b danger" data-a="delsel">删除所选</button>' : '')));
    hd.querySelectorAll('.vc-dkp-b').forEach(function (b) {
      b.addEventListener('click', function () {
        var a = b.getAttribute('data-a');
        if (a === 'delmode') { _dock.delMode = !_dock.delMode; _dock.sel = {}; _dockPanel(true); }
        else if (a === 'back') { _dock.trash = false; _dockPanel(true); }
        else if (a === 'trash') {
          _dock.trash = true;
          fetch('/api/assistant/voice-cards?trash=1').then(function (r) { return r.json(); })
            .then(function (d) { _dock.trashList = (d && d.cards) || []; _dockPanel(true); });
        } else if (a === 'delsel') {
          var ids = Object.keys(_dock.sel || {}).filter(function (k) { return _dock.sel[k]; });
          if (!ids.length) return;
          fetch('/api/assistant/voice-cards', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ op: 'del', ids: ids }) }).catch(function () {});
          _dock.list = _dock.list.filter(function (x) { return ids.indexOf(x.id) < 0; });
          _dock.sel = {}; _dock.delMode = false; _dockPanel(true); _dockBtn();
        }
      });
    });
    p0.appendChild(hd);
    // 时间线横滚区:按天分组——天标题在轴线上,竖线垂到卡,卡下标具体时刻
    var sc = document.createElement('div'); sc.className = 'vc-dkp-sc';
    var items = _dock.trash ? (_dock.trashList || []) : _dock.list.slice();
    items.sort(function (a2, b2) { return (a2.ts || 0) - (b2.ts || 0); });
    if (!items.length) { sc.innerHTML = '<div class="vc-dk-empty" style="align-self:center;width:100%">' + (_dock.trash ? '回收站是空的' : '空——把浮层卡拖到屏幕底边,或点卡片上的 ☆') + '</div>'; }
    var lastDay = '';
    items.forEach(function (it) {
      var dt = new Date((it.ts || 0) * 1000);
      var day = (dt.getMonth() + 1) + '月' + dt.getDate() + '日';
      if (day !== lastDay) {   // 天分组:轴线上的日期节点(单线条分隔)
        lastDay = day;
        var dsep = document.createElement('div'); dsep.className = 'vc-dkp-day';
        dsep.innerHTML = '<span>' + esc(day) + '</span>';
        sc.appendChild(dsep);
      }
      var w = document.createElement('div'); w.className = 'vc-dkp-cell';
      var hh = String(dt.getHours()).padStart(2, '0') + ':' + String(dt.getMinutes()).padStart(2, '0');
      var m = it.meta || {};
      var mline = [m.file, m.page && ('p' + m.page)].filter(Boolean).join(' · ');
      w.innerHTML = '<div class="vc-dkp-tick">' + esc(hh) + '</div>' +
        '<div class="vc-dk-card' + ((_dock.delMode && _dock.sel && _dock.sel[it.id]) ? ' del-mark' : '') + '">' +
        '<div class="vc-pc-l">' + esc(it.label) + '</div>' +
        '<div class="vc-dkp-txt">' + esc((it.text || '').replace(/\s+/g, ' ').slice(0, 56)) + '</div>' +
        (mline ? '<div class="vc-dk-m">' + esc(mline) + '</div>' : '') + '</div>';
      var cardEl = w.querySelector('.vc-dk-card');
      if (_dock.trash) {
        cardEl.addEventListener('click', function () {   // 回收站:点卡=恢复
          fetch('/api/assistant/voice-cards', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ op: 'restore', id: it.id }) }).then(function () {
              _dock.trashList = (_dock.trashList || []).filter(function (x) { return x.id !== it.id; });
              _dock.loaded = false; _dockLoad(function () { _dockPanel(true); });
            }).catch(function () {});
        });
      } else if (_dock.delMode) {
        cardEl.addEventListener('click', function () {   // 删除模式:点卡=红标多选
          _dock.sel = _dock.sel || {};
          _dock.sel[it.id] = !_dock.sel[it.id];
          cardEl.classList.toggle('del-mark', !!_dock.sel[it.id]);
        });
      } else {
        if (!it.cid) { it.cid = it.gid || _mkCid(); _favSave(it); }   // 旧学习卡沿用 gid，普通卡才补新 cid
        _pinReg(cardEl, it.cid);
        _pinBind(cardEl, it.label, function () { return it.text || ''; });   // 长按=带入上下文
        (function () {   // 83:向上拖出后**同一手势**直接贴页；无有效正文落点才回退成浮动钉子卡。
          // touch-action:pan-x=横滑归时间线、竖滑归拖出(旧版被滚动手势吃掉=拖不出的根因)。
          var sx0 = 0, sy0 = 0, drag = false, moved = false, ghost = null;
          cardEl.style.touchAction = 'pan-x';
          cardEl.addEventListener('pointerdown', function (ev) {
            sx0 = ev.clientX; sy0 = ev.clientY; drag = true; moved = false;
            try { cardEl.setPointerCapture(ev.pointerId); } catch (e) {}
          });
          cardEl.addEventListener('pointermove', function (ev) {
            if (!drag) return;
            if (!moved && (sy0 - ev.clientY) > 50) {
              moved = true;
              ghost = cardEl.cloneNode(true); ghost.className = 'vc-drag-ghost';
              ghost.style.width = Math.max(240, Math.min(480, Math.round(window.innerWidth * 0.44))) + 'px';
              document.body.appendChild(ghost);
            }
            if (moved && ghost) {
              ev.preventDefault();
              ghost.style.left = (ev.clientX - ghost.offsetWidth / 2) + 'px'; ghost.style.top = (ev.clientY - 26) + 'px';
              var gr0 = ghost.getBoundingClientRect(); _dragFx(gr0.left, gr0.top, cardEl, ghost);
            }
          });
          cardEl.addEventListener('pointerup', function (ev) {
            if (!drag) return; drag = false;
            var gr = null; try { if (ghost) gr = ghost.getBoundingClientRect(); } catch (e) {}
            if (ghost) { ghost.remove(); ghost = null; }
            _dragFxEnd();
            if (!moved) return;
            _dockPanel(false); _dockBtn();
            var px = gr ? gr.left : ev.clientX, py = gr ? gr.top : ev.clientY;
            var pinned = false;
            try { pinned = _payloadPinAt(it, px, py, cardEl); } catch (e) {}
            if (pinned) {
              _placeFx(ev.clientX, ev.clientY);
              try { if (typeof _toast === 'function') _toast('已从收藏夹贴到页面'); } catch (e) {}
            } else {
              var _oc = _payloadFloat(it, cardEl);   // 回退浮层仍是同编号、同状态机实例
              if (_oc) { _oc.pinned = true; _oc.pinMode = true; }
              try { if (typeof _toast === 'function') _toast('没落在正文上——卡片已浮出，可继续拖去钉住'); } catch (e) {}
            }
          });
          cardEl.addEventListener('pointercancel', function () { drag = false; moved = false; if (ghost) { ghost.remove(); ghost = null; } _dragFxEnd(); });
        })();
      }
      sc.appendChild(w);
    });
    p0.appendChild(sc);
    // 83:封面流尺寸——离视口中心越近越大(改宽度=内容自适应变多变少,非几何缩放);snap 停靠中心
    var _fitT = null;
    function _fit() {
      var cx = sc.scrollLeft + sc.clientWidth / 2;
      sc.querySelectorAll('.vc-dkp-cell').forEach(function (cell) {
        var c0 = cell.offsetLeft + cell.offsetWidth / 2;
        var d = Math.abs(c0 - cx) / (sc.clientWidth * 0.55);
        cell.dataset.lvl = d < 0.25 ? '0' : (d < 0.6 ? '1' : '2');
      });
    }
    sc.addEventListener('scroll', function () {
      if (_fitT) return;
      _fitT = requestAnimationFrame(function () { _fitT = null; _fit(); });
    });
    setTimeout(_fit, 60);
  }
  function _chipRender() {   // 77:侧栏开着时,选中内容=输入框上方竖排 chip(与旧上下文 chip 同风格,×=取消带入)
    var wrap = document.getElementById('vc-pin-chips');
    var effective = _effectivePins({ limit: 8, maxText: 2500 });
    var items = effective.items || [];
    if (!_sideOpen() || !items.length) { if (wrap) wrap.remove(); return; }
    var input = document.getElementById('asst-input');
    if (!input) return;
    if (!wrap) {
      wrap = document.createElement('div'); wrap.id = 'vc-pin-chips';
      wrap.style.cssText = 'display:flex;flex-direction:column;gap:5px;padding:6px 10px 0';
      input.parentNode.insertBefore(wrap, document.getElementById('asst-note-chips') || input);
    }
    wrap.innerHTML = '';
    items.forEach(function (item) {
      var k = item.label || item.id;
      var chip = document.createElement('div'); chip.className = 'asst-fig-chip vc-pin-chip';
      chip.innerHTML = '<span class="vc-pc-l">' + esc(k) + '</span><span class="vc-pc-s">' + esc((item.text || '').slice(0, 30)) + '</span>' +
        '<button type="button" class="vc-pc-x" aria-label="移除">✕</button>';
      chip.querySelector('.vc-pc-x').addEventListener('click', function () {
        var el0 = _pins.els[k];
        if (el0 && el0.classList) { el0.classList.remove('vc-picked'); delete el0.dataset.pinLabel; }
        var cid0 = el0 && el0.dataset && el0.dataset.vcCid;
        try { var registry0 = _ctxSelectionRegistry(); if (registry0 && item.id) registry0.deselect(item.id); } catch (e) {}
        _pinForget(k, cid0, el0);
        if (cid0) _pinPaint(cid0, false);
        _pinSync(); _chipRender();
      });
      wrap.appendChild(chip);
    });
  }
  function _cardsVisSync() {   // 77:侧栏开=浮层卡全部消失(不挡内容);关=回来
    var open = _sideOpen();
    var w = document.getElementById('vc-cards');
    if (w) w.style.display = open ? 'none' : '';
    var tl = document.getElementById('vc-tlayer');
    if (tl) tl.style.display = open ? 'none' : '';
    // 卡片被藏起来的这段时间不该计时:开侧栏时把在跑的定时器停掉,关侧栏时重新排 ——
    // 否则你开着侧栏做别的事,回来卡已经自己数完消失了(而它从头到尾没在屏幕上出现过)。
    try {
      (_cards.list || []).forEach(function (c) {
        if (!c || !c.el) return;
        if (open) { try { clearTimeout(c.t); } catch (e) {} c.t = null; }
        else _armAuto(c, c.el);
      });
    } catch (e) {}
    _chipRender();
  }
  // 正文与侧栏的真正空白共用这个事件。持久三态卡只收回标记态；
  // 没有标记态的临时输出卡走原关闭流程，不删除任何固定卡仓记录。
  window.addEventListener('rc:dismiss-transients', function () {
    (_cards.list || []).slice().forEach(function (card) {
      if (!card || !card.el || !card.el.isConnected) return;
      if (card.el.classList.contains('vc-hasdot')) {
        if (!card.el.classList.contains('vc-dot')) {
          try { _cardForm(card.el, 'dot'); } catch (e) {}
        }
        return;
      }
      _cardClose(card);
    });
  });
  // 工具卡图层(用户设计:交错重叠,落点按画面当前占用情况算)——左上角锚定,不进右下堆叠
  function _tlayer() {
    var t = document.getElementById('vc-tlayer');
    if (!t) {
      t = document.createElement('div'); t.id = 'vc-tlayer';
      t.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147481400';
      document.body.appendChild(t);
    }
    return t;
  }
  function _tSpot(w, h) {
    var W = window.innerWidth, H = window.innerHeight, pad = 14;
    var occ = [];
    _cards.list.forEach(function (c) {
      if (!c.el.classList.contains('vc-hasdot') || !c.el.isConnected) return;
      var r = c.el.getBoundingClientRect();
      occ.push({ x: r.left, y: r.top, w: r.width, h: r.height });
    });
    var lim = W;
    try { var sd = document.getElementById('ep-side') || document.getElementById('rc-side');
      if (sd && sd.classList.contains('open')) { var sr = sd.getBoundingClientRect(); if (sr.width > 0) lim = sr.left - 8; } } catch (e) {}
    var best = null, bs = -1e9;
    for (var i = 0; i < 70; i++) {
      var x = pad + Math.random() * Math.max(10, lim - w - pad * 2);
      var y = pad + Math.random() * Math.max(10, H - h - pad * 2 - 110);   // 底部给字幕留位
      var md = 1e9;
      occ.forEach(function (p) {
        var dx = Math.max(p.x - (x + w), x - (p.x + p.w), 0), dy = Math.max(p.y - (y + h), y - (p.y + p.h), 0);
        md = Math.min(md, Math.sqrt(dx * dx + dy * dy));
      });
      var sc = occ.length ? Math.min(md, 140) : 100;
      sc += (x / Math.max(1, lim)) * 40 + (y / H) * 22;   // 偏好右/下,别压正文左上
      if (sc > bs) { bs = sc; best = { x: x, y: y }; }
    }
    return best || { x: Math.max(8, W - w - 20), y: 90 };
  }
  function _cardLayout() {
    var vis = _cards.list.filter(function (c) { return !c.free; });   // 69:拖走的卡脱离堆叠,自由停放
    var n = vis.length;
    vis.forEach(function (c, i) {
      var k = n - 1 - i;
      c.el.style.transform = 'translate(' + (-k * 9) + 'px,' + (-k * 13) + 'px) scale(' + (1 - k * 0.035) + ')';
      c.el.style.zIndex = String(400 - k);
      c.el.style.opacity = k >= 3 ? '0' : '1';   // 只露 3 张,更旧的隐去(数量上限另有裁剪)
    });
  }
  function _cardClose(c) {
    var i = _cards.list.indexOf(c);
    if (i < 0) return;
    try {   // 77:选中的卡被×关闭=同时解除带入(卡都没了,别留幽灵参考)
      var lb = c.el.dataset && c.el.dataset.pinLabel;
      if (lb && _pins.map[lb]) { _pinForget(lb, c.el.dataset && c.el.dataset.vcCid, c.el); _pinSync(); _chipRender(); }
    } catch (e) {}
    _cards.list.splice(i, 1);
    try { clearTimeout(c.t); } catch (e) {}
    c.el.style.opacity = '0';
    setTimeout(function () { try { c.el.remove(); } catch (e) {} }, 320);
    _cardLayout();
  }
  // 钉子模式:把浮动卡钉到书页(内容坐标便签)。制卡卡→card便签(保交互+同gid联动);其它(天气/搜索/图)→html便签(内容快照)。
  //   钉页入口:卡头 📌 按钮(当前卡位)/ 拖到正文松手(该点)。钉成功=浮层卡转页面便签(_cardClose)。所有 vc-card 通用。
  function pinCardToPage(c, x, y) {
    if (!c || !c.el || !(window.RC && RC.stickynote)) return false;
    var el = c.el, bd = c.bd || el.querySelector('.vc-card-bd');
    var pts = [];
    if (x != null && y != null) { pts.push([x, y]); }   // 拖出松手:只认该点(非正文=不钉,落定)
    else { var r = el.getBoundingClientRect(); pts.push([r.left + Math.min(60, r.width / 2), r.top + Math.min(40, r.height / 2)]); pts.push([(window.innerWidth || 1024) / 2, (window.innerHeight || 768) / 2]); }   // 📌 按钮:卡位置 + 视野中央回退(卡落页边/空白时)
    function mk(px, py) {
      try {
        if (bd && bd.__fc && RC.stickynote.createCardAt) {   // 制卡卡:rc-flashcard 状态机 → card 便签
          var st = bd.__fc;
          // 唯一快照出口会保留 Anki/card/note/entity/source id、来源字段和
          // _display* 投影。禁止在这里再次手抄字段，否则钉页就变成失忆副本。
          var snap = (RC.flashcard && typeof RC.flashcard.snapshot === 'function')
            ? RC.flashcard.snapshot(bd)
            : [];
          if (!snap.length) return false;
          return RC.stickynote.createCardAt(px, py, snap, st.gid);
        } else if (bd && RC.stickynote.createHtmlAt) {   // 通用卡:HTML 快照 → html 便签；保留浮层卡 cid
          return RC.stickynote.createHtmlAt(px, py, {
            content: bd.innerHTML,
            // `c.raw` is the source answer passed to _cardPush.  Persist it as
            // AI context instead of re-reading the rendered controls.
            contextText: c.raw || bd.textContent || '',
            isHtml: true,
            label: c.label || '卡片',
            type: (el.style && el.style.getPropertyValue('--vc-tc')) || '',
            icon: (function () { try { var dd = el.querySelector('.vc-card-dot'); return dd ? dd.innerHTML : ''; } catch (e) { return ''; } })(),
            cid: c.cid || (el.dataset && el.dataset.vcCid) || ''
          });
        }
      } catch (e) {}
      return false;
    }
    var ok = false;
    for (var i = 0; i < pts.length && !ok; i++) ok = mk(pts[i][0], pts[i][1]);
    if (ok) { try { _cardClose(c); } catch (e) {} }   // 浮层卡 → 页面便签(转移,不并存)
    return ok;
  }
  function _cardDom(text, kindLabel, isHtml, opts) {
    // 卡片 DOM 构建(从 _cardPush **机械抽出**,浮层卡与钉入书页卡共用同一段渲染 —— 用户拍板"直接复用字幕模式的卡片代码"):
    //   卡头(label+📌+▶+✕)/形态 class/--vc-tc 主题色/TTS 念/bd 填充(mount/isHtml/renderMd)。
    //   浮层专属(cid/定位/关闭/拖动/收纳/自动消失)仍在 _cardPush;钉入侧由 _renderInto 消费。
    opts = opts || {};
    injectCss();
    var _f0 = opts.form || (opts.dot ? 'dot' : 'full');   // 初始形态:工具卡=标记出生;结果卡=方块出生(仍可循环)
    var el = document.createElement('div');
    el.className = 'vc-card' + (opts.dot ? ' vc-hasdot' : '') + (_f0 === 'dot' ? ' vc-dot' : (_f0 === 'min' ? ' vc-min' : ''));
    if (opts.type) el.style.setProperty('--vc-tc', opts.type);
    el.innerHTML = '<div class="vc-card-hd">' + (kindLabel || '文字回复') +
      '<button type="button" class="vc-card-pin" aria-label="钉到书页"><svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9.9 2.1l4 4-2.9 1-1.5 4.4-5-5L8.9 5z"/><path d="M6 10L2.5 13.5"/></svg></button>' +
      '<button type="button" class="vc-card-p" aria-label="念">▶</button>' +
      '<button type="button" class="vc-card-x" aria-label="关闭">' +
      '<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M3 3l6 6M9 3l-6 6"/></svg></button></div>' +
      '<div class="vc-card-sum"></div><div class="vc-card-bd"></div>';
    if (opts.dot) {   // 圆形标记(坐落在方块左上角)= 形态控制按钮:单击 圆 → 长条 → 方块 → 圆
      var dot = document.createElement('button');
      // ⚠ 别在这里写死 busy(那是"正在干活"的呼吸动画):工具卡由状态机(paintSum)开关它,
      //   结果卡(天气/图/视频)根本没有状态机 → 写死就永远在闪(用户实测"点成小方块后一直闪烁")。
      dot.type = 'button'; dot.className = 'vc-card-dot' + (opts.busy ? ' busy' : '');
      dot.innerHTML = opts.icon || '';
      dot.title = '点击切换形态(圆 / 长条 / 方块)';
      dot.addEventListener('click', function (ev) { ev.stopPropagation(); _cycleForm(el); });
      el.appendChild(dot);
    }
    (function () {   // 83:浮层卡 TTS 念(再点=停)
      var pb = el.querySelector('.vc-card-p'), stopFn = null;
      pb.addEventListener('click', function (ev) {
        ev.stopPropagation();
        if (stopFn) { stopFn(); stopFn = null; pb.textContent = '▶'; pb.classList.remove('playing'); return; }
        try { window.__vcTtsWarm && window.__vcTtsWarm(); } catch (e) {}
        var txt = (el.querySelector('.vc-card-bd') || {}).textContent || '';
        stopFn = window.__vcSpeakText ? window.__vcSpeakText(txt) : null;
        pb.textContent = '◼'; pb.classList.add('playing');
        setTimeout(function () { if (stopFn) { pb.textContent = '▶'; pb.classList.remove('playing'); stopFn = null; } }, Math.min(180000, txt.length * 350));
      });
    })();
    var _bd = el.querySelector('.vc-card-bd');
    try {
      if (typeof opts.mount === 'function') { _bd.style.whiteSpace = 'normal'; opts.mount(_bd); }   // ④ 承载可操作状态机卡(制卡卡:rc-flashcard mountDrafts);其余原样
      // isHtml 支路补一次公式排版:结果卡走的就是这条,而本文件此前零处 typeset ——
      // 同一张卡钉进书页时便签壳会 RC.typeset,浮层和侧栏却不会,公式表现三宿主不一致。
      else if (isHtml) {
        _bd.innerHTML = text; _bd.style.whiteSpace = 'normal';
        try { if (window.RC && RC.typeset) RC.typeset(_bd); } catch (e2) {}
      }
      // 三级兜底(对齐 rc-turncard.js:29-37)。renderMd 是在 mountPdfSidebar **函数体内**
      // 导出的,侧栏没挂载的页面上压根不存在;原来直接掉到 textContent,把中间这层
      // RC.md + typeset 漏了 —— 于是那些页面上连纯文本卡也不渲染 Markdown。
      else if (window.RC && RC.assistant && RC.assistant.renderMd) { RC.assistant.renderMd(_bd, text, true); _bd.style.whiteSpace = 'normal'; }
      else if (window.RC && RC.md) {
        _bd.innerHTML = RC.md(text || ''); _bd.style.whiteSpace = 'normal';
        try { if (RC.typeset) RC.typeset(_bd); } catch (e3) {}
      }
      else _bd.textContent = text;
    } catch (e) { _bd.textContent = String(text); }
    return { el: el, bd: _bd, f0: _f0 };
  }
  // 钉入书页渲染(rc-stickynote card/html 便签调):同一 _cardDom → 观感/结构与字幕浮层卡永远一致。
  //   已钉:卡头钉子按钮移除;✕=onClose(删便签);content 里若自带 vc-if-hd 标题条则剥掉(卡头已有 label,浮层同规矩:两条标题栏=bug)
  function _renderInto(host, spec) {
    spec = spec || {};
    // 三态与浮层结果卡同参数(1318:dot:true+form+type+icon)→ 钉入卡同样 标记/长条/方块 单击循环(用户:收缩逻辑要一样)
    var d = _cardDom(spec.text, spec.label, !!spec.isHtml, { type: spec.type, mount: spec.mount, dot: true, form: (spec.form || 'full'), icon: spec.icon || '🗂' });
    var el = d.el;
    var _cid0 = spec.cid || _mkCid();
    var _emitForm = function () { try { spec.onForm && spec.onForm(_cardForm(el)); } catch (e) {} };
    el.__bwCardSizeApply = typeof spec.onSize === 'function' ? spec.onSize : null;
    el.__bwCardFormApply = function () { _emitForm(); };
    _pinReg(el, _cid0);   // 钉页只是同一卡的另一个实例，不得因换宿主重发编号
    el.classList.add('vc-pinned');
    // 历史上以长条态存过的钉入卡:恢复时归一到完全展开。_cardDom 是直接写 class 的,
    //   不经过 _cardForm 的裁剪,所以这一步必须在打上 vc-pinned 之后补。
    if (_cardForm(el) === 'min') _cardForm(el, 'full');
    if (spec.type) el.classList.add('vc-typed');   // 有色磨砂(浮层同规矩 1326:type 色卡 = --vc-tc + vc-typed,卡头/边框/辉光同色)
    try { ['.vc-card-pin', '.vc-card-p', '.vc-card-x'].forEach(function (q) { var b0 = el.querySelector(q); if (b0) b0.remove(); }); } catch (e) {}   // 浮层结果卡同规矩(1328):▶/✕ 去掉;删除=拖到左上角删除区(用户拍板,无叉叉)
    try { var dup = d.bd.querySelector('.vc-if-hd'); if (dup) dup.remove(); } catch (e) {}
    var hd0 = el.querySelector('.vc-card-hd');
    if (hd0) hd0.addEventListener('click', function (ev) { if (ev.target.closest('button')) return; _cycleForm(el); _emitForm(); });   // 展开态标记隐藏→头部就是形态按钮(浮层 2420 同规矩)
    var db0 = el.querySelector('.vc-card-dot');
    if (db0) db0.addEventListener('click', _emitForm);   // 标记 click 已绑 _cycleForm(先注册先执行)→ 这里读新形态回调持久化
    host.appendChild(el);
    // 只有真正钉在页面上的 placement 才拥有尺寸状态。侧栏/收藏/复习虽与
    // 它共享 cid 和选中态，但不会读写或投影这个设备级页面布局。
    _cardSizePageReg(el, _cid0);
    return el;
  }
  // 侧栏对话流渲染(统一壳第三支:rc-toolchip 工具卡 / turnCard 结果卡·制卡卡都调它)——
  //   与浮层 _cardPush、钉入 _renderInto **同一个 _cardDom**,长相/磨砂/三态(圆点·长条·方块)永远一致。
  //   内联进对话流(vc-inflow:relative+100%宽,不 fixed);去 📌▶✕(对话记录不钉页/不念/不删,拖出=dragToDock 副本)。
  //   壳这层只统一"长相+三态";专属交互(pinBind 选中 / dragToDock 拖出 / igWire 图 / mount 状态机)由**调用方拿 el 后自挂**。
  function _renderInflow(host, spec) {
    spec = spec || {};
    var d = _cardDom(spec.text, spec.label, !!spec.isHtml, { type: spec.type, mount: spec.mount, dot: true, form: spec.form || 'full', icon: spec.icon || '🗂' });
    var el = d.el;
    el.classList.add('vc-inflow');
    if (spec.type) el.classList.add('vc-typed');   // 有色磨砂(与浮层/钉入同规矩)
    var _cid1 = spec.cid || _mkCid();
    el.__bwCardSizeApply = typeof spec.onSize === 'function' ? spec.onSize : null;
    _pinReg(el, _cid1);   // 侧栏/历史回放实例出生即登记，和浮层/收藏/页面处处同步
    try { ['.vc-card-pin', '.vc-card-p', '.vc-card-x'].forEach(function (q) { var b0 = el.querySelector(q); if (b0) b0.remove(); }); } catch (e) {}
    try { var dup = d.bd.querySelector('.vc-if-hd'); if (dup) dup.remove(); } catch (e) {}   // 内容自带标题条 → 剥(卡头已有 label,防双标题)
    var hd0 = el.querySelector('.vc-card-hd');
    if (hd0) hd0.addEventListener('click', function (ev) { if (ev.target.closest('button')) return; _cycleForm(el); });   // 展开态头部=形态循环(dot click 已在 _cardDom 绑)
    // 事件不透传(与浮层 _cardPush 同规矩):侧栏卡点击/拖动不冒泡到 document 级监听(点词/选中工具栏/单图拖出),否则"拖动变钉子/误触"
    ['pointerdown', 'pointerup', 'click', 'touchstart', 'touchend', 'dblclick'].forEach(function (evn) {
      el.addEventListener(evn, function (ev) { ev.stopPropagation(); });
    });
    if (host) host.appendChild(el);
    return { el: el, bd: d.bd };
  }
  function _cardPush(text, kindLabel, isHtml, force, cid, opts) {
    opts = opts || {};
    // opts(工具指示器 v2):{tool,type,icon,dot:true 起手标记态,form:初始形态,busy:标记呼吸,
    //   keepAsDot:到点收球不关掉(绑定卡)}
    if (!opts.dot && !opts.mount && (!text || (!isHtml && !text.trim()))) return null;   // mount 模式(制卡状态机卡)无 text,放行
    if (_sideOpen() && !force && !opts.dot) return null;   // 侧栏开着=内容已在对话流,不弹;force=92 拖放例外
    injectCss();
    // ── 按 cid 幂等：同一张卡再来一次是**替换**，不是多出一张 ──────────
    //   这是"写入先落地、连上再重放"能成立的前提。原来 outbox 刻意把浮层卡
    //   排除在持久队列之外，理由写在 ReaderRealtimeOutput.IsDurableMutation
    //   的注释里：「replaying them after an unknown result could duplicate」。
    //   那个顾虑在这之前是**成立的** —— 全文件没有任何一处在建卡前查过
    //   "这个 cid 已经存在了吗"（cid 只在建卡时写、读取时用）。
    //   把它变成不成立的，重放才安全，队列才能覆盖所有卡而不只是绑定卡。
    //
    //   ⚠ 只在调用方**明确给了 cid** 时去重。没给 cid 的是新卡，_mkCid()
    //   每次都不同；不能拿"内容一样"当同一张 —— 用户完全可能要两张一样的。
    if (cid) {
      var _prior = null;
      for (var _pi = 0; _pi < _cards.list.length; _pi++) {
        var _pc = _cards.list[_pi];
        if (_pc && _pc.el && _pc.el.dataset
            && _pc.el.dataset.vcCid === String(cid)) { _prior = _pc; break; }
      }
      if (_prior) {
        // 关掉旧的再往下建新的：比"就地改内容"简单，且天然覆盖
        // 形态/尺寸/计时器全部重置。用户看到的是这张卡被刷新，不是多一张。
        try {
          console.warn('[card] 同 cid 重放，替换既有卡而不是新建', cid);
        } catch (e) {}
        try { _cardClose(_prior); } catch (e) {}
      }
    }
    var w = document.getElementById('vc-cards');
    if (!w) { w = document.createElement('div'); w.id = 'vc-cards'; document.body.appendChild(w); }
    if (force || opts.dot) _cardsVisSync();   // 92:侧栏开着 force 建卡→容器保持隐藏,关侧栏时浮现
    var d0 = _cardDom(text, kindLabel, isHtml, opts);
    var el = d0.el, _f0 = d0.f0, _bd = d0.bd;
    var _cid = cid || _mkCid();   // 95:卡片编号(浮层/侧栏/收藏夹同号 → 选中处处同步)
    el.__bwCardSizeApply = typeof opts.onSize === 'function' ? opts.onSize : null;
    el.dataset.vcCid = _cid;
    // keepAsDot：到点**收起成球**而不是关掉。_armAuto 早就写好了这个分支，但在
    //   2026-08-19 之前**没有任何地方给它赋过值** —— 于是绑定卡到点照样消失。
    //   给绑定卡用：卡片的位置本身就是信息，关掉等于把"这一处有过一次纠正"也丢了。
    var c = { el: el, t: null, free: false, dx: 0, dy: 0, label: kindLabel || '文字回复', raw: text, isHtml: !!isHtml,
              keepAsDot: !!opts.keepAsDot };
    // 85:卡片不可透过——事件在卡内消化,不冒泡到 document 级监听(点词/选中工具栏等都挂 document)
    ['pointerdown', 'pointerup', 'click', 'touchstart', 'touchend', 'dblclick'].forEach(function (evn) {
      el.addEventListener(evn, function (ev) { ev.stopPropagation(); });
    });

    el.querySelector('.vc-card-x').addEventListener('click', function (ev) { ev.stopPropagation(); _cardClose(c); });
    var _pinBtn = el.querySelector('.vc-card-pin');
    if (_pinBtn) _pinBtn.addEventListener('click', function (ev) { ev.stopPropagation(); pinCardToPage(c); });   // 📌 钉到书页(当前卡位)
    el.addEventListener('pointerdown', function () {
      _armAuto(c, el);   // 碰了=在读:倒计时**重新开始**(旧版是永久掐掉 → 点一下就再也不消失了)
      _cards.topZ = (_cards.topZ || 500) + 1; el.style.zIndex = String(_cards.topZ);   // 69:点击=置顶
    });
    // 72:双击=收起/展开 —— **三态卡(opts.dot)已退役这个手势**:单击就是三态循环,双击会连触发两次、
    //    还跟这个 toggle 打架(用户问"双击到底对应什么" → 答案:三态卡上什么都不对应,已删)。
    //    非三态卡(普通文字卡/收藏夹拖出的副本)保留旧行为。
    if (!opts.dot) {
      el.addEventListener('dblclick', function (ev) {
        if (ev.target.closest('.vc-card-x')) return;
        ev.preventDefault();
        var min = !el.classList.contains('vc-min');
        if (min) {
          try {
            var _sm = el.querySelector('.vc-card-sum');
            if (_sm && !_sm.textContent) _sm.textContent = (el.querySelector('.vc-card-bd').textContent || '').replace(/\s+/g, ' ').trim().slice(0, 42) + '…';
          } catch (e) {}
        }
        el.classList.toggle('vc-min', min);
      });
    }
    // 69/72:按住头部拖动——物理感:阈值内粘住不动;拽过阈值"弹起"(微放大+深阴影)跟手;松手落定回弹。
    // 旧版粘滞/闪烁根因:堆叠布局的 transform 0.38s 过渡在拖动中每帧追赶——拖动期必须 transition:none
    var hd = el.querySelector('.vc-card-hd');
    _bindCardDrag(hd);
    if (opts.dot) {
      _bindCardDrag(el.querySelector('.vc-card-dot'));   // 收起态:头部隐藏,标记自己当把手
      // 展开后标记不显示 → **头部就是形态按钮**(与标记同一条循环,方向必须一致)
      hd.addEventListener('click', function (ev) {
        if (ev.target.closest('button')) return;
        _cycleForm(el);
      });
    }
    function _bindCardDrag(hd) {
      if (!hd) return;
      hd.style.cursor = 'grab'; hd.style.touchAction = 'none';
      var moved = false, baseDx = 0, baseDy = 0, baseFree = false, baseTransition = '', baseOrigin = '';
      return _bindChargedDrag(hd, {
        holdMs: 420,
        slop: 8,
        dragSlop: 1,
        feedbackEl: el,
        canStart: function (ev) {
          var control = ev.target && ev.target.closest &&
            ev.target.closest('.vc-card-x,.vc-card-pin,.vc-card-p,button,a,input,textarea,select,[contenteditable="true"]');
          if (control && !(control === hd && hd.classList.contains('vc-card-dot'))) return false;
          el.dataset.downT = String(Date.now());   // 真短点才由既有 click 切形态；ready 后 helper 会吞合成 click
          moved = false;
          baseDx = c.dx || 0; baseDy = c.dy || 0; baseFree = !!c.free;
          baseTransition = el.style.transition; baseOrigin = el.style.transformOrigin;
          return true;
        },
        onReady: function () {
          try { if (typeof el.__bwCancelPinHold === 'function') el.__bwCancelPinHold(); } catch (e0) {}
        },
        onMove: function (session, e2) {
          if (!moved && session.moved) {
            moved = true;
            try { if (typeof el.__bwCancelPinHold === 'function') el.__bwCancelPinHold(); } catch (e0) {}
            c.free = true;
            el.style.transition = 'none';                     // 跟手期零动画(粘滞/闪烁的根治)
            if (c.pinMode) el.style.transformOrigin = '0 0';   // #51:钉子卡 scale 左上原点——拖动中左上=钉入位置
            el.classList.add('vc-lift');                       // 弹起态:scale 1.03+深阴影
            _cardLayout();
          }
          if (!moved) return;
          c.dx = baseDx + session.dx; c.dy = baseDy + session.dy;
          el.style.transform = 'translate(' + c.dx + 'px,' + c.dy + 'px) scale(1.03)';
          el.dataset.touched = '1';                           // 动过 → 出结果不自动展开
          _dockHint(_inDockZone(e2.clientX, e2.clientY));      // 77b:接近底部=收藏区光晕提示
          _trashShow(true);                                    // 134:拖起来就亮出左上角删除区
          _trashHot(_inTrashZone(e2.clientX, e2.clientY));
          if (c.pinMode) { try { var _er9 = el.getBoundingClientRect(); _dragFx(_er9.left + 1, _er9.top + 1, el, el); } catch (e9) {} }   // #51:探测点=卡左上角+1(=钉入点);隐自身穿透
        },
        onEnd: function (session, e3) {
          var wasMoved = moved && session.moved;
          moved = false;
          el.dataset.downT = '';
          _dockHint(false); _trashShow(false); _dragFxEnd();
          if (!wasMoved) { el.classList.remove('vc-lift'); return; }
          if (e3 && _inTrashZone(e3.clientX, e3.clientY)) {   // 134:往上拖到左上角=删除(手动消失通道)
            try { el.style.transition = 'transform .26s ease,opacity .26s ease'; el.style.transform = 'translate(' + c.dx + 'px,' + c.dy + 'px) scale(.5)'; el.style.opacity = '0'; } catch (e) {}
            setTimeout(function () { _cardClose(c); }, 200);
            try { if (typeof _toast === 'function') _toast('已删除'); } catch (e) {}
            return;
          }
          if (e3 && _inDockZone(e3.clientX, e3.clientY)) { _dockAdd(c); return; }   // 77b:松手在区内=收入收藏夹
          if (c.pinMode && e3 && window.RC && RC.stickynote) {   // 钉子模式卡(侧栏/收藏夹拖出源):松手=钉入,**方块左上角位置**就是钉入点(用户拍板)
            var _pr = el.getBoundingClientRect();
            if (pinCardToPage(c, _pr.left + 1, _pr.top + 1)) return;   // 落正文=钉住(偏移 +1 与拖动视觉对齐);非正文=落定卡保持显示
          }
          // 落定:带一点弹性回落(overshoot 曲线),像重新"粘"回桌面
          el.style.transition = 'transform .38s cubic-bezier(.34,1.56,.64,1),box-shadow .3s,opacity .32s';
          el.classList.remove('vc-lift');
          el.style.transform = 'translate(' + c.dx + 'px,' + c.dy + 'px)';
        },
        onCancel: function () {
          var hadMoved = moved;
          moved = false;
          el.dataset.downT = '';
          _dockHint(false); _trashShow(false); _dragFxEnd();
          el.classList.remove('vc-lift');
          if (!hadMoved) return;
          c.dx = baseDx; c.dy = baseDy; c.free = baseFree;
          el.style.transition = baseTransition;
          el.style.transformOrigin = baseOrigin;
          el.style.transform = 'translate(' + c.dx + 'px,' + c.dy + 'px)';
          try { _cardLayout(); } catch (e) {}
        },
        onClickSuppressed: function () {
          el.dataset.downT = '';
          el.dataset.dragged = '';
        }
      });
    }
    if (opts.dot) {   // 工具卡:左上角锚定 + 交错重叠落点(不进右下堆叠,免得展开时往左上长、标记乱跑)
      c.free = true;
      var sp = _tSpot(330, 40);   // 按**展开态**的宽度找位(326px),否则靠右落点展开时会溢出屏幕
      el.style.left = sp.x + 'px'; el.style.top = sp.y + 'px';
      _tlayer().appendChild(el);
    } else {
      w.appendChild(el);
    }
    _cards.list.push(c);
    var _plain = _cards.list.filter(function (x) { return !x.el.classList.contains('vc-hasdot'); });
    while (_plain.length > 4) {   // 70:被选中(带入上下文)的卡不被数量裁剪挤掉;工具卡不参与裁剪
      var victim = null;
      for (var vi = 0; vi < _plain.length - 1; vi++) { if (!_plain[vi].el.classList.contains('vc-picked')) { victim = _plain[vi]; break; } }
      if (!victim) break;
      _cardClose(victim); _plain.splice(_plain.indexOf(victim), 1);
    }
    requestAnimationFrame(_cardLayout);
    _armAuto(c, el);
    c.cid = _cid;
    _pinReg(el, _cid);   // 登记进 cid 注册表:选中态处处同步
    return c;
  }
  // 138(用户设计):倒计时到点 → **卡片完全消失**(不是收起成小方块)。
  //   唯一豁免 = **长按选中**(紫边):选中的留下,没选中的到点全清。
  //   碰它(点/拖/切形态)= 你在看它 → 倒计时**重新开始**;松开不管它,照样会走。
  function _armAuto(c, el) {
    try { clearTimeout(c.t); } catch (e) {}
    c.t = null;
    if (!_cardHideOn()) return;
    // 侧栏开着时浮层卡只是 display:none(_cardsVisSync),**倒计时照跑** —— 卡在你看不见的
    // 时候自己数完然后没了。看不见就不该计时:这里直接不排,_cardsVisSync 关侧栏时补排。
    if (_sideOpen() && !c.keepAsDot) return;
    c.t = setTimeout(function () {
      if (el.classList.contains('vc-picked')) return;   // 选中 = 唯一豁免
      if (c.pinned) return;   // 钉子模式 = 豁免(拖出/侧栏收藏夹来源默认钉住,不自动消失)
      // 绑定到页面元素的卡:到点**收起成球,不关掉**。它的位置本身就是信息 ——
      // 关掉等于把"这一处有过一次纠正"这件事一起丢了,而球留在锚点上还认得出来。
      if (c.keepAsDot) { try { _cardForm(el, 'dot'); } catch (e2) {} return; }
      _cardClose(c);
    }, _cardSecs() * 1000);
  }

  // ── 形态循环(唯一入口,用户设计)──
  //   顺序恒为 小方块 → 长条 → 方块 → 小方块。⚠ 标记和头部必须**同方向**,否则:长条态标记是隐藏的、
  //   只有头部可点,若头部反着走就成了 长条↔小方块 死循环,永远到不了方块(用户实测)。
  //   ⚠ 只认**短按抬手**:长按 = 选中(_pinBind 600ms),拖动 = 移动 —— 这两种松手都不该改形态。
  var LP_MS = 600;   // 与 _pinBind 的长按阈值同口径
  function _cycleForm(el) {
    if (el.dataset.dragged === '1') { el.dataset.dragged = ''; return; }        // 刚拖过
    var down = +(el.dataset.downT || 0);
    if (down && Date.now() - down >= LP_MS - 60) { el.dataset.downT = ''; return; }   // 长按(已被判成选中)
    el.dataset.downT = '';
    el.dataset.touched = '1';   // 手动动过 → 出结果不再自动展开/自动收起(尊重用户摆放)
    var f = _cardForm(el);
    _cardForm(el, f === 'dot' ? 'min' : (f === 'min' ? 'full' : 'dot'));
  }
  // 形态读写:'dot'(圆) / 'min'(长条) / 'full'(方块)。浮层/钉入卡三态;**侧栏内联卡(vc-inflow)只 min/full 两态**——
  //   实测(2026-07-21):对话流里圆点态缩成孤立 40×40 小圆(像钉子按钮)且与拖动手势冲突(用户实锤"拖动变钉子"),
  //   对话记录不需要"缩圆点省空间"(那是浮层飘屏的需求),故内联卡不进圆点。壳/磨砂/正文仍与字幕卡完全统一。
  function _cardForm(el, f) {
    if (f === undefined) return el.classList.contains('vc-dot') ? 'dot' : (el.classList.contains('vc-min') ? 'min' : 'full');
    if (f === 'dot' && el.classList.contains('vc-inflow')) f = 'min';   // 内联卡圆点→长条(对话流不缩圆点,消除"拖动变钉子")
    // 钉在书页上的卡**不进长条态**(用户 2026-08-18 拍板):长条存在的理由是"在一堆卡里
    //   给个大概的信息概要",而钉住的卡**锚点本身就已经说明了它是关于什么的** ——
    //   概要与锚点重复,于是这一态从来没被用到。固定后只留 标记 ⇄ 完全展开 两态。
    //   (与上一行内联卡跳过圆点是同一手法:形态循环按宿主裁剪,而不是给每个宿主另造一套。)
    if (f === 'min' && el.classList.contains('vc-pinned')) f = 'full';
    el.classList.toggle('vc-dot', f === 'dot');
    el.classList.toggle('vc-min', f === 'min');
    if (f === 'min') {   // 长条:没摘要就从正文摘一行
      var sm = el.querySelector('.vc-card-sum');
      if (sm && !sm.textContent) sm.textContent = ((el.querySelector('.vc-card-bd') || {}).textContent || '').replace(/\s+/g, ' ').trim().slice(0, 42);
    }
    try { _cardLayout(); } catch (e) {}
    return f;
  }

  // ── 61 TTS 通用开关:文字输出流式切句代念(不等全文,尽快开口)。57 韧性(通道保证)+麦守护单例 ──
  // 67b:通道未就绪时的待念队列(用户实锤"只读最后一段"——朗读 WS 建立要 1-2s,流式前几句各自
  // 900ms 重试全死在建立窗口里被 speak 静默丢弃,只有末段赶上通道就绪。改队列:就绪瞬间按原序全放)
  var _sq = [], _sqT = null;
  function _sqPump() {
    if (_sqT) return;
    var t0 = Date.now();
    _sqT = setInterval(function () {
      if (_tts.ws && _tts.ws.readyState === 1) {
        clearInterval(_sqT); _sqT = null;
        _sq.splice(0).forEach(function (x) { try { speak(x); } catch (e) {} });
      } else if (Date.now() - t0 > 12000) {   // 12s 还没就绪=通道真起不来,放弃这批(别攒到下轮突然全念)
        clearInterval(_sqT); _sqT = null; _sq.length = 0;
        try { setSt('通话中(朗读通道未就绪,这段没念出来)'); } catch (e) {}
      }
    }, 250);
  }
  function _speakSafe(t) {
    if (!t || !t.trim()) return;
    // 67:2.1 的音频(等待语)可能还在 WebRTC 播放队列里——按估计的播放结束时刻延迟代念,不跟它叠音;
    // 禁麦(_ttsMicGuard)也等真正开念才生效,等待期间用户仍可抢话
    var wait = Math.max(0, (_rtc.aEnd || 0) - Date.now());
    var _go = function () {
      _ttsMicGuard();
      if (_tts.ws && _tts.ws.readyState === 1 && !_sq.length && !_sqT) { try { speak(t); } catch (e) {} }
      else {   // 未就绪(或队列在途,保序不插队):入队,通道就绪后按序放出
        _sq.push(t);
        try { _ttsEnsure(); } catch (e) {}
        _sqPump();
      }
    };
    if (wait > 0) setTimeout(_go, wait); else _go();
  }
  function _mkTtsFeeder() {   // 每个文字流一个 feeder:增量文本按句边界切片喂朗读(关着=只跟进偏移不念)
    var fed = 0;
    return function (full, fin) {
      if (!_ttsOn()) { fed = full.length; return; }
      var rest = full.slice(fed);
      if (fin) { if (rest.trim()) { _speakSafe(rest); } fed = full.length; return; }
      var idx = -1;
      for (var i = rest.length - 1; i >= 0; i--) { if ('。!?!?\n;;…'.indexOf(rest[i]) >= 0) { idx = i; break; } }
      if (idx >= 4) { _speakSafe(rest.slice(0, idx + 1)); fed += idx + 1; }
    };
  }
  var _turnFeed = null;   // 当前回复轮的代念 feeder(response.created 新建)
  var _tg = null;         // 麦守护单例:代念播放期禁麦(本地 WebAudio 不在 WebRTC AEC 参考里,不禁=模型听见自己)
  // ── 「可打断代念」模式(用户设计,131)──
  //   条件:GPT(2.1-mini,WebRTC)通话中 + 会用到 TTS 代念(文字/混合/路由档 且 代念开关开)。
  //   进 = 给 TTS 的 AudioContext 建 AEC 环回 → 代念声进回声消除的参考信号被消掉 → **不必禁麦**
  //        → 你随时开口即打断(VAD 收得到 speech_started),模型也听得见你。
  //   退 = 切回纯语音档 / 关掉代念 / 挂断 → 拆掉环回(省两条 pc + 一个 audio 元素),禁麦回到兜底位。
  function _ttsIrqWant() {
    try { return !!(_rtc.on && _ttsOn() && _voiceMode() !== 'sts'); } catch (e) { return false; }
  }
  function _ttsIrqSync() {
    var want = _ttsIrqWant();
    if (want && !_taec.ready) {
      if (!_tts.ac) { try { _ttsEnsure(); } catch (e) {} }        // 通道没起 → 先起(手势链外也无妨,AEC 不需要手势)
      if (_tts.ac) { try { _ttsAecSetup(_tts.ac); } catch (e) {} }
      try { setSt('通话中 · 代念可随时打断'); } catch (e) {}
    } else if (!want && _taec.ready) {
      _aecKill(_taec);                                            // 退出:环回拆掉,禁麦回到兜底位
    }
  }
  window.__vcTtsIrqSync = _ttsIrqSync;
  function _ttsMicGuard() {
    // 130:环回在 → 代念已进 AEC 参考、会被消掉 → **不禁麦**(禁了就打断不了,模型也听不见你)
    if (_taec.ready) return;
    var mt0 = _rtc.mic && _rtc.mic.getAudioTracks && _rtc.mic.getAudioTracks()[0];
    if (!mt0) return;
    if (_tg) { _tg.last = Date.now(); return; }   // 已在守护:刷新活动时间(流式多段 speak 不重启)
    mt0.enabled = false;
    _tg = { last: Date.now(), t0: Date.now(), sawPlay: false, quiet: 0 };
    var iv = setInterval(function () {
      if (!_tg) { clearInterval(iv); return; }
      var playing = false; try { playing = _tts.playing.length > 0; } catch (e) {}
      if (playing) { _tg.sawPlay = true; _tg.quiet = 0; } else if (_tg.sawPlay) _tg.quiet++;
      var dead = !_tg.sawPlay && Date.now() - _tg.t0 > 6000 && Date.now() - _tg.last > 3000;   // 一直没响=通道坏,别耗死麦
      var fin = _tg.sawPlay && _tg.quiet >= 3 && Date.now() - _tg.last > 1500;                 // 播完+短静默+没有新句
      var hard = Date.now() - _tg.t0 > 180000;
      if (dead || fin || hard) {
        clearInterval(iv); _tg = null;
        try { mt0.enabled = true; } catch (e) {}
        if (dead) { try { _cap.pend = []; setSt('通话中(朗读通道未就绪,这段没念出来)'); } catch (e) {} }
      }
    }, 400);
  }
  function _rtcRespCreate(src, longTool, options) {   // ㊿b 手动挡:按四态模式+来源选输出模态(每轮读当前档=通话中热切)
    var m = _voiceMode();
    // 61/66c:sts=全语音;half=提问语音·工具/深度文字;stt=全文字;route=语音,但**长工具结果轮程序切文字**
    // (0/4 实锤 mini 拿到资料+音频模态必念,prompt 治不了——短结果口头说,长结果它自己文字写,无截断)
    var wantAudio = (m === 'sts') || (m === 'route' && !longTool) || (m === 'half' && src === 'user');
    _rtc.turnText = !wantAudio;   // 本轮是文字输出:TTS 开关开着就流式代念
    // 64 用户拍板:全档 2048(≈100s 音频保险丝,正常轮碰不到)——不搞小预算硬截断,时长靠 prompt+route 自觉
    var response = { output_modalities: [wantAudio ? 'audio' : 'text'], max_output_tokens: 2048 };
    // 新笔迹是本轮唯一视觉目标：首个 response 直接锁定 see_ink，避免模型先读文字、
    // 再看整页、最后才看笔迹。工具结果后的 response 则由调用方传 toolChoice:'none'，
    // 保证只生成一次最终回答，不再串行调第二个视觉工具。
    if (options && Object.prototype.hasOwnProperty.call(options, 'toolChoice')) {
      response.tool_choice = options.toolChoice;
    } else if (src === 'user' && _rtcHasFreshInk()) {
      response.tool_choice = { type: 'function', name: 'see_ink' };
    }
    if (options && options.metadata) response.metadata = options.metadata;
    return _dcSend({ type: 'response.create', response: response });
  }
  function _rtcPendingToolCount() {
    return Object.keys(_rtc.pendingToolCalls || {}).length;
  }
  function _rtcFlushToolResponse() {
    var pending = _rtc.pendingToolResponse;
    if (!pending || _rtc.responseActive || _rtcPendingToolCount()) return false;
    _rtc.pendingToolResponse = null;
    var sent = _rtcRespCreate(pending.src, pending.longTool, pending.options);
    if (!sent) {
      _rtc.pendingToolResponse = pending;
      try { if (window.dlog) window.dlog('tool← 正式回答创建失败:data channel 未就绪', '#ff6b6b'); } catch (e) {}
    } else {
      try { if (window.dlog) window.dlog('tool← 工具结果已回填，开始生成正式回答', '#7be096'); } catch (e) {}
    }
    return sent;
  }
  function _rtcQueueToolResponse(src, longTool, options) {
    var old = _rtc.pendingToolResponse;
    var merged = Object.assign({}, (old && old.options) || {}, options || {});
    // Any visual result's tool_choice:none must survive aggregation with a
    // concurrently completed ordinary tool.
    if ((old && old.options && old.options.toolChoice === 'none') ||
        (options && options.toolChoice === 'none')) merged.toolChoice = 'none';
    if (options && options.metadata) merged.metadata = options.metadata;
    _rtc.pendingToolResponse = {
      src: src === 'deep' || (old && old.src === 'deep') ? 'deep' : 'tool',
      longTool: !!(longTool || (old && old.longTool)),
      options: merged
    };
    // function_call_arguments.done can precede its response.done. Creating a
    // response now races the still-active preamble and is rejected by Realtime.
    if (!_rtc.responseActive && !_rtcPendingToolCount()) {
      return _rtcFlushToolResponse();
    }
    return true;
  }
  function _rtcTrackToolCall(callId) {
    if (!callId) return;
    _rtc.pendingToolCalls = _rtc.pendingToolCalls || Object.create(null);
    _rtc.pendingToolCalls[callId] = true;
  }
  function _rtcFinishToolCall(callId) {
    if (callId && _rtc.pendingToolCalls) delete _rtc.pendingToolCalls[callId];
    _rtcFlushToolResponse();
  }
  function _rtcResponseHasFunctionCall(event) {
    if (_rtc.responseToolCalls > 0) return true;
    var output = event && event.response && event.response.output;
    return Array.isArray(output) && output.some(function (item) {
      return item && item.type === 'function_call';
    });
  }
  function _rtcNewInkAck() {
    _rtc.inkAckSeq = (_rtc.inkAckSeq || 0) + 1;
    return 'ink_' + Date.now().toString(36) + '_' + _rtc.inkAckSeq.toString(36);
  }
  function _rtcCompleteToolTurn(name, ok, versionAtStart, pageAtStart, callId, out, longTool, silent, turnEpochAtStart) {
    var stale = turnEpochAtStart != null && turnEpochAtStart !== (_rtc.turnEpoch || 0);
    var toolOutput = stale ? JSON.stringify({
      error: '该工具属于已被新问题取代的旧用户轮；调用已闭合，结果已丢弃',
      stale_turn: true
    }) : out;
    var outputSent = _dcSend({
      type: 'conversation.item.create',
      item: { type: 'function_call_output', call_id: callId, output: toolOutput }
    });
    var visualTarget = /^(see_ink|see_page|see_figure)$/.test(name);
    var responseSent = !!silent || stale;
    var ack = '';
    if (!silent && !stale && outputSent) {
      var responseOptions = visualTarget ? { toolChoice: 'none' } : {};
      if (name === 'see_ink' && ok) {
        ack = _rtcNewInkAck();
        responseOptions.metadata = { bw_ink_ack: ack };
        _rtc.inkResponseAcks = _rtc.inkResponseAcks || Object.create(null);
        _rtc.inkResponseAcks[ack] = {
          name: name, ok: ok, ver: versionAtStart, page: pageAtStart,
          epoch: turnEpochAtStart
        };
      }
      responseSent = _rtcQueueToolResponse(
        name === 'deep_think' ? 'deep' : 'tool',
        ok && String(out || '').length > 800,
        responseOptions
      );
      if (!responseSent && ack) delete _rtc.inkResponseAcks[ack];
    }
    var complete = !!(outputSent && responseSent);
    // see_ink 不在本地 send() 后消费；response.done(status=completed) 会用 metadata
    // 关联服务端真正接受并完成的那一条最终回答。拒绝/断线/新用户轮都保留 fresh。
    return { outputSent: outputSent, responseSent: responseSent, complete: complete, stale: stale, ack: ack };
  }
  function _rtcFinishInkAck(event) {
    var response = event && event.response;
    var marker = response && response.metadata && response.metadata.bw_ink_ack;
    var pending = marker && _rtc.inkResponseAcks && _rtc.inkResponseAcks[marker];
    if (!pending) return;
    delete _rtc.inkResponseAcks[marker];
    if (response.status === 'completed' && pending.epoch === (_rtc.turnEpoch || 0)) {
      _rtcMarkInkSeen(pending.name, pending.ok, pending.ver, pending.page);
    }
  }
  // 创造物库清单(与文字侧同源:/api/assistant/creations-brief → _creations_recent_line)。
  //   stale-while-revalidate:开口时用缓存注入,距上次拉取 >15s 就后台刷新;工具完成(_chipEnd)也刷新。
  function _rtcCreFetch() {
    if (_rtc.nativeDirect) return;
    var now = Date.now();
    if (now - (_rtcCreFetch._t || 0) < 15000) return;
    _rtcCreFetch._t = now;
    fetch('/api/assistant/creations-brief').then(function (r) { return r.json(); })
      .then(function (d) { if (d && d.ok) _rtc.creLine = d.line || ''; }).catch(function () {});
  }
  function _rtcFetchPageText(pk) {   // 页文本兜底拉取(去重:同 key 只飞一发)
    if (_rtc.nativeDirect) return;
    if (_rtc._ptBusy === pk) return;
    _rtc._ptBusy = pk;
    var _f = pk.slice(0, pk.lastIndexOf(':')), _p = pk.slice(pk.lastIndexOf(':') + 1);
    fetch('/api/assistant/voice-page-text?file=' + encodeURIComponent(_f) + '&page=' + encodeURIComponent(_p))
      .then(function (r) { return r.json(); })
      .then(function (d) { _rtc._ptBusy = ''; if (d && d.ok && d.text) _rtc._ptCache = { k: pk, t: d.text }; })
      .catch(function () { _rtc._ptBusy = ''; });
  }
  function _rtcFlushCtx() {   // ㊵ 拉模式核心:用户开口/发文字的瞬间才注入"他正看着的位置+可见内容"(同状态去重)
    // 127:注入走**前端自己的 data channel**(浏览器→OpenAI 一跳)。绕 relay 的旧路 = OpenAI→Pi→OpenAI
    //   跨海往返,短问题时注入常赶不上 VAD 判完,模型只好凭空答(截图里"谁"那次就是)。
    try {
      var vt = _rtc.pendText || '';
      if (!vt && _rtc.nativeDirect) vt = _nativeRealtimePageText();
      if (!vt && _rtc.ctxFile && _rtc.ctxPage) {   // 扫描书/图片模式:前端可见文本采不到 → 服务端 _page_text 兜底(OCR+钉入便签/卡片注入)
        // 用户注入策略(2026-07-20):翻到页后停留 ≥8s(在读)且 ≤12min(话题还新鲜)→ 开口注入;窗外不注入省 token(模型可 read_page)
        var _dwell = Date.now() - (_rtc.pageTs || 0);
        if (!_rtc.pageTs || (_dwell >= 8000 && _dwell <= 720000)) {
          var _pk = _rtc.ctxFile + ':' + _rtc.ctxPage;
          if (_rtc._ptCache && _rtc._ptCache.k === _pk) vt = _rtc._ptCache.t || '';
          else _rtcFetchPageText(_pk);   // 预拉没赶上/通话刚建:现场补拉,下一轮开口注入
        }
      }
      // ★创造物库告知(与文字侧同一个源;替代旧 __lastCheckResult 专线):只注入告知+句柄,内容 recall 取。
      _rtcCreFetch();
      var cre = (_rtc.creLine || '').replace(/\n/g, ';');
      var rcHint = cre
        ? ('。最近创造物(之前工具的产出;句柄=#id,**内容不在这里**,用 recall_creation(id=…)取回):' + cre
           + '。用户提到"刚才查的/搜的/那张纸/第几题的答案"→ **先 recall_creation 再答**;纸类条目会给题目+标准答案+检查报告——'
           + '题目是纸上自制的,书里没有逐字题目,别去 search_book 找题目原文')
        : '';
      // 圈画告知(用户拍板 2026-07-20:有笔迹**一律 see_ink** 看真实圈画——几何提取的'圈中文字'不可靠,不喂 AI)
      var freshInk = _rtcHasFreshInk();
      var ikHint = freshInk
        ? '。⚠ 本页有模型尚未看过的**新笔迹**；这是本轮最高优先级对象，直接且只调用 see_ink。其合成图已包含笔迹附近页面，不要先 read_selection/read_page，也不要随后 see_page'
        : ((_rtc.ink && _rtc.ink.length)
          ? '。本页有既存笔迹；用户明确提到圈画、手写、箭头或算式时调用 see_ink'
          : '');
      // 选中文字是用户此轮明确指向的对象。只在他开口/发文字时随同
      // 当前视口注入，不因单纯拖选就让模型主动开口；App 本机直连与
      // Pi 控制侧带因此保持同一语义。
      var sel = String(_rtc.sel || '').trim();
      var selHint = sel
        ? (freshInk
          ? '。他当前也有选区「' + sel.slice(0, 1000) + '」，但本轮新笔迹优先；see_ink 完成后再依据用户问题决定是否引用选区'
          : '。他当前明确选中了这段文字:「' + sel.slice(0, 1000) + '」——他说『这个/这段/这里』时优先指这段')
        : '';
      var fp = _rtc.ctxPage + '/' + (_rtc.ctxTotal || 0) + ':' + vt.length + ':' + vt.slice(0, 30) + ':' + cre.length + ':' + cre.slice(0, 24) + ':' + ((_rtc.ink && _rtc.ink.length) || 0) + ':' + (_rtc.inkVer || 0) + '/' + (_rtc.inkSeenVer || 0) + ':' + sel.length + ':' + sel.slice(0, 40);
      if (fp === _rtc._sentCtxFp) return;
      _rtc._sentCtxFp = fp;
      _rtcSys('(用户此刻在第 ' + _rtc.ctxPage + ' 页/章' + (_rtc.ctxTotal ? '(全书共 ' + _rtc.ctxTotal + ' 页)' : '') +
              (vt ? ',当前可见内容:' + vt.slice(0, 1500) : ',需要页面内容就调 read_page') + selHint + ikHint + rcHint +
              '。回答以本条为准;状态记录,不要回应本条。)');
    } catch (e) {}
  }
  async function _rtcDeep(q) {   // deep_think:转交侧栏 chat(Claude)拿完整回答回填,GPT 自己念
    if (!q) return '(问题为空)';
    try {
      var r = await fetch('/api/assistant/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: '(语音深度思考,直接给最终回答,口语化短句)\n' + q, rid: 'rtc' + Date.now(),
                               voice: 's2s', context: _rtc.ctxFile ? { file_rel: _rtc.ctxFile, page: _rtc.ctxPage } : {} }) });
      var txt = await r.text(), answer = '', ev = '', data = '';
      txt.split('\n').forEach(function (line) {
        if (line.indexOf('event:') === 0) ev = line.slice(6).trim();
        else if (line.indexOf('data:') === 0) data += line.slice(5).trim();
        else if (!line.trim()) {
          if (ev === 'answer' && data) { try { var p = JSON.parse(data); if (typeof p === 'string') answer = p.split(/\n?FOLLOWUP[::]/)[0]; } catch (e) {} }
          ev = ''; data = '';
        }
      });
      return (answer || '(没拿到回答)').slice(0, 3000);
    } catch (e) { return '(深度思考失败:' + String(e).slice(0, 100) + ')'; }
  }
  // ㉟c 视口截图(用户设计"把即时的重叠渲染后的结果交给AI"):html2canvas 自托管懒加载,
  //    截当前可见区域(正文+手写笔迹+插入页 overlay 所见即所得);EPUB 看图/看笔迹恒用,
  //    PDF 在服务端渲不出时(插入页未写回等)兜底。排除侧栏/通话条/字幕等悬浮 UI。
  var _h2cP = null;
  // 回退截图库的地址必须相对于**本文档实际的加载基址**,不能写死站点根。
  //
  // App 的本机运行时把阅读器挂在带能力令牌的前缀下(/r/<token>/...),于是写死的
  // '/static/pdf/html2canvas.min.js' 在 App 内 404 —— 加载失败 → reject → 上游 catch
  // → null,对外只剩一句"无图"。本文件自己是被成功加载进来的,它的 src 必然可达,
  // 所以以它的同目录为基准。
  function _h2cSrc() {
    try {
      var tag = document.querySelector('script[src*="rc-voicecall"]');
      var src = tag && tag.getAttribute('src');
      if (src) return new URL(src, document.baseURI).href
        .replace(/rc-voicecall[^/]*$/, 'html2canvas.min.js');
    } catch (e) {}
    return '/static/pdf/html2canvas.min.js';
  }
  function _loadH2C() {
    if (window.html2canvas) return Promise.resolve();
    if (_h2cP) return _h2cP;
    var url = _h2cSrc();
    _h2cP = new Promise(function (res, rej) {
      var s = document.createElement('script');
      s.src = url;
      s.onload = function () {
        // onload 只保证脚本执行完毕,不保证它导出了全局。两者要的修法不同,分开报。
        if (window.html2canvas) { res(); return; }
        _h2cP = null;
        rej(new Error('截图库已加载但未导出 html2canvas'));
      };
      s.onerror = function () { _h2cP = null; rej(new Error('截图库加载失败 ' + url)); };
      document.head.appendChild(s);
    });
    return _h2cP;
  }
  // 规则一/二(silent-failure-lessons):每个提前返回都要出声;折成 null 之前先说清原始情形。
  // 这条路径此前一律 catch→return null,于是"库没加载""画布被跨源污染""截出来是空白"
  // 这些要求完全不同修法的死因,对外是同一句"无图"。
  function _visualErrText(e) {
    if (!e) return '未知';
    return String((e && (e.message || e.name)) || e).slice(0, 160);
  }
  function _visualNull(where, why) {
    _visualStep(where + ' 放弃: ' + why);
    return null;
  }

  // App 本机的原生层级合成图。
  //
  // html2canvas 只画得到 WKWebView 内部,拿不到独立的 PencilKit 原生覆盖层,也无法
  // 离屏复现 PDFKit 底页与本机权威墨迹。原生 drawHierarchy 截的是整个可见视图层级,
  // 因而屏内的"底页+笔迹+卡片"天然就是同一张图。
  //
  // 基址里带能力令牌,所以这里只判存在性,任何分支都不把它写进日志。
  function _nativeCaptureBase() {
    try {
      var v = String(window.__BW_NATIVE_LOCAL_BASE_PATH__ || '');
      return /^\/r\/[a-f0-9]{64}$/.test(v) ? v : null;
    } catch (e) { return null; }
  }
  async function _nativeCapture(scope, rect, page, delivery) {
    var base = _nativeCaptureBase();
    // 非 App 本机(Safari/PWA)不是故障,是预期路径:静默回落 html2canvas。
    if (!base) return null;
    var q = 'scope=' + encodeURIComponent(scope);
    if (page != null) q += '&page=' + encodeURIComponent(String(page));
    if (rect) {
      q += '&x=' + rect.x.toFixed(6) + '&y=' + rect.y.toFixed(6) +
           '&w=' + rect.w.toFixed(6) + '&h=' + rect.h.toFixed(6);
    }
    var request = { cache: 'no-store' };
    if (delivery) {
      q += '&deliver=realtime';
      request.method = 'POST';
      request.headers = { 'Content-Type': 'application/json' };
      request.body = JSON.stringify({
        call_id: delivery.call_id,
        client_secret: delivery.client_secret,
        tool: delivery.tool
      });
      _visualStep('原生直投 ' + scope + ' 开始(图像不经过 JS)');
    }
    var resp;
    try {
      resp = await fetch(base + '/native-api/visual-capture?' + q, request);
    } catch (e) {
      if (delivery) {
        return {
          native_delivery_failed: true,
          error: '原生直投请求失败 ' + _visualErrText(e)
        };
      }
      return _visualNull('原生合成图', '请求失败 ' + _visualErrText(e));
    }
    if (!resp.ok) {
      var code = '';
      try { code = resp.headers.get('X-BW-Reader-Error') || ''; } catch (e2) {}
      var detail = '';
      try {
        var errorText = await resp.text();
        var errorBody = errorText ? JSON.parse(errorText) : null;
        detail = String((errorBody && errorBody.error) || '').slice(0, 160);
      } catch (e3) {
        detail = String(errorText || '').slice(0, 160);
      }
      if (delivery) {
        var directError = 'HTTP ' + resp.status + (code ? ' ' + code : '') +
          (detail ? ' ' + detail : '');
        _visualStep('原生直投失败 ' + directError);
        return { native_delivery_failed: true, error: directError };
      }
      if (!code) code = detail;
      return _visualNull('原生合成图', 'HTTP ' + resp.status + (code ? ' ' + code : ''));
    }
    if (delivery) {
      var delivered;
      try { delivered = await resp.json(); } catch (e5) {
        return {
          native_delivery_failed: true,
          error: '原生直投返回了无效 JSON'
        };
      }
      if (!delivered || delivered.ok !== true || delivered.delivered !== true) {
        return {
          native_delivery_failed: true,
          error: String((delivered && delivered.error) || '原生直投未确认完成').slice(0, 180)
        };
      }
      var directBytes = Number(delivered.bytes) || 0;
      var directItemID = /^bwi_[a-f0-9]{28}$/.test(String(delivered.item_id || ''))
        ? String(delivered.item_id) : '';
      _visualStep('原生直投完成 ' + Math.round(directBytes / 1024) + 'KB' +
        (delivered.ink === 'none' ? '(该页无笔迹)' : ''));
      return {
        media_type: 'image/jpeg',
        native_delivered: true,
        item_id: directItemID,
        byte_count: directBytes,
        ink: String(delivered.ink || 'unknown'),
        capture: String(delivered.capture || '')
      };
    }
    var b64 = '';
    try {
      var buf = new Uint8Array(await resp.arrayBuffer());
      var step = 0x8000, parts = [];
      for (var i = 0; i < buf.length; i += step) {
        parts.push(String.fromCharCode.apply(null, buf.subarray(i, i + step)));
      }
      b64 = btoa(parts.join(''));
    } catch (e4) {
      return _visualNull('原生合成图', '编码失败 ' + _visualErrText(e4));
    }
    if (b64.length <= 3000) {
      return _visualNull('原生合成图', '图过小 ' + b64.length + 'B(疑空白)');
    }
    // 「这页本来就没画过」是有效答案,不是故障 —— 图照常返回,但要说出来,
    // 否则模型会把一张干净的底页当成"笔迹看不清"。
    var inkState = '';
    try { inkState = resp.headers.get('X-BW-Visual-Ink') || ''; } catch (e5) {}
    _visualStep('原生合成图 ' + scope + ' 得到 ' + Math.round(b64.length / 1024) + 'KB' +
      (inkState === 'none' ? '(该页无笔迹)' : ''));
    return { media_type: 'image/jpeg', b64: b64 };
  }

  // 页内归一化框 → 视口归一化框,并说明有多少落在屏幕内。
  //
  // 原生 region 的坐标系是**视口**,而笔迹外接框算在**页元素**内 —— 两者只有该页
  // 完全可见时才重合。不换算就会请求到错误位置;而原生侧会把越界部分 intersect 掉,
  // 于是返回一张构图错误却看起来正常的图。安静的错图比失败更糟,所以越界要说出来。
  //
  // 滚出视口的笔迹需要离屏合成(PDFKit 渲页 + 本机权威墨迹重绘),那是 scope=page
  // 的职责;目标没有可用 PDF 页号时如实报告,而不是交付一张裁错的图。
  function _viewportRectFromPageRect(el, x0, y0, x1, y1) {
    try {
      var r = el.getBoundingClientRect();
      var vw = window.innerWidth || 0, vh = window.innerHeight || 0;
      if (!(r.width > 0 && r.height > 0 && vw > 0 && vh > 0)) return null;
      var out = {
        x: (r.left + x0 * r.width) / vw,
        y: (r.top + y0 * r.height) / vh,
        w: ((x1 - x0) * r.width) / vw,
        h: ((y1 - y0) * r.height) / vh
      };
      var visW = Math.max(0, Math.min(1, out.x + out.w) - Math.max(0, out.x));
      var visH = Math.max(0, Math.min(1, out.y + out.h) - Math.max(0, out.y));
      var area = out.w * out.h;
      out.visible = area > 0 ? (visW * visH) / area : 0;
      return out;
    } catch (e) { return null; }
  }
  async function _nativeInkRegion(el, x0, y0, x1, y1, delivery) {
    if (!_nativeCaptureBase()) return null;
    var rect = _viewportRectFromPageRect(el, x0, y0, x1, y1);
    // 视口内:走 region —— 那是屏幕层级合成,卡片、高亮等可见 UI 都在图里。
    if (rect && rect.visible >= 0.9) {
      var shot = await _nativeCapture('region', rect, null, delivery);
      if (shot) return shot;
    }
    // 屏外:改按页离屏合成。x0..y1 本就是页内归一化,正好是 scope=page 要的坐标系。
    // 该路径只有 PDF 底页与笔迹/选区,没有卡片 —— 取舍写在日志里,免得看图的人以为丢了东西。
    var pageNo = _inkPageNumber(el);
    var offRatio = rect ? Math.round(rect.visible * 100) : 0;
    if (pageNo) {
      _visualStep('笔迹 ' + offRatio + '% 在视口内,改按第 ' + pageNo + ' 页离屏合成(无卡片层)');
      return await _nativeCapture(
        'page', { x: x0, y: y0, w: x1 - x0, h: y1 - y0 }, pageNo, delivery
      );
    }
    if (!rect) return _visualNull('原生合成图', '无法换算到视口坐标');
    return _visualNull('原生合成图',
      '笔迹仅 ' + offRatio + '% 在视口内,且该页无 PDF 页号(插入页/EPUB),无法离屏合成');
  }
  // 可选 DocumentHost 视觉表面。PDF/EPUB 没实现时原路径完全不变；普通网页只在 adapter
  // 提供真实正文元素、布局尺寸和 canonical strokes，不复制截图/绘制算法。
  //
  // 实时快照会高频查询选区索引。缺少这个可选能力是一个稳定状态,不是每次查询都新发生的
  // 故障；只在能力状态变化时报告一次。若 adapter 稍后补上/撤下方法,下一次查询仍会重新
  // 探测并报告新的状态,不会把真正的运行时变化永久缓存掉。
  var _visualSurfaceCapabilityState = '';
  function _visualSurfaceGetter(ad) {
    var getter = ad && typeof ad.getVisualSurface === 'function'
      ? ad.getVisualSurface : null;
    var state = !ad ? 'no-adapter' : (getter ? 'available' : 'unsupported');
    if (state !== _visualSurfaceCapabilityState) {
      _visualSurfaceCapabilityState = state;
      if (state === 'no-adapter') _visualNull('原生取图面', '无 adapter');
      if (state === 'unsupported') {
        _visualNull('原生取图面', 'adapter 未实现 getVisualSurface');
      }
    }
    return getter;
  }
  function _visualSurface() {
    try {
      var ad = window.RC && RC.adapter ? RC.adapter() : null;
      var getter = _visualSurfaceGetter(ad);
      if (!getter) return null;
      var s = getter.call(ad);
      if (!s) return _visualNull('原生取图面', 'getVisualSurface 返回空');
      if (!s.element) return _visualNull('原生取图面', '缺 element');
      if (!(s.width > 0) || !(s.height > 0)) {
        return _visualNull('原生取图面', '尺寸非法 ' + s.width + 'x' + s.height);
      }
      s.strokes = Array.isArray(s.strokes) ? s.strokes : [];
      return s;
    } catch (e) { return _visualNull('原生取图面', _visualErrText(e)); }
  }
  function _visualCaptureScope(target) {
    var scope = target && typeof target === 'object' ? target.scope : null;
    return scope === 'viewport-context' || scope === 'drawing-nearby' ||
      scope === 'selection-near' ? scope : null;
  }
  function _visualCaptureSelectionId(target) {
    var value = target && typeof target === 'object' ? target.selectionId : null;
    return typeof value === 'string' && /^[A-Za-z0-9._:-]{1,160}$/.test(value)
      ? value : null;
  }
  // Reader page ink uses `p`, while App-owned native PDF/EPUB snapshots use
  // `pts`.  Visual capture is a consumer of both authorities, so it must not
  // silently discard the App form before the native Realtime bridge gets a
  // chance to send the composite.
  function _visualStrokePoints(stroke) {
    if (!stroke || typeof stroke !== 'object') return [];
    if (Array.isArray(stroke.p) && stroke.p.length) return stroke.p;
    if (Array.isArray(stroke.pts)) return stroke.pts;
    return Array.isArray(stroke.p) ? stroke.p : [];
  }
  function _surfaceInkCrop(s, selectionId) {
    var x0 = 1, y0 = 1, x1 = 0, y1 = 0;
    s.strokes.forEach(function (st) {
      if (selectionId && !(st && st.t === 'region' && st.id === selectionId)) return;
      _visualStrokePoints(st).forEach(function (pt) {
        var px = Number(pt[0]), py = Number(pt[1]);
        if (!Number.isFinite(px) || !Number.isFinite(py)) return;
        x0 = Math.min(x0, Math.max(0, Math.min(1, px)));
        y0 = Math.min(y0, Math.max(0, Math.min(1, py)));
        x1 = Math.max(x1, Math.max(0, Math.min(1, px)));
        y1 = Math.max(y1, Math.max(0, Math.min(1, py)));
      });
    });
    if (!(x1 >= x0 && y1 >= y0)) return null;
    var bx0 = x0 * s.width, by0 = y0 * s.height, bx1 = x1 * s.width, by1 = y1 * s.height;
    var bw = Math.max(1, bx1 - bx0), bh = Math.max(1, by1 - by0);
    var pad = Math.max(36, Math.min(180, Math.max(bw, bh) * 0.16));
    var vp = s.viewport || {};
    var wantW = Math.max(bw + pad * 2, Math.min(Number(vp.width) || 420, 420));
    var wantH = Math.max(bh + pad * 2, Math.min(Number(vp.height) || 280, 280));
    var cx = (bx0 + bx1) / 2, cy = (by0 + by1) / 2;
    var left = Math.max(0, Math.min(s.width - Math.min(wantW, s.width), cx - wantW / 2));
    var top = Math.max(0, Math.min(s.height - Math.min(wantH, s.height), cy - wantH / 2));
    return {
      x: left, y: top,
      width: Math.min(wantW, s.width),
      height: Math.min(wantH, s.height)
    };
  }
  function _drawSurfaceInk(canvas, s, crop, selectionId) {
    if (!canvas || !s.strokes.length || !window.RCInk || !RCInk.drawStroke) return;
    var ctx2 = canvas.getContext('2d');
    var sx = canvas.width / Math.max(1, crop.width), sy = canvas.height / Math.max(1, crop.height);
    var regionNumbers = RCInk.ensureRegionOrdinals
      ? RCInk.ensureRegionOrdinals(s.strokes) : null;
    var regions = s.strokes.filter(function (st) { return st && st.t === 'region'; });
    regions.sort(function (a, b) {
      var at = Number(a.createdAtEpochMs) || 0, bt = Number(b.createdAtEpochMs) || 0;
      if (at !== bt) return at - bt;
      return String(a.id || '').localeCompare(String(b.id || ''));
    });
    var ordered = s.strokes.slice();
    if (selectionId) {
      ordered.sort(function (a, b) {
        return (a && a.id === selectionId ? 1 : 0) -
          (b && b.id === selectionId ? 1 : 0);
      });
    }
    ordered.forEach(function (st) {
      var selected = !!(selectionId && st && st.t === 'region' && st.id === selectionId);
      var clone = {
        t: st.t || 'pen',
        id: st.id,
        createdAtEpochMs: st.createdAtEpochMs,
        c: selected ? '#0a84ff' : (st.c || '#e74c3c'),
        w: selected ? Math.max(3.5, Number(st.w) || 2) : (Number(st.w) || 2.5),
        p: _visualStrokePoints(st).map(function (pt) {
          return [
            ((Number(pt[0]) * s.width) - crop.x) / crop.width,
            ((Number(pt[1]) * s.height) - crop.y) / crop.height
          ];
        })
      };
      if (clone.p.length) RCInk.drawStroke(
        ctx2,
        clone,
        canvas.width,
        canvas.height,
        Math.min(sx, sy),
        st && st.t === 'region' ? {
          regionNumber: (regionNumbers && regionNumbers.get(st)) ||
            Number(st.ordinal) || regions.indexOf(st) + 1
        } : undefined
      );
    });
  }
  async function _captureSurface(s, crop, selectionId) {
    if (!s || !crop || !(crop.width > 0) || !(crop.height > 0)) return null;
    await _loadH2C();
    var longEdge = Math.max(crop.width, crop.height, 1);
    // Web ink reports document.body as its geometry surface, while the
    // extension's cards/notes portal is a sibling under <html>.  Capture the
    // whole document tree only for that web shape; PDF/EPUB keep their exact
    // renderer surface.  The ink SVG is excluded from the web base and drawn
    // once below so crop coordinates and selected-region emphasis stay exact.
    var webPinRoot = s.element === document.body &&
      document.getElementById('bw-reader-pins');
    var captureRoot = webPinRoot ? (document.documentElement || s.element) : s.element;
    var canvas = await window.html2canvas(captureRoot, {
      x: Math.round(crop.x), y: Math.round(crop.y),
      width: Math.round(crop.width), height: Math.round(crop.height),
      scale: Math.min(2, window.devicePixelRatio || 1, 1600 / longEdge),
      useCORS: true, logging: false, backgroundColor: '#ffffff',
      ignoreElements: function (el) {
        var id = el.id || '';
        var classes = el.classList;
        return id === 'bw-reader-host' || (!webPinRoot && id === 'bw-reader-pins') ||
               id === 'ep-side' || id === 'rc-vc' || id === 'vc-cap' ||
               id === 'word-pop' || id === 'sel-toolbar' ||
               !!(webPinRoot && classes &&
                  (classes.contains('bw-ink-document') ||
                   classes.contains('bw-ink-canvas')));
      }
    });
    _drawSurfaceInk(canvas, s, crop, selectionId);
    var b64 = '', qs = [0.85, 0.7, 0.5];
    for (var qi = 0; qi < qs.length; qi++) {
      b64 = (canvas.toDataURL('image/jpeg', qs[qi]).split(',')[1]) || '';
      if (b64.length <= 900000) break;
    }
    return b64.length > 3000 ? { media_type: 'image/jpeg', b64: b64 } : null;
  }
  async function _captureView(delivery) {
    try {
      var nat = await _nativeCapture('viewport', null, null, delivery);
      if (nat) return nat;
      var surface = _visualSurface();
      if (surface) {
        var vp = surface.viewport || {};
        return await _captureSurface(surface, {
          x: Math.max(0, Number(vp.x) || 0),
          y: Math.max(0, Number(vp.y) || 0),
          width: Math.min(surface.width, Number(vp.width) || window.innerWidth),
          height: Math.min(surface.height, Number(vp.height) || window.innerHeight)
        });
      }
      await _loadH2C();
      // 60 尺寸上限(审核P1):长边≤1600px——2×DPR 整视口无上限时复杂页 base64 可超服务端消息上限,断的是整条控制 WS
      var longEdge = Math.max(window.innerWidth, window.innerHeight);
      // 普通网页的卡片/便签 portal (#bw-reader-pins) 与 body 同为 html
      // 的直接子节点；以 body 为根会在进入裁剪前就把 portal 丢掉。
      // 改从 documentElement 合成整棵页面树，同时继续排除固定控制 UI。
      var captureRoot = document.documentElement || document.body;
      var canvas = await window.html2canvas(captureRoot, {
        x: window.scrollX, y: window.scrollY,
        width: window.innerWidth, height: window.innerHeight,
        scale: Math.min(2, window.devicePixelRatio || 1, 1600 / longEdge),
        useCORS: true, logging: false, backgroundColor: '#ffffff',
        ignoreElements: function (el) {
          var id = el.id || '';
          return id === 'bw-reader-host' ||
                 id === 'ep-side' || id === 'rc-vc' || id === 'vc-cap' ||
                 id === 'word-pop' || id === 'sel-toolbar';
        }
      });
      // 质量阶梯:编码后目标 ≤900KB base64(0.8→0.6→0.45),超了逐级降质而不是赌网关上限
      var b64 = '';
      var qs = [0.8, 0.6, 0.45];
      for (var qi = 0; qi < qs.length; qi++) {
        b64 = (canvas.toDataURL('image/jpeg', qs[qi]).split(',')[1]) || '';
        if (b64.length <= 900000) break;
      }
      // 太小=截了个寂寞(空白/失败)。这跟"截图库没加载"是两回事,分开说。
      if (b64.length <= 5000) return _visualNull('视口截图', '图过小 ' + b64.length + 'B(疑空白)');
      return { media_type: 'image/jpeg', b64: b64 };
    } catch (e) { return _visualNull('视口截图', _visualErrText(e)); }
  }
  // 通用原语(用户点子:前端渲染截图通用性强,统一覆盖各种取图):截**任意元素**为图(所见即所得)。
  //   检查纸(pdf-uishared)、笔迹查看(see_ink 插入页/覆盖层)、以后别处都走这一条,不再各写各的。
  async function _captureEl(el) {
    try {
      if (!el) return null;
      await _loadH2C();
      var longEdge = Math.max(el.offsetWidth || 0, el.offsetHeight || 0, 1);
      var canvas = await window.html2canvas(el, {
        useCORS: true, logging: false, backgroundColor: '#ffffff',
        scale: Math.min(2, window.devicePixelRatio || 1, 1600 / longEdge),
        ignoreElements: function (e2) { var id = e2.id || ''; return id === 'ep-side' || id === 'rc-vc' || id === 'vc-cap' || id === 'word-pop' || id === 'sel-toolbar'; }
      });
      var b64 = '', qs = [0.85, 0.7, 0.5];
      for (var qi = 0; qi < qs.length; qi++) { b64 = (canvas.toDataURL('image/jpeg', qs[qi]).split(',')[1]) || ''; if (b64.length <= 900000) break; }
      if (b64.length <= 3000) return _visualNull('元素截图', '图过小 ' + b64.length + 'B(疑空白)');
      return { media_type: 'image/jpeg', b64: b64 };
    } catch (e) { return _visualNull('元素截图', _visualErrText(e)); }
  }
  function _compositeTargetPage(target) {
    if (target == null) return null;
    var page = (typeof target === 'object') ? target.page : target;
    return (page == null || page === '') ? null : String(page);
  }
  function _compositePageMatches(el, targetPage) {
    if (targetPage == null) return true;
    var page = null;
    if (el && el.dataset) {
      if (el.dataset.pageNum != null) page = el.dataset.pageNum;
      else if (el.dataset.idx != null) {
        var sectionIdx = parseInt(el.dataset.idx, 10);
        page = Number.isFinite(sectionIdx) ? sectionIdx + 1 : el.dataset.idx;
      }
      else if (el.dataset.uid != null) page = el.dataset.uid;
    }
    if (page == null && el && el.__upRec && el.__upRec.page != null) {
      page = el.__upRec.page;
    }
    return page != null && String(page) === targetPage;
  }
  function _nativePDFCompositePage(targetPage) {
    var pageNumber = Number(targetPage);
    if (!Number.isSafeInteger(pageNumber) || pageNumber < 1) return null;
    try {
      var adapter = window.RC && typeof RC.adapter === 'function'
        ? RC.adapter() : null;
      return adapter && adapter.config && adapter.config.isPDF === true
        ? pageNumber : null;
    } catch (e) { return null; }
  }
  async function _captureBodyPageRect(rect, surface, surfaceCrop, selectionId) {
    try {
      if (!rect || !(rect.width > 0) || !(rect.height > 0)) return null;
      await _loadH2C();
      var longEdge = Math.max(rect.width, rect.height, 1);
      var captureRoot = document.documentElement || document.body;
      var canvas = await window.html2canvas(captureRoot, {
        x: Math.max(0, Math.round((window.scrollX || 0) + rect.left)),
        y: Math.max(0, Math.round((window.scrollY || 0) + rect.top)),
        width: Math.round(rect.width),
        height: Math.round(rect.height),
        scale: Math.min(2, window.devicePixelRatio || 1, 1600 / longEdge),
        useCORS: true, logging: false, backgroundColor: '#ffffff',
        // 卡片/便签可能是 body 的 portal 兄弟节点。这里以整棵 html
        // 为渲染源再裁页，不能排除 bw-reader-pins / vc-cards。墨迹 SVG
        // 会在下方按精确 crop/selection 再补画一次，因此底图必须排除，
        // 否则 html2canvas 与 _drawSurfaceInk 会把同一笔迹叠画两遍。
        ignoreElements: function (el) {
          var id = el.id || '';
          var classes = el.classList;
          return id === 'bw-reader-host' ||
                 id === 'ep-side' || id === 'rc-vc' || id === 'vc-cap' ||
                 id === 'word-pop' || id === 'sel-toolbar' ||
                 !!(classes && (classes.contains('bw-ink-document') ||
                                classes.contains('bw-ink-canvas')));
        }
      });
      if (surface && surfaceCrop) {
        _drawSurfaceInk(canvas, surface, surfaceCrop, selectionId);
      }
      var b64 = '', qs = [0.85, 0.7, 0.5];
      for (var qi = 0; qi < qs.length; qi++) {
        b64 = (canvas.toDataURL('image/jpeg', qs[qi]).split(',')[1]) || '';
        if (b64.length <= 900000) break;
      }
      if (b64.length <= 3000) return _visualNull('页区域截图', '图过小 ' + b64.length + 'B(疑空白)');
      return { media_type: 'image/jpeg', b64: b64 };
    } catch (e) { return _visualNull('页区域截图', _visualErrText(e)); }
  }
  async function _captureSurfaceCompositeCrop(surface, crop, selectionId) {
    if (!surface || !surface.element || !crop) return null;
    var rect = surface.element.getBoundingClientRect();
    var bodyRect = {
      left: (surface.element === document.body ? -(window.scrollX || 0) : rect.left) + crop.x,
      top: (surface.element === document.body ? -(window.scrollY || 0) : rect.top) + crop.y,
      width: crop.width,
      height: crop.height
    };
    return await _captureBodyPageRect(
      bodyRect,
      surface,
      crop,
      selectionId
    ) || await _captureSurface(surface, crop, selectionId);
  }
  async function _captureViewportComposite(delivery) {
    // 语义与原生 scope=viewport 完全对应:屏幕上那一块。
    var nat = await _nativeCapture('viewport', null, null, delivery);
    if (nat) return nat;
    var surface = _visualSurface();
    if (surface) {
      var vp = surface.viewport || {};
      var cropX = Math.max(0, Number(vp.x) || 0);
      var cropY = Math.max(0, Number(vp.y) || 0);
      var crop = {
        x: cropX,
        y: cropY,
        width: Math.min(
          Math.max(0, surface.width - cropX),
          Number(vp.width) || window.innerWidth
        ),
        height: Math.min(
          Math.max(0, surface.height - cropY),
          Number(vp.height) || window.innerHeight
        )
      };
      return await _captureSurfaceCompositeCrop(surface, crop, null);
    }
    return await _captureBodyPageRect({
      left: 0,
      top: 0,
      width: window.innerWidth,
      height: window.innerHeight
    }) || await _captureView(delivery);
  }
  // 当前逻辑页的完整“正文 + 墨迹 + 页内卡片/便签/高亮”合成图。
  // 只复用本文件已有 html2canvas/_captureSurface/_captureEl，不另造渲染器：
  // - 普通网页以当前视口 body 合成并补画 canonical strokes；
  // - PDF/EPUB/插入页以 body 为渲染源按精确页矩形裁切，保留外层 portal；
  // - 宿主尚未装载目标元素时回退当前视口，保持旧语音链可用。
  async function _capturePageComposite(target) {
    try {
      var delivery = target && target.__native_delivery;
      var requestedScope = _visualCaptureScope(target);
      if (requestedScope === 'viewport-context') {
        return await _captureViewportComposite(delivery);
      }
      if (requestedScope === 'drawing-nearby' || requestedScope === 'selection-near') {
        return await _captureInkRegion(target);
      }
      var targetPage = _compositeTargetPage(target);
      var nativePDFPage = _nativePDFCompositePage(targetPage);
      if (nativePDFPage) {
        // see_page means the complete logical PDF page, not the current
        // viewport. PDFKit can render it off-screen and the native broker
        // overlays persisted PencilKit ink before delivering it directly.
        var nativePage = await _nativeCapture(
          'page', null, nativePDFPage, delivery
        );
        if (nativePage) return nativePage;
      }
      var els = document.querySelectorAll(
        '.page-wrap[data-page-num], .pdf-upage, ' +
        '.ep-sec[data-idx], .ep-usec[data-uid]'
      );
      var fallback = null;
      for (var i = 0; i < els.length; i++) {
        var el = els[i];
        if (!_compositePageMatches(el, targetPage)) continue;
        var r = el.getBoundingClientRect();
        if (!(r.width > 0) || !(r.height > 0)) continue;
        if (!fallback) fallback = el;
        if (r.bottom > 0 && r.top < (window.innerHeight || 0)) {
          return await _captureBodyPageRect(r) || await _captureEl(el);
        }
      }
      if (fallback) {
        var fallbackRect = fallback.getBoundingClientRect();
        return await _captureBodyPageRect(fallbackRect) ||
          await _captureEl(fallback);
      }
      var surface = _visualSurface();
      if (surface) {
        var vp = surface.viewport || {};
        var crop = {
          x: Math.max(0, Number(vp.x) || 0),
          y: Math.max(0, Number(vp.y) || 0),
          width: Math.min(
            surface.width,
            Number(vp.width) || window.innerWidth
          ),
          height: Math.min(
            surface.height,
            Number(vp.height) || window.innerHeight
          )
        };
        var composite = await _captureBodyPageRect({
          left: 0,
          top: 0,
          width: window.innerWidth,
          height: window.innerHeight
        }, surface, crop);
        if (composite) return composite;
        if (surface.strokes.length) return await _captureSurface(surface, crop);
      }
      return await _captureView(delivery);
    } catch (e) {
      return _captureView(target && target.__native_delivery);
    }
  }
  function _inkTargetPage(target) {
    if (target == null) return null;
    var page = (typeof target === 'object') ? target.page : target;
    return (page == null || page === '') ? null : String(page);
  }
  function _inkPageMatchesTarget(el, targetPage) {
    if (targetPage == null) return true;   // 旧语音链无参调用:保持"首个可见墨迹页"
    var page = null;
    if (el && el.dataset) {
      if (el.dataset.pageNum != null) page = el.dataset.pageNum;
      else if (el.dataset.idx != null) {
        var sectionIdx = parseInt(el.dataset.idx, 10);
        page = Number.isFinite(sectionIdx) ? sectionIdx + 1 : el.dataset.idx;
      }
      else if (el.dataset.uid != null) page = el.dataset.uid;
    }
    if (page == null && el && el.__upRec && el.__upRec.page != null) page = el.__upRec.page;
    return page != null && String(page) === targetPage;
  }
  // 当前视口里**带手写**的页(page-wrap 或 pdf-upage);target 可选。
  // 快照 MCP 会传精确页码,双页同时可见时不能把相邻页的墨迹图冒充当前 revision。
  function _curInkPageEl(target, allowOffscreen) {
    var targetPage = _inkTargetPage(target);
    var els = document.querySelectorAll(
      '.page-wrap[data-page-num], .pdf-upage, ' +
      '.ep-sec[data-idx], .ep-usec[data-uid]'
    );
    // 视口内的优先(那条路能连卡片一起合成);找不到时才回退到屏外的那一页。
    //
    // 此前这里只认视口内的元素,屏外直接返回 null —— 于是"笔迹滚出屏幕"在最上游
    // 就被静默丢弃,离屏合成再怎么就绪也永远走不到。
    var offscreen = null;
    for (var i = 0; i < els.length; i++) {
      var el = els[i], r = el.getBoundingClientRect();
      if (!(_inkPageMatchesTarget(el, targetPage) &&
            el.__inkStrokes && el.__inkStrokes.length)) continue;
      if (r.bottom > 0 && r.top < (window.innerHeight || 0)) return el;
      if (!offscreen) offscreen = el;
    }
    if (allowOffscreen && offscreen) return offscreen;
    return null;
  }
  // PDF 页号;插入页与 EPUB 段落没有,离屏按页合成对它们不适用。
  function _inkPageNumber(el) {
    try {
      if (el && el.dataset && el.dataset.pageNum != null) {
        var n = parseInt(el.dataset.pageNum, 10);
        if (n > 0) return n;
      }
    } catch (e) {}
    return null;
  }
  // Small, AI-readable index of closed custom regions on the exact current
  // page. Geometry stays in the ink layer and is fetched only through the
  // visual tool; the live snapshot needs just enough metadata to name a region
  // without guessing or enumerating arbitrary DOM ids.
  function _selectionRegionsForPage(target) {
    try {
      var surface = _visualSurface();
      var strokes = surface && Array.isArray(surface.strokes)
        ? surface.strokes : null;
      if (!strokes) {
        var el = _curInkPageEl(target);
        strokes = el && Array.isArray(el.__inkStrokes)
          ? el.__inkStrokes : [];
      }
      var regions = strokes.filter(function (stroke) {
        return stroke && stroke.t === 'region' &&
          typeof stroke.id === 'string' &&
          /^[A-Za-z0-9._:-]{1,160}$/.test(stroke.id);
      });
      var regionNumbers = window.RCInk && RCInk.ensureRegionOrdinals
        ? RCInk.ensureRegionOrdinals(strokes) : null;
      regions.sort(function (left, right) {
        var leftOrdinal = (regionNumbers && regionNumbers.get(left)) ||
          Number(left.ordinal) || 0;
        var rightOrdinal = (regionNumbers && regionNumbers.get(right)) ||
          Number(right.ordinal) || 0;
        if (leftOrdinal && rightOrdinal && leftOrdinal !== rightOrdinal) {
          return leftOrdinal - rightOrdinal;
        }
        var byTime = (Number(left.createdAtEpochMs) || 0) -
          (Number(right.createdAtEpochMs) || 0);
        if (byTime) return byTime;
        return left.id < right.id ? -1 : (left.id > right.id ? 1 : 0);
      });
      var total = regions.length;
      var first = Math.max(0, total - 128);
      var items = regions.slice(first).map(function (stroke, index) {
        var ordinal = (regionNumbers && regionNumbers.get(stroke)) ||
          Number(stroke.ordinal) || first + index + 1;
        var createdAt = Number(stroke.createdAtEpochMs) || 0;
        var date = createdAt > 0 ? new Date(createdAt) : null;
        var pad = function (value) { return String(value).padStart(2, '0'); };
        var clock = date && Number.isFinite(date.getTime())
          ? pad(date.getHours()) + ':' + pad(date.getMinutes())
          : '--:--';
        return {
          selectionId: stroke.id,
          label: '#' + ordinal + ' ' + clock,
          ordinal: ordinal,
          createdAtEpochMs: createdAt,
        };
      });
      return {
        contract: 'reader-selection-regions/1',
        total: total,
        truncated: first > 0,
        items: items,
      };
    } catch (e) {
      return {
        contract: 'reader-selection-regions/1',
        total: 0,
        truncated: false,
        items: [],
      };
    }
  }
  // 用户点子:前端截图但**灵活截局部**——按笔迹外接框(+留白上下文)只截那一小块,而非整屏。所见即所得 + 聚焦。
  async function _captureInkRegion(target) {
    try {
      var scope = _visualCaptureScope(target);
      var selectionId = _visualCaptureSelectionId(target);
      var delivery = target && target.__native_delivery;
      if (scope === 'selection-near' && !selectionId) return null;
      var surface = _visualSurface();
      if (surface && surface.strokes.length) {
        var surfaceCrop = _surfaceInkCrop(surface, selectionId);
        if (!surfaceCrop) return null;
        return scope
          ? await _captureSurfaceCompositeCrop(surface, surfaceCrop, selectionId)
          : await _captureSurface(surface, surfaceCrop);
      }
      var el = _curInkPageEl(target, true);
      var strokes = el && el.__inkStrokes;
      if (!el || !strokes || !strokes.length) return null;
      if (scope) {
        var W0 = el.offsetWidth || el.getBoundingClientRect().width;
        var H0 = el.offsetHeight || el.getBoundingClientRect().height;
        var scopedSurface = {
          element: el,
          width: W0,
          height: H0,
          viewport: {
            width: Math.min(W0, window.innerWidth || W0),
            height: Math.min(H0, window.innerHeight || H0)
          },
          strokes: strokes
        };
        var scopedCrop = _surfaceInkCrop(scopedSurface, selectionId);
        if (!scopedCrop) return null;
        // see_ink 带 scope,走的是这一条 —— 原生取图必须接在这里。
        // 只接在下面的裸 strokes 分支时,笔迹路径永远退到 html2canvas,而后者在本机
        // 阅读器上必然失败(页面用了 CSS color() 现代色彩语法,html2canvas 不认)。
        // _surfaceInkCrop 给的是相对元素的像素框,原生要的是归一化,这里换算。
        if (W0 > 0 && H0 > 0) {
          var natScoped = await _nativeInkRegion(
            el,
            scopedCrop.x / W0,
            scopedCrop.y / H0,
            (scopedCrop.x + scopedCrop.width) / W0,
            (scopedCrop.y + scopedCrop.height) / H0,
            delivery
          );
          if (natScoped) return natScoped;
        }
        return await _captureSurfaceCompositeCrop(
          scopedSurface,
          scopedCrop,
          selectionId
        );
      }
      var x0 = 1, y0 = 1, x1 = 0, y1 = 0;   // 笔迹外接框(归一化 0-1)
      strokes.forEach(function (s) { _visualStrokePoints(s).forEach(function (pt) { x0 = Math.min(x0, pt[0]); y0 = Math.min(y0, pt[1]); x1 = Math.max(x1, pt[0]); y1 = Math.max(y1, pt[1]); }); });
      if (!(x1 > x0 && y1 > y0)) return null;
      var m = 0.08; x0 = Math.max(0, x0 - m); y0 = Math.max(0, y0 - m); x1 = Math.min(1, x1 + m); y1 = Math.min(1, y1 + m);   // 留白带上下文
      if (x1 - x0 < 0.28) { var cx = (x0 + x1) / 2; x0 = Math.max(0, cx - 0.14); x1 = Math.min(1, cx + 0.14); }   // 太窄→给最小宽(别只裁个点)
      if (y1 - y0 < 0.18) { var cy = (y0 + y1) / 2; y0 = Math.max(0, cy - 0.09); y1 = Math.min(1, cy + 0.09); }
      var natInk = await _nativeInkRegion(el, x0, y0, x1, y1, delivery);
      if (natInk) return natInk;
      await _loadH2C();
      var W = el.offsetWidth || el.getBoundingClientRect().width, H = el.offsetHeight || el.getBoundingClientRect().height;
      var canvas = await window.html2canvas(el, {
        x: Math.round(x0 * W), y: Math.round(y0 * H), width: Math.round((x1 - x0) * W), height: Math.round((y1 - y0) * H),
        useCORS: true, logging: false, backgroundColor: '#ffffff', scale: Math.min(2, window.devicePixelRatio || 1),
        ignoreElements: function (e2) { var id = e2.id || ''; return id === 'ep-side' || id === 'rc-vc' || id === 'vc-cap' || id === 'word-pop' || id === 'sel-toolbar'; }
      });
      var b64 = '', qs = [0.85, 0.7, 0.5];
      for (var q = 0; q < qs.length; q++) { b64 = (canvas.toDataURL('image/jpeg', qs[q]).split(',')[1]) || ''; if (b64.length <= 900000) break; }
      if (b64.length <= 3000) return _visualNull('笔迹裁图', '图过小 ' + b64.length + 'B(疑空白)');
      return { media_type: 'image/jpeg', b64: b64 };
    } catch (e) { return _visualNull('笔迹裁图', _visualErrText(e)); }
  }
  try {
    window.RC = window.RC || {};
    RC.captureView = _captureView;
    RC.captureEl = _captureEl;
    RC.captureInkRegion = _captureInkRegion;
    RC.capturePageComposite = _capturePageComposite;
    RC.selectionRegionsForPage = _selectionRegionsForPage;
  } catch (e) {}   // 共享截图:视口/指定元素/笔迹局部/**整页叠加合成图**。文字侧栏 EPUB 预拍;语音走 need_shot

  // Each stage of the visual path answers for itself.
  //
  // Composing the page, saving it locally, holding a valid call identity,
  // handing it to the sideband and getting it across the network are five
  // different things that can fail, and they used to arrive as one sentence:
  // "当前笔迹合成图生成失败". That sentence is wrong for four of the five, and
  // on a device with no console it is the only thing anyone ever sees -- so a
  // network refusal and an empty canvas were indistinguishable.
  //
  // The stage is carried in the message rather than replacing it, so existing
  // handling keeps working while the detail becomes readable.
  function _visualStageError(stage, detail) {
    var text = '看图失败[' + stage + ']';
    if (detail) text += ': ' + detail;
    var error = new Error(text);
    error.bwVisualStage = stage;
    return error;
  }

  // Writes each step to the on-screen debug log.
  //
  // A tool that stays at "处理中" reports nothing at all: no error is thrown,
  // so neither the tool card nor the model has anything to show, and the one
  // fact worth knowing -- which step stopped -- exists only inside a pending
  // promise. The reader already has a visible log; every step of this path
  // goes there so a stall names itself.
  function _visualStep(text) {
    try { if (window.dlog) window.dlog('see: ' + text, '#7bd0ff'); } catch (_) {}
  }

  // Fails loudly instead of hanging.
  //
  // The native bridge is a message round trip to Swift; if the other side never
  // answers, this await never settles and the call sits at "处理中" forever.
  // A bounded wait turns silence into a reportable stage.
  function _withVisualTimeout(promise, ms, stage) {
    return new Promise(function (resolve, reject) {
      var settled = false;
      var timer = setTimeout(function () {
        if (settled) return;
        settled = true;
        reject(_visualStageError(stage, '等待 ' + ms + 'ms 无响应'));
      }, ms);
      Promise.resolve(promise).then(function (value) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(value);
      }, function (error) {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        reject(error);
      });
    });
  }

  function _rethrowVisualStage(fallbackStage, error) {
    if (error && error.bwVisualStage) throw error;
    throw _visualStageError(fallbackStage, error && error.message);
  }

  async function _nativeRealtimeVisual(name, args) {
    var target = Object.assign({ page: _rtc.ctxPage || 0 }, args || {});
    if (!_rtc.callId) {
      throw _visualStageError('call 身份', '当前通话标识为空');
    }
    if (!_rtc.sidebandKey) {
      throw _visualStageError('sideband', '旁路密钥缺失，无法把图交给原生通道');
    }
    var delivery = {
      call_id: _rtc.callId,
      client_secret: _rtc.sidebandKey,
      tool: name
    };
    target.__native_delivery = delivery;
    var shot = null;
    var attempted = [];
    if (name === 'see_ink' && window.RC && RC.captureInkRegion) {
      attempted.push('笔迹裁图');
      _visualStep('笔迹裁图 开始');
      try {
        shot = await _withVisualTimeout(
          RC.captureInkRegion(target), 26000, '原生取图并直投/笔迹裁图'
        );
        if (shot && shot.native_delivery_failed) {
          throw _visualStageError('原生直投', shot.error || '原生直投失败');
        }
        _visualStep('笔迹裁图 ' + (shot ? '得到图' : '无图'));
      } catch (error) {
        _rethrowVisualStage('页面合成/笔迹裁图', error);
      }
    }
    if (!shot && window.RC && RC.capturePageComposite) {
      attempted.push('整页合成');
      _visualStep('整页合成 开始');
      try {
        var compositeTarget = name === 'see_page'
          ? target
          : (name === 'see_ink'
            ? target
            : Object.assign({}, target, { scope: 'viewport-context' }));
        shot = await _withVisualTimeout(RC.capturePageComposite(compositeTarget),
          26000, '原生取图并直投/整页合成');
        if (shot && shot.native_delivery_failed) {
          throw _visualStageError('原生直投', shot.error || '原生直投失败');
        }
        _visualStep('整页合成 ' + (shot ? '得到图' : '无图'));
      } catch (error) {
        _rethrowVisualStage('页面合成/整页合成', error);
      }
    }
    if (!shot) {
      attempted.push('视口截图');
      _visualStep('视口截图 开始');
      try {
        shot = await _withVisualTimeout(
          _captureView(delivery), 26000, '原生取图并直投/视口截图'
        );
        if (shot && shot.native_delivery_failed) {
          throw _visualStageError('原生直投', shot.error || '原生直投失败');
        }
        _visualStep('视口截图 ' + (shot ? '得到图' : '无图'));
      } catch (error) {
        _rethrowVisualStage('页面合成/视口截图', error);
      }
    }
    if (!shot || (!shot.b64 && !shot.native_delivered)) {
      // Names what was tried. "composition failed" without the list cannot
      // distinguish "there was no ink to crop" from "the canvas came back
      // blank", and those are opposite problems.
      throw _visualStageError(
        '页面合成',
        '已尝试 ' + (attempted.join('→') || '无可用途径') + '，均未产出图像'
      );
    }
    if (shot.native_delivered) {
      _visualStep('图像已在原生层直接送入当前 Realtime 会话');
      return shot;
    }
    _visualStep('图已就绪 ' + Math.round(String(shot.b64 || '').length / 1024) + 'KB，送往原生通道');
    var reply;
    try {
      reply = await _withVisualTimeout(_nativeRealtimeRequest({
        action: 'image', call_id: _rtc.callId,
        client_secret: _rtc.sidebandKey || '', tool: name,
        media_type: shot.media_type || 'image/jpeg', b64: shot.b64
      }), 15000, '本地保存/传输');
    } catch (error) {
      // The native side covers local save, reference read and network send;
      // it reports which one, and that detail is passed through unchanged.
      throw _visualStageError('本地保存/传输', error && error.message);
    }
    if (reply && reply.ok === false) {
      throw _visualStageError('本地保存/传输', reply.error || '原生通道未接受该图');
    }
    _visualStep('原生通道已接受');
    return shot;
  }

  function _nativeRealtimeDOMPageText(page) {
    try {
      page = Number(page) || 0;
      if (!page) return '';
      var wrap = document.querySelector('.page-wrap[data-page-num="' + page + '"]');
      var chars = wrap && wrap.__charBoxes;
      if (!Array.isArray(chars) || !chars.length) return '';
      return chars.map(function (item) {
        return item && item.c != null ? String(item.c) : '';
      }).join('').replace(/\u0000/g, '').trim().slice(0, 12000);
    } catch (e) { return ''; }
  }

  function _nativeRealtimePageText() {
    var text = String(_rtc.pendText || '').trim();
    if (!text) {
      try {
        var ctx = window.RC && RC.adapter && RC.adapter().getContext
          ? RC.adapter().getContext() : null;
        text = String((ctx && (ctx.visible_text || ctx.text)) || '').trim();
      } catch (e) {}
    }
    // 本机 PDF 的 adapter 过去没有公开 visible_text，但屏幕上的可选字符
    // 已经在 __charBoxes 中。不要把“adapter 漏字段”误报成“没有文字层”。
    if (!text) text = _nativeRealtimeDOMPageText(_rtc.ctxPage);
    return text.slice(0, 6000);
  }

  function _nativeRealtimeContextSnapshot(page) {
    var ctx = null;
    try {
      ctx = window.RC && RC.adapter && RC.adapter().getContext
        ? RC.adapter().getContext() : null;
    } catch (e) {}
    ctx = ctx || {};
    page = Number(page) || Number(ctx.page) || Number(_rtc.ctxPage) || 0;
    var title = String(ctx.book_name || ctx.title || '').trim();
    if (!title) {
      title = String(ctx.file_rel || ctx.file || _rtc.ctxFile || '').split(/[\\/]/).pop()
        .replace(/\.[^.]+$/, '').trim();
    }
    var visible = page === (Number(_rtc.ctxPage) || page)
      ? _nativeRealtimePageText() : _nativeRealtimeDOMPageText(page);
    return {
      title: title.slice(0, 300),
      file: String(ctx.file_rel || ctx.file || _rtc.ctxFile || '').slice(0, 800),
      page: page,
      total: Number(ctx.total || _rtc.ctxTotal) || 0,
      visible_text: String(visible || '').slice(0, 2400),
      selection: String(_rtc.sel || ctx.selection || '').trim().slice(0, 1000),
      selection_context: String(ctx.selection_sentence || '').trim().slice(0, 1200),
      user_question: String(_lastU || '').trim().slice(0, 1200)
    };
  }

  function _nativeRealtimeBounded(task, ms, fallback) {
    return new Promise(function (resolve) {
      var done = false;
      var finish = function (value) {
        if (done) return;
        done = true;
        try { clearTimeout(timer); } catch (e) {}
        resolve(value);
      };
      var timer = setTimeout(function () { finish(fallback); }, ms);
      Promise.resolve(task).then(finish, function () { finish(fallback); });
    });
  }

  function _nativeRealtimePageRecord(page, fallbackText) {
    page = Number(page) || 0;
    var fallback = {
      page: page,
      text: String(fallbackText || _nativeRealtimeDOMPageText(page) || '').trim(),
      source: fallbackText ? 'app-current-visible-text' : 'app-rendered-char-layer',
      revision: '', state: fallbackText ? 'ready' : 'idle'
    };
    if (!page) return Promise.resolve(fallback);
    try {
      var provider = window.BWReaderRuntime && window.BWReaderRuntime.pageTextProvider;
      if (!provider || provider.contract !== 'reader-page-text-provider/1' ||
          typeof provider.pageChars !== 'function') return Promise.resolve(fallback);
      return _nativeRealtimeBounded(provider.pageChars(page), 3500, fallback)
        .then(function (result) {
          var chars = result && Array.isArray(result.chars) ? result.chars : [];
          var text = chars.map(function (item) {
            return item && item.c != null ? String(item.c) : '';
          }).join('').replace(/\u0000/g, '').trim();
          if (!text) return fallback;
          return {
            page: page, text: text,
            source: String(result.source || 'native-page-text'),
            revision: String(result.revision || '').slice(0, 160),
            state: String(result.state || 'ready')
          };
        });
    } catch (e) { return Promise.resolve(fallback); }
  }

  async function _nativeRealtimePageContext(page, snapshot) {
    snapshot = snapshot || _nativeRealtimeContextSnapshot(page);
    page = Number(page) || Number(snapshot.page) || 0;
    var total = Number(snapshot.total) || 0;
    var previousPage = page > 1 ? page - 1 : 0;
    var nextPage = (!total || page < total) ? page + 1 : 0;
    var records = await Promise.all([
      previousPage ? _nativeRealtimePageRecord(previousPage, '') : Promise.resolve(null),
      _nativeRealtimePageRecord(page, snapshot.visible_text),
      nextPage ? _nativeRealtimePageRecord(nextPage, '') : Promise.resolve(null)
    ]);
    var current = records[1] || { text: '', source: 'none', revision: '', state: 'idle' };
    var beforeText = records[0] && records[0].text ? String(records[0].text) : '';
    var afterText = records[2] && records[2].text ? String(records[2].text) : '';
    var currentText = String(current.text || snapshot.visible_text || '');
    return {
      contract: 'reader-realtime-page-context/1',
      title: snapshot.title || '',
      file: snapshot.file || '',
      page: page,
      total: total,
      before_page: previousPage || null,
      before: beforeText.length > 700 ? ('…' + beforeText.slice(-700)) : beforeText,
      visible_text: String(snapshot.visible_text || currentText).slice(0, 2400),
      current_page_text: currentText.slice(0, 3200),
      after_page: nextPage || null,
      after: afterText.slice(0, 700),
      selection: snapshot.selection || '',
      selection_context: snapshot.selection_context || '',
      source: current.source || 'none',
      revision: current.revision || '',
      state: current.state || 'idle',
      truncated: beforeText.length > 700 || currentText.length > 3200 || afterText.length > 700
    };
  }

  function _nativeRealtimeHighlightTarget(snapshot) {
    var adapter = null, context = null;
    try {
      adapter = window.RC && RC.adapter ? RC.adapter() : null;
      context = adapter && typeof adapter.getContext === 'function'
        ? adapter.getContext() : null;
    } catch (e) {}
    context = context || {};
    if (adapter && adapter.config && adapter.config.isPDF === false) {
      var section = Number(context.current_section_idx);
      if (!Number.isInteger(section) || section < 0) section = Math.max(0, (Number(snapshot.page) || 1) - 1);
      return { kind: 'epub', section: section };
    }
    return { kind: 'pdf', page: Number(snapshot.page) || Number(_rtc.ctxPage) || 0 };
  }

  async function _nativeRealtimeHighlightSource(snapshot) {
    if (typeof window.__bwReaderHighlightSource !== 'function') return null;
    var fallback = null;
    return _nativeRealtimeBounded(
      Promise.resolve(window.__bwReaderHighlightSource({
        file: String(snapshot.file || _rtc.ctxFile || ''),
        target: _nativeRealtimeHighlightTarget(snapshot)
      })),
      3500,
      fallback
    );
  }

  function _nativeRealtimeRangeMatchesCurrent(rangeRef, snapshot) {
    if (!rangeRef || rangeRef.contract !== 'reader-source-range/1' ||
        rangeRef.documentId !== String(snapshot.file || _rtc.ctxFile || '')) {
      return false;
    }
    var currentTarget = _nativeRealtimeHighlightTarget(snapshot);
    var target = rangeRef.target;
    if (!target || target.kind !== currentTarget.kind) return false;
    return currentTarget.kind === 'pdf'
      ? Number(target.page) === currentTarget.page
      : Number(target.section) === currentTarget.section;
  }

  async function _rtcTool(name, args, callId) {   // 工具循环(本地):与 relay WS 版同语义,tool_status 卡/client_action 全复用
    if (name === 'wait_for_user') {   // 静音 no-op:回空 output、不 response.create=安静
      _dcSend({ type: 'conversation.item.create', item: { type: 'function_call_output', call_id: callId, output: '{}' } });
      return;
    }
    if (name === 'reply_text') {   // ㊿c 自动模式(外部审核设计):模型自判"长内容文字更合适"→答案在参数里,
      // 文字显示+落库,闭合 call 但**不 response.create**(与 wait_for_user 同构)=零输出音频;一次推理完成选择+回答
      var ans = String(args.text || '').slice(0, 6000);
      if (ans) {
        try { window.__asstVoiceMsg && window.__asstVoiceMsg('reset'); window.__asstVoiceMsg && window.__asstVoiceMsg('a', ans); } catch (e) {}
        try { window.__asstVoiceLog && window.__asstVoiceLog(_lastU, ans, _rtc.ctxFile, _rtc.ctxPage); _lastU = ''; } catch (e) {}
        try { _rtcCapFeed(ans, true); } catch (e) {}   // 侧栏关着时字幕也能看到
        if (_ttsOn()) { _speakSafe(ans); }   // TTS 开关:文字答案照样代念
        try { _cardPush(ans, '文字回复'); } catch (e) {}   // 65:侧栏关着时弹磨砂卡
      }
      _dcSend({ type: 'conversation.item.create', item: { type: 'function_call_output', call_id: callId, output: '{"displayed":true}' } });
      return;   // 关键:不发 response.create
    }
    if (name === 'route_to_text') {   // 61(fallback 路径;控制面在时由 relay 执行):程序门控按当前模式,热切生效
      _rtc.turnTool = true; _rtc.turnToolAny = true;
      if (_voiceMode() !== 'route') {
        _dcSend({ type: 'conversation.item.create', item: { type: 'function_call_output', call_id: callId,
                  output: '(当前输出模式未启用文字路由:请直接口头简要回答重点;想看长文可让用户切到「路由」模式)' } });
        _rtcQueueToolResponse('tool');
        return;
      }
      onToolStatus({ status: 'running', label: '路由详答·生成中' });
      (async function () {
        var full = '', err = '';
        try {
          var rr = await fetch('/api/assistant/route-text', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ intent: String(args.intent || ''), q: _lastU || '', file: _rtc.ctxFile, page: _rtc.ctxPage }) });
          var rd = rr.body.getReader(), dec = new TextDecoder(), buf = '', ev2 = '';
          var rst = { feed: _mkTtsFeeder() };
          try { window.__asstVoiceMsg && window.__asstVoiceMsg('reset'); } catch (e) {}
          _rtcCapReset();
          try { if (_cap.cur) _cap.cur.classList.add('vc-cap-route'); } catch (e) {}
          while (true) {
            var ch = await rd.read(); if (ch.done) break;
            buf += dec.decode(ch.value, { stream: true });
            var lines = buf.split('\n'); buf = lines.pop();
            for (var li = 0; li < lines.length; li++) {
              var ln = lines[li];
              if (ln.indexOf('event:') === 0) ev2 = ln.slice(6).trim();
              else if (ln.indexOf('data:') === 0 && ev2 === 'delta') {
                var seg = ''; try { seg = JSON.parse(ln.slice(5).trim()); } catch (e) {}
                if (seg) {
                  full += seg;
                  try { window.__asstVoiceMsg && window.__asstVoiceMsg('a', full); } catch (e) {}
                  _rtcCapFeed(full, false);
                  try { rst.feed(full, false); } catch (e) {}
                }
              } else if (ln.indexOf('data:') === 0 && ev2 === 'done') {
                try { _rtcTool._rbrief = (JSON.parse(ln.slice(5).trim()) || {}).summary || ''; } catch (e) {}
              } else if (ln.indexOf('data:') === 0 && ev2 === 'err') err = '生成后端不可用';
            }
          }
          if (full) {
            try { window.__asstVoiceMsg && window.__asstVoiceMsg('a', full, { md: true }); } catch (e) {}   // 67:终态 md
            try { window.__asstVoiceLog && window.__asstVoiceLog(_lastU, full, _rtc.ctxFile, _rtc.ctxPage); _lastU = ''; } catch (e) {}
            _rtcCapFeed(full, true);
            try { rst.feed(full, true); } catch (e) {}
            // 132(用户):字幕已经在显示 AI 的文字输出 → 文字输出档**不再另弹一张卡**(重复且挡内容)
          }
        } catch (e) { err = String(e).slice(0, 80); }
        var okR = !!full;
        onToolStatus({ status: okR ? 'done' : 'error', tool: 'route_to_text', label: '路由详答', args: args, rag: (full || err).slice(0, 400) });
        _dcSend({ type: 'conversation.item.create', item: { type: 'function_call_output', call_id: callId,
                  output: okR ? ('(文字详答已显示在用户屏幕上,本轮到此结束。内容简介:' + (_rtcTool._rbrief || full.slice(0, 200)) + '。用户下次说话时若相关直接运用;想让你看全文他会长按卡片带入。)') : ('(文字生成失败:' + err + ';请口头简要回答)') } });
        _rtcTool._rbrief = '';
        if (!okR) _rtcQueueToolResponse('tool');   // 成功=长文已显示,不再花一轮输出音频;失败=等原 response 结束后口头补救
      })();
      return;
    }
    var requestedName = name;
    var toolEpochAtStart = _rtc.turnEpoch || 0;
    var currentPageAtStart = _rtcFreshInkPage() || _rtc.ctxPage;
    var freshInkAtStart = _rtcHasFreshInk(currentPageAtStart);
    name = _rtcEffectiveTool(name, freshInkAtStart);
    var explicitSeeInkTarget = requestedName === 'see_ink' && !!(
      Number(args && args.page) ||
      String((args && args.selectionId) || '').trim() ||
      String((args && args.scope) || '').trim()
    );
    var forceCurrentInk = name === 'see_ink' && freshInkAtStart && !explicitSeeInkTarget;
    if (forceCurrentInk) {
      // 当前页 fresh 强占（包括低优先级工具被提升）时不能继承旧页码或
      // selectionId；普通显式 see_ink 没有 fresh 时仍保留用户指定页/选区。
      args = { page: currentPageAtStart, scope: 'drawing-nearby' };
    }
    var inkPageAtStart = name === 'see_ink'
      ? (Number(args && args.page) || currentPageAtStart) : currentPageAtStart;
    var inkVersionForDelivery = 0;
    var visualTargetAtStart = /^(see_ink|see_page|see_figure)$/.test(name);
    var visualPageAtStart = name === 'see_ink'
      ? inkPageAtStart : (Number(args && args.page) || currentPageAtStart);
    var visualNeedsTextContext = visualTargetAtStart && name !== 'see_ink';
    var visualContextSnapshot = visualNeedsTextContext
      ? _nativeRealtimeContextSnapshot(visualPageAtStart) : null;
    var visualContextPromise = null;
    _rtc.turnTool = true; _rtc.turnToolAny = true;   // ㊸:本轮真调了工具(承诺核查放行;turnToolAny 用户轮作用域,不随 response 复位)
    // OpenAI 可在原始 response 中一次排出多个 function_call；tool_choice:none
    // 只能阻止工具结果后的下一轮再调用，挡不住这些已排队的并发调用。
    // 在任何 await 之前认领本用户轮，第二个视觉调用只闭合、不取第二张图也不再答一次。
    if (visualTargetAtStart && _rtc.visualTurnEpoch === toolEpochAtStart) {
      var suppressedOutput = JSON.stringify({
        ok: true,
        suppressed: true,
        no_additional_answer: true,
        requested_tool: requestedName,
        resolved_as: name,
        reason: '同一用户轮已有一个视觉目标正在处理；本调用仅闭合，沿用该图与该次最终回答。'
      });
      _dcSend({
        type: 'conversation.item.create',
        item: { type: 'function_call_output', call_id: callId, output: suppressedOutput }
      });
      onToolStatus({
        status: 'done', tool: name, label: '已合并到本轮视觉查看',
        args: args, rag: suppressedOutput
      });
      return;
    }
    if (visualTargetAtStart) {
      _rtc.visualTurnEpoch = toolEpochAtStart;
      if (visualNeedsTextContext) {
        visualContextPromise = _nativeRealtimePageContext(
          visualPageAtStart, visualContextSnapshot
        );
      }
    }
    if (name === 'read_selection' && _rtc.sel && _rtc.sel.trim()) {   // 74:选中在手=短路闪回(fallback 路径与 relay 同构)
      onToolStatus({ status: 'done', tool: name, label: '读取选中(免调用)', rag: _rtc.sel.slice(0, 300) });
      _dcSend({ type: 'conversation.item.create', item: { type: 'function_call_output', call_id: callId,
                output: '(选中内容就在这里,无需再查:「' + _rtc.sel.slice(0, 800) + '」——直接使用)' } });
      _rtcQueueToolResponse('tool');
      return;
    }
    _rtc.toolN = (_rtc.toolN || 0) + 1;   // ㊷ 护栏:单会话工具调用异常多=可能循环失控,提醒但不硬断
    if (_rtc.toolN === 40) { try { threadMsg('asst-note', '⚠ 本次通话工具调用已达 40 次(异常偏多)——若感觉它在兜圈子,挂断重拨或点🗑清空。'); } catch (e) {} }
    onToolStatus({ status: 'running', label: name });
    // Entry point of every tool, with the route decided by nativeDirect.
    // A call that never returns leaves no other trace of having started.
    try {
      if (window.dlog) {
        window.dlog(
          'tool→ ' + name + ' route=' + (_rtc.nativeDirect ? 'local' : 'server')
            + ' flag=' + String(window.__BW_NATIVE_OPENAI_REALTIME__ === true)
            + ' bridge=' + String(!!(window.__bwNativeRealtime &&
              typeof window.__bwNativeRealtime.request === 'function')),
          '#9ad'
        );
      }
    } catch (_) {}
    var out = '', ok = true, label = name, took = null, argsUsed = args, vision = null,
        visualItemID = '', res;
    try {
      if (toolEpochAtStart !== (_rtc.turnEpoch || 0)) {
        throw new Error('工具所属用户轮已被新问题取代');
      }
      if (name === 'see_ink') {
        if (!(await _rtcAwaitInkCommit(inkPageAtStart, 1500))) {
          throw _visualStageError('笔迹提交', '落笔尚未写入当前页面，已保留为未查看，请重试');
        }
        if (forceCurrentInk && Number(_rtcFreshInkPage()) !== Number(inkPageAtStart)) {
          throw _visualStageError('页面定位', '查看笔迹前页面已切换，原页笔迹仍保留为未查看');
        }
        var inkStateForDelivery = _rtcInkPageState(inkPageAtStart, false);
        inkVersionForDelivery = (inkStateForDelivery && inkStateForDelivery.ver) || 0;
        if (toolEpochAtStart !== (_rtc.turnEpoch || 0)) {
          throw new Error('工具所属用户轮已被新问题取代');
        }
      }
      if (!_rtc.nativeDirect && /^(see_ink|see_page|see_figure)$/.test(name)) {
        // Refused rather than attempted.
        //
        // Without the native route the request would go to Pi, which has no
        // access to ink drawn on this device: it can only ever come back with
        // an image that lacks the very thing the user asked about, or with a
        // generic failure. Saying so plainly beats letting it look like a
        // compositor bug.
        throw _visualStageError(
          '模型工具触发/路由',
          '本机直连未启用（native_direct 未置位），视觉工具无法看到本机笔迹'
        );
      }
      if (_rtc.nativeDirect && /^(see_ink|see_page|see_figure)$/.test(name)) {
        var nativeShot = await _nativeRealtimeVisual(name, args);
        visualItemID = String((nativeShot && nativeShot.item_id) || '');
        if (toolEpochAtStart !== (_rtc.turnEpoch || 0)) {
          if (/^bwi_[a-f0-9]{28}$/.test(visualItemID)) {
            _dcSend({ type: 'conversation.item.delete', item_id: visualItemID });
            visualItemID = '';
          }
          throw new Error('工具所属用户轮已被新问题取代');
        }
        // Native delivery leaves the JPEG inside Swift. Only the legacy web
        // fallback returns b64 for the tool chip; passing a delivery receipt
        // to setVision would make the UI try to render an image with no bytes.
        vision = nativeShot && nativeShot.native_delivered ? null : [nativeShot];
        label = name === 'see_ink' ? '看笔迹标注'
          : (name === 'see_page' ? '看当前页面' : '看当前图像');
        if (name === 'see_ink') {
          // The image item is already present in the Realtime conversation.
          // Do not repeat the current page, adjacent pages, selection or the
          // user question in function_call_output: that wasted tokens and
          // could make the model privilege duplicated text over the drawing.
          // A function call still needs one output item to close, so keep it
          // deliberately empty and let the following response consume the
          // image plus the conversation that was already in the session.
          res = { local_direct: true, image_supplied: true };
          out = '{}';
        } else {
          var visualPageContext = await visualContextPromise;
          if (toolEpochAtStart !== (_rtc.turnEpoch || 0)) {
            if (/^bwi_[a-f0-9]{28}$/.test(visualItemID)) {
              _dcSend({ type: 'conversation.item.delete', item_id: visualItemID });
              visualItemID = '';
            }
            throw new Error('工具所属用户轮已被新问题取代');
          }
          res = {
            local_direct: true, image_supplied: true,
            page_context: visualPageContext
          };
          out = JSON.stringify({
            ok: true,
            requested_tool: requestedName,
            resolved_as: name,
            user_question: visualContextSnapshot.user_question || '',
            page_context: visualPageContext,
            result: '相关合成图与同页文字上下文已一起送入当前 Realtime 会话。',
            instruction: '只生成一次最终回答。必须综合当前用户问题、此前对话、page_context 中标明的前页/当前页/后页文字、当前选区与刚送入的合成图；图像用于外观与空间关系，文字用于人物身份、台词与语义。不得只描述图片，也不要再调用 read_page、see_page 或第二个视觉工具。'
          });
        }
        try {
          (_rtc.recentTools = _rtc.recentTools || []).push({
            tool: name, label: label,
            rag: '本机合成图已直接送入当前 Realtime 会话', images: []
          });
          if (_rtc.recentTools.length > 6) {
            _rtc.recentTools.splice(0, _rtc.recentTools.length - 6);
          }
        } catch (e) {}
      } else if (_rtc.nativeDirect && name === 'read_page') {
        var readPageSnapshot = _nativeRealtimeContextSnapshot(_rtc.ctxPage);
        var localPageContext = await _nativeRealtimePageContext(
          readPageSnapshot.page, readPageSnapshot
        );
        var localHighlightSource = await _nativeRealtimeHighlightSource(readPageSnapshot);
        if (localHighlightSource) localPageContext.highlight_source = localHighlightSource;
        var localPageText = String(
          localPageContext.current_page_text || localPageContext.visible_text || ''
        );
        label = '读取当前页';
        res = {
          local_direct: true, page: readPageSnapshot.page,
          text: localPageText, page_context: localPageContext
        };
        out = JSON.stringify({
          page: readPageSnapshot.page,
          total: readPageSnapshot.total || 0,
          title: readPageSnapshot.title || '',
          text: localPageText || '',
          page_context: localPageContext,
           note: localPageText ? '当前页文字来自 App 原生/预处理字符层；结合前后页与当前选区回答。'
             : '当前页字符层确实未返回文字；只有在问题需要版面或图像证据时才调用 see_page。',
           source: localPageContext.source || 'app-current-visible-text',
           highlight_source: localHighlightSource || undefined
         });
      } else if (_rtc.nativeDirect && name === 'highlight') {
        var localRangeRef = args && (args.rangeRef || args.range_ref);
        if (!localRangeRef || typeof localRangeRef !== 'object') {
          throw new Error('BW_READER_HIGHLIGHT_RANGE_REQUIRED:请先调用 read_page，并从 highlight_source 选择开始和结束 marker');
        }
        var currentRangeSnapshot = _nativeRealtimeContextSnapshot(_rtc.ctxPage);
        if (!_nativeRealtimeRangeMatchesCurrent(localRangeRef, currentRangeSnapshot)) {
          throw new Error('BW_READER_RANGE_SOURCE_STALE:当前书页已变化，请重新调用 read_page 取得 marker');
        }
        if (typeof window.__bwReaderHighlightRange !== 'function') {
          throw new Error('BW_READER_HIGHLIGHT_RANGE_UNAVAILABLE');
        }
        var localRangeColors = {
          '#fff59d': 'yellow', '#a7f3d0': 'green',
          '#a3d4ff': 'blue', '#fda4af': 'pink'
        };
        var localRangeColor = String(args.color || 'yellow').toLowerCase();
        localRangeColor = localRangeColors[localRangeColor] || localRangeColor;
        var localHighlightResult = await Promise.resolve(window.__bwReaderHighlightRange({
          rangeRef: localRangeRef,
          color: localRangeColor,
          note: String(args.note || '').slice(0, 1000)
        }));
        if (!localHighlightResult || localHighlightResult.ok !== true) {
          throw new Error('BW_READER_HIGHLIGHT_RANGE_NOT_CONFIRMED');
        }
        label = '本机范围高亮';
        res = Object.assign({ local_direct: true }, localHighlightResult);
        out = JSON.stringify({
          ok: true,
          highlighted: 1,
          id: localHighlightResult.id || '',
          target: localHighlightResult.target || localRangeRef.target,
          text: String(localHighlightResult.text || '').slice(0, 1000),
          source: 'app-authoritative-range'
        });
      } else if (_rtc.nativeDirect && name === 'read_selection') {
        label = '读取选中';
        res = { local_direct: true, selection: '' };
        out = JSON.stringify({
          selection: '',
          note: '当前没有选中文字。'
        });
      } else if (_rtc.nativeDirect && name === 'make_note') {
        var noteText = String(args.text || _rtc.sel || _nativeRealtimePageText() || '').trim();
        if (!noteText) throw new Error('当前没有可保存的选中内容或可见文字');
        var noteTitle = String(args.title || '').trim();
        if (!noteTitle) {
          var noteBook = String(_rtc.ctxFile || '').split(/[\\/]/).pop()
            .replace(/\.[^.]+$/, '').trim();
          var noteTime = new Date().toISOString().slice(0, 16)
            .replace('T', ' ').replace(':', '-');
          noteTitle = '语音笔记 · ' + (noteBook || '当前页面') + ' · ' + noteTime;
        }
        var notePage = Number(_rtc.ctxPage);
        if (!Number.isFinite(notePage) || notePage < 0) notePage = 0;
        var noteResponse = await fetch('/pdf/api/to-note', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            name: noteTitle.slice(0, 240), text: noteText.slice(0, 240000),
            file: String(_rtc.ctxFile || '').slice(0, 8000), page: Math.trunc(notePage)
          })
        });
        var noteRaw = await noteResponse.text();
        var noteResult = {};
        try { noteResult = noteRaw ? JSON.parse(noteRaw) : {}; } catch (e) {}
        if (!noteResponse.ok || !noteResult || noteResult.ok !== true) {
          throw new Error((noteResult && noteResult.error) ||
            ('本机笔记保存失败 (HTTP ' + noteResponse.status + ')'));
        }
        label = '保存本机笔记';
        res = Object.assign({ local_direct: true }, noteResult);
        out = JSON.stringify({
          ok: true, saved: true, owner: 'native-app',
          note_path: noteResult.note_path || '',
          message: '笔记已保存到 App 配置的本机笔记目录。'
        });
      } else if (_rtc.nativeDirect && !_nativeRealtimePiAITool(name)) {
        throw new Error('App 本机直连模式未开放该工具: ' + name);
      } else if (name === 'deep_think') {
        out = await _rtcDeep(String(args.question || ''));
        label = '深度思考';
      } else {
        var ctx = { file_rel: _rtc.ctxFile, page: _rtc.ctxPage };
        // An explicit visual tool must receive the real composite. rt_image
        // only governs opportunistic image input, not a model-requested read.
        if (_rtc.imgOn || name === 'see_ink' || name === 'see_page' || name === 'see_figure') ctx._want_vision = 1;
        if (_rtc.ink && _rtc.ink.length) ctx.ink = _rtc.ink;
        if (_rtc.sel) ctx.selection = _rtc.sel;
        if (name === 'make_anki' || name === 'make_note') ctx.recent_tools = (_rtc.recentTools || []).slice(-4);   // 61b:搜过的网页/配图随卡走
        var _needShot = (name === 'see_ink' || name === 'see_page');   // ㉟c:看图类恒附视口截图(EPUB 主路/PDF 兜底,后端按需取用)
        if (name === 'read_page') {   // 自建页(插入页)PDF 空白 → 视口里有自建页时也附渲染图(题目+手写),后端 read_page 会返给 AI
          try {
            var _ups = document.querySelectorAll('.pdf-upage');
            for (var _ui = 0; _ui < _ups.length; _ui++) {
              var _ur = _ups[_ui].getBoundingClientRect();
              if (_ur.bottom > 0 && _ur.top < (window.innerHeight || 0)) { _needShot = true; break; }
            }
          } catch (e) {}
        }
        if (_needShot) {
          try { var shot = await _captureView(); if (shot) ctx.view_image = shot; } catch (e) {}
        }
        // 142:**工具超时护栏**。原来这个 fetch 没有 AbortController —— 工具不回 = function_call_output 不回填
        //   = response.create 不发 = 整通对话死在这儿,AI 一句话都不说(账本实录:一次 read_page 卡了 164.9s)。
        //   同步工具实测最慢 6.9s(search_image),长任务后端本来就走 task_id 派发;45s 只可能兜住真挂起。
        //   超时后照常走下面的 catch → 回填「工具超时」的 function_call_output + response.create,让 AI 开口而不是哑掉。
        var _ac = new AbortController();
        var _to = setTimeout(function () { try { _ac.abort(); } catch (e) {} }, 45000);
        var r, d;
        try {
          r = await fetch('/api/assistant/voice-tool', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            signal: _ac.signal,
            body: JSON.stringify({ cmd: JSON.stringify({ tool: name, args: args }), ctx: ctx,
                                   rtc_call_id: _rtc.callId, rtc_sideband_secret: _rtc.sidebandKey || '' }) });
          d = await r.json();
        } catch (_e) {
          clearTimeout(_to);
          if (_e && _e.name === 'AbortError') throw new Error('工具超时(45秒无响应),请改用别的方式或稍后再试');
          throw _e;
        }
        clearTimeout(_to);
        ok = !!d.ok; label = d.label || name; took = d.took_s; argsUsed = d.args || args;
        res = d.result || {};
        _rtcTool._silent = !!res.silent && localStorage.getItem('rc-voice-toolreply') !== '1';   // 74/89:静默入库;「工具口头回报」开=放行
        var ca = res.client_action; delete res.client_action;
        var _resFeed = res;   // 喂回模型/recentTools 的精简版:有 cards_brief 时删 cards 全文——
        try {   //   截断喂回残 JSON=「AI 不知道自己做过什么卡/哪些图搜到了」的根因(用户 2026-07-20);UI 走 onToolStatus 的完整 result
          if (res.cards && res.cards_brief) { _resFeed = Object.assign({}, res); delete _resFeed.cards; }
          if (res.images && res.found_brief) { if (_resFeed === res) _resFeed = Object.assign({}, res); delete _resFeed.images; }   // 图 URL 对语音模型无用还挤爆 1800 预算;found_brief/missed 顶上
        } catch (e) {}
        try {   // 61b:最近工具结果环(搜索摘要/配图URL/已做卡片大意)——之后 make_anki/make_note 把对话现场带给制卡 AI
          var _imgs = [];
          (res.images || []).forEach(function (im) { var _u = im.image_url || im.url; if (_u) _imgs.push(_u); });
          (_rtc.recentTools = _rtc.recentTools || []).push({ tool: name, label: label, rag: JSON.stringify(_resFeed).slice(0, 600), images: _imgs.slice(0, 3) });
          if (_rtc.recentTools.length > 6) _rtc.recentTools.splice(0, _rtc.recentTools.length - 6);
        } catch (e) {}
        if (ca && ca.fn) dispatch(ca.fn, ca.args);           // 页面副作用本地直执行(比经 relay 更直接)
        delete res._vision;   // 图像由后端 sideband 注入(带 rtc_call_id 时后端已处理);绝不经 dc 发——
                              // SCTP 单条上限(Safari≈64KB),超限发送会**直接关闭 data channel**=通话哑死
        var slim = JSON.stringify(_resFeed);
        out = (slim && slim.length > 2) ? slim.slice(0, 1800) : '(无文本结果,界面元素已显示在用户屏幕上)';
      }
    } catch (e) {
      ok = false;
      // Says which route ran, not just what went wrong.
      //
      // Visual tools take one of two paths: local composition inside the App
      // (nativeDirect) or a round trip to Pi. Pi cannot see ink drawn on this
      // device, so that route fails by construction -- yet both routes reported
      // the same bare sentence, and the tool card showed "no extra content".
      // Knowing the route is the difference between "fix the compositor" and
      // "find out why the native route was not taken".
      var route = _rtc.nativeDirect ? 'local' : 'server';
      var stage = e && e.bwVisualStage ? e.bwVisualStage : '';
      out = JSON.stringify({
        error: String((e && e.message) || e).slice(0, 200),
        route: route,
        stage: stage || undefined,
        call: _rtc.callId ? 'present' : 'missing',
        sideband: _rtc.sidebandKey ? 'present' : 'missing',
        native_local: window.__BW_NATIVE_LOCAL_READER__ === true,
        native_flag: window.__BW_NATIVE_OPENAI_REALTIME__ === true,
        native_bridge: !!(window.__bwNativeRealtime &&
          typeof window.__bwNativeRealtime.request === 'function')
      });
    }
    try {
      if (window.dlog) {
        window.dlog('tool← ' + name + ' ' + (ok ? 'ok' : 'FAIL ') +
          (ok ? '' : String(out || '').slice(0, 160)), ok ? '#7be096' : '#ff6b6b');
      }
    } catch (_) {}
    if (toolEpochAtStart !== (_rtc.turnEpoch || 0) && /^bwi_[a-f0-9]{28}$/.test(visualItemID)) {
      _dcSend({ type: 'conversation.item.delete', item_id: visualItemID });
      visualItemID = '';
    }
    var completion = _rtcCompleteToolTurn(
      name, ok, inkVersionForDelivery, inkPageAtStart,
      callId, out, ok && String(out || '').length > 800, _rtcTool._silent,
      toolEpochAtStart
    );
    if (completion.stale) {
      ok = false;
      out = JSON.stringify({
        error: '该工具属于已被新问题取代的旧用户轮；已闭合调用但未再生成回答',
        stale_turn: true
      });
    } else if (!completion.complete) {
      ok = false;
      out = JSON.stringify({
        error: '工具结果未能送入当前 Realtime 会话；当前页笔迹仍保留为未查看',
        output_sent: completion.outputSent,
        response_sent: completion.responseSent
      });
      try { if (window.dlog) window.dlog('tool← ' + name + ' FAIL data-channel delivery', '#ff6b6b'); } catch (_) {}
    }
    _rtcTool._silent = false;
    onToolStatus({ status: ok ? 'done' : 'error', tool: name, label: label, took_s: took, args: argsUsed, rag: out.slice(0, 1600), result: (typeof res === 'object' ? res : undefined), vision: vision || undefined });   // result=完整体(UI 渲卡用;rag 是喂回模型的精简版)
  }
  // rtc 字幕队列:transcript delta 是文字生成速度(1-2s 内全到),远快于声音——直接 capStream 会瞬间跳到
  // 末句,且 response.done(生成完≠播完)后提前淡出(rtc 音频在 <audio> 元素,playing 队列看不见它)。
  // 改逐句估时放出(TTS ≈6字/秒)粗对齐声音;豆包路径(550 跟随合成节奏)不走这里。
  var _rtcCap = { q: [], t: null, fed: 0, done: false };
  function _rtcCapReset() {
    _rtcCap.q = []; _rtcCap.fed = 0; _rtcCap.done = false;
    _rtcCap.fb = 0; _cap.sawSeg = 0;   // 135:新一轮 —— 兜底放行 / 句边界帧标志都归零(否则串轮)
    if (_rtcCap.t) { clearTimeout(_rtcCap.t); _rtcCap.t = null; }
    if (_rtcCap.wd) { clearTimeout(_rtcCap.wd); _rtcCap.wd = null; }
  }
  // 135(用户):TTS 代念时,句子和音频的对应关系是**拿得到的**(relay 每句前发 tts_seg 帧,前端把它绑到
  //   该音频块的真实播放调度时刻 playT)→ 字幕能跟声音一秒不差。所以这一轮字幕归**精确路**,
  //   估算路(_rtcCapFeed:按字数×160ms 轮播)必须让开 —— 否则两条路同时跑,文字生成远快于说话,
  //   估算路一路狂奔在前、精确路在后面追,字幕就永远对不上(用户实测)。
  function _capTtsOwns() { try { return !!(_rtc.turnText && _ttsOn()); } catch (e) { return false; } }
  function _rtcCapFeed(full, isDone) {
    if (_capTtsOwns() && !_rtcCap.fb) {   // TTS 代念这轮:让位给精确路(fb=兜底放行,见看门狗)
      if (isDone) { _rtcCap.done = true; _capSegWatch(full); }
      return;
    }
    if (isDone) _rtcCap.done = true;
    var re = /[^。！？!?;；\n]+[。！？!?;；\n]*/g, parts = [], m;
    while ((m = re.exec(full))) { if (m[0].trim()) parts.push(m[0].trim()); }
    var closed = /[。！？!?;；\n]\s*$/.test(full);
    var upto = (isDone || closed) ? parts.length : parts.length - 1;   // 末句残缺就等它闭合,别闪半句
    for (var i = _rtcCap.fed; i < upto; i++) _rtcCap.q.push(parts[i]);
    if (upto > _rtcCap.fed) _rtcCap.fed = upto;
    _rtcCapPump();
  }
  function _capSegWatch(full) {   // TTS 通道没给句边界帧 → 回退估算路(宁可不精确,也不能没字幕)
    clearTimeout(_rtcCap.wd);
    _rtcCap.wd = setTimeout(function () {
      if (_cap.sawSeg) return;                 // 精确路已经在放了
      _rtcCap.fb = 1;                          // 放行估算路(本轮)
      try { _rtcCapFeed(full, true); } catch (e) {}
    }, 3500);
  }
  function _rtcCapPump() {
    if (_rtcCap.t) return;
    var s = _rtcCap.q.shift();
    if (s == null) { if (_rtcCap.done) _capMaybeHide(2500); return; }
    capShow(s, 'a');
    _rtcCap.t = setTimeout(function () { _rtcCap.t = null; _rtcCapPump(); },
                           Math.max(1100, Math.min(6000, s.length * 160)));
  }
  // ㊳ 会话内压缩(官方 8.4:item.delete 批量删旧轮+摘要顶上,低频批量>频繁逐条):
  //   触发判据=每轮 input_tokens(usage 实时监控,cached 也要按 cached 价反复付)超过阈值——
  //   此时"历史携带成本×剩余轮次"必然超过一次性缓存失效(摘要+近几轮全价),压缩更便宜。
  async function _rtcCompactNow(urgent) {
    if (_rtc.nativeDirect) return;
    if (!urgent && Date.now() - (_rtc.lastCompact || 0) < 90000) return;   // 低频保护(cookbook:低频批量>高频零碎;紧急线豁免)
    if (_rtc.items.length < 12) return;
    _rtc.lastCompact = Date.now();
    try {
      var r = await (await fetch('/api/assistant/compact-history', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file: _rtc.ctxFile || '', force: 1 }) })).json();
      var summary = (r && r.summary) || '';
      // 服务端 compact-history 有合法的 skip 出口(fresh/pack < 8),返回 {ok,skipped} 不带 summary。
      // 删除集必须由摘要派生(cookbook 不变式:没摘要就直接收场)——否则这里会无条件删掉除近 8 条外的全部历史、
      // 连上一版摘要一起删,通话中途模型记忆归零且没有任何东西顶上(第二次压缩最易踩:upto_ts 已推进,新增才几条 → skip)。
      if (!summary) { _rtc.lastCompact = 0; return; }   // 什么都别删;冷却重置,让下次积够了再试
      var keep = 8;   // 尾部近几轮 item 保留原文(含最近的 page/state system)
      var del = _rtc.items.slice(0, -keep);
      _rtc.items = _rtc.items.slice(-keep);
      var prevSumId = _rtc.sumId;   // 上一版摘要 id(在头部,不在 items 账本里);等新摘要插好再删,别先删空了顶不上
      // 125b(cookbook 原文核实):官方时序=**先插摘要、后删旧轮**(无"旧轮已删摘要未插"的空窗;
      // delete 定点删与 create 插 root 无冲突,官方范例不等 deleted 确认)。摘要固定 id(不入 items 账本,免排序错位)。
      _rtc.sumId = 'sum_' + Date.now().toString(36);
      _dcSend({ type: 'conversation.item.create', previous_item_id: 'root', item: {
        id: _rtc.sumId, type: 'message', role: 'system',
        content: [{ type: 'input_text', text: '(更早的对话已压缩为摘要:' + summary.slice(0, 1500) + '\n——以此为背景延续对话;状态记录,不要回应本条。)' }] } });
      if (prevSumId) del.push({ id: prevSumId });   // 新摘要已插:旧摘要现在可以安全删
      del.forEach(function (it) { _dcSend({ type: 'conversation.item.delete', item_id: it.id }); });
      _rtc._pageFp = ''; _rtc._inkFp = ''; _rtc._sentCtxFp = '';   // 旧注入可能被删:指纹全作废(盘点实锤:此前漏清 _sentCtxFp→删掉的 ctx 同状态不重推)
      try { RC.voiceCtx && RC.voiceCtx.invalidate(); } catch (e) {}   // 统一端口指纹同步作废(唯一清理入口)
      try { threadMsg('asst-note', '📦 通话上下文已压缩(单轮输入 ' + Math.round(_rtc.inTok / 1000) + 'k tokens → 摘要+近几轮)'); } catch (e) {}
    } catch (e) { _rtc.lastCompact = 0; }
  }
  function _rtcOnEvent(e) {   // dc 下行事件 → 既有 UI 语义(对话窗/字幕/按钮/工具卡)
    var t = e.type;
    if (t === 'conversation.item.added' || t === 'conversation.item.created') {   // ㊳:记 item 账本(GA/beta 两个事件名都认)
      try { if (e.item && e.item.id && !/^sum_/.test(e.item.id)) _rtc.items.push({ id: e.item.id }); } catch (_) {}   // 125b:摘要不入账本(它在头部,slice 排序会错位)
      return;
    }
    if (t === 'response.output_audio_transcript.delta' || t === 'response.output_text.delta') {   // ㊿ 文字模式回复=output_text.delta,同一渲染管线
      _rtc.turnText = (t === 'response.output_text.delta');   // 66c:本轮模态以实际事件为准(relay 发的工具轮 create 前端不经过,旧 turnText 会错)
      if (!_rtc.turnText && !_rec.mr && !_rec.oab) _recStart();   // 兜底:没有 output_audio_buffer.* 时才按 delta 开录(有官方事件就等它)
      if (!_rtc.turnText) {   // 67:估计 2.1 音频播放结束时刻(转写字数≈5.5字/秒+缓冲)——TTS 代念等它说完再开口,不叠音
        if (!_rtc.aStart) _rtc.aStart = Date.now();
        _rtc.aEnd = _rtc.aStart + curAText.length / 5.5 * 1000 + 800;
      }
      curAText += (e.delta || ''); setSub('a', curAText); _rtcCapFeed(curAText, false);
      if (_rtc.turnText && _turnFeed) { try { _turnFeed(curAText, false); } catch (_) {} }   // 61:文字轮边生成边代念
    } else if (t === 'input_audio_buffer.speech_started') {
      _rtcBeginUserTurn();
      // Native Realtime uses interrupt_response=false: the App owns the turn
      // boundary, so a new utterance must cancel the old response and clear
      // audio that WebRTC has already buffered.
      _rtcInterrupt();
      // 先同步、后注入：刚滚动/翻页/选中/落笔后立刻开口时，pendText 与
      // sideband 状态必须先刷新，不能把上一帧内容当成“用户此刻正在看”。
      _requestSyncNow();
      _rtcFlushCtx();   // ㊵ 拉模式:用户开口瞬间注入最新位置/可见内容(VAD 判定说完前必然到达)
      var _clipH = _recFinish();   // 66:被打断的半截轮也把已播语音收下(官方事件模式下 .cleared 负责真停录)
      try { if (window.__asstVoiceLog && (curAText || _lastU)) { window.__asstVoiceLog(_lastU, curAText, _rtc.ctxFile, _rtc.ctxPage, _clipH ? { clip: _clipH } : null); _lastU = ''; } } catch (_) {}   // ㉛:被打断的半截轮落库
      curAText = ''; curAEl = null;
      try { window.__asstVoiceMsg && window.__asstVoiceMsg('reset'); } catch (_) {}
      _rtcCapReset(); capClear();   // 用户插话=打断:字幕清掉回"正在听"(对齐 WS 版 450 语义)
      if (_ttsOn()) { try { bargeIn(); } catch (_) {} }   // 61:抢话同时打断 TTS 代念残播
      _rtc.aEnd = 0; _rtc.aStart = 0;   // 67:打断=2.1 音频已被截,代念不用再等它
    } else if (t === 'output_audio_buffer.started') {   // WebRTC 专属:模型音频**真正开始播** → 此刻开录
      _rec.oab = true;
      if (!_rec.mr) _recStart();
    } else if (t === 'output_audio_buffer.stopped' || t === 'output_audio_buffer.cleared') {
      _rec.oab = true;   // **播完 / 被打断** → 此刻停录(blob 完整,不再靠估算)
      _recStop();
    } else if (t === 'input_audio_buffer.speech_stopped') {
      _rtc.turnToolAny = false;   // ㊸b 承诺核查守卫:新用户语音轮开始 → 清"本轮调过任何工具"标志(用户轮作用域,不随 response 复位)
      // ★ 133-B1(对抗审查揪出的新单点故障):session 已改回**手动挡**(create_response=false),
      //   relay 成了 response 的**唯一发起者**。此处若仍"什么都不做",一旦 sideband(ctl WS)断开
      //   —— voice-rt 重启 / 网络抖动 / ctl 重连 5 次放弃 —— 就**没有任何一方发 create**:
      //   用户说话,OpenAI 照常收音、照常 commit item,但 AI **一个字都不说**,界面还毫无提示。
      //   整通通话从此全哑,比原来的"多答"更糟。所以前端必须能顶上。
      //   互斥是天然的:_rtc.ctl 为真=relay 在仲裁(它会验转写、挡假轮);为假=没人管,前端自己发。
      if (!_rtc.ctl) { _rtcRespCreate('user'); return; }
      // ctl 在,但 relay 可能卡死 → 兜底 create。**窗口必须长于 relay 的转写超时(4s)**,
      // 否则一次慢转写就会被误当成"relay 卡死",前端多发一个 create = 双答复活。
      // 正常路径下前端根本等不到这个定时器:relay 每次裁决(放行/判假)都会回执 event:'turn',
      // 前端收到就撤销它 —— 尤其是**判假**:假轮永远不会有 response.created,
      // 没有这条回执,兜底定时器就会替假轮补发一次 create,整个闸门当场白做。
      try { clearTimeout(_rtc._createT); } catch (e) {}
      _rtc._createT = setTimeout(function () {
        if (!_rtc.on || !_rtc.ctl) return;
        try { console.warn('[vc] relay 6.5s 无裁决回执,前端兜底 create'); } catch (e) {}
        _rtcRespCreate('user');
      }, 6500);
    } else if (t === 'conversation.item.input_audio_transcription.completed') {
      var tx = (e.transcript || '').trim();
      // 133/155:转写「提示词泄漏式幻觉」——噪音让 VAD 误开一个音频轮,但那段里没人声,
      //   转写模型就把我们喂的热词 prompt **原样复读**成"用户说的话"(用户实测:噪音一响就冒
      //   「关键词:Anki、笔迹、振假名…」)。
      // ⚠ 首版这里只认旧 prompt 的两个锚点(学习伴读通话/常说:这一页),而 prompt 早已换成
      //   「关键词:…」—— 一个锚点都不含 → 前端这道过滤**全程失效**。WebRTC 下转写是**直连数据
      //   通道回浏览器**的,relay 那道闸只管"要不要生成回答",管不住前端显示,于是假转写照样
      //   出气泡、还被 capUser 落进对话记录。
      // → 改成与 relay `_is_asr_ghost` **同一套判据**(锚点 + 去标点最长公共子串≥10),
      //   判据串由 VC_ASR_MIRROR 单点持有,tests/test_asr_ghost_sync.py 守三处不再漂移。
      if (tx && _isAsrGhost(tx)) { try { console.warn('[vc] 丢弃假转写(prompt 泄漏):', tx); } catch (_) {} tx = ''; }
      if (tx) {
        _lastU = tx;
        setSub('u', tx);
        // Audio transcription previously reached only the floating subtitle.
        // __asstVoiceLog then marked the SSE echo as locally rendered, so the
        // user's spoken sentence never appeared in the open side drawer.
        try {
          window.__asstVoiceMsg && window.__asstVoiceMsg(
            'u', tx, { utterId: e.item_id || '' }
          );
        } catch (_) {}
        if (!_rtcCap.t && !_rtcCap.q.length) capUser(tx);   // whisper 迟到:AI 字幕在放就别插队打乱滚动(对话窗已有)
      }
    } else if (t === 'response.function_call_arguments.done') {
      // ㊸b(2026-07-21 用户洞察:制卡工具"派发即返回"→AI 过早口头汇报"已做好"→触发承诺核查幻影补交):
      //   模型一**发起**工具 function_call(不论 ctl 是否由 relay 执行、不等任务真完成)即算"本用户轮调过工具",
      //   立即置 turnToolAny=true → 承诺核查绝不再补交第二个任务。这兜住 relay tool_status 迟到于正答轮的竞态。
      if (e.name && e.name !== 'reply_text' && e.name !== 'wait_for_user') _rtc.turnToolAny = true;
      // ㊺P2 双通道分工:控制面在(ctl)→工具执行归 relay(sideband);前端只处理纯前端语义的
      // reply_text(显示/落库)与 wait_for_user(静音)。控制面断线=ctl false=回退前端全套。
      if (_rtc.ctl && e.name !== 'reply_text' && e.name !== 'wait_for_user') return;
      var a = {}; try { a = JSON.parse(e.arguments || '{}'); } catch (_) {}
      // 60:reply_text 参数被输出预算截断=JSON 没闭合 parse 失败→抢救 text 字段已生成的部分,别让整条回复消失
      if (e.name === 'reply_text' && !(a && a.text) && e.arguments) {
        var m1 = /"text"\s*:\s*"((?:[^"\\]|\\.)*)/.exec(e.arguments);
        if (m1) {
          var rescued = m1[1];
          try { rescued = JSON.parse('"' + m1[1] + '"'); } catch (_) {}
          a = { text: rescued + '\n\n(⚠ 回复超出输出长度被截断——想看完整内容请再问一次,或让我分段讲)' };
        }
      }
      if (e.name) {
        var toolCallId = e.call_id || '';
        _rtc.responseToolCalls = (_rtc.responseToolCalls || 0) + 1;
        _rtcTrackToolCall(toolCallId);
        Promise.resolve(_rtcTool(
          e.name,
          (a && typeof a === 'object') ? a : {},
          toolCallId
        )).then(function () {
          _rtcFinishToolCall(toolCallId);
        }, function (error) {
          // _rtcTool should close its own failures. This guard prevents an
          // unexpected rejection from leaving the visible tool card spinning.
          try { if (window.dlog) window.dlog('tool← ' + e.name + ' 未捕获异常 ' + String(error || '').slice(0, 120), '#ff6b6b'); } catch (_) {}
          _rtcFinishToolCall(toolCallId);
        });
      }
    } else if (t === 'response.created') {
      try { clearTimeout(_rtc._createT); _rtc._createT = null; } catch (e) {}   // B1:回答已开始 → 撤销哑火兜底
      curAText = ''; curAEl = null;   // 每个 response 独立气泡(text 输入触发的响应没有 speech_started,不重置会续写上一轮)
      _rtc.aStart = 0;   // 67:新轮重置音频起点(aEnd 保留——文字轮要等上一音频轮播完才代念)
      try { if (_cap.cur) _cap.cur.classList.remove('vc-cap-route'); } catch (_) {}   // 64:路由字幕样式不残留到普通轮
      _recAbort();   // 82:created 时 turnText 还是上一轮旧值(66c delta 驱动的缝隙)——不再赌预期,等首个音频 delta 定性再开录
      _turnFeed = _mkTtsFeeder();     // 61:新回复轮=新代念流(TTS 开关开且本轮文字输出时工作)
      _rtc.turnTool = false;          // ㊸ 承诺核查:本轮是否真调过工具
      _rtc.responseActive = true;
      _rtc.responseToolCalls = 0;
      // 141:气泡改按**用户轮**断,不再按 response 断 —— 一次工具调用天然是两个 response
      //   (前置语+function_call / 工具结果+正答),按 response 断必然把它俩切成两条气泡,
      //   工具卡又夹在中间 = 三块散的。现在同一用户轮内续用同一张卡(见 __asstVoiceCard)。
      //   轮次边界由 relay 的裁决回执(event:'turn')给出;ctl 断线时退回按 response 断(老行为)。
      if (_rtc._newTurn || (!_rtc.ctl && !_rtc.nativeDirect)) {
        _rtc._newTurn = false;
        try { window.__asstVoiceMsg && window.__asstVoiceMsg('reset'); } catch (_) {}
      } else {
        // 同一用户轮里的下一个 response(典型:工具结果的正答)→ **开新段落**,不断轮。
        // 'a' 是全量覆盖语义,不断槽的话正答会把前置语覆盖掉;而复制前置语又会显示两遍。
        try { window.__asstVoiceMsg && window.__asstVoiceMsg('slot'); } catch (_) {}
      }
      _rtcCapReset();                 // fed 计数跟 curAText 同步归零(不清的话新一轮切句从错误偏移入队)
      callBtnSpeaking(true);
    } else if (t === 'response.done') {
      _rtcFinishInkAck(e);
      var responseHadToolCall = _rtcResponseHasFunctionCall(e);
      _rtc.responseActive = false;
      callBtnSpeaking(false);
      if (_rtc.turnText && _turnFeed && curAText) { try { _turnFeed(curAText, true); } catch (e) {} }   // 61:残句代念收尾(通道韧性+禁麦在 _speakSafe/_ttsMicGuard)
      if (curAText) {
        // Both text and spoken Realtime responses belong in the side drawer.
        // The audio path used to update only the floating subtitle while its
        // history SSE echo was suppressed as a duplicate, leaving no record.

        try { window.__asstVoiceMsg && window.__asstVoiceMsg('a', curAText, { md: true, info: { mode: _rtc.turnText ? ('文字回复(' + (_VM_TXT[_voiceMode()] || '') + '档)') : '语音回复(GPT Realtime)',
          tools: (_rtc.recentTools || []).slice(-3).map(function (t) { return t.label || t.tool; }),
          actions: ['deep'], voiceTab: true, note: '本轮主模型=GPT Realtime(见语音 Tab);下面是它可能调用的环节' },
          pin: { label: 'AI 回答', textFn: (function (txt) { return function () { return txt; }; })(curAText) },
          speak: !!_rtc.turnText }); } catch (e) {}   // 67/77b/79/83(☆撤,+TTS念钮)
      }
      // ㊸b 承诺核查(用户设计:语音模型只是扳机、不产卡片内容——察觉"说了做卡却没调工具"时,
      // **程序直接替它把工具真调了**,种子=本轮对话上下文,后台制卡模型自己判断做什么卡;
      // 不再让语音模型多走一轮(那只是白烧一轮音频输出费),只留一条零成本 system 记录让它知道)
      if (!_rtc.nativeDirect && !_rtc.turnTool && !_rtc.turnToolAny && /已经?[^。]{0,14}(整理成|做成|放进|加进|做好)[^。]{0,10}(卡片|カード|笔记|ノート)|(卡片|笔记)[^。]{0,4}(做好了|已做好|已生成|已入库|放进后台)/.test(curAText)   // 155b:裸'卡片…已'误伤'天气卡片里已经给出'(用户截图)——收紧为明确制作动作
          && Date.now() - (_rtc.scoldT || 0) > 60000) {
        _rtc.scoldT = Date.now();
        (function () {
          var isNote = /笔记|ノート/.test(curAText) && !/卡片|カード/.test(curAText);
          var tool = isNote ? 'make_note' : 'make_anki';
          var seed = ('(语音对话中用户请求做' + (isNote ? '笔记' : '卡片') + ')\n用户说:' + (_lastU || '(语音请求)') +
                      '\nAI 口头总结的要点:' + curAText).slice(0, 1600);
          onToolStatus({ status: 'running', tool: tool, label: (isNote ? '记笔记' : '做卡片') + '(系统代提交)' });
          var _ac2 = new AbortController();   // 142:这条是 fire-and-forget(不回填 function_call_output),挂了不会掐死对话,
          setTimeout(function () { try { _ac2.abort(); } catch (e) {} }, 60000);   //   但会留一张**永远转圈**的工具卡 → 同样封超时
          fetch('/api/assistant/voice-tool', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            signal: _ac2.signal,
            body: JSON.stringify({ cmd: JSON.stringify({ tool: tool, args: { text: seed } }),
                                   ctx: { file_rel: _rtc.ctxFile, page: _rtc.ctxPage, recent_tools: (_rtc.recentTools || []).slice(-4) } }) })
            .then(function (r) { return r.json(); })
            .then(function (d) {
              onToolStatus({ status: d.ok ? 'done' : 'error', tool: tool, label: (isNote ? '记笔记' : '做卡片') + '(系统代提交)',
                             rag: JSON.stringify((d && d.result) || {}).slice(0, 400) });
              _rtcSys('(系统核查:你宣称做了' + (isNote ? '笔记' : '卡片') + '但没有调用工具——系统已代为提交任务(后台生成,完成会通知用户)。今后必须真调 ' + tool + '。状态记录,不要回应本条。)');
            }).catch(function () { onToolStatus({ status: 'error', tool: tool, label: '系统代提交失败' }); });
        })();
      }
      var _clip0 = _rec.mr ? _recFinish() : '';   // 128:官方事件模式=只取 id(音频还在播,停录交给 .stopped);回退模式=老估算收尾
      if (_rec.oab && _rec.mr) {   // 看门狗:社区实测 output_audio_buffer.stopped 偶发大延迟/不来——
        var _wid = _rec.id, _cap = Math.max(0, (_rtc.aEnd || 0) - Date.now()) + 20000;   // 估算播完 + 20s 余量
        setTimeout(function () {
          if (_rec.mr && _rec.id === _wid) { try { console.warn('[voice] output_audio_buffer.stopped 没来,看门狗收尾'); } catch (e) {} _recStop(); }
        }, Math.min(_cap, 180000));
      }
      // A response containing a function call is only the preamble, not the
      // completed user turn. Do not log it or clear the user's question.
      if (!responseHadToolCall)
      try { if (window.__asstVoiceLog) { window.__asstVoiceLog(_lastU, curAText, _rtc.ctxFile, _rtc.ctxPage, _clip0 ? { clip: _clip0 } : null); _lastU = ''; } } catch (_) {}   // ㉛:轮次落库(+66 语音)
      _rtcCapFeed(curAText, true);    // 残句入队;淡出由队列放完时收尾(_capMaybeHide),不在这直接藏
      // Fast native tools may already be complete. The originating response
      // has now ended, so one consolidated final response can safely begin.
      _rtcFlushToolResponse();
      try { var u = e.response && e.response.usage;
            if (u) {
              u._model = _rtc.model || 'mini';   // ㊶:记账按模型选价表
              if (!_rtc.nativeDirect) fetch('/api/assistant/rtc-usage', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(u), keepalive: true });
              // ㊷ §12 告警:会话接近 60 分钟硬上限(到点 OpenAI 断线,_rtcDead 会自动重连+摘要回放,但提前知会更体面)
              if (!_rtc.warned && _rtc.t0 && Date.now() - _rtc.t0 > 55 * 60000) {
                _rtc.warned = 1;
                try { threadMsg('asst-note', '⏳ 本次通话已近 60 分钟(平台单会话上限),到点会自动重连并带上对话摘要。'); } catch (e2) {}
              }
              _rtc.inTok = u.input_tokens || 0;   // ㊳ 实时监控:本轮输入总量(cached 也按 cached 价反复计费)
              if (_rtc.compactTh && _rtc.inTok >= _rtc.compactTh) _rtcCompactNow(_rtc.inTok >= 27000);   // 125b:≥27k=紧急(服务端 28672 自动截断会先吃 root 摘要)
            } } catch (_) {}
    } else if (t === 'error') {
      var m0 = (e.error && e.error.message) || '';
      if (m0 && m0.toLowerCase().indexOf('cancel') < 0) setSt('⚠ ' + m0.slice(0, 60));
    }
  }
  async function _rtcInjectHistory() {   // ㉞ 重连历史回放 → ㊲ 压缩视图:摘要(旧轮次的滚动压缩)+近几轮原文,
    if (_rtc.nativeDirect) return;
    try {                                //   替代全量原文灌注(官方指南 8.4 形态,省上下文)
      var hu = (window.__asstHistUrl && window.__asstHistUrl()) || '/api/assistant/history';   // ㉟:经侧栏同一端点(EPUB=本书 epub-convo)
      hu += (hu.indexOf('?') >= 0 ? '&' : '?') + 'compact=1';
      var h = await (await fetch(hu)).json();
      var parts = [];
      if (h && h.summary) parts.push('[此前对话的摘要]\n' + String(h.summary).slice(0, 2000));
      var lines = [];
      ((h && h.messages) || []).slice(-10).forEach(function (m) {
        var t = String(m.content || '').replace(/\s+/g, ' ').trim().slice(0, 260);
        if (t) lines.push((m.role === 'assistant' ? '你说:' : '用户说:') + t);
      });
      if (lines.length) parts.push('[最近的对话]\n' + lines.join('\n'));
      if (parts.length) _rtcSys('(通话重新接通。' + parts.join('\n') + '\n——延续这段对话的语境回答,不要重新打招呼、不要对本条做任何回应。)');
    } catch (e) {}
  }
  function _rtcDead(reason) {   // ㉞ 连接死亡(后台挂起/网络切换):明示 + 非主动挂断自动重连(带历史回放)
    if (!_rtc.on) return;
    rtcTeardown();
    ws = null; callBtnOn(false); callBtnSpeaking(false);
    if (_userHung) { setSt('已挂断'); return; }
    setSt('⚠ ' + reason + ',正在重连…');
    setTimeout(function () { if (!_userHung && !_rtc.on && !_connecting) rtcStart(toggle._opts || {}); }, 800);   // 93:_connecting=有人在拨,别叠
  }
  function _ctlOpen() {   // ㊺P1/122(#290):控制 WS 连接(可恢复重连,见 references/rtc-controller-design.md)
    // App 本机模式把短期凭证签发和大图 sideband 都放在原生层，
    // 工具函数仍由当前页面执行；不再建立任何 Pi 控制 WebSocket。
    if (_rtc.nativeDirect) {
      _rtc.ctl = false;
      _rtc.ctlWs = null;
      _stateFp = null; _inkFp = '';
      _requestSyncNow();
      return;
    }
    try {
      // fe=2 版本握手(59):声明"本前端有 P2 分工逻辑",relay 才接管工具;旧页面 JS 不带此参数
      // → relay 退回 P1 观察,防新旧换代窗口双执行(同一工具前端+relay 各跑一遍+create 撞车)
      var cw = _openWs('/voice-rt?mode=rtc&fe=5&call_id=' + encodeURIComponent(_rtc.callId) +
                             '&file=' + encodeURIComponent(_rtc.ctxFile) + '&page=' + (_rtc.ctxPage || 0) +
                             '&uid=' + encodeURIComponent(_rtc.uid || '') + '&tk=' + encodeURIComponent(_rtc.tk || ''));
      // This short-lived identity created the call. Send it only in the
      // encrypted control socket body, never in URL, logs, or storage.
      cw.onopen = function () {
        try { cw.send(JSON.stringify({ type: 'rtc_auth', client_secret: _rtc.sidebandKey || '' })); } catch (e) {}
      };
      cw.onmessage = function (ev) {
        try {
          var m0 = JSON.parse(ev.data);
          if (m0.event === 'rtc_ctl') {
            _rtc.ctl = !!(m0.payload && m0.payload.ok);
            if (_rtc.ctl) {   // 122:重挂成功——重试清零+快照重推(relay 新会话不知道选中/墨迹/页码)
              _rtc.ctlRetry = 0;
              // 133:这句"重推"以前是空转的——__vcSyncNow 会被 syncInk/syncState 的模块级指纹挡下(值没变),
              // relay 重启后 book 是全新的却永远拿不回墨迹/选中(see_ink 因此没素材)。清指纹才推得动。
              // 安全性:_rtc._inkFp/_rtc.sel 不动 → 同一份墨迹重推只喂 relay,不会再朝模型注一遍"笔迹变了"。
              _stateFp = null; _inkFp = '';
              _requestSyncNow();
      try { _ttsIrqSync(); } catch (e2) {}   // 131:接通后按当前档位/代念开关,自动进入「可打断代念」
            }
          }
          else if (m0.event === 'superseded') {
            // ★ 133 单通话唯一性:这一路被**同一账号的新通话接管**了(多标签/多设备/僵尸页)。
            // ⚠ 这里是治「后端踢人 vs 前端自动重连打乒乓」的关键(commit 0b9999c 就栽在这):
            //   若把它当成普通断线,前端会指数退避自动重连 → 建出新 call → 反过来把用户正在用的那路踢掉
            //   → 无限乒乓。所以必须置 _userHung=true 进**终态**(等同用户主动挂断,teardown 不会触发重连)。
            // ⚠ 只对**确实是自己当前这一路**生效:迟到的 superseded 可能是给旧 call 的,不能误杀新通话。
            var _sp = m0.payload || {};
            if (!_sp.call_id || _sp.call_id === _rtc.callId) {
              try { console.warn('[vc] 本通话已被新通话接管 → 进终态,不重连'); } catch (e2) {}
              _userHung = true;                 // 关键:teardown 里的重连逻辑只认"意外断线"
              try { setSt('已在其它页面/设备开始新通话 —— 本通话结束'); } catch (e2) {}
              try { teardown(false); } catch (e2) {}
            }
          }
          else if (m0.event === 'turn') {
            // 133-B1 裁决回执:relay 已对这一轮做出裁决(放行 or 判假)→ 撤销前端的哑火兜底定时器。
            // 判假尤其重要:假轮不会有 response.created,没这条回执兜底就会替它补发 create(闸门白做)。
            try { clearTimeout(_rtc._createT); _rtc._createT = null; } catch (e2) {}
            // 141:放行 = 一个**新的用户轮**开始 → 下一个 response.created 才开新气泡(工具轮不开新的)
            if ((m0.payload || {}).verdict === 'accept') _rtc._newTurn = true;
          }
          else if (m0.event === 'client_action') dispatch((m0.payload || {}).fn, (m0.payload || {}).args);
          else if (m0.event === 'tool_status') {
            var tp = m0.payload || {};
            _rtc.turnTool = true; _rtc.turnToolAny = true;   // relay 执行了工具:承诺核查放行
            // Relay 路径也在开始时钉住页码+版本；结束时只消费那一版，截图期间
            // 新增笔迹或切到别页都不会被迟到回执误清。
            if (tp.status === 'running' && tp.tool === 'see_ink') {
              var relayInkState = _rtcInkPageState(_rtc.ctxPage, false);
              _rtc.relayInkCapture = {
                page: _rtc.ctxPage,
                ver: (relayInkState && relayInkState.ver) || 0
              };
            } else if (tp.tool === 'see_ink' && (tp.status === 'done' || tp.status === 'error' || tp.status === 'aborted')) {
              var relayInkCapture = _rtc.relayInkCapture || { page: _rtc.ctxPage, ver: 0 };
              if (tp.status === 'done') _rtcMarkInkSeen('see_ink', true, relayInkCapture.ver, relayInkCapture.page);
              _rtc.relayInkCapture = null;
            }
            onToolStatus(tp);
          }
          else if (m0.event === 'need_shot') {   // ㊺P2:relay 执行 see_ink/see_page 要截图(只有浏览器能拍)
            var sid0 = (m0.payload || {}).shot_id;   // 60:带 ID 配对,防两轮工具重叠时迟到截图被下一请求接走
            var _tool0 = (m0.payload || {}).tool || '';
            // see_ink:按笔迹外接框**截局部**(灵活位置/大小,聚焦你圈的那块);拿不到则回退整视口。see_page:整视口。
            var _capP = (_tool0 === 'see_ink' && RC.captureInkRegion)
              ? RC.captureInkRegion().then(function (s) { return s || _captureView(); })
              : _captureView();
            Promise.resolve(_capP).then(function (shot) {
              try { cw.send(JSON.stringify({ type: 'shot', shot_id: sid0, b64: (shot && shot.b64) || '', media_type: (shot && shot.media_type) || 'image/jpeg' })); } catch (e2) {}
            }).catch(function () { try { cw.send(JSON.stringify({ type: 'shot', shot_id: sid0, b64: '' })); } catch (e2) {} });
          }
          else if (m0.event === 'route_text') {   // 61 route 档:服务端文本模型流式长文(relay 生成下行)——显示+落库+可选 TTS 代念
            var rp = m0.payload || {};
            if (!_rtc._route) {
              _rtc._route = { buf: '', feed: _mkTtsFeeder() };
              try { window.__asstVoiceMsg && window.__asstVoiceMsg('reset'); } catch (e2) {}
              _rtcCapReset();   // 字幕偏移与上一轮脱钩
            }
            if (rp.delta != null) {
              _rtc._route.buf += rp.delta;
              try { window.__asstVoiceMsg && window.__asstVoiceMsg('a', _rtc._route.buf); } catch (e2) {}
              _rtcCapFeed(_rtc._route.buf, false);
              try { if (_cap.cur) _cap.cur.classList.add('vc-cap-route'); } catch (e2) {}   // 64:字幕路由专属样式
              try { _rtc._route.feed(_rtc._route.buf, false); } catch (e2) {}
            }
            if (rp.done) {
              var fullR = rp.text || _rtc._route.buf;
              try { window.__asstVoiceMsg && window.__asstVoiceMsg('a', fullR, { md: true, info: { mode: '路由详答(服务端文字引擎)', actions: ['route_text'], voiceTab: true },
                pin: { label: '路由详答', textFn: (function (txt) { return function () { return txt; }; })(fullR) },
                speak: true }); } catch (e2) {}
              try { window.__asstVoiceLog && window.__asstVoiceLog(_lastU, fullR, _rtc.ctxFile, _rtc.ctxPage); _lastU = ''; } catch (e2) {}
              _rtcCapFeed(fullR, true);
              try { _rtc._route.feed(fullR, true); } catch (e2) {}
              // 132(用户):字幕已经在显示 AI 的文字输出 → 文字输出档**不再另弹一张卡**(重复且挡内容)
              _rtc._route = null;
            }
          }
        } catch (e) {}
      };
      cw.onclose = function () {   // 122(#290):可恢复重连——relay 重启/网络抖动后自动重挂 P2(退避≤5次);挂断由 teardown 摘回调不触发
        _rtc.ctl = false; _rtc.ctlWs = null;
        if (!_rtc.on) return;
        var n = (_rtc.ctlRetry = (_rtc.ctlRetry || 0) + 1);
        if (n > 5) { console.warn('[vc] ctl 重连放弃(纯前端模式接管工具)'); return; }
        setTimeout(function () { if (_rtc.on && !_rtc.ctlWs) _ctlOpen(); }, Math.min(8000, 800 * Math.pow(2, n - 1)));
      };
      cw.onerror = function () {};
      _rtc.ctlWs = cw;
    } catch (e) { _rtc.ctl = false; }
  }

  // 133:过期拨号的自我了断。**关键在最后一句**:媒体是浏览器↔OpenAI 直连,pc.close() 只切断本端,
  // /rtc-call 已经在 OpenAI 侧建起来的那路 call **依然活着**(继续收音频、继续计费、继续跟新通话抢答)。
  // 必须调官方 hangup 从服务端真正终止它,否则就是我们观测到的"两路通话都在答"的僵尸 call。
  function _rtcRequestHangup(callId, sidebandSecret) {
    if (!callId) return;
    try {
      if (window.__BW_NATIVE_OPENAI_REALTIME__ === true &&
          window.__bwNativeRealtime &&
          typeof window.__bwNativeRealtime.request === 'function' &&
          /^ek_[A-Za-z0-9_-]{8,4096}$/.test(String(sidebandSecret || ''))) {
        window.__bwNativeRealtime.request({
          action: 'hangup', call_id: callId,
          client_secret: sidebandSecret || ''
        }).catch(function () {});
        return;
      }
    } catch (e) {}
    try {
      fetch('/api/assistant/rtc-hangup', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        keepalive: true, body: JSON.stringify({ call_id: callId,
                                                rtc_sideband_secret: sidebandSecret || '' }) }).catch(function () {});
    } catch (e) {}
  }
  function _rtcAbandon(pc, mic, callId, sidebandSecret) {
    try { if (pc) pc.close(); } catch (e) {}
    try { if (mic) mic.getTracks().forEach(function (t) { t.stop(); }); } catch (e) {}
    if (!callId) return;
    _rtcRequestHangup(callId, sidebandSecret);
    try { console.warn('[vc] 过期拨号已弃用并挂断 call=' + String(callId).slice(0, 14)); } catch (e) {}
  }

  function _installedDirectRtcClient() {
    // App local books and the browser extension own their network stack and
    // can call OpenAI with a short-lived ek_ credential. The server PWA keeps
    // the legacy SDP proxy fallback until its CSP is migrated separately.
    return window.__BW_NATIVE_LOCAL_READER__ === true ||
      typeof window.__bwReaderFetch === 'function';
  }

  function _nativeRealtimeRequest(payload) {
    try {
      var bridge = window.__bwNativeRealtime;
      if (window.__BW_NATIVE_OPENAI_REALTIME__ !== true || !bridge ||
          typeof bridge.request !== 'function') {
        return Promise.reject(new Error('App 原生 Realtime 尚未就绪'));
      }
      return Promise.resolve(bridge.request(payload));
    } catch (error) {
      return Promise.reject(error);
    }
  }

  async function _openDirectRtcCall(sdp, file, page) {
    var tokenRes = null, nativeDirect = false;
    var appRequiresNative = window.__BW_NATIVE_LOCAL_READER__ === true;
    if (appRequiresNative) {
      if (window.__BW_NATIVE_OPENAI_REALTIME__ !== true) {
        throw new Error('App 原生 Realtime 桥尚未就绪');
      }
      var nativeCall = await _nativeRealtimeRequest({
        action: 'call', sdp: sdp, file: file || '', page: page || 0
      });
      if (!nativeCall || !nativeCall.ok ||
          !/^rtc_[A-Za-z0-9_-]{8,160}$/.test(String(nativeCall.call_id || '')) ||
          !/^ek_[A-Za-z0-9_-]{8,4096}$/.test(String(nativeCall.client_secret || '')) ||
          !/^v=0(?:\r?\n|$)/.test(String(nativeCall.sdp || ''))) {
        throw new Error((nativeCall && nativeCall.error) || 'App 原生 Realtime 建连响应无效');
      }
      return {
        ok: true, sdp: nativeCall.sdp, call_id: nativeCall.call_id,
        uid: '', ticket: '', sideband_secret: nativeCall.client_secret,
        native_direct: true, model: nativeCall.model || '',
        rt_image: !!nativeCall.rt_image,
        compact_tokens: nativeCall.compact_tokens || 0
      };
    }
    if (window.__BW_NATIVE_OPENAI_REALTIME__ === true) {
      tokenRes = await _nativeRealtimeRequest({
        action: 'mint', file: file || '', page: page || 0
      });
      nativeDirect = !!(tokenRes && tokenRes.ok);
      if (!nativeDirect) {
        throw new Error((tokenRes && tokenRes.error) || 'App 原生 Realtime 临时凭证签发失败');
      }
    } else {
      tokenRes = await (await fetch('/api/assistant/rtc-client-secret', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file: file || '', page: page || 0 })
      })).json();
    }
    if (!tokenRes || !tokenRes.ok || !/^ek_[A-Za-z0-9_-]{8,4096}$/.test(String(tokenRes.client_secret || ''))) {
      throw new Error((tokenRes && tokenRes.error) || 'Realtime 临时凭证签发失败');
    }
    var direct = await fetch('https://api.openai.com/v1/realtime/calls', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + tokenRes.client_secret,
        'Content-Type': 'application/sdp'
      },
      body: sdp,
      credentials: 'omit',
      cache: 'no-store'
    });
    if (!direct.ok) {
      var detail = '';
      try { detail = String(await direct.text()).slice(0, 300); } catch (e) {}
      throw new Error('OpenAI Realtime ' + direct.status + (detail ? ': ' + detail : ''));
    }
    var callId = '';
    try {
      try { callId = String(direct.headers.get('Location') || '').replace(/\/$/, '').split('/').pop(); } catch (e) {}
      if (!/^rtc_[A-Za-z0-9_-]{8,160}$/.test(callId)) {
        throw new Error('OpenAI Realtime 未返回 call_id');
      }
      var answer = await direct.text();
      var bind = { ok: true, uid: '', ticket: '' };
      if (!nativeDirect) {
        bind = await (await fetch('/api/assistant/rtc-bind', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ call_id: callId, bind_grant: tokenRes.bind_grant || '' })
        })).json();
        if (!bind || !bind.ok) {
          throw new Error((bind && bind.error) || 'Realtime 控制通道绑定失败');
        }
      }
      return {
        ok: true, sdp: answer, call_id: callId,
        uid: bind.uid || '', ticket: bind.ticket || '',
        sideband_secret: tokenRes.client_secret,
        native_direct: nativeDirect,
        model: tokenRes.model || '', rt_image: !!tokenRes.rt_image,
        compact_tokens: tokenRes.compact_tokens || 0
      };
    } catch (error) {
      _rtcRequestHangup(callId, tokenRes.client_secret);
      throw error;
    }
  }

  async function rtcStart(opts) {
    if (_reviewVoiceGate(false)) return;
    // 93(用户实测双回答根因):单飞锁——通话已在/舞步进行中,任何来路的第二次拨号直接吞。
    // 没有这把锁时,清空重拨与迟到的自动重连可各建一个 call,两个模型同时听同一句各答一条。
    if (_rtc.on || _connecting) { try { console.warn('[vc] rtcStart 被单飞锁拦下(on=' + _rtc.on + ' connecting=' + _connecting + ')'); } catch (e) {} return; }
    var g = ++_gen;
    var fresh = !!toggle._fresh; toggle._fresh = false;   // 新话题:不回放历史(WebRTC 每连接本就是新会话)
    _rtc.ctxFile = (opts && opts.file) || ''; _rtc.ctxPage = (opts && opts.page) || 0;
    _rtcResetInkPages();
    _rtc.activeInkPage = null;
    _rtc.ink = null; _rtc.sel = ''; _rtc._inkFp = ''; _rtc.inkDirty = false;
    _rtc.hasInk = false; _rtc.inkVer = 0; _rtc.inkSeenVer = 0;
    _rtc.inkResponseAcks = Object.create(null); _rtc.inkAckSeq = 0; _rtc.turnEpoch = 0;
    _rtc.visualTurnEpoch = -1;
    _rtc._pageFp = ''; _rtc._sentCtxFp = '';
    _rtc.relayInkCapture = null; _rtc.nativeDirect = false;
    _rtcUseInkPage(_rtc.ctxPage);
    // 133(用户实测"圈完问这是什么,它说看不到"):上面清的是 _rtc.* 的注入指纹,但**发不发**由 syncInk/syncState
    // 各自的模块级指纹(_inkFp/_stateFp)说了算——它们只在 WS 版 start()(:3104)清过,rtcStart 这条路一直漏。
    // 后果:同一页面第二通电话起,墨迹/选中的指纹跟上一通一样 → syncInk 直接 return → 新会话**永远收不到**
    // 笔迹状态消息(relay 的 book.ink_strokes 也是空的)→ 模型不知道纸上有圈画,只能答"你把截图发一下"。
    _stateFp = null; _inkFp = '';
    if (toggle._opts) { toggle._opts._syncedPage = 0; toggle._opts._vtFp = ''; }   // 123:同款清零(RTC 重连同风险)
    var startupPc = null, startupMic = null, startupCallId = '', startupSidebandKey = '';
    var startupStage = '准备音频';
    try {
      setSt('连接中(WebRTC)…');
      callBtnConnecting(true);   // 96:按下即琥珀脉冲,"确实在等它开启"
      _connecting = true;
      // ㉑铁律(WS 版 start() 同款舞步,rtcStart 之前漏了=挂断后重拨 getUserMedia 必死的根因):
      // 挂断/朗读会把 iOS 音频会话切回 'playback'(该类别**静音麦克风**)——开麦前必须先撂下朗读通道
      // 再显式声明 play-and-record,否则第一次通话能成、之后每次都"启动失败"
      try { await _ttsShutdown(); } catch (e) {}
      if (g !== _gen || _reviewVoiceGate(false)) {
        if (g === _gen) { _connecting = false; callBtnConnecting(false); }
        return;
      }
      _audioSession('play-and-record');
      // ㊶ 指南§8.1:instructions 恒定(不含书页动态内容)→ 跨会话缓存可命中;开话视口进拉模式池,首次开口才注入
      try { _rtc.pendText = String(((window.RC && RC.adapter && RC.adapter().getContext()) || {}).visible_text || '').slice(0, 2000); } catch (e) {}
      var directRtc = _installedDirectRtcClient();
      if (!directRtc) {
        var sres = await (await fetch('/api/assistant/rtc-session', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ file: _rtc.ctxFile, page: _rtc.ctxPage }) })).json();
        if (!sres || !sres.ok) throw new Error((sres && sres.error) || 'rtc-session 失败');
        if (g !== _gen || _reviewVoiceGate(false)) {
          if (g === _gen) { _connecting = false; callBtnConnecting(false); }
          return;
        }
        _rtc.imgOn = !!sres.rt_image;
        _rtc.model = sres.model || '';   // ㊶ 账本按模型分价表(mini vs 标准版差 6-8 倍)
        _rtc.compactTh = sres.compact_tokens || 0;   // ㊳ 会话内压缩阈值(0=关)
      }
      _rtc.items = []; _rtc.inTok = 0; _rtc.lastCompact = 0;
      startupStage = '申请麦克风权限';
      var mic = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
      startupMic = mic;
      if (g !== _gen || _reviewVoiceGate(false)) {
        mic.getTracks().forEach(function (t) { t.stop(); });
        if (g === _gen) { _connecting = false; callBtnConnecting(false); }
        return;
      }
      var pc = new RTCPeerConnection();
      startupStage = '创建 WebRTC 连接';
      startupPc = pc;
      mic.getTracks().forEach(function (t) {
        pc.addTrack(t, mic);
        // ㊴ 后台"他听不到我":iOS 切到其他 app 时系统**静音网页麦克风**(下行 audio 可继续=你还听得到他),
        // 平台限制无法绕过——能做的是如实提示 + 回前台自动恢复;track 被彻底回收(ended)则自动重连重新拉流
        t.onmute = function () { if (_rtc.on) setSt('⚠ 麦克风被系统暂停(iOS 后台限制)——回到页面即恢复'); };
        t.onunmute = function () { if (_rtc.on) setSt('通话中(WebRTC·外放可用)'); };
        t.onended = function () { if (_rtc.on && _rtc.mic === mic) _rtcDead('麦克风被系统回收'); };
      });
      var dc = pc.createDataChannel('oai-events');
      dc.onmessage = function (ev) { try { _rtcOnEvent(JSON.parse(ev.data)); } catch (e) {} };
      dc.onopen = function () { if (!fresh) _rtcInjectHistory(); try { RC.voiceCtx && RC.voiceCtx.flushPending('rtc'); } catch (e) {} };   // ㉞:历史回放在前(延续语境),再补投 pending 通告(统一端口)
      dc.onclose = function () {   // 通话中 dc 意外关闭(如超限消息触发的规范性关闭)→ 重连自愈,别无声哑死
        if (_rtc.on && _rtc.dc === dc) _rtcDead('数据通道断开');
      };
      pc.onconnectionstatechange = function () {   // ㉞ 假活根治:iOS 切后台系统掐 WebRTC,回来 UI 还显示"通话中"
        if (!_rtc.on || _rtc.pc !== pc) return;
        var st = pc.connectionState;
        if (st === 'failed' || st === 'closed') _rtcDead('连接断开');
        else if (st === 'disconnected') setTimeout(function () {   // disconnected 可自愈(短暂网络抖动),3s 没恢复才判死
          if (_rtc.on && _rtc.pc === pc && pc.connectionState === 'disconnected') _rtcDead('连接断开');
        }, 3000);
      };
      pc.ontrack = function (e) {   // 远端音频:audio 元素播放(WebRTC 路径=浏览器 AEC 生效的关键)
        try { _rtc.remoteStream = e.streams[0]; } catch (e2) {}   // 66:同一路流供按轮录制(历史可回放当时语音)
        var el = document.createElement('audio');
        el.autoplay = true; el.setAttribute('playsinline', ''); el.style.display = 'none';
        el.srcObject = e.streams[0];
        document.body.appendChild(el);
        _rtc.el = el;
        try { el.play(); } catch (_) {}
      };
      var offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      if (g !== _gen || _reviewVoiceGate(false)) {
        _rtcAbandon(pc, mic, '');
        if (g === _gen) { _connecting = false; callBtnConnecting(false); }
        return;
      }
      // 133(配置错位,外部评审揪出):/rtc-call 的后端**忽略** body["session"](安全加固:防篡改型号/token 上限),
      // 一律用 body 的 file/page **重建**会话——可这里以前根本没发 file/page → 真正建起来的 OpenAI 会话
      // 书名为空、page=0(日志实证:ASR prompt 里只有通用"学习伴读通话",没有书名)。
      // /rtc-session 那次辛苦算好的配置等于**被整个丢弃**。必须在这里也带上。
      startupStage = directRtc ? '连接 OpenAI Realtime' : '连接语音服务';
      var cres = directRtc
        ? await _openDirectRtcCall(offer.sdp, _rtc.ctxFile, _rtc.ctxPage)
        : await (await fetch('/api/assistant/rtc-call', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ sdp: offer.sdp, file: _rtc.ctxFile, page: _rtc.ctxPage }) })).json();
      if (!cres || !cres.ok) throw new Error((cres && cres.error) || 'Realtime 建连失败');
      startupCallId = cres.call_id || '';
      if (directRtc) {
        _rtc.imgOn = !!cres.rt_image;
        _rtc.nativeDirect = !!cres.native_direct;
        startupSidebandKey = cres.sideband_secret || '';
        _rtc.sidebandKey = startupSidebandKey;
        _rtc.model = cres.model || '';
        _rtc.compactTh = cres.compact_tokens || 0;
      }
      if (g !== _gen || _reviewVoiceGate(false)) {
        _rtcAbandon(pc, mic, cres.call_id, cres.sideband_secret || '');
        if (g === _gen) { _connecting = false; callBtnConnecting(false); }
        return;
      }
      startupStage = '应用 OpenAI 应答';
      await pc.setRemoteDescription({ type: 'answer', sdp: cres.sdp });
      // 133(双通话真凶,外部评审揪出):setRemoteDescription **也是一个 await** —— 这期间用户完全可能
      // 挂断+重拨(teardown 会 ++_gen)。旧代码在此直接提交 _rtc.pc/_rtc.on,于是**过期的这一轮醒来后
      // 把自己的 pc 覆盖写进 _rtc**,新通话的 pc 就此失去引用:关不掉、却还活着、还在喂麦克风
      //  → 同一浏览器两路通话都听同一句话、各答一次(实测 17:48 那次 4 秒内两次拨号即此)。
      // 每个 await 之后都必须重新验世代;过期就把**自己建的**资源全收掉(含服务端已建的 call)。
      if (g !== _gen || _reviewVoiceGate(false)) {
        _rtcAbandon(pc, mic, startupCallId, startupSidebandKey);
        if (g === _gen) { _connecting = false; callBtnConnecting(false); }
        return;
      }
      // 只有最后一个 await 之后仍属当前世代，才把 call 身份提交到共享状态；
      // 迟到的旧拨号不能覆盖新通话的 callId/票据，更不能误挂断新通话。
      _rtc.callId = startupCallId;   // sideband 注入(后端发大图)要用
      _rtc.uid = cres.uid || ''; _rtc.tk = cres.ticket || '';   // 133:单通话唯一性票据(relay 验签后才敢接管旧通话)
      _rtc.pc = pc; _rtc.dc = dc; _rtc.mic = mic; _rtc.on = true;
      startupStage = '完成';
      _rtc.t0 = Date.now(); _rtc.toolN = 0; _rtc.warned = 0;   // ㊷ §12/§13:会话时长与工具调用护栏计数
      _connecting = false;
      ws = _rtcShimWs();   // 顶替全局 ws:同步轮询/输入框发送等全部现有代码照常工作
      _userHung = false; _acquireWL();
      setSt('通话中(WebRTC·外放可用)'); if (box) box.classList.add('on'); callBtnOn(true);
      try { var _ai0 = document.getElementById('asst-input'); if (_ai0) _ai0.classList.add('vc-live'); } catch (e) {}   // 66:输入框紫光=打字直达 2.1
      capWait(true);   // 等待指示点亮(对齐 WS 版 150/agent_ready)
      // 124(#287):getStats 遥测——诊断"断续/听不清"用数据说话(丢包/抖动/RTT 每 10s 上报,relay 落时间线)
      _rtc.statsT = setInterval(function () {
        if (!_rtc.on || !_rtc.pc) return;
        _rtc.pc.getStats().then(function (rep0) {
          var o = {};
          rep0.forEach(function (st) {
            if (st.type === 'inbound-rtp' && st.kind === 'audio') { o.lost = st.packetsLost; o.jit = Math.round((st.jitter || 0) * 1000); }
            if (st.type === 'candidate-pair' && st.state === 'succeeded' && st.currentRoundTripTime != null) o.rtt = Math.round(st.currentRoundTripTime * 1000);
          });
          if (o.lost != null || o.rtt != null) { try { ws && ws.send(JSON.stringify({ type: 'rtcstats', s: o })); } catch (e2) {} }
        }).catch(function () {});
      }, 10000);
      // ㊺P1 控制 WS:抽出为 _ctlOpen(122:可恢复重连);连不上=静默纯前端模式(现有代码即 fallback)
      _rtc.ctlRetry = 0;
      _ctlOpen();
      _refreshSpeakTg();
    } catch (ex) {
      _connecting = false;
      var errorName = String(ex && ex.name || '');
      var errorDetail = String(ex && ex.message || ex || '未知错误');
      if (startupStage === '申请麦克风权限' && errorName === 'NotAllowedError') {
        errorDetail = '麦克风权限未授予，请在 iPad“设置 → BWReader → 麦克风”中开启';
      }
      var startupMessage = '语音启动失败（' + startupStage + '）：' +
        (errorName && errorName !== 'Error' ? errorName + ' ' : '') +
        errorDetail.slice(0, 220);
      setSt(startupMessage);
      try { if (window.dlog) window.dlog('[voice] ' + startupMessage, '#ff6961'); } catch (e) {}
      try { if (window.RC && RC.toast) RC.toast(startupMessage); } catch (e) {}
      try { window.dispatchEvent(new CustomEvent('bw-native-realtime-start-error', {
        detail: { stage: startupStage, name: errorName, message: errorDetail.slice(0, 300) }
      })); } catch (e) {}
      _rtcAbandon(startupPc, startupMic, startupCallId, startupSidebandKey);
      rtcTeardown();
      ws = null; callBtnOn(false); callBtnSpeaking(false);   // 状态复位:按钮/shim 不残留假活
    }
  }
  function rtcTeardown() {
    var retiringCallId = _rtc.callId || '';
    var retiringSidebandKey = _rtc.sidebandKey || '';
    _rtc.on = false;
    try { var _ai1 = document.getElementById('asst-input'); if (_ai1) _ai1.classList.remove('vc-live'); } catch (e) {}
    _recStop();   // 128:挂断时把还在录的那段**收下并上传**(别丢掉用户刚听到的最后一段)
    _rec.oab = false;
    try { _aecKill(_taec); } catch (e) {}   // 131:退出「可打断代念」(环回随通话走)
    _rtcCapReset();
    // 摘回调再关:onclose 不摘=主动挂断被当成意外断线触发重连;onmessage 不摘=旧 ctlWs 的迟到消息
    // (尤其 superseded)会打到**新通话**的状态上(133:_userHung 被误置 → 新通话当场自杀)。
    try { if (_rtc.ctlWs) { _rtc.ctlWs.onclose = null; _rtc.ctlWs.onmessage = null; _rtc.ctlWs.close(); } } catch (e) {}
    _rtc.ctlWs = null; _rtc.ctl = false;
    try { if (_rtc.dc) _rtc.dc.close(); } catch (e) {}
    try { if (_rtc.pc) _rtc.pc.close(); } catch (e) {}
    try { if (_rtc.el) _rtc.el.remove(); } catch (e) {}
    try { if (_rtc.mic) _rtc.mic.getTracks().forEach(function (t) { t.stop(); }); } catch (e) {}
    _rtc.pc = null; _rtc.dc = null; _rtc.el = null; _rtc.mic = null; _rtc.callId = ''; _rtc.sidebandKey = ''; _rtc.nativeDirect = false;
    _rtcRequestHangup(retiringCallId, retiringSidebandKey);   // pc.close 不等于服务端挂断；正常挂断/意外重连都明确终止旧 call
    try { if (_rtc.statsT) { clearInterval(_rtc.statsT); _rtc.statsT = null; } } catch (e) {}   // 124:遥测随挂断走
    _connecting = false;   // 93:teardown 不经 teardown() 的路径(_rtcDead)也要解锁,否则单飞锁永久卡死
  }

  function _agentCtxQs() {   // ASR 语境注入(㉓):开 ASR 时把当前书/页带给 relay → 握手注入热词+页面语境
    try {
      var c = (window.RC && RC.adapter && RC.adapter() && RC.adapter().getContext && RC.adapter().getContext()) ||
              ((typeof window.__voiceContext === 'function') ? window.__voiceContext() : null) || {};
      var f = c.file_rel || c.file || '';
      var pg = c.page || (c.current_section_idx != null ? (c.current_section_idx + 1) : 0);   // ㉟ EPUB:section idx+1 当 page
      if (f) return '&file=' + encodeURIComponent(f) + '&page=' + pg;
    } catch (e) {}
    return '';
  }
  async function start(opts) {
    opts = opts || {};
    if (_reviewVoiceGate(false)) return;
    // 108(用户实测 Grok 多连接并存):单飞锁——93 只锁了 rtcStart,WS 引擎这条路漏配。
    // ws 活着/舞步进行中,任何来路的第二次拨号直接吞(teardown/断线都会把 ws 置 null,合法重拨不受影响)。
    if (ws || _connecting) { try { console.warn('[vc] start 被单飞锁拦下(ws=' + !!ws + ' connecting=' + _connecting + ')'); } catch (e) {} return; }
    // 新连接 = relay 端 book 状态全新 → 指纹清零,让 __vcSyncNow 下一轮把选中/墨迹/页码重推上去
    // (旧代码重连/🧹后指纹残留 → 状态永不重推,relay 不知道选中和圈画)
    _stateFp = null; _inkFp = '';
    if (toggle._opts) { toggle._opts._syncedPage = 0; toggle._opts._vtFp = ''; }   // 123:翻页同步指纹随新会话清零(重连后重推页码)
    // 连接世代:teardown/新 start 都推进 _gen;在飞的旧 start 每个 await 后自检,过期就清掉
    // 自己建的资源退出(否则 iOS 卡死的旧回合会在用户触屏后"复活",跟新回合抢出双连接+泄漏 AudioContext)
    var g = ++_gen, myAc = null, myMic = null;
    _upRate = 16000; _halfDuplex = false; _lastPlayEnd = 0;   // 每次连接复位;GPT 由 relay 的 up_rate 事件设置
    function _dead() { return g !== _gen || _assistantInReview(); }
    function _cleanLocal() {
      try { if (myMic) myMic.getTracks().forEach(function (t) { t.stop(); }); } catch (e) {}
      try { if (myAc) myAc.close(); } catch (e) {}
      if (g === _gen && _assistantInReview()) {
        _connecting = false;
        callBtnConnecting(false);
      }
    }
    _connecting = true;   // 建立窗口(到 ws 赋值前):挡住 _ttsEnsure 把类别改回 playback(那会让开出来的麦静音)
    try {
      try { await (_ttsShutdown() || 0); } catch (e) {}   // 关掉朗读通道并**等 close 落地**:playback 会话还激活着的话类别切不动;通话中朗读走通话 ws,不需要它
      _audioSession('play-and-record');   // 开麦通话:必须在建 AudioContext **之前**声明——会话一旦以 playback 激活,活跃中改类别 iOS 不可靠,麦克风会保持静音
      // 24k 定频(官方 console 同款):默认 48k 时每个 24k chunk 被**独立重采样**,边界无连续性=周期性 click/断续。
      // 采集侧同受益:worklet 以 24k 出帧,onCap 降采样更干净(iOS 14.5+ 支持 sampleRate 选项,失败回退默认)。
      try { myAc = new (window.AudioContext || window.webkitAudioContext)({ sampleRate: 24000 }); }
      catch (e0) { myAc = new (window.AudioContext || window.webkitAudioContext)(); }
      // ⚠ iOS 坑:非用户手势环境(后台断线自动重连)里 suspended AudioContext 的 resume() 可能
      // **永远 pending(不 resolve 不 reject)** → 旧代码 await 死等 = "一直显示重连中"的根因。
      // 改 800ms 超时竞速:suspended 也继续建链路(ws/字幕都通,只是暂时无声),等用户碰一下屏幕
      // 由常驻 pointerdown 监听 resume 恢复声音。
      try { await Promise.race([myAc.resume(), new Promise(function (r) { setTimeout(r, 800); })]); } catch (e) {}
      if (_dead()) { _cleanLocal(); return; }
      myMic = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
      if (_dead()) { _cleanLocal(); return; }
      ac = myAc; micStream = myMic;
      await ac.audioWorklet.addModule(URL.createObjectURL(new Blob([WORKLET], { type: 'text/javascript' })));
      if (_dead()) { _cleanLocal(); return; }
      var src = ac.createMediaStreamSource(micStream);
      capNode = new AudioWorkletNode(ac, 'vccap');
      capNode.port.onmessage = function (e) { onCap(e.data, ac.sampleRate); };
      src.connect(capNode);
      var qs = (mode === 'agent')
        ? '?mode=agent' + _agentCtxQs()
        : '?file=' + encodeURIComponent(opts.file || '') + '&page=' + (opts.page || 0) + (toggle._fresh ? '&fresh=1' : '');
      toggle._fresh = false;
      ws = _openWs('/voice-rt' + qs);
      if (_dead()) { try { ws.close(); } catch (e) {} ws = null; _cleanLocal(); return; }   // 108:过期回合不许留活连接
      _connecting = false;   // ws 已赋值:_ttsEnsure 的 !ws gate 接棒
      ws.binaryType = 'arraybuffer';
      ws.addEventListener('open', function () {   // 99:回声桥判定(auto=检测到耳机就直连;总是/关闭按设置)
        if (mode !== 's2s') return;
        var bp = _bridgePref();
        if (bp === '0') return;
        _headphonesIn().then(function (hp) {
          _hpIn = hp;   // 119:耳机状态供半双工判定(耳机=可打断)
          if (bp === 'auto' && hp) { console.warn('[vc] 检测到耳机,直连(不走回声桥)'); return; }
          if (myMic) _abridgeStart(myMic);
        });
      });
      ws.onopen = function () {
        _userHung = false; _reconnN = 0; _reconnPend = false; _acquireWL();
        setSt('通话中 · 说话即可(已带上本页内容)'); if (box) box.classList.add('on'); callBtnOn(true);
        taPlaceholder(mode === 'agent' ? '🎙 连续听中,说话即可…' : null);
        _refreshSpeakTg();   // 朗读开关切到当前语境的键(S2S=默认亮/其余=旧键)
        try { _aecSetup(ac); } catch (e) {}   // AEC 环回(手势链内建,fire-and-forget;失败播放直连兜底)
      };
      // teardown 会摘掉本回调 → 走到这里的必然是"没人主动挂断"的意外断线(网络波动/iOS 切后台掐 ws)
      ws.onclose = function () { _scheduleReconnect(); };
      ws.onerror = function () { setSt('连接出错'); };
      ws.onmessage = function (ev) {
        if (ev.data instanceof ArrayBuffer) { playPcm(ev.data); return; }
        var m; try { m = JSON.parse(ev.data); } catch (e) { return; }
        if (mode === 'agent') { handleAgentMsg(m); return; }
        var p = m.payload || {};
        if (m.event === 'up_rate') { _upRate = p.rate || 16000; _halfDuplex = !!p.half_duplex; return; }   // relay 声明上行采样率(GPT=24k)+半双工模式
        if (m.event === 'client_action') { dispatch(p.fn, p.args); return; }
        if (m.event === 'tool_status') { onToolStatus(p); return; }   // 执行通知 → 固定状态按钮(不进对话流,用户设计)
        if (m.event === -1 || m.event === 153 || m.event === 599 || m.code) { setSt('⚠ ' + (p.error || p.message || '').slice(0, 60)); return; }
        if (m.event === 150) { setSt('通话中(会话已建立)'); capWait(true); }
        else if (m.event === 359) {   // 一轮播完
          _playStat = { t0: 0, queued: 0 };   // 整轮播完=无需 truncate,统计清零
          try { if (window.__asstVoiceLog) { var _cw = _wsRecFinish(false); window.__asstVoiceLog(_lastU, curAText, (toggle._opts || {}).file, (toggle._opts || {}).page, _cw ? { clip: _cw } : null); _lastU = ''; } } catch (e) {}   // ㉛:轮次落库(与文字对话同历史)+66b 原声 clip
          curAText = ''; curAEl = null;   // #53②:一轮播完清累加器 → 下一轮(非打断式提问)从空开始,不再 A1A2 合并(event 450 打断路 3774 早有此清,359 漏了=既有 bug)。不加 reset:顺序由 rc-assistant 的 _vAnswered 门管,避免双重换轮
          if (p.status_code === '20000002') { setSt('👋 好,下次再聊'); setTimeout(function () { teardown(true); }, 2500); }   // 说"挂了吧/再见"→播完告别语自动挂断
          else _capMaybeHide(2500);   // 字幕停留几秒 → 回"正在听"等待态
        }
        else if (m.event === 450) {   // 用户开口:打断播报 + **立即同步一次上下文**(墨迹/选中,赶在模型答题前——治刚画完就问的竞态)
          _reportPlayed();   // 停播前先回报真实已播毫秒(GPT 引擎 relay 用它 truncate;豆包忽略该消息)
          _wsRecCutPend();   // 66b:上一轮若还在等队列放完 → 立即截住(别把新轮录进尾巴)
          try { if (window.__asstVoiceLog && (curAText || _lastU)) { var _cw2 = _wsRecFinish(true); window.__asstVoiceLog(_lastU, curAText, (toggle._opts || {}).file, (toggle._opts || {}).page, _cw2 ? { clip: _cw2 } : null); _lastU = ''; } else { _wsRecAbort(); } } catch (e) {}   // ㉛:被打断的半截轮也落库(下一轮覆盖前)+66b 原声(无文本轮=丢录音)
          stopPlayback(); curAText = ''; curAEl = null;
          try { window.__asstVoiceMsg && window.__asstVoiceMsg('reset'); } catch (e) {}   // ㉛:断 AI 轮气泡(下一轮开新气泡,别覆盖旧的)
          _requestSyncNow();
        }
        else if (m.event === 'bridge_answer') {   // 99:回声桥 answer
          try { if (_abridge.pc) _abridge.pc.setRemoteDescription({ type: 'answer', sdp: (p || {}).sdp || '' }); } catch (e2) {}
        }
        else if (m.event === 451) {   // ASR 转写:对话窗定稿句 + 字幕用户句(interim 也实时上屏)
          var r = (p.results || [])[0] || {};
          if (r.text && r.is_interim === false) { _lastU = r.text; setSub('u', r.text, r.iid); }
          else if (r.text && r.iid) setSub('u', r.text, r.iid);   // 112(用户规范):grok 可修订全文——interim 也进对话窗,同 iid 覆盖同一气泡
          if (r.text) capUser(r.text);
        }
        else if (m.event === 550) { curAText += (p.content || ''); setSub('a', curAText); capStream('a', curAText); }   // S2S 回复:对话窗 + 字幕(尾句 cur/前一句 prev)
      };
    } catch (ex) {
      _connecting = false;
      _cleanLocal();
      if (!_dead()) { setSt('启动失败: ' + ex.message); teardown(false); }
    }
  }

  function teardown(closeBox, preserveComputerGesture) {
    var wasNativeRealtime = !!_rtc.nativeDirect;
    // 从普通电话切到电脑按钮时，捕获阶段刚取得的 iOS 可信手势必须原样保留；
    // setDialPending(false) 本身也会释放 prepared surface，不能只跳过 cancel。
    if (!preserveComputerGesture) {
      _setComputerVoiceDialPending(false);
      _cancelComputerVoiceGesture();
    }
    if (_computerVoiceUnsub) {
      try { _computerVoiceUnsub(); } catch (e) {}
      _computerVoiceUnsub = null;
    }
    try {
      if (window.RC && RC.computerVoice && RC.computerVoice.isActive &&
          RC.computerVoice.isActive()) {
        RC.computerVoice.stop('reader-hangup').catch(function () {});
      }
    } catch (e) {}
    try { _wsRecAbort(); } catch (e) {}   // 66b:挂断=丢弃未定稿录音(已定稿的由闭包照常上传)
    _gen++;   // 杀死在飞的 start(卡在 iOS resume/getUserMedia 上的旧回合过期自毁,不会复活抢连接)
    _connecting = false;
    callBtnConnecting(false);   // 96:挂断/失败即退出等待态
    if (_rtc.on) rtcTeardown();   // WebRTC 通道随挂断走(pc/dc/audio 元素/mic 全清)
    _abridgeStop();   // 99:回声桥随挂断走
    _userHung = true; _releaseWL();
    if (_reconnT) { clearTimeout(_reconnT); _reconnT = null; }
    _reconnPend = false;
    // 摘回调再关:主动挂断不触发 onclose(重连逻辑只认"意外断");也防 fresh 重连时旧 ws 迟到的 onclose 误杀新连接
    try { if (ws) { ws.onclose = ws.onerror = ws.onmessage = null; if (ws.readyState === 1) { ws.send(JSON.stringify({ type: 'finish' })); } ws.close(); } } catch (e) {}
    ws = null; stopPlayback();
    try { if (capNode) capNode.disconnect(); } catch (e) {}
    try { if (micStream) micStream.getTracks().forEach(function (t) { t.stop(); }); } catch (e) {}
    try { if (ac) ac.close(); } catch (e) {}
    ac = null; capNode = null; micStream = null; f32buf = new Float32Array(0); curAText = ''; curAEl = null;
    _lastU = ''; try { window.__asstVoiceMsg && window.__asstVoiceMsg('reset'); } catch (e) {}   // ㉛:挂断断轮,下次通话开新气泡
    // ㊲ 挂断=空闲期做功:后台触发历史压缩(fire-and-forget;后端幂等——轮次不够/已清空都会跳过,
    //    竞态守卫防"清空后压缩把记忆复活");下次开话回放"摘要+近几轮"而非全量原文
    if (!wasNativeRealtime) {
      try {
        fetch('/api/assistant/compact-history', { method: 'POST', headers: { 'Content-Type': 'application/json' }, keepalive: true,
          body: JSON.stringify({ file: (toggle._opts || {}).file || '' }) }).catch(function () {});
      } catch (e) {}
    }
    vt.sent = 0; vt.tail = ''; vt.pref = ''; pendingUtter = null; activeUtter = '';
    capClear();   // 挂断:字幕/等待指示一并收掉
    callBtnOn(false); callBtnSpeaking(false);
    computerBtnOn(false); computerBtnSpeaking(false);
    taPlaceholder(null);
    if (box) { box.classList.remove('on'); if (closeBox) { box.remove(); box = null; } }
    try { _refreshSpeakTg(); } catch (e) {}
    // 普通电话→电脑按钮切换时，捕获阶段已把 iOS 会话设成 play-and-record；
    // 此处若改回 playback，会让刚取得的电脑桥麦克风静音并迫使用户点第二次。
    if (!preserveComputerGesture) _audioSession('playback');
    try { _ttsShutdown(); } catch (e) {}   // 朗读通道 ws+ac 一并关(通话期建的 ac 路由粘扬声器;残留 ws 会悬空收帧)→ 下次朗读重建拿干净会话
    _aecTeardown(); _aecKill(_taec);   // AEC 环回随通话走(pc/audio 元素清干净)
  }

  // 普通电话与电脑客户端是两个独立入口。普通电话只按 rt_engine 选择豆包/GPT/Grok；
  // 电脑按钮直接进入 Windows 桥，绝不再靠 computer_client 劫持电话按钮。
  var _computerVoiceOwnedButtons = new WeakSet();
  var _computerVoiceStarting = false;
  var _nativeComputerVoiceEventsBound = false;
  function _extensionComputerVoiceDirectAvailable() {
    try {
      var runtime = window.chrome && window.chrome.runtime;
      return !!(
        runtime && typeof runtime.id === 'string' && runtime.id &&
        typeof runtime.connect === 'function' &&
        window.RC && RC.computerVoice &&
        typeof RC.computerVoice.startFromUserGesture === 'function'
      );
    } catch (e) {
      return false;
    }
  }
  function _nativeComputerVoiceAppAvailable() {
    try {
      var webViewAvailable = window.__BW_NATIVE_COMPUTER_VOICE__ === true &&
        !!(
          window.webkit &&
          window.webkit.messageHandlers &&
          window.webkit.messageHandlers.bwNativeComputerVoice &&
          typeof window.webkit.messageHandlers.bwNativeComputerVoice.postMessage === 'function'
        );
      return webViewAvailable || _extensionComputerVoiceDirectAvailable();
    } catch (e) {
      return false;
    }
  }
  function _applyNativeComputerVoiceState(value) {
    // Safari extension pages own their direct bridge state locally.  Ignore
    // stale containing-App events so they cannot repaint a live direct call
    // as "opening BWReader App" or turn its button off.
    if (_extensionComputerVoiceDirectAvailable() &&
        window.__BW_NATIVE_COMPUTER_VOICE__ !== true) return;
    var state = value && typeof value === 'object' ? value : {};
    ['asst-computer', 'vc-top-computer'].forEach(function (id) {
      _configureNativeComputerVoiceButton(document.getElementById(id));
    });
    if (state.active === true) {
      computerBtnConnecting(false);
      computerBtnOn(true);
      taPlaceholder('电脑客户端通话中…');
    } else if (state.busy === true) {
      computerBtnOn(false);
      computerBtnConnecting(true);
      taPlaceholder('正在交给 BWReader App…');
    } else {
      computerBtnConnecting(false);
      computerBtnOn(false);
      taPlaceholder(null);
    }
    if (state.title) setSt(String(state.title));
  }
  function _bindNativeComputerVoiceEvents() {
    if (_nativeComputerVoiceEventsBound) return;
    _nativeComputerVoiceEventsBound = true;
    window.addEventListener('bw-native-computer-voice-capability', function () {
      ['asst-computer', 'vc-top-computer'].forEach(function (id) {
        _configureNativeComputerVoiceButton(document.getElementById(id));
      });
    });
    window.addEventListener('bw-native-computer-voice-state', function (event) {
      _applyNativeComputerVoiceState(event && event.detail);
    });
  }
  function _configureNativeComputerVoiceButton(button) {
    if (!button) return false;
    _bindNativeComputerVoiceEvents();
    var available = _nativeComputerVoiceAppAvailable();
    button.disabled = !available;
    button.classList.toggle('native-app-required', !available);
    button.setAttribute('aria-disabled', available ? 'false' : 'true');
    if (!available) {
      button.title = '请安装或更新 BWReader App 后使用电脑客户端语音';
      button.setAttribute('aria-label', button.title);
    } else if (_extensionComputerVoiceDirectAvailable()) {
      button.title = '直接连接 Windows 电脑客户端语音';
      button.setAttribute('aria-label', button.title);
    }
    return available;
  }
  // 桥接模式(ReaderPC 仅桥接):rc-computer-voice 每次 context-mode 查询后广播,
  // 两个按钮切蓝色描边态;点击不再发起通话,提示原因(诚实,不静默失败)。
  var _bridgeOnlyServiceMode = false;
  window.addEventListener('bw-computer-voice-service-mode', function (ev) {
    var bridged = !!(ev && ev.detail && ev.detail.serviceMode === 'bridge-only');
    _bridgeOnlyServiceMode = bridged;
    ['asst-computer', 'vc-top-computer'].forEach(function (id) {
      var b = document.getElementById(id);
      if (!b) return;
      b.classList.toggle('bridge-only', bridged);
      if (bridged) {
        b.title = '桥接模式:语音在电脑本机(通话不接到 App;切回完整模式可接过来)';
        b.setAttribute('aria-label', b.title);
      }
    });
  });
  function _toggleNativeComputerVoiceApp() {
    if (!_nativeComputerVoiceAppAvailable()) return false;
    if (_bridgeOnlyServiceMode) {
      try { if (window.RC && RC.toast) RC.toast('桥接模式:语音在电脑本机运行。上下文/出卷照常;要把通话接到 App,请切回完整模式。'); } catch (_) {}
      return true;   // 已消费点击:不发 START(服务端也会拒),不留静默失败
    }
    try {
      var computerVoice = window.RC && RC.computerVoice;
      function postTarget(appKind) {
        var target = appKind === 'chatgpt-classic'
          ? 'chatgpt-classic'
          : 'codex-desktop';
        if (
          window.webkit &&
          window.webkit.messageHandlers &&
          window.webkit.messageHandlers.bwNativeComputerVoice &&
          typeof window.webkit.messageHandlers.bwNativeComputerVoice.postMessage === 'function'
        ) {
          window.webkit.messageHandlers.bwNativeComputerVoice.postMessage({
            action: 'toggle',
            appKind: target
          });
          return true;
        }
        if (!_extensionComputerVoiceDirectAvailable()) return false;
        if (_computerVoiceStarting || _computerVoiceActive()) {
          _stopComputerVoiceOnly('extension-computer-button');
          return true;
        }
        _computerVoiceStart({ appKind: target }, _gen);
        return true;
      }
      var current = computerVoice &&
        typeof computerVoice.getTargetApp === 'function'
          ? computerVoice.getTargetApp()
          : 'codex-desktop';
      // A trusted App button gesture must synchronously cross into Swift.
      // Target loading is already warmed in rc-computer-voice; never put an
      // authenticated fetch between the user gesture and postMessage.
      return postTarget(current);
    } catch (e) {
      return false;
    }
  }
  function _setComputerVoiceDialPending(value) {
    try {
      if (window.RC && RC.computerVoice &&
          typeof RC.computerVoice.setDialPending === 'function') {
        RC.computerVoice.setDialPending(value === true);
      }
    } catch (e) {}
  }
  function _cancelComputerVoiceGesture() {
    try {
      if (window.RC && RC.computerVoice &&
          typeof RC.computerVoice.cancelPreparedGesture === 'function') {
        RC.computerVoice.cancelPreparedGesture();
      }
    } catch (e) {}
  }
  function _publishComputerVoiceButton(button) {
    if (!_computerVoiceOwnedButtons.has(button)) return false;
    try {
      return !!(
        window.RC &&
        RC.computerVoice &&
        typeof RC.computerVoice.registerComputerButton === 'function' &&
        RC.computerVoice.registerComputerButton(button) === true
      );
    } catch (e) {
      return false;
    }
  }
  function _ownComputerVoiceButton(button) {
    if (!button) return false;
    _computerVoiceOwnedButtons.add(button);
    return _publishComputerVoiceButton(button);
  }
  function _computerVoiceActive() {
    try {
      return !!(
        window.RC && RC.computerVoice &&
        RC.computerVoice.isActive && RC.computerVoice.isActive()
      );
    } catch (e) { return false; }
  }
  function _stopComputerVoiceOnly(reason) {
    _gen++;
    _computerVoiceStarting = false;
    _connecting = false;
    _setComputerVoiceDialPending(false);
    _cancelComputerVoiceGesture();
    if (_computerVoiceUnsub) {
      try { _computerVoiceUnsub(); } catch (e) {}
      _computerVoiceUnsub = null;
    }
    try {
      if (_computerVoiceActive()) {
        RC.computerVoice.stop(reason || 'reader-computer-button').catch(function () {});
      }
    } catch (e) {}
    computerBtnConnecting(false);
    computerBtnOn(false);
    taPlaceholder(null);
  }

  function _computerVoiceStart(opts, generation) {
    if (!window.RC || !RC.computerVoice ||
        typeof RC.computerVoice.startFromUserGesture !== 'function') {
      computerBtnConnecting(false);
      setSt('电脑客户端组件未加载');
      try { RC.toast('电脑客户端组件未加载'); } catch (e) {}
      return;
    }
    if (_computerVoiceUnsub) {
      try { _computerVoiceUnsub(); } catch (e) {}
      _computerVoiceUnsub = null;
    }
    // 电脑桥不自动重连；断线必须由用户再次点电脑按钮，避免重复快捷键。
    _userHung = true;
    _computerVoiceStarting = true;
    _computerVoiceUnsub = RC.computerVoice.onStatus(function (status) {
      if (generation !== _gen) return;
      var state = status && status.state || '';
      setSt(status && status.message || ('电脑客户端:' + state));
      if (state === 'connected') {
        computerBtnConnecting(false);
        computerBtnOn(true);
        taPlaceholder('电脑客户端通话中…');
      } else if (state === 'failed' || state === 'stopped') {
        _computerVoiceStarting = false;
        computerBtnConnecting(false);
        computerBtnOn(false);
        taPlaceholder(null);
      }
    });
    setSt('正在确认电脑客户端…');
    computerBtnConnecting(true);
    RC.computerVoice.startFromUserGesture(opts || {}).then(function () {
      _computerVoiceStarting = false;
      _connecting = false;
      _setComputerVoiceDialPending(false);
      if (generation !== _gen) {
        RC.computerVoice.stop('stale-reader-start').catch(function () {});
      }
    }).catch(function (error) {
      _computerVoiceStarting = false;
      _connecting = false;
      _setComputerVoiceDialPending(false);
      if (generation !== _gen) return;
      _userHung = true;
      computerBtnConnecting(false);
      computerBtnOn(false);
      var startMessage = (error && error.code ===
        'BW_COMPUTER_VOICE_GESTURE_REQUIRED')
        ? '电脑客户端刚载入，请再点一次电脑按钮'
        : ((error && error.message) || '电脑客户端启动失败');
      setSt(startMessage);
      taPlaceholder(null);
      try {
        if (window.RC && RC.toast) {
          RC.toast(startMessage);
        }
      } catch (e) {}
    });
  }
  toggle._connect = function (opts) {
    if (_reviewVoiceGate(false)) return;
    // Installed App/Safari always use the App-owned OpenAI Realtime path for
    // the ordinary phone button. This check must precede the legacy `mode`
    // branch, whose default `agent` value otherwise diverts the App to Pi.
    if (window.__BW_NATIVE_LOCAL_READER__ === true ||
        window.__BW_NATIVE_OPENAI_REALTIME__ === true) {
      if (_computerVoiceStarting || _computerVoiceActive()) {
        _stopComputerVoiceOnly('ordinary-voice-start');
      }
      rtcStart(opts);
      return;
    }
    if (mode !== 's2s') {
      if (mode === 'agent' && _nativeAgentAvailable()) _nativeAgentStart(opts);
      else start(opts);
      return;
    }
    if (_computerVoiceStarting || _computerVoiceActive()) {
      _stopComputerVoiceOnly('ordinary-voice-start');
    }
    // 133:这个 fetch 以前不受世代管辖 —— 用户在它在途时挂断,迟到的 .then 照样把拨号**复活**,
    // 建出一路没人管的通话。拨号前记世代,回调里过期就直接丢弃。
    var g0 = _gen;
    _connecting = true;
    fetch('/api/assistant/voice-config').then(function (r) { return r.json(); }).then(function (d) {
      if (g0 !== _gen || _reviewVoiceGate(false)) { try { console.warn('[vc] voice-config 迟到或已进入复习模式,拨号已取消'); } catch (e) {} return; }
      var engine = (((d || {}).cfg) || {}).rt_engine;
      _connecting = false;
      if (engine === 'openai_rtc' || engine === 'openai') rtcStart(opts);
      else start(opts);   // 历史 computer_client 值按默认豆包处理，不再控制电脑桥。
    }).catch(function () {
      if (g0 !== _gen) return;
      _connecting = false;
      callBtnConnecting(false);
      if (_reviewVoiceGate(false)) return;
      setSt('读取语音模型失败，请重试');
    });
  };
  function toggle(opts) {
    injectCss();
    opts = opts || {};
    if (_reviewVoiceGate(true)) return false;
    // ㉟ 唯一侧栏原则:调用方没带 file/page(EPUB 的通话按钮直接点、无 21-misc-ai 接线)→ 经中间层
    //    RC.adapter().getContext() 补齐;EPUB 的"page"=当前 section idx+1(1-based 章号,后端按 .epub 分流取章文本)
    if (!opts.file) {
      try {
        var _c = (window.RC && RC.adapter && RC.adapter().getContext()) || {};
        opts.file = _c.file_rel || _c.file || '';
        if (!opts.page) opts.page = _c.page || (_c.current_section_idx != null ? (_c.current_section_idx + 1) : 0);
      } catch (e) {}
    }
    mode = (opts && opts.mode) || 'agent';
    if (_computerVoiceStarting || _computerVoiceActive()) {
      _stopComputerVoiceOnly('ordinary-voice-start');
    }
    if (_connecting) {
      _gen++;
      _connecting = false;
      _setComputerVoiceDialPending(false);
      _cancelComputerVoiceGesture();
      callBtnConnecting(false);
      setSt('语音通话启动已取消');
      return true;
    }
    if (_nativeAgentEngaged()) {
      _nativeAgentStop();
      setSt('正在结束原生语音通话…');
      return true;
    }
    if (ws) { teardown(false); setSt('已挂断(再点 📞 重新通话)'); return; }
    if (mode === 'agent') {   // agent 模式无浮层:状态全靠按钮特效(绿=在听/蓝=在念)+ 输入框(转写/placeholder)
      toggle._opts = opts || {};
      taPlaceholder('连接语音…');
      if (_nativeAgentAvailable()) {
        if (!_nativeAgentStart(toggle._opts)) {
          callBtnConnecting(false);
          taPlaceholder(null);
          setSt('无法联系 BWReader App 原生语音');
        }
      } else {
        start(toggle._opts);
      }
      return true;
    }
    if (!box) {
      box = document.createElement('div'); box.id = 'rc-vc';
      // Apple 风:抓手(拖=调对话区高度)+ 状态点 + SF 线条按钮(↺=新话题 / ✕=结束通话);挂断大圆钮撤掉——
      // 入口按钮 #asst-call 本身就是开关(再点=挂断),窗内 ✕ 同义
      box.innerHTML = '<div class="vc-grab" title="拖动调整对话区高度"></div>' +
        '<div class="vc-head"><span class="vc-dot"></span><span class="vc-st">连接中…</span>' +
        '<button class="vc-new" title="新话题:清空对话记忆重新开始(书页上下文保留)">' +
        '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20.5 12a8.5 8.5 0 1 1-2.6-6.1"/><polyline points="18.5 2.5 18.5 6.5 14.5 6.5"/></svg></button>' +
        '<button class="vc-x" title="结束通话">' +
        '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><line x1="6" y1="6" x2="18" y2="18"/><line x1="18" y1="6" x2="6" y2="18"/></svg></button></div>' +
        '<div class="vc-sub"></div>' +
        '<div class="vc-vids"></div>';
      // 侧栏在 → 通话条内嵌在输入框上方(跟对话流一体);侧栏组件不在 → 右下角浮层兜底
      var asstInput = document.getElementById('asst-input');
      if (asstInput && asstInput.parentNode) { box.classList.add('vc-inline'); asstInput.parentNode.insertBefore(box, asstInput); }
      else document.body.appendChild(box);
      // ㉛(用户设计):对话内容直接进侧栏 #asst-thread(与文字对话同流同清)→ 浮层迷你对话区+抓手退役,
      //   通话条只剩状态行;视频卡区保留。无侧栏页面(收藏夹独立页等)维持原样兜底。
      if (window.__asstVoiceMsg) {
        box.querySelector('.vc-sub').style.display = 'none';
        box.querySelector('.vc-grab').style.display = 'none';
      }
      box.querySelector('.vc-x').addEventListener('click', function () { teardown(true); });
      box.querySelector('.vc-new').addEventListener('click', function () {   // ↺ 新话题:挂断 → 重连(豆包带 fresh=1 清 dialog_id;WebRTC 每连接本就是新会话)
        teardown(false);
        toggle._fresh = true;
        setSt('已清空记忆,重新开始…');
        toggle._connect(toggle._opts || {});
      });
      // 抓手拖拽:上拖=对话区变高(窗在输入框上方,向上扩展),高度存 localStorage 下次直接复原
      (function () {
        var g = box.querySelector('.vc-grab'), sub = box.querySelector('.vc-sub');
        function _clampH(h) { return Math.min(Math.max(h, 56), Math.round((window.innerHeight || 800) * 0.6)); }
        try { var h0 = parseInt(localStorage.getItem('rcVcSubH') || '', 10); if (h0) sub.style.height = _clampH(h0) + 'px'; } catch (e) {}
        var drag = false, y0 = 0, hStart = 0;
        g.addEventListener('pointerdown', function (e) {
          drag = true; y0 = e.clientY; hStart = sub.getBoundingClientRect().height;
          try { g.setPointerCapture(e.pointerId); } catch (_) {}
          e.preventDefault();
        });
        g.addEventListener('pointermove', function (e) {
          if (!drag) return;
          sub.style.height = _clampH(hStart + (y0 - e.clientY)) + 'px';
        });
        ['pointerup', 'pointercancel'].forEach(function (n) {
          g.addEventListener(n, function () {
            if (!drag) return; drag = false;
            try { localStorage.setItem('rcVcSubH', String(Math.round(sub.getBoundingClientRect().height))); } catch (e) {}
          });
        });
      })();
    }
    toggle._opts = opts || {};
    try { box.querySelector('.vc-new').style.display = (mode === 's2s') ? '' : 'none'; } catch (e) {}   // 🧹 是 S2S 记忆专用;agent 模式用助手自己的「清空」
    setSt('连接中…');
    callBtnConnecting(true);   // 96:豆包/GPT-WS 路径同样点亮等待态
    toggle._connect(toggle._opts);
    return true;
  }

  // 翻页同步:仅 s2s 模式需要(agent 模式每次发送时侧栏 ctx() 自带当前页,天然跟页走)
  function setPage(page, vtext) {   // ㉟b:vtext=EPUB 动态视口文本(整章太长;同章内滚动=page 不变但窗口变,也要推)
    if (mode !== 's2s') return;
    if (!ws || ws.readyState !== 1 || !page) return;
    var o = toggle._opts || {};
    var tfp = vtext ? (vtext.length + ':' + vtext.slice(0, 40) + vtext.slice(-40)) : '';
    // 123(用户实测"翻到第5页读的是第3页"根因):去重键改独立 _syncedPage——旧版用 o.page,重连(relay 重启)后
    // 新会话是"初始页脑子",而 o.page 残留新页码=去重永远拦住,page 消息再也不发。_syncedPage 在连接成功处清零。
    if (page === o._syncedPage && tfp === (o._vtFp || '')) return;
    o.page = page; o._syncedPage = page; o._vtFp = tfp;
    _inkFp = '';   // 换页后墨迹指纹作废(新页的墨迹要重新同步)
    var _tot = 0;
    try { _tot = (o.total || (window.RC && RC.adapter && RC.adapter() && RC.adapter().totalPages && RC.adapter().totalPages()) ||
                  (typeof pdfDoc !== 'undefined' && pdfDoc && pdfDoc.numPages) || 0) | 0; } catch (e) {}
    try { ws.send(JSON.stringify({ type: 'page', page: page, total: _tot || undefined,
                                   text: vtext ? String(vtext).slice(0, 2000) : undefined })); setSt('通话中 · 已同步到第 ' + page + ' 页'); } catch (e) {}
  }

  // 选中/chip 状态同步(与侧栏 __voiceContext 同源):选中文字/钉住焦点/带入图 → relay 热更 SP。
  // 指纹去重:变化(含变空=取消选中/点掉 chip)才发;relay 端再比对,真变了才 UpdateConfig。
  var _stateFp = null;
  function syncState(state) {
    // #54(用户实锤"语音模式选中 AI 不知道"):去掉 mode!=='s2s' 限制——RTC 路(openai_rtc)全局 ws 是 shim,
    //   ws.send 会镜像给控制 WS(1856)让 relay 拿到选中(page/ink 一直这么上报所以正常);只 s2s 发=RTC 路选中
    //   从不上报 → relay book.sel 空 → 开口时注入空 → AI 不知道选中。shim readyState 恒 1,下面 ws.send 照发。
    if (!ws || ws.readyState !== 1) return;
    state = state || {};
    var fp; try { fp = JSON.stringify(state); } catch (e) { fp = String(state.sel || '') + '|' + (state.figs || 0); }
    if (fp === _stateFp) return;
    var first = (_stateFp === null);
    _stateFp = fp;
    if (first && !(state.sel || state.focus || state.figs)) return;   // 首轮全空:不发(SP 本来就没有)
    try { ws.send(JSON.stringify({ type: 'state', sel: state.sel || '', focus: state.focus || '', figs: state.figs || 0 })); } catch (e) {}
  }

  // 侧栏「🗑 清空」→ 询问是否顺带把「回顾学习」记忆起点设为现在(v3-⑰c;对话记忆与学习档案分层:
  // 清空只清对话,时间线档案默认保留——想斩断的由这里显式确认)
  document.addEventListener('click', function (e) {
    try {
      var b = e.target && e.target.closest ? e.target.closest('[data-q="clear"]') : null;
      if (!b) return;
      // 复习助手有独立历史/摘要/清空域；清复习对话不能顺手移动普通
      // 助手的长期学习记忆起点。
      if (RC.assistant && RC.assistant.getMode &&
          RC.assistant.getMode() === 'review') return;
      setTimeout(function () {   // 等 clear 本体先跑完,再问(不阻塞清空)
        if (confirm('把「回顾学习」的记忆起点也设为现在吗?\n(之前的学习记录将不再被回顾提起;学习档案本身保留)')) {
          _setCutoff(Math.floor(Date.now() / 1000), function (ok) { if (typeof _toast === 'function') _toast(ok ? '记忆起点=现在' : '设置失败'); });
        }
      }, 50);
    } catch (_) {}
  }, true);
  // 侧栏「🗑 清空」→ S2S 记忆同步清空(fresh 重连;共享/native 两版按钮都是 data-q="clear",捕获阶段旁听不拦截)
  document.addEventListener('click', function (e) {
    try {
      var b = e.target && e.target.closest ? e.target.closest('[data-q="clear"]') : null;
      if (!b || mode !== 's2s' || !ws) return;
      if (RC.assistant && RC.assistant.getMode &&
          RC.assistant.getMode() === 'review') return;
      teardown(false);
      toggle._fresh = true;
      taPlaceholder('对话已清空,语音记忆重置中…');
      toggle._connect(toggle._opts || {});   // ㉛:按引擎分流(rtc=重连即新会话;豆包=fresh 清 dialog_id)——显示/服务端记录由侧栏 clear 本体清
    } catch (_) {}
  }, true);

  // 通话中圈画同步:21-misc-ai 轮询把当前页**内存实时墨迹**推过来(不等 sidecar 防抖保存,对齐侧栏 ctx["ink"] 机制),
  // 变了才发(指纹去重)→ relay 提取圈下文字 + 热更新 SP → 豆包知道"本页有你的标注"。
  var _inkFp = '';
  function syncInk(page, strokes) {
    if (mode !== 's2s' || !ws || ws.readyState !== 1 || !page) return;
    strokes = strokes || [];
    var fp = _rtcInkFingerprint(page, strokes);
    var pageState = _rtcInkPageState(page, true);
    var signalAtSend = pageState.signal || 0;
    var changed = signalAtSend > (pageState.signalSent || 0);
    if (fp === _inkFp) {
      // 事件发生但几何没变（例如无效短线）只结束 pending，不制造新版本。
      _rtcResolveInkPending(page);
      pageState.signalSent = Math.max(pageState.signalSent || 0, signalAtSend);
      return;
    }
    var first = (_inkFp === '');
    _inkFp = fp;
    if (first && !strokes.length && !changed) return;   // 首次空态只记指纹(没圈过东西不必更新 SP)
    var sendStrokes = strokes.slice(0, 60);
    try { sendStrokes = JSON.parse(JSON.stringify(sendStrokes)); } catch (e) {}
    function sendCurrent(shot) {
      // EPUB 截图异步返回时，若已有更新的笔迹指纹，旧快照绝不能反向覆盖新状态。
      if (fp !== _inkFp) return false;
      try {
        var payload = {
          type: 'ink', page: page, strokes: sendStrokes,
          revision: fp, changed: changed
        };
        if (shot && shot.b64) payload.shot = { media_type: shot.media_type, b64: shot.b64 };
        ws.send(JSON.stringify(payload));
        pageState.signalSent = Math.max(pageState.signalSent || 0, signalAtSend);
        setSt('通话中 · 已同步你的圈画');
        return true;
      } catch (e) { return false; }
    }
    // App 原生直连的视觉工具会在被调用时直接从当前页面取合成图；状态同步无需等待
    // EPUB 的 JS 截图，这样落笔后立即开口也能先锁定 see_ink。
    if (_rtc.nativeDirect) { sendCurrent(null); return; }
    // EPUB/HTML(reflow):后端拿到的是归一化 strokes,没有章节宽高无法无失真渲染合成图 → 由前端(唯一知道布局的中间层)
    // 产出笔迹合成图(视口截图)随 ink 消息发给 relay,存 book.view_shot 供 WS 引擎(豆包/Grok 不能直接看图,
    // 靠 see_ink 让视觉模型描述那张合成图)。PDF 走服务端裁图不需要;空笔迹不带 shot(relay view_shot=None 自动清陈旧)。
    try {
      var _isPdf = !!(window.RC && RC.adapter && RC.adapter().config && RC.adapter().config.isPDF);
      if (!_isPdf && strokes.length && window.RC && (RC.captureInkRegion || RC.captureView)) {
        var _shotP = RC.captureInkRegion
          ? RC.captureInkRegion().then(function (shot0) { return shot0 || (RC.captureView ? RC.captureView() : null); })
          : RC.captureView();
        _shotP.then(function (shot) { sendCurrent(shot); })
          .catch(function () { sendCurrent(null); });
        return;
      }
    } catch (e) {}
    sendCurrent(null);
  }

  // ── 入口按钮：电脑客户端占原麦克风位置；普通电话保留在它右侧。──
  function injectBtn() {
    var input = document.getElementById('asst-input');
    if (!input) return false;
    var mic = document.getElementById('asst-mic');
    if (mic) {
      mic.style.display = 'none';
      mic.setAttribute('aria-hidden', 'true');
      mic.tabIndex = -1;
    }
    var existingComputer = document.getElementById('asst-computer');
    var existingCall = document.getElementById('asst-call');
    if (existingComputer && existingCall) {
      _publishComputerVoiceButton(existingComputer);
      _configureNativeComputerVoiceButton(existingComputer);
      return true;
    }
    injectCss();
    var c = document.createElement('button');
    c.id = 'asst-computer'; c.type = 'button';
    c.title = '电脑客户端桥接：点=连接 Windows 上的 Codex/ChatGPT，再点=停止';
    c.setAttribute('aria-label', '电脑客户端桥接');
    c.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="3.5" y="4.5" width="17" height="12" rx="2"/><path d="M8 20h8M12 16.5V20"/></svg>';
    var b = document.createElement('button');
    b.id = 'asst-call'; b.type = 'button';
    b.title = '普通实时语音通话：点=开始，再点=挂断';
    b.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M5 5.3C5 14.8 9.2 19 18.7 19c.72 0 1.3-.58 1.3-1.3v-2.35c0-.56-.36-1.06-.9-1.23l-2.62-.87a1.3 1.3 0 0 0-1.33.32l-.95.95a11.6 11.6 0 0 1-4.72-4.72l.95-.95c.35-.35.47-.87.32-1.33L9.85 4.9A1.3 1.3 0 0 0 8.62 4H6.3C5.58 4 5 4.58 5 5.3z"/></svg>';
    if (mic && mic.parentNode === input) {
      input.insertBefore(c, mic);
      input.insertBefore(b, mic);
    } else {
      input.insertBefore(b, input.firstChild);
      input.insertBefore(c, b);
    }
    _ownComputerVoiceButton(c);
    _configureNativeComputerVoiceButton(c);
    c.addEventListener('click', function () {
      if (_reviewVoiceGate(true)) return;
      if (!_nativeComputerVoiceAppAvailable()) {
        setSt('请安装或更新 BWReader App 后使用电脑客户端语音');
        try { RC.toast('请安装或更新 BWReader App 后使用电脑客户端语音'); } catch (e) {}
        return;
      }
      if (ws || _rtc.on || _connecting || _reconnT || _reconnPend) {
        teardown(false, true);
      }
      try { navigator.vibrate && navigator.vibrate(10); } catch (e) {}
      if (!_toggleNativeComputerVoiceApp()) {
        computerBtnConnecting(false);
        setSt('无法联系 BWReader App 原生语音');
        try { RC.toast('无法联系 BWReader App 原生语音'); } catch (e) {}
      }
    });
    b.addEventListener('click', function () {
      if (_reviewVoiceGate(true)) return;
      if (ws || _reconnT || _reconnPend) {   // 通话中/重连排队中 → 挂断(开关 off)
        teardown(true);
        taPlaceholder(null);
        return;
      }
      try { navigator.vibrate && navigator.vibrate(10); } catch (e) {}
      if (window._voiceCallS2S) window._voiceCallS2S(); else toggle({ mode: 's2s' });
    });
    // 工具进行中按钮(v3-⑯b):调用开始出现转圈,点击=中止,结束自动消失
    var tb = document.createElement('button');
    tb.id = 'vc-tool-btn'; tb.type = 'button'; tb.title = '正在执行(点击中止)';
    input.insertBefore(tb, b.nextSibling);
    tb.addEventListener('click', function () {
      if (tb.className !== 'running') return;
      tb.innerHTML = '<span class="vc-spin" style="opacity:.4"></span>'; tb.title = '正在中止…';
      try { if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'tool_abort' })); } catch (e) {}
    });
    _syncReviewVoiceUi();
    return true;
  }
  // #asst-mic 长按 600ms = 豆包 ASR 连续听(agent 模式:说话→转写→自动问助手,文字答;朗读亮则也念)。
  // 单击保留原功能(系统听写)——长按触发后在**捕获阶段**吞掉随后的 click,不碰 rc-assistant 的原 handler。
  function _lpPop(btn) {   // 长按到点的视觉确认(用户设计:不然不知道按够没):弹一下+变紫,松手前就能看到
    try {
      btn.classList.remove('vc-lp-pop'); void btn.offsetWidth;   // 重触发动画
      btn.classList.add('vc-lp-pop');
      setTimeout(function () { btn.classList.remove('vc-lp-pop'); }, 450);
      navigator.vibrate && navigator.vibrate(15);
    } catch (e) {}
  }
  function _micLongAction() {   // mic 长按到点的动作(侧栏 #asst-mic 与顶栏镜像按钮共用)
    if (_reviewVoiceGate(true)) return;
    if (ws && mode === 'agent') { teardown(false); taPlaceholder(null); return; }   // 再长按=挂断 ASR
    if (ws) teardown(false);   // S2S 开着 → 先挂再开 ASR
    if (window._voiceCall) window._voiceCall(); else toggle({});
  }
  function _bindLongPress(btn, onLong) {   // 长按 600ms:到点即 pop 特效+执行;随后的 click 在捕获阶段吞掉
    if (btn.__vcLp) return;
    btn.__vcLp = true;
    var _t = null, _fired = false;
    btn.addEventListener('pointerdown', function () {
      _fired = false;
      _t = setTimeout(function () { _t = null; _fired = true; _lpPop(btn); onLong(); }, 600);
    });
    ['pointerup', 'pointercancel', 'pointerleave'].forEach(function (n) {
      btn.addEventListener(n, function () { if (_t) { clearTimeout(_t); _t = null; } });
    });
    btn.addEventListener('contextmenu', function (e) { e.preventDefault(); });   // iOS 长按不弹菜单
    btn.addEventListener('click', function (e) {
      if (_fired) { _fired = false; e.stopImmediatePropagation(); e.preventDefault(); }
    }, true);
  }
  function injectMicLongPress() {
    var m = document.getElementById('asst-mic');
    if (!m) return false;
    if (m.__vcLp) return true;
    _bindLongPress(m, _micLongAction);
    return true;
  }
  // ── 顶栏语音按钮：侧栏收起时显示电脑客户端 + 普通电话。──
  //    状态镜像:观察侧栏按钮 class(on/asr/speaking)同步变色呼吸;侧栏打开时这俩隐藏(那边有同款)。──
  function injectTopbarBtns() {
    if (document.getElementById('vc-top-computer')) {
      _publishComputerVoiceButton(document.getElementById('vc-top-computer'));
      _configureNativeComputerVoiceButton(document.getElementById('vc-top-computer'));
      return true;
    }
    var anchor = document.getElementById('fs-toggle');
    var srcComputer = document.getElementById('asst-computer');
    var srcCall = document.getElementById('asst-call');
    if (!anchor || !anchor.parentNode || !srcComputer || !srcCall) return false;
    injectCss();
    var tm = document.createElement('button');
    tm.id = 'vc-top-computer'; tm.type = 'button';
    tm.title = '电脑客户端桥接：点=连接，再点=停止';
    tm.setAttribute('aria-label', '电脑客户端桥接');
    tm.innerHTML = srcComputer.innerHTML;
    var tc = document.createElement('button');
    tc.id = 'vc-top-call'; tc.type = 'button';
    tc.title = '普通实时语音通话：点=开始，再点=挂断';
    tc.innerHTML = srcCall.innerHTML;   // 复用侧栏电话的 SF 线条 SVG
    anchor.parentNode.insertBefore(tm, anchor);
    anchor.parentNode.insertBefore(tc, anchor);
    _ownComputerVoiceButton(tm);
    _configureNativeComputerVoiceButton(tm);
    tm.addEventListener('click', function () { try { srcComputer.click(); } catch (e) {} });
    tc.addEventListener('click', function () { try { srcCall.click(); } catch (e) {} });
    function _mirror(src, dst, cls) {   // 状态镜像:侧栏按钮的状态类 → 顶栏同名类
      function sync() {
        cls.forEach(function (c) { dst.classList.toggle(c, src.classList.contains(c)); });
      }
      new MutationObserver(sync).observe(src, { attributes: true, attributeFilter: ['class'] });
      sync();   // 顶栏可能晚于连接建立；创建时先复制当前状态，不能只等下一次变化。
    }
    _mirror(srcComputer, tm, ['on', 'speaking', 'connecting']);
    _mirror(srcCall, tc, ['on', 'speaking', 'connecting']);
    _syncReviewVoiceUi();
    function _vis() {   // 侧栏开=隐藏(功能在侧栏里);收起才显示
      var open = false;
      try { open = !!(window.RC && RC.sidedrawer && RC.sidedrawer.isOpen()); } catch (e) {}
      tm.style.display = open ? 'none' : '';
      tc.style.display = open ? 'none' : '';
    }
    _vis();
    (function _hookSide(n) {   // 侧栏根元素可能晚于本注入出现 → 挂上为止
      var side = document.getElementById('ep-side');
      if (side) { new MutationObserver(_vis).observe(side, { attributes: true, attributeFilter: ['class'] }); _vis(); return; }
      if (n < 20) setTimeout(function () { _hookSide(n + 1); }, 800);
    })(0);
    return true;
  }
  // 「🔊 朗读」开关=统一的"要不要出声":S2S 通话中控制豆包音频播放(独立键,默认亮);
  // 其余场景(ASR 通话/纯打字)控制回答的 T2S 朗读(旧键,默认灭——读比听快)。语境切换时按钮亮灭自动刷新。
  // 挤进侧栏快捷栏(蹭 rc-media-tg 样式,与「书页/配图/视频」同排)。通话中点击即时生效。
  function _tgOn() { return (ws && mode === 's2s') ? s2sSpeakOn() : speakOn(); }
  function _refreshSpeakTg() {
    var b = document.querySelector('.vc-speak-tg'); if (!b) return;
    if (ws && mode === 's2s') {   // 61 通话中:四态标签(有音频输出的档视觉点亮)
      var m = _voiceMode();
      b.innerHTML = _VM_LABEL[m] || _VM_LABEL.sts;
      b.classList[(m !== 'stt') ? 'add' : 'remove']('on');
    } else {
      b.innerHTML = _VMI.spk + '<span>朗读</span>';
      b.classList[_tgOn() ? 'add' : 'remove']('on');
    }
  }
  var _recallRefresh = null;   // chip 文字刷新(清空联动后调)
  function _fmtCutoff(ts) {
    if (!ts) return '⏱ 记忆起点';
    var d = new Date(ts * 1000);
    return '⏱ ' + (d.getMonth() + 1) + '/' + d.getDate() + ' ' + String(d.getHours()).padStart(2, '0') + '时起';
  }
  function _setCutoff(ts, cb) {
    fetch('/api/assistant/voice-config', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ recall_cutoff: ts || '' }) })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (d && d.ok) { if (_recallRefresh) _recallRefresh(); if (cb) cb(true); } else if (cb) cb(false); })
      .catch(function () { if (cb) cb(false); });
  }
  function injectRecallChip() {
    var qb = document.getElementById('asst-quick');
    if (!qb) return false;
    if (qb.querySelector('.vc-recall-tg')) return true;
    var b = document.createElement('button'); b.type = 'button'; b.className = 'rc-media-tg vc-recall-tg';
    b.innerHTML = '<span>⏱ 记忆起点</span>';
    b.title = '「回顾学习」只统计此时间之后的学习记录(不设=全部)。点开选日期和小时,或一键设为现在';
    _recallRefresh = function () {
      fetch('/api/assistant/voice-config').then(function (r) { return r.json(); }).then(function (v) {
        if (v && v.ok) b.querySelector('span').textContent = _fmtCutoff(v.cfg && v.cfg.recall_cutoff);
      }).catch(function () {});
    };
    _recallRefresh();
    var pane = null;
    b.addEventListener('click', function () {
      if (pane) { pane.remove(); pane = null; return; }
      pane = document.createElement('div'); pane.className = 'vc-recall-pane';
      pane.innerHTML = '<input type="datetime-local" step="3600">' +
        '<button type="button" data-a="now">设为现在</button><button type="button" data-a="all">不限</button>';
      qb.parentNode.insertBefore(pane, qb.nextSibling);
      function done(ok) { if (typeof _toast === 'function') _toast(ok ? '已设置' : '设置失败'); if (pane) { pane.remove(); pane = null; } }
      pane.querySelector('input').addEventListener('change', function () {
        var t = Date.parse(this.value);
        if (t) _setCutoff(Math.floor(t / 1000), done);
      });
      pane.querySelector('[data-a="now"]').addEventListener('click', function () { _setCutoff(Math.floor(Date.now() / 1000), done); });
      pane.querySelector('[data-a="all"]').addEventListener('click', function () { _setCutoff(0, done); });
    });
    qb.appendChild(b);
    // 服务器为真相源:初始同步 rt_voice_mode/rt_tts_speak + 一次性迁移旧档值(61:tts 档→stt+TTS开)
    try {
      fetch('/api/assistant/voice-config').then(function (r) { return r.json(); }).then(function (d) {
        var c = (d && d.cfg) || {};
        var mv = c.rt_voice_mode || '';
        if (mv === 'tts') {   // 旧「TTS代念」档=新 stt+TTS 开(写回服务器,幂等)
          try { localStorage.setItem('rc-voice-mode-s2s', 'stt'); localStorage.setItem('rc-voice-tts', '1'); } catch (e) {}
          fetch('/api/assistant/voice-config', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ rt_voice_mode: 'stt', rt_tts_speak: '1' }) }).catch(function () {});
        } else if (mv) {
          var nv = _VM_OLD[mv] || mv;
          try { localStorage.setItem('rc-voice-mode-s2s', nv); } catch (e) {}
          if (nv !== mv) fetch('/api/assistant/voice-config', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ rt_voice_mode: nv }) }).catch(function () {});
        }
        if (c.rt_tts_speak != null && mv !== 'tts') { try { localStorage.setItem('rc-voice-tts', c.rt_tts_speak ? '1' : '0'); } catch (e) {} }
        var tb0 = document.querySelector('.vc-tts-tg'); if (tb0) tb0.classList[_ttsOn() ? 'add' : 'remove']('on');
        _refreshSpeakTg();
      }).catch(function () {});
    } catch (e) {}
    return true;
  }
  function injectSpeakToggle() {
    var qb = document.getElementById('asst-quick');
    if (!qb) return false;
    if (qb.querySelector('.vc-speak-tg')) return true;
    var b = document.createElement('button'); b.type = 'button'; b.className = 'rc-media-tg vc-speak-tg';
    b.innerHTML = _VMI.spk + '<span>朗读</span>';
    b.title = '朗读:点亮=AI 出声(语音通话中=播豆包语音;其余=回答用 TTS 流式念出来);按灭=只出文字。语音通话按灭时豆包音频仍生成计费(协议限制),真文本对话用麦克风长按的 ASR 模式';
    if (_tgOn()) b.classList.add('on');
    b.addEventListener('click', function () {
      if (ws && mode === 's2s') {   // 61 四态循环 语音→文字→混合→路由→语音(TTS 已独立成旁边的开关)
        var cur = _voiceMode();
        var nxt = _VM_SEQ[(_VM_SEQ.indexOf(cur) + 1) % _VM_SEQ.length];
        try { localStorage.setItem('rc-voice-mode-s2s', nxt); } catch (e) {}
        try { fetch('/api/assistant/voice-config', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ rt_voice_mode: nxt }) }).catch(function () {}); } catch (e) {}   // 持久化到服务器(跨设备;relay 门控/预算即时跟随=热切生效)
        // 127:自动挡下用户轮的模态由**会话级** output_modalities 决定 → 热切要立刻 session.update
        try { if (_rtc.on) _dcSend({ type: 'session.update',
          session: { type: 'realtime', output_modalities: [nxt === 'stt' ? 'text' : 'audio'] } }); } catch (e) {}
        try { _ttsIrqSync(); } catch (e) {}   // 131:切档 → 自动进/退「可打断代念」模式
        if (nxt === 'stt') stopPlayback();
        try { setSt('通话中 · ' + (_VM_TXT[nxt] || nxt)); } catch (e) {}
      } else {                      // 其余:切回答的 T2S 朗读
        var on2 = !speakOn();
        try { localStorage.setItem('rc-voice-speak', on2 ? '1' : '0'); } catch (e) {}
        if (on2) _ttsEnsure();      // 手势内预热(iOS AudioContext 必须手势启动)
        else { bargeIn(); _ttsShutdown(); }
      }
      _refreshSpeakTg();
    });
    qb.appendChild(b);
    // 61 TTS 通用开关(用户设计:与模式按钮相邻):任何模式的文字输出都用豆包朗读流式代念
    var tb = document.createElement('button'); tb.type = 'button'; tb.className = 'rc-media-tg vc-tts-tg';
    tb.innerHTML = _VMI.spk + '<span>代念</span>';
    tb.title = 'TTS 代念(通用开关):文字/路由/混合模式下的文字输出,句子生成到哪念到哪(豆包朗读通道);关=只显示文字。豆包引擎通话本身出声,不受此开关影响';
    if (_ttsOn()) tb.classList.add('on');
    tb.addEventListener('click', function () {
      var on = !_ttsOn();
      try { localStorage.setItem('rc-voice-tts', on ? '1' : '0'); } catch (e) {}
      tb.classList[on ? 'add' : 'remove']('on');
      if (on) { try { _ttsEnsure(); } catch (e) {} }              // 手势内预热(iOS AudioContext 必须手势启动)
      else { try { bargeIn(); _ttsShutdown(); } catch (e) {} }    // 关=停残播+撂通道省资源
      try { _ttsIrqSync(); } catch (e) {}                         // 131:代念开/关 → 进/退「可打断代念」模式
      try { fetch('/api/assistant/voice-config', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rt_tts_speak: on ? '1' : '' }) }).catch(function () {}); } catch (e) {}
    });
    qb.appendChild(tb);
    if (!document.getElementById('vc-quick-compact')) {   // 61:快捷栏整排紧凑(用户要求)
      var st0 = document.createElement('style'); st0.id = 'vc-quick-compact';
      st0.textContent = '#asst-quick .rc-media-tg,#ep-asst-quick .rc-media-tg{padding:4px 7px;font-size:12px;gap:3px;border-radius:7px}' +
        '#asst-quick .rc-media-tg svg,#ep-asst-quick .rc-media-tg svg{width:14px;height:14px;flex:none}' +
        '#vc-cap .vc-cap-line.vc-cap-route::before{content:"";position:absolute;left:0;top:7px;bottom:7px;width:2.5px;border-radius:2px;background:#9d7bff}' +
        '#vc-cap .vc-cap-line.vc-cap-route{color:#e9e2ff}';
      document.head.appendChild(st0);
    }
    return true;
  }

  // 侧栏 pane 注入时机不定(rc-assistant 加载在前,但保守起见轮询到出现为止)
  if (!(injectBtn() && injectSpeakToggle() && injectMicLongPress() && injectRecallChip() && injectTopbarBtns())) {
    var _tries = 0, _t = setInterval(function () { if ((injectBtn() && injectSpeakToggle() && injectMicLongPress() && injectRecallChip() && injectTopbarBtns()) || ++_tries > 40) clearInterval(_t); }, 750);
  }
  try {
    window.addEventListener('rc:assistant-mode-changed', function () {
      var blocked = _syncReviewVoiceUi();
      var computerOn = false;
      try { computerOn = !!(RC.computerVoice && RC.computerVoice.isActive && RC.computerVoice.isActive()); } catch (e) {}
      if (blocked && (ws || computerOn || _rtc.on || _connecting || _reconnT || _reconnPend)) teardown(true);
    });
  } catch (e) {}
  _syncReviewVoiceUi();

  // ㉟ 共享位置/选中/笔迹同步:宿主没提供 __vcSyncNow(PDF reader 的 21-misc-ai 有自己的
  // 2s 轮询)时,共享层经 adapter 同一接口同步。WebAdapter 只给状态，不另建语音逻辑。
  function _syncAdapterNow() {
    if (!ws || ws.readyState !== 1) return;
    try {
      var adapter = window.RC && RC.adapter ? RC.adapter() : null;
      var c = (adapter && adapter.getContext ? adapter.getContext() : null) || {};
      var pg = c.page || (c.current_section_idx != null ? (c.current_section_idx + 1) : 0);
      // ㊵ 拉模式下 setPage 经 shim 只更新本地状态(零网络/token 成本),恒推保持 pendText 最新即可;
      // 豆包(真 WS,SP 前缀架构)只在翻页时推、不带视口流(滚动流会打它的 dialog 缓存)
      if (pg) setPage(pg, _rtc.on ? String(c.visible_text || '').slice(0, 2000) : undefined);
      syncState({ sel: String(c.selection || '').slice(0, 500), focus: '', figs: 0 });
      var inkState = null;
      if (pg && Object.prototype.hasOwnProperty.call(c, 'ink')) {
        inkState = { page: pg, strokes: c.ink || [] };
      } else if (adapter && typeof adapter.getVoiceInk === 'function') {
        inkState = adapter.getVoiceInk();
      }
      if (inkState && inkState.page) syncInk(inkState.page, inkState.strokes || []);
    } catch (e) {}
  }
  function _requestSyncNow() {
    try {
      if (window.__vcSyncNow) window.__vcSyncNow();
      else _syncAdapterNow();
    } catch (e) {}
  }
  function _rtcPagesFromInkEvent(event) {
    var detail = (event && event.detail) || {};
    var raw = [];
    if (Array.isArray(detail.pages)) raw = raw.concat(detail.pages);
    if (detail.page != null) raw.push(detail.page);
    (Array.isArray(detail.changes) ? detail.changes : []).forEach(function (change) {
      if (change && change.page != null) raw.push(change.page);
    });
    (Array.isArray(detail.surfaceIds) ? detail.surfaceIds : []).forEach(function (id) {
      var match = /^page:(\d+)$/.exec(String(id || ''));
      if (match) { raw.push(parseInt(match[1], 10)); return; }
      match = /^section:(\d+)$/.exec(String(id || ''));
      if (match) raw.push(parseInt(match[1], 10) + 1);
    });
    if (!raw.length && _rtc.ctxPage) raw.push(_rtc.ctxPage);
    var seen = Object.create(null), pages = [];
    raw.forEach(function (value) {
      var page = Number(value);
      if (!(page > 0) || seen[String(page)]) return;
      seen[String(page)] = true;
      pages.push(page);
    });
    return pages;
  }
  function _onInkPending(event) {
    if (mode !== 's2s' || !(_rtc.on || _connecting || ws)) return;
    var opId = event && event.detail && event.detail.opId;
    var pages = _rtcPagesFromInkEvent(event);
    pages.forEach(function (page) {
      _rtcSetInkPending(page, opId);
    });
    if (pages.length) _rtc.activeInkPage = pages[pages.length - 1];
  }
  function _onInkChange(event) {
    if (mode === 's2s' && (_rtc.on || _connecting || ws)) {
      var source = event && event.detail && event.detail.source;
      var opId = event && event.detail && event.detail.opId;
      var changes = (event && event.detail && Array.isArray(event.detail.changes))
        ? event.detail.changes : [];
      var changesByPage = Object.create(null);
      changes.forEach(function (change) {
        var page = Number(change && change.page);
        if (page > 0) changesByPage[String(page)] = change;
      });
      var pages = _rtcPagesFromInkEvent(event);
      pages.forEach(function (page) {
        var state = _rtcInkPageState(page, true);
        // 每个完成事件才代表一个已经进入页面权威层的变化。pendingCount 让连续
        // 多笔必须全部提交后才放行 see_ink；首笔完成不能提前截掉仍在队列里的后续笔画。
        if (source === 'native-pencil') state = _rtcDecreaseInkPending(page, opId) || state;
        state.signal = (state.signal || 0) + 1;
        if (Number(page) === Number(_rtc.ctxPage)) _rtcUseInkPage(page);
        var change = changesByPage[String(page)];
        if (change) syncInk(page, change.strokes || []);
      });
      if (pages.length) _rtc.activeInkPage = pages[pages.length - 1];
    }
    _requestSyncNow();
  }
  function _onInkCancel(event) {
    if (mode !== 's2s' || !(_rtc.on || _connecting || ws)) return;
    var opId = event && event.detail && event.detail.opId;
    var pages = _rtcPagesFromInkEvent(event);
    pages.forEach(function (page) {
      _rtcCancelInkPending(page, opId);
    });
    if (_rtc.activeInkPage && !_rtcHasFreshInk(_rtc.activeInkPage)) _rtc.activeInkPage = null;
  }
  try {
    window.addEventListener('rc:inkpending', _onInkPending);
    window.addEventListener('rc:inkchange', _onInkChange);
    window.addEventListener('rc:inkcancel', _onInkCancel);
  } catch (e) {}
  setInterval(function () {
    if (window.__vcSyncNow || !ws || ws.readyState !== 1) return;
    _syncAdapterNow();
  }, 2000);

  RC.voicecall = { toggle: toggle,
    acceptRealtimeOutput: _acceptReaderRealtimeOutput,
    canCaptureComputerVoiceGesture: function () { return !_assistantInReview(); },
    isOpen: function () {
    try { return !!ws || _nativeAgentEngaged() || !!(RC.computerVoice && RC.computerVoice.isActive && RC.computerVoice.isActive()); }
    catch (e) { return !!ws; }
  }, setPage: setPage, syncInk: syncInk, syncState: syncState,
    // 设置面板改了语音配置 → 通知 relay 热更(S2S 通话中才有意义;relay 指纹含 tts,变了才真发 UpdateConfig)
    pushCfg: function () { try { if (ws && ws.readyState === 1 && mode === 's2s') ws.send(JSON.stringify({ type: 'cfg' })); } catch (e) {} } };
  // ── 统一注入端口接线(references/voice-context-injection.md):通道 bind + kind 注册 ──
  //   铁律:今后加任何注入只调 RC.voiceCtx.state/event,严禁直连 _rtcSys / relay ws。
  try {
    if (RC.voiceCtx) {
      RC.voiceCtx.bindTransport('rtc', {
        isOpen: function () { return !!(_rtc.on && _rtc.dc && _rtc.dc.readyState === 'open'); },
        send: function (t) { if (!(_rtc.on && _rtc.dc && _rtc.dc.readyState === 'open')) return false; _rtcSys(t); return true; }
      });
      RC.voiceCtx.bindTransport('relay', {   // 豆包 S2S:经上行 {type:'note'},relay 存 book 下次开口注入
        isOpen: function () { try { return !!(ws && !ws.__shim && ws.readyState === 1 && mode === 's2s' && !_rtc.on); } catch (e) { return false; } },   // __shim 排除:rtc 模式全局 ws 被 shim 顶替(readyState 恒1),不排除会把通告吞进假通道
        send: function (t) { try { ws.send(JSON.stringify({ type: 'note', text: t })); return true; } catch (e) { return false; } }
      });
      RC.voiceCtx.register('pins', { cls: 'state', budget: 2500,
        // Registry 的稳定序列化包含 id/父子关系/正文；同一有效集合字节不变，内容真变才失效。
        fp: function (p) { return p.serialized || (p.labels || []).join('¦'); },
        text: function (p) {
          var ks = p.labels || [];
          if (!ks.length) return '参考内容更新:用户已移除全部带入内容,之前的带入声明作废';
          return '参考内容更新(以本条为准,之前的带入/移除声明全部作废):用户当前带入 ' + ks.length + ' 项——' +
                 ks.map(function (k) { return '「' + k + '」:' + (p.map[k] || ''); }).join(';');
        } });
      RC.voiceCtx.register('removed_imgs', { cls: 'event', budget: 600,
        text: function (items) {
          var list = (items || []).map(function (it) { return '「' + (it.title || '图') + '」' + (it.aid ? '(编号 ' + it.aid + ')' : ''); }).join('、');
          return list ? ('用户点✕移除了配图卡里的:' + list + ' ——他不想要这些;他说「找错了/重新找」指的就是它们,换关键词重搜,别再展示这些编号') : '';
        } });
    }
  } catch (e) {}
})();
