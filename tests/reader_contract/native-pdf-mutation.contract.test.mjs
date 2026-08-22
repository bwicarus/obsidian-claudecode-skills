import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const MUTATION = read("ios/BWReader/App/ReaderNativePDFMutation.swift");
const OCR_STORE = read("ios/BWReader/App/NativeBookOCRStore.swift");
const OCR_MANAGER = read("ios/BWReader/App/NativeBookOCRManager.swift");
const WEB_VIEW = read("ios/BWReader/App/ReaderWebView.swift");
const RUNTIME = read("_server_deploy/static/pdf/native-local-runtime.js");
const PDF_READER = read("_server_deploy/pdf_reader.py");
const PROJECT = read("ios/BWReader/project.yml");

test("native PDF mutation prepares and validates a selectable sibling staging file before replacement", () => {
  assert.match(MUTATION, /actor ReaderNativePDFMutationActor/);
  assert.match(MUTATION, /\.staging\.pdf/);
  assert.match(MUTATION, /UIGraphicsPDFRenderer\(bounds:/);
  assert.match(MUTATION, /NSAttributedString/);
  assert.match(MUTATION, /source\.insert\(/);
  assert.match(MUTATION, /source\.removePage\(at:/);
  assert.match(MUTATION, /guard source\.write\(to: stagingURL\)/);
  assert.match(MUTATION, /PDFDocument\(url: stagingURL\)/);
  assert.match(MUTATION, /page\.string\?\.contains\("用户插入页"\)/);
  assert.match(PROJECT, /- path: App/);
});

test("native PDF mutation preserves immutable OCR layout for migrated pages", () => {
  const migrated = MUTATION.slice(
    MUTATION.indexOf("private static func migratedOCRPage("),
    MUTATION.indexOf("private static func migratedFormulaRegion("),
  );
  assert.match(migrated, /chars: value\.chars,[\s\S]*layout: value\.layout,[\s\S]*furigana: value\.furigana/);
});

test("native PDF replacement has an old-byte fence, sibling backup and verified rollback", () => {
  assert.match(MUTATION, /currentIdentity\.byteCount == mutation\.oldIdentity\.byteCount/);
  assert.match(MUTATION, /currentIdentity\.modifiedAt == mutation\.oldIdentity\.modifiedAt/);
  assert.match(MUTATION, /currentIdentity\.sha256 == mutation\.oldIdentity\.sha256/);
  assert.match(MUTATION, /replaceItemAt\([\s\S]*withItemAt: mutation\.stagingURL,[\s\S]*backupItemName:/);
  assert.match(MUTATION, /replacementIdentity\.sha256[\s\S]*stagedContentSHA256/);
  assert.match(MUTATION, /func restoreBackup/);
  assert.match(MUTATION, /restored\.sha256 == mutation\.oldIdentity\.sha256/);
  assert.match(MUTATION, /FileHandle\(forReadingFrom:/);
  assert.doesNotMatch(MUTATION, /Data\(contentsOf: mutation\.originalURL/);
  assert.match(MUTATION, /reader-native-pdf-mutation-journal\/1/);
  assert.match(MUTATION, /private func mutationJournalURL/);
  assert.match(MUTATION, /\.bw-pdf-mutation-\\\(localBookID\)\.journal\.json/);
  assert.doesNotMatch(MUTATION, /"\.bw-pdf-mutation-journal\.json"/);
  assert.match(MUTATION, /case preparing/);
  assert.match(MUTATION, /stagedContentSHA256 == nil/);
  assert.match(MUTATION, /cleanupPDFMutationArtifacts\([\s\S]*stagedContentSHA256: nil/);
  assert.match(MUTATION, /case committed/);
  assert.match(MUTATION, /journal\.phase = \.committed[\s\S]*writeJournal[\s\S]*removeItem\(at: mutation\.backupURL\)/);
  assert.match(MUTATION, /func recover\(/);
  assert.match(MUTATION, /rollbackPreparedMutation/);
  assert.match(MUTATION, /private func cleanupMutationArtifacts[\s\S]*failedReplacementURL/);
  assert.match(MUTATION, /stagePDFMutation/);
  assert.match(MUTATION, /migrateOCRContentDirectory/);
  assert.match(MUTATION, /NativeBookOCRSelectionCorrectionEnvelope/);
});

test("native PDF mutation fences OCR writers through one shared per-book lease", () => {
  assert.match(OCR_STORE, /static let shared = NativeBookOCRSidecarStore\(\)/);
  assert.match(OCR_STORE, /struct NativeBookOCRPDFMutationLease/);
  assert.match(OCR_STORE, /func beginPDFMutationLease\(/);
  assert.match(OCR_STORE, /pdfMutationLeasesByDigest/);
  assert.match(OCR_STORE, /private func assertPDFMutationWriteAllowed\(/);
  assert.match(OCR_STORE, /func stagePDFMutation\([\s\S]*transform: @Sendable \(URL\) throws -> Void/);
  assert.match(OCR_STORE, /func finishPDFMutationLease\(/);
  assert.match(OCR_MANAGER, /func beginPDFMutationLease\(/);
  assert.match(OCR_MANAGER, /await waitForWriteOperations\(bookID:/);
  assert.match(OCR_MANAGER, /func rebuildPDFMutationStatus\(/);
  assert.match(OCR_MANAGER, /func finishPDFMutationLease\(/);
  assert.match(MUTATION, /ocrLeaseToken/);
  assert.match(MUTATION, /func acknowledgeRecovery\(/);

  const beginLease = OCR_MANAGER.slice(
    OCR_MANAGER.indexOf("func beginPDFMutationLease("),
    OCR_MANAGER.indexOf("func rebuildPDFMutationStatus("),
  );
  assert.ok(beginLease.indexOf("pdfMutationLeases[bookID] = lease") >= 0);
  assert.ok(
    beginLease.indexOf("pdfMutationLeases[bookID] = lease") <
      beginLease.indexOf("await runningTask.value"),
    "the per-book gate must close before the first suspension",
  );
  assert.ok(
    beginLease.indexOf("await waitForWriteOperations") <
      beginLease.indexOf("store.beginPDFMutationLease(lease)"),
    "manual/import writers must drain before the store fence begins",
  );

  const settlement = WEB_VIEW.slice(
    WEB_VIEW.indexOf("private func settleNativePDFMutation("),
    WEB_VIEW.indexOf("private func handleNativePDFMutation("),
  );
  assert.ok(
    settlement.indexOf("rebuildPDFMutationStatus") <
      settlement.indexOf("acknowledgeRecovery"),
    "resolved OCR status must persist before the durable journal is removed",
  );
  assert.ok(
    settlement.indexOf("acknowledgeRecovery") <
      settlement.indexOf("finishPDFMutationLease"),
    "cleanup must finish before the OCR writer fence is released",
  );
});

test("shared OCR store isolates PDF mutation leases by book even when source digests match", () => {
  assert.match(
    OCR_STORE,
    /private var pdfMutationLeasesByDigest:\s*\[String: \[String: NativeBookOCRPDFMutationLease\]\] = \[:\]/,
  );
  assert.match(
    OCR_STORE,
    /private var pdfMutationTargetLeaseByDigest:\s*\[String: NativeBookOCRPDFMutationLease\] = \[:\]/,
  );

  const begin = OCR_STORE.slice(
    OCR_STORE.indexOf("func beginPDFMutationLease("),
    OCR_STORE.indexOf("func stagePDFMutation("),
  );
  assert.match(
    begin,
    /registerPDFMutationDigestLease\(\s*lease\.oldContentSHA256,\s*lease: lease\s*\)/,
  );
  assert.doesNotMatch(begin, /相同 PDF 内容已有另一项 OCR 改页租约/);
  assert.doesNotMatch(begin, /pdfMutationTargetLeaseByDigest/);

  const register = OCR_STORE.slice(
    OCR_STORE.indexOf("private func registerPDFMutationDigestLease("),
    OCR_STORE.indexOf("private func assertPDFMutationWriteAllowed("),
  );
  assert.match(register, /var leases = pdfMutationLeasesByDigest\[digest\] \?\? \[:\]/);
  assert.match(register, /if let active = leases\[lease\.bookID\], active != lease/);
  assert.match(register, /leases\[lease\.bookID\] = lease/);

  const stage = OCR_STORE.slice(
    OCR_STORE.indexOf("func stagePDFMutation("),
    OCR_STORE.indexOf("func installPDFMutation("),
  );
  assert.match(
    stage,
    /if let active = pdfMutationTargetLeaseByDigest\[stagedDigest\],[\s\S]*active != lease/,
  );
  assert.match(stage, /registerPDFMutationDigestLease\(stagedDigest, lease: lease\)/);

  const fence = OCR_STORE.slice(
    OCR_STORE.indexOf("private func assertPDFMutationWriteAllowed("),
    OCR_STORE.indexOf("private func validatePDFMutationTicket("),
  );
  assert.match(fence, /pdfMutationLeasesByDigest\[digest\]\?\[bookID\] == nil/);
  assert.match(fence, /pdfMutationTargetLeaseByDigest\[digest\] == nil/);

  const finish = OCR_STORE.slice(
    OCR_STORE.indexOf("func finishPDFMutationLease("),
    OCR_STORE.indexOf("func page("),
  );
  assert.match(finish, /leases\.removeValue\(forKey: lease\.bookID\)/);
  assert.match(finish, /if leases\.isEmpty[\s\S]*removeValue\(forKey: digest\)/);
  assert.match(
    finish,
    /pdfMutationTargetLeaseByDigest\.filter \{ \$0\.value != lease \}/,
  );

  // Executable reference evidence for the nested-index behavior. The Swift
  // source assertions above bind this model to the Store's actual key shape.
  const leasesByBook = new Map();
  const leasesByDigest = new Map();
  const targetByDigest = new Map();
  const beginLease = (bookID, token, digest) => {
    const currentBook = leasesByBook.get(bookID);
    if (currentBook && currentBook.token !== token) {
      throw new Error("same book is busy");
    }
    const leases = new Map(leasesByDigest.get(digest) || []);
    const active = leases.get(bookID);
    if (active && active !== token) throw new Error("same book is busy");
    leases.set(bookID, token);
    leasesByDigest.set(digest, leases);
    leasesByBook.set(bookID, { token, digests: new Set([digest]) });
  };
  const stageLease = (bookID, token, digest) => {
    const currentBook = leasesByBook.get(bookID);
    if (!currentBook || currentBook.token !== token) throw new Error("stale lease");
    const target = targetByDigest.get(digest);
    if (target && (target.bookID !== bookID || target.token !== token)) {
      throw new Error("same target is busy");
    }
    const leases = new Map(leasesByDigest.get(digest) || []);
    leases.set(bookID, token);
    leasesByDigest.set(digest, leases);
    currentBook.digests.add(digest);
    targetByDigest.set(digest, { bookID, token });
  };
  const canWrite = (bookID, digest) =>
    !leasesByBook.has(bookID) && !targetByDigest.has(digest);
  const finishLease = (bookID, token) => {
    const currentBook = leasesByBook.get(bookID);
    if (!currentBook || currentBook.token !== token) throw new Error("stale lease");
    leasesByBook.delete(bookID);
    for (const digest of currentBook.digests) {
      const leases = new Map(leasesByDigest.get(digest) || []);
      if (leases.get(bookID) === token) leases.delete(bookID);
      if (leases.size) leasesByDigest.set(digest, leases);
      else leasesByDigest.delete(digest);
      const target = targetByDigest.get(digest);
      if (target?.bookID === bookID && target.token === token) {
        targetByDigest.delete(digest);
      }
    }
  };
  const sharedDigest = "a".repeat(64);
  const targetA = "b".repeat(64);
  const targetB = "c".repeat(64);
  beginLease("book-a", "ticket-a", sharedDigest);
  assert.equal(canWrite("book-b", sharedDigest), true,
    "A's source lease must not reject B's legitimate shared-sidecar write");
  beginLease("book-b", "ticket-b", sharedDigest);
  stageLease("book-a", "ticket-a", targetA);
  stageLease("book-b", "ticket-b", targetB);
  assert.deepEqual(
    [...leasesByDigest.get(sharedDigest).keys()],
    ["book-a", "book-b"],
  );
  assert.equal(targetByDigest.get(targetA).bookID, "book-a");
  assert.equal(targetByDigest.get(targetB).bookID, "book-b");
  assert.throws(
    () => beginLease("book-a", "ticket-a2", sharedDigest),
    /same book is busy/,
  );
  beginLease("book-c", "ticket-c", sharedDigest);
  assert.throws(
    () => stageLease("book-c", "ticket-c", targetA),
    /same target is busy/,
  );
  finishLease("book-a", "ticket-a");
  assert.equal(leasesByDigest.get(sharedDigest).get("book-b"), "ticket-b");
  assert.equal(leasesByDigest.get(targetB).get("book-b"), "ticket-b");
  assert.equal(targetByDigest.get(targetB).bookID, "book-b");
});

test("native OCR formula IDs are page-rewritten on insert/delete for Apple and Pi origins", () => {
  assert.match(MUTATION, /value\.id\.hasPrefix\("pi-formula-p"\)/);
  assert.match(MUTATION, /value\.id\.hasPrefix\("formula-p"\)/);
  assert.match(MUTATION, /let expectedPrefix = "\\\(prefix\)\\\(oldPage\)-"/);
  assert.match(MUTATION, /id: "\\\(prefix\)\\\(newPage\)-\\\(suffix\)"/);
  assert.match(MUTATION, /公式区域 ID 不支持安全迁移/);
  assert.match(MUTATION, /公式区域页身份不匹配/);

  const pageMap = (page, operation, pivot) => {
    if (operation === "insert") return page >= pivot ? page + 1 : page;
    if (operation === "delete") {
      if (page === pivot) return null;
      return page > pivot ? page - 1 : page;
    }
    return page === pivot ? null : page;
  };
  const rewrite = (id, oldPage, newPage) => {
    const prefix = id.startsWith("pi-formula-p")
      ? "pi-formula-p" : "formula-p";
    const suffix = id.slice(`${prefix}${oldPage}-`.length);
    return `${prefix}${newPage}-${suffix}`;
  };
  for (const prefix of ["formula-p", "pi-formula-p"]) {
    const insertedPage = pageMap(4, "insert", 3);
    assert.equal(rewrite(`${prefix}4-region-a`, 4, insertedPage),
      `${prefix}5-region-a`);
    const deletedPage = pageMap(4, "delete", 3);
    assert.equal(rewrite(`${prefix}4-region-b`, 4, deletedPage),
      `${prefix}3-region-b`);
    assert.equal(pageMap(3, "delete", 3), null);
  }
});

test("native PDF actor isolates books and navigation distinguishes outgoing rollback from incoming recovery", () => {
  assert.match(MUTATION, /private var prepared: \[String: PreparedMutation\]/);
  assert.match(MUTATION, /private var preparedTicketByBook: \[String: String\]/);
  assert.match(MUTATION, /preparedTicketByBook\[request\.localBookID\] == nil/);
  assert.match(MUTATION, /func rollbackForOutgoingNavigation\(/);

  const openBook = WEB_VIEW.slice(
    WEB_VIEW.indexOf("private func openLocalBook("),
    WEB_VIEW.indexOf("private func cancelPendingLocalBookNavigation("),
  );
  const outgoing = openBook.indexOf("outgoingRollback: true");
  const clearBinding = openBook.indexOf(
    "nativePiGateway?.updateTrustedRemoteBookBinding(nil)",
  );
  const incomingComment = openBook.indexOf("Incoming crash recovery runs");
  const serverOpen = openBook.indexOf("localRuntimeServer.open(");
  assert.ok(outgoing >= 0 && outgoing < clearBinding,
    "outgoing rollback must finish before the page identity is cleared");
  assert.ok(incomingComment >= 0 && incomingComment < serverOpen,
    "incoming startup recovery must finish before open/JS");
});

test("clean PDF boot proves no native journal before returning without hashing the whole book", () => {
  const recoverCase = WEB_VIEW.slice(
    WEB_VIEW.indexOf("case .recover("),
    WEB_VIEW.indexOf("private func refreshLocalBookAfterPDFMutation("),
  );
  const journalProbe = recoverCase.indexOf("hasUnfinishedMutation(book: access)");
  const cleanReturn = recoverCase.indexOf("if !hasExpectedIdentity, !hasUnfinishedMutation");
  const verifiedRecovery = recoverCase.indexOf("settleNativePDFMutation(");

  assert.ok(journalProbe >= 0 && journalProbe < cleanReturn);
  assert.ok(cleanReturn >= 0 && cleanReturn < verifiedRecovery,
    "clean open must return before the verified recovery path can hash PDF bytes");
  assert.match(recoverCase, /"outcome": ReaderNativePDFMutationRecoveryReceipt[\s\S]*\.Outcome\.none\.rawValue/);
  assert.match(recoverCase, /"contentSHA256": currentLocalBookContentSHA256[\s\S]*NSNull\(\)/);
  assert.match(recoverCase, /let hasExpectedIdentity = ticket != nil[\s\S]*oldContentSHA256 != nil[\s\S]*stagedContentSHA256 != nil/);
});

test("the strict main-frame bridge and host keep replacement inside the current local book lifecycle", () => {
  assert.match(MUTATION, /reader-native-pdf-mutation-request\/1/);
  assert.match(MUTATION, /reader-native-pdf-mutation-response\/1/);
  assert.match(MUTATION, /message\.frameInfo\.isMainFrame/);
  assert.match(MUTATION, /message\.webView === webView/);
  assert.match(MUTATION, /Set\(body\.keys\) == Set\(/);
  assert.match(MUTATION, /localbook-\[a-f0-9\]\{64\}/);
  assert.match(WEB_VIEW, /ReaderNativePDFMutationBridge\(/);
  assert.match(WEB_VIEW, /currentLocalBook[\s\S]*book\.id == request\.localBookID/);
  assert.match(WEB_VIEW, /refreshLocalBookAfterPDFMutation/);
  assert.match(WEB_VIEW, /library\.ensureContentSHA256\(for: refreshed\)/);
  assert.match(WEB_VIEW, /localRuntimeServer\.open\(access\)/);
  assert.match(WEB_VIEW, /nativeBookOCRBridge\?\.updateTrustedContext/);
  assert.match(WEB_VIEW, /private func settleNativePDFMutation\(/);
  assert.match(MUTATION, /func rollbackForOutgoingNavigation\(/);
  assert.match(WEB_VIEW, /outgoingRollback: true[\s\S]*applyNativePDFRecoverySettlement/);
  assert.match(WEB_VIEW, /Incoming crash recovery runs before localRuntimeServer\.open/);
});

test("local runtime splits PDF bytes and page anchors into a two-phase rollback-capable job", () => {
  assert.match(RUNTIME, /function nativePDFMutationSnapshot/);
  assert.match(RUNTIME, /PDF_MUTATION_DOCUMENT_KINDS/);
  assert.match(RUNTIME, /document-highlights/);
  assert.match(RUNTIME, /document-notes-legacy/);
  assert.match(RUNTIME, /card-placements/);
  assert.match(RUNTIME, /entity-references/);
  assert.match(RUNTIME, /reader-positions/);
  assert.match(RUNTIME, /applyNativePDFMutationSnapshot/);
  assert.match(RUNTIME, /rollbackNativePDFMutationSnapshot/);
  assert.match(RUNTIME, /nativePDFMutationRequest\('prepare'/);
  assert.match(RUNTIME, /nativePDFMutationRequest\('commit'/);
  assert.match(RUNTIME, /nativePDFMutationRequest\('finalize'/);
  assert.match(RUNTIME, /nativePDFMutationRequest\('recover'/);
  assert.match(RUNTIME, /reader-native-pdf-mutation-web-journal\/1/);
  assert.match(RUNTIME, /recoverNativePDFMutationOnBoot/);
  assert.match(RUNTIME, /nativePDFMutationJournalRecord/);
  assert.match(RUNTIME, /PDF_MUTATION_PAGE_ANCHOR_DOMAINS/);
  assert.match(RUNTIME, /pdf-ocr-fix/);
  assert.match(RUNTIME, /assistant-convo/);
  assert.match(RUNTIME, /render-caches/);
  assert.match(RUNTIME, /NATIVE_PDF_MUTATION_JOB_PREFIX = 'npj_'/);
  assert.match(RUNTIME, /status: 'unknown'/);
  assert.match(RUNTIME, /PDF 正在改页；为避免把新数据写到旧页号/);
  assert.match(RUNTIME, /function beginNativePDFWriterBarrier\(\)/);
  assert.match(RUNTIME, /nativePDFActiveWriters === 0/);
  assert.match(RUNTIME, /beginNativePDFWriterBarrier[\s\S]*catch\(function \(error\) \{[\s\S]*nativePDFWriterAccepting = true/);
  assert.match(RUNTIME, /endNativePDFWriterBarrier\(writerBarrier\)[\s\S]*activeNativePDFMutationJob = null/);
  assert.match(RUNTIME, /acquireNativePDFWriterLease\('assistant-sse'\)/);
  assert.match(RUNTIME, /withNativePDFWriter\('assistant-voice'/);
  assert.match(RUNTIME, /withNativePDFWriter\('sync-batch'/);
  assert.match(RUNTIME, /assertNativePDFWriterLease\(writerLease\)/);
});

test("native PDF mutation policy covers every server page-anchor domain plus render caches", () => {
  const pythonBlock = PDF_READER.match(
    /PAGE_ANCHOR_MIGRATIONS\s*=\s*\[([\s\S]*?)\n\]/
  );
  const runtimeBlock = RUNTIME.match(
    /PDF_MUTATION_PAGE_ANCHOR_DOMAINS\s*=\s*Object\.freeze\(\[([\s\S]*?)\n\s*\]\)/
  );
  assert.ok(pythonBlock, "server PAGE_ANCHOR_MIGRATIONS must remain inspectable");
  assert.ok(runtimeBlock, "native domain policy must remain inspectable");

  const serverDomains = [...pythonBlock[1].matchAll(/\("([^"]+)",\s*_[^)]+\)/g)]
    .map((match) => match[1]);
  const nativeDomains = [...runtimeBlock[1].matchAll(/\['([^']+)',\s*'[^']+'\]/g)]
    .map((match) => match[1]);

  assert.equal(serverDomains.length, 19, "server page-anchor registry unexpectedly changed");
  assert.deepEqual(nativeDomains, [...serverDomains, "render-caches"]);
  assert.equal(new Set(nativeDomains).size, 20, "native migration domains must be unique");
});
