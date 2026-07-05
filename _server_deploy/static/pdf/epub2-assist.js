/* epub2-assist.js — epub.js 版 EPUB 阅读器的「助手侧栏(P8)」= 忠实移植 PDF reader.src/25-assistant.js
 *
 * 设计:**整文件按 PDF 的 25-assistant.js 重写,只换底座**(PDF=char 层+页+goToPage+DOM 偏移锚高亮;
 *       EPUB=epub.js iframe + section idx + R.display(idx) + CFI 注解高亮)。除底座外行为与 PDF 完全一致:
 *   · SSE 逐 event 处理(meta/done/tool/tool-done/answer/notice/actions/trace/task/undo/error);
 *   · answer 事件:剥 [[FOLLOWUP]](含未闭合容错)+ 流式轻量渲染(不每 chunk MathJax)+ 逐字浮现 reveal(揭示游标);
 *     收尾:剥 FOLLOWUP → 完整 MathJax 渲一次 → 追问 chip → 反馈条/追问错峰淡入;
 *   · actions 事件实时 runActions(工具一完成立即应用);
 *   · 感叹号「!」反馈弹窗(trace → 这条回答经过的 AI 调用 + ⚙ 模型设置);
 *   · 上下文卡 contextCard(选中 + 当前章节,公式走 MathJax);
 *   · 模型设置 ⚙(按功能配 后端/型号/深度,服务端预设,跟 PDF 助手共用一份)+ 读 RC.settings.aiParams();
 *   · 连续语音 mic / 撤销卡 / 后台任务轮询 / 历史恢复 / 断线 rid 重连 全部照搬。
 *
 * **自包含**:不依赖 rc-assistant.js(模板未加载它);splitFollowups/renderFollowups/contextCard/openModelSettings
 *   全部内联,并回写 window.RC.assistant + window.openModelSettings,使设置面板(rc-settings.js)的
 *   「⚙ 打开 AI 模型设置」按钮也能用同一份实现。后端复用 /pdf/api/epub-assistant(SSE)+ /pdf/api/epub-convo(历史),不改后端契约。
 *
 * 接入(模板已就绪):epub2.js 之后 <script src="/static/pdf/epub2-assist.js">。DOM 在 epub_reader.html:
 *   #ep-ai-body(对话区)/ #ep-asst-quick(底部快捷)/ #ep-ai-ta / #ep-ai-mic / #ep-ai-send。
 */
(function () {
  'use strict';
  function ready(fn) {
    if (window.__epub && window.__epub.rendition && window.RC) fn();
    else setTimeout(function () { ready(fn); }, 120);
  }
  ready(init);

  function init() {
    if (window.__epAsstLoaded) return; window.__epAsstLoaded = true;
    var R = window.__epub.rendition, B = window.__epub.book, CFG = window.__epub.cfg || {};
    var $ = function (id) { return document.getElementById(id); };
    var FREL = CFG.fileRel || '';
    var aiBody = $('ep-ai-body');
    if (!aiBody) return;

    // ── 共享工具(esc / toast / reqJson / markdown×MathJax / dbg)── 走统一控制层 RC(缺了有内联兜底)
    function esc(s) { return (window.RC && RC.esc) ? RC.esc(s) : String(s == null ? '' : s).replace(/[&<>"]/g, function (c) { return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]; }); }
    function toast(m) { try { if (window.RC && RC.toast) RC.toast(m); } catch (e) {} }
    function renderMdRaw(s) { if (window.RC && RC.md) return RC.md(s); return String(s == null ? '' : s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\n/g, '<br>'); }
    function typeset(el) { if (window.RC && RC.typeset) { RC.typeset(el); return; } try { if (el && window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([el]).catch(function () {}); } catch (e) {} }
    function reqJson(method, url, body, ok, err) { var o = { method: method, headers: {} }; if (body) { o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(body); } fetch(url, o).then(function (r) { return r.json(); }).then(function (d) { if (d && d.ok) ok(d); else err((d && d.error) || '失败'); }).catch(function (e) { err(e.message || '网络错误'); }); }
    var DBG = location.search.indexOf('dbg=1') >= 0 || localStorage.getItem('eph-debug') === '1';
    function dbg(m) { if (!DBG) return; try { fetch('/pdf/api/epub-dbg', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ msg: '[asst] ' + m }), keepalive: true }).catch(function () {}); } catch (e) {} }
    // markdown 渲染到 el(章节链接 linkify);withMath===false(流式期间)跳过 MathJax,收尾才整段渲一次(防长答案末段二次卡顿)
    function renderMdEl(el, text, withMath) {
      try { el.innerHTML = renderMdRaw(text == null ? ' ' : text); } catch (_) { el.innerHTML = esc(text); }
      linkifyChapters(el);
      if (withMath !== false) typeset(el);
    }
    function setMd(el, text) { renderMdEl(el, text, true); }

    // ════════════════════════════════════════════════════════════════════════
    // 底座:章节定位(epub.js)—— 替代 PDF 的 pdfDoc.numPages / 页码体系。
    //   · _curIdx:当前(顶部可见)章节 spine idx,靠 rendition 'relocate' 跟踪。
    //   · COUNT:spine 章节总数。 TOC:[{label, idx}](toc href 解析成 spine idx),供上下文/章名/章节链接。
    // ════════════════════════════════════════════════════════════════════════
    var _curIdx = 0, COUNT = 0, TOC = [];
    try { if (R.location && R.location.start && typeof R.location.start.index === 'number') _curIdx = R.location.start.index; } catch (e) {}
    try { var _cl = R.currentLocation && R.currentLocation(); if (_cl && _cl.start && typeof _cl.start.index === 'number') _curIdx = _cl.start.index; } catch (e) {}
    try { R.on('relocate', function (loc) { try { if (loc && loc.start && typeof loc.start.index === 'number') _curIdx = loc.start.index; } catch (e) {} }); } catch (e) {}
    // 连续模式 relocate.start.index 给的是「第一个已渲染章」(封面 idx0 还挂 DOM 上方)→ 不准。
    // 改:rendered 时给每章 iframe doc 标真实 section idx,再从「视口顶部可见的 iframe」取当前章。
    try { R.on('rendered', function (section, view) { try { if (section && view && view.contents && view.contents.document && typeof section.index === 'number') view.contents.document.__epSecIdx = section.index; } catch (e) {} }); } catch (e) {}
    function _epIfr(doc) {
      try { if (doc.defaultView && doc.defaultView.frameElement) return doc.defaultView.frameElement; } catch (e) {}
      try { var ifrs = document.querySelectorAll('#ep-viewer iframe'); for (var j = 0; j < ifrs.length; j++) { if (ifrs[j].contentDocument === doc) return ifrs[j]; } } catch (e) {}   // 沙箱 iframe frameElement 为 null → 父文档侧 querySelector 兜底
      return null;
    }
    function _liveCurIdx() {
      try {
        var cs = R.getContents() || [], vh = window.innerHeight || 800;
        for (var i = 0; i < cs.length; i++) {
          var doc = cs[i].document; if (!doc || typeof doc.__epSecIdx !== 'number') continue;
          var ifr = _epIfr(doc); if (!ifr) continue;   // iframe 元素在父视口 rect(不是 iframe 内 body 坐标系)
          var r = ifr.getBoundingClientRect();
          if (r.bottom > 60 && r.top < vh) return doc.__epSecIdx;   // 顶部可见章
        }
      } catch (e) {}
      return _curIdx;   // 兜底
    }
    try { B.ready.then(function () { try { COUNT = (B.spine && B.spine.items && B.spine.items.length) || (B.spine && B.spine.length) || 0; } catch (e) {} }); } catch (e) {}
    function _flatToc(items, out) { (items || []).forEach(function (it) { out.push(it); if (it.subitems && it.subitems.length) _flatToc(it.subitems, out); }); return out; }
    try {
      B.loaded.navigation.then(function (nav) {
        _flatToc(nav.toc || [], []).forEach(function (it) {
          try { var href = (it.href || '').split('#')[0]; var sp = B.spine.get(href); if (sp && typeof sp.index === 'number') TOC.push({ label: (it.label || '').trim(), idx: sp.index }); } catch (e) {}
        });
        TOC.sort(function (a, b) { return a.idx - b.idx; });
      });
    } catch (e) {}
    function chapLabelOf(idx) { var lab = ''; for (var i = 0; i < TOC.length; i++) { if (TOC[i].idx <= idx) lab = TOC[i].label; else break; } return lab; }
    function sectName(idx) { return chapLabelOf(idx) || ('第 ' + ((idx | 0) + 1) + ' 节'); }

    // ── 底座:取活动选区(epub.js iframe)—— 等价 PDF 的当前选中文本 + 所在句上下文。
    //    取的就是 epub2.js 维护的 cur 那同一份原生选区(text + 所在块上下文当 selection_sentence)。
    function curSelection() {
      try {
        var cs = R.getContents() || [];
        for (var i = 0; i < cs.length; i++) {
          var win = cs[i].window, doc = cs[i].document;
          if (!win || !doc) continue;
          var s = win.getSelection();
          if (s && s.rangeCount && !s.isCollapsed) {
            var tx = (s.toString() || '').trim();
            if (!tx) continue;
            var node = s.anchorNode;
            var blk = node ? (node.nodeType === 3 ? node.parentElement : node) : null;
            blk = blk && blk.closest ? blk.closest('p,li,td,blockquote,div,section,h1,h2,h3,h4') : null;
            var sent = blk ? (blk.textContent || '').trim().slice(0, 1200) : '';
            return { sel: tx, sent: (sent && sent.replace(/\s+/g, '') !== tx.replace(/\s+/g, '')) ? sent : '' };
          }
        }
      } catch (e) {}
      return { sel: '', sent: '' };
    }

    // ════════════════════════════════════════════════════════════════════════
    // CSS:把 PDF 25-assistant 的相关样式补进 EPUB 助手 CSS 块,类名统一 ep-* 前缀(不与 PDF 的 .asst-*/.ams-* 撞)。
    //   含:tool/notice/undo/edit 卡 + 章节链接 + 「!」反馈条/弹窗 + 追问 chip + 上下文卡 + 逐字浮现 reveal(ep-mfx-*)
    //   + ⚙ 模型设置面板(ep-ams-*)。
    // ════════════════════════════════════════════════════════════════════════
    (function injectAsstCss() {
      if (document.getElementById('ep-asst-extra-css')) return;
      var s = document.createElement('style'); s.id = 'ep-asst-extra-css';
      s.textContent =
        '.ep-asst-tool{color:#7c93c4;font-size:12.5px;font-style:italic;display:inline-flex;align-items:center;gap:6px}' +
        '.ep-asst-note{align-self:center;background:#2a2410;border:1px solid #5a4a18;color:#e7d28a;font-size:12px;padding:4px 10px;border-radius:9px;max-width:96%;line-height:1.5}' +
        '.ep-asst-undo{background:#3a1d2a;border:1px solid #6b3550;color:#ffd0e0;border-radius:7px;padding:2px 8px;font-size:12px;cursor:pointer;margin-left:6px}' +
        '.ep-asst-undo:active{background:#52283a}.ep-asst-undo:disabled{opacity:.5}' +
        '.ep-asst-jump{background:#16293a;border:1px solid #2a4a63;color:#bce0ff;border-radius:7px;padding:2px 8px;font-size:12px;cursor:pointer;margin-left:6px}' +
        '.ep-asst-jump:active{background:#1d3a52}' +
        '.ep-edit-card{background:#11233a;border:1px solid #2a4a63}' +
        '.ep-edit-h{font-size:13px;color:#cfe6ff;margin-bottom:6px}' +
        '.ep-edit-row{display:flex;gap:6px;flex-wrap:wrap}' +
        '.ep-edit-undo{background:#26344f;border:1px solid #3a5273;color:#dbe7ff;border-radius:8px;padding:3px 12px;font-size:12.5px;cursor:pointer}' +
        '.ep-edit-undo:active{background:#2f4061}.ep-edit-undo:disabled{opacity:.55}' +
        // 写操作「撤销/重做」持久卡(系统自动生成,随对话持久化):标题 + [查看详情][↩撤销][↪重做] + 详情折叠区
        '.ep-act-card{background:#0f1f17;border:1px solid #2a5a3e}' +
        '.ep-act-card.ep-act-undone{background:#231b22;border-color:#5a3550}' +
        '.ep-act-h{font-size:13px;color:#bfead0;margin-bottom:6px;display:flex;align-items:center;gap:6px}' +
        '.ep-act-card.ep-act-undone .ep-act-h{color:#e7c6dd;text-decoration:line-through;opacity:.85}' +
        '.ep-act-row{display:flex;gap:6px;flex-wrap:wrap}' +
        '.ep-act-btn{border-radius:8px;padding:3px 12px;font-size:12.5px;cursor:pointer;font-family:inherit}' +
        '.ep-act-detail-btn{background:#13233f;border:1px solid #2a3a63;color:#bcd0ff}.ep-act-detail-btn:active{background:#1d3358}' +
        '.ep-act-undo{background:#26344f;border:1px solid #3a5273;color:#dbe7ff}.ep-act-undo:active{background:#2f4061}' +
        '.ep-act-redo{background:#1d3a2a;border:1px solid #2f6347;color:#bfead0}.ep-act-redo:active{background:#244a35}' +
        '.ep-act-btn:disabled{opacity:.42;cursor:default}' +
        '.ep-act-detail-box{margin-top:7px;white-space:pre-wrap;word-break:break-word;max-height:240px;overflow:auto;background:#0a1020;border:1px solid #233156;border-radius:8px;padding:7px 9px;font-size:11.5px;color:#bcd0ee;line-height:1.55;-webkit-overflow-scrolling:touch}' +
        // 逐条高亮列表(auto_highlight 标完 / find_highlights 列出)= PDF .asst-hl-* 的 EPUB 版
        '.ep-hl-pick-h{font-size:12.5px;color:#cfe6ff;opacity:.9;margin-bottom:5px}' +
        '.ep-hl-row{display:flex;align-items:center;gap:7px;padding:4px 0;border-top:1px solid #1d2742}' +
        '.ep-hl-row:first-of-type{border-top:none}' +
        '.ep-hl-sw{flex:none;width:13px;height:13px;border-radius:3px;border:1px solid rgba(255,255,255,.3)}' +
        '.ep-hl-tx{flex:1 1 auto;min-width:0;font-size:12px;color:#dbe7ff;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
        '.ep-hl-del{flex:none;background:#3a1d2a;border:1px solid #6b3550;color:#ffd0e0;border-radius:7px;padding:2px 8px;font-size:12px;cursor:pointer}' +
        '.ep-hl-del:active{background:#52283a}.ep-hl-del:disabled{opacity:.5}' +
        '.ep-chaplink{color:#7dd3fc;cursor:pointer;text-decoration:underline dotted;text-underline-offset:2px}' +
        '.ep-chaplink:active{opacity:.7}' +
        // 追问 chip(照搬 .asst-followups / .asst-fu)
        '.ep-followups{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}' +
        '.ep-fu{background:#13233f;border:1px solid #2a3a63;color:#bcd0ff;border-radius:13px;padding:5px 11px;font-size:13px;cursor:pointer;text-align:left;font-family:inherit;line-height:1.4;-webkit-tap-highlight-color:transparent}' +
        '.ep-fu:active{background:#1d3358}' +
        // 上下文卡(选中 + 章节;公式走 MathJax)— 照搬 .asst-ctx-card / .actx-sel
        '.ep-ctx-card{margin-top:7px;display:flex;flex-direction:column;gap:5px}' +
        '.ep-ctx-row{font-size:12px;color:#dbe7ff;background:rgba(255,255,255,.10);border-left:2px solid rgba(255,255,255,.45);border-radius:4px;padding:3px 8px;line-height:1.4;word-break:break-word}' +
        '.ep-ctx-row.clk{cursor:pointer}.ep-ctx-row.clk:active{background:rgba(255,255,255,.2)}' +
        '.ep-ctx-row.fml{text-align:center;white-space:normal;overflow-x:auto;color:#eaf2ff}' +
        // 「!」反馈条 + 弹窗(照搬 .asst-fb-* / .afp-*)
        '.ep-fb-bar{position:relative;margin-top:7px;display:flex;justify-content:flex-end;align-items:center}' +
        '.ep-fb-tok{margin-right:auto;font-size:11px;color:#6f7fa3;background:#121a2e;border:1px solid #233156;border-radius:8px;padding:1px 7px}' +
        '.ep-fb-btn{width:22px;height:22px;line-height:20px;text-align:center;border-radius:50%;border:1px solid #2a3a63;background:#0e1525;color:#7c93c4;font-size:13px;font-weight:700;cursor:pointer;padding:0;-webkit-tap-highlight-color:transparent}' +
        '.ep-fb-btn:active{background:#1a2540}' +
        '.ep-fb-pop{position:absolute;right:0;bottom:28px;z-index:20;width:320px;max-width:88vw;background:#0d1426;border:1px solid #2a3a63;border-radius:11px;padding:9px;box-shadow:0 8px 22px rgba(0,0,0,.5);display:flex;flex-direction:column;gap:5px}' +
        '.ep-afp-l-btn{cursor:pointer;text-decoration:underline dotted;text-underline-offset:2px;-webkit-tap-highlight-color:transparent}' +
        '.ep-afp-l-btn:active{opacity:.7}' +
        '.ep-afp-detail{white-space:pre-wrap;word-break:break-word;max-height:260px;overflow:auto;background:#0a1020;border:1px solid #233156;border-radius:8px;padding:8px 10px;margin:2px 0 4px;font-size:11.5px;color:#bcd0ee;line-height:1.55;-webkit-overflow-scrolling:touch}' +
        '.ep-afp-h{font-size:11px;color:#7c93c4;margin-bottom:2px}' +
        '.ep-afp-step{display:flex;align-items:center;gap:7px;font-size:12px;line-height:1.5}' +
        '.ep-afp-l{color:#cdd9f2;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1 1 auto;min-width:0}' +
        '.ep-afp-m{color:#7c93c4;flex:none;font-variant-numeric:tabular-nums}' +
        '.ep-afp-gear-btn{flex:none;background:none;border:none;color:#6b7da0;font-size:13px;cursor:pointer;padding:0 1px;-webkit-tap-highlight-color:transparent}' +
        '.ep-afp-gear-btn:active{color:#bcd0ff}' +
        '.ep-afp-foot{font-size:11px;color:#6b7da0;margin-top:5px;text-align:right;font-variant-numeric:tabular-nums}' +
        '.ep-afp-acts{display:flex;flex-direction:column;gap:5px;margin-top:4px;border-top:1px solid #1d2742;padding-top:7px}' +
        '.ep-afp-act{text-align:left;border:1px solid #2a3a63;border-radius:8px;padding:6px 9px;font-size:12px;cursor:pointer;color:#dbe7ff}' +
        '.ep-afp-q{background:#16293a;border-color:#2a4a63}.ep-afp-q:active{background:#1d3a52}' +
        // 逐字浮现 reveal(照搬 mfx.css 的 stream-fx,ep- 前缀 + 作用域到 .ep-msg.a)
        '.ep-mfx-typing{display:inline-flex;align-items:center;gap:5px;padding:2px;vertical-align:middle}' +
        '.ep-mfx-typing i{width:7px;height:7px;border-radius:50%;background:#60a5fa;display:block;opacity:.4}' +
        '.ep-mfx-caret{display:inline-block;width:2px;height:1.05em;margin-left:1px;vertical-align:-0.18em;border-radius:1px;background:linear-gradient(#60a5fa,#7dd3fc);box-shadow:0 0 6px rgba(96,165,250,.7)}' +
        '.ep-msg.a.ep-mfx-streaming{box-shadow:0 0 0 1px rgba(96,165,250,.28),0 0 16px rgba(96,165,250,.14)}' +
        '.ep-mfx-w{opacity:1}' +
        '.ep-mfx-after{opacity:0;transform:translateY(6px)}' +
        '.ep-mfx-after.on{opacity:1;transform:none;transition:opacity .4s ease,transform .4s cubic-bezier(.22,1,.36,1)}' +
        '@media (prefers-reduced-motion:no-preference){' +
          '@keyframes epMfxBounce{0%,80%,100%{transform:translateY(0);opacity:.35}40%{transform:translateY(-5px);opacity:1}}' +
          '.ep-mfx-typing i{animation:epMfxBounce 1.2s ease-in-out infinite}' +
          '.ep-mfx-typing i:nth-child(2){animation-delay:.16s}.ep-mfx-typing i:nth-child(3){animation-delay:.32s}' +
          '@keyframes epMfxBlink{0%,45%{opacity:1}55%,100%{opacity:0}}' +
          '.ep-mfx-caret{animation:epMfxBlink 1s steps(1) infinite}' +
          '@keyframes epMfxChar{from{opacity:0;filter:blur(4px)}to{opacity:1;filter:none}}' +
          '.ep-msg.a.ep-mfx-streaming .ep-mfx-w{opacity:0}' +
          '.ep-msg.a.ep-mfx-streaming .ep-mfx-w.ep-mfx-shown{opacity:1}' +
          '.ep-msg.a.ep-mfx-streaming .ep-mfx-w.ep-mfx-reveal{animation:epMfxChar .34s ease both}' +
        '}' +
        // ⚙ 模型设置面板(照搬 .ams-*,ep-ams- 前缀)
        '.ep-ams-mask{position:fixed;inset:0;z-index:300;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;padding:16px}' +
        '.ep-ams-box{background:#0d1426;border:1px solid #2a3a63;border-radius:14px;max-width:440px;width:100%;max-height:86vh;overflow-y:auto;padding:14px 14px 16px;box-shadow:0 12px 40px rgba(0,0,0,.6)}' +
        '.ep-ams-h{font-size:15px;color:#dbe7ff;font-weight:600;display:flex;align-items:center;justify-content:space-between;margin-bottom:3px}' +
        '.ep-ams-x{background:none;border:none;color:#7c93c4;font-size:20px;cursor:pointer;padding:0 4px;line-height:1}' +
        '.ep-ams-sub{font-size:11px;color:#6b7da0;margin-bottom:10px;line-height:1.5}' +
        '.ep-ams-task{background:#0a1322;border:1px solid #243152;border-radius:10px;padding:10px;margin-bottom:9px}' +
        '.ep-ams-tname{font-size:13px;color:#cdd9f2;font-weight:600;margin-bottom:2px}' +
        '.ep-ams-tdef{font-size:11px;color:#6b7da0;margin-bottom:7px}' +
        '.ep-ams-row{display:flex;gap:6px;flex-wrap:wrap;align-items:center}' +
        '.ep-ams-sel{background:#0d1426;border:1px solid #2a3a63;color:#dbe7ff;border-radius:7px;padding:5px 6px;font-size:12px;flex:1 1 28%;min-width:0}' +
        '.ep-ams-sel:disabled{opacity:.45}' +
        '.ep-ams-rst{background:#1a2233;border:1px solid #2a3a63;color:#9fb4e0;border-radius:7px;padding:5px 9px;font-size:12px;cursor:pointer;flex:none}' +
        '.ep-ams-rst:active{background:#222d44}' +
        '.ep-ams-cur{font-size:11px;color:#7c93c4;margin-top:6px}' +
        '.ep-ams-note{font-size:11px;color:#bfae72;background:#221d10;border:1px solid #463a18;border-radius:7px;padding:6px 9px;margin-top:4px;line-height:1.5}';
      document.head.appendChild(s);
    })();

    // ── [[FOLLOWUP]] 剥离 + 追问 chip + 上下文卡 ── 逐字照搬 PDF 25-assistant.js(/rc-assistant.js),自包含
    function splitFollowups(text) {
      var fu = [];
      var push = function (body) { String(body || '').split(/[|\n]+/).forEach(function (q) { q = q.trim().replace(/^[\-·•\d\.\s]+/, ''); if (q) fu.push(q); }); };
      var clean = String(text == null ? '' : text).replace(/\[\[FOLLOWUP\]\]([\s\S]*?)\[\[\/FOLLOWUP\]\]/g, function (m, body) { push(body); return ''; });
      var open = clean.indexOf('[[FOLLOWUP]]');   // 模型常漏结束标记 → 未闭合:从 [[FOLLOWUP]] 到结尾都当追问
      if (open >= 0) { push(clean.slice(open + 12).replace(/\[\[\/?FOLLOWUP\]\]/g, '')); clean = clean.slice(0, open); }
      return { text: clean.trim(), followups: fu.slice(0, 4) };
    }
    function renderFollowups(afterEl, followups, onPick) {
      if (!afterEl || !followups || !followups.length) return;
      var box = document.createElement('div'); box.className = 'ep-followups';
      followups.forEach(function (q) {
        var b = document.createElement('button'); b.className = 'ep-fu';
        b.textContent = q;   // 纯文本占位;下面让 MathJax 渲 chip 里的 $..$(点击仍发原始 q)
        b.addEventListener('click', function () { try { if (typeof onPick === 'function') onPick(q); } catch (e) {} });
        box.appendChild(b);
      });
      afterEl.appendChild(box);
      typeset(box); aiBody.scrollTop = aiBody.scrollHeight;
    }
    // contextCard(items):items=[{text, formula?, onClick?}]。公式行(formula+$..$)走 MathJax,不显示裸 LaTeX。
    function contextCard(items) {
      if (!items || !items.length) return null;
      var rows = items.filter(function (it) { return it && (it.text != null) && String(it.text).trim() !== ''; });
      if (!rows.length) return null;
      var card = document.createElement('div'); card.className = 'ep-ctx-card';
      rows.forEach(function (it) {
        var row = document.createElement('div'); row.className = 'ep-ctx-row';
        var t = String(it.text);
        if (it.formula && /\$\$?[\s\S]+\$\$?/.test(t)) {   // 公式选区:走 MathJax
          row.classList.add('fml');
          var raw = t.replace(/^[^$]*\$\$?/, '').replace(/\$\$?[^$]*$/, '');
          var block = /\$\$/.test(t) || /\\begin\{|\\\\/.test(raw);
          row.textContent = block ? ('\\[' + raw + '\\]') : ('\\(' + raw + '\\)');
          typeset(row);
        } else {
          row.textContent = (t.length > 96 ? (t.slice(0, 64) + '…' + t.slice(-24)) : t);
        }
        if (it.title) row.title = it.title;
        if (typeof it.onClick === 'function') { row.classList.add('clk'); row.addEventListener('click', function () { try { it.onClick(); } catch (e) {} }); }
        card.appendChild(row);
      });
      return card;
    }
    // 问题是否指向「当前章/节内容」(有此指代才给「正在看」上下文行;纯概念问题不给卡)── 照搬 PDF _pageRefersToPage 同款正则
    function _pageRefersToPage(msg) {
      var m = msg || '';
      return /这一?页|本页|此页|当前页|这段|这里|这张?图|这幅图|如[下图]图?|上面这?|这个公式|这道?题|本章|这一?章|这一?节|本节|页面|图里|图中|这部分/.test(m)
          || /\bthis (page|figure|fig|section|paragraph|chapter|image|diagram|part)\b|\bhere\b/i.test(m);
    }

    // ════════════════════════════════════════════════════════════════════════
    // 工具循环回调:后端 actions 事件 → 调 window 上的动作函数。底座映射(全部映射到 epub.js):
    //   {fn:"jumpTo",args:[idx]}       → R.display(idx)
    //   {fn:"epubHighlight",args:[{section,texts,color}]} → CFI 注解高亮 + epubHl.persist 持久化 + _epAssistEdit 撤销卡
    //   {fn:"openBookAt",args:[fileRel,page]} → 跨书跳转
    // ════════════════════════════════════════════════════════════════════════
    function runActions(actions) {
      if (!actions || !actions.length) return;
      actions.forEach(function (a) { try { if (a && a.fn && typeof window[a.fn] === 'function') window[a.fn].apply(null, a.args || []); } catch (e) {} });
    }
    // ── 回到第X章返回条(照搬 PDF 05-nav.js jumpWithBack/pageGoBack;底座:页号→CFI)。CSS + #page-back-bar 在 epub_reader.html。──
    window.__epBack = null;
    function _epCurCfi() { try { var l = R.currentLocation && R.currentLocation(); return (l && l.start && l.start.cfi) || ''; } catch (e) { return ''; } }
    function _showPageBackBar(label) { var bar = document.getElementById('page-back-bar'); if (!bar) return; bar.textContent = '↩ 回到' + (label || '原处'); bar.style.display = 'block'; }
    function _hidePageBackBar() { var bar = document.getElementById('page-back-bar'); if (bar) bar.style.display = 'none'; }
    function _epRecordBack() {   // 第一次跳:记最早来处的 cfi + 章名(多次跳转仍回最早那处,对照 PDF __pageBackAnchor)
      if (window.__epBack == null) { var c = _epCurCfi(); if (c) window.__epBack = { cfi: c, label: sectName(_liveCurIdx()) }; }
    }
    function _epAfterJump() { if (window.__epBack && window.__epBack.cfi) _showPageBackBar(window.__epBack.label); else _hidePageBackBar(); }
    window.pageGoBack = function () {   // #page-back-bar 内联 onclick 调;回到记下的 cfi
      var b = window.__epBack; window.__epBack = null; _hidePageBackBar();
      if (b && b.cfi) { try { R.display(b.cfi); } catch (e) {} }
    };
    window.jumpTo = function (idx) {
      try {
        var i = parseInt(idx, 10); if (isNaN(i)) return;
        if (i === _liveCurIdx()) { R.display(i); return; }   // 跳到当前章:不弹返回条
        _epRecordBack(); R.display(i); _epAfterJump();
      } catch (e) {}
    };
    // 译页工具:后端助手「整页翻译」client_action {fn:'ptrans'} → 点顶栏「译页」按钮(照搬 PDF togglePageTranslate)
    window.ptrans = function () { try { var b = document.getElementById('ep-pagetr'); if (b) b.click(); } catch (e) {} };
    window.openBookAt = function (fileRel, page) {
      try {
        if (!fileRel) return;
        var url = /\.epub$/i.test(fileRel)
          ? ('/pdf/epub/view?file=' + encodeURIComponent(fileRel))
          : ('/pdf/view?file=' + encodeURIComponent(fileRel) + (page ? '&page=' + page : ''));
        location.href = url;
      } catch (e) {}
    };

    // ── epub.js 高亮(CFI 注解):在已渲染章的 iframe 里按文本定位出 Range → cfiFromRange → annotations.highlight(cfi)。 ──
    function _rangeFromText(doc, query) {
      query = String(query || ''); if (!query) return null;
      var root = doc.body || doc;
      var walker = doc.createTreeWalker(root, NodeFilter.SHOW_TEXT, null), n;
      var nodes = [], raw = '';
      while ((n = walker.nextNode())) { nodes.push({ node: n, start: raw.length, len: n.nodeValue.length }); raw += n.nodeValue; }
      if (!raw) return null;
      var i = raw.indexOf(query), qlen = query.length;
      if (i < 0) {   // 空白归一化容错(提取文本 vs DOM 原始空白常不同)
        var q = query.replace(/\s+/g, ' ').trim(); if (!q) return null;
        var comp = '', map = [], prev = false;
        for (var k = 0; k < raw.length; k++) { var c = raw[k]; if (/\s/.test(c)) { if (prev) continue; comp += ' '; map.push(k); prev = true; } else { comp += c; map.push(k); prev = false; } }
        var j = comp.indexOf(q); if (j < 0) return null;
        i = map[j]; var lastM = j + q.length - 1; var endRaw = lastM < map.length ? map[lastM] + 1 : raw.length; qlen = endRaw - i;
      }
      var startG = i, endG = i + qlen;
      function locate(g) { for (var x = 0; x < nodes.length; x++) { var nd = nodes[x]; if (g >= nd.start && g <= nd.start + nd.len) return { node: nd.node, offset: g - nd.start }; } var last = nodes[nodes.length - 1]; return { node: last.node, offset: last.len }; }
      var a = locate(startG), b = locate(endG);
      try { var rng = doc.createRange(); rng.setStart(a.node, a.offset); rng.setEnd(b.node, b.offset); return rng; } catch (e) { return null; }
    }
    // POST 进 sidecar(带 anchor{section} → 供 find_highlights 反查章号)并**捕获服务端 id**(逐条删要用)。
    //   不 renderHl(助手已用 R.annotations 在内存画过,避免与 epubHl 重影);刷新后由 loadHls 从 sidecar 渲染。
    function _persistHLCapture(cfi, text, color, section) {
      return fetch('/pdf/api/epub-highlights', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file: FREL, cfi: cfi, anchor: { section: section }, text: text || '', color: color || '#ffd54a' })
      }).then(function (r) { return r.json(); }).then(function (d) { return (d && d.ok) ? d.id : null; }).catch(function () { return null; });
    }
    function _findContentForSection(section) {
      try {
        var cs = R.getContents() || [];
        for (var i = 0; i < cs.length; i++) { var d = cs[i].document; if (d && d.__epSecIdx === section) return cs[i]; }
      } catch (e) {}
      return null;
    }
    // 确保某 section 已渲染(没渲染就 R.display 再轮询等它出现)→ resolve(content|null)。**串行**调用,避免多 section 抢 R.display。
    function _ensureSection(section) {
      return new Promise(function (resolve) {
        var c = _findContentForSection(section);
        if (c) { resolve(c); return; }
        try { var p = R.display(section); if (p && p.then) p.then(function () {}, function () {}); } catch (e) {}
        var tries = 0;
        (function poll() {
          var cc = _findContentForSection(section);
          if (cc) { resolve(cc); return; }
          if (tries++ > 25) { resolve(_findContentForSection(section)); return; }   // ~4s 兜底(给 null 则下面回退扫全部已渲染章)
          setTimeout(poll, 160);
        })();
      });
    }
    // 单句:优先在目标 section 的 doc 定位,找不到再扫其它已渲染章(老行为兜底);命中→画注解+POST 捕 id。
    function _markOneText(t, targetContent, section, color, created, posts) {
      var cs = R.getContents() || [], order = [];
      if (targetContent) order.push(targetContent);
      for (var i = 0; i < cs.length; i++) if (cs[i] !== targetContent) order.push(cs[i]);
      for (var j = 0; j < order.length; j++) {
        var content = order[j], doc = content && content.document; if (!doc) continue;
        var rng = _rangeFromText(doc, t); if (!rng) continue;
        var cfi = ''; try { cfi = content.cfiFromRange(rng); } catch (e) {}
        if (!cfi) continue;
        var sec = (typeof doc.__epSecIdx === 'number') ? doc.__epSecIdx : section;   // 实际命中章 → 跳转才准
        try { R.annotations.highlight(cfi, {}, null, 'ep-asst-hl', { fill: color, 'fill-opacity': '0.40' }); } catch (e) { dbg('annot err: ' + (e && e.message)); }
        posts.push(_persistHLCapture(cfi, t, color, sec).then(function (id) { created.push({ id: id, section: sec, cfi: cfi, text: t, color: color }); }));
        return true;
      }
      return false;
    }
    function _markTextsIn(targetContent, section, texts, color) {
      var created = [], posts = [];
      texts.forEach(function (t) { _markOneText(t, targetContent, section, color, created, posts); });
      return Promise.all(posts).then(function () { return created; });
    }
    // 统一入口:多 section(整章 auto_highlight,arg.sections=[{section,texts}],picker:true)走**串行**逐章定位画高亮、
    //   完成后渲「逐条跳转/删除」列表(showHlPicker);单 section(手动 epub_highlight,{section,texts})仍走合并撤销卡。
    window.epubHighlight = function (arg) {
      try {
        if (!arg) return;
        var color = arg.color || localStorage.getItem('eph-hl-color') || '#ffd54a';
        var groups;
        if (Array.isArray(arg.sections)) {
          groups = arg.sections.map(function (g) {
            var ts = (g.texts || []).map(function (t) { return String(t || '').trim(); }).filter(Boolean);
            var s = parseInt(g.section, 10); if (isNaN(s)) s = _liveCurIdx();
            return { section: s, texts: ts };
          }).filter(function (g) { return g.texts.length; });
        } else {
          var ts0 = (arg.texts || (arg.text ? [arg.text] : [])).map(function (t) { return String(t || '').trim(); }).filter(Boolean);
          if (!ts0.length) return;
          var s0 = parseInt(arg.section, 10); if (isNaN(s0)) s0 = _liveCurIdx();
          groups = [{ section: s0, texts: ts0 }];
        }
        if (!groups.length) return;
        var wantPicker = !!arg.picker, firstSection = groups[0].section, allCreated = [];
        (function runGroup(i) {
          if (i >= groups.length) {
            try { if (groups.length > 1) R.display(firstSection); } catch (e) {}   // 收尾回到整章起点
            if (wantPicker) { try { window.showHlPicker({ items: allCreated }); } catch (e) {} }   // 逐条跳转/删除(per-item)
            // 系统自动:整批高亮的「查看详情 / 撤销 / 重做」持久卡(单/多 section 都给;持久化进对话)
            try { _epHlAction(allCreated, color, wantPicker); } catch (e) { dbg('hl action err:' + (e && e.message)); }
            return;
          }
          var g = groups[i];
          _ensureSection(g.section).then(function (content) {
            _markTextsIn(content, g.section, g.texts, color).then(function (created) {
              allCreated = allCreated.concat(created); runGroup(i + 1);
            });
          });
        })(0);
      } catch (e) { dbg('epubHighlight err:' + (e && e.message)); }
    };
    // 逐条渲染一组高亮(色块 + 原文 + ↗跳转 + 🗑删除)= PDF window._showHlPicker 的 EPUB 版。
    //   auto_highlight 标完(前端拿到 id 后调)/ find_highlights(后端 client_action {fn:'showHlPicker'})都用它。
    window.showHlPicker = function (d) {
      try {
        var items = ((d && d.items) || []).filter(function (it) { return it && it.id; });   // 无 id(定位/POST 失败)删不了 → 不渲
        var card = document.createElement('div'); card.className = 'ep-msg a';
        if (!items.length) {
          card.innerHTML = '<span class="ep-asst-tool">没有可操作的高亮</span>';
          aiBody.appendChild(card); aiBody.scrollTop = aiBody.scrollHeight; return;
        }
        var h = document.createElement('div'); h.className = 'ep-hl-pick-h';
        h.textContent = '共 ' + items.length + ' 处高亮 —— 点「跳转」去看,点「删除」移除:';
        card.appendChild(h);
        items.forEach(function (it) {
          var row = document.createElement('div'); row.className = 'ep-hl-row';
          var sw = document.createElement('span'); sw.className = 'ep-hl-sw'; sw.style.background = it.color || '#ffd54a';
          var tx = document.createElement('span'); tx.className = 'ep-hl-tx';
          tx.textContent = ((it.section != null) ? (sectName(it.section) + ' · ') : '') + (it.text || '(无文字)');
          tx.title = it.text || '';
          var jb = document.createElement('button'); jb.className = 'ep-asst-jump';
          if (it.section != null) jb.dataset.idx = it.section;
          if (it.cfi) jb.dataset.cfi = it.cfi;
          jb.textContent = '↗ 跳转';
          var del = document.createElement('button'); del.className = 'ep-hl-del';
          del.dataset.id = it.id; if (it.cfi) del.dataset.cfi = it.cfi; del.textContent = '🗑 删除';
          row.appendChild(sw); row.appendChild(tx); row.appendChild(jb); row.appendChild(del);
          card.appendChild(row);
        });
        aiBody.appendChild(card); aiBody.scrollTop = aiBody.scrollHeight;
      } catch (e) { dbg('showHlPicker err:' + (e && e.message)); }
    };

    // ── (第N章)→ 可点链接(N = 工具返回的 section idx,0 基)── 照搬 PDF 的 _linkifyPages,换成章节
    function linkifyChapters(el) {
      try {
        var re = /第?\s*(\d{1,4})\s*[章節节]/g;   // 照搬 PDF _linkifyPages 宽松匹配(第可省、无需括号),范围由下面 cn<0||cn>=COUNT 兜底
        var nodes = [], w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null), nd;
        while ((nd = w.nextNode())) {
          if (nd.nodeValue && /\d\s*[章節节]/.test(nd.nodeValue) && nd.parentNode &&
              !(nd.parentNode.closest && nd.parentNode.closest('a,button,.ep-chaplink,code,pre'))) nodes.push(nd);
        }
        nodes.forEach(function (node) {
          var t = node.nodeValue, frag = document.createDocumentFragment(), last = 0, m; re.lastIndex = 0;
          while ((m = re.exec(t))) {
            var cn = parseInt(m[1], 10);
            if (isNaN(cn) || cn < 0 || cn >= COUNT) continue;
            if (m.index > last) frag.appendChild(document.createTextNode(t.slice(last, m.index)));
            var a = document.createElement('span'); a.className = 'ep-chaplink'; a.textContent = m[0]; a.dataset.idx = cn;
            frag.appendChild(a); last = m.index + m[0].length;
          }
          if (last) { if (last < t.length) frag.appendChild(document.createTextNode(t.slice(last))); node.parentNode.replaceChild(frag, node); }
        });
      } catch (e) {}
    }

    // ════════ 感叹号「!」反馈弹窗 —— 照搬 PDF 的 _attachFeedback + _buildFbPop(ep- 前缀;⚙ 调 openModelSettings)════════
    var _fbOpenPop = null;
    function _fbClosePop() { if (_fbOpenPop) { try { _fbOpenPop.remove(); } catch (_) {} _fbOpenPop = null; } }
    document.addEventListener('click', function (e) {
      if (_fbOpenPop && e.target && e.target.closest && !e.target.closest('.ep-fb-bar')) _fbClosePop();
    });
    function _epFmtTime(t) {
      var d = new Date(t), n = new Date();
      var hm = String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
      return (d.toDateString() === n.toDateString() ? '今天 ' : ((d.getMonth() + 1) + '/' + d.getDate() + ' ')) + hm;
    }
    function _buildFbPop(question, trace, close, ts) {
      var pop = document.createElement('div'); pop.className = 'ep-fb-pop';
      var h = document.createElement('div'); h.className = 'ep-afp-h';
      h.textContent = (trace && trace.length) ? '这条回答经过的 AI 调用' : '对这条回答不满意?';
      pop.appendChild(h);
      var _tot = 0;
      var steps = (trace && trace.length) ? trace : [{ label: '回答', model: '', action: 'orchestrator' }];
      steps.forEach(function (st) {
        var row = document.createElement('div'); row.className = 'ep-afp-step';
        var l = document.createElement('span'); l.className = 'ep-afp-l'; l.textContent = st.label || '步骤';
        if (st.detail) {   // 这步有完整内容 → 步骤名变可点按钮,点开/收起显示该步的完整 AI 产出
          l.classList.add('ep-afp-l-btn'); l.title = '点开看这一步的完整内容';
          l.addEventListener('click', function (e) {
            e.stopPropagation();
            var ex = row.nextSibling;
            if (ex && ex.classList && ex.classList.contains('ep-afp-detail')) { ex.remove(); return; }
            var dt = document.createElement('div'); dt.className = 'ep-afp-detail'; dt.textContent = st.detail;
            row.parentNode.insertBefore(dt, row.nextSibling);
          });
        }
        var m = document.createElement('span'); m.className = 'ep-afp-m';
        if (typeof st.sec === 'number') _tot += st.sec;
        var mt = st.model || '';
        m.textContent = mt + (typeof st.sec === 'number' ? (mt ? ' · ' : '') + st.sec + 's' : '');
        if (st.tier === 'free' || st.tier === 'paid') {   // Gemini 实际服务这条用了哪档 → 标「免费/付费」
          var tg = document.createElement('span'); tg.textContent = st.tier === 'paid' ? '付费' : '免费';
          tg.style.cssText = 'margin-left:6px;padding:0 6px;border-radius:6px;font-size:11px;vertical-align:middle;'
            + (st.tier === 'paid' ? 'background:#5a3a1a;color:#ffcf8f;' : 'background:#1f4a2e;color:#8fe3a8;');
          m.appendChild(tg);
        }
        row.appendChild(l); row.appendChild(m);
        if (st.action) {   // 这步会调模型 → ⚙ 直接设它的预设
          var g = document.createElement('button'); g.className = 'ep-afp-gear-btn'; g.textContent = '⚙'; g.title = '设这个动作的模型/深度';
          g.addEventListener('click', function (e) { e.stopPropagation(); _fbClosePop(); try { openModelSettings(st.action); } catch (_) {} });
          row.appendChild(g);
        }
        pop.appendChild(row);
      });
      if (ts || _tot) {   // 页脚:完成时刻 + 总耗时
        var ft = document.createElement('div'); ft.className = 'ep-afp-foot';
        var bits = [];
        if (ts) { try { bits.push('🕐 ' + _epFmtTime(ts * 1000)); } catch (_) {} }
        if (_tot) bits.push('共 ' + (Math.round(_tot * 10) / 10) + 's');
        if (bits.length) { ft.textContent = bits.join(' · '); pop.appendChild(ft); }
      }
      var acts = document.createElement('div'); acts.className = 'ep-afp-acts';
      var bSet = document.createElement('button'); bSet.className = 'ep-afp-act ep-afp-q';
      bSet.textContent = '⚙ 模型设置';
      bSet.addEventListener('click', function () { close(); try { openModelSettings(); } catch (_) {} });
      acts.appendChild(bSet); pop.appendChild(acts);
      return pop;
    }
    function attachTrace(am, trace, ts) {
      if (!am) return;
      try { var old = am.querySelector('.ep-fb-bar'); if (old) old.remove(); } catch (_) {}   // 重渲时防重复挂
      var bar = document.createElement('div'); bar.className = 'ep-fb-bar';
      var _tok = trace && trace[0] && trace[0].tok;   // 本轮累计 token
      if (_tok) {
        var tk = document.createElement('span'); tk.className = 'ep-fb-tok';
        tk.textContent = (_tok >= 1000 ? (_tok / 1000).toFixed(1).replace(/\.0$/, '') + 'k' : _tok) + ' tok';
        tk.title = '这条回答累计消耗 token：' + _tok;
        bar.appendChild(tk);
      }
      var btn = document.createElement('button'); btn.className = 'ep-fb-btn'; btn.textContent = '!'; btn.title = '对这条回答不满意?';
      btn.addEventListener('click', function (e) {
        e.stopPropagation();
        if (_fbOpenPop && _fbOpenPop._owner === btn) { _fbClosePop(); return; }
        _fbClosePop();
        var pop = _buildFbPop(null, trace, _fbClosePop, ts); pop._owner = btn;
        bar.appendChild(pop); _fbOpenPop = pop;
      });
      bar.appendChild(btn); am.appendChild(bar);
    }

    // ════════ ⚙ AI 模型设置面板(按功能配 后端/型号/深度)── 逐字照搬 PDF,命中同一组服务端端点 ════════
    //   /api/assistant/action-prefs(读)+ /api/assistant/action-pref(写),按 uid+action。PDF/EPUB 共用一份预设。
    function _setActionPref(action, backend, variant, depth, okMsg) {
      return fetch('/api/assistant/action-pref', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: action, backend: backend || '', variant: variant || '', depth: depth || '' }) })
        .then(function (r) { return r.json(); })
        .then(function (d) { if (d && d.ok) toast(okMsg || '已设置'); return d; })
        .catch(function () {});
    }
    var _DEPTH_LABEL = { auto: '自动(按问题)', low: 'low(快)', medium: 'medium', high: 'high(深)', xhigh: 'xhigh', max: 'max(最强)', none: '不思考', think: '思考' };
    var _BACKEND_LABEL = { claude: 'Claude', gemini: 'Gemini' };
    function _msMkSel(opts, val, labels, disabledSet) {
      var s = document.createElement('select'); s.className = 'ep-ams-sel';
      (opts || []).forEach(function (o) {
        var op = document.createElement('option'); op.value = o;
        op.textContent = (labels && labels[o]) || o;
        if (disabledSet && disabledSet.indexOf(o) >= 0) op.disabled = true;
        if (o === val) op.selected = true;
        s.appendChild(op);
      });
      return s;
    }
    function _buildMsTask(action, info, cat, names, locked) {
      var def = info.def, cur = info.pref || def;
      var card = document.createElement('div'); card.className = 'ep-ams-task';
      var nm = document.createElement('div'); nm.className = 'ep-ams-tname'; nm.textContent = names[action] || action;
      var df = document.createElement('div'); df.className = 'ep-ams-tdef';
      df.textContent = '默认:' + (_BACKEND_LABEL[def.backend] || def.backend) + ' · ' + (cat.variant_short[def.variant] || def.variant) + ' · ' + (_DEPTH_LABEL[def.depth] || def.depth);
      var row = document.createElement('div'); row.className = 'ep-ams-row';
      var gstat = cat.gemini_status || {};
      function _fmtRetry(s) { if (!s) return ''; if (s < 90) return s + '秒'; if (s < 5400) return Math.round(s / 60) + '分'; return Math.round(s / 3600) + '小时'; }
      var varLabels = {}; (cat.variants.gemini || []).forEach(function (v) {
        var st = gstat[v], tag = ' · 免费';
        if (st && st.free === false) { tag = ' · 付费(' + (st.reason || '免费不可用') + (st.retry ? ',还需' + _fmtRetry(st.retry) : '') + ')'; }
        varLabels[v] = (cat.variant_short[v] || v) + tag;
      });
      var lockB = locked[action] || [];
      var selB = _msMkSel(cat.backends, cur.backend, _BACKEND_LABEL, lockB);
      var selV = _msMkSel(cat.variants[cur.backend] || [], cur.variant, varLabels);
      var selD = _msMkSel(cat.depths[cur.backend] || [], cur.depth, _DEPTH_LABEL);
      function save() {
        _setActionPref(action, selB.value, selV.value, selD.value,
          '「' + (names[action] || action) + '」已设为 ' + (cat.variant_short[selV.value] || selV.value) + '·' + (_DEPTH_LABEL[selD.value] || selD.value));
      }
      function rebindVD(backend, keepVal, vv, dv) {
        var nv = _msMkSel(cat.variants[backend] || [], keepVal ? vv : (cat.variants[backend] || [])[0], varLabels);
        var nd = _msMkSel(cat.depths[backend] || [], keepVal ? dv : (cat.depths[backend] || [])[0], _DEPTH_LABEL);
        row.replaceChild(nv, selV); row.replaceChild(nd, selD); selV = nv; selD = nd;
        selV.addEventListener('change', save); selD.addEventListener('change', save);
      }
      selB.addEventListener('change', function () { rebindVD(selB.value, false); save(); });
      selV.addEventListener('change', save); selD.addEventListener('change', save);
      var rst = document.createElement('button'); rst.className = 'ep-ams-rst'; rst.textContent = '默认';
      rst.addEventListener('click', function () {
        _setActionPref(action, '', '', '', '「' + (names[action] || action) + '」恢复默认');
        selB.value = def.backend; rebindVD(def.backend, true, def.variant, def.depth);
      });
      row.appendChild(selB); row.appendChild(selV); row.appendChild(selD); row.appendChild(rst);
      card.appendChild(nm); card.appendChild(df); card.appendChild(row);
      if (lockB.indexOf('gemini') >= 0) {
        var lk = document.createElement('div'); lk.className = 'ep-ams-cur'; lk.textContent = '(根 agent 切 Gemini 需二期工具循环,暂锁)';
        card.appendChild(lk);
      }
      return card;
    }
    function openModelSettings(focusAction) {
      fetch('/api/assistant/action-prefs').then(function (r) { return r.json(); }).then(function (d) {
        if (!d || !d.ok) { toast('拉取设置失败'); return; }
        var mask = document.createElement('div'); mask.className = 'ep-ams-mask';
        mask.addEventListener('click', function (e) { if (e.target === mask) mask.remove(); });
        var box = document.createElement('div'); box.className = 'ep-ams-box';
        var h = document.createElement('div'); h.className = 'ep-ams-h';
        var ht = document.createElement('span'); ht.textContent = '⚙ AI 模型设置';
        var x = document.createElement('button'); x.className = 'ep-ams-x'; x.textContent = '×';
        x.addEventListener('click', function () { mask.remove(); });
        h.appendChild(ht); h.appendChild(x); box.appendChild(h);
        var sub = document.createElement('div'); sub.className = 'ep-ams-sub';
        sub.textContent = '每个任务可单独设 后端/型号/深度,改完即时生效。跟感叹号「更强重答」共用同一套预设。';
        box.appendChild(sub);
        var _focusCard = null;
        function _renderActs(list) {
          list.forEach(function (a) {
            var ai = d.actions[a]; if (!ai) return;
            var c = _buildMsTask(a, { pref: ai.pref, def: ai.default }, d.catalog, d.names, d.locked || {});
            if (a === focusAction) _focusCard = c;
            box.appendChild(c);
          });
        }
        _renderActs(['orchestrator', 'summarize', 'vision']);
        var _rh = document.createElement('div'); _rh.className = 'ep-ams-sub';
        _rh.style.cssText = 'margin-top:12px;font-weight:600;color:#9fc0ff;';
        _rh.textContent = '— 阅读器其它 AI —';
        box.appendChild(_rh);
        _renderActs(['explain', 'translate', 'dict']);
        var note = document.createElement('div'); note.className = 'ep-ams-note';
        note.textContent = '标「免费」= 免费档支持该型号;但免费是**共享算力**,高峰常过载(503)或限流时会自动落付费保不中断——'
          + '此时这里会标「付费(过载/限流)」、感叹号里也显付费。flash 高峰过载较多;想更稳的免费可试 flash-lite 系。';
        box.appendChild(note);
        mask.appendChild(box); document.body.appendChild(mask);
        if (_focusCard) { try { _focusCard.style.outline = '2px solid #6aa3ff'; _focusCard.style.borderRadius = '8px'; _focusCard.scrollIntoView({ block: 'center' }); } catch (_) {} }
      }).catch(function () { toast('拉取设置失败'); });
    }
    // 回写 RC.assistant + window.openModelSettings,使 rc-settings.js「⚙ 打开 AI 模型设置」按钮 / inline 调用也能用同一份实现。
    try {
      if (!window.RC.assistant) window.RC.assistant = {};
      var _A = window.RC.assistant;
      _A.splitFollowups = _A.splitFollowups || splitFollowups;
      _A.renderFollowups = _A.renderFollowups || renderFollowups;
      _A.contextCard = _A.contextCard || contextCard;
      _A.openModelSettings = _A.openModelSettings || openModelSettings;
      window.openModelSettings = window.openModelSettings || openModelSettings;
    } catch (_) {}

    // ── notice 横幅 / 后台任务轮询 / 撤销 / AI 高亮撤销卡 / PWA 通知 ── 照搬 PDF
    function showNotice(text) {
      var n = document.createElement('div'); n.className = 'ep-asst-note'; n.textContent = text;
      aiBody.appendChild(n); aiBody.scrollTop = aiBody.scrollHeight;
    }
    // 开场白(空历史 / 清空后调)── 照搬 PDF greet,底座名词 页→章/节
    function greet() {
      var g = document.createElement('div'); g.className = 'ep-msg a';
      g.innerHTML = '我是这本书的阅读助手。试试:<br>· 这章讲什么 / 总结本章<br>· 翻译这段(先选中)<br>· 找讲XX的章跳过去<br>· 把这段做成卡片 / 整理成笔记<br><span style="color:#7a8497">(写入/制卡/高亮都会弹「查看详情·撤销·重做」卡,刷新后还在;对话云端保存、跨设备;🗑 清空)</span>';
      aiBody.appendChild(g); aiBody.scrollTop = aiBody.scrollHeight;
    }
    function notify(title, body) {
      try {
        if (!('Notification' in window) || Notification.permission !== 'granted') return;
        var opt = { body: body, tag: 'ep-asst-task', icon: '/static/icons/icon-192.png' };
        if (navigator.serviceWorker && navigator.serviceWorker.ready) navigator.serviceWorker.ready.then(function (reg) { reg.showNotification(title, opt); }).catch(function () { try { new Notification(title, opt); } catch (_) {} });
        else try { new Notification(title, opt); } catch (_) {}
      } catch (_) {}
    }
    function trackAsstTask(id, label) {
      if (!id) return;
      var line = document.createElement('div'); line.className = 'ep-msg a';
      line.innerHTML = '<span class="ep-asst-tool"><span class="ep-spin"></span> ' + esc(label || '处理') + '中…(后台进行中,可继续聊)</span>';
      aiBody.appendChild(line); aiBody.scrollTop = aiBody.scrollHeight;
      var n = 0;
      (function poll() {
        if (n++ > 120) { line.innerHTML = '<span class="ep-asst-tool">⌛ ' + esc(label) + ':等太久了</span>'; return; }
        fetch('/api/voice/task-status?id=' + encodeURIComponent(id)).then(function (r) { return r.json(); }).then(function (d) {
          if (!d || !d.ok) { return; }   // 对照 PDF trackTask:取不到状态直接停(不再 2500ms 重试)
          if (d.status === 'running') { if (d.step) line.innerHTML = '<span class="ep-asst-tool"><span class="ep-spin"></span> ' + esc(d.step) + '…</span>'; setTimeout(poll, 2000); return; }
          if (d.status === 'done') {
            try { if (d.client_actions && d.client_actions.length) runActions(d.client_actions); } catch (e) {}
            var undoId = d.result && d.result.undo_id;
            line.innerHTML = '✓ ' + esc(d.speak || '完成');
            notify('阅读助手 ✓', d.speak || '任务完成');
            if (undoId) _epTaskAction(undoId);   // 系统自动:制卡/笔记/生词完成 → 查看详情/撤销/重做 持久卡
          } else { line.innerHTML = '✗ ' + esc(d.error || '没办成'); }
          aiBody.scrollTop = aiBody.scrollHeight;
        }).catch(function () { setTimeout(poll, 3000); });
      })();
    }
    function renderUndo(parsed) {
      var line = document.createElement('div'); line.className = 'ep-msg a';
      var jump = (parsed.section != null) ? ' <button class="ep-asst-jump" data-idx="' + esc(parsed.section) + '">↗ 跳转</button>' : '';
      line.innerHTML = '✓ ' + esc(parsed.label || '完成') + jump + ' <button class="ep-asst-undo" data-uid="' + esc(parsed.undo_id) + '">↩ 撤销</button>';
      aiBody.appendChild(line); aiBody.scrollTop = aiBody.scrollHeight;
    }
    // AI 画完高亮 → 「跳转 + 撤销⇄重做」卡片。底座差异:存 CFI 数组(epub.js 注解),撤销=annotations.remove、重做=highlight 重加。
    var _assistEdits = {}, _aeCtr = 0;
    function _epAssistEdit(d) {
      if (!d || !d.cfis || !d.cfis.length) return;
      var eid = 'ae' + (++_aeCtr);
      _assistEdits[eid] = { cfis: d.cfis.slice(), color: d.color || '#ffd54a', undone: false };
      var card = document.createElement('div'); card.className = 'ep-msg a ep-edit-card';
      card.innerHTML = '<div class="ep-edit-h">✏️ AI 已高亮 ' + d.cfis.length + ' 处</div>' +
        '<div class="ep-edit-row"><button class="ep-asst-jump" data-idx="' + d.section + '">→ 跳到此处</button>' +
        '<button class="ep-edit-undo" data-eid="' + eid + '">↩ 撤销</button></div>';
      aiBody.appendChild(card); aiBody.scrollTop = aiBody.scrollHeight;
    }

    // ════════════════════════════════════════════════════════════════════════
    // 写操作「撤销/重做」持久卡(系统自动生成,不靠 AI):每次写文件/Anki/高亮/笔记/生词 → 一张
    //   <title> [查看详情][↩撤销][↪重做] 卡;持久化进对话(刷新后还在、undo/redo 还能用)。
    //   action 记录 = 后端给的结构 {id,kind,title,detail,undo,redo,state}:
    //     · 高亮:前端建(服务端拿不到 CFI),调 /epub-action {op:attach} 落库;
    //     · 制卡/笔记/生词:后台任务完成后拿 undo_id 调 /epub-action {op:from_task} 由后端建+落库;
    //     · 同步写工具(预留):后端 SSE 'action' 事件直接给。
    // ════════════════════════════════════════════════════════════════════════
    var ICON_OF = { make_anki: '🎴', add_vocab: '📒', make_note: '🗒️', epub_highlight: '🖍️', auto_highlight: '🖍️' };
    function _epActSync(card) {                  // 据 state 切按钮可用态 + 卡片视觉
      var rec = card.__act || {}, undone = rec.state === 'undone';
      var bu = card.querySelector('.ep-act-undo'), br = card.querySelector('.ep-act-redo');
      if (bu) bu.disabled = undone;
      if (br) br.disabled = !undone;
      card.classList.toggle('ep-act-undone', undone);
    }
    // 高亮类 action 的客户端副作用:undo→抹注解;redo→照 CFI 重画注解(服务端只管 sidecar,阅读区注解在前端)
    function _epActClientFx(rec, toState) {
      if (!rec || (rec.kind !== 'epub_highlight' && rec.kind !== 'auto_highlight')) return;
      var items = (rec.redo && rec.redo.items) || [];
      items.forEach(function (it) {
        var cfi = it && it.cfi; if (!cfi) return;
        try {
          if (toState === 'undone') R.annotations.remove(cfi, 'highlight');
          else R.annotations.highlight(cfi, {}, null, 'ep-asst-hl', { fill: (it.color || '#ffd54a'), 'fill-opacity': '0.40' });
        } catch (e) {}
      });
    }
    function _epActDo(card, op) {
      var rec = card.__act; if (!rec) return;
      var btn = card.querySelector(op === 'undo' ? '.ep-act-undo' : '.ep-act-redo');
      if (!btn || btn.disabled) return;
      var old = btn.textContent; btn.disabled = true; btn.textContent = (op === 'undo' ? '撤销中…' : '重做中…');
      reqJson('POST', '/pdf/api/epub-action', { op: op, file: FREL, action: rec },
        function (d) {
          rec = card.__act = d.action || rec;
          rec.state = d.state || (op === 'undo' ? 'undone' : 'done');
          _epActClientFx(rec, rec.state);
          var box = card.querySelector('.ep-act-detail-box'); if (box && rec.detail) box.textContent = rec.detail;
          btn.textContent = old; _epActSync(card);
        },
        function (er) { btn.disabled = false; btn.textContent = old; toast((op === 'undo' ? '撤销' : '重做') + '失败:' + er); });
    }
    function _epActionCard(rec) {
      if (!rec || !rec.id) return null;
      var card = document.createElement('div'); card.className = 'ep-msg a ep-act-card';
      card.dataset.aid = rec.id; card.__act = rec;
      var h = document.createElement('div'); h.className = 'ep-act-h';
      h.textContent = (ICON_OF[rec.kind] || '✏️') + ' ' + (rec.title || '写操作');
      var row = document.createElement('div'); row.className = 'ep-act-row';
      var bd = document.createElement('button'); bd.className = 'ep-act-btn ep-act-detail-btn'; bd.textContent = '查看详情';
      var bu = document.createElement('button'); bu.className = 'ep-act-btn ep-act-undo'; bu.textContent = '↩ 撤销';
      var br = document.createElement('button'); br.className = 'ep-act-btn ep-act-redo'; br.textContent = '↪ 重做';
      var box = document.createElement('div'); box.className = 'ep-act-detail-box'; box.style.display = 'none';
      box.textContent = rec.detail || '(无详情)';
      bd.addEventListener('click', function () { box.style.display = (box.style.display === 'none') ? 'block' : 'none'; aiBody.scrollTop = aiBody.scrollHeight; });
      bu.addEventListener('click', function () { _epActDo(card, 'undo'); });
      br.addEventListener('click', function () { _epActDo(card, 'redo'); });
      row.appendChild(bd); row.appendChild(bu); row.appendChild(br);
      card.appendChild(h); card.appendChild(row); card.appendChild(box);
      _epActSync(card);
      return card;
    }
    function _epShowAction(rec) { var c = _epActionCard(rec); if (c) { aiBody.appendChild(c); aiBody.scrollTop = aiBody.scrollHeight; } return c; }
    // 前端建的 action(高亮)→ 落库(upsert by id,幂等)。流式中先等本轮 assistant 消息落库再 attach(防 attach 落到 user 消息上)。
    var _epPending = [];
    function _epFlushActions() {
      if (!_epPending.length) return;
      if (_streaming) { setTimeout(_epFlushActions, 300); return; }
      var batch = _epPending.slice(); _epPending = [];
      fetch('/pdf/api/epub-action', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ op: 'attach', file: FREL, actions: batch }) }).catch(function () { /* 落库失败:卡仍在本会话可用,下次刷新前手动操作仍生效 */ });
    }
    function _epQueueAction(rec) { if (rec) { _epPending.push(rec); _epFlushActions(); } }
    // 高亮写完(epubHighlight 串行收尾)→ 据 created 建一张批量撤销/重做卡 + 落库
    function _epHlAction(created, color, isAuto) {
      var withId = (created || []).filter(function (c) { return c && c.id; });
      if (!withId.length) return;
      var items = withId.map(function (c) { return { cfi: c.cfi, section: c.section, text: c.text, color: c.color || color }; });
      var rec = {
        id: 'act_hl_' + Date.now() + '_' + (_aeCtr++),
        kind: isAuto ? 'auto_highlight' : 'epub_highlight',
        title: '高亮:' + withId.length + ' 处',
        detail: items.map(function (it) { return '· ' + String(it.text || '').slice(0, 90); }).join('\n'),
        undo: { op: 'hl_delete', file: FREL, ids: withId.map(function (c) { return c.id; }) },
        redo: { op: 'hl_create', file: FREL, items: items },
        state: 'done', ts: Math.floor(Date.now() / 1000)
      };
      _epShowAction(rec); _epQueueAction(rec);
    }
    // 后台任务(制卡/笔记/生词)完成 → 拿 undo_id 让后端建完整 action(含快照)+ 落库,再渲卡
    function _epTaskAction(undoId) {
      if (!undoId) return;
      fetch('/pdf/api/epub-action', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ op: 'from_task', file: FREL, undo_id: undoId }) })
        .then(function (r) { return r.json(); })
        .then(function (d) { if (d && d.ok && d.action) _epShowAction(d.action); })
        .catch(function () {});
    }
    // 气泡区委托:章节链接跳转 / ↗跳转 chip / ↩撤销 / 高亮卡撤销⇄重做
    aiBody.addEventListener('click', function (e) {
      var t = e.target;
      var cl = t.closest && t.closest('.ep-chaplink');
      if (cl) { var ci = parseInt(cl.dataset.idx, 10); if (!isNaN(ci)) { if (window.RC && RC.sidedrawer) RC.sidedrawer.close(); window.jumpTo(ci); } return; }
      var jp = t.closest && t.closest('.ep-asst-jump');
      if (jp) {
        if (window.RC && RC.sidedrawer) RC.sidedrawer.close();
        var jcfi = jp.dataset.cfi;
        if (jcfi) { _epRecordBack(); try { R.display(jcfi); } catch (e) {} _epAfterJump(); }   // 逐条高亮列表:按 CFI 精确定位到该高亮 + 弹返回条
        else { var ji = parseInt(jp.dataset.idx, 10); if (!isNaN(ji)) window.jumpTo(ji); }
        return;
      }
      var hd = t.closest && t.closest('.ep-hl-del');   // showHlPicker 里的「🗑删除」:删该条(不替用户批量删)
      if (hd) {
        var hid = hd.dataset.id; if (!hid) return;
        var hcfi = hd.dataset.cfi || '';
        hd.disabled = true; hd.textContent = '删除中…';
        fetch('/pdf/api/epub-highlights?file=' + encodeURIComponent(FREL) + '&id=' + encodeURIComponent(hid), { method: 'DELETE' })
          .then(function (r) { return r.json(); })
          .then(function (dd) {
            if (dd && dd.ok) {
              if (hcfi) { try { R.annotations.remove(hcfi, 'highlight'); } catch (e) {} }   // 立刻抹掉阅读区注解(不等刷新)
              var row = hd.closest('.ep-hl-row');
              if (row) { row.style.opacity = '.45'; var tx = row.querySelector('.ep-hl-tx'); if (tx) tx.style.textDecoration = 'line-through'; }
              hd.outerHTML = '<span class="ep-asst-tool">🗑 已删</span>';
            } else { hd.disabled = false; hd.textContent = '🗑 删除'; toast('删除失败:' + ((dd && dd.error) || '')); }
          })
          .catch(function () { hd.disabled = false; hd.textContent = '🗑 删除'; });
        return;
      }
      var ub = t.closest && t.closest('.ep-asst-undo');
      if (ub) {
        var uid = ub.getAttribute('data-uid'); if (!uid) return;
        ub.disabled = true; ub.textContent = '撤销中…';
        reqJson('POST', '/api/assistant/undo', { id: uid },
          function () { ub.outerHTML = '<span class="ep-asst-tool">↩ 已撤销</span>'; },
          function (er) { ub.disabled = false; ub.textContent = '↩ 撤销'; toast('撤销失败:' + er); });
        return;
      }
      var eb = t.closest && t.closest('.ep-edit-undo');
      if (eb) {
        var eid = eb.getAttribute('data-eid'); var stt = _assistEdits[eid]; if (!stt) return;
        if (!stt.undone) {
          stt.cfis.forEach(function (cfi) { try { R.annotations.remove(cfi, 'highlight'); } catch (e) {} });
          stt.undone = true; eb.textContent = '↪ 重做';
        } else {
          stt.cfis.forEach(function (cfi) { try { R.annotations.highlight(cfi, {}, null, 'ep-asst-hl', { fill: stt.color, 'fill-opacity': '0.40' }); } catch (e) {} });
          stt.undone = false; eb.textContent = '↩ 撤销';
        }
        return;
      }
    });

    // ── 断线恢复 / 流式停止按钮 ── 照搬 PDF
    var _ridCtr = 0, _asstAbort = null, _asstRecovering = false, _asstLastTs = 0, _histLoaded = false, _streaming = false, chat = [];
    function _whenVisible() {
      return new Promise(function (res) {
        if (document.visibilityState !== 'hidden') return res();
        var h = function () { if (document.visibilityState !== 'hidden') { document.removeEventListener('visibilitychange', h); res(); } };
        document.addEventListener('visibilitychange', h);
      });
    }
    function _recoverAsst(tries) {
      tries = tries || 0;
      return fetch('/pdf/api/epub-convo?file=' + encodeURIComponent(FREL)).then(function (r) { return r.json(); }).then(function (d) {
        if (d && d.ok && d.messages && d.messages.length) {
          var last = d.messages[d.messages.length - 1];
          if (last && last.role === 'assistant' && last.content) return last;
        }
        if (tries < 2) return new Promise(function (rs) { setTimeout(rs, 800); }).then(function () { return _recoverAsst(tries + 1); });
        return null;
      }).catch(function () { return null; });
    }
    function _setSend(stop) {
      var sb = $('ep-ai-send'); if (!sb) return;
      if (stop) { sb.classList.add('stop'); sb.textContent = '■'; sb.title = '停止'; }
      else { sb.classList.remove('stop'); sb.textContent = '➤'; sb.title = '发送'; }
    }

    // ── 逐字浮现(揭示游标)── 照搬 PDF 的 _streamWrap / _revealTick / _appendCaret(ep-mfx- 前缀)
    function _appendCaret(el) { try { var c = document.createElement('span'); c.className = 'ep-mfx-caret'; el.appendChild(c); } catch (_) {} }
    // 把 el 正文按 字/词 切片包进 .ep-mfx-w(返回 {spans,total})。下标 < revN 的字打 .ep-mfx-shown(即时,不重播);
    // ≥ revN 的默认隐藏,由 _revealTick 游标推进时逐个加 .ep-mfx-reveal 淡入。光标放在揭示 frontier 后面。
    function _streamWrap(el, revN) {
      var idx = 0, spans = [];
      function walk(node) {
        var kids = Array.prototype.slice.call(node.childNodes);
        for (var i = 0; i < kids.length; i++) {
          var n = kids[i];
          if (n.nodeType === 3) {
            var toks = (n.nodeValue || '').match(/[一-鿿　-〿＀-￯]|[A-Za-z0-9]+(?:['’][A-Za-z]+)?|[^\sA-Za-z0-9一-鿿　-〿＀-￯]|\s+/g) || [];
            if (!toks.length) continue;
            var frag = document.createDocumentFragment();
            toks.forEach(function (p) {
              if (/^\s+$/.test(p)) { frag.appendChild(document.createTextNode(p)); return; }
              var s = document.createElement('span'); s.className = 'ep-mfx-w'; s.textContent = p;
              if (idx < revN) { s.classList.add('ep-mfx-shown'); }
              frag.appendChild(s); spans.push(s); idx++;
            });
            node.replaceChild(frag, n);
          } else if (n.nodeType === 1 && n.className !== 'ep-mfx-caret') {
            walk(n);
          }
        }
      }
      try { walk(el); } catch (_) { return { spans: [], total: idx }; }
      var f = spans[Math.min(revN, spans.length) - 1];
      var c = document.createElement('span'); c.className = 'ep-mfx-caret';
      if (f && f.parentNode) { f.parentNode.insertBefore(c, f.nextSibling); } else { el.appendChild(c); }
      return { spans: spans, total: idx };
    }
    function _fadeInAfter(el) {
      try {
        var xs = el.querySelectorAll('.ep-followups,.ep-fb-bar');
        Array.prototype.forEach.call(xs, function (x, k) { x.classList.add('ep-mfx-after'); setTimeout(function () { x.classList.add('on'); }, 80 + k * 120); });
      } catch (_) {}
    }

    // ── 主流程:sendChat → runAssistant(SSE 流式 + 逐字浮现 + FOLLOWUP 剥离 + rid 重连 + 历史恢复)──
    function sendChat(presetText) {
      if (_streaming) return;
      var ta = $('ep-ai-ta');
      var msg = (presetText != null ? presetText : (ta.value || '')).trim();
      var selInfo = curSelection();
      var _figs = (window.__figAttached || []);
      if (!msg) {   // 空输入但有选中/带入图 → 等于"就问这个",用默认问法直接发(照搬 PDF send 的空文本分支)
        if (_figs.length) msg = '讲讲这张图';
        else if (selInfo.sel) msg = (/\$/.test(selInfo.sel) ? '讲讲这个公式' : '讲讲这段');
        else return;   // 真·空(无任何上下文)→ 不发
      }
      micStop(); if (presetText == null) { ta.value = ''; ta.style.height = 'auto'; }
      var um = document.createElement('div'); um.className = 'ep-msg u'; um.textContent = msg; aiBody.appendChild(um);
      var _ci = _liveCurIdx();
      // 上下文卡:有选中→选中行;否则仅当问题指向「本章/节」(_pageRefersToPage)才给「正在看」行 ── 照搬 PDF _ctxCard(纯概念问题不显卡)
      var items = [];
      if (selInfo.sel) items.push({ text: '选中:' + selInfo.sel, formula: /\$/.test(selInfo.sel), onClick: function () { window.jumpTo(_ci); } });
      if (!selInfo.sel && !_figs.length && _pageRefersToPage(msg)) items.push({ text: '正在看:' + sectName(_ci), onClick: function () { window.jumpTo(_ci); } });
      var cc = contextCard(items); if (cc) um.appendChild(cc);
      var am = document.createElement('div'); am.className = 'ep-msg a'; am.innerHTML = '<span class="ep-mfx-typing"><i></i><i></i><i></i></span>'; aiBody.appendChild(am);
      aiBody.scrollTop = aiBody.scrollHeight;
      chat.push({ role: 'user', content: msg });
      _streaming = true; _setSend(true);
      runAssistant(msg, selInfo, am);
    }

    async function runAssistant(message, selInfo, am, opts) {
      var context = {
        file: FREL, book: CFG.fileName || '',
        current_section_idx: _liveCurIdx(), total_sections: COUNT, toc: TOC,
        selection: selInfo.sel || '', selection_sentence: selInfo.sent || '',
        figures: (window.__figAttached || [])   // 带入的图(epub2-extra 设 window.__figAttached)→ 随请求发,后端 epub_assistant 接 image 走视觉
      };
      // 模型/深度:只「感叹号重答」opts 时强制(跟 PDF 一致)。设置面板 model/effort **不**强制助手 —— 那是给翻译/解释/词典用的;
      // 助手模型走自己的 ⚙(openModelSettings,per-action 服务端预设)。强制设置面板模型会绕过省钱的 Gemini 路由,故不接。
      var _fm = (opts && opts.forceModel) || undefined;
      var _fe = (opts && opts.forceEffort) || undefined;
      var rid = 'e' + Date.now() + '_' + (_ridCtr++);
      var evSeen = 0, done = false, aborted = false, answer = '', traceData = null, spinner = true;
      // 逐字浮现状态(每轮独立)
      var _revN = 0, _spans = [], _tot = 0, _raf = null, _lastTs = 0, _acc = 0, _noChar = false;
      function _stopReveal() { if (_raf) { try { cancelAnimationFrame(_raf); } catch (_) {} _raf = null; } }
      function _revealTick(ts) {
        _raf = null;
        if (!_streaming) return;
        if (!_lastTs) _lastTs = ts;
        var dt = Math.min(ts - _lastTs, 120); _lastTs = ts;   // clamp:切后台回来 dt 巨大,别一次灌完
        var backlog = _tot - _revN;
        if (backlog > 0) {
          var rate = 0.05 * (1 + backlog / 40);               // 字/ms:落后越多揭示越快,追上自然放慢
          _acc += dt * rate;
          var n = Math.min(backlog, Math.floor(_acc), 6);     // 每帧上限 6,防一次性灌入又变"段"
          if (n > 0) {
            _acc -= n;
            for (var k = 0; k < n; k++) { var sp = _spans[_revN]; if (sp) sp.classList.add('ep-mfx-reveal'); _revN++; }
            var c = am.querySelector('.ep-mfx-caret'), f = _spans[_revN - 1];
            if (c && f && f.parentNode) f.parentNode.insertBefore(c, f.nextSibling);
            aiBody.scrollTop = aiBody.scrollHeight;
          }
        }
        if (_streaming) _raf = requestAnimationFrame(_revealTick);
      }

      function setTool(label) { am.innerHTML = '<span class="ep-asst-tool">🔧 ' + esc(label) + '…</span>'; spinner = true; aiBody.scrollTop = aiBody.scrollHeight; }
      function setThinking() { am.innerHTML = '<span class="ep-mfx-typing"><i></i><i></i><i></i></span>'; spinner = true; aiBody.scrollTop = aiBody.scrollHeight; }
      function handleEv(ev, parsed) {
        if (ev === 'meta') return;        // rid 确认,不计数
        evSeen++;
        if (ev === 'done') { done = true; return; }
        if (ev === 'tool') { setTool(parsed); }
        else if (ev === 'tool-done') { if (spinner) setThinking(); }   // EPUB 后端特有:工具结束 → 回到「思考中」(PDF 无此事件)
        else if (ev === 'answer') {   // 流式轻量渲(不 MathJax)+ 剥 FOLLOWUP + 逐字浮现(揭示游标)+ 光标
          answer = parsed; var _at = splitFollowups(answer).text;
          renderMdEl(am, _at, false); am.classList.add('ep-mfx-streaming');
          if (!_noChar && _at.length > 5000) { _noChar = true; _stopReveal(); }   // 超长答案:停揭示,改普通(保性能)
          if (_noChar) { _appendCaret(am); }
          else {
            var w = _streamWrap(am, _revN); _spans = w.spans; _tot = w.total;   // 重渲后重包:已揭示打 shown,新字等游标
            if (_revN > _tot) _revN = _tot;
            if (!_raf) { _lastTs = 0; _raf = requestAnimationFrame(_revealTick); }   // 启动/续跑揭示循环
          }
          spinner = false; aiBody.scrollTop = aiBody.scrollHeight;
        }
        else if (ev === 'notice') showNotice(parsed);
        else if (ev === 'actions') { try { runActions(parsed); } catch (e) {} }   // 实时:工具一执行完就应用(高亮/跳章立即生效)
        else if (ev === 'trace') traceData = parsed;   // 调用链 → 喂「!」反馈弹窗
        else if (ev === 'task') trackAsstTask(parsed && parsed.task_id, parsed && parsed.label);
        else if (ev === 'action' && parsed && parsed.id) { _epShowAction(parsed); _epQueueAction(parsed); }   // 同步写工具:系统自动弹撤销/重做卡 + 落库
        else if (ev === 'undo' && parsed && parsed.undo_id) renderUndo(parsed);
        else if (ev === 'error') { answer = '⚠️ ' + parsed; _stopReveal(); am.classList.remove('ep-mfx-streaming'); am.textContent = answer; spinner = false; }
      }
      async function streamOnce(body) {
        _asstAbort = (typeof AbortController !== 'undefined') ? new AbortController() : null;
        var res = await fetch('/pdf/api/epub-assistant', {
          method: 'POST', headers: { 'Content-Type': 'application/json', 'Accept': 'text/event-stream' },
          body: JSON.stringify(body), signal: _asstAbort ? _asstAbort.signal : undefined
        });
        if (res.status === 410) { done = 'gone'; return; }   // 任务已过期(>3min)→ 走历史恢复
        if (!res.ok || !res.body) throw new Error('HTTP ' + res.status);
        var reader = res.body.getReader(), dec = new TextDecoder(), buf = '';
        while (true) {
          var rd = await reader.read(); if (rd.done) break;
          _asstLastTs = Date.now();   // 有数据 = 流活着
          buf += dec.decode(rd.value, { stream: true });
          var idx;
          while ((idx = buf.indexOf('\n\n')) >= 0) {
            var chunk = buf.slice(0, idx); buf = buf.slice(idx + 2);
            var ev = 'message', data = '';
            chunk.split('\n').forEach(function (line) {
              if (line.indexOf('event:') === 0) ev = line.slice(6).trim();
              else if (line.indexOf('data:') === 0) data += line.slice(5).trim();
            });
            var parsed; try { parsed = JSON.parse(data); } catch (e) { parsed = data; }
            handleEv(ev, parsed);
            if (done) return;
          }
        }
      }
      _asstRecovering = false; _asstLastTs = Date.now();
      var tries = 0;
      while (!done && !aborted) {
        try {
          await streamOnce(tries === 0
            ? { message: message, context: context, rid: rid, from: 0, force_effort: _fe, force_model: _fm }
            : { rid: rid, from: evSeen });
        } catch (e) {
          if (e && e.name === 'AbortError') {
            if (_asstRecovering) { _asstRecovering = false; }   // 看门狗掐死僵死流 → 当断线,重连续传
            else { aborted = true; break; }                     // 用户点停止 → 保留已生成部分
          }
          // 其它(Load failed / 网络断)→ 落到下面重连
        }
        if (done || done === 'gone' || aborted) break;
        if (++tries > 40) break;
        try { setTool('连接断开,正在续传'); } catch (e) {}
        await _whenVisible();
        await new Promise(function (rs) { setTimeout(rs, Math.min(400 * tries, 2000)); });
      }
      // 任务过期 / 兜底没续上 → 从服务端历史恢复(worker 跑完已落库,绝不丢)
      if ((done === 'gone' || (!done && !aborted)) && !answer) {
        try { setTool('正在恢复'); } catch (e) {}
        var rec = await _recoverAsst();
        if (rec && rec.content) { answer = rec.content; traceData = rec.trace || traceData; }
      }
      // 收尾:停揭示 → 剥 FOLLOWUP → 完整渲染(MathJax 这一次)→ 追问 chip → 反馈条 → 错峰淡入
      _stopReveal(); am.classList.remove('ep-mfx-streaming');
      var pf = splitFollowups(answer);
      if (pf.text) { renderMdEl(am, pf.text, true); chat.push({ role: 'assistant', content: answer }); }
      else if (spinner) { am.textContent = aborted ? '(已停止)' : '(没拿到回答)'; }
      if (!aborted) { try { renderFollowups(am, pf.followups, function (q) { $('ep-ai-ta').value = q; sendChat(); }); } catch (e) {} }
      if (!aborted && pf.text) { try { attachTrace(am, traceData, Math.floor(Date.now() / 1000)); } catch (e) {} }
      if (!aborted) { try { _fadeInAfter(am); } catch (e) {} }
      _streaming = false; _setSend(false); _asstAbort = null; _asstRecovering = false;
      try { _epFlushActions(); } catch (e) {}   // 本轮 assistant 消息已落库 → 把排队的写操作卡 attach 进它的 meta.actions
    }

    // 切后台→回前台:iOS 常把进行中的 SSE 掐死/僵死 → 回来 3s 没新进度就主动 abort,走 rid 重连
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState !== 'visible' || !_streaming) return;
      setTimeout(function () {
        if (_streaming && !_asstRecovering && (Date.now() - _asstLastTs > 3000)) {
          _asstRecovering = true; try { _asstAbort && _asstAbort.abort(); } catch (e) {}
        }
      }, 3000);
    });

    // ── 历史:回灌服务端保存的对话(跨设备续上;一次性 guard)── 照搬 PDF。#ep-ai-body 与 snippets 卡共用,
    //    故仅当有历史才清空重灌;无历史不动 body(不覆盖 snippets 卡)。
    function loadHistory() {
      if (_histLoaded) return; _histLoaded = true;
      fetch('/pdf/api/epub-convo?file=' + encodeURIComponent(FREL)).then(function (r) { return r.json(); }).then(function (d) {
        if (!d || !d.ok || !d.messages || !d.messages.length) { greet(); return; }   // 空历史 → 开场白(append,不清 body,留 snippets 卡)
        aiBody.innerHTML = ''; chat = [];
        d.messages.forEach(function (m) {
          if (m.role === 'user') {
            var um = document.createElement('div'); um.className = 'ep-msg u'; um.textContent = m.content || ''; aiBody.appendChild(um);
            // 同 sendChat:选中行优先;无选中时仅当问题指向本章才给「正在看」行 ── 照搬 PDF _ctxCard(纯概念历史问题不显卡)
            var items = [];
            if (m.selection) items.push({ text: '选中:' + m.selection, formula: /\$/.test(m.selection) });
            if (m.section != null && !m.selection && _pageRefersToPage(m.content || '')) { var _sx = m.section; items.push({ text: '正在看:' + sectName(_sx), onClick: function () { window.jumpTo(_sx); } }); }
            var cc = contextCard(items); if (cc) um.appendChild(cc);
            chat.push({ role: 'user', content: m.content || '' });
          } else {
            var am = document.createElement('div'); am.className = 'ep-msg a'; aiBody.appendChild(am);
            var pf = splitFollowups(m.content || '');
            renderMdEl(am, pf.text, true);
            try { renderFollowups(am, pf.followups, function (q) { $('ep-ai-ta').value = q; sendChat(); }); } catch (e) {}
            try { attachTrace(am, m.trace || null, m.ts || null); } catch (e) {}
            // 持久化回放:这条回合做过的写操作 → 逐个按 state 重渲撤销/重做卡(done→可撤销;undone→可重做)
            if (Array.isArray(m.actions)) m.actions.forEach(function (a) { try { _epShowAction(a); } catch (e) {} });
            chat.push({ role: 'assistant', content: m.content || '' });
          }
        });
        aiBody.scrollTop = aiBody.scrollHeight;
        requestAnimationFrame(function () { aiBody.scrollTop = aiBody.scrollHeight; });
        setTimeout(function () { aiBody.scrollTop = aiBody.scrollHeight; }, 250);
      }).catch(function () {});
    }

    // ── 发送 / 输入框 / 底部快捷 ──
    $('ep-ai-send').addEventListener('click', function () {
      if (_streaming) { try { _asstAbort && _asstAbort.abort(); } catch (e) {} return; }   // 流式中点 ■ → 中止本轮
      sendChat();
    });
    $('ep-ai-ta').addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); micStop(); sendChat(); } });
    $('ep-ai-ta').addEventListener('input', function () { this.style.height = 'auto'; this.style.height = Math.min(120, this.scrollHeight) + 'px'; });
    $('ep-asst-quick').addEventListener('click', function (e) {
      var b = e.target.closest('button'); if (!b) return;
      if (b.dataset.q === 'models') { try { openModelSettings(); } catch (_) {} return; }
      if (_streaming) return;
      if (b.dataset.q === 'clear') {
        aiBody.innerHTML = ''; chat = []; _histLoaded = true;
        reqJson('POST', '/pdf/api/epub-convo/clear', { file: FREL }, function () {}, function () {});
        greet();   // 清空后给开场白(对照 PDF clear → greet())
        return;
      }
      if (b.dataset.send) { sendChat(b.dataset.send); }
    });

    // ── 苹果风格语音按钮(逐字照搬 PDF 25-assistant.js):持续聆听,只手动停。无 SR → 聚焦输入框用系统键盘听写。 ──
    var micBtn = $('ep-ai-mic');
    var ta = $('ep-ai-ta');
    function autorow() { ta.style.height = 'auto'; ta.style.height = Math.min(120, ta.scrollHeight) + 'px'; }
    var _SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    var micRec = null, micOn = false, micCommitted = '', micSessFinal = '', micSessTok = null, micLastWrite = '';
    var micStartTs = 0, micLastStart = 0, micFails = 0, micSessProductive = false;
    function micStop() {
      if (!micOn) return;
      micOn = false; micSessTok = null; micFails = 0;
      try { micRec && micRec.stop(); } catch (_) {}
      micBtn.classList.remove('on');
    }
    function micSpin() {
      if (!micOn) return;
      var tok = (micSessTok = {});
      var thisRec;
      try {
        thisRec = micRec = new _SR();
        micRec.lang = 'zh-CN'; micRec.interimResults = true; micRec.continuous = true; micRec.maxAlternatives = 1;
        micSessFinal = ''; micSessProductive = false; micLastStart = Date.now();
        micRec.onresult = function (e) {
          if (!micOn || micSessTok !== tok) return;
          micSessProductive = true;
          var f = '', it = '';
          for (var i = 0; i < e.results.length; i++) {
            if (e.results[i].isFinal) f += e.results[i][0].transcript; else it += e.results[i][0].transcript;
          }
          micSessFinal = f;
          ta.value = micCommitted + f + it; micLastWrite = ta.value; autorow();
        };
        micRec.onerror = function (ev) {
          if (ev && (ev.error === 'not-allowed' || ev.error === 'service-not-allowed' || ev.error === 'audio-capture')) micOn = false;
        };
        micRec.onend = function () {
          if (micRec !== thisRec) return;
          if (micSessTok === tok && micSessFinal) { micCommitted = (micCommitted + micSessFinal).replace(/\s+$/, '') + ' '; }
          micSessFinal = '';
          micBtn.classList.remove('on');
          if (!micOn) { autorow(); return; }
          if (Date.now() - micStartTs > 120000) { micStop(); return; }
          if (!micSessProductive && (Date.now() - micLastStart) < 1200) { if (++micFails >= 5) { micStop(); return; } }
          else micFails = 0;
          micBtn.classList.add('on');
          setTimeout(function () { if (micOn && micRec === thisRec) micSpin(); }, micFails ? 700 : 0);
        };
        micRec.start();
      } catch (_) { micOn = false; micSessTok = null; micBtn.classList.remove('on'); ta.focus(); }
    }
    function micStart() {
      if (!_SR) { ta.focus(); return; }
      micOn = true; micFails = 0; micStartTs = Date.now(); micBtn.classList.add('on');
      micCommitted = ta.value ? (ta.value.replace(/\s+$/, '') + ' ') : '';
      micSessFinal = ''; micLastWrite = ta.value;
      micSpin();
    }
    ta.addEventListener('input', function () {
      if (!micOn || ta.value === micLastWrite) return;
      micSessTok = null;
      micCommitted = ta.value; micLastWrite = ta.value; micSessFinal = '';
      try { micRec && micRec.stop(); } catch (_) {}
    });
    document.addEventListener('visibilitychange', function () { if (document.hidden) micStop(); });
    if (!_SR) micBtn.title = '点这里→用键盘的听写麦克风';
    micBtn.addEventListener('click', function () { micOn ? micStop() : micStart(); });

    // ── 冷启动预热 + 通知权限(开抽屉到助手 tab 时)── 照搬 PDF fab(prewarm + Notification.requestPermission + focus)。
    //    EPUB 无 fab(抽屉由把手/tab 开),改用 MutationObserver 观察「#ep-side.open 且助手 pane 激活」的进入沿触发。
    function prewarm(off) { try { fetch('/api/assistant/prewarm', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(off ? { off: 1 } : {}), keepalive: true }); } catch (_) {} }
    window.__epAsstPrewarm = function () { try { prewarm(false); } catch (_) {} };
    function _onAsstOpen() {
      prewarm(false);
      try { if ('Notification' in window && Notification.permission === 'default') Notification.requestPermission().catch(function () {}); } catch (_) {}
      setTimeout(function () { var t = $('ep-ai-ta'); if (t) t.focus(); }, 250);
    }
    (function watchDrawer() {
      try {
        var sideEl = document.getElementById('ep-side'); if (!sideEl || !window.MutationObserver) return;
        var _asstShown = false;
        function _check() {
          var open = sideEl.classList.contains('open');
          var pane = sideEl.querySelector('.ep-side-pane.active');
          var on = open && pane && pane.dataset && pane.dataset.pane === 'asst';
          if (on && !_asstShown) { _asstShown = true; _onAsstOpen(); }   // 进入沿:每次「开到助手 tab」触发一次,离开后再武装
          else if (!on) { _asstShown = false; }
        }
        new MutationObserver(_check).observe(sideEl, { attributes: true, attributeFilter: ['class'], subtree: true });   // subtree:同时盖 #ep-side.open 与 pane.active 两类 class 变更
        _check();   // 若进页面时抽屉已开在助手 tab 则立即预热
      } catch (_) {}
    })();

    // ── 历史预灌:抽屉默认 tab=asst,本模块自包含 → 进页面即预灌一次(_histLoaded guard 防重复)── 照搬 PDF
    setTimeout(loadHistory, 300);
    dbg('epub2-assist ready');
  }
})();
