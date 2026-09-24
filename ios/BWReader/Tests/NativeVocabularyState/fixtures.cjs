const fs = require('node:fs');
const V = require('../../../../_server_deploy/static/reader-runtime/vocabulary-state.js');
const inputs = [
  { key: ' ＷＯＲＤ\t分かる ' }, { key: 'İ I Σ ΟΣ' }, { key: '\ufeffTest\ufeff' },
  { lemma: '於く', word: 'おける', forms: ['置ける', '於く', 'おける'], language: 'ja', enabled: true },
  { kind: 'phrase', property: 'favorite', text: 'take\n off', enabled: false },
  { kind: 'word', property: 'lookup', key: 'する', aliases: ['します', 'した'], language: 'ja' },
  { key: 'a', aliases: ['𐀀', 'あ', 'z', 'é', 'e\u0301', 'Ａ'] },
  { key: 'a', enabled: 1 }, { key: 123 }, { key: '你好', language: 'en' },
  {}, { key: '\0bad' }, { key: '\u0085' }, { key: 'a'.repeat(241) },
  { key: 'a', language: 'xx' }, { key: 'a', property: 'favorite' },
  { key: 'a', id: 'wrong' }, { key: 'a', aliases: Array.from({length: 33}, (_, i) => 'z' + i) }
];
const cases = inputs.map(input => {
  try { return { input, result: V.normalizeRecord(input) }; }
  catch (_) { return { input, error: true }; }
});
const sequences = [];
for (const input of [
  { lemma: 'be', word: 'was', language: 'en' },
  { lemma: 'be', word: 'were', language: 'en' },
  { lemma: 'be', word: 'being', language: 'en' },
  { lemma: 'be', word: 'been', language: 'en' },
]) {
  const enabled = sequences.length !== 2;
  const result = V.setMastered(input, enabled).record;
  sequences.push({ input, enabled, result });
}
fs.writeFileSync(process.argv[2], JSON.stringify({ cases, sequences }));
