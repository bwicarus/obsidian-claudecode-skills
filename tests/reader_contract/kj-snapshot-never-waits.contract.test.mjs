import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import path from "node:path";
import { fileURLToPath } from "node:url";

// 快照路径上不许有网络等待（2026-09-09 用户：「不要因为获取 kj 信息导致查看
// 快照变慢」）。
//
// 实测的代价不是"KJ 算得慢"，而是**等一个没在跑的服务**：Flask 由 ReaderPC
// 托管，ReaderPC 一关它就没了，而 Windows 上连一个拒连的端口要约 2 秒
// （连测三次都是 2050 ms 上下）。于是每次带书页的快照都白等 2 秒。
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const CS = "extensions/bw-reader-webext/windows/ComputerVoiceAudio/";
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");
const KJ = read(CS + "KjPageClient.cs");
const MCP = read(CS + "ReaderContextMcpServer.cs");

test("附块的方法是同步的，签名里没有 Task", () => {
  // 刻意不写成 async Task：调用点就没有 await 可写，以后想"顺手等一下"
  // 得先改签名，而那一步足够让人停下来想。
  assert.match(KJ, /internal static void AttachToSnapshot\(JsonObject payload\)/);
  assert.ok(
    !/AttachToSnapshotAsync/.test(KJ),
    "旧的 async 版本必须整个消失，不能两个并存",
  );
});

test("快照调用点没有 await，也没有 KJ 的 HTTP", () => {
  const start = MCP.indexOf("JsonObject payload = BuildToolPayload(forModel: true)");
  assert.ok(start > 0);
  const region = MCP.slice(start, start + 2200);
  assert.match(region, /KjPageClient\.AttachToSnapshot\(payload\);/);
  assert.ok(
    !/await KjPageClient/.test(region),
    "快照路径不许 await KJ",
  );
});

test("附块只读缓存，缺了就丢后台", () => {
  const body = KJ.slice(
    KJ.indexOf("internal static void AttachToSnapshot"),
    KJ.indexOf("/// 后台补一次"),
  );
  assert.match(body, /TryCached\(bookKey, pageNo\)/);
  assert.match(body, /RefreshInBackground\(bookKey, pageNo\)/);
  // 附块这一段里不许出现真正的取数
  assert.ok(!/FetchAsync/.test(body), "附块不许自己去取");
  assert.ok(!/await /.test(body), "附块里一个 await 都不该有");
});

test("缺块时出声，不静默", () => {
  // 静默缺块会让模型以为这页没有 KJ 数据，而那跟"取不到"是两件事。
  const body = KJ.slice(
    KJ.indexOf("internal static void AttachToSnapshot"),
    KJ.indexOf("/// 后台补一次"),
  );
  assert.match(body, /"status"\] = "pending"/);
  assert.match(body, /"status"\] = "unavailable"/);
  // 措辞不能邀请轮询，否则省下的 2 秒会被模型反复查快照吃回去
  assert.match(body, /不必为此重复查快照/);
});

test("后台刷新用 CancellationToken.None", () => {
  // 快照那一刻就返回了；拿请求的 token 会让后台取数当场被取消，
  // 于是缓存永远填不上，而表现只是"那块永远是 pending"，没有一处会报错。
  const body = KJ.slice(
    KJ.indexOf("private static void RefreshInBackground"),
    KJ.indexOf("private static async Task<JsonObject> SendAsync"),
  );
  assert.match(body, /FetchAsync\(book, page, CancellationToken\.None\)/);
  // 同一页别并发取好几次
  assert.match(body, /if \(!InFlight\.Add\(key\)\) return;/);
});

test("失败不缓存，改开熔断", () => {
  // 缓存住一次失败等于把一次抖动变成整个 TTL 的空白。
  const fetch = KJ.slice(
    KJ.indexOf("private static async Task<JsonObject> FetchAsync"),
    KJ.indexOf("internal static async Task<JsonObject> SubmitAsync"),
  );
  assert.match(fetch, /StartCooling\(/);
  assert.match(fetch, /StopCooling\(\);\s*\n\s*Remember\(/);
});

test("page_text 那条仍然会取，但熔断时立刻返回", () => {
  // 那一刻模型正要交分析，值得等真实耗时；但不值得等一个已知不在的服务。
  const block = KJ.slice(
    KJ.indexOf("internal static async Task<JsonObject> BlockAsync"),
    KJ.indexOf("private static async Task<JsonObject> FetchAsync"),
  );
  assert.match(block, /TryCached\(file, page\)/);
  assert.match(block, /if \(Cooling\(out string why\)\)/);
  assert.match(block, /BW_KJ_COOLING/);
});

test("交完分析当场作废那一页的缓存", () => {
  // 否则模型刚交完，下一次快照还告诉它"这页没分析过，去交一份"，它会重复交。
  const submit = KJ.slice(
    KJ.indexOf("internal static async Task<JsonObject> SubmitAsync"),
    KJ.indexOf("internal static void AttachToSnapshot"),
  );
  assert.match(submit, /Forget\(book, pageNo\)/);
});

test("缓存有上限", () => {
  assert.match(KJ, /if \(Blocks\.Count > \d+\)/);
});
