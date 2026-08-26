// 网页钉页卡的「锚定到正文」按钮（2026-08-27）：对齐 App 自由便签卡的 ⚓。
// 链路 = 选区控制器(web-bind) → page-chars(page=1) → persistBoundCard 共享层。
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const root = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const PINS = readFileSync(
  join(root, "extensions/bw-reader-webext/src/web-pins.js"), "utf8");

test("钉页卡带锚定按钮,走共享落库层,失败不删卡", () => {
  const at = PINS.indexOf("bw-pin-anchor");
  assert.ok(at >= 0, "锚定按钮在场");
  const body = PINS.slice(at, at + 2600);
  assert.match(body, /__bwSelectionController\?\.current/,
    "锚来自 web-bind 的选区控制器 —— 与 App 的锁定选区同一语义");
  assert.match(body, /persistBoundCard/,
    "落库必须走共享层(仓库写入/幂等/tombstone),不得另造存储");
  assert.match(body, /请先选中网页正文的一段文字/,
    "无选区要明确提示,不猜位置");
  // 成功才转移;失败路径绝不能先删卡再报错 —— 断言删除动作在 ok===true 分支里。
  const okBranch = body.indexOf("res.ok===true");
  const removal = body.indexOf("pins=pins.filter(q=>q.id!==p.id);box.remove()");
  assert.ok(okBranch >= 0 && removal > okBranch,
    "钉页实例的删除必须在持久化成功之后");
});
