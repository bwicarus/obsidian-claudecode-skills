import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const LIBRARY = read("ios/BWReader/App/ReaderLocalLibrary.swift");
const LIBRARY_VIEW = read("ios/BWReader/App/ReaderLocalLibraryView.swift");
const REMOTE_LIBRARY = read("ios/BWReader/App/ReaderRemoteLibrary.swift");
const NATIVE_TOOLS = read("ios/BWReader/App/NativeReaderToolsView.swift");
const APP_ROOT = read("ios/BWReader/App/BWReaderNativeApp.swift");
const LOCAL_SERVER = read("ios/BWReader/App/ReaderLocalRuntimeServer.swift");
const LOCAL_PACKAGER = read("ios/BWReader/package_local_reader.py");
const NATIVE_PI_GATEWAY = read("ios/BWReader/App/ReaderNativePiGateway.swift");
const NATIVE_INTERFACE_MANIFEST = read(
  "ios/BWReader/App/ReaderNativeInterfaceManifest.swift",
);
const NATIVE_INTERFACE_ROUTES = JSON.parse(read(
  "ios/BWReader/native_reader_interface_manifest.json",
)).routes;
const WEB_VIEW = read("ios/BWReader/App/ReaderWebView.swift");
const NATIVE_FEATURES = read("ios/BWReader/App/ReaderWebViewNativeFeatures.swift");
const SPOTLIGHT = read("ios/BWReader/App/ReaderSpotlight.swift");
const PDF_BOOT = read("_server_deploy/static/pdf/reader.src/01-boot.js");
const PDF_LOADER = read("_server_deploy/static/pdf/reader.src/03-loader.js");

test("native local library keeps only a scoped folder bookmark and bounded metadata index", () => {
  assert.match(LIBRARY, /reader\.localLibrary\.folderBookmark/);
  assert.match(LIBRARY, /bookmarkData\(/);
  assert.match(LIBRARY, /startAccessingSecurityScopedResource\(\)/);
  assert.match(LIBRARY, /stopAccessingSecurityScopedResource\(\)/);
  assert.match(LIBRARY, /reader-local-library-index\/1/);
  assert.match(LIBRARY, /func makeFolderAccess\(\) throws -> ReaderLocalFolderAccess/);
  assert.match(LIBRARY, /never serialized or exposed to the WebView\/Safari extension/);
  assert.match(LIBRARY, /let relativePath: String/);
  assert.match(LIBRARY, /let byteCount: Int64/);
  assert.doesNotMatch(LIBRARY, /let (bookData|fileData|contents): Data/);
});

test("native local library recursively indexes only PDF and EPUB with sampled content identity", () => {
  assert.match(LIBRARY, /FileManager\.default\.enumerator\(/);
  assert.match(LIBRARY, /case pdf/);
  assert.match(LIBRARY, /case epub/);
  assert.match(LIBRARY, /let sampleLimit = 256 \* 1_024/);
  assert.match(LIBRARY, /reader-local-book-sample\/1/);
  assert.match(LIBRARY, /seek\(toOffset: fileSize - UInt64\(sampleLimit\)\)/);
  assert.match(LIBRARY, /previousByPath\[normalizedPath\]/);
  assert.match(LIBRARY, /previousByFingerprint\[fingerprint\.digest\]/);
  assert.match(LIBRARY, /stableID = cached\.id/);
  assert.match(LIBRARY, /reader-local-book-instance\/1/);
  assert.match(LIBRARY, /stableBookID\(/);
  assert.match(LIBRARY, /Dictionary\(\s*grouping: previousBooks/);
  assert.match(LIBRARY, /fingerprintMatches\.count == 1/);
  assert.match(LIBRARY, /!FileManager\.default\.fileExists/);
  assert.doesNotMatch(LIBRARY, /books = books\.filter/);
  assert.match(LIBRARY, /invalidRelativePath/);
  assert.match(LIBRARY, /isSymbolicLink != true/);
});

test("native shelf opens local bytes directly and keeps Pi transfer as an explicit sync action", () => {
  assert.match(LIBRARY_VIEW, /case local/);
  assert.match(LIBRARY_VIEW, /case pi/);
  assert.match(LIBRARY_VIEW, /case all/);
  assert.match(LIBRARY_VIEW, /Button\("打开"\)/);
  assert.match(LIBRARY_VIEW, /await reader\.openLocalBook\(book, library: library\)/);
  assert.match(LIBRARY_VIEW, /localID == nil \? "下载并打开" : "下载此版本并打开"/);
  assert.match(LIBRARY_VIEW, /await download\(book\)/);
  assert.doesNotMatch(LIBRARY_VIEW, /上传并打开/);
  assert.doesNotMatch(LIBRARY_VIEW, /remote\.readerURL\(for: book\)/);
  assert.match(REMOTE_LIBRARY, /pdf\/api\/library\/catalog/);
  assert.match(REMOTE_LIBRARY, /pdf\/api\/library\/upload/);
  assert.match(REMOTE_LIBRARY, /pdf\/api\/library\/download\//);
  assert.match(REMOTE_LIBRARY, /availableDestination/);
  assert.match(REMOTE_LIBRARY, /checksumMismatch/);
  assert.match(REMOTE_LIBRARY, /moveItem\(at: stagingURL, to: destinationURL\)/);
  assert.match(REMOTE_LIBRARY, /reader-library-sync-links\/1/);
  assert.match(REMOTE_LIBRARY, /lastSyncedSha256/);
  assert.match(REMOTE_LIBRARY, /withExtendedLifetime\(access\)/);
  assert.match(REMOTE_LIBRARY, /withExtendedLifetime\(folderAccess\)/);
  assert.match(REMOTE_LIBRARY, /willPerformHTTPRedirection/);
  assert.match(REMOTE_LIBRARY, /completionHandler\(nil\)/);
  assert.match(NATIVE_TOOLS, /Label\("本机书库", systemImage: "books\.vertical"\)/);
  assert.match(NATIVE_TOOLS, /ReaderLocalLibraryView\(reader: reader\)/);
  assert.match(APP_ROOT, /accessibilityLabel\("打开书库"\)/);
});

test("ordinary reading and local edits never require a Pi route", () => {
  const ownerByPath = new Map(
    NATIVE_INTERFACE_ROUTES.map((route) => [route.path, route.owner]),
  );
  const appOwned = [
    "/pdf/api/book-meta",
    "/pdf/api/page-image",
    "/pdf/api/page-chars",
    "/pdf/api/page-text-status",
    "/pdf/api/search",
    "/pdf/api/toc",
    "/pdf/api/epub-manifest",
    "/pdf/api/epub-section",
    "/pdf/api/epub-resource",
    "/pdf/api/epub-search",
    "/pdf/api/reading-pos",
    "/pdf/api/highlights",
    "/pdf/api/epub-highlights",
    "/pdf/api/ink",
    "/pdf/api/epub-ink",
    "/pdf/api/notes",
    "/pdf/api/userpages",
    "/pdf/api/prefs",
    "/pdf/api/video-player-prefs",
    "/pdf/api/book-langs",
    "/pdf/api/book-crop",
    "/pdf/api/ocr-selection",
    "/pdf/api/reocr-page",
    "/pdf/api/reocr-page/clear",
    "/pdf/api/pdf-insert-page",
    "/pdf/api/note-composite",
    "/pdf/api/to-note",
    "/pdf/api/outgoing/state",
    "/pdf/api/outgoing/drawing",
    "/pdf/api/outgoing/focus",
    "/pdf/api/outgoing/journal",
  ];
  for (const path of appOwned) {
    assert.ok(ownerByPath.has(path), `${path} is declared`);
    assert.notEqual(ownerByPath.get(path), "pi", `${path} is App-owned`);
  }
});

test("local renderer uses a stable loopback origin with signed static assets and scoped book bytes", () => {
  assert.match(LOCAL_SERVER, /import CryptoKit/);
  assert.match(LOCAL_SERVER, /import FlyingFox/);
  assert.match(LOCAL_SERVER, /static let origin = "http:\/\/127\.0\.0\.1:43129"/);
  assert.match(LOCAL_SERVER, /manifest\["contract"\].*"bw-local-reader-bundle\/1"/s);
  assert.match(LOCAL_SERVER, /requiredShells\.isSubset\(of: Set\(files\.keys\)\)/);
  assert.match(LOCAL_SERVER, /SHA256\.hash\(data: bytes\)/);
  assert.match(LOCAL_SERVER, /if relative\.hasPrefix\("static\/"\)/);
  assert.match(LOCAL_SERVER, /cacheControl: \[\.noCache\]/);
  assert.match(LOCAL_SERVER, /cacheControl: \[\.noStore\]/);
  assert.match(LOCAL_SERVER, /FileHTTPHandler\(/);
  assert.match(LOCAL_SERVER, /SecRandomCopyBytes/);
  assert.match(LOCAL_SERVER, /maximumEPUBBytes: Int64 = 512 \* 1_024 \* 1_024/);
  assert.match(LOCAL_SERVER, /throw ReaderLocalRuntimeError\.epubTooLarge/);
  assert.match(LOCAL_SERVER, /func validateCurrentFile\(maximumEPUBBytes: Int64\) throws/);
  assert.match(LOCAL_SERVER, /\.fileSizeKey, \.contentModificationDateKey/);
  assert.match(LOCAL_SERVER, /Int64\(values\.fileSize \?\? -1\) == record\.byteCount/);
  assert.match(LOCAL_SERVER, /values\.contentModificationDate == record\.modifiedAt/);
  assert.ok(
    [...LOCAL_SERVER.matchAll(/try access\.validateCurrentFile\(/g)].length >= 2,
    "opening and serving must both revalidate the current file",
  );
  assert.match(LOCAL_SERVER, /private var lifecycleTask: Task<Void, Error>\?/);
  assert.match(LOCAL_SERVER, /joinLifecycleTaskIfPresent\(\)/);
  assert.match(LOCAL_SERVER, /clearLifecycleTask\(ifMatching: token\)/);
  assert.match(LOCAL_SERVER, /__BW_LOCAL_CSP_NONCE__/);
  assert.match(LOCAL_SERVER, /script-src 'self' 'nonce-/);
  assert.match(LOCAL_SERVER, /script-src-attr 'unsafe-inline'/);
  assert.match(LOCAL_SERVER, /form-action 'none'/);
  assert.doesNotMatch(LOCAL_SERVER, /script-src\s+[^;"\n]*'unsafe-inline'/);
  assert.doesNotMatch(LOCAL_SERVER, /connect-src[^"\n]*https:\/\/bwicarus/);
  assert.match(
    LOCAL_SERVER,
    /private static let openAIRealtimeOrigin\s*=\s*"https:\/\/api\.openai\.com"/,
  );
  assert.doesNotMatch(LOCAL_SERVER, /realtimeControlWebSocketOrigin|__bwReaderWsUrl/);
  assert.match(
    LOCAL_SERVER,
    /connect-src 'self' [^"\n]*openAIRealtimeOrigin[^"\n]*wss:\/\/bwicarus-2\.taile44d0c\.ts\.net/,
  );
  assert.doesNotMatch(LOCAL_SERVER, /connect-src[^"\n]*wss:\/\/bwicarus\.taile44d0c\.ts\.net/);
  assert.match(
    WEB_VIEW,
    /requestMediaCapturePermissionFor origin:[\s\S]*origin\.host\.lowercased\(\) == ReaderLocalRuntimeServer\.host[\s\S]*frame\.isMainFrame[\s\S]*type == \.microphone[\s\S]*\? \.grant[\s\S]*: \.deny/,
  );
});

test("native PDF mode uses PDFKit page images without PWA or whole-book caching", () => {
  assert.match(PDF_BOOT, /window\.__BW_NATIVE_LOCAL_READER__ === true\s*\? true/);
  assert.match(PDF_BOOT, /window\.__BW_NATIVE_LOCAL_READER__ !== true && 'serviceWorker' in navigator/);
  assert.match(PDF_LOADER, /const _NATIVE_LOCAL_PDF = window\.__BW_NATIVE_LOCAL_READER__ === true/);
  assert.match(PDF_LOADER, /_NATIVE_LOCAL_PDF \? null : await _idbGet/);
  assert.match(PDF_LOADER, /if \(!_NATIVE_LOCAL_PDF && !_haveBuf/);
  assert.match(
    PDF_LOADER,
    /deferredNativeCrop = _NATIVE_LOCAL_PDF \? loadBookCrop\(\) : null/,
  );
  assert.match(PDF_LOADER, /if \(!deferredNativeCrop\) \{\s*await loadBookCrop\(\)/);
  assert.ok(
    PDF_LOADER.indexOf("pdfLoadHide();   // 首页已渲染") <
      PDF_LOADER.indexOf("deferredNativeCrop.then"),
    "native crop state may reflow after first paint but must not block it",
  );
});

test("PDFKit selection identity never blocks first paint and refreshes transient pages", () => {
  const open = WEB_VIEW.slice(
    WEB_VIEW.indexOf("private func openLocalBook("),
    WEB_VIEW.indexOf("private func cancelPendingLocalBookNavigation("),
  );
  const backgroundIdentity = WEB_VIEW.slice(
    WEB_VIEW.indexOf("private func scheduleLocalPDFContentIdentity("),
    WEB_VIEW.indexOf("/// Completes the native half"),
  );
  assert.match(
    open,
    /verifiedNativeRemoteBookBinding\([\s\S]*openingContentSHA256 = verified\.localContentSHA256/,
  );
  assert.ok(
    open.indexOf("localRuntimeServer.open(") < open.indexOf("webView.load(URLRequest("),
    "the PDF shell must begin opening before any fallback digest task",
  );
  assert.doesNotMatch(open, /await library\.ensureContentSHA256\(for: openingBook\)/);
  assert.match(
    WEB_VIEW,
    /didFinish navigation:[\s\S]*scheduleLocalPDFContentIdentity\(\)[\s\S]*schedulePendingBookUserStateImport\(\)/,
  );
  assert.match(backgroundIdentity, /currentLocalContentDigest\(/);
  assert.match(backgroundIdentity, /page: nil/);
  assert.match(backgroundIdentity, /await bridge\.sendUpdate\(/);
});

test("native deep links preserve PDF pages and EPUB sections without exposing a file path", () => {
  for (const marker of [
    "__BW_LOCAL_INITIAL_PAGE__",
    "__BW_LOCAL_INITIAL_PAGE_TS__",
    "__BW_LOCAL_INITIAL_EPUB_POS__",
    "__BW_LOCAL_INITIAL_EPUB_POS_TS__",
  ]) {
    assert.match(LOCAL_PACKAGER, new RegExp(marker));
    assert.match(LOCAL_SERVER, new RegExp(marker));
  }
  assert.match(LOCAL_SERVER, /request\.query\["page"\]/);
  assert.match(LOCAL_SERVER, /\(1\.\.\.10_000_000\)\.contains\(parsedPage\)/);
  assert.match(LOCAL_SERVER, /let initialEPUBPosition = max\(0, initialPage - 1\)/);
  assert.match(LOCAL_SERVER, /initialPage: Int\? = nil/);
  assert.match(LOCAL_SERVER, /URLQueryItem\([\s\S]*name: "page",[\s\S]*value: String\(initialPage\)/);
  assert.match(PDF_LOADER, /currentPage = Math\.max\(1, Math\.min\([\s\S]*pdfDoc\.numPages/);
});

test("legacy Reader shelf navigation opens the App-owned local library", () => {
  assert.match(WEB_VIEW, /@Published private\(set\) var libraryPresentationRequestID: UUID\?/);
  assert.match(
    WEB_VIEW,
    /private func takeOverLibraryNavigation\([\s\S]*sourceURL: URL\?[\s\S]*isTrustedReaderURL\(sourceURL\)[\s\S]*url\.path == "\/pdf\/"[\s\S]*libraryPresentationRequestID = UUID\(\)/,
  );
  assert.match(
    WEB_VIEW,
    /let sourceURL = navigationAction\.sourceFrame\.isMainFrame[\s\S]*\? webView\.url[\s\S]*: navigationAction\.sourceFrame\.request\.url[\s\S]*navigationAction\.targetFrame\?\.isMainFrame != false,[\s\S]*takeOverLibraryNavigation\(url, sourceURL: sourceURL\)[\s\S]*decisionHandler\(\.cancel\)/,
  );
  assert.match(
    APP_ROOT,
    /\.onReceive\(reader\.\$libraryPresentationRequestID\)[\s\S]*guard requestID != nil[\s\S]*showsLibrary = true/,
  );
});

test("legacy local navigation trusts the initiating frame and never fabricates capability routes", () => {
  assert.match(
    WEB_VIEW,
    /navigationAction\.sourceFrame\.isMainFrame[\s\S]*\? webView\.url[\s\S]*: navigationAction\.sourceFrame\.request\.url/,
  );
  assert.match(
    WEB_VIEW,
    /private func takeOverRemoteBookNavigation\([\s\S]*sourceURL: URL\?[\s\S]*isTrustedReaderURL\(sourceURL\)/,
  );
  assert.match(
    WEB_VIEW,
    /takeOverRemoteBookNavigation\(url, sourceURL: sourceURL\)/,
  );
  assert.doesNotMatch(WEB_VIEW, /localRuntimeRebasedURL/);
  assert.doesNotMatch(WEB_VIEW, /webView\.load\(URLRequest\(url: rebased\)\)/);
});

test("local activity and snapshot identity expose only opaque book IDs", () => {
  assert.match(SPOTLIGHT, /URLQueryItem\(name: "book", value: localBookID\)/);
  assert.match(SPOTLIGHT, /value\.hasPrefix\("localbook-"\), value\.count == 74/);
  assert.match(APP_ROOT, /ReaderLocalLibraryManager\.shared\.books\.first/);
  assert.match(APP_ROOT, /reader\.openLocalBook/);
  assert.match(NATIVE_FEATURES, /localBookID: clean\(window\.__BW_NATIVE_LOCAL_BOOK_ID__/);
  assert.match(NATIVE_FEATURES, /window\.__BW_NATIVE_LOCAL_READER__ === true\s*\? "" : clean\(location\.href/);
  assert.match(NATIVE_FEATURES, /isTrustedLocalRuntimeFeatureURL\(webView\.url\)/);
  assert.match(WEB_VIEW, /native-local:\/\/<capability-redacted>/);
  assert.match(WEB_VIEW, /return local/);
  assert.doesNotMatch(WEB_VIEW, /legacyReader|bwicarus\.taile44d0c\.ts\.net/);
  assert.doesNotMatch(NATIVE_FEATURES, /openNativeReaderURL|candidate\.port|bwicarus\.taile44d0c\.ts\.net/);
  assert.doesNotMatch(SPOTLIGHT, /readerURL|URLQueryItem\(name: "url"|bwicarus\.taile44d0c\.ts\.net/);
  assert.match(APP_ROOT, /@State private var showsLibrary = false/);
  assert.match(APP_ROOT, /reader\.setReaderScenePhase\(phase\)/);
  assert.match(WEB_VIEW, /case \.background:[\s\S]*readerWasBackgrounded = true/);
  assert.match(WEB_VIEW, /case \.inactive:[\s\S]*restartLocalRuntime: false/);
  assert.match(WEB_VIEW, /case \.active:[\s\S]*restartLocalRuntime: shouldRestart/);
  assert.doesNotMatch(WEB_VIEW, /"currentURL": webView\.url\?\.absoluteString/);
});

test("native WebView reloads only after server rebuild or content-process termination", () => {
  assert.match(WEB_VIEW, /private var webContentProcessNeedsReload = false/);
  assert.match(
    WEB_VIEW,
    /func webViewWebContentProcessDidTerminate\(_ webView: WKWebView\)[\s\S]*webContentProcessNeedsReload = true[\s\S]*guard readerForeground, isLocalRuntimeURL\(webView\.url\)[\s\S]*reloadLocalRuntimeAfterRecoveryIfNeeded\(serverRebuilt: false\)/,
  );
  assert.match(
    WEB_VIEW,
    /let restarted = try await localRuntimeServer[\s\S]*\.restartAfterForeground\(\)[\s\S]*reloadLocalRuntimeAfterRecoveryIfNeeded\([\s\S]*serverRebuilt: restarted/,
  );
  assert.match(
    WEB_VIEW,
    /private func reloadLocalRuntimeAfterRecoveryIfNeeded\([\s\S]{0,220}guard serverRebuilt \|\| webContentProcessNeedsReload else \{ return \}[\s\S]{0,320}webContentProcessNeedsReload = true[\s\S]{0,80}webView\.reload\(\)/,
  );
  assert.doesNotMatch(
    WEB_VIEW,
    /private func reloadLocalRuntimeAfterRecoveryIfNeeded\([\s\S]{0,520}webContentProcessNeedsReload = false/,
  );
  assert.match(
    WEB_VIEW,
    /didFinish navigation: WKNavigation![\s\S]*webContentProcessNeedsReload = false/,
  );
  assert.doesNotMatch(
    WEB_VIEW,
    /case \.inactive:[\s\S]{0,180}webView\.reload\(\)/,
  );
});

test("local WebView owns both bounded Pi gateway handlers on the main page world", () => {
  assert.match(WEB_VIEW, /ReaderNativePiGateway\(\s*webView: webView,\s*trustedBaseURL: localRuntimeServer\.baseURL/s);
  assert.match(WEB_VIEW, /ReaderNativePiSyncBridge\(\s*webView: webView,\s*trustedBaseURL: localRuntimeServer\.baseURL/s);
  assert.match(WEB_VIEW, /contentWorld: \.page,\s*name: ReaderNativePiSyncBridge\.messageName/s);
});

test("native Pi gateway canonicalizes first and authorizes through the packaged interface manifest", () => {
  assert.match(NATIVE_PI_GATEWAY, /canonicalRequestPath\(path\)/);
  assert.match(NATIVE_PI_GATEWAY, /components\.percentEncodedPath/);
  assert.match(NATIVE_PI_GATEWAY, /\["%2e", "%2f", "%5c"\]/);
  assert.match(NATIVE_PI_GATEWAY, /segment != "\." && segment != "\.\."/);
  assert.match(NATIVE_PI_GATEWAY, /ReaderNativeInterfaceManifest\(\)/);
  assert.match(NATIVE_PI_GATEWAY, /path: request\.routePath,[\s\S]*method: request\.method,[\s\S]*surface: surface/);
  assert.doesNotMatch(NATIVE_PI_GATEWAY, /let exact = Set\(\[/);
  assert.match(NATIVE_INTERFACE_MANIFEST, /reader-native-interface-manifest\/2/);
  assert.match(NATIVE_INTERFACE_MANIFEST, /native_reader_interface_manifest\.json/);
  assert.match(NATIVE_INTERFACE_MANIFEST, /appendingPathComponent\("ReaderBundle"/);
  assert.match(NATIVE_INTERFACE_MANIFEST, /route\.owner == \.pi/);
  assert.match(NATIVE_INTERFACE_MANIFEST, /route\.status == \.supported/);
  assert.match(NATIVE_INTERFACE_MANIFEST, /route\.methods\.contains\(method\)/);
  assert.match(NATIVE_INTERFACE_MANIFEST, /route\.surfaces\.contains\(surface\)/);

  const pathAllowed = (raw) => {
    let url;
    try {
      url = new URL(raw, "https://bwicarus.taile44d0c.ts.net");
    } catch {
      return false;
    }
    const encoded = raw.split("?", 1)[0].toLowerCase();
    if (["%2e", "%2f", "%5c"].some((token) => encoded.includes(token))) return false;
    const segments = raw.split("?", 1)[0].split("/").slice(1);
    if (segments.some((segment) => !segment || segment === "." || segment === "..")) return false;
    return url.pathname.startsWith("/api/assistant/") && url.pathname.length > "/api/assistant/".length;
  };
  assert.equal(pathAllowed("/api/assistant/thread-1"), true);
  assert.equal(pathAllowed("/api/assistant/../../admin"), false);
  assert.equal(pathAllowed("/api/assistant/%2e%2e/admin"), false);
  assert.equal(pathAllowed("/api/assistant/a%2fb"), false);
  assert.equal(pathAllowed("/api/assistant/a%5cb"), false);
});

test("native Pi gateway applies structured current/catalog book policies before network", () => {
  assert.match(
    NATIVE_INTERFACE_MANIFEST,
    /func piRoutePolicy\([\s\S]*\) -> ReaderNativePiRoutePolicy\?/,
  );
  assert.match(
    NATIVE_INTERFACE_MANIFEST,
    /remoteBook: route\.remoteBook/,
  );
  assert.match(
    REMOTE_LIBRARY,
    /func verifiedNativeRemoteBookBinding\([\s\S]*activeLibraryID == localBook\.libraryID[\s\S]*localDigest == remoteDigest/,
  );
  assert.match(
    NATIVE_PI_GATEWAY,
    /struct ReaderNativeRemoteBookBinding[\s\S]*localContentSHA256[\s\S]*remoteContentSHA256[\s\S]*remoteRelativePath/,
  );
  assert.match(
    NATIVE_PI_GATEWAY,
    /switch policy\.scope[\s\S]*case \.current:[\s\S]*currentRemoteBookBinding[\s\S]*case \.catalog:[\s\S]*catalogRemoteBookBindings/,
  );
  assert.match(
    NATIVE_PI_GATEWAY,
    /rewriteJSONPointer\([\s\S]*identity\.pointer[\s\S]*bindings: bindings/,
  );
  assert.match(
    NATIVE_PI_GATEWAY,
    /case \.prefixBeforeDelimiter:[\s\S]*value\.range\(of: "::"\)/,
  );
  assert.match(
    NATIVE_PI_GATEWAY,
    /continuations\[rid\][\s\S]*registered\.epoch == scopeEpoch/,
  );
  assert.match(
    NATIVE_PI_GATEWAY,
    /prefix == binding\.remoteRelativePath/,
  );
  assert.doesNotMatch(
    NATIVE_PI_GATEWAY,
    /replacingOccurrences\([\s\S]*remoteRelativePath/,
  );
  assert.match(
    WEB_VIEW,
    /currentLocalLibrary\.books\.compactMap[\s\S]*verifiedNativeRemoteBookBinding[\s\S]*updateTrustedRemoteBookBindings/,
  );
  assert.match(
    WEB_VIEW,
    /Publishers\.CombineLatest3\([\s\S]*verifiedNativeRemoteBookBinding/,
  );
  assert.match(APP_ROOT, /reader\.bind\(remoteLibrary: remoteLibrary\)/);
  assert.match(
    APP_ROOT,
    /await remoteLibrary\.refresh\([\s\S]*localLibrary: localLibrary/,
  );
});
