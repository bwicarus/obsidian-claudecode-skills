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

  function _thread() { return document.getElementById('asst-thread'); }
  function _esc(s) { var d = document.createElement('div'); d.textContent = String(s == null ? '' : s); return d.innerHTML; }
  function _md(el, text) {
    try {
      if (RC.assistant && RC.assistant.renderMd) { RC.assistant.renderMd(el, text, true); return; }
    } catch (e) {}
    el.textContent = text;
  }
  function _scroll() { try { var t = _thread(); if (t) t.scrollTop = t.scrollHeight; } catch (e) {} }

  // ── 容器 ─────────────────────────────────────────────────────────────────
  function open(tid) {
    var th = _thread();
    if (!th) return null;
    if (_turns[tid] && _turns[tid].el && _turns[tid].el.isConnected) { _cur = tid; return _turns[tid]; }
    var el = document.createElement('div');
    el.className = 'asst-msg asst-a rc-turn';   // 复用既有气泡外观;有工具时再补 .vc-if 卡头(见 _ensureHead)
    el.setAttribute('data-turn', tid);
    var bd = document.createElement('div'); bd.className = 'rc-turn-bd';
    var flow = document.createElement('div'); flow.className = 'rc-turn-flow'; flow.hidden = true;
    el.appendChild(bd); el.appendChild(flow);
    th.appendChild(el);
    var t = _turns[tid] = { el: el, hd: null, bd: bd, flow: flow, parts: [], draft: null };
    _cur = tid;
    _scroll();
    return t;
  }

  // 工具出现时才长出卡头(带【流程】按钮)。没有工具的轮次就是一条普通气泡 —— 容器**自适应**,
  // 而不是像旧代码那样把气泡"升格"成另一个 DOM(那会把还在流的文字搬来搬去 → 显示两遍)。
  function _ensureHead(t, label) {
    if (t.hd) {
      var s0 = t.hd.querySelector(':scope > span');
      if (s0 && label) s0.textContent = label;
      return t.hd;
    }
    // ⚠ 样式必须先在:rc-toolchip 的 mountFlow 就是这么做的(它注释写着「样式必须在,否则裸 <button> = 白块」),
    //   我上一版漏了这一步 → 【流程】按钮退化成 Safari 原生 push-button(白方块,用户实测)。
    try { if (RC.voiceCard && RC.voiceCard.css) RC.voiceCard.css(); } catch (e) {}
    t.el.classList.add('vc-if');   // 复用既有结果卡外观(见 _infoCardEl)
    var hd = document.createElement('div'); hd.className = 'vc-if-hd';
    var sp = document.createElement('span'); sp.textContent = label || '工具调用';
    hd.appendChild(sp);
    // 141:【流程】按钮**不再自己 new** —— 调 rc-toolchip 的唯一创建点(那条路一直渲染正常;
    //   我自己拼的那个一模一样的 <button> 却是白圆块)。跑同一段代码,差异就无处藏身。
    var b = RC.toolChip.flowBtn(function () {
      t.flow.hidden = !t.flow.hidden;
      if (!t.flow.hidden) _paintFlow(t);
      return !t.flow.hidden;
    });
    hd.appendChild(b);
    t.el.insertBefore(hd, t.bd);
    t.hd = hd;
    return hd;
  }

  // 141:ICON_FLOW 已删 —— 图标随 rc-toolchip 的 flowBtn 一起来(单一来源)。

  // ── ★ 唯一渲染器:实时与历史回放都走这里(不变式①)──────────────────────
  function renderPart(t, p) {
    var d = document.createElement('div');
    d.className = 'rc-part rc-part-' + (p.kind || 'text');
    if (p.kind === 'text') {
      if (!(p.text || '').trim()) return null;
      _md(d, p.text);
    } else if (p.kind === 'card') {
      // 结果卡(天气/搜索/配图/视频):**复用既有** _infoCardEl —— 别另造一套
      var ce = null;
      try { ce = window.__vcInfoCardEl && window.__vcInfoCardEl(p.card || {}); } catch (e) {}
      if (!ce) return null;
      ce.classList.remove('asst-msg', 'asst-a');   // 它现在是容器内的一块,不是独立气泡
      ce.classList.add('rc-part-cardin');
      d.appendChild(ce);
    } else if (p.kind === 'tool') {
      _ensureHead(t, p.label || p.tool || '工具');
      return null;   // 工具本身不在正文里占块 —— 它的细节收在【流程】按钮里(用户设计)
    } else if (p.kind === 'meta') {
      return null;   // 设置项只落库,不显示(点【流程】能看到)
    } else {
      return null;
    }
    t.bd.appendChild(d);
    _scroll();
    return d;
  }

  // 流程面板:把本轮所有 tool part(+meta)画成 AI 请求 → 工具 → 结果 的线性流程
  function _paintFlow(t) {
    var f = t.flow;
    f.innerHTML = '';
    var tools = t.parts.filter(function (p) { return p.kind === 'tool'; });
    var meta = t.parts.filter(function (p) { return p.kind === 'meta'; })[0];
    if (meta) {
      var m = document.createElement('div'); m.className = 'rc-flow-meta';
      m.textContent = ['模型 ' + (meta.model || '—'), meta.effort ? ('深度 ' + meta.effort) : '',
        meta.voice_mode ? ('档位 ' + meta.voice_mode) : ''].filter(Boolean).join(' · ');
      f.appendChild(m);
    }
    // ★ E:≥2 个工具的流程 → 提供「保存为工具」(ADR:任何 ≥2 工具的卡都能一键固化)。
    //   CLI 任务(t.taskId)的内部工具塞在**单个** part 的 sub_steps 里 → tools.length 恒=1,
    //   但用户拍板"所有走 CLI 的多步任务都要能保存" → 用它记的步数(sub_steps ≥2)也放行。
    var cliSteps = tools.reduce(function (a, p) { return a + ((p.steps && p.steps.length) || 0); }, 0);
    var canSave = tools.length >= 2 || (t.taskId && cliSteps >= 2);
    if (canSave) {
      var sv = document.createElement('div'); sv.className = 'rc-flow-save';
      var sb = document.createElement('span');
      sb.className = 'up2-b-btn'; sb.setAttribute('role', 'button'); sb.setAttribute('tabindex', '0');
      sb.textContent = '💾 保存为工具';
      sb.style.cssText = 'display:inline-flex;padding:6px 14px;border-radius:9px;background:#3b6fd4;color:#fff;font-size:12.5px;cursor:pointer;-webkit-appearance:none;appearance:none';
      sb.addEventListener('click', function (ev) {
        ev.stopPropagation();
        var nm = prompt('给这个工具起个名字(下次说名字就能直接用):');
        if (!nm) return;
        var body = { name: nm };
        if (t.taskId) {
          body.task_id = t.taskId;   // ★ CLI 任务:保存**执行轨迹**(自包含回放,不问数据源)
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
    if (!tools.length) { f.appendChild(document.createTextNode('(本轮没有工具调用)')); return; }
    // ★用户设计 #3:每个工具都以「工具长条」显示、可长按进各种设置 —— **复用 rc-toolchip 的 paintFlow**
    //   (.vc-fn 长条 + 长按 openDetail),别再自己画纯文本步骤(那正是"擅自设计"的又一处)。
    var main = tools[tools.length - 1] || {};
    var isCli = !!t.taskId && main.steps && main.steps.length;   // CLI 卡:内部工具塞在单个 part 的 sub_steps 里
    var steps;
    if (isCli) {
      steps = main.steps.map(function (s) { return { label: s.label || String(s), detail: s.detail || '' }; });
    } else {
      steps = tools.map(function (p) {
        var a = ''; try { a = (p.args && Object.keys(p.args).length) ? JSON.stringify(p.args) : ''; } catch (e) {}
        var det = a;
        if (p.result) det = (a ? a + '\n\n' : '') + String(p.result).slice(0, 2000);
        return { label: p.label || p.tool || '工具', detail: det, sec: p.took_s, model: p.model };
      });
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
    if (RC.toolChip && RC.toolChip.renderFlowInto) { RC.toolChip.renderFlowInto(box, chip); }
    else { box.textContent = steps.map(function (s) { return '· ' + s.label; }).join('\n'); }   // 兜底
  }

  // ── 注入 ─────────────────────────────────────────────────────────────────
  function addPart(tid, part) {
    var t = _turns[tid] || open(tid);
    if (!t) return null;
    part.seq = t.parts.length;
    t.parts.push(part);
    var el = renderPart(t, part);
    if (part.kind === 'tool' && !t.flow.hidden) _paintFlow(t);   // 面板开着 → 实时补画
    // ★ 容器一有新内容就通知落库。**不能只在 response.done 落库**:展示型工具(天气/搜索/配图)
    //   跑完后 relay 设了 no_create —— **不会再有下一个 response**,于是 tool/card 这两个 part
    //   永远等不到落库时机 → 刷新后卡片消失(用户实测)。容器是内容的唯一来源,由它自己触发。
    try { if (RC.turnCard.onChange) RC.turnCard.onChange(tid); } catch (e) {}
    return el;
  }

  // 流式文字:唯一允许"就地更新"的 part(不变式②的例外)。response 结束调 freezeDraft。
  function draftText(tid, text) {
    var t = _turns[tid] || open(tid);
    if (!t) return;
    if (!t.draft) {
      t.draft = { kind: 'text', text: '', seq: t.parts.length };
      t.parts.push(t.draft);
      t.draft._el = document.createElement('div');
      t.draft._el.className = 'rc-part rc-part-text';
      t.bd.appendChild(t.draft._el);
    }
    t.draft.text = text;
    _md(t.draft._el, text);
    _scroll();
  }
  function freezeDraft(tid) {
    var t = _turns[tid];
    if (!t || !t.draft) return;
    if (!(t.draft.text || '').trim()) {   // 空 draft:撤掉,别在历史里留空块
      try { t.draft._el.remove(); } catch (e) {}
      t.parts.splice(t.parts.indexOf(t.draft), 1);
    } else {
      delete t.draft._el;
    }
    t.draft = null;
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
    var s = _statusEl(t);
    if (!text) { s.hidden = true; return; }
    s.hidden = false;
    s.className = 'rc-turn-status' + (done ? ' done' : '');
    s.innerHTML = (done ? '✓ ' : '<span class="vc-spin vc-spin-s"></span> ') + _esc(text);
  }
  // busy = 某个工具在跑:标题=工具名 + 状态行"处理中"(兼容既有调用点;进度一律落在标题区,不进 body)。
  function busy(tid, label) {
    var t = _turns[tid] || open(tid);
    if (!t) return;
    _ensureHead(t, label);
    status(tid, '处理中', false);
  }
  function idle(tid) { var t = _turns[tid]; if (t && t.statusEl) t.statusEl.hidden = true; }

  // ── 历史回放:**同一个 renderPart**(不变式①)────────────────────────────
  function renderTurn(tid, parts) {
    var t = open(tid);
    if (!t) return null;
    (parts || []).forEach(function (p) {
      t.parts.push(p);
      renderPart(t, p);
    });
    return t.el;
  }

  function setTaskId(tid, taskId) { var t = _turns[tid]; if (t) t.taskId = taskId; }
  // CLI 任务:运行中就把卡头设成任务名(不必等结束的 tool part)——复用同一个 _ensureHead,
  // 卡片长这样:[卡头=任务名][body 增量渲结果][流程按钮]。
  function title(tid, label) { var t = _turns[tid] || open(tid); if (t) _ensureHead(t, label); }
  function partsOf(tid) {
    var t = _turns[tid];
    if (!t) return [];
    return t.parts.map(function (p) {
      var o = {}; for (var k in p) { if (k !== '_el') o[k] = p[k]; }
      return o;
    });
  }
  function reset() { _turns = {}; _cur = null; }

  RC.turnCard = {
    open: open, addPart: addPart, draftText: draftText, freezeDraft: freezeDraft, busy: busy, idle: idle,
    renderTurn: renderTurn, partsOf: partsOf, reset: reset, setTaskId: setTaskId, title: title, status: status,
    current: function () { return _cur; },
    has: function (tid) { return !!_turns[tid]; },
  };
})();
