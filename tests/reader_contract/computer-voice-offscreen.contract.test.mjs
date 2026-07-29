import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const backgroundSource = fs.readFileSync(
  "extensions/bw-reader-webext/background.js",
  "utf8",
);
const offscreenSource = fs.readFileSync(
  "extensions/bw-reader-webext/offscreen.js",
  "utf8",
);
const popupSource = fs.readFileSync(
  "extensions/bw-reader-webext/popup.js",
  "utf8",
);
const popupHtml = fs.readFileSync(
  "extensions/bw-reader-webext/popup.html",
  "utf8",
);

test("扩展不再暴露或转发旧 Pi 电脑语音配对入口", () => {
  for (const source of [backgroundSource, popupSource, popupHtml]) {
    assert.doesNotMatch(source, /BW_COMPUTER_VOICE_PAIR/);
    assert.doesNotMatch(source, /pairId|pairingCode/);
  }
  assert.doesNotMatch(popupHtml, /一次性配对码|Reader 配对 ID|配对这台电脑/);
});

test("旧设备记录保持原样，但后台不读取它恢复 offscreen", () => {
  assert.doesNotMatch(backgroundSource, /bwComputerVoiceDeviceV1/);
  assert.doesNotMatch(backgroundSource, /restoreComputerVoiceOffscreen/);
  assert.doesNotMatch(backgroundSource, /chrome\.offscreen\.createDocument/);
});

test("扩展后台只保留固定 Windows WSS relay，不再允许旧 Pi 电脑语音控制路由", () => {
  assert.doesNotMatch(backgroundSource, /\/api\/reader\/computer-voice/);
  assert.match(
    backgroundSource,
    /BW_COMPUTER_VOICE_DIRECT_V2/,
  );
  assert.match(
    backgroundSource,
    /wss:\/\/bwicarus-2\.taile44d0c\.ts\.net\/reader-computer-voice\/v1/,
  );
  assert.doesNotMatch(
    backgroundSource,
    /COMPUTER_VOICE_DIRECT_ENDPOINT\s*=[\s\S]{0,120}(?:message|port)\./,
  );
});

test("保留的 offscreen 文件完全惰性，不访问旧存储、Pi 或原生媒体", () => {
  assert.doesNotMatch(offscreenSource, /chrome\.storage|connectNative|fetch\s*\(/);
  assert.doesNotMatch(
    offscreenSource,
    /MediaStreamTrackGenerator|RTCPeerConnection|AudioData/,
  );
  assert.doesNotMatch(
    offscreenSource,
    /pairings\/consume|commands\/claim|device\/heartbeat/,
  );
});
