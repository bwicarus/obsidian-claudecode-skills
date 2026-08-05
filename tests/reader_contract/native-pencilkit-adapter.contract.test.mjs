import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const PDF = readFileSync(
  new URL("_server_deploy/static/pdf/pdf-tail.js", ROOT),
  "utf8",
);
const EPUB = readFileSync(
  new URL("_server_deploy/static/pdf/epub-html.js", ROOT),
  "utf8",
);
const SWIFT = readFileSync(
  new URL("ios/BWReader/App/NativePencilLiveOverlay.swift", ROOT),
  "utf8",
);
const WEBVIEW = readFileSync(
  new URL("ios/BWReader/App/ReaderWebView.swift", ROOT),
  "utf8",
);

test("App-only PencilKit ownership leaves a web fallback when native layout is stale", () => {
  assert.match(WEBVIEW, /window\.__BW_NATIVE_PENCILKIT_INK__ = true/);
  for (const source of [PDF, EPUB]) {
    const pointerStart = source.indexOf("function _inkPointerDown(e)");
    const pointerEnd = source.indexOf("function _inkPointerMove(e)", pointerStart);
    assert.ok(pointerStart >= 0 && pointerEnd > pointerStart);
    assert.doesNotMatch(
      source.slice(pointerStart, pointerEnd),
      /__BW_NATIVE_PENCILKIT_INK__|__bwNativeInkHost/,
    );
  }
});

test("native strokes use frozen stable surfaces and the existing save path", () => {
  assert.match(SWIFT, /strokeLayout = controller\.layout/);
  assert.match(SWIFT, /surfaceId/);
  assert.match(SWIFT, /private var pending: \[NativeInkOperation\]/);
  assert.match(SWIFT, /while let operation = self\.pending\.first/);
  assert.match(SWIFT, /controller\.report\(error\)[\s\S]{0,80}break/);
  assert.match(SWIFT, /documentGeneration/);
  assert.match(SWIFT, /confirmedStrokeCount/);

  assert.match(PDF, /\.page-wrap\[data-page-num\], \.pdf-upage/);
  assert.match(PDF, /_inkScheduleSave\(segment\.pw/);
  assert.match(EPUB, /_inkScheduleSave\(segment\.el, segment\.idx\)/);
});

test("navigation identity and operation ids make native retries fail closed and idempotent", () => {
  assert.match(WEBVIEW, /didStartProvisionalNavigation[\s\S]{0,220}invalidateDocument\(\)/);
  assert.match(SWIFT, /let documentToken: String/);
  assert.match(SWIFT, /"opId": operation\.id/);
  assert.match(SWIFT, /"documentToken": operation\.documentToken/);
  for (const source of [PDF, EPUB]) {
    assert.match(source, /documentToken: documentToken/);
    assert.match(source, /input\.documentToken !== documentToken/);
    assert.match(source, /appliedOps\[opId\]\.state === 'applied'/);
    assert.match(source, /rememberOperation\(opId/);
    assert.match(source, /operation\.state = 'applied'/);
  }
});

test("native layout excludes controls and preserves EPUB safety gates", () => {
  for (const source of [PDF, EPUB]) {
    assert.match(source, /'\.rc-note'/);
    assert.match(source, /interactiveSelector/);
    assert.match(source, /bwNativePencilInk/);
  }
  assert.match(EPUB, /favPdfLoaded/);
  assert.match(EPUB, /fav-up-editing/);
  assert.match(EPUB, /ep-up-editing/);
});

test("Swift keeps uncommitted PencilKit strokes until the host accepts them", () => {
  const evaluateIndex = SWIFT.indexOf("applyNativePencilOperation(operation)");
  const confirmIndex = SWIFT.indexOf("confirmedStrokeCount +=", evaluateIndex);
  assert.ok(evaluateIndex >= 0 && confirmIndex > evaluateIndex);
  assert.match(SWIFT, /guard !interactionActive/);
  assert.match(SWIFT, /点按重试|retryRequest/);
});
