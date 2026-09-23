// App 里本机 PDF 书的正文只由 PDFKit 画 —— 网页渲页整条不再露面（2026-09-23）。
//
// 用户："不能就把网页的渲染直接彻底删掉么 app 里不需要啊""所有旧的渲染在有新的
// 功能代替后都应该把旧的给去掉才对"。此前原生正文挂在一个默认关的开关后面，
// 挂上之前 / 被卸下重挂的那一段屏幕上露出的是网页渲的页和网页那套卡片、锁定框 ——
// 用户看到的就是"刚做好是细线框、滚动有残影，翻页回来又变了样"。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");
const code = (source) => source.split("\n").filter((l) => !/^\s*\/\//.test(l)).join("\n");

const APP = read("ios/BWReader/App/BWReaderNativeApp.swift");
const WEBVIEW = read("ios/BWReader/App/ReaderWebView.swift");
const SETTINGS = read("ios/BWReader/App/ReaderNativeReadingSettings.swift");

test("没有「用原生 PDFKit 渲染正文」开关：本机 PDF 一律原生", () => {
  assert.doesNotMatch(code(APP), /reader\.nativePDFRenderer/);
  assert.doesNotMatch(code(SETTINGS), /reader\.nativePDFRenderer|Toggle\("用原生 PDFKit 渲染正文"/);
  assert.doesNotMatch(code(WEBVIEW), /nativePDFRendererDefaultsKey/);
  const mount = WEBVIEW.slice(WEBVIEW.indexOf("func mountNativePDFDocument()"));
  assert.match(mount.slice(0, 400), /guard nativePDFExpected, nativePDFDocument == nil/);
  // 本机 PDF 由书的格式判定，不由开关判定。
  assert.match(WEBVIEW, /let pdf = currentLocalBookAccess\?\.record\.format == \.pdf/);
});

test("本机 PDF 书的网页层从头到尾都藏着（挂上之前也不露面）", () => {
  const hidden = APP.slice(APP.indexOf("private var webLayerHidden: Bool"), APP.indexOf("/// 原生正文还没挂上时占住阅读区"));
  assert.match(hidden, /reader\.nativePDFExpected \|\| reader\.nativePDFDocument != nil/);
  assert.match(APP, /\.opacity\(webLayerHidden \? 0 : 1\)/);
  assert.match(APP, /\.allowsHitTesting\(!webLayerHidden\)/);
  // 挂上之前 / 打不开时由原生占位，不退回网页渲页。
  assert.match(APP, /if webLayerHidden && !nativePDFSurfaceActive \{\s*nativePDFPlaceholder\s*\}/);
});

test("打不开要出声：原因 + 重试，而不是一块白或悄悄换回网页", () => {
  const mount = WEBVIEW.slice(WEBVIEW.indexOf("func mountNativePDFDocument()"), WEBVIEW.indexOf("/// 错误面板上的「重试」。"));
  assert.doesNotMatch(mount, /catch \{ return \}/, "prepare 失败不能静默 return");
  assert.match(mount, /self\.nativePDFOpenFailure = "正文没能打开：" \+ reason/);
  assert.match(mount, /self\.invalidateNativePDFDocument\(reason: "activate-failed"\)/);
  assert.match(WEBVIEW, /func retryNativePDFOpen\(\)/);
  assert.match(APP, /Button\("重试"\) \{ reader\.retryNativePDFOpen\(\) \}/);
});

test("从没写过的数据域（版本 0）不能让原生正文打不开", () => {
  // 2026-09-23 实报：一本没划过线的书打开就是"highlights 的版本或摘要无效"。
  // 本机导出里从没写过的域版本就是 0（导出解析本来就收 0...max），只有「发布出去的包」才要求 ≥ 1。
  const CODEC = read("ios/BWReader/App/ReaderBookUserStatePackage.swift");
  const DOC = read("ios/BWReader/App/ReaderNativePDFDocument.swift");
  const ADAPTER = read("ios/BWReader/App/ReaderBookUserStateWebAdapter.swift");
  assert.match(ADAPTER, /\(0\.\.\.ReaderBookUserStatePackageCodec\.maximumRevision\)\s*\.contains\(revision\)/);
  assert.match(CODEC, /\? \(0\.\.\.maximumRevision\)\.contains\(domain\.revision\)\s*: validRevision\(domain\.revision\)/);
  const calls = DOC.match(/validateDomainPayload\([^)]*\)/g) || [];
  assert.ok(calls.length >= 2);
  for (const call of calls) assert.match(call, /localExport: true/);
  // 发布出去的包仍然严格（validate 走默认参数）。
  assert.match(CODEC, /rawBytes \+= try validateDomainPayload\(domain\)/);
});

test("原生正文只接管一次：并发的布局回调不能互相拆台", () => {
  // 2026-09-23 实录：同一毫秒 3～4 条 activate failed —— 布局回调连发几下、各排一个 Task，
  // 后到的撞上「视口已被占用」失败，失败处理又把先到的成功卸掉，于是整本书打不开。
  const mount = WEBVIEW.slice(WEBVIEW.indexOf("func mountNativePDFDocument()"), WEBVIEW.indexOf("/// 错误面板上的「重试」。"));
  assert.match(mount, /let claim = ReaderNativeActivationClaim\(\)/);
  assert.match(mount, /guard let self, let document, !claim\.claimed,/);
  const claimAt = mount.indexOf("claim.claimed = true");
  assert.ok(claimAt > 0 && claimAt < mount.indexOf("try await self.activateNativePDFDocument(document)"),
    "认领必须在第一个 await 之前，否则排队的 Task 仍会并发进来");
  assert.match(mount, /guard self\.nativePDFDocument === document else \{ return \}/);
  assert.match(WEBVIEW, /@MainActor\nfinal class ReaderNativeActivationClaim \{/);
});

test("翻页不改会话 scope；原生视口的接管只认书的身份；选区用当下的 scope", () => {
  // 2026-09-23 实录：地址里的 ?page= 随翻页被改，scope 跟着变 → 原生视口接管随即失效
  // （翻页请求已过期或无效）、页码不再同步、选区每页被清、翻过去的卡"没同步"。
  const SCRIPT = read("ios/BWReader/App/ReaderNativeConversationScript.swift");
  const search = SCRIPT.slice(SCRIPT.indexOf("function identitySearch()"), SCRIPT.indexOf("function getScopeKey()"));
  assert.match(search, /params\.delete\('page'\)/);
  assert.doesNotMatch(SCRIPT, /\[navigationID, location\.pathname, location\.search/);
  const activate = WEBVIEW.slice(WEBVIEW.indexOf("func activateNativePDFDocument("), WEBVIEW.indexOf("activeNativePDFDocument = document"));
  assert.doesNotMatch(activate, /nativeConversation\.scope == scope/);
  assert.match(WEBVIEW, /scope: self\.nativeConversation\.scope\)/);
});

test("有字符数据的页一律走我们的选区菜单；读不到字符会退避重试而不是永久放弃", () => {
  // 2026-09-23 用户截图：选中弹的是系统菜单（Copy / Look Up / Translate）。
  const DOC = read("ios/BWReader/App/ReaderNativePDFDocument.swift");
  const inside = DOC.slice(DOC.indexOf("override func point(inside point: CGPoint, with event: UIEvent?) -> Bool {\n        // 锁定框"));
  assert.match(inside.slice(0, 900), /guard characters != nil else \{ return false \}/);
  assert.doesNotMatch(DOC, /!embeddedText \|\| characters\.textAuthority == \.localOverride/);
  const load = DOC.slice(DOC.indexOf("private func loadVisibleCharacterPages()"), DOC.indexOf("private func acceptOCRSelection("));
  assert.doesNotMatch(load.slice(0, load.indexOf("private func characterReadFailed")), /unavailableCharacterPages\.insert/,
    "第一次没读到不能直接判死");
  assert.match(load, /characterReadFailed\(number\)/);
  assert.match(load, /guard attempts < 6 else/);
  assert.match(load, /self\.loadVisibleCharacterPages\(\)/, "页面停着不动也要重试");
});

test("原生选区报进 AI 读的阅读快照（与侧栏开没开无关）", () => {
  // 2026-09-23 用户："他无法看到我的选中"。原生选区以前只写几个全局量，RC.ctxSync 里从来没有。
  const CARET = read("_server_deploy/static/pdf/reader.src/16-caret-select.js");
  const report = CARET.slice(CARET.indexOf("window.__bwReportNativeSelection = function"), CARET.indexOf("function checkSelection()"));
  assert.match(report, /RC\?\.ctxSync\?\.report\(/);
  assert.match(report, /selection: txt, sel_page: currentPage/);
  assert.match(report, /\{ immediate: true \}/);
  const SCRIPT = read("ios/BWReader/App/ReaderNativeConversationScript.swift");
  const handler = SCRIPT.slice(SCRIPT.indexOf("} else if (action === 'nativePageSelection') {"), SCRIPT.indexOf("} else if (action === 'nativeSelectionHighlight') {"));
  const reportAt = handler.indexOf("window.__bwReportNativeSelection?.(");
  assert.ok(reportAt > 0 && reportAt < handler.indexOf("if (!selection) {"), "清空也要报，所以要在分支之前");
  // 侧栏关着不钉进对话是原版规定，不算失败。
  assert.match(handler, /if \(pinned && window\.__focusSel\?\.text !== selection\)/);
});

test("选区照原版：单击查词；拖选/长按出 #sel-toolbar 那种窗口，不弹系统编辑菜单", () => {
  // 2026-09-23 用户："这和我们之前设计的不一样"。
  const DOC = read("ios/BWReader/App/ReaderNativePDFDocument.swift");
  const show = DOC.slice(DOC.indexOf("private func showMenu() {"), DOC.indexOf("func selectionWindowRect()"));
  assert.doesNotMatch(show, /presentEditMenu/);
  assert.match(show, /onPanel\?\(selected\)/);
  const tap = DOC.slice(DOC.indexOf("@objc private func tapText("), DOC.indexOf("@objc private func selectText("));
  assert.match(tap, /onLookup\?\(value, "dict"\)/, "单击一个词 = 直接查词");
  const PANEL = read("ios/BWReader/App/ReaderNativePDFSelectionPanel.swift");
  // 原版两组按钮与判据。
  assert.match(PANEL, /private var isWord: Bool/);
  for (const key of ["copy", "dict", "ocr", "search", "phrase", "translate", "explain", "chat", "grammar"]) {
    assert.match(PANEL, new RegExp(`"${key}"`), key);
  }
  assert.match(PANEL, /if active \{ activeColor = ""; return \}/, "再点当前色 = 只取消激活");
  assert.match(PANEL, /已选：/);
  const APP = read("ios/BWReader/App/BWReaderNativeApp.swift");
  assert.match(APP, /ReaderNativePDFSelectionPanelLayer\(document: document\)/);
});

test("卡头只有标题段认点按/拖动；「…」与删除不在状态切换范围里", () => {
  // 2026-09-23 用户："直接点击右上角三个点的按钮会关闭整个卡片导致按钮菜单无法使用"。
  const CARDS = read("ios/BWReader/App/ReaderNativePageCards.swift");
  const header = CARDS.slice(CARDS.indexOf("private var header: some View {"), CARDS.indexOf("private var card: some View {"));
  const gestureAt = header.indexOf(".gesture(pressGesture(onTap: tapHeader))");
  assert.ok(gestureAt > 0 && gestureAt < header.indexOf("if item.bound {"), "手势挂在标题 Text 上，不挂整条卡头");
  assert.equal((header.match(/pressGesture\(/g) || []).length, 1);
});

test("卡片收藏夹：原生按钮 + 面板，数据仍走 rc-voicecall 的收藏夹", () => {
  // 2026-09-23 用户："卡片收藏进收藏夹后也没有显示收藏夹的图标按钮"。
  const VC = read("_server_deploy/static/pdf/rc-voicecall.js");
  assert.match(VC, /window\.dispatchEvent\(new Event\('rc:favorites-changed'\)\)/);
  for (const api of ["count: function", "load: function", "remove: function", "place: function"]) assert.ok(VC.includes(api), api);
  const SCRIPT = read("ios/BWReader/App/ReaderNativeConversationScript.swift");
  assert.match(SCRIPT, /favoritesCount:/);
  assert.match(SCRIPT, /'rc:favorites-changed'/);
  for (const action of ["favoritesList", "favoritesPlace", "favoritesDelete"]) {
    assert.match(SCRIPT, new RegExp(`action === '${action}'`));
    assert.match(WEBVIEW, new RegExp(`"${action}"`));
  }
  const APP = read("ios/BWReader/App/BWReaderNativeApp.swift");
  assert.match(APP, /ReaderNativeFavoritesButton\(reader: reader, model: reader\.nativeConversation\)/);
});

test("卡片收藏夹是原版的样子：圆钮 + 底部横向时间轴，不是列表面板", () => {
  // 2026-09-23 用户："收藏夹和我之前设计的完全不同，我还是更喜欢原来的设计"。
  const FAV = read("ios/BWReader/App/ReaderNativeFavorites.swift");
  const APP = read("ios/BWReader/App/BWReaderNativeApp.swift");
  assert.match(APP, /ReaderNativeFavoritesPanelLayer\(reader: reader\)/);
  assert.doesNotMatch(FAV, /\.sheet\(|List \{|NavigationStack/, "不是系统列表 sheet");
  assert.match(FAV, /卡片收藏夹（向上拖出=复制到屏幕）/);
  assert.match(FAV, /ScrollView\(\.horizontal/);
  assert.match(FAV, /level == 0 \? min\(screenWidth \* 0\.76, 330\) : level == 1 \? 180 : 112/);
  for (const call of ["placeNativeFavorite(item, windowPoint:", "deleteNativeFavorites(", "loadNativeFavoritesTrash()",
                      "restoreNativeFavorite(", "toggleNativeFavoritePin("]) assert.ok(FAV.includes(call), call);
  const SCRIPT = read("ios/BWReader/App/ReaderNativeConversationScript.swift");
  for (const action of ["favoritesTrash", "favoritesRestore", "favoritesPin"]) {
    assert.match(SCRIPT, new RegExp(`action === '${action}'`));
    assert.match(WEBVIEW, new RegExp(`"${action}"`));
  }
});

test("慢词查词：不弹挡人的框，而是那个词呼吸高亮；回来了再弹或等人点", () => {
  // 2026-09-23 用户："没有命中时应该用之前我们的那个闪烁逻辑，不然挡在这里什么都干不了"。
  const DOC = read("ios/BWReader/App/ReaderNativePDFDocument.swift");
  assert.match(DOC, /struct PendingLookup/);
  assert.match(DOC, /item\.ready \? 0\.38 : 0\.12 \+ 0\.28 \* pulse/);
  assert.match(DOC, /if let id = pendingLookupAt\?\(location\) \{ onOpenPendingLookup\?\(id\); return \}/);
  assert.match(WEBVIEW, /func presentWordLookup\(/);
  assert.match(WEBVIEW, /document\.addPendingLookup\(id: id/);
  assert.match(WEBVIEW, /nativeLookupCancelSeq \+= 1/);
});

test("侧栏卡拖到书页：PDFKit 自带的 drop 交互拆掉；每一步出声", () => {
  const DOC = read("ios/BWReader/App/ReaderNativePDFDocument.swift");
  assert.match(DOC, /interaction is UIDropInteraction/);
  const place = WEBVIEW.slice(WEBVIEW.indexOf("func placeNativeConversationCard("), WEBVIEW.indexOf("func resizeNativeConversationCard("));
  assert.doesNotMatch(place, /else \{ return \}/, "条件不满足不能一声不吭地 return");
  assert.match(place, /\[card-drop\]/);
});

test("查词：phrase/isJa 在第一个用到它们的分支之前声明；面板带原版小框的按钮与字段", () => {
  // 2026-09-23 用户截图："Cannot access 'phrase' before initialization" —— 每一次查词都抛。
  const SRC = read("_server_deploy/static/pdf/reader.src/15-phrase-wordpop.js");
  const fn = SRC.slice(SRC.indexOf("window.__bwReaderLookupData = async function"), SRC.indexOf("function _phraseStateOf("));
  const declared = fn.indexOf("const phrase = request.mode === 'phrase';");
  assert.ok(declared > 0 && declared < fn.indexOf("(phrase && !isJa)"), "声明必须在第一次使用之前");
  assert.ok(fn.indexOf("const isJa = _isJaWord(text);") < fn.indexOf("(phrase && !isJa)"));
  assert.match(fn, /inflect: _lookupPlain\(_jpInflectHtml\(/);
  assert.match(fn, /inflect: _lookupPlain\(_enFormsHtml\(/);
  const VIEW = read("ios/BWReader/App/ReaderNativeLookupView.swift");
  assert.match(VIEW, /barButton\("语法", icon:/);
  assert.match(VIEW, /model\.inflection/);
  assert.match(VIEW, /model\.partOfSpeech/);
});

test("原生词典不阉割：小框 + 完整字典框的每一样都在，且与网页同一个数据入口", () => {
  // 2026-09-24 用户："词典内容也和之前不一样，少了很多元素 …… 应该在旧的基础上改动，
  // 进行一定美化而不是现在这样的阉割"。
  const SHARED = read("_server_deploy/static/pdf/rc-wordpop.js");
  const entry = SHARED.slice(SHARED.indexOf("async function nativeEntry("), SHARED.indexOf("async function nativeAction("));
  for (const use of ["lookupData(word, ctx, opts)", "_jpMeaningText(d)", "_jpExamples(d)", "_jpInflectHtml(", "_jpSourceHtml(d)", "_repoMastery("])
    assert.ok(entry.includes(use), "同一套判据：" + use);
  assert.doesNotMatch(entry, /e\.zh \|\| e\.en/, "例句中文缺了就空着补，绝不拿英文冒充");
  const action = SHARED.slice(SHARED.indexOf("async function nativeAction("), SHARED.indexOf("RC.wordpop = {"));
  for (const kind of ["example-zh", "jp-ai", "vocab-anki", "word-cards"]) assert.ok(action.includes(`'${kind}'`), kind);
  assert.match(action, /_queryWordCards\(/, "词锚卡与小框同一处查询");
  const SRC = read("_server_deploy/static/pdf/reader.src/15-phrase-wordpop.js");
  const EPUB = read("_server_deploy/static/pdf/epub-html.js");
  for (const source of [SRC, EPUB]) {
    assert.match(source, /RC\.wordpop\.nativeEntry\(text, context/);
    assert.match(source, /RC\.wordpop\.nativeAction\(request\.mode, text, context\)/);
  }
  const VIEW = read("ios/BWReader/App/ReaderNativeLookupView.swift");
  for (const piece of ["ReaderNativePitchView(reading: model.reading, accent: accent)", "model.origin", "boundCards",
                       "kanjiSection", "readingRow(\"音\"", "readingRow(\"訓\"", "aiSection", "model.meaningSource",
                       "model.exampleZh[index]", "model.addToAnki()", "点这里展开完整字典"])
    assert.ok(VIEW.includes(piece), piece);
  const SCRIPT = read("ios/BWReader/App/ReaderNativeConversationScript.swift");
  assert.match(SCRIPT, /'example-zh', 'jp-ai', 'vocab-anki', 'word-cards'\]\.includes\(value\.mode\)/);
});

test("侧栏卡能长按拖起：会话列表的滚动检测不在手指落下时就认领触摸", () => {
  // 2026-09-23 用户："拖卡甚至无法在侧边栏中长按进入拖动模式"。
  const VIEW = read("ios/BWReader/App/ReaderNativeConversationView.swift");
  assert.doesNotMatch(VIEW, /simultaneousGesture\(\s*DragGesture\(minimumDistance: 0\)/);
  const CARDS = read("ios/BWReader/App/ReaderNativeConversationCards.swift");
  assert.match(CARDS, /\.contentShape\(Rectangle\(\)\)\s*\n\s*\/\/ 拖出去时给一个像"卡片副本"的影子/);
});

test("点词查词贴着词弹小框（原版 #word-pop），不走底部面板", () => {
  const APP = read("ios/BWReader/App/BWReaderNativeApp.swift");
  assert.match(APP, /ReaderNativeWordPopLayer\(reader: reader\)/);
  assert.match(WEBVIEW, /if \["dict", "phrase"\]\.contains\(mode\), let document = nativePDFDocument,\s+let anchor = document\.lastLookupAnchor \{/);
  const POP = read("ios/BWReader/App/ReaderNativeWordPop.swift");
  assert.match(POP, /ReaderNativeLookupContent\(model: model\)/);
  assert.match(POP, /\.task \{ await model\.load\(\) \}/);
});

test("侧栏收起时 AI 回复出流式字幕（不只语音通话才有）", () => {
  // 2026-09-24 用户："侧边栏收起时候的字幕还是没有显示 …… 就是显示ai回复那个，现在是流式传输
  // 应该可以做成流式字幕"。网页 #vc-cap 只在语音链路活跃时才亮，打字问的回复永远不上字幕。
  const BAR = read("ios/BWReader/App/ReaderNativeCaptionBar.swift");
  assert.match(BAR, /private var replyLines: \[ReaderNativeCaptions\.Line\]/);
  assert.match(BAR, /if model\.captions\.on, !model\.captions\.lines\.isEmpty \{ return model\.captions\.lines \}/, "语音字幕优先");
  assert.match(BAR, /guard model\.captions\.enabled else \{ return \[\] \}/, "原版字幕开关仍然管用");
  assert.match(BAR, /\.task\(id: replyKey\)/, "回复写完停几秒再收");
  assert.match(BAR, /\.truncationMode\(line\.previous \? \.tail : \.head\)/, "正在长的那句留住最新的字");
  const SCRIPT = read("ios/BWReader/App/ReaderNativeConversationScript.swift");
  assert.match(SCRIPT, /enabled = localStorage\.getItem\('rc-voice-sub'\) !== '0'/);
});
