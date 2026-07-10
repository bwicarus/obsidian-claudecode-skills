/* rc-favorites.js — 统一控制层:⭐ 收藏夹选择弹窗(共享,PDF/EPUB 通用;零底座耦合)。
 * 设计:references/reader-userpages-favorites.md「二、收藏夹设计」阶段A。
 * RC.favorites.openPicker({file, kind:'pdf'|'epub', page|section}):
 *   弹窗列出所有收藏夹(checkbox 多选;当前页/章已在的夹打勾;勾=POST 加条目 / 取消勾=PATCH 移出,即时落盘)
 *   + 底部「+ 新建收藏夹」输入行(回车即建并自动把当前页收进去)。
 * 视觉照 rc-figures/rc-wordpop 弹窗风格(injectCss 一次;rc-fav-* 独立类名)。后端 /pdf/api/favorites。
 */
(function () {
  if (!window.RC) window.RC = {};
  if (window.RC.favorites) return;
  var EP = '/pdf/api/favorites';

  var injected = false;
  function injectCss() {
    if (injected) return; injected = true;
    var css = document.createElement('style'); css.id = 'rc-fav-css';
    css.textContent =
      '#rc-fav-mask{position:fixed;inset:0;z-index:320;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;padding:16px}' +
      '#rc-fav-pop{width:min(92vw,380px);max-height:74vh;display:flex;flex-direction:column;background:#11192c;color:#e8eeff;' +
      'border:1px solid #2a3a63;border-radius:16px;box-shadow:0 12px 40px rgba(0,0,0,.5);overflow:hidden}' +
      '#rc-fav-pop .rc-fav-h{flex:0 0 auto;display:flex;align-items:center;gap:8px;padding:12px 14px;border-bottom:1px solid #1f2b4a}' +
      '#rc-fav-pop .rc-fav-h .t{flex:1;font-size:14px;font-weight:600;color:#7fb0ff;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
      '#rc-fav-pop .rc-fav-h .x{color:#8aa;cursor:pointer;font-size:16px;line-height:1;padding:2px 4px}' +
      '#rc-fav-pop .rc-fav-sub{flex:0 0 auto;padding:6px 14px;font-size:11px;color:#8fa4cc;border-bottom:1px solid #16203a;' +
      'overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
      '#rc-fav-list{flex:1 1 auto;min-height:0;overflow-y:auto;-webkit-overflow-scrolling:touch;padding:6px 8px}' +
      '.rc-fav-row{display:flex;align-items:center;gap:10px;padding:9px 8px;border-radius:9px;cursor:pointer;-webkit-tap-highlight-color:transparent}' +
      '.rc-fav-row:hover{background:#16203a}' +
      '.rc-fav-row input[type=checkbox]{width:17px;height:17px;flex:0 0 auto;accent-color:#3b82f6;cursor:pointer;touch-action:manipulation}' +
      '.rc-fav-row .nm{flex:1;min-width:0;font-size:13.5px;color:#dbe7ff;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
      '.rc-fav-row .ct{flex:0 0 auto;font-size:11px;color:#7a8bb0}' +
      '.rc-fav-empty{color:#7c93c4;font-size:12.5px;text-align:center;padding:18px 10px}' +
      '#rc-fav-new{flex:0 0 auto;display:flex;gap:8px;padding:10px 12px;border-top:1px solid #1f2b4a}' +
      '#rc-fav-new input{flex:1;min-width:0;background:#0e1525;border:1px solid #2a3a63;color:#e6edf3;border-radius:8px;' +
      'padding:8px 10px;font-size:13px;outline:none}' +
      '#rc-fav-new input:focus{border-color:#3b6db5}' +
      '#rc-fav-new button{flex:0 0 auto;background:#1a2540;border:1px solid #3b6db5;color:#9fcbff;border-radius:8px;' +
      'padding:0 13px;font-size:13px;cursor:pointer;touch-action:manipulation}' +
      '.rc-fav-spin{display:inline-block;width:13px;height:13px;border:2px solid #2b3f6e;border-top-color:#7dd3fc;border-radius:50%;' +
      'animation:rcFavSpin .8s linear infinite;vertical-align:-2px}@keyframes rcFavSpin{to{transform:rotate(360deg)}}' +
      /* 顶栏 ⭐ 两态:未收藏=去色暗星,当前页/章已在任一收藏夹=原色亮星+微光 */
      '.rc-fav-star{filter:grayscale(1) brightness(.75);opacity:.5;transition:filter .25s,opacity .25s}' +
      '.rc-fav-star.rc-fav-on{filter:drop-shadow(0 0 5px rgba(255,205,80,.85));opacity:1}';
    document.head.appendChild(css);
  }

  function esc(s) { return (window.RC && RC.esc) ? RC.esc(s) : String(s == null ? '' : s); }
  function toast(m) { if (window.RC && RC.toast) RC.toast(m); }
  function req(method, url, body) { return RC.reqJson(method, url, body); }

  // 规整目标条目(只留 file/kind/page|section|id,别把宿主传的多余字段送进后端)
  //   userpage = 自己创建的插入页:file=该插入页所属书 rel,id=userpages 记录 id(用 id 判重)
  function normItem(t) {
    var it = { file: (t && t.file) || '', kind: (t && t.kind) || '' };
    if (it.kind === 'video') {   // YouTube / Bilibili 视频:无 file → 合成 video:<vid> 作 key(与后端 _fav_norm_item 对齐);带 vid/title/thumb/src 供 POST
      it.vid = String((t && t.vid) || '');
      it.file = 'video:' + it.vid;
      it.title = (t && t.title) || '';
      it.thumb = (t && t.thumb) || '';
      it.src = (t && t.src) || '';   // 'bili'|'yt':后端据此(或 bvid 兜底)出对应播放器/原链接
    } else if (it.kind === 'pdf') it.page = Math.max(1, parseInt((t && t.page) || 1, 10) || 1);
    else if (it.kind === 'userpage') it.id = String((t && t.id) || '');
    else it.section = Math.max(0, parseInt((t && t.section) || 0, 10) || 0);
    return it;
  }
  function sameItem(a, b) {
    return a.file === b.file && a.kind === b.kind &&
      String(a.page) === String(b.page) && String(a.section) === String(b.section) &&
      String(a.id) === String(b.id);   // userpage 用 id 判重(pdf/epub 两侧 id 都 undefined → 不影响原判重)
  }
  function hasItem(folder, it) {
    var items = (folder && folder.items) || [];
    for (var i = 0; i < items.length; i++) if (sameItem(items[i], it)) return true;
    return false;
  }
  function itemLabel(it) {
    if (it.kind === 'video') return '▶ ' + (it.title || 'YouTube 视频');
    var name = (it.file || '').split('/').pop();
    if (it.kind === 'userpage') return '📝 我的页 · ' + name;
    return name + ' · ' + (it.kind === 'pdf' ? ('第 ' + it.page + ' 页') : ('第 ' + (it.section + 1) + ' 节'));
  }

  // —— 顶栏 ⭐ 亮暗态:当前位置在任一收藏夹 → 亮。收藏夹清单缓存在 _folders(openPicker 每次 GET 也刷新它,
  //    勾选/新建的成功回调同步更新 → refreshStar),滚动经 document capture 节流刷新(两阅读器滚动容器通吃)。
  var _star = null, _folders = null, _rsPend = false;
  function _isFaved(it) {
    if (!_folders) return false;
    for (var i = 0; i < _folders.length; i++) if (hasItem(_folders[i], it)) return true;
    return false;
  }
  function refreshStar() {
    if (!_star || !_star.btn || !_star.btn.isConnected) return;
    var t = null; try { t = _star.getTarget(); } catch (e) {}
    if (!t) return;
    _star.btn.classList.toggle('rc-fav-on', _isFaved(normItem(t)));
  }
  function _rsThrottle() {
    if (_rsPend || !_star) return;
    _rsPend = true;
    setTimeout(function () { _rsPend = false; refreshStar(); }, 350);
  }
  function _reloadFolders() {
    req('GET', EP).then(function (d) {
      if (d && d.ok) { _folders = d.folders || []; refreshStar(); }
    }).catch(function () {});
  }
  function bindStar(btn, getTarget) {
    if (!btn || typeof getTarget !== 'function') return;
    // 禁自我收藏(收藏夹物化书自身):它的 file 落在 资源/收藏夹/ 前缀 → 藏 ⭐(PDF 侧由模板 _favWire 藏,
    // EPUB 侧走这里,epub-html.js 零改动即可)。openPicker 也拒该前缀,双保险。
    try { var _t0 = getTarget(); if (_t0 && typeof _t0.file === 'string' && _t0.file.indexOf('资源/收藏夹/') === 0) { btn.style.display = 'none'; return; } } catch (e) {}
    injectCss();
    _star = { btn: btn, getTarget: getTarget };
    btn.classList.add('rc-fav-star');
    _reloadFolders();
    document.addEventListener('scroll', _rsThrottle, { capture: true, passive: true });
    document.addEventListener('visibilitychange', function () { if (!document.hidden) _reloadFolders(); });  // 回前台重拉(别端可能改了收藏)
  }

  function closePicker() {
    var m = document.getElementById('rc-fav-mask');
    if (m) m.remove();
    document.removeEventListener('keydown', _esc, true);
  }
  function _esc(e) { if (e.key === 'Escape') { e.stopPropagation(); closePicker(); } }

  function openPicker(target, opts) {
    injectCss();
    var it = normItem(target);
    if (!it.file || (it.kind !== 'pdf' && it.kind !== 'epub' && it.kind !== 'userpage' && it.kind !== 'video')) { toast('无法收藏:缺文件信息'); return; }
    if (it.kind === 'video' && !it.vid) { toast('无法收藏:视频信息缺失'); return; }
    if (it.kind === 'userpage' && !it.id) { toast('这一页还没保存好,稍后再收藏'); return; }
    // 可选回调:每次勾选/取消/新建后回传「该条目当前是否已在任一夹」,供调用方(如视频卡 ☆ 按钮)同步状态。
    var _notify = function () { try { if (opts && typeof opts.onChange === 'function') opts.onChange(_isFaved(it)); } catch (_) {} };
    if (it.file.indexOf('资源/收藏夹/') === 0) { toast('收藏夹本身不能再收藏'); return; }   // 防收藏集套收藏集(规格 D)
    closePicker();
    var mask = document.createElement('div'); mask.id = 'rc-fav-mask';
    mask.addEventListener('click', function (e) { if (e.target === mask) closePicker(); });
    var pop = document.createElement('div'); pop.id = 'rc-fav-pop';
    pop.innerHTML =
      '<div class="rc-fav-h"><span class="t">⭐ 收藏到…</span><span class="x" title="关闭">✕</span></div>' +
      '<div class="rc-fav-sub">' + esc(itemLabel(it)) + '</div>' +
      '<div id="rc-fav-list"><div class="rc-fav-empty"><span class="rc-fav-spin"></span> 加载收藏夹…</div></div>' +
      '<div id="rc-fav-new"><input type="text" placeholder="+ 新建收藏夹(输入名字回车)" maxlength="80" enterkeyhint="done"><button>新建</button></div>';
    mask.appendChild(pop); document.body.appendChild(mask);
    pop.querySelector('.rc-fav-h .x').addEventListener('click', closePicker);
    document.addEventListener('keydown', _esc, true);

    var list = pop.querySelector('#rc-fav-list');

    // 一行收藏夹:checkbox(当前页已在→勾)+ 名字 + 条目数;勾/取消勾即时 POST/PATCH,失败回滚
    function renderRow(folder) {
      var row = document.createElement('div'); row.className = 'rc-fav-row';
      var cb = document.createElement('input'); cb.type = 'checkbox'; cb.checked = hasItem(folder, it);
      var nm = document.createElement('span'); nm.className = 'nm'; nm.textContent = folder.name || '(未命名)';
      var ct = document.createElement('span'); ct.className = 'ct';
      var setCt = function () { ct.textContent = ((folder.items || []).length) + ' 条'; };
      setCt();
      row.appendChild(cb); row.appendChild(nm); row.appendChild(ct);
      function apply(checked) {
        cb.disabled = true;
        var p = checked ? req('POST', EP, { folder: folder.id, item: it })
                        : req('PATCH', EP, { folder: folder.id, remove_item: it });
        p.then(function (d) {
          cb.disabled = false;
          if (d && d.ok && d.folder) { folder.items = d.folder.items || []; setCt(); cb.checked = hasItem(folder, it); refreshStar(); _notify(); }
          else { cb.checked = !checked; toast('保存失败:' + ((d && d.error) || '?')); }
        }).catch(function () { cb.disabled = false; cb.checked = !checked; toast('网络错误,没保存上'); });
      }
      cb.addEventListener('change', function () { apply(cb.checked); });
      row.addEventListener('click', function (e) {   // 整行可点(不重复触发 checkbox 自身)
        if (e.target === cb) return;
        if (cb.disabled) return;
        cb.checked = !cb.checked; apply(cb.checked);
      });
      return row;
    }

    function renderList(folders) {
      list.innerHTML = '';
      if (!folders || !folders.length) {
        list.innerHTML = '<div class="rc-fav-empty">还没有收藏夹。<br>在下方输入名字回车,新建并收藏这一页。</div>';
        return;
      }
      folders.forEach(function (f) { list.appendChild(renderRow(f)); });
    }

    req('GET', EP).then(function (d) {
      if (d && d.ok) { _folders = d.folders || []; renderList(_folders); refreshStar(); _notify(); }   // renderRow 改的就是 _folders 内对象
      else list.innerHTML = '<div class="rc-fav-empty">加载失败:' + esc((d && d.error) || '?') + '</div>';
    }).catch(function () { list.innerHTML = '<div class="rc-fav-empty">网络错误,加载失败</div>'; });

    // 新建收藏夹:回车/点「新建」即建 + 自动把当前页收进去(后端 POST {name,item} 一步到位)+ 行插到列表顶部已勾选
    var inp = pop.querySelector('#rc-fav-new input');
    var btn = pop.querySelector('#rc-fav-new button');
    function createFolder() {
      var name = (inp.value || '').trim();
      if (!name) { inp.focus(); return; }
      btn.disabled = true; inp.disabled = true;
      req('POST', EP, { name: name, item: it }).then(function (d) {
        btn.disabled = false; inp.disabled = false;
        if (d && d.ok && d.folder) {
          inp.value = '';
          var empty = list.querySelector('.rc-fav-empty'); if (empty) empty.remove();
          list.insertBefore(renderRow(d.folder), list.firstChild);
          if (_folders) _folders.unshift(d.folder);
          refreshStar(); _notify();
          toast('已建「' + name + '」并收藏');
        } else toast('新建失败:' + ((d && d.error) || '?'));
      }).catch(function () { btn.disabled = false; inp.disabled = false; toast('网络错误,没建上'); });
    }
    inp.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); createFolder(); } });
    btn.addEventListener('click', createFolder);
  }

  window.RC.favorites = {
    openPicker: openPicker,
    closePicker: closePicker,
    bindStar: bindStar,
    refreshStar: refreshStar,
    isFaved: function (t) { try { return _isFaved(normItem(t)); } catch (_) { return false; } }   // 供视频卡等外部按钮判断某条目当前是否已在任一夹
  };
})();
