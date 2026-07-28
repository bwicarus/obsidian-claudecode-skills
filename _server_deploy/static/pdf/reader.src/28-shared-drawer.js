// ═══════════ 28-shared-drawer.js — 唯一抽屉:rc-sidedrawer 接管 PDF 抽屉 chrome ═══════════
// 背景:此前 PDF/EPUB 各一套抽屉(PDF=模板静态 #grammar-panel+#side-tabs+18-grammar/05-nav 开合切换;
//   EPUB=rc-sidedrawer 动态 #ep-side),tab 集合也分叉(EPUB 有 高亮/目录,PDF 有 历史)。用户拍板「唯一存在」
//   → rc-sidedrawer 泛化为两 reader 唯一抽屉,本文件把 PDF 迁上去,并补齐 高亮/目录 两个 pane。
// 设计(映射见 references/…workflow wf_493a012e 地图):
//   · 镜像 body 类:rc-sidedrawer 开/悬浮时同步 body.grammar-open/.grammar-floating →
//     pdf-styles.css 既有挤压(#main/#header/#result-mask)/悬浮取消/窄屏规则原样生效,消费方 JS(12-vocab)零改。
//   · 静态 chrome(#side-handle/#side-tabs/#side-settings)摘除,由 rc-sidedrawer 自建(把手/tab 栏/⚙外观弹层);
//     抽屉根 #grammar-panel 改名 #ep-side(rc-sidedrawer 内部查询全按它;视觉底座换 rc 注入 CSS,同几何/同 z-index 120)。
//   · 4 个静态 pane(grammar/vocab/kg/hist)原地保留(加 .ep-side-pane 类,内部 id 全不动);
//     asst tab/pane 已由 rc-assistant 注入(27 先跑)→ tab 按钮搬进新 tab 栏,pane 就地换类。
//   · PDF 外观按排版分档(pdf-gp-*-{mode})经 opts.appearanceKeys 注入;重排经 opts.onReflow 走 _scheduleRefit;
//     双页临时切单列/还原走 opts.onLayoutChange;「🗑 清空分析」走 opts.tabButtons(仅 grammar tab 显示)。
//   · 旧入口(switchSideTab/openGrammarPanel/closeGrammarPanel/toggleGrammarPanel/toggleSidebar/toggleVocab/
//     _gpSet*/_gpApplyAppearance)全部改道 RC.sidedrawer,同名保留 → 26-figures/rc-assistant/模板兜底零改。
// 状态:浏览器验证通过(2026-07-07)→ 已翻默认;&drawer=legacy / #drawer=legacy = 旧 PDF 抽屉逃生舱
//   (旧抽屉 JS 的物理删除跟 ?ui=legacy 整体退役一个批次做,在那之前逃生舱免费)。
(() => {
  if (((location.search || '') + (location.hash || '')).indexOf('drawer=legacy') >= 0) return;
  if (!(window.RC && RC.sidedrawer && window.__uiShared)) return;
  const panel = document.getElementById('grammar-panel');
  if (!panel) return;
  window.__pdfSharedDrawer = true;

  // ── ① 接管前先保住 rc-assistant 注入的 asst tab(27 已跑,tab 在旧 #side-tabs 里)──
  const asstTabBtn = document.querySelector('#side-tabs .side-tab[data-pane="asst"]');
  if (asstTabBtn) asstTabBtn.remove();

  // ── ② 摘静态 chrome + 抽屉根改共享 id(清掉早期兜底可能留下的打开态)──
  ['side-handle', 'side-tabs', 'side-settings'].forEach(id => { const el = document.getElementById(id); if (el) el.remove(); });
  panel.classList.remove('open');
  document.body.classList.remove('grammar-open');
  panel.id = 'ep-side';
  panel.querySelectorAll('.side-pane').forEach(p => { p.classList.add('ep-side-pane'); p.classList.remove('active'); });

  // ── ③ 新 pane:高亮 / 目录(补齐与 EPUB 的 tab 对等)──
  const mkPane = (name, inner) => {
    const d = document.createElement('div');
    d.className = 'ep-side-pane'; d.dataset.pane = name; d.innerHTML = inner;
    panel.appendChild(d); return d;
  };
  mkPane('hl', '<div id="pdf-hl-list" style="padding:10px 12px"></div>');
  mkPane('toc', '<div id="pdf-toc-list" style="padding:10px 12px"></div>');

  // 高亮 pane:GET /pdf/api/highlights → rc-highlight.renderList(reader 无关,EPUB loadHlPane 同款)
  const _loadHlPane = () => {
    const box = document.getElementById('pdf-hl-list'); if (!box) return;
    box.innerHTML = '<div style="color:#5a6680;font-size:12px">加载…</div>';
    fetch('/pdf/api/highlights?file=' + encodeURIComponent(FILE_REL)).then(r => r.json()).then(d => {
      const hs = (d && d.highlights) || [];
      RC.highlight.renderList(box, hs, {
        reverse: true,
        emptyHtml: '还没有高亮。<br>选中文字 → 「🖍 高亮」',
        onJump: (h) => { try { jumpWithBack(h.page); RC.sidedrawer.afterJump(); } catch (_) {} },
        onDelete: (h) => {
          fetch('/pdf/api/highlights?file=' + encodeURIComponent(FILE_REL) + '&id=' + encodeURIComponent(h.id), { method: 'DELETE' })
            .then(() => { try { window._reloadHighlights && window._reloadHighlights(); } catch (_) {} })
            .catch(() => {});
        },
      });
    }).catch(() => { box.innerHTML = '<div style="color:#5a6680;font-size:12px">加载失败</div>'; });
  };

  // 目录 pane:GET /api/toc?entries=1(book_toc._effective_toc,page=印刷页)→ 简单列表(照 EPUB buildToc)
  let _tocLoadedOnce = false;
  const _loadTocPane = (force) => {
    const box = document.getElementById('pdf-toc-list'); if (!box) return;
    if (_tocLoadedOnce && !force) return;
    box.innerHTML = '<div style="color:#5a6680;font-size:12px">加载…</div>';
    fetch('/pdf/api/toc?file=' + encodeURIComponent(FILE_REL) + '&entries=1').then(r => r.json()).then(d => {
      const es = (d && d.entries) || [];
      _tocLoadedOnce = true;
      if (!es.length) { box.innerHTML = '<div style="color:#5a6680;font-size:12px;line-height:1.6">这本书还没有目录。<br>设置面板 →「书籍目录」可建立(原生书签或 AI 识别)。</div>'; return; }
      box.innerHTML = '';
      es.forEach(e => {
        const it = document.createElement('div');
        const lv = Math.max(0, (e.level || 1) - 1);
        it.textContent = e.title || '';
        it.title = '第 ' + (window._dispPage ? window._dispPage(e.page) : e.page) + ' 页';
        it.style.cssText = 'padding:6px 8px 6px ' + (8 + lv * 16) + 'px;font-size:' + (lv ? 12.5 : 13.5) + 'px;' +
          (lv ? 'color:#9aa7c4' : 'color:#dbe4f8;font-weight:600') + ';cursor:pointer;border-radius:6px;line-height:1.45';
        it.onmouseenter = () => { it.style.background = '#1a2540'; };
        it.onmouseleave = () => { it.style.background = ''; };
        it.onclick = () => {
          const pdfPage = (typeof _pdfFromDisp === 'function') ? _pdfFromDisp(e.page) : e.page;
          try { jumpWithBack(pdfPage); RC.sidedrawer.afterJump(); } catch (_) {}
        };
        box.appendChild(it);
      });
    }).catch(() => { box.innerHTML = '<div style="color:#5a6680;font-size:12px">加载失败</div>'; });
  };

  // ── ④ 唯一抽屉 init(tab 集合与 EPUB 同序:asst,vocab,kg,hl,toc,grammar + PDF 专属 hist 排尾)──
  const _si = (p) => '<svg class="si" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">' + p + '</svg>';
  RC.sidedrawer.init({
    tabs: [
      { name: 'vocab', label: '单词本', icon: _si('<path d="M6 4h11a1 1 0 0 1 1 1v15H8a2 2 0 0 1-2-2V4z"/><path d="M6 4a2 2 0 0 0-2 2v12a2 2 0 0 1 2-2h12"/>') },
      { name: 'kg', label: '知识点', icon: _si('<path d="M9 6h11M9 12h11M9 18h11"/><circle cx="4.5" cy="6" r="1.2" fill="currentColor" stroke="none"/><circle cx="4.5" cy="12" r="1.2" fill="currentColor" stroke="none"/><circle cx="4.5" cy="18" r="1.2" fill="currentColor" stroke="none"/>') },
      { name: 'hl', label: '高亮', icon: _si('<path d="M4 20h6M14 4l6 6-8.5 8.5H7v-4.5L14 4z"/>') },
      { name: 'toc', label: '目录', icon: _si('<path d="M4 6h16M4 12h16M4 18h16"/>') },
      { name: 'grammar', label: '语法', icon: _si('<path d="M4 6h16M4 12h11M4 18h7"/>') },
      { name: 'hist', label: '历史', icon: _si('<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>') },
    ],
    handleLabel: '助手 · 知识点',
    defaultTab: 'asst',
    mirrorOpenClass: 'grammar-open',
    mirrorFloatingClass: 'grammar-floating',
    appearanceKeys: (name) => 'pdf-gp-' + name + '-' + _gpMode(),   // 按排版分档(18-grammar._gpMode,同模块作用域)
    onReflow: () => { try { if (!document.body.classList.contains('grammar-floating') && typeof _scheduleRefit === 'function') _scheduleRefit(true); } catch (_) {} },   // 悬浮不重排防闪(照搬 18-grammar)
    onWidthChange: () => { try { if (!document.body.classList.contains('grammar-floating') && typeof _scheduleRefit === 'function') _scheduleRefit(true); } catch (_) {} },
    tabButtons: [{
      id: 'side-clear', title: '清空全部分析', tabs: ['grammar'],
      icon: _si('<path d="M4 7h16M10 4h4a1 1 0 0 1 1 1v2H9V5a1 1 0 0 1 1-1zM6 7l1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13"/>'),
      onClick: () => { try { window.clearGrammarBlocks && window.clearGrammarBlocks(); } catch (_) {} },
    }],
    onLayoutChange: (willOpen) => {   // 双页 spread + 挤压模式:开抽屉临时切单列,关还原(逐字照搬 18-grammar)
      try {
        if (willOpen) {
          if (readMode === 'spread' && !document.body.classList.contains('grammar-floating')) {
            _spreadBeforePanel = _spreadOffset; readMode = 'continuous'; _updateModeButtons();
          } else {
            _spreadBeforePanel = null;   // 单列下开栏:清残留标记(修"单页开关侧栏被莫名切到双页")
          }
        } else {
          try { _hideDepTip(); } catch (_) {}
          if (_spreadBeforePanel != null && readMode === 'continuous') {   // 仅还原"确实被临时切走的"
            readMode = 'spread'; _spreadOffset = _spreadBeforePanel; _updateModeButtons();
          }
          _spreadBeforePanel = null;
        }
      } catch (_) {}
      return null;   // 重排走 onReflow(_scheduleRefit 自带防抖),不需要滚动锚点回调
    },
    onTab: (name) => {   // 懒加载分发(平移自 05-nav switchSideTab 四钩子 + 新 hl/toc + asst 镜像 EPUB)
      if (name === 'vocab') { if (!_vocabLoaded) loadVocabList(); }
      else if (name === 'grammar') { if (!_grammarHistLoaded) loadGrammarHistory(); }
      else if (name === 'kg') loadPageNodes(currentPage);
      else if (name === 'hist') renderQueryHistory();
      else if (name === 'hl') _loadHlPane();
      else if (name === 'toc') _loadTocPane();
      else if (name === 'asst') {
        try { window.__renderFigChips && window.__renderFigChips(); } catch (_) {}
        try { window.__renderNoteChips && window.__renderNoteChips(); } catch (_) {}
        try { window.__renderFocusSel && window.__renderFocusSel(); } catch (_) {}
        try { window.__asstPrewarm && window.__asstPrewarm(); } catch (_) {}
        setTimeout(() => { const ta = document.getElementById('asst-ta'); if (ta) ta.focus(); }, 120);
      }
    },
  });

  // ── ⑤ asst tab 归位:搬进新 tab 栏第一位(类名换共享;active 态与 init 同步过的 pane 对齐)──
  if (asstTabBtn) {
    asstTabBtn.classList.remove('side-tab'); asstTabBtn.classList.add('ep-side-tab');
    const bar = document.getElementById('ep-side-tabs');
    if (bar) bar.insertBefore(asstTabBtn, bar.firstChild);
    const act = panel.querySelector('.ep-side-pane.active');
    asstTabBtn.classList.toggle('active', !!(act && act.dataset.pane === 'asst'));
  }

  // ── ⑥ 旧入口全部改道(同名覆盖;调用方 26-figures/rc-assistant HOST/模板兜底零改)──
  window.switchSideTab = (p) => RC.sidedrawer.setTab(p);
  openGrammarPanel = () => RC.sidedrawer.open();          // 模块级绑定重指(18-grammar 内部 callers 一起改道)
  window.closeGrammarPanel = () => RC.sidedrawer.close();
  window.toggleGrammarPanel = () => {                     // 把手/顶栏按钮:开着且在助手 → 关;否则开到助手(原语义)
    const onAsst = document.querySelector('#ep-side-tabs .ep-side-tab[data-pane="asst"]')?.classList.contains('active');
    if (RC.sidedrawer.isOpen() && onAsst) { RC.sidedrawer.close(); return; }
    RC.sidedrawer.open('asst');
  };
  window.toggleSidebar = () => {                          // 「📋 知识点」按钮
    const on = document.querySelector('#ep-side-tabs .ep-side-tab[data-pane="kg"]')?.classList.contains('active');
    if (RC.sidedrawer.isOpen() && on) { RC.sidedrawer.close(); return; }
    RC.sidedrawer.open('kg');
  };
  window.toggleVocab = () => {                            // 「单词本」按钮
    const on = document.querySelector('#ep-side-tabs .ep-side-tab[data-pane="vocab"]')?.classList.contains('active');
    if (RC.sidedrawer.isOpen() && on) { RC.sidedrawer.close(); return; }
    RC.sidedrawer.open('vocab');
  };
  window._gpSetFloating = (on) => RC.sidedrawer.setFloating(on);
  window._gpSetBlur = (v) => RC.sidedrawer.setBlur(v);
  window._gpApplyAppearance = () => RC.sidedrawer.applyAppearance();   // 06-layout 切排版后调 → 按新档重应用
})();
