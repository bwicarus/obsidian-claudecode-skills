import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const CS = "extensions/bw-reader-webext/windows/ComputerVoiceAudio/";
const SNAPSHOT = read(CS + "DirectContextSnapshot.cs");
const ADAPTERS = read(CS + "DirectBridgeAdapters.cs");
const SERVER = read(CS + "DirectBridgeServer.cs");
const MCP = read(CS + "ReaderContextMcpServer.cs");
const DETECTOR = read(CS + "UplinkSpeechEndDetector.cs");
const SELFTEST = read(CS + "DirectBridgeSelfTest.cs");
const DOC = read("references/codex-reader-context.md");

// 钉住的快照（2026-09-05 用户定）：「不断固定我停止说话时刻的快照，AI 查看时看到的就是
// 最后一次被固定的快照」；打字发出同理。这份契约钉的是三件事：
//   ① 只有一个原语（PinAsync），触发方式随通道变、原语不变；
//   ② 文件名只有一处定义，写方与读方都从它算路径；
//   ③ 模型拿到的 payload 明说 basis 是 pinned 还是 live。

test("原语只有一个：接口上的 PinAsync，未接线实现也得有它", () => {
  assert.match(SNAPSHOT, /Task<DirectSnapshotForwardResult> PinAsync\(\s*\n\s*string reason,/);
  const unwired = SNAPSHOT.slice(
    SNAPSHOT.indexOf("class UnwiredDirectSnapshotContextAdapter"),
    SNAPSHOT.indexOf("class FileDirectSnapshotContextAdapter"));
  assert.match(unwired, /PinAsync\(/, "未接线实现要显式报不可用，不能静默");
  const file = SNAPSHOT.slice(SNAPSHOT.indexOf("class FileDirectSnapshotContextAdapter"));
  assert.match(file, /public async Task<DirectSnapshotForwardResult> PinAsync\(/);
  assert.match(file, /snapshot\["pinned"\] = new JsonObject/, "钉住的副本要带 pinned 元数据");
  assert.match(file, /File\.Move\(temporaryPath, path, overwrite: true\);/, "原子落盘：半份 JSON 会被读方当成完整的");
});

test("文件名一处定义：写方与读方都用 PinnedPathFor", () => {
  assert.match(SNAPSHOT, /internal const string PinnedFileName =\s*\n\s*"reader-context-pinned\.json";/);
  assert.equal((SNAPSHOT.match(/"reader-context-pinned\.json"/g) ?? []).length, 1, "字面量只能出现一次");
  assert.doesNotMatch(MCP, /reader-context-pinned/, "MCP 不许抄第二份文件名");
  assert.match(MCP, /FileDirectSnapshotContextAdapter\.PinnedPathFor\(_statePath\)/);
});

test("触发一：上行 PCM 上判「说完」→ 钉；判定器只放大不猜", () => {
  assert.match(ADAPTERS, /private readonly UplinkSpeechEndDetector _speechEnd = new\(\);/);
  assert.match(ADAPTERS, /bool utteranceEnded = _speechEnd\.Observe\(frame\.PcmS16Le\.Span\);/, "每一帧都过判定器");
  assert.match(ADAPTERS, /PinAsync\(\s*\n\s*"speech-end",/, "原因写明是说完触发");
  assert.match(ADAPTERS, /LastPinFailure = exception\.Message;/, "钉不成要留下原因，不能吞掉");
  // 判定器的门：连续有声才开口、连续无声才说完、太短不算话。
  assert.match(DETECTOR, /internal const int OnsetFrames = 6;/);
  assert.match(DETECTOR, /internal const int ReleaseFrames = 35;/);
  assert.match(DETECTOR, /internal const int MinimumUtteranceFrames = 15;/);
  assert.match(DETECTOR, /internal bool Observe\(ReadOnlySpan<byte> pcmS16Le\)/);
});

test("触发二：HTTP 口 {\"pin\":{\"reason\"}} —— App 打字发送、别的通道都走它", () => {
  assert.match(SERVER, /if \(fields\.SetEquals\(new\[\] \{ "pin" \}\)\)/);
  assert.match(SERVER, /\.PinAsync\(reason, serviceCancellationToken\)/);
  assert.match(SERVER, /candidate\.Length is > 0 and <= 40/, "reason 有长度与字符集限制");
});

test("模型看到的 payload 明说依据：basis=pinned 按钉住时刻算新鲜度，basis=live 注明没有钉", () => {
  assert.match(MCP, /internal static readonly TimeSpan PinnedWindow = TimeSpan\.FromMinutes\(5\);/);
  assert.match(MCP, /ApplyFreshness\(pinned, at\);/, "新鲜度按钉住时刻算，不按现在");
  assert.match(MCP, /pinned\["basis"\] = "pinned";/);
  assert.match(MCP, /live\["basis"\] = "live";/);
  assert.match(MCP, /\["expired"\] = true,/, "过期的钉要说明是过期，不是没有");
  assert.match(MCP, /pinned\["live"\] = DescribeLiveDelta\(pinned, live\);/, "钉住之后变了什么要报");
  assert.match(MCP, /"Read basis first\. basis=pinned means the payload is "/, "工具描述要教模型看 basis");
  assert.match(MCP, /private JsonObject BuildLivePayload\(bool forModel\)/);
});

test("自检覆盖：判定器、协调器钉住、文件落盘", () => {
  assert.match(SELFTEST, /CheckSpeechEndDetector\(checks\)/);
  assert.match(SELFTEST, /CheckSpeechEndPinAsync\(/);
  assert.match(SELFTEST, /CheckSnapshotPinFileAsync\(/);
});

test("双工诊断：AI 出声期间上行有没有人声帧，GET 就能看，自检数过", () => {
  // 用户 2026-09-05："AI 在说话时听不到我"。先给数字，再下结论。
  assert.match(ADAPTERS, /internal JsonObject DuplexDiagnostics\(\)/);
  assert.match(ADAPTERS, /\["uplinkVoicedFramesDuringOutput"\]/);
  assert.match(ADAPTERS, /if \(frame\.Track == DirectPcmTrack\.AppOutput\)\s*\n\s*\{\s*\n\s*_lastAppOutputAtMs = _monotonicMilliseconds\(\);/,
    "AI 出声时刻从下行帧记");
  assert.match(SERVER, /\["duplex"\] = _coordinator\.DuplexDiagnostics\(\),/, "GET 端出去");
  // 方法表不放行 GET 的表现是 404，而处理函数里的 GET 分支看起来完全正常（0.1.288 丢过一次）。
  assert.match(SERVER, /"\/reader-context\/snapshot",\s*\n\s*new\[\] \{ "GET", "POST", "OPTIONS" \},/,
    "路由方法表要放行 GET");
  assert.match(SELFTEST, /duplex-diagnostics-count-uplink-and-voiced-frames/);
});

test("文档说清了触发方式与读取语义", () => {
  assert.match(DOC, /钉住的快照/);
  assert.match(DOC, /speech-end/);
  assert.match(DOC, /basis/);
});
