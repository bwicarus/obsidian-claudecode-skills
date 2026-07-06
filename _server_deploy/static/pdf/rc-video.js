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
      f.src = 'https://www.youtube-nocookie.com/embed/' + encodeURIComponent(v.id) + '?autoplay=1&playsinline=1&rel=0';
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
      '.rc-vid-ch{font-size:11px;color:#7c93c4;margin-top:3px}';
    (document.head || document.documentElement).appendChild(s);
  }
})();
