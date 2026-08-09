import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const CLIENT = readFileSync(
  new URL("_server_deploy/static/pdf/epub-html.js", ROOT),
  "utf8",
);
const SERVER = readFileSync(
  new URL("_server_deploy/epub_assistant.py", ROOT),
  "utf8",
);

test("native EPUB mutations show success only after local state and Pi metadata both commit", () => {
  assert.match(CLIENT, /function _nativeLocalEPUBMutationTransaction\(request\)/);
  assert.match(CLIENT, /window\.nativeLocalEPUBMutation = function \(request\)/);
  assert.match(CLIENT, /op: 'native_apply'[\s\S]*payload\.ok !== true[\s\S]*window\.notesReload\(\)[\s\S]*_epShowAction\(payload\.action\)/);
  assert.match(CLIENT, /function _epAttachActions\(batch\)[\s\S]*payload\.stored !== true/);
  assert.match(CLIENT, /_epAttachActions\(\[action\]\)\.then\(function \(\) \{[\s\S]*_epShowAction\(action\)/);
  assert.match(CLIENT, /\.catch\(function \(error\) \{[\s\S]*unapplyHl\(item\)[\s\S]*高亮未保存/);
});

test("Pi native action endpoint is metadata-only and CAS guarded", () => {
  assert.match(SERVER, /def _econvo_commit_native_action\(uid, file_rel, previous, action\):/);
  assert.match(SERVER, /if current != previous:[\s\S]*return "conflict"/);
  assert.match(SERVER, /if op == "native_commit":[\s\S]*_econvo_commit_native_action/);
  const nativeCommit = SERVER.slice(
    SERVER.indexOf('if op == "native_commit":'),
    SERVER.indexOf('if op in ("undo", "redo"):'),
  );
  assert.doesNotMatch(nativeCommit, /_action_undo|_action_redo|_epub_hl_edit|_notes_edit/);
});

test("native EPUB assistant reads the injected authority rather than Pi sidecars", () => {
  assert.match(SERVER, /def _native_epub_state\(ctx\):/);
  assert.match(SERVER, /set\(raw\) != \{"contract", "file", "revisions", "highlights", "notes", "ink"\}/);
  assert.match(SERVER, /hls = _ctx_epub_highlights\(ctx\)/);
  assert.match(SERVER, /_t_native_notes_query/);
  assert.match(SERVER, /_t_native_notes_read/);
  assert.match(SERVER, /写工具返回 pending 时，只能说已发出本机操作，不能说已经写入/);
});
