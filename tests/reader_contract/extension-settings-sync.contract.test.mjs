import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SOURCE = readFileSync(
  new URL("../../extensions/bw-reader-webext/src/settings-sync.js", import.meta.url),
  "utf8",
);
const ORIGIN = "https://bwicarus.taile44d0c.ts.net";
const CONTRACT = "preference-store/1";
const KEY = "bwReaderExtensionPreferencesV2";
const tick = () => new Promise((resolve) => setImmediate(resolve));

function harness({
  local = {},
  extension = null,
  pwa = false,
  withExtensionStore = true,
} = {}) {
  const windowListeners = [];
  const messages = [];
  const timers = [];
  const storeWrites = [];
  let timerSequence = 0;
  let stored = extension == null ? null : structuredClone(extension);

  class TestStorage {
    constructor(values = {}) {
      this.values = new Map(
        Object.entries(values).map(([key, value]) => [key, String(value)]),
      );
    }
    getItem(key) {
      key = String(key);
      return this.values.has(key) ? this.values.get(key) : null;
    }
    setItem(key, value) {
      this.values.set(String(key), String(value));
    }
    removeItem(key) {
      this.values.delete(String(key));
    }
  }

  const localStorage = new TestStorage(local);
  const document = { documentElement: { dataset: {} } };
  const location = { origin: ORIGIN };
  const window = {
    document,
    location,
    __bwPwaBridge: pwa ? { protocol: "bw-reader-pwa/1" } : undefined,
    addEventListener(type, listener) {
      if (type === "message") windowListeners.push(listener);
    },
    postMessage(message, targetOrigin) {
      messages.push({ message: structuredClone(message), targetOrigin });
    },
  };
  if (withExtensionStore) {
    window.__bwExtensionStore = {
      async get(key) {
        assert.equal(key, KEY);
        return stored == null ? null : structuredClone(stored);
      },
      async set(key, value) {
        assert.equal(key, KEY);
        stored = structuredClone(value);
        storeWrites.push(structuredClone(value));
        return true;
      },
    };
  }
  window.window = window;

  const context = {
    window,
    document,
    location,
    localStorage,
    Storage: TestStorage,
    TextEncoder,
    Promise,
    Map,
    Set,
    Object,
    String,
    Date,
    console,
    setTimeout(callback, delay) {
      const timer = {
        id: ++timerSequence,
        callback,
        delay,
        cancelled: false,
        fired: false,
      };
      timers.push(timer);
      return timer.id;
    },
    clearTimeout(id) {
      const timer = timers.find((item) => item.id === id);
      if (timer) timer.cancelled = true;
    },
  };
  vm.runInContext(SOURCE, vm.createContext(context), {
    filename: "settings-sync.js",
  });

  return {
    window,
    document,
    localStorage,
    messages,
    timers,
    storeWrites,
    stored: () => stored == null ? null : structuredClone(stored),
    emit(message) {
      for (const listener of windowListeners) {
        listener({ source: window, origin: ORIGIN, data: message });
      }
    },
    async runTimer(delay) {
      const timer = timers.find((item) =>
        !item.cancelled && !item.fired && (delay == null || item.delay === delay)
      );
      if (!timer) return null;
      timer.fired = true;
      timer.promise = Promise.resolve(timer.callback());
      await tick();
      await tick();
      return timer;
    },
  };
}

test("普通网页直接使用扩展本地权威设置，不依赖 PWA HELLO", async () => {
  assert.equal(SOURCE.includes("chrome.storage"), false);
  assert.equal(SOURCE.includes("ephSettingsV1"), false);
  const h = harness({
    local: {
      "eph-gp-floating": "page-old",
      "unmanaged-key": "page-only",
    },
    extension: {
      schema: 2,
      values: {
        "eph-gp-floating": "extension-authority",
        "bw-set-target-langs": '["ja"]',
      },
      updatedAt: 10,
    },
  });
  await tick();
  await tick();

  assert.equal(h.localStorage.getItem("eph-gp-floating"), "extension-authority");
  assert.equal(h.localStorage.getItem("bw-set-target-langs"), '["ja"]');
  assert.equal(h.localStorage.getItem("unmanaged-key"), "page-only");
  assert.equal(h.messages.length, 0);
  assert.equal(h.document.documentElement.dataset.bwReaderPreferenceSync, "ready");
});

test("受管设置写回扩展唯一记录，普通网页的非白名单 localStorage 不进入共享设置", async () => {
  const h = harness({
    extension: { schema: 2, values: {}, updatedAt: 0 },
  });
  await tick();
  h.localStorage.setItem("eph-gp-floating", "0");
  h.localStorage.setItem("unmanaged-key", "must-stay-page-local");
  await h.runTimer(120);

  assert.equal(h.storeWrites.length, 1);
  assert.equal(h.stored().schema, 2);
  assert.equal(h.stored().values["eph-gp-floating"], "0");
  assert.equal(
    Object.hasOwn(h.stored().values, "unmanaged-key"),
    false,
  );
  assert.equal(h.localStorage.getItem("unmanaged-key"), "must-stay-page-local");
  assert.equal(h.messages.length, 0);
});

test("书籍 PWA 只补扩展缺失值；冲突时扩展值获胜并镜像回 PWA 白名单", async () => {
  const h = harness({
    pwa: true,
    extension: {
      schema: 2,
      values: { "eph-gp-floating": "extension-wins" },
      updatedAt: 20,
    },
  });
  await tick();
  const hello = h.messages.find(({ message }) => message.type === "HELLO")?.message;
  assert.ok(hello);
  assert.equal(hello.__bwReaderPreference, CONTRACT);
  assert.equal(hello.direction, "extension-to-page");
  assert.equal(Object.hasOwn(hello, "namespace"), false);

  h.emit({
    __bwReaderPreference: CONTRACT,
    direction: "page-to-extension",
    type: "READY",
    requestId: hello.requestId,
    payload: {
      values: {
        "eph-gp-floating": "pwa-loses",
        "bw-set-target-langs": '["en"]',
      },
      allowedKeys: ["eph-gp-floating", "bw-set-target-langs"],
    },
  });
  await tick();
  await tick();

  const patch = h.messages.find(({ message }) => message.type === "PATCH")?.message;
  assert.ok(patch);
  assert.deepEqual(
    Array.from(patch.payload.changes, (item) => ({ ...item })),
    [
      { legacyKey: "eph-gp-floating", value: "extension-wins" },
      { legacyKey: "bw-set-target-langs", value: '["en"]' },
    ],
  );
  h.emit({
    __bwReaderPreference: CONTRACT,
    direction: "page-to-extension",
    type: "RESULT",
    requestId: patch.requestId,
    payload: {
      values: {
        "eph-gp-floating": "extension-wins",
        "bw-set-target-langs": '["en"]',
      },
    },
  });
  await tick();

  assert.equal(h.stored().values["eph-gp-floating"], "extension-wins");
  assert.equal(h.stored().values["bw-set-target-langs"], '["en"]');
  assert.equal(h.localStorage.getItem("eph-gp-floating"), "extension-wins");
  assert.equal(h.localStorage.getItem("bw-set-target-langs"), '["en"]');
  assert.equal(h.document.documentElement.dataset.bwReaderPreferenceSync, "ready");
});

test("PWA 镜像超时不回滚已落盘的扩展设置", async () => {
  const h = harness({
    pwa: true,
    extension: { schema: 2, values: {}, updatedAt: 0 },
  });
  await tick();
  const hello = h.messages.find(({ message }) => message.type === "HELLO")?.message;
  h.emit({
    __bwReaderPreference: CONTRACT,
    direction: "page-to-extension",
    type: "READY",
    requestId: hello.requestId,
    payload: {
      values: {},
      allowedKeys: ["eph-gp-floating"],
    },
  });
  await tick();
  await tick();

  h.localStorage.setItem("eph-gp-floating", "1");
  await h.runTimer(120);
  const patch = h.messages.filter(({ message }) => message.type === "PATCH").at(-1)?.message;
  assert.ok(patch);
  assert.equal(h.stored().values["eph-gp-floating"], "1");
  await h.runTimer(4000);
  assert.equal(h.stored().values["eph-gp-floating"], "1");
  assert.equal(
    h.document.documentElement.dataset.bwReaderPreferenceSync,
    "ready-local",
  );
});

test("缺少扩展存储门面时 fail closed，不篡改页面 Storage 原型", () => {
  const h = harness({
    local: { "eph-gp-floating": "page-value" },
    withExtensionStore: false,
  });
  h.localStorage.setItem("eph-gp-floating", "still-page");
  assert.equal(h.localStorage.getItem("eph-gp-floating"), "still-page");
  assert.equal(h.messages.length, 0);
  assert.deepEqual(h.document.documentElement.dataset, {});
});
