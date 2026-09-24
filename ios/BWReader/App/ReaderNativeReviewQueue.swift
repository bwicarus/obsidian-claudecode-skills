import Foundation

/// Queue acquisition and its one device-local recovery snapshot. Reader cards
/// remain canonical in CardRepository; remote cards retain server identities.
/// No view nodes, schedulers or new card records are created by this owner.
@MainActor
final class ReaderNativeReviewQueue {
    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }
    struct Response { let status: Int; let data: Data }
    typealias Object = [String: Any]
    typealias Fetch = (String, String, Data) async throws -> Response
    private let local: () throws -> Object
    private let read: () throws -> String?
    private let write: (String) throws -> Void
    private let fetch: Fetch
    private let now: () -> Double
    private var lease = ""
    private var contextKey = ""
    private var identity = Data()
    private var scope = "current"
    private var task: Task<Response, Error>?
    private var cancelled = false
    static let cacheKey = "native-review-queue-v1"

    init(local: @escaping () throws -> Object, read: @escaping () throws -> String?,
         write: @escaping (String) throws -> Void, fetch: @escaping Fetch,
         now: @escaping () -> Double = { Date().timeIntervalSince1970 * 1000 }) {
        self.local = local; self.read = read; self.write = write; self.fetch = fetch; self.now = now
    }
    func invalidate() {
        task?.cancel(); task = nil; lease = ""; contextKey = ""; identity = Data()
    }
    func cancel(_ id: String) {
        if lease == id { cancelled = true; task?.cancel(); task = nil }
    }
    private func current(_ id: String) throws {
        try Task.checkCancellation()
        guard lease == id, !cancelled else { throw CancellationError() }
    }
    private static func bytes(_ value: Object) throws -> Data {
        guard JSONSerialization.isValidJSONObject(value) else { throw Failure(message: "复习数据不是有效 JSON") }
        let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .fragmentsAllowed])
        guard data.count <= 8 * 1024 * 1024 else { throw Failure(message: "复习批次过大") }
        return data
    }
    private static func count(_ value: Any?, maximum: Int = 1_000_000_000) throws -> Int {
        let number = try ReaderNativeCardRules.integer(value ?? 0, "review count")
        guard number >= 0, number <= maximum else { throw Failure(message: "复习数量无效") }
        return Int(number)
    }
    static func snapshot(_ raw: Object) throws -> Object {
        guard let key = raw["client_context_key"] as? String, !key.isEmpty, key.utf16.count <= 240,
              let cards = raw["cards"] as? [Object], cards.count <= 200,
              let completed = raw["completed_ids"] as? [Any], completed.count <= 100 else {
            throw Failure(message: "复习快照不完整，未覆盖旧数据")
        }
        let ids: [Any] = try completed.map { item in
            if let string = item as? String, !string.isEmpty, string.utf16.count <= 240 { return string }
            let number = try ReaderNativeCardRules.integer(item, "completed card id")
            guard number > 0 else { throw Failure(message: "已完成卡号无效") }; return number
        }
        let stamp = try ReaderNativeCardRules.number(raw["ts"], "review timestamp")
        guard stamp >= 0 else { throw Failure(message: "复习时间无效") }
        let index = try count(raw["index"], maximum: 200)
        let result: Object = ["ts": stamp, "client_context_key": key, "cards": cards,
            "index": min(index, max(0, cards.count - 1)), "completed_ids": ids,
            "due_total": try count(raw["due_total"]), "related_total": try count(raw["related_total"])]
        _ = try bytes(result); return result
    }
    private func cached() throws -> Object? {
        guard let text = try read(), text.utf8.count <= 8 * 1024 * 1024,
              let record = try JSONSerialization.jsonObject(with: Data(text.utf8)) as? Object,
              record["contract"] as? String == "native-review-queue/1",
              let savedIdentity = record["identity"] as? String, savedIdentity == identity.base64EncodedString(),
              let value = record["snapshot"] as? Object else { return nil }
        return try Self.snapshot(value)
    }
    func peek() throws -> Object? { try cached() }
    @discardableResult
    func save(_ value: Object, request: String) throws -> Bool {
        // Late save chains from another book, scope or load are observations,
        // not a reason to overwrite the active recovery snapshot.
        guard !lease.isEmpty, request == lease,
              value["client_context_key"] as? String == contextKey else { return false }
        let snapshot = try Self.snapshot(value)
        let record: Object = ["contract": "native-review-queue/1", "identity": identity.base64EncodedString(), "snapshot": snapshot]
        try write(String(decoding: Self.bytes(record), as: UTF8.self)); return true
    }
    private func request(_ path: String, method: String = "GET", body: Object? = nil, id: String) async throws -> Object {
        try current(id)
        let data = try body.map(Self.bytes) ?? Data()
        let work = Task { [fetch] in try await fetch(path, method, data) }; task = work
        defer { if lease == id { task = nil } }
        let response = try await work.value
        try current(id)
        guard response.data.count <= 8 * 1024 * 1024,
              let result = try JSONSerialization.jsonObject(with: response.data) as? Object,
              (200..<300).contains(response.status), result["ok"] as? Bool == true,
              let cards = result["cards"] as? [Object], cards.count <= 200 else {
            throw Failure(message: "复习服务返回失败或不完整数据")
        }
        return result
    }
    func load(_ input: Object) async throws -> Object {
        guard let id = input["request"] as? String, UUID(uuidString: id) != nil,
              let key = input["contextKey"] as? String, !key.isEmpty, key.utf16.count <= 240,
              let requestedScope = input["scope"] as? String, ["all", "current"].contains(requestedScope),
              let context = input["context"] as? Object, let force = input["force"] as? Bool else {
            throw Failure(message: "复习请求缺少上下文或轮次")
        }
        guard try Self.bytes(context).count <= 32 * 1024 else { throw Failure(message: "复习上下文过大") }
        task?.cancel(); task = nil; cancelled = false; lease = id; contextKey = key; scope = requestedScope
        identity = try Self.bytes(["scope": scope, "context": context])
        func preparedLocal() throws -> Object? {
            let result = try local()
            guard let hasLocal = result["hasLocalCards"] as? Bool,
                  let entries = result["entries"] as? [Object], entries.count <= 30 else {
                throw Failure(message: "本机卡库返回无效数据")
            }
            let due = try Self.count(result["dueTotal"])
            return hasLocal ? ["kind": "local", "entries": entries, "dueTotal": due, "request": id] : nil
        }
        // A corrupt/unavailable local repository must never become a remote
        // fallback, including the valid case of an empty local due queue.
        if let result = try preparedLocal() { return result }
        var cache = try cached()
        if cache == nil, scope == "current", let old = input["legacyCache"] as? Object,
           old["client_context_key"] as? String == key {
            cache = try Self.snapshot(old)
        }
        let age = cache.map { now() - (($0["ts"] as? NSNumber)?.doubleValue ?? 0) } ?? .infinity
        let rejected = Set((input["rejectedIds"] as? [String] ?? []).prefix(200))
        let completed: [Any] = age >= 0 && age < 12 * 60 * 60 * 1000
            ? cache?["completed_ids"] as? [Any] ?? [] : []
        let filtered = completed.filter { !rejected.contains(String(describing: $0)) }
        func finish(_ data: Object, kind: String, notice: String = "", trimRelated: Bool = false) throws -> Object {
            try current(id)
            // Local cards might have arrived while a remote request awaited.
            if let result = try preparedLocal() { return result }
            var cards = data["cards"] as? [Object] ?? []
            let related = try Self.count(data["related_total"])
            if trimRelated, data["related_total"] != nil, !(data["related_total"] is NSNull) { cards = Array(cards.prefix(related)) }
            let snapshot: Object = ["ts": now(), "client_context_key": key, "cards": cards,
                "index": data["index"] ?? 0, "due_total": try Self.count(data["due_total"]),
                "related_total": related, "completed_ids": filtered]
            try save(snapshot, request: id)
            return ["kind": kind, "snapshot": snapshot, "request": id, "notice": notice]
        }
        if !force, age >= 0, age < 30 * 60 * 1000, let cache, !(cache["cards"] as? [Any] ?? []).isEmpty {
            return try finish(cache, kind: "cache")
        }
        let hasContext = scope == "current" && ["file", "url", "source_ref", "selection", "visible_text"].contains {
            !(context[$0] as? String ?? "").isEmpty
        }
        var result: Object
        var kind = "load", notice = "", trim = hasContext
        do {
            result = try await request(hasContext ? "/pdf/api/review-queue" : "/pdf/api/review-queue?limit=30",
                method: hasContext ? "POST" : "GET", body: hasContext ? ["limit": 30, "context": context, "exclude_card_ids": filtered] : nil, id: id)
        } catch {
            try current(id)
            var fallback: Object?
            if hasContext {
                do {
                    fallback = try await request("/pdf/api/review-queue?limit=30", id: id)
                    fallback?["related_total"] = 0
                } catch { try current(id) }
            }
            trim = false
            if let fallback { result = fallback; kind = "fallback"; notice = "相关卡暂不可用，已退回到期卡" }
            else if let cache, !(cache["cards"] as? [Any] ?? []).isEmpty {
                result = cache; kind = "offline"; notice = "离线：使用当前内容的本机复习快照"
            } else { throw error }
        }
        // Database failures are not transport failures. Do not restart a GET
        // or revive an older snapshot if this final canonical check/save fails.
        return try finish(result, kind: kind, notice: notice, trimRelated: trim)
    }
}
