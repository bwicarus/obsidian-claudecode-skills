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

// 2026-09-04 第三次踩坑(bindWordTextOf → _dictLineCache):卡内词典链路里被调用的名字必须在 rc-stickynote.js 里有定义。
// 全部包在 try/catch 里,ReferenceError 只会让功能静默死亡;这条契约在本地就把"用到却没定义"抓出来。
test("卡内词典链路用到的每个标识符都有定义", () => {
  const NOTE = readFileSync(new URL("../../_server_deploy/static/pdf/rc-stickynote.js", import.meta.url), "utf8");
  const bodyOfNote = (name) => {
    const start = NOTE.indexOf(`function ${name}(`);
    assert.ok(start >= 0, `缺 function ${name}`);
    const next = NOTE.slice(start + 1).search(/\n {2}function /);
    return next < 0 ? NOTE.slice(start) : NOTE.slice(start, start + 1 + next);
  };
  const GLOBALS = new Set([
    "window", "document", "String", "Array", "Promise", "Date", "Math", "JSON", "Number", "Object", "Boolean",
    "fetch", "setTimeout", "clearTimeout", "console", "encodeURIComponent", "decodeURIComponent",
    "IntersectionObserver", "AbortController", "parseInt", "parseFloat", "isFinite", "Error", "RegExp", "Set", "Map", "RC",
    "if", "for", "while", "return", "function", "typeof", "catch", "try", "new", "var", "else", "switch",
  ]);
  const defined = new Set();
  for (const m of NOTE.matchAll(/(?:^|\n)\s*(?:function\s+([A-Za-z_$][\w$]*)\s*\(|var\s+([A-Za-z_$][\w$]*)\s*[=;,])/g)) {
    defined.add(m[1] || m[2]);
  }
  const missing = new Set();
  for (const fn of ["appendWordDictLine", "appendDictWhenVisible", "_rebindWord", "dictLineNote", "bindWordTextOf"]) {
    const body = bodyOfNote(fn)
      .replace(/'(?:[^'\\\n]|\\.)*'/g, "''")
      .replace(/\/\/[^\n]*/g, "");
    // 只看"名字(" 与 "名字[" 两种用法,且前面不是 . (方法调用不算)
    for (const m of body.matchAll(/(?<![\w$.])([A-Za-z_$][\w$]*)\s*(?=\(|\[)/g)) {
      const id = m[1];
      if (GLOBALS.has(id) || defined.has(id)) continue;
      // 函数自己的形参/局部变量:出现在 body 内的 var/参数声明里就算有定义
      const local = new RegExp(`(?:var\\s+|\\(|,\\s*)${id.replace(/\$/g, "\\$")}\\b`).test(body);
      if (local) continue;
      missing.add(`${fn}: ${id}`);
    }
  }
  assert.deepEqual([...missing], [], "这些名字被调用却没有定义(会在运行时 ReferenceError 并被 catch 吞掉)");
});

// 2026-09-04 用户实锤 パンセオ:查词框靠 Codex 兜底有释义,卡内词典只打服务器词典却是空的;
// 有下划线(=查过)的词再点仍闪烁重查(结果只在内存缓存)。两条都要走同一条链并落设备缓存。
test("卡内词典与查词框共用 RC.wordpop.lookupData,查词结果落设备持久缓存", () => {
  const POP = readFileSync(new URL("../../_server_deploy/static/pdf/rc-wordpop.js", import.meta.url), "utf8");
  const NOTE = readFileSync(new URL("../../_server_deploy/static/pdf/rc-stickynote.js", import.meta.url), "utf8");
  assert.match(POP, /lookupData: lookupData, peekCache: peekCache, meaningText: _jpMeaningText \};/);
  // 键带版本:词条字段变化时必须换版,否则 App 端永远命中旧缓存(2026-09-04 加 source_* 三字段时的实锤)
  assert.match(POP, /var _PERSIST_KEY = 'rc-wordpop-dict-cache-v\d+';/);
  assert.match(POP, /var cached = _dictCache\.get\(word\) \|\| _persistGet\(word\);/);
  // 合成兜底词条(暂无词典释义…)不得落盘,否则永久短路真查询
  assert.match(POP, /if \(result\.meaning_source === 'synthetic' \|\| \/\^暂无词典释义\/\.test\(String\(result\.definition \|\| ''\)\)\) \{ _persistLastSkip = '合成兜底词条'; return false; \}/);
  assert.match(NOTE, /typeof RC\.wordpop\.lookupData === 'function';/);
  assert.match(NOTE, /Promise\.resolve\(RC\.wordpop\.lookupData\(text, bindCtx\)\)/);
  // 固化段与最新查词结果不一致或本身是不确定类文本 → 重写(2026-09-04 「未能确定」实锤)
  assert.match(NOTE, /固化段过期,按最新查词重写/);
  assert.match(POP, /function _isUncertainMeaning\(text\)/);
});

// 2026-09-04 用户实锤 ヘルスプロモーション:「明明是从英文来的但没标英文」。
// 服务端 stale-while-revalidate 先秒回旧条目再后台升级,而客户端三层缓存把**第一跳**
// 当终局存下 → 升级后的条目永远没人取。三层都要挡,且服务端必须如实上报 stale。
test("stale-while-revalidate 的第一跳一层都不许缓存", () => {
  const READER = read("_server_deploy/pdf_reader.py");
  // ① 服务端如实上报(响应是逐字段重建,不加就传不出去)
  assert.match(READER, /"stale": bool\(jp\.get\("stale_pv"\)\)/);
  // ② 会话内存 + localStorage 两层由 _cacheDictResult 一处把门
  assert.match(
    WORDPOP,
    /if \(result\.stale === true\) \{ _dictDiag\('不缓存「' \+ word \+ '」:服务端标了 stale/,
    "查词框缓存要认 stale",
  );
  // ③ 设备库 + 桥留底(桥被毒化会传染到别的设备)
  const fetchBody = bodyOf(RUNTIME, "nativeDictQuickFetch");
  assert.match(fetchBody, /if \(d && d\.ok === true && d\.stale !== true\) \{/);
  // 已被毒化的条目靠换键清掉:两处键版本必须一起往前走
  assert.match(WORDPOP, /rc-wordpop-dict-cache-v3/);
  assert.match(RUNTIME, /'\|' \+ langs \+ '\|v3'/);
});
