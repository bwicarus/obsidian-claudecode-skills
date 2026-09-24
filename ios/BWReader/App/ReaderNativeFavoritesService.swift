import Foundation

/// The existing server collection remains authoritative. Swift owns the
/// session cache, validation and serialized commands; the transitional web
/// adapter only observes snapshots. An uncertain write is never retried.
@MainActor
final class ReaderNativeFavoritesService {
    struct Response { let status: Int; let data: Data }
    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }
    typealias Fetch = (String, String, Data?) async throws -> Response
    private let fetch: Fetch
    private let changed: ([[String: Any]]) -> Void
    private(set) var records: [[String: Any]] = []
    private(set) var trashRecords: [[String: Any]] = []
    private var loaded = false
    private(set) var projectionRevision = 0
    private var pending: [String: [String: Any]] = [:]
    private var tail: Task<Void, Never>?
    private var ticket = UUID()
    private var valid = true

    init(fetch: @escaping Fetch, changed: @escaping ([[String: Any]]) -> Void = { _ in }) {
        self.fetch = fetch; self.changed = changed
    }

    func invalidate() { valid = false; tail?.cancel() }

    func perform(_ operation: String, _ value: [String: Any] = [:]) async throws -> [String: Any] {
        let previous = tail, token = UUID()
        ticket = token
        let work = Task { @MainActor in
            await previous?.value
            try self.check()
            return try await self.run(operation, value)
        }
        tail = Task { _ = try? await work.value }
        defer { if ticket == token { tail = nil } }
        return try await work.value
    }

    private func check() throws {
        try Task.checkCancellation()
        guard valid else { throw Failure(message: "收藏夹所属会话已切换，请重新读取") }
    }

    private func request(_ path: String = "/api/assistant/voice-cards", body: [String: Any]? = nil) async throws -> (Response, [String: Any]) {
        try check()
        let response = try await fetch(path, body == nil ? "GET" : "POST", try body.map { try ReaderNativeCardRules.bytes($0) })
        try check()
        guard response.data.count <= 64 * 1024 * 1024,
              let object = try JSONSerialization.jsonObject(with: response.data) as? [String: Any] else {
            throw Failure(message: "收藏夹返回内容无效，未覆盖已有数据")
        }
        return (response, object)
    }

    private func read(_ trash: Bool, force: Bool = false) async throws -> [[String: Any]] {
        if !trash && loaded && !force { return records }
        let (response, body) = try await request(trash ? "/api/assistant/voice-cards?trash=1" : "/api/assistant/voice-cards")
        guard (200..<300).contains(response.status), body["ok"] as? Bool == true,
              let incoming = body["cards"] as? [[String: Any]], incoming.count <= 200 else {
            throw Failure(message: "收藏夹读取未获确认，已保留原清单")
        }
        var ids = Set<String>()
        for row in incoming {
            let id = try Self.identifier(row["id"])
            guard ids.insert(id).inserted else { throw Failure(message: "收藏夹含重复编号，未覆盖已有数据") }
        }
        if trash { trashRecords = incoming; return incoming }
        var result = incoming
        for (id, row) in pending {
            if let index = result.firstIndex(where: { $0["id"] as? String == id }) {
                let remote = result[index]
                // A read can resolve an uncertain write. Equal identity alone
                // is insufficient; compare the semantic content as well.
                if Self.sameContent(remote, row) || Self.revision(remote) > Self.revision(row) {
                    pending.removeValue(forKey: id)
                } else { result[index] = row }
            } else { result.append(row) }
        }
        records = result; loaded = true; publish()
        return records
    }

    private func run(_ operation: String, _ value: [String: Any]) async throws -> [String: Any] {
        switch operation {
        case "read", "trash":
            return ["ok": true, "cards": try await read(operation == "trash", force: value["refresh"] as? Bool == true)]
        case "save":
            guard let input = value["card"] as? [String: Any] else { throw Failure(message: "收藏内容缺失") }
            // A failed initial read must not fabricate an empty authoritative
            // list. A new card can still remain in this session as unconfirmed.
            if !loaded { _ = try? await read(false) }
            try check()
            let row = try Self.prepare(input, current: records)
            let id = try Self.identifier(row["id"])
            guard pending[id] == nil else { throw Failure(message: "这张收藏的上次保存结果未确认，请刷新核对；未重复发送") }
            guard pending.count < 200 else { throw Failure(message: "待确认收藏过多，请先刷新核对") }
            let previous = records.first { $0["id"] as? String == id }
            var staged = row; staged["pendingConfirmation"] = true
            pending[id] = staged; replace(staged)
            let response: Response, body: [String: Any]
            do { (response, body) = try await request(body: ["op": "add", "card": row]) }
            catch { throw Failure(message: "收藏已保留在当前会话，服务器保存结果未确认；请刷新核对") }
            if [400, 413, 409].contains(response.status) {
                pending.removeValue(forKey: id)
                records.removeAll { $0["id"] as? String == id }
                if let previous { records.append(previous) }
                publish()
                if response.status == 409 { _ = try? await read(false, force: true) }
                throw Failure(message: response.status == 409 ? "收藏版本已变化，已重新读取；未覆盖较新内容" : "服务器拒绝了收藏内容，未标记为保存成功")
            }
            guard (200..<300).contains(response.status), body["ok"] as? Bool == true, body["id"] as? String == id else {
                throw Failure(message: "收藏保存未获确认，已保留当前内容；未重复发送")
            }
            pending.removeValue(forKey: id); replace(row)
            return ["ok": true, "id": id, "cards": records]
        case "delete":
            guard let input = value["ids"] as? [String], !input.isEmpty, input.count <= 200 else { throw Failure(message: "删除编号无效") }
            let ids = try Set(input.map { try Self.identifier($0) })
            let (response, body) = try await request(body: ["op": "del", "ids": ids.sorted()])
            try Self.confirm(response, body)
            records.removeAll { ids.contains($0["id"] as? String ?? "") }
            for id in ids { pending.removeValue(forKey: id) }
            publish()
            return ["ok": true, "cards": records]
        case "restore":
            let id = try Self.identifier(value["id"])
            let (response, body) = try await request(body: ["op": "restore", "id": id])
            try Self.confirm(response, body)
            if let row = trashRecords.first(where: { $0["id"] as? String == id }) {
                var restored = row; restored.removeValue(forKey: "deleted"); replace(restored)
            }
            trashRecords.removeAll { $0["id"] as? String == id }
            loaded = false // Next read refreshes, without resending restore.
            return ["ok": true, "cards": records]
        default: throw Failure(message: "收藏操作无效")
        }
    }

    private func replace(_ row: [String: Any]) {
        let id = row["id"] as? String
        if let index = records.firstIndex(where: { $0["id"] as? String == id }) { records[index] = row }
        else { records.append(row) }
        publish()
    }

    private func publish() { projectionRevision += 1; changed(records) }

    private static func confirm(_ response: Response, _ body: [String: Any]) throws {
        guard (200..<300).contains(response.status), body["ok"] as? Bool == true else {
            throw Failure(message: "收藏操作未获服务器确认，未重试或清空本机清单")
        }
    }

    static func identifier(_ value: Any?) throws -> String {
        guard let id = value as? String, !id.isEmpty, id.utf8.count <= 80,
              id.unicodeScalars.allSatisfy({ (48...57).contains($0.value) || (65...90).contains($0.value) ||
                  (97...122).contains($0.value) || $0 == "_" || $0 == "-" }) else {
            throw Failure(message: "收藏编号无效")
        }
        return id
    }

    static func revision(_ row: [String: Any]) -> Int64 {
        (try? ReaderNativeCardRules.integer(row["revision"] ?? 0, "revision")) ?? 0
    }

    static func prepare(_ input: [String: Any], current: [[String: Any]], now: Double = Date().timeIntervalSince1970) throws -> [String: Any] {
        var row = input
        let isCards = row["kind"] as? String == "cards" || !(row["gid"] as? String ?? "").isEmpty ||
            (row["payload"] as? [String: Any])?["kind"] as? String == "cards"
        let fallback = "c" + UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        func supplied(_ key: String) -> String? { (row[key] as? String).flatMap { $0.isEmpty ? nil : $0 } }
        let cid = try identifier(supplied("cid") ?? supplied("gid") ?? fallback)
        row["cid"] = cid
        if isCards {
            let gid = try identifier(supplied("gid") ?? cid)
            let id = try identifier(supplied("id") ?? gid)
            var payload = row["payload"] as? [String: Any]
            if row["payload"] == nil || row["payload"] is NSNull {
                let raw: Any?
                if let text = row["raw"] as? String { raw = try? JSONSerialization.jsonObject(with: Data(text.utf8)) }
                else { raw = row["raw"] }
                payload = ["version": 1, "kind": "cards", "cards": raw ?? NSNull()]
            }
            guard let payload, Set(payload.keys) == Set(["version", "kind", "cards"]),
                  (try? ReaderNativeCardRules.bool(payload["version"], "version")) == nil,
                  (try? ReaderNativeCardRules.integer(payload["version"], "version")) == 1,
                  payload["kind"] as? String == "cards", let cards = payload["cards"] as? [[String: Any]],
                  !cards.isEmpty, cards.count <= 64 else { throw Failure(message: "学习卡收藏数据不完整") }
            var nodes = 0
            try validateTree(payload, depth: 0, nodes: &nodes)
            guard try ReaderNativeCardRules.bytes(payload).count <= 256 * 1024 else { throw Failure(message: "学习卡收藏超过大小限制") }
            let old = current.first { $0["id"] as? String == id } ?? [:]
            let next = max(revision(row), revision(old)) + 1
            guard next <= 9_007_199_254_740_991 else { throw Failure(message: "收藏修订号无效") }
            row["id"] = id; row["gid"] = gid; row["kind"] = "cards"; row["payload"] = payload
            row["revision"] = next; row.removeValue(forKey: "raw")
        } else {
            let id = try identifier(supplied("id") ?? ("v" + UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()))
            guard id.utf8.count <= 40 else { throw Failure(message: "文本收藏编号超出原协议限制") }
            row["id"] = id
            row["raw"] = scalarPrefix(row["raw"] as? String ?? "", 20_000)
        }
        // Match the existing server's field bounds; preserve the complete
        // structured learning payload instead of a rendered/front-only copy.
        let label = row["label"] as? String ?? ""
        row["label"] = scalarPrefix(label.isEmpty ? "卡片" : label, 80)
        row["kind"] = scalarPrefix(row["kind"] as? String ?? "", 24)
        row["text"] = scalarPrefix(row["text"] as? String ?? "", 4_000)
        row["isHtml"] = row["isHtml"] as? Bool ?? false
        let meta = row["meta"] as? [String: Any] ?? [:]
        row["meta"] = Dictionary(uniqueKeysWithValues: ["file", "page", "q"].map {
            ($0, scalarPrefix(ReaderNativeCardRules.string(meta[$0]), 300))
        })
        row["ts"] = now; row.removeValue(forKey: "deleted"); row.removeValue(forKey: "pendingConfirmation")
        return row
    }

    private static func scalarPrefix(_ text: String, _ count: Int) -> String { String(String.UnicodeScalarView(text.unicodeScalars.prefix(count))) }

    private static func validateTree(_ value: Any, depth: Int, nodes: inout Int) throws {
        nodes += 1
        guard nodes <= 8192, depth <= 16 else { throw Failure(message: "学习卡结构超过限制") }
        if let dict = value as? [String: Any] { for child in dict.values { try validateTree(child, depth: depth + 1, nodes: &nodes) } }
        else if let array = value as? [Any] { for child in array { try validateTree(child, depth: depth + 1, nodes: &nodes) } }
        else if value is String || value is NSNull { }
        else if let number = value as? NSNumber, number.doubleValue.isFinite { }
        else { throw Failure(message: "学习卡包含无法保存的数据") }
    }

    private static func sameContent(_ a: [String: Any], _ b: [String: Any]) -> Bool {
        let keys = ["id", "cid", "gid", "kind", "label", "raw", "payload", "text", "isHtml", "meta", "revision"]
        return ReaderNativeCardRules.same(a.filter { keys.contains($0.key) }, b.filter { keys.contains($0.key) })
    }

    static func presentation(_ row: [String: Any], pinned: Bool = false) -> [String: Any] {
        let cards = (row["payload"] as? [String: Any])?["cards"] as? [[String: Any]]
        let meta = row["meta"] as? [String: Any] ?? [:]
        return ["id": row["id"] ?? "", "label": row["label"] ?? "收藏卡片",
                "kind": cards == nil ? "html" : "cards", "isHtml": row["isHtml"] as? Bool ?? false,
                "text": row["text"] ?? "", "content": row["raw"] ?? "", "cards": cards ?? [],
                "page": ReaderNativeCardRules.string(meta["page"]), "file": meta["file"] ?? "",
                "ts": row["ts"] ?? 0, "pinned": pinned,
                "pendingConfirmation": row["pendingConfirmation"] as? Bool ?? false]
    }
}
