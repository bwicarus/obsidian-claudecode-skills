import Foundation

/// Local PDF sentence segmentation and interlinear translation layout. Keeps
/// the original punctuation, ruby exclusion and vertical-column rules.
enum ReaderNativePageTranslation {
    typealias Row = [String: Any]
    private static let spaces = "[\\u0009-\\u000d\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]+"
    private static func number(_ row: Row, _ key: String) -> Double { (row[key] as? NSNumber)?.doubleValue ?? 0 }
    private static func text(_ row: Row) -> String { row["c"] as? String ?? "" }
    private static func spacer(_ row: Row) -> Bool { (row["sp"] as? NSNumber)?.boolValue == true }
    private static func round(_ value: Double) -> Double { floor(value * 100 + 0.5) / 100 }
    private static func box(_ row: Row) -> [Double] { ["x0","y0","x1","y1"].map { round(number(row, $0)) } }
    private static func trim(_ value: String) -> String {
        value.replacingOccurrences(of: "^" + spaces + "|" + spaces + "$", with: "", options: .regularExpression)
    }
    private static func utf16Prefix(_ value: String, _ count: Int) -> String { String(decoding: value.utf16.prefix(count), as: UTF16.self) }

    static func sentenceRects(_ chars: [Row]) -> [[Double]] {
        var result: [[Double]] = [], current: [Double]?
        for c in chars {
            if spacer(c) && current == nil { continue }
            let values = ["x0","y0","x1","y1"].map { number(c, $0) }
            let height = max(0.1, values[3] - values[1])
            if var box = current, abs(values[1] - box[1]) <= height * 0.5 {
                box[2] = max(box[2], values[2]); box[1] = min(box[1], values[1]); box[3] = max(box[3], values[3]); current = box
                continue
            }
            if let current { result.append(current.map(round)) }
            current = spacer(c) ? nil : values
        }
        if let current { result.append(current.map(round)) }
        return result
    }

    static func sentences(_ chars: [Row]) -> [Row] {
        let heights = chars.filter { !spacer($0) && !trim(text($0)).isEmpty }.map { number($0,"y1") - number($0,"y0") }
            .filter { $0.isFinite && $0 > 0 }.sorted()
        let median = heights.isEmpty ? 0 : heights[heights.count / 2]
        var output: [Row] = [], current: [Row] = [], previous: Row?, pendingPeriod = false
        func flush() {
            defer { current = [] }
            let nonSpace = current.filter { !spacer($0) }
            guard !nonSpace.isEmpty else { return }
            let value = utf16Prefix(trim(current.map(text).joined()).replacingOccurrences(of: spaces, with: " ", options: .regularExpression), 500)
            guard value.utf16.count >= 4 else { return }
            let width = nonSpace.map { number($0,"x1") }.max()! - nonSpace.map { number($0,"x0") }.min()!
            let height = nonSpace.map { number($0,"y1") }.max()! - nonSpace.map { number($0,"y0") }.min()!
            guard height <= width * 1.6 else { return }
            output.append(["text":value,"rects":sentenceRects(current),"first_char":box(nonSpace.first!),"last_char":box(nonSpace.last!)])
        }
        for c in chars {
            let value = text(c)
            if !spacer(c), median > 0, number(c,"y1") - number(c,"y0") < median * 0.6,
               value.range(of: "^[\\u3041-\\u3093\\u30a1-\\u30f6\\u30fc]$", options: .regularExpression) != nil { continue }
            if pendingPeriod {
                let height = previous.map { max(1, number($0,"y1") - number($0,"y0")) } ?? 1
                let sameLine = previous.map { !spacer($0) && abs(number(c,"y0") - number($0,"y0")) < height * 0.5 } ?? false
                let continuation = sameLine && !spacer(c) && value.utf16.count == 1 && value.range(of: "^[0-9a-z]$", options: .regularExpression) != nil
                if !continuation { flush() }
                pendingPeriod = false
            }
            if let previous {
                if let before = previous["bk"] as? NSNumber, let after = c["bk"] as? NSNumber, before != after,
                   number(c,"y0") - number(previous,"y0") < -0.5 * max(0.1, number(previous,"y1") - number(previous,"y0")) { flush() }
                if !spacer(previous), !spacer(c),
                   number(c,"y0") - number(previous,"y0") > max(0.1, number(previous,"y1") - number(previous,"y0")) * 1.5 { flush() }
            }
            if "•▪▶◆●○◇".contains(value) { flush(); current.append(c); previous = c; continue }
            current.append(c)
            if !spacer(c) {
                if "!?。！？".contains(value) { flush() }
                else if value == "." { pendingPeriod = true }
            }
            previous = c
        }
        flush(); return output
    }

    static func mergeLines(_ rects: [[Double]]) -> [[Double]] {
        let raw = rects.filter { $0.count == 4 && $0.allSatisfy(\.isFinite) && $0[2] - $0[0] > 0.5 && $0[3] - $0[1] > 0.5 }
        let height = raw.map { $0[3] - $0[1] }.max() ?? 1
        func cy(_ r: [Double]) -> Double { (r[1] + r[3]) / 2 }
        let sorted = raw.enumerated().sorted {
            if cy($0.element) != cy($1.element) { return cy($0.element) < cy($1.element) }
            if $0.element[0] != $1.element[0] { return $0.element[0] < $1.element[0] }
            return $0.offset < $1.offset
        }.map(\.element)
        var lines: [[Double]] = []
        for r in sorted {
            if let last = lines.last, abs(cy(r) - cy(last)) <= height * 0.5 {
                lines[lines.count - 1] = [min(last[0],r[0]),min(last[1],r[1]),max(last[2],r[2]),max(last[3],r[3])]
            } else { lines.append(r) }
        }
        return lines.enumerated().sorted { $0.element[1] == $1.element[1] ? $0.offset < $1.offset : $0.element[1] < $1.element[1] }.map(\.element)
    }

    static func slices(_ sentences: [Row]) -> [Row] {
        var output: [Row] = []
        for sentence in sentences {
            let translated = trim(sentence["zh"] as? String ?? "")
            guard !translated.isEmpty else { continue }
            let raw = (sentence["rects"] as? [[Double]] ?? []).filter { $0.count == 4 && $0[2] - $0[0] > 1 && $0[3] - $0[1] > 1 }
            let lines = mergeLines(raw), scalars = Array(translated.unicodeScalars)
            var index = 0
            for (i, line) in lines.enumerated() {
                let width = line[2] - line[0], height = line[3] - line[1]
                let annotationSize = max(7, height * 0.4)
                let capacity = max(1, Int(min(Double(Int.max / 2), floor(width / annotationSize))))
                let count = i == lines.count - 1 ? scalars.count - index : max(0, min(capacity, scalars.count - index))
                let slice = String(String.UnicodeScalarView(scalars[index..<index+count])); index += count
                guard !slice.isEmpty else { continue }
                let size = max(7, min(annotationSize, width / Double(slice.utf16.count)))
                output.append(["x":line[0],"y":line[1] - size * 0.34,"w":width,"fontSize":size,"text":slice])
            }
        }
        return output
    }

    static func translated(_ sentences: [Row], status: Int, data: Data) throws -> [Row] {
        guard (200..<300).contains(status), let raw = try JSONSerialization.jsonObject(with:data) as? Row,
              let translations = raw["translations"] as? [Any], translations.count == sentences.count else {
            throw ReaderNativeLookupRequest.Failure(message: "服务器页面翻译响应无效")
        }
        return zip(sentences,translations).map { sentence, translated in
            var value = sentence; value["zh"] = utf16Prefix(translated as? String ?? "",8000); return value
        }
    }
}
