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

    static func localMarks(_ chars: [Row], state: Index) -> [Row] { localMarkSpans(chars, state: state).map(\.row) }

    /// 同 localMarks，另带每条下划线在字符层里的起点 —— 生词句按它数「句中有几个下划线词」。
    /// （localMarks 的输出字段与网页版逐字比对，不能往里加私有字段。）
    static func localMarkSpans(_ chars: [Row], state: Index) -> [(row: Row, lo: Int)] {
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
        }.map { (row: $0.element.row, lo: $0.element.lo) }
    }

    /// 生词句（整句预翻译的框）：原生本地算（2026-09-26 用户：「为何要用网页代码不能用原生么」）。
    /// 真机走原生叠加层，它以前只从服务端取句子，而服务端对本机书一律拒（BW_PI_GATEWAY_REMOTE_BOOK）
    /// → 本机书上一个句子框都没有。规则逐条对应服务端 _build_unmastered_sentences：
    /// 句中下划线词（未掌握）≥3 且总词数 ≥10；断句 = 句末标点 / 「.」后非延续 / 换块 / 段距 >1.5 行高 /
    /// 新行是列表项或上一行是本块短行 / 列表符；排除竖排、页眉页脚、大字号、整句加粗、文本 <12 字。
    /// spans = localMarkSpans 的结果；visibleLemmas = 最终真正画出来的下划线（计数集 = 下划线集）。
    static func localSentences(_ chars: [Row], spans: [(row: Row, lo: Int)], visibleLemmas: Set<String>, pageHeight: Double) -> [Row] {
        func num(_ c: Row, _ key: String) -> Double { (c[key] as? NSNumber)?.doubleValue ?? 0 }
        func block(_ c: Row) -> Double? { (c["bk"] as? NSNumber)?.doubleValue }
        var markAt: [Int: String] = [:]
        for span in spans {
            guard let lemma = span.row["lemma"] as? String, visibleLemmas.contains(lemma), span.lo < chars.count else { continue }
            markAt[span.lo] = lemma
        }
        var heights: [Double] = [], edge: [Double: Double] = [:], left: [Double: Double] = [:]
        var rightEdge = 0.0, leftEdge = Double.infinity
        for c in chars where !spacer(c) && num(c, "x1") > num(c, "x0") {
            heights.append(num(c, "y1") - num(c, "y0"))
            rightEdge = max(rightEdge, num(c, "x1")); leftEdge = min(leftEdge, num(c, "x0"))
            if let bk = block(c) { edge[bk] = max(edge[bk] ?? -.infinity, num(c, "x1")); left[bk] = min(left[bk] ?? .infinity, num(c, "x0")) }
        }
        heights.sort()
        let medianH = heights.isEmpty ? 0 : heights[heights.count / 2]
        let textWidth = max(1, rightEdge - (leftEdge.isFinite ? leftEdge : 0))
        func lineIsShort(_ prev: Row) -> Bool {
            let bk = block(prev)
            let e = bk.flatMap { edge[$0] } ?? rightEdge
            let width = max(1, e - (bk.flatMap { left[$0] } ?? rightEdge - textWidth))
            return e - num(prev, "x1") > width * 0.30
        }
        func isListHead(_ index: Int) -> Bool {
            let head = chars[index..<min(index + 12, chars.count)].map(text).joined()
            return head.range(of: "^\\s*(\\d{1,3}([.)]|\\.\\d)|[A-Za-z][.)]|[ivxIVX]{1,4}[.)])", options: .regularExpression) != nil
        }
        var output: [Row] = [], start = -1
        func flush(_ end: Int) {
            defer { start = -1 }
            guard start >= 0, end >= start else { return }
            var lemmas = Set<String>(), words: [Double: String] = [:], body: [Row] = [], sentence = ""
            for k in start...end {
                let c = chars[k]
                sentence += text(c)
                if let lemma = markAt[k] { lemmas.insert(lemma) }
                if spacer(c) { continue }
                body.append(c)
                let t = text(c)
                if let w = token(c), w >= 0, t.range(of: "[A-Za-z\\u3040-\\u30ff\\u3400-\\u9fff]", options: .regularExpression) != nil {
                    words[w, default: ""] += t
                }
            }
            let total = words.values.filter { japanese($0) || $0.count >= 2 }.count
            let cleaned = String(sentence.replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
                .trimmingCharacters(in: .whitespacesAndNewlines).prefix(500))
            guard lemmas.count >= 3, total >= 10, cleaned.count >= 12, !body.isEmpty else { return }
            let bx0 = body.map { num($0, "x0") }.min()!, by0 = body.map { num($0, "y0") }.min()!
            let bx1 = body.map { num($0, "x1") }.max()!, by1 = body.map { num($0, "y1") }.max()!
            if by1 - by0 > (bx1 - bx0) * 1.6 { return }                                        // 竖排
            if pageHeight > 0 && (by0 > pageHeight * 0.90 || by1 < pageHeight * 0.06) { return } // 页眉页脚
            let avgH = body.reduce(0) { $0 + num($1, "y1") - num($1, "y0") } / Double(body.count)
            if medianH > 0 && avgH > medianH * 1.4 { return }                                   // 大字号标题
            if Double(body.filter { ($0["b"] as? NSNumber)?.boolValue == true }.count) / Double(body.count) > 0.9 { return } // 整句加粗
            func box(_ c: Row) -> [Double] { ["x0", "y0", "x1", "y1"].map { (num(c, $0) * 100).rounded() / 100 } }
            output.append(["text": cleaned, "rects": rects(body), "lemmas": lemmas.sorted(), "count": lemmas.count,
                           "total_words": total, "firstChar": box(body.first!), "lastChar": box(body.last!), "local": true])
        }
        var prevNs: Row?, pendingPeriod = false
        for i in chars.indices {
            if output.count >= 200 || Task.isCancelled { break }
            let ch = chars[i], c = text(ch)
            if pendingPeriod {
                let prev = chars[i - 1]
                let sameLine = !spacer(prev) && abs(num(ch, "y0") - num(prev, "y0")) < max(1, (num(prev, "y1") - num(prev, "y0")) * 0.5)
                let continuation = sameLine && !spacer(ch) && c.count == 1 && c.range(of: "^[0-9a-z]$", options: .regularExpression) != nil
                if !continuation { flush(i - 1) }
                pendingPeriod = false
            }
            if i > 0, let a = block(chars[i - 1]), let b = block(ch), a != b { flush(i - 1) }
            if let prev = prevNs, !spacer(ch) {
                let ph = max(0.1, num(prev, "y1") - num(prev, "y0")), gap = num(ch, "y0") - num(prev, "y0")
                if gap > ph * 1.5 { flush(i - 1) }
                else if abs(gap) > ph * 0.5 && (isListHead(i) || lineIsShort(prev)) { flush(i - 1) }
                // 同一行里隔着一大段空白（表格的不同格、一行排几个的列表项）→ 不是同一句。
                // 服务端靠 PyMuPDF 的块号（每格一块）断开；本机抽取没有这种块，按几何补上
                //（2026-09-26 用户：表格里的病名被连成一句加了框）。
                else if abs(gap) <= ph * 0.5 && num(ch, "x0") - num(prev, "x1") > ph * 1.2 { flush(i - 1) }
            }
            if !c.isEmpty && "•▪▶◆●○◇".contains(c) { flush(i - 1); start = i; prevNs = ch; continue }
            if start < 0 && !spacer(ch) { start = i }
            if spacer(ch) { continue }
            prevNs = ch
            if !c.isEmpty && "!?。！？".contains(c) { flush(i); continue }
            if c == "." { pendingPeriod = true }
        }
        flush(chars.count - 1)
        return output
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
