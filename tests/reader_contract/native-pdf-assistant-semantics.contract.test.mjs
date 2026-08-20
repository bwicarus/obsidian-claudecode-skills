import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const RUNTIME = read("_server_deploy/static/pdf/native-local-runtime.js");
const ASSISTANT = read("_server_deploy/assistant.py");
const ASSISTANT_UI = read("_server_deploy/static/pdf/reader.src/25-assistant.js");
const PDF_ADAPTER = read("_server_deploy/static/pdf/reader.src/27-rc-adapter.js");
const EPUB_UI = read("_server_deploy/static/pdf/epub-html.js");

function functionBody(source, name, nextName) {
  const start = source.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `missing ${name}`);
  const end = nextName ? source.indexOf(`function ${nextName}(`, start + 1) : source.length;
  assert.notEqual(end, -1, `missing boundary ${nextName}`);
  return source.slice(start, end);
}

test("each native PDF assistant request carries the four complete App authorities", () => {
  const snapshot = functionBody(
    RUNTIME,
    "nativePDFAuthoritySnapshot",
    "nativePDFAssistantContext",
  );
  for (const kind of [
    "document-highlights",
    "document-notes-legacy",
    "ink",
    "user-pages",
  ]) assert.match(snapshot, new RegExp(`storedStateRecord\\(stores\\.document, '${kind}'`));
  assert.match(snapshot, /reader-native-pdf-assistant-state\/1|NATIVE_PDF_ASSISTANT_STATE_CONTRACT/);
  assert.match(snapshot, /utf8\(encoded\)\.byteLength > 6 \* 1024 \* 1024/);
  assert.doesNotMatch(snapshot, /\.slice\(|\.substring\(/);

  const injection = functionBody(RUNTIME, "nativePDFRequestBody", "nativePDFOperationID");
  assert.match(injection, /file_rel: localFileRef\(\)/);
  assert.match(injection, /native_local_state: snapshot/);
  const chat = functionBody(RUNTIME, "nativePDFChatFetch", "nativePDFVoiceToolFetch");
  const voice = functionBody(RUNTIME, "nativePDFVoiceToolFetch", "nativeEPUBAuthoritySnapshot");
  assert.match(chat, /nativePDFRequestBody\(input, init, 'context', writerLease\)/);
  assert.match(voice, /nativePDFRequestBody\(input, init, 'ctx', writerLease\)/);
});

test("SSE and voice actions must commit locally before old UI can observe success", () => {
  const commit = functionBody(RUNTIME, "nativePDFCommitActions", "nativePDFResponseHeaders");
  assert.match(commit, /serializeLocalStateMutation\('document', 'pdf-assistant-bundle'/);
  assert.match(commit, /assertRevision\('highlights'/);
  assert.match(commit, /assertRevision\('notes'/);
  assert.match(commit, /stores\.document\.batch\(mutations, bound\)/);
  assert.match(commit, /stateRecordMutation\(\s*'pdf-assistant-undo'/);
  assert.match(commit, /stateRecordMutation\(\s*'pdf-assistant-ops'/);
  assert.match(commit, /received\.has\(operationID\)/);
  assert.match(commit, /deriveCardPlacements\(notes\)/);
  assert.match(commit, /deriveEntityReferences\(placements\)/);

  const sse = functionBody(RUNTIME, "nativePDFCommittedSSE", "nativePDFChatFailure");
  const commitAt = sse.search(
    /nativePDFCommitActions\(\s*actions,\s*authority,\s*writerLease\s*\)/,
  );
  const exposeAt = sse.indexOf("controller.enqueue", commitAt);
  assert.ok(commitAt >= 0 && exposeAt > commitAt, "SSE exposes action before local commit");
  assert.match(sse, /reader\.cancel\(error\)/);
  assert.match(sse, /event: error/);
  assert.match(sse, /event: done/);

  const voice = functionBody(RUNTIME, "nativePDFVoiceToolFetch", "nativeEPUBAuthoritySnapshot");
  const voiceCommitAt = voice.search(
    /nativePDFCommitActions\(\s*actions,\s*request\.snapshot,\s*writerLease\s*\)/,
  );
  const voiceReplyAt = voice.indexOf("return jsonResponse(payload", voiceCommitAt);
  assert.ok(voiceCommitAt >= 0 && voiceReplyAt > voiceCommitAt,
    "voice-tool exposes result before local commit");
  assert.match(voice, /BW_NATIVE_PDF_ASSISTANT_CONFLICT/);
});

test("Pi only proposes native PDF document mutations and keeps legacy/PWA sidecars intact", () => {
  assert.match(ASSISTANT, /_NATIVE_PDF_STATE_CONTRACT = "reader-native-pdf-assistant-state\/1"/);
  assert.match(ASSISTANT, /set\(state\) != required/);
  assert.match(ASSISTANT, /def _vb_hls\(file_rel, ctx=None\)/);
  assert.match(ASSISTANT, /def _vb_notes\(file_rel, ctx=None\)/);
  assert.match(ASSISTANT, /if ids and not native_pdf:[\s\S]*with pdf\._hl_edit/);
  assert.match(ASSISTANT, /if not native_pdf:[\s\S]*with pdf\._notes_edit/);
  assert.match(ASSISTANT, /native_operation_id/);
  assert.match(ASSISTANT, /"fn": "_nativePDFUndoLast"/);

  assert.match(ASSISTANT_UI, /window\._nativePDFRefreshAnnotations/);
  assert.match(ASSISTANT_UI, /window\._reloadHighlights/);
  assert.match(ASSISTANT_UI, /window\.notesReload/);
  assert.match(
    PDF_ADAPTER,
    /reloadHighlights: \(\) => \{ try \{ return window\._reloadHighlights \? window\._reloadHighlights\(\)/,
  );
});

test("generic assistant routes select the matching PDF or EPUB native transaction", () => {
  assert.match(
    RUNTIME,
    /nativeInterfaceSurface === 'pdf' && url\.pathname === '\/api\/assistant\/chat'[\s\S]*nativePDFChatFetch/,
  );
  assert.match(
    RUNTIME,
    /nativeInterfaceSurface === 'pdf' && url\.pathname === '\/api\/assistant\/voice-tool'[\s\S]*nativePDFVoiceToolFetch/,
  );
  assert.match(
    RUNTIME,
    /nativeInterfaceSurface === 'epub' && url\.pathname === '\/api\/assistant\/chat'[\s\S]*nativeEPUBGenericChatFetch/,
  );
  assert.match(
    RUNTIME,
    /nativeInterfaceSurface === 'epub' && url\.pathname === '\/api\/assistant\/voice-tool'[\s\S]*nativeEPUBGenericVoiceToolFetch/,
  );
});

test("generic EPUB chat and voice inject App authority and commit local actions before exposure", () => {
  const request = functionBody(
    RUNTIME,
    "nativeEPUBGenericRequestBody",
    "nativeEPUBClientActionKind",
  );
  assert.match(request, /nativeEPUBAuthoritySnapshot\(\)/);
  assert.match(request, /file_rel: localFileRef\(\)/);
  assert.match(request, /native_local_state: snapshot/);

  const commit = functionBody(
    RUNTIME,
    "nativeEPUBCommitAssistantActions",
    "nativeEPUBCommittedSSE",
  );
  assert.match(commit, /nativeEPUBAssertAssistantRevisions/);
  assert.match(commit, /root\.nativeLocalEPUBMutationTransaction/);
  assert.match(commit, /root\.nativeLocalEPUBHighlight/);
  assert.match(commit, /return nativeEPUBAuthoritySnapshot\(\)/);

  const sse = functionBody(
    RUNTIME,
    "nativeEPUBCommittedSSE",
    "nativeEPUBGenericChatFailure",
  );
  const commitAt = sse.indexOf("nativeEPUBCommitAssistantActions(actions, revisions)");
  const exposeAt = sse.indexOf("controller.enqueue", commitAt);
  assert.ok(commitAt >= 0 && exposeAt > commitAt,
    "EPUB SSE exposes action before the App transaction");
  assert.match(sse, /reader\.cancel\(error\)/);

  const voice = functionBody(
    RUNTIME,
    "nativeEPUBGenericVoiceToolFetch",
    "nativeEPUBActionOperation",
  );
  const voiceCommitAt = voice.indexOf("nativeEPUBCommitAssistantActions(actions");
  const voiceReplyAt = voice.indexOf("return jsonResponse(payload", voiceCommitAt);
  assert.ok(voiceCommitAt >= 0 && voiceReplyAt > voiceCommitAt,
    "EPUB voice-tool exposes result before the App transaction");

  assert.match(EPUB_UI, /window\.nativeLocalEPUBHighlight = _epubHighlightTransaction/);
  assert.match(EPUB_UI, /window\.epubHighlight = function \(arg\)[\s\S]*task\.catch/);
  assert.match(EPUB_UI, /window\.nativeLocalEPUBMutationTransaction = _nativeLocalEPUBMutationTransaction/);
  assert.match(EPUB_UI, /function _nativeLocalEPUBMutationTransaction\(request\)[\s\S]*return fetch\('\/pdf\/api\/epub-action'/);
  assert.match(ASSISTANT, /def _native_epub_tool_call\(name, targs, ctx\):/);
  assert.match(ASSISTANT, /return epub\._esys_prompt\(native_ctx\)/);
});

test("generic assistant remote-book pointers include chat and voice file_rel fields", () => {
  assert.match(RUNTIME, /'\/context\/file_rel'/);
  assert.match(RUNTIME, /'\/ctx\/file_rel'/);
});
