const fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
const root = path.resolve(__dirname, '../../../..');
const read = p => fs.readFileSync(path.join(root, p), 'utf8');
const vocab = read('_server_deploy/static/reader-runtime/vocabulary-state.js');
const native = read('_server_deploy/static/pdf/native-local-runtime.js');
const charlayer = read('_server_deploy/static/pdf/reader.src/08-charlayer.js');
function portion(src, start, end) { return src.slice(src.indexOf(start), src.indexOf(end, src.indexOf(start))); }
const algorithms = portion(native, '  function _insideLargerToken(', '  function handleLocalState(');
const merge = portion(charlayer, 'function _mergeVocabMarks(', 'function _applyPageVocabOverlay(');
const lookup = portion(charlayer, 'function _vocabularyStateRepo(', '// 区域划分同源');
const visible = portion(charlayer, 'function _vocabMarksForDisplay(', '/// 原生正文用的');
function chars(words, options = {}) {
  let x = 0, y = 0; const out = [];
  words.forEach((word, w) => {
    for (const c of word) {
      if (c === '\n') { y += 20; x = 0; continue; }
      out.push({c, w, sp: c === ' ', x0:x, y0:y, x1:x+9.99, y1:y+14}); x += 10;
    }
  });
  return out;
}
const record = (key, property = 'lookup', kind = 'word', enabled = true, language = 'ja', aliases = []) =>
  ({key, property, kind, enabled, language, aliases});
const definitions = [
  {chars:chars(['栄', '養', '素']), records:[record('養'),record('栄養','mastered')]},
  {chars:chars(['だろ','う','か']), records:[record('だろう'),record('か')]},
  {chars:chars(['感染症']), records:[record('感染'),record('感染症')]},
  {chars:chars(['衛生','活動']), records:[record('衛生'),record('活動'),record('衛生活動','favorite','phrase')]},
  {chars:chars(['食品','衛生','活動']), records:[record('衛生'),record('活動'),record('食品衛生活動','mastered','phrase')]},
  {chars:chars(['於ける','おける']), records:[record('おく','lookup','word',true,'ja',['於ける','おける'])]},
  {chars:chars(['take',' ','off']), records:[record('take off','favorite','phrase',true,'en'),record('take','lookup','word',true,'en')]},
  {chars:chars(['な','の','は','漢']), records:[record('な'),record('の'),record('は'),record('漢')]},
  {chars:chars(['has',' HAVE',' had']), records:[record('have','lookup','word',true,'en',['has','had']),record('have','mastered','word',true,'und')]},
  {chars:chars(['日本\n語']), records:[record('日本語')]},
  {chars:chars(['😀','😀','日本語']), records:[record('😀😀'),record('日本語')]},
  {chars:chars(['New', 'York']), records:[record('newyork','lookup','phrase',true,'en')]},
  {chars:[], records:[]}
];
// Deterministic combinations exercise boundaries, cross-token matches, alias
// precedence and mastered-range suppression against the actual old functions.
let seed = 913;
const rand = n => { seed = (Math.imul(seed,1664525)+1013904223)>>>0; return seed%n; };
for (let run = 0; run < 80; run++) {
  const words = Array.from({length:6},()=>['栄','養','衛生','活動','だろ','う','food','take','off'][rand(9)]);
  const records = new Map();
  for (let n=0;n<9;n++) {
    const at=rand(words.length), key=words.slice(at,at+1+rand(3)).join('');
    const property=['lookup','favorite','mastered'][rand(3)], kind=property==='favorite'?'phrase':rand(2)?'word':'phrase';
    const row=record(key,property,kind,rand(4)!==0,/^[a-z]+$/.test(key)?'en':'ja');
    records.set([key,property,kind,row.language].join('|'),row);
  }
  definitions.push({chars:chars(words),records:[...records.values()]});
}
const cases = definitions.map(input => {
  const context = vm.createContext({console});
  vm.runInContext(vocab, context);
  const state = context.BWReaderRuntime.vocabularyState;
  input.records.forEach(r => state.importRecord(r));
  context.root = context; context.window = context;
  vm.runInContext(algorithms + merge + lookup + visible, context);
  const local = context.localVocabMarks(input.chars);
  const remote = [{word:'remote',lemma:'remote',jp:false,label_slug:'new',rects:[[600,0,660,14]]},
    ...local.map(m=>({...m,local:false,rects:m.rects.map(r=>[r[0]+1,r[1]+1,r[2]+1,r[3]+1])}))];
  const combined = context._mergeVocabMarks(local,remote);
  return {...input,records:input.records.map(r=>state.normalizeRecord(r)),local,remote,combined,visible:context._vocabMarksForDisplay(combined)};
});
fs.writeFileSync(process.argv[2],JSON.stringify({cases}));
console.log('Vocabulary overlay oracle:',cases.length,'cases');
