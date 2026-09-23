import { writeFileSync } from 'node:fs';
import reference from '../../../../_server_deploy/static/reader-runtime/user-state-merge.js';

const cases = [];
function add(domain, base, mine, theirs) {
  const expected = reference.mergeDomain(domain, base, mine, theirs);
  cases.push({ domain, base, mine, theirs, expected, empty: reference.domainEmpty(domain, expected.value) });
}
const records = [[], [{ id: 'a', rev: 1, text: '原' }], [{ id: 'a', rev: 2, text: '改' }],
  [{ id: 'a', deleted: true, time: 8 }], [{ id: 'b', time: 5 }],
  [{ id: 'a', rev: null }, { id: 'a', rev: '3' }, { placementId: 'p', ts: 2 }],
  [{ entityId: 'e', updatedAt: '4' }, { id: 'a', rev: false, time: 8 }]];
for (let b = 0; b < records.length; b++) for (let m = 0; m < records.length; m++) for (let t = 0; t < records.length; t++) {
  add('notes', records[b], records[m], records[t]);
  add('highlights', { pdf: records[b], epub: [] }, { pdf: records[m], epub: records[b] }, { pdf: records[t], epub: records[m] });
}
const inks = [{}, { '1': [{ pts: [[0, 0]], t: 'pen' }] },
  { '1': [{ pts: [[0, 0]], t: 'pen' }, { t: 'pen', pts: [[1, 1]] }] },
  { '1': [{ t: 'pen', pts: [[0, 0]] }], '2': [{ t: 'erase' }] }, { '1': [] }];
for (const b of inks) for (const m of inks) for (const t of inks) {
  add('ink', { pdf: b, epub: {} }, { pdf: m, epub: b }, { pdf: t, epub: t });
}
for (const domain of ['reading-position', 'user-pages', 'card-placements', 'entity-references', 'closed-regions', 'unknown']) {
  for (const [b, m, t] of [[null, null, null], [null, [], []], [[], [], records[1]],
    [{ page:1 }, { page:2 }, { page:3 }], [{ ts:1 }, { ts:2 }, { ts:3 }],
    [null, { ts:[] }, { ts:'0x10' }], [null, { rev:'2' }, { rev:3 }],
    [{}, { pdf: {}, epub: {} }, { pdf: { '1': [{ t:'region', id:'a' }] }, epub: {} }]]) add(domain,b,m,t);
}
for (const revision of [null, false, true, [], [null], [false], [[true]], [1], ['2'], {}, '0b11', '0o10', '0x20', '  ', 'abc', '1e2']) {
  add('notes', [], [{ id:'a', rev:revision, text:'local' }], [{ id:'a', rev:2, text:'remote' }]);
  add('reading-position', null, { page:1, rev:revision }, { page:2, rev:2 });
}
writeFileSync(process.argv[2], JSON.stringify(cases));
console.log(`${cases.length} three-way merge reference cases`);
