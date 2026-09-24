import Foundation

/// Reads the original vocabulary records and retains the same bounded,
/// document-scoped enrichment cache. Cached remote data never replaces local
/// vocabulary, and a different text revision cannot reuse old coordinates.
final class ReaderNativePageOverlayStore {
    typealias Row = [String: Any]
    typealias R = ReaderNativeCardRules
    static let cacheKind = "page-overlay-enrichment-cache-v1"
    static let cacheCollection = "native-" + cacheKind
    private weak var indexStore: ReaderNativeDataStore?
    private var indexGeneration: UInt64?
    private var index: ReaderNativeVocabularyOverlay.Index?

    func vocabulary(_ store: ReaderNativeDataStore) throws -> ReaderNativeVocabularyOverlay.Index {
        let generation = store.generation(collection: ReaderNativeVocabularyState.collection)
        if indexStore === store, indexGeneration == generation, let index { return index }
        let values: [Row] = try store.inTransaction {
            var values: [Row] = [], offset = 0
            while true {
                let records = try store.records(collection: ReaderNativeVocabularyState.collection, limit: 500, offset: offset)
                for record in records where !record.deleted {
                    let raw = try R.object(JSONSerialization.jsonObject(with: Data(record.json.utf8)), "vocabulary envelope")
                    guard raw["id"] as? String == record.id, raw["collection"] as? String == record.collection else {
                        throw ReaderNativeVocabularyState.Failure(message: "词汇记录身份无效")
                    }
                    values.append(try R.object(raw["value"], "vocabulary value"))
                }
                if records.count < 500 { break }
                offset += records.count
            }
            return values
        }
        let value = ReaderNativeVocabularyOverlay.Index(values)
        indexStore = store; indexGeneration = generation; index = value
        return value
    }

    static func normalized(_ remote: Row, page: Int, revision: String, savedAt: Double) -> Row? {
        guard remote["ok"] as? Bool == true, page > 0, !revision.isEmpty, revision.utf16.count <= 512,
              savedAt.isFinite, savedAt >= 0,
              !revision.unicodeScalars.contains(where: { $0.value < 32 || $0.value == 127 }) else { return nil }
        let marks = remote["vocab_marks"] as? [Row] ?? [], sentences = remote["vocab_sentences"] as? [Row] ?? []
        let mastered = remote["mastered_furi"] as? [String] ?? []
        guard marks.count <= 8000, sentences.count <= 2000, mastered.count <= 8000 else { return nil }
        var entry: Row = ["page": page, "localRevision": revision, "vocab_marks": marks,
            "vocab_sentences": sentences, "mastered_furi": mastered,
            "offset": NSNull(),
            "cv": remote["cv"] as? String ?? "", "savedAt": savedAt]
        if let offset = remote["offset"] as? Row { entry["offset"] = offset }
        guard let encoded = try? R.bytes(entry), encoded.count <= 2 * 1024 * 1024 else { return nil }
        entry["byteSize"] = encoded.count
        return entry
    }

    private static func entries(_ store: ReaderNativeDataStore, bookID: String) throws -> (ReaderNativeDataStore.Record?, [Row]) {
        let id = bookID + ":" + cacheKind
        guard let record = try store.record(collection: cacheCollection, id: id) else { return (nil, []) }
        if record.deleted { return (record, []) }
        let envelope = try R.object(JSONSerialization.jsonObject(with: Data(record.json.utf8)), "overlay cache")
        let value = try R.object(envelope["value"], "overlay cache value")
        guard envelope["id"] as? String == id, envelope["collection"] as? String == cacheCollection,
              value["documentId"] as? String == bookID, value["id"] as? String == id,
              let entries = value["payload"] as? [Row] else { throw ReaderNativeVocabularyState.Failure(message: "页面缓存无效") }
        return (record, entries)
    }

    static func cached(_ store: ReaderNativeDataStore, bookID: String, page: Int, revision: String) throws -> Row? {
        let (_, items) = try entries(store, bookID: bookID)
        for item in items where (item["page"] as? NSNumber)?.intValue == page && item["localRevision"] as? String == revision {
            guard let saved = item["savedAt"] as? Double else { continue }
            var remote = item; remote["ok"] = true
            return normalized(remote, page: page, revision: revision, savedAt: saved)
        }
        return nil
    }

    static func save(_ entry: Row, store: ReaderNativeDataStore, bookID: String, deviceID: String) throws {
        guard let page = entry["page"] as? Int, let revision = entry["localRevision"] as? String,
              let saved = entry["savedAt"] as? Double else { throw ReaderNativeVocabularyState.Failure(message: "页面缓存字段无效") }
        var remote = entry; remote["ok"] = true
        guard let entry = normalized(remote, page: page, revision: revision, savedAt: saved),
              !bookID.isEmpty, !deviceID.isEmpty else { throw ReaderNativeVocabularyState.Failure(message: "页面缓存身份无效") }
        try store.inTransaction {
            let (previous, old) = try entries(store, bookID: bookID)
            var bytes = 0
            let items = ([entry] + old.filter { ($0["page"] as? NSNumber)?.intValue != page }).prefix(24).filter { row in
                guard let size = row["byteSize"] as? Int, size >= 0, bytes + size <= 2 * 1024 * 1024 else { return false }
                bytes += size; return true
            }
            let id = bookID + ":" + cacheKind, stamp = Int64(Date().timeIntervalSince1970 * 1000)
            let revision = previous?.rev ?? 0
            guard revision >= 0 && revision < 9_007_199_254_740_991 else { throw ReaderNativeVocabularyState.Failure(message: "缓存修订号无效") }
            let envelope: Row = ["schema": 1, "collection": cacheCollection, "id": id, "rev": revision + 1,
                "updatedAt": stamp, "updatedBy": deviceID, "deleted": false,
                "value": ["id": id, "documentId": bookID, "payload": items, "updatedAt": stamp]]
            // This is a local, rebuildable cache, never a remote vocabulary write.
            try store.commitWithinTransaction(record: .init(collection: cacheCollection, id: id, rev: revision + 1,
                updatedAt: stamp, deleted: false, json: String(decoding: R.bytes(envelope), as: UTF8.self)),
                mutationId: nil, journalJSON: nil, expectedRev: previous?.rev ?? 0, now: stamp)
        }
    }
}
