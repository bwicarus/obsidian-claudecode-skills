import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const SOURCE = fs.readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-flashcard.js", import.meta.url),
  "utf8",
);

test("rating pending is persisted before the irreversible request", () => {
  const start = SOURCE.indexOf("function rate(container, i, ease)");
  const end = SOURCE.indexOf("function cardsText", start);
  const rate = SOURCE.slice(start, end);
  const pending = rate.indexOf("notifyGroup(st.gid, 'review-pending', i)");
  const sync = rate.indexOf("_stateSync(st, i)", pending);
  const request = rate.indexOf("fetch('/pdf/api/review-answer'", pending);

  assert.ok(pending >= 0);
  assert.ok(sync > pending);
  assert.ok(request > sync);
  assert.match(rate, /c\._ratingAid\s*=\s*aid/);
  assert.match(rate, /c\._ratingEase\s*=\s*ease/);
  assert.match(rate, /c\._ratingCardId\s*=\s*exactCardId\s*\|\|\s*null/);
  assert.match(rate, /error\.reviewUnknown/);
});

test("local outcome-unknown dominates stale remote and authoritative learn", () => {
  const registerStart = SOURCE.indexOf("function register(container, opts)");
  const restoreStart = SOURCE.indexOf("function _restoreLegacyStates(container, st)");
  const mountStart = SOURCE.indexOf("function _restoreStates", restoreStart);
  const register = SOURCE.slice(registerStart, restoreStart);
  const restore = SOURCE.slice(restoreStart, mountStart);

  assert.match(
    register,
    /old\._ratingPending[\s\S]*old\._syncPending[\s\S]*old\._addPending/,
  );
  assert.match(
    register,
    /'_ratingAid'[\s\S]*'_ratingEase'[\s\S]*'_ratingCardId'/,
  );
  assert.match(restore, /var localPending\s*=\s*!!\(/);
  assert.match(restore, /var remotePending\s*=\s*!!\(/);
  assert.match(restore, /if \(localPending && !remotePending/);
  assert.match(
    SOURCE,
    /c\.isConnected\s*\|\|\s*c\s*===\s*except/,
    "detached renderInflow mount must remain registered until its shell is attached",
  );
});

test("Reader card confirmation persists locally before it is shown as saved", () => {
  const start = SOURCE.indexOf("function addToAnki(container, i)");
  const end = SOURCE.indexOf("function _recordExternalReceipt", start);
  const add = SOURCE.slice(start, end);
  const pending = add.indexOf("notifyGroup(st.gid, 'card-repository-pending', i)");
  const request = add.indexOf("repo.saveConfirmedCard(request", pending);
  const finishStart = SOURCE.indexOf("function finishRepositoryConfirmation");
  const finishEnd = SOURCE.indexOf("function failRepositoryConfirmation", finishStart);
  const finish = SOURCE.slice(finishStart, finishEnd);

  assert.ok(pending >= 0);
  assert.ok(request > pending);
  assert.match(finish, /notifyGroup\(st\.gid, 'card-repository-confirmed', i\)/);
  assert.match(add, /var request\s*=\s*\{[\s\S]*cards:\s*repositoryCards\(st\.cards\)/);
  assert.match(add, /var request\s*=\s*\{[\s\S]*source:\s*repositorySource\(st\)/);
  assert.match(finish, /✓ 已保存到 Reader 本地卡库/);
  assert.doesNotMatch(add, /\/pdf\/api\/anki-add-cards|addLocalAnkiCard/);
});

test("Reader confirmation outcome-unknown keeps one mutation pending and reconciles fail closed", () => {
  const unknownStart = SOURCE.indexOf("function repositoryOutcomeUnknown(error)");
  const addStart = SOURCE.indexOf("function addToAnki(container, i)", unknownStart);
  const end = SOURCE.indexOf("function removeDraft", addStart);
  const helpers = SOURCE.slice(unknownStart, addStart);
  const add = SOURCE.slice(addStart, end);

  assert.match(helpers, /current\.details && current\.details\.outcomeUnknown === true/);
  assert.match(helpers, /c\._addPending\s*=\s*true/);
  assert.match(helpers, /c\._addQueued\s*=\s*true/);
  assert.match(helpers, /c\._addAid\s*=\s*mutationId/);
  assert.match(
    helpers,
    /repo\.saveConfirmedCard\(request, \{ mutationId: mutationId \}\)[\s\S]*repo\.load\(st\.gid\)/,
  );
  assert.match(helpers, /repositoryConfirmation\(checked\.record, i\)/);
  assert.match(helpers, /repositoryOutcomeUnknown\(replayError\)/);
  assert.match(add, /if \(!st \|\| !c \|\| c\._addPending\) return/);
  assert.match(add, /c\._addAid\s*=\s*mutationId/);
  assert.match(add, /if \(!repositoryOutcomeUnknown\(error\)\)/);
  const unknownBranch = add.indexOf("holdUnknownRepositoryConfirmation(");
  const reconcile = add.indexOf("reconcileRepositoryConfirmation(", unknownBranch);
  assert.ok(unknownBranch >= 0 && reconcile > unknownBranch);
  assert.equal(
    add.slice(unknownBranch, reconcile).includes("failRepositoryConfirmation"),
    false,
  );
});

test("presentDraft validates existing cards/source and supplies a generic local source", () => {
  const start = SOURCE.indexOf("function presentDraft(cards, gid, options)");
  const present = SOURCE.slice(start, SOURCE.indexOf("RC.flashcard =", start));

  assert.doesNotMatch(present, /repo\.load\(gid\)[\s\S]*return record/);
  assert.match(present, /repo\.registerDraft\(\{/);
  assert.match(present, /cards:\s*repositoryCards\(cards\)/);
  assert.match(present, /var source = options\.repositorySource/);
  assert.match(present, /kind:\s*'reader-generated-card-draft'/);
  assert.match(present, /source:\s*source/);
  assert.match(present, /requireDraftIdForReplay:\s*true/);
});

test("draft textarea persists its live value at the stable batch index", () => {
  const start = SOURCE.indexOf("function bindSlide(container, slide, st, i)");
  const bind = SOURCE.slice(start, SOURCE.indexOf("function updateSlide", start));

  assert.match(bind, /st\.cards\[i\]\[ta\.dataset\.f\]\s*=\s*ta\.value/);
  assert.match(bind, /_stateSync\(st, i\)/);
  const exactFields = SOURCE.slice(
    SOURCE.indexOf("var _EXACT_STATE_FIELDS"),
    SOURCE.indexOf("var _repositorySubscription"),
  );
  assert.match(exactFields, /'front', 'back', 'cloze'/);
});

test("persisted draft deletion keeps stable cardIndex and hides only the removed slot", () => {
  const bindStart = SOURCE.indexOf("function bindSlide(container, slide, st, i)");
  const renderStart = SOURCE.indexOf("function renderTrack(container)", bindStart);
  const registerStart = SOURCE.indexOf("function register(container, opts)", renderStart);
  const removeStart = SOURCE.indexOf("function removeDraft(container, i)");
  const receiptStart = SOURCE.indexOf("function _recordExternalReceipt", removeStart);
  const bind = SOURCE.slice(bindStart, renderStart);
  const render = SOURCE.slice(renderStart, registerStart);
  const remove = SOURCE.slice(removeStart, receiptStart);

  assert.match(bind, /if \(act === 'del'\) \{ removeDraft\(container, i\); \}/);
  assert.doesNotMatch(SOURCE, /st\.cards\.splice\(/);
  assert.match(remove, /repo\.removeDraftCard\(st\.gid, i,/);
  assert.match(remove, /card\._removed\s*=\s*true/);
  assert.match(render, /activeIndices\.push\(index\)/);
  assert.match(render, /data-i="' \+ cardIndex/);
  assert.match(render, /bindSlide\(container, sl, st, Number\(sl\.dataset\.i\)\)/);
  assert.match(render, /st\.idx\s*=\s*activeIndices\[next\]/);
});

test("optional ReaderPC export records pending before the side effect and blocks unknown replay", () => {
  const start = SOURCE.indexOf("function exportToComputerAnki(container, i)");
  const end = SOURCE.indexOf("function exportToMobileAnki", start);
  const send = SOURCE.slice(start, end);
  const pending = send.indexOf("_recordExternalReceipt(st, i, 'readerpc', pending");
  const request = send.indexOf("RC.computerVoice.addLocalAnkiCard({", pending);

  assert.ok(pending >= 0);
  assert.ok(request > pending);
  assert.match(send, /c\._pcExportAid\s*=\s*aid/);
  assert.match(send, /c\._pcExportStatus === 'unknown'/);
  assert.match(send, /status:\s*safeToRetry \? 'failed' : 'unknown'/);
  assert.match(send, /电脑 Anki 接收结果未知，已阻止重复发送/);
});

test("outbox acceptance requires a real command-outbox mutation id", () => {
  assert.equal(
    (SOURCE.match(/typeof mutationId !== 'string'/g) || []).length,
    1,
  );
  assert.equal(
    (SOURCE.match(/!\^mut-v2-/g) || []).length,
    0,
  );
  assert.equal(
    (SOURCE.match(/\/\^mut-v2-\[a-f0-9\]\{32\}\$\//g) || []).length,
    1,
  );
});

test("long-press selection captures a live full-card snapshot", () => {
  const start = SOURCE.indexOf("function renderEntity(host, spec)");
  const renderEntity = SOURCE.slice(start);
  assert.match(
    renderEntity,
    /var live\s*=\s*snapshot\(result\.bd\)/,
  );
  assert.match(
    renderEntity,
    /meta\.contract\s*=\s*'anki-card-context\/1'/,
  );
  assert.match(renderEntity, /meta\.cards\s*=\s*copyCards\(live\)/);
  for (const field of [
    "card_id",
    "note_id",
    "entity_id",
    "source_ref",
    "source_url",
    "deck",
    "reason",
  ]) {
    assert.match(renderEntity, new RegExp(`'${field}'`));
  }
});
