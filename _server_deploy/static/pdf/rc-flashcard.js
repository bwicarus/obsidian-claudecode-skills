/* rc-flashcard.js — 融合复习卡(用户设计定稿 2026-07-21 修正,references/card-review-integration.md)。
 * 正确状态机(用户拍板):
 *   draft 草稿态(保留/修改):可编辑内容 + [🗑 删除] [✓ 入库]。点入库=**直接进 Anki**(即使不再操作也没关系)。
 *   ↓ 入库(POST /pdf/api/anki-add-cards → 拿 note_id)
 *   learn 学习态 = **普通 Anki 卡**:显示正面 → 点击显示背面 → 四档掌握度(再来/困难/良好/简单)。
 *   ↓ 评分(POST /pdf/api/review-answer{note_id,ease} → answerCards 真 FSRS + 返回下次到期)
 *   collapsed 收起态:长条 + 「距下次复习」倒计时(Anki 给的冷却时间)。可再点 → 圆球(B3 后续)。
 * 卡片版面后续直接复用到 Anki 复习页(rc-review)——用户说之后再讨论。
 * 美术:暗色 fc-* 系(#0d1322/#1f2740);B?后续可换 __vcInfoCardEl 天气卡形态 + 字幕模式镜像。 */
(function () {
  'use strict';
  var RC = (window.RC = window.RC || {});
  if (RC.flashcard) return;

  function esc(x) { return RC.esc ? RC.esc(x) : String(x == null ? '' : x); }
  function md(x) { try { return RC.md ? RC.md(String(x || '')) : esc(x); } catch (e) { return esc(x); } }
  function clozeSeg(t, showAns) { return md(String(t || '').replace(/\{\{c\d+::(.*?)(::[^}]*)?\}\}/g, showAns ? '<b>$1</b>' : '<b>[…]</b>')); }
  var _EASE = [['1', '再来', 'e1'], ['2', '困难', 'e2'], ['3', '良好', 'e3'], ['4', '简单', 'e4']];

  function injectCss() {
    if (document.getElementById('rc-flashcard-css')) return;
    var st = document.createElement('style'); st.id = 'rc-flashcard-css';
    st.textContent =
      '.fc-wrap{margin-top:8px}' +
      '.fc-nav{display:flex;align-items:center;gap:8px;font-size:12px;color:#8a9bb4;margin-bottom:6px}' +
      '.fc-nav button{background:#1a2540;border:1px solid #2a3550;color:#cfe6ff;border-radius:6px;padding:2px 10px;cursor:pointer;-webkit-tap-highlight-color:transparent}' +
      '.fc-card{background:#0d1322;border:1px solid #1f2740;border-radius:10px;padding:14px;font-size:15px;line-height:1.7;color:#e6e6f0}' +
      '.fc-lbl{font-size:10px;color:#7a8497;margin-bottom:3px}' +
      '.fc-ed{width:100%;box-sizing:border-box;background:#10182c;border:1px solid #2a3550;border-radius:8px;color:#e6e6f0;font:inherit;font-size:14px;line-height:1.6;padding:10px 12px;min-height:110px;resize:vertical;margin-bottom:8px}' +
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
      '.fc-collapsed{display:flex;align-items:center;gap:10px;background:#0d1322;border:1px solid #1f2740;border-radius:10px;padding:10px 14px;cursor:pointer;font-size:13px;color:#8a9bb4}' +
      '.fc-collapsed b{color:#86efac}';
    document.head.appendChild(st);
  }

  // 下次复习倒计时文案(Anki interval=天;queue 负=学习中按分钟)
  function nextLabel(next) {
    next = next || {};
    var iv = next.interval;
    if (typeof iv === 'number' && iv > 0) {
      if (iv >= 1) return iv + ' 天后';
      return Math.max(1, Math.round(iv * 24 * 60)) + ' 分钟后';
    }
    return '很快';   // 学习队列内(几分钟)/拿不到 → 兜底
  }

  function mountDrafts(container, cards, opts) {
    if (!container || !cards || !cards.length) return;
    injectCss();
    container.__fc = {
      cards: cards.map(function (c) {
        return { type: (c.type || 'basic'), front: c.front || '', back: c.back || '', cloze: c.cloze || c.text || '',
                 _st: 'draft', _showBack: false, _nid: null, _next: null };
      }),
      idx: 0, opts: opts || {}, readonly: false
    };
    render(container);
  }

  // 只读预览(已入库卡,不给编辑;正反同显)——语音路径若 result 非 deferred 时用
  function mountPreview(container, cards) {
    if (!container || !cards || !cards.length) return;
    injectCss();
    container.__fc = { cards: cards.map(function (c) { return { type: (c.type || 'basic'), front: c.front || '', back: c.back || '', cloze: c.cloze || c.text || '', _st: 'preview' }; }), idx: 0, opts: {}, readonly: true };
    render(container);
  }

  function render(container) {
    var st = container.__fc; if (!st) return;
    var n = st.cards.length;
    if (!n) { container.innerHTML = '<div class="fc-collapsed">（草稿已全部删除）</div>'; return; }
    if (st.idx >= n) st.idx = n - 1;
    var c = st.cards[st.idx];
    var stLabel = c._st === 'draft' ? '✏️ 草稿(可改)' : c._st === 'learn' ? '📖 学习中' : c._st === 'collapsed' ? '✓ 已入库' : '预览';
    var nav = '<div class="fc-nav"><button data-fc="prev">‹</button><span>卡 ' + (st.idx + 1) + '/' + n + '（' + stLabel + '）</span><button data-fc="next">›</button></div>';
    var body;

    if (c._st === 'collapsed') {
      // 收起态:长条 + 下次复习倒计时(点击可展开回看)
      body = '<div class="fc-collapsed" data-fc="expand">🗂 已入 Anki · 距下次复习 <b>' + esc(nextLabel(c._next)) + '</b> · 点看</div>';
      container.innerHTML = '<div class="fc-wrap">' + nav + body + '</div>';
    } else if (c._st === 'draft') {
      body = c.type === 'cloze'
        ? '<div class="fc-lbl">填空(cloze,答案用 {{c1::…}} 包住)</div><textarea class="fc-ed" data-f="cloze">' + esc(c.cloze) + '</textarea>'
        : '<div class="fc-lbl">正面</div><textarea class="fc-ed" data-f="front">' + esc(c.front) + '</textarea>' +
          '<div class="fc-lbl">背面</div><textarea class="fc-ed" data-f="back">' + esc(c.back) + '</textarea>';
      body += '<div class="fc-btns"><button class="fc-del" data-fc="del">🗑 删除</button><button class="fc-add" data-fc="add">✓ 入库到 Anki</button></div>';
      container.innerHTML = '<div class="fc-wrap">' + nav + '<div class="fc-card">' + body + '</div></div>';
    } else if (c._st === 'preview') {
      var pf = c.type === 'cloze' ? clozeSeg(c.cloze, false) : md(c.front);
      var pb = c.type === 'cloze' ? clozeSeg(c.cloze, true) : md(c.back);
      body = '<div class="fc-lbl">正面</div>' + pf + '<div class="fc-back"><div class="fc-lbl">背面</div>' + pb + '</div>';
      container.innerHTML = '<div class="fc-wrap">' + nav.replace(stLabel, '✓ 已入 Anki') + '<div class="fc-card">' + body + '</div></div>';
    } else {   // learn:普通 Anki 学习态
      var front = c.type === 'cloze' ? clozeSeg(c.cloze, false) : md(c.front);
      var back = c.type === 'cloze' ? clozeSeg(c.cloze, true) : md(c.back);
      if (!c._showBack) {
        body = '<div class="fc-face" data-fc="reveal">' + front + '<div class="fc-hint">点击显示答案 ▾</div></div>';
      } else {
        body = '<div class="fc-face">' + front + '<div class="fc-back">' + back + '</div></div>' +
          '<div class="fc-eases">' + _EASE.map(function (e) {
            return '<button class="fc-e ' + e[2] + '" data-ease="' + e[0] + '">' + e[1] + '</button>';
          }).join('') + '</div>';
      }
      container.innerHTML = '<div class="fc-wrap">' + nav + '<div class="fc-card">' + body + '</div></div>';
    }
    try { RC.typeset && RC.typeset(container.querySelector('.fc-card')); } catch (e) {}

    container.querySelectorAll('[data-fc]').forEach(function (el) {
      el.addEventListener('click', function (ev) {
        ev.stopPropagation();
        var act = el.dataset.fc, cc = st.cards[st.idx];
        if (act === 'prev') { st.idx = Math.max(0, st.idx - 1); render(container); }
        else if (act === 'next') { st.idx = Math.min(st.cards.length - 1, st.idx + 1); render(container); }
        else if (act === 'del') { st.cards.splice(st.idx, 1); render(container); RC.toast && RC.toast('草稿已删除(未入库)'); }
        else if (act === 'add') { addToAnki(container, cc); }
        else if (act === 'reveal') { cc._showBack = true; render(container); }
        else if (act === 'expand') { cc._st = 'learn'; cc._showBack = false; render(container); }
      });
    });
    container.querySelectorAll('.fc-ed').forEach(function (ta) {
      ta.addEventListener('input', function () { st.cards[st.idx][ta.dataset.f] = ta.value; });
    });
    container.querySelectorAll('.fc-e').forEach(function (el) {
      el.addEventListener('click', function (ev) { ev.stopPropagation(); rate(container, st.cards[st.idx], parseInt(el.dataset.ease, 10)); });
    });
  }

  // 草稿 → 直接入 Anki(拿 note_id)→ 转学习态
  function addToAnki(container, c) {
    var aid = 'fc_' + Array.from(crypto.getRandomValues(new Uint8Array(8))).map(function (b) { return b.toString(16).padStart(2, '0'); }).join('');
    var payload = { aid: aid, cards: [{ type: c.type, front: c.front, back: c.back, cloze: c.cloze }] };
    c._st = 'learn'; c._showBack = false; render(container);   // 乐观进学习态;失败回退 draft
    fetch('/pdf/api/anki-add-cards', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || d.ok === false) { c._st = 'draft'; render(container); RC.toast && RC.toast('入库失败:' + ((d && d.error) || '?')); }
        else { c._nid = (d.note_ids || [])[0]; RC.toast && RC.toast('✓ 已入 Anki,可直接复习这张'); }
      })
      .catch(function (e) {
        if (RC.outbox && e && e.name === 'TypeError') { RC.outbox.send('fcadd', aid, '/pdf/api/anki-add-cards', payload); RC.toast && RC.toast('离线:入库已入队,恢复后自动同步'); }
        else { c._st = 'draft'; render(container); RC.toast && RC.toast('入库失败'); }
      });
  }

  // 学习态四档评分 → answerCards(真 FSRS)→ 收起 + 倒计时
  function rate(container, c, ease) {
    if (!(ease >= 1 && ease <= 4)) return;
    var aid = 'rv_' + Array.from(crypto.getRandomValues(new Uint8Array(8))).map(function (b) { return b.toString(16).padStart(2, '0'); }).join('');
    var body = { aid: aid, note_id: c._nid, ease: ease };
    c._st = 'collapsed'; render(container);   // 乐观收起
    fetch('/pdf/api/review-answer', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (d && d.ok) { c._next = d.next || {}; render(container); } })
      .catch(function (e) {
        if (RC.outbox && e && e.name === 'TypeError') { RC.outbox.send('rev', aid, '/pdf/api/review-answer', body); }
      });
  }

  RC.flashcard = { mountDrafts: mountDrafts, mountPreview: mountPreview };
})();
