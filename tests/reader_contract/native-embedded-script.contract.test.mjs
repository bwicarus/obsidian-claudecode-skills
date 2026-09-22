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

/** 剥掉行注释再做文本断言。
 *
 * ⚠ 2026-09-22 这一天我被自己写的注释绐倒四次：注释里为了说清楚
 *   "不该再写 X"，就不得不把 X 写出来；而 `doesNotMatch(/X/)` 看不出
 *   那是注释。结果是"注释写得越清楚，测试越红"。
 */
function codeOnly(text) {
  const NL = String.fromCharCode(10);
  return text.split(NL).filter((line) => !line.trim().startsWith("//")).join(NL);
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

test("原生接管时根本不开网页抽屉", () => {
  // ⚠ 2026-09-22 用户截图拍实的那一幕：**两个侧栏并排**。
  //   只要还调 drawer().open()，网页那套侧栏就会滑出来，
  //   能不能看见取决于一堆条件（nativeMode 送到没、tab 是不是 asst、
  //   CSS 落了没）—— 任何一条不成立就是两套同时在。不开它，这整类条件就不存在了。
  const js = embeddedScript("ReaderNativeConversationScript.swift");
  const branch = js.slice(js.indexOf("action === 'toggleAssistant'"),
                          js.indexOf("action === 'clearSelection'"));
  assert.match(branch, /if \(nativeOwnsAssistant\(\)\) \{/, "原生接管时没走独立分支");
  const owned = branch.slice(branch.indexOf("if (nativeOwnsAssistant())"));
  assert.doesNotMatch(owned.slice(0, 400), /drawer\(\)\.open|drawer\(\)\?\.open/,
                      "原生分支里又去开网页抽屉了");
  // 开合由原生自己记，不再借网页抽屉的状态来表示。
  assert.match(js, /sidebarOpen: nativeOwnsAssistant\(\) \? nativeAssistantOpen/);
});

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
  // ⚠ 发件箱：桥没开/电脑睡了时报告不能蒸发 —— 而那恰恰是最需要报告的
  //   时候（用户在外面用 iPad）。发不出去就留着，下次启动再发。
  assert.match(reporter, /var outbox/, "没有发件箱，一次发失败就永久丢了");
  assert.match(reporter, /outbox = previous\?\.outbox \?\? \[\]/, "上次没发出去的没人接着发");
  assert.match(reporter, /\(200\.\.\.299\)\.contains/, "没看响应码就当发成了");

  const view = readFileSync(new URL(
    "../../ios/BWReader/App/ReaderWebView.swift", import.meta.url), "utf8");
  assert.match(view, /BW_WEBCONTENT_TERMINATED/, "渲染进程被回收没自动上报");
  const model = readFileSync(new URL(
    "../../ios/BWReader/App/ReaderNativeConversationModel.swift", import.meta.url), "utf8");
  // ⚠ 命令失败**只留面包屑，不各发一条上报**（2026-09-22 改）。
  //   上一版每次失败都 report，而 report 会落盘 + 触发整个发件箱重投；
  //   服务器书上这类失败是**成串**的（一次翻页九条），
  //   于是诊断机制自己变成了负载源 —— 用户报的正是"关掉服务器就不闪退了"。
  assert.match(model, /shared\.note\("fail"/, "命令失败连面包屑都没留");
  assert.doesNotMatch(model, /shared\.report\(/, "命令失败又改成逐条上报了");
  const app = readFileSync(new URL(
    "../../ios/BWReader/App/BWReaderNativeApp.swift", import.meta.url), "utf8");
  assert.match(app, /beginSession\(origin: ReaderServer\.origin\)/);
  assert.match(app, /endSession\(\)/, "没有干净退出标记 → 每次启动都误报");
});

test("网页外壳的隐藏在 documentStart 落地，不靠消息送达", () => {
  // ⚠ 这是 2026-09-22「所有元素好像都有两种实现同时存在」的根因：
  //   原来这条样式写在 atDocumentEnd 的脚本里，还要再等 Swift 把 setNativeMode
  //   送到页面才生效；而原生顶栏是 SwiftUI 画的、**不等任何人**。中间那段窗口
  //   两套一起在，消息一旦没送到（页面重载/渲染进程被回收后恢复）就是**永久**两套。
  const view = readFileSync(new URL(
    "../../ios/BWReader/App/ReaderWebView.swift", import.meta.url), "utf8");
  const start = view.indexOf("bw-native-shell-style");
  assert.ok(start > 0, "没有 documentStart 那条外壳样式");
  // ⚠ 片段要够长：这段注释会随着多藏一个元素就变长，写死 800
  //   会把 injectionTime 挤出片段外，断言就莫名其妙地红。
  const after = view.slice(start, start + 2600);
  assert.match(after, /injectionTime: \.atDocumentStart/, "外壳样式必须在 documentStart");
  // 默认方向必须是"没有网页外壳"，关掉原生界面才放回来 ——
  // 失败时的结果就从"两套都在"变成"只有原生"。
  assert.match(view, /:not\(\.bw-native-legacy-chrome\) #header/);

  const js = embeddedScript("ReaderNativeConversationScript.swift");
  // ⚠ 只认那条**外壳隐藏**规则搬没搬走。#header 本身不能当判据：
  //   同一段样式里还有一条合法的 body.grammar-open #header{padding-right:0}，
  //   拿它做断言等于用自己的合法代码绊自己（今天已经栽过一次）。
  const styles = js.slice(js.indexOf("bw-native-conversation-style"));
  assert.doesNotMatch(styles.slice(0, 1400), /#fs-restore|#ep-top/,
                      "外壳隐藏规则又被搬回 documentEnd 的脚本里了");
  const fn = js.slice(js.indexOf("function applyVisualMode("), js.indexOf("function setLegacy("));
  // ⚠ 只有**真得到过答案**（setNativeMode 调过）时才能碰这个类。
  //   本函数会在消息送到之前先跑，那时 nativeMode 还是初值 false ——
  //   照字面写就把 documentStart 藏好的网页顶栏又放出来，用户看到的
  //   是**两条顶栏并排**（2026-09-22 实际发生，是我自己引入的回归）。
  assert.match(fn, /if \(nativeModeKnown\) \{/, "没有先问知不知道就动了外壳");
  assert.match(fn, /bw-native-legacy-chrome', !nativeMode \|\| legacyVisible/);
  const js2 = embeddedScript("ReaderNativeConversationScript.swift");
  assert.match(js2, /nativeModeKnown = true;/, "setNativeMode 没把知情标记立起来");
});

test("App 里根本不建那套网页侧栏外壳", () => {
  // 用户 2026-09-22 连问三次「就不能把旧的页面删掉么」。能删的就是这些纯界面：
  // 把手、tab 条、外观设置面板、点空白关闭。剩下的 #ep-side 只当内容宿主 ——
  // 原生侧栏从同一棵 DOM 读内容，再往下删掉的就是对话引擎本身。
  const drawer = readFileSync(new URL(
    "../../_server_deploy/static/pdf/rc-sidedrawer.js", import.meta.url), "utf8");
  const build = drawer.slice(drawer.indexOf("function buildChrome(force)"),
                             drawer.indexOf("function _syncSettingsUI"));
  // ⚠ 提前返回必须在把手/tab 条之前 —— 排在后面等于白做。
  const guard = build.indexOf("shelllessDrawer() && force !== true");
  assert.ok(guard > 0, "没有无壳分支");
  assert.ok(guard < build.indexOf("ep-side-handle"), "无壳分支排在把手之后了");
  assert.ok(guard < build.indexOf("ep-side-tabs"), "无壳分支排在 tab 条之后了");
  // ⚠ 判据必须取 documentStart 打的那个类，不能再依赖一条要送达的消息。
  assert.match(drawer, /classList\.contains\('bw-native-shell'\)/);
  // 逃生出口不能丢：真要看旧界面时按需补建。
  assert.match(drawer, /if \(!_headless && shelllessDrawer\(\)\) ensureChrome\(\);/);
});

test("富文本的振假名重建不许在布局里重入", () => {
  // ⚠ 2026-09-22 那个崩溃就在这里（dSYM 符号化确认：
  //   ReaderNativeTextView.layoutSubviews +108，出错指令 ldr x21,[x8,#16]，x8=0，
  //   即读一个空的 Swift 数组缓冲区）。
  //   成因：在 enumerateAttribute 回调里 addSubview + rubyLabels.append。
  //   addSubview 让布局失效 → UIKit 同步重入 layoutSubviews → 那一轮开头
  //   removeAll(keepingCapacity:) → 同一个数组被两层同时改。
  //   表现跟"做了什么"无关，只跟"有没有触发一次布局"有关，所以三个毫不相干的
  //   操作崩在同一行 —— 极难从现象反推。
  const rich = readFileSync(new URL(
    "../../ios/BWReader/App/ReaderNativeRichText.swift", import.meta.url), "utf8");
  // ⚠ 结束锚点要从起点往后找：文件开头还有一个
  //   `private extension NSAttributedString.Key`，直接 indexOf 会落在起点**之前**，
  //   切出一个空串 —— 然后所有断言都"失败"得莫名其妙。
  const fnStart = rich.indexOf("override func layoutSubviews()");
  const fn = rich.slice(fnStart, rich.indexOf("private extension NSAttributedString {", fnStart));
  assert.match(fn, /guard !rebuildingRuby else \{ return \}/, "没有重入闸");
  assert.match(fn, /rebuildingRuby = true\s*\n\s*defer \{ rebuildingRuby = false \}/);
  // 回调里只许往局部数组塞，不许改属性、不许挂视图。
  // ⚠ 锚到**真正的调用**（带接收者），不能只写 enumerateAttribute ——
  //   上面那段注释里就提到了它，也提到了 addSubview，于是断言被自己写的
  //   注释绐倒（今天第三次犯）。
  const callback = fn.slice(fn.indexOf("attributedText.enumerateAttribute("));
  assert.doesNotMatch(callback.slice(0, callback.indexOf("built.forEach")),
                      /addSubview|rubyLabels\.append/,
                      "又在 enumerate 回调里挂视图/改属性了");
  // ⚠ 真因在这里：原来存着 `rubyLabels: [UILabel]`，而它被读出来是
  //   **空指针** —— Swift 数组永远不可能是空指针，除非这块内存还是零：
  //   `UITextView(usingTextLayoutManager:)` 是继承来的 ObjC 初始化器，
  //   它内部就可能触发一次布局，而那一刻子类的 Swift 存储属性还没赋值。
  //   ⚠ 所以不能只"加保护"：重入闸是个 Bool，零值正好放行。
  //   必须让那个会读到零的存储属性**不存在**。
  assert.doesNotMatch(codeOnly(fn), /rubyLabels/, "又把标签存回存储属性了");
  assert.match(fn, /subviews\.compactMap \{ \$0 as\? ReaderNativeRubyLabel \}/,
               "标签必须从 subviews 里认（ObjC 属性，任何时刻都合法）");
  assert.match(fn, /var built: \[ReaderNativeRubyLabel\] = \[\]/);
});
