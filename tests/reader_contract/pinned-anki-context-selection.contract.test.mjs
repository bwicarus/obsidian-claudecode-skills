import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const ROOT = fileURLToPath(new URL("../..", import.meta.url));
const STICKY_PATH = `${ROOT}/_server_deploy/static/pdf/rc-stickynote.js`;
const WEB_PINS_PATH = `${ROOT}/extensions/bw-reader-webext/src/web-pins.js`;
const VOICE_PATH = `${ROOT}/_server_deploy/static/pdf/rc-voicecall.js`;
const STICKY_SOURCE = fs.readFileSync(STICKY_PATH, "utf8");
const WEB_PINS_SOURCE = fs.readFileSync(WEB_PINS_PATH, "utf8");
const VOICE_SOURCE = fs.readFileSync(VOICE_PATH, "utf8");

function loadStickyWithVoiceSpy() {
  const calls = { pinReg: [], pinBind: [] };
  const RC = {
    voiceCard: {
      pinReg(el, gid) {
        calls.pinReg.push({ el, gid });
      },
      pinBind(el, label, textFn, spec, pressTarget) {
        calls.pinBind.push({ el, label, textFn, spec, pressTarget });
      },
    },
  };
  const window = { RC, addEventListener() {} };
  const document = {
    addEventListener() {},
    removeEventListener() {},
    visibilityState: "visible",
  };
  vm.runInNewContext(STICKY_SOURCE, {
    window,
    document,
    console,
    Promise,
    Date,
    Math,
    JSON,
    Object,
    Array,
    String,
    Number,
    Boolean,
    Error,
  }, { filename: "rc-stickynote.js" });
  return { RC, calls };
}

test("PWA 固定 Anki 卡复用 voiceCard pinBind，并以 card:<gid> 注册完整快照", () => {
  const { RC, calls } = loadStickyWithVoiceSpy();
  const pressTarget = { role: "expanded-card-body" };
  const el = {
    querySelector(selector) {
      return selector === ".vc-card-bd" ? pressTarget : null;
    },
  };
  const cards = [{
    id: 4201,
    note_id: 84201,
    entity_id: "card_ab12cd",
    entity_index: 3,
    type: "basic",
    front: "What is a lease?",
    back: "A bounded ownership grant.",
    source_ref: "note:000-sync.md",
    reason: "来自当前知识节点",
    _st: "learn",
    _showBack: false,
  }];

  const liveCards = cards.map((card) => ({ ...card }));
  assert.equal(
    RC.stickynote.bindCardSelection(
      el,
      () => liveCards,
      "anki_card_4201",
      "pwa-page-placement",
    ),
    true,
  );
  assert.deepEqual(calls.pinReg, [{ el, gid: "anki_card_4201" }]);
  assert.equal(calls.pinBind.length, 1);

  const binding = calls.pinBind[0];
  assert.equal(binding.el, el);
  assert.equal(
    binding.pressTarget,
    pressTarget,
    "整卡是 owner，只有展开正文承担长按手势",
  );
  assert.equal(binding.label, "学习卡片");
  assert.equal(binding.spec.id, "card:anki_card_4201");
  assert.equal(binding.spec.kind, "card");
  assert.equal(
    JSON.stringify(binding.spec.source),
    JSON.stringify({ cid: "anki_card_4201", gid: "anki_card_4201" }),
  );
  assert.equal(binding.spec.meta.contract, "anki-card-context/1");
  assert.equal(binding.spec.meta.host, "pwa-page-placement");
  liveCards[0]._showBack = true;
  liveCards[0]._next = { label: "3 天" };
  assert.match(binding.textFn(), /正面：What is a lease\?/);
  assert.match(binding.textFn(), /背面：A bounded ownership grant\./);
  assert.equal(
    binding.spec.meta.cards,
    liveCards,
    "长按发生时应从 getter 读取 mountState 的 live clone",
  );
  assert.equal(binding.spec.meta.cards[0].source_ref, "note:000-sync.md");
  assert.equal(binding.spec.meta.cards[0].entity_index, 3);
  assert.equal(binding.spec.meta.cards[0]._showBack, true);
  assert.deepEqual(binding.spec.meta.cards[0]._next, { label: "3 天" });
  assert.match(
    RC.stickynote.cardContextText(cards),
    /正面：What is a lease\?[\s\S]*背面：A bounded ownership grant\./,
  );
});

test("PWA 固定 HTML 工具卡使用同一 owner/正文 pressTarget，并保留稳定 cid", () => {
  const { RC, calls } = loadStickyWithVoiceSpy();
  const pressTarget = {
    textContent: "洛阳天气 23–31°C",
  };
  const owner = {
    querySelector(selector) {
      return selector === ".vc-card-bd" ? pressTarget : null;
    },
  };
  const live = {
    cid: "tool_card_luoyang",
    kind: "weather",
    label: "洛阳天气",
    content: "<strong>洛阳天气</strong><div>23–31°C</div>",
    isHtml: true,
  };

  assert.equal(
    RC.stickynote.bindHtmlCardSelection(
      owner,
      () => live,
      "tool_card_luoyang",
      "pwa-page-placement",
    ),
    true,
  );
  assert.deepEqual(calls.pinReg, [{ el: owner, gid: "tool_card_luoyang" }]);
  assert.equal(calls.pinBind.length, 1);

  const binding = calls.pinBind[0];
  assert.equal(binding.el, owner);
  assert.equal(binding.pressTarget, pressTarget);
  assert.equal(binding.spec.id, "card:tool_card_luoyang");
  assert.equal(binding.spec.kind, "card");
  assert.equal(binding.spec.meta.contract, "tool-card-context/1");
  assert.equal(binding.spec.meta.host, "pwa-page-placement");
  assert.equal(binding.textFn(), "洛阳天气 23–31°C");
  assert.equal(binding.spec.source.cid, "tool_card_luoyang");
  assert.equal(binding.spec.source.tool, "weather");
  assert.equal(binding.spec.meta.card.cid, "tool_card_luoyang");
  assert.equal(binding.spec.meta.card.content, live.content);
});

test("无 gid 或空卡片时 fail closed，不创建临时 placement 上下文编号", () => {
  const { RC, calls } = loadStickyWithVoiceSpy();
  assert.equal(RC.stickynote.bindCardSelection({}, [], "anki_card_1"), false);
  assert.equal(RC.stickynote.bindCardSelection({}, [{ front: "Q" }], ""), false);
  assert.equal(
    RC.stickynote.bindHtmlCardSelection({}, { content: "工具结果" }, ""),
    false,
  );
  assert.equal(calls.pinReg.length, 0);
  assert.equal(calls.pinBind.length, 0);
});

test("PWA Anki/HTML 正文绕过便签旧长按，卡头采用 420ms 蓄力拖动", () => {
  const bodyStart = STICKY_SOURCE.indexOf("function onBodyDown(ctl, e)");
  const bodyEnd = STICKY_SOURCE.indexOf("function onBodyLPMove", bodyStart);
  const bodyHandler = STICKY_SOURCE.slice(bodyStart, bodyEnd);
  assert.match(
    bodyHandler,
    /_isCardNote\(ctl\)[\s\S]*closest\('\.rc-note-card,\.rc-note-html'\)[\s\S]*return;/,
  );
  assert.ok(
    bodyHandler.indexOf("closest('.rc-note-card,.rc-note-html')") <
      bodyHandler.indexOf("_bd = {"),
    "学习卡和 HTML 工具卡都必须在旧便签 body 长按定时器武装前返回",
  );
  assert.match(
    STICKY_SOURCE,
    /bindCardSelection\(cardEl \|\| box\.querySelector\('\.fc-wrap'\) \|\| box, function \(\) \{[\s\S]*stateBody && stateBody\.__fc && stateBody\.__fc\.cards[\s\S]*card\.cards;[\s\S]*\}, card\.gid, 'pwa-page-placement'\)/,
  );
  assert.match(
    STICKY_SOURCE,
    /bindHtmlCardSelection\([\s\S]*el2 \|\| box\.querySelector\('\.vc-card'\) \|\| box[\s\S]*h\.cid,[\s\S]*'pwa-page-placement'/,
  );

  const handleStart = STICKY_SOURCE.indexOf("function onHandleDown(ctl, e)");
  const handleEnd = STICKY_SOURCE.indexOf("function startDrag(ctl)", handleStart);
  const handle = STICKY_SOURCE.slice(handleStart, handleEnd);
  assert.match(STICKY_SOURCE, /CARD_DRAG_HOLD_MS\s*=\s*420/);
  assert.match(STICKY_SOURCE, /CARD_DRAG_TOL\s*=\s*8/);
  assert.match(
    handle,
    /else if \(_isCardNote\(ctl\)\) \{[\s\S]*setTimeout\([\s\S]*startDrag\(ctl\)[\s\S]*CARD_DRAG_HOLD_MS/,
    "PWA 卡头必须蓄力完成后才进入拖动",
  );
  assert.doesNotMatch(
    handle,
    /else if \(_isCardNote\(ctl\)\)\s*startDrag\(ctl\)/,
    "卡片 pointerdown 不能立即进入拖动",
  );
});

test("普通网页 Anki/HTML placement 都以整卡为 owner、正文为 pressTarget", () => {
  assert.match(
    WEB_PINS_SOURCE,
    /RC\.stickynote\?\.bindCardSelection\?\.\(cardEl,\(\)=>stateBody\?\.__fc\?\.cards\|\|p\.cards\|\|\[\],p\.gid,'web-page-placement'\)/,
  );
  assert.match(
    WEB_PINS_SOURCE,
    /RC\.stickynote\?\.bindHtmlCardSelection\?\.\(cardEl,\(\)=>p\.html\|\|\{\},p\.cid,'web-page-placement'\)/,
  );
  assert.match(
    WEB_PINS_SOURCE,
    /raw:JSON\.stringify\(p\.cards\|\|\[\]\)[\s\S]*text:RC\.stickynote\?\.cardContextText\?\.\(p\.cards\|\|\[\]\)\|\|''/,
    "普通网页收藏必须保留完整 raw，并用共享正反面文本投影",
  );
  assert.match(
    STICKY_SOURCE,
    /raw: JSON\.stringify\(cards\)[\s\S]*text: cardContextText\(cards\)/,
    "PWA 收藏必须保留完整 raw，并用同一正反面文本投影",
  );
  assert.doesNotMatch(
    WEB_PINS_SOURCE,
    /card:\s*['"`]?\s*\+\s*p\.id/,
    "placement wp_* 不能成为上下文语义 id",
  );

  const bindDragStart = WEB_PINS_SOURCE.indexOf("function bindDrag");
  const mountStart = WEB_PINS_SOURCE.indexOf("function mount(p)");
  const dragHandler = WEB_PINS_SOURCE.slice(bindDragStart, mountStart);
  assert.match(WEB_PINS_SOURCE, /DRAG_HOLD_MS\s*=\s*420/);
  assert.match(WEB_PINS_SOURCE, /DRAG_SLOP\s*=\s*8/);
  assert.match(
    dragHandler,
    /RC\.voiceCard\.bindChargedDrag\(handles,\{[\s\S]*holdMs:DRAG_HOLD_MS,[\s\S]*slop:DRAG_SLOP,[\s\S]*onReady\(session,e\)\{[\s\S]*classList\.add\('bw-pin-dragging'\)/,
    "普通网页卡头/dot 必须复用共享蓄力拖动状态机，完成蓄力后才能进入拖动态",
  );
  const armAt = dragHandler.indexOf("holdMs:DRAG_HOLD_MS");
  const draggingAt = dragHandler.indexOf("classList.add('bw-pin-dragging')");
  assert.ok(armAt >= 0 && draggingAt > armAt,
    "普通网页卡必须先蓄力，再进入 bw-pin-dragging");
  const chargedStart = VOICE_SOURCE.indexOf("function _bindChargedDrag");
  const chargedEnd = VOICE_SOURCE.indexOf("function _dragToDock", chargedStart);
  const chargedDrag = VOICE_SOURCE.slice(chargedStart, chargedEnd);
  assert.ok(chargedStart >= 0 && chargedEnd > chargedStart,
    "共享卡片模块必须唯一提供 charged drag 状态机");
  assert.match(chargedDrag, /holdMs\s*=\s*opts\.holdMs\s*==\s*null\s*\?\s*420/);
  assert.match(chargedDrag, /slop\s*=\s*opts\.slop\s*==\s*null\s*\?\s*8/);
  assert.match(chargedDrag, /s\.timer\s*=\s*setTimeout\(_ready,\s*holdMs\)/);
  assert.match(
    chargedDrag,
    /if\s*\(!s\.ready\)\s*\{[\s\S]*Math\.hypot\([\s\S]*if\s*\(drift\s*>\s*slop\)\s*\{[\s\S]*clearTimeout\(s\.timer\)[\s\S]*_cancel\('slop'/,
    "共享蓄力拖动必须在定时器期间按移动容差取消，不能突然追上手指",
  );
  assert.match(
    chargedDrag,
    /function _ready\(\)[\s\S]*s\.ready\s*=\s*true[\s\S]*_call\('onReady'/,
    "只有蓄力定时器完成后才能调用 onReady",
  );
  assert.match(
    VOICE_SOURCE,
    /bindChargedDrag:\s*(?:_bindChargedDrag|function\s*\(handles,\s*opts\)\s*\{[\s\S]*return _bindChargedDrag\(handles,\s*opts\))/,
    "普通网页与 PWA 必须从 voiceCard 复用同一蓄力拖动实现",
  );
  assert.match(
    VOICE_SOURCE,
    /function _pinBind\(el, label, textFn, spec, pressTarget\)/,
    "pinBind 必须把视觉 owner 与手势 pressTarget 分开",
  );
  assert.match(
    VOICE_SOURCE,
    /pressTarget\s*=\s*pressTarget\s*\|\|\s*el[\s\S]*pressTarget\.addEventListener\('pointerdown'/,
  );
  assert.match(
    VOICE_SOURCE,
    /pinBind:\s*function\s*\(el,\s*label,\s*fn,\s*spec,\s*pressTarget\)\s*\{[\s\S]*_pinBind\(el,\s*label,\s*fn,\s*spec,\s*pressTarget\)/,
    "公开 voiceCard.pinBind 必须继续转发正文 pressTarget，不能在包装层退回整卡监听",
  );
  assert.match(
    VOICE_SOURCE,
    /function _cardPressEligible\(target\)[\s\S]*\.vc-card-rs,\.vc-card-x,button,a,input,textarea,select,[\s\S]*\[role="button"\],\.fc-dot/,
    "评分、链接、分页圆点和编辑控件必须由共享命中谓词排除",
  );
  assert.match(
    VOICE_SOURCE,
    /function _pinBind\(el, label, textFn, spec, pressTarget\)[\s\S]*if \(!_cardPressEligible\(ev\.target\)\) return;/,
    "正文长按必须使用与双击尺寸编辑相同的命中谓词",
  );
  assert.match(
    VOICE_SOURCE,
    /suppressClickUntil[\s\S]*stopImmediatePropagation/,
    "正文长按成功后必须吞掉浏览器随后合成的 click",
  );
  assert.doesNotMatch(
    STICKY_SOURCE,
    /closest\('\[data-fc\]/,
    "正面 reveal 是正文，不能再被整个 [data-fc] 围栏挡掉长按",
  );
});

test("页面评分每个状态边界都以完整快照回写 placement，pending 不得跳过", () => {
  assert.match(
    STICKY_SOURCE,
    /onPlacementStateChange = function \(snapshot, reason\) \{[\s\S]*card\.cards = cloneValue\(snapshot\);[\s\S]*patchNote\(ctl\.note, \{ card: card \}\);/,
  );
  assert.match(
    STICKY_SOURCE,
    /mountState\(bd, card\.cards, \{ bare: true, gid: card\.gid, nopin: true, onStateChange: onPlacementStateChange \}\)/,
  );
  assert.doesNotMatch(
    STICKY_SOURCE,
    /reason === 'review-pending'/,
    "PWA 未知评分结果必须以 pending fail closed，不能刷新成未答",
  );

  assert.match(
    WEB_PINS_SOURCE,
    /const onStateChange=\(snapshot,reason\)=>\{[\s\S]*p\.cards=JSON\.parse\(JSON\.stringify\(snapshot\)\)[\s\S]*persist\(\);/,
  );
  assert.match(
    WEB_PINS_SOURCE,
    /mountState\?\.\(bd,p\.cards\|\|\[\],\{gid:p\.gid,nopin:true,bare:true,onStateChange\}\)/,
  );
  assert.doesNotMatch(
    WEB_PINS_SOURCE,
    /reason==='review-pending'/,
    "扩展网页 placement 同样必须立即耐久化 pending",
  );
});
