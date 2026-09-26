import Foundation

/// 把 `native-store.js` 那个 port 接到本机 SQLite 上。
///
/// This compatibility port accepts already-formed records from remaining web
/// callers. Native book commands instead use ReaderNativeBookStore, which owns
/// record revisions, derived indexes and receipts in one SQLite transaction.
/// Do not put business rules in this low-level port or execute both paths for
/// the same operation. Both owners preserve the same record/journal schema.
///
/// port 的动作与 `references/reader-native-datastore-plan-20260922.md` 一一对应：
/// `read / readMany / listCollection / remembered / commit / journal / meta /
///  putMeta / info`。
///
/// ⚠ 请求与回复都只走**可序列化的字典**：记录本体以 JSON 字符串原样穿过，
/// 中间任何一层都不解析它。解析就意味着要理解它，也就离"在这里加判据"只差一步。
struct ReaderNativeDataStoreBridge {
    enum BridgeError: Error { case invalidRequest(String) }

    let store: ReaderNativeDataStore

    /// 时钟可注入，测试里才能造出确定的 rememberedAt。
    var now: () -> Int64 = { Int64(Date().timeIntervalSince1970 * 1000) }

    func handle(_ request: [String: Any]) throws -> [String: Any] {
        guard let action = request["action"] as? String else {
            throw BridgeError.invalidRequest("缺少 action")
        }
        switch action {
        case "read":
            let collection = try string(request, "collection")
            let id = try string(request, "id")
            return ["ok": true, "record": try recordJSON(collection: collection, id: id) as Any]

        case "readMany":
            let keys = request["keys"] as? [[String: Any]] ?? []
            guard keys.count <= 256 else { throw BridgeError.invalidRequest("一次取太多了") }
            let rows: [Any] = try keys.map { key in
                let collection = try string(key, "collection")
                let id = try string(key, "id")
                return try recordJSON(collection: collection, id: id) ?? NSNull()
            }
            return ["ok": true, "records": rows]

        case "listCollection":
            let collection = try string(request, "collection")
            let includeDeleted = request["includeDeleted"] as? Bool ?? false
            // limit 缺席＝要整个集合（调用方带了 documentId/afterId 这类下推不了的
            // 条件时会这样）。给一个硬上限兜底，别让一次调用把整库拖过桥。
            let limit = (request["limit"] as? NSNumber)?.intValue ?? 100_000
            let offset = (request["offset"] as? NSNumber)?.intValue ?? 0
            let rows = try store.records(collection: collection, limit: limit, offset: offset)
            return ["ok": true, "records": rows
                .filter { includeDeleted || !$0.deleted }
                .map(\.json)]

        case "remembered":
            let mutationId = try string(request, "mutationId")
            return ["ok": true, "record": try store.mutationResult(mutationId: mutationId) as Any]

        case "commit":
            return try commit(request)

        case "journal":
            let after = (request["after"] as? NSNumber)?.int64Value ?? 0
            let limit = (request["limit"] as? NSNumber)?.intValue ?? 500
            let items = try store.journal(after: after, limit: limit)
            let oldest = try store.journal(after: 0, limit: 1).first?.cursor
            let cursor = try store.cursor()
            return ["ok": true,
                    "items": items.map(\.json),
                    "cursor": cursor,
                    // 没有 journal 时 oldest 报 cursor+1 —— 与 IndexedDB 那边同口径，
                    // 让「要的位置已经被裁掉」这条判断有个确定的下界。
                    "oldestCursor": oldest ?? (cursor + 1)]

        case "meta":
            return ["ok": true, "value": try store.meta(try string(request, "key")) as Any]

        case "putMeta":
            try store.putMeta(try string(request, "key"), json: try string(request, "value"))
            return ["ok": true]

        case "info":
            let cursor = try store.cursor()
            return ["ok": true, "cursor": cursor,
                    "journalSize": try store.journalCount(),
                    "collections": try store.collections()]

        default:
            throw BridgeError.invalidRequest("不认识的 action：" + action)
        }
    }

    /// 整批一次事务提交。
    ///
    /// ⚠ **任何一条 rev 对不上就整批不落**。半批落地是 batch 最不该出现的结果 ——
    /// 调用方拿到的是"失败"，而库里却留下了一半，下次它会在一个自己没预期的
    /// 状态上继续写。
    private func commit(_ request: [String: Any]) throws -> [String: Any] {
        guard let entries = request["entries"] as? [[String: Any]], !entries.isEmpty else {
            throw BridgeError.invalidRequest("commit 没有条目")
        }
        guard entries.count <= 512 else { throw BridgeError.invalidRequest("一次提交太多了") }

        var parsed: [(record: ReaderNativeDataStore.Record, mutationId: String?,
                      change: [String: Any]?, expectedRev: Int64?)] = []
        for entry in entries {
            let collection = try string(entry, "collection")
            let id = try string(entry, "id")
            guard let recordValue = entry["record"] else {
                throw BridgeError.invalidRequest("commit 条目缺 record")
            }
            // 记录本体原样穿过：这里只把索引用得着的几个字段抽出来。
            let recordJSON = try canonicalJSON(recordValue)
            let fields = recordValue as? [String: Any] ?? [:]
            let record = ReaderNativeDataStore.Record(
                collection: collection, id: id,
                rev: (fields["rev"] as? NSNumber)?.int64Value ?? 0,
                updatedAt: (fields["updatedAt"] as? NSNumber)?.int64Value ?? now(),
                deleted: fields["deleted"] as? Bool ?? false,
                json: recordJSON)
            // journal 存的是**完整的变更信封**（调用方按它对齐增量），而 cursor
            // 要到提交那一刻才分配 —— 所以这里留着字典，提交时再把 cursor 放进去。
            // ⚠ 只存记录本体是不够的：那样 changes() 拿不到 operation/mutationId，
            //   增量同步就无从判断这一条是写还是删、是不是自己刚发出去的。
            //
            // ⚠ `journal: false` = 只写记录、不进 journal（入站同步走这条）。
            //   journal 是"待发出"的队列；把同步拉回来的记录也塞进去就成了回环，
            //   A 推给 B、B 原样再推回 A，两台设备来回顶而谁都没改过东西。
            //   ⚠ 默认是 **true**：缺字段的老调用方（本地写入）必须照旧进 journal，
            //     默认成 false 的表现是"改了能看见、就是同步不出去"。
            let wantsJournal = entry["journal"] as? Bool ?? true
            var change = entry["change"] as? [String: Any]
            if wantsJournal {
                guard change != nil else {
                    throw BridgeError.invalidRequest("commit 条目要进 journal 却缺 change")
                }
                change?["record"] = recordValue
            } else {
                change = nil
            }
            parsed.append((record, entry["mutationId"] as? String, change,
                           (entry["expectedRev"] as? NSNumber)?.int64Value))
        }

        let stamp = now()
        var cursors: [Int64] = []
        try store.inTransaction {
            for item in parsed {
                let envelope = item.change
                let cursor = try store.commitWithinTransaction(
                    record: item.record, mutationId: item.mutationId,
                    journalJSON: envelope == nil ? nil : { assigned in
                        var change = envelope ?? [:]
                        change["cursor"] = assigned
                        // 序列化失败不该悄悄写个空串进 journal —— 那条增量以后
                        // 谁也解释不了。退回记录本体，至少还认得出是哪条。
                        return (try? self.canonicalJSON(change)) ?? item.record.json
                    },
                    expectedRev: item.expectedRev, now: stamp)
                cursors.append(cursor)
            }
        }
        return ["ok": true, "cursors": cursors]
    }

    // MARK: - 小工具

    private func recordJSON(collection: String, id: String) throws -> String? {
        try store.record(collection: collection, id: id)?.json
    }

    private func string(_ source: [String: Any], _ key: String) throws -> String {
        guard let value = source[key] as? String, !value.isEmpty else {
            throw BridgeError.invalidRequest("缺少 " + key)
        }
        return value
    }

    /// ⚠ `.sortedKeys`：记录的字节要稳定。键序一变，同一条记录在两次导出里
    /// 摘要就不同，于是同步会以为"它改过了"而白走一轮。
    private func canonicalJSON(_ value: Any) throws -> String {
        let data = try JSONSerialization.data(withJSONObject: value,
                                              options: [.sortedKeys, .fragmentsAllowed])
        guard let text = String(data: data, encoding: .utf8) else {
            throw BridgeError.invalidRequest("记录不是合法 JSON")
        }
        return text
    }
}


/// 按库名持有若干个 `ReaderNativeDataStore`，并把网页递来的请求路由过去。
///
/// ⚠ 用户数据保留**三个库**而不是每本书一个：`native-local-runtime.js` 本来就分
/// global / document / device 三套（见 `createStores`），书的隔离靠记录里的
/// `documentId`，不是靠分库。照着它分，迁移时才是一对一。
/// device 那个库还会被整个删掉重建（回收墓碑空间），分开放才删得干净。
/// 账户待发命令另存 transport 库，不能跟着可回收设备缓存被清除。
///
/// ⚠ **库名走白名单**：名字来自网页，直接拿去拼文件路径的话，一个
/// `../../` 就能写到沙盒里别的地方。宁可拒绝一个合法但没登记的名字，
/// 也不要让页面决定往哪写。
final class ReaderNativeDataStoreHost {
    enum HostError: Error, CustomStringConvertible {
        case unknownStore(String)
        var description: String {
            switch self {
            case .unknownStore(let name): return "没有登记的库：" + name
            }
        }
    }

    static let allowedStores: Set<String> = [
        "bw-reader-native-v1-global",
        "bw-reader-native-v1-document",
        "bw-reader-native-v1-device",
        // Durable transport receipts must survive device cache reclamation.
        "bw-reader-native-v1-transport"
    ]

    private let root: URL
    private var stores: [String: ReaderNativeDataStore] = [:]

    init(root: URL? = nil) {
        let base = root ?? (FileManager.default.urls(for: .applicationSupportDirectory,
                                                     in: .userDomainMask).first
                            ?? FileManager.default.temporaryDirectory)
            .appendingPathComponent("BWReader/data-store", isDirectory: true)
        self.root = base
        try? FileManager.default.createDirectory(at: base, withIntermediateDirectories: true)
    }

    func bridge(for name: String) throws -> ReaderNativeDataStoreBridge {
        guard Self.allowedStores.contains(name) else { throw HostError.unknownStore(name) }
        if let existing = stores[name] { return ReaderNativeDataStoreBridge(store: existing) }
        let path = root.appendingPathComponent(name + ".sqlite").path
        if name == "bw-reader-native-v1-device" { reclaimOversizedDeviceStore(path: path) }
        if name == "bw-reader-native-v1-document" { reclaimDerivedDocumentHistory(path: path) }
        let store = try ReaderNativeDataStore(path: path)
        stores[name] = store
        return ReaderNativeDataStoreBridge(store: store)
    }

    /// device 库正常只有几 MB 活数据。超过这个大小说明是历史拷贝把它撑大了
    /// （2026-09-25 实测 18.8 GB：发送队列每次整条重写都往 journal/mutations 各抄一份）。
    static let deviceStoreRebuildBytes: Int64 = 512 * 1024 * 1024

    /// 首次打开前做一次，失败就照常打开旧库 —— 大而慢总比打不开强。
    private func reclaimOversizedDeviceStore(path: String) {
        let size = ((try? FileManager.default.attributesOfItem(atPath: path))?[.size] as? NSNumber)?.int64Value ?? 0
        guard size > Self.deviceStoreRebuildBytes else { return }
        let started = Date()
        let message: String
        do {
            let result = try ReaderNativeDataStore.rebuildKeepingLiveData(path: path)
            message = "device 库重建 \(result.before / 1_048_576)MB→\(result.after / 1_048_576)MB "
                + String(format: "%.1fs", Date().timeIntervalSince(started))
        } catch {
            message = "device 库重建失败(\(size / 1_048_576)MB): \(error)"
        }
        NSLog("[BWReader] %@", message)
        Task { @MainActor in ReaderNativeFaultReporter.shared.note("store", message) }
    }

    /// document 库超过 128MB：几乎一定是派生缓存的历史副本（真实数据通常只有几 MB）。
    /// 只清派生缓存那几类的日志/变更记录，用户数据与游标不动（见 purgeDerivedHistory）。
    static let documentStorePurgeBytes: Int64 = 128 * 1024 * 1024
    static let derivedCollections = ["native-page-overlay-enrichment-cache-v1"]
    private func reclaimDerivedDocumentHistory(path: String) {
        let files = FileManager.default
        let size = ["", "-wal"].reduce(Int64(0)) { $0 + (((try? files.attributesOfItem(atPath: path + $1))?[.size] as? NSNumber)?.int64Value ?? 0) }
        guard size > Self.documentStorePurgeBytes else { return }
        let started = Date()
        let message: String
        do {
            let result = try ReaderNativeDataStore.purgeDerivedHistory(path: path, collections: Self.derivedCollections)
            message = "document 库清派生缓存历史 \(result.before / 1_048_576)MB→\(result.after / 1_048_576)MB "
                + String(format: "%.1fs", Date().timeIntervalSince(started))
        } catch {
            message = "document 库清理失败(\(size / 1_048_576)MB): \(error)"
        }
        NSLog("[BWReader] %@", message)
        Task { @MainActor in ReaderNativeFaultReporter.shared.note("store", message) }
    }

    /// 处理一条网页请求：`{ store, action, ... }`。
    func handle(_ request: [String: Any]) throws -> [String: Any] {
        guard let name = request["store"] as? String else {
            throw ReaderNativeDataStoreBridge.BridgeError.invalidRequest("缺少 store")
        }
        return try bridge(for: name).handle(request)
    }

    /// device 库整个丢掉重建（回收墓碑空间）。⚠ 只对 device 开放：
    /// global/document 里是用户的东西，"回收空间"不该能把它们一起清了。
    func resetDeviceStore() throws {
        let name = "bw-reader-native-v1-device"
        stores[name]?.close()
        stores[name] = nil
        try? FileManager.default.removeItem(at: root.appendingPathComponent(name + ".sqlite"))
    }

    func closeAll() {
        stores.values.forEach { $0.close() }
        stores.removeAll()
    }
}
