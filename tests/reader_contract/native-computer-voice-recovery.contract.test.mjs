import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";

const BRIDGE = readFileSync(
  "ios/BWReader/App/NativeVoiceBridge.swift",
  "utf8",
);
const APP = readFileSync(
  "ios/BWReader/App/BWReaderNativeApp.swift",
  "utf8",
);
const SOCKET = readFileSync(
  "ios/BWReader/App/DirectVoiceSocket.swift",
  "utf8",
);
const WEB = readFileSync(
  "ios/BWReader/App/ReaderWebView.swift",
  "utf8",
);
const AUDIO = readFileSync(
  "ios/BWReader/App/NativeAudioEngine.swift",
  "utf8",
);

test("App 电脑语音用封顶退避恢复明确的 Windows 清理中响应", () => {
  assert.match(BRIDGE, /reconnectDelayNanoseconds:[\s\S]*15_000_000_000/);
  assert.match(BRIDGE, /BW_COMPUTER_VOICE_DIRECT_MEDIA_CLEANUP_PENDING/);
  assert.match(BRIDGE, /startRequestSent:[\s\S]*confirmedRecoveryRejectionCodes/);
  assert.match(BRIDGE, /resumeArmed = true[\s\S]*scheduleReconnect\(trigger: code\)/);
});

test("恢复仍对未知 START 结果 fail closed 且不自动 takeover", () => {
  const recovery = BRIDGE.slice(BRIDGE.indexOf("private func performRecovery"));
  assert.match(recovery, /newSocket\.start\(appKind: activeAppKind\)/);
  assert.doesNotMatch(recovery, /newSocket\.start\([\s\S]{0,160}takeover: true/);
  assert.match(BRIDGE, /if !startRequestSent \{[\s\S]*return true/);
  assert.match(BRIDGE, /confirmedRecoveryRejectionCodes\.contains\(failure\.code\)/);
});

test("App 回到前台会重新唤醒仍有用户意图的 suspended 会话", () => {
  assert.match(APP, /voiceBridge\.setAppForeground\(phase == \.active\)/);
  assert.match(
    BRIDGE,
    /func setAppForeground[\s\S]*desiredActive[\s\S]*resumeArmed[\s\S]*state\.phase == \.suspended[\s\S]*immediate: true/,
  );
});

test("App 前台原生巡检会主动验活并恢复 Reader 快照专线", () => {
  assert.match(
    APP,
    /guard scenePhase == \.active[\s\S]*reader\.probeReaderSnapshotLink\(\)[\s\S]*secondsUntilSnapshotRefresh = 12[\s\S]*reader\.probeReaderSnapshotLink\(\)/,
  );
  assert.match(
    WEB,
    /func probeReaderSnapshotLink\(\)[\s\S]*guard readerForeground, webView\.url != nil[\s\S]*"bw-native-reader-foreground"[\s\S]*probe: true/,
  );
});

test("App 快照巡检证明当前页发布而不只读取旧模式缓存", () => {
  assert.match(
    WEB,
    /probeReaderSnapshotLink[\s\S]*probe: true/,
  );
  const source = readFileSync(
    "_server_deploy/static/pdf/rc-computer-voice.js",
    "utf8",
  );
  assert.match(
    source,
    /function proveSnapshotPublication[\s\S]*request\("active-reading"[\s\S]*normalizeActiveReadingAck/,
  );
  assert.match(
    source,
    /event\.detail\.probe === true[\s\S]*proveSnapshotPublication\(state\)/,
  );
});

test("App 本地音频掉线复用现有 WSS 恢复且不重复申请权限或 START", () => {
  assert.match(AUDIO, /func restart\(\) throws[\s\S]*stopOnControlQueue\(\)[\s\S]*startOnControlQueue\(\)/);
  assert.match(AUDIO, /AVAudioEngineConfigurationChange/);
  assert.match(AUDIO, /routeChangeNotification/);
  assert.match(AUDIO, /mediaServicesWereResetNotification/);
  assert.match(BRIDGE, /startAudioHealthWatchdog[\s\S]*5_000_000_000[\s\S]*audio\.isOperational/);

  const localRecovery = BRIDGE.slice(
    BRIDGE.indexOf("private func performLocalAudioRecovery"),
    BRIDGE.indexOf("private func startSafariContextPump"),
  );
  assert.match(localRecovery, /try audio\.restart\(\)/);
  assert.match(localRecovery, /try startMicrophonePipeline/);
  assert.match(localRecovery, /scheduleLocalAudioRecovery\(trigger: "local-audio-recovery-failed"\)/);
  assert.doesNotMatch(localRecovery, /requestMicrophonePermission|\.start\(appKind:|scheduleReconnect|failActiveSession/);
});

test("iOS 音频中断只暂停本地音频并保持 Windows 会话", () => {
  const interruption = BRIDGE.slice(
    BRIDGE.indexOf("private func handleAudioInterruption"),
    BRIDGE.indexOf("private func handleNetworkPath"),
  );
  assert.match(interruption, /suspendLocalAudio/);
  assert.match(interruption, /scheduleLocalAudioRecovery/);
  assert.doesNotMatch(interruption, /suspendActiveSession|failActiveSession/);
});

test("只有只读心跳超时会允许自动重连", () => {
  assert.match(
    SOCKET,
    /action == "start"[\s\S]*BW_COMPUTER_VOICE_DIRECT_START_UNKNOWN[\s\S]*retryable: action == "heartbeat"/,
  );
  assert.match(
    BRIDGE,
    /recoverableCodes:[\s\S]*BW_COMPUTER_VOICE_DIRECT_TIMEOUT[\s\S]*failure\.retryable/,
  );
  assert.doesNotMatch(SOCKET, /retryable:\s*action\s*!=\s*"start"/);
});
