import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const BOARD = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderDisplayBoard.cs");
const SERVER = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/DirectBridgeServer.cs");
const CORE = read(
  "extensions/bw-reader-webext/windows/computer-voice-desktop/bridge_core.py");
const RENDER = read(
  "extensions/bw-reader-webext/windows/computer-voice-desktop/board_card_render.py");
const LAUNCHER = read(
  "extensions/bw-reader-webext/windows/computer-voice-desktop/readerpc_launcher.py");
const GUIDE = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderCapabilities/boards.md");
const INDEX = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderCapabilities/index.md");
const WIDGET = read("ios/BWReader/Widget/BWReaderWidget.swift");
const MODEL = read("ios/BWReader/Shared/ReaderSharedSnapshot.swift");
const TOOLS = read("ios/BWReader/App/NativeReaderToolsView.swift");
const MANAGER = read("ios/BWReader/App/ReaderDisplayBoardManager.swift");
const PROJECT = read("ios/BWReader/project.yml");

// 展示板（用户 2026-09-04 要求，09-05 改成卡片版）：给「一段时间里反复看状态」的任务
// 一块方格板。这份契约钉的是那些**改一处不等于到达**的地方，以及吃过教训的纪律。
// 架构（用户 09-05 定）：源头与渲染都在 Windows，设备端只当显示器。

test("端点路径在两份副本里一致：C# 路由表 + 桥的 serve 放行清单", () => {
  // 只加路由不放行 = 端点 404 而桥看起来完全正常（CLAUDE.md「先数清楚有几份副本」）。
  assert.match(BOARD, /internal const string BoardPath = "\/reader-board\/v1";/);
  assert.match(BOARD, /internal const string CardImagePath = "\/reader-board\/card\.png";/);
  assert.match(
    SERVER,
    /ReaderDisplayBoard\.BoardPath,\s*\n\s*new\[\] \{ "POST", "OPTIONS" \},/,
    "C# 侧要注册 POST 路由");
  assert.match(
    SERVER,
    /ReaderDisplayBoard\.CardImagePath,\s*\n\s*new\[\] \{ "GET" \},/,
    "出图端点要注册 GET 路由");
  assert.match(CORE, /"\/reader-board\/v1",/,
    "bridge_core 的 DIRECT_SERVE_SIBLING_PATHS 要放行写入面");
  assert.match(CORE, /"\/reader-board\/card\.png",/,
    "bridge_core 的 DIRECT_SERVE_SIBLING_PATHS 要放行出图面");
});

test("一个端点一个 op：板子级 + 卡片级都在，且拒绝未知 op", () => {
  for (const op of [
    "register", "update", "section", "card", "cards", "cardDelete",
    "clear", "delete", "enable", "get", "list",
  ]) {
    assert.match(BOARD, new RegExp(`case "${op}":`), `缺 op: ${op}`);
  }
  assert.match(BOARD, /"BW_BOARD_UNKNOWN_OP"/);
  assert.match(BOARD, /"BW_BOARD_UNKNOWN_CARD"/);
  // register 对同一 slug 幂等 —— 固定程序每次跑都能拿回同一个 code，不必自己存
  assert.match(BOARD, /board\["slug"\]\?\.GetValue<string>\(\) == slug/);
  assert.match(BOARD, /\["created"\] = false/);
  // 同 id 再发一次 = 整张替换（反复刷同一张卡是常态用法）
  const opCard = BOARD.slice(
    BOARD.indexOf("private static JsonObject OpCard("),
    BOARD.indexOf("private static JsonObject OpCards("));
  assert.match(opCard, /cards\[at\] = card;/);
  assert.match(opCard, /\["replaced"\] = at >= 0/);
});

test("每个尺寸都有上限，且超了是拒绝而不是悄悄截断", () => {
  for (const limit of [
    "MaxBoards", "MaxCards", "MaxCardHtmlChars", "MaxCardIdChars", "MaxCardAltChars",
    "MaxTitleChars", "MaxSlugChars", "MaxNoteChars", "MaxStoreBytes",
  ]) {
    assert.match(BOARD, new RegExp(`private const \\w+ ${limit} =`), limit);
  }
  assert.match(BOARD, /"BW_BOARD_FIELD_INVALID"[\s\S]{0,200}超过/);
  assert.match(BOARD, /"BW_BOARD_LIMIT"/);
});

test("AI 写的 HTML 入库先洗：整段扔掉危险标签、去事件、去外链", () => {
  // 渲染端已经断网断脚本，但同一段 HTML 还会进 App 的 WebView —— 不能只靠那一层。
  const sanitize = BOARD.slice(
    BOARD.indexOf("private static readonly string[] ForbiddenTags"),
    BOARD.indexOf("private static string NewCardId("));
  for (const tag of ["script", "iframe", "object", "embed", "form", "svg", "link", "meta"]) {
    assert.match(sanitize, new RegExp(`"${tag}"`), `禁用标签缺 ${tag}`);
  }
  assert.match(sanitize, /son\[a-zA-Z\]\+/, "on* 事件属性要去掉");
  assert.match(sanitize, /\(src\|href\|xlink:href\|background\|poster\)/, "外链属性要去掉");
  assert.match(sanitize, /"javascript:", "blocked:"/);
  assert.match(sanitize, /"@import", "blocked-import"/);
  // 洗空了就拒，别存一张空卡
  assert.match(BOARD, /html 洗掉之后是空的/);
});

test("渲染在 Windows：断网断脚本、按内容 sha 命名、原子落盘、子进程", () => {
  assert.match(RENDER, /java_script_enabled=False/);
  assert.match(RENDER, /offline=True/);
  assert.match(RENDER, /context\.route\("\*\*\/\*", lambda route: route\.abort\(\)\)/,
    "路由级拦截是断网的第二道保险");
  assert.match(RENDER, /os\.replace\(temporary, card_png_path\(sha, root\)\)/,
    "半张 PNG 会被消费端当成'渲好了'，必须原子改名");
  // sha 两边同一个算法：sha256 前 8 字节。不一致的表现是"永远有待渲染的卡"
  assert.match(RENDER, /hashlib\.sha256\(html\.encode\("utf-8"\)\)\.digest\(\)[\s\S]{0,40}\[:8\]\.hex\(\)/);
  assert.match(BOARD, /SHA256\.HashData\([\s\S]{0,80}Convert\.ToHexString\(digest, 0, 8\)/);
  // ReaderPC 走子进程，不在 exe 里 import playwright（打包边界之外，硬 import = 起不来）
  assert.doesNotMatch(LAUNCHER, /^import board_card_render/m);
  assert.match(LAUNCHER, /def run_board_card_render\(/);
  assert.match(LAUNCHER, /board_card_render\.py/);
  // 只在存储真的变过时才起浏览器；渲染不许在 Tk 主循环里做
  assert.match(LAUNCHER, /_board_render_seen/);
  assert.match(LAUNCHER, /name="readerpc-board-cards"/);
});

test("出图端点内容寻址、长缓存，没渲好回 404 而不是占位图", () => {
  const image = BOARD.slice(
    BOARD.indexOf("internal static async Task WriteCardImageAsync("),
    BOARD.indexOf("internal static async Task WriteResponseAsync("));
  assert.match(image, /sha\.Length != 16/);
  assert.match(image, /Status404NotFound/);
  assert.match(image, /public, max-age=31536000, immutable/);
  assert.doesNotMatch(image, /placeholder|占位/i, "占位图会被长缓存成'这张卡永远是灰的'");
});

test("自动消除在每次加载时结算（卡片按卡过期），本地墙钟", () => {
  assert.match(BOARD, /private static bool ApplyAutoClear\(/);
  const auto = BOARD.slice(
    BOARD.indexOf("private static bool ApplyAutoClear("),
    BOARD.indexOf("private static long MostRecentLocalBoundaryMs("));
  assert.match(auto, /board\["cards"\] is JsonArray cardList/, "afterHours 要按卡过期");
  assert.match(auto, /dailyCards\.Clear\(\)/, "dailyAtLocal 要清卡");
  const execute = BOARD.slice(
    BOARD.indexOf("internal static JsonObject Execute("),
    BOARD.indexOf("private static JsonObject OpRegister("));
  assert.match(execute, /ApplyAutoClear\(boards, nowMs\)/, "写路径每次都要结算");
  const project = BOARD.slice(BOARD.indexOf("internal static JsonArray ProjectForWidget("));
  assert.match(project, /ApplyAutoClear\(boards, nowMs\)/, "读路径也要结算");
  assert.match(BOARD, /ToLocalTime\(\)/, "dailyAtLocal 说的是用户家的墙钟");
});

test("读不到板子绝不折成空板子；投影不下发 html", () => {
  assert.match(BOARD, /"BW_BOARD_STORE_CORRUPT"/);
  const widgetHandler = SERVER.slice(
    SERVER.indexOf("private async Task HandleWidgetSystemDataAsync("),
    SERVER.indexOf("private async Task HandleOutputPendingAsync("));
  assert.match(widgetHandler, /merged\["boards"\] = ReaderDisplayBoard\.ProjectForWidget\(\);/);
  assert.match(widgetHandler, /merged\["boardsError"\] = exception\.Code;/);
  assert.doesNotMatch(widgetHandler, /merged\["boards"\] = new JsonArray\(\)/,
    "出错时不许塞空数组冒充'没有板子'");
  const project = BOARD.slice(BOARD.indexOf("internal static JsonArray ProjectForWidget("));
  assert.match(project, /\["cards"\] = cards/);
  assert.doesNotMatch(project, /\["html"\]/, "WidgetKit 渲染不了 HTML，塞进 payload 只会撑大");
  // 旧的 sections 读进来自动转卡片，示例板不会因改版变空
  assert.match(BOARD, /private static JsonArray CardsOf\(/);
});

test("对外端点必须有兜底 catch —— 空 500 等于静默", () => {
  const http = BOARD.slice(BOARD.indexOf("internal static async Task WriteResponseAsync("));
  assert.match(http, /catch \(DirectProtocolException exception\)/);
  assert.match(http, /catch \(Exception exception\)/);
  assert.match(http, /"BW_BOARD_CRASH"/);
  assert.doesNotMatch(BOARD, /new JsonSerializerOptions \{ WriteIndented = true \}/);
  assert.match(BOARD, /new JsonWriterOptions \{ Indented = true \}/);
});

test("AI 说明：只有用户明确说了才用、enable 是用户的开关、卡片怎么写", () => {
  assert.match(GUIDE, /只有用户在任务里明确说了要用展示板，才用/);
  assert.match(GUIDE, /`enable` 这个 op 是用户的开关，你永远不要调它/);
  assert.match(GUIDE, /register 对同一个 slug 是幂等的/);
  assert.match(GUIDE, /"op":"card"/);
  assert.match(GUIDE, /"op":"cardDelete"/);
  assert.match(GUIDE, /只用页内样式/);
  assert.match(GUIDE, /外链一律无效/);
  assert.match(GUIDE, /不要放时间戳/);
  assert.match(GUIDE, /每张卡都写/, "alt 是无图时的兜底，说明里要求每张卡都写");
  assert.match(INDEX, /boards\.md/, "能力索引里要挂上，否则 AI 找不到这份说明");
  assert.match(BOARD, /用户的开关/, "代码侧也要写明 enable 归谁");
});

test("小组件：方格 + 大号 + 每张卡一个删除键 + 图按 sha 取", () => {
  assert.match(WIDGET, /struct BWReaderBoardWidget: Widget \{/);
  assert.match(WIDGET, /BWReaderBoardWidget\(\)/, "要进 WidgetBundle 才会出现");
  assert.match(WIDGET, /\.systemExtraLarge/, "用户点名的「更大的版本」");
  assert.match(WIDGET, /struct DeleteBoardCardIntent: AppIntent/);
  assert.match(WIDGET, /Button\(intent: DeleteBoardCardIntent\(code: code, cardId: card\.id\)\)/,
    "每张卡固定一个删除键");
  assert.match(WIDGET, /"op": "cardDelete"/);
  assert.match(WIDGET, /reloadTimelines\(ofKind: "BWReaderBoardWidget"\)/, "删完立刻刷");
  assert.match(WIDGET, /default: return \(4, 2\)/, "大号放 8 张");
  // 图在 provider 里取好随 entry 交出去（视图里不能异步加载）
  assert.match(WIDGET, /var cardImages: \[String: UIImage\] = \[:\]/);
  assert.match(WIDGET, /WidgetCardImageCache\.images\(/);
  assert.match(WIDGET, /reader-board\/card\.png\?sha=/);
  // 只在真解出图之后才落盘
  assert.match(WIDGET, /UIImage\(data: data\) != nil\s*\n\s*else \{ return nil \}/);
  // 没图显示 alt，不留空方块
  assert.match(WIDGET, /card\.alt\.isEmpty \? "渲染中…" : card\.alt/);
  // 报错照实显示、数据时刻露出来
  assert.match(WIDGET, /boardsError: root\["boardsError"\] as\? String/);
  const view = WIDGET.slice(
    WIDGET.indexOf("private struct BoardWidgetView: View"),
    WIDGET.indexOf("struct BWReaderBoardWidget: Widget"));
  assert.match(view, /dataAge\(board\.updatedAtMs\)/);
  assert.match(view, /Text\(failure\)/);
  // 新字段一律可选，否则老缓存整份解码失败
  assert.match(MODEL, /var boards: \[Board\]\?/);
  assert.match(MODEL, /var boardsError: String\?/);
  assert.match(MODEL, /var cards: \[Card\]\?/);
  assert.equal(
    (PROJECT.match(/Shared\/ReaderSharedSnapshot\.swift/g) || []).length >= 2,
    true,
    "App 与 Widget 两个 target 都要列 ReaderSharedSnapshot.swift");
});

test("App 侧有手动操作面：停用 / 清空 / 删板子 / 逐张删卡，且失败看得见", () => {
  assert.match(TOOLS, /ReaderDisplayBoardSection\(\)/);
  assert.match(MANAGER, /func setEnabled\(/);
  assert.match(MANAGER, /func delete\(/);
  assert.match(MANAGER, /func clear\(/);
  assert.match(MANAGER, /func deleteCard\(/);
  assert.match(MANAGER, /"op": "cardDelete"/);
  assert.match(MANAGER, /reader-board\/card\.png\?sha=/, "App 里也按 sha 取渲好的图");
  assert.match(MANAGER, /WidgetCenter\.shared\.reloadAllTimelines\(\)/,
    "开关/删卡改变了小组件该显示什么，要立刻刷");
  assert.match(MANAGER, /payload\["detail"\] as\? String/,
    "桥的 detail 说得清错在哪，要原样端到界面上");
  assert.match(MANAGER, /@Published private\(set\) var failure: String\?/);
});
