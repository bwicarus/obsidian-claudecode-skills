// 同步桥：引擎与阅读器之间唯一的接触点。
//
// 这一层的两个风险都不会当场报错，只会表现成「同步没生效」：
// ① 事务里那三个字段（digest / byteCount / empty）任何一项对不上，**整笔**被拒；
// ② 导出/写回对错了书 —— 它们要穿过**当前打开那本书**的本地 runtime。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");

const BRIDGE = read("ios/BWReader/App/ReaderCloudUserStateBridge.swift");
const WEBVIEW = read("ios/BWReader/App/ReaderWebView.swift");
const MERGE = read("ios/BWReader/App/ReaderUserStateMerge.swift");
const RUNTIME = read("_server_deploy/static/pdf/native-local-runtime.js");

const body = (source, from, to) => source.slice(source.indexOf(from), source.indexOf(to));
const code = (source) =>
  source.split(/\r?\n/).filter((line) => !/^\s*(\/\/|\/\*|\*|\/\/\/)/.test(line)).join("\n");

test("① 事务那三个字段都从同一串 payloadJson 算出来", () => {
  const encode = body(BRIDGE, "static func payload(", "/// 事务要的 `remoteBookId`");
  assert.match(encode, /digest: sha256Hex\(json\)/, "digest 是 payloadJson 原样字节的 sha256");
  assert.match(encode, /byteCount: Data\(json\.utf8\)\.count/);
  assert.match(encode, /revision: max\(1, revision\)/, "runtime 硬要求 revision ≥ 1");
  // runtime 会重算并核对这两项。
  assert.match(RUNTIME, /digests\[0\] !== domain\.digest/);
  assert.match(RUNTIME, /bytes !== domain\.byteCount/);
});

test("② empty 复用已对照验证的原生合并规则", () => {
  // runtime 拿它自己的 userStateDomainEmpty 复核，对不上整笔事务被拒。
  assert.match(MERGE, /func domainEmpty\(domain: String, value: Any\) throws -> Bool/);
  assert.match(MERGE, /ReaderNativeBookMerge\.empty\(domain: domain, value: value\)/);
  assert.doesNotMatch(MERGE, /import JavaScriptCore|objectForKeyedSubscript/);
  const apply = body(WEBVIEW, "func applyUserStateFromCloudSync(", "/// Prepare against the original");
  assert.match(apply, /merger\.domainEmpty\(domain: domain\.name, value: value\)/);
  assert.doesNotMatch(code(apply), /isEmpty \? true : false|\.count == 0/);
  assert.match(RUNTIME, /userStateDomainEmpty\(domain\.name, value\) !== domain\.empty/);
});

test("③ expectedLocalHeaders 取此刻的本地头，不是快照时的", () => {
  const apply = body(WEBVIEW, "func applyUserStateFromCloudSync(", "/// Prepare against the original");
  assert.match(apply, /adapter\.snapshotHeaders\(localBookId: book\.id\)/);
  // ⚠ 从"读快照"到"写回"之间用户可能又划了一道。取旧的会把那一道盖掉，
  // 而 runtime 那道乐观并发闸（BW_USER_STATE_LOCAL_CHANGED）正是为此存在。
  assert.match(RUNTIME, /BW_USER_STATE_LOCAL_CHANGED/);
});

test("④ 摘要对不上就返回空，而不是返回别的书", () => {
  const exportFn = body(WEBVIEW, "func exportUserStateForCloudSync(", "/// 把合并结果整域写回本地");
  assert.match(exportFn, /currentLocalBookContentSHA256\?\.lowercased\(\) == contentSHA256\.lowercased\(\)/);
  // 导出前后各查一次：中间那次 await 里书可能换了。
  assert.ok((exportFn.match(/contentSHA256\.lowercased\(\)/g) || []).length >= 2);
  assert.match(exportFn, /generation == bookUserStateContextGeneration/);
});

test("⑤ 桥只看得到摘要，导出/写回留在阅读器那一侧", () => {
  // 它们要穿过该书的本地 runtime，在别处调就是对着错的书说话。
  assert.match(WEBVIEW, /var cloudSyncContentDigest: String\? \{/);
  assert.doesNotMatch(code(BRIDGE), /bookUserStateWebAdapter|currentLocalBook\b|applyAtomically/);
  assert.match(BRIDGE, /weak var reader: ReaderWebViewModel\?/);
});

test("⑥ remoteBookId 由内容摘要派生，不用 UUID", () => {
  // 用 UUID 会让同一本书在两台设备上生成两个事务身份。
  const derive = BRIDGE.slice(BRIDGE.indexOf("static func remoteBookId("));
  assert.match(derive, /"book_" \+ String\(contentSHA256\.lowercased\(\)\.prefix\(32\)\)/);
  assert.match(RUNTIME, /\^book_\[a-f0-9\]\{32\}\$/, "runtime 的格式要求没变");
});

test("⑦ 开关在启动时也要跟一次", () => {
  const APP = read("ios/BWReader/App/BWReaderNativeApp.swift");
  // ⚠ 只在设置里翻转时接的话，重开 App 后同步就**静静地**不工作了。
  assert.match(APP, /@AppStorage\("reader\.iCloudSync"\)/);
  assert.match(APP, /\.task\(id: iCloudSyncEnabled\) \{ reader\.setCloudSyncEnabled\(iCloudSyncEnabled\) \}/);
  const WORKSPACE = read("ios/BWReader/App/ReaderNativeWorkspace.swift");
  assert.match(WORKSPACE, /onCloudSyncChanged: \{ reader\.setCloudSyncEnabled\(\$0\) \}/);
});

test("⑧ 写入即标脏，开书即合待处理", () => {
  // 标脏挂在 withNativePDFWriter 的成功分支上 —— App 所有 PDF 用户状态写入的
  // 唯一咽喉。不挂这里就得在每个写入点各记一次，漏一个就少同步一类东西。
  assert.match(WEBVIEW, /markCloudSyncDirty\(\)/);
  assert.match(WEBVIEW, /private func markCloudSyncDirty\(\)/);
  const dirty = body(WEBVIEW, "private func markCloudSyncDirty()", "/// 当前这本书的内容摘要");
  assert.doesNotMatch(code(dirty), /export|snapshot/, "只登记，不导出");
  // 开书时把别的设备攒下的改动合掉，否则它们会一直躺着。
  assert.match(WEBVIEW, /cloudSync\.drainPending\(contentSHA256: digest\)/);
});

test("⑨ 关掉同步不等于删数据", () => {
  const setter = body(WEBVIEW, "func setCloudSyncEnabled(", "/// 本地写入后告诉同步器");
  assert.match(setter, /guard enabled else \{ cloudSync = nil; cloudSyncBridge = nil; return \}/);
  // 用户多半是"先别同步"而不是"把这些都扔了"。真要清由 accountChange 那条路负责。
  assert.doesNotMatch(code(setter), /clearAll|removeItem/);
});

test("⑩ EPUB 的写入也要标脏", () => {
  // ⚠ EPUB 写入**不经过** withNativePDFWriter（那是 PDF 专属的写者租约），
  // 于是它们此前一次都没 ping 过。原生正文那边无所谓（EPUB 没有原生渲染器），
  // 但"这本书脏了"挂在同一条信号上 —— 不补这一处，EPUB 里划的线永远不会
  // 同步出去，而且完全无声。
  assert.match(RUNTIME, /function announceUserStateKindWrite\(kind\)/);
  assert.match(RUNTIME, /'epub-highlights': true/);
  assert.match(RUNTIME, /'epub-ink': true/);
  // 两条 document 域写入路径都要报。
  const mutateDoc = body(RUNTIME, "function mutateDocumentState(kind, fallback",
                         "function deviceStateId(kind)");
  assert.match(mutateDoc, /announceUserStateKindWrite\(kind\)/);
  const mutateHl = body(RUNTIME, "function mutateHighlightCollection(kind, mutator",
                        "// 启动迁移：整册数组");
  assert.match(mutateHl, /announceUserStateKindWrite\(kind\)/);
  // 清单要盖住 userStateDomainsFromRecords 实际读的那几种：少一种，那种东西的
  // 改动就不会触发同步。
  for (const kind of ["reading-position", "document-highlights", "ink",
                      "document-notes-legacy", "user-pages", "card-placements",
                      "entity-references"]) {
    assert.match(RUNTIME, new RegExp(`'${kind}': true`), kind + " 不在标脏清单里");
  }
});
