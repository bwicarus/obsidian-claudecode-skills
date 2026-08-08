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
const NATIVE_PI_GATEWAY = read("ios/BWReader/App/ReaderNativePiGateway.swift");
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
});

test("native PDF mode bypasses PWA ownership and duplicate whole-book caching", () => {
  assert.match(PDF_BOOT, /window\.__BW_NATIVE_LOCAL_READER__ === true\s*\? false/);
  assert.match(PDF_BOOT, /window\.__BW_NATIVE_LOCAL_READER__ !== true && 'serviceWorker' in navigator/);
  assert.match(PDF_LOADER, /const _NATIVE_LOCAL_PDF = window\.__BW_NATIVE_LOCAL_READER__ === true/);
  assert.match(PDF_LOADER, /_NATIVE_LOCAL_PDF \? null : await _idbGet/);
  assert.match(PDF_LOADER, /if \(!_NATIVE_LOCAL_PDF && !_haveBuf/);
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
  assert.match(APP_ROOT, /@State private var showsLibrary = true/);
  assert.match(APP_ROOT, /reader\.setReaderScenePhase\(phase\)/);
  assert.match(WEB_VIEW, /case \.background:[\s\S]*readerWasBackgrounded = true/);
  assert.match(WEB_VIEW, /case \.inactive:[\s\S]*restartLocalRuntime: false/);
  assert.match(WEB_VIEW, /case \.active:[\s\S]*restartLocalRuntime: shouldRestart/);
  assert.doesNotMatch(WEB_VIEW, /"currentURL": webView\.url\?\.absoluteString/);
});

test("local WebView owns both bounded Pi gateway handlers on the main page world", () => {
  assert.match(WEB_VIEW, /ReaderNativePiGateway\(\s*webView: webView,\s*trustedBaseURL: localRuntimeServer\.baseURL/s);
  assert.match(WEB_VIEW, /ReaderNativePiSyncBridge\(\s*webView: webView,\s*trustedBaseURL: localRuntimeServer\.baseURL/s);
  assert.match(WEB_VIEW, /contentWorld: \.page,\s*name: ReaderNativePiSyncBridge\.messageName/s);
});

test("native Pi gateway rejects dot segments and encoded path separators before allowlisting", () => {
  assert.match(NATIVE_PI_GATEWAY, /canonicalRequestPath\(path\)/);
  assert.match(NATIVE_PI_GATEWAY, /components\.percentEncodedPath/);
  assert.match(NATIVE_PI_GATEWAY, /\["%2e", "%2f", "%5c"\]/);
  assert.match(NATIVE_PI_GATEWAY, /segment != "\." && segment != "\.\."/);
  assert.match(NATIVE_PI_GATEWAY, /isAllowed\(path: canonicalPath\.path\)/);

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
