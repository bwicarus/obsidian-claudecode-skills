import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");

const CAPTURE = read("ios/BWReader/App/ReaderWebViewNativeFeatures.swift");
const SERVER = read("ios/BWReader/App/ReaderLocalRuntimeServer.swift");
const WEBVIEW = read("ios/BWReader/App/ReaderWebView.swift");
const PENCIL = read("ios/BWReader/App/NativePencilLiveOverlay.swift");

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
  assert.match(handler, /trustedResourceSurface\([\s\S]*Referer/);
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

test("every native visual early exit has a stable visible error code", () => {
  for (const code of [
    "BW_NATIVE_VISUAL_INVALID_REGION",
    "BW_NATIVE_VISUAL_PAGE_UNAVAILABLE",
    "BW_NATIVE_VISUAL_PENCIL_OVERLAY_UNAVAILABLE",
    "BW_NATIVE_VISUAL_HIERARCHY_UNAVAILABLE",
    "BW_NATIVE_VISUAL_EMPTY_VIEWPORT",
    "BW_NATIVE_VISUAL_RENDER_FAILED",
    "BW_NATIVE_VISUAL_JPEG_FAILED",
    "BW_NATIVE_VISUAL_IMAGE_TOO_SMALL",
    "BW_NATIVE_VISUAL_IMAGE_TOO_LARGE",
  ]) {
    assert.match(CAPTURE, new RegExp(code));
  }
  assert.doesNotMatch(CAPTURE, /catch\s*\{\s*return nil/);
});
