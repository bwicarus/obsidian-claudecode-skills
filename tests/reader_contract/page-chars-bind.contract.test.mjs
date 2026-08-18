import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const BINDCARD = read("_server_deploy/static/pdf/reader.src/34-bindcard.js");
const CHARLAYER = read("_server_deploy/static/pdf/reader.src/08-charlayer.js");
const VOICECALL = read("_server_deploy/static/pdf/rc-voicecall.js");
const CONTRACT = read("_server_deploy/reader_card_contract.py");
const BRIDGE = read("scripts/bridge_client.py");
const ENVELOPE = read("reader-specs/specs/result-envelope.md");
const AGENTS = read("reader-specs/AGENTS.md");
const READER = read("_server_deploy/static/pdf/reader.js");
const CSS = read("_server_deploy/static/pdf/pdf-styles.css");

// C15 第二版（用户 2026-08-19：「记得把 c15 做完而不是只停在第一阶段」）。
// 第一版只能把卡钉到自建页的格子块；书页正文的字符锚是缺的那一半。

test("bind 契约四处都认识 page-chars", () => {
  // 四处同步是这个仓库的硬规矩：服务端规范化、跨机信封校验、两份规范文档。
  assert.match(CONTRACT, /_BIND_KINDS = \{"upage-block", "page-chars"\}/);
  assert.match(CONTRACT, /"page-chars": \("page", "from", "to"\)/);
  assert.match(BRIDGE, /"page-chars": \("page", "from", "to"\)/);
  assert.match(ENVELOPE, /"kind": "page-chars"/);
  assert.match(AGENTS, /kind:'page-chars'/);
});

test("歪掉的字符区间被拒，而不是在页面上定出一个荒唐的位置", () => {
  // 服务端：形状不对就整条丢掉（卡片仍显示，只是退回浮层）
  assert.match(CONTRACT, /if lo < 0 or hi < lo:\s*\n\s*return None/);
  // 跨机信封：宁可报错也别悄悄少一半语义
  assert.match(BRIDGE, /bind 的字符区间不合法/);
});

test("定位靠直接验证文本，不靠 revision 号推断", () => {
  // 版本号只是"可能变了"的间接证据；把那段字取出来一比是直接证据。
  // 而且页面这一侧根本没有可靠的字符层 revision 可读。
  assert.match(BINDCARD, /function _resolveRange\(boxes, want\)/);
  assert.match(BINDCARD, /if \(got === text\) return \{ from: from, to: to \};/);
  assert.doesNotMatch(BINDCARD, /dataset\.charsRev/);
});

test("同一个词重复出现时，用原序号挑最近的一处", () => {
  // 用户明确提过这一条。只按文本找会命中第一处，那不一定是卡当初钉的地方。
  const body = BINDCARD.slice(
    BINDCARD.indexOf("function _resolveRange("),
    BINDCARD.indexOf("function _rangeRect("),
  );
  assert.ok(body.length > 0);
  assert.match(body, /joined\.indexOf\(text, at \+ 1\)/, "没有继续找下一处");
  assert.match(body, /Math\.abs\(start - from\)/, "没有用原序号做消歧");
});

test("钉在书页上的卡跟高亮同一个坐标系，不用 fixed 跟滚", () => {
  // references/sticky-notes-design.md 的禁令：锚在内容坐标系。
  assert.match(BINDCARD, /position:absolute;left:/);
  assert.doesNotMatch(BINDCARD, /position:\s*fixed/);
  assert.match(BINDCARD, /ensurePageLayer\(pw, 'pgbind-layer'\)/);
  assert.match(CSS, /\.pgbind-layer \{ position: absolute; inset: 0;/);
});

test("生命周期与自建页那条一致：可见才计时、到点收球、能拆", () => {
  assert.match(BINDCARD, /new IntersectionObserver\(/);
  assert.match(BINDCARD, /if \(collapsed \|\| !visible\) return;/);
  assert.match(BINDCARD, /document\.visibilityState === 'hidden'/);
  assert.match(BINDCARD, /el\.__bwBindTeardown = function \(\)/);
  // 计时读用户设置，跟浮层卡同口径
  assert.match(BINDCARD, /localStorage\.getItem\('rc-voice-card-secs'\)/);
  assert.match(BINDCARD, /Math\.max\(5, Math\.min\(60, v\)\)/);
  // 球是圆的，且留在原位
  assert.match(CSS, /\.pgbind-card\.pgbind-dot \{[\s\S]{0,200}?border-radius: 50%/);
});

test("钉不上退回浮层，那页渲染出来时自己归位", () => {
  assert.match(VOICECALL, /_b\.kind === 'page-chars'/);
  assert.match(VOICECALL, /window\.__pageBindCard\(_b, _pp\)/);
  assert.match(VOICECALL, /__pageBindDefer\(_pendPageBind\.bind/);
  assert.match(BINDCARD, /window\.__pageBindRetry = function \(pageNum\)/);
  // 补绑的触发点必须是**字符层就绪**，不是页面渲染完 ——
  // 卡按字符序号定位，charBoxes 没挂上之前定不出位置。
  assert.match(CHARLAYER, /window\.__pageBindRetry && window\.__pageBindRetry\(num\)/);
  const charBoxesAt = CHARLAYER.indexOf("wrap.__charBoxes = charBoxes;");
  const retryAt = CHARLAYER.indexOf("window.__pageBindRetry(num)");
  assert.ok(charBoxesAt >= 0 && retryAt > charBoxesAt, "补绑排在 charBoxes 挂上之前");
  // 补上了要关掉浮层那份，否则同一内容两处并存
  assert.match(BINDCARD, /window\.__vcCardClose\(item\.card\)/);
  assert.match(VOICECALL, /window\.__vcCardClose = function \(c\)/);
});

test("新模块真的进了拼出来的 reader.js", () => {
  // reader.js 是 reader.src/*.js 按 NN- 前缀 cat 出来的单文件。
  // 忘了重建的话，源文件改了但线上跑的还是旧的 —— 而且不会有任何报错。
  assert.match(READER, /window\.__pageBindCard = function \(bind, payload\)/);
  assert.match(READER, /window\.__pageBindRetry = function \(pageNum\)/);
});
