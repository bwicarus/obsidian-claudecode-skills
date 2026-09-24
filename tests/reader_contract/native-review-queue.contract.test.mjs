import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import vm from 'node:vm';
const require = createRequire(import.meta.url);
const Native = require('../../_server_deploy/static/reader-runtime/native-store.js');
const Repository = require('../../_server_deploy/static/reader-runtime/card-repository.js');
const source = readFileSync(new URL('../../_server_deploy/static/pdf/rc-review.js', import.meta.url), 'utf8');
const load = source.slice(source.indexOf('  async function _loadLocalReviewQueue'), source.indexOf('  function _cardRequestCurrent'));

test('native navigation changes the visible card only after persistence; errors and stale replies preserve selection', async () => {
  const code=source.slice(source.indexOf('  function _selectNativeCard('),source.indexOf('  function _selectCard('));
  for (const scenario of ['success','failed','stale']) {
    let finish, fail, calls=0;
    const result=new Promise((resolve,reject)=>{finish=resolve;fail=reject;});
    const cards=[{id:'a'},{id:'b'}], sideEffects=[];
    const r={_nativeNavigationWork:null,_nativeStageWork:null,_stagedRating:null,_queue:cards,_queueBusy:false,_ratingCommitBusy:0,
      _queueRequestEpoch:1,_contextCacheKey:'context',_nativeQueueLease:'lease',_idx:0,_showingAnswer:true,_improveExpanded:true,
      _cacheWriteChain:Promise.resolve(),_stableCardId:x=>x.id,_commitStagedRating:async()=>false,
      _current:()=>cards[r._idx],_currentQueueSnapshot:()=>({cards,index:r._idx}),
      _nativeQueueCall:async(operation,input)=>{calls++;assert.equal(operation,'selectCard');assert.equal(input.target.id,'b');return result;},
      _rememberAndDeactivateSelections:()=>sideEffects.push('release'),_invalidateCardRequests(){},render(){},_activateCurrentSelections(){},
      _scheduleDecorate(){},_notifyAssistant(){},_publishPresentation(){},_toast(){}};
    vm.createContext(r);vm.runInContext(code,r);
    const work=r._selectNativeCard(1,'native');
    assert.equal(r._selectNativeCard(1,'native'),work);
    await new Promise(resolve=>setImmediate(resolve));
    assert.equal(calls,1);assert.equal(r._idx,0);assert.equal(sideEffects.length,0);
    if(scenario==='failed') fail(new Error('disk full'));
    else {if(scenario==='stale') r._queueRequestEpoch++;finish({changed:true,snapshot:{index:1}});}
    assert.equal(await work,scenario==='success');
    assert.equal(r._idx,scenario==='success'?1:0);assert.equal(sideEffects.length,scenario==='success'?1:0);
    if(scenario==='failed') assert.match(r._presentationNotice,/disk full/);
  }
});

test('native queue crosses once with a bounded batch and no whole-library read', async () => {
  const prepared = { hasLocalCards: true, dueTotal: 1, entries: [{
    record: { id: 'card_aabb', entityRev: 2, stateRev: 3, source: { kind: 'book' } },
    card: { type: 'basic', front: '題', back: '答' }, state: { phase: 'confirmed' }, cardIndex: 4, due: true
  }] };
  const calls = [];
  let fail = false;
  const store = Native.createNativeDataStore({ deviceId: 'queue-test', port: {
    read() { assert.fail('no web record read'); }, listCollection() { assert.fail('no full collection read'); },
    commit() { assert.fail('queue reads must not write'); },
    async cardRepositoryCall(operation, args, device) {
      calls.push({ operation, args, device });
      if (fail) throw Error('native unavailable');
      return { result: prepared, changes: [] };
    }
  } });
  const repo = Repository.createCardRepository({ store });
  const context = vm.createContext({ _cardRepository: () => repo, _bindCardRepository() {},
    _localReviewCard: (record, card, state, index, due) => ({ id: record.id + ':' + index, front: card.front, due }) });
  vm.runInContext(load, context);
  const result = await context._loadLocalReviewQueue(10);
  assert.equal(result.cards[0].id, 'card_aabb:4');
  assert.equal(result.cards[0].front, '題');
  assert.equal(result.dueTotal, 1);
  assert.deepEqual(calls, [{ operation: 'reviewQueue', args: [{ limit: 10 }], device: 'queue-test' }]);
  fail = true;
  await assert.rejects(context._loadLocalReviewQueue(10), /native unavailable/);
  store.close();
});

test('browser fallback retains local due-before-new ordering', async () => {
  const record = { id: 'card_aabb', cards: [{ front: 'new' }, { front: 'due' }], states: {
    0: { phase: 'confirmed', review: { status: 'new' } },
    1: { phase: 'confirmed', review: { status: 'review', dueAt: 1 } }
  } };
  const context = vm.createContext({
    _cardRepository: () => ({ reviewQueue: async () => null, snapshot: async () => [record] }),
    _bindCardRepository() {}, _localReviewCard: (r, card) => card
  });
  vm.runInContext(load, context);
  const result = await context._loadLocalReviewQueue(10);
  assert.equal(result.cards[0].front, 'due');
  assert.equal(result.cards[1].front, 'new');
});
