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
  // 比对前两边都去空白：字符层的 sp 条目不进 joined，而调用方送来的锚文本
  // 常常带分隔（segments 拼接、排版空隙）。2026-08-19 就是一个空格让整条锚定
  // 失效，而链路上没有任何一处说得出为什么。
  assert.match(BINDCARD, /if \(_stripWs\(got\) === text\) return \{ lo: from, hi: to, boxes: hit \};/);
  assert.match(BINDCARD, /function _stripWs/);
  assert.doesNotMatch(BINDCARD, /dataset\.charsRev/);
});

test("同一个词重复出现时，用原序号挑最近的一处", () => {
  // 用户明确提过这一条。只按文本找会命中第一处，那不一定是卡当初钉的地方。
  const a = BINDCARD.indexOf("function _resolveRange(");
  const b = BINDCARD.indexOf("function _rangeRects(");
  // ⚠ 两个下标都必须真找到。原先只断言 `body.length > 0`，而 indexOf 找不到
  //   时返回 -1，slice(a, -1) 会切出**几乎整份文件** —— 于是"在 _resolveRange
  //   里"这个前提悄悄没了，测试还照常绿。_rangeRect → _rangeRects 改名之后
  //   它就一直是这个状态。
  assert.ok(a >= 0 && b > a, "锚点函数名对不上了，切不出 _resolveRange 的函数体");
  const body = BINDCARD.slice(a, b);
  assert.match(body, /joined\.indexOf\(text, at \+ 1\)/, "没有继续找下一处");
  // 消歧依据必须是**原序号**（_oi）跟 from 的距离。这里不钉变量名 —— 钉了
  // 就会像上一版那样，一次正当重构把它弄红，而真正的行为其实没变。
  assert.match(body, /Math\.abs\(\(ord\[index\[at\]\]\._oi \| 0\) - from\)/, "没有用原序号做消歧");
});

test("钉在书页上的卡跟高亮同一个坐标系，不用 fixed 跟滚", () => {
  // references/sticky-notes-design.md 的禁令：锚在内容坐标系。
  assert.match(BINDCARD, /position:absolute;left:/);
  assert.doesNotMatch(BINDCARD, /position:\s*fixed/);
  assert.match(BINDCARD, /ensurePageLayer\(pw, 'pgbind-layer'\)/);
  assert.match(CSS, /\.pgbind-layer \{ position: absolute; inset: 0;/);
});

// ── 标记的形态（2026-08-20 用户重新定，替换了初版）────────────────
//
// 初版是「落下时展开、非活跃计时、到点收成球留在锚点上」。两条都被否掉：
//   「圆球过多会遮挡视野」
//   「实际的书中字符可不像你的例子那样有足够的空白位置，这样会盖到其它字符」
// 所以标记不再占正文面积 —— 给被锚的词描边、中间不填色，卡片按需点开。
// 这条测试替换了原来那条「可见才计时、到点收球、能拆」。

test("标记只描边不填色 —— 字像素完全不被碰", () => {
  // 填色即便走 multiply 也仍然改了字的底色，而扫描书底色本来就不匀。
  assert.match(CSS, /\.pgmark \{[\s\S]{0,240}?background: transparent/);
  assert.match(CSS, /\.pgmark \{[\s\S]{0,240}?border: 1\.5px solid var\(--pm-b\)/);
  // 边框色不能是色调原色：实测在纸上只有 1.6~2.5:1，够不到图形元素的 3:1
  assert.match(BINDCARD, /--pm-b:color-mix\(in srgb,' \+ tc \+ ' 60%,#2a2440\)/);
});

test("跨行的词按行切框，不是一个大包围盒", () => {
  // 整体包围盒会把两行之间的整片正文都圈进去
  assert.match(BINDCARD, /function _rangeRects/);
  assert.match(BINDCARD, /Math\.abs\(base - cur\.base\) < b\.height \* 0\.6/);
});

test("序号是页内位置，加删卡后整页重排", () => {
  // 序号是位置不是身份 —— 所以每次全量重算，不维护自增计数器
  assert.match(BINDCARD, /function _renumberMarks/);
  assert.match(BINDCARD, /ns\[i\]\.textContent = String\(i \+ 1\)/);
  // 先行后列
  assert.match(BINDCARD, /if \(Math\.abs\(ta - tb\) > 6\) return ta - tb;/);
});

test("卡片按需展开，且一次只开一张", () => {
  // 满页都是卡就回到了「圆球遮挡视野」那个老问题
  assert.match(BINDCARD, /function _toggleBindCard/);
  assert.match(BINDCARD, /var others = layer\.querySelectorAll\('\.pgbind-card'\)/);
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
