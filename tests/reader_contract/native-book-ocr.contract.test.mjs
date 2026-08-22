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

/// 取一个方法的函数体：从 `func 名(` 到下一个同缩进的 `func`。
///
/// 用它而不是 `/func x\([\s\S]{0,600}?y/` 这种字符窗口正则 —— 窗口只要被后来的
/// 代码撑过头就会静默失配，或者更糟：窗口太大时匹配到隔壁函数里的内容，测试
/// 看起来是绿的却什么都没在守。
const bodyOf = (source, name) => {
  const start = source.indexOf(`func ${name}(`);
  if (start < 0) return "";
  const next = source
    .slice(start + 1)
    .search(/\n {4}(?:@\w+\s*)*(?:private |static )*func /);
  return next < 0 ? source.slice(start) : source.slice(start, start + 1 + next);
};

/// 去掉行注释。断言要守的是代码的行为，注释里出现同样的字面量不算数
/// —— 否则一句解释性的注释就能让 doesNotMatch 假红，或让 match 假绿。
const codeOnly = (body) => body.replace(/\/\/.*$/gm, "");

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
  assert.match(
    STORE,
    /func writeStatus\([\s\S]*_ status: NativeBookOCRBookStatus,[\s\S]*mutationLease:/,
  );
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

test("Apple Vision cache revision refresh is narrow, layered, and fail-safe", () => {
  assert.match(MODELS, /engineRevision = "apple-vision-structured\/2"/);
  assert.match(MODELS, /manualEngineRevision = "apple-vision-manual\/2"/);
  assert.match(
    MANAGER,
    /page\.textAuthority != \.localOverride[\s\S]*page\.engineRevision\.hasPrefix\("apple-vision-structured\/"\)[\s\S]*page\.engineRevision != NativeBookOCRConfiguration\.engineRevision/,
  );

  const shouldRefresh = (page, currentRevision) => (
    page.textAuthority !== "local-override"
      && page.engineRevision.startsWith("apple-vision-structured/")
      && page.engineRevision !== currentRevision
  );
  const current = "apple-vision-structured/2";
  assert.equal(shouldRefresh({
    source: "apple", engineRevision: "apple-vision-structured/1",
    textAuthority: "supplemental",
  }, current), true, "an old Apple base must refresh");
  assert.equal(shouldRefresh({
    source: "pi", engineRevision: "apple-vision-structured/1",
    textAuthority: "supplemental", formulaCoverage: "complete",
  }, current), true, "a Pi formula attachment must not hide a stale Apple base");
  for (const protectedPage of [
    { source: "apple", engineRevision: current, textAuthority: "supplemental" },
    { source: "apple", engineRevision: "pdfkit-embedded-text/1", textAuthority: "supplemental" },
    { source: "pi", engineRevision: "pi-vision/1", textAuthority: "supplemental" },
    { source: "apple", engineRevision: "apple-vision-manual/1", textAuthority: "local-override" },
    { source: "apple", engineRevision: "apple-vision-structured/1+selection:ocrfix-1", textAuthority: "local-override" },
  ]) {
    assert.equal(shouldRefresh(protectedPage, current), false);
  }

  assert.match(MODELS, /func preservingPiFormulaAttachment\(/);
  assert.match(
    MODELS,
    /previous\.source == \.pi,[\s\S]*previous\.formulaCoverage == \.complete[\s\S]*formulaCoverage: previous\.formulaCoverage,[\s\S]*formulaRegions: previous\.formulaRegions/,
  );
  const failureGate = MANAGER.indexOf("if refreshingStaleVision,");
  const writePage = MANAGER.indexOf(
    "try await store.writePage(",
    failureGate,
  );
  assert.ok(failureGate >= 0 && writePage > failureGate);
  assert.match(
    MANAGER.slice(failureGate, writePage),
    /value\.status != \.ready,[\s\S]*value\.status != \.readyEmpty[\s\S]*已保留旧文字与公式[\s\S]*continue/,
    "a failed revision migration must not overwrite the last usable base",
  );
  assert.match(MANAGER, /var refreshFailures = Set<Int>\(\)/);
  assert.match(MANAGER, /let failed = !refreshFailures\.isEmpty/);
});

test("character interpolation publishes only positive non-overlapping estimates", () => {
  assert.match(
    PROCESSOR,
    /private static func resolvedCharacterRects\([\s\S]*\) -> \[CGRect\?\]/,
  );
  assert.match(PROCESSOR, /func strictlyOverlaps\(/);
  assert.match(PROCESSOR, /guard origin\.isFinite, extent\.isFinite, extent > 0/);
  assert.match(PROCESSOR, /guard isSafeEstimate\(estimate, at: index\) else \{ continue \}/);
  assert.match(PROCESSOR, /guard let normalizedRect = resolved\[offset\] else \{[\s\S]*continue/);
  assert.match(PROCESSOR, /guard x1 > x0, y1 > y0 else \{/);

  const overlap = (a, b) => (
    a.x < b.x + b.w && a.x + a.w > b.x
      && a.y < b.y + b.h && a.y + a.h > b.y
  );
  const resolveHorizontal = (pieces, observation) => {
    const output = pieces.map((item) => item && { ...item });
    const known = pieces.map((item, index) => item ? index : -1)
      .filter((index) => index >= 0);
    if (!known.length) {
      if (!(observation.w > 0 && observation.h > 0)) return output;
      return pieces.map((_, index) => ({
        x: observation.x + index * observation.w / pieces.length,
        y: observation.y,
        w: observation.w / pieces.length,
        h: observation.h,
      }));
    }
    const exact = known.map((index) => pieces[index]);
    const estimates = [];
    const nearestBefore = (index) => {
      for (let at = index - 1; at >= 0; at -= 1) if (output[at]) return output[at];
      return null;
    };
    const nearestAfter = (index) => {
      for (let at = index + 1; at < output.length; at += 1) if (output[at]) return output[at];
      return null;
    };
    const fill = (lower, upper) => {
      const start = lower === null ? 0 : lower + 1;
      const end = (upper === null ? pieces.length : upper) - 1;
      if (start > end) return;
      const span = end - start + 1;
      const left = lower === null ? observation.x : output[lower].x + output[lower].w;
      const right = upper === null ? observation.x + observation.w : output[upper].x;
      const extent = right - left;
      if (!(extent > 0)) return;
      const step = extent / span;
      for (let offset = 0; offset < span; offset += 1) {
        const index = start + offset;
        const estimate = {
          x: left + offset * step, y: observation.y, w: step, h: observation.h,
        };
        const before = nearestBefore(index);
        const after = nearestAfter(index);
        if (!(estimate.w > 0 && estimate.h > 0)
            || exact.some((item) => overlap(estimate, item))
            || estimates.some((item) => overlap(estimate, item))
            || (before && estimate.x + estimate.w / 2 <= before.x + before.w / 2)
            || (after && estimate.x + estimate.w / 2 >= after.x + after.w / 2)) {
          continue;
        }
        output[index] = estimate;
        estimates.push(estimate);
      }
    };
    fill(null, known[0]);
    for (let index = 1; index < known.length; index += 1) {
      fill(known[index - 1], known[index]);
    }
    fill(known.at(-1), null);
    return output;
  };

  const observation = { x: 0, y: 0, w: 1, h: 0.1 };
  const first = { x: 0.2, y: 0, w: 0.1, h: 0.1 };
  const second = { x: 0.6, y: 0, w: 0.1, h: 0.1 };
  const normal = resolveHorizontal([null, first, null, second, null], observation);
  assert.deepEqual(normal[1], first);
  assert.deepEqual(normal[3], second);
  assert.equal(normal.every((item) => item && item.w > 0 && item.h > 0), true);
  for (let index = 1; index < normal.length; index += 1) {
    assert.ok(
      normal[index - 1].x + normal[index - 1].w / 2
        < normal[index].x + normal[index].w / 2,
      "horizontal reading-axis centers must stay monotonic",
    );
  }
  for (let left = 0; left < normal.length; left += 1) {
    for (let right = left + 1; right < normal.length; right += 1) {
      assert.equal(overlap(normal[left], normal[right]), false);
    }
  }

  for (const [left, right] of [
    [{ x: 0.2, y: 0, w: 0.3, h: 0.1 }, { x: 0.4, y: 0, w: 0.3, h: 0.1 }],
    [{ x: 0.2, y: 0, w: 0.2, h: 0.1 }, { x: 0.4, y: 0, w: 0.2, h: 0.1 }],
    [{ x: 0.65, y: 0, w: 0.1, h: 0.1 }, { x: 0.25, y: 0, w: 0.1, h: 0.1 }],
  ]) {
    const conflicted = resolveHorizontal([left, null, right], observation);
    assert.deepEqual(conflicted[0], left, "the first exact box must remain unchanged");
    assert.deepEqual(conflicted[2], right, "the second exact box must remain unchanged");
    assert.equal(conflicted[1], null, "an impossible gap must remain unresolved");
  }
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
  for (const key of [
    "c", "x0", "y0", "x1", "y1", "sp", "w", "b", "bk", "line", "vertical",
  ]) {
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
  assert.match(STORE, /let targetSource: NativeBookOCRSource = executor == "pc" \? \.pc : \.pi/);
  assert.doesNotMatch(MODELS, /absolutePath|fileURL|bookmark/);
});

test("Pi and PC attachment receipts verify durable per-page character counts", () => {
  assert.match(MODELS, /enum NativeBookOCRSource[\s\S]*case pi[\s\S]*case pc/);
  assert.match(STORE, /reader-native-book-ocr-import-receipt\/2/);
  assert.match(STORE, /pageCharacterCounts: \[Int: Int\]\?/);
  assert.match(
    STORE,
    /probePages[\s\S]*densestPage[\s\S]*storedPage\.chars\.count == expectedCounts\[pageNumber\]/,
  );
  assert.match(STORE, /executor == "pc" \? \.pc : \.pi/);
  // 这一条守的核心是上面那个**同源**比较（落盘后的 chars.count 对回执里的
  // expectedCounts）—— 那个成立。而 textCharCount 与 chars.count 是两个不同口径
  // 的量（正文字数 vs 含 sp 的条目数），相等比一直在拒真数据；
  // 详见下面 "导入校验不拿 textCharCount 跟 chars.count 相等比"。
  assert.match(
    STORE,
    /textCharCount\.map\(\{ \$0 >= 0 && \$0 <= value\.chars\.count \}\) \?\? true/,
  );
});

test("new manga geometry profiles import as a fresh engine revision", () => {
  const importBody = bodyOf(STORE, "importDerivedAttachments");
  const convertBody = bodyOf(STORE, "convertPiPage");
  assert.match(STORE, /"quality-first-v1", "quality-first-v2", "quality-first-v3"/);
  assert.match(importBody, /processingProfile: processingProfile/);
  assert.match(convertBody, /processingProfile: String/);
  assert.match(
    convertBody,
    /value\.engine == "manga"[\s\S]*"pi-default-v2", "quality-first-v3"[\s\S]*\? 2[\s\S]*: 1/,
  );
  assert.match(convertBody, /let revision = "\\\(executor\)-\\\(value\.engine\)\/\\\(geometryVersion\)"/);
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
    MANAGER.indexOf("func beginPDFMutationLease"),
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

test("native page text trust accepts only separator-bounded capability descendants", () => {
  assert.match(
    BRIDGE,
    /private static func pathIsWithin\([\s\S]*let exact = basePath\.hasSuffix\("\/"\)[\s\S]*let withSeparator = exact \+ "\/"[\s\S]*url\.path == exact \|\| url\.path\.hasPrefix\(withSeparator\)/,
  );
  assert.match(BRIDGE, /return Self\.pathIsWithin\(url, base: trustedBaseURL\)/);
  assert.match(BRIDGE, /if !pathIsWithin\(url, base: base\)/);

  const pathIsWithin = (urlPath, basePath) => {
    const exact = basePath.endsWith("/") ? basePath.slice(0, -1) : basePath;
    return urlPath === exact || urlPath.startsWith(`${exact}/`);
  };
  assert.equal(pathIsWithin("/r/TOKEN/shells/pdf.html", "/r/TOKEN"), true);
  assert.equal(pathIsWithin("/r/TOKEN/shells/pdf.html", "/r/TOKEN/"), true);
  assert.equal(pathIsWithin("/r/TOKEN", "/r/TOKEN/"), true);
  assert.equal(pathIsWithin("/r/TOKENXXX/shells/pdf.html", "/r/TOKEN"), false);
  assert.equal(pathIsWithin("/other/shells/pdf.html", "/r/TOKEN"), false);
});

test("PDFKit image pages lazily recover the embedded selectable text layer", () => {
  assert.match(
    MANAGER,
    /func readerPageCharacters\([\s\S]*book: ReaderLocalBookAccess[\s\S]*processEmbeddedPage\(/,
  );
  assert.match(
    MANAGER,
    /let layer = try await store\.layerState\([\s\S]*guard layer == \.embedded else \{ return nil \}/,
  );
  assert.match(MANAGER, /private var readerEmbeddedProcessors:/);
  assert.match(
    BRIDGE,
    /case \.pageCharacters:[\s\S]*book: requestBookAccess[\s\S]*manager: manager/,
  );
  assert.match(
    BRIDGE,
    /func pageReply\([\s\S]*book: ReaderLocalBookAccess\?[\s\S]*manager\.readerPageCharacters\(/,
  );
  const readerFallback = MANAGER.slice(
    MANAGER.indexOf("func readerPageCharacters("),
    MANAGER.indexOf("func pageStatus("),
  );
  assert.doesNotMatch(readerFallback, /startLocal|forceVision:\s*true|startPi/);
});

test("first local PDF page exposes embedded text before the full digest finishes", () => {
  const pageReply = BRIDGE.slice(
    BRIDGE.indexOf("private static func pageReply("),
    BRIDGE.indexOf("private static func statusReply("),
  );
  assert.match(
    pageReply,
    /if let expectedContentSHA256[\s\S]*manager\.readerPageCharacters\([\s\S]*else if let book[\s\S]*manager\.provisionalReaderPageCharacters\(/,
  );
  assert.match(pageReply, /book\.record\.format == \.pdf/);

  const provisional = MANAGER.slice(
    MANAGER.indexOf("func provisionalReaderPageCharacters("),
    MANAGER.indexOf("func pageStatus("),
  );
  assert.match(provisional, /book\.record\.contentFingerprint\.lowercased\(\)/);
  assert.match(provisional, /try Self\.validateCurrentBook\(book\)/);
  assert.match(provisional, /cached\.byteCount == book\.record\.byteCount/);
  assert.match(provisional, /cached\.modifiedAt == book\.record\.modifiedAt/);
  assert.match(provisional, /processor\.processEmbeddedPage\(/);
  assert.doesNotMatch(
    provisional,
    /store\.|waitUntilReady|ensureContentSHA256|startLocal|startPi/,
    "the provisional render identity must stay in-memory and never touch sidecars",
  );
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
  assert.match(BRIDGE, /if let line = value\.line, line >= 0 \{ result\["line"\] = line \}/);
  assert.match(BRIDGE, /if let vertical = value\.vertical \{ result\["vertical"\] = vertical \}/);
  assert.match(BRIDGE, /"retryable": retryable/);
  assert.match(BRIDGE, /private static func pageRevision/);
  const revision = BRIDGE.slice(
    BRIDGE.indexOf("private static func pageRevision("),
    BRIDGE.indexOf("private static func stageObject("),
  );
  assert.match(revision, /removeValue\(forKey: "created_at"\)/);
  assert.ok(
    revision.indexOf('removeValue(forKey: "created_at")') <
      revision.indexOf("SHA256.hash(data: data)"),
    "page revision must remove extraction time before hashing",
  );
  assert.match(BRIDGE, /SHA256\.hash\(data: data\)/);
  assert.match(BRIDGE, /value\.engineRevision\.prefix\(72\)/);
  assert.match(BRIDGE, /return "\\\(engine\):\\\(String\(digest\.prefix\(72\)\)\)"/);
  assert.match(MANAGER, /layerStates\[bookID\] = try await store\.layerState/);
  assert.match(MANAGER, /当前文字层未自动切换/);
  assert.match(BRIDGE, /Self\.jsonNullable\(update\.page\)/);
  assert.match(BRIDGE, /Dictionary\(grouping: value\.matches/);
  assert.match(BRIDGE, /"count": hits\.count/);
  assert.match(BRIDGE, /"pages": status\.textProgress\.completed/);
});

test("manual page OCR and selection fixes are local, durable, layered, and never fake success", () => {
  assert.match(PROCESSOR, /forceVision: Bool = false/);
  assert.match(PROCESSOR, /if !forceVision, geometry\.rotation == 0/);
  assert.match(MODELS, /reader-native-selection-corrections\/1/);
  assert.match(MODELS, /case localOverride = "local-override"/);
  assert.match(STORE, /func writeManualPageOverride\(/);
  assert.match(STORE, /func clearManualPageOverride\(/);
  assert.match(STORE, /func appendSelectionCorrection\(/);
  assert.match(STORE, /overrides\/manual/);
  assert.match(STORE, /overrides\/selection/);
  assert.match(STORE, /applyingSelectionCorrections/);
  assert.match(STORE, /formulaRegions\.removeAll/);
  assert.match(MANAGER, /func recognizeSelection\(/);
  assert.match(MANAGER, /func reOCRPage\(/);
  assert.match(MANAGER, /func clearManualReOCR\(/);
  assert.match(MANAGER, /forceVision: true/);
  assert.match(MANAGER, /try await store\.appendSelectionCorrection/);
  assert.match(MANAGER, /try await store\.writeManualPageOverride/);
  assert.match(MANAGER, /try await store\.clearManualPageOverride/);
  assert.match(BRIDGE, /case recognizeSelection = "ocr-selection"/);
  assert.match(BRIDGE, /case reOCRPage = "reocr-page"/);
  assert.match(BRIDGE, /case clearReOCRPage = "clear-reocr-page"/);
  assert.match(BRIDGE, /private var localBookAccess: ReaderLocalBookAccess\?/);
  assert.match(BRIDGE, /"persisted": true/);
  assert.doesNotMatch(MANAGER, /try\? await store\.(?:appendSelectionCorrection|writeManualPageOverride|clearManualPageOverride)/);
});

test("导入的文字层在没人挑过时被采纳，用户挑过的绝不覆盖", () => {
  // 用户 2026-08-19 的实况：书里当前用的是 PDF 自带文字层，它的框比字高一大截、
  // 相邻两行重叠 12.7pt（实测），于是"选下面会一起选上面"，也没有词分组。我们
  // 跑好的 OCR 层就在旁边、几何是准的，却从没被选上 —— 预处理白做。
  //
  // 「导入不覆盖当前选择」本身是对的；错在把"从来没挑过"也算成了一次选择。
  assert.match(MODELS, /let chosenByUser: Bool\?/);
  assert.match(STORE, /func adoptImportedLayerIfUnchosen\(/);
  // 只在当前是内嵌层/兼容旧结果时采纳 —— 别把用户选中的另一份 OCR 顶掉。
  assert.match(
    STORE,
    /guard \[\.embedded, \.legacy\]\.contains\(current\.selected\) else \{\s*\n\s*return \(current, false\)/,
  );
  // 用户自己点过就到此为止。
  assert.match(STORE, /if stored\?\.chosenByUser == true \{ return \(current, false\) \}/);
  // 手点选择要留下"用户拍板"的印记，否则下次导入会把它顶掉；
  // 自动采纳则必须**不留**，否则以后再也不会自动采纳更好的层。
  // 同样按位置切函数体 —— 字符窗口式的正则会随函数长度悄悄失效。
  const selectBody = codeOnly(bodyOf(STORE, "selectLayer"));
  assert.ok(selectBody.length > 0, "找不到 selectLayer");
  assert.match(selectBody, /chosenByUser: true/);
  const adoptBody = codeOnly(bodyOf(STORE, "adoptImportedLayerIfUnchosen"));
  assert.ok(adoptBody.length > 0, "找不到 adoptImportedLayerIfUnchosen");
  assert.match(adoptBody, /chosenByUser: false/);
  assert.doesNotMatch(adoptBody, /chosenByUser: true/);
  // 导入收尾据实报，不能采纳了还说"未自动切换"。
  assert.match(MANAGER, /try await store\.adoptImportedLayerIfUnchosen\(/);
  assert.match(MANAGER, /adoption\.adopted[\s\S]{0,200}?并设为当前文字层/);
  assert.doesNotMatch(MANAGER, /try\? await store\.adoptImportedLayerIfUnchosen/);
});

test("删除本机文字层：先挪走选择再删目录，且不碰内嵌层", () => {
  assert.match(STORE, /func deleteLayer\(/);
  // 顺序是要害：**先把选择挪走，再删目录**。反过来的话，中间那一瞬
  // layerState() 会看到"选中的层不存在"，而每次读页都调它。
  // 用位置比较而不是一个跨几百字符的正则 —— 后者只要函数体一长就会假过。
  const deleteBody = codeOnly(bodyOf(STORE, "deleteLayer"));
  assert.ok(deleteBody.length > 0, "找不到 deleteLayer");
  const movesSelection = deleteBody.indexOf("if current.selected == layer");
  const removesDirectory = deleteBody.indexOf("removeItem(at: directory)");
  assert.ok(movesSelection >= 0, "deleteLayer 没有把选择挪走");
  assert.ok(removesDirectory >= 0, "deleteLayer 没有删目录");
  assert.ok(
    movesSelection < removesDirectory,
    "删目录排在挪选择之前 —— 中间那一瞬 layerState() 会看到选中的层不存在",
  );
  // 内嵌层与兼容旧结果不是"导入进来的一份"，没有可删的目录。
  assert.match(
    STORE,
    /func deleteLayer\([\s\S]{0,300}?guard \[\.appleVision, \.pi, \.pc\]\.contains\(layer\)/,
  );
  assert.match(MANAGER, /func deleteTextLayer\(/);
  assert.match(MANAGER, /try await store\.deleteLayer\(/);
});

test("导入校验不拿 textCharCount 跟 chars.count 相等比", () => {
  // 用户 2026-08-19：「导入预处理失败 —— 文字页字符层无效」。
  //
  // 根因：这两个**不是同一个量**。worker 那边 textCharCount 是正文字符数
  // （`len("".join(text.split()))`，去掉全部 Unicode 空白），而 chars 是 symbol
  // 列表、另含 sp 条目（只标记 Vision 报告的 detectedBreak）。两个口径出自不同
  // 数据源，永远不可能逐字对齐。
  //
  // 实测这条一直在拒：更早代码产的 57/60、Pi 产的 60/60、当天新跑的 53/53 页
  // 都不等 —— 也就是**导入从来没成功过**。按 sp 重数之后仍有 1/53 差 1，
  // 说明"调口径"走不通。
  //
  // 换成恒成立的不变量：正文字数不可能超过条目总数。三份结果共 1032 页全部通过。
  assert.doesNotMatch(
    STORE,
    /textCharCount\.map\(\{ \$0 == value\.chars\.count \}\)/,
    "又在拿两个不同口径的量做相等比",
  );
  assert.match(
    STORE,
    /textCharCount\.map\(\{ \$0 >= 0 && \$0 <= value\.chars\.count \}\)/,
  );
});
