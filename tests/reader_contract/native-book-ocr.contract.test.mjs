import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const MODELS = read("ios/BWReader/App/NativeBookOCRModels.swift");
const PROCESSOR = read("ios/BWReader/App/NativeBookOCRProcessor.swift");
const STORE = read("ios/BWReader/App/NativeBookOCRStore.swift");
const MANAGER = read("ios/BWReader/App/NativeBookOCRManager.swift");
const BRIDGE = read("ios/BWReader/App/NativeBookOCRBridge.swift");

test("native book OCR exposes durable manual lifecycle and staged progress", () => {
  assert.match(MANAGER, /final class NativeBookOCRManager: ObservableObject/);
  assert.match(MANAGER, /static let shared = NativeBookOCRManager\(\)/);
  for (const action of ["startLocal", "pause", "resume", "cancel", "retry"]) {
    assert.match(MANAGER, new RegExp(`func ${action}\\(`));
  }
  for (const field of [
    "textProgress", "wordProgress", "formulaProgress",
    "formulaPendingRegions", "formulaFailedRegions",
  ]) {
    assert.match(MODELS, new RegExp(`let ${field}:`));
  }
  assert.match(MODELS, /var canPause: Bool/);
  assert.match(MODELS, /var canResume: Bool/);
  assert.match(MODELS, /var canRetry: Bool/);
  assert.match(STORE, /writeStatus\(_ status: NativeBookOCRBookStatus\)/);
  assert.match(STORE, /App 上次退出时已保存进度/);
  assert.match(MANAGER, /recordStatusPersistenceFailure/);
  assert.doesNotMatch(MANAGER, /try\? await store\.writeStatus/);
});

test("Apple pass uses structured Vision boxes and real NaturalLanguage words", () => {
  assert.match(PROCESSOR, /import Vision/);
  assert.match(PROCESSOR, /VNRecognizeTextRequest\(\)/);
  assert.match(PROCESSOR, /try request\.supportedRecognitionLanguages\(\)/);
  assert.match(PROCESSOR, /supportedLanguages\.contains\(\$0\)/);
  assert.match(PROCESSOR, /guard !requestedLanguages\.isEmpty/);
  assert.match(PROCESSOR, /VNErrorCode\.unsupportedComputeDevice\.rawValue/);
  assert.match(PROCESSOR, /unsupportedCodes\.contains\(nsError\.code\)/);
  assert.match(PROCESSOR, /candidate\.boundingBox\(for: range\)/);
  assert.match(PROCESSOR, /import NaturalLanguage/);
  assert.match(PROCESSOR, /NLTokenizer\(unit: \.word\)/);
  assert.match(PROCESSOR, /tokenizer\.setLanguage\(language\)/);
  assert.doesNotMatch(PROCESSOR, /ImageAnalyzer|\.transcript/);
  assert.match(MODELS, /var w: Int/);
  assert.match(MODELS, /case ready\s*\n\s*case partial\s*\n\s*case unavailable/);
});

test("rotated and cropped PDF geometry maps to the displayed Reader viewport", () => {
  assert.match(PROCESSOR, /cropBoxX: Double\(bounds\.minX\)/);
  assert.match(PROCESSOR, /cropBoxY: Double\(bounds\.minY\)/);
  assert.match(PROCESSOR, /case 90, 270:/);
  assert.match(PROCESSOR, /CGSize\(width: unrotatedHeight, height: unrotatedWidth\)/);
  assert.match(PROCESSOR, /PDFPage\.thumbnail is already rotated for display/);
  assert.match(PROCESSOR, /\[0, 90, 180, 270\]\.contains\(rotation\)/);

  const displayed = (width, height, rotation) => {
    const normalized = ((rotation % 360) + 360) % 360;
    return normalized === 90 || normalized === 270
      ? [height, width]
      : [width, height];
  };
  assert.deepEqual(displayed(600, 800, 0), [600, 800]);
  assert.deepEqual(displayed(600, 800, 90), [800, 600]);
  assert.deepEqual(displayed(600, 800, 180), [600, 800]);
  assert.deepEqual(displayed(600, 800, 270), [800, 600]);
  assert.deepEqual(displayed(600, 800, -90), [800, 600]);

  const pageRect = (box, width, height) => ({
    x: box.x * width,
    y: (1 - box.y - box.height) * height,
    width: box.width * width,
    height: box.height * height,
  });
  const box = { x: 0.25, y: 0.5, width: 0.25, height: 0.125 };
  assert.deepEqual(pageRect(box, 600, 800), {
    x: 150, y: 300, width: 150, height: 100,
  });
  assert.deepEqual(pageRect(box, 800, 600), {
    x: 200, y: 225, width: 200, height: 75,
  });

  // Embedded PDFKit boxes are in absolute crop-box page space. A non-zero
  // crop origin must be removed before converting the bottom-left PDF origin
  // into the Reader's top-left viewport.
  const embeddedRect = (raw, crop) => ({
    x: raw.x - crop.x,
    y: crop.y + crop.height - (raw.y + raw.height),
    width: raw.width,
    height: raw.height,
  });
  assert.deepEqual(
    embeddedRect(
      { x: 110, y: 220, width: 30, height: 10 },
      { x: 100, y: 200, width: 600, height: 800 },
    ),
    { x: 10, y: 770, width: 30, height: 10 },
  );
});

test("page sidecars bind full content identity, geometry, and engine revision", () => {
  assert.match(MODELS, /reader-native-page-characters\/1/);
  for (const key of [
    "contentSHA256", "pageWidth", "pageHeight", "rotation",
    "geometryDigest", "engineRevision", "chars", "furigana",
    "wordSegmentation", "characterGeometry", "formulaCoverage", "formulaRegions",
  ]) {
    assert.match(MODELS, new RegExp(`let ${key}:`));
  }
  for (const key of ["c", "x0", "y0", "x1", "y1", "sp", "w", "b", "bk"]) {
    assert.match(MODELS, new RegExp(`(?:var|let) ${key}:`));
  }
  assert.match(PROCESSOR, /reader-native-page-geometry\/1/);
  assert.match(PROCESSOR, /contentSHA256\.lowercased\(\)/);
  assert.match(PROCESSOR, /engineRevision/);
  assert.match(STORE, /Data\(contentsOf: url\)/);
  assert.match(STORE, /write\(to: url, options: \.atomic\)/);
});

test("formula semantics stay honest and cannot turn Vision glyphs into fake LaTeX", () => {
  assert.match(MODELS, /case unknown/);
  assert.match(MODELS, /case unavailable/);
  assert.match(MODELS, /case partial/);
  assert.match(MODELS, /let latex: String\?/);
  assert.match(MODELS, /let multiline: Bool\?/);
  assert.match(PROCESSOR, /state: \.pending/);
  assert.match(PROCESSOR, /latex: nil/);
  assert.match(PROCESSOR, /if meaningfulCharacters\.isEmpty, !formulaRegions\.isEmpty/);
  assert.match(PROCESSOR, /do not leak its glyph guess into/);
  assert.match(PROCESSOR, /Formula-like observations are deliberately excluded/);
  assert.match(PROCESSOR, /weightedConfidence \/ qualityWeight/);
  assert.doesNotMatch(PROCESSOR, /latex:\s*(?:candidate|text)/);
});

test("Apple failures are persisted and never start Pi automatically", () => {
  assert.match(MANAGER, /不会自动改用 Pi/);
  assert.match(MANAGER, /手动选择 Pi 预处理/);
  assert.match(PROCESSOR, /status: \.failed/);
  assert.match(PROCESSOR, /error: message/);
  assert.doesNotMatch(PROCESSOR, /ReaderPi|startPi|PiGateway/);
  assert.doesNotMatch(MANAGER, /startPi|ReaderPi|PiGateway/);
  const explicitImport = MANAGER.indexOf("func importDerivedAttachments(");
  assert.ok(explicitImport >= 0, "Pi data may only enter through explicit import");
});

test("Pi attachments import by opaque id as one immutable derived revision", () => {
  assert.match(MODELS, /reader-book-attachments\/1/);
  for (const field of [
    "attachmentId", "kind", "category", "mergePolicy", "mediaType",
    "size", "sha256", "downloadUrl",
  ]) {
    assert.match(MODELS, new RegExp(`let ${field}:`));
  }
  assert.match(STORE, /files: \[String: Data\]/);
  assert.match(STORE, /entry\.category == "derived"/);
  assert.match(STORE, /manifest\.schema == 1/);
  assert.match(STORE, /manifest\.category == "derived"/);
  assert.match(STORE, /manifest\.mergePolicy == "immutable"/);
  assert.match(STORE, /entry\.mergePolicy == "immutable"/);
  assert.match(STORE, /Self\.sha256\(data\) == entry\.sha256\.lowercased\(\)/);
  assert.match(STORE, /fileManager\.replaceItemAt\(/);
  assert.match(STORE, /reader-native-book-ocr-import-receipt\/1/);
  assert.match(STORE, /func hasImportedRevision\(/);
  assert.match(STORE, /importReceiptURL\(/);
  assert.match(MANAGER, /func hasImportedRevision\(/);
  assert.match(MANAGER, /expectedContentSHA256\.caseInsensitiveCompare\(/);
  assert.match(STORE, /source: \.pi/);
  assert.doesNotMatch(MODELS, /absolutePath|fileURL|bookmark/);
});

test("native page text bridge data is available without coupling the core to UI files", () => {
  assert.match(MODELS, /reader-native-page-text-update\/1/);
  assert.match(MANAGER, /func pageCharacters\(/);
  assert.match(MANAGER, /func pageStatus\(/);
  assert.match(MANAGER, /func search\(/);
  assert.match(MODELS, /struct NativeBookOCRSearchResult/);
  assert.match(MODELS, /let incomplete: Bool/);
  assert.match(MANAGER, /func activate\(/);
  assert.match(MANAGER, /private var activeContentSHA256/);
  assert.match(MANAGER, /func waitUntilReady\(\) async/);
  assert.match(MANAGER, /private let statusLoadTask/);
  assert.match(MANAGER, /func pageCharacters\([\s\S]*expectedContentSHA256: String/);
  assert.match(MANAGER, /func search\([\s\S]*expectedContentSHA256: String/);
  assert.match(MANAGER, /try Self\.validateCurrentBook\(book\)/);
  const restore = MANAGER.slice(
    MANAGER.indexOf("func waitUntilReady"),
    MANAGER.indexOf("func activate"),
  );
  assert.doesNotMatch(
    restore,
    /activeContentSHA256\[/,
    "persisted statuses are history and cannot activate an opaque book id",
  );
});

test("native page text reply bridge is main-frame, trusted, strict, and passive", () => {
  assert.match(BRIDGE, /WKScriptMessageHandlerWithReply/);
  assert.match(BRIDGE, /static let messageName = "bwNativePageText"/);
  assert.match(BRIDGE, /reader-native-page-text-request\/1/);
  assert.match(BRIDGE, /reader-native-page-text-response\/1/);
  assert.match(BRIDGE, /message\.frameInfo\.isMainFrame/);
  assert.match(BRIDGE, /message\.webView === webView/);
  assert.match(BRIDGE, /localBookID == expectedLocalBookID/);
  assert.match(BRIDGE, /Set\(value\.keys\) == common\.union\(\["page"\]\)/);
  assert.match(BRIDGE, /Set\(value\.keys\) == common\.union\(\["query", "limit"\]\)/);
  assert.match(BRIDGE, /func pageReply\(/);
  assert.match(BRIDGE, /func statusReply\(/);
  assert.match(BRIDGE, /func searchReply\(/);
  assert.match(BRIDGE, /expectedContentSHA256: String\? = nil/);
  assert.match(BRIDGE, /manager\.activatedContentSHA256/);
  assert.match(BRIDGE, /await manager\.readyStatus/);
  assert.match(BRIDGE, /Restored jobs never activate themselves/);
  assert.doesNotMatch(BRIDGE, /startLocal|\.resume\(|\.retry\(|startPi/);
});

test("native update event and page formula reply keep the exact public shape", () => {
  assert.match(BRIDGE, /'bw:native-page-text-updated'/);
  for (const key of [
    "contract", "localBookId", "page", "state", "source", "revision",
  ]) {
    assert.match(BRIDGE, new RegExp(`"${key}":`));
  }
  for (const key of [
    "pageWidth", "pageHeight", "chars", "furigana", "wordSegmentation",
    "characterGeometry", "formulaCoverage", "formulaRegions",
  ]) {
    assert.match(BRIDGE, new RegExp(`"${key}":`));
  }
  assert.match(BRIDGE, /"multiline": jsonNullable\(value\.multiline\)/);
  assert.match(BRIDGE, /"retryable": retryable/);
  assert.match(BRIDGE, /private static func pageRevision/);
  assert.match(BRIDGE, /SHA256\.hash\(data: data\)/);
  assert.match(MANAGER, /result\.importedPages \+ result\.importedFormulaPages/);
  assert.match(BRIDGE, /Dictionary\(grouping: value\.matches/);
  assert.match(BRIDGE, /"count": hits\.count/);
  assert.match(BRIDGE, /"pages": status\.textProgress\.completed/);
});
