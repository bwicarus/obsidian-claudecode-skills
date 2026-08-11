import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const SOURCE = fs.readFileSync(
  "_server_deploy/static/pdf/rc-voicecall.js",
  "utf8",
);

function loadVisualSurface(adapterRef, logs) {
  const start = SOURCE.indexOf("  var _visualSurfaceCapabilityState = '';");
  const end = SOURCE.indexOf("  function _visualCaptureScope(target) {", start);
  assert.ok(start >= 0 && end > start, "visual surface capability source missing");

  const RC = {
    adapter() {
      return adapterRef.current;
    },
  };
  return vm.runInNewContext(
    `(function () {
      function _visualNull(where, why) {
        logs.push(where + ' 放弃: ' + why);
        return null;
      }
      function _visualErrText(error) {
        return String(error && (error.message || error.name) || error);
      }
${SOURCE.slice(start, end)}
      return _visualSurface;
    })()`,
    { window: { RC }, RC, logs },
  );
}

test("缺失的可选 getVisualSurface 只报告一次，并在能力变化后重新探测", () => {
  const adapterRef = { current: {} };
  const logs = [];
  const visualSurface = loadVisualSurface(adapterRef, logs);

  for (let i = 0; i < 12; i += 1) {
    assert.equal(visualSurface(), null);
  }
  assert.deepEqual(logs, ["原生取图面 放弃: adapter 未实现 getVisualSurface"]);

  const surface = { element: {}, width: 1200, height: 900, strokes: [] };
  adapterRef.current.getVisualSurface = () => surface;
  assert.equal(visualSurface(), surface);
  assert.equal(logs.length, 1, "capability becoming available is not an error");

  delete adapterRef.current.getVisualSurface;
  assert.equal(visualSurface(), null);
  assert.equal(visualSurface(), null);
  assert.equal(logs.length, 2, "a real capability transition is reported once");
});
