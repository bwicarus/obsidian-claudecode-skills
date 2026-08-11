import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");

const CAPTURE = read("ios/BWReader/App/ReaderWebViewNativeFeatures.swift");
const SERVER = read("ios/BWReader/App/ReaderLocalRuntimeServer.swift");
const WEBVIEW = read("ios/BWReader/App/ReaderWebView.swift");
const PENCIL = read("ios/BWReader/App/NativePencilLiveOverlay.swift");
const VOICE = read("_server_deploy/static/pdf/rc-voicecall.js");

test("App visual capture renders the shared WKWebView and PencilKit hierarchy", () => {
  const broker = CAPTURE.slice(
    CAPTURE.indexOf("@MainActor\nfinal class ReaderNativeVisualCaptureBroker"),
    CAPTURE.indexOf("private struct NativeReaderJavaScriptSnapshot"),
  );
  assert.match(broker, /@MainActor\s+final class ReaderNativeVisualCaptureBroker/);
  assert.match(broker, /private weak var webView: WKWebView\?/);
  assert.match(broker, /private weak var pencilCanvas: UIView\?/);
  assert.match(broker, /lowestCommonAncestor\(webView, pencilCanvas\)/);
  assert.match(broker, /webView\.convert\(webView\.bounds, to: host\)/);
  assert.match(broker, /host\.drawHierarchy\([\s\S]*afterScreenUpdates: true/);
  assert.doesNotMatch(broker, /webView\.takeSnapshot/);
  assert.doesNotMatch(broker, /base64EncodedString/);
});

test("native hierarchy capture registers and unregisters the real PencilKit canvas", () => {
  assert.match(
    WEBVIEW,
    /visualCaptureBroker\.bind\(\s*webView: webView,\s*pencilCanvas: canvas/s,
  );
  assert.match(
    WEBVIEW,
    /visualCaptureBroker\.unbind\(\s*pencilCanvas: canvas/s,
  );
  const make = PENCIL.slice(
    PENCIL.indexOf("func makeUIView"),
    PENCIL.indexOf("@MainActor\n    final class Coordinator"),
  );
  assert.match(make, /reader\.bindNativeVisualCaptureCanvas\(canvas\)/);
  assert.match(make, /static func dismantleUIView/);
  assert.match(make, /coordinator\.reader\.unbindNativeVisualCaptureCanvas\(canvas\)/);
});

test("capture encoding stays inside the existing Realtime image envelope", () => {
  assert.match(CAPTURE, /maximumLongEdge: CGFloat = 1_600/);
  assert.match(CAPTURE, /maximumJPEGBytes = 675_000/);
  assert.match(CAPTURE, /compressionQualities: \[CGFloat\] = \[0\.85, 0\.70, 0\.50\]/);
  assert.match(CAPTURE, /fallbackLongEdges: \[CGFloat\] = \[1_280, 1_024, 800\]/);
  assert.match(CAPTURE, /data\.count <= maximumJPEGBytes/);
  assert.match(CAPTURE, /BW_NATIVE_VISUAL_IMAGE_TOO_LARGE/);
});

test("the loopback route is capability-prefixed, trusted-shell-only, and diagnostic", () => {
  const prefixGuard = SERVER.indexOf(
    'let prefix = "/r/\\(capabilityToken)/"',
  );
  const route = SERVER.indexOf('case "native-api/visual-capture"');
  assert.ok(prefixGuard >= 0 && route > prefixGuard);
  const handler = SERVER.slice(
    SERVER.indexOf("private func serveNativeVisualCapture"),
    SERVER.indexOf("private func serveNativePageImage"),
  );
  assert.match(handler, /request\.method == \.GET/);
  assert.match(handler, /request\.headers\[HTTPHeader\("Referer"\)\]/);
  assert.match(handler, /trustedResourceURL\(referer: referer\)/);
  assert.match(handler, /trustedResourceSurface\(referer: referer\)/);
  assert.match(handler, /scope == "viewport"/);
  assert.match(handler, /scope == "region"/);
  for (const key of ["x", "y", "w", "h"]) {
    assert.match(handler, new RegExp(`request\\.query\\["${key}"\\]`));
  }
  assert.match(handler, /contentType: "image\/jpeg"/);
  assert.match(handler, /cacheControl: "no-store"/);
  assert.match(handler, /"X-BW-Visual-Capture"[\s\S]*"native-hierarchy\/1"/);
  assert.match(handler, /"X-BW-Reader-Error"/);
  assert.match(handler, /nativeVisualErrorResponse/);
});

test("offscreen PDF capture composes PDFKit pixels with authoritative page ink", () => {
  const broker = CAPTURE.slice(
    CAPTURE.indexOf("@MainActor\nfinal class ReaderNativeVisualCaptureBroker"),
    CAPTURE.indexOf("private struct NativeReaderJavaScriptSnapshot"),
  );
  const handler = SERVER.slice(
    SERVER.indexOf("private func serveNativeVisualCapture"),
    SERVER.indexOf("private func serveNativePageImage"),
  );
  assert.match(handler, /scope == "page"/);
  assert.match(handler, /trustedSurface == \.pdf/);
  assert.match(handler, /pageRenderer\.jpegData\(/);
  assert.match(handler, /visualCaptureBroker\.capturePDFPage\(/);
  assert.match(handler, /"X-BW-Visual-Capture"\): "native-pdf-page\/1"/);
  assert.match(handler, /"X-BW-Visual-Ink"/);
  assert.match(handler, /strokeCount > 0 \? "present" : "none"/);

  assert.match(broker, /func capturePDFPage\(/);
  assert.match(broker, /fetch\([\s\S]*"\/pdf\/api\/ink\?file="/);
  assert.match(broker, /window\._ink && window\._ink\.dirty/);
  assert.match(broker, /baseImage\.draw\(in: pageBounds\)/);
  assert.match(broker, /Self\.drawInk\(strokes/);
  assert.match(broker, /inkStrokeCount: strokes\.count/);
  assert.doesNotMatch(broker, /capabilityToken/);
});

test("shared visual tools consume the native viewport, region, and offscreen page routes", () => {
  const native = VOICE.slice(
    VOICE.indexOf("function _nativeCaptureBase"),
    VOICE.indexOf("function _visualSurface"),
  );
  assert.match(native, /__BW_NATIVE_LOCAL_BASE_PATH__/);
  assert.match(native, /\/native-api\/visual-capture\?/);
  assert.match(native, /scope=' \+ encodeURIComponent\(scope\)/);
  assert.match(native, /q \+= '&page=' \+ encodeURIComponent\(String\(page\)\)/);
  assert.match(native, /X-BW-Reader-Error/);
  assert.match(native, /X-BW-Visual-Ink/);
  assert.match(native, /rect\.visible >= 0\.9[\s\S]*_nativeCapture\('region'/);
  assert.match(
    native,
    /_nativeCapture\('page', \{ x: x0, y: y0, w: x1 - x0, h: y1 - y0 \}, pageNo\)/,
  );
  assert.doesNotMatch(native, /dlog\([^\n]*__BW_NATIVE_LOCAL_BASE_PATH__/);

  const ink = VOICE.slice(
    VOICE.indexOf("function _curInkPageEl"),
    VOICE.indexOf("try {\n    window.RC = window.RC || {}"),
  );
  assert.match(ink, /_curInkPageEl\(target, true\)/);
  assert.match(ink, /var natInk = await _nativeInkRegion\(el, x0, y0, x1, y1\)/);
  assert.ok(
    ink.indexOf("var natInk = await _nativeInkRegion") <
      ink.indexOf("await _loadH2C()"),
    "the App native route must run before the web fallback",
  );
});

test("every native visual early exit has a stable visible error code", () => {
  for (const code of [
    "BW_NATIVE_VISUAL_INVALID_REGION",
    "BW_NATIVE_VISUAL_INVALID_PAGE",
    "BW_NATIVE_VISUAL_PAGE_SCOPE_UNSUPPORTED",
    "BW_NATIVE_VISUAL_BOOK_UNAVAILABLE",
    "BW_NATIVE_VISUAL_PAGE_UNAVAILABLE",
    "BW_NATIVE_VISUAL_PENCIL_OVERLAY_UNAVAILABLE",
    "BW_NATIVE_VISUAL_HIERARCHY_UNAVAILABLE",
    "BW_NATIVE_VISUAL_EMPTY_VIEWPORT",
    "BW_NATIVE_VISUAL_RENDER_FAILED",
    "BW_NATIVE_VISUAL_PDF_RENDER_FAILED",
    "BW_NATIVE_VISUAL_INK_UNAVAILABLE",
    "BW_NATIVE_VISUAL_INK_INVALID",
    "BW_NATIVE_VISUAL_INK_TOO_LARGE",
    "BW_NATIVE_VISUAL_PAGE_COMPOSITE_FAILED",
    "BW_NATIVE_VISUAL_JPEG_FAILED",
    "BW_NATIVE_VISUAL_IMAGE_TOO_SMALL",
    "BW_NATIVE_VISUAL_IMAGE_TOO_LARGE",
  ]) {
    assert.match(CAPTURE, new RegExp(code));
  }
  assert.doesNotMatch(CAPTURE, /catch\s*\{\s*return nil/);
});
