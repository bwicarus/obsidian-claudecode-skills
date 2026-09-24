import Foundation

typealias F = ReaderNativeFavoritesService
@MainActor final class Server {
    var rows: [[String: Any]] = []
    var requests: [[String: Any]] = []
    var failRead = false
    var failWrite = false
    var loseWriteReply = false
    var held: CheckedContinuation<Void, Never>?
    var holdNextRead = false
    func fetch(_ path: String, _ method: String, _ bytes: Data?) async throws -> F.Response {
        let input = try bytes.map { try JSONSerialization.jsonObject(with: $0) as! [String: Any] } ?? [:]
        requests.append(["path": path, "method": method, "body": input])
        if method == "GET" {
            let snapshot = rows.filter { path.contains("trash=1") ? $0["deleted"] != nil : $0["deleted"] == nil }
            if holdNextRead { holdNextRead = false; await withCheckedContinuation { held = $0 } }
            if failRead { return .init(status: 503, data: Data(#"{"ok":false}"#.utf8)) }
            return .init(status: 200, data: try ReaderNativeCardRules.bytes(["ok": true, "cards": snapshot]))
        }
        if failWrite { return .init(status: 503, data: Data(#"{"ok":false}"#.utf8)) }
        let op = input["op"] as? String
        if op == "add" {
            let row = input["card"] as! [String: Any], id = row["id"] as! String
            rows.removeAll { $0["id"] as? String == id }; rows.append(row)
            if loseWriteReply { loseWriteReply = false; throw URLError(.networkConnectionLost) }
            return .init(status: 200, data: try ReaderNativeCardRules.bytes(["ok": true, "id": id]))
        }
        if op == "del" {
            let ids = input["ids"] as! [String]
            rows = rows.map { row in var next = row; if ids.contains(row["id"] as! String) { next["deleted"] = 1 }; return next }
        }
        if op == "restore" { rows = rows.map { row in var next = row; if row["id"] as? String == input["id"] as? String { next.removeValue(forKey: "deleted") }; return next } }
        return .init(status: 200, data: Data(#"{"ok":true}"#.utf8))
    }
}

@main struct Tests {
    @MainActor static func main() async throws {
        let original: [String: Any] = ["kind": "cards", "gid": "group_A", "cid": "group_A", "revision": 6,
            "payload": ["version": 1, "kind": "cards", "cards": [["front": "表", "back": "裏", "nodeIds": ["node-1"],
                "batchIndex": 3, "source": ["file": "book", "page": 9], "anki": ["noteId": 1234], "state": ["revealed": true]]]]]
        let prepared = try F.prepare(original, current: [["id": "group_A", "revision": 10]], now: 100)
        precondition(prepared["id"] as? String == "group_A" && prepared["cid"] as? String == "group_A")
        precondition(F.revision(prepared) == 11 && ReaderNativeCardRules.same(prepared["payload"]!, original["payload"]!))
        var oversized = original; oversized["payload"] = ["version": 1, "kind": "cards", "cards": [["front": String(repeating: "字", count: 100_000)]]]
        do { _ = try F.prepare(oversized, current: []); preconditionFailure("oversized payload accepted") } catch {}
        for id in ["../x", "x/y", String(repeating: "x", count: 81), ""] {
            do { _ = try F.identifier(id); preconditionFailure("invalid identity accepted") } catch {}
        }

        let server = Server()
        var projections: [[[String: Any]]] = []
        let owner = F(fetch: { try await server.fetch($0, $1, $2) }, changed: { projections.append($0) })
        _ = try await owner.perform("save", ["card": original])
        precondition(server.requests.count == 2 && owner.records.count == 1)
        precondition(owner.records[0]["pendingConfirmation"] == nil)
        _ = try await owner.perform("read")
        precondition(server.requests.count == 2, "cached read made an extra request")

        // Failed reads/deletes preserve the known row; a server rejection is
        // not a successful empty collection or successful deletion.
        server.failRead = true
        do { _ = try await owner.perform("read", ["refresh": true]); preconditionFailure("read succeeded") } catch {}
        precondition(owner.records.count == 1)
        server.failRead = false; server.failWrite = true
        do { _ = try await owner.perform("delete", ["ids": ["group_A"]]); preconditionFailure("delete succeeded") } catch {}
        precondition(owner.records.count == 1)
        server.failWrite = false
        _ = try await owner.perform("delete", ["ids": ["group_A"]])
        precondition(owner.records.isEmpty)
        _ = try await owner.perform("trash")
        precondition(owner.trashRecords.count == 1)
        _ = try await owner.perform("restore", ["id": "group_A"])
        _ = try await owner.perform("read")
        precondition(owner.records.count == 1 && owner.trashRecords.isEmpty)

        // A lost POST reply keeps the full card and forbids blind re-send.
        server.loseWriteReply = true
        do { _ = try await owner.perform("save", ["card": original]); preconditionFailure("unknown result marked successful") } catch {}
        precondition(owner.records[0]["pendingConfirmation"] as? Bool == true)
        let calls = server.requests.count
        do { _ = try await owner.perform("save", ["card": original]); preconditionFailure("uncertain write repeated") } catch {}
        precondition(server.requests.count == calls)
        _ = try await owner.perform("read", ["refresh": true])
        precondition(owner.records[0]["pendingConfirmation"] == nil)

        // Two native/compatibility callers serialize revision allocation.
        async let a = owner.perform("save", ["card": original])
        async let b = owner.perform("save", ["card": original])
        _ = try await (a, b)
        let additions = server.requests.filter { ($0["body"] as? [String: Any])?["op"] as? String == "add" }
        let revisions = additions.map { F.revision(($0["body"] as! [String: Any])["card"] as! [String: Any]) }
        precondition(zip(revisions, revisions.dropFirst()).allSatisfy { $0 < $1 })

        server.holdNextRead = true
        let pending = Task { try await owner.perform("read", ["refresh": true]) }
        while server.held == nil { await Task.yield() }
        let projectionCount = projections.count
        owner.invalidate(); server.held?.resume(); server.held = nil
        do { _ = try await pending.value; preconditionFailure("retired context published") } catch {}
        precondition(projections.count == projectionCount)
        print("Native favorites: content identity, serialized revisions, failure retention, read reconciliation, trash and stale-context checks passed")
    }
}
