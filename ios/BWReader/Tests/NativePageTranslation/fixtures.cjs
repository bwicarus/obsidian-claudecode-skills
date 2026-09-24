const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const root=path.resolve(__dirname,'../../../..');
const runtime=fs.readFileSync(path.join(root,'_server_deploy/static/pdf/native-local-runtime.js'),'utf8');
const page=fs.readFileSync(path.join(root,'_server_deploy/static/pdf/reader.src/10-pagetranslate.js'),'utf8');
const context=vm.createContext({});
vm.runInContext(runtime.slice(runtime.indexOf('  function roundedPageCoordinate('),runtime.indexOf('  function nativePageTranslate('))+
  page.slice(page.indexOf('function _mergeLines('),page.indexOf('function _drawPageTranslate(')),context);
function chars(text){let x=0,y=0,bk=1;return Array.from(text).flatMap(c=>{
  if(c==='\n'){x=0;y+=20;return []}if(c==='\t'){x=180;y=0;bk++;return []}
  const char={c,sp:c===' ',x0:x,y0:y,x1:x+9,y1:y+14,bk};x+=10;return [char];
});}
const inputs=[
  chars('これは例文です。次も例文です！'),chars('Dr.test has 3.14 things. Next one!'),
  chars('• First item\n▪ Second item\tThe next column.'),chars('字'),[],
  chars('Line wraps around\nhere, and ends. '),chars('中文测试？ 别的句子！！'),
  chars('A paragraph that is long enough.'),chars('word test.'),chars('😀😀😀😀!')
];
const ruby=chars('これは東京の文章です。');ruby.splice(4,0,{c:'と',x0:40,y0:0,x1:44,y1:5,sp:false,bk:1});inputs.push(ruby);
const vertical=chars('日本の縦書き文章。').map((c,i)=>({...c,x0:0,x1:12,y0:i*15,y1:i*15+14}));inputs.push(vertical);
for(let n=1;n<25;n++)inputs.push(chars('word '.repeat(n)+'3.14。\n'+'改行测试'.repeat(n)+'！'));
const cases=inputs.map(chars=>{
  const sentences=context.nativeSplitPageSentences(chars);
  const translated=sentences.map((s,i)=>({...s,zh:i%2?'这是第一个译文👩‍👩‍👧‍👦e\u0301':'这是比较长的中文译文，保留各行位置。'.repeat(4)}));
  return {chars,sentences,translated,slices:context._pageTranslateSlices(translated)};
});
fs.writeFileSync(process.argv[2],JSON.stringify({cases}));console.log('Page translation oracle:',cases.length,'cases');
