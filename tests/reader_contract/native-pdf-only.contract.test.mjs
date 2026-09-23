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
