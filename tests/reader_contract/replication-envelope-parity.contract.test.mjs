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
