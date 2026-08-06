import test from "node:test";
import assert from "node:assert/strict";
import vm from "node:vm";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const CORE = readFileSync(new URL("_server_deploy/static/pdf/rc-ink.js", ROOT), "utf8");
const PDF = readFileSync(new URL("_server_deploy/static/pdf/pdf-tail.js", ROOT), "utf8");
const EPUB = readFileSync(new URL("_server_deploy/static/pdf/epub-html.js", ROOT), "utf8");
const PDF_TEMPLATE = readFileSync(new URL("_server_deploy/templates/pdf_reader.html", ROOT), "utf8");
const EPUB_TEMPLATE = readFileSync(new URL("_server_deploy/templates/epub_html_reader.html", ROOT), "utf8");
const SWIFT = readFileSync(new URL("ios/BWReader/App/NativePencilLiveOverlay.swift", ROOT), "utf8");

function loadCore() {
  const window = { innerWidth: 1024, innerHeight: 768 };
  vm.runInNewContext(CORE, { window }, { filename: "rc-ink.js" });
  return window.RCInk;
}

function fakeCanvas() {
  const calls = [];
  const stack = [];
  const ctx = {
    globalAlpha: 1,
    calls,
    setTransform(...args) { calls.push(["setTransform", ...args]); },
    clearRect(...args) { calls.push(["clearRect", ...args]); },
    save() { stack.push({ globalAlpha: this.globalAlpha }); calls.push(["save"]); },
    restore() { Object.assign(this, stack.pop()); calls.push(["restore"]); },
    beginPath() { calls.push(["beginPath"]); },
    moveTo(...args) { calls.push(["moveTo", ...args]); },
    lineTo(...args) { calls.push(["lineTo", ...args]); },
    quadraticCurveTo(...args) { calls.push(["quadraticCurveTo", ...args]); },
    closePath() { calls.push(["closePath"]); },
    fill(...args) { calls.push(["fill", ...args]); },
    stroke() { calls.push(["stroke"]); },
    strokeRect(...args) { calls.push(["strokeRect", ...args]); },
    fillRect(...args) { calls.push(["fillRect", ...args]); },
    fillText(...args) { calls.push(["fillText", ...args]); },
    measureText(value) { return { width: String(value).length * 7 }; },
  };
  return {
    width: 100,
    height: 100,
    style: { width: "100px" },
    getContext() { return ctx; },
    ctx,
  };
}

function region(id, createdAtEpochMs, points = [[0.1, 0.1], [0.8, 0.1], [0.5, 0.8]]) {
  return { t: "region", id, createdAtEpochMs, c: "#0a84ff", w: 2, p: points };
}

test("shared region renders closed fill, boundary and deterministic dynamic labels", () => {
  const ink = loadCore();
  const canvas = fakeCanvas();
  const stamp = new Date(2026, 0, 1, 9, 7).getTime();
  ink.redraw(canvas, [region("b", stamp), region("a", stamp)], true);

  assert.equal(canvas.ctx.calls.filter(([name]) => name === "closePath").length, 2);
  assert.equal(canvas.ctx.calls.filter(([name]) => name === "fill").length, 2);
  assert.equal(canvas.ctx.calls.filter(([name]) => name === "stroke").length, 2);
  assert.deepEqual(
    canvas.ctx.calls.filter(([name]) => name === "fillText").map(([, value]) => value),
    ["#2 09:07", "#1 09:07"],
  );
  assert.equal(canvas.ctx.globalAlpha, 1, "region opacity must not leak into later strokes");
  assert.doesNotMatch(CORE, /localeCompare/);
});

test("region hit-testing includes its interior and closing edge, and eraser removes it whole", () => {
  const ink = loadCore();
  const target = region("one", 1);
  assert.equal(ink.hit(target, [0.5, 0.3], 0.01), true);
  assert.equal(ink.hit(target, [0.3, 0.45], 0.012), true, "closing edge must be hittable");
  assert.equal(ink.hit(target, [0.98, 0.98], 0.01), false);
  const strokes = [target, { t: "pen", p: [[0.9, 0.9], [1, 1]] }];
  assert.equal(ink.eraseAt(strokes, [0.5, 0.3], 0.01), true);
  assert.equal(strokes.length, 1);
  assert.equal(strokes[0].t, "pen");
});

test("region paths stay bounded without imposing a region-count limit", () => {
  const ink = loadCore();
  assert.equal(ink.REGION_MAX_POINTS, 512);
  const canvas = fakeCanvas();
  const points = Array.from({ length: 600 }, (_, index) => [index / 600, (index % 7) / 7]);
  ink.drawStroke(canvas.ctx, region("bounded", 1, points), 100, 100, 1, { regionNumber: 1 });
  assert.equal(canvas.ctx.calls.filter(([name]) => name === "lineTo").length, 511);
  assert.doesNotMatch(CORE, /REGION_MAX_PER_SURFACE|removeOldestRegion/);
});

test("PDF and EPUB expose the same region tool, double-tap policy and native host contract", () => {
  assert.match(PDF_TEMPLATE, /data-tool="region"/);
  assert.match(EPUB_TEMPLATE, /data-itool="region"/);
  for (const source of [PDF, EPUB]) {
    assert.match(source, /rc-ink-double-tap-action/);
    assert.match(source, /value === 'selection' \|\| value === 'none'/);
    assert.match(source, /action === 'none'/);
    assert.match(source, /tool === 'region' \? 'pen' : 'region'/);
    assert.match(source, /isRegion \? '#0a84ff'/);
    assert.match(source, /s\.t === 'region' && s\.p\.length < 3/);
    assert.match(source, /REGION_MAX_POINTS \|\| 512/);
    assert.doesNotMatch(source, /REGION_MAX_PER_SURFACE|removeOldestRegion/);
    assert.match(source, /createRegion: function \(input\)/);
    assert.match(source, /String\(input\.regionId \|\| ''\)/);
    assert.match(source, /Number\(input\.createdAtEpochMs\)/);
    assert.match(source, /_inkScheduleSave\(/);
    assert.match(source, /postMessage\(\{ type: 'tool', tool: t === 'region' \? 'selection' : t \}\)/);
  }
  assert.match(SWIFT, /case \.selection: webTool = "region"/);
  assert.match(SWIFT, /payload\["regionId"\] = regionId/);
  assert.match(SWIFT, /payload\["createdAtEpochMs"\] = createdAtEpochMs/);
  assert.match(SWIFT, /tool === "region" && value === "selection"/);
});

test("true-book ink toolbar follows Pencil hover or landing with viewport clamping", () => {
  assert.match(CORE, /function positionToolbarAbove\(toolbar, anchor\)/);
  assert.match(CORE, /anchor\.y - rect\.height - gap/);
  assert.match(CORE, /Math\.min\(maxLeft/);
  assert.match(CORE, /Math\.min\(maxTop/);
  for (const source of [PDF, EPUB]) {
    assert.match(source, /paletteAnchor/);
    assert.match(source, /pointerType === 'pen'/);
    assert.match(source, /_inkRememberPaletteAnchor\(e\.clientX, e\.clientY\)/);
    assert.match(source, /positionToolbarAbove/);
    assert.match(source, /requestAnimationFrame\(_inkPlaceToolbar\)/);
  }
});
