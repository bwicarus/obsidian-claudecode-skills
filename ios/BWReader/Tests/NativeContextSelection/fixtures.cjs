const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.resolve(__dirname,'../../../..','_server_deploy/static/reader-runtime/context-selection-registry.js'),'utf8');
let now = 0, timerID = 0;
const timers = new Map();
const context = {setTimeout(fn,ms) {const id=++timerID; timers.set(id,{at:now+ms,fn});return id;},clearTimeout(id){timers.delete(id);}};
vm.runInNewContext(source,context);
const registry = context.BWReaderRuntime.contextSelections;
const inputs = [];
function state() { return JSON.parse(JSON.stringify(registry.snapshot({maxText:100000}))); }
function step(operation,input,on) {
  // Use values in the registry's realm (metadata must be plain JSON objects).
  context.command = JSON.stringify({operation,input,on});
  vm.runInNewContext('var cmd=JSON.parse(command); BWReaderRuntime.contextSelections[cmd.operation](cmd.input,cmd.on);',context);
  const value={operation};
  if (operation !== 'clear') value.id=String(typeof input==='object'?input.id:input).trim();
  if(operation==='upsert'||typeof input==='object') {
    value.record=JSON.parse(JSON.stringify(registry.get(value.id)));
    if(Object.hasOwn(input,'selected'))value.selected=!!input.selected;
  }
  if(operation==='select')value.on=on!==false;
  inputs.push({command:value,now:now/1000,expected:state()});
}
function advance(ms) {
  now += ms;
  for(const [id,t] of timers)if(t.at<=now){timers.delete(id);t.fn();}
  inputs.push({now:now/1000,expected:state()});
}
step('select',{id:'card:A',label:'学习卡',text:'整卡',kind:'card',meta:{z:2,a:['保留',null,true]}});
step('select',{id:'part:A',parentId:'card:A',label:'段落',text:'局部'});
step('upsert',{id:'card:A',text:'完整正文修改'});
step('deselect','card:A');
step('select','card:A');
step('select',{id:'whole',covers:['card:A','card:A','', 'whole'],label:'全组',text:'组正文'});
step('select',{id:'cycle:B',covers:['cycle:A'],text:'B'});
step('select',{id:'cycle:A',covers:['cycle:B'],text:'A'});
step('select',{id:'é',text:'NFC'}); step('select',{id:'e\u0301',text:'NFD'});
step('select',{id:'\u{10000}',text:'astral before BMP in JS sort'});step('select',{id:'\uE000',text:'BMP'});
advance(299999);step('select','whole');advance(2);
step('select','card:A');step('upsert',{id:'card:A',text:'不会续期'});advance(300001);
step('upsert',{id:'persist',selected:true,text:'upsert 不新建 TTL'});advance(300001);
step('toggle','persist');step('toggle','persist');advance(300001);
step('select',{id:'remove-me',text:'removed'});step('remove','remove-me');advance(300001);
step('select',{id:'clear-me',text:'cleared'});step('clear');advance(300001);
// Deterministic overlapping graphs, updates, removal and expiry.
let seed=17023; const rand=n=>{seed=(Math.imul(seed,1664525)+1013904223)>>>0;return seed%n;};
for(let i=0;i<180;i++) {
  const id='random-'+rand(18), mode=rand(7);
  if(mode<3)step('select',{id,label:'同名卡片',text:'语义正文 '+i,parentId:mode===0?'parent-'+rand(3):'',covers:mode===1?['random-'+rand(18)]:[]});
  else if(mode===3)step('deselect',id);
  else if(mode===4)step('toggle',id);
  else if(mode===5)step('remove',id);
  else advance(150001);
}
fs.writeFileSync(process.argv[2],JSON.stringify(inputs));
console.log('Context selection parity fixtures:',inputs.length);
