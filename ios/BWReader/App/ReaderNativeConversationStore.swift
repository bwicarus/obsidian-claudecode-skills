import Foundation
import CoreFoundation
import CryptoKit

/// Ordered conversation data independent of WebKit nodes or a SwiftUI view's
/// lifetime. Transport batches are atomic and fenced by scope/base revision.
struct ReaderNativeConversationStore {
    enum Failure: LocalizedError {
        case malformed, missingBase
        var errorDescription: String? {
            switch self {
            case .malformed: return "对话更新数据不完整"
            case .missingBase: return "对话更新已跳序，正在重新同步"
            }
        }
    }
    private(set) var scope = ""
    private(set) var revision: Int64 = 0
    private var records: [String:[String:Any]] = [:]
    private var order: [String] = []
    var messages: [[String:Any]] { order.compactMap { records[$0] } }

    mutating func apply(_ batch:[String:Any], scope nextScope:String) throws -> Bool {
        func integer(_ key:String) throws -> Int64 {
            guard let number = batch[key] as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
                  number.doubleValue.isFinite, number.doubleValue >= 0, number.doubleValue <= 9_007_199_254_740_991,
                  number.doubleValue.rounded() == number.doubleValue else { throw Failure.malformed }
            return number.int64Value
        }
        let nextRevision = try integer("revision"), base = try integer("baseRevision")
        guard !nextScope.isEmpty, batch["contract"] as? String == "reader-native-conversation-delta/1",
              let full = batch["reset"] as? Bool, let updates = batch["upserts"] as? [[String:Any]],
              let ids = batch["order"] as? [String], ids.count <= 10_000, Set(ids).count == ids.count,
              ids.allSatisfy({ !$0.isEmpty && $0.utf16.count <= 2048 }), JSONSerialization.isValidJSONObject(updates) else { throw Failure.malformed }
        if nextScope == scope, nextRevision <= revision { return false }
        guard nextRevision > base, full || (nextScope == scope && base == revision) else { throw Failure.missingBase }
        var next = full ? [:] : records
        var seen = Set<String>()
        for message in updates {
            guard let id = message["id"] as? String, ids.contains(id), seen.insert(id).inserted,
                  let role = message["role"] as? String, ["assistant","user","system","status","tool"].contains(role),
                  message["text"] is String, message["parts"] is [[String:Any]], message["streaming"] is Bool else { throw Failure.malformed }
            next[id] = message
        }
        guard ids.allSatisfy({next[$0] != nil}) else { throw Failure.missingBase }
        let keep = Set(ids)
        next = next.filter { keep.contains($0.key) }
        scope = nextScope; revision = nextRevision; records = next; order = ids
        return true
    }

    static func fingerprint(_ messages:[[String:Any]]) -> String? {
        guard JSONSerialization.isValidJSONObject(messages),
              let data = try? JSONSerialization.data(withJSONObject:messages,options:[.sortedKeys,.withoutEscapingSlashes]) else { return nil }
        return SHA256.hash(data:data).map { String(format:"%02x",$0) }.joined()
    }
}
