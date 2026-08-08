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

test("each PDF exposes independent Apple and Pi preprocessing lifecycles", () => {
  assert.match(VIEW, /Label\("本机预处理"/);
  assert.match(VIEW, /Label\("Pi 预处理"/);
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
