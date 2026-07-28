import assert from "node:assert/strict";
import test from "node:test";
import { createRequire } from "node:module";
import {
  DataRegistry,
  makeStore,
} from "./helpers.mjs";

const require = createRequire(import.meta.url);
const Direct = require(
  "../../_server_deploy/static/reader-runtime/direct-sync-protocol.js",
);

const IDENTITY = Object.freeze({
  sessionId: "direct-session-v1-test",
  accountProof: "proof-v1-test-account-proof",
  registryDigest:
    "sync-v2:cards:explicit:0:1|highlights:explicit:0:1|notes:explicit:0:1",
});

function gatewayRequest(overrides = {}) {
  return {
    contract: "sync-gateway/2",
    direction: "push",
    deviceId: "device-left",
    cursor: 0,
    limit: 500,
    changes: [],
    ...overrides,
  };
}

function gatewayResult(overrides = {}) {
  return {
    contract: "sync-gateway/2",
    cursor: 0,
    headCursor: 0,
    oldestCursor: 0,
    hasMore: false,
    resetRequired: false,
    ackedMutationIds: [],
    changes: [],
    conflicts: [],
    ...overrides,
  };
}

function change(content, suffix = "1") {
  return {
    cursor: Number(suffix) || 1,
    mutationId: `mutation-${suffix}`,
    operation: "put",
    collection: "cards",
    record: {
      id: `card-${suffix}`,
      rev: 1,
      updatedAt: "2026-07-26T00:00:00.000Z",
      value: { content },
    },
  };
}

class FakeChannel {
  constructor({ events = true, deliver = true } = {}) {
    this.readyState = "open";
    this.bufferedAmount = 0;
    this.bufferedAmountLowThreshold = 0;
    this.sent = [];
    this.peer = null;
    this.deliver = deliver;
    this.closed = false;
    this.listeners = new Map();
    this.onmessage = null;
    if (!events) {
      this.addEventListener = undefined;
      this.removeEventListener = undefined;
    }
  }

  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, new Set());
    this.listeners.get(type).add(listener);
  }

  removeEventListener(type, listener) {
    this.listeners.get(type)?.delete(listener);
  }

  emit(type, event = {}) {
    for (const listener of this.listeners.get(type) || []) listener(event);
    const property = this[`on${type}`];
    if (typeof property === "function") property.call(this, event);
  }

  send(data) {
    if (this.readyState !== "open") throw new Error("channel closed");
    this.sent.push(data);
    if (!this.deliver || !this.peer) return;
    const peer = this.peer;
    queueMicrotask(() => peer.emit("message", { data }));
  }

  close() {
    this.closed = true;
    this.readyState = "closed";
    this.emit("close", {});
  }
}

function channelPair(options = {}) {
  const left = new FakeChannel(options.left);
  const right = new FakeChannel(options.right);
  left.peer = right;
  right.peer = left;
  return [left, right];
}

function transport(channel, options = {}) {
  return Direct.createChannelTransport({
    ...IDENTITY,
    channel,
    ...options,
  });
}

function echoRelay(capture = []) {
  return {
    async exchange(request) {
      capture.push(request);
      return gatewayResult({
        cursor: request.cursor,
        headCursor: request.cursor,
        ackedMutationIds: request.changes.map((item) => item.mutationId),
      });
    },
  };
}

async function ticks(count = 4) {
  for (let index = 0; index < count; index += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

test("大消息按 16–32 KiB 线框分帧并无损重组 Unicode", async () => {
  const [leftChannel, rightChannel] = channelPair();
  const received = [];
  const left = transport(leftChannel);
  const right = transport(rightChannel, { relay: echoRelay(received) });
  const payload = gatewayRequest({
    changes: [{
      ...change("汉字🙂".repeat(24_000)),
      imported: true,
      remote: false,
    }],
  });

  const result = await left.exchange(payload);
  assert.deepEqual(result.ackedMutationIds, ["mutation-1"]);
  assert.equal(received.length, 1);
  assert.equal(
    received[0].changes[0].record.value.content,
    payload.changes[0].record.value.content,
  );
  assert.equal("imported" in received[0].changes[0], false);
  assert.equal("remote" in received[0].changes[0], false);
  assert.ok(leftChannel.sent.length > 1);
  for (const raw of leftChannel.sent) {
    assert.ok(Buffer.byteLength(raw) <= Direct.MAX_FRAME_WIRE_BYTES);
    const frame = JSON.parse(raw);
    assert.equal(frame.contract, Direct.CONTRACT);
    assert.equal(frame.type, "FRAME");
  }

  left.close();
  right.close();
});

test("总消息超过 1 MiB 会在发送前 fail closed", async () => {
  const channel = new FakeChannel({ deliver: false });
  const direct = transport(channel);
  await assert.rejects(
    direct.exchange(gatewayRequest({
      changes: [change("x".repeat(Direct.MAX_MESSAGE_BYTES + 128))],
    })),
    (error) => error.code === "BW_DIRECT_TOO_LARGE" && !error.retryable,
  );
  assert.equal(channel.sent.length, 0);
  assert.equal((await direct.status()).pendingRequests, 0);
  direct.close();
});

test("bufferedAmount 高水位暂停发送，低水位事件后恢复", async () => {
  const [leftChannel, rightChannel] = channelPair();
  leftChannel.bufferedAmount = 300 * 1024;
  const left = transport(leftChannel, {
    bufferedAmountHighWater: 128 * 1024,
    bufferedAmountLowWater: 32 * 1024,
  });
  const right = transport(rightChannel, { relay: echoRelay() });
  const pending = left.exchange(gatewayRequest());

  await ticks(2);
  assert.equal(leftChannel.sent.length, 0);
  leftChannel.bufferedAmount = 0;
  leftChannel.emit("bufferedamountlow", {});
  await pending;
  assert.ok(leftChannel.sent.length > 0);

  left.close();
  right.close();
});

test("pending 请求数有硬上限，close 会结清所有 waiter", async () => {
  const channel = new FakeChannel({ deliver: false });
  const direct = transport(channel, {
    maxPendingRequests: 2,
    timeoutMs: 10_000,
  });
  const first = direct.exchange(gatewayRequest()).catch((error) => error);
  const second = direct.exchange(gatewayRequest()).catch((error) => error);
  await ticks(2);
  await assert.rejects(
    direct.exchange(gatewayRequest()),
    (error) => error.code === "BW_DIRECT_BUSY" && error.retryable,
  );
  assert.equal((await direct.status()).pendingRequests, 2);

  direct.close("test-close");
  assert.equal((await first).code, "BW_DIRECT_OFFLINE");
  assert.equal((await second).code, "BW_DIRECT_OFFLINE");
  assert.equal((await direct.status()).pendingRequests, 0);
});

test("proof、digest 或 session 不匹配会安全关闭专用信道", async () => {
  const [leftChannel, rightChannel] = channelPair();
  const left = transport(leftChannel);
  const right = transport(rightChannel, {
    accountProof: "proof-v1-another-account",
    relay: echoRelay(),
  });
  const pending = left.exchange(gatewayRequest()).catch((error) => error);
  await ticks(6);

  assert.equal(rightChannel.closed, true);
  const status = await right.status();
  assert.equal(status.state, "offline");
  assert.equal(status.error.code, "BW_DIRECT_FENCE");

  left.close();
  assert.equal((await pending).code, "BW_DIRECT_OFFLINE");
  right.close();
});

test("不完整分帧会超时释放，有界重组队列溢出时关闭", async () => {
  const capture = new FakeChannel({ deliver: false });
  const sender = transport(capture, {
    maxPendingRequests: 4,
    timeoutMs: 10_000,
  });
  const sends = ["a", "b", "c"].map((suffix, index) => sender.exchange(
    gatewayRequest({
      changes: [change(suffix.repeat(40_000), String(index + 1))],
    }),
  ).catch((error) => error));
  await ticks(8);
  const firstFrames = new Map();
  for (const raw of capture.sent) {
    const frame = JSON.parse(raw);
    if (!firstFrames.has(frame.messageId)) firstFrames.set(frame.messageId, raw);
  }
  assert.equal(firstFrames.size, 3);

  const inboundChannel = new FakeChannel({ deliver: false });
  const receiver = transport(inboundChannel, {
    relay: echoRelay(),
    maxInboundAssemblies: 2,
    reassemblyTimeoutMs: 50,
  });
  const frames = [...firstFrames.values()];
  inboundChannel.emit("message", { data: frames[0] });
  assert.equal((await receiver.status()).inboundAssemblies, 1);
  await new Promise((resolve) => setTimeout(resolve, 80));
  assert.equal((await receiver.status()).inboundAssemblies, 0);

  inboundChannel.emit("message", { data: frames[0] });
  inboundChannel.emit("message", { data: frames[1] });
  assert.equal((await receiver.status()).inboundAssemblies, 2);
  inboundChannel.emit("message", { data: frames[2] });
  assert.equal(inboundChannel.closed, true);
  assert.equal((await receiver.status()).error.code, "BW_DIRECT_BUSY");

  sender.close();
  receiver.close();
  await Promise.all(sends);
});

test("严格拒绝未知字段、非法 direction 和 pull 携带 changes", async () => {
  const channel = new FakeChannel({ deliver: false });
  const direct = transport(channel);
  await assert.rejects(
    direct.exchange(gatewayRequest({ direction: "delete" })),
    (error) => error.code === "BW_DIRECT_INVALID",
  );
  await assert.rejects(
    direct.exchange(gatewayRequest({
      direction: "pull",
      changes: [change("not-allowed")],
    })),
    (error) => error.code === "BW_DIRECT_INVALID",
  );
  await assert.rejects(
    direct.exchange({ ...gatewayRequest(), extra: true }),
    (error) => error.code === "BW_DIRECT_INVALID",
  );
  assert.equal(channel.sent.length, 0);
  direct.close();
});

test("store relay 精确 ACK 顺序子项、保留分支冲突并接受墓碑线性续写", async () => {
  const store = makeStore("direct-causal-relay");
  const base = await store.put(
    "user-settings",
    { id: "shared", value: "base" },
    { mutationId: "direct-base" },
  );
  const child = {
    ...base,
    rev: 1,
    updatedAt: base.updatedAt + 1,
    updatedBy: "peer-a",
    value: { id: "shared", value: "child" },
    causal: {
      contract: "record-parent-state/1",
      parent: { deleted: false, value: structuredClone(base.value) },
    },
  };
  const grandchild = {
    ...child,
    rev: 1,
    updatedAt: child.updatedAt + 1,
    value: { id: "shared", value: "grandchild" },
    causal: {
      contract: "record-parent-state/1",
      parent: { deleted: false, value: structuredClone(child.value) },
    },
  };
  const relay = Direct.createStoreRelay({ store, registry: DataRegistry });
  const sequential = await relay.exchange(gatewayRequest({
    changes: [
      {
        cursor: 1,
        mutationId: "direct-child",
        operation: "put",
        collection: "user-settings",
        record: child,
      },
      {
        cursor: 2,
        mutationId: "direct-grandchild",
        operation: "put",
        collection: "user-settings",
        record: grandchild,
      },
    ],
  }));
  assert.deepEqual(
    sequential.ackedMutationIds,
    ["direct-child", "direct-grandchild"],
  );
  assert.equal(sequential.conflicts.length, 0);
  assert.equal((await store.get("user-settings", "shared")).value.value, "grandchild");

  const offlineBranch = {
    ...child,
    rev: 999,
    updatedBy: "peer-b",
    value: { id: "shared", value: "offline-branch" },
  };
  const missingProof = { ...offlineBranch };
  delete missingProof.causal;
  const branchResult = await relay.exchange(gatewayRequest({
    changes: [
      {
        cursor: 3,
        mutationId: "direct-offline-branch",
        operation: "put",
        collection: "user-settings",
        record: offlineBranch,
      },
      {
        cursor: 4,
        mutationId: "direct-missing-proof",
        operation: "put",
        collection: "user-settings",
        record: missingProof,
      },
    ],
  }));
  assert.deepEqual(branchResult.ackedMutationIds, []);
  assert.deepEqual(
    branchResult.conflicts.map((item) => [
      item.mutationId,
      item.reason,
    ]),
    [
      ["direct-offline-branch", "causal-parent-mismatch"],
      ["direct-missing-proof", "causal-proof-missing"],
    ],
  );
  assert.equal((await store.get("user-settings", "shared")).value.value, "grandchild");

  const tombstone = await store.remove(
    "user-settings",
    "shared",
    { mutationId: "direct-tombstone" },
  );
  const linearTombstoneChild = {
    ...tombstone,
    rev: tombstone.rev + 1,
    updatedAt: tombstone.updatedAt + 1,
    updatedBy: "peer-c",
    deleted: false,
    value: { id: "shared", value: "returned-linearly" },
    causal: {
      contract: "record-parent-state/1",
      parent: { deleted: true },
    },
  };
  const missingTombstoneParent = { ...linearTombstoneChild };
  delete missingTombstoneParent.causal;
  const wrongTombstoneParent = {
    ...linearTombstoneChild,
    causal: {
      contract: "record-parent-state/1",
      parent: { deleted: false, value: structuredClone(grandchild.value) },
    },
  };
  const tombstoneResult = await relay.exchange(gatewayRequest({
    changes: [
      {
        cursor: 5,
        mutationId: "direct-tombstone-missing-parent",
        operation: "put",
        collection: "user-settings",
        record: missingTombstoneParent,
      },
      {
        cursor: 6,
        mutationId: "direct-tombstone-wrong-parent",
        operation: "put",
        collection: "user-settings",
        record: wrongTombstoneParent,
      },
      {
        cursor: 7,
        mutationId: "direct-tombstone-linear-child",
        operation: "put",
        collection: "user-settings",
        record: linearTombstoneChild,
      },
    ],
  }));
  assert.deepEqual(
    tombstoneResult.ackedMutationIds,
    ["direct-tombstone-linear-child"],
  );
  assert.deepEqual(
    tombstoneResult.conflicts.map((item) => [
      item.mutationId,
      item.reason,
    ]),
    [
      ["direct-tombstone-missing-parent", "tombstone-dominates"],
      ["direct-tombstone-wrong-parent", "tombstone-dominates"],
    ],
  );
  assert.equal(
    (await store.get("user-settings", "shared")).value.value,
    "returned-linearly",
  );
});

test("onmessage fallback 在关闭时恢复此前 handler", async () => {
  const channel = new FakeChannel({ events: false, deliver: false });
  let previousCalls = 0;
  const previous = () => { previousCalls += 1; };
  channel.onmessage = previous;
  const direct = transport(channel);
  assert.notEqual(channel.onmessage, previous);
  direct.close();
  assert.equal(channel.onmessage, previous);
  channel.onmessage({ data: "ignored" });
  assert.equal(previousCalls, 1);
});
