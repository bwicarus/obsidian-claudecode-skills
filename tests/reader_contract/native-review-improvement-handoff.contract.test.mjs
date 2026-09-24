import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../../_server_deploy/static/pdf/rc-review.js', import.meta.url), 'utf8');
const code = source.slice(source.indexOf('  async function _prepareNativeImprovement('), source.indexOf('  async function _prepareDraft('));
function fixture() {
  const r = {window:{}, _mode:true, _queueBusy:false, _queueRequestEpoch:1, _contextCacheKey:'book', _nativeQueueLease:'queue',
    _nativeNavigationWork:null, _nativeStageWork:null, _nativeReconcileWork:null, _cacheWriteChain:Promise.resolve(),
    _nativeImprovementFence:null, _draftState:null, _commitState:{}, _stagedRating:null, _ratingCommitBusy:0,
    _draftRequestEpoch:0, _commitRequestEpoch:0, _nativeDraftLease:'', _nativeReviewUI:()=>true,
    _current:()=>({id:'card'}), _stableCardId:card=>card.id, render(){}, _publishPresentation(){},
    _commitStagedRating:async()=>false};
  r._anyCommitBusy = () => Object.values(r._commitState).some(v=>v.busy);
  r._cardRequestCurrent = (epoch, id, kind) => r._mode && id === r._current().id &&
    epoch === (kind === 'commit' ? r._commitRequestEpoch : r._draftRequestEpoch);
  vm.createContext(r); vm.runInContext(code,r);
  const begin = (args={}) => r._prepareNativeImprovement({id:'operation-1',key:'prepareDraft',lease:'queue',cardId:'card',target:'anki',...args});
  const result = {draft:{ok:true,busy:false,_card_key:'card',draft_id:'draft',targets:['anki'],drafts:{front:'原题',back:'新答案'}},commits:{}};
  return {r, begin, result};
}

test('native draft handoff supplies only correlation metadata; duplicate and stale delivery cannot overwrite',async()=>{
  const {r,begin,result} = fixture();
  const fence = await begin();
  assert.equal(r._draftState.busy,true);
  assert.equal(fence.card,undefined); assert.equal(fence.pairs,undefined);
  await assert.rejects(begin(),/正在进行/);
  assert.equal(r._observeNativeImprovement({fence:{...fence,target:'note'},result}),false);
  assert.equal(r._observeNativeImprovement({fence,result}),true);
  assert.equal(r._draftState.drafts.back,'新答案');
  assert.equal(r._observeNativeImprovement({fence,result}),false);
  const next = await begin({id:'operation-2'});
  r._queueRequestEpoch++;
  assert.equal(r._observeNativeImprovement({fence:next,result}),false);
});

test('draft operations wait for an earlier rating and stop if its save fails or the card changes',async()=>{
  const {r,begin} = fixture();
  r._stagedRating = {id:'previous'};
  await assert.rejects(begin(),/评分尚未保存/);
  assert.equal(r._draftState,null);
  r._commitStagedRating=async()=>{r._stagedRating=null;return true;};
  let release; r._cacheWriteChain=new Promise(resolve=>{release=resolve;});
  const waiting=begin(); await Promise.resolve(); r._queueRequestEpoch++; release();
  await assert.rejects(waiting,/评分尚未保存/);
  assert.equal(r._draftState,null);
});

test('explicit draft confirmation preserves unknown write receipts and rejects another target',async()=>{
  const {r,begin,result} = fixture();
  r._observeNativeImprovement({fence:await begin(),result});
  const commit={id:'operation-2',key:'commitDraft',draftId:'draft',confirmed:true};
  await assert.rejects(begin({...commit,confirmed:false}),/确认/);
  await assert.rejects(begin({...commit,draftId:'old'}),/确认/);
  await assert.rejects(begin({...commit,target:'note'}),/确认/);
  const fence=await begin(commit);
  assert.equal(fence.draftLease,'operation-1');
  assert.equal(r._commitState.anki.busy,true);
  assert.equal(r._observeNativeImprovement({fence,result:{...result,commits:{anki:{ok:false,busy:false,unknown:true,message:'待核实'}}}}),true);
  await assert.rejects(begin(commit),/未重复提交/);
  assert.equal(r._commitState.anki.unknown,true);
});

test('native failure clears only the matching pending preview and permits a fresh preparation',async()=>{
  const {r,begin,result} = fixture();
  const fence=await begin();
  assert.equal(r._observeNativeImprovement({fence,error:'没有可用回答'}),true);
  assert.equal(r._draftState.busy,false);
  const next=await begin({id:'operation-2'});
  assert.equal(r._observeNativeImprovement({fence,error:'late failure'}),false);
  assert.equal(r._draftState.busy,true);
  assert.equal(r._observeNativeImprovement({fence:next,result}),true);
});

function lifecycle() {
  const {r} = fixture();
  Object.assign(r,{_mounted:true,_mode:false,_nativeLifecycleFence:null,_queue:[],_idx:0,_scopeMode:'current',
    _rejectedAnswers:{},_legacyCacheGet:async()=>null,_cardRepository:()=>({}),_bindCardRepository(){},
    _rememberAndDeactivateSelections(){},_invalidateCardRequests(){},_activateCurrentSelections(){},_scheduleDecorate(){},
    _acceptNativeReviewState(state){r._nativeQueuePresentation=state;},
    _applyQueueSnapshot(snapshot){r._queue=snapshot.cards;r._idx=snapshot.index;},
    loadQueue(){assert.fail('mode echo must not trigger another load');}});
  r._rejectedForContext=key=>r._rejectedAnswers[key]||[];
  vm.runInContext(source.slice(source.indexOf('  async function _prepareNativeLifecycle('),source.indexOf('  async function _loadNativeQueue(')),r);
  vm.runInContext(source.slice(source.indexOf('  function setMode(on)'),source.indexOf('  function injectCss()')),r);
  r._notifyAssistant=()=>r.setMode(r._mode); // Actual assistant mode-changed echo.
  const begin=(extra={})=>r._prepareNativeLifecycle({id:'load-1',enabled:true,scope:'all',contextKey:'book',...extra});
  const result=(id='load-1')=>({result:{request:id,snapshot:{cards:[{id:'card'}],index:0,client_context_key:'book'},kind:'local'},
    state:{lease:id,revision:1,queueIds:['card']}});
  return {r,begin,result};
}

test('native mode entry observes one acquisition; echoed mode changes cannot reload the queue',async()=>{
  const {r,begin,result}=lifecycle();
  const prepared=await begin();
  assert.equal(r._mode,true);assert.equal(r._queueBusy,true);
  assert.equal(r._observeNativeLifecycle({fence:prepared.fence,...result()}),true);
  assert.equal(r._queueBusy,false);assert.equal(r._queue[0].id,'card');
  assert.equal(r._observeNativeLifecycle({fence:prepared.fence,...result()}),false);
  const exited=await begin({id:'exit',enabled:false});
  assert.equal(r._mode,false);
  assert.equal(r._observeNativeLifecycle({fence:exited.fence}),true);
});

test('mode exit supersedes a slow load and a rejected rating prevents entry',async()=>{
  const {r,begin,result}=lifecycle();
  r._stagedRating={card:'pending'};
  await assert.rejects(begin(),/评分尚未保存/);
  assert.equal(r._mode,false);assert.equal(r._queueBusy,false);
  r._stagedRating=null;
  const old=await begin(), exit=await begin({id:'exit',enabled:false});
  assert.equal(r._observeNativeLifecycle({fence:old.fence,...result()}),false);
  assert.equal(r._queue.length,0);
  assert.equal(r._observeNativeLifecycle({fence:exit.fence}),true);
});

test('native queue failure clears loading without fallback and preserves unconsumed rejected scores',async()=>{
  const {r,begin,result}=lifecycle();
  const old={card:{id:7},original_index:0}, newer={card:{id:8},original_index:0};
  r._rejectedAnswers.book=[old];
  const first=await begin();r._rejectedAnswers.book.push(newer);
  assert.equal(r._observeNativeLifecycle({fence:first.fence,...result(),rejectedCards:first.rejectedCards}),true);
  assert.deepEqual(r._rejectedAnswers.book,[newer]);
  const next=await begin({id:'load-2'});
  assert.equal(r._observeNativeLifecycle({fence:next.fence,error:'offline'}),true);
  assert.equal(r._queueBusy,false);assert.match(r._presentationNotice,/offline/);
  assert.deepEqual(r._rejectedAnswers.book,[newer]);
});

test('App mode events route to the native owner without building a queue request in JavaScript',async()=>{
  const {r}=lifecycle();
  r.window.__BW_NATIVE_REVIEW_CONTROL__=true;
  r._nativeControlEpoch=0;
  const calls=[];
  r._nativeQueueCall=async(operation,values)=>{calls.push([operation,values]);return true;};
  r._toast=()=>{};
  vm.runInContext(source.slice(source.indexOf('  async function _dispatchNativeControl('),source.indexOf('  async function _prepareNativeLifecycle(')),r);
  assert.equal(await r.setMode(true),true);
  assert.deepEqual(JSON.parse(JSON.stringify(calls)),[['control',{enabled:true,scope:'current',force:false}]]);
  assert.equal(r._mode,false,'a request alone is not an observed mode switch');
});
