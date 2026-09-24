import Foundation

/// The existing local vocabulary projection, without a DOM or a JavaScript
/// runtime. Word boundaries refer to original character indexes throughout.
struct ReaderNativeVocabularyOverlay {
    typealias Row = [String: Any]
    struct Index {
        let records: [Row]
        private let insertionOrder: [Row]
        private var direct: [String: Row] = [:]
        private var aliases: [String: Row] = [:]

        init(_ values: [Row]) {
            insertionOrder = values.compactMap { try? ReaderNativeVocabularyState.normalized($0) }
            records = insertionOrder.sorted { ($0["id"] as! String).compare($1["id"] as! String, locale: Locale(identifier: "en_US")) == .orderedAscending }
            for row in insertionOrder {
                let prefix = Self.prefix(row)
                direct[prefix + (row["key"] as! String)] = row
                for key in [row["key"] as! String] + (row["aliases"] as! [String]) {
                    if aliases[prefix + key] == nil { aliases[prefix + key] = row }
                }
            }
        }
        private static func prefix(_ row: Row) -> String {
            ["property", "kind", "language"].map { row[$0] as? String ?? "" }.joined(separator: "\0") + "\0"
        }
        func lookup(_ input: Row, property: String) -> Row? {
            guard let spec = try? ReaderNativeVocabularyState.normalized(input, property: property) else { return nil }
            let key = spec["key"] as! String
            let languages = spec["language"] as? String == "und" ? ["und"] : [spec["language"] as! String, "und"]
            let prefixes = languages.map { Self.prefix(["property": property, "kind": spec["kind"]!, "language": $0]) }
            for prefix in prefixes { if let row = direct[prefix + key] { return row } }
            let keys = [key] + (spec["aliases"] as! [String])
            // The repository chooses the first record in snapshot order, not
            // the first query alias. Preserve that tie-break for overlapping aliases.
            for prefix in prefixes {
                let candidates = keys.compactMap { aliases[prefix + $0] }
                let ids = Set(candidates.compactMap { $0["id"] as? String })
                if ids.count == 1 { return candidates.first }
                if !ids.isEmpty, let row = insertionOrder.first(where: { ids.contains($0["id"] as! String) }) { return row }
            }
            return nil
        }
        func enabled(_ input: Row, _ property: String) -> Bool { lookup(input, property: property)?["enabled"] as? Bool == true }
    }

    private static let spaces = "[\\u0009-\\u000d\\u0020\\u00a0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000\\ufeff]+"
    private static func compact(_ value: String) -> String { value.replacingOccurrences(of: spaces, with: "", options: .regularExpression) }
    private static func japanese(_ text: String) -> Bool { text.range(of: "[\\u3040-\\u30ff\\u3400-\\u9fff]", options: .regularExpression) != nil }
    private static func kanji(_ text: String) -> Bool { text.range(of: "[\\u3400-\\u9fff]", options: .regularExpression) != nil }
    private static func text(_ c: Row) -> String { c["c"] as? String ?? "" }
    private static func spacer(_ c: Row) -> Bool { (c["sp"] as? NSNumber)?.boolValue == true }
    private static func token(_ c: Row) -> Double? { (c["w"] as? NSNumber)?.doubleValue }
    private static func spec(_ key: String, kind: String = "word", language: String? = nil) -> Row {
        ["kind": kind, "language": language ?? (japanese(key) ? "ja" : "en"), "lemma": key, "word": key]
    }
    private static func rects(_ chars: [Row]) -> [[Double]] {
        var output: [[Double]] = [], current: [Double]?
        func rounded(_ box: [Double]) -> [Double] { box.map { floor($0 * 100 + 0.5) / 100 } }
        for c in chars where !spacer(c) {
            let values = ["x0", "y0", "x1", "y1"].compactMap { (c[$0] as? NSNumber)?.doubleValue }
            guard values.count == 4, values.allSatisfy(\.isFinite) else { continue }
            if var box = current, abs(values[1] - box[1]) <= (values[3] - values[1]) * 0.5 {
                box[2] = max(box[2], values[2]); box[1] = min(box[1], values[1]); box[3] = max(box[3], values[3]); current = box
            } else { if let current { output.append(rounded(current)) }; current = values }
        }
        if let current { output.append(rounded(current)) }
        return output
    }
    private struct Mark {
        var row: Row
        let lo: Int, hi: Int
        var slug: String { row["label_slug"] as! String }
    }
    private static func mark(_ word: String, key: String, slug: String, rects: [[Double]], ja: Bool, lo: Int, hi: Int) -> Mark {
        Mark(row: ["word": word, "lemma": key, "mastery": slug == "seen" ? 0.4 : 0.1,
                   "label_slug": slug, "rects": rects, "jp": ja, "local": true], lo: lo, hi: hi)
    }
    private static func insideLargerToken(_ chars: [Row], lo: Int, hi: Int) -> Bool {
        guard let a = token(chars[lo]), let b = token(chars[hi]), a >= 0, b >= 0 else { return false }
        var before = lo - 1, after = hi + 1
        while before >= 0 && spacer(chars[before]) { before -= 1 }
        while after < chars.count && spacer(chars[after]) { after += 1 }
        return (before >= 0 && token(chars[before]) == a) || (after < chars.count && token(chars[after]) == b)
    }

    static func localMarks(_ chars: [Row], state: Index) -> [Row] {
        var marks: [Mark] = [], masteredRanges: [(Int, Int)] = []
        var i = 0
        while i < chars.count {
            if Task.isCancelled { return [] }
            guard !spacer(chars[i]), let wid = token(chars[i]), wid >= 0 else { i += 1; continue }
            let lo = i
            var end = i
            while end < chars.count && token(chars[end]) == wid { end += 1 }
            var tokens = Array(chars[i..<end]).filter { !spacer($0) }
            i = end
            var surface = tokens.map(text).joined()
            if surface.utf16.count == 1, kanji(surface), end < chars.count, let next = token(chars[end]), next >= 0 {
                var secondEnd = end
                while secondEnd < chars.count && token(chars[secondEnd]) == next { secondEnd += 1 }
                let more = Array(chars[end..<secondEnd]).filter { !spacer($0) }
                let key = compact(surface + more.map(text).joined())
                if !more.isEmpty && (state.enabled(spec(key), "mastered") || state.enabled(spec(key), "lookup")
                    || state.enabled(spec(key, kind: "phrase"), "favorite")) {
                    tokens += more; surface = tokens.map(text).joined(); i = secondEnd
                }
            }
            let key = compact(surface), ja = japanese(key)
            guard !key.isEmpty, key.utf16.count <= 64, !ja || key.utf16.count >= 2 || kanji(key) else { continue }
            let wordSpec = spec(key), phraseSpec = spec(key, kind: "phrase")
            if state.enabled(wordSpec, "mastered") || state.enabled(phraseSpec, "mastered") { masteredRanges.append((lo, i - 1)); continue }
            let slug = state.enabled(phraseSpec, "favorite") ? "seen" : state.enabled(wordSpec, "lookup") ? "new" : ""
            guard !slug.isEmpty else { continue }
            let boxes = rects(tokens)
            if !boxes.isEmpty { marks.append(mark(surface, key: key, slug: slug, rects: boxes, ja: ja, lo: lo, hi: i - 1)) }
            if marks.count >= 800 { break }
        }
        var wanted: [(key: String, slug: String, language: String)] = [], seen = Set<String>()
        for row in state.records where row["enabled"] as? Bool == true {
            if Task.isCancelled { return [] }
            let property = row["property"] as! String
            if property == "lookup" && row["kind"] as? String == "phrase" { continue }
            let slug = property == "mastered" ? "mastered" : property == "favorite" ? "seen" : "new"
            for raw in [row["key"] as! String] + (row["aliases"] as! [String]) {
                let key = compact(raw)
                if (2...64).contains(key.utf16.count), seen.insert(key + "|" + slug).inserted {
                    wanted.append((key, slug, row["language"] as! String))
                }
            }
        }
        if !wanted.isEmpty && wanted.count <= 4000 {
            var units: [UInt16] = [], source: [Int] = []
            for (idx, c) in chars.enumerated() where !spacer(c) {
                let value = Array(compact(text(c)).lowercased().utf16)
                units += value; source += Array(repeating: idx, count: value.count)
            }
            let joined = String(decoding: units, as: UTF16.self) as NSString
            func start(_ boxes: [[Double]]) -> String? { boxes.first.map { "\($0[0]),\($0[1])" } }
            var taken = Set(marks.compactMap { start($0.row["rects"] as! [[Double]]) })
            for wanted in wanted {
                if Task.isCancelled { return [] }
                var offset = 0, count = 0
                while offset < joined.length && count < 200 && marks.count < 800 {
                    let range = joined.range(of: wanted.key, options: [], range: NSRange(location: offset, length: joined.length - offset))
                    guard range.location != NSNotFound else { break }
                    offset = range.location + 1; count += 1
                    let lo = source[range.location], hi = source[NSMaxRange(range) - 1]
                    if wanted.slug == "mastered" { masteredRanges.append((lo, hi)); continue }
                    if insideLargerToken(chars, lo: lo, hi: hi) { continue }
                    let language = wanted.language == "en" ? "en" : "ja"
                    if state.enabled(spec(wanted.key, language: language), "mastered")
                        || state.enabled(spec(wanted.key, kind: "phrase", language: language), "mastered") { continue }
                    let boxes = rects(Array(chars[lo...hi]))
                    if let key = start(boxes), taken.insert(key).inserted {
                        marks.append(mark(wanted.key, key: wanted.key, slug: wanted.slug, rects: boxes, ja: wanted.language != "en", lo: lo, hi: hi))
                    }
                }
            }
        }
        return marks.enumerated().filter { index, mark in
            !marks.enumerated().contains { otherIndex, other in
                guard otherIndex != index, other.lo <= mark.lo, mark.hi <= other.hi else { return false }
                return other.hi - other.lo > mark.hi - mark.lo
                    || (other.lo == mark.lo && other.hi == mark.hi &&
                        ((other.slug == "seen" && mark.slug != "seen") || (other.slug == mark.slug && otherIndex < index)))
            } && !masteredRanges.contains { $0.0 <= mark.lo && mark.hi <= $0.1 }
        }.map { $0.element.row }
    }

    static func merge(_ local: [Row], _ remote: [Row]) -> [Row] {
        var output = local
        for row in remote {
            let rects = row["rects"] as? [[Double]] ?? []
            let overlaps = output.contains { existing in
                (existing["rects"] as? [[Double]] ?? []).contains { a in
                    rects.contains { b in
                        guard a.count == 4, b.count == 4 else { return false }
                        let vertical = min(a[3], b[3]) - max(a[1], b[1])
                        let horizontal = min(a[2], b[2]) - max(a[0], b[0])
                        return vertical >= max(1, min(a[3] - a[1], b[3] - b[1])) * 0.5
                            && horizontal / max(1, min(a[2] - a[0], b[2] - b[0])) >= 0.5
                    }
                }
            }
            if !overlaps { output.append(row) }
        }
        return output
    }

    static func visible(_ marks: [Row], state: Index, overrides: [String: Bool] = [:], legacyMastered: Set<String> = []) -> [Row] {
        marks.filter { row in
            let word = (row["word"] as? String ?? row["surface"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
            let lemma = (row["lemma"] as? String ?? word).trimmingCharacters(in: .whitespacesAndNewlines)
            let language = row["language"] as? String ?? row["lang"] as? String ?? (row["jp"] as? Bool == true ? "ja" : "en")
            let input: Row = ["kind": "word", "language": language, "lemma": lemma.isEmpty ? word : lemma,
                              "word": word.isEmpty ? lemma : word, "surface": word.isEmpty ? lemma : word, "forms": row["forms"] ?? []]
            if state.enabled(input, "mastered") { return false }
            var keys: [String] = []
            for raw in [row["lemma"], row["word"], row["surface"]].compactMap({ $0 }) + (row["forms"] as? [Any] ?? []) {
                if let key = try? ReaderNativeVocabularyState.normalizeKey(raw), !keys.contains(key) { keys.append(key) }
            }
            for key in keys { if let flag = overrides[key] { return !flag } }
            if keys.contains(where: legacyMastered.contains) { return false }
            return row["label_slug"] as? String != "mastered"
        }
    }
}
