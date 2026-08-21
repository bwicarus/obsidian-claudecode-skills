import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8");

const NOTE = read("_server_deploy/static/pdf/rc-stickynote.js");
const ADAPTER = read("_server_deploy/static/pdf/reader.src/27-rc-adapter.js");
const BINDCARD = read("_server_deploy/static/pdf/reader.src/34-bindcard.js");
const RUNTIME = read("_server_deploy/static/pdf/native-local-runtime.js");

// 手动把卡片拖到正文上 → 吸附到最近的**分词**，正文里显示成「词描边 + 右上角
// 序号」，点词才展开真卡。用户 2026-08-20 定的形态（动机：圆球太多挡正文）。
//
// 这条链有 4 处，少一处的表现**都是"退回老样子"而不是报错**：
//   ① 适配器要吐出词的下标区间（只吐屏幕矩形存不成锚）
//   ② 建卡时把区间写进 card 载荷
//   ③ 挂载时据此画标记并把浮层收起来
//   ④ 标记的点击要交回宿主（否则展开的是内建纯文本框，不是真卡）

test("适配器吐出的是能持久的东西：下标区间 + 文本，不只是屏幕矩形", () => {
  const i = ADAPTER.indexOf("noteWordRect:");
  const j = ADAPTER.indexOf("noteAnchorFromPoint:");
  assert.ok(i >= 0 && j > i, "noteWordRect 找不到了");
  const body = ADAPTER.slice(i, j);
  assert.match(body, /from: from, to: to, text: txt/);
  assert.match(body, /page: parseInt\(pw\.dataset\.pageNum, 10\)/);
  // ⚠ 文本必须按 _oi 排序后再拼：cbs 在 _mapCharBoxes 里被按 baseline 重排过，
  //   顺着数组拼会串行，而串出来的字**看起来仍像一个词**，比报错难查。
  assert.match(body, /seg\.sort\(\(a, b\) => \(a\._oi \| 0\) - \(b\._oi \| 0\)\)/);
});

test("手动投放保持自由卡片；AI 自动锚定仍把 bind 写进卡载荷", () => {
  const manual = NOTE.slice(
    NOTE.indexOf("function createCardAt("),
    NOTE.indexOf("function _sameWordBind("),
  );
  assert.doesNotMatch(manual, /wordBindFromPoint|bind:\s*_wb/,
    "手动拖入靠近文字也必须保持自由卡片");
  assert.match(manual, /✅ 自由卡片已放进书页/);
  const ai = NOTE.slice(
    NOTE.indexOf("function persistBoundCard("),
    NOTE.indexOf("RC.stickynote ="),
  );
  assert.match(ai, /bind:\s*cloneValue\(bind\)/,
    "AI page-chars 自动插入仍必须产生持久词锚");
  // anchor 在 native-local-runtime 里是逐字段重建的，词锚只能放 card/html 载荷。
  assert.match(RUNTIME, /anchor: normalizedNoteAnchor\(body\.anchor, code\)/,
    "anchor 仍是逐字段重建 —— 上面那条『别写进 anchor』的理由还成立");
  assert.match(RUNTIME, /card: body\.card == null \? null : boundedCanonicalJSON\(body\.card/,
    "card 不再是整块不透明存的话，bind 会被吞掉");
});

test("吸附范围跟拖动时看到的框是同一条，不能各算各的", () => {
  // 不一致的表现是「看到框、松手却钉到别处」——最难自证的一类。
  const helper = NOTE.slice(
    NOTE.indexOf("function wordBindFromPoint("),
    NOTE.indexOf("function createCardAt("),
  );
  assert.match(helper, /word\.dist != null && word\.dist > 48/);
  assert.match(NOTE, /return wordBindFromPoint\(point\.x, point\.y\);/,
    "显式 ⚓️ 的卡片附近回退必须复用同一个 48px 词锚解析器");
  assert.match(NOTE, /return wordBindFromPoint\(cx, cy\);/,
    "已锚定卡拖动重锚也必须复用同一解析器");
  assert.match(NOTE, /词中心 ≤48px/, "拖动反馈那条注释里的阈值改了就要一起改");
});

test("挂载时画标记，并把浮层收起来；绑不上则原样显示", () => {
  assert.match(NOTE, /function _applyWordBind\(ctl\)/);
  assert.match(NOTE, /window\.__pageBindCard\(b, \{/);
  assert.match(NOTE, /ctl\.root\.style\.display = ctl\._bindOpen \? '' : 'none'/);
  // 挂载时先藏 —— 不藏的话每次翻页都会先闪一张完整浮层卡盖住正文
  assert.match(NOTE, /if \(!ctl\._bindOpen && ctl\.root\.style\.display !== 'none'\) ctl\.root\.style\.display = 'none';/);
  // 但"先藏"就必须分清暂时失败和真失败，否则一张永远绑不上的卡会**永久隐身**
  assert.match(NOTE, /res\.why === 'page-not-rendered' \|\| res\.why === 'no-char-layer'/);
  assert.match(NOTE, /if \(!_tmp\) ctl\.root\.style\.display = '';/,
    "真失败时不放回来 = 卡片藏没了，比闪一下糟得多");
  // EPUB 没有这个全局 —— 缺它要静静回落到老浮层，不能抛
  assert.match(NOTE, /if \(!b \|\| !window\.__pageBindCard\) return;/);
});

test("点标记展开的是真卡，不是内建的纯文本框", () => {
  // 用户 2026-08-19：「打开的卡片应该是我们本来设计的卡片而不是你现在这个」
  assert.match(BINDCARD, /on = !!payload\.onToggle\(\{ source: source, bindKey: key, category: _bindCategory\(payload\) \}\)/);
  assert.match(NOTE, /wordPortalIn\(ctl\);\s*\/\/ body-fixed/,
    "宿主真卡必须 body-fixed portal 到页面容器之外，不能继续被 #main/page-wrap 裁切");
  assert.match(NOTE, /_placeWordCard\(ctl, meta && meta\.source\)/);
  // 宿主出口必须在兜底 _toggleBindCard **之前**，否则两个都会开
  const open = BINDCARD.slice(BINDCARD.indexOf("var open = function (ev)"));
  assert.ok(open.indexOf("payload.onToggle(") < open.indexOf("_toggleBindCard(key"),
    "宿主出口排在内建浮层之后 = 两份卡同时开");
  // 无宿主的兼容路径也必须复用正式 vc-card，旧 pgbind-card 视觉不能复活
  assert.match(BINDCARD, /RC\.voiceCard\.renderInto\(host,/);
  assert.doesNotMatch(BINDCARD, /host\.className = 'pgbind-card'/);
});

test("标记靠分层避开查词，不靠任何忽略名单", () => {
  const CSS = read("_server_deploy/static/pdf/pdf-styles.css");
  const SEL = read("_server_deploy/static/pdf/reader.src/13-selection.js");
  // 层 none + 子元素 auto。层若是 auto 且铺满整页 → **整页查词全死**，
  // 而症状是"点字没反应"，不会有任何报错。
  assert.match(CSS, /\.pgbind-layer \{ position: absolute; inset: 0; pointer-events: none; z-index: 10; \}/);
  // 层要压过振假名(8)/手写(7)/译页(9)/插图(9)：那几层都是 pointer-events:none，
  // 只会**视觉**盖住角标 —— 而看不见的把手等于没有把手。
  assert.match(CSS, /\.ruby-layer\{[^}]*z-index:8/);
  assert.match(CSS, /\.page-tr-layer\{[^}]*z-index:9/);
  assert.match(CSS, /\.pgmark \{[^}]*pointer-events: auto/);
  // 反过来：标记盖在正文上，char-layer 的手势处理必须挂在 cl 自身，
  // 挂到 pw/document 上的话标记就吃不掉事件 → 点标记同时触发查词。
  assert.match(SEL, /cl\.addEventListener\('touchstart'/);
  assert.match(SEL, /cl\.addEventListener\('touchend'/);
  // 竖滑必须能穿过标记，否则书页上多了个滑不动的洞
  assert.match(CSS, /\.pgmark \{[^}]*touch-action: pan-y/s);
});

test("标记层带 page-layer 类，去边模式下才不会错位", () => {
  const BOOT = read("_server_deploy/static/pdf/reader.src/01-boot.js");
  const CSS = read("_server_deploy/static/pdf/pdf-styles.css");
  // .fig-layer 当年就是漏了这个 → 去边时锚点跑飞、徽标被裁没
  assert.match(BOOT, /l\.className = cls \+ ' page-layer'/);
  assert.match(CSS, /\.crop-on>\.page-layer/);
  assert.match(BINDCARD, /ensurePageLayer\(pw, 'pgbind-layer'\)/);
});

test("charBoxes 就绪时必须重挂便签，否则首次开页一定退回浮层", () => {
  const CHAR = read("_server_deploy/static/pdf/reader.src/08-charlayer.js");
  const RENDER = read("_server_deploy/static/pdf/reader.src/04-render.js");
  const NOTE2 = read("_server_deploy/static/pdf/rc-stickynote.js");
  // 时序事实：便签挂载点在 dataset.loaded='1' 那一句，跑在 charBoxes 挂上**之前**。
  // 所以词锚在那一刻必然 no-char-layer → 退回老浮层，且不报任何错。
  // 表现是「钉的时候好好的，重开书变回圆球」。
  assert.match(RENDER, /wrap\.dataset\.loaded = '1';\s*\n\s*try \{ if \(window\.__uiShared && window\.RC && RC\.stickynote\) RC\.stickynote\.mountPending\(\)/);
  const iBoxes = CHAR.indexOf("wrap.__charBoxes = charBoxes;");
  const iRemount = CHAR.indexOf("RC.stickynote.repositionAll()");
  assert.ok(iBoxes >= 0 && iRemount > iBoxes, "重挂必须排在 __charBoxes 赋值之后");
  // 同一处也接 AI 那条 —— 两条路共用这个时机，别只接一条
  assert.ok(CHAR.indexOf("window.__pageBindRetry(num)") > iBoxes);
  const correctedStart = CHAR.indexOf("const d2 = await");
  const corrected = CHAR.slice(
    correctedStart,
    CHAR.indexOf("const enrichment = _nativePageOverlayEnrichment.get(num);", correctedStart),
  );
  assert.match(corrected, /window\.__pageBindRetry && window\.__pageBindRetry\(num\)/,
    "overlay 真 cv 替换字符几何后必须再次恢复词锚");
  assert.match(corrected, /RC\.stickynote\.repositionAll\(\)/,
    "overlay 真 cv 替换字符几何后必须再次挂载手动卡片");
  // repositionAll 就是 mountAll，幂等；不幂等的话这里会变成重复建 DOM
  assert.match(NOTE2, /repositionAll: mountAll,/);
});

test("退回浮层要留痕 —— 圆球是老形态，退回去看着像本来就该这样", () => {
  // silent-failure-lessons.md 第五节：最难发现的沉默不是"什么都不做"，
  // 而是"悄悄退回一个看起来完全正常的旧行为"。
  assert.match(NOTE, /console\.warn\('\[bind\] 词锚没画上，退回浮层便签'/);
  // 去重：mountAll 每次重排都会跑一遍，不去重的话一页翻下来刷屏，
  // 刷屏等于没有日志。
  assert.match(NOTE, /if \(ctl\._bindWhy !== \(res && res\.why\)\)/);
});

test("删卡要撤掉词框和序号，否则整页序号从此错位", () => {
  // 序号是**位置**不是身份，少一个就得整页重排。留在页上的话，人说的
  // 「第 3 个」和 AI 数出来的第 3 个就不是同一张了 —— 而这正是页内编号
  // 存在的理由（用户 2026-08-19：「这样就可以在沟通时说，把第三个删掉」）。
  assert.match(BINDCARD, /window\.__pageBindRemove = function \(bind, uid\)/);
  // 撤的必须是**这张卡**的标记，不能误伤同一个词上的另一张
  assert.match(BINDCARD, /var uk = uid \? \('u' \+ String\(uid\)/);
  assert.match(NOTE, /window\.__pageBindRemove\(_b, ctl\.note\.id\)/);
  assert.match(BINDCARD, /function _removeMark\(layer, key\) \{\s*\n\s*_removeRailMark\(key\)/,
    "正文标记删除时右侧浮标必须同步删除");
  assert.match(BINDCARD, /_renumberMarks\(pw\);\s*\n\s*return had;/, "撤掉之后必须整页重排序号");
  // 便签删除路径要真的调它，且必须在 root.remove() **之前**（之后就读不到 note 了）
  const rm = NOTE.slice(NOTE.indexOf("function removeLocal(noteId)"));
  const iCall = rm.indexOf("window.__pageBindRemove(_b, ctl.note.id)");
  const iRootRm = rm.indexOf("ctl.root.remove()");
  assert.ok(iCall >= 0 && iCall < iRootRm, "撤标记要排在便签 DOM 移除之前");
});

test("撤标记按解析后的区间找 key，不按 bind.from/to 反推", () => {
  // key 是 _resolveRange **之后**的区间。文字层换过时两者不相等，
  // 按 bind.from/to 反推会漏删 —— 表现是「删了卡，框还在」。
  const body = BINDCARD.slice(
    BINDCARD.indexOf("window.__pageBindRemove = function (bind, uid)"),
    BINDCARD.indexOf("/// 绑不上的卡记下来"),
  );
  assert.ok(body.length > 0);
  assert.match(body, /_resolveRange\(boxes, \{/);
  assert.match(body, /key = 'p' \+ page \+ 'b' \+ rg\.lo \+ '_' \+ rg\.hi/);
});

test("拖动仅重锚已有词锚；自由卡必须点 ⚓️ 才首次锚定", () => {
  // ⚠ 这条曾经是绿的、而功能完全不工作 —— 上一版只断言"调用点存在"，
  //   而调用点在 onResizeTLUp()（左上缩放手柄）里。卡片便签的
  //   `.rc-note-hascard .rc-note-rs-tl` 是 display:none，用户根本点不到，
  //   于是整段重锚是死代码。表现只是"拖了之后框没跟着搬"，不报任何错。
  //   所以这里钉的是**位置**，不只是存在。
  const iDrop = NOTE.indexOf("function dropNote(ctl, rect0, dx, dy)");
  const iNext = NOTE.indexOf("\n  function ", iDrop + 10);
  assert.ok(iDrop >= 0 && iNext > iDrop, "dropNote 改名了？重锚可能又挂错地方");
  const drop = NOTE.slice(iDrop, iNext);
  assert.match(drop, /_rebindWord\(ctl, _lr\.left \+ 1, _lr\.top \+ 1\)/,
    "重锚不在 dropNote 里 = 拖动时根本不会跑");
  // 探测点必须跟同一函数里的 reanchorAt 用同一个点，否则 anchor 和 bind 分家
  assert.match(drop, /reanchorAt\(ctl, _lr\.left \+ 1, _lr\.top \+ 1\)/,
    "两处探测点不一致 = anchor 和 bind 指向不同的地方");
  // 换了 bind 必须把实际 card/html 槽一起落库，否则重开又回到旧词
  assert.match(drop, /if \(_rb\) _pf\[_rb\] = ctl\.note\[_rb\];/);
  // 缩放手柄那条路**不该**再有重锚（它是死代码，留着只会让人以为已经处理了）
  const iTL = NOTE.indexOf("function onResizeTLUp()");
  const iTLEnd = NOTE.indexOf("\n  function ", iTL + 10);
  assert.doesNotMatch(NOTE.slice(iTL, iTLEnd), /_rebindWord/,
    "缩放手柄对卡片便签是 display:none，放这儿等于没放");
  // _rebindWord 本体的行为
  assert.match(NOTE, /function _rebindWord\(ctl, cx, cy\)/);
  assert.match(NOTE, /var slot = wordBindSlot\(ctl\.note\);\s*\n\s*if \(!slot\) return false;/,
    "自由卡普通拖动不得自动升级成词锚");
  assert.match(NOTE, /if \(!nb\) ctl\.root\.style\.display = '';/,
    "拖到空白/图区 → 撤词锚退回普通便签，别把卡藏没了");
  assert.match(NOTE, /if \(cx == null \|\| cy == null\) return false;/);
});

test("自由卡完全展开时显示统一线性锚图标，精确选区优先且 commit 后才切换", () => {
  assert.match(NOTE, /class="rc-note-anchor"/);
  assert.match(NOTE, /FREE_CARD_ANCHOR_ICON = '<svg[^']*stroke="currentColor"/);
  assert.match(NOTE, /aria-label="锚定到正文">' \+ FREE_CARD_ANCHOR_ICON/);
  assert.doesNotMatch(
    NOTE.slice(NOTE.indexOf("function buildCtl("), NOTE.indexOf("function buildTools(")),
    /⚓/,
    "系统 emoji 字形与现有线性按钮风格不一致",
  );
  assert.match(NOTE, /rc-note-free-card-open \.rc-note-anchor\{display:flex\}/);
  assert.match(NOTE, /form === 'full'/, "圆点/长条态不应遮进一个锚定按钮");
  assert.match(NOTE, /window\.__bwSelectionController/);
  assert.match(NOTE, /anchor\.kind !== 'pdf-char' && anchor\.kind !== 'page-chars'/);
  assert.doesNotMatch(
    NOTE.slice(NOTE.indexOf("function currentLockedPageBind("), NOTE.indexOf("function freeCardAnchorPoint(")),
    /__focusSel/,
    "焦点 chip 没有页码/字符下标，不能拿同名文字猜锚点",
  );
  const action = NOTE.slice(
    NOTE.indexOf("function anchorFreeCard("),
    NOTE.indexOf("function bindCtl("),
  );
  assert.ok(action.indexOf("patchNote(ctl.note, fields).then") < action.indexOf("_applyWordBind(live)"),
    "repository 未确认前不能先画成已锚定");
  assert.match(action, /if \(live\.portaled\) portalOut\(live\)/);
});

test("自由卡圆球非焦点尺寸跟随锁定目标的真实正文行高", () => {
  assert.match(NOTE, /function freeCardTargetLineHeight\(ctl\)/);
  assert.match(NOTE, /rect && rect\.client \? rect\.client : rect/,
    "PDF 选区 rect 的 client 包装不能被当成普通 rect");
  assert.match(NOTE, /O\.noteWordRect\(point\.x, point\.y\)/,
    "锁定选区和就近回退必须复用 host 的真实单词行高");
  assert.match(NOTE, /多行选区只探第一行/);
  assert.match(NOTE, /--rc-free-dot-size/);
  assert.match(NOTE, /rc-note-free-card-dot:not\(:focus-within\):not\(:hover\)/,
    "只有非焦点视觉圆球缩小，操作态仍保留正常尺寸");
  assert.match(NOTE, /width:max\(40px,100%\)/,
    "视觉圆面缩小后，粗指针命中区仍应至少 40px");
});

test("自由卡沿用拖到左上角删除，只有词锚卡展开后显示垃圾桶", () => {
  assert.doesNotMatch(NOTE, /rc-note-free-card-open \.rc-note-del\{display:flex!important/);
  assert.doesNotMatch(NOTE, /setWordDeleteUi\(ctl, show\)/);
  assert.match(NOTE, /rc-note-word-open \.rc-note-del\{display:flex!important/);
  assert.match(NOTE, /var pageCard = !!cardPayloadSlot\(ctl\.note\)/);
  assert.match(NOTE, /pageCard \? '删除这张卡片？' : '删除这张便签？'/);
});


test("展开时重算尺寸 —— 卡是在 display:none 里 mount 的", () => {
  // 那时 _formW 量到的宽是 0。不补这一下，第一次展开的卡宽度是错的。
  const toggle = NOTE.slice(NOTE.indexOf("onToggle: function (meta)"), NOTE.indexOf("});", NOTE.indexOf("onToggle: function (meta)")) + 3);
  assert.match(toggle, /ctl\._bindOpen = true;/);
  assert.match(toggle, /try \{ syncCtl\(ctl\); \} catch \(e\) \{\}/);
  // 靠 renderNoteCard 的 __sig 守卫避免重建卡片 DOM（重建 = 学习状态丢）。
  // 而宽度计算必须排在守卫**之前**，否则补跑等于没跑。
  const rc = NOTE.slice(NOTE.indexOf("function renderNoteCard(ctl)"));
  const iW = rc.indexOf("ctl.body.style.width = _formW(ctl, card.form)");
  const iGuard = rc.indexOf("if (box.__sig === sig) return;");
  assert.ok(iW >= 0 && iGuard > iW, "宽度计算跑到 __sig 守卫后面了，补跑 syncCtl 就白搭");
});

test("点击词锚卡外部时卡片与 frame/rail 激活态一起收起", () => {
  const outside = NOTE.slice(
    NOTE.indexOf("function onDocDown(e)"),
    NOTE.indexOf("// ─────────────────────────── 创建", NOTE.indexOf("function onDocDown(e)")),
  );
  assert.match(outside, /if \(c\._bindOpen\) \{[\s\S]*c\._bindOpen = false;[\s\S]*c\.root\.style\.display = 'none';/,
    "外点必须真正关闭并隐藏词锚卡");
  assert.match(outside, /if \(c\._bindKey\) \{[\s\S]*document\.querySelectorAll\('\[data-bindkey="' \+ c\._bindKey \+ '\"\]'\)[\s\S]*el\.classList\.remove\('on'\)/,
    "外点关闭不能留下正文框、角标或右侧浮标的打开态");
  assert.ok(outside.indexOf("classList.remove('on')") < outside.indexOf("portalOut(c)"),
    "激活态应在 portalOut 重挂 DOM 前撤掉");
});
