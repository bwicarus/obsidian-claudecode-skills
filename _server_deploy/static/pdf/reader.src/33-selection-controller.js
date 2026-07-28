// ═══════════ 33-selection-controller.js — 统一当前选区 + 外部内容宿主入口 ═══════════
//
// reader.js 是一个 ES module，lastSelText / _charSel / toolbar 都是模块词法状态。iframe 或
// 扩展脚本不能靠写 window 上的同名属性假装改变它们；所有外部选区必须进入这里，再由这里同步
// 词法状态、上下文、预览和工具条。PDF 原生选区仍由 13/14/16 的既有路径写入，本桥只提供统一
// 读取和外部宿主写入口，不改变 PDF 字符层几何。
(() => {
  let externalSelection = null;

  function _normalized(input) {
    if (!input) return null;
    const raw = {
      text: String(input.text == null ? '' : input.text).slice(0, 20000),
      context: String(input.context != null ? input.context
        : (input.ctx != null ? input.ctx : (input.sentence || ''))).slice(0, 50000),
      rect: input.rect || null,
      anchor: input.anchor || null,
      data: Object.assign({}, input.data || {}, {
        source: String(input.source || (input.data && input.data.source) || 'external')
      })
    };
    if (window.RC && RC.contract && RC.contract.selection) return RC.contract.selection(raw);
    if (!raw.text.trim()) return null;
    raw.ctx = raw.context;
    raw.sentence = raw.context;
    return raw;
  }

  function _adapter() {
    try { return window.RC && RC.adapter ? RC.adapter() : null; }
    catch (_) { return null; }
  }

  function current() {
    const adapter = _adapter();
    if (adapter && typeof adapter.captureSelection === 'function') {
      try {
        const captured = _normalized(adapter.captureSelection());
        if (captured) return captured;
      } catch (_) {}
    }
    // legacy / 非 shared PDF 仍可从真实模块词法值读取；不为它杜撰 anchor。
    return _normalized({
      text: lastSelText || '',
      context: (typeof window.__lastSelSentence === 'string' ? window.__lastSelSentence : ''),
      rect: externalSelection && externalSelection.rect,
      data: externalSelection && externalSelection.data
    });
  }

  function _positionToolbar(rect) {
    if (!toolbar || !rect) return;
    const left = Number(rect.left);
    const bottom = Number(rect.bottom);
    if (!Number.isFinite(left) || !Number.isFinite(bottom)) return;
    const width = toolbar.offsetWidth || 320;
    const height = toolbar.offsetHeight || 52;
    toolbar.style.position = 'fixed';
    toolbar.style.left = Math.max(
      8,
      Math.min((window.innerWidth || 1024) - width - 8, left)
    ) + 'px';
    toolbar.style.top = Math.max(
      8,
      Math.min((window.innerHeight || 768) - height - 8, bottom + 8)
    ) + 'px';
    toolbar.style.zIndex = '900';
  }

  function acceptExternal(input) {
    const selection = _normalized(input);
    const source = String(input && input.source || 'external');
    const adapter = _adapter();
    // 外部 web frame 不能在当前 PDF/EPUB adapter 上写入词法选区。
    if (adapter && adapter.kind && source !== 'external' && adapter.kind !== source) return null;

    if (!selection) {
      clearExternal(source);
      return null;
    }
    externalSelection = selection;
    _charSel = null;                         // 明确不伪造 PDF char geometry
    lastSelText = selection.text;            // 真正更新 reader.js 模块词法状态
    _updateSelPreview(lastSelText);
    try {
      window.__lastSelSentence = selection.context || '';
      window.__lastSelMeta = {
        kind: source,
        url: selection.data && selection.data.url || '',
        t: Date.now()
      };
    } catch (_) {}
    try { if (typeof _updateGrammarBtnVisibility === 'function') _updateGrammarBtnVisibility(); } catch (_) {}
    _positionToolbar(selection.rect);
    if (toolbar) toolbar.classList.add('open');
    try {
      document.dispatchEvent(new CustomEvent('bw:selection-changed', {
        detail: { source, text: selection.text, context: selection.context }
      }));
    } catch (_) {}
    return selection;
  }

  function clearExternal(source) {
    source = String(source || 'external');
    const activeSource = externalSelection && externalSelection.data
      ? String(externalSelection.data.source || 'external') : '';
    if (activeSource && source !== 'external' && source !== activeSource) return false;
    externalSelection = null;
    _charSel = null;
    lastSelText = '';
    _updateSelPreview('');
    try { window.__lastSelSentence = ''; window.__lastSelMeta = null; } catch (_) {}
    if (toolbar) toolbar.classList.remove('open');
    return true;
  }

  const controller = Object.freeze({
    contract: 'selection-controller/1',
    current,
    acceptExternal,
    clearExternal,
    adapter: _adapter
  });
  window.__bwSelectionController = controller;
  // 稳定的外部入口名：WebAdapter / 后续 EPUB iframe adapter 只依赖它，不接触模块词法变量。
  window.__setExternalSelection = (selection) => controller.acceptExternal(selection);
})();
