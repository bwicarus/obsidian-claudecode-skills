import Foundation

/// The existing settings records, revisions and journal stay authoritative.
/// Compatibility clients submit intent; Swift commits the complete transaction.
struct ReaderNativePreferences {
    typealias R = ReaderNativeCardRules
    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { "BW_PREFERENCE_RECORD: " + message }
    }
    struct Entry: Decodable {
        let legacyKey: String
        let collection: String
        let semanticKey: String
        let codec: String
        var id: String { "setting:" + semanticKey }
        var storeName: String { "bw-reader-native-v1-" + (collection == "user-settings" ? "global" : "device") }
    }
    struct Catalog: Decodable {
        let contract: String
        let entries: [Entry]
        init(data: Data) throws {
            self = try JSONDecoder().decode(Self.self, from: data)
            guard contract == "reader-native-preferences/1", !entries.isEmpty, entries.count <= 256,
                  Set(entries.map(\.legacyKey)).count == entries.count,
                  Set(entries.map { $0.collection + "/" + $0.id }).count == entries.count,
                  entries.allSatisfy({ ["user-settings", "device-preferences"].contains($0.collection)
                    && ["string", "boolean-string", "number-string", "json-string"].contains($0.codec)
                    && !$0.legacyKey.isEmpty && !$0.semanticKey.isEmpty }) else {
                throw Failure(message: "设置目录无效")
            }
        }
        static let packaged: Result<Catalog, Error> = Result {
            guard let base = Bundle.main.resourceURL else { throw Failure(message: "设置目录缺失") }
            return try Catalog(data: Data(contentsOf: base.appendingPathComponent("ReaderBundle/native_reader_preference_manifest.json")))
        }
        func entry(_ key: String) throws -> Entry {
            guard let entry = entries.first(where: { $0.legacyKey == key }) else { throw Failure(message: "设置键未登记：" + key) }
            return entry
        }
    }
    let store: ReaderNativeDataStore
    let deviceID: String
    var now: () -> Int64 = { Int64(Date().timeIntervalSince1970 * 1000) }

    static func rawValue(_ value: Any?) throws -> String {
        guard let raw = value as? String, raw.utf8.count <= 65_536 else { throw Failure(message: "设置值不是字符串或超过上限") }
        return raw
    }
    func record(_ entry: Entry) throws -> [String: Any]? {
        guard let row = try store.record(collection: entry.collection, id: entry.id) else { return nil }
        let envelope = try R.object(JSONSerialization.jsonObject(with: Data(row.json.utf8)), "preference envelope")
        guard envelope["collection"] as? String == entry.collection, envelope["id"] as? String == entry.id,
              try R.integer(envelope["rev"], "revision") == row.rev,
              try R.bool(envelope["deleted"], "deleted") == row.deleted else { throw Failure(message: "设置记录身份不一致") }
        if !row.deleted {
            let value = try R.object(envelope["value"], "preference value")
            guard value["legacyKey"] as? String == entry.legacyKey,
                  value["semanticKey"] as? String == entry.semanticKey else { throw Failure(message: "设置内容与目录不一致") }
            _ = try Self.rawValue(value["rawValue"])
        }
        return envelope
    }
    func raw(_ entry: Entry) throws -> String? {
        guard let record = try record(entry), record["deleted"] as? Bool != true else { return nil }
        return try Self.rawValue((record["value"] as? [String: Any])?["rawValue"])
    }
    func commit(_ entry: Entry, raw: String?, mutation: String, expectedRevision: Int64? = nil) throws -> [String: Any] {
        guard !deviceID.isEmpty, deviceID.utf16.count <= 240, !deviceID.contains("\0"),
              !mutation.isEmpty, mutation.utf8.count <= 1024, !mutation.contains("\0") else { throw Failure(message: "设置操作身份无效") }
        if let raw { _ = try Self.rawValue(raw) }
        let input: [String: Any] = ["key": entry.legacyKey, "raw": raw as Any? ?? NSNull(),
                                   "ifRev": expectedRevision as Any? ?? NSNull()]
        return try store.inTransaction {
            let receiptID = "native-preference:" + mutation
            if let saved = try store.mutationResult(mutationId: receiptID) {
                let receipt = try R.object(JSONSerialization.jsonObject(with: Data(saved.utf8)), "preference receipt")
                guard R.same(receipt["input"] as Any, input) else { throw Failure(message: "同一操作编号用于不同设置") }
                return ["ok": true, "result": receipt["record"]!, "changes": []]
            }
            let previous = try record(entry)
            let revision = try previous.map { try R.integer($0["rev"], "revision") } ?? 0
            if let expectedRevision, expectedRevision != revision {
                throw ReaderNativeDataStore.StoreError.revisionConflict(collection: entry.collection, id: entry.id, actual: revision)
            }
            guard revision < 9_007_199_254_740_991 else { throw Failure(message: "设置版本超出上限") }
            let value: [String: Any]
            if let raw {
                value = ["id": entry.id, "legacyKey": entry.legacyKey, "semanticKey": entry.semanticKey,
                         "codec": entry.codec, "rawValue": raw, "migration": "preference-store-v1"]
            } else { value = previous?["value"] as? [String: Any] ?? ["id": entry.id] }
            let stamp = now()
            var envelope: [String: Any] = ["schema": 1, "collection": entry.collection, "id": entry.id,
                "rev": revision + 1, "updatedAt": stamp, "updatedBy": deviceID, "deleted": raw == nil, "value": value]
            if entry.collection == "user-settings" {
                let parent: Any = previous == nil ? NSNull() : previous!["deleted"] as? Bool == true
                    ? ["deleted": true] : ["deleted": false, "value": previous!["value"]!] as [String: Any]
                envelope["causal"] = ["contract": "record-parent-state/1", "parent": parent]
            }
            var change: [String: Any] = ["mutationId": mutation, "operation": raw == nil ? "remove" : "put",
                                        "collection": entry.collection, "record": envelope]
            _ = try R.bytes(change)
            let cursor = try store.commitWithinTransaction(record: .init(collection: entry.collection, id: entry.id,
                rev: revision + 1, updatedAt: stamp, deleted: raw == nil, json: String(decoding: R.bytes(envelope), as: UTF8.self)),
                mutationId: mutation, journalJSON: { cursor in
                    var journal = change; journal["cursor"] = cursor
                    return String(decoding: try! R.bytes(journal), as: UTF8.self)
                }, expectedRev: revision, now: stamp)
            change["cursor"] = cursor
            try store.rememberMutationWithinTransaction(receiptID,
                json: String(decoding: R.bytes(["input": input, "record": envelope]), as: UTF8.self), now: stamp)
            return ["ok": true, "result": envelope, "changes": [change]]
        }
    }
}
