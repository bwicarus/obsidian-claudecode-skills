import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = name => readFileSync(new URL('../../_server_deploy/static/pdf/' + name, import.meta.url), 'utf8');
const settle = () => new Promise(resolve => setImmediate(resolve));
const response = (ok = true) => ({ ok, json: async () => ({ ok }) });

function runtime(kind = 'pdf') {
  const calls = [], events = [], timers = new Map();
  const surface = {
    dataset: { pageNum: '1', loaded: '1', idx: '0' }, isConnected: true,
    classList: { contains: () => false }, __inkCanvas: {}, __inkStrokes: [],
    closest: () => null, querySelectorAll: () => [],
  };
  const context = vm.createContext({
    TextEncoder, console,
    fetch: (url, options) => new Promise(resolve => calls.push({ url, options, resolve })),
    setTimeout: (fn) => { const id = timers.size + 1; timers.set(id, fn); return id; },
    clearTimeout: id => timers.delete(id),
    CustomEvent: function (type, options) { this.type = type; this.detail = options.detail; },
    MutationObserver: function () { this.observe = () => {}; },
    document: { documentElement: {}, body: { classList: { contains: () => false } },
      querySelectorAll: () => [surface], addEventListener: () => {} },
    _ink: { byPage: {}, saveTimers: {} }, _epInk: { data: {}, saveTimers: {} },
    FILE_REL: 'book.pdf', FREL: 'book.epub', currentPage: 1,
    _inkStrokesOf: el => el.__inkStrokes,
    _inkPushUndo: () => {}, _inkRedraw: () => {}, _inkEnsure: () => {},
    _inkIdxOf: () => 0, _voicePageOfInkEl: () => 1,
    _inkFileOf: () => 'book.epub', _inkShotSync: () => {},
  });
  context.window = context;
  context.crypto = { randomUUID: () => 'test-document' };
  context.__BW_NATIVE_PENCILKIT_INK__ = true;
  context.addEventListener = () => {};
  context.dispatchEvent = event => events.push(event);
  vm.runInContext(source('rc-ink.js'), context);
  const hostSource = source(kind === 'pdf' ? 'pdf-tail.js' : 'epub-html.js');
  const start = hostSource.indexOf('(function installNativeInkHost() {');
  const end = hostSource.indexOf('})();', start) + 5;
  vm.runInContext(hostSource.slice(start, end), context);
  const saveStart = hostSource.indexOf('function _inkScheduleSave(');
  const saveEnd = kind === 'pdf' ? hostSource.indexOf('function _inkFlushBeacon()', saveStart)
    : hostSource.indexOf('// 存笔迹时把', saveStart);
  vm.runInContext(hostSource.slice(saveStart, saveEnd), context);
  const input = { opId: 'stroke-1', documentToken: 'test-document', segments: [
    { surfaceId: kind === 'pdf' ? 'page:1' : 'section:0', points: [[0.2, 0.3], [0.4, 0.5]] },
  ] };
  return { context, calls, events, surface, input };
}

test('page writes freeze snapshots, serialize the same destination, and allow other pages', async () => {
  const { context, calls } = runtime();
  const ink = context.RCInk;
  const strokes = [{ p: [[0.1, 0.2]] }];
  const first = ink.persistPage('/pdf/api/ink', { file: 'b', page: 1, strokes });
  strokes[0].p[0][0] = 0.9;
  const second = ink.persistPage('/pdf/api/ink', { file: 'b', page: 1, strokes: [] });
  const other = ink.persistPage('/pdf/api/ink', { file: 'b', page: 2, strokes: [] });
  await settle();
  assert.equal(calls.length, 2);
  assert.equal(JSON.parse(calls[0].options.body).strokes[0].p[0][0], 0.1);
  calls[0].resolve(response()); calls[1].resolve(response());
  assert.equal((await first).current, false);
  await other; await settle();
  assert.equal(calls.length, 3);
  assert.deepEqual(JSON.parse(calls[2].options.body).strokes, []);
  calls[2].resolve(response());
  assert.equal((await second).current, true);
});

for (const kind of ['pdf', 'epub']) {
  test(`${kind}: native completion waits for persistence; retry does not duplicate ink`, async () => {
    const { context, calls, events, surface, input } = runtime(kind);
    const host = context.__bwNativeInkHost;
    assert.equal(host.commit(input).ok, true);
    assert.equal(surface.__inkStrokes.length, 1);
    assert.equal(events.length, 0, 'applying in memory must not release pending ink');
    let completed = false;
    const failed = host.persist(input).then(value => { completed = true; return value; });
    await settle();
    assert.equal(completed, false);
    assert.equal(calls.length, 1);
    calls[0].resolve(response(false));
    assert.equal((await failed).ok, false);
    assert.equal(events.length, 0);
    assert.equal(host.commit(input).duplicate, true);
    const retained = surface.__inkStrokes;
    surface.__inkStrokes = null; // A scrolled-off page may recycle its canvas before retry.
    const retry = host.persist(input);
    await settle();
    assert.equal(calls.length, 2);
    assert.equal(JSON.parse(calls[1].options.body).strokes.length, 1);
    assert.equal(retained.length, 1);
    calls[1].resolve(response());
    assert.equal((await retry).persisted, true);
    assert.equal(events.length, 1);
    assert.equal(events[0].detail.opId, input.opId);
    assert.equal((await host.persist(input)).duplicate, true);
    assert.equal(calls.length, 2);
    assert.equal((await host.persist({ ...input, documentToken: 'previous-book' })).ok, false);
  });
}

test('a rejected JSON receipt does not count as saved, and failure does not poison the queue', async () => {
  const { context, calls } = runtime();
  const payload = { file: 'b', page: 1, strokes: [] };
  const first = context.RCInk.persistPage('/pdf/api/ink', payload);
  const next = context.RCInk.persistPage('/pdf/api/ink', payload);
  await settle();
  calls[0].resolve({ ok: true, json: async () => ({ ok: false }) });
  assert.equal((await first).ok, false);
  await settle(); calls[1].resolve(response());
  assert.equal((await next).persisted, true);
});

test('inserted pages keep their real-page owner and do not acknowledge a temporary page', async () => {
  const { context, calls, surface } = runtime();
  context.UP_FILE = 'book.pdf';
  context._upIsTempId = value => value === 'temporary';
  const src = source('pdf-uishared.js');
  const start = src.indexOf('window._upInkPersist = function (el, retainedStrokes) {');
  const end = src.indexOf('\n  };', start) + 5;
  vm.runInContext(src.slice(start, end), context);
  surface.__upRec = { id: 'temporary', page: 4 };
  assert.equal((await context._inkScheduleSave(surface, 1, true)).ok, false);
  assert.equal(calls.length, 0);
  surface.__upRec.id = 'u_1234';
  const saved = context._inkScheduleSave(surface, 1, true);
  await settle();
  assert.equal(JSON.parse(calls[0].options.body).page, 4);
  assert.equal(Object.keys(context._ink.byPage).length, 0);
  calls[0].resolve(response());
  assert.equal((await saved).persisted, true);
});
