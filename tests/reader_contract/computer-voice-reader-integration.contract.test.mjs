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
const facade = read("extensions/bw-reader-webext/src/facade.js");
const callPage = read("extensions/bw-reader-webext/call.js");
const contentScript = read("extensions/bw-reader-webext/content.js");
const safariPackager = read("extensions/bw-reader-webext/package_safari.py");
const offscreen = read("extensions/bw-reader-webext/offscreen.js");
const readerWebView = read("ios/BWReader/App/ReaderWebView.swift");
const nativeVoiceSystem = read(
  "ios/BWReader/App/NativeVoiceSystemIntegration.swift",
);

test("电脑客户端保留原按钮与设置标签，App 与扩展按宿主分流", () => {
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
    /function _nativeComputerVoiceAppAvailable\(\)[\s\S]*window\.__BW_NATIVE_COMPUTER_VOICE__ === true[\s\S]*bwNativeComputerVoice\.postMessage[\s\S]*_extensionComputerVoiceDirectAvailable\(\)/,
  );
  assert.match(
    voicecall,
    /function _toggleNativeComputerVoiceApp\(\)[\s\S]*getTargetApp[\s\S]*postTarget\(current\)/,
  );
  const nativeToggle = voicecall.slice(
    voicecall.indexOf("function _toggleNativeComputerVoiceApp()"),
    voicecall.indexOf("function _computerVoiceActive()"),
  );
  assert.doesNotMatch(nativeToggle, /loadTargetApp\(\)\.then/);
  assert.match(
    voicecall,
    /function postTarget\(appKind\)[\s\S]*bwNativeComputerVoice\.postMessage\(\{[\s\S]*action: 'toggle',[\s\S]*appKind:/,
  );
  assert.match(
    nativeToggle,
    /if \(_computerVoiceStarting \|\| _computerVoiceActive\(\)\)[\s\S]*_stopComputerVoiceOnly\('extension-computer-button'\)/,
  );
  assert.match(
    nativeToggle,
    /_computerVoiceStart\(\{ appKind: target \}, _gen\)/,
  );
  assert.doesNotMatch(
    nativeToggle,
    /__bwNativeComputerVoiceExtensionBridge|bridge\.toggle|window\.location\.assign|voice\.toggle/,
  );
  assert.match(runtime, /rt_computer_target/);
  assert.match(runtime, /<option value="codex-desktop">Codex<\/option>/);
  assert.match(runtime, /<option value="chatgpt-classic">GPT Classic<\/option>/);
  assert.match(runtime, /appKind: state\.appKind/);
  assert.match(
    runtime,
    /if \(!response \|\| response\.ok !== true \|\|[\s\S]*typeof response\.json !== "function"\)/,
  );
});

test("电脑客户端设置读取真实 Codex 语音状态并按目标状态控制一次快捷键", () => {
  assert.match(runtime, /\["codexVoice"\][\s\S]*STATUS 响应/);
  assert.match(
    runtime,
    /function normalizeCodexVoicePayload[\s\S]*"available"[\s\S]*"unavailable"[\s\S]*"error"/,
  );

  const setStart = runtime.indexOf("function setCodexVoiceActive(desiredActive)");
  const setEnd = runtime.indexOf("function makeAudioSurface()", setStart);
  assert.ok(setStart >= 0 && setEnd > setStart);
  const setBody = runtime.slice(setStart, setEnd);
  assert.match(
    setBody,
    /opened\.request\([\s\S]*"codex-voice-set"[\s\S]*\{ active: desiredActive \}/,
  );
  assert.match(setBody, /if \(codexVoiceSetPromise\) return codexVoiceSetPromise/);
  assert.doesNotMatch(setBody, /request\(\s*"(?:start|stop)"/);

  const settingsStart = runtime.indexOf("function mountSettings(container)");
  const settingsEnd = runtime.indexOf("// Preference-only read", settingsStart);
  const settingsBody = runtime.slice(settingsStart, settingsEnd);
  assert.match(settingsBody, /data-role="codex-voice-status"/);
  assert.match(settingsBody, /data-role="codex-voice-toggle"/);
  assert.match(settingsBody, /● Codex 正在使用麦克风（通常表示语音已开启）/);
  assert.match(settingsBody, /○ Codex 当前未使用麦克风（通常表示语音已关闭）/);
  assert.match(
    settingsBody,
    /var desiredActive = !latestCodexVoice\.active;[\s\S]*setCodexVoiceActive\(desiredActive\)/,
  );
  assert.match(
    settingsBody,
    /codexVoiceToggle\.addEventListener\("click", function \(event\)[\s\S]*event\.isTrusted !== true[\s\S]*setCodexVoiceActive\(desiredActive\)/,
    "a third-party host must not synthesize the privileged Windows shortcut click",
  );

  const refreshStart = settingsBody.indexOf("function refresh()");
  const refreshEnd = settingsBody.indexOf("root.__rcComputerVoiceRefresh", refreshStart);
  const refreshBody = settingsBody.slice(refreshStart, refreshEnd);
  assert.match(refreshBody, /availability\(\)\.then\(render\)/);
  assert.doesNotMatch(refreshBody, /setCodexVoiceActive|codex-voice-set/);
});

test("电脑按钮按宿主分流，普通电话保持独立", () => {
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
  assert.match(computerClick, /if \(_reviewVoiceGate\(true\)\) return/);
  assert.match(
    computerClick,
    /if \(!_nativeComputerVoiceAppAvailable\(\)\)[\s\S]*return/,
  );
  assert.match(computerClick, /if \(ws \|\| _rtc\.on \|\| _connecting \|\| _reconnT \|\| _reconnPend\)[\s\S]*teardown\(false, true\)/);
  assert.match(computerClick, /_toggleNativeComputerVoiceApp\(\)/);
  assert.doesNotMatch(
    computerClick,
    /_computerVoiceStart|_setComputerVoiceDialPending|startFromUserGesture/,
  );
  assert.ok(
    computerClick.indexOf("_reviewVoiceGate(true)") <
      computerClick.indexOf("_nativeComputerVoiceAppAvailable()") &&
      computerClick.indexOf("_nativeComputerVoiceAppAvailable()") <
        computerClick.indexOf("teardown(false, true)") &&
      computerClick.indexOf("teardown(false, true)") <
        computerClick.indexOf("_toggleNativeComputerVoiceApp()"),
    "gate and host availability must be checked before releasing ordinary voice and dispatching the computer route",
  );

  const phoneClickStart = computerClickEnd;
  const phoneClickEnd = voicecall.indexOf(
    "var tb = document.createElement('button')",
    phoneClickStart,
  );
  assert.ok(phoneClickEnd > phoneClickStart);
  const phoneClick = voicecall.slice(phoneClickStart, phoneClickEnd);
  assert.match(phoneClick, /window\._voiceCallS2S/);
  assert.doesNotMatch(
    phoneClick,
    /_toggleNativeComputerVoiceApp|_computerVoiceStart|_setComputerVoiceDialPending|startFromUserGesture/,
  );
});

test("Safari 扩展电脑按钮直接启停 RC bridge，App WebView 仍走 postMessage", () => {
  assert.match(
    facade,
    /window\.__BW_NATIVE_APP_COMPUTER_VOICE__ = true/,
  );
  assert.doesNotMatch(
    facade,
    /window\.__BW_NATIVE_COMPUTER_VOICE__ = true/,
  );
  assert.match(
    voicecall,
    /function _extensionComputerVoiceDirectAvailable\(\)[\s\S]*window\.chrome && window\.chrome\.runtime[\s\S]*runtime\.connect[\s\S]*RC\.computerVoice\.startFromUserGesture/,
  );
  const nativeToggle = voicecall.slice(
    voicecall.indexOf("function _toggleNativeComputerVoiceApp()"),
    voicecall.indexOf("function _setComputerVoiceDialPending"),
  );
  assert.match(
    nativeToggle,
    /window\.webkit[\s\S]*bwNativeComputerVoice\.postMessage\(\{[\s\S]*action: 'toggle',[\s\S]*appKind: target/,
  );
  assert.match(
    nativeToggle,
    /_extensionComputerVoiceDirectAvailable\(\)[\s\S]*_stopComputerVoiceOnly\('extension-computer-button'\)[\s\S]*_computerVoiceStart\(\{ appKind: target \}, _gen\)/,
  );
  assert.doesNotMatch(
    nativeToggle,
    /__bwNativeComputerVoiceExtensionBridge|bridge\.toggle|window\.location\.assign|voice\.toggle/,
  );
  const phoneClickStart = voicecall.indexOf("b.addEventListener('click'");
  const phoneClickEnd = voicecall.indexOf(
    "var tb = document.createElement('button')",
    phoneClickStart,
  );
  const phoneClick = voicecall.slice(phoneClickStart, phoneClickEnd);
  assert.doesNotMatch(
    phoneClick,
    /BW_NATIVE_APP_REQUEST|sendNativeMessage|nativeComputerVoiceExtensionBridge/,
  );
});

test("Safari 侧栏电脑按钮在原位置嵌入扩展通话页直连，不依赖后台 relay", () => {
  assert.match(facade, /\^\(safari-web-extension\|chrome-extension\|moz-extension\)/);
  assert.match(facade, /runtime\.getURL\('call\.html\?compact=1'\)/);
  assert.match(facade, /setAttribute\('allow', 'microphone; autoplay'\)/);
  assert.match(facade, /getElementById\('asst-computer'\)/);
  assert.match(facade, /getElementById\('vc-top-computer'\)/);
  assert.match(
    callPage,
    /RC\.computerVoice\.startFromUserGesture/,
  );
  assert.doesNotMatch(
    callPage,
    /runtime\.connect|sendNativeMessage/,
  );
  assert.match(
    safariPackager,
    /"resources": \["call\.html", "inline-computer-voice\.html"\][\s\S]*"matches": \["https:\/\/\*\/\*", "http:\/\/\*\/\*"\]/,
  );
});

test("电脑按钮身份仍由 voicecall 闭包登记，App/扩展以外环境统一禁用", () => {
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
    /function _extensionComputerVoiceDirectAvailable\(\)[\s\S]*runtime\.id[\s\S]*runtime\.connect[\s\S]*startFromUserGesture/,
  );
  assert.match(
    voicecall,
    /_computerVoiceOwnedButtons\.has\(button\)[\s\S]*RC\.computerVoice\.registerComputerButton\(button\)/,
  );
  assert.match(voicecall, /_ownComputerVoiceButton\(c\)/);
  assert.match(voicecall, /_ownComputerVoiceButton\(tm\)/);
  assert.match(
    voicecall,
    /function _configureNativeComputerVoiceButton\(button\)[\s\S]*button\.disabled = !available[\s\S]*button\.classList\.toggle\('native-app-required', !available\)[\s\S]*button\.setAttribute\('aria-disabled', available \? 'false' : 'true'\)/,
  );
  assert.match(voicecall, /_configureNativeComputerVoiceButton\(c\)/);
  assert.match(voicecall, /_configureNativeComputerVoiceButton\(tm\)/);
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

test("BWReader App 接管电脑按钮后，网页运行时不会预备麦克风或发送 START", () => {
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
  const gestureStart = runtime.indexOf("function installGestureCapture()");
  const gestureEnd = runtime.indexOf("installGestureCapture();", gestureStart);
  assert.ok(gestureStart >= 0 && gestureEnd > gestureStart);
  const gestureCapture = runtime.slice(gestureStart, gestureEnd);
  assert.ok(
    gestureCapture.indexOf("window.__BW_NATIVE_COMPUTER_VOICE__ === true") >= 0 &&
      gestureCapture.indexOf("window.__BW_NATIVE_COMPUTER_VOICE__ === true") <
        gestureCapture.indexOf("prepareSurfaceFromGesture()"),
    "App guard must return before browser microphone preparation",
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

test("App 原生语音与 Reader 上下文使用独立 WSS，语音启停不再切断快照", () => {
  assert.match(
    runtime,
    /function nativeReaderUsesDedicatedContextLink\(\)[\s\S]*window\.__BW_NATIVE_COMPUTER_VOICE__ === true[\s\S]*contextDeliveryMode === CONTEXT_DELIVERY_SNAPSHOT/,
  );
  assert.match(
    runtime,
    /function nativeContextRequest\(action, fields, timeoutMs\)[\s\S]*action !== "context" && action !== "active-reading"[\s\S]*bwNativeComputerContext\.postMessage\(\{[\s\S]*requestId:[\s\S]*action:[\s\S]*fields:/,
  );
  assert.match(
    runtime,
    /window\.__bwNativeComputerContextApplyResult = applyNativeContextResult/,
  );
  assert.match(
    runtime,
    /function prepareNativeContextHandoff\(\)[\s\S]*nativeContextHandoffPending = true[\s\S]*RC\.ctxSync\.getConfig\(\)[\s\S]*contextDeliveryMode = value\.deliveryMode[\s\S]*nativeReaderUsesDedicatedContextLink\(\)[\s\S]*return reconcileSnapshotLink\(\)/,
  );
  assert.match(
    runtime,
    /function snapshotLinkWanted\(\)[\s\S]*independentOfVoice[\s\S]*readerContextSurfaceVisible\(\)[\s\S]*independentOfVoice \|\|[\s\S]*!nativeComputerVoiceOwnsWss\(\)/,
  );
  assert.match(
    runtime,
    /function reconcileNativeContextRelay\(\)[\s\S]*nativeReaderUsesDedicatedContextLink\(\)[\s\S]*stopNativeContextRelay\(\)[\s\S]*return reconcileSnapshotLink\(\)/,
  );
  assert.match(
    runtime,
    /"bw-native-reader-foreground",\s*resumeSnapshotLinkFromForeground/,
  );
  assert.match(
    runtime,
    /function setContextDeliveryMode\(mode\)[\s\S]*if \(nativeComputerVoiceOwnsWss\(\)\)[\s\S]*BW_READER_CONTEXT_DELIVERY_MODE_NATIVE_BUSY/,
  );
  const nativeRelay = runtime.slice(
    runtime.indexOf("function startNativeContextRelay(sessionId)"),
    runtime.indexOf("function reconcileNativeContextRelay()"),
  );
  assert.match(nativeRelay, /startContextPump\(state\)/);
  assert.match(
    nativeRelay,
    /contextDeliveryMode === CONTEXT_DELIVERY_SNAPSHOT[\s\S]*startActiveReadingPump\(state\)/,
  );
  assert.match(
    runtime,
    /function stopNativeContextRelay\(\)[\s\S]*state\.stopped = true[\s\S]*stopContextPump\(state\)[\s\S]*rejectNativeContextRequests/,
  );
  assert.match(
    runtime,
    /"bw-native-computer-voice-state",\s*reconcileNativeContextRelay/,
  );
  assert.match(
    runtime,
    /GPT Classic：[\s\S]*“测试旧版文字注入”[\s\S]*实时快照\/MCP 仍属于 Codex/,
  );
});

test("扩展上下文按设置和前台状态独立运行，不随语音停止", () => {
  for (const source of [callPage, contentScript]) {
    assert.match(source, /bwReaderExtensionPreferencesV2/);
    assert.match(source, /eph-ctx-sync/);
    assert.match(source, /chrome\.storage\.onChanged/);
    assert.match(source, /document\.visibilityState/);
  }
  assert.match(contentScript, /\["pageshow", "focus", "online"\]/);
  assert.match(contentScript, /contentDigest\(snap\.text\)/);
  assert.match(contentScript, /ACTIVE_CONTEXT_KEY = "bwActivePageContextV1"/);
  assert.match(
    contentScript,
    /extensionStore\.set\(ACTIVE_CONTEXT_KEY, envelope\)/,
  );
  assert.match(contentScript, /extensionStore\.get\(PREFERENCE_KEY\)/);
  assert.doesNotMatch(
    contentScript,
    /Promise\.resolve\(chrome\.storage\.local\.get\(PREFERENCE_KEY\)\)/,
  );
  assert.match(background, /LOCAL_STORAGE_KEYS = new Set\(\[[\s\S]*"bwActivePageContextV1"/);
  assert.match(callPage, /ACTIVE_CONTEXT_KEY = "bwActivePageContextV1"/);
  assert.match(callPage, /function storedPage\(value\)/);
  assert.match(callPage, /changes\[ACTIVE_CONTEXT_KEY\][\s\S]*forward\(page, true\)/);
  const forwardStart = callPage.indexOf("async function forward(page, force)");
  const forwardEnd = callPage.indexOf("function storedPage", forwardStart);
  const forwardBody = callPage.slice(forwardStart, forwardEnd);
  assert.ok(
    forwardBody.indexOf("await current.send(page)") <
      forwardBody.indexOf("lastSignature = signature"),
    "page deduplication may advance only after Windows accepts the snapshot",
  );
  assert.match(callPage, /function closeContextLink\(\)/);
  assert.match(callPage, /if \(!EMBEDDED\) closeWhenDone/);
  const closeWhenDone = callPage.slice(
    callPage.indexOf("function closeWhenDone"),
    callPage.indexOf("// --- embedded form", callPage.indexOf("function closeWhenDone")),
  );
  assert.doesNotMatch(closeWhenDone, /closeContextLink|link\.close/);
});

test("App 电脑按钮兼容缓存的一字段 Codex 消息，版本号取自安装包", () => {
  assert.match(
    readerWebView,
    /if body\.count == 1, body\["appKind"\] == nil[\s\S]*appKind = \.codexDesktop/,
  );
  assert.match(
    readerWebView,
    /body\.count == 2[\s\S]*DirectVoiceTargetApp\(rawValue: rawAppKind\)/,
  );
  assert.match(
    nativeVoiceSystem,
    /CFBundleShortVersionString/,
  );
  assert.doesNotMatch(nativeVoiceSystem, /nativeAppBuildVersion = "\d/);
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
  assert.match(
    runtime,
    /function normalizeEndpoint\(value\)[\s\S]*url\.toString\(\) !== DIRECT_ENDPOINT[\s\S]*url\.toString\(\) !== CONTEXT_ENDPOINT[\s\S]*return url\.toString\(\)/,
  );
  assert.match(
    runtime,
    /function normalizeEndpoint\(value\)[\s\S]*url\.toString\(\) !== DIRECT_ENDPOINT &&[\s\S]*url\.toString\(\) !== CONTEXT_ENDPOINT/,
  );
  assert.match(
    runtime,
    /function openDirect\(options, onCreate\)[\s\S]*var endpoint = \(options && options\.endpoint\) \|\| DIRECT_ENDPOINT;[\s\S]*new DirectSocket\(endpoint, options\)/,
  );
  assert.match(
    runtime,
    /var snapshotEndpoint = \([\s\S]*window\.__BW_NATIVE_COMPUTER_VOICE__ === true[\s\S]*\? CONTEXT_ENDPOINT : DIRECT_ENDPOINT[\s\S]*endpoint: snapshotEndpoint/,
  );
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
