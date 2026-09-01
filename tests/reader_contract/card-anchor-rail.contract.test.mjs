import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const BIND = read("_server_deploy/static/pdf/reader.src/34-bindcard.js");
const NOTE = read("_server_deploy/static/pdf/rc-stickynote.js");
const CSS = read("_server_deploy/static/pdf/pdf-styles.css");

test("词锚四类颜色由正文框、序号、浮标和展开卡共用", () => {
  for (const [category, tone] of Object.entries({
    text: "#b9a8ff",
    qa: "#7dd3fc",
    image: "#34d399",
    number: "#fbbf24",
  })) {
    assert.match(BIND, new RegExp(`${category}: '${tone}'`));
    assert.match(NOTE, new RegExp(`${category}: '${tone}'`));
  }
  assert.match(BIND, /var tone = _bindTone\(payload\)/);
  assert.match(BIND, /dot\.style\.cssText = tone/);
  assert.match(NOTE, /type: tone,/);
  assert.match(CSS, /background: color-mix\(in srgb,var\(--pm-t\) 34%,rgba\(245,245,250,\.72\)\)/);
  assert.match(CSS, /color: var\(--pm-i\)/, "半透明底上的编号必须继续保持高对比");
});

test("右侧 150px 浮标轨覆盖正文且按词的屏幕中心实时对齐", () => {
  assert.match(BIND, /var _RAIL_W = 150, _DOT_MIN = 17, _DOT_MAX = 30/);
  assert.match(CSS, /\.pgbind-rail \{ position: fixed; width: 150px; pointer-events: none; z-index: 188;/);
  assert.match(CSS, /\.pgbind-rail \{[^}]*opacity:1;/,
    "父轨道整体变淡会连数字一起降对比；透明度只能留在背景");
  assert.doesNotMatch(CSS, /\.pgbind-rail \{[^}]*opacity:\s*\.\d/);
  assert.match(CSS, /\.pgbind-rail-dot \{ position: absolute;[\s\S]*?pointer-events: auto;/);
  assert.match(BIND, /document\.body\.appendChild\(rail\)/,
    "浮标轨应是 body portal，不能给正文栏增加宽度");
  assert.match(BIND, /rail\.style\.left = Math\.round\(Math\.max\(sr\.left, sr\.right - padR - _RAIL_W\)\)/);
  assert.match(BIND, /var ar = a\.getBoundingClientRect\(\);/);
  assert.match(BIND, /var cy = ar\.top \+ ar\.height \/ 2 - top;/);
  assert.match(BIND, /Math\.max\(_DOT_MIN, Math\.min\(_DOT_MAX,/);
});

test("同行多卡先以数量重叠提示，首次点击只横向展开，空白或滚动会折叠", () => {
  assert.match(BIND, /function _railGroupKey\(row\)/);
  assert.match(BIND, /dot\.__bwGroupSize > 1[\s\S]{0,180}?rail\.__expandedGroupKey = groupKey[\s\S]{0,120}?return;/,
    "组未展开时必须在调用 open 之前返回");
  assert.match(BIND, /dot\.dataset\.groupCount = String\(group\.length\)/);
  assert.match(BIND, /var step = expanded \? spreadStep : 5;/,
    "收起态只少量错位，展开态才使用完整横向间距");
  assert.match(CSS, /\.pgbind-rail-dot\.group-lead:not\(\.group-open\)::after \{ content: attr\(data-group-count\)/);
  assert.match(CSS, /\.pgbind-rail-dot\.group-lead:not\(\.group-open\) \{ border-width: 2px; \}/,
    "收起态主标必须用固定加粗框明确表示复数，不能只靠背后错位");
  assert.match(CSS, /\*\{box-sizing:border-box\}/,
    "加粗边框必须落在 border-box 内，不能让同行组收起时尺寸跳变");
  assert.match(BIND, /document\.addEventListener\('pointerdown', onOutside, true\)/);
  assert.match(BIND, /var onMove = function \(\) \{\s*(?:_primeRailScroll\(rail\);\s*)?_collapseRailGroup\(rail\);/,
    "滚动与缩放重排前必须折叠已展开组");
});

test("浮标纵向位置立即跟随滚动，只保留同行横向展开动画", () => {
  const dotRule = CSS.match(/\.pgbind-rail-dot \{([\s\S]*?)\}/)?.[1] || "";
  const viewRule = CSS.match(/\.pgbind-rail-view \{([\s\S]*?)\}/)?.[1] || "";
  assert.doesNotMatch(dotRule, /(?:^|,)\s*top\s+[.\d]/,
    "浮标 top 过渡会在滚动时持续追赶正文锚点");
  assert.doesNotMatch(viewRule, /(?:^|,)\s*top\s+[.\d]/,
    "轨道视窗 top 也必须立即跟随滚动");
  assert.match(dotRule, /right \.22s cubic-bezier\(\.34,1\.5,\.64,1\)/,
    "同行横向展开动画仍需保留");
  assert.match(dotRule, /translateY\(calc\(-50% \+ var\(--rail-y-shift,0px\)\)\)/,
    "浮标应通过继承的合成层位移在 scroll 事件内先跟上正文");
  assert.doesNotMatch(dotRule, /transform\s+\.[\d]+s/,
    "即时滚动位移不能再被 transform transition 拖成追赶动画");
  assert.match(BIND, /function _primeRailScroll\(rail\)[\s\S]*?rail\.style\.setProperty\('--rail-y-shift', shift \+ 'px'\)/,
    "scroll 热路径只能按缓存的 scrollTop 写临时位移，不能等几何重测");
  assert.match(BIND, /var onMove = function \(\) \{\s*_primeRailScroll\(rail\);[\s\S]{0,160}?_scheduleRails\(\);/,
    "临时位移必须先于 rAF 合并调度写入");
  assert.match(BIND, /document\.addEventListener\('scroll', onMove, \{ capture: true, passive: true \}\)/,
    "内部滚动容器需由 capture scroll 统一覆盖");
  assert.match(BIND, /rail\.__layoutScrollTop = Number\(scrollEl\.scrollTop \|\| 0\);[\s\S]{0,160}?--rail-y-shift', '0px'/,
    "rAF 用最新几何落位后必须原子清掉临时位移");
});

test("展开复用正式 vc-card，并 portal 到页容器之外后按视口四边 clamp", () => {
  assert.match(BIND, /RC\.voiceCard\.renderInto\(host,/);
  assert.doesNotMatch(CSS, /\.pgbind-card\s*\{/,
    "旧的第二套卡片视觉不能重新出现");
  assert.match(NOTE, /function wordPortalIn\(ctl\)/);
  assert.match(NOTE, /document\.body\.appendChild\(ctl\.root\)/);
  assert.match(NOTE, /ctl\.root\.style\.position = 'fixed'/);
  assert.match(NOTE, /wordPortalIn\(ctl\);\s*\/\/ body-fixed/);
  assert.match(NOTE, /function _clampWordCard\(ctl\)/);
  assert.match(NOTE, /if \(r\.right > vw - gap\)/);
  assert.match(NOTE, /if \(r\.bottom > vh - gap\)/);
  assert.match(NOTE, /if \(left < gap\) left = gap;/);
  assert.match(NOTE, /if \(top < gap\) top = gap;/);
  assert.match(NOTE, /var gap = 8, maxW = Math\.max\(120, vw - gap \* 2\)/,
    "只移动不能救回比视口更宽的卡，必须先限制视觉宽度");
  assert.match(NOTE, /card\.style\.setProperty\('max-width', maxW \+ 'px', 'important'\)/);
  assert.match(NOTE, /function observeWordPortal\(ctl\)/,
    "图片载入或卡片内部展开改变尺寸后也要再次 clamp");
  assert.match(NOTE, /z-index:190;pointer-events:none/);
});

test("单图词锚首次展开按图片固有比例缩进视口，手动尺寸仍优先", () => {
  assert.match(NOTE,
    /function _fitWordImageCard\(ctl, card, maxW, maxH\)[\s\S]*?card\.classList\.contains\('vc-user-sized'\)[\s\S]*?cells\.length !== 1/,
    "自动比例只处理未手调尺寸的单图卡");
  assert.match(NOTE,
    /image\.addEventListener\('load',[\s\S]*?scheduleWordPortalPlacement\(\)/,
    "首次 mount 时图片尚未解码，也必须在 intrinsic size 可用后重新排版");
  assert.match(NOTE,
    /var ratio = Number\(image\.naturalWidth\) \/ Number\(image\.naturalHeight\)[\s\S]*?targetImageW = Math\.min\(baseImageW, imageMaxW, imageMaxH \* ratio\)/,
    "宽度必须由图片固有比例和视口双边界共同决定，不能只裁父容器");
  assert.match(NOTE,
    /ctl\.body\.style\.width = targetOuterW \+ 'px'[\s\S]*?--vc-word-image-max-h/,
    "卡片外壳和图片应一起缩放，不能只给图片制造 letterbox");
  assert.match(NOTE,
    /\.vc-card\.vc-word-image-fit:not\(\.vc-user-sized\)[^']*height:auto;max-height:var\(--vc-word-image-max-h[^']*object-fit:contain/,
    "图片保持原比例且完整显示，不允许 cover 裁切或拉伸");
  assert.match(NOTE,
    /card\.classList\.contains\('vc-user-sized'\) \|\| imageFit\s*\? maxH/,
    "图像自适应和用户尺寸都使用完整视口上界，普通文字卡仍保留紧凑上界");
});

test("词锚展开态只有明确的垃圾桶删除，失败不先收卡，成功由权威删除撤 frame+rail", () => {
  assert.match(NOTE, /\.rc-note\.rc-note-word-open \.rc-note-del\{display:flex!important/);
  assert.doesNotMatch(NOTE, /rc-note-free-card-open \.rc-note-del\{display:flex!important/);
  assert.match(NOTE, /ctl\.del\.title = '删除这张卡片'/);
  assert.match(NOTE, /ctl\.del\.setAttribute\('aria-label', '删除这张卡片'\)/);
  assert.match(NOTE, /window\.confirm\(pageCard \? '删除这张卡片？' : '删除这张便签？'\)/);
  assert.match(NOTE, /deleteNote\(ctl\.note, pageCard \? '🗑 卡片已删除'/);
  assert.match(NOTE, /if \(ok !== true && ctl\.del\) ctl\.del\.disabled = false/,
    "删除失败必须保持展开态并恢复按钮");
  assert.match(NOTE, /upsertRecord\(result, generation\);\s*\n\s*if \(generation !== _generation \|\| currentNote\(id\)\)/,
    "repository 成功 tombstone 必须先进入本地投影并确认记录消失，再报告成功");
});

test("AI 直绑只有持久化 Promise 成功后才可报告 bound", () => {
  assert.match(BIND, /window\.__pageBindPersist = function \(bind, payload\)/);
  assert.match(BIND, /var placement = deferred \? \{ deferredPdfPage: g\.page \} : _bindScreenPoint\(g\)/);
  assert.match(BIND, /RC\.stickynote\.persistBoundCard\(bindOut, normalized, placement\)/);
  assert.match(BIND, /bindOut\.from = g\.range\.lo/,
    "block+text 解析出的区间必须写回再传 —— persistBoundCard 只认数字 from/to");
  assert.match(BIND, /persisted: true/);
  assert.match(BIND, /why: 'persistence-required'/,
    "旧调用方直画临时 DOM 时必须 fail closed");
  assert.match(NOTE, /function persistBoundCard\(bind, payload, screenPoint\)/);
  assert.match(NOTE, /initialLegacyReady\(generation\)\.then/,
    "首次 legacy LIST 未完成时不能让 durable create 抢跑");
  assert.match(NOTE, /return ioCreate\(fields, generation, createIdentity\)/);
  assert.match(NOTE, /stableBoundCreateIdentity\(state\.identitySeed\)/);
  assert.match(NOTE, /mutationId: 'rc-note:create:' \+ noteId \+ ':bound-v1'/,
    "回执未知及跨 WebView 重放必须复用确定的 noteId+mutationId");
  assert.match(NOTE, /var projected = upsertRecord\(note, generation\)/);
  assert.match(NOTE, /persistBoundCard: persistBoundCard/);
});

test("未渲染目标页持久化 deferred placement，已渲染页仍使用真实字符屏幕点", async () => {
  const renderedPage = {
    dataset: { loaded: "1" },
    __charsBaseW: 600,
    __charBoxes: [{ _oi: 1, c: "词", sp: false, left: 20, top: 30, width: 10, height: 12 }],
    __charLayer: {
      clientWidth: 600,
      getBoundingClientRect: () => ({ left: 100, top: 200, width: 600, height: 800 }),
    },
  };
  const calls = [];
  const sandbox = {
    console,
    Promise,
    setTimeout,
    clearTimeout,
    innerWidth: 1200,
    innerHeight: 800,
    pdfDoc: { numPages: 10 },
    document: {
      querySelector(selector) {
        return selector.includes('data-page-num="5"') ? renderedPage : null;
      },
    },
    RC: {
      stickynote: {
        persistBoundCard(bind, payload, placement) {
          calls.push({ bind, payload, placement });
          return Promise.resolve({ ok: true, noteId: `note-${calls.length}`, persisted: true });
        },
      },
    },
  };
  sandbox.window = sandbox;
  vm.runInContext(BIND, vm.createContext(sandbox), { filename: "34-bindcard.js" });

  const deferredResult = await sandbox.__pageBindPersist(
    { kind: "page-chars", page: 4, from: 8, to: 9, text: "目标" },
    { uid: "deferred-card", raw: "<b>目标</b>", isHtml: true },
  );
  assert.equal(deferredResult.ok, true);
  assert.equal(deferredResult.deferred, true);
  assert.deepEqual(structuredClone(calls[0].placement), { deferredPdfPage: 4 });

  const invalidPageResult = await sandbox.__pageBindPersist(
    { kind: "page-chars", page: 11, from: 1, to: 1, text: "越界" },
    { uid: "invalid-page", raw: "<b>越界</b>", isHtml: true },
  );
  assert.equal(invalidPageResult.ok, false);
  assert.equal(invalidPageResult.why, "bad-page");
  assert.equal(calls.length, 1, "书外页码不能被当作未渲染页持久化");

  const renderedResult = await sandbox.__pageBindPersist(
    { kind: "page-chars", page: 5, from: 1, to: 1, text: "词" },
    { uid: "rendered-card", raw: "<b>词</b>", isHtml: true },
  );
  assert.equal(renderedResult.ok, true);
  assert.equal(renderedResult.deferred, false);
  assert.deepEqual(structuredClone(calls[1].placement), { x: 125, y: 236 });
});

test("刷新缩放会重建词框与浮标；删除会同步撤标并重排编号", () => {
  const reposition = NOTE.slice(NOTE.indexOf("function repositionPortaled()"), NOTE.indexOf("function ensureMounted("));
  assert.match(reposition, /if \(ctl\._bindOpen\) _applyWordBind\(ctl\)/);
  assert.match(NOTE, /ctl\._bindKey = res\.key/);
  assert.match(BIND, /function _removeRailMark\(key\)/);
  assert.match(BIND, /function _destroyRail\(rail\)/);
  assert.match(BIND, /if \(!rails\[i\]\.querySelector\('\.pgbind-rail-dot'\)\) _destroyRail\(rails\[i\]\)/,
    "最后一个浮标删除后不能留下空轨道/view");
  assert.match(BIND, /if \(!scrollEl \|\| !scrollEl\.isConnected\) \{ _destroyRail\(rail\); return; \}/,
    "滚动根被刷新替换后不能留下 body fixed 孤儿轨道");
  assert.match(BIND, /function _removeMark\(layer, key\) \{\s*\n\s*_removeRailMark\(key\)/);
  assert.match(BIND, /rd\.textContent = ns\[i\]\.textContent/,
    "正文编号重排后右侧浮标编号也必须同步");
  assert.match(NOTE, /var _b = wordBindOf\(ctl\.note\)/);
  assert.match(NOTE, /window\.__pageBindRemove\(_b, ctl\.note\.id\)/);
});

test("AI 补绑队列走异步持久化且 inFlight/uid 双重阻止重复写", async () => {
  const sandbox = {
    console,
    Promise,
    setTimeout,
    clearTimeout,
    document: {},
  };
  sandbox.window = sandbox;
  vm.runInContext(BIND, vm.createContext(sandbox), { filename: "34-bindcard.js" });

  let resolveCreate;
  const createGate = new Promise((resolve) => { resolveCreate = resolve; });
  let persistCalls = 0;
  let transientCalls = 0;
  let closeCalls = 0;
  sandbox.__pageBindPersist = () => {
    persistCalls += 1;
    return createGate;
  };
  sandbox.__pageBindCard = () => {
    transientCalls += 1;
    return { ok: true };
  };
  sandbox.__vcCardClose = () => { closeCalls += 1; };

  const bind = { kind: "page-chars", page: 4, from: 8, to: 9, text: "词" };
  const payload = { uid: "same-card", text: "正文", raw: "<b>正文</b>" };
  const floatingCard = {};
  sandbox.__pageBindDefer(bind, payload, null);
  sandbox.__pageBindDefer(bind, payload, floatingCard);
  sandbox.__pageBindRetry(4);
  sandbox.__pageBindRetry(4);
  assert.equal(persistCalls, 1, "同 uid 重复登记与重复 retry 都只能发起一次持久化");
  assert.equal(transientCalls, 0, "无 onToggle 的 AI 项不能再走 persistence-required 的短命 DOM 入口");
  assert.equal(closeCalls, 0, "repository 未确认前浮层仍是唯一可见副本，不能关闭");

  resolveCreate({ ok: true, noteId: "note-1", persisted: true });
  await createGate;
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(closeCalls, 1, "create+upsert 成功后才关闭浮层副本");
  sandbox.__pageBindRetry(4);
  assert.equal(persistCalls, 1, "成功出队后后续 char-layer 重建不能重复写");

  // 已经持久化、带真卡宿主的项只同步重画，不能再 create HTML 便签。
  sandbox.__pageBindDefer(
    { ...bind, page: 5 },
    { uid: "manual-note", onToggle() {} },
    null,
  );
  sandbox.__pageBindRetry(5);
  assert.equal(transientCalls, 1);
  assert.equal(persistCalls, 1);
});
