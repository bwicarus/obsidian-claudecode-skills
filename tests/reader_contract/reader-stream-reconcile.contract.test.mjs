import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const root = new URL('../../', import.meta.url);
const turns = readFileSync(new URL('_server_deploy/static/pdf/rc-turncard.js', root), 'utf8');
const assistant = readFileSync(new URL('_server_deploy/static/pdf/rc-assistant.js', root), 'utf8');
const events = assistant.slice(assistant.indexOf('  var _liveSeen = {};'), assistant.indexOf('  // ⚠ 就地导出:onHistoryEvent'));

class Node {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.parentNode = null; this.attrs = {};
    this.style = {}; this.dataset = {}; this.hidden = false; this._text = ''; this.className = '';
    this.classList = { add() {}, remove() {}, contains() { return false; }, toggle() {} };
  }
  get isConnected() { return this.root === true || !!this.parentNode?.isConnected; }
  get firstChild() { return this.children[0] || null; }
  appendChild(child) { child.remove(); this.children.push(child); child.parentNode = this; return child; }
  insertBefore(child, before) { child.remove(); const i = this.children.indexOf(before); this.children.splice(i < 0 ? this.children.length : i, 0, child); child.parentNode = this; return child; }
  removeChild(child) { this.children.splice(this.children.indexOf(child), 1); child.parentNode = null; }
  remove() { this.parentNode?.removeChild(this); }
  replaceWith(child) { const parent = this.parentNode; if (!parent) return; parent.insertBefore(child, this); this.remove(); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return this.attrs[k] ?? null; }
  addEventListener() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  set textContent(text) { this._text = String(text); this.children.forEach(c => { c.parentNode = null; }); this.children = []; }
  get innerHTML() { return this.textContent; }
  set innerHTML(text) { this.textContent = text; }
}

function harness() {
  const thread = new Node(); thread.root = true;
  const stage = new Node(); stage.root = true;
  const calls = []; const timers = [];
  const document = { getElementById: () => thread, createElement: tag => new Node(tag), createTextNode: text => { const n = new Node(); n.textContent = text; return n; } };
  let renderCards = 0;
  const sandbox = {
    document, thread, console, Array, Object, String, Number, Math, JSON, isFinite,
    RC: { assistant: { renderMd: (node, text) => { node.textContent = text; } },
      toolChip: { flowBtn: () => new Node('button'), renderFlowInto: (node, chip) => { node.textContent = JSON.stringify(chip); } } },
    __vcInfoCardEl: card => { renderCards++; const n = new Node(); n.textContent = card.title; return n; },
    fetch: async (url, options) => { calls.push({ url, options }); return { ok: true, json: async () => ({ ok: true, messages: [] }) }; },
    setTimeout: fn => { timers.push(fn); return timers.length; }, clearTimeout() {},
    _assistantMode: 'normal', _modeEpoch: 1, _clearing: false, _historyReloadInFlight: null,
    _modeNorm: mode => mode === 'review' ? 'review' : 'normal', _historyUrl: () => '/history',
    _vTid: '', _vAnswered: false, _turnModes: {}, _psT: {}, _syncPartsNow() {},
  };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(turns, sandbox);
  const historyReader = assistant.slice(assistant.indexOf('    function _readHistory('), assistant.indexOf('  var streaming'));
  vm.runInContext(historyReader + events + '\nthis.event = onHistoryEvent;', sandbox);
  return { sandbox, tc: sandbox.RC.turnCard, thread, stage, calls, timers, event: sandbox.event, cardRenders: () => renderCards };
}
const message = (id, content, role = 'assistant', parts) => ({ turn_id: id, role, content, ...(parts ? { parts } : {}) });
const delta = (id, content, revision = 1, role = 'assistant', item = 'answer') => ({ turn_id: id, stream: 'delta', content, role, streamRevision: revision, item_id: item });
const final = (id, content, revision = 2, role = 'assistant', parts) => ({ turn_id: id, stream: 'final', streamRevision: revision, messages: [message(id, content, role, parts)] });

test('user final updates its existing node and trailing partial cannot regress it; no history GET', () => {
  const h = harness();
  h.event(delta('u1', '未完成', 1, 'user', 'mic1'));
  const node = h.tc.open('user:u1').el;
  h.event({ ...final('u1', '完整提问', 2, 'user'), messages: [{ ...message('u1', '完整提问', 'user'), item_id: 'mic1' }] });
  h.event(delta('u1', '迟到草稿', 3, 'user', 'mic1'));
  assert.equal(h.tc.open('user:u1').el, node);
  assert.equal(node.textContent, '完整提问');
  assert.equal(h.thread.children.length, 1);
  assert.equal(h.calls.filter(c => !c.options?.method || c.options.method === 'GET').length, 0);
});

test('another user final and assistant final preserve live generated card DOM and open tool flow', () => {
  const h = harness();
  h.event(delta('a1', '我正在查询', 1));
  h.tc.addPart('a1', { kind: 'card', origin: 'app', card: { cid: 'saved-card', title: '生成物' } });
  const cardNode = h.tc.open('a1').parts.find(p => p.kind === 'card')._el;
  h.event({ turn_id: 'a1', stream: 'parts', streamRevision: 2, messages: [message('a1', '', 'assistant', [
    { kind: 'tool', call_id: 'search1', tool: 'search', status: 'running', args: { q: 'a' } },
  ])] });
  h.tc.openFlow('a1');
  const turnNode = h.tc.open('a1').el;
  h.event(final('u2', '补充提问', 3, 'user'));
  h.event(final('a1', '完整回答', 4, 'assistant', [
    { kind: 'text', item_id: 'answer', text: '完整回答', seq: 0 },
    { kind: 'tool', call_id: 'search1', tool: 'search', status: 'completed', result: '查到', seq: 1 },
  ]));
  assert.equal(h.tc.open('a1').el, turnNode);
  assert.equal(h.tc.open('a1').parts.find(p => p.kind === 'card')._el, cardNode);
  assert.equal(cardNode.isConnected, true);
  assert.equal(h.tc.flowOpen('a1'), true);
  assert.equal(h.cardRenders(), 1);
  assert.equal(h.tc.open('a1').bd.textContent, '完整回答生成物');
  assert.equal(h.calls.some(c => c.url === '/history'), false);
});

test('intermediate parts do not seal text; stable invocation updates once while another invocation remains', () => {
  const h = harness();
  h.event(delta('a', '开始', 1));
  const tool = (id, status) => ({ kind: 'tool', call_id: id, tool: 'search', status, args: { q: 'same' } });
  h.event({ turn_id: 'a', stream: 'parts', streamRevision: 2, messages: [message('a', '', 'assistant', [tool('one', 'running')])] });
  h.event(delta('a', '继续生成', 3));
  h.event(final('a', '完成', 4, 'assistant', [{ kind: 'text', item_id: 'answer', text: '完成' }, tool('one', 'completed'), tool('two', 'completed')]));
  h.event(final('a', '完成', 4, 'assistant', [{ kind: 'text', item_id: 'answer', text: '完成' }, tool('one', 'completed'), tool('two', 'completed')]));
  assert.deepEqual(Array.from(h.tc.partsOf('a').filter(p => p.kind === 'tool').map(p => [p.call_id, p.status])), [['one', 'completed'], ['two', 'completed']]);
  assert.equal(h.tc.partsOf('a').filter(p => p.kind === 'text').length, 1);
});

test('finishing one item does not seal the next voice item in the same backend turn', () => {
  const h = harness();
  h.event(delta('a', '后台答复', 1, 'assistant', 'backend'));
  h.event({ ...final('a', '后台答复', 2), messages: [{ ...message('a', '后台答复'), item_id: 'backend' }] });
  h.event(delta('a', '补充语音', 3, 'assistant', 'voice2'));
  h.event(delta('a', '迟到后台草稿', 4, 'assistant', 'backend'));
  assert.equal(h.tc.open('a').bd.textContent, '后台答复补充语音');
  assert.deepEqual(Array.from(h.tc.partsOf('a').map(p => p.text)), ['后台答复']);
  h.event({ ...final('a', '补充语音完整', 5), messages: [{ ...message('a', '补充语音完整'), item_id: 'voice2' }] });
  assert.deepEqual(Array.from(h.tc.partsOf('a').map(p => p.text)), ['后台答复', '补充语音完整']);
});

test('a late final for an earlier item cannot freeze another item still streaming', () => {
  const h = harness();
  h.event(delta('a', '第一项草稿', 1, 'assistant', 'one'));
  h.event(delta('a', '第二项草稿', 2, 'assistant', 'two'));
  h.event({ ...final('a', '第一项完成', 3), messages: [{ ...message('a', '第一项完成'), item_id: 'one' }] });
  assert.deepEqual(Array.from(h.tc.partsOf('a').map(p => p.text)), ['第一项完成']);
  assert.equal(h.tc.open('a').bd.textContent, '第一项完成第二项草稿');
});

test('mixed-role snapshot finalizes only its completed user item while assistant remains live', () => {
  const h = harness();
  h.event({ ...delta('shared', '用户完整话', 1, 'user', 'mic'), origin: 'voice' });
  h.event(delta('shared', '正在生成的新正文', 1, 'assistant', 'backend'));
  h.event({ turn_id: 'shared', role: 'user', item_id: 'mic', origin: 'voice', stream: 'final', streamRevision: 2,
    messages: [
      { ...message('shared', '用户完整话', 'user'), item_id: 'mic', origin: 'voice', stream_final: true },
      { ...message('shared', '旧快照正文'), item_id: 'backend', origin: 'runner', streamRevision: 1, stream_final: false },
    ] });
  h.event(delta('shared', '正在生成的新正文和后半句', 2, 'assistant', 'backend'));
  assert.equal(h.tc.open('user:shared').bd.textContent, '用户完整话');
  assert.equal(h.tc.open('shared').bd.textContent, '正在生成的新正文和后半句');
  assert.equal(h.tc.partsOf('shared').length, 0, '用户final不能提交助手未完成草稿');
  assert.equal(h.thread.children.length, 2);
});

test('after reopening history, resumed deltas reuse the same historical turn node', () => {
  const h = harness();
  const historical = h.tc.renderTurn('hist-a', [{ kind: 'text', item_id: 'old', origin: 'runner', text: '旧答复' }], h.thread,
    { historyReplay: true, meta: { turnId: 'a' } });
  h.event(delta('a', '新语音', 1, 'assistant', 'new'));
  assert.equal(h.tc.open('a').el, historical);
  assert.equal(h.thread.children.length, 1);
  h.tc.addPart('a', { kind: 'card', origin: 'app', card: { cid: 'resumed-card', title: '卡片' } });
  assert.equal(h.tc.partsOf('a').some(p => p.kind === 'card'), true);
});

test('renaming a live draft into existing real turn cannot persist or duplicate its unfinished text', () => {
  const h = harness();
  h.tc.draftText('tmp', '半句');
  h.tc.open('real'); h.tc.rename('tmp', 'real');
  assert.equal(h.tc.partsOf('real').length, 0);
  h.tc.draftText('real', '完整句子'); h.tc.freezeDraft('real');
  assert.deepEqual(Array.from(h.tc.partsOf('real').map(p => p.text)), ['完整句子']);
});

test('stale revisions and old scope events cannot overwrite a newer or cleared turn', () => {
  const h = harness();
  h.event(delta('a', '最新', 10)); h.event(delta('a', '过时', 9));
  assert.equal(h.tc.open('a').bd.textContent, '最新');
  h.sandbox._modeEpoch++;
  h.event(final('a', '清空前旧结果', 11));
  assert.equal(h.tc.open('a').bd.textContent, '最新');
  h.event({ ...delta('review-only', '复习', 1), assistant_mode: 'review' });
  assert.equal(h.tc.has('review-only'), false);
});

test('a snapshot started before new stream text preserves its nodes instead of reverting them', () => {
  const h = harness();
  const before = h.tc.streamVersion();
  h.event(delta('a', '实时正文', 1));
  h.tc.addPart('a', { kind: 'card', card: { cid: 'generated', title: '卡片' }, origin: 'app' });
  h.event(final('a', '实时正文', 2, 'assistant', [{ kind: 'text', item_id: 'answer', text: '实时正文' }]));
  const live = h.tc.open('a').el;
  h.tc.renderTurn('history-a', [{ kind: 'text', text: '旧快照' }], h.stage, { historyReplay: true, meta: { turnId: 'a' } });
  h.tc.preserveLive(h.stage, before);
  assert.equal(h.stage.children.length, 1);
  assert.equal(h.stage.children[0], live);
  assert.equal(live.textContent.includes('实时正文卡片'), true);
  assert.equal(h.cardRenders(), 1);
});

test('late cards still use the completed source turn until the next start', () => {
  const h = harness();
  h.event({ turn_id: 'a', stream: 'start', streamRevision: 1 });
  h.event(final('a', '完成', 2));
  assert.equal(h.sandbox.__bwLiveTurnId, 'a');
  h.event({ turn_id: 'b', stream: 'start', streamRevision: 3 });
  assert.equal(h.sandbox.__bwLiveTurnId, 'b');
});

test('legacy invalidations only merge requested turn and leave unrelated live material untouched', async () => {
  const h = harness();
  h.event(delta('active', '仍在流式', 1)); const active = h.tc.open('active').el;
  h.sandbox.fetch = async (url, options) => { h.calls.push({ url, options }); return { ok: true, json: async () => ({ ok: true, messages: [message('old', '之前回答'), message('unrequested', '无关')] }) }; };
  h.event({ turn_id: 'old' }); h.timers.shift()();
  for (let i = 0; i < 8; i++) await Promise.resolve();
  assert.equal(active.isConnected, true);
  assert.equal(active.textContent, '仍在流式');
  assert.equal(h.tc.has('old'), true);
  assert.equal(h.tc.has('unrequested'), false);
});
