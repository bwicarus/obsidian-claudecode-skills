import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import {
  DataRegistry,
  makeStore,
} from "./helpers.mjs";

const require = createRequire(import.meta.url);
const Coordinator = require(
  "../../_server_deploy/static/reader-runtime/sync-coordinator.js",
);
const SyncRuntime = require(
  "../../_server_deploy/static/reader-runtime/sync-runtime.js",
);

function timers() {
  let next = 0;
  const jobs = new Map();
  return {
    setTimeout(fn, delay) {
      const id = ++next;
      jobs.set(id, { fn, delay });
      return id;
    },
    clearTimeout(id) {
      jobs.delete(id);
    },
    size() {
      return jobs.size;
    },
    runNext() {
      const first = jobs.entries().next().value;
      if (!first) return false;
      jobs.delete(first[0]);
      first[1].fn();
      return true;
    },
  };
}

function empty(request) {
  return {
    contract: "sync-gateway/2",
    cursor: request.cursor,
    headCursor: request.cursor,
    oldestCursor: 0,
    hasMore: false,
    resetRequired: false,
    ackedMutationIds: [],
    changes: [],
    conflicts: [],
  };
}

test("pause fence prevents an in-flight server response from applying or advancing", async () => {
  const clock = timers();
  const store = makeStore("runtime-pause");
  await store.put("user-settings", { id: "theme" }, { mutationId: "theme-1" });
  let releasePush;
  const pushed = new Promise((resolve) => { releasePush = resolve; });
  const checkpoint = Coordinator.createMemoryCheckpointStore();
  const runtime = SyncRuntime.createSyncRuntime({
    coordinatorApi: Coordinator,
    store,
    registry: DataRegistry,
    checkpointStore: checkpoint,
    serverGateway: {
      async push(request) {
        await pushed;
        return {
          ...empty(request),
          ackedMutationIds: request.changes.map((change) => change.mutationId),
        };
      },
      async pull(request) {
        return {
          ...empty(request),
          cursor: 1,
          headCursor: 1,
          changes: [{
            cursor: 1,
            mutationId: "server-theme",
            operation: "put",
            collection: "user-settings",
            record: {
              schema: 1,
              collection: "user-settings",
              id: "server-theme",
              rev: 1,
              updatedAt: 1,
              updatedBy: "server",
              deleted: false,
              value: { id: "server-theme", mode: "light" },
            },
          }],
        };
      },
    },
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });

  runtime.start("boot");
  const run = runtime.runNow("manual");
  runtime.pause("extension-provider-attaching");
  releasePush();
  await assert.rejects(run, (error) => error.code === "BW_SYNC_OWNER_INACTIVE");
  assert.equal(await store.get("user-settings", "server-theme"), null);
  assert.equal(checkpoint.inspect(), null);
  assert.equal(clock.size(), 0);
  runtime.destroy();
});

test("server imports do not wake a writeback run; direct/local journal changes do", async () => {
  const clock = timers();
  const store = makeStore("runtime-events");
  const runtime = SyncRuntime.createSyncRuntime({
    coordinatorApi: Coordinator,
    store,
    registry: DataRegistry,
    serverGateway: {
      async push(request) {
        return {
          ...empty(request),
          ackedMutationIds: request.changes.map((change) => change.mutationId),
        };
      },
      async pull(request) { return empty(request); },
    },
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });

  runtime.start("boot");
  assert.equal(clock.size(), 1);
  clock.runNext();
  await new Promise((resolve) => setImmediate(resolve));
  runtime.pause("inspect");
  runtime.resume("resume");
  assert.equal(clock.size(), 1);

  await store.applyChanges([{
    mutationId: "server-import",
    operation: "put",
    collection: "user-settings",
    record: {
      schema: 1,
      collection: "user-settings",
      id: "server-import",
      rev: 1,
      updatedAt: 1,
      updatedBy: "server",
      deleted: false,
      value: { id: "server-import" },
    },
  }], { journal: false, tombstoneDominates: true });
  assert.equal(clock.size(), 1);

  clock.runNext();
  await new Promise((resolve) => setImmediate(resolve));
  await store.put(
    "vocabulary-state",
    { id: "word:be", mastered: true },
    { mutationId: "local-word" },
  );
  assert.equal(clock.size(), 1);
  runtime.destroy();
});

test("pending local pages schedule another bounded run while idle success uses interval", async () => {
  const clock = timers();
  const store = makeStore("runtime-continue");
  await store.put("user-settings", { id: "one" }, { mutationId: "one" });
  await store.put("user-settings", { id: "two" }, { mutationId: "two" });
  const runtime = SyncRuntime.createSyncRuntime({
    coordinatorApi: Coordinator,
    store,
    registry: DataRegistry,
    limit: 1,
    serverGateway: {
      async push(request) {
        return {
          ...empty(request),
          ackedMutationIds: request.changes.map((change) => change.mutationId),
        };
      },
      async pull(request) { return empty(request); },
    },
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
    intervalMs: 60000,
    debounceMs: 25,
  });
  runtime.start("boot");
  const first = await runtime.runNow("manual");
  assert.equal(first.server.pendingLocal, true);
  assert.equal(clock.size(), 1);
  clock.runNext();
  await new Promise((resolve) => setImmediate(resolve));
  const status = await runtime.status();
  assert.equal(status.lastResult.server.pendingLocal, false);
  assert.equal(status.scheduled, true);
  runtime.destroy();
});

test("explicit server conflict survives automatic resume until explicit resolution", async () => {
  const clock = timers();
  const store = makeStore("runtime-conflict");
  await store.put(
    "user-settings",
    { id: "theme", mode: "dark" },
    { mutationId: "theme-local" },
  );
  const runtime = SyncRuntime.createSyncRuntime({
    coordinatorApi: Coordinator,
    store,
    registry: DataRegistry,
    serverGateway: {
      async push(request) {
        return {
          ...empty(request),
          conflicts: [{
            collection: "user-settings",
            id: "theme",
            reason: "revision-conflict",
          }],
        };
      },
      async pull(request) { return empty(request); },
    },
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });

  runtime.start("boot");
  const result = await runtime.runNow("manual");
  assert.equal(result.server.conflicts.length, 1);
  const status = await runtime.status();
  assert.equal(status.paused, true);
  assert.equal(status.reason, "sync-conflict");
  assert.equal(status.pauseReason, "sync-conflict");
  assert.equal(status.scheduled, false);
  assert.equal(clock.size(), 0);

  assert.equal(runtime.resume("periodic-alarm"), false);
  assert.equal(runtime.resume("browser-startup"), false);
  assert.equal((await runtime.runNow("periodic-alarm")).skipped, true);
  assert.equal(clock.size(), 0);

  assert.equal(runtime.resolveConflict("user-resolved-conflict"), true);
  assert.equal(clock.size(), 1);
  runtime.destroy();
});

test("direct peer conflict cannot pause the durable server owner", async () => {
  const clock = timers();
  const store = makeStore("runtime-direct-conflict");
  await store.put(
    "user-settings",
    { id: "theme", mode: "dark" },
    { mutationId: "theme-local" },
  );
  const runtime = SyncRuntime.createSyncRuntime({
    coordinatorApi: Coordinator,
    store,
    registry: DataRegistry,
    serverGateway: {
      async push(request) {
        return {
          ...empty(request),
          ackedMutationIds: request.changes.map((change) => change.mutationId),
        };
      },
      async pull(request) { return empty(request); },
    },
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });
  runtime.addPeer("transient-peer", {
    async push(request) {
      return {
        ...empty(request),
        conflicts: [{
          collection: "user-settings",
          id: "theme",
          reason: "same-rev-different-value",
        }],
      };
    },
    async pull(request) { return empty(request); },
  }, { baselineReady: true });

  runtime.start("boot");
  const result = await runtime.runNow("manual");
  assert.equal(result.direct["transient-peer"].conflicts.length, 1);
  assert.equal(result.server.conflicts.length, 0);
  const status = await runtime.status();
  assert.equal(status.paused, false);
  assert.notEqual(status.pauseReason, "sync-conflict");
  assert.equal(status.scheduled, true);
  runtime.destroy();
});

test("runtime status exposes only a stable error code and clears it after recovery", async () => {
  const clock = timers();
  const store = makeStore("runtime-safe-error");
  let attempts = 0;
  const coordinatorApi = {
    CONTRACT: "sync-coordinator/1",
    createSyncCoordinator() {
      return {
        async runOnce() {
          attempts += 1;
          if (attempts === 1) {
            const error = new Error("private upstream authentication body");
            error.code = "BW_SYNC_AUTH";
            error.retryable = false;
            throw error;
          }
          return {
            server: {
              ok: true,
              pendingLocal: false,
              conflicts: [],
            },
            direct: {},
          };
        },
        async status() {
          return { contract: "sync-coordinator/1" };
        },
        addPeer() {},
        removePeer() {},
      };
    },
  };
  const runtime = SyncRuntime.createSyncRuntime({
    coordinatorApi,
    store,
    registry: DataRegistry,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
  });

  runtime.start("boot");
  await assert.rejects(
    runtime.runNow("manual"),
    (error) => error && error.code === "BW_SYNC_AUTH",
  );
  const failed = await runtime.status();
  assert.deepEqual(failed.lastError, {
    code: "BW_SYNC_AUTH",
    retryable: false,
  });
  assert.equal(
    JSON.stringify(failed).includes("private upstream authentication body"),
    false,
  );
  assert.equal(clock.size(), 0);

  await runtime.runNow("manual-recovery");
  const recovered = await runtime.status();
  assert.equal(recovered.lastError, null);
  assert.equal(recovered.lastResult.server.ok, true);
  assert.equal(recovered.scheduled, true);
  runtime.destroy();
});
