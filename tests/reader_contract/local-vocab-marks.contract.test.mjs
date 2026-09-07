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

test("vocabulary-state 多了 lookup 属性，词框查到即记（词组只认收藏）", () => {
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
  // 2026-09-04 用户:「词组的下划线应该是收藏后出现而不是查询后」→ 词组框查完**不再**记 lookup
  assert.doesNotMatch(PHRASEPOP, /setLookedUp\(lspec/);
});

test("本地 page-overlay 按本地字符层 + 本地状态算下划线，已掌握不画", () => {
  const overlay = bodyOf(RUNTIME, "localPageOverlay");
  assert.match(overlay, /vocab_marks: localVocabMarks\(result && result\.chars\)/);
  const marks = bodyOf(RUNTIME, "localVocabMarks");
  // 没有 vocabulary-state 时必须仍是 []（首开不出网、不制造假标记）
  assert.match(marks, /typeof state\.lookup !== 'function' \|\| !Array\.isArray\(chars\) \|\| !chars\.length\) return \[\];/);
  // 按分词 w 分组、跳过 sp
  assert.match(marks, /while \(j < n && chars\[j\] && chars\[j\]\.w === wid\)/);
  assert.match(marks, /if \(state\.isMastered\(spec\) \|\| state\.isMastered\(phraseSpec\)\) \{ masteredRanges\.push\(\[lo0, i - 1\]\); continue; \}/);   // 已掌握不画,但范围要记(2026-09-07)
  assert.match(marks, /if \(state\.isPhraseFavorite\(phraseSpec\)\) slug = 'seen';/);
  assert.match(marks, /else if \(state\.isLookedUp\(spec\)\) slug = 'new';/);   // 词组 lookup 不算(2026-09-04)
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

// 2026-09-03 App 客户端日志实锤「查词后下划线出现又消失」:查词后 1.8s/3.5s/1.5s 三轮全页刷新
// 拿服务端 page-vocab-marks 整页**覆盖**,服务端不知道 App 本地 lookup 状态,刚画出的下划线被冲掉。
test("查词后全页刷新与本地标记合并,不覆盖", () => {
  const SENT = readFileSync(new URL("../../_server_deploy/static/pdf/reader.src/12-vocab-sentences.js", import.meta.url), "utf8");
  assert.match(SENT, /pw\.__vocabMarks = _mergeVocabMarks\(pw\.__localVocabMarks, remote\);/);
  assert.doesNotMatch(SENT, /pw\.__vocabMarks = d\.vocab_marks \|\| \[\];/);
});

// 2026-09-04 用户:「词组的下划线应该是收藏后出现而不是查询后」。单词 lookup 即画;词组只认收藏。
test("词组下划线只认收藏,不认查过", () => {
  const RT = readFileSync(new URL("../../_server_deploy/static/pdf/native-local-runtime.js", import.meta.url), "utf8");
  const PP = readFileSync(new URL("../../_server_deploy/static/pdf/rc-phrasepop.js", import.meta.url), "utf8");
  assert.match(RT, /else if \(state\.isLookedUp\(spec\)\) slug = 'new';/);
  assert.doesNotMatch(RT, /state\.isLookedUp\(phraseSpec\)/);
  assert.match(RT, /if \(r\.property === 'lookup' && r\.kind === 'phrase'\) return;/);
  assert.doesNotMatch(PP, /setLookedUp\(lspec/);
});

// 用户 2026-09-07 实锤 おける：已查过未掌握却没有下划线。记录键是原形 於ける，下划线按页面表层 おける 查；
// 原形早有记录时词框跳过登记，表层永远进不了别名；第二遍全文搜也只搜键不搜别名。三处一起改。
test("表层进别名：原形已记也要补登表层，第二遍全文搜键 + 别名，别名并集", () => {
  const note = bodyOf(WORDPOP, "_noteLookedUp");
  assert.match(note, /if \(!_lookupCoversSurface\(state, spec, word\)\)/);
  const covers = bodyOf(WORDPOP, "_lookupCoversSurface");
  assert.match(covers, /state\.lookup\(spec, 'lookup'\)/);
  assert.match(covers, /have\.aliases\.indexOf\(surface\) >= 0/);
  const marks = bodyOf(RUNTIME, "localVocabMarks");
  assert.match(marks, /\[r\.key\]\.concat\(Array\.isArray\(r\.aliases\) \? r\.aliases : \[\]\)\.forEach/);
  const setProp = bodyOf(VSTATE, "setProperty");
  assert.match(setProp, /previous\.aliases\.forEach\(function \(alias\)/);
});

// 用户 2026-09-07 实锤：「更大范围的词组已经收藏并掌握了，但是其中的部分词反而又有下划线」。已掌握的词/词组本来
// 就不画，于是它的范围对里面的短标记没有任何压制力。现在把已掌握范围登记下来（第一遍按 w、第二遍按键+别名全文搜），
// 被完全包住的标记一律不画：部分词不能比整体"更不熟"。
test("已掌握的词/词组范围压掉里面的短标记", () => {
  const marks = bodyOf(RUNTIME, "localVocabMarks");
  assert.match(marks, /var masteredRanges = \[\];/);
  assert.match(marks, /if \(state\.isMastered\(spec\) \|\| state\.isMastered\(phraseSpec\)\) \{ masteredRanges\.push\(\[lo0, i - 1\]\); continue; \}/);
  assert.match(marks, /r\.property !== 'lookup' && r\.property !== 'favorite' && r\.property !== 'mastered'/);
  assert.match(marks, /if \(w\.slug === 'mastered'\) \{ masteredRanges\.push\(\[lo, hi\]\);/);
  assert.match(marks, /seenKey\[k \+ '\|' \+ slugFor\]/, "同一键既是查过又是已掌握时，掌握范围不能被去重吞掉");
  assert.match(marks, /if \(masteredRanges\[q\]\[0\] <= m\._lo && m\._hi <= masteredRanges\[q\]\[1\]\) return false;/);
});
