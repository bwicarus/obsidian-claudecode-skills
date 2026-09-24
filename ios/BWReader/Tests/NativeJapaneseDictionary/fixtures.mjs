import { readFileSync, writeFileSync } from 'node:fs';
import vm from 'node:vm';
const root = new URL('../../../../', import.meta.url);
const dictionaryRoot = new URL('ios/BWReader/DictionaryData/', root);
const sandbox = { TextEncoder, console, __BW_NATIVE_LOCAL_BASE_PATH__: '/r/test', RC: {},
  async fetch(url) {
    const path = url.replace('/r/test/native-api/offline-dictionary/', '');
    const value = JSON.parse(readFileSync(new URL(path, dictionaryRoot), 'utf8'));
    return { ok: true, json: async () => value };
  }
};
sandbox.window = sandbox;
vm.runInNewContext(readFileSync(new URL('_server_deploy/static/pdf/rc-offline-dictionary.js', root), 'utf8'), sandbox);
const ref = sandbox.RC.offlineDictionary;
const terms = ['取り寄せ', '幼児', '幼な子', 'う', '出している', '読んでいた', '食べておく', '行ってきた', '見たい',
  '出したかった', '高かった', '書かない', '読みます', '来て', 'ガッツ', 'スーパー', '漢字', '日本語', '肝炎',
  '知らない造語ZZZ', '  ', 'ハ\u309aン', '学校', 'さようなら', 'さっき', '終わった', '泳いだ', '手法', 'ネズミ', '詰め合わせ'];
const lookups = [];
for (const term of terms) {
  for (const legacy of [false, true]) lookups.push({ term, legacy, expected: await (legacy ? ref.lookupJapaneseLegacy(term) : ref.lookupJapanese(term)) });
}
const candidates = [];
const stems = ['出し', '読み', '書き', '食べ', '行っ', '来', 'き', 'し', 'あ', 'カタカナ', '😀'];
const endings = ['ている', 'ていません', 'てしまった', 'ておく', 'てある', 'てみた', 'てくる', 'てもらう', 'てほしい',
  'たい', 'たかった', 'たくない', 'たくて', 'せ', 'した', 'かった', 'くない', 'くて', 'った', 'んだ', 'いた', 'いだ', 'て', 'た', 'ない', 'ます'];
for (const term of [...terms, ...stems.flatMap(stem => endings.map(end => stem + end))])
  candidates.push({ term, expected: ref._candidateForms(term), shard: ref._shardKey(term), mora: ref._moraCount(term) });
writeFileSync(process.argv[2], JSON.stringify({ lookups, candidates }));
console.log(`Native dictionary reference: ${lookups.length} real-data lookups, ${candidates.length} forms`);
