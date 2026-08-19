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

test("词锚写进 card 载荷，不写进 anchor", () => {
  // anchor 在 native-local-runtime 里是**逐字段重建**的，加字段会被静默 strip
  // —— 当次会话看不出来，下次开书才发现锚退化。card 则是整块不透明存。
  assert.match(NOTE, /card: \{ cards: cards, gid: gid, cid: cid0, base_w: bw0, bind: _wb \}/);
  assert.match(RUNTIME, /anchor: normalizedNoteAnchor\(body\.anchor, code\)/,
    "anchor 仍是逐字段重建 —— 上面那条『别写进 anchor』的理由还成立");
  assert.match(RUNTIME, /card: body\.card == null \? null : boundedCanonicalJSON\(body\.card/,
    "card 不再是整块不透明存的话，bind 会被吞掉");
});

test("吸附范围跟拖动时看到的框是同一条，不能各算各的", () => {
  // 不一致的表现是「看到框、松手却钉到别处」——最难自证的一类。
  assert.match(NOTE, /_wr\.dist == null \|\| _wr\.dist <= 48/);
  assert.match(NOTE, /词中心 ≤48px/, "拖动反馈那条注释里的阈值改了就要一起改");
});

test("挂载时画标记，并把浮层收起来；绑不上则原样显示", () => {
  assert.match(NOTE, /function _applyWordBind\(ctl\)/);
  assert.match(NOTE, /window\.__pageBindCard\(b, \{/);
  assert.match(NOTE, /ctl\.root\.style\.display = ctl\._bindOpen \? '' : 'none'/);
  // 绑不上（页没渲染 / 文字层换过）时必须回到可见，否则表现是「卡片不见了」
  assert.match(NOTE, /ctl\._bindMarked = false;\s*\n\s*ctl\.root\.style\.display = '';/);
  // EPUB 没有这个全局 —— 缺它要静静回落到老浮层，不能抛
  assert.match(NOTE, /if \(!b \|\| !window\.__pageBindCard\) return;/);
});

test("点标记展开的是真卡，不是内建的纯文本框", () => {
  // 用户 2026-08-19：「打开的卡片应该是我们本来设计的卡片而不是你现在这个」
  assert.match(BINDCARD, /if \(payload && typeof payload\.onToggle === 'function'\) \{ payload\.onToggle\(\); return; \}/);
  // 出口必须在 _toggleBindCard **之前**，否则两个都会开
  const open = BINDCARD.slice(BINDCARD.indexOf("var open = function (ev)"));
  assert.ok(open.indexOf("payload.onToggle()") < open.indexOf("_toggleBindCard(layer"),
    "宿主出口排在内建浮层之后 = 两份卡同时开");
});

test("标记靠分层避开查词，不靠任何忽略名单", () => {
  const CSS = read("_server_deploy/static/pdf/pdf-styles.css");
  const SEL = read("_server_deploy/static/pdf/reader.src/13-selection.js");
  // 层 none + 子元素 auto。层若是 auto 且铺满整页 → **整页查词全死**，
  // 而症状是"点字没反应"，不会有任何报错。
  assert.match(CSS, /\.pgbind-layer \{ position: absolute; inset: 0; pointer-events: none; \}/);
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
  assert.match(BINDCARD, /window\.__pageBindRemove = function \(bind\)/);
  assert.match(BINDCARD, /_removeMark\(layer, key\);\s*\n\s*\/\/ 展开着的那张也要收掉/);
  assert.match(BINDCARD, /_renumberMarks\(pw\);\s*\n\s*return had;/, "撤掉之后必须整页重排序号");
  // 便签删除路径要真的调它，且必须在 root.remove() **之前**（之后就读不到 note 了）
  const rm = NOTE.slice(NOTE.indexOf("function removeLocal(noteId)"));
  const iCall = rm.indexOf("window.__pageBindRemove(_b)");
  const iRootRm = rm.indexOf("ctl.root.remove()");
  assert.ok(iCall >= 0 && iCall < iRootRm, "撤标记要排在便签 DOM 移除之前");
});

test("撤标记按解析后的区间找 key，不按 bind.from/to 反推", () => {
  // key 是 _resolveRange **之后**的区间。文字层换过时两者不相等，
  // 按 bind.from/to 反推会漏删 —— 表现是「删了卡，框还在」。
  const body = BINDCARD.slice(
    BINDCARD.indexOf("window.__pageBindRemove = function (bind)"),
    BINDCARD.indexOf("/// 绑不上的卡记下来"),
  );
  assert.ok(body.length > 0);
  assert.match(body, /_resolveRange\(boxes, \{/);
  assert.match(body, /key = 'b' \+ rg\.lo \+ '_' \+ rg\.hi/);
});

test("拖动已词锚的卡 = 重新锚定，不是让标记留在旧词上", () => {
  // 沿用这套的原语义（拖到哪就绑到哪），也是用户要的「手动和自动形成相同的效果」。
  // 只更新 anchor 不更新 card.bind 的话，标记留在旧词、卡片跑到别处 ——
  // 又一个"看着像正常"的错位。
  assert.match(NOTE, /function _rebindWord\(ctl, cx, cy\)/);
  // ⚠ 探测点必须是**加过 shift 的落点**，跟同一处 reanchorAt 用同一个点。
  //   r0 是撤掉 transform 后的矩形（拖动前的位置），拿它探测等于锚回原处。
  assert.match(NOTE, /_rebindWord\(g\.ctl, r0\.left \+ g\.shiftX \+ 4, r0\.top \+ g\.shiftY \+ 4\)/);
  assert.match(NOTE, /reanchorAt\(g\.ctl, r0\.left \+ g\.shiftX \+ 4, r0\.top \+ g\.shiftY \+ 4\)/,
    "两处必须同一个点；改了一处没改另一处 = anchor 和 bind 指向不同地方");
  // 换了 bind 必须跟 card 一起落库，否则重开又回到旧词
  assert.match(NOTE, /if \(_rb\) _pf\.card = g\.ctl\.note\.card;/);
  // 拖到空白/图区 → 撤词锚退回普通便签，比留个指向别处的旧 bind 好
  assert.match(NOTE, /if \(!nb\) ctl\.root\.style\.display = '';/);
  // 没真拖动（shift=0）不该重锚
  assert.match(NOTE, /if \(cx == null \|\| cy == null\) return false;/);
});

test("展开时重算尺寸 —— 卡是在 display:none 里 mount 的", () => {
  // 那时 _formW 量到的宽是 0。不补这一下，第一次展开的卡宽度是错的。
  assert.match(NOTE, /if \(ctl\._bindOpen\) \{ try \{ syncCtl\(ctl\); \} catch \(e\) \{\} \}/);
  // 靠 renderNoteCard 的 __sig 守卫避免重建卡片 DOM（重建 = 学习状态丢）。
  // 而宽度计算必须排在守卫**之前**，否则补跑等于没跑。
  const rc = NOTE.slice(NOTE.indexOf("function renderNoteCard(ctl)"));
  const iW = rc.indexOf("ctl.body.style.width = _formW(ctl, card.form)");
  const iGuard = rc.indexOf("if (box.__sig === sig) return;");
  assert.ok(iW >= 0 && iGuard > iW, "宽度计算跑到 __sig 守卫后面了，补跑 syncCtl 就白搭");
});
