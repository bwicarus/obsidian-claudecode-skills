import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const ROOT = new URL("../../", import.meta.url);
const VOICE = readFileSync(
  new URL("_server_deploy/static/pdf/rc-voicecall.js", ROOT),
  "utf8",
);

function queueHarness() {
  const helpers = VOICE.slice(
    VOICE.indexOf("function _rtcRespCreate"),
    VOICE.indexOf("function _rtcNewInkAck"),
  );
  const sandbox = {};
  vm.runInNewContext(`
    var _rtc = {
      responseActive: true,
      pendingToolCalls: Object.create(null),
      pendingToolResponse: null
    };
    var sent = [];
    var window = { dlog: function () {} };
    function _voiceMode() { return 'stt'; }
    function _dcSend(value) { sent.push(value); return true; }
    ${helpers}
    this.api = {
      state: _rtc,
      sent: sent,
      track: _rtcTrackToolCall,
      finish: _rtcFinishToolCall,
      queue: _rtcQueueToolResponse,
      flush: _rtcFlushToolResponse
    };
  `, sandbox);
  return sandbox.api;
}

test("fast native tool waits for the originating response to finish", () => {
  const api = queueHarness();
  api.track("call-1");
  assert.equal(api.queue("tool", false, {}), true);
  api.finish("call-1");
  assert.equal(api.sent.length, 0, "active preamble must block response.create");

  api.state.responseActive = false;
  assert.equal(api.flush(), true);
  assert.equal(api.sent.length, 1);
  assert.equal(api.sent[0].type, "response.create");
});

test("parallel tool outputs produce one consolidated final response", () => {
  const api = queueHarness();
  api.state.responseActive = false;
  api.track("call-a");
  api.track("call-b");
  api.queue("tool", false, {});
  api.queue("tool", true, { toolChoice: "none" });

  api.finish("call-a");
  assert.equal(api.sent.length, 0, "one tool is still pending");
  api.finish("call-b");
  assert.equal(api.sent.length, 1);
  assert.equal(api.sent[0].response.tool_choice, "none");
});

test("native direct keeps tool preamble and final answer in one persisted turn", () => {
  const begin = VOICE.slice(
    VOICE.indexOf("function _rtcBeginUserTurn"),
    VOICE.indexOf("var NATIVE_REALTIME_PI_AI_TOOLS"),
  );
  const created = VOICE.slice(
    VOICE.indexOf("} else if (t === 'response.created')"),
    VOICE.indexOf("} else if (t === 'response.done')"),
  );
  const done = VOICE.slice(
    VOICE.indexOf("} else if (t === 'response.done')"),
    VOICE.indexOf("} else if (t === 'error')"),
  );
  assert.match(begin, /_rtc\._newTurn = true/);
  assert.match(
    created,
    /_rtc\._newTurn \|\| \(!_rtc\.ctl && !_rtc\.nativeDirect\)/,
  );
  assert.match(created, /_rtc\.responseActive = true/);
  assert.match(done, /var responseHadToolCall = _rtcResponseHasFunctionCall\(e\)/);
  assert.match(done, /_rtc\.responseActive = false/);
  assert.match(done, /if \(!responseHadToolCall\)\s*try \{/);
  assert.match(done, /_rtcFlushToolResponse\(\)/);
  assert.match(done, /if \(curAText\) \{/);
  assert.match(done, /语音回复\(GPT Realtime\)/);
  const transcription = VOICE.slice(
    VOICE.indexOf("t === 'conversation.item.input_audio_transcription.completed'"),
    VOICE.indexOf("t === 'response.function_call_arguments.done'"),
  );
  assert.match(transcription, /__asstVoiceMsg\(\s*'u', tx/);

});
