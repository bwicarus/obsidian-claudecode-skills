import Foundation
import CoreFoundation

/// PDF highlight records keep the existing wire format. Validation and edits
/// operate on the current transaction snapshot, never on a hidden page layer.
enum ReaderNativeHighlightRules {
    enum HighlightError: LocalizedError {
        case invalid(String), missing, conflict
        var errorDescription: String? {
            switch self {
            case .invalid(let field): return "划线参数无效：" + field
            case .missing: return "未找到划线"
            case .conflict: return "划线操作编号已用于不同内容，未覆盖原记录"
            }
        }
    }
    static let palette = ["yellow":"#fff59d", "green":"#a7f3d0", "blue":"#a3d4ff", "pink":"#fda4af"]
    static func color(_ value: Any?, fallback: String = "") throws -> String {
        let text = try string(value, limit:64, fallback:fallback).trimmingCharacters(in:.whitespacesAndNewlines)
        guard text.isEmpty || text.range(of:"^#[0-9a-fA-F]{3,8}$|^(?:rgb|rgba|hsl|hsla)\\([0-9.,%\\s-]+\\)$",options:.regularExpression) != nil else {
            throw HighlightError.invalid("颜色")
        }
        return text
    }
    static func string(_ value: Any?, limit: Int, fallback: String = "") throws -> String {
        guard let value, !(value is NSNull) else { return fallback }
        guard let value = value as? String, value.utf8.count <= limit else { throw HighlightError.invalid("文字长度或类型") }
        return value
    }
    private static func number(_ value: Any?, min: Double, max: Double) throws -> Double {
        let number: Double?
        if let n = value as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID() { number = n.doubleValue }
        else if let text = value as? String { number = Double(text.trimmingCharacters(in:.whitespacesAndNewlines)) }
        else { number = nil }
        guard let n = number, n.isFinite, n >= min, n <= max else { throw HighlightError.invalid("坐标或页码") }
        return n
    }
    static func create(_ body: [String:Any], now: Int64) throws -> [String:Any] {
        let page = try number(body["page"],min:1,max:10_000_000)
        guard page.rounded() == page, let input = body["rects"] as? [[Any]], !input.isEmpty, input.count <= 2000 else {
            throw HighlightError.invalid("页码或矩形")
        }
        let rects: [[Double]] = try input.map { rect in
            guard rect.count == 4 else { throw HighlightError.invalid("矩形") }
            let r = try rect.map { try number($0,min:-100_000,max:100_000) }
            let x0 = min(r[0],r[2]), y0 = min(r[1],r[3]), x1 = max(r[0],r[2]), y1 = max(r[1],r[3])
            guard x1 > x0, y1 > y0 else { throw HighlightError.invalid("空矩形") }
            // Math.round rounds a negative half toward positive infinity.
            return [x0,y0,x1,y1].map { floor($0 * 100 + 0.5) / 100 }
        }
        let suppliedID = body["id"] as? String ?? ""
        let id = suppliedID.range(of:"^c_[a-f0-9]{8,32}$",options:.regularExpression) != nil
            ? suppliedID : "h_" + UUID().uuidString.replacingOccurrences(of:"-",with:"").lowercased().prefix(12)
        var result: [String:Any] = ["id":id,"page":Int(page),"rects":rects,
            "color":try color(body["color"],fallback:"#ffd54a"),"time":Double(now) / 1000,
            "kind":["note","translate","explain"].contains(body["kind"] as? String ?? "") ? body["kind"]! : "note"]
        for key in ["text","note","sentence","body"] { result[key] = try string(body[key],limit:key == "body" ? 8000 : 2000) }
        if let w = body["page_w"], !(w is NSNull), let h = body["page_h"], !(h is NSNull) {
            result["page_w"] = try number(w,min:1,max:100_000); result["page_h"] = try number(h,min:1,max:100_000)
        }
        return result
    }
    static func patch(_ body: [String:Any], old: [String:Any]) throws -> [String:Any] {
        var result = old
        if body.keys.contains("color") { result["color"] = try color(body["color"]) }
        for key in ["text","note","sentence","body"] where body.keys.contains(key) {
            result[key] = try string(body[key],limit:key == "body" ? 8000 : 2000)
        }
        if let kind = body["kind"] {
            guard let kind = kind as? String, ["note","translate","explain"].contains(kind) else { throw HighlightError.invalid("类型") }
            result["kind"] = kind
        }
        return result
    }
    static func fingerprint(_ highlight: [String:Any]) throws -> String {
        var fields = highlight.filter { ["id","page","rects","color","text","note","kind","sentence","body","page_w","page_h"].contains($0.key) }
        fields["surface"] = "pdf"
        return String(decoding:try JSONSerialization.data(withJSONObject:fields,options:[.sortedKeys,.withoutEscapingSlashes]),as:UTF8.self)
    }
    static func sameFingerprint(_ lhs: String, _ rhs: String) -> Bool {
        guard let a = try? JSONSerialization.jsonObject(with:Data(lhs.utf8)) as? NSDictionary,
              let b = try? JSONSerialization.jsonObject(with:Data(rhs.utf8)) as? NSDictionary else { return false }
        return a.isEqual(b)
    }
    static func boundedReceipts(_ receipts: [[String:Any]]) throws -> [[String:Any]] {
        var keep = Set<Int>(), bytes = 2
        func size(_ i: Int) throws -> Int { try JSONSerialization.data(withJSONObject:receipts[i],options:.withoutEscapingSlashes).count + 1 }
        for (i,item) in receipts.enumerated() where item["contract"] as? String == "reader-native-page-card-action/1" {
            if item["state"] as? String == "preparing" || (item["pending"] != nil && !(item["pending"] is NSNull) && item["pending"] as? Bool != false) {
                keep.insert(i); bytes += try size(i)
            }
        }
        for i in receipts.indices.reversed() where keep.count < 160 && !keep.contains(i) {
            let count = try size(i)
            if bytes + count <= 4 * 1024 * 1024 { keep.insert(i); bytes += count }
        }
        return receipts.enumerated().filter { keep.contains($0.offset) }.map(\.element)
    }
}
