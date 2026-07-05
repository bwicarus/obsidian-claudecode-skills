/* rc-assistant.js — 统一控制层(window.RC.assistant):可移植的「阅读助手 UI 增量」共享层
 *
 * 目的:把 PDF 阅读器 reader.src/25-assistant.js 里两块**纯展示、跟阅读器无耦合**的体验抽出来,
 *       让 EPUB(或将来别的 reader)的简易助手叠加上去 —— 而不去碰各自的 sendChat / 流式 / state。
 *       只做「AI 回答后的追问 chips」+「用户气泡下的上下文卡片」这两件可移植的事;
 *       流式、断线恢复、agentic 工具循环那些跟具体 reader 强绑定的,各 reader 自己留着,本模块不碰。
 *
 * 设计原则(跟 rc-md / rc-result / rc-settings 等一致):
 *   · 自包含:自己注入一次 <style>(scoped 到 .rc-fu* / .rc-asst-ctx*),不依赖任何模板 CSS;
 *   · opts 驱动:不假设 DOM 结构、不读全局变量,全部行为由调用方通过参数/回调传入;
 *   · 用共享层 RC.esc / RC.md / RC.typeset / RC.toast(缺了有内联兜底),被 onclick 调的挂 window.(本模块无);
 *   · 幂等:重复 load 不重复注册。
 *
 * API
 *   RC.assistant.splitFollowups(text) -> { text, followups:[string] }
 *       从 AI 回答里剥离 [[FOLLOWUP]]q1|q2|q3[[/FOLLOWUP]] 追问建议(容忍流式中途未闭合)。
 *       text = 去掉标记后的正文;followups = 至多 4 条问题。后端 prompt 需被告知输出该标记(见集成说明)。
 *
 *   RC.assistant.renderFollowups(afterEl, followups, onPick) -> void
 *       在 afterEl(通常是 AI 回答气泡)末尾追加一排「继续问」chip;点 chip 调 onPick(原始问题文本)。
 *       chip 文本里的 $..$ 会被 MathJax 渲成公式(点击仍发原始文本)。followups 空则 no-op。
 *
 *   RC.assistant.contextCard(items) -> HTMLElement | null
 *       生成「助手当前能看到啥」的上下文卡片(挂在用户气泡下)。items = [{ text, title?, onClick?, formula? }]。
 *       formula:true 时 text 当 LaTeX(去 $)走 MathJax;onClick 给则该行可点(如跳转)。空/全空 → 返回 null。
 *
 *   RC.assistant.openModelSettings(focusAction?) -> void
 *       ⚙ AI 模型设置面板(按功能配 后端/型号/深度)。**逐字照搬** PDF reader.src/25-assistant.js 的同名实现,
 *       命中**同一组**服务端端点(/api/assistant/action-pref[s])+ 同一组 action 名 → EPUB 改了模型,
 *       EPUB 后端 epub_assistant._eagent_run(经 assistant._resolve(action,uid))立刻读得到。
 *       focusAction 给则进面板后定位到对应任务卡(从 trace 某步 ⚙ 进来时用)。
 *
 *   RC.assistant.renderModelSettings(container, focusAction?) -> void
 *       同一份配置表渲进任意容器(openModelSettings 浮层 与 rc-settings AI tab 内嵌 共用,
 *       2026-07 界面收口:设置面板 AI tab 直接内嵌配置表,不再放旧 model/effort 下拉)。
 */
(function () {
  'use strict';
  var RC = (window.RC = window.RC || {});
  if (RC.assistant) return;

  function esc(s) {
    if (RC.esc) return RC.esc(s);
    var d = document.createElement('div'); d.textContent = (s == null ? '' : String(s)); return d.innerHTML;
  }
  function typeset(el) {
    try { if (RC.typeset) { RC.typeset(el); return; } } catch (e) {}
    try { if (el && window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([el]).catch(function () {}); } catch (e) {}
  }
  // 共享 toast 兜底:openModelSettings/_setActionPref 里的 `typeof _toast === 'function'` 由它满足(走 RC.toast)。
  function _toast(m) { try { if (RC.toast) RC.toast(m); } catch (e) {} }

  // 一次性注入样式(照搬 25-assistant.js 的 .asst-followups / .asst-ctx-card 视觉,改 scoped 前缀防撞;
  //  另把 ⚙ 模型设置面板的 .ams-* 样式**逐字照搬**进来 —— 这套 class 名与 PDF 完全一致,跟 EPUB 现有 .ep-* 不撞)
  (function injectCss() {
    if (document.getElementById('rc-assistant-css')) return;
    var css = document.createElement('style'); css.id = 'rc-assistant-css';
    css.textContent =
      '.rc-fu-box{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}' +
      '.rc-fu{background:#13233f;border:1px solid #2a3a63;color:#bcd0ff;border-radius:13px;padding:5px 11px;' +
      'font-size:13px;cursor:pointer;text-align:left;-webkit-tap-highlight-color:transparent;font-family:inherit;line-height:1.4}' +
      '.rc-fu:active{background:#1d3358}' +
      '.rc-asst-ctx{margin-top:7px;display:flex;flex-direction:column;gap:5px}' +
      '.rc-asst-ctx-row{font-size:12px;color:#dbe7ff;background:rgba(255,255,255,.10);border-left:2px solid rgba(255,255,255,.45);' +
      'border-radius:4px;padding:3px 8px;line-height:1.4;word-break:break-word}' +
      '.rc-asst-ctx-row.clk{cursor:pointer}.rc-asst-ctx-row.clk:active{background:rgba(255,255,255,.2)}' +
      '.rc-asst-ctx-row.fml{text-align:center;white-space:normal;overflow-x:auto;color:#eaf2ff}' +
      // ── ⚙ 模型设置面板(每任务 后端/型号/深度)── 逐字照搬 25-assistant.js:122-136
      '.ams-mask{position:fixed;inset:0;z-index:130;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;padding:16px}' +
      '.ams-box{background:#0d1426;border:1px solid #2a3a63;border-radius:14px;max-width:440px;width:100%;max-height:86vh;overflow-y:auto;padding:14px 14px 16px;box-shadow:0 12px 40px rgba(0,0,0,.6)}' +
      '.ams-h{font-size:15px;color:#dbe7ff;font-weight:600;display:flex;align-items:center;justify-content:space-between;margin-bottom:3px}' +
      '.ams-x{background:none;border:none;color:#7c93c4;font-size:20px;cursor:pointer;padding:0 4px;line-height:1}' +
      '.ams-sub{font-size:11px;color:#6b7da0;margin-bottom:10px;line-height:1.5}' +
      '.ams-task{background:#0a1322;border:1px solid #243152;border-radius:10px;padding:10px;margin-bottom:9px}' +
      '.ams-tname{font-size:13px;color:#cdd9f2;font-weight:600;margin-bottom:2px}' +
      '.ams-tdef{font-size:11px;color:#6b7da0;margin-bottom:7px}' +
      '.ams-row{display:flex;gap:6px;flex-wrap:wrap;align-items:center}' +
      '.ams-sel{background:#0d1426;border:1px solid #2a3a63;color:#dbe7ff;border-radius:7px;padding:5px 6px;font-size:12px;flex:1 1 28%;min-width:0}' +
      '.ams-sel:disabled{opacity:.45}' +
      '.ams-rst{background:#1a2233;border:1px solid #2a3a63;color:#9fb4e0;border-radius:7px;padding:5px 9px;font-size:12px;cursor:pointer;flex:none}' +
      '.ams-rst:active{background:#222d44}' +
      '.ams-cur{font-size:11px;color:#7c93c4;margin-top:6px}' +
      '.ams-note{font-size:11px;color:#bfae72;background:#221d10;border:1px solid #463a18;border-radius:7px;padding:6px 9px;margin-top:4px;line-height:1.5}';
    document.head.appendChild(css);
  })();

  function splitFollowups(text) {
    var fu = [];
    var push = function (body) {
      String(body || '').split(/[|\n]+/).forEach(function (q) {
        q = q.trim().replace(/^[\-·•\d\.\s]+/, '');
        if (q) fu.push(q);
      });
    };
    var clean = String(text == null ? '' : text)
      .replace(/\[\[FOLLOWUP\]\]([\s\S]*?)\[\[\/FOLLOWUP\]\]/g, function (m, body) { push(body); return ''; });
    var open = clean.indexOf('[[FOLLOWUP]]');   // 模型常漏结束标记 → 未闭合:从 [[FOLLOWUP]] 到结尾都当追问
    if (open >= 0) { push(clean.slice(open + 12).replace(/\[\[\/?FOLLOWUP\]\]/g, '')); clean = clean.slice(0, open); }
    return { text: clean.trim(), followups: fu.slice(0, 4) };
  }

  function renderFollowups(afterEl, followups, onPick) {
    if (!afterEl || !followups || !followups.length) return;
    var box = document.createElement('div'); box.className = 'rc-fu-box';
    followups.forEach(function (q) {
      var b = document.createElement('button'); b.className = 'rc-fu';
      b.textContent = q;   // 纯文本占位;下面让 MathJax 渲 chip 里的 $..$(点击仍发原始 q)
      b.addEventListener('click', function () { try { if (typeof onPick === 'function') onPick(q); } catch (e) {} });
      box.appendChild(b);
    });
    afterEl.appendChild(box);
    typeset(box);
  }

  function contextCard(items) {
    if (!items || !items.length) return null;
    var rows = items.filter(function (it) { return it && (it.text != null) && String(it.text).trim() !== ''; });
    if (!rows.length) return null;
    var card = document.createElement('div'); card.className = 'rc-asst-ctx';
    rows.forEach(function (it) {
      var row = document.createElement('div'); row.className = 'rc-asst-ctx-row';
      var t = String(it.text);
      if (it.formula && /^\$\$?[\s\S]+\$\$?$/.test(t.trim())) {   // 公式行:走 MathJax,不显示裸 LaTeX
        row.classList.add('fml');
        var raw = t.trim().replace(/^\$\$?/, '').replace(/\$\$?$/, '');
        var block = /^\$\$/.test(t.trim()) || /\\begin\{|\\\\/.test(raw);
        row.textContent = block ? ('\\[' + raw + '\\]') : ('\\(' + raw + '\\)');
        typeset(row);
      } else {
        row.textContent = (t.length > 96 ? (t.slice(0, 64) + '…' + t.slice(-24)) : t);
      }
      if (it.title) row.title = it.title;
      if (typeof it.onClick === 'function') {
        row.classList.add('clk');
        row.addEventListener('click', function () { try { it.onClick(); } catch (e) {} });
      }
      card.appendChild(row);
    });
    return card;
  }

  // ════════════════════════════════════════════════════════════════════════
  // ⚙ AI 模型设置面板:**逐字照搬** PDF reader.src/25-assistant.js 的
  //   _setActionPref / _DEPTH_LABEL / _BACKEND_LABEL / _msMkSel / _buildMsTask / openModelSettings。
  //   命中同一组服务端端点(/api/assistant/action-pref[s])+ 同一组 action 名 →
  //   EPUB 改模型后,EPUB 后端 epub_assistant._eagent_run(经 assistant._resolve(action,uid))读得到。
  //   存储是服务端 state/assistant-action-prefs.json(按 uid+action),不是 localStorage;PDF/EPUB 共用一份。
  // ════════════════════════════════════════════════════════════════════════

  // 给某动作存 (后端/型号/深度) 预设;backend 传 '' 清除回默认。跟感叹号「更强重答」共用此预设。
  function _setActionPref(action, backend, variant, depth, okMsg) {
    return fetch('/api/assistant/action-pref', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: action, backend: backend || '', variant: variant || '', depth: depth || '' }) })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (d && d.ok && typeof _toast === 'function') _toast(okMsg || '已设置'); return d; })
      .catch(function () {});
  }
  // ── ⚙ 模型设置面板:列出各 AI 任务,每个可设 后端/型号/深度 ──
  var _DEPTH_LABEL = { auto: '自动(按问题)', low: 'low(快)', medium: 'medium', high: 'high(深)', xhigh: 'xhigh', max: 'max(最强)', none: '不思考', think: '思考' };
  var _BACKEND_LABEL = { claude: 'Claude', gemini: 'Gemini' };
  function _msMkSel(opts, val, labels, disabledSet) {
    var s = document.createElement('select'); s.className = 'ams-sel';
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
    var card = document.createElement('div'); card.className = 'ams-task';
    var nm = document.createElement('div'); nm.className = 'ams-tname'; nm.textContent = names[action] || action;
    var df = document.createElement('div'); df.className = 'ams-tdef';
    df.textContent = '默认:' + (_BACKEND_LABEL[def.backend] || def.backend) + ' · ' + (cat.variant_short[def.variant] || def.variant) + ' · ' + (_DEPTH_LABEL[def.depth] || def.depth);
    var row = document.createElement('div'); row.className = 'ams-row';
    var gstat = cat.gemini_status || {};
    function _fmtRetry(s) { if (!s) return ''; if (s < 90) return s + '秒'; if (s < 5400) return Math.round(s / 60) + '分'; return Math.round(s / 3600) + '小时'; }
    var varLabels = {}; (cat.variants.gemini || []).forEach(function (v) {
      var st = gstat[v], tag = ' · 免费';
      if (st && st.paid_only) { tag = ' · 💰仅付费'; }   // ListModels 证实只在付费清单(如 3.1-pro):恒计费,非临时状态
      else if (st && st.free === false) { tag = ' · 付费(' + (st.reason || '免费不可用') + (st.retry ? ',还需' + _fmtRetry(st.retry) : '') + ')'; }
      varLabels[v] = (cat.variant_short[v] || v) + tag;
    });
    var lockB = locked[action] || [];
    // 型号清单:用户存的 variant 带 '@paid'(直连付费,来自「以后直接用付费」一键按钮)不在 ListModels
    // 清单里 → 插到对应裸型号后面并给可读 label,否则 select 显示成第一项、看不出已设直连付费。
    function _vlist(backend, val) {
      var l = (cat.variants[backend] || []).slice();
      if (val && l.indexOf(val) < 0 && /@paid$/.test(String(val))) {
        var bare = String(val).replace(/@paid$/, '');
        var i = l.indexOf(bare);
        l.splice(i >= 0 ? i + 1 : l.length, 0, val);
        varLabels[val] = (cat.variant_short[bare] || bare) + ' · 💰直连付费';
      }
      return l;
    }
    var selB = _msMkSel(cat.backends, cur.backend, _BACKEND_LABEL, lockB);
    var selV = _msMkSel(_vlist(cur.backend, cur.variant), cur.variant, varLabels);
    var selD = _msMkSel(cat.depths[cur.backend] || [], cur.depth, _DEPTH_LABEL);
    function save() {
      _setActionPref(action, selB.value, selV.value, selD.value,
        '「' + (names[action] || action) + '」已设为 ' + (cat.variant_short[selV.value] || selV.value) + '·' + (_DEPTH_LABEL[selD.value] || selD.value));
    }
    function rebindVD(backend, keepVal, vv, dv) {
      var nv = _msMkSel(_vlist(backend, keepVal ? vv : null), keepVal ? vv : (cat.variants[backend] || [])[0], varLabels);
      var nd = _msMkSel(cat.depths[backend] || [], keepVal ? dv : (cat.depths[backend] || [])[0], _DEPTH_LABEL);
      row.replaceChild(nv, selV); row.replaceChild(nd, selD); selV = nv; selD = nd;
      selV.addEventListener('change', save); selD.addEventListener('change', save);
    }
    selB.addEventListener('change', function () { rebindVD(selB.value, false); save(); });
    selV.addEventListener('change', save); selD.addEventListener('change', save);
    var rst = document.createElement('button'); rst.className = 'ams-rst'; rst.textContent = '默认';
    rst.addEventListener('click', function () {
      _setActionPref(action, '', '', '', '「' + (names[action] || action) + '」恢复默认');
      selB.value = def.backend; rebindVD(def.backend, true, def.variant, def.depth);
    });
    row.appendChild(selB); row.appendChild(selV); row.appendChild(selD); row.appendChild(rst);
    card.appendChild(nm); card.appendChild(df); card.appendChild(row);
    if (lockB.indexOf('gemini') >= 0) {
      var lk = document.createElement('div'); lk.className = 'ams-cur'; lk.textContent = '(根 agent 切 Gemini 需二期工具循环,暂锁)';
      card.appendChild(lk);
    }
    return card;
  }
  // 面板主体渲染(可复用):把「每任务 后端/型号/深度」配置卡渲进任意容器。两处共用同一实现:
  //   ① openModelSettings 的 .ams-mask 浮层(助手侧边栏 ⚙ / 感叹号步骤 ⚙);
  //   ② rc-settings 总设置面板 AI tab 的**内嵌**模式(2026-07 收口:AI tab 不再放旧 model/effort
  //      下拉 + 跳转按钮,直接内嵌这份配置表)。数据/保存都走同组服务端端点,改完即时生效。
  function renderModelSettings(container, focusAction) {
    if (!container) return;
    container.innerHTML = '<div class="ams-sub">加载中…</div>';
    fetch('/api/assistant/action-prefs').then(function (r) { return r.json(); }).then(function (d) {
      if (!d || !d.ok) { container.innerHTML = '<div class="ams-sub">拉取设置失败</div>'; return; }
      container.innerHTML = '';
      var sub = document.createElement('div'); sub.className = 'ams-sub';
      sub.textContent = '每个任务可单独设 后端/型号/深度,改完即时生效(服务端保存,全设备生效)。跟感叹号「更强重答」共用同一套预设。';
      container.appendChild(sub);
      var _focusCard = null;
      function _renderActs(list) {
        list.forEach(function (a) {
          var ai = d.actions[a]; if (!ai) return;
          var c = _buildMsTask(a, { pref: ai.pref, def: ai.default }, d.catalog, d.names, d.locked || {});
          if (a === focusAction) _focusCard = c;   // 从某步⚙进来 → 定位到对应任务卡
          container.appendChild(c);
        });
      }
      _renderActs(['orchestrator', 'summarize', 'vision']);
      // 阅读器其它 AI 入口(解释/翻译/字典/语法),跟助手共用同一套脱壳 Claude + Gemini 双后端预设
      var _rh = document.createElement('div'); _rh.className = 'ams-sub';
      _rh.style.cssText = 'margin-top:12px;font-weight:600;color:#9fc0ff;';
      _rh.textContent = '— 阅读器其它 AI —';
      container.appendChild(_rh);
      _renderActs(['explain', 'translate', 'dict', 'grammar']);   // 服务端没有的 action 自动跳过(向后兼容)
      var note = document.createElement('div'); note.className = 'ams-note';
      note.textContent = '标「免费」= 免费档支持该型号;但免费是**共享算力**,高峰常过载(503)或限流时会自动落付费保不中断——'
        + '此时这里会标「付费(过载/限流)」、感叹号里也显付费。「💰仅付费」= 该型号免费档没有(如 3.1-pro),'
        + '选它每次调用都按量计费。flash 高峰过载较多;想更稳的免费可试 flash-lite 系。';
      container.appendChild(note);
      if (_focusCard) { try { _focusCard.style.outline = '2px solid #6aa3ff'; _focusCard.style.borderRadius = '8px'; _focusCard.scrollIntoView({ block: 'center' }); } catch (_) {} }
    }).catch(function () { container.innerHTML = '<div class="ams-sub">拉取设置失败</div>'; });
  }
  function openModelSettings(focusAction) {
    var mask = document.createElement('div'); mask.className = 'ams-mask';
    mask.addEventListener('click', function (e) { if (e.target === mask) mask.remove(); });
    var box = document.createElement('div'); box.className = 'ams-box';
    var h = document.createElement('div'); h.className = 'ams-h';
    var ht = document.createElement('span'); ht.textContent = '⚙ AI 模型设置';
    var x = document.createElement('button'); x.className = 'ams-x'; x.textContent = '×';
    x.addEventListener('click', function () { mask.remove(); });
    h.appendChild(ht); h.appendChild(x); box.appendChild(h);
    var body = document.createElement('div');
    box.appendChild(body);
    mask.appendChild(box); document.body.appendChild(mask);
    renderModelSettings(body, focusAction);
  }
  try { window.openModelSettings = openModelSettings; } catch (_) {}   // 供 EPUB 总设置面板 / inline onclick 调起

  // ── 「免费 Gemini 受限→本次已用付费」提示条(SSE 'gemini-paid' 事件的渲染器,PDF/EPUB 共用)──
  //   data = 后端 _paid_fallback_note():{text, action, variant, paid_variant, depth}。
  //   返回元素由调用方 append 进对话流;**同 session 只提示一次**(内存标志,防每条消息刷屏)。
  //   按钮「以后直接用付费」= 调现有 action-pref 端点把该 action 的 variant 存成 '<型号>@paid'
  //   (服务端 _gemini_keys 对它跳过 free key,面板显示「💰直连付费」)。
  var _paidNoted = false, _recoverNoted = false;
  function paidNotice(data) {
    if (!data) return null;
    var rec = !!data.recovered;   // 恢复变体:「✅免费额度已恢复,已自动切回免费」(绿色,不带按钮;后端 _paid_recover_check 发)
    if (rec ? _recoverNoted : _paidNoted) return null;   // 各自同 session 只提示一次
    if (rec) _recoverNoted = true; else _paidNoted = true;
    var box = document.createElement('div'); box.className = 'rc-paid-note';
    box.style.cssText = 'align-self:center;max-width:96%;'
      + (rec ? 'background:#0f2a1a;border:1px solid #2a5a3a;color:#8ae7b0;' : 'background:#2a2410;border:1px solid #5a4a18;color:#e7d28a;')
      + 'font-size:12px;padding:6px 10px;border-radius:9px;line-height:1.55;display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:4px 0';
    var tx = document.createElement('span');
    tx.textContent = rec ? (data.text || '✅ Gemini 免费额度已恢复,已自动切回免费。')
                         : ('⚠️ ' + (data.text || '免费 Gemini 额度受限,本次已使用付费档。'));
    box.appendChild(tx);
    if (!rec && data.action && data.paid_variant) {
      var b = document.createElement('button');
      b.textContent = '以后直接用付费';
      b.title = '把「' + data.action + '」的型号设为直连付费(不再先试免费档);之后可在 ⚙ 模型设置里改回';
      b.style.cssText = 'background:#3d3210;border:1px solid #6e5a1e;color:#ffe9a8;border-radius:7px;padding:2px 10px;font-size:12px;cursor:pointer';
      b.addEventListener('click', function () {
        b.disabled = true;
        _setActionPref(data.action, 'gemini', data.paid_variant, data.depth || 'think', '已设为直连付费 Gemini(不再先试免费档)')
          .then(function (d) {
            if (d && d.ok) { b.textContent = '✓ 已设置'; }
            else { b.textContent = '以后直接用付费'; b.disabled = false; if (typeof _toast === 'function') _toast('设置失败,稍后再试'); }
          });
      });
      box.appendChild(b);
    }
    return box;
  }

  RC.assistant = {
    splitFollowups: splitFollowups,
    renderFollowups: renderFollowups,
    contextCard: contextCard,
    openModelSettings: openModelSettings,
    renderModelSettings: renderModelSettings,
    paidNotice: paidNotice
  };
})();
