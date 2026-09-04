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
const VOICE = read("_server_deploy/static/pdf/rc-computer-voice.js");
const CARDS = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderCapabilities/cards.md");
const MCPSERVER = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderContextMcpServer.cs");

// C15 第二版（用户 2026-08-19：「记得把 c15 做完而不是只停在第一阶段」）。
// 第一版只能把卡钉到自建页的格子块；书页正文的字符锚是缺的那一半。

test("bind 契约四处都认识 page-chars", () => {
  // 四处同步是这个仓库的硬规矩：服务端规范化、跨机信封校验、两份规范文档。
  assert.match(CONTRACT, /_BIND_KINDS = \{"upage-block", "page-chars"\}/);
  // 2026-08-23：序号与原文**二选一**，并可带 block 把按文本找限定在某一块里。
  // 用户定的寻址方式：助手读 Markdown 时本来就看得见 [NN]，说出「第 3 块 + 这句话」
  // 零成本，而块把范围锁住 —— 同一句话在页内重复时不必再多问一轮。
  // 只有 page 是无条件必需的。
  assert.match(CONTRACT, /"page-chars": \("page",\)/);
  assert.match(BRIDGE, /"page-chars": \("page",\)/);
  assert.match(
    CONTRACT, /_BIND_OPTIONAL = \{"page-chars": \("from", "to", "text", "rev", "block"\)\}/,
    "from/to/text/rev/block 都是可选，规则由 _norm_bind 的二选一判定守住",
  );
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
  // 2026-08-23：from/to 变可选后这段包进了 if (hasRange)，且带上 how。
  // **不变量不变**：序号取出来的那段文字要跟 text 对得上，序号才作数。
  assert.match(
    BINDCARD, /if \(_stripWs\(got\) === text\)/,
    '序号仍要靠文字复核 —— 对不上就不能拿它当精确锚',
  );
  assert.match(
    BINDCARD, /how: 'exact'/,
    '走精确路径要如实标出来，好跟按文本找区分开',
  );
  assert.match(
    BINDCARD, /how = wantBlock \? 'by-text-block-missed' : 'by-text'/,
    '带了块号却退回全页必须看得见 —— 静默降级是最难查的形态',
  );
  assert.match(BINDCARD, /function _stripWs/);
  assert.doesNotMatch(BINDCARD, /dataset\.charsRev/);
});

test("块号两套编号解析端都认，且页面每一种版面都印得出 [NN]", () => {
  // 2026-09-04 实锤（用户："codex 说绑定出错不是他的问题是程序的问题"）：
  // 助手说「第 11 块」，而那一页 bk 连号只有 8 块 → search(11) 空 → 退回全页
  // 按文本找 → 没给 from 时距离恒为 0 → 钉到页内第一处同样的字上。
  // 病根是两件事，缺一个都还会复发：
  //   ① 助手看见的 [NN] 是 region.order + 1（结构化投影），解析端却只认 bk 连号；
  //   ② [NN] 当时只在分镜网格那一条路印，散文页/表格页一个都不印，而能力说明
  //      写着「正文每一行形如 [NN] …」—— 于是助手只能自己数行号。
  const body = BINDCARD.slice(
    BINDCARD.indexOf("function _regionOiFilter("),
    BINDCARD.indexOf("function _rangeRects("),
  );
  assert.ok(BINDCARD.indexOf("function _regionOiFilter(") >= 0, "缺少版面区域号解析");
  // region.ranges 索引的就是 _oi（chars 数组下标），跟 13-selection 的 __regionByOi 同一条等式
  assert.match(body, /\(region\.order \| 0\) \+ 1 !== blockNumber/,
    "区域号必须按 order + 1 比 —— 那才是正文里印出来的那个号");
  assert.match(CHARLAYER, /chars\.__layout = layout/, "版面必须挂在字符层上，解析端才拿得到");
  // 两套都试，且区域号先（有版面的页面助手读到的就是它）
  assert.match(body, /var regionFilter = _regionOiFilter\(boxes, wantBlock\);/);
  assert.ok(
    body.indexOf("search(0, regionFilter)") > 0 &&
      body.indexOf("search(0, regionFilter)") < body.indexOf("var inBlock = search(wantBlock)"),
    "区域号要先试：bk 连号在有版面的页面上是另一套编号",
  );
  // 两套都没命中 → 必须出声，不能只在回执里留一个助手才看得见的 how
  assert.match(body, /两套编号都没命中/);
  assert.match(body, /dlog\(/);
});

test("块号对不上而页内文本不唯一时宁可不钉，也不钉到第一处", () => {
  // 退回全页那条路在没给 from 时距离恒为 0 —— 第一处必胜。所以"块号对不上"加上
  // "页内多处相同文字"等于抛硬币，而钉错比钉不上更难被发现（用户就是这么撞上的）。
  const body = BINDCARD.slice(
    BINDCARD.indexOf("function _regionOiFilter("),
    BINDCARD.indexOf("function _rangeRects("),
  );
  assert.match(body, /count: hits/, "search 要如实报命中几处");
  assert.match(
    body,
    /if \(anywhere && wantBlock && !hasRange && \(anywhere\.count \| 0\) > 1\) \{/,
    "块号对不上 + 没给序号 + 不唯一 → 不钉（带了 from/to 时距离偏好是真信息，照走）",
  );
  // 唯一命中时块号对不上仍然照钉，只是回执标 by-text-block-missed
  assert.match(body, /how = wantBlock \? 'by-text-block-missed' : 'by-text'/);
});

test("[NN] 由同一个 label 函数印，四条版面路径一条都不落", () => {
  // 只补一条路是这个 bug 的原始形态。所以钉「都走同一个函数」而不是钉四处字面量。
  assert.match(VOICE, /function appendLocalRegionLabel\(builder, region\) \{/);
  assert.match(VOICE, /String\(region\.order \+ 1\)\.padStart\(2, "0"\)/);
  assert.equal(
    (VOICE.match(/appendLocalRegionLabel\(builder, (?:region|block\.region)\)/g) || []).length,
    5,
    "定义 1 处 + 四条版面路径各 1 处：散文 / 分镜网格 / 表格页非表格块 / 假表回退成文本流",
  );
  // 真数据表的单元格**不印** —— 印了就成了 `| [01] 国家 | [02] 特征 |`，把数据表毁掉
  const tableBody = VOICE.slice(
    VOICE.indexOf("function appendLocalTableLayout("),
    VOICE.indexOf("function localStructuredPageProjection("),
  );
  const cellLoop = tableBody.slice(tableBody.indexOf("cells.forEach(function (row, rowIndex)"));
  assert.doesNotMatch(cellLoop, /appendLocalRegionLabel/, "真表格单元格不该印块号");
  // 除 label 函数自己之外，不该再有第二处手拼 [NN]
  assert.equal(
    (VOICE.match(/padStart\(2, "0"\)/g) || []).length, 1,
    "块号只能有一个印法",
  );
  // 说明也要跟着改：助手照着说明数行号正是另一半病根
  assert.match(CARDS, /不要自己数行号/);
  assert.match(CARDS, /正文里没有 `\[NN\]` 就\*\*别给 `block`\*\*/);
  assert.match(CARDS, /一页\*\*只有一套\*\*块编号/, "根治后说明必须讲成一套编号");
  assert.match(CARDS, /数据表/, "数据表单元格不印号、改调 reader_page_text 要讲清");
  assert.match(CARDS, /阅读器\*\*不会\*\*替你猜/, "不唯一时不钉,说明里要讲");
  assert.match(MCPSERVER, /never "\s*\n?\s*\+ "count lines yourself/);
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

test("正文锚跟高亮同坐标；右侧轨和展开卡才是视口 portal", () => {
  // 正文框必须留在内容坐标系，缩放/刷新由 charBoxes 重建；视口轨只读它的 BCR。
  assert.match(CSS, /\.pgmark \{ position: absolute;/);
  assert.match(BINDCARD, /m\.style\.cssText =\s*\n\s*'left:' \+ \(box\.x0 - PAD\)/);
  assert.match(BINDCARD, /ensurePageLayer\(pw, 'pgbind-layer'\)/);
  assert.match(CSS, /\.pgbind-layer \{ position: absolute; inset: 0;/);
  assert.match(CSS, /\.pgbind-rail \{ position: fixed; width: 150px; pointer-events: none;/);
  assert.match(BINDCARD, /var ar = a\.getBoundingClientRect\(\);/);
  assert.match(BINDCARD, /var cy = ar\.top \+ ar\.height \/ 2 - top;/);
  assert.match(CSS, /\.pgbind-card-portal \{ position: fixed; z-index: 190;/);
});

// ── 标记的形态（2026-08-20 用户重新定，替换了初版）────────────────
//
// 初版是「落下时展开、非活跃计时、到点收成球留在锚点上」。两条都被否掉：
//   「圆球过多会遮挡视野」
//   「实际的书中字符可不像你的例子那样有足够的空白位置，这样会盖到其它字符」
// 所以标记不再占正文面积 —— 透明底分类色描边，卡片按需点开。
// 这条测试替换了原来那条「可见才计时、到点收球、能拆」。

test("标记只保留透明底分类色 2px 边框", () => {
  assert.match(CSS, /\.pgmark \{[\s\S]{0,240}?border: 2px solid var\(--pm-b\);[^}]*background: transparent;/);
  assert.doesNotMatch(CSS, /\.pgmark::before/);
  assert.doesNotMatch(CSS, /\.pgmark::after/);
  assert.match(BINDCARD, /var PAD = 2;/,
    "2px 边框的几何外扩也必须同步为 2，避免贴住字形");
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
  // 先行后列。⚠ 同行判据是**相对行高**，不是固定 6px：同一行不同字号/字形的
  //   两个词，字形顶边差常常超过 6px，固定阈值会把同一行判成上下关系，
  //   编号顺序跟阅读顺序相反 —— 而人和 AI 说的「第 3 个」就此不是同一张。
  assert.match(BINDCARD, /var rowTol = Math\.max\(6, lh \* 0\.5\);/);
  assert.match(BINDCARD, /if \(Math\.abs\(ta - tb\) > rowTol\) return ta - tb;/);
  // 排序基准是被锚词的顶边（dataset.by，内容坐标），不是角标自己的 style.top
  assert.match(BINDCARD, /parseFloat\(el\.dataset\.by \|\| el\.style\.top\)/);
});

test("卡片按需展开，且一次只开一张", () => {
  // 展开复用正式 vc-card；激活态先清全局，再只点亮当前绑定。
  assert.match(BINDCARD, /function _toggleBindCard/);
  assert.match(BINDCARD, /RC\.voiceCard\.renderInto\(host,/);
  assert.match(BINDCARD, /_clearBindActive\(\);/);
  assert.match(BINDCARD, /_setBindActive\(key, on\);/);
});
test("钉不上退回浮层，那页渲染出来时自己归位", () => {
  assert.match(VOICECALL, /_b\.kind === 'page-chars'/);
  // AI 结果必须先等 document-notes 权威仓写入与本地投影完成；只有
  // persist 明确返回 ok 才能把它当成已绑定，不能先画临时框谎报成功。
  assert.match(VOICECALL, /await window\.__pageBindPersist\(_b, _pp\)/);
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
