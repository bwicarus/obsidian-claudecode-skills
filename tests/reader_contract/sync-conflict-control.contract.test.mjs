import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ConflictControl = require(
  "../../_server_deploy/static/reader-runtime/sync-conflict-control.js",
);

function runtimeStatus(overrides = {}) {
  return {
    contract: "sync-runtime/1",
    paused: false,
    destroyed: false,
    running: false,
    pauseReason: "",
    lastResult: {
      server: { conflicts: [] },
      direct: {},
    },
    ...structuredClone(overrides),
  };
}

function runtimeWith(status, options = {}) {
  const calls = [];
  return {
    calls,
    runtime: {
      contract: "sync-runtime/1",
      async status() {
        calls.push("status");
        if (options.error) throw options.error;
        return structuredClone(status);
      },
      resolveConflict() {
        calls.push("resolveConflict");
        throw new Error("manual sync control must not resolve");
      },
      async runNow(reason) {
        calls.push(`runNow:${reason}`);
        if (options.runError) throw options.runError;
        return structuredClone(options.runResult || {
          contract: "sync-runtime/1",
          server: {
            ok: true,
            pendingLocal: false,
            applied: 0,
            conflicts: [],
          },
          direct: {},
        });
      },
    },
  };
}

function controlFor(runtime, overrides = {}) {
  return ConflictControl.createSyncConflictControl({
    runtime,
    owner: "extension-background",
    now: () => 1_234_567,
    ...overrides,
  });
}

test("status 只发布有界白名单，不签发重试令牌也不泄漏原始记录", async () => {
  const namespace = `acct-v1-${"a".repeat(64)}`;
  const token = `sk-${"b".repeat(32)}`;
  const peer = "private-peer-session";
  const source = runtimeWith(runtimeStatus({
    paused: true,
    pauseReason: "sync-conflict",
    lastResult: {
      server: {
        conflicts: [{
          collection: "user-settings",
          id: "theme",
          reason: "same-rev-different-value",
          incoming: {
            collection: "user-settings",
            id: "theme",
            rev: 7,
            value: { namespace, token },
          },
          local: {
            collection: "user-settings",
            id: "theme",
            rev: 6,
            value: { private: "raw-local-record" },
          },
          upstreamMessage: "private upstream exception",
        }],
      },
      direct: {
        [peer]: {
          conflicts: [{
            collection: "vocabulary-state",
            id: namespace,
            reason: "private-reason",
            incomingRev: 3,
            currentRev: 2,
            raw: { token },
          }],
        },
      },
    },
  }));
  const control = controlFor(source.runtime);
  assert.equal(control.owner, "extension-background");
  const status = await control.status();

  assert.deepEqual(Object.keys(status).sort(), [
    "at",
    "conflictCount",
    "conflicts",
    "contract",
    "errorCode",
    "owner",
    "retryable",
    "state",
    "truncated",
  ]);
  assert.equal(status.contract, "sync-conflict-control/1");
  assert.equal(status.owner, "extension-background");
  assert.equal(status.state, "blocked");
  assert.equal(status.at, 1_234_567);
  assert.equal(status.errorCode, "");
  assert.equal(status.retryable, false);
  assert.equal(status.conflictCount, 2);
  assert.equal(status.conflicts.length, 2);
  for (const conflict of status.conflicts) {
    assert.deepEqual(Object.keys(conflict).sort(), [
      "collection",
      "currentRev",
      "id",
      "incomingRev",
      "lane",
      "reason",
    ]);
  }
  const serialized = JSON.stringify(status);
  for (const secret of [
    namespace,
    token,
    peer,
    "raw-local-record",
    "private upstream exception",
  ]) {
    assert.equal(serialized.includes(secret), false, secret);
  }
  assert.equal("retryAfterResolution" in control, false);
  assert.deepEqual(source.calls, ["status"]);
});

test("冲突列表最多公开 50 条并保留安全 Unicode id", async () => {
  const conflicts = Array.from({ length: 57 }, (_, index) => ({
    collection: "vocabulary-state",
    id: index === 0 ? "日语/語彙🙂" : `word-${index}`,
    reason: "revision-conflict",
    incomingRev: index + 1,
    currentRev: index,
  }));
  const source = runtimeWith(runtimeStatus({
    paused: true,
    pauseReason: "sync-conflict",
    lastResult: { server: { conflicts }, direct: {} },
  }));
  const status = await controlFor(source.runtime).status();
  assert.equal(status.conflictCount, 57);
  assert.equal(status.conflicts.length, 50);
  assert.equal(status.truncated, true);
  assert.equal(
    status.conflicts.some((entry) => entry.id === "日语/語彙🙂"),
    true,
  );
});

test("sync-v3 因果冲突原因保持可审计，未知原因仍被脱敏", async () => {
  const causalReasons = [
    "causal-proof-missing",
    "causal-proof-invalid",
    "causal-proof-too-large",
    "causal-parent-mismatch",
    "causal-revision-overflow",
  ];
  const source = runtimeWith(runtimeStatus({
    paused: true,
    pauseReason: "sync-conflict",
    lastResult: {
      server: {
        conflicts: [
          ...causalReasons.map((reason, index) => ({
            collection: "user-settings",
            id: `causal-${index}`,
            reason,
            incomingRev: index + 2,
            currentRev: index + 1,
          })),
          {
            collection: "user-settings",
            id: "private",
            reason: "private-upstream-detail",
          },
        ],
      },
      direct: {},
    },
  }));

  const status = await controlFor(source.runtime).status();
  assert.deepEqual(
    status.conflicts
      .filter((entry) => entry.id.startsWith("causal-"))
      .map((entry) => entry.reason)
      .sort(),
    causalReasons.slice().sort(),
  );
  assert.equal(
    status.conflicts.find((entry) => entry.id === "private").reason,
    "conflict",
  );
});

test("状态映射只读且不会调用 runtime 的裁决或重放方法", async () => {
  const cases = [
    [runtimeStatus(), "ready"],
    [runtimeStatus({ running: true }), "syncing"],
    [runtimeStatus({ paused: true, pauseReason: "manual" }), "paused"],
    [runtimeStatus({ destroyed: true }), "destroyed"],
    [runtimeStatus({
      lastResult: {
        server: { conflicts: [{ collection: "user-settings", id: "x" }] },
        direct: {},
      },
    }), "conflict-observed"],
  ];
  for (const [value, expected] of cases) {
    const source = runtimeWith(value);
    const control = controlFor(source.runtime);
    assert.equal((await control.status()).state, expected);
    assert.deepEqual(source.calls, ["status"]);
    assert.equal("retryAfterResolution" in control, false);
  }
});

test("syncNow 严格校验请求并复用同一 requestId 的 receipt", async () => {
  const source = runtimeWith(runtimeStatus(), {
    runResult: {
      contract: "sync-runtime/1",
      server: {
        ok: true,
        pendingLocal: false,
        applied: 3,
        conflicts: [],
      },
      direct: {},
    },
  });
  const control = controlFor(source.runtime, {
    collections: ["vocabulary-state", "user-settings", "user-settings"],
  });
  const request = {
    contract: "reader-pi-sync-request/1",
    requestId: "sync-123",
  };
  const first = control.syncNow(request);
  const second = control.syncNow(request);
  assert.strictEqual(first, second);
  const result = await first;
  assert.deepEqual(Object.keys(result).sort(), [
    "applied",
    "at",
    "collections",
    "conflictCount",
    "contract",
    "errorCode",
    "errorMessage",
    "owner",
    "pendingLocal",
    "requestId",
    "retryable",
    "state",
  ]);
  assert.equal(result.contract, "reader-pi-data-sync-result/1");
  assert.equal(result.requestId, "sync-123");
  assert.equal(result.state, "complete");
  assert.equal(result.applied, 3);
  assert.deepEqual(result.collections, ["user-settings", "vocabulary-state"]);
  assert.deepEqual(source.calls, [
    "status",
    "runNow:native-pi-sync:sync-123",
    "status",
  ]);

  await assert.rejects(
    control.syncNow({
      contract: "reader-pi-sync-request/1",
      requestId: "sync-124",
      force: true,
    }),
    (error) => error && error.code === "BW_PI_SYNC_REQUEST_INVALID",
  );
  assert.equal("resolveConflict" in control, false);
  assert.equal("retryAfterResolution" in control, false);
});

test("syncNow 遇到 conflict pause 只返回 blocked，不解除暂停或重放", async () => {
  const source = runtimeWith(runtimeStatus({
    paused: true,
    pauseReason: "sync-conflict",
    lastResult: {
      server: {
        conflicts: [{
          collection: "user-settings",
          id: "theme",
          reason: "revision-conflict",
        }],
      },
      direct: {},
    },
  }));
  const result = await controlFor(source.runtime).syncNow({
    contract: "reader-pi-sync-request/1",
    requestId: "blocked-1",
  });
  assert.equal(result.state, "blocked");
  assert.equal(result.conflictCount, 1);
  assert.deepEqual(source.calls, ["status"]);
});

test("App-owned local runtime reports native-app without masquerading as PWA", async () => {
  const source = runtimeWith(runtimeStatus());
  const result = await controlFor(source.runtime, {
    owner: "native-app",
  }).syncNow({
    contract: "reader-pi-sync-request/1",
    requestId: "native-owner-1",
  });
  assert.equal(result.owner, "native-app");
  assert.equal(result.state, "complete");
});

test("server lane 与 runtime 异常公开稳定错误码，不泄漏上游文本", async () => {
  const serverFailure = runtimeWith(runtimeStatus({
    lastResult: {
      server: {
        ok: false,
        code: "BW_SYNC_AUTH",
        retryable: false,
        error: "private server authentication body",
      },
      direct: {},
    },
  }));
  const serverStatus = await controlFor(serverFailure.runtime).status();
  assert.equal(serverStatus.state, "error");
  assert.equal(serverStatus.errorCode, "BW_SYNC_AUTH");
  assert.equal(serverStatus.retryable, false);
  assert.equal(
    JSON.stringify(serverStatus).includes("private server authentication body"),
    false,
  );

  const runtimeFailure = runtimeWith(runtimeStatus({
    lastError: {
      code: "BW_SYNC_RETRYABLE",
      retryable: true,
      error: "private transport exception",
    },
  }));
  const runtimeStatusView = await controlFor(runtimeFailure.runtime).status();
  assert.equal(runtimeStatusView.state, "error");
  assert.equal(runtimeStatusView.errorCode, "BW_SYNC_RETRYABLE");
  assert.equal(runtimeStatusView.retryable, true);
  assert.equal(
    JSON.stringify(runtimeStatusView).includes("private transport exception"),
    false,
  );
});

test("创建控制器只依赖 runtime status/runNow，不依赖 crypto", async () => {
  const calls = [];
  const runtime = {
    contract: "sync-runtime/1",
    async status() {
      calls.push("status");
      return runtimeStatus();
    },
    async runNow() {
      calls.push("runNow");
      return { server: { ok: true, conflicts: [] }, direct: {} };
    },
  };
  const control = ConflictControl.createSyncConflictControl({
    runtime,
    owner: "pwa",
    now: () => 9,
  });
  const status = await control.status();
  assert.equal(status.owner, "pwa");
  assert.equal(status.at, 9);
  assert.deepEqual(calls, ["status"]);
});

test("owner fence 在读取前后都 fail closed", async () => {
  let checks = 0;
  const source = runtimeWith(runtimeStatus());
  const control = controlFor(source.runtime, {
    assertFence() {
      checks += 1;
      return checks < 2;
    },
  });
  await assert.rejects(
    control.status(),
    (error) => error && error.code === "BW_SYNC_CONFLICT_FENCE",
  );
  assert.equal(checks, 2);
});

test("无效 runtime 与 status 错误只返回稳定公共错误码", async () => {
  assert.throws(
    () => ConflictControl.createSyncConflictControl({
      runtime: { contract: "sync-runtime/1" },
    }),
    (error) => error && error.code === "BW_SYNC_CONFLICT_DEPENDENCY",
  );

  const invalid = runtimeWith({ contract: "wrong-runtime" });
  await assert.rejects(
    controlFor(invalid.runtime).status(),
    (error) => error && error.code === "BW_SYNC_CONFLICT_RUNTIME",
  );

  const failure = runtimeWith(runtimeStatus(), {
    error: new Error("private database body"),
  });
  await assert.rejects(
    controlFor(failure.runtime).status(),
    (error) =>
      error &&
      error.code === "BW_SYNC_CONFLICT_RUNTIME" &&
      !error.message.includes("private database body"),
  );
});
