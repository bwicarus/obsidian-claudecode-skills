import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const ORIGIN = "https://bwicarus.taile44d0c.ts.net";
const NS_A = `acct-v1-${"a".repeat(64)}`;
const NS_B = `acct-v1-${"b".repeat(64)}`;
const EPOCH_A = `auth-v1-${"1".repeat(64)}`;
const EPOCH_B = `auth-v1-${"2".repeat(64)}`;
const AUTH_STATE_CACHE = "reader-auth-state-v1";
const AUTH_EPOCH_KEY = "/_bw/reader-auth/epoch";
const AUTH_PENDING_PREFIX = "/_bw/reader-auth/pending/";
const CACHE_A = `pdf-private-v4-${NS_A}-${EPOCH_A}`;
const CACHE_B = `pdf-private-v4-${NS_B}-${EPOCH_A}`;
const IDENTITY_CACHE = "pdf-private-identity-v1";
const IDENTITY_HEADER = "X-BW-Reader-Cache-Namespace";
const IDENTITY_ENDPOINT = "/pdf/api/cache-identity";
const REBIND = "BW_PDF_CACHE_REBIND";
const CLEAR = "BW_PDF_CACHE_CLEAR_PRIVATE";

const PYTHON_SOURCE = readFileSync(
  new URL("../../_server_deploy/pdf_reader.py", import.meta.url),
  "utf8",
);
const SW_MATCH = /_SW_JS = r"""([\s\S]*?)"""\n/.exec(PYTHON_SOURCE);
assert.ok(SW_MATCH, "必须能从 pdf_reader.py 提取可执行的 reader Service Worker");
const AUTH_SOURCE_FILE = readFileSync(
  new URL("../../_server_deploy/reader_sw_auth.py", import.meta.url),
  "utf8",
);
const AUTH_SOURCE_MATCH = /READER_SW_AUTH_JS = r"""([\s\S]*?)"""/.exec(
  AUTH_SOURCE_FILE,
);
assert.ok(AUTH_SOURCE_MATCH, "必须能提取两个 Service Worker 共用的 auth fence");
const AUTH_SOURCE = AUTH_SOURCE_MATCH[1];
const SW_SOURCE = SW_MATCH[1].replace("__BW_READER_SW_AUTH__", AUTH_SOURCE);
assert.doesNotMatch(SW_SOURCE, /__BW_READER_SW_AUTH__/);

const APP_SOURCE = readFileSync(
  new URL("../../_server_deploy/app.py", import.meta.url),
  "utf8",
);
const SITE_SW_MATCH = /_SITE_SW_TEMPLATE = """([\s\S]*?)"""\n/.exec(APP_SOURCE);
assert.ok(SITE_SW_MATCH, "必须能从 app.py 提取可执行的根 PWA Service Worker");
// Python 的普通三引号会把源码里的 `\\` 还原成 `\` 后再返回响应。
const SITE_SW_SOURCE = SITE_SW_MATCH[1]
  .replace("__BW_READER_SW_AUTH__", AUTH_SOURCE)
  .replace("%s", "contract")
  .replace(/\\\\/g, "\\");

const PRIVATE_PATHS = [
  "/pdf/",
  "/pdf/view",
  "/pdf/epub/view",
  "/pdf/html/view",
  "/pdf/fav/open",
];

function absolute(input) {
  if (typeof input === "string") return new URL(input, ORIGIN).href;
  return String(input?.url || "");
}

function response(body, namespace = "", contentType = "application/json") {
  const headers = { "Content-Type": contentType };
  if (namespace) headers[IDENTITY_HEADER] = namespace;
  return new Response(body, { status: 200, headers });
}

test("退役网页入口不再属于 reader 私有壳或可信身份来源", () => {
  for (const setName of ["PRIVATE_HTML_PATHS", "TRUSTED_IDENTITY_PATHS"]) {
    const match = new RegExp(
      `const ${setName} = new Set\\(\\[([\\s\\S]*?)\\]\\);`,
    ).exec(SW_SOURCE);
    assert.ok(match, `必须声明 ${setName}`);
    assert.doesNotMatch(match[1], /\/pdf\/web\/live/);
  }
});

function basicResponse(body, namespace = "", contentType = "application/json") {
  const result = response(body, namespace, contentType);
  Object.defineProperty(result, "type", { value: "basic" });
  return result;
}

function identityPayload(namespace, authEpoch = EPOCH_A, verifiedAt = Date.now()) {
  return JSON.stringify({
    schema: 1,
    namespace,
    authEpoch,
    verifiedAt,
  });
}

function seedAuthEpoch(cacheEntries, epoch = EPOCH_A) {
  if (!cacheEntries.has(AUTH_STATE_CACHE)) {
    cacheEntries.set(AUTH_STATE_CACHE, new Map());
  }
  const entries = cacheEntries.get(AUTH_STATE_CACHE);
  if (!entries.has(absolute(AUTH_EPOCH_KEY))) {
    entries.set(absolute(AUTH_EPOCH_KEY), new Response(epoch, {
      headers: { "Content-Type": "text/plain" },
    }));
  }
}

function authPendingKeys(cacheEntries) {
  const entries = cacheEntries.get(AUTH_STATE_CACHE) || new Map();
  return [...entries.keys()].filter((url) => (
    url.startsWith(absolute(AUTH_PENDING_PREFIX))
  ));
}

async function storedAuthEpoch(cacheEntries) {
  const stored = cacheEntries.get(AUTH_STATE_CACHE)?.get(
    absolute(AUTH_EPOCH_KEY),
  );
  return stored ? stored.clone().text() : "";
}

let cryptoCallCounter = 0;
function deterministicCrypto() {
  return {
    getRandomValues(bytes) {
      cryptoCallCounter += 1;
      for (let index = 0; index < bytes.length; index += 1) {
        bytes[index] = (cryptoCallCounter + index + 37) & 0xff;
      }
      return bytes;
    },
  };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

class FakeCache {
  constructor(state, name) {
    this.state = state;
    this.name = name;
    if (!state.cacheEntries.has(name)) state.cacheEntries.set(name, new Map());
    this.entries = state.cacheEntries.get(name);
  }

  seed(url, body = "cached", headers = {}) {
    this.entries.set(absolute(url), new Response(body, {
      status: 200,
      headers: {
        "Content-Type": headers["Content-Type"] || "text/html",
        ...headers,
      },
    }));
  }

  async match(request) {
    this.state.matches.push({ cache: this.name, url: absolute(request) });
    if (this.state.matchHook) {
      await this.state.matchHook({
        cache: this.name,
        url: absolute(request),
      });
    }
    const found = this.entries.get(absolute(request));
    return found ? found.clone() : undefined;
  }

  async put(request, storedResponse) {
    this.state.puts.push({ cache: this.name, url: absolute(request) });
    if (this.state.putHook) {
      await this.state.putHook({
        cache: this.name,
        url: absolute(request),
      });
    }
    this.entries.set(absolute(request), storedResponse.clone());
  }

  async keys() {
    this.state.keyScans.push(this.name);
    return [...this.entries.keys()].map((url) => new Request(url));
  }

  async delete(request, options = {}) {
    this.state.entryDeletes.push({
      cache: this.name,
      url: absolute(request),
      ignoreSearch: options.ignoreSearch === true,
    });
    if (options.ignoreSearch === true) {
      const target = new URL(absolute(request));
      let deleted = false;
      for (const url of [...this.entries.keys()]) {
        const candidate = new URL(url);
        if (
          candidate.origin === target.origin
          && candidate.pathname === target.pathname
        ) {
          this.entries.delete(url);
          deleted = true;
        }
      }
      return deleted;
    }
    return this.entries.delete(absolute(request));
  }

  has(url) {
    return this.entries.has(absolute(url));
  }

  async text(url) {
    const found = this.entries.get(absolute(url));
    return found ? found.clone().text() : null;
  }
}

function cacheStorage(state) {
  return {
    async keys() {
      return [...state.cacheEntries.keys()];
    },
    async open(name) {
      if (state.openHook) await state.openHook(name);
      return new FakeCache(state, name);
    },
    async delete(name) {
      state.deletedCaches.push(name);
      const deleted = state.cacheEntries.delete(name);
      if (state.cacheDeleteHook) await state.cacheDeleteHook(name);
      return deleted;
    },
  };
}

function harness(sharedCacheEntries = new Map()) {
  seedAuthEpoch(sharedCacheEntries);
  const listeners = new Map();
  const state = {
    cacheEntries: sharedCacheEntries,
    clients: new Map(),
    deletedCaches: [],
    fetchCalls: [],
    puts: [],
    matches: [],
    keyScans: [],
    entryDeletes: [],
    clientMessages: [],
    putHook: null,
    matchHook: null,
    openHook: null,
    cacheDeleteHook: null,
    claimed: false,
    fetchImpl: async () => {
      throw new TypeError("offline");
    },
  };
  const caches = cacheStorage(state);
  const crypto = deterministicCrypto();
  const self = {
    location: new URL(`${ORIGIN}/pdf/sw.js`),
    crypto,
    addEventListener(type, listener) {
      listeners.set(type, listener);
    },
    skipWaiting() {},
    clients: {
      async claim() {
        state.claimed = true;
      },
      async get(id) {
        return state.clients.get(String(id)) || null;
      },
      async matchAll() {
        return [...state.clients.values()];
      },
    },
  };
  const sandbox = {
    self,
    caches,
    URL,
    Request,
    Response,
    Headers,
    Set,
    Map,
    Promise,
    Date,
    Array,
    console,
    setTimeout,
    clearTimeout,
    crypto,
    fetch(request, options) {
      state.fetchCalls.push({
        url: absolute(request),
        request,
        options: options || {},
      });
      return state.fetchImpl(request, options || {});
    },
  };
  vm.runInContext(SW_SOURCE, vm.createContext(sandbox), {
    filename: "pdf-reader-sw.js",
  });

  function addClient(id, path) {
    const client = {
      id: String(id),
      url: `${ORIGIN}${path}`,
      postMessage(data) {
        state.clientMessages.push({ clientId: String(id), data });
      },
    };
    state.clients.set(String(id), client);
    return client;
  }

  function dispatchFetch(path, options = {}) {
    let responsePromise;
    const background = [];
    const event = {
      request: {
        url: `${ORIGIN}${path}`,
        method: options.method || "GET",
        mode: options.mode || "cors",
        headers: new Headers(options.headers || {}),
      },
      clientId: options.clientId || "",
      resultingClientId: options.resultingClientId || "",
      respondWith(value) {
        responsePromise = Promise.resolve(value);
      },
      waitUntil(value) {
        background.push(Promise.resolve(value));
      },
    };
    listeners.get("fetch")(event);
    return {
      response: responsePromise,
      done: Promise.all(background),
    };
  }

  async function dispatchMessage(clientId, data) {
    const replies = [];
    const background = [];
    const listener = listeners.get("message");
    assert.ok(listener, "reader SW 必须处理 identity/clear 消息");
    listener({
      data,
      source: state.clients.get(String(clientId)) || null,
      ports: [{ postMessage(value) { replies.push(value); } }],
      waitUntil(value) {
        background.push(Promise.resolve(value));
      },
    });
    await Promise.all(background);
    return replies;
  }

  return {
    state,
    addClient,
    async cache(name) {
      return caches.open(name);
    },
    hasCache(name) {
      return state.cacheEntries.has(name);
    },
    async activate() {
      let work = Promise.resolve();
      listeners.get("activate")({
        waitUntil(value) {
          work = Promise.resolve(value);
        },
      });
      await work;
    },
    async message(clientId, data) {
      return dispatchMessage(clientId, data);
    },
    navigate(path, options = {}) {
      const dispatched = dispatchFetch(path, {
        ...options,
        mode: "navigate",
      });
      assert.ok(dispatched.response, `导航 ${path} 必须由 reader SW 明确接管`);
      return dispatched.response;
    },
    request(path, options = {}) {
      const dispatched = dispatchFetch(path, options);
      assert.ok(dispatched.response, `私有请求 ${path} 必须由 reader SW 明确接管`);
      return dispatched.response;
    },
    startAuth(path, options = {}) {
      const dispatched = dispatchFetch(path, options);
      assert.ok(dispatched.response, "认证请求必须等待共享围栏");
      return dispatched;
    },
    async auth(path, options = {}) {
      const dispatched = this.startAuth(path, options);
      await dispatched.response;
      await dispatched.done;
    },
  };
}

function rootHarness(sharedCacheEntries = new Map()) {
  seedAuthEpoch(sharedCacheEntries);
  const listeners = new Map();
  const state = {
    cacheEntries: sharedCacheEntries,
    deletedCaches: [],
    puts: [],
    matches: [],
    keyScans: [],
    entryDeletes: [],
    fetchCalls: [],
    putHook: null,
    matchHook: null,
    openHook: null,
    cacheDeleteHook: null,
    fetchImpl: async () => basicResponse("network"),
  };
  const caches = cacheStorage(state);
  const crypto = deterministicCrypto();
  const self = {
    location: new URL(`${ORIGIN}/sw.js`),
    crypto,
    addEventListener(type, listener) {
      listeners.set(type, listener);
    },
    skipWaiting() {},
    clients: {
      async claim() {},
      async matchAll() { return []; },
    },
  };
  vm.runInContext(SITE_SW_SOURCE, vm.createContext({
    self,
    caches,
    location: new URL(`${ORIGIN}/sw.js`),
    URL,
    Request,
    Response,
    Headers,
    Promise,
    Date,
    Array,
    console,
    crypto,
    fetch(request, options) {
      state.fetchCalls.push({
        url: absolute(request),
        request,
        options: options || {},
      });
      return state.fetchImpl(request, options || {});
    },
  }), { filename: "root-pwa-sw.js" });

  function dispatchFetch(path, options = {}) {
    let responsePromise;
    const background = [];
    listeners.get("fetch")({
      request: {
        url: `${ORIGIN}${path}`,
        method: options.method || "GET",
        headers: new Headers(options.headers || {}),
        mode: options.mode || "cors",
      },
      waitUntil(value) {
        background.push(Promise.resolve(value));
      },
      respondWith(value) {
        responsePromise = Promise.resolve(value);
      },
    });
    return {
      response: responsePromise,
      done: Promise.all(background),
    };
  }

  return {
    state,
    async cache(name) {
      return caches.open(name);
    },
    async auth(path, method = "GET", headers = {}) {
      const dispatched = this.startAuth(path, {
        method,
        headers,
        mode: "navigate",
      });
      await dispatched.response;
      await dispatched.done;
    },
    startAuth(path, options = {}) {
      const dispatched = dispatchFetch(path, options);
      assert.ok(dispatched.response, "根 SW 必须等待认证围栏后再请求");
      return dispatched;
    },
    request(path, options = {}) {
      const dispatched = dispatchFetch(path, options);
      assert.ok(dispatched.response, `根 SW 必须接管 ${path}`);
      return dispatched;
    },
  };
}

test("activate 删除未分区 v3，清历史私有 HTML，并保留 v4 同账户离线数据与静态壳", async () => {
  const h = harness();
  const shell = await h.cache("pdf-shell-v1");
  const other = await h.cache("reader-manual-cache");
  const legacy = await h.cache("pdf-cache-v3");
  const account = await h.cache(CACHE_A);
  for (const path of PRIVATE_PATHS) {
    shell.seed(`${path}?file=old-user.pdf`, "window.__USER__=old");
    other.seed(`${path}?account=old`, "pvt-v1-old");
    account.seed(`${path}?namespace=old`, "old private html");
  }
  shell.seed("/static/pdf/reader.js?v=asset", "static");
  legacy.seed("/pdf/api/page-image?file=book.pdf&page=1", "unpartitioned");
  account.seed("/pdf/api/page-image?file=book.pdf&page=1", "account-a-page");

  await h.activate();

  assert.equal(h.hasCache("pdf-cache-v3"), false, "v3 必须整库删除");
  assert.equal(h.hasCache(CACHE_A), true, "v4 账户离线书不得因 SW 更新丢失");
  for (const path of PRIVATE_PATHS) {
    assert.equal(shell.has(`${path}?file=old-user.pdf`), false, path);
    assert.equal(other.has(`${path}?account=old`), false, path);
    assert.equal(account.has(`${path}?namespace=old`), false, path);
  }
  assert.equal(shell.has("/static/pdf/reader.js?v=asset"), true);
  assert.equal(account.has("/pdf/api/page-image?file=book.pdf&page=1"), true);
  assert.equal(h.state.claimed, true);
});

test("正式导航 network-only；日常只清 shell，正式响应头绑定 resulting client", async () => {
  const h = harness();
  const shell = await h.cache("pdf-shell-v1");
  const account = await h.cache(CACHE_A);
  h.addClient("client-a", "/pdf/view?file=book.pdf");
  for (const path of PRIVATE_PATHS) {
    shell.seed(`${path}?ticket=old`, "window.__USER__=old");
  }
  account.seed("/pdf/api/page-image?file=book.pdf&page=1", "account page");

  h.state.keyScans.length = 0;
  await assert.rejects(
    h.navigate("/pdf/view?file=book.pdf", { resultingClientId: "client-a" }),
    /offline/,
  );
  assert.deepEqual(
    h.state.keyScans.filter((name) => name !== AUTH_STATE_CACHE),
    [],
    "日常导航只允许检查 auth fence，不得枚举页数据 cache",
  );
  assert.equal(account.has("/pdf/api/page-image?file=book.pdf&page=1"), true);

  h.state.fetchImpl = async () => response(
    "<html>account-a</html>",
    NS_A,
    "text/html",
  );
  const online = await h.navigate("/pdf/view?file=book.pdf", {
    resultingClientId: "client-a",
  });
  assert.equal(await online.text(), "<html>account-a</html>");
  assert.equal(h.state.fetchCalls.at(-1).options.cache, "no-store");
  assert.ok(
    h.state.puts.every((put) => put.cache === IDENTITY_CACHE),
    "HTML 本体永远不得写 CacheStorage；导航只允许持久化非 HTML client 身份",
  );

  h.state.fetchImpl = async () => response("page-a", NS_A);
  const page = await h.request("/pdf/api/page-image?file=same.pdf&page=1", {
    clientId: "client-a",
  });
  assert.equal(await page.text(), "page-a", "正式导航头应已绑定 resulting client");
  assert.equal(h.state.puts.at(-1).cache, CACHE_A);
  const identity = await h.cache(IDENTITY_CACHE);
  const storedIdentity = JSON.parse(
    await identity.text("/_bw/pdf-cache-client/client-a"),
  );
  assert.equal(storedIdentity.schema, 1);
  assert.equal(storedIdentity.namespace, NS_A);
  assert.equal(storedIdentity.authEpoch, EPOCH_A);
  assert.ok(storedIdentity.verifiedAt > 0);
});

test("A/B 对同一 URL 在线与离线都严格分区，不读取对方页图", async () => {
  const h = harness();
  h.addClient("client-a", "/pdf/view?file=same.pdf");
  h.addClient("client-b", "/pdf/view?file=same.pdf");

  h.state.fetchImpl = async () => response("<html>A</html>", NS_A, "text/html");
  await h.navigate("/pdf/view?file=same.pdf", { resultingClientId: "client-a" });
  h.state.fetchImpl = async () => response("<html>B</html>", NS_B, "text/html");
  await h.navigate("/pdf/view?file=same.pdf", { resultingClientId: "client-b" });

  const sameUrl = "/pdf/api/page-image?file=same.pdf&page=1";
  h.state.fetchImpl = async () => response("A-online", NS_A, "image/webp");
  assert.equal(
    await (await h.request(sameUrl, { clientId: "client-a" })).text(),
    "A-online",
  );
  h.state.fetchImpl = async () => response("B-online", NS_B, "image/webp");
  assert.equal(
    await (await h.request(sameUrl, { clientId: "client-b" })).text(),
    "B-online",
  );

  const cacheA = await h.cache(CACHE_A);
  const cacheB = await h.cache(CACHE_B);
  assert.equal(cacheA.has(sameUrl), true);
  assert.equal(cacheB.has(sameUrl), true);

  h.state.fetchImpl = async () => { throw new TypeError("offline"); };
  assert.equal(
    await (await h.request(sameUrl, { clientId: "client-a" })).text(),
    "A-online",
  );
  assert.equal(
    await (await h.request(sameUrl, { clientId: "client-b" })).text(),
    "B-online",
  );
});

test("未登记或代理 client 始终 network-only，API 响应头与页面自报 namespace 都不能建绑定", async () => {
  const h = harness();
  h.addClient("unbound", "/pdf/view?file=book.pdf");
  h.addClient("proxy", "/pdf/web/proxy?url=https://example.com");
  h.addClient("rbi", "/pdf/web/rbi?url=https://example.com");
  const privateUrl = "/pdf/api/page-image?file=same.pdf&page=1";

  h.state.fetchImpl = async (request) => {
    const path = new URL(absolute(request)).pathname;
    if (path === IDENTITY_ENDPOINT) return response('{"ok":true}');
    return response("ordinary-api-with-header", NS_A, "image/webp");
  };
  const unboundResponse = await h.request(privateUrl, { clientId: "unbound" });
  assert.equal(await unboundResponse.text(), "ordinary-api-with-header");
  assert.equal(h.hasCache(CACHE_A), false, "普通 API 响应头不得建立首次绑定");
  assert.equal(h.state.puts.length, 0, "未绑定 client 不得落私有 cache");

  const forged = await h.message("unbound", {
    type: REBIND,
    namespace: NS_B,
  });
  assert.equal(forged.length, 1);
  assert.equal(forged[0]?.ok, false, "页面自报 namespace 必须被忽略");
  assert.equal(h.hasCache(CACHE_B), false);

  const callsBeforeProxy = h.state.fetchCalls.length;
  const proxyReply = await h.message("proxy", { type: REBIND, namespace: NS_A });
  const rbiReply = await h.message("rbi", { type: REBIND, namespace: NS_A });
  assert.equal(proxyReply[0]?.ok, false);
  assert.equal(rbiReply[0]?.ok, false);
  assert.equal(
    h.state.fetchCalls.length,
    callsBeforeProxy,
    "proxy/RBI 连 identity endpoint 都不能触发",
  );
});

test("已绑定 client 导航成 proxy/RBI 后立即失去账户 cache 访问权", async () => {
  const h = harness();
  const client = h.addClient("moving-client", "/pdf/view?file=same.pdf");
  h.state.fetchImpl = async () => response("<html>A</html>", NS_A, "text/html");
  await h.navigate("/pdf/view?file=same.pdf", {
    resultingClientId: "moving-client",
  });
  const privateUrl = "/pdf/api/page-image?file=same.pdf&page=1";
  h.state.fetchImpl = async () => response("A-page", NS_A, "image/webp");
  await h.request(privateUrl, { clientId: "moving-client" });

  client.url = `${ORIGIN}/pdf/web/rbi?url=https://example.com`;
  h.state.fetchImpl = async () => { throw new TypeError("offline"); };
  await assert.rejects(
    h.request(privateUrl, { clientId: "moving-client" }),
    /offline/,
  );
  const identity = await h.cache(IDENTITY_CACHE);
  assert.equal(
    identity.has("/_bw/pdf-cache-client/moving-client"),
    false,
    "路径失去信任时也必须删除持久 clientId 映射",
  );
});

test("SW 进程重启且全部网络断开时，用同一 active clientId 恢复本账号离线 cache", async () => {
  const first = harness();
  first.addClient("stable-client", "/pdf/view?file=book.pdf");
  first.state.fetchImpl = async () => response("<html>A</html>", NS_A, "text/html");
  await first.navigate("/pdf/view?file=book.pdf", {
    resultingClientId: "stable-client",
  });
  const privateUrl = "/pdf/api/page-chars?file=book.pdf&page=1";
  first.state.fetchImpl = async () => response("A-chars", NS_A);
  await first.request(privateUrl, { clientId: "stable-client" });

  const restarted = harness(first.state.cacheEntries);
  restarted.addClient("stable-client", "/pdf/view?file=book.pdf");
  restarted.state.fetchImpl = async () => { throw new TypeError("offline"); };
  assert.equal(
    await (await restarted.request(privateUrl, {
      clientId: "stable-client",
    })).text(),
    "A-chars",
  );
  assert.equal(
    restarted.state.fetchCalls.length,
    0,
    "身份端点也完全离线时不得发网络；应先恢复持久 clientId 映射",
  );
  assert.equal(restarted.hasCache(CACHE_B), false);
});

test("浏览器冷启动产生新 Client.id 时不继承旧身份，离线按失败关闭而非猜账户", async () => {
  const first = harness();
  first.addClient("old-client", "/pdf/view?file=book.pdf");
  first.state.fetchImpl = async () => response(
    "<html>A</html>",
    NS_A,
    "text/html",
  );
  await first.navigate("/pdf/view?file=book.pdf", {
    resultingClientId: "old-client",
  });
  const privateUrl = "/pdf/api/page-image?file=book.pdf&page=11";
  (await first.cache(CACHE_A)).seed(privateUrl, "old-offline-page");

  const cold = harness(first.state.cacheEntries);
  cold.addClient("new-client", "/pdf/view?file=book.pdf");
  cold.state.fetchImpl = async () => { throw new TypeError("offline"); };
  await assert.rejects(
    cold.request(privateUrl, { clientId: "new-client" }),
    /offline/,
  );
  assert.equal(
    cold.state.matches.some((item) => item.cache === CACHE_A),
    false,
    "没有 server-verified 新 Client.id 时连旧账户 cache 都不得打开",
  );
});

test("导航身份持久化与 logout 交错时，旧 put 完成后不能复活映射", async () => {
  const h = harness();
  h.addClient("racing-client", "/pdf/view?file=book.pdf");
  let releasePut;
  let putStartedResolve;
  let identityDeletedResolve;
  const putStarted = new Promise((resolve) => { putStartedResolve = resolve; });
  const identityDeleted = new Promise((resolve) => {
    identityDeletedResolve = resolve;
  });
  const release = new Promise((resolve) => { releasePut = resolve; });
  h.state.putHook = async ({ cache }) => {
    if (cache !== IDENTITY_CACHE) return;
    putStartedResolve();
    await release;
  };
  h.state.cacheDeleteHook = async (name) => {
    if (name === IDENTITY_CACHE) identityDeletedResolve();
  };
  h.state.fetchImpl = async () => response("<html>A</html>", NS_A, "text/html");
  const navigation = h.navigate("/pdf/view?file=book.pdf", {
    resultingClientId: "racing-client",
  });
  await putStarted;

  // 清理先完成，随后放行旧身份 put；generation 后置检查必须再删除旧写。
  const logout = h.auth("/logout", { clientId: "racing-client" });
  await identityDeleted;
  releasePut();
  await logout;
  await navigation;
  assert.equal(h.hasCache(IDENTITY_CACHE), false);

  h.state.putHook = null;
  h.state.fetchImpl = async () => { throw new TypeError("offline"); };
  await assert.rejects(
    h.request("/pdf/api/page-image?file=book.pdf&page=1", {
      clientId: "racing-client",
    }),
    /offline/,
  );
});

test("离线身份 restore/match 与 logout 交错时，旧映射和旧页都不能被晚到读取复活", async () => {
  const h = harness();
  h.addClient("restore-race", "/pdf/view?file=book.pdf");
  const identity = await h.cache(IDENTITY_CACHE);
  identity.seed(
    "/_bw/pdf-cache-client/restore-race",
    identityPayload(NS_A),
    { "Content-Type": "application/json" },
  );
  const privateUrl = "/pdf/api/page-image?file=book.pdf&page=9";
  (await h.cache(CACHE_A)).seed(privateUrl, "old-account-page");

  const matchStarted = deferred();
  const releaseMatch = deferred();
  h.state.matchHook = async ({ cache, url }) => {
    if (
      cache === IDENTITY_CACHE
      && url.endsWith("/_bw/pdf-cache-client/restore-race")
    ) {
      matchStarted.resolve();
      await releaseMatch.promise;
    }
  };
  h.state.fetchImpl = async (request) => {
    if (new URL(absolute(request)).pathname === "/logout") {
      return response("logged-out");
    }
    throw new TypeError("offline");
  };

  const oldRead = h.request(privateUrl, { clientId: "restore-race" });
  await matchStarted.promise;
  await h.auth("/logout", { clientId: "restore-race" });
  releaseMatch.resolve();

  await assert.rejects(oldRead, /offline/);
  assert.equal(h.hasCache(CACHE_A), false);
  assert.equal(h.hasCache(IDENTITY_CACHE), false);
});

test("私有数据 put 与身份切换交错时，晚到写会被撤销且旧响应不返回新会话", async () => {
  const h = harness();
  h.addClient("data-race", "/pdf/view?file=book.pdf");
  h.state.fetchImpl = async () => response(
    "<html>A</html>",
    NS_A,
    "text/html",
  );
  await h.navigate("/pdf/view?file=book.pdf", {
    resultingClientId: "data-race",
  });

  const putStarted = deferred();
  const releasePut = deferred();
  h.state.putHook = async ({ cache }) => {
    if (cache === CACHE_A) {
      putStarted.resolve();
      await releasePut.promise;
    }
  };
  h.state.fetchImpl = async (request) => {
    const path = new URL(absolute(request)).pathname;
    if (path === "/logout") return response("logged-out");
    return response("old-data-response", NS_A);
  };
  const oldRequest = h.request(
    "/pdf/api/page-image?file=book.pdf&page=7",
    { clientId: "data-race" },
  );
  await putStarted.promise;
  await h.auth("/logout", { clientId: "data-race" });
  releasePut.resolve();

  const result = await oldRequest;
  assert.equal(result.status, 0, "代际失效后旧网络响应必须 fail closed");
  assert.equal(h.hasCache(CACHE_A), false);
});

test("私有 cache.match 已开始后发生 logout，命中的旧页也不能越过最终 fence", async () => {
  const h = harness();
  h.addClient("match-race", "/pdf/view?file=book.pdf");
  h.state.fetchImpl = async () => response(
    "<html>A</html>",
    NS_A,
    "text/html",
  );
  await h.navigate("/pdf/view?file=book.pdf", {
    resultingClientId: "match-race",
  });
  const privateUrl = "/pdf/api/page-image?file=book.pdf&page=8";
  (await h.cache(CACHE_A)).seed(privateUrl, "cached-before-logout");
  const matchStarted = deferred();
  const releaseMatch = deferred();
  h.state.matchHook = async ({ cache, url }) => {
    if (cache === CACHE_A && url === absolute(privateUrl)) {
      matchStarted.resolve();
      await releaseMatch.promise;
    }
  };
  h.state.fetchImpl = async (request) => {
    if (new URL(absolute(request)).pathname === "/logout") {
      return response("logged-out");
    }
    throw new TypeError("offline");
  };

  const oldRead = h.request(privateUrl, { clientId: "match-race" });
  await matchStarted.promise;
  await h.auth("/logout");
  releaseMatch.resolve();

  assert.equal((await oldRead).status, 0);
  assert.equal(h.hasCache(CACHE_A), false);
});

test("identity endpoint 返回前 client 转入 proxy/RBI，不得建立持久身份或私有缓存", async () => {
  const h = harness();
  const client = h.addClient("path-race", "/pdf/view?file=book.pdf");
  const identityStarted = deferred();
  const releaseIdentity = deferred();
  h.state.fetchImpl = async (request) => {
    const path = new URL(absolute(request)).pathname;
    if (path === IDENTITY_ENDPOINT) {
      identityStarted.resolve();
      await releaseIdentity.promise;
      return response('{"ok":true}', NS_A);
    }
    return response("network-only", NS_A);
  };

  const request = h.request(
    "/pdf/api/page-image?file=book.pdf&page=3",
    { clientId: "path-race" },
  );
  await identityStarted.promise;
  client.url = `${ORIGIN}/pdf/web/proxy?url=https://example.com`;
  releaseIdentity.resolve();

  assert.equal(await (await request).text(), "network-only");
  assert.equal(h.hasCache(CACHE_A), false);
  const identity = await h.cache(IDENTITY_CACHE);
  assert.equal(identity.has("/_bw/pdf-cache-client/path-race"), false);
});

test("identity 记录只保留 active client，且即使全部 active 也硬性封顶 128 条", async () => {
  const h = harness();
  const identity = await h.cache(IDENTITY_CACHE);
  for (let index = 0; index < 140; index += 1) {
    const id = `active-${index}`;
    h.addClient(id, "/pdf/view?file=book.pdf");
    identity.seed(
      `/_bw/pdf-cache-client/${id}`,
      identityPayload(NS_A, EPOCH_A, index + 1),
      { "Content-Type": "application/json" },
    );
  }
  identity.seed(
    "/_bw/pdf-cache-client/dead-client",
    identityPayload(NS_A, EPOCH_A, 9999),
    { "Content-Type": "application/json" },
  );
  h.addClient("preserved-client", "/pdf/view?file=book.pdf");
  h.state.fetchImpl = async () => response(
    "<html>A</html>",
    NS_A,
    "text/html",
  );

  await h.navigate("/pdf/view?file=book.pdf", {
    resultingClientId: "preserved-client",
  });

  const keys = await identity.keys();
  assert.ok(keys.length <= 128, `identity 条目不得超过 128，实际 ${keys.length}`);
  assert.equal(identity.has("/_bw/pdf-cache-client/dead-client"), false);
  assert.equal(identity.has("/_bw/pdf-cache-client/preserved-client"), true);
});

test("PDF scope 的 current/all 清理与登录登出保留静态 shell", async () => {
  const h = harness();
  h.addClient("client-a", "/pdf/view?file=book.pdf");
  const shell = await h.cache("pdf-shell-v1");
  const a = await h.cache(CACHE_A);
  const b = await h.cache(CACHE_B);
  shell.seed("/static/pdf/reader.js?v=asset", "static");
  a.seed("/pdf/api/page-image?file=same.pdf&page=1", "A");
  b.seed("/pdf/api/page-image?file=same.pdf&page=1", "B");
  h.state.fetchImpl = async () => response('{"ok":true}', NS_A);
  await h.message("client-a", { type: REBIND });

  const currentReply = await h.message(
    "client-a",
    { type: CLEAR, scope: "current" },
  );
  assert.equal(currentReply.length, 1);
  assert.equal(currentReply[0]?.ok, true);
  assert.equal(h.hasCache(CACHE_A), false);
  assert.equal(h.hasCache(CACHE_B), true);
  assert.equal(shell.has("/static/pdf/reader.js?v=asset"), true);

  (await h.cache(CACHE_A)).seed("/pdf/api/page-image?file=x&page=1", "A2");
  await h.auth("/logout", { clientId: "client-a" });
  assert.equal(h.hasCache(CACHE_A), false);
  assert.equal(h.hasCache(CACHE_B), false);
  assert.equal(h.hasCache(IDENTITY_CACHE), false);
  assert.equal(shell.has("/static/pdf/reader.js?v=asset"), true);

  (await h.cache(CACHE_A)).seed("/pdf/api/page-image?file=x&page=1", "A3");
  await h.auth("/login", { clientId: "client-a", method: "POST" });
  assert.equal(h.hasCache(CACHE_A), false, "POST 登录切账户也必须先清私有 cache");
  assert.equal(shell.has("/static/pdf/reader.js?v=asset"), true);
});

test("根 PWA scope 的登录登出同样清全部 PDF 私有 cache，静态 shell 保留", async () => {
  const h = rootHarness();
  const shell = await h.cache("pdf-shell-v1");
  shell.seed("/static/pdf/reader.js?v=asset", "static");
  (await h.cache(CACHE_A)).seed("/pdf/api/page-image?file=x&page=1", "A");
  (await h.cache(CACHE_B)).seed("/pdf/api/page-image?file=x&page=1", "B");
  (await h.cache(IDENTITY_CACHE)).seed(
    "/_bw/pdf-cache-client/root-client",
    NS_A,
  );
  await h.auth("/logout");
  assert.equal(h.state.cacheEntries.has(CACHE_A), false);
  assert.equal(h.state.cacheEntries.has(CACHE_B), false);
  assert.equal(h.state.cacheEntries.has(IDENTITY_CACHE), false);
  assert.equal(shell.has("/static/pdf/reader.js?v=asset"), true);

  (await h.cache(CACHE_A)).seed("/pdf/api/page-image?file=x&page=1", "A2");
  (await h.cache(IDENTITY_CACHE)).seed(
    "/_bw/pdf-cache-client/root-client",
    NS_A,
  );
  await h.auth("/login", "POST");
  assert.equal(h.state.cacheEntries.has(CACHE_A), false);
  assert.equal(h.state.cacheEntries.has(IDENTITY_CACHE), false);
  assert.equal(shell.has("/static/pdf/reader.js?v=asset"), true);
});

test("POST register 与 Bearer 会话在 PDF/根两个 scope 都会旋转 epoch 并清私有缓存", async () => {
  const pdf = harness();
  const firstEpoch = await storedAuthEpoch(pdf.state.cacheEntries);
  (await pdf.cache(CACHE_A)).seed("/pdf/api/page-image?file=x&page=1", "A");
  pdf.state.fetchImpl = async () => response("registered");
  await pdf.auth("/register", { method: "POST" });
  const afterRegister = await storedAuthEpoch(pdf.state.cacheEntries);
  assert.notEqual(afterRegister, firstEpoch);
  assert.equal(pdf.hasCache(CACHE_A), false);

  const registeredCache = `pdf-private-v4-${NS_A}-${afterRegister}`;
  (await pdf.cache(registeredCache)).seed(
    "/pdf/api/page-image?file=x&page=2",
    "A2",
  );
  await pdf.auth("/api/browser-session", {
    headers: { Authorization: "Bearer session-token" },
  });
  const afterBearer = await storedAuthEpoch(pdf.state.cacheEntries);
  assert.notEqual(afterBearer, afterRegister);
  assert.equal(pdf.hasCache(registeredCache), false);

  const root = rootHarness();
  const rootFirstEpoch = await storedAuthEpoch(root.state.cacheEntries);
  (await root.cache(`bw-data-contract-${rootFirstEpoch}`)).seed(
    "/api/private.json",
    "old",
  );
  root.state.fetchImpl = async () => basicResponse("registered");
  await root.auth("/register", "POST");
  const rootAfterRegister = await storedAuthEpoch(root.state.cacheEntries);
  assert.notEqual(rootAfterRegister, rootFirstEpoch);
  assert.equal(
    root.state.cacheEntries.has(`bw-data-contract-${rootFirstEpoch}`),
    false,
  );

  (await root.cache(`bw-nav-contract-${rootAfterRegister}`)).seed(
    "/dashboard/",
    "old-nav",
  );
  await root.auth(
    "/api/browser-session",
    "GET",
    { Authorization: "Bearer session-token" },
  );
  assert.notEqual(
    await storedAuthEpoch(root.state.cacheEntries),
    rootAfterRegister,
  );
  assert.equal(
    root.state.cacheEntries.has(`bw-nav-contract-${rootAfterRegister}`),
    false,
  );
});

test("根 SW 的 data put 与 logout 交错时删除旧 epoch cache 并拒绝旧响应", async () => {
  const h = rootHarness();
  const dataStarted = deferred();
  const releaseData = deferred();
  h.state.fetchImpl = async (request) => {
    const path = new URL(absolute(request)).pathname;
    if (path === "/api/private.json") {
      dataStarted.resolve();
      await releaseData.promise;
      return basicResponse("old-root-data");
    }
    return basicResponse("auth-ok");
  };

  const oldData = h.request("/api/private.json");
  await dataStarted.promise;
  await h.auth("/logout");
  releaseData.resolve();

  const result = await oldData.response;
  assert.equal(result.status, 0);
  assert.equal(
    h.state.cacheEntries.has(`bw-data-contract-${EPOCH_A}`),
    false,
  );
});

test("根 SW 的 navigation put 已开始后发生 logout，也不会留下或返回旧导航壳", async () => {
  const h = rootHarness();
  const navCache = `bw-nav-contract-${EPOCH_A}`;
  const putStarted = deferred();
  const releasePut = deferred();
  h.state.putHook = async ({ cache }) => {
    if (cache === navCache) {
      putStarted.resolve();
      await releasePut.promise;
    }
  };
  h.state.fetchImpl = async (request) => {
    const path = new URL(absolute(request)).pathname;
    return path === "/dashboard/"
      ? basicResponse("<html>old navigation</html>", "", "text/html")
      : basicResponse("auth-ok");
  };

  const oldNavigation = h.request("/dashboard/", { mode: "navigate" });
  await putStarted.promise;
  await h.auth("/logout");
  releasePut.resolve();

  const result = await oldNavigation.response;
  assert.equal(result.status, 0);
  assert.equal(h.state.cacheEntries.has(navCache), false);
});

test("auth epoch 写入失败会撤销自己的 pending key，不会把两个 SW 阻塞十分钟", async () => {
  const h = harness();
  h.state.putHook = async ({ cache, url }) => {
    if (cache === AUTH_STATE_CACHE && url === absolute(AUTH_EPOCH_KEY)) {
      throw new Error("simulated epoch write failure");
    }
  };
  h.state.fetchImpl = async () => response("must-not-run");

  await assert.rejects(
    h.auth("/logout"),
    /simulated epoch write failure/,
  );
  assert.equal(authPendingKeys(h.state.cacheEntries).length, 0);
  assert.equal(h.state.fetchCalls.length, 0);
});

test("根/PDF 两个 SW 的并发身份切换使用独立 pending；任一未完成时私有读取均 fail closed", async () => {
  const shared = new Map();
  const pdf = harness(shared);
  const root = rootHarness(shared);
  const pdfFetchStarted = deferred();
  const rootFetchStarted = deferred();
  const releasePdf = deferred();
  const releaseRoot = deferred();
  pdf.state.fetchImpl = async (request) => {
    if (new URL(absolute(request)).pathname === "/logout") {
      pdfFetchStarted.resolve();
      await releasePdf.promise;
      return response("pdf-auth-ok");
    }
    return response("unexpected", NS_A);
  };
  root.state.fetchImpl = async (request) => {
    if (new URL(absolute(request)).pathname === "/login") {
      rootFetchStarted.resolve();
      await releaseRoot.promise;
      return basicResponse("root-auth-ok");
    }
    return basicResponse("unexpected");
  };

  const pdfAuth = pdf.startAuth("/logout");
  await pdfFetchStarted.promise;
  const rootAuth = root.startAuth("/login", {
    method: "POST",
    mode: "navigate",
  });
  await rootFetchStarted.promise;
  assert.equal(authPendingKeys(shared).length, 2);

  releasePdf.resolve();
  await pdfAuth.response;
  await pdfAuth.done;
  assert.equal(
    authPendingKeys(shared).length,
    1,
    "一个 scope 完成不得解除另一个 scope 的 pending",
  );

  pdf.addClient("blocked-client", "/pdf/view?file=x.pdf");
  const blockedPdf = await pdf.request(
    "/pdf/api/page-image?file=x.pdf&page=1",
    { clientId: "blocked-client" },
  );
  assert.equal(blockedPdf.status, 0);
  const blockedRoot = root.request("/api/private.json");
  assert.equal((await blockedRoot.response).status, 0);

  releaseRoot.resolve();
  await rootAuth.response;
  await rootAuth.done;
  assert.equal(authPendingKeys(shared).length, 0);
});

test("页面 helper 只能请求服务器重新绑定，不能发送 __USER__ 或 namespace", async () => {
  const helper = readFileSync(
    new URL(
      "../../_server_deploy/static/reader-runtime/pwa-cache-identity.js",
      import.meta.url,
    ),
    "utf8",
  );
  assert.match(helper, /BW_PDF_CACHE_REBIND/);
  assert.match(helper, /BW_PDF_CACHE_IDENTITY_REQUEST/);
  assert.doesNotMatch(
    helper,
    /postMessage\(\s*\{[^}]*namespace/s,
    "页面发给 SW 的消息中不得携带自报 namespace",
  );
});

test("静态壳仍可离线 cache-first，手动预热代码不再写 reader HTML", async () => {
  const h = harness();
  const shell = await h.cache("pdf-shell-v1");
  shell.seed("/static/pdf/reader.js?v=asset", "static-offline");
  const staticResponse = await h.request("/static/pdf/reader.js?v=asset");
  assert.equal(await staticResponse.text(), "static-offline");
  assert.equal(h.state.fetchCalls.length, 0);

  const indexSource = readFileSync(
    new URL("../../_server_deploy/templates/pdf_index.html", import.meta.url),
    "utf8",
  );
  const localBookSource = readFileSync(
    new URL(
      "../../_server_deploy/static/pdf/reader.src/31-localbook.js",
      import.meta.url,
    ),
    "utf8",
  );
  assert.doesNotMatch(indexSource, /shell\.put\(navUrl/);
  assert.doesNotMatch(localBookSource, /shell\.put\(navUrl/);
  assert.doesNotMatch(indexSource + localBookSource, /const\s+_LB_CACHE/);
  assert.doesNotMatch(
    indexSource + localBookSource,
    /caches\.open\(\s*['"]pdf-(?:cache|private)/,
    "页面不得自行选择或打开账户私有 cache",
  );
  assert.match(indexSource, /fetch\(navUrl,\s*\{\s*cache:\s*'no-store'\s*\}\)/);
});
