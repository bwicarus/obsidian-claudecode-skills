import Foundation

/// Session-scoped PDF figures and explicit attachments. Pages may be evicted;
/// explicitly attached figures survive paging until removed or consumed.
final class ReaderNativeFigures {
    struct Failure: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }
    private(set) var bookID = ""
    private(set) var epoch = UUID().uuidString
    private(set) var revision: UInt64 = 0
    private var pages: [Int: [[String: Any]]] = [:]
    private var order: [Int] = []
    private var attached: [[String: Any]] = []
    var hasAttachments: Bool { !attached.isEmpty }

    func open(_ book: String) {
        guard book != bookID else { return }
        bookID = book; epoch = UUID().uuidString; revision = 0
        pages.removeAll(); order.removeAll(); attached.removeAll()
    }

    // JS toFixed(3) rounds the actual binary value, ties away from zero. printf
    // uses ties-to-even; multiplying Double by 1000 first also changes ties.
    static func fixed3(_ value: Double) -> String {
        let bits = abs(value).bitPattern
        let exponent = Int((bits >> 52) & 0x7ff)
        let mantissa = (bits & 0x000f_ffff_ffff_ffff) | (exponent > 0 ? 1 << 52 : 0)
        let shift = (exponent > 0 ? exponent - 1023 : -1022) - 52
        let scaled = mantissa * 1000
        let units: UInt64
        if shift < -63 { units = 0 }
        else if shift < 0 {
            let count = -shift
            let remainder = scaled & ((UInt64(1) << count) - 1)
            units = (scaled >> count) + (remainder >= UInt64(1) << (count - 1) ? 1 : 0)
        } else { units = scaled << shift }
        return (value < 0 ? "-" : "") + String(units / 1000) + "." + String(format: "%03llu", units % 1000)
    }

    static func normalized(_ input: [[String: Any]], page: Int) -> [[String: Any]] {
        guard page > 0 else { return [] }
        return input.prefix(24).compactMap { figure in
            let full = figure["fbox"] as? [Double]
            guard let box = (full?.count == 4 ? full : figure["bbox"] as? [Double]), box.count == 4,
                  box.allSatisfy({ $0.isFinite && abs($0) <= 16 }), box[2] > box[0], box[3] > box[1] else { return nil }
            let badge = (figure["badge"] as? [Double]).flatMap { values -> [Double]? in
                values.count == 2 && values.allSatisfy({ $0.isFinite && abs($0) <= 16 }) ? values : nil
            }
            return ["id": String(page) + ":" + box.map(fixed3).joined(separator: ","), "page": page,
                "box": box, "badge": badge as Any? ?? NSNull(), "caption": figure["caption"] as? String ?? "",
                "desc": String((figure["desc"] as? String ?? "").prefix(6000)), "group": figure["group"] as? Bool == true]
        }
    }

    func accept(_ response: [String: Any], page: Int) throws -> Bool {
        guard response["ok"] as? Bool == true,
              response["figures"] == nil || response["figures"] is [[String: Any]] else {
            throw Failure(message: "插图数据未就绪")
        }
        let figures = response["figures"] as? [[String: Any]] ?? []
        let rows = Self.normalized(figures, page: page)
        guard let bytes = try? JSONSerialization.data(withJSONObject: rows), bytes.count <= 1024 * 1024 else {
            throw Failure(message: "插图数据过大")
        }
        order.removeAll { $0 == page }; order.append(page); pages[page] = rows
        while order.count > 24 || ((try? JSONSerialization.data(withJSONObject: Array(pages.values)).count) ?? 0) > 2 * 1024 * 1024 {
            pages.removeValue(forKey: order.removeFirst())
        }
        return response["pending"] as? Bool == true
    }

    func figures(page: Int) -> [[String: Any]]? {
        pages[page]?.map { entry in
            var row = entry; row["attached"] = attached.contains { $0["id"] as? String == row["id"] as? String }; return row
        }
    }

    func setAttached(_ desired: Bool, id: String, page: Int) throws -> Bool {
        if let index = attached.firstIndex(where: { $0["id"] as? String == id && $0["page"] as? Int == page }) {
            if desired { return true }
            attached.remove(at: index); revision &+= 1; return false
        }
        if !desired { return false }
        guard var row = pages[page]?.first(where: { $0["id"] as? String == id }), attached.count < 128 else {
            throw Failure(message: "插图已失效，请重新打开图说明")
        }
        row["file_rel"] = "localbook:" + bookID
        row["token"] = UUID().uuidString
        attached.append(row); revision &+= 1; return true
    }

    /// The sender consumes the exact attachment tokens it saw. A delayed ack
    /// cannot remove a newly reattached image with the same geometry/ID.
    @discardableResult func consume(epoch expected: String, tokens: [String]) -> Bool {
        guard expected == epoch else { return false }
        let previous = attached.count, target = Set(tokens)
        attached.removeAll { target.contains($0["token"] as? String ?? "") }
        guard attached.count != previous else { return false }
        revision &+= 1; return true
    }

    static func ink(_ strokes: [[String: Any]], box: [Double]) -> [[String: Any]] {
        guard box.count == 4 else { return [] }
        return Array(strokes.lazy.compactMap { stroke -> [String: Any]? in
            let points = stroke["p"] as? [[Double]] ?? []
            guard points.contains(where: { $0.count >= 2 && $0[0] >= box[0] && $0[0] <= box[2] && $0[1] >= box[1] && $0[1] <= box[3] }) else { return nil }
            let rounded = points.filter { $0.count >= 2 && $0[0].isFinite && $0[1].isFinite && abs($0[0]) <= 16 && abs($0[1]) <= 16 }
                .map { [Double(fixed3($0[0]))!, Double(fixed3($0[1]))!] }
            var row: [String: Any] = ["p": rounded]
            for key in ["t", "c", "w"] { if let value = stroke[key] { row[key] = value } }
            return row
        }.prefix(30))
    }

    func projection(ink strokes: [String: [[String: Any]]]) -> [String: Any] {
        let rows = attached.map { entry -> [String: Any] in
            var row = entry
            let ink = Self.ink(strokes[String(entry["page"] as? Int ?? 0)] ?? [], box: entry["box"] as? [Double] ?? [])
            row["ink"] = ink; row["has_ink"] = !ink.isEmpty
            return row
        }
        return ["file": "localbook:" + bookID, "epoch": epoch, "revision": revision, "items": rows]
    }
}
