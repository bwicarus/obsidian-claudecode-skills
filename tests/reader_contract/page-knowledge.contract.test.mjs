// 快照里的「这一页在讲什么」。
//
// 这条链有三段,任何一段漏掉这个字段,结果都是"Pi 侧测试全绿而助手永远看不到":
//   Pi 组装(kg_page_index → build_page_context) → journal → Windows 注入器
// 第三段是显式字段白名单,所以加字段必须两边都改 —— 这正是这个文件存在的理由。
//
// 注:Windows 侧无 .NET SDK,编译验证不了,只能按文本校验结构。这不能替代编译。

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SNAPSHOT = readFileSync(
  join(ROOT, "extensions/bw-reader-webext/windows/ComputerVoiceAudio/DirectContextSnapshot.cs"),
  "utf8",
);
const OUTGOING = readFileSync(
  join(ROOT, "_server_deploy/reader_outgoing_context.py"),
  "utf8",
);
const INDEX = readFileSync(
  join(ROOT, "_server_deploy/kg_page_index.py"),
  "utf8",
);

test("Pi 侧把知识点装进整页上下文", () => {
  assert.match(OUTGOING, /import kg_page_index/,
    "组装点必须真的调索引模块");
  assert.match(OUTGOING, /out\["knowledge"\]\s*=\s*_KG\.knowledge_for_page\(rel, page\)/);
});

test("Pi 侧取不到时仍带字段并说明原因", () => {
  // 少一个字段,上游分不清"这本书没建过图"和"这段代码没跑",
  // 于是会把后者当前者讲出来。
  const block = OUTGOING.slice(OUTGOING.indexOf("import kg_page_index"));
  assert.match(block, /except Exception as ex:/);
  assert.match(block, /"available":\s*False/);
  assert.match(block, /"reason":\s*f"知识图谱不可用/);
});

test("Windows 注入器把这个字段带过去", () => {
  // 注入器是显式白名单。不在这里加一行,字段会被静默丢掉,
  // 而 Pi 侧所有测试仍然全绿。
  assert.match(SNAPSHOT, /CopyKnowledge\(pageContext\["knowledge"\]\)/,
    "BuildPageContext 必须调用 CopyKnowledge");
  assert.match(SNAPSHOT, /next\["knowledge"\]\s*=\s*knowledge;/,
    "结果必须真的挂到快照上");
});

test("字段缺失时不解释——那是旧版 Pi,不是错误", () => {
  const start = SNAPSHOT.indexOf("private static JsonObject? CopyKnowledge(");
  assert.ok(start > 0, "找不到 CopyKnowledge");
  const body = SNAPSHOT.slice(start, start + 3000);
  assert.match(body, /if \(node is null\)\s*\{\s*return null;/,
    "null 要返回 null,而不是一条'不可用'的说明");
});

test("格式不合只丢这个字段,不丢整页正文", () => {
  const start = SNAPSHOT.indexOf("private static JsonObject? CopyKnowledge(");
  const body = SNAPSHOT.slice(start, start + 3000);
  assert.doesNotMatch(body, /throw JournalInvalid\(\)/,
    "正文是主线、知识点是增强:为一个增强字段丢掉整页正文,代价远大于收益");
  assert.match(body, /KnowledgeUnavailable\("知识图谱字段格式不正确"\)/,
    "但不能静默——换上的说明要一路带到助手那里");
});

test("节点内容逐字段校验,不原样透传", () => {
  const start = SNAPSHOT.indexOf("private static JsonObject? CopyKnowledgeNode(");
  assert.ok(start > 0, "找不到 CopyKnowledgeNode");
  const body = SNAPSHOT.slice(start, start + 1400);
  assert.doesNotMatch(body, /DeepClone/,
    "journal 来自网络,不能整段克隆");
  for (const field of ["id", "type", "summary"]) {
    assert.ok(body.includes(`"${field}"`), `缺字段 ${field}`);
  }
  assert.match(body, /name is null \|\| string\.IsNullOrWhiteSpace\(name\)|IsNullOrWhiteSpace\(name\)/,
    "没有名字的节点没有意义");
});

test("文本有长度与控制字符上限", () => {
  const start = SNAPSHOT.indexOf("private static string? SafeKnowledgeText(");
  assert.ok(start > 0, "找不到 SafeKnowledgeText");
  const body = SNAPSHOT.slice(start, start + 700);
  assert.match(body, /text\.Length > limit/);
  assert.match(body, /text\.Any\(char\.IsControl\)/);
});

test("两侧都限制节点数量,且 Windows 侧不比 Pi 侧宽松地无声接受", () => {
  const pi = /MAX_CONCEPTS_PER_PAGE = (\d+)/.exec(INDEX);
  assert.notEqual(pi, null, "Pi 侧找不到上限");
  const win = /KnowledgeConceptLimit = (\d+)/.exec(SNAPSHOT);
  assert.notEqual(win, null, "Windows 侧找不到上限");
  assert.ok(
    Number(win[1]) >= Number(pi[1]),
    `Windows 上限 ${win[1]} 必须 >= Pi 上限 ${pi[1]} —— `
      + "否则 Pi 发出来的合法快照会在这边被判成不可用",
  );
});

test("摘要要标明不是原文", () => {
  assert.match(INDEX, /不是本页原文/,
    "助手会拿概括当引用,而用户翻到那页会发现书上没有这句话");
  assert.match(SNAPSHOT, /result\["note"\] = note;/,
    "这句说明必须被带过去,否则等于没写");
});

test("掌握度不进这条通道", () => {
  // 快照是"这页讲什么",不是"你学得怎样"。掌握度另有权威来源,
  // 混进来会让助手拿一份没人保证新鲜的学习状态下断言。
  const start = INDEX.indexOf("def _trim(");
  const body = INDEX.slice(start, start + 800);
  for (const leaked of ["mastery", "state", "containing_notes"]) {
    assert.ok(!body.includes(`"${leaked}"`), `_trim 不该带出 ${leaked}`);
  }
});

test("匹配不到唯一一本时弃权", () => {
  assert.match(INDEX, /if len\(matches\) == 1:/,
    "必须唯一命中才用——对错书比不给更糟,而用户看不出张冠李戴");
  assert.match(INDEX, /无法确定是哪一张/);
});
