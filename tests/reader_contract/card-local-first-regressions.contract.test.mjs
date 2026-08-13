import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), 'utf8');
const VOICE = read('_server_deploy/static/pdf/rc-voicecall.js');
const FLASH = read('_server_deploy/static/pdf/rc-flashcard.js');
const MCP = read(
  'extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderContextMcpServer.cs',
);
const OUTPUT = read(
  'extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderRealtimeOutput.cs',
);
const LOCAL_ANKI = read(
  'extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderLocalAnki.cs',
);

function jsFunction(source, name, nextName) {
  const start = source.indexOf(`function ${name}(`);
  const end = source.indexOf(`function ${nextName}(`, start + 1);
  assert.ok(start >= 0 && end > start, `${name} function must be present`);
  return source.slice(start, end).trim();
}

test('ordinary card drafts carry no invented current-page provenance', () => {
  const mode = Function(
    `"use strict"; return (${jsFunction(VOICE, '_readerDraftSourceMode', '_readerOutputScroller')});`,
  )();
  assert.equal(mode({ cards: [{ front: 'Q', back: 'A' }] }), 'generic');
  assert.equal(
    mode({
      file: 'book.pdf',
      target: { kind: 'pdf', page: 2 },
      sourceText: 'verbatim',
    }),
    'exact',
  );
  assert.throws(
    () => mode({ file: 'book.pdf', cards: [] }),
    /BW_READER_ANKI_DRAFT_SOURCE_PARTIAL/,
  );

  const sourceFor = Function(
    `"use strict"; return (${jsFunction(VOICE, '_toolCardRepositorySource', '_applyCardSourceHighlight')});`,
  )();
  const generic = sourceFor('card_abcd', { text: 'generated material' }, 'make_anki');
  assert.equal(generic.kind, 'reader-tool-card-draft');
  assert.equal(generic.context, 'generated material');
  assert.equal('documentId' in generic, false);
  assert.equal('location' in generic, false);
  assert.equal('quote' in generic, false);

  const exact = sourceFor('card_abcd', {
    source_highlight: {
      file: 'book.pdf',
      target: { kind: 'pdf', page: 2 },
      text: 'verbatim',
    },
  }, 'make_anki');
  assert.equal(exact.kind, 'reader-book-exact-card-draft');
  assert.equal(exact.documentId, 'book.pdf');
  assert.deepEqual(exact.location, { kind: 'pdf', page: 2 });
  assert.equal(exact.quote, 'verbatim');
});

test('Windows draft protocol accepts cards-only but keeps exact source all-or-none', () => {
  assert.match(MCP, /required = includeCards[\s\S]*new JsonArray\("cards"\)/);
  assert.match(MCP, /schema\["dependentRequired"\][\s\S]*\["file"\][\s\S]*"target", "sourceText"/);
  assert.match(MCP, /bool genericAnki = kind == "anki-draft"[\s\S]*actual\.SetEquals\(new\[\] \{ "cards" \}\)/);

  assert.match(OUTPUT, /bool exactSource = hasFile && hasTarget && hasSourceText/);
  assert.match(OUTPUT, /if \(\(hasFile \|\| hasTarget \|\| hasSourceText\) && !exactSource\)/);
  assert.match(OUTPUT, /else[\s\S]*Exact\(root, "draftId", "cards"\)/);

  assert.match(LOCAL_ANKI, /bool exactSource = hasFile && hasTarget && hasSourceText/);
  assert.match(LOCAL_ANKI, /: new JsonObject\(\)/);
  assert.match(LOCAL_ANKI, /bool genericSource = !hasFile && !hasSourceText && !hasTarget/);
  assert.match(LOCAL_ANKI, /if \(string\.IsNullOrWhiteSpace\(registered\.File\)\)[\s\S]*return ""/);
});

test('Reader local confirmation acknowledges the atomic local write before projections', () => {
  const finish = jsFunction(
    FLASH,
    'finishRepositoryConfirmation',
    'failRepositoryConfirmation',
  );
  assert.match(finish, /_stateSync\(st, i\);/);
  assert.match(finish, /RC\.toast && RC\.toast\('✓ 已保存到 Reader 本地卡库'\);/);
  assert.doesNotMatch(finish, /return _stateSync/);
  assert.doesNotMatch(finish, /_stateSync\(st, i\)\.then/);

  const confirm = jsFunction(FLASH, 'addToAnki', 'removeDraft');
  assert.match(confirm, /repo\.saveConfirmedCard\(/);
  assert.match(confirm, /return finishRepositoryConfirmation\(/);
  assert.doesNotMatch(confirm, /addLocalAnkiCard|exportToComputerAnki|\/pdf\/api\/anki/);
});
