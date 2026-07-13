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
  // 用户口径:「不会留下**实际内容**的工具」= 只改状态 / 只是取数喂给 AI(读页面、翻页、看图、
  //   查目录、搜书内、取笔记…)→ 归 action:进行中显示,**完成立刻消失**(不留方块、不计时)。
  //   真正留内容给用户看的才留卡:制卡 / 配图 / 视频 / 天气 / 新闻 / 文字结果(翻译·查词·总结·深度思考·路由长答)。
  function typeOf(tool) {
    var t = String(tool || '');
    if (/^(make_anki|add_vocab)/.test(t)) return 'anki';
    if (/^search_image/.test(t)) return 'image';
    if (/^search_video/.test(t)) return 'video';
    if (/weather/.test(t)) return 'weather';
    if (/news/.test(t)) return 'news';
    if (/^(translate|lookup_word|summarize|deep_think|route_to_text|make_note|notes_create|notes_edit|web_search)/.test(t)) return 'text';
    if (/^(goto|turn|highlight|epub_highlight|auto_highlight|mark|open_book|undo_last|wait_for_user|read_|see_|toc|list_sections|find_highlights|search_book|search_all_books|notes_query|notes_read|page_vocab|section_vocab|recall)/.test(t)) return 'action';
    return 'text';
  }
  function isAction(t) { return t === 'action'; }
  // 标记图标按**工具语义**选(颜色仍按输出类型):看图=眼睛 / 读=书页 / 翻页=箭头 / 搜索=放大镜 / 高亮=笔 …
  var TICON = {
    eye:  '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4"><path d="M1.8 8s2.3-4.2 6.2-4.2S14.2 8 14.2 8s-2.3 4.2-6.2 4.2S1.8 8 1.8 8z"/><circle cx="8" cy="8" r="1.9"/></svg>',
    read: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"><path d="M3 2.8h7.5L13 5.3v8H3z"/><path d="M5.4 7h5.2M5.4 9.3h5.2M5.4 11.6h3.4"/></svg>',
    find: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="7" cy="7" r="4.2"/><path d="M10.3 10.3L13.6 13.6"/></svg>',
    pen:  '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" stroke-linecap="round"><path d="M9.8 3.2l3 3L6 13H3v-3z"/><path d="M8.4 4.6l3 3"/></svg>',
    dict: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"><path d="M3.5 2.5h9v11h-9a1.2 1.2 0 0 1 0-2.4h9"/><path d="M6.2 8.6L8 4.8l1.8 3.8M6.7 7.6h2.6"/></svg>',
    net:  '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3"><circle cx="8" cy="8" r="5.6"/><path d="M2.4 8h11.2M8 2.4c-3.4 3.4-3.4 7.8 0 11.2M8 2.4c3.4 3.4 3.4 7.8 0 11.2"/></svg>'
  };
  function iconOf(tool, type) {
    var t = String(tool || '');
    if (/^see_/.test(t)) return TICON.eye;
    if (/^(read_|toc|list_sections|notes_read)/.test(t)) return TICON.read;
    if (/^(search_book|search_all_books|find_highlights|recall|notes_query)/.test(t)) return TICON.find;
    if (/^(highlight|epub_highlight|auto_highlight|mark)/.test(t)) return TICON.pen;
    if (/^(lookup_word|translate|page_vocab|section_vocab)/.test(t)) return TICON.dict;
    if (/^web_search/.test(t)) return TICON.net;
    return SVG[type] || SVG.text;
  }
  function esc(s) { var d = document.createElement('div'); d.textContent = String(s == null ? '' : s); return d.innerHTML; }
  function VC() { return window.RC && RC.voiceCard; }
  function hdSplit(el, label, act) {   // 头部:纯文本标题 → <标题><状态> + 【数据流】按钮
    var hd = el.querySelector('.vc-card-hd');
    if (!hd || hd.querySelector('.vc-hd-l')) return;
    try { VC() && VC().css && VC().css(); } catch (e) {}
    var l = document.createElement('span'); l.className = 'vc-hd-l'; l.textContent = label;
    var st = document.createElement('span'); st.className = 'vc-hd-s';
    if (hd.firstChild && hd.firstChild.nodeType === 3) hd.replaceChild(l, hd.firstChild);
    else hd.insertBefore(l, hd.firstChild);
    hd.insertBefore(st, l.nextSibling);
    if (act) el.classList.add('vc-act');
    // 标题栏默认按钮 = 数据流(用户要求:侧栏 / 字幕模式的卡都有这一个)。点=展开/收起流程图。
    var b = document.createElement('button');
    b.type = 'button'; b.className = 'vc-flowb'; b.title = '数据流(这个结果是怎么来的)';
    b.innerHTML = ICON.flow;
    b.addEventListener('click', function (ev) {
      ev.stopPropagation();
      el.dataset.touched = '1';
      var vc = VC(); if (!vc) return;
      var open = !el.classList.contains('vc-min') && !el.classList.contains('vc-dot');
      vc.form(el, open ? 'min' : 'full');
      b.classList.toggle('on', !open);
    });
    hd.appendChild(b);
  }

  var chips = [];

  // ── 方块正文:制卡 = 完整卡片预览;其余 = 结果文本。末尾接「步骤」区(= 原「!」详情面板内容)──
  function ankiHtml(chip, view) {
    var r = chip.result || {}, cards = r.cards || [];
    var i = Math.max(0, Math.min(view.idx || 0, cards.length - 1)), c = cards[i] || {};
    var faces;
    if ((c.type || 'basic') === 'cloze') {
      var cz = mdInline(String(c.cloze || '')).replace(/\{\{c\d+::([\s\S]*?)(?:::[\s\S]*?)?\}\}/g,
        function (_m, a) { return '<span class="vc-cz">' + a + '</span>'; });
      faces = '<div class="vc-fc"><div class="vc-fc-t">填空</div>' + cz + '</div>';
    } else {
      faces = '<div class="vc-fc"><div class="vc-fc-t">正面</div>' + mdInline(c.front || '') + '</div>' +
              '<div class="vc-fc"><div class="vc-fc-t">背面</div>' + mdInline(c.back || '') + '</div>';
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
  // ── 展开视图 = 数据流图(用户设计):AI → 各处理步骤 → 结果,每个小方块可点开看它经手的数据 ──
  //    载荷用 markdown/MathJax 正常渲染(之前直接 esc() 出来一坨纯文本,根本没法读)。
  // 流程图节点图标:Apple 简约线条 SVG(currentColor),与项目其余图标同一风格——不要 emoji
  var ICON = {
    ai:   '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linejoin="round"><path d="M6.4 2.2l1.05 2.75L10.2 6l-2.75 1.05L6.4 9.8 5.35 7.05 2.6 6l2.75-1.05z"/><path d="M11.4 9l.62 1.63L13.65 11.25l-1.63.62L11.4 13.5l-.62-1.63L9.15 11.25l1.63-.62z"/></svg>',
    step: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.35"><circle cx="8" cy="8" r="2.1"/><path d="M8 2.7v1.5M8 11.8v1.5M2.7 8h1.5M11.8 8h1.5M4.3 4.3l1.05 1.05M10.65 10.65l1.05 1.05M11.7 4.3l-1.05 1.05M5.35 10.65L4.3 11.7" stroke-linecap="round"/></svg>',
    out:  '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M3.2 8.4l3 3 6.6-7"/></svg>',
    err:  '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M8 2.9l5.6 9.8H2.4z"/><path d="M8 6.4v2.6M8 11h.01"/></svg>',
    // 数据流:方块 →(箭头)→ 方块,一眼看懂"数据从哪流到哪"(小尺寸下也不会误读成发送箭头)
    flow: '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.35" stroke-linecap="round" stroke-linejoin="round"><rect x="1.8" y="2" width="5.4" height="3.8" rx="1.1"/><rect x="8.8" y="10.2" width="5.4" height="3.8" rx="1.1"/><path d="M4.5 5.8v3.1a1.2 1.2 0 0 0 1.2 1.2h3.1"/><path d="M7.6 8.7l1.4 1.4-1.4 1.4"/></svg>'
  };
  function stages(chip) {
    var m = {};
    (chip.meta || []).forEach(function (r) { m[r[0]] = r[1]; });
    var st = [];
    // ① AI 请求:它说了什么 / 带了什么上下文 / 传了什么参数(全部 markdown 渲染)
    var ai = '';
    if (m['指令(S2S 原话)']) ai += '**指令**:' + m['指令(S2S 原话)'] + '\n\n';
    if (m['携带上下文']) ai += '**携带上下文**:' + m['携带上下文'] + '\n\n';
    var args = m['参数'] || '';
    if (args) {
      try { args = JSON.stringify(JSON.parse(args), null, 1); } catch (e) {}
      ai += '**参数**\n\n```\n' + args + '\n```';
    }
    st.push({ ic: ICON.ai, t: 'AI 请求 · ' + chip.label, m: '', kind: 'md', body: ai || '(无参数)' });
    // ② 中间步骤
    (chip.steps || []).forEach(function (x) {
      st.push({ ic: ICON.step, t: x.label || String(x), m: (x.dt != null ? x.dt + 's' : ''),
                kind: 'md', body: x.detail || '' });
    });
    // ③ 结果(默认展开)
    st.push({ ic: chip.failed ? ICON.err : ICON.out,
              t: chip.failed ? '出错' : (chip.result && chip.result.kind === 'anki' ? '生成卡片' : '结果'),
              m: m['耗时'] || '',
              kind: (chip.result && chip.result.kind === 'anki') ? 'anki' : 'md',
              tts: !(chip.result && chip.result.kind === 'anki') && !chip.failed,   // 纯文字结果才给 ▶
              body: chip.detail || chip.summary || '', open: true });
    return st;
  }
  function renderPayload(el, sg, chip, view) {
    if (sg.kind === 'anki') { el.innerHTML = ankiHtml(chip, view); wireAnki(el, chip, view); return; }
    var body = prettyResult(sg.body);
    if (!body.trim()) { el.innerHTML = '<span style="color:#7f92b8">(没有额外内容)</span>'; return; }
    try {
      if (window.RC && RC.assistant && RC.assistant.renderMd) RC.assistant.renderMd(el, body, true);   // 主路:与对话同一套 md + MathJax
      else el.innerHTML = miniMd(body);                                                                // 兜底:绝不吐纯文本
    } catch (e) { el.innerHTML = miniMd(body); }
    try { if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([el]); } catch (e) {}
    if (sg.tts) addTts(el);   // 纯文字的最终结果 → 文字区域角落放一个 ▶(TTS 念它)
  }
  function prettyResult(t) {   // 工具回给模型的常是 JSON(截图里那坨 {"kind":"weather","note":...})→ 抽人话,别倒原文
    var raw = String(t || '');
    var o = null;
    try { o = JSON.parse(raw); } catch (e) { return raw; }
    if (!o || typeof o !== 'object') return raw;
    var pick = o.note || o.speak || o.summary || o.text || o.brief || o.result;
    if (typeof pick === 'string' && pick.trim()) return pick;
    var lines = [];
    Object.keys(o).forEach(function (k) {
      if (k.charAt(0) === '_' || o[k] == null || o[k] === false) return;
      var v = o[k];
      if (typeof v === 'object') { try { v = JSON.stringify(v); } catch (e) { v = ''; } }
      lines.push('- **' + k + '**:' + v);
    });
    return lines.join('\n') || raw;
  }
  function addTts(el) {   // 用户设计:唯一留 ▶ 的地方——纯文字结果那块内容的角落
    if (el.querySelector('.vc-fp-tts')) return;
    el.style.position = 'relative';
    var b = document.createElement('button');
    b.type = 'button'; b.className = 'vc-fp-tts'; b.title = '念一遍';
    b.innerHTML = '<svg viewBox="0 0 16 16" fill="currentColor"><path d="M5.6 3.6l6 4.4-6 4.4z"/></svg>';
    b.addEventListener('click', function (ev) {
      ev.stopPropagation();
      try { window.__vcTtsWarm && window.__vcTtsWarm(); } catch (e) {}
      try { window.__vcSpeakText && window.__vcSpeakText(el.textContent || ''); } catch (e) {}
    });
    el.appendChild(b);
  }
  function miniMd(t) {   // 兜底 markdown(侧栏未挂载时也不能吐原始文本):代码块/标题/列表/粗斜体/行内码
    var fences = [];
    var h = String(t || '').replace(/```[a-z]*\n([\s\S]*?)```/g, function (_m, code) {
      fences.push(code); return '\u0000F' + (fences.length - 1) + '\u0000';
    });
    h = esc(h)
      .replace(/`([^`\n]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    h = h.split(/\n{2,}/).map(function (blk) {
      if (/^#{1,4}\s/.test(blk)) return '<p><strong>' + blk.replace(/^#{1,4}\s*/, '') + '</strong></p>';
      if (/^\s*[-*·]\s+/m.test(blk)) {
        return '<ul>' + blk.split('\n').map(function (li) {
          return li.trim() ? '<li>' + li.replace(/^\s*[-*·]\s+/, '') + '</li>' : '';
        }).join('') + '</ul>';
      }
      return '<p>' + blk.replace(/\n/g, '<br>') + '</p>';
    }).join('');
    h = h.replace(/\u0000F(\d+)\u0000/g, function (_m, i) { return '<pre>' + esc(fences[+i]) + '</pre>'; });
    return h;
  }
  function mdInline(t) {   // Anki 卡面 = AI 产的富文本:已有 HTML(<img> 等)原样保留,markdown 补渲,$公式$ 交给 MathJax
    var h = String(t || '');
    if (!/[*`_]/.test(h)) return h;
    return h.replace(/`([^`\n]+)`/g, '<code>$1</code>')
            .replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>')
            .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
  }
  function wireAnki(bd, chip, view) {
    var p = bd.querySelector('.vc-fc-p'), n = bd.querySelector('.vc-fc-x2');
    if (p) p.addEventListener('click', function (e) { e.stopPropagation(); view.idx = Math.max(0, (view.idx || 0) - 1); paintBody(chip, view); });
    if (n) n.addEventListener('click', function (e) { e.stopPropagation(); view.idx = (view.idx || 0) + 1; paintBody(chip, view); });
    try { if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([bd]); } catch (e) {}
  }
  function paintBody(chip, view) {
    var bd = view.el.querySelector('.vc-card-bd');
    if (!bd) return;
    bd.style.whiteSpace = 'normal';
    var sgs = stages(chip);
    if (!view.open) view.open = {};
    bd.innerHTML = '<div class="vc-flow">' + sgs.map(function (sg, i) {
      var on = (view.open[i] === undefined ? !!sg.open : view.open[i]);
      return (i ? '<div class="vc-fw"></div>' : '') +
        '<div class="vc-fn' + (on ? ' on' : '') + '" data-i="' + i + '">' +
          '<span class="vc-fn-i">' + sg.ic + '</span>' +
          '<span class="vc-fn-t">' + esc(sg.t) + '</span>' +
          (sg.m ? '<span class="vc-fn-m">' + esc(sg.m) + '</span>' : '') +
          '<span class="vc-fn-x">▸</span>' +
        '</div>' +
        '<div class="vc-fp" data-p="' + i + '"' + (on ? '' : ' hidden') + '></div>';
    }).join('') + '</div>';
    sgs.forEach(function (sg, i) {
      var pane = bd.querySelector('.vc-fp[data-p="' + i + '"]');
      var on = (view.open[i] === undefined ? !!sg.open : view.open[i]);
      if (on && pane) renderPayload(pane, sg, chip, view);
    });
    bd.querySelectorAll('.vc-fn').forEach(function (nd) {
      nd.addEventListener('click', function (ev) {
        ev.stopPropagation();
        var i = +nd.getAttribute('data-i');
        var pane = bd.querySelector('.vc-fp[data-p="' + i + '"]');
        var now = !nd.classList.contains('on');
        view.open[i] = now;
        nd.classList.toggle('on', now);
        if (!pane) return;
        pane.hidden = !now;
        if (now) renderPayload(pane, sgs[i], chip, view);
      });
    });
  }
  function paintSum(chip) {   // 长条:进行中步骤 / 完成摘要;标记呼吸 = 正在干活
    chip.views.forEach(function (v) {
      var txt = chip.step || chip.summary || chip.label || '';
      var sm = v.el.querySelector('.vc-card-sum');
      if (sm) sm.textContent = txt;
      var hs = v.el.querySelector('.vc-hd-s');     // 一行长条:状态就显示在标题旁边
      if (hs) hs.textContent = (txt && txt !== chip.label) ? ('· ' + txt) : '';
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
    el.className = 'vc-card vc-inflow vc-hasdot vc-typed vc-min';   // hasdot=套用同一套一行长条/展开规则
    el.style.setProperty('--vc-tc', TYPE_C[chip.type] || TYPE_C.text);
    // 侧栏里的工具卡 = 对话记录本身 → **不要 ▶ 播放、不要 ✕ 删除**(用户明确)
    el.innerHTML = '<div class="vc-card-hd">' + esc(chip.label) + '</div>' +
      '<div class="vc-card-sum"></div><div class="vc-card-bd"></div>';
    var view = { el: el, inflow: true, idx: 0, deep: false };
    hdSplit(el, chip.label, isAction(chip.type));
    el.querySelector('.vc-card-hd').addEventListener('click', function (ev) {   // 点头部=折叠/展开(两态)
      if (ev.target.closest('button')) return;
      el.dataset.touched = '1';
      vc.form(el, el.classList.contains('vc-min') ? 'full' : 'min');
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
        tool: chip.tool, type: TYPE_C[type] || TYPE_C.text, icon: iconOf(chip.tool, type),
        dot: true, noAuto: true
      });
      if (c) {
        chip.card = c;
        c.el.classList.add('vc-typed');
        hdSplit(c.el, chip.label, isAction(type));
        // 字幕浮层的工具卡也**不要 ▶ / ✕**(与侧栏一致,用户要求):它是过程指示,不是可播可删的内容卡。
        //   收起靠点头部/标记;唯一保留 ▶ 的地方 = 纯文字结果那块内容的角落(见 renderPayload)。
        ['.vc-card-p', '.vc-card-x'].forEach(function (q) { var b = c.el.querySelector(q); if (b) b.remove(); });
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
    if (chip.absorbed) { repaintFlows(chip); return; }   // 已被结果卡吸收:没有自己的视图,只刷流程图
    chip.views.forEach(function (v) {
      paintBody(chip, v);
      if (isAction(chip.type) && !v.inflow) {   // 执行类:**立刻消失**(不留小方块、不计时)——用户要求
        if (chip.card) vc && vc.close(chip.card);
        return;
      }
      // 其余:出结果**不自动展开**(用户要求)——停在长条,想看点头部/标记再展开。
      //   形态节奏:创建=标记 → 有进展=长条 → 出结果仍是长条(只是内容/颜色变了)。
      if (v.el.dataset.touched === '1') return;
      vc && vc.form(v.el, 'min');
    });
  }
  function fail(chip, msg) {
    if (!chip) return;
    var vc = VC();
    chip.busy = false; chip.failed = true;
    chip.summary = chip.step = msg || '失败';
    chip.detail = msg || '失败';
    paintSum(chip);
    if (chip.absorbed) { repaintFlows(chip); return; }
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

  // ── 结果卡吸收(用户设计)──
  //   天气/网络搜索/配图/视频 这类工具**本来就有自己的结果卡**(renderInfo)。以前工具指示器还会
  //   另造一张 → 字幕模式一次弹两张。现在:结果卡是唯一显示,工具卡被它吸收,
  //   标题栏加一个「流程」按钮,点开在卡片内展开这条线性流程图(AI → 工具 → 结果)。
  function absorb(hosts) {
    hosts = [].concat(hosts || []).filter(Boolean);
    if (!hosts.length) return;
    var chip = null;                       // 认领:最近一个还没被吸收的 chip(工具是串行的)
    for (var i = chips.length - 1; i >= 0; i--) { if (!chips[i].absorbed) { chip = chips[i]; break; } }
    if (!chip) return;
    chip.absorbed = [];
    chip.views.slice().forEach(function (v) {   // 撤掉它自己的两个视图(浮层 + 侧栏)
      if (v.inflow) { try { v.el.remove(); } catch (e) {} }
    });
    if (chip.card) { try { VC().close(chip.card); } catch (e) {} }
    chip.views = [];
    hosts.forEach(function (h) { chip.absorbed.push(h); mountFlow(h, chip); });
  }
  function mountFlow(host, chip) {
    try { VC() && VC().css && VC().css(); } catch (e) {}   // 131:样式必须在,否则裸 <button> = 白块
    var hd = host.querySelector('.vc-if-hd') || host.querySelector('.vc-card-hd');
    if (!hd || hd.querySelector('.vc-flowb')) return;
    var b = document.createElement('button');
    b.type = 'button'; b.className = 'vc-flowb'; b.title = '看这个结果是怎么来的(工具流程)';
    b.innerHTML = ICON.flow;
    var box = document.createElement('div');
    box.className = 'vc-flowbox'; box.hidden = true;
    var view = { el: box, inflow: true, idx: 0, open: {}, host: 1 };
    b.addEventListener('click', function (ev) {
      ev.stopPropagation();
      box.hidden = !box.hidden;
      b.classList.toggle('on', !box.hidden);
      if (!box.hidden) paintFlow(chip, view);
    });
    hd.appendChild(b);
    host.appendChild(box);
    chip._flows = chip._flows || [];
    chip._flows.push(view);
  }
  function paintFlow(chip, view) {   // 复用同一套流程图渲染(view.el 直接当画布)
    var fake = { el: { querySelector: function (q) { return q === '.vc-card-bd' ? view.el : null; } },
                 inflow: true, idx: view.idx, deep: false, open: view.open };
    paintBody(chip, fake);
    view.idx = fake.idx;
  }
  function repaintFlows(chip) { (chip._flows || []).forEach(function (v) { if (!v.el.hidden) paintFlow(chip, v); }); }

  RC.toolChip = {
    absorb: absorb,
    create: create, progress: progress, done: done, fail: fail, remove: remove, clearAll: clearAll, track: track,
    typeOf: typeOf, isAction: isAction, setForm: form,
    setSteps: function (chip, st) { if (chip) { chip.steps = st || []; chip.views.forEach(function (v) { paintBody(chip, v); }); } },
    setMeta: function (chip, rows) { if (chip) chip.meta = rows || []; },
    _chips: function () { return chips; }
  };
})();
