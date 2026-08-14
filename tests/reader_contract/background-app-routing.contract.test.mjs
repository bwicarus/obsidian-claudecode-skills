// 扩展后台的请求分流：App-owned 打宿主 App，其余仍打 Pi。
//
// 这条改动最容易出的两种错，各有一条测试盯着：
//   · 把 origin 白名单放宽 —— 为了让 App 地址通过而放开校验，等于顺手拆掉
//     "只允许固定来源"这道边界
//   · 拿不到落点时回落 Pi —— 割裂被藏起来，用户以为存上了，换个入口就不见了
import assert from "node:assert/strict";
import test from "node:test";
import vm from "node:vm";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const ROOT = new URL("../../", import.meta.url);
const BG = readFileSync(
  new URL("extensions/bw-reader-webext/background.js", ROOT), "utf8");
const RD = require("../../extensions/bw-reader-webext/src/route-destination.js");
const LE = require("../../extensions/bw-reader-webext/src/local-endpoint.js");

const TOKEN = "b".repeat(64);
const APP_BASE = `http://127.0.0.1:43129/r/${TOKEN}`;
const PI = "https://bwicarus.taile44d0c.ts.net";

function balanced(source, start) {
  const open = source.indexOf("{", start);
  let depth = 0;
  for (let i = open; i < source.length; i += 1) {
    if (source[i] === "{") depth += 1;
    if (source[i] === "}") depth -= 1;
    if (depth === 0) return source.slice(start, i + 1);
  }
  assert.fail("括号未闭合");
}

/** 把 resolvedAppRequestURL 放进沙箱，注入可控的 capabilities。 */
function resolveWith(capabilities, pathname, search = "") {
  const start = BG.indexOf("async function resolvedAppRequestURL(");
  assert.notEqual(start, -1, "找不到落点解析函数");
  const context = {
    URL, Promise, Object, Math, String,
    BWLocalEndpoint: LE,
    sendSafariNativeMessage: () => Promise.resolve(capabilities),
    NATIVE_APP_CONTRACT: "bw-reader-native/1",
    out: null,
  };
  context.globalThis = context;
  const capStart = BG.indexOf("async function appLocalEndpointCapabilities(");
  vm.runInNewContext(
    `${balanced(BG, capStart)}
     ${balanced(BG, start)}
     out = resolvedAppRequestURL({ url: new URL(${JSON.stringify(PI + pathname + search)}) });`,
    context,
  );
  return context.out;
}

const READY = {
  localEndpointStatus: "ready",
  localEndpoint: { base: APP_BASE, startedAtEpochSeconds: 1_700_000_000 },
};

test("App-owned 请求被改指向宿主 App，路径与 query 原样保留", async () => {
  const url = await resolveWith(READY, "/pdf/api/highlights", "?file=book.pdf");
  assert.equal(url.origin, "http://127.0.0.1:43129");
  assert.equal(url.pathname, `/r/${TOKEN}/pdf/api/highlights`);
  assert.equal(url.search, "?file=book.pdf");
});

test("App 没在跑：硬失败，且错误码能让界面给出可操作提示", async () => {
  await assert.rejects(
    resolveWith({ localEndpointStatus: "app-not-running" }, "/pdf/api/notes"),
    (error) => {
      assert.equal(error.code, LE.ERROR_CODES.APP_NOT_RUNNING);
      return true;
    },
  );
});

test("拿不到落点时绝不回落 Pi", async () => {
  // 回落看起来友好，实际是把割裂藏起来：写进了另一份存储，
  // 而用户要等"东西不见了"才发现。
  for (const capabilities of [
    { localEndpointStatus: "app-not-running" },
    { localEndpointStatus: "unavailable", localEndpointError: "App Group 不可用" },
    { actions: ["capabilities"] },
  ]) {
    let resolved = null;
    try {
      resolved = await resolveWith(capabilities, "/pdf/api/highlights");
    } catch (_) {
      continue; // 抛错才是对的
    }
    assert.fail(`不该解析出地址，却得到 ${resolved}`);
  }
});

// ── 接线正确性（源码层）────────────────────────────────────────────
test("闸门返回 destination，且由分流表判定", () => {
  const start = BG.indexOf("function checkedBwFetchRequest(");
  const body = balanced(BG, start);
  assert.match(body, /BWRouteDestination\.destinationFor\(url\.pathname\)/);
  assert.match(body, /return \{ url, method, destination \}/);
});

test("origin 白名单没有被放宽", () => {
  // 最容易犯的错：为了让 127.0.0.1 通过而放开这道校验。
  // 正确做法是保持校验、在通过之后重写，所以这一行必须原样还在。
  const start = BG.indexOf("function checkedBwFetchRequest(");
  const body = balanced(BG, start);
  assert.match(
    body,
    /url\.origin !== ORIGIN && !directOpenAI/,
    "调用方仍只能传 Pi 的 URL；重写发生在校验之后",
  );
  assert.doesNotMatch(body, /127\.0\.0\.1/,
    "闸门里不该出现本机地址：那意味着白名单被改动了");
});

test("只有判为 APP 的请求才会被重写", () => {
  const anchor = BG.indexOf("checked = checkedBwFetchRequest(url, init);");
  assert.notEqual(anchor, -1);
  const region = BG.slice(anchor, anchor + 500);
  // 断言"按目的地分支"这件事，不是分支写成什么样。调用点刻意不引用
  // BWRouteDestination —— 这个文件被大量测试以沙箱方式逐段提取，
  // 多一处模块级依赖就多一处在别处炸掉的可能。
  assert.match(
    region,
    /checked\.destination === ("app"|BWRouteDestination\.DESTINATION\.APP)/,
    "必须按目的地分支，不能无条件重写",
  );
  assert.match(region, /await resolvedAppRequestURL\(checked\)/);
  assert.equal(
    RD.DESTINATION.APP, "app",
    "调用点比较的字面量必须与分流表的取值一致，否则分支永远不成立",
  );
});

test("两个模块已进入后台脚本清单与打包清单", () => {
  // 漏掉任一处，运行时就是 BWRouteDestination is not defined，
  // 而那会表现为"所有请求都失败"，跟分流本身看起来毫无关系。
  assert.match(BG, /"src\/route-destination\.js"/);
  assert.match(BG, /"src\/local-endpoint\.js"/);
  const pkg = readFileSync(
    new URL("extensions/bw-reader-webext/package_safari.py", ROOT), "utf8");
  assert.match(pkg, /"src\/route-destination\.js"/);
  assert.match(pkg, /"src\/local-endpoint\.js"/);
});

test("分流表与闸门用的是同一份判定", () => {
  assert.equal(RD.destinationFor("/pdf/api/highlights"), RD.DESTINATION.APP);
  assert.equal(RD.destinationFor("/pdf/api/dict"), RD.DESTINATION.PI);
});
