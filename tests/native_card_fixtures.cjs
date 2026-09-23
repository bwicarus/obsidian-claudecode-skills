// Execute the browser card owner against an isolated memory store. Native
// tests consume its results and committed envelopes with deterministic clocks.
const fs = require('node:fs');
const D = require('../_server_deploy/static/reader-runtime/data-store.js');
const C = require('../_server_deploy/static/reader-runtime/card-repository.js');
const cases = [];
function normalize(operation, inputs) {
  for (const input of inputs) {
    try { cases.push({ operation, input, result: C[operation](input) }); }
    catch (e) { cases.push({ operation, input, error: e.code }); }
  }
}
const basic = { type: 'basic', front: '前', back: '后' };
const cloze = { type: 'cloze', text: 'これは{{c1::本::名詞}}です。' };
const source = { kind: 'selection', documentId: 'localbook:book', draftId: 'original', quote: '段落', kjTrack: 'jp-word' };
normalize('normalizeCard', [basic, cloze, { ...basic, tags: ['b', 'a', 'b'] }, { ...basic, front: '  hi\r\nthere\r! ' },
  { ...basic, tags: ['空 格'] }, { ...basic, extra: 1 }, { ...basic, front: '' }, { ...basic, back: 'a'.repeat(65537) },
  { ...basic, front: '\0' }, { ...cloze, front: 'x' }, { type: 'cloze', cloze: '{{c0::none}}' },
  { type: 'cloze', cloze: 'one', text: 'two' }, { type: 'basic', front: 12, back: true }, null]);
normalize('normalizeCards', [[], [basic, cloze], [basic, null], Array(257).fill(basic)]);
normalize('normalizeCard', [{ ...basic, nodeIds: ['kj:0123456789'] }, { ...basic, nodeIds: [] },
  { ...basic, nodeIds: ['invalid'] }, { ...basic, nodeIds: ['kj:0123456789', 'kj:0123456789'] }]);
normalize('normalizeSource', [source, { kind: 'x' }, { kind: 'x', url: 'https://example.org', anchor: { text: '字', index: 3 } },
  { ...source, bad: 'field' }, { ...source, location: [] }, { ...source, context: '\0' }, null]);
function step(operation, ...args) { return { operation, args }; }
const id = 'card_aabbccdd';
const scenarios = [
  [step('registerDraft', { gid: id, cards: [{ ...basic, nodeIds: ['kj:0123456789'] }, { ...basic, nodeIds: ['kj:9876543210'] }], source: { ...source, kjTrack: '' } }),
   step('saveConfirmedCard', { gid: id, cardIndex: 1 }), step('load', id)],
  [step('registerDraft', { gid: id, cards: [basic, cloze], source }),
   step('registerDraft', { gid: id, cards: [basic, cloze], source: { ...source } }, { requireDraftIdForReplay: true }),
   step('saveConfirmedCard', { gid: id, cardIndex: 1 }), step('saveConfirmedCard', { gid: id, cardIndex: 1 }),
   step('replaceContent', id, [{ ...basic, back: 'changed' }, cloze]),
   step('patchState', id, 1, { review: { status: 'review', reps: 4, dueAt: 9000, ease: 2.3 }, flags: { favorite: true } }),
   step('recordAnkiReceipt', id, 1, 'desktop', { status: 'unknown', mutationId: 'anki-1' }),
   step('recordAnkiReceipt', id, 1, 'desktop', { status: 'succeeded', noteIds: [123], cardIds: ['456'] }),
   step('removeDraftCard', id, 1), step('removeDraftCard', id, 0),
   step('saveConfirmedCard', { gid: id, cardIndex: 0 }), step('removeCard', id, 1),
   step('patchState', id, 1, { flags: { archived: true } }),
   step('recordAnkiReceipt', id, 1, 'desktop', { status: 'failed', error: 'offline' }),
   step('load', id), step('snapshot'), step('tombstone', id), step('load', id),
   step('load', id, { includeDeleted: true }), step('snapshot', { includeDeleted: true }),
   step('registerDraft', { gid: id, cards: [basic, cloze], source })],
  [step('registerDraft', { gid: id, cards: [basic], source }),
   step('registerDraft', { gid: id, cards: [{ ...basic, back: 'fork' }], source }),
   step('registerDraft', { gid: id, cards: [basic], source: { ...source, draftId: 'fork' } }),
   step('replaceContent', id, [basic, cloze]),
   step('saveConfirmedCard', { gid: id, card: { ...basic, back: 'new' } }, { ifEntityRev: 999 }), step('load', id),
   step('replaceEntity', id, { source: { ...source, context: 'nearby passage' } }, { ifEntityRev: 1 }),
   step('patchState', id, 0, { exactState: { _st: 'draft', _opaque: { preserve: 1 } } }),
   step('patchState', id, 0, { phase: 'confirmed' }), step('patchState', id, 0, { review: { status: 'not-real' } }),
   step('patchState', id, 0, { flags: { favorite: 1 } }), step('saveConfirmedCard', { gid: id, cardIndex: 0 }),
   step('removeCard', id, 0, { ifStateRev: 2 }), step('tombstone', id, { ifStateRev: 999 }), step('load', id)],
  [step('saveConfirmedCard', { gid: id, card: basic, source }),
   step('recordAnkiReceipt', id, 0, '../bad', {}), step('load', 'card_ffff'),
   step('replaceContent', 'card_ffff', [basic]), step('tombstone', 'card_ffff')],
  [step('importLegacyBatch', [{ gid: id, kind: 'cards', cards: [basic, cloze], source_ref: 'old', ts: 1234,
      states: { 0: { _st: 'done', _nid: 23, card_id: 34 } } }]),
   step('importLegacyBatch', [{ gid: id, cards: [basic, cloze], source_ref: 'old', states: {} }]),
   step('importLegacyBatch', [{ gid: id, cards: [basic, cloze], source_ref: 'old', states: { 1: { _st: 'draft' } } }]),
   step('importLegacyBatch', [{ gid: 'card_ffff', cards: [basic] }, { gid: id, cards: [{ ...basic, front: 'fork' }] }]),
   step('load', 'card_ffff'),
   step('importLegacyBatch', [{ gid: id, cards: [{ ...basic, front: 'old' }] }, { gid: 'card_ffff', batch: { cards: [basic] } }], { missingOnly: true }),
   step('tombstone', id),
   step('importLegacyBatch', [{ gid: id, data: [basic] }], { missingOnly: true }),
   step('snapshot', { includeDeleted: true })]
];
(async () => {
  const sequences = [];
  for (const steps of scenarios) {
    let at = 1000;
    const store = D.createDataStore({ deviceId: 'fixture', clock: () => at, causalCollections: [C.ENTITY_COLLECTION, C.STATE_COLLECTION] });
    const repository = C.createCardRepository({ store, clock: () => at });
    const sequence = [];
    for (const item of steps) {
      at += 100;
      const record = { operation: item.operation, arguments: item.args, mutationId: 'fixture-' + at, at };
      let index = 2;
      if (['registerDraft', 'saveConfirmedCard', 'tombstone', 'importLegacyBatch'].includes(item.operation)) index = 1;
      if (item.operation === 'patchState') index = 3;
      if (item.operation === 'recordAnkiReceipt') index = 4;
      if (!['load', 'snapshot'].includes(item.operation)) {
        while (item.args.length <= index) item.args.push(null);
        item.args[index] = { ...(item.args[index] || {}), mutationId: record.mutationId };
      }
      try { record.result = await repository[item.operation](...item.args); }
      catch (e) { record.error = e.code; }
      record.records = [...await store.list(C.ENTITY_COLLECTION, { includeDeleted: true }), ...await store.list(C.STATE_COLLECTION, { includeDeleted: true })];
      sequence.push(record);
    }
    sequences.push(sequence);
  }
  fs.writeFileSync(process.argv[2], JSON.stringify({ cases, sequences }));
})();
