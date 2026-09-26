/* rc-turncard.js — 轮次容器(Turn Container),共享层(PDF + EPUB 共用)。
 * 设计见 references/adr-turn-container.md(用户提出,2026-07-14)。
 *
 * 一次「带工具的回答」在时间上本来就是一个整体:
 *   ①AI 先说「我去查一下，稍等」 ②工具执行 ③AI 说出正答 (④有些工具还自带结果卡)
 * 旧实现把它们**事后拼接**(气泡 + 工具卡 + 结果卡 + absorb 追认),于是"谁先到谁认领谁"成了竞态
 * —— 2026-07-14 一天就因此出了三个 bug(两张卡 / 前置语显示两遍 / 按钮白块),而且拼接结果
 * 只活在浏览器内存里,刷新即退化成纯文本。
 *
 * 本文件把它改成:**容器 + 多次注入**。
 *   容器按 turn_id 建立;内容以 part 按序注入;**实时与历史回放走同一个 renderPart()**。
 *   没有升格、没有顶替、没有认领竞争 —— 那类 bug 在架构上不再可能发生。
 *
 * 三条不变式(违反了就是 bug):
 *   ① 渲染器唯一:实时与回放必须调同一个 renderPart()。
 *   ② part 只追加不重写。**唯一例外**:正在流的那个 text part 是 draft,可就地更新;
 *      response 结束即 freeze,之后不可变。(流式必须的妥协,已在 ADR 记明)
 *   ③ 服务端持有全量 part;前端内存永远不是真相源。
 */
(function () {
  var RC = (window.RC = window.RC || {});
  if (RC.turnCard) return;

  var _turns = {};        // turn_id → {el, hd, parts:[], bd, flow, draft}
  var _cur = null;        // 当前轮 turn_id
  var _streamVersion = 0;
  function _nativePresentation() { return window.__BW_NATIVE_CONVERSATION_DATA__ === true; }
  function _lookup(tid) { var alias = tidByTurnId(tid); return _turns[tid] || (alias && _turns[alias]); }

  function _thread() { return document.getElementById('asst-thread'); }
  function _esc(s) { var d = document.createElement('div'); d.textContent = String(s == null ? '' : s); return d.innerHTML; }
  function _md(el, text) {
    try {
      if (RC.assistant && RC.assistant.renderMd) { RC.assistant.renderMd(el, text, true); return; }
    } catch (e) {}
    try {   // 非阅读器页面(如工具库)没有 renderMd → 退 RC.md(marked)+typeset,再退纯文本
      if (RC.md) { el.innerHTML = RC.md(text || ''); if (RC.typeset) RC.typeset(el); return; }
    } catch (e) {}
    el.textContent = text;
  }
  // 粘底滚动（2026-09-14）：只有用户本来就贴着底部（阈值内）才自动滚到底；
  // 一旦向上滚开去读，流式增量就不再把视图往下拽——否则每条 delta 都 scrollTop=scrollHeight，界面不断抽搐。
  var STICKY = 120;
  function _scroll() { if (_nativePresentation()) return; try { var t = _thread(); if (!t) return; if (t.scrollHeight - t.scrollTop - t.clientHeight <= STICKY) t.scrollTop = t.scrollHeight; } catch (e) {} }

  // ── 容器 ─────────────────────────────────────────────────────────────────
  function open(tid, target, options) {
    var th = target || _thread();
    if (!th) return null;
    if (!target) { var live = _lookup(tid); if (live && live.el.isConnected) { _cur = live.tid; return live; } }
    options = options && typeof options === 'object' ? options : {};
    // A history request may finish while this same live row is still changing.
    // Its staging projection must not replace the active row in the registry.
    if (target && _turns[tid] && _turns[tid]._live && _turns[tid].el.parentNode !== target) tid = 'replay:' + tid;
    if (
      _turns[tid] && _turns[tid].el && _turns[tid].el.isConnected &&
      (!target || _turns[tid].el.parentNode === target)
    ) {
      if (!target) _cur = tid;
      return _turns[tid];
    }
    var el = document.createElement('div');
    el.className = 'asst-msg asst-a rc-turn';   // 复用既有气泡外观;有工具时再补 .vc-if 卡头(见 _ensureHead)
    el.setAttribute('data-turn', tid);
    try { var _mt = options.meta && options.meta.turnId; if (_mt) el.setAttribute('data-turn-id', String(_mt)); } catch (e) {}   // 历史回放的容器记住真实 turn id：重载前后按它对上（展开态/滚动）
    var bd = document.createElement('div'); bd.className = 'rc-turn-bd';
    var flow = document.createElement('div'); flow.className = 'rc-turn-flow'; flow.hidden = true;
    el.appendChild(bd); el.appendChild(flow);
    th.appendChild(el); window.__bwNativeMessages?.publish(el,th);
    var t = _turns[tid] = { tid: tid, el: el, hd: null, bd: bd, flow: flow,
      parts: [], draft: null, orchTaskId: null,
      meta: (options.meta && typeof options.meta === 'object') ? options.meta : null,   // 历史来源(via/threadId/turnId):语音轮次的「保存为工具」要靠它送通知
      historyReplay: options.historyReplay === true };   // tid 也用于存量卡稳定补号
    if (!target) { _cur = tid; _scroll(); }
    return t;
  }

  // 工具出现时才长出卡头(带【流程】按钮)。没有工具的轮次就是一条普通气泡 —— 容器**自适应**,
  // 而不是像旧代码那样把气泡"升格"成另一个 DOM(那会把还在流的文字搬来搬去 → 显示两遍)。
  function _ensureHead(t, label) {
    if (label) t.presentationTitle = label;
    if (_nativePresentation()) return null;
    if (t.hd) {
      var s0 = t.hd.querySelector(':scope > span');
      if (s0 && label) { s0.textContent = label; s0.title = label; }
      return t.hd;
    }
    // ⚠ 样式必须先在:rc-toolchip 的 mountFlow 就是这么做的(它注释写着「样式必须在,否则裸 <button> = 白块」),
    //   我上一版漏了这一步 → 【流程】按钮退化成 Safari 原生 push-button(白方块,用户实测)。
    try { if (RC.voiceCard && RC.voiceCard.css) RC.voiceCard.css(); } catch (e) {}
    t.el.classList.add('vc-if');   // 复用既有结果卡外观(见 _infoCardEl)
    var hd = document.createElement('div'); hd.className = 'vc-if-hd';
    _progressCss();
    var sp = document.createElement('span'); sp.className = 'rc-flow-title'; sp.textContent = label || '工具调用'; sp.title = sp.textContent;
    hd.appendChild(sp);
    // 141:【流程】按钮**不再自己 new** —— 调 rc-toolchip 的唯一创建点(那条路一直渲染正常;
    //   我自己拼的那个一模一样的 <button> 却是白圆块)。跑同一段代码,差异就无处藏身。
    var b = RC.toolChip.flowBtn(function () {
      t.flow.hidden = !t.flow.hidden;
      if (!t.flow.hidden) _paintFlow(t);
      return !t.flow.hidden;
    });
    t._flowBtn = b;
    hd.appendChild(b);
    t.el.insertBefore(hd, t.bd);
    t.hd = hd;
    return hd;
  }

  // 141:ICON_FLOW 已删 —— 图标随 rc-toolchip 的 flowBtn 一起来(单一来源)。

  function _localCardGid(seed) {
    // 旧历史可能没有服务端签发的 gid；用轮次+序号生成稳定本地身份，避免每次
    // 回放都复制一批新卡。四个独立 FNV-1a 段只用于身份，不作为安全摘要。
    seed = String(seed || 'reader-card');
    var hex = '';
    for (var lane = 0; lane < 4; lane++) {
      var h = (2166136261 ^ (lane * 2654435761)) >>> 0;
      for (var i = 0; i < seed.length; i++) {
        h ^= seed.charCodeAt(i);
        h = Math.imul(h, 16777619) >>> 0;
      }
      hex += h.toString(16).padStart(8, '0');
    }
    return 'card_' + hex;
  }

  // ── ★ 唯一渲染器:实时与历史回放都走这里(不变式①)──────────────────────
  function renderPart(t, p) {
    // The native conversation consumes structured text/tool state. Building
    // Markdown, math, images and status controls in a hidden duplicate view
    // would still execute their renderer and resource requests.
    if (_nativePresentation() && (p.kind === 'text' || p.kind === 'tool' || p.kind === 'meta' || p.kind === 'hlcard')) {
      if (p.kind === 'tool') _ensureHead(t, p.label || p.tool || '工具');
      return null;
    }
    var d = document.createElement('div');
    d.className = 'rc-part rc-part-' + (p.kind || 'text');
    if (p.kind === 'text') {
      if (!(p.text || '').trim()) return null;
      if (p.role === 'user') d.className += ' rc-part-user';
      _md(d, p.text);
    } else if (p.kind === 'card') {
      // 结果卡(天气/搜索/配图/视频):**复用既有** _infoCardEl —— 别另造一套
      var ce = null;
      if (p.card && !p.card.cid) p.card.cid = 'tc_' + String(t.tid || 'turn') + '_' + String(p.seq == null ? t.parts.length : p.seq);   // 存量 part 以轮次+序号稳定补号
      try { ce = window.__vcInfoCardEl && window.__vcInfoCardEl(p.card || {}); } catch (e) {}
      if (!ce) return null;
      ce.classList.remove('asst-msg', 'asst-a');   // 它现在是容器内的一块,不是独立气泡
      ce.classList.add('rc-part-cardin');
      d.appendChild(ce);
    } else if (p.kind === 'hlcard') {
      // 高亮写操作卡(用户设计:旧「已高亮N处」独立气泡融入 turn 卡系统):
      //   头行可点展开 → 逐条[色块|原文|↗跳转|↩撤销⇄↪重做];状态改动 mutate part + onChange 落库 → 刷新回放仍可操作。
      d.appendChild(_hlCardEl(t, p));
    } else if (p.kind === 'tool') {
      _ensureHead(t, p.label || p.tool || '工具');
      return null;   // 工具本身不在正文里占块 —— 它的细节收在【流程】按钮里(用户设计)
    } else if (p.kind === 'cards') {
      // 制卡结果(语音/助手后台任务完成 → result.cards)。统一壳(用户拍板 2026-07-21):套进 vc-card(renderInflow)
      //   → 与浮层/侧栏其它卡同长相+三态圆点;正文=rc-flashcard(draft→mountDrafts 编辑/入库,非 draft→mountPreview
      //   只读;gid 联动 + entity 状态恢复不变,由 mount 回调进 bd)。原裸挂 rc-part 退役。
      try {
        if (!(window.RC && RC.flashcard)) return null;
        var _fcards = p.cards || []; if (!_fcards.length) return null;
        var _cardSeq = p.seq == null ? t.parts.length : p.seq;
        if (!/^card_[a-f0-9]{4,64}$/.test(String(p.gid || ''))) {
          p.gid = _localCardGid(String(t.tid || 'turn') + ':' + String(_cardSeq));
        }
        if (p.draft && RC.flashcard.presentDraft && !t.historyReplay) {
          // 上游若已经把这张草稿登记进本地仓，就必须原样沿用它的身份。
          //
          // 这里曾一律自造 assistant-turn:<tid>:<seq> 作为 source 并把
          // entityRegistered 写死为 false —— 于是浮层里那张卡和这里这张
          // 在仓库中成了两个实体，同一份草稿被登记两次，编辑与确认各走各的。
          // 双宿主的前提是"同一个实体的两个视图"，不是"两份拷贝"。
          var _upstreamSource = p.repositorySource || null;
          var _turnCardSourceId = 'assistant-turn:' +
            String(t.tid || 'turn') + ':' + String(_cardSeq);
          RC.flashcard.presentDraft(_fcards, p.gid, {
            host: d,
            // entityRegistered 是旧 Pi registry 的兼容标志，不代表本地已登记；
            // 原样沿用上游给的值，缺省为 false。
            entityRegistered: _upstreamSource ? !!p.entityRegistered : false,
            localDraft: _upstreamSource ? (p.localDraft || null) : null,
            repositorySource: _upstreamSource || {
              kind: 'assistant-turn',
              sourceId: _turnCardSourceId,
              draftId: _turnCardSourceId,
              tool: 'result.present'
            }
          }).then(function (result) {
            if (!result) d.textContent = '卡片草稿未能写入 Reader 本地卡库';
          }).catch(function (error) {
            d.textContent = '卡片草稿本地保存失败：' +
              String(error && (error.code || error.message) || error || '?').slice(0, 160);
          });
        } else if (RC.flashcard.renderEntity) {
          RC.flashcard.renderEntity(d, {
            label: '🎴 制卡' + (_fcards.length > 1 ? ' × ' + _fcards.length : ''),
            cards: _fcards,
            gid: p.gid,
            mode: p.draft ? 'draft' : 'preview',
            className: 'fc-tool-result'
          });
        } else if (p.draft && RC.flashcard.mountDrafts) {
          RC.flashcard.mountDrafts(d, _fcards, { gid: p.gid, bare: true, nopin: true });
        } else if (RC.flashcard.mountPreview) {
          RC.flashcard.mountPreview(d, _fcards, { gid: p.gid, bare: true, nopin: true });
        }
      } catch (e) { return null; }
    } else if (p.kind === 'meta') {
      return null;   // 设置项只落库,不显示(点【流程】能看到)
    } else {
      return null;
    }
    t.bd.appendChild(d);
    if (!t.historyReplay) _scroll();
    return d;
  }

  // ── 高亮写操作卡(kind:'hlcard';实时与回放同走 renderPart → 本渲染器)──────────
  var _hlCssDone = false;
  function _hlCss() {
    if (_hlCssDone || document.getElementById('rc-hlcard-css')) { _hlCssDone = true; return; }
    _hlCssDone = true;
    var s = document.createElement('style'); s.id = 'rc-hlcard-css';
    s.textContent =
      '.rc-hlcard{background:rgba(26,36,58,.55);border:1px solid rgba(91,118,184,.35);border-radius:10px;padding:8px 10px;font-size:13px}' +
      '.rc-hlcard-h{display:flex;align-items:center;gap:8px;cursor:pointer;-webkit-user-select:none;user-select:none}' +
      '.rc-hlcard-h .tt{flex:1;min-width:0}.rc-hlcard-h .ar{flex:0 0 auto;opacity:.7;font-size:11px;white-space:nowrap}' +
      '.rc-hlcard-bd{display:none;margin-top:6px;border-top:1px solid rgba(91,118,184,.2);padding-top:6px}' +
      '.rc-hlcard.open .rc-hlcard-bd{display:block}' +
      // ⚠ 作用域必须带 .rc-hlcard:这些 class 名与共享高亮编辑弹层(rc-highlight.js 的
      //   .rc-hl-pop .rc-hl-sw/.rc-hl-tx/.rc-hl-row)**同名**。裸选择器会命中弹层内部件——
      //   实测 .rc-hl-sw{width:10px;height:10px} 把弹层色板行(应 358×28)压成 10×10,圆点
      //   flex-wrap 竖排溢出 178px,砸在 textarea 与按钮行上 = 用户报的「弹窗元素重叠」。
      '.rc-hlcard .rc-hl-row{display:flex;align-items:center;gap:7px;padding:3px 0}' +
      '.rc-hlcard .rc-hl-sw{flex:0 0 auto;width:10px;height:10px;border-radius:3px}' +
      '.rc-hlcard .rc-hl-tx{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
      '.rc-hlcard .rc-hl-row.undone .rc-hl-tx{text-decoration:line-through;opacity:.55}' +
      '.rc-hl-b{flex:0 0 auto;-webkit-appearance:none;appearance:none;background:transparent;border:1px solid rgba(120,150,210,.45);color:#a8c4f0;border-radius:7px;padding:2px 8px;font-size:12px;cursor:pointer;touch-action:manipulation}' +
      '.rc-hl-b:disabled{opacity:.5}' +
      // 操作条(带 op 的条目):一行一条,不折叠
      '.rc-opcard{padding:4px 10px}.rc-opbar{display:flex;align-items:center;gap:7px;padding:4px 0}' +
      '.rc-opbar .tt{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--rc-success)}' +
      '.rc-opbar.undone .tt{text-decoration:line-through;opacity:.55}' +
      '.rc-opbar .pg{flex:0 0 auto;font-size:11px;opacity:.75;white-space:nowrap}';
    document.head.appendChild(s);
  }
  // 高亮单条撤销⇄重做(行内按钮与顶部「操作」tab 共用)。成功 resolve(true),失败 resolve(false) 且已 toast。
  function _hlToggle(file, it) {
    if (!it.undone) {   // 撤销 = 删这条高亮
      return RC.reqJson('DELETE', '/pdf/api/highlights?file=' + encodeURIComponent(file) + '&id=' + encodeURIComponent(it.id || ''), null)
        .then(function (d) {
          if (!(d && d.ok)) {
            var msg = String((d && (d.error || d.message)) || '');
            if (/未找到|not found|不存在/i.test(msg)) {   // 用户已手动删掉:这条撤销条没意义了,摘掉(2026-09-15)
              it.gone = true; it.undone = true;
              try { RC.toast('这条高亮已经不在了，已从记录里移除'); } catch (e) {}
              return 'gone';
            }
            try { RC.toast('撤销失败:' + (msg || '?')); } catch (e) {}
            return false;
          }
          it.undone = true; return true;
        }).catch(function () { try { RC.toast('网络错误'); } catch (e) {} return false; });
    }
    if (!(it.rects && it.rects.length)) { try { RC.toast('这条没有几何信息,无法重建'); } catch (e) {} return Promise.resolve(false); }
    return RC.reqJson('POST', '/pdf/api/highlights', { file: file, page: it.pdf_page, rects: it.rects, color: it.color, text: it.text || '' })
      .then(function (d) {
        if (!(d && d.ok)) { try { RC.toast('重做失败:' + ((d && d.error) || '?')); } catch (e) {} return false; }
        if (d.id) it.id = d.id;
        it.undone = false; return true;
      }).catch(function () { try { RC.toast('网络错误'); } catch (e) {} return false; });
  }
  // 任一操作条目的撤销⇄重做:高亮在本文件做;卡片改删/便签/自建页交给 rc-assistant 登记的 opAction(它有各自的 API 与撤销语义)
  function _opToggle(file, it) {
    if (!it.op) return _hlToggle(file, it);
    var fn = RC.assistant && RC.assistant.opAction;
    if (typeof fn !== 'function') { try { RC.toast('当前阅读器尚未准备好撤销'); } catch (e) {} return Promise.resolve(false); }
    var action = it.undone ? 'redo' : 'undo';
    return Promise.resolve(fn(it, file, action)).then(function (ok) { return !!ok; }).catch(function () { return false; });
  }
  function _opLabel(it) {
    if (!it.op) return '✏️ 高亮：' + (it.text || '(无文字)');
    if (it.op === 'page-card') return (it.act === 'delete' ? '🗑 已删除' : '✏️ 已修改') + (it.number ? ('第 ' + it.number + ' 个') : '自由') + '卡片';
    if (it.op === 'note') return '🗒 ' + (it.act === 'edit' ? '已修改便签' : '已创建便签');
    if (it.op === 'userpage') return (it.act === 'delete' ? '🗑 已删除自建页' : '✏️ 已改写自建页') + (it.title ? ('「' + it.title + '」') : '');
    return '✏️ ' + (it.label || it.op);
  }
  function _opPage(it) {   // 跳转用 pdf 页;显示用 disp 页
    var pdf = it.pdf_page != null ? it.pdf_page : it.page;
    var disp = it.disp_page != null ? it.disp_page : pdf;
    return { pdf: pdf, disp: disp };
  }
  var _opsListeners = [];
  function _opsChanged() { _opsListeners.forEach(function (fn) { try { fn(); } catch (e) {} }); }
  // 目标已不存在(用户手动删了):把条目从卡里摘掉;卡空了就把整个 part 连同 DOM 移除;落库 + 「操作」tab 同步
  //   ⚠ 条目只标 gone 不真删:items 空了契约校验会拒收、_syncParts 也跳过空 parts → 刷新后又回来。渲染时过滤 gone。
  function _dropItem(t, p, it) {
    try {
      it.gone = true;
      _rerenderPart(t, p);
      try { var tid = t.el.getAttribute('data-turn'); if (tid && RC.turnCard.onChange) RC.turnCard.onChange(tid); } catch (e) {}
      try { if (window._reloadHighlights) window._reloadHighlights(); } catch (e) {}
    } catch (e) {}
    _opsChanged();
  }
  // 就地重画一个 part(操作态变了:标题/按钮文字/删除线一起换新,折叠态保留)
  function _rerenderPart(t, p) {
    if (_nativePresentation()) return;
    try {
      if (!(p._el && p._el.isConnected)) return;
      var open0 = !!p._el.querySelector('.rc-hlcard.open');
      var nd = document.createElement('div'); nd.className = p._el.className;
      nd.appendChild(_hlCardEl(t, p));
      if (open0) { var c0 = nd.querySelector('.rc-hlcard'); if (c0) c0.classList.add('open'); }
      p._el.replaceWith(nd); p._el = nd;
    } catch (e) {}
  }
  function _opBarEl(t, p, it) {
    var row = document.createElement('div'); row.className = 'rc-opbar' + (it.undone ? ' undone' : '');
    var tt = document.createElement('span'); tt.className = 'tt'; tt.textContent = (it.undone ? '↩ 已撤销：' : '') + _opLabel(it); tt.title = tt.textContent;
    row.appendChild(tt);
    var pg = _opPage(it);
    if (pg.pdf) {
      var jb = document.createElement('button'); jb.type = 'button'; jb.className = 'rc-hl-b'; jb.textContent = '↗ 第' + pg.disp + '页';
      jb.addEventListener('click', function (ev) { ev.stopPropagation(); try { if (window.jumpWithBack) window.jumpWithBack(pg.pdf); } catch (e) {} });
      row.appendChild(jb);
    }
    var ub = document.createElement('button'); ub.type = 'button'; ub.className = 'rc-hl-b'; ub.textContent = it.undone ? '↪ 重做' : '↩ 撤销';
    ub.addEventListener('click', function (ev) {
      ev.stopPropagation();
      if (ub.disabled) return; ub.disabled = true; ub.textContent = it.undone ? '重做中…' : '撤销中…';
      _opToggle(p.file || '', it).then(function (ok) {
        ub.disabled = false;
        if (ok === 'gone') { _dropItem(t, p, it); return; }
        if (!ok) { ub.textContent = it.undone ? '↪ 重做' : '↩ 撤销'; return; }
        try { var tid = t.el.getAttribute('data-turn'); if (tid && RC.turnCard.onChange) RC.turnCard.onChange(tid); } catch (e) {}
        _rerenderPart(t, p); _opsChanged();
      });
    });
    row.appendChild(ub);
    return row;
  }
  function _hlCardEl(t, p) {
    _hlCss();
    var items = (p.items || []).filter(function (x) { return x && !x.gone; }), file = p.file || '';
    var box = document.createElement('div'); box.className = 'rc-hlcard';
    if (!items.length) { box.hidden = true; box.style.display = 'none'; return box; }   // 条目全被用户手动删掉了:这张卡没内容可管
    if (items.length && items.every(function (x) { return x && x.op; })) {   // 操作条(卡片改删/便签/自建页):扁平,不折叠
      box.classList.add('rc-opcard');
      items.forEach(function (it) { box.appendChild(_opBarEl(t, p, it)); });
      return box;
    }
    var pages = [], seen = {};
    items.forEach(function (it) { var dp = (it.disp_page != null) ? it.disp_page : it.pdf_page; if (dp != null && !seen[dp]) { seen[dp] = 1; pages.push(dp); } });
    var hd = document.createElement('div'); hd.className = 'rc-hlcard-h';
    var tt = document.createElement('span'); tt.className = 'tt';
    var ar = document.createElement('span'); ar.className = 'ar'; ar.textContent = '▸ 点开逐条管理';
    function live() { var n = 0; items.forEach(function (x) { if (!x.undone) n++; }); return n; }
    function title() { tt.textContent = '✏️ 已高亮 ' + live() + '/' + items.length + ' 处' + (pages.length ? '（第 ' + pages.join('、') + ' 页）' : ''); }
    title();
    hd.appendChild(tt); hd.appendChild(ar); box.appendChild(hd);
    var bd = document.createElement('div'); bd.className = 'rc-hlcard-bd'; box.appendChild(bd);
    hd.addEventListener('click', function () {
      box.classList.toggle('open');
      ar.textContent = box.classList.contains('open') ? '▾ 收起' : '▸ 点开逐条管理';
    });
    function sync() {   // 状态改动:标题刷新 + part 落库(刷新回放仍是最新态) + 页面高亮重渲 + 「操作」tab 刷新
      title();
      try { var tid = t.el.getAttribute('data-turn'); if (tid && RC.turnCard.onChange) RC.turnCard.onChange(tid); } catch (e) {}
      try { if (window._reloadHighlights) window._reloadHighlights(); } catch (e) {}
      _opsChanged();
    }
    items.forEach(function (it) {
      if (it.op) { bd.appendChild(_opBarEl(t, p, it)); return; }   // 混合卡(旧数据兜底):带 op 的画成操作条
      var row = document.createElement('div'); row.className = 'rc-hl-row' + (it.undone ? ' undone' : '');
      var sw = document.createElement('span'); sw.className = 'rc-hl-sw'; sw.style.background = it.color || '#fff59d';
      var tx = document.createElement('span'); tx.className = 'rc-hl-tx'; tx.textContent = it.text || '(无文字)'; tx.title = it.text || '';
      var jb = document.createElement('button'); jb.type = 'button'; jb.className = 'rc-hl-b'; jb.textContent = '↗';
      jb.title = '跳转到第 ' + ((it.disp_page != null) ? it.disp_page : it.pdf_page) + ' 页';
      jb.addEventListener('click', function (ev) { ev.stopPropagation(); try { if (window.jumpWithBack) window.jumpWithBack(it.pdf_page); } catch (e) {} });
      var ub = document.createElement('button'); ub.type = 'button'; ub.className = 'rc-hl-b'; ub.textContent = it.undone ? '↪ 重做' : '↩ 撤销';
      ub.addEventListener('click', function (ev) {
        ev.stopPropagation();
        if (ub.disabled) return; ub.disabled = true;
        _hlToggle(file, it).then(function (ok) {
          ub.disabled = false;
          if (ok === 'gone') { _dropItem(t, p, it); return; }
          if (!ok) return;
          row.classList.toggle('undone', !!it.undone); ub.textContent = it.undone ? '↪ 重做' : '↩ 撤销'; sync();
        });
      });
      row.appendChild(sw); row.appendChild(tx); row.appendChild(jb); row.appendChild(ub);
      bd.appendChild(row);
    });
    return box;
  }
  // ── 顶部「操作」tab 的数据源:所有轮次里的 hlcard 条目,按 DOM 顺序(旧→新),取最近 limit 条 ──
  function opItems(limit) {
    var turns = Object.keys(_turns).map(function (k) { return _turns[k]; }).filter(function (t) { return t && t.el && t.el.isConnected; });
    turns.sort(function (x, y) { return (x.el.compareDocumentPosition(y.el) & 4) ? -1 : 1; });   // 4 = DOCUMENT_POSITION_FOLLOWING
    var out = [];
    turns.forEach(function (t) {
      t.parts.forEach(function (p) {
        if (p.kind !== 'hlcard') return;
        (p.items || []).forEach(function (it) { if (it && !it.gone) out.push({ tid: t.tid, file: p.file || '', item: it, part: p, turn: t }); });
      });
    });
    var n = limit > 0 ? limit : 30;
    return out.length > n ? out.slice(out.length - n) : out;
  }
  function opAction(entry) {   // 顶部 tab 的按钮:执行 → 落库 → 轮内那张卡就地重画 → 通知
    if (!entry || !entry.item) return Promise.resolve(false);
    return _opToggle(entry.file, entry.item).then(function (ok) {
      if (ok === 'gone') { if (entry.turn && entry.part) _dropItem(entry.turn, entry.part, entry.item); else _opsChanged(); return true; }
      if (!ok) return false;
      try { if (RC.turnCard.onChange) RC.turnCard.onChange(entry.tid); } catch (e) {}
      try { if (!entry.item.op && window._reloadHighlights) window._reloadHighlights(); } catch (e) {}
      if (entry.turn && entry.part) _rerenderPart(entry.turn, entry.part);
      _opsChanged();
      return true;
    });
  }
  var nativeOperationBusy = new Set();
  async function performOperation(input) {
    var t = _lookup(input && input.tid);
    var p = t && t.parts.find(function (part) { return part.kind === 'hlcard' && part._nativeID === input.partID; });
    var index = input && input.index;
    var item = p && Number.isSafeInteger(index) && index >= 0 && (p.items || [])[index];
    if (!item || item.gone || String(item.id || '') !== input.expectedID || !!item.undone !== input.expectedUndone) throw new Error('操作记录已变化，请重新选择');
    if (input.action === 'jump') {
      var page = _opPage(item).pdf;
      if (!page || typeof window.jumpWithBack !== 'function') throw new Error('这条记录没有可跳转的位置');
      await window.jumpWithBack(page); return {ok:true};
    }
    if (input.action !== 'toggle') throw new Error('操作记录指令无效');
    var key = t.tid + ':' + p._nativeID + ':' + index;
    if (nativeOperationBusy.has(key)) throw new Error('此操作正在保存');
    nativeOperationBusy.add(key);
    try {
      if (!await opAction({tid:t.tid,file:p.file || '',item:item,part:p,turn:t})) throw new Error('操作未成功，已保留记录');
      await RC.turnCard.settle?.();
      return {ok:true};
    } finally { nativeOperationBusy.delete(key); }
  }
  // 对话流里的轮次在原生 TurnStore（迁出 P2 起网页不再回放历史），performOperation 按网页轮次表
  // 找记录就会落空（「操作记录已变化」）。这个入口只做执行：记录由原生从 TurnStore 交来（副本），
  // 执行后的状态回传，原生写回轮次并落库。
  async function performOperationItem(input) {
    var item = input && input.item && typeof input.item === 'object' && !Array.isArray(input.item)
      ? JSON.parse(JSON.stringify(input.item)) : null;
    if (!item) throw new Error('操作记录无效');
    if (input.action === 'jump') {
      var page = _opPage(item).pdf;
      if (!page || typeof window.jumpWithBack !== 'function') throw new Error('这条记录没有可跳转的位置');
      await window.jumpWithBack(page); return {ok:true};
    }
    if (input.action !== 'toggle') throw new Error('操作记录指令无效');
    var result = await _opToggle(String(input.file || ''), item);
    if (result === 'gone') { try { if (window._reloadHighlights) window._reloadHighlights(); } catch (e) {} _opsChanged(); return {ok:true, gone:true}; }
    if (!result) throw new Error('操作未成功，已保留记录');
    try { if (!item.op && window._reloadHighlights) window._reloadHighlights(); } catch (e) {}
    _opsChanged();
    return {ok:true, gone:false, undone:!!item.undone, id:item.id == null ? null : item.id,
      note:item.note && typeof item.note === 'object' ? item.note : null};
  }
  function markOp(pred, undone) {   // 别处(如卡片操作的小提示条)改了状态 → 同步条目并重画
    var hit = false;
    opItems(0).forEach(function (e) {
      try { if (pred(e.item)) { e.item.undone = !!undone; hit = true; _rerenderPart(e.turn, e.part); try { if (RC.turnCard.onChange) RC.turnCard.onChange(e.tid); } catch (x) {} } } catch (x) {}
    });
    if (hit) _opsChanged();
    return hit;
  }

  // 流程面板:把本轮所有 tool part(+meta)画成 AI 请求 → 工具 → 结果 的线性流程
  function _paintFlow(t) {
    if (_nativePresentation()) return;
    var f = t.flow;
    f.innerHTML = '';
    var tools = t.parts.filter(function (p) { return p.kind === 'tool'; });
    var meta = t.parts.filter(function (p) { return p.kind === 'meta'; })[0];
    if (meta) {
      // 感叹号并入流程(用户设计):编排模型·总耗时·tok·tier·时刻 + ⚙ 直达编排设置,全在这一行。
      var m = document.createElement('div'); m.className = 'rc-flow-meta';
      var bits = ['模型 ' + (meta.model || '—')];
      if (meta.effort) bits.push('深度 ' + meta.effort);
      if (meta.voice_mode) bits.push('档位 ' + meta.voice_mode);
      var _tsec = meta.total_sec || meta.sec;
      if (_tsec) bits.push('共 ' + (Math.round(_tsec * 10) / 10) + 's');
      if (meta.tok) bits.push((meta.tok >= 1000 ? (meta.tok / 1000).toFixed(1).replace(/\.0$/, '') + 'k' : meta.tok) + ' tok');
      if (meta.tier) bits.push(String(meta.tier));
      if (meta.ts) { try { var _d0 = new Date(meta.ts * 1000); bits.push('🕐 ' + _d0.getHours() + ':' + ('0' + _d0.getMinutes()).slice(-2)); } catch (e) {} }
      m.textContent = bits.filter(Boolean).join(' · ') + '  ';
      var gear = document.createElement('button'); gear.type = 'button'; gear.className = 'rc-hl-b'; gear.textContent = '⚙';
      gear.title = '编排 AI 设置(改动直接写当前预设)';
      gear.addEventListener('click', function (ev) {
        ev.stopPropagation();
        try { if (RC.assistant && RC.assistant.openModelSettings) RC.assistant.openModelSettings(meta.action || 'orchestrator'); } catch (e) {}
      });
      _hlCss();   // rc-hl-b 按钮样式来自 hlcard 的 CSS 块,先确保注入
      m.appendChild(gear);
      f.appendChild(m);
    }
    if (!tools.length) { f.appendChild(document.createTextNode('(本轮没有工具调用)')); return; }
    // ★用户设计 #3:每个工具都以「工具长条」显示、可长按进各种设置 —— **复用 rc-toolchip 的 paintFlow**
    //   (.vc-fn 长条 + 长按 openDetail),别再自己画纯文本步骤(那正是"擅自设计"的又一处)。
    var main = tools[tools.length - 1] || {};
    // CLI 卡判据 = **该 part 自带内部 steps**(不靠 t.taskId)。t.taskId 只活在内存,刷新回放后就没了 →
    //   旧判据 `!!t.taskId && …` 在回放时恒 false,于是把整条 cliPart 当**一条** do_task 长条渲染
    //   (而它只有 steps、没有 args/result)→ 流程塌成一条空条 = 用户实测"刷新后流程变了"。
    var isCli = !!(main.steps && main.steps.length);
    var taskId = t.orchTaskId || t.taskId || main.task_id || '';   // 阶段2:主编排轮次优先取 orchTaskId(服务端 vtask,含 steps);回放后 CLI 从持久化 part 取 task_id → 刷新后仍能「保存为工具」
    var steps;
    if (isCli) {
      steps = main.steps.map(function (s) { return { label: s.label || String(s), detail: s.detail || '', tool: s.tool || s.label || '' }; });   // tool=该 CLI 内部工具名 → 长按弹它自己的设置
    } else {
      steps = tools.map(function (p) {
        var a = ''; try { a = (p.args && Object.keys(p.args).length) ? JSON.stringify(p.args) : ''; } catch (e) {}
        var det = a;
        if (p.result) det = (a ? a + '\n\n' : '') + String(p.result).slice(0, 6000);   // 放宽:感叹号 detail 全量语义并入流程
        // 耗时:本机路径带 took_s;外部写入(Windows 语音同步)按契约只带 ms —— 两个都认(用户 2026-09-13:看不到耗时不利于调试)
        var _sec = (p.took_s != null) ? p.took_s : ((p.ms != null && isFinite(p.ms)) ? p.ms / 1000 : undefined);
        return { label: p.label || p.tool || '工具', detail: det, sec: _sec, model: p.model, tool: p.tool || '' };
      });
    }
    // ★#44 框选保存:CLI 轨迹默认全选;取消选中的工具不打包进新工具。t._sel=选中的 step 下标集合。
    if (isCli && !t._sel) { t._sel = {}; for (var _i = 0; _i < steps.length; _i++) t._sel[_i] = 1; }

    var cliSteps = tools.reduce(function (a, p) { return a + ((p.steps && p.steps.length) || 0); }, 0);
    var isVoice = !!(t.meta && t.meta.via === 'codex-voice' && t.meta.threadId && t.meta.turnId);   // Windows 语音轮次:保存=让 Codex 整理成 skill
    var canSave = tools.length >= 2 || (taskId && cliSteps >= 2) || (isVoice && tools.length >= 1);
    if (canSave) {
      var sv = document.createElement('div'); sv.className = 'rc-flow-save';
      var sb = document.createElement('span');
      sb.className = 'up2-b-btn'; sb.setAttribute('role', 'button'); sb.setAttribute('tabindex', '0');
      sb.textContent = '💾 保存为工具';
      sb.style.cssText = 'display:inline-flex;padding:6px 14px;border-radius:9px;background:#3b6fd4;color:#fff;font-size:12px;cursor:pointer;-webkit-appearance:none;appearance:none';
      if (isCli) { var _hint = document.createElement('div'); _hint.className = 'rc-flow-selhint'; _hint.textContent = '↓ 点工具前的圆点取消选中,只把选中的打包成工具'; sv.appendChild(_hint); }
      sb.addEventListener('click', function (ev) {
        ev.stopPropagation();
        if (isVoice) {
          // 2026-09-13 用户拍板:存不存只由用户决定,AI 不判断;名字可以留空让它起。
          //   这里不存任何东西 —— 把整理请求经通知通道送进那条 Codex 线程,由它按 organize-into-skill
          //   取真实轨迹、编 flow.json、校验+干跑、存 skill。口头说「存成工具」走同一个 skill。
          var vnm = prompt('给这个工具起个名字(留空让 AI 起):', '') || '';
          // @interaction assistant.voiceturn.organize
          try { RC.reqJson('POST', '/api/assistant/voice-turn-organize', { turn_id: t.meta.turnId, thread_id: t.meta.threadId, name: vnm }).then(function (r) {
            alert(r && r.ok ? '已交给电脑端整理,做完它会在对话里回一句' : '没送到:' + ((r && r.error) || '?'));
          }).catch(function () { alert('没送到(网络)'); }); } catch (e) {}
          return;
        }
        var nm = prompt('给这个工具起个名字(下次说名字就能直接用):');
        if (!nm) return;
        var body = { name: nm };
        if (taskId) {
          body.task_id = taskId;   // ★ CLI 任务:保存**执行轨迹**(自包含回放,不问数据源)
          if (t._sel) {   // #44:只保存选中的工具
            var sel = []; for (var k = 0; k < steps.length; k++) if (t._sel[k]) sel.push(k);
            if (!sel.length) { alert('至少选一个工具'); return; }
            if (sel.length < steps.length) body.select = sel;
          }
        } else {
          // 内置多工具流程:可挂一个上游工具当去壳数据源(用户可留空)
          var src = prompt('这个任务的内容来自哪个上游工具?(留空=不记数据源,直接固化)\n例:高亮 / 未掌握词', '') || '';
          var srcTool = tools.length ? tools[0].tool : '';
          if (src && srcTool) { body.source_label = src; body.source_spec = { call: srcTool, extract: 'text' }; }
        }
        try { RC.reqJson('POST', '/pdf/api/run-save', body).then(function (r) {
          alert((r && r.hint) || (r && r.ok ? '已保存' : '保存失败:' + ((r && r.error) || '?')));
        }).catch(function () { alert('保存失败(网络)'); }); } catch (e) {}
      });
      sv.appendChild(sb); f.appendChild(sv);
    }
    var vision = [];
    tools.forEach(function (p) { (p.vision || []).forEach(function (v) { vision.push(v); }); });
    var ans = t.parts.filter(function (p) { return p.kind === 'text' && (p.text || '').trim(); })
                     .map(function (p) { return p.text; }).join('\n\n');
    var titleTxt = (t.hd && t.hd.querySelector('span')) ? t.hd.querySelector('span').textContent
                                                        : (main.label || main.tool || '任务');
    var chip = { type: 'text', label: titleTxt, tool: (isCli ? 'do_task' : (main.tool || '')),
                 steps: steps, meta: [], vision: vision,
                 failed: tools.some(function (p) { return !!p.error; }),
                 detail: ans, summary: '' };
    var box = document.createElement('div'); f.appendChild(box);
    if (RC.toolChip && RC.toolChip.renderFlowInto) {
      RC.toolChip.renderFlowInto(box, chip);
      if (isCli && canSave) _wireFlowSelect(box, steps.length, t._sel);   // #44:给工具长条挂选中圆点
    } else { box.textContent = steps.map(function (s) { return '· ' + s.label; }).join('\n'); }   // 兜底
  }

  // #44:给流程里**每个工具长条**(.vc-fn)前面加一个可点圆点(实心=选中/空心=排除,默认全选)。
  //   stages() 的节点顺序 = [AI请求(0), 工具1(1)…工具N(N), 结果(N+1)];工具节点 data-i = 1..N。
  function _wireFlowSelect(box, nSteps, sel) {
    var nodes = box.querySelectorAll('.vc-fn');
    nodes.forEach(function (nd) {
      var di = parseInt(nd.getAttribute('data-i'), 10);
      if (!(di >= 1 && di <= nSteps)) return;   // 只给工具节点加,跳过 AI请求/结果
      var si = di - 1;
      var dot = document.createElement('span'); dot.className = 'rc-sel-dot';
      if (sel[si]) dot.classList.add('on');
      dot.title = '选中=打包进新工具;点一下排除';
      dot.addEventListener('click', function (ev) {
        ev.stopPropagation(); ev.preventDefault();
        if (sel[si]) { delete sel[si]; dot.classList.remove('on'); nd.classList.add('rc-fn-off'); }
        else { sel[si] = 1; dot.classList.add('on'); nd.classList.remove('rc-fn-off'); }
      });
      nd.insertBefore(dot, nd.firstChild);
      if (!sel[si]) nd.classList.add('rc-fn-off');
    });
  }

  // 同一次工具调用**只留一条**。同一个调用会被两边各画一次：运行器从 app-server 的条目里
  // 记一条（带 args/result/耗时），App 自己执行时也画一条芯片（只有标签）。两条并存就是
  // 用户 2026-09-18 说的「有重复」。按工具名归一：留信息多的那条，后到的更全就**就地升级**。
  // ⚠ 两个入口都要走这里 —— addPart（同容器内先后到达）和 rename 的合并分支（两条原本
  //   在不同容器：App 芯片先落进临时容器，运行器那条在真轮次容器，改名时才碰面）。
  //   只堵一个入口 = 只修一半。
  // 工具部件在正文里不占块（renderPart 对 tool 返回 null，细节收在【流程】里），所以升级
  // 只改字段 + 重画流程面板与卡头，没有 DOM 块要换。
  function _absorbTool(t, part, silent) {
    if (!part || part.kind !== 'tool' || !(part.tool || part.label)) return false;
    var key = String(part.tool || part.label);
    var callId = String(part.call_id || part.callId || part.item_id || part.id || '');
    var rich = function (p) { return (p.result ? 2 : 0) + (p.args ? 1 : 0); };
    for (var j = t.parts.length - 1; j >= 0; j--) {
      var pt = t.parts[j];
      if (pt.kind !== 'tool' || String(pt.tool || pt.label) !== key) continue;
      var priorId = String(pt.call_id || pt.callId || pt.item_id || pt.id || '');
      if (callId && priorId) {
        if (part.origin && pt.origin && part.origin !== pt.origin) continue;
        if (callId !== priorId) continue;
        // The producer's invocation identity, not its tool name or content, owns
        // running -> completed updates and replay deduplication.
        var terminal = /^(completed|done|failed|error|cancelled)$/.test(String(pt.status || ''));
        var incomingRunning = /^(running|started|in_progress)$/.test(String(part.status || ''));
        if (!(terminal && incomingRunning)) {
          for (var field in part) { if (field !== 'seq' && field.charAt(0) !== '_') pt[field] = part[field]; }
          _ensureHead(t, pt.label || pt.tool || '工具');
          if (!t.flow.hidden) _paintFlow(t);
        }
        return true;
      }
      // ⚠ 只吞"光有标签"的那条。同一轮里同一个工具**真被调用两次**是正常的
      //   （连做两张卡），两条都带 args/result 时按两次算，否则就是把用户的第二次
      //   操作从流程里抹掉 —— 去重反而变成丢信息。
      if (rich(pt) !== 0 && rich(part) !== 0) continue;
      if (rich(part) > rich(pt)) {
        for (var k in part) { if (k !== 'seq' && k !== '_el') pt[k] = part[k]; }
        try { _ensureHead(t, pt.label || pt.tool || '工具'); } catch (e) {}
        if (!t.flow.hidden) _paintFlow(t);
        // ⚠ 回放时不回写：这一刻正在把存储里的东西画出来，再 onChange 就是拿渲染结果
        //   倒灌回存储，等于让显示层改写权威数据。
        if (!t.historyReplay && !silent) { try { if (RC.turnCard.onChange) RC.turnCard.onChange(t.tid); } catch (e) {} }
      }
      return true;
    }
    return false;
  }

  // ── 注入 ─────────────────────────────────────────────────────────────────
  function addPart(tid, part) {
    var t = _lookup(tid) || open(tid);
    if (!t) return null;
    t._live = true; t._streamVersion = ++_streamVersion;
    // 同一次工具调用**只留一条**。同一个调用会被两边各画一次：运行器从 app-server 的
    // 条目里记一条（带 args/result/耗时），App 自己执行时也画一条芯片（只有标签）。
    // 两条并存 = 用户 2026-09-18 说的「有重复」。这里按工具名归一：留信息多的那条，
    // 后到的若更全就**就地升级**，不新增方块（合并同 ADR 的不变式②：part 只追加不重写，
    // 例外要显式——这是第二个显式例外，和 draft 并列）。
    if (part.kind === 'tool' && _absorbTool(t, part)) return null;
    if (part.kind === 'hlcard') {   // 同轮同书的高亮**合并进一张卡**(AI 调两次 highlight ≠ 两张卡,用户实测)
      for (var _i = t.parts.length - 1; _i >= 0; _i--) {
        var _p0 = t.parts[_i];
        if (_p0.kind === 'hlcard' && _p0.file === part.file) {
          _p0.items = (_p0.items || []).concat(part.items || []);
          try {
            if (_p0._el && _p0._el.isConnected) {
              var _open0 = !!_p0._el.querySelector('.rc-hlcard.open');
              var _nd = document.createElement('div'); _nd.className = _p0._el.className;
              _nd.appendChild(_hlCardEl(t, _p0));
              if (_open0) { var _c0 = _nd.querySelector('.rc-hlcard'); if (_c0) _c0.classList.add('open'); }
              _p0._el.replaceWith(_nd); _p0._el = _nd;
            }
          } catch (e) {}
          try { if (RC.turnCard.onChange) RC.turnCard.onChange(tid); } catch (e) {}
          return _p0._el || null;
        }
      }
    }
    part.seq = t.parts.length;
    t.parts.push(part);
    var el = renderPart(t, part);
    if (el) part._el = el;   // 供 hlcard 合并就地重建(partsOf 复制时显式跳过 _el,不泄漏进落库)
    if (part.kind === 'hlcard') _opsChanged();
    if (part.kind === 'tool' && !t.flow.hidden) _paintFlow(t);   // 面板开着 → 实时补画
    // ★ 容器一有新内容就通知落库。**不能只在 response.done 落库**:展示型工具(天气/搜索/配图)
    //   跑完后 relay 设了 no_create —— **不会再有下一个 response**,于是 tool/card 这两个 part
    //   永远等不到落库时机 → 刷新后卡片消失(用户实测)。容器是内容的唯一来源,由它自己触发。
    try { if (RC.turnCard.onChange) RC.turnCard.onChange(tid); } catch (e) {}
    return el;
  }

  // 流式文字:唯一允许"就地更新"的 part(不变式②的例外)。response 结束调 freezeDraft。
  function _draftKey(itemId, origin, role) { return (origin || 'app') + ':' + (role || 'assistant') + ':' + (itemId || 'legacy'); }
  function draftText(tid, text, role, itemId, origin) {
    var t = _lookup(tid) || open(tid);
    if (!t) return;
    t._live = true; t._liveFinal = false; t._streamVersion = ++_streamVersion;
    if (role === 'user') t.el.className = 'asst-msg asst-u rc-turn';
    itemId = String(itemId || '');
    origin = origin || 'app';
    var draftKey = _draftKey(itemId, origin, role);
    t._drafts = t._drafts || Object.create(null);
    t.draft = t._drafts[draftKey] ||
      (itemId ? t.parts.filter(function (p) { return p.kind === 'text' && p.item_id === itemId && (p.origin || 'runner') === origin; })[0] : t.draft) || null;
    if (!t.draft) {
      t.draft = { kind: 'text', text: '', role: role || 'assistant', origin: origin, seq: t.parts.length, _streamText: true };
      if (itemId) t.draft.item_id = itemId;
      t.parts.push(t.draft);
      if (!_nativePresentation()) {
      t.draft._el = document.createElement('div');
      // role=user：用户自己正在说的话，渲成他的气泡而不是助手正文（2026-09-21）。
      // 复用 .asst-u（已有的蓝色靠右气泡）：实时那条和定稿后那条长得一样，
      // 定稿替换时不会有样式跳变。
      t.draft._el.className = (role === 'user')
        ? 'rc-part rc-part-text rc-part-user'
        : 'rc-part rc-part-text';
      t.bd.appendChild(t.draft._el);
      }
    }
    t._drafts[draftKey] = t.draft;
    t.draft._draftKey = draftKey;
    t.draft._streamDraft = true;
    t.draft.text = text;
    if (!_nativePresentation() && t.draft._el) _md(t.draft._el, text);
    _scroll();
  }
  function _freezePart(t, draft) {
    if (!draft) return;
    if (!(draft.text || '').trim()) {   // 空 draft:撤掉,别在历史里留空块
      try { draft._el.remove(); } catch (e) {}
      t.parts.splice(t.parts.indexOf(draft), 1);
    }
    draft._streamDraft = false;
    if (t._drafts) delete t._drafts[draft._draftKey];
    if (t.draft === draft) t.draft = null;
  }
  function freezeDraft(tid, itemId, origin, role) {
    var t = _lookup(tid);
    if (!t) return;
    var draft = itemId != null ? (t._drafts && t._drafts[_draftKey(itemId, origin, role)]) : t.draft;
    _freezePart(t, draft);
  }

  function _partIdentity(part, index) {
    var source = String(part.origin || 'runner') + ':';
    if (part.kind === 'tool') {
      var call = part.call_id || part.callId || part.item_id || part.id;
      if (call) return source + 'tool:' + call;
    }
    if (part.id || part.item_id) return source + part.kind + ':' + (part.item_id || part.id);
    var entity = part.gid || part.cid || (part.card && (part.card.gid || part.card.cid));
    if (entity) return part.kind + ':entity:' + entity;
    return part.kind + ':' + (part.origin || 'runner') + ':' + (part.seq == null ? index : part.seq);
  }

  // Reconcile one persisted message. Never rebuild a live turn: card entities,
  // open tool details and the reader's selection belong to their existing nodes.
  // Omitted App parts are retained; only explicit absorbed_ids delete a turn.
  function reconcile(tid, message, options) {
    options = options || {};
    var alias = tidByTurnId(tid);
    var t = (alias && _turns[alias]) || _turns[tid] || open(tid);
    if (!t) return null;
    t._live = true; t._streamVersion = ++_streamVersion;
    t.el.setAttribute('data-turn-id', String(tid));
    t.meta = { via: message.via || (t.meta && t.meta.via) || '',
      threadId: message.thread_id || (t.meta && t.meta.threadId) || '', turnId: tid };
    var role = message.role || 'assistant';
    var sourceOrigin = message.origin || options.origin || 'runner';
    var incoming = Array.isArray(message.parts) ? message.parts : [];
    var textParts = incoming.filter(function (p) { return p && p.kind === 'text'; });
    var legacySlot = t.parts.filter(function (p) { return p.kind === 'text' && p._streamText && !p.item_id; })[0];
    if (role === 'user' || !incoming.length || (legacySlot && !textParts.some(function (p) { return p.item_id || p.id; }))) {
      var text = String(message.content || textParts.map(function (p) { return p.text || ''; }).join('\n\n'));
      if (!legacySlot && !incoming.length) legacySlot = t.parts.filter(function (p) { return p.kind === 'text' && !p.item_id; })[0];
      var active = t._drafts && t._drafts[_draftKey(message.item_id, sourceOrigin, role)];
      if (options.final || !active) {
        if (legacySlot && !message.item_id) t.draft = legacySlot;
        draftText(t.tid, text, role, message.item_id, sourceOrigin);
        if (options.final) freezeDraft(t.tid, message.item_id, sourceOrigin, role);
      }
    }
    incoming.forEach(function (part, index) {
      if (!part || !part.kind || (part.kind === 'text' && legacySlot && !part.item_id && !part.id)) return;
      var identity = _partIdentity(part, index);
      var current = t.parts.filter(function (p, i) { return _partIdentity(p, i) === identity; })[0];
      if (part.kind === 'tool' && _absorbTool(t, part, true)) return;
      if (current) {
        if (part.kind === 'text') {
          var completesItem = options.final && (!message.item_id || part.item_id === message.item_id || part.id === message.item_id);
          if ((!current._streamDraft || completesItem) && current.text !== part.text) {
            current.text = part.text || ''; if (current._el) _md(current._el, current.text);
          }
          current.origin = part.origin || 'runner';
          if (completesItem) _freezePart(t, current);
        }
        // Entity-backed cards update through their repository subscriptions.
        // Recreating their projection here would lose edits and repeat delivery.
        return;
      }
      var copy = {}; Object.keys(part).forEach(function (k) { if (k.charAt(0) !== '_') copy[k] = part[k]; });
      copy.origin = copy.origin || 'runner';
      t.parts.push(copy);
      var wasReplay = t.historyReplay, node;
      try { t.historyReplay = true; node = renderPart(t, copy); } finally { t.historyReplay = wasReplay; }
      if (node) copy._el = node;
    });
    if (options.final) {
      if (message.item_id) freezeDraft(t.tid, message.item_id, sourceOrigin, role);
      else {
        Object.keys(t._drafts || {}).forEach(function (key) { _freezePart(t, t._drafts[key]); });
        freezeDraft(t.tid);
      }
      idle(t.tid); t._liveFinal = !Object.keys(t._drafts || {}).length;
    }
    if (!t.flow.hidden) _paintFlow(t);
    return t.el;
  }

  function preserveLive(stage, sinceVersion) {
    Object.keys(_turns).forEach(function (tid) {
      var t = _turns[tid];
      if (!t || !t._live || !t.el.isConnected || t.el.parentNode === stage ||
          (t._liveFinal && t._streamVersion <= sinceVersion)) return;
      var realId = t.el.getAttribute('data-turn-id') || tid;
      var replacement = Array.prototype.filter.call(stage.children || [], function (node) {
        return (node.getAttribute('data-turn-id') || node.getAttribute('data-turn')) === realId;
      })[0];
      if (replacement) { window.__bwNativeMessages?.replace(t.el,replacement,stage); replacement.replaceWith(t.el); }
      else { stage.appendChild(t.el); window.__bwNativeMessages?.publish(t.el,stage); }
    });
  }

  // ── 进度状态行 ★用户设计 #49/#52:进行中的状态显示在**标题的下面一行**(卡片标题区内),
  //   绝不做成 body 上方那种独立 spinner。**不是 part、不落库**(所以不违反"只追加不重写")。
  function _statusEl(t) {
    if (!t.statusEl) {
      _ensureHead(t, t.hd ? undefined : '处理中');   // 状态行依附标题下 → 先确保有标题栏
      t.statusEl = document.createElement('div');
      t.statusEl.className = 'rc-turn-status';
      t.el.insertBefore(t.statusEl, t.bd);   // hd 与 bd 之间 = "标题的下面一行"
    }
    return t.statusEl;
  }
  function status(tid, text, done) {
    var t = _turns[tid] || open(tid);
    if (!t) return;
    t.presentationStatus = { text: String(text || ''), done: !!done };
    if (_nativePresentation()) return;
    var s = _statusEl(t);
    if (!text) { s.hidden = true; return; }
    s.hidden = false;
    s.className = 'rc-turn-status' + (done ? ' done' : '');
    s.innerHTML = (done ? '<span class="rc-i rc-i-check"></span> ' : '<span class="vc-spin vc-spin-s"></span> ') + _esc(text);
  }
  // busy = 某个工具在跑:标题=工具名 + 状态行"处理中"(兼容既有调用点;进度一律落在标题区,不进 body)。
  function busy(tid, label) {
    var t = _turns[tid] || open(tid);
    if (!t) return;
    _ensureHead(t, label);
    status(tid, '处理中', false);
  }
  function idle(tid) {
    var t = _lookup(tid);
    if (!t) return;
    t.presentationStatus = { text: '', done: true };
    if (t.statusEl) t.statusEl.hidden = true;
  }

  // ── 历史回放:**同一个 renderPart**(不变式①)────────────────────────────
  function renderTurn(tid, parts, target, options) {
    var t = open(tid, target, options);
    if (!t) return null;
    (parts || []).forEach(function (p) {
      if (p && p.kind === 'text' && p.role === 'user') t.el.className = 'asst-msg asst-u rc-turn';
      // 回放同样要去重：存储里可能并存两条同一次调用（运行器一条 + App 芯片一条），
      // 不挡住的话【流程】面板里同一个工具会列两遍。
      if (p && p.kind === 'tool' && _absorbTool(t, p)) return;
      t.parts.push(p);
      var el = renderPart(t, p);
      if (el) p._el = el;   // 回放的操作卡也要能就地重画(顶部「操作」tab 撤销后轮内那张同步换新)
    });
    return t.el;
  }

  function setTaskId(tid, taskId) { var t = _turns[tid]; if (t) { t.taskId = taskId; if (t._cliPart) t._cliPart.task_id = taskId; } }
  // 阶段1:主编排(AI 直连多步工具,非委托 CLI)的服务端 vtask id → 存**独立字段**,绝不走 setTaskId
  //   (setTaskId 会建 _cliPart 把整卡翻成 isCli=true 的 CLI 渲染,毁掉主编排多 tool part 流程)。阶段2 保存按钮读它。
  function setOrchTaskId(tid, taskId) { var t = _turns[tid]; if (t) t.orchTaskId = taskId; }
  // CLI 任务:运行中就建/更新**唯一**工具 part(否则【流程】要等 done 才有内容 = 用户实测"流程空")。
  //   同一个 part 就地更新 steps(它是本轮的当前 part,未冻结),流程开着就实时补画。
  function cliPart(tid, part) {
    var t = _turns[tid] || open(tid);
    if (!t) return;
    _ensureHead(t, part.label || '任务');
    if (!t._cliPart) { t._cliPart = { kind: 'tool', tool: 'do_task', label: '', steps: [], seq: t.parts.length }; t.parts.push(t._cliPart); }
    if (t.taskId) t._cliPart.task_id = t.taskId;   // 落库 → 刷新回放后仍能「保存为工具」(见 _paintFlow taskId)
    if (part.label) t._cliPart.label = part.label;
    if (part.steps) t._cliPart.steps = part.steps;
    if (part.error != null) t._cliPart.error = part.error;
    if (!t.flow.hidden) _paintFlow(t);
    try { if (RC.turnCard.onChange) RC.turnCard.onChange(tid); } catch (e) {}
  }
  // CLI 任务:运行中就把卡头设成任务名(不必等结束的 tool part)——复用同一个 _ensureHead,
  // 卡片长这样:[卡头=任务名][body 增量渲结果][流程按钮]。
  function title(tid, label) { var t = _turns[tid] || open(tid); if (t) _ensureHead(t, label); }
  function partsOf(tid) {
    var t = _lookup(tid);
    if (!t) return [];
    // ⚠ 2026-09-18：**还在流的那一条草稿不落库**。它每来一个 delta 就变一次，
    //   而 onChange → _syncParts 会把 partsOf 整个发出去 —— 于是半截话被当成正式内容
    //   写进记录（实录：content=「明白了:正面放中文,背面」，而完整那句在另一条记录里，
    //   侧栏因此同时显示半截和全文两份）。定稿由 freezeDraft 之后的那次同步负责。
    return t.parts.filter(function (p) { return p !== t.draft && !p._streamDraft; }).map(function (p) {
      var o = {}; for (var k in p) { if (k.charAt(0) !== '_') o[k] = p[k]; }
      return o;
    });
  }
  // Display and persistence have different contracts: native views need the
  // current draft, while partsOf must never persist an unfinished transcript.
  // Read model state, not rendered Markdown/DOM. Return detached JSON so a
  // native renderer cannot accidentally mutate the authoritative turn.
  function presentationOf(tid) {
    var t = _lookup(tid);
    if (!t) return null;
    var parts = t.parts.map(function (p) {
      var out = {};
      Object.keys(p).forEach(function (key) {
        if (key.charAt(0) !== '_') out[key] = p[key];
      });
      out.streaming = !!p._streamDraft;
      return out;
    });
    var state = t.presentationStatus || { text: '', done: true };
    return JSON.parse(JSON.stringify({
      contract: 'reader-turn-presentation/1',
      tid: t.tid,
      role: parts.some(function (p) { return p.kind === 'text' && p.role === 'user'; }) ? 'user' : 'assistant',
      parts: parts,
      status: state,
      title: t.presentationTitle || '',
      progress: t.prog || null,
      streaming: parts.some(function (p) { return p.streaming; }) || !!(state.text && !state.done)
    }));
  }
  function reset() { Object.values(_turns).forEach(function(t) { window.__bwNativeMessages?.remove(t.el); }); _turns = {}; _cur = null; }
  function setNativePresentation(enabled) {
    enabled = !!enabled;
    if (_nativePresentation() === enabled) return;
    window.__BW_NATIVE_CONVERSATION_DATA__ = enabled;
    Object.keys(_turns).forEach(function (tid) {
      var t = _turns[tid];
      if (enabled) {
        t.parts.forEach(function (part) {
          if (part.kind === 'text' && part._el) { part._el.remove(); delete part._el; }
        });
        if (t._progressResize) { t._progressResize.disconnect(); t._progressResize = null; }
        if (t.hd) { t.hd.remove(); t.hd = null; }
        if (t.statusEl) { t.statusEl.remove(); t.statusEl = null; }
        t._flowBtn = null; t.flow.hidden = true; t.flow.textContent = '';
      } else {
        // The optional legacy mode reconstructs only presentation, without
        // replaying card registration, tool execution or persistence hooks.
        t.parts.forEach(function (part, index) {
          if (part.kind !== 'text' || part._el) return;
          var node = renderPart(t, part); if (!node) return;
          part._el = node;
          var next = t.parts.slice(index + 1).find(function (p) { return p._el && p._el.parentNode === t.bd; });
          if (next) t.bd.insertBefore(node, next._el);
        });
        if (t.presentationTitle) _ensureHead(t, t.presentationTitle);
        if (t.presentationStatus) status(tid, t.presentationStatus.text, t.presentationStatus.done);
        if (t.prog) _paintProgress(t);
      }
    });
  }
  // 容器改名（2026-09-15 根治）：运行器推来真实 turn id 时，把本地临时容器连同已画的部件搬到真实 id 下
  function rename(oldTid, newTid) {
    if (!oldTid || !newTid || oldTid === newTid) return false;
    var t = _turns[oldTid];
    if (!t) return false;
    if (_turns[newTid]) {   // 真实 id 下已有容器（极少）：把部件并过去
      var dst = _turns[newTid];
      t.parts.forEach(function (p) {
        if (p.kind === 'text' && p.item_id && dst.parts.some(function (other) { return other.kind === 'text' && other.item_id === p.item_id; })) {
          try { if (p._el) p._el.remove(); } catch (e) {} return;
        }
        if (p === t.draft && dst.draft) { try { if (p._el) p._el.remove(); } catch (e) {} return; }
        if (_absorbTool(dst, p)) { try { if (p._el) p._el.remove(); } catch (e) {} return; }
        p.seq = dst.parts.length; dst.parts.push(p);
        if (p === t.draft) dst.draft = p;
        if (p._streamDraft) { dst._drafts = dst._drafts || Object.create(null); dst._drafts[p._draftKey] = p; }
        if (p._el && p._el.isConnected) dst.bd.appendChild(p._el);
      });
      dst._live = dst._live || t._live;
      dst._streamVersion = Math.max(dst._streamVersion || 0, t._streamVersion || 0);
      if (dst.draft) dst._liveFinal = false;
      try { window.__bwNativeMessages?.remove(t.el); if (t.el && t.el.parentNode) t.el.parentNode.removeChild(t.el); } catch (e) {}
      if (t._progressResize) t._progressResize.disconnect();
      delete _turns[oldTid];
      if (_cur === oldTid) _cur = newTid;
      return true;
    }
    delete _turns[oldTid];
    t.tid = newTid;
    _turns[newTid] = t;
    try { t.el.setAttribute('data-turn', newTid); t.el.setAttribute('data-turn-id', newTid); } catch (e) {}
    if (_cur === oldTid) _cur = newTid;
    return true;
  }
  function flowOpen(tid) { var t = _lookup(tid); return !!(t && t.flow && !t.flow.hidden); }
  function openFlow(tid) {
    if (_nativePresentation()) return false;
    var t = _lookup(tid); if (!t || !t.flow) return false;
    t.flow.hidden = false; _paintFlow(t);
    try { if (t._flowBtn) t._flowBtn.classList.add('on'); } catch (e) {}
    return true;
  }
  // 按真实 turn id（data-turn-id）找容器 id：回放容器的 id 是 hist_…，对外身份是 turn id
  function tidByTurnId(turnId) {
    var hit = null;
    Object.keys(_turns).forEach(function (k) { var t = _turns[k]; if (!hit && t && t.el && t.el.getAttribute('data-turn-id') === String(turnId)) hit = k; });
    return hit;
  }
  function prune() {
    Object.keys(_turns).forEach(function (tid) {
      var t = _turns[tid];
      if (!t || !t.el || !t.el.isConnected) {
        if (t && t._progressResize) t._progressResize.disconnect();
        window.__bwNativeMessages?.remove(t?.el); delete _turns[tid];
      }
    });
    if (_cur && !_turns[_cur]) _cur = null;
  }

  // ── CLI 委托任务追踪(从 rc-assistant 搬入:卡头/增量正文/流程/建纸 CA;阅读器与工具库页共用同一份)──
  function trackCli(tid, taskId, label) {
    // ★ 用户拍板(9a1a6c1 截图):CLI 卡要跟 web_search 卡**同款**——[卡头=任务名][body 增量渲结果][流程]。
    //   不再用"进度行 + 全塞 sub_steps"那套。走 turn 容器同一套:title 设卡头、draftText 增量渲正文、
    //   done 时补一个 tool part(只为设卡头 + 进【流程】+ 让"保存为工具"能拿到 steps)。
    try { RC.turnCard && RC.turnCard.setTaskId && RC.turnCard.setTaskId(tid, taskId); } catch (_) {}
    try { RC.turnCard.title(tid, label); } catch (_) {}                 // 卡头=CLI任务名(用户设计 #57)
    try { RC.turnCard.status(tid, '规划中', false); } catch (_) {}      // 进度=标题下面一行(用户设计 #49/#52)
    var n = 0, _appliedCA = 0, _miss = 0;
    function _applyNewCAs(cas) {   // #2:client_action(建纸)一出现就应用,不等 CLI 说完(page_show 一跑纸就开始建)
      cas = cas || [];
      for (var i = _appliedCA; i < cas.length; i++) {
        var a = cas[i];
        try { if (a && a.fn && typeof window[a.fn] === 'function') window[a.fn].apply(null, a.args || []); } catch (_) {}
      }
      _appliedCA = cas.length;
    }
    function _mkSteps(steps) {   // 每步带 tool + **输入(args)/输出(result)** → 流程条能看到工具间数据流(#44)
      var out = (steps || []).map(function (x) {
        var d = '';
        try { if (x.args && Object.keys(x.args).length) d += '**输入**\n```json\n' + JSON.stringify(x.args, null, 1) + '\n```\n'; } catch (_) {}
        if (x.result) d += '**输出**\n' + String(x.result);
        return { label: x.name || String(x), detail: d || (x.status || ''), tool: x.name || '' };
      });
      // #1(用户):造纸流程只要建了「让 AI 检查」按钮(page_add/page_show 的 args 带 event:check),
      //   就补一个合成节点「检查按钮 → 判分 AI」,tool=dictation_grade → **长按弹的是判分 AI 自己的设置**
      //   (可改判分指令 + 换模型),而不是外层 CLI 的设置。
      var hasCheck = false;
      try {
        (steps || []).forEach(function (x) {
          var s = JSON.stringify(x.args || '');
          if (/"event"\s*:\s*"check"/.test(s) || s.indexOf('让 AI 检查') >= 0) hasCheck = true;
        });
      } catch (_) {}
      if (hasCheck) {
        out.push({ label: '🔘 检查按钮 → 判分 AI', tool: 'dictation_grade',
          detail: '**输入**  你在这张纸上的手写作答\n\n**这一步**  按下纸上「让 AI 检查」时，用判分 AI 看这页手写、逐空识别并打分。\n\n**输出**  检查结果(逐空对错 / 点评 / 总评)\n\n> 长按本条可**改判分指令、换模型**。' });
      }
      return out;
    }
    function showSnapshot(d) {
        var steps = d.steps || [];
        // 进度 → 标题下面一行;结果 → body 增量渲(#52/#57);工具 → 运行中就进【流程】(#5:流程不再空)。
        try { RC.turnCard.status(tid, (d.step || '规划中') + (steps.length ? ('  ·  已用 ' + steps.length + ' 个工具') : ''), false); } catch (_) {}
        if (d.partial) { try { RC.turnCard.draftText(tid, d.partial); } catch (_) {} }
        try { RC.turnCard.cliPart(tid, { label: label, steps: _mkSteps(steps) }); } catch (_) {}   // #5 流程实时填
        _applyNewCAs(d.client_actions);                                                             // #2 建纸即时开始
        if (d.status === 'done' || d.status === 'error') {
          try {
            var body = String(d.partial || d.speak || (d.status === 'error' ? d.error : '') || '').trim();
            var _fl = d.result && d.result.flow;   // ★流程摘要(查找类带实际参数)→ 附在正文下,让用户看到 CLI 做了啥
            if (_fl) body = (body ? body + '\n\n' : '') + '🔎 过程:' + _fl;
            if (body) { RC.turnCard.draftText(tid, body); }
            RC.turnCard.freezeDraft(tid);
            RC.turnCard.status(tid, d.status === 'error' ? ('出错：' + (d.error || '')) : '制作完毕', true);   // #52
            RC.turnCard.cliPart(tid, { label: label, steps: _mkSteps(steps),
              error: d.status === 'error' ? (d.error || '失败') : '' });
            _applyNewCAs(d.client_actions);   // 收尾补一次(done 时才出现的 client_action)
          } catch (_) {}
          return true;
        }
        return false;
    }
    function stopWaiting(message, suffix) {
      try { RC.turnCard.status(tid, message, true); RC.turnCard.freezeDraft(tid);
        RC.turnCard.cliPart(tid, {label:label + (suffix || ''),error:message}); } catch (_) {}
    }
    if (window.__bwNativeAssistantStream?.watchTask) {
      return window.__bwNativeAssistantStream.watchTask('cli', taskId, showSnapshot).then(function (outcome) {
        if (outcome === 'timeout') stopWaiting('等太久了，没等到结果', '(超时)');
        else if (outcome === 'missing') stopWaiting('任务丢失(服务可能重启了),请核对结果', '(任务丢失)');
      }).catch(function (error) { stopWaiting('任务追踪失败：' + (error.message || error)); });
    }
    (function poll() {
      if (n++ > 600) { stopWaiting('等太久了，没等到结果', '(超时)'); return; }
      fetch('/api/voice/task-status?id=' + encodeURIComponent(taskId)).then(function (r) { return r.json(); }).then(function (d) {
        if (!d || !d.ok) {
          if (++_miss >= 8) { stopWaiting('任务丢失(服务可能重启了),重发一次即可', '(任务丢失)'); return; }
          setTimeout(poll, 1500); return;
        }
        _miss = 0;
        if (!showSnapshot(d)) setTimeout(poll, 1500);
      }).catch(function () { setTimeout(poll, 2000); });
    })();
  }

  // ── 进度点线（2026-09-13）：一串圆点用线连起来，颜色=状态。
  //   已知 total（skill 运行器）= 固定 total 个点，按 step 上色；未知（临场调用）= running 加点。
  //   只是显示，不落库：刷新后流程面板里有完整列表，点线不需要回放。
  function _progressCss() {
    if (document.getElementById('rc-flow-dots-css')) return;
    var s = document.createElement('style'); s.id = 'rc-flow-dots-css';
    s.textContent = '.rc-turn>.vc-if-hd{min-width:0;overflow:hidden}' +
      '.rc-turn>.vc-if-hd>.rc-flow-title{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
      '.rc-turn>.vc-if-hd>.rc-flow-dots-host{flex:0 1 50%;min-width:0;max-width:50%;overflow:hidden;line-height:18px}' +
      '.rc-flow-progress{display:block;max-width:100%;overflow:hidden;white-space:nowrap}' +
      '.rc-flow-dots{display:inline-flex;align-items:center;gap:0;vertical-align:middle}' +
      '.rc-flow-dots i{width:10px;height:10px;border-radius:50%;background:#3a4256;border:1.5px solid #4c5670;box-sizing:border-box;flex:none}' +
      '.rc-flow-dots i.run{background:#3b6fd4;border-color:#6fa1ff;animation:rcFlowPulse 1s ease-in-out infinite}' +
      '.rc-flow-dots i.done{background:#7ee787;border-color:#7ee787}' +
      '.rc-flow-dots i.err{background:#f85149;border-color:#f85149}' +
      '.rc-flow-dots b{width:14px;height:2px;background:#4c5670;flex:none}' +
      '.rc-flow-dots b.done{background:#7ee787}' +
      '.rc-flow-counts{display:none;align-items:center;flex-wrap:wrap;gap:2px 8px;font-size:11px;line-height:18px;font-variant-numeric:tabular-nums}' +
      '.rc-flow-counts small{font-size:inherit;font-weight:600;white-space:nowrap;color:#9aa4b8}' +
      '.rc-flow-counts .done{color:#7ee787}.rc-flow-counts .err{color:#f85149}.rc-flow-counts .run{color:#6fa1ff}' +
      '.rc-flow-progress.is-counted .rc-flow-dots{display:none}.rc-flow-progress.is-counted .rc-flow-counts{display:flex}' +
      '@keyframes rcFlowPulse{0%,100%{opacity:1}50%{opacity:.45}}';
    (document.head || document.documentElement).appendChild(s);
  }
  function progress(tid, evt) {
    var t = _turns[tid]; if (!t || !evt) return null;
    if (!_nativePresentation()) _progressCss();
    var pr = t.prog || (t.prog = { total: null, states: [], skill: '', label: '' });
    if (evt.skill) pr.skill = evt.skill;
    if (evt.label) pr.label = evt.label;
    if (evt.total) {   // skill 运行器：定长
      if (pr.total !== evt.total) { pr.total = evt.total; pr.states = new Array(evt.total); }
      var i = Math.max(0, Math.min(evt.total, evt.step || 1) - 1);
      pr.states[i] = evt.status === 'done' ? 'done' : (evt.status === 'error' ? 'err' : 'run');
    } else if (evt.status === 'running') {
      pr.states.push('run');
    } else if (pr.states.length) {
      var last = pr.states.length - 1;
      pr.states[last] = evt.status === 'error' ? 'err' : 'done';
    } else {
      pr.states.push(evt.status === 'error' ? 'err' : 'done');
    }
    _paintProgress(t);
    return pr;
  }
  function progressHtml(tid, availableWidth) {
    var t = _turns[tid]; if (!t || !t.prog || !t.prog.states.length) return '';
    var pr = t.prog, n = pr.total || pr.states.length;
    var counts = { done: 0, err: 0, run: 0, pending: 0 };
    for (var j = 0; j < n; j++) counts[pr.states[j] || 'pending']++;
    var summary = '成功 ' + counts.done + '，失败 ' + counts.err + '，运行中 ' + counts.run + '，待处理 ' + counts.pending;
    var width = 24 * n - 14;
    // A bounded default also protects the floating subtitle, which reuses this HTML.
    var counted = width > (availableWidth > 0 ? availableWidth : 130);
    var out = '<span class="rc-flow-progress' + (counted ? ' is-counted' : '') + '" data-points-width="' + width + '" role="img" aria-label="' + summary + '" title="' + summary + '"><span class="rc-flow-dots" aria-hidden="true">';
    for (var i = 0; i < n; i++) {
      var st = pr.states[i] || '';
      if (i) out += '<b class="' + (pr.states[i - 1] === 'done' ? 'done' : '') + '"></b>';
      out += '<i class="' + st + '"></i>';
    }
    out += '</span><span class="rc-flow-counts" aria-hidden="true"><small class="done">✓ ' + counts.done + '</small><small class="err">! ' + counts.err + '</small>';
    if (counts.run) out += '<small class="run">◌ ' + counts.run + '</small>';
    if (counts.pending) out += '<small>· ' + counts.pending + '</small>';
    out += '</span></span>';
    return out;
  }
  function _fitProgress(el) {
    var row = el && el.firstElementChild; if (!row) return;
    var available = el.clientWidth;
    row.classList.toggle('is-counted', Number(row.getAttribute('data-points-width')) > (available > 0 ? available : 130));
  }
  function _paintProgress(t) {
    if (_nativePresentation()) return;
    try {
      _ensureHead(t, t.prog && t.prog.skill ? t.prog.skill : undefined);
      var host = t.hd; if (!host) return;
      var el = host.querySelector(':scope > .rc-flow-dots-host');
      if (!el) {
        el = document.createElement('span'); el.className = 'rc-flow-dots-host'; host.insertBefore(el, host.querySelector('button'));
        if (typeof ResizeObserver !== 'undefined') {
          t._progressResize = new ResizeObserver(function (entries) { entries.forEach(function (entry) { _fitProgress(entry.target); }); });
          t._progressResize.observe(el);
        }
      }
      el.innerHTML = progressHtml(t.tid, el.clientWidth);
      _fitProgress(el);
    } catch (e) {}
  }

  /** 把某个轮次容器从页面上摘掉（2026-09-21）。
   *
   * ⚠ 为什么需要它：语音先以**独立气泡**实时渲出来，随后那句话被「收拢」进后台
   *   轮次卡（服务端把库里那条零散记录删了）。但屏幕上那个独立元素没人去清 ——
   *   表现是同一句在卡外面和卡里面各出现一次，而库里其实只有一条。
   *   服务端现在会把被收拢的 id 随事件推来，这里负责真的把它摘掉。
   * ⚠ 只摘我们自己建的容器；摘不到就当没有，绝不抛异常打断事件处理。
   */
  function drop(tid) {
    tid = String(tid || '');
    var t = tid && _turns[tid];
    if (!t) return false;
    try { window.__bwNativeMessages?.remove(t.el); if (t.el && t.el.parentNode) t.el.parentNode.removeChild(t.el); } catch (_) {}
    if (t._progressResize) t._progressResize.disconnect();
    delete _turns[tid];
    return true;
  }

  RC.turnCard = {
    setNativePresentation: setNativePresentation,
    presentationOf: presentationOf,
    drop: drop,
    open: open, addPart: addPart, progress: progress, progressHtml: progressHtml, draftText: draftText, freezeDraft: freezeDraft, busy: busy, idle: idle,
    renderTurn: renderTurn, reconcile: reconcile, preserveLive: preserveLive, streamVersion: function () { return _streamVersion; }, partsOf: partsOf, reset: reset, prune: prune, setTaskId: setTaskId, setOrchTaskId: setOrchTaskId, title: title, status: status, cliPart: cliPart,
    current: function () { return _cur; },
    trackCli: trackCli,
    has: function (tid) { return !!_lookup(tid); },
    // 操作条(高亮/卡片改删/便签/自建页)的统一出口:顶部「操作」tab 用
    opItems: opItems, opAction: opAction, markOp: markOp, performOperation: performOperation, performOperationItem: performOperationItem,
    rename: rename, openFlow: openFlow, flowOpen: flowOpen, tidByTurnId: tidByTurnId,
    onOpsChange: function (fn) { if (typeof fn === 'function') _opsListeners.push(fn); },
    opsChanged: _opsChanged,
  };

  // In the App, the functions below are compatibility views of Swift-owned
  // turns. They enqueue commands, then reuse existing artifact action handles
  // from the committed projection. The browser keeps the synchronous owner.
  if (window.__bwNativeTurns) {
    var nativeTurns = window.__bwNativeTurns, nativeEpoch = 0, nativeApply = false;
    var original = Object.assign({}, RC.turnCard), changeListener = null;
    function enabled() { return _nativePresentation(); }
    function sendTurn(command) {
      command.generation = nativeEpoch;
      nativeTurns.enqueue(command);
    }
    function ensure(tid, target, options) {
      var t = open(tid, target, options);
      if (t && !t._nativeOpened) {
        t._nativeOpened = true;
        sendTurn({action:'open',tid:t.tid,meta:t.meta,historyReplay:t.historyReplay});
      }
      return t;
    }
    function change(tid, action, values) {
      var t = ensure(tid);
      if (t) sendTurn(Object.assign({action:action,tid:t.tid},values || {}));
      return t;
    }
    function disposition(p) { return JSON.stringify((p.items || []).map(function (it) { return [it.id,it.note,!!it.undone,!!it.gone]; })); }
    function commitOperations(tid) {
      var t = _lookup(tid), updates = [];
      if (!t) return;
      t.parts.forEach(function (p) {
        if (p.kind !== 'hlcard' || !p._nativeID || p._nativeDisposition === disposition(p)) return;
        p._nativeDisposition = disposition(p);
        updates.push({id:p._nativeID,items:(p.items || []).map(function (it,index) { return {index:index,id:it.id,note:it.note,undone:!!it.undone,gone:!!it.gone}; })});
      });
      if (updates.length) sendTurn({action:'operationState',tid:t.tid,parts:updates});
    }
    function acceptTurns(result) {
      // Each batch is committed before this callback. A reset submitted while
      // an older batch was running must not remount the older conversation.
      if (result.generation !== nativeEpoch) return;
      nativeApply = true;
      try {
        (result.removed || []).forEach(function (id) { original.drop(id); });
        (result.turns || []).forEach(function (state) {
          var t = _turns[state.id];
          if (!t || !t._nativeOpened) return;
          var previous = new Map(t.parts.map(function (p) { return [p._nativeID,p]; })), next = [];
          (state.parts || []).forEach(function (value) {
            var p = previous.get(value._nativeID), oldItems = p && p.kind === 'hlcard' ? JSON.stringify(p.items) : '';
            if (p) {
              previous.delete(value._nativeID);
              Object.keys(p).forEach(function (key) { if (key !== '_el' && key !== '_nativeDisposition' && !(key in value)) delete p[key]; });
              Object.assign(p,value);
              if (p.kind === 'hlcard' && oldItems !== JSON.stringify(p.items)) _rerenderPart(t,p);
            } else {
              p = value;
              // Reconciliation is a replay: it must not register a new draft
              // simply because history attached an already existing entity.
              var replay = t.historyReplay;
              t.historyReplay = state.historyReplay || p.origin === 'runner';
              try { var el = renderPart(t,p); if (el) p._el = el; }
              finally { t.historyReplay = replay; }
            }
            if (p.kind === 'hlcard') p._nativeDisposition = disposition(p);
            next.push(p);
          });
          previous.forEach(function (p) { try { p._el?.remove(); } catch (_) {} });
          t.parts = next; t.draft = next.find(function (p) { return p._nativeID === state.draft; }) || null;
          t._drafts = Object.create(null);
          Object.keys(state.drafts || {}).forEach(function (key) { var p = next.find(function (p) { return p._nativeID === state.drafts[key]; }); if (p) t._drafts[key] = p; });
          t._cliPart = next.find(function (p) { return p._nativeID === state.cliPart; }) || null;
          t.presentationTitle = state.title; t.presentationStatus = state.status; t.prog = state.progress;
          t.meta = state.meta; t.taskId = state.taskId; t.orchTaskId = state.orchTaskId;
          t._live = state.live; t._liveFinal = state.final; t._streamVersion = state.streamVersion;
          t._nativePresentation = state.presentation; t._nativePersistedParts = state.persistedParts;
          t.el.className = 'asst-msg ' + (state.presentation.role === 'user' ? 'asst-u' : 'asst-a') + ' rc-turn';
          if (state.meta?.turnId) t.el.setAttribute('data-turn-id',state.meta.turnId);
        });
        _streamVersion = result.streamVersion;
        if (result.current && _turns[result.current]) _cur = result.current;
      } finally { nativeApply = false; }
      (result.persist || []).forEach(function (tid) { if (_turns[tid] && changeListener) changeListener(tid); });
      _opsChanged();
      window.dispatchEvent(new CustomEvent('rc:native-turn-update'));
    }
    nativeTurns.connect(acceptTurns);
    Object.defineProperty(RC.turnCard,'onChange',{configurable:true,
      get:function () { return function (tid) {
        if (!enabled()) { if (changeListener) changeListener(tid); return; }
        if (!nativeApply) commitOperations(tid);
        nativeTurns.settle().then(function () { if (changeListener && _lookup(tid)) changeListener(tid); }).catch(function () {});
      }; },
      set:function (fn) { changeListener = typeof fn === 'function' ? fn : null; }
    });
    function wrap(name, fn) { RC.turnCard[name] = function () { return enabled() ? fn.apply(null,arguments) : original[name].apply(null,arguments); }; }
    wrap('open',ensure);
    wrap('addPart',function (tid,part) { change(tid,'append',{part:part}); return null; });
    wrap('draftText',function (tid,text,role,itemId,origin) { change(tid,'draft',{text:String(text || ''),role:role || 'assistant',itemId:itemId == null ? undefined : String(itemId),origin:origin || 'app'}); });
    wrap('freezeDraft',function (tid,itemId,origin,role) { change(tid,'freeze',{itemId:itemId == null ? undefined : String(itemId),origin:origin || 'app',role:role || 'assistant'}); });
    wrap('title',function (tid,text) { change(tid,'title',{text:text}); });
    wrap('status',function (tid,text,done) { change(tid,'status',{text:text,done:!!done}); });
    wrap('busy',function (tid,text) { change(tid,'busy',{text:text}); });
    wrap('idle',function (tid) { change(tid,'idle'); });
    wrap('setTaskId',function (tid,id) { change(tid,'task',{taskId:id}); });
    wrap('setOrchTaskId',function (tid,id) { change(tid,'orchestrator',{taskId:id}); });
    wrap('cliPart',function (tid,part) { change(tid,'cli',{part:part}); });
    wrap('progress',function (tid,event) { var t = change(tid,'progress',{event:event}); return t?.prog || null; });
    wrap('renderTurn',function (tid,parts,target,options) { var t = ensure(tid,target,options); if (!t) return null; sendTurn({action:'import',tid:t.tid,parts:parts || []}); return t.el; });
    wrap('reconcile',function (tid,message,options) { var t = ensure(tid); if (!t) return null; sendTurn({action:'reconcile',tid:tid,message:message,options:options || {}}); return t.el; });
    wrap('partsOf',function (tid) { return JSON.parse(JSON.stringify(_lookup(tid)?._nativePersistedParts || [])); });
    wrap('presentationOf',function (tid) { return JSON.parse(JSON.stringify(_lookup(tid)?._nativePresentation || null)); });
    wrap('drop',function (tid) { if (!_lookup(tid)) return false; sendTurn({action:'drop',tid:tid}); return original.drop(_lookup(tid).tid); });
    wrap('rename',function (oldTid,newTid) {
      var t = _turns[oldTid]; if (!t || !newTid || oldTid === newTid) return false;
      sendTurn({action:'rename',tid:oldTid,newTid:newTid});
      if (_turns[newTid]) {
        // Move existing artifact handles before removing the old shell. The
        // committed native merge decides which survive; no draft is registered twice.
        var target = _turns[newTid];
        t.parts.forEach(function (p) { if (p._el) target.bd.appendChild(p._el); });
        target.parts = target.parts.concat(t.parts);
        original.drop(oldTid);
      }
      else { delete _turns[oldTid]; t.tid = newTid; _turns[newTid] = t; t.el.setAttribute('data-turn',newTid); t.el.setAttribute('data-turn-id',newTid); }
      if (_cur === oldTid) _cur = newTid; return true;
    });
    wrap('reset',function () { nativeEpoch++; sendTurn({action:'reset'}); original.reset(); });
    wrap('prune',function () {
      Object.keys(_turns).forEach(function (tid) {
        var t = _turns[tid]; if (!t?.el?.isConnected) { sendTurn({action:'drop',tid:tid}); original.drop(tid); }
      });
      if (_cur && !_turns[_cur]) _cur = null;
    });
    RC.turnCard.settle = function () { return enabled() ? nativeTurns.settle() : Promise.resolve(); };
  }
})();
