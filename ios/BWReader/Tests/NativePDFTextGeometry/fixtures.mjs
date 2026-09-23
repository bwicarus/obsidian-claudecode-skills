import {readFileSync, writeFileSync} from 'node:fs';
import vm from 'node:vm';
const root = new URL('../../../../', import.meta.url);
const read = path => readFileSync(new URL(path, root), 'utf8');
const section = (source, start, end) => {
  const a = source.indexOf(start), b = source.indexOf(end, a + start.length);
  if (a < 0 || b < 0) throw Error('selection oracle boundary changed');
  return source.slice(a, b);
};
const mapping = read('_server_deploy/static/pdf/reader.src/08-charlayer.js');
const selection = read('_server_deploy/static/pdf/reader.src/13-selection.js');
const binding = read('_server_deploy/static/pdf/reader.src/34-bindcard.js');
const context = vm.createContext({});
vm.runInContext([
  section(mapping, 'function _selectionUsesBlockFilter(', 'const _nativePageOverlayEnrichment ='),
  section(selection, 'function _selectionEndpointFilter(', '// 未声明语言的书里'),
  section(selection, 'function _findCharAt(', 'function _charBlockId('),
  section(selection, 'function _charBlockId(', 'function _selByCharRange('),
  section(binding, 'function _stripWs(', 'function _bindCategory('),
  read('ios/BWReader/NativePDFSelectionCore.js'),
].join('\n'), context);
const glyph = (c, x, y, extra = {}) => ({c, x0:x, x1:x+10, y0:y, y1:y+12, w:-1, bk:0, sp:0, ...extra});
const row = (text, x, y, extra) => Array.from(text, (c, i) => glyph(c,x+i*11,y,extra));
const page = (chars, extra = {}) => ({page_w:400, page_h:600, chars, source:'embedded',engine_revision:'embedded-pdfkit',...extra});
const pages = [
  page([...row('①感染症。',10,10,{bk:1}),...row('②鳥インフル',10,28,{bk:2}),glyph(' ',65,28,{sp:1,bk:2}),...row('エンザ。',10,47,{bk:3})]),
  page([...row('Hello',10,10,{w:1}),glyph(' ',65,10,{sp:1}),...row('world.',80,10,{w:2}),...row('Next line!',10,30,{bk:1})]),
  page([...row('一段目',10,10,{bk:17}),...row('二段目',10,28,{bk:18}),...row('三段目',10,46,{bk:19}),...row('別の欄',230,28,{bk:25})],{source:'apple',engine_revision:'vision'}),
  page([glyph('右',70,10,{line:0,vertical:true}),glyph('列',70,25,{line:0,vertical:true}),glyph('左',45,10,{line:1,vertical:true}),glyph('列',45,25,{line:1,vertical:true})],{source:'pi',engine_revision:'pi-manga/1',character_geometry:'estimated'}),
  page([...row('トートマ',70,10),...row('味がある。',10,30),glyph('ン',70,50)],{source:'pc',engine_revision:'pc-manga/1',layout:{regions:[
    {kind:'table-cell',tableId:'t',row:0,column:1,order:0,ranges:[[0,3],[9,9]]},
    {kind:'table-cell',tableId:'t',row:0,column:0,order:1,ranges:[[4,8]]}
  ]}}),
  page([...row('SARS',10,10,{bk:9}),...row('SARS',10,40,{bk:17})]),
  page([],{}),
];
const cases = [];
for (let p=0;p<pages.length;p++) {
  const raw=pages[p], core=context.BWNativePDFSelection(raw);
  const add=(method,args)=> {
    const value=core[method](...args);
    cases.push({page:p,method,args,expected:value == null ? null : (method==='hit' ? value : {
      indexes:value.indexes,text:value.text,sentence:value.sentence,rects:value.rects,
      quality:value.quality??null,matches:value.matches??1
    })});
  };
  for(let a=0;a<raw.chars.length;a++) {
    add('exact',[[a]]);add('sentence',[[a]]);
    const c=raw.chars[a];add('hit',[(c.x0+c.x1)/2,(c.y0+c.y1)/2,-1,true]);
    add('hit',[c.x1+7,c.y1+8,a,false]);
    for(let b=a;b<Math.min(a+7,raw.chars.length);b++) add('range',[a,b]);
  }
  if(raw.chars.length) {
    add('exact',[[0,raw.chars.length-1]]);
    add('binding',[{text:raw.chars.filter(c=>!c.sp).map(c=>c.c).join('')}]);
    add('binding',[{ois:[0],text:raw.chars[0].c}]);
    add('binding',[{from:0,to:Math.min(3,raw.chars.length-1)}]);
  }
  add('binding',[{text:'SARS'}]);add('binding',[{text:'SARS',block:2}]);add('binding',[{text:'SARS',block:99}]);
  add('hit',[399,599,-1,true]);
}
writeFileSync(process.argv[2],JSON.stringify({pages,cases}));
console.log(`Generated ${cases.length} geometry parity cases from the web core`);
