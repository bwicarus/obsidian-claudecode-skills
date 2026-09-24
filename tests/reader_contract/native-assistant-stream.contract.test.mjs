import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {randomUUID} from 'node:crypto';
const source = readFileSync(new URL('../../ios/BWReader/App/ReaderNativeAssistantStreamBridge.swift',import.meta.url),'utf8');
const script = source.split('#"""')[1].split('"""#')[0];
function setup(prepareReply = {ok:true}) {
  const requests=[]; let resolve,reject;
  const pending = new Promise((a,b)=>{resolve=a;reject=b});
  const window={webkit:{messageHandlers:{bwNativeAssistantStream:{postMessage(command){
    requests.push(command); return command.action === 'start' ? pending : Promise.resolve(command.action === 'prepare' ? prepareReply : {ok:true});
  }}}}}; window.top=window;
  vm.runInNewContext(script,{window,crypto:{randomUUID}});
  return {api:window.__bwNativeAssistantStream,requests,resolve,reject};
}

test('native preparation returns the canonical message and identity without starting transport', async()=>{
  const body={rid:'cfixed',turn_id:'tfixed',message:'讲讲这段',assistant_mode:'normal',context:{selection:'接種'}};
  const {api,requests}=setup({ok:true,body});
  const prepared=await api.prepare({message:'',assistant_mode:'normal',context:{selection:'接種'}});
  assert.equal(prepared,body);
  assert.equal(requests.length,1);
  assert.equal(requests[0].action,'prepare');
  assert.equal(requests[0].body.message,'');
});

test('invalid preparation does not silently return the old request', async()=>{
  for (const reply of [{ok:false},{ok:true,body:{}},{ok:true,body:{rid:'c',turn_id:'t'}}]) {
    const {api,requests}=setup(reply);
    await assert.rejects(api.prepare({message:'hello',context:{}}),/上下文未准备好/);
    assert.deepEqual(requests.map(x=>x.action),['prepare']);
  }
});

test('changing conversation mode during preparation discards the stale turn and prevents a concurrent send', async()=>{
  const assistant=readFileSync(new URL('../../_server_deploy/static/pdf/rc-assistant.js',import.meta.url),'utf8');
  const prefix=assistant.slice(assistant.indexOf('var _preparingNativeContext = false;'),assistant.indexOf('var _historyWasLoading = _historyLoadCount > 0;'));
  let finish; const pending=new Promise(resolve=>{finish=resolve;}); let calls=0;
  const runtime={window:{__bwNativeAssistantStream:{prepare(){calls++;return pending;}}},
    streaming:false,_clearing:false,_modeEpoch:0,_assistantMode:'normal',
    _chatUrl:()=>'/api/assistant/chat',ctx:()=>({selection:'接種'}),addMsg:()=>{},esc:x=>x,ta:{value:''},autorow(){}};
  vm.createContext(runtime);
  vm.runInContext(prefix+'return {text,sentCtx}; } globalThis.send = send;',runtime);
  const first=runtime.send('explain');
  assert.equal(await runtime.send('duplicate'),undefined);
  assert.equal(calls,1);
  runtime._modeEpoch++;
  finish({message:'explain',context:{selection:'接種'},rid:'c1',turn_id:'t1'});
  assert.equal(await first,undefined);
  assert.equal(vm.runInContext('_preparingNativeContext',runtime),false);
});
test('native stream consumes structured events once and waits for native completion',async()=>{
  const {api,requests,resolve}=setup(), events=[];
  const result=api.run('/api/assistant/chat',{message:'test',omit:undefined},(...args)=>events.push(args));
  const id=requests[0].id;
  assert.equal('omit' in requests[0].body,false);
  const batch={id,sequence:1,events:[{name:'actions',data:'[{"id":"a"}]'},{name:'answer',data:'"日本語"'}]};
  assert.equal(api.accept(batch).ok,true);
  assert.equal(api.accept(batch).ok,true);assert.equal(events.length,2);
  assert.equal(events[1][1],'日本語');
  assert.equal(api.accept({...batch,sequence:3}).ok,false);
  resolve({ok:true,status:'done'});assert.equal(await result,'done');
  assert.equal(api.accept({...batch,sequence:2}).ok,false);
});
test('stop sends native cancellation; late events cannot execute actions',async()=>{
  const {api,requests,resolve}=setup(), controller=new AbortController(), events=[];
  const result=api.run('/api/assistant/chat',{},event=>events.push(event),controller.signal);
  controller.abort();
  assert.equal(requests[1].action,'cancel');assert.equal(requests[1].id,requests[0].id);
  assert.equal(api.accept({id:requests[0].id,sequence:1,events:[{name:'actions',data:'[]'}]}).ok,false);
  resolve({ok:true,status:'aborted'});await assert.rejects(result,{name:'AbortError'});
  assert.equal(events.length,0);
});
test('unknown native outcome does not start browser fetch or resubmit',async()=>{
  const {api,requests,reject}=setup();
  const result=api.run('/api/assistant/chat',{},()=>{});
  reject(new Error('lost delivery')); await assert.rejects(result,/lost delivery/);
  assert.equal(requests.length,1);
  const assistant=readFileSync(new URL('../../_server_deploy/static/pdf/rc-assistant.js',import.meta.url),'utf8');
  assert.match(assistant,/if \(window\.__bwNativeAssistantStream\)[\s\S]+?\} else while \(!done && !aborted\)/);
});

test('native history adapter retains status and payload; failure does not retry the web transport', async()=>{
  const calls=[];
  const window={webkit:{messageHandlers:{bwNativeAssistantStream:{async postMessage(command){
    calls.push(command);
    if(command.operation==='clear') throw new Error('unknown clear');
    return {ok:false,status:401,body:'{"ok":false}'};
  }}}}}; window.top=window;
  vm.runInNewContext(script,{window,crypto:{randomUUID}});
  const response=await window.__bwNativeAssistantHistory.request('/api/assistant/history','read','normal');
  assert.equal(response.status,401); assert.equal(response.ok,false);
  assert.deepEqual(JSON.parse(JSON.stringify(await response.json())),{ok:false});
  await assert.rejects(window.__bwNativeAssistantHistory.request('/api/assistant/clear','clear','normal'),/unknown clear/);
  assert.equal(calls.length,2);
});
