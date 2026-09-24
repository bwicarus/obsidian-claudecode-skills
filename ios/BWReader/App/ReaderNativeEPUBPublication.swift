import Foundation
#if canImport(FoundationXML)
import FoundationXML
#endif

/// EPUB package/TOC data only. Chapter HTML and its anchor offsets are untouched.
enum ReaderNativeEPUBPublication {
    typealias Object = [String: Any]
    private typealias Failure = ReaderNativeEPUBArchive.Failure
    private final class Node {
        enum Part { case text(String), child(Node) }
        let name: String
        let attributes: [String: String]
        var parts: [Part] = []
        init(_ name: String, _ attributes: [String: String] = [:]) {
            self.name = name.split(separator: ":").last.map(String.init) ?? name
            self.attributes = attributes
        }
        var children: [Node] { parts.compactMap { if case .child(let node) = $0 { return node }; return nil } }
        var text: String { parts.map { part in switch part { case .text(let text): return text; case .child(let child): return child.text } }.joined() }
        func all(_ name: String) -> [Node] { children.flatMap { ($0.name == name ? [$0] : []) + $0.all(name) } }
    }
    private final class XML: NSObject, XMLParserDelegate {
        let root = Node("#document")
        var stack: [Node] = []
        var nodes = 0, textBytes = 0
        var failure: Error?
        func parse(_ data: Data) throws -> Node {
            guard data.count <= 8 * 1024 * 1024 else { throw Failure(message: "EPUB 目录文本过大") }
            stack = [root]
            let parser = XMLParser(data: data)
            parser.shouldResolveExternalEntities = false
            parser.delegate = self
            guard parser.parse(), failure == nil, stack.count == 1 else {
                throw failure ?? parser.parserError ?? Failure(message: "EPUB XML 无效")
            }
            return root
        }
        func parser(_ parser: XMLParser, didStartElement name: String, namespaceURI: String?, qualifiedName: String?, attributes: [String: String]) {
            nodes += 1
            guard nodes <= 100_000, stack.count < 128 else {
                failure = Failure(message: "EPUB XML 结构过大"); parser.abortParsing(); return
            }
            let node = Node(name, attributes)
            stack.last?.parts.append(.child(node)); stack.append(node)
        }
        func parser(_ parser: XMLParser, didEndElement name: String, namespaceURI: String?, qualifiedName: String?) {
            if stack.count > 1 { stack.removeLast() }
        }
        func parser(_ parser: XMLParser, foundCharacters string: String) {
            textBytes += string.utf8.count
            guard textBytes <= 8 * 1024 * 1024 else {
                failure = Failure(message: "EPUB XML 文本过大"); parser.abortParsing(); return
            }
            stack.last?.parts.append(.text(string))
        }
        func parser(_ parser: XMLParser, foundCDATA block: Data) {
            self.parser(parser, foundCharacters: String(decoding: block, as: UTF8.self))
        }
    }
    private static func compact(_ node: Node?, limit: Int) -> String {
        let text = (node?.text ?? "").replacingOccurrences(of: "[\\s\\uFEFF]+", with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return String(decoding: text.utf16.prefix(limit), as: UTF16.self)
    }
    private static func path(_ raw: String?, relativeTo base: String = "", link: Bool = false) -> String? {
        guard var raw, !raw.isEmpty else { return nil }
        if link {
            raw = String(raw.prefix { $0 != "#" && $0 != "?" })
            guard !raw.isEmpty, let decoded = raw.removingPercentEncoding else { return nil }
            raw = decoded
        }
        // Match the original loader: a rooted/schemed href is never made local.
        guard ReaderNativeEPUBArchive.canonicalPath(raw) != nil || raw.hasPrefix("..") else { return nil }
        let parent = base.split(separator: "/", omittingEmptySubsequences: false).dropLast().joined(separator: "/")
        return ReaderNativeEPUBArchive.canonicalPath((parent.isEmpty ? "" : parent + "/") + raw)
    }

    static func load(available: Set<Data>, read: (String) throws -> Data) throws -> Object {
        let container = try XML().parse(read("META-INF/container.xml"))
        guard let opfPath = path(container.all("rootfile").first?.attributes["full-path"]) else {
            throw Failure(message: "EPUB 缺少 OPF")
        }
        let opf = try XML().parse(read(opfPath))
        var manifest: [Data: Object] = [:], order: [String] = []
        for item in opf.all("manifest").flatMap({ $0.children.filter { $0.name == "item" } }) {
            let id = item.attributes["id"] ?? ""
            guard !id.isEmpty, let resolved = path(item.attributes["href"], relativeTo: opfPath),
                  available.contains(Data(resolved.utf8)) else { continue }
            if manifest[Data(id.utf8)] == nil { order.append(id) }
            manifest[Data(id.utf8)] = ["id": id, "path": resolved, "mediaType": item.attributes["media-type"] ?? "",
                            "properties": item.attributes["properties"] ?? ""]
        }
        // JavaScript Object.keys lists integer-like IDs before other IDs.
        func integerID(_ id: String) -> UInt32? {
            guard let number = UInt32(id), number < UInt32.max, String(number) == id else { return nil }; return number
        }
        let numeric = order.filter { integerID($0) != nil }.sorted { integerID($0)! < integerID($1)! }
        order = numeric + order.filter { integerID($0) == nil }
        let spines = opf.all("spine")
        let spine: [Object] = spines.flatMap { $0.children.filter { $0.name == "itemref" } }.compactMap { manifest[Data(($0.attributes["idref"] ?? "").utf8)] }
        guard !spine.isEmpty else { throw Failure(message: "EPUB spine 为空") }
        let items = order.compactMap { manifest[Data($0.utf8)] }
        let nav = items.first { item in
            (item["properties"] as? String ?? "").split(whereSeparator: { $0.isWhitespace }).contains("nav") ||
                (item["path"] as? String ?? "").range(of: "(?:^|/)nav\\.x?html?$", options: [.regularExpression, .caseInsensitive]) != nil
        }
        let ncx = manifest[Data((spines.first?.attributes["toc"] ?? "").utf8)] ?? items.first { item in
            (item["mediaType"] as? String ?? "").lowercased() == "application/x-dtbncx+xml" ||
                (item["path"] as? String ?? "").range(of: "(?:^|/)toc\\.ncx$", options: [.regularExpression, .caseInsensitive]) != nil
        }
        func toc(_ item: Object?, ncx: Bool) -> [Object] {
            guard let base = item?["path"] as? String, let data = try? read(base), let doc = try? XML().parse(data) else { return [] }
            let rows: [(String?, Node)]
            if ncx { rows = doc.all("navPoint").compactMap { node in
                guard let label = node.all("text").first else { return nil }
                return (node.all("content").first?.attributes["src"], label)
            } } else {
                let navs = doc.all("nav")
                let target = navs.first { node in
                    (node.attributes["epub:type"] ?? node.attributes["type"] ?? "").split(whereSeparator: { $0.isWhitespace }).contains("toc")
                } ?? navs.first
                rows = target?.all("a").map { ($0.attributes["href"], $0) } ?? []
            }
            var seen = Set<Data>(), output: [Object] = []
            for (href, node) in rows where output.count < 5000 {
                guard let resolved = path(href, relativeTo: base, link: true),
                      let index = spine.firstIndex(where: { ($0["path"] as? String)?.utf8.elementsEqual(resolved.utf8) == true }) else { continue }
                let label = compact(node, limit: 80)
                guard !label.isEmpty, seen.insert(Data("\(index):\(label)".utf8)).inserted else { continue }
                output.append(["label": label, "idx": index])
            }
            return output
        }
        var contents = toc(nav, ncx: false)
        if contents.isEmpty { contents = toc(ncx, ncx: true) }
        if contents.isEmpty {
            contents = spine.enumerated().map { index, item in
                let filename = (item["path"] as? String ?? "").split(separator: "/").last.map(String.init) ?? "章节 \(index + 1)"
                return ["label": String(decoding: (filename.removingPercentEncoding ?? filename).utf16.prefix(80), as: UTF16.self), "idx": index]
            }
        }
        return ["opfPath": opfPath, "manifestItems": items, "spine": spine, "title": compact(opf.all("title").first, limit: 500), "toc": contents]
    }
}
