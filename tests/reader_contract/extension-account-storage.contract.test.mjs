import test from "node:test";
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const AccountContext = require(
  "../../_server_deploy/static/reader-runtime/account-context.js",
);
const AccountStorageFactory = require(
  "../../_server_deploy/static/reader-runtime/extension-account-storage.js",
);

const ACCOUNT_A = `acct-v1-${"a".repeat(64)}`;
const ACCOUNT_B = `acct-v1-${"b".repeat(64)}`;

function context(namespace) {
  const value = AccountContext.createContext();
  value.activate({ namespace, source: "provider-ticket" });
  return value;
}

function fakeStorage(initial = {}) {
  const state = structuredClone(initial);
  return {
    state,
    reads: [],
    writes: [],
    async get(input) {
      this.reads.push(structuredClone(input));
      const keys = Array.isArray(input) ? input : [input];
      const result = {};
      for (const key of keys) {
        if (Object.hasOwn(state, key)) result[key] = structuredClone(state[key]);
      }
      return result;
    },
    async set(values) {
      this.writes.push(structuredClone(values));
      Object.assign(state, structuredClone(values));
    },
  };
}

function create(storage, now = () => 1000) {
  return AccountStorageFactory.create({
    accountContext: AccountContext,
    storage,
    crypto: webcrypto,
    now,
  });
}

test("扩展账户存储只开放 credentials，正式键必须由 AccountContext 分区", async () => {
  const storage = fakeStorage();
  const api = create(storage);
  const a = context(ACCOUNT_A);
  const b = context(ACCOUNT_B);
  const leaseA = a.lease();
  const leaseB = b.lease();

  assert.equal(api.CONTRACT, "extension-account-storage/1");
  assert.deepEqual(Object.keys(api.BASES), ["CREDENTIALS"]);
  assert.equal(
    api.credentialKey(a, leaseA),
    a.namespacedKey("extension:credentials-v1", leaseA),
  );
  assert.notEqual(api.credentialKey(a, leaseA), api.credentialKey(b, leaseB));

  await api.saveVerifiedToken(a, leaseA, "token-account-a");
  await api.saveVerifiedToken(b, leaseB, "token-account-b");
  assert.equal(await api.activeToken(a, leaseA), "token-account-a");
  assert.equal(await api.activeToken(b, leaseB), "token-account-b");
  assert.equal(Object.hasOwn(storage.state, "apiToken"), false);
  assert.equal(
    Object.keys(storage.state).every((key) =>
      key.startsWith("bw.reader.account.v1:acct-v1-")
    ),
    true,
  );
});

test("每个账户同时只有一个 active token，旧候选保留且状态不含明文", async () => {
  let timestamp = 100;
  const storage = fakeStorage();
  const api = create(storage, () => ++timestamp);
  const account = context(ACCOUNT_A);
  const lease = account.lease();

  const first = await api.saveVerifiedToken(account, lease, "first-token");
  assert.equal(first.configured, true);
  assert.equal(first.candidateCount, 1);
  assert.equal(first.inactiveCandidateCount, 0);

  const second = await api.saveVerifiedToken(account, lease, "second-token");
  assert.equal(second.candidateCount, 2);
  assert.equal(second.inactiveCandidateCount, 1);
  assert.equal(await api.activeToken(account, lease), "second-token");
  assert.equal(JSON.stringify(second).includes("first-token"), false);
  assert.equal(JSON.stringify(second).includes("second-token"), false);

  const reactivated = await api.saveVerifiedToken(account, lease, "first-token");
  assert.equal(reactivated.candidateCount, 2);
  assert.equal(reactivated.inactiveCandidateCount, 1);
  assert.equal(await api.activeToken(account, lease), "first-token");
});

test("账户切换后晚到 storage 写只可能落旧键且结果被租约围栏拒绝", async () => {
  let releaseSet;
  let signalSet;
  const setStarted = new Promise((resolve) => { signalSet = resolve; });
  const setGate = new Promise((resolve) => { releaseSet = resolve; });
  const storage = fakeStorage();
  const originalSet = storage.set.bind(storage);
  storage.set = async (values) => {
    await originalSet(values);
    signalSet();
    await setGate;
  };
  const api = create(storage);
  const account = context(ACCOUNT_A);
  const oldLease = account.lease();

  const pending = api.saveVerifiedToken(account, oldLease, "late-token");
  await setStarted;
  account.activate({ namespace: ACCOUNT_B, source: "provider-ticket" });
  releaseSet();
  await assert.rejects(pending, { code: "BW_ACCOUNT_CONTEXT_STALE" });

  const newLease = account.lease();
  assert.equal(await api.activeToken(account, newLease), "");
  assert.equal(
    Object.keys(storage.state).some((key) =>
      key.includes(ACCOUNT_B) && JSON.stringify(storage.state[key]).includes("late-token")
    ),
    false,
  );
});

test("storage 临时写失败不污染队列，后续重试仍可保存同账户令牌", async () => {
  const storage = fakeStorage();
  const originalSet = storage.set.bind(storage);
  let failOnce = true;
  storage.set = async (values) => {
    if (failOnce) {
      failOnce = false;
      throw new Error("temporary storage failure");
    }
    await originalSet(values);
  };
  const api = create(storage);
  const account = context(ACCOUNT_A);
  const lease = account.lease();

  await assert.rejects(
    api.saveVerifiedToken(account, lease, "retry-token"),
    /temporary storage failure/,
  );
  assert.equal(await api.activeToken(account, lease), "");

  const status = await api.saveVerifiedToken(account, lease, "retry-token");
  assert.equal(status.configured, true);
  assert.equal(status.candidateCount, 1);
  assert.equal(await api.activeToken(account, lease), "retry-token");
});

test("旧裸键只报告隔离状态和字节数，既不命中也不删除", async () => {
  const legacy = {
    apiToken: "must-never-be-used",
    dictCache: { secret: { body: "old" } },
    webTrCacheV1: { page: { items: { old: "旧" } } },
    ephSettingsV1: { "eph-gp-floating": "1" },
  };
  const storage = fakeStorage(legacy);
  const api = create(storage);
  const account = context(ACCOUNT_A);
  const lease = account.lease();

  assert.equal(await api.activeToken(account, lease), "");
  const inventory = await api.legacyInventory();
  for (const key of Object.keys(legacy)) {
    assert.equal(inventory[key].quarantined, true);
    assert.equal(inventory[key].present, true);
    assert.ok(inventory[key].bytes > 0);
    assert.equal(Object.hasOwn(inventory[key], "value"), false);
    assert.deepEqual(storage.state[key], legacy[key]);
  }
  assert.equal(JSON.stringify(inventory).includes("must-never-be-used"), false);
});
