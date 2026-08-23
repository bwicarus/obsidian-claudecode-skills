import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");

const VOICECALL = read("_server_deploy/static/pdf/rc-voicecall.js");
const INBOUND = read("_server_deploy/static/pdf/rc-computer-voice.js");
const VENDOR_IN = read("extensions/bw-reader-webext/vendor/rc-computer-voice.js");
const CALL = read("extensions/bw-reader-webext/call.js");
const FACADE = read("extensions/bw-reader-webext/src/facade.js");
const ENVELOPE = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderRealtimeOutput.cs",
);
const RPC = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderRealtimeOutputRpc.cs",
);
const MCP = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderContextMcpServer.cs",
);

// 回执要能说出「卡片钉在正文上了没有」。
//
// 2026-08-19 用户连问四轮「AI 说成功但什么都看不到」，根子是**回执里没有这条
// 信息的位置**：
//   · rc-voicecall.js 的 then 回调不接参数，各分支拼的结构化结果被整个丢掉，
//     回执永远是固定字面量 { outcome: 'applied' }
//   · 而 MCP 终点那句 status 是对 request.Kind 做 switch 得来的**常量**，
//     card 落进 `_ => "delivered"` —— 它只表示「这条 kind 是 card」
// 也就是说：「已送达」跟卡片有没有钉上，从来就是两回事。
//
// 这条链上有 11 处闸/重建点，少改一处就断在那儿，**而症状全都是
// 「AI 照常说已送达」** —— 没有任何一处会报错。所以这里逐处钉住。

test("绑定结果从阅读器出发时就被带上", () => {
  // 每次 renderInfo 自己返回结果；不能在异步持久化之后读取一份全局“上一张卡”状态。
  assert.match(VOICECALL, /async function renderInfo\(card, options\)/);
  assert.match(VOICECALL, /bindOutcome: outcome \|\| 'none'/);
  assert.doesNotMatch(VOICECALL, /_lastBindOutcome/);
  assert.match(
    VOICECALL,
    /work = Promise\.resolve\(renderInfo\(p\.card,[\s\S]{0,500}?result\.rendered !== true/,
    "回执必须等待本次卡片渲染和持久化的 Promise",
  );
  // ⚠ 回调必须接住值。原先写的是 `.then(function () {`，各分支拼的东西全丢。
  assert.match(
    VOICECALL,
    /return Promise\.resolve\(work\)\.then\(function \(value\) \{/,
    "then 回调不接参数的话，上游拼什么都进不了回执",
  );
  // 只搬认识的键：下游 exactObject 是全等白名单，多一个键会让**成功的投递
  // 被回成 rejected**，且链路上零报错。
  assert.match(VOICECALL, /if \(value\.bindOutcome\) receipt\.bindOutcome = value\.bindOutcome;/);
});

test("同一投递在落库完成前共享 single-flight，不会重复写卡", () => {
  assert.match(VOICECALL, /var _readerOutputPending = Object\.create\(null\)/);
  assert.match(
    VOICECALL,
    /if \(_readerOutputPending\[correlation\]\) return _readerOutputPending\[correlation\]/,
  );
  assert.match(
    VOICECALL,
    /Promise\.resolve\(\)\.then\(function \(\) \{\s*return _applyReaderRealtimeOutput\(delivery\)/,
    "先登记 pending，再在微任务中执行持久化，堵住同步重入窗口",
  );
  assert.match(VOICECALL, /_readerOutputPending\[correlation\] = pending/);
});

test("page-chars 回退后真实重放只重试绑定，成功回执可再次证明 bound", async () => {
  const start = VOICECALL.indexOf("var _readerOutputSeen = Object.create(null)");
  const end = VOICECALL.indexOf("// 把已登记的草稿镜像到当前对话流", start);
  assert.ok(start >= 0 && end > start, "找不到 Reader output 接收器源码");
  const lifecycle = VOICECALL.slice(start, end);
  const renderCalls = [];
  const accept = new Function(
    "renderInfo",
    "window",
    "RC",
    `${lifecycle}\nreturn _acceptReaderRealtimeOutput;`,
  )(
    async (_card, options) => {
      renderCalls.push({ ...options });
      return renderCalls.length === 1
        ? { rendered: true, bindOutcome: "floating", bindReason: "create-failed" }
        : { rendered: true, bindOutcome: "bound", bindReason: null };
    },
    {},
    {},
  );
  const delivery = {
    correlation: "output-durable-page-card-1",
    kind: "card",
    payload: {
      card: {
        kind: "general",
        title: "重放绑定",
        data: { text: "只显示一次" },
        bind: { kind: "page-chars", page: 25, from: 8, to: 11, text: "目标词" },
      },
    },
  };

  assert.deepEqual(await accept(delivery), {
    outcome: "applied",
    bindOutcome: "floating",
    bindReason: "create-failed",
  });
  assert.deepEqual(await accept(delivery), {
    outcome: "applied",
    bindOutcome: "bound",
  });
  assert.deepEqual(await accept(delivery), {
    outcome: "replay",
    bindOutcome: "bound",
  });
  assert.deepEqual(renderCalls, [
    { uid: delivery.correlation, bindOnly: false },
    { uid: delivery.correlation, bindOnly: true },
  ], "第一次失败后不能把 correlation 提前记成完成，重放也不能再次完整渲染");
});

test("bindOnly 使用真实 renderInfo 时不会再生成第二张回退卡", async () => {
  const start = VOICECALL.indexOf("function _renderInfoResult(rendered, outcome, reason)");
  const end = VOICECALL.indexOf("function renderImgs(imgs)", start);
  assert.ok(start >= 0 && end > start, "找不到 renderInfo 源码");
  const renderSource = VOICECALL.slice(start, end);
  let persistCalls = 0;
  let toastCalls = 0;
  const renderInfo = new Function(
    "window",
    "RC",
    "_infoHtml",
    "_infoText",
    `${renderSource}\nreturn renderInfo;`,
  )(
    {
      async __pageBindPersist() {
        persistCalls += 1;
        return { ok: false, why: "create-failed" };
      },
    },
    { toast() { toastCalls += 1; } },
    () => "<p>卡片内容</p>",
    () => "卡片内容",
  );

  const result = await renderInfo({
    kind: "general",
    title: "只重试落库",
    data: { text: "卡片内容" },
    bind: { kind: "page-chars", page: 25, from: 8, to: 11, text: "目标词" },
  }, { uid: "output-durable-page-card-1", bindOnly: true });

  assert.deepEqual(result, {
    rendered: true,
    bindOutcome: "floating",
    bindReason: "create-failed",
  });
  assert.equal(persistCalls, 1);
  assert.equal(toastCalls, 0, "重放失败不能重复 toast，更不能继续进入浮层/对话渲染");
});

test("frame 回执等待时间覆盖 document-notes 的合法写入预算", () => {
  assert.match(CALL, /const REALTIME_OUTPUT_RECEIPT_TIMEOUT_MS = 18000/);
  assert.match(CALL, /\}, REALTIME_OUTPUT_RECEIPT_TIMEOUT_MS\)/);
});

test("page-chars 只在权威 placement 提交后回 bound", () => {
  assert.match(VOICECALL, /await window\.__pageBindPersist\(_b, _pp\)/);
  assert.match(VOICECALL, /if \(_pr && _pr\.ok === true\) return _renderInfoResult\(true, 'bound'\)/);
  assert.doesNotMatch(VOICECALL, /window\.__pageBindCard\(_b, _pp\)/);
  assert.match(VOICECALL, /uid: card\.cid \|\| options\.uid \|\| ''/);
});

test("阅读器入站闸放行这两个字段", () => {
  // 这一处没放行的话，上游一带新字段就被判无效 → 下面的 catch 整个换成
  // rejected，表现是「一次成功的绑定被回成失败」。
  for (const src of [INBOUND, VENDOR_IN]) {
    assert.match(
      src,
      /exactObject\(receipt, \["outcome"\], \["error", "bindOutcome", "bindReason"\]/,
      "vendor 副本靠 build.py 生成，忘了重跑它就还是旧闸",
    );
  }
});

test("放行之后还要真的搬 —— 三处 ACK 重建都不能丢", () => {
  // 只放行不搬 = 校验全过、就是不生效，比被拒难查得多
  assert.match(INBOUND, /bindOutcome: receipt\.bindOutcome \|\| null/);
  assert.match(CALL, /bindOutcome: receipt\.bindOutcome \|\| null/);
  assert.match(FACADE, /bindOutcome: receipt\.bindOutcome \|\| null/);
});

test("不能复用 error 字段", () => {
  // error 必须当且仅当 outcome==='rejected' 时存在，而「退回浮层」是 applied
  // —— 卡确实送到了，只是没钉上。所以必须是独立字段。
  assert.match(
    INBOUND,
    /\(receipt\.outcome === "rejected"\) !== Object\.prototype\.hasOwnProperty\.call\(receipt, "error"\)/,
    "这条不变式还在，说明 bind 结果确实没走 error",
  );
});

test("跨机信封开了槽并显式取值", () => {
  // record 是容器闸：不开槽，前后两处 new 就无处可搬
  assert.match(ENVELOPE, /string\? BindOutcome,\s*\n\s*string\? BindReason\)/);
  // Exact 是 SetEquals，可选字段必须走 ExactWithOptional
  assert.match(ENVELOPE, /new\[\] \{ "bindOutcome", "bindReason" \}/);
  // 过闸之后 JsonElement 的其它字段就不存在了，必须显式取
  assert.match(ENVELOPE, /message\.TryGetProperty\("bindOutcome", out _\)/);
  // 枚举收窄，别让任意字符串混进来
  assert.match(ENVELOPE, /"none" or "bound" or "floating" or "unknown"/);
});

test("命名管道两条路都带这两个键", () => {
  assert.match(RPC, /\["bindOutcome"\] = ack\.BindOutcome/);
  // ⚠ 失败包也必须带（填 null）：RequireExact 在读 ok 之前就跑，
  //   缺键会让一出错就变成「回包字段不匹配」这另一种错，真原因被盖掉。
  assert.match(RPC, /\["bindOutcome"\] = null/);
  assert.match(RPC, /"bindOutcome",\s*\n\s*"bindReason",/);
});

test("终点：AI 看得到，且 status 不再冒充答案", () => {
  assert.match(MCP, /\["bind_outcome"\] = ack\.BindOutcome/);
  assert.match(MCP, /\["bind_reason"\] = ack\.BindReason/);
  // status 那句常量还在（它有自己的用处），但旁边必须写明它不是答案
  assert.match(MCP, /status 是对 request\.Kind 做 switch 得来的\*\*常量\*\*/);
});

test("没执行过的路径填 unknown，不是编一个结果", () => {
  // 超时/过期时「钉没钉上」是真的未知。填 floating 就是编的。
  assert.match(CALL, /bindOutcome: "unknown"/);
});
