import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {randomUUID} from 'node:crypto';
const source = readFileSync(new URL('../../ios/BWReader/App/ReaderNativeAssistantStreamBridge.swift',import.meta.url),'utf8');
const script = source.split('#"""')[1].split('"""#')[0];
function setup() {
  const requests=[]; let resolve,reject;
  const pending = new Promise((a,b)=>{resolve=a;reject=b});
  const window={webkit:{messageHandlers:{bwNativeAssistantStream:{postMessage(command){
    requests.push(command); return command.action === 'start' ? pending : Promise.resolve({ok:true});
  }}}}}; window.top=window;
  vm.runInNewContext(script,{window,crypto:{randomUUID}});
  return {api:window.__bwNativeAssistantStream,requests,resolve,reject};
}
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
