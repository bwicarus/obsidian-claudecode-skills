/* rc-toolchip.js — 工具活动指示器 v2(用户设计,2026-07-14)
 *
 * ⚠ 铁律(用户拍板):**不另造 UI**。用的就是现有那张语音卡片(`.vc-card`,DOM/CSS 在 rc-voicecall)——
 *   「我很喜欢这个方块的样式,在这个基础上进行修改就好」。本文件只是**状态机 + 内容渲染**;
 *   拖动 / 收藏 / TTS ▶ / ✕ / 紫边选中 全部复用 RC.voiceCard 暴露的那张卡。
 *
 * 形态(在原卡片的「方块 ↔ 长条」之上**加第三态:圆形标记**):
 *   圆(dot)    创建 / 收起 —— 透明玻璃标记、无边缘;图标呼吸 = 正在干活
 *   长条(min)  折叠 / 进行中 —— 原卡片折叠态,**内部步骤在这里滚**
 *   方块(full) 展开 / 结果   —— 原卡片展开态;制卡 = 完整卡片预览(正反面 / 公式 / 图)
 *   圆形标记本身 = 形态控制按钮(坐落在方块左上角),单击循环三态。
 *   **侧栏内联卡只有 长条 ↔ 方块 两态**(用户明确:侧栏不要圆)。
 *
 * 手势:长按 = 选中/取消(紫边,原 _pinBind);长按拖动 = 移动 / 拖出副本 / 拖到底部收藏(原 _dragToDock)。
 * 选中按 cid 全局广播:同一张卡在浮层 / 侧栏 / 收藏夹**处处高亮、处处取消**(_pinReg + _pinPaint)。
 *
 * 颜色 = 输出类型(紫色只表示选中,不参与类型编码):
 *   anki 制卡(单独色) / text / image / video / weather / news;
 *   action 执行类(翻页等):浮层上无方块、完成即消失;侧栏里保留(点开 = 完成后的状态确认)。
 */
(function () {
  'use strict';
  if (window.RC && RC.toolChip) return;
  window.RC = window.RC || {};

  var AUTO_COLLAPSE = 20000;   // 出结果 20s 后自动收起成圆标记(用户选)

  // ── 类型 → 主色 + 图标(SF 线条,currentColor)──
  var TYPE_C = {
    anki: '#39d98a', text: '#7b9cff', image: '#c77dff', video: '#ff7a59',
    weather: '#2dd4bf', news: '#fbbf24', action: '#8194b8'
  };
  var SVG = {
    anki: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"><rect x="2.4" y="3.6" width="8.6" height="9" rx="1.6"/><path d="M5 3.6V2.8a1 1 0 0 1 1-1h6.6a1 1 0 0 1 1 1v8a1 1 0 0 1-1 1h-.8"/><path d="M4.8 6.6h3.8M4.8 9h2.4"/></svg>',
    text: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9.8 3.2l3 3L6 13H3v-3z"/><path d="M8.4 4.6l3 3"/></svg>',
    image: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="2.2" y="3.2" width="11.6" height="9.6" rx="2"/><circle cx="5.6" cy="6.4" r="1.1"/><path d="M3 11.4l3.2-3 2.3 2 2.2-2.3 2.3 2.6"/></svg>',
    video: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><rect x="2" y="3.4" width="12" height="9.2" rx="2"/><path d="M6.6 6.4l3.6 1.9-3.6 1.9z" fill="currentColor" stroke="none"/></svg>',
    weather: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"><circle cx="6" cy="6.4" r="2.4"/><path d="M6 1.8v1.2M1.6 6.4h1.2M3 3.4l.85.85M9 3.4l-.85.85"/><path d="M6.4 12.6h5.2a2 2 0 1 0-.6-3.9 2.8 2.8 0 0 0-5.1 1.1" stroke-linejoin="round"/></svg>',
    news: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"><rect x="2.2" y="3.4" width="11.6" height="9.2" rx="1.6"/><path d="M4.6 6.2h4M4.6 8.4h6.8M4.6 10.4h6.8"/></svg>',
    action: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M3 8h9M8.6 4.2L12.4 8l-3.8 3.8"/></svg>'
  };
  function typeOf(tool) {
    var t = String(tool || '');
    if (/^(make_anki|add_vocab)/.test(t)) return 'anki';
    if (/^search_image/.test(t)) return 'image';
    if (/^search_video/.test(t)) return 'video';
    if (/weather/.test(t)) return 'weather';
    if (/news/.test(t)) return 'news';
    if (/^(goto|turn|highlight|epub_highlight|auto_highlight|mark|open_book|undo_last|wait_for_user)/.test(t)) return 'action';
    return 'text';
  }
  function isAction(t) { return t === 'action'; }
  function esc(s) { var d = document.createElement('div'); d.textContent = String(s == null ? '' : s); return d.innerHTML; }
  function VC() { return window.RC && RC.voiceCard; }

  var chips = [];

  // ── 方块正文:制卡 = 完整卡片预览;其余 = 结果文本。末尾接「步骤」区(= 原「!」详情面板内容)──
  function ankiHtml(chip, view) {
    var r = chip.result || {}, cards = r.cards || [];
    var i = Math.max(0, Math.min(view.idx || 0, cards.length - 1)), c = cards[i] || {};
    var faces;
    if ((c.type || 'basic') === 'cloze') {
      var cz = String(c.cloze || '').replace(/\{\{c\d+::([\s\S]*?)(?:::[\s\S]*?)?\}\}/g,
        function (_m, a) { return '<span class="vc-cz">' + a + '</span>'; });
      faces = '<div class="vc-fc"><div class="vc-fc-t">填空</div>' + cz + '</div>';
    } else {
      faces = '<div class="vc-fc"><div class="vc-fc-t">正面</div>' + (c.front || '') + '</div>' +
              '<div class="vc-fc"><div class="vc-fc-t">背面</div>' + (c.back || '') + '</div>';
    }
    var dots = cards.map(function (_x, k) { return '<span class="vc-fc-d' + (k === i ? ' on' : '') + '"></span>'; }).join('');
    return '<div style="font-weight:600;margin-bottom:2px">已加 ' + (r.n || cards.length) + ' 张卡 · ' + esc(r.deck || 'QA') + '</div>' +
      faces +
      (cards.length > 1
        ? '<div class="vc-fc-n"><button class="vc-fc-p"' + (i === 0 ? ' disabled' : '') + '>‹</button>' +
          '<span style="display:flex;gap:4px;flex:1;align-items:center">' + dots + '</span>' +
          '<button class="vc-fc-x2"' + (i >= cards.length - 1 ? ' disabled' : '') + '>›</button></div>'
        : '');
  }
  function stepsHtml(chip, view) {
    var st = chip.steps || [], rows = chip.meta || [];
    if (!st.length && !rows.length) return '';
    var h = '<div class="vc-stp"><button class="vc-stp-b">' + (view.deep ? '▾' : '▸') + ' ' +
      (st.length ? st.length + ' 个步骤' : '调用详情') + '</button>';
    if (view.deep) {
      h += st.map(function (s) { return '<div class="vc-stp-i"><i></i><span>' + esc(s.label || s) + '</span></div>'; }).join('');
      h += rows.map(function (r) { return '<div class="vc-stp-k">' + esc(r[0]) + '</div><div class="vc-stp-v">' + esc(r[1]) + '</div>'; }).join('');
    }
    return h + '</div>';
  }
  function paintBody(chip, view) {
    var bd = view.el.querySelector('.vc-card-bd');
    if (!bd) return;
    var r = chip.result || {};
    var main = (r.kind === 'anki' && (r.cards || []).length)
      ? ankiHtml(chip, view)
      : (chip.detail ? '<div class="vc-fc">' + esc(String(chip.detail).slice(0, 900)) + '</div>' : '');
    bd.innerHTML = main + stepsHtml(chip, view);
    bd.style.whiteSpace = 'normal';
    var p = bd.querySelector('.vc-fc-p'), n = bd.querySelector('.vc-fc-x2'), m = bd.querySelector('.vc-stp-b');
    if (p) p.addEventListener('click', function (e) { e.stopPropagation(); view.idx = Math.max(0, (view.idx || 0) - 1); paintBody(chip, view); });
    if (n) n.addEventListener('click', function (e) { e.stopPropagation(); view.idx = (view.idx || 0) + 1; paintBody(chip, view); });
    if (m) m.addEventListener('click', function (e) { e.stopPropagation(); view.deep = !view.deep; paintBody(chip, view); });
    try { if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([bd]); } catch (e) {}
  }
  function paintSum(chip) {   // 长条:进行中步骤 / 完成摘要;标记呼吸 = 正在干活
    chip.views.forEach(function (v) {
      var sm = v.el.querySelector('.vc-card-sum');
      if (sm) sm.textContent = chip.step || chip.summary || chip.label || '';
      var dot = v.el.querySelector('.vc-card-dot');
      if (dot) dot.classList.toggle('busy', !!chip.busy);
      v.el.classList.toggle('vc-busy', !!chip.busy);   // 创建/进行中=标记透明玻璃;完成=有色磨砂(一眼可辨)
      v.el.classList.toggle('vc-err', !!chip.failed);
    });
  }
  function form(chip, f) {
    var vc = VC(); if (!vc || !chip) return;
    chip.views.forEach(function (v) { vc.form(v.el, f); });
  }

  // ── 侧栏内联视图:同一张卡的样式,只有 长条 ↔ 方块(用户明确:侧栏不要圆)──
  function mkInflow(chip) {
    var vc = VC(), th = document.getElementById('asst-thread');
    if (!vc || !th) return null;
    var el = document.createElement('div');
    el.className = 'vc-card vc-inflow vc-typed vc-min';
    el.style.setProperty('--vc-tc', TYPE_C[chip.type] || TYPE_C.text);
    el.innerHTML = '<div class="vc-card-hd">' + esc(chip.label) +
      '<button type="button" class="vc-card-p" aria-label="念">▶</button>' +
      '<button type="button" class="vc-card-x" aria-label="移除">' +
      '<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M3 3l6 6M9 3l-6 6"/></svg></button></div>' +
      '<div class="vc-card-sum"></div><div class="vc-card-bd"></div>';
    var view = { el: el, inflow: true, idx: 0, deep: false };
    el.querySelector('.vc-card-hd').addEventListener('click', function (ev) {   // 点头部=折叠/展开(两态)
      if (ev.target.closest('button')) return;
      el.dataset.touched = '1';
      vc.form(el, el.classList.contains('vc-min') ? 'full' : 'min');
    });
    el.querySelector('.vc-card-x').addEventListener('click', function (ev) { ev.stopPropagation(); el.remove(); });
    el.querySelector('.vc-card-p').addEventListener('click', function (ev) {
      ev.stopPropagation();
      try { window.__vcTtsWarm && window.__vcTtsWarm(); } catch (e) {}
      try { window.__vcSpeakText && window.__vcSpeakText((el.querySelector('.vc-card-bd') || {}).textContent || ''); } catch (e) {}
    });
    vc.pinReg(el, chip.cid);   // 选中按 cid 全局同步(同号卡处处高亮/处处取消)
    if (!isAction(chip.type)) {   // 执行类(翻页等)不产出内容 → 不参与选中,但仍显示+可点开确认
      vc.pinBind(el, chip.label, function () { return (el.querySelector('.vc-card-bd') || {}).textContent || chip.summary || ''; });
    }
    vc.dragToDock(el, function () {   // 长按拖出=副本/收藏(原件留在侧栏)
      return { label: chip.label, raw: (el.querySelector('.vc-card-bd') || {}).innerHTML || '', isHtml: true, cid: chip.cid };
    });
    th.appendChild(el);
    try { th.scrollTop = th.scrollHeight; } catch (e) {}
    return view;
  }

  // ── 生命周期 ──
  function create(o) {
    o = o || {};
    var vc = VC();
    var type = o.type || typeOf(o.tool);
    var chip = {
      cid: o.cid || (vc ? vc.mkCid() : 't' + Date.now().toString(36) + Math.random().toString(36).slice(2, 5)),
      tool: o.tool || '', label: o.label || o.tool || '工具', type: type,
      busy: true, failed: false,
      step: '', summary: '', detail: '', result: null, steps: [], meta: [],
      views: [], card: null
    };
    chips.push(chip);
    if (vc && o.floating !== false) {   // 浮层:出生 = 透明玻璃圆标记
      var c = vc.push('', chip.label, true, true, chip.cid, {
        tool: chip.tool, type: TYPE_C[type] || TYPE_C.text, icon: SVG[type] || SVG.text,
        dot: true, noAuto: true
      });
      if (c) {
        chip.card = c;
        c.el.classList.add('vc-typed');
        chip.views.push({ el: c.el, inflow: false, idx: 0, deep: false });
        if (!isAction(type)) {   // 长按=选中/取消(紫边,按 cid 全局广播);执行类不参与选中
          vc.pinBind(c.el, chip.label, function () { return (c.el.querySelector('.vc-card-bd') || {}).textContent || chip.summary || ''; });
        }
      }
    }
    if (o.mount !== null) {            // 侧栏:同一张卡进对话流(长条起手)
      var iv = mkInflow(chip);
      if (iv) chip.views.push(iv);
    }
    paintSum(chip);
    return chip;
  }
  function progress(chip, step) {
    if (!chip) return;
    chip.busy = true;
    if (step) chip.step = step;
    var vc = VC();
    chip.views.forEach(function (v) {          // 干活 → 展成长条(步骤在长条里滚)
      if (v.el.dataset.touched === '1') return;   // 用户动过 → 尊重当前形态
      vc && vc.form(v.el, 'min');
    });
    paintSum(chip);
  }
  function done(chip, o) {
    if (!chip) return;
    o = o || {};
    var vc = VC();
    chip.busy = false; chip.failed = false;
    if (o.result) chip.result = o.result;
    if (o.steps) chip.steps = o.steps;
    if (o.meta) chip.meta = o.meta;
    chip.summary = o.summary || chip.summary || chip.label;
    chip.detail = o.detail || chip.detail || '';
    chip.step = '';
    paintSum(chip);
    chip.views.forEach(function (v) {
      paintBody(chip, v);
      if (isAction(chip.type) && !v.inflow) {   // 执行类(不产出内容):**完成即消失**——报一下就走人
        vc && vc.form(v.el, 'min');              // (只有出错才留在页面上,见 fail();侧栏那条记录保留)
        setTimeout(function () { if (chip.card) vc && vc.close(chip.card); }, 1500);
        return;
      }
      if (v.el.dataset.touched === '1') return;   // 动过 → 不自动展开
      vc && vc.form(v.el, 'full');
      if (!v.inflow) {                            // 浮层:20s 后自动收起成圆标记
        clearTimeout(v._t);
        v._t = setTimeout(function () {
          if (v.el.dataset.touched !== '1' && !v.el.classList.contains('vc-picked')) vc && vc.form(v.el, 'dot');
        }, AUTO_COLLAPSE);
      }
    });
  }
  function fail(chip, msg) {
    if (!chip) return;
    var vc = VC();
    chip.busy = false; chip.failed = true;
    chip.summary = chip.step = msg || '失败';
    chip.detail = msg || '失败';
    paintSum(chip);
    // 用户设计:出错的卡**留在页面上**(不自动消失,包括执行类)——点开看错误详情
    chip.views.forEach(function (v) {
      v.el.classList.add('vc-canfull');   // 执行类平时没有方块,出错时开放展开(为了看 error)
      paintBody(chip, v);
      if (v.el.dataset.touched !== '1') vc && vc.form(v.el, 'min');
    });
  }
  function remove(chip) {
    if (!chip) return;
    if (chip.card) VC() && VC().close(chip.card);
    chip.views.forEach(function (v) { if (v.inflow) { try { v.el.remove(); } catch (e) {} } });
    chips = chips.filter(function (c) { return c !== chip; });
  }
  function clearAll() { chips.slice().forEach(remove); }

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
          if (d.status === 'done') done(chip, { summary: d.speak || '完成', result: d.result || null, detail: d.speak || '', steps: d.steps || [] });
          else if (d.status === 'error') fail(chip, d.error || '失败');
          else { if (d.steps) chip.steps = d.steps; progress(chip, d.step || '处理中…'); setTimeout(poll, 1200); }
        }).catch(function () { setTimeout(poll, 1600); });
    })();
  }

  RC.toolChip = {
    create: create, progress: progress, done: done, fail: fail, remove: remove, clearAll: clearAll, track: track,
    typeOf: typeOf, isAction: isAction, setForm: form,
    setSteps: function (chip, st) { if (chip) { chip.steps = st || []; chip.views.forEach(function (v) { paintBody(chip, v); }); } },
    setMeta: function (chip, rows) { if (chip) chip.meta = rows || []; },
    _chips: function () { return chips; }
  };
})();
