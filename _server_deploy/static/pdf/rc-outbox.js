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
          r = await fetch(it.url, { method: it.method || 'POST', headers: { 'Content-Type': 'application/json' },
                          body: (it.body == null ? undefined : JSON.stringify(it.body)) });
        } catch (e) { return; }               // 网络不通:停止本轮,整队保留
        if (r.status >= 500) return;           // 服务端故障:保留下轮
        var cur = _load();                     // 期间可能被同键新写覆盖 → 只删自己那个版本
        if (cur[ks[i]] && cur[ks[i]].ts === it.ts) { delete cur[ks[i]]; _save(cur); }
        if (r.status >= 400) { try { console.warn('[outbox] 服务端拒绝', it.url, r.status); } catch (e) {} }
      }
    } finally { _busy = false; }
  }
  RC.outbox = {
    send: function (kind, key, url, body, method) {
      var m = _load();
      m[kind + ':' + key] = { url: url, body: body, ts: Date.now(), kind: kind, method: method || 'POST' };
      _save(m);
      setTimeout(flush, 50);
    },
    flush: flush,
    size: function () { return Object.keys(_load()).length; }
  };
  // ── 写拦截(高亮 PATCH/DELETE):调用点分散在 19-dict/28-shared-drawer/25-assistant 等四处,
  //    统一在 fetch 层兜:网络不通 → 入队(键=操作+id,同键留最新)+ 合成 {ok:true,queued:true} 让
  //    乐观 UI 照常走;恢复后按原 method 重放。重放顺序=插入序(JS 对象键序)→ 离线"建了又删"也正确。
  var _f0 = window.fetch.bind(window);
  window.fetch = function (input, init) {
    var url = (typeof input === 'string') ? input : ((input && input.url) || '');
    var method = (((init && init.method) || (input && input.method) || 'GET') + '').toUpperCase();
    var isHl = url.indexOf('/pdf/api/highlights') === 0 && (method === 'PATCH' || method === 'DELETE');
    var isNote = url.indexOf('/pdf/api/notes') === 0 && (method === 'POST' || method === 'PATCH' || method === 'DELETE');
    if (!isHl && !isNote) return _f0(input, init);
    return _f0(input, init).catch(function (e) {
      if (!(e && e.name === 'TypeError')) throw e;
      var body = null;
      try { body = (init && typeof init.body === 'string') ? JSON.parse(init.body) : null; } catch (_) {}
      var id = (body && body.id) || '';
      try { if (!id) id = new URL(url, location.origin).searchParams.get('id') || ''; } catch (_) {}
      if (isNote && method === 'POST' && !id && body) {   // 便签离线新建:此层注入客户端 id(服务端幂等 upsert)
        id = 'c_' + Array.from(crypto.getRandomValues(new Uint8Array(8))).map(function (b) { return b.toString(16).padStart(2, '0'); }).join('');
        body.id = id;
      }
      if (!id) throw e;   // 拿不到 id 没法幂等合并 → 维持原失败
      var tag = isHl ? 'hl' : 'note';
      var key = (method === 'DELETE') ? (tag + 'd:' + id)
        : (method === 'POST') ? (tag + 'c:' + id)
        : (tag + 'p:' + id + ':' + Object.keys(body || {}).sort().join(','));
      RC.outbox.send(tag, key, url, body, method);
      var synth = { ok: true, queued: true, id: id };
      if (isHl && method === 'PATCH' && body) synth.highlight = body;
      if (isNote && (method === 'POST' || method === 'PATCH') && body) {
        var now = Math.floor(Date.now() / 1000);
        synth.note = Object.assign({ color: '#fff8c5', w: 260, h: 180, collapsed: false, strokes: [],
                                     created: now, updated: now }, body);
      }
      return new Response(JSON.stringify(synth), { status: 200, headers: { 'Content-Type': 'application/json' } });
    });
  };
  window.addEventListener('online', function () { setTimeout(flush, 800); });
  setInterval(function () { if (RC.outbox.size()) flush(); }, 15000);
  setTimeout(flush, 2500);   // 开页补投上次遗留
})();
