import Foundation
import CoreFoundation
import CryptoKit

/// Native book mutations, using the existing record/journal schema. The main
/// record, derived indexes and retry receipt either all commit or all roll back.
struct ReaderNativeBookStore {
    enum MutationError: LocalizedError {
        case invalid(String), replayConflict, unavailable
        var errorDescription: String? {
            switch self {
            case .invalid(let detail): return "书籍数据无效：" + detail
            case .replayConflict: return "同一操作编号已用于不同内容，未重复写入"
            case .unavailable: return "原生书籍数据库尚未准备好"
            }
        }
    }
    let store: ReaderNativeDataStore
    let bookID: String
    let deviceID: String
    var now: () -> Int64 = { Int64(Date().timeIntervalSince1970 * 1000) }
    var displayName: String? = nil
    var contentSHA256: String? = nil
    private var projection: ReaderNativeBookProjection { .init(store: store) }

    func perform(_ request: [String: Any]) throws -> [String: Any] {
        guard !bookID.isEmpty, !deviceID.isEmpty, deviceID.utf16.count <= 240,
              let mutation = request["mutationId"] as? String, !mutation.isEmpty, mutation.utf16.count <= 240,
              let operation = request["operation"] as? String, request["bookID"] as? String == bookID,
              let value = request["value"], JSONSerialization.isValidJSONObject(request) else { throw MutationError.invalid("操作身份") }
        let key = "native-book:\(bookID.utf16.count):\(bookID):\(mutation)"
        let fingerprint = SHA256.hash(data: try Self.bytes(request)).map { String(format: "%02x", $0) }.joined()
        let stamp = now()
        return try store.inTransaction {
            if let previous = try store.mutationResult(mutationId: key) {
                guard let saved = try JSONSerialization.jsonObject(with: Data(previous.utf8)) as? [String: Any],
                      saved["fingerprint"] as? String == fingerprint,
                      var receipt = saved["receipt"] as? [String: Any] else { throw MutationError.replayConflict }
                receipt["replayed"] = true
                return receipt
            }
            let expected: Int64?
            if let raw = request["expectedRevision"] {
                guard let number = raw as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
                      number.doubleValue.isFinite, number.doubleValue.rounded() == number.doubleValue,
                      number.doubleValue >= 0, number.doubleValue <= 9_007_199_254_740_991 else { throw MutationError.invalid("预期修订号") }
                expected = number.int64Value
            } else {
                guard ["reading-position", "note-api", "note-operation", "replication-enqueue", "ink-operation", "ink-sync"].contains(operation) else { throw MutationError.invalid("缺少预期修订号") }
                expected = nil
            }
            let revision: Int64
            var result: [String: Any]? = nil
            var bindingChanges: [[String: Any]] = []
            switch operation {
            case "ink-operation":
                guard let input = value as? [String: Any] else { throw MutationError.invalid("手写操作") }
                let outcome = try mutateInk(input, mutation: mutation, at: stamp)
                revision = outcome.revision; result = outcome.result
            case "ink-sync":
                revision = try flushInk(mutation: mutation, at: stamp)
                result = ["ok": true]
            case "replication-enqueue":
                guard let command = value as? [String: Any] else { throw MutationError.invalid("复制命令") }
                let queued = try enqueueReplication(command, mutation: mutation, at: stamp)
                revision = queued.revision
                result = ["ok": true, "queued": queued.queued]
            case "note-api", "note-operation":
                guard let api = value as? [String: Any] else { throw MutationError.invalid("便签请求") }
                let state = try projection.state("document-notes-legacy", bookID: bookID)
                guard state.payload == nil || state.payload is [[String: Any]] else { throw MutationError.invalid("便签数据损坏") }
                let notes = state.payload as? [[String: Any]] ?? []
                let method: String, body: [String:Any]
                if operation == "note-operation" {
                    guard let id = api["id"] as? String, let note = notes.first(where: { $0["id"] as? String == id }) else {
                        throw ReaderNativeNoteRules.NoteError.missing
                    }
                    let plan = try ReaderNativeNoteActions.request(api,note:note,file:"localbook:" + bookID,now:stamp)
                    method = plan.method; body = plan.body
                } else {
                    guard let verb = api["method"] as? String, let fields = api["body"] as? [String:Any] else { throw MutationError.invalid("便签请求") }
                    method = verb; body = fields
                }
                let outcome = try ReaderNativeNoteRules.apply(method: method, body: body, notes: notes,
                    file: "localbook:" + bookID, now: stamp,
                    newID: { "n" + UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased().prefix(11) })
                revision = try writeNotes(outcome.notes, expected: state.revision, mutation: mutation, at: stamp)
                result = outcome.result
                if operation == "note-operation" {
                    _ = try enqueueReplication(["url":"/pdf/api/notes","method":method,"body":body],mutation:mutation + ":replication",at:stamp)
                }
                let before = Dictionary(Self.wordBindings(notes).map { ($0["cid"] as! String, $0["key"] as! String) }, uniquingKeysWith: { _, last in last })
                let after = Dictionary(Self.wordBindings(outcome.notes).map { ($0["cid"] as! String, $0["key"] as! String) }, uniquingKeysWith: { _, last in last })
                bindingChanges = Set(before.keys).union(after.keys).sorted().compactMap { id in
                    guard before[id] != after[id] else { return nil }
                    return ["cid": id, "before": before[id] ?? "", "after": after[id] ?? ""]
                }
            case "notes":
                guard let notes = value as? [[String: Any]] else { throw MutationError.invalid("注解列表") }
                var ids = Set<String>()
                for note in notes {
                    guard let id = note["id"] as? String, !id.isEmpty, id.utf16.count <= 240,
                          ids.insert(id).inserted else { throw MutationError.invalid("注解编号") }
                }
                revision = try writeNotes(notes, expected: expected, mutation: mutation, at: stamp)
            case "pdf-highlights", "epub-highlights":
                guard let items = value as? [[String: Any]] else { throw MutationError.invalid("划线列表") }
                revision = try writeHighlights(operation == "pdf-highlights" ? "document-highlights" : "epub-highlights",
                                               items: items, expected: expected, mutation: mutation, at: stamp)
            case "reading-position":
                guard value is [String: Any] else { throw MutationError.invalid("阅读位置") }
                revision = try writeState(operation, value: value, expected: expected, mutation: mutation, at: stamp)
            case "ink", "epub-ink":
                guard value is [String: [[String: Any]]] else { throw MutationError.invalid("墨迹") }
                revision = try writeState(operation, value: value, expected: expected, mutation: mutation, at: stamp)
            default: throw MutationError.invalid("未登记的操作")
            }
            var receipt: [String: Any] = ["ok": true, "bookID": bookID, "mutationId": mutation, "revision": revision]
            if let result { receipt["result"] = result; receipt["bindingChanges"] = bindingChanges }
            try store.rememberMutationWithinTransaction(key, json: Self.string(["fingerprint": fingerprint, "receipt": receipt]), now: stamp)
            return receipt
        }
    }

    /// Reading the latest strokes and saving undo, ink and the deferred sync
    /// marker are one transaction. Unknown replies are safe to retry by ID.
    private func mutateInk(_ input: [String: Any], mutation: String, at: Int64) throws -> (revision: Int64, result: [String: Any]) {
        guard let action = input["action"] as? String else { throw MutationError.invalid("手写动作") }
        let state = try projection.state("ink", bookID: bookID)
        guard state.payload == nil || state.payload is NSNull || state.payload is [String: [[String: Any]]] else { throw MutationError.invalid("笔迹记录损坏") }
        let original = state.payload as? [String: [[String: Any]]] ?? [:]
        var output = original, before: [String: [[String: Any]]] = [:]
        var written = 0, removed = 0
        if ["undo", "redo", "clear"].contains(action) {
            guard let page = input["page"] as? NSNumber, CFGetTypeID(page) != CFBooleanGetTypeID(),
                  page.doubleValue > 0, page.doubleValue < 10_000_000,
                  page.doubleValue.rounded() == page.doubleValue else { throw MutationError.invalid("手写页码") }
            let key = String(page.intValue), current = original[key] ?? []
            var history = try inkHistory(page: key, matching: current)
            var undo = history["undo"] as? [[[String: Any]]] ?? [], redo = history["redo"] as? [[[String: Any]]] ?? []
            let next: [[String: Any]]
            if action == "undo" {
                guard let value = undo.popLast() else { return (state.revision, ["ok":true,"persisted":true,"changes":[]]) }
                redo.append(current); next = value
            } else if action == "redo" {
                guard let value = redo.popLast() else { return (state.revision, ["ok":true,"persisted":true,"changes":[]]) }
                undo.append(current); next = value
            } else { undo.append(current); redo = []; next = [] }
            before[key] = current
            if next.isEmpty { output.removeValue(forKey:key) } else { output[key] = next }
            history = ["undo":undo,"redo":redo,"current":next]
            try writeInkHistory(history, page:key, mutation:mutation, at:at)
        } else {
            let outcome = try ReaderNativeStrokeRules.apply(action:action, input:input, surfaces:original, now:at)
            output = outcome.surfaces; before = outcome.before; written = outcome.written; removed = outcome.removed
            for page in before.keys.sorted() {
                let prior = before[page]!, next = output[page] ?? []
                if try Self.bytes(prior) == Self.bytes(next) { continue }
                var history = try inkHistory(page:page, matching:prior)
                var undo = history["undo"] as? [[[String: Any]]] ?? []
                undo.append(prior); history = ["undo":undo,"redo":[[[String:Any]]](),"current":next]
                try writeInkHistory(history, page:page, mutation:mutation, at:at)
            }
        }
        let changes: [[String:Any]] = try before.keys.sorted().compactMap { page in
            let next = output[page] ?? []
            return try Self.bytes(before[page]!) == Self.bytes(next) ? nil : ["page":Int(page)!,"strokes":next]
        }
        if !changes.isEmpty {
            var pending = try pendingInk()
            for change in changes { pending[String(change["page"] as! Int)] = at + 60_000 }
            try writeState("ink-pending", value:pending, mutation:mutation + ":pending", at:at)
        }
        let revision = changes.isEmpty ? state.revision : try writeState("ink", value:output, expected:state.revision, mutation:mutation, at:at)
        return (revision, ["ok":true,"persisted":true,"written":written,"removed":removed,"changes":changes])
    }

    private func inkHistory(page: String, matching strokes: [[String:Any]]) throws -> [String:Any] {
        let value = try projection.state("ink-history-" + page, bookID:bookID).payload as? [String:Any]
        // Cloud imports and page mutations may replace strokes; do not let an
        // old local undo stack overwrite those changes.
        guard let value, let current = value["current"] as? [[String:Any]],
              try Self.bytes(current) == Self.bytes(strokes) else { return [:] }
        return value
    }

    private func writeInkHistory(_ input: [String:Any], page:String, mutation:String, at:Int64) throws {
        var value = input
        var undo = Array((value["undo"] as? [[[String:Any]]] ?? []).suffix(40))
        var redo = Array((value["redo"] as? [[[String:Any]]] ?? []).suffix(40))
        while true {
            value["undo"] = undo; value["redo"] = redo
            if try Self.bytes(value).count <= 8 * 1024 * 1024 || (undo.isEmpty && redo.isEmpty) { break }
            if undo.count >= redo.count && !undo.isEmpty { undo.removeFirst() } else { redo.removeFirst() }
        }
        try writeState("ink-history-" + page, value:value, mutation:mutation + ":history:" + page, at:at)
    }

    private func pendingInk() throws -> [String:Int64] {
        guard let value = try projection.state("ink-pending", bookID:bookID).payload else { return [:] }
        guard let rows = value as? [String:NSNumber] else { throw MutationError.invalid("笔迹待同步记录损坏") }
        var result: [String:Int64] = [:]
        for (key, n) in rows {
            guard key.range(of:"^[1-9][0-9]{0,7}$",options:.regularExpression) != nil,
                  CFGetTypeID(n) != CFBooleanGetTypeID(), n.doubleValue.isFinite,
                  n.doubleValue >= 0, n.doubleValue <= 9_007_199_254_740_991,
                  n.doubleValue.rounded() == n.doubleValue else { throw MutationError.invalid("笔迹待同步时间") }
            result[key] = n.int64Value
        }
        return result
    }

    func nextInkSyncTime() throws -> Int64? { try pendingInk().values.min() }

    private func flushInk(mutation:String, at:Int64) throws -> Int64 {
        var pending = try pendingInk()
        let due = pending.keys.filter { pending[$0]! <= at }.sorted()
        guard !due.isEmpty else { return 0 }
        let state = try projection.state("ink", bookID:bookID)
        guard state.payload == nil || state.payload is NSNull || state.payload is [String:[[String:Any]]] else { throw MutationError.invalid("笔迹记录损坏") }
        let strokes = state.payload as? [String:[[String:Any]]] ?? [:]
        for page in due {
            _ = try enqueueReplication(["url":"/pdf/api/ink","method":"POST","body":["file":"localbook:" + bookID,
                "page":Int(page)!,"strokes":strokes[page] ?? []]], mutation:mutation + ":ink:" + page, at:at)
            pending.removeValue(forKey:page)
        }
        return try writeState("ink-pending",value:pending,mutation:mutation + ":settled",at:at)
    }

    /// The durable outbox uses the existing wire protocol. Creating the book
    /// link and its pair announcement is atomic with the first command, so a
    /// crash cannot leave an unannounced identity or an unrepeatable command.
    private func enqueueReplication(_ command: [String: Any], mutation: String, at: Int64) throws -> (revision: Int64, queued: Bool) {
        let routes: [String: Set<String>] = [
            "/pdf/api/notes": ["POST", "PATCH", "DELETE"],
            "/pdf/api/highlights": ["POST", "PATCH", "DELETE"],
            "/pdf/api/epub-highlights": ["POST", "PATCH", "DELETE"],
            "/pdf/api/userpages": ["POST", "PATCH", "DELETE"],
            "/pdf/api/ink": ["POST"], "/pdf/api/epub-ink": ["POST"],
            "/pdf/api/reading-pos": ["POST"], "/replication/activity": ["POST"],
            "/replication/diagnostic": ["POST"], "/replication/resync": ["POST"],
            "/replication/notification": ["POST"]
        ]
        guard Set(command.keys) == Set(["url", "method", "body"]),
              let url = command["url"] as? String, let method = command["method"] as? String,
              routes[url]?.contains(method) == true, let body = command["body"] as? [String: Any] else {
            throw MutationError.invalid("复制路由或参数")
        }
        if let file = body["file"], file as? String != "localbook:" + bookID { throw MutationError.invalid("复制书籍不匹配") }
        guard bookID != "localbook-welcome" else { return (0, false) }
        let existing = try projection.state("replication-link", bookID: bookID)
        let replicationID: String
        let needsPair: Bool
        if let payload = existing.payload {
            guard let link = payload as? [String: Any], let id = link["replicationBookId"] as? String,
                  id.range(of: "^repbook-[a-f0-9]{32}$", options: .regularExpression) != nil else {
                throw MutationError.invalid("复制配对记录损坏")
            }
            replicationID = id; needsPair = false
        } else { replicationID = "repbook-" + Self.uuid(); needsPair = true }
        func envelope(_ path: String, _ verb: String, _ payload: [String: Any]) -> [String: Any] {
            ["contract": "replication-command/1", "deviceId": deviceID,
             "replicationBookId": replicationID, "actor": "user",
             "op": ["mutationId": "mut-v2-" + Self.uuid(), "url": path, "method": verb, "body": payload]]
        }
        let message = envelope(url, method, body)
        guard try Self.bytes(message).count <= 5 * 1024 * 1024 else { throw MutationError.invalid("复制命令超过信封上限") }
        func append(_ message: [String: Any], suffix: String) throws -> Int64 {
            // Persistent counter is scoped to this database and never resets on
            // reload. Native IDs cannot collide with the legacy JS counter.
            let previous = Int64(try store.meta("nativeReplicationSequence") ?? "0") ?? 0
            guard previous >= 0 && previous < Int64.max, at >= 0 else { throw MutationError.invalid("复制队列序号") }
            let sequence = previous + 1
            try store.putMeta("nativeReplicationSequence", json: String(sequence))
            let rowID = bookID + ":ro-" + String(format: "%012llx-n%016llx", at, sequence)
            return try write(collection: "native-replication-outbox", id: rowID,
                payload: ["envelope": message], expected: 0, mutation: mutation + suffix, at: at)
        }
        if needsPair {
            try writeState("replication-link", value: ["replicationBookId": replicationID, "pairedAt": at / 1000],
                           expected: existing.revision, mutation: mutation + ":link", at: at)
            var pair: [String: Any] = ["peerBookId": bookID, "replicationBookId": replicationID,
                                      "displayName": String((displayName.flatMap { $0.isEmpty ? nil : $0 } ?? bookID).prefix(512))]
            if let sha = contentSHA256, sha.range(of: "^[a-f0-9]{64}$", options: .regularExpression) != nil { pair["contentSha256"] = sha }
            _ = try append(envelope("/replication/pair", "POST", pair), suffix: ":pair")
        }
        return (try append(message, suffix: ":command"), true)
    }

    private static func uuid() -> String { UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased() }

    private func writeNotes(_ notes: [[String: Any]], expected: Int64?, mutation: String, at: Int64) throws -> Int64 {
        let revision = try writeState("document-notes-legacy", value: notes, expected: expected, mutation: mutation + ":notes", at: at)
        let placements = Self.placements(notes)
        try writeState("card-placements", value: placements, mutation: mutation + ":placements", at: at)
        try writeState("entity-references", value: Self.references(placements), mutation: mutation + ":references", at: at)
        try writeState("word-bindings", value: Self.wordBindings(notes), mutation: mutation + ":words", at: at)
        return revision
    }

    @discardableResult
    private func writeState(_ kind: String, value: Any, expected: Int64? = nil, mutation: String, at: Int64) throws -> Int64 {
        try write(collection: "native-" + kind, id: bookID + ":" + kind, payload: value,
                  expected: expected, mutation: mutation, at: at)
    }
    @discardableResult
    private func write(collection: String, id: String, payload: Any, expected: Int64? = nil, mutation: String, at: Int64) throws -> Int64 {
        let current = try store.record(collection: collection, id: id), revision = current?.rev ?? 0
        if let expected, expected != revision { throw ReaderNativeDataStore.StoreError.revisionConflict(collection: collection, id: id, actual: revision) }
        guard revision >= 0 && revision < 9_007_199_254_740_991 else { throw MutationError.invalid("修订号溢出") }
        let record: [String: Any] = ["schema": 1, "collection": collection, "id": id, "rev": revision+1,
            "updatedAt": at, "updatedBy": deviceID, "deleted": false,
            "value": ["id": id, "documentId": bookID, "payload": payload, "updatedAt": at]]
        // Journal mutation IDs are database-wide, whereas callers reuse IDs
        // within a book. Scope the key to book and record so another book or a
        // delimiter inside an item ID cannot alias this committed write.
        let mutationKey = try Self.bytes([bookID, collection, id, mutation])
        let mutationID = "native-" + SHA256.hash(data: mutationKey).map { String(format: "%02x", $0) }.joined()
        let json = try Self.string(record)
        let change: [String: Any] = ["mutationId": mutationID, "operation": "put", "collection": collection, "record": record]
        _ = try Self.bytes(change)
        try store.commitWithinTransaction(record: .init(collection: collection, id: id, rev: revision+1,
            updatedAt: at, deleted: false, json: json), mutationId: mutationID,
            journalJSON: { cursor in
                var envelope = change; envelope["cursor"] = cursor
                return try! Self.string(envelope) // All constituent values were validated above.
            }, expectedRev: revision, now: at)
        return revision+1
    }

    private func writeHighlights(_ kind: String, items: [[String: Any]], expected: Int64?, mutation: String, at: Int64) throws -> Int64 {
        let current = try projection.highlights(kind, bookID: bookID)
        let meta = try projection.state(kind + "-split-meta", bookID: bookID)
        if let expected, current.revision != expected {
            throw ReaderNativeDataStore.StoreError.revisionConflict(collection: "native-" + kind, id: bookID, actual: current.revision)
        }
        // The existing resumable importer owns conversion of non-empty legacy
        // arrays; a normal write may not reset their collection revision.
        guard meta.payload != nil || current.items.isEmpty else { throw MutationError.unavailable }
        var prior: [String: [String: Any]] = [:], seen = Set<String>(), order: [String] = []
        for item in current.items {
            guard let id = item["id"] as? String, !id.isEmpty, prior[id] == nil else { throw MutationError.invalid("原划线编号") }
            prior[id] = item
        }
        func save(_ item: [String: Any], id: String) throws {
            try write(collection: "native-" + kind + "-items", id: "native-\(kind)-item-v1:\(bookID.utf16.count):\(bookID):\(id)",
                      payload: item, mutation: mutation + ":" + id, at: at)
        }
        for item in items {
            guard let id = item["id"] as? String, !id.isEmpty, id.utf16.count <= 200,
                  seen.insert(id).inserted, item["deleted"] as? Bool != true else { throw MutationError.invalid("划线编号重复或无效") }
            order.append(id)
            if let old = prior[id], try Self.bytes(old) == Self.bytes(item) { continue }
            try save(item, id: id)
        }
        for id in prior.keys.sorted() where !seen.contains(id) { try save(["id": id, "deleted": true, "time": at / 1000], id: id) }
        return try writeState(kind + "-split-meta", value: ["order": order], expected: meta.revision, mutation: mutation + ":meta", at: at)
    }

    static func placements(_ notes: [[String: Any]]) -> [[String: Any]] {
        notes.compactMap { note in
            guard let id = note["id"] as? String, !id.isEmpty,
                  note["card"] is [String: Any] || note["html"] is [String: Any] || note["video"] is [String: Any] else { return nil }
            let kind = note["card"] is [String: Any] ? "card" : (note["html"] is [String: Any] ? "html" : "video")
            var entityIDs: [String] = []
            for (field, keys) in [("card", ["gid", "cid", "id"]), ("html", ["cid", "id"]), ("video", ["id"])] {
                for key in keys {
                    let value = ((note[field] as? [String: Any])?[key] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
                    if !value.isEmpty && value.utf16.count <= 240 && !entityIDs.contains(value) { entityIDs.append(value) }
                }
            }
            func dimension(_ key: String) -> Any {
                guard let n = note[key] as? NSNumber, n.doubleValue.isFinite else { return NSNull() }; return n
            }
            return ["placementId": id, "noteId": id, "kind": kind, "anchor": note["anchor"] ?? NSNull(),
                    "w": dimension("w"), "h": dimension("h"), "collapsed": note["collapsed"] as? Bool ?? false, "entityIds": entityIDs]
        }
    }
    static func references(_ placements: [[String: Any]]) -> [[String: Any]] {
        var grouped: [String: [String: Any]] = [:]
        for placement in placements {
            for id in placement["entityIds"] as? [String] ?? [] {
                var value = grouped[id] ?? ["entityId": id, "kind": placement["kind"] ?? "", "placementIds": [String]()]
                var ids = value["placementIds"] as? [String] ?? []
                if let placementID = placement["placementId"] as? String, !ids.contains(placementID) { ids.append(placementID) }
                value["placementIds"] = ids.sorted(); grouped[id] = value
            }
        }
        return grouped.keys.sorted().compactMap { grouped[$0] }
    }
    static func wordBindings(_ notes: [[String: Any]]) -> [[String: Any]] {
        notes.enumerated().compactMap { index, note in
            guard let html = note["html"] as? [String: Any], let binding = html["bind"] as? [String: Any],
                  binding["kind"] as? String == "page-chars", let text = binding["text"] as? String,
                  let cid = html["cid"] as? String, !cid.isEmpty else { return nil }
            let key = text.filter { !$0.isWhitespace }.lowercased()
            guard !key.isEmpty, key.utf16.count <= 64 else { return nil }
            return ["cid": cid, "noteId": note["id"] ?? "", "key": key, "text": String(text.prefix(64)),
                    "page": (binding["page"] as? NSNumber)?.intValue ?? 0, "label": String((html["label"] as? String ?? "").prefix(120)),
                    "at": note["created"] as? NSNumber ?? 0, "order": index]
        }
    }
    private static func bytes(_ value: Any) throws -> Data {
        try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .fragmentsAllowed, .withoutEscapingSlashes])
    }
    private static func string(_ value: Any) throws -> String { String(decoding: try bytes(value), as: UTF8.self) }
}
