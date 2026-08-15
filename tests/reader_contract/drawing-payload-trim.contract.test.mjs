// 绘图状态给模型的那份要精简，但精简过的那份绝不能被内部逻辑读到。
//
// BuildToolPayload 有九个调用点，核实过全部九处：只有 reader_context_snapshot
// 那一处真正把结果交给模型；其余八处是内部逻辑 —— 构造取图请求要读
// drawingRevision/inProgress/empty，校验请求是否仍然有效也要读。喂给它们
// 精简过的投影会让取图静默失效(字段缺失读成 null，判断自然不成立，且不报错)。
//
// 所以默认值必须是「不精简」，精简必须是调用方显式选择。这个文件钉住的正是
// 这条不对称：新增调用点忘了想这件事时,默认值要保底选安全的那边。

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { join } from "node:path";

const ROOT = join(fileURLToPath(new URL("../..", import.meta.url)));
const SRC = readFileSync(
  join(ROOT, "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderContextMcpServer.cs"),
  "utf8",
);

test("BuildToolPayload 默认不精简", () => {
  assert.match(
    SRC,
    /private JsonObject BuildToolPayload\(bool forModel = false\)/,
    "默认值必须是 false —— 九处调用点里八处是内部逻辑，喂给精简过的投影会让取图静默失效",
  );
});

test("只有一处显式传 forModel: true", () => {
  const hits = [...SRC.matchAll(/BuildToolPayload\(\s*forModel:\s*true\s*\)/g)];
  assert.equal(hits.length, 1,
    `期望恰好一处显式精简，实际 ${hits.length} 处。新增调用点前想清楚它是不是那个真正交给模型的地方`);
});

test("精简排在 BuildVisualAccess 之后", () => {
  // BuildVisualAccess 要读 stable/inProgress/empty/drawingRevision 去算
  // scopes；顺序反了它会拿到精简后缺字段的投影。
  const at = SRC.indexOf("TrimDrawingForModel(snapshot)");
  assert.ok(at > 0, "找不到 TrimDrawingForModel 的调用");
  const before = SRC.slice(0, at);
  const lastVisualAccess = before.lastIndexOf('snapshot["visualAccess"] = BuildVisualAccess(');
  assert.ok(
    lastVisualAccess > 0 && at - lastVisualAccess < 400,
    "TrimDrawingForModel 必须紧跟在 BuildVisualAccess 之后",
  );
});

test("精简函数只在 forModel 分支里被调用", () => {
  const start = SRC.indexOf('if (forModel)');
  const end = SRC.indexOf("return snapshot;", start);
  assert.ok(start > 0 && end > start, "找不到 forModel 分支");
  const body = SRC.slice(start, end);
  assert.match(body, /TrimDrawingForModel\(snapshot\)/);
});

function trimBody() {
  const start = SRC.indexOf("private static void TrimDrawingForModel(");
  assert.ok(start > 0, "找不到 TrimDrawingForModel 定义");
  const end = SRC.indexOf("\n    private JsonObject BuildToolPayload", start);
  assert.ok(end > start, "找不到函数结尾");
  return SRC.slice(start, end);
}

test("hasInk 以 empty 为唯一事实来源，不读 has_ink", () => {
  const body = trimBody();
  assert.match(body, /drawing\["empty"\]/);
  assert.doesNotMatch(body, /drawing\["has_ink"\]/,
    "has_ink 被校验强制等于 !empty，两个字段说同一句话，读哪个都行但只能读一个");
});

test("revision 未稳定时保留 null，不整体丢弃", () => {
  // 模型要能看出「有图但还不能取」，而不是「没有 drawing 这回事」。
  const body = trimBody();
  assert.match(body, /trimmed\["revision"\] = drawing\["drawingRevision"\]\?\.DeepClone\(\);/);
});

test("noise 字段确实被排除在精简结果之外", () => {
  const body = trimBody();
  for (const noise of ["contract", "freshWindowS", "pendingSince", "\"ref\"", "\"file\"", "\"page\""]) {
    assert.ok(
      !body.includes(`trimmed[${noise}]`) && !body.includes(`trimmed["${noise.replace(/"/g, "")}"]`),
      `${noise} 不该出现在精简结果里`,
    );
  }
});

test("has_ink 从 visual 层移除，不是留着两份", () => {
  const body = trimBody();
  assert.match(body, /visual\.Remove\("has_ink"\)/);
});
