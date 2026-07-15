/* rc-userpages.js — 统一控制层:插入页(用户页)。设计:references/reader-userpages-favorites.md「一、插入页(用户页)设计」阶段A。
 * 锚定铁律:用户页 id=u_<8hex>(独立编号空间),插入位置只存 {after:N}(N=原书 PDF 页 1-based / EPUB 章序 1-based;0=书首);
 * 容器是原书页/章元素的**兄弟**节点(EPUB=.ep-usec 不带 .ep-sec class、PDF=.pdf-upage 不带 .page-wrap/data-page-num)
 * → 原书编号零挤占,全书已有高亮/便签/墨迹/进度锚不动。
 * 共享层只管策略(CRUD/markdown 渲染/编辑器);「插进 DOM 哪里」由 host 的 opts.place 提供(机制归 host,同 rc-stickynote v4)。
 * host 契约:RC.userpages.init({
 *   file, cls:'ep-usec'|'pdf-upage',
 *   place(el, after, refEl)->bool  // 把 el 放到 after 位置(refEl=同 after 的前一个用户页,链式保序);目标未渲染→false,
 *                                  // 之后 mountAll 幂等重试(host 可顺带在不该显示时把 el 摘下,如 PDF 单页模式)
 *   afterCurrent()->int,           // ➕ 建页用:当前阅读位置对应的 after 值
 *   posLabel(after)->str,          // prompt 里的人话位置描述
 *   scrollTo(el)                   // 建完滚到新页
 * })
 * 内容=markdown 文本(渲染走 RC.md+RC.typeset,跟 AI 回复同管线,公式/列表天然支持);
 * ✏️ 编辑=textarea 覆盖(iOS: 整条链显式 user-select:text,照 rc-stickynote 的 WebKit 教训);🗑=confirm 删除。 */
(function () {
  if (!window.RC) window.RC = {};
  if (window.RC.userpages) return;
  var EP = '/pdf/api/userpages';
  var O = null;        // host opts
  var _pages = [];     // 本书用户页列表(按 after,created 排序)
  var _els = {};       // id -> 容器 el(build 一次,place 幂等挪位)

  var injected = false;
  function injectCss() {
    if (injected) return; injected = true;
    var css = document.createElement('style'); css.id = 'rc-up-css';
    css.textContent =
      /* 通用骨架(容器内):工具条 + 正文 + 编辑器。iOS:根/编辑链显式 user-select:text(PDF 侧 .page-wrap 家族是 none) */
      '.rc-upage{position:relative;box-sizing:border-box;-webkit-user-select:text;user-select:text}' +
      '.rc-upage .rc-up-bar{display:flex;align-items:center;gap:8px;padding:7px 12px;border-bottom:1px dashed rgba(110,135,195,.45);font-size:13px;-webkit-user-select:none;user-select:none}' +
      '.rc-upage .rc-up-badge{flex:0 0 auto;font-size:11px;color:#5b76b8;border:1px solid rgba(91,118,184,.45);border-radius:999px;padding:1px 8px;white-space:nowrap}' +
      '.rc-upage .rc-up-title{flex:0 1 auto;min-width:0;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
      '.rc-upage .rc-up-sp{flex:1 1 auto}' +
      '.rc-upage .rc-up-bar button{flex:0 0 auto;background:transparent;border:1px solid rgba(110,135,195,.4);border-radius:7px;padding:3px 9px;font-size:13px;line-height:1.3;cursor:pointer;color:inherit;touch-action:manipulation;-webkit-tap-highlight-color:transparent}' +
      '.rc-upage .rc-up-body{padding:10px 14px 14px;min-height:36px;overflow-wrap:break-word}' +
      '.rc-upage .rc-up-body.rc-up-empty{color:#8a93a8;font-size:13px}' +
      '.rc-upage.editing .rc-up-body{display:none}' +
      '.rc-upage .rc-up-edit{display:flex;flex-direction:column;gap:8px;padding:10px 12px 12px;-webkit-user-select:text;user-select:text}' +
      '.rc-upage .rc-up-ti{width:100%;box-sizing:border-box;padding:8px 10px;border-radius:8px;border:1px solid rgba(110,135,195,.5);background:rgba(255,255,255,.8);color:#16233c;-webkit-text-fill-color:#16233c;caret-color:#16233c;font-size:14px;font-weight:600;outline:none;-webkit-user-select:text;user-select:text;-webkit-appearance:none;appearance:none}' +
      '.rc-upage .rc-up-ta{width:100%;box-sizing:border-box;min-height:220px;resize:vertical;font:14px/1.6 ui-monospace,Menlo,Consolas,monospace;padding:10px;border-radius:8px;border:1px solid rgba(110,135,195,.5);background:rgba(255,255,255,.8);color:#16233c;-webkit-text-fill-color:#16233c;caret-color:#16233c;outline:none;-webkit-user-select:text;user-select:text;-webkit-appearance:none;appearance:none}' +
      '.rc-upage .rc-up-ebar{display:flex;gap:10px;justify-content:flex-end}' +
      '.rc-upage .rc-up-ebar button{background:#1a2540;border:1px solid #3b6db5;color:#9fcbff;border-radius:8px;padding:7px 14px;font-size:13px;cursor:pointer;touch-action:manipulation}' +
      '.rc-upage .rc-up-ebar .rc-up-cancel{background:transparent;border-color:rgba(110,135,195,.4);color:inherit}' +
      /* 容器视觉:EPUB 用主题变量(在 #ep-col 内继承排版);PDF 白纸样式对齐 .page-wrap */
      /* ── EPUB 插入页(instant 模式):整页空白纸(≈一屏高,视觉=书里多出的一页)+ 所见即所得覆盖层 + 左上角 Aa 即时编辑。
         照搬 PDF overlay(pdf_reader.html .up2-content/.up2-edit-btn/即时保存)的编辑/保存机制,EPUB 主题变量适配。
         PDF v1 legacy 虚拟页不传 instant → 走上面卡片模式,本段不生效(零 PDF 回归)。 */
      '.ep-usec{position:relative;margin:1.6em 0;min-height:86vh;background:var(--pa,#f6f3ea);color:var(--ink,#1b1b1b);border-radius:9px;box-shadow:0 2px 12px rgba(0,0,0,.18),0 0 0 1px rgba(130,130,130,.24);-webkit-user-select:text;user-select:text}' +
      '.ep-usec .rc-up-disp{padding:2.6em 8% 3.2em}' +
      '.ep-usec .rc-up-hd{font-size:12px;color:var(--lnk,#2a5db0);opacity:.9;border:1px solid rgba(120,140,190,.42);border-radius:999px;padding:2px 11px;display:inline-block;margin-bottom:1.5em;-webkit-user-select:none;user-select:none}' +
      '.ep-usec .rc-up-body{font-size:var(--fs,19px);line-height:var(--lh,1.7);overflow-wrap:break-word;min-height:2em}' +
      '.ep-usec .rc-up-body.rc-up-empty{opacity:.5;font-style:italic}' +
      '.ep-usec .rc-up-body p{margin:0 0 .9em}' +
      '.ep-usec .rc-up-body h1,.ep-usec .rc-up-body h2,.ep-usec .rc-up-body h3{line-height:1.35;margin:1em 0 .5em}' +
      '.ep-usec .rc-up-body ul,.ep-usec .rc-up-body ol{margin:0 0 .9em;padding-left:1.6em}' +
      '.ep-usec .rc-up-body mark.ep-hl{cursor:pointer}' +
      /* Aa 编辑按钮(苹果简约毛玻璃,区分手写 ✏️):唯一进即时编辑入口,z 高于墨迹层(z5) */
      '.ep-up-editbtn{position:absolute;left:10px;top:10px;z-index:12;height:30px;padding:0 13px;display:inline-flex;align-items:center;justify-content:center;background:rgba(255,255,255,.74);-webkit-backdrop-filter:saturate(180%) blur(20px);backdrop-filter:saturate(180%) blur(20px);border:.5px solid rgba(0,0,0,.14);border-radius:9px;font:600 14px/1 -apple-system,BlinkMacSystemFont,"SF Pro Text",system-ui,sans-serif;color:#1d1d1f;cursor:pointer;box-shadow:0 1px 3px rgba(0,0,0,.12);-webkit-user-select:none;user-select:none;transition:transform .1s,background .12s;touch-action:manipulation}' +
      '.ep-up-editbtn:active{background:rgba(255,255,255,.96);transform:scale(.96)}' +
      /* 右上角 ⭐:收藏这一 EPUB 插入页到收藏夹(instant 模式=EPUB;编辑态隐藏,避免压住「完成」按钮)*/
      '.ep-up-favbtn{position:absolute;right:10px;top:10px;z-index:12;height:30px;padding:0 11px;display:inline-flex;align-items:center;justify-content:center;background:rgba(255,255,255,.74);-webkit-backdrop-filter:saturate(180%) blur(20px);backdrop-filter:saturate(180%) blur(20px);border:.5px solid rgba(0,0,0,.14);border-radius:9px;font-size:15px;line-height:1;color:#1d1d1f;cursor:pointer;box-shadow:0 1px 3px rgba(0,0,0,.12);-webkit-user-select:none;user-select:none;transition:transform .1s,background .12s;touch-action:manipulation}' +
      '.ep-up-favbtn:active{background:rgba(255,255,255,.96);transform:scale(.96)}' +
      '.ep-usec.editing .ep-up-favbtn{display:none}' +
      /* 即时编辑态:标题 input + textarea(自动保存,无「保存」按钮)+ 完成/删除条;编辑时藏显示层与墨迹层 */
      '.ep-usec.editing .rc-up-disp{display:none}' +
      '.ep-usec.editing .ep-ink-canvas{display:none}' +
      '.ep-usec .rc-up-edit{display:flex;flex-direction:column;min-height:86vh}' +
      '.ep-usec .rc-up-ebar{flex:0 0 auto;display:flex;gap:8px;align-items:center;padding:10px 14px;border-bottom:1px dashed rgba(120,140,190,.4);position:sticky;top:0;background:var(--pa,#f6f3ea);z-index:2}' +
      '.ep-usec .rc-up-ti{flex:1;min-width:0;box-sizing:border-box;padding:7px 10px;border:1px solid rgba(120,140,190,.45);border-radius:7px;font-size:14px;font-weight:600;outline:none;background:rgba(255,255,255,.92);color:#16233c;-webkit-text-fill-color:#16233c;caret-color:#16233c;-webkit-user-select:text;user-select:text;-webkit-appearance:none;appearance:none}' +
      '.ep-usec .rc-up-del{flex:0 0 auto;background:transparent;border:none;color:#e5484d;border-radius:8px;padding:6px 10px;font:500 13px -apple-system,system-ui,sans-serif;cursor:pointer;touch-action:manipulation}' +
      '.ep-usec .rc-up-done{flex:0 0 auto;background:#007aff;border:none;color:#fff;border-radius:8px;padding:7px 16px;font:600 13px -apple-system,system-ui,sans-serif;cursor:pointer;touch-action:manipulation}' +
      '.ep-usec .rc-up-done:active{background:#0062cc}' +
      '.ep-usec .rc-up-ta{flex:1 1 auto;width:100%;box-sizing:border-box;min-height:58vh;border:none;outline:none;resize:none;padding:16px 8%;font:15px/1.7 ui-monospace,Menlo,Consolas,monospace;background:var(--pa,#f6f3ea);color:var(--ink,#1b1b1b);-webkit-text-fill-color:var(--ink,#1b1b1b);caret-color:var(--lnk,#2a5db0);-webkit-user-select:text;user-select:text;-webkit-appearance:none;appearance:none;transform:translateZ(0)}' +
      '.ep-usec .rc-up-savehint{flex:0 0 auto;font-size:11px;opacity:.55;padding:5px 8% 12px;text-align:right;-webkit-user-select:none;user-select:none}' +
      /* ── 手动高度(编辑态下边缘拖动手柄设置,持久化到 userpages 记录 .h;设计见 references「插入页高度」)──
         有 .uh-set(=用户拖过)时 --uh(px)独占驱动高度,覆盖等比 keepRatio;侧栏开合宽变高不变(用户明确要的自定义高)。
         min-height 而非 height:内容比 --uh 高时自然增长,永不裁掉文字。编辑态编辑器/文本域一并跟随 --uh。 */
      '.ep-usec.uh-set{min-height:var(--uh,86vh)}' +
      '.ep-usec.uh-set .rc-up-edit{min-height:var(--uh,86vh)}' +
      '.ep-usec.uh-set .rc-up-ta{min-height:0}' +
      /* 下边缘拖动手柄:仅编辑态显示(.editing);touch-action:none 防触摸滚动抢事件;居中 grip 指示条 */
      '.ep-up-rz{position:absolute;left:0;right:0;bottom:0;height:16px;display:none;align-items:center;justify-content:center;cursor:ns-resize;touch-action:none;z-index:11;-webkit-user-select:none;user-select:none}' +
      '.ep-usec.editing .ep-up-rz{display:flex}' +
      '.ep-up-rz::before{content:"";width:46px;height:5px;border-radius:3px;background:rgba(120,140,190,.5);transition:background .12s}' +
      '.ep-up-rz:active::before,.ep-up-rz.dragging::before{background:rgba(120,140,190,.95)}' +
      '.pdf-upage{margin:0 auto 12px;background:#fff;color:#1b1b1b;border-radius:6px;box-shadow:0 0 0 1.5px rgba(59,109,181,.5),0 2px 8px rgba(0,0,0,.35);max-width:100%}' +
      '.pdf-upage .rc-up-body{font-size:15px;line-height:1.65}' +
      '.pdf-upage .rc-up-body p{margin:0 0 .8em}' +
      '.pdf-upage .rc-up-body h1,.pdf-upage .rc-up-body h2,.pdf-upage .rc-up-body h3{line-height:1.35;margin:.9em 0 .45em}' +
      '.pdf-upage .rc-up-body ul,.pdf-upage .rc-up-body ol{margin:0 0 .8em;padding-left:1.6em}';
    document.head.appendChild(css);
  }

  function esc(s) { return (window.RC && RC.esc) ? RC.esc(s) : String(s == null ? '' : s); }
  function toast(m) { if (window.RC && RC.toast) RC.toast(m); }
  function req(method, url, body) { return RC.reqJson(method, url, body); }
  // per-page 文件:收藏夹用 mountOne 挂进来的页绑各自「原书文件」(p.__file);原书自己的页无 __file → 回退 O.file。
  //   → 编辑/改高/存/删 全走这个,收藏夹与原书**同一份代码**,写回各自原书(向后兼容,原书路径零回归)。
  function _fileOf(p) { return (p && p.__file) || (O && O.file) || ''; }

  function sortPages() {
    _pages.sort(function (a, b) {
      return ((a.after | 0) - (b.after | 0)) || ((a.created || 0) - (b.created || 0)) || String(a.id).localeCompare(String(b.id));
    });
  }

  function renderBody(el, p) {
    if (O && O.instant) { renderInstant(el, p); return; }
    var body = el.querySelector('.rc-up-body'); if (!body) return;
    var md = (p.md || '').trim();
    if (md) {
      body.classList.remove('rc-up-empty');
      body.innerHTML = (window.RC && RC.md) ? RC.md(p.md) : esc(p.md).replace(/\n/g, '<br>');
      try { if (window.RC && RC.typeset) RC.typeset(body); } catch (_) {}
    } else {
      body.classList.add('rc-up-empty');
      body.textContent = '(空白页 — 点 ✏️ 开始写)';
    }
    var t = el.querySelector('.rc-up-title'); if (t) t.textContent = p.title || '我的页';
  }

  function ensureEl(p) {
    var el = _els[p.id];
    if (el) return el;
    if (O && O.instant) return buildInstant(p);
    el = document.createElement('div');
    el.className = 'rc-upage' + (O && O.cls ? ' ' + O.cls : '');
    el.dataset.uid = p.id;
    el.dataset.after = String(p.after | 0);
    el.innerHTML =
      '<div class="rc-up-bar"><span class="rc-up-badge">📝 我的页</span><span class="rc-up-title"></span>' +
      '<span class="rc-up-sp"></span><button class="rc-up-e" title="编辑(markdown,支持 $公式$/列表/标题)">✏️</button>' +
      '<button class="rc-up-d" title="删除这一页">🗑</button></div>' +
      '<div class="rc-up-body"></div>';
    el.querySelector('.rc-up-e').addEventListener('click', function (e) { e.stopPropagation(); openEditor(p); });
    el.querySelector('.rc-up-d').addEventListener('click', function (e) {
      e.stopPropagation();
      if (!confirm('删除用户页「' + (p.title || '我的页') + '」?(不可恢复)')) return;
      req('DELETE', EP + '?file=' + encodeURIComponent(_fileOf(p)) + '&id=' + encodeURIComponent(p.id), null)
        .then(function (d) {
          if (!(d && d.ok)) { toast('删除失败:' + ((d && d.error) || '?')); return; }
          try { el.remove(); } catch (_) {}
          delete _els[p.id];
          _pages = _pages.filter(function (x) { return x.id !== p.id; });
          toast('已删除');
        }).catch(function () { toast('网络错误,没删掉'); });
    });
    _els[p.id] = el;
    renderBody(el, p);
    return el;
  }

  // ═══════════ instant 模式(EPUB 插入页:整页空白 + Aa 即时编辑)═══════════
  // 照搬 PDF overlay 的即时保存三件套(pdf_reader.html _upTextSchedule/_upTextSave/_upTextFlushBeacon):
  //   打字防抖 600ms PATCH 存边车(/api/userpages,EPUB 虚拟页有 after 无 page → 后端字段级合并),
  //   无「保存」按钮,离页 keepalive PATCH 兜底防丢字。PDF 侧不受影响(instant 仅 EPUB 传)。
  var _upTimers = {}, _upDirty = {}, _upSnap = {}, _beaconBound = false;
  function _instTextSchedule(p, title, md) {
    _upDirty[p.id] = true; _upSnap[p.id] = { title: title, md: md, file: _fileOf(p) };   // 快照防延迟读;带 file → 存回各自原书(per-page)
    clearTimeout(_upTimers[p.id]);
    _upTimers[p.id] = setTimeout(function () { _instTextSave(p.id); }, 600);
  }
  function _instTextSave(id) {
    var snap = _upSnap[id]; if (!snap || !snap.file) return Promise.resolve();
    delete _upDirty[id];
    return req('PATCH', EP, { file: snap.file, id: id, title: snap.title, md: snap.md })
      .then(function (d) { if (!(d && d.ok)) _upDirty[id] = true; return d; })
      .catch(function () { _upDirty[id] = true; });
  }
  function _instTextFlush(id) { clearTimeout(_upTimers[id]); return _instTextSave(id); }
  function _instFlushBeacon() {
    for (var id in _upDirty) {
      if (!_upDirty[id]) continue;
      var snap = _upSnap[id]; if (!snap || !snap.file) continue;
      clearTimeout(_upTimers[id]);
      // ⚠ 用 keepalive fetch 走 PATCH,不用 sendBeacon:userpages 路由 POST=建新页(sendBeacon 只能 POST)→ 会误建重复页;
      //   keepalive PATCH 才是正确的「离页仍送达的更新」(照 PDF _upSyncFlushKeepalive)。snap.file=各自原书(per-page)。
      try { fetch(EP, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, keepalive: true, body: JSON.stringify({ file: snap.file, id: id, title: snap.title, md: snap.md }) }); } catch (_) {}
    }
    _upDirty = {};
  }
  function _bindBeaconOnce() {
    if (_beaconBound) return; _beaconBound = true;
    window.addEventListener('pagehide', _instFlushBeacon);
    document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'hidden') _instFlushBeacon(); });
  }
  // 整页空白纸容器 + 显示态 + Aa 入口(照 PDF _upMountOverlay 的常驻覆盖层语义)
  // 插入页保持原先(侧栏关闭=最宽态)的宽高比:助手/知识点侧栏用 padding-right 挤窄 #ep-content → #ep-col 变窄
  // → .ep-usec 宽度缩小,但固定 min-height:86vh 使其「变瘦」失真。这里让高度随宽度等比缩放(像一张纸)。
  //   naturalHeight = 86vh(px,与 CSS min-height 口径一致,侧栏开合不变);naturalWidth = 观测到的最大宽度(= 侧栏关闭态)。
  //   aspect-ratio = natW/natH → 满宽时高度=86vh(视觉不变),变窄时高度等比缩小。用最大宽度自愈:即便首次挂载正好
  //   在侧栏已开的窄态,之后关侧栏 width 超过旧 natW 即重算修正。EPUB 无固定页几何(桌面≈横、手机≈竖),
  //   必须按设备实测宽度而非写死比例。只在 O.instant(=EPUB 插入页)用;PDF legacy 卡片不进本路径。
  function keepRatio(el) {
    if (!window.ResizeObserver) return;
    var natW = 0;
    var ro = new ResizeObserver(function () {
      var w = el.getBoundingClientRect().width;
      if (w > natW + 1) {                                   // 新的最大宽度(自然/侧栏关闭态)→ 重算比例
        natW = w;
        var natH = Math.round((window.innerHeight || 800) * 0.86);   // 86vh
        el.style.aspectRatio = natW + ' / ' + natH;
        el.style.minHeight = '0';                           // 让 aspect-ratio 独占驱动高度(原 86vh 会挡住等比缩小)
      }
    });
    ro.observe(el);
    el.__epRatioRO = ro;
  }
  // 手动高度 clamp:最小 120px(别拖没了 + 编辑器 chrome 放得下)、最大 300vh(防失控)。
  function _clampH(v) {
    var vh = window.innerHeight || 800;
    return Math.max(120, Math.min(Math.round(vh * 3), Math.round(v || 0)));
  }
  // 手动高度落库(防抖 300ms 合并快速多拖;PATCH 只带 h,后端字段级合并不动 title/md)。
  var _upHTimers = {};
  function _instHeightSchedule(p) {
    clearTimeout(_upHTimers[p.id]);
    var id = p.id, hv = p.h;
    _upHTimers[p.id] = setTimeout(function () {
      var f = _fileOf(p); if (!f) return;
      req('PATCH', EP, { file: f, id: id, h: hv }).catch(function () {});   // per-page:改高存回各自原书
    }, 300);
  }
  // 下边缘拖动手柄:仅编辑态露出(CSS gate)。按住上下拖 → 实时改 .ep-usec 高度;松手 clamp + 落库。
  //   一旦拖过就切「手动模式」:停 keepRatio(disconnect RO)、清等比 aspect-ratio、加 .uh-set → --uh 独占驱动高度,
  //   侧栏开合宽度变但高度保持用户设的值(不再等比覆盖手动值,keepRatio 与手动互斥不打架)。用 pointer 事件统一鼠标/触摸。
  // 通知阅读器改高各阶段(start 存墨迹原始快照 / move 实时回算 / end 落库),阅读器据此把墨迹 y 按 startH/curH 回算,
  // 保持像素位置不变 = 下方扩展空间而非整体拉伸(墨迹层归一到 section 高度,不补偿就会随高度拉伸)。
  function _rzEmit(el, phase, startH, curH) {
    try { el.dispatchEvent(new CustomEvent('rc-upage-resize', { bubbles: true, detail: { phase: phase, startH: startH, curH: curH } })); } catch (_) {}
  }
  function bindResize(el, p) {
    var h = el.querySelector('.ep-up-rz'); if (!h) return;
    var startY = 0, startH = 0, active = false, pid = null;
    h.addEventListener('pointerdown', function (e) {
      active = true; pid = e.pointerId;
      startY = e.clientY; startH = el.getBoundingClientRect().height;
      try { if (el.__epRatioRO) { el.__epRatioRO.disconnect(); el.__epRatioRO = null; } } catch (_) {}
      el.style.aspectRatio = ''; el.style.minHeight = '';   // 清掉 keepRatio 留的等比/内联 min-height,交给 .uh-set
      el.classList.add('uh-set'); h.classList.add('dragging');
      _rzEmit(el, 'start', startH, startH);   // 存墨迹原始快照,供按比例回算防拉伸
      try { h.setPointerCapture(e.pointerId); } catch (_) {}
      e.preventDefault(); e.stopPropagation();
    });
    h.addEventListener('pointermove', function (e) {
      if (!active || e.pointerId !== pid) return;
      var nh = _clampH(startH + (e.clientY - startY));
      el.style.setProperty('--uh', nh + 'px');
      _rzEmit(el, 'move', startH, nh);        // 实时回算墨迹 y(RO 用回算后的笔画在新尺寸重绘 → 不拉伸)
      if (e.cancelable) e.preventDefault();
    });
    function end() {
      if (!active) return; active = false; h.classList.remove('dragging');
      try { h.releasePointerCapture(pid); } catch (_) {}
      var nh = _clampH(parseFloat(el.style.getPropertyValue('--uh')) || el.getBoundingClientRect().height);
      el.style.setProperty('--uh', nh + 'px');
      p.h = nh; _instHeightSchedule(p);
      _rzEmit(el, 'end', startH, nh);         // 落库回算后的墨迹
    }
    h.addEventListener('pointerup', end);
    h.addEventListener('pointercancel', end);
  }
  function buildInstant(p) {
    var el = document.createElement('div');
    el.className = 'rc-upage ep-usec';   // ep-usec = EPUB 插入页(不带 .ep-sec、不进 secEls → 原书锚零挤占)
    el.dataset.uid = p.id;
    el.dataset.after = String(p.after | 0);
    el.innerHTML =
      '<div class="ep-up-editbtn" title="编辑这一页的文字(markdown,自动保存)">Aa</div>' +
      '<div class="rc-up-disp"><span class="rc-up-hd"></span><div class="rc-up-body"></div></div>' +
      '<div class="ep-up-rz" title="拖动调整这一页高度(编辑态)"></div>';   // 下边缘高度拖动手柄(仅编辑态露出)
    el.querySelector('.ep-up-editbtn').addEventListener('click', function (e) { e.stopPropagation(); enterEditInstant(p); });
    // 右上角 ⭐ 收藏这一页(自己创建的插入页)→ rc-favorites 弹窗;禁自我收藏:收藏夹物化书前缀不给
    if (window.RC && RC.favorites && RC.favorites.openPicker && O && O.file && O.file.indexOf('资源/收藏夹/') !== 0) {
      var fb = document.createElement('div'); fb.className = 'ep-up-favbtn';
      fb.textContent = '⭐'; fb.title = '收藏这一页到收藏夹';
      fb.addEventListener('click', function (e) { e.stopPropagation(); RC.favorites.openPicker({ file: O.file, kind: 'userpage', id: p.id }); });
      el.appendChild(fb);
    }
    _els[p.id] = el;
    renderInstant(el, p);
    // 高度策略(keepRatio 与手动高度互斥,优先级明确):
    //   · 有存过的手动高度 p.h → 手动模式:.uh-set + --uh 独占驱动高度,不跑 keepRatio(侧栏开合宽变高不变)。
    //   · 从没设过 → 默认 keepRatio 等比(侧栏挤压时等比缩放,不变瘦),行为与旧版一致,零回归。
    if (typeof p.h === 'number' && p.h > 0) {
      el.classList.add('uh-set');
      el.style.setProperty('--uh', _clampH(p.h) + 'px');
    } else {
      keepRatio(el);
    }
    bindResize(el, p);   // 下边缘拖动手柄(编辑态露出)——首次拖动即切手动模式
    return el;
  }
  // 显示态:RC.md 渲染(公式/列表/标题);typeset + 高亮/墨迹复原交给 host(onRender,照 PDF _ovTypesetThenHl 排版后复原口径一致)
  function renderInstant(el, p) {
    var hd = el.querySelector('.rc-up-hd'); if (hd) hd.textContent = '📝 ' + (p.title || '我的页');
    var body = el.querySelector('.rc-up-body'); if (!body) return;
    var md = (p.md || '').trim();
    if (md) { body.classList.remove('rc-up-empty'); body.innerHTML = (window.RC && RC.md) ? RC.md(p.md) : esc(p.md).replace(/\n/g, '<br>'); }
    else { body.classList.add('rc-up-empty'); body.textContent = '（点左上角 Aa 写笔记 · 自动保存,无需按钮）'; }
    if (O && O.onRender) { try { O.onRender(el, p); } catch (_) {} }
    else { try { if (window.RC && RC.typeset) RC.typeset(body); } catch (_) {} }
  }
  // 即时编辑:textarea 打字 → 防抖存边车(无保存按钮);「✓ 完成」收起回显示态;🗑 删除
  function enterEditInstant(p) {
    var el = _els[p.id]; if (!el || !el.isConnected) return;
    if (el.classList.contains('editing') || el.querySelector('.rc-up-edit')) return;
    _bindBeaconOnce();
    var ed = document.createElement('div'); ed.className = 'rc-up-edit';
    ed.innerHTML =
      '<div class="rc-up-ebar"><input class="rc-up-ti" type="text" maxlength="120" placeholder="标题(可空)">' +
      '<button class="rc-up-del" title="删除这一页">🗑</button><button class="rc-up-done">✓ 完成</button></div>' +
      '<textarea class="rc-up-ta" placeholder="正文…(markdown:# 标题 / 列表 / **粗体** / $..$ 公式;自动保存)"></textarea>' +
      '<div class="rc-up-savehint">自动保存,无需手动</div>';
    var ti = ed.querySelector('.rc-up-ti'), ta = ed.querySelector('.rc-up-ta');
    ti.value = p.title || ''; ta.value = p.md || '';
    el.appendChild(ed); el.classList.add('editing');
    document.body.classList.add('ep-up-editing');   // 编辑期禁手写/选词(host ink gate 认这个 class)
    function onInput() { p.title = ti.value; p.md = ta.value; _instTextSchedule(p, ti.value.trim(), ta.value); }
    ti.addEventListener('input', onInput); ta.addEventListener('input', onInput);
    ed.querySelector('.rc-up-done').addEventListener('click', function (e) {
      e.stopPropagation();
      try { ed.remove(); } catch (_) {}
      el.classList.remove('editing'); document.body.classList.remove('ep-up-editing');
      _instTextFlush(p.id);       // 收起时把最后一批存完
      renderInstant(el, p);       // 回显示态(RC.md + 高亮/墨迹复原)
    });
    ed.querySelector('.rc-up-del').addEventListener('click', function (e) {
      e.stopPropagation();
      // 同一张纸:收藏夹挂载页(__mounted)删的也是原书那页,后端级联移除各收藏夹条目(双向同步,用户拍板)
      var msg = p.__mounted ? '删除用户页「' + (p.title || '我的页') + '」?将从原书和收藏夹同步删除(不可恢复)'
                            : '删除用户页「' + (p.title || '我的页') + '」?(不可恢复)';
      if (!confirm(msg)) return;
      try { ed.remove(); } catch (_) {}
      el.classList.remove('editing'); document.body.classList.remove('ep-up-editing');
      removePage(p);
    });
    setTimeout(function () { try { ta.focus(); } catch (_) {} }, 60);
  }
  function removePage(p) {
    req('DELETE', EP + '?file=' + encodeURIComponent(_fileOf(p)) + '&id=' + encodeURIComponent(p.id), null).then(function (d) {
      if (!(d && d.ok)) { toast('删除失败:' + ((d && d.error) || '?')); return; }
      var el = _els[p.id]; if (el) { try { if (el.__epRatioRO) el.__epRatioRO.disconnect(); } catch (_) {} try { el.remove(); } catch (_) {} }
      delete _els[p.id]; _pages = _pages.filter(function (x) { return x.id !== p.id; });
      if (O && O.onRemoved) { try { O.onRemoved(p); } catch (_) {} }   // host 清该页高亮/墨迹缓存,避免孤儿
      toast('已删除');
    }).catch(function () { toast('网络错误,没删掉'); });
  }
  // 乐观新建(instant):EPUB 建记录=一次本地边车 POST(无后台 job),POST 回来即插入 + 进即时编辑(用户马上打字)。
  //   比 PDF 简单(PDF 建页=多秒 job 才需临时 id/绑定;EPUB 边车 POST ~毫秒 → 直接建记录再本地插,设计如是)。
  function createInstant() {
    var after = 0;
    try { after = (O.afterCurrent ? O.afterCurrent() : 0) | 0; } catch (_) {}
    if (after < 0) after = 0;
    req('POST', EP, { file: O.file, after: after, title: '', md: '' }).then(function (d) {
      if (!(d && d.ok && d.page)) { toast('创建失败:' + ((d && d.error) || '?')); return; }
      _pages.push(d.page); sortPages(); mountAll();
      var el = _els[d.page.id];
      if (el && el.isConnected) {
        if (O.scrollTo) { try { O.scrollTo(el); } catch (_) {} }
        setTimeout(function () { enterEditInstant(d.page); }, 60);
      } else { toast('已创建(翻到对应位置可见)'); }
    }).catch(function () { toast('网络错误,没创建上'); });
  }

  function openEditor(p) {
    if (O && O.instant) { enterEditInstant(p); return; }
    var el = _els[p.id]; if (!el || !el.isConnected) return;
    if (el.querySelector('.rc-up-edit')) return;   // 已在编辑
    var ed = document.createElement('div'); ed.className = 'rc-up-edit';
    var ti = document.createElement('input'); ti.className = 'rc-up-ti'; ti.type = 'text'; ti.maxLength = 120;
    ti.placeholder = '标题'; ti.value = p.title || '';
    var ta = document.createElement('textarea'); ta.className = 'rc-up-ta';
    ta.placeholder = 'markdown 正文…(支持 $..$ 公式 / 列表 / 标题)'; ta.value = p.md || '';
    var bar = document.createElement('div'); bar.className = 'rc-up-ebar';
    bar.innerHTML = '<button class="rc-up-cancel">✕ 取消</button><button class="rc-up-save">✓ 保存</button>';
    ed.appendChild(ti); ed.appendChild(ta); ed.appendChild(bar);
    el.appendChild(ed); el.classList.add('editing');
    function close() { try { ed.remove(); } catch (_) {} el.classList.remove('editing'); }
    bar.querySelector('.rc-up-cancel').addEventListener('click', function (e) { e.stopPropagation(); close(); });
    bar.querySelector('.rc-up-save').addEventListener('click', function (e) {
      e.stopPropagation();
      var btn = bar.querySelector('.rc-up-save');
      btn.disabled = true; btn.textContent = '…';
      req('PATCH', EP, { file: O.file, id: p.id, title: ti.value.trim(), md: ta.value }).then(function (d) {
        if (!(d && d.ok)) { btn.disabled = false; btn.textContent = '✓ 保存'; toast('保存失败:' + ((d && d.error) || '?')); return; }
        p.title = d.page ? d.page.title : ti.value.trim();
        p.md = d.page ? d.page.md : ta.value;
        close(); renderBody(el, p); toast('已保存');
      }).catch(function () { btn.disabled = false; btn.textContent = '✓ 保存'; toast('网络错误,没保存上'); });
    });
    setTimeout(function () { try { ta.focus(); } catch (_) {} }, 60);
  }

  // 幂等补挂:每页 el 建一次,place 决定放哪/该不该在(目标页未渲染 → false,下次再试;
  // 同 after 多页经 refEl 链保序)。挂好后顺带 stickynote.mountPending(便签可能锚在用户页 u_* 上)。
  // O.filter(p)->bool:host 可排除某些记录不 mount(PDF 真插入页记录带 page 字段,由 host 渲角标;EPUB 不传=全 mount)。
  function mountAll() {
    if (!O) return;
    var lastByAfter = {}, placedAny = false;
    for (var i = 0; i < _pages.length; i++) {
      var p = _pages[i];
      if (p.__mounted) continue;   // 收藏夹 mountOne 挂进来的页已放在各自 fav 章,别走原书 place 逻辑再挪
      if (O.filter && !O.filter(p)) continue;
      var el = ensureEl(p);
      var ok = false;
      try { ok = !!O.place(el, p.after | 0, lastByAfter[p.after | 0] || null); } catch (_) {}
      if (ok) { lastByAfter[p.after | 0] = el; placedAny = true; }
    }
    if (placedAny) { try { if (window.RC && RC.stickynote && RC.stickynote.mountPending) RC.stickynote.mountPending(); } catch (_) {} }
  }

  function load() {
    if (!O || !O.file) return;
    req('GET', EP + '?file=' + encodeURIComponent(O.file), null).then(function (d) {
      if (!(d && d.ok)) return;
      var mounted = _pages.filter(function (p) { return p.__mounted; });   // 保留收藏夹跨书挂载的页(load 只覆盖本书自己的页,别把 mountOne 挂的丢了。审查确认)
      _pages = (d.pages || []).concat(mounted);
      sortPages();
      mountAll();
    }).catch(function () {});
  }

  function create() {
    if (!O) return;
    if (O.instant) { createInstant(); return; }
    var after = 0;
    try { after = (O.afterCurrent ? O.afterCurrent() : 0) | 0; } catch (_) {}
    if (after < 0) after = 0;
    var pos = O.posLabel ? O.posLabel(after) : ('位置 ' + after);
    var title = prompt('插入我的页(markdown 笔记页,渲染进书里)\n位置:' + pos + ' 之后\n输入标题:', '');
    if (title == null) return;   // 取消
    req('POST', EP, { file: O.file, after: after, title: title.trim(), md: '' }).then(function (d) {
      if (!(d && d.ok && d.page)) { toast('创建失败:' + ((d && d.error) || '?')); return; }
      _pages.push(d.page); sortPages(); mountAll();
      var el = _els[d.page.id];
      if (el && el.isConnected) {
        if (O.scrollTo) { try { O.scrollTo(el); } catch (_) {} }
        setTimeout(function () { openEditor(d.page); }, 120);
      } else {
        toast('已创建(翻到对应位置可见)');
      }
    }).catch(function () { toast('网络错误,没创建上'); });
  }

  // 把「单个插入页」挂进任意容器,绑到任意 (file, id) —— 收藏夹复用这套(与原书编辑器**完全同一份代码**:
  //   Aa 即时编辑 / 下边缘改高 / keepRatio / 自动保存,全经 buildInstant + _fileOf 存回各自原书)。返回 Promise<el|null>。
  function mountOne(container, opts) {
    opts = opts || {};
    var file = opts.file, id = opts.id;
    if (!container || !file || !id) return Promise.resolve(null);
    if (_els[id]) { try { container.appendChild(_els[id]); } catch (_) {} return Promise.resolve(_els[id]); }   // 已挂 → 幂等重接
    return req('GET', EP + '?file=' + encodeURIComponent(file), null).then(function (d) {
      var p = ((d && d.pages) || []).filter(function (x) { return x.id === id; })[0];
      if (!p) return null;
      p.__file = file; p.__mounted = true;   // per-page 文件 + 标记(mountAll 跳过,不走原书 place)
      var el = buildInstant(p);              // 复用原书那套(内部 _els[id]=el + onRender 墨迹/typeset)
      if (_pages.indexOf(p) < 0) _pages.push(p);   // 进 _pages 供 elOf/墨迹集成(_epUpEls)找到
      try { container.appendChild(el); } catch (_) {}
      _bindBeaconOnce();
      return el;
    }).catch(function () { return null; });
  }

  window.RC.userpages = {
    init: function (opts) { O = opts || null; injectCss(); },
    load: load,
    mountAll: mountAll,
    create: create,
    mountOne: mountOne,   // 收藏夹:把单页绑到原书 (file,id) 挂进 fav 容器,与原书同一份编辑器/改高/存
    elOf: function (id) { return _els[id] || null; },   // 便签 host 用:u_* 锚 → 容器 el(未插入时也返回,调用方自查 isConnected)
    pages: function () { return _pages.slice(); },
    // 纯本地移除(不调服务端)——PDF 删页的服务端删除走异步改页 job,前端必须**立刻**把这条从
    //   _pages 剔除,否则 mountAll/_upMountBadges 会从陈旧列表把已删页重挂回 DOM(删了还显示的根因)。
    removeLocal: function (id) {
      var p = null, i;
      for (i = 0; i < _pages.length; i++) { if (_pages[i].id === id) { p = _pages[i]; break; } }
      var el = _els[id]; if (el) { try { if (el.__epRatioRO) el.__epRatioRO.disconnect(); } catch (_) {} try { el.remove(); } catch (_) {} }
      delete _els[id]; _pages = _pages.filter(function (x) { return x.id !== id; });
      if (p && O && O.onRemoved) { try { O.onRemoved(p); } catch (_) {} }
    }
  };
})();
