/* rc-toolchip.js — 工具活动指示器 v2(用户设计,2026-07-14)
 *
 * 一次工具调用 = 一个 chip(逻辑单元),可以同时有**多个视图**:
 *   · 浮层视图(字幕模式,绝对定位、交错重叠)
 *   · 侧栏视图(对话流内联,inflow)
 *   · 侧栏拖出的副本(原件留在侧栏,副本落到画面上)
 *   所有视图共享同一 cid、同一状态 —— 状态更新一次,处处同步;
 *   选中一处,**处处高亮**;取消一处,**处处取消**(用户强调的全局广播)。
 *
 * 每个视图在三种形态间生长(圆形标记本身就是形态控制按钮,坐落在方块的左上角):
 *   圆(circle)   创建 / 收起 —— 玻璃符号,透明无边缘
 *   长条(bar)    折叠 / 进行中 —— 有色磨砂胶囊,**内部步骤在这里滚**
 *   方块(card)   展开 / 结果   —— 有色磨砂 + 边缘阴影;制卡=完整卡片预览(正反面/公式/图)
 *
 * 手势(复用现有语音层判定口径):
 *   单击            → 形态循环 circle → bar → card → circle
 *   长按(320ms)     → 按下特效,进入可拖;拖动**粘性 8px**(与 _dragToDock 同阈值)
 *   长按原地松手     → 选中 / 取消选中(紫边,全局广播)
 *   一旦被拖动/长按 → touched=1,出结果时**不自动展开**(尊重用户摆放)
 *   出结果 20s 后    → 没动过的自动收起成圆标记(单击可再展开)
 *
 * 颜色 = 输出类型(紫色**只**表示选中,不参与类型编码):
 *   anki 制卡(单独色) / text 字符 / image 图片 / video 视频 / weather 天气 / news 新闻
 *   action 执行类(翻页等):浮层上无方块、不可选中、完成即消失;侧栏里保留(可点开看确认)
 *
 * 数据源:后端 /api/voice/task-status 的 {step, steps[], result:{kind,n,deck,cards[]}}
 *   —— steps 直接渲进方块的「步骤」区(即原「!」详情面板的内容)。
 */
(function () {
  'use strict';
  if (window.RC && RC.toolChip) return;
  window.RC = window.RC || {};

  var LONG_PRESS = 320;      // 长按阈值
  var STICKY = 8;            // 拖动粘性阈值(与 rc-voicecall _dragToDock 同口径)
  var AUTO_COLLAPSE = 20000; // 出结果后自动收起成圆标记(用户选:约 20 秒)

  // ── 类型 → 颜色 + 图标(SF 线条,currentColor)──
  var SVG = {
    anki: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><rect x="2.4" y="3.6" width="8.6" height="9" rx="1.6"/><path d="M5 3.6V2.8a1 1 0 0 1 1-1h6.6a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1h-.8"/><path d="M4.8 6.6h3.8M4.8 9h2.4"/></svg>',
    text: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9.8 3.2l3 3L6 13H3v-3z"/><path d="M8.4 4.6l3 3"/></svg>',
    image: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="2.2" y="3.2" width="11.6" height="9.6" rx="2"/><circle cx="5.6" cy="6.4" r="1.1"/><path d="M3 11.4l3.2-3 2.3 2 2.2-2.3 2.3 2.6"/></svg>',
    video: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="2" y="3.4" width="12" height="9.2" rx="2"/><path d="M6.6 6.4l3.6 1.9-3.6 1.9z" fill="currentColor" stroke="none"/></svg>',
    weather: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"><circle cx="6" cy="6.4" r="2.4"/><path d="M6 1.8v1.2M1.6 6.4h1.2M3 3.4l.85.85M9 3.4l-.85.85"/><path d="M6.4 12.6h5.2a2 2 0 1 0-.6-3.9 2.8 2.8 0 0 0-5.1 1.1" stroke-linejoin="round"/></svg>',
    news: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"><rect x="2.2" y="3.4" width="11.6" height="9.2" rx="1.6"/><path d="M4.6 6.2h4M4.6 8.4h6.8M4.6 10.4h6.8"/></svg>',
    action: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8h9M8.6 4.2L12.4 8l-3.8 3.8"/></svg>',
    gear: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="8" cy="8" r="2.2"/><path d="M8 2.6v1.7M8 11.7v1.7M2.6 8h1.7M11.7 8h1.7M4.2 4.2l1.2 1.2M10.6 10.6l1.2 1.2M11.8 4.2l-1.2 1.2M5.4 10.6l-1.2 1.2"/></svg>'
  };
  var TYPE_C = {   // 输出类型 → 主色
    anki: '#39d98a', text: '#5b9cff', image: '#c77dff', video: '#ff7a59',
    weather: '#2dd4bf', news: '#fbbf24', action: '#8194b8'
  };
  // 工具名 → 输出类型(制卡单独一色;不产出内容的执行类归 action)
  function typeOf(tool) {
    var t = String(tool || '');
    if (/^(make_anki|add_vocab)/.test(t)) return 'anki';
    if (/^search_image/.test(t)) return 'image';
    if (/^search_video/.test(t)) return 'video';
    if (/weather/.test(t)) return 'weather';
    if (/news/.test(t)) return 'news';
    // 执行类:不产出可看的内容,只改状态(翻页/高亮/开书/撤销/跳章)
    if (/^(goto|turn|highlight|epub_highlight|auto_highlight|mark|open_book|undo_last|wait_for_user)/.test(t)) return 'action';
    if (/^(make_note|notes_|read|see|summarize|lookup|translate|dict|search|find_highlights|toc|list_sections|page_vocab|section_vocab|recall|web_search|deep_think|route_to_text)/.test(t)) return 'text';
    return 'text';
  }
  function isAction(type) { return type === 'action'; }

  // ── 样式(注入一次)──
  var _css = false;
  function injectCss() {
    if (_css) return; _css = true;
    var s = document.createElement('style');
    s.textContent =
      '.rtc-chip{position:absolute;z-index:2147481400;cursor:pointer;touch-action:none;-webkit-tap-highlight-color:transparent;' +
        'color:var(--rc-c);' +
        '--rc-fill:color-mix(in srgb,var(--rc-c) 20%,rgba(16,22,38,.86));' +
        '--rc-line:color-mix(in srgb,var(--rc-c) 45%,transparent);' +
        'transition:width .32s cubic-bezier(.2,.85,.3,1),height .32s cubic-bezier(.2,.85,.3,1),' +
        'border-radius .32s,background .3s,box-shadow .3s,border-color .2s,transform .12s,opacity .2s}' +
      '.rtc-chip .rtc-ico{position:absolute;left:0;top:0;width:38px;height:38px;display:flex;align-items:center;justify-content:center}' +
      '.rtc-chip .rtc-ico svg{width:19px;height:19px}' +
      // 圆:创建/收起 —— 透明玻璃、无边缘
      '.rtc-chip.circle{width:38px;height:38px;border-radius:50%;border:1px solid transparent;' +
        'background:rgba(255,255,255,.05);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);box-shadow:none}' +
      '.rtc-chip.circle .rtc-body{display:none}' +
      '.rtc-chip.circle .rtc-ico{color:color-mix(in srgb,var(--rc-c) 78%,#fff)}' +
      // 长条:折叠/进行中 —— 有色磨砂胶囊
      '.rtc-chip.bar{width:236px;height:38px;border-radius:19px;border:1px solid var(--rc-line);' +
        'background:var(--rc-fill);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);' +
        'box-shadow:0 8px 22px -12px rgba(0,0,0,.6)}' +
      '.rtc-chip.bar .rtc-body{position:absolute;left:38px;right:10px;top:0;height:38px;display:flex;align-items:center;overflow:hidden}' +
      '.rtc-chip.bar .rtc-sum{font-size:12.5px;color:#e7edfa;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}' +
      // 方块:展开/结果 —— 有色磨砂 + 边缘阴影
      '.rtc-chip.card{width:274px;height:auto;border-radius:15px;border:1px solid var(--rc-line);' +
        'background:var(--rc-fill);backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);' +
        'box-shadow:0 20px 46px -18px rgba(0,0,0,.85),0 0 0 1px rgba(255,255,255,.04) inset}' +
      '.rtc-chip.card .rtc-body{display:block;padding:9px 12px 12px;margin-left:34px}' +
      '.rtc-chip.card .rtc-sum{font-size:13px;font-weight:650;color:#fff;margin-bottom:6px;line-height:1.35}' +
      '.rtc-rich{font-size:12.5px;color:#93a4c6;line-height:1.55}' +
      '.rtc-face{margin-top:7px;background:rgba(0,0,0,.28);border:1px solid rgba(255,255,255,.08);border-radius:8px;padding:7px 9px;color:#dbe4f5;' +
        'max-height:200px;overflow:auto;word-break:break-word}' +
      '.rtc-face img{max-width:100%;border-radius:6px;margin-top:5px;display:block}' +
      '.rtc-tag{font-size:9.5px;letter-spacing:.12em;text-transform:uppercase;color:#65769a;font-weight:700;margin-bottom:3px}' +
      '.rtc-cloze{background:rgba(123,108,255,.22);border-bottom:1.5px solid #7b6cff;border-radius:3px;padding:0 5px;color:#cdc6ff;font-weight:600}' +
      '.rtc-nav{display:flex;align-items:center;gap:7px;margin-top:8px}' +
      '.rtc-nav button{background:transparent;border:1px solid rgba(255,255,255,.14);border-radius:7px;color:#93a4c6;' +
        'width:26px;height:24px;cursor:pointer;font-size:13px;line-height:1;padding:0}' +
      '.rtc-nav button:disabled{opacity:.3}' +
      '.rtc-pgs{display:flex;gap:4px;flex:1;align-items:center}' +
      '.rtc-pg{width:6px;height:6px;border-radius:50%;background:rgba(255,255,255,.2)}' +
      '.rtc-pg.on{background:#fff;width:15px;border-radius:3px}' +
      // 步骤区(=原「!」详情面板:每一步都推出来)
      '.rtc-steps{margin-top:9px;border-top:1px solid rgba(255,255,255,.08);padding-top:7px}' +
      '.rtc-more{background:transparent;border:0;color:#7f92b8;font-size:11px;padding:0;cursor:pointer;display:flex;align-items:center;gap:4px}' +
      '.rtc-more:active{opacity:.6}' +
      '.rtc-step{display:flex;gap:7px;align-items:flex-start;margin-top:5px;font-size:11.5px;color:#a9b8d4}' +
      '.rtc-step i{flex:none;width:5px;height:5px;border-radius:50%;background:var(--rc-c);margin-top:5px;opacity:.85}' +
      '.rtc-step em{font-style:normal;color:#65769a;margin-left:auto;flex:none;font-variant-numeric:tabular-nums}' +
      '.rtc-kv{margin-top:6px}' +
      '.rtc-k{color:#7c93c4;font-size:10.5px;font-weight:700;margin-bottom:2px}' +
      '.rtc-v{color:#b9c6e0;font-family:ui-monospace,Menlo,monospace;font-size:11px;line-height:1.5;word-break:break-all;' +
        'white-space:pre-wrap;max-height:150px;overflow:auto}' +
      // 选中:紫边(全局广播)
      '.rtc-chip.sel{border-color:#7b6cff!important;' +
        'box-shadow:0 0 0 1px #7b6cff,0 0 22px -4px rgba(123,108,255,.65),0 16px 40px -18px rgba(0,0,0,.8)!important}' +
      // 交互态
      '.rtc-chip.press{transform:scale(.94)}' +
      '.rtc-chip.dragging{opacity:.42}' +
      '.rtc-chip.busy .rtc-ico{animation:rtcBr 1.6s ease-in-out infinite}' +
      '@keyframes rtcBr{0%,100%{opacity:.45}50%{opacity:1}}' +
      '.rtc-chip.err{--rc-c:#ff6961}' +
      '.rtc-chip.born{animation:rtcBorn .34s cubic-bezier(.2,.9,.3,1.25)}' +
      '@keyframes rtcBorn{from{transform:scale(.3);opacity:0}to{transform:scale(1);opacity:1}}' +
      '.rtc-chip.gone{animation:rtcGone .3s ease forwards}' +
      '@keyframes rtcGone{to{transform:scale(.4);opacity:0}}' +
      '.rtc-ghost{position:fixed;pointer-events:none;z-index:2147481500;opacity:.85;transform:scale(.96)}' +
      // 侧栏内(静态流式排布,不绝对定位)
      '.rtc-chip.inflow{position:relative;margin:7px 0;left:auto!important;top:auto!important}' +
      '.rtc-chip.inflow.card{width:100%;max-width:none}';
    document.head.appendChild(s);
  }

  // ── 全局状态 ──
  var chips = [];        // 所有逻辑 chip
  var byCid = {};        // cid → [所有视图 el](浮层/侧栏/副本)——选中广播用
  var selected = {};     // cid → true
  function host() {      // 浮层容器(绝对定位那批)
    var h = document.getElementById('rtc-chip-layer');
    if (!h) {
      h = document.createElement('div');
      h.id = 'rtc-chip-layer';
      h.style.cssText = 'position:fixed;inset:0;pointer-events:none;z-index:2147481400';
      document.body.appendChild(h);
    }
    return h;
  }
  function esc(s) { var d = document.createElement('div'); d.textContent = String(s == null ? '' : s); return d.innerHTML; }

  // ── 选中:全局广播(同 cid 处处高亮 / 处处取消)──
  function setSel(cid, on) {
    if (!cid) return;
    if (on) selected[cid] = true; else delete selected[cid];
    byCid[cid] = (byCid[cid] || []).filter(function (el) { return el && el.isConnected; });
    byCid[cid].forEach(function (el) { el.classList.toggle('sel', !!on); });
    try { window.dispatchEvent(new CustomEvent('rc-chip-sel', { detail: { cid: cid, on: !!on } })); } catch (e) {}
  }
  function toggleSel(cid) { setSel(cid, !selected[cid]); }
  function register(cid, el) {   // 任何渲染位置(浮层/侧栏/副本)都来登记,才能被广播命中
    if (!cid || !el) return;
    (byCid[cid] = byCid[cid] || []).push(el);
    if (selected[cid]) el.classList.add('sel');
  }
  function unregister(el) {
    Object.keys(byCid).forEach(function (k) {
      byCid[k] = (byCid[k] || []).filter(function (x) { return x !== el && x.isConnected; });
      if (!byCid[k].length) delete byCid[k];
    });
  }

  // ── 位置:交错重叠,落点按画面当前占用情况打分(避开已有 chip、不压正文左上)──
  function floatViews() {
    var out = [];
    chips.forEach(function (c) { c.views.forEach(function (v) { if (!v.inflow && v.el.isConnected) out.push(v); }); });
    return out;
  }
  function findSpot(w, h) {
    var W = window.innerWidth, H = window.innerHeight, pad = 16;
    var occupied = floatViews().map(function (v) {
      var r = v.el.getBoundingClientRect(); return { x: r.left, y: r.top, w: r.width, h: r.height };
    });
    var lim = W;   // 侧栏开着 → 落点限制在侧栏左侧
    try {
      var sd = document.getElementById('rc-side') || document.getElementById('ep-side') || document.getElementById('side-drawer');
      if (sd && sd.classList.contains('open')) {
        var sr = sd.getBoundingClientRect();
        if (sr.width > 0 && sr.left < W - 4) lim = sr.left - 10;
      }
    } catch (e) {}
    var best = null, bs = -1e9;
    for (var i = 0; i < 70; i++) {
      var x = pad + Math.random() * Math.max(10, (lim - w - pad * 2));
      var y = pad + Math.random() * Math.max(10, (H - h - pad * 2 - 100));   // 底部给字幕留位
      var minD = 1e9;
      occupied.forEach(function (p) {
        var dx = Math.max(p.x - (x + w), x - (p.x + p.w), 0);
        var dy = Math.max(p.y - (y + h), y - (p.y + p.h), 0);
        minD = Math.min(minD, Math.sqrt(dx * dx + dy * dy));
      });
      var s = occupied.length ? Math.min(minD, 150) : 100;
      s += (x / Math.max(1, lim)) * 45 + (y / H) * 25;   // 偏好右/下,别压正文左上
      if (s > bs) { bs = s; best = { x: x, y: y }; }
    }
    return best || { x: Math.max(8, W - w - 24), y: 90 };
  }

  // ── 方块内容 ──
  function ankiBody(chip, view) {
    var r = chip.result || {}, cards = r.cards || [];
    var i = Math.max(0, Math.min(view.idx || 0, cards.length - 1));
    var c = cards[i] || {};
    var faces;
    if ((c.type || 'basic') === 'cloze') {
      var cz = String(c.cloze || '').replace(/\{\{c\d+::([\s\S]*?)(?:::[\s\S]*?)?\}\}/g,
        function (_m, a) { return '<span class="rtc-cloze">' + a + '</span>'; });
      faces = '<div class="rtc-face"><div class="rtc-tag">填空</div>' + cz + '</div>';
    } else {
      faces = '<div class="rtc-face"><div class="rtc-tag">正面</div>' + (c.front || '') + '</div>' +
              '<div class="rtc-face"><div class="rtc-tag">背面</div>' + (c.back || '') + '</div>';
    }
    var pgs = cards.map(function (_x, k) { return '<span class="rtc-pg' + (k === i ? ' on' : '') + '"></span>'; }).join('');
    return '<div class="rtc-rich">' + faces +
      (cards.length > 1 ?
        '<div class="rtc-nav"><button class="rtc-prev"' + (i === 0 ? ' disabled' : '') + '>‹</button>' +
        '<span class="rtc-pgs">' + pgs + '</span>' +
        '<button class="rtc-next"' + (i >= cards.length - 1 ? ' disabled' : '') + '>›</button></div>' : '') +
      '</div>';
  }
  function stepsBody(chip, view) {
    var st = chip.steps || [], rows = chip.meta || [];
    if (!st.length && !rows.length) return '';
    var open = !!view.deep;
    var h = '<div class="rtc-steps"><button class="rtc-more">' + (open ? '▾' : '▸') + ' ' +
      (st.length ? st.length + ' 个步骤' : '调用详情') + '</button>';
    if (open) {
      h += st.map(function (s) {
        return '<div class="rtc-step"><i></i><span>' + esc(s.label || s) + '</span>' +
          (s.dt != null ? '<em>' + esc(s.dt) + 's</em>' : '') + '</div>';
      }).join('');
      h += rows.map(function (r) {
        return '<div class="rtc-kv"><div class="rtc-k">' + esc(r[0]) + '</div><div class="rtc-v">' + esc(r[1]) + '</div></div>';
      }).join('');
    }
    return h + '</div>';
  }
  function cardBody(chip, view) {
    var r = chip.result || {};
    var head, main;
    if (r.kind === 'anki' && (r.cards || []).length) {
      head = '已加 ' + (r.n || r.cards.length) + ' 张卡 · ' + esc(r.deck || 'QA');
      main = ankiBody(chip, view);
    } else {
      head = esc(chip.summary || chip.label || '完成');
      main = chip.detail ? '<div class="rtc-rich"><div class="rtc-face">' + esc(String(chip.detail).slice(0, 900)) + '</div></div>' : '';
    }
    return '<div class="rtc-sum">' + head + '</div>' + main + stepsBody(chip, view);
  }

  function paint(chip, view) {
    var el = view.el;
    el.className = 'rtc-chip ' + view.form + (chip.busy ? ' busy' : '') + (view.inflow ? ' inflow' : '') +
      (chip.failed ? ' err' : '') + (selected[chip.cid] ? ' sel' : '');
    el.style.setProperty('--rc-c', chip.failed ? '#ff6961' : (TYPE_C[chip.type] || TYPE_C.text));
    var body = '';
    if (view.form === 'bar') body = '<div class="rtc-sum">' + esc(chip.step || chip.summary || chip.label || '') + '</div>';
    else if (view.form === 'card') body = cardBody(chip, view);
    el.innerHTML = '<div class="rtc-ico">' + (SVG[chip.type] || SVG.gear) + '</div><div class="rtc-body">' + body + '</div>';
    if (view.form === 'card') {
      var p = el.querySelector('.rtc-prev'), n = el.querySelector('.rtc-next'), m = el.querySelector('.rtc-more');
      if (p) p.addEventListener('click', function (e) { e.stopPropagation(); view.idx = Math.max(0, (view.idx || 0) - 1); paint(chip, view); });
      if (n) n.addEventListener('click', function (e) { e.stopPropagation(); view.idx = (view.idx || 0) + 1; paint(chip, view); });
      if (m) m.addEventListener('click', function (e) { e.stopPropagation(); view.deep = !view.deep; paint(chip, view); });
      try { if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([el]); } catch (e) {}
    }
    if (view.inflow) { try { var th = el.parentNode; if (th) th.scrollTop = th.scrollHeight; } catch (e) {} }
  }
  function paintAll(chip) { chip.views.forEach(function (v) { if (v.el.isConnected) paint(chip, v); }); }

  function formsOf(chip, view) {   // 浮层上的执行类没有方块;侧栏里保留(点开=完成后的状态确认)
    return (isAction(chip.type) && !view.inflow) ? ['circle', 'bar'] : ['circle', 'bar', 'card'];
  }
  function setForm(chip, view, form) {
    var order = formsOf(chip, view);
    if (order.indexOf(form) < 0) form = order[order.length - 1];
    view.form = form;
    paint(chip, view);
  }

  // ── 手势:单击换形态 / 长按可拖(粘性)/ 原地松手=选中 ──
  function bindGestures(chip, view) {
    var el = view.el;
    var sx = 0, sy = 0, moved = false, longP = false, lpT = null, ghost = null, ox = 0, oy = 0;
    el.style.pointerEvents = 'auto';
    el.addEventListener('pointerdown', function (e) {
      if (e.target.closest && e.target.closest('button')) return;   // 卡片内翻页/展开钮不触发
      e.preventDefault();
      sx = e.clientX; sy = e.clientY; moved = false; longP = false;
      try { el.setPointerCapture(e.pointerId); } catch (_) {}
      lpT = setTimeout(function () { longP = true; el.classList.add('press'); }, LONG_PRESS);

      function mv(e2) {
        var dx = e2.clientX - sx, dy = e2.clientY - sy;
        if (!moved && (Math.abs(dx) + Math.abs(dy)) > STICKY) {   // 粘性阈值:没超过不算移动
          if (!longP) return;                                     // 未长按不拖(防误触/防和滚动打架)
          moved = true;
          chip.touched = 1;                                       // 动过 → 出结果不自动展开
          el.classList.add('dragging');
          ghost = el.cloneNode(true);
          ghost.className = 'rtc-ghost rtc-chip ' + view.form;
          ghost.style.setProperty('--rc-c', TYPE_C[chip.type] || TYPE_C.text);
          ghost.style.width = el.offsetWidth + 'px';
          document.body.appendChild(ghost);
          var r = el.getBoundingClientRect(); ox = sx - r.left; oy = sy - r.top;
        }
        if (moved && ghost) { ghost.style.left = (e2.clientX - ox) + 'px'; ghost.style.top = (e2.clientY - oy) + 'px'; }
      }
      function up(e3) {
        clearTimeout(lpT);
        el.removeEventListener('pointermove', mv); el.removeEventListener('pointerup', up); el.removeEventListener('pointercancel', up);
        el.classList.remove('press');
        if (moved) {
          if (ghost) { ghost.remove(); ghost = null; }
          el.classList.remove('dragging');
          if (view.inflow) {
            addFloatView(chip, e3.clientX - ox, e3.clientY - oy, view.form);   // 侧栏拖出 → 画面副本(原件留下)
          } else {
            var W = window.innerWidth, H = window.innerHeight;
            el.style.left = Math.max(6, Math.min(W - el.offsetWidth - 6, e3.clientX - ox)) + 'px';
            el.style.top = Math.max(6, Math.min(H - el.offsetHeight - 6, e3.clientY - oy)) + 'px';
          }
        } else if (longP) {
          chip.touched = 1;
          toggleSel(chip.cid);                                    // 长按原地松手 = 选中/取消(全局广播)
        } else {
          var order = formsOf(chip, view);
          chip.touched = 1;                                       // 手动换过形态 → 不再自动收起
          setForm(chip, view, order[(order.indexOf(view.form) + 1) % order.length]);
        }
      }
      el.addEventListener('pointermove', mv); el.addEventListener('pointerup', up); el.addEventListener('pointercancel', up);
    });
  }

  // ── 视图 ──
  function mkView(chip, opt) {
    var el = document.createElement('div');
    el.className = 'rtc-chip circle born' + (opt.inflow ? ' inflow' : '');
    el.dataset.cid = chip.cid;
    var view = { el: el, inflow: !!opt.inflow, form: 'circle', idx: 0, deep: false };
    chip.views.push(view);
    paint(chip, view);
    el.classList.add('born');
    register(chip.cid, el);
    bindGestures(chip, view);
    return view;
  }
  function addFloatView(chip, x, y, form) {
    var v = mkView(chip, { inflow: false });
    host().appendChild(v.el);
    setForm(chip, v, form || 'bar');
    v.el.style.left = Math.max(6, x || 20) + 'px';
    v.el.style.top = Math.max(6, y || 80) + 'px';
    return v;
  }

  // ── 创建:默认同时长出「浮层视图」+「侧栏视图」──
  function create(o) {
    injectCss();
    o = o || {};
    var chip = {
      cid: o.cid || ('t' + Date.now().toString(36) + Math.random().toString(36).slice(2, 5)),
      tool: o.tool || '', label: o.label || o.tool || '工具',
      type: o.type || typeOf(o.tool),
      busy: true, failed: false, touched: 0,
      step: '', summary: '', detail: '', result: null, steps: [], meta: [],
      views: []
    };
    chips.push(chip);

    if (o.floating !== false) {
      var fv = mkView(chip, { inflow: false });
      var spot = findSpot(38, 38);
      fv.el.style.left = spot.x + 'px'; fv.el.style.top = spot.y + 'px';
      host().appendChild(fv.el);
    }
    var mount = o.mount === undefined ? document.getElementById('asst-thread') : o.mount;
    if (mount) {                       // 侧栏轨道:同一套卡片/长条系统进对话流
      var sv = mkView(chip, { inflow: true });
      mount.appendChild(sv.el);
      setForm(chip, sv, 'bar');        // 侧栏默认长条(看得见在干什么)
      try { mount.scrollTop = mount.scrollHeight; } catch (e) {}
    }
    return chip;
  }

  // ── 生命周期 ──
  function progress(chip, step) {
    if (!chip) return;
    chip.busy = true;
    if (step) chip.step = step;
    chip.views.forEach(function (v) {
      if (v.form === 'circle' && !chip.touched) v.form = 'bar';   // 干活自动展成长条
    });
    paintAll(chip);
  }
  function done(chip, o) {
    if (!chip) return;
    o = o || {};
    chip.busy = false; chip.failed = false;
    if (o.result) chip.result = o.result;
    if (o.steps) chip.steps = o.steps;
    if (o.meta) chip.meta = o.meta;
    chip.summary = o.summary || chip.summary || chip.label;
    chip.detail = o.detail || chip.detail || '';
    chip.step = '';
    chip.views.forEach(function (v) {
      if (isAction(chip.type) && !v.inflow) {   // 浮层执行类:报一下就消失
        setForm(chip, v, 'bar');
        setTimeout(function () { dropView(chip, v); }, 1600);
        return;
      }
      if (chip.touched) { paint(chip, v); return; }   // 动过 → 尊重用户当前摆放,不自动展开
      setForm(chip, v, 'card');
      if (!v.inflow) {                                 // 浮层:20s 后自动收起成圆标记
        clearTimeout(v._ac);
        v._ac = setTimeout(function () {
          if (!chip.touched && v.form === 'card') setForm(chip, v, 'circle');
        }, AUTO_COLLAPSE);
      }
    });
  }
  function fail(chip, msg) {
    if (!chip) return;
    chip.busy = false; chip.failed = true;
    chip.summary = msg || '失败'; chip.step = msg || '失败';
    chip.views.forEach(function (v) {
      setForm(chip, v, v.form === 'card' ? 'card' : 'bar');
      if (!v.inflow) setTimeout(function () { if (v.form !== 'card') dropView(chip, v); }, 8000);
    });
  }
  function dropView(chip, view) {
    if (!view || !view.el) return;
    view.el.classList.add('gone');
    var el = view.el;
    setTimeout(function () {
      unregister(el);
      try { el.remove(); } catch (e) {}
      chip.views = chip.views.filter(function (v) { return v !== view; });
      if (!chip.views.length) chips = chips.filter(function (c) { return c !== chip; });
    }, 300);
  }
  function remove(chip) {
    if (!chip) return;
    chip.views.slice().forEach(function (v) { dropView(chip, v); });
  }
  function clearAll() {   // 清空对话时调用(与侧栏对话流同清)
    chips.slice().forEach(remove);
    selected = {}; byCid = {};
  }

  // ── 后台任务(制卡/笔记/生词):轮询步骤 → 长条滚动;完成 → 方块(完整卡片预览)──
  function track(chip, tid) {
    if (!chip || !tid) return;
    var n = 0;
    (function poll() {
      if (n++ > 240) { fail(chip, '等太久了'); return; }
      fetch('/api/voice/task-status?id=' + encodeURIComponent(tid))
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d || !d.ok) { setTimeout(poll, 1200); return; }
          if (d.status === 'done') {
            done(chip, { summary: d.speak || '完成', result: d.result || null, detail: d.speak || '', steps: d.steps || [] });
          } else if (d.status === 'error') {
            fail(chip, d.error || '失败');
          } else {
            if (d.steps) chip.steps = d.steps;   // 内部步骤实时推出来(方块「步骤」区)
            progress(chip, d.step || '处理中…');
            setTimeout(poll, 1200);
          }
        }).catch(function () { setTimeout(poll, 1600); });
    })();
  }

  RC.toolChip = {
    create: create, progress: progress, done: done, fail: fail, remove: remove, clearAll: clearAll, track: track,
    typeOf: typeOf, isAction: isAction,
    setSteps: function (chip, steps) { if (chip) { chip.steps = steps || []; paintAll(chip); } },
    setMeta: function (chip, rows) { if (chip) { chip.meta = rows || []; } },
    // 选中(全局广播:同 cid 处处高亮 / 处处取消)
    toggleSel: toggleSel, setSel: setSel, isSel: function (cid) { return !!selected[cid]; },
    register: register, unregister: unregister,
    selected: function () { return Object.keys(selected); },
    _chips: function () { return chips; }
  };
})();
