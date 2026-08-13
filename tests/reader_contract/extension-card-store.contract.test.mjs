import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import { webcrypto } from "node:crypto";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "../..");
const FACADE_PATH = path.join(
  ROOT,
  "extensions/bw-reader-webext/src/facade.js",
);
const MANIFEST_PATH = path.join(
  ROOT,
  "extensions/bw-reader-webext/manifest.json",
);
const CARD_REPOSITORY_PATH = path.join(
  ROOT,
  "_server_deploy/static/reader-runtime/card-repository.js",
);
const SOURCE = fs.readFileSync(FACADE_PATH, "utf8");
const MANIFEST = JSON.parse(fs.readFileSync(MANIFEST_PATH, "utf8"));
const CARD_REPOSITORY_SOURCE = fs.readFileSync(
  CARD_REPOSITORY_PATH,
  "utf8",
);
const FACTORY_START = SOURCE.indexOf(
  "  function createCardStoreTransport(environment) {",
);
const FACTORY_END = SOURCE.indexOf(
  "\n  const cardStoreTransport = createCardStoreTransport({ window, chrome });",
  FACTORY_START,
);
const RUNTIME_END = SOURCE.indexOf(
  "\n\n  const nativeBridgeEncoder",
  FACTORY_END,
);
assert.ok(FACTORY_START >= 0 && FACTORY_END > FACTORY_START);
assert.ok(RUNTIME_END > FACTORY_END);
const FACTORY_SOURCE = SOURCE.slice(FACTORY_START, FACTORY_END);
const RUNTIME_SOURCE = SOURCE.slice(FACTORY_START, RUNTIME_END);
const CAPABILITIES = {
  collections: ["card-entities", "card-states"],
  operations: ["batch", "get", "list", "put", "remove", "subscribe"],
};

function loadFactory() {
  const sandbox = {
    Error,
    JSON,
    Map,
    Number,
    Object,
    Promise,
    Set,
    String,
  };
  vm.runInNewContext(
    `${FACTORY_SOURCE}\nglobalThis.factory = createCardStoreTransport;`,
    sandbox,
    { filename: "facade-card-store-transport.js" },
  );
  return sandbox.factory;
}

function makePort() {
  const messageListeners = [];
  const disconnectListeners = [];
  return {
    messages: [],
    disconnected: false,
    onMessage: {
      addListener(listener) {
        messageListeners.push(listener);
      },
    },
    onDisconnect: {
      addListener(listener) {
        disconnectListeners.push(listener);
      },
    },
    postMessage(message) {
      this.messages.push(structuredClone(message));
    },
    disconnect() {
      if (this.disconnected) return;
      this.disconnected = true;
      for (const listener of [...disconnectListeners]) listener();
    },
    receive(message) {
      const incoming = this.realmClone
        ? this.realmClone(message)
        : structuredClone(message);
      for (const listener of [...messageListeners]) {
        listener(incoming);
      }
    },
  };
}

function harness(options = {}) {
  const ports = [];
  const targetWindow = {
    structuredClone,
    setTimeout,
    clearTimeout,
  };
  const chrome = {
    runtime: {
      lastError: null,
      connect(connectOptions) {
        assert.equal(connectOptions?.name, "bw-card-store");
        const port = makePort();
        ports.push(port);
        return port;
      },
    },
  };
  const transport = loadFactory()({
    window: targetWindow,
    chrome,
    requestTimeoutMs: 1000,
    ...options,
  });
  return { chrome, ports, targetWindow, transport };
}

const message = (value) => ({
  protocol: "bw-card-store/1",
  ...value,
});

const clean = (value) => JSON.parse(JSON.stringify(value));

async function waitFor(predicate, timeoutMs = 1000) {
  const started = Date.now();
  while (!predicate()) {
    if (Date.now() - started > timeoutMs) {
      throw new Error("condition timed out");
    }
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

function sendReady(port, capabilities = CAPABILITIES) {
  port.receive(message({ type: "READY", capabilities }));
}

test("manifest 在 facade 后、卡片 UI 前加载 card-repository，且不把 IndexedDB runtime 注入网页", () => {
  const full = MANIFEST.content_scripts[1];
  const facadeIndex = full.js.indexOf("src/facade.js");
  const repositoryIndex = full.js.indexOf(
    "vendor/reader-runtime-card-repository.js",
  );
  const flashcardIndex = full.js.indexOf("vendor/rc-flashcard.js");
  assert.ok(facadeIndex >= 0);
  assert.ok(repositoryIndex > facadeIndex);
  assert.ok(flashcardIndex > repositoryIndex);
  assert.equal(full.js.includes("vendor/reader-runtime-data-store.js"), false);
  assert.equal(full.js.includes("vendor/reader-runtime-indexeddb-store.js"), false);
  assert.equal(Object.hasOwn(full, "world"), false);
  assert.equal(FACTORY_SOURCE.includes("window.postMessage"), false);
  assert.equal(FACTORY_SOURCE.includes("document.dispatchEvent"), false);
  assert.equal(FACTORY_SOURCE.includes("namespace"), false);
  assert.equal(FACTORY_SOURCE.includes("token"), false);
});

test("隔离世界 runtime.storage 只返回冻结的六方法后台门面", () => {
  const ports = [];
  const window = {
    structuredClone,
    setTimeout,
    clearTimeout,
  };
  const chrome = {
    runtime: {
      lastError: null,
      connect({ name }) {
        assert.equal(name, "bw-card-store");
        const port = makePort();
        ports.push(port);
        return port;
      },
    },
  };
  vm.runInNewContext(RUNTIME_SOURCE, {
    window,
    chrome,
    Error,
    JSON,
    Map,
    Number,
    Object,
    Promise,
    Set,
    String,
  }, { filename: "facade-card-store-runtime.js" });
  const store = window.__BW_READER_RUNTIME__.storage();
  assert.equal(store, window.__BW_READER_RUNTIME__.storage());
  assert.equal(Object.isFrozen(store), true);
  assert.deepEqual(
    Object.keys(store).sort(),
    ["batch", "contract", "get", "list", "put", "remove", "subscribe"],
  );
  assert.equal(store.contract, "data-store/1");
  assert.equal(Object.hasOwn(window, "__bwCardStoreTransport"), false);
  assert.equal(ports.length, 0, "加载仓库门面本身不能提前打开账户 Vault");
});

test("card-repository 默认实例经 runtime.storage 原子写后台，不创建页面 IndexedDB", async () => {
  const ports = [];
  const sandbox = {
    chrome: {
      runtime: {
        lastError: null,
        connect({ name }) {
          assert.equal(name, "bw-card-store");
          const port = makePort();
          ports.push(port);
          return port;
        },
      },
    },
    crypto: webcrypto,
    TextEncoder,
    structuredClone,
    setTimeout,
    clearTimeout,
  };
  sandbox.window = sandbox;
  const context = vm.createContext(sandbox);
  vm.runInContext(RUNTIME_SOURCE, context, {
    filename: "facade-card-store-runtime.js",
  });
  vm.runInContext(CARD_REPOSITORY_SOURCE, context, {
    filename: "card-repository.js",
  });
  const repository = sandbox.BWReaderRuntime.cardRepository;
  const id = `card_${"a".repeat(32)}`;
  const pending = vm.runInContext(`
    BWReaderRuntime.cardRepository.saveConfirmedCard({
      id: ${JSON.stringify(id)},
      cardIndex: 0,
      cards: [{ type: "basic", front: "Q", back: "A" }],
      source: {
        kind: "reader-selection",
        sourceId: "web:https://example.com/article#selection-1"
      }
    }, { mutationId: "repo-confirm" })
  `, context);
  let pendingFailure = null;
  pending.catch((cause) => { pendingFailure = cause; });

  await waitFor(() => ports.length === 1);
  const port = ports[0];
  port.realmClone = vm.runInContext(
    "(value) => JSON.parse(JSON.stringify(value))",
    context,
  );
  sendReady(port);
  await waitFor(() => port.messages.length === 2);
  for (const call of port.messages.slice(0, 2)) {
    assert.equal(call.operation, "get");
    assert.equal(
      ["card-entities", "card-states"].includes(call.args.collection),
      true,
    );
    port.receive(message({
      type: "RESULT",
      id: call.id,
      ok: true,
      data: null,
    }));
  }
  await waitFor(() => port.messages.length === 3 || pendingFailure);
  assert.ifError(pendingFailure);
  const batch = port.messages[2];
  assert.equal(batch.operation, "batch");
  assert.deepEqual(
    clean(batch.args.mutations.map((mutation) => mutation.collection)),
    ["card-entities", "card-states"],
  );
  port.receive(message({
    type: "RESULT",
    id: batch.id,
    ok: true,
    data: batch.args.mutations.map((mutation) => ({
      schema: 1,
      collection: mutation.collection,
      id: mutation.options.id,
      rev: 1,
      updatedAt: 1,
      updatedBy: "extension-test",
      deleted: false,
      value: mutation.value,
    })),
  }));
  const saved = await pending;
  assert.equal(saved.id, id);
  assert.equal(saved.cid, id);
  assert.equal(saved.gid, id);
  assert.equal(saved.states[0].phase, "confirmed");
  assert.equal(Object.hasOwn(sandbox, "indexedDB"), false);
  assert.equal(JSON.stringify(port.messages).includes("namespace"), false);
});

test("subscribe 在后续写入前完成，CHANGE 只投递对应 collection", async () => {
  const h = harness();
  const changes = [];
  const unsubscribe = h.transport.subscribe(
    { collection: "card-entities" },
    (change) => changes.push(change),
  );
  const batchPromise = h.transport.batch([{
    collection: "card-entities",
    value: { id: "card_aaaa" },
    options: { mutationId: "m-entity", ifRev: 0, id: "card_aaaa" },
  }]);
  await waitFor(() => h.ports.length === 1);
  const port = h.ports[0];
  assert.equal(port.messages.length, 0);
  sendReady(port);
  await waitFor(() => port.messages.length === 1);
  const subscribeCall = port.messages[0];
  assert.deepEqual(clean(subscribeCall), {
    protocol: "bw-card-store/1",
    type: "CALL",
    id: subscribeCall.id,
    operation: "subscribe",
    args: { query: { collection: "card-entities" } },
  });
  assert.equal(
    port.messages.some((item) => item.operation === "batch"),
    false,
  );

  port.receive(message({
    type: "RESULT",
    id: subscribeCall.id,
    ok: true,
    data: { collection: "card-entities", subscribed: true },
  }));
  await waitFor(() => port.messages.length === 2);
  const batchCall = port.messages[1];
  assert.equal(batchCall.operation, "batch");

  const entityChange = {
    collection: "card-entities",
    mutationId: "m-entity",
    record: { collection: "card-entities", id: "card_aaaa", rev: 1 },
  };
  port.receive(message({ type: "CHANGE", data: entityChange }));
  assert.deepEqual(clean(changes), [entityChange]);
  port.receive(message({
    type: "RESULT",
    id: batchCall.id,
    ok: true,
    data: [],
  }));
  assert.deepEqual(await batchPromise, []);
  unsubscribe();
  port.receive(message({
    type: "CHANGE",
    data: { ...entityChange, mutationId: "after-unsubscribe" },
  }));
  assert.equal(changes.length, 1);
});

test("get/list/put/remove 保留 DataStore 参数，不允许调用方选择其它 collection", async () => {
  const h = harness();
  const getPromise = h.transport.get(
    "card-entities",
    "card_aaaa",
    { includeDeleted: true },
  );
  await waitFor(() => h.ports.length === 1);
  const port = h.ports[0];
  sendReady(port);
  await waitFor(() => port.messages.length === 1);
  const getCall = port.messages[0];
  assert.deepEqual(clean(getCall.args), {
    collection: "card-entities",
    id: "card_aaaa",
    options: { includeDeleted: true },
  });
  port.receive(message({
    type: "RESULT",
    id: getCall.id,
    ok: true,
    data: { id: "card_aaaa", rev: 1 },
  }));
  assert.equal((await getPromise).rev, 1);

  const calls = [
    [
      h.transport.list("card-states", {
        includeDeleted: false,
        offset: 0,
        limit: 200,
      }),
      "list",
      [],
    ],
    [
      h.transport.put("card-states", { id: "card_aaaa" }, {
        id: "card_aaaa",
        ifRev: 0,
        mutationId: "put-state",
      }),
      "put",
      { id: "card_aaaa", rev: 1 },
    ],
    [
      h.transport.remove("card-states", "card_aaaa", {
        ifRev: 1,
        mutationId: "remove-state",
      }),
      "remove",
      { id: "card_aaaa", rev: 2, deleted: true },
    ],
  ];
  for (let index = 0; index < calls.length; index += 1) {
    const [promise, operation, result] = calls[index];
    await waitFor(() => port.messages.length >= index + 2);
    const call = port.messages[index + 1];
    assert.equal(call.operation, operation);
    assert.equal(Object.hasOwn(call.args, "namespace"), false);
    port.receive(message({
      type: "RESULT",
      id: call.id,
      ok: true,
      data: result,
    }));
    assert.deepEqual(clean(await promise), result);
  }

  assert.throws(
    () => h.transport.get("user-settings", "card_aaaa"),
    (cause) => cause.code === "BW_CARD_STORE_COLLECTION",
  );
  assert.equal(h.ports.length, 1);
});

test("物理断线标记在途写结果未知，并在新 generation 自动重连恢复订阅", async () => {
  const h = harness({ reconnectBaseMs: 10 });
  const invalidations = [];
  h.transport.subscribe(
    { collection: "card-states" },
    (event) => invalidations.push(event),
  );
  await waitFor(() => h.ports.length === 1);
  const port = h.ports[0];
  sendReady(port);
  await waitFor(() => port.messages.length === 1);
  const subscribeCall = port.messages[0];
  port.receive(message({
    type: "RESULT",
    id: subscribeCall.id,
    ok: true,
    data: { collection: "card-states", subscribed: true },
  }));

  const putPromise = h.transport.put(
    "card-states",
    { id: "card_aaaa" },
    { id: "card_aaaa", ifRev: 0, mutationId: "state-put-unknown" },
  );
  await waitFor(() => port.messages.length === 2);
  port.disconnect();
  await assert.rejects(putPromise, (cause) => (
    cause.code === "BW_CARD_STORE_DISCONNECTED" &&
    cause.details?.outcomeUnknown === true &&
    cause.details?.mutationId === "state-put-unknown"
  ));
  assert.equal(invalidations.length, 1);
  assert.equal(invalidations[0].type, "RECONNECTING");

  await waitFor(() => h.ports.length === 2);
  const restored = h.ports[1];
  sendReady(restored);
  await waitFor(() => restored.messages.length === 1);
  const restoredSubscribe = restored.messages[0];
  assert.equal(restoredSubscribe.operation, "subscribe");
  restored.receive(message({
    type: "RESULT",
    id: restoredSubscribe.id,
    ok: true,
    data: { collection: "card-states", subscribed: true },
  }));

  const listPromise = h.transport.list("card-states", {});
  await waitFor(() => restored.messages.length === 2);
  const listCall = restored.messages[1];
  assert.equal(listCall.operation, "list");
  restored.receive(message({
    type: "RESULT",
    id: listCall.id,
    ok: true,
    data: [],
  }));
  assert.deepEqual(await listPromise, []);
  assert.equal(
    invalidations.some((event) => event.type === "RECONNECTED"),
    true,
  );
});

test("后台明确拒绝未验证账户时，第一次调用返回原始错误，之后保持 fail closed", async () => {
  const h = harness();
  const pending = h.transport.get("card-entities", "card_aaaa", {});
  await waitFor(() => h.ports.length === 1);
  const port = h.ports[0];
  port.receive(message({
    type: "ERROR",
    code: "BW_ACCOUNT_CONTEXT_UNAVAILABLE",
    error: "尚未验证账户",
  }));
  await assert.rejects(
    pending,
    (cause) => cause.code === "BW_ACCOUNT_CONTEXT_UNAVAILABLE",
  );
  await assert.rejects(
    h.transport.get("card-entities", "card_aaaa", {}),
    (cause) => cause.code === "BW_CARD_STORE_STALE",
  );
  assert.equal(h.ports.length, 1);
});
