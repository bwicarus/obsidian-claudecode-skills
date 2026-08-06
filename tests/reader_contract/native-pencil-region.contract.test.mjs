import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const LIVE = read("ios/BWReader/App/NativePencilLiveOverlay.swift");
const SETTINGS = read("ios/BWReader/App/NativePencilSettings.swift");
const ANNOTATION = read("ios/BWReader/App/NativePencilAnnotation.swift");
const WEBVIEW = read("ios/BWReader/App/ReaderWebView.swift");
const RC_SETTINGS = read("_server_deploy/static/pdf/rc-settings.js");
const REGISTRY = read("_server_deploy/static/reader-runtime/data-registry.js");
const SETTINGS_SYNC = read("extensions/bw-reader-webext/src/settings-sync.js");

test("App exposes selection pen in native tools and Pencil gesture settings", () => {
  assert.match(SETTINGS, /case toggleSelection = "toggle-selection"/);
  assert.match(ANNOTATION, /切换画笔与选区笔/);
  assert.match(LIVE, /case selection/);
  assert.match(LIVE, /Image\(systemName: "lasso"\)/);
  assert.match(WEBVIEW, /case toggleSelection = "toggle-selection"/);
  assert.match(WEBVIEW, /nativePencilInk\.toggleSelection\(\)/);
});

test("native selection is a bounded canonical region operation, not a pen commit", () => {
  assert.match(LIVE, /case createRegion/);
  assert.match(LIVE, /closedRegionSegments/);
  assert.match(LIVE, /prefix\(511\)/);
  assert.match(LIVE, /points\.append\(first\)/);
  assert.match(LIVE, /regionId: regionId/);
  assert.match(LIVE, /createdAtEpochMs:/);
  assert.match(LIVE, /payload\["regionId"\]/);
  assert.match(LIVE, /payload\["createdAtEpochMs"\]/);
  assert.match(LIVE, /pathTool == \.selection \? \.createRegion : \.erase/);
});

test("native palette follows Pencil hover or the last finished path", () => {
  assert.match(LIVE, /@Published private\(set\) var paletteAnchor: CGPoint\?/);
  assert.match(LIVE, /UIHoverGestureRecognizer/);
  assert.match(LIVE, /trackPencilHover/);
  assert.match(LIVE, /gesture\.state == \.ended[\s\S]{0,120}updatePaletteAnchor/);
  assert.match(LIVE, /position\(palettePosition\(in: geometry\.size\)\)/);
});

test("touch double-tap choice is shared across Reader and extension sites", () => {
  assert.match(RC_SETTINGS, /rc-ink-double-tap-action/);
  assert.match(RC_SETTINGS, /value="eraser"/);
  assert.match(RC_SETTINGS, /value="selection"/);
  assert.match(RC_SETTINGS, /value="none"/);
  assert.match(SETTINGS_SYNC, /rc-ink-/);
  assert.match(REGISTRY, /legacyKey: 'rc-ink-double-tap-action'/);
  assert.match(LIVE, /event\?\.type == \.hover/);
  assert.match(WEBVIEW, /body\["type"\] as\? String == "tool"/);
  assert.match(WEBVIEW, /case "selection": nativePencilInk\.select\(\.selection\)/);
});
