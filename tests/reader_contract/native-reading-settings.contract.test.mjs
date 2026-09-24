import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const source = readFileSync(new URL('../../_server_deploy/static/pdf/reader.src/21-misc-ai.js', import.meta.url),'utf8');
const begin = source.indexOf('window.__bwReaderAcceptNativeReadingSettings =');
const adapter = source.slice(begin, source.indexOf('function _applyDebugVisibility()',begin));
function setup() {
  const effects = [];
  const context = { FILE_REL:'localbook:book', BOOK_LANGS:[], _crop:{}, _cropOn:false,
    __BW_NATIVE_DATA_STORE_REQUIRED__:true, RC:{readerPreferences:{}},
    _applyDebugVisibility:()=>effects.push('debug'),
    _rememberOrientLayout:()=>effects.push('orientation'),
    setGrammarView:(mode,persist)=>effects.push({mode,persist}),
    __bwNativeBookLanguagesChanged:(file,langs)=>effects.push({file,langs}),
    fetch:()=>{throw Error('observer must not fetch')},
    localStorage:{setItem(){throw Error('observer must not persist')}},
    document:{querySelectorAll(){throw Error('observer must not build hidden page layers')}} };
  context.window = context;
  vm.runInNewContext(adapter,context);
  return {context,effects,apply:context.__bwReaderAcceptNativeReadingSettings};
}
const state = {host:'pdf',book:'localbook:book',languages:['ja'],crop:{l:1.25,r:0,t:0,b:0},cropEnabled:true,
  figures:true,figuresAvailable:true,warnings:[],grammar:'tree',autoOrient:true};
test('native settings mirror updates committed state without hidden rendering or second writes',()=>{
  const {context,effects,apply}=setup();
  assert.equal(apply(state,'languages'),true);
  assert.deepEqual(context.BOOK_LANGS,['ja']);
  assert.equal(context._crop.l,1.25); assert.equal(context._cropOn,true);
  assert.equal(context.__figBookOn,true);
  apply(state,'grammar');
  assert.equal(effects.at(-1).persist,false);
  apply(state,'autoOrient'); assert.equal(effects.at(-1),'orientation');
  apply(state,'debug'); assert.equal(effects.at(-1),'debug');
});
test('late settings for another book and non-App hosts cannot replace current state',()=>{
  const {context,apply}=setup();
  assert.equal(apply({...state,book:'localbook:old'},'languages'),false);
  assert.deepEqual(context.BOOK_LANGS,[]);
  context.__BW_NATIVE_DATA_STORE_REQUIRED__=false;
  assert.equal(apply(state,'languages'),false);
  assert.deepEqual(context._crop,{});
});
