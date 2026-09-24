import Foundation
import CoreGraphics

/// Assistant input is a projection of one native book snapshot and its source
/// character geometry. It never needs a page image or hidden web elements.
enum ReaderNativePDFContext {
    typealias O = [String:Any]
    typealias F = ReaderNativeAssistantEdits.Failure

    static func pages(_ context: O) -> [Int] {
        var result: [Int] = []
        for value in Array((context["pages"] as? [Any] ?? []).prefix(12)) + [context["page"] ?? NSNull()] {
            if let page = integer(value), (1...10_000_000).contains(page), !result.contains(page) { result.append(page) }
        }
        return result
    }

    private static func integer(_ value: Any?) -> Int? {
        let number: Double?
        if let value = value as? String { number = Double(value) }
        else { number = (value as? NSNumber)?.doubleValue }
        guard let number, number.isFinite, number.rounded() == number, abs(number) <= 9_007_199_254_740_991 else { return nil }
        return Int(number)
    }

    static func geometry(_ source: O) throws -> ReaderNativePDFTextGeometry {
        var value = source
        value["page_w"] = source["pageWidth"]; value["page_h"] = source["pageHeight"]
        value["character_geometry"] = source["characterGeometry"]
        return try ReaderNativePDFTextGeometry(page:value)
    }

    /// The native visible markers and assistant use the same source-space
    /// ordering, so zooming cannot renumber a card between query and action.
    static func ordered(_ rectangles: [[CGRect]]) -> [Int] {
        let tolerance = max(6, (rectangles.compactMap { $0.first?.height }.max() ?? 0) * 0.5)
        return rectangles.indices.sorted { a,b in
            let first = rectangles[a].last ?? .zero, second = rectangles[b].last ?? .zero
            if abs(first.minY - second.minY) > tolerance { return first.minY < second.minY }
            if first.maxX != second.maxX { return first.maxX < second.maxX }
            return a < b
        }
    }

    private static func plain(_ value: Any?, limit: Int = 100_000) -> String {
        String(decoding:ReaderNativePageCardPlan.plain(value as? String ?? "").utf16.prefix(limit),as:UTF16.self)
    }
    private static func first(_ values: Any?...) -> Any? {
        for case let value? in values where !(value is NSNull) { return value }; return nil
    }
    private static func nonempty(_ values: Any?...) -> String {
        values.compactMap { $0 as? String }.first { !$0.isEmpty } ?? ""
    }
    private static func text(_ note: O, _ payload: O, anki: Bool) -> String {
        let supplied = plain(nonempty(payload["contextText"],payload["context_text"]))
        if !supplied.isEmpty { return supplied }
        if anki {
            var rows: [String] = []
            for face in payload["cards"] as? [O] ?? [] {
                let main = first(face["front"],face["question"],face["q"])
                let front = plain(main ?? first(face["cloze"],face["text"]))
                let back = plain(first(face["back"],face["answer"],face["a"]))
                let cloze = main == nil ? "" : plain(face["cloze"])
                let pieces = [front,back,cloze].filter { !$0.isEmpty }
                if !pieces.isEmpty { rows.append(pieces.joined(separator:" / ")) }
                if rows.reduce(0,{ $0 + $1.utf16.count }) >= 100_000 { break }
            }
            return plain(nonempty(rows.joined(separator:"\n"),payload["text"],note["text"]))
        }
        return plain(nonempty(payload["text"],payload["content"],note["text"]))
    }

    static func cards(notes: [O], page: Int, geometry: ReaderNativePDFTextGeometry) throws -> [O] {
        var anchored: [O] = [], free: [O] = [], boxes: [[CGRect]] = [], seen = Set<String>()
        for note in notes {
            let anki = note["card"] is O
            guard let payload = (anki ? note["card"] : note["html"]) as? O else { continue }
            let binding = payload["bind"] as? O
            let pageBound = binding?["kind"] as? String == "page-chars"
            if pageBound && integer(binding?["page"]) != page { continue }
            let anchor = note["anchor"] as? O
            let belongs = anchor?["kind"] as? String == "pdf" && integer(anchor?["page"]) == page
            guard pageBound || belongs else { continue }
            let id = nonempty(note["id"],note["noteId"])
            guard !id.isEmpty, id.count <= 240, seen.insert(id).inserted else { throw F("页面卡片编号缺失或重复") }
            var row: O = ["id":id,"kind":anki ? "anki" : "card",
                "label":plain(nonempty(binding?["text"],payload["label"],payload["title"],payload["gid"],payload["cid"],id),limit:120),
                "text":text(note,payload,anki:anki)]
            if anki {
                let learningID = nonempty(payload["gid"],payload["cid"])
                if !learningID.isEmpty { row["learning"] = ["id":learningID] }
            }
            if pageBound {
                guard let binding, let value = try? geometry.binding(binding), !value.rects.isEmpty,
                      let from = value.indexes.min(), let to = value.indexes.max() else { continue }
                row["bind"] = ["kind":"page-chars","page":page,"from":from,"to":to,"text":plain(binding["text"],limit:200)]
                row["unbound"] = false
                anchored.append(row); boxes.append(value.rects)
            } else {
                row["bind"] = NSNull(); row["number"] = NSNull(); row["unbound"] = true; free.append(row)
            }
            guard anchored.count + free.count <= 2000 else { throw F("页面卡片过多，无法完整编号") }
        }
        return ordered(boxes).enumerated().map { number,index in
            var row = anchored[index]; row["number"] = number + 1; return row
        } + free
    }

    static func prepare(_ input: O, authority: O, sources: [Int:O]) throws -> O {
        guard authority["contract"] as? String == "reader-native-pdf-assistant-state/1",
              let file = authority["file"] as? String, let revisions = authority["revisions"] as? O,
              let notes = authority["notes"] as? [O] else { throw F("PDF 助手书籍状态无效") }
        var body = input, context = input["context"] as? O ?? [:], snapshot = authority
        let requested = pages(context)
        var projected: O = [:], complete = !requested.isEmpty
        for page in requested {
            guard let source = sources[page], let geometry = try? geometry(source),
                  let rows = try? cards(notes:notes,page:page,geometry:geometry) else { complete = false; continue }
            projected[String(page)] = rows
        }
        if complete {
            snapshot["page_cards"] = ["contract":"reader-native-page-card-projection/1",
                "revision":revisions["notes"] ?? NSNull(),"pages":projected]
        } else { snapshot.removeValue(forKey:"page_cards") }
        if (context["visible_text"] as? String ?? "").trimmingCharacters(in:.whitespacesAndNewlines).isEmpty,
           let page = integer(context["page"]) ?? requested.first, let source = sources[page],
           let geometry = try? geometry(source), let chars = source["chars"] as? [O] {
            if !chars.isEmpty, let value = try? geometry.range(from:0,to:chars.count-1) {
                context["visible_text"] = String(decoding:value.text.utf16.prefix(4000),as:UTF16.self)
            }
            context["native_page_text"] = ["state":"ready","source":source["source"] ?? "none","page":page]
        }
        guard try JSONSerialization.data(withJSONObject:snapshot).count <= 6 * 1024 * 1024 else { throw F("本机 PDF 批注状态过大，未截断发送") }
        context["file_rel"] = file; context["native_local_state"] = snapshot; body["context"] = context
        return body
    }

    /// ReaderPC receives the same source-space block numbers and card markers
    /// as the native reading view. This projection does not request page images.
    static func readerPC(_ current:O, authority:O, sources:[Int:O]) throws -> O {
        guard current["kind"] as? String == "pdf", let file = authority["file"] as? String,
              current["file"] as? String == file, let page = integer(current["page"]), page > 0,
              let source = sources[page], let notes = authority["notes"] as? [O] else { throw F("阅读状态已改变") }
        let selection = current["selectionState"] as? String == "active" || !(current["selection"] as? String ?? "").trimmingCharacters(in:.whitespacesAndNewlines).isEmpty
        let rows = try cards(notes:notes,page:page,geometry:geometry(source))
        let revision = (authority["revisions"] as? O)?["notes"] ?? NSNull()
        var middle = try ContextText(source), sections:[String] = []
        var inserts:[Int:[String]] = [:], unbound:[String] = []
        var unboundSize = 0, omitted = false
        for row in rows {
            let marker = contextCard(row,revision:revision,layout:middle.structured && row["unbound"] as? Bool != true)
            if row["unbound"] as? Bool == true {
                if unboundSize + marker.utf16.count + 1 > 220_000 / 3 { omitted = true; continue }
                unbound.append(marker); unboundSize += marker.utf16.count + 1
            } else if let bind = row["bind"] as? O, let end = integer(bind["to"]),
                      middle.after.indices.contains(end), let offset = middle.after[end] {
                inserts[offset,default:[]].append(marker)
            }
        }
        let visible = escaped((current["visibleText"] as? String ?? "").trimmingCharacters(in:.whitespacesAndNewlines))
        let matched = visible.isEmpty ? NSRange(location:NSNotFound,length:0) : (middle.text as NSString).range(of:visible)
        if !middle.structured, matched.location != NSNotFound {
            let end = (middle.text as NSString).length
            var before:[String] = [], after:[String] = []
            if !selection, let source = sources[page-1] { before.append(suffixUTF16(try ContextText(source).text,2200)) }
            before.append(middle.annotated(max(0,matched.location-1800),matched.location,inserts:inserts))
            after.append(middle.annotated(NSMaxRange(matched),min(end,NSMaxRange(matched)+1800),inserts:inserts))
            if !selection, let source = sources[page+1] { after.append(prefixUTF16(try ContextText(source).text,2200)) }
            if before.contains(where:{ !$0.isEmpty }) { sections.append("【当前显示区域之前】\n" + before.filter { !$0.isEmpty }.joined(separator:"\n")) }
            sections.append("【当前显示区域（重点）】\n" + middle.annotated(matched.location,NSMaxRange(matched),inserts:inserts))
            if after.contains(where:{ !$0.isEmpty }) { sections.append("【当前显示区域之后】\n" + after.filter { !$0.isEmpty }.joined(separator:"\n")) }
        } else {
        middle.insert(inserts)
        if !selection, page > 1, let previous = sources[page-1] {
            let text = try ContextText(previous).text
            if !text.isEmpty { sections.append("【当前页之前】\n" + suffixUTF16(text,2200)) }
        }
        if !middle.text.isEmpty {
            sections.append((middle.structured
                ? "【当前页结构化文字（Markdown；按 [NN] 编号顺序阅读；Markdown 字符位置不可用作 bind 下标）】\n"
                : "【当前页文字（视口范围暂不可精确定位）】\n") + middle.text)
        }
        if middle.structured { sections.append("【锚点下标从哪来】上面的 Markdown 是排版投影，字符位置不能当作 bind 的 from/to。需要下标时，从 reader_page_text 的 segments 取原文闭区间。") }
        else if source["layout"] is O { sections.append("【布局提示】布局信息置信度不足，已按原字符顺序提供正文；如需确认空间关系，可按需调用 reader_visual_image。") }
        if !selection, let following = sources[page+1] {
            let text = try ContextText(following).text
            if !text.isEmpty { sections.append("【当前页之后】\n" + prefixUTF16(text,2200)) }
        }
        }
        if !unbound.isEmpty { sections.append("【当前页未锚定卡片（不参与正文及右侧标记序号）】\n" + unbound.joined(separator:"\n")) }
        let bounded = boundedContext(sections.joined(separator:"\n\n"))
        return ["kind":"pdf","file":file,"page":page,"title":current["title"] as? String ?? "",
            "text":bounded.text,"textAvailable":!bounded.text.trimmingCharacters(in:.whitespacesAndNewlines).isEmpty,
            "textSource":middle.structured ? "app-local-structured-layout" : "app-local-visible-window",
            "fallbackReason":bounded.text.isEmpty ? "本机文字层尚未提供当前页文字" as Any : NSNull(),
            "truncated":bounded.truncated || omitted]
    }

    private static func prefixUTF16(_ value:String,_ count:Int) -> String {
        let units = Array(value.utf16.prefix(count)); var end = units.count
        if end > 0, (0xD800...0xDBFF).contains(units[end-1]) { end -= 1 }
        return String(decoding:units.prefix(end),as:UTF16.self)
    }
    private static func suffixUTF16(_ value:String,_ count:Int) -> String {
        let units = Array(value.utf16.suffix(count)); var start = 0
        if let first = units.first, (0xDC00...0xDFFF).contains(first) { start = 1 }
        return String(decoding:units.dropFirst(start),as:UTF16.self)
    }
    private static func escaped(_ value:String,layout:Bool = false) -> String {
        var result = value.replacingOccurrences(of:"\\",with:"\\\\")
            .replacingOccurrences(of:"⟦",with:"\\⟦").replacingOccurrences(of:"⟧",with:"\\⟧")
        if layout { result = result.replacingOccurrences(of:"|",with:"\\|") }
        return result
    }
    private static func contextCard(_ row:O,revision:Any,layout:Bool) -> String {
        func attr(_ value:Any?,_ limit:Int) -> String {
            prefixUTF16(value is NSNull ? "" : value.map { String(describing:$0) } ?? "",limit)
                .replacingOccurrences(of:#"\s+"#,with:" ",options:.regularExpression).trimmingCharacters(in:.whitespacesAndNewlines)
                .replacingOccurrences(of:"\"",with:"'").replacingOccurrences(of:"⟦",with:"").replacingOccurrences(of:"⟧",with:"")
        }
        let unbound = row["unbound"] as? Bool == true
        let anchor = unbound ? "\" unbound=\"true" : "\" anchor=\"" + attr((row["bind"] as? O)?["text"],120)
        let body = row["text"] as? String ?? ""
        var text = "⟦CARD_START n=\"" + (unbound ? "" : attr(row["number"],20)) + "\" id=\"" + attr(row["id"],120)
        text += "\" revision=\"" + attr(revision,30) + "\" type=\"" + attr(row["kind"],32) + "\" label=\"" + attr(row["label"],120)
        if let learning = (row["learning"] as? O)?["id"] { text += "\" learning=\"" + attr(learning,80) }
        text += anchor + "\"⟧" + escaped(body.isEmpty ? "（这张卡片没有可读文字；需要完整源内容时请按 ID 读取）" : body) + "⟦CARD_END⟧"
        return layout ? text.replacingOccurrences(of:"|",with:"\\|").replacingOccurrences(of:#"\r\n?|\n"#,with:"<br>",options:.regularExpression) : text
    }
    private static func boundedContext(_ input:String) -> (text:String,truncated:Bool) {
        let units = Array(input.utf16); var cut = min(units.count,220_000)
        func slice(_ end:Int) -> String { prefixUTF16(input,end) }
        func bytes(_ end:Int) -> Int { (try? JSONSerialization.data(withJSONObject:slice(end),options:[.fragmentsAllowed,.withoutEscapingSlashes]).count) ?? Int.max }
        if bytes(cut) > 224 * 1024 {
            var lo = 0, hi = cut
            while lo < hi { let mid = lo + (hi-lo+1)/2; if bytes(mid) <= 224*1024 { lo = mid } else { hi = mid-1 } }
            cut = lo
        }
        if cut >= units.count { return (input,false) }
        let value = input as NSString; var index = 0
        while index < cut {
            if units[index] == 92 { if index+1 >= cut { cut = index; break }; index += 2; continue }
            if units[index] == 0x27E6, value.substring(with:NSRange(location:index,length:min(11,value.length-index))).hasPrefix("⟦CARD_START") {
                let end = value.range(of:"⟦CARD_END⟧",range:NSRange(location:index,length:value.length-index))
                if end.location == NSNotFound || NSMaxRange(end) > cut { cut = index; break }
                index = NSMaxRange(end); continue
            }
            index += 1
        }
        return (slice(cut),true)
    }

    private struct ContextText {
        var text = "", after:[Int?], structured = false
        private var length = 0
        mutating func append(_ value:String) { text += value; length += value.utf16.count }
        let chars:[O]
        init(_ source:O) throws {
            try Task.checkCancellation()
            guard let values = source["chars"] as? [O], values.count <= 100_000 else { throw F("页面字符数据不完整") }
            chars = values; after = Array(repeating:nil,count:values.count)
            if let layout = source["layout"] as? O, layout["textSource"] as? String == "vision", layout["confidence"] as? String == "high",
               ["manga","table"].contains(layout["mode"] as? String ?? ""), let regions = layout["regions"] as? [O], !regions.isEmpty {
                structured = true
                try renderLayout(layout,regions:regions)
                if after.contains(where:{ $0 == nil }) { throw F("页面布局未完整覆盖字符") }
            } else {
                var pending = ""
                for (i, item) in chars.enumerated() {
                    for char in (item["c"] as? String ?? "").replacingOccurrences(of:"\0",with:"").replacingOccurrences(of:#"\r\n?"#,with:"\n",options:.regularExpression) {
                        if char == " " || char == "\t" { if !pending.contains("\n") { pending = " " }; continue }
                        if char == "\n" { pending = pending.contains("\n") ? "\n\n" : "\n"; continue }
                        if !text.isEmpty { append(pending) }; pending = ""; append(String(char))
                    }
                    after[i] = length
                }
                // Escape while preserving the source-index insertion offsets.
                let original = text as NSString; var cursor = 0, output = "", outputLength = 0
                for i in after.indices {
                    let end = after[i] ?? cursor
                    let piece = escaped(original.substring(with:NSRange(location:cursor,length:end-cursor)))
                    output += piece; outputLength += piece.utf16.count
                    after[i] = outputLength; cursor = end
                }
                text = output; length = outputLength
            }
        }
        mutating func insert(_ inserts:[Int:[String]]) {
            text = annotated(0,(text as NSString).length,inserts:inserts)
        }
        func annotated(_ start:Int,_ end:Int,inserts:[Int:[String]]) -> String {
            let source = text as NSString; var lower = start, upper = end
            if lower < source.length, (0xDC00...0xDFFF).contains(source.character(at:lower)) { lower += 1 }
            if upper > lower, (0xD800...0xDBFF).contains(source.character(at:upper-1)) { upper -= 1 }
            var cursor = lower, value = ""
            for offset in inserts.keys.sorted() where offset > lower && offset <= upper {
                value += source.substring(with:NSRange(location:cursor,length:offset-cursor)) + inserts[offset]!.joined()
                cursor = offset
            }
            return value + source.substring(with:NSRange(location:cursor,length:upper-cursor))
        }
        mutating func label(_ region:O) { append(String(format:"[%02d] ",(region["order"] as? Int ?? 0)+1)) }
        mutating func region(_ region:O) throws {
            try Task.checkCancellation()
            guard let ranges = region["ranges"] as? [[Int]] else { throw F("页面块范围缺失") }
            var wrote = false, last:O?
            func cjk(_ value:Character?) -> Bool { value.map { String($0).range(of:#"[\u3000-\u30ff\u3400-\u9fff\uf900-\ufaff\uff00-\uffef]"#,options:.regularExpression) != nil } ?? false }
            for (r, range) in ranges.enumerated() {
                guard range.count == 2, range[0] >= 0, range[1] >= range[0], range[1] < chars.count else { throw F("页面块范围越界") }
                var pending:[Int] = [], lineWrote = false
                for index in range[0]...range[1] {
                    let source = chars[index], value = (source["c"] as? String ?? "").replacingOccurrences(of:"\0",with:"")
                        .replacingOccurrences(of:#"\s+"#,with:" ",options:.regularExpression).trimmingCharacters(in:.whitespacesAndNewlines)
                    if value.isEmpty { pending.append(index); continue }
                    let joined = cjk(value.first) && cjk((last?["c"] as? String)?.last)
                    if !pending.isEmpty, wrote || lineWrote, !joined { append(" ") }
                    for i in pending { after[i] = length }; pending = []
                    if r > 0, !lineWrote, wrote {
                        let previousWord = last?["w"] as? Int
                        if previousWord == nil || previousWord! < 0 || source["w"] as? Int != previousWord { append("<br>") }
                    }
                    append(escaped(value,layout:true)); after[index] = length
                    last = source; lineWrote = true; wrote = true
                }
                for i in pending { after[i] = length }
            }
        }
        mutating func renderLayout(_ layout:O,regions:[O]) throws {
            func ordered(_ values:[O]) -> [O] { values.sorted { ($0["order"] as? Int ?? 0) < ($1["order"] as? Int ?? 0) } }
            func bounds(_ row:O) -> [Double] { let b = row["bounds"] as? [Double] ?? []; return b.count == 4 ? b : [0,0,0,0] }
            if layout["mode"] as? String == "manga" {
                let main = regions.filter { $0["kind"] as? String != "vision-supplement" }
                let width = main.map { bounds($0)[2] }.max() ?? 0
                let prose = main.count >= 2 && width > 0 && Double(main.filter { bounds($0)[2]-bounds($0)[0] >= width*0.5 }.count)/Double(main.count) >= 0.4
                if prose {
                    var previous:O?
                    for item in ordered(regions) {
                        if let previous { let a = bounds(previous), b = bounds(item); append(min(a[3],b[3])-max(a[1],b[1]) > 0 && b[0] >= a[0] ? " " : "\n") }
                        label(item); try region(item); previous = item
                    }
                    append("\n"); return
                }
                let rows = layout["gridRows"] as? Int ?? 0
                guard rows > 0, rows <= 10_000 else { throw F("页面网格无效") }
                let cells = Dictionary(grouping:ordered(regions)) { "\($0["gridRow"] ?? ""):\($0["gridColumn"] ?? "")" }
                append("| 左 | 中左 | 中右 | 右 |\n| --- | --- | --- | --- |\n")
                for row in 0..<rows {
                    append("|")
                    for column in 0..<4 {
                        append(" ")
                        for (i,item) in (cells["\(row):\(column)"] ?? []).enumerated() {
                            if i > 0 { append("<br>") }; label(item); try region(item)
                        }
                        append(" |")
                    }
                    append("\n")
                }
                return
            }
            let tables = layout["tables"] as? [O] ?? []
            var blocks = regions.filter { $0["kind"] as? String != "table-cell" }.map { (order:$0["order"] as? Int ?? 0,region:Optional($0),table:Optional<O>.none,regions:[O]()) }
            for table in tables {
                let cells = regions.filter { $0["kind"] as? String == "table-cell" && ($0["tableId"] as? Int) == (table["id"] as? Int) }
                blocks.append((cells.map { $0["order"] as? Int ?? 0 }.min() ?? Int.max,nil,table,cells))
            }
            for (index,block) in blocks.sorted(by:{ $0.order < $1.order }).enumerated() {
                if index > 0 { append("\n") }
                if let item = block.region { label(item); try region(item); append("\n"); continue }
                guard let table = block.table, let rows = table["rows"] as? Int, let columns = table["columns"] as? Int,
                      rows > 0, columns > 0, rows <= 10_000, columns <= 128, rows*columns <= 100_000 else { throw F("页面表格无效") }
                if rows*columns >= 8, Double(block.regions.count)/Double(rows*columns) < 0.4 {
                    for item in ordered(block.regions) { label(item); try region(item); append("\n") }; continue
                }
                let cells = Dictionary(grouping:ordered(block.regions)) { "\($0["row"] ?? ""):\($0["column"] ?? "")" }
                for row in 0..<rows {
                    append("|")
                    for column in 0..<columns {
                        append(" "); var previous:O?
                        for item in cells["\(row):\(column)"] ?? [] {
                            if let previous { let a = bounds(previous), b = bounds(item), overlap = min(a[3],b[3])-max(a[1],b[1]), height = min(a[3]-a[1],b[3]-b[1]); if !(overlap > 0 && height > 0 && overlap >= height/2) { append("<br>") } }
                            try region(item); previous = item
                        }
                        append(" |")
                    }
                    append("\n")
                    if row == 0 { append("|" + Array(repeating:" --- ",count:columns).joined(separator:"|") + "|\n") }
                }
            }
        }
    }
}
