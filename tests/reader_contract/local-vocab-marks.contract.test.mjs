import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const RUNTIME = read("_server_deploy/static/pdf/native-local-runtime.js");
const CHARLAYER = read("_server_deploy/static/pdf/reader.src/08-charlayer.js");
const WORDPOP = read("_server_deploy/static/pdf/rc-wordpop.js");
const PHRASEPOP = read("_server_deploy/static/pdf/rc-phrasepop.js");
const VSTATE = read("_server_deploy/static/reader-runtime/vocabulary-state.js");
const VOICE = read("_server_deploy/static/pdf/rc-computer-voice.js");

const bodyOf = (source, name) => {
  const start = source.indexOf(`function ${name}(`);
  if (start < 0) return "";
  const next = source.slice(start + 1).search(/\n {2}function /);
  return next < 0 ? source.slice(start) : source.slice(start, start + 1 + next);
};

// 用户 2026-09-03 实锤：「这个词只是查询过但是没有标记掌握，应该有下划线但是现在看不到」。
// 病根：下划线只来自服务端 vocab 索引，而日语查词本地 JMdict 命中根本不出网，
// 查过的词服务端永远不知道。现在生词下划线以本地为准。

test("vocabulary-state 多了 lookup 属性，词框与词组框查到即记", () => {
  assert.match(VSTATE, /var VALID_PROPERTY = \{ mastered: true, favorite: true, lookup: true \}/);
  assert.match(VSTATE, /setLookedUp: function \(input, value, options\)/);
  assert.match(VSTATE, /isLookedUp: function \(input\) \{ return enabled\(input, 'lookup'\); \}/);
  // 词框：渲染成功后记；合成兜底词条（"暂无词典释义"）不算查到
  assert.match(WORDPOP, /_cacheDictResult\(word, d\);\n\s*_wordPopState\.lemma = d\.lemma \|\| word;\n\s*_noteLookedUp\(word, d\);/);
  const note = bodyOf(WORDPOP, "_noteLookedUp");
  // 点过就算查过(2026-09-03):词典没有的词也记 —— 它恰恰是你不认识的
  assert.doesNotMatch(note, /indexOf\('暂无词典释义'\) === 0\) return;/);
  assert.match(note, /state\.setLookedUp\(/);
  // 本地/缓存命中时补报服务端 lookup-event（它的查词日志、生词笔记链路照旧）
  assert.match(note, /var servedLocally = d\.source === 'local-jmdict' \|\| d\.cached === true/);
  assert.match(note, /fetch\('\/pdf\/api\/lookup-event'/);
  // 当前页立即刷新
  assert.match(note, /window\.refreshLocalVocabMarks\(_ctx\.page \|\| 0\)/);
  // 词组框同样记（kind: phrase）
  assert.match(PHRASEPOP, /var lspec = \{ kind: 'phrase', language: isJa \? 'ja' : 'en', lemma: text, word: text \};/);
  assert.match(PHRASEPOP, /vs\.setLookedUp\(lspec, true, \{ source: 'rc-phrasepop' \}\)/);
});

test("本地 page-overlay 按本地字符层 + 本地状态算下划线，已掌握不画", () => {
  const overlay = bodyOf(RUNTIME, "localPageOverlay");
  assert.match(overlay, /vocab_marks: localVocabMarks\(result && result\.chars\)/);
  const marks = bodyOf(RUNTIME, "localVocabMarks");
  // 没有 vocabulary-state 时必须仍是 []（首开不出网、不制造假标记）
  assert.match(marks, /typeof state\.lookup !== 'function' \|\| !Array\.isArray\(chars\) \|\| !chars\.length\) return \[\];/);
  // 按分词 w 分组、跳过 sp
  assert.match(marks, /while \(j < n && chars\[j\] && chars\[j\]\.w === wid\)/);
  assert.match(marks, /if \(state\.isMastered\(spec\) \|\| state\.isMastered\(phraseSpec\)\) continue;/);
  assert.match(marks, /if \(state\.isPhraseFavorite\(phraseSpec\)\) slug = 'seen';/);
  assert.match(marks, /else if \(state\.isLookedUp\(spec\) \|\| state\.isLookedUp\(phraseSpec\)\) slug = 'new';/);
  assert.match(marks, /label_slug: slug, rects: rects, jp: ja, local: true/);
});

test("字符层：本地标记与服务端增强做并集，增强到达不冲掉本地", () => {
  assert.match(CHARLAYER, /function _mergeVocabMarks\(local, remote\)/);
  const apply = bodyOf(CHARLAYER, "_applyPageVocabOverlay");
  assert.match(apply, /const isEnrichment = !!\(overlay && overlay\.savedAt\);/);
  assert.match(apply, /if \(!isEnrichment\) wrap\.__localVocabMarks = \(overlay && overlay\.vocab_marks\) \|\| \[\];/);
  assert.match(apply, /_mergeVocabMarks\(wrap\.__localVocabMarks, overlay\.vocab_marks\)/);
  // 开页时先套本地再叠增强；此前 `currentEnrichment || ov` 有增强就跳过本地
  assert.match(CHARLAYER, /_applyPageVocabOverlay\(wrap, ov\);\n\s*if \(currentEnrichment\) _applyPageVocabOverlay\(wrap, currentEnrichment\);/);
  assert.doesNotMatch(CHARLAYER, /_applyPageVocabOverlay\(wrap, currentEnrichment \|\| ov\)/);
  assert.match(CHARLAYER, /window\.refreshLocalVocabMarks = function \(page\)/);
});

// 用户 2026-09-03 实锤（52 页）：对话行 + 表格的混合页被视觉层标成 manga，4 列网格把一行
// 文字拆进两格、留下大片空格。整页宽行的页不是分镜，按阅读顺序输出正文。
test("快照：漫画网格只给真漫画页，整页宽行的页按阅读顺序输出正文", () => {
  const prose = bodyOf(VOICE, "mangaLayoutIsProse");
  assert.match(prose, /region\.kind !== "vision-supplement"/);
  assert.match(prose, /\(region\.bounds\[2\] - region\.bounds\[0\]\) >= pageWidth \* 0\.5/);
  assert.match(prose, /return wide \/ regions\.length >= 0\.4;/);
  const manga = bodyOf(VOICE, "appendLocalMangaLayout");
  assert.match(manga, /if \(mangaLayoutIsProse\(layout\)\) \{\n\s*appendLocalProseLayout\(builder, pageRecord, layout\);\n\s*return;/);
  const flow = bodyOf(VOICE, "appendLocalProseLayout");
  // 同一视觉行的块用空格接上（一行被网格拆成两格的病根）
  assert.match(flow, /builder\.append\(sameLine \? " " : "\\n"\)/);
});
