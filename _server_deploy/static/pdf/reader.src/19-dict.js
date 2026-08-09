// ─── 字典 SSE 流式渲染：ECDICT 立刻显示，free/mw/translate 后续追加 ───
async function dictStream(word, ctx) {
  const params = new URLSearchParams({
    word, file: FILE_REL || '', page: String((typeof _selPageNum === 'function' ? _selPageNum() : currentPage) || 0), context: ctx || '',
  });
  window.dlog?.(`dictStream word="${word}" file=${FILE_REL?'Y':'N'} page=${currentPage} ctxLen=${ctx?.length||0}`);
  // 立刻 openResult 占位，避免空等
  openResult('📖 ' + word, word, '<div class="loading">⏳ 查词中…</div>');
  const _optPw = _charSel?.pw;   // 乐观下划线目标页（查词时所在页）
  const myReq = _resultReqId;    // 本次查词的请求序号；被新结果框作废后，后到的 SSE 渲染一律丢弃
  // 无论 SSE / JSON / 失败：1.8s 后无条件触发一次下划线刷新（vocab note 写盘耗时）
  setTimeout(() => {
    window.dlog?.('refreshVocabUnderlines (timer) for ' + word);
    refreshVocabUnderlinesForAllPages();
  }, 1800);
  // 3.5s 再刷一次（等 paragraph_exposure 完成）
  setTimeout(() => { refreshVocabUnderlinesForAllPages(); }, 3500);
  const contentEl = document.getElementById('result-content');
  const state = {
    word, lemma: word, forms: [],
    phon_us: '', phon_uk: '', audio_us: '', audio_uk: '',
    freq_bnc: 0, translation: '', definition: '',
    fd_defs: [], mw_defs: [], examples: new Set(), examples_zh: {},
    synonyms: [], antonyms: [],
    sources_hit: [], vocab_note: '',
  };
  const esc = s => String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;');
  const renderState = () => {
    const s = state;
    let html = '';
    const head = [];
    if (s.phon_us) head.push(`<span style="font-style:italic">US ${esc(s.phon_us)}</span>`);
    if (s.phon_uk) head.push(`<span style="font-style:italic">UK ${esc(s.phon_uk)}</span>`);
    if (s.freq_bnc) head.push(`<span style="color:#5a6680;font-size:11px">BNC #${s.freq_bnc}</span>`);
    if (s.audio_us) head.push(`<button onclick="new Audio('${esc(s.audio_us)}').play()" style="background:transparent;border:1px solid #3b6db5;color:#a8cdff;border-radius:50%;width:24px;height:24px;cursor:pointer;font-size:11px;padding:0">🔊</button>`);
    html += `<div style="display:flex;gap:8px;align-items:center;color:#a8cdff;font-size:13px">${head.join(' · ')}</div>`;
    if (s.lemma && s.lemma !== word) {
      html += `<div style="margin-top:4px;color:#7a8497;font-size:11px">原型：<code>${esc(s.lemma)}</code>${s.forms?.length?'（'+s.forms.map(esc).join('/')+'）':''}</div>`;
    }
    if (s.translation) html += `<div style="margin-top:10px;color:#cfe6ff;white-space:pre-wrap;line-height:1.6">${esc(s.translation)}</div>`;
    // MW + Free Dict 例句（合并）
    const allDefs = [];
    if (s.mw_defs.length) allDefs.push({label: '📚 MW', defs: s.mw_defs});
    if (s.fd_defs.length) allDefs.push({label: '🌐 Wiktionary', defs: s.fd_defs});
    for (const grp of allDefs) {
      html += `<div style="margin-top:12px;padding-top:8px;border-top:1px solid #2a3550;color:#8a9bb4;font-size:12px"><b style="color:#7a8497">${esc(grp.label)}</b>`;
      html += `<ul style="margin:6px 0 0 18px;padding:0;line-height:1.6">`;
      for (const d of grp.defs.slice(0, 6)) {
        html += `<li>${d.pos ? '<b>'+esc(d.pos)+'</b> ' : ''}${esc(d.en)}`;
        for (const ex of (d.examples||[]).slice(0, 2)) {
          const zh = state.examples_zh[ex];
          html += `<br><span style="color:#7a8497;font-size:11px">▸ ${esc(ex)}${zh ? '<br>　🇨🇳 ' + esc(zh) : ''}</span>`;
        }
        html += `</li>`;
      }
      html += `</ul></div>`;
    }
    // 同义反义
    if (s.synonyms.length || s.antonyms.length) {
      const meta = [];
      if (s.synonyms.length) meta.push('同 ' + s.synonyms.slice(0,5).map(esc).join(', '));
      if (s.antonyms.length) meta.push('反 ' + s.antonyms.slice(0,5).map(esc).join(', '));
      html += `<div style="margin-top:8px;color:#7a8497;font-size:11px">${meta.join(' · ')}</div>`;
    }
    contentEl.innerHTML = html;
    // 底部 actions：搬到 #vocab-actions（脱离内容滚动区，始终可见）
    const va = document.getElementById('vocab-actions');
    if (va) {
      va.className = 'show';
      va.innerHTML =
        `<button onclick="addVocabAnki('${esc(s.lemma||word)}')" style="background:#244470;border:1px solid #3b6db5;color:#fff;border-radius:6px;padding:6px 14px;cursor:pointer;font-size:12px">🎴 加入 Anki</button>` +
        `<button onclick="markVocabKnown('${esc(s.lemma||word)}', this)" style="background:#1d3a28;border:1px solid #2e7d4f;color:#9fe0b8;border-radius:6px;padding:6px 14px;cursor:pointer;font-size:12px" title="掌握度直接设为 100%，此后不再算作生词">✓ 已掌握</button>` +
        (s.sources_hit.length
          ? `<span style="color:#5a6680;font-size:10px;margin-left:auto">源：${s.sources_hit.join(' + ')}${s.vocab_note ? ' · <a href="obsidian://open?vault=obsidian&file='+encodeURIComponent(s.vocab_note)+'" style="color:#60a5fa">在 Obsidian 打开词条 →</a>' : ''}</span>`
          : `<span style="color:#5a6680;font-size:10px;margin-left:auto">⏳ 加载更多源…</span>`);
    }
  };

  let r;
  try {
    r = await fetch('/pdf/api/dict?' + params.toString(), {
      headers: {'Accept': 'text/event-stream'},
    });
  } catch (e) { return false; }
  if (!r.ok) return false;
  const ct = r.headers.get('content-type') || '';
  if (!ct.includes('event-stream')) {
    // 后端非 SSE 模式：fall back 一次性渲染（兼容旧路径，理论不会走这里）
    const d = await r.json().catch(() => ({}));
    if (!d.ok) return false;
    Object.assign(state, {
      lemma: d.lemma, forms: d.forms||[],
      phon_us: d.phonetic_us, phon_uk: d.phonetic_uk,
      audio_us: d.audio_us, audio_uk: d.audio_uk,
      freq_bnc: d.freq_bnc, translation: d.translation,
      synonyms: d.synonyms||[], antonyms: d.antonyms||[],
      sources_hit: d.sources_hit||[], vocab_note: d.vocab_note||'',
    });
    renderState();
    return true;
  }
  // SSE 模式：边读边渲染
  const reader = r.body.getReader();
  const decoder = new TextDecoder();
  let buf = '', gotEcdict = false;
  let renderQueued = false, lastRender = 0;
  const scheduleRender = () => {
    if (myReq !== _resultReqId) return;   // 已被新结果框(解释/翻译)作废 → 不再写回，防延迟结果覆盖
    const now = Date.now();
    if (now - lastRender >= 80) { renderState(); lastRender = now; return; }
    if (renderQueued) return;
    renderQueued = true;
    setTimeout(() => { renderQueued = false; renderState(); lastRender = Date.now(); }, 80 - (now - lastRender));
  };
  while (true) {
    const {value, done} = await reader.read();
    if (done) break;
    buf += decoder.decode(value, {stream: true});
    let idx;
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const block = buf.slice(0, idx);
      buf = buf.slice(idx + 2);
      let evt = 'message', data = '';
      for (const line of block.split('\n')) {
        if (line.startsWith('event:')) evt = line.slice(6).trim();
        else if (line.startsWith('data:')) data += line.slice(5).trim();
      }
      let payload = {};
      try { payload = JSON.parse(data || '{}'); } catch(_) {}
      if (evt === 'ecdict') {
        gotEcdict = true;
        Object.assign(state, {
          lemma: payload.lemma, forms: payload.forms || [],
          phon_us: payload.phonetic || '',
          freq_bnc: payload.freq_bnc || 0,
          translation: payload.translation || '',
          definition: payload.definition || '',
          sources_hit: ['ecdict'],
        });
        scheduleRender();
        _markVocabOptimistic(_optPw, payload.lemma, payload.forms || []);   // 立即标下划线，不等笔记
      } else if (evt === 'free') {
        if (payload.phon_us) state.phon_us = payload.phon_us;
        if (payload.phon_uk) state.phon_uk = payload.phon_uk;
        if (payload.audio_us && !state.audio_us) state.audio_us = payload.audio_us;
        if (payload.audio_uk && !state.audio_uk) state.audio_uk = payload.audio_uk;
        state.fd_defs = payload.definitions_en || [];
        state.synonyms = payload.synonyms || [];
        state.antonyms = payload.antonyms || [];
        if (!state.sources_hit.includes('free_dict')) state.sources_hit.push('free_dict');
        scheduleRender();
      } else if (evt === 'mw') {
        if (payload.phon_us) state.phon_us = payload.phon_us;
        if (payload.audio_us) state.audio_us = payload.audio_us;
        state.mw_defs = payload.definitions_en || [];
        if (!state.sources_hit.includes('mw')) state.sources_hit.push('mw');
        scheduleRender();
      } else if (evt === 'translate') {
        if (payload.en && payload.zh) {
          state.examples_zh[payload.en] = payload.zh;
          scheduleRender();
        }
      } else if (evt === 'done') {
        state.vocab_note = payload.vocab_note || '';
        renderState();
        // 1.5s 后刷新下划线：等后台 vocab note 写完 + paragraph_exposure 跑完
        setTimeout(() => { refreshVocabUnderlinesForAllPages(); }, 1500);
      } else if (evt === 'error') {
        if (!gotEcdict) return false;   // ECDICT 都没拿到 → 让 AI 回落
      }
    }
  }
  return gotEcdict;
}
window.dictStream = dictStream;

// 完整字典框「✓ 已掌握」按钮：mastery 直接锁 100% → POST /pdf/api/vocab-mark
window.markVocabKnown = async (lemma, btn) => {
  if (!lemma) return;
  const old = btn.textContent;
  btn.disabled = true; btn.style.opacity = '0.6'; btn.textContent = '⏳ …';
  try {
    const r = await fetch('/pdf/api/vocab-mark', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({word: lemma, mark: 'known'}),
    });
    const d = await r.json().catch(() => ({}));
    if (d.ok) {
      btn.textContent = '✓ 已掌握 100%';
      btn.style.background = '#1f5132'; btn.style.borderColor = '#3ba566'; btn.style.color = '#cdf5d9';
      btn.style.opacity = '1';
      refreshVocabUnderlinesForAllPages?.();   // 掌握后该词不再标生词下划线
      window.dlog?.('vocab-mark known ok: ' + lemma + ' → mastery 1.0');
    } else {
      btn.disabled = false; btn.style.opacity = '1'; btn.textContent = old;
      window.dlog?.('vocab-mark failed: ' + (d.error || 'unknown'));
    }
  } catch (e) {
    btn.disabled = false; btn.style.opacity = '1'; btn.textContent = old;
  }
};

// 字典 modal「🎴 加入 Anki」按钮：POST /pdf/api/vocab-anki
window.addVocabAnki = async (lemma) => {
  if (!lemma) return;
  _toast('🎴 正在加 Anki…');
  try {
    const r = await fetch('/pdf/api/vocab-anki', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({word: lemma}),
    });
    const d = await r.json();
    if (d.ok) _toast(d.action === 'created' ? '✅ Anki 卡已创建' : '✅ Anki 卡已更新');
    else _toast('❌ ' + (d.error || '失败'));
  } catch (e) {
    _toast('❌ 网络错误：' + e.message);
  }
};

// 点击已存在的高亮 → popover（预览块 + 颜色 + 备注 + 删除）
let _popoverHL = null;
function closeHlPopover() {
  _popoverHL = null;
  document.getElementById('hl-popover')?.classList.remove('open');
}
window.closeHlPopover = closeHlPopover;
function openHlPopover(h, anchorDiv, pw) {
  // 共享模式(__uiShared)→ 走 PdfAdapter.openHlEditor → RC.highlight.openEditor(编辑浮层统一)。
  //   改色/备注/删除回调复用底座 _hlUpdate/_hlDelete(取消颜色语义照搬下方 268-277);RC 不可用 → fallback 回 _openHlPopoverNative(原逻辑逐字不变)。
  if (window.__uiShared && window.PdfAdapter) {
    document.getElementById('hl-popover')?.classList.remove('open');   // 防原生小框残留
    PdfAdapter.openHlEditor({
      colors: getHlColors(), current: h.color, note: h.note || '',
      preview: h.text || '', sentence: h.sentence || '', body: h.body || '', kind: h.kind,
      anchorEl: anchorDiv, anchorSelector: '.hl-saved', placeBelow: true,
      silent: true,   // _hlUpdate 会弹「已保存」→ 抑制 rc-highlight 的重复 toast(M5)
      onColor: (c) => {
        if (!c) {                                       // 取消颜色:照搬下方 268-277 语义
          const hasNote = (h.note || '').trim() || (h.body || '').trim() || (h.sentence || '').trim();
          if (!hasNote) _hlDelete(h, pw);
          else { _hlUpdate(h, pw, { color: '' }); _toast('已取消颜色（备注保留）'); }
        } else _hlUpdate(h, pw, { color: c });
      },
      onNote: (t) => _hlUpdate(h, pw, { note: t }),
      onDelete: () => _hlDelete(h, pw),
      fallback: () => _openHlPopoverNative(h, anchorDiv, pw),
    });
    return;
  }
  return _openHlPopoverNative(h, anchorDiv, pw);
}
function _openHlPopoverNative(h, anchorDiv, pw) {
  _popoverHL = h;
  const pop = document.getElementById('hl-popover');
  window.dlog?.('openHlPopover: pop=' + (pop ? 'OK' : 'MISSING'));
  if (!pop) return;
  const colorsHtml = getHlColors().map(c =>
    `<span class="swatch${c===h.color?' cur':''}" data-c="${escHtml(c)}" style="background:${escHtml(c)}"></span>`
  ).join('');
  const kindLbl = h.kind === 'translate' ? '🌐 翻译'
                : h.kind === 'explain'   ? '💡 解释'
                : '📝 备注';
  pop.innerHTML = `
    <div class="hl-snip-wrap" data-id="${escHtml(h.id)}">
      <div class="hl-snip">
        <div class="hl-snip-content">
          ${h.text ? `<div class="hl-snip-row text"><span class="row-lbl">📌 选中</span>${escHtml(h.text)}</div>` : ''}
          ${h.sentence ? `<div class="hl-snip-row sentence"><span class="row-lbl">📖 所在句</span>${escHtml(h.sentence)}</div>` : ''}
          ${h.body ? `<div class="hl-snip-row body"><span class="row-lbl">${kindLbl}</span>${escHtml(h.body)}</div>` : ''}
          ${(!h.text && !h.sentence && !h.body) ? `<div class="hl-snip-row text" style="color:#7a8497">（无文字内容）</div>` : ''}
        </div>
        <div class="hl-snip-circle" title="按住左滑显示删除"></div>
      </div>
      <button class="hl-snip-del-row" type="button" title="删除高亮">🗑</button>
    </div>
    <div class="row"><span class="row-lbl">🎨 颜色</span>${colorsHtml}</div>
    <textarea id="hl-note" placeholder="自定义备注（可空）">${escHtml(h.note || '')}</textarea>
    <div class="actions">
      <button data-act="save" class="primary">保存</button>
    </div>
  `;
  // 预览块：点击展开 / 收起；右侧圆圈左滑（或整体触屏左滑）→ 下方滑出删除栏
  const wrapEl = pop.querySelector('.hl-snip-wrap');
  if (wrapEl) _attachSnipBehavior(wrapEl, () => _hlDelete(h, pw));
  // 颜色 swatch：点 = 立即切换颜色（PATCH 后端 + 重渲染）
  //   - 点已是 .cur 的色 → 取消高亮颜色
  //       · 没备注（note/body/sentence 都空）→ 直接删除该高亮
  //       · 有备注 → 保留高亮（color 设空）+ 视觉变"仅备注"虚框样式
  //   - 点别的色 → 立即切到该色
  pop.querySelectorAll('.row .swatch').forEach(sw => {
    sw.onclick = async (e) => {
      e.stopPropagation();
      if (sw.classList.contains('cur')) {
        const hasNote = (h.note || '').trim() || (h.body || '').trim() || (h.sentence || '').trim();
        if (!hasNote) {
          await _hlDelete(h, pw);
        } else {
          await _hlUpdate(h, pw, {color: ''});
          closeHlPopover();
          _toast('已取消颜色（备注保留）');
        }
        return;
      }
      // 切换到新色：立即 PATCH，不用等 [保存]
      const newColor = sw.dataset.c;
      pop.querySelectorAll('.row .swatch').forEach(s => s.classList.remove('cur'));
      sw.classList.add('cur');
      await _hlUpdate(h, pw, {color: newColor});
    };
  });
  // [保存] 只更新 note 文字（颜色由色板点击立即生效）
  pop.querySelector('[data-act=save]').onclick = async (e) => {
    e.stopPropagation();
    await _hlUpdate(h, pw, { note: document.getElementById('hl-note').value });
    closeHlPopover();
  };
  // 删除入口现在统一在 .hl-snip-del-row（左滑揭示）；底部不再有 [data-act=del] 按钮
  // 定位：高亮元素下方（贴齐左边，跟 main 滚动）
  const r = anchorDiv.getBoundingClientRect();
  pop.style.left = (r.left + window.scrollX) + 'px';
  pop.style.top  = (r.bottom + window.scrollY + 6) + 'px';
  pop.classList.add('open');
  // 防止 popover 跑出视口右侧
  requestAnimationFrame(() => {
    const pr = pop.getBoundingClientRect();
    if (pr.right > _visRight() - 8) {
      pop.style.left = Math.max(8, _visRight() - pr.width - 8) + 'px';
    }
  });
}

async function _hlUpdate(h, pw, patch) {
  try {
    const r = await fetch('/pdf/api/highlights', {
      method: 'PATCH', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({file: FILE_REL, id: h.id, ...patch}),
    });
    const d = await r.json();
    if (!d.ok) { alert('保存失败：' + (d.error || '?')); return; }
    Object.assign(h, d.highlight);
    renderHighlightsOnPage(pw, h.page);
    _toast('已保存');
  } catch (e) { alert('保存异常：' + e.message); }
}
async function _hlDelete(h, pw) {
  try {
    const r = await fetch('/pdf/api/highlights', {
      method: 'DELETE', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({file: FILE_REL, id: h.id}),
    });
    const d = await r.json();
    if (!d.ok) { alert('删除失败：' + (d.error || '?')); return; }
    _allHighlights = _allHighlights.filter(x => x.id !== h.id);
    _hlByPage[h.page] = (_hlByPage[h.page] || []).filter(x => x.id !== h.id);
    renderHighlightsOnPage(pw, h.page);
    closeHlPopover();
    _toast('已删除');
  } catch (e) { alert('删除异常：' + e.message); }
}

// 预览块的交互：
//   - 单击文字内容 → 展开/收起全文
//   - 右侧圆圈左滑（或整体触屏左滑） → 下方滑出删除栏（.swiped）
//   - 再点圆圈 / 任意位置（除按钮）→ 复位
function _attachSnipBehavior(wrap, onDel) {
  const snip = wrap.querySelector('.hl-snip');
  const content = wrap.querySelector('.hl-snip-content');
  const circle = wrap.querySelector('.hl-snip-circle');
  const del = wrap.querySelector('.hl-snip-del-row');
  if (!snip || !del) return;
  del.onclick = (e) => { e.stopPropagation(); onDel(); };

  const reveal = () => { wrap.classList.add('swiped'); snip.style.transform = ''; };
  const reset  = () => { wrap.classList.remove('swiped'); snip.style.transform = ''; };

  // 触屏 swipe（整体 snip）
  let sx = 0, sy = 0, dx = 0, dy = 0, swiping = false, axis = '';
  const onTouchStart = (e) => {
    const t = e.touches[0]; sx = t.clientX; sy = t.clientY; dx = dy = 0; swiping = true; axis = '';
  };
  const onTouchMove = (e) => {
    if (!swiping) return;
    const t = e.touches[0];
    dx = t.clientX - sx; dy = t.clientY - sy;
    if (!axis) {
      if (Math.abs(dx) > 6 || Math.abs(dy) > 6) axis = Math.abs(dx) > Math.abs(dy) ? 'x' : 'y';
    }
    if (axis === 'x') {
      if (dx < 0) snip.style.transform = `translateX(${Math.max(dx,-80)}px)`;
      else if (wrap.classList.contains('swiped')) snip.style.transform = `translateX(${Math.min(dx,30)}px)`;
    }
  };
  const onTouchEnd = () => {
    swiping = false;
    if (axis === 'x') {
      if (dx < -40) reveal();
      else if (dx > 30 || !wrap.classList.contains('swiped')) reset();
      else reveal();   // 已 swipe 态、小位移 → 维持
    }
    dx = dy = 0; axis = '';
  };
  snip.addEventListener('touchstart', onTouchStart, {passive:true});
  snip.addEventListener('touchmove',  onTouchMove,  {passive:true});
  snip.addEventListener('touchend',   onTouchEnd);

  // 鼠标 swipe（按住圆圈拖动；其他位置点击 = 展开/收起）
  if (circle) {
    let md = false, mx = 0, mdx = 0;
    circle.addEventListener('mousedown', (e) => {
      md = true; mx = e.clientX; mdx = 0; e.preventDefault();
    });
    document.addEventListener('mousemove', (e) => {
      if (!md) return;
      mdx = e.clientX - mx;
      if (mdx < 0) snip.style.transform = `translateX(${Math.max(mdx,-80)}px)`;
      else if (wrap.classList.contains('swiped')) snip.style.transform = `translateX(${Math.min(mdx,30)}px)`;
    });
    document.addEventListener('mouseup', () => {
      if (!md) return;
      md = false;
      if (mdx < -40) reveal();
      else if (mdx > 30 || !wrap.classList.contains('swiped')) reset();
      mdx = 0;
    });
    // 圆圈单击（无 drag）= 切换 swiped
    circle.addEventListener('click', (e) => {
      e.stopPropagation();
      if (wrap.classList.contains('swiped')) reset(); else reveal();
    });
  }

  // 单击 content → 展开/收起全文
  if (content) {
    content.addEventListener('click', (e) => {
      if (e.target.closest('button')) return;
      // swipe 态时，点击不展开，先复位
      if (wrap.classList.contains('swiped')) { reset(); return; }
      content.classList.toggle('expanded');
    });
  }
}

// 设置 modal 里的颜色管理
function renderHlColorSetting() {
  const c = document.getElementById('set-hl-colors');
  if (!c) return;
  c.innerHTML = '';
  for (const col of getHlColors()) {
    const w = document.createElement('div');
    w.className = 'swatch-w';
    const sw = document.createElement('span');
    sw.className = 'swatch';
    sw.style.background = col;
    w.appendChild(sw);
    const del = document.createElement('button');
    del.className = 'del';
    del.textContent = '×';
    del.title = '删除';
    del.onclick = () => {
      const cur = getHlColors().filter(x => x !== col);
      saveHlColors(cur.length ? cur : DEFAULT_HL_COLORS);
      renderHlPicker();
      renderHlColorSetting();
    };
    w.appendChild(del);
    c.appendChild(w);
  }
}
window.addHlColor = () => {
  const v = (document.getElementById('set-hl-new')?.value || '').trim();
  if (!/^#[0-9a-fA-F]{3,8}$/.test(v)) { alert('颜色格式应为 #rrggbb'); return; }
  const arr = getHlColors();
  if (arr.includes(v)) return;
  arr.push(v);
  saveHlColors(arr);
  renderHlPicker();
  renderHlColorSetting();
};
window.resetHlColors = () => {
  saveHlColors(DEFAULT_HL_COLORS);
  renderHlPicker();
  renderHlColorSetting();
};

// 全局：点 popover 外 / hl-saved 外 → 关 popover
document.addEventListener('click', (e) => {
  if (!_popoverHL) return;
  if (e.target.closest?.('#hl-popover')) return;
  if (e.target.closest?.('.hl-saved')) return;
  closeHlPopover();
}, true);

// 缩放/重渲染后重新贴高亮（zoom 变了 css px 也变了）
const _origRenderPageInto = null;  // 仅占位
// 在 _renderPageInto 里 loadCharsAndBindLayer 内部已经调用了 renderHighlightsOnPage

// ── 日语词「完整字典」大页面(离线富内容 + 按需 AI 深入)──────────────────
let _jpKanjiData = [];   // 当前词的汉字拆解,供 chip 点击展开
let _jpPollTimer = null;   // 例句/汉字字义中译后台轮询替换的计时器
// 轮询 /api/dict-jp-zh,拿后台翻好的例句/汉字字义中文，原地替换英文（跟英文单词一致，不增加等待）
function _jpPollZh(word) {
  clearInterval(_jpPollTimer);
  let tries = 0;
  _jpPollTimer = setInterval(async () => {
    tries++;
    let d = null;
    try { d = await (await fetch('/pdf/api/dict-jp-zh?word=' + encodeURIComponent(word))).json(); }
    catch (_) { d = null; }
    if (!d || !d.ok) { if (tries >= 10) clearInterval(_jpPollTimer); return; }
    let pending = false;
    (d.examples || []).forEach((e, i) => {
      if (e.zh) {
        const el = document.querySelector('.jp-ex-zh[data-exi="' + i + '"]:not([data-zhdone])');
        if (el) { el.textContent = e.zh; el.dataset.zhdone = '1'; }
      } else pending = true;
    });
    (d.kanji || []).forEach((k, i) => {
      if (k.meanings_zh) {
        if (_jpKanjiData[i] && !_jpKanjiData[i].meanings_zh) {
          _jpKanjiData[i].meanings_zh = k.meanings_zh;
          const chip = document.querySelectorAll('.jp-kanji-chip')[i];
          if (chip && chip.classList.contains('active')) _jpKanjiTap(i);   // 详情正打开 → 刷新
        }
      } else pending = true;
    });
    if (!pending || tries >= 10) clearInterval(_jpPollTimer);
  }, 1500);
}
async function dictStreamJP(word, ctx) {
  clearInterval(_jpPollTimer);   // 取消上一个词的中译轮询，避免串到当前词
  openResult('📖 ' + word, word, '<div class="loading">⏳ 查词中…</div>');
  const myReq = _resultReqId;
  const contentEl = document.getElementById('result-content');
  let d;
  try {
    // @interaction dictionary.jp.read
    const r = await fetch('/pdf/api/dict-jp?word=' + encodeURIComponent(word) +
      '&context=' + encodeURIComponent(ctx || '') +
      '&file=' + encodeURIComponent(FILE_REL || '') +
      '&page=' + encodeURIComponent((typeof _selPageNum === 'function' ? _selPageNum() : currentPage) || 0) +
      '&langs=' + encodeURIComponent((BOOK_LANGS || []).join(',')));
    d = await r.json();
  } catch (e) {
    if (myReq === _resultReqId) contentEl.innerHTML = '<div style="color:#c00;padding:14px">查词失败：' + e.message + '</div>';
    return false;
  }
  if (myReq !== _resultReqId) return false;
  if (!d.ok) return dictStream(word, ctx);   // 也许其实是英文词 → 回退三源框
  const esc = s => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
  const wq = esc(word).replace(/'/g, "\\'");
  const rq = esc(d.reading || word).replace(/'/g, "\\'");   // 发音念假名读音
  const phon = (d.reading && d.accent != null) ? _renderPitch(d.reading, d.accent)
    : (d.reading ? '<span class="wp-phon">' + esc(d.reading) + '</span>' : '');
  let html = '<div class="jp-head">' + phon +
    (d.romaji ? '<span class="jp-romaji">' + esc(d.romaji) + '</span>' : '') +
    (d.pos ? '<span class="jp-pos">' + esc(d.pos) + '</span>' : '') + '</div>';
  if (d.zh) html += '<div class="jp-zh">' + esc(d.zh) + '</div>';
  html += _jpInflectHtml(d.inflect, word);   // 变形分析:原形 + 语法标签
  _jpKanjiData = d.kanji || [];
  if (_jpKanjiData.length) {
    html += '<div class="jp-sec-label">汉字（点字看音读/训读）</div><div class="jp-kanji-row">' +
      _jpKanjiData.map((k, i) => '<button class="jp-kanji-chip" onclick="_jpKanjiTap(' + i + ')">' + esc(k.kanji) + '</button>').join('') +
      '</div><div id="jp-kanji-detail" class="jp-kanji-detail"></div>';
  }
  if ((d.examples || []).length) {
    html += '<div class="jp-sec-label">母语例句</div><div class="jp-ex">';
    d.examples.forEach((e, ei) => {
      html += '<div class="jp-ex-ja">' + esc(e.ja) + '</div>' +
              '<div class="jp-ex-zh" data-exi="' + ei + '"' + (e.zh ? ' data-zhdone="1"' : '') + '>' +
              esc(e.zh || e.en || '') + '</div>';   // 没中译先回退英文，后台翻好由 _jpPollZh 替换
    });
    html += '</div>';
  }
  html += '<button id="jp-ai-btn" class="jp-ai-btn" onclick="_jpAiDeep(\'' + wq + '\')">✨ AI 深入讲解（用法 / 语感 / 近义辨析）</button>' +
          '<div id="jp-ai-out" class="jp-ai-out"></div>';
  contentEl.innerHTML = html;
  contentEl.scrollTop = 0;
  if (_jpKanjiData.length) _jpKanjiTap(0);   // 默认展开第一个汉字
  // 有未翻的例句/汉字字义 → 后台翻 + 轮询替换英文（不增加等待）
  if ((d.examples || []).some(e => !e.zh) || _jpKanjiData.some(k => !k.meanings_zh)) _jpPollZh(word);
  const va = document.getElementById('vocab-actions');
  if (va) {
    va.className = 'show';
    const bs = 'border-radius:6px;padding:6px 14px;cursor:pointer;font-size:12px';
    va.innerHTML =
      '<button onclick="_ttsWord(\'' + rq + '\', \'ja-JP\')" style="background:transparent;border:1px solid #3b6db5;color:#a8cdff;' + bs + '">🔊 朗读</button>' +
      '<button onclick="addVocabAnki(\'' + wq + '\')" style="background:#244470;border:1px solid #3b6db5;color:#fff;' + bs + '">🎴 加入 Anki</button>' +
      '<button onclick="markVocabKnown(\'' + wq + '\', this)" style="background:#1d3a28;border:1px solid #2e7d4f;color:#9fe0b8;' + bs + '" title="掌握度设为100%">✓ 已掌握</button>';
  }
  return true;
}
// 共享模式(__uiShared)下 rc-wordpop.js 自己的日语汉字拆解 chip 已改用 addEventListener+本模块闭包(H1修复,不依赖全局);
//   本文件无条件赋值会覆盖 rc-wordpop.js 留的兼容导出,且读的是本文件自己的 _jpKanjiData(共享模式下从未被填,恒空数组)。
//   门控:共享模式下别赋值;legacy 模式(!__uiShared)保留原生行为不变。
if (!window.__uiShared) {
window._jpKanjiTap = (i) => {
  const k = _jpKanjiData[i]; if (!k) return;
  document.querySelectorAll('.jp-kanji-chip').forEach((c, j) => c.classList.toggle('active', j === i));
  const det = document.getElementById('jp-kanji-detail'); if (!det) return;
  const esc = s => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;');
  let h = '<div class="jk-lit">' + esc(k.kanji) + '</div><div class="jk-body">';
  if ((k.on || []).length) h += '<div><span class="jk-tag jk-on">音</span>' + k.on.map(esc).join('、') + '</div>';
  if ((k.kun || []).length) h += '<div><span class="jk-tag jk-kun">訓</span>' + k.kun.map(esc).join('、') + '</div>';
  // 字义优先显示中文(meanings_zh,后端 Google 翻译)，缺失才回退英文
  const _meanEn = (k.meanings || []).map(esc).join('; ');
  if (k.meanings_zh || _meanEn) h += '<div class="jk-mean">' + (k.meanings_zh ? esc(k.meanings_zh) : _meanEn) + '</div>';
  h += '</div>';
  det.innerHTML = h;
};
}
if (!window.__uiShared) {
window._jpAiDeep = async (word) => {
  const btn = document.getElementById('jp-ai-btn');
  const out = document.getElementById('jp-ai-out');
  if (!out) return;
  const myReq = _resultReqId;
  if (btn) { btn.disabled = true; btn.textContent = '✨ 生成中…'; }
  const ctx = (_wordPopState && _wordPopState.ctx) || '';
  try {
    const render = (text) => {
      if (myReq !== _resultReqId) return;   // 结果框已被新内容作废
      out.innerHTML = md(text || ' ');
      if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([out]).catch(() => {});
      out.scrollIntoView && out.scrollIntoView({block: 'nearest'});
    };
    const res = await _aiStream('/pdf/api/dict-jp-ai?word=' + encodeURIComponent(word) +
      '&context=' + encodeURIComponent(ctx), { method: 'GET', onText: render });
    if (myReq !== _resultReqId) return;
    if (res.ok) render(res.text);
    else out.innerHTML = '<span style="color:#c00">AI 失败：' + (res.error || '') + '</span>';
  } catch (e) {
    out.innerHTML = '<span style="color:#c00">AI 失败：' + e.message + '</span>';
  }
  if (btn) btn.style.display = 'none';
};
}

// 结果 modal
