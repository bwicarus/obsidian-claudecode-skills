import Foundation

/// Native owner of the existing AnkiMobile receipt contract. Launching an URL
/// never counts as delivery; only the nonce-bound callback can confirm it.
struct ReaderNativeAnkiMobile {
    static let target = "ankimobile-ipad"
    static let lifetime: Int64 = 600_000
    let repository: ReaderNativeCardRepository
    var now: () -> Int64 = { Int64(Date().timeIntervalSince1970 * 1000) }

    struct Pending {
        let gid: String
        let index: Int
        let nonce: String
        let attempt: String
        let expiresAt: Int64
    }
    struct Prepared {
        let pending: Pending
        let url: String
        let record: [String: Any]
        var request: [String: Any] { ["action": "open", "gid": pending.gid, "index": pending.index,
            "nonce": pending.nonce, "expiresAt": pending.expiresAt, "url": url] }
    }
    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }
    private static func fail(_ message: String) -> Failure { Failure(message: message) }
    private func state(_ record: [String: Any], index: Int) throws -> [String: Any] {
        guard record["deleted"] as? Bool != true, (0...255).contains(index),
              let state = (record["states"] as? [String: Any])?[String(index)] as? [String: Any] else {
            throw Self.fail("本地卡库找不到这张卡")
        }
        return state
    }
    private func receipt(_ record: [String: Any], index: Int) throws -> [String: Any] {
        let state = try state(record, index: index)
        return ((state["projections"] as? [String: Any])?["anki"] as? [String: Any])?[Self.target] as? [String: Any] ?? [:]
    }
    static func pending(_ record: [String: Any], index: Int) -> Pending? {
        guard record["deleted"] as? Bool != true, let gid = record["gid"] as? String,
              (0...255).contains(index), let state = (record["states"] as? [String: Any])?[String(index)] as? [String: Any],
              let receipt = ((state["projections"] as? [String: Any])?["anki"] as? [String: Any])?[target] as? [String: Any],
              receipt["status"] as? String == "pending", let attempt = receipt["mutationId"] as? String,
              let at = (receipt["updatedAt"] as? NSNumber)?.int64Value, at >= 0, at <= Int64.max - lifetime,
              let detail = receipt["detail"] as? [String: Any], detail["channel"] as? String == "x-callback-url",
              detail["callbackExpected"] as? Bool == true, detail["callbackReceived"] as? Bool == false,
              let nonce = detail["callbackNonce"] as? String, nonce.range(of: "^[a-f0-9]{32}$", options: .regularExpression) != nil,
              let expiry = (detail["callbackExpiresAt"] as? NSNumber)?.int64Value, expiry == at + lifetime,
              attempt.hasPrefix("ankimobile:\(gid):\(index):\(at):") else { return nil }
        return Pending(gid: gid, index: index, nonce: nonce, attempt: attempt, expiresAt: expiry)
    }
    func pending() throws -> [Pending] {
        try repository.snapshot().flatMap { record in
            ((record["states"] as? [String: Any]) ?? [:]).keys.compactMap { Int($0).flatMap { Self.pending(record, index: $0) } }
        }
    }
    func prepare(gid: String, index: Int, nonce: String = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()) throws -> Prepared {
        guard gid.range(of: "^card_[a-f0-9]{4,64}$", options: .regularExpression) != nil,
              nonce.range(of: "^[a-f0-9]{32}$", options: .regularExpression) != nil,
              let record = try repository.load(gid), let cards = record["cards"] as? [[String: Any]], cards.indices.contains(index) else {
            throw Self.fail("AnkiMobile 卡片身份无效")
        }
        let saved = try state(record, index: index)
        guard saved["phase"] as? String == "confirmed", saved["removed"] as? Bool != true else { throw Self.fail("请先确认保存这张卡") }
        let prior = try receipt(record, index: index)
        if let status = prior["status"] as? String, ["pending", "unknown", "succeeded"].contains(status) {
            throw Self.fail(status == "succeeded" ? "这张卡已发送到 AnkiMobile，无需重复发送" : "这张卡的外部接收结果尚未确定，已阻止重复发送")
        }
        let url = try Self.addURL(gid: gid, index: index, card: cards[index], nonce: nonce)
        let at = now()
        let entry = Pending(gid: gid, index: index, nonce: nonce,
            attempt: "ankimobile:\(gid):\(index):\(at):\(nonce)", expiresAt: at + Self.lifetime)
        let value: [String: Any] = ["status": "pending", "mutationId": entry.attempt, "updatedAt": at, "error": "",
            "detail": ["channel": "x-callback-url", "callbackExpected": true, "callbackReceived": false,
                       "urlBytes": url.utf8.count, "callbackNonce": nonce, "callbackExpiresAt": entry.expiresAt]]
        let result = try write(entry, value: value, record: record, mutation: "pending")
        return Prepared(pending: entry, url: url, record: result)
    }
    private func write(_ entry: Pending, value: [String: Any], record: [String: Any], mutation: String) throws -> [String: Any] {
        let options: [String: Any] = ["ifEntityRev": record["entityRev"]!, "ifStateRev": record["stateRev"]!]
        let reply = try repository.perform(["operation": "recordAnkiReceipt", "arguments": [entry.gid, entry.index, Self.target, value, options],
            "mutationId": "native-anki-mobile:\(entry.nonce):\(mutation)"])
        return reply["result"] as! [String: Any]
    }
    /// Failure here means UIKit explicitly did not open the external app.
    /// An interrupted request remains pending/unknown, never safely retryable.
    func didNotOpen(_ entry: Pending, message: String) throws -> [String: Any] {
        guard let record = try repository.load(entry.gid), let current = Self.pending(record, index: entry.index),
              current.nonce == entry.nonce else { throw Self.fail("AnkiMobile 导出状态已经改变") }
        return try write(entry, value: ["status": "failed", "mutationId": entry.attempt, "updatedAt": now(),
            "error": String(message.prefix(4000)), "detail": ["channel": "x-callback-url", "callbackExpected": true, "callbackReceived": false]],
            record: record, mutation: "failed")
    }
    func confirm(gid: String, index: Int, nonce: String) throws -> [String: Any] {
        guard let record = try repository.load(gid) else { throw Self.fail("AnkiMobile 回调没有对应卡片") }
        let old = try receipt(record, index: index), detail = old["detail"] as? [String: Any] ?? [:]
        if old["status"] as? String == "succeeded", detail["callbackNonce"] as? String == nonce,
           detail["channel"] as? String == "x-callback-url", detail["callbackReceived"] as? Bool == true { return record }
        guard let entry = Self.pending(record, index: index), entry.nonce == nonce, entry.expiresAt > now() else {
            throw Self.fail("AnkiMobile 回调已过期或不属于这次导出")
        }
        let at = now()
        return try write(entry, value: ["status": "succeeded", "mutationId": entry.attempt, "updatedAt": at, "exportedAt": at, "error": "",
            "detail": ["channel": "x-callback-url", "callbackExpected": true, "callbackReceived": true,
                       "callbackNonce": nonce, "callbackExpiresAt": entry.expiresAt]], record: record, mutation: "succeeded")
    }
    func expire(_ entry: Pending) throws -> [String: Any]? {
        guard entry.expiresAt <= now(), let record = try repository.load(entry.gid),
              let current = Self.pending(record, index: entry.index), current.nonce == entry.nonce else { return nil }
        return try write(entry, value: ["status": "unknown", "mutationId": entry.attempt, "updatedAt": now(), "error": "",
            "detail": ["channel": "x-callback-url", "callbackExpected": true, "callbackReceived": false, "reason": "callback-expired"]],
            record: record, mutation: "unknown")
    }
    static func addURL(gid: String, index: Int, card: [String: Any], nonce: String) throws -> String {
        func field(_ key: String) throws -> String {
            let text = (card[key] as? String ?? "").replacingOccurrences(of: "\r\n", with: "\n").replacingOccurrences(of: "\r", with: "\n")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            guard !text.isEmpty, !text.contains("\0") else { throw fail("AnkiMobile 卡片内容为空或含无效字符") }
            return text.replacingOccurrences(of: "\n", with: "<br>")
        }
        func query(_ values: [(String, String)]) -> String {
            let allowed = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
            return values.map { $0.0.addingPercentEncoding(withAllowedCharacters: allowed)! + "=" + $0.1.addingPercentEncoding(withAllowedCharacters: allowed)! }.joined(separator: "&")
        }
        var fields: [(String, String)]
        if card["type"] as? String == "basic" { fields = [("type", "Basic"), ("deck", "BW Reader"), ("fldFront", try field("front")), ("fldBack", try field("back"))] }
        else if card["type"] as? String == "cloze" {
            let text = try field("cloze")
            guard text.range(of: #"\{\{c[1-9]\d*::[\s\S]+?\}\}"#, options: .regularExpression) != nil else { throw fail("Cloze 卡片缺少有效挖空") }
            fields = [("type", "Cloze"), ("deck", "BW Reader"), ("fldText", text)]
        } else { throw fail("AnkiMobile 仅支持 Basic 与 Cloze") }
        let supplied = card["tags"] as? [String] ?? []
        guard supplied.count <= 32 else { throw fail("卡片标签过多") }
        var tags = ["bwreader", "bwgid_" + gid, "bwindex_" + String(index)]
        for value in supplied {
            let tag = value.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !tag.isEmpty, tag.utf8.count <= 128, !tag.contains("\0"), tag.rangeOfCharacter(from: .whitespacesAndNewlines) == nil else { throw fail("卡片标签无效") }
            if !tags.contains(tag) { tags.append(tag) }
        }
        fields += [("tags", tags.joined(separator: " ")), ("x-success", "bwreader://anki-export-success?" + query([("gid", gid), ("index", String(index)), ("nonce", nonce)]))]
        let url = "anki://x-callback-url/addnote?" + query(fields)
        guard url.utf8.count <= 32 * 1024 else { throw fail("AnkiMobile URL 超出 32KB，未打开外部 App") }
        return url
    }
}
