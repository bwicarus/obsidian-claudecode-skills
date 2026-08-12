import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SOURCE = readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-phrasepop.js", import.meta.url),
  "utf8",
);

function normalized(value) {
  return String(value ?? "").trim().replace(/\s+/g, " ").toLowerCase();
}

function vocabularyState(initial) {
  const records = new Map();
  const listeners = [];
  const writes = [];

  function recordId(property, spec) {
    return `${property}:${spec.kind || "phrase"}:${spec.language || "und"}:${normalized(spec.text || spec.key)}`;
  }
  function put(property, spec, enabled, source = "test") {
    const record = {
      id: recordId(property, spec),
      property,
      kind: "phrase",
      language: spec.language || "und",
      key: normalized(spec.text || spec.key),
      enabled: enabled === true,
    };
    records.set(record.id, record);
    writes.push({ property, enabled: record.enabled, source });
    const event = { contract: "vocabulary-state/1", source, record };
    for (const listener of listeners) listener(event);
    return {
      applied: true,
      record,
      // 模拟本地持久层永不回执：UI 仍必须同步完成。
      durable: new Promise(() => {}),
    };
  }
  for (const item of initial) put(item.property, item, item.enabled, "hydrate");
  writes.length = 0;

  return {
    CONTRACT: "vocabulary-state/1",
    writes,
    snapshot() { return [...records.values()].map((item) => ({ ...item })); },
    ready() { return Promise.resolve({ attached: true }); },
    subscribe(listener) {
      listeners.push(listener);
      return () => {
        const index = listeners.indexOf(listener);
        if (index >= 0) listeners.splice(index, 1);
      };
    },
    lookup(spec, property) {
      const direct = records.get(recordId(property, spec));
      if (direct) return { ...direct };
      if ((spec.language || "und") !== "und") {
        const legacy = records.get(recordId(property, { ...spec, language: "und" }));
        if (legacy) return { ...legacy };
      }
      return null;
    },
    isMastered(spec) {
      return this.lookup(spec, "mastered")?.enabled === true;
    },
    isPhraseFavorite(spec) {
      return this.lookup(spec, "favorite")?.enabled === true;
    },
    setMastered(spec, enabled, options = {}) {
      return put("mastered", spec, enabled, options.source);
    },
    setPhraseFavorite(spec, enabled, options = {}) {
      return put("favorite", spec, enabled, options.source);
    },
  };
}

function button() {
  const classes = new Set();
  return {
    disabled: false,
    textContent: "",
    title: "",
    classList: {
      toggle(name, enabled) {
        if (enabled) classes.add(name);
        else classes.delete(name);
      },
      contains(name) { return classes.has(name); },
    },
  };
}

function createHarness() {
  const requests = [];
  const callbacks = { favorite: [], mastered: [] };
  const outbox = [];
  const listeners = new Map();
  const elements = new Map();

  function element(tag = "div") {
    const node = {
      tagName: tag.toUpperCase(),
      style: {},
      parentElement: null,
      offsetWidth: 320,
      offsetHeight: 160,
      innerHTML: "",
      textContent: "",
      classList: button().classList,
      setAttribute() {},
      contains() { return false; },
      appendChild(child) {
        child.parentElement = node;
        if (child.id) elements.set(child.id, child);
        return child;
      },
    };
    return node;
  }
  const body = element("body");
  const head = element("head");
  const pop = element("div");
  pop.id = "word-pop";
  body.appendChild(pop);

  const fetchImpl = (url, options = {}) => {
    let resolve;
    let reject;
    const promise = new Promise((yes, no) => {
      resolve = (payload) => yes({ json: async () => payload });
      reject = no;
    });
    requests.push({
      url,
      method: options.method || "GET",
      options,
      promise,
      resolve,
      reject,
      settled: false,
    });
    return promise;
  };
  const state = vocabularyState([
    {
      property: "favorite",
      kind: "phrase",
      language: "en",
      text: "In Spite Of",
      enabled: true,
    },
    {
      property: "mastered",
      kind: "phrase",
      language: "en",
      text: "In Spite Of",
      enabled: true,
    },
  ]);
  const sandbox = {
    console,
    Promise,
    Map,
    Set,
    Date,
    JSON,
    Math,
    encodeURIComponent,
    setTimeout,
    clearTimeout,
    innerWidth: 1200,
    innerHeight: 800,
    fetch: fetchImpl,
    BWReaderRuntime: { vocabularyState: state },
    RC: {
      toast() {},
      outbox: {
        send(...args) { outbox.push(args); },
      },
    },
    document: {
      body,
      head,
      createElement: element,
      getElementById(id) { return elements.get(id) || null; },
      addEventListener(name, listener) {
        if (!listeners.has(name)) listeners.set(name, []);
        listeners.get(name).push(listener);
      },
    },
  };
  sandbox.window = sandbox;
  vm.runInContext(SOURCE, vm.createContext(sandbox), {
    filename: "rc-phrasepop.js",
  });

  function show() {
    sandbox.RC.phrasepop.show({
      text: "In Spite Of",
      langs: ["en"],
      result: { zh: "尽管" },
      onFav(_text, value) { callbacks.favorite.push(value); },
      onMastered(_text, value) { callbacks.mastered.push(value); },
    });
  }
  function pending(method, url) {
    return requests.find(
      (item) => !item.settled && item.method === method && item.url === url,
    );
  }
  function settle(request, payload) {
    request.settled = true;
    request.resolve(payload);
  }
  return {
    sandbox,
    state,
    pop,
    requests,
    callbacks,
    outbox,
    show,
    pending,
    settle,
  };
}

async function flush() {
  await new Promise((resolve) => setImmediate(resolve));
}

test("词组收藏与掌握同步更新共享状态，迟到服务器快照不得回滚", async () => {
  const harness = createHarness();
  const { sandbox, state, pop, callbacks } = harness;

  assert.deepEqual(Array.from(sandbox.RC.phrasepop.favList()), ["in spite of"]);
  harness.show();
  assert.match(pop.innerHTML, /★ 已收藏/);
  assert.match(pop.innerHTML, /✓ 已掌握 100/);

  const favoriteButton = button();
  sandbox._epPhraseFav(favoriteButton);
  assert.equal(
    state.isPhraseFavorite({ kind: "phrase", language: "en", text: "In Spite Of" }),
    false,
  );
  assert.deepEqual(Array.from(sandbox.RC.phrasepop.favList()), []);
  assert.equal(favoriteButton.textContent, "☆ 收藏为词组");
  assert.deepEqual(callbacks.favorite, [false]);
  assert.equal(
    state.isMastered({ kind: "phrase", language: "en", text: "In Spite Of" }),
    true,
    "收藏状态不能改动掌握状态",
  );

  const masterButton = button();
  sandbox._epPhraseMaster(masterButton);
  assert.equal(
    state.isMastered({ kind: "phrase", language: "en", text: "In Spite Of" }),
    false,
  );
  assert.equal(masterButton.textContent, "☆ 标记掌握");
  assert.deepEqual(callbacks.mastered, [false]);

  // 第一次取消收藏的服务器请求尚未返回时再次收藏；第二个本地动作仍立即生效。
  sandbox._epPhraseFav(favoriteButton);
  assert.equal(
    state.isPhraseFavorite({ kind: "phrase", language: "en", text: "In Spite Of" }),
    true,
  );
  assert.equal(favoriteButton.textContent, "★ 已收藏");

  await flush();
  const removeRequest = harness.pending("DELETE", "/pdf/api/phrases");
  assert.ok(removeRequest);
  harness.settle(removeRequest, { ok: true, phrases: [] });
  await flush();
  assert.equal(
    state.isPhraseFavorite({ kind: "phrase", language: "en", text: "In Spite Of" }),
    true,
    "迟到的旧 DELETE 回执不得覆盖第二次本地收藏",
  );

  const addRequest = harness.pending("POST", "/pdf/api/phrases");
  assert.ok(addRequest, "同一词组的兼容请求应串行发送");
  harness.settle(addRequest, { ok: true, phrases: [] });
  const masterRequest = harness.pending("POST", "/pdf/api/phrase-mark");
  assert.ok(masterRequest);
  harness.settle(masterRequest, { ok: true, mastered: ["in spite of"] });

  // 启动时的旧服务器快照也只能补齐缺失记录，不能覆盖已存在的本地 false/true。
  const favoriteRead = harness.pending("GET", "/pdf/api/phrases");
  const masteredRead = harness.pending("GET", "/pdf/api/phrase-mark");
  assert.ok(favoriteRead);
  assert.ok(masteredRead);
  harness.settle(favoriteRead, { ok: true, phrases: [] });
  harness.settle(masteredRead, { ok: true, mastered: ["in spite of"] });
  await flush();

  assert.equal(
    state.isPhraseFavorite({ kind: "phrase", language: "en", text: "In Spite Of" }),
    true,
  );
  assert.equal(
    state.isMastered({ kind: "phrase", language: "en", text: "In Spite Of" }),
    false,
  );
  harness.show();
  assert.match(pop.innerHTML, /★ 已收藏/);
  assert.match(pop.innerHTML, /☆ 标记掌握/);
});

test("兼容服务器断网只进入 outbox，不回滚本地词组状态", async () => {
  const harness = createHarness();
  const { sandbox, state } = harness;
  harness.show();

  const masterButton = button();
  sandbox._epPhraseMaster(masterButton);
  assert.equal(
    state.isMastered({ kind: "phrase", language: "en", text: "In Spite Of" }),
    false,
  );
  await flush();
  const request = harness.pending("POST", "/pdf/api/phrase-mark");
  assert.ok(request);
  request.settled = true;
  request.reject(new TypeError("offline"));
  await flush();

  assert.equal(
    state.isMastered({ kind: "phrase", language: "en", text: "In Spite Of" }),
    false,
  );
  assert.equal(masterButton.textContent, "☆ 标记掌握");
  assert.equal(harness.outbox.length, 1);
  assert.equal(harness.outbox[0][0], "phrase");
});

test("日语词组把当前句境交给词典，词典失败时不采用无句境机器翻译", async () => {
  const harness = createHarness();
  const results = [];
  harness.sandbox.__bwSelectionController = {
    current() {
      return {
        text: "取り寄せ",
        context: "メインをお取り寄せの牛肉にしたりするわよね。",
      };
    },
  };

  harness.sandbox.RC.phrasepop.show({
    text: "取り寄せ",
    langs: ["ja"],
    noDisplay: true,
    onResult(value) { results.push(value); },
  });

  const dictionary = harness.requests.find((item) =>
    item.method === "GET" && item.url.startsWith("/pdf/api/dict-jp?"));
  assert.ok(dictionary);
  assert.equal(
    new URL(dictionary.url, "https://reader.invalid").searchParams.get("context"),
    "メインをお取り寄せの牛肉にしたりするわよね。",
  );

  harness.settle(dictionary, { ok: false, error: "dictionary unavailable" });
  await flush();
  assert.equal(
    harness.requests.some((item) => item.url === "/pdf/api/translate-sentence"),
    false,
  );
  assert.equal(results[0].zh, "");
});

test("日语结构化词典命中时不调用句子翻译", async () => {
  const harness = createHarness();
  const results = [];
  harness.sandbox.RC.phrasepop.show({
    text: "取り寄せ",
    context: "商品を取り寄せる。",
    langs: ["ja"],
    noDisplay: true,
    onResult(value) { results.push(value); },
  });

  const dictionary = harness.requests.find((item) =>
    item.method === "GET" && item.url.startsWith("/pdf/api/dict-jp?"));
  harness.settle(dictionary, {
    ok: true,
    zh: "订购；调货",
    reading: "とりよせ",
    accent: 0,
  });
  await flush();

  assert.equal(
    harness.requests.some((item) => item.url === "/pdf/api/translate-sentence"),
    false,
  );
  assert.equal(results[0].text, "取り寄せ");
  assert.equal(results[0].zh, "订购；调货");
  assert.equal(results[0].reading, "とりよせ");
  assert.equal(results[0].accent, 0);
});
