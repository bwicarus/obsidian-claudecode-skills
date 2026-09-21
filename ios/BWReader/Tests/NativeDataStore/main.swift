import Foundation

// 本机数据库的用例。存储层不能靠「契约测试看文本」验 —— 那只能证明代码里写了
// 某个词，而存储写错的表现是数据丢、或者读回来是旧的，只有真跑才看得出。
//
// 跟 NativePDFCrop / JapaneseWordChain 一样：独立 swiftc 可执行文件，CI 直接跑，
// 不用模拟器、不用签名。为此 ReaderNativeDataStore.swift 只依赖 Foundation+SQLite3。

func check(_ condition: @autoclosure () -> Bool, _ reason: String) {
    guard condition() else { fatalError(reason) }
}

func record(_ collection: String, _ id: String, rev: Int64, at: Int64,
            deleted: Bool = false, json: String? = nil) -> ReaderNativeDataStore.Record {
    ReaderNativeDataStore.Record(
        collection: collection, id: id, rev: rev, updatedAt: at, deleted: deleted,
        json: json ?? "{\"id\":\"\(id)\",\"rev\":\(rev)}")
}

let store = try! ReaderNativeDataStore(path: ":memory:")

// ── 写进去要读得回来，游标从 1 开始按序发 ──
let firstCursor = try! store.commit(record: record("hl", "a", rev: 1, at: 100),
                                    mutationId: "m1", journalJSON: { "{\"cursor\":\($0)}" },
                                    expectedRev: 0, now: 100)
check(firstCursor == 1, "第一个游标应该是 1，实际 \(firstCursor)")
let readBack = try! store.record(collection: "hl", id: "a")
check(readBack?.rev == 1, "读回来的 rev 不对")
check(readBack?.json.contains("\"id\":\"a\"") == true, "读回来的 json 不对")
check(try! store.cursor() == 1, "游标没落库")

// ── 乐观并发：读到的那一版不是当前版就该拒绝 ──
// ⚠ 这是模型的正常分支（ifRev 本来就在语义里），不是异常情况。
var conflicted = false
do {
    _ = try store.commit(record: record("hl", "a", rev: 2, at: 110),
                         mutationId: "m2", journalJSON: { "{\"cursor\":\($0)}" },
                         expectedRev: 0, now: 110)
} catch ReaderNativeDataStore.StoreError.revisionConflict(_, _, let actual) {
    conflicted = true
    check(actual == 1, "冲突里报的当前 rev 不对：\(actual)")
} catch { fatalError("应该报 revisionConflict，实际 \(error)") }
check(conflicted, "rev 对不上却提交成功了 —— 这会静默覆盖别人的写入")

// 对得上就能写
_ = try! store.commit(record: record("hl", "a", rev: 2, at: 110),
                      mutationId: "m2", journalJSON: { "{\"cursor\":\($0)}" },
                      expectedRev: 1, now: 110)
check(try! store.record(collection: "hl", id: "a")?.rev == 2, "第二次写没生效")

// expectedRev = nil 表示权威写入（导入/迁移），不检查
_ = try! store.commit(record: record("hl", "a", rev: 9, at: 120),
                      mutationId: nil, journalJSON: { "{\"cursor\":\($0)}" },
                      expectedRev: nil, now: 120)
check(try! store.record(collection: "hl", id: "a")?.rev == 9, "权威写入没生效")

// ── 列表按 (updatedAt, id) 排，与 IndexedDB 那个复合索引同序 ──
// ⚠ 顺序不一样的话，两个实现翻页翻出来的东西不同。
_ = try! store.commit(record: record("note", "z", rev: 1, at: 10), mutationId: nil,
                      journalJSON: { "{\"cursor\":\($0)}" }, expectedRev: nil, now: 10)
_ = try! store.commit(record: record("note", "y", rev: 1, at: 30), mutationId: nil,
                      journalJSON: { "{\"cursor\":\($0)}" }, expectedRev: nil, now: 30)
_ = try! store.commit(record: record("note", "x", rev: 1, at: 20), mutationId: nil,
                      journalJSON: { "{\"cursor\":\($0)}" }, expectedRev: nil, now: 20)
let ordered = try! store.records(collection: "note", limit: 10, offset: 0).map(\.id)
check(ordered == ["z", "x", "y"], "列表顺序应按 updatedAt，实际 \(ordered)")
check(try! store.recordCount(collection: "note") == 3, "计数不对")
let paged = try! store.records(collection: "note", limit: 1, offset: 1).map(\.id)
check(paged == ["x"], "翻页不对，实际 \(paged)")
// 别的 collection 不能混进来
check(try! store.records(collection: "hl", limit: 10, offset: 0).count == 1, "collection 串了")

// ── mutation 备忘：重放去重靠它 ──
check(try! store.mutationResult(mutationId: "m1") != nil, "mutation 没记住")
check(try! store.mutationResult(mutationId: "没有这个") == nil, "不存在的 mutation 不该有值")

// ── journal 从游标起顺序读 ──
let changes = try! store.journal(after: 0, limit: 100)
check(changes.count == 6, "journal 条数不对：\(changes.count)")
check(changes.map(\.cursor) == Array(1...6), "journal 游标不连续：\(changes.map(\.cursor))")
check(try! store.journal(after: 4, limit: 100).map(\.cursor) == [5, 6], "从游标起读不对")
check(try! store.journal(after: 0, limit: 2).count == 2, "limit 不生效")

// ── 裁剪：留最新的，且在同一个事务里做 ──
// ⚠ 分开做的话崩在中间会留下一个比上限大的库，而下次启动没人会再收拾它。
let trimmed = try! ReaderNativeDataStore(path: ":memory:")
for index in 1...10 {
    _ = try! trimmed.commit(record: record("c", "i\(index)", rev: 1, at: Int64(index)),
                            mutationId: "mm\(index)", journalJSON: { "{\"cursor\":\($0)}" },
                            expectedRev: nil, now: Int64(index),
                            maxJournal: 3, maxMutations: 3)
}
let kept = try! trimmed.journal(after: 0, limit: 100).map(\.cursor)
check(kept == [8, 9, 10], "journal 该只留最新 3 条，实际 \(kept)")
check(try! trimmed.mutationResult(mutationId: "mm10") != nil, "最新的 mutation 被裁掉了")
check(try! trimmed.mutationResult(mutationId: "mm1") == nil, "最旧的 mutation 没被裁掉")
// 裁 journal 不能连记录一起裁掉
check(try! trimmed.recordCount(collection: "c") == 10, "记录被裁剪波及了")

// ── 事务：中途抛错要整笔回滚 ──
// ⚠ 漏掉 ROLLBACK 的话后面所有写入都会被堵在一个没结束的事务里，
//   表现是"突然什么都存不进去"。
let rollback = try! ReaderNativeDataStore(path: ":memory:")
_ = try! rollback.commit(record: record("c", "keep", rev: 1, at: 1), mutationId: nil,
                         journalJSON: { "{\"cursor\":\($0)}" }, expectedRev: nil, now: 1)
struct Boom: Error {}
do {
    try rollback.inTransaction {
        try rollback.execute("DELETE FROM records")
        throw Boom()
    }
    fatalError("应该把 Boom 抛出来")
} catch is Boom {
    // 期望路径
} catch { fatalError("抛出来的不是 Boom：\(error)") }
check(try! rollback.recordCount(collection: "c") == 1, "事务没回滚，记录被删了")
// 回滚之后还能继续写 —— 这一条才是真正要守的
_ = try! rollback.commit(record: record("c", "after", rev: 1, at: 2), mutationId: nil,
                         journalJSON: { "{\"cursor\":\($0)}" }, expectedRev: nil, now: 2)
check(try! rollback.recordCount(collection: "c") == 2, "回滚之后写不进去了")

// ── meta 往返 ──
try! rollback.putMeta("epoch", json: "\"e1\"")
check(try! rollback.meta("epoch") == "\"e1\"", "meta 没存住")
try! rollback.putMeta("epoch", json: "\"e2\"")
check(try! rollback.meta("epoch") == "\"e2\"", "meta 没覆盖")
check(try! rollback.meta("没有这个") == nil, "不存在的 meta 不该有值")

// ── journal 传 nil：只写记录，不入队、不动游标 ──
// ⚠ 入站同步靠这条形状。journal 是"待发出"的队列；把同步拉回来的记录也塞进去
//   就成了回环 —— A 推给 B、B 原样再推回 A，两台设备来回顶而谁都没改过东西。
let inbound = try! ReaderNativeDataStore(path: ":memory:")
_ = try! inbound.commit(record: record("hl", "local", rev: 1, at: 10), mutationId: nil,
                        journalJSON: { "{\"cursor\":\($0)}" }, expectedRev: nil, now: 10)
let cursorBeforeInbound = try! inbound.cursor()
let inboundCursor = try! inbound.commit(record: record("hl", "remote", rev: 9, at: 20),
                                        mutationId: "r1", journalJSON: nil,
                                        expectedRev: nil, now: 20)
check(inboundCursor == 0, "不入队时该报 0，实际 \(inboundCursor)")
check(try! inbound.cursor() == cursorBeforeInbound, "不入队却动了游标")
check(try! inbound.journalCount() == 1, "不入队却多了一条 journal")
// 记录本身要真写进去，mutation 备忘也要留（重发时认得出来）。
check(try! inbound.record(collection: "hl", id: "remote")?.rev == 9, "入站记录没落库")
check(try! inbound.mutationResult(mutationId: "r1") != nil, "入站的 mutation 备忘没留")
inbound.close()

// ── 同一批里两条改同一条 id：后一条的 expectedRev 是前一条刚写出的 rev ──
// ⚠ 核对必须逐条「读当前 → 比 → 写」。改成"先整批核对、再整批写"的话，第二条
//   在核对那一刻还看不到第一条，于是被判成冲突 —— 表现是一次拉取只落了一半。
let chained = try! ReaderNativeDataStore(path: ":memory:")
try! chained.inTransaction {
    _ = try chained.commitWithinTransaction(
        record: record("hl", "same", rev: 1, at: 1), mutationId: nil,
        journalJSON: nil, expectedRev: 0, now: 1)
    _ = try chained.commitWithinTransaction(
        record: record("hl", "same", rev: 2, at: 2), mutationId: nil,
        journalJSON: nil, expectedRev: 1, now: 2)
}
check(try! chained.record(collection: "hl", id: "same")?.rev == 2, "同批第二条没落地")
chained.close()

// ── 真文件：落盘之后重开还在（内存库验不到这条）──
let file = FileManager.default.temporaryDirectory
    .appendingPathComponent("bw-datastore-test-\(UUID().uuidString).sqlite")
let onDisk = try! ReaderNativeDataStore(path: file.path)
_ = try! onDisk.commit(record: record("c", "persist", rev: 3, at: 7), mutationId: nil,
                       journalJSON: { "{\"cursor\":\($0)}" }, expectedRev: nil, now: 7)
onDisk.close()
let reopened = try! ReaderNativeDataStore(path: file.path)
check(try! reopened.record(collection: "c", id: "persist")?.rev == 3, "重开之后数据没了")
check(try! reopened.cursor() == 1, "重开之后游标没了 —— 会导致 journal 从头发号")
reopened.close()
try? FileManager.default.removeItem(at: file)

// ── 桥：网页递过来的那层字典 ──
//
// 桥上有一个**默认值**，错了的表现极安静：`journal` 缺字段时必须当成 true。
// 默认成 false 的话，本地写入不再入队，用户看到的是「改了能看见、就是同步
// 不出去」—— 本机一切正常，只有另一台设备永远收不到。

let bridgeStore = try! ReaderNativeDataStore(path: ":memory:")
let bridge = ReaderNativeDataStoreBridge(store: bridgeStore, now: { 4242 })

func bridgeEntry(_ id: String, rev: Int, journal: Bool?, expected: Int?) -> [String: Any] {
    var entry: [String: Any] = [
        "collection": "hl", "id": id,
        "record": ["schema": 1, "collection": "hl", "id": id, "rev": rev,
                   "updatedAt": 1000 + rev, "updatedBy": "dev",
                   "deleted": false, "value": ["id": id]] as [String: Any],
        "change": ["operation": "put", "collection": "hl"] as [String: Any]
    ]
    if let journal { entry["journal"] = journal }
    if let expected { entry["expectedRev"] = expected }
    return entry
}

// 缺 journal 字段 → 照旧入队（本地写入的默认形状）。
let defaultReply = try! bridge.handle([
    "store": "s", "action": "commit",
    "entries": [bridgeEntry("a", rev: 1, journal: nil, expected: 0)]
])
check((defaultReply["cursors"] as? [Int64])?.first == 1,
      "缺 journal 字段时没入队 —— 同步会永远发不出去")
check(try! bridgeStore.journalCount() == 1, "默认没写 journal")

// journal:false → 只写记录，不入队、不动游标。
let silentReply = try! bridge.handle([
    "store": "s", "action": "commit",
    "entries": [bridgeEntry("b", rev: 1, journal: false, expected: 0)]
])
check((silentReply["cursors"] as? [Int64])?.first == 0, "不入队时该报 0")
check(try! bridgeStore.journalCount() == 1, "journal:false 还是入队了")
check(try! bridgeStore.record(collection: "hl", id: "b")?.rev == 1, "记录本身没写进去")

// 要入队却没给 change → 必须报错，不能悄悄写个空信封进 journal。
var missingChange = bridgeEntry("c", rev: 1, journal: true, expected: 0)
missingChange.removeValue(forKey: "change")
var refusedMissingChange = false
do {
    _ = try bridge.handle(["store": "s", "action": "commit", "entries": [missingChange]])
} catch { refusedMissingChange = true }
check(refusedMissingChange, "要入队却缺 change，居然放行了")

// 一批里有一条 rev 对不上 → 整批不落（半批落地是最不该出现的结果）。
var halfBatchRefused = false
do {
    _ = try bridge.handle([
        "store": "s", "action": "commit",
        "entries": [bridgeEntry("d", rev: 1, journal: false, expected: 0),
                    bridgeEntry("e", rev: 1, journal: false, expected: 99)]
    ])
} catch { halfBatchRefused = true }
check(halfBatchRefused, "整批该被拒")
check(try! bridgeStore.record(collection: "hl", id: "d") == nil, "半批落地了")
bridgeStore.close()

// 库名走白名单：名字来自网页，直接拿去拼路径的话一个 ../../ 就写到沙盒里别处了。
let hostRoot = FileManager.default.temporaryDirectory
    .appendingPathComponent("bw-store-host-\(UUID().uuidString)", isDirectory: true)
let host = ReaderNativeDataStoreHost(root: hostRoot)
var rejectedStore = false
do { _ = try host.handle(["store": "../../etc/passwd", "action": "info"]) }
catch { rejectedStore = true }
check(rejectedStore, "没登记的库名居然放行了")
check((try? host.handle(["store": "bw-reader-native-v1-global", "action": "info"])) != nil,
      "登记过的库名反而被拒")
host.closeAll()
try? FileManager.default.removeItem(at: hostRoot)

print("ReaderNativeDataStore: 全部用例通过")
