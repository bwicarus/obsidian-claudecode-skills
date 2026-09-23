import Foundation
import CryptoKit

/// Records are removed only after the server confirms this exact mutation.
/// A disconnect, partial chunk, malformed reply or local commit failure keeps
/// the original envelope, including its stable mutation ID, available to retry.
struct ReaderNativeReplicationOutbox {
    struct Entry {
        let row: ReaderNativeDataStore.Record
        let bookID: String
        let mutationID: String
        let envelope: Data
    }
    enum OutboxError: LocalizedError {
        case corrupt, changed
        var errorDescription: String? { self == .corrupt ? "待同步记录无效，已保留待核对" : "待同步记录已变化，未按旧回执清除" }
    }
    static let collection = "native-replication-outbox"
    let store: ReaderNativeDataStore

    func pending(limit: Int = 100) throws -> [Entry] {
        try store.liveRecords(collection:Self.collection,limit:limit).map { row in
            guard let object = try JSONSerialization.jsonObject(with:Data(row.json.utf8)) as? [String:Any],
                  object["id"] as? String == row.id, object["collection"] as? String == Self.collection,
                  let value = object["value"] as? [String:Any], value["id"] as? String == row.id,
                  let book = value["documentId"] as? String, !book.isEmpty,
                  let envelope = (value["payload"] as? [String:Any])?["envelope"] as? [String:Any],
                  envelope["contract"] as? String == "replication-command/1",
                  let mutation = (envelope["op"] as? [String:Any])?["mutationId"] as? String,
                  mutation.range(of:"^mut-v2-[a-f0-9]{32}$",options:.regularExpression) != nil else { throw OutboxError.corrupt }
            return Entry(row:row,bookID:book,mutationID:mutation,
                envelope:try JSONSerialization.data(withJSONObject:envelope,options:[.sortedKeys,.withoutEscapingSlashes]))
        }
    }

    func acknowledge(_ entry: Entry, mutationID: String, outcome: String, now: Int64) throws {
        guard mutationID == entry.mutationID, outcome == "accepted" else { return }
        try store.inTransaction {
            guard let current = try store.record(collection:Self.collection,id:entry.row.id), !current.deleted else { return }
            guard current == entry.row, current.rev >= 0, current.rev < 9_007_199_254_740_991,
                  var record = try JSONSerialization.jsonObject(with:Data(current.json.utf8)) as? [String:Any] else { throw OutboxError.changed }
            let revision = current.rev + 1
            record["rev"] = revision; record["deleted"] = true; record["updatedAt"] = now
            let json = String(decoding:try JSONSerialization.data(withJSONObject:record,options:[.sortedKeys,.withoutEscapingSlashes]),as:UTF8.self)
            let id = "native-replication-ack-" + SHA256.hash(data:Data(entry.row.id.utf8)).map { String(format:"%02x",$0) }.joined()
            _ = try store.commitWithinTransaction(record:.init(collection:Self.collection,id:entry.row.id,rev:revision,updatedAt:now,deleted:true,json:json),
                mutationId:id,journalJSON:{ cursor in
                    let change: [String:Any] = ["mutationId":id,"operation":"delete","collection":Self.collection,"record":record,"cursor":cursor]
                    return String(decoding:try! JSONSerialization.data(withJSONObject:change,options:[.sortedKeys,.withoutEscapingSlashes]),as:UTF8.self)
                },expectedRev:current.rev,now:now)
        }
    }
}
