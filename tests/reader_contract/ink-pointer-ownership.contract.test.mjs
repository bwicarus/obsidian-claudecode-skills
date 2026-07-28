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

function section(source, start, end) {
  const from = source.indexOf(start);
  const to = source.indexOf(end, from + start.length);
  assert.notEqual(from, -1, `missing ${start}`);
  assert.notEqual(to, -1, `missing ${end}`);
  return source.slice(from, to);
}

for (const [name, source, nextAfterUp] of [
  ["PDF", PDF, "window._inkPointerDown = _inkPointerDown;"],
  ["EPUB", EPUB, "  // Apple Pencil 触摸"],
]) {
  test(`${name} 手写只消费发起笔画的 pointerId，并在结束时释放捕获`, () => {
    assert.match(source, /pid:\s*e\.pointerId/);
    assert.match(source, /captureEl:/);

    const move = section(
      source,
      "function _inkPointerMove(e)",
      "function _inkPointerUp(e)",
    );
    assert.match(move, /if \(e\.pointerId !== d\.pid\) return;/);
    assert.ok(
      move.indexOf("e.pointerId !== d.pid") < move.indexOf("e.preventDefault()"),
      "foreign touch must be released before preventDefault",
    );

    const up = section(source, "function _inkPointerUp(e)", nextAfterUp);
    assert.match(up, /if \(e\.pointerId !== d\.pid\) return;/);
    assert.match(up, /releasePointerCapture\(d\.pid\)/);
  });
}
