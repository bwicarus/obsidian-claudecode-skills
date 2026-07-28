import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const read = (path) => fs.readFileSync(path, "utf8");
const manifest = JSON.parse(read("extensions/bw-reader-webext/manifest.json"));
const full = manifest.content_scripts[1].js;
const assistant = read("_server_deploy/static/pdf/rc-assistant.js");
const voicecall = read("_server_deploy/static/pdf/rc-voicecall.js");
const runtime = read("_server_deploy/static/pdf/rc-computer-voice.js");
const background = read("extensions/bw-reader-webext/background.js");
const offscreen = read("extensions/bw-reader-webext/offscreen.js");
const offscreenHtml = read("extensions/bw-reader-webext/offscreen.html");
const popup = read("extensions/bw-reader-webext/popup.js");
const installer = read(
  "extensions/bw-reader-webext/windows/install-computer-voice-native-host.ps1",
);

test("电脑客户端选项复用共享设置和电话按钮，选择本身不启动", () => {
  assert.match(assistant, /value="computer_client"/);
  assert.match(assistant, /RC\.computerVoice\.mountSettings\(card\)/);
  assert.doesNotMatch(
    assistant,
    /computer_client[\s\S]{0,300}startFromUserGesture/,
  );
  assert.match(
    voicecall,
    /engine === 'computer_client'\) _computerVoiceStart\(opts, g0\)/,
  );
  assert.match(
    voicecall,
    /RC\.computerVoice\.startFromUserGesture\(opts \|\| \{\}\)/,
  );
});

test("Reader 端只接收双轨，应用输出播放而麦克风不回放", () => {
  assert.match(runtime, /ROLE_READER_RECEIVER/);
  assert.match(runtime, /TRACK_APP_OUTPUT/);
  assert.match(runtime, /TRACK_USER_MIC/);
  assert.match(runtime, /surface\.outputStream\.addTrack\(event\.track\)/);
  assert.match(runtime, /surface\.micStream\.addTrack\(event\.track\)/);
  assert.doesNotMatch(runtime, /audio\.srcObject\s*=\s*surface\.micStream/);
  assert.doesNotMatch(runtime, /getUserMedia|mediaDevices/);
});

test("内容脚本仅可访问 Reader 侧路由，设备凭据路由不暴露给页面", () => {
  assert.match(background, /computer-voice\\\/devices\\\/\[A-Za-z0-9/);
  assert.match(background, /computer-voice\\\/sessions\\\//);
  assert.doesNotMatch(
    background,
    /BW_FETCH_DYNAMIC_ROUTES[\s\S]*computer-voice\\\/device\\\//,
  );
  assert.doesNotMatch(runtime, /deviceToken|Authorization|commands\/claim/);
});

test("扩展按唯一共享顺序加载 WebRTC、Reader 入口和 voicecall", () => {
  const rtc = full.indexOf("vendor/reader-runtime-computer-voice-webrtc.js");
  const entry = full.indexOf("vendor/rc-computer-voice.js");
  const call = full.indexOf("vendor/rc-voicecall.js");
  assert.ok(rtc >= 0 && entry > rtc && call > entry);
});

test("offscreen 媒体桥只在扩展私有上下文加载原生合同与 WebRTC", () => {
  assert.ok(manifest.permissions.includes("nativeMessaging"));
  assert.ok(manifest.permissions.includes("offscreen"));
  const native = offscreenHtml.indexOf("src/computer-voice-native-protocol.js");
  const rtc = offscreenHtml.indexOf(
    "vendor/reader-runtime-computer-voice-webrtc.js",
  );
  const host = offscreenHtml.indexOf("offscreen.js");
  assert.ok(native >= 0 && rtc > native && host > rtc);
  assert.match(offscreen, /chrome\.runtime\.connectNative\(NATIVE_HOST\)/);
  assert.match(offscreen, /MediaStreamTrackGenerator/);
  assert.match(offscreen, /new AudioData/);
  assert.doesNotMatch(offscreen, /getUserMedia|system-wide|default-device/);
  assert.doesNotMatch(offscreen, /Bearer /);
});

test("Windows 发送端先建立 WebRTC，再启动原生采集和一次性快捷键", () => {
  const flow = offscreen.match(
    /await current\.controller\.start[\s\S]*?await waitForConnected\(current\)[\s\S]*?nativePort\.postMessage\(message\)/,
  );
  assert.ok(flow, "WebRTC 必须先连通，之后才能向原生宿主发送 start");
  assert.match(offscreen, /authorization:\s*\{[\s\S]*oneTimeTrigger: true/);
  assert.match(offscreen, /outputTarget: "codex-desktop"/);
  assert.match(offscreen, /companion:[\s\S]*kind: "voice-typist"/);
});

test("首次配对凭据只保存在扩展 storage，页面只显示一次性 pairId/code", () => {
  assert.match(popup, /BW_COMPUTER_VOICE_PAIR/);
  assert.match(offscreen, /chrome\.storage\.local\.set/);
  assert.match(offscreen, /deviceToken: randomToken\(48\)/);
  assert.doesNotMatch(runtime, /deviceToken|chrome\.storage/);
  assert.match(runtime, /"配对 ID " \+ value\.pairId/);
});

test("Windows 安装器要求交互桌面、显式麦克风与 ENABLE，并可撤销", () => {
  assert.match(installer, /SessionId -eq 0/);
  assert.match(installer, /Get-CaptureEndpoints/);
  assert.match(installer, /Read-Host "Type ENABLE exactly to opt in"/);
  assert.match(installer, /\$confirmation -cne "ENABLE"/);
  assert.match(installer, /localOptIn = \$true/);
  assert.match(installer, /localOptIn = \$false/);
  assert.match(installer, /outputScope = "process-only"/);
  assert.match(installer, /systemOutputFallback = \$false/);
  assert.doesNotMatch(installer, /ExecutionPolicy|Set-ExecutionPolicy/);
});

for (const [template, version] of [
  ["_server_deploy/templates/pdf_reader.html", "shared_js_v"],
  ["_server_deploy/templates/epub_html_reader.html", "reader_js_v"],
  ["_server_deploy/templates/html_reader.html", "reader_js_v"],
]) {
  test(`${template} 在 voicecall 前加载唯一电脑桥运行时`, () => {
    const html = read(template);
    const rtc = html.indexOf(
      `/static/reader-runtime/computer-voice-webrtc.js?v={{ ${version} }}`,
    );
    const entry = html.indexOf(
      `/static/pdf/rc-computer-voice.js?v={{ ${version} }}`,
    );
    const call = html.indexOf(
      `/static/pdf/rc-voicecall.js?v={{ ${version} }}`,
    );
    assert.ok(rtc >= 0 && entry > rtc && call > entry);
  });
}
