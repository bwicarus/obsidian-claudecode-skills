import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const registrySource = fs.readFileSync(new URL('../../_server_deploy/static/reader-runtime/context-selection-registry.js',import.meta.url),'utf8');
const bridge = fs.readFileSync(new URL('../../ios/BWReader/App/ReaderNativeContextSelectionBridge.swift',import.meta.url),'utf8').split('static let script = #"""')[1].split('"""#')[0];
function setup() {
  const server = vm.createContext({}); vm.runInContext(registrySource,server);
  const ids = new Set(), requests = [], events = [];
  let session = '', sequence = 0, fail = false, gate = null;
  const run = code => vm.runInContext(code,server);
  const projection = () => JSON.parse(JSON.stringify({revision:run('BWReaderRuntime.contextSelections.version()'),
    selected:[...ids].filter(id=>{server.id=id;return run('BWReaderRuntime.contextSelections.isSelected(id)');}),
    snapshot:run('BWReaderRuntime.contextSelections.snapshot({maxText:Number.MAX_SAFE_INTEGER})')}));
  const context = vm.createContext({console,crypto:{randomUUID:()=> 'session-test'},
    CustomEvent:class {constructor(type,init){this.type=type;this.detail=init?.detail;}},
    dispatchEvent:e=>events.push(e),
    setTimeout(){throw new Error('App registry must not create a browser expiry timer');},
    clearTimeout(){throw new Error('App registry must not own a browser expiry timer');},
    webkit:{messageHandlers:{bwNativeContextSelection:{async postMessage(command) {
      requests.push(JSON.parse(JSON.stringify(command)));
      if(gate)await gate;
      if(fail)throw new Error('native owner unavailable');
      if(command.action==='start'){session=command.session;return {ok:true,session,sequence:0,state:projection()};}
      assert.equal(command.session,session);assert.equal(command.sequence,++sequence);
      if(command.action==='mutate') {
        const c=command.value; server.command=JSON.stringify(c);
        if(c.id)ids.add(c.id);
        run(`var c=JSON.parse(command), r=BWReaderRuntime.contextSelections;
          if(c.record)r.upsert(Object.assign({},c.record,Object.hasOwn(c,'selected')?{selected:c.selected}:{}));
          if(c.operation!=='upsert')r[c.operation](c.id,c.on);`);
      }
      return {ok:true,session,sequence,state:projection()};
    }}}}});
  context.window=context;context.top=context;
  vm.runInContext(bridge,context);vm.runInContext(registrySource,context);
  const invoke = (operation,...args) => {
    context.args=JSON.stringify(args);
    return vm.runInContext(`BWReaderRuntime.contextSelections.${operation}(...JSON.parse(args))`,context);
  };
  return {context,invoke,requests,events,
    value:()=>JSON.parse(JSON.stringify(invoke('snapshot'))),
    fail:()=>{fail=true;},hold:()=>{let release;gate=new Promise(r=>release=r);return()=>{gate=null;release();};},
    expire(id){server.id=id;run('BWReaderRuntime.contextSelections.deselect(id)');context.__bwNativeContextSelections.accept({session,state:projection()});},
    server,projection};
}

test('native selection keeps one entity, maximal-node context, full metadata and original order',async()=>{
  const s=setup();
  s.invoke('select',{id:'part',parentId:'whole',text:'partial'});
  s.invoke('select',{id:'whole',kind:'card',label:'card',text:'full',source:{gid:'kept'},meta:{revision:9}});
  s.invoke('upsert',{id:'whole',text:'updated'});
  await s.invoke('settle');
  assert.equal(s.value().items.length,1);assert.equal(s.value().items[0].text,'updated');
  assert.deepEqual(s.value().items[0].source,{gid:'kept'});
  s.invoke('deselect','whole'); await s.invoke('settle');
  assert.deepEqual(s.value().items.map(x=>x.id),['part']);
  assert.deepEqual(s.requests.map(x=>x.action),['start','mutate','mutate','mutate','read','mutate','read']);
});

test('native expiry updates the synchronous compatibility projection without resetting a JS timer',async()=>{
  const s=setup();s.invoke('select',{id:'a',text:'card'});await s.invoke('settle');
  s.expire('a');assert.equal(s.invoke('isSelected','a'),false);assert.deepEqual(s.value().items,[]);
  s.invoke('select','a');await s.invoke('settle');assert.equal(s.invoke('isSelected','a'),true);
  assert.equal(s.context.__bwNativeContextSelections.accept({session:'previous-book',state:s.projection()}),false);
});

test('pending changes do not get overwritten by old acknowledgements and settle waits for them',async()=>{
  const s=setup();await s.invoke('settle');
  const release=s.hold();s.invoke('select',{id:'one',text:'one'});
  const pending=s.invoke('settle');s.invoke('select',{id:'two',text:'two'});
  assert.equal(s.value().items.length,2);release();await pending;
  assert.deepEqual(s.value().items.map(x=>x.id),['one','two']);
});

test('subscriber writes stay after their triggering operation, not before it',async()=>{
  const s=setup();await s.invoke('settle');
  let done=false;
  s.context.BWReaderRuntime.contextSelections.subscribe(event=>{
    if(!done&&event.type==='select'&&event.id==='first'){done=true;s.invoke('select',{id:'next',parentId:'first',text:'child'});}
  });
  s.invoke('select',{id:'first',text:'parent'});await s.invoke('settle');
  assert.deepEqual(s.requests.filter(x=>x.action==='mutate').map(x=>x.value.id),['first','next']);
  assert.deepEqual(s.value().items.map(x=>x.id),['first']);
});

test('failed native commit prevents request snapshots and never falls back to stale selected cards',async()=>{
  const s=setup();await s.invoke('settle');s.fail();s.invoke('select',{id:'a',text:'not committed'});
  await assert.rejects(s.invoke('settle'),/native owner unavailable/);
  assert.throws(()=>s.invoke('snapshot'),/native owner unavailable/);
  assert.ok(s.events.some(x=>x.type==='rc:context-selection-error'));
});

test('native context contract is attached at document start and invalidated with navigation/recovery',()=>{
  const host=fs.readFileSync(new URL('../../ios/BWReader/App/ReaderWebView.swift',import.meta.url),'utf8');
  assert.match(host,/ReaderNativeContextSelectionBridge\.script,[\s\S]{0,90}\.atDocumentStart/);
  assert.equal((host.match(/nativeContextSelections\?\.invalidate\(\)/g)||[]).length,2);
  const assistant=fs.readFileSync(new URL('../../_server_deploy/static/pdf/rc-assistant.js',import.meta.url),'utf8');
  const send=assistant.slice(assistant.indexOf('var _preparingNativeContext = false;'));
  assert.ok(send.indexOf('await selections.settle()')<send.indexOf('var sentCtx = ctx()'));
  assert.match(send,/finally \{ _preparingNativeContext = false; \}/);
});
