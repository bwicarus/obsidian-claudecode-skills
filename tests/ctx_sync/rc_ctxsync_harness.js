// 在 node 里加载**真实的** rc-core.js,用最小 DOM stub 跑 RC.ctxSync 的行为契约。
// 测的是真行为(有没有发请求、发了几次、发的什么、挂没挂监听),不是字符串匹配。
const fs = require('fs');
const path = require('path');

const SRC = path.join(__dirname, '..', '..', '_server_deploy', 'static', 'pdf', 'rc-core.js');

function fakeEl() {
  const H = {};
  return {
    __h: H,
    addEventListener(t, fn) { (H[t] = H[t] || []).push(fn); },
    fire(t, ev) { (H[t] || []).forEach(f => f(Object.assign({ pointerType: 'touch', clientX: 0, clientY: 0 }, ev))); },
  };
}

function makeEnv() {
  const store = {};
  const listeners = [];        // 记录挂了哪些监听器 → 验证「关闭时不挂监听」
  const posts = [];            // 记录每一次真实网络调用 → 验证节流合并
  const responses = [];
  let resolveNext = null;
  const g = {
    posts, listeners, store,
    localStorage: {
      getItem: (k) => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
    },
    navigator: {},             // 无 sendBeacon → beacon 路径回退 fetch
    document: {
      visibilityState: 'visible',
      getElementById: () => null,
      createElement: () => ({ style: {}, set textContent(v) { this._t = v; }, get innerHTML() { return this._t || ''; } }),
      documentElement: { insertBefore: () => {} },
      head: { insertBefore: () => {} },
      addEventListener: (t) => listeners.push('doc:' + t),
      removeEventListener: (t) => {
        const i = listeners.indexOf('doc:' + t); if (i >= 0) listeners.splice(i, 1);
      },
    },
    fetch: (url, opt) => {
      posts.push({ url, body: JSON.parse((opt && opt.body) || '{}') });
      return new Promise((res) => {
        resolveNext = () => {
          const configured = responses.shift() || { httpOk: true, body: { ok: true } };
          res({
            ok: configured.httpOk !== false,
            json: () => Promise.resolve(configured.body),
          });
        };
      });
    },
    queueResponse: (body, httpOk = true) => responses.push({ body, httpOk }),
    flushInflight: () => { if (resolveNext) { const r = resolveNext; resolveNext = null; r(); } },
  };
  // 定时器不是 ECMAScript 内置,vm context 里要显式注入(否则 debounce/心跳直接 ReferenceError)
  g.setTimeout = setTimeout; g.clearTimeout = clearTimeout;
  g.setInterval = setInterval; g.clearInterval = clearInterval;
  g.window = g;
  g.addEventListener = (t) => listeners.push('win:' + t);
  g.removeEventListener = (t) => {
    const i = listeners.indexOf('win:' + t); if (i >= 0) listeners.splice(i, 1);
  };
  return g;
}

function load(env) {
  const vm = require('vm');
  vm.createContext(env);
  vm.runInContext(fs.readFileSync(SRC, 'utf8'), env);
  return env.window.RC;
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
let failed = 0;
function check(name, cond, extra) {
  if (cond) { console.log('  ✓ ' + name); }
  else { console.log('  ✗ ' + name + (extra ? '  → ' + extra : '')); failed++; }
}

(async () => {
  // ── 1) 默认关:零网络、零监听 ─────────────────────────────────────────
  console.log('[1] 默认关闭 = 零开销');
  let env = makeEnv(); let RC = load(env);
  check('默认 enabled() 为 false', RC.ctxSync.enabled() === false);
  const r0 = RC.ctxSync.report({ kind: 'pdf', file: 'a.pdf', pos: 3 });
  await sleep(1300);
  check('关闭时 report() 返回 false', r0 === false);
  check('关闭时不发任何请求', env.posts.length === 0, JSON.stringify(env.posts));
  check('关闭时不挂任何监听器', env.listeners.length === 0, JSON.stringify(env.listeners));

  // ── 2) 开启后:连续翻页只合并成一次 ────────────────────────────────────
  console.log('[2] 1s trailing debounce 合并连续翻页');
  env = makeEnv(); RC = load(env);
  env.store['eph-ctx-sync'] = '1';
  for (let i = 1; i <= 10; i++) RC.ctxSync.report({ kind: 'pdf', file: 'a.pdf', pos: i });
  await sleep(300);
  check('停手前不发(300ms 时仍为 0)', env.posts.length === 0, JSON.stringify(env.posts));
  await sleep(1000);
  check('停手约 1s 后只发 1 次', env.posts.length === 1, '实际 ' + env.posts.length);
  check('发的是最后一次的完整状态 pos=10', env.posts[0] && env.posts[0].body.pos === 10, JSON.stringify(env.posts[0]));
  check('打到 active-reading 端点', /\/pdf\/api\/active-reading$/.test(env.posts[0].url), env.posts[0].url);
  check('开启后才挂监听(可见性+卸载)', env.listeners.length === 2, JSON.stringify(env.listeners));

  // ── 3) 单次在途:在途期间的新变化,回来后补发一次(不是并发多发) ───────────
  console.log('[3] 单次在途 + 回来补发');
  RC.ctxSync.report({ kind: 'pdf', file: 'a.pdf', pos: 11 });
  await sleep(1200);
  check('在途期间不并发第二个请求', env.posts.length === 1, '实际 ' + env.posts.length);
  env.flushInflight();
  await sleep(150);
  check('在途结束后补发最新状态', env.posts.length === 2 && env.posts[1].body.pos === 11, JSON.stringify(env.posts));

  // ── 4) 换书整份重置:上一本的 title/selection 不许粘过来 ────────────────
  console.log('[4] 换文档不粘连旧字段');
  env = makeEnv(); RC = load(env);
  env.store['eph-ctx-sync'] = '1';
  RC.ctxSync.report({ kind: 'pdf', file: 'a.pdf', pos: 3, title: '甲书', selection: '旧选区' });
  RC.ctxSync.report({ kind: 'pdf', file: 'b.pdf', pos: 1, title: '乙书' });
  await sleep(1300);
  const b = env.posts[0].body;
  check('换书后 file 是新书', b.file === 'b.pdf', JSON.stringify(b));
  check('换书后不带上一本的 selection', b.selection === undefined, JSON.stringify(b));
  check('同书内字段仍然合并', (() => {
    RC.ctxSync.report({ kind: 'pdf', file: 'b.pdf', selection: '新选区' });
    return true;
  })());
  env.flushInflight();          // 先放行上一发在途请求(否则 debounce 命中时会被单次在途挡住)
  await sleep(1300);
  const b2 = env.posts[1] && env.posts[1].body;
  check('同书补充字段时保留 pos', b2 && b2.pos === 1 && b2.selection === '新选区', JSON.stringify(b2));

  // ── 4.5) 安全护栏:没指定 Pi 源时,网页宿主不许把标题/URL 发到第三方站点 ────
  console.log('[4.5] 第三方站点护栏');
  env = makeEnv(); RC = load(env);
  env.store['eph-ctx-sync'] = '1';
  const webBlocked = RC.ctxSync.report({ kind: 'web', url: 'https://example.com/a', title: 'x' });
  await sleep(1300);
  check('未 setBase 时 web 上报被拒', webBlocked === false);
  check('未 setBase 时不发任何请求', env.posts.length === 0, JSON.stringify(env.posts));
  RC.ctxSync.setBase('https://bwicarus.taile44d0c.ts.net');
  RC.ctxSync.report({ kind: 'web', url: 'https://example.com/a', title: 'x' });
  await sleep(1300);
  check('setBase 后允许上报且打到 Pi 源', env.posts.length === 1 &&
    env.posts[0].url.indexOf('https://bwicarus.taile44d0c.ts.net') === 0, JSON.stringify(env.posts[0] || {}));
  env = makeEnv(); RC = load(env);
  env.store['eph-ctx-sync'] = '1';
  RC.ctxSync.report({ kind: 'pdf', file: 'b.pdf', pos: 1 });
  await sleep(1300);

  // ── 4.8) 无变化不上报:真机实测的 bug —— 宿主选区漏斗每次 selectionchange 都调一次,
  //         翻页会清选区 → 一路发"selection 仍是空"的即时上报,把导航合并打穿。
  console.log('[4.8] 状态没变就不发(护住导航合并)');
  env = makeEnv(); RC = load(env);
  env.store['eph-ctx-sync'] = '1';
  RC.ctxSync.report({ kind: 'pdf', file: 'a.pdf', pos: 5, selection: '' });
  await sleep(1300); env.flushInflight();
  const base = env.posts.length;
  const dup = RC.ctxSync.report({ kind: 'pdf', file: 'a.pdf', pos: 5, selection: '' }, { immediate: true });
  await sleep(600);
  check('重复的同一状态被丢弃(即使标了 immediate)', dup === false && env.posts.length === base,
    'dup=' + dup + ' posts=' + env.posts.length);
  // 模拟"连翻 6 页,每页都清一次空选区":应只合并成 1 次
  env = makeEnv(); RC = load(env);
  env.store['eph-ctx-sync'] = '1';
  for (let i = 1; i <= 6; i++) {
    RC.ctxSync.report({ kind: 'pdf', file: 'a.pdf', pos: 20 + i });
    RC.ctxSync.report({ kind: 'pdf', file: 'a.pdf', pos: 20 + i, selection: '' }, { immediate: true });
  }
  const during = env.posts.length;
  await sleep(1400);
  check('连翻 6 页(每页伴随空选区):途中 0 次、停手后 1 次',
    during === 0 && env.posts.length === 1, `途中${during} 之后${env.posts.length}`);
  check('落的是最后一页', env.posts[0] && env.posts[0].body.pos === 26, JSON.stringify(env.posts[0] || {}));
  // 选区真的变了仍要即时
  env.flushInflight(); await sleep(50);
  RC.ctxSync.report({ kind: 'pdf', file: 'a.pdf', pos: 26, selection: '真选中' }, { immediate: true });
  await sleep(400);
  check('选区内容真变化 → 即时推(不被 1s 拖住)', env.posts.length === 2, env.posts.length);

  // ── 5) 关闭:摘监听 + 清待发 + 不再发 ──────────────────────────────────
  console.log('[5] 关闭后彻底停止');
  const before = env.posts.length;
  RC.ctxSync.setEnabled(false);
  RC.ctxSync.report({ kind: 'pdf', file: 'b.pdf', pos: 99 });
  await sleep(1300);
  const afterActive = env.posts.filter((p) => /active-reading/.test(p.url)).length;
  check('关闭后不再上报活动状态', afterActive === before, before + ' → ' + afterActive);
  check('关闭时通知服务端(同一开关管两个方向)',
    env.posts.some((p) => /context-sync/.test(p.url) && p.body.enabled === false));
  check('关闭后摘掉监听器', env.listeners.length === 0, JSON.stringify(env.listeners));

  // ── 5.5) vbook 只接受与本次 POST 精确绑定的服务端真实卷页映射 ──────────
  console.log('[5.5] vbook canonical ACK 绑定与陈旧响应隔离');
  env = makeEnv(); RC = load(env);
  env.store['eph-ctx-sync'] = '1';
  env.queueResponse({
    ok: true,
    canonical: {
      kind: 'pdf',
      file: 'books/part-2.pdf',
      page: 7,
      viewFile: 'vbook:g_book',
      viewPage: 31,
    },
  });
  RC.ctxSync.report(
    { kind: 'pdf', file: 'vbook:g_book', pos: 31, selection: '' },
    { immediate: true },
  );
  await sleep(30); env.flushInflight(); await sleep(30);
  check('精确绑定 ACK 保存真实卷页',
    RC.ctxSync._state().canonical &&
    RC.ctxSync._state().canonical.file === 'books/part-2.pdf' &&
    RC.ctxSync._state().canonical.page === 7,
    JSON.stringify(RC.ctxSync._state().canonical));
  RC.ctxSync.report(
    { kind: 'pdf', file: 'vbook:g_book', pos: 31, selection: '即时选区' },
    { immediate: true },
  );
  check('同一视图页的选区变化不丢 canonical',
    RC.ctxSync._state().canonical &&
    RC.ctxSync._state().canonical.viewPage === 31,
    JSON.stringify(RC.ctxSync._state().canonical));
  env.flushInflight(); await sleep(30);

  env = makeEnv(); RC = load(env);
  env.store['eph-ctx-sync'] = '1';
  env.queueResponse({
    ok: true,
    canonical: {
      kind: 'pdf',
      file: 'books/part-2.pdf',
      page: 7,
      viewFile: 'vbook:g_book',
      viewPage: 31,
    },
  });
  RC.ctxSync.report(
    { kind: 'pdf', file: 'vbook:g_book', pos: 31 },
    { immediate: true },
  );
  await sleep(30);
  RC.ctxSync.report(
    { kind: 'pdf', file: 'vbook:g_book', pos: 32 },
    { immediate: true },
  );
  env.flushInflight(); await sleep(30);
  check('翻页后旧 ACK 不会绑定到新页',
    RC.ctxSync._state().canonical === null,
    JSON.stringify(RC.ctxSync._state().canonical));
  check('新页仍留在 pend 等自己的 ACK',
    RC.ctxSync._state().pend.pos === 32,
    JSON.stringify(RC.ctxSync._state().pend));
  RC.ctxSync.setEnabled(false);
  check('关闭同步会同步清掉 canonical',
    RC.ctxSync._state().canonical === null);

  // ── 6) 出向上下文:焦点去重/取消不复活、绘图不逐笔发 ────────────────────
  console.log('[6] 出向上下文(焦点 + 绘图版本)');
  env = makeEnv(); RC = load(env);
  check('总开关关时焦点不发', RC.ctxSync.enabled() === false && RC.outgoing.focus('text', { t: 'a' }) === false);
  check('总开关关时绘图不发', RC.outgoing.drawingTouched('a.pdf', 1) === false);
  await sleep(1200);
  check('关闭态零请求', env.posts.length === 0, JSON.stringify(env.posts));

  env.store['eph-ctx-sync'] = '1';
  check('设焦点返回 true', RC.outgoing.focus('text', { t: 'a' }) === true);
  check('同一对象重复上报被丢弃', RC.outgoing.focus('text', { t: 'a' }) === false);
  check('换对象才再发', RC.outgoing.focus('card', { cid: 'c1' }) === true);
  await sleep(80);
  var fp = env.posts.filter(function (p) { return /outgoing\/focus/.test(p.url); });
  check('焦点请求数=2(去重生效)', fp.length === 2, fp.length);
  check('第二条是卡片焦点', fp[1] && fp[1].body.kind === 'card', JSON.stringify(fp[1] || {}));
  check('未登记的 kind 不发', RC.outgoing.focus('hologram', { x: 1 }) === false);

  check('取消返回 true', RC.outgoing.cancel() === true);
  check('重复取消不再发(不发空取消)', RC.outgoing.cancel() === false);
  await sleep(60);
  var cp = env.posts.filter(function (p) { return /outgoing\/focus/.test(p.url) && p.body.cancel; });
  check('取消请求恰好 1 条', cp.length === 1, cp.length);
  check('取消后同一对象要重新 set 才发(旧焦点不复活)',
    RC.outgoing.focus('card', { cid: 'c1' }) === true);

  // 绘图:连画 8 笔只应触发一次取版本
  env = makeEnv(); RC = load(env);
  env.store['eph-ctx-sync'] = '1';
  for (var i = 0; i < 8; i++) { RC.outgoing.drawingTouched('a.pdf', 3); await sleep(60); }
  var drawDuring = env.posts.filter(function (p) { return /outgoing\/drawing/.test(p.url); }).length;
  await sleep(1300);
  var drawAfter = env.posts.filter(function (p) { return /outgoing\/drawing/.test(p.url); }).length;
  check('连画 8 笔:途中 0 次取版本', drawDuring === 0, drawDuring);
  check('停手约 1s 后只取 1 次', drawAfter === 1, drawAfter);
  check('取的是当前页', /file=a\.pdf/.test(env.posts[env.posts.length - 1].url) &&
    /page=3/.test(env.posts[env.posts.length - 1].url), env.posts[env.posts.length - 1].url);

  // ── 7) 绘图区长按焦点:不干扰笔/擦除/滚动,可取消,目标失效不设 ──────────
  console.log('[7] 绘图区长按焦点');
  let dfp = [], dcp = [];
  env = makeEnv(); RC = load(env);
  env.store['eph-ctx-sync'] = '1';
  let ctx = { file: 'a.pdf', page: 3, hasInk: true };
  const el = fakeEl();
  check('绑定成功', RC.outgoing.bindDrawingFocus(el, () => ctx) === true);
  check('同一元素不重复绑定', RC.outgoing.bindDrawingFocus(el, () => ctx) === false);
  check('监听全部注册', ['pointerdown','pointermove','pointerup','pointercancel','pointerleave']
    .every(k => (el.__h[k] || []).length > 0), Object.keys(el.__h).join(','));

  // 笔:压根不该进入长按流程
  el.fire('pointerdown', { pointerType: 'pen' });
  await sleep(650);
  let ddfp = env.posts.filter(p => /outgoing\/focus/.test(p.url));
  check('笔按下不设焦点(画笔零影响)', dfp.length === 0, dfp.length);

  // 手指长按但中途移动 = 滚页,不设焦点
  el.fire('pointerdown', {}); el.fire('pointermove', { clientX: 40, clientY: 0 });
  await sleep(650);
  dfp = env.posts.filter(p => /outgoing\/focus/.test(p.url));
  check('移动超阈值=滚页,不设焦点', dfp.length === 0, dfp.length);

  // 手指长按不动 = 设焦点
  el.fire('pointerdown', {});
  await sleep(650);
  dfp = env.posts.filter(p => /outgoing\/focus/.test(p.url));
  check('手指长按设为焦点', dfp.length === 1, dfp.length);
  check('kind=drawing', dfp[0] && dfp[0].body.kind === 'drawing', JSON.stringify(dfp[0] || {}));
  check('只带最小引用(页+revision),不带笔画',
    dfp[0] && dfp[0].body.ref.page === 3 && !('strokes' in dfp[0].body.ref),
    JSON.stringify(dfp[0] && dfp[0].body.ref));
  check('未稳定时 drawingRevision 如实为 null',
    dfp[0] && dfp[0].body.ref.drawingRevision === null, JSON.stringify(dfp[0].body.ref));

  // 再长按一次 = 取消
  el.fire('pointerdown', {});
  await sleep(650);
  dcp = env.posts.filter(p => /outgoing\/focus/.test(p.url) && p.body.cancel);
  check('再长按一次=取消焦点', dcp.length === 1, dcp.length);

  // 提前抬手 = 不触发
  const n0 = env.posts.length;
  el.fire('pointerdown', {}); el.fire('pointerup', {});
  await sleep(650);
  check('提前抬手不触发', env.posts.length === n0, env.posts.length - n0);

  // 目标失效(本页无墨迹)= 不设空焦点
  ctx = { file: 'a.pdf', page: 4, hasInk: false };
  el.fire('pointerdown', {});
  await sleep(650);
  check('本页无墨迹时不设焦点', env.posts.length === n0, env.posts.length - n0);

  // 切页丢弃:当前焦点不是绘图时不该乱发
  env = makeEnv(); RC = load(env);
  env.store['eph-ctx-sync'] = '1';
  RC.outgoing.focus('card', { cid: 'c9' });
  await sleep(60);
  check('非绘图焦点时 dropDrawingFocus 不发', RC.outgoing.dropDrawingFocus() === false);

  // ── 8) 停留(dwell)判定:连续翻页不逐页注入,停在一页 2-3s 才补一条整页上下文 ──
  //      服务端只认 reason='dwell'(或带选区)才发 page.context,所以这条计时器
  //      就是"翻页途中零注入"的唯一保证。
  console.log('[8] 停留 2.5s 才请求整页上下文');
  env = makeEnv(); RC = load(env);
  env.store['eph-ctx-sync'] = '1';
  const dwellPosts = () => env.posts.filter(p => /active-reading/.test(p.url) && p.body.reason === 'dwell');
  // 连翻 8 页,每页间隔 300ms(总 2.4s > dwell 阈值:证明是"每页重置"而非"总时长")
  for (let i = 1; i <= 8; i++) { RC.ctxSync.report({ kind: 'pdf', file: 'a.pdf', pos: 100 + i }); await sleep(300); }
  check('连续翻页途中 0 条 dwell', dwellPosts().length === 0, dwellPosts().length);
  env.flushInflight();
  await sleep(2900);
  const dw = dwellPosts();
  check('停手后恰好 1 条 dwell', dw.length === 1, dw.length);
  check('dwell 落在最后停留的那一页', dw[0] && dw[0].body.pos === 108, JSON.stringify(dw[0] && dw[0].body));
  check('dwell 带完整状态(书+kind)', dw[0] && dw[0].body.file === 'a.pdf' && dw[0].body.kind === 'pdf',
    JSON.stringify(dw[0] && dw[0].body));
  // 同页继续心跳/重复上报不应再发第二条 dwell
  env.flushInflight(); await sleep(50);
  RC.ctxSync.report({ kind: 'pdf', file: 'a.pdf', pos: 108, title: 'x' });
  await sleep(2900);
  check('同一页不重复 dwell', dwellPosts().length === 1, dwellPosts().length);
  // 换页后重新计时,再发一条
  env.flushInflight(); await sleep(50);
  RC.ctxSync.report({ kind: 'pdf', file: 'a.pdf', pos: 109 });
  await sleep(2900);
  const dw2 = dwellPosts();
  check('换页后重新计时并再发一条', dw2.length === 2 && dw2[1].body.pos === 109,
    JSON.stringify(dw2.map(p => p.body.pos)));
  // 总开关关掉后,已武装的计时器到点也不许发
  env = makeEnv(); RC = load(env);
  env.store['eph-ctx-sync'] = '1';
  RC.ctxSync.report({ kind: 'pdf', file: 'a.pdf', pos: 200 });
  await sleep(200);
  env.store['eph-ctx-sync'] = '0';
  await sleep(2900);
  check('关开关后到点的 dwell 不发',
    env.posts.filter(p => /active-reading/.test(p.url) && p.body.reason === 'dwell').length === 0,
    JSON.stringify(env.posts.map(p => p.body.reason)));

  // ── 9) 出向事件身份归一:focus/drawing 必须与 ctxSync 用同一份 canonical ──
  // 这组直接对应用户现场症状:journal 交替出现 focus(vbook 全局页) 与
  // page.context(真实卷/卷内页),Windows 把同一逻辑页当成两页 → 正文瞬时清空、选区丢失。
  console.log('[9] 出向事件身份归一(vbook → 真实卷)');

  async function bindCanonical(RC, env, view, page, real, realPage) {
    env.queueResponse({
      ok: true,
      canonical: { kind: 'pdf', file: real, page: realPage, viewFile: view, viewPage: page },
    });
    RC.ctxSync.report({ kind: 'pdf', file: view, pos: page, selection: '' }, { immediate: true });
    await sleep(30); env.flushInflight(); await sleep(30);
  }

  env = makeEnv(); RC = load(env);
  env.store['eph-ctx-sync'] = '1';
  await bindCanonical(RC, env, 'vbook:g_book', 31, 'books/part-2.pdf', 7);
  check('前置:canonical 已绑定',
    RC.ctxSync._state().canonical && RC.ctxSync._state().canonical.file === 'books/part-2.pdf');

  check('vbook 焦点发出为 true', RC.outgoing.focus('text', { file: 'vbook:g_book', page: 31, text: '选中' }) === true);
  await sleep(60);
  var f8 = env.posts.filter((p) => /outgoing\/focus/.test(p.url));
  check('焦点身份已归一到真实卷', f8.length === 1 && f8[0].body.ref.file === 'books/part-2.pdf',
    JSON.stringify(f8.map((p) => p.body.ref)));
  check('焦点页归一到卷内页', f8[0] && f8[0].body.ref.page === 7, JSON.stringify(f8[0] && f8[0].body.ref));
  check('保留视图身份供回指', f8[0] && f8[0].body.ref.viewFile === 'vbook:g_book' &&
    f8[0].body.ref.viewPage === 31, JSON.stringify(f8[0] && f8[0].body.ref));
  check('不丢原有字段', f8[0] && f8[0].body.ref.text === '选中');

  // 归一后再报同一逻辑对象:签名用归一身份,不应重复发
  check('归一后同对象仍去重',
    RC.outgoing.focus('text', { file: 'vbook:g_book', page: 31, text: '选中' }) === false);

  // fail closed:没有 canonical 时绝不拿 vbook 身份发
  env = makeEnv(); RC = load(env);
  env.store['eph-ctx-sync'] = '1';
  check('无 canonical 时 vbook 焦点不发(fail closed)',
    RC.outgoing.focus('text', { file: 'vbook:g_book', page: 31 }) === false);
  check('无 canonical 时 vbook 绘图不排队',
    RC.outgoing.drawingTouched('vbook:g_book', 31) === false);
  await sleep(1200);
  check('fail closed 时零请求', env.posts.length === 0, JSON.stringify(env.posts.map((p) => p.url)));

  // 与本次事件身份不匹配(换页)也必须 fail closed,不能凭 vbook 猜真实卷
  env = makeEnv(); RC = load(env);
  env.store['eph-ctx-sync'] = '1';
  await bindCanonical(RC, env, 'vbook:g_book', 31, 'books/part-2.pdf', 7);
  check('别页焦点不套用当前映射',
    RC.outgoing.focus('text', { file: 'vbook:g_book', page: 32 }) === false);
  check('别的合并书不套用当前映射',
    RC.outgoing.focus('text', { file: 'vbook:g_other', page: 31 }) === false);

  // 普通书与无 file 焦点不受归一影响
  env = makeEnv(); RC = load(env);
  env.store['eph-ctx-sync'] = '1';
  check('普通书焦点照常发', RC.outgoing.focus('text', { file: 'a.pdf', page: 3 }) === true);
  check('无 file 的卡片焦点照常发', RC.outgoing.focus('card', { cid: 'c1' }) === true);
  await sleep(60);
  var f8b = env.posts.filter((p) => /outgoing\/focus/.test(p.url));
  check('普通书身份原样不变', f8b[0] && f8b[0].body.ref.file === 'a.pdf' &&
    f8b[0].body.ref.viewFile === undefined, JSON.stringify(f8b[0] && f8b[0].body.ref));

  // 绘图:归一后按真实卷去问版本
  env = makeEnv(); RC = load(env);
  env.store['eph-ctx-sync'] = '1';
  await bindCanonical(RC, env, 'vbook:g_book', 31, 'books/part-2.pdf', 7);
  check('vbook 绘图排队返回 true', RC.outgoing.drawingTouched('vbook:g_book', 31) === true);
  await sleep(1200); env.flushInflight(); await sleep(60);
  var dg = env.posts.concat(env.gets || []).filter((p) => /outgoing\/drawing/.test(p.url));
  if (dg.length) {
    check('绘图查询用真实卷 rel', /books%2Fpart-2\.pdf|books\/part-2\.pdf/.test(dg[0].url), dg[0].url);
    check('绘图查询用卷内页', /page=7(&|$)/.test(dg[0].url), dg[0].url);
  } else {
    check('绘图查询已发出', false, '未捕获 outgoing/drawing 请求');
  }

  // cancel 不携带身份,不应被归一拦住;取消后旧焦点不复活
  env = makeEnv(); RC = load(env);
  env.store['eph-ctx-sync'] = '1';
  await bindCanonical(RC, env, 'vbook:g_book', 31, 'books/part-2.pdf', 7);
  RC.outgoing.focus('text', { file: 'vbook:g_book', page: 31 });
  check('取消照常发出', RC.outgoing.cancel() === true);
  check('无焦点时不发空取消', RC.outgoing.cancel() === false);
  check('取消后同一对象要重新 set',
    RC.outgoing.focus('text', { file: 'vbook:g_book', page: 31 }) === true);

  console.log(failed === 0 ? '\nALL PASS' : `\n${failed} FAILED`);
  process.exit(failed === 0 ? 0 : 1);
})();
