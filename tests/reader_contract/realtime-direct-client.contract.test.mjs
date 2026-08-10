import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");

const SERVER = read("_server_deploy/assistant.py");
const VOICE = read("_server_deploy/static/pdf/rc-voicecall.js");
const VOICE_CONTEXT = read("_server_deploy/static/pdf/rc-voicectx.js");
const SETTINGS = read("_server_deploy/static/pdf/rc-assistant.js");
const EXTENSION_BACKGROUND = read("extensions/bw-reader-webext/background.js");
const EXTENSION_FACADE = read("extensions/bw-reader-webext/src/facade.js");
const LOCAL_SERVER = read("ios/BWReader/App/ReaderLocalRuntimeServer.swift");
const NATIVE_MANIFEST = JSON.parse(read(
  "ios/BWReader/native_reader_interface_manifest.json",
));

test("installed App and extension establish Realtime media directly with an ephemeral key", () => {
  assert.match(SERVER, /@bp\.route\("\/rtc-client-secret", methods=\["POST"\]\)/);
  assert.match(SERVER, /https:\/\/api\.openai\.com\/v1\/realtime\/client_secrets/);
  assert.match(SERVER, /"expires_after": \{"anchor": "created_at", "seconds": 90\}/);
  assert.match(SERVER, /"client_secret": secret/);
  assert.match(SERVER, /result\.headers\["Cache-Control"\] = "no-store"/);

  const direct = VOICE.slice(
    VOICE.indexOf("async function _openDirectRtcCall"),
    VOICE.indexOf("async function rtcStart"),
  );
  assert.match(direct, /fetch\('\/api\/assistant\/rtc-client-secret'/);
  assert.match(direct, /fetch\('https:\/\/api\.openai\.com\/v1\/realtime\/calls'/);
  assert.match(direct, /'Authorization': 'Bearer ' \+ tokenRes\.client_secret/);
  assert.match(direct, /'Content-Type': 'application\/sdp'/);
  assert.match(direct, /direct\.headers\.get\('Location'\)/);
  assert.match(direct, /fetch\('\/api\/assistant\/rtc-bind'/);
  assert.match(direct, /catch \(error\) \{\s*_rtcRequestHangup\(callId\);\s*throw error/);
  assert.doesNotMatch(direct, /sk-[A-Za-z0-9]/);

  assert.match(
    VOICE,
    /window\.__BW_NATIVE_LOCAL_READER__ === true \|\|\s*typeof window\.__bwReaderFetch === 'function'/,
  );
  assert.match(
    EXTENSION_FACADE,
    /u !== OPENAI_REALTIME_CALL_URL[\s\S]*return fetch\(url, init\)/,
  );
  assert.match(EXTENSION_BACKGROUND, /\^Bearer ek_/);
  assert.match(EXTENSION_BACKGROUND, /contentType !== "application\/sdp"/);
  assert.match(EXTENSION_BACKGROUND, /if \(!checked\.directOpenAI\) \{\s*headers\.set\("Authorization"/);
});

test("direct media keeps the existing Reader context and tool sideband intact", () => {
  const speechStarted = VOICE.slice(
    VOICE.indexOf("t === 'input_audio_buffer.speech_started'"),
    VOICE.indexOf("t === 'output_audio_buffer.started'"),
  );
  assert.ok(
    speechStarted.indexOf("_requestSyncNow()") <
      speechStarted.indexOf("_rtcFlushCtx()"),
    "the current viewport must refresh before its text is injected",
  );
  assert.match(VOICE, /visible_text \|\| ''\)\.slice\(0, 2000\)/);
  assert.match(VOICE, /dc\.onopen = function \(\) \{[\s\S]*_rtcInjectHistory\(\)[\s\S]*RC\.voiceCtx && RC\.voiceCtx\.flushPending\('rtc'\)/);
  assert.match(VOICE, /function _ctlOpen\(\)[\s\S]*mode=rtc&fe=4&call_id=/);
  assert.match(VOICE, /_rtc\.uid = cres\.uid[\s\S]*_rtc\.tk = cres\.ticket/);
  assert.match(VOICE, /_ctlOpen\(\);[\s\S]*_refreshSpeakTg\(\)/);
  assert.match(VOICE, /function rtcTeardown\(\) \{\s*var retiringCallId = _rtc\.callId[\s\S]*_rtcRequestHangup\(retiringCallId\)/);
  const remoteCommit = VOICE.slice(
    VOICE.indexOf("await pc.setRemoteDescription"),
    VOICE.indexOf("_rtc.pc = pc"),
  );
  assert.ok(
    remoteCommit.indexOf("if (g !== _gen") <
      remoteCommit.indexOf("_rtc.callId = startupCallId"),
    "a stale dial must not commit its call identity",
  );
  assert.match(remoteCommit, /_rtcAbandon\(pc, mic, startupCallId\)/);
  assert.match(VOICE, /setPage: setPage, syncInk: syncInk, syncState: syncState/);
  assert.match(VOICE, /type: 'page'[\s\S]*text: vtext/);
  assert.match(VOICE, /type: 'state', sel: state\.sel/);
  assert.match(VOICE, /type: 'ink', page: page, strokes:/);

  assert.match(VOICE_CONTEXT, /if \(_deliver\(ch,[\s\S]*FP\[ch\]\[kind\] = fp/);
  assert.match(VOICE_CONTEXT, /function flushPending\(ch\)/);
  assert.match(VOICE_CONTEXT, /bindTransport: function \(ch, tr\)/);

  assert.match(LOCAL_SERVER, /Object\.defineProperty\(window,"__bwReaderWsUrl"/);
  assert.match(LOCAL_SERVER, /value:\(path\)=>[\s\S]*\/voice-rt/);
  assert.match(LOCAL_SERVER, /openAIRealtimeOrigin[\s\S]*realtimeControlWebSocketOrigin/);
});

test("native proxy manifest exposes only the two new bounded Pi control endpoints", () => {
  const byPath = new Map(NATIVE_MANIFEST.routes.map((route) => [route.path, route]));
  const secret = byPath.get("/api/assistant/rtc-client-secret");
  const bind = byPath.get("/api/assistant/rtc-bind");
  assert.ok(secret);
  assert.ok(bind);
  assert.deepEqual(secret.methods, ["POST"]);
  assert.equal(secret.owner, "pi");
  assert.equal(secret.remoteBook.mode, "conditional");
  assert.equal(secret.remoteBook.identities[0].pointer, "/file");
  assert.deepEqual(bind.methods, ["POST"]);
  assert.equal(bind.owner, "pi");
  assert.equal(bind.remoteBook, null);
});

test("legacy OpenAI setting migrates to the single direct WebRTC engine", () => {
  assert.match(SETTINGS, /if \(voiceEngine === 'openai'\) voiceEngine = 'openai_rtc'/);
  assert.match(SERVER, /if b\.get\("rt_engine"\) == "openai":[\s\S]*b\["rt_engine"\] = "openai_rtc"/);
  assert.doesNotMatch(SETTINGS, /<option value="openai"/);
});
