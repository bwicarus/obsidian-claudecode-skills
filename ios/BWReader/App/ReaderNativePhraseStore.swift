import Foundation
import CoreFoundation

/// Existing device phrase record, kept in its original collection/envelope.
/// Native OCR, vocabulary and server mirrors are derived effects. Their intent
/// is durable alongside the list so interruption never loses an accepted edit.
struct ReaderNativePhraseStore {
    typealias R = ReaderNativeCardRules
    struct Snapshot {
        let phrases: [String]
        let seeded: Bool
        let revision: Int64
    }
    let store: ReaderNativeDataStore
    let deviceID: String
    static let kind = "phrase-favorites"
    static let collection = "native-phrase-favorites"
    private var id: String { deviceID + ":" + Self.kind }
    private var effectsKey: String { "native-phrase-effects:" + deviceID }
    private static let whitespace = "[\\u0009-\\u000d\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]+"

    static func normalize(_ text: String) -> String {
        text.replacingOccurrences(of: whitespace, with: "", options: .regularExpression)
    }

    func read() throws -> Snapshot {
        guard !deviceID.isEmpty, deviceID.utf16.count <= 240, !deviceID.contains("\0"),
              try store.meta("legacyImport") == "done" else { throw ReaderNativeVocabularyState.Failure(message: "词组库尚未就绪") }
        guard let row = try store.record(collection: Self.collection, id: id) else {
            return Snapshot(phrases: [], seeded: false, revision: 0)
        }
        let raw = try R.object(JSONSerialization.jsonObject(with: Data(row.json.utf8)), "phrase envelope")
        guard raw["id"] as? String == id, raw["collection"] as? String == Self.collection,
              (raw["rev"] as? NSNumber)?.int64Value == row.rev,
              raw["deleted"] as? Bool == row.deleted else {
            throw ReaderNativeVocabularyState.Failure(message: "词组版本损坏，未覆盖原数据")
        }
        if row.deleted { return Snapshot(phrases: [], seeded: false, revision: row.rev) }
        let value = try R.object(raw["value"], "phrase value")
        let payload = try R.object(value["payload"], "phrase payload")
        guard raw["id"] as? String == id, raw["collection"] as? String == Self.collection,
              (raw["rev"] as? NSNumber)?.int64Value == row.rev,
              value["id"] as? String == id, value["deviceId"] as? String == deviceID,
              let phrases = payload["phrases"] as? [String], let seeded = payload["seeded"] as? Bool else {
            throw ReaderNativeVocabularyState.Failure(message: "词组记录损坏，未覆盖原数据")
        }
        var seen = Set<String>()
        return Snapshot(phrases: phrases.map(Self.normalize).filter { !$0.isEmpty && seen.insert($0).inserted },
                        seeded: seeded, revision: row.rev)
    }

    func seed(_ phrases: [String]) throws -> Snapshot {
        try store.inTransaction {
            let current = try read()
            guard !current.seeded, current.phrases.isEmpty else { return current }
            var seen = Set<String>()
            let values = phrases.map(Self.normalize).filter { !$0.isEmpty && seen.insert($0).inserted }
            return try write(values, previous: current, change: nil)
        }
    }

    func set(_ text: String, enabled: Bool) throws -> Snapshot {
        let phrase = Self.normalize(text)
        guard !phrase.isEmpty, phrase.utf16.count <= 64, !phrase.contains("\0") else {
            throw ReaderNativeVocabularyState.Failure(message: "词组无效")
        }
        return try store.inTransaction {
            let current = try read()
            guard current.seeded || !current.phrases.isEmpty else {
                throw ReaderNativeVocabularyState.Failure(message: "历史词组尚未取回，请稍后再试")
            }
            var values = current.phrases
            if enabled && !values.contains(phrase) { values.append(phrase) }
            if !enabled { values.removeAll { $0 == phrase } }
            // Explicit desired state makes double taps/unknown receipts safe.
            if values == current.phrases { return current }
            return try write(values, previous: current, change: ["text": phrase, "enabled": enabled,
                "mutation": "native-phrase:" + UUID().uuidString])
        }
    }

    func pendingEffects() throws -> [String: Any]? {
        guard let data = try store.meta(effectsKey) else { return nil }
        let value = try R.object(JSONSerialization.jsonObject(with: Data(data.utf8)), "phrase effects")
        return value.isEmpty ? nil : value
    }

    func completeEffects(revision: Int64) throws {
        try store.inTransaction {
            guard let current = try pendingEffects(), (current["revision"] as? NSNumber)?.int64Value == revision else { return }
            try store.putMeta(effectsKey, json: "{}")
        }
    }

    private func write(_ phrases: [String], previous: Snapshot, change: [String: Any]?) throws -> Snapshot {
        guard previous.revision < 9_007_199_254_740_991 else { throw ReaderNativeVocabularyState.Failure(message: "词组版本无效") }
        let stamp = Int64(Date().timeIntervalSince1970 * 1000), revision = previous.revision + 1
        let mutation = "native-phrases-list:" + UUID().uuidString
        let value: [String: Any] = ["id": id, "deviceId": deviceID, "updatedAt": stamp,
                                    "payload": ["phrases": phrases, "seeded": true]]
        let envelope: [String: Any] = ["schema": 1, "collection": Self.collection, "id": id,
            "rev": revision, "updatedAt": stamp, "updatedBy": deviceID, "deleted": false, "value": value]
        _ = try store.commitWithinTransaction(record: .init(collection: Self.collection, id: id, rev: revision,
            updatedAt: stamp, deleted: false, json: String(decoding: R.bytes(envelope), as: UTF8.self)),
            mutationId: mutation, journalJSON: { cursor in
                String(decoding: try! R.bytes(["cursor": cursor, "mutationId": mutation, "operation": "put",
                                               "collection": Self.collection, "record": envelope]), as: UTF8.self)
            }, expectedRev: previous.revision, now: stamp)
        var changes = try pendingEffects()?["changes"] as? [[String: Any]] ?? []
        if let change {
            changes.removeAll { $0["text"] as? String == change["text"] as? String }; changes.append(change)
        }
        try store.putMeta(effectsKey, json: String(decoding: R.bytes([
            "revision": revision, "phrases": phrases, "changes": changes
        ]), as: UTF8.self))
        return Snapshot(phrases: phrases, seeded: true, revision: revision)
    }
}
