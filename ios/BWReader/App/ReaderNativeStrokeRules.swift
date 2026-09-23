import Foundation
import CoreFoundation

/// Pencil operations on the existing normalized stroke format. No canvas or
/// page element is required; callers commit the returned surfaces atomically.
enum ReaderNativeStrokeRules {
    enum StrokeError: LocalizedError {
        case invalid(String)
        var errorDescription: String? { switch self { case .invalid(let why): return "笔迹参数无效：" + why } }
    }
    struct Outcome {
        var surfaces: [String: [[String: Any]]]
        var before: [String: [[String: Any]]]
        var written: Int
        var removed: Int
    }

    static func apply(action: String, input: [String: Any], surfaces: [String: [[String: Any]]], now: Int64) throws -> Outcome {
        guard ["commit", "erase", "createRegion"].contains(action),
              let opID = input["opId"] as? String, validID(opID),
              let segments = input["segments"] as? [[String: Any]], !segments.isEmpty, segments.count <= 64,
              JSONSerialization.isValidJSONObject(input) else { throw StrokeError.invalid("操作或分段") }
        let region = action == "createRegion", minimum = action == "erase" ? 1 : (region ? 3 : 2)
        let regionID = input["regionId"] as? String ?? ""
        if region && !validID(regionID) { throw StrokeError.invalid("圈选编号") }
        var result = Outcome(surfaces: surfaces, before: [:], written: 0, removed: 0)
        for (index, segment) in segments.enumerated() {
            guard let surface = segment["surfaceId"] as? String,
                  surface.range(of: "^page:[1-9][0-9]{0,7}$", options: .regularExpression) != nil,
                  let raw = segment["points"] as? [[Any]], raw.count >= minimum, raw.count <= (region ? 512 : 4096) else {
                throw StrokeError.invalid("页码或点数")
            }
            let points: [[Double]] = try raw.map { point in
                guard point.count >= 2, let x = numeric(point[0]), let y = numeric(point[1]) else { throw StrokeError.invalid("坐标") }
                return [max(0, min(1, x)), max(0, min(1, y))]
            }
            let key = String(surface.dropFirst(5))
            var strokes = result.surfaces[key] ?? []
            if result.before[key] == nil { result.before[key] = strokes }
            if action == "erase" {
                for point in points {
                    let count = strokes.count
                    strokes.removeAll { hit($0, point: point, threshold: 0.018) }
                    if strokes.count != count { result.removed += 1 }
                }
            } else {
                let rawColor = segment["color"] as? String ?? ""
                let color = rawColor.range(of: "^#[a-fA-F0-9]{6}$", options: .regularExpression) != nil ? rawColor : "#ff3b30"
                let rawWidth = numeric(segment["width"])
                let width = max(1, min(20, rawWidth == nil || rawWidth == 0 ? (region ? 3 : 4) : rawWidth!))
                var stroke: [String: Any] = ["t": region ? "region" : "pen", "c": color, "w": width, "p": points]
                if region {
                    stroke["id"] = segments.count > 1 ? String(regionID.prefix(86)) + "-" + String(index) : regionID
                    let fallback = numeric(input["createdAtEpochMs"]).flatMap { $0 > 0 ? $0 : nil } ?? Double(now)
                    stroke["createdAtEpochMs"] = numeric(segment["createdAtEpochMs"]).flatMap { $0 > 0 ? $0 : nil } ?? fallback
                } else if let widths = segment["widths"] as? [Any], widths.count == points.count {
                    stroke["ww"] = widths.map { raw in
                        let value = numeric(raw)
                        return max(0.3, min(48, value == nil || value == 0 ? width : value!))
                    }
                }
                strokes.append(stroke); result.written += 1
            }
            ensureRegionOrdinals(&strokes)
            guard strokes.count <= 5000,
                  try JSONSerialization.data(withJSONObject: strokes, options: [.sortedKeys, .withoutEscapingSlashes]).count <= 16 * 1024 * 1024 else {
                throw StrokeError.invalid("笔画容量")
            }
            if strokes.isEmpty { result.surfaces.removeValue(forKey:key) }
            else { result.surfaces[key] = strokes }
        }
        return result
    }

    static func validID(_ id: String) -> Bool { id.range(of: "^[A-Za-z0-9_-]{1,96}$", options: .regularExpression) != nil }
    static func numeric(_ value: Any?) -> Double? {
        if value is NSNull { return 0 }
        if let n = value as? NSNumber { return n.doubleValue.isFinite ? n.doubleValue : nil }
        if let s = value as? String {
            let text = s.trimmingCharacters(in: .whitespacesAndNewlines)
            if text.isEmpty { return 0 }
            if let n = Double(text), n.isFinite { return n }
        }
        return nil
    }
    static func hit(_ stroke: [String: Any], point: [Double], threshold: Double) -> Bool {
        let raw = stroke["p"] ?? stroke["pts"]
        guard let data = raw as? [[Any]], point.count == 2 else { return false }
        let type = stroke["t"] as? String ?? "pen"
        let points = (type == "region" ? Array(data.prefix(512)) : data).compactMap { p -> [Double]? in
            guard p.count >= 2, let x = numeric(p[0]), let y = numeric(p[1]) else { return nil }
            return [x,y]
        }
        guard !points.isEmpty else { return false }
        if type == "region", points.count >= 3 {
            var inside = false
            for i in points.indices {
                let a = points[i], b = points[(i + points.count - 1) % points.count]
                if (a[1] > point[1]) != (b[1] > point[1]),
                   point[0] < (b[0] - a[0]) * (point[1] - a[1]) / (b[1] - a[1]) + a[0] { inside.toggle() }
            }
            if inside { return true }
            return points.indices.contains { distance(point, points[$0], points[($0+1) % points.count]) < threshold }
        }
        if type == "rect", points.count >= 2 {
            let x0 = min(points[0][0],points[1][0]), x1 = max(points[0][0],points[1][0])
            let y0 = min(points[0][1],points[1][1]), y1 = max(points[0][1],points[1][1])
            return ((abs(point[0]-x0) < threshold || abs(point[0]-x1) < threshold) && point[1] > y0-threshold && point[1] < y1+threshold)
                || ((abs(point[1]-y0) < threshold || abs(point[1]-y1) < threshold) && point[0] > x0-threshold && point[0] < x1+threshold)
        }
        if points.count == 1 { return hypot(point[0]-points[0][0], point[1]-points[0][1]) < threshold }
        return (0..<points.count-1).contains { distance(point, points[$0], points[$0+1]) < threshold }
    }
    private static func distance(_ p: [Double], _ a: [Double], _ b: [Double]) -> Double {
        let dx = b[0]-a[0], dy = b[1]-a[1], length = dx*dx+dy*dy
        if length == 0 { return hypot(p[0]-a[0], p[1]-a[1]) }
        let t = max(0, min(1, ((p[0]-a[0])*dx+(p[1]-a[1])*dy)/length))
        return hypot(p[0]-(a[0]+t*dx), p[1]-(a[1]+t*dy))
    }
    static func ensureRegionOrdinals(_ strokes: inout [[String:Any]]) {
        let indexes = strokes.indices.filter { strokes[$0]["t"] as? String == "region" }.sorted {
            let a = max(0,numeric(strokes[$0]["createdAtEpochMs"]) ?? 0), b = max(0,numeric(strokes[$1]["createdAtEpochMs"]) ?? 0)
            if a != b { return a < b }
            let ai = strokes[$0]["id"] as? String ?? "", bi = strokes[$1]["id"] as? String ?? ""
            return ai == bi ? $0 < $1 : ai.utf16.lexicographicallyPrecedes(bi.utf16)
        }
        var used = Set<Int64>(), missing: [Int] = [], highest: Int64 = 0
        for i in indexes {
            if let n = numeric(strokes[i]["ordinal"]), n > 0, n <= 9_007_199_254_740_991, n.rounded() == n, used.insert(Int64(n)).inserted {
                highest = max(highest, Int64(n))
            } else { missing.append(i) }
        }
        for i in missing { highest += 1; strokes[i]["ordinal"] = highest }
    }
}
