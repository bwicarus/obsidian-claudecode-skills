// 两条 2026-09-05 用户实锤的 bug，各自钉住行为而不是钉字面量。
//
// ① 句子划分停在行尾：漫画气泡里「これらの問題を…総合的に」没有接上下一行
//    「そして平等に解決していこうってわけだ」。原因是 `_paraGap`（行距 > 1.5 倍字高）
//    在气泡里天天成立 —— OCR 字符盒紧贴字形，而漫画行距本来就宽。
//    `bk` 在这条链上就是段落/气泡（mokuro 的块=气泡，Vision 替换的字符继承它的 bk），
//    所以同一块内的换行永远不是段落边界。
//
// ② 「栄養」已掌握，却在「養」下面单独画了一条下划线：分词把它切成 栄|養，
//    而「養」是 2026-05-31 单查过一次的独立词条（state/jp-vocab.json 实锤）。
//    下划线链路完全信任分词边界，于是一个汉字落在更长的、用户已掌握的词里也照画。

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const SOURCE = read("_server_deploy/static/pdf/reader.src/13-selection.js");
const BUILT = read("_server_deploy/static/pdf/reader.js");
const RUNTIME = read("_server_deploy/static/pdf/native-local-runtime.js");
const SERVER = read("_server_deploy/pdf_reader.py");

/// 把 `_expandSentenceFromRange` 从源码里抠出来真跑 —— 这条规则全是数值判断，
/// 钉字面量拦不住"改了阈值但没改行为"的回归。
function loadSentenceExpander(source) {
  const start = source.indexOf("function _expandSentenceFromRange(");
  assert.ok(start >= 0, "找不到 _expandSentenceFromRange");
  const after = source.indexOf("\nfunction ", start + 1);
  const body = source.slice(start, after < 0 ? undefined : after);
  // eslint-disable-next-line no-new-func
  return new Function(`${body}; return _expandSentenceFromRange;`)();
}

/// 造一段 OCR 风格的字符：每行 `perLine` 个字，行距 = pitch × height。
function makeChars(lines, { height = 20, pitch = 1.6 } = {}) {
  const chars = [];
  lines.forEach((line, lineIndex) => {
    [...line.text].forEach((ch, columnIndex) => {
      chars.push({
        c: ch,
        bk: line.bk,
        top: lineIndex * height * pitch,
        height,
        left: columnIndex * height,
        width: height,
      });
    });
  });
  return chars;
}

test("同一块内的换行不断句：气泡里的三行是一句话", () => {
  const expand = loadSentenceExpander(SOURCE);
  const lines = [
    { bk: 3, text: "主な活動項目だけど" },
    { bk: 3, text: "これらの問題を地域住民が自分たちで総合的に" },
    { bk: 3, text: "そして平等に解決していこうってわけだ" },
  ];
  const chars = makeChars(lines);
  // 从第二行中间点一个字（用户点的就是这一行）
  const second = lines[0].text.length + 3;
  const range = expand(chars, second, second);
  const text = chars.slice(range.start, range.end + 1).map((c) => c.c).join("");
  assert.ok(
    text.includes("そして平等に解決していこうってわけだ"),
    "句子必须接上下一行，实际取到：" + text);
  assert.ok(text.includes("主な活動項目だけど"), "同块的上一行也属于这句");
});

test("换了块又换了行才断：标题不会被并进正文那句", () => {
  const expand = loadSentenceExpander(SOURCE);
  const chars = makeChars([
    { bk: 1, text: "見出しだけの行" },
    { bk: 2, text: "本文はここから始まる" },
    { bk: 2, text: "そして次の行に続く" },
  ]);
  const at = "見出しだけの行".length + 2;
  const range = expand(chars, at, at);
  const text = chars.slice(range.start, range.end + 1).map((c) => c.c).join("");
  assert.ok(!text.includes("見出し"), "标题在别的块，不该并进来：" + text);
  assert.ok(text.includes("そして次の行に続く"), "同块的下一行仍要接上");
});

test("句末标点仍然断句，块内的巨大空隙也断", () => {
  const expand = loadSentenceExpander(SOURCE);
  const chars = makeChars([
    { bk: 5, text: "前の文です。" },
    { bk: 5, text: "次の文が始まる" },
  ]);
  const at = "前の文です。".length + 2;
  const range = expand(chars, at, at);
  const text = chars.slice(range.start, range.end + 1).map((c) => c.c).join("");
  assert.equal(text, "次の文が始まる", "。之后是新的一句");

  // 同块但空了一大截（> 3 倍字高）：那不像换行，像块内真有空行
  const spaced = makeChars([
    { bk: 7, text: "上のかたまり" },
    { bk: 7, text: "下のかたまり" },
  ], { pitch: 4 });
  const at2 = "上のかたまり".length + 1;
  const range2 = expand(spaced, at2, at2);
  const text2 = spaced.slice(range2.start, range2.end + 1)
    .map((c) => c.c).join("");
  assert.equal(text2, "下のかたまり", "块内的巨大空隙仍然断");
});

test("构建产物跟着更新了（只改源不重建，线上跑的还是旧的）", () => {
  const built = loadSentenceExpander(BUILT);
  const chars = makeChars([
    { bk: 3, text: "これらの問題を" },
    { bk: 3, text: "そして平等に" },
  ]);
  const range = built(chars, 2, 2);
  const text = chars.slice(range.start, range.end + 1).map((c) => c.c).join("");
  assert.ok(text.includes("そして平等に"), "reader.js 里还是旧规则：" + text);
});

test("单汉字 token 先并进后一个 token 再查生词库（两处都要有）", () => {
  // 「養」单独有下划线而「栄養」已掌握 —— 下划线信任了分词边界。
  // ⚠ 服务端与 App 各画一遍下划线，只改一处等于没改。
  assert.match(SERVER, /_CJK_ONE_RE = re\.compile/);
  const jp = SERVER.slice(
    SERVER.indexOf("def _build_jp_vocab_marks("),
    SERVER.indexOf("def _word_mastered("));
  assert.match(jp, /if len\(surf\) == 1 and _CJK_ONE_RE\.match\(surf\) and j < n:/);
  assert.match(jp, /if more and idx\.get\(merged\.lower\(\)\):/);
  assert.match(jp, /surf = merged/);
  assert.match(jp, /j = k/, "合并之后要把两个 token 一起消费掉");

  const local = RUNTIME.slice(
    RUNTIME.indexOf("function localVocabMarks("),
    RUNTIME.indexOf("function handleLocalState("));
  assert.match(local, /surf\.length === 1 && \/\^\[\\u3400-\\u9fff\]\$\/\.test\(surf\)/);
  assert.match(local, /mergedKnown = state\.isMastered\(mergedSpec\)/);
  assert.match(local, /i = k2;/, "合并之后要把两个 token 一起消费掉");
});
