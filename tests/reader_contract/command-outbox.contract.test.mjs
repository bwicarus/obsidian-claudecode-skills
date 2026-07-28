import test from "node:test";
import assert from "node:assert/strict";
import { webcrypto } from "node:crypto";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const require = createRequire(import.meta.url);
const AccountContext = require(
  "../../_server_deploy/static/reader-runtime/account-context.js",
);
const InteractionPolicy = require(
  "../../_server_deploy/static/reader-runtime/interaction-policy.js",
);
const SOURCE = readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-outbox.js", import.meta.url),
  "utf8",
);
const ACCOUNT_A = `acct-v1-${"a".repeat(64)}`;
const ACCOUNT_B = `acct-v1-${"b".repeat(64)}`;
const LEGACY_KEY = "rc-outbox-v1";

function batchResponse(ownerNamespace, statuses, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() {
      return {
        ok: true,
        contract: "command-outbox/2",
        ownerNamespace,
        results: statuses.map((value) => ({ status: value })),
      };
    },
  };
}

function sharedStorage() {
  const map = new Map();
  const storage = {
    get length() { return map.size; },
    key(index) { return [...map.keys()][index] ?? null; },
    getItem(key) { return map.has(key) ? map.get(key) : null; },
    setItem(key, value) { map.set(String(key), String(value)); },
    removeItem(key) { map.delete(String(key)); },
    clear() { map.clear(); },
    map,
  };
  storage.setItem(
    LEGACY_KEY,
    JSON.stringify({
      "vocab:legacy-a": { url: "/pdf/api/vocab-mark" },
      "note:legacy-b": { url: "/pdf/api/notes" },
    }),
  );
  return storage;
}

function createWindow({
  storage = sharedStorage(),
  clock = { value: 1_000 },
  fetchImpl,
  interactionPolicy = InteractionPolicy,
} = {}) {
  const account = AccountContext.createContext();
  const requests = [];
  const listeners = new Map();
  const timers = new Map();
  let timerSequence = 0;
  class ClockDate extends Date {
    static now() { return clock.value; }
  }
  const network = async (url, options) => {
    requests.push({ url, options });
    if (fetchImpl) return fetchImpl(url, options, requests);
    const body = JSON.parse(options.body);
    return batchResponse(body.ownerNamespace, body.ops.map(() => 200));
  };
  const addListener = (name, listener) => {
    if (!listeners.has(name)) listeners.set(name, []);
    listeners.get(name).push(listener);
  };
  const sandbox = {
    console,
    URL,
    Blob,
    Response,
    Uint8Array,
    Date: ClockDate,
    Object,
    Array,
    JSON,
    Math,
    Promise,
    crypto: webcrypto,
    location: {
      origin: "https://reader.example",
      href: "https://reader.example/pdf/view?file=book.pdf",
    },
    navigator: {
      onLine: true,
      sendBeacon() { return true; },
    },
    document: {
      visibilityState: "visible",
      addEventListener: addListener,
    },
    localStorage: storage,
    fetch: network,
    setTimeout(callback) {
      const id = ++timerSequence;
      timers.set(id, callback);
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
    setInterval() { return ++timerSequence; },
    clearInterval() {},
    addEventListener: addListener,
    BWReaderRuntime: {
      accountContext: account,
      interactionPolicy,
    },
    RC: {},
  };
  sandbox.window = sandbox;
  vm.runInContext(SOURCE, vm.createContext(sandbox), {
    filename: "rc-outbox.js",
  });
  return { account, clock, requests, sandbox, storage, timers };
}

function samplePath(route) {
  return route.path.replace(
    /\{([A-Za-z][A-Za-z0-9_]*)\}/g,
    (_placeholder, name) => (name === "id" ? "card_abc" : `sample_${name}`),
  );
}

function policyOutboxCases() {
  return InteractionPolicy.policies()
    .filter((policy) => policy.transport.outbox)
    .flatMap((policy) => policy.matches.flatMap(
      (route) => route.methods.map((method) => ({
        id: policy.id,
        url: samplePath(route),
        method,
      })),
    ));
}

function records(storage, ownerNamespace, recordType) {
  const values = [];
  for (const [key, raw] of storage.map) {
    if (key === LEGACY_KEY) continue;
    let value;
    try { value = JSON.parse(raw); } catch { continue; }
    if (
      value?.contract === "command-outbox/2" &&
      value?.ownerNamespace === ownerNamespace &&
      value?.recordType === recordType
    ) {
      values.push({ key, raw, ...value });
    }
  }
  return values;
}

test("每个 mutation 使用含 mutationId 的独立 namespaced 记录，账户与旧 v1 隔离", () => {
  const storage = sharedStorage();
  const h = createWindow({ storage });
  const legacyBefore = storage.getItem(LEGACY_KEY);
  assert.equal(h.sandbox.RC.outbox.contract, "command-outbox/2");
  assert.equal(h.sandbox.RC.outbox.legacySize(), 2);
  assert.equal(h.sandbox.RC.outbox.status().legacySize, 2);

  h.account.activate({ namespace: ACCOUNT_A, source: "server-session" });
  const first = h.sandbox.RC.outbox.send(
    "vocab",
    "same-word",
    "/pdf/api/vocab-mark",
    { word: "alpha", mark: "known" },
  );
  h.clock.value += 1;
  const second = h.sandbox.RC.outbox.send(
    "vocab",
    "same-word",
    "/pdf/api/vocab-mark",
    { word: "alpha", mark: "" },
  );
  assert.notEqual(first, second);
  const accountARecords = records(storage, ACCOUNT_A, "mutation");
  assert.equal(accountARecords.length, 2);
  assert.equal(new Set(accountARecords.map((item) => item.key)).size, 2);
  for (const item of accountARecords) {
    assert.match(item.key, new RegExp(`${item.mutationId}$`));
    assert.equal(item.queueKey, "vocab:same-word");
  }

  h.account.activate({ namespace: ACCOUNT_B, source: "server-session" });
  assert.equal(h.sandbox.RC.outbox.size(), 0);
  h.sandbox.RC.outbox.send(
    "phrase",
    "account-b",
    "/pdf/api/phrase-mark",
    { text: "account b", mark: "mastered" },
  );
  assert.equal(records(storage, ACCOUNT_A, "mutation").length, 2);
  assert.equal(records(storage, ACCOUNT_B, "mutation").length, 1);
  assert.equal(storage.getItem(LEGACY_KEY), legacyBefore);
  assert.equal(h.requests.length, 0);
});

test("共享 storage 的双窗口交错写只发送同 queueKey 最新值，精确删除不伤在途新写", async () => {
  const storage = sharedStorage();
  const clock = { value: 100 };
  let resolveFirst;
  const left = createWindow({
    storage,
    clock,
    fetchImpl: () => new Promise((resolve) => { resolveFirst = resolve; }),
  });
  const right = createWindow({ storage, clock });
  left.account.activate({ namespace: ACCOUNT_A, source: "server-session" });
  right.account.activate({ namespace: ACCOUNT_A, source: "server-session" });

  const mutation1 = left.sandbox.RC.outbox.send(
    "vocab", "shared", "/pdf/api/vocab-mark",
    { word: "shared", mark: "seen" },
  );
  clock.value = 200;
  const mutation2 = right.sandbox.RC.outbox.send(
    "vocab", "shared", "/pdf/api/vocab-mark",
    { word: "shared", mark: "known" },
  );
  const pending = left.sandbox.RC.outbox.flush();
  assert.equal(left.requests.length, 1);
  const firstBatch = JSON.parse(left.requests[0].options.body);
  assert.equal(firstBatch.ops.length, 1);
  assert.equal(firstBatch.ops[0].mutationId, mutation2);
  assert.deepEqual(firstBatch.ops[0].body, { word: "shared", mark: "known" });

  clock.value = 300;
  const mutation3 = right.sandbox.RC.outbox.send(
    "vocab", "shared", "/pdf/api/vocab-mark",
    { word: "shared", mark: "" },
  );
  resolveFirst(batchResponse(ACCOUNT_A, [200]));
  assert.equal((await pending).ok, true);

  const afterFirstFlush = records(storage, ACCOUNT_A, "mutation");
  assert.deepEqual(afterFirstFlush.map((item) => item.mutationId), [mutation3]);
  assert.equal(afterFirstFlush.some((item) => item.mutationId === mutation1), false);
  assert.equal(afterFirstFlush.some((item) => item.mutationId === mutation2), false);

  await right.sandbox.RC.outbox.flush();
  const secondBatch = JSON.parse(right.requests.at(-1).options.body);
  assert.equal(secondBatch.ops[0].mutationId, mutation3);
  assert.equal(records(storage, ACCOUNT_A, "mutation").length, 0);
});

test("普通 4xx 全部保留在当前账户 dead-letter，status 只公开数量", async () => {
  const storage = sharedStorage();
  const h = createWindow({
    storage,
    fetchImpl: async (_url, options) => {
      const body = JSON.parse(options.body);
      return batchResponse(body.ownerNamespace, body.ops.map(() => 400));
    },
  });
  h.account.activate({ namespace: ACCOUNT_A, source: "server-session" });
  for (let i = 0; i < 105; i++) {
    h.clock.value += 1;
    h.sandbox.RC.outbox.send(
      "review",
      `answer-${i}`,
      "/pdf/api/review-answer",
      { aid: `answer-${i}`, card_id: i, ease: 3 },
    );
  }
  await h.sandbox.RC.outbox.flush();
  assert.equal(h.sandbox.RC.outbox.size(), 5);
  assert.equal(h.sandbox.RC.outbox.deadLetterSize(), 100);
  await h.sandbox.RC.outbox.flush();
  assert.equal(h.sandbox.RC.outbox.size(), 0);
  assert.equal(h.sandbox.RC.outbox.deadLetterSize(), 105);
  assert.equal(h.sandbox.RC.outbox.status().deadLetterSize, 105);
  assert.equal("deadLetters" in h.sandbox.RC.outbox, false);
  for (const item of records(storage, ACCOUNT_A, "dead-letter")) {
    assert.match(item.key, new RegExp(`${item.mutationId}$`));
    assert.equal(item.status, 400);
  }
});

test("最新 mutation 被 4xx 拒绝时，旧合并记录进入 dead-letter 而非丢弃或倒退重放", async () => {
  const h = createWindow({
    fetchImpl: async (_url, options) => {
      const body = JSON.parse(options.body);
      return batchResponse(body.ownerNamespace, [422]);
    },
  });
  h.account.activate({ namespace: ACCOUNT_A, source: "server-session" });
  const oldMutation = h.sandbox.RC.outbox.send(
    "vocab", "same-word", "/pdf/api/vocab-mark",
    { word: "alpha", mark: "seen" },
  );
  h.clock.value += 1;
  const rejectedLatest = h.sandbox.RC.outbox.send(
    "vocab", "same-word", "/pdf/api/vocab-mark",
    { word: "alpha", mark: "known" },
  );
  await h.sandbox.RC.outbox.flush();

  assert.equal(h.sandbox.RC.outbox.size(), 0);
  const dead = records(h.storage, ACCOUNT_A, "dead-letter");
  assert.equal(dead.length, 2);
  const rejected = dead.find((item) => item.mutationId === rejectedLatest);
  const superseded = dead.find((item) => item.mutationId === oldMutation);
  assert.equal(rejected.status, 422);
  assert.equal(rejected.reason, undefined);
  assert.equal(superseded.status, 0);
  assert.equal(superseded.reason, "superseded-by-rejected-latest");
  assert.equal(superseded.supersededByMutationId, rejectedLatest);

  await h.sandbox.RC.outbox.flush();
  assert.equal(h.requests.length, 1);
  assert.equal(h.sandbox.RC.outbox.size(), 0);
});

test("dead-letter 空间不足时保留原 pending，不以清旧记录换空间", async () => {
  const storage = sharedStorage();
  const h = createWindow({
    storage,
    fetchImpl: async (_url, options) => {
      const body = JSON.parse(options.body);
      return batchResponse(body.ownerNamespace, [422]);
    },
  });
  h.account.activate({ namespace: ACCOUNT_A, source: "server-session" });
  const mutation = h.sandbox.RC.outbox.send(
    "review",
    "storage-full",
    "/pdf/api/review-answer",
    { aid: "storage-full", card_id: 7, ease: 1 },
  );
  const originalSetItem = storage.setItem.bind(storage);
  storage.setItem = (key, value) => {
    if (String(key).includes("dead-letter")) throw new Error("quota");
    originalSetItem(key, value);
  };

  const result = await h.sandbox.RC.outbox.flush();
  assert.equal(result.ok, false);
  assert.equal(h.sandbox.RC.outbox.deadLetterSize(), 0);
  assert.equal(h.sandbox.RC.outbox.size(), 1);
  assert.equal(
    records(storage, ACCOUNT_A, "mutation")[0].mutationId,
    mutation,
  );
});

test("429、5xx 与网络失败均保留原 mutation，不进入 dead-letter", async () => {
  const statuses = [429, 500];
  let networkFailure = false;
  const h = createWindow({
    fetchImpl: async (_url, options) => {
      if (networkFailure) throw new TypeError("offline");
      const body = JSON.parse(options.body);
      return batchResponse(
        body.ownerNamespace,
        body.ops.map(() => statuses.shift() ?? 500),
      );
    },
  });
  h.account.activate({ namespace: ACCOUNT_A, source: "server-session" });
  const mutation = h.sandbox.RC.outbox.send(
    "review",
    "retryable",
    "/pdf/api/review-answer",
    { aid: "retryable", card_id: 1, ease: 2 },
  );
  await h.sandbox.RC.outbox.flush();
  assert.equal(h.sandbox.RC.outbox.size(), 1);
  assert.equal(h.sandbox.RC.outbox.deadLetterSize(), 0);
  await h.sandbox.RC.outbox.flush();
  assert.equal(h.sandbox.RC.outbox.size(), 1);
  assert.equal(h.sandbox.RC.outbox.deadLetterSize(), 0);
  networkFailure = true;
  const offline = await h.sandbox.RC.outbox.flush();
  assert.equal(offline.offline, true);
  assert.equal(records(h.storage, ACCOUNT_A, "mutation")[0].mutationId, mutation);
  assert.equal(h.sandbox.RC.outbox.deadLetterSize(), 0);
});

test("flush 响应回来前 lease generation 变化时 fail closed，不移动或删除旧 owner 记录", async () => {
  let resolveFetch;
  const h = createWindow({
    fetchImpl: () => new Promise((resolve) => { resolveFetch = resolve; }),
  });
  h.account.activate({ namespace: ACCOUNT_A, source: "server-session" });
  const mutation = h.sandbox.RC.outbox.send(
    "review",
    "answer-a",
    "/pdf/api/review-answer",
    { aid: "answer-a", card_id: 1, ease: 3 },
  );
  const pending = h.sandbox.RC.outbox.flush();
  h.account.activate({ namespace: ACCOUNT_B, source: "server-session" });
  resolveFetch(batchResponse(ACCOUNT_A, [400]));
  const result = await pending;
  assert.equal(result.ok, false);
  assert.equal(result.stale, true);
  assert.equal(records(h.storage, ACCOUNT_A, "mutation")[0].mutationId, mutation);
  assert.equal(records(h.storage, ACCOUNT_A, "dead-letter").length, 0);
  assert.equal(records(h.storage, ACCOUNT_B, "mutation").length, 0);
});

test("客户端命令端点完全由交互策略派生，并拒绝任意站内写请求", () => {
  const h = createWindow();
  h.account.activate({ namespace: ACCOUNT_A, source: "server-session" });
  const allowed = policyOutboxCases();
  assert.ok(allowed.length > 0);
  allowed.forEach(({ id, url, method }, index) => {
    assert.match(
      h.sandbox.RC.outbox.send(
        "allowed",
        `${index}`,
        `${url}?policy_case=${encodeURIComponent(id)}`,
        { index },
        method,
      ),
      /^mut-v2-[a-f0-9]{32}$/,
    );
  });
  const before = h.sandbox.RC.outbox.size();
  for (const [url, method] of [
    ["/pdf/api/translate", "POST"],
    ["/api/assistant/chat", "POST"],
    ["/pdf/api/reading-pos", "PUT"],
    ["https://evil.example/pdf/api/vocab-mark", "POST"],
    ["https://user:pass@reader.example/pdf/api/vocab-mark", "POST"],
    ["/pdf/api/vocab-mark#unexpected", "POST"],
    ["/pdf/api/entity/card.with-dot", "PATCH"],
    [`/pdf/api/entity/${"a".repeat(161)}`, "PATCH"],
    ["/pdf/api/entity/card%2Fchild", "PATCH"],
  ]) {
    assert.throws(
      () => h.sandbox.RC.outbox.send("blocked", url, url, {}, method),
      (error) => error.code === "BW_OUTBOX_ENDPOINT",
    );
  }
  assert.equal(h.sandbox.RC.outbox.size(), before);
});

test("缺失或无效的交互策略时 outbox fail closed，且不落入本地队列", () => {
  for (const interactionPolicy of [
    null,
    {
      CONTRACT: "interaction-policy/1",
      validate() {
        return {
          contract: "interaction-policy/1",
          ok: false,
          errors: ["tampered"],
        };
      },
      match() {
        return {
          sync: "outbox",
          surfaces: ["pwa"],
          transport: { outbox: true },
        };
      },
    },
  ]) {
    const h = createWindow({ interactionPolicy });
    h.account.activate({ namespace: ACCOUNT_A, source: "server-session" });
    assert.throws(
      () => h.sandbox.RC.outbox.send(
        "vocab",
        "fail-closed",
        "/pdf/api/vocab-mark",
        { word: "alpha", mark: "known" },
      ),
      (error) => error.code === "BW_OUTBOX_POLICY_UNAVAILABLE",
    );
    assert.equal(records(h.storage, ACCOUNT_A, "mutation").length, 0);
  }
});
