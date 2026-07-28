import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const Transport = require(
  "../../_server_deploy/static/reader-runtime/server-sync-transport.js",
);

const namespace = "acct-v1-" + "a".repeat(64);
const protocol = {
  syncContract: "sync-v3",
  syncChangeContract: "record-parent-state/1",
  registryDigest:
    "sync-v3:record-parent-state/1|" +
    "user-settings:explicit:0:1|vocabulary-state:explicit:0:1",
};
const ownerFields = {
  deviceFamilyId: `pwa-install-v1-${"b".repeat(32)}`,
  ownerRole: "pwa",
  ownerInstanceId: `owner-instance-v1:pwa:${"c".repeat(32)}`,
  ownerGeneration: 7,
  ownerToken: `owner-token-v1-${"d".repeat(32)}`,
};
function ownerLease() {
  return {
    contract: "owner-lease/1",
    async ensureActive() {
      return structuredClone(ownerFields);
    },
    status() {
      return {
        contract: "owner-lease/1",
        state: "active",
        ownerGeneration: ownerFields.ownerGeneration,
      };
    },
  };
}

function response(data, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() { return structuredClone(data); },
  };
}

test("PWA transport uses only fixed endpoints and same-origin session credentials", async () => {
  const calls = [];
  const transport = Transport.createServerSyncTransport({
    origin: "https://reader.example/",
    ownerNamespace: namespace,
    deviceId: "pwa-install-v1-device",
    ...protocol,
    ownerLease: ownerLease(),
    credentials: "same-origin",
    fetch: async (url, init) => {
      calls.push({ url, init });
      return response({
        ok: true,
        cursor: 0,
        headCursor: 0,
        changes: [],
      });
    },
  });

  await transport.exchange({
    direction: "pull",
    cursor: 0,
    limit: 10,
    changes: [],
    ownerNamespace: "attacker-value",
    deviceId: "attacker-device",
  });
  assert.equal(calls[0].url, "https://reader.example/api/reader/sync/exchange");
  assert.equal(calls[0].init.credentials, "same-origin");
  assert.equal(calls[0].init.cache, "no-store");
  const body = JSON.parse(calls[0].init.body);
  assert.equal(body.ownerNamespace, namespace);
  assert.equal(body.deviceId, "pwa-install-v1-device");
  assert.equal(body.contract, "sync-gateway/2");
  assert.equal(body.syncContract, protocol.syncContract);
  assert.equal(body.syncChangeContract, protocol.syncChangeContract);
  assert.equal(body.registryDigest, protocol.registryDigest);
  assert.equal(body.deviceFamilyId, ownerFields.deviceFamilyId);
  assert.equal(body.ownerRole, ownerFields.ownerRole);
  assert.equal(body.ownerInstanceId, ownerFields.ownerInstanceId);
  assert.equal(body.ownerGeneration, ownerFields.ownerGeneration);
  assert.equal(body.ownerToken, ownerFields.ownerToken);
  assert.equal((await transport.status()).credentials, "same-origin");
});

test("extension-style transport defaults to omit and preserves structured HTTP errors", async () => {
  const calls = [];
  const transport = Transport.createServerSyncTransport({
    origin: "https://reader.example",
    ownerNamespace: namespace,
    deviceId: "extension-install-v1-device",
    ...protocol,
    ownerLease: ownerLease(),
    headers: () => ({ Authorization: "Bearer private" }),
    fetch: async (url, init) => {
      calls.push({ url, init });
      return response({
        ok: false,
        code: "BW_SYNC_OWNER_MISMATCH",
        error: "owner mismatch",
      }, 403);
    },
  });

  await assert.rejects(
    transport.exchange({ direction: "push", changes: [] }),
    (error) => (
      error.code === "BW_SYNC_OWNER_MISMATCH" &&
      error.status === 403 &&
      error.retryable === false
    ),
  );
  assert.equal(calls[0].init.credentials, "omit");
  assert.equal(calls[0].init.headers.get("Authorization"), "Bearer private");
});

test("invalid credential mode and non-JSON responses fail closed", async () => {
  assert.throws(
    () => Transport.createServerSyncTransport({
      origin: "https://reader.example",
      ownerNamespace: namespace,
      deviceId: "device",
      ...protocol,
      ownerLease: ownerLease(),
      credentials: "include",
      fetch: async () => response({}),
    }),
    /credentials/,
  );

  const transport = Transport.createServerSyncTransport({
    origin: "https://reader.example",
    ownerNamespace: namespace,
    deviceId: "device",
    ...protocol,
    ownerLease: ownerLease(),
    credentials: "same-origin",
    fetch: async () => ({
      ok: false,
      status: 502,
      async json() { throw new Error("html"); },
    }),
  });
  await assert.rejects(
    transport.snapshot({ limit: 10 }),
    (error) => (
      error.code === "BW_SYNC_HTTP_JSON" &&
      error.status === 502 &&
      error.retryable === true
    ),
  );
});

test("transport rejects mismatched causal protocol before any request", () => {
  for (const override of [
    { syncContract: "sync-v2" },
    { syncChangeContract: "unknown-parent/1" },
    { registryDigest: "sync-v3:forged" },
  ]) {
    assert.throws(
      () => Transport.createServerSyncTransport({
        origin: "https://reader.example",
        ownerNamespace: namespace,
        deviceId: "device",
        ...protocol,
        ownerLease: ownerLease(),
        ...override,
        fetch: async () => response({ ok: true }),
      }),
      /同步协议版本/,
    );
  }
});

test("transport 在缺少 owner lease 时不允许构造或发送", () => {
  assert.throws(
    () => Transport.createServerSyncTransport({
      origin: "https://reader.example",
      ownerNamespace: namespace,
      deviceId: "device",
      ...protocol,
      fetch: async () => response({ ok: true }),
    }),
    /owner-lease\/1/,
  );
});
