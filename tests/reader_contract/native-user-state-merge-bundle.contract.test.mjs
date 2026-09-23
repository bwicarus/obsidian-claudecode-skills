// Native migration keeps browser clients compatible through executable parity
// fixtures, without embedding a JS engine in the App's cloud merge path.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
const root = new URL('../../', import.meta.url);
const read = p => readFileSync(new URL(p, root), 'utf8').replace(/\r\n/g, '\n');
const swift = read('ios/BWReader/App/ReaderUserStateMerge.swift');
const workflow = read('.github/workflows/safari-extension-ios.yml');
const packager = read('ios/BWReader/package_local_reader.py');

test('App merge and geometry do not embed retired JS engines or assets', () => {
  assert.doesNotMatch(swift, /import JavaScriptCore|JSContext|objectForKeyedSubscript/);
  assert.match(swift, /ReaderNativeBookMerge\.merge/);
  assert.doesNotMatch(packager, /write_bytes\(root, "native\/(?:user-state-merge|pdf-selection-core)\.js"/);
  assert.match(packager, /contains retired JavaScriptCore asset/);
});
test('packaging runs compiled native merge against actual browser decisions', () => {
  assert.match(workflow, /NativeBookMerge\/fixtures\.mjs/);
  assert.match(workflow, /ReaderNativeBookMerge\.swift/);
  assert.match(workflow, /NativeBookMerge\/main\.swift/);
  const fixture = read('ios/BWReader/Tests/NativeBookMerge/fixtures.mjs');
  assert.match(fixture, /reference\.mergeDomain/);
  assert.match(fixture, /reference\.domainEmpty/);
});
test('invalid data does not silently pick a cloud side', () => {
  assert.match(swift, /try ReaderNativeBookMerge\.merge/);
  assert.doesNotMatch(swift, /try\? ReaderNativeBookMerge/);
  assert.match(swift, /options: \[\.sortedKeys, \.fragmentsAllowed\]/);
});
