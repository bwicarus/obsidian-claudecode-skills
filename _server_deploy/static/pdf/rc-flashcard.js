/* rc-flashcard.js — 融合复习卡 B1(设计:references/card-review-integration.md,用户规格 2026-07-21)。
 * 状态机:草稿(编辑/撤销)→[完成]→学习(正面→点按显背面;翻面方式=localStorage fc-flip:'flip'|'both')
 * →[☆掌握确认]→已入库(经 /pdf/api/anki-add-cards,幂等 aid;离线入 outbox)。未确认的卡不进 Anki 库。
 * B1=单卡+简单 ‹›切换;B2 改 CSS scroll-snap 中线吸附;B3 收纳链(长条/倒计时/圆球);B4 拖出钉页。
 * 美术与现有暗色卡一致(#0d1322/#1f2740 系)。挂载:RC.flashcard.mountDrafts(container, cards, opts)。 */
(function () {
  'use strict';
  var RC = (window.RC = window.RC || {});
  if (RC.flashcard) return;

  function esc(x) { return RC.esc ? RC.esc(x) : String(x == null ? '' : x); }
  function md(x) { try { return RC.md ? RC.md(String(x || '')) : esc(x); } catch (e) { return esc(x); } }
  function flipMode() { try { return localStorage.getItem('fc-flip') === 'both' ? 'both' : 'flip'; } catch (e) { return 'flip'; } }

  function injectCss() {
    if (document.getElementById('rc-flashcard-css')) return;
    var st = document.createElement('style'); st.id = 'rc-flashcard-css';
    st.textContent =
      '.fc-wrap{margin-top:8px}' +
      '.fc-nav{display:flex;align-items:center;gap:8px;font-size:12px;color:#8a9bb4;margin-bottom:6px}' +
      '.fc-nav button{background:#1a2540;border:1px solid #2a3550;color:#cfe6ff;border-radius:6px;padding:2px 10px;cursor:pointer;-webkit-tap-highlight-color:transparent}' +
      '.fc-card{background:#0d1322;border:1px solid #1f2740;border-radius:10px;padding:12px;font-size:14px;line-height:1.6;color:#e6e6f0}' +
      '.fc-state{font-size:10px;color:#5a6680;margin-bottom:6px}' +
      '.fc-ed{width:100%;box-sizing:border-box;background:#10182c;border:1px solid #2a3550;border-radius:8px;color:#e6e6f0;font:inherit;padding:8px;min-height:52px;margin-bottom:6px}' +
      '.fc-lbl{font-size:10px;color:#7a8497;margin-bottom:2px}' +
      '.fc-btns{display:flex;gap:7px;margin-top:8px}' +
      '.fc-btns button{flex:1;border-radius:8px;padding:9px 0;font-size:13px;cursor:pointer;border:1px solid #2a3550;background:#1a2540;color:#cfe6ff;-webkit-tap-highlight-color:transparent}' +
      '.fc-del{border-color:#7f1d1d!important;color:#fca5a5!important}' +
      '.fc-ok{border-color:#14532d!important;color:#86efac!important}' +
      '.fc-face{cursor:pointer}.fc-face .fc-hint{font-size:10px;color:#5a6680;margin-top:6px}' +
      '.fc-back{border-top:1px dashed #2a3550;margin-top:8px;padding-top:8px}' +
      '.fc-done{color:#86efac;font-size:13px;padding:6px 0}';
    document.head.appendChild(st);
  }

  // container 内状态:{cards:[...每张加 _st:'draft'|'learn'|'done', _showBack:false], idx}
  function mountDrafts(container, cards, opts) {
    if (!container || !cards || !cards.length) return;
    injectCss();
    var st = {
      cards: cards.map(function (c) { return { type: (c.type || 'basic'), front: c.front || '', back: c.back || '', cloze: c.cloze || c.text || '', _st: 'draft', _showBack: false }; }),
      idx: 0, opts: opts || {}
    };
    container.__fc = st;
    render(container);
  }

  function render(container) {
    var st = container.__fc; if (!st) return;
    var n = st.cards.length;
    if (!n) { container.innerHTML = '<div class="fc-done">（所有草稿已撤销）</div>'; return; }
    if (st.idx >= n) st.idx = n - 1;
    var c = st.cards[st.idx];
    var nav = '<div class="fc-nav"><button data-fc="prev">‹</button><span>卡 ' + (st.idx + 1) + '/' + n +
      '（' + (c._st === 'draft' ? '✏️ 修改模式' : c._st === 'learn' ? '📖 学习模式' : '✓ 已入库') + '）</span><button data-fc="next">›</button></div>';
    var body;
    if (c._st === 'draft') {
      body = c.type === 'cloze'
        ? '<div class="fc-lbl">填空(cloze,{{c1::…}})</div><textarea class="fc-ed" data-f="cloze">' + esc(c.cloze) + '</textarea>'
        : '<div class="fc-lbl">正面</div><textarea class="fc-ed" data-f="front">' + esc(c.front) + '</textarea>' +
          '<div class="fc-lbl">背面</div><textarea class="fc-ed" data-f="back">' + esc(c.back) + '</textarea>';
      body += '<div class="fc-btns"><button class="fc-del" data-fc="del">🗑 撤销这张</button><button class="fc-ok" data-fc="fin">✓ 完成</button></div>';
    } else if (c._st === 'learn') {
      var frontHtml = md(c.type === 'cloze' ? c.cloze.replace(/\{\{c\d+::(.*?)(::[^}]*)?\}\}/g, '<b>[…]</b>') : c.front);
      var backHtml = md(c.type === 'cloze' ? c.cloze.replace(/\{\{c\d+::(.*?)(::[^}]*)?\}\}/g, '<b>$1</b>') : c.back);
      if (!c._showBack) {
        body = '<div class="fc-face" data-fc="reveal">' + frontHtml + '<div class="fc-hint">点击显示背面 ▾</div></div>';
      } else if (flipMode() === 'both') {
        body = '<div class="fc-face">' + frontHtml + '<div class="fc-back">' + backHtml + '</div></div>' +
          '<div class="fc-btns"><button class="fc-ok" data-fc="confirm">☆ 掌握确认(入库)</button></div>';
      } else {
        body = '<div class="fc-face" data-fc="reveal">' + backHtml + '<div class="fc-hint">点击翻回正面</div></div>' +
          '<div class="fc-btns"><button class="fc-ok" data-fc="confirm">☆ 掌握确认(入库)</button></div>';
      }
    } else {
      body = '<div class="fc-done">✓ 确认已完成,已入 Anki(QA 牌组)</div>';   // B3:收长条+倒计时
    }
    container.innerHTML = '<div class="fc-wrap">' + nav + '<div class="fc-card">' + body + '</div></div>';
    try { RC.typeset && RC.typeset(container.querySelector('.fc-card')); } catch (e) {}

    container.querySelectorAll('[data-fc]').forEach(function (el) {
      el.addEventListener('click', function (ev) {
        ev.stopPropagation();
        var act = el.dataset.fc, cc = st.cards[st.idx];
        if (act === 'prev') { st.idx = Math.max(0, st.idx - 1); render(container); }
        else if (act === 'next') { st.idx = Math.min(st.cards.length - 1, st.idx + 1); render(container); }
        else if (act === 'del') { st.cards.splice(st.idx, 1); render(container); RC.toast && RC.toast('已撤销草稿(未入库)'); }
        else if (act === 'fin') {
          container.querySelectorAll('.fc-ed').forEach(function (ta) { cc[ta.dataset.f] = ta.value; });
          cc._st = 'learn'; cc._showBack = false; render(container);
        }
        else if (act === 'reveal') { cc._showBack = !cc._showBack; render(container); }
        else if (act === 'confirm') { confirmCard(container, cc); }
      });
    });
    // 编辑内容随时落回状态(防切卡丢改动)
    container.querySelectorAll('.fc-ed').forEach(function (ta) {
      ta.addEventListener('input', function () { st.cards[st.idx][ta.dataset.f] = ta.value; });
    });
  }

  function confirmCard(container, c) {
    var aid = 'fc_' + Array.from(crypto.getRandomValues(new Uint8Array(8))).map(function (b) { return b.toString(16).padStart(2, '0'); }).join('');
    var payload = { aid: aid, cards: [{ type: c.type, front: c.front, back: c.back, cloze: c.cloze }] };
    c._st = 'done'; render(container);   // 乐观;失败回退 learn
    fetch('/pdf/api/anki-add-cards', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || d.ok === false) { c._st = 'learn'; render(container); RC.toast && RC.toast('入库失败:' + ((d && d.error) || '?')); }
        else { c._nid = (d.note_ids || [])[0]; RC.toast && RC.toast('✓ 已入 Anki'); }
      })
      .catch(function (e) {
        if (RC.outbox && e && e.name === 'TypeError') { RC.outbox.send('fcadd', aid, '/pdf/api/anki-add-cards', payload); RC.toast && RC.toast('离线:确认已记录,恢复后自动入库'); }
        else { c._st = 'learn'; render(container); RC.toast && RC.toast('入库失败'); }
      });
  }

  RC.flashcard = { mountDrafts: mountDrafts };
})();
