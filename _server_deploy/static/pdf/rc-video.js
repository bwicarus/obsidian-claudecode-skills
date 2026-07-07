/* rc-video.js — 助手「搜索视频」结果卡(YouTube lite-embed:先缩略图、点击才加载 iframe 播放,
 *   YouTube 官方推荐做法:省资源、快、不预连一堆第三方)。
 * 后端 search_video 工具返回 client_action {fn:'renderVideos', args:[videos]} → 前端 window[fn].apply
 *   → 这里把可播放卡片插进对话流。PDF(.asst-msg.asst-a)/EPUB(.ep-msg.a)通用。
 * ⚠ 卡片插在助手气泡**之后**(sibling),不在气泡内——气泡在流式生成时会被 innerHTML 覆盖,插内部会被清掉。
 */
(function () {
  var _seen = {};   // 防重复渲染(actions 实时 + client_actions 收尾两条路径可能都触发同一组)

  function _hostBubble() {
    var ep = document.querySelectorAll('.ep-msg.a'); if (ep.length) return ep[ep.length - 1];
    var pf = document.querySelectorAll('.asst-msg.asst-a'); if (pf.length) return pf[pf.length - 1];
    return null;
  }
  function _esc(s) { var d = document.createElement('div'); d.textContent = (s == null ? '' : String(s)); return d.innerHTML; }

  // ── 中文字幕(YouTube 无原生中文轨时,拉英文字幕 AI 精翻 / Cloud STT 转录;后端 youtube_subtitles.py,
  //    跟健身系统共用缓存)。YouTube iframe 无法注入自定义字幕轨 → 做成视频下方字幕条,跟播放进度高亮当前段。
  //    进度用 enablejsapi=1 + postMessage「listening」→ 收 infoDelivery.currentTime(照搬 rc-stickynote 便签机制,不引入 YT IFrame API)。──
  function _hookVidCur() {   // 全局一次:infoDelivery 里的 currentTime → 存到对应卡的 el.__vcur
    if (window.__rcVidCurHook) return; window.__rcVidCurHook = 1;
    window.addEventListener('message', function (e) {
      if (typeof e.data !== 'string' || e.data.indexOf('"infoDelivery"') < 0) return;
      var d; try { d = JSON.parse(e.data); } catch (_) { return; }
      if (!d || d.event !== 'infoDelivery' || !d.info || typeof d.info.currentTime !== 'number') return;
      var cards = document.querySelectorAll('.rc-vid');
      for (var i = 0; i < cards.length; i++) { if (cards[i].__vif && cards[i].__vif.contentWindow === e.source) { cards[i].__vcur = d.info.currentTime; break; } }
    });
  }
  function _playVid(el, v) {   // 缩略图 → iframe 播放(enablejsapi=1;抽出供字幕按钮也能触发)
    var box = el.querySelector('.rc-vid-thumb'); if (!box) return null;
    var ex = box.querySelector('iframe'); if (ex) return ex;
    var f = document.createElement('iframe');
    // enablejsapi=1:postMessage 拿进度(字幕跟随);cc_lang_pref=zh-Hans/hl=zh-CN:优先中文轨 + 中文 UI
    f.src = 'https://www.youtube-nocookie.com/embed/' + encodeURIComponent(v.id) + '?enablejsapi=1&autoplay=1&playsinline=1&rel=0&cc_lang_pref=zh-Hans&hl=zh-CN';
    f.setAttribute('allow', 'autoplay; encrypted-media; picture-in-picture; fullscreen');
    f.setAttribute('allowfullscreen', ''); f.className = 'rc-vid-frame';
    box.innerHTML = ''; box.appendChild(f);
    el.__vif = f; el.__vid = v.id; _hookVidCur();
    f.addEventListener('load', function () { try { f.contentWindow.postMessage('{"event":"listening"}', '*'); } catch (e) {} });
    return f;
  }
  function _subStop(el) { if (el.__subTimer) { clearInterval(el.__subTimer); el.__subTimer = null; } }
  function _subLoop(el) {   // 200ms 读 el.__vcur → 线性查当前段 → 显示 zh/en(照搬 fitness startSubLoop)
    _subStop(el);
    el.__subTimer = setInterval(function () {
      var sub = el.__sub; if (!sub || !sub.enabled) return;
      var zhEl = el.querySelector('.rc-sub-zh'), enEl = el.querySelector('.rc-sub-en'); if (!zhEl) return;
      var t = el.__vcur;
      if (typeof t !== 'number') { if (sub.lastIdx !== -3) { zhEl.textContent = '▶ 播放后字幕跟随进度'; enEl.textContent = ''; sub.lastIdx = -3; } return; }
      var idx = -1;
      for (var i = 0; i < sub.segments.length; i++) { var s = sub.segments[i]; if (t >= s.start && t < s.start + s.duration) { idx = i; break; } if (s.start > t) break; }
      if (idx === sub.lastIdx) return;
      sub.lastIdx = idx;
      if (idx >= 0) { zhEl.textContent = sub.segments[idx].zh || '(未翻译)'; enEl.textContent = sub.segments[idx].en || ''; }
      else { zhEl.textContent = ''; enEl.textContent = ''; }
    }, 200);
  }
  async function _toggleSub(el, v, source, force) {   // 照搬 fitness toggleSubtitle(端点换 /pdf/api/video-subtitles,进度走 postMessage)
    if (!v || !v.id) return;
    var subBox = el.querySelector('.rc-vid-sub'), zhEl = el.querySelector('.rc-sub-zh'), enEl = el.querySelector('.rc-sub-en');
    var ccBtn = el.querySelector('.rc-sub-cc'), hqBtn = el.querySelector('.rc-sub-hq');
    var actBtn = (source === 'hq') ? hqBtn : ccBtn, othBtn = (source === 'hq') ? ccBtn : hqBtn;
    var sub = el.__sub;
    if (sub && sub.source !== source) { _subStop(el); el.__sub = null; sub = null; othBtn && othBtn.classList.remove('on'); }   // 换档 → 重拉
    if (sub && sub.enabled) { sub.enabled = false; subBox.style.display = 'none'; _subStop(el); actBtn.classList.remove('on'); return; }   // 同档再点 = 关
    if (sub && sub.segments) { sub.enabled = true; subBox.style.display = 'block'; actBtn.classList.add('on'); _subLoop(el); return; }   // 已拉过 → 直接重开
    if (!el.__vif) _playVid(el, v);   // 没在播放 → 先播(才有进度)
    subBox.style.display = 'block'; actBtn.classList.add('on');
    zhEl.textContent = (source === 'hq') ? '⏳ 高质量字幕(英文原文 + AI 精翻;无字幕才转录,较慢)…' : '⏳ 加载字幕(YT 字幕 + 翻译,~5s)…';
    enEl.textContent = '';
    var myPoll = (el.__subPoll = (el.__subPoll || 0) + 1);
    try {
      var d = null;
      for (var i = 0; i < 200; i++) {   // 200×3s ≈ 10min 上限(hq LLM 精翻可能慢)
        var fq = (force && i === 0) ? '&force=1' : '';
        var r = await fetch('/pdf/api/video-subtitles/' + encodeURIComponent(v.id) + '?source=' + source + fq);
        d = await r.json();
        if (d.status !== 'running') break;
        await new Promise(function (rs) { setTimeout(rs, 3000); });
        if (el.__subPoll !== myPoll) return;   // 切档/重开 → 弃旧轮询
      }
      if (!d || !d.ok || !d.segments) {
        var msg = d ? (d.error || (d.status === 'running' ? '仍在后台生成,稍后再点' : '?')) : '?';
        zhEl.textContent = '✗ 字幕失败: ' + msg + ' ';
        var retry = document.createElement('a'); retry.textContent = '重试'; retry.href = 'javascript:void(0)'; retry.style.color = '#7dd3fc';
        retry.addEventListener('click', function () { _toggleSub(el, v, source, true); });   // 重试带 force=1(负缓存出口)
        zhEl.appendChild(retry); actBtn.classList.remove('on');
        return;
      }
      el.__sub = { source: source, segments: d.segments, lastIdx: -2, enabled: true };
      enEl.textContent = d.from_cache ? ('(已缓存 · ' + source.toUpperCase() + ')') : ('(新生成 · ' + source.toUpperCase() + ',下次秒出)');
      setTimeout(function () { if (el.__sub && el.__sub.lastIdx < 0) enEl.textContent = ''; }, 2000);
      _subLoop(el);
    } catch (e) { zhEl.textContent = '✗ 网络失败'; actBtn.classList.remove('on'); }
  }

  function _card(v) {
    var el = document.createElement('div'); el.className = 'rc-vid';
    el.innerHTML =
      '<div class="rc-vid-thumb"><img loading="lazy" src="' + _esc(v.thumb) + '" alt="">' +
      '<button class="rc-vid-play" aria-label="播放">▶</button></div>' +
      '<div class="rc-vid-meta"><div class="rc-vid-title">' + _esc(v.title) + '</div>' +
      '<div class="rc-vid-ch">' + _esc(v.channel) + '</div></div>' +
      '<div class="rc-vid-subbar"><button class="rc-sub-cc" type="button" title="中文字幕(YouTube 字幕 + 机翻,快;首次 ~5s 之后秒出)">🇨🇳 字幕</button>' +
      '<button class="rc-sub-hq" type="button" title="高质量中文字幕(YouTube 英文字幕原文 + AI 精翻;无字幕才 Cloud STT 转录)">🎯 精翻</button></div>' +
      '<div class="rc-vid-sub" style="display:none"><div class="rc-sub-zh"></div><div class="rc-sub-en"></div></div>';
    el.querySelector('.rc-vid-thumb').addEventListener('click', function () { _playVid(el, v); });
    el.querySelector('.rc-sub-cc').addEventListener('click', function () { _toggleSub(el, v, 'auto', false); });
    el.querySelector('.rc-sub-hq').addEventListener('click', function () { _toggleSub(el, v, 'hq', false); });
    _bindDragToBook(el, v);   // 阶段 B:长按视频卡 → 拖到书页放置(建视频便签)
    // 阶段 D:☆ 收藏到收藏夹(第一个夹,无则建「⭐ 我的收藏」;再点取消)
    var fav = document.createElement('button'); fav.className = 'rc-vid-fav'; fav.type = 'button'; fav.innerHTML = '☆'; fav.title = '收藏这个视频到收藏夹';
    fav.addEventListener('click', function (e) { e.stopPropagation(); e.preventDefault(); _toggleFav(v, fav); });
    el.querySelector('.rc-vid-thumb').appendChild(fav);
    return el;
  }
  function _favToast(m) { try { if (window.RC && RC.toast) RC.toast(m); else if (window._toast) window._toast(m); } catch (e) {} }
  function _toggleFav(v, btn) {
    var item = { kind: 'video', vid: v.id, title: v.title || '', thumb: v.thumb || '' };
    if (btn.classList.contains('on')) {
      if (btn.__fid) fetch('/pdf/api/favorites', { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ folder: btn.__fid, remove_item: item }) })
        .then(function () { btn.classList.remove('on'); btn.innerHTML = '☆'; _favToast('已取消收藏'); }).catch(function () {});
      return;
    }
    fetch('/pdf/api/favorites').then(function (r) { return r.json(); }).then(function (d) {
      var fs = (d && d.folders) || [];
      var done = function (fid, nm) { btn.classList.add('on'); btn.innerHTML = '★'; btn.__fid = fid; _favToast('已收藏到《' + (nm || '收藏夹') + '》'); };
      if (fs.length) {
        fetch('/pdf/api/favorites', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ folder: fs[0].id, item: item }) })
          .then(function (r) { return r.json(); }).then(function () { done(fs[0].id, fs[0].name); }).catch(function () {});
      } else {
        fetch('/pdf/api/favorites', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: '⭐ 我的收藏', item: item }) })
          .then(function (r) { return r.json(); }).then(function (dd) { done((dd && (dd.folder || dd.id)) || '', '我的收藏'); }).catch(function () {});
      }
    }).catch(function () {});
  }

  // 阶段 B(手柄式,根治触摸冲突):缩略图角上一个**拖动手柄** ⠿,只在手柄上 pointerdown 才拖 → 建视频便签。
  //   避开触摸设备三大冲突:①缩略图 click=播放 ②列表滚动 ③iOS 长按图片弹「存储图像」菜单。
  //   手柄 pointerdown 立即拖(不长按计时)+ preventDefault/stopPropagation(不触发播放/菜单)+ touch-action:none。
  function _bindDragToBook(el, v) {
    var thumb = el.querySelector('.rc-vid-thumb'); if (!thumb) return;
    var handle = document.createElement('button');
    handle.className = 'rc-vid-drag'; handle.type = 'button';
    handle.title = '按住我拖到书页,放成视频便签'; handle.setAttribute('aria-label', '拖到书页');
    handle.innerHTML = '⠿';   // ⠿ 抓取点
    thumb.appendChild(handle);
    var dragging = false, ghost = null, onMove, onUp;
    var moveGhost = function (x, y) { if (ghost) { ghost.style.left = (x + 12) + 'px'; ghost.style.top = (y + 14) + 'px'; } };
    var endDrag = function (e, drop) {
      document.removeEventListener('pointermove', onMove, true);
      document.removeEventListener('pointerup', onUp, true);
      document.removeEventListener('pointercancel', onUp, true);
      if (ghost) { ghost.remove(); ghost = null; }
      if (dragging && drop && e) {
        var tgt = document.elementFromPoint(e.clientX, e.clientY);
        var inUi = tgt && tgt.closest && tgt.closest('#ep-ai, #ep-side, #asst-panel, .asst-panel, .rc-vids, #result-mask, #ep-asst-quick, #asst-quick');
        if (!inUi && window.RC && RC.stickynote && RC.stickynote.createVideoAt) RC.stickynote.createVideoAt(e.clientX, e.clientY, v.id);
      }
      dragging = false;
    };
    onMove = function (e) { if (dragging) { e.preventDefault(); moveGhost(e.clientX, e.clientY); } };
    onUp = function (e) { endDrag(e, true); };
    handle.addEventListener('pointerdown', function (e) {
      if (e.button && e.button !== 0) return;
      e.preventDefault(); e.stopPropagation();   // 手柄=明确拖动:阻止冒泡到缩略图 click(播放)+ 阻止 iOS 长按菜单
      dragging = true;
      ghost = document.createElement('div'); ghost.className = 'rc-vid-ghost'; ghost.textContent = '🎬 拖到书页放置';
      document.body.appendChild(ghost); moveGhost(e.clientX, e.clientY);
      try { navigator.vibrate && navigator.vibrate(12); } catch (_) {}
      document.addEventListener('pointermove', onMove, true);
      document.addEventListener('pointerup', onUp, true);
      document.addEventListener('pointercancel', onUp, true);
    });
    handle.addEventListener('contextmenu', function (e) { e.preventDefault(); });   // 长按不弹菜单
    handle.addEventListener('click', function (e) { e.stopPropagation(); e.preventDefault(); });   // 手柄单击不误播放
  }

  window.renderVideos = function (videos) {
    if (!videos || !videos.length) return;
    var key = videos.map(function (v) { return v && v.id; }).join(',');
    if (!key || _seen[key]) return; _seen[key] = 1;
    var host = _hostBubble(); if (!host || !host.parentNode) return;
    var wrap = document.createElement('div'); wrap.className = 'rc-vids';
    videos.forEach(function (v) { if (v && v.id) wrap.appendChild(_card(v)); });
    if (!wrap.childNodes.length) return;
    host.parentNode.insertBefore(wrap, host.nextSibling);   // 插在气泡后(sibling),不被流式 innerHTML 覆盖
    try { wrap.scrollIntoView({ block: 'nearest' }); } catch (e) {}
  };

  // ── 输入框上的「配图 / 视频」偏好开关(两个独立 toggle,状态存 localStorage 跨会话)──
  // 开启 = 倾向不强制:发给 AI 的消息附一句偏好提示(内容适合可视化时优先调 search_image/search_video,
  // 纯推导/基础常识不硬配)。气泡仍显示原问题。PDF/EPUB sendChat 发送前把 rcMediaBias() 拼到 message。
  // 偏好作为**独立字段** media_prefer 随请求发(不拼进 message → 不污染气泡/对话历史/后续上下文)。后端 _sys_prompt 注入偏好提示。
  window.rcMediaPrefer = function () {
    try { return { image: localStorage.getItem('rc-prefer-image') === '1', video: localStorage.getItem('rc-prefer-video') === '1' }; }
    catch (e) { return null; }
  };
  // Apple 简约:细线条 SF 风图标(currentColor,跟顶栏一致)+ 文字标签,选中态蓝色描边
  var _MEDIA_SVG = {
    image: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4.5" width="18" height="15" rx="2.5"/><circle cx="8.5" cy="9.5" r="1.6"/><path d="M4 17l4.5-4.5 3.5 3 3-2.5 5 5"/></svg>',
    video: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5.5" width="18" height="13" rx="2.5"/><path d="M10 9.5l4.5 2.5L10 14.5z" fill="currentColor" stroke="none"/></svg>'
  };
  _MEDIA_SVG.book = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 5.5A1.5 1.5 0 0 1 5.5 4H12v15H5.5A1.5 1.5 0 0 1 4 17.5z"/><path d="M20 5.5A1.5 1.5 0 0 0 18.5 4H12v15h6.5a1.5 1.5 0 0 0 1.5-1.5z"/></svg>';
  // 三个偏好 toggle 追加进快捷按钮栏(跟「清空/模型」同一行)。
  // 「书页」默认**点亮**(把书本上下文喂 AI);点暗 → sendChat 发 no_book,后端当通用助手答(可问跟书无关的问题)。
  // 「配图/视频」默认关(倾向不强制)。三元 [key, label, defaultOn, title]。
  window.rcBuildMediaRow = function (quickBar) {
    if (!quickBar || quickBar.__mediaRow) return;
    quickBar.__mediaRow = 1;
    [['book', '书页', true, '点亮=把当前书页内容作为上下文喂给 AI;点暗=不带书本内容,可问跟这本书无关的问题'],
     ['image', '配图', false, '开启后 AI 回答时会倾向于配图辅助解释(适合才配,不硬塞)'],
     ['video', '视频', false, '开启后 AI 回答时会倾向于配视频辅助解释(适合才配,不硬塞)']].forEach(function (t) {
      var b = document.createElement('button'); b.type = 'button'; b.className = 'rc-media-tg';
      b.innerHTML = _MEDIA_SVG[t[0]] + '<span>' + t[1] + '</span>';
      b.title = t[3];
      var lk = 'rc-prefer-' + t[0];
      try { var v = localStorage.getItem(lk); if (v === null ? t[2] : v === '1') b.classList.add('on'); } catch (e) {}
      b.addEventListener('click', function () {
        var on = b.classList.toggle('on');
        try { localStorage.setItem(lk, on ? '1' : '0'); } catch (e) {}
      });
      quickBar.appendChild(b);
    });
  };
  window.rcNoBook = function () { try { return localStorage.getItem('rc-prefer-book') === '0'; } catch (e) { return false; } };

  // 阶段D:收藏夹里的视频条目(.fav-video,收藏夹 section 走 raw 不消毒 → data-yt 保留)→ 点缩略图换 iframe 播放。
  if (!window.__rcFavVidHook) {
    window.__rcFavVidHook = 1;
    document.addEventListener('click', function (e) {
      var t = e.target && e.target.closest ? e.target.closest('.fav-video') : null;
      if (!t || t.querySelector('iframe')) return;
      var yt = t.getAttribute('data-yt'); var box = t.querySelector('.fav-video-thumb');
      if (!yt || !box) return;
      var f = document.createElement('iframe'); f.className = 'fav-video-if';
      f.src = 'https://www.youtube-nocookie.com/embed/' + encodeURIComponent(yt) + '?autoplay=1&playsinline=1&rel=0&cc_lang_pref=zh-Hans&hl=zh-CN';
      f.setAttribute('allow', 'autoplay; encrypted-media; picture-in-picture; fullscreen'); f.setAttribute('allowfullscreen', '');
      box.innerHTML = ''; box.appendChild(f);
    });
  }

  // AI 回答里的图片加载失败(常见:模型编造了不存在的图片 URL)→ 友好占位,不显示难看的破图标。
  // img 的 error 不冒泡 → 必须用 capture;只管 AI 回答容器内的图(.ep-msg.a / .asst-a / #result-content)。
  if (!window.__rcImgErrHook) {
    window.__rcImgErrHook = 1;
    document.addEventListener('error', function (e) {
      var t = e.target;
      if (!t || t.tagName !== 'IMG' || t.__brk) return;
      if (!(t.closest && t.closest('.ep-msg.a, .asst-a, #result-content'))) return;
      var src = t.getAttribute('src') || '';
      if (!t.__proxied && /wikimedia\.org|wikipedia\.org/.test(src) && src.indexOf('/api/img-proxy') < 0) {
        (window.__rcImgFail = window.__rcImgFail || {})[src] = 1;   // 记全局失败表:重渲新建的同图由 rcImgStabilize 直接换代理,不再先撞一次墙
        t.__proxied = 1; t.src = '/pdf/api/img-proxy?url=' + encodeURIComponent(src); return;   // iPad 直连维基常被挡 → 走服务器代理重试
      }
      t.__brk = 1; t.style.display = 'none';
      var s = document.createElement('span'); s.className = 'rc-img-broken'; s.textContent = '🖼 图片加载失败';
      if (t.parentNode) t.parentNode.insertBefore(s, t);
    }, true);
  }
  // 渲染时稳定图 src:全局失败表(__rcImgFail,上面错误钩子填)里的维基图直接换成代理 URL。
  // 错误钩子只救"已插入且失败的那一个 img";重渲(流式收尾/历史回放)每次都新建 img 元素,
  // 不在渲染时换 src 就会每张都先撞一次墙再走代理 → 由共享侧栏 renderMd 收尾时调用。
  window.rcImgStabilize = function (root) {
    try {
      if (!root || !root.querySelectorAll) return;
      root.querySelectorAll('img').forEach(function (im) {
        var s = im.getAttribute('src') || '';
        if (s && (window.__rcImgFail || {})[s] && s.indexOf('/api/img-proxy') < 0) {
          im.__proxied = 1; im.src = '/pdf/api/img-proxy?url=' + encodeURIComponent(s);
        }
      });
    } catch (e) {}
  };

  if (!document.getElementById('rc-video-css')) {
    var s = document.createElement('style'); s.id = 'rc-video-css';
    s.textContent =
      '.rc-vids{display:grid;grid-template-columns:1fr;gap:8px;margin:8px 0 4px;align-self:stretch}' +
      '@media(min-width:440px){.rc-vids{grid-template-columns:1fr 1fr}}' +
      '.rc-vid{background:#0d1322;border:1px solid #263255;border-radius:10px;overflow:hidden}' +
      '.rc-vid-thumb{position:relative;aspect-ratio:16/9;background:#000;cursor:pointer;-webkit-touch-callout:none}' +
      '.rc-vid-thumb img{width:100%;height:100%;object-fit:cover;display:block;-webkit-touch-callout:none;-webkit-user-select:none;user-select:none;pointer-events:none}' +
      '.rc-vid-drag{position:absolute;left:6px;bottom:6px;z-index:4;width:30px;height:30px;border-radius:8px;border:none;background:rgba(0,0,0,.6);color:#fff;font-size:16px;line-height:1;cursor:grab;display:flex;align-items:center;justify-content:center;padding:0;touch-action:none;-webkit-touch-callout:none;-webkit-user-select:none;user-select:none;-webkit-tap-highlight-color:transparent}' +
      '.rc-vid-drag:active{cursor:grabbing;background:rgba(59,109,181,.92);transform:scale(1.08)}' +
      '.rc-vid-frame{width:100%;aspect-ratio:16/9;border:0;display:block}' +
      '.rc-vid-play{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:46px;height:46px;border-radius:50%;border:none;background:rgba(0,0,0,.55);color:#fff;font-size:17px;cursor:pointer;display:flex;align-items:center;justify-content:center;padding-left:2px}' +
      '.rc-vid-thumb:hover .rc-vid-play{background:rgba(220,40,40,.92)}' +
      '.rc-vid-meta{padding:7px 9px}' +
      '.rc-vid-title{font-size:12.5px;color:#dbe7ff;line-height:1.35;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}' +
      '.rc-vid-ch{font-size:11px;color:#7c93c4;margin-top:3px}' +
      /* 中文字幕:🇨🇳字幕 / 🎯精翻 两档按钮 + 下方字幕条(跟播放进度高亮) */
      '.rc-vid-subbar{display:flex;gap:6px;padding:2px 9px 6px}' +
      '.rc-vid-subbar button{flex:0 0 auto;background:#16203a;border:1px solid #2a3a63;color:#bcd0ff;border-radius:7px;padding:3px 9px;font-size:12px;cursor:pointer;-webkit-tap-highlight-color:transparent}' +
      '.rc-vid-subbar button:active{transform:scale(.96)}' +
      '.rc-vid-subbar button.on{background:#1d3a52;border-color:#3b6db5;color:#bce0ff}' +
      '.rc-vid-sub{margin:0 9px 8px;padding:8px 11px;background:rgba(0,0,0,.45);border:1px solid #243152;border-radius:8px;text-align:center;line-height:1.45}' +
      '.rc-sub-zh{color:#fff;font-size:14.5px;min-height:19px;word-break:break-word}' +
      '.rc-sub-en{color:#9aa4af;font-size:12px;margin-top:2px;min-height:14px;word-break:break-word}' +
      /* 偏好 toggle:混进 quick 栏(同「模型」一行),尺寸/圆角对齐 quick button,用透明描边+选中蓝区分是开关不是即时动作 */
      '#ep-asst-quick button.rc-media-tg,#asst-quick button.rc-media-tg{display:inline-flex;align-items:center;gap:5px;font-size:13px;color:#8a9bb4;background:transparent;border:1px solid #2a3550;border-radius:8px;padding:6px 10px;cursor:pointer;-webkit-tap-highlight-color:transparent;transition:color .15s,border-color .15s,background .15s}' +
      '.rc-media-tg svg{opacity:.85}' +
      '.rc-media-tg:active{transform:scale(.96)}' +
      '#ep-asst-quick button.rc-media-tg.on,#asst-quick button.rc-media-tg.on{color:#7dd3fc;border-color:#3b6db5;background:rgba(59,109,181,.14)}' +
      '.rc-media-tg.on svg{opacity:1}' +
      '.rc-img-broken{font-size:12px;color:#caa;display:inline-block;padding:3px 9px;border:1px dashed rgba(200,140,140,.5);border-radius:6px;margin:2px 0}' +
      '.rc-vid-ghost{position:fixed;z-index:99999;pointer-events:none;background:#1a2540;color:#cfe6ff;border:1px solid #3b6db5;border-radius:8px;padding:6px 12px;font-size:12px;box-shadow:0 8px 24px rgba(0,0,0,.5)}' +
      '.rc-vid-fav{position:absolute;top:6px;right:6px;z-index:3;width:26px;height:26px;border-radius:50%;border:none;background:rgba(0,0,0,.55);color:#fff;font-size:14px;cursor:pointer;display:flex;align-items:center;justify-content:center;padding:0}' +
      '.rc-vid-fav.on{background:rgba(240,180,40,.92);color:#3a2a00}' +
      '.fav-video{max-width:640px;margin:10px auto}' +
      '.fav-video-thumb{position:relative;aspect-ratio:16/9;background:#000;cursor:pointer;border-radius:10px;overflow:hidden}' +
      '.fav-video-thumb img{width:100%;height:100%;object-fit:cover;display:block}' +
      '.fav-video-play{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:52px;height:52px;border-radius:50%;background:rgba(0,0,0,.55);color:#fff;font-size:19px;display:flex;align-items:center;justify-content:center}' +
      '.fav-video-thumb:hover .fav-video-play{background:rgba(220,40,40,.9)}' +
      '.fav-video-if{width:100%;aspect-ratio:16/9;border:0;display:block;border-radius:10px}' +
      '.fav-video-title{font-size:14px;margin-top:8px;text-align:center;opacity:.85}';
    (document.head || document.documentElement).appendChild(s);
  }
})();
