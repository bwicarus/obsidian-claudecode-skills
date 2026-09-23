import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = name => readFileSync(new URL('../../_server_deploy/static/pdf/reader.src/' + name, import.meta.url), 'utf8');

function host() {
  const saved = [], events = [], timers = [];
  const scroll = { addEventListener: (_, callback) => { scroll.callback = callback; } };
  const context = vm.createContext({
    URL, FILE_REL: 'original/book.pdf', pdfDoc: { numPages: 20 }, currentPage: 2, scale: 1,
    readMode: 'continuous', _spreadOffset: 0, RC: {}, __BW_NATIVE_LOCAL_READER__: true,
    _crop: {l:0,r:0,t:0,b:0}, _cropOn: false, _updateCropBtn: () => {},
    location: { href: 'http://reader.test/pdf?file=original%2Fbook.pdf' },
    history: { replaceState: () => {} },
    document: { getElementById: id => id === 'main' ? scroll : null },
    CustomEvent: function (type, options) { this.type = type; this.detail = options.detail; },
    _dispPage: page => page, _pdfFromDisp: page => page,
    _saveLastPosition: value => saved.push(value),
    _getLastPosition: () => ({ page: 2, frac: 0.37 }),
    setTimeout: callback => { timers.push(callback); return timers.length; }, clearTimeout: () => {},
    _scrollSaveTimer: null,
  });
  context.window = context;
  context.dispatchEvent = event => events.push(event);
  const nav = source('05-nav.js');
  vm.runInContext(nav.slice(nav.indexOf('window.changePage ='), nav.indexOf('// 页码对齐:')), context);
  const render = source('04-render.js');
  vm.runInContext(render.slice(render.indexOf('async function renderPage('), render.indexOf('// 去边模式')), context);
  const position = source('02-position.js');
  vm.runInContext(position.slice(position.indexOf('function _attachScrollSaver()')), context);
  const layout = source('06-layout.js');
  vm.runInContext(layout.slice(0, layout.indexOf('// 容器宽度变化')), context);
  return { context, nav: context.RC.readerNavigation, saved, events, scroll, timers };
}

const position = (sequence, page) => ({ sequence, page, fraction: 0.42, scale: 1.2, visiblePages: [page] });

test('native-owned continuation reports context without a second localStorage write', () => {
  const {context, nav} = host();
  const writes = [], reports = [];
  context.localStorage = {getItem: () => '{}', setItem: (...args) => writes.push(args)};
  context.document.title = 'Book · Reader';
  context.RC.ctxSync = {report: value => reports.push(value)};
  const code = source('02-position.js');
  vm.runInContext(code.slice(0,code.indexOf('function _getLastPosition()')),context);
  nav.attachNativeViewport({file:context.FILE_REL,token:'native',persistsNatively:true,goToPage: () => {}});
  nav.acceptNativePosition('native',position(1,8));
  assert.equal(writes.length,0);
  assert.equal(reports.length,1);
  assert.equal(reports[0].pos,8);
  nav.detachNativeViewport('native');
  context._saveLastPosition({page:9});
  assert.equal(writes.length,1,'browser fallback still owns its position');
});

test('original navigation waits for native success and shares the canonical page-relative position', async () => {
  const { context, nav, saved, events } = host();
  let sequence = 0, succeeds = false;
  nav.attachNativeViewport({ file: context.FILE_REL, token: 'native-1', goToPage: async page => ({ ok: succeeds, position: position(++sequence, page) }) });
  await assert.rejects(context.goToPage(8), /未完成/);
  assert.equal(context.currentPage, 2);
  assert.equal(saved.length, 0);
  succeeds = true;
  await context.goToPage(8);
  assert.equal(context.currentPage, 8);
  assert.equal(saved[0].frac, 0.42);
  assert.equal(context.__nativeReaderViewport.page, 8);
  assert.equal(events[0].detail.visiblePages[0], 8);
  assert.throws(() => nav.acceptNativePosition('native-1', position(1, 3)), /过期/);
  assert.equal(context.currentPage, 8);
  assert.throws(() => nav.acceptNativePosition('native-1', { ...position(3, 9), fraction: NaN }), /无效/);
  assert.equal(context.currentPage, 8);
});

test('a delayed reply from a replaced native viewport cannot move the new document', async () => {
  const { context, nav, saved } = host();
  let finish;
  nav.attachNativeViewport({ file: context.FILE_REL, token: 'old', goToPage: () => new Promise(resolve => { finish = resolve; }) });
  const pending = context.goToPage(7);
  assert.equal(nav.detachNativeViewport('wrong-token'), false);
  assert.equal(nav.detachNativeViewport('old'), true);
  nav.attachNativeViewport({ file: context.FILE_REL, token: 'new', goToPage: async page => ({ ok: true, position: position(1, page) }) });
  finish({ ok: true, position: position(1, 7) });
  await assert.rejects(pending, /未完成/);
  assert.equal(saved.length, 0);
  assert.equal(context.currentPage, 2);
  assert.throws(() => nav.attachNativeViewport({ file: 'another.pdf', token: 'foreign', goToPage: () => {} }), /无效/);
  assert.equal(nav.nativeViewport.token, 'new');
});

test('old scroll callbacks cannot overwrite the native viewport, even if already scheduled', () => {
  const { context, nav, saved, scroll, timers } = host();
  context._attachScrollSaver();
  scroll.callback();
  assert.equal(timers.length, 1);
  nav.attachNativeViewport({ file: context.FILE_REL, token: 'native', goToPage: () => {} });
  timers[0]();
  scroll.callback();
  assert.equal(timers.length, 1);
  assert.equal(saved.length, 0);
});

test('a later native finger scroll wins over an in-flight navigation receipt', async () => {
  const { context, nav, saved } = host();
  let finish;
  nav.attachNativeViewport({ file: context.FILE_REL, token: 'native', goToPage: () => new Promise(resolve => { finish = resolve; }) });
  const pending = context.goToPage(7);
  nav.acceptNativePosition('native', position(2, 9));
  finish({ ok: true, position: position(1, 7) });
  await pending;
  assert.equal(context.currentPage, 9);
  assert.equal(saved.at(-1).page, 9);
});

test('initial native restoration uses the already arbitrated page and only its own fraction', () => {
  const { context, nav } = host();
  assert.equal(nav.nativeState().page, 2);
  assert.equal(nav.nativeState().fraction, 0.37);
  context.currentPage = 3;
  assert.equal(nav.nativeState().fraction, 0);
  context.__BW_NATIVE_LOCAL_READER__ = false;
  assert.throws(() => nav.nativeState(), /本机 PDF/);
});

test('AI context reports the native visible pages instead of hidden web spread arithmetic', () => {
  const { context, nav } = host();
  const code = source('05-nav.js');
  vm.runInContext(code.slice(code.indexOf('window.__voiceContext =')), context);
  nav.attachNativeViewport({ file: context.FILE_REL, token: 'native', goToPage: () => {} });
  nav.acceptNativePosition('native', { ...position(1, 4), visiblePages: [3, 4, 5] });
  assert.deepEqual(Array.from(context.__voiceContext().pages), [3, 4, 5]);
  assert.equal(context.__voiceContext().page, 4);
  nav.detachNativeViewport('native');
  assert.deepEqual(Array.from(context.__voiceContext().pages), [4]);
});

test('original spread cycle changes saved mode only after a native layout receipt', async () => {
  const { context, nav, saved } = host();
  let succeeds = false, sequence = 0;
  nav.attachNativeViewport({ file: context.FILE_REL, token: 'native', goToPage: () => {},
    perform: async (action, value) => {
      assert.equal(action, 'layout');
      return { ok: succeeds, position: { ...position(++sequence, 2), ...value } };
    }
  });
  await assert.rejects(context.toggleSpread(), /未完成/);
  assert.equal(context.readMode, 'continuous');
  assert.equal(saved.length, 0);
  succeeds = true;
  await context.toggleSpread();
  assert.equal(context.readMode, 'spread'); assert.equal(context._spreadOffset, 0);
  await context.toggleSpread();
  assert.equal(context.readMode, 'spread'); assert.equal(context._spreadOffset, 1);
  await context.toggleSpread();
  assert.equal(context.readMode, 'continuous');
  await context.toggleReadMode();
  assert.equal(context.readMode, 'single');
  assert.equal(saved.at(-1).mode, 'single');
  assert.throws(() => nav.acceptNativePosition('native', { ...position(100, 3), mode: 'unsupported' }), /无效/);
  assert.equal(context.readMode, 'single');
});

test('crop uses the original save owner, and native display failure never pretends it was enabled', async () => {
  const { context, nav } = host();
  const loader = source('03-loader.js');
  vm.runInContext(loader.slice(loader.indexOf('function _updateCropBtn()')), context);
  let stored, succeeds = false, sequence = 0, commands = 0;
  context.fetch = async (_, options) => { stored = JSON.parse(options.body); return {ok:true,json:async () => ({ok:true})}; };
  nav.attachNativeViewport({ file: context.FILE_REL, token: 'native', goToPage: () => {},
    perform: async (action, value) => {
      commands++;
      assert.equal(action, 'crop');
      return {ok:succeeds, position: {...position(++sequence, 2), cropEnabled:value.enabled, ...(value.enabled ? {crop:value.crop} : {})}};
    }
  });
  const value = {l:10,r:20,t:30,b:5};
  await assert.rejects(context.saveCropSettings(value, true), /未完成/);
  assert.deepEqual(stored, {file:context.FILE_REL,crop:value});
  assert.equal(context._cropOn, false);
  assert.deepEqual({...context._crop}, value); // The durable settings did save.
  succeeds = true;
  await context.toggleCrop();
  assert.equal(context._cropOn, true);
  await context.toggleCrop();
  assert.equal(context._cropOn, false);
  assert.deepEqual({...context._crop}, value); // Disabling keeps percentages.
  context.fetch = async () => ({ok:false,status:503,json:async () => ({error:'offline'})});
  const before = commands;
  await assert.rejects(context.saveCropSettings({l:1,r:2,t:3,b:4}, true), /offline/);
  assert.equal(commands, before);
  assert.deepEqual({...context._crop}, value);
});
