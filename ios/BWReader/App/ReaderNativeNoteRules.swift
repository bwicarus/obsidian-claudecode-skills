import Foundation
import CoreFoundation

/// The existing /pdf/api/notes contract, without a webpage, fetch shim or DOM.
/// This function is pure; BookStore commits its result and derived indexes in
/// one transaction and records the operation receipt for safe retries.
enum ReaderNativeNoteRules {
    struct Outcome {
        let notes: [[String: Any]]
        let result: [String: Any]
    }
    enum NoteError: LocalizedError {
        case invalid(String), missing
        var errorDescription: String? {
            switch self {
            case .invalid(let field): return "便签参数无效：" + field
            case .missing: return "未找到便签"
            }
        }
    }
    private static let fields: Set<String> = ["file", "id", "anchor", "text", "color", "w", "h", "collapsed", "strokes", "video", "card", "html", "iar"]

    static func apply(method: String, body: [String: Any], notes: [[String: Any]], file: String,
                      now: Int64, newID: () -> String) throws -> Outcome {
        guard body["file"] as? String == file else { throw NoteError.invalid("file") }
        let allowed: Set<String> = method == "DELETE" ? ["file", "id"] : fields
        guard Set(body.keys).isSubset(of: allowed), ["POST", "PATCH", "DELETE"].contains(method) else {
            throw NoteError.invalid("请求字段或方法")
        }
        let stamp = now / 1000
        var output = notes
        if method == "POST" {
            guard let rawAnchor = body["anchor"] else { throw NoteError.invalid("anchor") }
            let proposed = body["id"] as? String ?? ""
            let id = proposed.range(of: "^c_[a-f0-9]{8,32}$", options: .regularExpression) != nil ? proposed : newID()
            let note: [String: Any] = [
                "id": id, "anchor": try anchor(rawAnchor),
                "text": try string(body["text"], limit: 8000, fallback: ""),
                "color": try string(body["color"], limit: 64, fallback: "#fff8c5", nonempty: true),
                "w": try number(body["w"], min: 40, max: 4096, fallback: 260).rounded(.toNearestOrAwayFromZero),
                "h": try number(body["h"], min: 40, max: 4096, fallback: 180).rounded(.toNearestOrAwayFromZero),
                "collapsed": body["collapsed"] as? Bool == true,
                "strokes": try strokes(body["strokes"] is NSNull ? [] : body["strokes"] ?? []),
                "video": try bounded(body["video"], bytes: 512 * 1024),
                "card": try bounded(body["card"], bytes: 2 * 1024 * 1024),
                "html": try bounded(body["html"], bytes: 2 * 1024 * 1024),
                "iar": try ratio(body["iar"]), "created": stamp, "updated": stamp
            ]
            output.removeAll { $0["id"] as? String == id }
            output.append(note)
            return Outcome(notes: output, result: ["ok": true, "id": id, "note": note])
        }
        let id = (body["id"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        guard id.range(of: "^[A-Za-z0-9_-]{2,96}$", options: .regularExpression) != nil else { throw NoteError.invalid("id") }
        guard
              let index = output.firstIndex(where: { $0["id"] as? String == id }) else { throw NoteError.missing }
        if method == "DELETE" {
            output.remove(at: index)
            return Outcome(notes: output, result: ["ok": true])
        }
        var note = output[index]
        for (key, value) in body {
            switch key {
            case "anchor": note[key] = try anchor(value)
            case "text": note[key] = try string(value, limit: 8000, fallback: "")
            case "color": note[key] = try string(value, limit: 64, fallback: "#fff8c5", nonempty: true)
            case "w", "h": note[key] = try number(value, min: 40, max: 4096).rounded(.toNearestOrAwayFromZero)
            case "collapsed":
                guard let n = value as? NSNumber, CFGetTypeID(n) == CFBooleanGetTypeID() else { throw NoteError.invalid(key) }
                note[key] = n.boolValue
            case "strokes": note[key] = try strokes(value)
            case "video", "card", "html": note[key] = try bounded(value, bytes: key == "video" ? 512 * 1024 : 2 * 1024 * 1024)
            case "iar": note[key] = try ratio(value)
            default: break
            }
        }
        note["updated"] = stamp; output[index] = note
        return Outcome(notes: output, result: ["ok": true, "note": note])
    }

    private static func number(_ value: Any?, min: Double, max: Double, fallback: Double? = nil, integer: Bool = false) throws -> Double {
        if value == nil || value is NSNull {
            if let fallback { return fallback }
            throw NoteError.invalid("数值缺失")
        }
        let result: Double?
        if let n = value as? NSNumber, !integer || CFGetTypeID(n) != CFBooleanGetTypeID() { result = n.doubleValue }
        else if !integer, let text = value as? String {
            let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            result = trimmed.isEmpty ? 0 : Double(trimmed)
        } else { result = nil }
        guard let result, result.isFinite, result >= min, result <= max,
              !integer || result.rounded() == result else { throw NoteError.invalid("数值范围") }
        return result
    }
    private static func string(_ value: Any?, limit: Int, fallback: String, nonempty: Bool = false) throws -> String {
        guard value != nil && !(value is NSNull) else { return fallback }
        guard var text = value as? String else { throw NoteError.invalid("文字") }
        if nonempty { text = text.trimmingCharacters(in: .whitespacesAndNewlines) }
        guard text.utf8.count <= limit else { throw NoteError.invalid("文字长度") }
        return text
    }
    private static func bounded(_ value: Any?, bytes: Int) throws -> Any {
        let value = value ?? NSNull()
        let data = try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys, .fragmentsAllowed, .withoutEscapingSlashes])
        guard data.count <= bytes else { throw NoteError.invalid("内容过长") }
        return value
    }
    private static func ratio(_ value: Any?) throws -> Any {
        if value == nil || value is NSNull { return NSNull() }
        return try number(value, min: 0.01, max: 100)
    }
    private static func strokes(_ value: Any) throws -> [[String: Any]] {
        guard let list = value as? [[String: Any]], list.count <= 5000 else { throw NoteError.invalid("strokes") }
        _ = try bounded(list, bytes: 16 * 1024 * 1024)
        return list
    }
    private static func anchor(_ value: Any) throws -> [String: Any] {
        guard let a = value as? [String: Any], let kind = a["kind"] as? String,
              ["pdf", "epub"].contains(kind),
              Set(a.keys).isSubset(of: ["kind", "page", "section", "x", "y", "off", "dx", "dy", "clamped"]) else { throw NoteError.invalid("anchor") }
        var out: [String: Any] = ["kind": kind]
        let key = kind == "pdf" ? "page" : "section"
        if let virtual = a[key] as? String, virtual.range(of: "^u_[0-9a-fA-F]{4,16}$", options: .regularExpression) != nil {
            out[key] = virtual
        } else { out[key] = try number(a[key], min: kind == "pdf" ? 1 : 0, max: 10_000_000, integer: true) }
        for (key, limits) in ["x": (-0.05,1.05), "y": (-0.05,1.05), "dx": (-1_000_000.0,1_000_000.0), "dy": (-1_000_000.0,1_000_000.0), "off": (0.0,100_000_000.0)] {
            if let value = a[key], !(value is NSNull) { out[key] = try number(value, min: limits.0, max: limits.1, integer: key == "off") }
        }
        if let value = a["clamped"], !(value is NSNull) {
            out["clamped"] = (value as? NSNumber)?.boolValue == true ? 1 : 0
        }
        return out
    }
}
