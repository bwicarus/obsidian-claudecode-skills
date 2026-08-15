// 刷新页面后，网页上已有的高亮与固定卡片必须回来。
//
// 症状是「刷新后卡片消失」，根因不在存储：MV3 的 service worker 会休眠，
// 页面重载时 content script 立刻 sendMessage，worker 若正在冷启动就带着
// chrome.runtime.lastError 回来。两个模块都写着 `.catch(() => {})`，
// 于是这一次读失败被吞掉，页面空着，数据其实还在磁盘上。
//
// 用户看到的是「我固定的卡片被删了」，真相是「这一次没读到」——
// 两者的处理完全不同：一个是重试，一个是重新做一张。所以：
//   · 短暂失败必须重试（冷启动是暂时的）
//   · 重试用尽必须出声（无声的空页面没有任何线索可查）
import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const PINS = readFileSync(
  new URL("extensions/bw-reader-webext/src/web-pins.js", ROOT), "utf8");
const HIGHLIGHTS = readFileSync(
  new URL("extensions/bw-reader-webext/src/web-highlights.js", ROOT), "utf8");

const CASES = [
  { name: "固定卡片", source: PINS, restore: "restorePins" },
  { name: "网页高亮", source: HIGHLIGHTS, restore: "restoreHighlights" },
];

for (const { name, source, restore } of CASES) {
  test(`${name}：载入失败会重试，不是一次就放弃`, () => {
    assert.match(source, new RegExp(`function ${restore}\\(attempt\\)`),
      "要有一个带尝试次数的恢复函数");
    assert.match(source, /attempt\s*<\s*\w*RESTORE_ATTEMPTS/,
      "必须有重试上限，不能无限重试");
    assert.match(source, new RegExp(`${restore}\\(attempt\\s*\\+\\s*1\\)`),
      "失败后要递增重试");
    assert.match(source, /setTimeout\([^)]*BACKOFF_MS\s*\*\s*attempt/,
      "退避要随尝试次数增长，避免在冷启动期间连打");
  });

  test(`${name}：重试用尽必须出声`, () => {
    const tail = source.slice(source.indexOf(`function ${restore}(`));
    assert.match(tail, /console\.warn/, "至少留下可查的日志");
    assert.match(tail, /RC\.toast\?\./,
      "设备上没有控制台，用户必须在界面上看到");
    assert.match(tail, /载入失败|未能载入/,
      "提示要说明是「没读到」，而不是含糊的出错");
  });

  test(`${name}：不再存在吞掉一切的空 catch`, () => {
    // 这正是原来的样子：load().catch(() => {})
    const code = source.replace(/\/\/[^\n]*/g, "");   // 去掉注释里的引用
    assert.doesNotMatch(
      code, /\.catch\(\s*\(\s*\)\s*=>\s*\{\s*\}\s*\)/,
      "空 catch 会让「这次没读到」和「本来就没有」长得一模一样",
    );
  });

  test(`${name}：恢复被真正调用`, () => {
    // 只定义不调用的话，页面永远是空的，而所有断言都还是绿的。
    assert.match(source, new RegExp(`\\n\\s*${restore}\\(1\\);`),
      "恢复流程必须真的启动");
  });
}

test("两个模块用同一套恢复语义", () => {
  // 同一个根因分散在两处，若各写各的，下次只会修好一处。
  for (const source of [PINS, HIGHLIGHTS]) {
    assert.match(source, /RESTORE_ATTEMPTS\s*=\s*3/);
    assert.match(source, /RESTORE_BACKOFF_MS\s*=\s*120/);
  }
});

test("失败路径不会清空已有数据", () => {
  // 最坏的修法是把失败当成「读到了空」然后覆盖写回去 ——
  // 那会把磁盘上真实存在的卡片抹掉，且不可逆。
  const tail = PINS.slice(PINS.indexOf("function restorePins("));
  const failureBranch = tail.slice(tail.indexOf(".catch("));
  assert.doesNotMatch(failureBranch, /persist\(\)/,
    "失败分支绝不能触发写回：那会用空数据覆盖真实数据");
});
