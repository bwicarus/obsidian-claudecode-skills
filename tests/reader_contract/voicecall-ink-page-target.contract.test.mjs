import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const SOURCE = fs.readFileSync(
  "_server_deploy/static/pdf/rc-voicecall.js",
  "utf8",
);
const INK_SOURCE = fs.readFileSync(
  "_server_deploy/static/pdf/rc-ink.js",
  "utf8",
);
const WEB_INK_SOURCE = fs.readFileSync(
  "extensions/bw-reader-webext/src/web-ink.js",
  "utf8",
);

function loadRealInkPagePicker(elements) {
  const start = SOURCE.indexOf("  function _inkTargetPage(target) {");
  const end = SOURCE.indexOf("  async function _captureInkRegion(target) {");
  assert.ok(start >= 0 && end > start, "real ink page picker source missing");
  return vm.runInNewContext(
    `(function () {
${SOURCE.slice(start, end)}
      return _curInkPageEl;
    })()`,
    {
      document: {
        querySelectorAll(selector) {
          assert.equal(
            selector,
            ".page-wrap[data-page-num], .pdf-upage, " +
              ".ep-sec[data-idx], .ep-usec[data-uid]",
          );
          return elements;
        },
      },
      window: { innerHeight: 900 },
    },
  );
}

function loadRealScopedCropHelpers() {
  const start = SOURCE.indexOf("  function _visualCaptureScope(target) {");
  const end = SOURCE.indexOf("  function _drawSurfaceInk(", start);
  assert.ok(start >= 0 && end > start, "real scoped crop helpers missing");
  return vm.runInNewContext(
    `(function () {
${SOURCE.slice(start, end)}
      return {
        scope: _visualCaptureScope,
        selectionId: _visualCaptureSelectionId,
        crop: _surfaceInkCrop
      };
    })()`,
  );
}

function loadRealCompositePageMatcher() {
  const start = SOURCE.indexOf("  function _compositeTargetPage(target) {");
  const end = SOURCE.indexOf("  function _inkTargetPage(target) {");
  assert.ok(
    start >= 0 && end > start,
    "real composite page matcher source missing",
  );
  return vm.runInNewContext(
    `(function () {
${SOURCE.slice(start, end)}
      return {
        target: _compositeTargetPage,
        matches: _compositePageMatches
      };
    })()`,
  );
}

function loadStableSelectionHelpers(strokes) {
  const inkWindow = {};
  vm.runInNewContext(INK_SOURCE, { window: inkWindow });
  const start = SOURCE.indexOf("  function _selectionRegionsForPage(target) {");
  const end = SOURCE.indexOf("  // 用户点子:前端截图但", start);
  assert.ok(start >= 0 && end > start, "real selection region index missing");
  const selectionRegions = vm.runInNewContext(
    `(function (surface) {
      function _visualSurface() { return surface; }
      function _curInkPageEl() { return null; }
${SOURCE.slice(start, end)}
      return _selectionRegionsForPage;
    })(surface)`,
    {
      surface: { strokes },
      window: { RCInk: inkWindow.RCInk },
      RCInk: inkWindow.RCInk,
    },
  );
  return { ink: inkWindow.RCInk, selectionRegions };
}

function visibleInkPage(page) {
  return {
    dataset: { pageNum: String(page) },
    __inkStrokes: [{ p: [[0.1, 0.1], [0.2, 0.2]] }],
    getBoundingClientRect() {
      return { top: 10, bottom: 800 };
    },
  };
}

test("captureInkRegion 页目标在双页可见时精确选页，无参保持旧行为", () => {
  const page21 = visibleInkPage(21);
  const page22 = visibleInkPage(22);
  const inserted23 = {
    dataset: {},
    __upRec: { page: 23 },
    __inkStrokes: [{ p: [[0.2, 0.2], [0.3, 0.3]] }],
    getBoundingClientRect() {
      return { top: 100, bottom: 850 };
    },
  };
  const epub24 = {
    // EPUB data-idx is zero-based while every voice/page tool uses one-based pages.
    dataset: { idx: "23" },
    __inkStrokes: [{ p: [[0.3, 0.3], [0.4, 0.4]] }],
    getBoundingClientRect() {
      return { top: 120, bottom: 820 };
    },
  };
  const pick = loadRealInkPagePicker([page21, page22, inserted23, epub24]);

  assert.equal(pick(), page21);
  assert.equal(pick({ page: 22 }), page22);
  assert.equal(pick({ page: "23" }), inserted23);
  assert.equal(pick({ page: 24 }), epub24);
  assert.equal(pick({ page: 25 }), null);
});

test("selection-near 只按精确 region id 取归一化外接框并保留上下文留白", () => {
  const helper = loadRealScopedCropHelpers();
  const surface = {
    width: 1000,
    height: 500,
    viewport: { width: 420, height: 280 },
    strokes: [
      {
        t: "pen",
        id: "r-target",
        p: [[0.01, 0.01], [0.99, 0.99]],
      },
      {
        t: "region",
        id: "r-other",
        p: [[0.7, 0.7], [0.8, 0.8], [0.75, 0.85]],
      },
      {
        t: "region",
        id: "r-target",
        p: [[0.2, 0.3], [0.3, 0.4], [0.25, 0.45]],
      },
    ],
  };

  assert.equal(helper.scope({ scope: "selection-near" }), "selection-near");
  assert.equal(helper.scope({ scope: "arbitrary" }), null);
  assert.equal(helper.selectionId({ selectionId: "r-target" }), "r-target");
  assert.equal(helper.selectionId({ selectionId: "bad selector[]" }), null);
  assert.deepEqual(
    { ...helper.crop(surface, "r-target") },
    { x: 40, y: 47.5, width: 420, height: 280 },
  );
  assert.equal(helper.crop(surface, "r-missing"), null);
});

test("App 原生 pts 笔迹与网页 p 笔迹使用同一合成图裁剪合同", () => {
  const helper = loadRealScopedCropHelpers();
  const nativeSurface = {
    width: 1000,
    height: 500,
    viewport: { width: 420, height: 280 },
    strokes: [{
      t: "pen",
      pts: [[0.2, 0.3], [0.3, 0.4], [0.25, 0.45]],
    }],
  };
  const webSurface = {
    ...nativeSurface,
    strokes: [{ t: "pen", p: nativeSurface.strokes[0].pts }],
  };

  assert.deepEqual(
    { ...helper.crop(nativeSurface, null) },
    { ...helper.crop(webSurface, null) },
  );
  assert.match(SOURCE, /function _visualStrokePoints\(stroke\)/);
  assert.doesNotMatch(
    SOURCE.slice(
      SOURCE.indexOf("  function _visualStrokePoints("),
      SOURCE.indexOf("  try {\n    window.RC = window.RC || {};"),
    ),
    /\(st\.p \|\| \[\]\)/,
  );
});

test("capturePageComposite 对 PDF、EPUB 与插入页使用同一精确页身份", () => {
  const matcher = loadRealCompositePageMatcher();
  assert.equal(matcher.target({ page: 22 }), "22");
  assert.equal(matcher.target("sec-3"), "sec-3");
  assert.equal(matcher.target({}), null);
  assert.equal(
    matcher.matches({ dataset: { pageNum: "22" } }, "22"),
    true,
  );
  assert.equal(
    matcher.matches({ dataset: { idx: "7" } }, "8"),
    true,
  );
  assert.equal(
    matcher.matches({ dataset: { idx: "7" } }, "7"),
    false,
  );
  assert.equal(
    matcher.matches({ dataset: { uid: "sec-3" } }, "sec-3"),
    true,
  );
  assert.equal(
    matcher.matches({ dataset: {}, __upRec: { page: 23 } }, "23"),
    true,
  );
  assert.equal(
    matcher.matches({ dataset: { pageNum: "21" } }, "22"),
    false,
  );
  assert.match(
    SOURCE,
    /RC\.capturePageComposite\s*=\s*_capturePageComposite/,
  );
  const bodyCaptureStart = SOURCE.indexOf(
    "  async function _captureBodyPageRect(",
  );
  const bodyCaptureEnd = SOURCE.indexOf(
    "  // 当前逻辑页的完整",
    bodyCaptureStart,
  );
  const bodyCapture = SOURCE.slice(bodyCaptureStart, bodyCaptureEnd);
  assert.match(
    bodyCapture,
    /captureRoot = document\.documentElement \|\| document\.body;[\s\S]*html2canvas\(captureRoot,/,
  );
  assert.match(bodyCapture, /id === 'bw-reader-host'/);
  assert.match(bodyCapture, /classes\.contains\('bw-ink-document'\)/);
  assert.match(bodyCapture, /classes\.contains\('bw-ink-canvas'\)/);
  assert.doesNotMatch(
    bodyCapture,
    /id\s*===\s*['"](?:bw-reader-pins|vc-cards)['"]/,
  );
  assert.match(
    SOURCE.slice(
      SOURCE.indexOf("  async function _capturePageComposite("),
      SOURCE.indexOf("  function _inkTargetPage("),
    ),
    /_captureBodyPageRect\(r\)\s*\|\|\s*await _captureEl\(el\)/,
  );
});

test("闭合选区 ordinal 创建后随笔画持久化，删除早期选区不重排后续编号", () => {
  const strokes = [
    { t: "region", id: "rg_a", createdAtEpochMs: 1000, p: [[0, 0], [1, 0], [0, 1]] },
    { t: "region", id: "rg_b", createdAtEpochMs: 2000, p: [[0, 0], [1, 0], [0, 1]] },
    { t: "region", id: "rg_c", createdAtEpochMs: 3000, p: [[0, 0], [1, 0], [0, 1]] },
  ];
  const initial = loadStableSelectionHelpers(strokes);
  initial.ink.ensureRegionOrdinals(strokes);
  assert.deepEqual(strokes.map((stroke) => stroke.ordinal), [1, 2, 3]);

  const reloadedAfterDelete = JSON.parse(JSON.stringify(strokes)).slice(1);
  const stable = loadStableSelectionHelpers(reloadedAfterDelete);
  assert.equal(stable.ink.nextRegionOrdinal(reloadedAfterDelete), 4);
  const items = Array.from(stable.selectionRegions({ page: 1 }).items);
  assert.deepEqual(
    items.map((item) => [item.selectionId, item.ordinal]),
    [["rg_b", 2], ["rg_c", 3]],
  );
  assert.match(items[0].label, /^#2 \d{2}:\d{2}$/);
  assert.match(items[1].label, /^#3 \d{2}:\d{2}$/);

  assert.match(WEB_INK_SOURCE, /ordinal:selection\?nextRegionOrdinal\(\):undefined/);
  assert.match(WEB_INK_SOURCE, /ordinal:current\.ordinal/);
  assert.doesNotMatch(
    WEB_INK_SOURCE,
    /forEach\(\(s,index\)=>map\.set\(s\.id,\{ordinal:index\+1/,
  );
});

test("普通网页 surface 合成保留 portal 卡片且只补画一次墨迹", () => {
  const start = SOURCE.indexOf("async function _captureSurface");
  const end = SOURCE.indexOf("async function _captureView", start);
  assert.ok(start >= 0 && end > start);
  const capture = SOURCE.slice(start, end);
  assert.match(capture, /s\.element === document\.body/);
  assert.match(capture, /document\.getElementById\('bw-reader-pins'\)/);
  assert.match(capture, /webPinRoot \? \(document\.documentElement \|\| s\.element\) : s\.element/);
  assert.match(capture, /!webPinRoot && id === 'bw-reader-pins'/);
  assert.match(capture, /classes\.contains\('bw-ink-document'\)/);
  assert.match(capture, /_drawSurfaceInk\(canvas, s, crop, selectionId\)/);
});
