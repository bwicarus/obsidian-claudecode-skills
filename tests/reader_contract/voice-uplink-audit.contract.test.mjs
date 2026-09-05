import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const CS = "extensions/bw-reader-webext/windows/ComputerVoiceAudio/";
const PY = "extensions/bw-reader-webext/windows/computer-voice-desktop/";
const PROFILE = read(CS + "DirectAppTargetProfile.cs");
const CONTRACT = read(CS + "DirectBridgeContract.cs");
const ACTIVITY = read(CS + "CodexVoiceActivity.cs");
const ADAPTERS = read(CS + "DirectBridgeAdapters.cs");
const SERVER = read(CS + "DirectBridgeServer.cs");
const RENDER = read(CS + "VirtualMicrophoneRenderSession.cs");
const PACKETS = read(CS + "PcmCaptureContract.cs");
const WINDOWS_ADAPTERS = read(CS + "WindowsDirectAdapters.cs");
const DUCKING = read(CS + "AudioSessionDuckingInterop.cs");
const SELFTEST = read(CS + "ContractSelfTest.cs");
const BRIDGE_CORE = read(PY + "bridge_core.py");
const SERVICES = read(PY + "readerpc_services.py");
const LAUNCHER = read(PY + "readerpc_launcher.py");

// 2026-09-06：语音上行审计（用户："只需要保证桥接后 codex 不会在某些情况无法获得语音输入"）
// 与 Codex 正式版切换（用户："从现在开始使用 gpt 的普通版本而不是 beta"）的契约。

test("Codex 目标默认正式版：三份身份副本一致，Beta 只在回滚目标里出现", () => {
  const stableFamily = "OpenAI.Codex_2p2nqsd0c76g0";
  assert.match(CONTRACT, /CodexAppUserModelId =\s*\n\s*"OpenAI\.Codex_2p2nqsd0c76g0!App";/);
  assert.match(BRIDGE_CORE, /"codex-desktop": "OpenAI\.Codex_2p2nqsd0c76g0!App",/);
  assert.match(SERVICES, /ConsentStore\\microphone\\OpenAI\.Codex_2p2nqsd0c76g0"/);
  assert.match(ACTIVITY, /\+ "OpenAI\.Codex_2p2nqsd0c76g0";/);
  // 正式版的进程/映像名是 ChatGPT / ChatGPT.exe（本机实测），路径标记带下划线不会前缀命中 Beta。
  const codexProfile = PROFILE.slice(
    PROFILE.indexOf("private static readonly DirectAppTargetProfile Codex = new("),
    PROFILE.indexOf("private static readonly DirectAppTargetProfile CodexBeta = new("));
  assert.match(codexProfile, /@"\\WindowsApps\\OpenAI\.Codex_",/);
  assert.match(codexProfile, new RegExp(`"${stableFamily}",`));
  assert.match(codexProfile, /ProcessName: "ChatGPT",/);
  assert.match(codexProfile, /ExecutableSuffix: @"\\ChatGPT\.exe"\);/);
  assert.doesNotMatch(codexProfile, /CodexBeta|\(Beta\)/, "默认目标里不许再有 Beta");
  // Beta 保留为独立目标，供回滚。
  assert.match(PROFILE, /internal const string CodexDesktopBeta = "codex-desktop-beta";/);
  assert.match(PROFILE, /CodexDesktopBeta => CodexBeta,/);
  assert.match(BRIDGE_CORE, /"codex-desktop-beta": "OpenAI\.CodexBeta_2p2nqsd0c76g0!App",/);
  // 语音球隐藏按映像名过滤，正式版的 chatgpt.exe 必须在名单里。
  assert.match(LAUNCHER, /"chatgpt\.exe"/, "语音球扫描要认正式版的 chatgpt.exe");
});

test("C03：服务换代/重启不撤销 keepalive —— 撤销就是按 F24 把活着的通话关掉", () => {
  const stop = LAUNCHER.slice(
    LAUNCHER.indexOf("def stop_readerpc_voice("),
    LAUNCHER.indexOf("def ", LAUNCHER.indexOf("def stop_readerpc_voice(") + 10));
  assert.match(stop, /if disable_configuration:\s*\n\s*try:\s*\n\s*set_codex_voice_keep_active\(bridge_paths, False\)/);
  assert.doesNotMatch(stop.split("if disable_configuration:")[0], /set_codex_voice_keep_active\(bridge_paths, False\)/,
    "disable_configuration 之外不许撤销 keepalive");
});

test("C04：AI 声音的下行队列满了丢最旧，不 fail-closed；默认构造仍 fail-closed", () => {
  assert.match(PACKETS, /bool dropOldestWhenFull = false\)/);
  assert.match(PACKETS, /_droppedPackets \+= 1;/);
  assert.match(WINDOWS_ADAPTERS, /BoundedPcmPacketQueue outputQueue = new\(\s*\n\s*32,\s*\n\s*2 \* 1024 \* 1024,\s*\n\s*dropOldestWhenFull: true\);/);
  assert.match(SELFTEST, /downlink-packet-queue-drops-oldest-instead-of-failing-closed/);
  assert.match(SELFTEST, /packet-queue-default-still-fails-closed/);
});

test("C05：状态文件只在状态真变了时写，写失败不拆通话", () => {
  assert.match(SERVER, /string\? lastWrittenStatusState = null;/);
  assert.match(SERVER, /if \(nextStatusState != lastWrittenStatusState\)/);
  assert.match(SERVER, /lastWrittenStatusState = nextStatusState;/);
});

test("C06：上行抖动队列 400 ms，满了先丢静音再丢最旧", () => {
  assert.match(RENDER, /internal const int MaximumBufferedMilliseconds = 400;/);
  assert.match(RENDER, /internal const double SilentFrameRms = 200;/);
  assert.match(RENDER, /DropOneFramePreferringSilence\(\);/);
  assert.match(RENDER, /UplinkSpeechEndDetector\.Rms\(frames\[index\]\) < SilentFrameRms/);
  assert.match(SELFTEST, /virtual-mic-uplink-queue-drops-silence-before-speech/);
  assert.match(SELFTEST, /MaximumBufferedMilliseconds == 400/);
});

test("C01：虚拟麦克风渲染会话退出 Windows 通讯闪避，结果进双工诊断", () => {
  assert.match(DUCKING, /\[Guid\("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D"\)\]/, "要按 IAudioSessionControl2 的 IID 去 QI");
  assert.match(DUCKING, /control\.SetDuckingPreference\(optOut: true\)/);
  assert.match(RENDER, /AudioSessionDucking\.LastVirtualMicrophoneOptOut =\s*\n\s*AudioSessionDucking\.TryOptOut\(_audioClient\);/);
  assert.match(ADAPTERS, /\["virtualMicDucking"\] = AudioSessionDucking\.LastVirtualMicrophoneOptOut,/);
});

test("媒体「意外停止」要带上最近一次停止原因", () => {
  assert.match(ADAPTERS, /internal string\? LastMediaStopReason => _lastMediaStopReason;/);
  for (const reason of ["takeover-by-new-start:", "stop-request:", "connection-closed:", "fault:", "coordinator-dispose"]) {
    assert.match(ADAPTERS, new RegExp(reason.replace(/[-:]/g, (m) => "\\" + m)), `停止原因缺 ${reason}`);
  }
  assert.match(SERVER, /_coordinator\.LastMediaStopReason is string stopReason/);
});
