import test from "node:test";
import assert from "node:assert/strict";
import {
  SyncGateway,
  makeRelayTransport,
  makeStore,
} from "./helpers.mjs";

test("SyncGateway 只中转不透明 change，重复 push 幂等", async () => {
  const relay = makeRelayTransport();
  const gateway = SyncGateway.createSyncGateway({ transport: relay, deviceId: "device-a" });
  const change = {
    cursor: 1,
    mutationId: "op-1",
    collection: "cards",
    operation: "put",
    record: { id: "card-1", rev: 1, value: { anchor: { private: [1, 2, 3] } } },
  };
  const first = await gateway.push({ cursor: 0, changes: [change] });
  const replay = await gateway.push({ cursor: first.cursor, changes: [change] });
  assert.deepEqual(first.ackedMutationIds, ["op-1"]);
  assert.deepEqual(replay.ackedMutationIds, ["op-1"]);
  assert.equal(relay.state.changes.length, 1);
  assert.deepEqual(relay.state.changes[0].record.value.anchor, { private: [1, 2, 3] });
});

test("离线错误可重试，本地 journal 保留", async () => {
  const store = makeStore("offline");
  await store.put("cards", { id: "offline-card" }, { mutationId: "offline-op" });
  const before = await store.changes({ after: 0 });
  const gateway = SyncGateway.createOfflineSyncGateway({ deviceId: "device-offline" });
  await assert.rejects(
    gateway.push({ cursor: 0, changes: before.changes }),
    (error) => error.code === "BW_SYNC_OFFLINE" && error.retryable === true,
  );
  assert.deepEqual(await store.changes({ after: 0 }), before);
});

test("服务器冲突显式返回，不由 Gateway 裁决", async () => {
  const gateway = SyncGateway.createSyncGateway({
    deviceId: "device-conflict",
    transport: {
      push(request) {
        return {
          cursor: request.cursor + 1,
          ackedMutationIds: [],
          changes: [],
          conflicts: [{ id: "card-1", reason: "same-rev-different-value" }],
        };
      },
      pull(request) {
        return { cursor: request.cursor, ackedMutationIds: [], changes: [], conflicts: [] };
      },
    },
  });
  const result = await gateway.push({ cursor: 0, changes: [] });
  assert.deepEqual(result.conflicts, [{ id: "card-1", reason: "same-rev-different-value" }]);
});

test("syncOnce push 后仍从原游标拉取，不跳过此前远端变化", async () => {
  const relay = makeRelayTransport();

  const remoteStore = makeStore("remote-before-push");
  await remoteStore.put(
    "cards",
    { id: "remote-card", text: "先于本轮 push 存在的远端卡片" },
    { mutationId: "remote-op" },
  );
  const remoteBatch = await remoteStore.changes({ after: 0 });
  const remoteGateway = SyncGateway.createSyncGateway({
    transport: relay,
    deviceId: "device-remote",
  });
  await remoteGateway.push({ cursor: 0, changes: remoteBatch.changes });

  const localStore = makeStore("local-sync-once");
  await localStore.put(
    "cards",
    { id: "local-card", text: "本轮上传的本地卡片" },
    { mutationId: "local-op" },
  );
  const localGateway = SyncGateway.createSyncGateway({
    transport: relay,
    deviceId: "device-local",
  });

  const result = await SyncGateway.syncOnce({
    store: localStore,
    gateway: localGateway,
    cursor: 0,
    afterLocal: 0,
  });

  assert.equal(result.cursor, 2);
  assert.equal((await localStore.get("cards", "remote-card")).value.text, "先于本轮 push 存在的远端卡片");
  assert.equal((await localStore.get("cards", "local-card")).value.text, "本轮上传的本地卡片");
});

test("syncOnce 检测到本地 journal 缺口时在联网前停止", async () => {
  const store = makeStore("local-gap");
  // makeStore 使用默认 10000 条 journal；直接模拟契约返回缺口，确保
  // SyncGateway 不会把不完整的增量当作完整批次上传。
  const originalChanges = store.changes;
  store.changes = async () => ({
    contract: "data-store/1",
    cursor: 100,
    nextCursor: 100,
    oldestCursor: 51,
    resetRequired: true,
    hasMore: false,
    changes: [],
  });
  let pushCount = 0;
  const gateway = {
    push() {
      pushCount += 1;
      return Promise.resolve({ cursor: 0, changes: [], conflicts: [], ackedMutationIds: [] });
    },
    pull() {
      throw new Error("不应调用 pull");
    },
  };

  await assert.rejects(
    SyncGateway.syncOnce({ store, gateway, afterLocal: 0, cursor: 0 }),
    (error) => error.code === "BW_SYNC_RESET_REQUIRED"
      && error.retryable === false
      && error.details.phase === "local-journal"
      && error.details.oldestCursor === 51,
  );
  assert.equal(pushCount, 0);
  store.changes = originalChanges;
});

test("syncOnce 检测到远端增量缺口时不落库并要求完整对账", async () => {
  const store = makeStore("remote-gap");
  let applyCount = 0;
  const originalApply = store.applyChanges;
  store.applyChanges = async (...args) => {
    applyCount += 1;
    return originalApply(...args);
  };
  const gateway = SyncGateway.createSyncGateway({
    deviceId: "remote-gap-device",
    transport: {
      push(request) {
        return {
          cursor: request.cursor,
          oldestCursor: 0,
          resetRequired: false,
          changes: [],
          conflicts: [],
          ackedMutationIds: [],
        };
      },
      pull() {
        return {
          cursor: 80,
          oldestCursor: 41,
          resetRequired: true,
          changes: [{
            collection: "cards",
            mutationId: "must-not-apply",
            record: { id: "unsafe-card", rev: 1, value: { id: "unsafe-card" } },
          }],
          conflicts: [],
          ackedMutationIds: [],
        };
      },
    },
  });

  await assert.rejects(
    SyncGateway.syncOnce({ store, gateway, cursor: 0, afterLocal: 0 }),
    (error) => error.code === "BW_SYNC_RESET_REQUIRED"
      && error.details.phase === "remote-pull"
      && error.details.oldestCursor === 41,
  );
  assert.equal(applyCount, 0);
  assert.equal(await store.get("cards", "unsafe-card"), null);
});

test("syncOnce 分页上传返回最后已发送的 localCursor，不跳过下一页", async () => {
  const relay = makeRelayTransport();
  const store = makeStore("paged-local");
  for (let index = 1; index <= 3; index += 1) {
    await store.put(
      "cards",
      { id: `paged-card-${index}` },
      { mutationId: `paged-op-${index}` },
    );
  }
  const gateway = SyncGateway.createSyncGateway({
    transport: relay,
    deviceId: "paged-device",
  });

  const first = await SyncGateway.syncOnce({
    store,
    gateway,
    cursor: 0,
    afterLocal: 0,
    limit: 2,
  });
  assert.equal(first.localCursor, 2);
  assert.equal(first.localHasMore, true);
  assert.equal(relay.state.changes.length, 2);

  const second = await SyncGateway.syncOnce({
    store,
    gateway,
    cursor: first.cursor,
    afterLocal: first.localCursor,
    limit: 2,
  });
  assert.equal(second.localCursor, 3);
  assert.equal(second.localHasMore, false);
  assert.equal(relay.state.changes.length, 3);
});
