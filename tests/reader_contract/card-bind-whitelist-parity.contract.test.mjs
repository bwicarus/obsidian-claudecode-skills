import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");

const INBOUND = read("_server_deploy/static/pdf/rc-computer-voice.js");
const VENDOR = read("extensions/bw-reader-webext/vendor/rc-computer-voice.js");
const CONTRACT = read("_server_deploy/reader_card_contract.py");
const EXT_CALL = read("extensions/bw-reader-webext/call.js");
const CSHARP = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderRealtimeOutput.cs",
);
const MCP = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderContextMcpServer.cs",
);

// 卡片能不能带 `bind`（把卡钉在正文某一段上），这件事在链路上有**五份**
// 各自独立的字段白名单。2026-08-19 一天之内它咬了两次：
//
//   第一次：前三份（C# ValidateCard、MCP inputSchema、服务端契约）都放行了，
//           第四份 —— 阅读器侧的入站闸 rc-computer-voice.js —— 没放行，
//           于是助手照说明发出来的卡片被回 BW_READER_REALTIME_OUTPUT_SCHEMA。
//   更早：  同一天还有 _normalize_pc_page 的允许字段、MCP 的
//           additionalProperties、localFileQuery 的精确参数表 —— 全是
//           「清单类的东西改了一处，以为改好了」。
//
// 所以这里不测某一处放没放行，测的是**各份是否彼此一致**。少改一处就红。
// ⚠ 数量本身也会错：写这份测试时算的是五份，真实是六份 —— 漏掉的
//   extensions/bw-reader-webext/call.js 正是当时还在拒的那一处。
//   所以别再靠数，用 scripts/contract_sites.py 查。
//
// ⚠ 入站闸里还有一个更隐蔽的形态：那两处都是**重建**卡片对象而不是透传，
//   所以"放行"和"搬过去"是两件事。只放行不搬的表现是「校验全过、卡片照常
//   出现、就是不钉」—— 比被拒难查得多，因为链路上没有任何一处报错。
//   下面第 3、4 条专门钉这个。

test("bind 在六份卡片字段白名单里都放行", () => {
  // ① 阅读器入站闸：card 分支的可选字段
  assert.match(
    INBOUND,
    /exactObject\(\s*rawCard,\s*\["kind", "title", "data"\],\s*\["bind"\]/,
    "rc-computer-voice.js 的 card 分支必须把 bind 列为可选字段",
  );

  // ② 同文件的 normalizeResultCard —— 第二道闸，容易只改上面那道
  assert.match(
    INBOUND,
    /\["title", "brief", "sources", "bind"\]/,
    "normalizeResultCard 的可选字段也必须含 bind",
  );

  // ③ 服务端契约
  assert.match(
    CONTRACT,
    /_CARD_TOP_OPT = \([^)]*"bind"/,
    "reader_card_contract._CARD_TOP_OPT 必须含 bind",
  );

  // ④ C# 跨机信封校验：用 ExactWithOptional 而不是 Exact（后者是全等，多一个就拒）
  assert.match(
    CSHARP,
    /ExactWithOptional\(\s*card,\s*new\[\] \{ "kind", "title", "data" \},\s*new\[\] \{ "bind" \}\s*\)/,
    "ReaderRealtimeOutput.ValidateCard 必须用 ExactWithOptional 放行 bind",
  );

  // ⑤ MCP inputSchema：AI 看不见就不会传
  assert.match(
    MCP,
    /\["bind"\] = new JsonObject/,
    "BuildTypedCardArgumentsSchema 必须暴露 bind",
  );

  // ⑥ 扩展侧投递闸。写这份测试时它没被算进来 —— 前五处全放行之后，用户拿到的
  //    仍是 BW_READER_REALTIME_OUTPUT_SCHEMA，卡的就是这里。
  //    「五份」当时是我数出来的，而不是查出来的；现在登记在
  //    reader-specs/contract-sites.json，用 scripts/contract_sites.py 查。
  assert.match(
    EXT_CALL,
    /keysWithOptional\(p\.card, \["kind", "title", "data"\], \["bind"\]\)/,
    "extensions/bw-reader-webext/call.js 的 card 闸必须放行 bind",
  );
});

test("放行之后还要真的搬过去 —— 两处重建都不能把 bind 丢掉", () => {
  // card 分支重建 cardValue
  assert.match(
    INBOUND,
    /cardValue\.bind = normalizeCardBind\(rawCard\.bind\)/,
    "card 分支重建 cardValue 时必须把 bind 搬过去",
  );
  // normalizeResultCard 重建 normalized
  assert.match(
    INBOUND,
    /normalized\.bind = card\.bind/,
    "normalizeResultCard 重建 normalized 时必须把 bind 搬过去",
  );
});

test("两种 bind 形状都被校验，且区间不合法是拒收而不是静默丢弃", () => {
  assert.match(INBOUND, /function normalizeCardBind\(/);
  assert.match(INBOUND, /kind === "upage-block"/);
  assert.match(INBOUND, /kind === "page-chars"/);
  // 与其在页面上定出一个荒唐的位置，不如让调用方立刻知道发错了
  assert.match(
    INBOUND,
    /Reader 卡片 bind 的字符区间无效/,
    "page-chars 的区间校验必须存在且报错，不能悄悄放过",
  );
});

test("扩展自带的 vendor 副本与源文件同步", () => {
  // vendor 是 build.py 从源文件生成的。忘了重新生成，扩展表面就还是旧闸 ——
  // 而它的表现跟"没改过"完全一样。
  for (const probe of [
    'exactObject(rawCard, ["kind", "title", "data"], ["bind"]',
    "function normalizeCardBind(",
    "normalized.bind = card.bind",
  ]) {
    assert.ok(
      VENDOR.includes(probe),
      `vendor/rc-computer-voice.js 缺少「${probe}」—— 跑 extensions/bw-reader-webext/build.py 重新生成`,
    );
  }
});
