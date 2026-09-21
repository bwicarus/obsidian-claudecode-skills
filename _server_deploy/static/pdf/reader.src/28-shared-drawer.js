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

  // ── ① 接管前先保住 rc-assistant 注入的 tab(27 已跑,tab 在旧 #side-tabs 里):asst + 「操作」等共享 tab 全部保住,
  //      下面 ② 会把 #side-tabs 整个摘掉,漏了的就消失(2026-09-15 用户:「操作 tab 我没看到」)──
  const sharedTabBtns = Array.from(document.querySelectorAll('#side-tabs .side-tab[data-pane]'));
  sharedTabBtns.forEach(b => b.remove());
  const asstTabBtn = sharedTabBtns.find(b => b.dataset.pane === 'asst') || null;

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
    fetch('/pdf/api/highlights?file=' + encodeURIComponent(FILE_REL)).then(r => {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }).then(d => {
      if (!d || d.ok === false || !Array.isArray(d.highlights)) throw new Error((d && d.error) || '响应无效');
      const hs = d.highlights;
      RC.highlight.renderList(box, hs, {
        reverse: true,
        emptyHtml: '还没有高亮。<br>选中文字 → 「🖍 高亮」',
        onJump: (h) => { try { jumpWithBack(h.page); RC.sidedrawer.afterJump(); } catch (_) {} },
        // 专门的高亮管理页直接复用统一删除,不再自己发一套 fetch。
        //
        // 只把列表行删掉是不够的:_allHighlights / _hlByPage 与当前页的叠层都要跟着变。
        // 而 shared 模式下 25-assistant.js 顶部提前 return,window._reloadHighlights
        // 根本没有注册,所以"删完刷新一下"这条退路在这里是不存在的。
        // _hlDelete 一次做完四件事:请求后端、更新内存、重绘该页、给出提示,
        // 并返回 true/false 供列表决定要不要移除该行。
        onDelete: (h) => {
          const pw = document.querySelector(
            '.page-wrap[data-page-num="' + (h && h.page) + '"]'
          );
          return _hlDelete(h, pw);
        },

      });
    }).catch(error => {
      box.innerHTML = '<div style="color:#d88;font-size:12px">加载失败：' +
        String((error && error.message) || '未知原因').replace(/[&<>"']/g, s => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[s])) +
        '<br><button type="button" id="pdf-hl-retry">重试</button></div>';
      document.getElementById('pdf-hl-retry')?.addEventListener('click', _loadHlPane);
    });
  };

  // 目录 pane:GET /api/toc?entries=1(book_toc._effective_toc,page=印刷页)→ 简单列表(照 EPUB buildToc)
  let _tocLoadedOnce = false;
  // Shared data/navigation entry points: native controls never click drawer rows.
  RC.readerTOC = {
    read: async (options) => {
      const r = await fetch('/pdf/api/toc?file=' + encodeURIComponent(FILE_REL) + '&entries=1', { signal: options?.signal });
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const d = await r.json();
      if (!d || d.ok === false || !Array.isArray(d.entries)) throw new Error(d?.error || '目录加载失败');
      return d.entries.map(e => ({ title: String(e.title || ''), level: Math.max(1, Number(e.level) || 1),
        label: 'P' + e.page, locator: Number(e.page) }));
    },
    jump: (entry) => {
      if (!Number.isInteger(entry?.locator)) throw new Error('目录位置已失效');
      const page = typeof _pdfFromDisp === 'function' ? _pdfFromDisp(entry.locator) : entry.locator;
      if (page < 1) throw new Error('目录页码超出书籍范围');
      return jumpWithBack(page);
    }
  };
  const _loadTocPane = (force) => {
    const box = document.getElementById('pdf-toc-list'); if (!box) return;
    if (_tocLoadedOnce && !force) return;
    box.innerHTML = '<div style="color:#5a6680;font-size:12px">加载…</div>';
    RC.readerTOC.read().then(es => {
      _tocLoadedOnce = true;
      if (!es.length) { box.innerHTML = '<div style="color:#5a6680;font-size:12px;line-height:1.6">这本书还没有目录。<br>设置面板 →「书籍目录」可建立(原生书签或 AI 识别)。</div>'; return; }
      box.innerHTML = '';
      es.forEach(e => {
        const it = document.createElement('div');
        const lv = Math.max(0, (e.level || 1) - 1);
        it.textContent = e.title || '';
        it.title = e.label;
        it.style.cssText = 'padding:6px 8px 6px ' + (8 + lv * 16) + 'px;font-size:' + (lv ? 12.5 : 13.5) + 'px;' +
          (lv ? 'color:#9aa7c4' : 'color:#dbe4f8;font-weight:600') + ';cursor:pointer;border-radius:6px;line-height:1.45';
        it.onmouseenter = () => { it.style.background = '#1a2540'; };
        it.onmouseleave = () => { it.style.background = ''; };
        it.onclick = () => {
          try { RC.readerTOC.jump(e); RC.sidedrawer.afterJump(); } catch (_) {}
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

  // ── ⑤ 共享 tab 归位:按原顺序搬进新 tab 栏最前面(类名换共享;active 态与 init 同步过的 pane 对齐)──
  {
    const bar = document.getElementById('ep-side-tabs');
    const act = panel.querySelector('.ep-side-pane.active');
    let anchor = bar ? bar.firstChild : null;
    sharedTabBtns.forEach(b => {
      // ⚠ 只搬新 tab 栏**没有**的那些 —— 这段代码的本意是保住 rc-assistant 注入的
      //   asst / 操作，不是把 init 里已经声明过的 grammar/vocab/kg/hist 也搬回来。
      //   一起搬的结果是同一个 pane 两个按钮（2026-09-20 用户：「同一个内容出现两个
      //   图标，有字和无字的」），而且两个都会被标成 active。
      if (bar && bar.querySelector('.ep-side-tab[data-pane="' + b.dataset.pane + '"]')) {
        return;
      }
      b.classList.remove('side-tab'); b.classList.add('ep-side-tab');
      // ⚠ 旧按钮的标签是**裸文本节点**（`<svg…/>助手`），不在 .ep-side-tab-lb 里，
      //   于是窄屏那条「只显图标」的规则对它无效 —— 旧的永远带字、新的永远不带，
      //   并排就是不一致。搬进来时统一包一层，让同一条规则管得到。
      Array.from(b.childNodes).forEach(n => {
        if (n.nodeType !== 3 || !n.textContent.trim()) return;
        const lb = document.createElement('span');
        lb.className = 'ep-side-tab-lb'; lb.textContent = n.textContent.trim();
        b.replaceChild(lb, n);
      });
      if (bar) bar.insertBefore(b, anchor);
      b.classList.toggle('active', !!(act && act.dataset.pane === b.dataset.pane));
    });
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
