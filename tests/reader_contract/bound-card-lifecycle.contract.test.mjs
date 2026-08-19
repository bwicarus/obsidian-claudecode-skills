import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const VOICECALL = read("_server_deploy/static/pdf/rc-voicecall.js");
const UISHARED = read("_server_deploy/static/pdf/pdf-uishared.js");
const CONTRACT = read("_server_deploy/reader_card_contract.py");

/// 取一个函数的函数体：从 `function 名(` 到下一个同缩进的 `function`。
/// 别用 indexOf(A)→indexOf(B) 切片 —— B 定义在 A 之前时会静默返回空串，
/// 测试就变成"对空字符串做断言"，红得莫名其妙（或者更糟：绿得莫名其妙）。
const bodyOf = (source, name) => {
  const start = source.indexOf(`function ${name}(`);
  if (start < 0) return "";
  const next = source.slice(start + 1).search(/\n {2}function /);
  return next < 0 ? source.slice(start) : source.slice(start, start + 1 + next);
};

// 用户设计（2026-08-18）：「（绑定卡）落下时按普通浮动卡的非活跃计时且自动保持
// 打开，计时结束不消失而是收起为圆球」。
//
// 卡片的位置本身就是信息 —— 关掉等于把「这一处有过一次纠正」也一起丢了，
// 而球留在原位还认得出来。

test("绑定卡到点收起成球，不会消失", () => {
  // ① 契约里有 bind 字段（四处同步的那一处）
  assert.match(CONTRACT, /_CARD_TOP_OPT = \([^)]*"bind"/);

  // ② _armAuto 里有 keepAsDot 分支：到点变 dot 而不是 _cardClose
  assert.match(
    VOICECALL,
    /if \(c\.keepAsDot\) \{[^}]*_cardForm\(el, 'dot'\)/,
    "_armAuto 缺少 keepAsDot 分支",
  );

  // ③ 关键：keepAsDot 必须真的被**赋值**。
  //    2026-08-19 之前这个分支写好了却没有任何写入点，于是绑定卡照样消失 ——
  //    "机制在但没接线"是最难发现的一类缺陷：代码读起来完全正确。
  assert.match(
    VOICECALL,
    /keepAsDot: !!opts\.keepAsDot/,
    "_cardPush 没把 opts.keepAsDot 写进卡对象",
  );
  assert.match(
    VOICECALL,
    /keepAsDot: !!card\.bind/,
    "带 bind 的卡退回浮层时没有请求 keepAsDot",
  );
});

test("侧栏开着时绑定卡照常计时（它的归宿是变成球，不是消失）", () => {
  // 普通浮层卡在侧栏开着时不排计时（看不见就不该计时，关侧栏时补排）；
  // 绑定卡不同 —— 它到点是收成球留在原地，跟可见性无关。
  assert.match(VOICECALL, /if \(_sideOpen\(\) && !c\.keepAsDot\) return;/);
});

test("绑进自建页的卡有自己的展开↔球与计时，且形态跨会话保留", () => {
  // 这一条路（bind.kind === 'upage-block'）不走浮层：卡片成为自建页 blocks
  // 数组里的一个 block，屏幕位置和内容序列位置是同一件事。
  assert.match(UISHARED, /function _upBindCardForm\(/);
  assert.match(UISHARED, /if \(b\.boundTo\) _upBindCardForm\(/);
  const body = bodyOf(UISHARED, "_upBindCardForm");
  assert.ok(body.length > 0, "找不到 _upBindCardForm");
  // 计时到点收球
  assert.match(body, /setTimeout\(function \(\) \{ collapsed = true;/);
  // 形态落盘 —— 不落盘的话刷新一次球又变回展开，"这一处看过了"的信息就没了。
  // 落盘按 rec 合并：同页多张绑定卡会**同时**到点，各发一个整数组 PATCH 会互相覆盖。
  assert.match(body, /blk\.form = collapsed \? 'dot' : 'full'/);
  assert.match(body, /_upBindSaveSoon\(rec\)/);
  assert.match(UISHARED, /function _upBindSaveSoon\(rec\)[\s\S]{0,400}?RC\.reqJson\('PATCH', UP_TEXT_API/);
  // 点球展开回来
  assert.match(body, /collapsed = false; paint\(\); save\(\); arm\(\);/);
});

test("绑定卡看不见时不计时", () => {
  // 自建页的元素挂进滚动容器后**不虚拟化**，所以裸 setTimeout 会让"绑在第 12 页
  // 的卡"在你读第 3 页时自己收成球 —— 全程没被看见过。浮层那层早有这个修复
  // （_cardsVisSync：卡片被藏起来的这段时间不该计时），块这层一直没有对等物。
  const body = bodyOf(UISHARED, "_upBindCardForm");
  assert.ok(body.length > 0, "找不到 _upBindCardForm");
  assert.match(body, /new IntersectionObserver\(/);
  assert.match(body, /if \(!visible\) return;/, "arm() 没有检查可见性");
  assert.match(body, /document\.visibilityState === 'hidden'/, "切后台没有停表");
  assert.match(body, /visibilitychange/);
});

test("重渲染会拆掉上一轮的计时器，不留僵尸", () => {
  // _upRenderBlocks 每次重建 innerHTML 并重跑 _upBindCardForm。旧闭包的 timer
  // 从来没人 clear，它仍会在已脱离的元素上触发，并改**同一个** blk 对象写
  // form:'dot' —— 表现为"刚展开的卡自己又收起来了"。
  assert.match(UISHARED, /el\.__bwBindTeardown = function \(\)/);
  assert.match(
    UISHARED,
    /if \(el\.__bwBindTeardown\) el\.__bwBindTeardown\(\);/,
    "_upBindCardForm 开头没有先拆旧实例",
  );
  // 唯一销毁块 DOM 的地方 = 唯一正确的拆卸点
  const render = UISHARED.slice(UISHARED.indexOf("function _upRenderBlocks("));
  const teardown = render.indexOf("__bwBindTeardown()");
  const wipe = render.indexOf("ov.innerHTML =");
  assert.ok(teardown >= 0, "_upRenderBlocks 没有拆卸旧卡");
  assert.ok(teardown < wipe, "拆卸排在 innerHTML 清空之后就晚了 —— 那时元素已经没了");
});

test("绑定卡的计时读用户设置，跟浮层卡同一口径", () => {
  // 写死 20s 的话，用户把自动收起关掉、或改成 60s，绑定卡完全不理。
  assert.match(UISHARED, /function _upBindIdleMs\(\)/);
  assert.match(UISHARED, /localStorage\.getItem\('rc-voice-card-hide'\) === '0'/);
  assert.match(UISHARED, /localStorage\.getItem\('rc-voice-card-secs'\)/);
  // 关掉自动收起 = 永不排表
  assert.match(UISHARED, /if \(ms === null\) return;/);
  // 与浮层卡的口径必须一致（rc-voicecall 那边的钳位也是 5..60）
  assert.match(VOICECALL, /Math\.max\(5, Math\.min\(60, v\)\)/);
  assert.match(UISHARED, /Math\.max\(5, Math\.min\(60, v\)\)/);
});

test("绑不上的卡记住想去哪，那页出现时自己归位", () => {
  // 最常见的失败是"那页还没渲染"，而这种失败**会自己好** —— 只要那页出现。
  assert.match(VOICECALL, /window\.__upBindRetry = function \(upageId\)/);
  assert.match(VOICECALL, /_bindPending\.push\(_pendBind\)/);
  // 补绑成功要把浮层那份关掉，否则同一内容两处并存
  assert.match(VOICECALL, /if \(item\.card\) _cardClose\(item\.card\)/);
  // 触发点在页面渲染收尾（挂载路径有好几条，但都收敛到 _upRenderOverlay）
  assert.match(UISHARED, /function _upBindRetryFor\(rec\)/);
  assert.match(UISHARED, /_upBindRetryFor\(rec\);\s+\/\/ 这一页出现了/);
});

test("绑不上时退回浮层而不是丢卡", () => {
  // 位置信息没了还能补，内容没了就真没了。
  assert.match(VOICECALL, /if \(okBind\) return true;/);
  assert.match(VOICECALL, /那一页还没打开，卡片先放浮层，等页面出现会自己归位/);
});

// ── AI 侧：它得**看得见**这个参数才可能用 ────────────────────────────
//
// 用户 2026-08-19 反馈：「ai 说他没有看到关于绑定元素的参数所以无法把卡片绑定到
// 某个元素上」—— AI 说得完全对。C15 做了"契约四处同步"
// （reader_card_contract / bridge_client / result-envelope.md / AGENTS.md），
// 但漏了**AI 唯一真正读到的那处**：MCP 工具的 inputSchema 与 description。
// 链路上有三道都不认 bind，任何一道漏掉这个功能对 AI 就不存在。

test("MCP 卡片工具把 bind 暴露给 AI", () => {
  const mcp = readFileSync(
    new URL(
      "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderContextMcpServer.cs",
      ROOT,
    ),
    "utf8",
  );
  const schema = mcp.slice(
    mcp.indexOf("BuildTypedCardArgumentsSchema() => new()"),
    mcp.indexOf("private static JsonObject BuildExactSourceArgumentsSchema"),
  );
  assert.ok(schema.length > 0);
  // ⚠ card 是 additionalProperties:false —— 不在 properties 里就等于传不进。
  assert.match(schema, /\["bind"\] = new JsonObject/);
  assert.match(schema, /\["const"\] = "upage-block"/);
  assert.match(schema, /\["const"\] = "page-chars"/);
  // 描述里要说清楚它是干什么的，否则 AI 看得见也不知道何时用
  assert.match(mcp, /Optional `bind` pins the card to one element/);
  // 「同一个词在一页里出现好几次」这条必须讲给 AI —— 不带 text 就没法消歧
  assert.match(mcp, /the same word often\s*"\s*\+\s*"appears several times on one page/);
});

test("跨机信封放行 bind，但形状不对就拒收", () => {
  const output = readFileSync(
    new URL(
      "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderRealtimeOutput.cs",
      ROOT,
    ),
    "utf8",
  );
  // Exact 是 SetEquals（多一个字段就拒），所以可选字段要用另一个校验器 ——
  // 不是放宽 Exact：别的调用点仍然该严格。
  assert.match(output, /private static void ExactWithOptional\(/);
  assert.match(
    output,
    /ExactWithOptional\(card, new\[\] \{ "kind", "title", "data" \}, new\[\] \{ "bind" \}\)/,
  );
  assert.match(output, /private static void ValidateCardBind\(JsonElement bind\)/);
  // 两种 kind 都认
  assert.match(output, /case "upage-block":/);
  assert.match(output, /case "page-chars":/);
  // 歪掉的区间要拒 —— 与其在页面上定出荒唐位置，不如让调用方立刻知道发错了
  assert.match(output, /if \(page < 1 \|\| from < 0 \|\| to < from\)/);
  assert.match(output, /Reader 卡片 bind 类型无效/);
});

test("AI 能自己定位到正文的一段 —— 不必要求用户先选中", () => {
  // 用户 2026-08-19：「他说绑定需要我选中，我原先不是这样设计的不是么」。
  // 用户此前的原话：「选中后要求出卡应该不是常态，而是自动化操作」。
  //
  // AI 那么说不是偷懒，是对现状的准确描述：bind.page-chars 要 from/to（字符
  // 序号），而 reader_page_text 只回一段纯文本 —— 序号在那里就丢了，于是它
  // 唯一能拿到序号的途径就是用户选区。
  const assistant = readFileSync(new URL("_server_deploy/assistant.py", ROOT), "utf8");
  const runtime = readFileSync(
    new URL("_server_deploy/static/pdf/native-local-runtime.js", ROOT), "utf8");
  const mcp = readFileSync(
    new URL(
      "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderContextMcpServer.cs",
      ROOT,
    ),
    "utf8",
  );

  // ① 服务端能给出带序号的分段
  assert.match(assistant, /def _page_text_segments\(file_rel: str, page: int\)/);
  assert.match(assistant, /out\["segments"\] = _page_text_segments\(f, pg\)/);
  // 空白不成段，但**占序号** —— 序号必须与字符层真实下标一致，否则绑上去会偏
  assert.match(assistant, /空白不进正文，但\*\*保留它占的序号\*\*/);
  // 按 w 聚合：fugashi 分词生效后 w 才是有意义的词边界
  assert.match(assistant, /word = char\.get\("w", -1\)/);

  // ② runtime 请求并透传
  assert.match(runtime, /'&segments=1'/);
  assert.match(runtime, /segments: segments/);
  // 透传前要校验（序号非法就丢，不能让坏数据变成错误的绑定位置）
  assert.match(runtime, /if \(!Number\.isInteger\(from\) \|\| !Number\.isInteger\(to\) \|\|/);

  // ③ 告诉 AI 这条路存在，且**不必**要用户选中
  assert.match(mcp, /You do not need the user to\s*"\s*\+\s*"select anything first/);
  assert.match(mcp, /Binding is meant to be\s*"\s*\+\s*"\*\*automatic\*\*/);
  assert.match(mcp, /do not ask the user to\s*"\s*\+\s*"select text first/);
});
