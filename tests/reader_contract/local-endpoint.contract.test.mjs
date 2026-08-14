// 扩展直连宿主 App 的本机落点。
//
// 这条链的价值全在「拿不到落点时怎么办」。回落 Pi 看起来更友好，实际是把数据
// 割裂藏起来：用户以为存上了，换个入口就不见了，而且再也想不起是哪一次存到了
// 另一边。所以硬失败 —— 当场不可用，比事后对不上账好。
//
// 另一条：三种拿不到的原因要分开报。「App 没开」用户自己能解决，「App Group
// 配置坏了」用户解决不了、要看日志，「旧版 App 没这个字段」该去更新。折成一句
// 「连不上」，三种人会走同一条错路。
import assert from "node:assert/strict";
import test from "node:test";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const LE = require("../../extensions/bw-reader-webext/src/local-endpoint.js");

const TOKEN = "a".repeat(64);
const GOOD_BASE = `http://127.0.0.1:43129/r/${TOKEN}`;

// ── 落点校验 ────────────────────────────────────────────────────────
test("只认本机回环上的落点", () => {
  assert.equal(LE.isLoopbackBase(GOOD_BASE), true);
});

test("非回环地址一律拒绝", () => {
  // 这条 base 会被拼上 capability token 去发请求。指错地方等于把 token
  // 交给别人 —— 而 token 就是本机服务的全部门禁。
  const hostile = [
    `https://evil.example/r/${TOKEN}`,
    `http://127.0.0.1.evil.example/r/${TOKEN}`,
    `http://192.168.1.7:43129/r/${TOKEN}`,
    `http://localhost:43129/r/${TOKEN}`,       // 主机名可被解析到别处
    `http://user@127.0.0.1:43129/r/${TOKEN}`,  // userinfo 混淆
    `http://127.0.0.1:43129/r/${TOKEN}?x=1`,
    `http://127.0.0.1:43129/r/${TOKEN}#x`,
    `http://127.0.0.1:43129/r/NOTHEX`,
    `http://127.0.0.1:43129/other/${TOKEN}`,
    "",
    "not-a-url",
  ];
  for (const value of hostile) {
    assert.equal(LE.isLoopbackBase(value), false, `应拒绝: ${value}`);
  }
});

// ── 三种拿不到的原因 ────────────────────────────────────────────────
test("App 没在跑：可自解，提示打开 App", () => {
  const resolved = LE.parse({ localEndpointStatus: "app-not-running" });
  assert.equal(resolved.status, LE.STATUS.APP_NOT_RUNNING);
  assert.throws(
    () => LE.requireBase(resolved),
    (error) => {
      assert.equal(error.code, LE.ERROR_CODES.APP_NOT_RUNNING);
      assert.match(error.message, /打开 App/, "要说明用户该做什么");
      return true;
    },
  );
});

test("App Group 不可用：用户解决不了，要能看见原因", () => {
  const resolved = LE.parse({
    localEndpointStatus: "unavailable",
    localEndpointError: "BWReader App Group 不可用",
  });
  assert.equal(resolved.status, LE.STATUS.UNAVAILABLE);
  assert.throws(
    () => LE.requireBase(resolved),
    (error) => {
      assert.equal(error.code, LE.ERROR_CODES.UNAVAILABLE);
      assert.match(error.message, /App Group/, "原因要带出来，否则无从查起");
      return true;
    },
  );
});

test("旧版 App 没有这个字段：归为 missing，不是 unavailable", () => {
  // 两者的修法完全不同：一个去更新 App，一个去查签名与 App Group 配置。
  const resolved = LE.parse({ actions: ["capabilities"] });
  assert.equal(resolved.status, LE.STATUS.MISSING);
  assert.throws(
    () => LE.requireBase(resolved),
    (error) => {
      assert.equal(error.code, LE.ERROR_CODES.MALFORMED);
      assert.match(error.message, /更新 App/);
      return true;
    },
  );
});

test("声称 ready 但落点非法时降级为 unavailable，不放行", () => {
  const resolved = LE.parse({
    localEndpointStatus: "ready",
    localEndpoint: { base: "https://evil.example/r/" + TOKEN },
  });
  assert.equal(resolved.status, LE.STATUS.UNAVAILABLE);
  assert.throws(() => LE.requireBase(resolved));
});

// ── 正常路径 ────────────────────────────────────────────────────────
test("拿到落点后能构造出指向 App 的地址", () => {
  const resolved = LE.parse({
    localEndpointStatus: "ready",
    localEndpoint: { base: GOOD_BASE, startedAtEpochSeconds: 1_700_000_000 },
  });
  assert.equal(resolved.status, LE.STATUS.READY);
  const base = LE.requireBase(resolved);
  assert.equal(
    LE.localURL(base, "/pdf/api/highlights?file=x"),
    `${GOOD_BASE}/pdf/api/highlights?file=x`,
  );
});

test("localURL 拒绝非法 base 与相对路径", () => {
  assert.throws(() => LE.localURL("https://evil.example", "/pdf/api/notes"));
  assert.throws(() => LE.localURL(GOOD_BASE, "pdf/api/notes"),
    (error) => error.code === LE.ERROR_CODES.MALFORMED);
  // 空 base 拼上路径会变成相对请求，打到当前网页的源上去 —— 那才是最坏的：
  // 请求看起来发出去了，落到了一个毫不相干的站点。
  assert.throws(() => LE.localURL("", "/pdf/api/notes"));
});

test("永不给出回落到 Pi 的出口", () => {
  const source = require("node:fs").readFileSync(
    new URL("../../extensions/bw-reader-webext/src/local-endpoint.js",
      import.meta.url), "utf8");
  assert.doesNotMatch(
    source, /bwicarus\.space|taile44d0c|https:\/\/[^"'\s]*\/pdf\/api/,
    "模块里不该出现任何 Pi 地址：一旦有，某天就会有人接一条回落分支",
  );
});
