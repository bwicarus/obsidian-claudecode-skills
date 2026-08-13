/* rc-videoplayer.js — 统一浮动视频播放器浮层(RC.videoPlayer)
 * 侧栏视频卡 / 视频便签 点播放都开这一个浮层。半透明、可长按顶栏拖动移动、右下角缩放改大小;
 * 内含:中文字幕轨(auto/hq,跟音频外推同步)+ 控制条(起/止时间钉 ⏱、循环、倍速、字幕)+ 关闭/(便签)移除。
 * 浮层的位置+大小全局通用；App 把 /pdf/api/video-player-prefs 存入本机设备库，PWA 才使用服务端。
 * per-video 参数(起/止/循环/倍速)= 便签打开时经 onChange 回写 note.video(patchNote);侧栏视频不持久(临时)。
 * 铁律:iframe 从不 reparent/从不 innerHTML 重建(拖动改 left/top、缩放改宽、换片改 src)→ 免「reparent 强制重载丢进度」坑。
 */
(function () {
  if (!window.RC) window.RC = {};
  if (RC.videoPlayer) return;

  var PREFS_URL = '/pdf/api/video-player-prefs';
  var box = null, bar = null, iframe = null, sub = null;
  var cur = null;   // {v:{id,start,end,loop,rate,cc}, noteId, onChange, onRemove, title}
  var _prefs = { x: null, y: null, w: 380, h: null };   // w=左列(视频)宽,h=整个浮层高(自由拖拽);缺省算 16:9
  var _prefsLoaded = false;
  var _showEn = true;      // 字幕是否显示英文原文(默认中英双语)
  var _subOutside = false; // 字幕位置:false=视频内部下方(叠画面) / true=视频外部下方(独立条,不遮画面)
  // 进度(单例):infoDelivery 推 currentTime → 外推平滑
  var _vcur = 0, _vcurAt = 0, _vplaying = true, _vrate = 1;
  // 字幕
  var _sub = null, _subTimer = null, _subPoll = 0, _transCurIdx = -1, _userScrollAt = 0;

  function _now() { return (window.performance && performance.now) ? performance.now() : Date.now(); }
  function esc(s) { var d = document.createElement('div'); d.textContent = (s == null ? '' : String(s)); return d.innerHTML; }

  // ── 当前宿主的全局位置/大小（App 本机 / PWA 服务端）──
  function _loadPrefs(cb) {
    if (_prefsLoaded) { cb && cb(); return; }
    fetch(PREFS_URL).then(function (r) { return r.json(); }).then(function (d) {
      var p = (d && d.prefs) || {};
      if (typeof p.x === 'number') _prefs.x = p.x;
      if (typeof p.y === 'number') _prefs.y = p.y;
      if (typeof p.w === 'number') _prefs.w = p.w;
      if (typeof p.h === 'number') _prefs.h = p.h;
      if (typeof p.showEn === 'boolean') _showEn = p.showEn;
      if (typeof p.subOut === 'boolean') _subOutside = p.subOut;
      _prefsLoaded = true; cb && cb();
    }).catch(function () { _prefsLoaded = true; cb && cb(); });
  }
  var _saveT = null;
  function _savePrefs(patch) {
    for (var k in patch) _prefs[k] = patch[k];
    clearTimeout(_saveT);
    _saveT = setTimeout(function () {
      try { fetch(PREFS_URL, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ patch: patch }), keepalive: true }).catch(function () {}); } catch (e) {}
    }, 400);
  }

  // 显式 src 优先(调用方恒传);仅无 src 时按 bvid 兜底(BV+10=12 字符,不误判 11 位 YT id)。
  function _isBili(v) { return !!(v && (v.src ? v.src === 'bili' : /^BV[0-9A-Za-z]{10}/.test(v.id || ''))); }
  // ── embed / 进度 / 倍速(照搬 rc-stickynote + rc-video 已验证机制)──
  // B 站:官方 player.bilibili.com iframe(自带控制条)。它**不支持** YouTube 那套 postMessage(进度/倍速同步、
  //   我方字幕轨、起止钉、循环)→ open() 时给浮层加 .rcvp-bili 类,CSS 隐掉这些 YT 专属控件(靠 B 站原生控制条)。
  //   仅 start(t=秒)B 站 URL 支持,end/loop 不支持。
  function vEmbedSrc(v) {
    if (_isBili(v)) {
      var bp = ['bvid=' + encodeURIComponent(v.id), 'autoplay=1', 'danmaku=0', 'high_quality=1', 'p=1'];
      if (v.start) bp.push('t=' + Math.max(0, v.start | 0));
      return 'https://player.bilibili.com/player.html?' + bp.join('&');
    }
    var p = ['enablejsapi=1', 'playsinline=1', 'rel=0', 'autoplay=1', 'cc_lang_pref=zh-Hans', 'hl=zh-CN', 'cc_load_policy=0'];   // 我们有自己的中文字幕轨 → 不强制 YT 原生 CC(免双字幕)
    if (v.start) p.push('start=' + Math.max(0, v.start | 0));
    if (v.end) p.push('end=' + Math.max(0, v.end | 0));
    if (v.loop) { p.push('loop=1'); p.push('playlist=' + v.id); }   // 单视频循环必须 playlist=自己
    return 'https://www.youtube-nocookie.com/embed/' + encodeURIComponent(v.id) + '?' + p.join('&');
  }
  function _hook() {   // 单例 infoDelivery 监听(浮层只一个 iframe)
    if (window.__rcvpHook) return; window.__rcvpHook = 1;
    window.addEventListener('message', function (e) {
      if (!iframe || e.source !== iframe.contentWindow) return;
      if (typeof e.data !== 'string' || e.data.indexOf('"infoDelivery"') < 0) return;
      var d; try { d = JSON.parse(e.data); } catch (_) { return; }
      if (!d || d.event !== 'infoDelivery' || !d.info) return;
      var info = d.info;
      if (typeof info.currentTime === 'number') { _vcur = info.currentTime; _vcurAt = _now(); }
      if (typeof info.playerState === 'number') _vplaying = (info.playerState === 1);
      if (typeof info.playbackRate === 'number') _vrate = info.playbackRate;
    });
  }
  function _est() {   // 两次推送之间按墙钟×倍速外推 → 字幕跟音频同步
    if (_vplaying === false) return _vcur;
    var dt = (_now() - _vcurAt) / 1000;
    if (dt < 0 || dt > 2) return _vcur;
    return _vcur + dt * (_vrate || 1);
  }
  function _setRate(r) {
    if (!iframe || !iframe.contentWindow) return;
    [200, 700, 1500].forEach(function (dl) { setTimeout(function () { try { iframe.contentWindow.postMessage(JSON.stringify({ event: 'command', func: 'setPlaybackRate', args: [r] }), '*'); } catch (e) {} }, dl); });
  }
  function _reload() {   // 起/止/循环变了 → 重建 src(reload;进度会从新 start 开始,符合钉的语义)
    if (!iframe || !cur) return;
    iframe.src = vEmbedSrc(cur.v);
    _vcur = cur.v.start || 0; _vcurAt = _now();
  }
  function _persist() { try { if (cur && cur.onChange) cur.onChange({ id: cur.v.id, start: cur.v.start, end: cur.v.end, loop: cur.v.loop, rate: cur.v.rate, cc: cur.v.cc }); } catch (e) {} }

  // ── 字幕(auto/hq;端点 /pdf/api/video-subtitles,照搬 rc-video)──
  function _subStop() { if (_subTimer) { clearInterval(_subTimer); _subTimer = null; } }
  function _subLoop() {
    _subStop();
    _subTimer = setInterval(function () {
      if (!_sub || !_sub.enabled || !box) return;
      var zh = box.querySelector('.rcvp-zh'), en = box.querySelector('.rcvp-en'); if (!zh) return;
      var t = _est();
      if (typeof t !== 'number') { if (_sub.lastIdx !== -3) { zh.textContent = '▶ 播放后字幕跟随'; en.textContent = ''; _sub.lastIdx = -3; } return; }
      // 当前段 = 最后一个 start<=t 的段(等同播放器「到下一段 start 才切换」)。
      // ★ 不用 t<start+duration:YouTube 自动字幕(ASR)的 duration 严重过长→段大量重叠,按 duration 会高亮到过时的早段=完全对不上。
      var segs = _sub.segments, idx = -1;
      for (var i = 0; i < segs.length; i++) { if (segs[i].start <= t) idx = i; else break; }
      if (idx >= 0 && idx === segs.length - 1) { var _ls = segs[idx]; if (t > _ls.start + (_ls.duration || 3) + 1) idx = -1; }   // 末段播完 → 清空,不一直挂着
      if (idx === _sub.lastIdx) return; _sub.lastIdx = idx;
      if (idx >= 0) { zh.textContent = _sub.segments[idx].zh || '(未翻译)'; en.textContent = _sub.segments[idx].en || ''; }
      else { zh.textContent = ''; en.textContent = ''; }
      _hlTrans(idx);   // 字幕列表边栏同步高亮当前行 + 滚动跟随
    }, 100);
  }
  async function _toggleSub(source, force) {
    if (!box || !cur) return;
    var subBox = box.querySelector('.rcvp-sub'), zh = box.querySelector('.rcvp-zh'), en = box.querySelector('.rcvp-en');
    var ccBtn = box.querySelector('.rcvp-cc'), hqBtn = box.querySelector('.rcvp-hq');
    var actBtn = (source === 'hq') ? hqBtn : ccBtn, othBtn = (source === 'hq') ? ccBtn : hqBtn;
    if (_sub && _sub.source !== source) { _subStop(); _sub = null; othBtn && othBtn.classList.remove('on'); }
    if (_sub && _sub.enabled) { _sub.enabled = false; subBox.style.display = 'none'; _subStop(); actBtn.classList.remove('on'); return; }
    if (_sub && _sub.segments) { _sub.enabled = true; subBox.style.display = 'block'; actBtn.classList.add('on'); _subLoop(); return; }
    subBox.style.display = 'block'; actBtn.classList.add('on');
    zh.textContent = (source === 'hq') ? '⏳ 高质量字幕(英文原文 + AI 精翻;无字幕才转录,较慢)…' : '⏳ 加载字幕(YT 字幕 + 翻译,~5s)…';
    en.textContent = '';
    var myPoll = (_subPoll = _subPoll + 1), vid = cur.v.id;
    try {
      var d = null;
      for (var i = 0; i < 200; i++) {
        var fq = (force && i === 0) ? '&force=1' : '';
        var r = await fetch('/pdf/api/video-subtitles/' + encodeURIComponent(vid) + '?source=' + source + fq);
        d = await r.json();
        if (d.status !== 'running') break;
        await new Promise(function (rs) { setTimeout(rs, 3000); });
        if (_subPoll !== myPoll || !cur || cur.v.id !== vid) return;
      }
      if (!d || !d.ok || !d.segments) {
        var msg = d ? (d.error || (d.status === 'running' ? '仍在生成,稍后再点' : '?')) : '?';
        zh.textContent = '✗ 字幕失败: ' + msg + ' ';
        var retry = document.createElement('a'); retry.textContent = '重试'; retry.href = 'javascript:void(0)'; retry.style.color = '#7dd3fc';
        retry.addEventListener('click', function () { _toggleSub(source, true); });
        zh.appendChild(retry); actBtn.classList.remove('on');
        return;
      }
      _sub = { source: source, segments: d.segments, lastIdx: -2, enabled: true };
      en.textContent = d.from_cache ? ('(已缓存 · ' + source.toUpperCase() + ')') : ('(新生成 · ' + source.toUpperCase() + ',下次秒出)');
      setTimeout(function () { if (_sub && _sub.lastIdx < 0 && en) en.textContent = ''; }, 2000);
      _subLoop();
      _renderTranscript();   // 字幕到手 → 若列表边栏开着,填充全文
    } catch (e) { zh.textContent = '✗ 网络失败'; actBtn.classList.remove('on'); }
  }

  // ── CSS ──
  function _injectCss() {
    if (document.getElementById('rc-vplayer-css')) return;
    var s = document.createElement('style'); s.id = 'rc-vplayer-css';
    s.textContent =
      '#rc-vplayer{position:fixed;z-index:2147483000;pointer-events:auto;background:rgba(12,16,26,.86);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);border:1px solid #2a3a63;border-radius:12px;box-shadow:0 12px 40px rgba(0,0,0,.6);display:flex;flex-direction:column;overflow:hidden;user-select:none;-webkit-user-select:none}' +
      '.rcvp-bar{flex:0 0 auto;display:flex;align-items:center;gap:6px;padding:6px 8px;background:rgba(0,0,0,.3);cursor:grab;touch-action:none;-webkit-touch-callout:none}' +
      '.rcvp-bar.drag{cursor:grabbing}' +
      '.rcvp-grip{color:#6b7da0;font-size:14px;flex:none}' +
      '.rcvp-title{flex:1 1 auto;min-width:0;color:#cdd8f5;font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}' +
      '.rcvp-x{flex:none;background:transparent;border:none;color:#9fb4e0;font-size:16px;cursor:pointer;padding:2px 6px;line-height:1;-webkit-tap-highlight-color:transparent}' +
      '.rcvp-x:active{color:#fff}' +
      '.rcvp-stage{position:relative;width:100%;background:#000;flex:1 1 auto;min-height:0}' +   // 占左列剩余高度;视频比例由 YouTube iframe 内部 letterbox 处理(浮层可自由改宽高)
      '.rcvp-if{width:100%;height:100%;border:0;display:block}' +
      '.rcvp-sub{position:absolute;left:0;right:0;bottom:5%;padding:0 10px;text-align:center;pointer-events:none}' +
      '.rcvp-zh{color:#fff;font-size:clamp(14px,3.2vw,20px);line-height:1.35;text-shadow:0 2px 6px #000,0 0 3px #000;word-break:break-word}' +
      '.rcvp-en{color:#d3d9e0;font-size:clamp(11px,2.2vw,14px);line-height:1.3;text-shadow:0 2px 5px #000;margin-top:1px;word-break:break-word}' +
      '.rcvp-sub.rcvp-noen .rcvp-en{display:none}' +   // 原文开关:关=只中文
      // 外部字幕:视频与底部按钮之间的**固定高度**空白区,字幕在其中居中 → 内容 1 行/2 行/空都不改变布局(不抖)
      '.rcvp-sub.rcvp-out{position:static;left:auto;right:auto;bottom:auto;flex:0 0 auto;height:56px;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:2px 12px;background:transparent;border:none;overflow:hidden}' +
      '.rcvp-sub.rcvp-out .rcvp-zh{font-size:14.5px;text-shadow:none}' +
      '.rcvp-sub.rcvp-out .rcvp-en{font-size:12px;text-shadow:none;color:#9aa4af}' +
      // 控制条:Apple 简约风——透明/无重边框,hover 淡底,active=iOS 蓝 tint(而非重填充)
      '.rcvp-ctrls{flex:0 0 auto;display:flex;flex-wrap:wrap;align-items:center;gap:4px 5px;padding:7px 10px;background:transparent}' +
      /* B 站:隐掉依赖 YouTube postMessage 的控件(起止钉/倍速/循环/字幕轨/字幕列表)→ 用 B 站原生控制条。整条控制栏空了就不占位。 */
      '.rcvp-bili .rcvp-grp,.rcvp-bili .rcvp-ck,.rcvp-bili .rcvp-cc,.rcvp-bili .rcvp-hq,.rcvp-bili .rcvp-en-tg,.rcvp-bili .rcvp-pos-tg,.rcvp-bili .rcvp-list{display:none!important}' +
      '.rcvp-bili .rcvp-ctrls:empty,.rcvp-bili .rcvp-ctrls{padding:0}' +
      '.rcvp-grp{display:inline-flex;align-items:center;gap:2px;color:#8a9bb4;font-size:12px}' +
      '.rcvp-t{width:26px;background:rgba(255,255,255,.06);border:none;color:#e6eeff;border-radius:5px;padding:3px 2px;font-size:12px;text-align:center;outline:none}' +
      '.rcvp-cn{color:#5a6680}' +
      '.rcvp-now,.rcvp-btn{background:transparent;border:none;color:#9fb4e0;border-radius:7px;padding:4px 9px;font-size:12.5px;cursor:pointer;-webkit-tap-highlight-color:transparent;transition:background .12s,color .12s}' +
      '.rcvp-now:hover,.rcvp-btn:hover{background:rgba(255,255,255,.09)}' +
      '.rcvp-now:active,.rcvp-btn:active{transform:scale(.93)}' +
      '.rcvp-btn.on{background:rgba(10,132,255,.2);color:#5aa9ff}' +
      '.rcvp-sel{background:rgba(255,255,255,.06);border:none;color:#dbe7ff;border-radius:6px;padding:3px 5px;font-size:12px;outline:none}' +
      '.rcvp-ck{display:inline-flex;align-items:center;gap:4px;color:#9fb4e0;font-size:12.5px;cursor:pointer}' +
      '.rcvp-rm{color:#ff6b81}' +
      '.rcvp-rm:hover{background:rgba(255,80,100,.14)}' +
      '.rcvp-rs{position:absolute;right:0;bottom:0;width:22px;height:22px;cursor:nwse-resize;touch-action:none;z-index:5}' +
      '.rcvp-rs::before{content:"";position:absolute;right:4px;bottom:4px;width:9px;height:9px;border-right:2px solid #6b7da0;border-bottom:2px solid #6b7da0;border-bottom-right-radius:2px}' +
      // 主体两列:左=视频+控制,右=字幕列表边栏(可展开)
      '.rcvp-body{flex:1 1 auto;min-height:0;display:flex;flex-direction:row}' +   // 撑满 box 除顶栏外的高度 → 给 stage/字幕栏一个确定高度(字幕栏才能溢出滚动)
      '.rcvp-left{flex:1 1 auto;min-width:0;min-height:0;display:flex;flex-direction:column}' +
      '.rcvp-list{flex:none;background:transparent;border:none;color:#9fb4e0;font-size:14px;cursor:pointer;padding:2px 6px;-webkit-tap-highlight-color:transparent}' +
      '.rcvp-list.on{color:#5aa9ff}' +
      '.rcvp-trans{flex:none;width:240px;max-width:46vw;border-left:1px solid #2a3a63;background:rgba(0,0,0,.32);position:relative;min-height:0;overflow:hidden}' +
      // tlist 绝对铺满字幕栏(字幕栏高度=body 高度,由 box 明确高度决定)→ 根治 flex 高度歧义,内容超出就滚动
      '.rcvp-tlist{position:absolute;inset:0;overflow-y:auto;-webkit-overflow-scrolling:touch;overscroll-behavior:contain;touch-action:pan-y;padding:4px 0}' +
      '.rcvp-tempty{color:#7c93c4;font-size:12px;padding:14px 12px;line-height:1.5;text-align:center}' +
      '.rcvp-tline{display:flex;gap:7px;padding:5px 9px;cursor:pointer;border-left:2px solid transparent}' +
      '.rcvp-tline:hover{background:rgba(255,255,255,.05)}' +
      '.rcvp-tline.cur{background:rgba(59,109,181,.22);border-left-color:#7dd3fc}' +
      '.rcvp-tt{flex:none;color:#6b7da0;font-size:10.5px;font-variant-numeric:tabular-nums;padding-top:2px;min-width:30px}' +
      '.rcvp-tx{flex:1 1 auto;min-width:0}' +
      '.rcvp-tzh{display:block;color:#e6eeff;font-size:12.5px;line-height:1.35;word-break:break-word}' +
      '.rcvp-ten{display:block;color:#8a9bb4;font-size:11px;line-height:1.3;word-break:break-word;margin-top:1px}';
    document.head.appendChild(s);
  }

  // ── DOM 构建(一次)──
  function _build() {
    _injectCss();
    box = document.createElement('div'); box.id = 'rc-vplayer';
    box.innerHTML =
      '<div class="rcvp-bar"><span class="rcvp-grip">⠿</span><span class="rcvp-title"></span><button class="rcvp-list" title="字幕列表(按时间轴显示全文,点句子跳转)">📜</button><button class="rcvp-x" title="关闭">✕</button></div>' +
      '<div class="rcvp-body">' +
        '<div class="rcvp-left">' +
          '<div class="rcvp-stage"><div class="rcvp-sub" style="display:none"><div class="rcvp-zh"></div><div class="rcvp-en"></div></div></div>' +
          '<div class="rcvp-ctrls">' +
            '<span class="rcvp-grp">起<input class="rcvp-t rcvp-sm" inputmode="numeric" maxlength="3" placeholder="0"><span class="rcvp-cn">:</span><input class="rcvp-t rcvp-ss" inputmode="numeric" maxlength="2" placeholder="00"><button class="rcvp-now" data-w="start" title="设为当前播放位置">⏱</button></span>' +
            '<span class="rcvp-grp">止<input class="rcvp-t rcvp-em" inputmode="numeric" maxlength="3" placeholder="—"><span class="rcvp-cn">:</span><input class="rcvp-t rcvp-es" inputmode="numeric" maxlength="2" placeholder="00"><button class="rcvp-now" data-w="end" title="设为当前播放位置">⏱</button></span>' +
            '<span class="rcvp-grp">速<select class="rcvp-sel rcvp-rate"><option>0.5</option><option>0.75</option><option>1</option><option>1.25</option><option>1.5</option><option>2</option></select></span>' +
            '<label class="rcvp-ck"><input type="checkbox" class="rcvp-loop">循环</label>' +
            '<button class="rcvp-btn rcvp-cc" title="中文字幕(YT 字幕 + 机翻,快)">字幕</button>' +
            '<button class="rcvp-btn rcvp-hq" title="高质量中文字幕(英文原文 + AI 精翻;无字幕才转录)">精翻</button>' +
            '<button class="rcvp-btn rcvp-en-tg" title="显示/隐藏英文原文">原文</button>' +
            '<button class="rcvp-btn rcvp-pos-tg" title="字幕显示在视频内部下方 ↔ 外部下方(不遮画面)">内</button>' +
            '<button class="rcvp-btn rcvp-rm" title="从便签移除该视频" style="display:none">移除</button>' +
          '</div>' +
        '</div>' +
        '<div class="rcvp-trans" style="display:none"><div class="rcvp-tlist"></div></div>' +
      '</div>' +
      '<div class="rcvp-rs" title="拖动改大小"></div>';
    document.body.appendChild(box);
    iframe = document.createElement('iframe'); iframe.className = 'rcvp-if';
    iframe.allow = 'autoplay; encrypted-media; picture-in-picture; fullscreen'; iframe.setAttribute('allowfullscreen', '');
    iframe.addEventListener('load', function () { _setRate(cur ? (cur.v.rate || 1) : 1); try { iframe.contentWindow.postMessage('{"event":"listening"}', '*'); } catch (e) {} });
    box.querySelector('.rcvp-stage').insertBefore(iframe, box.querySelector('.rcvp-sub'));
    bar = box.querySelector('.rcvp-bar');
    box.querySelector('.rcvp-list').addEventListener('click', _transToggle);   // 📜 展开/收起字幕列表边栏
    // 用户主动滚字幕列表(滚轮/触摸/按下)→ 记时刻,暂停自动跟随;auto-scroll 用 scrollTo 不触发这些,故能区分
    var _tlEl = box.querySelector('.rcvp-tlist');
    ['wheel', 'touchmove', 'pointerdown'].forEach(function (ev) { _tlEl.addEventListener(ev, function () { _userScrollAt = _now(); }, { passive: true }); });
    try { if (window.ResizeObserver) { new ResizeObserver(function () { _syncTransHeight(); }).observe(box.querySelector('.rcvp-left')); } } catch (e) {}   // 左列(视频)高度变(缩放/换行)→ 字幕栏高度跟随锁定
    _hook();
    _wireControls();
    _wireDrag();
    _wireResize();
  }

  function _readT(mSel, sSel) { var m = parseInt(box.querySelector(mSel).value, 10) || 0, s = parseInt(box.querySelector(sSel).value, 10) || 0; return m * 60 + Math.max(0, Math.min(59, s)); }
  function _fillT(mSel, sSel, secs) { box.querySelector(mSel).value = secs ? Math.floor(secs / 60) : ''; box.querySelector(sSel).value = secs ? ('0' + (secs % 60)).slice(-2) : ''; }

  function _wireControls() {
    // 起/止:输入或 ⏱ 设当前 → 更新 v + reload + 回写便签
    var applyStart = function () { cur.v.start = _readT('.rcvp-sm', '.rcvp-ss'); _reload(); _persist(); };
    var applyEnd = function () { cur.v.end = _readT('.rcvp-em', '.rcvp-es'); _reload(); _persist(); };
    box.querySelector('.rcvp-sm').addEventListener('change', applyStart);
    box.querySelector('.rcvp-ss').addEventListener('change', applyStart);
    box.querySelector('.rcvp-em').addEventListener('change', applyEnd);
    box.querySelector('.rcvp-es').addEventListener('change', applyEnd);
    box.querySelectorAll('.rcvp-now').forEach(function (b) {
      b.addEventListener('click', function () {
        var secs = Math.max(0, Math.round(_est()));
        if (b.dataset.w === 'start') { cur.v.start = secs; _fillT('.rcvp-sm', '.rcvp-ss', secs); applyStart(); }
        else { cur.v.end = secs; _fillT('.rcvp-em', '.rcvp-es', secs); applyEnd(); }
      });
    });
    box.querySelector('.rcvp-rate').addEventListener('change', function () { cur.v.rate = parseFloat(this.value) || 1; _setRate(cur.v.rate); _persist(); });   // 倍速用 postMessage,不 reload
    box.querySelector('.rcvp-loop').addEventListener('change', function () { cur.v.loop = this.checked; _reload(); _persist(); });
    box.querySelector('.rcvp-cc').addEventListener('click', function () { _toggleSub('auto', false); });
    box.querySelector('.rcvp-hq').addEventListener('click', function () { _toggleSub('hq', false); });
    box.querySelector('.rcvp-en-tg').addEventListener('click', function () { _showEn = !_showEn; _applySubUI(); _savePrefs({ showEn: _showEn }); });
    box.querySelector('.rcvp-pos-tg').addEventListener('click', function () { _subOutside = !_subOutside; _applySubUI(); _savePrefs({ subOut: _subOutside }); });
    box.querySelector('.rcvp-rm').addEventListener('click', function () { try { cur && cur.onRemove && cur.onRemove(); } catch (e) {} close(); });
    box.querySelector('.rcvp-x').addEventListener('click', close);
  }

  // ── 拖动(顶栏按下即拖,改 left/top;松手存服务器)──
  function _clampPos() {
    var r = box.getBoundingClientRect();
    var maxX = Math.max(0, (window.innerWidth || 0) - r.width), maxY = Math.max(0, (window.innerHeight || 0) - r.height);
    _prefs.x = Math.max(0, Math.min(_prefs.x, maxX)); _prefs.y = Math.max(0, Math.min(_prefs.y, maxY));
    box.style.left = _prefs.x + 'px'; box.style.top = _prefs.y + 'px';
  }
  function _wireDrag() {
    var sx = 0, sy = 0, ox = 0, oy = 0, dragging = false;
    function down(e) {
      if (e.target.closest('.rcvp-x')) return;
      dragging = true; bar.classList.add('drag');
      sx = e.clientX; sy = e.clientY; ox = _prefs.x || 0; oy = _prefs.y || 0;
      try { bar.setPointerCapture(e.pointerId); } catch (_) {}
      document.addEventListener('pointermove', move, true);
      document.addEventListener('pointerup', up, true);
      document.addEventListener('pointercancel', up, true);
      e.preventDefault();
    }
    function move(e) {
      if (!dragging) return;
      _prefs.x = ox + (e.clientX - sx); _prefs.y = oy + (e.clientY - sy);
      _clampPos();
    }
    function up() {
      if (!dragging) return; dragging = false; bar.classList.remove('drag');
      document.removeEventListener('pointermove', move, true);
      document.removeEventListener('pointerup', up, true);
      document.removeEventListener('pointercancel', up, true);
      _savePrefs({ x: _prefs.x, y: _prefs.y });
    }
    bar.addEventListener('pointerdown', down);
  }
  // ── 字幕列表边栏(transcript):按时间轴全文 + 随播放高亮 + 点句跳转 ──
  function _mmss(s) { s = Math.max(0, s | 0); return Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2); }
  function _seek(sec) {
    try { iframe.contentWindow.postMessage(JSON.stringify({ event: 'command', func: 'seekTo', args: [sec, true] }), '*'); } catch (e) {}
    _vcur = sec; _vcurAt = _now(); _vplaying = true;   // 立即反映 → 字幕/高亮即时对齐
  }
  function _renderTranscript() {
    if (!box) return;
    var t = box.querySelector('.rcvp-trans'); if (!t || t.style.display === 'none') return;
    var list = t.querySelector('.rcvp-tlist'); if (!list) return;
    if (!_sub || !_sub.segments || !_sub.segments.length) {
      list.innerHTML = '<div class="rcvp-tempty">点 🇨🇳 字幕 / 🎯 精翻 加载字幕后,这里按时间轴显示全文(中文+原文),点句子可跳转</div>';
      _transCurIdx = -1; return;
    }
    var h = '';
    for (var i = 0; i < _sub.segments.length; i++) {
      var s = _sub.segments[i];
      h += '<div class="rcvp-tline" data-t="' + (s.start || 0) + '"><span class="rcvp-tt">' + _mmss(s.start) + '</span>' +
        '<span class="rcvp-tx"><span class="rcvp-tzh">' + esc(s.zh || '(未译)') + '</span><span class="rcvp-ten">' + esc(s.en || '') + '</span></span></div>';
    }
    list.innerHTML = h;
    Array.prototype.forEach.call(list.querySelectorAll('.rcvp-tline'), function (ln) {
      ln.addEventListener('click', function () { _seek(parseFloat(ln.getAttribute('data-t')) || 0); });
    });
    _transCurIdx = -1;
  }
  // 字幕开关:原文显隐 + 内/外位置(内=叠画面底,外=视频下方独立条)。状态存服务器全局通用。
  function _applySubUI() {
    if (!box) return;
    var sub = box.querySelector('.rcvp-sub'), stage = box.querySelector('.rcvp-stage'), left = box.querySelector('.rcvp-left'), ctrls = box.querySelector('.rcvp-ctrls');
    if (!sub) return;
    sub.classList.toggle('rcvp-noen', !_showEn);
    if (_subOutside) { sub.classList.add('rcvp-out'); if (sub.parentNode !== left) left.insertBefore(sub, ctrls); }   // 外部:移到 stage 外、ctrls 前(视频下方独立条)
    else { sub.classList.remove('rcvp-out'); if (sub.parentNode !== stage) stage.appendChild(sub); }                  // 内部:移回 stage 内(absolute 叠画面底)
    var enBtn = box.querySelector('.rcvp-en-tg'), posBtn = box.querySelector('.rcvp-pos-tg');
    if (enBtn) enBtn.classList.toggle('on', _showEn);
    if (posBtn) { posBtn.classList.toggle('on', _subOutside); posBtn.textContent = _subOutside ? '外' : '内'; }
    _syncTransHeight();   // 内/外切换改变左列高度 → 同步字幕列表高度锁
  }
  function _syncTransHeight() {}   // 浮层已有明确高度(flex 链自动:body flex:1 → trans stretch → tlist 溢出滚动),无需 JS 锁高
  function _hlTrans(idx) {   // 高亮当前段;自动把当前句滚到第一行(用户手动滚动时暂停,停手 3.5s 后恢复)
    if (!box) return;
    var t = box.querySelector('.rcvp-trans'); if (!t || t.style.display === 'none') return;
    var listEl = t.querySelector('.rcvp-tlist'); if (!listEl) return;
    var lines = listEl.querySelectorAll('.rcvp-tline'); if (!lines.length) return;
    if (_transCurIdx >= 0 && lines[_transCurIdx]) lines[_transCurIdx].classList.remove('cur');
    if (idx >= 0 && lines[idx]) {
      lines[idx].classList.add('cur');
      if (_now() - _userScrollAt > 3500) {   // 用户最近没手动滚 → 自动滚:当前句到列表顶部(第一行)
        var top0 = lines[0] ? lines[0].offsetTop : 0;
        try { listEl.scrollTo({ top: Math.max(0, lines[idx].offsetTop - top0), behavior: 'smooth' }); }
        catch (e) { listEl.scrollTop = Math.max(0, lines[idx].offsetTop - top0); }
      }
    }
    _transCurIdx = idx;
  }
  function _transToggle() {
    if (!box) return;
    var t = box.querySelector('.rcvp-trans'); if (!t) return;
    var on = (t.style.display === 'none' || !t.style.display);
    t.style.display = on ? 'block' : 'none';
    box.querySelector('.rcvp-list').classList.toggle('on', on);
    if (on) { _renderTranscript(); _syncTransHeight(); if (!_sub && cur) _toggleSub('auto', false); }   // 开列表:锁高度 + 没字幕自动拉 auto
    _clampPos();
  }

  // ── 缩放(右下角柄,自由拖:左右改左列宽 w、上下改浮层高 h;视频比例交给 YouTube iframe 自适应)──
  function _applySize() {
    var l = box.querySelector('.rcvp-left'); if (l) l.style.width = _prefs.w + 'px';
    if (typeof _prefs.h === 'number') box.style.height = _prefs.h + 'px';
  }
  function _wireResize() {
    var rs = box.querySelector('.rcvp-rs'); var sx = 0, sy = 0, w0 = 0, h0 = 0, sz = false, raf = null;
    function down(e) {
      sz = true; sx = e.clientX; sy = e.clientY; w0 = _prefs.w;
      h0 = (typeof _prefs.h === 'number') ? _prefs.h : box.getBoundingClientRect().height;
      try { rs.setPointerCapture(e.pointerId); } catch (_) {}
      document.addEventListener('pointermove', move, true);
      document.addEventListener('pointerup', up, true);
      document.addEventListener('pointercancel', up, true);
      e.preventDefault(); e.stopPropagation();
    }
    function move(e) {
      if (!sz) return;
      var vw = window.innerWidth || 400, vh = window.innerHeight || 400;
      var maxW = Math.max(240, vw * 0.98 - (_subOutside ? 0 : 0));
      _prefs.w = Math.max(240, Math.min(w0 + (e.clientX - sx), maxW));
      _prefs.h = Math.max(180, Math.min(h0 + (e.clientY - sy), vh * 0.96));
      if (!raf) raf = requestAnimationFrame(function () { raf = null; _applySize(); _clampPos(); });
    }
    function up() {
      if (!sz) return; sz = false;
      document.removeEventListener('pointermove', move, true);
      document.removeEventListener('pointerup', up, true);
      document.removeEventListener('pointercancel', up, true);
      _savePrefs({ w: _prefs.w, h: _prefs.h });
    }
    rs.addEventListener('pointerdown', down);
  }

  function _place() {   // 应用位置/大小(缺省 → 高按 16:9 算,位置右下角)
    if (typeof _prefs.h !== 'number') { _prefs.h = Math.round(_prefs.w * 9 / 16) + 96; }   // 默认高 = 视频 16:9 + 顶栏/控制条 chrome(~96);之后用户拖成什么就存什么
    _applySize();
    if (typeof _prefs.x !== 'number' || typeof _prefs.y !== 'number') {
      var r = box.getBoundingClientRect();
      _prefs.x = Math.max(8, (window.innerWidth || 400) - r.width - 14);
      _prefs.y = Math.max(8, (window.innerHeight || 400) - r.height - 84);
    }
    _clampPos();
  }

  function open(opts) {
    opts = opts || {}; if (!opts.id) return;
    if (!box) _build();
    box.style.display = 'flex';
    var _bili = _isBili({ src: opts.src, id: opts.id });
    cur = {
      v: { id: opts.id, start: opts.start | 0, end: opts.end | 0, loop: !!opts.loop, rate: parseFloat(opts.rate) || 1, cc: opts.cc, src: (_bili ? 'bili' : 'yt') },
      noteId: opts.noteId || null, onChange: opts.onChange || null, onRemove: opts.onRemove || null,
    };
    box.classList.toggle('rcvp-bili', _bili);   // B 站:CSS 隐掉 YT 专属控件(字幕/倍速/起止钉/循环/字幕列表)
    // 换视频重置字幕/进度/字幕列表
    _subStop(); _sub = null; _transCurIdx = -1; _vcur = cur.v.start || 0; _vcurAt = _now(); _vplaying = true; _vrate = cur.v.rate || 1;
    box.querySelector('.rcvp-title').textContent = opts.title || '视频';
    box.querySelector('.rcvp-sub').style.display = 'none';
    box.querySelector('.rcvp-zh').textContent = ''; box.querySelector('.rcvp-en').textContent = '';
    box.querySelector('.rcvp-cc').classList.remove('on'); box.querySelector('.rcvp-hq').classList.remove('on');
    _fillT('.rcvp-sm', '.rcvp-ss', cur.v.start); _fillT('.rcvp-em', '.rcvp-es', cur.v.end);
    box.querySelector('.rcvp-rate').value = String(cur.v.rate || 1);
    box.querySelector('.rcvp-loop').checked = !!cur.v.loop;
    box.querySelector('.rcvp-rm').style.display = cur.noteId ? '' : 'none';   // 移除视频仅便签来源
    iframe.src = vEmbedSrc(cur.v);
    if (_bili) {
      // B 站:字幕/字幕列表是 YouTube-only(靠 YT postMessage + /api/video-subtitles)。开 B站 视频时
      //   强制收起残留的字幕列表侧栏 + 字幕浮层,且**不触发** _toggleSub(避免对 bvid 发一次注定失败的 YT 字幕请求)。
      try { var _tpB = box.querySelector('.rcvp-trans'); if (_tpB) _tpB.style.display = 'none'; } catch (e) {}
      box.querySelector('.rcvp-sub').style.display = 'none';
    } else {
      _applySubUI();   // 按当前(或默认)原文/内外开关先应用一次
      try { var _tp = box.querySelector('.rcvp-trans'); if (_tp && _tp.style.display !== 'none') { _renderTranscript(); if (cur) _toggleSub('auto', false); } } catch (e) {}   // 换视频时列表开着 → 清空并重拉本视频字幕
    }
    _loadPrefs(function () { _place(); if (!_bili) _applySubUI(); });   // 服务器 prefs 到手 → 再应用一次(首次开浮层生效);B站不碰字幕 UI
  }
  function close() {
    _subStop(); _sub = null;
    if (iframe) iframe.src = 'about:blank';   // 停播(不 reparent)
    if (box) box.style.display = 'none';
    cur = null;
  }

  RC.videoPlayer = { open: open, close: close, isOpen: function () { return !!(box && box.style.display !== 'none' && cur); } };
})();
