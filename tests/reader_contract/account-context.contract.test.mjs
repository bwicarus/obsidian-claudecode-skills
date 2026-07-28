import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const AccountContext = require(
  "../../_server_deploy/static/reader-runtime/account-context.js",
);

const ACCOUNT_A = `acct-v1-${"a".repeat(64)}`;
const ACCOUNT_B = `acct-v1-${"b".repeat(64)}`;

test("AccountContext 只接受正式 namespace 与两个可信身份来源", () => {
  assert.equal(AccountContext.CONTRACT, "account-context/1");
  assert.equal(AccountContext.LEASE_CONTRACT, "account-context-lease/1");
  assert.equal(AccountContext.normalizeNamespace(` ${ACCOUNT_A} `), ACCOUNT_A);

  for (const namespace of [
    "",
    "acct-v1-short",
    `acct-v1-${"A".repeat(64)}`,
    `user-${"a".repeat(64)}`,
  ]) {
    assert.throws(
      () => AccountContext.createContext().activate({
        namespace,
        source: "server-session",
      }),
      (error) => error.code === "BW_ACCOUNT_NAMESPACE",
      namespace,
    );
  }

  for (const source of ["", "api-token", "popup", "cache"]) {
    assert.throws(
      () => AccountContext.createContext().activate({
        namespace: ACCOUNT_A,
        source,
      }),
      (error) => error.code === "BW_ACCOUNT_SOURCE",
      source,
    );
  }
});

test("activate/deactivate 递增 generation，并让旧 lease 立即失效", () => {
  const context = AccountContext.createContext();
  assert.deepEqual(
    {
      active: context.snapshot().active,
      namespace: context.snapshot().namespace,
      generation: context.snapshot().generation,
    },
    { active: false, namespace: "", generation: 0 },
  );
  assert.throws(
    () => context.lease(),
    (error) => error.code === "BW_ACCOUNT_CONTEXT_UNAVAILABLE",
  );

  const activeA = context.activate({
    namespace: ACCOUNT_A,
    source: "server-session",
  });
  const leaseA = context.lease();
  assert.equal(activeA.generation, 1);
  assert.equal(context.isCurrent(leaseA), true);
  assert.equal(context.assertCurrent(leaseA).namespace, ACCOUNT_A);

  context.activate({
    namespace: ACCOUNT_B,
    source: "provider-ticket",
  });
  assert.equal(context.snapshot().generation, 2);
  assert.equal(context.isCurrent(leaseA), false);
  assert.throws(
    () => context.assertCurrent(leaseA),
    (error) => error.code === "BW_ACCOUNT_CONTEXT_STALE",
  );

  const leaseB = context.lease();
  context.deactivate("provider-disconnected");
  assert.equal(context.snapshot().generation, 3);
  assert.equal(context.snapshot().reason, "provider-disconnected");
  assert.equal(context.isCurrent(leaseB), false);
  context.deactivate("already-inactive");
  assert.equal(context.snapshot().generation, 3);
});

test("不同 context 的同 namespace、同 generation lease 也不能互换", () => {
  const left = AccountContext.createContext();
  const right = AccountContext.createContext();
  left.activate({ namespace: ACCOUNT_A, source: "server-session" });
  right.activate({ namespace: ACCOUNT_A, source: "provider-ticket" });

  const leftLease = left.lease();
  const rightLease = right.lease();
  assert.equal(leftLease.generation, rightLease.generation);
  assert.equal(left.isCurrent(rightLease), false);
  assert.equal(right.isCurrent(leftLease), false);
});

test("subscribe 只收到本 context 的激活/撤销事件，并可退订", () => {
  const context = AccountContext.createContext();
  const events = [];
  const unsubscribe = context.subscribe((event) => events.push(event));

  context.activate({ namespace: ACCOUNT_A, source: "server-session" });
  context.deactivate("logout");
  unsubscribe();
  context.activate({ namespace: ACCOUNT_B, source: "server-session" });

  assert.deepEqual(events.map((event) => event.type), ["activate", "deactivate"]);
  assert.equal(events[0].previous.active, false);
  assert.equal(events[0].current.namespace, ACCOUNT_A);
  assert.equal(events[1].previous.namespace, ACCOUNT_A);
  assert.equal(events[1].current.reason, "logout");
  assert.throws(
    () => context.subscribe(null),
    (error) => error.code === "BW_ACCOUNT_SUBSCRIBER",
  );
});

test("namespacedKey 只能使用当前 lease，并稳定编码逻辑键", () => {
  const context = AccountContext.createContext();
  assert.throws(
    () => context.namespacedKey("cache"),
    (error) => error.code === "BW_ACCOUNT_CONTEXT_UNAVAILABLE",
  );

  context.activate({ namespace: ACCOUNT_A, source: "server-session" });
  const lease = context.lease();
  assert.equal(
    context.namespacedKey("translation cache/ja", lease),
    `bw.reader.account.v1:${ACCOUNT_A}:translation%20cache%2Fja`,
  );
  assert.equal(context.key("cache", lease).endsWith(":cache"), true);
  assert.throws(
    () => context.namespacedKey("", lease),
    (error) => error.code === "BW_ACCOUNT_KEY",
  );

  context.deactivate("logout");
  assert.throws(
    () => context.namespacedKey("cache", lease),
    (error) => error.code === "BW_ACCOUNT_CONTEXT_STALE",
  );
});
