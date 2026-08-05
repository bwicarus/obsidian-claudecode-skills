import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const SWIFT = readFileSync(
  new URL("ios/BWReader/App/NativeFormulaRecognition.swift", ROOT),
  "utf8",
);
const TOOLS = readFileSync(
  new URL("ios/BWReader/App/NativeReaderToolsView.swift", ROOT),
  "utf8",
);

test("native formula action detects missing boxes before starting LaTeX OCR", () => {
  const noBoxes = SWIFT.indexOf('value["error"] as? String == "no_boxes"');
  const detector = SWIFT.indexOf("/pdf/api/book-figures", noBoxes);
  assert.ok(noBoxes >= 0 && detector > noBoxes);
  assert.match(SWIFT, /detectingBoxes: true/);
  assert.match(TOOLS, /现有 DocLayout 模型预处理/);
  assert.match(TOOLS, /Core ML 版会逐页读取已下载书籍的本地页图/);
  assert.match(TOOLS, /不会重复下载整本书/);
});
