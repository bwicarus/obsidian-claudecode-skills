/* rc-review.js — 侧栏「复习」tab v1(2026-07-20,"尽可能脱离服务器"批):
 * 到期卡队列拉到本地(localStorage 快照,SW netFallback → 离线可复习);答题乐观推进,
 * POST /pdf/api/review-answer 回流真 Anki(服务端 answerCards=真 FSRS 调度;aid 幂等防补投双答);
 * 离线/网络错 → RC.outbox 入队。Again(1)卡片回到本地队尾。
 * 自挂载:等 #ep-side-tabs 出现(rc-sidedrawer 建好)→ 注入 tab+pane(class 同抽屉体系)。
 * v2(产品化):ts-fsrs 端上真调度 + 卡片库 IndexedDB。 */
(function () {
  'use strict';
  var RC = (window.RC = window.RC || {});
  if (RC.review) return;
  var LSQ = 'review-queue-v1';
  var _queue = [], _idx = 0, _showingAns = false, _dueTotal = 0;

  function _esc(s) { return (RC.esc ? RC.esc(s) : String(s || '')); }
  function _sanitize(html) {   // Anki 卡自家内容,但进侧栏前仍剥 <script>(防旧模板脚本在阅读器里乱跑)
    try { var d = document.createElement('div'); d.innerHTML = String(html || '');
          d.querySelectorAll('script').forEach(function (x) { x.remove(); }); return d.innerHTML; }
    catch (e) { return ''; }
  }
  function _saveLocal() { try { localStorage.setItem(LSQ, JSON.stringify({ ts: Date.now(), due_total: _dueTotal, cards: _queue.slice(_idx) })); } catch (e) {} }

  async function loadQueue(force) {
    var pane = document.getElementById('rc-review-body'); if (!pane) return;
    if (!force) {
      try { var c = JSON.parse(localStorage.getItem(LSQ) || 'null');
            if (c && c.cards && c.cards.length && Date.now() - c.ts < 30 * 60000) { _queue = c.cards; _idx = 0; _dueTotal = c.due_total || c.cards.length; render(); return; } } catch (e) {}
    }
    pane.innerHTML = '<div class="rv-dim">⏳ 拉取到期卡…</div>';
    try {
      var d = await (await fetch('/pdf/api/review-queue?limit=30')).json();
      if (!d.ok) throw new Error(d.error || 'fail');
      _queue = d.cards || []; _idx = 0; _dueTotal = d.due_total || _queue.length;
      _saveLocal(); render();
    } catch (e) {
      try { var c2 = JSON.parse(localStorage.getItem(LSQ) || 'null');
            if (c2 && c2.cards && c2.cards.length) { _queue = c2.cards; _idx = 0; _dueTotal = c2.due_total || 0; render(); RC.toast && RC.toast('离线:用本机复习快照'); return; } } catch (e2) {}
      pane.innerHTML = '<div class="rv-dim">拉取失败:' + _esc(e.message) + '<br><button class="rv-btn" onclick="RC.review.reload()">重试</button></div>';
    }
  }

  function render() {
    var pane = document.getElementById('rc-review-body'); if (!pane) return;
    var head = '<div class="rv-head">到期 ' + _dueTotal + ' · 本批剩 ' + Math.max(0, _queue.length - _idx) +
      ' <button class="rv-x" title="重新拉取" onclick="RC.review.reload()">⟳</button></div>';
    if (_idx >= _queue.length) {
      pane.innerHTML = head + '<div class="rv-done">🎉 本批完成!' + (_dueTotal > _queue.length ? '(还有到期卡,⟳ 拉下一批)' : '') + '</div>';
      _saveLocal(); return;
    }
    var c = _queue[_idx];
    var body = '<div class="rv-card">' + _sanitize(_showingAns ? c.answer : c.question) + '</div>';
    var btns = _showingAns
      ? '<div class="rv-eases">' +
        '<button class="rv-e rv-e1" onclick="RC.review.answer(1)">再来</button>' +
        '<button class="rv-e rv-e2" onclick="RC.review.answer(2)">困难</button>' +
        '<button class="rv-e rv-e3" onclick="RC.review.answer(3)">良好</button>' +
        '<button class="rv-e rv-e4" onclick="RC.review.answer(4)">简单</button></div>'
      : '<button class="rv-show" onclick="RC.review.show()">显示答案</button>';
    pane.innerHTML = head + '<div class="rv-deck">' + _esc(c.deck) + '</div>' + body + btns;
    try { RC.typeset && RC.typeset(pane.querySelector('.rv-card')); } catch (e) {}
  }

  function show() { _showingAns = true; render(); }

  function answer(ease) {
    var c = _queue[_idx]; if (!c) return;
    _showingAns = false;
    if (ease === 1) _queue.push(c);   // Again:本地回队尾(真间隔由 Anki 调度,此处只管本批)
    _idx++;
    render(); _saveLocal();
    var aid = 'a_' + Array.from(crypto.getRandomValues(new Uint8Array(8))).map(function (b) { return b.toString(16).padStart(2, '0'); }).join('');
    var body = { aid: aid, card_id: c.id, ease: ease };
    fetch('/pdf/api/review-answer', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (!d || d.ok === false) { RC.toast && RC.toast('答题同步失败:' + ((d && d.error) || '?')); } })
      .catch(function (e) {
        if (RC.outbox && e && e.name === 'TypeError') { RC.outbox.send('rev', aid, '/pdf/api/review-answer', body); RC.toast && RC.toast('离线:答题已入队,恢复后同步'); }
        else { RC.toast && RC.toast('答题同步失败'); }
      });
  }

  function injectCss() {
    if (document.getElementById('rc-review-css')) return;
    var st = document.createElement('style'); st.id = 'rc-review-css';
    st.textContent = '#rc-review-body{padding:12px;display:flex;flex-direction:column;gap:10px;min-height:0}' +
      '.rv-head{font-size:12px;color:#8a9bb4;display:flex;align-items:center;gap:8px}' +
      '.rv-x{margin-left:auto;background:transparent;border:1px solid #2a3550;color:#7a8497;border-radius:6px;padding:2px 9px;cursor:pointer}' +
      '.rv-deck{font-size:10px;color:#5a6680}' +
      '.rv-card{background:#0d1322;border:1px solid #1f2740;border-radius:10px;padding:14px;font-size:15px;line-height:1.6;color:#e6e6f0;overflow:auto;max-height:46vh}' +
      '.rv-card img{max-width:100%}' +
      '.rv-show{background:#244470;border:1px solid #3b6db5;color:#cfe6ff;border-radius:9px;padding:10px;font-size:14px;cursor:pointer;-webkit-tap-highlight-color:transparent}' +
      '.rv-eases{display:flex;gap:7px}' +
      '.rv-e{flex:1;border:1px solid #2a3550;border-radius:9px;padding:10px 0;font-size:13px;cursor:pointer;background:#1a2540;color:#cfe6ff;-webkit-tap-highlight-color:transparent}' +
      '.rv-e1{border-color:#7f1d1d;color:#fca5a5}.rv-e2{border-color:#78350f;color:#fcd34d}' +
      '.rv-e3{border-color:#14532d;color:#86efac}.rv-e4{border-color:#1e3a8a;color:#93c5fd}' +
      '.rv-dim,.rv-done{color:#8a9bb4;font-size:13px;padding:16px;text-align:center}' +
      '.rv-btn{margin-top:8px;background:#1a2540;border:1px solid #2a3550;color:#cfe6ff;border-radius:7px;padding:6px 14px;cursor:pointer}';
    document.head.appendChild(st);
  }

  function mount() {
    var tabs = document.getElementById('ep-side-tabs');
    var side = document.getElementById('ep-side');
    if (!tabs || !side) return false;
    if (document.querySelector('#ep-side-tabs [data-pane="review"]')) return true;
    injectCss();
    var b = document.createElement('button');
    b.className = 'ep-side-tab'; b.dataset.pane = 'review'; b.title = '复习';
    b.innerHTML = '<svg class="si" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="5" width="16" height="14" rx="2"/><path d="M8 3v4M16 3v4M4 11h16"/></svg><span class="ep-side-tab-lb">复习</span>';
    b.addEventListener('click', function () { try { RC.sidedrawer.setTab('review'); } catch (e) {} loadQueue(false); });
    var sp = tabs.querySelector('.ep-side-tab-sp');
    tabs.insertBefore(b, sp || null);
    var pane = document.createElement('div');
    pane.className = 'ep-side-pane'; pane.dataset.pane = 'review';
    pane.innerHTML = '<div id="rc-review-body"><div class="rv-dim">点开即拉当日到期卡</div></div>';
    side.appendChild(pane);
    return true;
  }
  var _mt = setInterval(function () { if (mount()) clearInterval(_mt); }, 600);
  setTimeout(function () { clearInterval(_mt); }, 25000);

  RC.review = { reload: function () { loadQueue(true); }, show: show, answer: answer, _mount: mount };
})();
