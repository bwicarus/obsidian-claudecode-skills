import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const read = (name) => fs.readFileSync(path.join(ROOT, name), "utf8");
const REMOTE = read("ios/BWReader/App/ReaderRemoteLibrary.swift");
const PENDING = read(
  "ios/BWReader/App/ReaderBookUserStatePendingImport.swift"
);
const WEB = read("ios/BWReader/App/ReaderWebView.swift");
const LIBRARY_VIEW = read("ios/BWReader/App/ReaderLocalLibraryView.swift");

test("the Pi package GET is account, version and origin bound", () => {
  assert.match(REMOTE, /pdf\/api\/library\/user-state\/\\\(bookId\)/);
  assert.match(REMOTE, /URLQueryItem\([\s\S]*name: "contentSha256"/);
  assert.match(REMOTE, /if http\.statusCode == 404 \{ return nil \}/);
  assert.match(REMOTE, /X-Reader-Account-Scope-Digest/);
  assert.match(REMOTE, /X-Reader-User-State-Contract/);
  assert.match(REMOTE, /package\.bookId == bookId/);
  assert.match(REMOTE, /package\.contentSha256 == contentSha256/);
  assert.match(REMOTE, /completionHandler\(nil\)/);
});

test("raw package bytes and failed fetch intent survive App restarts", () => {
  assert.match(PENDING, /applicationSupportDirectory/);
  assert.match(PENDING, /PendingUserStateImports/);
  assert.match(PENDING, /let packageData: Data/);
  assert.match(PENDING, /options: \[\.atomic\]/);
  assert.match(PENDING, /reader-book-user-state-pending-fetch\/1/);
  assert.match(PENDING, /func markFetchFailure/);
  assert.match(PENDING, /func loadFetchIntent/);
  assert.match(PENDING, /A newer download replaced this hand-off/);
});

test("derived OCR and mutable user state finish independently", () => {
  const finish = LIBRARY_VIEW.slice(
    LIBRARY_VIEW.indexOf("private func finishDownloadedBook"),
    LIBRARY_VIEW.indexOf(
      "private func scheduleAutomaticNativeOCR",
      LIBRARY_VIEW.indexOf("private func finishDownloadedBook")
    )
  );
  assert.match(finish, /let derivedTask = Task/);
  assert.match(finish, /piOCR\.importAvailableAttachments/);
  assert.match(finish, /let userStateTask = Task/);
  assert.match(finish, /remote\.fetchAndStageUserState/);
  assert.match(finish, /await \(derivedTask\.value, userStateTask\.value\)/);
  assert.doesNotMatch(finish, /guard attachmentsReady/);
});

test("trusted local page readiness gates one conflict-aware atomic import", () => {
  assert.match(WEB, /waitForBookUserStateAPI/);
  assert.match(WEB, /window\.top === window/);
  assert.match(WEB, /bookUserState\.snapshotHeaders/);
  assert.match(WEB, /bookUserState\.applyAtomically/);
  assert.match(WEB, /localIsNewOrEmpty: true/);
  assert.match(WEB, /coordinator\.prepareImport/);
  assert.match(WEB, /coordinator\.commitImport/);
  assert.match(WEB, /pendingBookUserStateStore\.remove\(pending\)/);
  assert.match(WEB, /classification == \.conflict/);
  assert.match(WEB, /classification == \.localNewer/);
});

test("a stable local id cannot import state for replaced book bytes", () => {
  assert.match(WEB, /currentLocalContentDigest/);
  assert.match(WEB, /pending\.contentSha256 == digest/);
  assert.match(WEB, /beforeCommitDigest == pending\.contentSha256/);
  assert.match(WEB, /library\.ensureContentSHA256\(for: current\)/);
  assert.match(WEB, /bookUserStateContextGeneration/);
});

test("failure is visible and retained; no user state is uploaded", () => {
  assert.match(WEB, /重新打开本书可重试/);
  assert.match(WEB, /bw-native-user-state-status/);
  assert.match(REMOTE, /打开本书可重试/);
  assert.match(REMOTE, /markFetchFailure/);
  assert.doesNotMatch(REMOTE, /user-state\/[^"\n]*upload/);
  const importBody = WEB.slice(
    WEB.indexOf("private func importPendingBookUserState"),
    WEB.indexOf("private func verifyCurrentBookUserStateAccountScope")
  );
  assert.doesNotMatch(importBody, /remove\(pending\)[\s\S]*catch/);
});

test("A to B account switch is rejected before any renderer mutation", () => {
  const verify = WEB.slice(
    WEB.indexOf("private func verifyCurrentBookUserStateAccountScope"),
    WEB.indexOf("private func waitForBookUserStateAPI")
  );
  assert.match(
    verify,
    /current\.accountScopeDigest == pending\.accountScopeDigest/
  );
  assert.match(verify, /accountScopeChanged/);
  assert.match(
    PENDING,
    /当前 Pi 账户与下载书籍数据时不同，已保留待导入数据且未覆盖本机内容/
  );

  const flow = WEB.slice(
    WEB.indexOf("private func importPendingBookUserState"),
    WEB.indexOf("private func verifyCurrentBookUserStateAccountScope")
  );
  const probes = [...flow.matchAll(
    /verifyCurrentBookUserStateAccountScope\(/g
  )].map((match) => match.index);
  assert.equal(probes.length, 2);
  const prepare = flow.indexOf("coordinator.prepareImport(");
  const commit = flow.indexOf("coordinator.commitImport(");
  assert.ok(probes[0] < prepare, "scope must be checked before prepare");
  assert.ok(prepare < probes[1], "scope must be checked again after prepare");
  assert.ok(probes[1] < commit, "second scope check must precede mutation");
  assert.ok(
    commit < flow.indexOf("pendingBookUserStateStore.remove(pending)"),
    "a rejected account must retain pending bytes"
  );
});

test("missing authentication fails closed while the same account may continue", () => {
  const verify = WEB.slice(
    WEB.indexOf("private func verifyCurrentBookUserStateAccountScope"),
    WEB.indexOf("private func waitForBookUserStateAPI")
  );
  assert.match(verify, /let cookies = await remoteLibraryCookies\(\)/);
  assert.match(verify, /guard !cookies\.isEmpty else/);
  assert.match(verify, /status == 401 \|\| status == 403/);
  assert.match(verify, /authenticationUnavailable/);
  assert.match(verify, /current = payload/);
  assert.match(
    verify,
    /guard current\.accountScopeDigest == pending\.accountScopeDigest else \{[\s\S]*accountScopeChanged[\s\S]*\n\s*\}/
  );
  assert.doesNotMatch(verify, /applyAtomically|commitImport|upload/);
});
