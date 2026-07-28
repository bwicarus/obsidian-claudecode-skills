import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { DataRegistry, makeStore } from "./helpers.mjs";

const require = createRequire(import.meta.url);
const Coordinator = require(
  "../../_server_deploy/static/reader-runtime/sync-coordinator.js",
);

function emptyPull(request) {
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

test("relay 确认同记录的旧冲突与后续版本时，本地游标一次越过完整连续批次", async () => {
  const store = makeStore("semantic-convergence");
  const first = await store.put(
    "user-settings",
    { id: "theme", value: "dark" },
    { mutationId: "theme-old" },
  );
  await store.put(
    "user-settings",
    { id: "theme", value: "light" },
    { ifRev: first.rev, mutationId: "theme-final" },
  );

  const pushes = [];
  const coordinator = Coordinator.createSyncCoordinator({
    store,
    registry: DataRegistry,
    checkpointStore: Coordinator.createMemoryCheckpointStore(),
    serverGateway: {
      async push(request) {
        pushes.push(structuredClone(request.changes));
        return {
          ...emptyPull(request),
          // This is the relay's safe same-push supersession result: the older
          // journal mutation is acknowledged together with its later state.
          ackedMutationIds: ["theme-old", "theme-final"],
        };
      },
      async pull(request) {
        return emptyPull(request);
      },
    },
  });

  const result = await coordinator.runOnce();
  assert.equal(pushes.length, 1);
  assert.deepEqual(
    pushes[0].map((change) => change.mutationId),
    ["theme-old", "theme-final"],
  );
  assert.equal(result.server.localCursor, 2);
  assert.equal(result.checkpoint.server.localCursor, 2);
  assert.equal(result.server.pendingLocal, false);
});
