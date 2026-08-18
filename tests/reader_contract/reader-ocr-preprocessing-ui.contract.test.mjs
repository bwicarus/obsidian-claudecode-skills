import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const read = (name) => fs.readFileSync(path.join(ROOT, name), "utf8");
const VIEW = read("ios/BWReader/App/ReaderLocalLibraryView.swift");
const PI = read("ios/BWReader/App/ReaderPiBookOCR.swift");
const REMOTE = read("ios/BWReader/App/ReaderRemoteLibrary.swift");
const SETTINGS = read("ios/BWReader/App/NativeReaderToolsView.swift");

test("each PDF exposes independent Apple, Pi, and PC preprocessing lifecycles", () => {
  assert.match(VIEW, /Label\("本机预处理"/);
  assert.match(VIEW, /Label\("Pi \/ PC 预处理"/);
  assert.match(VIEW, /executor: "pi"/);
  assert.match(VIEW, /executor: "pc"/);
  for (const action of ["startLocal", "pause", "resume", "cancel", "retry"]) {
    assert.match(VIEW, new RegExp(`nativeOCR\\.${action}\\(`));
  }
  for (const action of ["pause", "resume", "cancel", "retry"]) {
    assert.match(VIEW, new RegExp(`controlPi\\("${action}"`));
  }
  for (const stage of ["textProgress", "wordProgress", "formulaProgress"]) {
    assert.match(VIEW, new RegExp(`status\\.${stage}`));
    assert.match(VIEW, new RegExp(`job\\.${stage}`));
  }
  assert.match(VIEW, /EPUB 使用其可重排文字层/);
  assert.match(VIEW, /EPUB 当前不支持 Pi 的 PDF 页图预处理/);
});

test("PC preprocessing is an explicit Pi-coordinated executor with live availability", () => {
  assert.match(PI, /struct ReaderOCRExecutorStatus: Codable, Hashable, Identifiable, Sendable/);
  assert.match(PI, /pdf\/api\/library\/ocr\/executors/);
  assert.match(PI, /func refreshExecutors\(cookies:/);
  assert.match(PI, /@Published private\(set\) var executorStatuses:/);
  assert.match(PI, /request\.timeoutInterval = 5/);
  assert.match(PI, /\["pi", "pc"\]\.contains\(executor\)/);
  assert.match(PI, /if executor == "pc" \{[\s\S]*body\["executor"\] = executor/);
  assert.doesNotMatch(
    PI.slice(PI.indexOf("var body = ["), PI.indexOf('if executor == "pc"')),
    /"executor"/,
  );
  assert.match(VIEW, /Menu\(executor == "pc" \? "PC 预处理" : "Pi 预处理"\)/);
  assert.match(VIEW, /Label\("此电脑 GPU", systemImage: "desktopcomputer"\)/);
  assert.match(VIEW, /executor == "pc" && !pcExecutorAcceptingJobs/);
  assert.match(VIEW, /async let executorRefresh: Void = piOCR\.refreshExecutors\(cookies: cookies\)/);
  assert.match(VIEW, /_ = await \(executorRefresh, bookRefresh\)/);
  assert.match(VIEW, /status\.lastSeenAtEpochMs/);
  assert.match(VIEW, /ageMilliseconds <= 35_000/);
  const automatic = VIEW.slice(
    VIEW.indexOf("private func startAutomaticNativeOCRIfNeeded"),
    VIEW.indexOf("private func startNativeOCR", VIEW.indexOf("private func startAutomaticNativeOCRIfNeeded")),
  );
  assert.doesNotMatch(automatic, /executor|PC 预处理|piOCR/);
});

test("preprocessing details default collapsed and keep only sheet-session expansion", () => {
  assert.match(VIEW, /@State private var expandedPreprocessingBookIDs = Set<String>\(\)/);
  assert.match(VIEW, /preprocessingToggleButton\([\s\S]*bookID: "local:/);
  assert.match(VIEW, /preprocessingToggleButton\([\s\S]*bookID: "remote:/);
  assert.match(VIEW, /if expandedPreprocessingBookIDs\.contains\("local:[\s\S]*preprocessingPanel\(localBook:/);
  assert.match(VIEW, /if expandedPreprocessingBookIDs\.contains\("remote:[\s\S]*preprocessingPanel\(remoteBook:/);
  assert.match(VIEW, /Text\("处理：\\\(summary\)"\)/);
  assert.match(VIEW, /Label\("文字、分词与公式", systemImage: "text\.viewfinder"\)/);
  assert.doesNotMatch(VIEW, /DisclosureGroup/);
  assert.doesNotMatch(VIEW, /@AppStorage[\s\S]*expandedPreprocessingBookIDs/);
});

test("progress bars count only completed pages and spell out every other state", () => {
  const fraction = VIEW.slice(
    VIEW.indexOf("private func stageFraction("),
    VIEW.indexOf("private func nativeStateTitle", VIEW.indexOf("private func stageFraction(")),
  );
  assert.match(fraction, /Double\(completed\) \/ Double\(total\)/);
  assert.doesNotMatch(fraction, /completed \+ failed|completed \+ unavailable/);
  for (const label of ["完成", "待处理", "失败", "不可用"]) {
    assert.match(VIEW, new RegExp(label));
  }
  assert.match(PI, /fractionCompleted[\s\S]*Double\(completed\) \/ Double\(total\)/);
  assert.doesNotMatch(PI.slice(0, PI.indexOf("struct ReaderPiOCRJob")), /completed \+ failed|completed \+ unavailable/);
});

test("Pi request failures stay visible and actions are never silently preference-disabled", () => {
  const refresh = PI.slice(
    PI.indexOf("func refresh("),
    PI.indexOf("func control(", PI.indexOf("func refresh(")),
  );
  assert.doesNotMatch(refresh, /status == 404|jobs\.removeValue/);
  assert.match(refresh, /recordError\([\s\S]*explicit: false/);
  assert.match(PI, /throw ReaderPiOCRError\.invalidResponse/);
  assert.match(PI, /Pi 预处理接口未部署，或服务器返回了网页而不是协议数据/);
  assert.match(PI, /http\.statusCode == 404 && responseContract != Self\.contract/);
  for (const code of ["legacy-adoption-busy", "legacy-result-incomplete", "book-ocr-busy"]) {
    assert.match(PI, new RegExp(code));
  }
  assert.doesNotMatch(VIEW, /\.onChange\(of: piOCR\.errorMessage\)/);
  assert.match(VIEW, /private func presentPiErrorIfNeeded/);
  assert.match(
    VIEW,
    /await piOCR\.control\([\s\S]*presentPiErrorIfNeeded\(for: book\)/,
  );
  assert.match(
    VIEW,
    /let imported = await piOCR\.importAvailableAttachments\([\s\S]*if !imported \{[\s\S]*presentPiErrorIfNeeded\(for: book\)/,
  );
  assert.match(VIEW, /piOCR\.error\(for: remoteBook\)/);
  assert.doesNotMatch(VIEW, /\.disabled\(\s*!recognitionPreferences\.isEnabled/);
  assert.match(VIEW, /let activeIDs = \[[\s\S]*ocrActionBookID,[\s\S]*piOCR\.activeBookID/);
});

test("cancelled row tasks stay silent and passive errors remain isolated per book", () => {
  assert.match(PI, /@Published private var bookErrors: \[String: BookError\]/);
  assert.match(PI, /let contentSHA256: String/);
  assert.match(PI, /func error\(for book: ReaderRemoteBook\) -> String\?/);
  assert.match(
    PI,
    /error\.contentSHA256\.caseInsensitiveCompare\(book\.contentSha256\)[\s\S]*== \.orderedSame/,
  );
  assert.match(PI, /contentSHA256: book\.contentSha256\.lowercased\(\)/);
  assert.match(PI, /error is CancellationError/);
  assert.match(PI, /\(error as\? URLError\)\?\.code == \.cancelled/);
  const refresh = PI.slice(
    PI.indexOf("func refresh("),
    PI.indexOf("func adoptExisting(", PI.indexOf("func refresh(")),
  );
  assert.match(refresh, /guard !Task\.isCancelled else \{ return \}/);
  assert.match(refresh, /guard !isCancellation\(error\) else \{ return \}/);
  assert.doesNotMatch(refresh, /errorBookID\s*=|errorMessage\s*=/);
  const poll = PI.slice(
    PI.indexOf("private func schedulePoll("),
    PI.indexOf("private func recordError(", PI.indexOf("private func schedulePoll(")),
  );
  assert.match(poll, /guard !self\.isCancellation\(error\) else \{ return \}/);
  assert.match(poll, /explicit: false/);
  assert.doesNotMatch(poll, /self\.errorBookID|self\.errorMessage/);
});

test("idle Pi status previews legacy results without adopting or starting them", () => {
  assert.match(PI, /struct ReaderPiOCRAdoption: Codable, Hashable, Sendable/);
  assert.match(PI, /case charCache = "char-cache"/);
  assert.match(PI, /sourceTotal == adoption\.totalPages/);
  assert.match(PI, /sources\.missing == adoption\.missingPages\.count/);
  assert.match(PI, /adoption\.available == \(sources\.missing == 0\)/);
  assert.match(PI, /\["succeeded", "pending", "failed"\]\.contains/);
  assert.match(PI, /\^ocr_\[0-9a-f\]\{20\}\$/);
  assert.match(PI, /pdf\/api\/library\/ocr\/adoption-preview/);
  assert.match(PI, /reader-library-ocr-adoption\/1/);
  const refresh = PI.slice(
    PI.indexOf("func refresh("),
    PI.indexOf("func adoptExisting(", PI.indexOf("func refresh(")),
  );
  assert.match(
    refresh,
    /client\.status\([\s\S]*if job\.state == "idle", previewsLegacyResults[\s\S]*client\.adoptionPreview\(/,
  );
  assert.doesNotMatch(refresh, /client\.adoptExisting|client\.start/);
  assert.match(PI, /previewsLegacyResults: Bool = false/);
  assert.match(VIEW, /previewsLegacyResults: expandedPreprocessingBookIDs\.contains\(/);
  assert.match(VIEW, /previewsLegacyResults \? "preview" : "status"/);
  assert.match(VIEW, /piOCR\.previewingBookID == remoteBook\.bookId/);
  assert.match(VIEW, /Text\("正在检查现有结果"\)/);
  assert.match(VIEW, /Button\("重试检查现有结果"\)/);
  const passiveView = VIEW.slice(
    VIEW.indexOf("private func refreshPiStatus("),
    VIEW.indexOf("private func upload(", VIEW.indexOf("private func refreshPiStatus(")),
  );
  assert.doesNotMatch(passiveView, /ocrErrorMessage|adoptExistingPiResult/);
  const statusSections = VIEW.slice(
    VIEW.indexOf("private var statusSections"),
    VIEW.indexOf("private func errorSection", VIEW.indexOf("private var statusSections")),
  );
  assert.doesNotMatch(statusSections, /piOCR\.errorMessage/);
});

test("legacy Pi results require an explicit adopt and then use normal job and attachment import", () => {
  assert.match(VIEW, /Text\("已有 Pi 结果，可采用"\)/);
  assert.match(VIEW, /Button\("采用现有 Pi 结果"\)/);
  assert.match(VIEW, /piOCR\.adoption\(for: remoteBook\)/);
  assert.match(VIEW, /let job = piOCR\.job\(for: remoteBook\),\s*job\.state != "idle"/);
  const explicitAdopt = VIEW.slice(
    VIEW.indexOf("private func adoptExistingPiResult("),
    VIEW.indexOf("private func importPiAttachments(", VIEW.indexOf("private func adoptExistingPiResult(")),
  );
  assert.match(explicitAdopt, /await piOCR\.adoptExisting\(/);
  assert.match(explicitAdopt, /presentPiErrorIfNeeded\(for: book\)/);
  const clientAdopt = PI.slice(
    PI.indexOf("func adoptExisting("),
    PI.indexOf("func control(", PI.indexOf("func adoptExisting(")),
  );
  assert.match(clientAdopt, /pdf\/api\/library\/ocr\/adopt/);
  assert.match(clientAdopt, /"bookId": book\.bookId/);
  assert.match(clientAdopt, /"contentSha256": book\.contentSha256/);
  assert.doesNotMatch(clientAdopt, /"engine"/);
  assert.match(clientAdopt, /payload\.job\.pageCharsRevision == payload\.adoption\.revision/);
  const coordinatorAdopt = PI.slice(
    PI.indexOf("func adoptExisting(", PI.indexOf("final class ReaderPiOCRCoordinator")),
    PI.indexOf("func control(", PI.indexOf("func adoptExisting(", PI.indexOf("final class ReaderPiOCRCoordinator"))),
  );
  assert.match(coordinatorAdopt, /accept\([\s\S]*importsAttachments: false/);
  assert.match(coordinatorAdopt, /await importAvailableAttachments\(/);
  assert.match(coordinatorAdopt, /requiresManifest: true/);
  assert.match(coordinatorAdopt, /reportsExplicitFailure: true/);
  assert.doesNotMatch(coordinatorAdopt, /client\.start/);
});

test("published jobs require attachments while an ordinary download keeps the no-result no-op", () => {
  const importer = PI.slice(
    PI.indexOf("func importAvailableAttachments("),
    PI.indexOf("func dismissMessages", PI.indexOf("func importAvailableAttachments(")),
  );
  assert.match(importer, /requiresManifest: Bool = false/);
  assert.match(
    importer,
    /if requiresManifest \{[\s\S]*client\.attachmentManifest\([\s\S]*\} else \{[\s\S]*attachmentManifestIfAvailable/,
  );
  const explicitImport = VIEW.slice(
    VIEW.indexOf("private func importPiAttachments("),
    VIEW.indexOf("private func presentPiErrorIfNeeded", VIEW.indexOf("private func importPiAttachments(")),
  );
  assert.match(explicitImport, /requiresManifest: true/);
  assert.match(explicitImport, /reportsExplicitFailure: true/);
  const downloadFinish = VIEW.slice(
    VIEW.indexOf("private func finishDownloadedBook("),
    VIEW.indexOf("private func scheduleAutomaticNativeOCR", VIEW.indexOf("private func finishDownloadedBook(")),
  );
  assert.match(downloadFinish, /piOCR\.importAvailableAttachments\(/);
  assert.doesNotMatch(downloadFinish, /requiresManifest: true/);
});

test("automatic preprocessing is Apple-only, preference-gated, and operational", () => {
  assert.match(VIEW, /recognitionPreferences\.automaticLocalProcessingEnabled/);
  assert.match(VIEW, /book\.format == \.pdf/);
  assert.match(
    VIEW,
    /nativeOCR\.status\([\s\S]*for: book\.id,[\s\S]*expectedContentSHA256: digest[\s\S]*\)\.state == \.idle/,
  );
  assert.match(VIEW, /await nativeOCR\.waitUntilReady\(\)/);
  assert.match(VIEW, /await startNativeOCR\(book, reportFailure: true\)/);
  const auto = VIEW.slice(
    VIEW.indexOf("private func startAutomaticNativeOCRIfNeeded"),
    VIEW.indexOf("private func startNativeOCR", VIEW.indexOf("private func startAutomaticNativeOCRIfNeeded"))
  );
  assert.doesNotMatch(auto, /piOCR|startPiOCR|remote\.upload/);
  assert.match(SETTINGS, /PDF 下载到本机或首次打开时会启动这台设备的 Apple 预处理/);
  assert.match(SETTINGS, /不会上传书籍，也不会自动调用 Pi/);
});

test("local-only Pi preprocessing uploads first and never happens automatically", () => {
  const start = VIEW.slice(
    VIEW.indexOf("private func startPiOCR"),
    VIEW.indexOf("private func controlPi", VIEW.indexOf("private func startPiOCR"))
  );
  assert.ok(start.indexOf("remote.upload(") < start.indexOf("piOCR.start("));
  assert.match(
    start,
    /targetMatchesLocal = target\.map[\s\S]*caseInsensitiveCompare\(digest\)[\s\S]*if !targetMatchesLocal[\s\S]*remote\.upload\(/,
  );
  assert.match(start, /localContentSHA256: localDigest/);
  assert.match(start, /engine: engine/);
  assert.match(PI, /guard \["vision", "manga"\]\.contains\(engine\)/);
  assert.match(PI, /let engine: String\?/);
});

test("Pi-derived attachments are verified, 404-tolerant, and imported after download", () => {
  assert.match(REMOTE, /func download\([\s\S]*async -> ReaderLocalBookRecord\?/);
  assert.match(VIEW, /finishDownloadedBook\(downloaded, remoteBook: book, cookies: cookies\)/);
  assert.match(VIEW, /piOCR\.importAvailableAttachments\(/);
  assert.match(PI, /attachmentManifestIfAvailable/);
  assert.match(PI, /case \.server\(let status, _\) = error, status == 404/);
  assert.match(PI, /Int64\(payload\.count\) == entry\.size/);
  assert.match(PI, /digest\.caseInsensitiveCompare\(entry\.sha256\) == \.orderedSame/);
  assert.match(PI, /NativeBookOCRManager\.shared\.importDerivedAttachments\(/);
  assert.match(PI, /entry\.category == "derived"/);
  assert.match(PI, /entry\.mergePolicy == "immutable"/);
});

test("Pi attachment import checks the durable receipt before downloading payload bytes", () => {
  const imported = PI.slice(
    PI.indexOf("func importAvailableAttachments("),
    PI.indexOf("func dismissMessages", PI.indexOf("func importAvailableAttachments(")),
  );
  const manifest = imported.indexOf("attachmentManifestIfAvailable(");
  const receipt = imported.indexOf("hasImportedRevision(");
  const payloads = imported.indexOf("client.downloadAttachments(");
  const commit = imported.indexOf("importDerivedAttachments(");
  assert.ok(manifest >= 0);
  assert.ok(receipt > manifest);
  assert.ok(payloads > receipt);
  assert.ok(commit > payloads);
  assert.doesNotMatch(PI, /importedAttachmentRevisions/);
});

test("explicit reimport bypasses a stale durable receipt while passive import stays cached", () => {
  const imported = PI.slice(
    PI.indexOf("func importAvailableAttachments("),
    PI.indexOf("func dismissMessages", PI.indexOf("func importAvailableAttachments(")),
  );
  const explicit = VIEW.slice(
    VIEW.indexOf("private func importPiAttachments("),
    VIEW.indexOf("private func presentPiErrorIfNeeded", VIEW.indexOf("private func importPiAttachments(")),
  );
  assert.match(imported, /forceReimport: Bool = false/);
  assert.match(imported, /if !forceReimport,[\s\S]*hasImportedRevision\(/);
  assert.match(explicit, /forceReimport: true/);
});

test("every local Pi binding is content-identity-bound", () => {
  assert.match(PI, /localContentSHA256: String\?/);
  assert.match(
    PI,
    /localContentSHA256\.caseInsensitiveCompare\([\s\S]*book\.contentSha256[\s\S]*== \.orderedSame/,
  );
  assert.match(
    PI,
    /importAvailableAttachments\([\s\S]*localContentSHA256: String/,
  );
  assert.match(
    PI,
    /importDerivedAttachments\([\s\S]*expectedContentSHA256: localContentSHA256/,
  );
  assert.match(VIEW, /library\.ensureContentSHA256\(for: localBook\)/);
  assert.match(VIEW, /matchingLocalIdentity\(/);
  assert.match(VIEW, /localContentSHA256: localIdentity\?\.contentSHA256/);
});

test("opening an embedded-text PDF is never gated by preprocessing state", () => {
  const open = VIEW.slice(
    VIEW.indexOf("private func openLocal("),
    VIEW.indexOf("private func finishDownloadedBook("),
  );
  assert.ok(open.indexOf("reader.openLocalBook(") < open.indexOf("scheduleAutomaticNativeOCR("));
  assert.doesNotMatch(open, /nativeOCR\.status|recognitionPreferences\.isEnabled/);
});

test("书库列出历次预处理结果，带日期、可切换、可删除", () => {
  // 用户 2026-08-18：「我希望书库里能够删除预处理的结果，还有预处理的结果
  // 标记上日期用以区分，而不是覆盖或者拒绝进行多次预处理」。
  assert.match(VIEW, /Label\("服务器上的结果"/);
  assert.match(VIEW, /piOCR\.releases\(/);
  assert.match(VIEW, /piOCR\.activateRelease\(/);
  assert.match(VIEW, /piOCR\.deleteRelease\(/);
  // 删除是唯一会真的抹掉数据的动作 —— 必须过二次确认。
  assert.match(VIEW, /confirmationDialog\(\s*"删除这份预处理结果？"/);
  // 而且要分辨删的是不是当前生效那份：两种后果不一样，文案必须分开。
  assert.match(VIEW, /pending\.release\.isActive/);
  assert.match(VIEW, /原书不会被删除/);
  // 切换之后必须重新导入，否则服务器换了而 iPad 上还是旧结果。
  assert.match(
    VIEW,
    /activateRelease[\s\S]{0,900}importPiAttachments\(/,
  );
  // 展开面板才拉，打开书架不为每本书发请求。
  assert.match(VIEW, /\.task\(id: remoteBook\.bookId\)/);
});

test("历次结果的日期取不到时如实显示未知，不编造", () => {
  assert.match(PI, /struct ReaderPiOCRRelease/);
  // Pi 若还没升级，这些字段不会出现；非可选类型会让整条响应解码失败，
  // 把"还没升级"变成"功能坏了"。
  assert.match(PI, /publishedAtEpochMs: Int64\?/);
  assert.match(PI, /totalPages: Int\?/);
  assert.match(PI, /"日期未知"/);
});

test("一层坏掉不得连累整本书的文字层", () => {
  const STORE = read("ios/BWReader/App/NativeBookOCRStore.swift");
  // page() 每次读页第一行就调 layerState()，所以这里任何一个 throw
  // 都会让整本书每一页文字层全抛。删除功能上线后这条路径会被真实走到。
  assert.doesNotMatch(STORE, /throw NativeBookOCRError\.storage\("文字层页数与元数据不匹配"\)/);
  assert.doesNotMatch(STORE, /throw NativeBookOCRError\.storage\("当前选择的文字层不可用"\)/);
  assert.match(STORE, /selected = available\.contains\(where: \{ \$0\.layer == \.legacy \}\)/);
});
