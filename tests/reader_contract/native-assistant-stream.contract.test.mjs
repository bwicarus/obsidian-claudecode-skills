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
    requests.push(command); return ['start','watchTask'].includes(command.action) ? pending : Promise.resolve(command.action === 'prepare' ? prepareReply : {ok:true});
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

test('native composer gets acceptance before completion and gets a rejection when preparation fails', async()=>{
  const source=readFileSync(new URL('../../_server_deploy/static/pdf/rc-assistant.js',import.meta.url),'utf8');
  const wrapper=source.slice(source.indexOf('  window.__asstSendAccepted ='),source.indexOf('  window.__asstBusy ='));
  let finish;
  const runtime={window:{},send:async(text,opts)=>{
    opts.onAccepted({ok:true}); await new Promise(resolve=>{finish=resolve;});
  }};
  vm.runInNewContext(wrapper,runtime);
  assert.equal((await runtime.window.__asstSendAccepted('hello')).ok,true);
  finish();

  const prefix=source.slice(source.indexOf('var _preparingNativeContext = false;'),source.indexOf('var _historyWasLoading = _historyLoadCount > 0;'));
  const actual={window:{__bwNativeAssistantStream:{async prepare(){throw new Error('source expired');}}},
    streaming:false,_clearing:false,_modeEpoch:0,_assistantMode:'normal',_chatUrl:()=>'/api/assistant/chat',ctx:()=>({selection:'語'}),
    addMsg(){},esc:x=>x,ta:{value:''},autorow(){}};
  vm.runInNewContext(prefix+'throw new Error("unexpected submission"); }'+wrapper,actual);
  const rejected=await actual.window.__asstSendAccepted('请解释');
  assert.equal(rejected.ok,false); assert.match(rejected.error,/source expired/);
  assert.equal(actual.ta.value,'请解释');
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

test('native task watch joins duplicate requests, consumes in order and retires its callback', async()=>{
  const {api,requests,resolve}=setup(), snapshots=[];
  const pending=api.watchTask('cli','task-1',value=>snapshots.push(value));
  assert.equal(api.watchTask('cli','task-1',()=>{throw new Error('duplicate consumer');}),pending);
  await Promise.resolve();
  assert.equal(requests.length,1); assert.equal(requests[0].action,'watchTask');
  const id=requests[0].id, first={id,sequence:1,snapshot:{ok:true,status:'running'}};
  assert.equal(api.acceptTask(first).ok,true);assert.equal(api.acceptTask(first).ok,true);
  assert.equal(api.acceptTask({...first,sequence:3}).ok,false);
  assert.equal(snapshots.length,1);
  assert.equal(api.acceptTask({id,sequence:2,snapshot:{ok:true,status:'done',result:{undo_id:'same'}}}).ok,true);
  resolve({ok:true,status:'done'});assert.equal(await pending,'done');
  assert.equal(api.acceptTask({...first,sequence:3}).ok,false);
  assert.equal(snapshots.length,2);
});

test('native task consumers retain result and undo controls without starting browser polling', async()=>{
  const source=readFileSync(new URL('../../_server_deploy/static/pdf/rc-assistant.js',import.meta.url),'utf8');
  const start=source.indexOf('  function trackTask('), end=source.indexOf('\n  }',start)+4;
  const line={innerHTML:'',textContent:''}, actions=[],notifications=[];
  const runtime={window:{__bwNativeAssistantStream:{async watchTask(kind,id,consume){
    assert.equal(kind,'write');assert.equal(id,'task-1');
    consume({ok:true,status:'running',step:'saving'});
    consume({ok:true,status:'done',result:{undo_id:'same-undo'},speak:'saved',client_actions:[{fn:'refresh'}]});
    return 'done';
  }}},HOST:{},addMsg:()=>line,esc:x=>x,runActions:x=>actions.push(...x),notify:(...x)=>notifications.push(x),scrollDown(){},
  fetch(){throw new Error('web fetch invoked');},setTimeout(){throw new Error('web timer invoked');}};
  vm.runInNewContext(source.slice(start,end)+'\nglobalThis.track=trackTask;',runtime);
  await runtime.track('task-1','save');
  assert.match(line.innerHTML,/same-undo/);assert.equal(actions.length,1);assert.equal(notifications.length,1);
  const manifest=JSON.parse(readFileSync(new URL('../../ios/BWReader/native_reader_interface_manifest.json',import.meta.url),'utf8'));
  const routes=manifest.routes.filter(x=>x.path.startsWith('/api/voice/'));
  assert.deepEqual(routes.map(x=>[x.path,x.match,x.methods]),[['/api/voice/task-status','exact',['GET']]]);
});

test('native response projection drives display and voice without browser parsing or reveal layout',()=>{
  const source=readFileSync(new URL('../../_server_deploy/static/pdf/rc-assistant.js',import.meta.url),'utf8');
  const start=source.indexOf('    function _handleEv('), end=source.indexOf('    async function _stream(body)',start);
  const displayed=[],spoken=[];
  const runtime={turnEpoch:1,_modeEpoch:1,evSeen:0,done:false,answer:'',nativeTurnState:null,sawTool:false,sawCliCard:false,
    window:{__asstVoiceTap:text=>spoken.push(text)},aMsg:{classList:{add(){}},style:{}},
    _nativeOwnsThread:()=>true,_stopReveal(){},renderMd:(_,text)=>displayed.push(text),scrollDown(){},
    _toolChip(){},_splitFollowups(){throw new Error('browser parsing invoked');}};
  vm.createContext(runtime); vm.runInContext(source.slice(start,end)+'globalThis.accept = _handleEv;',runtime);
  runtime.accept('answer','raw markers',{answer:'raw markers',displayText:'干净正文',voiceText:'朗读正文',followups:[],sawTool:false,sawCliCard:false,done:false});
  assert.deepEqual(displayed,['干净正文']);assert.deepEqual(spoken,['朗读正文']);
  runtime.accept('tool2',{name:'lookup'},{sawTool:true,sawCliCard:false,done:false});
  assert.equal(runtime.sawTool,true); assert.equal(runtime.aMsg.style.display,'none');
  runtime.accept('done',{}, {sawTool:true,sawCliCard:false,done:true});
  assert.equal(runtime.done,true);
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

test('native history projection is read-only metadata and never leaks into saved original messages', async()=>{
  const message={role:'assistant',content:'[语气:认真]正文',card:{cid:'same',html:'<b>original</b>'}};
  const plan={kind:'card',mode:'normal',turnID:'hist_normal_x'};
  const window={webkit:{messageHandlers:{bwNativeAssistantStream:{async postMessage(){
    return {ok:true,status:200,body:JSON.stringify({ok:true,messages:[message]}),presentation:JSON.stringify([plan])};
  }}}}};window.top=window;
  vm.runInNewContext(script,{window,crypto:{randomUUID}});
  const response=await window.__bwNativeAssistantHistory.request('/api/assistant/history','read','normal');
  const row=(await response.json()).messages[0];
  assert.equal(row.__bwNativeHistory.turnID,plan.turnID);
  assert.deepEqual(JSON.parse(JSON.stringify(row)),message);
  assert.throws(()=>{row.__bwNativeHistory={kind:'user'};},TypeError);
});
