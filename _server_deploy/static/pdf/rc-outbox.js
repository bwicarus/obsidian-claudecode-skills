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
  // v3(2026-07-21 用户提案"攒批分段传输"):全队打包为一次 /api/sync-batch(N 写一次连接,
  // 服务端子请求分发,鉴权/幂等原样);同键合并早已保证"窗口内反复改不重发"。
  async function flush() {
    if (_busy) return;
    _busy = true;
    try {
      var m = _load(), ks = Object.keys(m);
      if (!ks.length) return;
      var ops = ks.map(function (k) { var it = m[k]; return { key: k, url: it.url, method: it.method || 'POST', body: it.body, ts: it.ts }; });
      var r;
      try {
        r = await fetch('/pdf/api/sync-batch', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ ops: ops.map(function (o) { return { url: o.url, method: o.method, body: o.body }; }) }),
                        keepalive: ops.length <= 8 });
      } catch (e) { return; }               // 网络不通:整队保留
      if (!r.ok) return;                     // 批量端点自身 4xx/5xx:保留下轮(部署过渡期兼容)
      var d = null; try { d = await r.json(); } catch (e) {}
      var res = (d && d.results) || [];
      var cur = _load();
      for (var i = 0; i < ops.length; i++) {
        var st = (res[i] && res[i].status) || 0;
        if (st >= 200 && st < 500) {         // 2xx 成功 / 4xx 数据性拒绝 → 出队;5xx/缺结果 → 留队
          if (cur[ops[i].key] && cur[ops[i].key].ts === ops[i].ts) delete cur[ops[i].key];
          if (st >= 400) { try { console.warn('[outbox] 服务端拒绝', ops[i].url, st); } catch (e) {} }
        }
      }
      _save(cur);
    } finally { _busy = false; }
  }
  // 离场兜底:关页/切后台瞬间把整队 beacon 出去(读不到响应→队列保留,端点幂等→重投安全)。
  // 扩展环境(跨源经 background 桥)beacon 打不到 Pi → 跳过,靠窗口/心跳。
  function _beacon() {
    try {
      if (window.__bwReaderFetch) return;
      var m = _load(), ks = Object.keys(m);
      if (!ks.length || !navigator.sendBeacon) return;
      var ops = ks.map(function (k) { var it = m[k]; return { url: it.url, method: it.method || 'POST', body: it.body }; });
      navigator.sendBeacon('/pdf/api/sync-batch', new Blob([JSON.stringify({ ops: ops })], { type: 'application/json' }));
    } catch (e) {}
  }
  window.addEventListener('pagehide', _beacon);
  document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'hidden') _beacon(); });
  RC.outbox = {
    send: function (kind, key, url, body, method) {
      var m = _load();
      m[kind + ':' + key] = { url: url, body: body, ts: Date.now(), kind: kind, method: method || 'POST' };
      _save(m);
      // 攒批窗口:首个待传改动起 5s 后统一发(窗口内同键反复改=只发终态);≥20 条提前发
      if (Object.keys(m).length >= 20) { if (RC.outbox._winT) { clearTimeout(RC.outbox._winT); RC.outbox._winT = null; } setTimeout(flush, 50); }
      else if (!RC.outbox._winT) RC.outbox._winT = setTimeout(function () { RC.outbox._winT = null; flush(); }, 30000);   // 30s 窗口(2026-07-21 用户:5s 太频;队列落盘+离场 beacon 兜底,拉长零风险)
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
  setInterval(function () { if (RC.outbox.size()) flush(); }, 60000);   // 心跳对齐 30s 窗口(只兜卡住的队)
  setTimeout(flush, 2500);   // 开页补投上次遗留
})();
