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
  const restoreStart = SOURCE.indexOf("function _restoreStates(container)");
  const mountStart = SOURCE.indexOf("function mountState", restoreStart);
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

test("Anki add pending is fail-closed and persists its original aid", () => {
  const start = SOURCE.indexOf("function addToAnki(container, i)");
  const end = SOURCE.indexOf("function _stateSync", start);
  const add = SOURCE.slice(start, end);
  const pending = add.indexOf("notifyGroup(st.gid, 'anki-add-pending', i)");
  const sync = add.indexOf("_stateSync(st, i)", pending);
  const request = add.indexOf("fetch('/pdf/api/anki-add-cards'", pending);

  assert.ok(pending >= 0);
  assert.ok(sync > pending);
  assert.ok(request > sync);
  assert.match(add, /c\._addAid\s*=\s*aid/);
  assert.match(add, /anki_add_outcome_unknown/);
  assert.match(add, /anki_add_idempotency_unavailable/);
  assert.match(add, /已阻止重复提交/);
});

test("outbox acceptance requires a real command-outbox mutation id", () => {
  assert.equal(
    (SOURCE.match(/typeof mutationId !== 'string'/g) || []).length,
    2,
  );
  assert.equal(
    (SOURCE.match(/!\^mut-v2-/g) || []).length,
    0,
  );
  assert.equal(
    (SOURCE.match(/\/\^mut-v2-\[a-f0-9\]\{32\}\$\//g) || []).length,
    2,
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
