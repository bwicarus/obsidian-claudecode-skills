import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const read = (path) => fs.readFileSync(path, "utf8");
const manifest = JSON.parse(read("extensions/bw-reader-webext/manifest.json"));
const full = manifest.content_scripts[1].js;
const assistant = read("_server_deploy/static/pdf/rc-assistant.js");
const voicecall = read("_server_deploy/static/pdf/rc-voicecall.js");
const runtime = read("_server_deploy/static/pdf/rc-computer-voice.js");
const settings = read("_server_deploy/static/pdf/rc-settings.js");
const background = read("extensions/bw-reader-webext/background.js");
const offscreen = read("extensions/bw-reader-webext/offscreen.js");

test("电脑客户端使用独立按钮与设置标签，历史引擎值回落普通电话", () => {
  assert.doesNotMatch(assistant, /value="computer_client"/);
  assert.doesNotMatch(assistant, /RC\.computerVoice\.mountSettings\(card\)/);
  assert.doesNotMatch(
    assistant,
    /computer_client[\s\S]{0,300}startFromUserGesture/,
  );
  assert.match(
    assistant,
    /var voiceEngine = c\.rt_engine === 'computer_client' \? '' : \(c\.rt_engine \|\| ''\)/,
  );
  assert.match(settings, /data-pane="computer">电脑客户端<\/button>/);
  assert.match(settings, /id="rcset-computer-inline"/);
  assert.match(settings, /RC\.computerVoice\.mountSettings\(host\)/);
  assert.match(voicecall, /c\.id = 'asst-computer'; c\.type = 'button'/);
  assert.match(voicecall, /b\.id = 'asst-call'; b\.type = 'button'/);
  assert.match(
    voicecall,
    /RC\.computerVoice\.startFromUserGesture\(opts \|\| \{\}\)/,
  );
});

test("普通电话不再启动电脑桥，两个入口切换时互斥清理", () => {
  const connectStart = voicecall.indexOf("toggle._connect = function (opts)");
  const connectEnd = voicecall.indexOf("function toggle(opts)", connectStart);
  assert.ok(connectStart >= 0 && connectEnd > connectStart);
  const connect = voicecall.slice(connectStart, connectEnd);
  assert.doesNotMatch(connect, /_computerVoiceStart\(|startFromUserGesture/);
  assert.match(connect, /if \(_computerVoiceStarting \|\| _computerVoiceActive\(\)\)[\s\S]*_stopComputerVoiceOnly\('ordinary-voice-start'\)/);
  assert.match(connect, /if \(engine === 'openai_rtc'\) rtcStart\(opts\);\s*else start\(opts\)/);

  const teardownStart = voicecall.indexOf(
    "function teardown(closeBox, preserveComputerGesture)",
  );
  const teardownEnd = voicecall.indexOf(
    "function _setComputerVoiceDialPending",
    teardownStart,
  );
  assert.ok(teardownStart >= 0 && teardownEnd > teardownStart);
  const teardown = voicecall.slice(teardownStart, teardownEnd);
  assert.match(
    teardown,
    /if \(!preserveComputerGesture\) \{\s*_setComputerVoiceDialPending\(false\);\s*_cancelComputerVoiceGesture\(\);\s*\}/,
  );
  assert.match(
    teardown,
    /if \(!preserveComputerGesture\) _audioSession\('playback'\)/,
  );

  const injectStart = voicecall.indexOf("function injectBtn()");
  const computerClickStart = voicecall.indexOf(
    "c.addEventListener('click'",
    injectStart,
  );
  const computerClickEnd = voicecall.indexOf(
    "b.addEventListener('click'",
    computerClickStart,
  );
  assert.ok(
    injectStart >= 0 &&
      computerClickStart > injectStart &&
      computerClickEnd > computerClickStart,
  );
  const computerClick = voicecall.slice(computerClickStart, computerClickEnd);
  assert.match(computerClick, /if \(ws \|\| _rtc\.on \|\| _connecting \|\| _reconnT \|\| _reconnPend\)[\s\S]*teardown\(false, true\)/);
  assert.match(computerClick, /_setComputerVoiceDialPending\(true\)[\s\S]*_computerVoiceStart\(opts, generation\)/);
  assert.ok(
    computerClick.indexOf("teardown(false, true)") <
      computerClick.indexOf("_setComputerVoiceDialPending(true)") &&
      computerClick.indexOf("_setComputerVoiceDialPending(true)") <
        computerClick.indexOf("_computerVoiceStart(opts, generation)"),
    "ordinary voice must release while preserving the trusted surface before START",
  );
});

test("只有 voicecall 闭包创建并登记的电脑按钮身份可以签发 START 租约", () => {
  assert.match(runtime, /var registeredComputerButtons = new WeakSet\(\)/);
  assert.match(
    runtime,
    /function registerComputerButton\(button\)[\s\S]*registeredComputerButtons\.add\(button\)/,
  );
  assert.match(
    runtime,
    /function computerButtonFromEvent\(event\)[\s\S]*target\.id === "asst-computer"[\s\S]*target\.id === "vc-top-computer"[\s\S]*registeredComputerButtons\.has\(target\)/,
  );
  assert.match(voicecall, /var _computerVoiceOwnedButtons = new WeakSet\(\)/);
  assert.match(
    voicecall,
    /_computerVoiceOwnedButtons\.has\(button\)[\s\S]*RC\.computerVoice\.registerComputerButton\(button\)/,
  );
  assert.match(voicecall, /_ownComputerVoiceButton\(c\)/);
  assert.match(voicecall, /_ownComputerVoiceButton\(tm\)/);
});

test("Reader v3 严格区分 app-output 下行与 browser-microphone track 3 上行", () => {
  assert.match(runtime, /var PCM_FRAME_BYTES = 1956/);
  assert.match(runtime, /var PCM_HEADER_BYTES = 36/);
  assert.match(runtime, /var PCM_UPLINK_TRACK = 3/);
  assert.match(runtime, /view\.getUint8\(5\)/);
  assert.match(runtime, /track === 1[\s\S]*queueAppOutput\(state, samples\)/);
  assert.match(
    runtime,
    /navigator\.mediaDevices\.getUserMedia\(\{[\s\S]*echoCancellation: true[\s\S]*video: false/,
  );
  assert.match(
    runtime,
    /state\.channel\.sendBinary\([\s\S]*encodeMicrophoneFrame\(state, output\)/,
  );
  assert.match(runtime, /view\.setUint8\(5, PCM_UPLINK_TRACK\)/);
  assert.doesNotMatch(
    runtime,
    /RTCPeerConnection|createComputerVoiceWebRtcController|signalTransport/,
  );
});

test("Reader 与扩展都不再调用旧 Pi computer-voice API", () => {
  assert.doesNotMatch(
    background,
    /\/api\/reader\/computer-voice|BW_COMPUTER_VOICE_(?:PAIR|STATUS|STOP)/,
  );
  assert.doesNotMatch(
    background,
    /chrome\.offscreen\.createDocument|chrome\.runtime\.connectNative/,
  );
  assert.match(runtime, /reader-computer-voice-direct\/1/);
  assert.match(
    runtime,
    /currentOrigin\(\) === READER_ORIGIN[\s\S]*new window\.WebSocket\(endpoint\)/,
  );
  assert.match(
    runtime,
    /window\.chrome && window\.chrome\.runtime[\s\S]*new ExtensionRelaySocket\(runtime\)/,
  );
  assert.match(runtime, /BW_COMPUTER_VOICE_DIRECT_RELAY_REQUIRED/);
  assert.match(background, /BW_COMPUTER_VOICE_DIRECT_V3/);
  assert.match(
    background,
    /wss:\/\/bwicarus-2\.taile44d0c\.ts\.net\/reader-computer-voice\/v1/,
  );
  assert.doesNotMatch(runtime, /\/api\/reader\/computer-voice/);
  assert.doesNotMatch(
    runtime,
    /deviceToken|Authorization|commands\/claim|localStorage|Bearer /,
  );
});

test("扩展在所有 HTTP(S) 页面加载同一 direct 入口并先于 voicecall", () => {
  assert.deepEqual(
    manifest.content_scripts[1].matches,
    ["https://*/*", "http://*/*"],
  );
  const entry = full.indexOf("vendor/rc-computer-voice.js");
  const call = full.indexOf("vendor/rc-voicecall.js");
  assert.ok(entry >= 0 && call > entry);
  assert.equal(
    full.filter((path) => path === "vendor/rc-computer-voice.js").length,
    1,
  );
});

test("旧 offscreen 文件仅为惰性 tombstone，不能恢复 Native/WebRTC 链", () => {
  assert.match(offscreen, /兼容占位/);
  assert.doesNotMatch(
    offscreen,
    /chrome\.|connectNative|MediaStreamTrackGenerator|AudioData|RTCPeerConnection|fetch\s*\(/,
  );
});

test("v3 只在可信电脑按钮手势预备可撤销麦克风，START 走固定 WSS", () => {
  assert.match(
    runtime,
    /wss:\/\/bwicarus-2\.taile44d0c\.ts\.net\/reader-computer-voice\/v1/,
  );
  assert.match(runtime, /channel\.request\("hello", \{\s*protocolVersion: 3/);
  assert.match(
    runtime,
    /function computerButtonFromEvent\(event\)[\s\S]*registeredComputerButtons\.has\(target\)/,
  );
  assert.match(
    runtime,
    /RC\.voicecall\.canCaptureComputerVoiceGesture\(\) !== true[\s\S]*return/,
  );
  assert.match(
    runtime,
    /function prepareSurfaceFromGesture\(\)[\s\S]*prepareMicrophoneFromGesture\(preparedSurface\)/,
  );
  assert.match(runtime, /if \(!active\) \{[\s\S]*prepareSurfaceFromGesture\(\)/);
  assert.match(
    runtime,
    /function snapshotLinkWanted\(\)[\s\S]*contextSyncEnabled\(\)[\s\S]*contextDeliveryMode !== CONTEXT_DELIVERY_LEGACY/,
  );
  const snapshotWanted = runtime.slice(
    runtime.indexOf("function snapshotLinkWanted()"),
    runtime.indexOf("function stopSnapshotLink()", runtime.indexOf("function snapshotLinkWanted()")),
  );
  assert.doesNotMatch(snapshotWanted, /selectedEngineKnown|computerVoiceSelected/);
  assert.match(
    runtime,
    /function directChannelLive\(channel\)[\s\S]*channel\.socket[\s\S]*socket\.readyState === 1/,
  );
  assert.match(
    runtime,
    /function claimSnapshotLinkForStart\(\)[\s\S]*directChannelLive\(state\.channel\)/,
  );
  assert.match(
    runtime,
    /function startFromUserGesture[\s\S]*return surface\.microphonePromise[\s\S]*channel\.request\("start", \{\s*sessionId: state\.sessionId/,
  );
  assert.doesNotMatch(runtime, /connectNative|RTCPeerConnection|signalTransport/);
});

test("扩展上行用 sequence + binary-accepted 单 credit，STATUS 保留 lastError", () => {
  assert.match(
    runtime,
    /if \(this\.binaryInFlight !== null\) return false/,
  );
  assert.match(runtime, /sequence: new DataView\(/);
  assert.match(
    runtime,
    /message\.type === "binary-accepted"[\s\S]*message\.sequence !== this\.binaryInFlight[\s\S]*this\.binaryInFlight = null/,
  );
  assert.match(
    runtime,
    /var sent = state\.channel\.sendBinary[\s\S]*if \(sent\) state\.uplinkSequence \+= 1/,
  );
  assert.match(
    background,
    /\["type", "data", "bytes", "sequence"\][\s\S]*message\.sequence[\s\S]*type: "binary-accepted"/,
  );
  assert.match(
    runtime,
    /\["failureId", "code", "stage", "hresult", "atUtc"\]/,
  );
  assert.match(runtime, /value\.status && value\.status\.lastError/);
});

test("Reader v3 固定 WSS 免配对直连，不保存身份或显示显式配置", () => {
  assert.match(runtime, /channel\.request\("hello", \{\s*protocolVersion: 3/);
  assert.match(runtime, /new DirectSocket\(DIRECT_ENDPOINT, options\)/);
  assert.doesNotMatch(
    runtime,
    /indexedDB|generateKey|exportKey|clientPublicKeySpki|pairingCode|deviceToken|chrome\.storage|localStorage|Authorization|Bearer /i,
  );
  assert.match(runtime, /data-role="refresh">刷新直连状态<\/button>/);
  assert.doesNotMatch(
    runtime,
    /data-role="(?:endpoint|code|pair|forget)"|一次性配对码|忘记此桥接器/,
  );
});

test("Reader 直连状态明确区分缺少虚拟线缆与桥接器离线", () => {
  assert.match(
    runtime,
    /BW_COMPUTER_VOICE_DIRECT_RENDER_ENDPOINT_UNAVAILABLE:\s*"两根虚拟音频线缆尚未安装、失活或配置不匹配"/,
  );
  assert.match(
    runtime,
    /BW_COMPUTER_VOICE_DIRECT_OUTPUT_ROUTE_UNVERIFIED:\s*"尚未验证 Codex\/ChatGPT 输出已固定到虚拟扬声器 B"/,
  );
  assert.match(
    runtime,
    /桥接器不会创建或安装虚拟设备，A\/B 必须是 Windows 已有的两根独立虚拟音频线/,
  );
  assert.match(runtime, /Windows 桥接器离线或电脑正在睡眠/);
});

test("扩展版本与后台构建版本保持一致", () => {
  const match = background.match(
    /__BW_READER_BACKGROUND_BUILD_VERSION = "([^"]+)"/,
  );
  assert.equal(match?.[1], manifest.version);
});

for (const [template, version] of [
  ["_server_deploy/templates/pdf_reader.html", "shared_js_v"],
  ["_server_deploy/templates/epub_html_reader.html", "reader_js_v"],
  ["_server_deploy/templates/html_reader.html", "reader_js_v"],
]) {
  test(`${template} 在 voicecall 前加载唯一电脑桥运行时`, () => {
    const html = read(template);
    const entry = html.indexOf(
      `/static/pdf/rc-computer-voice.js?v={{ ${version} }}`,
    );
    const call = html.indexOf(
      `/static/pdf/rc-voicecall.js?v={{ ${version} }}`,
    );
    assert.ok(entry >= 0 && call > entry);
    assert.equal(
      html.split(
        `/static/pdf/rc-computer-voice.js?v={{ ${version} }}`,
      ).length - 1,
      1,
    );
  });
}
