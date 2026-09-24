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
}
