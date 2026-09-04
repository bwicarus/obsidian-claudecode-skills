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
const GUIDE = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderCapabilities/boards.md");
const INDEX = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderCapabilities/index.md");
const WIDGET = read("ios/BWReader/Widget/BWReaderWidget.swift");
const MODEL = read("ios/BWReader/Shared/ReaderSharedSnapshot.swift");
const TOOLS = read("ios/BWReader/App/NativeReaderToolsView.swift");
const MANAGER = read("ios/BWReader/App/ReaderDisplayBoardManager.swift");
const PROJECT = read("ios/BWReader/project.yml");

// 展示板（用户 2026-09-04 要求）：给「一段时间里反复看状态」的任务一块分了区的板子。
// 这份契约钉的是那些**改一处不等于到达**的地方，以及三条已经吃过教训的纪律。

test("端点路径在两份副本里一致：C# 路由表 + 桥的 serve 放行清单", () => {
  // 只加路由不放行 = 端点 404 而桥看起来完全正常（CLAUDE.md「先数清楚有几份副本」）。
  assert.match(BOARD, /internal const string BoardPath = "\/reader-board\/v1";/);
  assert.match(
    SERVER,
    /ReaderDisplayBoard\.BoardPath,\s*\n\s*new\[\] \{ "POST", "OPTIONS" \},/,
    "C# 侧要注册 POST 路由");
  assert.match(CORE, /"\/reader-board\/v1",/,
    "bridge_core 的 DIRECT_SERVE_SIBLING_PATHS 要放行同一条路径");
});

test("一个端点一个 op，八个操作都在，且拒绝未知 op", () => {
  for (const op of [
    "register", "update", "section", "clear", "delete", "enable", "get", "list",
  ]) {
    assert.match(BOARD, new RegExp(`case "${op}":`), `缺 op: ${op}`);
  }
  assert.match(BOARD, /"BW_BOARD_UNKNOWN_OP"/);
  // register 对同一 slug 幂等 —— 固定程序每次跑都能拿回同一个 code，不必自己存
  assert.match(BOARD, /board\["slug"\]\?\.GetValue<string>\(\) == slug/);
  assert.match(BOARD, /\["created"\] = false/);
});

test("每个尺寸都有上限，且超了是拒绝而不是悄悄截断", () => {
  for (const limit of [
    "MaxBoards", "MaxSections", "MaxLines", "MaxLineChars",
    "MaxTitleChars", "MaxSlugChars", "MaxNoteChars", "MaxStoreBytes",
  ]) {
    assert.match(BOARD, new RegExp(`private const \\w+ ${limit} =`), limit);
  }
  assert.match(BOARD, /"BW_BOARD_FIELD_INVALID"[\s\S]{0,200}超过/);
  assert.match(BOARD, /"BW_BOARD_LIMIT"/);
});

test("自动消除在每次加载时结算，不靠定时器；本地墙钟", () => {
  // 定时器只在进程活着时才跑，而这份数据的读者（小组件）不在这个进程里。
  assert.match(BOARD, /private static bool ApplyAutoClear\(/);
  const execute = BOARD.slice(
    BOARD.indexOf("internal static JsonObject Execute("),
    BOARD.indexOf("private static JsonObject OpRegister("));
  assert.match(execute, /ApplyAutoClear\(boards, nowMs\)/,
    "写路径每次都要结算");
  const project = BOARD.slice(BOARD.indexOf("internal static JsonArray ProjectForWidget("));
  assert.match(project, /ApplyAutoClear\(boards, nowMs\)/,
    "读路径（小组件投影）也要结算");
  assert.match(BOARD, /ToLocalTime\(\)/, "dailyAtLocal 说的是用户家的墙钟");
});

test("读不到板子绝不折成空板子（空板子会被下游当权威）", () => {
  assert.match(BOARD, /"BW_BOARD_STORE_CORRUPT"/);
  // 小组件那一跳：板子出错只报 boardsError，别的照发，**不让整份数据变 503**
  const widgetHandler = SERVER.slice(
    SERVER.indexOf("private async Task HandleWidgetSystemDataAsync("),
    SERVER.indexOf("private async Task HandleOutputPendingAsync("));
  assert.match(widgetHandler, /merged\["boards"\] = ReaderDisplayBoard\.ProjectForWidget\(\);/);
  assert.match(widgetHandler, /merged\["boardsError"\] = exception\.Code;/);
  assert.doesNotMatch(
    widgetHandler,
    /merged\["boards"\] = new JsonArray\(\)/,
    "出错时不许塞空数组冒充'没有板子'");
});

test("对外端点必须有兜底 catch —— 空 500 等于静默", () => {
  // 2026-09-05 实锤：section/update 首次真机调用返回空 500，零线索；
  // 加了兜底把异常类型与消息带出来，一次调用就看见真因。
  const http = BOARD.slice(BOARD.indexOf("internal static async Task WriteResponseAsync("));
  assert.match(http, /catch \(DirectProtocolException exception\)/);
  assert.match(http, /catch \(Exception exception\)/);
  assert.match(http, /"BW_BOARD_CRASH"/);
  assert.match(http, /exception\.GetType\(\)\.Name/);
  // 缩进不许用自己 new 的 JsonSerializerOptions（这个宿主里反射默认关，会抛）
  assert.doesNotMatch(BOARD, /new JsonSerializerOptions \{ WriteIndented = true \}/);
  assert.match(BOARD, /new JsonWriterOptions \{ Indented = true \}/);
});

test("AI 说明：只有用户明确说了才用，且 enable 是用户的开关", () => {
  // 用户原话：「这个展示板的使用是用户在创建任务时明确说明需要开启的，
  // 所以不需要 ai 自行判断是否开启」。面向 AI 的说明写反比没写更糟。
  assert.match(GUIDE, /只有用户在任务里明确说了要用展示板，才用/);
  assert.match(GUIDE, /`enable` 这个 op 是用户的开关，你永远不要调它/);
  assert.match(GUIDE, /register 对同一个 slug 是幂等的/);
  assert.match(GUIDE, /不要放 Markdown 标记/);
  assert.match(GUIDE, /不要放时间戳/);
  assert.match(INDEX, /boards\.md/, "能力索引里要挂上，否则 AI 找不到这份说明");
  assert.match(BOARD, /用户的开关/, "代码侧也要写明 enable 归谁");
});

test("小组件：解码 boards、报错照实显示、数据时刻露出来", () => {
  assert.match(WIDGET, /struct BWReaderBoardWidget: Widget \{/);
  assert.match(WIDGET, /BWReaderBoardWidget\(\)/, "要进 WidgetBundle 才会出现");
  assert.match(WIDGET, /boardsError: root\["boardsError"\] as\? String/);
  const view = WIDGET.slice(
    WIDGET.indexOf("private struct BoardWidgetView: View"),
    WIDGET.indexOf("struct BWReaderBoardWidget: Widget"));
  assert.match(view, /dataAge\(board\.updatedAtMs\)/,
    "快照必须显示数据时刻，否则会拿'拉取成功'冒充'内容新鲜'");
  assert.match(view, /entry\.data\?\.boardsError/);
  assert.match(view, /Text\(failure\)/, "错误码要显示出来：小组件没有控制台");
  // 新字段一律可选，否则老缓存整份解码失败
  assert.match(MODEL, /var boards: \[Board\]\?/);
  assert.match(MODEL, /var boardsError: String\?/);
  // Shared 下的文件每个 target 都要单独列（project.yml 里那条注释的同一坑）
  assert.equal(
    (PROJECT.match(/Shared\/ReaderSharedSnapshot\.swift/g) || []).length >= 2,
    true,
    "App 与 Widget 两个 target 都要列 ReaderSharedSnapshot.swift");
});

test("App 侧有手动操作面：停用 / 清空 / 删除，且失败看得见", () => {
  assert.match(TOOLS, /ReaderDisplayBoardSection\(\)/);
  assert.match(MANAGER, /func setEnabled\(/);
  assert.match(MANAGER, /func delete\(/);
  assert.match(MANAGER, /func clear\(/);
  assert.match(MANAGER, /WidgetCenter\.shared\.reloadAllTimelines\(\)/,
    "开关改变了小组件该显示什么，要立刻刷");
  assert.match(MANAGER, /payload\["detail"\] as\? String/,
    "桥的 detail 说得清错在哪，要原样端到界面上");
  assert.match(MANAGER, /@Published private\(set\) var failure: String\?/);
});
