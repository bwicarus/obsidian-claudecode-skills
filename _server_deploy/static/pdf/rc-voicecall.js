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
  var vt = { sent: 0, tail: '', sid: 0, pref: '' };   // 语音 tap 状态:已消费长度 / 未成句尾巴 / 句序号 / 上次 full(前缀判定轮次替换)
  var pendingUtter = null;                   // 助手忙时到达的新话(覆盖式排队,回答完自动发)

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
      // 朗读字幕(v3-⑳):底部居中悬浮,Apple TV 字幕风(深毛玻璃胶囊);pointer-events:none 全链穿透
      '#vc-cap{position:fixed;left:50%;transform:translateX(-50%) translateY(10px);bottom:calc(76px + env(safe-area-inset-bottom,0px));' +
      'z-index:2147481500;display:flex;flex-direction:column;align-items:center;gap:6px;pointer-events:none;' +
      'max-width:min(88vw,620px);opacity:0;transition:opacity .35s ease,transform .35s ease;font-family:-apple-system,system-ui,sans-serif}' +
      '#vc-cap.on{opacity:1;transform:translateX(-50%) translateY(0)}' +
      '#vc-cap .vc-cap-line{background:rgba(28,28,30,.6);-webkit-backdrop-filter:blur(18px) saturate(1.5);backdrop-filter:blur(18px) saturate(1.5);' +
      'color:#fff;border-radius:14px;padding:7px 14px;font-size:15px;line-height:1.5;text-align:center;' +
      'box-shadow:0 6px 24px rgba(0,0,0,.18);max-width:100%;word-break:break-word;transition:opacity .3s}' +
      '#vc-cap .vc-cap-prev{opacity:.45;font-size:13px;padding:5px 12px}' +
      '#vc-cap .vc-cap-st{opacity:.85;font-size:13px;padding:5px 12px;background:rgba(28,28,30,.48)}' +
      '#vc-cap .vc-cap-u{background:rgba(10,132,255,.62)}' +   // 用户句:iMessage 蓝,与 AI 深灰一眼区分
      // "正在听"等待指示:mic 线条图标 + 三点依次跳动(ASR 通话空闲时常驻)
      '#vc-cap .vc-cap-wait{display:flex;align-items:center;gap:7px;padding:6px 13px;background:rgba(28,28,30,.48)}' +
      '#vc-cap .vc-cap-st{display:flex;align-items:center;gap:7px}' +
      '#vc-cap .vc-cap-st svg{width:14px;height:14px;flex:none;opacity:.9}' +
      '#vc-cap .vc-cap-st.vc-st-ok{background:rgba(38,58,44,.62);color:#c7f0d2}' +
      '#vc-cap .vc-cap-st.vc-st-err{background:rgba(70,36,36,.62);color:#ffd0cc}' +
      '#vc-cap .vc-cap-st .vc-tks.ok{color:#30d158;font-weight:600}' +
      '#vc-cap .vc-cap-st .vc-tks.err{color:#ff6961}' +
      '.vc-spin-s{width:11px;height:11px;border-width:1.6px;flex:none}' +
      '#vc-cap .vc-cap-wait svg{width:13px;height:13px;opacity:.8;flex:none}' +
      '#vc-cap .vc-cap-wait i{width:4px;height:4px;border-radius:50%;background:#fff;opacity:.3;animation:vcCapDot 1.4s ease-in-out infinite}' +
      '#vc-cap .vc-cap-wait i:nth-of-type(2){animation-delay:.22s}' +
      '#vc-cap .vc-cap-wait i:nth-of-type(3){animation-delay:.44s}' +
      '@keyframes vcCapDot{0%,100%{opacity:.25;transform:translateY(0)}50%{opacity:.95;transform:translateY(-2.5px)}}' +
      // 65 文字卡片:iOS 通知风磨砂堆叠(右下锚,新卡在前,旧卡左上交错缩小)
      '#vc-cards{position:fixed;right:14px;bottom:calc(150px + env(safe-area-inset-bottom,0px));z-index:2147481400;width:min(80vw,340px);pointer-events:none}' +
      '.vc-card{position:absolute;right:0;bottom:0;width:100%;background:rgba(28,28,30,.72);-webkit-backdrop-filter:blur(24px) saturate(1.6);backdrop-filter:blur(24px) saturate(1.6);' +
      '-webkit-user-select:none;user-select:none;-webkit-touch-callout:none;' +
      'border:0.5px solid rgba(255,255,255,.14);border-radius:16px;box-shadow:0 12px 40px rgba(0,0,0,.4);color:#f2f2f7;font-size:14px;line-height:1.55;' +
      'padding:10px 13px 12px;pointer-events:auto;display:flex;flex-direction:column;max-height:36vh;' +
      'transition:transform .38s cubic-bezier(.32,.72,.36,1),opacity .32s ease;font-family:-apple-system,system-ui,sans-serif}' +
      '.vc-card-hd{display:flex;align-items:center;gap:6px;font-size:12px;color:#b9a8ff;margin-bottom:6px;flex:none}' +
      '.vc-card-x{margin-left:auto;width:22px;height:22px;border-radius:50%;background:rgba(255,255,255,.14);border:none;color:#e8e8ee;display:flex;align-items:center;justify-content:center;cursor:pointer;padding:0;flex:none}' +
      '.vc-card-x svg{width:10px;height:10px}' +
      '.vc-card-p{margin-left:auto;width:22px;height:22px;border-radius:50%;background:rgba(123,108,255,.16);border:none;color:#9d8cff;' +
      'font-size:9px;display:flex;align-items:center;justify-content:center;cursor:pointer;padding:0 0 0 1px;flex:none;transition:transform .12s}' +
      '.vc-card-p:active{transform:scale(.82)}' +
      '.vc-card-p.playing{background:#7b6cff;color:#fff;animation:vcClipBreath2 1.8s ease-in-out infinite}' +
      '@keyframes vcClipBreath2{0%,100%{box-shadow:0 0 0 0 rgba(123,108,255,.45)}50%{box-shadow:0 0 0 6px rgba(123,108,255,0)}}' +
      '.vc-card-hd .vc-card-x{margin-left:6px}' +
      '.vc-card-bd{overflow-y:auto;white-space:pre-wrap;word-break:break-word;-webkit-overflow-scrolling:touch;min-height:0}' +
      '.vc-card.vc-lift{box-shadow:0 22px 60px rgba(0,0,0,.55),0 0 0 0.5px rgba(255,255,255,.2);cursor:grabbing}' +
      '.vc-card-sum{display:none;font-size:12.5px;color:#aab8d4;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}' +
      '.vc-card.vc-min .vc-card-bd{display:none}.vc-card.vc-min .vc-card-sum{display:block}' +
      '.vc-card.vc-min{padding:9px 13px}' +
      // ── 工具指示器 v2(用户设计):在这张卡上加**第三态=圆形标记**,并让标记本身当形态控制按钮 ──
      //    圆(vc-dot,创建/收起:透明玻璃无边缘) → 长条(vc-min) → 方块(展开)。标记坐落在方块左上角。
      // 标记 = 圆角方形(用户改:套长条的外观,别用正圆),坐落在卡片**左上角**,永远是形态控制按钮
      '.vc-card-dot{position:absolute;left:0;top:0;width:36px;height:36px;border-radius:12px;padding:0;border:0.5px solid var(--vc-tl,rgba(255,255,255,.16));' +
        'background:var(--vc-tf,rgba(28,28,30,.72));-webkit-backdrop-filter:blur(18px);backdrop-filter:blur(18px);' +
        'color:var(--vc-tc,#b9a8ff);display:flex;align-items:center;justify-content:center;cursor:pointer;z-index:2;' +
        'transition:transform .16s cubic-bezier(.34,1.5,.64,1),background .3s,border-color .3s,box-shadow .3s}' +
      '.vc-card-dot svg{width:17px;height:17px}' +
      '.vc-card.vc-hasdot .vc-card-hd{padding-left:42px;min-height:36px}' +   // 标记坐在左上角 → 标题让位
      '.vc-card.vc-hasdot.vc-min .vc-card-sum{padding-left:42px}' +
      '.vc-card-dot:active{transform:scale(.9)}' +
      '.vc-card-dot.busy{animation:vcDotBr 1.5s ease-in-out infinite}' +
      '@keyframes vcDotBr{0%,100%{opacity:.45}50%{opacity:1}}' +
      // 进行中/创建:标记是**透明玻璃**(无色);出结果:有色磨砂(两个状态一眼可辨,用户要求)
      '.vc-card.vc-typed .vc-card-dot{--vc-tf:color-mix(in srgb,var(--vc-tc) 22%,rgba(22,26,38,.8));' +
        '--vc-tl:color-mix(in srgb,var(--vc-tc) 48%,transparent);box-shadow:0 6px 18px -8px rgba(0,0,0,.5)}' +
      '.vc-card.vc-busy .vc-card-dot{--vc-tf:rgba(255,255,255,.06);--vc-tl:transparent;box-shadow:none}' +
      // 收起态:整张卡就是那枚圆角方形标记
      '.vc-card.vc-dot{width:36px;height:36px;min-height:0;padding:0;border-radius:12px;border-color:transparent;' +
        'background:transparent;box-shadow:none;overflow:visible}' +
      '.vc-card.vc-dot .vc-card-hd,.vc-card.vc-dot .vc-card-sum,.vc-card.vc-dot .vc-card-bd{display:none}' +
      // 三态生长:**以左上角(标记位置)为原点**拉长/展开——标记不动,卡片从它身上长出来
      '.vc-card.vc-hasdot{right:auto;bottom:auto;transform-origin:0 0;' +
        'transition:width .34s cubic-bezier(.2,.85,.3,1),height .34s cubic-bezier(.2,.85,.3,1),' +
        'border-radius .3s,background .3s,box-shadow .3s,border-color .25s,' +
        'transform .3s cubic-bezier(.34,1.35,.64,1),opacity .3s}' +
      // 长条 = 原卡片的折叠态(标题行 + ▶/✕ + 一行摘要,就是用户截图里那个),只是给左上角标记让出位置
      '.vc-card.vc-hasdot.vc-min{width:296px}' +
      '.vc-card.vc-hasdot:not(.vc-min):not(.vc-dot){width:320px}' +
      // 完成态:有色磨砂 + 边缘阴影(跟"创建时的透明玻璃圆"区分开)
      '.vc-card.vc-typed{border-color:color-mix(in srgb,var(--vc-tc) 42%,transparent);' +
        'background:color-mix(in srgb,var(--vc-tc) 13%,rgba(28,28,30,.74));' +
        'box-shadow:0 16px 42px rgba(0,0,0,.5),0 0 22px -8px color-mix(in srgb,var(--vc-tc) 55%,transparent)}' +
      '.vc-card.vc-typed .vc-card-hd{color:var(--vc-tc)}' +
      '.vc-card.vc-typed.vc-dot{background:rgba(255,255,255,.05);box-shadow:none;border-color:transparent}' +
      '.vc-card.vc-err{--vc-tc:#ff6961}' +
      // 步骤区(内部步骤全推出来 = 原「!」详情面板的内容)
      '.vc-stp{margin-top:8px;border-top:0.5px solid rgba(255,255,255,.1);padding-top:6px}' +
      '.vc-stp-b{background:transparent;border:0;color:#8a9bb4;font-size:11px;padding:0;cursor:pointer}' +
      '.vc-stp-i{display:flex;gap:7px;align-items:flex-start;margin-top:5px;font-size:11.5px;color:#a9b8d4}' +
      '.vc-stp-i i{flex:none;width:5px;height:5px;border-radius:50%;background:var(--vc-tc,#b9a8ff);margin-top:5px}' +
      '.vc-stp-k{color:#7c93c4;font-size:10.5px;font-weight:700;margin-top:6px}' +
      '.vc-stp-v{color:#b9c6e0;font-family:ui-monospace,Menlo,monospace;font-size:11px;line-height:1.5;word-break:break-all;white-space:pre-wrap;max-height:140px;overflow:auto}' +
      // Anki 完整卡片预览(正/反面翻页 + 挖空 + 公式/图由 MathJax/img 渲染)
      '.vc-fc{margin-top:7px;background:rgba(0,0,0,.26);border:0.5px solid rgba(255,255,255,.1);border-radius:9px;padding:7px 9px;color:#e6ecf8;font-size:13px}' +
      '.vc-fc img{max-width:100%;border-radius:6px;margin-top:5px;display:block}' +
      '.vc-fc-t{font-size:9.5px;letter-spacing:.1em;color:#7c8bab;font-weight:700;margin-bottom:3px}' +
      '.vc-cz{background:rgba(123,108,255,.22);border-bottom:1.5px solid #7b6cff;border-radius:3px;padding:0 5px;color:#cdc6ff;font-weight:600}' +
      '.vc-fc-n{display:flex;align-items:center;gap:7px;margin-top:7px}' +
      '.vc-fc-n button{background:transparent;border:0.5px solid rgba(255,255,255,.16);border-radius:7px;color:#93a4c6;width:26px;height:24px;cursor:pointer;font-size:13px;padding:0}' +
      '.vc-fc-n button:disabled{opacity:.3}' +
      '.vc-fc-d{width:6px;height:6px;border-radius:50%;background:rgba(255,255,255,.22)}' +
      '.vc-fc-d.on{background:#fff;width:14px;border-radius:3px}' +
      // 侧栏内联(同一张卡进对话流:静态排布,不绝对定位)
      '.vc-card.vc-inflow{position:relative;right:auto;bottom:auto;width:100%;margin:8px 0;transform:none!important;max-height:none}' +
      '@keyframes vcPinPop{0%{filter:brightness(1)}40%{filter:brightness(1.4)}100%{filter:brightness(1)}}' +   // ⚠不用 transform:会覆盖拖动后的内联 translate 导致瞬移
      '.vc-pin-pop{animation:vcPinPop .4s ease}' +
      '#vc-dock-btn{position:fixed;right:14px;bottom:calc(96px + env(safe-area-inset-bottom,0px));z-index:2147481420;width:40px;height:40px;border-radius:50%;' +
      'border:0.5px solid rgba(255,255,255,.16);background:rgba(40,36,64,.72);-webkit-backdrop-filter:blur(20px);backdrop-filter:blur(20px);' +
      'color:#b9a8ff;display:none;align-items:center;justify-content:center;cursor:pointer;box-shadow:0 8px 26px rgba(0,0,0,.4);padding:0}' +
      '#vc-dock-btn .vc-dk-n{position:absolute;top:-4px;right:-4px;min-width:16px;height:16px;border-radius:8px;background:#7b6cff;color:#fff;font-size:10px;display:flex;align-items:center;justify-content:center;padding:0 4px}' +
      '#vc-dock-hint{position:fixed;left:0;right:0;bottom:0;height:150px;pointer-events:none;z-index:2147481410;opacity:0;transition:opacity .25s;' +
      'background:linear-gradient(to top,rgba(123,108,255,.38),rgba(123,108,255,.1) 55%,transparent)}' +
      '#vc-dock-hint.on{opacity:1}' +
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
      '.vc-drag-ghost{position:fixed;z-index:2147481460;pointer-events:none;opacity:.88;transform:scale(.92);border-radius:14px;overflow:hidden;color:#dde6f5;font-size:12.5px;line-height:1.5;' +
      'background:rgba(30,32,42,.9);-webkit-backdrop-filter:blur(20px);backdrop-filter:blur(20px);border:0.5px solid rgba(255,255,255,.18);' +
      'box-shadow:0 18px 50px rgba(0,0,0,.55);padding:10px 12px;max-height:120px}' +
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
      '.vc-if-hd{font-size:12px;color:#c3cee6;margin:-4px -6px 6px;padding:5px 8px;display:flex;align-items:center;gap:6px;cursor:grab;' +
      'background:rgba(255,255,255,.06);border-radius:9px;font-weight:600}' +
      '.vc-if-hd span:first-child{flex:1}' +
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
      '.vc-ig-cell.vc-picked{box-shadow:0 0 0 2px rgba(123,108,255,.9)}' +
      '.vc-ig-img{width:100%;display:block;border-radius:10px 10px 0 0;cursor:pointer}' +
      '.vc-ig-t{font-size:10.5px;color:#9fb0cf;padding:3px 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}' +
      '.vc-vg-wrap{position:relative}' +
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
      '#asst-call{background:#16203a;border:1px solid #2a3a63;color:#9fb4e0;width:42px;height:42px;border-radius:12px;cursor:pointer;flex:none;display:flex;align-items:center;justify-content:center;transition:background .2s,color .2s,border-color .2s,transform .1s;-webkit-tap-highlight-color:transparent}' +
      '#asst-call:active{transform:scale(.9)}' +
      '#asst-call.on{background:#1a7f4b;border-color:#1a7f4b;color:#fff;animation:vcCallPulse 1.6s ease-in-out infinite}' +
      // 播报中:蓝色快脉冲(盖过 .on 绿;user 开口打断后自动回绿)
      '#asst-call.speaking{background:#0a84ff;border-color:#0a84ff;color:#fff;animation:vcCallPulse 1s ease-in-out infinite}' +
      '#asst-call.connecting{background:#8a5a00;border-color:#ff9f0a;color:#ffd60a;animation:vcCallPulse .7s ease-in-out infinite}' +
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
      '#vc-top-mic,#vc-top-call{-webkit-user-select:none;user-select:none;-webkit-touch-callout:none;touch-action:manipulation}' +
      // 长按/连点这些控件时禁掉 iOS 文本选中高亮与放大镜(长按手势专用控件,选中毫无意义)
      '#asst-call,#asst-mic,#vc-tool-btn,.vc-speak-tg,#asst-input button,#asst-quick button,#rc-vc .vc-grab,#rc-vc .vc-head button{-webkit-user-select:none;user-select:none;-webkit-touch-callout:none;touch-action:manipulation}' +
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
  function _chipKey(p) { return (p.tool || p.label || 'tool') + '#' + (p.call_id || p.id || _rtc.toolN || 0); }
  function _chipStart(p) {
    if (!(window.RC && RC.toolChip)) return null;
    var k = _chipKey(p);
    if (_chipOf[k]) return _chipOf[k];
    var c = RC.toolChip.create({ tool: p.tool || '', label: p.label || p.tool || '工具' });
    RC.toolChip.progress(c, (p.label || '处理') + '…');
    _chipOf[k] = c;
    return c;
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
  function _chipEnd(p) {
    if (!(window.RC && RC.toolChip)) return;
    var k = _chipKey(p), c = _chipOf[k];
    if (!c) {   // 没见过 running(缓存命中/补发)→ 现造一个直接收尾
      c = RC.toolChip.create({ tool: p.tool || '', label: p.label || '工具' });
      _chipOf[k] = c;
    }
    delete _chipOf[k];
    RC.toolChip.setMeta(c, _chipMeta(p));
    if (p.status === 'error') { RC.toolChip.fail(c, p.label || '失败'); return; }
    // 后台任务(制卡/记笔记/生词):工具只是"派发成功",真正的步骤与结果要继续轮询 task-status
    var tid = p.task_id || (p.result && p.result.task_id) || _pickTaskId(p.rag);
    if (tid) { RC.toolChip.progress(c, '已派发,正在后台执行…'); _chipTrackTask(c, tid); return; }
    RC.toolChip.done(c, { summary: p.label || '完成', detail: p.rag || p.result_brief || '' });
  }
  function _pickTaskId(rag) {   // 工具返回体里带 task_id(voice-tool 的 rag 是 JSON 字符串)
    try { var o = typeof rag === 'string' ? JSON.parse(rag) : rag; return (o && o.task_id) || ''; } catch (e) { return ''; }
  }
  function _chipTrackTask(c, tid) { RC.toolChip.track(c, tid); }   // 轮询后台任务(组件内实现,与文字对话共用)

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
  async function _aecSetup(acx) {
    if (_aec.ready && _aec.ctx === acx) return;
    _aecTeardown();
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
        _aec.el = el;
        try { el.play(); } catch (_) {}
      };
      dest.stream.getTracks().forEach(function (t) { pc1.addTrack(t, dest.stream); });
      var offer = await pc1.createOffer();
      await pc1.setLocalDescription(offer);
      await pc2.setRemoteDescription(offer);
      var ans = await pc2.createAnswer();
      await pc2.setLocalDescription(ans);
      await pc1.setRemoteDescription(ans);
      _aec.dest = dest; _aec.pc1 = pc1; _aec.pc2 = pc2; _aec.ctx = acx; _aec.ready = true;
    } catch (e) { _aecTeardown(); }   // 失败回落直连(现状),不阻塞通话
  }
  function _aecTeardown() {
    try { if (_aec.pc1) _aec.pc1.close(); } catch (e) {}
    try { if (_aec.pc2) _aec.pc2.close(); } catch (e) {}
    try { if (_aec.el) _aec.el.remove(); } catch (e) {}
    _aec = { dest: null, el: null, pc1: null, pc2: null, ready: false, ctx: null };
  }

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
    if (fn === 'renderVideos') { renderVids((args || [[]])[0] || [], (args || [])[1]); return; }
    if (fn === 'renderImages') { renderImgs((args || [[]])[0] || []); return; }
    if (fn === 'renderInfoCard') { renderInfo((args || [{}])[0] || {}); return; }
    try { if (typeof window[fn] === 'function') window[fn].apply(null, args || []); } catch (e) {}
  }
  // ── 70 结构化结果卡(用户设计):web_search 的 Gemini 综合直接给 kind/data/brief——
  //    系统按类型渲染(天气/新闻/事实/综合),2.1 只口头一句概况;双击卡=内容带入 2.1 上下文(再双击=移出) ──
  function _infoHtml(card) {
    var k = card.kind, d = card.data || {}, h = '';
    function e0(x) { return esc(String(x == null ? '' : x)); }
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
        return '<div class="vc-ig-cell" data-i="' + i + '">' +
          '<button type="button" class="vc-ig-x" data-i="' + i + '" aria-label="移除">✕</button>' +
          '<img class="vc-ig-img" data-i="' + i + '" src="' + esc(it.url || '') + '" alt="' + esc(it.title || '') + '">' +
          (it.title ? '<div class="vc-ig-t">' + esc(it.title) + '</div>' : '') + '</div>';
      }).join('') + '</div>';
    } else if (k === 'videos') {
      h = '<div class="vc-ig">' + (d.items || []).map(function (it, i) {
        return '<div class="vc-ig-cell" data-i="' + i + '">' +
          '<button type="button" class="vc-ig-x" data-i="' + i + '" aria-label="移除">✕</button>' +
          '<span class="vc-vg-tag' + (it.src === 'bili' ? ' bili' : '') + '">' + (it.src === 'bili' ? 'B站' : 'YouTube') + '</span>' +
          '<div class="vc-vg-wrap"><img class="vc-ig-img" data-i="' + i + '" loading="lazy" referrerpolicy="no-referrer" src="' + esc(it.thumb || '') + '" alt="">' +
          '<button type="button" class="vc-vg-play" data-i="' + i + '" aria-label="播放">▶</button></div>' +
          '<div class="vc-ig-t">' + esc(it.title || '') + (it.channel ? '<br><span class="vc-vg-ch">' + esc(it.channel) + '</span>' : '') + '</div></div>';
      }).join('') + '</div>';
    } else if (k === 'fact') {
      h = '<div class="vc-if-f"><div class="vc-if-fa">' + e0(d.answer) + '</div>' +
          (d.detail ? '<div class="vc-if-fd">' + e0(d.detail) + '</div>' : '') + '</div>';
    } else {
      h = '<div class="vc-if-g">' + e0(d.text || card.brief || '') + '</div>';
    }
    if (card.sources && card.sources.length) {
      h += '<div class="vc-if-srcs">' + card.sources.slice(0, 3).map(function (sc) {
        return '<a href="' + esc(sc.url || '#') + '" target="_blank" rel="noopener">' + e0((sc.title || '来源').split('.')[0]) + '</a>';
      }).join(' · ') + '</div>';
    }
    return h;
  }
  function _infoText(card) {   // 双击带入上下文用的纯文本化
    var d = card.data || {}, k = card.kind;
    if (k === 'weather') return (card.title || '天气') + ':' + [d.loc, d.date, d.cond, (d.lo != null ? d.lo + '-' + d.hi + '°C' : ''), (d.precip != null ? '降水' + d.precip + '%' : ''), d.tip].filter(Boolean).join(',');
    if (k === 'news') return (card.title || '新闻') + ':' + (d.items || []).map(function (it) { return (it.t || '') + '(' + (it.s || '') + ')'; }).join(';');
    if (k === 'fact') return (card.title || '') + ':' + (d.answer || '') + ' ' + (d.detail || '');
    if (k === 'images') return (card.title || '配图') + ':' + (d.items || []).map(function (it) { return (it.title || '图') + ' ' + (it.url || ''); }).join(';');
    if (k === 'videos') return (card.title || '视频') + ':' + (d.items || []).map(function (it) { return (it.title || '') + '(' + (it.channel || '') + ')' + (it.url || ''); }).join(';');
    return d.text || card.brief || card.title || '';
  }
  // 77 pin 状态中心:选中集合为唯一真相(卡片紫框只是视图)。注入改**覆盖式快照**(防抖 1.2s+指纹):
  // 反复选中/取消若最终状态没变=零注入;变了=一条"当前带入清单(以本条为准,旧声明作废)"——历史不膨胀、语义无歧义
  var _pins = { map: {}, fp: null, t: null, els: {}, cids: {} };   // 95:cids={卡片稳定编号:label}——同卡多实例(浮层/侧栏/收藏夹拖出)去重
  var _cidSeq = 0;
  function _mkCid() { return 'c' + Date.now().toString(36) + '-' + (++_cidSeq); }   // 95:卡片出生编号,跟随卡片所有形态流转
  function _pinSync() {
    if (_pins.t) clearTimeout(_pins.t);
    _pins.t = setTimeout(function () {
      _pins.t = null;
      var ks = Object.keys(_pins.map);
      var fp = ks.sort().join('|');
      if (fp === _pins.fp) return;
      _pins.fp = fp;
      if (!_rtc.on) return;
      var msg = ks.length
        ? '(参考内容更新(以本条为准,之前的带入/移除声明全部作废):用户当前带入 ' + ks.length + ' 项——' +
          ks.map(function (k) { return '「' + k + '」:' + _pins.map[k]; }).join(';') + '。状态记录,不要回应本条。)'
        : '(参考内容更新:用户已移除全部带入内容,之前的带入声明作废。状态记录,不要回应本条。)';
      try { _rtcSys(msg); } catch (e) {}
    }, 1200);
  }
  // 用户强调:同一编号(cid)的卡无论出现在字幕浮层 / 侧栏 / 收藏夹,选中一处则**处处高亮**,
  //   取消一处则**处处取消**。每个渲染实例出生时来登记,选中态按 cid 广播到全部实例。
  _pins.byCid = {};
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
  function _pinToggle(el, label, textFn) {
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
      _pins.map[lb] = String(textFn()).slice(0, 2500);   // 79:长按=全文入脑(路由长文也放得下)
      _pins.els[lb] = el;
      if (cid) _pins.cids[cid] = lb;
    } else {
      var lb0 = el.dataset.pinLabel || label;
      delete _pins.map[lb0]; delete _pins.els[lb0];
      if (cid && _pins.cids[cid] === lb0) delete _pins.cids[cid];
    }
    el.classList.toggle('vc-picked', on);
    el.classList.add('vc-pin-pop'); setTimeout(function () { el.classList.remove('vc-pin-pop'); }, 420);
    if (cid) _pinPaint(cid, on);   // 同号卡:处处高亮 / 处处取消(用户强调)
    _pinSync(); _chipRender();
  }
  function _pinBind(el, label, textFn) {   // 72:长按 600ms=选中带入 2.1 上下文(紫边框+pop 特效),再长按=移出;选中的浮层卡不自动消失
    el.classList.add('vc-pinnable');
    var lpT = null, lpX = 0, lpY = 0;
    function _fire() {
      lpT = null;
      // 97(用户设计):不再限通话中——文字模式同样可长按带入(chips 照常出输入框上方,send 时随 ctx.pinned 走文字管线)
      _pinToggle(el, label, textFn);   // 77:状态中心统一管(通话=覆盖式注入;文字=send 注入;chip 同步)
    }
    el.addEventListener('pointerdown', function (ev) {
      if (ev.target.closest('.vc-card-x')) return;
      lpX = ev.clientX; lpY = ev.clientY;
      if (lpT) clearTimeout(lpT);
      lpT = setTimeout(_fire, 600);
    });
    el.addEventListener('pointermove', function (ev) {
      if (lpT && (Math.abs(ev.clientX - lpX) + Math.abs(ev.clientY - lpY)) > 14) { clearTimeout(lpT); lpT = null; }   // 拖动=取消长按(75:8px 手指按住必抖过=长按永远触发不了的根因,放宽)
    });
    el.addEventListener('contextmenu', function (ev) { ev.preventDefault(); });   // 75:iOS 长按系统菜单会抢手势
    ['pointerup', 'pointercancel', 'pointerleave'].forEach(function (evn) {
      el.addEventListener(evn, function () { if (lpT) { clearTimeout(lpT); lpT = null; } });
    });
  }
  function _igWire(root, card) {   // 88/98:图卡+视频卡交互——✕移除;点封面=只选中这一张(带入上下文,再点取消);视频▶=播放
    if (!card || (card.kind !== 'images' && card.kind !== 'videos')) return;
    root.addEventListener('click', function (ev) {
      var pb = ev.target.closest && ev.target.closest('.vc-vg-play');
      if (pb) {   // 98:播放钮=原播放行为(浮动播放器/新窗),不参与选中
        ev.stopPropagation();
        var ip = +pb.getAttribute('data-i');
        var vt = ((card.data || {}).items || [])[ip] || {};
        try {
          if (window.RC && RC.videoPlayer) RC.videoPlayer.open({ id: vt.id, src: vt.src === 'bili' ? 'bili' : 'yt', title: vt.title });
          else window.open(vt.url || '', '_blank');
        } catch (e) {}
      }
    });
    root.addEventListener('click', function (ev) {
      var x = ev.target.closest('.vc-ig-x');
      if (x) {
        ev.stopPropagation();
        var i0 = +x.getAttribute('data-i');
        var cell = x.closest('.vc-ig-cell');
        if (cell) cell.remove();
        try { (card.data.items || [])[i0]._gone = 1; } catch (e) {}
        return;
      }
      var img = ev.target.closest('.vc-ig-img');
      if (img) {
        ev.stopPropagation();
        var i1 = +img.getAttribute('data-i');
        var it = ((card.data || {}).items || [])[i1] || {};
        var cell1 = img.closest('.vc-ig-cell');
        var on = !cell1.classList.contains('vc-picked');
        root.querySelectorAll('.vc-ig-cell.vc-picked').forEach(function (c2) {   // 单选:先清其它
          c2.classList.remove('vc-picked');
          var lb2 = c2.dataset.pinLabel; if (lb2 && _pins.map[lb2]) { delete _pins.map[lb2]; delete _pins.els[lb2]; }
          var gc2 = c2.dataset.vcCid; if (gc2 && _pins.cids[gc2]) delete _pins.cids[gc2];
        });
        if (on) {
          var gcid = (card.cid || '') + '#' + i1;   // 95:图编号=卡号#序号(浮层/侧栏两实例互斥)
          if (_pins.cids[gcid] && _pins.map[_pins.cids[gcid]]) {
            try { if (typeof _toast === 'function') _toast('这张图已在上下文中'); } catch (e) {}
            return;
          }
          cell1.classList.add('vc-picked');
          var lb = (it.title || (card.kind === 'videos' ? '视频' : '配图')) + (card.kind === 'videos' ? '·视频' : '·图') + (i1 + 1);
          cell1.dataset.pinLabel = lb; cell1.dataset.vcCid = gcid;
          _pins.map[lb] = ((it.title || '') + (it.channel ? '(' + it.channel + ')' : '') + ' ' + (it.url || '')).slice(0, 500);
          _pins.els[lb] = cell1;
          _pins.cids[gcid] = lb;
        }
        _pinSync(); _chipRender();
      }
    });
  }
  function _infoCardEl(card) {   // 87:构一张侧栏信息卡(实时与历史回放共用——刷新后卡永远还是卡)
    if (!card.cid) card.cid = _mkCid();   // 95:历史旧卡(落库时还没 cid 字段)补发——本次会话内该实例稳定
    var label = card.title || '搜索结果';
    var html = '<div class="vc-if-hd"><span>' + esc(label) + '</span><span class="vc-grip">⠿</span></div>' + _infoHtml(card);
    var d = document.createElement('div'); d.className = 'asst-msg asst-a vc-if';
    d.innerHTML = html;
    _pinBind(d, label, function () { return _infoText(card); });
    var _srcs = '';
    if (card.kind === 'videos') _srcs = ((card.data || {}).q || '') || '未记录(旧卡片)';   // 98:视频溯源=两源搜索词
    if (card.kind === 'images') {   // 88/90:溯源——每张图哪个源哪个词命中;源名映射可读,绝不显示问号
      var SRC_NAME = { commons: '维基共享(Commons)', google: 'Google 图搜', openai: 'OpenAI 搜索', gemini: 'Gemini' };
      var seenS = {};
      _srcs = ((card.data || {}).items || []).map(function (it) {
        var nm = SRC_NAME[it.src] || it.src || '';
        var k2 = nm ? (nm + (it.q ? '「' + it.q + '」' : '')) : '';
        if (!k2 || seenS[k2]) return ''; seenS[k2] = 1; return k2;
      }).filter(Boolean).join(' · ') || '未记录(旧卡片)';
    }
    try { window.__asstInfoBtn && window.__asstInfoBtn(d, { kind: '搜索卡 · ' + card.kind, mode: '静默入库(联网搜索)', srcs: _srcs || undefined,
      actions: (card.kind === 'images' ? ['img_norm'] : card.kind === 'videos' ? ['pick_video'] : ['web_search']) }); } catch (e) {}
    try { _dragToDock(d, function () { return { label: label, kind: card.kind, raw: html, isHtml: true, text: _infoText(card) }; }); } catch (e) {}
    try { _igWire(d, card); } catch (e) {}   // 88:图卡交互(✕/单选)
    try { d.dataset.vcCid = card.cid; } catch (e) {}   // 95:同卡编号(与浮层镜像实例共享)
    return d;
  }
  window.__vcInfoCardEl = function (card) { try { return (card && card.kind) ? _infoCardEl(card) : null; } catch (e) { return null; } };
  function renderInfo(card) {
    if (!card || !card.kind) return;
    // 75(用户设计):静默入库配听觉确认——卡片弹出时念一声"搜索完成"(仅通话中且当前形态有语音输出)
    if (_rtc.on && (_voiceMode() !== 'stt' || _ttsOn())) { try { _speakSafe('搜索完成'); } catch (e) {} }
    var label = card.title || '搜索结果';
    var th = document.getElementById('asst-thread');
    if (th) { var d = _infoCardEl(card); th.appendChild(d); th.scrollTop = th.scrollHeight; }
    if (!_sideOpen()) {
      var html = '<div class="vc-if-hd"><span>' + esc(label) + '</span><span class="vc-grip">⠿</span></div>' + _infoHtml(card);
      var c = _cardPush(html, label, true, false, card.cid);   // 字幕模式:浮层镜像(html,与侧栏卡同编号)
      if (c) { _pinBind(c.el, label, function () { return _infoText(card); }); try { _igWire(c.el, card); } catch (e) {} }
    }
    // 87:卡片落库(独立条,content=brief,结构在 meta.card)——刷新/跨设备后历史里仍是可交互的卡
    try {   // ⚠EPUB 自有历史(epub-convo)结构不同,card 落库暂只走 PDF 主历史
      fetch('/api/assistant/log', { method: 'POST', headers: { 'Content-Type': 'application/json' }, keepalive: true,
        body: JSON.stringify({ assistant: '', card: card, via: 'voice', file: _rtc.ctxFile || '', page: _rtc.ctxPage || 0 }) }).catch(function () {});
    } catch (e) {}
  }
  function renderImgs(imgs) {   // 88:图片结果升格为结构化卡(kind:'images')走 renderInfo 全管线——对话流+浮层同款、落库、回放、✕/单选/溯源
    if (!imgs || !imgs.length) return;
    renderInfo({
      kind: 'images', title: '配图 × ' + imgs.length,
      brief: imgs.map(function (im) { return im.title || im.concept || '图'; }).join('、').slice(0, 120),
      data: { items: imgs.filter(function (im) { return im && im.image_url; }).map(function (im) {
        return { url: im.image_url, title: im.title || im.concept || '', page: im.page_url || '',
                 src: im.source || '', q: im.matched_query || '' };
      }) }
    });
  }
  function renderVids(vids, meta) {   // 98(用户设计):视频升格结构卡(kind:'videos')走 renderInfo 全管线——对话流+浮层同款、✕/单选/播放、落库回放、溯源
    if (!vids || !vids.length) return;
    renderInfo({
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
    var w = (ws && mode === 'agent' && ws.readyState === 1) ? ws : _tts.ws;   // agent 通话在→走它;否则朗读专用通道
    if (w && w.readyState === 1) { try { w.send(JSON.stringify({ type: 'speak', text: t, id: ++vt.sid, mood: vt.mood || '' })); } catch (e) {} }
  }
  function bargeIn() {   // 打断:清本地播放队列 + 作废 relay 侧排队/在流的合成(两条通道都发)
    try { _sq.length = 0; if (_sqT) { clearInterval(_sqT); _sqT = null; } } catch (e) {}   // 67b:排队待念的也作废
    stopPlayback(); _ttsStopPlay(); capClear();
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
  function _capSeg(text) { if (text) { _cap.pend.push(text); _cap.bind = true; } }   // 收到 tts_seg 帧:下一个音频块=该句开头
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
    } catch (e) {}
    if (_tts.ws && _tts.ws.readyState <= 1) return;
    try {
      var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
      var w = new WebSocket(proto + location.host + '/voice-rt?mode=tts');
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
    var src = a.createBufferSource(); src.buffer = ab; src.connect(a.destination);
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
    if (!speakOn()) return;                 // 「🔊 朗读」没点亮=零 TTS 成本(读比听快,用户拍板默认关)
    if (ws && mode === 's2s') return;       // S2S 通话:豆包自己出声,朗读开关不适用
    if (!(ws && mode === 'agent')) _ttsEnsure();   // 没开 ASR 通话(纯打字/听写提问)→ lazy 朗读专用通道
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
        var wd = (ws && mode === 'agent' && ws.readyState === 1) ? ws : _tts.ws;
        if (wd && wd.readyState === 1) wd.send(JSON.stringify({ type: 'speak_done' }));   // FinishSession:让尾巴合成完
      } catch (e) {}
      if (pendingUtter) { var p = pendingUtter; pendingUtter = null; sendToAssistant(p, true); }   // 排队的下一句(别掐掉刚念的尾巴)
    }
  };
  window.__asstVoiceOn = function () { return speakOn() && !(ws && mode === 's2s'); };   // 朗读亮且非 S2S 通话 → 后端回答用『适合朗读』风格
  function sendToAssistant(text, keepAudio) {
    if (!keepAudio) bargeIn();        // 新问题:停掉还在念的旧回答(排队派发除外)
    vt.sent = 0; vt.tail = ''; vt.pref = ''; vt.mood = null;
    if (window.__asstBusy && window.__asstBusy()) { pendingUtter = text; taPlaceholder('⏳ 上一条还在答,已排队:' + text.slice(0, 14) + '…'); return; }
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
  var _rtc = { pc: null, dc: null, el: null, mic: null, on: false, imgOn: false, callId: '',
               ctxFile: '', ctxPage: 0, ink: null, sel: '', _inkFp: '', inkDirty: false,
               items: [], inTok: 0, compactTh: 0, lastCompact: 0 };   // ㊳:item 账本+每轮输入量+会话内压缩阈值
  function _dcSend(obj) { try { if (_rtc.dc && _rtc.dc.readyState === 'open') _rtc.dc.send(JSON.stringify(obj)); } catch (e) {} }
  function _rtcSys(text) {
    _dcSend({ type: 'conversation.item.create', item: { type: 'message', role: 'system', content: [{ type: 'input_text', text: text }] } });
  }
  // ── 66 按轮语音录制(用户设计:历史对话可回放当时的语音):remote 轨 MediaRecorder,
  //    音频轮 created 开录 / done 收尾拿 clipId(上传异步),文字轮不录;打断的半截轮也收 ──
  var _rec = { mr: null, chunks: [], mime: '' };
  function _recMime() {
    var c = ['audio/mp4', 'audio/webm;codecs=opus', 'audio/webm'];
    for (var i = 0; i < c.length; i++) { try { if (window.MediaRecorder && MediaRecorder.isTypeSupported(c[i])) return c[i]; } catch (e) {} }
    return '';
  }
  function _recStart() {
    _recAbort();
    if (!_rtc.remoteStream || !window.MediaRecorder) return;
    var mime = _recMime(); if (!mime) return;
    try {
      var mr = new MediaRecorder(_rtc.remoteStream, { mimeType: mime });
      _rec = { mr: mr, chunks: [], mime: mime };
      mr.ondataavailable = function (e) { if (e.data && e.data.size) _rec.chunks.push(e.data); };
      mr.start(1000);
    } catch (e) { _rec.mr = null; }
  }
  function _recAbort() {   // 97:flush 语义——已设 onstop(=已 _recFinish 拿过 id)的照常触发上传;未定稿的无 id 引用,丢弃无害
    try { if (_rec.mr && _rec.mr.state !== 'inactive') { _rec.mr.stop(); } } catch (e) {}
    _rec.mr = null;
  }
  function _recFinish() {   // 返回 clipId(录着才有);blob 在 onstop 异步上传,落库先带 id 不等传完
    var mr = _rec.mr;
    if (!mr || mr.state === 'inactive') { _rec.mr = null; return ''; }
    var id = 'c' + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
    var mime = _rec.mime, chunks = _rec.chunks;
    mr.onstop = function () {
      try {
        var blob = new Blob(chunks, { type: mime });
        if (blob.size > 4000) fetch('/api/assistant/voice-clip?id=' + id, { method: 'POST', headers: { 'Content-Type': mime }, body: blob }).catch(function () {});
      } catch (e) {}
    };
    // 97(用户实测"回放只响一声"根因):录的是 WebRTC **实时**流——response.done 只是数据推完,
    // 音频还要播好几秒;立即 stop=只录到已播的开头。延到估算播放结束(aEnd)+余量再停。
    var _wait = 0;
    try { _wait = Math.max(0, (_rtc.aEnd || 0) - Date.now()) + 600; } catch (e) { _wait = 600; }
    setTimeout(function () { try { if (mr.state !== 'inactive') mr.stop(); } catch (e) {} }, Math.min(_wait, 90000));
    _rec.mr = null;
    return id;
  }
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
    return { readyState: 1, close: function () {}, send: function (data) {
      if (typeof data !== 'string') return;
      // ㊺P2:上行状态(page/state/ink)镜像给控制 WS——relay 是工具执行者,ctx 要最新笔迹/选中/页码
      try { if (_rtc.ctlWs && _rtc.ctlWs.readyState === 1) _rtc.ctlWs.send(data); } catch (e) {}
      var j; try { j = JSON.parse(data); } catch (e) { return; }
      _rtcHandleUp(j);
    } };
  }
  function _rtcHandleUp(j) {
    var t = j.type;
    if (t === 'page') {
      // ㊵ 拉模式(用户拍板"只在需要时读取"):翻页/滚动**只更新本地状态,一个字都不发**——
      // 内容在用户开口/发文字的瞬间经 _rtcFlushCtx 注入(不问=零注入);模型要更多内容自己调 read_page
      var np = j.page;
      if (np && np !== _rtc.ctxPage) { _rtc._inkFp = ''; _rtc.inkDirty = false; }
      if (np) _rtc.ctxPage = np;
      if (j.text != null) _rtc.pendText = String(j.text || '');
    } else if (t === 'ink') {
      var strokes = j.strokes || [];
      _rtc.ink = strokes;
      var fp = (j.page || 0) + ':' + strokes.length;
      if (fp === _rtc._inkFp) return;
      _rtc._inkFp = fp;
      if (!strokes.length) { _rtc.inkDirty = false; _rtcSys('(用户清空了本页笔迹。状态记录,不要回应本条。)'); return; }   // 127:经 data channel 直连注入
      // 边沿触发(用户拍板):笔迹"变了"只通知一次,之后继续画多少笔都不再打扰;
      // AI 重新看过(see_ink/see_page 成功,_rtcTool 里复位)后,下次变化才再通知。
      // 关键是这一次要把旧记忆作废——上次 see_ink 的结果还在上下文里,不否定它模型就凭旧印象答"没变化"。
      if (_rtc.inkDirty) return;
      _rtc.inkDirty = true;   // 127:注入走前端 dc(不再让 relay 代劳绕一圈)
      _rtcSys('(状态更新:用户的手写笔迹刚刚发生了变化。你之前通过 see_ink 看到的笔迹内容**已过时作废**——你现在并不知道纸面上实际写了什么。这只是状态记录,不要回应本条、不要主动评论。之后他问「我写的/我画的/现在呢/看到了什么/有没有变化」这类问题时,唯一正确的做法是先调 see_ink 重新看再回答;没重新看之前,凭旧印象说"和原来一样/没有变化"是错误行为。)');
    } else if (t === 'state') {
      var sel = (j.sel || '').trim();
      if (sel === _rtc.sel) return;
      _rtc.sel = sel;   // 127:注入走前端 dc
      _rtcSys('(状态更新:' + (sel ? ('用户当前选中了「' + sel.slice(0, 200) + '」(他说「这段/我选的」就指它;' +
        (sel.length <= 200 ? '**选中内容已完整在此,直接使用**' : '选中较长已截断,需要完整上下文可 read_page 当前页') + ')') :
        '用户当前没有选中文字') + ';状态记录,不要回应本条)');
    } else if (t === 'text' && j.content) {
      _rtcFlushCtx();   // ㊵ 拉模式:提问瞬间注入他正看着的内容
      _lastU = String(j.content).slice(0, 2000);   // ㉛:打字输入的问题也随轮次落库
      _dcSend({ type: 'conversation.item.create', item: { type: 'message', role: 'user', content: [{ type: 'input_text', text: _lastU }] } });
      _rtcRespCreate('user');
    } else if (t === 'cancel' || t === 'tool_abort') {
      _dcSend({ type: 'response.cancel' });
    }
  }
  // ── 65 文字卡片(用户设计):route/stt 等文字回复在**侧栏关闭**时弹半透明磨砂卡——固定右下,
  //    按时间层叠(新卡在前,旧卡向左上交错缩小),每张可关;自动消失开关+秒数在语音设置卡(设备级)──
  var _cards = { list: [] };
  function _cardHideOn() { try { return localStorage.getItem('rc-voice-card-hide') !== '0'; } catch (e) { return true; } }
  function _cardSecs() { var v = 20; try { v = parseInt(localStorage.getItem('rc-voice-card-secs') || '20', 10) || 20; } catch (e) {} return Math.max(5, Math.min(60, v)); }
  function _sideOpen() { var sd = document.getElementById('ep-side'); return !!(sd && sd.classList.contains('open')); }
  var _dock = { list: [], open: false, loaded: false };
  function _dockLoad(cb) {   // 78:收藏夹服务端持久化(独立于会话,清空对话不清它)
    if (_dock.loaded) { cb && cb(); return; }
    fetch('/api/assistant/voice-cards').then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.ok) { _dock.list = d.cards || []; _dock.loaded = true; }
      _dockBtn(); cb && cb();
    }).catch(function () { cb && cb(); });
  }
  function _favSave(rec) {
    rec.id = rec.id || ('v' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6));
    fetch('/api/assistant/voice-cards', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ op: 'add', card: rec }) }).catch(function () {});
    var i = -1;
    _dock.list.forEach(function (x, k) { if (x.id === rec.id) i = k; });
    if (i < 0) _dock.list.push(rec); else _dock.list[i] = rec;
    _dockBtn(); if (_dock.open) _dockPanel(true);
    return rec.id;
  }
  function _favMeta() {   // 元数据:书/页/触发这轮的问题——长回答离开会话也能自释
    return { file: (_rtc.ctxFile || '').split('/').pop() || '', page: String(_rtc.ctxPage || ''), q: (_lastU || '').slice(0, 120) };
  }
  window.__vcPinBind = function (el, label, textFn) { try { _pinBind(el, label, textFn); } catch (e) {} };   // 79:气泡长按带入(rc-assistant 消费)
  // 工具指示器 v2:把**这张卡**(.vc-card)整套能力暴露出去,rc-toolchip 只当状态机、不另造 DOM——
  //   用户拍板:「我很喜欢这个方块的样式,在这个基础上进行修改就好」。
  RC.voiceCard = {
    push: function (text, label, isHtml, force, cid, opts) { try { return _cardPush(text, label, isHtml, force, cid, opts); } catch (e) { return null; } },
    close: function (c) { try { _cardClose(c); } catch (e) {} },
    form: function (el, f) { try { return _cardForm(el, f); } catch (e) { return 'full'; } },
    layout: function () { try { _cardLayout(); } catch (e) {} },
    mkCid: _mkCid,
    pinReg: function (el, cid) { try { _pinReg(el, cid); } catch (e) {} },       // 登记实例 → 选中按 cid 处处同步
    pinBind: function (el, label, fn) { try { _pinBind(el, label, fn); } catch (e) {} },   // 长按=选中/取消(紫边)
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
  function _dragToDock(el, payloadFn) {   // 83(用户设计):**头部当拖动把手**(仿浮层卡)——即时拖,和对话流滚动零冲突
    try { injectCss(); } catch (e) {}   // 92:ghost 样式保险——通话 UI 没初始化过时侧栏拖动 ghost 曾无样式(看不见"卡片")
    var hd = el.querySelector('.vc-if-hd') || el.firstElementChild || el;
    hd.style.touchAction = 'none'; hd.style.cursor = 'grab';
    var ghost = null, moved = false, sx = 0, sy = 0;
    function _pos(x, y) { if (ghost) { ghost.style.left = (x - ghost.offsetWidth / 2) + 'px'; ghost.style.top = (y - 26) + 'px'; } }
    hd.addEventListener('pointerdown', function (ev) {
      if (ev.target.closest('button,a')) return;
      ev.preventDefault();
      sx = ev.clientX; sy = ev.clientY; moved = false;
      try { hd.setPointerCapture(ev.pointerId); } catch (e) {}
      function mv(e2) {
        if (!moved && (Math.abs(e2.clientX - sx) + Math.abs(e2.clientY - sy)) > 8) {
          moved = true;
          ghost = el.cloneNode(true); ghost.className = 'vc-drag-ghost';
          ghost.style.width = Math.min(el.offsetWidth, 300) + 'px';
          document.body.appendChild(ghost);
          el.style.opacity = '.35';
        }
        if (moved) { _pos(e2.clientX, e2.clientY); _dockHint(_inDockZone(e2.clientX, e2.clientY)); }
      }
      function up(e3) {
        hd.removeEventListener('pointermove', mv); hd.removeEventListener('pointerup', up); hd.removeEventListener('pointercancel', up);
        if (ghost) { ghost.remove(); ghost = null; }
        el.style.opacity = '';
        _dockHint(false);
        if (moved && e3 && _inDockZone(e3.clientX, e3.clientY)) {
          var rec = payloadFn();
          rec.meta = rec.meta || _favMeta();
          rec.cid = rec.cid || (el.dataset && el.dataset.vcCid) || _mkCid();   // 95:收藏保留卡片编号
          _dockLoad(function () { _favSave(rec); });
          try { if (typeof _toast === 'function') _toast('已收入收藏夹'); } catch (e) {}
        } else if (moved && e3 && _sideOpen()) {
          // 92(用户设计):从侧栏把卡拖出到阅读器区(没进收藏夹)=放入字幕浮层。侧栏开着浮层隐身,
          // 只放"飞入"特效示意确实放过去了;关侧栏它就在那。
          var sd2 = document.getElementById('ep-side');
          var sl = sd2 ? sd2.getBoundingClientRect().left : window.innerWidth;
          if (e3.clientX < sl - 30) {
            var rec2 = payloadFn();
            var c2 = _cardPush(rec2.isHtml ? rec2.raw : (rec2.raw || rec2.text), rec2.label, !!rec2.isHtml, true,
              (el.dataset && el.dataset.vcCid) || '');
            if (c2) {
              _placeFx(e3.clientX, e3.clientY);
              try { if (typeof _toast === 'function') _toast('已放入字幕浮层(关闭侧栏可见)'); } catch (e) {}
            }
          }
        }
      }
      hd.addEventListener('pointermove', mv); hd.addEventListener('pointerup', up); hd.addEventListener('pointercancel', up);
    });
  }
  window.__vcDragToDock = function (el, payloadFn) { try { _dragToDock(el, payloadFn); } catch (e) {} };
  window.__vcTtsWarm = function () { try { _ttsEnsure(); } catch (e) {} };   // 82:必须在点击手势**同步栈**内调(iOS AudioContext 手势激活)
  window.__vcSpeakText = function (text) {   // 83:TTS 念一段文字(卡片/气泡播放钮);返回 stop 函数
    try { _ttsEnsure(); } catch (e) {}
    try { _speakSafe(String(text || '').slice(0, 4000)); } catch (e) {}
    return function () { try { bargeIn(); } catch (e) {} };
  };
  window.__vcPins = function () {   // 97:文字助手 send 时取当前带入的卡片(非通话模式的上下文注入)
    try { return Object.keys(_pins.map).map(function (k) { return { label: k, text: _pins.map[k] }; }); } catch (e) { return []; }
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
  function _dockAdd(c) {   // 收入:持久化到服务端(78,清空对话不丢),浮层 DOM 撤
    try { clearTimeout(c.t); } catch (e) {}
    var rec = { label: c.label || '卡片', raw: c.raw || '', isHtml: !!c.isHtml,
                text: (c.el.querySelector('.vc-card-bd') || {}).textContent || '',
                meta: _favMeta() };
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
        _pinBind(cardEl, it.label, function () { return it.text || ''; });   // 长按=带入上下文
        (function () {   // 83:向上拖出=复制浮层卡。touch-action:pan-x=横滑归滚动、竖滑归拖出(旧版被滚动手势吃掉=拖不出的根因)
          var sy0 = 0, drag = false;
          cardEl.style.touchAction = 'pan-x';
          cardEl.addEventListener('pointerdown', function (ev) { sy0 = ev.clientY; drag = true; try { cardEl.setPointerCapture(ev.pointerId); } catch (e) {} });
          cardEl.addEventListener('pointermove', function (ev) {
            if (drag && (sy0 - ev.clientY) > 50) {
              drag = false;
              _cardPush(it.isHtml ? it.raw : (it.raw || it.text), it.label, it.isHtml, false, it.cid || '');   // 95:拖出复制=同一张卡(同编号)
              _dockPanel(false); _dockBtn();
            }
          });
          ['pointerup', 'pointercancel'].forEach(function (evn) { cardEl.addEventListener(evn, function () { drag = false; }); });
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
    var ks = Object.keys(_pins.map);
    if (!_sideOpen() || !ks.length) { if (wrap) wrap.remove(); return; }
    var input = document.getElementById('asst-input');
    if (!input) return;
    if (!wrap) {
      wrap = document.createElement('div'); wrap.id = 'vc-pin-chips';
      wrap.style.cssText = 'display:flex;flex-direction:column;gap:5px;padding:6px 10px 0';
      input.parentNode.insertBefore(wrap, document.getElementById('asst-note-chips') || input);
    }
    wrap.innerHTML = '';
    ks.forEach(function (k) {
      var chip = document.createElement('div'); chip.className = 'asst-fig-chip vc-pin-chip';
      chip.innerHTML = '<span class="vc-pc-l">' + esc(k) + '</span><span class="vc-pc-s">' + esc((_pins.map[k] || '').slice(0, 30)) + '</span>' +
        '<button type="button" class="vc-pc-x" aria-label="移除">✕</button>';
      chip.querySelector('.vc-pc-x').addEventListener('click', function () {
        var el0 = _pins.els[k];
        if (el0 && el0.classList) { el0.classList.remove('vc-picked'); delete el0.dataset.pinLabel; }
        delete _pins.map[k]; delete _pins.els[k];
        _pinSync(); _chipRender();
      });
      wrap.appendChild(chip);
    });
  }
  function _cardsVisSync() {   // 77:侧栏开=浮层卡全部消失(不挡内容);关=回来
    var w = document.getElementById('vc-cards');
    if (w) w.style.display = _sideOpen() ? 'none' : '';
    var tl = document.getElementById('vc-tlayer');
    if (tl) tl.style.display = _sideOpen() ? 'none' : '';
    _chipRender();
  }
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
      if (lb && _pins.map[lb]) { delete _pins.map[lb]; delete _pins.els[lb]; _pinSync(); _chipRender(); }
    } catch (e) {}
    _cards.list.splice(i, 1);
    try { clearTimeout(c.t); } catch (e) {}
    c.el.style.opacity = '0';
    setTimeout(function () { try { c.el.remove(); } catch (e) {} }, 320);
    _cardLayout();
  }
  function _cardPush(text, kindLabel, isHtml, force, cid, opts) {
    opts = opts || {};
    // opts(工具指示器 v2):{tool,type,dot:true 起手圆态,noAuto:自动收起成圆标记而不是关掉}
    if (!opts.dot && (!text || (!isHtml && !text.trim()))) return null;
    if (_sideOpen() && !force && !opts.dot) return null;   // 侧栏开着=内容已在对话流,不弹;force=92 拖放例外
    injectCss();
    var w = document.getElementById('vc-cards');
    if (!w) { w = document.createElement('div'); w.id = 'vc-cards'; document.body.appendChild(w); }
    if (force || opts.dot) _cardsVisSync();   // 92:侧栏开着 force 建卡→容器保持隐藏,关侧栏时浮现
    var el = document.createElement('div'); el.className = 'vc-card' + (opts.dot ? ' vc-dot vc-hasdot' : '');
    var _cid = cid || _mkCid();   // 95:卡片编号(浮层/侧栏/收藏夹同号 → 选中处处同步)
    el.dataset.vcCid = _cid;
    if (opts.type) el.style.setProperty('--vc-tc', opts.type);
    el.innerHTML = '<div class="vc-card-hd">' + (kindLabel || '文字回复') +
      '<button type="button" class="vc-card-p" aria-label="念">▶</button>' +
      '<button type="button" class="vc-card-x" aria-label="关闭">' +
      '<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M3 3l6 6M9 3l-6 6"/></svg></button></div>' +
      '<div class="vc-card-sum"></div><div class="vc-card-bd"></div>';
    if (opts.dot) {   // 圆形标记(坐落在方块左上角)= 形态控制按钮:单击 圆 → 长条 → 方块 → 圆
      var dot = document.createElement('button');
      dot.type = 'button'; dot.className = 'vc-card-dot busy';
      dot.innerHTML = opts.icon || '';
      dot.title = '点击切换形态(圆 / 长条 / 方块)';
      dot.addEventListener('click', function (ev) {
        ev.stopPropagation();
        if (el.dataset.dragged === '1') { el.dataset.dragged = ''; return; }   // 刚拖过 → 这次 click 不算形态切换
        el.dataset.touched = '1';   // 手动动过 → 出结果不再自动展开/自动收起(尊重用户摆放)
        _cardForm(el, _cardForm(el) === 'dot' ? 'min' : (_cardForm(el) === 'min' ? 'full' : 'dot'));
      });
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
      if (isHtml) { _bd.innerHTML = text; _bd.style.whiteSpace = 'normal'; }
      else if (window.RC && RC.assistant && RC.assistant.renderMd) { RC.assistant.renderMd(_bd, text, true); _bd.style.whiteSpace = 'normal'; }
      else _bd.textContent = text;
    } catch (e) { _bd.textContent = String(text); }
    var c = { el: el, t: null, free: false, dx: 0, dy: 0, label: kindLabel || '文字回复', raw: text, isHtml: !!isHtml };
    // 85:卡片不可透过——事件在卡内消化,不冒泡到 document 级监听(点词/选中工具栏等都挂 document)
    ['pointerdown', 'pointerup', 'click', 'touchstart', 'touchend', 'dblclick'].forEach(function (evn) {
      el.addEventListener(evn, function (ev) { ev.stopPropagation(); });
    });

    el.querySelector('.vc-card-x').addEventListener('click', function (ev) { ev.stopPropagation(); _cardClose(c); });
    el.addEventListener('pointerdown', function () {
      if (c.t) { clearTimeout(c.t); c.t = null; }   // 碰了=在读:取消自动消失
      _cards.topZ = (_cards.topZ || 500) + 1; el.style.zIndex = String(_cards.topZ);   // 69:点击=置顶
    });
    // 72:双击=收起/展开(收起=头部+一行摘要,仍可拖可关可长按)
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
    // 69/72:按住头部拖动——物理感:阈值内粘住不动;拽过阈值"弹起"(微放大+深阴影)跟手;松手落定回弹。
    // 旧版粘滞/闪烁根因:堆叠布局的 transform 0.38s 过渡在拖动中每帧追赶——拖动期必须 transition:none
    var hd = el.querySelector('.vc-card-hd');
    _bindCardDrag(hd);
    if (opts.dot) _bindCardDrag(el.querySelector('.vc-card-dot'));   // 圆态:头部隐藏,标记自己当把手
    function _bindCardDrag(hd) {
    if (!hd) return;
    hd.style.cursor = 'grab'; hd.style.touchAction = 'none';
    hd.addEventListener('pointerdown', function (ev) {
      if (ev.target.closest('.vc-card-x')) return;
      ev.preventDefault();
      var sx = ev.clientX - c.dx, sy = ev.clientY - c.dy, moved = false;
      try { hd.setPointerCapture(ev.pointerId); } catch (e) {}
      function mv(e2) {
        var nx = e2.clientX - sx, ny = e2.clientY - sy;
        if (!moved && (Math.abs(nx - c.dx) + Math.abs(ny - c.dy)) > 6) {   // 粘滞阈值:拽过才起
          moved = true; c.free = true;
          el.style.transition = 'none';                     // 跟手期零动画(粘滞/闪烁的根治)
          el.classList.add('vc-lift');                      // 弹起态:scale 1.03+深阴影(见 CSS)
          _cardLayout();
        }
        if (moved) {
          c.dx = nx; c.dy = ny; el.style.transform = 'translate(' + c.dx + 'px,' + c.dy + 'px) scale(1.03)';
          el.dataset.dragged = '1'; el.dataset.touched = '1';   // 动过 → 出结果不自动展开;松手后的 click 不算形态切换
          _dockHint(_inDockZone(e2.clientX, e2.clientY));   // 77b:接近右下=收藏区光晕提示
        }
      }
      function up(e3) {
        hd.removeEventListener('pointermove', mv); hd.removeEventListener('pointerup', up); hd.removeEventListener('pointercancel', up);
        _dockHint(false);
        if (moved && e3 && _inDockZone(e3.clientX, e3.clientY)) { _dockAdd(c); return; }   // 77b:松手在区内=收入收藏夹
        if (moved) {   // 落定:带一点弹性回落(overshoot 曲线),像重新"粘"回桌面
          el.style.transition = 'transform .38s cubic-bezier(.34,1.56,.64,1),box-shadow .3s,opacity .32s';
          el.classList.remove('vc-lift');
          el.style.transform = 'translate(' + c.dx + 'px,' + c.dy + 'px)';
        }
      }
      hd.addEventListener('pointermove', mv); hd.addEventListener('pointerup', up); hd.addEventListener('pointercancel', up);
    });
    }
    if (opts.dot) {   // 工具卡:左上角锚定 + 交错重叠落点(不进右下堆叠,免得展开时往左上长、标记乱跑)
      c.free = true;
      var sp = _tSpot(300, 40);
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
    if (_cardHideOn()) {
      c.t = setTimeout(function () {
        if (el.classList.contains('vc-picked')) return;
        if (opts.noAuto) { if (el.dataset.touched !== '1') _cardForm(el, 'dot'); }   // 工具卡:收起成圆标记(不销毁,单击可再展开)
        else _cardClose(c);
      }, _cardSecs() * 1000);
    }
    c.cid = _cid;
    _pinReg(el, _cid);   // 登记进 cid 注册表:选中态处处同步
    return c;
  }
  // 形态读写:'dot'(圆) / 'min'(长条) / 'full'(方块)。侧栏内联卡只有 min/full 两态(用户要求:不要圆)。
  function _cardForm(el, f) {
    if (f === undefined) return el.classList.contains('vc-dot') ? 'dot' : (el.classList.contains('vc-min') ? 'min' : 'full');
    if (f === 'dot' && el.classList.contains('vc-inflow')) f = 'min';
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
  function _ttsMicGuard() {
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
  function _rtcRespCreate(src, longTool) {   // ㊿b 手动挡:按四态模式+来源选输出模态(每轮读当前档=通话中热切)
    var m = _voiceMode();
    // 61/66c:sts=全语音;half=提问语音·工具/深度文字;stt=全文字;route=语音,但**长工具结果轮程序切文字**
    // (0/4 实锤 mini 拿到资料+音频模态必念,prompt 治不了——短结果口头说,长结果它自己文字写,无截断)
    var wantAudio = (m === 'sts') || (m === 'route' && !longTool) || (m === 'half' && src === 'user');
    _rtc.turnText = !wantAudio;   // 本轮是文字输出:TTS 开关开着就流式代念
    // 64 用户拍板:全档 2048(≈100s 音频保险丝,正常轮碰不到)——不搞小预算硬截断,时长靠 prompt+route 自觉
    _dcSend({ type: 'response.create', response: { output_modalities: [wantAudio ? 'audio' : 'text'],
                                                   max_output_tokens: 2048 } });
  }
  function _rtcFlushCtx() {   // ㊵ 拉模式核心:用户开口/发文字的瞬间才注入"他正看着的位置+可见内容"(同状态去重)
    // 127:注入走**前端自己的 data channel**(浏览器→OpenAI 一跳)。绕 relay 的旧路 = OpenAI→Pi→OpenAI
    //   跨海往返,短问题时注入常赶不上 VAD 判完,模型只好凭空答(截图里"谁"那次就是)。
    try {
      var vt = _rtc.pendText || '';
      var fp = _rtc.ctxPage + ':' + vt.length + ':' + vt.slice(0, 30);
      if (fp === _rtc._sentCtxFp) return;
      _rtc._sentCtxFp = fp;
      _rtcSys('(用户此刻在第 ' + _rtc.ctxPage + ' 页/章' + (vt ? ',当前可见内容:' + vt.slice(0, 1500) : ',需要页面内容就调 read_page') +
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
  function _loadH2C() {
    if (window.html2canvas) return Promise.resolve();
    if (_h2cP) return _h2cP;
    _h2cP = new Promise(function (res, rej) {
      var s = document.createElement('script');
      s.src = '/static/pdf/html2canvas.min.js';
      s.onload = res; s.onerror = function () { _h2cP = null; rej(new Error('h2c load fail')); };
      document.head.appendChild(s);
    });
    return _h2cP;
  }
  async function _captureView() {
    try {
      await _loadH2C();
      // 60 尺寸上限(审核P1):长边≤1600px——2×DPR 整视口无上限时复杂页 base64 可超服务端消息上限,断的是整条控制 WS
      var longEdge = Math.max(window.innerWidth, window.innerHeight);
      var canvas = await window.html2canvas(document.body, {
        x: window.scrollX, y: window.scrollY,
        width: window.innerWidth, height: window.innerHeight,
        scale: Math.min(2, window.devicePixelRatio || 1, 1600 / longEdge),
        useCORS: true, logging: false, backgroundColor: '#ffffff',
        ignoreElements: function (el) {
          var id = el.id || '';
          return id === 'ep-side' || id === 'rc-vc' || id === 'vc-cap' || id === 'word-pop' || id === 'sel-toolbar';
        }
      });
      // 质量阶梯:编码后目标 ≤900KB base64(0.8→0.6→0.45),超了逐级降质而不是赌网关上限
      var b64 = '';
      var qs = [0.8, 0.6, 0.45];
      for (var qi = 0; qi < qs.length; qi++) {
        b64 = (canvas.toDataURL('image/jpeg', qs[qi]).split(',')[1]) || '';
        if (b64.length <= 900000) break;
      }
      return b64.length > 5000 ? { media_type: 'image/jpeg', b64: b64 } : null;   // 太小=截了个寂寞(空白/失败)
    } catch (e) { return null; }
  }
  try { window.RC = window.RC || {}; RC.captureView = _captureView; } catch (e) {}   // 共享截图能力:文字侧栏(rc-assistant)EPUB 笔迹场景发送前 await 一张视口截图(语音链路走 need_shot,文字链路一次性 HTTP 只能预拍)
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
      _rtc.turnTool = true;
      if (_voiceMode() !== 'route') {
        _dcSend({ type: 'conversation.item.create', item: { type: 'function_call_output', call_id: callId,
                  output: '(当前输出模式未启用文字路由:请直接口头简要回答重点;想看长文可让用户切到「路由」模式)' } });
        _rtcRespCreate('tool');
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
            try { _cardPush(full, '路由详答'); } catch (e) {}   // 65:侧栏关着时弹磨砂卡
          }
        } catch (e) { err = String(e).slice(0, 80); }
        var okR = !!full;
        onToolStatus({ status: okR ? 'done' : 'error', tool: 'route_to_text', label: '路由详答', args: args, rag: (full || err).slice(0, 400) });
        _dcSend({ type: 'conversation.item.create', item: { type: 'function_call_output', call_id: callId,
                  output: okR ? ('(文字详答已显示在用户屏幕上,本轮到此结束。内容简介:' + (_rtcTool._rbrief || full.slice(0, 200)) + '。用户下次说话时若相关直接运用;想让你看全文他会长按卡片带入。)') : ('(文字生成失败:' + err + ';请口头简要回答)') } });
        _rtcTool._rbrief = '';
        if (!okR) _rtcRespCreate('tool');   // 成功=长文已显示,不再花一轮输出音频;失败=让它口头补救
      })();
      return;
    }
    _rtc.turnTool = true;   // ㊸:本轮真调了工具(承诺核查放行)
    if (name === 'read_selection' && _rtc.sel && _rtc.sel.trim()) {   // 74:选中在手=短路闪回(fallback 路径与 relay 同构)
      onToolStatus({ status: 'done', tool: name, label: '读取选中(免调用)', rag: _rtc.sel.slice(0, 300) });
      _dcSend({ type: 'conversation.item.create', item: { type: 'function_call_output', call_id: callId,
                output: '(选中内容就在这里,无需再查:「' + _rtc.sel.slice(0, 800) + '」——直接使用)' } });
      _rtcRespCreate('tool');
      return;
    }
    _rtc.toolN = (_rtc.toolN || 0) + 1;   // ㊷ 护栏:单会话工具调用异常多=可能循环失控,提醒但不硬断
    if (_rtc.toolN === 40) { try { threadMsg('asst-note', '⚠ 本次通话工具调用已达 40 次(异常偏多)——若感觉它在兜圈子,挂断重拨或点🗑清空。'); } catch (e) {} }
    onToolStatus({ status: 'running', label: name });
    var out = '', ok = true, label = name, took = null, argsUsed = args;
    try {
      if (name === 'deep_think') {
        out = await _rtcDeep(String(args.question || ''));
        label = '深度思考';
      } else {
        var ctx = { file_rel: _rtc.ctxFile, page: _rtc.ctxPage };
        if (_rtc.imgOn) ctx._want_vision = 1;
        if (_rtc.ink && _rtc.ink.length) ctx.ink = _rtc.ink;
        if (_rtc.sel) ctx.selection = _rtc.sel;
        if (name === 'make_anki' || name === 'make_note') ctx.recent_tools = (_rtc.recentTools || []).slice(-4);   // 61b:搜过的网页/配图随卡走
        if (name === 'see_ink' || name === 'see_page') {   // ㉟c:看图类恒附视口截图(EPUB 主路/PDF 兜底,后端按需取用)
          try { var shot = await _captureView(); if (shot) ctx.view_image = shot; } catch (e) {}
        }
        var r = await fetch('/api/assistant/voice-tool', { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ cmd: JSON.stringify({ tool: name, args: args }), ctx: ctx, rtc_call_id: _rtc.callId }) });
        var d = await r.json();
        ok = !!d.ok; label = d.label || name; took = d.took_s; argsUsed = d.args || args;
        if (ok && (name === 'see_ink' || name === 'see_page' || name === 'see_figure')) _rtc.inkDirty = false;   // 重新看过了:边沿复位,下次变化再通知
        var res = d.result || {};
        _rtcTool._silent = !!res.silent && localStorage.getItem('rc-voice-toolreply') !== '1';   // 74/89:静默入库;「工具口头回报」开=放行
        var ca = res.client_action; delete res.client_action;
        try {   // 61b:最近工具结果环(搜索摘要/配图URL)——之后 make_anki/make_note 把对话现场带给制卡 AI
          var _imgs = [];
          (res.images || []).forEach(function (im) { var _u = im.image_url || im.url; if (_u) _imgs.push(_u); });
          (_rtc.recentTools = _rtc.recentTools || []).push({ tool: name, label: label, rag: JSON.stringify(res).slice(0, 600), images: _imgs.slice(0, 3) });
          if (_rtc.recentTools.length > 6) _rtc.recentTools.splice(0, _rtc.recentTools.length - 6);
        } catch (e) {}
        if (ca && ca.fn) dispatch(ca.fn, ca.args);           // 页面副作用本地直执行(比经 relay 更直接)
        delete res._vision;   // 图像由后端 sideband 注入(带 rtc_call_id 时后端已处理);绝不经 dc 发——
                              // SCTP 单条上限(Safari≈64KB),超限发送会**直接关闭 data channel**=通话哑死
        var slim = JSON.stringify(res);
        out = (slim && slim.length > 2) ? slim.slice(0, 1800) : '(无文本结果,界面元素已显示在用户屏幕上)';
      }
    } catch (e) { ok = false; out = JSON.stringify({ error: String(e).slice(0, 200) }); }
    _dcSend({ type: 'conversation.item.create', item: { type: 'function_call_output', call_id: callId, output: out } });
    if (!_rtcTool._silent) _rtcRespCreate(name === 'deep_think' ? 'deep' : 'tool', ok && String(out || '').length > 800);   // 66c/74:silent=静默入库不发言
    _rtcTool._silent = false;
    onToolStatus({ status: ok ? 'done' : 'error', tool: name, label: label, took_s: took, args: argsUsed, rag: out.slice(0, 1600) });
  }
  // rtc 字幕队列:transcript delta 是文字生成速度(1-2s 内全到),远快于声音——直接 capStream 会瞬间跳到
  // 末句,且 response.done(生成完≠播完)后提前淡出(rtc 音频在 <audio> 元素,playing 队列看不见它)。
  // 改逐句估时放出(TTS ≈6字/秒)粗对齐声音;豆包路径(550 跟随合成节奏)不走这里。
  var _rtcCap = { q: [], t: null, fed: 0, done: false };
  function _rtcCapReset() {
    _rtcCap.q = []; _rtcCap.fed = 0; _rtcCap.done = false;
    if (_rtcCap.t) { clearTimeout(_rtcCap.t); _rtcCap.t = null; }
  }
  function _rtcCapFeed(full, isDone) {
    if (isDone) _rtcCap.done = true;
    var re = /[^。！？!?;；\n]+[。！？!?;；\n]*/g, parts = [], m;
    while ((m = re.exec(full))) { if (m[0].trim()) parts.push(m[0].trim()); }
    var closed = /[。！？!?;；\n]\s*$/.test(full);
    var upto = (isDone || closed) ? parts.length : parts.length - 1;   // 末句残缺就等它闭合,别闪半句
    for (var i = _rtcCap.fed; i < upto; i++) _rtcCap.q.push(parts[i]);
    if (upto > _rtcCap.fed) _rtcCap.fed = upto;
    _rtcCapPump();
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
    if (!urgent && Date.now() - (_rtc.lastCompact || 0) < 90000) return;   // 低频保护(cookbook:低频批量>高频零碎;紧急线豁免)
    if (_rtc.items.length < 12) return;
    _rtc.lastCompact = Date.now();
    try {
      var r = await (await fetch('/api/assistant/compact-history', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file: _rtc.ctxFile || '', force: 1 }) })).json();
      var summary = (r && r.summary) || '';
      var keep = 8;   // 尾部近几轮 item 保留原文(含最近的 page/state system)
      var del = _rtc.items.slice(0, -keep);
      _rtc.items = _rtc.items.slice(-keep);
      if (_rtc.sumId) del.push({ id: _rtc.sumId });   // 125b:上一个摘要也删(它在头部,不在 items 账本里)
      // 125b(cookbook 原文核实):官方时序=**先插摘要、后删旧轮**(无"旧轮已删摘要未插"的空窗;
      // delete 定点删与 create 插 root 无冲突,官方范例不等 deleted 确认)。摘要固定 id(不入 items 账本,免排序错位)。
      if (summary) {
        _rtc.sumId = 'sum_' + Date.now().toString(36);
        _dcSend({ type: 'conversation.item.create', previous_item_id: 'root', item: {
          id: _rtc.sumId, type: 'message', role: 'system',
          content: [{ type: 'input_text', text: '(更早的对话已压缩为摘要:' + summary.slice(0, 1500) + '\n——以此为背景延续对话;状态记录,不要回应本条。)' }] } });
      }
      del.forEach(function (it) { _dcSend({ type: 'conversation.item.delete', item_id: it.id }); });
      _rtc._pageFp = ''; _rtc._inkFp = '';   // 旧 page/ink 注入可能被删:指纹作废,2s 轮询会把当前页/笔迹状态重推
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
      if (!_rtc.turnText && !_rec.mr) _recStart();   // 82:音频轮的首个 delta=确认真在出声,此刻开录(文字轮永不误录)
      if (!_rtc.turnText) {   // 67:估计 2.1 音频播放结束时刻(转写字数≈5.5字/秒+缓冲)——TTS 代念等它说完再开口,不叠音
        if (!_rtc.aStart) _rtc.aStart = Date.now();
        _rtc.aEnd = _rtc.aStart + curAText.length / 5.5 * 1000 + 800;
      }
      curAText += (e.delta || ''); setSub('a', curAText); _rtcCapFeed(curAText, false);
      if (_rtc.turnText && _turnFeed) { try { _turnFeed(curAText, false); } catch (_) {} }   // 61:文字轮边生成边代念
    } else if (t === 'input_audio_buffer.speech_started') {
      _rtcFlushCtx();   // ㊵ 拉模式:用户开口瞬间注入最新位置/可见内容(VAD 判定说完前必然到达)
      var _clipH = _recFinish();   // 66:被打断的半截轮也把已播语音收下
      try { if (window.__asstVoiceLog && (curAText || _lastU)) { window.__asstVoiceLog(_lastU, curAText, _rtc.ctxFile, _rtc.ctxPage, _clipH ? { clip: _clipH } : null); _lastU = ''; } } catch (_) {}   // ㉛:被打断的半截轮落库
      curAText = ''; curAEl = null;
      try { window.__asstVoiceMsg && window.__asstVoiceMsg('reset'); } catch (_) {}
      _rtcCapReset(); capClear();   // 用户插话=打断:字幕清掉回"正在听"(对齐 WS 版 450 语义)
      if (_ttsOn()) { try { bargeIn(); } catch (_) {} }   // 61:抢话同时打断 TTS 代念残播
      _rtc.aEnd = 0; _rtc.aStart = 0;   // 67:打断=2.1 音频已被截,代念不用再等它
      try { window.__vcSyncNow && window.__vcSyncNow(); } catch (_) {}
    } else if (t === 'input_audio_buffer.speech_stopped') {
      // 127(用户拍板):**什么都不做**——官方自动挡(session.turn_detection.create_response=true)。
      // 旧的手动挡要么前端发 create、要么绕 Pi 的 sideband 发,都是白等一个往返;第一句最明显。
      // 本轮模态由会话级 output_modalities 决定(四态档,热切时 session.update)。
    } else if (t === 'conversation.item.input_audio_transcription.completed') {
      var tx = (e.transcript || '').trim();
      if (tx && (tx.indexOf('学习伴读通话') >= 0 || tx.indexOf('常说:这一页') >= 0)) tx = '';   // 85:转写 prompt 泄漏(静音时模型复读语境提示词)→丢弃
      if (tx) {
        _lastU = tx;
        setSub('u', tx);
        if (!_rtcCap.t && !_rtcCap.q.length) capUser(tx);   // whisper 迟到:AI 字幕在放就别插队打乱滚动(对话窗已有)
      }
    } else if (t === 'response.function_call_arguments.done') {
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
      if (e.name) _rtcTool(e.name, (a && typeof a === 'object') ? a : {}, e.call_id || '');
    } else if (t === 'response.created') {
      curAText = ''; curAEl = null;   // 每个 response 独立气泡(text 输入触发的响应没有 speech_started,不重置会续写上一轮)
      _rtc.aStart = 0;   // 67:新轮重置音频起点(aEnd 保留——文字轮要等上一音频轮播完才代念)
      try { if (_cap.cur) _cap.cur.classList.remove('vc-cap-route'); } catch (_) {}   // 64:路由字幕样式不残留到普通轮
      _recAbort();   // 82:created 时 turnText 还是上一轮旧值(66c delta 驱动的缝隙)——不再赌预期,等首个音频 delta 定性再开录
      _turnFeed = _mkTtsFeeder();     // 61:新回复轮=新代念流(TTS 开关开且本轮文字输出时工作)
      _rtc.turnTool = false;          // ㊸ 承诺核查:本轮是否真调过工具
      try { window.__asstVoiceMsg && window.__asstVoiceMsg('reset'); } catch (_) {}
      _rtcCapReset();                 // fed 计数跟 curAText 同步归零(不清的话新一轮切句从错误偏移入队)
      callBtnSpeaking(true);
    } else if (t === 'response.done') {
      callBtnSpeaking(false);
      if (_rtc.turnText && _turnFeed && curAText) { try { _turnFeed(curAText, true); } catch (e) {} }   // 61:残句代念收尾(通道韧性+禁麦在 _speakSafe/_ttsMicGuard)
      if (_rtc.turnText && curAText) {
        try {
          var cT = _cardPush(curAText, '文字回复');
          if (cT) _pinBind(cT.el, '文字回复', (function (txt) { return function () { return txt; }; })(curAText));   // 85:浮层文字卡长按选中(此前漏绑)
          _cardPush._did = cT;
        } catch (e) {}
        try { window.__asstVoiceMsg && window.__asstVoiceMsg('a', curAText, { md: true, info: { mode: '文字回复(' + (_VM_TXT[_voiceMode()] || '') + '档)',
          tools: (_rtc.recentTools || []).slice(-3).map(function (t) { return t.label || t.tool; }),
          actions: ['deep'], voiceTab: true, note: '本轮主模型=GPT Realtime(见语音 Tab);下面是它可能调用的环节' },
          pin: { label: 'AI 回答', textFn: (function (txt) { return function () { return txt; }; })(curAText) },
          speak: true }); } catch (e) {}   // 67/77b/79/83(☆撤,+TTS念钮)
      }
      // ㊸b 承诺核查(用户设计:语音模型只是扳机、不产卡片内容——察觉"说了做卡却没调工具"时,
      // **程序直接替它把工具真调了**,种子=本轮对话上下文,后台制卡模型自己判断做什么卡;
      // 不再让语音模型多走一轮(那只是白烧一轮音频输出费),只留一条零成本 system 记录让它知道)
      if (!_rtc.turnTool && /已经?[^。]{0,14}(整理成|做成|放进|加进|做好)[^。]{0,10}(卡片|カード|笔记|ノート)|(卡片|笔记)[^。]{0,6}(已|放进后台|做好了)/.test(curAText)
          && Date.now() - (_rtc.scoldT || 0) > 60000) {
        _rtc.scoldT = Date.now();
        (function () {
          var isNote = /笔记|ノート/.test(curAText) && !/卡片|カード/.test(curAText);
          var tool = isNote ? 'make_note' : 'make_anki';
          var seed = ('(语音对话中用户请求做' + (isNote ? '笔记' : '卡片') + ')\n用户说:' + (_lastU || '(语音请求)') +
                      '\nAI 口头总结的要点:' + curAText).slice(0, 1600);
          onToolStatus({ status: 'running', label: (isNote ? '记笔记' : '做卡片') + '(系统代提交)' });
          fetch('/api/assistant/voice-tool', { method: 'POST', headers: { 'Content-Type': 'application/json' },
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
      var _clip0 = _rec.mr ? _recFinish() : '';   // 66/82:有录音器在跑才收(启停已由实际模态驱动)
      try { if (window.__asstVoiceLog) { window.__asstVoiceLog(_lastU, curAText, _rtc.ctxFile, _rtc.ctxPage, _clip0 ? { clip: _clip0 } : null); _lastU = ''; } } catch (_) {}   // ㉛:轮次落库(+66 语音)
      _rtcCapFeed(curAText, true);    // 残句入队;淡出由队列放完时收尾(_capMaybeHide),不在这直接藏
      try { var u = e.response && e.response.usage;
            if (u) {
              u._model = _rtc.model || 'mini';   // ㊶:记账按模型选价表
              fetch('/api/assistant/rtc-usage', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(u), keepalive: true });
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
    try {
      var proto0 = location.protocol === 'https:' ? 'wss://' : 'ws://';
      // fe=2 版本握手(59):声明"本前端有 P2 分工逻辑",relay 才接管工具;旧页面 JS 不带此参数
      // → relay 退回 P1 观察,防新旧换代窗口双执行(同一工具前端+relay 各跑一遍+create 撞车)
      var cw = new WebSocket(proto0 + location.host + '/voice-rt?mode=rtc&fe=4&call_id=' + encodeURIComponent(_rtc.callId) +
                             '&file=' + encodeURIComponent(_rtc.ctxFile) + '&page=' + (_rtc.ctxPage || 0));
      cw.onmessage = function (ev) {
        try {
          var m0 = JSON.parse(ev.data);
          if (m0.event === 'rtc_ctl') {
            _rtc.ctl = !!(m0.payload && m0.payload.ok);
            if (_rtc.ctl) {   // 122:重挂成功——重试清零+快照重推(relay 新会话不知道选中/墨迹/页码)
              _rtc.ctlRetry = 0;
              try { window.__vcSyncNow && window.__vcSyncNow(); } catch (e2) {}
            }
          }
          else if (m0.event === 'client_action') dispatch((m0.payload || {}).fn, (m0.payload || {}).args);
          else if (m0.event === 'tool_status') {
            var tp = m0.payload || {};
            _rtc.turnTool = true;   // relay 执行了工具:承诺核查放行
            // 边沿复位镜像:ctl 模式下 see_* 在 relay 跑,前端 _rtcTool 不经过——在这里复位
            if (tp.status === 'done' && /^(see_ink|see_page|see_figure)/.test(tp.tool || '')) _rtc.inkDirty = false;
            onToolStatus(tp);
          }
          else if (m0.event === 'need_shot') {   // ㊺P2:relay 执行 see_ink/see_page 要视口截图(只有浏览器能拍)
            var sid0 = (m0.payload || {}).shot_id;   // 60:带 ID 配对,防两轮工具重叠时迟到截图被下一请求接走
            _captureView().then(function (shot) {
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
              try {
                var cR = _cardPush(fullR, '路由详答');
                if (cR) _pinBind(cR.el, '路由详答', (function (txt) { return function () { return txt; }; })(fullR));   // 79:长按=全文带入
              } catch (e2) {}
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

  async function rtcStart(opts) {
    // 93(用户实测双回答根因):单飞锁——通话已在/舞步进行中,任何来路的第二次拨号直接吞。
    // 没有这把锁时,清空重拨与迟到的自动重连可各建一个 call,两个模型同时听同一句各答一条。
    if (_rtc.on || _connecting) { try { console.warn('[vc] rtcStart 被单飞锁拦下(on=' + _rtc.on + ' connecting=' + _connecting + ')'); } catch (e) {} return; }
    var g = ++_gen;
    var fresh = !!toggle._fresh; toggle._fresh = false;   // 新话题:不回放历史(WebRTC 每连接本就是新会话)
    _rtc.ctxFile = (opts && opts.file) || ''; _rtc.ctxPage = (opts && opts.page) || 0;
    _rtc.ink = null; _rtc.sel = ''; _rtc._inkFp = ''; _rtc.inkDirty = false;
    if (toggle._opts) { toggle._opts._syncedPage = 0; toggle._opts._vtFp = ''; }   // 123:同款清零(RTC 重连同风险)
    try {
      setSt('连接中(WebRTC)…');
      callBtnConnecting(true);   // 96:按下即琥珀脉冲,"确实在等它开启"
      _connecting = true;
      // ㉑铁律(WS 版 start() 同款舞步,rtcStart 之前漏了=挂断后重拨 getUserMedia 必死的根因):
      // 挂断/朗读会把 iOS 音频会话切回 'playback'(该类别**静音麦克风**)——开麦前必须先撂下朗读通道
      // 再显式声明 play-and-record,否则第一次通话能成、之后每次都"启动失败"
      try { await _ttsShutdown(); } catch (e) {}
      _audioSession('play-and-record');
      // ㊶ 指南§8.1:instructions 恒定(不含书页动态内容)→ 跨会话缓存可命中;开话视口进拉模式池,首次开口才注入
      try { _rtc.pendText = String(((window.RC && RC.adapter && RC.adapter().getContext()) || {}).visible_text || '').slice(0, 2000); } catch (e) {}
      var sres = await (await fetch('/api/assistant/rtc-session', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file: _rtc.ctxFile, page: _rtc.ctxPage }) })).json();
      if (!sres || !sres.ok) throw new Error((sres && sres.error) || 'rtc-session 失败');
      _rtc.imgOn = !!sres.rt_image;
      _rtc.model = sres.model || '';   // ㊶ 账本按模型分价表(mini vs 标准版差 6-8 倍)
      _rtc.compactTh = sres.compact_tokens || 0;   // ㊳ 会话内压缩阈值(0=关)
      _rtc.items = []; _rtc.inTok = 0; _rtc.lastCompact = 0;
      var mic = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
      if (g !== _gen) { mic.getTracks().forEach(function (t) { t.stop(); }); return; }
      var pc = new RTCPeerConnection();
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
      dc.onopen = function () { if (!fresh) _rtcInjectHistory(); };   // ㉞:非新话题=把之前的对话记录带回来
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
      var cres = await (await fetch('/api/assistant/rtc-call', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sdp: offer.sdp, session: sres.session }) })).json();
      if (!cres || !cres.ok) throw new Error((cres && cres.error) || 'SDP 代理失败');
      if (g !== _gen) { pc.close(); mic.getTracks().forEach(function (t) { t.stop(); }); return; }
      _rtc.callId = cres.call_id || '';   // sideband 注入(后端发大图)要用
      await pc.setRemoteDescription({ type: 'answer', sdp: cres.sdp });
      _rtc.pc = pc; _rtc.dc = dc; _rtc.mic = mic; _rtc.on = true;
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
      setSt('WebRTC 启动失败: ' + String(ex.name || '') + ' ' + String(ex.message || ex).slice(0, 80));
      rtcTeardown();
      ws = null; callBtnOn(false); callBtnSpeaking(false);   // 状态复位:按钮/shim 不残留假活
    }
  }
  function rtcTeardown() {
    _rtc.on = false;
    try { var _ai1 = document.getElementById('asst-input'); if (_ai1) _ai1.classList.remove('vc-live'); } catch (e) {}
    _recAbort();
    _rtcCapReset();
    try { if (_rtc.ctlWs) { _rtc.ctlWs.onclose = null; _rtc.ctlWs.close(); } } catch (e) {}
    _rtc.ctlWs = null; _rtc.ctl = false;
    try { if (_rtc.dc) _rtc.dc.close(); } catch (e) {}
    try { if (_rtc.pc) _rtc.pc.close(); } catch (e) {}
    try { if (_rtc.el) _rtc.el.remove(); } catch (e) {}
    try { if (_rtc.mic) _rtc.mic.getTracks().forEach(function (t) { t.stop(); }); } catch (e) {}
    _rtc.pc = null; _rtc.dc = null; _rtc.el = null; _rtc.mic = null; _rtc.callId = '';
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
    function _dead() { return g !== _gen; }
    function _cleanLocal() {
      try { if (myMic) myMic.getTracks().forEach(function (t) { t.stop(); }); } catch (e) {}
      try { if (myAc) myAc.close(); } catch (e) {}
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
      var proto = location.protocol === 'https:' ? 'wss://' : 'ws://';
      ws = new WebSocket(proto + location.host + '/voice-rt' + qs);
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
          if (p.status_code === '20000002') { setSt('👋 好,下次再聊'); setTimeout(function () { teardown(true); }, 2500); }   // 说"挂了吧/再见"→播完告别语自动挂断
          else _capMaybeHide(2500);   // 字幕停留几秒 → 回"正在听"等待态
        }
        else if (m.event === 450) {   // 用户开口:打断播报 + **立即同步一次上下文**(墨迹/选中,赶在模型答题前——治刚画完就问的竞态)
          _reportPlayed();   // 停播前先回报真实已播毫秒(GPT 引擎 relay 用它 truncate;豆包忽略该消息)
          _wsRecCutPend();   // 66b:上一轮若还在等队列放完 → 立即截住(别把新轮录进尾巴)
          try { if (window.__asstVoiceLog && (curAText || _lastU)) { var _cw2 = _wsRecFinish(true); window.__asstVoiceLog(_lastU, curAText, (toggle._opts || {}).file, (toggle._opts || {}).page, _cw2 ? { clip: _cw2 } : null); _lastU = ''; } else { _wsRecAbort(); } } catch (e) {}   // ㉛:被打断的半截轮也落库(下一轮覆盖前)+66b 原声(无文本轮=丢录音)
          stopPlayback(); curAText = ''; curAEl = null;
          try { window.__asstVoiceMsg && window.__asstVoiceMsg('reset'); } catch (e) {}   // ㉛:断 AI 轮气泡(下一轮开新气泡,别覆盖旧的)
          try { window.__vcSyncNow && window.__vcSyncNow(); } catch (e) {}
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

  function teardown(closeBox) {
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
    try {
      fetch('/api/assistant/compact-history', { method: 'POST', headers: { 'Content-Type': 'application/json' }, keepalive: true,
        body: JSON.stringify({ file: (toggle._opts || {}).file || '' }) }).catch(function () {});
    } catch (e) {}
    vt.sent = 0; vt.tail = ''; vt.pref = ''; pendingUtter = null;
    capClear();   // 挂断:字幕/等待指示一并收掉
    callBtnOn(false); callBtnSpeaking(false); taPlaceholder(null);
    if (box) { box.classList.remove('on'); if (closeBox) { box.remove(); box = null; } }
    try { _refreshSpeakTg(); } catch (e) {}
    _audioSession('playback');   // 挂断:会话切回纯播放(耳机路由;若视频等还在响导致这次没生效,下次 _ttsEnsure 会再声明)
    try { _ttsShutdown(); } catch (e) {}   // 朗读通道 ws+ac 一并关(通话期建的 ac 路由粘扬声器;残留 ws 会悬空收帧)→ 下次朗读重建拿干净会话
    _aecTeardown();   // AEC 环回随通话走(pc/audio 元素清干净)
  }

  // 通话引擎分流(㉚):s2s 通话按设置选 WebRTC 直连(外放无回声+全双工)或 WS relay(豆包 S2S / GPT-WS);
  // agent 模式(mic 长按 ASR)恒走豆包 relay,不受 rt_engine 影响(与 WS 版 relay 按 mode 分发同语义)
  toggle._connect = function (opts) {
    if (mode !== 's2s') { start(opts); return; }
    fetch('/api/assistant/voice-config').then(function (r) { return r.json(); }).then(function (d) {
      if ((((d || {}).cfg) || {}).rt_engine === 'openai_rtc') rtcStart(opts);
      else start(opts);
    }).catch(function () { start(opts); });
  };
  function toggle(opts) {
    injectCss();
    opts = opts || {};
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
    if (ws) { teardown(false); setSt('已挂断(再点 📞 重新通话)'); return; }
    if (mode === 'agent') {   // agent 模式无浮层:状态全靠按钮特效(绿=在听/蓝=在念)+ 输入框(转写/placeholder)
      toggle._opts = opts || {};
      taPlaceholder('连接语音…');
      start(toggle._opts);
      return;
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
    try { ws.send(JSON.stringify({ type: 'page', page: page, text: vtext ? String(vtext).slice(0, 2000) : undefined })); setSt('通话中 · 已同步到第 ' + page + ' 页'); } catch (e) {}
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

  // 侧栏「🗑 清空」→ 询问是否顺带把「回顾学习」记忆起点设为现在(v3-⑰c;对话记忆与学习档案分层:
  // 清空只清对话,时间线档案默认保留——想斩断的由这里显式确认)
  document.addEventListener('click', function (e) {
    try {
      var b = e.target && e.target.closest ? e.target.closest('[data-q="clear"]') : null;
      if (!b) return;
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
    var fp; try { fp = page + ':' + strokes.length + ':' + JSON.stringify(strokes).length; } catch (e) { fp = page + ':' + strokes.length; }
    if (fp === _inkFp) return;
    var first = (_inkFp === '');
    _inkFp = fp;
    if (first && !strokes.length) return;   // 首次空态只记指纹(没圈过东西不必更新 SP)
    // EPUB/HTML(reflow):后端拿到的是归一化 strokes,没有章节宽高无法无失真渲染合成图 → 由前端(唯一知道布局的中间层)
    // 产出笔迹合成图(视口截图)随 ink 消息发给 relay,存 book.view_shot 供 WS 引擎(豆包/Grok 不能直接看图,
    // 靠 see_ink 让视觉模型描述那张合成图)。PDF 走服务端裁图不需要;空笔迹不带 shot(relay view_shot=None 自动清陈旧)。
    try {
      var _isPdf = !!(window.RC && RC.adapter && RC.adapter().config && RC.adapter().config.isPDF);
      if (!_isPdf && strokes.length && window.RC && RC.captureView) {
        RC.captureView().then(function (shot) {
          try { ws.send(JSON.stringify({ type: 'ink', page: page, strokes: strokes.slice(0, 60), shot: (shot && shot.b64) ? { media_type: shot.media_type, b64: shot.b64 } : null })); setSt('通话中 · 已同步你的圈画'); } catch (e) {}
        }).catch(function () { try { ws.send(JSON.stringify({ type: 'ink', page: page, strokes: strokes.slice(0, 60) })); } catch (e) {} });
        return;
      }
    } catch (e) {}
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
    b.title = '豆包语音通话(S2S 专属):点=开始,再点=挂断;翻页/圈画/选中它都实时知道,说"找视频/翻到第N页"它真执行。文本对话请长按旁边的麦克风(ASR 模式)';
    b.innerHTML = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M5 5.3C5 14.8 9.2 19 18.7 19c.72 0 1.3-.58 1.3-1.3v-2.35c0-.56-.36-1.06-.9-1.23l-2.62-.87a1.3 1.3 0 0 0-1.33.32l-.95.95a11.6 11.6 0 0 1-4.72-4.72l.95-.95c.35-.35.47-.87.32-1.33L9.85 4.9A1.3 1.3 0 0 0 8.62 4H6.3C5.58 4 5 4.58 5 5.3z"/></svg>';
    var mic = document.getElementById('asst-mic');
    if (mic && mic.parentNode === input) input.insertBefore(b, mic.nextSibling);
    else input.insertBefore(b, input.firstChild);
    // 单击 = S2S 通话开关(用户裁定:不要"先开小窗再按开始"的两步;语音输入归旁边的系统听写 #asst-mic)。
    // 旧 agent 模式(豆包 ASR 转写进输入框)入口撤掉,代码保留(window._voiceCall 仍可调)。
    b.addEventListener('click', function () {
      if (ws || _reconnT || _reconnPend) {   // 通话中/重连排队中 → 挂断(开关 off)
        teardown(true);
        taPlaceholder(null);
        return;
      }
      try { navigator.vibrate && navigator.vibrate(10); } catch (e) {}
      if (window._voiceCallS2S) window._voiceCallS2S(); else toggle({ mode: 's2s' });   // 电话按钮=S2S 专属
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
  // ── 顶栏语音按钮(㉒,用户设计):侧栏收起时顶栏出 mic+电话,逻辑与侧栏完全一致——
  //    mic 单击=Apple 听写(转发给 #asst-mic 原 handler,说完自动发送)/长按=豆包 ASR;电话=S2S 开关。
  //    状态镜像:观察侧栏按钮 class(on/asr/speaking)同步变色呼吸;侧栏打开时这俩隐藏(那边有同款)。──
  function injectTopbarBtns() {
    if (document.getElementById('vc-top-mic')) return true;
    var anchor = document.getElementById('fs-toggle');
    var srcMic = document.getElementById('asst-mic'), srcCall = document.getElementById('asst-call');
    if (!anchor || !anchor.parentNode || !srcMic || !srcCall) return false;
    injectCss();
    var tm = document.createElement('button');
    tm.id = 'vc-top-mic'; tm.type = 'button';
    tm.title = '语音输入:单击=系统听写(说完自动发送);长按=豆包连续听(ASR,到点会弹紫)';
    tm.innerHTML = '<svg class="rc-tbi" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="2.5" width="6" height="11" rx="3"/><path d="M5.5 10.5v.5a6.5 6.5 0 0 0 13 0v-.5"/><path d="M12 17.5V21"/></svg>';
    var tc = document.createElement('button');
    tc.id = 'vc-top-call'; tc.type = 'button';
    tc.title = '豆包语音通话(S2S):点=开始,再点=挂断';
    tc.innerHTML = srcCall.innerHTML;   // 复用侧栏电话的 SF 线条 SVG
    anchor.parentNode.insertBefore(tm, anchor);
    anchor.parentNode.insertBefore(tc, anchor);
    tm.addEventListener('click', function () { try { srcMic.click(); } catch (e) {} });   // 单击=听写开/停(原 handler;长按后的 click 已被 _bindLongPress 吞)
    _bindLongPress(tm, _micLongAction);
    tc.addEventListener('click', function () { try { srcCall.click(); } catch (e) {} });
    function _mirror(src, dst, cls) {   // 状态镜像:侧栏按钮的状态类 → 顶栏同名类
      new MutationObserver(function () {
        cls.forEach(function (c) { dst.classList.toggle(c, src.classList.contains(c)); });
      }).observe(src, { attributes: true, attributeFilter: ['class'] });
    }
    _mirror(srcMic, tm, ['on', 'asr']);
    _mirror(srcCall, tc, ['on', 'speaking', 'connecting']);
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
      try { fetch('/api/assistant/voice-config', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ rt_tts_speak: on ? '1' : '' }) }).catch(function () {}); } catch (e) {}
    });
    qb.appendChild(tb);
    if (!document.getElementById('vc-quick-compact')) {   // 61:快捷栏整排紧凑(用户要求)
      var st0 = document.createElement('style'); st0.id = 'vc-quick-compact';
      st0.textContent = '#asst-quick .rc-media-tg,#ep-asst-quick .rc-media-tg{padding:4px 7px;font-size:12px;gap:3px;border-radius:7px}' +
        '#asst-quick .rc-media-tg svg,#ep-asst-quick .rc-media-tg svg{width:14px;height:14px;flex:none}' +
        '.vc-cap-line.vc-cap-route{box-shadow:inset 3px 0 0 rgba(157,123,255,.85),0 6px 24px rgba(0,0,0,.18);background:rgba(48,38,80,.6);color:#e9e2ff}';
      document.head.appendChild(st0);
    }
    return true;
  }

  // 侧栏 pane 注入时机不定(rc-assistant 加载在前,但保守起见轮询到出现为止)
  if (!(injectBtn() && injectSpeakToggle() && injectMicLongPress() && injectRecallChip() && injectTopbarBtns())) {
    var _tries = 0, _t = setInterval(function () { if ((injectBtn() && injectSpeakToggle() && injectMicLongPress() && injectRecallChip() && injectTopbarBtns()) || ++_tries > 40) clearInterval(_t); }, 750);
  }

  // ㉟ 共享位置/选中同步:宿主没提供 __vcSyncNow(PDF reader 的 21-misc-ai 有自己的 2s 轮询)时,
  //    共享层自建——经 RC.adapter().getContext() 拿位置(EPUB=section idx+1 当 page)与选中,变化才推。
  setInterval(function () {
    if (window.__vcSyncNow || !ws || ws.readyState !== 1) return;
    try {
      var c = (window.RC && RC.adapter && RC.adapter().getContext()) || {};
      var pg = c.page || (c.current_section_idx != null ? (c.current_section_idx + 1) : 0);
      // ㊵ 拉模式下 setPage 经 shim 只更新本地状态(零网络/token 成本),恒推保持 pendText 最新即可;
      // 豆包(真 WS,SP 前缀架构)只在翻页时推、不带视口流(滚动流会打它的 dialog 缓存)
      if (pg) setPage(pg, _rtc.on ? String(c.visible_text || '').slice(0, 2000) : undefined);
      syncState({ sel: String(c.selection || '').slice(0, 500), focus: '', figs: 0 });
    } catch (e) {}
  }, 2000);

  RC.voicecall = { toggle: toggle, isOpen: function () { return !!ws; }, setPage: setPage, syncInk: syncInk, syncState: syncState,
    // 设置面板改了语音配置 → 通知 relay 热更(S2S 通话中才有意义;relay 指纹含 tts,变了才真发 UpdateConfig)
    pushCfg: function () { try { if (ws && ws.readyState === 1 && mode === 's2s') ws.send(JSON.stringify({ type: 'cfg' })); } catch (e) {} } };
})();
