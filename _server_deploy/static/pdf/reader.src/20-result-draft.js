function openResult(title, src, contentHtml) {
  try { _pushQueryHistory(); } catch (_) {}   // 开新结果前，把上一个结果快照进「📜 历史」
  _resultReqId++;   // 新结果框 → 作废所有进行中的旧异步任务（查词/翻译/解释），它们的延迟回调将被丢弃
  document.getElementById('result-title').textContent = title;
  document.getElementById('result-src').textContent = src;
  document.getElementById('result-content').innerHTML = contentHtml;
  document.getElementById('result-content').scrollTop = 0;   // 新结果回到顶部
  document.getElementById('result-content').dataset.title = title;
  document.getElementById('result-content').dataset.src = src;
  // 清掉上次 vocab-actions（翻译/解释/AI 调用不需要它）
  const va = document.getElementById('vocab-actions');
  if (va) { va.className = ''; va.innerHTML = ''; }
  document.getElementById('result-mask').classList.add('open');
  if (window.MathJax && MathJax.typesetPromise) {
    MathJax.typesetPromise([document.getElementById('result-content')]).catch(()=>{});
  }
}
window.closeResult = () => { try { _pushQueryHistory(); } catch (_) {} document.getElementById('result-mask').classList.remove('open'); };

// ──────── AI 回答里的加号选中（同 QA browser 风格） ────────
function _headLevel(h) { return parseInt(h.tagName.slice(1), 10); }
function _isFakeHead(h) { return h.classList && h.classList.contains('fake-head'); }
function addResultPickers() {
  const md = document.getElementById('result-content');
  if (!md) return;
  // 先清掉旧的（流式过程中会被多次 marked.parse 覆盖）
  md.querySelectorAll('.head-pick').forEach(b => b.remove());
  md.querySelectorAll('.has-pick').forEach(el => el.classList.remove('has-pick'));
  const existingAll = document.querySelector('.reply-pick-all-result');
  if (existingAll) existingAll.remove();

  const realHeads = Array.from(md.querySelectorAll('h1,h2,h3,h4,h5,h6'));
  // 粗体段落假标题（AI 常用 **标题** 而非 ## 标题）
  const fakeHeads = [];
  md.querySelectorAll('p, li').forEach(el => {
    if (el.closest('h1,h2,h3,h4,h5,h6')) return;
    const strongs = el.querySelectorAll('strong');
    if (strongs.length !== 1) return;
    const t = (el.textContent || '').trim();
    const st = (strongs[0].textContent || '').trim();
    if (t.length >= 3 && st.length >= 2 && st.length / t.length >= 0.85) {
      el.classList.add('fake-head');
      fakeHeads.push(el);
    }
  });
  const heads = [...realHeads, ...fakeHeads];

  // 右上角小加号（选用整条回答），定位在 result-modal 标题旁
  const modal = document.getElementById('result-modal');
  let all = modal.querySelector('.reply-pick-all-result');
  if (!all) {
    all = document.createElement('button');
    all.className = 'pick-btn reply-pick-all-result';
    all.title = '选用整条回答（加入草稿）';
    all.style.position = 'absolute';
    all.style.right = '22px';
    all.style.top = '20px';
    modal.style.position = 'relative';
    modal.appendChild(all);
  }
  all.textContent = '+';
  all.classList.remove('on');
  all.onclick = (e) => {
    e.stopPropagation();
    const on = all.classList.toggle('on');
    all.textContent = on ? '✓' : '+';
    md.classList.toggle('picked-all', on);
    const fullText = (md.dataset.raw || md.textContent || '').trim();
    if (on) {
      if (fullText && !_drafts.some(d => d.text === fullText)) {
        _drafts.push({
          id: 'd_' + Date.now() + '_' + Math.random().toString(36).slice(2,6),
          text: fullText,
          source: md.dataset.title || '',
          src: md.dataset.src || '',
          time: Date.now(),
          selected: true,
        });
        _persistDrafts();
        _updateDraftBadge();
      }
    } else {
      _drafts = _drafts.filter(d => d.text !== fullText);
      _persistDrafts();
      _updateDraftBadge();
    }
  };
  md.dataset.raw = md.textContent || '';

  if (!heads.length) return;
  let singleTopCovers = false, singleTop = null;
  if (realHeads.length) {
    const minLvl = Math.min(...realHeads.map(_headLevel));
    const tops = realHeads.filter(h => _headLevel(h) === minLvl);
    if (tops.length === 1 && md.firstElementChild === tops[0]) {
      singleTopCovers = true; singleTop = tops[0];
    }
  }
  heads.forEach(h => {
    if (singleTopCovers && h === singleTop) return;
    if (h.querySelector('.head-pick')) return;
    h.classList.add('has-pick');
    const btn = document.createElement('button');
    btn.className = 'pick-btn head-pick';
    btn.textContent = '+';
    btn.onclick = (e) => { e.stopPropagation(); _toggleResultPick(h, md); };
    h.appendChild(btn);
  });
}
function _toggleResultPick(h, md) {
  const newOn = !h.querySelector('.head-pick').classList.contains('on');
  if (!_isFakeHead(h) && /^H[1-6]$/.test(h.tagName)) {
    const heads = Array.from(md.querySelectorAll('h1,h2,h3,h4,h5,h6'));
    const i = heads.indexOf(h);
    const lvl = _headLevel(h);
    _setResultPick(h, newOn);
    for (let j = i + 1; j < heads.length; j++) {
      if (_headLevel(heads[j]) <= lvl) break;
      _setResultPick(heads[j], newOn);
    }
  } else {
    _setResultPick(h, newOn);
  }
  _collectAndPersistResultSelection();
}
function _setResultPick(h, on) {
  const btn = h.querySelector('.head-pick');
  if (btn) btn.classList.toggle('on', on);
  h.classList.toggle('hsec-picked', on);
  let sib = h.nextElementSibling;
  while (sib && !/^H[1-6]$/.test(sib.tagName) && !(sib.classList && sib.classList.contains('fake-head'))) {
    sib.classList.toggle('hsec-picked-body', on);
    sib = sib.nextElementSibling;
  }
}
function _collectSelectedSectionTexts() {
  const md = document.getElementById('result-content');
  if (!md) return [];
  const parts = [];
  md.querySelectorAll('h1,h2,h3,h4,h5,h6,p.fake-head,li.fake-head').forEach(h => {
    const btn = h.querySelector('.head-pick');
    if (!btn || !btn.classList.contains('on')) return;
    let txt;
    if (/^H[1-6]$/.test(h.tagName)) {
      txt = '#'.repeat(_headLevel(h)) + ' ' + (h.textContent || '').replace(/\+\s*$/, '').trim();
    } else {
      txt = (h.textContent || '').replace(/\+\s*$/, '').trim();
    }
    let sib = h.nextElementSibling;
    while (sib && !/^H[1-6]$/.test(sib.tagName) && !(sib.classList && sib.classList.contains('fake-head'))) {
      const t = (sib.textContent || '').trim();
      if (t) txt += '\n' + t;
      sib = sib.nextElementSibling;
    }
    parts.push(txt.trim());
  });
  return parts;
}
function _collectAndPersistResultSelection() {
  // 收集当前 modal 内已选段，去重 push 到 _drafts
  const texts = _collectSelectedSectionTexts();
  const title = document.getElementById('result-content').dataset.title || '';
  const src = document.getElementById('result-content').dataset.src || '';
  for (const t of texts) {
    if (!t) continue;
    if (!_drafts.some(d => d.text === t)) {
      _drafts.push({id: 'd_' + Date.now() + '_' + Math.random().toString(36).slice(2,6), text: t, source: title, src: src, time: Date.now(), selected: true});
    }
  }
  _persistDrafts();
  _updateDraftBadge();
}

// ──────── 草稿列表（多个 AI 回答的勾选段累积） ────────
let _drafts = [];
try { _drafts = JSON.parse(localStorage.getItem('pdf-drafts') || '[]'); } catch (_) {}
function _persistDrafts() {
  try { localStorage.setItem('pdf-drafts', JSON.stringify(_drafts)); } catch (_) {}
}
function _syncDraftsLS() {   // 共享模式(?ui=shared):reader.js 与 rc-result 各有 _drafts 内存副本,读前从 localStorage 重载,rc-result 的「+选段」才对 reader.js 的草稿框/制卡可见;默认路径(__uiShared 未定义)early-return 零改动
  if (!window.__uiShared) return;
  try { _drafts = JSON.parse(localStorage.getItem('pdf-drafts') || '[]'); } catch (_) {}
}
// 直接把一段文本(如公式 LaTeX)加进草稿,供「制卡/笔记」用(公式浮层等外部调用)
window.addDraftText = (text, source, src) => {
  _syncDraftsLS();   // 共享模式:先从 localStorage 重载,别用陈旧 _drafts 覆盖掉 rc-result「+选段」刚写入的草稿
  const t = (text || '').trim();
  if (!t) return false;
  if (_drafts.some(d => d.text === t)) { _updateDraftBadge(); return true; }
  _drafts.push({ id: 'd_' + Date.now() + '_' + Math.random().toString(36).slice(2, 6),
                 text: t, source: source || '公式', src: src || '', time: Date.now(), selected: true });
  _persistDrafts(); _updateDraftBadge();
  return true;
};
function _updateDraftBadge() {
  _syncDraftsLS();
  const b = document.getElementById('draft-badge');
  document.getElementById('draft-count').textContent = _drafts.length;
  b.classList.toggle('show', _drafts.length > 0);
}
window.openDraftModal = () => {
  _syncDraftsLS();
  const list = document.getElementById('draft-list');
  const cnt = document.getElementById('draft-modal-count');
  cnt.textContent = `（共 ${_drafts.length} 段，已选 ${_drafts.filter(d => d.selected).length}）`;
  if (!_drafts.length) {
    list.innerHTML = '<div class="draft-empty">还没有勾选任何段落。<br>在 AI 回答里点 + 按钮收集段落。</div>';
  } else {
    list.innerHTML = _drafts.map((d) => `
      <div class="draft-item-wrap" data-id="${d.id}">
        <div class="draft-item ${d.selected ? 'selected' : ''}">
          <div class="body">
            <div class="src">${(d.source||'').replace(/&/g,'&amp;').replace(/</g,'&lt;')} · ${new Date(d.time).toLocaleString().slice(5,16)}</div>
            <div class="text" id="dt-${d.id}">${d.text.slice(0, 200).replace(/&/g,'&amp;').replace(/</g,'&lt;')}${d.text.length>200?'…':''}</div>
          </div>
          <div class="sel-circle" title="${d.selected?'已选（点击取消）':'未选（点击勾选）'}"></div>
        </div>
        <button class="draft-item-del-row" type="button" title="删除">🗑</button>
      </div>
    `).join('');
    // 绑定每条 draft 的交互
    list.querySelectorAll('.draft-item-wrap').forEach(w => _bindDraftItem(w));
  }
  document.getElementById('draft-mask').classList.add('open');
};
window.closeDraftModal = () => document.getElementById('draft-mask').classList.remove('open');

// 草稿项交互：圆圈右侧（单击=切换 selected）；body 单击=展开；触屏左滑 / 鼠标 body 拖 = 下方滑出删除栏
function _bindDraftItem(wrap) {
  const id = wrap.dataset.id;
  const item = wrap.querySelector('.draft-item');
  const body = wrap.querySelector('.body');
  const circle = wrap.querySelector('.sel-circle');
  const delBtn = wrap.querySelector('.draft-item-del-row');
  if (!item || !circle) return;
  const reveal = () => { wrap.classList.add('swiped'); item.style.transform = ''; };
  const reset  = () => { wrap.classList.remove('swiped'); item.style.transform = ''; };

  delBtn.onclick = (e) => { e.stopPropagation(); deleteDraft(id); };

  // 圆圈单击 = 切换 selected（如果当前 swiped 态先复位）
  circle.addEventListener('click', (e) => {
    e.stopPropagation();
    if (wrap.classList.contains('swiped')) { reset(); return; }
    toggleDraftSel(id);
  });

  // 触屏：item 整体左滑揭示删除
  let sx = 0, sy = 0, dx = 0, dy = 0, swiping = false, axis = '';
  item.addEventListener('touchstart', (e) => {
    const t = e.touches[0]; sx = t.clientX; sy = t.clientY; dx = dy = 0; swiping = true; axis = '';
  }, {passive: true});
  item.addEventListener('touchmove', (e) => {
    if (!swiping) return;
    const t = e.touches[0];
    dx = t.clientX - sx; dy = t.clientY - sy;
    if (!axis && (Math.abs(dx) > 6 || Math.abs(dy) > 6)) {
      axis = Math.abs(dx) > Math.abs(dy) ? 'x' : 'y';
    }
    if (axis === 'x') {
      if (dx < 0) item.style.transform = `translateX(${Math.max(dx,-80)}px)`;
      else if (wrap.classList.contains('swiped')) item.style.transform = `translateX(${Math.min(dx,30)}px)`;
    }
  }, {passive: true});
  item.addEventListener('touchend', () => {
    swiping = false;
    if (axis === 'x') {
      if (dx < -40) reveal();
      else if (dx > 30 || !wrap.classList.contains('swiped')) reset();
    }
    dx = dy = 0; axis = '';
  });

  // 鼠标：在 body 区域水平拖 → swipe；纯点击 = 展开
  let md = false, mx = 0, mdx = 0, dragged = false;
  body.addEventListener('mousedown', (e) => {
    md = true; mx = e.clientX; mdx = 0; dragged = false;
  });
  const onMove = (e) => {
    if (!md) return;
    mdx = e.clientX - mx;
    if (Math.abs(mdx) > 4) dragged = true;
    if (mdx < 0) item.style.transform = `translateX(${Math.max(mdx,-80)}px)`;
    else if (wrap.classList.contains('swiped')) item.style.transform = `translateX(${Math.min(mdx,30)}px)`;
  };
  const onUp = () => {
    if (!md) return;
    md = false;
    if (dragged) {
      if (mdx < -40) reveal();
      else if (mdx > 30 || !wrap.classList.contains('swiped')) reset();
    }
    mdx = 0;
  };
  document.addEventListener('mousemove', onMove);
  document.addEventListener('mouseup', onUp);

  // body 单击 = 展开/收起；处于 swiped 态则先复位
  body.addEventListener('click', (e) => {
    if (dragged) { dragged = false; e.stopPropagation(); return; }
    if (wrap.classList.contains('swiped')) { reset(); return; }
    expandDraft(id);
  });

  // 清理 listener：modal 关闭时调用（openDraftModal 重新渲染会重复绑定，需要清旧）
  // 这里粗暴依赖每次 openDraftModal 重写 innerHTML（旧 listener 随 DOM 销毁自动 GC，
  // document.mousemove/mouseup 还在但闭包引用的 wrap/md 状态已脱离 DOM，无副作用）
}
window.toggleDraftSel = (id) => {
  const d = _drafts.find(x => x.id === id);
  if (!d) return;
  d.selected = !d.selected;
  _persistDrafts();
  openDraftModal();
};
window.expandDraft = (id) => {
  const el = document.getElementById('dt-' + id);
  const d = _drafts.find(x => x.id === id);
  if (!el || !d) return;
  // 切换全文/截断显示
  if (el.dataset.full === '1') {
    el.textContent = d.text.slice(0, 200) + (d.text.length > 200 ? '…' : '');
    el.dataset.full = '0';
    el.style.maxHeight = '80px';
  } else {
    el.textContent = d.text;
    el.dataset.full = '1';
    el.style.maxHeight = 'none';
  }
};
window.deleteDraft = (id) => {
  _drafts = _drafts.filter(d => d.id !== id);
  _persistDrafts();
  _updateDraftBadge();
  openDraftModal();
};
window.clearAllDrafts = () => {
  if (!confirm('清空所有 ' + _drafts.length + ' 段已选内容？')) return;
  _drafts = [];
  _persistDrafts();
  _updateDraftBadge();
  closeDraftModal();
};

// ── 后台任务进度条（右下角堆叠）：AI 创建笔记/Anki 不阻塞阅读器 ──
let _bgJobSeq = 0;
function _ensureBgJobsEl() {
  let c = document.getElementById('bg-jobs');
  if (!c) {
    c = document.createElement('div'); c.id = 'bg-jobs';
    c.style.cssText = 'position:fixed;right:18px;bottom:80px;display:flex;flex-direction:column;gap:6px;z-index:520;align-items:flex-end';
    document.body.appendChild(c);
  }
  return c;
}
function _startBgJob(text) {
  const id = 'bgj' + (++_bgJobSeq);
  const el = document.createElement('div'); el.id = id;
  el.style.cssText = 'background:#10162a;border:1px solid #3b6db5;color:#cfe6ff;padding:7px 12px;border-radius:8px;font-size:12px;box-shadow:0 4px 12px rgba(0,0,0,.5);max-width:280px';
  el.textContent = '⏳ ' + text;
  _ensureBgJobsEl().appendChild(el);
  return id;
}
function _finishBgJob(id, text, openUrl) {
  const el = document.getElementById(id); if (!el) return;
  el.style.borderColor = '#34d399'; el.style.color = '#34d399';
  el.textContent = '✓ ' + text + (openUrl ? ' · 点击打开' : '');
  if (openUrl) { el.style.cursor = 'pointer'; el.onclick = () => { location.href = openUrl; }; }
  setTimeout(() => { el.style.transition = 'opacity .4s'; el.style.opacity = '0'; setTimeout(() => el.remove(), 400); }, openUrl ? 8000 : 4500);
}
function _failBgJob(id, text, restore) {
  const el = document.getElementById(id); if (!el) return;
  el.style.borderColor = '#f87171'; el.style.color = '#f87171'; el.style.cursor = 'pointer';
  el.textContent = '✗ ' + text + ' · 点关闭';
  el.onclick = () => el.remove();
  // 失败 → 把段落放回草稿，方便重试
  if (restore && restore.length) {
    for (const d of restore) { if (!_drafts.some(x => x.text === d.text)) { d.selected = true; _drafts.push(d); } }
    _persistDrafts(); _updateDraftBadge();
  }
}

// 轮询后台 job：网页切后台时轮询暂停、回来继续；job 在服务器跑完结果存着，不丢
function _pollJob(jobId, jobUi, restoreOnFail) {
  let tries = 0, unknownTries = 0;
  const iv = setInterval(async () => {
    tries++;
    try {
      const r = await fetch('/pdf/api/job-status?id=' + encodeURIComponent(jobId));
      const d = await r.json();
      if (d.status === 'done') {
        clearInterval(iv);
        const out = d.result || {};
        if (out.ok) {
          const parts = [];
          if (out.note_path) parts.push('笔记已建');
          if (out.anki_added) parts.push('Anki ' + out.anki_added + ' 张');
          _finishBgJob(jobUi, parts.join(' · ') || '完成', out.obsidian_url || '');
        } else { _failBgJob(jobUi, out.error || '失败', restoreOnFail); }
      } else if (d.status === 'error') {
        clearInterval(iv); _failBgJob(jobUi, d.error || '失败', restoreOnFail);
      } else if (d.status === 'unknown') {
        if (++unknownTries >= 3) { clearInterval(iv); _failBgJob(jobUi, '任务丢失(服务重启?)', restoreOnFail); }
      }
      // running → 继续轮询
    } catch (e) {
      // 网络瞬断/网页在后台 → 不立即失败，继续轮询；超 6 分钟才放弃
      if (tries > 180) { clearInterval(iv); _failBgJob(jobUi, '轮询超时', restoreOnFail); }
    }
  }, 2000);
}

async function _doCreate(makeNote, makeAnki) {
  _syncDraftsLS();
  const picked = _drafts.filter(d => d.selected);
  if (!picked.length) { alert('请先勾选 (圆圈) 要使用的段落'); return; }
  let noteName = '';
  if (makeNote) {
    noteName = prompt('请输入笔记名（不含 .md）：', '');
    if (noteName === null) return;
    noteName = (noteName || '').trim();
    if (!noteName) { alert('未输入笔记名'); return; }
  }
  // 立即关 modal + 乐观移除已选段（失败再放回），任务丢后台跑，不挡着阅读器
  const used = picked.slice();
  _drafts = _drafts.filter(x => !x.selected);
  _persistDrafts(); _updateDraftBadge(); closeDraftModal();
  const label = (makeNote && makeAnki) ? '笔记+Anki' : (makeNote ? '笔记' : 'Anki');
  const jobUi = _startBgJob('创建' + label + '中…（' + used.length + ' 段）');
  try {
    const ov = _getAiOverrides();
    // 提交到服务器后台 job（短请求，立即返回 job_id），再轮询；任务在服务器跑，网页切后台也不中断
    // @interaction learning.snippets.enqueue
    const r = await fetch('/pdf/api/snippets-to-async', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({
        file: FILE_REL || '',
        page: currentPage || 1,
        source: { kind: 'pdf', page: currentPage || 1 },
        snippets: used.map(d => ({text: d.text, source: d.source})),
        make_note: makeNote, make_anki: makeAnki,
        note_name: noteName,
        model: ov.model || '', effort: ov.effort || '',
      }),
    });
    const d = await r.json();
    if (!d.ok || !d.job_id) { _failBgJob(jobUi, d.error || '提交失败', used); return; }
    _pollJob(d.job_id, jobUi, used);
  } catch (e) {
    _failBgJob(jobUi, e.message, used);
  }
}
window.createNoteFromDrafts = () => _doCreate(true, false);
window.createAnkiFromDrafts = () => _doCreate(false, true);
window.createBothFromDrafts = () => _doCreate(true, true);

// 启动时刷新 badge
setTimeout(_updateDraftBadge, 200);
