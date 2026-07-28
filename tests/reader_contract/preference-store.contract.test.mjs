import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const require = createRequire(import.meta.url);
const AccountContext = require(
  "../../_server_deploy/static/reader-runtime/account-context.js",
);
const DataRegistry = require(
  "../../_server_deploy/static/reader-runtime/data-registry.js",
);
const PreferenceStore = require(
  "../../_server_deploy/static/reader-runtime/preference-store.js",
);
const DataStore = require(
  "../../_server_deploy/static/reader-runtime/data-store.js",
);
const StorageRouter = require(
  "../../_server_deploy/static/reader-runtime/storage-router.js",
);

const ACCOUNT_A = `acct-v1-${"a".repeat(64)}`;
const ACCOUNT_B = `acct-v1-${"b".repeat(64)}`;

class MemoryStorage {
  constructor(seed = {}) {
    this.map = new Map(Object.entries(seed).map(([key, value]) => [key, String(value)]));
  }
  get length() {
    return this.map.size;
  }
  getItem(key) {
    return this.map.has(String(key)) ? this.map.get(String(key)) : null;
  }
  setItem(key, value) {
    this.map.set(String(key), String(value));
  }
  removeItem(key) {
    this.map.delete(String(key));
  }
  clear() {
    this.map.clear();
  }
  key(index) {
    return [...this.map.keys()][index] ?? null;
  }
}

function contextFor(namespace) {
  const context = AccountContext.createContext();
  context.activate({ namespace, source: "server-session" });
  return context;
}

function makeDataStore(name) {
  return DataStore.createDataStore({
    backend: DataStore.createMemoryBackend(),
    deviceId: name,
    idFactory: (prefix) => `${prefix}_${name}`,
  });
}

function makeRouter(label = "prefs") {
  return StorageRouter.createStorageRouter({
    globalStore: makeDataStore(`${label}-global`),
    documentStore: makeDataStore(`${label}-document`),
    deviceStore: makeDataStore(`${label}-device`),
    scopes: DataRegistry.scopes(),
    dataRegistryApi: DataRegistry,
  });
}

function createPreferences(storage, context, options = {}) {
  return PreferenceStore.createPreferenceStore({
    accountContext: context,
    dataRegistry: options.dataRegistry || DataRegistry,
    storage,
    lease: context.lease(),
    messageBridge: false,
  });
}

function readMirror(storage, namespace) {
  const raw = storage.getItem(`${PreferenceStore.MIRROR_PREFIX}${namespace}`);
  return raw ? JSON.parse(raw) : null;
}

test("A/B 切换先保全旧 owner，再只加载当前账户镜像", async () => {
  const storage = new MemoryStorage({
    "ep-side-width": "420",
  });
  const contextA = contextFor(ACCOUNT_A);
  const preferencesA = createPreferences(storage, contextA);
  assert.equal(preferencesA.getRaw("ep-side-width"), "420");
  await preferencesA.setRaw("eph-th", "night");
  preferencesA.destroy();

  // 模拟 isolated world 或旧代码直接更新裸键；切换时必须仍归还给 A。
  storage.setItem("ep-side-width", "444");
  const contextB = contextFor(ACCOUNT_B);
  const preferencesB = createPreferences(storage, contextB);
  assert.equal(preferencesB.getRaw("ep-side-width"), null);
  assert.equal(storage.getItem("ep-side-width"), null);
  assert.equal(readMirror(storage, ACCOUNT_A).values["ep-side-width"], "444");
  assert.equal(readMirror(storage, ACCOUNT_A).values["eph-th"], "night");

  await preferencesB.setRaw("ep-side-width", "560");
  preferencesB.destroy();
  const contextA2 = contextFor(ACCOUNT_A);
  const returnedA = createPreferences(storage, contextA2);
  assert.equal(returnedA.getRaw("ep-side-width"), "444");
  assert.equal(storage.getItem("ep-side-width"), "444");
  assert.equal(readMirror(storage, ACCOUNT_B).values["ep-side-width"], "560");
});

test("无 owner 的首次裸设置只归当前账户，不会自动合并给下一账户", () => {
  const storage = new MemoryStorage({
    "bw-set-target-langs": '["en","ja"]',
    "eph-hl-colors": '["#ffee58"]',
  });
  const contextA = contextFor(ACCOUNT_A);
  const preferencesA = createPreferences(storage, contextA);
  assert.equal(storage.getItem(PreferenceStore.OWNER_KEY), ACCOUNT_A);
  assert.equal(
    readMirror(storage, ACCOUNT_A).values["bw-set-target-langs"],
    '["en","ja"]',
  );
  preferencesA.destroy();

  const contextB = contextFor(ACCOUNT_B);
  const preferencesB = createPreferences(storage, contextB);
  assert.equal(preferencesB.getRaw("bw-set-target-langs"), null);
  assert.equal(preferencesB.getRaw("eph-hl-colors"), null);
});

test("generic 与 PDF continuous/spread 外观使用不同稳定 ID", async () => {
  const storage = new MemoryStorage({
    "ep-side-width": "400",
    "pdf-gp-width-continuous": "520",
    "pdf-gp-width-spread": "640",
    "pdf-gp-floating-continuous": "1",
    "pdf-gp-floating-spread": "0",
  });
  const context = contextFor(ACCOUNT_A);
  const preferences = createPreferences(storage, context);
  const router = makeRouter("pdf-mode");
  await preferences.attach(router);

  const generic = await router.get(
    "device-preferences",
    "setting:sidebar.width",
  );
  const continuous = await router.get(
    "device-preferences",
    "setting:sidebar.pdf.continuous.width",
  );
  const spread = await router.get(
    "device-preferences",
    "setting:sidebar.pdf.spread.width",
  );
  assert.equal(generic.value.rawValue, "400");
  assert.equal(continuous.value.rawValue, "520");
  assert.equal(spread.value.rawValue, "640");
  assert.notEqual(continuous.id, spread.id);
  assert.equal(
    (await router.get(
      "device-preferences",
      "setting:sidebar.pdf.continuous.floating",
    )).value.rawValue,
    "1",
  );
  assert.equal(
    (await router.get(
      "device-preferences",
      "setting:sidebar.pdf.spread.floating",
    )).value.rawValue,
    "0",
  );
});

test("attach 按 collection 预取设置快照，避免大型 EPUB 冷启动串行逐键读取", async () => {
  const storage = new MemoryStorage({
    "ep-side-width": "472",
  });
  const context = contextFor(ACCOUNT_A);
  const preferences = createPreferences(storage, context);
  const router = makeRouter("prefetch");
  await router.put("user-settings", {
    id: "setting:translation.display-style",
    semanticKey: "translation.display-style",
    legacyKey: "rcWebTrStyle",
    codec: "string",
    rawValue: "inline",
    migration: "preference-store-v1",
  }, {
    id: "setting:translation.display-style",
    mutationId: "prefetch-authority-fixture",
  });

  const listCalls = [];
  let getCalls = 0;
  const observed = {
    get() {
      getCalls += 1;
      return router.get(...arguments);
    },
    list(collection, query) {
      listCalls.push({ collection, query: structuredClone(query) });
      return router.list(collection, query);
    },
    put: router.put,
    remove: router.remove,
    subscribe: router.subscribe,
  };
  await preferences.attach(observed);

  assert.deepEqual(
    listCalls,
    [
      {
        collection: "user-settings",
        query: { includeDeleted: true, offset: 0, limit: 1000 },
      },
      {
        collection: "device-preferences",
        query: { includeDeleted: true, offset: 0, limit: 1000 },
      },
    ],
  );
  assert.equal(getCalls, 0);
  assert.equal(storage.getItem("rcWebTrStyle"), "inline");
  assert.equal(
    (await router.get(
      "device-preferences",
      "setting:sidebar.width",
    )).value.rawValue,
    "472",
  );
});

test("没有 list 的兼容 router 仍逐键精确读取，不跳过偏好水合", async () => {
  const registry = {
    CONTRACT: "data-registry/1",
    settingMigrations: () => [{
      legacyKey: "test-setting",
      collection: "device-preferences",
      semanticKey: "test.setting",
      codec: "string",
    }],
    collection: (name) => name === "device-preferences"
      ? { scope: "device", status: "ready", provider: false }
      : null,
  };
  const storage = new MemoryStorage();
  const context = contextFor(ACCOUNT_A);
  const preferences = createPreferences(storage, context, {
    dataRegistry: registry,
  });
  let getCalls = 0;
  const router = {
    get: async () => {
      getCalls += 1;
      return null;
    },
    put: async () => null,
    remove: async () => null,
    subscribe: () => () => {},
  };
  await preferences.attach(router);
  assert.equal(getCalls, 1);
});

test("白名单保留宿主差异，并排除文档、缓存、语音与旧模型键", () => {
  const migrations = new Map(
    DataRegistry.settingMigrations().map((entry) => [entry.legacyKey, entry]),
  );
  assert.equal(
    migrations.get("eph-hl-colors").semanticKey,
    "highlight.palette.epub-web",
  );
  assert.equal(
    migrations.get("pdf-hl-colors").semanticKey,
    "highlight.palette",
  );
  assert.equal(
    migrations.get("eph-set-tab").semanticKey,
    "settings.active-tab.epub",
  );
  assert.equal(
    migrations.get("bw-set-tab").semanticKey,
    "settings.active-tab.web",
  );
  assert.equal(
    migrations.get("pdf-set-tab").semanticKey,
    "settings.active-tab.pdf",
  );
  for (const key of [
    "html-fs-pct",
    "html-lh",
    "html-th",
    "pdf-debug",
    "pdf-auto-orient",
    "pdf-charbox",
    "pdf-ruby",
    "pdf-img-mode",
    "pdf-grammar-view",
    "asst-followups-on",
  ]) {
    assert.equal(migrations.has(key), true, key);
  }
  for (const key of [
    "pdf-ai-overrides",
    "pdf-last-positions",
    "pdf-drafts",
    "pdf-bookshelf-cache",
    "review-queue-v1",
    "rc-voice-speak",
    "eph-pos:",
    "pdf-use-compressed:",
    "book-langs",
  ]) {
    assert.equal(migrations.has(key), false, key);
  }
});

test("旧 shadow 只作升级输入，首次可见值获胜并成为新权威记录", async () => {
  const storage = new MemoryStorage({
    "eph-gp-floating": "1",
  });
  const context = contextFor(ACCOUNT_A);
  const preferences = createPreferences(storage, context);
  const router = makeRouter("shadow-upgrade");
  await router.put("device-preferences", {
    id: "setting:sidebar.floating",
    semanticKey: "sidebar.floating",
    legacyKey: "eph-gp-floating",
    codec: "boolean-string",
    rawValue: "0",
    migration: "legacy-localstorage-shadow-v1",
  }, {
    id: "setting:sidebar.floating",
    mutationId: "old-shadow-fixture",
  });

  await preferences.attach(router);
  const upgraded = await router.get(
    "device-preferences",
    "setting:sidebar.floating",
  );
  assert.equal(upgraded.value.rawValue, "1");
  assert.equal(upgraded.value.migration, "preference-store-v1");
  assert.equal(storage.getItem("eph-gp-floating"), "1");
});

test("异步写入完成前账户 lease 失效时拒绝发布旧结果", async () => {
  const registry = {
    CONTRACT: "data-registry/1",
    settingMigrations: () => [{
      legacyKey: "test-setting",
      collection: "device-preferences",
      semanticKey: "test.setting",
      codec: "string",
    }],
    collection: (name) => name === "device-preferences"
      ? { scope: "device", status: "ready", provider: false }
      : null,
  };
  const storage = new MemoryStorage();
  const context = contextFor(ACCOUNT_A);
  const preferences = createPreferences(storage, context, {
    dataRegistry: registry,
  });
  let releasePut;
  const putGate = new Promise((resolve) => {
    releasePut = resolve;
  });
  const router = {
    get: async () => null,
    put: async (_collection, value) => {
      await putGate;
      return {
        collection: "device-preferences",
        id: value.id,
        rev: 1,
        deleted: false,
        value,
      };
    },
    remove: async () => null,
    subscribe: () => () => {},
  };
  await preferences.attach(router);
  const write = preferences.setRaw("test-setting", "A");
  context.activate({ namespace: ACCOUNT_B, source: "server-session" });
  releasePut();
  await assert.rejects(
    write,
    (error) => error.code === "BW_ACCOUNT_CONTEXT_STALE",
  );
  assert.throws(
    () => preferences.getRaw("test-setting"),
    (error) => error.code === "BW_ACCOUNT_CONTEXT_STALE",
  );
});

test("新账户接管 owner 后，旧页面不能读取或改写当前裸镜像", async () => {
  const storage = new MemoryStorage({ "eph-th": "paper" });
  const contextA = contextFor(ACCOUNT_A);
  const preferencesA = createPreferences(storage, contextA);

  const contextB = contextFor(ACCOUNT_B);
  const preferencesB = createPreferences(storage, contextB);
  await preferencesB.setRaw("eph-th", "night");
  assert.equal(storage.getItem("eph-th"), "night");

  assert.throws(
    () => preferencesA.setRaw("eph-th", "paper"),
    (error) => error.code === "BW_PREFERENCE_OWNER_STALE",
  );
  assert.throws(
    () => preferencesA.getRaw("eph-th"),
    (error) =>
      error.code === "BW_PREFERENCE_OWNER_STALE" ||
      error.code === "BW_ACCOUNT_CONTEXT_UNAVAILABLE" ||
      error.code === "BW_ACCOUNT_CONTEXT_STALE",
  );
  assert.equal(storage.getItem("eph-th"), "night");
  assert.equal(contextA.snapshot().reason, "legacy-owner-changed");
});

test("PATCH 先完整校验，后项无效时前项也不会部分落盘", async () => {
  const storage = new MemoryStorage({ "eph-th": "paper" });
  const context = contextFor(ACCOUNT_A);
  const preferences = createPreferences(storage, context);

  await assert.rejects(
    preferences.applyPatch([
      { legacyKey: "eph-th", value: "night" },
      { legacyKey: "not-whitelisted", value: "x" },
    ]),
    (error) => error.code === "BW_PREFERENCE_KEY",
  );
  assert.equal(storage.getItem("eph-th"), "paper");
  assert.equal(preferences.getRaw("eph-th"), "paper");

  await assert.rejects(
    preferences.applyPatch([
      { legacyKey: "eph-th", value: "night" },
      { legacyKey: "eph-th", value: "sepia" },
    ]),
    (error) => error.code === "BW_PREFERENCE_PATCH_DUPLICATE",
  );
  assert.equal(storage.getItem("eph-th"), "paper");
});

test("损坏的当前账户 mirror 先隔离保全，再以当前裸值重建", () => {
  const corrupt = "{not-json";
  const storage = new MemoryStorage({
    [PreferenceStore.OWNER_KEY]: ACCOUNT_A,
    [`${PreferenceStore.MIRROR_PREFIX}${ACCOUNT_A}`]: corrupt,
    "eph-th": "night",
  });
  const context = contextFor(ACCOUNT_A);
  const preferences = createPreferences(storage, context);
  assert.equal(preferences.getRaw("eph-th"), "night");
  const quarantineKeys = [...storage.map.keys()].filter((key) =>
    key.startsWith("bw.reader.preference-quarantine.v1:")
  );
  assert.equal(quarantineKeys.length, 1);
  assert.equal(
    JSON.parse(storage.getItem(quarantineKeys[0])).raw,
    corrupt,
  );
  assert.doesNotThrow(() =>
    JSON.parse(storage.getItem(`${PreferenceStore.MIRROR_PREFIX}${ACCOUNT_A}`))
  );
});

test("同账户多实例逐键合并 mirror，不用旧整包覆盖另一标签的新设置", async () => {
  const storage = new MemoryStorage();
  const context = contextFor(ACCOUNT_A);
  const left = createPreferences(storage, context);
  const right = createPreferences(storage, context);
  await left.setRaw("eph-th", "night");
  await right.setRaw("ep-side-width", "488");
  const mirror = readMirror(storage, ACCOUNT_A);
  assert.equal(mirror.values["eph-th"], "night");
  assert.equal(mirror.values["ep-side-width"], "488");
});

test("受管 clear 只清当前白名单设置，保留 outbox、安装编号和隔离数据", async () => {
  class BrowserStorage extends MemoryStorage {}
  const previousStorageCtor = globalThis.Storage;
  globalThis.Storage = BrowserStorage;
  const storage = new BrowserStorage({
    "eph-th": "night",
    "ep-side-width": "480",
    "rc-outbox-v1": '{"commands":[]}',
    "bw.reader.pwa.install-id.v1": `pwa-install-v1-${"1".repeat(32)}`,
    "bw.reader.preference-quarantine.v1:fixture": "keep-quarantine",
    "unrelated-document-key": "keep-document",
  });
  const context = contextFor(ACCOUNT_A);
  let preferences;
  try {
    preferences = createPreferences(storage, context);
    storage.clear();
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(storage.getItem("eph-th"), null);
    assert.equal(storage.getItem("ep-side-width"), null);
    assert.equal(storage.getItem("rc-outbox-v1"), '{"commands":[]}');
    assert.equal(
      storage.getItem("bw.reader.pwa.install-id.v1"),
      `pwa-install-v1-${"1".repeat(32)}`,
    );
    assert.equal(
      storage.getItem("bw.reader.preference-quarantine.v1:fixture"),
      "keep-quarantine",
    );
    assert.equal(storage.getItem("unrelated-document-key"), "keep-document");
    assert.equal(
      storage.getItem(PreferenceStore.OWNER_KEY),
      ACCOUNT_A,
    );
  } finally {
    preferences?.destroy();
    if (previousStorageCtor === undefined) delete globalThis.Storage;
    else globalThis.Storage = previousStorageCtor;
  }
});

test("Storage 拦截在写裸键前校验大小与 owner，失败不污染新账户镜像", async () => {
  class BrowserStorage extends MemoryStorage {}
  const previousStorageCtor = globalThis.Storage;
  globalThis.Storage = BrowserStorage;
  const storage = new BrowserStorage({ "eph-th": "paper" });
  const context = contextFor(ACCOUNT_A);
  let preferences;
  try {
    preferences = createPreferences(storage, context);
    assert.throws(
      () => storage.setItem("eph-th", "界".repeat(30000)),
      (error) => error.code === "BW_PREFERENCE_VALUE_SIZE",
    );
    assert.equal(storage.getItem("eph-th"), "paper");

    const mirrorB = {
      contract: "preference-store/1",
      namespace: ACCOUNT_B,
      mutationSeq: 0,
      updatedAt: Date.now(),
      values: { "eph-th": "night" },
      states: { "eph-th": { status: "synced", absent: false } },
    };
    storage.setItem(
      `${PreferenceStore.MIRROR_PREFIX}${ACCOUNT_B}`,
      JSON.stringify(mirrorB),
    );
    storage.setItem(PreferenceStore.OWNER_KEY, ACCOUNT_B);
    assert.throws(
      () => storage.setItem("eph-th", "sepia"),
      (error) => error.code === "BW_PREFERENCE_OWNER_STALE",
    );
    assert.equal(storage.getItem("eph-th"), "night");
  } finally {
    preferences?.destroy();
    if (previousStorageCtor === undefined) delete globalThis.Storage;
    else globalThis.Storage = previousStorageCtor;
  }
});

test("每次写入使用新的安全 mutationId，值往返也不会被 DataStore replay", async () => {
  const registry = {
    CONTRACT: "data-registry/1",
    settingMigrations: () => [{
      legacyKey: "test-setting",
      collection: "device-preferences",
      semanticKey: "test.setting",
      codec: "string",
    }],
    collection: (name) => name === "device-preferences"
      ? { scope: "device", status: "ready", provider: false }
      : null,
  };
  const storage = new MemoryStorage();
  const context = contextFor(ACCOUNT_A);
  const preferences = createPreferences(storage, context, {
    dataRegistry: registry,
  });
  const mutationIds = [];
  let rev = 0;
  const router = {
    get: async () => null,
    put: async (collection, value, options) => {
      mutationIds.push(options.mutationId);
      rev += 1;
      return {
        collection,
        id: value.id,
        rev,
        deleted: false,
        value: structuredClone(value),
      };
    },
    remove: async () => null,
    subscribe: () => () => {},
  };
  await preferences.attach(router);
  await preferences.setRaw("test-setting", "A");
  await preferences.setRaw("test-setting", "B");
  await preferences.setRaw("test-setting", "A");
  assert.equal(new Set(mutationIds).size, 3);
  assert.equal(
    mutationIds.every((id) =>
      /^preference-v1:put:test\.setting:[a-f0-9]{32}$/.test(id)
    ),
    true,
  );
});

test("非墓碑 DataStore 记录必须含匹配白名单身份与字符串 rawValue", async () => {
  const registry = {
    CONTRACT: "data-registry/1",
    settingMigrations: () => [{
      legacyKey: "test-setting",
      collection: "device-preferences",
      semanticKey: "test.setting",
      codec: "string",
    }],
    collection: (name) => name === "device-preferences"
      ? { scope: "device", status: "ready", provider: false }
      : null,
  };
  const storage = new MemoryStorage();
  const context = contextFor(ACCOUNT_A);
  const preferences = createPreferences(storage, context, {
    dataRegistry: registry,
  });
  const router = {
    get: async () => ({
      collection: "device-preferences",
      id: "setting:test.setting",
      rev: 2,
      deleted: false,
      value: {
        id: "setting:test.setting",
        semanticKey: "wrong.setting",
        legacyKey: "test-setting",
      },
    }),
    put: async () => null,
    remove: async () => null,
    subscribe: () => () => {},
  };
  await assert.rejects(
    preferences.attach(router),
    (error) => error.code === "BW_PREFERENCE_RECORD",
  );
  assert.equal(storage.getItem("test-setting"), null);
});

test("trusted PWA 消息桥只回当前镜像，并以白名单 PATCH 排队", async () => {
  const preferenceSource = readFileSync(
    new URL(
      "../../_server_deploy/static/reader-runtime/preference-store.js",
      import.meta.url,
    ),
    "utf8",
  );
  const accountSource = readFileSync(
    new URL(
      "../../_server_deploy/static/reader-runtime/account-context.js",
      import.meta.url,
    ),
    "utf8",
  );
  const registrySource = readFileSync(
    new URL(
      "../../_server_deploy/static/reader-runtime/data-registry.js",
      import.meta.url,
    ),
    "utf8",
  );
  const listeners = new Map();
  const posted = [];
  const storage = new MemoryStorage({ "eph-th": "paper" });
  let randomSeed = 1;
  const sandbox = {
    console,
    crypto: {
      getRandomValues(bytes) {
        for (let index = 0; index < bytes.length; index += 1) {
          bytes[index] = (randomSeed + index * 13) & 255;
        }
        randomSeed += 1;
        return bytes;
      },
    },
    localStorage: storage,
    location: {
      pathname: "/pdf/epub/view",
      origin: "https://reader.example",
    },
    addEventListener(type, listener) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(listener);
    },
    removeEventListener(type, listener) {
      const group = listeners.get(type) || [];
      const index = group.indexOf(listener);
      if (index >= 0) group.splice(index, 1);
    },
    postMessage(message, targetOrigin) {
      posted.push({ message: structuredClone(message), targetOrigin });
    },
    CustomEvent: class {
      constructor(type, options = {}) {
        this.type = type;
        this.detail = options.detail;
      }
    },
    document: {
      dispatchEvent() {},
    },
  };
  const context = vm.createContext(sandbox);
  const window = vm.runInContext("globalThis", context);
  vm.runInContext(accountSource, context, { filename: "account-context.js" });
  vm.runInContext(registrySource, context, { filename: "data-registry.js" });
  vm.runInContext(preferenceSource, context, { filename: "preference-store.js" });
  window.BWReaderRuntime.accountContext.activate({
    namespace: ACCOUNT_A,
    source: "server-session",
  });
  window.BWReaderRuntime.preferenceStore.createPreferenceStore({
    accountContext: window.BWReaderRuntime.accountContext,
    dataRegistry: window.BWReaderRuntime.dataRegistry,
    storage,
    lease: window.BWReaderRuntime.accountContext.lease(),
    trustedWindow: true,
  });
  const dispatchMessage = (data) => {
    for (const listener of listeners.get("message") || []) {
      listener({
        source: window,
        origin: "https://reader.example",
        data,
      });
    }
  };

  dispatchMessage({
    __bwReaderPreference: "preference-store/1",
    direction: "extension-to-page",
    type: "HELLO",
    requestId: "hello-1",
    payload: {},
  });
  assert.equal(posted.at(-1).message.type, "READY");
  assert.equal(posted.at(-1).message.payload.values["eph-th"], "paper");
  assert.equal(
    posted.at(-1).message.payload.allowedKeys.includes("eph-th"),
    true,
  );
  assert.equal("namespace" in posted.at(-1).message.payload, false);

  dispatchMessage({
    __bwReaderPreference: "preference-store/1",
    direction: "extension-to-page",
    type: "PATCH",
    requestId: "patch-1",
    payload: {
      changes: [
        { legacyKey: "eph-th", value: "night" },
        { legacyKey: "ep-side-width", value: "480" },
      ],
    },
  });
  for (let turn = 0; turn < 10 && posted.at(-1).message.type !== "RESULT"; turn += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.equal(posted.at(-1).message.type, "RESULT");
  assert.equal(posted.at(-1).message.payload.ok, true);
  assert.equal(posted.at(-1).message.payload.queued, true);
  assert.deepEqual(
    JSON.parse(JSON.stringify(posted.at(-1).message.payload.values)),
    {
      "eph-th": "night",
      "ep-side-width": "480",
    },
  );
  assert.equal(storage.getItem("eph-th"), "night");
  assert.equal(storage.getItem("ep-side-width"), "480");
});

test("三个 PWA reader 都在 pwa-runtime 前加载 PreferenceStore", () => {
  for (const relative of [
    "../../_server_deploy/templates/pdf_reader.html",
    "../../_server_deploy/templates/epub_html_reader.html",
    "../../_server_deploy/templates/html_reader.html",
  ]) {
    const source = readFileSync(new URL(relative, import.meta.url), "utf8");
    const preference = source.indexOf("/reader-runtime/preference-store.js");
    const runtime = source.indexOf("/reader-runtime/pwa-runtime.js");
    assert.notEqual(preference, -1, relative);
    assert.notEqual(runtime, -1, relative);
    assert.equal(preference < runtime, true, relative);
  }
});
