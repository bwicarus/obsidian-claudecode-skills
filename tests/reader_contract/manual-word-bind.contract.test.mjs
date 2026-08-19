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
