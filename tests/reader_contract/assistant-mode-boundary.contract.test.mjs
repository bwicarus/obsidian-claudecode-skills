import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const SOURCE = fs.readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-assistant.js", import.meta.url),
  "utf8"
);

function functionBody(name, nextName) {
  const start = SOURCE.indexOf(`function ${name}(`);
  assert.notEqual(start, -1, `missing ${name}`);
  const end = nextName
    ? SOURCE.indexOf(`function ${nextName}(`, start + 1)
    : SOURCE.length;
  assert.notEqual(end, -1, `missing boundary ${nextName}`);
  return SOURCE.slice(start, end);
}

test("clear is an awaited mode-local transaction with stale I/O invalidation", () => {
  const body = functionBody("_clearCurrentConversation", null);
  const epochAt = body.indexOf("_modeEpoch++; _historyEpoch++;");
  const abortAt = body.indexOf("_abort.abort()");
  const fetchAt = body.indexOf(
    "await fetch(_clearUrl(clearMode), clearOpts)"
  );

  assert.ok(epochAt >= 0, "clear must invalidate both mode and history epochs");
  assert.ok(abortAt > epochAt, "epoch invalidation must precede SSE abort");
  assert.ok(fetchAt > abortAt, "clear request must start after stale stream abort");
  assert.match(body, /var clearMode = _assistantMode/);
  assert.match(body, /JSON\.stringify\(\{ assistant_mode: 'review' \}\)/);
  assert.match(body, /clearResponse\.ok === false/);
  assert.match(body, /clearResult && clearResult\.ok === false/);
  assert.match(
    body,
    /await loadHistory\(clearMode, \{ greetOnError: false \}\)/
  );
  assert.match(body, /greet\(\);\s+return true/);
  assert.match(body, /finally \{\s+_setClearingUi\(false\)/);

  // The retired implementation cleared optimistically and ignored the
  // server promise. Keeping this exact anti-contract guards future merges.
  assert.doesNotMatch(
    SOURCE,
    /fetch\(_clearUrl\(clearMode\), clearOpts\)\.catch\(function \(\) \{\}\);\s+greet\(\)/
  );
});

test("send, Enter, microphone and mode changes cannot cross clear boundary", () => {
  const sendBody = functionBody("send", "_setClearingUi");
  const modeBody = functionBody("setAssistantMode", "loadHistory");

  assert.match(sendBody, /if \(streaming \|\| _clearing\) return/);
  assert.match(SOURCE, /window\.__asstBusy = function \(\) \{ return !!\(streaming \|\| _clearing\); \}/);
  assert.match(
    SOURCE,
    /if \(e\.key === 'Enter' && !e\.shiftKey\) \{ e\.preventDefault\(\); if \(streaming \|\| _clearing\) return/
  );
  assert.match(SOURCE, /sendBtn\.addEventListener\('click', function \(\) \{\s+if \(_clearing\) return/);
  assert.match(SOURCE, /if \(!micOn \|\| _clearing\) return/);
  assert.match(
    SOURCE,
    /micBtn\.addEventListener\('click', function \(\) \{ if \(!_clearing\)/
  );
  assert.match(modeBody, /if \(_clearing\)/);
  assert.match(modeBody, /mode !== _assistantMode/);
  assert.match(modeBody, /rc:assistant-mode-changed/);
  assert.match(SOURCE, /reviewToggle\.disabled = _clearing/);
  assert.match(SOURCE, /clearButton\.disabled = _clearing/);
});

test("TTS clip attachment captures the message conversation mode", () => {
  const body = functionBody("_attachClipBtn", "setAssistantMode");
  assert.match(body, /function _attachClipBtn\(el, m, assistantMode\)/);
  assert.match(
    body,
    /var clipMode = _modeNorm\(assistantMode \|\| m\.assistant_mode \|\| _assistantMode\)/
  );
  assert.match(body, /assistant_mode: clipMode/);
  assert.match(
    body,
    /voice-clip\?id=' \+ id \+ '&assistant_mode=' \+ encodeURIComponent\(clipMode\)/
  );
  assert.match(
    body,
    /voice-clip\/' \+ encodeURIComponent\(m\.clip\) \+ '\?assistant_mode=' \+ encodeURIComponent\(clipMode\)/
  );
  assert.doesNotMatch(body, /assistant_mode: _assistantMode/);

  // Live text answers freeze turnMode, voice callbacks freeze voiceMode, and
  // replayed history freezes loadHistory's explicit mode.
  assert.ok(
    SOURCE.match(/_attachClipBtn\([^\n]+turnMode\)/g)?.length >= 2,
    "both live-answer render paths must pass turnMode"
  );
  assert.match(
    SOURCE,
    /_attachClipBtn\(_bel, \{ content: a, clip: \(extra && extra\.clip\) \|\| '' \}, voiceMode\)/
  );
  assert.match(SOURCE, /_attachClipBtn\(el, m, mode\)/);
});
