import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const OwnerLease = require(
  "../../_server_deploy/static/reader-runtime/sync-owner-lease.js",
);

const namespace = `acct-v1-${"a".repeat(64)}`;
const family = `pwa-install-v1-${"b".repeat(32)}`;
const registryDigest =
  "sync-v3:record-parent-state/1|" +
  "user-settings:explicit:0:1|vocabulary-state:explicit:0:1";
const instance = "owner-instance-v1:pwa:" + "c".repeat(32);

function createHarness(overrides = {}) {
  let nowMs = 1_000_000;
  let generation = 0;
  const calls = [];
  const responses = [];
  const manager = OwnerLease.createSyncOwnerLease({
    ownerNamespace: namespace,
    deviceId: "pwa-device-a",
    deviceFamilyId: family,
    ownerRole: "pwa",
    ownerInstanceId: instance,
    syncContract: "sync-v3",
    syncChangeContract: "record-parent-state/1",
    registryDigest,
    now: () => nowMs,
    autoRenew: false,
    request: async (path, body, requestOptions) => {
      calls.push({
        path,
        body: structuredClone(body),
        requestOptions: structuredClone(requestOptions || {}),
      });
      const queued = responses.shift();
      if (queued instanceof Error) throw queued;
      if (queued) return structuredClone(queued);
      generation += 1;
      return {
        ok: true,
        contract: "owner-lease/1",
        deviceId: "pwa-device-a",
        deviceFamilyId: family,
        ownerRole: "pwa",
        ownerInstanceId: instance,
        ownerGeneration: generation,
        ownerToken: `owner-token-v1-${String(generation).padStart(24, "x")}`,
        expiresAt: Math.floor(nowMs / 1000) + 30,
      };
    },
    ...overrides,
  });
  return {
    manager,
    calls,
    responses,
    now: () => nowMs,
    setNow: (value) => { nowMs = value; },
  };
}

test("owner lease claim 只发送固定身份并返回业务请求围栏", async () => {
  const h = createHarness();
  const fields = await h.manager.start();
  assert.equal(h.calls.length, 1);
  assert.equal(h.calls[0].path, OwnerLease.CLAIM_PATH);
  assert.deepEqual(
    Object.keys(h.calls[0].body).sort(),
    [
      "contract",
      "deviceFamilyId",
      "deviceId",
      "ownerInstanceId",
      "ownerNamespace",
      "ownerRole",
      "registryDigest",
      "syncChangeContract",
      "syncContract",
    ],
  );
  assert.equal(h.calls[0].body.contract, "owner-lease/1");
  assert.equal(h.calls[0].body.deviceFamilyId, family);
  assert.equal(fields.ownerGeneration, 1);
  assert.equal(fields.ownerToken.startsWith("owner-token-v1-"), true);
  assert.deepEqual(h.manager.fields(), fields);
  assert.equal(JSON.stringify(h.manager.status()).includes(fields.ownerToken), false);
});

test("并发 ensureActive 合并成一次 claim，临近过期才 renew", async () => {
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  let requests = 0;
  const h = createHarness({
    request: async (_path, _body) => {
      requests += 1;
      await gate;
      return {
        ok: true,
        contract: "owner-lease/1",
        deviceId: "pwa-device-a",
        deviceFamilyId: family,
        ownerRole: "pwa",
        ownerInstanceId: instance,
        ownerGeneration: requests,
        ownerToken: `owner-token-v1-${String(requests).padStart(24, "z")}`,
        expiresAt: Math.floor(h.now() / 1000) + 30,
      };
    },
  });
  const first = h.manager.start();
  const second = h.manager.ensureActive();
  release();
  assert.deepEqual(await first, await second);
  assert.equal(requests, 1);
  await h.manager.ensureActive();
  assert.equal(requests, 1);
  h.setNow(h.now() + 21_000);
  await h.manager.ensureActive();
  assert.equal(requests, 2);
});

test("renew 和 release 必须携带当前 token/generation，释放后 fail closed", async () => {
  const h = createHarness();
  const acquired = await h.manager.start();
  h.setNow(h.now() + 21_000);
  const renewed = await h.manager.renew();
  assert.equal(h.calls[1].path, OwnerLease.RENEW_PATH);
  assert.equal(h.calls[1].body.ownerGeneration, acquired.ownerGeneration);
  assert.equal(h.calls[1].body.ownerToken, acquired.ownerToken);
  assert.equal(renewed.ownerGeneration, 2);
  await h.manager.stop("test-release");
  assert.equal(h.calls[2].path, OwnerLease.RELEASE_PATH);
  assert.equal(h.calls[2].body.ownerGeneration, renewed.ownerGeneration);
  assert.equal(h.calls[2].body.ownerToken, renewed.ownerToken);
  assert.equal(h.calls[2].requestOptions.keepalive, undefined);
  assert.throws(
    () => h.manager.fields(),
    (error) => error.code === "BW_SYNC_OWNER_INACTIVE",
  );
});

test("pagehide release 会把 keepalive 选项透传给请求器", async () => {
  const h = createHarness();
  await h.manager.start();
  await h.manager.stop(
    "pagehide-bfcache",
    true,
    { keepalive: true },
  );
  assert.equal(h.calls.length, 2);
  assert.equal(h.calls[0].path, OwnerLease.CLAIM_PATH);
  assert.equal(h.calls[0].requestOptions.keepalive, undefined);
  assert.equal(h.calls[1].path, OwnerLease.RELEASE_PATH);
  assert.equal(h.calls[1].requestOptions.keepalive, true);
  assert.throws(
    () => h.manager.assertActive(),
    (error) => error.code === "BW_SYNC_OWNER_INACTIVE",
  );
});

test("过期 lease 不能继续为 DataChannel 或 HTTP 提供围栏", async () => {
  const lost = [];
  const h = createHarness({
    onLost: (error) => lost.push(error.code),
  });
  await h.manager.start();
  h.setNow(h.now() + 31_000);
  assert.throws(
    () => h.manager.assertActive(),
    (error) => error.code === "BW_SYNC_OWNER_INACTIVE",
  );
  assert.deepEqual(lost, ["BW_SYNC_OWNER_INACTIVE"]);
  assert.equal(h.manager.status().state, "waiting");
});

test("服务端绝对时钟领先时，本地租约仍被限制在协议 30 秒窗口内", async () => {
  const h = createHarness({
    request: async () => ({
      ok: true,
      contract: "owner-lease/1",
      deviceId: "pwa-device-a",
      deviceFamilyId: family,
      ownerRole: "pwa",
      ownerInstanceId: instance,
      ownerGeneration: 1,
      ownerToken: `owner-token-v1-${"k".repeat(32)}`,
      // 模拟服务端时钟比设备快五分钟；不能据此延长本地写权限。
      expiresAt: Math.floor(h.now() / 1000) + 330,
    }),
  });
  await h.manager.start();
  h.setNow(h.now() + OwnerLease.MAX_LOCAL_LEASE_MS - 1);
  assert.equal(h.manager.assertActive(), true);
  h.setNow(h.now() + 2);
  assert.throws(
    () => h.manager.assertActive(),
    (error) => error.code === "BW_SYNC_OWNER_INACTIVE",
  );
});

test("迟到响应从请求发出时计时，不能在服务端到期后留下双 owner 窗口", async () => {
  let wallNow = 4_000_000;
  let monotonicNow = 100_000;
  let releaseResponse;
  let markRequestStarted;
  const responseGate = new Promise((resolve) => {
    releaseResponse = resolve;
  });
  const requestStarted = new Promise((resolve) => {
    markRequestStarted = resolve;
  });
  const h = createHarness({
    now: () => wallNow,
    monotonicNow: () => monotonicNow,
    request: async () => {
      markRequestStarted();
      await responseGate;
      return {
        ok: true,
        contract: "owner-lease/1",
        deviceId: "pwa-device-a",
        deviceFamilyId: family,
        ownerRole: "pwa",
        ownerInstanceId: instance,
        ownerGeneration: 1,
        ownerToken: `owner-token-v1-${"l".repeat(32)}`,
        // 服务端时钟领先，不能让客户端从“收到响应”重新获得 29 秒。
        expiresAt: Math.floor(wallNow / 1000) + 330,
      };
    },
  });
  const pending = h.manager.start();
  await requestStarted;
  wallNow += 5_000;
  monotonicNow += 5_000;
  releaseResponse();
  await pending;
  wallNow += OwnerLease.MAX_LOCAL_LEASE_MS - 5_000 + 1;
  monotonicNow += OwnerLease.MAX_LOCAL_LEASE_MS - 5_000 + 1;
  assert.throws(
    () => h.manager.assertActive(),
    (error) => error.code === "BW_SYNC_OWNER_INACTIVE",
  );
});

test("领取后系统时钟回退不能延长单调租约截止时间", async () => {
  let wallNow = 2_000_000;
  let monotonicNow = 50_000;
  const h = createHarness({
    now: () => wallNow,
    monotonicNow: () => monotonicNow,
    request: async () => ({
      ok: true,
      contract: "owner-lease/1",
      deviceId: "pwa-device-a",
      deviceFamilyId: family,
      ownerRole: "pwa",
      ownerInstanceId: instance,
      ownerGeneration: 1,
      ownerToken: `owner-token-v1-${"m".repeat(32)}`,
      expiresAt: Math.floor(wallNow / 1000) + 30,
    }),
  });
  await h.manager.start();
  wallNow -= 3_600_000;
  monotonicNow += OwnerLease.MAX_LOCAL_LEASE_MS + 1;
  assert.throws(
    () => h.manager.assertActive(),
    (error) => error.code === "BW_SYNC_OWNER_INACTIVE",
  );
});

test("自动续租 timer 提前触发并复用现租约后仍会重新安排", async () => {
  const timers = [];
  let requests = 0;
  const h = createHarness({
    autoRenew: true,
    setTimeout(callback, delay) {
      const timer = { callback, delay, cleared: false };
      timers.push(timer);
      return timer;
    },
    clearTimeout(timer) {
      timer.cleared = true;
    },
    request: async () => {
      requests += 1;
      return {
        ok: true,
        contract: "owner-lease/1",
        deviceId: "pwa-device-a",
        deviceFamilyId: family,
        ownerRole: "pwa",
        ownerInstanceId: instance,
        ownerGeneration: 1,
        ownerToken: `owner-token-v1-${"t".repeat(32)}`,
        expiresAt: Math.floor(h.now() / 1000) + 30,
      };
    },
  });
  await h.manager.start();
  assert.equal(timers.length, 1);
  h.setNow(h.now() + 18_000);
  timers[0].callback();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();
  assert.equal(requests, 1, "提前触发时尚未进入 renew 窗口");
  assert.equal(timers.length, 2, "复用当前 lease 后仍需安排下一 timer");
  assert.equal(timers[1].cleared, false);
});

test("系统睡眠只推进墙钟时，旧租约也必须立即失效", async () => {
  let wallNow = 3_000_000;
  let monotonicNow = 75_000;
  const h = createHarness({
    now: () => wallNow,
    monotonicNow: () => monotonicNow,
    request: async () => ({
      ok: true,
      contract: "owner-lease/1",
      deviceId: "pwa-device-a",
      deviceFamilyId: family,
      ownerRole: "pwa",
      ownerInstanceId: instance,
      ownerGeneration: 1,
      ownerToken: `owner-token-v1-${"n".repeat(32)}`,
      expiresAt: Math.floor(wallNow / 1000) + 30,
    }),
  });
  await h.manager.start();
  wallNow += OwnerLease.MAX_LOCAL_LEASE_MS + 1;
  assert.throws(
    () => h.manager.assertActive(),
    (error) => error.code === "BW_SYNC_OWNER_INACTIVE",
  );
});

test("响应身份、token、generation 或过期时间不精确时拒绝激活", async () => {
  const invalidResponses = [
    { deviceId: "pwa-device-b" },
    { deviceFamilyId: `pwa-install-v1-${"d".repeat(32)}` },
    { ownerRole: "extension" },
    { ownerInstanceId: "other-owner" },
    { ownerGeneration: 0 },
    { ownerToken: "weak" },
    { expiresAt: 999 },
  ];
  for (const override of invalidResponses) {
    const h = createHarness();
    h.responses.push({
      ok: true,
      contract: "owner-lease/1",
      deviceId: "pwa-device-a",
      deviceFamilyId: family,
      ownerRole: "pwa",
      ownerInstanceId: instance,
      ownerGeneration: 1,
      ownerToken: `owner-token-v1-${"x".repeat(24)}`,
      expiresAt: 1030,
      ...override,
    });
    await assert.rejects(
      h.manager.start(),
      (error) => error.code === "BW_SYNC_OWNER_RESPONSE",
    );
  }
});

test("stop 立即撤销本地围栏，晚到 claim 不能复活且停止后不重新请求", async () => {
  let releaseClaim;
  const claimGate = new Promise((resolve) => { releaseClaim = resolve; });
  let requests = 0;
  const h = createHarness({
    request: async (path) => {
      requests += 1;
      assert.equal(path, OwnerLease.CLAIM_PATH);
      await claimGate;
      return {
        ok: true,
        contract: "owner-lease/1",
        deviceId: "pwa-device-a",
        deviceFamilyId: family,
        ownerRole: "pwa",
        ownerInstanceId: instance,
        ownerGeneration: 1,
        ownerToken: `owner-token-v1-${"q".repeat(32)}`,
        expiresAt: 1030,
      };
    },
  });
  const pending = h.manager.start();
  await Promise.resolve();
  await h.manager.stop("claim-in-flight");
  assert.throws(
    () => h.manager.fields(),
    (error) => error.code === "BW_SYNC_OWNER_INACTIVE",
  );
  releaseClaim();
  await assert.rejects(
    pending,
    (error) => error.code === "BW_SYNC_OWNER_INACTIVE",
  );
  await assert.rejects(
    h.manager.ensureActive(),
    (error) => error.code === "BW_SYNC_OWNER_INACTIVE",
  );
  assert.equal(requests, 1);
  assert.equal(h.manager.status().state, "stopped");
});

test("stop 与 renew 乱序时旧响应不能覆盖同步失效，destroy 永久 fail closed", async () => {
  let releaseRenew;
  const renewGate = new Promise((resolve) => { releaseRenew = resolve; });
  const h = createHarness({
    request: async (path, body) => {
      if (path === OwnerLease.CLAIM_PATH) {
        return {
          ok: true,
          contract: "owner-lease/1",
          deviceId: "pwa-device-a",
          deviceFamilyId: family,
          ownerRole: "pwa",
          ownerInstanceId: instance,
          ownerGeneration: 1,
          ownerToken: `owner-token-v1-${"r".repeat(32)}`,
          expiresAt: 1030,
        };
      }
      if (path === OwnerLease.RENEW_PATH) {
        await renewGate;
        return {
          ok: true,
          contract: "owner-lease/1",
          deviceId: "pwa-device-a",
          deviceFamilyId: family,
          ownerRole: "pwa",
          ownerInstanceId: instance,
          ownerGeneration: 2,
          ownerToken: `owner-token-v1-${"s".repeat(32)}`,
          expiresAt: 1050,
        };
      }
      assert.equal(path, OwnerLease.RELEASE_PATH);
      return {
        ok: true,
        contract: "owner-lease/1",
        released: true,
        ownerGeneration: body.ownerGeneration,
        expiresAt: 0,
      };
    },
  });
  await h.manager.start();
  h.setNow(1_021_000);
  const renewing = h.manager.renew();
  await Promise.resolve();
  const stopping = h.manager.destroy("destroy-during-renew");
  assert.throws(
    () => h.manager.assertActive(),
    (error) => error.code === "BW_SYNC_OWNER_INACTIVE",
  );
  releaseRenew();
  await assert.rejects(
    renewing,
    (error) => error.code === "BW_SYNC_OWNER_INACTIVE",
  );
  await stopping;
  assert.equal(h.manager.status().state, "stopped");
  await assert.rejects(
    h.manager.start(),
    (error) => error.code === "BW_SYNC_OWNER_INACTIVE",
  );
});
