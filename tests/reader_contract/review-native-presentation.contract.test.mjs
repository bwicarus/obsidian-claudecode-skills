import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../../_server_deploy/static/pdf/rc-review.js', import.meta.url), 'utf8');
const block = source.slice(source.indexOf('  function _presentationState()'), source.indexOf('  RC.review = {'));

function fixture() {
  const calls = [];
  const card = { id: '123', front: '<ruby>語<rt>ご</rt></ruby>'.repeat(200), back: '完整答案', entity_id: 'original' };
  const state = {
    _mode: true, _contextCacheKey: 'book-a', _scopeMode: 'current',
    _queueBusy: false, _idx: 0, _queue: [card], _dueTotal: 4, _relatedTotal: 2,
    _showingAnswer: false, _cardExpanded: true, _stagedRating: null, _ratingCommitBusy: 0, _nativeStageWork: null, _nativeNavigationWork: null,
    _improveExpanded: false, _improveMode: 'verbose', _draftState: null, _commitState: {}, _presentationNotice: '',
    _stableCardId: c => 'anki_card_' + c.id,
    _legacyReviewNoteId: c => c?.note_id ?? null,
    _cardForAssistant: c => ({ front: c.front, back: c.back, entity_id: c.entity_id }),
    selectedPairs: () => [{ answer: '保留选择', selection_ids: ['answer-1'] }],
    _publishPresentation: () => {},
    _commitStagedRating: reason => { calls.push(['flush', reason]); return Promise.resolve(); },
    loadQueue: async () => { calls.push(['load']); },
    _selectCard: index => { state._idx = index; calls.push(['select', index]); },
    _openSource: () => calls.push(['source']), render: () => {},
    _invalidateCardRequests: () => calls.push(['invalidate']),
    _prepareDraft: async target => calls.push(['prepare', target]),
    _commitDraft: async (target, confirmation) => calls.push(['commit', target, confirmation]),
  };
  state._current = () => state._queue[state._idx] || null;
  state._cardKey = c => state._stableCardId(c);
  state.setMode = enabled => { state._mode = enabled; };
  state._showAnswer = () => { state._showingAnswer = true; };
  state._answerCurrent = ease => { state._stagedRating = { card, ease }; state._queue = []; state._showingAnswer = false; };
  state._undoStagedRating = () => {
    if (!state._stagedRating) return false;
    state._queue = [state._stagedRating.card]; state._stagedRating = null; return true;
  };
  vm.createContext(state);
  vm.runInContext(block, state);
  const run = (key, values = {}) => state._performNativeInteraction({ contextKey: 'book-a', cardId: state._current() ? 'anki_card_123' : '', key, ...values });
  return { state, card, calls, run };
}

test('native review presentation retains full original faces and never lends mutable storage', () => {
  const { state, card } = fixture();
  state._draftState = { drafts: { cards: [{ front: '草稿原件' }] } };
  const value = state._presentationState();
  assert.equal(value.current.front, card.front);
  assert.ok(value.current.front.length > 2000);
  value.current.front = '不得回写';
  value.queueIds.pop();
  value.draft.drafts.cards[0].front = '不得回写';
  assert.notEqual(card.front, '不得回写');
  assert.equal(state._queue.length, 1);
  assert.equal(state._draftState.drafts.cards[0].front, '草稿原件');
});

test('stale book and card commands cannot touch a scheduler, source or draft', async () => {
  const { calls, run } = fixture();
  await assert.rejects(run('source', { contextKey: 'book-b' }), /切换/);
  await assert.rejects(run('prepareDraft', { cardId: 'anki_card_other', target: 'anki' }), /已变化/);
  await assert.rejects(run('rate', { ease: 3 }), /不能评分/);
  assert.deepEqual(calls, []);
});

test('native rating still stages through original owner and can undo before external commit', async () => {
  const { run, calls } = fixture();
  await run('reveal');
  const rated = await run('rate', { ease: 3 });
  assert.equal(rated.state.ratingStaged.cardId, 'anki_card_123');
  assert.equal(rated.state.canUndo, true);
  assert.equal(rated.state.count, 0);
  assert.deepEqual(calls, []);
  const undone = await run('undo');
  assert.equal(undone.state.current.id, 'anki_card_123');
  assert.equal(undone.state.canUndo, false);
});

test('native draft confirmation is bound to the exact draft and allowed target', async () => {
  const { state, run, calls } = fixture();
  state._draftState = { ok: true, draft_id: 'draft-1', targets: ['anki'] };
  await assert.rejects(run('commitDraft', { draftId: 'draft-1', target: 'anki' }), /确认/);
  await assert.rejects(run('commitDraft', { draftId: 'old-draft', target: 'anki', confirmed: true }), /确认/);
  await assert.rejects(run('commitDraft', { draftId: 'draft-1', target: 'note', confirmed: true }), /确认/);
  assert.deepEqual(calls, []);
  await run('commitDraft', { draftId: 'draft-1', target: 'anki', confirmed: true });
  assert.deepEqual(JSON.parse(JSON.stringify(calls)), [
    ['flush', 'commit-draft'],
    ['commit', 'anki', { target: 'anki', draftId: 'draft-1', cardKey: 'anki_card_123' }],
  ]);
});
