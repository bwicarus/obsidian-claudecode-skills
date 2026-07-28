import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const Signals = require(
  "../../_server_deploy/static/reader-runtime/direct-sync-signal-transport.js",
);

const NAMESPACE = `acct-v1-${"a".repeat(64)}`;
const PROOF = `account-proof-v1-${"b".repeat(64)}`;
const OWNER_FIELDS = {
  deviceFamilyId: `pwa-install-v1-${"c".repeat(32)}`,
  ownerRole: "pwa",
  ownerInstanceId: `owner-instance-v1:pwa:${"d".repeat(32)}`,
  ownerGeneration: 3,
  ownerToken: `owner-token-v1-${"e".repeat(32)}`,
};
function ownerLease() {
  return {
    contract: "owner-lease/1",
    async ensureActive() {
      return structuredClone(OWNER_FIELDS);
    },
    status() {
      return { contract: "owner-lease/1", state: "active" };
    },
  };
}

function response(request, overrides = {}) {
  return {
    contract: "direct-signal/1",
    accountProof: PROOF,
    headCursor: request.serverCursor,
    baselineReady: request.serverReady,
    signalCursor: request.signalCursor,
    ackedSignalIds: (request.signals || []).map((signal) => signal.signalId),
    signalResetRequired: false,
    hasMore: false,
    peers: [],
    signals: [],
    ...overrides,
  };
}

test("PWA 信令只访问固定同源端点，并发送 serverReady 与稳定 signalId", async () => {
  const calls = [];
  const transport = Signals.createDirectSignalTransport({
    origin: "https://reader.example/",
    ownerNamespace: NAMESPACE,
    deviceId: "pwa-install-v1-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    registryDigest:
      "sync-v3:record-parent-state/1|" +
      "user-settings:explicit:0:1|vocabulary-state:explicit:0:1",
    ownerLease: ownerLease(),
    credentials: "same-origin",
    fetch: async (url, init) => {
      calls.push({ url, init });
      const request = JSON.parse(init.body);
      return new Response(JSON.stringify(response(request)), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    },
  });
  const result = await transport.exchange({
    serverCursor: 7,
    serverReady: true,
    signalCursor: 3,
    signals: [{
      signalId: "signal-a-1",
      toDeviceId: "peer-b",
      sessionId: "direct-session-v1-abc",
      kind: "offer",
      payload: { type: "offer", sdp: "test" },
    }],
  });

  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "https://reader.example/api/reader/sync/signal");
  assert.equal(calls[0].init.credentials, "same-origin");
  const body = JSON.parse(calls[0].init.body);
  assert.equal(body.contract, "direct-signal/1");
  assert.equal(body.ownerNamespace, NAMESPACE);
  assert.equal(body.deviceFamilyId, OWNER_FIELDS.deviceFamilyId);
  assert.equal(body.ownerRole, OWNER_FIELDS.ownerRole);
  assert.equal(body.ownerInstanceId, OWNER_FIELDS.ownerInstanceId);
  assert.equal(body.ownerGeneration, OWNER_FIELDS.ownerGeneration);
  assert.equal(body.ownerToken, OWNER_FIELDS.ownerToken);
  assert.equal(body.serverReady, true);
  assert.equal(body.signals[0].signalId, "signal-a-1");
  assert.equal(result.accountProof, PROOF);
});

test("扩展可注入后台 exchange，内容宿主不需要 token 或任意 URL", async () => {
  let seen;
  const transport = Signals.createDirectSignalTransport({
    ownerNamespace: NAMESPACE,
    deviceId: "extension-install-v1-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    registryDigest:
      "sync-v3:record-parent-state/1|user-settings:explicit:0:1",
    exchange: async (request) => {
      seen = request;
      return response(request);
    },
  });

  await transport.exchange({
    serverCursor: 0,
    serverReady: false,
    signalCursor: 0,
    signals: [],
  });
  assert.equal(seen.ownerNamespace, NAMESPACE);
  assert.equal(transport.status().endpoint, "/api/reader/sync/signal");
  assert.equal(JSON.stringify(seen).includes("Bearer"), false);
});

test("信令响应缺失或畸形账户代际证明时 fail closed", async () => {
  for (const accountProof of [undefined, "account-proof-v1-short"]) {
    const transport = Signals.createDirectSignalTransport({
      ownerNamespace: NAMESPACE,
      deviceId: "extension-device",
      registryDigest:
        "sync-v3:record-parent-state/1|user-settings:explicit:0:1",
      exchange: async (request) => response(request, { accountProof }),
    });
    await assert.rejects(
      transport.exchange({
        serverCursor: 0,
        serverReady: false,
        signalCursor: 0,
        signals: [],
      }),
      (error) => error.code === "BW_DIRECT_SIGNAL_INVALID" &&
        error.retryable === false,
    );
  }
});

test("信令严格拒绝未知 kind、缺 signalId 与超大 payload", () => {
  const transport = Signals.createDirectSignalTransport({
    ownerNamespace: NAMESPACE,
    deviceId: "device-a",
    registryDigest:
      "sync-v3:record-parent-state/1|user-settings:explicit:0:1",
    exchange: async (request) => response(request),
  });
  assert.throws(
    () => transport.exchange({
      signals: [{
        toDeviceId: "device-b",
        sessionId: "session-a",
        kind: "offer",
        payload: {},
      }],
    }),
    (error) => error.code === "BW_DIRECT_SIGNAL_INVALID",
  );
  assert.throws(
    () => transport.exchange({
      signals: [{
        signalId: "signal-a",
        toDeviceId: "device-b",
        sessionId: "session-a",
        kind: "execute",
        payload: {},
      }],
    }),
    (error) => error.code === "BW_DIRECT_SIGNAL_INVALID",
  );
  assert.throws(
    () => transport.exchange({
      signals: [{
        signalId: "signal-b",
        toDeviceId: "device-b",
        sessionId: "session-a",
        kind: "ice",
        payload: { candidate: "x".repeat(70_000) },
      }],
    }),
    (error) => error.code === "BW_DIRECT_SIGNAL_TOO_LARGE",
  );
});

test("HTTP 信令缺少 owner lease 时 fail closed，后台注入 exchange 不重复暴露 token", () => {
  assert.throws(
    () => Signals.createDirectSignalTransport({
      origin: "https://reader.example",
      ownerNamespace: NAMESPACE,
      deviceId: "pwa-device",
      registryDigest:
        "sync-v3:record-parent-state/1|user-settings:explicit:0:1",
      credentials: "same-origin",
      fetch: async () => new Response("{}"),
    }),
    (error) => error.code === "BW_DIRECT_SIGNAL_DEPENDENCY",
  );
  assert.doesNotThrow(
    () => Signals.createDirectSignalTransport({
      ownerNamespace: NAMESPACE,
      deviceId: "extension-device",
      registryDigest:
        "sync-v3:record-parent-state/1|user-settings:explicit:0:1",
      exchange: async (request) => response(request),
    }),
  );
});
