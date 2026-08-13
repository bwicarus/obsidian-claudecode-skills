import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), 'utf8');
const ASSISTANT = read('_server_deploy/assistant.py');
const VOICE = read('_server_deploy/voice.py');
const EPUB = read('_server_deploy/epub_assistant.py');
const CLIENT = read('_server_deploy/static/pdf/rc-voicecall.js');
const TURN_CARD = read('_server_deploy/static/pdf/rc-turncard.js');
const FLASHCARD = read('_server_deploy/static/pdf/rc-flashcard.js');

function sliceFunction(source, name, nextName) {
  const start = source.indexOf(`def ${name}(`);
  const end = source.indexOf(`\ndef ${nextName}(`, start + 1);
  assert.ok(start >= 0 && end > start, `${name} function must be present`);
  return source.slice(start, end);
}

test('synchronous make_anki returns a pure local-first draft', () => {
  const fn = sliceFunction(ASSISTANT, '_t_make_anki', '_t_make_note');
  assert.doesNotMatch(fn, /_entity_reg_cards\(/);
  assert.doesNotMatch(fn, /_mark_source_highlight\(/);
  assert.match(fn, /"source_ref": src/);
  assert.match(fn, /result\["source_highlight"\]/);
  assert.match(fn, /Reader 本地卡库/);
});

test('background make_anki does not register a Pi card before local persistence', () => {
  const fn = sliceFunction(VOICE, '_task_anki', '_task_vocab');
  assert.doesNotMatch(fn, /_entity_reg_cards\(/);
  assert.match(fn, /"source_ref": link\[:4096\]/);
  assert.match(fn, /result\["source_highlight"\]/);
  const epub = sliceFunction(EPUB, '_t_make_anki', '_t_make_note');
  assert.doesNotMatch(epub, /client_action|epubHighlight/);
});

test('source highlight is projected only after the local draft is durable', () => {
  assert.match(CLIENT, /function _applyCardSourceHighlight\(/);
  assert.match(CLIENT, /__bwReaderHighlightExactText\(/);
  const immediate = CLIENT.slice(
    CLIENT.indexOf('if (_sc) {'),
    CLIENT.indexOf('// 后台任务', CLIENT.indexOf('if (_sc) {')),
  );
  const persist = immediate.indexOf('RC.flashcard.presentDraft(_sc, _gid');
  const project = immediate.indexOf('_projectCardSourceHighlight(_sr, _gid)');
  assert.ok(persist >= 0 && project > persist);
  const background = CLIENT.slice(
    CLIENT.indexOf("if (stt === 'done'"),
    CLIENT.indexOf("} else if (stt === 'error')"),
  );
  const backgroundPersist = background.indexOf('RC.flashcard.presentDraft(_cds, _gid2');
  const backgroundProject = background.indexOf('_projectCardSourceHighlight(d.result, _gid2)');
  assert.ok(backgroundPersist >= 0 && backgroundProject > backgroundPersist);
});

test('bridge result cards are locally persisted before they become saveable', () => {
  const sanitize = sliceFunction(ASSISTANT, '_sanitize_ext_parts', '_reader_result_url');
  assert.doesNotMatch(sanitize, /_entity_reg_cards\(/);
  assert.match(sanitize, /"card_" \+ __import__\("uuid"\)\.uuid4\(\)\.hex/);
  assert.match(TURN_CARD, /function _localCardGid\(/);
  assert.match(TURN_CARD, /RC\.flashcard\.presentDraft\(_fcards, p\.gid/);
  assert.match(TURN_CARD, /kind: 'assistant-turn'/);
  assert.match(TURN_CARD, /draftId: _turnCardSourceId/);
  assert.match(FLASHCARD, /renderEntity\(options\.host \|\| null/);
});
