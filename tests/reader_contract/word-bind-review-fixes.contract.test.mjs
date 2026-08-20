import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8");

const BINDCARD = read("_server_deploy/static/pdf/reader.src/34-bindcard.js");
const NOTE = read("_server_deploy/static/pdf/rc-stickynote.js");
const VOICE = read("_server_deploy/static/pdf/rc-voicecall.js");
const CSS = read("_server_deploy/static/pdf/pdf-styles.css");
const PY = read("_server_deploy/pdf_reader.py");

// 2026-08-20 对抗式复查（六视角找 → 三镜头验伪，10/15 存活）之后的修复。
// 这些全都是「不报错、只是悄悄退回一个看起来正常的旧行为」那一类，
// 所以每条都钉住**行为**而不是某个字面量。

test("标记按卡片身份去重，不按它落在哪个词", () => {
  // 5 个独立视角都撞到这条。用区间当 key 时，同一个词上的第二张卡会把第一张的
  // 标记删掉；而第一张此时已被 _applyWordBind 设成 display:none，于是它
  // **看不见也点不开**，并且永远恢复不了（它自己每次都 ok，走不到失败兜底）。
  assert.match(BINDCARD, /var key = uid \? \('u' \+ uid\) : \('b' \+ range\.lo \+ '_' \+ range\.hi\);/);
  // 两条路都要传身份，少一条那条路就还是老行为
  assert.match(NOTE, /uid: ctl\.note\.id,/, "便签侧没传身份");
  assert.match(VOICE, /uid: card\.cid \|\| '',/, "AI 侧没传身份");
  // 撤除同理
  assert.match(BINDCARD, /window\.__pageBindRemove = function \(bind, uid\)/);
});

test("同一个词上多张卡时角标横向错开，不叠在一起", () => {
  // 不错开的话被压住的那个点不中 —— 等于身份键只修了一半
  assert.match(BINDCARD, /var dup = seen\[sig\] \|\| 0; seen\[sig\] = dup \+ 1;/);
  assert.match(BINDCARD, /dup \* \(wEst \+ 2\)/);
});

test("角标退到词的内侧，不往外挑压住邻字", () => {
  // 往外只偏 2.5px：中日文无空格排版 + 紧贴墨迹的 OCR 盒 + 低缩放，
  // 三条同时成立时白光晕会啃掉后一个字左上角的笔画。
  // 盖自己这个词的尾巴无所谓（本来就被框起来了），盖邻字才是「遮挡」。
  assert.match(BINDCARD, /var wEst = String\(ns\[i\]\.textContent \|\| '1'\)\.length \* 5\.4 \+ 1;/);
  assert.match(BINDCARD, /\(parseFloat\(ns\[i\]\.dataset\.bx\) \|\| 0\) - wEst/);
  assert.doesNotMatch(BINDCARD, /dataset\.bx\) \|\| 0\) \+ 2\.5/, "又改回往外挑了");
});

test("翻页时不闪完整浮层卡：挂载先藏，但要分清暂时失败和真失败", () => {
  // 挂载点是 04-render 的 dataset.loaded='1'，跑在 __charBoxes 挂上之前，
  // 所以第一次必然 no-char-layer。不先藏 → 每次翻页/缩放先闪一张完整的卡
  // 盖住正文，几百毫秒后才收成描边（正是用户否掉圆球时说的「遮挡视野」）。
  assert.match(NOTE, /if \(!ctl\._bindOpen && ctl\.root\.style\.display !== 'none'\) ctl\.root\.style\.display = 'none';/);
  // 先藏就必须分清，否则一张永远绑不上的卡**永久隐身**，比闪一下糟得多
  assert.match(NOTE, /var _tmp = res && \(res\.why === 'page-not-rendered' \|\| res\.why === 'no-char-layer'\);/);
  assert.match(NOTE, /if \(!_tmp\) ctl\.root\.style\.display = '';/);
});

test("同行判据用相对行高，不用固定像素", () => {
  // 固定 6px：同一行不同字号的两个词顶边差常常超过它 → 同一行被判成上下关系，
  // 编号跟阅读顺序相反。人和 AI 说的「第 3 个」从此不是同一张。
  assert.match(BINDCARD, /var rowTol = Math\.max\(6, lh \* 0\.5\);/);
  // 排序基准必须是内容坐标（被锚词顶边），不是角标自己的 style.top ——
  // 后者现在被错开逻辑改写过，拿它排序会自我打架
  assert.match(BINDCARD, /var byOf = function \(el\) \{ return parseFloat\(el\.dataset\.by \|\| el\.style\.top\) \|\| 0; \};/);
});

test("标记层压过所有装饰叠层", () => {
  // 振假名 8 / 手写 7 / 译页 9 / 插图 9 都是 pointer-events:none，
  // 只会**视觉**盖住角标 —— 但看不见的把手等于没有把手。
  assert.match(CSS, /\.pgbind-layer \{ position: absolute; inset: 0; pointer-events: none; z-index: 10; \}/);
  // 层内层级：描边 < 角标 < 展开的卡
  assert.match(CSS, /\.pgmark \{ position: absolute; z-index: 1;/);
  assert.match(CSS, /\.pgmark-n \{ position: absolute; z-index: 2;/);
  assert.match(BINDCARD, /'width:' \+ w \+ 'px;z-index:3';/);
});

test("removeAllEls 也要撤标记，否则留下点不动的孤儿", () => {
  // 它的调用方（loadAll：撤销 / AI 改便签 / 跨端 LIST 回来）**不重建 page-wrap**，
  // 所以框和数字会留在页上，还把后面的序号顶歪。
  const body = NOTE.slice(NOTE.indexOf("function removeAllEls()"));
  const end = body.indexOf("\n  function ");
  assert.ok(end > 0);
  assert.match(body.slice(0, end), /window\.__pageBindRemove\(_b0, id\)/);
});

test("侧栏开着时 AI 的补绑也要登记 —— toast 不能是空头支票", () => {
  // __pageBindDefer 原先只在 `if (!_sideOpen())` 分支里调，而失败 toast 是
  // **无条件**弹的。侧栏开着时承诺了「翻到时会自己归位」却根本没登记。
  const tail = VOICE.slice(VOICE.indexOf("var _rendered = _tcOk") - 900,
                           VOICE.indexOf("var _rendered = _tcOk"));
  assert.match(tail, /window\.__pageBindDefer\(_pendPageBind\.bind, _pendPageBind\.payload, null\)/);
  // 这一处必须在 !_sideOpen 分支**之外**（否则等于没改）
  const iSide = VOICE.indexOf("if (!_sideOpen())");
  const iFix = VOICE.indexOf("_pendPageBind.payload, null)");
  const iSideEnd = VOICE.indexOf("var _rendered = _tcOk");
  assert.ok(iFix > iSide && iFix < iSideEnd);
  assert.ok(VOICE.slice(iSide, iFix).includes("_cardPush"), "位置关系变了，重新确认这条");
});

test("插删页时 card.bind.page 跟着迁", () => {
  // 漏掉不报错：anchor 迁了、bind 留在旧页号 → 卡片去了新页，
  // 而描边和序号画在旧页号那一页（或那页没了就干脆不出现）。
  const body = PY.slice(PY.indexOf("def _pam_notes(ctx):"), PY.indexOf("def _pam_notes(ctx):") + 2200);
  assert.match(body, /b = \(\(n or \{\}\)\.get\("card"\) or \{\}\)\.get\("bind"\) or \{\}/);
  assert.match(body, /if b\.get\("kind"\) == "page-chars"/);
  // 被锚的页删了 → 撤掉词锚退回普通便签，别让它指向不存在的页
  assert.match(body, /n\["card"\]\["bind"\] = None/);
});
