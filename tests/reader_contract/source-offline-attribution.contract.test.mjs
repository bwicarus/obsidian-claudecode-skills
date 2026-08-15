// 「来源不在线」必须说清是哪一种不在线。
//
// 快照里带着来源标识、内容也在正常上报,取图却答「来源不在线」。这句话对两种
// 完全不同的故障一视同仁:
//
//   桥上一个来源都没有 → 那个页面从未完成 visual-register(连接没建或握手没走完)
//   桥上有别的来源     → 注册是通的,但注册下来的跟快照报的不是同一个
//
// 前者要去查扩展侧的连接与认领,后者要去查两条路的标识为何不一致。分不清就
// 只能靠猜,而桥自己一直知道答案。
//
// 注:本机只有 .NET runtime 无 SDK,这是文本校验,不能替代编译。

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const DIR = join(
  dirname(fileURLToPath(import.meta.url)),
  "..", "..",
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio",
);
const VISUAL = readFileSync(join(DIR, "ReaderVisualDelivery.cs"), "utf8");
const OUTPUT = readFileSync(join(DIR, "ReaderRealtimeOutput.cs"), "utf8");
const QUERY = readFileSync(join(DIR, "ReaderQuery.cs"), "utf8");

function describeBody() {
  const start = VISUAL.indexOf("internal string DescribeRegisteredSources()");
  assert.ok(start > 0, "找不到 DescribeRegisteredSources");
  // 切到下一个成员为止。固定长度会越过函数边界读到邻居的代码,
  // 于是「函数里有没有加锁」这种断言会被隔壁的锁满足 —— 那是一条
  // 永远为真的断言,比没有断言更坏。
  const rest = VISUAL.slice(start + 10);
  const end = rest.search(/\n    (internal|private|public)\s/);
  assert.ok(end > 0, "找不到函数结尾");
  return VISUAL.slice(start, start + 10 + end);
}

test("空与非空分开说", () => {
  const body = describeBody();
  assert.match(body, /_sources\.Count == 0/);
  assert.match(body, /没有任何已注册的来源/,
    "一个都没有 → 去查连接与认领");
  assert.match(body, /已注册 \{_sources\.Count\} 个来源|已注册 \{/,
    "有别的 → 去查标识为何不一致");
});

test("只回前缀,不把完整标识写进错误里", () => {
  // 错误会流向模型再流向用户。区分是谁只需要前几位。
  const body = describeBody();
  assert.match(body, /key\[\.\.8\]/, "应只取前 8 位");
  assert.match(body, /\.Take\(/, "数量也要有上限");
});

test("读取时持锁", () => {
  // _sources 由别的线程写。不持锁读会在正好掉线的那一刻抛出,
  // 于是诊断自己变成第二个故障。
  const body = describeBody();
  assert.match(body, /lock \(_gate\)/);
});

test("三条通道的同一处判断都带上归因", () => {
  // 三个 broker 各自 TryGetLease 失败后都抛「不在线」。只改一处的话,
  // 下次从另一条通道遇到时又回到没有线索的状态。
  for (const [name, source, code] of [
    ["visual", VISUAL, "BW_READER_VISUAL_SOURCE_OFFLINE"],
    ["output", OUTPUT, "BW_READER_REALTIME_OUTPUT_SOURCE_OFFLINE"],
    ["query", QUERY, "BW_READER_QUERY_SOURCE_OFFLINE"],
  ]) {
    const at = source.indexOf(code);
    assert.ok(at > 0, `${name}: 找不到 ${code}`);
    const around = source.slice(at, at + 420);
    assert.match(around, /DescribeRegisteredSources\(\)/,
      `${name} 的首次判断没有带上归因`);
  }
});

test("写回通道同时保留等待时长", () => {
  // 等待与归因回答的是不同的问题:等了多久 vs 桥上到底有什么。
  // 加归因时不该把等待时长挤掉。
  const at = OUTPUT.indexOf("BW_READER_REALTIME_OUTPUT_SOURCE_OFFLINE");
  const around = OUTPUT.slice(at, at + 420);
  assert.match(around, /已等待/);
  assert.match(around, /DescribeRegisteredSources/);
});

test("发出之后的离线不加归因", () => {
  // 那时请求已经上线、结果未知,问题不再是「有没有注册」。
  // 在那里贴一份注册表只会把注意力引向已经排除掉的方向。
  const first = VISUAL.indexOf("BW_READER_VISUAL_SOURCE_OFFLINE");
  const second = VISUAL.indexOf("BW_READER_VISUAL_SOURCE_OFFLINE", first + 10);
  assert.ok(second > first, "应有第二处(取图期间掉线)");
  const around = VISUAL.slice(second, second + 260);
  assert.doesNotMatch(around, /DescribeRegisteredSources/);
});
