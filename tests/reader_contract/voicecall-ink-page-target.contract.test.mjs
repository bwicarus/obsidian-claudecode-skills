import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const SOURCE = fs.readFileSync(
  "_server_deploy/static/pdf/rc-voicecall.js",
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
            ".page-wrap[data-page-num], .pdf-upage",
          );
          return elements;
        },
      },
      window: { innerHeight: 900 },
    },
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
  const pick = loadRealInkPagePicker([page21, page22, inserted23]);

  assert.equal(pick(), page21);
  assert.equal(pick({ page: 22 }), page22);
  assert.equal(pick({ page: "23" }), inserted23);
  assert.equal(pick({ page: 24 }), null);
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
    matcher.matches({ dataset: { idx: "7" } }, "7"),
    true,
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
  assert.match(bodyCapture, /html2canvas\(document\.body,/);
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
