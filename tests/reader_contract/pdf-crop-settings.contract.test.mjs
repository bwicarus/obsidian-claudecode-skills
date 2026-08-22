import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const LOADER = read("_server_deploy/static/pdf/reader.src/03-loader.js");
const SETTINGS = read("_server_deploy/static/pdf/reader.src/21-misc-ai.js");

function functionSource(source, name) {
  const marker = `async function ${name}(`;
  const start = source.indexOf(marker);
  assert.ok(start >= 0, `${name} exists`);
  const brace = source.indexOf("{", start);
  let depth = 0;
  for (let index = brace; index < source.length; index += 1) {
    if (source[index] === "{") depth += 1;
    if (source[index] === "}") {
      depth -= 1;
      if (depth === 0) return source.slice(start, index + 1);
    }
  }
  throw new Error(`${name} body is incomplete`);
}

function cropHarness(fetchImpl, refitImpl = async () => {}) {
  const events = { stored: [], refit: 0, remembered: 0, button: 0, order: [] };
  const context = vm.createContext({
    fetch: fetchImpl,
    FILE_REL: "localbook:test",
    localStorage: {
      setItem(key, value) { events.stored.push([key, value]); },
    },
    _cropKey: () => "pdf-crop-on:localbook:test",
    _updateCropBtn: () => { events.button += 1; },
    _refitToWidth: async () => {
      events.refit += 1;
      events.order.push("refit-start");
      await refitImpl();
      events.order.push("refit-end");
    },
    window: {
      _rememberOrientLayout: () => {
        events.remembered += 1;
        events.order.push("remember");
      },
    },
  });
  vm.runInContext(`
    let _crop = {l: 0, r: 0, t: 0, b: 0};
    let _cropOn = false;
    ${functionSource(LOADER, "saveCropSettings")}
    this.save = saveCropSettings;
    this.state = () => ({ crop: {..._crop}, cropOn: _cropOn });
  `, context);
  return { context, events };
}

test("applying non-zero crop while off persists, enables, refits, and remembers layout", async () => {
  const { context, events } = cropHarness(async () => ({
    ok: true,
    status: 200,
    json: async () => ({ ok: true, crop: { l: 3, r: 3, t: 3, b: 3 } }),
  }));
  await context.save({ l: 3, r: 3, t: 3, b: 3 }, true);
  assert.deepEqual(JSON.parse(JSON.stringify(context.state())), {
    crop: { l: 3, r: 3, t: 3, b: 3 },
    cropOn: true,
  });
  assert.deepEqual(events.stored, [["pdf-crop-on:localbook:test", "1"]]);
  assert.equal(events.button, 1);
  assert.equal(events.refit, 1);
  assert.equal(events.remembered, 1);
  assert.deepEqual(events.order, ["refit-start", "refit-end", "remember"]);
});

test("orientation state is remembered only after an asynchronous crop refit finishes", async () => {
  let releaseRefit;
  let reportRefitStarted;
  const refitStarted = new Promise((resolve) => { reportRefitStarted = resolve; });
  const refitPending = new Promise((resolve) => { releaseRefit = resolve; });
  const { context, events } = cropHarness(
    async () => ({
      ok: true,
      status: 200,
      json: async () => ({ ok: true }),
    }),
    async () => {
      reportRefitStarted();
      await refitPending;
    },
  );
  const saving = context.save({ l: 3, r: 3, t: 3, b: 3 }, true);
  await refitStarted;
  assert.deepEqual(events.order, ["refit-start"]);
  assert.equal(events.remembered, 0);
  releaseRefit();
  await saving;
  assert.deepEqual(events.order, ["refit-start", "refit-end", "remember"]);
});

test("failed crop persistence leaves the visible state untouched", async () => {
  const { context, events } = cropHarness(async () => ({
    ok: false,
    status: 500,
    json: async () => ({ ok: false, error: "write failed" }),
  }));
  await assert.rejects(
    context.save({ l: 3, r: 3, t: 3, b: 3 }, true),
    /write failed/,
  );
  assert.deepEqual(JSON.parse(JSON.stringify(context.state())), {
    crop: { l: 0, r: 0, t: 0, b: 0 },
    cropOn: false,
  });
  assert.deepEqual(events.stored, []);
  assert.equal(events.refit, 0);
  assert.equal(events.remembered, 0);
});

test("settings closes and reports success only after the crop save resolves", () => {
  const start = SETTINGS.indexOf("window._applyCropSettings = async");
  const end = SETTINGS.indexOf("\n};", start);
  assert.ok(start >= 0 && end > start);
  const body = SETTINGS.slice(start, end);
  const save = body.indexOf("await saveCropSettings");
  const close = body.indexOf("closeSettings()", save);
  const success = body.indexOf("去边已应用", close);
  assert.ok(save >= 0 && close > save && success > close);
  assert.match(body, /catch \(error\)[\s\S]*去边保存失败/);
});

test("an unconfigured crop toggle opens settings instead of enabling a zero crop", () => {
  const start = LOADER.indexOf("window.toggleCrop = () => {");
  const end = LOADER.indexOf("\n};", start);
  assert.ok(start >= 0 && end > start);
  const body = LOADER.slice(start, end);
  const emptyGuard = body.indexOf("!(_crop.l || _crop.r || _crop.t || _crop.b)");
  const openSettings = body.indexOf("window.openSettings?.()", emptyGuard);
  const returnAfterSettings = body.indexOf("return;", openSettings);
  const toggle = body.indexOf("_cropOn = !_cropOn", returnAfterSettings);
  assert.ok(emptyGuard >= 0 && openSettings > emptyGuard);
  assert.ok(returnAfterSettings > openSettings && toggle > returnAfterSettings);
});
