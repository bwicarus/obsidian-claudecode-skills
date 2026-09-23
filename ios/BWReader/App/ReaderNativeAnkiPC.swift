import Foundation
import CoreFoundation

/// Durable Reader -> desktop Anki projection. A timeout is an unknown result,
/// not permission to submit another note. Reader identity and AID stay stable.
struct ReaderNativeAnkiPC {
    let repository: ReaderNativeCardRepository
    var now: () -> Int64 = { Int64(Date().timeIntervalSince1970 * 1000) }
    struct Failure: LocalizedError {
        let code: String
        let message: String
        var errorDescription: String? { message }
    }
    struct Attempt {
        let gid: String, index: Int, aid: String
        let request: Data
        let record: [String: Any]
    }
    private static func fail(_ message: String) -> Failure { .init(code: "BW_READER_LOCAL_ANKI_SCHEMA", message: message) }
    static func receipt(_ record: [String: Any], index: Int) -> [String: Any] {
        let state = (record["states"] as? [String: Any])?[String(index)] as? [String: Any] ?? [:]
        return ((state["projections"] as? [String: Any])?["anki"] as? [String: Any])?["readerpc"] as? [String: Any] ?? [:]
    }
    func prepare(gid: String, index: Int, render: (String) throws -> String) throws -> Attempt {
        guard let record = try repository.load(gid), record["deleted"] as? Bool != true,
              let cards = record["cards"] as? [[String: Any]], (1...20).contains(cards.count), cards.indices.contains(index),
              let state = (record["states"] as? [String: Any])?[String(index)] as? [String: Any],
              state["phase"] as? String == "confirmed", state["removed"] as? Bool != true else { throw Self.fail("请先确认保存这张卡") }
        let old = Self.receipt(record, index: index)
        guard !["pending", "unknown", "succeeded"].contains(old["status"] as? String ?? "") else {
            throw Failure(code: "BW_NATIVE_ANKI_ALREADY_SUBMITTED", message: "这张卡已提交，不能重复发送")
        }
        let previousAid = old["mutationId"] as? String ?? ""
        let aid = previousAid.range(of: "^fc_[a-f0-9]{32}$", options: .regularExpression) != nil ? previousAid : "fc_" + UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        let source = record["source"] as? [String: Any] ?? [:]
        let rawTrack = (source["kjTrack"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines).lowercased().replacingOccurrences(of: "_", with: "-")
        let track = ["jp-word", "jp-grammar", "en-word", "en-grammar"].contains(rawTrack) ? rawTrack : ""
        let ownNodes = cards[index]["nodeIds"] as? [String] ?? []
        let nodes = track.isEmpty ? (ownNodes.isEmpty ? (source["kjNodes"] as? String ?? "").split(separator: ",").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty } : ownNodes) : []
        guard !track.isEmpty || ((1...8).contains(nodes.count) && Set(nodes).count == nodes.count && nodes.allSatisfy({ $0.range(of: "^kj:[0-9A-HJKMNP-TV-Z]{10}$", options: .regularExpression) != nil })) else {
            throw Failure(code: "BW_READER_ANKI_NODE_REQUIRED", message: "这张卡需要知识节点或语言学习轨道，尚未发送")
        }
        func canonical(_ card: [String: Any]) throws -> [String: Any] {
            let keys = card["type"] as? String == "basic" ? ["front", "back"] : ["cloze"]
            guard ["basic", "cloze"].contains(card["type"] as? String ?? "") else { throw Self.fail("卡片类型无效") }
            var result: [String: Any] = ["type": card["type"]!]
            for key in keys {
                guard let value = card[key] as? String, value.utf16.count <= 64_000, !value.contains("\0"), key == "back" || !value.isEmpty else { throw Self.fail("卡片内容无效或过长") }
                result[key] = value
            }
            return result
        }
        let batch = try cards.map(canonical), card = batch[index]
        var projection = card
        for key in ["front", "back", "cloze"] { if let text = card[key] as? String { projection[key] = try render(text) } }
        var request: [String: Any] = ["entityId": gid, "cards": batch, "cardIndex": index, "aid": aid,
            "card": card, "projection": projection, "nodeIds": nodes, "track": track]
        let at = source["location"] as? [String: Any] ?? source["anchor"] as? [String: Any] ?? [:]
        var target: [String: Any]?
        if let page = at["page"] as? Int, page > 0 { target = ["kind": "pdf", "page": page] }
        else if let section = at["section"] as? Int, section >= 0 { target = ["kind": "epub", "section": section] }
        if let target, let file = source["documentId"] as? String, !file.isEmpty,
           let quote = source["quote"] as? String, !quote.isEmpty {
            guard file.utf16.count <= 4096, quote.utf16.count <= 8000 else { throw Self.fail("卡片引用来源过长") }
            request["file"] = file; request["target"] = target; request["sourceText"] = quote
        }
        let bytes = try JSONSerialization.data(withJSONObject: request, options: [.sortedKeys, .withoutEscapingSlashes])
        guard bytes.count <= 192 * 1024 else { throw Self.fail("Anki 导出请求超过 192KiB") }
        let pending = try write(gid: gid, index: index, record: record, receipt: ["status": "pending", "mutationId": aid, "updatedAt": now(), "detail": ["owner": "native-pc"]])
        return Attempt(gid: gid, index: index, aid: aid, request: bytes, record: pending)
    }
    private func write(gid: String, index: Int, record: [String: Any], receipt: [String: Any]) throws -> [String: Any] {
        let result = try repository.perform(["operation": "recordAnkiReceipt", "arguments": [gid, index, "readerpc", receipt,
            ["ifStateRev": record["stateRev"]!, "ifEntityRev": record["entityRev"]!]], "mutationId": "native-pc-receipt:" + UUID().uuidString])
        return result["result"] as! [String: Any]
    }
    func settle(_ attempt: Attempt, result: Data?, errorCode: String = "", message: String = "", sent: Bool = true) throws -> [String: Any] {
        guard let record = try repository.load(attempt.gid) else { throw Self.fail("卡片已不存在") }
        let prior = Self.receipt(record, index: attempt.index)
        guard prior["mutationId"] as? String == attempt.aid, prior["status"] as? String == "pending" else { throw Self.fail("导出回执与当前操作不一致") }
        var value: [String: Any] = ["mutationId": attempt.aid, "updatedAt": now(), "detail": ["owner": "native-pc"]]
        if let result {
            let response = try Self.validated(result)
            value["status"] = "succeeded"; value["exportedAt"] = now()
            value["noteIds"] = response["note_ids"]!; value["cardIds"] = response["card_ids"]!
        } else {
            value["status"] = !sent || Self.knownFailure(errorCode) ? "failed" : "unknown"
            value["error"] = String((errorCode.isEmpty ? message : errorCode).prefix(1000))
        }
        return try write(gid: attempt.gid, index: attempt.index, record: record, receipt: value)
    }
    static func knownFailure(_ code: String) -> Bool {
        code.range(of: "^(?:BW_READER_LOCAL_ANKI_(?:SCHEMA|CHANNEL_UNAVAILABLE|CONTEXT_INVALID)|BW_READER_ANKI_(?:LOCAL_UNAVAILABLE|CONTEXT_ONLY_REQUIRED|REQUEST_INVALID|DRAFT_NOT_REGISTERED|DRAFT_SOURCE_MISMATCH|DRAFT_CARD_INDEX_INVALID|AID_REUSED|AID_AMBIGUOUS|CONNECT_UNREACHABLE|CONNECT_RESPONSE_INVALID|CONNECT_ERROR))$", options: .regularExpression) != nil
    }
    static func validated(_ data: Data) throws -> [String: Any] {
        func id(_ value: Any) -> Bool { guard let n = value as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID() else { return false }; let d = n.doubleValue; return d > 0 && d <= 9_007_199_254_740_991 && d.rounded(.towardZero) == d }
        guard let value = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              Set(value.keys).subtracting(["ok", "added", "note_ids", "card_ids", "card_ids_by_note", "dedup"]).isEmpty,
              value["ok"] as? Bool == true, let added = value["added"], id(added),
              let notes = value["note_ids"] as? [Any], notes.count == (added as! NSNumber).intValue, notes.allSatisfy(id),
              let cards = value["card_ids"] as? [Any], cards.allSatisfy(id),
              let mapping = value["card_ids_by_note"] as? [String: Any], mapping.allSatisfy({ key, value in
                  key.range(of: "^[0-9]+$", options: .regularExpression) != nil && (value as? [Any])?.allSatisfy(id) == true
              }), value["dedup"] == nil || value["dedup"] is Bool else {
            throw Failure(code: "BW_READER_LOCAL_ANKI_RESPONSE_INVALID", message: "Anki 回执格式无效，结果仍需核实")
        }
        return value
    }
    /// A process restart must never retry a possibly delivered mutation.
    func recoverInterrupted() throws -> [[String: Any]] {
        var output: [[String: Any]] = []
        for group in try repository.snapshot() where group["deleted"] as? Bool != true {
            guard let gid = group["gid"] as? String else { continue }
            for key in (group["states"] as? [String: Any] ?? [:]).keys {
                guard let index = Int(key), let fresh = try repository.load(gid) else { continue }
                var receipt = Self.receipt(fresh, index: index)
                guard receipt["status"] as? String == "pending", (receipt["detail"] as? [String: Any])?["owner"] as? String == "native-pc" else { continue }
                receipt["status"] = "unknown"; receipt["error"] = "App 重启，外部接收结果待核实"; receipt["updatedAt"] = now()
                output.append(try write(gid: gid, index: index, record: fresh, receipt: receipt))
            }
        }
        return output
    }
}
