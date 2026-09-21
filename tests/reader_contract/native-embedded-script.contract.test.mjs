// Swift 里内嵌的那几段 JS 也要过语法检查。
//
// ⚠ 为什么单独立一条：这些 JS 写在 Swift 的 raw string 里，**Swift 编译器只当它
//   是一串字符**。写坏一个括号，CI 全绿、包照出，到设备上整层原生会话直接不工作
//   —— 而且是安静地不工作（注入脚本抛错不会有人看见）。项目里所有 .js 文件都有
//   语法门禁，唯独这几段没有。
import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync, writeFileSync, mkdtempSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { tmpdir } from "node:os";
import { join } from "node:path";

/** Swift raw string（#"""…"""#）里那段 JS。 */
function embeddedScript(file) {
  const source = readFileSync(new URL("../../ios/BWReader/App/" + file, import.meta.url), "utf8");
  const start = source.indexOf('#"""');
  const end = source.lastIndexOf('"""#');
  assert.ok(start >= 0 && end > start, file + " 里找不到内嵌脚本");
  // \#(…) 是 Swift 插值，不是 JS；换成一个常量再送去解析。
  return source.slice(start + 4, end).replace(/\\#\([^)]*\)/g, "0");
}

const FILES = ["ReaderNativeConversationScript.swift"];

for (const file of FILES) {
  test(file + " 内嵌的 JS 语法正确", () => {
    const scratch = mkdtempSync(join(tmpdir(), "bw-embedded-"));
    const path = join(scratch, file.replace(".swift", ".mjs"));
    writeFileSync(path, embeddedScript(file), "utf8");
    execFileSync(process.execPath, ["--check", path], { stdio: "pipe" });
  });
}

test("开侧栏是「先压住再开」，不是反过来", () => {
  // ⚠ 顺序反了就是这次那个崩：网页抽屉先按自己的样式开出来（旧侧栏闪一下 +
  //   整本 EPUB 连续重排 400ms + backdrop-filter 开始合成），下一次快照才去压。
  //   大书上这一串足以把 WebContent 进程顶掉。
  const js = embeddedScript("ReaderNativeConversationScript.swift");
  const branch = js.slice(js.indexOf("action === 'toggleAssistant'"),
                          js.indexOf("action === 'clearSelection'"));
  assert.ok(branch.includes("applyVisualMode(true); drawer().open('asst')"),
            "必须先 applyVisualMode(true) 再 open");
});

test("压住与否只看「这个面归不归原生管」，不看抽屉开没开", () => {
  const js = embeddedScript("ReaderNativeConversationScript.swift");
  const fn = js.slice(js.indexOf("function applyVisualMode("),
                      js.indexOf("function setLegacy("));
  // ⚠ 条件里出现 isOpen() 就回到了"开了才压"，闪烁与重排会一起回来。
  assert.doesNotMatch(fn, /isOpen\(\)/, "条件里不该再看 isOpen()");
  // ⚠ 但 tab 判断要留着：网页自己也会开抽屉到 grammar/kg/vocab，那些面原生没
  //   接管，一并压住就是"点了什么都不出来"。
  assert.match(fn, /activeTab\(\) === 'asst'/, "tab 判断不能一起删掉");
});

test("渲染进程被回收要出声，不能只是默默重载", () => {
  // ⚠ 这条防的是 2026-09-22 那次：页面白屏转圈再自己回来，用户只能叫它"崩溃"，
  //   而崩之前在做什么、崩了几次，没有任何地方说得出来 —— 于是只能靠猜。
  const view = readFileSync(
    new URL("../../ios/BWReader/App/ReaderWebView.swift", import.meta.url), "utf8");
  const handler = view.slice(view.indexOf("func webViewWebContentProcessDidTerminate"),
                             view.indexOf("private func noteWebContentTermination"));
  assert.match(handler, /noteWebContentTermination\(\)/, "回收时没有记一笔");
  // 线索必须在 resetForNavigation() 之前取：它会把上一条命令一起清掉。
  assert.ok(handler.indexOf("noteWebContentTermination()") <
            handler.indexOf("nativeConversation.resetForNavigation()"),
            "记录要排在 resetForNavigation 之前，否则线索已经被清了");
  assert.match(view, /lastCommandAction/, "没带上「崩之前在做什么」");
  const app = readFileSync(
    new URL("../../ios/BWReader/App/BWReaderNativeApp.swift", import.meta.url), "utf8");
  assert.match(app, /reader\.webContentRecoveryNotice/, "提示没有接到界面上");
});

test("原生接管时抽屉走无头 —— 不是用 CSS 盖住", () => {
  // ⚠ 盖住只解决"看不看得见"。`open()` 里的 `body.ep-side-open` 照样执行，
  //   `#ep-viewer` 的 margin 照样变一次再被压回去，`_reflow()` 照样 dispatch
  //   resize —— **整本 EPUB 连续重排两轮**，那才是把渲染进程顶掉的东西。
  const js = embeddedScript("ReaderNativeConversationScript.swift");
  const fn = js.slice(js.indexOf("function applyVisualMode("), js.indexOf("function setLegacy("));
  assert.match(fn, /setHeadless\?\.\(nativeMode && !legacyVisible, 'asst'\)/);

  const drawer = readFileSync(new URL(
    "../../_server_deploy/static/pdf/rc-sidedrawer.js", import.meta.url), "utf8");
  const open = drawer.slice(drawer.indexOf("function open(tab)"), drawer.indexOf("function close()"));
  // 无头分支必须排在 _layoutKeep 之前 —— 排在后面就等于白做。
  assert.ok(open.indexOf("_headless") < open.indexOf("_layoutKeep(true)"),
            "无头分支要在 _layoutKeep 之前返回");
  assert.doesNotMatch(open.slice(0, open.indexOf("_layoutKeep(true)")), /ep-side-open/,
                      "无头路径上不许加 body 类");
  // ⚠ 只对原生接管的那个 tab 无头：其余 tab（grammar/kg/vocab）原生没接，
  //   一并无头就成了"点了什么都不出来"。
  assert.match(open, /_headlessOwned/, "无头不该对所有 tab 一刀切");
});

test("故障会自己送出去，而不是死在原地", () => {
  // 用户 2026-09-22：「不能做一个出问题不立刻退出而是自动发送故障信息给你的机制么」
  const reporter = readFileSync(new URL(
    "../../ios/BWReader/App/ReaderNativeFaultReporter.swift", import.meta.url), "utf8");
  // 两条命缺一不可：① 页面死、App 活 → 当场报；② App 也死 → 下次启动补报。
  assert.match(reporter, /reader-error-log/, "没接到已经通了的那条管子上");
  assert.match(reporter, /BW_APP_UNCLEAN_EXIT/, "App 自己崩没人补报");
  assert.match(reporter, /func persist\(clean: Bool\)/, "面包屑没落盘，App 一死就全没了");
  assert.match(reporter, /guard !began else/, "beginSession 不是一次性的，会覆盖上次的证据");

  const view = readFileSync(new URL(
    "../../ios/BWReader/App/ReaderWebView.swift", import.meta.url), "utf8");
  assert.match(view, /BW_WEBCONTENT_TERMINATED/, "渲染进程被回收没自动上报");
  const model = readFileSync(new URL(
    "../../ios/BWReader/App/ReaderNativeConversationModel.swift", import.meta.url), "utf8");
  assert.match(model, /BW_NATIVE_COMMAND_FAILED/, "原生命令失败只写在没人看的地方");
  const app = readFileSync(new URL(
    "../../ios/BWReader/App/BWReaderNativeApp.swift", import.meta.url), "utf8");
  assert.match(app, /beginSession\(origin: ReaderServer\.origin\)/);
  assert.match(app, /endSession\(\)/, "没有干净退出标记 → 每次启动都误报");
});
