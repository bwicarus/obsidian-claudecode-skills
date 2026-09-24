import test from 'node:test';
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {readFileSync} from 'node:fs';
const swift=readFileSync(new URL('../../ios/BWReader/App/ReaderNativeFigureBridge.swift',import.meta.url),'utf8');
const script=swift.split('#"""')[1].split('"""#')[0];
function setup(){
  const sent=[],events=[];
  const window={webkit:{messageHandlers:{bwNativeConversation:{postMessage:m=>sent.push(m)}}},dispatchEvent:e=>events.push(e)};
  const ctx=vm.createContext({window,FILE_REL:'localbook:one',Event:class{constructor(type){this.type=type}}});
  const apply=payload=>{ctx.payload=payload;return vm.runInContext('(function(){'+script+'})()',ctx)};
  return {window,sent,events,apply};
}
const item=(token='a')=>({id:'1:0,0,1,1',page:1,box:[0,0,1,1],token,ink:[{p:[[.2,.3]]}]});
const payload=(items,revision=1,epoch='e')=>({file:'localbook:one',items,revision,epoch});
test('native figure projection consumes once without rendering or fetching hidden thumbnails',()=>{
  const {window,sent,apply}=setup();
  assert.equal(apply(payload([item()])),true);
  assert.equal(window.__figInk(1,[0,0,1,1]).length,1);
  window.__renderFigChips(); window.__clearFigFocus();
  assert.equal(window.__figAttached.length,0);
  assert.equal(sent.length,1);assert.deepEqual(Array.from(sent[0].tokens),['a']);
  // Equal-revision, delayed ink projection cannot resurrect consumed entries.
  apply(payload([item()]));assert.equal(window.__figAttached.length,0);
  apply(payload([],2));assert.equal(window.__bwNativeFigureConsumed.size,0);
  assert.equal(apply(payload([item()],1)),false);
  apply(payload([item('b')],3));assert.equal(window.__figAttached[0].token,'b');
});
test('wrong document is rejected; epoch reset keeps newly selected geometry',()=>{
  const {window,apply}=setup();
  assert.equal(apply({...payload([item()]),file:'localbook:two'}),false);
  assert.equal(window.__figAttached,undefined);
  apply(payload([item()]));window.__clearFigFocus();
  apply(payload([item()],0,'new'));
  assert.equal(window.__figAttached.length,1);
});
