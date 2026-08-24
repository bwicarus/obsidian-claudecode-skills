// 复制命令信封 replication-command/1 的多副本一致性闸。
// 权威在 Python（replication_command_ledger.py），副本在 C# 闸、App 发送端、
// 传输层 —— 登记见 reader-specs/contract-sites.json 的
// replication-command-envelope。这里把"逐字段一致"变成会红的测试：
// 任何一处改了 regex/字段名/白名单而没同步其它处，这里当场红。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const PY = read(
  "extensions/bw-reader-webext/windows/computer-voice-desktop/replication_command_ledger.py",
);
const CS = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReplicationCommandIntake.cs",
);
const RUNTIME = read("_server_deploy/static/pdf/native-local-runtime.js");
const VOICE = read("_server_deploy/static/pdf/rc-computer-voice.js");
const APPLY = read(
  "extensions/bw-reader-webext/windows/computer-voice-desktop/replication_apply.py",
);

test("envelope contract string is identical at every site", () => {
  for (const [name, source] of [["py", PY], ["cs", CS], ["runtime", RUNTIME]]) {
    assert.match(source, /replication-command\/1/, name);
  }
});

test("device family regex is identical across Python and C#", () => {
  const pattern = "(?:native-app|pwa-install|server-node)-v1-[a-f0-9]{32}";
  assert.ok(PY.includes(pattern), "python device regex");
  assert.ok(CS.includes(pattern), "csharp device regex");
  // App 发送端用 runtime 的 stableDeviceId（pwa-install-v1-<32hex>），
  // 必须落在这个族内。
  assert.match(RUNTIME, /pwa-install-v1-\[a-f0-9\]\{32\}/);
});

test("mutation id and repbook shapes agree", () => {
  for (const [name, source] of [["py", PY], ["cs", CS]]) {
    assert.ok(source.includes("mut-v2-[a-f0-9]{32}"), name + " mutation regex");
    assert.ok(source.includes("repbook-[a-f0-9]{32}"), name + " repbook regex");
  }
  // runtime 铸造侧：randomHex(16) = 32 hex
  assert.match(RUNTIME, /'mut-v2-' \+ randomHex\(16\)/);
  assert.match(RUNTIME, /'repbook-' \+ randomHex\(16\)/);
});

test("envelope and op field sets agree at every site", () => {
  // Python 权威
  assert.match(PY, /"contract", "deviceId", "replicationBookId", "actor", "op"/);
  assert.match(PY, /"mutationId", "url", "method", "body"/);
  // C# 闸
  assert.match(CS, /\["contract", "deviceId", "replicationBookId", "actor", "op"\]/);
  assert.match(CS, /\["mutationId", "url", "method", "body"\]/);
  // runtime 发送端构造的对象字面量
  const build = RUNTIME.slice(
    RUNTIME.indexOf("function buildReplicationEnvelope"),
    RUNTIME.indexOf("function replicationOutboxItemMutation"),
  );
  for (const field of ["contract:", "deviceId:", "replicationBookId:", "actor:", "op:"]) {
    assert.ok(build.includes(field), "runtime envelope field " + field);
  }
  for (const field of ["mutationId:", "url:", "method:", "body:"]) {
    assert.ok(build.includes(field), "runtime op field " + field);
  }
});

test("actor vocabulary agrees", () => {
  assert.match(PY, /_ACTORS = frozenset\(\("user", "ai", "system"\)\)/);
  assert.match(CS, /actor is not \("user" or "ai" or "system"\)/);
});

test("transport pushes one command per frame over a context-only channel and echoes mutationId", () => {
  const push = VOICE.slice(
    VOICE.indexOf("function pushReplicationCommands"),
    VOICE.indexOf("function lookupJapaneseFallback"),
  );
  assert.match(push, /request\("context-open"/, "先进入 context-only");
  assert.match(push, /request\("replication-command", \{\s*sessionId: session\.id,\s*envelope: envelope,\s*\}\)/);
  assert.match(push, /\["contract", "mutationId", "outcome"\]/, "回执形状校验");
  // C# 端 action 名与回执字段一致
  assert.match(CS, /CommandType = "replication-command"/);
  assert.match(
    read("extensions/bw-reader-webext/windows/ComputerVoiceAudio/DirectBridgeProtocol.cs"),
    /mutationId = envelope\.MutationId,\s*outcome = receipt\.Outcome,/,
  );
});

test("windows applier requires POST bodies to carry the stored item id (sender contract)", () => {
  assert.match(APPLY, /POST 必须带发送端已落库的完整条目/);
  // runtime 发送端确实带完整条目
  assert.match(
    RUNTIME,
    /'\/pdf\/api\/highlights', 'POST',\s*Object\.assign\(\{ file: localFileRef\(\) \}, clone\(value\.highlight\)\)/,
  );
});

test("drain settles only accepted commands (at-least-once, server-side idempotency)", () => {
  const drain = RUNTIME.slice(
    RUNTIME.indexOf("function drainReplicationOutbox"),
    RUNTIME.indexOf("function scheduleReplicationDrain"),
  );
  assert.match(drain, /item\.outcome === 'accepted'/);
  assert.match(drain, /accepted\.has\(/);
  assert.doesNotMatch(drain, /outcome === 'applied'/, "接收端从不谎称 applied");
});

test("resync and digest-query names agree across all sites", () => {
  // 整域重同步：runtime 发送端 ↔ Windows 执行器
  assert.match(RUNTIME, /REPLICATION_RESYNC_URL = '\/replication\/resync'/);
  assert.match(APPLY, /RESYNC_URL = "\/replication\/resync"/);
  // 对账/重同步的域名单：Windows 白名单 ↔ App 对账域一一对应（六域）
  const RESYNC_BLOCK = APPLY.slice(
    APPLY.indexOf("_RESYNC_DOMAINS = frozenset(("),
    APPLY.indexOf("))", APPLY.indexOf("_RESYNC_DOMAINS")),
  );
  const DOMAINS = [
    "pdf-highlights", "epub-highlights", "document-notes",
    "user-pages", "pdf-ink", "epub-ink",
  ];
  for (const domain of DOMAINS) {
    assert.ok(RESYNC_BLOCK.includes(`"${domain}"`), "windows resync 域 " + domain);
    assert.match(RUNTIME, new RegExp(`domain: '${domain}'`), "runtime 对账域 " + domain);
  }
  // 便签/插入页执行映射与 PATCH 白名单跟 App 路由允许集一致
  assert.match(APPLY, /\("\/pdf\/api\/notes", "POST"\): \("document-notes", "upsert"\)/);
  assert.match(APPLY, /"anchor", "text", "color", "w", "h", "collapsed",\s*"strokes", "video", "card", "html", "iar",/);
  assert.match(APPLY, /\("\/pdf\/api\/userpages", "POST"\): \("user-pages", "upsert"\)/);
  assert.match(APPLY, /"user-pages": frozenset\(\("title", "md", "after", "h", "blocks"\)\)/);
  // 墨迹：一页一条目、空笔画=墓碑；两端物化都按键排序
  assert.match(APPLY, /\("\/pdf\/api\/ink", "POST"\): \("pdf-ink", "ink-set"\)/);
  assert.match(APPLY, /if domain\.endswith\("-ink"\):\s*\n\s*return \[items\[item_id\] for item_id in sorted\(items\)\]/);
  assert.match(RUNTIME, /Object\.keys\(map\)\.sort\(\)\.map/);
  // 墨迹静置后才入队（规格 §3）
  assert.match(RUNTIME, /REPLICATION_INK_SETTLE_MS = 60000/);
  // 摘要查询：C# action ↔ 传输层 ↔ 视图 contract ↔ Python 导出 contract
  const CS_INTAKE = read(
    "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReplicationCommandIntake.cs",
  );
  assert.match(CS_INTAKE, /DigestQueryType = "replication-digest-query"/);
  assert.match(VOICE, /request\("replication-digest-query"/);
  assert.match(CS_INTAKE, /DigestsViewContract = "replication-digests-view\/1"/);
  assert.match(VOICE, /"contract", "replicationBookId", "generatedAtUtcMs", "domains"/);
  assert.match(APPLY, /REPLICATION_DIGESTS_CONTRACT = "replication-digests\/1"/);
  assert.match(CS_INTAKE, /!= "replication-digests\/1"/);
});

test("both ends materialize domains by the same rule before digesting", () => {
  // Python：order → 存活条目，序外存活条目按 id 排序补末尾
  assert.match(APPLY, /def materialize_domain_items/);
  assert.match(APPLY, /for item_id in sorted\(items\):/);
  // App：readHighlightCollection 的物化即对账输入（canonical + sha256）
  assert.match(RUNTIME, /sha256Hex\(canonicalJSONString\(payload\)\)/);
  // Python canonical 与 JS JSON.stringify 逐位一致的三要素
  assert.match(APPLY, /sort_keys=True, separators=\(",", ":"\)/);
  assert.match(APPLY, /ensure_ascii=False/);
});

test("idle reconcile loop exists and never blocks process exit", () => {
  // 对账只在队列排空后触发的缺口：无命令活动（如真实插入页走 mutation
  // 不入队）时永不对账。必须有 unref 的周期对账链兜底。
  const idle = RUNTIME.slice(
    RUNTIME.indexOf("function scheduleReplicationIdleReconcile"),
    RUNTIME.indexOf("var REPLICATION_DOMAINS"),
  );
  assert.match(idle, /maybeReconcileReplication\(\)/);
  assert.match(idle, /scheduleReplicationIdleReconcile\(\);/, "自我重排成链");
  assert.match(idle, /timer\.unref === 'function'\) timer\.unref\(\)/);
  assert.match(RUNTIME, /scheduleReplicationIdleReconcile\(\);\s*\}\s*try \{/,
    "boot 就绪后挂上空闲对账链");
});

test("chunk protocol constants agree across transport and receiver", () => {
  const CS_INTAKE = read(
    "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReplicationCommandIntake.cs",
  );
  // 账本层信封上限：Python 权威 ↔ C# 同值（6MB）；帧限是传输层的事
  assert.match(PY, /MAX_ENVELOPE_BYTES = 6 \* 1024 \* 1024/);
  assert.match(CS_INTAKE, /MaxEnvelopeBytes = 6 \* 1024 \* 1024/);
  // 发送端切片 ≤ 接收端单片上限；App 入队闸 < 账本层上限（留余量提前拒）
  assert.match(VOICE, /REPLICATION_CHUNK_PART_CHARS = 160 \* 1024/);
  assert.match(CS_INTAKE, /MaxChunkPartChars = 200 \* 1024/);
  assert.match(RUNTIME, /byteLength > 5 \* 1024 \* 1024/);
  // 片数上限覆盖最大命令：64 × 160KiB base64 ≈ 7.6MB 原文 > 6MB
  assert.match(CS_INTAKE, /MaxChunkCount = 64/);
  // action 名与片字段两端一致
  assert.match(VOICE, /request\("replication-command-chunk"/);
  assert.match(CS_INTAKE, /ChunkType = "replication-command-chunk"/);
  assert.match(VOICE, /mutationId: mutationId,\s*seq: index,\s*total: total,/);
  const CS_PROTO = read(
    "extensions/bw-reader-webext/windows/ComputerVoiceAudio/DirectBridgeProtocol.cs",
  );
  assert.match(CS_PROTO, /\["mutationId", "seq", "total", "part"\]/);
  // 中间片绝不谎称 accepted：发送端校验 partial，接收端只在末片落盘后 accepted
  const send = VOICE.slice(
    VOICE.indexOf("function sendReplicationEnvelope"),
    VOICE.indexOf("function queryReplicationDigests"),
  );
  assert.match(send, /index < total - 1 && checked\.outcome !== "partial"/);
  assert.match(CS_PROTO, /outcome = "partial",/);
});
