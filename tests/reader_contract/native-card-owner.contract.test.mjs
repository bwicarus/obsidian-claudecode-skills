import assert from 'node:assert/strict';
import test from 'node:test';
import { createRequire } from 'node:module';
const require = createRequire(import.meta.url);
const Native = require('../../_server_deploy/static/reader-runtime/native-store.js');
const Repository = require('../../_server_deploy/static/reader-runtime/card-repository.js');

test('native card commands cross once and publish committed changes without replaying web writes', async () => {
  const calls = [], notifications = [];
  const saved = { id: 'card_aabb', cid: 'card_aabb', gid: 'card_aabb', contract: 'card-repository/1', cards: [], states: {} };
  const store = Native.createNativeDataStore({ deviceId: 'native-test', port: {
    read() { throw Error('web must not read records to execute a native card command'); },
    commit() { throw Error('web must not commit a native card command twice'); },
    async cardRepositoryCall(operation, args, device) {
      calls.push({ operation, args, device });
      return { result: saved, changes: operation === 'load' ? [] : [
        { collection: 'card-entities', record: { id: saved.id, rev: 1 } },
        { collection: 'card-states', record: { id: saved.id, rev: 1 } }
      ] };
    }
  } });
  const repo = Repository.createCardRepository({ store });
  const off = repo.subscribe(event => notifications.push(event));
  const input = { gid: saved.id, cards: [{ type: 'basic', front: 'before', back: 'answer' }] };
  const pending = repo.registerDraft(input);
  input.cards[0].front = 'mutated after submit';
  assert.deepEqual(await pending, saved);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls[0].operation, 'registerDraft');
  assert.equal(calls[0].args[0].cards[0].front, 'before');
  assert.equal(calls[0].device, 'native-test');
  assert.equal(calls.filter(call => call.operation === 'registerDraft').length, 1);
  assert.equal(notifications.length, 1);
  assert.deepEqual(notifications[0].record, saved);
  off();
  store.close();
  await assert.rejects(repo.load(saved.id), { code: 'BW_DATA_CLOSED' });
});

test('native card owner failures never fall back to a second web mutation', async () => {
  let nativeCalls = 0;
  const store = Native.createNativeDataStore({ port: {
    read() { throw Error('fallback read'); }, commit() { throw Error('fallback commit'); },
    cardRepositoryCall() { nativeCalls++; const error = Error('original conflict'); error.code = 'BW_CARD_REPOSITORY_CONFLICT'; return Promise.reject(error); }
  } });
  const repo = Repository.createCardRepository({ store });
  await assert.rejects(repo.saveConfirmedCard({ gid: 'card_aabb' }), { code: 'BW_CARD_REPOSITORY_CONFLICT' });
  assert.equal(nativeCalls, 1);
});
