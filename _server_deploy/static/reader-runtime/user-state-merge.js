/* user-state-merge.js — 书籍用户状态的三方合并（本地 / 远端 / 共同祖先）。
 *
 * 为什么需要它：`apply-atomically` 是**整域权威覆盖**（带 expectedLocalHeaders
 * 的乐观并发）。两台设备各改各的时，后写的那次要么被拒、要么把对方整域盖掉。
 * 跨设备同步（CKSyncEngine 把冲突的服务端记录交回来时）必须先在条目层面合完再写。
 *
 * ⚠ 这里**只做合并**：不读存储、不发网络、不认识 CloudKit。纯函数，node 里直接测，
 *   Swift 侧用 ReaderNativeBookMerge；打包前生成本模块的真实结果作对照，
 *   编译运行原生三方合并测试，防止墓碑、冲突决策与浏览器版本漂移。
 *
 * 三方语义（base = 上次同步成功时双方都认的那一版）：
 *   · 只有一边动过 → 取动过的那边。这条覆盖绝大多数真实情况。
 *   · 两边都动过 → 按域的规则逐条合：
 *       id 集合取并集；同一个 id 两边都改 → 按各自的版本字段决胜，
 *       没有版本字段就保留**本地**那份（宁可让远端那次改动落空，也不要
 *       让用户眼前的东西在同步后突然变样）。
 *   · 墓碑（highlights 的 {id, deleted:true, time}）参与决胜：删除不是"缺席"，
 *     缺席才是缺席。把删除当缺席的话，另一台设备上那条还在的记录会把它复活。
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.BWReaderRuntime = root.BWReaderRuntime || {};
  root.BWReaderRuntime.userStateMerge = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var CONTRACT = 'user-state-merge/1';

  /* 与 native-local-runtime.js 的 canonicalJSONString 同一套规则：键排序后序列化。
     两边必须一致 —— 这串字符串是"有没有变过"的唯一判据。 */
  function canonical(value) {
    if (value === null || typeof value !== 'object') return JSON.stringify(value === undefined ? null : value);
    if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
    var keys = Object.keys(value).sort();
    var parts = [];
    for (var i = 0; i < keys.length; i++) {
      if (value[keys[i]] === undefined) continue;
      parts.push(JSON.stringify(keys[i]) + ':' + canonical(value[keys[i]]));
    }
    return '{' + parts.join(',') + '}';
  }

  function same(a, b) { return canonical(a) === canonical(b); }
  function clone(value) { return value === undefined ? null : JSON.parse(JSON.stringify(value)); }
  function list(value) { return Array.isArray(value) ? value : []; }
  function num(value) { return Number.isFinite(Number(value)) ? Number(value) : 0; }

  /* 一条记录的"版本"。没有版本字段的域返回 null —— 调用方据此走"保留本地"。
     ⚠ 不要拿 JSON 长度、字段多少之类当版本：那不是单调的，合并结果会随手一改就翻转。 */
  function versionOf(item) {
    if (!item || typeof item !== 'object') return null;
    if (Number.isFinite(Number(item.rev))) return Number(item.rev);          // 便签
    if (Number.isFinite(Number(item.time))) return Number(item.time);        // 高亮（含墓碑）
    if (Number.isFinite(Number(item.updatedAt))) return Number(item.updatedAt);
    if (Number.isFinite(Number(item.ts))) return Number(item.ts);
    return null;
  }

  function keyOf(item) {
    if (!item || typeof item !== 'object') return '';
    if (typeof item.id === 'string' && item.id) return item.id;
    if (typeof item.placementId === 'string' && item.placementId) return item.placementId;
    if (typeof item.entityId === 'string' && item.entityId) return item.entityId;
    return '';
  }

  function indexBy(items) {
    var map = Object.create(null);
    list(items).forEach(function (item) {
      var key = keyOf(item);
      if (key) map[key] = item;
    });
    return map;
  }

  /* id 集合的三方合并。返回**数组**，顺序以本地为准、远端新增的追加在后 ——
     顺序不是语义（两侧渲染都按自己的规则排），但保持稳定能让 digest 少变一次。 */
  function mergeCollection(base, mine, theirs) {
    var b = indexBy(base), m = indexBy(mine), t = indexBy(theirs);
    var out = [], taken = Object.create(null);

    function decide(key) {
      var inBase = b[key], inMine = m[key], inTheirs = t[key];
      if (inMine === undefined && inTheirs === undefined) return undefined;   // 两边都删了
      // 一边没动过（与 base 相同或都不存在）→ 听另一边的，包括"另一边把它删了"。
      var mineChanged = !(inMine === undefined && inBase === undefined) && !same(inMine, inBase);
      var theirsChanged = !(inTheirs === undefined && inBase === undefined) && !same(inTheirs, inBase);
      if (!mineChanged) return inTheirs;
      if (!theirsChanged) return inMine;
      if (same(inMine, inTheirs)) return inMine;
      // 两边都改了同一条：有版本按版本，平手或无版本保留本地。
      var mv = versionOf(inMine), tv = versionOf(inTheirs);
      if (mv !== null && tv !== null && tv > mv) return inTheirs;
      if (mv === null && tv !== null && inMine === undefined) return inTheirs;
      return inMine;
    }

    function push(key) {
      if (taken[key]) return;
      taken[key] = true;
      var picked = decide(key);
      if (picked !== undefined && picked !== null) out.push(clone(picked));
    }

    list(mine).forEach(function (item) { var k = keyOf(item); if (k) push(k); });
    list(theirs).forEach(function (item) { var k = keyOf(item); if (k) push(k); });
    // base 里有、两边都没列出来的 id：两边都删了，不复活。
    return out;
  }

  /* 笔迹：{surfaceId: [stroke...]}。笔画没有 id，所以按**面**做三方合并；
     同一面两边都画过时取并集（按 canonical 去重）—— 笔迹是追加型的，
     丢掉任何一边都等于把用户画过的东西弄没了。 */
  function mergeStrokeMap(base, mine, theirs) {
    base = (base && typeof base === 'object' && !Array.isArray(base)) ? base : {};
    mine = (mine && typeof mine === 'object' && !Array.isArray(mine)) ? mine : {};
    theirs = (theirs && typeof theirs === 'object' && !Array.isArray(theirs)) ? theirs : {};
    var out = {}, keys = Object.create(null);
    [mine, theirs].forEach(function (side) {
      Object.keys(side).forEach(function (key) { keys[key] = true; });
    });
    Object.keys(keys).sort().forEach(function (key) {
      var b = list(base[key]), m = list(mine[key]), t = list(theirs[key]);
      var mineChanged = !same(m, b), theirsChanged = !same(t, b);
      if (!mineChanged && !theirsChanged) { if (m.length) out[key] = clone(m); return; }
      if (!mineChanged) { if (t.length) out[key] = clone(t); return; }
      if (!theirsChanged) { if (m.length) out[key] = clone(m); return; }
      var seen = Object.create(null), union = [];
      m.concat(t).forEach(function (stroke) {
        var sig = canonical(stroke);
        if (seen[sig]) return;
        seen[sig] = true;
        union.push(clone(stroke));
      });
      if (union.length) out[key] = union;
    });
    return out;
  }

  /* 阅读位置：单值，按时间取新。没有时间戳就保留本地 —— 把别的设备的位置
     硬塞过来，表现是"打开书跳到我没读过的地方"。 */
  function mergePosition(base, mine, theirs) {
    if (same(mine, base)) return clone(theirs);
    if (same(theirs, base)) return clone(mine);
    var mv = versionOf(mine), tv = versionOf(theirs);
    if (mv !== null && tv !== null && tv > mv) return clone(theirs);
    return clone(mine);
  }

  function mergeSided(base, mine, theirs, merge) {
    base = base || {}; mine = mine || {}; theirs = theirs || {};
    return {
      pdf: merge(base.pdf, mine.pdf, theirs.pdf),
      epub: merge(base.epub, mine.epub, theirs.epub)
    };
  }

  /** 合并一个域。name 取 USER_STATE_DOMAINS 里的名字。
   *  返回 {value, changed}：changed=false 表示结果与本地一致，可以不写回。 */
  function mergeDomain(name, base, mine, theirs) {
    var value;
    if (same(mine, theirs)) {
      value = clone(mine);
    } else if (name === 'reading-position') {
      value = mergePosition(base, mine, theirs);
    } else if (name === 'highlights') {
      value = mergeSided(base, mine, theirs, mergeCollection);
    } else if (name === 'ink' || name === 'closed-regions') {
      value = mergeSided(base, mine, theirs, mergeStrokeMap);
    } else if (name === 'notes' || name === 'user-pages' ||
               name === 'card-placements' || name === 'entity-references') {
      value = mergeCollection(base, mine, theirs);
    } else {
      // 没认出来的域不猜：保留本地，让调用方看见 unknown 并决定要不要整域取远端。
      return { value: clone(mine), changed: false, unknown: true };
    }
    return { value: value, changed: !same(value, mine), unknown: false };
  }

  /* 域是不是"空"。
   *
   * ⚠ 这是 native-local-runtime.js 里 `userStateDomainEmpty` 的**逐字副本**。
   * 为什么要副本：那份函数关在 15000 行的 IIFE 里，没有导出口，为了它把整个
   * runtime 塞进 JavaScriptCore 不合算。为什么敢用副本：
   * `tests/reader_contract/user-state-merge.contract.test.mjs` 里有一条闸门，
   * 把两边的函数体取出来逐字比 —— 那边一改，这边立刻红。
   *
   * 为什么非要它：往回写的事务里每个域都要带 `empty`，而 runtime 会用它自己的
   * 这份重算一遍来校验，对不上整笔事务就被拒（`BW_USER_STATE_DOMAIN_INVALID`）。
   */
  function domainEmpty(name, value) {
    if (name === 'highlights' || name === 'ink' || name === 'closed-regions') {
      var pdf = value && value.pdf;
      var epub = value && value.epub;
      var pdfEmpty = Array.isArray(pdf) ? !pdf.length :
        !!(pdf && typeof pdf === 'object' && !Object.keys(pdf).length);
      var epubEmpty = Array.isArray(epub) ? !epub.length :
        !!(epub && typeof epub === 'object' && !Object.keys(epub).length);
      return pdfEmpty && epubEmpty;
    }
    if (value == null || value === '') return true;
    if (Array.isArray(value)) return !value.length;
    return typeof value === 'object' && !Object.keys(value).length;
  }

  return {
    contract: CONTRACT,
    domainEmpty: domainEmpty,
    canonical: canonical,
    mergeDomain: mergeDomain,
    mergeCollection: mergeCollection,
    mergeStrokeMap: mergeStrokeMap,
    mergePosition: mergePosition
  };
});
