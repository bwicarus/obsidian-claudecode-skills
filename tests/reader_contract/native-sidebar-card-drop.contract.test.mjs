import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const script = readFileSync(new URL('../../ios/BWReader/App/ReaderNativeConversationScript.swift', import.meta.url), 'utf8').replace(/\r\n/g, '\n');
const start = script.indexOf('          const originalCard = target.inspect?.().content;');
const end = script.indexOf('\n        }\n        message.parts', start);
assert.ok(start > 0 && end > start);
const register = vm.runInNewContext('(function(part,node,target,group,body,cardElement){' + script.slice(start, end) + '\n})', {
  rc: () => runtime, innerWidth: 1000, innerHeight: 800,
  registerAction: (id, node, callback) => { action = callback; return id; },
});
let action, runtime;
const point = {x: 0, y: 0, value: {page: 44, x: 0.3, y: 0.4}};

test('native information card drops without any web body, using the full current original', async () => {
  const calls = [];
  let card = {kind: 'fact', cid: 'same-card', data: {answer: 'old'}};
  runtime = {voiceCard: {placementSnapshot: card => ({cid: card.cid, content: card.data.answer, isHtml: false})},
    stickynote: {placeHtmlAt: async (...args) => {calls.push(args); return true;}}};
  const part = {id: 'p-1', title: 'title', text: 'truncated', data: {}};
  action = null;
  register(part, {}, {inspect: () => ({content: card})}, null, null, null);
  assert.ok(part.data.dragId && action);
  card = {...card, data: {answer: 'complete '.repeat(8000)}};
  await action(point);
  assert.equal(calls.length, 1);
  assert.equal(calls[0][2].content, card.data.answer);
  assert.equal(calls[0][2].cid, 'same-card');
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0][3])), {kind: 'pdf', ...point.value});
});

test('Anki drop keeps its gid and complete live card snapshot', async () => {
  const cards = [{front: '表', back: '裏', nodeIds: ['n1'], anki: {noteId: 31}, _st: 'learn'}];
  const group = {__fc: {gid: 'g1'}}, calls = [];
  runtime = {flashcard: {snapshot: current => {assert.equal(current, group); return cards;}},
    stickynote: {placeCardAt: async (...args) => {calls.push(args); return true;}}};
  register({id: 'anki', data: {}}, {}, {inspect: () => ({content: {gid: 'g1', card: cards[0]}})}, group, null, null);
  await action(point);
  assert.equal(calls[0][2], cards);
  assert.equal(calls[0][3], 'g1');
});

test('rejected persistence reports failure instead of reporting a successful drop', async () => {
  runtime = {voiceCard: {placementSnapshot: () => ({content: 'x'})}, stickynote: {placeHtmlAt: async () => false}};
  register({id: 'failed', data: {}}, {}, {inspect: () => ({content: {kind: 'fact', cid: 'c'}})}, null, null, null);
  await assert.rejects(action(point), /未保存/);
  await assert.rejects(action({...point, value: {page: 1, x: -1, y: 0}}), /落点无效/);
});
