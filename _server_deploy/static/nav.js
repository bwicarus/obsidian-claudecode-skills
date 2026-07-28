/* 全站通用左侧导航 (磨砂玻璃抽屉) — 由 app.py after_request 自动注入到登录后页面 */
(function () {
  if (window.__NAV_LOADED__) return;
  window.__NAV_LOADED__ = true;

  var STYLE = ''
    + '.nav-handle{'
    + 'position:fixed;top:14px;left:0;z-index:90;'
    + 'width:44px;height:56px;padding:0;'
    + 'display:flex;align-items:center;justify-content:center;'
    + 'color:rgba(255,255,255,0.85);'
    + 'background:linear-gradient(135deg,rgba(255,255,255,0.12),rgba(255,255,255,0.05)),rgba(99,102,241,0.20);'
    + 'backdrop-filter:blur(14px) saturate(150%) brightness(1.04);'
    + '-webkit-backdrop-filter:blur(14px) saturate(150%) brightness(1.04);'
    + 'border:1px solid rgba(255,255,255,0.22);border-left:none;'
    + 'border-radius:0 16px 16px 0;'
    + 'box-shadow:inset -1px 0 0 rgba(255,255,255,0.18),3px 6px 18px rgba(0,0,0,0.28);'
    + 'cursor:grab;user-select:none;touch-action:none;'
    + 'transition:left 0.4s cubic-bezier(0.4,0,0.2,1),background 0.15s,color 0.15s,box-shadow 0.15s;'
    + '}'
    + '.nav-handle:hover{'
    + 'background:linear-gradient(135deg,rgba(255,255,255,0.18),rgba(255,255,255,0.08)),rgba(99,102,241,0.32);'
    + 'color:#fff;box-shadow:inset -1px 0 0 rgba(255,255,255,0.22),4px 8px 22px rgba(0,0,0,0.34);'
    + '}'
    + '.nav-handle .grip{'
    + 'display:flex;flex-direction:column;gap:4px;align-items:center;justify-content:center;'
    + '}'
    + '.nav-handle .grip span{'
    + 'display:block;width:18px;height:2.5px;border-radius:2px;background:currentColor;opacity:0.9;'
    + '}'
    + 'body.nav-open .nav-handle{left:min(280px,72vw);}'

    + '.nav-drawer{'
    + 'position:fixed;top:0;left:0;bottom:0;width:min(280px,72vw);z-index:80;'
    + 'display:flex;flex-direction:column;padding:18px;'
    + 'background:linear-gradient(135deg,rgba(255,255,255,0.10),rgba(255,255,255,0.04)),'
    +  'linear-gradient(135deg,rgba(15,12,41,0.32),rgba(26,26,62,0.28));'
    + 'backdrop-filter:blur(16px) saturate(150%) brightness(1.04);'
    + '-webkit-backdrop-filter:blur(16px) saturate(150%) brightness(1.04);'
    + 'border-right:1px solid rgba(255,255,255,0.20);'
    + 'box-shadow:14px 0 48px rgba(0,0,0,0.40),inset -1px 0 0 rgba(255,255,255,0.16);'
    + 'transform:translateX(-102%);'
    + 'transition:transform 0.4s cubic-bezier(0.4,0,0.2,1);'
    + 'color:rgba(255,255,255,0.92);'
    + 'font-family:system-ui,-apple-system,sans-serif;'
    + '}'
    + '.nav-drawer.open{transform:translateX(0);}'

    + '.nav-user{'
    + 'display:flex;align-items:center;gap:10px;padding:6px 4px 14px;'
    + 'border-bottom:1px solid rgba(255,255,255,0.10);'
    + '}'
    + '.nav-user-link{'
    + 'flex:1;min-width:0;color:inherit;text-decoration:none;'
    + 'font-weight:700;font-size:16px;'
    + 'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;'
    + '}'
    + '.nav-user-link:hover{color:#c7d2fe;}'
    + '.nav-gear{'
    + 'flex-shrink:0;width:30px;height:30px;'
    + 'border:1px solid rgba(255,255,255,0.14);background:rgba(255,255,255,0.06);'
    + 'color:rgba(255,255,255,0.65);border-radius:8px;cursor:pointer;'
    + 'display:flex;align-items:center;justify-content:center;text-decoration:none;'
    + 'transition:background 0.15s,color 0.15s;'
    + '}'
    + '.nav-gear:hover{background:rgba(255,255,255,0.14);color:#fff;}'

    + '.nav-links{'
    + 'flex:1;padding:12px 0;overflow-y:auto;'
    + 'display:flex;flex-direction:column;gap:4px;'
    + '}'
    + '.nav-link{'
    + 'display:flex;align-items:center;gap:10px;padding:10px 12px;border-radius:8px;'
    + 'color:rgba(255,255,255,0.85);text-decoration:none;font-size:14px;'
    + 'transition:background 0.15s,color 0.15s;'
    + '}'
    + '.nav-link:hover{background:rgba(255,255,255,0.10);color:#fff;}'
    + '.nav-link.current{background:rgba(165,180,252,0.18);color:#e0e7ff;}'

    + '.nav-edit-btn{'
    + 'margin-top:8px;padding:8px 12px;'
    + 'background:rgba(255,255,255,0.04);border:1px dashed rgba(255,255,255,0.20);border-radius:8px;'
    + 'color:rgba(255,255,255,0.55);font-size:12px;cursor:pointer;'
    + 'transition:background 0.15s,color 0.15s;'
    + '}'
    + '.nav-edit-btn:hover{background:rgba(255,255,255,0.10);color:#fff;}'

    + '.nav-foot{'
    + 'padding-top:12px;margin-top:8px;'
    + 'border-top:1px solid rgba(255,255,255,0.10);'
    + 'display:flex;gap:14px;align-items:center;'
    + '}'
    + '.nav-foot a{color:rgba(255,255,255,0.55);text-decoration:none;font-size:12px;}'
    + '.nav-foot a:hover{color:#c7d2fe;}'

    + '.nav-edit-row{display:flex;gap:6px;padding:4px 0;align-items:center;}'
    + '.nav-edit-row input{'
    + 'flex:1;min-width:0;padding:6px 8px;'
    + 'background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.14);'
    + 'border-radius:6px;color:#fff;font-size:12px;outline:none;font-family:inherit;'
    + '}'
    + '.nav-edit-row input:focus{border-color:#a5b4fc;}'
    + '.nav-edit-row button{'
    + 'flex-shrink:0;padding:5px 9px;'
    + 'background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.14);'
    + 'color:rgba(255,255,255,0.65);border-radius:6px;font-size:11px;cursor:pointer;'
    + '}'
    + '.nav-edit-row button:hover{background:rgba(255,255,255,0.18);color:#fff;}'
    + '.nav-edit-row button.danger:hover{background:rgba(239,68,68,0.30);color:#fee2e2;border-color:rgba(239,68,68,0.4);}'
    ;

  var DEFAULT_LINKS = [
    { label: '主页', url: '/' },
    { label: '仪表板', url: '/dashboard/' },
    { label: '学习数据看板', url: '/insights/' },
    { label: '全文搜索', url: '/pdf/search' },
    { label: '问答历史', url: '/history/' }
  ];
  var LS_KEY = 'site.navLinks.v1';     // 离线/迁移缓存
  var API    = '/api/nav-links';

  // 链接现在持久化到服务器（per-user），localStorage 只作离线 fallback +
  // 旧用户首次访问时的一次性迁移源。
  function readCache() {
    try {
      var raw = localStorage.getItem(LS_KEY);
      if (raw) {
        var arr = JSON.parse(raw);
        if (Array.isArray(arr)) return arr;
      }
    } catch (_) {}
    return null;
  }
  function writeCache(arr) {
    try { localStorage.setItem(LS_KEY, JSON.stringify(arr)); } catch (_) {}
  }

  function loadLinks() {
    return readCache() || DEFAULT_LINKS.slice();
  }

  function fetchLinks(onUpdate) {
    fetch(API, { credentials: 'same-origin', cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (arr) {
        if (!Array.isArray(arr)) return;
        if (arr.length === 0) {
          // 服务端为空：若本地有旧 localStorage 数据，迁移到服务器；否则保持默认
          var cached = readCache();
          if (cached && cached.length) saveLinks(cached);
          return;
        }
        writeCache(arr);
        if (typeof onUpdate === 'function') onUpdate(arr);
      })
      .catch(function () { /* 离线时静默 fallback 到缓存 */ });
  }

  function saveLinks(arr) {
    writeCache(arr);
    fetch(API, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(arr)
    }).catch(function () { /* 离线时只保留 cache */ });
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function escapeAttr(s) { return String(s).replace(/"/g, '&quot;'); }

  var GEAR_SVG = ''
    + '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">'
    + '<circle cx="12" cy="12" r="3"></circle>'
    + '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"></path>'
    + '</svg>';

  function build() {
    var u = window.__USER__ || { username: '我', role: 'user' };

    var style = document.createElement('style');
    style.textContent = STYLE;
    document.head.appendChild(style);

    var drawer = document.createElement('aside');
    drawer.className = 'nav-drawer';

    var userBlock = document.createElement('div');
    userBlock.className = 'nav-user';
    userBlock.innerHTML = ''
      + '<a class="nav-user-link" href="/profile/" title="个人页">' + escapeHtml(u.username) + '</a>'
      + '<a class="nav-gear" href="/profile/" title="设置（个人页）" aria-label="设置">' + GEAR_SVG + '</a>';

    var linksList = document.createElement('div');
    linksList.className = 'nav-links';

    var editBtn = document.createElement('button');
    editBtn.type = 'button';
    editBtn.className = 'nav-edit-btn';

    var foot = document.createElement('div');
    foot.className = 'nav-foot';
    var footHtml = '';
    if (u.role === 'admin') footHtml += '<a href="/admin/">管理后台</a>';
    footHtml += '<a href="/pdf/shared-note">共享便签</a>';
    footHtml += '<a href="/logout">登出</a>';
    foot.innerHTML = footHtml;

    drawer.append(userBlock, linksList, editBtn, foot);
    document.body.appendChild(drawer);

    var handle = document.createElement('button');
    handle.type = 'button';
    handle.className = 'nav-handle';
    handle.setAttribute('aria-label', '切换导航菜单');
    handle.innerHTML = '<span class="grip"><span></span><span></span><span></span></span>';
    document.body.appendChild(handle);

    var editing = false;

    function renderLinks() {
      var links = loadLinks();
      var cur = location.pathname;
      linksList.innerHTML = '';
      if (editing) {
        links.forEach(function (lnk, idx) {
          var row = document.createElement('div');
          row.className = 'nav-edit-row';
          row.innerHTML = ''
            + '<input data-i="' + idx + '" data-k="label" value="' + escapeAttr(lnk.label) + '" placeholder="名称">'
            + '<input data-i="' + idx + '" data-k="url" value="' + escapeAttr(lnk.url) + '" placeholder="链接">'
            + '<button class="danger" data-del="' + idx + '" type="button" title="删除">×</button>';
          linksList.appendChild(row);
        });
        var addRow = document.createElement('div');
        addRow.className = 'nav-edit-row';
        addRow.innerHTML = '<button data-add type="button" style="flex:1;padding:8px;">+ 新增链接</button>';
        linksList.appendChild(addRow);
        editBtn.textContent = '完成编辑';
      } else {
        if (links.length === 0) {
          var empty = document.createElement('div');
          empty.style.cssText = 'padding:14px 12px;color:rgba(255,255,255,0.4);font-size:12px;text-align:center;';
          empty.textContent = '暂无链接，点下方按钮添加';
          linksList.appendChild(empty);
        }
        links.forEach(function (lnk) {
          var a = document.createElement('a');
          a.className = 'nav-link';
          if (cur === lnk.url || (lnk.url !== '/' && cur.indexOf(lnk.url) === 0)) {
            a.classList.add('current');
          }
          a.href = lnk.url;
          a.textContent = lnk.label;
          linksList.appendChild(a);
        });
        editBtn.textContent = '+ 编辑链接';
      }
    }

    editBtn.addEventListener('click', function () {
      if (editing) {
        // 退出编辑：把 input 当前值收集起来保存（input 事件已经在改时保存了，这里再保险一次）
        var rows = linksList.querySelectorAll('.nav-edit-row');
        var arr = [];
        rows.forEach(function (row) {
          var labelEl = row.querySelector('input[data-k="label"]');
          var urlEl = row.querySelector('input[data-k="url"]');
          if (!labelEl || !urlEl) return;
          var label = labelEl.value.trim();
          var url = urlEl.value.trim();
          if (label && url) arr.push({ label: label, url: url });
        });
        saveLinks(arr);
      }
      editing = !editing;
      renderLinks();
    });

    linksList.addEventListener('click', function (e) {
      var t = e.target;
      if (t.matches && t.matches('[data-del]')) {
        var i = +t.getAttribute('data-del');
        var arr = loadLinks();
        arr.splice(i, 1);
        saveLinks(arr);
        renderLinks();
      } else if (t.matches && t.matches('[data-add]')) {
        var arr2 = loadLinks();
        arr2.push({ label: '新链接', url: '/' });
        saveLinks(arr2);
        renderLinks();
      }
    });

    linksList.addEventListener('input', function (e) {
      var t = e.target;
      if (!t.matches || !t.matches('input[data-i]')) return;
      var i = +t.getAttribute('data-i');
      var k = t.getAttribute('data-k');
      var arr = loadLinks();
      if (arr[i]) {
        arr[i][k] = t.value;
        saveLinks(arr);
      }
    });

    function setOpen(open) {
      drawer.classList.toggle('open', open);
      document.body.classList.toggle('nav-open', open);
    }

    // ── 手柄竖向拖动调整位置（per-device 持久化 localStorage）+ 轻点=开抽屉 ──
    var HANDLE_TOP_KEY = 'nav-handle-top';
    function clampTop(t) {
      var h = handle.offsetHeight || 56;
      return Math.max(4, Math.min(t, window.innerHeight - h - 4));
    }
    (function () {                                   // 启动时恢复上次位置
      var saved = parseFloat(localStorage.getItem(HANDLE_TOP_KEY));
      if (!isNaN(saved)) handle.style.top = clampTop(saved) + 'px';
    })();
    window.addEventListener('resize', function () {  // 旋转/改窗口 → 重新夹紧防出屏
      if (handle.style.top) handle.style.top = clampTop(parseFloat(handle.style.top)) + 'px';
    });
    var drag = null, navDragged = false;
    handle.addEventListener('pointerdown', function (e) {
      drag = { startY: e.clientY, startTop: handle.getBoundingClientRect().top, moved: false };
      navDragged = false;
      handle.style.cursor = 'grabbing';
      try { handle.setPointerCapture(e.pointerId); } catch (_) {}
    });
    handle.addEventListener('pointermove', function (e) {
      if (!drag) return;
      var dy = e.clientY - drag.startY;
      if (!drag.moved && Math.abs(dy) < 6) return;   // 6px 阈值内仍当点击
      drag.moved = true; navDragged = true;
      e.preventDefault();
      handle.style.top = clampTop(drag.startTop + dy) + 'px';
    });
    function endDrag(e) {
      if (!drag) return;
      handle.style.cursor = 'grab';
      try { handle.releasePointerCapture(e.pointerId); } catch (_) {}
      if (drag.moved) localStorage.setItem(HANDLE_TOP_KEY, String(clampTop(parseFloat(handle.style.top) || drag.startTop)));
      drag = null;
    }
    handle.addEventListener('pointerup', endDrag);
    handle.addEventListener('pointercancel', endDrag);

    handle.addEventListener('click', function (e) {
      e.stopPropagation();
      if (navDragged) { navDragged = false; return; }   // 刚拖动过 → 不切换抽屉
      setOpen(!drawer.classList.contains('open'));
    });

    document.addEventListener('click', function (e) {
      if (!drawer.classList.contains('open')) return;
      if (drawer.contains(e.target) || handle.contains(e.target)) return;
      setOpen(false);
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && drawer.classList.contains('open')) setOpen(false);
    });

    renderLinks();
    // 启动时异步拉服务端 per-user 链接，到了就重渲染
    fetchLinks(function () { if (!editing) renderLinks(); });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', build);
  } else {
    build();
  }
})();
