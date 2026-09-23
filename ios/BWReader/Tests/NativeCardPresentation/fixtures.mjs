import fs from 'node:fs';
import vm from 'node:vm';
const source = fs.readFileSync(new URL('../../../../_server_deploy/static/pdf/rc-flashcard.js', import.meta.url), 'utf8');
const RC = { computerVoice: { addLocalAnkiCard() {} } };
const window = { RC, BWReaderRuntime: { ankiMobileExport: { available: () => true } } };
vm.runInNewContext(source, { window, document: {}, console, setTimeout, clearTimeout });
const cases = [];
const states = ['draft','learn','done','preview'];
const variants = [{}, { _showBack: true }, { _addPending: true }, { _addPending: true, _addQueued: true },
  { _ratingPending: true, _showBack: true }, { _syncPending: true, _showBack: true }, { _removePending: true },
  ...['not-exported','external','export-pending','export-unknown','missing','multiple'].map(reason => ({ _showBack: true, _ratingUnavailable: true, _ratingUnavailableReason: reason, _pcExportStatus: 'failed' }))];
for (const _st of states) for (const variant of variants) for (const readonly of [true, false]) {
  const container = { __fc: { gid: 'card_aabb', idx: 0, readonly, controlledReview: true, opts: {},
    cards: [{ type: 'cloze', cloze: '前{{c1::日本語::hint}}\n{{c2::hello}}', _st, _next: { interval: 0.02 }, _revealMode: 'replace', ...variant }] } };
  cases.push({ input: RC.flashcard.presentationInput(container, 0), expected: RC.flashcard.interactionState(container, 0) });
}
for (const interval of [null, 0, 1, 1.5, 100]) {
  const container = { __fc: { gid: 'card_aabb', readonly: false, opts: { projectFaceHtml: (card, side) => side === 'front' ? '<ruby>字<rt>じ</rt></ruby>' : '<p>字</p>' },
    cards: [{ type: 'basic', front: 'front', back: 'back', _st: 'done', _next: { interval } }] } };
  cases.push({ input: RC.flashcard.presentationInput(container, 0), expected: RC.flashcard.interactionState(container, 0) });
}
fs.writeFileSync(process.argv[2], JSON.stringify(cases));
