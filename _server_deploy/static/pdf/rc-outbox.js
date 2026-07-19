/* rc-outbox.js — local-first 写队列(共享层,2026-07-20)。
 * 写操作本地先行(乐观 UI 由各调用点自管),网络失败时入队;恢复后自动重放同步 Pi。
 * 设计:同 kind+key 合并只留最新(掌握标记/阅读进度都是"终态"语义 → 天然幂等、无需保序);
 * 队列存 localStorage(rc-outbox-v1,跨刷新存活);重放触发 = 开页 / online 事件 / 15s 心跳。
 * 结果处理:网络错→保队列下轮再试;HTTP 5xx→保队列;4xx→丢弃(数据性拒绝,重试无意义,console 留痕)。
 * 调用点(均带 RC.outbox 守卫,未加载时回退原行为):rc-wordpop 掌握标记 / rc-phrasepop 词组掌握 /
 * pdf_reader.html 阅读进度上报。高亮/便签 CRUD 需客户端 id 方案,下一批。
 */
(function () {
  var RC = (window.RC = window.RC || {});
  if (RC.outbox) return;
  var KEY = 'rc-outbox-v1';
  function _load() { try { return JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch (e) { return {}; } }
  function _save(m) { try { localStorage.setItem(KEY, JSON.stringify(m)); } catch (e) {} }
  var _busy = false;
  async function flush() {
    if (_busy) return;
    _busy = true;
    try {
      var m = _load(), ks = Object.keys(m);
      for (var i = 0; i < ks.length; i++) {
        var it = m[ks[i]], r;
        try {
          r = await fetch(it.url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(it.body) });
        } catch (e) { return; }               // 网络不通:停止本轮,整队保留
        if (r.status >= 500) return;           // 服务端故障:保留下轮
        var cur = _load();                     // 期间可能被同键新写覆盖 → 只删自己那个版本
        if (cur[ks[i]] && cur[ks[i]].ts === it.ts) { delete cur[ks[i]]; _save(cur); }
        if (r.status >= 400) { try { console.warn('[outbox] 服务端拒绝', it.url, r.status); } catch (e) {} }
      }
    } finally { _busy = false; }
  }
  RC.outbox = {
    send: function (kind, key, url, body) {
      var m = _load();
      m[kind + ':' + key] = { url: url, body: body, ts: Date.now(), kind: kind };
      _save(m);
      setTimeout(flush, 50);
    },
    flush: flush,
    size: function () { return Object.keys(_load()).length; }
  };
  window.addEventListener('online', function () { setTimeout(flush, 800); });
  setInterval(function () { if (RC.outbox.size()) flush(); }, 15000);
  setTimeout(flush, 2500);   // 开页补投上次遗留
})();
