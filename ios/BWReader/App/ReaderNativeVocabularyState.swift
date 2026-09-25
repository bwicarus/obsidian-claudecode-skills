import Foundation
import CoreFoundation

/// Owns the existing vocabulary-state records and their sync journal. The
/// compatibility web projection only observes committed values.
struct ReaderNativeVocabularyState {
    typealias R = ReaderNativeCardRules
    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { "BW_VOCABULARY_STATE: " + message }
    }
    static let collection = "vocabulary-state"
    let store: ReaderNativeDataStore
    let deviceID: String
    var now: () -> Int64 = { Int64(Date().timeIntervalSince1970 * 1000) }

    /// Query the same semantic record that set() writes. A missing phrase
    /// record must not inherit the dictionary headword's mastery state.
    func enabled(_ input: [String: Any], property: String) throws -> Bool {
        let query = try Self.normalized(input, property: property)
        let id = query["id"] as! String
        guard let row = try store.record(collection: Self.collection, id: id), !row.deleted else { return false }
        let envelope = try R.object(JSONSerialization.jsonObject(with: Data(row.json.utf8)), "vocabulary envelope")
        let value = try Self.normalized(R.object(envelope["value"], "vocabulary value"))
        guard value["id"] as? String == id, envelope["id"] as? String == id,
              envelope["collection"] as? String == Self.collection,
              (envelope["rev"] as? NSNumber)?.int64Value == row.rev,
              envelope["deleted"] as? Bool == false else { throw Failure(message: "词汇记录损坏") }
        return value["enabled"] as? Bool == true
    }

    /// Favorites and mastered phrases both remain atomic during selection.
    /// Do not add mastered phrases to the favorites collection itself.
    func tokenizationPhrases(favorites: [String]) throws -> [String] {
        var result = Set(favorites)
        for record in try store.records(collection: Self.collection, idPrefix: "vstate-v1.mastered.phrase.") where !record.deleted {
            let envelope = try R.object(JSONSerialization.jsonObject(with: Data(record.json.utf8)), "vocabulary envelope")
            let value = try Self.normalized(R.object(envelope["value"], "vocabulary value"))
            guard value["enabled"] as? Bool == true, let key = value["key"] as? String else { continue }
            result.insert(key)
            result.formUnion(value["aliases"] as? [String] ?? [])
        }
        return result.sorted { $0.utf16.lexicographicallyPrecedes($1.utf16) }
    }

    // ECMAScript whitespace, including BOM and excluding ICU-only NEL.
    private static let whitespace = "[\\u0009-\\u000d\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]+"
    static func normalizeKey(_ value: Any?) throws -> String {
        let key = R.string(value).precomposedStringWithCompatibilityMapping
            .replacingOccurrences(of: whitespace, with: " ", options: .regularExpression)
            .trimmingCharacters(in: CharacterSet(charactersIn: " "))
            .lowercased(with: Locale(identifier: "en_US"))
        guard !key.isEmpty, key.utf8.count <= 240,
              !key.unicodeScalars.contains(where: { $0.value < 32 || $0.value == 127 }) else {
            throw Failure(message: "词汇状态键为空或过长")
        }
        return key
    }
    static func normalized(_ input: [String: Any], property: String? = nil, enabled: Bool? = nil) throws -> [String: Any] {
        func choice(_ key: String, _ allowed: [String], _ fallback: String, override: String? = nil) throws -> String {
            let raw = override ?? R.string(input[key])
            let value = (raw.isEmpty ? fallback : raw).trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
            guard allowed.contains(value) else { throw Failure(message: key + " 无效") }
            return value
        }
        let kind = try choice("kind", ["word", "phrase"], "word")
        let language = try choice("language", ["en", "ja", "und"], "und")
        let property = try choice("property", ["mastered", "favorite", "lookup"], "mastered", override: property)
        guard kind == "phrase" || property != "favorite" else { throw Failure(message: "只有词组可以收藏") }
        let source = ["lemma", "key", "text", "word"].compactMap { input[$0] }.first { !R.string($0).isEmpty }
        let key = try normalizeKey(source)
        let inputs = [input["word"], input["text"], input["surface"]].compactMap { $0 }
            + (input["forms"] as? [Any] ?? []) + (input["aliases"] as? [Any] ?? [])
        let aliases = Set(inputs.compactMap { try? normalizeKey($0) }.filter { $0 != key })
            .sorted { $0.utf16.lexicographicallyPrecedes($1.utf16) }
        guard aliases.count <= 32, aliases.reduce(0, { $0 + $1.utf8.count }) <= 4096 else {
            throw Failure(message: "词汇别名超出上限")
        }
        let encoded = Data(key.utf8).base64EncodedString().replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
        let id = ["vstate-v1", property, kind, language, encoded].joined(separator: ".")
        if let supplied = input["id"], R.string(supplied) != id { throw Failure(message: "词汇编号与内容不一致") }
        let flag = (input["enabled"] as? NSNumber).map { CFGetTypeID($0) == CFBooleanGetTypeID() && $0.boolValue } ?? false
        return ["id": id, "schema": 1, "property": property, "kind": kind, "language": language,
                "key": key, "aliases": aliases, "enabled": enabled ?? flag]
    }

    /// Read and merge aliases inside the same transaction as the causal parent,
    /// record, journal and retry receipt. No optimistic web write is required.
    func set(_ input: [String: Any], property: String, enabled: Bool, mutation: String) throws -> [String: Any] {
        let incoming = try Self.normalized(input, property: property, enabled: enabled)
        let id = incoming["id"] as! String
        guard !deviceID.isEmpty, deviceID.utf16.count <= 240, !deviceID.contains("\0"),
              !mutation.isEmpty, mutation.utf8.count <= 1024, !mutation.contains("\0") else {
            throw Failure(message: "词汇写入身份无效")
        }
        // 显式写出闭包类型：Xcode 27 的 Swift 编译器推断这个长闭包的返回类型时自身崩溃
        // （"failed to produce diagnostic for expression"，2026-09-25 Mac 本机编译实测）。
        return try store.inTransaction { () throws -> [String: Any] in
            let receiptID = "native-vocabulary:" + mutation
            if let saved = try store.mutationResult(mutationId: receiptID) {
                let receipt = try R.object(JSONSerialization.jsonObject(with: Data(saved.utf8)), "vocabulary receipt")
                guard R.same(receipt["input"] as Any, incoming) else { throw Failure(message: "同一操作编号已用于不同内容") }
                return try R.object(receipt["record"], "vocabulary record")
            }
            let current = try store.record(collection: Self.collection, id: id)
            let previous: [String: Any]?
            if let current {
                let envelope = try R.object(JSONSerialization.jsonObject(with: Data(current.json.utf8)), "vocabulary envelope")
                guard envelope["id"] as? String == id, envelope["collection"] as? String == Self.collection,
                      (envelope["rev"] as? NSNumber)?.int64Value == current.rev,
                      envelope["deleted"] as? Bool == current.deleted else { throw Failure(message: "词汇记录损坏") }
                if current.deleted { previous = nil }
                else {
                    let raw = try R.object(envelope["value"], "vocabulary value")
                    let checked = try Self.normalized(raw)
                    guard checked["id"] as? String == id, R.same(raw, checked) else { throw Failure(message: "词汇内容损坏") }
                    previous = checked
                }
            } else { previous = nil }
            var value = incoming
            if let aliases = previous?["aliases"] as? [String] {
                let merged = Set((incoming["aliases"] as! [String]) + aliases).filter { $0 != incoming["key"] as? String }
                    .sorted { $0.utf16.lexicographicallyPrecedes($1.utf16) }
                if merged.count <= 32, merged.reduce(0, { $0 + $1.utf8.count }) <= 4096 { value["aliases"] = merged }
            }
            let stamp = now()
            if let previous, R.same(previous, value) {
                try store.rememberMutationWithinTransaction(receiptID,
                    json: String(decoding: R.bytes(["input": incoming, "record": value]), as: UTF8.self), now: stamp)
                return value
            }
            let revision = current?.rev ?? 0
            guard revision >= 0, revision < 9_007_199_254_740_991 else { throw Failure(message: "词汇版本无效") }
            let parent: Any = current == nil ? NSNull() : current!.deleted ? ["deleted": true]
                : ["deleted": false, "value": previous!] as [String: Any]
            let envelope: [String: Any] = ["schema": 1, "collection": Self.collection, "id": id,
                "rev": revision + 1, "updatedAt": stamp, "updatedBy": deviceID, "deleted": false,
                "value": value, "causal": ["contract": "record-parent-state/1", "parent": parent]]
            let change: [String: Any] = ["mutationId": mutation, "operation": "put", "collection": Self.collection, "record": envelope]
            _ = try R.bytes(change)
            _ = try store.commitWithinTransaction(record: .init(collection: Self.collection, id: id, rev: revision + 1,
                updatedAt: stamp, deleted: false, json: String(decoding: R.bytes(envelope), as: UTF8.self)),
                mutationId: mutation, journalJSON: { cursor in
                    var entry = change; entry["cursor"] = cursor
                    return String(decoding: try! R.bytes(entry), as: UTF8.self)
                }, expectedRev: revision, now: stamp)
            try store.rememberMutationWithinTransaction(receiptID,
                json: String(decoding: R.bytes(["input": incoming, "record": value]), as: UTF8.self), now: stamp)
            return value
        }
    }
}
