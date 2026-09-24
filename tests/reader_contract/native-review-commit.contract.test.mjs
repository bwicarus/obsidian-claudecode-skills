import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import vm from 'node:vm';
const require = createRequire(import.meta.url);
const Native = require('../../_server_deploy/static/reader-runtime/native-store.js');
const Repository = require('../../_server_deploy/static/reader-runtime/card-repository.js');
const source = readFileSync(new URL('../../_server_deploy/static/pdf/rc-review.js', import.meta.url), 'utf8');
const commit = source.slice(source.indexOf('  function _commitLocalRating('), source.indexOf('  function _restoreRejectedAnswer('));

test('App scoring sends only its stage identity and observes the native result without scheduling or another event submission', async () => {
  const stage={nativeStageID:'one',nativeQueueLease:'lease',contextKey:'book',pendingKey:'p',ease:3,
    card:{_localReview:{gid:'group',cardIndex:0}}};
  const calls=[], events=[];
  const r={window:{RC:{},dispatchEvent:e=>events.push(e)},RC:{},CustomEvent:class {constructor(type){this.type=type;}},
    _nativeScoreObserved:new Set(),_mode:true,_stagedRating:stage,_ratingCommitBusy:0,_nativeRatingCommitWork:null,
    _contextCacheKey:'book',_nativeQueueLease:'lease',_ratingPending:{p:true},
    _nativeQueueCall:async(...args)=>{calls.push(args);return {ok:true,stage,local:true,
      record:{id:'group'},event:{aid:'native-review:one',reviewedAt:10},snapshot:{cards:[]},state:{}};},
    _rememberAndDeactivateSelections(){},_invalidateCardRequests(){},_applyQueueSnapshot(){},_acceptNativeReviewState(){},
    _patchSharedCard(){},render(){},_activateCurrentSelections(){},_scheduleDecorate(){},_notifyAssistant(){},_publishPresentation(){},_toast(){},
    _commitLocalRating:()=>assert.fail('web scheduler must not run'),_reportReviewEvent:()=>assert.fail('event already in native outbox')};
  vm.createContext(r);vm.runInContext(source.slice(source.indexOf('  function _commitNativeScore('),source.indexOf('  function _commitStagedRating(')),r);
  assert.equal(await r._commitNativeScore(stage),true);
  assert.deepEqual(JSON.parse(JSON.stringify(calls)),[['commitRating',{lease:'lease',stageId:'one'}]]);
  assert.equal(r._ratingPending.p,undefined);assert.equal(r._ratingCommitBusy,0);assert.equal(events.length,1);
  assert.equal(await r._commitNativeScore(stage),true);assert.equal(events.length,1,'receipt replay published the effect twice');
  r._nativeQueueCall=async()=>{throw Error('lost bridge reply');};
  assert.equal(await r._commitNativeScore(stage),false);
  assert.equal(r._stagedRating,stage,'retry must keep the original operation id');
});

function harness(repository) {
  const events = [];
  const context = vm.createContext({ _cardRepository: () => repository, _answerAid: () => 'answer-1',
    _ratingPending: { test: true }, _contextCacheKey: 'current', RC: {},
    window: { UP_FILE: 'book', dispatchEvent() { events.push('notify'); } },
    CustomEvent: class {}, _restoreCommittedStage() { events.push('restore'); }, _toast() {},
    _reportReviewEvent() { events.push('report'); }, _projectLegacyLocalAnswer() { events.push('project'); },
    _scheduledLocalReview() { events.push('schedule'); return { reps: 1 }; }
  });
  vm.runInContext(commit, context);
  return { events, run: () => context._commitLocalRating({ pendingKey: 'test', contextKey: 'previous', ease: 3,
    card: { _localReview: { gid: 'card_aabb', cardIndex: 2, entityRev: 4, stateRev: 5, review: {} } } }) };
}
test('native rating commits once before event publication, without browser scheduling', async () => {
  const calls = [];
  const store = Native.createNativeDataStore({ deviceId: 'ratings', port: {
    read() { assert.fail('native transaction owns reads'); },
    listCollection() { assert.fail('rating must not enumerate browser data'); },
    commit() { assert.fail('native transaction owns writes'); },
    async cardRepositoryCall(operation, args) {
      calls.push({ operation, args });
      assert.equal(h.events.length, 0);
      return { result: { states: { 2: { review: { reps: 1 } } } }, changes: [] };
    }
  } });
  const repo = Repository.createCardRepository({ store });
  const h = harness(repo);
  assert.equal(await h.run(), true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].operation, 'commitReview');
  assert.equal(calls[0].args[0].entityRev, 4);
  assert.equal(calls[0].args[0].stateRev, 5);
  assert.equal(calls[0].args[1].mutationId, 'review:card_aabb:2:answer-1');
  assert.deepEqual(h.events, ['report', 'project', 'notify']);
  store.close();
});
test('native rejection restores rating without another write or reporting success', async () => {
  const h = harness({ commitReview: async () => { throw Error('stale card'); },
    patchState: () => assert.fail('must not fall back after native failure') });
  assert.equal(await h.run(), false);
  assert.deepEqual(h.events, ['restore']);
});
test('browser null owner retains existing scheduler and reports only committed rating', async () => {
  const h = harness({ commitReview: async () => null, patchState: async () => {
    assert.deepEqual(h.events, ['schedule']);
    return { states: { 2: { review: { reps: 1 } } } };
  } });
  assert.equal(await h.run(), true);
  assert.deepEqual(h.events, ['schedule', 'report', 'project', 'notify']);
});

test('native interval refinement passes the committed review to the atomic owner and never browser-patches on failure', async () => {
  const calls = [], notices = [];
  const code = source.slice(source.indexOf('  function _adoptExternalSchedule('), source.indexOf('  function _projectLegacyLocalAnswer('));
  const local = { gid: 'card_aabb', cardIndex: 2, entityRev: 9, review: { reps: 4, lapses: 1, lastReviewedAt: 6000 } };
  let fail = false;
  const store = Native.createNativeDataStore({ deviceId: 'refine', port: {
    read() { assert.fail('no browser read'); }, listCollection() { assert.fail('no enumeration'); }, commit() { assert.fail('no browser write'); },
    async cardRepositoryCall(operation, args) {
      if (fail) throw Error('disk unavailable');
      calls.push({ operation, args }); return { result: { applied: true }, changes: [] };
    }
  } });
  const repo = Repository.createCardRepository({ store });
  const context = vm.createContext({ _nativeReviewUI: () => true, _cardRepository: () => repo,
    _toast: message => notices.push(message), _externalScheduleFrom() { assert.fail('native owner computes the interval'); } });
  vm.runInContext(code, context);
  await context._adoptExternalSchedule(local, { interval: -600 }, 6000, 'answer-a');
  assert.equal(calls[0].operation, 'adoptReviewSchedule');
  assert.equal(calls[0].args[0].expectedReview.reps, 4);
  assert.equal(calls[0].args[0].entityRev, 9);
  assert.equal(calls[0].args[1].mutationId, 'sched:card_aabb:2:answer-a');
  fail = true;
  await context._adoptExternalSchedule(local, { interval: 3 }, 6000, 'answer-b');
  assert.equal(calls.length, 1);
  assert.match(notices[0], /本地评分已保存.*disk unavailable/);
  store.close();
});
