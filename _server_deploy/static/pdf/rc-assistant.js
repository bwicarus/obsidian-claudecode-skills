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

  // ── 共享快捷按钮栏(PDF #asst-quick / EPUB #ep-asst-quick 单一来源;根治两边按钮分叉)──
  //   统一为 [🧩 本X知识点(data-send) · 🗑 清空 · ⚙ 模型] + 媒体行(rcBuildMediaRow「配图/视频」偏好开关)。
  //   「总结本页/本页生词」历史按钮不再纳入(已从两边移除)。点击处理仍走各 reader 在容器上的既有委托
  //   (data-send 直接发 / data-q=clear 清空 / data-q=models 开模型面板),故只产 markup、不绑事件 → 零耦合。
  //   假定容器为空;幂等(__quickBuilt 去重);容器需已存在。收藏夹整集按钮由 EPUB 在此之后自行 prepend,不受影响。
  window.rcBuildQuickBar = function (container, opts) {
    if (!container || container.__quickBuilt) return;
    container.__quickBuilt = 1;
    opts = opts || {};
    container.insertAdjacentHTML('beforeend',
      '<button data-q="clear">🗑 清空</button>' +
      '<button data-q="models">⚙ 模型</button>');
    try { if (window.rcBuildMediaRow) window.rcBuildMediaRow(container); } catch (e) {}   // 「配图/视频」偏好开关并入本栏
  };

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
      '.ams-note{font-size:11px;color:#bfae72;background:#221d10;border:1px solid #463a18;border-radius:7px;padding:6px 9px;margin-top:4px;line-height:1.5}' +
      // 预设条:一排 chips,点=整包应用,长按/右键=删,＋=存当前
      '.ams-profiles{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}' +
      '.ams-prof{background:#14203a;border:1px solid #2a3a63;color:#cdd9f2;border-radius:14px;padding:4px 12px;font-size:12px;cursor:pointer}' +
      '.ams-prof:active{background:#1e2c4d}' +
      '.ams-prof.on{background:#2b4a8f;border-color:#5b82d8;color:#fff;font-weight:600}' +   // 当前应用中的预设(单项改过即熄灭=已偏离)
      '.ams-prof-add{background:none;border:1px dashed #2a3a63;color:#7c93c4}';
    document.head.appendChild(css);
  })();

  // 剥语气标记 [语气:XX](AI 按 voice prompt 输出,驱动 2.0 朗读引擎情绪;显示与朗读都不要它)。
  // v3-⑱c 转折升级:标签可出现在**任意位置**(情绪转折点)→ 全局剥;尾部未闭合半截(流式撕裂)暂截住等下一增量。
  function stripMoodTag(t) {
    t = String(t || '');
    var mood = null;
    var text = t.replace(/[\[【]语气[::]\s*([^\]】]{1,12})[\]】]\s*/g, function (_, m) { mood = m.trim(); return ''; });
    text = text.replace(/[\[【]语气?[::]?[^\]】]{0,12}$/, '');   // 尾部撕裂 hold
    return { text: text, mood: mood };
  }
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
    var rows = items.filter(function (it) { return it && (it.image || ((it.text != null) && String(it.text).trim() !== '')); });
    if (!rows.length) return null;
    var card = document.createElement('div'); card.className = 'rc-asst-ctx';
    rows.forEach(function (it) {
      if (it.image) {   // 图缩略图行(带入图 / 便签合成图回放):有 image(url 或 data_url)就渲小图,点开走 onClick(如 lightbox)
        var irow = document.createElement('div'); irow.className = 'rc-asst-ctx-row rc-asst-ctx-img';
        var im = document.createElement('img'); im.src = it.image; im.alt = it.text || '';
        im.style.cssText = 'max-width:120px;max-height:80px;border-radius:6px;object-fit:cover;display:block' + (typeof it.onClick === 'function' ? ';cursor:zoom-in' : '');
        if (typeof it.onClick === 'function') im.addEventListener('click', function () { try { it.onClick(); } catch (e) {} });
        irow.appendChild(im);
        if (it.text) { var cap = document.createElement('span'); cap.textContent = it.text; cap.style.cssText = 'margin-left:6px;font-size:12px;opacity:.8;vertical-align:top'; irow.appendChild(cap); }
        card.appendChild(irow);
        return;
      }
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
  var _BACKEND_LABEL = { claude: 'Claude', gemini: 'Gemini', codex: 'Codex(GPT)' };
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
    var _bl = (cat.backends_by_action && cat.backends_by_action[action]) || cat.backends;   // 该任务真正支持的后端(编排不接 Codex、语音深度只走 Claude——选不了就不会被静默降级坑)
    var selB = _msMkSel(_bl, cur.backend, _BACKEND_LABEL, lockB);
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
  // ── 🎙 语音通话(豆包 S2S)设置(v3-⑮):写凭证白名单字段(服务端保存全设备生效);
  //    通话中改 → RC.voicecall.pushCfg() 让 relay UpdateConfig 热切音色/语速。──
  var _VC_SPK = [
    ['zh_female_vv_jupiter_bigtts', 'vv · 活泼灵动女声(默认,支持方言)'],
    ['zh_female_xiaohe_jupiter_bigtts', 'xiaohe · 甜美女声(台湾腔)'],
    ['zh_male_yunzhou_jupiter_bigtts', 'yunzhou · 清爽沉稳男声'],
    ['zh_male_xiaotian_jupiter_bigtts', 'xiaotian · 清爽磁性男声'],
    ['en_male_tim_uranus_bigtts', 'Tim · 美式英语男声'],
    ['en_female_dacey_uranus_bigtts', 'Dacey · 美式英语女声'],
    ['en_female_stokie_uranus_bigtts', 'Stokie · 美式英语女声']];
  var _VC_DIA = [['', '标准(无方言)'], ['dongbei', '东北话'], ['sichuan', '四川话'], ['shaanxi', '陕西话']];
  var _VC_TTS_SPK = [   // 朗读音色:uranus 系=2.0(单向引擎,支持下面的语气指令);moon 系=1.0(双向引擎)
    ['zh_female_vv_uranus_bigtts', 'vv · 2.0(推荐,支持语气指令)'],
    ['zh_female_shuangkuaisisi_uranus_bigtts', '爽快思思 · 2.0'],
    ['zh_male_yuanboxiaoshu_uranus_bigtts', '渊博小叔 · 2.0(讲解风)'],
    ['zh_male_shenyeboke_uranus_bigtts', '深夜播客 · 2.0'],
    ['zh_female_wenrouxiaoya_uranus_bigtts', '温柔小雅 · 2.0'],
    ['zh_male_ruyaqingnian_uranus_bigtts', '儒雅青年 · 2.0'],
    ['zh_female_qinqienv_uranus_bigtts', '亲切女声 · 2.0'],
    ['zh_female_shuangkuaisisi_moon_bigtts', '爽快思思(1.0,中英双语)'],
    ['zh_male_wennuanahu_moon_bigtts', '温暖阿虎(中英双语)'],
    ['zh_male_shaonianzixin_moon_bigtts', '少年梓辛(中英双语)'],
    ['zh_male_yuanboxiaoshu_moon_bigtts', '渊博小叔(讲解风)'],
    ['zh_male_jieshuoxiaoming_moon_bigtts', '解说小明'],
    ['zh_male_shenyeboke_moon_bigtts', '深夜播客'],
    ['zh_female_qinqienvsheng_moon_bigtts', '亲切女声'],
    ['zh_female_linjianvhai_moon_bigtts', '邻家女孩'],
    ['zh_female_kailangjiejie_moon_bigtts', '开朗姐姐'],
    ['zh_female_gaolengyujie_moon_bigtts', '高冷御姐'],
    ['zh_female_wanwanxiaohe_moon_bigtts', '湾湾小何(台湾腔)'],
    ['en_female_lauren_moon_bigtts', 'Lauren(纯英语)']];
  function _renderVoiceCfg(container) {
    container.querySelectorAll('.ams-voice-part').forEach(function (n) { n.remove(); });   // 切引擎重绘:先清旧区块
    var h = document.createElement('div'); h.className = 'ams-sub ams-voice-part';
    h.style.cssText = 'margin-top:12px;font-weight:600;color:#9fc0ff;';
    h.textContent = '— 🎙 语音通话 —';
    container.appendChild(h);
    var card = document.createElement('div'); card.className = 'ams-task ams-voice-part';
    card.innerHTML = '<div class="ams-tdef">加载中…</div>';
    container.appendChild(card);
    fetch('/api/assistant/voice-config').then(function (r) { return r.json(); }).then(function (d) {
      if (!d || !d.ok) { card.innerHTML = '<div class="ams-tdef">拉取语音设置失败</div>'; return; }
      var c = d.cfg || {};
      function esc2(x) { var e = document.createElement('div'); e.textContent = String(x == null ? '' : x); return e.innerHTML; }
      // ㉖b:按通话引擎分组渲染——选 GPT 就藏豆包 S2S 专属项、显示 GPT 专属项;朗读/ASR 与引擎无关恒显。
      var isOA = (c.rt_engine === 'openai' || c.rt_engine === 'openai_rtc');
      var H = '<div class="ams-row" style="margin-bottom:7px"><select class="ams-sel" data-k="rt_engine" style="flex:1 1 100%">' +
          '<option value=""' + (!c.rt_engine ? ' selected' : '') + '>通话引擎:豆包 S2S(默认)</option>' +
          '<option value="openai_rtc"' + (c.rt_engine === 'openai_rtc' ? ' selected' : '') + '>通话引擎:GPT Realtime(WebRTC·推荐:外放无回声+可随时插话)</option>' +
          '<option value="openai"' + (c.rt_engine === 'openai' ? ' selected' : '') + '>通话引擎:GPT Realtime(WebSocket·外放半双工)</option>' +
          '<option value="grok"' + (c.rt_engine === 'grok' ? ' selected' : '') + '>通话引擎:Grok Voice(WebSocket·耳机推荐)</option>' +
        '</select></div>';
      if (c.rt_engine === 'grok') {   // ── 94 Grok 专属:音色 + 能力边界说明 ──
        var _GKV = ['eve', 'ara', 'rex', 'sal', 'leo'];
        H += '<div class="ams-row" style="margin-bottom:7px"><select class="ams-sel" data-k="rt_grok_voice" style="flex:1 1 100%">' +
          _GKV.map(function (v) { return '<option value="' + v + '"' + ((c.rt_grok_voice || 'eve') === v ? ' selected' : '') + '>Grok 音色:' + v + (v === 'eve' ? '(默认)' : '') + '</option>'; }).join('') +
          '</select></div>' +
          '<div class="ams-row" style="margin-bottom:7px"><select class="ams-sel" data-k="rt_grok_vad" style="flex:1 1 100%">' +
            '<option value=""' + (!c.rt_grok_vad ? ' selected' : '') + '>轮次判定:本地 VAD(省钱:静默零上传;应答≈0.9s)</option>' +
            '<option value="server"' + (c.rt_grok_vad === 'server' ? ' selected' : '') + '>轮次判定:服务端 VAD(实验:官方打断手感;全程计费 $3/小时)</option>' +
          '</select></div>' +
          '<div class="ams-tdef">Grok Voice:$0.05/分钟(音频时长) · 首音频≈1s · 中日文可用 · 工具可用 · 断线30分钟内自动续接记忆。边界:恒纯语音、不支持看图(走文字转述)。</div>';
      }
      if (isOA) {   // ── GPT Realtime 专属(2.1 可调项全暴露)──
        var _RTM = [['gpt-realtime-2.1-mini', '模型:2.1 mini(推荐,音频费≈豆包一半)'], ['gpt-realtime-2.1', '模型:2.1 完整版(更聪明,约 3 倍贵)']];
        var _RTV = ['marin', 'cedar', 'alloy', 'ash', 'ballad', 'coral', 'echo', 'sage', 'shimmer', 'verse'];
        var _RTL = [['', '语言:自动(跟随你说话的语言)'], ['zh', '语言:中文'], ['ja', '语言:日本語'], ['en', '语言:English']];
        var _RTE = [['low', '思考强度:low(推荐,低延迟)'], ['minimal', '思考强度:minimal(最快)'], ['medium', '思考强度:medium'], ['high', '思考强度:high(慢且贵)']];
        var _RTG = [['auto', '接话灵敏度:自动(推荐)'], ['low', '接话灵敏度:慢热(多等你想完)'], ['medium', '接话灵敏度:中等'], ['high', '接话灵敏度:急性子(尽快接话)']];
        H += '<div class="ams-row" style="margin-bottom:7px"><select class="ams-sel" data-k="rt_model" style="flex:1 1 100%">' +
            _RTM.map(function (o) { return '<option value="' + o[0] + '"' + ((c.rt_model || 'gpt-realtime-2.1-mini') === o[0] ? ' selected' : '') + '>' + o[1] + '</option>'; }).join('') +
          '</select></div>' +
          '<div class="ams-row" style="margin-bottom:7px"><select class="ams-sel" data-k="rt_voice" style="flex:1 1 100%">' +
            _RTV.map(function (v) { return '<option value="' + v + '"' + ((c.rt_voice || 'marin') === v ? ' selected' : '') + '>GPT 音色:' + v + (v === 'marin' || v === 'cedar' ? '(官方推荐)' : '') + '</option>'; }).join('') +
          '</select></div>' +
          '<label class="ams-cur">通话语速(下次通话生效)<span class="vcv-rts" style="float:right">' + (c.rt_speed || 1) + '×</span></label>' +
          '<input type="range" min="0.5" max="1.5" step="0.05" value="' + (c.rt_speed || 1) + '" data-k="rt_speed" style="width:100%">' +
          '<div class="ams-row" style="margin-bottom:7px"><select class="ams-sel" data-k="rt_lang" style="flex:1 1 100%">' +
            _RTL.map(function (o) { return '<option value="' + o[0] + '"' + ((c.rt_lang || '') === o[0] ? ' selected' : '') + '>' + o[1] + '</option>'; }).join('') +
          '</select></div>' +
          '<div class="ams-row" style="margin-bottom:7px"><select class="ams-sel" data-k="rt_noise" style="flex:1 1 100%">' +
            [['', '噪音抑制:近场(耳机 / 手持,默认)'], ['far', '噪音抑制:远场(iPad 放桌上外放——环境音 / 操作声抑制更强)']].map(function (o) {
              return '<option value="' + o[0] + '"' + ((c.rt_noise || '') === o[0] ? ' selected' : '') + '>' + o[1] + '</option>';
            }).join('') +
          '</select></div>' +
          '<div class="ams-row" style="margin-bottom:7px"><select class="ams-sel" data-k="rt_eagerness" style="flex:1 1 100%">' +
            _RTG.map(function (o) { return '<option value="' + o[0] + '"' + ((c.rt_eagerness || 'auto') === o[0] ? ' selected' : '') + '>' + o[1] + '</option>'; }).join('') +
          '</select></div>' +
          '<div class="ams-row" style="margin-bottom:7px"><select class="ams-sel" data-k="rt_effort" style="flex:1 1 100%">' +
            _RTE.map(function (o) { return '<option value="' + o[0] + '"' + ((c.rt_effort || 'low') === o[0] ? ' selected' : '') + '>' + o[1] + '</option>'; }).join('') +
          '</select></div>' +
          '<textarea class="ams-sel" data-k="rt_instructions" rows="2" placeholder="人设 / 附加指令(留空=默认学习伙伴;语言规则和工具纪律会自动拼在它后面)" style="width:100%;resize:vertical">' + esc2(c.rt_instructions || '') + '</textarea>' +
          '<label class="ams-cur" style="display:flex;align-items:center;gap:6px;margin:4px 0 2px;cursor:pointer">' +
          '<input type="checkbox" data-k="rt_full_duplex"' + (c.rt_full_duplex ? ' checked' : '') + '>全双工打断(⚠仅戴耳机时勾:AI 说话期间也收音,可随时插话;外放勾了会回声自问自答。默认=半双工:它说话时不收音,防外放回声)</label>' +
          '<label class="ams-cur" style="display:flex;align-items:center;gap:6px;margin:4px 0 2px;cursor:pointer">' +
          '<input type="checkbox" data-k="rt_image"' + (c.rt_image ? ' checked' : '') + '>图像输入(看图类工具的渲染图直接给 GPT 自己看,不经文字转述;实验性,报错就关掉)</label>' +
        '<label class="ams-cur" style="display:flex;align-items:center;gap:6px;margin:4px 0 2px;cursor:pointer">' +
        '<input type="checkbox" data-k="rt_tool_reply"' + (c.rt_tool_reply ? ' checked' : '') + '>工具完成后口头回报(搜索/配图等展示型工具:关=静默入库只显示卡片[推荐];开=AI 拿到结果后自由回答)</label>' +
          '<div class="ams-tdef" style="margin:2px 0 6px">语言选「自动」它跟着你切换;读日语书建议选「日本語」或「自动」(原文按原生发音念)。以上都是下次开话生效;接话灵敏度=semantic VAD 的 eagerness(按语义判断你说完没)。</div>';
      } else {      // ── 豆包 S2S 专属 ──
        H += '<div class="ams-row" style="margin-bottom:7px"><select class="ams-sel" data-k="speaker" style="flex:1 1 100%">' +
          _VC_SPK.map(function (o) { return '<option value="' + o[0] + '"' + ((c.speaker || _VC_SPK[0][0]) === o[0] ? ' selected' : '') + '>' + o[1] + '</option>'; }).join('') +
        '</select></div>' +
        '<div class="ams-row" style="margin-bottom:7px"><select class="ams-sel" data-k="explicit_dialect" style="flex:1 1 100%">' +
          _VC_DIA.map(function (o) { return '<option value="' + o[0] + '"' + ((c.explicit_dialect || '') === o[0] ? ' selected' : '') + '>方言:' + o[1] + '</option>'; }).join('') +
        '</select></div>' +
        '<div class="ams-cur" style="margin:0 0 2px">通话语速(S2S) <b class="vcv-sr">' + (c.speech_rate || 0) + '</b>(-50 慢 ~ 100 快)</div>' +
        '<input type="range" min="-50" max="100" step="5" value="' + (c.speech_rate || 0) + '" data-k="speech_rate" style="width:100%">' +
        '<div class="ams-cur" style="margin:4px 0 2px">通话音量(S2S) <b class="vcv-lr">' + (c.loudness_rate || 0) + '</b>(-50 轻 ~ 100 响)</div>' +
        '<input type="range" min="-50" max="100" step="5" value="' + (c.loudness_rate || 0) + '" data-k="loudness_rate" style="width:100%">' +
        '<div class="ams-row" style="margin:7px 0"><input class="ams-sel" data-k="bot_name" placeholder="名字(默认:豆包)" value="' + esc2(c.bot_name || '') + '" style="flex:1 1 44%">' +
        '<input class="ams-sel" data-k="speaking_style" placeholder="说话风格(如:口吻拽拽的)" value="' + esc2(c.speaking_style || '') + '" style="flex:1 1 50%"></div>' +
        '<textarea class="ams-sel" data-k="system_role" rows="2" placeholder="人设(背景设定,留空=默认学习伙伴;伴读工具协议会自动拼在它后面)" style="width:100%;resize:vertical">' + esc2(c.system_role || '') + '</textarea>' +
        '<label class="ams-cur" style="display:flex;align-items:center;gap:6px;margin-top:6px;cursor:pointer">' +
        '<input type="checkbox" data-k="enable_music"' + (c.enable_music ? ' checked' : '') + '>唱歌能力(检索版权曲库,让它真能唱)</label>';
      }
      // ── 与通话引擎无关(朗读=独立 TTS 链路 / ASR=mic 长按豆包识别):恒显 ──
      H += '<div class="ams-sub" style="margin:10px 0 4px;color:#8fa8d8">— 朗读 / 语音输入(与通话引擎无关)—</div>' +
        '<div class="ams-row" style="margin-bottom:7px"><select class="ams-sel" data-k="tts_speaker" style="flex:1 1 100%">' +
          _VC_TTS_SPK.map(function (o) { return '<option value="' + o[0] + '"' + ((c.tts_speaker || _VC_TTS_SPK[0][0]) === o[0] ? ' selected' : '') + '>朗读音色:' + o[1] + '</option>'; }).join('') +
        '</select></div>' +
        '<div class="ams-cur" style="margin:4px 0 2px">朗读语速 <b class="vcv-tsr">' + (c.tts_speech_rate || 0) + '</b>(-50 慢 ~ 100 快)</div>' +
        '<input type="range" min="-50" max="100" step="5" value="' + (c.tts_speech_rate || 0) + '" data-k="tts_speech_rate" style="width:100%">' +
        '<div class="ams-row" style="margin:7px 0 0"><input class="ams-sel" data-k="tts_instruction" placeholder="默认朗读语气(AI 会按内容自动调整情绪;这里是它没给时的兜底,仅2.0音色)" value="' + esc2(c.tts_instruction || '') + '" style="flex:1 1 100%"></div>' +
        '<label class="ams-cur" style="display:flex;align-items:center;gap:6px;margin:6px 0 2px;cursor:pointer">' +
        '<input type="checkbox" id="vcv-cap-tg">朗读字幕(侧栏关闭时屏幕下方显示当前句+上一句,跟声音同步,不挡触控;本设备)</label>' +
        '<div class="ams-row" style="margin:7px 0"><select class="ams-sel" id="vcv-bridge" style="flex:1 1 100%">' +
          '<option value="auto">回声桥:自动(外放走 Pi 桥消回声,戴耳机自动直连;豆包/GPT-WS/Grok 引擎)</option>' +
          '<option value="1">回声桥:总是</option>' +
          '<option value="0">回声桥:关闭</option>' +
        '</select></div>' +
        '<label class="ams-cur" style="display:flex;align-items:center;gap:6px;margin:4px 0 2px;cursor:pointer">' +
        '<input type="checkbox" id="vcv-card-tg">文字卡自动消失(路由/文字模式的回复在侧栏关闭时弹磨砂卡片;开=到时自动收起,关=常驻到手动关;本设备)</label>' +
        '<label class="ams-cur" style="display:flex;align-items:center;gap:6px;margin:4px 0 2px;cursor:pointer">' +
        '<input type="checkbox" id="vcv-cue-tg">任务完成预制语音(搜索/制卡等做完念一句固定提示音;本设备)' +
        '<span style="color:#7a8497">— 开了「工具完成后口头回报」时**自动禁用**,免得跟 AI 的真实回答同时出声</span></label>' +
        '<div class="ams-row" id="vcv-card-row" style="display:flex;align-items:center;gap:8px;margin:2px 0 6px;padding-left:22px">' +
        '<span style="font-size:12px;color:#8a9bb4;flex:none">停留 <b id="vcv-card-v">20</b> 秒</span>' +
        '<input type="range" id="vcv-card-sec" min="5" max="60" step="5" style="flex:1;accent-color:#7b6cff"></div>' +
        '<label class="ams-cur" style="display:flex;align-items:center;gap:6px;margin-top:4px;cursor:pointer">' +
        '<input type="checkbox" data-k="asr_v2"' + (c.asr_v2 ? ' checked' : '') + '>ASR 2.0(长按麦克风的豆包识别换新模型,关键词召回+20%;⚠需先在火山控制台开通「流式语音识别2.0」商品,没开通会连不上)</label>' +
        (isOA ? '' :
        '<div class="ams-tdef" style="margin-top:6px">改完即存;通话中改音色/语速立即生效;朗读音色/语气下一句生效(2.0 音色支持自然语言语气指令;停顿由 AI 的标点/省略号控制)。人设/风格下次开话生效。角色扮演在这里写人设+挑音色(SC2.0 克隆音色线不支持工具协议,不接)</div>');
      card.innerHTML = H;
      function _save(k, v, el) {
        var body = {}; body[k] = v;
        fetch('/api/assistant/voice-config', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
          .then(function (r) { return r.json(); })
          .then(function (x) {
            if (x && x.ok) {
              if (typeof _toast === 'function') _toast('已保存');
              try { if (window.RC && RC.voicecall && RC.voicecall.pushCfg) RC.voicecall.pushCfg(); } catch (_) {}
              if (k === 'rt_engine') _renderVoiceCfg(container);   // 切引擎:整卡重绘,只显示该引擎相关项(89:container=tab2 pane)
            } else if (typeof _toast === 'function') _toast('保存失败');
          }).catch(function () {});
      }
      var _brSel = card.querySelector('#vcv-bridge');   // 99:回声桥三态(设备级)
      if (_brSel) {
        try { _brSel.value = localStorage.getItem('rc-voice-bridge') || 'auto'; } catch (e) {}
        _brSel.addEventListener('change', function () {
          try { localStorage.setItem('rc-voice-bridge', _brSel.value); } catch (e) {}
          if (typeof _toast === 'function') _toast('回声桥:' + (_brSel.value === 'auto' ? '自动' : _brSel.value === '1' ? '总是' : '关闭') + '(下次通话生效)');
        });
      }
      var _capTg = card.querySelector('#vcv-cap-tg');   // 朗读字幕开关:设备级偏好(localStorage),不进服务端凭证
      if (_capTg) {
        try { _capTg.checked = localStorage.getItem('rc-voice-sub') !== '0'; } catch (e) {}
        _capTg.addEventListener('change', function () {
          try { localStorage.setItem('rc-voice-sub', _capTg.checked ? '1' : '0'); } catch (e) {}
          if (typeof _toast === 'function') _toast(_capTg.checked ? '朗读字幕已开' : '朗读字幕已关');
        });
      }
      var _cueTg = card.querySelector('#vcv-cue-tg');   // 任务完成预制语音(设备级;与「工具口头回报」互斥)
      if (_cueTg) {
        var _syncCue = function () {
          var muted = false;
          try { muted = localStorage.getItem('rc-voice-toolreply') === '1'; } catch (e) {}
          _cueTg.disabled = muted;
          _cueTg.parentNode.style.opacity = muted ? '.4' : '1';
          _cueTg.parentNode.title = muted ? '「工具完成后口头回报」开着 → 预制语音已自动禁用(避免两个声音同时发)' : '';
        };
        try {   // 服务端的 rt_tool_reply 是真相源 → 开面板时镜像到本地(互斥判定和 _rtcTool._silent 都读它)
          var _tr0 = card.querySelector('[data-k="rt_tool_reply"]');
          if (_tr0) localStorage.setItem('rc-voice-toolreply', _tr0.checked ? '1' : '0');
        } catch (e) {}
        try { _cueTg.checked = localStorage.getItem('rc-voice-cue') !== '0'; } catch (e) {}
        _syncCue();
        _cueTg.addEventListener('change', function () {
          try { localStorage.setItem('rc-voice-cue', _cueTg.checked ? '1' : '0'); } catch (e) {}
          if (typeof _toast === 'function') _toast(_cueTg.checked ? '任务完成预制语音已开' : '任务完成预制语音已关');
        });
        card.addEventListener('change', function (ev) {   // 口头回报开关一变 → 预制音开关跟着灰/亮
          if (ev.target && ev.target.getAttribute && ev.target.getAttribute('data-k') === 'rt_tool_reply') {
            try { localStorage.setItem('rc-voice-toolreply', ev.target.checked ? '1' : '0'); } catch (e) {}
            _syncCue();
          }
        });
      }
      var _cdTg = card.querySelector('#vcv-card-tg'), _cdSec = card.querySelector('#vcv-card-sec'), _cdV = card.querySelector('#vcv-card-v');
      if (_cdTg) {   // 65 文字卡自动消失:开关+秒数(设备级)
        try { _cdTg.checked = localStorage.getItem('rc-voice-card-hide') !== '0'; } catch (e) {}
        try { var _s0 = parseInt(localStorage.getItem('rc-voice-card-secs') || '20', 10) || 20; _cdSec.value = _s0; _cdV.textContent = _s0; } catch (e) {}
        var _cdRow = card.querySelector('#vcv-card-row');
        function _cdSync() { if (_cdRow) _cdRow.style.opacity = _cdTg.checked ? '1' : '.35'; }
        _cdSync();
        _cdTg.addEventListener('change', function () {
          try { localStorage.setItem('rc-voice-card-hide', _cdTg.checked ? '1' : '0'); } catch (e) {}
          _cdSync();
          if (typeof _toast === 'function') _toast(_cdTg.checked ? '文字卡到时自动收起' : '文字卡常驻(手动关)');
        });
        if (_cdSec) _cdSec.addEventListener('input', function () {
          if (_cdV) _cdV.textContent = _cdSec.value;
          try { localStorage.setItem('rc-voice-card-secs', String(_cdSec.value)); } catch (e) {}
        });
      }
      card.querySelectorAll('[data-k]').forEach(function (el) {
        el.addEventListener('change', function () {
          var k = el.getAttribute('data-k'), v;
          if (el.type === 'checkbox') v = el.checked;
          else if (el.type === 'range') v = ((el.getAttribute('step') || '').indexOf('.') >= 0 ? (parseFloat(el.value) || 1) : (parseInt(el.value, 10) || 0));   // 92:小数步长滑条(通话语速)按 float 存
          else v = el.value.trim();
          var _bmap = { speech_rate: '.vcv-sr', loudness_rate: '.vcv-lr', tts_speech_rate: '.vcv-tsr', rt_speed: '.vcv-rts' };
          if (_bmap[k]) { var b1 = card.querySelector(_bmap[k]); if (b1) b1.textContent = v; }
          _save(k, v, el);
        });
        if (el.type === 'range') el.addEventListener('input', function () {   // 拖动实时显示数值(change 才保存)
          var k = el.getAttribute('data-k');
          var m = { speech_rate: '.vcv-sr', loudness_rate: '.vcv-lr', tts_speech_rate: '.vcv-tsr', rt_speed: '.vcv-rts' };
          var b = card.querySelector(m[k] || '.vcv-sr'); if (b) b.textContent = el.value + (k === 'rt_speed' ? '×' : '');
        });
      });
    }).catch(function () { card.innerHTML = '<div class="ams-tdef">拉取语音设置失败</div>'; });
  }
  function renderModelSettings(container, focusAction) {
    if (!container) return;
    var _rootEl = container;   // 外层容器引用:下面 528 行 container 被重指到 pane1,预设切换的整体重渲必须用它(否则 tab 行叠加,用户实测每切一次多一组)
    container.innerHTML = '<div class="ams-sub">加载中…</div>';
    fetch('/api/assistant/action-prefs').then(function (r) { return r.json(); }).then(function (d) {
      if (!d || !d.ok) { container.innerHTML = '<div class="ams-sub">拉取设置失败</div>'; return; }
      container.innerHTML = '';
      // 89(用户设计):内容太多→Tab 分区。tab1=阅读 AI 任务(预设+各环节模型);tab2=语音通话·朗读
      var tabbar = document.createElement('div'); tabbar.className = 'ams-tabs';
      tabbar.innerHTML = '<button type="button" class="ams-tab on" data-t="1">阅读 AI 任务</button>' +
        '<button type="button" class="ams-tab" data-t="2">语音通话 · 朗读</button>';
      var pane1 = document.createElement('div'), pane2 = document.createElement('div');
      pane2.style.display = 'none';
      container.appendChild(tabbar); container.appendChild(pane1); container.appendChild(pane2);
      tabbar.querySelectorAll('.ams-tab').forEach(function (tb) {
        tb.addEventListener('click', function () {
          tabbar.querySelectorAll('.ams-tab').forEach(function (x) { x.classList.remove('on'); });
          tb.classList.add('on');
          var t = tb.getAttribute('data-t');
          pane1.style.display = t === '1' ? '' : 'none';
          pane2.style.display = t === '2' ? '' : 'none';
          if (t === '2' && !pane2.childElementCount) _renderVoiceCfg(pane2);
        });
      });
      container = pane1;   // 下方既有代码原样渲进 tab1(最小侵入;focusAction 定位的 actions 都在 tab1)
      window.__amsVoicePane = pane2;   // 语音区挂 tab2(见下方 523 行改调)
      var sub = document.createElement('div'); sub.className = 'ams-sub';
      sub.textContent = '每个任务可单独设 后端/型号/深度,改完即时生效(服务端保存,全设备生效)。点预设=进入该预设的设置页,✓亮的是当前生效预设,改动自动存进它;切到别的预设互不影响。';
      container.appendChild(sub);
      // 界面开关:回答下方的「追问建议按钮」默认不显示(用户拍板放弃),这里可重新打开(本机 localStorage)。
      var fuRow = document.createElement('label'); fuRow.className = 'ams-sub';
      fuRow.style.cssText = 'display:flex;align-items:center;gap:7px;cursor:pointer;margin:2px 0 8px';
      var fuCk = document.createElement('input'); fuCk.type = 'checkbox';
      try { fuCk.checked = localStorage.getItem('asst-followups-on') === '1'; } catch (_) {}
      fuCk.addEventListener('change', function () { try { localStorage.setItem('asst-followups-on', fuCk.checked ? '1' : '0'); } catch (_) {} });
      fuRow.appendChild(fuCk); fuRow.appendChild(document.createTextNode('回答下方显示「追问建议」按钮(本设备)'));
      container.appendChild(fuRow);
      // ── 预设条:整套设置的快照,点 chip 一键切换(服务端 /pref-profiles)。长按/右键删,＋存当前 ──
      var pbar = document.createElement('div'); pbar.className = 'ams-profiles';
      container.appendChild(pbar);
      (function _loadProfiles() {
        fetch('/api/assistant/pref-profiles').then(function (r) { return r.json(); }).then(function (p) {
          if (!p || !p.ok) return;
          var act = p.active || '';   // 当前应用中的预设(存/应用会设,单项改动会清)
          pbar.innerHTML = '';
          function _del(nm) {
            if (!confirm('删除预设「' + nm + '」?')) return;
            fetch('/api/assistant/pref-profiles', { method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ op: 'delete', name: nm }) }).then(_loadProfiles).catch(function () {});
          }
          (p.profiles || []).forEach(function (nm) {
            var b = document.createElement('button'); b.className = 'ams-prof' + (nm === act ? ' on' : ''); b.textContent = (nm === act ? '✓ ' : '') + nm;
            var _t = null, _held = false;
            b.addEventListener('touchstart', function () {
              _held = false; _t = setTimeout(function () { _t = null; _held = true; _del(nm); }, 600);
            }, { passive: true });
            b.addEventListener('touchend', function () { if (_t) { clearTimeout(_t); _t = null; } });
            b.addEventListener('touchmove', function () { if (_t) { clearTimeout(_t); _t = null; } }, { passive: true });
            b.addEventListener('contextmenu', function (e) { e.preventDefault(); _held = true; _del(nm); });
            b.addEventListener('click', function () {
              if (_held) { _held = false; return; }   // 长按删除后 iOS 可能补发 click
              b.disabled = true;
              fetch('/api/assistant/pref-profiles', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ op: 'apply', name: nm }) })
                .then(function (r) { return r.json(); })
                .then(function (d) {
                  if (d && d.ok) { if (typeof _toast === 'function') _toast('已切换到「' + nm + '」'); renderModelSettings(_rootEl, focusAction); }
                  else { b.disabled = false; if (typeof _toast === 'function') _toast('应用失败'); }
                }).catch(function () { b.disabled = false; });
            });
            pbar.appendChild(b);
          });
          var add = document.createElement('button'); add.className = 'ams-prof ams-prof-add'; add.textContent = '＋存为预设';
          add.addEventListener('click', function () {
            var nm = prompt('新预设名(复制当前全部设置为一个新预设;之后在它页面里的改动会自动存进它):'); if (!nm || !nm.trim()) return;
            fetch('/api/assistant/pref-profiles', { method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ op: 'save', name: nm.trim() }) })
              .then(function (r) { return r.json(); })
              .then(function (d) { if (d && d.ok) { if (typeof _toast === 'function') _toast('已保存'); _loadProfiles(); } })
              .catch(function () {});
          });
          pbar.appendChild(add);
        }).catch(function () {});
      })();
      var _focusCard = null;
      function _renderActs(list) {
        list.forEach(function (a) {
          var ai = d.actions[a]; if (!ai) return;
          var c = _buildMsTask(a, { pref: ai.pref, def: ai.default }, d.catalog, d.names, d.locked || {});
          if (a === focusAction) _focusCard = c;   // 从某步⚙进来 → 定位到对应任务卡
          container.appendChild(c);
        });
      }
      _renderActs(['orchestrator', 'summarize', 'vision', 'deep']);
      // 阅读器其它 AI 入口(解释/翻译/字典/语法),跟助手共用同一套脱壳 Claude + Gemini 双后端预设
      var _rh = document.createElement('div'); _rh.className = 'ams-sub';
      _rh.style.cssText = 'margin-top:12px;font-weight:600;color:#9fc0ff;';
      _rh.textContent = '— 阅读器其它 AI —';
      container.appendChild(_rh);
      _renderActs(['explain', 'translate', 'dict', 'grammar', 'pick_video']);   // 服务端没有的 action 自动跳过(向后兼容);pick_video=找视频拟词+筛选
      var _wh = document.createElement('div'); _wh.className = 'ams-sub';
      _wh.style.cssText = 'margin-top:12px;font-weight:600;color:#9fc0ff;';
      _wh.textContent = '— 联网与语音文字环节 —';
      container.appendChild(_wh);
      _renderActs(['web_search', 'route_text', 'img_norm']);   // 91:总面板补全(此前 img_norm 只有感叹号直达)
      var note = document.createElement('div'); note.className = 'ams-note';
      note.textContent = '标「免费」= 免费档支持该型号;但免费是**共享算力**,高峰常过载(503)或限流时会自动落付费保不中断——'
        + '此时这里会标「付费(过载/限流)」、感叹号里也显付费。「💰仅付费」= 该型号免费档没有(如 3.1-pro),'
        + '选它每次调用都按量计费。flash 高峰过载较多;想更稳的免费可试 flash-lite 系。';
      container.appendChild(note);
      // 89:语音区改挂 tab2(点开 tab 才渲,省首屏);tab1 只留阅读任务
      // _renderVoiceCfg 由 tab 切换懒加载(见上方 tabbar 逻辑)
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

  // ── 91(用户设计):「本环节 AI 设置」迷你面板——感叹号⚙不再只是快捷键,而是把该环节相关的
  //    AI 调用设置(action-prefs 同一套行 UI + 同一组端点)单独拿出来直接改。actions=相关环节键列表。──
  function openActionSettings(actions, opts) {
    opts = opts || {};
    var mask = document.createElement('div'); mask.className = 'ams-mask';
    mask.addEventListener('click', function (e) { if (e.target === mask) mask.remove(); });
    var box = document.createElement('div'); box.className = 'ams-box';
    var h = document.createElement('div'); h.className = 'ams-h';
    var ht = document.createElement('span'); ht.textContent = '⚙ 本环节 AI 设置';
    var x = document.createElement('button'); x.className = 'ams-x'; x.textContent = '×';
    x.addEventListener('click', function () { mask.remove(); });
    h.appendChild(ht); h.appendChild(x); box.appendChild(h);
    var body = document.createElement('div'); box.appendChild(body);
    mask.appendChild(box); document.body.appendChild(mask);
    body.innerHTML = '<div class="ams-sub">加载中…</div>';
    fetch('/api/assistant/action-prefs').then(function (r) { return r.json(); }).then(function (d) {
      if (!d || !d.ok) { body.innerHTML = '<div class="ams-sub">拉取设置失败</div>'; return; }
      body.innerHTML = '';
      if (opts.note) { var n0 = document.createElement('div'); n0.className = 'ams-sub'; n0.textContent = opts.note; body.appendChild(n0); }
      var got = 0;
      (actions || []).forEach(function (a) {
        var ai = d.actions[a]; if (!ai) return;
        body.appendChild(_buildMsTask(a, { pref: ai.pref, def: ai.default }, d.catalog, d.names, d.locked || {}));
        got++;
      });
      if (!got) { var n1 = document.createElement('div'); n1.className = 'ams-sub'; n1.textContent = '该环节走固定链路,暂无可调模型。'; body.appendChild(n1); }
      if (opts.voiceTab) {   // 语音轮:主模型=GPT Realtime,设置在总面板「语音通话·朗读」Tab——给一键直达
        var vb = document.createElement('button'); vb.className = 'vc-inf-set'; vb.textContent = '🎙 语音通话主模型与开关(打开语音 Tab)';
        vb.addEventListener('click', function () {
          mask.remove();
          try { openModelSettings(); } catch (e) { return; }
          var tries = 0;
          (function _hit() {   // renderModelSettings 是 fetch 异步渲染,tabbar 晚到——轮询点 tab2
            var t2 = document.querySelector('.ams-mask .ams-tab[data-t="2"]');
            if (t2) { t2.click(); return; }
            if (tries++ < 25) setTimeout(_hit, 120);
          })();
        });
        body.appendChild(vb);
      }
    }).catch(function () { body.innerHTML = '<div class="ams-sub">拉取设置失败</div>'; });
  }
  try { window.__asstActionSettings = openActionSettings; } catch (_) {}

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
    stripMoodTag: stripMoodTag,
    renderFollowups: renderFollowups,
    contextCard: contextCard,
    openModelSettings: openModelSettings,
    renderModelSettings: renderModelSettings,
    paidNotice: paidNotice
  };
})();


// ════════════════════════════════════════════════════════════════════════
// ②b:PDF 阅读器侧栏(从 reader.src/25-assistant.js 机械搬迁;裸全局经 HOST 别名、
//     live 值经 HOST getter/setter)。由 27-rc-adapter.js 在 PdfAdapter.bind 之后、
//     且 ?asst=shared 时调 mountPdfSidebar()(保证 HOST 就绪 + 默认仍走老 25-assistant)。
//     window.* 调用在 PDF 上原样存活;本地函数照常在体内定义。EPUB 无 #grammar-panel → 不挂。
// ════════════════════════════════════════════════════════════════════════
(function () {
  var RC = (window.RC = window.RC || {});
  if (!RC.assistant) return;
  RC.assistant.mountPdfSidebar = function () {
  if (window.__asstLoaded) return;
    // ── ②b/③ 共享层 HOST:裸全局从 PdfAdapter._host.asst 取(纯转发,PDF 行为不变);挂载点/注解端点也经 HOST(reader 无关)。
    //    本地定义的函数(_flashSelOnPage/_jumpToCtx/_showHlPicker/_reloadHighlights/_assistEdit/_noteNearText)照常在下方定义。
    var HOST = (window.RC && RC.adapter && RC.adapter()._host && RC.adapter()._host.asst) || {};
  var panelEl = (HOST.mountPanel && HOST.mountPanel()) || document.getElementById('grammar-panel');   // ③-3:挂载容器经 HOST(EPUB=#ep-side)
  var tabsEl = (HOST.mountTabs && HOST.mountTabs()) || document.getElementById('side-tabs');
  if (!panelEl || !tabsEl) return;   // 抽屉不在(非阅读器页)就不挂
  window.__asstLoaded = true;
    var md = HOST.md, _toast = HOST.toast, _qhFmtTime = HOST.fmtTime,
        loadAllHighlights = HOST.loadAllHighlights, renderHighlightsOnPage = HOST.renderHighlightsOnPage,
        renderPhraseHl = HOST.renderPhraseHl, _removePhraseHighlight = HOST.removePhraseHighlight,
        _charsRangeToText = HOST.charsRangeToText, _charRangeToPtRects = HOST.charRangeToPtRects,
        openGrammarPanel = HOST.openDrawer;
    var _HLURL = (HOST.hlUrl && HOST.hlUrl()) || '/pdf/api/highlights', _NOTESURL = (HOST.notesUrl && HOST.notesUrl()) || '/pdf/api/notes', _NCURL = (HOST.noteCompositeUrl && HOST.noteCompositeUrl()) || '/pdf/api/note-composite';   // ③-2:注解端点 reader 无关
    // ③-4b:chat/history/clear 端点经 HOST(PDF 默认=原字面量→零回归;EPUB=epub-assistant/epub-convo[/clear]?file=)。
    //   undo(/api/assistant/undo)、prewarm(/api/assistant/prewarm)、action-pref[s]、voice task-status 两阅读器**本就共用同一后端**,无需路由。
    var _CHATURL = (HOST.chatUrl && HOST.chatUrl()) || '/api/assistant/chat',
        _HISTURL = (HOST.historyUrl && HOST.historyUrl()) || '/api/assistant/history',
        _CLEARURL = (HOST.clearUrl && HOST.clearUrl()) || '/api/assistant/clear';


  var streaming = false;   // 对话历史由服务端保存,前端不再持本地数组

  // ── tab 注入(放第一个,最显眼)──
  var tabBtn = document.createElement('button');
  tabBtn.className = 'side-tab'; tabBtn.dataset.pane = 'asst';
  // Apple/SF「sparkles」图标(替代 🤖 emoji),复用模板 .si 样式
  tabBtn.innerHTML = '<svg class="si" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4l1.4 4.2L18 9.6l-4.6 1.4L12 16l-1.4-4.6L6 9.6l4.6-1.4L12 4z"/><path d="M18.6 14.5l.6 1.7 1.7.6-1.7.6-.6 1.7-.6-1.7-1.7-.6 1.7-.6.6-1.7z"/></svg><span class="ep-side-tab-lb">助手</span>';   // 标签包 span:统一抽屉窄屏只显图标时一起隐藏
  tabBtn.title = '助手';
  tabBtn.onclick = function () { HOST.switchTab && HOST.switchTab('asst'); setTimeout(function () { ta && ta.focus(); }, 200); };
  tabsEl.insertBefore(tabBtn, tabsEl.firstChild);

  // ── pane 注入 ──
  var pane = document.createElement('div');
  pane.className = 'side-pane'; pane.dataset.pane = 'asst'; pane.id = 'side-pane-asst';
  // 快捷栏:共享构建器 rcBuildQuickBar(在则空容器等它填,与 EPUB 同一份来源 → 按钮永不分叉;
  //   历史「总结本页/本页生词」不再纳入)。legacy 模式(rc-assistant 未加载)→ native 兜底同款三按钮。
  var _quickNative = window.rcBuildQuickBar ? '' :
      '<button data-q="clear">🗑 清空</button>' +
      '<button data-q="models">⚙ 模型</button>';
  pane.innerHTML =
    '<div id="asst-thread"></div>' +
    '<div id="asst-quick">' + _quickNative + '</div>' +
    '<div id="asst-input">' +
      '<button id="asst-mic" title="语音输入"><svg viewBox="0 0 24 24" width="20" height="20"><path fill="currentColor" d="M12 15a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v6a3 3 0 0 0 3 3zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.93V22h2v-3.07A7 7 0 0 0 19 12h-2z"/></svg></button>' +
      '<textarea id="asst-ta" rows="1" placeholder="问这本书 / 让我帮你…"></textarea>' +
      '<button id="asst-send" title="发送">➤</button></div>';
  panelEl.appendChild(pane);
  try {
    var _qb = document.getElementById('asst-quick');
    if (window.rcBuildQuickBar) window.rcBuildQuickBar(_qb, {});
    else if (window.rcBuildMediaRow) window.rcBuildMediaRow(_qb);   // legacy:至少并上「配图/视频」媒体行
  } catch (e) {}

  var css = document.createElement('style');
  css.textContent =
    '#asst-fab{position:fixed;right:14px;bottom:90px;z-index:115;width:50px;height:50px;border-radius:50%;border:none;' +
    'background:#2563eb;color:#fff;font-size:24px;box-shadow:0 6px 18px rgba(0,0,0,.4);cursor:pointer;display:flex;align-items:center;justify-content:center;-webkit-tap-highlight-color:transparent}' +
    '#asst-fab:active{transform:scale(.92)}' +
    '#side-pane-asst.active{display:flex;flex-direction:column;overflow:hidden;height:100%}' +
    '#asst-thread{flex:1 1 auto;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:10px;-webkit-overflow-scrolling:touch;min-height:0;overscroll-behavior:contain;touch-action:pan-y}' +   // contain+pan-y:滚到头不把滚动链漏给底下 PDF(否则阅读器在浮层下偷偷滚→IO 渲页=卡)
    '.asst-msg{max-width:92%;padding:9px 12px;border-radius:13px;font-size:14px;line-height:1.55;word-break:break-word}' +
    '.asst-u{align-self:flex-end;background:#1d4ed8;color:#fff;border-bottom-right-radius:4px}' +
    '.asst-a{align-self:flex-start;background:#161d31;border:1px solid #243152;border-bottom-left-radius:4px}' +
    '.asst-a p{margin:.4em 0}.asst-a ul,.asst-a ol{margin:.3em 0;padding-left:1.3em}.asst-a code{background:#0b1220;padding:1px 4px;border-radius:4px}' +
    '.asst-a h1,.asst-a h2,.asst-a h3{font-size:1em;margin:.5em 0 .2em}' +
    /* 内容图给浅色画布 matte:助手气泡恒深底(#161d31),透明底 SVG/图的黑轴黑字看不清 → 白底救场(同 rc-result;MathJax=chtml 无 svg 不误伤) */
    '.asst-a img,.asst-a svg{max-width:100%;height:auto;border-radius:8px;display:block;margin:.4em auto;background:#fff;padding:10px;box-sizing:border-box}' +
    '.asst-a img{cursor:zoom-in}' +
    '.asst-tool{align-self:flex-start;color:#7c93c4;font-size:12px;padding:2px 6px;font-style:italic}' +
    '.asst-note{align-self:center;background:#2a2410;border:1px solid #5a4a18;color:#e7d28a;font-size:12px;padding:4px 10px;border-radius:9px;max-width:96%}' +
    '.asst-undo{background:#3a1d2a;border:1px solid #6b3550;color:#ffd0e0;border-radius:7px;padding:2px 8px;font-size:12px;cursor:pointer;margin-left:6px}' +
    '.asst-undo:active{background:#52283a}.asst-undo:disabled{opacity:.5}' +
    '.asst-jump{background:#16293a;border:1px solid #2a4a63;color:#bce0ff;border-radius:7px;padding:2px 8px;font-size:12px;cursor:pointer;margin-left:6px}' +
    '.asst-jump:active{background:#1d3a52}' +
    '.asst-hl-row{display:flex;align-items:center;gap:6px;padding:5px 6px;border-radius:8px;margin-top:5px;background:#161d33}' +
    '.asst-hl-sw{flex:0 0 auto;width:12px;height:12px;border-radius:3px;border:1px solid #ffffff33}' +
    '.asst-hl-tx{flex:1 1 auto;min-width:0;font-size:12.5px;color:#cdd8f5;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}' +
    '.asst-hl-del{flex:0 0 auto;background:#3a1d1d;border:1px solid #6b3535;color:#ffd0d0;border-radius:7px;padding:2px 8px;font-size:12px;cursor:pointer}' +
    '.asst-hl-del:active{background:#522828}.asst-hl-del:disabled{opacity:.5}' +
    '.asst-hl-redo{flex:0 0 auto;background:#1d3a2a;border:1px solid #2f6347;color:#bfead0;border-radius:7px;padding:2px 8px;font-size:12px;cursor:pointer}.asst-hl-redo:active{background:#244a35}.asst-hl-redo:disabled{opacity:.5}' +   // M9:删完转「↪ 重做」
    '.asst-edit-card{align-self:flex-start;max-width:92%;background:#13203a;border:1px solid #294060;border-radius:11px;padding:8px 11px;display:flex;flex-direction:column;gap:7px}' +
    '.asst-edit-h{font-size:12.5px;color:#bfe0c8}' +
    '.asst-edit-chips{display:flex;flex-wrap:wrap;gap:6px}' +
    '.asst-edit-undo{align-self:flex-start;background:#26344f;border:1px solid #3a5273;color:#dbe7ff;border-radius:8px;padding:3px 12px;font-size:12.5px;cursor:pointer}' +
    '.asst-edit-undo:active{background:#2f4061}.asst-edit-undo:disabled{opacity:.55}' +
    '#asst-quick{flex:0 0 auto;display:flex;flex-wrap:nowrap;gap:6px;padding:8px 10px;border-top:1px solid #233156;overflow-x:auto;-webkit-overflow-scrolling:touch;scrollbar-width:none}' +
    '#asst-quick::-webkit-scrollbar{display:none}' +
    '#asst-quick button{flex:none;white-space:nowrap}' +   // 按钮多了不换行:单行横滑(iOS chips 惯例)
    '#asst-quick button{background:#16203a;border:1px solid #2a3a63;color:#bcd0ff;border-radius:8px;padding:6px 10px;font-size:13px;cursor:pointer}' +
    '#asst-quick button:active{background:#22305a}' +
    '#asst-quick button.asst-learn{background:#16293a;border-color:#2a4a63;color:#bce0ff}' +   // 学习类按钮:跟导航类区分
    '#asst-send.stop{background:#b23b3b}' +
    '.vc-bub-grip{position:absolute;top:5px;right:7px;color:#4a5a7d;font-size:11px;cursor:grab;touch-action:none;user-select:none;-webkit-user-select:none;padding:2px 4px}' +
    '.ams-tabs{display:flex;gap:6px;margin:2px 0 10px}' +
    '.ams-tab{flex:1;padding:7px 0;border-radius:9px;border:1px solid #2a3550;background:transparent;color:#8a9bb4;font-size:12.5px;cursor:pointer}' +
    '.ams-tab.on{background:#233150;color:#cfe0ff;border-color:#3a4f7f;font-weight:600}' +
    '#asst-thread{overscroll-behavior:contain}' +   // 87:侧栏滚到头不再把滚动漏给底下的阅读器
    '#side-pane-asst,#ep-side{overscroll-behavior:contain}' +
    '.vc-bub-hd{display:flex;align-items:center;gap:6px;margin:-3px -4px 6px;padding:4px 8px;border-radius:8px;' +
    'background:rgba(255,255,255,.06);cursor:grab;touch-action:none;-webkit-user-select:none;user-select:none;-webkit-touch-callout:none}' +
    '.vc-bub-t{flex:1;font-size:11.5px;color:#aab8d4;font-weight:600}' +
    '.vc-bub-hd .vc-grip{flex:none;color:#6f7d9e;font-size:12px;letter-spacing:1px}' +
    '.vc-inf-b{position:absolute;right:6px;bottom:5px;width:18px;height:18px;border-radius:50%;border:none;cursor:pointer;' +
    'background:rgba(255,255,255,.1);color:#8fa0c2;font-size:11px;font-weight:700;line-height:1;display:flex;align-items:center;justify-content:center;padding:0}' +
    '.vc-inf-pop{position:absolute;right:4px;bottom:26px;z-index:60;min-width:210px;max-width:270px;padding:10px 12px;border-radius:12px;' +
    'background:rgba(24,26,34,.94);-webkit-backdrop-filter:blur(20px);backdrop-filter:blur(20px);border:0.5px solid rgba(255,255,255,.14);box-shadow:0 10px 30px rgba(0,0,0,.45);font-size:12px}' +
    '.vc-inf-r{display:flex;gap:8px;margin:3px 0;color:#c6d1e8}.vc-inf-r span{flex:none;color:#7f8cab}' +
    '.vc-inf-set{margin-top:7px;width:100%;padding:6px;border-radius:8px;border:1px solid #35446b;background:#1b2338;color:#9fb4e0;cursor:pointer;font-size:12px}' +
    '.asst-clip{display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;border-radius:50%;border:none;cursor:pointer;' +
    'font-size:9px;line-height:1;background:rgba(123,108,255,.16);color:#9d8cff;margin:6px 0 0 2px;padding:0 0 0 1px;' +
    '-webkit-tap-highlight-color:transparent;vertical-align:middle;transition:transform .12s ease,background .2s;-webkit-user-select:none;user-select:none}' +
    '.asst-clip:active{transform:scale(.82)}' +                                            // 80:按压特效
    '.asst-clip.dim{background:rgba(255,255,255,.08);color:#5a6478}' +                     // 无录音=灰
    '.asst-clip.playing{background:#7b6cff;color:#fff;animation:vcClipBreath 1.8s ease-in-out infinite}' +   // 播放=呼吸闪光
    '.asst-clip.busy{background:rgba(255,190,90,.22);color:#f0b451;animation:vcClipBreath 1.1s ease-in-out infinite;pointer-events:none}' +   // 生成录音中=琥珀
    '@keyframes vcClipBreath{0%,100%{box-shadow:0 0 0 0 rgba(123,108,255,.45);opacity:1}50%{box-shadow:0 0 0 6px rgba(123,108,255,0);opacity:.75}}' +   // 流式中:发送→停止(红)
    // 回答底部操作行:▶ / 追问chips / tok+! 排一行(用户:别占多行;chips 多则行内横滚)
    '.asst-btm{display:flex;align-items:center;gap:6px;margin-top:8px;min-width:0}' +
    '.asst-btm .asst-followups{margin-top:0;flex:1 1 auto;min-width:0;flex-wrap:nowrap;overflow-x:auto;scrollbar-width:none;-webkit-overflow-scrolling:touch}' +
    '.asst-btm .asst-followups::-webkit-scrollbar{display:none}' +
    '.asst-btm .asst-fu{flex:0 0 auto;white-space:nowrap}' +
    '.asst-btm .asst-fb-bar{margin-top:0;margin-left:auto;flex:0 0 auto}' +
    '.asst-btm .asst-clip{margin:0;flex:0 0 auto}' +
    // AI 答完的「追问建议」chip
    '.asst-followups{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}' +
    '.asst-fu{background:#13233f;border:1px solid #2a3a63;color:#bcd0ff;border-radius:13px;padding:5px 11px;font-size:13px;cursor:pointer;text-align:left}' +
    '.asst-fu:active{background:#1d3358}' +
    // 每条回答右下角的「!」反馈按钮 + 弹出:显示这条回答经过了哪些 AI 调用(各步模型),再给两个回报动作
    '.asst-fb-bar{position:relative;margin-top:7px;display:flex;justify-content:flex-end;align-items:center}' +
    '.asst-tok{margin-right:auto;font-size:11px;color:#6f7fa3;background:#121a2e;border:1px solid #233156;border-radius:8px;padding:1px 7px}' +
    '.asst-fb-btn{width:22px;height:22px;line-height:20px;text-align:center;border-radius:50%;border:1px solid #2a3a63;background:#0e1525;color:#7c93c4;font-size:13px;font-weight:700;cursor:pointer;padding:0;-webkit-tap-highlight-color:transparent}' +
    '.asst-fb-btn:active{background:#1a2540}' +
    '.asst-fb-pop{position:absolute;right:0;bottom:28px;z-index:20;width:320px;max-width:88vw;background:#0d1426;border:1px solid #2a3a63;border-radius:11px;padding:9px;box-shadow:0 8px 22px rgba(0,0,0,.5);display:flex;flex-direction:column;gap:5px}' +
    '.afp-l-btn{cursor:pointer;text-decoration:underline dotted;text-underline-offset:2px;-webkit-tap-highlight-color:transparent}' +
    '.afp-l-btn:active{opacity:.7}' +
    '.afp-detail{white-space:pre-wrap;word-break:break-word;max-height:260px;overflow:auto;background:#0a1020;border:1px solid #233156;border-radius:8px;padding:8px 10px;margin:2px 0 4px;font-size:11.5px;color:#bcd0ee;line-height:1.55;-webkit-overflow-scrolling:touch}' +
    '.afp-h{font-size:11px;color:#7c93c4;margin-bottom:2px}' +
    '.afp-step{display:flex;align-items:center;gap:7px;font-size:12px;line-height:1.5}' +
    '.afp-l{color:#cdd9f2;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1 1 auto;min-width:0}' +
    '.afp-m{color:#7c93c4;flex:none;font-variant-numeric:tabular-nums}' +
    '.afp-gear-btn{flex:none;background:none;border:none;color:#6b7da0;font-size:13px;cursor:pointer;padding:0 1px;-webkit-tap-highlight-color:transparent}' +
    '.afp-gear-btn:active{color:#bcd0ff}' +
    '.afp-gear{display:flex;flex-wrap:wrap;align-items:center;gap:5px;margin:1px 0 5px;padding:7px;background:#0a1322;border:1px solid #243152;border-radius:8px}' +
    '.afp-glab{font-size:11px;color:#7c93c4;width:100%}' +
    '.afp-sel{background:#0d1426;border:1px solid #2a3a63;color:#dbe7ff;border-radius:6px;padding:3px 5px;font-size:12px;flex:1 1 42%;min-width:0}' +
    '.afp-gset{background:#16293a;border:1px solid #2a4a63;color:#bce0ff;border-radius:6px;padding:4px 9px;font-size:12px;cursor:pointer;flex:1 1 auto}' +
    '.afp-gdef{background:#1a2233;border:1px solid #2a3a63;color:#9fb4e0;border-radius:6px;padding:4px 9px;font-size:12px;cursor:pointer;flex:none}' +
    '.afp-foot{font-size:11px;color:#6b7da0;margin-top:5px;text-align:right;font-variant-numeric:tabular-nums}' +
    '.afp-acts{display:flex;flex-direction:column;gap:5px;margin-top:4px;border-top:1px solid #1d2742;padding-top:7px}' +
    '.afp-act{text-align:left;border:1px solid #2a3a63;border-radius:8px;padding:6px 9px;font-size:12px;cursor:pointer;color:#dbe7ff}' +
    '.afp-q{background:#16293a;border-color:#2a4a63}.afp-q:active{background:#1d3a52}' +
    '.afp-s{background:#1a2233}.afp-s:active{background:#222d44}' +
    '#asst-input{flex:0 0 auto;display:flex;gap:8px;padding:10px;border-top:1px solid #233156;align-items:flex-end}' +
    '#asst-ta{flex:1;background:#0b1220;border:1px solid #2a3a63;color:#e6eeff;border-radius:12px;padding:9px 11px;font-size:15px;resize:none;max-height:120px;line-height:1.4;font-family:inherit}' +
    '#asst-send{background:#2563eb;border:none;color:#fff;width:42px;height:42px;border-radius:12px;font-size:18px;cursor:pointer;flex:none}' +
    '#asst-send:disabled{opacity:.5}' +
    // 苹果风格语音按钮:静默时素净,听写时 iOS 蓝 + 呼吸光环
    '#asst-mic{background:#16203a;border:1px solid #2a3a63;color:#9fb4e0;width:42px;height:42px;border-radius:12px;cursor:pointer;flex:none;display:flex;align-items:center;justify-content:center;transition:background .2s,color .2s,border-color .2s,transform .1s;-webkit-tap-highlight-color:transparent}' +
    '#asst-mic:active{transform:scale(.9)}' +
    '#asst-mic.on{background:#0a84ff;border-color:#0a84ff;color:#fff;animation:asstMicPulse 1.5s ease-in-out infinite}' +
    '@keyframes asstMicPulse{0%,100%{box-shadow:0 0 0 0 rgba(10,132,255,.5)}50%{box-shadow:0 0 0 9px rgba(10,132,255,0)}}' +
    // 用户气泡里的「上下文卡片」:用过的图缩略图 / 选中的字段 / 涉及的页码,均可点击跳转
    '.asst-ctx-card{margin-top:7px;display:flex;flex-direction:column;gap:5px}' +
    '.actx-thumbs{display:flex;flex-wrap:wrap;gap:5px}' +
    '.actx-thumb{width:42px;height:42px;object-fit:cover;border-radius:6px;border:1px solid rgba(255,255,255,.45);background:#fff;cursor:pointer;flex:none}' +
    '.actx-thumb:active{transform:scale(.94)}' +
    '.actx-sel{font-size:12px;color:#dbe7ff;background:rgba(255,255,255,.13);border-left:2px solid rgba(255,255,255,.5);border-radius:4px;padding:3px 7px;cursor:pointer;line-height:1.4}' +
    '.actx-sel:active{background:rgba(255,255,255,.22)}' +
    '.actx-sel.actx-fml{text-align:center;white-space:normal;overflow-x:auto;color:#eaf2ff}' +
    '.actx-page{align-self:flex-start;font-size:11px;color:#eaf2ff;background:rgba(255,255,255,.16);border-radius:9px;padding:2px 9px;cursor:pointer}' +
    '.actx-page:active{background:rgba(255,255,255,.28)}' +
    // ⚙ 模型设置面板(每任务 后端/型号/深度)
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
    '.ams-note{font-size:11px;color:#bfae72;background:#221d10;border:1px solid #463a18;border-radius:7px;padding:6px 9px;margin-top:4px;line-height:1.5}' +
    '.asst-imgph{display:inline-block;font-size:12px;color:#7c93c4;background:#121a2e;border:1px dashed #2a3a63;border-radius:8px;padding:3px 9px;margin:.3em 0}';
  // EPUB 页没有 mfx.css(只有 PDF 模板引它)→ 揭示游标/流光/闪烁光标全无样式 = 流式动效消失。
  // 检测不到 mfx.css 时补注入等价规则(颜色取 mfx tokens 实值,不依赖 var(--c-*);PDF 上有 mfx.css → 不注入,零重复)。
  if (!document.querySelector('link[href*="mfx.css"]')) {
    css.textContent +=
      '.mfx-typing{display:inline-flex;align-items:center;gap:5px;padding:2px 2px;vertical-align:middle}' +
      '.mfx-typing i{width:7px;height:7px;border-radius:50%;background:#60a5fa;display:block;opacity:.4}' +
      '.mfx-caret{display:inline-block;width:2px;height:1.05em;margin-left:1px;vertical-align:-0.18em;border-radius:1px;background:linear-gradient(#60a5fa,#22d3ee);box-shadow:0 0 6px rgba(96,165,250,.7)}' +
      '.asst-a.mfx-streaming{box-shadow:0 0 0 1px rgba(96,165,250,.28),0 0 16px rgba(96,165,250,.14)}' +
      '.mfx-w{opacity:1}' +
      '@media (prefers-reduced-motion:no-preference){' +
        '@keyframes mfx-bounce{0%,80%,100%{transform:translateY(0);opacity:.35}40%{transform:translateY(-5px);opacity:1}}' +
        '.mfx-typing i{animation:mfx-bounce 1.2s ease-in-out infinite}' +
        '.mfx-typing i:nth-child(2){animation-delay:.16s}.mfx-typing i:nth-child(3){animation-delay:.32s}' +
        '@keyframes mfx-blink{0%,45%{opacity:1}55%,100%{opacity:0}}' +
        '.mfx-caret{animation:mfx-blink 1s steps(1) infinite}' +
        '@keyframes mfx-char{from{opacity:0;filter:blur(4px)}to{opacity:1;filter:none}}' +
        '.asst-a.mfx-streaming .mfx-w{opacity:0}' +
        '.asst-a.mfx-streaming .mfx-w.mfx-shown{opacity:1}' +
        '.asst-a.mfx-streaming .mfx-w.mfx-reveal{animation:mfx-char .34s ease both}' +
      '}';
  }
  document.head.appendChild(css);

  // 悬浮机器人 FAB(#asst-fab)已按用户要求去掉——开助手走右侧抽屉把手(#ep-side-handle / #side-handle)即可,不再额外占屏。

  var thread = pane.querySelector('#asst-thread');
  var ta = pane.querySelector('#asst-ta');
  var sendBtn = pane.querySelector('#asst-send');

  function esc(s) { var d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }
  // 把回答里的页码引用(「第40页」「40页」)变成可点链接 → 跳页 + 底部「回到」条
  function _linkifyPages(el) {
    try {
      var total = (HOST.pdfNumPages() || 99999);
      var re = /第?\s*(\d{1,4})\s*页/g;
      var nodes = [], w = document.createTreeWalker(el, NodeFilter.SHOW_TEXT, null), nd;
      while ((nd = w.nextNode())) {
        if (nd.nodeValue && /\d\s*页/.test(nd.nodeValue) && nd.parentNode &&
            !nd.parentNode.closest('a,button,.asst-pagelink,code,pre')) nodes.push(nd);
      }
      nodes.forEach(function (node) {
        var t = node.nodeValue, frag = document.createDocumentFragment(), last = 0, m; re.lastIndex = 0;
        while ((m = re.exec(t))) {
          var pn = parseInt(m[1], 10);
          if (!pn || pn < 1 || pn > total) continue;
          if (m.index > last) frag.appendChild(document.createTextNode(t.slice(last, m.index)));
          var a = document.createElement('span'); a.className = 'asst-pagelink'; a.textContent = m[0]; a.dataset.page = pn;
          frag.appendChild(a); last = m.index + m[0].length;
        }
        if (last) { if (last < t.length) frag.appendChild(document.createTextNode(t.slice(last))); node.parentNode.replaceChild(frag, node); }
      });
    } catch (_) {}
  }
  function _assetInline(el) {   // 资产编号就地渲染(用户设计 2026-07-21):AI 文字里写 #img_xxxxxx → 前端读到命令字符就直接渲染成图
    //   (/pdf/api/asset/ 编号解析:本地化过走本地文件,否则302外链)。AI 零 URL、8字符引用一张图;code/pre/链接内不动。
    try {
      var re = /#((?:img|vid|ast)_[a-f0-9]{4,12})\b/g;
      var walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT), tn, hits = [];
      while ((tn = walker.nextNode())) { if (re.test(tn.nodeValue)) hits.push(tn); re.lastIndex = 0; }
      hits.forEach(function (t) {
        if (t.parentNode && t.parentNode.closest && t.parentNode.closest('code,pre,a')) return;
        var frag = document.createDocumentFragment(), s0 = t.nodeValue, last = 0, m;
        re.lastIndex = 0;
        while ((m = re.exec(s0))) {
          frag.appendChild(document.createTextNode(s0.slice(last, m.index)));
          var im = document.createElement('img');
          im.src = '/pdf/api/asset/' + m[1]; im.dataset.aid = m[1];
          im.style.cssText = 'max-width:100%;border-radius:8px;display:block;margin:6px 0';
          frag.appendChild(im);
          last = m.index + m[0].length;
        }
        frag.appendChild(document.createTextNode(s0.slice(last)));
        t.parentNode.replaceChild(frag, t);
      });
    } catch (_) {}
  }
  function renderMd(el, text, withMath) {
    try { el.innerHTML = (typeof md === 'function') ? md(text || ' ') : esc(text).replace(/\n/g, '<br>'); }
    catch (_) { el.innerHTML = esc(text).replace(/\n/g, '<br>'); }
    _linkifyPages(el);
    _assetInline(el);
    // withMath===false(流式期间)跳过 MathJax:原先每 100ms 对整段重 typeset,长答案末段二次方卡顿 → 收尾只跑一次
    if (withMath !== false) { try { if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([el]).catch(function () {}); } catch (_) {} }
    // 图片策略:流式期间不实例化 <img>——每个 delta 全量重渲会把图元素反复销毁重建,同一 URL 洪泛请求
    // (旧 img 上的 __proxied 防重标记随元素一起死)把后端 worker 打满 → 502;收尾/历史那次才真渲图,一图一请求。
    // 真渲时先过 rcImgStabilize(rc-video.js):已知失败的维基图直接换代理 URL,不再先撞一次墙。
    if (withMath === false) {
      try { el.querySelectorAll('img').forEach(function (im) { var ph = document.createElement('span'); ph.className = 'asst-imgph'; ph.textContent = '🖼 图片将在回答完成后显示'; if (im.parentNode) im.parentNode.replaceChild(ph, im); }); } catch (_) {}
    } else {
      try { window.rcImgStabilize && window.rcImgStabilize(el); } catch (_) {}
    }
  }
  // stream-fx(mfx):流式期间在回答末尾挂一个闪烁光标(renderMd 每 delta 重渲 innerHTML,故每次都补挂)
  function _appendCaret(el) { try { var c = document.createElement('span'); c.className = 'mfx-caret'; el.appendChild(c); } catch (_) {} }
  // 逐字浮现 —— 把 el 正文按 字/词 切片包进 .mfx-w(返回 {spans,total})。
  //   下标 < revN(揭示游标,已揭示)的字打 .mfx-shown → 即时显示,不重播(整段重渲下防闪);
  //   下标 ≥ revN 的字默认隐藏(CSS),由 _revealTick 揭示游标连续推进时逐个加 .mfx-reveal 淡入。
  //   这样"揭示节奏"由稳定速度的游标驱动,跟 SSE delta 的到达节奏解耦 → 真·连续逐字(不是段一段)。
  //   光标放在揭示 frontier(第 revN-1 个)后面。长答案(>5000 字)外层跳过逐字以保性能。
  function _streamWrap(el, revN) {
    var idx = 0, spans = [];
    function walk(node) {
      var kids = Array.prototype.slice.call(node.childNodes);
      for (var i = 0; i < kids.length; i++) {
        var n = kids[i];
        if (n.nodeType === 3) {                       // 文本节点 → 切 字/词 包 span
          var toks = (n.nodeValue || '').match(/[一-鿿　-〿＀-￯]|[A-Za-z0-9]+(?:['’][A-Za-z]+)?|[^\sA-Za-z0-9一-鿿　-〿＀-￯]|\s+/g) || [];
          if (!toks.length) continue;
          var frag = document.createDocumentFragment();
          toks.forEach(function (p) {
            if (/^\s+$/.test(p)) { frag.appendChild(document.createTextNode(p)); return; }
            var s = document.createElement('span'); s.className = 'mfx-w'; s.textContent = p;
            if (idx < revN) { s.classList.add('mfx-shown'); }   // 已揭示:即时
            frag.appendChild(s); spans.push(s); idx++;
          });
          node.replaceChild(frag, n);
        } else if (n.nodeType === 1 && n.className !== 'mfx-caret') {
          walk(n);
        }
      }
    }
    try { walk(el); } catch (_) { return { spans: [], total: idx }; }
    var f = spans[Math.min(revN, spans.length) - 1];
    var c = document.createElement('span'); c.className = 'mfx-caret';
    if (f && f.parentNode) { f.parentNode.insertBefore(c, f.nextSibling); } else { el.appendChild(c); }
    return { spans: spans, total: idx };
  }
  // 收尾:把追问 chip / 「!」反馈条做一次淡入(逐个错峰)
  function _fadeInAfter(el) {
    try {
      var xs = el.querySelectorAll('.asst-followups,.asst-fb-bar');
      Array.prototype.forEach.call(xs, function (x, k) {
        x.classList.add('mfx-after');
        setTimeout(function () { x.classList.add('on'); }, 80 + k * 120);
      });
    } catch (_) {}
  }
  // 从回答里剥离 [[FOLLOWUP]]q1|q2|q3[[/FOLLOWUP]] 追问建议(容忍流式中途未闭合)
  // ── 阶段5 门控:ui=shared → PdfAdapter.splitFollowups → rc-assistant.splitFollowups(纯解析,逐字等价);
  //   else 原逻辑逐字(_splitFollowupsNative)。只迁解析(纯函数无 DOM);渲染 _renderFollowups 留 native
  //   (PDF 的 _fadeInAfter 错峰淡入绑死 .asst-followups class,rc 版产出 .rc-fu-box → 迁渲染会丢淡入)。──
  function _splitFollowups(text) {
    if (window.__uiShared && window.PdfAdapter && PdfAdapter.splitFollowups)
      return PdfAdapter.splitFollowups(text, _splitFollowupsNative);
    return _splitFollowupsNative(text);
  }
  function _stripTornFU(t) {   // 尾部撕裂的 [[FOLLOWUP]] 前缀截掉:流式快照可能停在标记半截,留着会让
    t = String(t || '');       // 朗读 tap 把"标记补全后文本变短"误判成新一轮(整答重念)+ 把 "[[FOLLO" 喂给 TTS
    var TAG = '[[FOLLOWUP]]';
    for (var k = Math.min(TAG.length - 1, t.length); k >= 2; k--) {
      if (t.slice(-k) === TAG.slice(0, k)) return t.slice(0, -k);
    }
    return t;
  }
  function _splitFollowupsNative(text) {
    var fu = [];
    var push = function (body) { body.split(/[|\n]+/).forEach(function (q) { q = q.trim().replace(/^[\-·•\d\.\s]+/, ''); if (q) fu.push(q); }); };
    var clean = (text || '').replace(/\[\[FOLLOWUP\]\]([\s\S]*?)\[\[\/FOLLOWUP\]\]/g, function (m, body) { push(body); return ''; });
    var open = clean.indexOf('[[FOLLOWUP]]');   // 模型常漏结束标记 → 未闭合:从 [[FOLLOWUP]] 到结尾都当追问
    if (open >= 0) { push(clean.slice(open + 12).replace(/\[\[\/?FOLLOWUP\]\]/g, '')); clean = clean.slice(0, open); }
    return { text: clean.trim(), followups: fu.slice(0, 4) };
  }
  function _renderFollowups(afterEl, fus) {
    if (!fus || !fus.length) return;
    try { if (localStorage.getItem('asst-followups-on') !== '1') return; } catch (_) {}   // ★默认不显示(用户:放弃建议按钮;⚙ 模型设置里有开关)
    var box = document.createElement('div'); box.className = 'asst-followups';
    fus.forEach(function (q) {
      var b = document.createElement('button'); b.className = 'asst-fu';
      b.textContent = q;   // 纯文本占位;下面让 MathJax 就地把 chip 里的 $..$ 渲成公式(点击仍发原始 q)
      b.addEventListener('click', function () { if (!streaming) send(q); });
      box.appendChild(b);
    });
    _btmBar(afterEl).appendChild(box);   // 进底部操作行(▶/chips/! 同一行)
    try { if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([box]).catch(function () {}); } catch (_) {}   // 追问 chip 里的公式渲染
    scrollDown();
  }

  // ── 每条回答的「!」反馈:点开看这条回答经过了哪些 AI 调用(任务名 + 模型 + 耗时),再给三种调控 ──
  //  · 🎯 答得不够好 → 把「回答」动作的预设升一档 + 立刻用该档重答本题
  //  · 🐢 太慢了    → 把「回答」动作的预设调到「同质量更快」的档(不重答,只影响以后)
  //  · 每步 ⚙      → 直接给这个动作选 模型 + 深度(haiku/sonnet/opus × low…max),存为该动作预设
  // 速度/质量谱(Pareto,实测:opus·low ≈ sonnet·high 质量但更快 → 取代 sonnet·high;haiku 在最快端)
  // Pareto 清洗后的自动谱:每档=该质量下最快的配置。快端粗(haiku/sonnet·快不需要细分 effort);
  // 深端(opus)给全 effort 范围 low→max(含 medium)。sonnet·medium/high 被 opus·low 支配,故不入谱(⚙ 里仍可手选)。
  var _SPEC = [
    { model: 'haiku',  effort: 'low',    label: 'haiku·快' },
    { model: 'sonnet', effort: 'low',    label: 'sonnet·快' },
    { model: 'opus',   effort: 'low',    label: 'opus·快' },      // ≈sonnet·深 质量但实测更快 → 占这一档
    { model: 'opus',   effort: 'medium', label: 'opus·中' },
    { model: 'opus',   effort: 'high',   label: 'opus·深' },
    { model: 'opus',   effort: 'xhigh',  label: 'opus·更深' },
    { model: 'opus',   effort: 'max',    label: 'opus·max' }
  ];
  function _specIdx(m, e) { for (var i = 0; i < _SPEC.length; i++) if (_SPEC[i].model === m && _SPEC[i].effort === e) return i; return -1; }
  function _tierLabel(m, e) { var i = _specIdx(m, e); return i >= 0 ? _SPEC[i].label : (m + '·' + e); }
  function _curTier(trace) {   // 从 trace[0].model(如 "sonnet·high")解析本次「回答」动作档位
    try {
      var mm = String((trace && trace[0] && trace[0].model) || '').match(/([a-z]+)[^a-z]+([a-z]+)/i);
      if (mm) return { model: mm[1].toLowerCase(), effort: mm[2].toLowerCase() };
    } catch (_) {}
    return null;
  }
  // cur 在谱上的下标。sonnet·深(默认深答,不在谱上)按"质量≈opus·快"映射到 opus·快 的位置,使升/降一致。
  function _ladderIdxOf(cur) {
    if (!cur) return -1;
    if (cur.model === 'sonnet' && cur.effort === 'high') return _specIdx('opus', 'low');
    return _specIdx(cur.model, cur.effort);
  }
  function _strongerTier(cur) {   // 质量↑一档;null=已 opus·max;未知→默认 opus·深
    var i = _ladderIdxOf(cur);
    if (i < 0) { var d = _specIdx('opus', 'high'); return d >= 0 ? _SPEC[d] : _SPEC[_SPEC.length - 1]; }
    return (i + 1 < _SPEC.length) ? _SPEC[i + 1] : null;
  }
  function _fasterTier(cur) {   // 速度↑、尽量保质量;null=已 haiku·快;未知→sonnet·快
    // sonnet·深:同质量的更快档 = 直接换 opus·快(横向 Pareto 改进,你的洞见),不降质量
    if (cur && cur.model === 'sonnet' && cur.effort === 'high') { var o = _specIdx('opus', 'low'); return o >= 0 ? _SPEC[o] : _SPEC[1]; }
    var i = _ladderIdxOf(cur);
    if (i < 0) return _SPEC[1];
    return (i > 0) ? _SPEC[i - 1] : null;
  }
  var _fbOpenPop = null;
  function _fbClosePop() { if (_fbOpenPop) { try { _fbOpenPop.remove(); } catch (_) {} _fbOpenPop = null; } }
  document.addEventListener('click', function (e) {   // 点弹窗外任意处 → 收起
    if (_fbOpenPop && e.target && e.target.closest && !e.target.closest('.asst-fb-bar')) _fbClosePop();
  });
  // 给某动作存 (后端/型号/深度) 预设;backend 传 '' 清除回默认。跟感叹号「更强重答」共用此预设。
  function _setActionPref(action, backend, variant, depth, okMsg) {
    return fetch('/api/assistant/action-pref', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: action, backend: backend || '', variant: variant || '', depth: depth || '' }) })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (d && d.ok && typeof _toast === 'function') _toast(okMsg || '已设置'); return d; })
      .catch(function () {});
  }
  var _ACT_NAME = { orchestrator: '回答', summarize: '章节总结', vision: '看图' };
  // ── ⚙ 模型设置面板:列出各 AI 任务,每个可设 后端/型号/深度 ──
  var _DEPTH_LABEL = { auto: '自动(按问题)', low: 'low(快)', medium: 'medium', high: 'high(深)', xhigh: 'xhigh', max: 'max(最强)', none: '不思考', think: '思考' };
  var _BACKEND_LABEL = { claude: 'Claude', gemini: 'Gemini', codex: 'Codex(GPT)' };
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
    // 用户存的 variant 带 '@paid'(直连付费)不在 ListModels 清单里 → 插到对应裸型号后并给可读 label(同 rc-assistant)
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
    var _bl = (cat.backends_by_action && cat.backends_by_action[action]) || cat.backends;   // 该任务真正支持的后端(编排不接 Codex、语音深度只走 Claude——选不了就不会被静默降级坑)
    var selB = _msMkSel(_bl, cur.backend, _BACKEND_LABEL, lockB);
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
  // ── 阶段5 门控:ui=shared → PdfAdapter.openModelSettings → rc-assistant.openModelSettings
  //   (同组端点 /api/assistant/action-pref[s] + 同组 action 名,消 ~140 行逐字重复);
  //   else 原逻辑逐字(_openModelSettingsNative);RC 不可用 → fallback 回 native,绝不吞功能。──
  function openModelSettings(focusAction) {
    if (window.__uiShared && window.PdfAdapter && PdfAdapter.openModelSettings) {
      return PdfAdapter.openModelSettings({ focusAction: focusAction, fallback: function () { _openModelSettingsNative(focusAction); } });
    }
    if (window.RC && RC.assistant && RC.assistant.openModelSettings) {
      // EPUB(EpubHtmlAdapter,无 window.PdfAdapter):直接用共享 Tab 版(阅读 AI/语音通话·朗读)。
      // 此前门控只认 PdfAdapter → EPUB 掉进 native 旧浮层=「EPUB 设置无语音 Tab」根因(2026-07-13 用户截图实锤)
      return RC.assistant.openModelSettings(focusAction);
    }
    return _openModelSettingsNative(focusAction);
  }
  function _openModelSettingsNative(focusAction) {
    fetch('/api/assistant/action-prefs').then(function (r) { return r.json(); }).then(function (d) {
      if (!d || !d.ok) { if (typeof _toast === 'function') _toast('拉取设置失败'); return; }
      var mask = document.createElement('div'); mask.className = 'ams-mask';
      mask.addEventListener('click', function (e) { if (e.target === mask) mask.remove(); });
      var box = document.createElement('div'); box.className = 'ams-box';
      var h = document.createElement('div'); h.className = 'ams-h';
      var ht = document.createElement('span'); ht.textContent = '⚙ AI 模型设置';
      var x = document.createElement('button'); x.className = 'ams-x'; x.textContent = '×';
      x.addEventListener('click', function () { mask.remove(); });
      h.appendChild(ht); h.appendChild(x); box.appendChild(h);
      var sub = document.createElement('div'); sub.className = 'ams-sub';
      sub.textContent = '每个任务可单独设 后端/型号/深度,改完即时生效。跟感叹号「更强重答」共用同一套预设。';
      box.appendChild(sub);
      var _focusCard = null;
      function _renderActs(list) {
        list.forEach(function (a) {
          var ai = d.actions[a]; if (!ai) return;
          var c = _buildMsTask(a, { pref: ai.pref, def: ai.default }, d.catalog, d.names, d.locked || {});
          if (a === focusAction) _focusCard = c;   // 从某步⚙进来 → 定位到对应任务卡
          box.appendChild(c);
        });
      }
      _renderActs(['orchestrator', 'summarize', 'vision', 'deep']);
      // PDF 阅读器其它 AI 入口(解释/翻译/字典/语法),跟助手共用同一套脱壳 Claude + Gemini 双后端预设
      var _rh = document.createElement('div'); _rh.className = 'ams-sub';
      _rh.style.cssText = 'margin-top:12px;font-weight:600;color:#9fc0ff;';
      _rh.textContent = '— PDF 阅读器其它 AI —';
      box.appendChild(_rh);
      _renderActs(['explain', 'translate', 'dict', 'grammar', 'pick_video']);   // pick_video=找视频拟词+相关性筛选
      var note = document.createElement('div'); note.className = 'ams-note';
      note.textContent = '标「免费」= 免费档支持该型号;但免费是**共享算力**,高峰常过载(503)或限流时会自动落付费保不中断——'
        + '此时这里会标「付费(过载/限流)」、感叹号里也显付费。「💰仅付费」= 该型号免费档没有(如 3.1-pro),'
        + '选它每次调用都按量计费。flash 高峰过载较多;想更稳的免费可试 flash-lite 系。';
      box.appendChild(note);
      mask.appendChild(box); document.body.appendChild(mask);
      if (_focusCard) { try { _focusCard.style.outline = '2px solid #6aa3ff'; _focusCard.style.borderRadius = '8px'; _focusCard.scrollIntoView({ block: 'center' }); } catch (_) {} }
    }).catch(function () { if (typeof _toast === 'function') _toast('拉取设置失败'); });
  }
  try { window.openModelSettings = openModelSettings; } catch (_) {}   // 供 PDF 总设置面板调起
  function _buildFbPop(question, trace, close, ts) {
    var pop = document.createElement('div'); pop.className = 'asst-fb-pop';
    var h = document.createElement('div'); h.className = 'afp-h';
    h.textContent = (trace && trace.length) ? '这条回答经过的 AI 调用' : '对这条回答不满意?';
    pop.appendChild(h);
    var _tot = 0;
    // 无 trace(早期回答没存调用轨迹)→ 兜底合成一个「回答」步,保证每条都至少有「回答」动作的 ⚙
    var steps = (trace && trace.length) ? trace : [{ label: '回答', model: '', action: 'orchestrator' }];
    steps.forEach(function (st) {
      var row = document.createElement('div'); row.className = 'afp-step';
      var l = document.createElement('span'); l.className = 'afp-l'; l.textContent = st.label || '步骤';   // 任务名
      if (st.detail) {   // 这步有完整内容 → 步骤名变可点按钮,点开/收起显示该步的完整 AI 产出
        l.classList.add('afp-l-btn'); l.title = '点开看这一步的完整内容';
        l.addEventListener('click', function (e) {
          e.stopPropagation();
          var ex = row.nextSibling;
          if (ex && ex.classList && ex.classList.contains('afp-detail')) { ex.remove(); return; }   // 再点收起
          var dt = document.createElement('div'); dt.className = 'afp-detail'; dt.textContent = st.detail;
          row.parentNode.insertBefore(dt, row.nextSibling);
        });
      }
      var m = document.createElement('span'); m.className = 'afp-m';
      if (typeof st.sec === 'number') _tot += st.sec;
      var mt = st.model || '';
      m.textContent = mt + (typeof st.sec === 'number' ? (mt ? ' · ' : '') + st.sec + 's' : '');   // 模型 · 耗时(老回答可能都没有)
      if (st.tier === 'free' || st.tier === 'paid') {   // Gemini 实际服务这条用了哪档 → 标「免费/付费」
        var tg = document.createElement('span'); tg.textContent = st.tier === 'paid' ? '付费' : '免费';
        tg.style.cssText = 'margin-left:6px;padding:0 6px;border-radius:6px;font-size:11px;vertical-align:middle;'
          + (st.tier === 'paid' ? 'background:#5a3a1a;color:#ffcf8f;' : 'background:#1f4a2e;color:#8fe3a8;');
        m.appendChild(tg);
      }
      row.appendChild(l); row.appendChild(m);
      if (st.action) {   // 这一步是会调模型的动作 → 给个 ⚙ 直接设它的预设
        var g = document.createElement('button'); g.className = 'afp-gear-btn'; g.textContent = '⚙'; g.title = '设这个动作的模型/深度';
        g.addEventListener('click', function (e) {
          e.stopPropagation();
          _fbClosePop();   // 收起感叹号弹窗
          // 打开统一三维设置面板(支持 Claude/Gemini + 免费/付费标),定位到本动作 —— 不再是只有 Claude 的简易档
          try { openModelSettings(st.action); } catch (_) {}
        });
        row.appendChild(g);
      }
      pop.appendChild(row);
    });
    if (ts || _tot) {   // 页脚:完成时刻 + 总耗时(时间只要有 ts 就显示,不依赖 trace)
      var ft = document.createElement('div'); ft.className = 'afp-foot';
      var bits = [];
      if (ts && typeof _qhFmtTime === 'function') { try { bits.push('🕐 ' + _qhFmtTime(ts * 1000)); } catch (_) {} }
      if (_tot) bits.push('共 ' + (Math.round(_tot * 10) / 10) + 's');
      if (bits.length) { ft.textContent = bits.join(' · '); pop.appendChild(ft); }
    }
    var cur = _curTier(trace);
    var up = _strongerTier(cur);     // null = 已最强
    var down = _fasterTier(cur);     // null = 已最快
    var acts = document.createElement('div'); acts.className = 'afp-acts';
    // 去掉了🎯升档/🐢调快的爬梯子;只留一个「模型设置」按钮 → 打开统一三维设置面板(后端/型号/深度)
    var bSet = document.createElement('button'); bSet.className = 'afp-act afp-q';
    bSet.textContent = '⚙ 模型设置';
    bSet.addEventListener('click', function () { close(); try { openModelSettings('orchestrator'); } catch (_) {} });   // 无工具轮的「!」=编排信息+编排设置直达(工具轮的调用链已并入卡片流程)
    acts.appendChild(bSet); pop.appendChild(acts);
    return pop;
  }
  function _attachFeedback(bubble, question, trace, ts) {
    if (!bubble) return;
    try { var old = bubble.querySelector('.asst-fb-bar'); if (old) old.remove(); } catch (_) {}   // 重渲时防重复挂
    var bar = document.createElement('div'); bar.className = 'asst-fb-bar';
    var _tok = trace && trace[0] && trace[0].tok;   // 本轮累计 token → 显示「3.6k tok」
    if (_tok) {
      var tk = document.createElement('span'); tk.className = 'asst-tok';
      tk.textContent = (_tok >= 1000 ? (_tok / 1000).toFixed(1).replace(/\.0$/, '') + 'k' : _tok) + ' tok';
      tk.title = '这条回答累计消耗 token：' + _tok;
      bar.appendChild(tk);
    }
    var btn = document.createElement('button'); btn.className = 'asst-fb-btn'; btn.textContent = '!'; btn.title = '对这条回答不满意?';
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      if (_fbOpenPop && _fbOpenPop._owner === btn) { _fbClosePop(); return; }
      _fbClosePop();
      var pop = _buildFbPop(question, trace, _fbClosePop, ts); pop._owner = btn;
      bar.appendChild(pop); _fbOpenPop = pop;   // 不再 scrollDown:历史回答在中间时,点开不该把视图拽到底
    });
    bar.appendChild(btn); _btmBar(bubble).appendChild(bar);   // 进底部操作行(与 ▶/chips 同一行,不再独占一行)
  }
  // 回答底部操作行:▶ / 追问chips / tok+「!」排**一行**(用户:别占多行)。幂等取/建。
  function _btmBar(el) {
    var b = el.querySelector(':scope > .asst-btm');
    if (!b) { b = document.createElement('div'); b.className = 'asst-btm'; el.appendChild(b); }
    return b;
  }

  document.addEventListener('click', function (e) {   // 点回答里的页码链接 → 跳页 + 底部回到条
    var t = e.target;
    if (t && t.classList && t.classList.contains('asst-pagelink') && t.dataset.page && typeof window.jumpWithBack === 'function') {
      // AI 写的「第N页」是**书上印刷页码** → 跳转前转回 PDF 页索引(过本书页码对齐偏移)
      var _pg = (typeof window._pdfFromDisp === 'function') ? HOST.pdfFromDisp(t.dataset.page) : parseInt(t.dataset.page, 10);
      HOST.goTo(_pg);
    }
  });
  function scrollDown() { thread.scrollTop = thread.scrollHeight; }
  function addMsg(cls, html) { var d = document.createElement('div'); d.className = 'asst-msg ' + cls; d.innerHTML = html; thread.appendChild(d); scrollDown(); return d; }

  // 视口焦点:当前与 #main 视口相交的页的字符层文字(镜像 EPUB _visibleText)。让 AI 回答/找视频/配图/拟搜索词
  // 都紧扣"用户此刻在看的这段",而非泛泛的整页/整章主题(后端 _sys_prompt 的「紧扣可见段落」指引靠它才生效)。
  function _visibleText() {
    try {
      var main = document.getElementById('main'); if (!main) return '';
      var mr = main.getBoundingClientRect(), top = mr.top, bot = mr.bottom;
      var pws = document.querySelectorAll('.page-wrap[data-page-num]'), parts = [];
      for (var i = 0; i < pws.length; i++) {
        var pw = pws[i], r = pw.getBoundingClientRect();
        if (r.height && r.bottom > top + 8 && r.top < bot - 8 && pw.__charBoxes && pw.__charBoxes.length) {   // 与视口相交
          var t = ''; try { t = _charsRangeToText(pw.__charBoxes, 0, pw.__charBoxes.length - 1); } catch (e) {}
          t = (t || '').replace(/\s+/g, ' ').trim();
          if (t) parts.push(t);
        }
      }
      var txt = parts.join('\n');
      return txt.length > 1000 ? txt.slice(0, 1000) + '…' : txt;
    } catch (e) { return ''; }
  }

  function ctx() {
    var c = { page_type: 'pdf' };
    // 取阅读器当前上下文经统一中间层 RC.adapter().getContext()(PdfAdapter 只读包 __voiceContext);
    // 中间层不可用(legacy 无 adapter / RC 未加载)→ 回退直连 __voiceContext。便签合并见下方(消费侧,不变)。
    try {
      var g = (window.RC && RC.adapter && RC.adapter().getContext) ? RC.adapter().getContext() : null;
      c = g || ((typeof window.__voiceContext === 'function') ? (HOST.voiceContext() || c) : c);
    } catch (_) {}
    try { if (c && !c.visible_text) c.visible_text = _visibleText(); } catch (_) {}   // 视口焦点(镜像 EPUB 2516):AI 找视频/配图/回答紧扣当前屏幕,不退回泛章节
    // 便签注入(双击便签 → __noteAttached,见下方注入块):无笔画=文字+锚点附近正文走 context.notes 文本通道;
    // 有笔画=kind:'note' 条目并入 figures 走视觉通道(服务端 see_figure 认 note_id → _note_composite_png 现场合成)
    try {
      var atts = window.__noteAttached || [];
      var txtNotes = [], inkNotes = [];
      atts.forEach(function (n) {
        if (n.has_ink) {
          inkNotes.push({ kind: 'note', note_id: n.id, page: n.page || 0, caption: '手写便签',
                          desc: String(n.text || '').slice(0, 300), near: String(n.near || '').slice(0, 600),
                          file_rel: (typeof HOST.fileRel() !== 'undefined' ? HOST.fileRel() : ''), has_ink: true });
        } else {
          txtNotes.push({ id: n.id, text: String(n.text || '').slice(0, 2000), near: String(n.near || '').slice(0, 1200), page: n.page || 0 });
        }
      });
      if (txtNotes.length) c.notes = txtNotes.slice(0, 4);
      if (inkNotes.length) c.figures = ((c.figures || []).concat(inkNotes.slice(0, 4))).slice(0, 6);
    } catch (_) {}
    return c;
  }

  // ── 便签注入(阶段3,设计见 references/sticky-notes-design.md 用户规格8):双击便签(rc-stickynote onDoubleTap,
  //   经 27-rc-adapter 接到这里)→ 加入 __noteAttached + 输入框上方 chip(挨图附件条,同款视觉,✕ 移除)。
  //   chip 生命周期同图附件条:发送时定格进 ctx、发完即清。──
  window.__noteAttached = [];   // [{id,text,near,page,has_ink,_thumb}](_thumb=合成图 data_url,仅前端显示不随请求发)
  function _renderNoteChips() {
    try {
      var paneEl = document.getElementById('side-pane-asst');
      var input = paneEl && paneEl.querySelector('#asst-input');
      var list = window.__noteAttached || [];
      var wrap = document.getElementById('asst-note-chips');
      if (!list.length) { if (wrap) wrap.remove(); return; }
      if (!input) return;   // 助手还没建 → 数据仍在,开了再渲
      if (!wrap) {
        wrap = document.createElement('div'); wrap.id = 'asst-note-chips';
        wrap.style.cssText = 'display:flex;flex-wrap:wrap;gap:6px;padding:6px 10px 0';
        input.parentNode.insertBefore(wrap, input);
      }
      wrap.innerHTML = '';
      list.forEach(function (n) {
        var chip = document.createElement('div'); chip.className = 'asst-fig-chip';
        if (n.has_ink) {
          var img = document.createElement('img'); img.className = 'afc-thumb'; img.alt = '';
          if (n._thumb) {
            img.src = n._thumb; img.style.cursor = 'zoom-in';
            img.addEventListener('click', function () {   // 点缩略图看大图(复用 26-figures 的 .fig-lightbox 样式)
              var mask = document.createElement('div'); mask.className = 'fig-lightbox';
              var big = document.createElement('img'); big.src = n._thumb; big.alt = '';
              mask.appendChild(big); document.body.appendChild(mask);
              mask.addEventListener('click', function () { mask.remove(); });
            });
          }
          chip.appendChild(img);
        } else {
          var ic = document.createElement('span'); ic.textContent = '🗒'; ic.style.cssText = 'flex:none;font-size:15px'; chip.appendChild(ic);
        }
        var t = String(n.text || '').replace(/\s+/g, ' ').trim();
        var cap = document.createElement('span'); cap.className = 'afc-cap';
        cap.textContent = n.has_ink ? ('手写便签' + (t ? ' · ' + t.slice(0, 14) : '')) : (t.slice(0, 20) || '便签');
        var x = document.createElement('button'); x.className = 'afc-x'; x.textContent = '✕';
        x.addEventListener('click', function () { window.__noteAttached = (window.__noteAttached || []).filter(function (z) { return z.id !== n.id; }); _renderNoteChips(); });
        chip.appendChild(cap); chip.appendChild(x); wrap.appendChild(chip);
      });
    } catch (_) {}
  }
  window.__renderNoteChips = _renderNoteChips;
  window.__clearNoteAttached = function () { window.__noteAttached = []; _renderNoteChips(); };
  function _noteFetchThumb(entry) {
    setTimeout(function () {   // 稍等 rc-stickynote 的文字/笔画 PATCH 先落库,合成图才含最新内容
      fetch(_NCURL, { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ file: (typeof HOST.fileRel() !== 'undefined' ? HOST.fileRel() : ''), id: entry.id }) })
        .then(function (r) { return r.json(); })
        .then(function (d) { if (d && d.ok && d.data_url) { entry._thumb = d.data_url; _renderNoteChips(); } })
        .catch(function () {});
    }, 350);
  }
  // 锚点附近正文(contextAt):便签所在页字符层里,离锚点 y 最近的字符前后各 ±600 字
  function _noteNearText(anchor) {
    try {
      if (!anchor || anchor.kind !== 'pdf') return '';
      var pw = document.querySelector('.page-wrap[data-page-num="' + anchor.page + '"]');
      var ch = pw && pw.__charBoxes;
      if (!ch || !ch.length) return '';
      var yPx = Math.max(0, Math.min(1, anchor.y || 0)) * (pw.clientHeight || 1);
      var best = 0, bestD = Infinity;
      for (var i = 0; i < ch.length; i++) {
        var d = Math.abs((ch[i].top + ch[i].height / 2) - yPx);
        if (d < bestD) { bestD = d; best = i; }
      }
      return _charsRangeToText(ch, Math.max(0, best - 600), Math.min(ch.length - 1, best + 600)).slice(0, 1300);
    } catch (_) { return ''; }
  }
  window.__noteInject = function (note) {
    try {
      if (!note || !window.__asstOpen || !HOST.asstOpen()) return false;   // 助手没开 → 维持现状(不注入)
      var list = window.__noteAttached = window.__noteAttached || [];
      var hasInk = !!(note.strokes && note.strokes.length);
      var old = null;
      for (var i = 0; i < list.length; i++) if (list[i].id === note.id) old = list[i];
      if (old) {   // 已在附件条 → 只刷新内容(文字/笔画可能变了),不重复加
        old.text = note.text || '';
        if (hasInk && !old.has_ink) { old.has_ink = true; old._thumb = ''; }
        if (old.has_ink) _noteFetchThumb(old);
        _renderNoteChips();
        if (typeof _toast === 'function') _toast('已在对话上下文');
        return true;
      }
      var entry = { id: note.id, text: note.text || '', near: _noteNearText(note.anchor),
                    page: (note.anchor && note.anchor.page) || 0, has_ink: hasInk, _thumb: '' };
      list.push(entry);
      _renderNoteChips();
      if (entry.has_ink) _noteFetchThumb(entry);
      if (typeof _toast === 'function') _toast('🗒 便签已带进对话');
      return true;
    } catch (_) { return false; }
  };

  // 点击历史/上下文卡片 → 跳到那一页(同书走 jumpWithBack 带「回到」条;跨书则打开那本书定位到页)
  function _jumpToCtx(file_rel, page) {
    page = parseInt(page, 10); if (!page || page < 1) return;
    var cur = (typeof HOST.fileRel() !== 'undefined') ? HOST.fileRel() : '';
    if (file_rel && cur && file_rel !== cur) { location.href = '/pdf/view?file=' + encodeURIComponent(file_rel) + '&page=' + page; return; }   // 跨书:正确路由是 /pdf/view(/pdf/ 是书架)
    if (typeof window.jumpWithBack === 'function') HOST.goTo(page);
  }
  // ③ 上下文卡「选中」跳页后:目标页字符层里找到这段文字 → 临时呼吸高亮几秒后自动移除。
  // 机制**复用不重写**:高亮走 15-phrase 的 _activePhraseHl 状态 + renderPhraseHl 渲染管线
  // (绝对定位进 page-wrap 内容坐标系随页滚动,08-charlayer 页重渲还会自动补画,铁律2/3);
  // 等页字符层就绪的重试节奏照搬 11-search 的 _applyPendingSearchHighlight。找不到文字(公式
  // 选区/OCR 差异)就只跳页不闪。定时移除前校验高亮仍是本次的,不误删用户随后发起的词组查询高亮。
  var _ctxSelFlashT = null;
  function _flashSelOnPage(page, text, tries) {
    page = parseInt(page, 10); text = String(text || '').trim();
    if (!page || !text) return;
    tries = tries || 0;
    var wrap = document.querySelector('[data-page-num="' + page + '"]');
    if (!(wrap && wrap.dataset.loaded === '1' && wrap.__charBoxes && wrap.__charBoxes.length)) {
      if (tries < 30) setTimeout(function () { _flashSelOnPage(page, text, tries + 1); }, 160);   // 最多 ~4.8s(同搜索)
      return;
    }
    try {
      var chars = wrap.__charBoxes;
      var full = chars.map(function (c) { return c.c || ''; }).join('').toLowerCase();
      var i = full.indexOf(text.toLowerCase());
      if (i < 0) return;
      var rects = _charRangeToPtRects(chars, i, i + text.length - 1);   // 起止含端点(同 _showPhraseHighlight 的 startIdx..endIdx)
      if (!rects.length) return;
      HOST.setActivePhraseHl({ page: page, text: text, rects: rects, solid: false });   // 追加临时闪烁高亮(多高亮:不清别的并存高亮)
      renderPhraseHl(wrap);
      var mine = HOST.activePhraseHl();   // = 刚 push 的那个(带 id)
      var first = wrap.querySelector('.phrase-hl-layer[data-phid="' + (mine && mine.id) + '"] .hl');
      if (first) { try { first.scrollIntoView({ block: 'center' }); } catch (_) {} }
      clearTimeout(_ctxSelFlashT);
      _ctxSelFlashT = setTimeout(function () { try { _removePhraseHighlight(mine); } catch (_) {} }, 5000);   // 5s 后只删这个临时高亮(幂等;被点掉/换书已删则 no-op)
    } catch (_) {}
  }
  // open_book 工具的 client_action 用:打开另一本书(可定位页)
  window.openBookAt = function (fr, pg) { try { _jumpToCtx(fr, parseInt(pg, 10) || 1); } catch (_) {} };
  // 一条用户消息的上下文卡片:用过的图缩略图 + 选中的字段 + 涉及的页码,点任意一处都能跳过去
  // meta:{figures:[{file_rel,page,box,caption,group,has_ink}], selection, page, file_rel}; live=刚发的那条(图有笔迹走实时合成)
  // 问题是否跟「本页内容」相关:有选中/图(由它们承载跳转),或问题文字含本页指代 → 才给页码按钮;
  // 跟本页无关的纯问题(如"什么是特征值")不带页码 chip
  function _pageRefersToPage(msg) {
    var m = msg || '';
    return /这一?页|本页|此页|当前页|这段|这里|这张?图|这幅图|如[下图]图?|上面这?|这个公式|这道?题|本章|这一?章|这一?节|本节|页面|图里|图中|这部分/.test(m)
        || /\bthis (page|figure|fig|section|paragraph|chapter|image|diagram|part)\b|\bhere\b/i.test(m);
  }
  function _ctxCard(meta, live, msg) {
    if (!meta) return null;
    var figs = (meta.figures || []).filter(function (f) { return f && (f.box || f.src); });   // PDF=box 裁图 / EPUB=图自带 src(epub-res)
    var sel = (meta.selection || '').trim();
    var page = parseInt(meta.page, 10) || 0;
    var bookRel = meta.file_rel || (typeof HOST.fileRel() !== 'undefined' ? HOST.fileRel() : '');
    // 页码 chip:有选中/图时由它们跳转(不重复给);否则仅当问题确实指向本页才给
    var showPage = page && !figs.length && !sel && _pageRefersToPage(msg);
    // 章 chip(EPUB 路径):host 有 locLabel 且 meta 带节 idx(发送=current_section_idx / 历史=section)→
    // 恒显「正在看:章名」可点跳回(镜像旧内联 UX;PDF 无 locLabel → 走原 page 路径零变化)
    var secIdx = (!page && HOST.locLabel)
      ? (meta.section != null ? meta.section : (meta.current_section_idx != null ? meta.current_section_idx : null)) : null;
    if (!figs.length && !sel && !showPage && secIdx == null) return null;
    var card = document.createElement('div'); card.className = 'asst-ctx-card';
    if (secIdx != null) {
      var sc = document.createElement('span'); sc.className = 'actx-page';
      sc.textContent = '📖 正在看:' + (HOST.locLabel(secIdx) || ('第 ' + (secIdx + 1) + ' 节'));
      sc.title = '点击跳到该章节';
      sc.addEventListener('click', function () { try { HOST.goTo(secIdx); } catch (_) {} });
      card.appendChild(sc);
    }
    if (figs.length) {
      var row = document.createElement('div'); row.className = 'actx-thumbs';
      figs.forEach(function (f) {
        var fr = f.file_rel || bookRel;
        var img = document.createElement('img'); img.className = 'actx-thumb'; img.alt = '';
        img.title = (f.group ? '图组 · ' : '') + (f.caption || '图') + (f.page != null ? (' · p' + f.page) : '') + ' · 点击跳转';
        if (f.src && !f.box) img.src = f.src;   // EPUB:图自带 src(epub-res 直链),不走 PDF 裁图
        else if (typeof window.__figThumb === 'function') HOST.figThumb({ file_rel: fr, page: f.page, box: f.box, has_ink: f.has_ink }, img, live);
        img.addEventListener('click', function () {
          if (f.page != null) { _jumpToCtx(fr, f.page); return; }
          var _fs = parseInt(f.section, 10);   // EPUB:跳图所在节(section 可能是 userpage 字符串 uid → 跳不了就不动)
          if (!isNaN(_fs)) { try { HOST.goTo(_fs); } catch (_) {} }
        });
        row.appendChild(img);
      });
      card.appendChild(row);
    }
    if (sel) {
      var s = document.createElement('div'); s.className = 'actx-sel';
      if (/^\$\$?[\s\S]+\$\$?$/.test(sel)) {   // 公式选区($..$/$$..$$)→ MathJax 渲染,不显示成裸 LaTeX
        s.classList.add('actx-fml');
        var _raw = sel.replace(/^\$\$?/, '').replace(/\$\$?$/, '');
        var _block = /^\$\$/.test(sel) || /\\begin\{|\\\\/.test(_raw);
        s.textContent = _block ? ('\\[' + _raw + '\\]') : ('\\(' + _raw + '\\)');   // 跟公式浮层一致,避免单 $ 行内未启用
        if (window.MathJax && MathJax.typesetPromise) setTimeout(function () { try { MathJax.typesetPromise([s]); } catch (_) {} }, 0);
      } else {
        s.textContent = '“' + (sel.length > 64 ? sel.slice(0, 64) + '…' : sel) + '”';
      }
      s.title = page ? ('跳到第 ' + ((typeof window._dispPage === 'function') ? HOST.dispPage(page) : page) + ' 页') : '';
      s.addEventListener('click', function () {   // ③ 跳页后把这段选中在页上临时呼吸高亮(跨书整页跳走,不闪)
        // EPUB 路径(无 page):选区锚(selection_anchor.section)/当前节 → host flashSelOnPage(跳节+呼吸高亮)
        if (!page && secIdx != null && HOST.flashSelOnPage) {
          var _ss = (meta.selection_anchor && meta.selection_anchor.section != null) ? meta.selection_anchor.section : secIdx;
          try { HOST.flashSelOnPage(_ss, sel); } catch (_) {}
          return;
        }
        _jumpToCtx(bookRel, page);
        var curF = (typeof HOST.fileRel() !== 'undefined') ? HOST.fileRel() : '';
        if (page && (!bookRel || !curF || bookRel === curF)) _flashSelOnPage(page, sel);
      });
      card.appendChild(s);
    }
    if (showPage) {
      var pg = document.createElement('span'); pg.className = 'actx-page';
      pg.textContent = '📄 第 ' + ((typeof window._dispPage === 'function') ? HOST.dispPage(page) : page) + ' 页';   // 存的是 PDF 页 → 显印刷页
      pg.addEventListener('click', function () { _jumpToCtx(bookRel, page); });
      card.appendChild(pg);
    }
    return card;
  }

  function runActions(actions) {
    if (!actions || !actions.length) return;
    actions.forEach(function (a) { try { if (a && a.fn && typeof window[a.fn] === 'function') window[a.fn].apply(null, a.args || []); } catch (_) {} });
  }
  // 助手「列出可删高亮」工具产生 → 在对话里逐条渲染:色块 + 文字 + 「↗跳转」+「🗑删除」。用户点跳转看/点删除移除(不替他删)。
  window._showHlPicker = function (d) {
    try {
      if (!d || !Array.isArray(d.items) || !d.items.length) return;
      var fileRel = d.file_rel || '';
      var box = document.createElement('div'); box.className = 'asst-msg asst-a';
      var head = document.createElement('div'); head.style.cssText = 'margin-bottom:4px;opacity:.85;font-size:12.5px';
      head.textContent = '共 ' + d.items.length + ' 处高亮 —— 点「跳转」去看,点「删除」移除:';
      box.appendChild(head);
      d.items.forEach(function (it) {
        var row = document.createElement('div'); row.className = 'asst-hl-row';
        var sw = document.createElement('span'); sw.className = 'asst-hl-sw'; sw.style.background = it.color || '#fff59d';
        var tx = document.createElement('span'); tx.className = 'asst-hl-tx';
        tx.textContent = '第' + it.page + '页 · ' + (it.text || '(无文字)');   // it.page=印刷页(显示)
        tx.title = it.text || '';
        var _jp = (it.pdf_page != null) ? it.pdf_page : it.page;   // 跳转用 PDF 页(jumpWithBack 收 PDF 页)
        var jb = document.createElement('button'); jb.className = 'asst-jump'; jb.setAttribute('data-page', _jp); jb.textContent = '↗ 跳转';
        var db2 = document.createElement('button'); db2.className = 'asst-hl-del';
        db2.setAttribute('data-id', it.id); db2.setAttribute('data-file', fileRel);
        try { db2.setAttribute('data-hl', JSON.stringify({ page: _jp, rects: it.rects || [], color: it.color, text: it.text || '' })); } catch (_) {}   // M9:删完「↪重做」重建用
        db2.textContent = '🗑 删除';
        row.appendChild(sw); row.appendChild(tx); row.appendChild(jb); row.appendChild(db2);
        box.appendChild(row);
      });
      thread.appendChild(box); scrollDown();
    } catch (_) {}
  };
  // agent 画完高亮后:重新拉高亮 + 重渲所有可见页(复用 17-highlight 的模块函数,本模块同作用域可调)
  window._reloadHighlights = async function () {
    try {
      if (typeof loadAllHighlights === 'function') await loadAllHighlights();
      document.querySelectorAll('.page-wrap').forEach(function (pw) {
        var n = parseInt(pw.dataset.pageNum); if (n && typeof renderHighlightsOnPage === 'function') renderHighlightsOnPage(pw, n);
      });
    } catch (_) {}
  };
  // AI 建/改便签、撤销/重做后:重挂页面便签(rc-stickynote.loadAll 幂等全量;legacy 模式无 RC 则静默跳过)。
  // 后端 notes_create/notes_edit/undo_last 的 client_action {fn:'notesReload'} 也走这里(runActions → window[fn])。
  window.notesReload = function () {
    try { if (window.RC && RC.stickynote && RC.stickynote.loadAll) RC.stickynote.loadAll(); } catch (_) {}
  };
  // ── 改动发生时**自动**生成「跳转 + 撤销/重做」卡片(系统在高亮/便签写入时生成,非 AI 文本生成)──
  var _assistEdits = {}, _aeCtr = 0;
  window._assistEdit = function (d) {
    try {
      if (!d || !Array.isArray(d.items) || !d.items.length) return;
      if (d.type === 'note') return _assistNoteCard(d);   // 便签写操作(notes_create/notes_edit)→ 便签版卡
      if (d.type !== 'highlight') return;
      try { window._reloadHighlights && HOST.reloadHighlights(); } catch (_) {}   // 先把刚画的高亮渲出来
      // ★用户设计:融入 turn 卡系统 —— hlcard part(头行可点展开 → 逐条[原文|↗跳转|↩撤销⇄↪重做]),
      //   走唯一渲染器 + parts 落库(旧独立卡刷新即丢;这个刷新回放仍可操作)。RC 不可用才落到下面 legacy 卡。
      try {
        if (window.RC && RC.turnCard && window.__asstVoiceTid) {
          RC.turnCard.addPart(window.__asstVoiceTid(), { kind: 'hlcard', file: d.file || '', items: d.items.slice() });
          return;
        }
      } catch (_) {}
      var eid = 'ae' + (++_aeCtr);
      _assistEdits[eid] = { file: d.file || '', items: d.items.slice(),
                            ids: d.items.map(function (it) { return it.id; }).filter(Boolean), undone: false };
      var pages = [], seen = {};
      d.items.forEach(function (it) {
        var dp = (it.disp_page != null) ? it.disp_page : it.pdf_page;
        if (dp != null && !seen[dp]) { seen[dp] = 1; pages.push({ disp: dp, pdf: (it.pdf_page != null ? it.pdf_page : dp) }); }
      });
      var card = document.createElement('div'); card.className = 'asst-edit-card';
      var head = document.createElement('div'); head.className = 'asst-edit-h';
      head.textContent = '✏️ 已高亮 ' + d.items.length + ' 处' + (pages.length ? '（第 ' + pages.map(function (p) { return p.disp; }).join('、') + ' 页）' : '');
      card.appendChild(head);
      if (pages.length) {
        var chips = document.createElement('div'); chips.className = 'asst-edit-chips';
        pages.forEach(function (p) {
          var c = document.createElement('button'); c.className = 'asst-jump';
          c.setAttribute('data-page', p.pdf); c.textContent = '→ 第' + p.disp + '页'; chips.appendChild(c);
        });
        card.appendChild(chips);
      }
      var btn = document.createElement('button'); btn.className = 'asst-edit-undo';
      btn.setAttribute('data-eid', eid); btn.textContent = '↩ 撤销';
      card.appendChild(btn);
      thread.appendChild(card); scrollDown();
    } catch (_) {}
  };
  // 便签写操作的「跳转 + 撤销⇄重做」卡(同高亮卡形态;后端 notes_create/notes_edit 的 client_action 触发):
  //   create:撤销=DELETE 该便签,重做=POST 快照重建(拿新 id 接管撤销);edit:撤销=PATCH 旧 text/color,重做=PATCH 新值。
  //   任何一步都只碰 text/color/整条,绝不动 strokes/anchor/尺寸。
  function _assistNoteCard(d) {
    try {
      HOST.notesReload();   // 先把刚建/改的便签渲出来
      var eid = 'ae' + (++_aeCtr);
      _assistEdits[eid] = { ntype: 'note', op: d.op || 'create', file: d.file || '', items: d.items.slice(), undone: false };
      var it0 = d.items[0] || {};
      var card = document.createElement('div'); card.className = 'asst-edit-card';
      var head = document.createElement('div'); head.className = 'asst-edit-h';
      head.textContent = '🗒 ' + (d.op === 'edit' ? '已修改便签' : '已创建便签') + (it0.disp_page ? '（第 ' + it0.disp_page + ' 页）' : '');
      card.appendChild(head);
      if (it0.pdf_page) {
        var chips = document.createElement('div'); chips.className = 'asst-edit-chips';
        var c = document.createElement('button'); c.className = 'asst-jump';
        c.setAttribute('data-page', it0.pdf_page); c.textContent = '→ 第' + (it0.disp_page || it0.pdf_page) + '页';
        chips.appendChild(c); card.appendChild(chips);
      }
      var btn = document.createElement('button'); btn.className = 'asst-edit-undo';
      btn.setAttribute('data-eid', eid); btn.textContent = '↩ 撤销';
      card.appendChild(btn);
      thread.appendChild(card); scrollDown();
    } catch (_) {}
  }
  // 便签卡的撤销⇄重做执行(点 .asst-edit-undo 且 st.ntype==='note' 时走这;完成后重挂页面便签)
  function _noteEditToggle(st, eb) {
    var API = _NOTESURL;
    function fin(undone) { st.undone = undone; eb.disabled = false; eb.textContent = undone ? '↪ 重做' : '↩ 撤销'; HOST.notesReload(); }
    function patchAll(vals, undone) {
      Promise.all((st.items || []).map(function (it) {
        var v = it[vals] || {};
        return fetch(API, { method: 'PATCH', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ file: st.file, id: it.id, text: v.text, color: v.color }) }).catch(function () {});
      })).then(function () { fin(undone); });
    }
    if (!st.undone) {
      eb.textContent = '撤销中…';
      if (st.op === 'edit') { patchAll('old', true); return; }
      Promise.all((st.items || []).map(function (it) {
        return fetch(API + '?file=' + encodeURIComponent(st.file) + '&id=' + encodeURIComponent(it.id), { method: 'DELETE' }).catch(function () {});
      })).then(function () { fin(true); });
    } else {
      eb.textContent = '重做中…';
      if (st.op === 'edit') { patchAll('new', false); return; }
      Promise.all((st.items || []).map(function (it) {
        var n = it.note || {};
        return fetch(API, { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ file: st.file, anchor: n.anchor, text: n.text, color: n.color, w: n.w, h: n.h, collapsed: n.collapsed, strokes: n.strokes }) })
          .then(function (r) { return r.json(); })
          .then(function (dd) { if (dd && dd.ok) { it.id = dd.id; it.note = dd.note || n; } })
          .catch(function () {});
      })).then(function () { fin(false); });
    }
  }

  var _abort = null, _recovering = false, _lastProgressTs = 0, _ridCtr = 0;
  function _whenVisibleAsst() {   // 在后台 → 等回到前台再继续(重连前先回前台,后台重连也会被掐)
    return new Promise(function (res) {
      if (document.visibilityState !== 'hidden') return res();
      var h = function () { if (document.visibilityState !== 'hidden') { document.removeEventListener('visibilitychange', h); res(); } };
      document.addEventListener('visibilitychange', h);
    });
  }
  // 切后台→回前台:iOS 常把进行中的 SSE fetch 掐死/僵死 → 回来报 "Load failed" 或永远卡在「思考中」。
  // 回前台后给 3s 看有无新进度,没有就主动 abort 这条死流 → 走「从服务端历史恢复本轮回答」(服务端 finally 已落库)。
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState !== 'visible' || !streaming) return;
    setTimeout(function () {
      if (streaming && !_recovering && (Date.now() - _lastProgressTs > 3000)) {
        _recovering = true; try { _abort && _abort.abort(); } catch (_) {}
      }
    }, 3000);
  });
  // 流断/掐死后:服务端早把用户消息落了库、并在 finally 落了助手回答(可能完整也可能到断点)→ 拉回来补上
  async function _recoverFromHistory(tries) {
    tries = tries || 0;
    try {
      var r = await fetch(_HISTURL);
      var d = await r.json();
      if (d && d.ok && d.messages && d.messages.length) {
        var last = d.messages[d.messages.length - 1];
        if (last && last.role === 'assistant' && last.content) return last;
        if (tries < 2) { await new Promise(function (rs) { setTimeout(rs, 800); }); return _recoverFromHistory(tries + 1); }   // 服务端 finally 还没落库 → 等等再试
      }
    } catch (_) {}
    return null;
  }
  function _setSendMode(stop) {   // 流式中:发送键→停止键(红 ■);否则发送(➤)
    window.__asstStreaming = !!stop;   // 对外暴露流式态:EPUB 动作卡 attach(_epFlushActions)等本轮 assistant 消息落库再发
    if (stop) { sendBtn.classList.add('stop'); sendBtn.textContent = '■'; sendBtn.title = '停止'; }
    else { sendBtn.classList.remove('stop'); sendBtn.textContent = '➤'; sendBtn.title = '发送'; }
    sendBtn.disabled = false;
  }

  async function send(text, opts) {
    if (streaming) return;
    text = (text || '').trim();
    // 66:2.1(WebRTC)通话中打字直达实时模型(输入框紫光=在此状态);消费成功=不走文字助手管线
    if (text && window.__vcSendText) {
      try {
        if (window.__vcSendText(text)) {
          try { var _ta0 = pane.querySelector('#asst-ta'); if (_ta0) { _ta0.value = ''; _ta0.style.height = 'auto'; } } catch (e) {}
          return;
        }
      } catch (e) {}
    }
    var sentCtx = ctx();                                // 发送时定格上下文(图/选中/页),气泡卡片与后端保存的元数据一致
    try { var _pn0 = window.__vcPins ? window.__vcPins() : []; if (_pn0.length) sentCtx.pinned = _pn0.slice(0, 8); } catch (e) {}   // 97:长按带入的卡片(文字模式同样入上下文)
    // 「书页」点暗(rcNoBook)= 脱离这本书问通用问题 → 只剥**书本大上下文**(章/页定位、视口文字、选中所在句);
    //   **保留**用户显式选中/带入的 chip(选中文字/图/便签/焦点)——它们变成"独立片段/图"仍喂给 AI(用户诉求:
    //   关书页不该连我选中的都看不见)。后端 _ctx_block(no_book) 会把这些当独立内容拼进去。
    //   仅影响本次发送;历史里旧消息的 chip 是当时事实,不动。
    try {
      if (window.rcNoBook && window.rcNoBook()) {
        sentCtx.no_book = true;
        delete sentCtx.current_section_idx; delete sentCtx.section;   // 书本定位:去
        delete sentCtx.selection_sentence; delete sentCtx.selection_anchor;   // 选中的书本周边句:去(让选中变独立)
        delete sentCtx.visible_text;   // 整页/视口文字:去
        sentCtx.page = 0;   // 页定位去(figures 自带各自 page,see_figure 不受影响)
        // selection / figures / notes / focus_sel **保留**(这些是带 ✕ 的 chip,用户明确要 AI 看的)
      }
    } catch (e) {}
    // 隐式选中(无 chip 的持久兜底)也要"所见即所得":升格为可见焦点 chip(带 ✕)→ 之后每条都看得见、随时可取消
    // (用户反馈:选中悄悄跟着每条消息发,但上方没有那个带 x 的框,无法取消)
    try { if (!(window.__focusSel && window.__focusSel.text) && sentCtx.selection && sentCtx.selection.trim() && window.__setFocusSel) HOST.setFocusSel(sentCtx.selection, 'text'); } catch (_) {}
    if (!text) {
      // 空输入但有焦点上下文(带入的图 / 钉住的公式或段落 / 当前选中)→ 等于"就问这个",用默认问法直接发
      var _hasFig = (sentCtx.figures && sentCtx.figures.length);
      var _hasNote = (sentCtx.notes && sentCtx.notes.length);
      var _fs = sentCtx.focus_sel;
      var _hasSel = (sentCtx.selection && sentCtx.selection.trim());
      if (_hasFig) text = (sentCtx.figures.every(function (f) { return f && f.kind === 'note'; })) ? '讲讲这个便签' : '讲讲这张图';
      else if (_hasNote) text = '讲讲这个便签';
      else if (_fs && _fs.text) text = (_fs.kind === 'formula') ? '讲讲这个公式' : '讲讲这段';
      else if (_hasSel) text = '讲讲这段';
      else return;   // 真·空(无任何上下文)→ 不发
    }
    streaming = true; _setSendMode(true);
    var uMsg = addMsg('asst-u', esc(text));
    try { var _cc = _ctxCard(sentCtx, true, text); if (_cc) uMsg.appendChild(_cc); } catch (_) {}
    try { (HOST.clearFigFocus ? HOST.clearFigFocus() : (window.__clearFigFocus && window.__clearFigFocus())); } catch (_) {}   // 图已"用掉"并进了这条历史 → 清空带入列表,下一条不再重复携带(经 HOST:EPUB=__clearFigAttached)
    try { window.__clearNoteAttached && HOST.clearNoteAttached(); } catch (_) {}   // 便签 chip 同图附件条:发完即清(已定格进 sentCtx)
    var aMsg = addMsg('asst-a', '<span class="mfx-typing"><i></i><i></i><i></i></span>');
    var sawCliCard = false;   // #2:本轮委托给了 CLI(make_paper/do_task)→ CLI 卡就是回答,别再单独出编排答案气泡+建议按钮
    var sawTool = false;      // ★用户设计:本轮出现工具调用 → 「工具方块」模式(回答/流程/按钮全进 turn 卡)
    _vTid = 't' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);   // 每轮一个新方块(旧 bug:从不重置→多轮堆一卡);立即生成→随请求上送,后端落库带 turn_id,_syncParts 的 parts upsert 才能命中(审查 Q6)
    if (sentCtx.want_viewshot && window.RC && (RC.captureView || RC.captureEl)) {
      // adapter 声明 want_viewshot(服务端渲不了的内容:EPUB HTML 笔迹 / PDF 插入页覆盖层)→ 预拍一张
      //   随请求发,see_ink/see_page 拿 ctx.view_image 所见即所得。**优先 adapter.captureShot**
      //   (可截**具体那块**,如插入页元素,比整视口更聚焦);没有才退回整视口 captureView。
      try {
        var _ad = (RC.adapter && RC.adapter()) || null;
        var _vs = (_ad && _ad.captureShot) ? await _ad.captureShot() : (RC.captureView ? await RC.captureView() : null);
        if (_vs && _vs.b64) sentCtx.view_image = _vs;
      } catch (_) {}
    }
    delete sentCtx.want_viewshot;   // 前端标志,不入后端 ctx / 历史 meta
    var answer = '', acts = [], aborted = false, traceData = null, _recTs = 0;
    // 逐字浮现的"揭示游标":跟 SSE delta 到达节奏解耦,由 rAF 稳定速度推进 → 连续逐字(不段一段)
    var _revN = 0, _spans = [], _tot = 0, _raf = null, _lastTs = 0, _acc = 0, _noChar = false;
    function _revealTick(ts) {
      _raf = null;
      if (!streaming) return;
      if (!_lastTs) _lastTs = ts;
      var dt = Math.min(ts - _lastTs, 120); _lastTs = ts;   // clamp:切后台回来 dt 巨大,别一次灌完
      var backlog = _tot - _revN;
      if (backlog > 0) {
        var rate = 0.05 * (1 + backlog / 40);               // 字/ms:落后越多揭示越快,追上自然放慢
        _acc += dt * rate;
        var n = Math.min(backlog, Math.floor(_acc), 6);     // 每帧上限 6,防一次性灌入又变"段"
        if (n > 0) {
          _acc -= n;
          for (var k = 0; k < n; k++) { var s = _spans[_revN]; if (s) s.classList.add('mfx-reveal'); _revN++; }
          var c = aMsg.querySelector('.mfx-caret'), f = _spans[_revN - 1];
          if (c && f && f.parentNode) f.parentNode.insertBefore(c, f.nextSibling);
          scrollDown();
        }
      }
      if (streaming) _raf = requestAnimationFrame(_revealTick);
    }
    function _stopReveal() { if (_raf) { try { cancelAnimationFrame(_raf); } catch (_) {} _raf = null; } }
    var rid = 'c' + Date.now() + '_' + (_ridCtr++);   // 本轮任务 id:断线用它重连续读(服务端 detached 跑,不绑请求)
    var evSeen = 0, done = false;                      // 已消费的缓冲事件数(重连用 from=evSeen 续传)
    function _handleEv(ev, parsed) {
      if (ev === 'meta') return;                       // rid 确认,不计数
      evSeen++;
      if (ev === 'done') { done = true; return; }
      if (ev === 'tool') { aMsg.innerHTML = '<span class="asst-tool">🔧 ' + esc(parsed) + '…</span>'; scrollDown(); try { window.__vcCapStatus && window.__vcCapStatus('⚙︎ ' + parsed + '…'); } catch (_) {} }   // 朗读字幕兼状态显示(侧栏关着也能看到)
      else if (ev === 'tool-done') { try { aMsg.innerHTML = '<span class="asst-tool">思考中…</span>'; scrollDown(); } catch (_) {} try { window.__vcCapStatus && window.__vcCapStatus(null); } catch (_) {} }   // L3:工具完→中性「思考中」直到下个 answer/tool(镜像 EPUB)
      else if (ev === 'answer') {   // 流式轻量渲(不 MathJax)+ 剥 FOLLOWUP + 提亮&逐字浮现(揭示游标)+光标(mfx)
        answer = parsed;
        var _raw = _stripTornFU(_splitFollowups(answer).text);
        var _at = (window.RC && RC.assistant && RC.assistant.stripMoodTag) ? RC.assistant.stripMoodTag(_raw).text : _raw;
        try { window.__asstVoiceTap && window.__asstVoiceTap(_raw, false); } catch (_) {}   // tap 收**原文**(含语气标签,自己解析转折点逐句换情绪)
        if (sawTool && window.RC && RC.turnCard && RC.turnCard.has(_vTid)) {   // ★工具方块模式:回答直接流进卡 body(不再回气泡,收尾也在卡)
          try { RC.turnCard.draftText(_vTid, _at); } catch (_) {}
          scrollDown(); return;
        }
        renderMd(aMsg, _at, false); aMsg.classList.add('mfx-streaming');
        if (!_noChar && _at.length > 5000) { _noChar = true; _stopReveal(); }   // 超长答案:停揭示,改普通(保性能)
        if (_noChar) { _appendCaret(aMsg); }
        else {
          var w = _streamWrap(aMsg, _revN); _spans = w.spans; _tot = w.total;   // 重渲后重包:已揭示打 mfx-shown,新字等游标
          if (_revN > _tot) _revN = _tot;
          if (!_raf) { _lastTs = 0; _raf = requestAnimationFrame(_revealTick); }   // 启动/续跑揭示循环
        }
        scrollDown();
      }
      else if (ev === 'notice') { addMsg('asst-note', esc(parsed)); scrollDown(); }
      else if (ev === 'gemini-paid') {   // ② 免费 Gemini 受限→本次已用付费:提示条 + 一键「以后直接用付费」(渲染器在 rc-assistant,legacy 模式退纯文字)
        try {
          var _pn = (window.RC && RC.assistant && RC.assistant.paidNotice) ? RC.assistant.paidNotice(parsed) : null;
          if (_pn) { thread.appendChild(_pn); scrollDown(); }
          else if (!window.__paidNoted) { window.__paidNoted = true; addMsg('asst-note', esc((parsed && parsed.text) || '免费 Gemini 额度受限,本次已使用付费档。')); scrollDown(); }
        } catch (_) {}
      }
      else if (ev === 'actions') { try { runActions(parsed); } catch (_) {} }   // 实时:工具一执行完就应用(高亮/跳页立即生效),不等 AI 输出完
      else if (ev === 'tool2' && parsed && parsed.name) {
        if (parsed.task_id && (parsed.name === 'do_task' || parsed.name === 'make_paper' || parsed.name === 'read_check_report' || parsed.name === 'run_saved_task')) {   // #2 委托 CLI:CLI 卡接管 → 隐藏单独的编排答案气泡
          sawCliCard = true; try { aMsg.style.display = 'none'; } catch (_) {}
        } else if (!sawTool) {   // ★第一个工具调用出现 → 本轮进入「工具方块」模式:此后所有输出(含最终回答)都在卡内
          sawTool = true; try { aMsg.style.display = 'none'; } catch (_) {}
        }
        _toolChip(parsed);   // 工具指示器 v2:同一套圆/长条/方块(与语音通话共用)
      }
      else if (ev === 'trace') { traceData = parsed; }   // 调用链 → 喂「!」反馈弹窗
      else if (ev === 'task') { trackTask(parsed.task_id, parsed.label); }
      else if (ev === 'action' && parsed && parsed.id) { try { HOST.showAction && HOST.showAction(parsed); HOST.queueAction && HOST.queueAction(parsed); } catch (_) {} }   // EPUB 同步写工具:持久撤销/重做卡 + 排队落库(PDF 后端不发此事件)
      else if (ev === 'undo' && parsed && parsed.undo_id) {
        var _ujp = parsed.page ? ' <button class="asst-jump" data-page="' + esc(parsed.page) + '">↗ 跳转</button>' : '';
        addMsg('asst-a', '✓ ' + esc(parsed.label || '完成') + _ujp + ' <button class="asst-undo" data-uid="' + esc(parsed.undo_id) + '">↩ 撤销</button>');
      }
      else if (ev === 'error') { answer = '⚠️ ' + parsed; aMsg.innerHTML = esc(answer); }
    }
    // 开一条 SSE 读到自然结束/断开。首连带 message+context;重连只带 rid+from(服务端按 rid 续发缓冲事件)。
    async function _stream(body) {
      _abort = (typeof AbortController !== 'undefined') ? new AbortController() : null;
      var r = await fetch(_CHATURL, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body), signal: _abort ? _abort.signal : undefined,
      });
      if (r.status === 410) { done = 'gone'; return; }   // 任务已过期(>3min)→ 走历史恢复
      if (!r.ok || !r.body) throw new Error('http ' + r.status);
      var reader = r.body.getReader(), dec = new TextDecoder(), buf = '';
      while (true) {
        var rd = await reader.read(); if (rd.done) break;
        _lastProgressTs = Date.now();   // 有数据 = 流活着(回前台看门狗据此判断僵死)
        buf += dec.decode(rd.value, { stream: true });
        var idx;
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          var chunk = buf.slice(0, idx); buf = buf.slice(idx + 2);
          var ev = 'message', data = '';
          chunk.split('\n').forEach(function (ln) {
            if (ln.indexOf('event:') === 0) ev = ln.slice(6).trim();
            else if (ln.indexOf('data:') === 0) data += ln.slice(5).trim();
          });
          var parsed; try { parsed = JSON.parse(data); } catch (_) { parsed = data; }
          _handleEv(ev, parsed);
          if (done) return;
        }
      }
    }
    _recovering = false; _lastProgressTs = Date.now();
    var tries = 0;
    while (!done && !aborted) {
      try {
        try { if (sentCtx && window.rcNoBook && window.rcNoBook()) sentCtx.no_book = true; } catch (e) {}
        await _stream(tries === 0
          ? { message: text, context: sentCtx, rid: rid, turn_id: _vTid, media_prefer: (window.rcMediaPrefer ? window.rcMediaPrefer() : undefined), force_effort: (opts && opts.forceEffort) || undefined, force_model: (opts && opts.forceModel) || undefined,
              voice: (window.__asstVoiceOn && window.__asstVoiceOn()) ? 1 : undefined }   // 语音对话中:后端 prompt 切"适合朗读"风格
          : { rid: rid, from: evSeen });
      } catch (e) {
        if (e && e.name === 'AbortError') {
          if (_recovering) { _recovering = false; }   // 看门狗掐死僵死流 → 当断线,重连续传
          else { aborted = true; break; }             // 用户点停止 → 保留已生成部分
        }
        // 其它(Load failed / 网络断)→ 落到下面重连
      }
      if (done || done === 'gone' || aborted) break;
      if (++tries > 40) break;                          // 兜底:worker 6min 内必完成;真连不上才放弃
      try { aMsg.innerHTML = '<span class="asst-tool">连接断开,正在续传…</span>'; scrollDown(); } catch (_) {}
      await _whenVisibleAsst();                         // 等回到前台再重连
      await new Promise(function (rs) { setTimeout(rs, Math.min(400 * tries, 2000)); });
    }
    // 任务过期 / 兜底没续上 → 从服务端历史恢复(worker 跑完已落库,绝不丢)
    if ((done === 'gone' || (!done && !aborted)) && !answer) {
      try { aMsg.innerHTML = '<span class="asst-tool">正在恢复…</span>'; } catch (_) {}
      var rec = await _recoverFromHistory();
      if (rec && rec.content) { answer = rec.content; traceData = rec.trace || traceData; _recTs = rec.ts || 0; }
    }
    // 收尾:剥 FOLLOWUP → 完整渲染(MathJax 这一次)→ 追问 chip
    _stopReveal();                            // stream-fx:停揭示循环(下面 renderMd 重渲成干净 markdown,无 span/光标)
    aMsg.classList.remove('mfx-streaming');   // 停止提亮
    var pf = _splitFollowups(answer);
    try { if (!aborted) window.__asstVoiceTap && window.__asstVoiceTap(_stripTornFU(pf.text || ''), true); } catch (_) {}   // 语音对话:回答完,尾句也念(原文含标签,tap 自己解析;撕裂 FOLLOWUP 残段截掉)
    if (sawCliCard) {
      // #2 委托 CLI:CLI 卡即回答,撤掉这个多余的编排答案气泡(连带追问建议按钮),跟语音模式一致。
      try { aMsg.remove(); } catch (_) {}
    } else if (sawTool && window.RC && RC.turnCard && RC.turnCard.has(_vTid)) {
      // ★用户设计:**有工具调用的轮 = 回答写进工具方块**(turn 卡 body,与语音/CLI 同一形态),气泡撤掉。
      //   感叹号信息并入卡:编排模型/耗时/tok/时刻 → meta part(流程面板顶部,⚙ 直达编排设置)。
      try {
        try { RC.turnCard.idle(_vTid); } catch (_) {}   // 审查 Q1:工具 running 后走 error/停止,done 永不来 → 收尾必须掐掉卡头 spinner
        var _pftT = (RC.assistant && RC.assistant.stripMoodTag) ? RC.assistant.stripMoodTag(pf.text || '').text : pf.text;
        if (_pftT && !aborted) RC.turnCard.draftText(_vTid, _pftT);
        else RC.turnCard.status(_vTid, aborted ? '已停止' : '没拿到回答(可以重问一次)', true);   // 空回答兜底:别让卡空着像没结束(审查后补)
        RC.turnCard.freezeDraft(_vTid);
        if (traceData && traceData.length) {
          var _t0 = traceData[0] || {};
          RC.turnCard.addPart(_vTid, { kind: 'meta', model: _t0.model || '', sec: _t0.sec, tok: _t0.tok, tier: _t0.tier,
            total_sec: traceData.reduce(function (a, s) { return a + (s.sec || 0); }, 0),
            ts: _recTs || Math.floor(Date.now() / 1000), action: 'orchestrator' });
        }
        var _tc0 = RC.turnCard.open(_vTid);
        if (_tc0 && _tc0.bd && !aborted) {   // 底部操作行(▶/追问)挂在卡 body 内,一行
          if (pf.followups && pf.followups.length) { try { _renderFollowups(_tc0.bd, pf.followups); } catch (_) {} }
          if (pf.text) { try { _attachClipBtn(_tc0.bd, { content: answer, ts: _recTs || 0 }); } catch (_) {} }
        }
        aMsg.remove();
      } catch (_) {}
    } else {
      var _pft = (RC.assistant && RC.assistant.stripMoodTag) ? RC.assistant.stripMoodTag(pf.text || '').text : pf.text;
      if (_pft) renderMd(aMsg, _pft, true);
      else if (aMsg.innerHTML.indexOf('asst-tool') >= 0 || aMsg.innerHTML.indexOf('mfx-typing') >= 0) aMsg.innerHTML = esc(aborted ? '(已停止)' : '(没拿到回答)');
      if (!aborted) { try { _renderFollowups(aMsg, pf.followups); } catch (_) {} }
      if (!aborted && pf.text) { try { _attachFeedback(aMsg, text, traceData, _recTs || Math.floor(Date.now() / 1000)); } catch (_) {} }   // 「!」反馈按钮(带本轮调用链 + 耗时/时刻 + 可重答)
      if (!aborted && pf.text) { try { _attachClipBtn(aMsg, { content: answer, ts: _recTs || 0 }); } catch (_) {} }   // 66b:实时回答立即有 ▶(灰=TTS 念+保存;此前要刷新重渲历史才出现)
      if (!aborted) { try { _fadeInAfter(aMsg); } catch (_) {} }   // stream-fx:追问/反馈条错峰淡入
    }
    runActions(acts);
    streaming = false; _abort = null; _recovering = false; _setSendMode(false);
  }

  // 语音对话桥(rc-voicecall agent 模式):send/忙态都在本 IIFE 内,暴露出去 → ASR 终稿直接走完整聊天管线
  window.__asstSend = send;
  window.__asstBusy = function () { return !!streaming; };

  // ㉛ 通话对话进侧栏(用户设计):S2S/rtc 端到端通话的双方句子直接进 #asst-thread,与文字对话
  //   同流同清(🗑 清空=显示+服务端记录+语音记忆一起清,rc-voicecall 旁听 fresh 重连);
  //   通话浮层的迷你对话区(vc-sub)退役,只在无侧栏的页面兜底。
  var _vTid = '';        // 141:当前轮次容器 id(RC.turnCard;设计见 references/adr-turn-container.md)
  var _vTurnEl = null;   // 当前轮容器的 DOM(供 ▶/长按/落库等既有逻辑挂钩)
  // 141:一个**用户轮**里可以有多个 response(典型:「稍等」+function_call 是一个,工具结果的正答是另一个)。
  //   'a' 是**全量覆盖**语义 —— 若两个 response 共用一个写入点,后一个会把前一个覆盖掉;
  //   而若把前一个复制成"前置语"再让后一个写正文,那前一个还在流的 delta 又会把自己写进正文 = **显示两遍**
  //   (用户实测截图里「我去查一下东京明天的天气」出现两次,就是这么来的)。
  //   正解:**每个 response 一个槽位** —— 'slot' 断槽,'a' 只覆盖当前槽。
  var _vSlot = null;
  var _vUEl = null;   // 112:{iid, el} 最近用户句气泡——grok 可修订全文按 iid 覆盖
  window.__asstVoiceMsg = function (who, text) {
    try {
      // 141(轮次容器):'reset'=新用户轮→开新容器;'slot'=同轮内换 response→冻结当前草稿段落。
      //   DOM 结构不再由这里拼 —— 一律交给 RC.turnCard 的唯一渲染器(ADR 不变式①)。
      if (who === 'reset') {
        _vTurnEl = null; _vSlot = null;
        _vTid = 't' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
        return true;
      }
      if (who === 'slot') { try { RC.turnCard && RC.turnCard.freezeDraft(_vTid); } catch (e) {} return true; }
      if (who === 'u') {
        var _o2 = arguments[2] || {};
        if (_o2.utterId && _vUEl && _vUEl.iid === _o2.utterId && _vUEl.el && _vUEl.el.parentNode === thread) {
          _vUEl.el.textContent = text;   // 112(用户规范):同 item 的修订=覆盖同一气泡,不追加
          scrollDown(); return true;
        }
        var d = document.createElement('div'); d.className = 'asst-msg asst-u'; d.textContent = text;
        if (_o2.utterId) _vUEl = { iid: _o2.utterId, el: d };
        // GPT 用户转写(whisper)迟到,AI 回复常已在流:按真实时序插进行中气泡**前面**,不断 AI 轮
        if (_vTurnEl && _vTurnEl.parentNode === thread) thread.insertBefore(d, _vTurnEl);
        else { thread.appendChild(d); _vTurnEl = null; }
        scrollDown(); return true;
      }
      // 141:AI 文字一律写进**轮次容器**的草稿段落(md 渲染在唯一渲染器里做)。
      if (!_vTid) _vTid = 't' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
      var _tc = null;
      try { RC.turnCard.draftText(_vTid, text); _tc = RC.turnCard.open(_vTid); } catch (e) {}
      if (!_tc) return true;
      _vTurnEl = _tc.el;
      var _tgt = (_tc.draft && _tc.draft._el) || _tc.bd;
      if (arguments[2] && arguments[2].md) {   // 67:文字轮/路由长文的**终态**用 Markdown 渲染(流式期间纯文本省性能)
        try { renderMd(_tgt, text, true); } catch (e) { _tgt.textContent = text; }
        // 139(用户):AI 的文字输出**不需要「!」这样的按键** —— 工具调用的详情归工具卡的【数据流】按钮 + 长按详情窗
        try {   // 83:长文气泡 TTS 念钮(☆撤——用户裁定收藏走拖拽/浮层)
          if (arguments[2].speak && !_vTurnEl.querySelector('.asst-clip')) {   // 跨层查(clip 可能在 .asst-btm 内,审查 Q7)
            var _sb = document.createElement('button'); _sb.type = 'button'; _sb.className = 'asst-clip dim'; _sb.textContent = '▶';
            _sb.title = 'TTS 念这条';
            var _txt0 = text, _stop0 = null;
            _sb.addEventListener('click', function (ev) {
              ev.stopPropagation();
              try { window.__vcTtsWarm && window.__vcTtsWarm(); } catch (e2) {}
              if (_stop0) { _stop0(); _stop0 = null; _sb.textContent = '▶'; _sb.classList.remove('playing'); return; }
              _stop0 = window.__vcSpeakText ? window.__vcSpeakText(_txt0) : null;
              _sb.textContent = '◼'; _sb.classList.add('playing');
            });
            _vTurnEl.appendChild(_sb);
          }
        } catch (e) {}
        try { if (arguments[2].pin && window.__vcPinBind) window.__vcPinBind(_vTurnEl, arguments[2].pin.label, arguments[2].pin.textFn); } catch (e) {}   // 79:长按=全文带入
        try { _bubDecor(_vTurnEl, arguments[2].pin && arguments[2].pin.label, arguments[2].pin && arguments[2].pin.textFn); } catch (e) {}   // 86/87:标题把手条
      } else _tgt.textContent = text;
      scrollDown(); return true;
    } catch (e) { return false; }
  };
  // 141:容器内容一变就同步落库(防抖)。见 rc-turncard 的注释:展示型工具之后不再有 response,
  //   只靠 response.done 落库会把 tool/card 永远漏在服务器之外。
  var _psT = null;
  function _syncParts(tid) {
    if (!tid) return;
    clearTimeout(_psT);
    _psT = setTimeout(function () {
      try {
        var ps = RC.turnCard.partsOf(tid);
        if (!ps || !ps.length) return;
        var txt = ps.filter(function (p) { return p.kind === 'text'; })
                    .map(function (p) { return p.text; }).join('\n\n');
        var ctx = {};
        try { ctx = (RC.adapter && RC.adapter().getContext && RC.adapter().getContext()) || {}; } catch (e) {}
        // upsert_only:记录还不存在就**什么都不做**(见后端注释:抢在 response.done 前面建记录
        //   会把用户的提问挤掉)。没落上就 2.5s 后再试一次 —— 那时 response.done 的记录必已就位。
        fetch('/api/assistant/log', { method: 'POST', headers: { 'Content-Type': 'application/json' }, keepalive: true,
          body: JSON.stringify({ assistant: txt, parts: ps, turn_id: tid, via: 'voice', upsert_only: 1,
            file: ctx.file_rel || ctx.file || '', page: ctx.page || 0 }) })
          .then(function (r) { return r.json(); })
          .then(function (d) { if (d && d.upserted === false && !_psRetry[tid]) { _psRetry[tid] = 1; setTimeout(function () { _syncParts(tid); }, 2500); } })
          .catch(function () {});
      } catch (e) {}
    }, 900);
  }
  var _psRetry = {};
  try { if (window.RC && RC.turnCard) RC.turnCard.onChange = _syncParts; } catch (e) {}

  // 141:当前轮容器 id 的**唯一出口**(rc-voicecall 的工具/结果卡靠它把 part 注进同一个容器)
  window.__asstVoiceTid = function () {
    if (!_vTid) _vTid = 't' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
    return _vTid;
  };
  // 141:__asstVoiceCard(把气泡"升格"成卡)已**删除** —— 结构改由 RC.turnCard 容器承担。
  //   ADR 铁律:旧的实时拼接路径不许留着当 fallback,留着就等于允许实时/回放分叉。
  // 141:__asstVoiceMoveLead(把前置语搬进结果卡)已**删除** —— 结果卡现在是容器里的一个 card part,
  //   前置语是它前面的 text part,天然同框,不需要"搬"。
  try { window.RC = window.RC || {}; RC.assistant = RC.assistant || {}; RC.assistant.renderMd = renderMd; RC.assistant.trackCliTask = _trackCliTask; } catch (e) {}   // 67:文字卡片等外部组件复用 md 渲染;trackCliTask 供语音路(rc-voicecall)复用(CLI 委托任务的流程/结果/建纸)
  // 统一「主动问一句」出口:开抽屉 + 走 send(通话中→实时模型 __vcSendText,否则→文字管线)。
  //   供外部(如检查结果面板「让 AI 讲讲」)把内容回报给前端编排 AI,文字/语音自动路由。
  try { RC.assistant.ask = function (t) { try { if (openGrammarPanel) openGrammarPanel(); } catch (e) {} return send(t); }; } catch (e) {}
  window.__asstInfoBtn = function (el, info) {   // 77b:右下角小「!」详情钮(语音气泡/搜索卡通用)
    try {
      if (!el || el.querySelector(':scope > .vc-inf-b')) return;
      el.style.position = 'relative';
      var b = document.createElement('button'); b.type = 'button'; b.className = 'vc-inf-b'; b.textContent = '!';
      b.title = '本轮详情 / 模型设置';
      b.addEventListener('click', function (ev) {
        ev.stopPropagation();
        var old = el.querySelector(':scope > .vc-inf-pop');
        if (old) { old.remove(); return; }
        var pop = document.createElement('div'); pop.className = 'vc-inf-pop';
        var rows = [];
        if (info && info.mode) rows.push('<div class="vc-inf-r"><span>输出形态</span>' + esc(info.mode) + '</div>');
        if (info && info.tools && info.tools.length) rows.push('<div class="vc-inf-r"><span>本段工具</span>' + esc(info.tools.join(' · ')) + '</div>');
        if (info && info.kind) rows.push('<div class="vc-inf-r"><span>类型</span>' + esc(info.kind) + '</div>');
        if (info && info.srcs) rows.push('<div class="vc-inf-r"><span>搜索链路</span>' + esc(info.srcs) + '</div>');
        pop.innerHTML = rows.join('') +
          '<div class="vc-inf-r"><span>中间步骤</span>见对话流中各工具卡(点开看参数与结果)</div>' +
          '<button type="button" class="vc-inf-set">⚙ 调整各环节 AI 模型</button>';
        pop.querySelector('.vc-inf-set').addEventListener('click', function () {
          pop.remove();
          try {   // 91:有环节清单→只开该环节的直改面板;没有→退回总设置
            if (info && info.actions && window.__asstActionSettings) window.__asstActionSettings(info.actions, { note: info.note, voiceTab: info.voiceTab });
            else if (window.openModelSettings) window.openModelSettings(info && info.focus);
          } catch (e2) {}
        });
        el.appendChild(pop);
      });
      el.appendChild(b);
    } catch (e) {}
  };
  window.__asstVoiceLog = function (q, a, file, page, extra) {   // 通话轮次落库(与文字对话同一历史,清空一起清);extra.clip=66 该轮语音录音 id
    if (!q && !a) return;
    try {   // 66b:本轮气泡实时挂 ▶(有录音=放当时原声;无=灰钮 TTS)——延迟避开 md 终态重渲清 DOM;先删 1960 的旧 TTS 钮防双钮
      var _bel = _vTurnEl;
      if (_bel && a) setTimeout(function () {
        try {
          if (!_bel.parentNode) return;
          _bel.querySelectorAll('.asst-clip').forEach(function (x) { x.remove(); });   // 跨层删(同上)
          _attachClipBtn(_bel, { content: a, clip: (extra && extra.clip) || '' });
        } catch (e) {}
      }, 60);
    } catch (e) {}
    try {
      if (HOST.voiceLog) { HOST.voiceLog(q, a, page); return; }   // ㉟ adapter 自定义落库(EPUB=本书 epub-convo;clip 暂不支持该路径)
      var _b = { user: q || '', assistant: a || '', file: file || '', page: page || 0, via: 'voice' };
      if (extra && extra.clip) _b.clip = extra.clip;
      // 141(轮次容器):把本轮的**全量 part 结构**一起落库 —— 刷新/跨设备后回放走**同一个渲染器**复原。
      //   ⚠ 带上 turn_id:一个用户轮里天然有多个 response(「稍等」+工具 / 正答),而落库挂在 response.done 上
      //   → 不带 turn_id 就会**一轮落两条**(回放渲两遍 + 早期快照里还没有结果卡 = 卡片丢失,用户实测)。
      //   服务端按 turn_id **覆盖**那条助手消息,始终只留一条、且是最新的完整 parts。
      try {
        RC.turnCard.freezeDraft(_vTid);
        var _ps = RC.turnCard.partsOf(_vTid);
        if (_ps && _ps.length) { _b.parts = _ps; _b.turn_id = _vTid; }
      } catch (e) {}
      fetch('/api/assistant/log', { method: 'POST', headers: { 'Content-Type': 'application/json' }, keepalive: true,
        body: JSON.stringify(_b) }).catch(function () {});
    } catch (e) {}
  };
  window.__asstHistUrl = function () { return _HISTURL; };   // ㉟ 语音重连历史回放读侧栏同一端点(EPUB=本书 epub-convo)

  // ── MCP 遥控统一执行器(共享层,阅读器统一架构):外部 agent(Claude/ChatGPT app 经 MCP)的
  //    client_action 由两阅读器的 reader-events SSE 监听各自挂载、都调这里执行。
  //    顺序:① adapter.execAction(fn,args) 精确翻译钩子(per-reader 语义,可选实现,返回 true=已接住)
  //         ② window[fn] 原生全局(PDF 的 jumpWithBack 等,保留原语义:跳转带返回按钮)
  //         ③ 跳转语义映射 HOST 契约 goTo(EPUB 无 window.jumpWithBack → 章跳,两 adapter 都实现了 goTo)──
  try {
    window.RC = window.RC || {};
    RC.execRemote = function (action) {
      if (!action || !action.fn) return false;
      var fn = action.fn, args = action.args || [];
      try {
        var ad = (RC.adapter && RC.adapter()) || null;
        if (ad && typeof ad.execAction === 'function' && ad.execAction(fn, args)) return true;
      } catch (e) {}
      try { if (typeof window[fn] === 'function') { window[fn].apply(null, args); return true; } } catch (e) {}
      try {
        if (fn === 'jumpWithBack') {
          var host = (RC.adapter && RC.adapter()._host && RC.adapter()._host.asst) || null;
          if (host && host.goTo) { host.goTo(parseInt(args[0], 10) || 0); return true; }
        }
      } catch (e) {}
      return false;
    };
  } catch (e) {}

  // ── 工具指示器 v2(rc-toolchip):文字对话的每次工具调用也长一个 chip 进对话流 ──
  //    与语音通话完全同一套组件/同一套选中广播(同 cid 处处高亮);后台任务继续轮询步骤。
  var _tchips = {};
  // 141(用户实测:侧栏 chat 多工具 = 6 张平铺卡):改成注入**轮次容器**(与语音路径一致)——
  //   多工具聚合成**一张卡**,点【流程】看每步。不再给每个工具单独造 chip 挂 thread。
  function _toolChip(d) {
    if (!(window.RC && RC.toolChip)) return;
    if (!_vTid) { _vTid = 't' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6); }
    var tid = _vTid;
    if (d.status === 'running') {
      try { RC.turnCard && RC.turnCard.busy(tid, d.label || d.name || '工具'); } catch (_) {}
      return;
    }
    // done/error:注入一个 tool part(参数/子步骤/结果全在里面)
    try {
      RC.turnCard.idle(tid);
      // ★ do_task(CLI 编排):后台 agent 的**内部工具调用**要展进这张卡的流程 —— 否则没 parts,
      //   保存工具(E)也无从下手(用户点破:CLI 不显示工具,保存就做不了)。轮询 task-status 拉 steps。
      // CLI 委托任务(do_task/make_paper)→ 卡内进度+结果(_trackCliTask);其它后台写任务(制卡/笔记)走 task 事件的卡外浮动+撤销。
      if (d.task_id && (d.name === 'do_task' || d.name === 'make_paper' || d.name === 'read_check_report' || d.name === 'run_saved_task')) { _trackCliTask(tid, d.task_id, d.label || d.name || '造纸'); return; }
      RC.turnCard.addPart(tid, { kind: 'tool', tool: d.name || '', label: d.label || d.name || '工具',
        args: d.args || {}, steps: d.sub_steps || [], result: String(d.brief || '').slice(0, 6000),   // 放宽:感叹号 detail 全量语义并入流程
        vision: d.vision || [],   // #8:实际发给 AI 的图 → 流程「AI 请求」节点展示(点击放大)
        took_s: d.sec, model: d.model, error: d.status === 'error' ? (d.brief || '失败') : '' });
    } catch (_) {}
  }
  // 轮询 CLI 后台任务:进度进 busy 行,**内部每个工具**作为 sub_steps 展进 tool part 的流程。
  function _trackCliTask(tid, taskId, label) {
    // 实现已搬入 rc-turncard(RC.turnCard.trackCli)——工具库页等非阅读器页面复用同一份(用户:一份代码)。
    try { return RC.turnCard.trackCli(tid, taskId, label); } catch (_) {}
  }

  // 后台写任务(制卡/笔记/生词):轮询完成 → 在对话里给结果 + 「↩ 撤销」按钮 + PWA 通知
  function trackTask(id, label) {
    if (!id) return;
    var line = addMsg('asst-a', '<span class="asst-tool">⏳ ' + esc(label || '处理') + '中…</span>');
    var n = 0;
    (function poll() {
      if (n++ > 120) { line.innerHTML = '<span class="asst-tool">⌛ ' + esc(label) + ':等太久了</span>'; return; }
      fetch('/api/voice/task-status?id=' + encodeURIComponent(id)).then(function (r) { return r.json(); }).then(function (d) {
        if (!d || !d.ok) { return; }
        if (d.status === 'running') { if (d.step) line.innerHTML = '<span class="asst-tool">⏳ ' + esc(d.step) + '…</span>'; setTimeout(poll, 2000); return; }
        if (d.status === 'done') {
          try { if (d.client_actions && d.client_actions.length) runActions(d.client_actions); } catch (_) {}   // 任务附带的客户端副作用(如生词下划线刷新)
          var uid = d.result && d.result.undo_id;
          if (uid && HOST.taskAction) {   // EPUB:后台任务完成 → 持久「详情/撤销/重做」卡(落库,刷新仍在)
            line.innerHTML = '✓ ' + esc(d.speak || '完成');
            try { HOST.taskAction(uid); } catch (_) {}
          } else {
            line.innerHTML = '✓ ' + esc(d.speak || '完成') + (uid ? ' <button class="asst-undo" data-uid="' + esc(uid) + '">↩ 撤销</button>' : '');
          }
          notify('阅读助手 ✓', d.speak || '任务完成');
        } else { line.innerHTML = '✗ ' + esc(d.error || '没办成'); }
        scrollDown();
      }).catch(function () { setTimeout(poll, 3000); });
    })();
  }
  thread.addEventListener('click', function (e) {
    var jb = e.target && e.target.closest && e.target.closest('.asst-jump');
    if (jb) { var jp = parseInt(jb.getAttribute('data-page'), 10); if (jp && typeof window.jumpWithBack === 'function') HOST.goTo(jp); return; }
    var redo = e.target && e.target.closest && e.target.closest('.asst-hl-redo');   // M9:删完的「↪ 重做」→ 用存的锚重建高亮(拿新 id)
    if (redo) {
      var rf = redo.getAttribute('data-file'), rd = {};
      try { rd = JSON.parse(redo.getAttribute('data-hl') || '{}'); } catch (_) {}
      if (!rd.rects || !rd.rects.length) { if (typeof _toast === 'function') _toast('这条没有几何信息,无法重建'); return; }
      redo.disabled = true; redo.textContent = '重建中…';
      fetch(_HLURL, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ file: rf, page: rd.page, rects: rd.rects, color: rd.color, text: rd.text }) })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d && d.ok) {
            try { window._reloadHighlights && HOST.reloadHighlights(); } catch (_) {}
            var db = document.createElement('button'); db.className = 'asst-hl-del';
            db.setAttribute('data-id', d.id || ''); db.setAttribute('data-file', rf); db.setAttribute('data-hl', redo.getAttribute('data-hl') || ''); db.textContent = '🗑 删除';
            var row2 = redo.closest('.asst-hl-row'); redo.replaceWith(db);
            if (row2) { row2.style.opacity = ''; var tx2 = row2.querySelector('.asst-hl-tx'); if (tx2) tx2.style.textDecoration = ''; }
          } else { redo.disabled = false; redo.textContent = '↪ 重做'; if (typeof _toast === 'function') _toast('重建失败'); }
        })
        .catch(function () { redo.disabled = false; redo.textContent = '↪ 重做'; });
      return;
    }
    var del = e.target && e.target.closest && e.target.closest('.asst-hl-del');   // 「列出可删高亮」里的删除按钮
    if (del) {
      var hid = del.getAttribute('data-id'), hfile = del.getAttribute('data-file');
      del.disabled = true; del.textContent = '删除中…';
      fetch(_HLURL, { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ file: hfile, id: hid }) })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (d && d.ok) {
            try {   // 立刻把这条高亮的 DOM 元素(.hl-saved[data-id])抹掉 → 不等重拉就即时反映在页面
              var esc2 = (window.CSS && CSS.escape) ? CSS.escape(hid) : hid;
              document.querySelectorAll('.hl-saved[data-id="' + esc2 + '"]').forEach(function (el) { el.remove(); });
            } catch (_) {}
            try { window._reloadHighlights && HOST.reloadHighlights(); } catch (_) {}   // 再重拉,同步 _hlByPage,翻页回来不复现
            var row = del.closest('.asst-hl-row');
            if (row) { row.style.opacity = '.45'; var tx = row.querySelector('.asst-hl-tx'); if (tx) tx.style.textDecoration = 'line-through'; }
            var _rb = document.createElement('button'); _rb.className = 'asst-hl-redo'; _rb.textContent = '↪ 重做';   // M9:删完转重做(用存的锚重建)
            _rb.setAttribute('data-file', hfile); _rb.setAttribute('data-hl', del.getAttribute('data-hl') || '');
            del.replaceWith(_rb);
          } else { del.disabled = false; del.textContent = '🗑 删除'; if (typeof _toast === 'function') _toast('删除失败:' + ((d && d.error) || '')); }
        })
        .catch(function () { del.disabled = false; del.textContent = '🗑 删除'; });
      return;
    }
    // 自动卡片的「撤销 ⇄ 重做」切换:撤销=删全部 id,重做=用存的字段重建(拿新 id),按钮文字来回切
    var eb = e.target && e.target.closest && e.target.closest('.asst-edit-undo');
    if (eb) {
      var eid2 = eb.getAttribute('data-eid'); var st = _assistEdits[eid2]; if (!st) return;
      eb.disabled = true;
      if (st.ntype === 'note') { _noteEditToggle(st, eb); return; }   // 便签卡:走便签版撤销⇄重做
      if (!st.undone) {
        eb.textContent = '撤销中…';
        Promise.all((st.ids || []).map(function (id) {
          return fetch(_HLURL, { method: 'DELETE', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ file: st.file, id: id }) }).then(function (r) { return r.json(); }).catch(function () { return { ok: false }; });
        })).then(function () {
          try { window._reloadHighlights && HOST.reloadHighlights(); } catch (_) {}
          st.undone = true; eb.disabled = false; eb.textContent = '↪ 重做';
        });
      } else {
        eb.textContent = '重做中…';
        Promise.all((st.items || []).map(function (it) {
          return fetch(_HLURL, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ file: st.file, page: it.pdf_page, rects: it.rects, color: it.color, text: it.text }) }).then(function (r) { return r.json(); }).then(function (d) { return (d && d.ok) ? d.id : null; }).catch(function () { return null; });
        })).then(function (nids) {
          st.ids = nids.filter(Boolean);
          try { window._reloadHighlights && HOST.reloadHighlights(); } catch (_) {}
          st.undone = false; eb.disabled = false; eb.textContent = '↩ 撤销';
        });
      }
      return;
    }
    var btn = e.target && e.target.closest && e.target.closest('.asst-undo'); if (!btn) return;
    var uid = btn.getAttribute('data-uid'); btn.disabled = true; btn.textContent = '撤销中…';
    fetch('/api/assistant/undo', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ id: uid }) })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (d && d.ok && d.kind === 'highlight') { try { window._reloadHighlights && HOST.reloadHighlights(); } catch (_) {} }   // 撤销高亮要重渲页面才视觉清掉
        btn.outerHTML = d && d.ok ? '<span class="asst-tool">↩ 已撤销</span>' : ('<span class="asst-tool">撤销失败:' + esc((d && d.error) || '') + '</span>');
      })
      .catch(function () { btn.disabled = false; btn.textContent = '↩ 撤销'; });
  });
  function notify(title, body) {
    try {
      if (!('Notification' in window) || Notification.permission !== 'granted') return;
      var opt = { body: body, tag: 'asst-task', icon: '/static/icons/icon-192.png' };
      if (navigator.serviceWorker && navigator.serviceWorker.ready) navigator.serviceWorker.ready.then(function (reg) { reg.showNotification(title, opt); }).catch(function () { try { new Notification(title, opt); } catch (_) {} });
      else try { new Notification(title, opt); } catch (_) {}
    } catch (_) {}
  }

  // 快捷按钮
  pane.querySelector('#asst-quick').addEventListener('click', function (e) {
    var btn = e.target && e.target.closest ? e.target.closest('button') : e.target;
    var ds = btn && btn.getAttribute('data-send');
    if (ds) { if (!streaming) send(ds); return; }   // 学习类快捷按钮:直接发预设问题
    var q = btn && btn.getAttribute('data-q'); if (!q) return;
    try {
      if (q === 'prev') HOST.changePage(-1);
      else if (q === 'next') HOST.changePage(1);
      else if (q === 'fit') HOST.fitWidth();
      else if (q === 'zin') HOST.zoomBy(0.15);
      else if (q === 'zout') HOST.zoomBy(-0.15);
      else if (q === 'ptrans') HOST.toggleTranslate();
      else if (q === 'clear') { if (streaming) { try { _abort && _abort.abort(); } catch (_) {} streaming = false; _setSendMode(false); } thread.innerHTML = ''; try { RC.toolChip && RC.toolChip.clearAll(); } catch (_) {} fetch(_CLEARURL, { method: 'POST' }).catch(function () {}); greet(); }   // L5:流式中清空先中止,防在已移除气泡上继续写 + streaming 卡死
      else if (q === 'models') { openModelSettings(); }
    } catch (_) {}
  });

  // 输入
  function autorow() { ta.style.height = 'auto'; ta.style.height = Math.min(120, ta.scrollHeight) + 'px'; }
  ta.addEventListener('input', autorow);
  ta.addEventListener('keydown', function (e) { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); if (streaming) return; micStop(); var v = ta.value; ta.value = ''; autorow(); send(v); } });
  sendBtn.addEventListener('click', function () {
    if (streaming) { try { _abort && _abort.abort(); } catch (_) {} return; }   // 流式中点 ■ → 中止本轮
    micStop(); var v = ta.value; ta.value = ''; autorow(); send(v);
  });

  // ── 苹果风格语音按钮:持续聆听,只手动停(再点麦克风 / 点发送即停)。设备原生 STT(iOS=Siri 级)。
  //    iOS 的 SpeechRecognition 静默时会自己结束,所以只要用户没手动停,onend 就重启 = 真·持续聆听。
  //    识别结果只填进输入框(用户审一眼再发),续写已有内容;无 SR 的浏览器→聚焦输入框,用系统键盘自带听写麦克风。
  //    micStop/micStart 为函数声明(在本 IIFE 内提升),上面的发送处理器即可调用 micStop 收口,避免迟到结果回填残留。
  var micBtn = pane.querySelector('#asst-mic');
  var _SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  var micRec = null, micOn = false, micCommitted = '', micSessFinal = '', micSessTok = null, micLastWrite = '';
  var micStartTs = 0, micLastStart = 0, micFails = 0, micSessProductive = false;   // 总时长软上限 + 空转(弱网/无语音)退避
  var micAutoT = null;   // 听写自动发送(用户设计:跟豆包 ASR 一样,说完一句静默即发,连续对话)
  var micHold = false;   // 听写-朗读互斥(㉔):发送后暂停听写,AI 播完再恢复——否则听写占麦让 iOS 会话粘录音类别,
                         // 朗读被强制走扬声器(用户报告的"戴耳机不走耳机"残余场景);顺带根治 AI 声音被听写录回去
  function _micResumeWhenQuiet() {
    if (!micOn) { micHold = false; return; }
    var busy = false;
    try { busy = !!(window.__vcTtsBusy && window.__vcTtsBusy()); } catch (_) {}
    if (busy || streaming) { setTimeout(_micResumeWhenQuiet, 600); return; }   // 回答还在生成/朗读还在响 → 再等
    micHold = false; micBtn.classList.add('on'); micSpin();                    // 安静了:恢复连续听
  }
  function _micAutoSend() {
    if (!micOn) return;
    if (streaming) { micAutoT = setTimeout(_micAutoSend, 1200); return; }   // 上一条还在答:等答完再发,别丢话
    var t = (ta.value || '').trim();
    if (!t) return;
    micSessTok = null;                  // 作废当前段:迟到 onresult 不回填
    micCommitted = ''; micSessFinal = ''; micLastWrite = '';
    ta.value = ''; autorow();
    micHold = true;                     // 暂停听写(onend 不重启),按钮回暗=「AI 在说」;播完 _micResumeWhenQuiet 自动亮回
    try { window.__vcDictAudioOff && window.__vcDictAudioOff(); } catch (_) {}   // 关掉听写期间建的朗读 ac(路由粘扬声器),tap 重建拿干净耳机会话
    try { micRec && micRec.stop(); } catch (_) {}
    Promise.resolve(send(t)).then(_micResumeWhenQuiet, _micResumeWhenQuiet);
  }
  function micStop() {                 // 手动停:micOn=false + 作废会话 → 迟到 onresult 不回填、onend 不重启
    if (!micOn) return;
    micOn = false; micSessTok = null; micFails = 0; micHold = false;
    if (micAutoT) { clearTimeout(micAutoT); micAutoT = null; }
    try { micRec && micRec.stop(); } catch (_) {}
    micBtn.classList.remove('on');
    try { window.__vcCapDictEnd && window.__vcCapDictEnd(); } catch (_) {}   // 字幕:听写态结束
  }
  function micSpin() {                  // 起一段识别(每段:会话令牌 tok + 实例身份 thisRec 双重身份)
    if (!micOn) return;
    var tok = (micSessTok = {});
    var thisRec;
    try {
      thisRec = micRec = new _SR();
      micRec.lang = 'zh-CN'; micRec.interimResults = true; micRec.continuous = true; micRec.maxAlternatives = 1;
      micSessFinal = ''; micSessProductive = false; micLastStart = Date.now();
      micRec.onresult = function (e) {
        if (!micOn || micSessTok !== tok) return;   // 发送/停止/编辑作废的会话:不回填(防残留/旧词复活/孤立实例串扰)
        micSessProductive = true;
        var f = '', it = '';
        for (var i = 0; i < e.results.length; i++) {
          if (e.results[i].isFinal) f += e.results[i][0].transcript; else it += e.results[i][0].transcript;
        }
        micSessFinal = f;
        ta.value = micCommitted + f + it; micLastWrite = ta.value; autorow();
        try { if (ta.value.trim()) window.__vcCapUser && window.__vcCapUser(ta.value.trim()); } catch (_) {}   // 侧栏关着听写:转写实时上字幕(开着时字幕自身 gate 掉)
        if (micAutoT) { clearTimeout(micAutoT); micAutoT = null; }
        if ((micCommitted + f).trim() && !it) micAutoT = setTimeout(_micAutoSend, 900);   // 有定稿且没有进行中片段 → 静默 0.9s 自动发送(与豆包 ASR 同节奏)
      };
      micRec.onerror = function (ev) {  // 权限/无麦:立即放弃;network/no-speech 等交给下面的空转计数收口,不单次就放弃
        if (ev && (ev.error === 'not-allowed' || ev.error === 'service-not-allowed' || ev.error === 'audio-capture')) micOn = false;
      };
      micRec.onend = function () {
        if (micRec !== thisRec) return;             // 已被更晚的 spin 取代的孤立实例:不提交不重启(根治 orphan + 竞态)
        if (micSessTok === tok && micSessFinal) { micCommitted = (micCommitted + micSessFinal).replace(/\s+$/, '') + ' '; }
        micSessFinal = '';
        micBtn.classList.remove('on');
        if (!micOn) { autorow(); return; }
        if (micHold) return;   // 发送后暂停中(AI 在答/在念):不重启,_micResumeWhenQuiet 到点恢复(按钮暗=暂停,亮回=继续听)
        if (Date.now() - micStartTs > 120000) { micStop(); return; }   // 总时长软上限 2min:忘关也不会一直占麦
        // 这段没出任何结果且很快就结束 = 疑似弱网/引擎空转 → 累计 5 次即停;出过结果或在正常等静默则清零
        if (!micSessProductive && (Date.now() - micLastStart) < 1200) { if (++micFails >= 5) { micStop(); return; } }
        else micFails = 0;
        micBtn.classList.add('on');
        setTimeout(function () { if (micOn && micRec === thisRec) micSpin(); }, micFails ? 700 : 0);   // 异步重启(打断紧致 churn)+ 退避
      };
      micRec.start();
    } catch (_) { micOn = false; micSessTok = null; micBtn.classList.remove('on'); ta.focus(); }
  }
  function micStart() {
    if (!_SR) { ta.focus(); return; }   // 无原生 STT:聚焦输入框,用系统键盘的听写麦克风
    micOn = true; micFails = 0; micStartTs = Date.now(); micBtn.classList.add('on');
    micCommitted = ta.value ? (ta.value.replace(/\s+$/, '') + ' ') : '';   // 续写已有内容
    micSessFinal = ''; micLastWrite = ta.value;
    micSpin();
  }
  // 听写中用户手动改输入框(典型:逐字删除):以改后文本为新基线 + 作废当前会话重起一段(新实例 results 为空,
  // 旧词不会被下次 onresult 带回来)。我们自己填的值不算手动编辑(programmatic 赋值不触发 input,micLastWrite 再兜底)。
  ta.addEventListener('input', function () {
    if (!micOn || ta.value === micLastWrite) return;
    micSessTok = null;                  // 作废:在途/后续旧 onresult 不再回填
    if (micAutoT) { clearTimeout(micAutoT); micAutoT = null; }   // 用户接管编辑:先停自动发送,等下一个定稿再计时
    micCommitted = ta.value; micLastWrite = ta.value; micSessFinal = '';
    try { micRec && micRec.stop(); } catch (_) {}   // onend(micOn 仍真,且是当前实例)→ micSpin 重起 fresh-results 新会话
  });
  document.addEventListener('visibilitychange', function () { if (document.hidden) micStop(); });   // 切走/锁屏即停,免后台占麦空转
  if (!_SR) micBtn.title = '点这里→用键盘的听写麦克风';
  micBtn.addEventListener('click', function () { micOn ? micStop() : micStart(); });

  function prewarm(off) { try { fetch('/api/assistant/prewarm', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(off ? { off: 1 } : {}), keepalive: true }); } catch (_) {} }
  window.__asstPrewarm = function () { try { prewarm(false); } catch (_) {} };   // 切到助手 tab 时也预热(减第二条起的冷启动)
  function greet() {
    var _n = (HOST.locNoun && HOST.locNoun()) || '页';   // 位置量词按 reader(PDF=页 / EPUB=章)
    addMsg('asst-a', '我是这本书的阅读助手。试试:<br>· 这' + _n + '讲什么 / 总结这' + _n + '<br>· 翻译这段(先选中)<br>· 找讲XX的' + _n + '跳过去<br>· 把这段做成卡片 / 整理成笔记<br><span style="color:#7a8497">(写入/制卡都可「↩ 撤销」;对话云端保存、跨设备;🗑 清空)</span>');
  }
  // ── 87:气泡装饰(标题整行把手,voiceMsg 实时与 loadHistory 回放共用——"消息记录中的卡片永远是卡片")──
  function _bubDecor(el, label, textFn) {
    // 98(用户反馈"2.1 的回复被当成了卡片"):普通回复**不再**披卡片外观——整行标题条退役,
    // 改右上角弱化小把手(拖收藏功能保留);长按带入在气泡本体照旧。只有真正的结构卡才有标题条。
    if (!el || !label || !textFn || el.querySelector(':scope > .vc-bub-grip')) return;
    el.style.position = 'relative';
    var g0 = document.createElement('span'); g0.className = 'vc-bub-grip'; g0.textContent = '⠿';
    g0.title = '按住拖到屏幕底边=收入收藏夹;长按气泡=带入对话';
    el.insertBefore(g0, el.firstChild);
    try {
      if (window.__vcDragToDock) window.__vcDragToDock(el, function () { var t0 = textFn(); return { label: label, raw: t0, isHtml: false, text: String(t0).slice(0, 4000) }; });
      if (window.__vcPinBind) window.__vcPinBind(el, label, textFn);
    } catch (e) {}
  }
  // ── 66 历史语音回放(用户设计):每条 AI 消息一个播放钮——通话时录下的原声(clip)直接播;
  //    没有录音(文字轮/旧记录)=灰钮,点击用 TTS 现场念+同时录下上传,并回写历史(clip-attach)变紫 ──
  var _clipAudio = null, _clipBtnCur = null;
  function _clipStop() {
    try { if (_clipAudio) { _clipAudio.pause(); _clipAudio = null; } } catch (e) {}
    try { window.__vcMicHold && window.__vcMicHold(false); } catch (e) {}   // 82:播放结束/停止恢复麦
    if (_clipBtnCur) { _clipBtnCur.classList.remove('playing'); _clipBtnCur.textContent = '▶'; _clipBtnCur = null; }
  }
  function _attachClipBtn(el, m) {
    if (!m || !(m.content || '').trim()) return;
    var b = document.createElement('button'); b.type = 'button';
    b.className = 'asst-clip' + (m.clip ? '' : ' dim');
    b.title = m.clip ? '播放当时的语音' : '没有录音:点击用 TTS 念这条并保存';
    b.textContent = '▶';   // 80:纯字符替代 SVG(仿「!」钮实现——SVG 在部分环境渲成白块的保底修复)
    function _ttsGen() {
      if (!window.__vcTtsCapture) { if (typeof _toast === 'function') _toast('朗读引擎不可用'); return; }
      b.classList.add('busy');
      window.__vcTtsCapture((m.content || '').slice(0, 3000)).then(function (blob) {
        b.classList.remove('busy');
        if (!blob || blob.size < 4000) return;   // 念了但没录到(引擎没出声)就不存
        var id = 'c' + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
        fetch('/api/assistant/voice-clip?id=' + id, { method: 'POST', headers: { 'Content-Type': blob.type || 'audio/mp4' }, body: blob })
          .then(function (r) { return r.json(); })
          .then(function (d) {
            if (!(d && d.ok)) return;
            m.clip = id;
            b.classList.remove('dim'); b.title = '播放当时的语音';
            fetch('/api/assistant/clip-attach', { method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ ts: m.ts || 0, head: (m.content || '').slice(0, 60), clip: id }) }).catch(function () {});
          }).catch(function () {});
      });
    }
    b.addEventListener('click', function () {
      try { window.__vcTtsWarm && window.__vcTtsWarm(); } catch (e) {}   // 82:手势同步栈内预热(灰钮不出声根因:异步链里激活 AudioContext 被 iOS 拒)
      if (b.classList.contains('busy')) {   // 97(用户设计):生成中再点=停(TTS 停→录音自然收尾→blob 过小被丢弃,不入库)
        try { window.__vcTtsStop && window.__vcTtsStop(); } catch (e) {}
        b.classList.remove('busy');
        return;
      }
      if (b.classList.contains('playing')) { _clipStop(); return; }
      _clipStop();
      if (m.clip) {
        var a = new Audio('/api/assistant/voice-clip/' + encodeURIComponent(m.clip));
        _clipAudio = a; _clipBtnCur = b; b.classList.add('playing'); b.textContent = '◼';
        try { window.__vcMicHold && window.__vcMicHold(true); } catch (e) {}   // 82:<audio> 不在 AEC 参考里——播放期禁麦防通话 AI 听到
        a.onended = _clipStop;
        a.onerror = function () { _clipStop(); _ttsGen(); };   // 录音缺失(当时没传成)→ 降级 TTS 生成
        a.play().catch(function () { _clipStop(); _ttsGen(); });
      } else _ttsGen();
    });
    try { el.querySelectorAll('.asst-clip').forEach(function (x) { x.remove(); }); } catch (_) {}   // 审查 Q7:跨层去重(clip 现在在 .asst-btm 内,旧 :scope> 守卫看不见 → 双 ▶)
    _btmBar(el).insertBefore(b, _btmBar(el).firstChild);   // ▶ 进底部操作行最左(与 chips/! 同一行,不再各占一行)
  }
  function loadHistory() {   // 开面板载入服务端保存的历史(跨设备续上)
    fetch(_HISTURL).then(function (r) { return r.json(); }).then(function (d) {
      if (d && d.ok && d.messages && d.messages.length) {
        var _lastQ = '';   // 历史回答的「!」反馈要带上「重答」用的原问题 → 记住上一条用户消息
        d.messages.forEach(function (m) {
          if (m.role === 'user') {
            _lastQ = m.content || '';
            var uel = addMsg('asst-u', esc(m.content));
            try { var c = _ctxCard({ figures: m.figures, selection: m.selection, page: m.page, file_rel: m.file_rel, section: m.section, selection_anchor: m.sel_anchor }, false, m.content); if (c) uel.appendChild(c); } catch (_) {}   // section/sel_anchor=EPUB 历史字段(PDF 无此字段不受影响)
          }
          else if (Array.isArray(m.parts) && m.parts.length && window.RC && RC.turnCard) {
            // ★ 141(轮次容器)回放:**走与实时完全同一个 renderPart()**(ADR 不变式①)——
            //   实时和回放不可能分叉,今天那类"刷新后卡片退化成纯文本"的 bug 从架构上消失。
            var _rtid = 'h' + (m.ts || Math.random().toString(36).slice(2, 8));
            RC.turnCard.renderTurn(_rtid, m.parts);
          }
          else if (m.card && window.__vcInfoCardEl) {   // 87:旧数据(没有 parts)→ 回落到结构化卡回放,保持向后兼容
            var ce = window.__vcInfoCardEl(m.card);
            if (ce) { thread.appendChild(ce); }
          }
          else {
            var el = addMsg('asst-a', ''); var _pf = _splitFollowups(m.content || '');
            renderMd(el, (RC.assistant && RC.assistant.stripMoodTag) ? RC.assistant.stripMoodTag(_pf.text).text : _pf.text);
            try { _attachClipBtn(el, m); } catch (_) {}   // 66:语音回放按钮(有录音=紫;无=灰,点了 TTS 念+保存)
            try { if (m.via === 'voice' && (m.content || '').length > 120) _bubDecor(el, 'AI 回答', (function (t0) { return function () { return t0; }; })(m.content || '')); } catch (_) {}   // 87:历史语音长文=同样可拖可长按
            try { _renderFollowups(el, _pf.followups); } catch (_) {}
            try { _attachFeedback(el, _lastQ, m.trace || null, m.ts || null); } catch (_) {}   // 历史也带 trace(步骤/模型/耗时)+ 时刻;质量回报用 _lastQ 重答
            if (Array.isArray(m.videos) && m.videos.length && window.renderVideos) { try { window.renderVideos(m.videos); } catch (_) {} }   // 视频卡刷新回放(镜像 EPUB 阶段C)
            if (Array.isArray(m.undo_cards)) m.undo_cards.forEach(function (u) {   // H2:高亮撤销卡刷新回放(undo_id 服务端持久,撤销/跳转 handler 已复用)
              if (!u || !u.undo_id) return;
              var _ujp = u.page ? ' <button class="asst-jump" data-page="' + esc(u.page) + '">↗ 跳转</button>' : '';
              addMsg('asst-a', '✓ ' + esc(u.label || '完成') + _ujp + ' <button class="asst-undo" data-uid="' + esc(u.undo_id) + '">↩ 撤销</button>');
            });
            if (Array.isArray(m.actions) && HOST.showAction) m.actions.forEach(function (a) {   // EPUB 动作卡回放(持久撤销⇄重做,存 meta.actions;PDF 无此字段)
              try { HOST.showAction(a); } catch (_) {}
            });
          }
        });
        // 进面板自动滚到最新(最下方):渲完滚一次,再隔 250ms 补一次(图/MathJax 异步撑高后位置会漂)
        requestAnimationFrame(scrollDown);
        setTimeout(scrollDown, 250);
      } else greet();
    }).catch(greet);
  }
  loadHistory();
  };
})();
