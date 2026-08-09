import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const GATEWAY = read("ios/BWReader/App/ReaderNativePiGateway.swift");
const PROXY = read("ios/BWReader/App/ReaderNativePiProxy.swift");
const SERVER = read("ios/BWReader/App/ReaderLocalRuntimeServer.swift");
const WEB_VIEW = read("ios/BWReader/App/ReaderWebView.swift");
const MANIFEST = JSON.parse(read("ios/BWReader/native_reader_interface_manifest.json"));

test("native Pi fetch returns a one-use loopback stream instead of a buffered base64 body", () => {
  assert.match(GATEWAY, /reader-native-pi-response\/2/);
  assert.match(GATEWAY, /"streamURL": streamURL\.absoluteString/);
  assert.match(GATEWAY, /issueAuthorizedRequest\(/);
  assert.doesNotMatch(GATEWAY, /session\.data\(for:/);
  assert.doesNotMatch(GATEWAY, /bodyBase64/);

  assert.match(PROXY, /tickets\.removeValue\(forKey: ticketToken\)/);
  assert.match(PROXY, /expiresAt: now\.addingTimeInterval\(15\)/);
  assert.match(PROXY, /HTTPBodySequence\([\s\S]*from: bytes/);
  assert.match(PROXY, /bodyContinuation\.yield\(data\)/);
  assert.match(PROXY, /willPerformHTTPRedirection[\s\S]*completionHandler\(nil\)/);
  assert.match(PROXY, /onTermination[\s\S]*self\?\.cancel\(\)/);
});

test("binary request schema decodes canonical base64 with the Pi 8 MiB raw limit", () => {
  assert.match(GATEWAY, /reader-native-pi-request\/2/);
  assert.match(GATEWAY, /"bodyEncoding"/);
  assert.match(GATEWAY, /case "utf8"/);
  assert.match(GATEWAY, /case "base64"/);
  assert.match(GATEWAY, /Data\(base64Encoded: bodyText, options: \[\]\)/);
  assert.match(GATEWAY, /data\.base64EncodedString\(\) == bodyText/);
  assert.match(GATEWAY, /maximumRequestBytes = 8 \* 1_024 \* 1_024/);
  assert.match(GATEWAY, /request\.httpBody = input\.body\.isEmpty \? nil : input\.body/);
});

test("book or catalog changes revoke unused tickets, continuations and active streams", () => {
  assert.match(GATEWAY, /scopeEpoch &\+= 1/);
  assert.match(GATEWAY, /continuations\.removeAll/);
  assert.match(GATEWAY, /piProxyBroker\.rotateScope\(to: scopeEpoch\)/);
  assert.match(PROXY, /tickets\.removeAll/);
  assert.match(PROXY, /transports\.forEach \{ \$0\.cancel\(\) \}/);
  assert.match(WEB_VIEW, /currentLocalLibrary\.books\.compactMap/);
  assert.match(WEB_VIEW, /updateTrustedRemoteBookBindings\([\s\S]*current: currentBinding,[\s\S]*catalog: catalogBindings/);
});

test("direct media/image resources require the capability shell referer and manifest authorization", () => {
  assert.match(SERVER, /isDirectPiResourcePath\(decodedPath\)/);
  assert.match(SERVER, /trustedResourceSurface\([\s\S]*Referer/);
  assert.match(SERVER, /\/r\/\\\(capabilityToken\)\/shells\//);
  assert.match(SERVER, /responseForResource/);
  assert.match(GATEWAY, /prepareAuthorizedResource[\s\S]*piRoutePolicy\([\s\S]*method: "GET"/);
  for (const route of [
    "/pdf/api/page-image",
    "/pdf/api/figure-crop",
    "/pdf/api/img-proxy",
    "/pdf/api/reader-events",
    "/pdf/api/vocab-audio",
    "/pdf/api/asset/",
    "/api/assistant/voice-clip/",
  ]) {
    assert.ok(SERVER.includes(route), `missing direct resource route ${route}`);
  }
  assert.match(SERVER, /HTTPHeader\("Referrer-Policy"\): "same-origin"/);
  assert.match(SERVER, /if headers\[HTTPHeader\("Referrer-Policy"\)\] == nil[\s\S]*"no-referrer"/);
});

test("direct asset requests force Pi byte relay without dropping manifest or cookie authorization", () => {
  assert.match(GATEWAY, /let authorizedTarget = try Self\.forceAssetProxyMode\(canonical\)/);
  assert.match(GATEWAY, /path: authorizedTarget/);
  assert.match(GATEWAY, /forceAssetProxyMode[\s\S]*hasPrefix\("\/pdf\/api\/asset\/"\)/);
  assert.match(GATEWAY, /components\.queryItems = \[URLQueryItem\(name: "proxy", value: "1"\)\]/);
  assert.match(GATEWAY, /forceAssetProxyMode[\s\S]*canonicalRequestPath\(candidate\)[\s\S]*forced\.path == canonical\.path/);
  assert.match(GATEWAY, /prepareAuthorizedResource[\s\S]*piRoutePolicy\([\s\S]*let authorization = try authorize/);
  assert.match(GATEWAY, /prepareProxyRequest[\s\S]*cookies\([\s\S]*HTTPCookie\.requestHeaderFields/);
  assert.match(PROXY, /willPerformHTTPRedirection[\s\S]*completionHandler\(nil\)/);
});

test("native PDF metadata is real, capability-scoped and keeps the old response fields", () => {
  assert.match(SERVER, /case "native-api\/book-meta"/);
  assert.match(SERVER, /state\.access\(for: opaqueBookID\)/);
  assert.match(SERVER, /PDFDocument\(url: access\.url\)/);
  assert.match(SERVER, /"page_count": document\.pageCount/);
  assert.match(SERVER, /"page_w": Double\(bounds\.width\)/);
  assert.match(SERVER, /"page_h": Double\(bounds\.height\)/);
  assert.match(SERVER, /let modifiedAt = access\.record\.modifiedAt[\s\S]*"mtime": Int\(modifiedAt\.timeIntervalSince1970\)/);
});

test("local PDF page pixels are rendered and bounded by PDFKit without Pi", () => {
  const route = MANIFEST.routes.find((candidate) => candidate.path === "/pdf/api/page-image");
  assert.equal(route?.owner, "local");
  assert.deepEqual(route?.surfaces, ["pdf"]);
  assert.equal(route?.remoteBook, null);
  assert.match(SERVER, /private actor ReaderNativePDFPageRenderer/);
  assert.match(SERVER, /page\.thumbnail\(of: targetSize, for: \.cropBox\)/);
  assert.match(SERVER, /format\.opaque = true/);
  assert.match(SERVER, /setFillColor\(UIColor\.white\.cgColor\)/);
  assert.match(SERVER, /image\.jpegData\(compressionQuality: 0\.9\)/);
  assert.match(SERVER, /imageCache\.totalCostLimit = 96 \* 1_024 \* 1_024/);
  assert.match(SERVER, /let pixelWidth = min\(3_000, max\(400, requestedWidth\)\)/);
  assert.match(SERVER, /"X-BW-PDF-Renderer"\): "pdfkit"/);
  assert.match(SERVER, /cacheControl: "private, max-age=31536000, immutable"/);
});

test("PDFKit serves the legacy TOC shape locally without a Pi book dependency", () => {
  const route = MANIFEST.routes.find((candidate) => candidate.path === "/pdf/api/toc");
  assert.equal(route?.owner, "native");
  assert.deepEqual(route?.methods, ["GET"]);
  assert.equal(route?.remoteBook, null);
  assert.match(SERVER, /decodedPath == "\/pdf\/api\/toc"/);
  assert.match(SERVER, /trustedResourceSurface\([\s\S]*\) == \.pdf/);
  assert.match(SERVER, /let entries = Self\.nativeTOCEntries\(in: document\)/);
  assert.match(SERVER, /document\.outlineRoot/);
  assert.match(SERVER, /"title": title,[\s\S]*"page": pageIndex \+ 1,[\s\S]*"level": max\(1, level\)/);
  assert.match(SERVER, /"exists": !entries\.isEmpty/);
  assert.match(SERVER, /"source": entries\.isEmpty \? "none" : "native"/);
  assert.match(SERVER, /if request\.query\["entries"\] != nil/);
});

test("disabled native notes return an explicit conflict instead of falling through to loopback 404", () => {
  assert.match(WEB_VIEW, /guard manager\.isEnabled else \{[\s\S]*"status": 409/);
  assert.match(WEB_VIEW, /BW_NATIVE_NOTES_DISABLED/);
  assert.match(WEB_VIEW, /未向 Pi 或回环地址假提交/);
});

test("legacy cross-book navigation stays inside the native local-library lifecycle", () => {
  assert.match(WEB_VIEW, /takeOverRemoteBookNavigation\(url, sourceURL: sourceURL\)/);
  assert.match(WEB_VIEW, /\["\/pdf\/view", "\/pdf\/epub\/view"\]\.contains\(url\.path\)/);
  assert.match(WEB_VIEW, /decisionHandler\(\.cancel\)[\s\S]*takeOverRemoteBookNavigation|takeOverRemoteBookNavigation[\s\S]*decisionHandler\(\.cancel\)/);
  assert.match(WEB_VIEW, /Self\.isSafeRemoteLibraryRelativePath\(fileValues\[0\]\)/);
  assert.match(WEB_VIEW, /\(1\.\.\.10_000_000\)\.contains\(page\)/);
  assert.match(WEB_VIEW, /remoteLibraryCoordinator\.localBookID\([\s\S]*verifiedNativeRemoteBookBinding/);
  assert.match(WEB_VIEW, /remoteLibraryCoordinator\.download\([\s\S]*remoteBook,[\s\S]*localLibrary: localLibrary/);
  assert.match(WEB_VIEW, /fetchAndStageUserState\([\s\S]*for: remoteBook,[\s\S]*localBook: downloaded/);
  assert.match(WEB_VIEW, /restorationToken: nil,[\s\S]*initialPage: initialPage/);
  assert.doesNotMatch(WEB_VIEW, /UIApplication\.shared\.open\(url\)[\s\S]*takeOverRemoteBookNavigation/);
});
