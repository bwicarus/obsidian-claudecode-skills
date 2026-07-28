import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const SOURCE = readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-settings.js", import.meta.url),
  "utf8",
);
const SYNC_IDS = Object.freeze({
  section: "rcset-sync-section",
  status: "rcset-sync-status",
  conflicts: "rcset-sync-conflicts",
});
const tick = () => new Promise((resolve) => setImmediate(resolve));

function decodeText(value) {
  return String(value || "")
    .replace(/&nbsp;/g, " ")
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&#39;/g, "'")
    .replace(/&quot;/g, "\"")
    .trim();
}

function createDom() {
  const elements = new Map();
  const documentListeners = new Map();
  let serial = 0;

  function matching(elementsToSearch, selector) {
    const source = Array.from(elementsToSearch || []);
    if (selector === "#eph2-theme button") {
      return source.filter((entry) =>
        entry.tagName === "BUTTON" && typeof entry.dataset.th === "string"
      );
    }
    if (selector.startsWith("#")) {
      const found = elements.get(selector.slice(1));
      return found ? [found] : [];
    }
    if (selector.startsWith(".")) {
      const className = selector.slice(1);
      return source.filter((entry) => entry.classList.contains(className));
    }
    const dataMatch = selector.match(/^\[data-([a-z0-9-]+)="([^"]*)"\]$/i);
    if (dataMatch) {
      const key = dataMatch[1].replace(/-([a-z])/g, (_, letter) =>
        letter.toUpperCase()
      );
      return source.filter((entry) => entry.dataset[key] === dataMatch[2]);
    }
    const tagName = selector.toUpperCase();
    return source.filter((entry) => entry.tagName === tagName);
  }

  function makeElement(tagName = "div") {
    const listeners = new Map();
    const classes = new Set();
    const children = [];
    const element = {
      _serial: ++serial,
      _scope: null,
      _text: "",
      tagName: String(tagName).toUpperCase(),
      id: "",
      value: "",
      type: "",
      checked: false,
      disabled: false,
      hidden: false,
      dataset: {},
      style: {
        display: "",
        setProperty(name, value) {
          this[name] = value;
        },
      },
      classList: {
        add(...names) {
          names.filter(Boolean).forEach((name) => classes.add(name));
        },
        remove(...names) {
          names.forEach((name) => classes.delete(name));
        },
        contains(name) {
          return classes.has(name);
        },
        toggle(name, force) {
          if (force === true) {
            classes.add(name);
            return true;
          }
          if (force === false) {
            classes.delete(name);
            return false;
          }
          if (classes.has(name)) {
            classes.delete(name);
            return false;
          }
          classes.add(name);
          return true;
        },
      },
      set className(value) {
        classes.clear();
        String(value || "").split(/\s+/).filter(Boolean)
          .forEach((name) => classes.add(name));
      },
      get className() {
        return Array.from(classes).join(" ");
      },
      set textContent(value) {
        this._text = String(value == null ? "" : value);
        children.length = 0;
      },
      get textContent() {
        return this._text + children.map((child) => child.textContent).join("");
      },
      set innerHTML(value) {
        const html = String(value == null ? "" : value);
        this._text = decodeText(html.replace(/<[^>]*>/g, " "));
        children.length = 0;
        if (!html.includes("<")) return;
        const parsed = [];
        const tagPattern = /<([a-z][a-z0-9:-]*)([^>]*)>/gi;
        let match;
        while ((match = tagPattern.exec(html))) {
          if (match[0].startsWith("</")) continue;
          const child = makeElement(match[1]);
          const attributes = match[2] || "";
          const attributePattern =
            /([a-z_:][a-z0-9_:.-]*)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+)))?/gi;
          let attribute;
          while ((attribute = attributePattern.exec(attributes))) {
            const name = attribute[1].toLowerCase();
            const raw = attribute[2] ?? attribute[3] ?? attribute[4] ?? "";
            if (name === "id") child.id = raw;
            else if (name === "class") child.className = raw;
            else if (name === "value") child.value = raw;
            else if (name === "type") child.type = raw;
            else if (name === "style") {
              raw.split(";").forEach((declaration) => {
                const separator = declaration.indexOf(":");
                if (separator < 0) return;
                const property = declaration.slice(0, separator).trim();
                const propertyValue = declaration.slice(separator + 1).trim();
                if (property) child.style[property] = propertyValue;
              });
            }
            else if (name === "hidden") child.hidden = true;
            else if (name === "disabled") child.disabled = true;
            else if (name === "checked") child.checked = true;
            else if (name.startsWith("data-")) {
              const key = name.slice(5).replace(/-([a-z])/g, (_, letter) =>
                letter.toUpperCase()
              );
              child.dataset[key] = raw;
            }
          }
          const textStart = tagPattern.lastIndex;
          const nextTag = html.indexOf("<", textStart);
          if (nextTag >= 0) {
            const immediate = decodeText(html.slice(textStart, nextTag));
            if (immediate) child._text = immediate;
          }
          child._scope = parsed;
          parsed.push(child);
          children.push(child);
          if (child.id) elements.set(child.id, child);
        }
        for (const child of parsed) child._scope = parsed;
      },
      get innerHTML() {
        return this._text;
      },
      setAttribute(name, value) {
        const normalized = String(name || "").toLowerCase();
        if (normalized === "id") {
          this.id = String(value || "");
          if (this.id) elements.set(this.id, this);
        } else if (normalized === "class") {
          this.className = value;
        } else if (normalized.startsWith("data-")) {
          const key = normalized.slice(5).replace(/-([a-z])/g, (_, letter) =>
            letter.toUpperCase()
          );
          this.dataset[key] = String(value || "");
        }
      },
      addEventListener(type, listener) {
        const bucket = listeners.get(type) || [];
        bucket.push(listener);
        listeners.set(type, bucket);
      },
      appendChild(child) {
        children.push(child);
        if (child?.id) elements.set(child.id, child);
        return child;
      },
      replaceChildren(...next) {
        children.splice(0, children.length, ...next);
      },
      remove() {
        if (this.id) elements.delete(this.id);
      },
      querySelector(selector) {
        return matching(this._scope || children, selector)[0] || null;
      },
      querySelectorAll(selector) {
        return matching(this._scope || children, selector);
      },
      async emit(type, init = {}) {
        const event = {
          type,
          target: this,
          currentTarget: this,
          preventDefault() {},
          stopPropagation() {},
          ...init,
        };
        for (const listener of listeners.get(type) || []) {
          await listener.call(this, event);
        }
      },
    };
    return element;
  }

  const head = makeElement("head");
  const body = makeElement("body");
  const documentElement = makeElement("html");
  const document = {
    head,
    body,
    documentElement,
    createElement: makeElement,
    getElementById(id) {
      return elements.get(String(id)) || null;
    },
    querySelector(selector) {
      return matching(elements.values(), selector)[0] || null;
    },
    querySelectorAll(selector) {
      return matching(elements.values(), selector);
    },
    addEventListener(type, listener) {
      const bucket = documentListeners.get(type) || [];
      bucket.push(listener);
      documentListeners.set(type, bucket);
    },
    dispatchEvent(event) {
      for (const listener of documentListeners.get(event?.type) || []) {
        listener.call(document, event);
      }
      return true;
    },
  };
  return { document, elements };
}

function validStatus(overrides = {}) {
  return {
    contract: "sync-conflict-control/1",
    owner: "pwa",
    state: "ready",
    at: 1,
    conflictCount: 0,
    truncated: false,
    conflicts: [],
    ...structuredClone(overrides),
  };
}

function harness({
  control = null,
  exposeRuntime = true,
} = {}) {
  const { document, elements } = createDom();
  const storage = new Map();
  const toasts = [];
  const fetchCalls = [];
  let timerSequence = 0;
  const timers = new Map();
  const sandbox = {
    console,
    document,
    localStorage: {
      getItem(key) {
        return storage.has(key) ? storage.get(key) : null;
      },
      setItem(key, value) {
        storage.set(String(key), String(value));
      },
      removeItem(key) {
        storage.delete(String(key));
      },
    },
    fetch: async (...args) => {
      fetchCalls.push(structuredClone(args));
      return { json: async () => ({ ok: false }) };
    },
    setTimeout(callback, delay = 0) {
      timerSequence += 1;
      timers.set(timerSequence, {
        callback,
        delay: Number(delay) || 0,
        active: true,
      });
      return timerSequence;
    },
    clearTimeout(id) {
      const timer = timers.get(id);
      if (timer) timer.active = false;
    },
    setInterval,
    clearInterval,
    CustomEvent: class CustomEvent {
      constructor(type, init = {}) {
        this.type = type;
        this.detail = init.detail;
      }
    },
    RC: {
      toast(message) {
        toasts.push(String(message));
      },
    },
  };
  if (exposeRuntime) {
    sandbox.BWReaderRuntime = {
      pwaRuntime: {
        syncControl() {
          return control;
        },
      },
    };
  }
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.runInContext(SOURCE, vm.createContext(sandbox), {
    filename: "rc-settings.js",
  });

  function element(id) {
    return elements.get(id) || null;
  }
  function visible(id) {
    const target = element(id);
    return !!target && target.hidden !== true && target.style.display !== "none";
  }
  function syncText() {
    return Array.from(elements.entries())
      .filter(([id]) => id.startsWith("rcset-sync"))
      .map(([, entry]) => entry.textContent)
      .join("\n");
  }
  async function open() {
    sandbox.RC.settings.open({ host: "web", tab: "ai" });
    await tick();
    await tick();
    await tick();
  }
  return {
    sandbox,
    elements,
    fetchCalls,
    toasts,
    timers,
    element,
    visible,
    syncText,
    open,
    activeTimers(delay) {
      return Array.from(timers.values()).filter(
        (timer) => timer.active && (delay == null || timer.delay === delay),
      );
    },
  };
}

test("没有可用的 pwaRuntime.syncControl 时不显示同步区", async () => {
  for (const options of [
    { exposeRuntime: false },
    { control: null },
    { control: { contract: "wrong/1", status() {} } },
    { control: { contract: "sync-conflict-control/1" } },
  ]) {
    const h = harness(options);
    await h.open();
    assert.equal(
      h.visible(SYNC_IDS.section),
      false,
      "不完整或错误合同不得显示一个看似可用的同步入口",
    );
  }
});

test("纯 PWA blocked 状态只渲染白名单摘要，并保持只读安全暂停", async () => {
  const conflictSetId = `conflict-set-v1-${"b".repeat(32)}`;
  const calls = { status: 0 };
  const control = {
    contract: "sync-conflict-control/1",
    async status() {
      calls.status += 1;
      return validStatus({
        owner: "pwa",
        state: "blocked",
        at: 20,
        conflictSetId,
        conflictCount: 1,
        conflicts: [{
          lane: "server",
          collection: "user-settings",
          id: "theme",
          reason: "revision-conflict",
          incomingRev: 4,
          currentRev: 3,
          rawRecord: "raw-record-must-not-render",
          upstreamText: "upstream-text-must-not-render",
        }],
        namespace: `acct-v1-${"c".repeat(64)}`,
        token: "pwa-sync-token-must-not-render",
        rawRecord: "top-level-record-must-not-render",
      });
    },
  };
  const h = harness({ control });
  await h.open();

  assert.equal(h.visible(SYNC_IDS.section), true);
  const rendered = h.syncText();
  for (const expected of [
    "server",
    "user-settings",
    "theme",
    "revision-conflict",
    "4",
    "3",
  ]) {
    assert.equal(rendered.includes(expected), true, `缺少白名单字段 ${expected}`);
  }
  for (const privateValue of [
    conflictSetId,
    "raw-record-must-not-render",
    "upstream-text-must-not-render",
    `acct-v1-${"c".repeat(64)}`,
    "pwa-sync-token-must-not-render",
    "top-level-record-must-not-render",
  ]) {
    assert.equal(rendered.includes(privateValue), false, privateValue);
  }
  assert.match(
    rendered,
    /同步已安全暂停，完整裁决器尚未启用，不自动选择本地\/服务器版本。/,
  );
  assert.equal(h.element("rcset-sync-retry"), null);
  assert.ok(calls.status >= 1);
  assert.equal(SOURCE.includes("retryAfterResolution"), false);
  assert.equal(SOURCE.includes("conflictSetId"), false);
});

test("extension-background owner 同样只显示白名单摘要与安全暂停说明", async () => {
  const control = {
    contract: "sync-conflict-control/1",
    async status() {
      return validStatus({
        owner: "extension-background",
        state: "blocked",
        conflictSetId: `conflict-set-v1-${"d".repeat(32)}`,
        conflictCount: 1,
        conflicts: [{
          lane: "server",
          collection: "user-settings",
          id: "theme",
          reason: "revision-conflict",
          incomingRev: 2,
          currentRev: 1,
        }],
      });
    },
  };
  const h = harness({ control });
  await h.open();

  assert.equal(h.visible(SYNC_IDS.section), true);
  assert.match(
    h.syncText(),
    /同步已安全暂停，完整裁决器尚未启用，不自动选择本地\/服务器版本。/,
  );
  assert.equal(h.syncText().includes("扩展弹窗"), false);
  assert.equal(h.element("rcset-sync-retry"), null);
});

test("PWA error 状态只显示安全错误码与处理提示，不泄露原始错误", async () => {
  const rawMessage = "upstream token verification detail must not render";
  const nestedMessage = "private server stack must not render";
  const control = {
    contract: "sync-conflict-control/1",
    async status() {
      return validStatus({
        owner: "pwa",
        state: "error",
        at: 66,
        errorCode: "BW_SYNC_AUTH",
        retryable: false,
        error: rawMessage,
        lastError: {
          code: "BW_SYNC_AUTH",
          message: nestedMessage,
        },
      });
    },
  };
  const h = harness({ control });
  await h.open();

  const rendered = h.syncText();
  assert.match(rendered, /同步失败/);
  assert.match(rendered, /错误码：BW_SYNC_AUTH/);
  assert.match(rendered, /需要检查账户或配置/);
  assert.equal(rendered.includes(rawMessage), false);
  assert.equal(rendered.includes(nestedMessage), false);
});

test("并发状态读取只允许最新响应渲染，且只保留一个轮询 timer", async () => {
  const pending = [];
  const control = {
    contract: "sync-conflict-control/1",
    status() {
      return new Promise((resolve) => {
        pending.push(resolve);
      });
    },
  };
  const h = harness({ control });
  await h.open();
  assert.equal(pending.length, 1);

  h.sandbox.document.dispatchEvent(
    new h.sandbox.CustomEvent("bw:reader-sync-status"),
  );
  await tick();
  assert.equal(pending.length, 2);

  pending[1](validStatus({
    state: "blocked",
    at: 2,
    conflictCount: 1,
    conflicts: [{
      lane: "server",
      collection: "user-settings",
      id: "latest-status",
      reason: "revision-conflict",
      incomingRev: 8,
      currentRev: 7,
    }],
  }));
  await tick();
  await tick();
  assert.match(h.syncText(), /latest-status/);
  assert.equal(h.activeTimers(5000).length, 1);

  pending[0](validStatus({
    state: "blocked",
    at: 1,
    conflictCount: 1,
    conflicts: [{
      lane: "server",
      collection: "user-settings",
      id: "stale-status",
      reason: "revision-conflict",
      incomingRev: 5,
      currentRev: 4,
    }],
  }));
  await tick();
  await tick();
  assert.match(h.syncText(), /latest-status/);
  assert.equal(h.syncText().includes("stale-status"), false);
  assert.equal(
    h.activeTimers(5000).length,
    1,
    "旧请求完成后不能创建孤儿轮询 timer",
  );
});
