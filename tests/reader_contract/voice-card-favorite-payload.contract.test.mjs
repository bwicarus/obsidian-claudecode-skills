import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const VOICE_SOURCE = fs.readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-voicecall.js", import.meta.url),
  "utf8"
);
const SERVER_SOURCE = fs.readFileSync(
  new URL("../../_server_deploy/assistant.py", import.meta.url),
  "utf8"
);
const FLASHCARD_SOURCE = fs.readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-flashcard.js", import.meta.url),
  "utf8"
);

test("learning-card favorites use one bounded structured payload instead of sliced JSON", () => {
  assert.match(VOICE_SOURCE, /_FAV_CARDS_PAYLOAD_VERSION\s*=\s*1/);
  assert.match(VOICE_SOURCE, /_FAV_CARDS_MAX_BYTES\s*=\s*256\s*\*\s*1024/);
  assert.match(
    VOICE_SOURCE,
    /payload\s*=\s*\{\s*version:\s*_FAV_CARDS_PAYLOAD_VERSION,\s*kind:\s*'cards',\s*cards:\s*cards\s*\}/
  );
  assert.match(
    VOICE_SOURCE,
    /key\s*!==\s*'raw'\s*&&\s*key\s*!==\s*'payload'/
  );
  assert.match(
    VOICE_SOURCE,
    /rec\.payload\s*&&\s*rec\.payload\.version\s*===\s*_FAV_CARDS_PAYLOAD_VERSION/
  );

  assert.match(SERVER_SOURCE, /_VCARD_CARDS_MAX_BYTES\s*=\s*256\s*\*\s*1024/);
  assert.match(SERVER_SOURCE, /_VCARD_REQUEST_MAX_BYTES\s*=\s*320\s*\*\s*1024/);
  assert.match(SERVER_SOURCE, /allow_nan=False/);
  assert.match(SERVER_SOURCE, /rec\["payload"\]\s*=\s*cards_payload/);
  assert.match(SERVER_SOURCE, /"code":\s*"stale_cards_revision"/);
  assert.match(
    SERVER_SOURCE,
    /with _vcard_lock:\s*return _assistant_voice_cards_locked\(\)/
  );
  assert.match(VOICE_SOURCE, /rec\.revision\s*=\s*revision/);
  assert.match(VOICE_SOURCE, /error\.staleRevision\s*=\s*response\.status\s*===\s*409/);
  assert.doesNotMatch(
    SERVER_SOURCE,
    /"raw":\s*str\(c\.get\("raw"\)\s*or\s*""\)\[:20000\]/
  );
});

test("plain HTML/text favorites keep the legacy presentation payload", () => {
  assert.match(
    SERVER_SOURCE,
    /rec\["raw"\]\s*=\s*str\(c\.get\("raw"\)\s*or\s*""\)\[:20000\]/
  );
  assert.match(
    VOICE_SOURCE,
    /if\s*\(!isCards\)\s*return\s*\{\s*ok:\s*true,\s*record:\s*rec\s*\}/
  );
});

test("favorite state writes back the complete live card snapshot", () => {
  assert.match(VOICE_SOURCE, /onStateChange:\s*function\s*\(nextCards\)/);
  assert.match(
    VOICE_SOURCE,
    /next\.payload\s*=\s*\{\s*version:\s*_FAV_CARDS_PAYLOAD_VERSION,\s*kind:\s*'cards',\s*cards:\s*nextCards/
  );
  assert.match(VOICE_SOURCE, /_favSave\(next\)/);
  assert.match(
    VOICE_SOURCE,
    /var cards\s*=\s*RC\.flashcard\.snapshot\(body\)/
  );
  assert.match(
    VOICE_SOURCE,
    /payload:\s*\{\s*version:\s*_FAV_CARDS_PAYLOAD_VERSION,\s*kind:\s*'cards',\s*cards:\s*cards/
  );
  for (const reason of [
    "review-pending",
    "review-accepted",
    "review-queued",
    "review-reverted",
  ]) {
    assert.match(
      FLASHCARD_SOURCE,
      new RegExp(`notifyGroup\\(st\\.gid, '${reason}', i\\)`)
    );
  }
  assert.match(
    FLASHCARD_SOURCE,
    /c\._st\s*=\s*'done';\s*c\._ratingPending\s*=\s*true;/
  );
});

test("favorite drag-out reuses the unique live flashcard renderer", () => {
  assert.match(
    VOICE_SOURCE,
    /var entity\s*=\s*RC\.flashcard\.renderEntity\(null,\s*\{\s*surface:\s*'float',\s*mode:\s*'state'/
  );
  assert.match(VOICE_SOURCE, /c\s*=\s*entity\s*&&\s*entity\.voiceCard/);
  assert.doesNotMatch(
    VOICE_SOURCE,
    /c\s*=\s*_cardPush\(null,\s*rec\.label\s*\|\|\s*'🎴 学习卡片'/
  );
});

test("drag start explicitly cancels the same card's pending long-press", () => {
  assert.match(VOICE_SOURCE, /el\.__bwCancelPinHold\s*=\s*_cancelPinHold/);
  assert.match(
    VOICE_SOURCE,
    /moved\s*=\s*true;[\s\S]{0,320}if\s*\(typeof el\.__bwCancelPinHold === 'function'\)\s*el\.__bwCancelPinHold\(\)/
  );
});

test("floating learning-card pinning uses the canonical full snapshot", () => {
  assert.match(
    VOICE_SOURCE,
    /RC\.flashcard\s*&&\s*typeof RC\.flashcard\.snapshot === 'function'/
  );
  assert.match(VOICE_SOURCE, /RC\.flashcard\.snapshot\(bd\)/);
  assert.doesNotMatch(
    VOICE_SOURCE,
    /st\.cards\.map\(function\s*\(cc\)\s*\{\s*return\s*\{\s*type:\s*cc\.type/
  );
});

test("both voice completions and favorite drag-out use the unique flashcard renderer", () => {
  const floatCalls = VOICE_SOURCE.match(
    /RC\.flashcard\.renderEntity\(null,\s*\{\s*surface:\s*'float'/g
  ) || [];
  assert.equal(floatCalls.length, 3);
  assert.doesNotMatch(
    VOICE_SOURCE,
    /RC\.voiceCard\.push\(null,\s*'🎴 制卡'/
  );
  assert.doesNotMatch(
    VOICE_SOURCE,
    /RC\.flashcard\.mount(?:Drafts|Preview)\(bd,\s*_(?:sc|cds)/
  );
});
