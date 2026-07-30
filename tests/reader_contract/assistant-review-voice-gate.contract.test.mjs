import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const SOURCE = fs.readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-voicecall.js", import.meta.url),
  "utf8"
);
const COMPUTER_VOICE_SOURCE = fs.readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-computer-voice.js", import.meta.url),
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

test("review mode gates every realtime voice entry before transport", () => {
  const rtc = functionBody("rtcStart", "rtcTeardown");
  const start = functionBody("start", "toggle");
  const toggle = functionBody("toggle", "setPage");
  const connectStart = SOURCE.indexOf("toggle._connect = function (opts)");
  const connectEnd = SOURCE.indexOf("function toggle(opts)", connectStart);
  assert.ok(connectStart >= 0 && connectEnd > connectStart);
  const connect = SOURCE.slice(connectStart, connectEnd);

  assert.match(SOURCE, /function _assistantInReview\(\)/);
  assert.match(
    SOURCE,
    /RC\.assistant\.getMode\(\) === 'review'/
  );
  assert.ok(
    rtc.indexOf("_reviewVoiceGate(false)") <
      rtc.indexOf("fetch('/api/assistant/rtc-session'"),
    "RTC must stop before session creation"
  );
  assert.ok(
    start.indexOf("_reviewVoiceGate(false)") <
      start.indexOf("_openWs('/voice-rt'"),
    "WS voice must stop before opening a socket"
  );
  assert.match(connect, /if \(_reviewVoiceGate\(false\)\) return/);
  assert.match(
    connect,
    /g0 !== _gen \|\| _reviewVoiceGate\(false\)/
  );
  assert.match(toggle, /if \(_reviewVoiceGate\(true\)\) return false/);
});

test("computer and phone clicks are blocked in review; hidden legacy mic stays inert", () => {
  const button = functionBody("injectBtn", "_lpPop");
  const longAction = functionBody("_micLongAction", "_bindLongPress");
  const topbar = functionBody("injectTopbarBtns", "_fmtCutoff");

  const gateAt = button.indexOf("_reviewVoiceGate(true)");
  const computerAt = button.indexOf("_computerVoiceStart(opts, generation)");
  const callAt = button.indexOf("window._voiceCallS2S");
  assert.ok(gateAt >= 0 && computerAt > gateAt && callAt > gateAt);
  assert.match(longAction, /if \(_reviewVoiceGate\(true\)\) return/);

  assert.match(
    button,
    /mic\.style\.display = 'none'[\s\S]*mic\.setAttribute\('aria-hidden', 'true'\)[\s\S]*mic\.tabIndex = -1/,
  );
  assert.match(
    topbar,
    /tm\.addEventListener\('click', function \(\) \{ try \{ srcComputer\.click\(\)/
  );
  assert.match(
    topbar,
    /tc\.addEventListener\('click', function \(\) \{ try \{ srcCall\.click\(\)/,
  );
  assert.doesNotMatch(topbar, /srcMic|vc-top-mic|_bindLongPress/);
  assert.match(
    SOURCE,
    /canCaptureComputerVoiceGesture:\s*function \(\) \{ return !_assistantInReview\(\); \}/
  );
  assert.match(
    COMPUTER_VOICE_SOURCE,
    /RC\.voicecall\.canCaptureComputerVoiceGesture\(\) !== true[\s\S]*return/
  );
});

test("mode changes update accessibility state and tear down in-flight voice", () => {
  assert.match(
    SOURCE,
    /window\.addEventListener\('rc:assistant-mode-changed'/
  );
  assert.match(
    SOURCE,
    /blocked && \(ws \|\| computerOn \|\| _rtc\.on \|\| _connecting \|\| _reconnT \|\| _reconnPend\)\) teardown\(true\)/
  );
  assert.match(
    SOURCE,
    /el\.setAttribute\('aria-disabled', blocked \? 'true' : 'false'\)/
  );
  assert.match(
    SOURCE,
    /el\.dataset\.vcLongpress = blocked \? 'disabled-in-review' : 'enabled'/
  );
});
