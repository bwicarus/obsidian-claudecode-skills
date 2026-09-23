import CryptoKit
import Foundation

/// Native read model of the existing book records. No new store or schema:
/// SQLite owns the snapshot transaction and the original IDs/revisions survive.
/// This deliberately does not write partial replicas or infer absent records
/// from a failed read.
struct ReaderNativeBookProjection {
    enum ProjectionError: Error { case invalidRequest, invalidResponse }
    let store: ReaderNativeDataStore

    func exportReadingDomains(bookID: String) throws -> [ReaderBookUserStateDomainPayload] {
        guard !bookID.isEmpty else { throw ProjectionError.invalidRequest }
        return try store.inTransaction {
            let pdfHighlights = try highlights("document-highlights", bookID: bookID)
            let epubHighlights = try highlights("epub-highlights", bookID: bookID)
            let pdfInk = try state("ink", bookID: bookID)
            let epubInk = try state("epub-ink", bookID: bookID)
            let notes = try state("document-notes-legacy", bookID: bookID)
            let position = try state("reading-position", bookID: bookID)
            return try [
                domain(.readingPosition, revision: position.revision, value: position.payload ?? NSNull()),
                domain(.notes, revision: notes.revision, value: notes.payload ?? []),
                domain(.highlights, revision: max(pdfHighlights.revision, epubHighlights.revision),
                       value: ["pdf": pdfHighlights.items, "epub": epubHighlights.items]),
                domain(.ink, revision: max(pdfInk.revision, epubInk.revision),
                       value: ["pdf": strokes(pdfInk.payload, regions: false), "epub": strokes(epubInk.payload, regions: false)]),
                domain(.closedRegions, revision: max(pdfInk.revision, epubInk.revision),
                       value: ["pdf": strokes(pdfInk.payload, regions: true), "epub": strokes(epubInk.payload, regions: true)])
            ]
        }
    }

    func state(_ kind: String, bookID: String) throws -> (revision: Int64, payload: Any?) {
        guard let record = try store.record(collection: "native-" + kind, id: bookID + ":" + kind),
              !record.deleted else { return (0, nil) }
        let value = try envelope(record, bookID: bookID)
        return (record.rev, value["payload"] is NSNull ? nil : value["payload"])
    }

    private func envelope(_ record: ReaderNativeDataStore.Record, bookID: String) throws -> [String: Any] {
        guard let raw = try JSONSerialization.jsonObject(with: Data(record.json.utf8)) as? [String: Any],
              raw["id"] as? String == record.id,
              raw["collection"] as? String == record.collection,
              let value = raw["value"] as? [String: Any],
              value["id"] as? String == record.id, value["documentId"] as? String == bookID else {
            throw ProjectionError.invalidResponse
        }
        return value
    }

    func highlights(_ kind: String, bookID: String) throws -> (revision: Int64, items: [[String: Any]]) {
        let meta = try state(kind + "-split-meta", bookID: bookID)
        // Legacy records are read in place until the existing split transaction
        // has completed. Reading is never permission to migrate or drop data.
        if meta.payload == nil {
            let old = try state(kind, bookID: bookID)
            guard old.payload == nil || old.payload is [[String: Any]] else { throw ProjectionError.invalidResponse }
            return (old.revision, old.payload as? [[String: Any]] ?? [])
        }
        guard let metadata = meta.payload as? [String: Any], let order = metadata["order"] as? [String] else {
            throw ProjectionError.invalidResponse
        }
        let prefix = "native-" + kind + "-item-v1:" + String(bookID.utf16.count) + ":" + bookID + ":"
        var byID: [String: [String: Any]] = [:]
        for record in try store.records(collection: "native-" + kind + "-items", idPrefix: prefix) where !record.deleted {
            let value = try envelope(record, bookID: bookID)
            guard let payload = value["payload"] as? [String: Any], let id = payload["id"] as? String,
                  !id.isEmpty, record.id == prefix + id else { throw ProjectionError.invalidResponse }
            if payload["deleted"] as? Bool != true { byID[id] = payload }
        }
        var seen = Set<String>()
        let ids = (order + byID.keys.sorted()).filter { byID[$0] != nil && seen.insert($0).inserted }
        return (meta.revision, ids.compactMap { byID[$0] })
    }

    private func strokes(_ value: Any?, regions: Bool) throws -> [String: [[String: Any]]] {
        guard let value else { return [:] }
        guard let surfaces = value as? [String: [[String: Any]]] else { throw ProjectionError.invalidResponse }
        return surfaces.compactMapValues { values in
            let selected = values.filter { (($0["t"] as? String) == "region") == regions }
            return selected.isEmpty ? nil : selected
        }
    }

    private func domain(_ name: ReaderBookUserStateDomainName, revision: Int64, value: Any) throws -> ReaderBookUserStateDomainPayload {
        let bytes = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .fragmentsAllowed, .withoutEscapingSlashes])
        let digest = SHA256.hash(data: bytes).map { String(format: "%02x", $0) }.joined()
        let isEmpty: Bool
        if let hosts = value as? [String: Any], [.ink, .closedRegions, .highlights].contains(name) {
            isEmpty = hosts.values.allSatisfy { ($0 as? [Any])?.isEmpty == true || ($0 as? [String: Any])?.isEmpty == true }
        } else { isEmpty = value is NSNull || (value as? [Any])?.isEmpty == true || (value as? [String: Any])?.isEmpty == true }
        let result = ReaderBookUserStateDomainPayload(name: name, revision: revision, digest: digest,
            byteCount: bytes.count, empty: isEmpty, payloadJson: String(decoding: bytes, as: UTF8.self))
        _ = try ReaderBookUserStatePackageCodec.validateDomainPayload(result, localExport: true)
        return result
    }
}
