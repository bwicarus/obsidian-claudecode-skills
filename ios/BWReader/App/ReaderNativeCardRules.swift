import Foundation
import CoreFoundation

/// Card entity/state rules shared by the native repository and native views.
/// IDs, stable batch indexes and external Anki receipts keep their existing
/// contracts; neither a view nor an export creates a second card identity.
enum ReaderNativeCardRules {
    struct Failure: LocalizedError {
        let code: String
        let detail: String
        var errorDescription: String? { code + ": " + detail }
    }
    static func fail(_ suffix: String = "INPUT", _ message: String) -> Failure {
        .init(code: "BW_CARD_REPOSITORY_" + suffix, detail: message)
    }
    static func has(_ value: Any?) -> Bool { value != nil && !(value is NSNull) }
    static func object(_ value: Any?, _ label: String, code: String = "INPUT") throws -> [String: Any] {
        guard let result = value as? [String: Any] else { throw fail(code, label + " 必须是对象") }
        return result
    }
    static func fields(_ value: [String: Any], _ names: [String], _ label: String) throws {
        if let unknown = value.keys.sorted().first(where: { !names.contains($0) }) {
            throw fail("INPUT", label + " 含未声明字段：" + unknown)
        }
    }
    static func string(_ value: Any?) -> String {
        guard has(value) else { return "" }
        if let result = value as? String { return result }
        if let number = value as? NSNumber {
            if CFGetTypeID(number) == CFBooleanGetTypeID() { return number.boolValue ? "true" : "false" }
            return number.stringValue
        }
        return "[object Object]"
    }
    static func text(_ value: Any?, _ label: String, _ limit: Int, required: Bool = false) throws -> String {
        let output = string(value).replacingOccurrences(of: "\r\n", with: "\n")
            .replacingOccurrences(of: "\r", with: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
        guard !output.contains("\0"), output.utf8.count <= limit, !required || !output.isEmpty else {
            throw fail("INPUT", label + " 为空、含 NUL 或超出长度上限")
        }
        return output
    }
    static func number(_ value: Any?, _ label: String) throws -> Double {
        let output: Double?
        if value is NSNull { output = 0 }
        else if let number = value as? NSNumber { output = number.doubleValue }
        else if let text = value as? String {
            let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            output = trimmed.isEmpty ? 0 : Double(trimmed)
        } else { output = nil }
        guard let output, output.isFinite, output >= 0 else { throw fail("INPUT", label + " 无效") }
        return output
    }
    static func integer(_ value: Any?, _ label: String) throws -> Int64 {
        let output = try number(value, label)
        guard output.rounded() == output, output <= 9_007_199_254_740_991 else { throw fail("INPUT", label + " 无效") }
        return Int64(output)
    }
    static func bool(_ value: Any?, _ label: String) throws -> Bool {
        guard let number = value as? NSNumber, CFGetTypeID(number) == CFBooleanGetTypeID() else {
            throw fail("STATE", label + " 必须是布尔值")
        }
        return number.boolValue
    }
    static func bytes(_ value: Any) throws -> Data {
        try JSONSerialization.data(withJSONObject: value, options: [.fragmentsAllowed, .sortedKeys, .withoutEscapingSlashes])
    }
    static func same(_ left: Any, _ right: Any) -> Bool {
        guard let lhs = try? bytes(left), let rhs = try? bytes(right) else { return false }
        return lhs == rhs
    }
    static func bounded(_ value: Any, _ label: String, _ limit: Int) throws {
        func check(_ item: Any) throws {
            if let text = item as? String, text.contains("\0") { throw fail("INPUT", label + " 含 NUL") }
            if let dict = item as? [String: Any] {
                for (key, child) in dict { try check(key); try check(child) }
            } else if let array = item as? [Any] { for child in array { try check(child) } }
        }
        try check(value)
        guard try bytes(value).count <= limit else { throw fail("INPUT", label + " 超出大小上限") }
    }
    static func matches(_ value: String, _ pattern: String) -> Bool {
        value.range(of: pattern, options: .regularExpression) != nil
    }
    static func id(_ value: Any?) throws -> String {
        let output = string(value).trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard matches(output, "^card_[a-f0-9]{4,64}$"), !output.contains("\n") else {
            throw fail("ID", "卡组编号必须是稳定的 card_ 十六进制编号")
        }
        return output
    }
    static func identity(_ input: [String: Any], generate: Bool) throws -> String {
        let ids = try ["id", "cid", "gid", "cardId"].compactMap { key -> String? in
            guard has(input[key]), !string(input[key]).trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
            return try id(input[key])
        }
        guard let first = ids.first else {
            guard generate else { throw fail("ID", "缺少 card_* gid") }
            return "card_" + UUID().uuidString.lowercased().replacingOccurrences(of: "-", with: "")
        }
        guard ids.allSatisfy({ $0 == first }) else { throw fail("ID", "id/cid/gid 必须指向同一个 Reader 卡组实体") }
        return first
    }
    static func card(_ value: Any?) throws -> [String: Any] {
        let input = try object(value, "card")
        try fields(input, ["type", "front", "back", "cloze", "text", "deck", "tags", "reason"], "card")
        let type = try text(input["type"], "card.type", 32, required: true).lowercased()
        var result: [String: Any] = ["type": type]
        if type == "basic" {
            guard !has(input["cloze"]), !has(input["text"]) else { throw fail("CARD_SHAPE", "basic 卡不得携带 cloze/text") }
            result["front"] = try text(input["front"], "card.front", 32768, required: true)
            result["back"] = try text(input["back"], "card.back", 65536, required: true)
        } else if type == "cloze" {
            guard !has(input["front"]), !has(input["back"]), !(has(input["cloze"]) && has(input["text"])) else {
                throw fail("CARD_SHAPE", "cloze 卡字段不兼容")
            }
            let content = try text(has(input["cloze"]) ? input["cloze"] : input["text"], "card.cloze", 98304, required: true)
            guard matches(content, "\\{\\{c[1-9][0-9]*::[\\s\\S]+?\\}\\}") else { throw fail("CARD_SHAPE", "cloze 卡至少需要一个 {{c1::…}} 挖空") }
            result["cloze"] = content
        } else { throw fail("CARD_SHAPE", "Reader 本地卡仓只接受 basic 或 cloze") }
        for (key, limit) in [("deck", 512), ("reason", 4096)] where has(input[key]) {
            result[key] = try text(input[key], "card." + key, limit)
        }
        if has(input["tags"]) {
            guard let tags = input["tags"] as? [Any], tags.count <= 32 else { throw fail("INPUT", "tags 必须是有限数组") }
            let normalized = try tags.map { value -> String in
                let tag = try text(value, "tags[]", 128, required: true)
                guard !matches(tag, "\\s") else { throw fail("INPUT", "tag 不得含空白") }
                return tag
            }
            result["tags"] = Set(normalized).sorted { $0.utf16.lexicographicallyPrecedes($1.utf16) }
        }
        return result
    }
    static func cards(_ value: Any?) throws -> [[String: Any]] {
        guard let items = value as? [Any], !items.isEmpty, items.count <= 256 else { throw fail("CARD_SHAPE", "cards 必须包含 1-256 张卡") }
        return try items.map(card)
    }
    static func cardsOf(_ input: [String: Any]) throws -> [[String: Any]] {
        if input["cards"] is [Any] { return try cards(input["cards"]) }
        if input["card"] is [String: Any] { return [try card(input["card"])] }
        throw fail("CARD_SHAPE", "缺少 cards/card")
    }
    static func source(_ value: Any?) throws -> [String: Any] {
        let input = try object(value, "source", code: "SOURCE")
        let limits = ["kind":80,"sourceId":4096,"documentId":4096,"bookId":4096,"url":8192,"title":1024,
            "quote":32768,"context":65536,"tool":160,"draftId":512,"sourceInstanceId":512,"requirement":32768,"kjNodes":256,"kjTrack":32]
        let nested = ["location", "anchor", "selection", "legacy"]
        try fields(input, Array(limits.keys) + nested, "source")
        var result: [String: Any] = [:]
        for (key, limit) in limits where has(input[key]) { result[key] = try text(input[key], "source." + key, limit, required: key == "kind") }
        guard !(result["kind"] as? String ?? "").isEmpty else { throw fail("SOURCE", "source.kind 不能为空") }
        for key in nested where has(input[key]) { result[key] = try object(input[key], "source." + key, code: "SOURCE") }
        guard ["sourceId", "documentId", "bookId", "url", "draftId", "sourceInstanceId"].contains(where: { !(result[$0] as? String ?? "").isEmpty }) else {
            throw fail("SOURCE", "source 至少需要一个稳定来源编号或文档地址")
        }
        try bounded(result, "source", 128 * 1024)
        return result
    }
    static func defaultReview(_ phase: String) -> [String: Any] {
        ["status": phase == "confirmed" ? "new" : "unavailable", "dueAt": NSNull(), "lastReviewedAt": NSNull(),
         "intervalDays": 0, "ease": 0, "reps": 0, "lapses": 0]
    }
    static func review(_ value: Any?, previous: [String: Any]? = nil) throws -> [String: Any] {
        var result = previous ?? defaultReview("draft")
        guard has(value) else { return result }
        let input = try object(value, "review", code: "STATE")
        try fields(input, ["status", "dueAt", "lastReviewedAt", "intervalDays", "ease", "reps", "lapses"], "review")
        if input["status"] != nil {
            let status = try text(input["status"], "review.status", 32, required: true).lowercased()
            guard ["unavailable", "new", "learning", "review", "relearning", "suspended", "buried"].contains(status) else { throw fail("STATE", "review.status 无效") }
            result["status"] = status
        }
        for key in ["dueAt", "lastReviewedAt"] where input[key] != nil {
            result[key] = has(input[key]) ? try integer(input[key], "review." + key) as Any : NSNull()
        }
        for key in ["reps", "lapses"] where input[key] != nil { result[key] = try integer(input[key], "review." + key) }
        for key in ["intervalDays", "ease"] where input[key] != nil { result[key] = try number(input[key], "review." + key) }
        return result
    }
    static func flags(_ value: Any?, previous: [String: Any]? = nil) throws -> [String: Any] {
        var result: [String: Any] = ["favorite": false, "archived": false]
        previous?.forEach { result[$0.key] = $0.value }
        guard has(value) else { return result }
        let input = try object(value, "flags", code: "STATE")
        try fields(input, ["favorite", "archived"], "flags")
        for key in ["favorite", "archived"] where input[key] != nil { result[key] = try bool(input[key], "flags." + key) }
        return result
    }
    static func receipt(_ value: Any?, previous: [String: Any]? = nil) throws -> [String: Any] {
        let input = try object(value, "Anki receipt", code: "RECEIPT")
        try fields(input, ["status", "mutationId", "noteIds", "cardIds", "exportedAt", "updatedAt", "error", "detail"], "ankiReceipt")
        var result: [String: Any] = ["status": "pending", "mutationId": "", "noteIds": [], "cardIds": [], "exportedAt": NSNull(), "updatedAt": NSNull(), "error": ""]
        previous?.forEach { result[$0.key] = $0.value }
        if input["status"] != nil {
            let status = try text(input["status"], "ankiReceipt.status", 32, required: true).lowercased()
            guard ["pending", "succeeded", "failed", "unknown"].contains(status) else { throw fail("RECEIPT", "ankiReceipt.status 无效") }
            result["status"] = status
        }
        for (key, limit) in [("mutationId", 512), ("error", 4096)] where input[key] != nil { result[key] = try text(input[key], "ankiReceipt." + key, limit) }
        for key in ["noteIds", "cardIds"] where input[key] != nil {
            guard let ids = input[key] as? [Any], ids.count <= 128 else { throw fail("RECEIPT", "ankiReceipt." + key + " 无效") }
            result[key] = try ids.map { value -> Any in
                if value is String { return try text(value, "ankiReceipt." + key, 256, required: true) }
                guard let number = value as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID() else { throw fail("RECEIPT", "Anki 外部编号无效") }
                return try integer(number, "ankiReceipt." + key)
            }
        }
        for key in ["exportedAt", "updatedAt"] where input[key] != nil { result[key] = has(input[key]) ? try integer(input[key], "ankiReceipt." + key) as Any : NSNull() }
        if let detail = input["detail"] { try bounded(detail, "ankiReceipt.detail", 16384); result["detail"] = detail }
        return result
    }
    static func target(_ value: String) throws -> String {
        guard matches(value, "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"), !value.contains("\n") else { throw fail("RECEIPT", "Anki target 无效") }
        return value
    }
    static func projections(_ value: Any?, previous: [String: Any]? = nil) throws -> [String: Any] {
        var result = previous ?? ["anki": [:]]
        var anki = result["anki"] as? [String: Any] ?? [:]
        if has(value) {
            let input = try object(value, "projections", code: "STATE")
            try fields(input, ["anki"], "projections")
            if has(input["anki"]) {
                for (key, raw) in try object(input["anki"], "projections.anki", code: "STATE") {
                    _ = try target(key)
                    anki[key] = try receipt(raw, previous: anki[key] as? [String: Any])
                }
            }
        }
        result["anki"] = anki
        return result
    }
    static func exactState(_ value: Any?) throws -> [String: Any] {
        guard has(value) else { return [:] }
        let result = try object(value, "exactState", code: "STATE")
        try bounded(result, "exactState", 128 * 1024)
        return result
    }
    static func state(_ input: [String: Any]) throws -> [String: Any] {
        guard let phase = input["phase"] as? String, ["draft", "confirmed"].contains(phase) else { throw fail("STATE", "phase 无效") }
        let removed = has(input["removed"]) ? try bool(input["removed"], "state.removed") : false
        return ["phase": phase, "removed": removed,
            "confirmedAt": phase == "confirmed" ? try integer(input["confirmedAt"], "state.confirmedAt") as Any : NSNull(),
            "review": try review(input["review"], previous: defaultReview(phase)), "flags": try flags(input["flags"]),
            "projections": try projections(input["projections"]), "exactState": try exactState(input["exactState"])]
    }
    static func states(_ value: Any?, count: Int) throws -> [String: Any] {
        let input = try object(value, "states", code: "STATE")
        guard input.count == count else { throw fail("CORRUPT", "states 数量与 cards 不一致") }
        var result: [String: Any] = [:]
        for index in 0..<count { result[String(index)] = try state(object(input[String(index)], "states.\(index)", code: "CORRUPT")) }
        return result
    }
    static func freshStates(_ count: Int) throws -> [String: Any] {
        var result: [String: Any] = [:]
        for index in 0..<count { result[String(index)] = try state(["phase": "draft"]) }
        return result
    }
    static func entity(id: String, cards: Any, source: Any, created: Any, updated: Any) throws -> [String: Any] {
        let result: [String: Any] = ["contract": "card-entity/1", "schema": 1, "id": id, "cid": id, "gid": id,
            "cards": try self.cards(cards), "source": try self.source(source),
            "createdAt": try integer(created, "entity.createdAt"), "contentUpdatedAt": try integer(updated, "entity.contentUpdatedAt")]
        try bounded(result, "card entity", 2 * 1024 * 1024)
        return result
    }
    static func stateValue(id: String, states: Any, count: Int) throws -> [String: Any] {
        ["contract": "card-state/1", "schema": 1, "id": id, "cid": id, "gid": id, "states": try self.states(states, count: count)]
    }
    static func legacy(_ value: Any) throws -> [String: Any] {
        let input = try object(value, "legacy record", code: "LEGACY"), id = try identity(input, generate: false)
        if has(input["kind"]), string(input["kind"]) != "cards" { throw fail("LEGACY", "legacy record.kind 不是 cards") }
        let batch = input["batch"] as? [String: Any] ?? [:]
        let shapes = [input["cards"], input["data"], input["batch"], batch["cards"], batch["data"]].compactMap { $0 as? [Any] }
        guard let first = shapes.first else { throw fail("LEGACY", "legacy record 缺少 cards/data/batch") }
        let normalized = try cards(first)
        for shape in shapes.dropFirst() where try !same(normalized, cards(shape)) { throw fail("LEGACY", "legacy record 的 cards/data/batch 内容分叉") }
        if has(input["states"]), has(batch["states"]), !same(input["states"]!, batch["states"]!) { throw fail("LEGACY", "legacy record 的 states/batch.states 内容分叉") }
        let rawStates = try object(has(input["states"]) ? input["states"] : (batch["states"] ?? [:]), "legacy states", code: "LEGACY")
        var stamp: Int64 = 0
        if has(input["ts"]) {
            let number = try number(input["ts"], "legacy ts")
            stamp = try integer(number < 100_000_000_000 ? (number * 1000).rounded(.toNearestOrAwayFromZero) : number, "legacy ts")
        }
        var states = try freshStates(normalized.count)
        for (key, value) in rawStates {
            guard matches(key, "^(0|[1-9][0-9]*)$"), !key.contains("\n"), let index = Int(key), index < normalized.count else { throw fail("LEGACY", "legacy state index 无效或超出 cards") }
            let exact = try exactState(value), status = string(exact["_st"])
            let phase = status.isEmpty || status == "draft" ? "draft" : "confirmed"
            var review = defaultReview(phase)
            if phase == "confirmed" { review["status"] = status == "done" ? "review" : "learning" }
            var legacyReceipt: [String: Any] = ["status": "succeeded", "noteIds": [], "cardIds": [],
                "exportedAt": stamp > 0 ? stamp as Any : NSNull(), "updatedAt": stamp > 0 ? stamp as Any : NSNull()]
            if has(exact["_nid"]) { legacyReceipt["noteIds"] = [exact["_nid"]!] }
            if has(exact["card_id"]) || has(exact["id"]) { legacyReceipt["cardIds"] = [has(exact["card_id"]) ? exact["card_id"]! : exact["id"]!] }
            let hasIDs = !(legacyReceipt["noteIds"] as! [Any]).isEmpty || !(legacyReceipt["cardIds"] as! [Any]).isEmpty
            states[key] = try state(["phase": phase, "confirmedAt": max(1, stamp), "review": review,
                "projections": hasIDs ? ["anki": ["pi-legacy": try receipt(legacyReceipt)]] : ["anki": [:]], "exactState": exact])
        }
        let reference = try text(has(input["source_ref"]) ? input["source_ref"] : input["src"], "legacy.source_ref", 8192)
        let excluded = ["id", "cid", "gid", "kind", "cards", "data", "batch", "states", "source_ref", "src", "req"]
        var metadata = input.filter { !excluded.contains($0.key) }
        if !reference.isEmpty { metadata["source_ref"] = reference }
        var source: [String: Any] = ["kind": "pi-legacy-card-registry", "sourceId": reference.isEmpty ? "pi-card-registry:" + id : reference]
        if has(input["req"]) {
            metadata["req"] = input["req"]
            source["requirement"] = input["req"] is String ? input["req"] : String(decoding: try bytes(input["req"]!), as: UTF8.self)
        }
        if !metadata.isEmpty { source["legacy"] = metadata }
        return ["id": id, "cards": normalized, "source": try self.source(source), "states": states, "timestamp": stamp]
    }
}
