// 复习模式进上下文快照的合同。
//
// 语义:active-reading 的 review 字段**缺席 = 未进入复习模式**。旧构建不发
// 这个字段,缺席就是今天的行为;所以链上任何一层都不许造出"空的 review"。
// 在场时形状是白名单重建(App 侧 localReviewSnapshot)+ 整条拒绝
// (Windows 侧 ValidateReviewState)—— 一个越界字段会让位置和选中一起陪葬。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const read = (relative) => readFileSync(join(root, relative), "utf8");

const REVIEW = read("_server_deploy/static/pdf/rc-review.js");
const VOICE = read("_server_deploy/static/pdf/rc-computer-voice.js");
const SNAPSHOT = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/DirectContextSnapshot.cs");
const PRESENTATION = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/DirectSnapshotPresentation.cs");

test("rc-review 导出只读投影,非复习模式返回 null 而不是空对象", () => {
  const at = REVIEW.indexOf("snapshotState: function () {");
  assert.ok(at >= 0, "RC.review.snapshotState 存在");
  const body = REVIEW.slice(at, at + 900);
  assert.match(body, /if \(!_mode\) return null;/,
    "缺席语义由源头保证:未进入复习模式时没有投影可言");
  assert.match(body, /_queue\.slice\(0, 200\)/, "队列限量");
  assert.match(body, /_stableCardId\(card\)\)\.slice\(0, 120\)/, "编号限长");
  assert.match(body, /\.slice\(0, 2000\)/, "卡片正文限长");
});

function extractReviewProjection() {
  const start = VOICE.indexOf("var REVIEW_CARD_ID_RE");
  const end = VOICE.indexOf("function localHighlightTarget", start);
  assert.ok(start >= 0 && end > start, "投影代码块存在");
  const factory = new Function(
    "RC",
    "plainObject",
    VOICE.slice(start, end) + "\nreturn localReviewSnapshot;");
  return (snapshotState) => factory(
    { review: { snapshotState } },
    function plainObject(value) {
      return !!value && typeof value === "object" &&
        !Array.isArray(value) &&
        Object.prototype.toString.call(value) === "[object Object]";
    },
  )();
}

test("非复习模式与异常都折成缺席,不折成空 review", () => {
  const project = extractReviewProjection();
  assert.equal(project(() => null), null);
  assert.equal(project(() => { throw new Error("boom"); }), null);
  assert.equal(project(() => "not-an-object"), null);
});

test("合法投影按白名单重建:clamp、过滤非法编号、清洗正文", () => {
  const project = extractReviewProjection();
  const built = project(() => ({
    dueTotal: 7,
    index: 2,
    queueIds: ["anki_card_123", "bad id!", "anki_note_9"],
    showingAnswer: "truthy-but-not-true",
    current: {
      id: "anki_card_123",
      front: "问题\u0007带铃铛\r\n第二行",
      back: "答案\t保留制表",
    },
    extraField: "must not survive",
  }));
  assert.deepEqual(built, {
    dueTotal: 7,
    index: 2,
    queueIds: ["anki_card_123", "anki_note_9"],
    showingAnswer: false,
    current: {
      id: "anki_card_123",
      front: "问题带铃铛\n第二行",
      back: "答案\t保留制表",
    },
  });
});

test("坏的计数整条不带;坏的当前卡只丢 current 不丢投影", () => {
  const project = extractReviewProjection();
  assert.equal(project(() => ({
    dueTotal: -1, index: 0, queueIds: [], showingAnswer: false,
  })), null);
  assert.equal(project(() => ({
    dueTotal: 0, index: 0.5, queueIds: [], showingAnswer: false,
  })), null);
  const noCurrent = project(() => ({
    dueTotal: 3, index: 0, queueIds: [], showingAnswer: true,
    current: { id: "bad id!", front: "x", back: "y" },
  }));
  assert.deepEqual(noCurrent, {
    dueTotal: 3, index: 0, queueIds: [], showingAnswer: true,
  });
});

test("投影挂进 active-reading 组装,且只在在场时挂", () => {
  assert.match(VOICE,
    /var review = localReviewSnapshot\(\);\s*\n\s*if \(review\) \{\s*\n\s*activeReading\.review = review;/);
});

test("Windows 白名单放行 review 并整形校验", () => {
  assert.match(SNAPSHOT, /\.Append\("review"\)/, "allow-set 放行");
  const at = SNAPSHOT.indexOf(
    "private static JsonElement ValidateReviewState(JsonElement value)");
  assert.ok(at >= 0, "校验函数存在");
  const body = SNAPSHOT.slice(at, at + 4200);
  assert.match(body, /"dueTotal",\s*\n\s*"index",\s*\n\s*"queueIds",\s*\n\s*"showingAnswer",/,
    "必需键清单");
  assert.match(body, /\.Append\("current"\)/, "current 是唯一可选键");
  assert.match(body, /keys\.Any\(key => !allowedKeys\.Contains\(key\)\)/,
    "越界键整条拒绝");
  assert.match(body, /queueCount > 200/, "队列限量与 App 侧一致");
  assert.match(body, /DirectBridgeContract\.IsSafeId\(entryId\)/, "编号形状");
  assert.match(body, /text\.Length > 2000/, "正文限长与 App 侧一致");
  assert.match(body, /character is not \('\\n' or '\\t'\)/,
    "控制字符纪律与 selectionContext 相同");
});

test("review 不进连续性保留名单 —— 缺席必须表示已退出复习", () => {
  const at = SNAPSHOT.indexOf(
    "private static void PreserveActiveReadingContinuity(");
  const end = SNAPSHOT.indexOf("private static bool WebSourceDiffers(", at);
  assert.ok(at >= 0 && end > at);
  assert.ok(!SNAPSHOT.slice(at, end).includes("review"),
    "一个复活的旧 review 会让 AI 以为用户还停在复习里");
});

test("三处呈现都表达「未进入复习模式」的缺席语义", () => {
  assert.match(PRESENTATION, /_未进入复习模式。_/, "markdown 投影");
  assert.match(PRESENTATION, /未进入复习模式。/, "终端投影");
  assert.match(PRESENTATION, /复习模式：未进入复习模式/, "HTML 实时页");
  assert.match(PRESENTATION, /待复习数量/, "在场时给出数量");
  assert.match(PRESENTATION, /队列卡片编号/, "在场时给出编号清单");
});
