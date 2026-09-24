import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const voice = readFileSync(new URL('../../_server_deploy/static/pdf/rc-voicecall.js', import.meta.url), 'utf8');
const runtime = readFileSync(new URL('../../_server_deploy/static/pdf/native-local-runtime.js', import.meta.url), 'utf8');
const adapter = runtime.slice(runtime.indexOf('  root.__bwNativeFavorites ='), runtime.indexOf('  function nativePhrasesFetch('));
const observer = voice.slice(voice.indexOf('  var _dock ='), voice.indexOf('  var _FAV_CARDS_PAYLOAD_VERSION'));
const save = voice.slice(voice.indexOf('  function _favSave('), voice.indexOf('  function _favMeta('));
function setup(reply) {
  const calls = [], events = [], notices = [];
  const root = {dispatchEvent: e => events.push(e.type), webkit: {messageHandlers: {bwNativeDataStore: {postMessage: async request => {
    calls.push(request); return reply(request);
  }}}}};
  const context = vm.createContext({root, window: root, Event: class {constructor(type) {this.type = type;}},
    bootPromise: Promise.resolve(), _toast: x => notices.push(x),
    fetch: () => {throw new Error('legacy network executed');}, document: new Proxy({}, {get() {throw new Error('hidden DOM accessed');}})});
  vm.runInContext(adapter + observer + save, context);
  return {root, calls, events, notices, context};
}
test('native favorite adapter sends full card data once and does not render a hidden copy', async () => {
  const record = {id: 'g_1', cid: 'g_1', gid: 'g_1', payload: {version: 1, kind: 'cards', cards: [{front: '表', back: '裏', nodeIds: ['node-1'], anki: {noteId: 42}}]}};
  const env = setup(() => ({ok: true, id: 'g_1', cards: [record], revision: 2, context: '10'}));
  env.context.record = record;
  assert.equal(await vm.runInContext('_favSave(record)', env.context), 'g_1');
  assert.equal(env.calls.length, 1);
  assert.deepEqual(env.calls[0].value.card, record);
  assert.equal(vm.runInContext('_dock.list[0].payload.cards[0].anki.noteId', env.context), 42);
  assert.deepEqual(env.events, ['bw:native-favorites-changed']);
});
test('unknown native write does not fall back or fabricate success', async () => {
  const env = setup(() => {throw new Error('unknown write result');});
  assert.equal(await vm.runInContext('_favSave({id:"a"})', env.context), '');
  assert.equal(env.calls.length, 1);
  assert.deepEqual(env.notices, ['unknown write result']);
  assert.equal(vm.runInContext('_dock.loaded', env.context), false);
});
test('out-of-order snapshots cannot replace newer collection state', () => {
  const env = setup(() => ({}));
  const accept = env.root.__bwReaderAcceptNativeFavorites;
  assert.equal(accept({context: '10', revision: 2, cards: [{id: 'new'}]}), true);
  assert.equal(accept({context: '10', revision: 1, cards: []}), false);
  assert.equal(accept({context: '9', revision: 100, cards: []}), false);
  assert.equal(vm.runInContext('_dock.list[0].id', env.context), 'new');
  assert.equal(accept({context: '11', revision: 0, cards: []}), true);
});
