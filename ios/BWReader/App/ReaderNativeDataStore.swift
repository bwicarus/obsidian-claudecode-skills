import Foundation
import SQLite3

/// 本机数据库：阅读器全部用户数据的落地处。
///
/// 设计与分阶段见 `references/reader-native-datastore-plan-20260922.md`。一句话：
/// **这里只管持久化机制，不管判据**。记录该长什么样（revision、墓碑、causal
/// 证明、批次规划）全在 `reader-runtime/data-store.js` 里，照抄一遍等于让
/// 「同一次写入该得到什么记录」有两个答案 —— 而这种分歧只在两边真跑过同一条
/// 数据时才暴露，那时已经写进库里了。
///
/// ⚠ **只依赖 Foundation + SQLite3**，不 import UIKit/WebKit：它要能被
/// `xcrun swiftc` 单文件编译出来直接跑（`Tests/NativeDataStore/main.swift`）。
/// 存储层不能靠「契约测试看文本」验 —— 那只能证明代码里写了某个词，
/// 而存储写错的表现是数据丢、或者读回来是旧的。
///
/// 四张表对应原来的四个对象仓：
/// | records | `collection\|id` | 单取 / 按 collection 列 / 覆盖 |
/// | journal | `cursor` | 从游标起顺序读 / 追加 / 裁剪最旧 |
/// | mutations | `mutationId` | 单取（重放去重）/ put / 按时间裁剪 |
/// | meta | `key` | 游标、epoch、迁移标记 |
final class ReaderNativeDataStore {
    enum StoreError: Error, CustomStringConvertible {
        case open(String)
        case sql(String)
        /// 乐观并发没对上：调用方读到的那一版已经不是当前版本了，该重来一轮。
        /// ⚠ 这**不是**异常情况，是这套模型的正常分支（`ifRev` 本来就在语义里）。
        case revisionConflict(collection: String, id: String, actual: Int64)

        var description: String {
            switch self {
            case .open(let message): return "打不开数据库：" + message
            case .sql(let message): return "SQLite：" + message
            case .revisionConflict(let collection, let id, let actual):
                return "记录已被改过 \(collection)/\(id)（当前 rev=\(actual)）"
            }
        }
    }

    /// 一条记录在库里的样子。`json` 是 data-store.js 那边算好的完整记录，
    /// 这里不解释它 —— 只把 collection/id/rev/updatedAt 抽出来当索引用。
    struct Record: Equatable {
        let collection: String
        let id: String
        let rev: Int64
        let updatedAt: Int64
        let deleted: Bool
        let json: String
    }

    struct JournalEntry: Equatable {
        let cursor: Int64
        let json: String
    }

    private var handle: OpaquePointer?
    private let path: String

    /// `path` 传 `":memory:"` 可以开一个内存库 —— 测试用。
    init(path: String) throws {
        self.path = path
        var handle: OpaquePointer?
        guard sqlite3_open_v2(path, &handle,
                              SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX,
                              nil) == SQLITE_OK, let handle else {
            throw StoreError.open(path)
        }
        self.handle = handle
        // ⚠ WAL + NORMAL：阅读时写入很碎（每一笔墨迹一次），默认的
        //   journal 模式会让每次写都等一次 fsync。⚠ 内存库不支持 WAL，
        //   设了会报错 —— 所以按路径分开。
        if path != ":memory:" {
            try execute("PRAGMA journal_mode=WAL")
            try execute("PRAGMA synchronous=NORMAL")
        }
        try execute("PRAGMA foreign_keys=ON")
        try migrate()
    }

    deinit { if let handle { sqlite3_close_v2(handle) } }

    func close() {
        if let handle { sqlite3_close_v2(handle) }
        handle = nil
    }

    // MARK: - 表结构

    private func migrate() throws {
        try execute("""
            CREATE TABLE IF NOT EXISTS records (
                pk TEXT PRIMARY KEY,
                collection TEXT NOT NULL,
                id TEXT NOT NULL,
                rev INTEGER NOT NULL,
                updatedAt INTEGER NOT NULL,
                deleted INTEGER NOT NULL,
                json TEXT NOT NULL
            )
            """)
        // 列表要按 (updatedAt, id) 排 —— 与 IndexedDB 那个 collectionUpdated
        // 复合索引同序，否则两个实现翻页翻出来的顺序不一样。
        try execute("CREATE INDEX IF NOT EXISTS records_collection_updated ON records (collection, updatedAt, id)")
        try execute("""
            CREATE TABLE IF NOT EXISTS journal (
                cursor INTEGER PRIMARY KEY,
                json TEXT NOT NULL
            )
            """)
        try execute("""
            CREATE TABLE IF NOT EXISTS mutations (
                mutationId TEXT PRIMARY KEY,
                rememberedAt INTEGER NOT NULL,
                json TEXT NOT NULL
            )
            """)
        try execute("CREATE INDEX IF NOT EXISTS mutations_remembered ON mutations (rememberedAt)")
        try execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                json TEXT NOT NULL
            )
            """)
    }

    // MARK: - 读

    static func primaryKey(collection: String, id: String) -> String { collection + "\u{0}" + id }

    func record(collection: String, id: String) throws -> Record? {
        try records(matching: "pk = ?", bind: [.text(Self.primaryKey(collection: collection, id: id))]).first
    }

    func records(collection: String, limit: Int, offset: Int) throws -> [Record] {
        try records(matching: "collection = ? ORDER BY updatedAt, id LIMIT ? OFFSET ?",
                    bind: [.text(collection), .int(Int64(max(0, limit))), .int(Int64(max(0, offset)))])
    }

    func recordCount(collection: String) throws -> Int {
        var result = 0
        try query("SELECT COUNT(*) FROM records WHERE collection = ?", bind: [.text(collection)]) { statement in
            result = Int(sqlite3_column_int64(statement, 0))
        }
        return result
    }

    func mutationResult(mutationId: String) throws -> String? {
        var json: String?
        try query("SELECT json FROM mutations WHERE mutationId = ?", bind: [.text(mutationId)]) { statement in
            json = Self.text(statement, 0)
        }
        return json
    }

    func journal(after cursor: Int64, limit: Int) throws -> [JournalEntry] {
        var entries: [JournalEntry] = []
        try query("SELECT cursor, json FROM journal WHERE cursor > ? ORDER BY cursor LIMIT ?",
                  bind: [.int(cursor), .int(Int64(max(0, limit)))]) { statement in
            entries.append(JournalEntry(cursor: sqlite3_column_int64(statement, 0),
                                        json: Self.text(statement, 1) ?? ""))
        }
        return entries
    }

    func meta(_ key: String) throws -> String? {
        var json: String?
        try query("SELECT json FROM meta WHERE key = ?", bind: [.text(key)]) { statement in
            json = Self.text(statement, 0)
        }
        return json
    }

    func cursor() throws -> Int64 {
        guard let raw = try meta("cursor"), let value = Int64(raw) else { return 0 }
        return value
    }

    // MARK: - 写

    /// 一次提交：记录 + journal 条目 + mutation 备忘，**一个事务里做完**。
    ///
    /// ⚠ `expectedRev` 是乐观并发：调用方在 JS 那边读到的那一版。对不上就抛
    /// `revisionConflict`，由调用方重来一轮 —— 这正是 `ifRev` 的语义，不是妥协。
    /// 传 nil 表示"不检查"（用于导入/迁移这类权威写入）。
    ///
    /// 返回分配到的 journal 游标。
    @discardableResult
    func commit(record: Record, mutationId: String?, journalJSON: (Int64) -> String,
                expectedRev: Int64?, now: Int64,
                maxJournal: Int = 10_000, maxMutations: Int = 20_000) throws -> Int64 {
        try inTransaction {
            if let expectedRev {
                let current = try self.record(collection: record.collection, id: record.id)
                let actual = current?.rev ?? 0
                guard actual == expectedRev else {
                    throw StoreError.revisionConflict(collection: record.collection,
                                                      id: record.id, actual: actual)
                }
            }
            try self.execute("""
                INSERT INTO records (pk, collection, id, rev, updatedAt, deleted, json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(pk) DO UPDATE SET
                    rev = excluded.rev, updatedAt = excluded.updatedAt,
                    deleted = excluded.deleted, json = excluded.json
                """, bind: [
                    .text(Self.primaryKey(collection: record.collection, id: record.id)),
                    .text(record.collection), .text(record.id), .int(record.rev),
                    .int(record.updatedAt), .int(record.deleted ? 1 : 0), .text(record.json)
                ])

            let next = try self.cursor() + 1
            try self.execute("INSERT INTO journal (cursor, json) VALUES (?, ?)",
                             bind: [.int(next), .text(journalJSON(next))])
            try self.execute("INSERT INTO meta (key, json) VALUES ('cursor', ?) "
                             + "ON CONFLICT(key) DO UPDATE SET json = excluded.json",
                             bind: [.text(String(next))])

            if let mutationId, !mutationId.isEmpty {
                try self.execute("""
                    INSERT INTO mutations (mutationId, rememberedAt, json) VALUES (?, ?, ?)
                    ON CONFLICT(mutationId) DO UPDATE SET
                        rememberedAt = excluded.rememberedAt, json = excluded.json
                    """, bind: [.text(mutationId), .int(now), .text(record.json)])
            }

            // 裁剪放在同一个事务里：分开做的话崩在中间会留下一个比上限大的库，
            // 而下一次启动没人会再去收拾它。
            try self.trim(table: "journal", orderBy: "cursor", keep: maxJournal)
            try self.trim(table: "mutations", orderBy: "rememberedAt", keep: maxMutations)
            return next
        }
    }

    func putMeta(_ key: String, json: String) throws {
        try execute("INSERT INTO meta (key, json) VALUES (?, ?) "
                    + "ON CONFLICT(key) DO UPDATE SET json = excluded.json",
                    bind: [.text(key), .text(json)])
    }

    private func trim(table: String, orderBy: String, keep: Int) throws {
        guard keep > 0 else { return }
        try execute("DELETE FROM \(table) WHERE rowid IN ("
                    + "SELECT rowid FROM \(table) ORDER BY \(orderBy) DESC LIMIT -1 OFFSET ?)",
                    bind: [.int(Int64(keep))])
    }

    // MARK: - SQLite 细节

    enum Value {
        case text(String)
        case int(Int64)
    }

    /// ⚠ 手写事务而不是 `BEGIN`/`COMMIT` 散在各处：抛错时必须 ROLLBACK，
    /// 漏一次就会把后面所有写入都堵在一个没结束的事务里，表现是"突然什么都存不进去"。
    func inTransaction<T>(_ work: () throws -> T) throws -> T {
        try execute("BEGIN IMMEDIATE")
        do {
            let value = try work()
            try execute("COMMIT")
            return value
        } catch {
            try? execute("ROLLBACK")
            throw error
        }
    }

    func execute(_ sql: String, bind values: [Value] = []) throws {
        try query(sql, bind: values) { _ in }
    }

    private func query(_ sql: String, bind values: [Value] = [],
                       row: (OpaquePointer) -> Void) throws {
        guard let handle else { throw StoreError.sql("数据库已关闭") }
        var statement: OpaquePointer?
        guard sqlite3_prepare_v2(handle, sql, -1, &statement, nil) == SQLITE_OK, let statement else {
            throw StoreError.sql(String(cString: sqlite3_errmsg(handle)) + " :: " + sql)
        }
        defer { sqlite3_finalize(statement) }
        for (index, value) in values.enumerated() {
            let position = Int32(index + 1)
            switch value {
            case .text(let text):
                // ⚠ SQLITE_TRANSIENT：不给的话 SQLite 会以为这段内存归它管，
                //   而 Swift 的 String 在语句执行前就可能被回收 —— 那是随机的脏数据。
                sqlite3_bind_text(statement, position, text, -1,
                                  unsafeBitCast(-1, to: sqlite3_destructor_type.self))
            case .int(let number):
                sqlite3_bind_int64(statement, position, number)
            }
        }
        while true {
            let step = sqlite3_step(statement)
            if step == SQLITE_ROW { row(statement); continue }
            if step == SQLITE_DONE { break }
            throw StoreError.sql(String(cString: sqlite3_errmsg(handle)) + " :: " + sql)
        }
    }

    private func records(matching clause: String, bind values: [Value]) throws -> [Record] {
        var found: [Record] = []
        try query("SELECT collection, id, rev, updatedAt, deleted, json FROM records WHERE " + clause,
                  bind: values) { statement in
            found.append(Record(collection: Self.text(statement, 0) ?? "",
                                id: Self.text(statement, 1) ?? "",
                                rev: sqlite3_column_int64(statement, 2),
                                updatedAt: sqlite3_column_int64(statement, 3),
                                deleted: sqlite3_column_int64(statement, 4) != 0,
                                json: Self.text(statement, 5) ?? ""))
        }
        return found
    }

    private static func text(_ statement: OpaquePointer, _ column: Int32) -> String? {
        guard let pointer = sqlite3_column_text(statement, column) else { return nil }
        return String(cString: pointer)
    }
}
