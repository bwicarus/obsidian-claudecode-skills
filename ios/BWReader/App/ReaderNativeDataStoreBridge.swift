import Foundation

/// 把 `native-store.js` 那个 port 接到本机 SQLite 上。
///
/// ⚠ **这里不做任何判据**：记录该长什么样是 `data-store.js` 算好的，这里只
/// 存/取。哪天在这里看到「rev + 1」「要不要算墓碑」之类的东西，那就是分叉开始
/// 的地方 —— 同一次写入会有两个答案，而这种分歧只在两边真跑过同一条数据时
/// 才暴露。
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
    /// 每本书一个库。⚠ 身份用**内容摘要**而不是 localBookId —— 与 iCloud 同步
    /// 同一口径：本机导入的书在每台设备上 id 都不同，而同一个文件的 sha256 一样。
    let contentSHA256: String

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
                      change: [String: Any], expectedRev: Int64?)] = []
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
            guard var change = entry["change"] as? [String: Any] else {
                throw BridgeError.invalidRequest("commit 条目缺 change")
            }
            change["record"] = recordValue
            parsed.append((record, entry["mutationId"] as? String, change,
                           (entry["expectedRev"] as? NSNumber)?.int64Value))
        }

        let stamp = now()
        var cursors: [Int64] = []
        try store.inTransaction {
            for item in parsed {
                let cursor = try store.commitWithinTransaction(
                    record: item.record, mutationId: item.mutationId,
                    journalJSON: { assigned in
                        var change = item.change
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
