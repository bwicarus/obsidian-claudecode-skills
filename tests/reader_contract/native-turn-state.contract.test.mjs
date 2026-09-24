import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
import {randomUUID} from 'node:crypto';

const swift = readFileSync(new URL('../../ios/BWReader/App/ReaderNativeTurnBridge.swift',import.meta.url),'utf8');
const script = swift.split('#"""')[1].split('"""#')[0];
const plain = value => JSON.parse(JSON.stringify(value));
function host(deliver = command => ({ok:true,session:command.session,sequence:command.sequence,result:{}})) {
  const requests = [], events = [];
  const window = {dispatchEvent:event=>events.push(event)}; window.top = window;
  window.webkit = {messageHandlers:{bwNativeTurns:{postMessage(command) {
    requests.push(plain(command));
    if (command.action === 'start') return Promise.resolve({ok:true,session:command.session,sequence:0});
    if (command.action === 'failure') return Promise.resolve({ok:true});
    return Promise.resolve(deliver(command));
  }}}};
  vm.runInNewContext(script,{window,crypto:{randomUUID},queueMicrotask,CustomEvent:class { constructor(type,{detail}) { this.type=type; this.detail=detail; } }});
  return {api:window.__bwNativeTurns,requests,events};
}

test('consecutive same-item deltas collapse but freeze and another speaker fence ordering',async()=>{
  const {api,requests} = host(), accepted = [];
  api.connect(value=>accepted.push(value));
  const draft = {action:'draft',tid:'one',itemId:'a',origin:'runner',role:'assistant'};
  api.enqueue({...draft,text:'first'});
  api.enqueue({...draft,text:'complete'});
  api.enqueue({action:'freeze',tid:'one',itemId:'a',origin:'runner',role:'assistant'});
  api.enqueue({...draft,text:'new answer'});
  api.enqueue({...draft,role:'user',text:'new question'});
  await api.settle();
  const batches = requests.filter(x=>x.action==='apply');
  assert.equal(batches.length,1);
  assert.deepEqual(batches[0].commands.map(x=>[x.action,x.text||'',x.role]),[
    ['draft','complete','assistant'],['freeze','','assistant'],['draft','new answer','assistant'],['draft','new question','user']
  ]);
  assert.equal(accepted.length,1);
});

test('save waits for native freeze acknowledgement and does not overlap a later state batch',async()=>{
  let release;
  const blocked = new Promise(resolve=>release=resolve);
  const {api,requests} = host(async command=>{
    if(command.action==='apply') { await blocked; return {ok:true,session:command.session,sequence:command.sequence,result:{}}; }
    return {ok:true,result:{saved:true}};
  });
  api.enqueue({action:'freeze',tid:'one'});
  const saving = api.logVoice('one',{user:'问',assistant:'答'});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(requests.some(x=>x.action==='logVoice'),false);
  release();
  assert.deepEqual(plain(await saving),{saved:true});
  assert.deepEqual(requests.map(x=>x.action),['start','apply','logVoice']);
});

test('rejected or mismatched delivery stops the queue and never falls back to legacy state',async()=>{
  const {api,requests,events} = host(command=>({ok:true,session:command.session,sequence:command.sequence+1,result:{}}));
  api.enqueue({action:'append',tid:'one',part:{kind:'tool',call_id:'once'}});
  await assert.rejects(api.settle(),/未获确认/);
  assert.throws(()=>api.enqueue({action:'append',tid:'two'}),/未获确认/);
  await assert.rejects(api.persist('one',{}),/未获确认/);
  assert.equal(requests.filter(x=>x.action==='apply').length,1);
  assert.equal(requests.some(x=>x.action==='persist'),false);
  assert.equal(events[0].type,'rc:native-turn-error');
});

test('compatibility projection failure is reported to the native UI, not silently saved',async()=>{
  const {api,requests,events}=host();
  api.connect(()=>{throw new Error('artifact not ready');});
  api.enqueue({action:'append',tid:'one',part:{kind:'cards',gid:'card_aaaa'}});
  await assert.rejects(api.settle(),/artifact not ready/);
  assert.equal(requests.filter(x=>x.action==='failure').length,1);
  assert.equal(events[0].detail.message,'artifact not ready');
});

test('a failed batch blocks all queued successors and reports the original failure once',async()=>{
  let reject;
  const blocked = new Promise((_,fail)=>reject=fail);
  const {api,requests,events} = host(command=>command.action==='apply' ? blocked : {ok:true});
  api.enqueue({action:'draft',tid:'one',text:'first'});
  await new Promise(resolve=>setImmediate(resolve));
  api.enqueue({action:'freeze',tid:'one'});
  await new Promise(resolve=>setImmediate(resolve));
  reject({message:'connection interrupted'});
  await assert.rejects(api.settle(),/connection interrupted/);
  assert.equal(requests.filter(x=>x.action==='apply').length,1);
  assert.equal(requests.filter(x=>x.action==='failure').length,1);
  assert.deepEqual(events.map(x=>x.detail.message),['connection interrupted']);
  await assert.rejects(api.logVoice('one',{user:'问题',assistant:'旧回复'}),/connection interrupted/);
  assert.equal(requests.some(x=>x.action==='logVoice'),false);
});

const assistant = readFileSync(new URL('../../_server_deploy/static/pdf/rc-assistant.js',import.meta.url),'utf8');
const voiceStart = assistant.indexOf('window.__asstVoiceLog = async function');
const voiceCode = assistant.slice(voiceStart,assistant.indexOf('window.__asstHistUrl',voiceStart));
function voiceHost({settle=async()=>{},logVoice=async()=>{},native=true}={}) {
  const calls=[];
  const state={window:{dispatchEvent:event=>calls.push(['error',event.detail.message])},
    RC:{turnCard:{freezeDraft:tid=>calls.push(['freeze',tid]),settle,partsOf:()=>[{kind:'text',text:'complete'}]}},
    HOST:{voiceLog(){calls.push(['legacy-host']);}},_assistantMode:'normal',_modeEpoch:0,_vTid:'v1',_vTurnEl:null,_liveSeen:{},
    _historyMarkSeen:id=>calls.push(['seen',id]),fetch:()=>{calls.push(['legacy-fetch']);return Promise.resolve({});},
    CustomEvent:class{constructor(type,{detail}){this.type=type;this.detail=detail;}}};
  if(native)state.window.__bwNativeTurns={async logVoice(tid,body){calls.push(['native-log',tid,plain(body)]);return logVoice(tid,body);}};
  vm.runInNewContext(voiceCode,state);
  return {calls,state,log:()=>state.window.__asstVoiceLog('问','答','book',7,{clip:'clip-1'})};
}
test('actual voice response completion drains state and uses the native history writer once',async()=>{
  let release;const waiting=new Promise(resolve=>release=resolve);
  const {calls,log}=voiceHost({settle:()=>waiting});
  const pending=log();
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(calls,[['freeze','v1']]);
  release();await pending;
  assert.deepEqual(calls.map(x=>x[0]),['freeze','seen','seen','native-log']);
  const metadata=calls.at(-1)[2];
  assert.equal(metadata.user,'问');assert.equal(metadata.clip,'clip-1');assert.equal(metadata.assistant_mode,'normal');
});
test('voice native failure or a mode switch never falls back to a legacy writer',async()=>{
  for(const failAt of ['settle','logVoice']) {
    const {calls,log}=voiceHost({[failAt]:async()=>{throw new Error('not committed');}});
    await log();
    assert.equal(calls.filter(x=>x[0].startsWith('legacy')).length,0);
    assert.deepEqual(calls.at(-1),['error','not committed']);
  }
  let release;const waiting=new Promise(resolve=>release=resolve);
  const {calls,state,log}=voiceHost({settle:()=>waiting});
  const pending=log();state._modeEpoch++;release();await pending;
  assert.deepEqual(calls,[['freeze','v1']]);
  const browser=voiceHost({native:false});await browser.log();
  assert.deepEqual(browser.calls,[['legacy-host']]);
});
const clearStart = assistant.indexOf('async function _clearCurrentConversation()');
const clearCode = assistant.slice(clearStart,assistant.indexOf('// 快捷按钮',clearStart));
function clearHost(request) {
  const calls=[];
  const state = {window:{__bwNativeAssistantHistory:{request}},RC:{
    turnCard:{async settle(){calls.push('settle');},reset(){calls.push('reset');}},
    toolChip:{clearAll(){calls.push('tools-cleared');}}
  },_clearing:false,_assistantMode:'normal',_modeEpoch:0,_historyEpoch:0,streaming:false,
  thread:{innerHTML:'old history'},micStop(){},_setSendMode(){},_setClearingUi(on){calls.push(['busy',on]);},
  _clearUrl:()=>'/api/assistant/clear',greet(){calls.push('greet');},
  async loadHistory(mode){calls.push(['reload',mode]);},_toast(){}};
  vm.createContext(state);
  vm.runInContext(clearCode,state);
  return {state,calls,clear:()=>state._clearCurrentConversation()};
}

test('confirmed clear resets native artifact handles only after server acknowledgement',async()=>{
  let finish;
  const reply=new Promise(resolve=>finish=resolve);
  const {calls,clear}=clearHost(()=>reply);
  const clearing=clear();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(calls.includes('reset'),false);
  finish({ok:true,json:async()=>({ok:true})});
  assert.equal(await clearing,true);
  assert.deepEqual(calls,[['busy',true],'settle','reset','settle','tools-cleared','greet',['busy',false]]);
});

test('unconfirmed clear rereads authoritative history without clearing native handles',async()=>{
  for(const payload of [null,{}, {ok:false}]) {
    const {calls,clear}=clearHost(async()=>({ok:true,json:async()=>payload}));
    assert.equal(await clear(),false);
    assert.equal(calls.includes('reset'),false);
    assert.equal(calls.includes('greet'),false);
    assert.deepEqual(calls.filter(Array.isArray),[['busy',true],['reload','normal'],['busy',false]]);
  }
});
