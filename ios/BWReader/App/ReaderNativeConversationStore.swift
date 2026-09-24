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
    private var groups: [String:[String]] = [:]
    var messages: [[String:Any]] { order.compactMap { records[$0] } }

    mutating func apply(_ batch:[String:Any], scope nextScope:String) throws -> Bool {
        func integer(_ key:String) throws -> Int64 {
            guard let number = batch[key] as? NSNumber, CFGetTypeID(number) != CFBooleanGetTypeID(),
                  number.doubleValue.isFinite, number.doubleValue >= 0, number.doubleValue <= 9_007_199_254_740_991,
                  number.doubleValue.rounded() == number.doubleValue else { throw Failure.malformed }
            return number.int64Value
        }
        let nextRevision = try integer("revision"), base = try integer("baseRevision")
        if batch["contract"] as? String == "reader-native-conversation-delta/2" {
            return try applyEvents(batch, scope:nextScope, nextRevision:nextRevision, base:base)
        }
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
        scope = nextScope; revision = nextRevision; records = next; order = ids; groups = ["thread":ids]
        return true
    }

    /// Producers report lifecycle events, not a scan of hidden page nodes.
    /// History is prepared in a separate group then atomically adopted; live
    /// turns moved into that group retain their identity and position.
    private mutating func applyEvents(_ batch:[String:Any], scope nextScope:String, nextRevision:Int64, base:Int64) throws -> Bool {
        guard !nextScope.isEmpty, let reset = batch["reset"] as? Bool,
              let updates = batch["upserts"] as? [[String:Any]],
              let events = batch["events"] as? [[String:Any]], events.count <= 50_000,
              JSONSerialization.isValidJSONObject(updates) else { throw Failure.malformed }
        if nextScope == scope, nextRevision <= revision { return false }
        guard nextRevision > base, reset || (nextScope == scope && base == revision) else { throw Failure.missingBase }
        var nextGroups = reset ? [:] : groups, nextRecords = reset ? [:] : records
        func identity(_ value:Any?) throws -> String {
            guard let value = value as? String, !value.isEmpty, value.utf16.count <= 2048 else { throw Failure.malformed }; return value
        }
        for event in events {
            switch event["action"] as? String {
            case "place":
                let id = try identity(event["id"]), group = try identity(event["group"])
                let before = event["before"] as? String
                if let from = event["from"] { nextGroups[try identity(from)]?.removeAll { $0 == id } }
                var members = nextGroups[group] ?? []
                let replacedIndex = before == id ? members.firstIndex(of:id) : nil
                members.removeAll { $0 == id }
                if let replacedIndex { members.insert(id,at:min(replacedIndex,members.count)) }
                else if let before, let index = members.firstIndex(of:before) { members.insert(id,at:index) }
                else { members.append(id) }
                nextGroups[group] = members
            case "remove":
                let id = try identity(event["id"])
                let group = try identity(event["group"])
                nextGroups[group]?.removeAll { $0 == id }
            case "clear": nextGroups.removeValue(forKey:try identity(event["group"]))
            case "adopt":
                let group = try identity(event["group"])
                guard group != "thread" else { throw Failure.malformed }
                nextGroups["thread"] = nextGroups.removeValue(forKey:group) ?? []
            default: throw Failure.malformed
            }
        }
        let members = Set(nextGroups.values.flatMap { $0 })
        guard members.count <= 10_000, nextGroups.count <= 128 else { throw Failure.malformed }
        if let erased = batch["erased"] as? [String] { for id in erased { nextRecords.removeValue(forKey:id) } }
        var seen = Set<String>()
        for message in updates {
            let id = try identity(message["id"])
            guard members.contains(id), seen.insert(id).inserted,
                  let role = message["role"] as? String, ["assistant","user","system","status","tool"].contains(role),
                  message["text"] is String, message["parts"] is [[String:Any]], message["streaming"] is Bool else { throw Failure.malformed }
            nextRecords[id] = message
        }
        nextRecords = nextRecords.filter { members.contains($0.key) }
        scope = nextScope; revision = nextRevision; groups = nextGroups; records = nextRecords
        order = nextGroups["thread"] ?? []
        return true
    }

    static func fingerprint(_ messages:[[String:Any]]) -> String? {
        guard JSONSerialization.isValidJSONObject(messages),
              let data = try? JSONSerialization.data(withJSONObject:messages,options:[.sortedKeys,.withoutEscapingSlashes]) else { return nil }
        return SHA256.hash(data:data).map { String(format:"%02x",$0) }.joined()
    }
}
