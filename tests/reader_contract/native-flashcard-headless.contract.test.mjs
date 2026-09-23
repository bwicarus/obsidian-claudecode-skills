import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';
import { webcrypto } from 'node:crypto';

const source = readFileSync(new URL('../../_server_deploy/static/pdf/rc-flashcard.js', import.meta.url), 'utf8');
function harness() {
  let htmlWrites = 0, renders = 0, clears = 0, destroyed = 0;
  const events = [];
  const fail = () => { throw new Error('Native card accessed the web renderer'); };
  const body = {
    isConnected: true, firstChild: {}, classList: { add() {} },
    __fcPager: { destroy() { destroyed++; } },
    replaceChildren() { this.firstChild = null; clears++; },
    querySelector: fail, querySelectorAll: fail,
    set innerHTML(_) { htmlWrites++; },
  };
  const RC = { md() { renders++; return ''; }, typeset: fail };
  const window = { RC, crypto: webcrypto, __BW_NATIVE_CONVERSATION_DATA__: true, dispatchEvent(event) { events.push(event.detail); } };
  const context = { window, document: { createElement: fail, getElementById() { return null; } },
    CustomEvent: class { constructor(type, value) { this.type = type; this.detail = value.detail; } },
    console, setTimeout, clearTimeout };
  vm.runInNewContext(source, context);
  return { RC, window, body, events, counts: () => ({ htmlWrites, renders, clears, destroyed }) };
}

test('native review mount and reveal never build HTML, fetch images, typeset or start a web pager', async () => {
  const h = harness();
  h.RC.flashcard.mountReview(h.body, [{ type: 'basic', front: '<img src="https://example.org/x.png">題', back: '答' }], { gid: 'review:1' });
  assert.deepEqual(h.counts(), { htmlWrites: 0, renders: 0, clears: 1, destroyed: 1 });
  assert.equal(h.RC.flashcard.containerOf('review:1'), h.body);
  assert.equal(h.RC.flashcard.interactionState(h.body, 0).presentation.faces.length, 1);
  await h.RC.flashcard.performInteraction(h.body, 0, 'reveal');
  assert.equal(h.RC.flashcard.interactionState(h.body, 0).presentation.faces[1].content, '答');
  assert.equal(h.counts().renders, 0);
  assert.equal(h.counts().htmlWrites, 0);
  assert.ok(h.events.some(e => e.reason === 'native-card-changed'));
  h.RC.flashcard.setNativePresentation(true);
  assert.equal(h.counts().clears, 1, 'an already empty card must not cause another DOM mutation');
});

test('headless draft success uses committed semantic state rather than existence of a hidden fc-card', async () => {
  const h = harness();
  const gid = 'card_aabb';
  const record = { id: gid, cid: gid, gid, cards: [{ type: 'basic', front: '題', back: '答' }],
    states: { 0: { phase: 'draft', review: { status: 'new' }, projections: {} } } };
  h.window.BWReaderRuntime = { cardRepository: {
    status: () => ({ available: true }), registerDraft: async () => record, load: async () => record,
  } };
  const el = { dataset: {}, classList: { add() {} }, querySelector() { assert.fail('Draft receipt must not inspect hidden markup'); } };
  h.RC.voiceCard = { renderInflow(host, spec) { spec.mount(h.body); return { el, bd: h.body }; } };
  const result = await h.RC.flashcard.presentDraft(record.cards, gid, { host: {} });
  assert.equal(result.el, el);
  assert.equal(h.RC.flashcard.interactionState(h.body, 0).editable, true);
  assert.equal(h.counts().htmlWrites, 0);
  const next = { ...record, entityRev: 1, stateRev: 2, states: { 0: { ...record.states[0], exactState: { front: '更新題' } } } };
  h.RC.flashcard.acceptNativeRecord(next, 0, 'edit');
  assert.equal(h.RC.flashcard.interactionState(h.body, 0).fields[0].value, '更新題');
  assert.equal(h.RC.flashcard.presentationInput(h.body, 0).stateRev, 2);
});
