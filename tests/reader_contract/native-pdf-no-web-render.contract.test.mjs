import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';

const root = new URL('../../', import.meta.url);
const read = name => readFileSync(new URL('_server_deploy/static/pdf/reader.src/' + name, root), 'utf8');
const render = read('04-render.js');
const highlight = read('17-highlight.js');
const fn = (source, start, end) => {
  const first = source.indexOf(start);
  assert.ok(first >= 0, `missing function ${start}`);
  const last = source.indexOf(end, first);
  return source.slice(first, last < 0 ? source.length : last);
};

test('App boot and native ownership never create a hidden raster page or prefetch neighbours', async () => {
  for (const native of [false, true]) {
    let pages = 0, images = 0, fetches = 0;
    const context = vm.createContext({
      _NATIVE_LOCAL_PDF: native, _imgMode: true, scale: 1,
      window: {RC: {readerNavigation: {nativeViewport: native ? null : {}}}, __imgMeta: {page_count: 100}},
      pdfDoc: {getPage: async () => { pages++; return {getViewport: () => ({})}; }},
      document: {createElement() { images++; throw Error('hidden page'); }},
      fetch() { fetches++; throw Error('prefetch'); },
    });
    vm.runInContext(fn(render, 'const _prefetched', 'window._prefetchAround'), context);
    vm.runInContext(fn(render, 'async function _renderPageImg', '// 拿到「模糊近似图」'), context);
    vm.runInContext(fn(render, 'async function _renderPageInto', '\nfunction ',), context);
    // _renderPageInto extends to end of this file if there is no following declaration.
    if (typeof context._renderPageInto !== 'function') vm.runInContext(render.slice(render.indexOf('async function _renderPageInto')), context);
    await context._renderPageInto(4, {dataset: {loaded: '0'}});
    await context._renderPageImg(4, {}, {});
    context._prefetchAround(4);
    assert.deepEqual([pages, images, fetches], [0, 0, 0]);
  }
});

test('native source lookup consumes character data without reading DOM, navigating or rasterizing', async () => {
  const requests = [];
  const context = vm.createContext({
    _NATIVE_LOCAL_PDF: true, pdfDoc: {numPages: 10},
    document: {querySelector() { throw Error('DOM is not a data source'); }},
    window: {webkit: {messageHandlers: {bwNativeReaderGeometry: {postMessage: async value => {
      requests.push(value);
      return {ok: true, chars: [{c: '字'}], pageWidth: 600, pageHeight: 800, revision: 'r1'};
    }}}}},
    _mapCharBoxes: chars => chars,
  });
  vm.runInContext(fn(highlight, 'async function _pdfExactTextPage', 'async function _pdfWaitForHighlightVisible'), context);
  const value = await context._pdfExactTextPage(7);
  assert.equal(value.__nativeSource, true);
  assert.equal(value.__charBoxes[0].c, '字');
  assert.equal(value.__pageTextRevision, 'r1');
  assert.equal(value.dataset.pageNum, '7');
  assert.equal(JSON.stringify(requests), '[{"action":"characters","page":7}]');
  await assert.rejects(context._pdfExactTextPage(11), /PAGE_INVALID/);
});

test('missing native characters fail explicitly, without reviving hidden rendering', async () => {
  const context = vm.createContext({_NATIVE_LOCAL_PDF: true, pdfDoc: {numPages: 2}, window: {}});
  vm.runInContext(fn(highlight, 'async function _pdfExactTextPage', 'async function _pdfWaitForHighlightVisible'), context);
  await assert.rejects(context._pdfExactTextPage(1), /GEOMETRY_UNAVAILABLE/);
});

test('continuous mode does not even allocate page placeholders in App', async () => {
  const context = vm.createContext({_NATIVE_LOCAL_PDF: true});
  vm.runInContext(fn(read('07-continuous.js'), 'async function setupContinuousMode', '\nfunction '), context);
  await context.setupContinuousMode();
});
