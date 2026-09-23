import Foundation

/// PDF text geometry in source-index space. This is a native data model, not a
/// second renderer. It preserves the reader's block/cell/word rules and is
/// exercised against fixtures produced by the existing web selection core.
final class ReaderNativePDFTextGeometry {
    enum GeometryError: Error { case invalidPage, invalidSelection }
    struct Result {
        let indexes: [Int]
        let text: String
        let sentence: String
        let rects: [CGRect] // PDF display points, top-left origin
        var quality: String? = nil
        var matches = 1
    }
    private struct Glyph {
        let index: Int, text: String, word: Int, block: Int, line: Int?
        let space: Bool, vertical: Bool?
        let rect: CGRect
        var blockID: Int { block >= 0 ? block : (word < 0 ? -1 : word / 1_000_000) }
        var lineKey: String? { line.map { "\(blockID):\($0)" } }
    }
    private enum Axis { case horizontal, vertical, ambiguous }
    private struct Shape {
        var rect = CGRect.null
        var widths: [CGFloat] = [], heights: [CGFloat] = []
        var vertical = 0, horizontal = 0, hasLines = false
        var axis = Axis.ambiguous
        var width: CGFloat { max(1, widths.sorted().dropFirst(widths.count / 2).first ?? rect.width) }
        var height: CGFloat { max(1, heights.sorted().dropFirst(heights.count / 2).first ?? rect.height) }
    }
    let width: Double, height: Double
    private let chars: [Glyph], positions: [Int: Int], blockFilter: Bool
    private let regions: [Int: String], numberedRegions: [Int: Set<Int>]
    private let blocks: [Int: Shape], lines: [String: Shape]

    init(page: [String: Any]) throws {
        guard let width = (page["page_w"] as? NSNumber)?.doubleValue,
              let height = (page["page_h"] as? NSNumber)?.doubleValue,
              width.isFinite, height.isFinite, width > 0, height > 0,
              let raw = page["chars"] as? [[String: Any]], raw.count <= 100_000 else { throw GeometryError.invalidPage }
        self.width = width; self.height = height
        let source = (page["source"] as? String ?? "").lowercased()
        let revision = (page["engine_revision"] as? String ?? "").lowercased()
        let geometry = (page["character_geometry"] as? String ?? "").lowercased()
        if source == "embedded" || revision.hasPrefix("embedded-") || revision.contains("pdfkit-embedded-text/") {
            blockFilter = false
        } else if ["pi", "pc"].contains(source) {
            if revision.hasPrefix(source + "-manga/") { blockFilter = true }
            else if revision.hasPrefix(source + "-vision/") || geometry == "exact" { blockFilter = false }
            else { blockFilter = true }
        } else { blockFilter = true }
        var mapped = try raw.enumerated().map { index, value -> Glyph in
            guard let text = value["c"] as? String,
                  let x0 = value["x0"] as? Double, let y0 = value["y0"] as? Double,
                  let x1 = value["x1"] as? Double, let y1 = value["y1"] as? Double,
                  [x0, y0, x1, y1].allSatisfy(\.isFinite), x1 >= x0, y1 >= y0 else { throw GeometryError.invalidPage }
            let line = (value["line"] as? NSNumber)?.intValue
            return Glyph(index: index, text: text, word: (value["w"] as? Int) ?? -1,
                         block: (value["bk"] as? Int) ?? -1, line: line.flatMap { $0 >= 0 ? $0 : nil },
                         space: (value["sp"] as? NSNumber)?.boolValue ?? false,
                         vertical: value["vertical"] as? Bool,
                         rect: CGRect(x: x0, y: y0, width: x1-x0, height: y1-y0))
        }
        mapped.sort {
            let ref = max($0.rect.height, $1.rect.height, 1)
            let difference = $0.rect.maxY - $1.rect.maxY
            if abs(difference) > ref * 0.8 { return difference < 0 }
            if ($0.word != -1 && $0.word == $1.word) || abs($0.rect.minX - $1.rect.minX) < ref * 0.3 { return $0.index < $1.index }
            return $0.rect.minX < $1.rect.minX
        }
        let lineShapes = Self.lineShapes(mapped)
        var directions: [Int: (vertical: Int, horizontal: Int)] = [:]
        var countedLines = Set<String>()
        for char in mapped where char.block >= 0 {
            guard let key = char.lineKey, countedLines.insert(key).inserted, let shape = lineShapes[key] else { continue }
            var votes = directions[char.block] ?? (0, 0)
            if shape.axis == .vertical { votes.vertical += 1 }
            if shape.axis == .horizontal { votes.horizontal += 1 }
            directions[char.block] = votes
        }
        for (block, votes) in directions where votes.vertical > 0 && votes.horizontal == 0 {
            let slots = mapped.indices.filter { mapped[$0].block == block }
            let members = slots.map { mapped[$0] }.sorted {
                if $0.line != $1.line { return ($0.line ?? Int.max) < ($1.line ?? Int.max) }
                if abs($0.rect.minY - $1.rect.minY) > max($0.rect.height, $1.rect.height) * 0.25 { return $0.rect.minY < $1.rect.minY }
                return $0.index < $1.index
            }
            for (offset, slot) in slots.enumerated() { mapped[slot] = members[offset] }
        }
        chars = mapped
        positions = Dictionary(uniqueKeysWithValues: mapped.enumerated().map { ($0.element.index, $0.offset) })
        lines = lineShapes; blocks = Self.blockShapes(mapped, lines: lineShapes)
        var byIndex: [Int: String] = [:], byNumber: [Int: Set<Int>] = [:]
        let layout = page["layout"] as? [String: Any]
        for region in layout?["regions"] as? [[String: Any]] ?? [] {
            let key = region["kind"] as? String == "table-cell"
                ? "t\(region["tableId"] ?? ""):\(region["row"] ?? ""):\(region["column"] ?? "")" : "page"
            for range in region["ranges"] as? [[Int]] ?? [] where range.count >= 2 {
                let start = max(0, min(range[0], range[1])), end = min(mapped.count - 1, max(range[0], range[1]))
                if start > end { continue }
                for index in start...end {
                    byIndex[index] = key
                    if let number = region["order"] as? Int { byNumber[number + 1, default: []].insert(index) }
                }
            }
        }
        regions = byIndex; numberedRegions = byNumber
    }

    func hit(x: Double, y: Double, anchor: Int?, exactOnly: Bool) -> Int? {
        guard x.isFinite, y.isFinite else { return nil }
        if let char = chars.first(where: { !$0.space && x >= $0.rect.minX && x <= $0.rect.maxX && y >= $0.rect.minY && y <= $0.rect.maxY }) { return char.index }
        if exactOnly { return nil }
        var allowed: Set<Int>?
        if blockFilter, let anchor, let position = positions[anchor], chars[position].blockID >= 0 {
            allowed = connected(blocks, seeds: [chars[position].blockID])
        }
        let candidates = chars.filter { !$0.space && (allowed?.contains($0.blockID) ?? true) }
        func nearest(_ values: [Glyph], distance: (Glyph) -> Double) -> (Glyph, Double)? {
            var result: (Glyph, Double)?
            for char in values {
                let d = distance(char)
                if result == nil || d < result!.1 { result = (char, d) }
            }
            return result
        }
        if let found = nearest(candidates.filter { y >= $0.rect.minY - 2 && y <= $0.rect.maxY + 2 }, distance: { max(0, $0.rect.minX - x, x - $0.rect.maxX) }) { return found.0.index }
        if let found = nearest(candidates.filter { x >= $0.rect.minX - 2 && x <= $0.rect.maxX + 2 }, distance: { max(0, $0.rect.minY - y, y - $0.rect.maxY) }) { return found.0.index }
        guard let found = nearest(candidates, distance: { abs(x - $0.rect.midX) + abs(y - $0.rect.midY) * 3 }) else { return nil }
        if allowed != nil && found.1 > max(1, found.0.rect.width, found.0.rect.height) * 1.5 { return nil }
        return found.0.index
    }

    func range(from: Int, to: Int) throws -> Result? {
        guard let a = positions[from], let b = positions[to] else { throw GeometryError.invalidSelection }
        return selected(wordEdge(min(a, b), step: -1), wordEdge(max(a, b), step: 1))
    }
    func exact(_ indexes: [Int]) throws -> Result? {
        let values = try mappedIndexes(indexes)
        guard let first = values.first, let last = values.last else { return nil }
        return selected(first, last, keep: Set(values))
    }
    func sentence(_ indexes: [Int]) throws -> Result? {
        let values = try mappedIndexes(indexes)
        guard let first = values.first, let last = values.last else { throw GeometryError.invalidSelection }
        let range = sentenceRange(first, last)
        return selected(range.0, range.1)
    }
    private func mappedIndexes(_ indexes: [Int]) throws -> [Int] {
        guard indexes.count <= chars.count else { throw GeometryError.invalidSelection }
        return try indexes.map { index in
            guard let value = positions[index] else { throw GeometryError.invalidSelection }; return value
        }.sorted()
    }

    private func selected(_ start: Int, _ end: Int, keep: Set<Int>? = nil) -> Result? {
        guard chars.indices.contains(start), chars.indices.contains(end), start <= end else { return nil }
        let accepts = filter(start, end)
        let values = (start...end).filter { !chars[$0].space && accepts(chars[$0]) && (keep?.contains($0) ?? true) }
        if values.isEmpty { return nil }
        let exact = Set(values), text = text(start, end, keep: exact)
        let range = sentenceRange(start, end), sentence = String(self.text(range.0, range.1).prefix(600))
        return Result(indexes: values.map { chars[$0].index }, text: text,
                      sentence: Self.strip(sentence) == Self.strip(text) ? "" : sentence,
                      rects: visualRects(start, end, keep: exact))
    }

    private func filter(_ start: Int, _ end: Int) -> (Glyph) -> Bool {
        let first = chars[start], last = chars[end]
        var allowed: Set<Int>?
        if blockFilter && first.blockID >= 0 && last.blockID >= 0 {
            if first.blockID == last.blockID { allowed = [first.blockID] }
            else {
                let relevant = Set(chars[start...end].map(\.blockID))
                allowed = connected(blocks.filter { relevant.contains($0.key) }, seeds: [first.blockID, last.blockID])
            }
        }
        let cell = regions[first.index] == regions[last.index] ? regions[first.index] : nil
        return { char in
            (allowed == nil || allowed!.contains(char.blockID) || (char.blockID < 0 && char.space)) &&
                (cell == nil || char.space || self.regions[char.index] == cell)
        }
    }

    private static func axis(_ rect: CGRect) -> Axis {
        let w = max(1, rect.width), h = max(1, rect.height)
        return h > w * 1.15 ? .vertical : (w > h * 1.15 ? .horizontal : .ambiguous)
    }
    private static func lineShapes(_ chars: [Glyph]) -> [String: Shape] {
        var result: [String: Shape] = [:]
        for char in chars where char.rect.width > 0 && char.rect.height > 0 {
            guard let key = char.lineKey else { continue }
            var shape = result[key] ?? Shape(); shape.rect = shape.rect.union(char.rect)
            if char.vertical == true { shape.vertical += 1 }
            if char.vertical == false { shape.horizontal += 1 }
            shape.axis = shape.vertical > 0 && shape.horizontal == 0 ? .vertical
                : (shape.horizontal > 0 && shape.vertical == 0 ? .horizontal : axis(shape.rect))
            result[key] = shape
        }
        return result
    }
    private static func blockShapes(_ chars: [Glyph], lines: [String: Shape]) -> [Int: Shape] {
        var result: [Int: Shape] = [:]
        for char in chars where char.blockID >= 0 {
            var shape = result[char.blockID] ?? Shape(); shape.rect = shape.rect.union(char.rect)
            if !char.space && char.rect.width > 0 { shape.widths.append(char.rect.width) }
            if !char.space && char.rect.height > 0 { shape.heights.append(char.rect.height) }
            let line = char.lineKey.flatMap { lines[$0] }
            if line != nil { shape.hasLines = true }
            if line?.axis == .vertical || (line == nil && char.vertical == true) { shape.vertical += 1 }
            if line?.axis == .horizontal || (line == nil && char.vertical == false) { shape.horizontal += 1 }
            result[char.blockID] = shape
        }
        for (key, var shape) in result {
            let geometry = axis(shape.rect)
            let legacy: Axis = geometry == .vertical && shape.rect.width > shape.width * 2.2 && shape.widths.count > 3 ? .horizontal : geometry
            shape.axis = shape.vertical > 0 && shape.horizontal == 0 ? .vertical
                : (shape.horizontal > 0 && shape.vertical == 0 ? .horizontal : (shape.hasLines ? geometry : legacy))
            result[key] = shape
        }
        return result
    }
    private static func overlap(_ a0: CGFloat, _ a1: CGFloat, _ b0: CGFloat, _ b1: CGFloat) -> CGFloat {
        max(0, min(a1, b1) - max(a0, b0)) / max(1, min(a1-a0, b1-b0))
    }
    private static func gap(_ a0: CGFloat, _ a1: CGFloat, _ b0: CGFloat, _ b1: CGFloat) -> CGFloat {
        max(0, max(a0, b0) - min(a1, b1))
    }
    private func connected(_ blocks: [Int: Shape], seeds: Set<Int>) -> Set<Int> {
        var allowed = seeds, grew = true
        while grew {
            grew = false
            for (id, shape) in blocks where !allowed.contains(id) {
                for otherID in allowed {
                    guard let other = blocks[otherID] else { continue }
                    let a = shape.rect, b = other.rect
                    let horizontal = shape.axis != .vertical && other.axis != .vertical &&
                        Self.overlap(a.minX, a.maxX, b.minX, b.maxX) >= 0.3 && Self.gap(a.minY, a.maxY, b.minY, b.maxY) <= max(shape.height, other.height) * 1.8
                    let tolerance: CGFloat = shape.axis == .vertical && other.axis == .vertical ? 1.8 : 0.6
                    let vertical = shape.axis != .horizontal && other.axis != .horizontal &&
                        Self.overlap(a.minY, a.maxY, b.minY, b.maxY) >= 0.3 && Self.gap(a.minX, a.maxX, b.minX, b.maxX) <= max(shape.width, other.width) * tolerance
                    if horizontal || vertical { allowed.insert(id); grew = true; break }
                }
            }
        }
        return allowed
    }

    private static func matches(_ text: String, _ pattern: String) -> Bool { text.range(of: pattern, options: .regularExpression) != nil }
    private static func cjk(_ text: String) -> Bool { matches(text, "[぀-ヿ㐀-鿿　-〿＀-￯]") }
    private static func strip(_ text: String) -> String { text.filter { !$0.isWhitespace } }
    private func text(_ start: Int, _ end: Int, keep: Set<Int>? = nil) -> String {
        let accepts = filter(start, end)
        var output = "", last: Glyph?, real: Glyph?, pending = false
        for i in start...end {
            let char = chars[i]
            if !accepts(char) || (!char.space && !(keep?.contains(i) ?? true)) { continue }
            if let previous = last, !char.space {
                let priorReal = real ?? previous, pair = Self.cjk(char.text) && Self.cjk(priorReal.text)
                if abs(char.rect.minY - priorReal.rect.minY) > char.rect.height * 0.5 {
                    if !pair { output += "\n" }
                } else {
                    let tolerance: CGFloat = Self.matches(char.text, "[A-Za-z]") && Self.matches(previous.text, "[A-Za-z]") ? 1.3 : 0.6
                    if !pair && !previous.space && char.rect.minX - previous.rect.maxX > min(char.rect.height, previous.rect.height) * tolerance { output += " " }
                }
            }
            if char.space { pending = true }
            else {
                if pending && !(Self.cjk(char.text) && real.map { Self.cjk($0.text) } == true) { output += " " }
                pending = false; output += char.text; real = char
            }
            last = char
        }
        return output.replacingOccurrences(of: "[ \\t]+", with: " ", options: .regularExpression)
            .replacingOccurrences(of: " ?\\n ?", with: "\n", options: .regularExpression).trimmingCharacters(in: .whitespacesAndNewlines)
    }
    private func sentenceRange(_ start: Int, _ end: Int) -> (Int, Int) {
        func stop(_ a: Glyph, _ b: Glyph) -> Bool {
            let dy = abs(a.rect.minY - b.rect.minY), height = max(a.rect.height, b.rect.height)
            if a.block >= 0 && a.block == b.block { return dy > height * 3 }
            return (a.block >= 0 && b.block >= 0 && a.block != b.block && dy > height * 0.5) || dy > height * 1.5
        }
        func item(_ s: String) -> Bool { Self.matches(s, "[①-⑳⓪Ⓐ-ⓩ㉑-㉟㊱-㊿⓵-⓾]") }
        var a = start, b = end
        while a > 0 {
            if Self.matches(chars[a-1].text, "[.!?。！？]") || item(chars[a].text) { break }
            if item(chars[a-1].text) { a -= 1; break }
            if stop(chars[a-1], chars[a]) { break }; a -= 1
        }
        while b < chars.count - 1 {
            if Self.matches(chars[b].text, "[.!?。！？]") || item(chars[b+1].text) || stop(chars[b], chars[b+1]) { break }
            b += 1
        }
        return (a, b)
    }
    private func wordEdge(_ index: Int, step: Int) -> Int {
        var i = index
        func word(_ text: String) -> Bool { Self.matches(text, "[A-Za-z0-9_]") }
        if chars[i].word != -1 {
            let id = chars[i].word
            while chars.indices.contains(i + step) && chars[i+step].word == id && !["/", "|", "／"].contains(chars[i+step].text) { i += step }
            while chars.indices.contains(i - step) && chars[i-step].word == id && !word(chars[i].text) && !Self.cjk(chars[i].text) { i -= step }
        } else if word(chars[i].text) {
            while chars.indices.contains(i + step) && word(chars[i+step].text) {
                let a = chars[min(i, i+step)].rect, b = chars[max(i, i+step)].rect
                if abs(a.minY - b.minY) > chars[i].rect.height * 0.5 || b.minX - a.maxX > chars[i].rect.height * 0.8 { break }; i += step
            }
        }
        return i
    }

    private struct Visual { var rect: CGRect; let axis: Axis, line: String?; var first: Int }
    private func visualRects(_ start: Int, _ end: Int, keep: Set<Int>) -> [CGRect] {
        let accepts = filter(start, end)
        var entries: [Visual] = [], pending: [Visual] = [], lastReal: Visual?, lastRects: [String: CGRect] = [:]
        for i in start...end {
            let char = chars[i]
            if !accepts(char) || (!char.space && !keep.contains(i)) { continue }
            let line = char.lineKey.flatMap { lines[$0] }
            let axis: Axis = line != nil && line!.axis != .ambiguous ? line!.axis : (blocks[char.blockID]?.axis == .vertical ? .vertical : .horizontal)
            let key = char.lineKey ?? "block:\(char.blockID)"
            var rect = char.rect
            if (rect.width <= 0 || rect.height <= 0 || rect.width < 0.5) && char.space, let previous = lastRects[key] {
                rect = axis == .vertical
                    ? CGRect(x: previous.minX, y: previous.maxY, width: previous.width, height: max(1, previous.width) * 0.3)
                    : CGRect(x: previous.maxX, y: previous.minY, width: max(1, previous.height) * 0.3, height: previous.height)
            }
            if rect.width <= 0 || rect.height <= 0 { continue }
            let entry = Visual(rect: rect, axis: axis, line: char.lineKey, first: i)
            if char.space { if lastReal != nil { pending.append(entry) } }
            else {
                if let previous = lastReal {
                    for space in pending {
                        let a = previous.rect, s = space.rect, b = rect, vertical = axis == .vertical
                        let sameRow = vertical
                            ? Self.overlap(a.minX, a.maxX, s.minX, s.maxX) >= 0.35 && Self.overlap(b.minX, b.maxX, s.minX, s.maxX) >= 0.35
                            : Self.overlap(a.minY, a.maxY, s.minY, s.maxY) >= 0.35 && Self.overlap(b.minY, b.maxY, s.minY, s.maxY) >= 0.35
                        let epsilon = max(1, (vertical ? b.width : b.height) * 0.5)
                        let between = vertical ? (s.minY >= a.maxY-epsilon && s.maxY <= b.minY+epsilon) : (s.minX >= a.maxX-epsilon && s.maxX <= b.minX+epsilon)
                        if sameRow && between { entries.append(space) }
                    }
                }
                pending = []; entries.append(entry); lastReal = entry
            }
            lastRects[key] = rect
        }
        var groups: [Visual] = []
        for entry in entries {
            var chosen: Int?, score = -CGFloat.infinity
            for (i, group) in groups.enumerated() {
                if entry.axis != group.axis || (entry.line != nil && group.line != nil && entry.line != group.line) { continue }
                let a = group.rect, b = entry.rect, vertical = entry.axis == .vertical
                let overlap = vertical ? Self.overlap(a.minX,a.maxX,b.minX,b.maxX) : Self.overlap(a.minY,a.maxY,b.minY,b.maxY)
                let gap = vertical ? Self.gap(a.minY,a.maxY,b.minY,b.maxY) : Self.gap(a.minX,a.maxX,b.minX,b.maxX)
                if overlap < 0.35 || gap > max(2, (vertical ? max(a.width,b.width) : max(a.height,b.height)) * 0.6) { continue }
                if overlap * 1000-gap > score { chosen = i; score = overlap * 1000-gap }
            }
            if let chosen { groups[chosen].rect = groups[chosen].rect.union(entry.rect); groups[chosen].first = min(groups[chosen].first, entry.first) }
            else { groups.append(entry) }
        }
        return groups.sorted { $0.first < $1.first }.map(\.rect)
    }

    func binding(_ want: [String: Any]) throws -> Result? {
        let text = want["text"] as? String ?? "", needle = Self.strip(text)
        guard text.utf16.count <= 16_000 else { throw GeometryError.invalidSelection }
        let ordered = chars.sorted { $0.index < $1.index }
        func result(_ values: [Glyph], quality: String, count: Int = 1) -> Result? {
            let real = values.filter { !$0.space }
            if real.isEmpty { return nil }
            // Stored card frames historically group by baseline, not selection
            // order. Keep this projection distinct from the drag overlay.
            let sorted = real.sorted {
                let d = $0.rect.maxY-$1.rect.maxY
                return abs(d) > max($0.rect.height,$1.rect.height,1)*0.6 ? d < 0 : $0.rect.minX < $1.rect.minX
            }
            var rects: [CGRect] = [], baseline: CGFloat?
            for char in sorted {
                if let base = baseline, abs(base-char.rect.maxY) < char.rect.height*0.6 {
                    rects[rects.count-1] = rects[rects.count-1].union(char.rect)
                } else { rects.append(char.rect); baseline = char.rect.maxY }
            }
            return Result(indexes: real.map(\.index), text: text.isEmpty ? real.map(\.text).joined() : text,
                          sentence: "", rects: rects, quality: quality, matches: count)
        }
        if let indexes = want["ois"] as? [Int], !indexes.isEmpty {
            _ = try mappedIndexes(indexes)
            let set = Set(indexes), picked = ordered.filter { set.contains($0.index) }
            if needle.isEmpty || Self.strip(picked.filter { !$0.space }.map(\.text).joined()) == needle { return result(picked, quality: "exact-set") }
        }
        let from = want["from"] as? Int, to = want["to"] as? Int
        let hasRange = from != nil && to != nil
        if let from, let to {
            let picked = ordered.filter { $0.index >= from && $0.index <= max(from, to) }
            if !picked.isEmpty && (needle.isEmpty || Self.strip(picked.filter { !$0.space }.map(\.text).joined()) == needle) { return result(picked, quality: "exact") }
        }
        if needle.isEmpty { return nil }
        var blockNumbers: [Int: Int] = [:]
        for char in ordered where !char.space && !char.text.isEmpty && blockNumbers[char.block] == nil { blockNumbers[char.block] = blockNumbers.count+1 }
        func search(_ stream: [Glyph], quality: String) -> Result? {
            var joined = "", offsets: [Int] = []
            for (i, char) in stream.enumerated() where !char.space {
                joined += char.text; offsets.append(contentsOf: repeatElement(i, count: char.text.utf16.count))
            }
            let haystack = joined as NSString, target = needle as NSString
            var cursor = 0, count = 0, best: NSRange?, distance = Int.max
            while cursor <= haystack.length-target.length {
                let range = haystack.range(of: needle, range: NSRange(location: cursor, length: haystack.length-cursor))
                if range.location == NSNotFound { break }
                count += 1
                let d = hasRange ? abs(stream[offsets[range.location]].index-from!) : 0
                if d < distance { distance = d; best = range }
                cursor = range.location+1
            }
            guard let best else { return nil }
            return result(Array(stream[offsets[best.location]...offsets[best.location+best.length-1]]), quality: quality, count: count)
        }
        let block = want["block"] as? Int ?? 0
        if block > 0 {
            if let indexes = numberedRegions[block], let found = search(ordered.filter { indexes.contains($0.index) }, quality: "by-block") { return found }
            if let found = search(ordered.filter { blockNumbers[$0.block] == block }, quality: "by-block") { return found }
        }
        if let found = search(ordered, quality: block > 0 ? "by-text-block-missed" : "by-text") {
            return block > 0 && !hasRange && found.matches > 1 ? nil : found
        }
        // Same-cell source stream handles a word split across table rows while
        // another column interrupts the page source order.
        for key in Set(regions.values).sorted() {
            if let found = search(ordered.filter { regions[$0.index] == key }, quality: "by-text-region") { return found }
        }
        return nil
    }
}
