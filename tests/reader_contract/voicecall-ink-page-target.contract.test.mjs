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
