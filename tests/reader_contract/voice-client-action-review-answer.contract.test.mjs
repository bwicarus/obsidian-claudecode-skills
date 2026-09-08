import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import path from "node:path";
import { fileURLToPath } from "node:url";

// 语音复习代评分（2026-09-09 用户：「根据我回答的结果 ai 来判断掌握程度后录入」）。
//
// ⚠ `voice-client-action` 这张白名单有**四处副本而一个契约测试都没有**
//    （scripts/contract_sites.py 明说「改动后没有任何东西会红」）。
//    这份补上其中一条动作的四处一致性 —— 少一处的表现是：
//    桥放行了、阅读器不认，或者反过来，而两种都不会有人报错。
const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), "utf8");

const FN = "_nativeReaderReviewAnswer";
const SITES = {
  "跨机信封校验": "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderRealtimeOutput.cs",
  "阅读器入站闸": "_server_deploy/static/pdf/rc-computer-voice.js",
  "执行映射": "_server_deploy/static/pdf/rc-voicecall.js",
  "扩展自带副本": "extensions/bw-reader-webext/vendor/rc-computer-voice.js",
};

test("四处站点都认识这个动作", () => {
  for (const [label, rel] of Object.entries(SITES)) {
    assert.ok(read(rel).includes(FN), `${label}（${rel}）里没有 ${FN}`);
  }
});

test("实现挂在受信入口上，而不是让桥去 window 上找函数", () => {
  const runtime = read("_server_deploy/static/pdf/native-local-runtime.js");
  assert.match(runtime, /root\._nativeReaderReviewAnswer = function/);
  assert.match(runtime, /reviewAnswer: nativeReaderReviewAnswer/);
  const call = read("_server_deploy/static/pdf/rc-voicecall.js");
  // 执行侧必须是**显式取表**，不能 window[fn] —— 后者把一条跨进程消息
  // 升级成"调用页面任意函数"。
  assert.ok(call.includes("_caTarget = window._nativeReaderReviewAnswer"));
});

test("ease 只认 1..4，每一处都要卡", () => {
  // 一处漏掉就等于没卡：桥放行 0 或 9，阅读器那边 Number 转出来照样往下走。
  for (const rel of [
    "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderRealtimeOutput.cs",
    "_server_deploy/static/pdf/rc-computer-voice.js",
    "_server_deploy/static/pdf/rc-voicecall.js",
    "_server_deploy/static/pdf/native-local-runtime.js",
  ]) {
    const source = read(rel);
    assert.ok(
      /ease < 1 \|\| ease > 4/.test(source) ||
        /Ease < 1 \|\| _caEase > 4/.test(source) ||
        /ratingEase < 1 \|\| ratingEase > 4/.test(source) ||
        /ease < 1 \|\| ease > 4/i.test(source),
      `${rel} 没有把 ease 卡在 1..4`,
    );
  }
});

test("cardId 是互锁：实现必须比对当前卡，对不上就拒绝", () => {
  const runtime = read("_server_deploy/static/pdf/native-local-runtime.js");
  const start = runtime.indexOf("function nativeReaderReviewAnswer");
  assert.ok(start > 0);
  const body = runtime.slice(start, runtime.indexOf("\n  var api = {", start));
  // AI 念完那张、等用户答完再评分，中间卡可能已经翻过去了。
  assert.match(body, /actual !== expected/);
  assert.match(body, /BW_REVIEW_ANSWER_CARD_CHANGED/);
  // 拒绝时要说出**实际是哪一张**，否则 AI 无从判断该重念还是重试。
  assert.match(body, /BW_REVIEW_ANSWER_CARD_CHANGED[^\n]*actual/);
});

test("评分走 RC.review.answer，不自己实现一套调度", () => {
  const runtime = read("_server_deploy/static/pdf/native-local-runtime.js");
  const start = runtime.indexOf("function nativeReaderReviewAnswer");
  const body = runtime.slice(start, runtime.indexOf("\n  var api = {", start));
  assert.match(body, /review\.answer\(ease\)/);
  // answer() 要求答案已揭示，跟人手动复习一样
  assert.match(body, /review\.show\(\)/);
  // 不该出现绕过去的写法。查的是"另打一次请求"和"自己改排期"，
  // 不是字面量 —— 返回契约名里本来就含 review-answer。
  assert.ok(!/fetch\(/.test(body), "不要在这里另打一次评分请求");
  assert.ok(!/_next\s*=/.test(body), "不要在这里自己改卡的排期状态");
  assert.ok(!/answerCards/.test(body), "不要在这里直接调 AnkiConnect");
});

test("不在复习模式时明确失败，而不是静默什么都不做", () => {
  const runtime = read("_server_deploy/static/pdf/native-local-runtime.js");
  const start = runtime.indexOf("function nativeReaderReviewAnswer");
  const body = runtime.slice(start, runtime.indexOf("\n  var api = {", start));
  assert.match(body, /BW_REVIEW_ANSWER_NOT_IN_REVIEW/);
  assert.match(body, /BW_REVIEW_ANSWER_NO_CARD/);
});

test("入站闸重建参数而不是透传", () => {
  const gate = read("_server_deploy/static/pdf/rc-computer-voice.js");
  const start = gate.indexOf('actionFn === "_nativeReaderReviewAnswer"');
  assert.ok(start > 0);
  const body = gate.slice(start, start + 1600);
  // 多一个字段就该被丢掉，而不是跟着过桥
  assert.match(body, /ratingKeys\.length !== 2/);
  assert.match(body, /args: \[\{ ease: ratingEase, cardId: ratingCardId \}\]/);
});

test("AI 能调到它：MCP 工具存在且写明 cardId 是互锁", () => {
  const mcp = read(
    "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderContextMcpServer.cs",
  );
  assert.match(mcp, /ReviewAnswerToolName = "reader_review_answer"/);
  assert.ok(mcp.includes('["fn"] = "_nativeReaderReviewAnswer"'));
  // 面向 AI 的说明写反比没写更糟：这里要说清 cardId 为什么不能随便填
  assert.match(mcp, /interlock, not a formality/);
});
