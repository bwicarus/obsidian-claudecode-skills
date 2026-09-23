import Foundation
import CoreFoundation

/// Native implementation of user-state-merge/1. Browser/extension clients keep
/// their own implementation; shared reference fixtures check both against the
/// same conflict, tombstone and stroke-union decisions before packaging.
enum ReaderNativeBookMerge {
    struct Result {
        let value: Any
        let changed: Bool
        let unknown: Bool
    }
    static func merge(domain: String, base: Any?, mine: Any?, theirs: Any?) throws -> Result {
        // Validate all inputs before examining a branch. Invalid input must not
        // silently choose one side of a cloud conflict.
        _ = try encoded(base); _ = try encoded(mine); _ = try encoded(theirs)
        let value: Any
        if try same(mine, theirs) { value = mine ?? NSNull() }
        else {
            switch domain {
            case "reading-position": value = try position(base, mine, theirs)
            case "highlights": value = try sided(base, mine, theirs, using: collection)
            case "ink", "closed-regions": value = try sided(base, mine, theirs, using: strokes)
            case "notes", "user-pages", "card-placements", "entity-references": value = try collection(base, mine, theirs)
            default: return Result(value: try copied(mine), changed: false, unknown: true)
            }
        }
        return Result(value: try copied(value), changed: try !same(value, mine), unknown: false)
    }
    static func empty(domain: String, value: Any) -> Bool {
        func emptyContainer(_ value: Any?) -> Bool {
            if let array = value as? [Any] { return array.isEmpty }
            if let object = value as? [String: Any] { return object.isEmpty }
            return false
        }
        if ["highlights", "ink", "closed-regions"].contains(domain) {
            let sides = value as? [String: Any] ?? [:]
            return emptyContainer(sides["pdf"]) && emptyContainer(sides["epub"])
        }
        return value is NSNull || (value as? String == "") || emptyContainer(value)
    }
    private static func encoded(_ value: Any?) throws -> Data {
        try JSONSerialization.data(withJSONObject: value ?? NSNull(), options: [.sortedKeys, .fragmentsAllowed, .withoutEscapingSlashes])
    }
    private static func same(_ a: Any?, _ b: Any?) throws -> Bool { try encoded(a) == encoded(b) }
    private static func copied(_ value: Any?) throws -> Any { try JSONSerialization.jsonObject(with: encoded(value), options: [.fragmentsAllowed]) }
    private static func list(_ value: Any?) -> [Any] { value as? [Any] ?? [] }
    private static func object(_ value: Any?) -> [String: Any] { value as? [String: Any] ?? [:] }

    // Number(value) compatibility is important for old snapshots with a string
    // revision or explicit null. Missing is different from null (NaN vs zero).
    private static func number(_ value: Any?) -> Double? {
        guard let value else { return nil }
        if value is NSNull { return 0 }
        if let number = value as? NSNumber { return number.doubleValue.isFinite ? number.doubleValue : nil }
        if let array = value as? [Any] {
            if array.isEmpty { return 0 }
            if array.count == 1 {
                if array[0] is NSNull { return 0 }
                // JS converts arrays to strings before Number: [true] becomes
                // "true" (NaN), while a bare true is 1.
                if let n = array[0] as? NSNumber, CFGetTypeID(n) == CFBooleanGetTypeID() { return nil }
                if array[0] is NSNumber || array[0] is String || array[0] is [Any] { return number(array[0]) }
            }
            return nil
        }
        if let text = value as? String {
            let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            if trimmed.isEmpty { return 0 }
            if let number = Double(trimmed), number.isFinite { return number }
            for (prefix, radix) in [("0x",16), ("0b",2), ("0o",8)] where trimmed.lowercased().hasPrefix(prefix) {
                if let n = UInt64(trimmed.dropFirst(2), radix: radix) { return Double(n) }
            }
        }
        return nil
    }
    private static func version(_ value: Any?) -> Double? {
        let record = object(value)
        for key in ["rev", "time", "updatedAt", "ts"] { if let n = number(record[key]) { return n } }
        return nil
    }
    private static func key(_ value: Any) -> String? {
        let record = object(value)
        for field in ["id", "placementId", "entityId"] { if let k = record[field] as? String, !k.isEmpty { return k } }
        return nil
    }
    private static func index(_ value: Any?) -> [String: Any] {
        var result: [String: Any] = [:]
        for item in list(value) { if let key = key(item) { result[key] = item } }
        return result
    }
    private static func collection(_ base: Any?, _ mine: Any?, _ theirs: Any?) throws -> Any {
        let b = index(base), m = index(mine), t = index(theirs)
        var seen = Set<String>(), result: [Any] = []
        for item in list(mine) + list(theirs) {
            guard let key = key(item), seen.insert(key).inserted else { continue }
            let bv = b[key], mv = m[key], tv = t[key]
            let picked: Any?
            if try same(mv, bv) { picked = tv }
            else if try same(tv, bv) { picked = mv }
            else if try same(mv, tv) { picked = mv }
            else if let tr = version(tv), let mr = version(mv), tr > mr { picked = tv }
            else if mv == nil, version(tv) != nil { picked = tv }
            else { picked = mv }
            if let picked, !(picked is NSNull) { result.append(picked) }
        }
        return result
    }
    private static func strokes(_ base: Any?, _ mine: Any?, _ theirs: Any?) throws -> Any {
        let b = object(base), m = object(mine), t = object(theirs)
        var result: [String: Any] = [:]
        for key in Set(m.keys).union(t.keys).sorted() {
            let bv = list(b[key]), mv = list(m[key]), tv = list(t[key])
            let selected: [Any]
            if try same(mv, bv) { selected = tv }
            else if try same(tv, bv) { selected = mv }
            else {
                var union: [Any] = [], seen = Set<Data>()
                for stroke in mv + tv { if try seen.insert(encoded(stroke)).inserted { union.append(stroke) } }
                selected = union
            }
            if !selected.isEmpty { result[key] = selected }
        }
        return result
    }
    private static func position(_ base: Any?, _ mine: Any?, _ theirs: Any?) throws -> Any {
        if try same(mine, base) { return theirs ?? NSNull() }
        if try same(theirs, base) { return mine ?? NSNull() }
        if let mr = version(mine), let tr = version(theirs), tr > mr { return theirs ?? NSNull() }
        return mine ?? NSNull()
    }
    private static func sided(_ base: Any?, _ mine: Any?, _ theirs: Any?, using merge: (Any?, Any?, Any?) throws -> Any) throws -> Any {
        let b = object(base), m = object(mine), t = object(theirs)
        return ["pdf": try merge(b["pdf"], m["pdf"], t["pdf"]), "epub": try merge(b["epub"], m["epub"], t["epub"])]
    }
}
