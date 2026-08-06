import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const SOURCE = readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-settings.js", import.meta.url),
  "utf8",
);
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

// rc-settings owns one large generated modal. This deliberately small DOM
// implements only the browser primitives that modal and this contract use.
function createDom() {
  const elements = new Map();

  function matching(source, selector) {
    const entries = Array.from(source || []);
    if (selector === "#eph2-theme button") {
      return entries.filter((entry) =>
        entry.tagName === "BUTTON" && typeof entry.dataset.th === "string"
      );
    }
    if (selector.startsWith("#")) {
      const found = elements.get(selector.slice(1));
      return found ? [found] : [];
    }
    if (selector.startsWith(".")) {
      const className = selector.slice(1);
      return entries.filter((entry) => entry.classList.contains(className));
    }
    const dataMatch = selector.match(/^\[data-([a-z0-9-]+)="([^"]*)"\]$/i);
    if (dataMatch) {
      const key = dataMatch[1].replace(/-([a-z])/g, (_, letter) =>
        letter.toUpperCase()
      );
      return entries.filter((entry) => entry.dataset[key] === dataMatch[2]);
    }
    return entries.filter((entry) => entry.tagName === selector.toUpperCase());
  }

  function makeElement(tagName = "div") {
    const listeners = new Map();
    const classes = new Set();
    const children = [];
    const element = {
      _scope: null,
      _text: "",
      _children: children,
      tagName: String(tagName).toUpperCase(),
      id: "",
      value: "",
      type: "",
      checked: false,
      disabled: false,
      hidden: false,
      dataset: {},
      style: { display: "", setProperty(name, value) { this[name] = value; } },
      classList: {
        add(...names) { names.filter(Boolean).forEach((name) => classes.add(name)); },
        remove(...names) { names.forEach((name) => classes.delete(name)); },
        contains(name) { return classes.has(name); },
        toggle(name, force) {
          if (force === true) { classes.add(name); return true; }
          if (force === false) { classes.delete(name); return false; }
          if (classes.has(name)) { classes.delete(name); return false; }
          classes.add(name); return true;
        },
      },
      set className(value) {
        classes.clear();
        String(value || "").split(/\s+/).filter(Boolean)
          .forEach((name) => classes.add(name));
      },
      get className() { return Array.from(classes).join(" "); },
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
                if (property) child.style[property] = declaration.slice(separator + 1).trim();
              });
            } else if (name === "hidden") child.hidden = true;
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
          if (nextTag >= 0) child._text = decodeText(html.slice(textStart, nextTag));
          parsed.push(child);
          children.push(child);
          if (child.id) elements.set(child.id, child);
        }
        parsed.forEach((child) => { child._scope = parsed; });
      },
      get innerHTML() { return this._text; },
      setAttribute(name, value) {
        const normalized = String(name || "").toLowerCase();
        if (normalized === "id") {
          this.id = String(value || "");
          if (this.id) elements.set(this.id, this);
        } else if (normalized === "class") this.className = value;
        else if (normalized.startsWith("data-")) {
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
      replaceChildren(...next) { children.splice(0, children.length, ...next); },
      querySelector(selector) {
        return matching(this._scope || children, selector)[0] || null;
      },
      querySelectorAll(selector) { return matching(this._scope || children, selector); },
      async emit(type, init = {}) {
        const event = {
          type, target: this, currentTarget: this,
          preventDefault() {}, stopPropagation() {}, ...init,
        };
        for (const listener of listeners.get(type) || []) {
          await listener.call(this, event);
        }
      },
    };
    return element;
  }

  const document = {
    head: makeElement("head"),
    body: makeElement("body"),
    documentElement: makeElement("html"),
    createElement: makeElement,
    getElementById(id) { return elements.get(String(id)) || null; },
    querySelector(selector) { return matching(elements.values(), selector)[0] || null; },
    querySelectorAll(selector) { return matching(elements.values(), selector); },
    addEventListener() {},
  };
  return { document, elements };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function storage(overrides = {}) {
  return {
    enabled: true,
    configured: true,
    folderName: "My Vault",
    updatedAt: 1,
    count: 0,
    pendingCount: 0,
    ...overrides,
  };
}

function harness(bridge) {
  const { document, elements } = createDom();
  const values = new Map();
  const sandbox = {
    console,
    document,
    Date,
    Promise,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    fetch: async () => ({ json: async () => ({}) }),
    localStorage: {
      getItem(key) { return values.has(key) ? values.get(key) : null; },
      setItem(key, value) { values.set(String(key), String(value)); },
      removeItem(key) { values.delete(String(key)); },
    },
    RC: { toast() {} },
    __bwNativeAppDataBridge: bridge,
  };
  sandbox.window = sandbox;
  sandbox.globalThis = sandbox;
  vm.runInContext(SOURCE, vm.createContext(sandbox), { filename: "rc-settings.js" });

  async function open() {
    sandbox.RC.settings.open({ host: "web", tab: "note" });
    await tick();
    await tick();
  }
  async function refresh() {
    await elements.get("rcset-native-notes-refresh").emit("click");
    await tick();
    await tick();
  }
  return { sandbox, elements, open, refresh };
}

test("桥缺失、未配置、线路关闭、空列表和失败都明确且保留刷新入口", async (t) => {
  await t.test("桥缺失", async () => {
    const h = harness(null);
    await h.open();
    assert.match(h.elements.get("rcset-native-notes-status").textContent, /没有 BWReader App 原生桥/);
    assert.equal(h.elements.get("rcset-native-notes-refresh").disabled, false);
  });

  for (const [name, state, expected] of [
    ["未配置", storage({ configured: false, enabled: false }), /尚未选择 Obsidian Vault/],
    ["线路关闭", storage({ enabled: false, count: 2 }), /线路已关闭.*2 条历史笔记/],
    ["空列表", storage({ count: 0 }), /目前没有 App 本机 Markdown 笔记/],
  ]) {
    await t.test(name, async () => {
      const bridge = {
        status: async () => ({ storage: state }),
        listNotes: async () => ({ storage: state, notes: [] }),
        readNote: async () => { throw new Error("unexpected read"); },
      };
      const h = harness(bridge);
      await h.open();
      assert.match(h.elements.get("rcset-native-notes-status").textContent, expected);
      await h.refresh();
      assert.match(h.elements.get("rcset-native-notes-status").textContent, expected);
      assert.equal(h.elements.get("rcset-native-notes-refresh").disabled, false);
    });
  }

  await t.test("失败可重试", async () => {
    let attempts = 0;
    const bridge = {
      status: async () => ({ storage: storage() }),
      listNotes: async () => {
        attempts += 1;
        if (attempts === 1) throw new Error("native offline");
        return { storage: storage(), notes: [] };
      },
      readNote: async () => { throw new Error("unexpected read"); },
    };
    const h = harness(bridge);
    await h.open();
    await h.refresh();
    assert.match(h.elements.get("rcset-native-notes-status").textContent, /读取列表失败.*可点.*重试/);
    assert.equal(h.elements.get("rcset-native-notes-refresh").disabled, false);
    await h.refresh();
    assert.match(h.elements.get("rcset-native-notes-status").textContent, /目前没有/);
    assert.equal(attempts, 2);
  });
});

test("列表与正文纯文本渲染，刷新代际阻止旧响应回灌", async () => {
  const first = deferred();
  let listCalls = 0;
  const malicious = "<img src=x onerror=alert(1)> **Markdown**";
  const summary = {
    id: "12345678-note",
    title: "<b>标题</b>",
    fileName: "note.md",
    preview: malicious,
    contentTruncated: false,
    sourceFile: "book.pdf",
    sourcePage: 7,
    createdAt: 1,
    pendingExport: true,
  };
  const bridge = {
    status: async () => ({ storage: storage({ count: 1, pendingCount: 1 }) }),
    listNotes: () => {
      listCalls += 1;
      if (listCalls === 1) return first.promise;
      return Promise.resolve({
        storage: storage({ count: 1, pendingCount: 1 }),
        notes: [summary],
      });
    },
    readNote: async () => ({
      note: { ...summary, content: malicious, contentTruncated: true },
    }),
  };
  const h = harness(bridge);
  await h.open();

  await h.elements.get("rcset-native-notes-refresh").emit("click");
  await tick();
  await h.elements.get("rcset-native-notes-refresh").emit("click");
  await tick();
  await tick();
  first.resolve({ storage: storage(), notes: [] });
  await tick();
  await tick();

  const list = h.elements.get("rcset-native-notes-list");
  assert.equal(list._children.length, 1, "旧的空列表响应不得覆盖第二次刷新");
  assert.equal(list._children[0].textContent.includes("<b>标题</b>"), true);
  assert.equal(list._children[0].textContent.includes(malicious), true);
  assert.equal(list._children[0].textContent.includes("等待写入"), true);

  await list._children[0].emit("click");
  await tick();
  await tick();
  const body = h.elements.get("rcset-native-notes-body");
  assert.equal(body.textContent, malicious + "\n\n（内容投影已截断）");
  assert.equal(body._children.length, 0, "正文必须只经 textContent，不产生 HTML 子节点");
  assert.equal(h.elements.get("rcset-native-notes-detail").style.display, "");
});
