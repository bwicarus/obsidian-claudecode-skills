import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => readFileSync(new URL(path, ROOT), "utf8");
const RUNTIME = read("_server_deploy/static/pdf/native-local-runtime.js");
const WORDPOP = read("_server_deploy/static/pdf/rc-wordpop.js");
const VOICECALL = read("_server_deploy/static/pdf/rc-voicecall.js");
const STICKY = read("_server_deploy/static/pdf/rc-stickynote.js");

/// 取一个函数的函数体：从 `function 名(` 到下一个同缩进的 `function`。
const bodyOf = (source, name) => {
  const start = source.indexOf(`function ${name}(`);
  if (start < 0) return "";
  const next = source.slice(start + 1).search(/\n {2}function /);
  return next < 0 ? source.slice(start) : source.slice(start, start + 1 + next);
};

// 用户设计（2026-09-03）：「字典的内容还是保存在原本的字典内，卡片的内容还是保存在
// 卡片内，只是锁定后进行一次标记……解绑等动作发生时需要将其解除……都要立刻体现」。
//
// 病根是旧索引存的是卡片内容的**副本**：建卡时写入，之后解绑/改锁/移动都不碰它。
// 重做后索引只剩「词键 → cid」标记，由便签写入在同一批事务里派生；内容显示时现读。

test("word-bindings 与便签同批派生：写入便签的两条路径都带上它", () => {
  // ① mutateDocumentStateNow：前置读四种记录、batch 四条 put
  const mutate = bodyOf(RUNTIME, "mutateDocumentStateNow");
  assert.match(mutate, /\['document-notes-legacy', 'card-placements', 'entity-references', 'word-bindings'\]/);
  assert.match(mutate, /var bindingsBefore = deriveWordBindings\(records\[0\]\.payload\)/);
  assert.match(mutate, /stateRecordMutation\('word-bindings', bindings, suffix \+ '-words', records\[3\]\.rev\)/);
  // 事务提交后才广播绑定变化（与 announceLocalNotesChanged 同一处，同一条件）
  assert.match(mutate, /announceLocalNotesChanged\('mutation'\);\n\s*announceWordBindingsChanged\(bindingsBefore, bindings, 'mutation'\)/);
  // ② writeNotesAndIndexes（整册替换：导入/迁移）
  const replace = bodyOf(RUNTIME, "writeNotesAndIndexes");
  assert.match(replace, /stateRecordMutation\('word-bindings', bindings, suffix \+ '-words'\)/);
  assert.match(replace, /announceWordBindingsChanged\(before, bindings, 'replace'\)/);
});

test("派生规则：只认 page-chars 绑定，键=去空白小写的绑定文本，不带内容", () => {
  const derive = bodyOf(RUNTIME, "deriveWordBindings");
  assert.match(derive, /bind\.kind !== 'page-chars'\) return;/);
  assert.match(derive, /var key = wordBindingKey\(bind\.text\)/);
  assert.doesNotMatch(derive, /content/, "索引记录里不得再出现内容副本");
  const keyFn = bodyOf(RUNTIME, "wordBindingKey");
  assert.match(keyFn, /\.toLowerCase\(\)/);
});

test("查询门面按 lemma / word / cid 找标记，内容从各书便签现读，自愈掉失效标记", () => {
  const route = bodyOf(RUNTIME, "localWordCardIndex");
  assert.match(route, /strictQuery\(url, \['lemma', 'cid', 'word'\], \[\], code\)/);
  assert.match(route, /ensureWordBindingsRebuilt\(\)\.then\(listAllWordBindings\)/);
  assert.match(route, /return liveWordCards\(refs\)/);
  // POST 不再存副本
  assert.match(route, /deprecated: true/);
  assert.doesNotMatch(route, /mutateDeviceState\('word-card-index'/);
  const live = bodyOf(RUNTIME, "liveWordCards");
  assert.match(live, /stores\.document\.get\('native-document-notes-legacy', docId \+ ':document-notes-legacy'\)/);
  // 便签没了 / 绑定键不再匹配 → 当作不存在（解绑立刻生效的根据）
  assert.match(live, /wordBindingKey\(bind\.text\) !== r\.key\) return null;/);
});

test("旧数据一次性重建标记，此后只靠写入维护", () => {
  const rebuild = bodyOf(RUNTIME, "ensureWordBindingsRebuilt");
  assert.match(rebuild, /readDeviceState\('word-bindings-rebuilt', null\)/);
  assert.match(rebuild, /stores\.document\.list\('native-document-notes-legacy'/);
  assert.match(rebuild, /payload: deriveWordBindings\(value\.payload\)/);
});

test("词卡整理直接写回各书便签本体，撤销用单独的 prev 记录", () => {
  const cons = bodyOf(RUNTIME, "nativeReaderWordCardsConsolidate");
  assert.match(cons, /applyWordCardContents\(/);
  assert.match(cons, /'word-card-consolidations'/);
  assert.doesNotMatch(cons, /mutateDeviceState\('word-card-index'/, "整理不得再经索引副本中转");
  const apply = bodyOf(RUNTIME, "applyWordCardContents");
  assert.match(apply, /writeNotesAndIndexesFor\(docId, notes, record \? record\.rev : undefined, 'consolidate'\)/);
  const writeFor = bodyOf(RUNTIME, "writeNotesAndIndexesFor");
  assert.match(writeFor, /rec\('word-bindings', bindings, '-words'\)/);
});

test("单词框只信索引，并订阅绑定变化即时重取卡片段", () => {
  const attach = bodyOf(WORDPOP, "_attachWordCards");
  assert.match(attach, /\/pdf\/api\/word-card-index\?lemma=' \+ encodeURIComponent\(_nwKey\(key\)\) \+\n\s*'&word=' \+ encodeURIComponent\(_nwKey\(word\)\)/);
  // 索引路由不存在的宿主才退回本书便签扫描；在 App 内不得用本地扫描盖过索引
  assert.match(attach, /if \(cards === null\) cards = _localBoundCards\(wordKeys\)/);
  // 旧卡片段先摘掉再放新的（不再"已有就返回"，否则解绑后旧段留着）
  assert.match(attach, /var old = pop\.querySelector\('\.wp-cards'\);\n\s*if \(old\) old\.remove\(\);/);
  assert.doesNotMatch(attach, /excludeCids/, "本地知识覆盖索引的补丁已无必要");
  assert.match(WORDPOP, /document\.addEventListener\('bw:native-word-bindings-changed'/);
  assert.match(WORDPOP, /_attachWordCards\(pop, _wordPopState\.word, _wordPopState\.lemma, _wordPopState\.cardsRect \|\| null\)/);
});

test("内容副本的两个旧入口已拆：语音建卡不再登记副本，便签不再开卷对账", () => {
  assert.doesNotMatch(VOICECALL, /function _registerWordCard\(/);
  assert.doesNotMatch(VOICECALL, /\/pdf\/api\/word-card-index/);
  assert.doesNotMatch(STICKY, /function reconcileConsolidatedWordCard\(/);
  assert.doesNotMatch(STICKY, /reconcileConsolidatedWordCard\(ctl, h\)/);
  // 建卡后的成功返回形状不变（契约锁定的那一行）
  assert.match(VOICECALL, /if \(_pr && _pr\.ok === true\) return _renderInfoResult\(true, 'bound'\);/);
});

test("绑定变化事件带契约与键集合，电脑端快照仍由既有便签变更信号驱动", () => {
  assert.match(RUNTIME, /var WORD_BINDINGS_CHANGED_CONTRACT = 'reader-word-bindings-changed\/1'/);
  assert.match(RUNTIME, /var WORD_BINDINGS_CHANGED_EVENT = 'bw:native-word-bindings-changed'/);
  const announce = bodyOf(RUNTIME, "announceWordBindingsChanged");
  assert.match(announce, /changes\.push\(\{ cid: cid, before: bmap\[cid\] \|\| '', after: amap\[cid\] \|\| '' \}\)/);
  const dispatch = bodyOf(RUNTIME, "dispatchWordBindingsChanged");
  assert.match(dispatch, /keys: keys\.slice\(\), changes: changes\.slice\(\)/);
  // 绑定是便签的一次写入，announceLocalNotesChanged 照旧触发 page.context 重发
  assert.match(bodyOf(RUNTIME, "mutateDocumentStateNow"), /announceLocalNotesChanged\('mutation'\)/);
});

// 2026-09-04 618 App 日志实锤:appendWordDictLine 一进门 ReferenceError(bindWordTextOf 未定义)被空 catch 吞掉,
// 卡内词典从上一版起一次都没跑过。名字被引用就必须有定义,而且取的是词锚的 text。
test("bindWordTextOf 有定义且取词锚 text", () => {
  const NOTE = readFileSync(new URL("../../_server_deploy/static/pdf/rc-stickynote.js", import.meta.url), "utf8");
  assert.match(NOTE, /function bindWordTextOf\(bind\) \{\n\s*return String\(\(bind && bind\.text\) \|\| ''\)\.trim\(\);/);
  // 用到它的三处都在同一个 IIFE 作用域里
  assert.ok(NOTE.indexOf("function bindWordTextOf(bind)") < NOTE.indexOf("var text = bindWordTextOf(bind);"));
});
