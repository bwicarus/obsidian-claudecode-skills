import Foundation
import CryptoKit

/// Device-local storage for the existing account-scoped command-outbox/2
/// records. Acknowledgement consumes only the exact captured revision, never
/// a newer enqueue. It does not execute commands or infer delivery from errors.
struct ReaderNativeCommandOutbox {
    typealias Object = [String: Any]
    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }
    struct Entry {
        let row: ReaderNativeDataStore.Record
        let value: Object
        var mutationID: String { value["mutationId"] as! String }
        var queueKey: String { value["queueKey"] as! String }
        var timestamp: Double { (value["ts"] as? NSNumber)?.doubleValue ?? 0 }
        var operation: Object {
            ["mutationId": mutationID, "url": value["url"]!, "method": value["method"]!, "body": value["body"] ?? NSNull()]
        }
    }
    static let contract = "command-outbox/2"
    static let collection = "native-command-outbox"
    let store: ReaderNativeDataStore
    let namespace: String
    var now: () -> Int64 = { Int64(Date().timeIntervalSince1970 * 1000) }

    init(store: ReaderNativeDataStore, namespace: String) throws {
        guard namespace.range(of: "^acct-v1-[a-f0-9]{64}$", options: .regularExpression) != nil else {
            throw Failure(message: "待发送队列缺少有效账户")
        }
        self.store = store; self.namespace = namespace
    }
    private func key(_ id: String) -> String { namespace + ":" + id }
    private func bytes(_ value: Object) throws -> Data {
        guard JSONSerialization.isValidJSONObject(value) else { throw Failure(message: "待发送命令不是有效 JSON") }
        let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys])
        guard data.count <= 8 * 1024 * 1024 else { throw Failure(message: "待发送命令过大") }
        return data
    }
    static func route(_ raw: String, method: String) -> Bool {
        guard raw.utf16.count <= 2048, raw.hasPrefix("/pdf/api/"), !raw.contains("\\"),
              let url = URLComponents(string: raw), url.scheme == nil, url.host == nil, url.fragment == nil,
              !url.path.contains("/../"), !url.path.contains("/./"),
              url.percentEncodedPath == url.path else { return false }
        let routes: [String: Set<String>] = [
            "/pdf/api/lookup-event": ["POST"], "/pdf/api/vocab-mark": ["POST"], "/pdf/api/jp-vocab-mark": ["POST"],
            "/pdf/api/phrases": ["POST", "DELETE"], "/pdf/api/phrase-mark": ["POST"],
            "/pdf/api/highlights": ["POST", "PATCH", "DELETE"], "/pdf/api/notes": ["POST", "PATCH", "DELETE"],
            "/pdf/api/anki-add-cards": ["POST"], "/pdf/api/review-answer": ["POST"],
            "/pdf/api/review-event": ["POST"], "/pdf/api/reading-pos": ["POST"]]
        if routes[url.path]?.contains(method) == true { return true }
        return method == "PATCH" && url.path.range(of: "^/pdf/api/entity/[A-Za-z0-9_-]{1,160}$", options: .regularExpression) != nil
    }
    private func checked(_ value: Object) throws -> Object {
        guard value["settledDigest"] == nil,
              value["contract"] as? String == Self.contract, value["ownerNamespace"] as? String == namespace,
              let id = value["mutationId"] as? String, id.range(of: "^mut-v2-[a-f0-9]{32}$", options: .regularExpression) != nil,
              let queue = value["queueKey"] as? String, !queue.isEmpty, queue.utf16.count <= 1001,
              !queue.unicodeScalars.contains(where: { $0.value < 32 || $0.value == 127 }),
              let path = value["url"] as? String, let method = value["method"] as? String,
              Self.route(path, method: method), let type = value["recordType"] as? String,
              ["mutation", "dead-letter"].contains(type),
              let stamp = value["ts"] as? NSNumber, stamp.doubleValue.isFinite else {
            throw Failure(message: "待发送命令的账户、编号或端点无效")
        }
        if type == "dead-letter" {
            let status = (value["status"] as? NSNumber)?.intValue ?? 0
            let rejected = (400..<500).contains(status) && status != 429
            let previous = value["supersededByMutationId"] as? String ?? ""
            guard rejected || (status == 0 && value["reason"] as? String == "superseded-by-rejected-latest"
                && previous.range(of: "^mut-v2-[a-f0-9]{32}$", options: .regularExpression) != nil) else {
                throw Failure(message: "命令拒绝回执无效")
            }
        }
        _ = try bytes(value)
        return value
    }
    private func write(_ value: Object, previous: ReaderNativeDataStore.Record?, settled: Bool = false) throws {
        // Keep a small identity receipt after delivery, not a permanent second
        // copy of every note/card body. It still detects conflicting reimports.
        let stored: Object = settled ? ["contract": Self.contract, "ownerNamespace": namespace,
            "mutationId": value["mutationId"]!, "settledDigest": try digest(value)] : value
        let text = String(decoding: try bytes(stored), as: UTF8.self)
        let row = ReaderNativeDataStore.Record(collection: Self.collection, id: key(value["mutationId"] as! String),
            rev: (previous?.rev ?? 0) + 1, updatedAt: now(), deleted: settled, json: text)
        // The existing records table supplies atomicity and revisions. This
        // device transport state never becomes a synchronized learning record.
        _ = try store.commitWithinTransaction(record: row, mutationId: nil, journalJSON: nil,
            expectedRev: previous?.rev ?? 0, now: now())
    }
    /// Import only this account's v2 records. Legacy v1 remains quarantined.
    /// Retained tombstones prevent a still-present old WebKit copy replaying.
    func importRecords(_ originals: [Object]) throws {
        guard originals.count <= 128 else { throw Failure(message: "一次导入的待发送命令过多") }
        let values = try originals.map(checked)
        guard try values.reduce(0, { try $0 + bytes($1).count }) <= 8 * 1024 * 1024 else {
            throw Failure(message: "一次导入的待发送命令过大")
        }
        try store.inTransaction {
            for value in values {
                let id = value["mutationId"] as! String
                if let current = try store.record(collection: Self.collection, id: key(id)) {
                    guard let existing = try JSONSerialization.jsonObject(with: Data(current.json.utf8)) as? Object else {
                        throw Failure(message: "原生队列记录损坏，未覆盖")
                    }
                    guard try sameCommand(existing, value) else { throw Failure(message: "同一命令编号的内容发生变化") }
                    if current.deleted { continue }
                    if existing["recordType"] as? String == "dead-letter" { continue }
                    if value["recordType"] as? String == "dead-letter" { try write(value, previous: current) }
                } else { try write(value, previous: nil) }
            }
        }
    }
    private func digest(_ value: Object) throws -> String {
        let fields = ["contract", "ownerNamespace", "mutationId", "queueKey", "url", "method", "body", "ts"]
        let core = Dictionary(uniqueKeysWithValues: fields.map { ($0, value[$0] ?? NSNull()) })
        return SHA256.hash(data: try bytes(core)).map { String(format: "%02x", $0) }.joined()
    }
    private func sameCommand(_ a: Object, _ b: Object) throws -> Bool {
        let first = try (a["settledDigest"] as? String) ?? digest(a)
        return try first == digest(b)
    }
    @discardableResult
    func enqueue(_ record: Object) throws -> String {
        let value = try checked(record)
        guard value["recordType"] as? String == "mutation" else { throw Failure(message: "拒绝记录不能重新入队") }
        try store.inTransaction {
            let id = value["mutationId"] as! String
            if let current = try store.record(collection: Self.collection, id: key(id)) {
                guard let existing = try JSONSerialization.jsonObject(with: Data(current.json.utf8)) as? Object,
                      try sameCommand(existing, value) else { throw Failure(message: "同一命令编号的内容发生变化") }
            } else { try write(value, previous: nil) }
        }
        return value["mutationId"] as! String
    }
    func entries() throws -> [Entry] {
        try store.records(collection: Self.collection, idPrefix: namespace + ":", includeDeleted: false).compactMap { row in
            guard !row.deleted else { return nil }
            guard let value = try JSONSerialization.jsonObject(with: Data(row.json.utf8)) as? Object else {
                throw Failure(message: "待发送队列记录损坏")
            }
            return Entry(row: row, value: try checked(value))
        }
    }
    private func earlier(_ a: Entry, _ b: Entry) -> Bool {
        a.timestamp == b.timestamp ? a.mutationID < b.mutationID : a.timestamp < b.timestamp
    }
    func pending() throws -> [Entry] { try entries().filter { $0.value["recordType"] as? String == "mutation" } }
    func selected(_ snapshot: [Entry]) -> [Entry] {
        var latest: [String: Entry] = [:]
        for entry in snapshot {
            if let previous = latest[entry.queueKey], !earlier(previous, entry) { continue }
            latest[entry.queueKey] = entry
        }
        var result: [Entry] = []
        var size = 0
        for entry in latest.values.sorted(by: earlier).prefix(100) {
            let length = entry.row.json.utf8.count
            if size + length > 10 * 1024 * 1024 { break }
            size += length; result.append(entry)
        }
        return result
    }
    /// 2xx settles the captured command group. Terminal 4xx records rejection
    /// (and which newer rejected command superseded each older entry). Other
    /// statuses keep it pending. Entries queued during transport stay untouched.
    func acknowledge(_ selected: Entry, snapshot: [Entry], status: Int) throws {
        guard (200..<300).contains(status) || ((400..<500).contains(status) && status != 429) else { return }
        guard selected.value["ownerNamespace"] as? String == namespace else { throw Failure(message: "命令回执账户不匹配") }
        try store.inTransaction {
            guard let current = try store.record(collection: Self.collection, id: selected.row.id),
                  current == selected.row else { return }
            let group = snapshot.filter { $0.queueKey == selected.queueKey && !earlier(selected, $0) }
            for entry in group {
                guard entry.value["ownerNamespace"] as? String == namespace else { throw Failure(message: "队列快照账户不匹配") }
                guard let row = try store.record(collection: Self.collection, id: entry.row.id), row == entry.row else { continue }
                if (200..<300).contains(status) { try write(entry.value, previous: row, settled: true) }
                else {
                    var dead = entry.value
                    dead["recordType"] = "dead-letter"; dead["failedAt"] = now()
                    dead["status"] = entry.mutationID == selected.mutationID ? status : 0
                    if entry.mutationID != selected.mutationID {
                        dead["reason"] = "superseded-by-rejected-latest"; dead["supersededByMutationId"] = selected.mutationID
                    }
                    try write(dead, previous: row)
                }
            }
        }
    }
    func status() throws -> Object {
        let values = try entries()
        return ["contract": Self.contract, "ownerNamespace": namespace,
            "size": values.filter { $0.value["recordType"] as? String == "mutation" }.count,
            "deadLetterSize": values.filter { $0.value["recordType"] as? String == "dead-letter" }.count]
    }
}

/// Owns captured batches across asynchronous transport. The compatibility
/// caller supplies HTTP results, never rows to delete. Storage is independent
/// of the disposable device cache and is partitioned by account namespace.
final class ReaderNativeCommandOutboxPort {
    typealias Object = [String: Any]
    private struct Scope: Equatable {
        let namespace: String
        let context: String
        let generation: Int
    }
    private struct Batch {
        let scope: Scope
        let snapshot: [ReaderNativeCommandOutbox.Entry]
        let selected: [ReaderNativeCommandOutbox.Entry]
    }
    private let store: () throws -> ReaderNativeDataStore
    private var batches: [String: Batch] = [:]
    init(store: @escaping () throws -> ReaderNativeDataStore) { self.store = store }
    func invalidate() { batches.removeAll() }

    func handle(_ request: Object) throws -> Object {
        guard request["contract"] as? String == ReaderNativeCommandOutbox.contract,
              let lease = request["lease"] as? Object,
              lease["contract"] as? String == "account-context-lease/1",
              let namespace = lease["namespace"] as? String,
              let context = lease["contextId"] as? String, !context.isEmpty, context.utf16.count <= 128,
              let generation = lease["generation"] as? Int, generation >= 0,
              let action = request["operation"] as? String else {
            throw ReaderNativeCommandOutbox.Failure(message: "原生队列请求或账户租约无效")
        }
        let scope = Scope(namespace: namespace, context: context, generation: generation)
        let outbox = try ReaderNativeCommandOutbox(store: store(), namespace: namespace)
        var result: Object = ["ok": true, "contract": ReaderNativeCommandOutbox.contract,
            "ownerNamespace": namespace, "generation": generation]
        switch action {
        case "import":
            guard let records = request["records"] as? [Object] else {
                throw ReaderNativeCommandOutbox.Failure(message: "缺少待导入命令")
            }
            try outbox.importRecords(records)
            result["accepted"] = records.compactMap { $0["mutationId"] as? String }
        case "capture":
            // One outstanding batch per document. A new capture
            // abandons the older receipt token, but never its durable commands.
            batches.removeAll()
            let snapshot = try outbox.pending()
            let selected = outbox.selected(snapshot)
            let token = UUID().uuidString
            batches[token] = Batch(scope: scope, snapshot: snapshot, selected: selected)
            result["token"] = token
            result["ops"] = selected.map(\.operation)
        case "ack", "release":
            guard let token = request["token"] as? String, let batch = batches[token], batch.scope == scope else {
                throw ReaderNativeCommandOutbox.Failure(message: "待发送批次已失效或账户不匹配")
            }
            if action == "ack" {
                guard let statuses = request["statuses"] as? [Int], statuses.count == batch.selected.count,
                      statuses.allSatisfy({ (0...599).contains($0) }) else {
                    throw ReaderNativeCommandOutbox.Failure(message: "待发送批次回执不完整")
                }
                // Each group is atomic; successful earlier groups can remain
                // acknowledged if a later disk write fails. Retrying preserves
                // mutation IDs and never replays a settled group.
                for (entry, status) in zip(batch.selected, statuses) {
                    try outbox.acknowledge(entry, snapshot: batch.snapshot, status: status)
                }
            }
            batches.removeValue(forKey: token)
        case "status": break
        default: throw ReaderNativeCommandOutbox.Failure(message: "未知待发送队列操作")
        }
        result["state"] = try outbox.status()
        return result
    }
}
