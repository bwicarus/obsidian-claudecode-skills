/* rc-core.js — 统一控制层(Reader Control / window.RC)的地基。
 * 目标:PDF 阅读器(reader.src/*.js)和 EPUB 阅读器(epub-html.js)**共用一份控制层代码**,
 * 各自只提供一个「适配器」(选区/坐标/锚点/跳转/上下文/端点/能力开关)——底座耦合的唯一落点。
 * 本文件零底座耦合:只做命名空间引导 + 适配器注册 + 通用工具。是 rc-md / rc-* 的底座。
 * 迁移策略:先抽共享模块 + 只接 EPUB 验证;再逐个把 PDF 部件 behind window.RC_USE.<mod> flag 切过来(旧实现保留)。
 */
(function () {
  if (window.RC && window.RC.use) return;   // 按能力守卫:别的模块先建了空 RC 壳(如曾被 rc-outbox 抢先)也不跳过初始化;已有成员由下方赋值保留语义=覆盖为正版
  // 共享契约 v1:只描述、审计和提供已确认等价的常量，不给 adapter 暗补方法、不抛错。
  // 这是“不丢功能”的安全边界:差异实现继续原样运行，audit 只把差异暴露给测试/调试。
  var _CONTRACT_VERSION = 'reader-host/1';
  var _COMMON_ENDPOINTS = {
    dict: '/pdf/api/dict', dictJp: '/pdf/api/dict-jp', dictJpAi: '/pdf/api/dict-jp-ai',
    translate: '/pdf/api/translate', explain: '/pdf/api/explain',
    vocabMap: '/pdf/api/vocab-mastery-map', vocabAnki: '/pdf/api/vocab-anki',
    toNote: '/pdf/api/to-note', snippetsTo: '/pdf/api/snippets-to-async', jobStatus: '/pdf/api/job-status'
  };
  var _CONFIG_PROFILES = {
    web: { isPDF: false, reflow: true, hasFigures: false, hasFormula: true,
      dictMode: 'sse', popupMode: 'fixed', clickWordDetect: true, anchorKind: 'offset' }
  };
  var _BASE_METHODS = ['getContext', 'currentLocation'];
  var _SELECTION_METHODS = ['captureSelection', 'clearSelection'];
  var _ASST_HOST_METHODS = ['md', 'toast', 'fileRel', 'mountPanel', 'mountTabs', 'openDrawer', 'switchTab'];
  function _copy(base, overrides) {
    var out = {}, k;
    for (k in base) if (Object.prototype.hasOwnProperty.call(base, k)) out[k] = base[k];
    overrides = overrides || {};
    for (k in overrides) if (Object.prototype.hasOwnProperty.call(overrides, k)) out[k] = overrides[k];
    return out;
  }
  function _missing(obj, names) {
    var out = [];
    for (var i = 0; i < names.length; i++) if (!obj || typeof obj[names[i]] !== 'function') out.push(names[i]);
    return out;
  }
  // 统一选区 DTO，只补齐跨宿主同义字段；anchor/rect/page/file 等 opaque 字段逐项保留，绝不转换坐标。
  function _selectionSnapshot(input) {
    if (!input) return null;
    var out = _copy(input, {});
    out.text = String(input.text == null ? '' : input.text);
    if (!out.text.trim()) return null;
    var context = input.context != null ? String(input.context)
      : (input.ctx != null ? String(input.ctx) : (input.sentence != null ? String(input.sentence) : ''));
    out.context = context;
    if (out.ctx == null) out.ctx = context;
    if (out.sentence == null) out.sentence = context;
    if (!Object.prototype.hasOwnProperty.call(out, 'anchor')) out.anchor = null;
    if (!Object.prototype.hasOwnProperty.call(out, 'rect')) out.rect = null;
    return out;
  }
  function _auditAdapter(adapter) {
    adapter = adapter || {};
    var host = adapter._host && adapter._host.asst;
    var actionNames = [], actions = adapter._actions || {};
    for (var name in actions) if (Object.prototype.hasOwnProperty.call(actions, name)) actionNames.push(name);
    actionNames.sort();
    return {
      contract: _CONTRACT_VERSION,
      kind: adapter.kind || 'unknown',
      baseMissing: _missing(adapter, _BASE_METHODS),
      selectionMissing: _missing(adapter, _SELECTION_METHODS),
      assistantHostMissing: _missing(host, _ASST_HOST_METHODS),
      actionNames: actionNames,
      config: _copy(adapter.config || {}, {})
    };
  }
  // 【iOS 根治】button 的原生外观(push-button)会画一层浅色圆角块盖住自定义 background → 用户看到的「白色方块」。
  // 桌面 Chromium 不画 ⇒ headless 测不出。这里在共享层地基上兜底一次(最低优先级,零回归),覆盖没有引 pdf/epub-styles 的页面。
  try {
    if (!document.getElementById('rc-btn-reset')) {
      var _bs = document.createElement('style'); _bs.id = 'rc-btn-reset';
      _bs.textContent = 'button{-webkit-appearance:none;appearance:none}' +
        /* 共享阅读 UI 内统一隐藏原生滚动槽：滚轮/触摸/键盘仍可滚，避免窄框和 textarea 里出现灰色“框中框”。 */
        '#ep-side *,#result-mask *,#draft-mask *,#word-pop *,#rc-hl-pop *,.rc-set-mask *,.ams-mask *,#rc-vc *,#vc-dock-panel *,.rc-note *{scrollbar-width:none}' +
        '#ep-side *::-webkit-scrollbar,#result-mask *::-webkit-scrollbar,#draft-mask *::-webkit-scrollbar,#word-pop *::-webkit-scrollbar,#rc-hl-pop *::-webkit-scrollbar,.rc-set-mask *::-webkit-scrollbar,.ams-mask *::-webkit-scrollbar,#rc-vc *::-webkit-scrollbar,#vc-dock-panel *::-webkit-scrollbar,.rc-note *::-webkit-scrollbar{width:0!important;height:0!important;display:none!important}';
      (document.head || document.documentElement).insertBefore(_bs, (document.head || document.documentElement).firstChild);
    }
  } catch (e) {}
  // ══ 双向上下文同步(2026-07-26):三宿主唯一上报器 ═══════════════════════════════
  // PDF/EPUB/HTML(以及扩展 web 宿主)各自只负责说一句「我现在在哪本、第几页」,
  // 合并/节流/单次在途/心跳/开关 gate 全都只有这一份实现——宿主侧不许再各写一套。
  // 普通网页/扩展仍由本地明示偏好控制。原生 App 是例外:它的快照服务由
  // ReaderPC 拥有,App 只接受 Windows CONTEXT-MODE 的易失运行时授权,不保存也不反向改写。
  var _CTX_LS = 'eph-ctx-sync';
  // 时序合同(用户拍板 2026-07-27):**默认即时推**,不搞一刀切长防抖。
  // 唯一需要合并的是「连续翻页/快速滚动」这种临时位置——持续导航期间不推中间页,
  // 停手约 1s 只推最终页。选区建立/清空、换书这类即时操作不许被导航防抖拖住。
  var _CTX_NAV_MS = 1000;         // 导航(同一本书内翻页)专用 trailing 合并窗
  // 停留判定(共享便签 rev18 / 任务书 A5):**连续翻页不逐页注入**,用户停在一页
  // 约 2-3 秒才补一条 reason='dwell' → 服务端据此发整页正文。换页即重置计时器,
  // 所以快速翻十页只会在最后停下的那一页发一次。
  var _CTX_DWELL_MS = 2500;
  var _dwellTimer = null, _dwellKey = '';
  var _CTX_NOW_MS = 0;            // 其余一切:下一个 tick 就发(仍受单次在途保护,不会打爆)
  var _CTX_HEARTBEAT_MS = 60000;  // 可见时心跳:久读同一页也不会被服务端判成「不新鲜」
  var _ctxS = {
    base: '', pend: null, canonical: null,
    timer: null, inflight: false, dirty: false, hb: null, bound: false
  };
  var _ctxServerMode = null;
  function _ctxServerOwned() { return window.__BW_NATIVE_COMPUTER_VOICE__ === true; }
  function _ctxOn() {
    if (_ctxServerOwned()) return _ctxServerMode === 'snapshot-mcp';
    try { return localStorage.getItem(_CTX_LS) === '1'; } catch (e) { return false; }
  }
  function _ctxApplyServerMode(mode) {
    if (!_ctxServerOwned()) return false;
    var wasOn = _ctxOn();
    _ctxServerMode = mode === 'snapshot-mcp' ? mode : null;
    var isOn = _ctxOn();
    if (isOn) {
      _ctxBind(true);
      if (_ctxS.pend) {
        _ctxSchedule(0);
        _ctxArmDwell(_ctxS.pend);
      }
    } else if (wasOn || _ctxS.bound || _ctxS.timer) {
      _ctxClear();
      if (_dwellTimer) { clearTimeout(_dwellTimer); _dwellTimer = null; }
      _ctxS.dirty = false;
      _ctxS.canonical = null;
      _ctxBind(false);
      if (_og.drawTimer) { clearTimeout(_og.drawTimer); _og.drawTimer = null; }
      _og.drawPend = null;
      _og.focus = null;
    }
    return isOn;
  }
  function _ctxU(p) { return (_ctxS.base || '') + p; }
  function _ctxClear() { if (_ctxS.timer) { clearTimeout(_ctxS.timer); _ctxS.timer = null; } }
  function _ctxSchedule(ms) { _ctxClear(); _ctxS.timer = setTimeout(_ctxSend, ms); }
  function _ctxScalarEq(a, b) {
    if (a === undefined || a === null) return b === undefined || b === null;
    if (b === undefined || b === null) return false;
    return String(a) === String(b);
  }
  function _ctxValidScalar(value, allowNull) {
    if (value === undefined || value === null) return !!allowNull;
    if (typeof value === 'number') return Number.isSafeInteger(value) && value >= 0;
    return typeof value === 'string' && value.length > 0 && value.length <= 256 &&
      !/[\u0000-\u001f\u007f-\u009f]/.test(value);
  }
  function _ctxValidFile(value) {
    return typeof value === 'string' && value.length > 0 && value.length <= 4096 &&
      !/[\u0000-\u001f\u007f-\u009f]/.test(value);
  }
  function _ctxIdentityEq(left, right) {
    if (!left || !right || left.kind !== right.kind) return false;
    var leftFile = left.kind === 'web' ? left.url : left.file;
    var rightFile = right.kind === 'web' ? right.url : right.file;
    return leftFile === rightFile && _ctxScalarEq(left.pos, right.pos);
  }
  function _ctxCanonicalFor(source, body) {
    var value = body && body.ok === true && body.canonical;
    if (!source || !value || typeof value !== 'object' || value.kind !== source.kind ||
        !_ctxValidFile(value.file) || !_ctxValidScalar(value.page, true)) return null;
    var sourceFile = source.kind === 'web' ? source.url : source.file;
    if (!_ctxValidFile(sourceFile)) return null;
    var isView = source.kind !== 'web' && sourceFile.indexOf('vbook:') === 0;
    if (isView) {
      if (!_ctxValidScalar(source.pos, false) ||
          value.viewFile !== sourceFile ||
          !_ctxScalarEq(value.viewPage, source.pos) ||
          !_ctxValidScalar(value.viewPage, false) ||
          value.file.indexOf('vbook:') === 0 ||
          !_ctxValidScalar(value.page, false)) return null;
    } else if (
      value.file !== sourceFile ||
      !_ctxScalarEq(value.page, source.pos) ||
      (value.viewFile !== undefined && value.viewFile !== null) ||
      (value.viewPage !== undefined && value.viewPage !== null)
    ) {
      return null;
    }
    return {
      kind: value.kind,
      file: value.file,
      page: value.page === undefined ? null : value.page,
      viewFile: isView ? value.viewFile : null,
      viewPage: isView ? value.viewPage : null
    };
  }
  function _ctxCanonicalMatches(source, canonical) {
    if (!source || !canonical || source.kind !== canonical.kind) return false;
    var sourceFile = source.kind === 'web' ? source.url : source.file;
    if (source.kind !== 'web' && typeof sourceFile === 'string' &&
        sourceFile.indexOf('vbook:') === 0) {
      return canonical.viewFile === sourceFile &&
        _ctxScalarEq(canonical.viewPage, source.pos);
    }
    return canonical.viewFile === null && canonical.file === sourceFile &&
      _ctxScalarEq(canonical.page, source.pos);
  }
  function _ctxAcceptCanonical(sent, body) {
    var canonical = _ctxCanonicalFor(sent, body);
    if (canonical && _ctxIdentityEq(sent, _ctxS.pend)) {
      _ctxS.canonical = canonical;
    }
  }
  function _ctxReadAck(response, sent) {
    if (!response || response.ok !== true || typeof response.json !== 'function') return null;
    return response.json().then(function (body) {
      _ctxAcceptCanonical(sent, body);
      return body;
    }).catch(function () { return null; });
  }
  function _ctxSend() {
    _ctxS.timer = null;
    if (!_ctxOn() || !_ctxS.pend) return;
    if (_ctxS.inflight) { _ctxS.dirty = true; return; }   // 单次在途:回来之后再补发最新的那份
    _ctxS.inflight = true;
    var sent = Object.assign({}, _ctxS.pend);
    // @interaction context.active.report
    fetch(_ctxU('/pdf/api/active-reading'), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(sent), keepalive: true, credentials: 'include'
    }).then(function (response) {
      return _ctxReadAck(response, sent);
    }).catch(function () {}).then(function () {
      _ctxS.inflight = false;
      if (_ctxS.dirty) { _ctxS.dirty = false; _ctxSchedule(0); }
    });
  }
  function _ctxBeacon() {   // 切后台/关页:卸载中 fetch 会被浏览器砍,用 beacon 送最后一次
    _ctxClear();
    if (!_ctxOn() || !_ctxS.pend) return;
    try {
      var raw = JSON.stringify(_ctxS.pend);
      // The native sendBeacon shim owns this same-origin local state write.
      // Give it the JSON synchronously: Blob.text() may never resume after iOS
      // suspends a pagehide callback, leaving the snapshot on the previous page.
      var b = window.__BW_NATIVE_LOCAL_READER__ === true
        ? raw : new Blob([raw], { type: 'application/json' });
      // @interaction context.active.report
      if (navigator.sendBeacon && navigator.sendBeacon(_ctxU('/pdf/api/active-reading'), b)) return;
    } catch (e) {}
    _ctxSend();
  }
  function _ctxVis() { if (document.visibilityState === 'hidden') _ctxBeacon(); else _ctxSchedule(0); }
  function _ctxBind(on) {
    if (on && !_ctxS.bound) {
      document.addEventListener('visibilitychange', _ctxVis);
      window.addEventListener('pagehide', _ctxBeacon);
      _ctxS.hb = setInterval(function () {
        if (!_ctxOn() || !_ctxS.pend || document.visibilityState === 'hidden') return;
        _ctxSchedule(0);   // 心跳只刷新 ts,内容原样(不额外采集,不影响滚动)
      }, _CTX_HEARTBEAT_MS);
      _ctxS.bound = true;
    } else if (!on && _ctxS.bound) {
      document.removeEventListener('visibilitychange', _ctxVis);
      window.removeEventListener('pagehide', _ctxBeacon);
      if (_ctxS.hb) { clearInterval(_ctxS.hb); _ctxS.hb = null; }
      _ctxS.bound = false;
    }
  }
  // viewport 是"用户此刻看到第几段"的补充信息(EPUB 用),滚动期间一直在变。
  // 它**不参与**下面两个判等:否则每次滚动都被当成状态变化而即时推,把导航合并整个
  // 打穿——跟选区漏斗那个坑同类。它的更新走 report() 里的专门分支。
  // 注:即使不显式跳过,String({para:1}) 和 String({para:2}) 都是 "[object Object]"
  // 也会碰巧判等;但那是巧合,不能作为依据。
  function _ctxViewportEq(a, b) {
    try { return JSON.stringify(a || null) === JSON.stringify(b || null); } catch (e) { return a === b; }
  }

  function _ctxSameState(prev, next) {
    var keys = {}, k;
    for (k in prev) keys[k] = 1;
    for (k in next) keys[k] = 1;
    for (k in keys) {
      if (k === 'ts' || k === 'viewport') continue;
      if (String(prev[k] === undefined ? '' : prev[k]) !== String(next[k] === undefined ? '' : next[k])) return false;
    }
    return true;
  }

  function _ctxOnlyPosChanged(prev, next) {
    // 选区字段必须逐字比较:选区一变(建立/改动/清空)就不算导航,要即时推。
    var keys = {}, k;
    for (k in prev) keys[k] = 1;
    for (k in next) keys[k] = 1;
    for (k in keys) {
      if (k === 'pos' || k === 'ts' || k === 'viewport') continue;
      if (String(prev[k] === undefined ? '' : prev[k]) !== String(next[k] === undefined ? '' : next[k])) return false;
    }
    return true;
  }

  // ══ 出向上下文:焦点 + 绘图版本(2026-07-28,任务书 A5 前端接线)═════════════
  // 唯一实现放共享层,PDF/EPUB/HTML 三宿主都调同一份;没有该能力的宿主(如 HTML 无墨迹)
  // 不调 drawingTouched 即自然降级,不需要各自写分支。
  // 与 ctxSync 共用同一运行时 gate:普通网页由用户偏好开启,原生 App 只由 ReaderPC 授权。
  var _og = { focus: null, drawTimer: null, drawPend: null, inflight: false };
  var _OG_DRAW_MS = 1000;   // 绘图停手约 1s 才提交 —— 逐笔发送会把服务端和网络都打满
  var _OG_DRAW_EVENT = 'bw-reader-drawing-state';

  function _ogSig(kind, ref) { return kind + '|' + JSON.stringify(ref); }
  function _ogEmitDrawing(state, value) {
    try {
      if (!document || !document.dispatchEvent || !window.CustomEvent) return;
      document.dispatchEvent(new CustomEvent(_OG_DRAW_EVENT, {
        detail: {
          state: state,
          file: value && value.file,
          page: value && value.page,
          drawingRevision: (
            value && typeof value.drawingRevision === 'string'
              ? value.drawingRevision : null
          )
        }
      }));
    } catch (e) {}
  }

  // ── 出向事件的身份归一(与 ctxSync 同源)────────────────────────────────
  // 问题:focus/drawing 用的是宿主直接给的 ref —— 合并书里那是 vbook 全局身份;
  // 而 page.context 经 active-reading 已归一到真实卷 rel + 卷内页。两种身份交替到达
  // Windows,同一逻辑页被当成两页 → 正文瞬时清空、选区被忽略。
  //
  // 归一只用**同一份已验证映射** `_ctxS.canonical`(服务端 ack 回来、且当时上报身份与之
  // 逐字段匹配才被接受;换书/翻页/关开关都会把它置空)。**绝不凭 vbook 前缀猜真实卷**。
  //
  // fail closed 的含义:映射缺失、过期或与本事件身份不匹配时**返回 null → 不发这条事件**,
  // 而不是退回发 vbook 身份 —— 后者正是会清空新页正文的那种事件。宁可少一条焦点,
  // 也不能让旧身份把新页正文/选区打掉。
  // 非 vbook 的书本身就是 canonical,原样返回,不受影响。
  function _ogCanonicalRef(ref) {
    if (!ref || typeof ref !== 'object') return null;
    var file = ref.file;
    // 不带 file 的焦点(卡片 {cid}、纯文本片段等)与书页身份无关,归一规则不适用,
    // **原样放行** —— 否则会静默吞掉这类焦点。
    if (typeof file !== 'string' || !file) return ref;
    if (file.indexOf('vbook:') !== 0) return ref;      // 普通书:本就是真实身份
    var c = _ctxS.canonical;
    if (!c || c.viewFile !== file || !_ctxScalarEq(c.viewPage, ref.page)) return null;
    if (!_ctxValidFile(c.file)) return null;
    var out = {}, k;
    for (k in ref) if (Object.prototype.hasOwnProperty.call(ref, k)) out[k] = ref[k];
    out.file = c.file;
    out.page = c.page;
    // 保留视图身份供消费方回指(例如要跳回合并书视图),但**不参与身份判定**
    out.viewFile = file;
    out.viewPage = ref.page === undefined ? null : ref.page;
    return out;
  }

  function _ogPost(path, body) {
    if (!_ctxOn()) return Promise.resolve(null);
    // @interaction context.focus.report
    return fetch(_ctxU(path), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body), credentials: 'include'
    }).then(function (r) { return r.json(); }).catch(function () { return null; });
  }

  function _ogDrawFlush() {
    _og.drawTimer = null;
    if (!_ctxOn() || !_og.drawPend || _og.inflight) return;
    var q = _og.drawPend; _og.drawPend = null; _og.inflight = true;
    var confirmStable = false;
    // 取当前页绘图引用:服务端按"内容摘要 + 静默 1s"判定是否已稳定,未稳定不给引用
    // @interaction context.drawing.revision
    fetch(_ctxU('/pdf/api/outgoing/drawing?file=' + encodeURIComponent(q.file) +
                (q.page != null ? '&page=' + encodeURIComponent(q.page) : '')),
          { credentials: 'include' })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        _og.lastDrawing = d ? Object.assign({ file: q.file }, d) : null;
        if (d && d.empty === true) {
          _ogEmitDrawing('empty', _og.lastDrawing);
        } else if (d && d.stable === true && d.drawingRevision) {
          // 第一次 pending + 一次有界稳定确认，总计约停笔 2s。稳定边沿只
          // 发状态事件；PWA→Windows 的图像捕获/传输由电脑语音模块拥有。
          _ogEmitDrawing('stable', _og.lastDrawing);
        }
        // 这一请求可能是服务端第一次见到新墨迹,只会启动稳定计时并返回 pending。
        // 停笔后仅再确认一次即可取得 stable revision；不能无限轮询。
        confirmStable = !!(d && d.ok !== false && d.empty === false &&
                           d.stable === false && !q.confirmed);
      })
      .catch(function () {})
      .then(function () {
        _og.inflight = false;
        if (_og.drawPend) {
          _ogSchedDraw(0);                   // 期间又画了 → 回来再取一次最新
        } else if (confirmStable) {
          _og.drawPend = { file: q.file, page: q.page, confirmed: true };
          _ogSchedDraw(_OG_DRAW_MS);         // pending 后只做一次有界稳定确认
        }
      });
  }
  function _ogSchedDraw(ms) {
    if (_og.drawTimer) clearTimeout(_og.drawTimer);
    _og.drawTimer = setTimeout(_ogDrawFlush, ms);
  }

  function _ctxArmDwell(state) {
    // 只对"有页码的文档"计时;选区类上报不重置停留(选区本身会立即触发整页补齐)
    if (!state || state.pos === undefined) return;
    var key = String(state.file || state.url || '') + '#' + state.pos;
    if (_dwellTimer) { clearTimeout(_dwellTimer); _dwellTimer = null; }
    if (key === _dwellKey) return;        // 还在同一页:已发过或已在计时,不重复武装
    _dwellTimer = setTimeout(function () {
      _dwellTimer = null;
      if (!_ctxOn()) return;
      var cur = _ctxS.pend || {};
      var now = String(cur.file || cur.url || '') + '#' + cur.pos;
      if (now !== key) return;            // 期间又翻页了 → 这页不算停留,不注入
      _dwellKey = key;
      // 一次性事件,不并进 _ctxS.pend:reason 一旦并进合并状态就会粘住,
      // 之后每次翻页都带 reason='dwell',服务端的"翻页不注入"闸门会被整个打穿。
      // @interaction context.active.report
      var sent = Object.assign({}, cur, { reason: 'dwell' });
      fetch(_ctxU('/pdf/api/active-reading'), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(sent), credentials: 'include'
      }).then(function (response) {
        return _ctxReadAck(response, sent);
      }).catch(function () {});
    }, _CTX_DWELL_MS);
  }

  var _outgoing = {
    drawDebounceMs: _OG_DRAW_MS,
    kinds: ['text', 'image', 'card', 'drawing', 'region'],

    /** 焦点建立/替换。同一对象重复上报会被丢弃(选中态每次重绘都调也不会刷请求)。
     *  合并书(vbook)必须先归一到真实卷身份;归一不了就**不发**(见 _ogCanonicalRef)。 */
    focus: function (kind, ref) {
      if (!_ctxOn() || !kind || !ref) return false;
      if (_outgoing.kinds.indexOf(kind) < 0) return false;   // 宿主传了没登记的类型 → 静默不发
      var canon = _ogCanonicalRef(ref);
      if (!canon) return false;                              // fail closed:宁可少一条焦点
      // 去重签名用**归一后**的身份:同一逻辑对象在映射就绪前后不该被当成两次
      var sig = _ogSig(kind, canon);
      if (_og.focus === sig) return false;                   // 去重:没变就不发
      _og.focus = sig;
      _ogPost('/pdf/api/outgoing/focus', { kind: kind, ref: canon });
      return true;
    },

    /** 显式取消。取消后**不允许旧焦点复活**:签名清空,下次同一对象要重新走一次 set。 */
    cancel: function () {
      if (!_ctxOn()) return false;
      if (_og.focus === null) return false;                  // 本来就没有 → 不发空取消
      _og.focus = null;
      _ogPost('/pdf/api/outgoing/focus', { cancel: true });
      return true;
    },

    /** 绘图有改动(每一笔都可以调,内部合并)。停手约 1s 才真正去取版本。
     *  与 focus 同一条身份规则:vbook 归一不了就不排队,避免拿全局页去问绘图版本。 */
    drawingTouched: function (file, page) {
      if (!_ctxOn() || !file) return false;
      var canon = _ogCanonicalRef({ file: file, page: page });
      if (!canon) return false;                              // fail closed
      // 新笔/擦除一发生，上一张稳定合成图立即失效；真正截图仍等约 2s
      // 稳定确认，绝不逐笔捕获或逐笔发送大图。
      _ogEmitDrawing('changed', {
        file: canon.file,
        page: canon.page,
        drawingRevision: null
      });
      _og.drawPend = { file: canon.file, page: canon.page };
      _ogSchedDraw(_OG_DRAW_MS);
      return true;
    },

    /** 把某个绘图区绑成"可长按设为焦点"。**不碰指针状态机**:
     *  只监听 pointerType !== 'pen' 的指针(手指/鼠标),笔和橡皮压根不经过这里;
     *  全程 passive、不 preventDefault、不 stopPropagation → 画笔/擦除/滚动手势零影响。
     *  语义与项目既有「长按=加入上下文」一致(高亮/便签/图已是这套)。
     *  @param el      绘图层元素
     *  @param getCtx  () => {file, page, hasInk} —— 由宿主提供,决定当前页锚点与目标是否有效
     */
    bindDrawingFocus: function (el, getCtx) {
      if (!el || typeof getCtx !== 'function' || el.__ogBound) return false;
      el.__ogBound = true;
      var t = null, sx = 0, sy = 0, moved = false;
      var LONG_MS = 500, MOVE_PX = 10;
      function clear() { if (t) { clearTimeout(t); t = null; } }
      el.addEventListener('pointerdown', function (e) {
        if (e.pointerType === 'pen') return;      // 笔=画画,永不设焦点
        moved = false; sx = e.clientX; sy = e.clientY;
        clear();
        t = setTimeout(function () {
          t = null;
          if (moved || !_ctxOn()) return;
          var c = getCtx() || {};
          // 目标失效(本页没有墨迹)→ 什么都不做,更不能设一个空焦点
          if (!c.file || !c.hasInk) return;
          var d = _og.lastDrawing || {};
          var ref = {
            file: c.file, page: c.page,
            drawingRevision: (d.file === c.file ? d.drawingRevision : null) || null,
            region: c.region || null
          };
          // 再长按同一块 = 取消。签名必须用**与 focus() 完全相同的算法**算,
          // 否则"是不是同一块"永远判不相等(首版分别用 file#page 与 JSON,取消因此失效)。
          if (_og.focus === _ogSig('drawing', ref)) { _outgoing.cancel(); return; }
          _outgoing.focus('drawing', ref);
        }, LONG_MS);
      }, { passive: true });
      ['pointermove'].forEach(function (ev) {
        el.addEventListener(ev, function (e) {
          if (!t) return;
          if (Math.abs(e.clientX - sx) > MOVE_PX || Math.abs(e.clientY - sy) > MOVE_PX) {
            moved = true; clear();      // 位移=滚页/画,不是长按
          }
        }, { passive: true });
      });
      ['pointerup', 'pointercancel', 'pointerleave'].forEach(function (ev) {
        el.addEventListener(ev, clear, { passive: true });
      });
      return true;
    },

    /** 切页 / 墨迹被清空 / 目标失效 → 丢弃绘图焦点。只在当前焦点确实是绘图时才发。 */
    dropDrawingFocus: function () {
      if (!_ctxOn()) return false;
      if (!_og.focus || _og.focus.indexOf('drawing|') !== 0) return false;
      return _outgoing.cancel();
    },

    /** 最近一次取到的绘图引用(未稳定时 drawingRevision 为 null)。 */
    lastDrawing: function () { return _og.lastDrawing || null; },
    _state: function () { return _og; }
  };

  var _ctxSync = {
    LS_KEY: _CTX_LS,
    navDebounceMs: _CTX_NAV_MS,
    enabled: _ctxOn,
    _serverSnapshotEnabled: function () { return _ctxServerOwned() && _ctxOn(); },
    _applyServerMode: _ctxApplyServerMode,
    setBase: function (origin) { _ctxS.base = origin || ''; },   // 扩展在别人的站上跑,要显式指向 Pi
    // 宿主调这一个入口。patch:{kind:'pdf'|'epub'|'html'|'web', file|url, pos, title, total, selection, reason}
    report: function (patch, opts) {
      if (!patch || !patch.kind) return false;
      // 安全护栏:扩展在别人的站点里跑,相对路径会把书名/页码/标题 POST 到**第三方站点**。
      // 所以网页宿主必须先 setBase(Pi 源) 才允许上报;没设就干脆不发(宁可少一条上下文)。
      if (patch.kind === 'web' && !_ctxS.base) return false;
      var cur = _ctxS.pend || {}, k;
      // 换文档就整份重置:不然上一本的 title/selection 会粘到新书上(合并语义的经典坑)
      var same = cur.kind === patch.kind &&
        (patch.file !== undefined ? cur.file === patch.file : cur.url === patch.url);
      var next = {};
      if (same) { for (k in cur) if (Object.prototype.hasOwnProperty.call(cur, k)) next[k] = cur[k]; }
      for (k in patch) if (Object.prototype.hasOwnProperty.call(patch, k)) {
        if (patch[k] !== undefined && patch[k] !== null) next[k] = patch[k];
      }
      // 判定这次变更算不算「导航」:同一文档内只有页码动了 = 导航,合并;
      // 换书、选区建立/清空、标题或其它字段变化 = 即时。调用方也可用 opts.immediate 强制。
      // 首次上报(还没有基线)也算导航:否则开关刚打开时的第一次翻页会把**中间页**立刻推出去
      //(真机实测:连翻 6 页 → 途中 1 次)。带 immediate 的调用不受影响。
      var first = !_ctxS.pend;
      var navOnly = !(opts && opts.immediate) && (patch.pos !== undefined) &&
        (first || (same && patch.pos !== cur.pos && _ctxOnlyPosChanged(cur, next)));
      // 无变化就别发:宿主的选区漏斗每次 selectionchange 都会调一次,翻页会清选区 →
      // 一路发"selection 仍是空"的即时上报,把导航合并整个打穿(真机实测:连翻 6 页发了 4 次)。
      // 判等放共享层,三个宿主一次受益,也省得每个宿主各记一份 last 值。
      if (same && _ctxSameState(cur, next)) {
        // 只有视口动了:必须更新 pend(否则 dwell 那一发带的是旧视口,服务端按错误
        // 位置截取段落),但**不因此排定发送** —— 滚动本身不是要上报的事件。
        if (!_ctxViewportEq(cur.viewport, next.viewport)) _ctxS.pend = next;
        return false;
      }
      _ctxS.pend = next;
      if (!_ctxCanonicalMatches(next, _ctxS.canonical)) _ctxS.canonical = null;
      // App 页面可能先打开、ReaderPC 后启动。当 Windows 尚未授权时保留最新页事实,
      // 但不写本地/网络;收到 snapshot-mcp 后 _ctxApplyServerMode 会立即补发,无需再翻页。
      if (!_ctxOn()) return false;
      _ctxBind(true);
      _ctxSchedule(navOnly ? _CTX_NAV_MS : _CTX_NOW_MS);
      _ctxArmDwell(next);
      return true;
    },
    // 设置面板唯一入口:同时管两个方向(前端上报 + Pi→Windows 快照推送)
    setEnabled: function (on) {
      if (_ctxServerOwned()) {
        return Promise.resolve({
          ok: true,
          enabled: _ctxOn(),
          deliveryMode: _ctxServerMode,
          authority: 'readerpc'
        });
      }
      on = !!on;
      try { localStorage.setItem(_CTX_LS, on ? '1' : '0'); } catch (e) {}
      if (!on) {
        _ctxClear();
        _ctxS.pend = null;
        _ctxS.canonical = null;
        _ctxS.dirty = false;
        _ctxBind(false);
      }
      // @interaction context.sync.toggle
      return fetch(_ctxU('/pdf/api/context-sync'), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: on }), credentials: 'include'
      }).then(function (r) { return r.json(); });
    },
    getConfig: function () {
      // @interaction context.sync.read
      return fetch(_ctxU('/pdf/api/context-sync'), {
        method: 'GET',
        credentials: 'include',
        cache: 'no-store'
      }).then(function (r) { return r.json(); }).then(function (value) {
        if (!value || value.ok !== true ||
            (value.deliveryMode !== 'legacy-inject' &&
             value.deliveryMode !== 'snapshot-mcp')) {
          throw new Error('上下文同步配置无效');
        }
        return value;
      });
    },
    setDeliveryMode: function (mode) {
      if (mode !== 'legacy-inject' && mode !== 'snapshot-mcp') {
        return Promise.reject(new Error('上下文交付模式无效'));
      }
      if (!RC.computerVoice ||
          typeof RC.computerVoice.setContextDeliveryMode !== 'function') {
        return Promise.reject(new Error('Windows 电脑桥接器未加载'));
      }
      return RC.computerVoice.setContextDeliveryMode(mode);
    },
    _state: function () { return _ctxS; }   // 仅供自测/调试观察合并与在途状态
  };

  var _pre = window.RC || {};   // 先到的成员(如 rc-outbox.outbox)保留
  var RC = window.RC = {
    _adapter: null,
    // 各 reader 在自己脚本末尾 RC.use(adapter) 注册整套适配器方法
    use: function (adapter) {
      RC._adapter = adapter || {};
      RC._adapterAudit = _auditAdapter(RC._adapter);   // 最近一次登记快照；adapterAudit() 会实时重算晚绑定宿主
      return RC;
    },
    adapter: function () { return RC._adapter || {}; },
    adapterAudit: function () {
      RC._adapterAudit = _auditAdapter(RC._adapter || {});
      return RC._adapterAudit;
    },
    contract: {
      version: _CONTRACT_VERSION,
      endpoints: function (overrides) { return _copy(_COMMON_ENDPOINTS, overrides); },
      adapterConfig: function (profile, overrides) { return _copy(_CONFIG_PROFILES[profile] || {}, overrides); },
      selection: _selectionSnapshot,
      audit: _auditAdapter,
      requirements: function () {
        return { base: _BASE_METHODS.slice(), selection: _SELECTION_METHODS.slice(), assistantHost: _ASST_HOST_METHODS.slice() };
      }
    },
    // 能力开关位:门控 reader 专属逻辑(公式/单击词/语音/SSE三源…),防另一端报错或误触
    config: function () { var a = RC._adapter; return (a && a.config) || {}; },
    // 所有后端 URL 参数化(PDF=/api/assistant/chat,EPUB=/pdf/api/epub-chat 等)
    endpoints: function () { var a = RC._adapter; return (a && a.getEndpoints && a.getEndpoints()) || {}; },
    // 跨宿主动作只统一“调用入口与所有权”；处理函数仍登记在当前 adapter，存储/几何不进入共享层。
    actions: {
      bind: function (name, handler, meta) {
        var a = RC._adapter;
        if (!a || !name || typeof handler !== 'function') return false;
        a._actions = a._actions || {};
        a._actions[name] = { run: handler, meta: _copy(meta || {}, {}) };
        return true;
      },
      has: function (name) {
        var a = RC._adapter, e = a && a._actions && a._actions[name];
        return !!(e && typeof e.run === 'function');
      },
      run: function (name, payload, fallback) {
        var a = RC._adapter, e = a && a._actions && a._actions[name];
        if (e && typeof e.run === 'function') return e.run.call(a, payload || {});
        if (typeof fallback === 'function') return fallback(payload || {});
        throw new Error('当前宿主未登记动作：' + name);
      },
      audit: function () {
        var a = RC._adapter, actions = (a && a._actions) || {}, out = [];
        for (var name in actions) if (Object.prototype.hasOwnProperty.call(actions, name)) {
          var e = actions[name] || {}, meta = _copy(e.meta || {}, {});
          meta.name = name; out.push(meta);
        }
        out.sort(function (x, y) { return x.name < y.name ? -1 : (x.name > y.name ? 1 : 0); });
        return out;
      }
    },
    ctxSync: _ctxSync,
    outgoing: _outgoing,
    // ── 通用工具(纯,无底座耦合)──
    esc: function (s) { var d = document.createElement('div'); d.textContent = (s == null ? '' : String(s)); return d.innerHTML; },
    debounce: function (fn, ms) { var t; return function () { var a = arguments, c = this; clearTimeout(t); t = setTimeout(function () { fn.apply(c, a); }, ms); }; },
    // 容错 fetch JSON
    reqJson: function (method, url, body) {
      var o = { method: method, headers: {} };
      if (body) { o.headers['Content-Type'] = 'application/json'; o.body = JSON.stringify(body); }
      return fetch(url, o).then(function (r) { return r.json(); });
    },
    // 共享 toast(适配器可设 RC._adapter.toast 覆盖样式;默认底部居中)
    toast: function (msg) {
      var a = RC._adapter; if (a && a.toast) { try { a.toast(msg); return; } catch (e) {} }
      var el = document.getElementById('rc-toast');
      if (!el) {
        el = document.createElement('div'); el.id = 'rc-toast';
        el.style.cssText = 'position:fixed;left:50%;bottom:44px;transform:translateX(-50%);background:#10162a;border:1px solid #3b6db5;color:#cfe6ff;padding:8px 16px;border-radius:8px;font-size:13px;z-index:9000;box-shadow:0 6px 16px rgba(0,0,0,.6);transition:opacity .2s;pointer-events:none';
        document.body.appendChild(el);
      }
      el.textContent = msg; el.style.opacity = '1';
      clearTimeout(RC._toastT); RC._toastT = setTimeout(function () { el.style.opacity = '0'; }, 1400);
    }
  };
  for (var k in _pre) { if (!(k in RC)) RC[k] = _pre[k]; }   // 合并先到成员
})();
