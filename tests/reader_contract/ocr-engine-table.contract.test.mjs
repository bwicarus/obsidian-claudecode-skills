import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const SERVER = read("_server_deploy/reader_book_ocr.py");
const WORKER = read("_server_deploy/reader_book_ocr_worker.py");
const PC_WORKER = read("scripts/reader_pc_preprocess_worker.py");
const PI_OCR = read("ios/BWReader/App/ReaderPiBookOCR.swift");
const STORE = read("ios/BWReader/App/NativeBookOCRStore.swift");
const MODELS = read("ios/BWReader/App/NativeBookOCRModels.swift");
const LIBRARY = read("ios/BWReader/App/ReaderLocalLibraryView.swift");

// 预处理引擎表（2026-09-06 加 native：有文字层的书不 OCR、只分词）。
// 这张表在十一处各有一份副本：少改一处的表现是"服务器接受了、App 拒收"或"按钮有、
// 服务器 400"，每一处各自都自洽（CLAUDE.md「先数清楚有几份副本」）。

test("服务端：ENGINES 含 native，布局校验认 textSource=native", () => {
  assert.match(SERVER, /ENGINES = frozenset\(\("vision", "manga", "native"\)\)/);
  assert.match(SERVER, /layout\.get\("textSource"\) not in \{"vision", "native", "unavailable"\}/);
});

test("Pi worker：CLI 认 native，逐页分支读字符层再分词，页标 tokenized", () => {
  assert.match(WORKER, /parser\.add_argument\("--engine", choices=\("vision", "manga", "native"\)\)/);
  assert.match(WORKER, /def _native_page\(page\)/);
  assert.match(WORKER, /elif args\.engine == "native":\s*\n[\s\S]{0,200}_native_page\(page\)/);
  assert.match(WORKER, /layout\["textSource"\] = "native"/);
  assert.match(WORKER, /if args\.engine == "native":\s*\n[\s\S]{0,160}sidecar\["tokenized"\] = True/);
  // 字符结构与 Vision 页一致；w 先置 -1（App 侧必填）由分词覆盖。
  assert.match(WORKER, /"w": -1,\s*\n\s*"b": 1 if bold else 0,\s*\n\s*"bk": block_index,/);
});

test("PC worker：能力表、claim 校验、逐页分支三处都认 native，复用 core._native_page", () => {
  assert.match(PC_WORKER, /if engine not in \("vision", "manga", "native"\):/);
  assert.match(PC_WORKER, /if engine in \("vision", "manga", "native"\)\)/);
  assert.match(PC_WORKER, /elif claim\.engine == "native":\s*\n[\s\S]{0,300}core\._native_page\(page\)/);
  assert.match(PC_WORKER, /default=os\.environ\.get\("BW_READER_PC_OCR_ENGINES", "vision,manga,native"\)/,
    "默认能力表要带 native，否则 PC 执行器不会认领这类任务");
});

test("App：五处白名单、几何精确判定、布局枚举、两个按钮都有 native", () => {
  assert.match(PI_OCR, /guard \["vision", "manga", "native"\]\.contains\(engine\),/);
  assert.match(PI_OCR, /isSubset\(of: Set\(\["vision", "manga", "native"\]\)\)/);
  assert.equal((PI_OCR.match(/\["vision", "manga", "native", "legacy"\]/g) ?? []).length, 3, "结果引擎白名单三处");
  assert.doesNotMatch(PI_OCR, /\["vision", "manga", "legacy"\]/, "不许留旧表");
  assert.match(STORE, /\["vision", "manga", "native"\]\.contains\(value\.engine\)/);
  assert.match(STORE, /\|\| value\.engine == "native"/, "字符层坐标本来就是精确的");
  assert.match(STORE, /\["vision", "manga", "native", "legacy"\]\.contains\(\$0\)/);
  assert.match(MODELS, /enum NativeBookOCRPageLayoutTextSource[\s\S]{0,200}case native/);
  assert.equal((LIBRARY.match(/engine: "native",/g) ?? []).length, 2, "普通与「重新跑一份」两处按钮");
  assert.match(LIBRARY, /文字层 PDF（不 OCR，只分词）/);
});
