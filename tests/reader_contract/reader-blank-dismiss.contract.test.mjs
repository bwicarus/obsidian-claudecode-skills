import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const SIDEDRAWER = readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-sidedrawer.js", import.meta.url),
  "utf8",
);
const CARET = readFileSync(
  new URL(
    "../../_server_deploy/static/pdf/reader.src/16-caret-select.js",
    import.meta.url,
  ),
  "utf8",
);
const VOICECALL = readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-voicecall.js", import.meta.url),
  "utf8",
);
const STICKY = readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-stickynote.js", import.meta.url),
  "utf8",
);

test("true sidebar blank dismisses reader transients without stealing controls", () => {
  const start = SIDEDRAWER.indexOf("function _sideBlankDismissTarget");
  const end = SIDEDRAWER.indexOf("function buildChrome", start);
  const contract = SIDEDRAWER.slice(start, end);

  assert.ok(start >= 0 && end > start);
  assert.match(contract, /event\.composedPath/,
    "shadow-hosted controls must be classified by their real composed target");
  assert.match(
    contract,
    /button,a,input,textarea,select,option,label,summary,details/,
  );
  assert.match(
    contract,
    /\.vc-card,[\s\S]*?\.rv-improve-panel,[\s\S]*?\.asst-msg,[\s\S]*?#ep-side-settings/,
    "cards, messages, the improvement sheet and settings keep their own clicks",
  );
  assert.match(contract, /adapter\.clearSelection\(\)/);
  assert.match(contract, /CustomEvent\('rc:dismiss-transients'/);
  assert.match(
    SIDEDRAWER,
    /function buildChrome\(\)[\s\S]*?_bindSideBlankDismiss\(side\)/,
    "the blank-dismiss contract must be bound for both existing and fallback drawers",
  );
});

test("reader blank and sidebar blank share one complete selection cleanup", () => {
  const start = CARET.indexOf("function _shouldCloseToolbar");
  const end = CARET.indexOf("// 手写起笔时调", start);
  const contract = CARET.slice(start, end);

  assert.ok(start >= 0 && end > start);
  assert.match(contract, /target\?\.closest\?\.\('\.char-layer'\)/,
    "character selection owns clicks inside the PDF text layer");
  assert.match(
    contract,
    /button,a,input,textarea,select,option,label,summary,details/,
  );
  assert.match(contract, /function _dismissSelectionTransients\(\)/);
  assert.match(contract, /window\.getSelection\(\)\.removeAllRanges\(\)/);
  assert.match(contract, /lastSelText = ''/);
  assert.match(contract, /_charSel = null/);
  assert.match(
    contract,
    /window\.addEventListener\('rc:dismiss-transients', _dismissSelectionTransients\)/,
    "sidebar blanks and reader blanks must clear the same transient state",
  );
});

test("sidebar blank collapses persistent cards and closes only transient floats", () => {
  assert.match(
    VOICECALL,
    /addEventListener\('rc:dismiss-transients',[\s\S]*?classList\.contains\('vc-hasdot'\)[\s\S]*?_cardForm\(card\.el, 'dot'\)[\s\S]*?_cardClose\(card\)/,
  );
  assert.match(
    STICKY,
    /window\.addEventListener\('rc:dismiss-transients', onDocDown\)/,
    "pinned cards must reuse the same outside-click lowering path",
  );
});
