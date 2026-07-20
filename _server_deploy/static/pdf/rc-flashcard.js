/* rc-flashcard.js — 融合复习卡(用户设计定稿 2026-07-21,references/card-review-integration.md)。
 * 状态机:draft 草稿[🗑删除][✓入库到Anki](入库=直接进Anki拿note_id)→ learn 普通Anki(正面→点击显答案→四档
 *   再来/困难/良好/简单→answerCards真FSRS)→ collapsed 收起长条+距下次复习倒计时。
 * 多卡(B2 用户拍板):**CSS scroll-snap 左右滑动、中线吸附**(替代 ‹› 前后按钮);底部圆点指示当前张。
 *   字幕浮层与侧边栏共用同一渲染(bare 模式套进 vc-card 天气卡壳)。操作后只就地重渲当前 slide→滑动位置不跳。 */
(function () {
  'use strict';
  var RC = (window.RC = window.RC || {});
  if (RC.flashcard) return;
  function esc(x) { return RC.esc ? RC.esc(x) : String(x == null ? '' : x); }
  function md(x) { try { return RC.md ? RC.md(String(x || '')) : esc(x); } catch (e) { return esc(x); } }
  function clozeSeg(t, showAns) { return md(String(t || '').replace(/\{\{c\d+::(.*?)(::[^}]*)?\}\}/g, showAns ? '<b>$1</b>' : '<b>[…]</b>')); }
  var _EASE = [['1', '再来', 'e1'], ['2', '困难', 'e2'], ['3', '良好', 'e3'], ['4', '简单', 'e4']];
  var _groups = {};   // gid → {cards:共享卡对象数组, conts:[渲染实例容器]}:同 gid 多宿主(侧栏/浮层)状态联动
  function injectCss() {
    if (document.getElementById('rc-flashcard-css')) return;
    var st = document.createElement('style'); st.id = 'rc-flashcard-css';
    st.textContent =
      '.fc-wrap{margin-top:8px;position:relative}' +
      '.fc-pin{position:absolute;top:-1px;right:2px;z-index:2;background:none;border:none;color:#8a9bb4;cursor:pointer;opacity:.7;padding:3px 5px;-webkit-tap-highlight-color:transparent}' +
      '.fc-pin svg{width:13px;height:13px;display:block}' +
      '.fc-pin:hover{opacity:1;transform:scale(1.1)}' +
      '.fc-track{display:flex;overflow-x:auto;scroll-snap-type:x mandatory;-webkit-overflow-scrolling:touch;scrollbar-width:none;overscroll-behavior-x:contain}' +
      '.fc-track::-webkit-scrollbar{display:none}' +
      '.fc-slide{flex:0 0 100%;scroll-snap-align:center;box-sizing:border-box;min-width:0}' +
      '.fc-dots{display:flex;justify-content:center;flex-wrap:wrap;gap:7px;margin-top:10px;padding:2px 4px}' +
      '.fc-dot{width:8px;height:8px;border-radius:50%;background:#3a4560;cursor:pointer;flex:none;transition:background .15s,transform .15s}' +
      '.fc-dot.on{background:#7dd3fc;transform:scale(1.25)}' +
      '.fc-bare .fc-card{background:transparent;border:none;padding:2px 0}.fc-bare .fc-wrap{margin-top:0}' +
      '.fc-slbl{font-size:11px;color:#8a9bb4;margin-bottom:6px}' +
      '.fc-card{background:#0d1322;border:1px solid #1f2740;border-radius:10px;padding:14px;font-size:15px;line-height:1.7;color:#e6e6f0;max-height:min(46vh,300px);overflow-y:auto;-webkit-overflow-scrolling:touch;overscroll-behavior:contain}' +
      '.fc-lbl{font-size:10px;color:#7a8497;margin-bottom:3px}' +
      '.fc-ed{width:100%;box-sizing:border-box;background:#10182c;border:1px solid #2a3550;border-radius:8px;color:#e6e6f0;font:inherit;font-size:14px;line-height:1.6;padding:10px 12px;min-height:72px;resize:vertical;margin-bottom:8px}' +
      '.fc-btns{display:flex;gap:7px;margin-top:10px}' +
      '.fc-btns button{flex:1;border-radius:9px;padding:10px 0;font-size:13px;cursor:pointer;border:1px solid #2a3550;background:#1a2540;color:#cfe6ff;-webkit-tap-highlight-color:transparent}' +
      '.fc-del{border-color:#7f1d1d!important;color:#fca5a5!important;flex:0 0 42%!important}' +
      '.fc-add{border-color:#14532d!important;color:#86efac!important}' +
      '.fc-face{cursor:pointer}.fc-face .fc-hint{font-size:11px;color:#5a6680;margin-top:8px}' +
      '.fc-back{border-top:1px dashed #2a3550;margin-top:10px;padding-top:10px}' +
      '.fc-eases{display:flex;gap:6px;margin-top:12px}' +
      '.fc-e{flex:1;border-radius:9px;padding:9px 0;font-size:13px;cursor:pointer;border:1px solid #2a3550;background:#1a2540;color:#cfe6ff;-webkit-tap-highlight-color:transparent}' +
      '.fc-e.e1{border-color:#7f1d1d;color:#fca5a5}.fc-e.e2{border-color:#78350f;color:#fcd34d}' +
      '.fc-e.e3{border-color:#14532d;color:#86efac}.fc-e.e4{border-color:#1e3a8a;color:#93c5fd}' +
      '.fc-collapsed{display:flex;align-items:center;gap:10px;background:#0d1322;border:1px solid #1f2740;border-radius:10px;padding:12px 14px;cursor:pointer;font-size:13px;color:#8a9bb4}' +
      '.fc-collapsed b{color:#86efac}' +
      '.fc-donehd{display:flex;align-items:center;gap:8px;font-size:12px;color:#86efac;padding-bottom:9px;margin-bottom:10px;border-bottom:1px dashed #2a3550}' +
      '.fc-donehd b{color:#bbf7d0}';
    document.head.appendChild(st);
  }
  function nextLabel(next) {
    next = next || {}; var iv = next.interval;
    if (typeof iv === 'number' && iv > 0) return iv >= 1 ? (iv + ' 天后') : (Math.max(1, Math.round(iv * 24 * 60)) + ' 分钟后');
    return '很快';
  }
  function cardHtml(st, c, i) {
    // 顶部状态提示行已去掉(用户:下方圆点足够指示);卡片框固定大小、内容超出内部滚动(.fc-card max-height)
    if (c._st === 'done') {
      var df = c.type === 'cloze' ? clozeSeg(c.cloze, false) : md(c.front);
      var db = c.type === 'cloze' ? clozeSeg(c.cloze, true) : md(c.back);
      return '<div class="fc-card"><div class="fc-donehd">✓ 已复习 · 距下次复习 <b>' + esc(nextLabel(c._next)) + '</b></div><div class="fc-lbl">正面</div>' + df + '<div class="fc-back"><div class="fc-lbl">背面</div>' + db + '</div></div>';
    }
    if (c._st === 'draft') {
      var b = c.type === 'cloze'
        ? '<div class="fc-lbl">填空(cloze,答案用 {{c1::…}} 包住)</div><textarea class="fc-ed" data-f="cloze">' + esc(c.cloze) + '</textarea>'
        : '<div class="fc-lbl">正面</div><textarea class="fc-ed" data-f="front">' + esc(c.front) + '</textarea><div class="fc-lbl">背面</div><textarea class="fc-ed" data-f="back">' + esc(c.back) + '</textarea>';
      b += '<div class="fc-btns"><button class="fc-del" data-fc="del">🗑 删除</button><button class="fc-add" data-fc="add">✓ 入库到 Anki</button></div>';
      return '<div class="fc-card">' + b + '</div>';
    }
    if (c._st === 'preview') {
      var pf = c.type === 'cloze' ? clozeSeg(c.cloze, false) : md(c.front);
      var pb = c.type === 'cloze' ? clozeSeg(c.cloze, true) : md(c.back);
      return '<div class="fc-card"><div class="fc-lbl">正面</div>' + pf + '<div class="fc-back"><div class="fc-lbl">背面</div>' + pb + '</div></div>';
    }
    var front = c.type === 'cloze' ? clozeSeg(c.cloze, false) : md(c.front);
    var back = c.type === 'cloze' ? clozeSeg(c.cloze, true) : md(c.back);
    var body = !c._showBack
      ? '<div class="fc-face" data-fc="reveal">' + front + '<div class="fc-hint">点击显示答案 ▾</div></div>'
      : '<div class="fc-face">' + front + '<div class="fc-back">' + back + '</div></div><div class="fc-eases">' + _EASE.map(function (e) { return '<button class="fc-e ' + e[2] + '" data-ease="' + e[0] + '">' + e[1] + '</button>'; }).join('') + '</div>';
    return '<div class="fc-card">' + body + '</div>';
  }
  function bindSlide(container, slide, st, i) {
    slide.querySelectorAll('[data-fc]').forEach(function (el) {
      el.addEventListener('click', function (ev) {
        ev.stopPropagation(); var act = el.dataset.fc, cc = st.cards[i];
        if (act === 'del') { st.cards.splice(i, 1); renderTrack(container); broadcast(st.gid, null, container); RC.toast && RC.toast('草稿已删除(未入库)'); }
        else if (act === 'add') { addToAnki(container, i); }
        else if (act === 'reveal') { cc._showBack = true; updateSlide(container, i); broadcast(st.gid, i, container); }
      });
    });
    slide.querySelectorAll('.fc-ed').forEach(function (ta) { ta.addEventListener('input', function () { st.cards[i][ta.dataset.f] = ta.value; broadcast(st.gid, i, container); }); });
    slide.querySelectorAll('.fc-e').forEach(function (el) { el.addEventListener('click', function (ev) { ev.stopPropagation(); rate(container, i, parseInt(el.dataset.ease, 10)); }); });
  }
  function updateSlide(container, i) {
    var st = container.__fc; if (!st) return;
    var slide = container.querySelector('.fc-slide[data-i="' + i + '"]'); if (!slide) return;
    slide.innerHTML = cardHtml(st, st.cards[i], i); bindSlide(container, slide, st, i);
    try { RC.typeset && RC.typeset(slide); } catch (e) {}
  }
  function renderDots(container) {
    var st = container.__fc, box = container.querySelector('.fc-dots'); if (!box) return;
    box.querySelectorAll('.fc-dot').forEach(function (d, i) { d.classList.toggle('on', i === st.idx); });
  }
  function renderTrack(container) {
    var st = container.__fc; if (!st) return;
    var n = st.cards.length;
    if (!n) { container.innerHTML = '<div class="fc-collapsed">（草稿已全部删除）</div>'; return; }
    if (st.idx >= n) st.idx = n - 1;
    var slides = '';
    for (var i = 0; i < n; i++) slides += '<div class="fc-slide" data-i="' + i + '">' + cardHtml(st, st.cards[i], i) + '</div>';
    var dots = n > 1 ? '<div class="fc-dots">' + st.cards.map(function (_, j) { return '<span class="fc-dot' + (j === st.idx ? ' on' : '') + '" data-goto="' + j + '"></span>'; }).join('') + '</div>' : '';
    var pin = (window.RC && RC.stickynote && RC.stickynote.createCardAt && !(st.opts && st.opts.nopin)) ? '<button class="fc-pin" title="钉到书页"><svg viewBox="0 0 16 16" width="11" height="11" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M9.9 2.1l4 4-2.9 1-1.5 4.4-5-5L8.9 5z"/><path d="M6 10L2.5 13.5"/></svg></button>' : '';
    container.innerHTML = '<div class="fc-wrap">' + pin + '<div class="fc-track">' + slides + '</div>' + dots + '</div>';
    var _pinBtn = container.querySelector('.fc-pin');
    if (_pinBtn) _pinBtn.addEventListener('click', function (ev) { ev.stopPropagation(); pinToPage(container); });
    var track = container.querySelector('.fc-track');
    container.querySelectorAll('.fc-slide').forEach(function (sl, j) { bindSlide(container, sl, st, j); });
    try { RC.typeset && RC.typeset(container); } catch (e) {}
    container.querySelectorAll('.fc-dot').forEach(function (d) {
      d.addEventListener('click', function (ev) {
        ev.stopPropagation(); var gi = parseInt(d.dataset.goto, 10);
        var sl = container.querySelector('.fc-slide[data-i="' + gi + '"]');
        if (sl) { try { sl.scrollIntoView({ inline: 'center', behavior: 'smooth', block: 'nearest' }); } catch (e) { if (track) track.scrollLeft = sl.offsetLeft; } }
        st.idx = gi; renderDots(container);
      });
    });
    var _t = null;
    track.addEventListener('scroll', function () {
      clearTimeout(_t);
      _t = setTimeout(function () {
        var mid = track.scrollLeft + track.clientWidth / 2, best = 0, bd = 1e9;
        container.querySelectorAll('.fc-slide').forEach(function (sl, j) { var cx = sl.offsetLeft + sl.offsetWidth / 2, dd = Math.abs(cx - mid); if (dd < bd) { bd = dd; best = j; } });
        if (best !== st.idx) { st.idx = best; renderDots(container); }
      }, 90);
    }, { passive: true });
  }
  function register(container, opts) {
    var gid = opts && opts.gid; if (!gid) return;
    container.__fc.gid = gid;
    var g = _groups[gid] || (_groups[gid] = { cards: null, conts: [] });
    g.conts = g.conts.filter(function (c) { return c && c.isConnected && c.__fc; });
    if (g.cards) container.__fc.cards = g.cards;   // 同 gid 已有 → 复用共享卡对象(编辑/入库/评分天然同步)
    else g.cards = container.__fc.cards;           // 首个实例:登记为共享源
    g.conts.push(container);
  }
  function broadcast(gid, i, except) {
    if (!gid) return; var g = _groups[gid]; if (!g) return;
    g.conts = g.conts.filter(function (c) { return c && c.isConnected && c.__fc; });
    g.conts.forEach(function (c) { if (c === except) return; if (typeof i === 'number') updateSlide(c, i); else renderTrack(c); });
  }
  function mountState(container, cards, opts) {
    // 保留每张卡的 _st/_nid/_next 的 mount(便签/收藏夹宿主用:钉出去的卡保持草稿/学习/已复习态)
    if (!container || !cards || !cards.length) return;
    injectCss();
    if (opts && opts.bare) container.classList.add('fc-bare');
    container.__fc = { cards: cards.map(function (c) { return { type: (c.type || 'basic'), front: c.front || '', back: c.back || '', cloze: c.cloze || c.text || '', _st: c._st || 'draft', _showBack: !!c._showBack, _nid: (c._nid != null ? c._nid : null), _next: c._next || null }; }), idx: 0, opts: opts || {}, readonly: false };
    register(container, opts);
    renderTrack(container);
  }
  function pinToPage(container) {
    // 钉到书页:把当前卡组快照(含状态)交给 rc-stickynote 建 card 便签;同 gid → 便签卡与原卡联动。拖出手势(真机)复用同一 createCardAt
    var st = container.__fc; if (!st || !(window.RC && RC.stickynote && RC.stickynote.createCardAt)) return;
    var snap = st.cards.map(function (c) { return { type: c.type, front: c.front, back: c.back, cloze: c.cloze, _st: c._st, _showBack: c._showBack, _nid: c._nid, _next: c._next }; });
    var cx = (window.innerWidth || 1024) / 2, cy = (window.innerHeight || 768) / 2;
    RC.stickynote.createCardAt(cx, cy, snap, st.gid);
    RC.toast && RC.toast('📌 已钉到书页');
  }
  function mountDrafts(container, cards, opts) {
    if (!container || !cards || !cards.length) return;
    injectCss();
    if (opts && opts.bare) container.classList.add('fc-bare');
    container.__fc = { cards: cards.map(function (c) { return { type: (c.type || 'basic'), front: c.front || '', back: c.back || '', cloze: c.cloze || c.text || '', _st: 'draft', _showBack: false, _nid: null, _next: null }; }), idx: 0, opts: opts || {}, readonly: false };
    register(container, opts);
    renderTrack(container);
  }
  function mountPreview(container, cards, opts) {
    if (!container || !cards || !cards.length) return;
    injectCss();
    if (opts && opts.bare) container.classList.add('fc-bare');
    container.__fc = { cards: cards.map(function (c) { return { type: (c.type || 'basic'), front: c.front || '', back: c.back || '', cloze: c.cloze || c.text || '', _st: 'preview' }; }), idx: 0, opts: opts || {}, readonly: true };
    register(container, opts);
    renderTrack(container);
  }
  function addToAnki(container, i) {
    var st = container.__fc, c = st.cards[i];
    var aid = 'fc_' + Array.from(crypto.getRandomValues(new Uint8Array(8))).map(function (b) { return b.toString(16).padStart(2, '0'); }).join('');
    var payload = { aid: aid, cards: [{ type: c.type, front: c.front, back: c.back, cloze: c.cloze }] };
    c._st = 'learn'; c._showBack = false; updateSlide(container, i); broadcast(st.gid, i, container);
    fetch('/pdf/api/anki-add-cards', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || d.ok === false) { c._st = 'draft'; updateSlide(container, i); broadcast(st.gid, i, container); RC.toast && RC.toast('入库失败:' + ((d && d.error) || '?')); }
        else { c._nid = (d.note_ids || [])[0]; broadcast(st.gid, i, container); _stateSync(st, i); RC.toast && RC.toast('✓ 已入 Anki,可直接复习这张'); }
      })
      .catch(function (e) {
        if (RC.outbox && e && e.name === 'TypeError') { RC.outbox.send('fcadd', aid, '/pdf/api/anki-add-cards', payload); RC.toast && RC.toast('离线:入库已入队,恢复后自动同步'); }
        else { c._st = 'draft'; updateSlide(container, i); RC.toast && RC.toast('入库失败'); }
      });
  }
  function _stateSync(st, i) {
    // 统一编号协议(用户设计 2026-07-21):gid 是全局卡编号(card_)时把卡状态回写服务端注册表——
    //   刷新/#id 引用/其它宿主 mountState 时还原,"一张卡两种状态"跨会话也消失。fire-and-forget+outbox 兜。
    if (!st || !/^card_/.test(st.gid || '')) return;
    var c = st.cards[i]; if (!c) return;
    var body = { idx: i, state: { _st: c._st, _nid: c._nid, _next: c._next } };
    fetch('/pdf/api/entity/' + st.gid, { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .catch(function (e) { try { if (RC.outbox && e && e.name === 'TypeError') RC.outbox.send('entst', st.gid + ':' + i, '/pdf/api/entity/' + st.gid, body, 'PATCH'); } catch (e2) {} });
  }
  function dockToShell(container, st, c) {
    // ★ 复用 vc-card 外壳三态(圆/长条/方块):评分后把倒计时写进长条摘要 .vc-card-sum,单卡自动收成长条;
    //   侧栏/无外壳则优雅跳过。rc-flashcard 只管 bd 内容,形态收纳一律交外壳,不自造(用户拍板)。
    try {
      var host = container.closest && container.closest('.vc-card');
      if (!host) return;
      var sum = host.querySelector('.vc-card-sum');
      if (sum) sum.textContent = '🎴 已复习 · 距下次复习 ' + nextLabel(c._next);
      if (st.cards.length === 1 && RC.voiceCard && RC.voiceCard.form) RC.voiceCard.form(host, 'min');
    } catch (e) {}
  }
  function rate(container, i, ease) {
    if (!(ease >= 1 && ease <= 4)) return;
    var st = container.__fc, c = st.cards[i];
    var aid = 'rv_' + Array.from(crypto.getRandomValues(new Uint8Array(8))).map(function (b) { return b.toString(16).padStart(2, '0'); }).join('');
    var body = { aid: aid, note_id: c._nid, ease: ease };
    c._st = 'done'; updateSlide(container, i); broadcast(st.gid, i, container);
    fetch('/pdf/api/review-answer', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (d && d.ok) { c._next = d.next || {}; updateSlide(container, i); dockToShell(container, st, c); broadcast(st.gid, i, container); _stateSync(st, i); } })
      .catch(function (e) { if (RC.outbox && e && e.name === 'TypeError') RC.outbox.send('rev', aid, '/pdf/api/review-answer', body); });
  }
  RC.flashcard = { mountDrafts: mountDrafts, mountPreview: mountPreview, mountState: mountState, pinToPage: pinToPage };
})();
