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

  function _card(v) {
    var el = document.createElement('div'); el.className = 'rc-vid';
    el.innerHTML =
      '<div class="rc-vid-thumb"><img loading="lazy" src="' + _esc(v.thumb) + '" alt="">' +
      '<button class="rc-vid-play" aria-label="播放">▶</button></div>' +
      '<div class="rc-vid-meta"><div class="rc-vid-title">' + _esc(v.title) + '</div>' +
      '<div class="rc-vid-ch">' + _esc(v.channel) + '</div></div>';
    el.querySelector('.rc-vid-thumb').addEventListener('click', function () {
      var box = el.querySelector('.rc-vid-thumb'); if (!box || box.querySelector('iframe')) return;
      var f = document.createElement('iframe');
      // cc_lang_pref=zh-Hans:点 CC 时优先中文字幕(有中文轨/自动翻译时);hl=zh-CN:播放器 UI 中文。不加 cc_load_policy → 不强制默认开字幕,只定语言偏好。
      f.src = 'https://www.youtube-nocookie.com/embed/' + encodeURIComponent(v.id) + '?autoplay=1&playsinline=1&rel=0&cc_lang_pref=zh-Hans&hl=zh-CN';
      f.setAttribute('allow', 'autoplay; encrypted-media; picture-in-picture; fullscreen');
      f.setAttribute('allowfullscreen', '');
      f.className = 'rc-vid-frame';
      box.innerHTML = ''; box.appendChild(f);
    });
    return el;
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
  window.rcMediaBias = function () {
    var s = '';
    try {
      if (localStorage.getItem('rc-prefer-image') === '1')
        s += '\n\n[用户开启了「配图」偏好:若这个内容适合用图辅助理解,就调 search_image 配一张真实图片;不适合(抽象理论/纯推导/基础常识)别硬配。]';
      if (localStorage.getItem('rc-prefer-video') === '1')
        s += '\n\n[用户开启了「视频」偏好:若适合,就调 search_video 找一个讲解视频放进对话;不适合别硬找。]';
    } catch (e) {}
    return s;
  };
  // Apple 简约:细线条 SF 风图标(currentColor,跟顶栏一致)+ 文字标签,选中态蓝色描边
  var _MEDIA_SVG = {
    image: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4.5" width="18" height="15" rx="2.5"/><circle cx="8.5" cy="9.5" r="1.6"/><path d="M4 17l4.5-4.5 3.5 3 3-2.5 5 5"/></svg>',
    video: '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="5.5" width="18" height="13" rx="2.5"/><path d="M10 9.5l4.5 2.5L10 14.5z" fill="currentColor" stroke="none"/></svg>'
  };
  // 把两个偏好 toggle 追加进快捷按钮栏(跟「清空/模型」同一行),而非单独一行
  window.rcBuildMediaRow = function (quickBar) {
    if (!quickBar || quickBar.__mediaRow) return;
    quickBar.__mediaRow = 1;
    [['image', '配图'], ['video', '视频']].forEach(function (t) {
      var b = document.createElement('button'); b.type = 'button'; b.className = 'rc-media-tg';
      b.innerHTML = _MEDIA_SVG[t[0]] + '<span>' + t[1] + '</span>';
      b.title = '开启后 AI 回答时会倾向于用' + t[1] + '辅助解释(适合才配,不硬塞)';
      var lk = 'rc-prefer-' + t[0];
      try { if (localStorage.getItem(lk) === '1') b.classList.add('on'); } catch (e) {}
      b.addEventListener('click', function () {
        var on = b.classList.toggle('on');
        try { localStorage.setItem(lk, on ? '1' : '0'); } catch (e) {}
      });
      quickBar.appendChild(b);
    });
  };

  if (!document.getElementById('rc-video-css')) {
    var s = document.createElement('style'); s.id = 'rc-video-css';
    s.textContent =
      '.rc-vids{display:grid;grid-template-columns:1fr;gap:8px;margin:8px 0 4px;align-self:stretch}' +
      '@media(min-width:440px){.rc-vids{grid-template-columns:1fr 1fr}}' +
      '.rc-vid{background:#0d1322;border:1px solid #263255;border-radius:10px;overflow:hidden}' +
      '.rc-vid-thumb{position:relative;aspect-ratio:16/9;background:#000;cursor:pointer}' +
      '.rc-vid-thumb img{width:100%;height:100%;object-fit:cover;display:block}' +
      '.rc-vid-frame{width:100%;aspect-ratio:16/9;border:0;display:block}' +
      '.rc-vid-play{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:46px;height:46px;border-radius:50%;border:none;background:rgba(0,0,0,.55);color:#fff;font-size:17px;cursor:pointer;display:flex;align-items:center;justify-content:center;padding-left:2px}' +
      '.rc-vid-thumb:hover .rc-vid-play{background:rgba(220,40,40,.92)}' +
      '.rc-vid-meta{padding:7px 9px}' +
      '.rc-vid-title{font-size:12.5px;color:#dbe7ff;line-height:1.35;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}' +
      '.rc-vid-ch{font-size:11px;color:#7c93c4;margin-top:3px}' +
      /* 偏好 toggle:混进 quick 栏(同「模型」一行),尺寸/圆角对齐 quick button,用透明描边+选中蓝区分是开关不是即时动作 */
      '#ep-asst-quick button.rc-media-tg,#asst-quick button.rc-media-tg{display:inline-flex;align-items:center;gap:5px;font-size:13px;color:#8a9bb4;background:transparent;border:1px solid #2a3550;border-radius:8px;padding:6px 10px;cursor:pointer;-webkit-tap-highlight-color:transparent;transition:color .15s,border-color .15s,background .15s}' +
      '.rc-media-tg svg{opacity:.85}' +
      '.rc-media-tg:active{transform:scale(.96)}' +
      '#ep-asst-quick button.rc-media-tg.on,#asst-quick button.rc-media-tg.on{color:#7dd3fc;border-color:#3b6db5;background:rgba(59,109,181,.14)}' +
      '.rc-media-tg.on svg{opacity:1}';
    (document.head || document.documentElement).appendChild(s);
  }
})();
