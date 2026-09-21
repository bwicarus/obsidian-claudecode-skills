// iCloud 同步引擎的几条设计约束。
//
// 这些不是"风格"，每一条都对应一种会丢数据或会烧配额的具体失败；写成闸门是因为
// 它们在代码里长得都很不起眼，很容易在后续重构里被顺手改掉。
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8").replace(/\r\n/g, "\n");

const SYNC = read("ios/BWReader/App/ReaderCloudUserStateSync.swift");
const body = (source, from, to) => source.slice(source.indexOf(from), source.indexOf(to));
const code = (source) =>
  source.split(/\r?\n/).filter((line) => !/^\s*(\/\/|\/\*|\*|\/\/\/)/.test(line)).join("\n");

test("① 跨设备身份用内容摘要，不是 localBookId", () => {
  // 本机导入的书在每台设备上 id 都不同；同一个 PDF 的 sha256 一样。
  assert.match(SYNC, /static func recordName\(digest: String, domain: String\)/);
  assert.match(SYNC, /"b_" \+ digest \+ "\|" \+ domain/);
  assert.match(SYNC, /guard digest\.count == 64/, "摘要长度要校验，别让半截 id 进云端");
  assert.doesNotMatch(code(SYNC), /localBookId/, "localBookId 不该出现在同步身份里");
});

test("② 域负载用 CKAsset —— 墨迹会超过单字段 1MB", () => {
  const fill = body(SYNC, "static func fill(", "static func payloadJSON(");
  assert.match(fill, /CKAsset\(fileURL: url\)/);
  // 写成字段的话，炸的恰好是批注最多的那本书。
  assert.doesNotMatch(code(fill), /record\["payload"\] = snapshot\.payloadJson/);
});

test("③ 引擎的不透明状态必须持久化", () => {
  // 官方要求；不存＝每次冷启动全量重来。
  assert.match(SYNC, /case \.stateUpdate\(let update\)/);
  assert.match(SYNC, /store\.saveEngineState\(update\.stateSerialization\)/);
  // Serialization 是 Codable，用 JSON 存。
  assert.match(SYNC, /JSONDecoder\(\)\.decode\(CKSyncEngine\.State\.Serialization\.self/);
});

test("④ 保存成功后要存 system fields，否则每次都撞 serverRecordChanged", () => {
  assert.match(SYNC, /record\.encodeSystemFields\(with: coder\)/);
  const sent = body(SYNC, "case .sentRecordZoneChanges(let sent)", "case .accountChange");
  assert.match(sent, /store\.saveSystemFields\(record\)/);
});

test("⑤ 合不出来就什么都不做 —— 不许整域取一边", () => {
  const apply = body(SYNC, "private func applyRemote(", "private func handleFailedSave(");
  assert.match(apply, /guard let merged = try\? merger\.mergeJSON/);
  // ⚠ 这里若加一句"合并失败就用远端/本地"，就是静默丢掉另一台设备的改动。
  assert.doesNotMatch(code(apply), /else \{\s*store\.saveBase[^}]*theirs/);
  assert.match(apply, /return\s*\n\s*\}/);
  // 冲突时服务端那份是新起点，合完再发。
  assert.match(SYNC, /case \.serverRecordChanged:/);
  assert.match(SYNC, /failure\.error\.serverRecord/);
});

test("⑥ 与基线一致的域不重发 —— 否则每次同步把八个域原样再推一遍", () => {
  const send = body(SYNC, "private func recordToSend(", "private func snapshot(");
  assert.match(send, /store\.loadBase\(digest: parsed\.digest, domain: parsed\.domain\) == snapshot\.payloadJson/);
  assert.match(send, /syncEngine\.state\.remove\(pendingRecordZoneChanges: \[\.saveRecord\(id\)\]\)/);
});

test("⑦ 换账号要清基线 —— 否则下一次合并拿错祖先", () => {
  assert.match(SYNC, /case \.accountChange:/);
  assert.match(SYNC, /store\.clearAll\(\)/);
});

test("⑧ 没登录 iCloud 时安静地不起，而不是报错", () => {
  const start = body(SYNC, "func start() async", "func markDirty(");
  assert.match(start, /accountStatus\(\), status == \.available else \{ return \}/);
  // 本地优先本来就能用；把"没登录"做成错误会让不用 iCloud 的人一直看到红字。
});

test("⑨ markDirty 只登记，不在里面导出", () => {
  const mark = body(SYNC, "func markDirty(contentSHA256: String)", "/// 「现在就同步」");
  // 写入很频繁（每一笔墨迹都算），每次都导出整包会把主线程压住。
  assert.doesNotMatch(code(mark), /exportDomains|await/);
  assert.match(mark, /engine\.state\.add\(pendingRecordZoneChanges:/);
});

test("⑩ 这里的基线跟 Pi 那套不是一回事，别合并它们", () => {
  // Pi 的基线每个域只存 digest（够判断谁更新）；三方合并需要**祖先的内容本身**。
  assert.match(SYNC, /ReaderBookUserStateBaselineStore/, "把区别写在注释里，防止被顺手去重");
  assert.match(SYNC, /func loadBase\(digest: String, domain: String\) -> String\?/);
  assert.match(SYNC, /func saveBase\(digest: String, domain: String, json: String\)/);
});

test("⑪ 一趟发送不把整本书导出八遍", () => {
  // 一次发送要问八个域，而导出是整包的（每个域都要规范化 JSON + 算一遍 sha256，
  // 还要穿过 WebView 那一跳）。不缓存就是一次同步把整本书导出八遍。
  const snap = body(SYNC, "private func snapshot(digest: String, domain: String)",
                    "/// 写回之后缓存立刻作废");
  assert.match(snap, /exportCache\[digest\]/);
  assert.match(snap, /timeIntervalSince\(cached\.at\) < 2/, "只活两秒：这是趟内缓存");
  // ⚠ 它绝不能被当成"数据没变"的依据 —— 那是 store.loadBase 的活。
  const send = body(SYNC, "private func recordToSend(", "/// 取某本书某个域的当前快照");
  assert.match(send, /store\.loadBase\(/);
  // 写回之后立刻作废，否则同一趟里后面的域会拿到写入前那版。
  assert.match(SYNC, /invalidateExportCache\(parsed\.digest\)/);
});

test("⑫ 别的书的远端改动不能就地丢掉", () => {
  // 导出/写回只对**当前打开的那本书**有效（要穿过该书的本地 runtime）。
  // ⚠ 引擎已经认为这条"取过了"，丢了就是永久少一份 —— 而且完全无声。
  const apply = body(SYNC, "private func applyRemote(", "/// 某本书打开时");
  assert.match(apply, /store\.savePending\(digest: parsed\.digest, domain: parsed\.domain, json: theirs\)/);
  assert.match(SYNC, /func drainPending\(contentSHA256: String\) async/);
  // 合完才清：先清后合的话中间失败就两头落空。
  const drain = body(SYNC, "func drainPending(contentSHA256: String)", "private func merge(digest:");
  assert.ok(drain.indexOf("await merge(") < drain.indexOf("store.clearPending("));
});

test("⑬ 容器标识符两处必须逐字一致", () => {
  // ⚠ 容器标识符**区分大小写**，而它有两份副本：entitlements 和 Swift 常量。
  // 对不上的表现分两种，都很难看出原因：签名时带着一个 App ID 上没有的容器
  // （构建红），或者签名过了但运行时所有 CloudKit 调用静静地失败。
  const ENTITLEMENTS = read("ios/BWReader/App/BWReader.entitlements");
  const declared = ENTITLEMENTS.match(
    /<key>com\.apple\.developer\.icloud-container-identifiers<\/key>\s*<array>\s*<string>([^<]+)<\/string>/);
  assert.ok(declared, "entitlements 里要声明容器");
  const inCode = SYNC.match(/static let containerIdentifier = "([^"]+)"/);
  assert.ok(inCode, "Swift 里要有那个常量");
  assert.equal(inCode[1], declared[1]);
  // 用 CKSyncEngine 就必须是 CloudKit 那一档（不是 iCloud Documents/KV）。
  assert.match(ENTITLEMENTS, /<key>com\.apple\.developer\.icloud-services<\/key>\s*<array>\s*<string>CloudKit<\/string>/);
  // 服务端要能推醒它，否则只能等下次前台轮询。
  assert.match(ENTITLEMENTS, /<key>aps-environment<\/key>/);
});
