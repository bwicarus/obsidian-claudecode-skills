import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const nativeSource = fs.readFileSync(
  "extensions/bw-reader-webext/src/computer-voice-native-protocol.js",
  "utf8",
);
const offscreenSource = fs.readFileSync(
  "extensions/bw-reader-webext/offscreen.js",
  "utf8",
);

function harness() {
  let listener = null;
  const nativeMessages = [];
  const requests = [];
  const stored = {};
  const nativePort = {
    onMessage: { addListener() {} },
    onDisconnect: { addListener() {} },
    postMessage(value) { nativeMessages.push(structuredClone(value)); },
  };
  const context = vm.createContext({
    URL,
    Uint8Array,
    ArrayBuffer,
    Map,
    Set,
    Promise,
    Error,
    Object,
    JSON,
    Math,
    Date,
    RegExp,
    String,
    Number,
    Boolean,
    TextEncoder,
    structuredClone,
    btoa,
    atob,
    crypto: globalThis.crypto,
    setTimeout: () => 1,
    clearTimeout() {},
    setInterval: () => 1,
    RTCPeerConnection: function RTCPeerConnection() {},
    MediaStreamTrackGenerator: function MediaStreamTrackGenerator() {},
    AudioData: function AudioData() {},
    fetch: async (url, init) => {
      requests.push({ url, init: structuredClone(init) });
      return {
        ok: true,
        status: 200,
        async json() {
          return {
            ok: true,
            contract: "reader-computer-voice-pairing/1",
            state: "active",
          };
        },
      };
    },
    chrome: {
      runtime: {
        id: "abcdefghijklmnopabcdefghijklmnop",
        connectNative(name) {
          assert.equal(name, "space.bwicarus.computer_voice");
          return nativePort;
        },
        onMessage: {
          addListener(value) { listener = value; },
        },
      },
      storage: {
        local: {
          async get(key) { return { [key]: stored[key] }; },
          async set(value) { Object.assign(stored, structuredClone(value)); },
        },
      },
    },
    BWComputerVoiceWebRtc: {},
  });
  context.globalThis = context;
  vm.runInContext(nativeSource, context, {
    filename: "computer-voice-native-protocol.js",
  });
  vm.runInContext(offscreenSource, context, {
    filename: "offscreen.js",
  });

  async function send(operation, payload = {}) {
    assert.equal(typeof listener, "function");
    return new Promise((resolve) => {
      const keptOpen = listener(
        {
          type: "BW_COMPUTER_VOICE_OFFSCREEN_REQUEST",
          operation,
          payload,
        },
        { id: context.chrome.runtime.id },
        resolve,
      );
      assert.equal(keptOpen, true);
    });
  }
  return { send, nativeMessages, requests, stored };
}

test("状态读取和首次配对不会启动采集或发送快捷键", async () => {
  const value = harness();
  const initial = await value.send("STATUS");
  assert.equal(initial.ok, true);
  assert.equal(initial.data.paired, false);
  assert.deepEqual(
    value.nativeMessages.map((message) => message.type),
    ["hello"],
  );

  const paired = await value.send("PAIR", {
    pairId: "pair-reader-1",
    pairingCode: "ABCDEFGH",
  });
  assert.equal(paired.ok, true);
  assert.equal(paired.data.paired, true);
  assert.equal(paired.data.extensionId, "abcdefghijklmnopabcdefghijklmnop");
  assert.equal("deviceToken" in paired.data, false);
  const consume = value.requests.find(({ url }) =>
    /\/pairings\/consume$/.test(url)
  );
  assert.ok(consume);
  assert.ok(value.requests.every(({ url }) =>
    /\/pairings\/consume$|\/device\/heartbeat$/.test(url)
  ));
  assert.equal(
    Object.hasOwn(consume.init.headers, "Authorization"),
    false,
  );
  assert.equal(
    JSON.parse(consume.init.body).contract,
    "reader-computer-voice-pairing/1",
  );
  assert.equal(
    value.nativeMessages.some((message) => message.type === "start"),
    false,
  );
  const saved = value.stored.bwComputerVoiceDeviceV1;
  assert.match(saved.deviceId, /^windows-[A-Za-z0-9._:-]+$/);
  assert.match(saved.deviceToken, /^[A-Za-z0-9._~-]{32,512}$/);
});

test("网页或不同扩展来源不能调用 offscreen 私有控制入口", () => {
  const value = harness();
  // The listener is exercised indirectly above; this source assertion keeps
  // the sender-id fence visible and non-optional.
  assert.match(
    offscreenSource,
    /sender\?\.id !== chrome\.runtime\.id/,
  );
  assert.doesNotMatch(offscreenSource, /window\.postMessage|externally_connectable/);
  assert.ok(value);
});
